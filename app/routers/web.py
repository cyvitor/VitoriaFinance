from calendar import monthrange
from copy import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import case, delete, extract, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import allowed_person_ids, current_user, current_workspace_id
from app.models import (
    Account, AccountRole, AccountType, Card, CardBillingPeriod, Category, Person, PersonType, SystemAccount, SystemSetting,
    Transaction, TransactionStatus, TransactionType, User, UserPersonAccess, WorkspaceMember, TelegramLink,
    RecurrenceRule, RecurrenceOccurrence, AccountingPeriod, Financing, FinancingAmortization, MonthlyBudget,
    MemberRole, UserMemory, Workspace,
)
from app.security import hash_password, verify_password
from app.services.card_billing import (
    card_invoice_is_closed, card_purchase_competence, shift_month as shift_competence_month,
)
from app.services.monthly_projection import monthly_budget_positions, projection_budget_remaining
from app.services.telegram_auth import PAIRING_TTL_MINUTES, create_pairing_code, unlink_telegram
from scripts.seed import seed_categories

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


def money(value):
    value = Decimal(value or 0)
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters["money"] = money
templates.env.globals["set"] = set


def render(request: Request, template: str, user=None, **context):
    context.update(request=request, user=user, flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, template, context)


def flash(request: Request, message: str, kind: str = "success"):
    request.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def require_account_admin(user: User):
    if not user.is_super_admin and user.account_role != AccountRole.admin:
        raise HTTPException(403, "Acesso restrito ao administrador da conta")


def require_super_admin(user: User):
    if not has_global_admin_access(user):
        raise HTTPException(403, "Acesso restrito ao superadministrador")


def has_global_admin_access(user: User) -> bool:
    return bool(
        user.is_super_admin
        or (
            user.account_role == AccountRole.admin
            and user.system_account
            and user.system_account.is_super_account
        )
    )


def set_system_setting(db: Session, key: str, value: str, is_secret: bool = False):
    setting = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not setting:
        setting = SystemSetting(key=key, is_secret=is_secret); db.add(setting)
    setting.value = value


def looks_like_telegram_token(value: str) -> bool:
    parts = value.split(":", 1)
    return len(parts) == 2 and parts[0].isdigit() and len(parts[1]) > 20


def visible_people(user: User, workspace_id: int, db: Session):
    allowed = allowed_person_ids(user, db)
    query = select(Person).where(Person.workspace_id == workspace_id, Person.is_active)
    if allowed is not None:
        query = query.where(Person.id.in_(allowed))
    return db.scalars(query.order_by(Person.name)).all()


def restrict_transactions(query, user: User, db: Session):
    allowed = allowed_person_ids(user, db)
    return query if allowed is None else query.where(Transaction.person_id.in_(allowed))


def visible_accounts(user: User, workspace_id: int, db: Session):
    allowed = allowed_person_ids(user, db)
    query = select(Account).where(Account.workspace_id == workspace_id, Account.is_active)
    if allowed is not None:
        query = query.where(Account.person_id.in_(allowed))
    return db.scalars(query.order_by(Account.name)).all()


def ensure_area_access(person_id: int, user: User, workspace_id: int, db: Session):
    area = db.scalar(select(Person).where(Person.id == person_id, Person.workspace_id == workspace_id, Person.is_active))
    allowed = allowed_person_ids(user, db)
    if not area or (allowed is not None and person_id not in allowed):
        raise HTTPException(403, "Área financeira não permitida")


def ensure_account_access(account_id: int | None, user: User, workspace_id: int, db: Session):
    if not account_id:
        return
    account = db.scalar(select(Account).where(Account.id == account_id, Account.workspace_id == workspace_id))
    if not account:
        raise HTTPException(403, "Conta bancária não permitida")
    if account.person_id:
        ensure_area_access(account.person_id, user, workspace_id, db)


def optional_int(value: str | None) -> int | None:
    """Converte valores de selects opcionais; o navegador envia vazio como ''."""
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def optional_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.replace(".", "").replace(",", ".") if "," in value else value)
    except InvalidOperation:
        return None


def shift_month(value: date, offset: int) -> date:
    month_index = value.month - 1 + offset
    return date(value.year + month_index // 12, month_index % 12 + 1, 1)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return redirect("/")
    return render(request, "auth/login.html")


@router.post("/login")
def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if not user or not user.is_active or not user.system_account or not user.system_account.is_active or not verify_password(password, user.password_hash):
        flash(request, "Usuário ou senha inválidos.", "danger")
        return redirect("/login")
    membership = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
    request.session.clear()
    request.session["user_id"] = user.id
    if membership:
        request.session["workspace_id"] = membership.workspace_id
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, area_id: str | None = None,
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    areas = visible_people(user, wid, db)
    allowed_ids = {area.id for area in areas}
    area_id = optional_int(area_id)
    if area_id not in allowed_ids:
        area_id = None
    area_condition = Transaction.person_id == area_id if area_id else (
        True if allowed is None else Transaction.person_id.in_(allowed_ids)
    )
    today = date.today()
    month_items = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid,
        area_condition,
        Transaction.competence_year == today.year,
        Transaction.competence_month == today.month,
        Transaction.status != TransactionStatus.cancelled,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    )).all()
    income_paid = sum((Decimal(item.amount) for item in month_items
                       if item.transaction_type == TransactionType.income and item.status == TransactionStatus.paid), Decimal(0))
    expense_paid = sum((Decimal(item.amount) for item in month_items
                        if item.transaction_type == TransactionType.expense and item.status == TransactionStatus.paid), Decimal(0))
    income_pending = sum((Decimal(item.amount) for item in month_items
                          if item.transaction_type == TransactionType.income and item.status == TransactionStatus.pending), Decimal(0))
    expense_pending = sum((Decimal(item.amount) for item in month_items
                           if item.transaction_type == TransactionType.expense and item.status == TransactionStatus.pending), Decimal(0))
    rule_query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceRule.is_active.is_(True),
        RecurrenceRule.amount.is_not(None),
    )
    if area_id:
        rule_query = rule_query.where(RecurrenceRule.person_id == area_id)
    elif allowed is not None:
        rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed_ids))
    rules = db.scalars(rule_query).all()
    occurrences = db.scalars(select(RecurrenceOccurrence).join(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceOccurrence.year == today.year,
        RecurrenceOccurrence.month == today.month,
    )).all()
    occurrence_map = {occurrence.recurrence_rule_id: occurrence for occurrence in occurrences}
    pending_rules = []
    for rule in rules:
        occurrence = occurrence_map.get(rule.id)
        if rule.created_at.date() > today.replace(day=monthrange(today.year, today.month)[1]) and not occurrence:
            continue
        if occurrence and occurrence.status != "partial":
            continue
        amount = Decimal(occurrence.remaining_amount or 0) if occurrence and occurrence.status == "partial" else Decimal(rule.amount)
        pending_rules.append((rule, amount))
        if rule.transaction_type == TransactionType.income:
            income_pending += amount
        elif rule.transaction_type == TransactionType.expense:
            expense_pending += amount
    account_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(Account.workspace_id == wid)
    if area_id:
        account_query = account_query.where(Account.person_id == area_id)
    elif allowed is not None:
        account_query = account_query.where(Account.person_id.in_(allowed_ids))
    account_initial = db.scalar(account_query)
    paid_flow = db.scalar(select(func.coalesce(func.sum(case(
        (Transaction.transaction_type == TransactionType.income, Transaction.amount),
        (Transaction.transaction_type == TransactionType.expense, -Transaction.amount), else_=0,
    )), 0)).where(Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid))
    balance = Decimal(account_initial) + Decimal(paid_flow)
    budget_person_ids = (area_id,) if area_id else tuple(allowed_ids)
    budget_positions = monthly_budget_positions(
        db, workspace_ids=(wid,), person_ids=budget_person_ids,
        year=today.year, month=today.month,
    )
    budget_total = sum((Decimal(position.budget.amount) for position in budget_positions), Decimal(0))
    budget_spent = sum((min(position.spent, Decimal(position.budget.amount))
                        for position in budget_positions), Decimal(0))
    budget_remaining = projection_budget_remaining(budget_positions)
    projected_balance = balance + income_pending - expense_pending - budget_remaining
    recent = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid, area_condition, Transaction.status != TransactionStatus.cancelled,
    ).order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(8)).all()
    category_totals: dict[str, Decimal] = {}
    for item in month_items:
        if item.transaction_type != TransactionType.expense:
            continue
        name = item.category.name if item.category else "Sem categoria"
        category_totals[name] = category_totals.get(name, Decimal(0)) + Decimal(item.amount)
    for rule, amount in pending_rules:
        if rule.transaction_type != TransactionType.expense:
            continue
        name = rule.category.name if rule.category else "Sem categoria"
        category_totals[name] = category_totals.get(name, Decimal(0)) + amount
    categories = sorted(category_totals.items(), key=lambda row: row[1], reverse=True)[:6]
    return render(request, "dashboard.html", user=user, areas=areas, selected_area=area_id,
                  balance=balance, projected_balance=projected_balance,
                  incomes=income_paid + income_pending, income_paid=income_paid, income_pending=income_pending,
                  expenses=expense_paid + expense_pending, expense_paid=expense_paid, expense_pending=expense_pending,
                  budget_total=budget_total, budget_spent=budget_spent, budget_remaining=budget_remaining,
                  budget_percent=min(100, int(budget_spent / budget_total * 100)) if budget_total else 0,
                  pending_income_count=sum(1 for rule, _ in pending_rules if rule.transaction_type == TransactionType.income),
                  pending_expense_count=sum(1 for rule, _ in pending_rules if rule.transaction_type == TransactionType.expense),
                  recent=recent, chart_labels=[r[0] for r in categories],
                  chart_values=[float(r[1]) for r in categories], today=today)


