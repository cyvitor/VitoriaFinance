from datetime import datetime, timezone
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Account, AccountType, Card, Category, SystemSetting, Transaction, TransactionStatus, TransactionType,
    UserMemory, Workspace,
)
from app.routers.web import br_datetime
from app.services.access_context import build_user_access_context
from app.services.financial_tools import execute_tool
from app.services.telegram_agent import run_financial_agent
from test_telegram_expenses import setup_linked_user
from test_web import seed_test


def _setup(db):
    user = setup_linked_user(db)
    person_workspace = db.scalar(select(Workspace.id))
    db.add(Category(workspace_id=person_workspace, name="Jogos", parent_name="Lazer", kind=TransactionType.expense))
    db.add_all([SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
                SystemSetting(key="deepinfra_model", value="deepseek")])
    db.commit()
    return user


def _fake_model(monkeypatch, description, narrated_category):
    """Seletor: prepara a despesa sem categoria; sintese: o modelo 'inventa' a categoria do resumo."""
    def complete(token, model, messages, *args, **kwargs):
        if kwargs.get("json_mode"):
            if messages[-1]["content"].strip().lower() == "sim":
                return json.dumps({"action": "tool", "tool": "confirmar_despesa", "arguments": {}})
            return json.dumps({"action": "tool", "tool": "preparar_despesa",
                               "arguments": {"amount": 8.6, "description": description}})
        return f"Confirme: {description}, R$ 8,60. Categoria: {narrated_category}. Posso registrar?"

    monkeypatch.setattr("app.services.telegram_agent._complete", complete)
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args: None)


def test_summary_category_is_the_one_saved_even_if_the_model_narrates_another(monkeypatch):
    _fake_model(monkeypatch, "Vampire the Masquerade", "Lazer > Jogos")
    with SessionLocal() as db:
        user = _setup(db)
        summary = run_financial_agent(db, user, "gastei 8,60 em Vampire the Masquerade")
        assert "Categoria: Nao identificada" in summary  # resumo vem do rascunho, nao do texto do modelo
        assert "Lazer > Jogos" not in summary
        confirmation = run_financial_agent(db, user, "sim")
        transaction = db.scalar(select(Transaction))
        assert transaction.category_id is None
        assert "sem categoria" in confirmation  # a confirmacao informa o que foi gravado


def test_category_remembered_for_the_merchant_is_applied_shown_and_saved(monkeypatch):
    _fake_model(monkeypatch, "Steam Vampire the Masquerade", "Lazer > Jogos")
    with SessionLocal() as db:
        user = _setup(db)
        db.add(UserMemory(user_id=user.id, memory_type="category_preference", subject="steam",
                          value_json=json.dumps({"category": "Lazer > Jogos"}),
                          summary="Compras na Steam sao da categoria Lazer > Jogos.", confidence=0.9))
        db.commit()
        summary = run_financial_agent(db, user, "gastei 8,60 na Steam")
        assert "Categoria: Lazer > Jogos" in summary
        confirmation = run_financial_agent(db, user, "sim")
        transaction = db.scalar(select(Transaction))
        assert (transaction.category.parent_name, transaction.category.name) == ("Lazer", "Jogos")
        assert "Categoria: **Lazer > Jogos**" in confirmation


def test_unrelated_memory_does_not_assign_a_category():
    with SessionLocal() as db:
        user = _setup(db)
        db.add(UserMemory(user_id=user.id, memory_type="category_preference", subject="steam",
                          value_json="{}", summary="Steam e Lazer > Jogos.", confidence=0.9))
        db.commit()
        context = build_user_access_context(db, user)
        result = execute_tool(db, context, "preparar_despesa", {"amount": 30, "description": "Padaria"})
        assert "Categoria: Nao identificada" in result["summary"]


def test_preparing_the_same_expense_again_keeps_category_and_date():
    with SessionLocal() as db:
        user = _setup(db)
        context = build_user_access_context(db, user)
        first = execute_tool(db, context, "preparar_despesa", {
            "amount": 8.6, "description": "Loja Y", "category": "Jogos", "transaction_date": "2026-11-09"})
        assert "Lazer > Jogos" in first["summary"]
        again = execute_tool(db, context, "preparar_despesa", {"amount": 8.6, "description": "Loja Y"})
        assert "Categoria: Lazer > Jogos" in again["summary"] and "09/11/2026" in again["summary"]
        registered = execute_tool(db, context, "confirmar_despesa", {})
        assert registered["category"] == "Lazer > Jogos"
        assert db.scalar(select(Transaction)).category.name == "Jogos"


def test_utc_timestamps_are_shown_in_sao_paulo_time():
    assert br_datetime(datetime(2026, 11, 9, 15, 49)) == "09/11/2026 às 12:49"
    assert br_datetime(datetime(2026, 11, 9, 15, 49, tzinfo=timezone.utc), "%H:%M") == "12:49"
    assert br_datetime(datetime(2026, 11, 10, 1, 30)) == "09/11/2026 às 22:30"  # vira o dia
    assert br_datetime(None) == ""


def test_card_page_shows_inclusion_time_in_sao_paulo(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        account = Account(workspace_id=workspace_id, person_id=area_id, name="C6 Conta", account_type=AccountType.checking)
        db.add(account); db.flush()
        card = Card(workspace_id=workspace_id, account_id=account.id, name="NuVirtual", closing_day=5, due_day=12)
        db.add(card); db.flush()
        db.add(Transaction(
            workspace_id=workspace_id, transaction_type=TransactionType.expense, description="Steam", amount=8.6,
            transaction_date=datetime(2026, 11, 9).date(), competence_year=2026, competence_month=11,
            status=TransactionStatus.pending, person_id=area_id, account_id=account.id, card_id=card.id,
            payment_method="Crédito", created_by_id=1, created_at=datetime(2026, 11, 9, 15, 49),
        ))
        db.commit(); card_id = card.id
    page = client.get(f"/cards/{card_id}?year=2026&month=11")
    assert "incluído 09/11/2026 às 12:49" in page.text
    assert "15:49" not in page.text.split("incluído", 1)[1][:40]