from datetime import date, datetime, timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, Card, CardBillingPeriod, Category, Financing, FinancingAmortization, MemberRole, Person, PersonType,
    FuelFillup, RecurrenceRule, SystemAccount, SystemSetting, TelegramPendingAction, Transaction,
    TransactionStatus, TransactionType, User, UserMemory, UserPersonAccess, Vehicle, Workspace, WorkspaceMember,
)
from app.security import hash_password
from app.services.access_context import build_user_access_context
from app.services.financial_tools import ToolError, execute_tool
from app.services.telegram_agent import (
    _ai_category_recommendation, _enforce_balance_intent, _enforce_category_recommendation_intent,
    _enforce_expense_reference_date, _enforce_pending_confirmation_intent,
    _enforce_pending_description_intent, _enforce_pending_expense_intent,
    _expense_tool_reply, _requires_financial_tool,
    _enforce_spending_feasibility_intent, _enforce_transaction_period_intent,
    _fallback_tool_reply, _looks_like_multiple_expenses, run_financial_agent,
)
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


def test_fuel_fillup_tool_calculates_liters_from_known_expense_total():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        vehicle = Vehicle(workspace_id=financing.workspace_id, name="Corsa", initial_odometer_km=100000)
        transaction = Transaction(
            workspace_id=financing.workspace_id, transaction_type=TransactionType.expense,
            description="Gasolina", amount=Decimal("50.00"), transaction_date=date(2026, 9, 8),
            competence_year=2026, competence_month=9, status=TransactionStatus.pending,
            person_id=financing.person_id, created_by_id=user.id,
        )
        db.add_all([vehicle, transaction]); db.flush()
        db.add(TelegramPendingAction(
            user_id=user.id, action_type="fuel_fillup", status="collecting",
            payload=json.dumps({"transaction_id": transaction.id, "phase": "offer"}),
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        ))
        db.commit()
        result = execute_tool(db, build_user_access_context(db, user), "preparar_abastecimento", {
            "odometer_km": 140000, "price_per_liter": 7,
        })
        assert result["status"] == "awaiting_confirmation"
        assert result["liters"] == "7.143"
        assert db.scalar(select(FuelFillup)) is None
        result = execute_tool(db, build_user_access_context(db, user), "confirmar_acao_pendente", {})
        assert result["registered"] is True
        fillup = db.scalar(select(FuelFillup))
        assert fillup.vehicle_id == vehicle.id
        correction = execute_tool(db, build_user_access_context(db, user), "preparar_correcao_abastecimento", {
            "new_price_per_liter": 5.66,
        })
        assert correction["status"] == "awaiting_confirmation"
        execute_tool(db, build_user_access_context(db, user), "confirmar_acao_pendente", {})
        db.refresh(fillup)
        assert fillup.price_per_liter == Decimal("5.660")
        assert fillup.liters == Decimal("8.834")


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


def test_income_collects_destination_account_and_registers_only_after_confirmation():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        vitor = db.get(Person, user.default_person_id)
        db.add_all([
            Account(
                workspace_id=vitor.workspace_id, person_id=vitor.id, name="C6",
                bank_name="C6", account_type=AccountType.checking, initial_balance=0,
            ),
            Account(
                workspace_id=vitor.workspace_id, person_id=vitor.id, name="NU",
                bank_name="Nubank", account_type=AccountType.checking, initial_balance=0,
            ),
            Category(
                workspace_id=vitor.workspace_id, parent_name="Receitas", name="PIX",
                kind=TransactionType.income,
            ),
        ])
        db.commit()
        context = build_user_access_context(db, user)

        first = execute_tool(db, context, "preparar_receita", {
            "amount": 260, "description": "Central VT",
            "transaction_date": "2026-07-29", "payment_method": "PIX",
        })
        assert first["status"] == "collecting"
        assert first["missing_fields"] == ["conta de destino"]
        assert first["account_options"] == ["C6", "NU"]
        assert db.scalar(select(Transaction)) is None

        second = execute_tool(db, context, "preparar_receita", {"account": "C6"})
        assert second["status"] == "awaiting_confirmation"
        assert second["preview"]["category"] == "Receitas > PIX"
        assert db.scalar(select(Transaction)) is None

        result = execute_tool(db, context, "confirmar_acao_pendente", {})
        transaction = db.scalar(select(Transaction))
        assert result["registered"] is True
        assert result["transaction_type"] == "income"
        assert transaction.amount == Decimal("260.00")
        assert transaction.description == "Central VT"
        assert transaction.transaction_date == date(2026, 7, 29)
        assert transaction.transaction_type == TransactionType.income
        assert transaction.status == TransactionStatus.paid
        assert transaction.account.name == "C6"
        assert transaction.category.name == "PIX"
        assert transaction.payment_method == "PIX"
        assert transaction.source == "telegram"
        categories = execute_tool(db, context, "consultar_categorias_receita", {})
        assert categories["categories"] == ["Receitas > PIX"]


