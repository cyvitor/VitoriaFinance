from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account, Card, Category, Financing, FinancingAmortization, Person, RecurrenceOccurrence, RecurrenceRule,
    FuelFillup, TelegramPendingAction, Transaction, TransactionStatus, TransactionType, User, Vehicle,
)
from app.services.access_context import UserAccessContext
from app.services.card_billing import card_invoice_is_closed, card_purchase_competence
from app.services.monthly_projection import monthly_budget_positions, projection_budget_remaining
from app.services.telegram_expenses import (
    ExpenseInput, cancel_expense_draft, confirm_expense_draft, create_expense_draft,
    finish_processing_queue_item, format_draft, get_active_draft,
    pop_next_queued_expense, queued_expense_count,
    replace_expense_queue, suggest_expense_category, update_expense_draft,
)


class ToolError(ValueError):
    pass


def _money(value) -> str:
    return f"{Decimal(value or 0):.2f}"


def _decimal(value, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ToolError(f"{field} invalido") from exc
    if result < 0:
        raise ToolError(f"{field} nao pode ser negativo")
    return result


def _date(value, default: date | None = None) -> date:
    if not value:
        return default or date.today()
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError("data invalida") from exc


def _invoice_month(value) -> tuple[int, int] | None:
    if value in (None, ""):
        return None
    try:
        year, month = map(int, str(value).split("-", 1))
    except (TypeError, ValueError) as exc:
        raise ToolError("fatura invalida; use YYYY-MM") from exc
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        raise ToolError("fatura invalida; use YYYY-MM")
    return year, month


def _base_transaction_filters(context: UserAccessContext):
    return (
        Transaction.workspace_id.in_(context.workspace_ids),
        Transaction.person_id.in_(context.allowed_person_ids),
        Transaction.status != TransactionStatus.cancelled,
    )


def _resolve_person(db: Session, context: UserAccessContext, name: str | None) -> Person | None:
    if not context.allowed_person_ids:
        raise ToolError("Usuario sem areas financeiras permitidas")
    if not name:
        if context.default_person_id:
            return db.get(Person, context.default_person_id)
        if len(context.allowed_person_ids) == 1:
            return db.get(Person, context.allowed_person_ids[0])
        return None
    people = db.scalars(select(Person).where(Person.id.in_(context.allowed_person_ids))).all()
    normalized = name.casefold().strip()
    exact = [person for person in people if person.name.casefold() == normalized]
    if len(exact) == 1:
        return exact[0]
    partial = [person for person in people if normalized in person.name.casefold() or person.name.casefold() in normalized]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ToolError(f"Area financeira '{name}' nao encontrada ou nao permitida")
    raise ToolError("Area ambigua: " + ", ".join(person.name for person in partial))


def _period(args: dict) -> tuple[date, date]:
    today = date.today()
    start = _date(args.get("start_date"), today.replace(day=1))
    if args.get("end_date"):
        end = _date(args["end_date"])
    else:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    if end < start:
        raise ToolError("Periodo invalido")
    return start, end


def list_financial_areas(db: Session, context: UserAccessContext, args: dict) -> dict:
    people = db.scalars(select(Person).where(Person.id.in_(context.allowed_person_ids)).order_by(Person.name)).all()
    return {"areas": [{"name": item.name, "type": item.person_type.value,
                        "default": item.id == context.default_person_id} for item in people]}


def list_accounts(db: Session, context: UserAccessContext, args: dict) -> dict:
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    query = select(Account).where(
        Account.workspace_id.in_(context.workspace_ids), Account.person_id.in_(context.allowed_person_ids),
        Account.is_active.is_(True),
    )
    if person:
        query = query.where(Account.person_id == person.id)
    accounts = db.scalars(query.order_by(Account.name)).all()
    result = []
    for account in accounts:
        income = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *_base_transaction_filters(context), Transaction.account_id == account.id,
            Transaction.transaction_type == TransactionType.income, Transaction.status == TransactionStatus.paid,
        ))
        expense = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *_base_transaction_filters(context), Transaction.account_id == account.id,
            Transaction.transaction_type == TransactionType.expense, Transaction.status == TransactionStatus.paid,
        ))
        transfers_in = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.workspace_id.in_(context.workspace_ids), Transaction.destination_account_id == account.id,
            Transaction.transaction_type == TransactionType.transfer, Transaction.status == TransactionStatus.paid,
        ))
        transfers_out = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.workspace_id.in_(context.workspace_ids), Transaction.account_id == account.id,
            Transaction.transaction_type == TransactionType.transfer, Transaction.status == TransactionStatus.paid,
        ))
        result.append({"name": account.name, "bank": account.bank_name, "area": account.person.name if account.person else None,
                       "estimated_balance": _money(Decimal(account.initial_balance) + income - expense + transfers_in - transfers_out)})
    return {"accounts": result}


def list_cards(db: Session, context: UserAccessContext, args: dict) -> dict:
    query = select(Card).join(Account, Account.id == Card.account_id).where(
        Account.person_id.in_(context.allowed_person_ids), Card.is_active.is_(True)
    )
    cards = db.scalars(query.order_by(Card.name)).all()
    today = date.today(); start = today.replace(day=1)
    result = []
    for card in cards:
        spent = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *_base_transaction_filters(context), Transaction.card_id == card.id,
            Transaction.transaction_type == TransactionType.expense,
            Transaction.competence_year == today.year, Transaction.competence_month == today.month,
        ))
        result.append({"name": card.name, "area": card.account.person.name if card.account and card.account.person else None,
                       "credit_limit": _money(card.credit_limit), "current_month_spending": _money(spent),
                       "available_limit": _money(Decimal(card.credit_limit) - spent),
                       "closing_day": card.closing_day, "due_day": card.due_day})
    return {"month": start.strftime("%Y-%m"), "cards": result}


def list_income_categories(db: Session, context: UserAccessContext, args: dict) -> dict:
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    workspace_ids = [person.workspace_id] if person else context.workspace_ids
    categories = db.scalars(select(Category).where(
        Category.workspace_id.in_(workspace_ids),
        Category.kind == TransactionType.income,
    ).order_by(Category.parent_name, Category.name)).all()
    labels = []
    for item in categories:
        label = f"{item.parent_name} > {item.name}" if item.parent_name else item.name
        if label not in labels:
            labels.append(label)
    return {"categories": labels}


def _resolve_category(db: Session, context: UserAccessContext, value: str | None,
                      kind: TransactionType) -> Category:
    if not value or not str(value).strip():
        raise ToolError("Informe a categoria")
    normalized = " ".join(str(value).casefold().replace(">", " ").split())
    categories = db.scalars(select(Category).where(
        Category.workspace_id.in_(context.workspace_ids), Category.kind == kind,
    )).all()
    exact = [item for item in categories if normalized in {
        item.name.casefold(),
        " ".join(f"{item.parent_name or ''} {item.name}".casefold().split()),
    }]
    if len(exact) == 1:
        return exact[0]
    partial = [item for item in categories if normalized in
               " ".join(f"{item.parent_name or ''} {item.name}".casefold().split())]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ToolError(f"Categoria '{value}' nao encontrada")
    raise ToolError("Categoria ambigua: " + ", ".join(item.name for item in partial))