@router.get("/analysis", response_class=HTMLResponse)
def financial_analysis(request: Request, history: int = 12, horizon: int = 12, area_id: str | None = None,
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    history = history if history in (3, 6, 12, 24) else 12
    horizon = horizon if horizon in (3, 6, 12, 24, 36, 60) else 12
    area_id = optional_int(area_id)
    wid = current_workspace_id(request, user, db); today = date.today(); areas = visible_people(user, wid, db)
    allowed_ids = {area.id for area in areas}
    if area_id not in allowed_ids: area_id = None
    area_condition = Transaction.person_id == area_id if area_id else (
        True if allowed_person_ids(user, db) is None else Transaction.person_id.in_(allowed_ids)
    )
    history_start = shift_month(today.replace(day=1), -(history - 1))
    competence_year = func.coalesce(Transaction.competence_year, extract("year", Transaction.transaction_date))
    competence_month = func.coalesce(Transaction.competence_month, extract("month", Transaction.transaction_date))
    items = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid,
        ((competence_year > history_start.year) | (
            (competence_year == history_start.year) & (competence_month >= history_start.month)
        )),
        ((competence_year < today.year) | (
            (competence_year == today.year) & (competence_month <= today.month)
        )),
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    ).order_by(competence_year, competence_month, Transaction.transaction_date)).all()
    incomes = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.income), Decimal(0))
    expenses = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.expense), Decimal(0))
    credit_expenses = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.expense
                           and (item.payment_method or "").lower() in ("crédito", "credito")), Decimal(0))
    category_totals, sector_totals = {}, {}
    monthly = {}
    for item in items:
        key = (item.competence_year or item.transaction_date.year,
               item.competence_month or item.transaction_date.month)
        monthly.setdefault(key, {"income": Decimal(0), "expense": Decimal(0)})[item.transaction_type.value] += Decimal(item.amount)
        if item.transaction_type == TransactionType.expense:
            category = item.category.name if item.category else "Sem categoria"
            sector = item.category.parent_name if item.category and item.category.parent_name else "Outros"
            category_totals[category] = category_totals.get(category, Decimal(0)) + Decimal(item.amount)
            sector_totals[sector] = sector_totals.get(sector, Decimal(0)) + Decimal(item.amount)
    month_keys = [(shift_month(history_start, offset).year, shift_month(history_start, offset).month) for offset in range(history)]
    month_names = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    monthly_labels = [f"{month_names[m-1]}/{str(y)[2:]}" for y, m in month_keys]
    monthly_incomes = [float(monthly.get(key, {}).get("income", 0)) for key in month_keys]
    monthly_expenses = [float(monthly.get(key, {}).get("expense", 0)) for key in month_keys]
    # Médias usam somente meses completos, evitando distorção do mês corrente.
    complete_keys = month_keys[:-1] if month_keys and month_keys[-1] == (today.year, today.month) else month_keys
    divisor = Decimal(len(complete_keys) or 1)
    average_income = sum((monthly.get(key, {}).get("income", Decimal(0)) for key in complete_keys), Decimal(0)) / divisor
    average_expense = sum((monthly.get(key, {}).get("expense", Decimal(0)) for key in complete_keys), Decimal(0)) / divisor
    rule_query = select(RecurrenceRule).where(RecurrenceRule.workspace_id == wid, RecurrenceRule.is_active.is_(True))
    if area_id: rule_query = rule_query.where(RecurrenceRule.person_id == area_id)
    elif allowed_person_ids(user, db) is not None: rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed_ids))
    rules = db.scalars(rule_query).all()
    recurring_income = sum((Decimal(rule.amount or 0) for rule in rules if rule.transaction_type == TransactionType.income), Decimal(0))
    fixed_expense = sum((Decimal(rule.amount or 0) for rule in rules if rule.transaction_type == TransactionType.expense), Decimal(0))
    known_history_income = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.income and item.recurrence_rule_id), Decimal(0)) / divisor
    known_history_expense = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.expense and item.recurrence_rule_id), Decimal(0)) / divisor
    variable_income = max(Decimal(0), average_income - known_history_income)
    variable_expense = max(Decimal(0), average_expense - known_history_expense)
    projected_income, projected_expense = recurring_income + variable_income, fixed_expense + variable_expense
    account_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(Account.workspace_id == wid)
    if area_id: account_query = account_query.where(Account.person_id == area_id)
    elif allowed_person_ids(user, db) is not None: account_query = account_query.where(Account.person_id.in_(allowed_ids))
    initial = Decimal(db.scalar(account_query))
    all_flow_query = select(Transaction).where(Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid)
    all_paid = db.scalars(all_flow_query).all()
    current_balance = initial + sum((Decimal(x.amount) if x.transaction_type == TransactionType.income else
        -Decimal(x.amount) if x.transaction_type == TransactionType.expense else Decimal(0) for x in all_paid), Decimal(0))
    projection_labels, projection_balances, balance = [], [], current_balance
    for offset in range(1, horizon + 1):
        period = shift_month(today.replace(day=1), offset)
        balance += projected_income - projected_expense
        projection_labels.append(f"{month_names[period.month-1]}/{str(period.year)[2:]}")
        projection_balances.append(float(balance))
    top_categories = sorted(category_totals.items(), key=lambda pair: pair[1], reverse=True)[:10]
    sectors = sorted(sector_totals.items(), key=lambda pair: pair[1], reverse=True)
    savings_rate = ((incomes - expenses) / incomes * 100) if incomes else Decimal(0)
    return render(request, "analysis/index.html", user=user, areas=areas, selected_area=area_id,
        history=history, horizon=horizon, incomes=incomes, expenses=expenses, credit_expenses=credit_expenses,
        current_balance=current_balance, average_income=average_income, average_expense=average_expense,
        projected_income=projected_income, projected_expense=projected_expense, savings_rate=savings_rate,
        monthly_labels=monthly_labels, monthly_incomes=monthly_incomes, monthly_expenses=monthly_expenses,
        category_labels=[x[0] for x in top_categories], category_values=[float(x[1]) for x in top_categories],
        sector_labels=[x[0] for x in sectors], sector_values=[float(x[1]) for x in sectors],
        projection_labels=projection_labels, projection_balances=projection_balances, top_categories=top_categories)


@router.get("/monthly-analysis", response_class=HTMLResponse)
def monthly_analysis(request: Request, year: int | None = None, month: int | None = None,
                     area_id: str | None = None, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    today = date.today()
    if year is None or month is None or not 1 <= month <= 12 or not 2000 <= year <= 2100:
        year, month = today.year, today.month
    selected = date(year, month, 1)
    wid = current_workspace_id(request, user, db)
    area_id = optional_int(area_id)
    areas = visible_people(user, wid, db)
    allowed_ids = {area.id for area in areas}
    if area_id not in allowed_ids:
        area_id = None
    allowed = allowed_person_ids(user, db)
    tx_query = select(Transaction).where(Transaction.workspace_id == wid,
        Transaction.status != TransactionStatus.cancelled,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]))
    rule_query = select(RecurrenceRule).where(RecurrenceRule.workspace_id == wid,
        RecurrenceRule.is_active.is_(True),
        RecurrenceRule.transaction_type.in_([TransactionType.income, TransactionType.expense]))
    if area_id:
        tx_query = tx_query.where(Transaction.person_id == area_id)
        rule_query = rule_query.where(RecurrenceRule.person_id == area_id)
    elif allowed is not None:
        tx_query = tx_query.where(Transaction.person_id.in_(allowed_ids))
        rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed_ids))
    transactions, rules = db.scalars(tx_query).all(), db.scalars(rule_query).all()
    rule_ids = [rule.id for rule in rules]
    occurrences = db.scalars(select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id.in_(rule_ids))).all() if rule_ids else []
    occurrence_map = {(item.recurrence_rule_id, item.year, item.month): item for item in occurrences}

    def period_totals(period: date, with_categories: bool = False):
        totals = {"income": Decimal(0), "expense": Decimal(0), "pending": Decimal(0)}
        categories: dict[str, Decimal] = {}
        for item in transactions:
            item_period = (item.competence_year or item.transaction_date.year,
                           item.competence_month or item.transaction_date.month)
            if item_period != (period.year, period.month):
                continue
            amount = Decimal(item.amount)
            totals[item.transaction_type.value] += amount
            if item.status == TransactionStatus.pending:
                totals["pending"] += amount
            if with_categories and item.transaction_type == TransactionType.expense:
                name = item.category.name if item.category else "Sem categoria"
                categories[name] = categories.get(name, Decimal(0)) + amount
        for rule in rules:
            last_day = date(period.year, period.month, monthrange(period.year, period.month)[1])
            occurrence = occurrence_map.get((rule.id, period.year, period.month))
            if ((rule.created_at.date() > last_day and not occurrence)
                    or (rule.start_date and rule.start_date > last_day)
                    or (occurrence and occurrence.status != "partial")):
                continue
            amount = Decimal(occurrence.remaining_amount if occurrence and occurrence.status == "partial" else rule.amount or 0)
            totals[rule.transaction_type.value] += amount
            totals["pending"] += amount
            if with_categories and rule.transaction_type == TransactionType.expense:
                name = rule.category.name if rule.category else "Sem categoria"
                categories[name] = categories.get(name, Decimal(0)) + amount
        return totals, categories

    selected_totals, category_totals = period_totals(selected, True)
    previous = shift_month(selected, -1)
    previous_totals, _ = period_totals(previous)
    trend_months = [shift_month(selected, offset) for offset in range(-11, 1)]
    trend = [period_totals(period)[0] for period in trend_months]
    month_names = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    income, expense = selected_totals["income"], selected_totals["expense"]
    commitment_rate = expense / income * 100 if income else None
    expense_change = ((expense - previous_totals["expense"]) / previous_totals["expense"] * 100
                      if previous_totals["expense"] else None)
    categories = sorted(category_totals.items(), key=lambda pair: pair[1], reverse=True)
    return render(request, "analysis/monthly.html", user=user, areas=areas, selected_area=area_id,
        selected=selected, previous=previous, next_month=shift_month(selected, 1), income=income,
        expense=expense, balance=income - expense, pending=selected_totals["pending"],
        commitment_rate=commitment_rate, expense_change=expense_change,
        category_rows=[(name, value, value / expense * 100 if expense else Decimal(0)) for name, value in categories],
        category_labels=[name for name, _ in categories], category_values=[float(value) for _, value in categories],
        trend_labels=[f"{month_names[p.month - 1]}/{str(p.year)[2:]}" for p in trend_months],
        trend_incomes=[float(item["income"]) for item in trend], trend_expenses=[float(item["expense"]) for item in trend])


@router.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    query = select(Transaction).where(
        Transaction.workspace_id == wid, Transaction.status != TransactionStatus.pending
    )
    query = restrict_transactions(query, user, db)
    items = db.scalars(query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())).all()
    return render(request, "transactions/index.html", user=user, items=items)


@router.get("/card-expenses/new", response_class=HTMLResponse)
def card_expense_form(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    accounts = visible_accounts(user, wid, db); account_ids = [account.id for account in accounts]
    query = select(Card).where(Card.workspace_id == wid, Card.is_active)
    if allowed_person_ids(user, db) is not None: query = query.where(Card.account_id.in_(account_ids))
    cards = db.scalars(query.order_by(Card.name)).all()
    categories = db.scalars(select(Category).where(
        Category.workspace_id == wid, Category.kind == TransactionType.expense
    ).order_by(Category.parent_name, Category.name)).all()
    return render(request, "cards/expense_form.html", user=user, cards=cards, categories=categories,
                  today=date.today())


@router.post("/card-expenses/new")
def card_expense_create(request: Request, card_id: int = Form(), description: str = Form(),
        amount: Decimal = Form(), payment_method: str = Form(), purchase_type: str = Form("cash"),
        installments: int = Form(1), category_id: int = Form(), purchase_date: date = Form(),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid, Card.is_active))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    category = db.scalar(select(Category).where(
        Category.id == category_id, Category.workspace_id == wid,
        Category.kind == TransactionType.expense,
    ))
    if not category:
        flash(request, "Selecione uma categoria de despesa válida.", "danger")
        return redirect("/card-expenses/new")
    if not card.account_id or not card.account or not card.account.person_id:
        flash(request, "O cartão precisa estar vinculado a uma conta com área financeira.", "danger")
        return redirect("/card-expenses/new")
    if amount <= 0:
        flash(request, "O valor deve ser maior que zero.", "danger"); return redirect("/card-expenses/new")
    is_credit = payment_method.lower() in ("crédito", "credito")
    count = installments if is_credit and purchase_type == "installments" else 1
    if count < 1 or count > 120:
        flash(request, "A quantidade de parcelas deve estar entre 1 e 120.", "danger")
        return redirect("/card-expenses/new")
    total_cents = int((amount * 100).quantize(Decimal("1")))
    base_cents, remainder = divmod(total_cents, count)
    first_year, first_month = (
        card_purchase_competence(db, card, purchase_date) if is_credit
        else (purchase_date.year, purchase_date.month)
    )
    for index in range(count):
        year, month = shift_competence_month(first_year, first_month, index)
        tx_date = date(year, month, min(purchase_date.day, monthrange(year, month)[1]))
        cents = base_cents + (remainder if index == count - 1 else 0)
        label = f"{description.strip()} ({index + 1}/{count})" if count > 1 else description.strip()
        db.add(Transaction(
            workspace_id=wid, transaction_type=TransactionType.expense, description=label,
            amount=Decimal(cents) / 100, transaction_date=tx_date,
            status=TransactionStatus.pending if is_credit else TransactionStatus.paid,
            account_id=card.account_id, card_id=card.id, person_id=card.account.person_id,
            category_id=category.id,
            payment_method="Crédito" if is_credit else "Débito", created_by_id=user.id,
            competence_year=year, competence_month=month,
        ))
    db.commit()
    flash(request, f"Gasto no cartão registrado{' em ' + str(count) + ' parcelas' if count > 1 else ''}.")
    return redirect(f"/cards/{card.id}?year={first_year}&month={first_month}&view=detailed")


