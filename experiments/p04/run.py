"""P04 real RAG eval. Reuses P02 embedder and persisted DB; never writes to DB."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
spec = importlib.util.spec_from_file_location('p02', ROOT.parent / 'p02/run.py')
p02 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p02)

SEARCH = '''SELECT id, title, body, 1 - (embedding <=> %s::vector) AS score
            FROM p02_notes ORDER BY embedding <=> %s::vector, id LIMIT %s'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also rejects an existing empty/failed evidence file.
    with args.output.open('x') as output:
        evidence = {'started_utc': datetime.now(timezone.utc).isoformat(),
                    'python': platform.python_version(), 'platform': platform.platform(),
                    'psycopg': p02.psycopg.__version__, 'queries': [], 'errors': []}
        started = time.perf_counter()
        try:
            config = json.loads((ROOT / 'config.json').read_text())
            questions = json.loads((ROOT / 'questions.json').read_text())
            protocol = json.loads((ROOT / 'protocol.json').read_text())
            assert all(sha(PROJECT / path) == checksum for path, checksum in protocol['sha256'].items()), 'Protocol changed'
            evidence.update(config=config, protocol=protocol)
            base = config['base_url']
            evidence['runtime'] = p02.api(base, '/api/version')
            tags = p02.api(base, '/api/tags')['models']
            embedding_pin = json.loads((ROOT.parent / 'p02/model-manifest.json').read_text())['ollama_digest']
            generation_pin = json.loads((ROOT.parent / 'p01/results/ps-first.json').read_text())['models'][0]['digest']
            evidence['artifacts'] = {}
            for model, pin in ((p02.MODEL, embedding_pin), (config['generation']['model'], generation_pin)):
                artifact = next(m for m in tags if m['name'] == model)
                assert artifact['digest'] == pin, f'Pinned model changed: {model}'
                evidence['artifacts'][model] = artifact
            evidence['embedding_config'] = {'model':p02.MODEL, 'dimension':p02.DIM,
                'instruction':p02.INSTRUCTION, 'normalization':'explicit L2', 'num_ctx':8192,
                'truncate':False, 'metric':'cosine <=>', 'keep_alive':'10m'}
            # Fixed disposable DSN from P02; read-only enforced by PostgreSQL.
            with p02.psycopg.connect(p02.DSN, autocommit=True,
                                    options='-c default_transaction_read_only=on') as conn:
                identity = conn.execute('SELECT current_database(), current_user, version(), current_setting(\'default_transaction_read_only\')').fetchone()
                assert identity[:2] == ('p02_experiment', 'p02_experiment') and identity[3] == 'on'
                evidence['database'] = {'name':identity[0], 'role':identity[1], 'version':identity[2],
                    'read_only':identity[3], 'pgvector':conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]}
                rows = conn.execute('SELECT id, language, title, body, model_digest, embedding::text FROM p02_notes ORDER BY id').fetchall()
                notes = json.loads((ROOT.parent / 'p02/corpus.json').read_text())
                assert len(rows) == len(notes) == 24
                for row, note in zip(rows, sorted(notes, key=lambda n:n['id']), strict=True):
                    assert row[:5] == (note['id'], note['language'], p02.normalize(note['title']), p02.normalize(note['body']), embedding_pin)
                    assert len(json.loads(row[5])) == p02.DIM
                snapshot = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
                evidence['database']['rows_sha256_before'] = snapshot
                evidence['database']['rows'] = len(rows)
                for question in questions:
                    item = {'id':question['id'], 'question':question['question']}
                    evidence['queries'].append(item)
                    begin = time.perf_counter()
                    try:
                        query_input = f"Instruct: {p02.INSTRUCTION}\nQuery: {p02.normalize(question['question'])}"
                        item['embedding_input'] = query_input
                        (vector,), item['embedding'] = p02.embed(base, [query_input])
                        param = p02.vector_sql(vector)
                        sql_start = time.perf_counter()
                        hits = conn.execute(SEARCH, (param, param, config['retrieval']['top_k'])).fetchall()
                        item['sql_s'] = time.perf_counter() - sql_start
                        item['retrieval'] = [dict(zip(('id','title','body','score'), hit)) for hit in hits]
                        item['expected_notes_in_top_k'] = all(n in [h[0] for h in hits] for n in question['expected_notes']) if question['expected_notes'] else None
                        context = '\n\n'.join(h[1] + '\n' + h[2] for h in hits)
                        # Expectations are evaluation data only, never sent to the models.
                        request = {**config['generation'], 'messages':[
                            {'role':'system', 'content':config['system_prompt']},
                            {'role':'user', 'content':'Заметки:\n' + context + '\n\nВопрос:\n' + question['question']}]}
                        item['generation_request'] = request
                        gen_start = time.perf_counter()
                        response = p02.api(base, '/v1/chat/completions', request)
                        item['generation_s'] = time.perf_counter() - gen_start
                        item['generation_response'] = response
                        choice = response['choices'][0]
                        item['answer'] = choice['message']['content']
                        assert choice['finish_reason'] == 'stop', 'Incomplete answer'
                        assert isinstance(item['answer'], str) and item['answer'].strip(), 'Empty answer'
                        item['memory_api_ps'] = p02.api(base, '/api/ps')
                    except Exception as exc:
                        item['error'] = f'{type(exc).__name__}: {exc}'
                        evidence['errors'].append(question['id'] + ': ' + item['error'])
                    finally:
                        item['total_s'] = time.perf_counter() - begin
                    print(item['id'], json.dumps(item.get('answer', item.get('error')), ensure_ascii=False), flush=True)
                after = conn.execute('SELECT id, language, title, body, model_digest, embedding::text FROM p02_notes ORDER BY id').fetchall()
                evidence['database']['rows_sha256_after'] = hashlib.sha256(json.dumps(after, ensure_ascii=False).encode()).hexdigest()
                assert evidence['database']['rows_sha256_after'] == snapshot, 'DB changed during eval'
        except Exception as exc:
            evidence['errors'].append(f'{type(exc).__name__}: {exc}')
            raise
        finally:
            evidence['wall_s'] = time.perf_counter() - started
            json.dump(evidence, output, ensure_ascii=False, indent=2)
            output.write('\n')
        if evidence['errors']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
