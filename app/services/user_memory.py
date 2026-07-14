from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UserMemory


ALLOWED_MEMORY_TYPES = {
    "merchant_alias", "preferred_account", "preferred_card", "category_preference",
    "financial_area_alias", "financing_alias", "user_preference", "financial_goal",
}
ALWAYS_INCLUDED_TYPES = {"user_preference", "financial_area_alias", "financing_alias"}
FORBIDDEN_TERMS = {"senha", "password", "token", "api key", "codigo de autenticacao", "secret"}


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(character for character in text if not unicodedata.combining(character)).split())


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{3,}", _normalize(value))}


def relevant_memories(db: Session, user_id: int, message: str, limit: int = 12) -> list[UserMemory]:
    memories = db.scalars(select(UserMemory).where(
        UserMemory.user_id == user_id, UserMemory.is_active.is_(True)
    )).all()
    message_tokens = _tokens(message)
    ranked = []
    for memory in memories:
        memory_tokens = _tokens(f"{memory.subject} {memory.summary}")
        overlap = len(message_tokens & memory_tokens)
        always = memory.memory_type in ALWAYS_INCLUDED_TYPES
        if overlap or always:
            score = overlap * 10 + float(memory.confidence) + (3 if always else 0)
            ranked.append((score, memory))
    selected = [memory for _, memory in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]
    now = datetime.utcnow()
    for memory in selected:
        memory.last_used_at = now
    return selected


def format_memory_context(memories: list[UserMemory]) -> str:
    if not memories:
        return "Nenhuma memoria permanente relevante."
    lines = []
    for memory in memories:
        confidence = float(memory.confidence)
        guidance = "confirmada" if confidence >= 0.90 else "provavel; confirme antes de assumir"
        lines.append(f"- {memory.summary} (confianca {confidence:.2f}, {guidance})")
    return "Memorias permanentes relevantes:\n" + "\n".join(lines)


def store_candidates(db: Session, user_id: int, candidates: list[dict]) -> int:
    stored = 0
    for candidate in candidates[:10]:
        memory_type = str(candidate.get("type") or "").strip()
        subject = " ".join(str(candidate.get("subject") or "").split())[:160]
        summary = " ".join(str(candidate.get("summary") or "").split())[:500]
        value = candidate.get("value")
        combined = _normalize(f"{subject} {summary} {json.dumps(value, ensure_ascii=False)}")
        if memory_type not in ALLOWED_MEMORY_TYPES or not subject or not summary:
            continue
        if any(term in combined for term in FORBIDDEN_TERMS):
            continue
        try:
            confidence = Decimal(str(candidate.get("confidence", "0.60"))).quantize(Decimal("0.001"))
        except InvalidOperation:
            confidence = Decimal("0.600")
        confidence = max(Decimal("0.400"), min(confidence, Decimal("0.950")))
        normalized_subject = _normalize(subject)
        existing = db.scalar(select(UserMemory).where(
            UserMemory.user_id == user_id, UserMemory.memory_type == memory_type,
            UserMemory.subject == normalized_subject,
        ))
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if existing:
            same_value = existing.value_json == value_json
            existing.observation_count += 1
            existing.is_active = True
            if same_value:
                existing.confidence = min(Decimal("0.990"), Decimal(existing.confidence) + Decimal("0.080"))
                if confidence >= Decimal("0.850"):
                    existing.confirmation_count += 1
            elif confidence >= Decimal(existing.confidence):
                existing.value_json = value_json
                existing.summary = summary
                existing.confidence = confidence
            stored += 1
            continue
        db.add(UserMemory(user_id=user_id, memory_type=memory_type, subject=normalized_subject,
                          value_json=value_json, summary=summary, confidence=confidence,
                          confirmation_count=1 if confidence >= Decimal("0.900") else 0,
                          source="automatic_extraction"))
        stored += 1
    return stored
