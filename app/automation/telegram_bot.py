import re

from sqlalchemy.orm import Session

from app.services.telegram_auth import consume_pairing_code


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
    if text.startswith("/start"):
        return "Para associar sua conta, gere um codigo em Meu perfil no VitoriaFinance e envie /start CODIGO."
    return "Ainda nao reconheco esse comando. Use /start CODIGO para associar sua conta."
