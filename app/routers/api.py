"""Versioned JSON API used by the native mobile application."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import allowed_person_ids, writable_person_ids
from app.models import (
    Account, AccountRole, AccountingPeriod, Category, Card, MobileApiSession, MonthlyBudget,
    Person, RecurrenceOccurrence, RecurrenceRule, Transaction, TransactionStatus, TransactionType, User,
    WorkspaceMember,
)
from app.security import verify_password
from app.services.card_billing import card_purchase_competence
from app.services.monthly_projection import monthly_budget_positions, projection_budget_remaining

router = APIRouter(prefix="/api/v1", tags=["mobile-api"])
bearer = HTTPBearer(auto_error=False)
SESSION_DAYS = 30


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
    device_name: str | None = Field(default=None, max_length=120)


class TransactionPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_type: TransactionType
    description: str = Field(min_length=1, max_length=180)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    transaction_date: date
    status: TransactionStatus = TransactionStatus.paid
    person_id: int | None = None
    account_id: int | None = None
    destination_account_id: int | None = None
    card_id: int | None = None
    category_id: int | None = None
    payment_method: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=4000)


class BudgetCreatePayload(BaseModel):
    person_id: int
    category_id: int
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    start_year: int = Field(ge=2000, le=2200)
    start_month: int = Field(ge=1, le=12)
    include_in_projection: bool = True


class BudgetUpdatePayload(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    start_year: int = Field(ge=2000, le=2200)
    start_month: int = Field(ge=1, le=12)
    include_in_projection: bool = True


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _workspace_id(db: Session, user: User) -> int:
    membership = db.scalar(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.id)
    )
    if not membership:
        raise HTTPException(status_code=403, detail="Usuário sem workspace")
    return membership.workspace_id


def _visible_area_ids(user: User, db: Session) -> set[int] | None:
    allowed = allowed_person_ids(user, db)
    return None if allowed is None else set(allowed)


def _transaction_area_filter(user: User, workspace_id: int, db: Session, area_id: int | None):
    visible = _visible_area_ids(user, db)
    if area_id is not None:
        account_ids = select(Account.id).where(
            Account.workspace_id == workspace_id, Account.person_id == area_id,
        )
        return or_(
            Transaction.person_id == area_id,
            and_(Transaction.transaction_type == TransactionType.transfer,
                 or_(Transaction.account_id.in_(account_ids), Transaction.destination_account_id.in_(account_ids))),
        )
    if visible is None:
        return True
    if not visible:
        return False
    account_ids = select(Account.id).where(
        Account.workspace_id == workspace_id, Account.person_id.in_(visible),
    )
    return or_(
        Transaction.person_id.in_(visible),
        and_(Transaction.transaction_type == TransactionType.transfer,
             or_(Transaction.account_id.in_(account_ids), Transaction.destination_account_id.in_(account_ids))),
    )


def _areas(user: User, workspace_id: int, db: Session) -> list[Person]:
    query = select(Person).where(Person.workspace_id == workspace_id, Person.is_active.is_(True))
    allowed = _visible_area_ids(user, db)
    if allowed is not None:
        query = query.where(Person.id.in_(allowed)) if allowed else query.where(False)
    return list(db.scalars(query.order_by(Person.name)).all())


def _selected_area(user: User, workspace_id: int, db: Session, area_id: int | None) -> int | None:
    areas = _areas(user, workspace_id, db)
    allowed = {area.id for area in areas}
    if area_id is not None:
        if area_id not in allowed:
            raise HTTPException(status_code=403, detail="Área financeira não permitida")
        return area_id
    if user.default_person_id in allowed:
        return user.default_person_id
    return areas[0].id if areas else None


def _require_area(user: User, workspace_id: int, db: Session, area_id: int | None, *, write: bool):
    if area_id is None:
        if write:
            raise HTTPException(status_code=422, detail="Informe a área financeira do lançamento")
        return
    area = db.scalar(select(Person).where(
        Person.id == area_id, Person.workspace_id == workspace_id, Person.is_active.is_(True),
    ))
    allowed = _visible_area_ids(user, db)
    if not area or (allowed is not None and area_id not in allowed):
        raise HTTPException(status_code=403, detail="Área financeira não permitida")
    if write:
        writable = writable_person_ids(user, db)
        if writable is not None and area_id not in writable:
            raise HTTPException(status_code=403, detail="Esta área está disponível apenas para leitura")


def _account(db: Session, user: User, workspace_id: int, account_id: int | None, *, write: bool) -> Account | None:
    if account_id is None:
        return None
    account = db.scalar(select(Account).where(
        Account.id == account_id, Account.workspace_id == workspace_id, Account.is_active.is_(True),
    ))
    if not account:
        raise HTTPException(status_code=422, detail="Conta bancária inválida")
    if account.person_id is None and _visible_area_ids(user, db) is not None:
        raise HTTPException(status_code=403, detail="Conta bancária não permitida")
    _require_area(user, workspace_id, db, account.person_id, write=write)
    return account


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _transaction_json(item: Transaction) -> dict:
    return {
        "id": item.id,
        "transaction_type": item.transaction_type.value,
        "description": item.description,
        "amount": _money(item.amount),
        "transaction_date": item.transaction_date.isoformat(),
        "competence_year": item.competence_year,
        "competence_month": item.competence_month,
        "status": item.status.value,
        "person_id": item.person_id,
        "account_id": item.account_id,
        "destination_account_id": item.destination_account_id,
        "card_id": item.card_id,
        "category_id": item.category_id,
        "category_name": item.category.name if item.category else None,
        "account_name": item.account.name if item.account else None,
        "payment_method": item.payment_method,
        "notes": item.notes,
    }


def _mobile_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> tuple[User, MobileApiSession, Session]:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Autenticação necessária", headers={"WWW-Authenticate": "Bearer"})
    session = db.scalar(select(MobileApiSession).where(
        MobileApiSession.token_hash == _token_hash(credentials.credentials),
        MobileApiSession.revoked_at.is_(None),
        MobileApiSession.expires_at > datetime.utcnow(),
    ))
    if not session:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, session.user_id)
    if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
        session.revoked_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=401, detail="Usuário inativo", headers={"WWW-Authenticate": "Bearer"})
    return user, session, db


def _user_json(user: User, db: Session) -> dict:
    workspace_id = _workspace_id(db, user)
    areas = _areas(user, workspace_id, db)
    writable = writable_person_ids(user, db)
    writable_ids = {area.id for area in areas} if writable is None else set(writable)
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "is_admin": user.is_super_admin or user.account_role == AccountRole.admin,
        "workspace_id": workspace_id,
        "default_person_id": _selected_area(user, workspace_id, db, None),
        "areas": [
            {"id": area.id, "name": area.name, "person_type": area.person_type.value,
             "can_write": area.id in writable_ids}
            for area in areas
        ],
    }


@router.post("/auth/login")
def login(payload: LoginPayload, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    if user.system_account and not user.system_account.is_active:
        raise HTTPException(status_code=403, detail="Conta desativada")
    _workspace_id(db, user)
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(days=SESSION_DAYS)
    db.add(MobileApiSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        device_name=payload.device_name,
        expires_at=expires_at,
    ))
    db.commit()
    return {"access_token": token, "token_type": "Bearer", "expires_at": expires_at.isoformat() + "Z",
            "user": _user_json(user, db)}


@router.get("/auth/me")
def me(context=Depends(_mobile_context)):
    user, _, db = context
    return {"user": _user_json(user, db)}


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(context=Depends(_mobile_context)):
    _, session, db = context
    session.revoked_at = datetime.utcnow()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/bootstrap")
def bootstrap(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    areas = _areas(user, workspace_id, db)
    allowed = {area.id for area in areas}
    accounts_query = select(Account).where(Account.workspace_id == workspace_id, Account.is_active.is_(True))
    if _visible_area_ids(user, db) is not None:
        accounts_query = accounts_query.where(Account.person_id.in_(allowed)) if allowed else accounts_query.where(False)
    accounts = list(db.scalars(accounts_query.order_by(Account.name)).all())
    account_ids = {account.id for account in accounts}
    cards = list(db.scalars(select(Card).where(
        Card.workspace_id == workspace_id, Card.is_active.is_(True), Card.account_id.in_(account_ids),
    ).order_by(Card.name)).all()) if account_ids else []
    categories = db.scalars(select(Category).where(Category.workspace_id == workspace_id).order_by(
        Category.parent_name, Category.name
    )).all()
    writable = writable_person_ids(user, db)
    writable_ids = allowed if writable is None else set(writable)
    return {
        "user": _user_json(user, db), "selected_area_id": selected,
        "areas": [{"id": item.id, "name": item.name, "person_type": item.person_type.value,
                   "can_write": item.id in writable_ids} for item in areas],
        "accounts": [{"id": item.id, "name": item.name, "person_id": item.person_id,
                      "account_type": item.account_type.value, "initial_balance": _money(item.initial_balance),
                      "color": item.color} for item in accounts],
        "cards": [{"id": item.id, "name": item.name, "brand": item.brand,
                   "account_id": item.account_id, "credit_limit": _money(item.credit_limit),
                   "closing_day": item.closing_day, "due_day": item.due_day, "color": item.color}
                  for item in cards],
        "categories": [{"id": item.id, "name": item.name, "parent_name": item.parent_name,
                        "kind": item.kind.value, "color": item.color} for item in categories],
    }


@router.get("/areas")
def areas(context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    writable = writable_person_ids(user, db)
    writable_ids = {area.id for area in _areas(user, workspace_id, db)} if writable is None else set(writable)
    return {"items": [{"id": area.id, "name": area.name, "person_type": area.person_type.value,
                       "can_write": area.id in writable_ids}
                      for area in _areas(user, workspace_id, db)]}


@router.get("/accounts")
def accounts(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    query = select(Account).where(Account.workspace_id == workspace_id, Account.is_active.is_(True))
    visible = _visible_area_ids(user, db)
    if selected is not None:
        query = query.where(Account.person_id == selected)
    elif visible is not None:
        query = query.where(Account.person_id.in_(visible)) if visible else query.where(False)
    items = db.scalars(query.order_by(Account.name)).all()
    return {"items": [{"id": item.id, "name": item.name, "person_id": item.person_id,
                       "account_type": item.account_type.value, "initial_balance": _money(item.initial_balance),
                       "color": item.color} for item in items]}


@router.get("/categories")
def categories(kind: TransactionType | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    query = select(Category).where(Category.workspace_id == workspace_id)
    if kind:
        query = query.where(Category.kind == kind)
    items = db.scalars(query.order_by(Category.parent_name, Category.name)).all()
    return {"items": [{"id": item.id, "name": item.name, "parent_name": item.parent_name,
                       "kind": item.kind.value, "color": item.color} for item in items]}


@router.get("/dashboard")
def dashboard(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    today = date.today()
    area_filter = _transaction_area_filter(user, workspace_id, db, selected)
    account_filter = Account.person_id == selected if selected is not None else True
    allowed = _visible_area_ids(user, db)
    if selected is None and allowed is not None:
        account_filter = Account.person_id.in_(allowed) if allowed else False

    accounts_initial = db.scalar(select(func.coalesce(func.sum(Account.initial_balance), 0)).where(
        Account.workspace_id == workspace_id, account_filter,
    )) or Decimal(0)
    paid_balance = db.scalar(select(func.coalesce(func.sum(case(
        (Transaction.transaction_type == TransactionType.income, Transaction.amount),
        (Transaction.transaction_type == TransactionType.expense, -Transaction.amount), else_=0,
    )), 0)).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.status == TransactionStatus.paid,
    )) or Decimal(0)
    month_query = select(Transaction).options(joinedload(Transaction.category), joinedload(Transaction.account)).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.competence_year == today.year, Transaction.competence_month == today.month,
        Transaction.status != TransactionStatus.cancelled,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    )
    month_items = list(db.scalars(month_query.order_by(Transaction.transaction_date.desc())).all())
    income = sum((item.amount for item in month_items if item.transaction_type == TransactionType.income), Decimal(0))
    expenses = sum((item.amount for item in month_items if item.transaction_type == TransactionType.expense), Decimal(0))
    recent = db.scalars(select(Transaction).options(
        joinedload(Transaction.category), joinedload(Transaction.account),
    ).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.status != TransactionStatus.cancelled,
    ).order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(10)).unique().all()
    return {
        "date": today.isoformat(), "selected_area_id": selected,
        "balance": _money(Decimal(accounts_initial) + Decimal(paid_balance)),
        "month": {"year": today.year, "month": today.month, "income": _money(income),
                  "expenses": _money(expenses), "net": _money(income - expenses)},
        "recent_transactions": [_transaction_json(item) for item in recent],
    }


@router.get("/months/{year}/{month}")
def month_view(year: int, month: int, area_id: int | None = None, context=Depends(_mobile_context)):
    if year < 2000 or year > 2200 or month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="Competência inválida")
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    area_filter = _transaction_area_filter(user, workspace_id, db, selected)
    year_items = db.scalars(select(Transaction).options(
        joinedload(Transaction.category), joinedload(Transaction.account),
    ).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.competence_year == year,
        Transaction.status != TransactionStatus.cancelled,
    ).order_by(Transaction.transaction_date.desc(), Transaction.id.desc())).unique().all()
    items = [item for item in year_items if (item.competence_month or item.transaction_date.month) == month]
    period = db.scalar(select(AccountingPeriod.id).where(
        AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == year,
        AccountingPeriod.month == month,
        AccountingPeriod.is_closed.is_(True),
    ))
    income = sum((item.amount for item in items if item.transaction_type == TransactionType.income), Decimal(0))
    expenses = sum((item.amount for item in items if item.transaction_type == TransactionType.expense), Decimal(0))
    monthly_totals = [{
        "income_paid": Decimal(0), "income_pending": Decimal(0),
        "expense_paid": Decimal(0), "expense_pending": Decimal(0),
    } for _ in range(12)]
    for item in year_items:
        item_month = item.competence_month or item.transaction_date.month
        if not 1 <= item_month <= 12 or item.status not in (TransactionStatus.paid, TransactionStatus.pending):
            continue
        key = f"{item.transaction_type.value}_{item.status.value}"
        if key in monthly_totals[item_month - 1]:
            monthly_totals[item_month - 1][key] += Decimal(item.amount)

    visible_ids = _visible_area_ids(user, db)
    if selected is not None:
        budget_person_ids = (selected,)
    elif visible_ids is not None:
        budget_person_ids = tuple(visible_ids)
    else:
        budget_person_ids = tuple(db.scalars(select(Person.id).where(
            Person.workspace_id == workspace_id, Person.is_active.is_(True),
        )).all())

    # Mirror the web month projection: opening balance plus confirmed and pending
    # activity, less the unspent portion of budgets that count toward projection.
    initial_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(
        Account.workspace_id == workspace_id,
    )
    if selected is not None:
        initial_query = initial_query.where(Account.person_id == selected)
    elif visible_ids is not None:
        initial_query = initial_query.where(Account.person_id.in_(visible_ids)) if visible_ids else initial_query.where(False)
    opening_balance = Decimal(db.scalar(initial_query) or 0)
    previous_paid = db.scalars(select(Transaction).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.competence_year < year, Transaction.status == TransactionStatus.paid,
    )).all()
    opening_balance += sum((
        Decimal(item.amount) if item.transaction_type == TransactionType.income else
        -Decimal(item.amount) if item.transaction_type == TransactionType.expense else Decimal(0)
        for item in previous_paid
    ), Decimal(0))

    recurrence_query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == workspace_id, RecurrenceRule.is_active.is_(True),
        RecurrenceRule.description.is_not(None), RecurrenceRule.amount.is_not(None),
    )
    if selected is not None:
        recurrence_query = recurrence_query.where(RecurrenceRule.person_id == selected)
    elif visible_ids is not None:
        recurrence_query = recurrence_query.where(RecurrenceRule.person_id.in_(visible_ids)) if visible_ids else recurrence_query.where(False)
    recurrence_rules = db.scalars(recurrence_query).all()
    occurrence_rows = db.execute(select(RecurrenceOccurrence, RecurrenceRule).join(
        RecurrenceRule, RecurrenceRule.id == RecurrenceOccurrence.recurrence_rule_id,
    ).where(RecurrenceRule.workspace_id == workspace_id, RecurrenceOccurrence.year == year)).all()
    occurrences = {(rule.id, occurrence.month): occurrence for occurrence, rule in occurrence_rows}
    resolved = {key for key, occurrence in occurrences.items() if occurrence.status != "partial"}
    for rule in recurrence_rules:
        for item_month in range(1, month + 1):
            occurrence = occurrences.get((rule.id, item_month))
            if (year, item_month) < (rule.created_at.year, rule.created_at.month) and not occurrence:
                continue
            if (rule.id, item_month) in resolved:
                continue
            amount = Decimal(occurrence.remaining_amount or 0) if occurrence and occurrence.status == "partial" else Decimal(rule.amount)
            key = "income_pending" if rule.transaction_type == TransactionType.income else "expense_pending"
            monthly_totals[item_month - 1][key] += amount

    monthly_positions = {}
    projected_balance = opening_balance
    for item_month in range(1, month + 1):
        positions = monthly_budget_positions(
            db, workspace_ids=(workspace_id,), person_ids=budget_person_ids,
            year=year, month=item_month,
        ) if budget_person_ids else []
        monthly_positions[item_month] = positions
        totals = monthly_totals[item_month - 1]
        projected_balance += totals["income_paid"] + totals["income_pending"]
        projected_balance -= totals["expense_paid"] + totals["expense_pending"]
        projected_balance -= projection_budget_remaining(positions)

    selected_positions = monthly_positions[month]
    budget_rows = []
    for position in selected_positions:
        budget = position.budget
        related = [item for item in items if item.transaction_type == TransactionType.expense
                   and item.person_id == budget.person_id and item.category_id == budget.category_id]
        paid = sum((Decimal(item.amount) for item in related if item.status == TransactionStatus.paid), Decimal(0))
        pending = sum((Decimal(item.amount) for item in related if item.status == TransactionStatus.pending), Decimal(0))
        planned = Decimal(budget.amount)
        budget_rows.append({
            "id": budget.id, "person_id": budget.person_id,
            "person_name": budget.person.name if budget.person else "Área financeira",
            "category_id": budget.category_id,
            "category_name": budget.category.name if budget.category else "Categoria",
            "parent_name": budget.category.parent_name if budget.category else None,
            "start_year": budget.start_year, "start_month": budget.start_month,
            "planned": _money(planned), "paid": _money(paid), "pending": _money(pending),
            "spent": _money(position.spent), "remaining": _money(position.remaining),
            "percent": min(100, int(position.spent / planned * 100)) if planned else 0,
            "exceeded": position.spent > planned, "released": position.is_released,
            "released_amount": _money(position.occurrence.released_amount or 0) if position.occurrence else "0.00",
            "include_in_projection": budget.include_in_projection,
        })
    selected_totals = monthly_totals[month - 1]
    realized = selected_totals["income_paid"] - selected_totals["expense_paid"]
    selected_opening_balance = projected_balance - (
        selected_totals["income_paid"] + selected_totals["income_pending"]
        - selected_totals["expense_paid"] - selected_totals["expense_pending"]
        - projection_budget_remaining(selected_positions)
    )
    return {"year": year, "month": month, "selected_area_id": selected, "is_closed": bool(period),
            "income": _money(income), "expenses": _money(expenses), "net": _money(income - expenses),
            "opening_balance": _money(selected_opening_balance),
            "closing_balance": _money(selected_opening_balance + realized),
            "projected_balance": _money(projected_balance),
            "budget_projection_remaining": _money(projection_budget_remaining(selected_positions)),
            "budgets": budget_rows,
            "transactions": [_transaction_json(item) for item in items]}


@router.get("/transactions")
def transactions(
    year: int | None = Query(default=None, ge=2000, le=2200),
    month: int | None = Query(default=None, ge=1, le=12),
    area_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context=Depends(_mobile_context),
):
    if (year is None) != (month is None):
        raise HTTPException(status_code=422, detail="Informe ano e mês juntos")
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    area_filter = _transaction_area_filter(user, workspace_id, db, selected)
    query = select(Transaction).options(
        joinedload(Transaction.category), joinedload(Transaction.account),
    ).where(
        Transaction.workspace_id == workspace_id, area_filter,
        Transaction.status != TransactionStatus.cancelled,
    )
    if year is not None:
        query = query.where(Transaction.competence_year == year, Transaction.competence_month == month)
    items = db.scalars(query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
                       .offset(offset).limit(limit)).unique().all()
    return {"items": [_transaction_json(item) for item in items], "limit": limit, "offset": offset}


@router.get("/cards")
def cards(area_id: int | None = None, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    selected = _selected_area(user, workspace_id, db, area_id)
    account_query = select(Account.id).where(Account.workspace_id == workspace_id, Account.is_active.is_(True))
    visible = _visible_area_ids(user, db)
    if selected is not None:
        account_query = account_query.where(Account.person_id == selected)
    elif visible is not None:
        account_query = account_query.where(Account.person_id.in_(visible)) if visible else account_query.where(False)
    account_ids = list(db.scalars(account_query).all())
    items = db.scalars(select(Card).where(
        Card.workspace_id == workspace_id, Card.is_active.is_(True), Card.account_id.in_(account_ids),
    ).order_by(Card.name)).all() if account_ids else []
    return {"items": [{"id": item.id, "name": item.name, "brand": item.brand,
                       "account_id": item.account_id, "credit_limit": _money(item.credit_limit),
                       "closing_day": item.closing_day, "due_day": item.due_day, "color": item.color}
                      for item in items]}


@router.post("/budgets", status_code=status.HTTP_201_CREATED)
def create_budget(payload: BudgetCreatePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    _require_area(user, workspace_id, db, payload.person_id, write=True)
    category = db.scalar(select(Category).where(
        Category.id == payload.category_id, Category.workspace_id == workspace_id,
        Category.kind == TransactionType.expense,
    ))
    if not category:
        raise HTTPException(status_code=422, detail="Categoria de despesa inválida")
    budget = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.workspace_id == workspace_id, MonthlyBudget.person_id == payload.person_id,
        MonthlyBudget.category_id == payload.category_id,
    ))
    created = budget is None
    if created:
        budget = MonthlyBudget(
            workspace_id=workspace_id, person_id=payload.person_id,
            category_id=payload.category_id, created_by_id=user.id,
        )
        db.add(budget)
    budget.amount = payload.amount
    budget.start_year = payload.start_year
    budget.start_month = payload.start_month
    budget.include_in_projection = payload.include_in_projection
    budget.is_active = True
    db.commit()
    db.refresh(budget)
    return {"id": budget.id, "person_id": budget.person_id, "category_id": budget.category_id,
            "amount": _money(budget.amount), "start_year": budget.start_year,
            "start_month": budget.start_month, "include_in_projection": budget.include_in_projection,
            "created": created}


@router.patch("/budgets/{budget_id}")
def update_budget(budget_id: int, payload: BudgetUpdatePayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    budget = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.id == budget_id, MonthlyBudget.workspace_id == workspace_id,
    ))
    if not budget or not budget.is_active:
        raise HTTPException(status_code=404, detail="Orçamento não encontrado")
    _require_area(user, workspace_id, db, budget.person_id, write=True)
    budget.amount = payload.amount
    budget.start_year = payload.start_year
    budget.start_month = payload.start_month
    budget.include_in_projection = payload.include_in_projection
    db.commit()
    return {"id": budget.id, "person_id": budget.person_id, "category_id": budget.category_id,
            "amount": _money(budget.amount), "start_year": budget.start_year,
            "start_month": budget.start_month, "include_in_projection": budget.include_in_projection}


@router.delete("/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget(budget_id: int, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    budget = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.id == budget_id, MonthlyBudget.workspace_id == workspace_id,
    ))
    if not budget or not budget.is_active:
        raise HTTPException(status_code=404, detail="Orçamento não encontrado")
    _require_area(user, workspace_id, db, budget.person_id, write=True)
    budget.is_active = False
    db.commit()


@router.post("/transactions", status_code=status.HTTP_201_CREATED)
def create_transaction(payload: TransactionPayload, context=Depends(_mobile_context)):
    user, _, db = context
    workspace_id = _workspace_id(db, user)
    if payload.status == TransactionStatus.cancelled:
        raise HTTPException(status_code=422, detail="Não é possível criar um lançamento cancelado")

    person_id = payload.person_id
    if payload.transaction_type != TransactionType.transfer:
        _require_area(user, workspace_id, db, person_id, write=True)

    account = _account(db, user, workspace_id, payload.account_id, write=True)
    destination = _account(db, user, workspace_id, payload.destination_account_id, write=True)
    card = None
    card_credit_purchase = False
    competence_year, competence_month = payload.transaction_date.year, payload.transaction_date.month

    if payload.transaction_type == TransactionType.transfer:
        if not account or not destination or account.id == destination.id:
            raise HTTPException(status_code=422, detail="Selecione contas de origem e destino diferentes")
        if payload.card_id or payload.category_id:
            raise HTTPException(status_code=422, detail="Transferências não usam cartão ou categoria")
    elif payload.destination_account_id:
        raise HTTPException(status_code=422, detail="Destino só pode ser informado em transferências")
    elif account and account.person_id != person_id:
        raise HTTPException(status_code=403, detail="A conta selecionada pertence a outra área financeira")

    # Preserve the web app behavior for a competence that has already been closed.
    while db.scalar(select(AccountingPeriod.id).where(
        AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == competence_year,
        AccountingPeriod.month == competence_month,
        AccountingPeriod.is_closed.is_(True),
    )):
        competence_month += 1
        if competence_month > 12:
            competence_month = 1
            competence_year += 1

    if payload.card_id:
        if payload.transaction_type != TransactionType.expense:
            raise HTTPException(status_code=422, detail="Cartão só pode ser usado em despesas")
        card = db.scalar(select(Card).where(
            Card.id == payload.card_id, Card.workspace_id == workspace_id, Card.is_active.is_(True),
        ))
        if not card:
            raise HTTPException(status_code=422, detail="Cartão inválido")
        card_account = _account(db, user, workspace_id, card.account_id, write=True)
        if card_account and person_id and card_account.person_id != person_id:
            raise HTTPException(status_code=403, detail="O cartão pertence a outra área financeira")
        account = card_account
        method = (payload.payment_method or "Crédito").casefold()
        if method in ("crédito", "credito"):
            card_credit_purchase = True
            competence_year, competence_month = card_purchase_competence(db, card, payload.transaction_date)
        else:
            method = "débito"

    category = None
    if payload.category_id:
        category = db.scalar(select(Category).where(
            Category.id == payload.category_id, Category.workspace_id == workspace_id,
        ))
        if not category or category.kind != payload.transaction_type:
            raise HTTPException(status_code=422, detail="Categoria incompatível com o lançamento")

    item = Transaction(
        workspace_id=workspace_id,
        transaction_type=payload.transaction_type,
        description=payload.description,
        amount=payload.amount,
        transaction_date=payload.transaction_date,
        competence_year=competence_year,
        competence_month=competence_month,
        status=(TransactionStatus.pending if card_credit_purchase else TransactionStatus.paid) if card else payload.status,
        account_id=account.id if account else None,
        destination_account_id=destination.id if destination else None,
        card_id=card.id if card else None,
        person_id=person_id,
        category_id=category.id if category else None,
        payment_method=(payload.payment_method or "Crédito") if card else payload.payment_method,
        notes=payload.notes,
        created_by_id=user.id,
        source="mobile_app",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _transaction_json(item)