def query_transactions(db: Session, context: UserAccessContext, args: dict, kind: TransactionType) -> dict:
    start, end = _period(args)
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    query = select(Transaction).where(
        *_base_transaction_filters(context), Transaction.transaction_type == kind,
        Transaction.transaction_date.between(start, end),
    )
    if person:
        query = query.where(Transaction.person_id == person.id)
    category_name = args.get("category")
    if category_name:
        query = query.join(Category, Category.id == Transaction.category_id).where(or_(
            func.lower(Category.name).contains(category_name.casefold()),
            func.lower(Category.parent_name).contains(category_name.casefold()),
        ))
    count = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    total = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.id.in_(select(query.order_by(None).subquery().c.id))
    ))
    all_items = db.scalars(query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())).all()
    breakdown = {}
    for item in all_items:
        method = (item.payment_method or "").casefold()
        if item.card_id and method in ("crédito", "credito", "credit"):
            key = "cartao_credito"
            label = f"Crédito — {item.card.name}" if item.card else "Cartão de crédito"
        elif item.card_id and method in ("débito", "debito", "debit"):
            key = "cartao_debito"
            label = f"Débito — {item.card.name}" if item.card else "Cartão de débito"
        elif item.account_id:
            key = "conta"
            label = f"Conta — {item.account.name}" if item.account else "Conta"
        else:
            key = "nao_informado"
            label = "Forma de pagamento não informada"
        bucket_key = f"{key}:{label}"
        bucket = breakdown.setdefault(bucket_key, {
            "type": key, "label": label, "amount": Decimal(0), "count": 0,
        })
        bucket["amount"] += Decimal(item.amount)
        bucket["count"] += 1
    items = all_items[:20]
    return {
        "type": kind.value, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "area": person.name if person else "todas as areas permitidas",
        "category": category_name, "total": _money(total),
        "count": count,
        "payment_breakdown": [
            {**bucket, "amount": _money(bucket["amount"])} for bucket in breakdown.values()
        ],
        "coverage": {
            "cards": "todos os cartões permitidos, incluindo crédito e débito",
            "accounts": "todas as contas permitidas",
            "statuses": "lançamentos pagos e pendentes; cancelados excluídos",
        },
        "items": [{"date": item.transaction_date.isoformat(), "description": item.description,
                   "amount": _money(item.amount), "area": item.person.name if item.person else None,
                   "category": item.category.name if item.category else None,
                   "payment_method": item.payment_method,
                   "card": item.card.name if item.card else None,
                   "account": item.account.name if item.account else None,
                   "status": item.status.value} for item in items[:20]],
        "truncated": count > 20,
    }


def query_card_spending(db: Session, context: UserAccessContext, args: dict) -> dict:
    start, end = _period(args)
    query = select(Card).join(Account, Account.id == Card.account_id).where(
        Account.person_id.in_(context.allowed_person_ids), Card.is_active.is_(True)
    )
    card_name = str(args.get("card") or "").strip()
    cards = db.scalars(query).all()
    if card_name:
        normalized = card_name.casefold()
        cards = [card for card in cards if normalized in card.name.casefold()]
    if not cards:
        raise ToolError("Cartao nao encontrado ou sem permissao")
    if len(cards) > 1 and card_name:
        raise ToolError("Cartao ambiguo: " + ", ".join(card.name for card in cards))
    card_ids = [card.id for card in cards]
    transactions = db.scalars(select(Transaction).where(
        *_base_transaction_filters(context), Transaction.card_id.in_(card_ids),
        Transaction.transaction_type == TransactionType.expense,
        Transaction.transaction_date.between(start, end),
    ).order_by(Transaction.transaction_date.desc()).limit(20)).all()
    total = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        *_base_transaction_filters(context), Transaction.card_id.in_(card_ids),
        Transaction.transaction_type == TransactionType.expense,
        Transaction.transaction_date.between(start, end),
    ))
    count = db.scalar(select(func.count(Transaction.id)).where(
        *_base_transaction_filters(context), Transaction.card_id.in_(card_ids),
        Transaction.transaction_type == TransactionType.expense,
        Transaction.transaction_date.between(start, end),
    )) or 0
    return {"cards": [card.name for card in cards], "start_date": start.isoformat(), "end_date": end.isoformat(),
            "total": _money(total), "count": count,
            "items": [{"date": item.transaction_date.isoformat(), "description": item.description,
                       "amount": _money(item.amount), "area": item.person.name if item.person else None}
                      for item in transactions], "truncated": count > 20}


def monthly_summary(db: Session, context: UserAccessContext, args: dict) -> dict:
    expense = query_transactions(db, context, args, TransactionType.expense)
    income = query_transactions(db, context, args, TransactionType.income)
    return {"period": {"start": income["start_date"], "end": income["end_date"]},
            "area": expense["area"], "income": income["total"], "expense": expense["total"],
            "free_result": _money(Decimal(income["total"]) - Decimal(expense["total"])),
            "definition": "receitas menos despesas registradas no periodo"}


def query_budgets(db: Session, context: UserAccessContext, args: dict) -> dict:
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    person_ids = (person.id,) if person else context.allowed_person_ids
    reference = _date(args.get("reference_date"), date.today())
    positions = monthly_budget_positions(
        db, workspace_ids=context.workspace_ids, person_ids=person_ids,
        year=reference.year, month=reference.month,
    )
    items = []
    for position in positions:
        impact = position.impact()
        items.append({
            "area": position.budget.person.name,
            "category": (f"{position.budget.category.parent_name} > {position.budget.category.name}"
                         if position.budget.category.parent_name else position.budget.category.name),
            **impact,
        })
    return {
        "competence": f"{reference.year:04d}-{reference.month:02d}",
        "area": person.name if person else "todas as areas permitidas",
        "budgets": items,
        "total_planned": _money(sum((Decimal(item["planned"]) for item in items), Decimal(0))),
        "total_spent": _money(sum((Decimal(item["spent"]) for item in items), Decimal(0))),
        "total_remaining": _money(sum((Decimal(item["remaining"]) for item in items), Decimal(0))),
        "projection_reserve": _money(projection_budget_remaining(positions)),
    }


def query_category_budget(db: Session, context: UserAccessContext, args: dict) -> dict:
    person = _resolve_person(db, context, args.get("area"))
    if not person:
        raise ToolError("Informe a area financeira para consultar o orcamento da categoria")
    category = _resolve_category(db, context, args.get("category"), TransactionType.expense)
    reference = _date(args.get("reference_date"), date.today())
    planned_spending = _decimal(args.get("planned_spending"), "gasto planejado") or Decimal(0)
    positions = monthly_budget_positions(
        db, workspace_ids=context.workspace_ids, person_ids=(person.id,),
        year=reference.year, month=reference.month, category_id=category.id,
    )
    if not positions:
        return {"found": False, "area": person.name,
                "category": f"{category.parent_name} > {category.name}" if category.parent_name else category.name,
                "competence": f"{reference.year:04d}-{reference.month:02d}",
                "message": "Nenhum orcamento ativo para esta categoria"}
    position = positions[0]
    return {
        "found": True, "area": person.name,
        "category": f"{category.parent_name} > {category.name}" if category.parent_name else category.name,
        "competence": f"{reference.year:04d}-{reference.month:02d}",
        **position.impact(planned_spending),
    }


