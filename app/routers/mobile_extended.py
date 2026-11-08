"""Additional native-app endpoints for finance workflows already available on web."""
import logging
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.database import get_db
from app.dependencies import allowed_person_ids, writable_person_ids
from app.models import (
    Account, AccountType, AccountingPeriod, Card, CardBillingPeriod, Category, ChatMedia, FinancialGoal, Financing,
    FinancingAmortization, FuelFillup, MonthlyBudget, MonthlyBudgetOccurrence, Person, PersonType,
    RecurrenceOccurrence, RecurrenceRule, SystemSetting, TelegramConversationMessage,
    Transaction, TransactionStatus, TransactionType, User, UserMemory, Vehicle, WorkspaceMember,
)
from app.routers.api import (
    _account, _mobile_context, _money, _require_area, _selected_area, _transaction_area_filter,
    _user_json, _workspace_id, _visible_area_ids,
)
from app.services.chat_history import model_window
from app.services.card_billing import card_invoice_is_closed, card_purchase_competence
from app.services.monthly_projection import monthly_budget_positions
from app.services.accounting_periods import current_open_month
from app.services.chat_media import (IMAGE_TRANSCRIPT_PREFIX, MAX_MEDIA_BYTES, MediaProcessingError, describe_receipt,
    read_bytes as read_private_media, store_bytes, synthesize_speech, transcribe_audio)
from app.services.recurrence_payments import (
    RecurrenceAlreadyResolved, apply_recurrence_confirmation, find_occurrence, remaining_amount,
)
from app.security import hash_password, verify_password

router = APIRouter(prefix="/api/v1", tags=["mobile-api"])
logger = logging.getLogger(__name__)


class RecurrencePayload(BaseModel):
    transaction_type: TransactionType
    description: str = Field(min_length=1, max_length=180)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    person_id: int
    account_id: int | None = None
    category_id: int | None = None
    payment_method: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=4000)
    due_day: int | None = Field(default=None, ge=1, le=31)


class RecurrencePayment(BaseModel):
    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    account_id: int | None = None
    card_id: int | None = None
    transaction_date: date | None = None
    description: str | None = Field(default=None, max_length=180)
    notes: str | None = Field(default=None, max_length=4000)
    settlement_mode: str = "full"


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class GoalPayload(BaseModel):
    goal_type: str = Field(pattern=r"^(purchase|habit)$")
    title: str = Field(min_length=1, max_length=180)
    sentence: str = Field(min_length=1, max_length=2000)
    target_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    weekly_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    hour_label: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    start_date: date
    end_date: date
    weeks: int = Field(ge=1, le=520)
    is_tight: bool = False
    person_id: int


class GoalProgressPayload(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


class AccountPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    bank_name: str = Field(default="", max_length=120)
    account_type: AccountType
    initial_balance: Decimal = Field(default=Decimal(0), max_digits=14, decimal_places=2)
    person_id: int
    color: str = Field(default="#6c5ce7", pattern=r"^#[0-9A-Fa-f]{6}$")


class CategoryPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: TransactionType
    parent_name: str | None = Field(default=None, max_length=120)
    color: str = Field(default="#64748b", pattern=r"^#[0-9A-Fa-f]{6}$")


class CardPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    brand: str = Field(default="", max_length=60)
    account_id: int
    credit_limit: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    closing_day: int = Field(ge=1, le=31)
    due_day: int = Field(ge=1, le=31)
    color: str = Field(default="#1e293b", pattern=r"^#[0-9A-Fa-f]{6}$")


class FinancingPayload(BaseModel):
    description: str = Field(min_length=1, max_length=180)
    contract_type: str = Field(default="financing", pattern=r"^(financing|negotiated_debt|personal_loan)$")
    person_id: int
    total_installments: int = Field(ge=1)
    paid_installments: int = Field(default=0, ge=0)
    installment_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    financed_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    outstanding_balance: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    nominal_interest_rate: Decimal | None = Field(default=None, ge=0, max_digits=7, decimal_places=4)
    institution: str | None = Field(default=None, max_length=120)
    account_id: int | None = None
    category_id: int | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    start_date: date | None = None
    notes: str | None = Field(default=None, max_length=4000)


class AmortizationPayload(BaseModel):
    amortization_date: date
    new_outstanding_balance: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    strategy: str = Field(pattern=r"^(reduce_term|reduce_installment|reduce_both)$")
    amortized_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    remaining_installments: int | None = Field(default=None, ge=1)
    new_installment_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    notes: str | None = Field(default=None, max_length=4000)


class VehiclePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    year: int | None = Field(default=None, ge=1900, le=2200)
    initial_odometer_km: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=1)
    fuel_type: str | None = Field(default=None, max_length=40)


class VehicleUpdatePayload(VehiclePayload):
    is_active: bool = True


class FuelPayload(BaseModel):
    vehicle_id: int
    transaction_id: int | None = None
    fillup_date: date
    odometer_km: Decimal = Field(ge=0, max_digits=12, decimal_places=1)
    liters: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    price_per_liter: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    is_full_tank: bool = False
    station: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)


class PasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class AreaPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    person_type: PersonType = PersonType.personal


class TransactionUpdatePayload(BaseModel):
    description: str = Field(min_length=1, max_length=180)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    transaction_date: date
    account_id: int | None = None
    category_id: int | None = None
    status: TransactionStatus
    notes: str | None = Field(default=None, max_length=4000)


class CardExpenseUpdatePayload(BaseModel):
    description: str = Field(min_length=1, max_length=180)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    category_id: int
    transaction_date: date
    invoice_year: int | None = Field(default=None, ge=2000, le=2200)
    invoice_month: int | None = Field(default=None, ge=1, le=12)


def _recurrence_json(rule: RecurrenceRule) -> dict:
    return {
        "id": rule.id, "transaction_type": rule.transaction_type.value,
        "description": rule.description, "amount": _money(rule.amount),
        "person_id": rule.person_id, "person_name": rule.person.name if rule.person else None,
        "account_id": rule.account_id, "account_name": rule.account.name if rule.account else None,
        "category_id": rule.category_id, "category_name": rule.category.name if rule.category else None,
        "payment_method": rule.payment_method, "notes": rule.notes,
        "due_day": int((rule.days_of_month or "0").split(",")[0]) if (rule.days_of_month or "").split(",")[0].isdigit() else None,
        "is_active": rule.is_active,
    }


@router.get("/recurrences")
def list_recurrences(area_id: int | None = None, kind: TransactionType | None = None,
                     year: int | None = Query(default=None, ge=2000, le=2200),
                     month: int | None = Query(default=None, ge=1, le=12),
                     include_inactive: bool = False, context=Depends(_mobile_context)):
    if (year is None) != (month is None):
        raise HTTPException(422, "Informe ano e mês juntos")
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    query = select(RecurrenceRule).options(joinedload(RecurrenceRule.person), joinedload(RecurrenceRule.account),
        joinedload(RecurrenceRule.category)).where(
            RecurrenceRule.workspace_id == workspace_id,
            RecurrenceRule.description.is_not(None), RecurrenceRule.amount.is_not(None),
        )
    if kind:
        query = query.where(RecurrenceRule.transaction_type == kind)
        if kind == TransactionType.expense:
            financing_rules = select(Financing.recurrence_rule_id).where(Financing.recurrence_rule_id.is_not(None))
            query = query.where(RecurrenceRule.id.not_in(financing_rules))
    if not include_inactive:
        query = query.where(RecurrenceRule.is_active.is_(True))
    visible = _visible_area_ids(user, db)
    if selected is not None:
        query = query.where(RecurrenceRule.person_id == selected)
    elif visible is not None:
        query = query.where(RecurrenceRule.person_id.in_(visible)) if visible else query.where(False)
    rules = db.scalars(query.order_by(
        RecurrenceRule.transaction_type, RecurrenceRule.is_active.desc(), RecurrenceRule.description,
    )).unique().all()
    items = []
    for rule in rules:
        row = _recurrence_json(rule)
        if year and month:
            occurrence = find_occurrence(db, rule.id, year, month)
            row["occurrence_status"] = occurrence.status if occurrence else None
            row["remaining_amount"] = _money(remaining_amount(rule, occurrence))
            row["occurrence_transaction_id"] = occurrence.transaction_id if occurrence else None
            row["is_due_this_month"] = bool(rule.created_at and (rule.created_at.year, rule.created_at.month) <= (year, month))
        items.append(row)
    return {"items": items}


