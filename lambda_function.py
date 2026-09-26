import json
import logging
import os
import time
import uuid

import boto3
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("A variável de ambiente TELEGRAM_TOKEN não foi configurada.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
client = genai.Client(api_key=GEMINI_API_KEY)

IS_LAMBDA = os.environ.get("AWS_EXECUTION_ENV") is not None

if IS_LAMBDA:
    dynamodb = boto3.resource("dynamodb")
    tabela = dynamodb.Table("TransacoesPendentes")
else:
    tabela_mock = {}


class TransactionSchema(BaseModel):
    data_hora: str = Field(description="Data e hora no formato DD/MM/AAAA HH:MM ou data aproximada")
    origem_destino: str = Field(description="Nome do estabelecimento, recebedor ou pagador")
    categoria: str = Field(description="Categoria financeira estritamente escolhida da lista permitida")
    forma_pagamento: str = Field(description="Forma de pagamento (PIX, Cartão de Crédito, Cartão de Débito, Dinheiro, etc.)")
    valor: str = Field(description="Valor numérico positivo com vírgula (ex: '15,50')")
    tipo: str = Field(description="'Despesa' ou 'Receita'")
    natureza: str = Field(description="'Fixo' ou 'Variável'")


def salvar_transacao_estado(id_transacao: str, chat_id: int, dados_extraidos: dict) -> None:
    if IS_LAMBDA:
        tabela.put_item(
            Item={
                "id_transacao": id_transacao,
                "chat_id": chat_id,
                "dados": json.dumps(dados_extraidos),
            }
        )
    else:
        tabela_mock[id_transacao] = dados_extraidos


def puxar_e_apagar_transacao_estado(id_transacao: str):
    if IS_LAMBDA:
        response_db = tabela.get_item(Key={"id_transacao": id_transacao})
        if "Item" in response_db:
            dados = json.loads(response_db["Item"]["dados"])
            tabela.delete_item(Key={"id_transacao": id_transacao})
            return dados
        return None
    return tabela_mock.pop(id_transacao, None)


def carregar_perfis() -> dict:
    caminho = os.getenv("PERFIS_PATH", "perfis_usuarios.json")
    if not os.path.exists(caminho):
        caminho = "perfis_usuarios.example.json"
    if not os.path.exists(caminho):
        return {}

    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def carregar_usuarios_autorizados() -> dict:
    usuarios = {}
    for i in range(1, 11):
        user_id_str = os.getenv(f"USER{i}_ID")
        if not user_id_str or not user_id_str.strip().isdigit():
            continue

        user_id = int(user_id_str.strip())
        usuarios[user_id] = {
            "id": user_id,
            "chave_env": f"USER{i}_ID",
            "nome": os.getenv(f"USER{i}_NAME", f"User {i}"),
            "nome_completo": os.getenv(f"USER{i}_FULL_NAME", f"User {i}"),
            "webhook_url": os.getenv(f"USER{i}_WEBHOOK_URL") or os.getenv("WEBHOOK_URL", ""),
        }
    return usuarios


USUARIOS_AUTORIZADOS = carregar_usuarios_autorizados()


def get_user_config(user_id: int) -> dict:
    usuario = USUARIOS_AUTORIZADOS.get(user_id)
    if not usuario:
        return None

    perfis = carregar_perfis()
    chave_env = usuario.get("chave_env")
    perfil = perfis.get(str(user_id)) or perfis.get(chave_env) or {}

    categorias_despesa = perfil.get("categorias_despesa", ["Outras Despesas"])
    categorias_receita = perfil.get("categorias_receita", ["Outras Receitas"])
    regras_extras = perfil.get("regras_extras", "")

    categorias_despesa_str = ", ".join(f'"{c}"' for c in categorias_despesa)
    categorias_receita_str = ", ".join(f'"{c}"' for c in categorias_receita)

    prompt_template = perfil.get("prompt_template")
    if prompt_template:
        prompt = prompt_template.format(
            nome_completo=usuario["nome_completo"],
            categorias_despesa=categorias_despesa_str,
            categorias_receita=categorias_receita_str,
            regras_extras=regras_extras,
        )
    else:
        prompt = (
            f"Analise esta transação financeira (comprovante, mensagem de texto ou áudio). "
            f"Retorne os dados estruturados de acordo com o esquema requerido:\n"
            f'1. "data_hora": Data e hora no formato "DD/MM/AAAA HH:MM" ou data aproximada.\n'
            f'2. "origem_destino": Nome da contraparte da transação. NUNCA use o nome do titular ({usuario["nome_completo"]}). Se despesa, quem recebeu. Se receita, quem pagou.\n'
            f'3. "categoria": Escolha ESTRITAMENTE da lista permitida:\n'
            f'    - Se Despesa: {categorias_despesa_str}.\n'
            f'    - Se Receita: {categorias_receita_str}.\n'
            f"    Regras de Categorização Automática:\n"
            f"    {regras_extras}\n"
            f'4. "forma_pagamento": PIX, Cartão de Crédito, Cartão de Débito, Dinheiro, etc.\n'
            f'5. "valor": Apenas numérico positivo com vírgula (ex: "15,50").\n'
            f'6. "tipo": "Despesa" ou "Receita".\n'
            f'7. "natureza": "Fixo" ou "Variável".'
        )

    return {
        "categorias_despesa": categorias_despesa,
        "categorias_receita": categorias_receita,
        "prompt": prompt,
    }


def disparar_webhook(dados: dict, url_planilha: str) -> None:
    if not url_planilha:
        raise ValueError("URL do Webhook da planilha não está configurada.")

    data_hora = dados.get("data_hora", "")
    mes_ano = data_hora[3:10] if len(data_hora) >= 10 else ""

    payload = {
        "id": str(uuid.uuid4()).split("-")[0],
        "data_hora": data_hora,
        "mes_ano": mes_ano,
        "destinatario": dados.get("origem_destino", ""),
        "categoria": dados.get("categoria", ""),
        "forma_pagamento": dados.get("forma_pagamento", ""),
        "valor": dados.get("valor", ""),
        "tipo": dados.get("tipo", ""),
        "natureza": dados.get("natureza", ""),
    }
    requests.post(url_planilha, json=payload, timeout=10)


def processar_entrada_usuario(message, tipo_entrada: str) -> None:
    chat_id = message.chat.id
    if chat_id not in USUARIOS_AUTORIZADOS:
        bot.reply_to(message, "⛔ Acesso Negado. Você não tem autorização para usar este bot.")
        return

    usuario = USUARIOS_AUTORIZADOS[chat_id]
    user_config = get_user_config(chat_id)
    if not user_config:
        bot.reply_to(message, "⛔ Configurações de perfil do usuário não encontradas.")
        return

    msg_status = bot.reply_to(message, "⏳ Processando transação...")

    try:
        contents = [user_config["prompt"]]

        if tipo_entrada == "text":
            contents.append(f"Entrada informada pelo usuário em texto: {message.text}")
        else:
            if tipo_entrada == "photo":
                file_id = message.photo[-1].file_id
                mime_type = "image/jpeg"
            elif tipo_entrada == "voice":
                file_id = message.voice.file_id
                mime_type = getattr(message.voice, "mime_type", None) or "audio/ogg"
            elif tipo_entrada == "audio":
                file_id = message.audio.file_id
                mime_type = getattr(message.audio, "mime_type", None) or "audio/mp3"
            elif tipo_entrada == "document":
                file_id = message.document.file_id
                mime_type = getattr(message.document, "mime_type", None) or "application/pdf"
            else:
                raise ValueError(f"Tipo de entrada não suportado: {tipo_entrada}")

            file_info = bot.get_file(file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            contents.append(types.Part.from_bytes(data=downloaded_file, mime_type=mime_type))

        modelos_disponiveis = [
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
        ]
        response = None

        for _ in range(2):
            for modelo in modelos_disponiveis:
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_status.message_id,
                        text=f"⏳ Analisando com {modelo}...",
                    )
                    response = client.models.generate_content(
                        model=modelo,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=TransactionSchema,
                        ),
                    )
                    break
                except Exception as e:
                    erro_str = str(e)
                    if "503" in erro_str or "429" in erro_str or "UNAVAILABLE" in erro_str:
                        continue
                    raise e
            if response:
                break
            time.sleep(2)

        if not response:
            raise RuntimeError("Serviços de IA temporariamente indisponíveis. Tente novamente.")

        transacao = TransactionSchema.model_validate_json(response.text)
        dados_extraidos = transacao.model_dump()
        id_transacao = str(msg_status.message_id)

        salvar_transacao_estado(id_transacao, chat_id, dados_extraidos)

        tipo = dados_extraidos.get("tipo", "Despesa")
        categoria_sugerida = dados_extraidos.get("categoria", "")
        lista_opcoes = (
            user_config["categorias_receita"]
            if tipo == "Receita"
            else user_config["categorias_despesa"]
        )

        markup = InlineKeyboardMarkup()
        markup.row_width = 2
        markup.add(
            InlineKeyboardButton(
                f"✅ Confirmar: {categoria_sugerida}",
                callback_data=f"save_{id_transacao}_{categoria_sugerida}",
            )
        )

        botoes = [
            InlineKeyboardButton(cat, callback_data=f"save_{id_transacao}_{cat}")
            for cat in lista_opcoes
            if cat != categoria_sugerida
        ]
        markup.add(*botoes)

        emoji_tipo = "💸" if tipo == "Despesa" else "💰"
        resumo = (
            f"📄 **Resumo da Transação:**\n"
            f"👤 **Origem/Destino:** {dados_extraidos.get('origem_destino')}\n"
            f"💵 **Valor:** R$ {dados_extraidos.get('valor')}\n"
            f"💳 **Pagamento:** {dados_extraidos.get('forma_pagamento')}\n"
            f"{emoji_tipo} **Tipo:** {tipo}\n"
            f"📅 **Data:** {dados_extraidos.get('data_hora')}\n\n"
            f"🤖 *Selecione a categoria para confirmar e gravar na planilha:*"
        )
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_status.message_id,
            text=resumo,
            reply_markup=markup,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Erro ao processar {tipo_entrada}: {e}", exc_info=True)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_status.message_id, text=f"❌ Erro: {e}")