def calculate_free_balance(db: Session, context: UserAccessContext, args: dict) -> dict:
    today = date.today()
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    person_ids = (person.id,) if person else context.allowed_person_ids
    planned_spending = _decimal(args.get("planned_spending"), "gasto planejado") or Decimal(0)
    initial_balance = Decimal(db.scalar(select(func.coalesce(func.sum(Account.initial_balance), 0)).where(
        Account.workspace_id.in_(context.workspace_ids), Account.person_id.in_(person_ids),
        Account.is_active.is_(True),
    )))
    paid_flow = Decimal(db.scalar(select(func.coalesce(func.sum(case(
        (Transaction.transaction_type == TransactionType.income, Transaction.amount),
        (Transaction.transaction_type == TransactionType.expense, -Transaction.amount), else_=0,
    )), 0)).where(
        Transaction.workspace_id.in_(context.workspace_ids), Transaction.person_id.in_(person_ids),
        Transaction.status == TransactionStatus.paid,
    )))
    pending_transactions = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.workspace_id.in_(context.workspace_ids), Transaction.person_id.in_(person_ids),
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.pending,
        Transaction.competence_year == today.year, Transaction.competence_month == today.month,
    )))
    rules = db.scalars(select(RecurrenceRule).where(
        RecurrenceRule.workspace_id.in_(context.workspace_ids), RecurrenceRule.person_id.in_(person_ids),
        RecurrenceRule.transaction_type.in_([TransactionType.income, TransactionType.expense]),
        RecurrenceRule.is_active.is_(True),
    )).all()
    recurring_income = Decimal(0)
    recurring_commitments = Decimal(0)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])
    for rule in rules:
        if rule.start_date and rule.start_date > month_end:
            continue
        occurrence = db.scalar(select(RecurrenceOccurrence).where(
            RecurrenceOccurrence.recurrence_rule_id == rule.id,
            RecurrenceOccurrence.year == today.year, RecurrenceOccurrence.month == today.month,
        ))
        if occurrence and occurrence.status in ("confirmed", "skipped"):
            continue
        if occurrence and occurrence.status == "partial" and occurrence.remaining_amount is not None:
            amount = Decimal(occurrence.remaining_amount)
        else:
            amount = Decimal(rule.amount or 0)
        if rule.transaction_type == TransactionType.income:
            recurring_income += amount
        else:
            recurring_commitments += amount
    month_rows = db.execute(select(
        Transaction.transaction_type, func.coalesce(func.sum(Transaction.amount), 0)
    ).where(
        Transaction.workspace_id.in_(context.workspace_ids), Transaction.person_id.in_(person_ids),
        Transaction.status != TransactionStatus.cancelled,
        Transaction.competence_year == today.year, Transaction.competence_month == today.month,
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    ).group_by(Transaction.transaction_type)).all()
    month_totals = {kind: Decimal(amount) for kind, amount in month_rows}
    projected_income = month_totals.get(TransactionType.income, Decimal(0)) + recurring_income
    projected_expense = month_totals.get(TransactionType.expense, Decimal(0)) + recurring_commitments
    projected_month_result = projected_income - projected_expense
    current_balance = initial_balance + paid_flow
    budget_positions = monthly_budget_positions(
        db, workspace_ids=context.workspace_ids, person_ids=person_ids,
        year=today.year, month=today.month,
    )
    budget_remaining = projection_budget_remaining(budget_positions)
    free_balance = current_balance - pending_transactions - recurring_commitments - budget_remaining
    after_planned_spending = free_balance - planned_spending
    payment_method = str(args.get("payment_method") or "cash").casefold()
    is_credit = payment_method in ("credit", "credito", "crédito", "cartao", "cartão")
    card_context = None
    projected_after_planned = projected_month_result - planned_spending
    if is_credit:
        cards = db.scalars(select(Card).join(Account, Account.id == Card.account_id).where(
            Card.is_active.is_(True), Account.person_id.in_(person_ids),
        ).order_by(Card.name)).all()
        requested_card = str(args.get("card") or "").strip()
        if requested_card:
            normalized = requested_card.casefold()
            cards = [card for card in cards if normalized in card.name.casefold()]
            if len(cards) != 1:
                raise ToolError("Cartao nao encontrado, ambiguo ou sem permissao")
        if not cards:
            raise ToolError("Nenhum cartao permitido foi encontrado")
        competences = {card.name: card_purchase_competence(db, card, today) for card in cards}
        unique_competences = set(competences.values())
        selection_required = len(unique_competences) > 1 and not requested_card
        purchase_competence = next(iter(unique_competences)) if len(unique_competences) == 1 else None
        affects_current_month = purchase_competence == (today.year, today.month) if purchase_competence else None
        projected_after_planned = (
            projected_month_result - planned_spending if affects_current_month is not False
            else projected_month_result
        )
        card_context = {
            "cards": [{"name": card.name, "closing_day": card.closing_day, "due_day": card.due_day,
                       "purchase_competence": f"{competences[card.name][0]:04d}-{competences[card.name][1]:02d}"}
                      for card in cards],
            "card_selection_required": selection_required,
            "purchase_competence": f"{purchase_competence[0]:04d}-{purchase_competence[1]:02d}" if purchase_competence else None,
            "affects_current_month": affects_current_month,
            "next_competence_commitment": _money(planned_spending) if affects_current_month is False else "0.00",
        }
    return {
        "as_of": today.isoformat(), "area": person.name if person else "todas as areas permitidas",
        "current_balance": _money(current_balance),
        "pending_transactions": _money(pending_transactions),
        "unresolved_recurring_commitments": _money(recurring_commitments),
        "budget_remaining": _money(budget_remaining),
        "free_balance": _money(free_balance), "planned_spending": _money(planned_spending),
        "free_balance_after_planned_spending": _money(after_planned_spending),
        "can_afford": after_planned_spending >= 0,
        "payment_method": "credit" if is_credit else "cash",
        "projected_income_month": _money(projected_income),
        "projected_expense_month": _money(projected_expense),
        "projected_month_result": _money(projected_month_result),
        "projected_month_result_after_planned_spending": _money(projected_after_planned),
        "can_close_month": projected_after_planned >= 0,
        "card_context": card_context,
        "definition": {
            "cash": "saldo em conta agora menos despesas pendentes, recorrencias ainda nao resolvidas e reserva orcamentaria",
            "month_projection": "receitas confirmadas e previstas menos despesas confirmadas e previstas da competencia",
        },
        "warning": "Nao inclui transacoes ainda nao cadastradas nem compromissos fora do VitoriaFinance.",
    }


def list_financings(db: Session, context: UserAccessContext, args: dict) -> dict:
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    query = select(Financing).where(
        Financing.workspace_id.in_(context.workspace_ids), Financing.person_id.in_(context.allowed_person_ids)
    )
    if person:
        query = query.where(Financing.person_id == person.id)
    if not args.get("include_paid"):
        query = query.where(Financing.status == "active")
    items = db.scalars(query.order_by(Financing.description)).all()
    return {"financings": [_financing_data(item) for item in items]}


def _financing_data(item: Financing) -> dict:
    return {"description": item.description, "institution": item.institution, "area": item.person.name,
            "status": item.status, "paid_installments": item.paid_installments,
            "total_installments": item.total_installments,
            "remaining_installments": max(0, item.total_installments - item.paid_installments),
            "installment_amount": _money(item.installment_amount),
            "outstanding_balance": _money(item.outstanding_balance) if item.outstanding_balance is not None else None,
            "nominal_interest_rate": str(item.nominal_interest_rate) if item.nominal_interest_rate is not None else None}


def _active_action(db: Session, user_id: int) -> TelegramPendingAction | None:
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id,
        TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]),
    ).order_by(TelegramPendingAction.id.desc()))
    if action and action.expires_at < datetime.utcnow():
        action.status = "expired"; action.resolved_at = datetime.utcnow(); db.commit()
        return None
    return action


def _resolve_income_account(
    db: Session, context: UserAccessContext, person: Person, name: str | None,
) -> tuple[Account | None, list[str]]:
    accounts = db.scalars(select(Account).where(
        Account.workspace_id == person.workspace_id,
        Account.person_id == person.id,
        Account.is_active.is_(True),
    ).order_by(Account.name)).all()
    if not name:
        return (accounts[0], []) if len(accounts) == 1 else (None, [item.name for item in accounts])
    normalized = name.casefold().strip()
    exact = [item for item in accounts if item.name.casefold() == normalized]
    matches = exact or [
        item for item in accounts
        if normalized in item.name.casefold() or item.name.casefold() in normalized
    ]
    if len(matches) == 1:
        return matches[0], []
    if not matches:
        raise ToolError(f"Conta '{name}' nao encontrada ou nao permitida nesta area")
    raise ToolError("Conta ambigua: " + ", ".join(item.name for item in matches))


