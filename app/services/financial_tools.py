from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account, Card, Category, Financing, FinancingAmortization, Person, RecurrenceRule,
    TelegramPendingAction, Transaction, TransactionStatus, TransactionType, User,
)
from app.services.access_context import UserAccessContext
from app.services.telegram_expenses import (
    ExpenseInput, cancel_expense_draft, confirm_expense_draft, create_expense_draft,
    format_draft, get_active_draft,
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
    items = db.scalars(query.order_by(Transaction.transaction_date.desc()).limit(20)).all()
    return {
        "type": kind.value, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "area": person.name if person else "todas as areas permitidas",
        "category": category_name, "total": _money(total),
        "count": count,
        "items": [{"date": item.transaction_date.isoformat(), "description": item.description,
                   "amount": _money(item.amount), "area": item.person.name if item.person else None,
                   "category": item.category.name if item.category else None} for item in items[:20]],
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
    if user.default_person_id:
        person = db.get(Person, user.default_person_id)
        if person and person.workspace_id not in context.writable_workspace_ids:
            raise ToolError("Usuario possui somente leitura na area financeira padrao")
    draft = create_expense_draft(db, user, ExpenseInput(amount, description[:180]))
    if not draft:
        raise ToolError("Defina uma area financeira padrao antes de registrar pelo Telegram")
    return {"status": "awaiting_confirmation", "summary": format_draft(draft)}


def confirm_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    user = db.get(User, context.user_id)
    transaction = confirm_expense_draft(db, user)
    if not transaction:
        raise ToolError("Nao ha despesa aguardando confirmacao")
    return {"registered": True, "description": transaction.description, "amount": _money(transaction.amount)}


def cancel_expense(db: Session, context: UserAccessContext, args: dict) -> dict:
    user = db.get(User, context.user_id)
    return {"cancelled": cancel_expense_draft(db, user)}


TOOLS = {
    "listar_areas_financeiras": list_financial_areas,
    "consultar_contas": list_accounts,
    "consultar_cartoes": list_cards,
    "consultar_gastos_cartao": query_card_spending,
    "consultar_receitas": lambda db, context, args: query_transactions(db, context, args, TransactionType.income),
    "consultar_despesas": lambda db, context, args: query_transactions(db, context, args, TransactionType.expense),
    "consultar_resumo_mensal": monthly_summary,
    "consultar_financiamentos": list_financings,
    "preparar_amortizacao_financiamento": prepare_financing_amortization,
    "confirmar_acao_pendente": confirm_pending_action,
    "cancelar_acao_pendente": cancel_pending_action,
    "preparar_despesa": prepare_expense,
    "confirmar_despesa": confirm_expense,
    "cancelar_despesa": cancel_expense,
}


def execute_tool(db: Session, context: UserAccessContext, name: str, arguments: dict | None) -> dict:
    tool = TOOLS.get(name)
    if not tool:
        raise ToolError(f"Ferramenta nao permitida: {name}")
    return tool(db, context, arguments or {})