def test_registered_transaction_is_corrected_only_after_confirmation():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        vitor = db.get(Person, user.default_person_id)
        category = Category(
            workspace_id=vitor.workspace_id, parent_name="Lazer", name="Cinema",
            kind=TransactionType.expense,
        )
        account = Account(workspace_id=vitor.workspace_id, person_id=vitor.id, name="C6 Conta",
                          account_type=AccountType.checking)
        db.add_all([category, account]); db.flush()
        card = Card(workspace_id=vitor.workspace_id, account_id=account.id, name="C6",
                    closing_day=5, due_day=10)
        db.add(card); db.flush()
        db.add(CardBillingPeriod(workspace_id=vitor.workspace_id, card_id=card.id,
                                 year=2026, month=7, is_closed=True))
        expense = Transaction(
            workspace_id=vitor.workspace_id, transaction_type=TransactionType.expense,
            description="Pipoca no cinema", amount=Decimal("61.00"),
            transaction_date=date(2026, 7, 30), competence_year=2026, competence_month=7,
            status=TransactionStatus.paid, person_id=vitor.id, account_id=account.id,
            card_id=card.id, category_id=category.id,
            payment_method="Crédito", created_by_id=user.id, source="telegram",
        )
        db.add(expense); db.commit()
        context = build_user_access_context(db, user)

        prepared = execute_tool(db, context, "preparar_correcao_lancamento", {
            "target_description": "Pipoca no cinema", "target_amount": 61,
            "new_transaction_date": "2026-07-29", "new_invoice_month": "2026-08",
        })
        assert prepared["status"] == "awaiting_confirmation"
        assert prepared["before"]["transaction_date"] == "2026-07-30"
        assert prepared["after"]["transaction_date"] == "2026-07-29"
        assert prepared["before"]["invoice_month"] == "2026-07"
        assert prepared["after"]["invoice_month"] == "2026-08"
        db.refresh(expense)
        assert expense.transaction_date == date(2026, 7, 30)

        result = execute_tool(db, context, "confirmar_acao_pendente", {})
        db.refresh(expense)
        assert result["transaction_updated"] is True
        assert expense.transaction_date == date(2026, 7, 29)
        assert (expense.competence_year, expense.competence_month) == (2026, 8)
        assert expense.status == TransactionStatus.pending
        assert expense.amount == Decimal("61.00")
        assert db.scalar(select(func.count(Transaction.id)).where(
            Transaction.description == "Pipoca no cinema"
        )) == 1


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
        card = Card(workspace_id=person.workspace_id, account_id=account.id, name="Cartao",
                    closing_day=max(1, today.day - 1), due_day=10, credit_limit=Decimal("2000"))
        income_rule = RecurrenceRule(workspace_id=person.workspace_id, person_id=person.id,
            transaction_type=TransactionType.income, description="Salario futuro", amount=Decimal("2000"))
        db.add_all([
            card, income_rule,
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.income, description="Receita", amount=Decimal("500"),
                        transaction_date=today, competence_year=today.year, competence_month=today.month,
                        status=TransactionStatus.paid, created_by_id=user.id),
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.expense, description="Pago", amount=Decimal("200"),
                        transaction_date=today, competence_year=today.year, competence_month=today.month,
                        status=TransactionStatus.paid, created_by_id=user.id),
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
        assert result["projected_income_month"] == "2500.00"
        assert result["projected_expense_month"] == "1597.16"
        assert result["projected_month_result_after_planned_spending"] == "802.84"
        credit = execute_tool(db, context, "consultar_saldo_livre", {
            "planned_spending": 100, "payment_method": "credit", "card": "Cartao",
        })
        assert credit["card_context"]["affects_current_month"] is True
        assert credit["projected_month_result_after_planned_spending"] == "802.84"
        assert credit["card_context"]["next_competence_commitment"] == "0.00"
        assert credit["can_close_month"] is True
        db.add(CardBillingPeriod(workspace_id=person.workspace_id, card_id=card.id,
                                 year=today.year, month=today.month, is_closed=True))
        db.commit()
        after_close = execute_tool(db, context, "consultar_saldo_livre", {
            "planned_spending": 100, "payment_method": "credit", "card": "Cartao",
        })
        assert after_close["card_context"]["affects_current_month"] is False
        assert after_close["projected_month_result_after_planned_spending"] == "902.84"
        assert after_close["card_context"]["next_competence_commitment"] == "100.00"


