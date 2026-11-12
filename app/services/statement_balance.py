"""Ajusta o saldo da conta para bater com o extrato, sem lançar receita.

O saldo estimado e saldo inicial + receitas pagas - despesas pagas + transferencias.
A diferenca entra em Account.initial_balance e fica registrada numa linha de auditoria.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import (
    Account, AccountBalanceAdjustment, TelegramPendingAction, Transaction, TransactionStatus, TransactionType, User,
)
from app.services.access_context import build_user_access_context, scope_to_principal_area
from app.services.bot_confirmations import confirm_keyboard
from app.services.fixed_bills import BotReply, format_brl, parse_amount
from app.services.telegram_expenses import format_money

logger = get_bot_logger()
ACTION_BALANCE = "statement_balance"
ACTION_KIND = "document_kind"
EXPIRATION = timedelta(minutes=30)
STATEMENT_TERMS = (
    "extrato", "saldo final", "saldo anterior", "saldo do dia", "saldo em conta",
    "movimentacoes", "posicao em", "agencia",
)
RECEIPT_TERMS = ("cupom fiscal", "nfce", "nfc-e", "danfe", "comprovante", "nota fiscal")
BALANCE_PATTERNS = (
    r"saldo final[^0-9]{0,30}(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"saldo disponivel[^0-9]{0,30}(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"saldo atual[^0-9]{0,30}(\d{1,3}(?:\.\d{3})*,\d{2})",
    r"saldo em conta[^0-9]{0,30}(\d{1,3}(?:\.\d{3})*,\d{2})",
)


class StatementReadError(ValueError):
    pass


def _norm(text: str | None) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", (text or "").casefold())
                   if not unicodedata.combining(char))


def classify_financial_document(text: str) -> str:
    """statement, receipt ou unknown. Um comprovante com um valor continua no fluxo de gasto."""
    normalized = _norm(text)
    statement = sum(term in normalized for term in STATEMENT_TERMS)
    receipt = sum(term in normalized for term in RECEIPT_TERMS)
    if statement and receipt and abs(statement - receipt) <= 1:
        return "unknown"
    if statement >= 2 and statement > receipt:
        return "statement"
    if statement >= 1 and receipt == 0 and any(term in normalized for term in ("extrato", "saldo final", "saldo anterior")):
        return "statement"
    if receipt > statement:
        return "receipt"
    if statement == 0 and parse_amount(text or ""):
        return "receipt"
    return "unknown"


def extract_pdf(data: bytes) -> tuple[str, list[tuple[bytes, str]]]:
    """Texto do PDF e, se ele estiver quase vazio, imagens embutidas para o modelo de visao."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(BytesIO(data))
    except (PdfReadError, OSError, ValueError) as exc:
        raise StatementReadError("PDF ilegível") from exc
    texts = []
    images: list[tuple[bytes, str]] = []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
        if len("\n".join(texts).strip()) >= 40:
            continue
        try:
            embedded = list(page.images)
        except Exception:
            embedded = []
        for image in embedded:
            raw = getattr(image, "data", b"") or b""
            if raw[:8] == b"\x89PNG\r\n\x1a\n":
                images.append((raw, "image/png"))
            elif raw[:2] == b"\xff\xd8":
                images.append((raw, "image/jpeg"))
    return "\n".join(texts).strip(), images


def _money_token(raw: str) -> Decimal | None:
    token = raw.strip()
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    try:
        return Decimal(token).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def extract_statement_fields(text: str) -> dict:
    normalized = _norm(text)
    balance = None
    for pattern in BALANCE_PATTERNS:
        found = re.search(pattern, normalized)
        if found:
            balance = _money_token(found.group(1))
            break
    if balance is None:
        balance = parse_amount(text)
    dates = re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", text or "")
    statement_date = None
    if dates:
        day, month, year = dates[-1]
        try:
            statement_date = date(int(year), int(month), int(day))
        except ValueError:
            statement_date = None
    return {"balance": balance, "statement_date": statement_date, "text": text or ""}


def estimated_balance(db: Session, account: Account) -> Decimal:
    """Saldo que a consulta de contas mostra: inicial + fluxo pago + transferencias."""
    income = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.account_id == account.id, Transaction.transaction_type == TransactionType.income,
        Transaction.status == TransactionStatus.paid,
    )) or 0)
    expense = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.account_id == account.id, Transaction.transaction_type == TransactionType.expense,
        Transaction.status == TransactionStatus.paid,
    )) or 0)
    incoming = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.destination_account_id == account.id, Transaction.transaction_type == TransactionType.transfer,
        Transaction.status == TransactionStatus.paid,
    )) or 0)
    outgoing = Decimal(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.account_id == account.id, Transaction.transaction_type == TransactionType.transfer,
        Transaction.status == TransactionStatus.paid,
    )) or 0)
    return (Decimal(account.initial_balance) + income - expense + incoming - outgoing).quantize(Decimal("0.01"))


def opening_for_statement(account: Account, current: Decimal, target: Decimal) -> Decimal:
    """Novo saldo inicial para o saldo estimado ficar igual ao saldo do extrato."""
    return (Decimal(account.initial_balance) + (target - current)).quantize(Decimal("0.01"))


