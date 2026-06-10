$env:OPENAI_API_KEY = "sk-dummy"
$env:OPENAI_BASE_URL = "http://10.129.107.145:8001/v1"
$env:MODEL_NAME = "Qwen_agent"

python __main__.py $args
