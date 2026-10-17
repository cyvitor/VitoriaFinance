from datetime import date, datetime, timedelta
from decimal import Decimal
import json

from sqlalchemy import select

from app.automation import runner
from app.automation.telegram_bot import handle_callback, handle_message
from app.database import SessionLocal
from app.models import (
    Category, FuelFillup, Person, TelegramConversationMessage, TelegramExpenseDraft, TelegramPendingAction,
    Transaction, TransactionStatus, TransactionType, Vehicle,
)
from app.services.fixed_bills import BotReply
from app.services.fuel_followup import parse_fuel_details
from app.services import fixed_bills
from app.services.telegram_ai import AIUnavailableError
from test_fixed_bills_telegram import CHAT, callback, message, seed, TODAY


def llm(monkeypatch, *decisions):
    """Fila so para a escolha de ferramenta; a redacao final cai no texto deterministico."""
    queue = list(decisions)

    def complete(*args, **kwargs):
        selecting = any("Escolha no maximo uma ferramenta" in str(item.get("content"))
                        for item in (args[2] if len(args) > 2 else []))
        if selecting and queue:
            return json.dumps(queue.pop(0))
        raise AIUnavailableError("ocupado")
    monkeypatch.setattr("app.services.telegram_agent._complete", complete)
    monkeypatch.setattr(fixed_bills, "today_sp", lambda: TODAY)

GAS = {"action": "tool", "tool": "preparar_despesa",
       "arguments": {"description": "Gasolina", "amount": 100, "card": "C6", "payment_method": "credito"}}


def seed_fuel(db):
    user, luz, *_ = seed(db)
    luz.is_active = False
    vitor = db.scalar(select(Person).where(Person.name == "Vitor"))
    category = Category(workspace_id=vitor.workspace_id, parent_name="Transporte", name="Combustível",
                        kind=TransactionType.expense)
    vehicle = Vehicle(workspace_id=vitor.workspace_id, name="Corsa", initial_odometer_km=Decimal("178000"))
    db.add_all([category, vehicle]); db.commit()
    return user, vitor, category, vehicle


def fuel_tx(db, user, vitor, category, days_ago=0, amount="100.00"):
    when = date.today() - timedelta(days=days_ago)
    item = Transaction(workspace_id=vitor.workspace_id, transaction_type=TransactionType.expense,
                       description="Gasolina", amount=Decimal(amount), transaction_date=when,
                       competence_year=when.year, competence_month=when.month, status=TransactionStatus.paid,
                       person_id=vitor.id, category_id=category.id, created_by_id=user.id, source="telegram")
    db.add(item); db.commit()
    return item


def datas(reply):
    return [b["callback_data"] for row in (reply.keyboard or []) for b in row]


def pick(reply, prefix):
    return next(item for item in datas(reply) if item.startswith(prefix))


def with_category(decision, category="Combustível"):
    return {**decision, "arguments": {**decision["arguments"], "category": category}}


def test_parse_fuel_details_from_free_text():
    assert parse_fuel_details("100 gasolina, 14,51 litros") == {"liters": "14.51"}
    found = parse_fuel_details("178.640 km, 6,89 o litro, completei")
    assert found == {"odometer_km": "178640", "price_per_liter": "6.89", "full_tank": True}
    assert parse_fuel_details("nao completei, 20 l")["full_tank"] is False


def test_offer_buttons_yes_no_are_local_idempotent_and_saved_in_history(monkeypatch):
    llm(monkeypatch, with_category(GAS))
    with SessionLocal() as db:
        user, vitor, category, vehicle = seed_fuel(db)
        summary = handle_message(db, message("100 gasolina"))
        confirmed = handle_callback(db, callback(pick(summary, "ok:c:d:")))
        assert confirmed.text.startswith("✅ Confirmado por você.")
        assert "Deseja registrar também os dados deste abastecimento" in confirmed.text
        yes, no = pick(confirmed, "fu:y:"), pick(confirmed, "fu:n:")
        asked = handle_callback(db, callback(yes))
        assert "Falta informar" in asked.text and "quilometragem" in asked.text and "Corsa" in asked.text
        assert "já foi respondida" in handle_callback(db, callback(yes)).text
        stopped = handle_callback(db, callback(no))
        assert "permanece registrada" in stopped.text
        assert "já foi respondida" in handle_callback(db, callback(no)).text
        assert db.scalar(select(FuelFillup)) is None
        history = [m.content for m in db.scalars(select(TelegramConversationMessage).order_by(TelegramConversationMessage.id))]
        assert "[botão] Confirmar" in history and "[botão] Sim (abastecimento)" in history
        assert any("Deseja registrar também" in item for item in history)


def test_text_sim_after_button_confirmation_is_handled_without_llm(monkeypatch):
    llm(monkeypatch, with_category(GAS))  # depois disso o modelo fica "ocupado"
    with SessionLocal() as db:
        seed_fuel(db)
        summary = handle_message(db, message("100 gasolina"))
        handle_callback(db, callback(pick(summary, "ok:c:d:")))
        reply = handle_message(db, message("Sim"))
        text = reply.text if isinstance(reply, BotReply) else reply
        assert "Falta informar" in text and "quilometragem" in text
        cancelled = handle_message(db, message("não"))
        assert "permanece registrada" in (cancelled.text if isinstance(cancelled, BotReply) else cancelled)