def test_credit_spending_and_month_close_are_forced_to_projection():
    first = _enforce_balance_intent(
        "Posso gastar 150 no crédito?", [],
        {"action": "tool", "tool": "consultar_saldo_livre", "arguments": {"planned_spending": 150}},
    )
    assert first == {"action": "tool", "tool": "consultar_saldo_livre", "arguments": {
        "planned_spending": 150.0, "payment_method": "credit",
    }}

    history = [
        {"role": "user", "content": "Posso gastar 150 no crédito?"},
        {"role": "assistant", "content": "Resposta anterior"},
    ]
    follow_up = _enforce_balance_intent(
        "E no cartão de crédito? Consigo fechar o mês?", history,
        {"action": "tool", "tool": "consultar_cartoes", "arguments": {"area": "Vitor"}},
    )
    assert follow_up["tool"] == "consultar_saldo_livre"
    assert follow_up["arguments"] == {
        "area": "Vitor", "payment_method": "credit", "planned_spending": 150.0,
    }

    card_list = _enforce_balance_intent(
        "Quais cartões eu tenho?", [],
        {"action": "tool", "tool": "consultar_cartoes", "arguments": {}},
    )
    assert card_list["tool"] == "consultar_cartoes"


def test_going_out_question_uses_full_financial_projection_without_inventing_amount():
    decision = _enforce_spending_feasibility_intent(
        "Como está meu mês financeiro? Sextou amanhã, posso sair pra tomar uma?",
        [{"role": "user", "content": "Ontem gastei 80 no mercado"}],
        {"action": "tool", "tool": "consultar_resumo_mensal",
         "arguments": {"start_date": "2026-08-01", "end_date": "2026-08-31"}},
    )
    assert decision == {"action": "tool", "tool": "consultar_saldo_livre", "arguments": {}}


def test_balance_fallback_answers_query_instead_of_returning_format_error():
    reply = _fallback_tool_reply("consultar_saldo_livre", {
        "current_balance": "500.00", "free_balance": "120.00",
        "projected_month_result": "739.77", "planned_spending": "0.00",
    })
    assert "R$ 120,00" in reply
    assert "R$ 739,77" in reply
    assert "quanto pretende gastar" in reply


def test_agent_respects_model_tool_selection_without_fixed_intent_override(monkeypatch):
    responses = iter([
        json.dumps({"action": "tool", "tool": "consultar_resumo_mensal", "arguments": {}}),
        "Receitas e despesas consultadas.",
    ])
    called = {}

    def execute(_db, _context, tool, arguments):
        called.update(tool=tool, arguments=arguments)
        return {
            "current_balance": "500.00", "free_balance": "120.00",
            "projected_month_result": "739.77", "planned_spending": "0.00",
            "payment_method": "cash",
        }

    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("app.services.telegram_agent.execute_tool", execute)
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args: None)
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        db.add_all([SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
                    SystemSetting(key="deepinfra_model", value="deepseek")])
        db.commit()
        reply = run_financial_agent(
            db, user, "Como está meu mês financeiro? Sextou amanhã, posso sair pra tomar uma?",
            reference_date=date(2026, 8, 20),
        )
    assert called == {"tool": "consultar_resumo_mensal", "arguments": {}}
    assert reply == "Receitas e despesas consultadas."


