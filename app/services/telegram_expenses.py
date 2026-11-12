from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account, AccountRole, AreaAccessLevel, Card, Category, Person, TelegramExpenseDraft, TelegramExpenseQueueItem,
    Transaction, TransactionStatus, TransactionType, User, UserMemory, UserPersonAccess, WorkspaceMember,
)
from app.services.card_billing import card_purchase_competence

DRAFT_TTL_MINUTES = 30


@dataclass(frozen=True)
class ExpenseInput:
    amount: Decimal
    description: str


def _person_is_writable(db: Session, user: User, person: Person) -> bool:
    if user.is_super_admin or user.account_role == AccountRole.admin:
        return True
    return bool(db.scalar(select(UserPersonAccess.id).where(
        UserPersonAccess.user_id == user.id,
        UserPersonAccess.person_id == person.id,
        UserPersonAccess.access_level == AreaAccessLevel.write,
    )))


def _telegram_context(db: Session, user: User) -> tuple[int, Person] | None:
    if user.default_person_id:
        person = db.get(Person, user.default_person_id)
        if person and person.is_active and db.scalar(select(WorkspaceMember.id).where(
            WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id == person.workspace_id,
        )) and _person_is_writable(db, user, person):
            return person.workspace_id, person
    memberships = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    if len(memberships) == 1:
        people = db.scalars(select(Person).where(
            Person.workspace_id == memberships[0].workspace_id, Person.is_active,
        )).all()
        if len(people) == 1 and _person_is_writable(db, user, people[0]):
            return memberships[0].workspace_id, people[0]
    return None


def _category_label(category: Category) -> str:
    return f"{category.parent_name} > {category.name}" if category.parent_name else category.name


def _memory_category(db: Session, user_id: int | None, categories: list[Category],
                     normalized_description: str) -> Category | None:
    """Categoria que o usuario ja associou ao estabelecimento em uma memoria (ex.: Steam -> Lazer > Jogos)."""
    if not user_id:
        return None
    description_words = set(re.findall(r"[a-z0-9]{3,}", normalized_description))
    memories = db.scalars(select(UserMemory).where(
        UserMemory.user_id == user_id, UserMemory.is_active.is_(True),
        UserMemory.memory_type.in_(("category_preference", "merchant_alias")),
    ).order_by(UserMemory.confidence.desc(), UserMemory.id.desc())).all()
    for memory in memories:
        if not description_words & set(re.findall(r"[a-z0-9]{3,}", _normalize(memory.subject))):
            continue
        text = _normalize(f"{memory.summary} {memory.value_json}")
        found = [category for category in categories if category.parent_name and re.search(
            rf"(?<![a-z0-9]){re.escape(_normalize(category.parent_name + ' ' + category.name))}(?![a-z0-9])", text)]
        if found:
            return max(found, key=lambda category: len(category.name))
    return None


def _infer_category(
    db: Session, workspace_id: int, person_id: int, description: str, user_id: int | None = None,
) -> tuple[Category | None, str | None]:
    categories = db.scalars(select(Category).where(
        Category.workspace_id == workspace_id, Category.kind == TransactionType.expense,
    )).all()
    normalized = _normalize(description)
    history = db.scalars(select(Transaction).where(
        Transaction.workspace_id == workspace_id,
        Transaction.person_id == person_id,
        Transaction.transaction_type == TransactionType.expense,
        Transaction.status != TransactionStatus.cancelled,
        Transaction.category_id.is_not(None),
    ).order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).limit(500)).all()
    same_merchant = [item for item in history if _normalize(item.description) == normalized]
    if same_merchant:
        counts: dict[int, int] = {}
        for item in same_merchant:
            counts[item.category_id] = counts.get(item.category_id, 0) + 1
        best_id = max(counts, key=lambda category_id: (
            counts[category_id],
            next(index for index, item in enumerate(reversed(same_merchant), start=1)
                 if item.category_id == category_id),
        ))
        category = next((item for item in categories if item.id == best_id), None)
        if category:
            return category, "historico do estabelecimento"
    remembered = _memory_category(db, user_id, categories, normalized)
    if remembered:
        return remembered, "memoria do usuario"
    merchant_aliases = {
        "ifood": ("alimentacao", "delivery"),
        "hiperideal": ("alimentacao", "mercado"),
        "barbeiro": ("cuidados pessoais", "barbearia"),
        "barbearia": ("cuidados pessoais", "barbearia"),
        "drogasil": ("saude", "medicamentos"),
        "pague menos": ("saude", "medicamentos"),
    }
    for merchant, target in merchant_aliases.items():
        if merchant in normalized:
            category = next((item for item in categories if (
                _normalize(item.parent_name or "") == target[0]
                and _normalize(item.name) == target[1]
            )), None)
            if category:
                return category, "tipo de estabelecimento"
    exact = [category for category in categories if _normalize(category.name) in normalized]
    if exact:
        return max(exact, key=lambda category: len(category.name)), "descricao"
    return None, None


