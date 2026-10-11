from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TelegramExpenseDraft, User
from app.services.llm_client import LLMUnavailable, chat_completion, load_llm_config


class AIUnavailableError(RuntimeError):
    pass


class AIResponseFormatError(AIUnavailableError):
    """O provedor respondeu, mas nao entregou a decisao estruturada esperada."""


class AIToolSelectionError(AIUnavailableError):
    """O modelo nao selecionou uma ferramenta obrigatoria para dados atuais."""


@dataclass(frozen=True)
class FinancialIntent:
    intent: str
    amount: Decimal | None = None
    description: str | None = None
    reply: str | None = None


def _extract_json(content: str) -> dict:
    content = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    else:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            content = content[start:end + 1]
    return json.loads(content)


def interpret_financial_message(
    db: Session, user: User, text: str, active_draft: TelegramExpenseDraft | None,
) -> FinancialIntent:
    config = load_llm_config(db)
    if config is None:
        raise AIUnavailableError("DeepInfra nao configurada")
    draft_context = "Nenhum gasto aguardando confirmacao."
    if active_draft:
        draft_context = (
            f"Ha um gasto aguardando confirmacao: descricao={active_draft.description!r}, "
            f"valor={active_draft.amount}."
        )
    system_prompt = f"""Voce e Vitoria, gestora financeira pessoal de {user.full_name}.
Voce e Vitoria; nunca chame o usuario de Vitoria. Trate-o pelo nome informado ou sem usar nome.
Interprete a mensagem em portugues brasileiro. {draft_context}
Retorne SOMENTE JSON valido, sem markdown, neste formato:
{{"intent":"create_expense|confirm|cancel|chat","amount":number|null,"description":string|null,"reply":string|null}}
Regras:
- create_expense quando a pessoa disser que gastou, pagou ou comprou algo; extraia valor e uma descricao curta.
- confirm quando ela aceitar claramente o gasto que aguarda confirmacao, inclusive respostas como sim, pode, correto, confirma.
- cancel quando ela recusar ou pedir para cancelar o rascunho.
- Nunca invente valor. Se faltar valor ou descricao, use chat e pergunte naturalmente o dado ausente em reply.
- Para outros assuntos use chat e responda de forma breve em reply, explicando que neste momento voce registra gastos.
- Nao afirme que um gasto foi salvo; o sistema fara isso depois.
"""
    try:
        content, _model = chat_completion(
            config, [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
            220, purpose="intent",
        )
        payload = _extract_json(content)
        intent = payload.get("intent")
        if intent not in {"create_expense", "confirm", "cancel", "chat"}:
            raise ValueError("intencao invalida")
        amount = None
        if payload.get("amount") is not None:
            amount = Decimal(str(payload["amount"])).quantize(Decimal("0.01"))
            if amount <= 0:
                raise ValueError("valor invalido")
        description = " ".join(str(payload.get("description") or "").split()) or None
        if description and len(description) > 180:
            description = description[:180]
        if intent == "create_expense" and (amount is None or not description):
            return FinancialIntent("chat", reply="Qual foi o valor e a descricao desse gasto?")
        return FinancialIntent(intent, amount, description, payload.get("reply"))
    except (LLMUnavailable, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, InvalidOperation) as exc:
        raise AIUnavailableError(str(exc)) from exc
