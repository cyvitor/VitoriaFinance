from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.models import (
    AccountRole, Category, Person, PersonType, SystemAccount, TelegramExpenseDraft,
    TelegramLink, Transaction, TransactionType, User, Workspace, WorkspaceMember,
)
from app.security import hash_password
from app.services.telegram_ai import AIUnavailableError, FinancialIntent


def setup_linked_user(db):
    account = SystemAccount(name="Telegram Finance")
    db.add(account)
    db.flush()
    user = User(
        system_account_id=account.id, username="expense-user", full_name="Expense User",
        password_hash=hash_password("password123"), account_role=AccountRole.admin,
    )
    workspace = Workspace(system_account_id=account.id, name="Familia")
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id))
    person = Person(workspace_id=workspace.id, name="Vitor", person_type=PersonType.personal)
    db.add(person)
    db.flush()
    user.default_person_id = person.id
    category = Category(
        workspace_id=workspace.id, name="Mercado", parent_name="Alimentacao",
        kind=TransactionType.expense,
    )
    db.add(category)
    db.add(TelegramLink(user_id=user.id, telegram_user_id="777", telegram_chat_id="777"))
    db.commit()
    return user


def message(text):
    return {"text": text, "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}


def test_expense_is_only_created_after_natural_confirmation(monkeypatch):
    def interpret(db, user, text, draft):
        if text == "sim, pode registrar":
            return FinancialIntent("confirm")
        return FinancialIntent("create_expense", Decimal("85.90"), "Mercado da esquina")
    monkeypatch.setattr("app.automation.telegram_bot.interpret_financial_message", interpret)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("gastei 85,90 no mercado da esquina"))
        assert "Confirme este gasto" in response
        assert "Alimentacao > Mercado" in response
        assert db.scalar(select(func.count(Transaction.id))) == 0

        response = handle_message(db, message("sim, pode registrar"))
        assert "registrado com sucesso" in response
        transaction = db.scalar(select(Transaction))
        assert transaction.amount == Decimal("85.90")
        assert transaction.source == "telegram"
        assert transaction.person.name == "Vitor"
        assert transaction.category.name == "Mercado"

        handle_message(db, message("sim, pode registrar"))
        assert db.scalar(select(func.count(Transaction.id))) == 1


def test_expense_can_be_cancelled_and_expired_draft_is_not_confirmed(monkeypatch):
    def interpret(db, user, text, draft):
        if "cancela" in text:
            return FinancialIntent("cancel")
        if text == "pode lancar":
            return FinancialIntent("confirm")
        value = Decimal("20") if "lanche" in text else Decimal("30")
        return FinancialIntent("create_expense", value, "lanche" if value == 20 else "transporte")
    monkeypatch.setattr("app.automation.telegram_bot.interpret_financial_message", interpret)
    with SessionLocal() as db:
        setup_linked_user(db)
        handle_message(db, message("paguei 20 num lanche"))
        assert handle_message(db, message("nao, cancela isso")) == "Gasto cancelado."
        assert db.scalar(select(func.count(Transaction.id))) == 0

        handle_message(db, message("gastei 30 com transporte"))
        draft = db.scalar(select(TelegramExpenseDraft).where(TelegramExpenseDraft.status == "awaiting_confirmation"))
        draft.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        assert "expirou" in handle_message(db, message("pode lancar"))
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_unlinked_telegram_cannot_create_expense():
    with SessionLocal() as db:
        response = handle_message(db, {
            "text": "gastei 100 no mercado", "from": {"id": 999},
            "chat": {"id": 999, "type": "private"},
        })
        assert "nao esta associado" in response
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_ai_failure_does_not_create_transaction(monkeypatch):
    def unavailable(*args):
        raise AIUnavailableError("offline")
    monkeypatch.setattr("app.automation.telegram_bot.interpret_financial_message", unavailable)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("gastei 50 no posto"))
        assert "interface web" in response
        assert db.scalar(select(func.count(Transaction.id))) == 0
