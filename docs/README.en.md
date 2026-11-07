# Passerelle ChatGPT

A personal, self-hosted gateway for eligible ChatGPT plan requests through the documented **Sign in with ChatGPT** flow. It provides a subset of the OpenAI Responses and Chat Completions interfaces, local access keys, model restrictions, daily quotas, image inputs and JSON/SSE output.

This independent MIT-licensed project is not affiliated with or endorsed by OpenAI. Availability depends on account eligibility, consent, model access and provider policy. Requests consume the authorized account's quota. A metered OpenAI API-key backend and automatic paid fallback are not implemented. Each operator connects their own account; this repository does not offer a shared inference service.

## Quick start

Requires Python 3.12+ and a browser for OAuth.

```bash
git clone https://github.com/Alexisdevpro/passerelle-chatgpt.git
cd passerelle-chatgpt
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps .
python -m gateway.cli init
python -m gateway.cli login
python -m gateway.cli models
python -m gateway.cli set-default --model YOUR_AVAILABLE_MODEL --effort auto
python -m gateway.cli serve
```

On Windows activate with `.\.venv\Scripts\Activate.ps1`. Replace `YOUR_AVAILABLE_MODEL` with an ID from the authenticated catalog. Requests use `http://127.0.0.1:8787/v1` and the local key saved in `.local/client-key.txt`. Set `GATEWAY_API_KEY` for the example scripts. Never publish the key or the data directory.

```python
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key=Path(".local/client-key.txt").read_text().strip(),
)
print(client.responses.create(
    model="passerelle-default", input="Hello!"
).output_text)
```

Install the client SDK separately with `python -m pip install openai`.

## Scope

Supported routes: `/v1/models`, `/v1/models/{model}`, `/v1/responses`, `/v1/chat/completions`. Responses and Chat support JSON or SSE. ChatGPT requests can include images with text output. Ollama is an explicitly selected, separate local provider. Tools/function calling, structured JSON, audio, Files, image generation and embeddings are not implemented in this adapter. Unsupported parameters are rejected explicitly.

The gateway uses its own OAuth registration, PKCE/OIDC verification, encrypted token storage and serialized refresh. Client keys are hashed in SQLite. Encrypted tokens and their decryption key reside on the same host, so disk and account security remain necessary.

See the [main guide](../README.md), [compatibility](../COMPATIBILITY.md), [Docker deployment](../DEPLOYMENT.md), [security policy](../SECURITY.md), [tests](../TEST-REPORT.md) and [contributing](../CONTRIBUTING.md). French documentation currently contains the detailed operator reference; English documentation contributions are welcome.

Provider reference: [Sign in with ChatGPT for OSS](https://developers.openai.com/siwc/token-sharing-open-source).

