from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    Account, Card, Category, RecurrenceOccurrence, RecurrenceRule, Transaction,
    TransactionStatus, TransactionType,
)
from app.services.card_billing import card_purchase_competence, shift_month
from app.services.monthly_projection import monthly_budget_positions, projection_budget_remaining


CENT = Decimal("0.01")
MONTH_NAMES = (
    "jan", "fev", "mar", "abr", "mai", "jun",
    "jul", "ago", "set", "out", "nov", "dez",
)


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT):.2f}"


def _credit_payment_filter():
    return func.lower(Transaction.payment_method).in_(("credito", "crédito", "credit"))


def _split_installments(total: Decimal, count: int) -> list[Decimal]:
    total_cents = int((total.quantize(CENT) * 100).to_integral_value())
    base, remainder = divmod(total_cents, count)
    return [Decimal(base + (1 if index < remainder else 0)) / 100 for index in range(count)]


def _current_balance(db: Session, workspace_id: int, person_id: int) -> Decimal:
    initial = Decimal(db.scalar(select(func.coalesce(func.sum(Account.initial_balance), 0)).where(
        Account.workspace_id == workspace_id,
        Account.person_id == person_id,
        Account.is_active.is_(True),
    )))
    paid_flow = Decimal(db.scalar(select(func.coalesce(func.sum(case(
        (Transaction.transaction_type == TransactionType.income, Transaction.amount),
        (Transaction.transaction_type == TransactionType.expense, -Transaction.amount),
        else_=0,
    )), 0)).where(
        Transaction.workspace_id == workspace_id,
        Transaction.person_id == person_id,
        Transaction.status == TransactionStatus.paid,
    )))
    return initial + paid_flow


def _recurring_flow(db: Session, workspace_id: int, person_id: int, year: int, month: int) -> tuple[Decimal, Decimal]:
    rules = db.scalars(select(RecurrenceRule).where(
        RecurrenceRule.workspace_id == workspace_id,
        RecurrenceRule.person_id == person_id,
        RecurrenceRule.is_active.is_(True),
        RecurrenceRule.transaction_type.in_((TransactionType.income, TransactionType.expense)),
    )).all()
    income = Decimal(0)
    expense = Decimal(0)
    month_end = date(year, month, monthrange(year, month)[1])
    for rule in rules:
        if rule.start_date and rule.start_date > month_end:
            continue
        created = rule.created_at.date()
        if (created.year, created.month) > (year, month):
            continue
        occurrence = db.scalar(select(RecurrenceOccurrence).where(
            RecurrenceOccurrence.recurrence_rule_id == rule.id,
            RecurrenceOccurrence.year == year,
            RecurrenceOccurrence.month == month,
        ))
        if occurrence and occurrence.status in ("confirmed", "skipped"):
            continue
        amount = (
            Decimal(occurrence.remaining_amount or 0)
            if occurrence and occurrence.status == "partial"
            else Decimal(rule.amount or 0)
        )
        if rule.transaction_type == TransactionType.income:
            income += amount
        else:
            expense += amount
    return income, expense


def _pending_flow(db: Session, workspace_id: int, person_id: int, year: int, month: int) -> tuple[Decimal, Decimal]:
    rows = db.execute(select(
        Transaction.transaction_type,
        func.coalesce(func.sum(Transaction.amount), 0),
    ).where(
        Transaction.workspace_id == workspace_id,
        Transaction.person_id == person_id,
        Transaction.status == TransactionStatus.pending,
        Transaction.competence_year == year,
        Transaction.competence_month == month,
        Transaction.transaction_type.in_((TransactionType.income, TransactionType.expense)),
    ).group_by(Transaction.transaction_type)).all()
    totals = {kind: Decimal(amount) for kind, amount in rows}
    return totals.get(TransactionType.income, Decimal(0)), totals.get(TransactionType.expense, Decimal(0))


