from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import allowed_person_ids, current_user, current_workspace_id
from app.models import (
    Account, AccountRole, AccountType, Card, Category, Person, PersonType, SystemAccount, SystemSetting,
    Transaction, TransactionStatus, TransactionType, User, UserPersonAccess, WorkspaceMember,
    RecurrenceRule, RecurrenceOccurrence, AccountingPeriod, MemberRole, Workspace,
)
from app.security import hash_password, verify_password
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
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    allowed = allowed_person_ids(user, db)
    area_condition = True if allowed is None else Transaction.person_id.in_(allowed)
    today = date.today()
    month_start = today.replace(day=1)
    month_end = date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1)
    rows = db.execute(
        select(
            Transaction.transaction_type,
            func.coalesce(func.sum(Transaction.amount), 0),
        ).where(
            Transaction.workspace_id == wid,
            area_condition,
            Transaction.competence_year == today.year,
            Transaction.competence_month == today.month,
            Transaction.status == TransactionStatus.paid,
        ).group_by(Transaction.transaction_type)
    ).all()
    totals = {kind.value: Decimal(total) for kind, total in rows}
    incomes = totals.get("income", Decimal(0))
    expenses = totals.get("expense", Decimal(0))
    commitments = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.workspace_id == wid,
        area_condition,
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.pending,
        Transaction.competence_year == today.year,
        Transaction.competence_month == today.month,
    ))
    account_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(Account.workspace_id == wid)
    if allowed is not None: account_query = account_query.where(Account.person_id.in_(allowed))
    account_initial = db.scalar(account_query)
    paid_flow = db.scalar(select(func.coalesce(func.sum(case(
        (Transaction.transaction_type == TransactionType.income, Transaction.amount),
        (Transaction.transaction_type == TransactionType.expense, -Transaction.amount), else_=0,
    )), 0)).where(Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid))
    balance = Decimal(account_initial) + Decimal(paid_flow)
    recent = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid
    ).order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).limit(8)).all()
    category_rows = db.execute(select(Category.name, func.sum(Transaction.amount)).join(Transaction, Transaction.category_id == Category.id).where(
        Transaction.workspace_id == wid,
        area_condition,
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.paid,
        Transaction.competence_year == today.year,
        Transaction.competence_month == today.month,
    ).group_by(Category.name).order_by(func.sum(Transaction.amount).desc()).limit(6)).all()
    return render(request, "dashboard.html", user=user, balance=balance, incomes=incomes, expenses=expenses,
                  commitments=commitments, free_balance=balance - Decimal(commitments), recent=recent,
                  chart_labels=[r[0] for r in category_rows], chart_values=[float(r[1]) for r in category_rows], today=today)


@router.get("/analysis", response_class=HTMLResponse)
def financial_analysis(request: Request, history: int = 12, horizon: int = 12, area_id: int | None = None,
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    history = history if history in (3, 6, 12, 24) else 12
    horizon = horizon if horizon in (3, 6, 12, 24, 36, 60) else 12
    wid = current_workspace_id(request, user, db); today = date.today(); areas = visible_people(user, wid, db)
    allowed_ids = {area.id for area in areas}
    if area_id not in allowed_ids: area_id = None
    area_condition = Transaction.person_id == area_id if area_id else (
        True if allowed_person_ids(user, db) is None else Transaction.person_id.in_(allowed_ids)
    )
    history_start = shift_month(today.replace(day=1), -(history - 1))
    items = db.scalars(select(Transaction).where(
        Transaction.workspace_id == wid, area_condition, Transaction.status == TransactionStatus.paid,
        Transaction.transaction_date <= today, Transaction.transaction_date >= history_start,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    ).order_by(Transaction.transaction_date)).all()
    incomes = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.income), Decimal(0))
    expenses = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.expense), Decimal(0))
    credit_expenses = sum((Decimal(item.amount) for item in items if item.transaction_type == TransactionType.expense
                           and (item.payment_method or "").lower() in ("crédito", "credito")), Decimal(0))
    category_totals, sector_totals = {}, {}
    monthly = {}
    for item in items:
        key = (item.transaction_date.year, item.transaction_date.month)
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
    return render(request, "cards/expense_form.html", user=user, cards=cards)


