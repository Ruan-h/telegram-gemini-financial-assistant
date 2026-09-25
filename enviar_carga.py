import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("USER2_WEBHOOK_URL") or os.getenv("USER1_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("Variável de ambiente WEBHOOK_URL não configurada.")

ARQUIVO_JSON = os.getenv("ARQUIVO_JSON", "extrato.json")


def enviar_carga(arquivo_json: str, webhook_url: str) -> None:
    with open(arquivo_json, "r", encoding="utf-8") as f:
        dados = json.load(f)

    print(f"Enviando {len(dados) if isinstance(dados, list) else 1} registros para a planilha...")
    resposta = requests.post(webhook_url, json=dados, timeout=30)

    if resposta.status_code in (200, 302):
        print(f"✅ Sucesso: {resposta.text}")
    else:
        print(f"❌ Erro HTTP {resposta.status_code}: {resposta.text}")


if __name__ == "__main__":
    try:
        enviar_carga(ARQUIVO_JSON, WEBHOOK_URL)
    except Exception as e:
        print(f"❌ Erro na execução da carga: {e}")
