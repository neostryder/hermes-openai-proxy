# Example curl invocations against hermes-openai-proxy.
#
# Start the proxy first:
#   python -m hermes_openai_proxy
# Then run these in another shell.

# Health check
curl http://127.0.0.1:8765/healthz | python -m json.tool

# List available models (filtered to credentialled providers only)
curl http://127.0.0.1:8765/v1/models | python -m json.tool

# Basic chat completion (OpenAI-style)
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role":"user","content":"Say pong"}],
    "max_tokens": 20
  }'

# Streaming (Note the -N flag for unbuffered streaming)
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role":"user","content":"Count to 5"}],
    "stream": true
  }'

# Tool-calling roundtrip (LangChain-style)
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [
      {"role":"user","content":"What is the weather in Paris?"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get the current weather in a location",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type":"string"}
            },
            "required": ["location"]
          }
        }
      }
    ]
  }'

# Structured output (json_schema -- auto-translated to json_object + hint)
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role":"user","content":"Pick a random French name"}],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "name",
        "schema": {
          "type": "object",
          "properties": {
            "first_name": {"type":"string"},
            "last_name": {"type":"string"}
          },
          "required": ["first_name", "last_name"]
        }
      }
    }
  }'

# Anthropic-style request -- the proxy translates this to native Anthropic
# format and back, transparently.
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-sonnet-4-5",
    "messages": [{"role":"user","content":"Hello"}],
    "max_tokens": 50
  }'

# Vision (image URL) -- works with any provider that supports it.
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [
      {
        "role":"user",
        "content": [
          {"type":"text","text":"What is in this image?"},
          {"type":"image_url","image_url":{"url":"https://upload.wikimedia.org/wikipedia/commons/thumb/0/0c/MakingMinimax-MiniMax-M3-001.jpg/640px-MakingMinimax-MiniMax-M3-001.jpg"}}
        ]
      }
    ],
    "max_tokens": 200
  }'