from datetime import date
from decimal import Decimal
import json

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, Card, CardBillingPeriod, Category, MemberRole,
    MonthlyBudget, Person, PersonType, SystemAccount, SystemSetting, Transaction, TransactionStatus,
    TransactionType, User, Workspace, WorkspaceMember,
)
from app.security import hash_password
from app.services.access_context import build_user_access_context
from app.services.financial_tools import execute_tool
from app.services.telegram_agent import _purchase_simulation_reply
from app.services.telegram_agent import run_financial_agent


def setup_purchase_scenario(db, *, initial_balance="10000.00", credit_limit="1000.00"):
    system_account = SystemAccount(name="Familia")
    db.add(system_account); db.flush()
    user = User(
        system_account_id=system_account.id,
        username="comprador",
        full_name="Vitor Hugo",
        password_hash=hash_password("password123"),
        account_role=AccountRole.admin,
    )
    workspace = Workspace(system_account_id=system_account.id, name="Casa")
    db.add_all([user, workspace]); db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=MemberRole.admin))
    person = Person(workspace_id=workspace.id, name="Vitor", person_type=PersonType.personal)
    db.add(person); db.flush(); user.default_person_id = person.id
    account = Account(
        workspace_id=workspace.id,
        person_id=person.id,
        name="Conta C6",
        account_type=AccountType.checking,
        initial_balance=Decimal(initial_balance),
    )
    category = Category(
        workspace_id=workspace.id,
        parent_name="Lazer",
        name="Eletronicos",
        kind=TransactionType.expense,
    )
    db.add_all([account, category]); db.flush()
    card = Card(
        workspace_id=workspace.id,
        account_id=account.id,
        name="C6",
        credit_limit=Decimal(credit_limit),
        closing_day=5,
        due_day=10,
    )
    db.add(card); db.commit(); db.refresh(user)
    return user, workspace, person, account, card, category


def test_purchase_simulation_uses_budget_reserve_without_creating_transaction():
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(db)
        db.add(MonthlyBudget(
            workspace_id=workspace.id,
            person_id=person.id,
            category_id=category.id,
            amount=Decimal("300.00"),
            start_year=2026,
            start_month=9,
            include_in_projection=True,
            is_active=True,
        ))
        db.add(Transaction(
            workspace_id=workspace.id,
            transaction_type=TransactionType.expense,
            description="Compra anterior",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 9, 1),
            competence_year=2026,
            competence_month=9,
            status=TransactionStatus.pending,
            account_id=account.id,
            card_id=card.id,
            person_id=person.id,
            payment_method="Crédito",
            created_by_id=user.id,
        ))
        db.commit()
        before_count = db.scalar(select(func.count(Transaction.id)))

        result = execute_tool(db, build_user_access_context(db, user), "simular_compra_cartao", {
            "amount": 200,
            "card": "C6",
            "installments": 2,
            "category": "Eletronicos",
            "purchase_date": "2026-09-14",
            "horizon_months": 6,
        })

        assert result["status"] == "simulated"
        assert result["read_only"] is True
        assert result["purchase"]["first_invoice"] == "2026-09"
        assert result["first_invoice"] == {"before": "100.00", "after": "200.00"}
        assert result["card_limit"]["available_before"] == "900.00"
        assert result["card_limit"]["available_after"] == "700.00"
        assert result["projections"][0]["new_installment"] == "100.00"
        assert result["projections"][0]["absorbed_by_budget"] == "100.00"
        assert result["projections"][0]["balance_before"] == result["projections"][0]["balance_after"]
        assert db.scalar(select(func.count(Transaction.id))) == before_count


def test_purchase_simulation_moves_first_installment_past_closed_invoice():
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(db)
        db.add(CardBillingPeriod(
            workspace_id=workspace.id,
            card_id=card.id,
            year=2026,
            month=9,
            is_closed=True,
        ))
        db.commit()

        result = execute_tool(db, build_user_access_context(db, user), "simular_compra_cartao", {
            "amount": 200,
            "card": "C6",
            "category": "Lazer Eletronicos",
            "purchase_date": "2026-09-14",
            "horizon_months": 3,
        })

        assert result["purchase"]["first_invoice"] == "2026-10"
        assert result["projections"][0]["competence"] == "2026-10"