@router.get("/future", response_class=HTMLResponse)
def future_transactions(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    query = select(Transaction).where(
        Transaction.workspace_id == wid, Transaction.status == TransactionStatus.pending
    )
    query = restrict_transactions(query, user, db)
    items = db.scalars(query.order_by(Transaction.transaction_date, Transaction.id)).all()
    return render(request, "transactions/future.html", user=user, items=items)


@router.get("/recurring-incomes", response_class=HTMLResponse)
def recurring_incomes(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    rule_query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceRule.description.is_not(None),
        RecurrenceRule.transaction_type == TransactionType.income,
    )
    if allowed is not None: rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed))
    rules = db.scalars(rule_query.order_by(RecurrenceRule.is_active.desc(), RecurrenceRule.description)).all()
    return render(request, "recurring_incomes/index.html", user=user, rules=rules,
        accounts=visible_accounts(user, wid, db), people=visible_people(user, wid, db),
        categories=db.scalars(select(Category).where(
            Category.workspace_id == wid, Category.kind == TransactionType.income
        ).order_by(Category.name)).all())


@router.post("/recurring-incomes")
def recurring_income_create(request: Request, description: str = Form(), amount: Decimal = Form(),
        account_id: str | None = Form(None), person_id: str | None = Form(None),
        category_id: str | None = Form(None), notes: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    person_id_value = optional_int(person_id)
    if not person_id_value:
        flash(request, "Selecione uma área financeira.", "danger")
        return redirect("/recurring-incomes")
    ensure_area_access(person_id_value, user, wid, db)
    ensure_account_access(optional_int(account_id), user, wid, db)
    if amount <= 0:
        flash(request, "O valor deve ser maior que zero.", "danger")
        return redirect("/recurring-incomes")
    db.add(RecurrenceRule(
        workspace_id=wid, frequency="monthly", description=description.strip(), amount=amount,
        transaction_type=TransactionType.income,
        account_id=optional_int(account_id), person_id=person_id_value,
        category_id=optional_int(category_id), notes=notes, created_by_id=user.id, is_active=True,
    ))
    db.commit()
    flash(request, "Receita recorrente adicionada. Ela aparecerá para confirmação a cada mês.")
    return redirect("/recurring-incomes")


@router.post("/recurring-incomes/{rule_id}/edit")
def recurring_income_edit(rule_id: int, request: Request, description: str = Form(), amount: Decimal = Form(),
                          account_id: str | None = Form(None), person_id: int = Form(),
                          category_id: str | None = Form(None), notes: str | None = Form(None),
                          db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(
        RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid,
        RecurrenceRule.transaction_type == TransactionType.income,
        RecurrenceRule.is_active.is_(True),
    ))
    if not rule: raise HTTPException(404)
    ensure_area_access(rule.person_id, user, wid, db)
    ensure_area_access(person_id, user, wid, db)
    account_value, category_value = optional_int(account_id), optional_int(category_id)
    ensure_account_access(account_value, user, wid, db)
    if category_value and not db.scalar(select(Category.id).where(
        Category.id == category_value, Category.workspace_id == wid,
        Category.kind == TransactionType.income,
    )):
        flash(request, "Selecione uma categoria de receita válida.", "danger")
        return redirect("/recurring-incomes")
    if amount <= 0 or not description.strip():
        flash(request, "Informe descrição e valor válidos.", "danger")
        return redirect("/recurring-incomes")
    rule.description = description.strip()
    rule.amount = amount
    rule.account_id = account_value
    rule.person_id = person_id
    rule.category_id = category_value
    rule.notes = (notes or "").strip() or None
    db.commit()
    flash(request, "Receita recorrente atualizada. As alterações valem para os próximos meses.")
    return redirect("/recurring-incomes")


@router.post("/recurring-incomes/{rule_id}/delete")
def recurring_income_delete(rule_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid))
    if not rule:
        raise HTTPException(404)
    rule.is_active = False
    db.commit()
    flash(request, "Receita recorrente excluída dos meses futuros. Lançamentos já confirmados foram preservados.")
    return redirect("/recurring-incomes")


@router.get("/fixed-expenses", response_class=HTMLResponse)
def fixed_expenses(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    rule_query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceRule.description.is_not(None),
        RecurrenceRule.transaction_type == TransactionType.expense,
        ~RecurrenceRule.id.in_(select(Financing.recurrence_rule_id).where(Financing.recurrence_rule_id.is_not(None))),
    )
    if allowed is not None: rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed))
    rules = db.scalars(rule_query.order_by(RecurrenceRule.is_active.desc(), RecurrenceRule.description)).all()
    return render(request, "fixed_expenses/index.html", user=user, rules=rules,
        accounts=visible_accounts(user, wid, db), people=visible_people(user, wid, db),
        categories=db.scalars(select(Category).where(
            Category.workspace_id == wid, Category.kind == TransactionType.expense
        ).order_by(Category.parent_name, Category.name)).all())


@router.post("/fixed-expenses")
def fixed_expense_create(request: Request, description: str = Form(), amount: Decimal = Form(),
        account_id: str | None = Form(None), person_id: str | None = Form(None),
        category_id: str | None = Form(None), notes: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    person_id_value = optional_int(person_id)
    if not person_id_value:
        flash(request, "Selecione uma área financeira.", "danger")
        return redirect("/fixed-expenses")
    ensure_area_access(person_id_value, user, wid, db)
    ensure_account_access(optional_int(account_id), user, wid, db)
    if amount <= 0:
        flash(request, "O valor deve ser maior que zero.", "danger")
        return redirect("/fixed-expenses")
    db.add(RecurrenceRule(
        workspace_id=wid, frequency="monthly", transaction_type=TransactionType.expense,
        description=description.strip(), amount=amount, account_id=optional_int(account_id),
        person_id=person_id_value, category_id=optional_int(category_id), notes=notes,
        created_by_id=user.id, is_active=True,
    ))
    db.commit()
    flash(request, "Despesa fixa adicionada. Ela aparecerá para confirmação a cada mês.")
    return redirect("/fixed-expenses")


@router.post("/fixed-expenses/{rule_id}/edit")
def fixed_expense_edit(rule_id: int, request: Request, description: str = Form(), amount: Decimal = Form(),
                       category_id: str | None = Form(None),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id,
        RecurrenceRule.workspace_id == wid, RecurrenceRule.transaction_type == TransactionType.expense,
        RecurrenceRule.is_active.is_(True)))
    if not rule or amount <= 0:
        raise HTTPException(404)
    ensure_area_access(rule.person_id, user, wid, db)
    category_value = optional_int(category_id)
    if category_value and not db.scalar(select(Category.id).where(
        Category.id == category_value, Category.workspace_id == wid,
        Category.kind == TransactionType.expense,
    )):
        flash(request, "Selecione uma categoria de despesa válida.", "danger")
        return redirect("/fixed-expenses")
    rule.description, rule.amount, rule.category_id = description.strip(), amount, category_value
    db.commit()
    flash(request, "Despesa fixa atualizada.")
    return redirect("/fixed-expenses")


