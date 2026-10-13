from datetime import date, datetime
from decimal import Decimal
import json

from sqlalchemy import func, select

from app.automation.telegram_bot import handle_callback, handle_message
from app.database import SessionLocal
from app.models import (
    Account, AccountType, Card, Person, RecurrenceOccurrence, RecurrenceRule, SystemSetting, TelegramLink,
    TelegramPendingAction, Transaction, TransactionStatus, TransactionType,
)
from app.services import fixed_bills
from app.services.fixed_bills import BotReply, prepare_payment, send_daily_reminders
from app.services.telegram_agent import run_financial_agent
from app.services.telegram_ai import AIUnavailableError
from test_financial_agent import setup_financings

TODAY = date(2026, 10, 13)
CHAT = 555


def seed(db):
    user, financing, hidden, imovel = setup_financings(db)
    imovel.is_active = False  # foca nas contas criadas aqui
    vitor = db.scalar(select(Person).where(Person.name == "Vitor"))
    esposa = db.scalar(select(Person).where(Person.name == "Esposa"))
    caixa = Account(workspace_id=vitor.workspace_id, person_id=vitor.id, name="Caixa",
                    account_type=AccountType.checking, initial_balance=Decimal("1000"))
    santander = Account(workspace_id=vitor.workspace_id, person_id=esposa.id, name="Santander",
                        account_type=AccountType.checking, initial_balance=Decimal("1000"))
    c6 = Account(workspace_id=vitor.workspace_id, person_id=vitor.id, name="C6",
                 account_type=AccountType.checking, initial_balance=Decimal("500"))
    db.add_all([caixa, santander, c6]); db.flush()
    db.add(Card(workspace_id=vitor.workspace_id, account_id=c6.id, name="C6", closing_day=5, due_day=12))
    created = datetime(2026, 9, 1, 12, 0)

    def rule(description, amount, person, account):
        item = RecurrenceRule(workspace_id=vitor.workspace_id, transaction_type=TransactionType.expense,
                              description=description, amount=Decimal(amount), person_id=person.id,
                              account_id=account.id, is_active=True, created_at=created)
        db.add(item); return item

    luz = rule("Luz", "300.00", vitor, caixa)
    claro_v = rule("Claro", "80.00", vitor, caixa)
    claro_e = rule("Claro", "77.00", esposa, santander)
    db.add(TelegramLink(user_id=user.id, telegram_user_id=str(CHAT), telegram_chat_id=str(CHAT)))
    db.add(SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True))
    db.commit()
    return user, luz, claro_v, claro_e


def message(text):
    stamp = int(datetime(2026, 10, 13, 12, 0).timestamp())
    return {"text": text, "date": stamp, "from": {"id": CHAT}, "chat": {"id": CHAT, "type": "private"}}


def callback(data, sender=CHAT):
    return {"id": "cb", "data": data, "from": {"id": sender},
            "message": {"chat": {"id": sender, "type": "private"}, "date": int(datetime(2026, 10, 13, 12).timestamp())}}


def llm(monkeypatch, *decisions):
    queue = list(decisions)

    def complete(*args, **kwargs):
        if queue:
            return json.dumps(queue.pop(0))
        raise AIUnavailableError("ocupado")
    monkeypatch.setattr("app.services.telegram_agent._complete", complete)
    monkeypatch.setattr(fixed_bills, "today_sp", lambda: TODAY)


def pick(reply, label):
    """Toca no botao de 'Pagou como?' cujo texto contem label."""
    return next(b["callback_data"] for row in reply.keyboard for b in row if label in b["text"])


def payments(db, rule_id):
    return db.scalar(select(func.count(Transaction.id)).where(Transaction.recurrence_rule_id == rule_id))


def test_pending_command_lists_oldest_competence_first_with_buttons(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        reply = handle_message(db, message("/contas"))
        assert isinstance(reply, BotReply)
        assert reply.text.index("09/2026") < reply.text.index("10/2026")
        assert "Claro (Esposa)" in reply.text and "Luz (Vitor)" in reply.text
        datas = [button["callback_data"] for row in reply.keyboard for button in row]
        assert f"fb:p:{luz.id}:202609" in datas and f"fb:m:{luz.id}" in datas
        assert all(len(item.encode()) <= 64 for item in datas)


def test_natural_payment_with_different_amount_and_local_confirmation(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_pagamento_conta_fixa",
                      "arguments": {"bill": "luz", "amount": 287.40}})
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        ask = run_financial_agent(db, user, "paguei luz 287,40 hoje", reference_date=TODAY)
        assert "Pagou como" in ask and "⭐ Conta Caixa (débito)" in ask and "Cartão C6 (crédito)" in ask
        reply = handle_message(db, message("débito caixa")).text
        assert "Forma de pagamento: **Conta Caixa (débito)**" in reply
        assert "Competência: **09/2026**" in reply
        assert "R$ 287,40" in reply and "diferente do previsto" in reply and "10/2026" in reply
        assert payments(db, luz.id) == 0
        done = run_financial_agent(db, user, "sim", reference_date=TODAY)
        assert "Pagamento registrado" in done
        transaction = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == luz.id))
        assert transaction.amount == Decimal("287.40") and transaction.source == "telegram"
        assert transaction.status == TransactionStatus.paid and transaction.account.name == "Caixa"
        assert (transaction.competence_year, transaction.competence_month) == (2026, 9)
        occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.recurrence_rule_id == luz.id))
        assert (occurrence.year, occurrence.month, occurrence.status) == (2026, 9, "confirmed")


