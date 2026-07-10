from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountRole, Category, Person, TelegramExpenseDraft, Transaction, TransactionStatus,
    TransactionType, User, UserPersonAccess, WorkspaceMember,
)

DRAFT_TTL_MINUTES = 30


@dataclass(frozen=True)
class ExpenseCommand:
    amount: Decimal
    description: str


def parse_expense_command(text: str) -> ExpenseCommand | None:
    text = text.strip()
    match = re.fullmatch(r"/gasto(?:@\w+)?\s+(?:R\$\s*)?([\d.,]+)\s+(.+)", text, re.IGNORECASE)
    if not match:
        return None
    raw_amount = match.group(1)
    if "," in raw_amount:
        raw_amount = raw_amount.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(raw_amount).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    description = " ".join(match.group(2).split()).strip()
    if amount <= 0 or not description or len(description) > 180:
        return None
    return ExpenseCommand(amount, description)


def _telegram_context(db: Session, user: User) -> tuple[int, Person] | None:
    if user.default_person_id:
        person = db.get(Person, user.default_person_id)
        if person and person.is_active and db.scalar(select(WorkspaceMember.id).where(
            WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id == person.workspace_id,
        )):
            has_access = user.is_super_admin or user.account_role == AccountRole.admin or db.scalar(
                select(UserPersonAccess.id).where(
                    UserPersonAccess.user_id == user.id, UserPersonAccess.person_id == person.id,
                )
            )
            if has_access:
                return person.workspace_id, person
    memberships = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    if len(memberships) == 1:
        people = db.scalars(select(Person).where(
            Person.workspace_id == memberships[0].workspace_id, Person.is_active,
        )).all()
        if len(people) == 1:
            return memberships[0].workspace_id, people[0]
    return None


def _infer_category(db: Session, workspace_id: int, description: str) -> Category | None:
    categories = db.scalars(select(Category).where(
        Category.workspace_id == workspace_id, Category.kind == TransactionType.expense,
    )).all()
    normalized = description.casefold()
    exact = [category for category in categories if category.name.casefold() in normalized]
    return max(exact, key=lambda category: len(category.name)) if exact else None


def create_expense_draft(db: Session, user: User, command: ExpenseCommand, now: datetime | None = None) -> TelegramExpenseDraft | None:
    now = now or datetime.utcnow()
    context = _telegram_context(db, user)
    if not context:
        return None
    workspace_id, person = context
    active = db.scalar(select(TelegramExpenseDraft).where(
        TelegramExpenseDraft.user_id == user.id,
        TelegramExpenseDraft.status == "awaiting_confirmation",
    ).order_by(TelegramExpenseDraft.id.desc()))
    if active:
        active.status = "cancelled"
        active.resolved_at = now
    category = _infer_category(db, workspace_id, command.description)
    draft = TelegramExpenseDraft(
        user_id=user.id, workspace_id=workspace_id, person_id=person.id,
        description=command.description, amount=command.amount, transaction_date=date.today(),
        category_id=category.id if category else None,
        expires_at=now + timedelta(minutes=DRAFT_TTL_MINUTES),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def get_active_draft(db: Session, user_id: int, now: datetime | None = None) -> TelegramExpenseDraft | None:
    now = now or datetime.utcnow()
    draft = db.scalar(select(TelegramExpenseDraft).where(
        TelegramExpenseDraft.user_id == user_id,
        TelegramExpenseDraft.status == "awaiting_confirmation",
    ).order_by(TelegramExpenseDraft.id.desc()))
    if draft and draft.expires_at < now:
        draft.status = "expired"
        draft.resolved_at = now
        db.commit()
        return None
    return draft


def confirm_expense_draft(db: Session, user: User, now: datetime | None = None) -> Transaction | None:
    now = now or datetime.utcnow()
    draft = get_active_draft(db, user.id, now)
    if not draft:
        return None
    transaction = Transaction(
        workspace_id=draft.workspace_id, transaction_type=TransactionType.expense,
        description=draft.description, amount=draft.amount, transaction_date=draft.transaction_date,
        competence_year=draft.transaction_date.year, competence_month=draft.transaction_date.month,
        status=TransactionStatus.paid, person_id=draft.person_id, category_id=draft.category_id,
        created_by_id=user.id, source="telegram",
    )
    db.add(transaction)
    db.flush()
    draft.status = "confirmed"
    draft.transaction_id = transaction.id
    draft.resolved_at = now
    db.commit()
    db.refresh(transaction)
    return transaction


def cancel_expense_draft(db: Session, user: User, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    draft = get_active_draft(db, user.id, now)
    if not draft:
        return False
    draft.status = "cancelled"
    draft.resolved_at = now
    db.commit()
    return True


def format_draft(draft: TelegramExpenseDraft) -> str:
    amount = f"{draft.amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    category = f"{draft.category.parent_name} > {draft.category.name}" if draft.category else "Nao identificada"
    return (
        "Confirme este gasto:\n\n"
        f"Descricao: {draft.description}\nValor: R$ {amount}\n"
        f"Data: {draft.transaction_date.strftime('%d/%m/%Y')}\nArea: {draft.person.name}\n"
        f"Categoria: {category}\n\nEnvie /confirmar ou /cancelar."
    )
