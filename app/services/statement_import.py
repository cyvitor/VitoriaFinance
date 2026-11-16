"""Extrato bancario em PDF vira lancamentos na conta e ajusta o saldo, sem lancar receita para o ajuste.

O texto do extrato e tratado so como dado: nada nele e executado, e o parser (sem LLM) so aceita um
extrato cujas linhas fecham a conta (saldo anterior +/- cada movimento = saldo da linha, e os totais batem).
Se algo nao fechar, o fluxo antigo (apenas ajustar o saldo) continua valendo.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import (
    Account, AccountBalanceAdjustment, TelegramPendingAction, Transaction, TransactionStatus, TransactionType, User,
)
from app.services.access_context import build_user_access_context, scope_to_principal_area
from app.services.bot_confirmations import confirm_keyboard
from app.services.fixed_bills import format_brl

logger = get_bot_logger()
ACTION_IMPORT = "statement_import"
MAX_ENTRIES = 150
MAX_TEXT_CHARS = 30000
MONEY = r"-?\d{1,3}(?:\.\d{3})*,\d{2}-?"
DATE_PREFIX = re.compile(r"^(\d{2}/\d{2}/\d{4})\s*(.*)$")
ENTRY_END = re.compile(rf"^(?P<head>.*?)\s*\b(?P<doc>\d{{4,10}})\s+(?P<amount>{MONEY})\s+(?P<balance>{MONEY})$")
TOTAL_LINE = re.compile(rf"^total\s+(?P<credits>{MONEY})\s+(?P<debits>{MONEY})\s+(?P<balance>{MONEY})$", re.I)
OPENING_LINE = re.compile(rf"^saldo\s+anterior\D{{0,20}}(?P<balance>{MONEY})$", re.I)
HEADER_LINE = re.compile(r"^(bradesco celular|data:|nome:|extrato de|data\s+hist)", re.I)
HEADER_DATE = re.compile(r"^Data:\s*(\d{2}/\d{2}/\d{4})", re.I | re.M)
MARKER = re.compile(r"\b(?:DES|REM):\s*", re.I)
GENERIC_KINDS = ("pix", "compra", "pagto", "pagamento", "debito", "credito")


@dataclass
class StatementEntry:
    date: date
    description: str
    amount: Decimal
    kind: str  # "income" ou "expense"
    payment_method: str


@dataclass
class ParsedStatement:
    entries: list[StatementEntry]
    opening: Decimal
    closing: Decimal
    credits: Decimal
    debits: Decimal
    statement_date: date | None


def _money(raw: str) -> Decimal:
    token = raw.strip()
    negative = token.startswith("-") or token.endswith("-")
    value = Decimal(token.strip("-").replace(".", "").replace(",", "."))
    return (-value if negative else value).quantize(Decimal("0.01"))


def _clean(text: str, limit: int = 180) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text or "").split())[:limit]


def _describe(lines: list[str]) -> tuple[str, str]:
    """(descricao, forma de pagamento) a partir das linhas do historico de um movimento."""
    joined = _clean(" ".join(lines), 400)
    method = "PIX" if re.search(r"\bpix\b", joined, re.I) else "Conta bancária"
    found = MARKER.search(joined)
    if found:
        kind, who = joined[:found.start()].strip(), joined[found.end():].strip()
    else:
        kind, who = (_clean(lines[0], 120), _clean(" ".join(lines[1:]), 300)) if len(lines) > 1 else (joined, "")
    who = re.sub(r"\s+\d{2}/\d{2}$", "", who).strip()
    if not who or re.match(r"^(bco|age|cta):", who, re.I):
        return _clean(kind.title()), method
    if kind.casefold().startswith(GENERIC_KINDS) and "devolucao" not in kind.casefold():
        return _clean(who.title()), method
    return _clean(f"{kind.title()} - {who.title()}"), method


def parse_statement(text: str) -> ParsedStatement | None:
    """Le as movimentacoes de um extrato (data, historico, documento, valor, saldo). None se nao fechar a conta."""
    if not text or len(text) > MAX_TEXT_CHARS:
        return None
    raw_entries: list[tuple[date, list[str], Decimal, Decimal]] = []
    totals = opening_line = None
    current: date | None = None
    buffer: list[str] = []
    for line in (item.strip() for item in text.splitlines()):
        if not line or HEADER_LINE.match(line):
            continue
        total = TOTAL_LINE.match(line)
        if total:
            totals = (_money(total["credits"]), _money(total["debits"]), _money(total["balance"]))
            buffer = []
            continue
        opening = OPENING_LINE.match(line)
        if opening:
            opening_line = _money(opening["balance"])
            continue
        dated = DATE_PREFIX.match(line)
        if dated:
            try:
                day, month, year = dated.group(1).split("/")
                current = date(int(year), int(month), int(day))
            except ValueError:
                return None
            line = dated.group(2).strip()
            if not line:
                continue
        end = ENTRY_END.match(line)
        if end:
            if current is None:
                return None
            if end["head"].strip():
                buffer.append(end["head"].strip())
            if not buffer:
                return None
            raw_entries.append((current, buffer, abs(_money(end["amount"])), _money(end["balance"])))
            buffer = []
        else:
            buffer.append(line)
    if not raw_entries or len(raw_entries) > MAX_ENTRIES:
        return None
    if opening_line is not None:
        opening = opening_line
    elif totals:
        opening = totals[2] - totals[0] + totals[1]
    else:
        return None
    entries: list[StatementEntry] = []
    previous = opening
    for when, lines, amount, balance in raw_entries:
        if balance - previous == amount:
            kind = "income"
        elif previous - balance == amount:
            kind = "expense"
        else:
            return None
        description, method = _describe(lines)
        entries.append(StatementEntry(when, description or "Movimentação do extrato", amount, kind, method))
        previous = balance
    credits = sum((item.amount for item in entries if item.kind == "income"), Decimal("0"))
    debits = sum((item.amount for item in entries if item.kind == "expense"), Decimal("0"))
    if totals and (totals[0] != credits or totals[1] != debits or totals[2] != previous):
        return None
    header = HEADER_DATE.search(text)
    statement_date = None
    if header:
        day, month, year = header.group(1).split("/")
        try:
            statement_date = date(int(year), int(month), int(day))
        except ValueError:
            statement_date = None
    return ParsedStatement(entries, opening, previous, credits, debits, statement_date)


def _existing_duplicates(db: Session, account: Account, entries: list[StatementEntry]) -> list[bool]:
    """Marca os movimentos que ja existem na conta (mesma data, valor e tipo), um lancamento para um movimento."""
    existing = db.scalars(select(Transaction).where(
        Transaction.account_id == account.id,
        Transaction.status != TransactionStatus.cancelled,
        Transaction.transaction_type.in_((TransactionType.income, TransactionType.expense)),
        Transaction.transaction_date >= min(item.date for item in entries),
        Transaction.transaction_date <= max(item.date for item in entries),
    )).all()
    used: set[int] = set()
    flags: list[bool] = []
    for entry in entries:
        match = next((row for row in existing if row.id not in used
                      and row.transaction_date == entry.date and Decimal(row.amount) == entry.amount
                      and row.transaction_type.value == entry.kind), None)
        if match:
            used.add(match.id)
        flags.append(match is not None)
    return flags


def _category(db: Session, account: Account, user: User, entry: StatementEntry):
    from app.services.financial_tools import ToolError, _resolve_income_category
    from app.services.telegram_expenses import _infer_category
    try:
        if entry.kind == "expense":
            return _infer_category(db, account.workspace_id, account.person_id, entry.description, user.id)[0]
        return _resolve_income_category(db, account.workspace_id, None, entry.payment_method)
    except ToolError:
        return None


def _label(category) -> str:
    return f"{category.parent_name} > {category.name}" if category and category.parent_name else (
        category.name if category else "sem categoria")


def build_payload(db: Session, user: User, account: Account, parsed: ParsedStatement, when: date) -> dict:
    from app.services.statement_balance import estimated_balance
    flags = _existing_duplicates(db, account, parsed.entries)
    new, skipped = [], []
    for entry, duplicate in zip(parsed.entries, flags):
        item = {"date": entry.date.isoformat(), "description": entry.description, "amount": str(entry.amount),
                "kind": entry.kind, "payment_method": entry.payment_method}
        if duplicate:
            skipped.append(item)
            continue
        category = _category(db, account, user, entry)
        item["category_id"] = category.id if category else None
        item["category"] = _label(category) if category else None
        new.append(item)
    current = estimated_balance(db, account)
    net = sum((Decimal(i["amount"]) if i["kind"] == "income" else -Decimal(i["amount"]) for i in new), Decimal("0"))
    return {
        "account_id": account.id, "statement_balance": str(parsed.closing),
        "statement_date": when.isoformat(), "previous_balance": str(current),
        "projected_balance": str(current + net), "entries": new, "skipped": skipped,
        "credits": str(sum((Decimal(i["amount"]) for i in new if i["kind"] == "income"), Decimal("0"))),
        "debits": str(sum((Decimal(i["amount"]) for i in new if i["kind"] == "expense"), Decimal("0"))),
    }


def confirmation_text(account: Account, payload: dict) -> str:
    entries, skipped = payload["entries"], payload["skipped"]
    target, projected = Decimal(payload["statement_balance"]), Decimal(payload["projected_balance"])
    lines = [
        "Confirme o lançamento do extrato:", "",
        f"Conta: {account.name}",
        f"Movimentações a lançar: {len(entries)} "
        f"(receitas {format_brl(payload['credits'])}, despesas {format_brl(payload['debits'])})",
        f"Saldo atual no sistema: {format_brl(payload['previous_balance'])}",
        f"Saldo final do extrato: {format_brl(target)}",
        f"Ajuste de saldo (não entra como receita): {format_brl(target - projected)}",
        f"Data do extrato: {date.fromisoformat(payload['statement_date']).strftime('%d/%m/%Y')}",
    ]
    if skipped:
        lines.append(f"Já lançadas, puladas: {len(skipped)}")
    lines.append("")
    used = sum(len(line) + 1 for line in lines)
    shown = 0
    for item in entries:
        sign = "+" if item["kind"] == "income" else "-"
        row = (f"{item['date'][8:]}/{item['date'][5:7]} {sign}{format_brl(item['amount'])[3:]} "
               f"{item['description'][:30]} · {item['category'] or 'sem categoria'}")
        if used + len(row) > 3300:
            break
        lines.append(row)
        used += len(row) + 1
        shown += 1
    if shown < len(entries):
        lines.append(f"… e mais {len(entries) - shown} movimentações")
    uncategorized = sum(1 for item in entries if not item["category"])
    if uncategorized:
        lines.append(f"{uncategorized} ficarão sem categoria.")
    lines += ["", "Posso lançar tudo e ajustar o saldo?"]
    return "\n".join(lines)


def prepare(db: Session, user: User, account: Account, text: str, when: date | None = None):
    """Texto + botoes para confirmar o lancamento do extrato, ou None se o extrato nao puder ser lido linha a linha."""
    from app.services.statement_balance import _store
    parsed = parse_statement(text)
    if parsed is None:
        return None
    payload = build_payload(db, user, account, parsed, parsed.statement_date or when or date.today())
    if not payload["entries"] and not payload["skipped"]:
        return None
    action = _store(db, user.id, ACTION_IMPORT, payload, "awaiting_confirmation")
    return confirmation_text(account, payload), confirm_keyboard("a", action.id)


def _cancel(db: Session, action: TelegramPendingAction, message: str) -> dict:
    action.status = "cancelled"
    action.resolved_at = datetime.utcnow()
    db.commit()
    return {"deterministic": True, "message": message}


def apply(db: Session, user: User, action: TelegramPendingAction) -> dict:
    from app.services.statement_balance import estimated_balance, opening_for_statement
    payload = json.loads(action.payload)
    context = scope_to_principal_area(build_user_access_context(db, user))
    account = db.get(Account, payload.get("account_id"))
    if (not account or not account.is_active or account.person_id not in context.writable_person_ids
            or account.workspace_id not in context.writable_workspace_ids):
        return _cancel(db, action, "A conta do extrato não está mais disponível. Nada foi alterado.")
    before = estimated_balance(db, account)
    created, duplicates = 0, 0
    def _entries(items):
        return [StatementEntry(date.fromisoformat(i["date"]), i["description"], Decimal(i["amount"]), i["kind"],
                               i["payment_method"]) for i in items]
    entries = _entries(payload["entries"])
    skipped_before = len(payload["skipped"])
    # os ja puladas contam primeiro: so entra de novo o que apareceu na conta depois do resumo
    flags = (_existing_duplicates(db, account, _entries(payload["skipped"]) + entries)[skipped_before:]
             if entries else [])
    credits = debits = Decimal("0")
    for item, entry, duplicate in zip(payload["entries"], entries, flags):
        if duplicate:  # alguem lancou o mesmo movimento depois do resumo
            duplicates += 1
            continue
        db.add(Transaction(
            workspace_id=account.workspace_id, person_id=account.person_id, account_id=account.id,
            transaction_type=TransactionType(entry.kind), description=entry.description, amount=entry.amount,
            transaction_date=entry.date, competence_year=entry.date.year, competence_month=entry.date.month,
            status=TransactionStatus.paid, category_id=item.get("category_id"),
            payment_method=entry.payment_method, notes="Importado do extrato bancário",
            created_by_id=user.id, source="telegram",
        ))
        created += 1
        if entry.kind == "income":
            credits += entry.amount
        else:
            debits += entry.amount
    db.flush()
    current = estimated_balance(db, account)
    target = Decimal(payload["statement_balance"]).quantize(Decimal("0.01"))
    previous_initial = Decimal(account.initial_balance)
    account.initial_balance = opening_for_statement(account, current, target)
    db.add(AccountBalanceAdjustment(
        account_id=account.id, user_id=user.id, adjustment_date=date.fromisoformat(payload["statement_date"]),
        previous_balance=before, new_balance=target,
        previous_initial_balance=previous_initial, new_initial_balance=account.initial_balance,
        description="Ajuste de saldo pelo extrato", notes="Saldo Anterior",
    ))
    action.status = "confirmed"
    action.resolved_at = datetime.utcnow()
    db.commit()
    adjustment = target - current
    logger.info("statement_imported user_id=%s account_id=%s created=%s skipped=%s", user.id, account.id,
                created, duplicates + len(payload["skipped"]))
    message = (f"Extrato lançado na conta **{account.name}**: {created} movimentações "
               f"(receitas {format_brl(credits)}, despesas {format_brl(debits)}). "
               f"Saldo ajustado para {format_brl(target)} (ajuste de {format_brl(adjustment)}, "
               f"que não entra como receita).")
    skipped = duplicates + len(payload["skipped"])
    if skipped:
        message += f" {skipped} já estavam lançadas e foram puladas."
    return {"deterministic": True, "registered": True, "created": created, "skipped": skipped,
            "balance": str(target), "adjustment": str(adjustment), "message": message}