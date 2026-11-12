"""Gasto programado usa o resumo padrão e não duplica a competência."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Account, AccountType, Card, Category, RecurrenceOccurrence, RecurrenceRule, SystemSetting, Transaction,
    TransactionType, UserMemory,
)
from app.services.telegram_agent import run_financial_agent
from test_telegram_expenses import setup_linked_user


def _seed(db, *, extra_rule=False, preferred=False):
    user = setup_linked_user(db)
    from app.models import Person
    person = db.get(Person, user.default_person_id)
    account = Account(workspace_id=person.workspace_id, person_id=person.id, name="C6",
                      bank_name="C6", account_type=AccountType.checking)
    db.add(account)
    db.flush()
    db.add(Card(workspace_id=person.workspace_id, account_id=account.id, name="C6", closing_day=5, due_day=12))
    category = Category(workspace_id=person.workspace_id, name="Assinaturas", parent_name="Lazer",
                        kind=TransactionType.expense)
    db.add(category)
    db.flush()
    rule = RecurrenceRule(
        workspace_id=person.workspace_id, transaction_type=TransactionType.expense,
        description="YouTube", amount=Decimal("53.90"), person_id=person.id, account_id=account.id,
        category_id=category.id, is_active=True, created_at=datetime.utcnow(),
    )
    db.add(rule)
    if extra_rule:
        db.add(RecurrenceRule(
            workspace_id=person.workspace_id, transaction_type=TransactionType.expense,
            description="YouTube Music", amount=Decimal("16.90"), person_id=person.id, account_id=account.id,
            category_id=category.id, is_active=True, created_at=datetime.utcnow(),
        ))
    if preferred:
        db.add(UserMemory(
            user_id=user.id, memory_type="preferred_card", subject="cartao padrao",
            value_json='{"card":"C6","payment_method":"credit"}',
            summary="Sem cartão informado, usar C6 no crédito.", confidence=Decimal("0.950"),
            source="user_explicit",
        ))
    db.add(SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True))
    db.commit()
    return user, rule


def test_programmed_expense_with_payment_uses_the_standard_template_and_marks_paid(monkeypatch):
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *a, **k: "{}")
    with SessionLocal() as db:
        user, rule = _seed(db)
        reply = run_financial_agent(db, user, "confirme o gasto programado youtube c6, credito")
        assert reply.startswith("Confirme este gasto:")
        assert "Descricao: YouTube" in reply
        assert "Valor: R$ 53,90" in reply
        assert "Categoria: Lazer > Assinaturas" in reply
        assert "Forma de pagamento: Crédito" in reply
        assert "Cartao: C6" in reply
        assert "Posso registrar?" in reply
        assert db.info["bot_keyboard"][0][0]["callback_data"].startswith("ok:c:a:")
        done = run_financial_agent(db, user, "sim")
        assert "registrado" in done.casefold()
        occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.recurrence_rule_id == rule.id))
        assert occurrence.status == "confirmed"
        assert db.scalar(select(func.count(Transaction.id))) == 1
        again = run_financial_agent(db, user, "confirme o gasto programado youtube c6, credito")
        assert "já foi confirmado" in again or "já estava confirmado" in again
        assert db.scalar(select(func.count(Transaction.id))) == 1


def test_programmed_expense_asks_payment_and_mentions_preferred_card(monkeypatch):
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *a, **k: "{}")
    with SessionLocal() as db:
        user, rule = _seed(db, preferred=True)
        ask = run_financial_agent(db, user, "confirme o gasto programado youtube")
        assert "Pagou como" in ask
        assert "C6" in ask
        assert "Confirme este gasto" not in ask
        reply = run_financial_agent(db, user, "c6 crédito")
        assert "Confirme este gasto:" in reply and "Valor: R$ 53,90" in reply and "Cartao: C6" in reply
        assert db.scalar(select(func.count(Transaction.id))) == 0


def test_programmed_expense_asks_when_several_bills_match(monkeypatch):
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *a, **k: None)
    monkeypatch.setattr("app.services.telegram_agent._complete", lambda *a, **k: "{}")
    with SessionLocal() as db:
        user, _rule = _seed(db, extra_rule=True)
        reply = run_financial_agent(db, user, "confirme o gasto programado youtube")
        assert "mais de uma" in reply
        assert db.scalar(select(func.count(Transaction.id))) == 0
