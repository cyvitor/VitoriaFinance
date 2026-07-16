from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, Financing, FinancingAmortization, MemberRole, Person, PersonType,
    RecurrenceRule, SystemAccount, SystemSetting, Transaction, TransactionStatus, TransactionType, User, UserMemory, UserPersonAccess, Workspace, WorkspaceMember,
)
from app.security import hash_password
from app.services.access_context import build_user_access_context
from app.services.financial_tools import ToolError, execute_tool
from app.services.telegram_agent import run_financial_agent
from app.services.telegram_ai import AIUnavailableError
from app.services.user_memory import relevant_memories


def setup_financings(db, member_role=MemberRole.editor, account_role=AccountRole.admin):
    account = SystemAccount(name="Familia")
    db.add(account); db.flush()
    user = User(system_account_id=account.id, username="agent-user", full_name="Agente",
                password_hash=hash_password("password123"), account_role=account_role)
    workspace = Workspace(system_account_id=account.id, name="Casa")
    db.add_all([user, workspace]); db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=member_role))
    vitor = Person(workspace_id=workspace.id, name="Vitor", person_type=PersonType.personal)
    wife = Person(workspace_id=workspace.id, name="Esposa", person_type=PersonType.personal)
    db.add_all([vitor, wife]); db.flush(); user.default_person_id = vitor.id
    if account_role == AccountRole.member:
        db.add(UserPersonAccess(user_id=user.id, person_id=vitor.id))
    rule = RecurrenceRule(workspace_id=workspace.id, transaction_type=TransactionType.expense,
                          description="Imovel - parcela 163/360", amount=Decimal("1097.16"), person_id=vitor.id)
    db.add(rule); db.flush()
    financing = Financing(workspace_id=workspace.id, person_id=vitor.id, recurrence_rule_id=rule.id,
        description="Imovel", paid_installments=162, total_installments=360,
        installment_amount=Decimal("1097.16"), outstanding_balance=Decimal("85081.93"), status="active")
    hidden = Financing(workspace_id=workspace.id, person_id=wife.id, description="Carro da esposa",
        paid_installments=10, total_installments=48, installment_amount=Decimal("1500"),
        outstanding_balance=Decimal("50000"), status="active")
    db.add_all([financing, hidden]); db.commit(); db.refresh(user)
    return user, financing, hidden, rule


def test_tools_never_return_financing_from_forbidden_area():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db, account_role=AccountRole.member)
        context = build_user_access_context(db, user)
        result = execute_tool(db, context, "consultar_financiamentos", {})
        assert [item["description"] for item in result["financings"]] == ["Imovel"]
        with pytest.raises(ToolError):
            execute_tool(db, context, "preparar_amortizacao_financiamento", {
                "financing": "Carro da esposa", "new_outstanding_balance": 40000,
                "remaining_installments": 30,
            })


def test_amortization_collects_missing_data_and_updates_only_after_confirmation():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        context = build_user_access_context(db, user)
        first = execute_tool(db, context, "preparar_amortizacao_financiamento", {"financing": "Imovel"})
        assert first["status"] == "collecting" and "novo saldo devedor oficial" in first["missing_fields"]
        second = execute_tool(db, context, "preparar_amortizacao_financiamento", {
            "new_outstanding_balance": 72450.30, "remaining_installments": 124,
        })
        assert second["status"] == "awaiting_confirmation"
        db.refresh(financing)
        assert financing.outstanding_balance == Decimal("85081.93")
        result = execute_tool(db, context, "confirmar_acao_pendente", {})
        db.refresh(financing); db.refresh(rule)
        assert result["updated"] is True
        assert financing.outstanding_balance == Decimal("72450.30")
        assert financing.total_installments == 286
        assert rule.description == "Imovel - parcela 163/286"
        history = db.scalar(select(FinancingAmortization))
        assert history.previous_outstanding_balance == Decimal("85081.93")
        assert history.new_outstanding_balance == Decimal("72450.30")
        assert history.source == "telegram"


def test_read_only_members_cannot_amortize():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(
            db, member_role=MemberRole.reader, account_role=AccountRole.member
        )
        context = build_user_access_context(db, user)
        assert context.can_write is False
        with pytest.raises(ToolError, match="permissao"):
            execute_tool(db, context, "preparar_amortizacao_financiamento", {"financing": "Imovel"})