@router.post("/recurrences", status_code=status.HTTP_201_CREATED)
def create_recurrence(payload: RecurrencePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    account = _account(db, user, workspace_id, payload.account_id, write=True)
    if account and account.person_id != payload.person_id:
        raise HTTPException(403, "A conta precisa pertencer à área financeira escolhida")
    category = db.get(Category, payload.category_id) if payload.category_id else None
    if category and (category.workspace_id != workspace_id or category.kind != payload.transaction_type):
        raise HTTPException(422, "Categoria inválida para este tipo de recorrência")
    rule = RecurrenceRule(
        workspace_id=workspace_id, frequency="monthly", transaction_type=payload.transaction_type,
        description=payload.description.strip(), amount=payload.amount, person_id=payload.person_id,
        account_id=payload.account_id, category_id=payload.category_id,
        payment_method=payload.payment_method, notes=payload.notes,
        days_of_month=str(payload.due_day) if payload.due_day else None,
        created_by_id=user.id, is_active=True,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _recurrence_json(rule)


@router.patch("/recurrences/{rule_id}")
def update_recurrence(rule_id: int, payload: RecurrencePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id,
        RecurrenceRule.workspace_id == workspace_id, RecurrenceRule.is_active.is_(True)))
    if not rule or rule.transaction_type != payload.transaction_type:
        raise HTTPException(404, "Recorrência não encontrada")
    _require_area(user, workspace_id, db, rule.person_id, write=True)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    account = _account(db, user, workspace_id, payload.account_id, write=True)
    if account and account.person_id != payload.person_id:
        raise HTTPException(403, "A conta precisa pertencer à área financeira escolhida")
    category = db.get(Category, payload.category_id) if payload.category_id else None
    if category and (category.workspace_id != workspace_id or category.kind != payload.transaction_type):
        raise HTTPException(422, "Categoria inválida")
    rule.description, rule.amount, rule.person_id = payload.description.strip(), payload.amount, payload.person_id
    rule.account_id, rule.category_id = payload.account_id, payload.category_id
    rule.payment_method, rule.notes = payload.payment_method, payload.notes
    rule.days_of_month = str(payload.due_day) if payload.due_day else None
    db.commit()
    return _recurrence_json(rule)


@router.delete("/recurrences/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recurrence(rule_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id,
        RecurrenceRule.workspace_id == workspace_id))
    if not rule:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, rule.person_id, write=True)
    rule.is_active = False
    db.commit()
    return None


@router.post("/recurrences/{rule_id}/confirm")
def confirm_recurrence(rule_id: int, payload: RecurrencePayment, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id,
        RecurrenceRule.workspace_id == workspace_id, RecurrenceRule.is_active.is_(True)))
    if not rule:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, rule.person_id, write=True)
    occurrence = find_occurrence(db, rule.id, payload.year, payload.month, lock=True)
    if occurrence and occurrence.status != "partial":
        raise HTTPException(409, "Esta recorrência já foi resolvida no mês selecionado")
    is_partial = rule.transaction_type == TransactionType.expense and payload.settlement_mode == "partial"
    remaining = remaining_amount(rule, occurrence)
    if is_partial and payload.amount >= remaining:
        raise HTTPException(422, "Para manter saldo programado, informe menos que o saldo restante")
    account_id, card_id, payment_method = rule.account_id, None, rule.payment_method
    transaction_status = TransactionStatus.paid
    if rule.transaction_type == TransactionType.expense:
        if payload.card_id:
            card = db.scalar(select(Card).where(Card.id == payload.card_id,
                Card.workspace_id == workspace_id, Card.is_active.is_(True)))
            if not card or not card.account_id:
                raise HTTPException(422, "Cartão inválido ou sem conta vinculada")
            account = _account(db, user, workspace_id, card.account_id, write=True)
            if account.person_id != rule.person_id:
                raise HTTPException(403, "O cartão não pertence à área financeira desta conta")
            account_id, card_id, payment_method = account.id, card.id, "Crédito"
            transaction_status = TransactionStatus.pending
        else:
            account = _account(db, user, workspace_id, payload.account_id, write=True)
            if not account or account.person_id != rule.person_id:
                raise HTTPException(422, "Selecione uma conta de pagamento desta área financeira")
            account_id, payment_method = account.id, "Conta bancária"
    try:
        item, target_year, target_month = apply_recurrence_confirmation(
            db, rule=rule, year=payload.year, month=payload.month, amount=payload.amount,
            created_by_id=user.id, account_id=account_id, card_id=card_id,
            payment_method=payment_method, transaction_status=transaction_status,
            transaction_date=payload.transaction_date or date.today(), is_partial=is_partial,
            description=(payload.description or rule.description), notes=payload.notes or rule.notes,
            source="mobile",
        )
    except RecurrenceAlreadyResolved as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    db.refresh(item)
    return {"transaction": {"id": item.id, "amount": _money(item.amount), "status": item.status.value,
        "description": item.description, "competence_year": target_year, "competence_month": target_month},
        "remaining": _money(remaining - payload.amount if is_partial else Decimal(0))}


@router.post("/recurrences/{rule_id}/skip")
def skip_recurrence(rule_id: int, year: int = Query(..., ge=2000, le=2200),
                    month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id,
        RecurrenceRule.workspace_id == workspace_id))
    if not rule:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, rule.person_id, write=True)
    occurrence = find_occurrence(db, rule.id, year, month, lock=True)
    if occurrence:
        raise HTTPException(409, "Esta recorrência já tem movimentação neste mês")
    db.add(RecurrenceOccurrence(recurrence_rule_id=rule.id, year=year, month=month, status="skipped"))
    db.commit()
    return {"skipped": True, "year": year, "month": month}


@router.get("/future")
def future_transactions(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    query = select(Transaction).options(joinedload(Transaction.category), joinedload(Transaction.account)).where(
        Transaction.workspace_id == workspace_id, Transaction.status == TransactionStatus.pending,
        Transaction.card_id.is_(None),
        _transaction_area_filter(user, workspace_id, db, selected),
    ).order_by(Transaction.transaction_date, Transaction.id)
    from app.routers.api import _transaction_json
    return {"items": [_transaction_json(item) for item in db.scalars(query).unique().all()]}


@router.post("/transactions/{transaction_id}/confirm")
def confirm_transaction(transaction_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id,
        Transaction.workspace_id == workspace_id, Transaction.status == TransactionStatus.pending))
    if not item:
        raise HTTPException(404, "Lançamento pendente não encontrado")
    _require_area(user, workspace_id, db, item.person_id, write=True)
    year, month = item.competence_year or item.transaction_date.year, item.competence_month or item.transaction_date.month
    while db.scalar(select(AccountingPeriod.id).where(AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == year, AccountingPeriod.month == month, AccountingPeriod.is_closed.is_(True))):
        month += 1
        if month == 13:
            year, month = year + 1, 1
    item.competence_year, item.competence_month = year, month
    item.status = TransactionStatus.paid
    db.commit()
    return {"confirmed": True, "competence_year": year, "competence_month": month}


