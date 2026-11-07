import os

from openai import OpenAI

# La clé de passerelle gw_... est différente des credentials OAuth du serveur.
client = OpenAI(base_url=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1"), api_key=os.environ["GATEWAY_API_KEY"], max_retries=0)
model = os.environ.get("GATEWAY_MODEL", "passerelle-default")  # Configurer l'alias via set-default.

answer = client.responses.create(model=model, input="Explique LTE et 5G NR en deux phrases.")
print(answer.output_text)

with client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Bonjour !"}], stream=True) as stream:
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
print()
