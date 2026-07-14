from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Boolean, Date, DateTime, Enum as SAEnum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MemberRole(str, Enum):
    admin = "admin"
    editor = "editor"
    reader = "reader"
    consultant = "consultant"


class AccountRole(str, Enum):
    admin = "admin"
    member = "member"


class PersonType(str, Enum):
    personal = "personal"
    shared = "shared"


class AccountType(str, Enum):
    checking = "checking"
    savings = "savings"
    investment = "investment"
    digital_wallet = "digital_wallet"
    international = "international"
    crypto = "crypto"
    cash = "cash"


class TransactionType(str, Enum):
    income = "income"
    expense = "expense"
    transfer = "transfer"


class TransactionStatus(str, Enum):
    pending = "pending"
    paid = "paid"
    cancelled = "cancelled"


class SystemAccount(Base):
    __tablename__ = "system_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_super_account: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    system_account_id: Mapped[int | None] = mapped_column(ForeignKey("system_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(150))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    account_role: Mapped[AccountRole] = mapped_column(SAEnum(AccountRole), default=AccountRole.member)
    default_person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    memberships: Mapped[list["WorkspaceMember"]] = relationship(back_populates="user")
    default_person: Mapped["Person | None"] = relationship(foreign_keys=[default_person_id])
    system_account: Mapped[SystemAccount | None] = relationship()
    telegram_link: Mapped["TelegramLink | None"] = relationship(back_populates="user", uselist=False)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32))
    telegram_username: Mapped[str | None] = mapped_column(String(80), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship(back_populates="telegram_link")


class TelegramPairingCode(Base):
    __tablename__ = "telegram_pairing_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TelegramExpenseDraft(Base):
    __tablename__ = "telegram_expense_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(String(180))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    transaction_date: Mapped[date] = mapped_column(Date)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_confirmation", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    person: Mapped["Person"] = relationship()
    category: Mapped["Category | None"] = relationship()


class TelegramProcessedUpdate(Base):
    __tablename__ = "telegram_processed_updates"
    id: Mapped[int] = mapped_column(primary_key=True)
    update_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TelegramConversationMessage(Base):
    __tablename__ = "telegram_conversation_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class UserMemory(Base):
    __tablename__ = "user_memories"
    __table_args__ = (UniqueConstraint("user_id", "memory_type", "subject"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    memory_type: Mapped[str] = mapped_column(String(50), index=True)
    subject: Mapped[str] = mapped_column(String(160))
    value_json: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.500"))
    observation_count: Mapped[int] = mapped_column(default=1)
    confirmation_count: Mapped[int] = mapped_column(default=0)
    source: Mapped[str] = mapped_column(String(40), default="conversation")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramPendingAction(Base):
    __tablename__ = "telegram_pending_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="collecting", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FinancingAmortization(Base):
    __tablename__ = "financing_amortizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    financing_id: Mapped[int] = mapped_column(ForeignKey("financings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    amortization_date: Mapped[date] = mapped_column(Date)
    amortized_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    previous_outstanding_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    new_outstanding_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    previous_total_installments: Mapped[int] = mapped_column()
    new_total_installments: Mapped[int] = mapped_column()
    previous_installment_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    new_installment_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    strategy: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(30), default="telegram")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    system_account_id: Mapped[int] = mapped_column(ForeignKey("system_accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[MemberRole] = mapped_column(SAEnum(MemberRole), default=MemberRole.editor)
    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    person_type: Mapped[PersonType] = mapped_column(SAEnum(PersonType), default=PersonType.personal)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserPersonAccess(Base):
    __tablename__ = "user_person_access"
    __table_args__ = (UniqueConstraint("user_id", "person_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    account_type: Mapped[AccountType] = mapped_column(SAEnum(AccountType))
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    color: Mapped[str] = mapped_column(String(7), default="#6c5ce7")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    person: Mapped[Person | None] = relationship()


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str] = mapped_column(String(60), default="")
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    closing_day: Mapped[int] = mapped_column(default=1)
    due_day: Mapped[int] = mapped_column(default=10)
    color: Mapped[str] = mapped_column(String(7), default="#1e293b")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    account: Mapped[Account | None] = relationship()


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("workspace_id", "parent_name", "name", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[TransactionType] = mapped_column(SAEnum(TransactionType))
    parent_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    color: Mapped[str] = mapped_column(String(7), default="#64748b")


class RecurrenceRule(Base):
    __tablename__ = "recurrence_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    frequency: Mapped[str] = mapped_column(String(30), default="monthly")
    transaction_type: Mapped[TransactionType] = mapped_column(SAEnum(TransactionType), default=TransactionType.income)
    days_of_month: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(String(180), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    account: Mapped[Account | None] = relationship()
    person: Mapped[Person | None] = relationship()
    category: Mapped[Category | None] = relationship()


class RecurrenceOccurrence(Base):
    __tablename__ = "recurrence_occurrences"
    __table_args__ = (UniqueConstraint("recurrence_rule_id", "year", "month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recurrence_rule_id: Mapped[int] = mapped_column(ForeignKey("recurrence_rules.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column()
    month: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    remaining_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rule: Mapped[RecurrenceRule] = relationship()


class Financing(Base):
    __tablename__ = "financings"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    recurrence_rule_id: Mapped[int | None] = mapped_column(ForeignKey("recurrence_rules.id", ondelete="SET NULL"), unique=True, nullable=True)
    description: Mapped[str] = mapped_column(String(180))
    paid_installments: Mapped[int] = mapped_column(default=0)
    total_installments: Mapped[int] = mapped_column()
    installment_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    financed_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    outstanding_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    nominal_interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    institution: Mapped[str | None] = mapped_column(String(120), nullable=True)
    due_day: Mapped[int | None] = mapped_column(nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    person: Mapped[Person] = relationship()
    recurrence_rule: Mapped[RecurrenceRule | None] = relationship()


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (UniqueConstraint("workspace_id", "year", "month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column()
    month: Mapped[int] = mapped_column()
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    transaction_type: Mapped[TransactionType] = mapped_column(SAEnum(TransactionType), index=True)
    description: Mapped[str] = mapped_column(String(180))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    competence_year: Mapped[int | None] = mapped_column(nullable=True, index=True)
    competence_month: Mapped[int | None] = mapped_column(nullable=True, index=True)
    status: Mapped[TransactionStatus] = mapped_column(SAEnum(TransactionStatus), default=TransactionStatus.paid)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    destination_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    recurrence_rule_id: Mapped[int | None] = mapped_column(ForeignKey("recurrence_rules.id", ondelete="SET NULL"), nullable=True, index=True)
    payment_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(30), default="web", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    account: Mapped[Account | None] = relationship(foreign_keys=[account_id])
    destination_account: Mapped[Account | None] = relationship(foreign_keys=[destination_account_id])
    card: Mapped[Card | None] = relationship()
    person: Mapped[Person | None] = relationship()
    category: Mapped[Category | None] = relationship()
    recurrence_rule: Mapped[RecurrenceRule | None] = relationship()