@router.get("/cards/{card_id}/statement")
def card_statement(card_id: int, year: int = Query(..., ge=2000, le=2200),
                   month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == workspace_id,
        Card.is_active.is_(True)))
    if not card:
        raise HTTPException(404)
    account = _account(db, user, workspace_id, card.account_id, write=False)
    query = select(Transaction).options(joinedload(Transaction.category)).where(
        Transaction.workspace_id == workspace_id, Transaction.card_id == card.id,
        Transaction.competence_year == year, Transaction.competence_month == month,
        Transaction.status != TransactionStatus.cancelled,
    ).order_by(Transaction.transaction_date, Transaction.id)
    items = list(db.scalars(query).unique().all())
    period = db.scalar(select(CardBillingPeriod).where(CardBillingPeriod.card_id == card.id,
        CardBillingPeriod.year == year, CardBillingPeriod.month == month))
    from app.routers.api import _transaction_json
    return {"card": {"id": card.id, "name": card.name, "brand": card.brand,
                      "credit_limit": _money(card.credit_limit), "closing_day": card.closing_day,
                      "due_day": card.due_day, "account_id": card.account_id,
                      "account_name": account.name if account else None},
            "year": year, "month": month, "is_closed": bool(period and period.is_closed),
            "credit_total": _money(sum((Decimal(x.amount) for x in items if "credit" in (x.payment_method or "").casefold() or "crédito" in (x.payment_method or "").casefold()), Decimal(0))),
            "debit_total": _money(sum((Decimal(x.amount) for x in items if x.status == TransactionStatus.paid), Decimal(0))),
            "transactions": [_transaction_json(item) for item in items]}


@router.post("/cards/{card_id}/billing/confirm")
def confirm_card_billing(card_id: int, year: int = Query(..., ge=2000, le=2200),
                         month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == workspace_id))
    if not card:
        raise HTTPException(404)
    _account(db, user, workspace_id, card.account_id, write=True)
    items = db.scalars(select(Transaction).where(Transaction.workspace_id == workspace_id,
        Transaction.card_id == card.id, Transaction.competence_year == year,
        Transaction.competence_month == month, Transaction.status == TransactionStatus.pending)).all()
    for item in items:
        item.status = TransactionStatus.paid
    period = db.scalar(select(CardBillingPeriod).where(CardBillingPeriod.card_id == card.id,
        CardBillingPeriod.year == year, CardBillingPeriod.month == month))
    if not period:
        period = CardBillingPeriod(workspace_id=workspace_id, card_id=card.id, year=year, month=month)
        db.add(period)
    period.is_closed, period.closed_at, period.closed_by_id = True, datetime.utcnow(), user.id
    db.commit()
    return {"confirmed": True, "count": len(items), "paid_total": _money(sum((Decimal(x.amount) for x in items), Decimal(0)))}


@router.post("/cards/{card_id}/billing/reopen")
def reopen_card_billing(card_id: int, year: int = Query(..., ge=2000, le=2200),
                        month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == workspace_id))
    if not card:
        raise HTTPException(404)
    _account(db, user, workspace_id, card.account_id, write=True)
    period = db.scalar(select(CardBillingPeriod).where(CardBillingPeriod.card_id == card.id,
        CardBillingPeriod.year == year, CardBillingPeriod.month == month))
    if period:
        period.is_closed, period.closed_at, period.closed_by_id = False, None, None
        db.commit()
    return {"reopened": True}


@router.patch("/cards/{card_id}/transactions/{transaction_id}")
def update_card_expense(card_id: int, transaction_id: int, payload: CardExpenseUpdatePayload,
                        context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == workspace_id))
    if not card:
        raise HTTPException(404)
    account = _account(db, user, workspace_id, card.account_id, write=True)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id,
        Transaction.workspace_id == workspace_id, Transaction.card_id == card.id,
        Transaction.transaction_type == TransactionType.expense))
    if not item:
        raise HTTPException(404)
    category = db.scalar(select(Category).where(Category.id == payload.category_id,
        Category.workspace_id == workspace_id, Category.kind == TransactionType.expense))
    if not category:
        raise HTTPException(422, "Selecione uma categoria válida")
    is_credit = "credit" in (item.payment_method or "").casefold() or "crédito" in (item.payment_method or "").casefold()
    if is_credit and (payload.invoice_year is None) != (payload.invoice_month is None):
        raise HTTPException(422, "Informe ano e mês da fatura juntos")
    if is_credit and payload.invoice_year is not None:
        year, month = payload.invoice_year, payload.invoice_month
    elif is_credit:
        year, month = card_purchase_competence(db, card, payload.transaction_date)
    else:
        year, month = payload.transaction_date.year, payload.transaction_date.month
    item.description, item.amount, item.category_id = payload.description.strip(), payload.amount, category.id
    item.transaction_date, item.competence_year, item.competence_month = payload.transaction_date, year, month
    if is_credit:
        item.status = TransactionStatus.paid if card_invoice_is_closed(db, card.id, year, month) else TransactionStatus.pending
    db.commit()
    from app.routers.api import _transaction_json
    return _transaction_json(item)


@router.delete("/cards/{card_id}/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_card_expense(card_id: int, transaction_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == workspace_id))
    if not card:
        raise HTTPException(404)
    _account(db, user, workspace_id, card.account_id, write=True)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id,
        Transaction.workspace_id == workspace_id, Transaction.card_id == card.id))
    if not item:
        raise HTTPException(404)
    db.delete(item)
    db.commit()
    return None


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    item = Account(workspace_id=workspace_id, person_id=payload.person_id, name=payload.name.strip(),
        bank_name=payload.bank_name.strip(), account_type=payload.account_type,
        initial_balance=payload.initial_balance, color=payload.color)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name, "bank_name": item.bank_name,
        "account_type": item.account_type.value, "person_id": item.person_id,
        "initial_balance": _money(item.initial_balance), "color": item.color}


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    if not (user.is_super_admin or user.account_role.value == "admin"):
        raise HTTPException(403, "Somente administradores podem criar categorias")
    item = Category(workspace_id=workspace_id, name=payload.name.strip(), kind=payload.kind,
        parent_name=(payload.parent_name or "").strip() or None, color=payload.color)
    db.add(item)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "Já existe uma categoria com este nome e grupo")
    db.refresh(item)
    return {"id": item.id, "name": item.name, "parent_name": item.parent_name,
        "kind": item.kind.value, "color": item.color}


@router.post("/cards", status_code=status.HTTP_201_CREATED)
def create_card(payload: CardPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    account = _account(db, user, workspace_id, payload.account_id, write=True)
    item = Card(workspace_id=workspace_id, account_id=account.id, name=payload.name.strip(),
        brand=payload.brand.strip(), credit_limit=payload.credit_limit,
        closing_day=payload.closing_day, due_day=payload.due_day, color=payload.color)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name, "brand": item.brand,
        "account_id": item.account_id, "credit_limit": _money(item.credit_limit),
        "closing_day": item.closing_day, "due_day": item.due_day, "color": item.color}


