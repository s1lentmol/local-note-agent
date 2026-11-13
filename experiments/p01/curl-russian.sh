#!/bin/sh
set -eu
: "${BASE_URL:?Source config.example.sh first}"
: "${MODEL:?Source config.example.sh first}"
: "${API_KEY:?Source config.example.sh first}"
# Generate JSON safely even if MODEL contains JSON-special characters.
"${PYTHON:-python3}" -c 'import json,os; print(json.dumps({"model":os.environ["MODEL"],"stream":False,"temperature":0,"seed":42,"max_tokens":512,"reasoning_effort":"none","messages":[{"role":"user","content":"Объясни по-русски в двух предложениях, зачем делать резервные копии заметок."}]},ensure_ascii=False))' |
curl --noproxy '*' --fail-with-body --silent --show-error --max-time 180 \
  "${BASE_URL%/}/chat/completions" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $API_KEY" \
  --data-binary @- --write-out '\nHTTP %{http_code}; seconds %{time_total}\n'