def simulate_credit_purchase(
    db: Session,
    *,
    card: Card,
    category: Category,
    amount: Decimal,
    installments: int,
    purchase_date: date,
    horizon_months: int = 6,
) -> dict:
    """Simula uma compra no cartão sem persistir qualquer lançamento."""
    if not card.account or not card.account.person_id:
        raise ValueError("O cartão precisa estar associado a uma área financeira")
    if amount <= 0:
        raise ValueError("O valor da compra deve ser positivo")
    if installments < 1 or installments > 120:
        raise ValueError("A quantidade de parcelas deve ficar entre 1 e 120")
    if horizon_months not in (3, 6, 12):
        raise ValueError("O horizonte deve ser de 3, 6 ou 12 meses")

    person_id = card.account.person_id
    workspace_id = card.workspace_id
    first_year, first_month = card_purchase_competence(db, card, purchase_date)
    installment_values = _split_installments(amount, installments)
    new_by_month: dict[tuple[int, int], Decimal] = {}
    for offset, value in enumerate(installment_values):
        competence = shift_month(first_year, first_month, offset)
        new_by_month[competence] = new_by_month.get(competence, Decimal(0)) + value

    committed = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.card_id == card.id,
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.pending,
        _credit_payment_filter(),
    )))
    limit_before = Decimal(card.credit_limit or 0) - committed
    limit_after = limit_before - amount
    first_invoice_before = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.card_id == card.id,
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.pending,
        Transaction.competence_year == first_year,
        Transaction.competence_month == first_month,
        _credit_payment_filter(),
    )))

    balance_before = _current_balance(db, workspace_id, person_id)
    cumulative_extra_impact = Decimal(0)
    projections = []
    category_impacts = []
    for offset in range(horizon_months):
        year, month = shift_month(first_year, first_month, offset)
        pending_income, pending_expense = _pending_flow(db, workspace_id, person_id, year, month)
        recurring_income, recurring_expense = _recurring_flow(db, workspace_id, person_id, year, month)
        budget_positions = monthly_budget_positions(
            db,
            workspace_ids=(workspace_id,),
            person_ids=(person_id,),
            year=year,
            month=month,
        )
        budget_reserve = projection_budget_remaining(budget_positions)
        balance_before += pending_income + recurring_income - pending_expense - recurring_expense - budget_reserve

        installment = new_by_month.get((year, month), Decimal(0))
        category_position = next(
            (position for position in budget_positions if position.budget.category_id == category.id),
            None,
        )
        absorbed = Decimal(0)
        category_remaining = None
        if category_position:
            category_remaining = category_position.remaining
            if category_position.budget.include_in_projection and not category_position.is_released:
                absorbed = min(max(category_position.remaining, Decimal(0)), installment)
        extra_impact = installment - absorbed
        cumulative_extra_impact += extra_impact
        balance_after = balance_before - cumulative_extra_impact
        existing_card_installments = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.card_id == card.id,
            Transaction.transaction_type == TransactionType.expense,
            Transaction.status == TransactionStatus.pending,
            Transaction.competence_year == year,
            Transaction.competence_month == month,
            _credit_payment_filter(),
        )))
        projections.append({
            "competence": f"{year:04d}-{month:02d}",
            "label": f"{MONTH_NAMES[month - 1]}/{str(year)[2:]}",
            "balance_before": _money(balance_before),
            "balance_after": _money(balance_after),
            "new_installment": _money(installment),
            "existing_card_installments": _money(existing_card_installments),
            "absorbed_by_budget": _money(absorbed),
            "additional_projection_impact": _money(extra_impact),
        })
        if installment:
            category_impacts.append({
                "competence": f"{year:04d}-{month:02d}",
                "budget_found": category_position is not None,
                "remaining_before": _money(category_remaining) if category_remaining is not None else None,
                "remaining_after": _money(category_remaining - installment) if category_remaining is not None else None,
                "exceeds_budget": bool(category_remaining is not None and installment > category_remaining),
                "exceeded_by": _money(max(installment - category_remaining, Decimal(0))) if category_remaining is not None else None,
            })

    worst = min(projections, key=lambda item: Decimal(item["balance_after"]))
    reasons = []
    level = "comfortable"
    if limit_after < 0:
        level = "risky"
        reasons.append("limite do cartão insuficiente para o valor total da compra")
    if Decimal(worst["balance_after"]) < 0:
        level = "risky"
        reasons.append(f"o saldo projetado fica negativo em {worst['label']}")
    budget_exceeded = any(item["exceeds_budget"] for item in category_impacts)
    if budget_exceeded:
        reasons.append("a compra ultrapassa o orçamento da categoria em pelo menos uma competência")
        if level != "risky":
            level = "attention"
    positive_before = [Decimal(item["balance_before"]) for item in projections if Decimal(item["balance_before"]) > 0]
    if level == "comfortable" and positive_before:
        most_reduced = max(
            (Decimal(item["balance_before"]) - Decimal(item["balance_after"])) / Decimal(item["balance_before"])
            for item in projections if Decimal(item["balance_before"]) > 0
        )
        if most_reduced >= Decimal("0.50"):
            level = "attention"
            reasons.append("a compra consome pelo menos metade da margem projetada em uma competência")
    if not reasons:
        reasons.append("o limite, o orçamento e os saldos projetados permanecem preservados")

    category_label = f"{category.parent_name} > {category.name}" if category.parent_name else category.name
    return {
        "status": "simulated",
        "read_only": True,
        "purchase": {
            "amount": _money(amount),
            "card": card.name,
            "area": card.account.person.name if card.account.person else None,
            "category": category_label,
            "purchase_date": purchase_date.isoformat(),
            "installments": installments,
            "first_installment": _money(installment_values[0]),
            "first_invoice": f"{first_year:04d}-{first_month:02d}",
        },
        "card_limit": {
            "credit_limit": _money(Decimal(card.credit_limit or 0)),
            "committed_before": _money(committed),
            "available_before": _money(limit_before),
            "available_after": _money(limit_after),
            "sufficient": limit_after >= 0,
        },
        "first_invoice": {
            "before": _money(first_invoice_before),
            "after": _money(first_invoice_before + installment_values[0]),
        },
        "category_budget": category_impacts,
        "projections": projections,
        "worst_projected_balance": {
            "competence": worst["competence"],
            "label": worst["label"],
            "amount": worst["balance_after"],
        },
        "risk": {"level": level, "reasons": reasons},
        "assumptions": [
            "parcelamento sem juros",
            "receitas e despesas recorrentes ativas permanecem nos valores cadastrados",
            "o saldo projetado considera a reserva restante dos orçamentos ativos",
            "compras que cabem na reserva da categoria não reduzem a projeção duas vezes",
        ],
    }
