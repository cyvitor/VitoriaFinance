import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TelegramLink, User
from app.services.telegram_auth import consume_pairing_code
from app.services.telegram_expenses import (
    cancel_expense_draft, confirm_expense_draft, create_expense_draft,
    format_draft, parse_expense_command,
)


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
    command = parse_expense_command(text)
    if command:
        draft = create_expense_draft(db, user, command)
        if not draft:
            return "Defina uma area financeira padrao em Meu perfil antes de registrar gastos pelo Telegram."
        return format_draft(draft)
    normalized = text.casefold().split("@", 1)[0]
    if normalized == "/confirmar":
        transaction = confirm_expense_draft(db, user)
        if not transaction:
            return "Nao ha um gasto aguardando confirmacao ou o rascunho expirou."
        amount = f"{transaction.amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"Gasto registrado com sucesso: {transaction.description} - R$ {amount}."
    if normalized == "/cancelar":
        return "Gasto cancelado." if cancel_expense_draft(db, user) else "Nao ha um gasto aguardando confirmacao."
    return "Comando nao reconhecido. Para registrar, envie /gasto VALOR DESCRICAO. Exemplo: /gasto 85,90 mercado."