def _resolve_income_category(
    db: Session, workspace_id: int, name: str | None, payment_method: str | None,
) -> Category | None:
    requested = name
    if not requested and (payment_method or "").casefold() == "pix":
        requested = "PIX"
    if not requested:
        return None
    categories = db.scalars(select(Category).where(
        Category.workspace_id == workspace_id,
        Category.kind == TransactionType.income,
    )).all()
    normalized = requested.casefold().strip()
    exact = [
        item for item in categories
        if item.name.casefold() == normalized
        or f"{item.parent_name or ''} > {item.name}".casefold().strip(" >") == normalized
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        item for item in categories
        if normalized in item.name.casefold()
        or (item.parent_name and normalized in item.parent_name.casefold())
    ]
    if len(partial) == 1:
        return partial[0]
    if not exact and not partial:
        raise ToolError(f"Categoria de receita '{requested}' nao encontrada")
    raise ToolError("Categoria de receita ambigua: " + ", ".join(
        f"{item.parent_name} > {item.name}" if item.parent_name else item.name
        for item in (exact or partial)
    ))


def prepare_income(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para registrar receitas")
    action = _active_action(db, context.user_id)
    payload = json.loads(action.payload) if action and action.action_type == "income_registration" else {}
    if action and action.action_type != "income_registration":
        raise ToolError("Conclua ou cancele a alteracao pendente antes de registrar uma receita")
    for field in ("amount", "description", "transaction_date", "area", "account", "category", "payment_method"):
        if args.get(field) not in (None, ""):
            payload[field] = args[field]
    amount = _decimal(payload.get("amount"), "valor")
    description = " ".join(str(payload.get("description") or "").split())
    person = _resolve_person(db, context, payload.get("area"))
    missing = []
    if amount is None or amount <= 0:
        missing.append("valor")
    if not description:
        missing.append("origem ou descricao")
    if not person:
        missing.append("area financeira")
    account = None
    account_options = []
    category = None
    category_options = []
    if person:
        if person.workspace_id not in context.writable_workspace_ids:
            raise ToolError("Usuario possui somente leitura nesta area financeira")
        account, account_options = _resolve_income_account(db, context, person, payload.get("account"))
        if not account:
            missing.append("conta de destino")
        category = _resolve_income_category(
            db, person.workspace_id, payload.get("category"), payload.get("payment_method")
        )
        if not category:
            category_options = list_income_categories(db, context, {"area": person.name})["categories"]
            missing.append("categoria")
    if account:
        payload["account_id"] = account.id
        payload["account"] = account.name
    if category:
        payload["category_id"] = category.id
        payload["category"] = (
            f"{category.parent_name} > {category.name}" if category.parent_name else category.name
        )
    if person:
        payload["person_id"] = person.id
        payload["area"] = person.name
        payload["workspace_id"] = person.workspace_id
    if amount is not None:
        payload["amount"] = _money(amount)
    if description:
        payload["description"] = description[:180]
    payload["transaction_date"] = _date(payload.get("transaction_date")).isoformat()
    payload["payment_method"] = str(payload.get("payment_method") or "PIX").upper()
    if not action:
        action = TelegramPendingAction(
            user_id=context.user_id, action_type="income_registration", payload="{}",
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db.add(action)
    action.payload = json.dumps(payload, ensure_ascii=False)
    action.status = "collecting" if missing else "awaiting_confirmation"
    db.commit()
    return {
        "action": "income_registration", "status": action.status,
        "missing_fields": missing, "account_options": account_options,
        "category_options": category_options,
        "preview": {
            key: payload.get(key) for key in (
                "description", "amount", "transaction_date", "area", "account",
                "category", "payment_method",
            )
        },
        "instruction": (
            "Pergunte somente os dados ausentes" if missing
            else "Apresente o resumo e solicite confirmacao clara antes de registrar"
        ),
    }


def prepare_transaction_update(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para corrigir lancamentos")
    active = _active_action(db, context.user_id)
    if active:
        if active.action_type == "transaction_update":
            active.status = "cancelled"
            active.resolved_at = datetime.utcnow()
            db.commit()
        else:
            raise ToolError("Conclua ou cancele a alteracao pendente antes de corrigir um lancamento")
    description = " ".join(str(args.get("target_description") or "").split())
    amount = _decimal(args.get("target_amount"), "valor original")
    if not description and amount is None:
        return {
            "status": "missing_information",
            "missing_fields": ["descricao ou valor do lancamento que deve ser corrigido"],
        }
    query = select(Transaction).where(
        *_base_transaction_filters(context),
        Transaction.transaction_type.in_([TransactionType.income, TransactionType.expense]),
    )
    if description:
        query = query.where(func.lower(Transaction.description).contains(description.casefold()))
    if amount is not None:
        query = query.where(Transaction.amount == amount)
    if args.get("target_transaction_date"):
        query = query.where(Transaction.transaction_date == _date(args["target_transaction_date"]))
    if args.get("area"):
        person = _resolve_person(db, context, args["area"])
        query = query.where(Transaction.person_id == person.id)
    matches = db.scalars(query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).limit(10)).all()
    if not matches:
        return {
            "status": "not_found",
            "message": "Nenhum lancamento registrado corresponde aos dados informados",
        }
    if len(matches) > 1:
        return {
            "status": "selection_required",
            "message": "Encontrei mais de um lancamento; pergunte qual deve ser corrigido",
            "candidates": [{
                "description": item.description, "amount": _money(item.amount),
                "transaction_date": item.transaction_date.isoformat(),
                "area": item.person.name if item.person else None,
            } for item in matches],
        }
    item = matches[0]
    if item.workspace_id not in context.writable_workspace_ids:
        raise ToolError("Usuario possui somente leitura neste lancamento")
    new_description = " ".join(str(args.get("new_description") or item.description).split())[:180]
    new_amount = _decimal(args.get("new_amount"), "novo valor") or Decimal(item.amount)
    new_date = _date(args.get("new_transaction_date"), item.transaction_date)
    requested_invoice = _invoice_month(args.get("new_invoice_month"))
    is_credit_card = bool(item.card_id and (item.payment_method or "").casefold() in ("credito", "crédito", "credit"))
    if requested_invoice and not is_credit_card:
        raise ToolError("A fatura só pode ser alterada em gastos no cartão de crédito")
    if requested_invoice:
        updated_year, updated_month = requested_invoice
    elif is_credit_card and new_date != item.transaction_date:
        updated_year, updated_month = card_purchase_competence(db, item.card, new_date)
    else:
        updated_year = item.competence_year or item.transaction_date.year
        updated_month = item.competence_month or item.transaction_date.month
    category = item.category
    if args.get("new_category"):
        categories = db.scalars(select(Category).where(
            Category.workspace_id == item.workspace_id,
            Category.kind == item.transaction_type,
        )).all()
        normalized = str(args["new_category"]).casefold().strip()
        matches_category = [
            candidate for candidate in categories
            if candidate.name.casefold() == normalized
            or f"{candidate.parent_name or ''} > {candidate.name}".casefold().strip(" >") == normalized
        ]
        if len(matches_category) != 1:
            raise ToolError("Categoria nova nao encontrada ou ambigua")
        category = matches_category[0]
    payload = {
        "transaction_id": item.id,
        "workspace_id": item.workspace_id,
        "original": {
            "description": item.description, "amount": _money(item.amount),
            "transaction_date": item.transaction_date.isoformat(),
            "invoice_month": f"{item.competence_year:04d}-{item.competence_month:02d}",
            "competence_year": item.competence_year, "competence_month": item.competence_month,
            "category_id": item.category_id,
            "category": (
                f"{item.category.parent_name} > {item.category.name}"
                if item.category and item.category.parent_name else item.category.name if item.category else None
            ),
        },
        "updated": {
            "description": new_description, "amount": _money(new_amount),
            "transaction_date": new_date.isoformat(),
            "invoice_month": f"{updated_year:04d}-{updated_month:02d}",
            "competence_year": updated_year, "competence_month": updated_month,
            "category_id": category.id if category else None,
            "category": (
                f"{category.parent_name} > {category.name}"
                if category and category.parent_name else category.name if category else None
            ),
        },
    }
    if payload["original"] == payload["updated"]:
        return {"status": "unchanged", "message": "Nenhuma alteracao foi informada"}
    action = TelegramPendingAction(
        user_id=context.user_id, action_type="transaction_update",
        payload=json.dumps(payload, ensure_ascii=False), status="awaiting_confirmation",
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )
    db.add(action)
    db.commit()
    return {
        "action": "transaction_update", "status": "awaiting_confirmation",
        "before": payload["original"], "after": payload["updated"],
        "instruction": "Mostre antes e depois e solicite confirmacao clara antes de alterar",
    }


def _resolve_financing(db: Session, context: UserAccessContext, name: str | None) -> Financing:
    query = select(Financing).where(
        Financing.workspace_id.in_(context.workspace_ids), Financing.person_id.in_(context.allowed_person_ids),
        Financing.status == "active",
    )
    items = db.scalars(query).all()
    if name:
        normalized = name.casefold()
        matches = [item for item in items if normalized in item.description.casefold() or
                   (item.institution and normalized in item.institution.casefold())]
    else:
        matches = items
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError("Financiamento nao encontrado ou sem permissao de acesso")
    raise ToolError("Informe qual financiamento: " + ", ".join(item.description for item in matches))


def prepare_financing_amortization(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para alterar financiamentos")
    action = _active_action(db, context.user_id)
    payload = json.loads(action.payload) if action and action.action_type == "financing_amortization" else {}
    if not payload:
        financing = _resolve_financing(db, context, args.get("financing"))
        if financing.workspace_id not in context.writable_workspace_ids:
            raise ToolError("Usuario possui somente leitura neste financiamento")
        payload = {"financing_id": financing.id, "description": financing.description,
                   "previous_balance": _money(financing.outstanding_balance) if financing.outstanding_balance is not None else None,
                   "previous_total_installments": financing.total_installments,
                   "paid_installments": financing.paid_installments,
                   "previous_installment_amount": _money(financing.installment_amount)}
        action = TelegramPendingAction(user_id=context.user_id, action_type="financing_amortization",
            payload="{}", expires_at=datetime.utcnow() + timedelta(minutes=30))
        db.add(action)
    mappings = {
        "new_outstanding_balance": "new_outstanding_balance", "remaining_installments": "remaining_installments",
        "new_installment_amount": "new_installment_amount", "amortized_amount": "amortized_amount",
        "strategy": "strategy", "amortization_date": "amortization_date", "notes": "notes",
    }
    for source, target in mappings.items():
        if args.get(source) not in (None, ""):
            payload[target] = args[source]
    if not payload.get("strategy"):
        has_term, has_amount = payload.get("remaining_installments") is not None, payload.get("new_installment_amount") is not None
        if has_term and has_amount: payload["strategy"] = "reduce_both"
        elif has_term: payload["strategy"] = "reduce_term"
        elif has_amount: payload["strategy"] = "reduce_installment"
    missing = []
    if payload.get("new_outstanding_balance") in (None, ""): missing.append("novo saldo devedor oficial")
    if not payload.get("strategy"): missing.append("se a amortizacao reduziu prazo, parcela ou ambos")
    if payload.get("strategy") in ("reduce_term", "reduce_both") and payload.get("remaining_installments") in (None, ""):
        missing.append("quantidade de parcelas restantes")
    if payload.get("strategy") in ("reduce_installment", "reduce_both") and payload.get("new_installment_amount") in (None, ""):
        missing.append("novo valor da parcela")
    action.payload = json.dumps(payload, ensure_ascii=False)
    action.status = "collecting" if missing else "awaiting_confirmation"
    db.commit()
    return {"action": "financing_amortization", "status": action.status, "missing_fields": missing,
            "preview": payload, "instruction": "Pergunte somente os dados ausentes" if missing else "Solicite confirmacao clara antes de alterar"}


def confirm_pending_action(db: Session, context: UserAccessContext, args: dict) -> dict:
    action = _active_action(db, context.user_id)
    if not action or action.status != "awaiting_confirmation":
        raise ToolError("Nao ha alteracao completa aguardando confirmacao")
    if action.action_type == "fuel_fillup":
        payload = json.loads(action.payload)
        transaction = db.scalar(select(Transaction).where(
            Transaction.id == payload.get("transaction_id"),
            Transaction.workspace_id.in_(context.writable_workspace_ids),
            Transaction.person_id.in_(context.allowed_person_ids),
        ))
        vehicle = db.scalar(select(Vehicle).where(
            Vehicle.id == payload.get("vehicle_id"), Vehicle.is_active.is_(True),
            Vehicle.workspace_id.in_(context.writable_workspace_ids),
        ))
        if not transaction or not vehicle:
            action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
            raise ToolError("A despesa ou o veiculo nao esta mais disponivel")
        if db.scalar(select(FuelFillup.id).where(FuelFillup.transaction_id == transaction.id)):
            action.status = "confirmed"; action.resolved_at = datetime.utcnow(); db.commit()
            return {"registered": False, "already_registered": True, "fuel_fillup": True}
        odometer, liters = Decimal(payload["odometer_km"]), Decimal(payload["liters"])
        price, total = Decimal(payload["price_per_liter"]), Decimal(transaction.amount)
        previous = db.scalar(select(FuelFillup.odometer_km).where(
            FuelFillup.vehicle_id == vehicle.id,
        ).order_by(FuelFillup.odometer_km.desc()))
        minimum = Decimal(previous) if previous is not None else Decimal(vehicle.initial_odometer_km)
        if odometer < minimum or abs((liters * price) - total) > Decimal("0.80"):
            action.status = "collecting"; db.commit()
            raise ToolError("Os dados mudaram ou ficaram inconsistentes; revise o abastecimento")
        db.add(FuelFillup(workspace_id=transaction.workspace_id, vehicle_id=vehicle.id,
                          transaction_id=transaction.id, fillup_date=transaction.transaction_date,
                          odometer_km=odometer, liters=liters, price_per_liter=price,
                          is_full_tank=bool(payload["full_tank"])))
        action.status = "confirmed"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"registered": True, "fuel_fillup": True, "vehicle": vehicle.name,
                "odometer_km": str(odometer), "liters": str(liters),
                "price_per_liter": str(price), "full_tank": bool(payload["full_tank"]),
                "total": _money(total)}
    if action.action_type == "fuel_fillup_update":
        payload = json.loads(action.payload)
        fillup = db.scalar(select(FuelFillup).where(
            FuelFillup.id == payload["fillup_id"],
            FuelFillup.workspace_id.in_(context.writable_workspace_ids),
        ))
        original = payload["original"]
        if not fillup or any((str(fillup.odometer_km) != original["odometer_km"],
                              str(fillup.liters) != original["liters"],
                              str(fillup.price_per_liter) != original["price_per_liter"],
                              fillup.is_full_tank != original["full_tank"])):
            action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
            raise ToolError("O abastecimento mudou desde o resumo; inicie a correcao novamente")
        fillup.odometer_km = Decimal(payload["odometer_km"])
        fillup.liters = Decimal(payload["liters"])
        fillup.price_per_liter = Decimal(payload["price_per_liter"])
        fillup.is_full_tank = bool(payload["full_tank"])
        action.status = "confirmed"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"updated": True, "fuel_fillup_update": True, "vehicle": fillup.vehicle.name,
                "odometer_km": payload["odometer_km"], "liters": payload["liters"],
                "price_per_liter": payload["price_per_liter"], "full_tank": bool(payload["full_tank"])}
    if action.action_type == "transaction_update":
        payload = json.loads(action.payload)
        item = db.scalar(select(Transaction).where(
            Transaction.id == payload["transaction_id"],
            Transaction.workspace_id.in_(context.writable_workspace_ids),
            Transaction.person_id.in_(context.allowed_person_ids),
            Transaction.status != TransactionStatus.cancelled,
        ))
        original = payload["original"]
        if not item or any((
            item.description != original["description"],
            _money(item.amount) != original["amount"],
            item.transaction_date.isoformat() != original["transaction_date"],
            item.competence_year != original["competence_year"],
            item.competence_month != original["competence_month"],
            item.category_id != original["category_id"],
        )):
            action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
            raise ToolError("O lancamento mudou ou foi excluido desde o resumo; inicie a correcao novamente")
        updated = payload["updated"]
        item.description = updated["description"]
        item.amount = _decimal(updated["amount"], "valor")
        item.transaction_date = _date(updated["transaction_date"])
        item.category_id = updated["category_id"]
        item.competence_year = updated["competence_year"]
        item.competence_month = updated["competence_month"]
        if item.card and (item.payment_method or "").casefold() in ("credito", "crédito", "credit"):
            item.status = (
                TransactionStatus.paid
                if card_invoice_is_closed(db, item.card_id, item.competence_year, item.competence_month)
                else TransactionStatus.pending
            )
        action.status = "confirmed"; action.resolved_at = datetime.utcnow()
        db.commit()
        return {
            "updated": True, "transaction_updated": True,
            "description": item.description, "amount": _money(item.amount),
            "transaction_date": item.transaction_date.isoformat(),
            "invoice_month": updated["invoice_month"],
            "category": updated["category"],
        }
    if action.action_type == "income_registration":
        payload = json.loads(action.payload)
        person = db.scalar(select(Person).where(
            Person.id == payload["person_id"],
            Person.id.in_(context.allowed_person_ids),
            Person.workspace_id.in_(context.writable_workspace_ids),
        ))
        account = db.scalar(select(Account).where(
            Account.id == payload["account_id"],
            Account.workspace_id.in_(context.writable_workspace_ids),
            Account.person_id == payload["person_id"],
            Account.is_active.is_(True),
        ))
        category = db.scalar(select(Category).where(
            Category.id == payload["category_id"],
            Category.workspace_id == payload["workspace_id"],
            Category.kind == TransactionType.income,
        )) if payload.get("category_id") else None
        if not person or not account:
            action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
            raise ToolError("A area ou conta mudou desde o resumo; inicie o recebimento novamente")
        transaction_date = _date(payload.get("transaction_date"))
        transaction = Transaction(
            workspace_id=payload["workspace_id"], transaction_type=TransactionType.income,
            description=payload["description"], amount=_decimal(payload["amount"], "valor"),
            transaction_date=transaction_date, competence_year=transaction_date.year,
            competence_month=transaction_date.month, status=TransactionStatus.paid,
            account_id=account.id, person_id=person.id,
            category_id=category.id if category else None,
            payment_method=payload.get("payment_method"), created_by_id=context.user_id,
            source="telegram",
        )
        db.add(transaction)
        action.status = "confirmed"; action.resolved_at = datetime.utcnow()
        db.commit()
        return {
            "registered": True, "transaction_type": "income",
            "description": transaction.description, "amount": _money(transaction.amount),
            "transaction_date": transaction.transaction_date.isoformat(),
            "area": person.name, "account": account.name,
            "category": payload.get("category"), "payment_method": transaction.payment_method,
            "transaction_id": transaction.id,
        }
    if action.action_type != "financing_amortization":
        raise ToolError("Tipo de acao pendente nao suportado")
    payload = json.loads(action.payload)
    financing = db.scalar(select(Financing).where(
        Financing.id == payload["financing_id"], Financing.workspace_id.in_(context.workspace_ids),
        Financing.person_id.in_(context.allowed_person_ids), Financing.status == "active",
    ))
    if not financing:
        raise ToolError("Financiamento nao encontrado ou sem permissao")
    if financing.workspace_id not in context.writable_workspace_ids:
        raise ToolError("Usuario possui somente leitura neste financiamento")
    if financing.total_installments != payload["previous_total_installments"] or _money(financing.installment_amount) != payload["previous_installment_amount"]:
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        raise ToolError("O financiamento mudou desde o resumo; inicie a atualizacao novamente")
    new_balance = _decimal(payload["new_outstanding_balance"], "saldo devedor")
    strategy = payload["strategy"]
    remaining = int(payload.get("remaining_installments", financing.total_installments - financing.paid_installments))
    new_amount = _decimal(payload.get("new_installment_amount"), "valor da parcela") or Decimal(financing.installment_amount)
    if remaining < 0 or new_amount <= 0:
        raise ToolError("Prazo ou parcela invalido")
    new_total = financing.paid_installments + remaining
    history = FinancingAmortization(
        financing_id=financing.id, user_id=context.user_id,
        amortization_date=_date(payload.get("amortization_date")),
        amortized_amount=_decimal(payload.get("amortized_amount"), "valor amortizado"),
        previous_outstanding_balance=financing.outstanding_balance, new_outstanding_balance=new_balance,
        previous_total_installments=financing.total_installments, new_total_installments=new_total,
        previous_installment_amount=financing.installment_amount, new_installment_amount=new_amount,
        strategy=strategy, source="telegram", notes=payload.get("notes"),
    )
    db.add(history)
    financing.outstanding_balance = new_balance
    financing.total_installments = new_total
    financing.installment_amount = new_amount
    rule = db.get(RecurrenceRule, financing.recurrence_rule_id) if financing.recurrence_rule_id else None
    if remaining == 0 or new_balance == 0:
        financing.status = "paid"
        if rule: rule.is_active = False
    elif rule:
        rule.amount = new_amount
        rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{new_total}"
    action.status = "confirmed"; action.resolved_at = datetime.utcnow()
    db.commit()
    return {"updated": True, "financing": _financing_data(financing), "amortization_id": history.id}


def cancel_pending_action(db: Session, context: UserAccessContext, args: dict) -> dict:
    action = _active_action(db, context.user_id)
    if not action:
        return {"cancelled": False, "message": "Nenhuma alteracao pendente"}
    action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
    return {"cancelled": True}


def _draft_budget_impact(db: Session, context: UserAccessContext, draft) -> dict | None:
    if not draft or not draft.category_id:
        return None
    year, month = draft.transaction_date.year, draft.transaction_date.month
    if draft.payment_method == "Crédito" and draft.card_id:
        year, month = card_purchase_competence(db, draft.card, draft.transaction_date)
    positions = monthly_budget_positions(
        db, workspace_ids=context.workspace_ids, person_ids=(draft.person_id,),
        year=year, month=month, category_id=draft.category_id,
    )
    if not positions:
        return None
    position = positions[0]
    return {
        "area": draft.person.name,
        "category": (f"{draft.category.parent_name} > {draft.category.name}"
                     if draft.category.parent_name else draft.category.name),
        "competence": f"{year:04d}-{month:02d}",
        **position.impact(Decimal(draft.amount)),
    }


def prepare_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para registrar despesas")
    amount = _decimal(args.get("amount"), "valor")
    description = " ".join(str(args.get("description") or "").split())
    if amount is None or amount <= 0 or not description:
        return {"status": "missing_information", "missing_fields": [
            name for name, missing in (("valor", amount is None), ("descricao", not description)) if missing
        ]}
    user = db.get(User, context.user_id)
    # Começar uma nova despesa encerra apenas o complemento opcional de combustível anterior.
    pending_action = _active_action(db, context.user_id)
    if pending_action and pending_action.action_type == "fuel_fillup":
        pending_action.status = "cancelled"
        pending_action.resolved_at = datetime.utcnow()
        db.commit()
    if user.default_person_id:
        person = db.get(Person, user.default_person_id)
        if person and person.workspace_id not in context.writable_workspace_ids:
            raise ToolError("Usuario possui somente leitura na area financeira padrao")
    draft = create_expense_draft(db, user, ExpenseInput(amount, description[:180]))
    if not draft:
        raise ToolError("Defina uma area financeira padrao antes de registrar pelo Telegram")
    missing = []
    category_options = []
    transaction_date = _date(args.get("transaction_date"), date.today()) if args.get("transaction_date") else None
    if any(args.get(field) for field in ("category", "payment_method", "card", "transaction_date")):
        draft, missing, category_options = update_expense_draft(
            db, user, category=args.get("category"), payment_method=args.get("payment_method"),
            card=args.get("card"), transaction_date=transaction_date,
        )
    return {"status": "missing_information" if missing else "awaiting_confirmation",
            "missing_fields": missing, "category_options": category_options,
            "summary": format_draft(draft), "budget_impact": _draft_budget_impact(db, context, draft)}


def prepare_expenses(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para registrar despesas")
    expenses = args.get("expenses")
    if not isinstance(expenses, list) or len(expenses) < 2:
        return {"status": "missing_information", "missing_fields": ["ao menos duas despesas"]}
    normalized = [item for item in expenses[:20] if isinstance(item, dict)]
    if len(normalized) < 2:
        return {"status": "missing_information", "missing_fields": ["despesas validas"]}
    invalid_positions = [
        index for index, item in enumerate(normalized, start=1)
        if not str(item.get("description") or "").strip()
        or (_decimal(item.get("amount"), f"valor da despesa {index}") or Decimal(0)) <= 0
    ]
    if invalid_positions:
        return {"status": "missing_information",
                "missing_fields": [f"valor e descricao da despesa {index}" for index in invalid_positions]}
    user = db.get(User, context.user_id)
    replace_expense_queue(db, user.id, normalized[1:])
    first = prepare_expense(db, context, normalized[0])
    first["batch"] = {"total": len(normalized), "current": 1, "queued": len(normalized) - 1}
    return first


def _advance_expense_queue(db: Session, context: UserAccessContext) -> dict | None:
    payload = pop_next_queued_expense(db, context.user_id)
    if not payload:
        return None
    result = prepare_expense(db, context, payload)
    result["queue"] = {"remaining_after_current": queued_expense_count(db, context.user_id)}
    return result


def confirm_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    user = db.get(User, context.user_id)
    draft = get_active_draft(db, user.id)
    if draft and draft.payment_method == "Crédito" and not draft.card_id:
        return {"status": "missing_information", "missing_fields": ["cartao"],
                "summary": format_draft(draft)}
    transaction = confirm_expense_draft(db, user)
    if not transaction:
        raise ToolError("Nao ha despesa aguardando confirmacao")
    finish_processing_queue_item(db, context.user_id, "confirmed")
    next_expense = _advance_expense_queue(db, context)
    fuel_followup = False
    category = db.get(Category, transaction.category_id) if transaction.category_id else None
    category_label = f"{category.parent_name or ''} {category.name or ''}".casefold() if category else ""
    vehicles = db.scalars(select(Vehicle).where(
        Vehicle.workspace_id == transaction.workspace_id, Vehicle.is_active.is_(True),
    ).order_by(Vehicle.name)).all()
    if not next_expense and vehicles and any(term in category_label for term in ("combust", "gasolina", "etanol", "diesel")):
        previous = _active_action(db, context.user_id)
        if previous:
            previous.status = "cancelled"; previous.resolved_at = datetime.utcnow()
        db.add(TelegramPendingAction(
            user_id=context.user_id, action_type="fuel_fillup", status="collecting",
            payload=json.dumps({"phase": "offer", "transaction_id": transaction.id}, ensure_ascii=False),
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        ))
        db.commit()
        fuel_followup = True
    return {"registered": True, "description": transaction.description, "amount": _money(transaction.amount),
            "next_expense": next_expense, "fuel_followup": fuel_followup,
            "vehicles": [item.name for item in vehicles] if fuel_followup else []}


def update_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    if not context.can_write:
        raise ToolError("Usuario sem permissao para alterar despesas")
    user = db.get(User, context.user_id)
    transaction_date = _date(args.get("transaction_date"), date.today()) if args.get("transaction_date") else None
    person = _resolve_person(db, context, args.get("area")) if args.get("area") else None
    draft, missing, category_options = update_expense_draft(
        db, user, description=args.get("description"), category=args.get("category"),
        payment_method=args.get("payment_method"),
        card=args.get("card"), transaction_date=transaction_date, person=person,
    )
    if not draft:
        raise ToolError("Nao ha despesa aguardando confirmacao")
    return {"status": "missing_information" if missing else "awaiting_confirmation",
            "missing_fields": missing, "category_options": category_options,
            "summary": format_draft(draft), "budget_impact": _draft_budget_impact(db, context, draft)}


def recommend_expense_category(db: Session, context: UserAccessContext, args: dict) -> dict:
    user = db.get(User, context.user_id)
    draft, category, source = suggest_expense_category(db, user)
    if not draft:
        raise ToolError("Nao ha despesa pendente para categorizar")
    if not category:
        categories = db.scalars(select(Category).where(
            Category.workspace_id == draft.workspace_id,
            Category.kind == TransactionType.expense,
        ).order_by(Category.parent_name, Category.name)).all()
        return {
            "status": "recommendation_unavailable",
            "message": "Nao foi possivel escolher uma categoria com seguranca",
            "description": draft.description,
            "category_options": [
                f"{item.parent_name} > {item.name}" if item.parent_name else item.name
                for item in categories
            ],
            "summary": format_draft(draft),
        }
    return {
        "status": "awaiting_confirmation", "recommended": True,
        "description": draft.description,
        "category": f"{category.parent_name} > {category.name}" if category.parent_name else category.name,
        "recommendation_source": source, "summary": format_draft(draft),
    }


def cancel_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    user = db.get(User, context.user_id)
    draft = get_active_draft(db, user.id)
    description = draft.description if draft else None
    amount = _money(draft.amount) if draft else None
    cancelled = cancel_expense_draft(db, user)
    if cancelled:
        finish_processing_queue_item(db, context.user_id, "cancelled")
    return {"cancelled": cancelled, "description": description, "amount": amount,
            "next_expense": _advance_expense_queue(db, context) if cancelled else None}


def prepare_fuel_fillup(db: Session, context: UserAccessContext, args: dict) -> dict:
    """Completa um abastecimento pendente; a IA decide quando e com quais dados chamar."""
    action = _active_action(db, context.user_id)
    if not action or action.action_type != "fuel_fillup":
        raise ToolError("Nao ha abastecimento pendente. Primeiro registre e confirme a despesa de combustivel")
    payload = json.loads(action.payload)
    transaction = db.scalar(select(Transaction).where(
        Transaction.id == payload.get("transaction_id"),
        Transaction.workspace_id.in_(context.writable_workspace_ids),
        Transaction.person_id.in_(context.allowed_person_ids),
    ))
    if not transaction:
        action.status = "cancelled"; action.resolved_at = datetime.utcnow(); db.commit()
        raise ToolError("A despesa vinculada nao esta mais disponivel")
    vehicles = db.scalars(select(Vehicle).where(
        Vehicle.workspace_id == transaction.workspace_id, Vehicle.is_active.is_(True),
    ).order_by(Vehicle.name)).all()
    requested_vehicle = " ".join(str(args.get("vehicle") or "").casefold().split())
    vehicle = db.get(Vehicle, payload.get("vehicle_id")) if payload.get("vehicle_id") else None
    if requested_vehicle:
        matches = [item for item in vehicles if requested_vehicle == item.name.casefold() or requested_vehicle in item.name.casefold()]
        if len(matches) != 1:
            return {"status": "missing_information", "missing_fields": ["veiculo valido"],
                    "vehicle_options": [item.name for item in vehicles]}
        vehicle = matches[0]; payload["vehicle_id"] = vehicle.id
    elif not vehicle and len(vehicles) == 1:
        vehicle = vehicles[0]; payload["vehicle_id"] = vehicle.id
    for field in ("odometer_km", "liters", "price_per_liter"):
        if args.get(field) not in (None, ""):
            try:
                value = Decimal(str(args.get(field)))
            except InvalidOperation as exc:
                raise ToolError(f"{field} invalido") from exc
            if value is None or value <= 0:
                raise ToolError(f"{field} deve ser maior que zero")
            payload[field] = str(value)
    if args.get("full_tank") is not None:
        payload["full_tank"] = bool(args.get("full_tank"))
    total = Decimal(transaction.amount)
    odometer = Decimal(payload["odometer_km"]) if payload.get("odometer_km") else None
    liters = Decimal(payload["liters"]) if payload.get("liters") else None
    price = Decimal(payload["price_per_liter"]) if payload.get("price_per_liter") else None
    calculated = []
    if liters is None and price:
        liters = (total / price).quantize(Decimal("0.001")); payload["liters"] = str(liters); calculated.append("litros")
    if price is None and liters:
        price = (total / liters).quantize(Decimal("0.001")); payload["price_per_liter"] = str(price); calculated.append("preco por litro")
    action.payload = json.dumps(payload, ensure_ascii=False)
    missing = []
    if not vehicle: missing.append("veiculo")
    if odometer is None: missing.append("quilometragem")
    if liters is None and price is None: missing.append("litros ou preco por litro")
    if "full_tank" not in payload: missing.append("se o tanque foi completado")
    if missing:
        db.commit()
        return {"status": "missing_information", "missing_fields": missing,
                "vehicle_options": [item.name for item in vehicles], "known_total": _money(total)}
    previous = db.scalar(select(FuelFillup.odometer_km).where(
        FuelFillup.vehicle_id == vehicle.id,
    ).order_by(FuelFillup.odometer_km.desc()))
    minimum = Decimal(previous) if previous is not None else Decimal(vehicle.initial_odometer_km)
    if odometer < minimum:
        db.commit()
        return {"status": "inconsistent", "field": "odometer_km", "informed": str(odometer),
                "minimum": str(minimum), "message": "A quilometragem e menor que a ultima registrada"}
    computed_total = liters * price
    difference = abs(computed_total - total)
    if difference > Decimal("0.80"):
        db.commit()
        return {"status": "inconsistent", "field": "fuel_values", "known_total": _money(total),
                "computed_total": _money(computed_total), "difference": _money(difference),
                "expected_liters": str((total / price).quantize(Decimal("0.001"))),
                "message": "Litros e preco por litro nao correspondem ao valor da despesa"}
    existing = db.scalar(select(FuelFillup).where(FuelFillup.transaction_id == transaction.id))
    if existing:
        action.status = "confirmed"; action.resolved_at = datetime.utcnow(); db.commit()
        return {"registered": False, "already_registered": True, "fuel_fillup": True}
    action.status = "awaiting_confirmation"
    action.payload = json.dumps(payload, ensure_ascii=False)
    db.commit()
    return {"status": "awaiting_confirmation", "fuel_fillup": True, "vehicle": vehicle.name,
            "odometer_km": str(odometer), "liters": str(liters), "price_per_liter": str(price),
            "total": _money(total), "full_tank": payload["full_tank"], "calculated_fields": calculated}


def prepare_fuel_fillup_update(db: Session, context: UserAccessContext, args: dict) -> dict:
    query = select(FuelFillup).where(FuelFillup.workspace_id.in_(context.writable_workspace_ids)).join(Vehicle)
    vehicle_name = " ".join(str(args.get("vehicle") or "").casefold().split())
    if vehicle_name:
        query = query.where(func.lower(Vehicle.name).contains(vehicle_name))
    fillup = db.scalar(query.order_by(FuelFillup.fillup_date.desc(), FuelFillup.id.desc()))
    if not fillup:
        raise ToolError("Nenhum abastecimento foi encontrado para corrigir")
    total = Decimal(fillup.transaction.amount) if fillup.transaction else Decimal(fillup.liters) * Decimal(fillup.price_per_liter)
    odometer = Decimal(str(args.get("new_odometer_km", fillup.odometer_km)))
    liters = Decimal(str(args.get("new_liters", fillup.liters)))
    price = Decimal(str(args.get("new_price_per_liter", fillup.price_per_liter)))
    full_tank = bool(args.get("new_full_tank")) if args.get("new_full_tank") is not None else fillup.is_full_tank
    if args.get("new_price_per_liter") not in (None, "") and args.get("new_liters") in (None, "") and fillup.transaction:
        liters = (total / price).quantize(Decimal("0.001"))
    elif args.get("new_liters") not in (None, "") and args.get("new_price_per_liter") in (None, "") and fillup.transaction:
        price = (total / liters).quantize(Decimal("0.001"))
    if min(odometer, liters, price) <= 0 or abs((liters * price) - total) > Decimal("0.80"):
        return {"status": "inconsistent", "known_total": _money(total),
                "computed_total": _money(liters * price), "message": "Os dados corrigidos nao fecham com o total"}
    previous = _active_action(db, context.user_id)
    if previous:
        previous.status = "cancelled"; previous.resolved_at = datetime.utcnow()
    payload = {"fillup_id": fillup.id, "vehicle_id": fillup.vehicle_id,
               "odometer_km": str(odometer), "liters": str(liters), "price_per_liter": str(price), "full_tank": full_tank,
               "original": {"odometer_km": str(fillup.odometer_km), "liters": str(fillup.liters),
                            "price_per_liter": str(fillup.price_per_liter), "full_tank": fillup.is_full_tank}}
    db.add(TelegramPendingAction(user_id=context.user_id, action_type="fuel_fillup_update",
        status="awaiting_confirmation", payload=json.dumps(payload, ensure_ascii=False),
        expires_at=datetime.utcnow() + timedelta(minutes=30)))
    db.commit()
    return {"status": "awaiting_confirmation", "fuel_fillup_update": True,
            "vehicle": fillup.vehicle.name, "odometer_km": str(odometer), "liters": str(liters),
            "price_per_liter": str(price), "full_tank": full_tank, "total": _money(total)}


TOOLS = {
    "listar_areas_financeiras": list_financial_areas,
    "consultar_contas": list_accounts,
    "consultar_cartoes": list_cards,
    "consultar_categorias_receita": list_income_categories,
    "consultar_gastos_cartao": query_card_spending,
    "consultar_receitas": lambda db, context, args: query_transactions(db, context, args, TransactionType.income),
    "consultar_despesas": lambda db, context, args: query_transactions(db, context, args, TransactionType.expense),
    "consultar_resumo_mensal": monthly_summary,
    "consultar_orcamentos": query_budgets,
    "consultar_orcamento_categoria": query_category_budget,
    "consultar_saldo_livre": calculate_free_balance,
    "consultar_financiamentos": list_financings,
    "preparar_receita": prepare_income,
    "preparar_correcao_lancamento": prepare_transaction_update,
    "preparar_amortizacao_financiamento": prepare_financing_amortization,
    "confirmar_acao_pendente": confirm_pending_action,
    "cancelar_acao_pendente": cancel_pending_action,
    "preparar_despesa": prepare_expense,
    "preparar_despesas": prepare_expenses,
    "atualizar_despesa": update_expense,
    "sugerir_categoria_despesa": recommend_expense_category,
    "confirmar_despesa": confirm_expense,
    "cancelar_despesa": cancel_expense,
    "preparar_abastecimento": prepare_fuel_fillup,
    "preparar_correcao_abastecimento": prepare_fuel_fillup_update,
}


def execute_tool(db: Session, context: UserAccessContext, name: str, arguments: dict | None) -> dict:
    tool = TOOLS.get(name)
    if not tool:
        raise ToolError(f"Ferramenta nao permitida: {name}")
    return tool(db, context, arguments or {})
