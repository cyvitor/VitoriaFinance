"""Meta SMART financeira. Os números saem da fala e ficam no Python; o modelo só conversa."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


def _fold(text: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFKD", (text or "").casefold())
        if not unicodedata.combining(char)
    ).split())


_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _end_of_month(year: int, month: int) -> date:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return nxt - timedelta(days=1)


def parse_deadline(text: str, today: date) -> tuple[int, date] | None:
    folded = _fold(text)
    match = re.search(r"(\d+)\s*(semanas?|mes(?:es)?|anos?)\b", folded)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("semana"):
            weeks = value
        elif unit.startswith("mes"):
            weeks = max(1, int(round(value * 30.4 / 7)))
        else:
            weeks = value * 52
        return weeks, today + timedelta(weeks=weeks)
    dated = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", folded)
    target = None
    if dated:
        day, month = int(dated.group(1)), int(dated.group(2))
        year_raw = dated.group(3)
        try:
            year = today.year if not year_raw else int(year_raw) + (2000 if len(year_raw) == 2 else 0)
            target = date(year, month, day)
            if target <= today:
                target = date(year + 1, month, day)
        except ValueError:
            return None
    else:
        for name, number in _MONTHS.items():
            if re.search(rf"\b(ate|para|em)\s+{name}\b", folded):
                target = _end_of_month(today.year, number)
                if target <= today + timedelta(days=7):
                    target = _end_of_month(today.year + 1, number)
                break
    if target is None or target <= today:
        return None
    weeks = max(1, int(math.ceil((target - today).days / 7)))
    return weeks, target


def parse_money(text: str) -> Decimal | None:
    match = re.search(
        r"(?:r\$\s*)(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)",
        text or "",
        re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\b(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d{2,7}(?:,\d{1,2})?)\b", text or "")
    if not match:
        return None
    raw = match.group(1)
    if "." in raw and "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def parse_habit(text: str) -> dict:
    folded = _fold(text)
    days = None
    if re.search(r"\b(todo dia|todos os dias|diariamente|cada dia)\b", folded):
        days = 7
    else:
        count = re.search(r"(\d+)\s*(?:x|vezes)\b", folded)
        if count:
            days = int(count.group(1))
    hour = None
    clock = re.search(r"\b(\d{1,2})\s*h\b", folded)
    if clock:
        value = int(clock.group(1))
        if 0 <= value <= 23:
            hour = f"{value:02d}:00"
    title = "hábito"
    if "academia" in folded:
        title = "ir à academia"
    elif "treinar" in folded or "treino" in folded:
        title = "treinar"
    return {"title": title, "days_per_week": days, "hour": hour}


def _title_from_purchase(text: str) -> str:
    folded = _fold(text)
    if "celular" in folded:
        return "comprar um celular"
    if "viagem" in folded:
        return "fazer uma viagem"
    return "guardar para uma compra"


def build_goal(text: str, today: date) -> dict:
    folded = _fold(text)
    habit_words = any(term in folded for term in ("academia", "treinar", "treino", "todo dia", "todos os dias"))
    money = parse_money(text)
    deadline = parse_deadline(text, today)
    habit = parse_habit(text)
    if habit_words and money is None:
        missing = []
        if not habit["days_per_week"]:
            missing.append("quantos dias por semana")
        goal_type = "habit"
        weeks, end = deadline or (12, today + timedelta(weeks=12))
        days = habit["days_per_week"]
        if days is not None and not 1 <= days <= 7:
            return {"status": "invalid", "summary": "Uma semana tem no máximo 7 dias."}
        sentence = None
        if not missing and days:
            when = f" às {habit['hour']}" if habit["hour"] else ""
            sentence = (
                f"Quero {habit['title']} {days} {'dia' if days == 1 else 'dias'} por semana{when}, "
                f"até {end.strftime('%d/%m/%Y')}."
            )
        return {
            "status": "missing" if missing else "ready",
            "goal_type": goal_type,
            "title": habit["title"],
            "sentence": sentence,
            "target_amount": None,
            "weekly_amount": None,
            "days_per_week": days,
            "hour": habit["hour"],
            "weeks": weeks,
            "end_date": end.isoformat(),
            "missing_fields": missing[:1],
            "tight": False,
        }
    missing = []
    if money is None:
        missing.append("valor da meta")
    if deadline is None:
        missing.append("prazo")
    weeks, end = deadline or (None, None)
    weekly = None
    if money is not None and weeks:
        weekly = (money / Decimal(weeks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    title = _title_from_purchase(text)
    sentence = None
    if not missing and money is not None and end and weekly is not None:
        sentence = (
            f"Quero {title} de R$ {_br(money)} até {end.strftime('%d/%m/%Y')}, "
            f"separando R$ {_br(weekly)} por semana."
        )
    return {
        "status": "missing" if missing else "ready",
        "goal_type": "purchase",
        "title": title,
        "sentence": sentence,
        "target_amount": str(money) if money is not None else None,
        "weekly_amount": str(weekly) if weekly is not None else None,
        "days_per_week": None,
        "hour": None,
        "weeks": weeks,
        "end_date": end.isoformat() if end else None,
        "missing_fields": missing[:1],
        "tight": False,
    }


def _br(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
