"""Regras compartilhadas de confirmacao de despesas/receitas recorrentes (web e Telegram)."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountingPeriod, Financing, RecurrenceOccurrence, RecurrenceRule, Transaction, TransactionStatus,
    TransactionType,
)


class RecurrenceAlreadyResolved(ValueError):
    """A competencia ja foi confirmada ou ignorada."""


def first_open_competence(db: Session, workspace_id: int, year: int, month: int) -> tuple[int, int]:
    """Avanca enquanto o periodo contabil estiver fechado, como a tela web sempre fez."""
    target_year, target_month = year, month
    while db.scalar(select(AccountingPeriod.id).where(
        AccountingPeriod.workspace_id == workspace_id, AccountingPeriod.year == target_year,
        AccountingPeriod.month == target_month, AccountingPeriod.is_closed.is_(True),
    )):
        target_month += 1
        if target_month == 13:
            target_month, target_year = 1, target_year + 1
    return target_year, target_month


def find_occurrence(db: Session, rule_id: int, year: int, month: int, lock: bool = False):
    query = select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id == rule_id,
        RecurrenceOccurrence.year == year, RecurrenceOccurrence.month == month,
    )
    if lock:
        query = query.with_for_update()
    return db.scalar(query)


def remaining_amount(rule: RecurrenceRule, occurrence: RecurrenceOccurrence | None) -> Decimal:
    if occurrence and occurrence.remaining_amount is not None:
        return Decimal(occurrence.remaining_amount)
    return Decimal(rule.amount or 0)


def apply_recurrence_confirmation(
    db: Session, *, rule: RecurrenceRule, year: int, month: int, amount: Decimal, created_by_id: int,
    account_id: int | None, card_id: int | None = None, payment_method: str | None = None,
    transaction_status: TransactionStatus = TransactionStatus.paid, transaction_date: date | None = None,
    is_partial: bool = False, description: str | None = None, category_id: int | None = None,
    notes: str | None = None, source: str = "web",
) -> tuple[Transaction, int, int]:
    """Grava o pagamento de uma competencia. Nao faz commit."""
    existing = find_occurrence(db, rule.id, year, month, lock=True)
    if existing and existing.status != "partial":
        raise RecurrenceAlreadyResolved(f"Competencia {month:02d}/{year} ja resolvida")
    remaining_before = remaining_amount(rule, existing)
    target_year, target_month = first_open_competence(db, rule.workspace_id, year, month)
    transaction = Transaction(
        workspace_id=rule.workspace_id, transaction_type=rule.transaction_type,
        description=(description or rule.description or "").strip(), amount=amount,
        transaction_date=transaction_date or date.today(), status=transaction_status,
        account_id=account_id, card_id=card_id, person_id=rule.person_id,
        category_id=category_id if category_id is not None else rule.category_id,
        payment_method=payment_method, notes=notes if notes is not None else rule.notes,
        created_by_id=created_by_id, recurrence_rule_id=rule.id,
        competence_year=target_year, competence_month=target_month, source=source,
    )
    db.add(transaction); db.flush()
    if existing:
        existing.status = "partial" if is_partial else "confirmed"
        existing.remaining_amount = remaining_before - amount if is_partial else Decimal(0)
        existing.transaction_id = transaction.id
    else:
        db.add(RecurrenceOccurrence(
            recurrence_rule_id=rule.id, year=year, month=month,
            status="partial" if is_partial else "confirmed", transaction_id=transaction.id,
            remaining_amount=remaining_before - amount if is_partial else Decimal(0),
        ))
    financing = db.scalar(select(Financing).where(Financing.recurrence_rule_id == rule.id))
    if financing and not is_partial:
        financing.paid_installments = min(financing.total_installments, financing.paid_installments + 1)
        if financing.paid_installments >= financing.total_installments:
            financing.status = "paid"
            rule.is_active = False
        else:
            rule.description = (f"{financing.description} - parcela {financing.paid_installments + 1}/"
                                f"{financing.total_installments}")
    db.flush()
    return transaction, target_year, target_month


def rule_due_day(rule: RecurrenceRule, financing_due_day: int | None = None) -> int | None:
    raw = (rule.days_of_month or "").split(",")[0].strip()
    if raw.isdigit() and 1 <= int(raw) <= 31:
        return int(raw)
    if financing_due_day and 1 <= financing_due_day <= 31:
        return financing_due_day
    return None


def due_date_for(day: int | None, year: int, month: int) -> date | None:
    if not day:
        return None
    return date(year, month, min(day, monthrange(year, month)[1]))


@dataclass
class PendingBill:
    rule: RecurrenceRule
    year: int
    month: int
    amount: Decimal
    due_date: date | None
    partial: bool = False

    @property
    def competence(self) -> str:
        return f"{self.month:02d}/{self.year}"

    def is_overdue(self, today: date) -> bool:
        if self.due_date:
            return self.due_date < today
        return (self.year, self.month) < (today.year, today.month)

    def is_due_today(self, today: date) -> bool:
        return self.due_date == today


def pending_fixed_bills(
    db: Session, *, workspace_ids, person_ids, today: date, through: tuple[int, int] | None = None,
    rule_ids=None,
) -> list[PendingBill]:
    """Competencias de despesas fixas sem confirmacao, do mes de criacao ate `through`."""
    if not workspace_ids or not person_ids:
        return []
    through = through or (today.year, today.month)
    query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id.in_(tuple(workspace_ids)),
        RecurrenceRule.person_id.in_(tuple(person_ids)),
        RecurrenceRule.transaction_type == TransactionType.expense,
        RecurrenceRule.is_active.is_(True),
        RecurrenceRule.description.is_not(None), RecurrenceRule.amount.is_not(None),
    )
    if rule_ids is not None:
        query = query.where(RecurrenceRule.id.in_(tuple(rule_ids)))
    rules = db.scalars(query).all()
    if not rules:
        return []
    ids = [rule.id for rule in rules]
    occurrences = {(item.recurrence_rule_id, item.year, item.month): item for item in db.scalars(
        select(RecurrenceOccurrence).where(RecurrenceOccurrence.recurrence_rule_id.in_(ids))
    ).all()}
    financing_days = dict(db.execute(select(Financing.recurrence_rule_id, Financing.due_day).where(
        Financing.recurrence_rule_id.in_(ids)
    )).all())
    bills: list[PendingBill] = []
    for rule in rules:
        created = rule.created_at.date() if rule.created_at else today
        year, month = created.year, created.month
        day = rule_due_day(rule, financing_days.get(rule.id))
        while (year, month) <= through:
            occurrence = occurrences.get((rule.id, year, month))
            if not occurrence or occurrence.status == "partial":
                bills.append(PendingBill(
                    rule=rule, year=year, month=month, amount=remaining_amount(rule, occurrence),
                    due_date=due_date_for(day, year, month), partial=bool(occurrence),
                ))
            month += 1
            if month == 13:
                year, month = year + 1, 1
    bills.sort(key=lambda item: (item.year, item.month, item.due_date or date.max,
                                 (item.rule.description or "").casefold()))
    return bills