@router.post("/transactions/{transaction_id}/edit")
def update_transaction(transaction_id: int, payload: TransactionUpdatePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id,
        Transaction.workspace_id == workspace_id,
        Transaction.status.in_([TransactionStatus.paid, TransactionStatus.pending])))
    if not item:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, item.person_id, write=True)
    if item.card_id:
        raise HTTPException(409, "Edite compras no cartão pelo extrato da fatura")
    account = _account(db, user, workspace_id, payload.account_id, write=True)
    if account and item.person_id and account.person_id != item.person_id:
        raise HTTPException(403, "A conta precisa pertencer à mesma área financeira")
    category = db.get(Category, payload.category_id) if payload.category_id else None
    if category and (category.workspace_id != workspace_id or category.kind != item.transaction_type):
        raise HTTPException(422, "Categoria incompatível com o lançamento")
    item.description, item.amount, item.transaction_date = payload.description.strip(), payload.amount, payload.transaction_date
    item.account_id, item.category_id, item.status, item.notes = account.id if account else None, payload.category_id, payload.status, payload.notes
    if item.competence_month is None:
        item.competence_year, item.competence_month = payload.transaction_date.year, payload.transaction_date.month
    db.commit()
    from app.routers.api import _transaction_json
    return _transaction_json(item)


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(transaction_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id, Transaction.workspace_id == workspace_id))
    if not item:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, item.person_id, write=True)
    occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.transaction_id == item.id))
    if occurrence:
        db.delete(occurrence)
    db.delete(item)
    db.commit()
    return None


@router.post("/months/{year}/{month}/close")
def close_month(year: int, month: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    if not 2000 <= year <= 2200 or not 1 <= month <= 12:
        raise HTTPException(422, "Competência inválida")
    allowed = allowed_person_ids(user, db)
    writable = writable_person_ids(user, db)
    if allowed is not None and (writable is None or set(allowed) != set(writable)):
        raise HTTPException(403, "Fechar o mês exige escrita em todas as áreas financeiras")
    period = db.scalar(select(AccountingPeriod).where(AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == year, AccountingPeriod.month == month))
    if not period:
        period = AccountingPeriod(workspace_id=workspace_id, year=year, month=month)
        db.add(period)
    period.is_closed, period.closed_at, period.closed_by_id = True, datetime.utcnow(), user.id
    db.commit()
    return {"closed": True}


@router.post("/months/{year}/{month}/reopen")
def reopen_month(year: int, month: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    allowed = allowed_person_ids(user, db)
    writable = writable_person_ids(user, db)
    if allowed is not None and (writable is None or set(allowed) != set(writable)):
        raise HTTPException(403, "Reabrir o mês exige escrita em todas as áreas financeiras")
    period = db.scalar(select(AccountingPeriod).where(AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == year, AccountingPeriod.month == month))
    if period:
        period.is_closed, period.closed_at, period.closed_by_id = False, None, None
        db.commit()
    return {"reopened": True}


@router.post("/budgets/{budget_id}/release")
def release_budget(budget_id: int, year: int = Query(..., ge=2000, le=2200),
                   month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    budget = db.scalar(select(MonthlyBudget).where(MonthlyBudget.id == budget_id,
        MonthlyBudget.workspace_id == workspace_id, MonthlyBudget.is_active.is_(True)))
    if not budget:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, budget.person_id, write=True)
    occurrence = db.scalar(select(MonthlyBudgetOccurrence).where(
        MonthlyBudgetOccurrence.monthly_budget_id == budget.id,
        MonthlyBudgetOccurrence.year == year, MonthlyBudgetOccurrence.month == month))
    position = next((position for position in monthly_budget_positions(
        db, workspace_ids=(workspace_id,), person_ids=(budget.person_id,), year=year, month=month,
    ) if position.budget.id == budget.id), None)
    if not position:
        raise HTTPException(404, "Orçamento não ativo nesta competência")
    if position.is_released:
        raise HTTPException(409, "A reserva deste mês já foi liberada")
    amount = max(Decimal(position.remaining), Decimal(0))
    if occurrence:
        occurrence.status, occurrence.released_amount = "released", amount
        occurrence.released_at, occurrence.released_by_id = datetime.utcnow(), user.id
    else:
        db.add(MonthlyBudgetOccurrence(monthly_budget_id=budget.id, year=year, month=month,
            status="released", released_amount=amount, released_at=datetime.utcnow(), released_by_id=user.id))
    db.commit()
    return {"released": True}


@router.post("/budgets/{budget_id}/reactivate")
def reactivate_budget(budget_id: int, year: int = Query(..., ge=2000, le=2200),
                      month: int = Query(..., ge=1, le=12), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    budget = db.scalar(select(MonthlyBudget).where(MonthlyBudget.id == budget_id,
        MonthlyBudget.workspace_id == workspace_id, MonthlyBudget.is_active.is_(True)))
    if not budget:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, budget.person_id, write=True)
    occurrence = db.scalar(select(MonthlyBudgetOccurrence).where(
        MonthlyBudgetOccurrence.monthly_budget_id == budget.id,
        MonthlyBudgetOccurrence.year == year, MonthlyBudgetOccurrence.month == month))
    if occurrence:
        db.delete(occurrence)
        db.commit()
    return {"reactivated": True}


@router.get("/financings")
def list_financings(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    query = select(Financing).options(joinedload(Financing.person)).where(Financing.workspace_id == workspace_id)
    visible = _visible_area_ids(user, db)
    if selected is not None:
        query = query.where(Financing.person_id == selected)
    elif visible is not None:
        query = query.where(Financing.person_id.in_(visible)) if visible else query.where(False)
    rows = db.scalars(query.order_by(Financing.status, Financing.description)).unique().all()
    return {"items": [{"id": x.id, "description": x.description, "contract_type": x.contract_type,
        "person_id": x.person_id, "person_name": x.person.name if x.person else None,
        "paid_installments": x.paid_installments, "total_installments": x.total_installments,
        "installment_amount": _money(x.installment_amount), "financed_amount": _money(x.financed_amount),
        "outstanding_balance": _money(x.outstanding_balance),
        "nominal_interest_rate": str(x.nominal_interest_rate) if x.nominal_interest_rate is not None else None,
        "institution": x.institution, "due_day": x.due_day, "status": x.status,
        "remaining_installments": max(0, x.total_installments-x.paid_installments)} for x in rows]}


@router.post("/financings", status_code=status.HTTP_201_CREATED)
def create_financing(payload: FinancingPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    if payload.paid_installments >= payload.total_installments:
        raise HTTPException(422, "As parcelas pagas devem ser menores que o total")
    account = _account(db, user, workspace_id, payload.account_id, write=True)
    if account and account.person_id != payload.person_id:
        raise HTTPException(403, "A conta deve pertencer à área financeira do contrato")
    category = db.get(Category, payload.category_id) if payload.category_id else None
    if category and (category.workspace_id != workspace_id or category.kind != TransactionType.expense):
        raise HTTPException(422, "Categoria de despesa inválida")
    rule = RecurrenceRule(workspace_id=workspace_id, frequency="monthly", transaction_type=TransactionType.expense,
        start_date=(payload.start_date or date.today()).replace(day=1),
        description=f"{payload.description.strip()} - parcela {payload.paid_installments+1}/{payload.total_installments}",
        amount=payload.installment_amount, account_id=payload.account_id, person_id=payload.person_id,
        category_id=payload.category_id, notes=payload.notes, created_by_id=user.id, is_active=True)
    db.add(rule)
    db.flush()
    item = Financing(workspace_id=workspace_id, person_id=payload.person_id, recurrence_rule_id=rule.id,
        description=payload.description.strip(), contract_type=payload.contract_type, start_date=payload.start_date,
        paid_installments=payload.paid_installments,
        total_installments=payload.total_installments, installment_amount=payload.installment_amount,
        financed_amount=payload.financed_amount, outstanding_balance=payload.outstanding_balance,
        nominal_interest_rate=payload.nominal_interest_rate,
        institution=(payload.institution or "").strip() or None, due_day=payload.due_day,
        notes=payload.notes, status="active")
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "description": item.description, "status": item.status,
        "recurrence_rule_id": item.recurrence_rule_id}


@router.get("/financings/{financing_id}")
def financing_detail(financing_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Financing).where(Financing.id == financing_id,
        Financing.workspace_id == workspace_id))
    if not item:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, item.person_id, write=False)
    history = db.scalars(select(FinancingAmortization).where(
        FinancingAmortization.financing_id == item.id).order_by(
            FinancingAmortization.amortization_date.desc(), FinancingAmortization.id.desc())).all()
    return {"financing": {"id": item.id, "description": item.description,
        "contract_type": item.contract_type, "institution": item.institution,
        "financed_amount": _money(item.financed_amount), "nominal_interest_rate": str(item.nominal_interest_rate) if item.nominal_interest_rate is not None else None,
        "due_day": item.due_day, "start_date": item.start_date.isoformat() if item.start_date else None,
        "notes": item.notes,
        "person_id": item.person_id, "paid_installments": item.paid_installments,
        "total_installments": item.total_installments, "installment_amount": _money(item.installment_amount),
        "outstanding_balance": _money(item.outstanding_balance), "status": item.status},
        "history": [{"id": h.id, "date": h.amortization_date.isoformat(),
            "amortized_amount": _money(h.amortized_amount), "new_balance": _money(h.new_outstanding_balance),
            "strategy": h.strategy, "notes": h.notes} for h in history]}


@router.post("/financings/{financing_id}/amortizations", status_code=status.HTTP_201_CREATED)
def amortize_financing(financing_id: int, payload: AmortizationPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Financing).where(Financing.id == financing_id,
        Financing.workspace_id == workspace_id, Financing.status == "active"))
    if not item:
        raise HTTPException(404)
    _require_area(user, workspace_id, db, item.person_id, write=True)
    if payload.strategy in ("reduce_term", "reduce_both") and payload.remaining_installments is None:
        raise HTTPException(422, "Informe as parcelas restantes")
    if payload.strategy in ("reduce_installment", "reduce_both") and payload.new_installment_amount is None:
        raise HTTPException(422, "Informe o novo valor da parcela")
    remaining = payload.remaining_installments if payload.remaining_installments is not None else item.total_installments-item.paid_installments
    installment = payload.new_installment_amount or item.installment_amount
    total = item.paid_installments + remaining
    history = FinancingAmortization(financing_id=item.id, user_id=user.id,
        amortization_date=payload.amortization_date, amortized_amount=payload.amortized_amount,
        previous_outstanding_balance=item.outstanding_balance, new_outstanding_balance=payload.new_outstanding_balance,
        previous_total_installments=item.total_installments, new_total_installments=total,
        previous_installment_amount=item.installment_amount, new_installment_amount=installment,
        strategy=payload.strategy, source="mobile", notes=payload.notes)
    db.add(history)
    item.outstanding_balance, item.total_installments, item.installment_amount = payload.new_outstanding_balance, total, installment
    rule = db.get(RecurrenceRule, item.recurrence_rule_id) if item.recurrence_rule_id else None
    if remaining == 0 or payload.new_outstanding_balance == 0:
        item.status = "paid"
        if rule: rule.is_active = False
    elif rule:
        rule.amount = installment
        rule.description = f"{item.description} - parcela {item.paid_installments+1}/{total}"
    db.commit()
    return {"saved": True, "status": item.status, "remaining_installments": max(0, total-item.paid_installments)}


