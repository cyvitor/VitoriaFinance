"""Contas fixas pelo Telegram: listar, preparar e confirmar pagamentos, vencimentos e aviso diario.

Todo texto aqui e deterministico (sem LLM), para funcionar mesmo com a DeepInfra indisponivel.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import (
    Account, Person, RecurrenceRule, SystemSetting, TelegramBillReminder, TelegramLink,
    TelegramPendingAction, TransactionType, User,
)
from app.services.access_context import UserAccessContext, build_user_access_context
from app.services.recurrence_payments import (
    PendingBill, RecurrenceAlreadyResolved, apply_recurrence_confirmation, pending_fixed_bills,
)

logger = get_bot_logger()
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
ACTION_TYPE = "fixed_bill_payment"
EXPIRATION = timedelta(minutes=30)
DEFAULT_REMINDER_TIME = "08:00"
MAX_KEYBOARD_BILLS = 10
STOPWORDS = {
    "a", "o", "as", "os", "da", "do", "das", "dos", "de", "e", "em", "no", "na", "nos", "nas", "um", "uma",
    "conta", "contas", "fixa", "fixas", "paguei", "pago", "paga", "pagar", "pagamento", "pagei", "hoje",
    "ontem", "mes", "atual", "valor", "reais", "r", "rs", "foi", "eu", "ja", "que", "com", "pela", "pelo",
    "vence", "vencimento", "dia", "todo", "sempre", "boleto", "fatura", "referente", "competencia",
}
CURRENT_MONTH_TERMS = {"atual", "current", "mes atual", "este mes", "esse mes", "deste mes", "desse mes"}


@dataclass
class BotReply:
    text: str
    keyboard: list[list[dict]] | None = None

    def __contains__(self, item: str) -> bool:  # facilita asserts e logs
        return item in self.text


def today_sp() -> date:
    return datetime.now(SAO_PAULO).date()


def _norm(text: str | None) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", (text or "").casefold())
                   if not unicodedata.combining(char))


def format_brl(value) -> str:
    formatted = f"{Decimal(str(value or 0)):,.2f}"
    return "R$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def parse_amount(text: str) -> Decimal | None:
    match = re.search(r"(?<![\d/])(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\d/])", text or "")
    if not match:
        return None
    raw = match.group(1)
    raw = raw.replace(".", "").replace(",", ".") if "," in raw else (
        raw.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) else raw)
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _context(db: Session, user_id: int) -> UserAccessContext | None:
    user = db.get(User, user_id)
    return build_user_access_context(db, user) if user else None


def _people(db: Session, context: UserAccessContext) -> dict[int, Person]:
    if not context.writable_person_ids:
        return {}
    return {item.id: item for item in db.scalars(select(Person).where(
        Person.id.in_(context.writable_person_ids))).all()}


def _rules(db: Session, context: UserAccessContext) -> list[RecurrenceRule]:
    if not context.writable_person_ids or not context.writable_workspace_ids:
        return []
    return db.scalars(select(RecurrenceRule).where(
        RecurrenceRule.workspace_id.in_(context.writable_workspace_ids),
        RecurrenceRule.person_id.in_(context.writable_person_ids),
        RecurrenceRule.transaction_type == TransactionType.expense,
        RecurrenceRule.is_active.is_(True), RecurrenceRule.description.is_not(None),
        RecurrenceRule.amount.is_not(None),
    ).order_by(RecurrenceRule.description)).all()


def _label(rule: RecurrenceRule, people: dict[int, Person]) -> str:
    person = people.get(rule.person_id)
    return f"{rule.description} ({person.name})" if person else str(rule.description)


def find_rules(db: Session, context: UserAccessContext, bill: str | None,
               area: str | None = None) -> list[RecurrenceRule]:
    people = _people(db, context)
    query = _norm(bill)
    area_text = _norm(area)
    area_ids = set()
    for person_id, person in people.items():
        name = _norm(person.name)
        if area_text and (name == area_text or area_text in name or name in area_text):
            area_ids.add(person_id)
        elif name and re.search(rf"\b{re.escape(name)}\b", query):
            area_ids.add(person_id)
            query = re.sub(rf"\b(?:d[aoe]s?\s+)?{re.escape(name)}\b", " ", query)
    tokens = [item for item in re.findall(r"[a-z0-9]+", query)
              if item not in STOPWORDS and not item.isdigit() and len(item) > 1]
    if not tokens:
        return []
    rules = _rules(db, context)
    if area_ids:
        rules = [rule for rule in rules if rule.person_id in area_ids]

    def score(rule: RecurrenceRule) -> int:
        description = _norm(rule.description)
        words = set(re.findall(r"[a-z0-9]+", description))
        return sum(1 for token in tokens if token in words or (len(token) >= 4 and token in description))

    scored = [(score(rule), rule) for rule in rules]
    best = max((value for value, _ in scored), default=0)
    return [rule for value, rule in scored if value == best] if best else []


def mentioned_rule(db: Session, user_id: int, text: str) -> RecurrenceRule | None:
    """Usado para corrigir 'paguei a luz' quando o modelo escolhe preparar_despesa."""
    context = _context(db, user_id)
    if not context:
        return None
    normalized = _norm(text)
    found = []
    for rule in _rules(db, context):
        words = [item for item in re.findall(r"[a-z0-9]+", _norm(rule.description))
                 if item not in STOPWORDS and not item.isdigit() and len(item) > 2]
        if words and re.search(rf"\b{re.escape(words[0])}\b", normalized):
            found.append(rule)
    return found[0] if found else None


def _ambiguous_reply(rules: list[RecurrenceRule], people: dict[int, Person], bill: str) -> dict:
    options = [_label(rule, people) for rule in rules]
    return {
        "fixed_bill": True, "status": "ambiguous", "options": options,
        "message": (f"Encontrei mais de uma conta fixa para **{bill}**:\n\n"
                    + "\n".join(f"- {item}" for item in options)
                    + "\n\nQual delas? Pode responder, por exemplo, \"" + options[-1] + "\"."),
    }


def _not_found_reply(db: Session, context: UserAccessContext, bill: str | None) -> dict:
    people = _people(db, context)
    names = [_label(rule, people) for rule in _rules(db, context)]
    listing = ("\n\nContas fixas cadastradas:\n" + "\n".join(f"- {item}" for item in names)) if names else ""
    return {"fixed_bill": True, "status": "not_found",
            "message": f"Não encontrei a conta fixa **{bill or ''}**.{listing}"}


def _resolve_rule(db: Session, context: UserAccessContext, *, rule_id=None, bill=None, area=None):
    people = _people(db, context)
    if rule_id:
        rule = next((item for item in _rules(db, context) if item.id == int(rule_id)), None)
        return rule, None if rule else {"fixed_bill": True, "status": "not_found",
                                        "message": "Essa conta fixa não está mais disponível para você."}
    matches = find_rules(db, context, bill, area)
    if not matches:
        return None, _not_found_reply(db, context, bill)
    if len(matches) > 1:
        return None, _ambiguous_reply(matches, people, bill or "")
    return matches[0], None


def _accounts(db: Session, rule: RecurrenceRule) -> list[Account]:
    return db.scalars(select(Account).where(
        Account.workspace_id == rule.workspace_id, Account.person_id == rule.person_id,
        Account.is_active.is_(True),
    ).order_by(Account.name)).all()


def _resolve_account(db: Session, rule: RecurrenceRule, name: str | None) -> tuple[Account | None, list[Account]]:
    accounts = _accounts(db, rule)
    if name:
        wanted = _norm(name)
        exact = [item for item in accounts if _norm(item.name) == wanted]
        partial = exact or [item for item in accounts if wanted in _norm(item.name) or _norm(item.name) in wanted]
        if len(partial) == 1:
            return partial[0], accounts
        return None, accounts
    preferred = next((item for item in accounts if item.id == rule.account_id), None)
    if preferred:
        return preferred, accounts
    if len(accounts) == 1:
        return accounts[0], accounts
    return None, accounts


def _parse_competence(value, today: date) -> tuple[int, int] | None:
    if value in (None, ""):
        return None
    if isinstance(value, (tuple, list)):
        return int(value[0]), int(value[1])
    text = _norm(str(value)).strip()
    if text in CURRENT_MONTH_TERMS:
        return today.year, today.month
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", text) or None
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"(\d{1,2})/(\d{4})", text)
    if match:
        return int(match.group(2)), int(match.group(1))
    return None


def _parse_date(value, today: date) -> date:
    if not value:
        return today
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return today


def _open_bills(db: Session, context: UserAccessContext, rule: RecurrenceRule, today: date,
                through: tuple[int, int] | None = None) -> list[PendingBill]:
    limit = max(through or (today.year, today.month), (today.year, today.month))
    return pending_fixed_bills(db, workspace_ids=context.writable_workspace_ids,
                               person_ids=context.writable_person_ids, today=today,
                               through=limit, rule_ids=[rule.id])


def _confirmation_text(rule: RecurrenceRule, people, payload: dict, account: Account | None,
                       other_open: list[PendingBill]) -> str:
    amount, expected = Decimal(payload["amount"]), Decimal(payload["expected"])
    paid_on = date.fromisoformat(payload["paid_on"])
    lines = [
        "Confirme o pagamento da conta fixa:", "",
        f"Conta: **{_label(rule, people)}**",
        f"Competência: **{payload['month']:02d}/{payload['year']}**",
        f"Valor previsto: {format_brl(expected)}",
        f"Valor pago: **{format_brl(amount)}**" + (" (diferente do previsto)" if amount != expected else ""),
        f"Data do pagamento: {paid_on.strftime('%d/%m/%Y')}",
        f"Pago pela conta: {account.name if account else 'não informada'}",
    ]
    others = [item.competence for item in other_open
              if (item.year, item.month) != (payload["year"], payload["month"])]
    if others:
        lines.extend(["", "Outras competências em aberto: " + ", ".join(others)
                      + ". Se o pagamento for de outro mês, me diga (ex.: \"é do mês atual\")."])
    lines.extend(["", "Posso confirmar? Responda *sim* para registrar ou *não* para cancelar."])
    return "\n".join(lines)


def _active_actions(db: Session, user_id: int):
    return db.scalars(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == ACTION_TYPE,
        TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]),
    ).order_by(TelegramPendingAction.id.desc())).all()


def active_collecting_action(db: Session, user_id: int) -> TelegramPendingAction | None:
    for action in _active_actions(db, user_id):
        if action.expires_at < datetime.utcnow():
            action.status = "expired"; action.resolved_at = datetime.utcnow(); db.commit()
            continue
        return action if action.status == "collecting" else None
    return None


def confirmation_keyboard(action_id: int) -> list[list[dict]]:
    return [[{"text": "✅ Confirmar", "callback_data": f"fb:c:{action_id}"},
             {"text": "❌ Cancelar", "callback_data": f"fb:x:{action_id}"}]]


def prepare_payment(db: Session, user_id: int, *, today: date | None = None, rule_id=None, bill=None,
                    area=None, amount=None, paid_date=None, competence=None, account=None,
                    ask_amount: bool = False) -> dict:
    today = today or today_sp()
    context = _context(db, user_id)
    if not context or not context.writable_person_ids:
        return {"fixed_bill": True, "status": "forbidden",
                "message": "Você não tem permissão para registrar pagamentos de contas fixas."}
    rule, error = _resolve_rule(db, context, rule_id=rule_id, bill=bill, area=area)
    if error:
        return error
    people = _people(db, context)
    requested = _parse_competence(competence, today)
    open_bills = _open_bills(db, context, rule, today, requested)
    if requested:
        chosen = next((item for item in open_bills if (item.year, item.month) == requested), None)
        if not chosen:
            listing = ", ".join(item.competence for item in open_bills) or "nenhuma"
            return {"fixed_bill": True, "status": "already_resolved",
                    "message": (f"A competência {requested[1]:02d}/{requested[0]} de **{_label(rule, people)}** "
                                f"já foi confirmada ou não está em aberto. Competências em aberto: {listing}.")}
    else:
        if not open_bills:
            return {"fixed_bill": True, "status": "nothing_open",
                    "message": f"Não há competência em aberto para **{_label(rule, people)}**."}
        chosen = open_bills[0]
    paid_amount = None
    if amount not in (None, ""):
        try:
            paid_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except InvalidOperation:
            paid_amount = parse_amount(str(amount))
        if paid_amount is not None and paid_amount <= 0:
            paid_amount = None
    selected_account, accounts = _resolve_account(db, rule, account)
    if not accounts:
        return {"fixed_bill": True, "status": "no_account",
                "message": (f"Não há conta bancária ativa na área de **{_label(rule, people)}**. "
                            "Cadastre uma conta na interface web antes de confirmar.")}
    phase = "amount" if ask_amount and paid_amount is None else ("account" if not selected_account else None)
    payload = {
        "rule_id": rule.id, "year": chosen.year, "month": chosen.month,
        "expected": str(chosen.amount), "amount": str(paid_amount or chosen.amount),
        "paid_on": _parse_date(paid_date, today).isoformat(),
        "account_id": selected_account.id if selected_account else None, "phase": phase,
    }
    action = None
    for item in _active_actions(db, user_id):
        old = json.loads(item.payload)
        if action is None and (old.get("rule_id"), old.get("year"), old.get("month")) == (
                rule.id, chosen.year, chosen.month) and item.expires_at >= datetime.utcnow():
            action = item  # reaproveita: clique duplo nao cria outro rascunho
        else:
            item.status = "cancelled"; item.resolved_at = datetime.utcnow()
    if action is None:
        action = TelegramPendingAction(user_id=user_id, action_type=ACTION_TYPE, payload="{}",
                                       status="collecting", expires_at=datetime.utcnow() + EXPIRATION)
        db.add(action)
    action.payload = json.dumps(payload, ensure_ascii=False)
    action.status = "collecting" if phase else "awaiting_confirmation"
    action.expires_at = datetime.utcnow() + EXPIRATION
    db.commit()
    logger.info("fixed_bill_prepared user_id=%s rule_id=%s competence=%s-%02d phase=%s action_id=%s",
                user_id, rule.id, chosen.year, chosen.month, phase, action.id)
    base = {"fixed_bill": True, "action_id": action.id, "rule_id": rule.id,
            "competence": f"{chosen.year}-{chosen.month:02d}", "amount": payload["amount"]}
    if phase == "amount":
        return {**base, "status": "missing_amount",
                "message": (f"Qual foi o valor pago de **{_label(rule, people)}** "
                            f"(competência {chosen.competence})? Previsto: {format_brl(chosen.amount)}.")}
    if phase == "account":
        options = "\n".join(f"- {item.name}" for item in accounts)
        return {**base, "status": "missing_account", "account_options": [item.name for item in accounts],
                "message": (f"De qual conta saiu o pagamento de **{_label(rule, people)}**?\n\n{options}")}
    return {**base, "status": "awaiting_confirmation",
            "message": _confirmation_text(rule, people, payload, selected_account, open_bills)}


def confirm_payment(db: Session, user_id: int, action_id: int | None = None) -> dict:
    query = select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == ACTION_TYPE)
    if action_id:
        query = query.where(TelegramPendingAction.id == int(action_id))
    else:
        query = query.where(TelegramPendingAction.status == "awaiting_confirmation").order_by(
            TelegramPendingAction.id.desc())
    action = db.scalar(query.with_for_update())
    if not action:
        return {"fixed_bill": True, "status": "not_found",
                "message": "Não encontrei pagamento de conta fixa aguardando confirmação."}
    if action.status == "confirmed":
        return {"fixed_bill": True, "status": "already_registered", "already_registered": True,
                "message": "Esse pagamento já foi registrado. Nada foi lançado de novo."}
    if action.status != "awaiting_confirmation":
        return {"fixed_bill": True, "status": action.status,
                "message": "Esse pedido não está mais aguardando confirmação. Envie /contas para ver as pendentes."}
    if action.expires_at < datetime.utcnow():
        action.status = "expired"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"fixed_bill": True, "status": "expired",
                "message": "Esse pedido expirou. Envie /contas para começar de novo."}
    payload = json.loads(action.payload)
    context = _context(db, user_id)
    rule = next((item for item in _rules(db, context) if item.id == payload["rule_id"]), None) if context else None
    account = db.get(Account, payload.get("account_id")) if payload.get("account_id") else None
    if not rule or not account or not account.is_active or account.person_id != rule.person_id:
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"fixed_bill": True, "status": "cancelled",
                "message": "A conta fixa ou a conta de pagamento mudou desde o resumo. Nada foi registrado."}
    people = _people(db, context)
    label = _label(rule, people)
    amount = Decimal(payload["amount"])
    paid_on = date.fromisoformat(payload["paid_on"])
    try:
        transaction, target_year, target_month = apply_recurrence_confirmation(
            db, rule=rule, year=payload["year"], month=payload["month"], amount=amount,
            created_by_id=user_id, account_id=account.id, payment_method="Conta bancária",
            transaction_date=paid_on, source="telegram",
        )
        action.status = "confirmed"; action.resolved_at = datetime.utcnow()
        db.commit()
    except RecurrenceAlreadyResolved:
        db.rollback()
        action = db.get(TelegramPendingAction, action.id)
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"fixed_bill": True, "status": "already_resolved", "already_registered": True,
                "message": (f"A competência {payload['month']:02d}/{payload['year']} de **{label}** já estava "
                            "confirmada. Nada foi lançado de novo.")}
    except IntegrityError:
        db.rollback()
        return {"fixed_bill": True, "status": "already_resolved", "already_registered": True,
                "message": "Esse pagamento já foi registrado. Nada foi lançado de novo."}
    logger.info("fixed_bill_confirmed user_id=%s rule_id=%s competence=%s-%02d transaction_id=%s",
                user_id, rule.id, payload["year"], payload["month"], transaction.id)
    note = ""
    if (target_year, target_month) != (payload["year"], payload["month"]):
        note = f" Lançado na competência {target_month:02d}/{target_year} porque o mês estava fechado."
    return {"fixed_bill": True, "status": "registered", "registered": True, "transaction_id": transaction.id,
            "message": (f"Pagamento registrado: **{label}**, competência {payload['month']:02d}/{payload['year']}, "
                        f"{format_brl(amount)} em {paid_on.strftime('%d/%m/%Y')} pela conta {account.name}.{note}")}


def cancel_payment(db: Session, user_id: int, action_id: int | None = None) -> dict:
    actions = _active_actions(db, user_id)
    if action_id:
        actions = [item for item in actions if item.id == int(action_id)]
    if not actions:
        return {"fixed_bill": True, "status": "not_found",
                "message": "Não há pagamento de conta fixa pendente para cancelar."}
    for item in actions:
        item.status = "cancelled"; item.resolved_at = datetime.utcnow()
    db.commit()
    return {"fixed_bill": True, "status": "cancelled", "cancelled": True,
            "message": "Pagamento cancelado. Nada foi registrado."}


def set_due_day(db: Session, user_id: int, *, bill=None, area=None, due_day=None, rule_id=None) -> dict:
    context = _context(db, user_id)
    if not context or not context.writable_person_ids:
        return {"fixed_bill": True, "status": "forbidden",
                "message": "Você não tem permissão para alterar contas fixas."}
    try:
        day = int(str(due_day).strip())
    except (TypeError, ValueError):
        day = 0
    if not 1 <= day <= 31:
        return {"fixed_bill": True, "status": "invalid_day", "message": "Informe um dia de vencimento entre 1 e 31."}
    rule, error = _resolve_rule(db, context, rule_id=rule_id, bill=bill, area=area)
    if error:
        return error
    rule.days_of_month = str(day)
    db.commit()
    logger.info("fixed_bill_due_day_set user_id=%s rule_id=%s day=%s", user_id, rule.id, day)
    return {"fixed_bill": True, "status": "updated", "updated_due_day": True,
            "message": f"Combinado: **{_label(rule, _people(db, context))}** vence todo dia {day}."}


def _due_text(bill: PendingBill, today: date) -> str:
    if bill.due_date:
        if bill.due_date < today:
            return f"⚠️ vencida em {bill.due_date.strftime('%d/%m')}"
        if bill.due_date == today:
            return "⏰ vence hoje"
        return f"vence {bill.due_date.strftime('%d/%m')}"
    return "⚠️ competência anterior em aberto" if bill.is_overdue(today) else "sem dia de vencimento"


def bills_keyboard(bills: list[PendingBill], today: date) -> list[list[dict]]:
    rows = []
    for index, bill in enumerate(bills[:MAX_KEYBOARD_BILLS], start=1):
        key = f"{bill.rule.id}:{bill.year}{bill.month:02d}"
        rows.append([{"text": f"✅ Paguei {index}. {bill.rule.description}"[:60], "callback_data": f"fb:p:{key}"}])
        second = [{"text": f"✏️ Outro valor {index}", "callback_data": f"fb:v:{key}"}]
        if (bill.year, bill.month) != (today.year, today.month):
            second.append({"text": f"📅 Mês atual {index}", "callback_data": f"fb:m:{bill.rule.id}"})
        rows.append(second)
    return rows


def _bill_lines(bills: list[PendingBill], people, today: date) -> list[str]:
    return [f"{index}. **{_label(bill.rule, people)}** — {bill.competence} — {format_brl(bill.amount)} — "
            f"{_due_text(bill, today)}" for index, bill in enumerate(bills, start=1)]


def user_pending_bills(db: Session, user_id: int, today: date, area: str | None = None):
    context = _context(db, user_id)
    if not context:
        return [], {}
    people = _people(db, context)
    person_ids = context.writable_person_ids
    if area:
        wanted = _norm(area)
        person_ids = tuple(pid for pid, person in people.items()
                           if _norm(person.name) == wanted or wanted in _norm(person.name)) or person_ids
    bills = pending_fixed_bills(db, workspace_ids=context.writable_workspace_ids, person_ids=person_ids, today=today)
    return bills, people


def pending_reply(db: Session, user_id: int, today: date | None = None, area: str | None = None) -> BotReply:
    today = today or today_sp()
    bills, people = user_pending_bills(db, user_id, today, area)
    if not bills:
        return BotReply(f"Nenhuma conta fixa em aberto até {today.month:02d}/{today.year}. 🎉")
    lines = ["**Contas fixas em aberto**", "", *_bill_lines(bills, people, today)]
    if len(bills) > MAX_KEYBOARD_BILLS:
        lines.append(f"\nBotões para as {MAX_KEYBOARD_BILLS} primeiras. Para as demais, escreva \"paguei ...\".")
    lines.append("\nUse os botões ou escreva, por exemplo, \"paguei a luz 287,40 hoje\".")
    return BotReply("\n".join(lines), bills_keyboard(bills, today))


def list_pending_tool(db: Session, user_id: int, args: dict) -> dict:
    today = today_sp()
    reply = pending_reply(db, user_id, today, args.get("area"))
    return {"fixed_bill": True, "status": "listed", "message": reply.text}


PENDING_COMMANDS = {"/contas", "contas", "contas pendentes", "contas a pagar", "contas em aberto",
                    "contas fixas", "contas fixas pendentes", "quais contas faltam pagar"}
DUE_DAY_PATTERN = re.compile(r"^\s*(?:a\s+|o\s+)?(.+?)\s+vence\s+(?:todo\s+)?(?:o\s+)?dia\s+(\d{1,2})\s*[.!]?\s*$")


def handle_bill_text(db: Session, user: User, text: str, today: date | None = None) -> BotReply | None:
    """Tratamento local (sem LLM) de comandos e respostas dos fluxos de contas fixas."""
    today = today or today_sp()
    normalized = " ".join(_norm(text).strip(" .!?").split())
    command = normalized.split("@")[0]
    if command in PENDING_COMMANDS or command.startswith("/contas"):
        return pending_reply(db, user.id, today)
    due = DUE_DAY_PATTERN.match(normalized)
    if due:
        result = set_due_day(db, user.id, bill=due.group(1), due_day=due.group(2))
        if result["status"] in ("updated", "ambiguous", "invalid_day"):
            return BotReply(result["message"])
    action = active_collecting_action(db, user.id)
    if not action:
        return None
    payload = json.loads(action.payload)
    if normalized in {"nao", "n", "cancelar", "cancela", "deixa", "esquece"}:
        return BotReply(cancel_payment(db, user.id, action.id)["message"])
    if payload.get("phase") == "amount":
        amount = parse_amount(text)
        if amount is None:
            action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
            return None
        result = prepare_payment(db, user.id, today=today, rule_id=payload["rule_id"], amount=amount,
                                 competence=(payload["year"], payload["month"]), paid_date=payload.get("paid_on"))
    elif payload.get("phase") == "account":
        result = prepare_payment(db, user.id, today=today, rule_id=payload["rule_id"], amount=payload["amount"],
                                 competence=(payload["year"], payload["month"]), paid_date=payload.get("paid_on"),
                                 account=text)
        if result.get("status") == "missing_account" and len(normalized.split()) > 4:
            cancel_payment(db, user.id, result.get("action_id"))
            return None
    else:
        return None
    keyboard = confirmation_keyboard(result["action_id"]) if result.get("status") == "awaiting_confirmation" else None
    return BotReply(result["message"], keyboard)


def handle_bill_callback(db: Session, user: User, data: str, today: date | None = None) -> BotReply:
    today = today or today_sp()
    parts = (data or "").split(":")
    try:
        if len(parts) < 3 or parts[0] != "fb":
            raise ValueError
        kind = parts[1]
        if kind in ("p", "v"):
            rule_id, raw = int(parts[2]), parts[3]
            competence = (int(raw[:4]), int(raw[4:]))
            result = prepare_payment(db, user.id, today=today, rule_id=rule_id, competence=competence,
                                     ask_amount=kind == "v")
        elif kind == "m":
            result = prepare_payment(db, user.id, today=today, rule_id=int(parts[2]),
                                     competence=(today.year, today.month))
        elif kind == "c":
            result = confirm_payment(db, user.id, int(parts[2]))
        elif kind == "x":
            result = cancel_payment(db, user.id, int(parts[2]))
        else:
            raise ValueError
    except (ValueError, IndexError):
        return BotReply("Não reconheci esse botão. Envie /contas para ver as contas pendentes.")
    keyboard = confirmation_keyboard(result["action_id"]) if result.get("status") == "awaiting_confirmation" else None
    return BotReply(result["message"], keyboard)


def reminder_time(db: Session) -> time:
    raw = db.scalar(select(SystemSetting.value).where(SystemSetting.key == "bill_reminder_time")) or ""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip()) or re.fullmatch(r"(\d{1,2}):(\d{2})", DEFAULT_REMINDER_TIME)
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = 8, 0
    return time(hour, minute)


def send_daily_reminders(db: Session, send, now: datetime | None = None) -> int:
    """Envia, uma vez por dia e por usuario, as contas que vencem hoje e as vencidas em aberto.

    O registro em telegram_bill_reminders e gravado ANTES do envio, com chave unica (usuario, dia),
    para que um restart ou um segundo worker nunca repitam o aviso.
    """
    now = (now or datetime.now(SAO_PAULO)).astimezone(SAO_PAULO)
    if now.time() < reminder_time(db):
        return 0
    today = now.date()
    sent = 0
    for link in db.scalars(select(TelegramLink)).all():
        user = db.get(User, link.user_id)
        if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
            continue
        if db.scalar(select(TelegramBillReminder.id).where(
                TelegramBillReminder.user_id == user.id, TelegramBillReminder.reminder_date == today)):
            continue
        db.add(TelegramBillReminder(user_id=user.id, reminder_date=today))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        bills, people = user_pending_bills(db, user.id, today)
        bills = [item for item in bills if item.is_overdue(today) or item.is_due_today(today)]
        if not bills:
            continue
        text = "\n".join(["**Bom dia! Contas fixas que pedem atenção hoje**", "",
                          *_bill_lines(bills, people, today),
                          "", "Use os botões para confirmar o pagamento ou envie /contas."])
        try:
            send(link.telegram_chat_id, BotReply(text, bills_keyboard(bills, today)))
            sent += 1
            logger.info("bill_reminder_sent user_id=%s bills=%s", user.id, len(bills))
        except Exception:
            logger.exception("bill_reminder_failed user_id=%s", user.id)
    return sent