def suggest_expense_category(db: Session, user: User) -> tuple[TelegramExpenseDraft | None, Category | None, str | None]:
    draft = get_active_draft(db, user.id)
    if not draft:
        return None, None, None
    category, source = _infer_category(
        db, draft.workspace_id, draft.person_id, draft.description, user.id,
    )
    if category:
        draft.category_id = category.id
        db.commit()
        db.refresh(draft)
    return draft, category, source


def _normalize(value: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    ).replace(">", " ").split())


CARD_QUERY_NOISE = {"credito", "debito", "cartao", "de", "do", "no", "na"}


def _card_query(value: str) -> str:
    """Ex.: C6 Credito -> c6; a modalidade vai em payment_method, nao no nome do cartao."""
    return " ".join(word for word in _normalize(value).split() if word not in CARD_QUERY_NOISE)


def _resolve_category(db: Session, workspace_id: int, value: str) -> tuple[Category | None, list[str]]:
    normalized_value = _normalize(value)
    words = normalized_value.split()
    categories = db.scalars(select(Category).where(
        Category.workspace_id == workspace_id, Category.kind == TransactionType.expense,
    )).all()
    aliases = {
        "farmacia": ("saude", "medicamentos"),
        "drogaria": ("saude", "medicamentos"),
        "saude farmacia": ("saude", "medicamentos"),
        "saude drogaria": ("saude", "medicamentos"),
    }
    alias_target = aliases.get(normalized_value)
    if alias_target:
        alias_matches = [
            category for category in categories
            if _normalize(category.parent_name or "") == alias_target[0]
            and _normalize(category.name) == alias_target[1]
        ]
        if len(alias_matches) == 1:
            return alias_matches[0], []
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
    requested_parent = normalized_value.split(" ", 1)[0] if words else ""
    parent_matches = [
        category for category in categories
        if _normalize(category.parent_name or "") == requested_parent
    ]
    if parent_matches:
        return None, [f"{category.parent_name} > {category.name}" for category in parent_matches]
    return None, [f"{category.parent_name} > {category.name}" for category in matches[:12]]


def update_expense_draft(db: Session, user: User, *, description: str | None = None,
                         category: str | None = None,
                         payment_method: str | None = None, card: str | None = None,
                         transaction_date: date | None = None,
                         person: Person | None = None) -> tuple[TelegramExpenseDraft | None, list[str], list[str]]:
    draft = get_active_draft(db, user.id)
    if not draft:
        return None, [], []
    missing = []
    category_options = []
    if person and person.id != draft.person_id:
        if not _person_is_writable(db, user, person):
            missing.append("area com permissao de escrita")
        else:
            # Conta e cartão pertencem à área: nunca carregue esses vínculos para outra área.
            draft.person_id = person.id
            draft.account_id = None
            draft.card_id = None
    if description is not None:
        normalized_description = " ".join(str(description).split())
        if normalized_description:
            draft.description = normalized_description[:180]
        else:
            missing.append("descricao valida")
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
            if not matches and _card_query(card):
                matches = [item for item in cards if _card_query(card) in _normalize(item.name)]
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
    category, _ = _infer_category(db, workspace_id, person.id, expense.description, user.id)
    draft = TelegramExpenseDraft(
        user_id=user.id, workspace_id=workspace_id, person_id=person.id,
        description=expense.description, amount=expense.amount, transaction_date=date.today(),
        category_id=category.id if category else None,
        expires_at=now + timedelta(minutes=DRAFT_TTL_MINUTES),
    )
    if (active and active.person_id == person.id and active.amount == expense.amount
            and _normalize(active.description) == _normalize(expense.description)):
        # Mesmo gasto preparado de novo (ex.: o modelo repete a ferramenta ao receber o cartao):
        # categoria e pagamento ja escolhidos nao podem se perder no rascunho novo.
        draft.category_id = draft.category_id or active.category_id
        draft.payment_method = active.payment_method
        draft.card_id, draft.account_id = active.card_id, active.account_id
        draft.transaction_date = active.transaction_date
        draft.fuel_details = active.fuel_details
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
    person = db.get(Person, draft.person_id)
    if not person or not _person_is_writable(db, user, person):
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


