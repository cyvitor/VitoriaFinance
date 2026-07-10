from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TelegramLink, TelegramPairingCode, User

PAIRING_TTL_MINUTES = 10


@dataclass(frozen=True)
class PairingResult:
    ok: bool
    message: str
    user: User | None = None


def _hash_code(code: str) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, code.encode("ascii"), hashlib.sha256).hexdigest()


def create_pairing_code(db: Session, user: User, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    db.execute(delete(TelegramPairingCode).where(TelegramPairingCode.user_id == user.id))
    for _ in range(10):
        code = f"{secrets.randbelow(100_000_000):08d}"
        code_hash = _hash_code(code)
        if not db.scalar(select(TelegramPairingCode.id).where(TelegramPairingCode.code_hash == code_hash)):
            db.add(TelegramPairingCode(
                user_id=user.id,
                code_hash=code_hash,
                expires_at=now + timedelta(minutes=PAIRING_TTL_MINUTES),
            ))
            db.commit()
            return code
    raise RuntimeError("Nao foi possivel gerar um codigo de vinculacao unico")


def consume_pairing_code(
    db: Session,
    code: str,
    telegram_user_id: int | str,
    telegram_chat_id: int | str,
    telegram_username: str | None = None,
    now: datetime | None = None,
) -> PairingResult:
    now = now or datetime.utcnow()
    normalized = "".join(character for character in code if character.isdigit())
    if len(normalized) != 8:
        return PairingResult(False, "Codigo invalido. Gere um novo codigo no VitoriaFinance.")

    pairing = db.scalar(select(TelegramPairingCode).where(
        TelegramPairingCode.code_hash == _hash_code(normalized),
        TelegramPairingCode.used_at.is_(None),
    ))
    if not pairing or pairing.expires_at < now:
        return PairingResult(False, "Codigo invalido ou expirado. Gere um novo codigo no VitoriaFinance.")

    user = db.get(User, pairing.user_id)
    if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
        return PairingResult(False, "A conta do VitoriaFinance nao esta ativa.")

    telegram_id = str(telegram_user_id)
    existing_telegram = db.scalar(select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_id))
    existing_user = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    if existing_telegram and existing_telegram.user_id != user.id:
        return PairingResult(False, "Este Telegram ja esta associado a outro usuario.")
    if existing_user and existing_user.telegram_user_id != telegram_id:
        return PairingResult(False, "Seu usuario ja possui outro Telegram associado. Desvincule-o primeiro.")

    link = existing_user or existing_telegram
    if not link:
        link = TelegramLink(user_id=user.id, telegram_user_id=telegram_id, telegram_chat_id=str(telegram_chat_id))
        db.add(link)
    link.telegram_chat_id = str(telegram_chat_id)
    link.telegram_username = telegram_username
    link.last_seen_at = now
    pairing.used_at = now
    db.commit()
    return PairingResult(True, f"Telegram associado com sucesso a {user.full_name}.", user)


def unlink_telegram(db: Session, user: User) -> bool:
    link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    if not link:
        return False
    db.delete(link)
    db.commit()
    return True