def test_register_fillup_for_last_fuel_expense_without_pending_action(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_abastecimento", "arguments": {}},
        {"action": "tool", "tool": "preparar_abastecimento",
         "arguments": {"odometer_km": 178640, "liters": 14.51, "full_tank": True}})
    with SessionLocal() as db:
        user, vitor, category, vehicle = seed_fuel(db)
        old = fuel_tx(db, user, vitor, category, days_ago=10)
        recent = fuel_tx(db, user, vitor, category)
        first = handle_message(db, message("Registre o abastecimento do veículo"))
        text = first.text if isinstance(first, BotReply) else first
        assert "Gasolina R$ 100,00 em" in text and "Falta informar" in text
        summary = handle_message(db, message("178640 km, 14,51 litros, completei"))
        action = db.scalar(select(TelegramPendingAction).where(TelegramPendingAction.status == "awaiting_confirmation"))
        assert datas(summary) == [f"ok:c:a:{action.id}", f"ok:x:a:{action.id}"]
        done = handle_callback(db, callback(f"ok:c:a:{action.id}"))
        assert "registrado" in done.text
        fillup = db.scalar(select(FuelFillup))
        assert fillup.transaction_id == recent.id and fillup.liters == Decimal("14.510") and fillup.is_full_tank
        assert db.scalar(select(FuelFillup).where(FuelFillup.transaction_id == old.id)) is None


def test_several_fuel_expenses_ask_which_one_with_buttons(monkeypatch):
    llm(monkeypatch, {"action": "tool", "tool": "preparar_abastecimento", "arguments": {}})
    with SessionLocal() as db:
        user, vitor, category, vehicle = seed_fuel(db)
        first = fuel_tx(db, user, vitor, category, days_ago=1, amount="80.00")
        second = fuel_tx(db, user, vitor, category)
        ask = handle_message(db, message("o gasto já foi registrado, basta colocar o abastecimento"))
        assert "Qual despesa de combustível" in ask.text
        assert datas(ask) == [f"fl:{second.id}:0", f"fl:{first.id}:0"]
        chosen = handle_callback(db, callback(f"fl:{first.id}:0"))
        assert "Gasolina R$ 80,00" in chosen.text and "fu:n:" in "".join(datas(chosen))
        again = handle_callback(db, callback(f"fl:{first.id}:0"))
        assert "Gasolina R$ 80,00" in again.text
        actions = db.scalars(select(TelegramPendingAction).where(TelegramPendingAction.status == "collecting")).all()
        assert len(actions) == 1 and json.loads(actions[0].payload)["transaction_id"] == first.id


def test_liters_in_first_message_skip_offer_and_ask_only_missing(monkeypatch):
    llm(monkeypatch, with_category(GAS),
        {"action": "tool", "tool": "preparar_abastecimento", "arguments": {"odometer_km": 178640, "full_tank": True}})
    with SessionLocal() as db:
        seed_fuel(db)
        summary = handle_message(db, message("100 gasolina, 14,51 litros"))
        assert "Abastecimento: 14,51 L" in summary.text
        draft = db.scalar(select(TelegramExpenseDraft))
        assert json.loads(draft.fuel_details) == {"liters": "14.51"}
        confirmed = handle_callback(db, callback(pick(summary, "ok:c:d:")))
        assert "Já anotei: 14,51 L" in confirmed.text and "quilometragem" in confirmed.text
        assert "se o tanque foi completado" in confirmed.text and "Deseja registrar" not in confirmed.text
        assert datas(confirmed)[0].startswith("fu:n:")
        final = handle_message(db, message("178640 km, completei"))
        handle_callback(db, callback(pick(final, "ok:c:a:")))
        fillup = db.scalar(select(FuelFillup))
        assert fillup.liters == Decimal("14.510") and fillup.price_per_liter == Decimal("6.892")


def test_same_day_fuel_duplicate_offers_to_link_instead_of_new_expense(monkeypatch):
    llm(monkeypatch, with_category(GAS))
    with SessionLocal() as db:
        user, vitor, category, vehicle = seed_fuel(db)
        existing = fuel_tx(db, user, vitor, category)
        reply = handle_message(db, message("100 reais, 14,51 litros"))
        draft = db.scalar(select(TelegramExpenseDraft))
        assert "Possível duplicata" in reply.text and "Gasolina R$ 100,00" in reply.text
        assert datas(reply) == [f"fl:{existing.id}:{draft.id}", f"ok:c:d:{draft.id}", f"ok:x:d:{draft.id}"]
        linked = handle_callback(db, callback(f"fl:{existing.id}:{draft.id}"))
        assert "não criei outra despesa" in linked.text and "Já anotei: 14,51 L" in linked.text
        db.refresh(draft)
        assert draft.status == "cancelled"
        assert len(db.scalars(select(Transaction).where(Transaction.description == "Gasolina")).all()) == 1


class FakeClient:
    def __init__(self):
        self.cleared, self.sent = [], []

    def answer_callback_query(self, *args):
        pass

    def clear_keyboard(self, chat_id, message_id):
        self.cleared.append(message_id)

    def send_reply(self, chat_id, reply):
        self.sent.append(reply)


def test_keyboard_removed_after_decision_click_but_kept_on_lists(monkeypatch):
    llm(monkeypatch, with_category(GAS))
    with SessionLocal() as db:
        seed_fuel(db)
        summary = handle_message(db, message("100 gasolina"))
        data = pick(summary, "ok:x:d:")
    client = FakeClient()
    event = callback(data); event["message"]["message_id"] = 42
    runner.process_callback(client, {"update_id": 9001}, event)
    assert client.cleared == [42] and client.sent[0].text.startswith("❌ Cancelado por você.")
    listing = callback("fb:p:999:202609"); listing["message"]["message_id"] = 43
    runner.process_callback(client, {"update_id": 9002}, listing)
    assert client.cleared == [42]