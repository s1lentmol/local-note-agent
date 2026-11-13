"""Offline evidence integrity checks; semantic judgments remain manual."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as output:
        result = {'checked_utc':datetime.now(timezone.utc).isoformat(), 'checks':{}}
        try:
            protocol = json.loads((ROOT / 'protocol.json').read_text())
            for group in ('sha256', 'existing_evidence_sha256'):
                assert all(sha(PROJECT / p) == checksum for p, checksum in protocol[group].items()), group
                result['checks'][group] = {'result':'PASS', 'files':len(protocol[group])}
            raw_path = ROOT / 'results/eval-real.json'
            raw = json.loads(raw_path.read_text())
            questions = json.loads((ROOT / 'questions.json').read_text())
            assert not raw['errors'] and len(raw['queries']) == len(questions) == 10
            assert protocol['prepared_utc'] < raw['started_utc']
            assert raw['database']['read_only'] == 'on'
            assert raw['database']['rows_sha256_before'] == raw['database']['rows_sha256_after']
            for item, question in zip(raw['queries'], questions, strict=True):
                assert item['id'] == question['id'] and item['question'] == question['question']
                assert item['generation_response']['choices'][0]['finish_reason'] == 'stop'
                assert item['generation_response']['choices'][0]['message']['content'] == item['answer']
                assert len(item['retrieval']) == 3
                assert item['expected_notes_in_top_k'] is not False
                assert all(n in [h['id'] for h in item['retrieval']] for n in question['expected_notes'])
                context = '\n\n'.join(h['title'] + '\n' + h['body'] for h in item['retrieval'])
                assert item['generation_request']['messages'][1]['content'] == 'Заметки:\n' + context + '\n\nВопрос:\n' + question['question']
            result['checks']['raw_chain_and_database_snapshot'] = 'PASS'
            review = json.loads((ROOT / 'results/manual-review.json').read_text())
            assert review['evidence_sha256'] == sha(raw_path)
            assert [q['id'] for q in review['queries']] == [q['id'] for q in questions]
            result['checks']['manual_review_link'] = 'PASS (integrity only, not semantic regrading)'
            before = sha(raw_path)
            command = [sys.executable, str(ROOT / 'run.py'), '--output', str(raw_path)]
            attempt = subprocess.run(command, capture_output=True, text=True)
            assert attempt.returncode != 0 and 'FileExistsError' in attempt.stderr and before == sha(raw_path)
            result['checks']['existing_output_rejected'] = {'result':'PASS', 'exit_code':attempt.returncode,
                'exception':'FileExistsError', 'sha256_unchanged':True}
            diff = subprocess.run(['git','diff','--check'], cwd=PROJECT, capture_output=True, text=True)
            assert diff.returncode == 0, diff.stdout + diff.stderr
            result['checks']['git diff --check'] = 'PASS'
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