@router.post("/card-expenses/new")
def card_expense_create(request: Request, card_id: int = Form(), description: str = Form(),
        amount: Decimal = Form(), payment_method: str = Form(), purchase_type: str = Form("cash"),
        installments: int = Form(1), db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    card = db.scalar(select(Card).where(Card.id == card_id, Card.workspace_id == wid, Card.is_active))
    if not card: raise HTTPException(404)
    ensure_account_access(card.account_id, user, wid, db)
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
    today = date.today()
    for index in range(count):
        month_index = today.month - 1 + index
        year, month = today.year + month_index // 12, month_index % 12 + 1
        tx_date = date(year, month, min(today.day, monthrange(year, month)[1]))
        cents = base_cents + (remainder if index == count - 1 else 0)
        label = f"{description.strip()} ({index + 1}/{count})" if count > 1 else description.strip()
        db.add(Transaction(
            workspace_id=wid, transaction_type=TransactionType.expense, description=label,
            amount=Decimal(cents) / 100, transaction_date=tx_date,
            status=TransactionStatus.pending if is_credit else TransactionStatus.paid,
            account_id=card.account_id, card_id=card.id, person_id=card.account.person_id,
            payment_method="Crédito" if is_credit else "Débito", created_by_id=user.id,
            competence_year=year, competence_month=month,
        ))
    db.commit()
    flash(request, f"Gasto no cartão registrado{' em ' + str(count) + ' parcelas' if count > 1 else ''}.")
    return redirect(f"/cards/{card.id}?year={today.year}&month={today.month}")


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


@router.post("/recurrences/{rule_id}/confirm")
def recurring_income_confirm(rule_id: int, request: Request, year: int = Form(), month: int = Form(),
        confirmed_amount: Decimal = Form(),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    rule = db.scalar(select(RecurrenceRule).where(
        RecurrenceRule.id == rule_id, RecurrenceRule.workspace_id == wid, RecurrenceRule.is_active.is_(True)
    ))
    existing = db.scalar(select(RecurrenceOccurrence).where(
        RecurrenceOccurrence.recurrence_rule_id == rule_id,
        RecurrenceOccurrence.year == year, RecurrenceOccurrence.month == month,
    ))
    if not rule or existing:
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
    transaction = Transaction(
        workspace_id=wid, transaction_type=rule.transaction_type, description=rule.description,
        amount=confirmed_amount, transaction_date=date.today(), status=TransactionStatus.paid,
        account_id=rule.account_id, person_id=rule.person_id, category_id=rule.category_id,
        payment_method=rule.payment_method, notes=rule.notes, created_by_id=user.id,
        recurrence_rule_id=rule.id, competence_year=target_year, competence_month=target_month,
    )
    db.add(transaction); db.flush()
    db.add(RecurrenceOccurrence(recurrence_rule_id=rule.id, year=year, month=month,
                                status="confirmed", transaction_id=transaction.id))
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
    resolved = {(occurrence.recurrence_rule_id, occurrence.month) for occurrence in occurrences}
    virtual_by_month = {number: [] for number in range(1, 13)}
    for rule in recurring_rules:
        created = rule.created_at.date()
        for number in range(1, 13):
            if (selected_year, number) < (created.year, created.month):
                continue
            if (rule.id, number) in resolved:
                continue
            virtual_by_month[number].append(rule)
            key = "income_pending" if rule.transaction_type == TransactionType.income else "expense_pending"
            months[number - 1][key] += Decimal(rule.amount)
    running_balance = Decimal(0)
    for summary in months:
        summary["realized"] = summary["income_paid"] - summary["expense_paid"]
        summary["projected"] = (
            summary["income_paid"] + summary["income_pending"]
            - summary["expense_paid"] - summary["expense_pending"]
        )
        running_balance += summary["realized"]
        summary["running_balance"] = running_balance
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
    initial_query = select(func.coalesce(func.sum(Account.initial_balance), 0)).where(Account.workspace_id == wid)
    if allowed is not None: initial_query = initial_query.where(Account.person_id.in_(allowed))
    initial_balance = Decimal(db.scalar(initial_query))
    previous_query = select(Transaction).where(
        Transaction.workspace_id == wid,
        ((Transaction.competence_year < selected_year) | ((Transaction.competence_year == selected_year) & (Transaction.competence_month < selected_month))),
        Transaction.status == TransactionStatus.paid,
    )
    previous_items = db.scalars(restrict_transactions(previous_query, user, db)).all()
    opening_balance = initial_balance + sum(
        (Decimal(item.amount) if item.transaction_type == TransactionType.income else -Decimal(item.amount)
         if item.transaction_type == TransactionType.expense else Decimal(0)) for item in previous_items
    )
    selected_summary["opening_balance"] = opening_balance
    selected_summary["closing_balance"] = opening_balance + selected_summary["realized"]
    selected_summary["projected_balance"] = opening_balance + selected_summary["projected"]
    income_items = [item for item in selected_items if item.transaction_type == TransactionType.income]
    expense_items = [item for item in selected_items if item.transaction_type == TransactionType.expense and not item.card_id]
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
    return render(request, "family/index.html", user=user, months=months, selected_year=selected_year,
                  selected_month=selected_month, confirmed=confirmed, forecasts=forecasts, people=people,
                  selected_summary=selected_summary, income_items=income_items, expense_items=expense_items,
                  period_closed=bool(period and period.is_closed), visible_months=visible_months,
                  recurring_pending=recurring_pending, fixed_expense_pending=fixed_expense_pending,
                  card_groups=card_groups)


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
def transaction_form(request: Request, kind: str = "expense", db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    return render(request, "transactions/form.html", user=user, kind=kind, today=date.today(),
        accounts=visible_accounts(user, wid, db),
        cards=db.scalars(select(Card).where(Card.workspace_id == wid, Card.is_active)).all(),
        people=visible_people(user, wid, db),
        categories=db.scalars(select(Category).where(Category.workspace_id == wid).order_by(Category.parent_name, Category.name)).all())


@router.post("/transactions/new")
def transaction_create(request: Request, transaction_type: TransactionType = Form(), description: str = Form(), amount: Decimal = Form(),
        transaction_date: date = Form(), status: TransactionStatus = Form(), account_id: str | None = Form(None),
        destination_account_id: str | None = Form(None), card_id: str | None = Form(None), person_id: str | None = Form(None),
        category_id: str | None = Form(None), payment_method: str | None = Form(None), notes: str | None = Form(None),
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    account_id = optional_int(account_id)
    destination_account_id = optional_int(destination_account_id)
    card_id = optional_int(card_id)
    person_id = optional_int(person_id)
    category_id = optional_int(category_id)
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
        competence_year=transaction_date.year, competence_month=transaction_date.month,
    ))
    db.commit()
    flash(request, "Lançamento registrado.")
    return redirect("/transactions")


@router.post("/transactions/{item_id}/delete")
def transaction_delete(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    wid = current_workspace_id(request, user, db)
    item = db.scalar(select(Transaction).where(Transaction.id == item_id, Transaction.workspace_id == wid))
    if not item: raise HTTPException(404)
    db.delete(item); db.commit(); flash(request, "Lançamento removido.")
    return redirect("/transactions")


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
                db: Session = Depends(get_db), user: User = Depends(current_user)):
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
    return render(request, "cards/detail.html", user=user, card=card, credit=credit, debit=debit,
                  selected_year=selected_year, selected_month=selected_month,
                  credit_total=sum((Decimal(x.amount) for x in credit), Decimal(0)),
                  debit_total=sum((Decimal(x.amount) for x in debit), Decimal(0)))


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
    db.commit(); flash(request, f"Fatura do cartão {card.name} confirmada.")
    return redirect(f"/cards/{card.id}?year={year}&month={month}")


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
def profile(request: Request, user: User = Depends(current_user)):
    return render(request, "profile/index.html", user=user)


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
