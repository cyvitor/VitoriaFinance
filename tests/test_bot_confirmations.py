from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.automation.telegram_bot import handle_callback, handle_message
from app.database import SessionLocal
from app.models import (
    Category, Person, RecurrenceRule, TelegramExpenseDraft, TelegramPendingAction, Transaction,
    TransactionStatus, TransactionType,
)
from app.services.fixed_bills import BotReply
from test_fixed_bills_telegram import TODAY, callback, llm, message, payments, seed


def seed_youtube(db):
    user, luz, claro_v, claro_e = seed(db)
    youtube = RecurrenceRule(workspace_id=luz.workspace_id, transaction_type=TransactionType.expense,
                             description="Youtube", amount=Decimal("24.90"), person_id=luz.person_id,
                             account_id=luz.account_id, is_active=True, created_at=datetime(2026, 9, 1, 12))
    db.add(youtube); db.commit()
    return user, youtube


def datas(reply):
    return [b["callback_data"] for row in (reply.keyboard or []) for b in row]


def button(reply, prefix):
    return next(item for item in datas(reply) if item.startswith(prefix))


def drafts(db):
    return db.scalars(select(TelegramExpenseDraft)).all()


def test_real_case_confirma_despesa_youtube_asks_and_goes_to_fixed_bill(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "Youtube", "card": "C6", "payment_method": "crédito"}})
    with SessionLocal() as db:
        user, youtube = seed_youtube(db)
        ask = handle_message(db, message("Confirma a despesa youtube c6 crédito"))
        assert isinstance(ask, BotReply)
        assert "conta fixa **Youtube** de **09/2026**" in ask.text and "despesa nova" in ask.text
        assert drafts(db) == []  # nada preparado como despesa nova
        chosen = handle_callback(db, callback(button(ask, "ec:b:")))
        assert "Confirme o pagamento da conta fixa" in chosen.text
        assert "Forma de pagamento: **Cartão C6 (crédito)**" in chosen.text  # forma reaproveitada
        again = handle_callback(db, callback(button(ask, "ec:b:")))
        assert "já foi feita" in again.text
        handle_callback(db, callback(chosen.keyboard[0][0]["callback_data"]))
        tx = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == youtube.id))
        assert tx.card_id is not None and tx.amount == Decimal("24.90")


def test_case_insensitive_match_and_new_expense_choice(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "YOUTUBE premium", "amount": 30, "account": "Caixa",
                                    "payment_method": "débito"}})
    with SessionLocal() as db:
        user, youtube = seed_youtube(db)
        ask = handle_message(db, message("YOUTUBE premium 30 caixa"))
        new = handle_callback(db, callback(button(ask, "ec:n:")))
        assert len(drafts(db)) == 1 and payments(db, youtube.id) == 0
        assert "ok:c:d:" in "".join(datas(new)) and "ok:x:d:" in "".join(datas(new))
        cancel = handle_callback(db, callback(button(new, "ok:x:d:")))
        assert "Nada foi registrado" in cancel.text or "cancel" in cancel.text.lower()
        assert drafts(db)[0].status == "cancelled"
        assert "já foi cancelada" in handle_callback(db, callback(button(new, "ok:c:d:"))).text
        assert db.scalar(select(Transaction).where(Transaction.description.ilike("%youtube%"))) is None


def test_keyword_paguei_goes_straight_to_fixed_bill(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "youtube", "amount": 24.90}})
    with SessionLocal() as db:
        user, youtube = seed_youtube(db)
        reply = handle_message(db, message("paguei o youtube"))
        assert "Pagou como" in reply.text and "ec:" not in "".join(datas(reply))
        assert drafts(db) == []


def test_keyword_conta_fixa_goes_to_fixed_bill(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "youtube", "card": "C6", "payment_method": "credito"}})
    with SessionLocal() as db:
        user, youtube = seed_youtube(db)
        reply = handle_message(db, message("confirmar a conta youtube no c6 credito"))
        assert "Confirme o pagamento da conta fixa" in reply.text and "Cartão C6" in reply.text


def test_keyword_gastei_creates_new_expense_with_buttons(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "Youtube filme", "amount": 15, "account": "Caixa",
                                    "payment_method": "débito"}})
    with SessionLocal() as db:
        user, youtube = seed_youtube(db)
        reply = handle_message(db, message("gastei 15 no youtube filme caixa debito"))
        assert isinstance(reply, BotReply) and "ec:" not in "".join(datas(reply))
        draft = drafts(db)[0]
        assert datas(reply) == [f"ok:c:d:{draft.id}", f"ok:x:d:{draft.id}"]
        if draft.category_id is None:
            category = Category(workspace_id=draft.workspace_id, parent_name="Lazer", name="Streaming",
                                kind=TransactionType.expense)
            db.add(category); db.flush(); draft.category_id = category.id; db.commit()
        done = handle_callback(db, callback(f"ok:c:d:{draft.id}"))
        assert "registrada" in done.text
        assert "já foi registrada" in handle_callback(db, callback(f"ok:c:d:{draft.id}")).text
        assert len(db.scalars(select(Transaction).where(Transaction.description == "Youtube filme")).all()) == 1
        assert "nao esta associado" in handle_callback(db, callback(f"ok:c:d:{draft.id}", sender=999)).text


def test_income_confirmation_has_buttons_and_is_idempotent(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_receita",
                      "arguments": {"amount": 260, "description": "Central VT", "transaction_date": "2026-10-13",
                                    "payment_method": "PIX", "account": "Caixa"}})
    with SessionLocal() as db:
        user, _ = seed_youtube(db)
        vitor = db.scalar(select(Person).where(Person.name == "Vitor"))
        db.add(Category(workspace_id=vitor.workspace_id, parent_name="Receitas", name="PIX",
                        kind=TransactionType.income)); db.commit()
        reply = handle_message(db, message("recebi 260 central vt pix na caixa"))
        action = db.scalar(select(TelegramPendingAction).where(TelegramPendingAction.status == "awaiting_confirmation"))
        assert datas(reply) == [f"ok:c:a:{action.id}", f"ok:x:a:{action.id}"]
        done = handle_callback(db, callback(f"ok:c:a:{action.id}"))
        assert "Receita registrada" in done.text
        assert "já foi confirmada" in handle_callback(db, callback(f"ok:c:a:{action.id}")).text
        incomes = db.scalars(select(Transaction).where(Transaction.description == "Central VT")).all()
        assert len(incomes) == 1 and incomes[0].status == TransactionStatus.paid


def test_text_sim_still_confirms_after_buttons(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"description": "Padaria", "amount": 12, "account": "Caixa",
                                    "payment_method": "débito", "category": "Alimentação"}})
    with SessionLocal() as db:
        user, _ = seed_youtube(db)
        reply = handle_message(db, message("comprei pão 12 caixa debito"))
        draft = drafts(db)[0]
        assert f"ok:c:d:{draft.id}" in datas(reply)