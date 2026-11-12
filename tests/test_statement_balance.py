"""Extrato ajusta o saldo inicial e não vira receita."""
from datetime import date
from decimal import Decimal
from io import BytesIO

from sqlalchemy import func, select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.models import (
    Account, AccountBalanceAdjustment, AccountType, Transaction, TransactionStatus, TransactionType,
)
from app.services.statement_balance import (
    classify_financial_document, estimated_balance, extract_pdf, extract_statement_fields,
    opening_for_statement, prepare_from_document,
)
from app.services.telegram_agent import run_financial_agent
from test_telegram_expenses import setup_linked_user

EXTRATO = (
    "Extrato Banco C6\nAgência 0001 Conta C6\n"
    "saldo anterior R$ 100,00\n"
    "saldo final R$ 1.234,56 em 10/11/2026\n"
    "movimentacoes do periodo"
)
RECIBO = "Cupom fiscal Mercado Bom\nValor total R$ 54,90\nNFC-e 10/11/2026"


def pdf_with_text(text: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = page.get("/Resources") or DictionaryObject()
    fonts = resources.get("/Font") or DictionaryObject()
    fonts[NameObject("/F1")] = font
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = stream
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_document_kind_separates_statement_receipt_and_doubt():
    assert classify_financial_document(EXTRATO) == "statement"
    assert classify_financial_document(RECIBO) == "receipt"
    assert classify_financial_document("Mercado Bom, R$ 54,90, hoje, cartão") == "receipt"
    assert classify_financial_document("veja este arquivo por favor") == "unknown"
    fields = extract_statement_fields(EXTRATO)
    assert fields["balance"] == Decimal("1234.56")
    assert fields["statement_date"] == date(2026, 11, 10)


def test_pdf_text_extraction_reads_the_fixture():
    text, images = extract_pdf(pdf_with_text("Extrato Banco C6 saldo final R$ 1.234,56 em 10/11/2026 saldo anterior R$ 100,00"))
    assert "saldo final" in text and "1.234,56" in text
    assert images == []
    blank, _ = extract_pdf(pdf_with_text(""))
    assert blank.strip() == "" or "()" in blank


def test_opening_balance_matches_statement_without_creating_income():
    with SessionLocal() as db:
        user = setup_linked_user(db)
        from app.models import Person
        person = db.get(Person, user.default_person_id)
        account = Account(workspace_id=person.workspace_id, person_id=person.id, name="C6",
                          bank_name="C6", account_type=AccountType.checking, initial_balance=Decimal("100.00"))
        db.add(account)
        db.flush()
        db.add(Transaction(
            workspace_id=person.workspace_id, person_id=person.id, account_id=account.id,
            transaction_type=TransactionType.expense, description="já lançada", amount=Decimal("40.00"),
            transaction_date=date(2026, 11, 1), status=TransactionStatus.paid, created_by_id=user.id,
        ))
        db.commit()
        assert estimated_balance(db, account) == Decimal("60.00")
        assert opening_for_statement(account, Decimal("60.00"), Decimal("90.00")) == Decimal("130.00")
        text, keyboard = prepare_from_document(db, user, EXTRATO.replace("1.234,56", "90,00"), date(2026, 11, 10))
        assert "Conta: C6" in text and "Saldo atual no sistema: R$ 60,00" in text
        assert "Saldo do extrato: R$ 90,00" in text and "Diferença: R$ 30,00" in text
        assert keyboard[0][0]["callback_data"].startswith("ok:c:a:")
        action_id = int(keyboard[0][0]["callback_data"].rsplit(":", 1)[-1])
        from app.services.financial_tools import execute_tool
        from app.services.access_context import build_user_access_context
        result = execute_tool(db, build_user_access_context(db, user), "confirmar_acao_pendente", {})
        assert result["registered"] is True
        db.refresh(account)
        assert account.initial_balance == Decimal("130.00")
        assert estimated_balance(db, account) == Decimal("90.00")
        income = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.transaction_type == TransactionType.income))
        assert Decimal(income) == 0
        assert db.scalar(select(func.count(Transaction.id)).where(Transaction.description == "já lançada")) == 1
        adjustment = db.scalar(select(AccountBalanceAdjustment))
        assert adjustment.previous_balance == Decimal("60.00") and adjustment.new_balance == Decimal("90.00")
        assert adjustment.description == "Ajuste de saldo pelo extrato"
        assert adjustment.notes == "Saldo Anterior"
        assert adjustment.adjustment_date == date(2026, 11, 10)
        assert action_id > 0


def test_telegram_pdf_statement_asks_confirmation_and_receipt_stays_a_single_expense(monkeypatch):
    calls = []

    def fake_agent(db, user, text, reference_date=None, **kwargs):
        calls.append(text)
        return "resumo do comprovante"

    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", fake_agent)
    pdf = pdf_with_text("Extrato Banco C6 saldo final R$ 1.234,56 em 10/11/2026 saldo anterior R$ 100,00 movimentacoes")
    from test_telegram_media import FakeTelegram
    telegram = FakeTelegram(data=pdf)
    with SessionLocal() as db:
        user = setup_linked_user(db)
        from app.models import Person
        person = db.get(Person, user.default_person_id)
        db.add(Account(workspace_id=person.workspace_id, person_id=person.id, name="C6",
                       bank_name="C6", account_type=AccountType.checking))
        db.commit()
        reply = handle_message(db, {
            "document": {"file_id": "PDF1", "mime_type": "application/pdf", "file_name": "extrato.pdf"},
            "from": {"id": 777}, "chat": {"id": 777, "type": "private"},
        }, media_client=telegram)
        assert "Confirme o ajuste de saldo pelo extrato" in reply.text
        assert "R$ 1.234,56" in reply.text
        assert calls == []
        doubt = classify_financial_document("pode ser um boleto ou o extrato da conta, saldo final 10,00 e cupom fiscal")
        assert doubt == "unknown"


def test_text_statement_is_handled_by_the_same_agent(monkeypatch):
    monkeypatch.setattr("app.services.telegram_agent._extract_memories", lambda *a, **k: None)
    with SessionLocal() as db:
        user = setup_linked_user(db)
        from app.models import Person, SystemSetting
        person = db.get(Person, user.default_person_id)
        db.add(Account(workspace_id=person.workspace_id, person_id=person.id, name="C6",
                       bank_name="C6", account_type=AccountType.checking, initial_balance=Decimal("10")))
        db.add(SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True))
        db.commit()
        reply = run_financial_agent(db, user, EXTRATO)
        assert "Confirme o ajuste de saldo pelo extrato" in reply
        assert "C6" in reply
        assert db.scalar(select(func.count(Transaction.id)).where(Transaction.transaction_type == TransactionType.income)) in (None, 0)
