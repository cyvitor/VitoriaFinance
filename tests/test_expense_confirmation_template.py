"""A confirmação de gasto sai do rascunho, mesmo quando o modelo escreve outro texto."""
from datetime import date
from decimal import Decimal
import json

import pytest

from app.database import SessionLocal
from app.models import Account, AccountType, Card, Category, MonthlyBudget, SystemSetting, TransactionType, UserMemory
from app.services.telegram_agent import run_financial_agent
from app.services.telegram_expenses import budget_sentence, parse_quick_expense, render_expense_confirmation
from test_telegram_expenses import setup_linked_user

WHEN = date(2026, 11, 10)
BAD = {
    "122,58 cursor crédito c6": "Confirma o gasto de R$ 122,58 com Cursor, no crédito do C6, categoria Trabalho > Software, para o Vitor?",
    "295,89 americanas crédito c6": "Confirma o gasto de R$ 295,89 na Americanas, no crédito do C6, para o Vitor?",
    "Mc donalds, 12, c6 crédito": "Confirmei aqui:\n\nMc Donalds\n- Valor: R$ 12,00\n- Cartão: C6",
}


def test_template_uses_brazilian_money_including_the_budget_line():
    line = budget_sentence({
        "category": "Alimentação > Padaria", "remaining": "200.00",
        "remaining_after_spending": "184.01", "exceeds_budget": False,
    })
    text = render_expense_confirmation(
        description="Padaria", amount=Decimal("15.99"), when=WHEN, area="Vitor",
        category="Alimentação > Padaria", payment="Crédito", card="C6", budget_line=line,
    )
    assert text == (
        "Confirme este gasto:\n\n"
        "Descricao: Padaria\n"
        "Valor: R$ 15,99\n"
        "Data: 10/11/2026\n"
        "Area: Vitor\n"
        "Categoria: Alimentação > Padaria\n"
        "Forma de pagamento: Crédito\n"
        "Cartao: C6\n\n"
        "Orçamento de Alimentação > Padaria: havia R$ 200,00 disponíveis "
        "e restarão R$ 184,01 após esta compra.\n\n"
        "Posso registrar?"
    )
    assert "200.00" not in text and "184.01" not in text


def _seed(db):
    user = setup_linked_user(db)
    person_id = user.default_person_id
    from app.models import Person
    person = db.get(Person, person_id)
    account = Account(workspace_id=person.workspace_id, person_id=person.id, name="C6",
                      bank_name="C6", account_type=AccountType.checking, initial_balance=Decimal("1000"))
    db.add(account)
    db.flush()
    db.add(Card(workspace_id=person.workspace_id, account_id=account.id, name="C6", closing_day=5, due_day=12))
    category = Category(workspace_id=person.workspace_id, name="Software", parent_name="Trabalho",
                        kind=TransactionType.expense)
    db.add(category)
    db.flush()
    db.add(MonthlyBudget(
        workspace_id=person.workspace_id, person_id=person.id, category_id=category.id,
        amount=Decimal("200.00"), start_year=date.today().year, start_month=1,
        include_in_projection=True, created_by_id=user.id,
    ))
    db.add(UserMemory(
        user_id=user.id, memory_type="category_preference", subject="cursor",
        value_json=json.dumps({"category": "Trabalho > Software"}),
        summary="Cursor é Trabalho > Software.", confidence=Decimal("0.950"),
    ))
    db.add_all([
        SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True),
        SystemSetting(key="deepinfra_model", value="deepseek"),
    ])
    db.commit()
    return user


def _expect(user_text: str) -> str:
    parsed = parse_quick_expense(user_text)
    assert parsed, user_text
    amount = Decimal(parsed["amount"])
    description = parsed["description"]
    category = "Trabalho > Software" if "cursor" in description.casefold() else None
    budget = None
    if category:
        after = Decimal("200.00") - amount
        budget = budget_sentence({
            "category": category, "remaining": "200.00",
            "remaining_after_spending": f"{after:.2f}", "exceeds_budget": False,
        })
    return render_expense_confirmation(
        description=description, amount=amount, when=date.today(), area="Vitor",
        category=category, payment="Crédito", card="C6", budget_line=budget,
        ask_category=category is None,
    )


@pytest.mark.parametrize("mode", ["tool", "respond"])
@pytest.mark.parametrize("user_text", list(BAD))
def test_three_real_inputs_ignore_the_model_confirmation(monkeypatch, mode, user_text):
    bad = BAD[user_text]

    def complete(token, model, messages, *args, **kwargs):
        if kwargs.get("json_mode"):
            if mode == "respond":
                return json.dumps({"action": "respond", "reply": bad})
            return json.dumps({"action": "tool", "tool": "preparar_despesa",
                               "arguments": parse_quick_expense(user_text)})
        return bad

    monkeypatch.setattr("app.services.telegram_agent._complete", complete)
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *args, **kwargs: None)
    with SessionLocal() as db:
        user = _seed(db)
        reply = run_financial_agent(db, user, user_text)
        assert reply == _expect(user_text)
        assert bad not in reply
        assert "200.00" not in reply
        buttons = db.info.get("bot_keyboard")
        assert buttons and buttons[0][0]["text"].endswith("Confirmar")
        assert buttons[0][1]["text"].endswith("Cancelar")