def test_model_choosing_generic_expense_is_corrected_to_fixed_bill(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_despesa",
                      "arguments": {"amount": 300, "description": "Luz"}})
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        reply = run_financial_agent(db, user, "paguei a luz", reference_date=TODAY)
        assert "Pagou como" in reply and "Luz (Vitor)" in reply
        cancelled = handle_message(db, message("não")).text
        assert "Nada foi registrado" in cancelled and payments(db, luz.id) == 0


def test_ambiguous_name_asks_which_area(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, claro_v, claro_e = seed(db)
        result = prepare_payment(db, user.id, today=TODAY, bill="claro")
        assert result["status"] == "ambiguous"
        assert result["options"] == ["Claro (Vitor)", "Claro (Esposa)"] or set(result["options"]) == {
            "Claro (Vitor)", "Claro (Esposa)"}
        chosen = prepare_payment(db, user.id, today=TODAY, bill="claro da esposa")
        assert chosen["status"] == "missing_method" and chosen["rule_id"] == claro_e.id
        assert chosen["source_options"] == ["Conta Santander (débito)"]
        assert "Santander" in chosen["message"]


def test_current_month_competence_by_text_and_button(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        result = prepare_payment(db, user.id, today=TODAY, bill="luz", competence="atual")
        assert result["competence"] == "2026-10"
        ask = handle_callback(db, callback(f"fb:m:{luz.id}"))
        reply = handle_callback(db, callback(pick(ask, "Caixa")))
        assert "Competência: **10/2026**" in reply.text
        handle_callback(db, callback(reply.keyboard[0][0]["callback_data"]))
        occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.recurrence_rule_id == luz.id))
        assert (occurrence.year, occurrence.month) == (2026, 10)
        again = prepare_payment(db, user.id, today=TODAY, bill="luz", competence="atual")
        assert again["status"] == "already_resolved"


def test_callbacks_are_idempotent_and_validate_user(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        ask1 = handle_callback(db, callback(f"fb:p:{luz.id}:202609"))
        ask2 = handle_callback(db, callback(f"fb:p:{luz.id}:202609"))
        assert ask1.keyboard == ask2.keyboard  # clique duplo reaproveita o mesmo rascunho
        first = handle_callback(db, callback(pick(ask1, "Caixa")))
        again = handle_callback(db, callback(pick(ask1, "Caixa")))
        assert first.keyboard == again.keyboard
        confirm = first.keyboard[0][0]["callback_data"]
        stranger = handle_callback(db, callback(confirm, sender=999))
        assert "nao esta associado" in stranger.text and payments(db, luz.id) == 0
        assert "Pagamento registrado" in handle_callback(db, callback(confirm)).text
        assert "já foi registrado" in handle_callback(db, callback(confirm)).text
        assert payments(db, luz.id) == 1
        # Botao antigo da lista para a mesma competencia nao duplica o pagamento.
        stale = handle_callback(db, callback(f"fb:p:{luz.id}:202609"))
        assert "já foi confirmada" in stale.text and payments(db, luz.id) == 1
        assert "Não reconheci" in handle_callback(db, callback("fb:zz")).text


def test_other_value_button_asks_amount(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        ask = handle_callback(db, callback(f"fb:v:{luz.id}:202609"))
        assert "Qual foi o valor pago" in ask.text and ask.keyboard is None
        method = handle_message(db, message("250,90"))
        assert isinstance(method, BotReply) and "Pagou como" in method.text and "R$ 250,90" in method.text
        summary = handle_callback(db, callback(pick(method, "Caixa")))
        assert "R$ 250,90" in summary.text
        handle_callback(db, callback(summary.keyboard[0][0]["callback_data"]))
        transaction = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == luz.id))
        assert transaction.amount == Decimal("250.90")


