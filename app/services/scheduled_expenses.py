"""Confirma um gasto programado (conta fixa) pelo mesmo resumo das despesas avulsas.

O texto e montado no codigo. A ocorrencia do mes so e marcada como paga depois do Confirmar,
e uma competencia ja confirmada nao gera outro lancamento.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import RecurrenceOccurrence, RecurrenceRule, TelegramPendingAction, User, UserMemory
from app.services import fixed_bills
from app.services.bot_confirmations import confirm_keyboard
from app.services.fixed_bills import (
    BotReply, _context, _label, _people, _resolve_rule, match_source, source_key, source_label,
)
from app.services.recurrence_payments import RecurrenceAlreadyResolved, apply_recurrence_confirmation, pending_fixed_bills
from app.services.telegram_expenses import render_expense_confirmation

logger = get_bot_logger()
ACTION_TYPE = "scheduled_expense"
EXPIRATION = timedelta(minutes=30)
COMMAND = re.compile(r"^(?:confirma|confirme|confirmar)\s+(?:o\s+)?gasto\s+programado\s+(.+)$")


def _norm(text: str) -> str:
    return fixed_bills._norm(text)


def _preferred_summary(db: Session, user_id: int) -> str | None:
    memories = db.scalars(select(UserMemory).where(
        UserMemory.user_id == user_id, UserMemory.is_active.is_(True),
        UserMemory.memory_type.in_(("preferred_card", "preferred_account", "user_preference")),
    ).order_by(UserMemory.confidence.desc(), UserMemory.id.desc())).all()
    for memory in memories:
        blob = _norm(f"{memory.summary} {memory.value_json}")
        if memory.memory_type == "preferred_card" or any(word in blob for word in ("cartao", "credito", "conta", "debito")):
            return memory.summary
    return None


def _active(db: Session, user_id: int) -> TelegramPendingAction | None:
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == ACTION_TYPE,
        TelegramPendingAction.status.in_(("collecting", "awaiting_confirmation")),
    ).order_by(TelegramPendingAction.id.desc()))
    if action and action.expires_at < datetime.utcnow():
        action.status = "expired"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return None
    return action


def _save(db: Session, user_id: int, payload: dict, status: str) -> TelegramPendingAction:
    previous = _active(db, user_id)
    if previous:
        previous.status = "cancelled"
        previous.resolved_at = datetime.utcnow()
    action = TelegramPendingAction(
        user_id=user_id, action_type=ACTION_TYPE, status=status,
        payload=json.dumps(payload, ensure_ascii=False),
        expires_at=datetime.utcnow() + EXPIRATION,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _open_bill(db: Session, context, rule: RecurrenceRule, today: date):
    bills = pending_fixed_bills(
        db, workspace_ids=context.writable_workspace_ids, person_ids=context.writable_person_ids,
        today=today, rule_ids=[rule.id],
    )
    return next((item for item in bills if (item.year, item.month) == (today.year, today.month)), None) or (
        bills[0] if bills else None)


def _already_paid(db: Session, rule: RecurrenceRule, today: date) -> bool:
    occurrence = db.scalar(select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id == rule.id,
        RecurrenceOccurrence.year == today.year, RecurrenceOccurrence.month == today.month,
        RecurrenceOccurrence.status.in_(("confirmed", "skipped")),
    ))
    return occurrence is not None


def _confirmation(db: Session, rule: RecurrenceRule, people, source, today: date, amount) -> str:
    kind, obj = source
    category = None
    if rule.category:
        category = (f"{rule.category.parent_name} > {rule.category.name}"
                    if rule.category.parent_name else rule.category.name)
    person = people.get(rule.person_id)
    return render_expense_confirmation(
        description=rule.description or "", amount=amount, when=today,
        area=person.name if person else "", category=category,
        payment="Crédito" if kind == "card" else "Débito",
        card=obj.name if kind == "card" else None,
        ask_category=category is None,
    )


def _ask_method(db: Session, user: User, rule, bill, people, today: date) -> tuple[str, list | None]:
    sources = fixed_bills._sources(db, rule)
    if not sources:
        return (f"Não há conta nem cartão ativo para **{_label(rule, people)}**.", None)
    preferred = _preferred_summary(db, user.id)
    suggested = match_source(sources, preferred) if preferred else None
    payload = {
        "phase": "method", "rule_id": rule.id, "year": bill.year, "month": bill.month,
        "amount": str(bill.amount), "paid_on": today.isoformat(),
        "candidates": [rule.id],
    }
    action = _save(db, user.id, payload, "collecting")
    hint = f"\n\nVocê costuma usar: {preferred}. Mesmo assim, me diga a forma deste pagamento." if preferred else ""
    options = "\n".join(f"- {source_label(*item)}" for item in sources)
    message = (f"Encontrei o gasto programado **{_label(rule, people)}** "
               f"({fixed_bills.format_brl(bill.amount)}).\n\n"
               f"Pagou como? Pode ser crédito ou débito, com o cartão ou a conta.\n\n{options}{hint}")
    return message, fixed_bills.method_keyboard(action.id, sources, source_key(*suggested) if suggested else None)


def _prepare(db: Session, user: User, rule: RecurrenceRule, today: date, payment_text: str | None):
    context = _context(db, user.id)
    people = _people(db, context) if context else {}
    if _already_paid(db, rule, today):
        return (f"O gasto programado **{_label(rule, people)}** de {today.month:02d}/{today.year} "
                "já foi confirmado. Nada foi lançado de novo.", None)
    bill = _open_bill(db, context, rule, today) if context else None
    if not bill:
        return (f"Não há competência em aberto para **{_label(rule, people)}**.", None)
    sources = fixed_bills._sources(db, rule)
    selected = match_source(sources, payment_text) if payment_text else None
    if not selected:
        return _ask_method(db, user, rule, bill, people, today)
    payload = {
        "phase": None, "rule_id": rule.id, "year": bill.year, "month": bill.month,
        "amount": str(bill.amount), "paid_on": today.isoformat(), "source": source_key(*selected),
    }
    action = _save(db, user.id, payload, "awaiting_confirmation")
    return _confirmation(db, rule, people, selected, today, bill.amount), confirm_keyboard("a", action.id)


def _start(db: Session, user: User, remainder: str, today: date):
    context = _context(db, user.id)
    if not context:
        return ("Defina uma área financeira padrão com permissão de escrita.", None)
    rule, error = _resolve_rule(db, context, bill=remainder)
    if error and error.get("status") == "ambiguous":
        matches = fixed_bills.find_rules(db, context, remainder)
        payload = {"phase": "choose", "query": remainder, "candidates": [item.id for item in matches]}
        _save(db, user.id, payload, "collecting")
        return error["message"], None
    if error or not rule:
        return ((error or {}).get("message") or "Não encontrei esse gasto programado.", None)
    payment = remainder
    return _prepare(db, user, rule, today, payment)


def local_reply(db: Session, user: User, text: str, today: date | None = None):
    """Resposta local (Telegram e app). None deixa o restante do agente seguir."""
    today = today or fixed_bills.today_sp()
    normalized = " ".join(_norm(text).strip(" .!?").split())
    action = _active(db, user.id)
    match = COMMAND.match(normalized)
    if match and (not action or action.status != "collecting"):
        return _start(db, user, match.group(1), today)
    if not action or action.status != "collecting":
        return None
    payload = json.loads(action.payload)
    if normalized in {"nao", "n", "cancelar", "cancela"}:
        result = cancel(db, user.id, action.id)
        return result["message"], None
    if payload.get("phase") == "choose":
        context = _context(db, user.id)
        chosen = None
        for rule_id in payload.get("candidates") or []:
            rule = db.get(RecurrenceRule, int(rule_id))
            if rule and _norm(rule.description or "") in normalized or normalized in _norm(rule.description or ""):
                chosen = rule
                break
        if not chosen and context:
            rule, error = _resolve_rule(db, context, bill=text)
            if rule and rule.id in (payload.get("candidates") or []):
                chosen = rule
            elif error:
                return error["message"], None
        if not chosen:
            return ("Não identifiquei qual gasto programado. Responda com o nome de um deles.", None)
        return _prepare(db, user, chosen, today, text)
    if payload.get("phase") == "method":
        rule = db.get(RecurrenceRule, payload.get("rule_id"))
        if not rule:
            return ("Esse gasto programado não está mais disponível.", None)
        return _prepare(db, user, rule, today, text)
    return None


def confirm(db: Session, user_id: int, action_id: int | None = None) -> dict:
    action = db.get(TelegramPendingAction, action_id) if action_id else _active(db, user_id)
    if not action or action.user_id != user_id or action.action_type != ACTION_TYPE:
        return {"deterministic": True, "message": "Não encontrei gasto programado aguardando confirmação."}
    if action.status == "confirmed":
        return {"deterministic": True, "already_registered": True,
                "message": "Esse gasto programado já foi registrado. Nada foi lançado de novo."}
    if action.status != "awaiting_confirmation":
        return {"deterministic": True, "message": "Esse gasto ainda não está pronto para confirmar."}
    payload = json.loads(action.payload)
    context = _context(db, user_id)
    rule = db.get(RecurrenceRule, payload.get("rule_id")) if context else None
    source = fixed_bills._find_source(fixed_bills._sources(db, rule), payload.get("source")) if rule else None
    if not rule or not source or _already_paid(db, rule, date.fromisoformat(payload["paid_on"])):
        action.status = "cancelled"
        action.resolved_at = datetime.utcnow()
        db.commit()
        label = _label(rule, _people(db, context)) if rule and context else "esse gasto"
        return {"deterministic": True, "already_registered": True,
                "message": f"O gasto programado **{label}** já estava confirmado ou mudou. Nada foi lançado de novo."}
    kind, obj = source
    fields = fixed_bills.payment_fields(kind, obj)
    try:
        transaction, _, _ = apply_recurrence_confirmation(
            db, rule=rule, year=int(payload["year"]), month=int(payload["month"]),
            amount=fixed_bills.parse_amount(str(payload["amount"])) or rule.amount,
            created_by_id=user_id, source="telegram",
            transaction_date=date.fromisoformat(payload["paid_on"]), **fields,
        )
        action.status = "confirmed"
        action.resolved_at = datetime.utcnow()
        db.commit()
    except RecurrenceAlreadyResolved:
        db.rollback()
        action = db.get(TelegramPendingAction, action.id)
        action.status = "cancelled"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return {"deterministic": True, "already_registered": True,
                "message": "Essa competência já estava confirmada. Nada foi lançado de novo."}
    logger.info("scheduled_expense_confirmed user_id=%s rule_id=%s transaction_id=%s",
                user_id, rule.id, transaction.id)
    return {"deterministic": True, "registered": True, "transaction_id": transaction.id,
            "message": (f"Gasto programado registrado: **{rule.description}**, "
                        f"{fixed_bills.format_brl(transaction.amount)} em "
                        f"{transaction.transaction_date.strftime('%d/%m/%Y')} — {source_label(kind, obj)}.")}


def cancel(db: Session, user_id: int, action_id: int | None = None) -> dict:
    action = db.get(TelegramPendingAction, action_id) if action_id else _active(db, user_id)
    if not action or action.user_id != user_id or action.action_type != ACTION_TYPE:
        return {"deterministic": True, "message": "Não há gasto programado pendente para cancelar."}
    action.status = "cancelled"
    action.resolved_at = datetime.utcnow()
    db.commit()
    return {"deterministic": True, "cancelled": True, "message": "Cancelado. Nada foi registrado."}


def handle_callback(db: Session, user: User, data: str):
    """O teclado de forma de pagamento reutiliza fb:s; a confirmacao usa os botoes ok:."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != "fb" or parts[1] != "s" or not parts[2].isdigit():
        return None
    action = db.get(TelegramPendingAction, int(parts[2]))
    if not action or action.user_id != user.id or action.action_type != ACTION_TYPE:
        return None
    code = parts[3]
    key = ("card:" if code.startswith("c") else "account:") + code[1:]
    payload = json.loads(action.payload)
    rule = db.get(RecurrenceRule, payload.get("rule_id"))
    source = fixed_bills._find_source(fixed_bills._sources(db, rule), key) if rule else None
    if not rule or not source:
        return BotReply("Não encontrei essa forma de pagamento.")
    context = _context(db, user.id)
    people = _people(db, context) if context else {}
    today = date.fromisoformat(payload["paid_on"])
    created = _save(db, user.id, {
        "phase": None, "rule_id": rule.id, "year": payload["year"], "month": payload["month"],
        "amount": payload["amount"], "paid_on": payload["paid_on"], "source": key,
    }, "awaiting_confirmation")
    text = _confirmation(db, rule, people, source, today, payload["amount"])
    return BotReply(text, confirm_keyboard("a", created.id))
