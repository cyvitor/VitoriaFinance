from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Card, CardBillingPeriod, Transaction, TransactionStatus


def shift_month(year: int, month: int, offset: int = 1) -> tuple[int, int]:
    index = year * 12 + month - 1 + offset
    return index // 12, index % 12 + 1


def card_invoice_is_closed(db: Session, card_id: int, year: int, month: int) -> bool:
    return bool(db.scalar(select(CardBillingPeriod.id).where(
        CardBillingPeriod.card_id == card_id,
        CardBillingPeriod.year == year,
        CardBillingPeriod.month == month,
        CardBillingPeriod.is_closed.is_(True),
    )))


def card_purchase_competence(db: Session, card: Card, purchase_date: date) -> tuple[int, int]:
    """Retorna a fatura aberta do cartão ou a primeira posterior disponível."""
    open_invoice = db.execute(
        select(Transaction.competence_year, Transaction.competence_month)
        .outerjoin(
            CardBillingPeriod,
            (CardBillingPeriod.card_id == Transaction.card_id)
            & (CardBillingPeriod.year == Transaction.competence_year)
            & (CardBillingPeriod.month == Transaction.competence_month),
        )
        .where(
            Transaction.card_id == card.id,
            Transaction.status == TransactionStatus.pending,
            Transaction.payment_method.in_(("Crédito", "Credito", "crédito", "credito")),
            Transaction.competence_year.is_not(None),
            Transaction.competence_month.is_not(None),
            CardBillingPeriod.id.is_(None) | CardBillingPeriod.is_closed.is_(False),
        )
        .order_by(Transaction.competence_year, Transaction.competence_month)
        .limit(1)
    ).first()
    if open_invoice:
        return open_invoice[0], open_invoice[1]

    year, month = purchase_date.year, purchase_date.month
    for _ in range(120):
        if not card_invoice_is_closed(db, card.id, year, month):
            return year, month
        year, month = shift_month(year, month)
    raise ValueError("Não foi possível localizar uma fatura aberta.")
