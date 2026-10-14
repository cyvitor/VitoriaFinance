import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TelegramLink, User
from app.logging_config import get_bot_logger
from app.services.telegram_auth import consume_pairing_code
from app.services.telegram_ai import AIResponseFormatError, AIToolSelectionError, AIUnavailableError
from app.services.telegram_agent import run_financial_agent
from app.services.fixed_bills import BotReply, handle_bill_callback, handle_bill_text, keyboard_for_recent_action
from app.services import bot_confirmations

logger = get_bot_logger()


def _linked_user(db: Session, sender: dict) -> User | None:
    link = db.scalar(select(TelegramLink).where(TelegramLink.telegram_user_id == str(sender.get("id"))))
    user = db.get(User, link.user_id) if link else None
    if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
        return None
    return user


def handle_callback(db: Session, callback: dict) -> BotReply:
    """Botoes inline de contas fixas: tratados sem LLM e somente para o usuario vinculado."""
    sender = callback.get("from") or {}
    chat = (callback.get("message") or {}).get("chat") or {}
    if chat.get("type") not in (None, "private"):
        return BotReply("Por seguranca, use os botoes somente na conversa privada com o bot.")
    user = _linked_user(db, sender) if sender.get("id") else None
    if not user:
        logger.warning("telegram_callback_unlinked telegram_user_id=%s", sender.get("id"))
        return BotReply("Seu Telegram ainda nao esta associado. Gere um codigo em Meu perfil e envie /start CODIGO.")
    try:
        data = str(callback.get("data") or "")
        reply = bot_confirmations.handle_callback(db, user, data)
        if reply is not None:
            return reply
        return handle_bill_callback(db, user, data, _message_date(callback.get("message") or {}, user))
    except Exception:
        logger.exception("telegram_callback_failed user_id=%s data=%r", user.id, callback.get("data"))
        db.rollback()
        return BotReply("Nao consegui concluir essa acao agora. Nada foi registrado; tente novamente.")


def _message_date(message: dict, user: User):
    if message.get("date") is None:
        return None
    try:
        return datetime.fromtimestamp(int(message["date"]), tz=ZoneInfo("America/Sao_Paulo")).date()
    except (TypeError, ValueError, OSError):
        logger.warning("telegram_message_invalid_date user_id=%s date=%r", user.id, message.get("date"))
        return None


def _choice_keyboard(db: Session, user_id: int):
    from sqlalchemy import select
    from app.models import TelegramPendingAction
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id,
        TelegramPendingAction.action_type == bot_confirmations.CHOICE_TYPE,
        TelegramPendingAction.status == "collecting",
    ).order_by(TelegramPendingAction.id.desc()))
    if not action or action.expires_at < datetime.utcnow():
        return None
    return [[{"text": "🧾 Conta fixa", "callback_data": f"ec:b:{action.id}"},
             {"text": "🆕 Despesa nova", "callback_data": f"ec:n:{action.id}"}]]


def handle_message(db: Session, message: dict) -> str | BotReply:
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
        message_date = None
        if message.get("date") is not None:
            try:
                message_date = datetime.fromtimestamp(
                    int(message["date"]), tz=ZoneInfo("America/Sao_Paulo")
                ).date()
            except (TypeError, ValueError, OSError):
                logger.warning("telegram_message_invalid_date user_id=%s date=%r", user.id, message.get("date"))
        bill_reply = handle_bill_text(db, user, text, message_date)
        if bill_reply is not None:
            return bill_reply
        started = datetime.utcnow()
        reply = run_financial_agent(db, user, text, reference_date=message_date)
        keyboard = (keyboard_for_recent_action(db, user.id, started)
                    or bot_confirmations.keyboard_for_pending(db, user.id)
                    or _choice_keyboard(db, user.id))
        return BotReply(reply, keyboard) if keyboard else reply
    except (AIResponseFormatError, AIToolSelectionError) as exc:
        logger.error("agent_structured_response_failed user_id=%s error=%s", user.id, exc)
        return "Não consegui organizar essa consulta com segurança agora. Nenhum lançamento foi feito; tente novamente em instantes."
    except AIUnavailableError as exc:
        logger.error("deepinfra_unavailable user_id=%s error=%s", user.id, exc)
        return "Não consegui acessar a Vitoria pela DeepInfra agora. Nenhum lançamento foi feito; use a interface web por enquanto."
    except Exception:
        logger.exception("unexpected_agent_error user_id=%s", user.id)
        return "Ocorreu um erro inesperado ao processar sua mensagem. Nenhum lançamento foi feito; use a interface web por enquanto."
