import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TelegramLink, User
from app.logging_config import get_bot_logger
from app.services.telegram_auth import consume_pairing_code
from app.services.telegram_ai import AIResponseFormatError, AIToolSelectionError, AIUnavailableError
from app.services.telegram_agent import run_financial_agent

logger = get_bot_logger()


def handle_message(db: Session, message: dict) -> str:
    text = (message.get("text") or "").strip()
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if chat.get("type") not in (None, "private"):
        return "Por seguranca, associe sua conta somente em uma conversa privada com o bot."
    if not sender.get("id") or not chat.get("id"):
        return "Nao foi possivel identificar sua conta do Telegram."
    match = re.fullmatch(r"(?:/start(?:@\w+)?\s+)?(\d{8})", text)
    if match:
        result = consume_pairing_code(
            db,
            match.group(1),
            telegram_user_id=sender.get("id", ""),
            telegram_chat_id=chat.get("id", ""),
            telegram_username=sender.get("username"),
        )
        return result.message
    link = db.scalar(select(TelegramLink).where(TelegramLink.telegram_user_id == str(sender["id"])))
    user = db.get(User, link.user_id) if link else None
    if text.startswith("/start"):
        return "Para associar sua conta, gere um codigo em Meu perfil no VitoriaFinance e envie /start CODIGO."
    if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
        return "Seu Telegram ainda nao esta associado. Gere um codigo em Meu perfil e envie /start CODIGO."
    try:
        return run_financial_agent(db, user, text)
    except (AIResponseFormatError, AIToolSelectionError) as exc:
        logger.error("agent_structured_response_failed user_id=%s error=%s", user.id, exc)
        return "Não consegui organizar essa consulta com segurança agora. Nenhum lançamento foi feito; tente novamente em instantes."
    except AIUnavailableError as exc:
        logger.error("deepinfra_unavailable user_id=%s error=%s", user.id, exc)
        return "Não consegui acessar a Vitoria pela DeepInfra agora. Nenhum lançamento foi feito; use a interface web por enquanto."
    except Exception:
        logger.exception("unexpected_agent_error user_id=%s", user.id)
        return "Ocorreu um erro inesperado ao processar sua mensagem. Nenhum lançamento foi feito; use a interface web por enquanto."