@router.get("/vehicles")
def list_vehicles(context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    vehicles = db.scalars(select(Vehicle).where(Vehicle.workspace_id == workspace_id).order_by(
        Vehicle.is_active.desc(), Vehicle.name)).all()
    fillups = db.scalars(select(FuelFillup).options(joinedload(FuelFillup.vehicle)).where(
        FuelFillup.workspace_id == workspace_id).order_by(FuelFillup.fillup_date.desc(), FuelFillup.id.desc()).limit(100)).all()
    return {"vehicles": [{"id": x.id, "name": x.name, "year": x.year, "initial_odometer_km": str(x.initial_odometer_km),
        "fuel_type": x.fuel_type, "is_active": x.is_active} for x in vehicles],
        "fillups": [{"id": x.id, "vehicle_id": x.vehicle_id, "vehicle_name": x.vehicle.name,
            "transaction_id": x.transaction_id, "date": x.fillup_date.isoformat(),
            "odometer_km": str(x.odometer_km), "liters": str(x.liters),
            "price_per_liter": str(x.price_per_liter), "total": _money(Decimal(x.liters)*Decimal(x.price_per_liter)),
            "is_full_tank": x.is_full_tank, "station": x.station} for x in fillups]}


@router.post("/vehicles", status_code=status.HTTP_201_CREATED)
def create_vehicle(payload: VehiclePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = Vehicle(workspace_id=workspace_id, name=payload.name.strip(), year=payload.year,
        initial_odometer_km=payload.initial_odometer_km, fuel_type=payload.fuel_type)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name, "is_active": item.is_active}


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, payload: VehicleUpdatePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.workspace_id == workspace_id))
    if not item:
        raise HTTPException(404, "Veículo não encontrado")
    latest_odometer = db.scalar(select(FuelFillup.odometer_km).where(
        FuelFillup.vehicle_id == item.id).order_by(FuelFillup.odometer_km.desc()))
    if latest_odometer is not None and payload.initial_odometer_km > latest_odometer:
        raise HTTPException(422, "A quilometragem inicial não pode superar um abastecimento registrado")
    item.name = payload.name.strip()
    item.year = payload.year
    item.initial_odometer_km = payload.initial_odometer_km
    item.fuel_type = payload.fuel_type
    item.is_active = payload.is_active
    db.commit()
    return {"id": item.id, "name": item.name, "is_active": item.is_active}


