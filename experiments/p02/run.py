"""P02 only: synthetic corpus -> Ollama -> explicit Psycopg SQL -> evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import time
import unicodedata
import urllib.request

import psycopg

ROOT = Path(__file__).resolve().parent
MODEL = 'qwen3-embedding:0.6b'
DIM = 1024
INSTRUCTION = 'Given a web search query, retrieve relevant passages that answer the query'
# Deliberately no DATABASE_URL: this runner only accesses the disposable P02 DB.
DSN = 'host=127.0.0.1 port=55432 dbname=p02_experiment user=p02_experiment password=p02_disposable_only connect_timeout=5'
SEARCH = '''SELECT id, title, 1 - (embedding <=> %s::vector) AS score
            FROM p02_notes ORDER BY embedding <=> %s::vector, id LIMIT %s'''
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def normalize(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())


def api(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data,
                                     headers={'Content-Type': 'application/json'})
    with HTTP.open(request, timeout=180) as response:
        return json.load(response)


def embed(base, texts):
    start = time.perf_counter()
    response = api(base, '/api/embed', {
        'model': MODEL, 'input': texts, 'truncate': False,
        'keep_alive': '10m', 'options': {'num_ctx': 8192},
    })
    elapsed = time.perf_counter() - start
    raw = response.pop('embeddings')
    if len(raw) != len(texts):
        raise ValueError('Wrong embedding batch length')
    norms, vectors = [], []
    for vector in raw:
        if len(vector) != DIM or not all(math.isfinite(x) for x in vector):
            raise ValueError('Invalid embedding dimension or values')
        norm = math.sqrt(sum(x*x for x in vector))
        if norm <= 0:
            raise ValueError('Zero embedding')
        norms.append(norm)
        vectors.append([x / norm for x in vector])
    return vectors, {'wall_s': elapsed, 'raw_norm_min': min(norms),
                     'raw_norm_max': max(norms), 'response': response}


def vector_sql(vector):
    # This string is a bound VALUE, never interpolated into SQL.
    return '[' + ','.join(map(str, vector)) + ']'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:11435')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reuse', action='store_true', help='Search persisted vectors without reindexing')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new evidence filename; existing evidence is immutable')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    notes = json.loads((ROOT / 'corpus.json').read_text())
    queries = json.loads((ROOT / 'queries.json').read_text())
    assert 20 <= len(notes) <= 30 and len({n['id'] for n in notes}) == len(notes)
    ids = {n['id'] for n in notes}
    assert all(set(q['expected']) <= ids for q in queries)
    protocol = {name: sha(ROOT / name) for name in ('corpus.json', 'queries.json', '001_initial.sql')}
    expected_protocol = json.loads((ROOT / 'protocol.json').read_text())
    assert protocol == expected_protocol['sha256'], 'Predefined corpus/query protocol changed'
    evidence = {'started_utc': datetime.now(timezone.utc).isoformat(),
                'protocol_sha256': protocol, 'model': MODEL, 'dimension': DIM,
                'instruction': INSTRUCTION, 'query_format': 'Instruct: {instruction}\nQuery: {query}',
                'document_format': 'NFC/whitespace-normalized title + newline + body; no instruction',
                'normalization': 'Explicit client L2, float32 pgvector storage, cosine <=>',
                'python': platform.python_version(), 'platform': platform.platform(),
                'psycopg': psycopg.__version__, 'reuse': args.reuse, 'queries': [], 'errors': []}
    start_all = time.perf_counter()
    try:
        evidence['runtime'] = api(args.base_url, '/api/version')
        tags = api(args.base_url, '/api/tags')['models']
        model = next(m for m in tags if m['name'] == MODEL)
        digest = model['digest']
        manifest = json.loads((ROOT / 'model-manifest.json').read_text())
        if digest != manifest['ollama_digest']:
            raise ValueError('Model tag changed: expected the pinned P02 artifact')
        evidence['artifact'] = model
        show = api(args.base_url, '/api/show', {'model': MODEL})
        evidence['model_metadata'] = {k: show.get(k) for k in ('details', 'model_info', 'capabilities', 'parameters')}
        # No DB connection/transaction exists during document inference.
        vectors = None
        if not args.reuse:
            vectors, evidence['document_embedding'] = embed(args.base_url, [
                normalize(n['title']) + '\n' + normalize(n['body']) for n in notes])
        with psycopg.connect(DSN, autocommit=True) as conn:
            identity = conn.execute('SELECT current_database(), current_user, version()').fetchone()
            assert identity[:2] == ('p02_experiment', 'p02_experiment')
            evidence['database'] = {'database': identity[0], 'user': identity[1], 'version': identity[2]}
            if not args.reuse:
                start = time.perf_counter()
                with conn.transaction():
                    conn.execute((ROOT / '001_initial.sql').read_text())
                    conn.execute('TRUNCATE p02_notes')
                    with conn.cursor() as cursor:
                        cursor.executemany('''INSERT INTO p02_notes
                            (id, language, title, body, model_digest, embedding)
                            VALUES (%s, %s, %s, %s, %s, %s::vector)''', [
                            (n['id'], n['language'], normalize(n['title']), normalize(n['body']), digest, vector_sql(v))
                            for n, v in zip(notes, vectors, strict=True)])
                evidence['store_s'] = time.perf_counter() - start
            evidence['database']['pgvector'] = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
            rows = conn.execute('SELECT id, language, title, body, model_digest, embedding::text FROM p02_notes ORDER BY id').fetchall()
            assert len(rows) == len(notes)
            for row, note in zip(rows, sorted(notes, key=lambda n: n['id']), strict=True):
                assert row[:5] == (note['id'], note['language'], normalize(note['title']), normalize(note['body']), digest)
            stored = {r[0]: json.loads(r[5]) for r in rows}
            norms = [math.sqrt(sum(x*x for x in v)) for v in stored.values()]
            assert all(len(v) == DIM for v in stored.values()) and max(abs(n-1) for n in norms) < 1e-5
            evidence['storage_check'] = {'rows': len(rows), 'dimension': DIM, 'norm_min': min(norms), 'norm_max': max(norms)}
            try:
                conn.execute('SELECT embedding <=> %s::vector FROM p02_notes LIMIT 1', ('[1,0]',))
            except psycopg.errors.DataException as exc:
                evidence['dimension_rejection'] = {'sqlstate': exc.sqlstate, 'message': str(exc)}
            else:
                raise AssertionError('Wrong vector dimension was accepted')
            for q in queries:
                start = time.perf_counter()
                query_input = f"Instruct: {INSTRUCTION}\nQuery: {normalize(q['text'])}"
                # autocommit=True: inference never runs inside a transaction.
                (vector,), metrics = embed(args.base_url, [query_input])
                param = vector_sql(vector)
                sql_start = time.perf_counter()
                hits = conn.execute(SEARCH, (param, param, 3)).fetchall()
                sql_s = time.perf_counter() - sql_start
                oracle = sorted(stored, key=lambda id: (
                    -sum(x*y for x, y in zip(stored[id], vector)) / math.sqrt(sum(x*x for x in stored[id])), id))[:3]
                assert oracle == [h[0] for h in hits], 'SQL ranking differs from independent cosine calculation'
                expected = set(q['expected'])
                rank = next((i for i, hit in enumerate(hits, 1) if hit[0] in expected), None)
                item = {**q, 'input': query_input, 'hits': [{'id': h[0], 'title': h[1], 'score': h[2]} for h in hits],
                        'first_expected_rank': rank, 'recall_at_3': len(expected & {h[0] for h in hits}) / len(expected) if expected else None,
                        'embedding': metrics, 'sql_s': sql_s, 'total_s': time.perf_counter() - start}
                evidence['queries'].append(item)
                print(q['id'], ' '.join(f'{h[0]}:{h[2]:.4f}' for h in hits), 'expected_rank=', rank, flush=True)
            evidence['explain_analyze'] = [r[0] for r in conn.execute(
                'EXPLAIN (ANALYZE, BUFFERS) ' + SEARCH, (param, param, 3)).fetchall()]
            evidence['memory_api_ps'] = api(args.base_url, '/api/ps')
            positive = [q for q in evidence['queries'] if q['expected'] and q['kind'] != 'ambiguous']
            evidence['summary'] = {
                'positive_queries': len(positive), 'hit_at_1': sum(q['first_expected_rank'] == 1 for q in positive),
                'hit_at_3': sum(q['first_expected_rank'] is not None for q in positive),
                'mrr_at_3': statistics.mean(1/q['first_expected_rank'] if q['first_expected_rank'] else 0 for q in positive),
                'sql_cosine_oracle': 'PASS for every query',
                'median_total_s': statistics.median(q['total_s'] for q in evidence['queries']),
                'max_total_s': max(q['total_s'] for q in evidence['queries']),
                'median_sql_s': statistics.median(q['sql_s'] for q in evidence['queries'])}
            if any(q['recall_at_3'] != 1 for q in evidence['queries'] if q['expected']):
                evidence['errors'].append('Some predefined expected notes are missing from top-3')
    except Exception as exc:
        evidence['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        evidence['wall_s'] = time.perf_counter() - start_all
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(evidence['summary'], indent=2))
    if evidence['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