def _accounts(db: Session, user: User) -> list[Account]:
    context = scope_to_principal_area(build_user_access_context(db, user))
    if not context.allowed_person_ids:
        return []
    return db.scalars(select(Account).where(
        Account.workspace_id.in_(context.workspace_ids), Account.person_id.in_(context.allowed_person_ids),
        Account.is_active.is_(True),
    ).order_by(Account.name)).all()


def match_accounts(accounts: list[Account], text: str) -> list[Account]:
    normalized = _norm(text)
    hits = []
    for account in accounts:
        names = [name for name in (_norm(account.name), _norm(account.bank_name or "")) if len(name) >= 2]
        if any(re.search(rf"\b{re.escape(name)}\b", normalized) for name in names):
            hits.append(account)
    if hits:
        return hits
    return accounts if len(accounts) == 1 else []


def _active(db: Session, user_id: int, action_type: str) -> TelegramPendingAction | None:
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id, TelegramPendingAction.action_type == action_type,
        TelegramPendingAction.status.in_(("collecting", "awaiting_confirmation")),
    ).order_by(TelegramPendingAction.id.desc()))
    if action and action.expires_at < datetime.utcnow():
        action.status = "expired"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return None
    return action


def _store(db: Session, user_id: int, action_type: str, payload: dict, status: str) -> TelegramPendingAction:
    for kind in (ACTION_BALANCE, ACTION_KIND):
        previous = _active(db, user_id, kind)
        if previous:
            previous.status = "cancelled"
            previous.resolved_at = datetime.utcnow()
    action = TelegramPendingAction(
        user_id=user_id, action_type=action_type, status=status,
        payload=json.dumps(payload, ensure_ascii=False, default=str),
        expires_at=datetime.utcnow() + EXPIRATION,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def confirmation_text(account: Account, current: Decimal, target: Decimal, when: date) -> str:
    return "\n".join([
        "Confirme o ajuste de saldo pelo extrato:",
        "",
        f"Conta: {account.name}",
        f"Saldo atual no sistema: {format_money(current)}",
        f"Saldo do extrato: {format_money(target)}",
        f"Diferença: {format_money(target - current)}",
        f"Data do extrato: {when.strftime('%d/%m/%Y')}",
        "",
        "Posso ajustar?",
    ])


def _prepare_account(db: Session, user: User, account: Account, target: Decimal, when: date):
    current = estimated_balance(db, account)
    new_initial = opening_for_statement(account, current, target)
    action = _store(db, user.id, ACTION_BALANCE, {
        "account_id": account.id, "statement_balance": str(target), "statement_date": when.isoformat(),
        "previous_balance": str(current), "previous_initial_balance": str(account.initial_balance),
        "new_initial_balance": str(new_initial),
    }, "awaiting_confirmation")
    return confirmation_text(account, current, target, when), confirm_keyboard("a", action.id)


def prepare_from_document(db: Session, user: User, text: str, today: date | None = None):
    today = today or date.today()
    fields = extract_statement_fields(text)
    if fields["balance"] is None:
        return ("Não encontrei o saldo final nesse extrato. Pode me dizer a conta, a data e o saldo?", None)
    when = fields["statement_date"] or today
    accounts = _accounts(db, user)
    matches = match_accounts(accounts, text)
    if not matches:
        names = ", ".join(item.name for item in accounts) or "nenhuma"
        action = _store(db, user.id, ACTION_KIND, {
            "phase": "account", "balance": str(fields["balance"]), "statement_date": when.isoformat(),
            "text": text[:4000],
        }, "collecting")
        return (f"Não identifiquei a conta do extrato. Qual delas? {names}", None)
    if len(matches) > 1:
        names = ", ".join(item.name for item in matches)
        _store(db, user.id, ACTION_KIND, {
            "phase": "account", "balance": str(fields["balance"]), "statement_date": when.isoformat(),
            "candidates": [item.id for item in matches], "text": text[:4000],
        }, "collecting")
        return (f"Encontrei mais de uma conta nesse extrato: {names}. Qual delas?", None)
    return _prepare_account(db, user, matches[0], fields["balance"], when)


def ask_which(db: Session, user: User, text: str, today: date | None = None) -> tuple[str, list]:
    today = today or date.today()
    action = _store(db, user.id, ACTION_KIND, {"phase": "kind", "text": (text or "")[:4000],
                                               "today": today.isoformat()}, "collecting")
    keyboard = [[
        {"text": "📄 Extrato", "callback_data": f"dk:s:{action.id}"},
        {"text": "🧾 Comprovante", "callback_data": f"dk:r:{action.id}"},
    ]]
    return ("Não tenho certeza se isso é um extrato bancário ou um comprovante de um único gasto. Qual dos dois?",
            keyboard)


def apply_adjustment(db: Session, user: User, action: TelegramPendingAction) -> dict:
    payload = json.loads(action.payload)
    account = db.get(Account, payload.get("account_id"))
    allowed = {item.id for item in _accounts(db, user)}
    if not account or account.id not in allowed:
        action.status = "cancelled"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return {"deterministic": True, "message": "A conta do extrato não está mais disponível. Nada foi alterado."}
    current = estimated_balance(db, account)
    target = Decimal(payload["statement_balance"]).quantize(Decimal("0.01"))
    previous_initial = Decimal(account.initial_balance)
    account.initial_balance = opening_for_statement(account, current, target)
    when = date.fromisoformat(payload["statement_date"])
    db.add(AccountBalanceAdjustment(
        account_id=account.id, user_id=user.id, adjustment_date=when,
        previous_balance=current, new_balance=target,
        previous_initial_balance=previous_initial, new_initial_balance=account.initial_balance,
        description="Ajuste de saldo pelo extrato",
        notes="Saldo Anterior",
    ))
    action.status = "confirmed"
    action.resolved_at = datetime.utcnow()
    db.commit()
    logger.info("statement_balance_adjusted user_id=%s account_id=%s from=%s to=%s",
                user.id, account.id, current, target)
    return {"deterministic": True, "registered": True,
            "message": (f"Saldo da conta **{account.name}** ajustado para {format_brl(target)} "
                        f"(antes {format_brl(current)}). Isso não entra como receita.")}


def cancel(db: Session, user_id: int, action_id: int | None = None) -> dict:
    action = db.get(TelegramPendingAction, action_id) if action_id else (
        _active(db, user_id, ACTION_BALANCE) or _active(db, user_id, ACTION_KIND))
    if not action or action.user_id != user_id or action.action_type not in (ACTION_BALANCE, ACTION_KIND):
        return {"deterministic": True, "message": "Não há ajuste de extrato pendente para cancelar."}
    action.status = "cancelled"
    action.resolved_at = datetime.utcnow()
    db.commit()
    return {"deterministic": True, "cancelled": True, "message": "Cancelado. O saldo não foi alterado."}


def _continue_account_choice(db: Session, user: User, action: TelegramPendingAction, text: str):
    payload = json.loads(action.payload)
    accounts = _accounts(db, user)
    if payload.get("candidates"):
        accounts = [item for item in accounts if item.id in payload["candidates"]]
    matches = match_accounts(accounts, text)
    if len(matches) != 1:
        names = ", ".join(item.name for item in accounts)
        return (f"Qual conta? {names}", None)
    return _prepare_account(db, user, matches[0], Decimal(payload["balance"]), date.fromisoformat(payload["statement_date"]))


def local_reply(db: Session, user: User, text: str, today: date | None = None):
    today = today or date.today()
    if (text or "").startswith("O usuário enviou uma imagem de comprovante"):
        return None
    normalized = _norm(text)
    kind_action = _active(db, user.id, ACTION_KIND)
    if kind_action and kind_action.status == "collecting":
        payload = json.loads(kind_action.payload)
        if normalized in {"nao", "n", "cancelar", "cancela"}:
            result = cancel(db, user.id, kind_action.id)
            return result["message"], None
        if payload.get("phase") == "account":
            return _continue_account_choice(db, user, kind_action, text)
        if payload.get("phase") == "kind":
            if "extrato" in normalized:
                return prepare_from_document(db, user, payload.get("text") or text, today)
            if "comprovante" in normalized or "recibo" in normalized or "gasto" in normalized:
                from app.services.telegram_agent import receipt_prompt, run_financial_agent
                kind_action.status = "confirmed"
                kind_action.resolved_at = datetime.utcnow()
                db.commit()
                reply = run_financial_agent(db, user, receipt_prompt(payload.get("text") or text), today)
                return reply, db.info.get("bot_keyboard")
            return ("Responda **extrato** ou **comprovante**.", ask_which(db, user, payload.get("text") or "", today)[1])
    balance_action = _active(db, user.id, ACTION_BALANCE)
    if balance_action and balance_action.status == "collecting":
        return None
    explicit = any(term in normalized for term in ("ajuste de saldo", "saldo do extrato", "extrato bancario"))
    looks_like_statement = (
        classify_financial_document(text) == "statement"
        and extract_statement_fields(text)["balance"] is not None
        and len(text or "") > 40
    )
    if explicit or looks_like_statement:
        return prepare_from_document(db, user, text, today)
    return None


def handle_callback(db: Session, user: User, data: str):
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "dk" or parts[1] not in {"s", "r"} or not parts[2].isdigit():
        return None
    action = db.get(TelegramPendingAction, int(parts[2]))
    if not action or action.user_id != user.id or action.action_type != ACTION_KIND:
        return None
    payload = json.loads(action.payload)
    if parts[1] == "s":
        text, keyboard = prepare_from_document(db, user, payload.get("text") or "", date.today())
        return BotReply(text, keyboard)
    from app.services.telegram_agent import receipt_prompt, run_financial_agent
    action.status = "confirmed"
    action.resolved_at = datetime.utcnow()
    db.commit()
    reply = run_financial_agent(db, user, receipt_prompt(payload.get("text") or ""))
    return BotReply(reply, db.info.get("bot_keyboard"))