@bot.message_handler(commands=["start"])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id not in USUARIOS_AUTORIZADOS:
        bot.reply_to(message, "⛔ Acesso Negado. Você não tem permissão para usar este sistema.")
        return

    usuario = USUARIOS_AUTORIZADOS[chat_id]
    bot.reply_to(
        message,
        f"Fala {usuario['nome']}! 🤖\n\n"
        "Você pode registrar transações de 4 formas diferentes:\n"
        "📸 **Foto** de cupom/recibo\n"
        "📄 **PDF** de comprovante bancário\n"
        "🎙️ **Áudio de voz** (ex: *'Gastei 35 reais no almoço no débito'*)\n"
        "💬 **Texto livre** (ex: *'padaria 14,50 pix'*)\n\n"
        "Eu analiso tudo com IA e lanço direto na sua planilha após sua confirmação!",
        parse_mode="Markdown",
    )


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    processar_entrada_usuario(message, tipo_entrada="photo")


@bot.message_handler(content_types=["document"])
def handle_document(message):
    processar_entrada_usuario(message, tipo_entrada="document")


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    processar_entrada_usuario(message, tipo_entrada="voice")


@bot.message_handler(content_types=["audio"])
def handle_audio(message):
    processar_entrada_usuario(message, tipo_entrada="audio")