def test_agent_uses_natural_conversation_and_keeps_confirmed_write_on_synthesis_failure(monkeypatch):
    responses = iter([
        json.dumps({"action": "tool", "tool": "preparar_despesa",
                    "arguments": {"amount": 42.5, "description": "Cinema"}}),
        "Entendi: R$ 42,50 no cinema. Posso registrar?",
        json.dumps({"action": "tool", "tool": "confirmar_despesa", "arguments": {}}),
        AIUnavailableError("offline apos gravacao"),
    ])

    def complete(*args, **kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("app.services.telegram_agent._complete", complete)
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args: None)
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        db.add_all([SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
                    SystemSetting(key="deepinfra_model", value="deepseek")])
        db.commit()
        first = run_financial_agent(db, user, "gastei 42,50 no cinema")
        assert "Posso registrar" in first
        assert db.scalar(select(Transaction)) is None
        second = run_financial_agent(db, user, "sim, pode registrar")
        assert "registrada com sucesso" in second
        transaction = db.scalar(select(Transaction))
        assert transaction.amount == Decimal("42.50") and transaction.source == "telegram"


def test_agent_automatically_extracts_permanent_memory(monkeypatch):
    responses = iter([
        json.dumps({"action": "respond", "reply": "Entendi."}),
        json.dumps({"candidates": [{
            "type": "merchant_alias", "subject": "padaria",
            "value": {"merchant": "Doce Pao"},
            "summary": "Padaria normalmente significa Doce Pao.", "confidence": 0.82,
        }]}),
    ])
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: next(responses))
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        db.add_all([SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
                    SystemSetting(key="deepinfra_model", value="deepseek")])
        db.commit()
        assert run_financial_agent(db, user, "Quando eu disser padaria, geralmente e a Doce Pao.") == "Entendi."
        memory = db.scalar(select(UserMemory).where(UserMemory.user_id == user.id))
        assert memory.memory_type == "merchant_alias" and memory.subject == "padaria"
        assert relevant_memories(db, user.id, "gastei quinze na padaria") == [memory]


def test_free_balance_tool_simulates_planned_spending():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        person = db.scalar(select(Person).where(Person.name == "Vitor"))
        account = Account(workspace_id=person.workspace_id, person_id=person.id, name="Conta",
                          account_type=AccountType.checking, initial_balance=Decimal("1000"))
        db.add(account); db.flush()
        today = date.today()
        db.add_all([
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.income, description="Receita", amount=Decimal("500"),
                        transaction_date=today, status=TransactionStatus.paid, created_by_id=user.id),
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.expense, description="Pago", amount=Decimal("200"),
                        transaction_date=today, status=TransactionStatus.paid, created_by_id=user.id),
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.expense, description="Pendente", amount=Decimal("300"),
                        transaction_date=today, competence_year=today.year, competence_month=today.month,
                        status=TransactionStatus.pending, created_by_id=user.id),
        ])
        # A regra criada pelo financiamento vale 1.097,16 e ainda nao foi resolvida no mes.
        db.commit()
        context = build_user_access_context(db, user)
        result = execute_tool(db, context, "consultar_saldo_livre", {"planned_spending": 100})
        assert result["current_balance"] == "1300.00"
        assert result["pending_transactions"] == "300.00"
        assert result["unresolved_recurring_commitments"] == "1097.16"
        assert result["free_balance_after_planned_spending"] == "-197.16"
        assert result["can_afford"] is False


def test_agent_retries_plain_text_and_forces_financial_tool(monkeypatch):
    responses = iter([
        "Deixa eu verificar seus dados para voce.",
        json.dumps({"action": "tool", "tool": "consultar_saldo_livre",
                    "arguments": {"planned_spending": 100}}),
        "Depois de considerar seus compromissos, o gasto de R$ 100,00 nao cabe no saldo livre.",
    ])
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args: None)
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        db.add_all([SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
                    SystemSetting(key="deepinfra_model", value="deepseek")])
        db.commit()
        reply = run_financial_agent(db, user, "ainda tenho saldo livre? pretendo gastar 100 reais")
        assert "R$ 100,00" in reply
