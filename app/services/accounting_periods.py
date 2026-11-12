from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountingPeriod


def current_open_month(db: Session, workspace_id: int, today: date | None = None) -> tuple[int, int]:
    """Usa a competência atual se estiver aberta; se estiver fechada, avança para a próxima aberta."""
    today = today or date.today()
    periods = db.scalars(select(AccountingPeriod).where(
        AccountingPeriod.workspace_id == workspace_id,
        AccountingPeriod.year == today.year,
        AccountingPeriod.month <= today.month,
    )).all()
    closed = {period.month for period in periods if period.is_closed}
    if today.month not in closed:
        return today.year, today.month
    for month in range(today.month + 1, 13):
        if month not in closed:
            return today.year, month
    return today.year, today.month