def test_purchase_simulation_flags_budget_overrun_and_negative_balance():
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(
            db, initial_balance="300.00", credit_limit="1000.00",
        )
        db.add(MonthlyBudget(
            workspace_id=workspace.id,
            person_id=person.id,
            category_id=category.id,
            amount=Decimal("50.00"),
            start_year=2026,
            start_month=9,
            include_in_projection=True,
            is_active=True,
        ))
        db.commit()

        result = execute_tool(db, build_user_access_context(db, user), "simular_compra_cartao", {
            "amount": 400,
            "card": "C6",
            "category": "Eletronicos",
            "purchase_date": "2026-09-14",
            "horizon_months": 3,
        })

        assert result["category_budget"][0]["exceeds_budget"] is True
        assert result["category_budget"][0]["exceeded_by"] == "350.00"
        assert result["projections"][0]["balance_after"] == "-100.00"
        assert result["risk"]["level"] == "risky"


def test_purchase_simulation_requests_only_missing_information():
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(db)
        result = execute_tool(db, build_user_access_context(db, user), "simular_compra_cartao", {
            "amount": 200,
        })
        assert result["status"] == "missing_information"
        assert result["missing_fields"] == ["categoria"]
        reply = _purchase_simulation_reply(result)
        assert "categoria" in reply
        assert "nenhum lançamento" in reply


def test_purchase_simulation_turns_unknown_category_into_a_conversational_follow_up():
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(db)
        db.add(Category(
            workspace_id=workspace.id,
            parent_name="Saúde",
            name="Consulta",
            kind=TransactionType.expense,
        ))
        db.commit()
        result = execute_tool(db, build_user_access_context(db, user), "simular_compra_cartao", {
            "amount": 150,
            "category": "Podólogo",
            "purchase_date": "2026-09-14",
        })
        assert result["status"] == "missing_information"
        assert result["missing_fields"] == ["categoria"]
        assert result["known_purchase"]["amount"] == "150.00"
        assert result["known_purchase"]["requested_category"] == "Podólogo"
        assert "Saúde > Consulta" in result["category_options"]


def test_purchase_simulation_error_fallback_never_invents_zero_values():
    reply = _purchase_simulation_reply({"error": "Categoria não encontrada"})
    assert "não consegui concluir" in reply.lower()
    assert "R$ 0,00" not in reply
    assert "None" not in reply


def test_agent_can_leave_purchase_simulation_and_change_subject(monkeypatch):
    responses = iter([
        json.dumps({
            "action": "tool",
            "tool": "simular_compra_cartao",
            "arguments": {
                "amount": 200,
                "card": "C6",
                "category": "Eletronicos",
                "purchase_date": "2026-09-14",
            },
        }),
        "Pelos números atuais, essa compra cabe. Como é uma necessidade, não faz sentido tratá-la como impulso.",
        json.dumps({"action": "respond", "reply": "Claro. Sobre qual assunto você quer falar?"}),
    ])
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args: None)
    with SessionLocal() as db:
        user, workspace, person, account, card, category = setup_purchase_scenario(db)
        db.add_all([
            SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
            SystemSetting(key="deepinfra_model", value="deepseek"),
        ])
        db.commit()
        simulation = run_financial_agent(
            db, user, "Estou pensando em comprar um eletrônico de 200 reais no C6",
            reference_date=date(2026, 9, 14),
        )
        assert simulation == (
            "Pelos números atuais, essa compra cabe. Como é uma necessidade, "
            "não faz sentido tratá-la como impulso."
        )
        assert db.scalar(select(Transaction)) is None
        changed_subject = run_financial_agent(
            db, user, "Mudei de assunto: quero conversar sobre outra coisa",
            reference_date=date(2026, 9, 14),
        )
        assert changed_subject == "Claro. Sobre qual assunto você quer falar?"
