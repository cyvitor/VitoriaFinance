from datetime import datetime, timedelta

from sqlalchemy import select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.models import SystemAccount, TelegramLink, TelegramPairingCode, User
from app.security import hash_password
from app.services.telegram_auth import consume_pairing_code, create_pairing_code


def create_user(db, username="telegram-user"):
    account = SystemAccount(name=f"Conta {username}")
    db.add(account)
    db.flush()
    user = User(
        system_account_id=account.id,
        username=username,
        full_name="Usuario Telegram",
        password_hash=hash_password("password123"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_pairing_code_is_hashed_single_use_and_links_numeric_id():
    with SessionLocal() as db:
        user = create_user(db)
        code = create_pairing_code(db, user)
        stored = db.scalar(select(TelegramPairingCode))
        assert len(code) == 8 and code.isdigit()
        assert stored.code_hash != code

        response = handle_message(db, {
            "text": f"/start {code}",
            "from": {"id": 123456789, "username": "tester"},
            "chat": {"id": 123456789, "type": "private"},
        })
        assert "sucesso" in response
        link = db.scalar(select(TelegramLink))
        assert link.user_id == user.id
        assert link.telegram_user_id == "123456789"
        assert link.telegram_username == "tester"

        repeated = consume_pairing_code(db, code, 123456789, 123456789)
        assert not repeated.ok


def test_expired_code_and_cross_account_takeover_are_rejected():
    now = datetime.utcnow()
    with SessionLocal() as db:
        first = create_user(db, "first")
        expired_code = create_pairing_code(db, first, now=now - timedelta(minutes=11))
        assert not consume_pairing_code(db, expired_code, 1, 1, now=now).ok

        valid_code = create_pairing_code(db, first, now=now)
        assert consume_pairing_code(db, valid_code, 1, 1, now=now).ok

        second = create_user(db, "second")
        second_code = create_pairing_code(db, second, now=now)
        result = consume_pairing_code(db, second_code, 1, 1, now=now)
        assert not result.ok
        assert "outro usuario" in result.message


def test_pairing_is_rejected_in_group_chat():
    with SessionLocal() as db:
        user = create_user(db)
        code = create_pairing_code(db, user)
        response = handle_message(db, {
            "text": f"/start {code}",
            "from": {"id": 123},
            "chat": {"id": -999, "type": "group"},
        })
        assert "conversa privada" in response
        assert db.scalar(select(TelegramLink)) is None
