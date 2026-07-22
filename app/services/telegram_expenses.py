from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account, AccountRole, Card, Category, Person, TelegramExpenseDraft, Transaction, TransactionStatus,
    TransactionType, User, UserPersonAccess, WorkspaceMember,
)
from app.services.card_billing import card_purchase_competence

DRAFT_TTL_MINUTES = 30


@dataclass(frozen=True)
class ExpenseInput:
    amount: Decimal
    description: str


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


def _normalize(value: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    ).replace(">", " ").split())


def _resolve_category(db: Session, workspace_id: int, value: str) -> tuple[Category | None, list[str]]:
    normalized_value = _normalize(value)
    words = normalized_value.split()
    categories = db.scalars(select(Category).where(
        Category.workspace_id == workspace_id, Category.kind == TransactionType.expense,
    )).all()
    exact_names = [category for category in categories if _normalize(category.name) == normalized_value]
    if len(exact_names) == 1:
        return exact_names[0], []
    parent_matches = [category for category in categories if _normalize(category.parent_name or "") == normalized_value]
    if parent_matches:
        return None, [f"{category.parent_name} > {category.name}" for category in parent_matches]
    matches = [category for category in categories if all(
        word in _normalize(f"{category.parent_name or ''} {category.name}") for word in words
    )]
    if len(matches) == 1:
        return matches[0], []
    return None, [f"{category.parent_name} > {category.name}" for category in matches[:12]]


def update_expense_draft(db: Session, user: User, *, category: str | None = None,
                         payment_method: str | None = None, card: str | None = None,
                         transaction_date: date | None = None) -> tuple[TelegramExpenseDraft | None, list[str], list[str]]:
    draft = get_active_draft(db, user.id)
    if not draft:
        return None, [], []
    missing = []
    category_options = []
    if category:
        resolved_category, category_options = _resolve_category(db, draft.workspace_id, category)
        if not resolved_category:
            missing.append("subcategoria" if category_options else "categoria valida")
        else:
            draft.category_id = resolved_category.id
    if transaction_date:
        draft.transaction_date = transaction_date
    if payment_method:
        normalized_method = payment_method.casefold()
        is_credit = normalized_method in ("credit", "credito", "crédito", "cartao", "cartão")
        draft.payment_method = "Crédito" if is_credit else payment_method.strip().title()
        if not is_credit:
            draft.card_id = None
    if draft.payment_method == "Crédito":
        cards = db.scalars(select(Card).join(Account, Account.id == Card.account_id).where(
            Card.workspace_id == draft.workspace_id, Card.is_active.is_(True),
            Account.person_id == draft.person_id,
        )).all()
        if card:
            matches = [item for item in cards if card.casefold() in item.name.casefold()]
            if len(matches) == 1:
                draft.card_id = matches[0].id
                draft.account_id = matches[0].account_id
            else:
                missing.append("cartao valido")
        elif len(cards) == 1:
            draft.card_id = cards[0].id
            draft.account_id = cards[0].account_id
        elif not draft.card_id:
            missing.append("cartao (" + ", ".join(item.name for item in cards) + ")")
    db.commit()
    db.refresh(draft)
    return draft, missing, category_options


def create_expense_draft(db: Session, user: User, expense: ExpenseInput, now: datetime | None = None) -> TelegramExpenseDraft | None:
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
    category = _infer_category(db, workspace_id, expense.description)
    draft = TelegramExpenseDraft(
        user_id=user.id, workspace_id=workspace_id, person_id=person.id,
        description=expense.description, amount=expense.amount, transaction_date=date.today(),
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
    is_credit = draft.payment_method == "Crédito"
    if is_credit and not draft.card_id:
        return None
    competence_year, competence_month = draft.transaction_date.year, draft.transaction_date.month
    if is_credit:
        competence_year, competence_month = card_purchase_competence(db, draft.card, draft.transaction_date)
    transaction = Transaction(
        workspace_id=draft.workspace_id, transaction_type=TransactionType.expense,
        description=draft.description, amount=draft.amount, transaction_date=draft.transaction_date,
        competence_year=competence_year, competence_month=competence_month,
        status=TransactionStatus.pending if is_credit else TransactionStatus.paid,
        person_id=draft.person_id, category_id=draft.category_id, account_id=draft.account_id,
        card_id=draft.card_id, payment_method=draft.payment_method,
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
        f"Categoria: {category}\nForma de pagamento: {draft.payment_method or 'Nao informada'}\n"
        f"Cartao: {draft.card.name if draft.card else 'Nao informado'}\n\nPosso registrar?"
    )
