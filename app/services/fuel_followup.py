"""Complemento de abastecimento: botoes Sim/Nao, vinculo com despesa de combustivel ja registrada,
dados de litros/km na primeira mensagem e aviso de possivel duplicata. Sem LLM nos callbacks."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import (
    Category, FuelFillup, TelegramExpenseDraft, TelegramPendingAction, Transaction, TransactionType, User, Vehicle,
)
from app.services.fixed_bills import BotReply, _norm, format_brl

logger = get_bot_logger()

FUEL_TERMS = ("combust", "gasolina", "etanol", "alcool", "diesel", "gnv")
FUEL_TTL = timedelta(hours=6)
LINK_DAYS = 7
DETAIL_FIELDS = ("liters", "odometer_km", "price_per_liter", "full_tank")


def is_fuel(description: str | None, category: Category | None = None) -> bool:
    label = _norm(" ".join([description or "", (category.parent_name or "") if category else "",
                            (category.name or "") if category else ""]))
    return any(term in label for term in FUEL_TERMS)


def _number(value: str) -> Decimal | None:
    raw = value.replace(".", "").replace(",", ".") if "," in value else value
    try:
        result = Decimal(raw)
    except InvalidOperation:
        return None
    return result if result > 0 else None


def parse_fuel_details(text: str | None) -> dict:
    """'100 gasolina, 14,51 litros, 178640 km, completei' -> litros, km, preco, tanque cheio."""
    normalized = _norm(text)
    if not normalized:
        return {}
    found: dict = {}
    number = r"(\d+(?:[.,]\d+)*)"
    liters = re.search(number + r"\s*(?:l|lt|lts|litros?)\b", normalized)
    if liters and _number(liters.group(1)):
        found["liters"] = str(_number(liters.group(1)))
    km = re.search(number + r"\s*(?:km|quilometros?)\b", normalized)
    if km:
        value = _number(km.group(1).replace(".", "").replace(",", ""))
        if value:
            found["odometer_km"] = str(value)
    price = (re.search(r"(?:preco|valor)\s*(?:por\s*|do\s*|o\s*)?litro\s*(?:de|:|=|a)?\s*(?:r\$\s*)?" + number, normalized)
             or re.search(r"(?:r\$\s*)?" + number + r"\s*(?:o|por|/)\s*litro\b", normalized))
    if price and _number(price.group(1)):
        found["price_per_liter"] = str(_number(price.group(1)))
    if re.search(r"\b(nao (?:completei|enchi|encheu)|parcial)\b", normalized):
        found["full_tank"] = False
    elif re.search(r"\b(completei|enchi|encheu|tanque cheio|ate completar)\b", normalized):
        found["full_tank"] = True
    return found


def merge_details(args: dict, hint: str | None) -> dict:
    details = parse_fuel_details(hint)
    for field in DETAIL_FIELDS:
        value = args.get(field)
        if value in (None, ""):
            continue
        if field == "full_tank":
            details[field] = bool(value)
        else:
            parsed = _number(str(value))
            if parsed:
                details[field] = str(parsed)
    return details


def details_text(details: dict) -> str:
    parts = []
    if details.get("liters"):
        parts.append(f"{str(details['liters']).replace('.', ',')} L")
    if details.get("odometer_km"):
        parts.append(f"{details['odometer_km']} km")
    if details.get("price_per_liter"):
        parts.append(f"{format_brl(details['price_per_liter'])}/L")
    if "full_tank" in details:
        parts.append("tanque cheio" if details["full_tank"] else "parcial")
    return ", ".join(parts)


def transaction_label(transaction: Transaction) -> str:
    return (f"{transaction.description} {format_brl(transaction.amount)} "
            f"em {transaction.transaction_date.strftime('%d/%m')}")


def _active_vehicles(db: Session, workspace_id: int) -> list[Vehicle]:
    return db.scalars(select(Vehicle).where(Vehicle.workspace_id == workspace_id, Vehicle.is_active.is_(True))
                      .order_by(Vehicle.name)).all()


def link_candidates(db: Session, context, today: date | None = None) -> list[Transaction]:
    """Despesas de combustivel dos ultimos 7 dias, nas areas com escrita, ainda sem abastecimento."""
    today = today or date.today()
    if not context.writable_workspace_ids or not context.writable_person_ids:
        return []
    rows = db.scalars(select(Transaction).outerjoin(FuelFillup, FuelFillup.transaction_id == Transaction.id).where(
        FuelFillup.id.is_(None), Transaction.transaction_type == TransactionType.expense,
        Transaction.workspace_id.in_(context.writable_workspace_ids),
        Transaction.person_id.in_(context.writable_person_ids),
        Transaction.transaction_date >= today - timedelta(days=LINK_DAYS),
    ).order_by(Transaction.transaction_date.desc(), Transaction.id.desc())).all()
    result = []
    for item in rows:
        category = db.get(Category, item.category_id) if item.category_id else None
        if is_fuel(item.description, category) and _active_vehicles(db, item.workspace_id):
            result.append(item)
    return result


def start_fuel_action(db: Session, user_id: int, transaction: Transaction, details: dict | None = None,
                      phase: str | None = None) -> TelegramPendingAction:
    for item in db.scalars(select(TelegramPendingAction).where(
            TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == "fuel_fillup",
            TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]))).all():
        item.status = "cancelled"; item.resolved_at = datetime.utcnow()
    vehicles = _active_vehicles(db, transaction.workspace_id)
    payload = {"phase": phase or ("details" if len(vehicles) == 1 else "vehicle"),
               "transaction_id": transaction.id, **(details or {})}
    if len(vehicles) == 1 and payload["phase"] != "offer":
        payload["vehicle_id"] = vehicles[0].id
    action = TelegramPendingAction(user_id=user_id, action_type="fuel_fillup", status="collecting",
                                   payload=json.dumps(payload, ensure_ascii=False),
                                   expires_at=datetime.utcnow() + FUEL_TTL)
    db.add(action); db.flush()
    return action


def missing_details(payload: dict) -> list[str]:
    missing = []
    if not payload.get("vehicle_id"):
        missing.append("veículo")
    if not payload.get("odometer_km"):
        missing.append("quilometragem")
    if not payload.get("liters") and not payload.get("price_per_liter"):
        missing.append("litros ou preço por litro")
    if "full_tank" not in payload:
        missing.append("se encheu o tanque")
    return missing


def details_question(db: Session, action: TelegramPendingAction, transaction: Transaction) -> str:
    payload = json.loads(action.payload)
    vehicle = db.get(Vehicle, payload.get("vehicle_id")) if payload.get("vehicle_id") else None
    known = details_text(payload)
    lines = [f"Abastecimento da despesa **{transaction_label(transaction)}**"
             + (f" no **{vehicle.name}**." if vehicle else ".")]
    if known:
        lines.append(f"Já anotei: {known}.")
    missing = missing_details(payload)
    if not vehicle:
        options = ", ".join(item.name for item in _active_vehicles(db, transaction.workspace_id))
        lines.append(f"Qual veículo foi abastecido? {options}")
    elif missing:
        lines.append("Falta informar: **" + ", ".join(missing) + "**. Exemplo: `178640 km, completei`.")
    return "\n".join(lines)


def offer_keyboard(action_id: int) -> list[list[dict]]:
    return [[{"text": "✅ Sim", "callback_data": f"fu:y:{action_id}"},
             {"text": "❌ Não", "callback_data": f"fu:n:{action_id}"}]]


def skip_keyboard(action_id: int) -> list[list[dict]]:
    return [[{"text": "❌ Não registrar abastecimento", "callback_data": f"fu:n:{action_id}"}]]


def latest_fuel_action(db: Session, user_id: int) -> TelegramPendingAction | None:
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == "fuel_fillup",
        TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]),
    ).order_by(TelegramPendingAction.id.desc()))
    if action and action.expires_at < datetime.utcnow():
        action.status = "expired"; action.resolved_at = datetime.utcnow(); db.commit()
        return None
    return action


def duplicate_of(db: Session, context, draft: TelegramExpenseDraft) -> Transaction | None:
    """Despesa de combustivel ja registrada no mesmo dia e valor (possivel duplicata)."""
    candidates = db.scalars(select(Transaction).where(
        Transaction.transaction_type == TransactionType.expense,
        Transaction.workspace_id.in_(context.writable_workspace_ids),
        Transaction.person_id.in_(context.writable_person_ids),
        Transaction.transaction_date == draft.transaction_date, Transaction.amount == draft.amount,
    ).order_by(Transaction.id.desc())).all()
    for item in candidates:
        category = db.get(Category, item.category_id) if item.category_id else None
        if is_fuel(item.description, category):
            return item
    return None


def duplicate_keyboard(db: Session, transaction: Transaction, draft_id: int) -> list[list[dict]]:
    rows = []
    has_fillup = db.scalar(select(FuelFillup.id).where(FuelFillup.transaction_id == transaction.id))
    if not has_fillup:
        rows.append([{"text": "⛽ É a mesma: registrar abastecimento", "callback_data": f"fl:{transaction.id}:{draft_id}"}])
    rows.append([{"text": "🆕 Nova despesa", "callback_data": f"ok:c:d:{draft_id}"},
                 {"text": "❌ Cancelar", "callback_data": f"ok:x:d:{draft_id}"}])
    return rows


def _context(db: Session, user: User):
    from app.services.access_context import build_user_access_context, scope_to_principal_area
    return scope_to_principal_area(build_user_access_context(db, user))


def handle_callback(db: Session, user: User, data: str) -> BotReply | None:
    parts = (data or "").split(":")
    try:
        if parts[0] == "fu" and len(parts) == 3 and parts[1] in ("y", "n"):
            return _answer_offer(db, user, parts[1] == "y", int(parts[2]))
        if parts[0] == "fl" and len(parts) == 3:
            return _link_expense(db, user, int(parts[1]), int(parts[2]))
    except ValueError:
        return BotReply("Não reconheci esse botão. Envie a mensagem de novo.")
    return None


def _answer_offer(db: Session, user: User, yes: bool, action_id: int) -> BotReply:
    action = db.get(TelegramPendingAction, action_id)
    if not action or action.user_id != user.id or action.action_type != "fuel_fillup":
        return BotReply("Esse pedido não pertence a você ou não existe mais.")
    if action.status not in ("collecting", "awaiting_confirmation"):
        done = {"confirmed": "Esse abastecimento já foi registrado.", "cancelled": "Essa pergunta já foi respondida.",
                "expired": "Essa pergunta expirou. Peça \"registre o abastecimento\" para retomar."}
        return BotReply(done.get(action.status, "Essa pergunta já foi respondida."))
    if action.expires_at < datetime.utcnow():
        action.status = "expired"; action.resolved_at = datetime.utcnow(); db.commit()
        return BotReply("Essa pergunta expirou. Peça \"registre o abastecimento\" para retomar.")
    payload = json.loads(action.payload)
    if not yes:
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        logger.info("fuel_offer_answered user_id=%s action_id=%s answer=no", user.id, action.id)
        return BotReply("Tudo certo. A despesa permanece registrada sem os dados do abastecimento.")
    if payload.get("phase") != "offer":
        return BotReply("Essa pergunta já foi respondida. Continue pela última mensagem.")
    latest = latest_fuel_action(db, user.id)
    if not latest or latest.id != action.id:
        return BotReply("Essa pergunta não está mais ativa.")
    from app.services.telegram_agent import _fuel_conversation_reply
    reply = _fuel_conversation_reply(db, user, "sim") or ""
    db.refresh(action)
    transaction = db.get(Transaction, payload.get("transaction_id"))
    if action.status == "collecting" and transaction and json.loads(action.payload).get("phase") == "details":
        reply = details_question(db, action, transaction)
    logger.info("fuel_offer_answered user_id=%s action_id=%s answer=yes", user.id, action.id)
    return BotReply(reply, skip_keyboard(action.id) if action.status == "collecting" else None)


def _link_expense(db: Session, user: User, transaction_id: int, draft_id: int) -> BotReply:
    context = _context(db, user)
    candidates = {item.id: item for item in link_candidates(db, context)}
    transaction = candidates.get(transaction_id)
    if not transaction:
        owned = db.get(Transaction, transaction_id)
        if owned and db.scalar(select(FuelFillup.id).where(FuelFillup.transaction_id == transaction_id)):
            return BotReply("Essa despesa já tem abastecimento registrado.")
        return BotReply("Essa despesa não está disponível para vincular abastecimento.")
    current = latest_fuel_action(db, user.id)
    if current and json.loads(current.payload).get("transaction_id") == transaction.id:
        return BotReply(details_question(db, current, transaction), skip_keyboard(current.id))
    details = {}
    if draft_id:
        draft = db.get(TelegramExpenseDraft, draft_id)
        if draft and draft.user_id == user.id and draft.status == "awaiting_confirmation":
            details = json.loads(draft.fuel_details or "{}")
            draft.status = "cancelled"; draft.resolved_at = datetime.utcnow()
    action = start_fuel_action(db, user.id, transaction, details)
    db.commit()
    logger.info("fuel_linked_to_expense user_id=%s transaction_id=%s action_id=%s", user.id, transaction.id, action.id)
    prefix = "Ok, não criei outra despesa. " if draft_id else ""
    return BotReply(prefix + details_question(db, action, transaction), skip_keyboard(action.id))


def local_text_reply(db: Session, user: User, text: str) -> str | None:
    """'sim'/'nao' para a oferta do abastecimento e escolha do veiculo, sem LLM."""
    from app.services.telegram_agent import _fuel_conversation_reply, _simple_reply_kind
    action = latest_fuel_action(db, user.id)
    if not action or action.status != "collecting":
        return None
    payload = json.loads(action.payload)
    kind = _simple_reply_kind(text)
    if payload.get("phase") == "offer" and kind:
        reply = _fuel_conversation_reply(db, user, "sim" if kind == "confirm" else "nao")
        db.refresh(action)
        transaction = db.get(Transaction, payload.get("transaction_id"))
        if kind == "confirm" and action.status == "collecting" and transaction and \
                json.loads(action.payload).get("phase") == "details":
            reply = details_question(db, action, transaction)
        return reply
    if payload.get("phase") in ("details", "vehicle") and kind == "cancel":
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        return "Tudo certo. A despesa permanece registrada sem os dados do abastecimento."
    if payload.get("phase") == "vehicle":
        reply = _fuel_conversation_reply(db, user, text)
        if reply and "identifiquei" not in _norm(reply):
            db.refresh(action)
            transaction = db.get(Transaction, payload.get("transaction_id"))
            return details_question(db, action, transaction) if transaction else reply
    return None