@bot.message_handler(func=lambda msg: not msg.text.startswith("/"), content_types=["text"])
def handle_text(message):
    processar_entrada_usuario(message, tipo_entrada="text")


@bot.callback_query_handler(func=lambda call: call.data.startswith("save_"))
def callback_salvar(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id

    if chat_id not in USUARIOS_AUTORIZADOS:
        return

    usuario = USUARIOS_AUTORIZADOS[chat_id]
    _, id_transacao, categoria_escolhida = call.data.split("_", 2)

    try:
        dados = puxar_e_apagar_transacao_estado(id_transacao)
        if dados:
            dados["categoria"] = categoria_escolhida
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text="🚀 Gravando na planilha do Google...",
            )
            disparar_webhook(dados, usuario["webhook_url"])
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=(
                    f"✅ Salvo com sucesso na planilha de **{usuario['nome']}**!\n"
                    f"Valor: R$ {dados['valor']} em **{categoria_escolhida}**."
                ),
                parse_mode="Markdown",
            )
        else:
            bot.send_message(chat_id=chat_id, text="⚠️ Essa transação já foi salva ou expirou.")
    except Exception as e:
        logger.error(f"Erro no callback de salvamento: {e}", exc_info=True)
        bot.send_message(chat_id=chat_id, text=f"❌ Erro ao salvar: {e}")


def lambda_handler(event, context):
    try:
        if "body" in event:
            json_string = event["body"]
            if isinstance(json_string, dict):
                json_string = json.dumps(json_string)

            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])

        return {
            "statusCode": 200,
            "body": json.dumps("Mensagem processada com sucesso"),
        }
    except Exception as e:
        logger.error(f"Erro na execução da Lambda: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps(f"Erro interno: {str(e)}"),
        }


if __name__ == "__main__":
    print("🤖 Bot iniciado localmente em modo polling. Pressione Ctrl+C para encerrar.")
    bot.remove_webhook()
    time.sleep(1)
    bot.infinity_polling()
