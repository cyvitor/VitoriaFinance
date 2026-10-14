"""Botoes Confirmar/Cancelar genericos e a escolha 'conta fixa ou despesa nova' (sem LLM)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_bot_logger
from app.models import TelegramExpenseDraft, TelegramPendingAction, User
from app.services import fixed_bills
from app.services.fixed_bills import BotReply, _norm

logger = get_bot_logger()

CHOICE_TYPE = "expense_or_fixed_bill"
CHOICE_EXPIRATION = timedelta(minutes=30)
FIXED_KEYWORDS = re.compile(r"\b(paguei|confirm\w*\s+(?:a\s+|o\s+)?(?:pagamento\s+d[ao]\s+)?conta|contas?\s+fixas?)\b")
NEW_KEYWORDS = re.compile(r"\b(gastei|comprei)\b")


def intent_keyword(text: str) -> str | None:
    """'paguei', 'confirma a conta', 'conta fixa' -> fixed; 'gastei', 'comprei' -> new."""
    normalized = _norm(text)
    if NEW_KEYWORDS.search(normalized):
        return "new"
    if FIXED_KEYWORDS.search(normalized):
        return "fixed"
    return None


def open_rule_match(db: Session, user_id: int, text: str, today: date | None = None):
    """Conta fixa em aberto da area cujo nome aparece no texto (case/acentos ignorados)."""
    today = today or fixed_bills.today_sp()
    context = fixed_bills._context(db, user_id)
    if not context or not context.writable_person_ids:
        return None
    normalized = _norm(text)
    for rule in fixed_bills._rules(db, context):
        words = [item for item in re.findall(r"[a-z0-9]+", _norm(rule.description))
                 if item not in fixed_bills.STOPWORDS and not item.isdigit() and len(item) > 2]
        if not words or not re.search(rf"\b{re.escape(words[0])}\b", normalized):
            continue
        open_bills = fixed_bills._open_bills(db, context, rule, today)
        if open_bills:
            return rule, open_bills[0]
    return None


def payment_text(arguments: dict) -> str:
    return " ".join(str(arguments.get(key) or "") for key in ("card", "payment_method", "account")).strip()


def fixed_bill_arguments(rule, arguments: dict) -> dict:
    return {"rule_id": rule.id, "amount": arguments.get("amount"),
            "paid_date": arguments.get("transaction_date") or arguments.get("paid_date"),
            "account": payment_text(arguments) or None}


def ask_choice(db: Session, user: User, text: str, arguments: dict, rule, bill) -> tuple[str, list]:
    for item in db.scalars(select(TelegramPendingAction).where(
            TelegramPendingAction.user_id == user.id, TelegramPendingAction.action_type == CHOICE_TYPE,
            TelegramPendingAction.status == "collecting")).all():
        item.status = "cancelled"; item.resolved_at = datetime.utcnow()
    action = TelegramPendingAction(
        user_id=user.id, action_type=CHOICE_TYPE, status="collecting",
        expires_at=datetime.utcnow() + CHOICE_EXPIRATION,
        payload=json.dumps({"rule_id": rule.id, "year": bill.year, "month": bill.month,
                            "arguments": arguments, "text": text}, ensure_ascii=False, default=str),
    )
    db.add(action); db.flush()
    logger.info("expense_or_fixed_bill_asked user_id=%s rule_id=%s action_id=%s", user.id, rule.id, action.id)
    reply = (f"Isso é o pagamento da conta fixa **{rule.description}** de **{bill.competence}** "
             f"(previsto {fixed_bills.format_brl(bill.amount)}) ou uma despesa nova?")
    keyboard = [[{"text": "🧾 Conta fixa", "callback_data": f"ec:b:{action.id}"},
                 {"text": "🆕 Despesa nova", "callback_data": f"ec:n:{action.id}"}]]
    return reply, keyboard


def _agent_context(db: Session, user: User):
    from app.services.access_context import build_user_access_context
    from app.services.telegram_agent import scope_to_principal_area
    return scope_to_principal_area(build_user_access_context(db, user))


def _expense_reply(tool: str, result: dict) -> str:
    from app.services.telegram_agent import _expense_tool_reply, _fallback_tool_reply, _fuel_tool_reply
    if result.get("fixed_bill"):
        return str(result.get("message") or "")
    reply = _expense_tool_reply(tool, result, _fallback_tool_reply(tool, result))
    return _fuel_tool_reply(result, reply)


def handle_choice(db: Session, user: User, choice: str, action_id: int) -> BotReply:
    action = db.get(TelegramPendingAction, action_id)
    if not action or action.user_id != user.id or action.action_type != CHOICE_TYPE:
        return BotReply("Esse pedido não pertence a você ou não existe mais.")
    if action.status == "confirmed":
        return BotReply("Essa escolha já foi feita. Continue pela última mensagem.")
    if action.status != "collecting" or action.expires_at < datetime.utcnow():
        return BotReply("Esse pedido não está mais ativo. Envie a mensagem de novo.")
    payload = json.loads(action.payload)
    action.status = "confirmed"; action.resolved_at = datetime.utcnow()
    db.commit()
    arguments = payload.get("arguments") or {}
    if choice == "b":
        result = fixed_bills.prepare_payment(
            db, user.id, rule_id=payload["rule_id"], amount=arguments.get("amount"),
            paid_date=arguments.get("transaction_date"), competence=(payload["year"], payload["month"]),
            account=payment_text(arguments) or None, hint=payload.get("text"))
        logger.info("expense_or_fixed_bill_chosen user_id=%s choice=fixed status=%s", user.id, result.get("status"))
        return BotReply(result.get("message") or "", result.get("keyboard"))
    from app.services.financial_tools import ToolError, execute_tool
    try:
        result = execute_tool(db, _agent_context(db, user), "preparar_despesa", arguments)
    except ToolError as exc:
        result = {"error": str(exc)}
    db.commit()
    logger.info("expense_or_fixed_bill_chosen user_id=%s choice=new status=%s", user.id, result.get("status"))
    return BotReply(_expense_reply("preparar_despesa", result), keyboard_after_tool(db, user.id, "preparar_despesa", result))


def confirm_keyboard(kind: str, item_id: int) -> list[list[dict]]:
    return [[{"text": "✅ Confirmar", "callback_data": f"ok:c:{kind}:{item_id}"},
             {"text": "❌ Cancelar", "callback_data": f"ok:x:{kind}:{item_id}"}]]


def _active_draft(db: Session, user_id: int):
    from app.services.telegram_expenses import get_active_draft
    return get_active_draft(db, user_id)


def _awaiting_action(db: Session, user_id: int):
    return db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id,
        TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]),
    ).order_by(TelegramPendingAction.id.desc()))


EXPENSE_TOOLS = {"preparar_despesa", "preparar_despesas", "atualizar_despesa", "confirmar_despesa",
                 "cancelar_despesa", "sugerir_categoria_despesa"}


def keyboard_after_tool(db: Session, user_id: int, tool: str, result: dict) -> list[list[dict]] | None:
    """Botoes Confirmar/Cancelar quando o resultado da ferramenta deixa algo aguardando confirmacao."""
    if result.get("fixed_bill"):
        return result.get("keyboard")
    if "error" in result:
        return None
    waiting = result.get("status") == "awaiting_confirmation" or bool(result.get("next_expense"))
    if not waiting:
        return None
    if tool in EXPENSE_TOOLS:
        draft = _active_draft(db, user_id)
        if draft and not (result.get("next_expense") or {}).get("missing_fields"):
            return confirm_keyboard("d", draft.id)
        return None
    action = _awaiting_action(db, user_id)
    if action and action.status == "awaiting_confirmation" and action.action_type not in (
            fixed_bills.ACTION_TYPE, CHOICE_TYPE):
        return confirm_keyboard("a", action.id)
    return None


def handle_confirm(db: Session, user: User, verb: str, kind: str, item_id: int) -> BotReply:
    from app.services.financial_tools import ToolError, execute_tool
    if kind == "d":
        draft = db.get(TelegramExpenseDraft, item_id)
        if not draft or draft.user_id != user.id:
            return BotReply("Esse pedido não pertence a você ou não existe mais.")
        active = _active_draft(db, user.id)
        if not active or active.id != item_id:
            db.refresh(draft)
            done = {"confirmed": "Essa despesa já foi registrada.", "cancelled": "Essa despesa já foi cancelada."}
            return BotReply(done.get(draft.status, "Essa despesa não está mais aguardando confirmação."))
        tool = "confirmar_despesa" if verb == "c" else "cancelar_despesa"
    else:
        action = db.get(TelegramPendingAction, item_id)
        if not action or action.user_id != user.id:
            return BotReply("Esse pedido não pertence a você ou não existe mais.")
        latest = _awaiting_action(db, user.id)
        if action.status != "awaiting_confirmation" or not latest or latest.id != action.id:
            done = {"confirmed": "Essa ação já foi confirmada.", "cancelled": "Essa ação já foi cancelada."}
            return BotReply(done.get(action.status, "Essa ação não está mais aguardando confirmação."))
        tool = "confirmar_acao_pendente" if verb == "c" else "cancelar_acao_pendente"
    try:
        result = execute_tool(db, _agent_context(db, user), tool, {})
    except ToolError as exc:
        result = {"error": str(exc)}
    db.commit()
    logger.info("confirmation_button user_id=%s tool=%s item=%s:%s", user.id, tool, kind, item_id)
    reply = _expense_reply(tool, result)
    if verb == "x" and "error" not in result and not result.get("next_expense") and not result.get("fixed_bill"):
        reply = reply if reply and not reply.startswith("A consulta foi executada") else "Cancelado. Nada foi registrado."
    return BotReply(reply, keyboard_after_tool(db, user.id, tool, result))


def keyboard_for_pending(db: Session, user_id: int) -> list[list[dict]] | None:
    """Botoes de confirmacao genericos para rascunho de despesa ou acao pendente."""
    draft = _active_draft(db, user_id)
    if draft:
        return confirm_keyboard("d", draft.id)
    action = _awaiting_action(db, user_id)
    if action and action.status == "awaiting_confirmation" and action.action_type not in (
            fixed_bills.ACTION_TYPE, CHOICE_TYPE):
        return confirm_keyboard("a", action.id)
    return None


def handle_callback(db: Session, user: User, data: str) -> BotReply | None:
    parts = (data or "").split(":")
    try:
        if parts[0] == "ec" and len(parts) == 3 and parts[1] in ("b", "n"):
            return handle_choice(db, user, parts[1], int(parts[2]))
        if parts[0] == "ok" and len(parts) == 4 and parts[1] in ("c", "x") and parts[2] in ("d", "a"):
            return handle_confirm(db, user, parts[1], parts[2], int(parts[3]))
    except ValueError:
        return BotReply("Não reconheci esse botão. Envie a mensagem de novo.")
    return None