from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Card, CardBillingPeriod


def shift_month(year: int, month: int, offset: int = 1) -> tuple[int, int]:
    index = year * 12 + month - 1 + offset
    return index // 12, index % 12 + 1


def card_purchase_competence(db: Session, card: Card, purchase_date: date) -> tuple[int, int]:
    """Retorna a primeira fatura aberta a partir do mês da compra."""
    year, month = purchase_date.year, purchase_date.month
    for _ in range(120):
        closed = db.scalar(select(CardBillingPeriod.id).where(
            CardBillingPeriod.card_id == card.id,
            CardBillingPeriod.year == year,
            CardBillingPeriod.month == month,
            CardBillingPeriod.is_closed.is_(True),
        ))
        if not closed:
            return year, month
        year, month = shift_month(year, month)
    raise ValueError("Não foi possível localizar uma fatura aberta.")