@router.delete("/vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_vehicle(vehicle_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.workspace_id == workspace_id))
    if not item:
        raise HTTPException(404, "Veículo não encontrado")
    item.is_active = False
    db.commit()
    return None


@router.post("/fuel-fillups", status_code=status.HTTP_201_CREATED)
def create_fuel_fillup(payload: FuelPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    vehicle = db.scalar(select(Vehicle).where(Vehicle.id == payload.vehicle_id,
        Vehicle.workspace_id == workspace_id, Vehicle.is_active.is_(True)))
    if not vehicle:
        raise HTTPException(404, "Veículo não encontrado")
    transaction = None
    if payload.transaction_id:
        transaction = db.scalar(select(Transaction).where(Transaction.id == payload.transaction_id,
            Transaction.workspace_id == workspace_id, Transaction.transaction_type == TransactionType.expense))
        if not transaction:
            raise HTTPException(404, "Despesa vinculada não encontrada")
        _require_area(user, workspace_id, db, transaction.person_id, write=True)
    duplicate = db.scalar(select(FuelFillup.id).where(FuelFillup.transaction_id == payload.transaction_id)) if payload.transaction_id else None
    if duplicate:
        raise HTTPException(409, "Este lançamento já tem abastecimento associado")
    item = FuelFillup(workspace_id=workspace_id, vehicle_id=vehicle.id, transaction_id=payload.transaction_id,
        fillup_date=payload.fillup_date, odometer_km=payload.odometer_km, liters=payload.liters,
        price_per_liter=payload.price_per_liter, is_full_tank=payload.is_full_tank,
        station=payload.station, notes=payload.notes)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "total": _money(Decimal(item.liters)*Decimal(item.price_per_liter))}


@router.post("/profile/password")
def update_password(payload: PasswordPayload, context=Depends(_mobile_context)):
    user, session, db = context
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(400, "A senha atual está incorreta")
    user.password_hash = hash_password(payload.new_password)
    session.revoked_at = datetime.utcnow()
    db.commit()
    return {"changed": True, "session_revoked": True}


@router.post("/areas", status_code=status.HTTP_201_CREATED)
def create_area(payload: AreaPayload, context=Depends(_mobile_context)):
    user, _, db = context
    if not (user.is_super_admin or user.account_role.value == "admin"):
        raise HTTPException(403, "Somente administradores podem criar áreas financeiras")
    workspace_id = _workspace_id(db, user)
    area = Person(workspace_id=workspace_id, name=payload.name.strip(), person_type=payload.person_type)
    db.add(area)
    db.flush()
    if not user.default_person_id:
        user.default_person_id = area.id
    db.commit()
    db.refresh(area)
    return {"id": area.id, "name": area.name, "person_type": area.person_type.value, "can_write": True}


@router.post("/areas/{area_id}/default")
def set_default_area(area_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, area_id, write=False)
    user.default_person_id = area_id
    db.commit()
    return {"default_person_id": area_id}


@router.get("/analysis/summary")
def analysis_summary(area_id: int | None = None,
                     history: int = Query(default=12, ge=3, le=24),
                     horizon: int = Query(default=12, ge=3, le=60), context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    visible = _visible_area_ids(user, db)
    query = select(Transaction).options(joinedload(Transaction.category)).where(
        Transaction.workspace_id == workspace_id, Transaction.status == TransactionStatus.paid,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    )
    if selected is not None:
        query = query.where(Transaction.person_id == selected)
    elif visible is not None:
        query = query.where(Transaction.person_id.in_(visible)) if visible else query.where(False)
    items = db.scalars(query).unique().all()
    today = date.today().replace(day=1)
    periods = []
    for offset in range(history - 1, -1, -1):
        index = today.year * 12 + today.month - 1 - offset
        year, month = index // 12, index % 12 + 1
        periods.append((year, month))
    totals = {key: {"income": Decimal(0), "expense": Decimal(0)} for key in periods}
    for item in items:
        key = (item.competence_year or item.transaction_date.year,
               item.competence_month or item.transaction_date.month)
        if key in totals:
            totals[key][item.transaction_type.value] += Decimal(item.amount)
    monthly = [{"year": y, "month": m, "income": _money(totals[(y,m)]["income"]),
                "expense": _money(totals[(y,m)]["expense"])} for y,m in periods]
    incomes = sum((row["income"] for row in totals.values()), Decimal(0))
    expenses = sum((row["expense"] for row in totals.values()), Decimal(0))
    average_income = incomes / max(len(periods), 1)
    average_expense = expenses / max(len(periods), 1)
    category_totals: dict[str, Decimal] = {}
    for item in items:
        key = (item.competence_year or item.transaction_date.year,
               item.competence_month or item.transaction_date.month)
        if key in totals and item.transaction_type == TransactionType.expense:
            name = item.category.name if item.category else "Sem categoria"
            category_totals[name] = category_totals.get(name, Decimal(0)) + Decimal(item.amount)
    balance_start = Decimal(0)
    account_query = select(Account).where(Account.workspace_id == workspace_id)
    if selected is not None:
        account_query = account_query.where(Account.person_id == selected)
    elif visible is not None:
        account_query = account_query.where(Account.person_id.in_(visible)) if visible else account_query.where(False)
    balance_start += sum((Decimal(a.initial_balance or 0) for a in db.scalars(account_query).all()), Decimal(0))
    past = db.scalars(select(Transaction).where(Transaction.workspace_id == workspace_id,
        Transaction.status == TransactionStatus.paid,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]))).all()
    for item in past:
        if selected is not None and item.person_id != selected: continue
        if visible is not None and item.person_id not in visible: continue
        if (item.competence_year or item.transaction_date.year, item.competence_month or item.transaction_date.month) < periods[0]:
            balance_start += Decimal(item.amount) if item.transaction_type == TransactionType.income else -Decimal(item.amount)
    projections = []
    projected_balance = balance_start
    for offset in range(history):
        period = periods[offset]
        projected_balance += totals[period]["income"] - totals[period]["expense"]
    for offset in range(horizon):
        index = today.year * 12 + today.month + offset
        year, month = index // 12, index % 12 + 1
        projected_balance += average_income - average_expense
        projections.append({"year": year, "month": month, "balance": _money(projected_balance)})
    return {"history_months": history, "horizon_months": horizon,
        "income_total": _money(incomes), "expense_total": _money(expenses),
        "credit_expense_total": _money(sum((Decimal(x.amount) for x in items if x.transaction_type == TransactionType.expense and x.card_id), Decimal(0))),
        "average_income": _money(average_income), "average_expense": _money(average_expense),
        "savings_rate": float((incomes-expenses)/incomes*100) if incomes else 0,
        "monthly": monthly, "top_categories": [{"name": k, "total": _money(v)} for k,v in sorted(category_totals.items(), key=lambda x:x[1], reverse=True)[:10]],
        "projection": projections}