def replace_expense_queue(db: Session, user_id: int, items: list[dict]) -> int:
    now = datetime.utcnow()
    active_items = db.scalars(select(TelegramExpenseQueueItem).where(
        TelegramExpenseQueueItem.user_id == user_id,
        TelegramExpenseQueueItem.status.in_(["queued", "processing"]),
    )).all()
    for item in active_items:
        item.status = "cancelled"
        item.resolved_at = now
    for position, payload in enumerate(items, start=1):
        db.add(TelegramExpenseQueueItem(
            user_id=user_id, position=position,
            payload=json.dumps(payload, ensure_ascii=False), status="queued",
        ))
    db.commit()
    return len(items)


def pop_next_queued_expense(db: Session, user_id: int) -> dict | None:
    item = db.scalar(select(TelegramExpenseQueueItem).where(
        TelegramExpenseQueueItem.user_id == user_id,
        TelegramExpenseQueueItem.status == "queued",
    ).order_by(TelegramExpenseQueueItem.position, TelegramExpenseQueueItem.id))
    if not item:
        return None
    item.status = "processing"
    item.resolved_at = datetime.utcnow()
    db.commit()
    return json.loads(item.payload)


def queued_expense_count(db: Session, user_id: int) -> int:
    return len(db.scalars(select(TelegramExpenseQueueItem.id).where(
        TelegramExpenseQueueItem.user_id == user_id,
        TelegramExpenseQueueItem.status == "queued",
    )).all())


def finish_processing_queue_item(db: Session, user_id: int, status: str) -> None:
    item = db.scalar(select(TelegramExpenseQueueItem).where(
        TelegramExpenseQueueItem.user_id == user_id,
        TelegramExpenseQueueItem.status == "processing",
    ).order_by(TelegramExpenseQueueItem.id.desc()))
    if item:
        item.status = status
        item.resolved_at = datetime.utcnow()
        db.commit()


SEM_CATEGORIA = "Sem categoria"
CATEGORIA_NAO_IDENTIFICADA = (
    "A categoria não foi identificada automaticamente. "
    "Posso registrar assim mesmo ou você quer indicar uma categoria (ex: mercado, lazer, casa)?"
)
_CARD_TOKEN = re.compile(r"c\d{1,3}")
_BANK_TOKEN = {"nubank", "inter", "itau", "bradesco", "santander", "picpay"}
_QUICK_SKIP = re.compile(r"\b(quanto|qual|quais|posso|saldo|resumo|orcamento|fatura|listar|consultar)\b")
_AMOUNT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9/])(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![A-Za-z0-9/])"
)
_DESCRIPTION_NOISE = {
    "credito", "debito", "cartao", "no", "na", "em", "com", "de", "do", "da", "r", "rs", "reais",
    "o", "a", "e", "um", "uma", "no", "pelo", "pela",
}


def format_money(value) -> str:
    """R$ 1.234,56 — sempre com separador brasileiro, inclusive centavos."""
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    formatted = f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}R$ {formatted}"


