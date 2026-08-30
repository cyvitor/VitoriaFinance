from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MonthlyBudget, MonthlyBudgetOccurrence, Transaction, TransactionStatus, TransactionType


@dataclass(frozen=True)
class BudgetPosition:
    budget: MonthlyBudget
    spent: Decimal
    remaining: Decimal
    occurrence: MonthlyBudgetOccurrence | None = None

    @property
    def is_released(self) -> bool:
        return bool(self.occurrence and self.occurrence.status == "released")

    def impact(self, planned_spending: Decimal = Decimal(0)) -> dict:
        after = self.remaining - planned_spending
        return {
            "planned": f"{Decimal(self.budget.amount):.2f}",
            "spent": f"{self.spent:.2f}",
            "remaining": f"{self.remaining:.2f}",
            "planned_spending": f"{planned_spending:.2f}",
            "remaining_after_spending": f"{after:.2f}",
            "exceeds_budget": after < 0,
            "exceeded_by": f"{max(-after, Decimal(0)):.2f}",
            "usage_percent": float((self.spent / Decimal(self.budget.amount) * 100)
                                   if self.budget.amount else 0),
            "include_in_projection": self.budget.include_in_projection,
            "released": self.is_released,
            "released_amount": f"{Decimal(self.occurrence.released_amount or 0):.2f}" if self.occurrence else "0.00",
        }


def monthly_budget_positions(
    db: Session, *, workspace_ids: tuple[int, ...] | list[int], person_ids: tuple[int, ...] | list[int],
    year: int, month: int, category_id: int | None = None,
) -> list[BudgetPosition]:
    query = select(MonthlyBudget).where(
        MonthlyBudget.workspace_id.in_(workspace_ids), MonthlyBudget.person_id.in_(person_ids),
        MonthlyBudget.is_active.is_(True),
        (MonthlyBudget.start_year < year) | (
            (MonthlyBudget.start_year == year) & (MonthlyBudget.start_month <= month)
        ),
    )
    if category_id is not None:
        query = query.where(MonthlyBudget.category_id == category_id)
    budgets = db.scalars(query.order_by(MonthlyBudget.person_id, MonthlyBudget.category_id)).all()
    occurrences = db.scalars(select(MonthlyBudgetOccurrence).where(
        MonthlyBudgetOccurrence.monthly_budget_id.in_([budget.id for budget in budgets]),
        MonthlyBudgetOccurrence.year == year, MonthlyBudgetOccurrence.month == month,
    )).all() if budgets else []
    occurrence_by_budget = {occurrence.monthly_budget_id: occurrence for occurrence in occurrences}
    positions = []
    for budget in budgets:
        spent = sum((Decimal(value) for value in db.scalars(select(Transaction.amount).where(
            Transaction.workspace_id == budget.workspace_id,
            Transaction.person_id == budget.person_id,
            Transaction.category_id == budget.category_id,
            Transaction.transaction_type == TransactionType.expense,
            Transaction.status.in_([TransactionStatus.paid, TransactionStatus.pending]),
            Transaction.competence_year == year, Transaction.competence_month == month,
        )).all()), Decimal(0))
        amount = Decimal(budget.amount)
        positions.append(BudgetPosition(
            budget=budget, spent=spent, remaining=amount - spent,
            occurrence=occurrence_by_budget.get(budget.id),
        ))
    return positions


def projection_budget_remaining(positions: list[BudgetPosition]) -> Decimal:
    return sum((max(position.remaining, Decimal(0)) for position in positions
                if position.budget.include_in_projection and not position.is_released), Decimal(0))