@router.get("/goals")
def list_goals(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    query = select(FinancialGoal).where(FinancialGoal.user_id == user.id,
        FinancialGoal.workspace_id == workspace_id)
    if selected is not None:
        query = query.where(FinancialGoal.person_id == selected)
    rows = db.scalars(query.order_by(FinancialGoal.status, FinancialGoal.end_date)).all()
    return {"items": [{"id": g.id, "person_id": g.person_id, "goal_type": g.goal_type,
        "title": g.title, "sentence": g.sentence, "target_amount": _money(g.target_amount),
        "saved_amount": _money(g.saved_amount), "weekly_amount": _money(g.weekly_amount),
        "days_per_week": g.days_per_week, "hour_label": g.hour_label,
        "start_date": g.start_date.isoformat(), "end_date": g.end_date.isoformat(),
        "weeks": g.weeks, "status": g.status, "is_tight": g.is_tight} for g in rows]}


@router.post("/goals", status_code=status.HTTP_201_CREATED)
def create_goal(payload: GoalPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    if payload.end_date <= payload.start_date:
        raise HTTPException(422, "A data final precisa ser posterior à data inicial")
    if payload.goal_type == "purchase" and (payload.target_amount is None or payload.weeks < 1):
        raise HTTPException(422, "Informe valor alvo e prazo para a meta financeira")
    weekly = (payload.target_amount / payload.weeks).quantize(Decimal("0.01")) if payload.target_amount else None
    item = FinancialGoal(workspace_id=workspace_id, person_id=payload.person_id, user_id=user.id,
        goal_type=payload.goal_type, title=payload.title.strip(), sentence=payload.sentence.strip(),
        target_amount=payload.target_amount, weekly_amount=weekly, days_per_week=payload.days_per_week,
        hour_label=payload.hour_label, start_date=payload.start_date, end_date=payload.end_date,
        weeks=payload.weeks, is_tight=payload.is_tight, status="active")
    db.add(item); db.commit(); db.refresh(item)
    return {"id": item.id, "status": item.status, "weekly_amount": _money(item.weekly_amount)}


@router.post("/goals/{goal_id}/progress")
def add_goal_progress(goal_id: int, payload: GoalProgressPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    item = db.scalar(select(FinancialGoal).where(FinancialGoal.id == goal_id,
        FinancialGoal.user_id == user.id, FinancialGoal.workspace_id == workspace_id))
    if not item:
        raise HTTPException(404, "Meta não encontrada")
    _require_area(user, workspace_id, db, item.person_id, write=True)
    if item.goal_type != "purchase" or item.status != "active":
        raise HTTPException(422, "Esta meta não aceita aportes")
    item.saved_amount = min(Decimal(item.target_amount or 0), Decimal(item.saved_amount or 0) + payload.amount)
    if item.saved_amount >= Decimal(item.target_amount or 0):
        item.status = "completed"
    db.commit()
    return {"id": item.id, "saved_amount": _money(item.saved_amount), "status": item.status}


AUDIO_PENDING_TIMEOUT = timedelta(minutes=10)


def _expire_stale_audio(db: Session, user: User) -> None:
    """A job lost to a server restart would stay pending forever; let the user retry it instead."""
    stale = db.scalars(select(TelegramConversationMessage).where(
        TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.status == "pending",
        TelegramConversationMessage.created_at < datetime.utcnow() - AUDIO_PENDING_TIMEOUT)).all()
    for item in stale:
        item.status = "error"
    if stale:
        db.commit()


HISTORY_PAGE_DEFAULT = 20
HISTORY_PAGE_MAX = 100
HISTORY_CHANNELS = ["telegram", "app"]


@router.get("/assistant/history")
def assistant_history(before_id: int | None = Query(default=None, ge=1),
                      limit: int | None = Query(default=None, ge=1, le=HISTORY_PAGE_MAX),
                      context=Depends(_mobile_context)):
    """Without parameters: the same window the model sees (last N done messages within the char budget,
    plus anything still processing). With ``before_id``/``limit``: an older page, newest of that page last.
    ``memory_start_id`` is the oldest message the model still remembers."""
    user, _, db = context
    _expire_stale_audio(db, user)
    window = model_window(db, user.id)
    memory_start_id = window[0].id if window else None
    base = select(TelegramConversationMessage).where(
        TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.channel.in_(HISTORY_CHANNELS))
    if before_id is None and limit is None and memory_start_id is not None:
        rows = db.scalars(base.where(TelegramConversationMessage.id >= memory_start_id)
                          .order_by(TelegramConversationMessage.id.asc())).all()
    else:
        query = base if before_id is None else base.where(TelegramConversationMessage.id < before_id)
        rows = list(reversed(db.scalars(query.order_by(TelegramConversationMessage.id.desc())
                                        .limit(limit or HISTORY_PAGE_DEFAULT)).all()))
    has_more = bool(rows) and db.scalar(select(TelegramConversationMessage.id).where(
        TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.channel.in_(HISTORY_CHANNELS),
        TelegramConversationMessage.id < rows[0].id).limit(1)) is not None
    ids = [item.id for item in rows]
    media_by_message: dict[int, list[dict]] = {}
    if ids:
        for media in db.scalars(select(ChatMedia).where(
            ChatMedia.user_id == user.id, ChatMedia.message_id.in_(ids)
        ).order_by(ChatMedia.id)).all():
            media_by_message.setdefault(media.message_id, []).append({
                "id": media.id, "kind": media.kind, "mime_type": media.mime_type,
                "transcript": media.transcript, "url": f"/assistant/media/{media.id}",
                "played": media.played_at is not None,
            })

    def shown(item):
        kinds = {x["kind"] for x in media_by_message.get(item.id, [])}
        if "audio_user" in kinds:
            return "\u00c1udio enviado."
        if "receipt_image" in kinds:
            return "Comprovante enviado."
        return item.content

    return {"items": [{"id": item.id, "role": item.role, "status": item.status, "client_key": item.client_key,
        "channel": item.channel, "content": shown(item), "created_at": item.created_at.isoformat(),
        "media": media_by_message.get(item.id, [])} for item in rows],
        "has_more": has_more, "memory_start_id": memory_start_id}


@router.post("/assistant/media/{media_id}/played")
def assistant_media_played(media_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    media = db.scalar(select(ChatMedia).where(ChatMedia.id == media_id, ChatMedia.user_id == user.id,
        ChatMedia.kind == "audio_response"))
    if not media:
        raise HTTPException(404, "M\u00eddia n\u00e3o encontrada")
    if media.played_at is None:
        media.played_at = datetime.utcnow()
        db.commit()
    return {"id": media.id, "played": True}


def _attach_private_media(db: Session, user: User, message: TelegramConversationMessage,
                          kind: str, data: bytes, filename: str, mime_type: str,
                          transcript: str | None = None) -> ChatMedia:
    key = store_bytes(data, mime_type)
    media = ChatMedia(user_id=user.id, message_id=message.id, kind=kind, storage_key=key,
        filename=filename[:180], mime_type=mime_type, transcript=transcript)
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _latest_assistant_message(db: Session, user: User, after_id: int) -> TelegramConversationMessage | None:
    return db.scalar(select(TelegramConversationMessage).where(
        TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.channel == "app",
        TelegramConversationMessage.role == "assistant", TelegramConversationMessage.id > after_id,
    ).order_by(TelegramConversationMessage.id.desc()))


def _add_generated_audio(db: Session, user: User, assistant: TelegramConversationMessage | None) -> None:
    if assistant is None:
        return
    try:
        generated = synthesize_speech(db, assistant.content)
        if generated:
            audio_bytes, mime_type = generated
            extension = ".wav" if mime_type == "audio/wav" else ".mp3"
            _attach_private_media(db, user, assistant, "audio_response", audio_bytes,
                f"resposta{extension}", mime_type, assistant.content)
    except MediaProcessingError:
        # A resposta escrita deve continuar disponível se o serviço de voz falhar.
        db.rollback()


@router.get("/assistant/media/{media_id}")
def assistant_media(media_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    media = db.scalar(select(ChatMedia).where(ChatMedia.id == media_id, ChatMedia.user_id == user.id))
    if not media:
        raise HTTPException(404, "Mídia não encontrada")
    try:
        data = read_private_media(media.storage_key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Mídia não encontrada") from exc
    return Response(content=data, media_type=media.mime_type,
        headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})


def _check_upload(data: bytes, filename: str, mime_type: str, allowed_prefix: str) -> None:
    if not data:
        raise HTTPException(422, "O arquivo enviado está vazio")
    if len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(413, "O arquivo excede o limite de 12 MB")
    if not mime_type.startswith(allowed_prefix):
        raise HTTPException(415, "Formato de mídia não suportado")
    if mime_type not in {"audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/aac", "audio/ogg", "audio/webm", "image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Use áudio M4A, MP3, WAV, AAC, OGG ou WebM; para imagem, JPEG, PNG ou WebP")


def setting_exists(db: Session, key: str) -> bool:
    return bool(db.scalar(select(SystemSetting.value).where(SystemSetting.key == key)))


_USER_JOB_LOCKS: dict[int, threading.Lock] = {}
_USER_JOB_LOCKS_GUARD = threading.Lock()


def _user_job_lock(user_id: int) -> threading.Lock:
    """One audio job at a time per user: the agent keeps per-user pending actions and conversation order."""
    with _USER_JOB_LOCKS_GUARD:
        return _USER_JOB_LOCKS.setdefault(user_id, threading.Lock())


MEDIA_KINDS = ("audio_user", "receipt_image")
UPLOAD_LABELS = {"audio_user": "\u00c1udio enviado.", "receipt_image": "Comprovante enviado."}


def _media_job_json(message: TelegramConversationMessage, duplicate: bool = False) -> dict:
    return {"id": message.id, "status": message.status, "client_key": message.client_key, "duplicate": duplicate}


def _run_media_job(bind, user_id: int, message_id: int) -> None:
    """Background: read the audio/image, answer with the agent, synthesize the reply (audio only), mark done."""
    with _user_job_lock(user_id):
        db = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)()
        agent_finished = False
        try:
            user = db.get(User, user_id)
            message = db.get(TelegramConversationMessage, message_id)
            if not user or not message or message.status != "pending":
                return
            media = db.scalar(select(ChatMedia).where(ChatMedia.message_id == message.id, ChatMedia.kind.in_(MEDIA_KINDS)))
            if not media:
                raise MediaProcessingError("Anexo n\u00e3o encontrado")
            if media.kind == "audio_user":
                if not media.transcript:
                    media.transcript = transcribe_audio(db, read_private_media(media.storage_key), media.filename, media.mime_type)
                    db.commit()
                transcript = media.transcript
                prompt = (f"O usu\u00e1rio enviou um \u00e1udio. Transcri\u00e7\u00e3o: {transcript}. Responda ao pedido e use as ferramentas "
                    "financeiras quando necess\u00e1rio; confirme qualquer inclus\u00e3o ou altera\u00e7\u00e3o antes de efetiv\u00e1-la.")
                stored = f"\u00c1udio enviado. Transcri\u00e7\u00e3o: {transcript}"
            else:
                if not media.transcript:
                    extracted = describe_receipt(db, read_private_media(media.storage_key), media.mime_type)
                    media.transcript = f"{IMAGE_TRANSCRIPT_PREFIX}{extracted}"
                    db.commit()
                extracted = media.transcript.removeprefix(IMAGE_TRANSCRIPT_PREFIX)
                prompt = ("O usu\u00e1rio enviou uma imagem de comprovante para ajudar a registrar uma despesa. "
                    f"Dados extra\u00eddos, que devem ser confirmados pelo usu\u00e1rio: {extracted}. "
                    "Apresente os dados de forma clara, pergunte o que estiver ausente e nunca registre sem mostrar o resumo e receber confirma\u00e7\u00e3o expl\u00edcita.")
                stored = f"Comprovante enviado.\n{media.transcript}"
            from app.services.telegram_agent import run_financial_agent
            run_financial_agent(db, user, prompt[:4000], channel="app", user_message=message, stored_user_content=stored)
            agent_finished = True
            if media.kind == "audio_user":
                _add_generated_audio(db, user, _latest_assistant_message(db, user, message.id))
            message.status = "done"
            db.commit()
        except Exception:
            logger.exception("media_job_failed user_id=%s message_id=%s", user_id, message_id)
            db.rollback()
            try:
                row = db.get(TelegramConversationMessage, message_id)
                if row is not None:
                    # Once the agent has answered, the reply exists: never reprocess it (would repeat actions).
                    row.status = "done" if agent_finished else "error"
                    db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()


def _accept_media_upload(background: BackgroundTasks, db: Session, user: User, kind: str, allowed_prefix: str,
                         file: UploadFile, idempotency_key: str | None) -> dict:
    """Saves the user's message and file right away and processes them in the background (202)."""
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 64:
        raise HTTPException(422, "Idempotency-Key muito longa")
    if key:
        existing = db.scalar(select(TelegramConversationMessage).where(
            TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.client_key == key))
        if existing:
            if existing.status == "error":
                existing.status = "pending"
                db.commit()
                background.add_task(_run_media_job, db.get_bind(), user.id, existing.id)
            return _media_job_json(existing, duplicate=True)
    mime_type = (file.content_type or "").lower()
    data = file.file.read(MAX_MEDIA_BYTES + 1)
    _check_upload(data, file.filename or "anexo", mime_type, allowed_prefix)
    message = TelegramConversationMessage(user_id=user.id, role="user", channel="app", status="pending",
        content=UPLOAD_LABELS[kind], client_key=key)
    db.add(message)
    try:
        db.flush()
    except IntegrityError:  # the same key arrived twice at once: the first request owns the message
        db.rollback()
        existing = db.scalar(select(TelegramConversationMessage).where(
            TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.client_key == key))
        if existing:
            return _media_job_json(existing, duplicate=True)
        raise
    default_name = "audio.m4a" if kind == "audio_user" else "imagem.jpg"
    _attach_private_media(db, user, message, kind, data, file.filename or default_name, mime_type)
    background.add_task(_run_media_job, db.get_bind(), user.id, message.id)
    return _media_job_json(message)


@router.post("/assistant/media/audio", status_code=202)
def assistant_audio_message(background: BackgroundTasks, file: UploadFile = File(...),
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                            context=Depends(_mobile_context)):
    user, _, db = context
    return _accept_media_upload(background, db, user, "audio_user", "audio/", file, idempotency_key)


@router.post("/assistant/media/image", status_code=202)
def assistant_image_message(background: BackgroundTasks, file: UploadFile = File(...),
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                            context=Depends(_mobile_context)):
    user, _, db = context
    return _accept_media_upload(background, db, user, "receipt_image", "image/", file, idempotency_key)


@router.post("/assistant/messages/{message_id}/retry", status_code=202)
def assistant_media_retry(message_id: int, background: BackgroundTasks, context=Depends(_mobile_context)):
    user, _, db = context
    message = db.scalar(select(TelegramConversationMessage).where(
        TelegramConversationMessage.id == message_id, TelegramConversationMessage.user_id == user.id,
        TelegramConversationMessage.channel == "app", TelegramConversationMessage.role == "user"))
    if not message or not db.scalar(select(ChatMedia.id).where(
            ChatMedia.message_id == message.id, ChatMedia.kind.in_(MEDIA_KINDS))):
        raise HTTPException(404, "Mensagem n\u00e3o encontrada")
    if message.status == "error":
        message.status = "pending"
        db.commit()
        background.add_task(_run_media_job, db.get_bind(), user.id, message.id)
    return _media_job_json(message)


@router.post("/assistant/messages")
def assistant_message(payload: ChatMessage, context=Depends(_mobile_context)):
    user, _, db = context
    from app.services.telegram_agent import run_financial_agent
    before_id = db.scalar(select(TelegramConversationMessage.id).where(
        TelegramConversationMessage.user_id == user.id).order_by(TelegramConversationMessage.id.desc())) or 0
    try:
        reply = run_financial_agent(db, user, payload.message.strip(), channel="app")
    except Exception as exc:
        db.rollback()
        raise HTTPException(503, "A Vitoria IA está temporariamente indisponível") from exc
    return {"reply": reply}


@router.get("/profile/memories")
def list_memories(context=Depends(_mobile_context)):
    user, _, db = context
    rows = db.scalars(select(UserMemory).where(UserMemory.user_id == user.id,
        UserMemory.is_active.is_(True)).order_by(UserMemory.updated_at.desc())).all()
    return {"items": [{"id": row.id, "summary": row.summary, "type": row.memory_type,
        "updated_at": row.updated_at.isoformat()} for row in rows]}


@router.delete("/profile/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    row = db.scalar(select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user.id))
    if not row:
        raise HTTPException(404)
    row.is_active = False
    db.commit()
    return None
