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
from app.services import bot_confirmations, fuel_followup
from app.models import TelegramConversationMessage

BUTTON_ECHO = {
    "ok:c": "✅ Confirmado por você.", "ok:x": "❌ Cancelado por você.",
    "fb:c": "✅ Confirmado por você.", "fb:x": "❌ Cancelado por você.",
    "fu:y": "✅ Sim, registrar o abastecimento.", "fu:n": "❌ Não registrar o abastecimento.",
    "ec:b": "🧾 Você escolheu: conta fixa.", "ec:n": "🆕 Você escolheu: despesa nova.",
}
BUTTON_LABEL = {
    "ok:c": "Confirmar", "ok:x": "Cancelar", "fb:c": "Confirmar pagamento", "fb:x": "Cancelar pagamento",
    "fb:p": "Paguei", "fb:v": "Outro valor", "fb:m": "Mes atual", "fb:s": "Forma de pagamento",
    "fu:y": "Sim (abastecimento)", "fu:n": "Nao (abastecimento)", "ec:b": "Conta fixa", "ec:n": "Despesa nova",
    "fl": "Registrar abastecimento nesta despesa",
}
REFUSALS = ("já foi", "não está mais", "não pertence", "não existe", "não reconheci", "expirou",
            "não está disponível", "nao esta associado")


def removes_keyboard(data: str) -> bool:
    """Botoes de decisao somem apos o clique; listas (/contas) continuam utilizaveis."""
    return data.startswith(("ok:", "fu:", "ec:", "fl:", "fb:c:", "fb:x:", "fb:s:"))


def _button_key(data: str) -> str:
    parts = data.split(":")
    return parts[0] if parts[0] == "fl" else ":".join(parts[:2])

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
        reply = (bot_confirmations.handle_callback(db, user, data)
                 or fuel_followup.handle_callback(db, user, data)
                 or handle_bill_callback(db, user, data, _message_date(callback.get("message") or {}, user)))
        key = _button_key(data)
        echo = BUTTON_ECHO.get(key)
        if key == "fl" and not any(term in reply.text.casefold() for term in REFUSALS):
            echo = "⛽ Você escolheu registrar o abastecimento nesta despesa."
        if echo and not any(term in reply.text.casefold() for term in REFUSALS):
            reply = BotReply(f"{echo}\n\n{reply.text}", reply.keyboard)
        # Cliques tambem entram no historico: a IA precisa ver a mesma conversa que o usuario.
        db.add_all([TelegramConversationMessage(user_id=user.id, role="user",
                                                content=f"[botão] {BUTTON_LABEL.get(key, key)}"),
                    TelegramConversationMessage(user_id=user.id, role="assistant", content=reply.text[:4000])])
        db.commit()
        return reply
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
        db.info.pop("bot_keyboard", None)
        reply = run_financial_agent(db, user, text, reference_date=message_date)
        keyboard = (db.info.pop("bot_keyboard", None) or keyboard_for_recent_action(db, user.id, started)
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