def test_expense_typo_with_financial_context_still_requires_tool():
    assert _requires_financial_tool("gatei 5 no credito c6 na doce pão") is True
    assert _requires_financial_tool(
        "grave na sua memória que se eu não disser a data do gasto, considere o dia corrente"
    ) is False


def test_explicit_default_date_preference_is_stored_without_deepinfra():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        reply = run_financial_agent(
            db, user,
            "Grave na sua memória q se eu n disse q data do gasto, considerar sempre o dia corrente",
            reference_date=date(2026, 8, 6),
        )
        assert "guardei essa preferência" in reply
        memory = db.scalar(select(UserMemory).where(
            UserMemory.user_id == user.id,
            UserMemory.subject == "data_padrao_dos_gastos",
        ))
        assert memory is not None
        assert json.loads(memory.value_json)["default"] == "telegram_message_date"


def test_category_reply_updates_existing_expense_instead_of_recreating_it():
    decision = {"action": "tool", "tool": "preparar_despesa", "arguments": {
        "amount": 57, "description": "GameStation", "category": "Jogos",
    }}
    corrected = _enforce_pending_expense_intent("Jogos", True, decision)
    assert corrected["tool"] == "atualizar_despesa"
    assert corrected["arguments"]["category"] == "Jogos"


def test_explicit_pending_description_is_kept_with_category_update():
    corrected = _enforce_pending_description_intent(
        "Colocar na descrição: uber tia, categoria uber", True,
        {"action": "tool", "tool": "atualizar_despesa", "arguments": {"category": "Uber"}},
    )
    assert corrected == {
        "action": "tool", "tool": "atualizar_despesa",
        "arguments": {"category": "Uber", "description": "uber tia"},
    }


def test_category_help_and_message_date_are_enforced_without_model_guessing():
    recommendation = _enforce_category_recommendation_intent(
        "procure a categoria que mais se encaixa", True, {"action": "undecided"},
    )
    assert recommendation["tool"] == "sugerir_categoria_despesa"
    dated = _enforce_expense_reference_date({
        "action": "tool", "tool": "preparar_despesa",
        "arguments": {"amount": 101.40, "description": "Hiperideal"},
    }, date(2026, 7, 31))
    assert dated["arguments"]["transaction_date"] == "2026-07-31"


def test_model_cannot_reuse_old_date_when_expense_message_has_no_date():
    dated = _enforce_expense_reference_date({
        "action": "tool", "tool": "preparar_despesa",
        "arguments": {
            "amount": 105.07, "description": "Openia", "transaction_date": "2026-08-21",
        },
    }, date(2026, 8, 26), "105,07 openia")
    assert dated["arguments"]["transaction_date"] == "2026-08-26"


def test_explicit_expense_date_is_resolved_from_current_telegram_message():
    yesterday = _enforce_expense_reference_date({
        "action": "tool", "tool": "preparar_despesa",
        "arguments": {"amount": 64.94, "description": "Hiperideal", "transaction_date": "2026-08-21"},
    }, date(2026, 8, 27), "64,94 hiperideal ontem")
    assert yesterday["arguments"]["transaction_date"] == "2026-08-26"

    absolute = _enforce_expense_reference_date({
        "action": "tool", "tool": "preparar_despesa",
        "arguments": {"amount": 64.94, "description": "Hiperideal"},
    }, date(2026, 8, 27), "64,94 hiperideal no dia 24/08")
    assert absolute["arguments"]["transaction_date"] == "2026-08-24"


def test_batch_without_dates_forces_message_date_for_every_expense():
    dated = _enforce_expense_reference_date({
        "action": "tool", "tool": "preparar_despesas",
        "arguments": {"expenses": [
            {"amount": 10, "description": "Primeiro", "transaction_date": "2026-08-21"},
            {"amount": 20, "description": "Segundo", "transaction_date": "2026-08-20"},
        ]},
    }, date(2026, 8, 27), "10 no primeiro e 20 no segundo")
    assert [item["transaction_date"] for item in dated["arguments"]["expenses"]] == [
        "2026-08-27", "2026-08-27",
    ]


