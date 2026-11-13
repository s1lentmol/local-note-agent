export BASE_URL=${BASE_URL:-http://${OLLAMA_HOST:-127.0.0.1:11435}/v1}
export MODEL=${MODEL:-qwen3:8b}
# Local Ollama ignores this placeholder; never put real keys in this file.
export API_KEY=${API_KEY:-local-p01}