@router.get("/financings", response_class=HTMLResponse)
def financings(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    query = select(Financing).where(Financing.workspace_id == wid)
    if allowed is not None:
        query = query.where(Financing.person_id.in_(allowed))
    categories = db.scalars(select(Category).where(Category.workspace_id == wid,
        Category.kind == TransactionType.expense).order_by(Category.parent_name, Category.name)).all()
    return render(request, "financings/index.html", user=user,
        items=db.scalars(query.order_by(Financing.status, Financing.description)).all(),
        people=visible_people(user, wid, db), accounts=visible_accounts(user, wid, db), categories=categories)


@router.post("/financings")
def financing_create(request: Request, description: str = Form(), paid_installments: int = Form(),
        total_installments: int = Form(), installment_amount: Decimal = Form(), person_id: int = Form(),
        account_id: str | None = Form(None), category_id: str | None = Form(None),
        financed_amount: str | None = Form(None), outstanding_balance: str | None = Form(None),
        nominal_interest_rate: str | None = Form(None), institution: str | None = Form(None),
        due_day: str | None = Form(None), notes: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    ensure_area_access(person_id, user, wid, db)
    account_value, category_value = optional_int(account_id), optional_int(category_id)
    ensure_account_access(account_value, user, wid, db)
    if total_installments < 1 or paid_installments < 0 or paid_installments >= total_installments or installment_amount <= 0:
        flash(request, "Confira a quantidade de parcelas e o valor mensal.", "danger")
        return redirect("/financings")
    rule = RecurrenceRule(workspace_id=wid, frequency="monthly", transaction_type=TransactionType.expense,
        start_date=date.today().replace(day=1),
        description=f"{description.strip()} - parcela {paid_installments + 1}/{total_installments}", amount=installment_amount,
        account_id=account_value, person_id=person_id, category_id=category_value,
        notes=notes, created_by_id=user.id, is_active=True)
    db.add(rule); db.flush()
    db.add(Financing(workspace_id=wid, person_id=person_id, recurrence_rule_id=rule.id,
        description=description.strip(), paid_installments=paid_installments,
        total_installments=total_installments, installment_amount=installment_amount,
        financed_amount=optional_decimal(financed_amount), outstanding_balance=optional_decimal(outstanding_balance),
        nominal_interest_rate=optional_decimal(nominal_interest_rate), institution=(institution or "").strip() or None,
        due_day=optional_int(due_day), notes=notes, status="active"))
    db.commit()
    flash(request, "Financiamento cadastrado e parcela mensal programada.")
    return redirect("/financings")


@router.get("/financings/{financing_id}", response_class=HTMLResponse)
def financing_detail(financing_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    financing = db.scalar(select(Financing).where(
        Financing.id == financing_id, Financing.workspace_id == wid,
    ))
    if not financing: raise HTTPException(404)
    ensure_area_access(financing.person_id, user, wid, db)
    histories = db.scalars(select(FinancingAmortization).where(
        FinancingAmortization.financing_id == financing.id
    ).order_by(FinancingAmortization.amortization_date.desc(),
               FinancingAmortization.id.desc())).all()
    user_ids = {history.user_id for history in histories}
    history_users = {item.id: item for item in db.scalars(select(User).where(User.id.in_(user_ids))).all()} \
        if user_ids else {}
    categories = db.scalars(select(Category).where(
        Category.workspace_id == wid, Category.kind == TransactionType.expense
    ).order_by(Category.parent_name, Category.name)).all()
    return render(request, "financings/detail.html", user=user, financing=financing,
                  histories=histories, history_users=history_users,
                  people=visible_people(user, wid, db), accounts=visible_accounts(user, wid, db),
                  categories=categories, today=date.today())


@router.post("/financings/{financing_id}/edit")
def financing_edit(financing_id: int, request: Request, description: str = Form(), person_id: int = Form(),
                   institution: str | None = Form(None), account_id: str | None = Form(None),
                   category_id: str | None = Form(None), financed_amount: str | None = Form(None),
                   nominal_interest_rate: str | None = Form(None), due_day: str | None = Form(None),
                   start_date: date | None = Form(None), notes: str | None = Form(None),
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    financing = db.scalar(select(Financing).where(
        Financing.id == financing_id, Financing.workspace_id == wid,
    ))
    if not financing: raise HTTPException(404)
    ensure_area_access(financing.person_id, user, wid, db)
    ensure_area_access(person_id, user, wid, db)
    account_value, category_value = optional_int(account_id), optional_int(category_id)
    ensure_account_access(account_value, user, wid, db)
    if category_value and not db.scalar(select(Category.id).where(
        Category.id == category_value, Category.workspace_id == wid,
        Category.kind == TransactionType.expense,
    )):
        flash(request, "Selecione uma categoria de despesa válida.", "danger")
        return redirect(f"/financings/{financing.id}")
    due_day_value = optional_int(due_day)
    if not description.strip() or (due_day_value and not 1 <= due_day_value <= 31):
        flash(request, "Confira a descrição e o dia de vencimento.", "danger")
        return redirect(f"/financings/{financing.id}")
    financing.description = description.strip()
    financing.person_id = person_id
    financing.institution = (institution or "").strip() or None
    financing.financed_amount = optional_decimal(financed_amount)
    financing.nominal_interest_rate = optional_decimal(nominal_interest_rate)
    financing.due_day = due_day_value
    financing.start_date = start_date
    financing.notes = (notes or "").strip() or None
    rule = db.get(RecurrenceRule, financing.recurrence_rule_id) if financing.recurrence_rule_id else None
    if rule:
        rule.person_id = person_id
        rule.account_id = account_value
        rule.category_id = category_value
        rule.notes = financing.notes
        rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{financing.total_installments}"
    db.commit()
    flash(request, "Informações do financiamento atualizadas.")
    return redirect(f"/financings/{financing.id}")


@router.post("/financings/{financing_id}/amortize")
def financing_amortize(financing_id: int, request: Request, amortization_date: date = Form(),
                       new_outstanding_balance: Decimal = Form(), strategy: str = Form(),
                       amortized_amount: str | None = Form(None), remaining_installments: str | None = Form(None),
                       new_installment_amount: str | None = Form(None), notes: str | None = Form(None),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    financing = db.scalar(select(Financing).where(
        Financing.id == financing_id, Financing.workspace_id == wid, Financing.status == "active",
    ))
    if not financing: raise HTTPException(404)
    ensure_area_access(financing.person_id, user, wid, db)
    if strategy not in ("reduce_term", "reduce_installment", "reduce_both") or new_outstanding_balance < 0:
        flash(request, "Confira a estratégia e o novo saldo devedor.", "danger")
        return redirect(f"/financings/{financing.id}")
    remaining_value = optional_int(remaining_installments)
    installment_value = optional_decimal(new_installment_amount)
    if strategy in ("reduce_term", "reduce_both") and remaining_value is None:
        flash(request, "Informe a nova quantidade de parcelas restantes.", "danger")
        return redirect(f"/financings/{financing.id}")
    if strategy in ("reduce_installment", "reduce_both") and installment_value is None:
        flash(request, "Informe o novo valor da parcela.", "danger")
        return redirect(f"/financings/{financing.id}")
    remaining_value = remaining_value if remaining_value is not None \
        else financing.total_installments - financing.paid_installments
    installment_value = installment_value if installment_value is not None else Decimal(financing.installment_amount)
    if remaining_value < 0 or installment_value <= 0:
        flash(request, "A quantidade restante e o valor da parcela devem ser válidos.", "danger")
        return redirect(f"/financings/{financing.id}")
    new_total = financing.paid_installments + remaining_value
    history = FinancingAmortization(
        financing_id=financing.id, user_id=user.id, amortization_date=amortization_date,
        amortized_amount=optional_decimal(amortized_amount),
        previous_outstanding_balance=financing.outstanding_balance,
        new_outstanding_balance=new_outstanding_balance,
        previous_total_installments=financing.total_installments,
        new_total_installments=new_total,
        previous_installment_amount=financing.installment_amount,
        new_installment_amount=installment_value,
        strategy=strategy, source="web", notes=(notes or "").strip() or None,
    )
    db.add(history)
    financing.outstanding_balance = new_outstanding_balance
    financing.total_installments = new_total
    financing.installment_amount = installment_value
    rule = db.get(RecurrenceRule, financing.recurrence_rule_id) if financing.recurrence_rule_id else None
    if remaining_value == 0 or new_outstanding_balance == 0:
        financing.status = "paid"
        if rule: rule.is_active = False
    elif rule:
        rule.amount = installment_value
        rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{new_total}"
    db.commit()
    flash(request, "Amortização registrada e próximas parcelas atualizadas.")
    return redirect(f"/financings/{financing.id}")


@router.post("/fixed-expenses/{rule_id}/delete")
def fixed_expense_delete(rule_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(
        RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid,
        RecurrenceRule.transaction_type == TransactionType.expense,
    ))
    if not rule:
        raise HTTPException(404)
    rule.is_active = False
    db.commit()
    flash(request, "Despesa fixa excluída dos meses futuros. Pagamentos confirmados foram preservados.")
    return redirect("/fixed-expenses")


@router.get("/budgets", response_class=HTMLResponse)
def budgets(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    query = select(MonthlyBudget).where(MonthlyBudget.workspace_id == wid)
    if allowed is not None:
        query = query.where(MonthlyBudget.person_id.in_(allowed))
    items = db.scalars(query.order_by(MonthlyBudget.is_active.desc(), MonthlyBudget.id.desc())).all()
    categories = db.scalars(select(Category).where(
        Category.workspace_id == wid, Category.kind == TransactionType.expense,
    ).order_by(Category.parent_name, Category.name)).all()
    return render(request, "budgets/index.html", user=user, items=items,
                  people=visible_people(user, wid, db), categories=categories,
                  current_year=date.today().year, current_month=date.today().month)


@router.post("/budgets")
def budget_create(request: Request, person_id: int = Form(), category_id: int = Form(),
                  amount: Decimal = Form(), start_month: str = Form(),
                  include_in_projection: bool = Form(False),
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    ensure_area_access(person_id, user, wid, db)
    category = db.scalar(select(Category).where(
        Category.id == category_id, Category.workspace_id == wid,
        Category.kind == TransactionType.expense,
    ))
    try:
        start_year, start_month_number = map(int, start_month.split("-", 1))
    except (TypeError, ValueError):
        category = None
        start_year, start_month_number = 0, 0
    if not category or amount <= 0 or not 1 <= start_month_number <= 12:
        flash(request, "Informe área, categoria, valor e mês inicial válidos.", "danger")
        return redirect("/budgets")
    existing = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.workspace_id == wid, MonthlyBudget.person_id == person_id,
        MonthlyBudget.category_id == category_id,
    ))
    if existing:
        existing.amount = amount
        existing.start_year = start_year
        existing.start_month = start_month_number
        existing.include_in_projection = include_in_projection
        existing.is_active = True
    else:
        db.add(MonthlyBudget(
            workspace_id=wid, person_id=person_id, category_id=category_id, amount=amount,
            start_year=start_year, start_month=start_month_number,
            include_in_projection=include_in_projection, created_by_id=user.id,
        ))
    db.commit()
    flash(request, "Orçamento mensal salvo.")
    return redirect("/budgets")


@router.post("/budgets/{budget_id}/edit")
def budget_edit(budget_id: int, request: Request, amount: Decimal = Form(), start_month: str = Form(),
                include_in_projection: bool = Form(False),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    budget = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.id == budget_id, MonthlyBudget.workspace_id == wid,
    ))
    if not budget:
        raise HTTPException(404)
    ensure_area_access(budget.person_id, user, wid, db)
    try:
        start_year, start_month_number = map(int, start_month.split("-", 1))
    except (TypeError, ValueError):
        start_year, start_month_number = 0, 0
    if amount <= 0 or not 1 <= start_month_number <= 12:
        flash(request, "Informe valor e mês inicial válidos.", "danger")
        return redirect("/budgets")
    budget.amount = amount
    budget.start_year = start_year
    budget.start_month = start_month_number
    budget.include_in_projection = include_in_projection
    db.commit()
    flash(request, "Orçamento atualizado.")
    return redirect("/budgets")


@router.post("/budgets/{budget_id}/delete")
def budget_delete(budget_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    budget = db.scalar(select(MonthlyBudget).where(
        MonthlyBudget.id == budget_id, MonthlyBudget.workspace_id == wid,
    ))
    if not budget:
        raise HTTPException(404)
    ensure_area_access(budget.person_id, user, wid, db)
    budget.is_active = False
    db.commit()
    flash(request, "Orçamento desativado para os próximos meses.")
    return redirect("/budgets")


@router.post("/recurrences/{rule_id}/confirm")
def recurring_income_confirm(rule_id: int, request: Request, year: int = Form(), month: int = Form(),
        confirmed_amount: Decimal = Form(), payment_source: str | None = Form(None),
        settlement_mode: str = Form("full"), confirmed_description: str | None = Form(None),
        confirmed_category_id: str | None = Form(None), paid_on: date | None = Form(None),
        confirmed_notes: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(
        RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid, RecurrenceRule.is_active.is_(True)
    ))
    existing = db.scalar(select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id == rule_id,
        RecurrenceOccurrence.year == year, RecurrenceOccurrence.month == month,
    ))
    if not rule or (existing and existing.status != "partial"):
        raise HTTPException(404)
    if confirmed_amount <= 0:
        flash(request, "O valor confirmado deve ser maior que zero.", "danger")
        return redirect(f"/month?year={year}&month={month}")
    target_year, target_month = year, month
    while db.scalar(select(AccountingPeriod.id).where(
        AccountingPeriod.workspace_id == wid, AccountingPeriod.year == target_year,
        AccountingPeriod.month == target_month, AccountingPeriod.is_closed.is_(True),
    )):
        target_month += 1
        if target_month == 13:
            target_month, target_year = 1, target_year + 1
    remaining_before = Decimal(existing.remaining_amount) if existing and existing.remaining_amount is not None else Decimal(rule.amount)
    is_partial = rule.transaction_type == TransactionType.expense and settlement_mode == "partial"
    if is_partial and confirmed_amount >= remaining_before:
        flash(request, "Para manter saldo programado, informe um valor menor que o saldo restante.", "danger")
        return redirect(f"/month?year={year}&month={month}")
    account_id, card_id, payment_method = rule.account_id, None, rule.payment_method
    transaction_status = TransactionStatus.paid
    if rule.transaction_type == TransactionType.expense:
        if not payment_source or ":" not in payment_source:
            flash(request, "Selecione a conta ou o cartão usado no pagamento.", "danger")
            return redirect(f"/month?year={year}&month={month}")
        source_type, raw_id = payment_source.split(":", 1)
        source_id = optional_int(raw_id)
        if source_type == "card":
            card = db.scalar(select(Card).where(Card.id == source_id, Card.workspace_id == wid, Card.is_active))
            if not card or not card.account_id:
                raise HTTPException(403)
            ensure_account_access(card.account_id, user, wid, db)
            account_id, card_id, payment_method = card.account_id, card.id, "Crédito"
            transaction_status = TransactionStatus.pending
        elif source_type == "account":
            account = db.scalar(select(Account).where(Account.id == source_id, Account.workspace_id == wid, Account.is_active))
            if not account:
                raise HTTPException(403)
            ensure_account_access(account.id, user, wid, db)
            account_id, payment_method = account.id, "Conta bancária"
        else:
            raise HTTPException(400)
    description = (confirmed_description or rule.description).strip()
    category_id = rule.category_id
    requested_category_id = optional_int(confirmed_category_id)
    if is_partial and requested_category_id:
        category = db.scalar(select(Category).where(Category.id == requested_category_id,
            Category.workspace_id == wid, Category.kind == TransactionType.expense))
        if not category:
            raise HTTPException(400)
        category_id = category.id
    transaction = Transaction(
        workspace_id=wid, transaction_type=rule.transaction_type, description=description,
        amount=confirmed_amount, transaction_date=paid_on if is_partial and paid_on else date.today(), status=transaction_status,
        account_id=account_id, card_id=card_id, person_id=rule.person_id, category_id=category_id,
        payment_method=payment_method, notes=confirmed_notes if is_partial and confirmed_notes is not None else rule.notes, created_by_id=user.id,
        recurrence_rule_id=rule.id, competence_year=target_year, competence_month=target_month,
    )
    db.add(transaction); db.flush()
    if existing:
        existing.status = "partial" if is_partial else "confirmed"
        existing.remaining_amount = remaining_before - confirmed_amount if is_partial else Decimal(0)
        existing.transaction_id = transaction.id
    else:
        db.add(RecurrenceOccurrence(recurrence_rule_id=rule.id, year=year, month=month,
            status="partial" if is_partial else "confirmed", transaction_id=transaction.id,
            remaining_amount=remaining_before - confirmed_amount if is_partial else Decimal(0)))
    financing = db.scalar(select(Financing).where(Financing.recurrence_rule_id == rule.id))
    if financing and not is_partial:
        financing.paid_installments = min(financing.total_installments, financing.paid_installments + 1)
        if financing.paid_installments >= financing.total_installments:
            financing.status = "paid"
            rule.is_active = False
        else:
            rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{financing.total_installments}"
    db.commit()
    label = "Receita" if rule.transaction_type == TransactionType.income else "Despesa"
    flash(request, f"{label} confirmada na competência {target_month:02d}/{target_year}.")
    return redirect(f"/month?year={target_year}&month={target_month}")


@router.post("/recurrences/{rule_id}/skip")
def recurring_income_skip(rule_id: int, request: Request, year: int = Form(), month: int = Form(),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid))
    if not rule:
        raise HTTPException(404)
    existing = db.scalar(select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id == rule_id,
        RecurrenceOccurrence.year == year, RecurrenceOccurrence.month == month,
    ))
    if not existing:
        db.add(RecurrenceOccurrence(recurrence_rule_id=rule.id, year=year, month=month, status="skipped"))
        db.commit()
    flash(request, "Recorrência ignorada somente neste mês.")
    return redirect(f"/month?year={year}&month={month}")


@router.get("/month", response_class=HTMLResponse)
def family_view(request: Request, year: int | None = None, month: int | None = None,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    today = date.today()
    selected_year = year or today.year
    selected_month = month if month and 1 <= month <= 12 else today.month
    year_start = date(selected_year, 1, 1)
    year_end = date(selected_year + 1, 1, 1)
    item_query = select(Transaction).where(
        Transaction.workspace_id == wid,
        Transaction.competence_year == selected_year,
        Transaction.status != TransactionStatus.cancelled,
    )
    items = db.scalars(restrict_transactions(item_query, user, db).order_by(Transaction.transaction_date, Transaction.id)).all()
    months = [{
        "number": number, "income_paid": Decimal(0), "expense_paid": Decimal(0),
        "income_pending": Decimal(0), "expense_pending": Decimal(0),
    } for number in range(1, 13)]
    for item in items:
        summary = months[(item.competence_month or item.transaction_date.month) - 1]
        key = f"{item.transaction_type.value}_{item.status.value}"
        if key in summary:
            summary[key] += Decimal(item.amount)
    rule_query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceRule.is_active.is_(True),
        RecurrenceRule.description.is_not(None), RecurrenceRule.amount.is_not(None),
    )
    if allowed is not None: rule_query = rule_query.where(RecurrenceRule.person_id.in_(allowed))
    recurring_rules = db.scalars(rule_query).all()
    occurrences = db.scalars(select(RecurrenceOccurrence).join(RecurrenceRule).where(
        RecurrenceRule.workspace_id == wid, RecurrenceOccurrence.year == selected_year,
    )).all()
    occurrence_by_key = {(occurrence.recurrence_rule_id, occurrence.month): occurrence for occurrence in occurrences}
    resolved = {key for key, occurrence in occurrence_by_key.items() if occurrence.status != "partial"}
    virtual_by_month = {number: [] for number in range(1, 13)}
    for rule in recurring_rules:
        created = rule.created_at.date()
        for number in range(1, 13):
            partial = occurrence_by_key.get((rule.id, number))
            if (selected_year, number) < (created.year, created.month) and not partial:
                continue
            if (rule.id, number) in resolved:
                continue
            display_rule = rule
            if partial and partial.status == "partial":
                display_rule = copy(rule)
                display_rule.amount = Decimal(partial.remaining_amount or 0)
            virtual_by_month[number].append(display_rule)
            key = "income_pending" if rule.transaction_type == TransactionType.income else "expense_pending"
            months[number - 1][key] += Decimal(display_rule.amount)
    initial_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(Account.workspace_id == wid)
    if allowed is not None: initial_query = initial_query.where(Account.person_id.in_(allowed))
    initial_balance = Decimal(db.scalar(initial_query))
    before_year_query = select(Transaction).where(
        Transaction.workspace_id == wid,
        Transaction.competence_year < selected_year,
        Transaction.status == TransactionStatus.paid,
    )
    before_year_items = db.scalars(restrict_transactions(before_year_query, user, db)).all()
    year_opening_balance = initial_balance + sum(
        (Decimal(item.amount) if item.transaction_type == TransactionType.income else -Decimal(item.amount)
         if item.transaction_type == TransactionType.expense else Decimal(0)) for item in before_year_items
    )
    budget_person_ids = tuple(allowed) if allowed is not None else tuple(db.scalars(
        select(Person.id).where(Person.workspace_id == wid, Person.is_active.is_(True))
    ).all())
    projected_opening_balance = year_opening_balance
    for summary in months:
        summary["realized"] = summary["income_paid"] - summary["expense_paid"]
        summary["projected"] = (
            summary["income_paid"] + summary["income_pending"]
            - summary["expense_paid"] - summary["expense_pending"]
        )
        summary["opening_balance"] = projected_opening_balance
        positions = monthly_budget_positions(
            db, workspace_ids=(wid,), person_ids=budget_person_ids,
            year=selected_year, month=summary["number"],
        )
        budget_projection_remaining = projection_budget_remaining(positions)
        summary["budget_projection_remaining"] = budget_projection_remaining
        summary["projected_balance"] = (
            projected_opening_balance + summary["projected"] - budget_projection_remaining
        )
        projected_opening_balance = summary["projected_balance"]
        summary["running_balance"] = projected_opening_balance
        summary["has_activity"] = any((
            summary["income_paid"], summary["expense_paid"],
            summary["income_pending"], summary["expense_pending"],
        ))
    visible_months = [summary for summary in months if summary["has_activity"]]
    if selected_year == today.year and not any(summary["number"] == today.month for summary in visible_months):
        visible_months.append(months[today.month - 1])
    if not any(summary["number"] == selected_month for summary in visible_months):
        selected_month = today.month if selected_year == today.year else (visible_months[0]["number"] if visible_months else 1)
    visible_months.sort(key=lambda summary: summary["number"])
    selected_items = [item for item in items if (item.competence_month or item.transaction_date.month) == selected_month]
    confirmed = [item for item in selected_items if item.status == TransactionStatus.paid]
    forecasts = [item for item in selected_items if item.status == TransactionStatus.pending]
    selected_summary = months[selected_month - 1]
    selected_start = date(selected_year, selected_month, 1)
    opening_balance = selected_summary["opening_balance"]
    selected_summary["closing_balance"] = opening_balance + selected_summary["realized"]
    income_items = [item for item in selected_items if item.transaction_type == TransactionType.income]
    expense_items = [item for item in selected_items if item.transaction_type == TransactionType.expense and not item.card_id]
    selected_budget_positions = monthly_budget_positions(
        db, workspace_ids=(wid,), person_ids=budget_person_ids,
        year=selected_year, month=selected_month,
    )
    budget_rows = []
    for position in selected_budget_positions:
        budget = position.budget
        related = [item for item in selected_items if (
            item.transaction_type == TransactionType.expense
            and item.person_id == budget.person_id
            and item.category_id == budget.category_id
        )]
        paid = sum((Decimal(item.amount) for item in related if item.status == TransactionStatus.paid), Decimal(0))
        pending = sum((Decimal(item.amount) for item in related if item.status == TransactionStatus.pending), Decimal(0))
        spent = position.spent
        amount = Decimal(budget.amount)
        budget_rows.append({
            "budget": budget, "planned": amount, "paid": paid, "pending": pending,
            "spent": spent, "remaining": amount - spent,
            "percent": min(100, int((spent / amount) * 100)) if amount else 0,
            "exceeded": spent > amount,
        })
    selected_summary["budget_projection_remaining"] = projection_budget_remaining(selected_budget_positions)
    card_groups_map = {}
    for item in selected_items:
        if item.transaction_type != TransactionType.expense or not item.card:
            continue
        method = (item.payment_method or "Crédito").capitalize()
        key = (item.card_id, method, item.status.value)
        group = card_groups_map.setdefault(key, {"card": item.card, "method": method, "status": item.status,
                                                 "amount": Decimal(0), "count": 0})
        group["amount"] += Decimal(item.amount); group["count"] += 1
    card_groups = list(card_groups_map.values())
    recurring_pending = [rule for rule in virtual_by_month[selected_month] if rule.transaction_type == TransactionType.income]
    fixed_expense_pending = [rule for rule in virtual_by_month[selected_month] if rule.transaction_type == TransactionType.expense]
    period = db.scalar(select(AccountingPeriod).where(
        AccountingPeriod.workspace_id == wid, AccountingPeriod.year == selected_year,
        AccountingPeriod.month == selected_month,
    ))
    people = visible_people(user, wid, db)
    accounts = visible_accounts(user, wid, db)
    account_ids = [account.id for account in accounts]
    cards = db.scalars(select(Card).where(Card.workspace_id == wid, Card.is_active.is_(True),
        Card.account_id.in_(account_ids)).order_by(Card.name)).all() if account_ids else []
    expense_categories = db.scalars(select(Category).where(Category.workspace_id == wid,
        Category.kind == TransactionType.expense).order_by(Category.parent_name, Category.name)).all()
    transaction_categories = db.scalars(select(Category).where(
        Category.workspace_id == wid
    ).order_by(Category.kind, Category.parent_name, Category.name)).all()
    return render(request, "family/index.html", user=user, months=months, selected_year=selected_year,
                  selected_month=selected_month, confirmed=confirmed, forecasts=forecasts, people=people,
                  selected_summary=selected_summary, income_items=income_items, expense_items=expense_items,
                  period_closed=bool(period and period.is_closed), visible_months=visible_months,
                  recurring_pending=recurring_pending, fixed_expense_pending=fixed_expense_pending,
                  card_groups=card_groups, accounts=accounts, cards=cards,
                  expense_categories=expense_categories, transaction_categories=transaction_categories,
                  budget_rows=budget_rows, today=today)


@router.get("/family", include_in_schema=False)
def old_family_view():
    return redirect("/month")


@router.post("/month/close")
def close_month(request: Request, year: int = Form(), month: int = Form(),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    if not 1 <= month <= 12:
        raise HTTPException(400)
    period = db.scalar(select(AccountingPeriod).where(
        AccountingPeriod.workspace_id == wid, AccountingPeriod.year == year, AccountingPeriod.month == month,
    ))
    if not period:
        period = AccountingPeriod(workspace_id=wid, year=year, month=month)
        db.add(period)
    period.is_closed = True
    period.closed_at = datetime.utcnow()
    period.closed_by_id = user.id
    db.commit()
    flash(request, "Mês fechado. Novas confirmações devem usar o próximo período aberto.")
    return redirect(f"/month?year={year}&month={month}")


@router.post("/month/reopen")
def reopen_month(request: Request, year: int = Form(), month: int = Form(),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    period = db.scalar(select(AccountingPeriod).where(
        AccountingPeriod.workspace_id == wid, AccountingPeriod.year == year, AccountingPeriod.month == month,
    ))
    if period:
        period.is_closed = False
        period.closed_at = None
        period.closed_by_id = None
        db.commit()
    flash(request, "Mês reaberto.")
    return redirect(f"/month?year={year}&month={month}")


@router.post("/transactions/{item_id}/confirm")
def transaction_confirm(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    item = db.scalar(select(Transaction).where(
        Transaction.id == item_id, Transaction.workspace_id == wid, Transaction.status == TransactionStatus.pending
    ))
    if not item:
        raise HTTPException(404)
    target_year = item.competence_year or item.transaction_date.year
    target_month = item.competence_month or item.transaction_date.month
    referer = request.headers.get("referer", "")
    parsed = urlparse(referer)
    if parsed.path == "/month":
        params = parse_qs(parsed.query)
        try:
            target_year = int(params.get("year", [target_year])[0])
            target_month = int(params.get("month", [target_month])[0])
        except (TypeError, ValueError):
            pass
    while db.scalar(select(AccountingPeriod.id).where(
        AccountingPeriod.workspace_id == wid, AccountingPeriod.year == target_year,
        AccountingPeriod.month == target_month, AccountingPeriod.is_closed.is_(True),
    )):
        target_month += 1
        if target_month == 13:
            target_month = 1
            target_year += 1
    item.competence_year = target_year
    item.competence_month = target_month
    item.status = TransactionStatus.paid
    db.commit()
    flash(request, f"Lançamento confirmado na competência {target_month:02d}/{target_year}.")
    if parsed.path == "/month":
        return redirect(f"/month?year={target_year}&month={target_month}")
    return redirect("/future")


@router.get("/transactions/new", response_class=HTMLResponse)
def transaction_form(request: Request, kind: str = "expense", return_to: str | None = None,
        return_year: int | None = None, return_month: int | None = None,
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    return render(request, "transactions/form.html", user=user, kind=kind, today=date.today(),
        accounts=visible_accounts(user, wid, db),
        cards=db.scalars(select(Card).where(Card.workspace_id == wid, Card.is_active)).all(),
        people=visible_people(user, wid, db), return_to=return_to,
        return_year=return_year, return_month=return_month,
        categories=db.scalars(select(Category).where(Category.workspace_id == wid).order_by(Category.parent_name, Category.name)).all())


@router.post("/transactions/new")
def transaction_create(request: Request, transaction_type: TransactionType = Form(), description: str = Form(), amount: Decimal = Form(),
        transaction_date: date = Form(), status: TransactionStatus = Form(), account_id: str | None = Form(None),
        destination_account_id: str | None = Form(None), card_id: str | None = Form(None), person_id: str | None = Form(None),
        category_id: str | None = Form(None), payment_method: str | None = Form(None), notes: str | None = Form(None),
        return_to: str | None = Form(None), return_year: str | None = Form(None),
        return_month: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    account_id = optional_int(account_id)
    destination_account_id = optional_int(destination_account_id)
    card_id = optional_int(card_id)
    person_id = optional_int(person_id)
    category_id = optional_int(category_id)
    return_year = optional_int(return_year)
    return_month = optional_int(return_month)
    competence_year, competence_month = transaction_date.year, transaction_date.month
    if return_to == "month" and return_year and return_month and 1 <= return_month <= 12:
        competence_year, competence_month = return_year, return_month
        while db.scalar(select(AccountingPeriod.id).where(
            AccountingPeriod.workspace_id == wid,
            AccountingPeriod.year == competence_year,
            AccountingPeriod.month == competence_month,
            AccountingPeriod.is_closed.is_(True),
        )):
            competence_month += 1
            if competence_month == 13:
                competence_month, competence_year = 1, competence_year + 1
    if transaction_type != TransactionType.transfer and not person_id:
        flash(request, "Selecione uma área financeira.", "danger")
        return redirect(f"/transactions/new?kind={transaction_type.value}")
    if person_id:
        ensure_area_access(person_id, user, wid, db)
    ensure_account_access(account_id, user, wid, db)
    ensure_account_access(destination_account_id, user, wid, db)
    if card_id:
        card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
        if not card: raise HTTPException(403, "Cartão não permitido")
        ensure_account_access(card.account_id, user, wid, db)
        method = (payment_method or "Crédito").lower()
        status = TransactionStatus.pending if method in ("crédito", "credito") else TransactionStatus.paid
        if status == TransactionStatus.pending:
            competence_year, competence_month = card_purchase_competence(db, card, transaction_date)
        if not account_id: account_id = card.account_id
    if amount <= 0:
        flash(request, "O valor deve ser maior que zero.", "danger")
        return redirect(f"/transactions/new?kind={transaction_type.value}")
    if transaction_type == TransactionType.transfer and (not account_id or not destination_account_id or account_id == destination_account_id):
        flash(request, "Selecione contas de origem e destino diferentes.", "danger")
        return redirect("/transactions/new?kind=transfer")
    db.add(Transaction(
        workspace_id=wid, transaction_type=transaction_type, description=description.strip(), amount=amount,
        transaction_date=transaction_date, status=status, account_id=account_id,
        destination_account_id=destination_account_id, card_id=card_id, person_id=person_id,
        category_id=category_id, payment_method=payment_method, notes=notes, created_by_id=user.id,
        competence_year=competence_year, competence_month=competence_month,
    ))
    db.commit()
    flash(request, "Lançamento registrado.")
    if return_to == "month":
        return redirect(f"/month?year={competence_year}&month={competence_month}")
    return redirect("/transactions")


@router.post("/transactions/{item_id}/delete")
def transaction_delete(item_id: int, request: Request, return_year: int | None = Form(None),
                       return_month: int | None = Form(None),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    item = db.scalar(select(Transaction).where(Transaction.id == item_id, Transaction.workspace_id == wid))
    if not item: raise HTTPException(404)
    if item.person_id:
        ensure_area_access(item.person_id, user, wid, db)
    occurrence = db.scalar(select(RecurrenceOccurrence).where(RecurrenceOccurrence.transaction_id == item.id))
    if occurrence:
        db.delete(occurrence)
    financing = db.scalar(select(Financing).where(Financing.recurrence_rule_id == item.recurrence_rule_id)) \
        if item.recurrence_rule_id else None
    if financing and item.status == TransactionStatus.paid:
        financing.paid_installments = max(0, financing.paid_installments - 1)
        financing.status = "active"
        rule = db.get(RecurrenceRule, financing.recurrence_rule_id)
        if rule:
            rule.is_active = True
            rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{financing.total_installments}"
    db.delete(item); db.commit(); flash(request, "Lançamento removido.")
    if return_year and return_month and 1 <= return_month <= 12:
        return redirect(f"/month?year={return_year}&month={return_month}")
    return redirect("/transactions")


@router.post("/transactions/{item_id}/edit")
def transaction_edit(item_id: int, request: Request, description: str = Form(), amount: Decimal = Form(),
                     transaction_date: date = Form(), category_id: str | None = Form(None),
                     account_id: str | None = Form(None), competence_month: str | None = Form(None),
                     notes: str | None = Form(None), return_year: int | None = Form(None),
                     return_month: int | None = Form(None), db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    item = db.scalar(select(Transaction).where(
        Transaction.id == item_id, Transaction.workspace_id == wid,
        Transaction.status == TransactionStatus.paid,
    ))
    if not item: raise HTTPException(404)
    if item.person_id:
        ensure_area_access(item.person_id, user, wid, db)
    category_value, account_value = optional_int(category_id), optional_int(account_id)
    if account_value:
        account = db.scalar(select(Account).where(
            Account.id == account_value, Account.workspace_id == wid, Account.is_active.is_(True),
        ))
        if not account or (item.person_id and account.person_id != item.person_id):
            flash(request, "Selecione uma fonte pagadora da mesma área financeira.", "danger")
            return redirect(f"/month?year={return_year or item.competence_year}&month={return_month or item.competence_month}")
        ensure_account_access(account_value, user, wid, db)
    target_year = item.competence_year or item.transaction_date.year
    target_month = item.competence_month or item.transaction_date.month
    if competence_month:
        try:
            target_year, target_month = map(int, competence_month.split("-", 1))
            if not 2000 <= target_year <= 2100 or not 1 <= target_month <= 12:
                raise ValueError
        except (TypeError, ValueError):
            flash(request, "Informe um mês de competência válido.", "danger")
            return redirect(f"/month?year={return_year or item.competence_year}&month={return_month or item.competence_month}")
    if category_value and not db.scalar(select(Category.id).where(
        Category.id == category_value, Category.workspace_id == wid,
        Category.kind == item.transaction_type,
    )):
        flash(request, "Selecione uma categoria válida para o lançamento.", "danger")
        return redirect(f"/month?year={return_year or item.competence_year}&month={return_month or item.competence_month}")
    if amount <= 0 or not description.strip():
        flash(request, "Informe descrição e valor válidos.", "danger")
        return redirect(f"/month?year={return_year or item.competence_year}&month={return_month or item.competence_month}")
    item.description = description.strip()
    item.amount = amount
    item.transaction_date = transaction_date
    item.competence_year = target_year
    item.competence_month = target_month
    item.account_id = account_value
    item.category_id = category_value
    item.notes = (notes or "").strip() or None
    db.commit()
    flash(request, "Lançamento atualizado.")
    return redirect(f"/month?year={item.competence_year}&month={item.competence_month}")


def crud_list(request, db, user, model, template):
    wid = current_workspace_id(request, user, db)
    items = db.scalars(select(model).where(model.workspace_id == wid).order_by(model.id.desc())).all()
    return render(request, template, user=user, items=items, accounts=db.scalars(select(Account).where(Account.workspace_id == wid)).all())


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    return render(request, "accounts/index.html", user=user, items=visible_accounts(user, wid, db),
                  people=visible_people(user, wid, db))


@router.post("/accounts")
def account_create(request: Request, name: str = Form(), bank_name: str = Form(""), account_type: AccountType = Form(),
                   person_id: int = Form(), initial_balance: Decimal = Form(0), color: str = Form("#6c5ce7"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db); ensure_area_access(person_id, user, wid, db)
    db.add(Account(workspace_id=wid, person_id=person_id, name=name.strip(), bank_name=bank_name.strip(), account_type=account_type, initial_balance=initial_balance, color=color)); db.commit()
    flash(request, "Conta adicionada."); return redirect("/accounts")


@router.get("/cards", response_class=HTMLResponse)
def cards(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    accounts = visible_accounts(user, wid, db)
    account_ids = [account.id for account in accounts]
    query = select(Card).where(Card.workspace_id == wid)
    if allowed_person_ids(user, db) is not None:
        query = query.where(Card.account_id.in_(account_ids))
    return render(request, "cards/index.html", user=user, items=db.scalars(query.order_by(Card.id.desc())).all(), accounts=accounts)


@router.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(card_id: int, request: Request, year: int | None = None, month: int | None = None,
                view: str = "detailed", db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db); today = date.today()
    selected_year, selected_month = year or today.year, month or today.month
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    items = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid, Transaction.card_id == card.id,
        Transaction.competence_year == selected_year, Transaction.competence_month == selected_month,
        Transaction.status != TransactionStatus.cancelled,
    ).order_by(Transaction.transaction_date, Transaction.id)).all()
    credit = [item for item in items if (item.payment_method or "").lower() in ("crédito", "credito")]
    debit = [item for item in items if item not in credit]
    billing_period = db.scalar(select(CardBillingPeriod).where(
        CardBillingPeriod.card_id == card.id, CardBillingPeriod.year == selected_year,
        CardBillingPeriod.month == selected_month,
    ))
    view = view if view in ("analytic", "detailed") else "detailed"
    grouped = {}
    for item in items:
        display_name = re.sub(r"\s*\(\d+/\d+\)\s*$", "", item.description).strip()
        method = "Crédito" if item in credit else "Débito"
        key = (" ".join(display_name.casefold().split()), method.casefold())
        row = grouped.setdefault(key, {
            "description": display_name, "payment_method": method, "count": 0,
            "total": Decimal(0), "categories": set(), "first_date": item.transaction_date,
            "last_date": item.transaction_date,
        })
        row["count"] += 1
        row["total"] += Decimal(item.amount)
        row["first_date"] = min(row["first_date"], item.transaction_date)
        row["last_date"] = max(row["last_date"], item.transaction_date)
        if item.category:
            row["categories"].add(f"{item.category.parent_name} > {item.category.name}" if item.category.parent_name else item.category.name)
    analytic_rows = sorted(grouped.values(), key=lambda row: (-row["total"], row["description"].casefold()))
    for row in analytic_rows:
        row["average"] = row["total"] / row["count"]
        row["category_label"] = ", ".join(sorted(row["categories"])) or "Sem categoria"
    categories = db.scalars(select(Category).where(
        Category.workspace_id == wid, Category.kind == TransactionType.expense
    ).order_by(Category.parent_name, Category.name)).all()
    return render(request, "cards/detail.html", user=user, card=card, credit=credit, debit=debit,
                  categories=categories, view=view, analytic_rows=analytic_rows,
                  invoice_closed=bool(billing_period and billing_period.is_closed),
                  selected_year=selected_year, selected_month=selected_month,
                  credit_total=sum((Decimal(x.amount) for x in credit), Decimal(0)),
                  debit_total=sum((Decimal(x.amount) for x in debit), Decimal(0)))


@router.post("/cards/{card_id}/expenses/{transaction_id}/edit")
def card_expense_edit(card_id: int, transaction_id: int, request: Request,
                      description: str = Form(), amount: Decimal = Form(), category_id: int = Form(),
                      transaction_date: date = Form(), invoice_month: str | None = Form(None),
                      db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    item = db.scalar(select(Transaction).where(
        Transaction.id == transaction_id, Transaction.card_id == card.id,
        Transaction.workspace_id == wid,
    ))
    if not item: raise HTTPException(404)
    category = db.scalar(select(Category).where(
        Category.id == category_id, Category.workspace_id == wid,
        Category.kind == TransactionType.expense,
    ))
    year, month = item.competence_year, item.competence_month
    if not description.strip() or amount <= 0 or not category:
        flash(request, "Informe descrição, valor e categoria válidos.", "danger")
        return redirect(f"/cards/{card.id}?year={year}&month={month}")
    item.description = description.strip()
    item.amount = amount
    item.category_id = category.id
    item.transaction_date = transaction_date
    is_credit = (item.payment_method or "").lower() in ("crédito", "credito")
    if is_credit and invoice_month:
        try:
            target_year, target_month = map(int, invoice_month.split("-", 1))
            if not 1 <= target_month <= 12 or not 2000 <= target_year <= 2100:
                raise ValueError
        except (TypeError, ValueError):
            flash(request, "Informe uma fatura válida.", "danger")
            return redirect(f"/cards/{card.id}?year={year}&month={month}&view=detailed")
    else:
        target_year, target_month = (
            card_purchase_competence(db, card, transaction_date) if is_credit
            else (transaction_date.year, transaction_date.month)
        )
    item.competence_year = target_year
    item.competence_month = target_month
    if is_credit:
        item.status = (
            TransactionStatus.paid if card_invoice_is_closed(db, card.id, target_year, target_month)
            else TransactionStatus.pending
        )
    db.commit()
    flash(request, "Gasto do cartão atualizado.")
    return redirect(f"/cards/{card.id}?year={target_year}&month={target_month}&view=detailed")


@router.post("/cards/{card_id}/confirm")
def card_confirm(card_id: int, request: Request, year: int = Form(), month: int = Form(),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    items = db.scalars(select(Transaction).where(
        Transaction.card_id == card.id, Transaction.competence_year == year,
        Transaction.competence_month == month, Transaction.status == TransactionStatus.pending,
    )).all()
    for item in items: item.status = TransactionStatus.paid
    period = db.scalar(select(CardBillingPeriod).where(
        CardBillingPeriod.card_id == card.id, CardBillingPeriod.year == year,
        CardBillingPeriod.month == month,
    ))
    if not period:
        period = CardBillingPeriod(workspace_id=wid, card_id=card.id, year=year, month=month)
        db.add(period)
    period.is_closed = True
    period.closed_at = datetime.utcnow()
    period.closed_by_id = user.id
    db.commit(); flash(request, f"Fatura do cartão {card.name} confirmada.")
    return redirect(f"/cards/{card.id}?year={year}&month={month}")


@router.post("/cards/{card_id}/reopen")
def card_reopen(card_id: int, request: Request, year: int = Form(), month: int = Form(),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    period = db.scalar(select(CardBillingPeriod).where(
        CardBillingPeriod.card_id == card.id, CardBillingPeriod.year == year,
        CardBillingPeriod.month == month,
    ))
    if period:
        period.is_closed = False
        period.closed_at = None
        period.closed_by_id = None
        db.commit()
    flash(request, f"Fatura do cartão {card.name} reaberta.")
    return redirect(f"/cards/{card.id}?year={year}&month={month}&view=detailed")


@router.post("/cards/{card_id}/expenses/{transaction_id}/delete")
def card_expense_delete(card_id: int, transaction_id: int, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
    item = db.scalar(select(Transaction).where(Transaction.id == transaction_id, Transaction.card_id == card.id))
    if not item: raise HTTPException(404)
    year, month = item.competence_year, item.competence_month
    db.delete(item); db.commit(); flash(request, "Gasto estornado e removido do cartão.")
    return redirect(f"/cards/{card.id}?year={year}&month={month}")


@router.post("/cards")
def card_create(request: Request, name: str = Form(), brand: str = Form(""), account_id: str | None = Form(None), credit_limit: Decimal = Form(0), closing_day: int = Form(), due_day: int = Form(), color: str = Form("#1e293b"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not 1 <= closing_day <= 31 or not 1 <= due_day <= 31: flash(request, "Os dias devem estar entre 1 e 31.", "danger"); return redirect("/cards")
    wid = current_workspace_id(request, user, db); account_value = optional_int(account_id); ensure_account_access(account_value, user, wid, db)
    db.add(Card(workspace_id=wid, name=name.strip(), brand=brand.strip(), account_id=account_value, credit_limit=credit_limit, closing_day=closing_day, due_day=due_day, color=color)); db.commit()
    flash(request, "Cartão adicionado."); return redirect("/cards")


@router.get("/people", response_class=HTMLResponse)
def people(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    return render(request, "people/index.html", user=user, items=visible_people(user, wid, db),
                  can_manage=user.is_super_admin or user.account_role == AccountRole.admin)


@router.post("/people")
def person_create(request: Request, name: str = Form(), person_type: PersonType = Form(PersonType.personal), db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_account_admin(user)
    area = Person(workspace_id=current_workspace_id(request, user, db), name=name.strip(), person_type=person_type)
    db.add(area); db.flush(); user.default_person_id = area.id; db.commit(); flash(request, "Área adicionada e definida como padrão."); return redirect("/people")


@router.post("/people/{person_id}/default")
def person_set_default(person_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    ensure_area_access(person_id, user, wid, db)
    user.default_person_id = person_id
    db.commit(); flash(request, "Área padrão alterada.")
    return redirect("/people")


@router.get("/categories", response_class=HTMLResponse)
def categories(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return crud_list(request, db, user, Category, "categories/index.html")


@router.post("/categories")
def category_create(request: Request, name: str = Form(), parent_name: str | None = Form(None), kind: TransactionType = Form(), color: str = Form("#64748b"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.add(Category(workspace_id=current_workspace_id(request, user, db), name=name.strip(), parent_name=parent_name.strip() if parent_name else None, kind=kind, color=color)); db.commit(); flash(request, "Categoria adicionada."); return redirect("/categories")


@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    telegram_link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    memories = db.scalars(select(UserMemory).where(
        UserMemory.user_id == user.id, UserMemory.is_active.is_(True)
    ).order_by(UserMemory.updated_at.desc())).all()
    return render(request, "profile/index.html", user=user, telegram_link=telegram_link,
                  pairing_ttl_minutes=PAIRING_TTL_MINUTES, memories=memories)


@router.post("/profile/memories/{memory_id}/delete")
def memory_delete(memory_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    memory = db.scalar(select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user.id))
    if not memory:
        raise HTTPException(404)
    memory.is_active = False
    db.commit()
    flash(request, "Memória removida da Vitoria.")
    return redirect("/profile")


@router.post("/profile/telegram/code")
def telegram_pairing_code(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if db.scalar(select(TelegramLink.id).where(TelegramLink.user_id == user.id)):
        flash(request, "Seu Telegram ja esta associado. Desvincule-o antes de gerar outro codigo.", "warning")
        return redirect("/profile")
    code = create_pairing_code(db, user)
    request.session["telegram_pairing_code"] = code
    return redirect("/profile")


@router.post("/profile/telegram/unlink")
def telegram_unlink(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.session.pop("telegram_pairing_code", None)
    if unlink_telegram(db, user):
        flash(request, "Telegram desvinculado com sucesso.")
    else:
        flash(request, "Nenhum Telegram estava associado.", "warning")
    return redirect("/profile")


@router.post("/profile/password")
def change_password(request: Request, current_password: str = Form(), new_password: str = Form(), confirm_password: str = Form(), db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(current_password, user.password_hash): flash(request, "A senha atual está incorreta.", "danger")
    elif len(new_password) < 8: flash(request, "A nova senha deve ter pelo menos 8 caracteres.", "danger")
    elif new_password != confirm_password: flash(request, "A confirmação não confere.", "danger")
    else:
        user.password_hash = hash_password(new_password); db.commit(); flash(request, "Senha alterada com sucesso.")
    return redirect("/profile")


@router.get("/account/users", response_class=HTMLResponse)
def account_users(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_account_admin(user)
    wid = current_workspace_id(request, user, db)
    users = db.scalars(select(User).where(User.system_account_id == user.system_account_id).order_by(User.full_name)).all()
    areas = db.scalars(select(Person).where(Person.workspace_id == wid, Person.is_active).order_by(Person.name)).all()
    access_rows = db.execute(select(UserPersonAccess.user_id, UserPersonAccess.person_id)).all()
    access = {}
    for user_id, person_id in access_rows:
        access.setdefault(user_id, set()).add(person_id)
    return render(request, "admin/users.html", user=user, users=users, areas=areas, access=access)


@router.post("/account/users")
def account_user_create(request: Request, username: str = Form(), full_name: str = Form(), password: str = Form(),
        account_role: AccountRole = Form(AccountRole.member), area_ids: list[int] = Form(default=[]),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_account_admin(user)
    wid = current_workspace_id(request, user, db)
    if len(password) < 6:
        flash(request, "A senha deve ter pelo menos 6 caracteres.", "danger")
        return redirect("/account/users")
    if db.scalar(select(User).where(User.username == username.strip())):
        flash(request, "Este nome de usuário já existe.", "danger")
        return redirect("/account/users")
    new_user = User(system_account_id=user.system_account_id, username=username.strip(), full_name=full_name.strip(),
                    password_hash=hash_password(password), account_role=account_role)
    db.add(new_user); db.flush()
    role = MemberRole.admin if account_role == AccountRole.admin else MemberRole.editor
    db.add(WorkspaceMember(workspace_id=wid, user_id=new_user.id, role=role))
    if account_role != AccountRole.admin:
        valid_ids = db.scalars(select(Person.id).where(Person.workspace_id == wid, Person.id.in_(area_ids))).all() if area_ids else []
        db.add_all([UserPersonAccess(user_id=new_user.id, person_id=area_id) for area_id in valid_ids])
        new_user.default_person_id = valid_ids[0] if valid_ids else None
    db.commit(); flash(request, "Usuário adicionado à conta.")
    return redirect("/account/users")


@router.post("/account/users/{target_id}/access")
def account_user_access(target_id: int, request: Request, area_ids: list[int] = Form(default=[]),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_account_admin(user)
    target = db.scalar(select(User).where(User.id == target_id, User.system_account_id == user.system_account_id))
    if not target: raise HTTPException(404)
    db.execute(delete(UserPersonAccess).where(UserPersonAccess.user_id == target.id))
    if target.account_role != AccountRole.admin:
        wid = current_workspace_id(request, user, db)
        valid_ids = db.scalars(select(Person.id).where(Person.workspace_id == wid, Person.id.in_(area_ids))).all() if area_ids else []
        db.add_all([UserPersonAccess(user_id=target.id, person_id=area_id) for area_id in valid_ids])
        if target.default_person_id not in valid_ids:
            target.default_person_id = valid_ids[0] if valid_ids else None
    db.commit(); flash(request, "Permissões atualizadas.")
    return redirect("/account/users")


@router.get("/system/settings", response_class=HTMLResponse)
def system_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    values = {item.key: item.value for item in db.scalars(select(SystemSetting)).all()}
    try: models = json.loads(values.get("deepinfra_models", "[]"))
    except json.JSONDecodeError: models = []
    return render(request, "admin/system_settings.html", user=user, values=values, models=models)


@router.post("/system/settings")
def system_settings_save(request: Request, deepinfra_api_key: str = Form(""), deepinfra_model: str = Form(""),
        telegram_bot_token: str = Form(""),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    if deepinfra_api_key: set_system_setting(db, "deepinfra_api_key", deepinfra_api_key, True)
    if deepinfra_model: set_system_setting(db, "deepinfra_model", deepinfra_model)
    if telegram_bot_token: set_system_setting(db, "telegram_bot_token", telegram_bot_token, True)
    db.commit(); flash(request, "Configurações globais atualizadas.")
    return redirect("/system/settings")


@router.post("/system/deepinfra/models")
def deepinfra_models_refresh(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    token_setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "deepinfra_api_key"))
    if not token_setting or not token_setting.value:
        flash(request, "Salve a API key da DeepInfra antes de atualizar os modelos.", "danger")
        return redirect("/system/settings")
    if looks_like_telegram_token(token_setting.value):
        flash(request, "A chave informada tem formato de token do Telegram, não de API key da DeepInfra.", "danger")
        return redirect("/system/settings")
    try:
        response = httpx.get("https://api.deepinfra.com/v1/models",
            headers={"Authorization": f"Bearer {token_setting.value}"}, timeout=20.0)
        response.raise_for_status()
        models = sorted({item["id"] for item in response.json().get("data", []) if item.get("id")})
        set_system_setting(db, "deepinfra_models", json.dumps(models))
        db.commit(); flash(request, f"{len(models)} modelos carregados da DeepInfra.")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        flash(request, f"Não foi possível atualizar os modelos da DeepInfra: {exc}", "danger")
    return redirect("/system/settings")


@router.post("/system/deepinfra/test")
def deepinfra_connection_test(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    settings = {item.key: item.value for item in db.scalars(select(SystemSetting)).all()}
    token, model = settings.get("deepinfra_api_key"), settings.get("deepinfra_model")
    if not token or not model:
        flash(request, "Configure a API key e o modelo da DeepInfra antes do teste.", "danger")
        return redirect("/system/settings")
    if looks_like_telegram_token(token):
        flash(request, "O valor salvo como DeepInfra parece ser um token do Telegram.", "danger")
        return redirect("/system/settings")
    try:
        response = httpx.post("https://api.deepinfra.com/v1/openai/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "Responda apenas OK."}], "max_tokens": 8},
            timeout=30.0)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"].strip()[:100]
        flash(request, f"DeepInfra conectada. Resposta do modelo: {answer}")
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        flash(request, f"Falha no teste da DeepInfra: {exc}", "danger")
    return redirect("/system/settings")


@router.post("/system/telegram/test")
def telegram_connection_test(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    token_setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "telegram_bot_token"))
    if not token_setting or not token_setting.value:
        flash(request, "Configure e salve o token do Telegram antes do teste.", "danger")
        return redirect("/system/settings")
    try:
        response = httpx.get(f"https://api.telegram.org/bot{token_setting.value}/getMe", timeout=20.0)
        response.raise_for_status(); payload = response.json()
        if not payload.get("ok"): raise ValueError(payload.get("description", "Resposta inválida"))
        username = payload.get("result", {}).get("username", "sem username")
        flash(request, f"Telegram conectado com sucesso ao bot @{username}.")
    except (httpx.HTTPError, ValueError) as exc:
        flash(request, f"Falha no teste do Telegram: {exc}", "danger")
    return redirect("/system/settings")


@router.get("/system/accounts", response_class=HTMLResponse)
def system_accounts(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    accounts = db.scalars(select(SystemAccount).order_by(SystemAccount.name)).all()
    admins = {item.system_account_id: item for item in db.scalars(select(User).where(User.account_role == AccountRole.admin)).all()}
    return render(request, "admin/accounts.html", user=user, accounts=accounts, admins=admins)


@router.get("/system/accounts/{account_id}/edit", response_class=HTMLResponse)
def system_account_edit(account_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    account = db.get(SystemAccount, account_id)
    if not account: raise HTTPException(404)
    admin = db.scalar(select(User).where(
        User.system_account_id == account.id, User.account_role == AccountRole.admin
    ).order_by(User.id))
    return render(request, "admin/account_edit.html", user=user, account=account, admin=admin,
                  is_own_account=account.id == user.system_account_id)


@router.post("/system/accounts/{account_id}/edit")
def system_account_update(account_id: int, request: Request, account_name: str = Form(),
        is_super_account: bool = Form(False), is_active: bool = Form(False),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    account = db.get(SystemAccount, account_id)
    if not account: raise HTTPException(404)
    if account.id == user.system_account_id and not is_active:
        flash(request, "Você não pode desativar a própria conta.", "danger")
        return redirect(f"/system/accounts/{account.id}/edit")
    account.name = account_name.strip()
    account.is_super_account = is_super_account
    account.is_active = is_active
    db.commit(); flash(request, "Conta atualizada.")
    return redirect("/system/accounts")


@router.post("/system/accounts")
def system_account_create(request: Request, account_name: str = Form(), is_super_account: bool = Form(False),
        admin_name: str = Form(), admin_username: str = Form(), admin_password: str = Form(),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_super_admin(user)
    if len(admin_password) < 6:
        flash(request, "A senha inicial deve ter pelo menos 6 caracteres.", "danger"); return redirect("/system/accounts")
    if db.scalar(select(User).where(User.username == admin_username.strip())):
        flash(request, "Este nome de usuário já existe.", "danger"); return redirect("/system/accounts")
    account = SystemAccount(name=account_name.strip(), is_super_account=is_super_account)
    db.add(account); db.flush()
    workspace = Workspace(system_account_id=account.id, name="Principal")
    admin = User(system_account_id=account.id, username=admin_username.strip(), full_name=admin_name.strip(),
                 password_hash=hash_password(admin_password), account_role=AccountRole.admin)
    db.add_all([workspace, admin]); db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id, role=MemberRole.admin))
    seed_categories(db, workspace.id)
    db.commit(); flash(request, "Conta e administrador inicial criados.")
    return redirect("/system/accounts")
