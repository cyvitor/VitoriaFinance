from decimal import Decimal

from sqlalchemy import func, select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, Card, Category, Person, PersonType, SystemAccount, TelegramLink,
    Transaction, TransactionType, User, Workspace, WorkspaceMember,
)
from app.security import hash_password
from app.services.access_context import build_user_access_context
from app.services.financial_tools import execute_tool
from app.services.telegram_ai import AIResponseFormatError, AIUnavailableError


def setup_linked_user(db):
    account = SystemAccount(name="Telegram Finance")
    db.add(account); db.flush()
    user = User(system_account_id=account.id, username="expense-user", full_name="Expense User",
                password_hash=hash_password("password123"), account_role=AccountRole.admin)
    workspace = Workspace(system_account_id=account.id, name="Familia")
    db.add_all([user, workspace]); db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id))
    person = Person(workspace_id=workspace.id, name="Vitor", person_type=PersonType.personal)
    db.add(person); db.flush(); user.default_person_id = person.id
    db.add(Category(workspace_id=workspace.id, name="Mercado", parent_name="Alimentacao",
                    kind=TransactionType.expense))
    db.add(TelegramLink(user_id=user.id, telegram_user_id="777", telegram_chat_id="777"))
    db.commit(); db.refresh(user)
    return user


def message(text, sender=777):
    return {"text": text, "from": {"id": sender}, "chat": {"id": sender, "type": "private"}}


def test_expense_tool_only_creates_transaction_after_confirmation():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        result = execute_tool(db, context, "preparar_despesa", {"amount": 85.90, "description": "Mercado da esquina"})
        assert result["status"] == "awaiting_confirmation"
        assert db.scalar(select(func.count(Transaction.id))) == 0
        result = execute_tool(db, context, "confirmar_despesa", {})
        assert result["registered"] is True
        transaction = db.scalar(select(Transaction))
        assert transaction.amount == Decimal("85.90") and transaction.source == "telegram"
        assert transaction.person.name == "Vitor" and transaction.category.name == "Mercado"


def test_expense_tool_can_cancel_draft():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        execute_tool(db, context, "preparar_despesa", {"amount": 20, "description": "Lanche"})
        assert execute_tool(db, context, "cancelar_despesa", {})["cancelled"] is True
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_expense_draft_persists_category_and_credit_card_before_confirmation():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        category = Category(workspace_id=person.workspace_id, name="Padaria", parent_name="Alimentacao",
                            kind=TransactionType.expense)
        account = Account(workspace_id=person.workspace_id, person_id=person.id, name="Conta C6",
                          account_type=AccountType.checking)
        db.add_all([category, account]); db.flush()
        card = Card(workspace_id=person.workspace_id, account_id=account.id, name="C6", closing_day=5, due_day=10)
        db.add(card); db.commit()
        execute_tool(db, context, "preparar_despesa", {"amount": 5, "description": "Doce Pao"})
        updated = execute_tool(db, context, "atualizar_despesa", {
            "category": "Alimentacao Padaria", "payment_method": "credit", "card": "C6",
        })
        assert updated["status"] == "awaiting_confirmation"
        execute_tool(db, context, "confirmar_despesa", {})
        transaction = db.scalar(select(Transaction))
        assert transaction.category.name == "Padaria"
        assert transaction.payment_method == "Crédito" and transaction.card.name == "C6"
        assert transaction.account_id == account.id and str(transaction.status) == "TransactionStatus.pending"


def test_parent_category_returns_real_subcategories_and_preserves_relative_date():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        db.add_all([
            Category(workspace_id=person.workspace_id, name="Cinema", parent_name="Lazer",
                     kind=TransactionType.expense),
            Category(workspace_id=person.workspace_id, name="Jogos", parent_name="Lazer",
                     kind=TransactionType.expense),
        ])
        db.commit()
        result = execute_tool(db, context, "preparar_despesa", {
            "amount": 57, "description": "GameStation", "category": "Lazer",
            "transaction_date": "2026-07-21",
        })
        assert result["status"] == "missing_information"
        assert result["missing_fields"] == ["subcategoria"]
        assert result["category_options"] == ["Lazer > Cinema", "Lazer > Jogos"]
        result = execute_tool(db, context, "atualizar_despesa", {"category": "Jogos"})
        assert result["status"] == "awaiting_confirmation"
        assert "Data: 21/07/2026" in result["summary"]
        assert "Categoria: Lazer > Jogos" in result["summary"]


def test_unlinked_telegram_cannot_use_agent(monkeypatch):
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", lambda *args: "nao deveria executar")
    with SessionLocal() as db:
        response = handle_message(db, message("quanto gastei?", sender=999))
        assert "nao esta associado" in response


def test_ai_failure_does_not_create_transaction(monkeypatch):
    def unavailable(*args):
        raise AIUnavailableError("offline")
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", unavailable)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("gastei 50 no posto"))
        assert "interface web" in response
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_invalid_structured_response_has_specific_message(monkeypatch):
    def invalid(*args):
        raise AIResponseFormatError("json invalido")
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", invalid)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("quanto ainda posso gastar?"))
        assert "organizar essa consulta" in response
        assert "DeepInfra" not in response