def test_ai_category_recommendation_is_restricted_to_valid_options(monkeypatch):
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: json.dumps({
        "category": "Alimentação > Delivery", "confidence": 0.91,
    }))
    result = _ai_category_recommendation("token", "model", {
        "description": "Ifood",
        "category_options": ["Alimentação > Mercado", "Alimentação > Delivery"],
    })
    assert result == "Alimentação > Delivery"

    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *args, **kwargs: json.dumps({
        "category": "Categoria inventada", "confidence": 0.99,
    }))
    assert _ai_category_recommendation("token", "model", {
        "description": "Loja", "category_options": ["Outros > Diversos"],
    }) is None


def test_multiple_expenses_are_detected_without_confusing_dates_or_card_name():
    text = "gastei 96,02 no hiperideal ontem, e 8 no afemaria hj, os dois no credito c6"
    assert _looks_like_multiple_expenses(text) is True
    assert _looks_like_multiple_expenses("gastei 57 no C6 em 22/07/2026") is False


def test_expense_replies_always_identify_pending_and_next_item():
    missing = _expense_tool_reply("atualizar_despesa", {
        "status": "missing_information", "missing_fields": ["subcategoria"],
        "category_options": ["Saúde > Medicamentos", "Saúde > Consulta"],
        "summary": "Confirme este gasto:\n\nDescricao: Fraumed\nValor: R$ 4,99\n\nPosso registrar?",
    }, "resposta vaga")
    assert "Fraumed" in missing and "Saúde > Medicamentos" in missing
    assert "Posso registrar?" not in missing

    advanced = _expense_tool_reply("confirmar_despesa", {
        "registered": True, "description": "Drogasil", "amount": "109.92",
        "next_expense": {"summary": "Descricao: Fraumed\nValor: R$ 4,99"},
    }, "resposta incompleta")
    assert "Drogasil" in advanced and "Fraumed" in advanced


def test_pending_expense_confirmation_does_not_recreate_batch_from_history():
    batch_decision = {"action": "tool", "tool": "preparar_despesas", "arguments": {
        "expenses": [{"amount": 13.43}, {"amount": 29.90}],
    }}
    assert _enforce_pending_confirmation_intent("yes", True, batch_decision)["tool"] == "confirmar_despesa"
    assert _enforce_pending_confirmation_intent("pode", True, batch_decision)["tool"] == "confirmar_despesa"
    assert _enforce_pending_confirmation_intent(
        "não, já foi registrado", True, batch_decision
    )["tool"] == "cancelar_despesa"


def test_today_expense_query_includes_credit_debit_and_accounts():
    with SessionLocal() as db:
        user, financing, hidden, rule = setup_financings(db)
        person = db.scalar(select(Person).where(Person.name == "Vitor"))
        account = Account(workspace_id=person.workspace_id, person_id=person.id, name="Conta",
                          account_type=AccountType.checking)
        db.add(account); db.flush()
        card = Card(workspace_id=person.workspace_id, account_id=account.id, name="C6",
                    closing_day=5, due_day=10)
        db.add(card); db.flush()
        today = date.today()
        db.add_all([
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        card_id=card.id, transaction_type=TransactionType.expense,
                        description="Credito", amount=Decimal("10"), transaction_date=today,
                        status=TransactionStatus.pending, payment_method="Crédito", created_by_id=user.id),
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        card_id=card.id, transaction_type=TransactionType.expense,
                        description="Debito", amount=Decimal("20"), transaction_date=today,
                        status=TransactionStatus.paid, payment_method="Débito", created_by_id=user.id),
            Transaction(workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
                        transaction_type=TransactionType.expense, description="Conta",
                        amount=Decimal("30"), transaction_date=today,
                        status=TransactionStatus.paid, payment_method="Pix", created_by_id=user.id),
        ])
        db.commit()
        context = build_user_access_context(db, user)
        result = execute_tool(db, context, "consultar_despesas", {
            "start_date": today.isoformat(), "end_date": today.isoformat(),
        })
        assert result["total"] == "60.00"
        assert {item["type"] for item in result["payment_breakdown"]} == {
            "cartao_credito", "cartao_debito", "conta",
        }
        assert result["coverage"]["statuses"].startswith("lançamentos pagos e pendentes")

    decision = _enforce_transaction_period_intent("quanto gastei hj?", {
        "action": "tool", "tool": "consultar_despesas", "arguments": {},
    })
    assert decision["arguments"] == {
        "start_date": date.today().isoformat(), "end_date": date.today().isoformat(),
    }


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