def payment_label(method: str | None) -> str:
    normalized = _normalize(method or "")
    if not normalized:
        return "Não informada"
    if "credito" in normalized or normalized in {"credit", "cartao"}:
        return "Crédito"
    if "debito" in normalized or "conta" in normalized or normalized in {"cash", "debit", "pix"}:
        return "Débito"
    return str(method).strip()


def budget_sentence(impact: dict | None) -> str | None:
    """Linha de orçamento no mesmo formato brasileiro do restante do resumo."""
    if not impact or not impact.get("category"):
        return None
    before = format_money(impact.get("remaining"))
    category = impact["category"]
    if impact.get("exceeds_budget"):
        over = format_money(impact.get("exceeded_by"))
        return (f"Orçamento de {category}: havia {before} disponíveis e esta compra ultrapassa "
                f"o orçamento em {over}. O lançamento ainda pode ser registrado.")
    after = format_money(impact.get("remaining_after_spending"))
    return (f"Orçamento de {category}: havia {before} disponíveis "
            f"e restarão {after} após esta compra.")


def render_expense_confirmation(*, description: str, amount, when: date, area: str,
                                category: str | None, payment: str | None, card: str | None,
                                budget_line: str | None = None, ask_category: bool = False) -> str:
    """Texto único de confirmação. Sai do rascunho gravado, nunca do texto livre do modelo."""
    lines = [
        "Confirme este gasto:",
        "",
        f"Descricao: {description}",
        f"Valor: {format_money(amount)}",
        f"Data: {when.strftime('%d/%m/%Y')}",
        f"Area: {area}",
        f"Categoria: {category or SEM_CATEGORIA}",
        f"Forma de pagamento: {payment_label(payment)}",
        f"Cartao: {card or 'Não informado'}",
        "",
    ]
    if budget_line:
        lines.extend([budget_line, ""])
    lines.append("Posso registrar?")
    if ask_category and not category:
        lines.extend(["", CATEGORIA_NAO_IDENTIFICADA])
    return "\n".join(lines)


def format_draft(draft: TelegramExpenseDraft, budget_impact: dict | None = None, *,
                 ask_category: bool = False) -> str:
    category = None
    if draft.category:
        category = (f"{draft.category.parent_name} > {draft.category.name}"
                    if draft.category.parent_name else draft.category.name)
    return render_expense_confirmation(
        description=draft.description, amount=draft.amount, when=draft.transaction_date,
        area=draft.person.name if draft.person else "", category=category,
        payment=draft.payment_method, card=draft.card.name if draft.card else None,
        budget_line=budget_sentence(budget_impact),
        ask_category=ask_category and not category,
    )


def parse_quick_expense(text: str) -> dict | None:
    """Lê um gasto curto ('122,58 cursor crédito c6') quando o modelo só escreve a confirmação."""
    raw = " ".join((text or "").split())
    if not raw or "?" in raw or _QUICK_SKIP.search(_normalize(raw)):
        return None
    amounts = _AMOUNT_TOKEN.findall(raw)
    if len(amounts) != 1:
        return None
    from app.services.fixed_bills import parse_amount
    amount = parse_amount(raw)
    if amount is None:
        return None
    match = _AMOUNT_TOKEN.search(raw)
    working = f"{raw[:match.start()]} {raw[match.end():]}" if match else raw
    normalized = _normalize(working)
    payment = "credit" if re.search(r"\bcredito\b", normalized) else (
        "debit" if re.search(r"\bdebito\b", normalized) else None)
    card = None
    description_words = []
    for token in re.findall(r"[A-Za-zÀ-ÿ0-9]+", working):
        norm = _normalize(token)
        if norm in _DESCRIPTION_NOISE:
            continue
        if card is None and (_CARD_TOKEN.fullmatch(norm) or (payment and norm in _BANK_TOKEN)):
            card = token
            continue
        description_words.append(token)
    description = " ".join(description_words).strip()
    if len(description) < 2:
        return None
    arguments = {"amount": str(amount), "description": description[:180]}
    if payment:
        arguments["payment_method"] = payment
    if card:
        arguments["card"] = card
    return arguments
