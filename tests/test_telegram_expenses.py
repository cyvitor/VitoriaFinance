from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, Card, Category, Person, PersonType, SystemAccount, TelegramLink,
    TelegramExpenseQueueItem, Transaction, TransactionType, User, Workspace, WorkspaceMember,
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


def test_pharmacy_alias_resolves_to_health_medication_category():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        medication = Category(workspace_id=person.workspace_id, name="Medicamentos",
                              parent_name="Saude", kind=TransactionType.expense)
        db.add(medication); db.commit()
        execute_tool(db, context, "preparar_despesa", {
            "amount": 4.99, "description": "Fraumed",
        })
        result = execute_tool(db, context, "atualizar_despesa", {
            "category": "Saúde > Farmácia",
        })
        assert result["status"] == "awaiting_confirmation"
        assert "Categoria: Saude > Medicamentos" in result["summary"]


def test_category_is_inferred_from_confirmed_transactions_including_web_changes():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        category = db.scalar(select(Category).where(Category.name == "Mercado"))
        db.add(Transaction(
            workspace_id=person.workspace_id, person_id=person.id,
            transaction_type=TransactionType.expense, description="Hiperideal",
            amount=Decimal("80.00"), transaction_date=date(2026, 7, 20),
            competence_year=2026, competence_month=7, category_id=category.id,
            created_by_id=user.id, source="web",
        ))
        db.commit()

        result = execute_tool(db, context, "preparar_despesa", {
            "amount": 101.40, "description": "Hiperideal", "transaction_date": "2026-07-31",
        })
        assert result["status"] == "awaiting_confirmation"
        assert "Categoria: Alimentacao > Mercado" in result["summary"]


def test_category_recommendation_chooses_valid_merchant_category():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        db.add(Category(
            workspace_id=person.workspace_id, name="Barbearia",
            parent_name="Cuidados Pessoais", kind=TransactionType.expense,
        ))
        db.commit()
        execute_tool(db, context, "preparar_despesa", {
            "amount": 35, "description": "Barbeiro", "transaction_date": "2026-07-31",
        })
        result = execute_tool(db, context, "sugerir_categoria_despesa", {})
        assert result["recommended"] is True
        assert result["category"] == "Cuidados Pessoais > Barbearia"
        assert "Categoria: Cuidados Pessoais > Barbearia" in result["summary"]


def test_telegram_message_timestamp_is_used_as_agent_reference_date(monkeypatch):
    captured = {}

    def agent(db, user, text, reference_date=None):
        captured["reference_date"] = reference_date
        return "ok"

    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", agent)
    with SessionLocal() as db:
        setup_linked_user(db)
        timestamp = int(datetime(2026, 7, 31, 19, 18).timestamp())
        payload = message("gastei 10 no mercado")
        payload["date"] = timestamp
        assert handle_message(db, payload) == "ok"
        assert captured["reference_date"] == date(2026, 7, 31)


def test_multiple_expenses_advance_sequentially_after_confirmation():
    with SessionLocal() as db:
        user = setup_linked_user(db); context = build_user_access_context(db, user)
        person = db.get(Person, user.default_person_id)
        cafe = Category(workspace_id=person.workspace_id, name="Cafe", parent_name="Alimentacao",
                        kind=TransactionType.expense)
        account = Account(workspace_id=person.workspace_id, person_id=person.id, name="Conta C6",
                          account_type=AccountType.checking)
        db.add_all([cafe, account]); db.flush()
        card = Card(workspace_id=person.workspace_id, account_id=account.id, name="C6",
                    closing_day=5, due_day=10)
        db.add(card); db.commit()
        result = execute_tool(db, context, "preparar_despesas", {"expenses": [
            {"amount": 96.02, "description": "Hiperideal", "transaction_date": "2026-07-22",
             "category": "Alimentacao > Mercado", "payment_method": "credit", "card": "C6"},
            {"amount": 8, "description": "Afemaria", "transaction_date": "2026-07-23",
             "category": "Alimentacao > Cafe", "payment_method": "credit", "card": "C6"},
        ]})
        assert result["batch"] == {"total": 2, "current": 1, "queued": 1}
        assert "Hiperideal" in result["summary"]
        assert db.scalar(select(func.count(TelegramExpenseQueueItem.id))) == 1

        first = execute_tool(db, context, "confirmar_despesa", {})
        assert first["registered"] is True
        assert "Afemaria" in first["next_expense"]["summary"]
        assert first["next_expense"]["queue"]["remaining_after_current"] == 0
        first_queue_item = db.scalar(select(TelegramExpenseQueueItem))
        assert first_queue_item.status == "processing"

        second = execute_tool(db, context, "confirmar_despesa", {})
        assert second["registered"] is True and second["next_expense"] is None
        assert first_queue_item.status == "confirmed"
        transactions = db.scalars(select(Transaction).order_by(Transaction.id)).all()
        assert [(item.description, item.transaction_date) for item in transactions] == [
            ("Hiperideal", date(2026, 7, 22)), ("Afemaria", date(2026, 7, 23)),
        ]


def test_unlinked_telegram_cannot_use_agent(monkeypatch):
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", lambda *args: "nao deveria executar")
    with SessionLocal() as db:
        response = handle_message(db, message("quanto gastei?", sender=999))
        assert "nao esta associado" in response


def test_ai_failure_does_not_create_transaction(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AIUnavailableError("offline")
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", unavailable)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("gastei 50 no posto"))
        assert "interface web" in response
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_invalid_structured_response_has_specific_message(monkeypatch):
    def invalid(*args, **kwargs):
        raise AIResponseFormatError("json invalido")
    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", invalid)
    with SessionLocal() as db:
        setup_linked_user(db)
        response = handle_message(db, message("quanto ainda posso gastar?"))
        assert "organizar essa consulta" in response
        assert "DeepInfra" not in response