def test_due_day_by_text_and_daily_reminder_without_duplicates(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, claro_v, claro_e = seed(db)
        reply = handle_message(db, message("luz vence dia 10"))
        assert "vence todo dia 10" in reply.text
        db.refresh(luz); assert luz.days_of_month == "10"
        ambiguous = handle_message(db, message("claro vence dia 13"))
        assert "mais de uma conta" in ambiguous.text
        handle_message(db, message("claro da esposa vence dia 13"))
        sent = []
        sender = lambda chat, reply: sent.append((chat, reply))
        tz = fixed_bills.SAO_PAULO
        assert send_daily_reminders(db, sender, datetime(2026, 10, 13, 7, 59, tzinfo=tz)) == 0
        assert send_daily_reminders(db, sender, datetime(2026, 10, 13, 8, 0, tzinfo=tz)) == 1
        chat, text = sent[0][0], sent[0][1].text
        assert chat == str(CHAT)
        assert "Luz (Vitor)" in text and "vencida em 10/10" in text and "vence hoje" in text
        assert sent[0][1].keyboard
        # restart do worker no mesmo dia: nao repete
        assert send_daily_reminders(db, sender, datetime(2026, 10, 13, 15, 0, tzinfo=tz)) == 0
        db.add(SystemSetting(key="bill_reminder_time", value="09:30")); db.commit()
        assert send_daily_reminders(db, sender, datetime(2026, 10, 14, 9, 0, tzinfo=tz)) == 0
        assert send_daily_reminders(db, sender, datetime(2026, 10, 14, 9, 31, tzinfo=tz)) == 1


def test_admin_fixed_expense_due_day_and_reminder_time(client):
    from test_web import seed_test
    seed_test(); client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        person = db.scalar(select(Person))
        person_id = person.id
    client.post("/fixed-expenses", data={"description": "Agua", "amount": "90", "person_id": str(person_id),
                                         "due_day": "15"})
    with SessionLocal() as db:
        rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.description == "Agua"))
        assert rule.days_of_month == "15"
        rule_id = rule.id
    assert "vence dia 15" in client.get("/fixed-expenses").text
    client.post(f"/fixed-expenses/{rule_id}/edit", data={"description": "Agua", "amount": "95", "due_day": "20"})
    with SessionLocal() as db:
        assert db.get(RecurrenceRule, rule_id).days_of_month == "20"
    client.post("/system/settings", data={"deepinfra_api_key": "", "deepinfra_model": "", "telegram_bot_token": "",
                                          "bill_reminder_time": "07:45"})
    with SessionLocal() as db:
        assert db.scalar(select(SystemSetting.value).where(SystemSetting.key == "bill_reminder_time")) == "07:45"
    assert 'name="bill_reminder_time"' in client.get("/system/settings").text

def test_card_payment_by_button_goes_to_invoice_like_web(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        ask = handle_callback(db, callback(f"fb:p:{luz.id}:202609"))
        assert ask.keyboard[0][0]["text"].startswith("⭐")  # forma cadastrada na regra vem primeiro
        summary = handle_callback(db, callback(pick(ask, "Cartão C6")))
        assert "Forma de pagamento: **Cartão C6 (crédito)**" in summary.text and "fatura" in summary.text
        confirm = summary.keyboard[0][0]["callback_data"]
        done = handle_callback(db, callback(confirm))
        assert "Cartão C6 (crédito)" in done.text
        assert "já foi registrado" in handle_callback(db, callback(confirm)).text
        transaction = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == luz.id))
        card = db.scalar(select(Card).where(Card.name == "C6"))
        assert transaction.card_id == card.id and transaction.account_id == card.account_id
        assert transaction.payment_method == "Crédito" and transaction.status == TransactionStatus.pending
        occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.recurrence_rule_id == luz.id))
        assert (occurrence.year, occurrence.month, occurrence.status) == (2026, 9, "confirmed")
        assert payments(db, luz.id) == 1


def test_card_payment_by_natural_text_resolves_method(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_pagamento_conta_fixa", "arguments": {"bill": "luz"}})
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        reply = handle_message(db, message("paguei luz no c6 credito"))
        assert isinstance(reply, BotReply) and reply.keyboard  # botoes Confirmar/Cancelar
        assert "Pagou como" not in reply.text and "Forma de pagamento: **Cartão C6 (crédito)**" in reply.text
        assert "Pagamento registrado" in run_financial_agent(db, user, "sim", reference_date=TODAY)
        transaction = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == luz.id))
        assert transaction.card_id is not None and transaction.status == TransactionStatus.pending


def test_ambiguous_method_name_asks_again(monkeypatch):
    llm(monkeypatch)
    with SessionLocal() as db:
        user, luz, *_ = seed(db)
        handle_callback(db, callback(f"fb:p:{luz.id}:202609"))
        again = handle_message(db, message("c6"))  # conta C6 e cartao C6
        assert "Pagou como" in again.text and payments(db, luz.id) == 0
        debit = handle_message(db, message("c6 débito"))
        assert "Conta C6 (débito)" in debit.text
