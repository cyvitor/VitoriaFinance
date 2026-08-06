from datetime import date
from decimal import Decimal

from app.database import SessionLocal
from sqlalchemy import func, select
from app.models import Account, AccountRole, AccountType, Card, Category, Financing, FinancingAmortization, Person, PersonType, SystemAccount, SystemSetting, User, Workspace, WorkspaceMember, MemberRole, RecurrenceRule, RecurrenceOccurrence, Transaction, TransactionStatus, TransactionType
from app.security import hash_password


def seed_test():
    with SessionLocal() as db:
        account = SystemAccount(name="VH")
        db.add(account); db.flush()
        user = User(system_account_id=account.id, username="vh", full_name="Vitor Hugo", password_hash=hash_password("123456"), is_super_admin=True, account_role=AccountRole.admin)
        workspace = Workspace(system_account_id=account.id, name="Principal")
        db.add_all([user, workspace]); db.flush()
        db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=MemberRole.admin)); db.commit()
        area = Person(workspace_id=workspace.id, name="Vitor", person_type=PersonType.personal)
        db.add(area); db.commit()
        return area.id


def test_health(client): assert client.get("/health").json()["status"] == "ok"


def test_login_dashboard_and_change_password(client):
    seed_test()
    invalid = client.post("/login", data={"username": "vh", "password": "incorreta"})
    assert "Usuário ou senha inválidos" in invalid.text
    assert 'class="auth-alert ' in invalid.text
    response = client.post("/login", data={"username": "vh", "password": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/").status_code == 200
    response = client.post("/profile/password", data={"current_password":"123456", "new_password":"novasenha8", "confirm_password":"novasenha8"}, follow_redirects=False)
    assert response.status_code == 303


def test_month_income_form_is_simplified_and_returns_to_selected_month(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        user = db.scalar(select(User).where(User.username == "vh"))
        user.default_person_id = area_id
        account = Account(
            workspace_id=workspace_id, person_id=area_id, name="C6",
            account_type=AccountType.checking,
        )
        category = Category(
            workspace_id=workspace_id, kind=TransactionType.income,
            parent_name="Receitas", name="PIX",
        )
        db.add_all([account, category]); db.commit()
        account_id, category_id = account.id, category.id

    page = client.get(
        "/transactions/new?kind=income&return_to=month&return_year=2026&return_month=7"
    )
    assert page.status_code == 200
    assert 'name="person_id"' in page.text
    assert f'value="{area_id}" selected' in page.text
    assert 'name="card_id"' not in page.text
    assert 'name="payment_method"' not in page.text

    response = client.post("/transactions/new", data={
        "transaction_type": "income", "description": "Central VT", "amount": "260.00",
        "transaction_date": "2026-07-29", "status": "paid",
        "account_id": str(account_id), "person_id": str(area_id),
        "category_id": str(category_id), "return_to": "month",
        "return_year": "2026", "return_month": "7",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/month?year=2026&month=7"


def test_recurring_income_monthly_confirmation_skip_and_delete(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    response = client.post("/recurring-incomes", data={
        "description": "Salário", "amount": "1000.00", "account_id": "",
        "person_id": str(area_id), "category_id": "",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count(RecurrenceRule.id))) == 1
        assert db.scalar(select(func.count(Transaction.id))) == 0
        rule_id = db.scalar(select(RecurrenceRule.id))
    month = client.get("/month?year=2026&month=7")
    assert month.status_code == 200
    assert "Não receber neste mês" in month.text
    assert "R$ 1.000,00" in month.text
    response = client.post(f"/recurrences/{rule_id}/confirm", data={"year": "2026", "month": "7", "confirmed_amount": "1050.25"}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Transaction.id))) == 1
        assert db.scalar(select(RecurrenceOccurrence.status)) == "confirmed"
        assert db.scalar(select(Transaction.amount)) == Decimal("1050.25")
    response = client.post(f"/recurrences/{rule_id}/skip", data={"year": "2026", "month": "8"}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(RecurrenceOccurrence.status).where(RecurrenceOccurrence.month == 8)) == "skipped"
    response = client.post(f"/recurring-incomes/{rule_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.get(RecurrenceRule, rule_id).is_active is False
        assert db.scalar(select(func.count(Transaction.id))) == 1
    september = client.get("/month?year=2026&month=9")
    assert "Não receber neste mês" not in september.text


def test_recurring_income_can_be_edited_without_changing_confirmed_history(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        user_id = db.scalar(select(User.id).where(User.username == "vh"))
        category = Category(workspace_id=workspace_id, kind=TransactionType.income,
                            parent_name="Receitas", name="Salário", color="#00b894")
        account = Account(workspace_id=workspace_id, person_id=area_id, name="Conta salário",
                          account_type=AccountType.checking)
        rule = RecurrenceRule(workspace_id=workspace_id, transaction_type=TransactionType.income,
                              frequency="monthly", description="Salário", amount=Decimal("1000.00"),
                              person_id=area_id, created_by_id=user_id, is_active=True)
        db.add_all([category, account, rule]); db.flush()
        confirmed = Transaction(workspace_id=workspace_id, transaction_type=TransactionType.income,
            description="Salário", amount=Decimal("1000.00"), transaction_date=date(2026, 7, 5),
            competence_year=2026, competence_month=7, status=TransactionStatus.paid,
            person_id=area_id, recurrence_rule_id=rule.id, created_by_id=user_id)
        db.add(confirmed); db.commit()
        category_id, account_id, rule_id, confirmed_id = category.id, account.id, rule.id, confirmed.id
    page = client.get("/recurring-incomes")
    assert "Editar receita recorrente" in page.text
    response = client.post(f"/recurring-incomes/{rule_id}/edit", data={
        "description": "Salário líquido", "amount": "1125.50", "account_id": str(account_id),
        "person_id": str(area_id), "category_id": str(category_id), "notes": "Novo acordo",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        edited = db.get(RecurrenceRule, rule_id)
        historical = db.get(Transaction, confirmed_id)
        assert edited.description == "Salário líquido" and edited.amount == Decimal("1125.50")
        assert edited.account_id == account_id and edited.category_id == category_id
        assert historical.description == "Salário" and historical.amount == Decimal("1000.00")


def test_fixed_expense_inline_confirm_and_skip(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        account = Account(workspace_id=workspace_id, person_id=area_id, name="Conta", account_type=AccountType.checking)
        db.add(account); db.commit(); account_id = account.id
    response = client.post("/fixed-expenses", data={
        "description": "Aluguel", "amount": "750.00", "account_id": "",
        "person_id": str(area_id), "category_id": "",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        rule = db.scalar(select(RecurrenceRule).where(RecurrenceRule.transaction_type == TransactionType.expense))
        rule_id = rule.id
    month = client.get("/month?year=2026&month=7")
    assert "Aluguel" in month.text
    assert "Confirmar pagamento" in month.text
    assert "Não pagar neste mês" in month.text
    response = client.post(f"/recurrences/{rule_id}/confirm", data={"year": "2026", "month": "7", "confirmed_amount": "732.45", "payment_source": f"account:{account_id}"}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        tx = db.scalar(select(Transaction).where(Transaction.recurrence_rule_id == rule_id))
        assert tx.transaction_type == TransactionType.expense
        assert tx.amount == Decimal("732.45")


def test_fixed_expense_category_can_be_edited(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        category = Category(workspace_id=workspace_id, kind=TransactionType.expense,
                            parent_name="Moradia", name="Aluguel", color="#6c5ce7")
        rule = RecurrenceRule(workspace_id=workspace_id, frequency="monthly",
                              transaction_type=TransactionType.expense, description="Moradia",
                              amount=Decimal("750.00"), person_id=area_id, is_active=True)
        db.add_all([category, rule]); db.commit(); category_id = category.id; rule_id = rule.id
    page = client.get("/fixed-expenses")
    assert "Editar despesa fixa" in page.text and "Categoria" in page.text
    response = client.post(f"/fixed-expenses/{rule_id}/edit", data={
        "description": "Aluguel residencial", "amount": "760.00", "category_id": str(category_id),
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        edited = db.get(RecurrenceRule, rule_id)
        assert edited.category_id == category_id
        assert edited.description == "Aluguel residencial"


def test_fixed_expense_partial_payment_keeps_remaining_and_uses_card(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        account = Account(workspace_id=workspace_id, person_id=area_id, name="C6 Conta", account_type=AccountType.checking)
        db.add(account); db.flush()
        card = Card(workspace_id=workspace_id, account_id=account.id, name="C6")
        db.add(card); db.commit(); card_id = card.id
    client.post("/fixed-expenses", data={"description":"Mercado", "amount":"400.00",
        "account_id":"", "person_id":str(area_id), "category_id":""})
    with SessionLocal() as db:
        rule_id = db.scalar(select(RecurrenceRule.id))
    response = client.post(f"/recurrences/{rule_id}/confirm", data={"year":"2026", "month":"7",
        "confirmed_amount":"150.00", "payment_source":f"card:{card_id}", "settlement_mode":"partial",
        "confirmed_description":"Compra rápida", "paid_on":"2026-07-15", "confirmed_notes":"Itens que faltavam"},
        follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        tx = db.scalar(select(Transaction))
        occurrence = db.scalar(select(RecurrenceOccurrence))
        assert tx.card_id == card_id and tx.status == TransactionStatus.pending
        assert tx.payment_method == "Crédito"
        assert tx.description == "Compra rápida" and tx.transaction_date.isoformat() == "2026-07-15"
        assert tx.notes == "Itens que faltavam"
        assert occurrence.status == "partial" and occurrence.remaining_amount == Decimal("250.00")
    month = client.get("/month?year=2026&month=7")
    assert "R$ 250,00" in month.text


def test_financing_creates_monthly_installment_and_advances_when_paid(client):
    area_id = seed_test()
    client.post("/login", data={"username":"vh", "password":"123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        account = Account(workspace_id=workspace_id, person_id=area_id, name="Conta", account_type=AccountType.checking)
        db.add(account); db.commit(); account_id = account.id
    response = client.post("/financings", data={"description":"Imóvel", "paid_installments":"162",
        "total_installments":"360", "installment_amount":"1097.16", "person_id":str(area_id),
        "account_id":str(account_id), "category_id":"", "financed_amount":"114000.00",
        "outstanding_balance":"85081.93", "nominal_interest_rate":"7.66", "institution":"",
        "due_day":"", "notes":""}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/financings")
    assert "198" in page.text and "85.081,93" in page.text
    assert 'href="/financings" class="active"' in page.text
    with SessionLocal() as db:
        financing = db.scalar(select(Financing)); rule_id = financing.recurrence_rule_id; financing_id = financing.id
        assert financing.paid_installments == 162 and financing.total_installments == 360
    detail = client.get(f"/financings/{financing_id}")
    assert detail.status_code == 200 and "Histórico de amortizações" in detail.text
    response = client.post(f"/financings/{financing_id}/edit", data={
        "description":"Imóvel residencial", "person_id":str(area_id), "institution":"Caixa",
        "account_id":str(account_id), "category_id":"", "financed_amount":"114000.00",
        "nominal_interest_rate":"7.50", "due_day":"10", "start_date":"2020-01-10",
        "notes":"Contrato atualizado",
    }, follow_redirects=False)
    assert response.status_code == 303
    response = client.post(f"/financings/{financing_id}/amortize", data={
        "amortization_date":"2026-07-20", "amortized_amount":"10000.00",
        "new_outstanding_balance":"74000.00", "strategy":"reduce_both",
        "remaining_installments":"120", "new_installment_amount":"900.00",
        "notes":"Uso de FGTS",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        financing = db.get(Financing, financing_id)
        rule = db.get(RecurrenceRule, rule_id)
        history = db.scalar(select(FinancingAmortization))
        assert financing.description == "Imóvel residencial" and financing.institution == "Caixa"
        assert financing.outstanding_balance == Decimal("74000.00")
        assert financing.total_installments == 282 and financing.installment_amount == Decimal("900.00")
        assert rule.amount == Decimal("900.00") and "163/282" in rule.description
        assert history.amortized_amount == Decimal("10000.00") and history.source == "web"
        assert history.previous_total_installments == 360 and history.new_total_installments == 282
    history_page = client.get(f"/financings/{financing_id}")
    assert "Uso de FGTS" in history_page.text and "R$ 10.000,00" in history_page.text
    client.post(f"/recurrences/{rule_id}/confirm", data={"year":"2026", "month":"7",
        "confirmed_amount":"900.00", "payment_source":f"account:{account_id}"})
    with SessionLocal() as db:
        assert db.scalar(select(Financing.paid_installments)) == 163


def test_account_admin_grants_only_selected_areas(client):
    first_area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    client.post("/people", data={"name": "Casal", "person_type": "shared"})
    response = client.post("/account/users", data={
        "username": "esposa", "full_name": "Esposa", "password": "abcdef",
        "account_role": "member", "area_ids": str(first_area_id),
    }, follow_redirects=False)
    assert response.status_code == 303
    client.post("/logout")
    assert client.post("/login", data={"username": "esposa", "password": "abcdef"}).status_code == 200
    areas = client.get("/people")
    assert "Vitor" in areas.text
    assert "Casal" not in areas.text


def test_new_area_becomes_default_and_can_be_changed(client):
    first_area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    client.post("/people", data={"name": "Casal", "person_type": "shared"})
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "vh"))
        casal = db.scalar(select(Person).where(Person.name == "Casal"))
        assert user.default_person_id == casal.id
    client.post(f"/people/{first_area_id}/default")
    with SessionLocal() as db:
        assert db.scalar(select(User.default_person_id).where(User.username == "vh")) == first_area_id


def test_credit_and_debit_card_expenses_are_grouped_and_detailed(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        category = Category(workspace_id=workspace_id, kind=TransactionType.expense,
                            parent_name="Alimentação", name="Mercado", color="#00b894")
        db.add(category); db.flush(); category_id = category.id
        account = Account(workspace_id=workspace_id, person_id=area_id, name="C6 Conta", account_type=AccountType.checking)
        db.add(account); db.flush()
        card = Card(workspace_id=workspace_id, account_id=account.id, name="C6", closing_day=5, due_day=12)
        db.add(card); db.commit(); card_id = card.id; account_id = account.id
    common = {"transaction_type":"expense", "transaction_date":"2026-07-04", "description":"Compra",
              "amount":"100.00", "person_id":str(area_id), "account_id":str(account_id),
              "card_id":str(card_id), "category_id":"", "notes":""}
    client.post("/transactions/new", data={**common, "status":"paid", "payment_method":"Crédito"})
    client.post("/transactions/new", data={**common, "description":"Mercado", "amount":"50.00", "status":"pending", "payment_method":"Débito"})
    with SessionLocal() as db:
        statuses = {item.payment_method: item.status for item in db.scalars(select(Transaction)).all()}
        assert statuses["Crédito"] == TransactionStatus.pending
        assert statuses["Débito"] == TransactionStatus.paid
    month = client.get("/month?year=2026&month=7")
    assert "Cartão Crédito C6" in month.text
    assert "Cartão Débito C6" in month.text
    detail = client.get(f"/cards/{card_id}?year=2026&month=7")
    assert "Compra" in detail.text and "Mercado" in detail.text
    assert 'name="invoice_month"' in detail.text
    assert "Valor: maior para menor" in detail.text
    assert "Data/hora: recentes" in detail.text
    assert 'data-sort-target="creditExpenses"' in detail.text
    analytic = client.get(f"/cards/{card_id}?year=2026&month=7&view=analytic")
    assert "Gastos agrupados" in analytic.text and "Analítica" in analytic.text and "Detalhada" in analytic.text
    analysis = client.get("/analysis?history=12&horizon=24")
    assert analysis.status_code == 200
    assert "Análise financeira" in analysis.text
    assert "Gastos no crédito" in analysis.text
    assert "Projeção de saldo" in analysis.text
    assert "Gasto no cartão" in client.get("/").text
    monthly_analysis = client.get("/monthly-analysis?year=2026&month=7")
    assert monthly_analysis.status_code == 200
    assert "Análise mensal" in monthly_analysis.text
    assert "Comprometimento da renda" in monthly_analysis.text
    assert "Sem categoria" in monthly_analysis.text
    response = client.post("/card-expenses/new", data={
        "card_id": str(card_id), "description": "Notebook", "amount": "100.00",
        "payment_method": "Crédito", "purchase_type": "installments", "installments": "3",
        "category_id": str(category_id), "purchase_date": "2026-07-14",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("&view=detailed")
    with SessionLocal() as db:
        parcels = db.scalars(select(Transaction).where(Transaction.description.like("Notebook%"))).all()
        assert len(parcels) == 3
        assert sum((item.amount for item in parcels), Decimal(0)) == Decimal("100.00")
        assert all(item.status == TransactionStatus.pending for item in parcels)
        assert all(item.category_id == category_id for item in parcels)
        assert [(item.competence_year, item.competence_month) for item in parcels] == [(2026, 7), (2026, 8), (2026, 9)]
        first_parcel_id = parcels[0].id
    open_invoice = client.post("/card-expenses/new", data={
        "card_id": str(card_id), "description": "Compra antes do fechamento", "amount": "20.00",
        "payment_method": "Crédito", "purchase_type": "cash", "installments": "1",
        "category_id": str(category_id), "purchase_date": "2026-08-02",
    }, follow_redirects=False)
    assert "year=2026&month=7" in open_invoice.headers["location"]
    with SessionLocal() as db:
        before_close = db.scalar(select(Transaction).where(Transaction.description == "Compra antes do fechamento"))
        assert (before_close.competence_year, before_close.competence_month) == (2026, 7)
    close_response = client.post(f"/cards/{card_id}/confirm", data={"year": "2026", "month": "7"}, follow_redirects=False)
    assert close_response.status_code == 303
    closed_page = client.get(f"/cards/{card_id}?year=2026&month=7")
    assert "Fechada" in closed_page.text and "Reabrir fatura" in closed_page.text
    next_invoice = client.post("/card-expenses/new", data={
        "card_id": str(card_id), "description": "Compra após fechamento", "amount": "25.00",
        "payment_method": "Crédito", "purchase_type": "cash", "installments": "1",
        "category_id": str(category_id), "purchase_date": "2026-07-20",
    }, follow_redirects=False)
    assert "year=2026&month=8" in next_invoice.headers["location"]
    with SessionLocal() as db:
        later = db.scalar(select(Transaction).where(Transaction.description == "Compra após fechamento"))
        assert (later.competence_year, later.competence_month) == (2026, 8)
    response = client.post(f"/cards/{card_id}/expenses/{first_parcel_id}/edit", data={
        "description": "Notebook ajustado", "amount": "34.00", "category_id": str(category_id),
        "transaction_date": "2026-08-02", "invoice_month": "2026-09",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        edited = db.get(Transaction, first_parcel_id)
        assert edited.description == "Notebook ajustado"
        assert edited.amount == Decimal("34.00")
        assert edited.transaction_date.isoformat() == "2026-08-02"
        assert (edited.competence_year, edited.competence_month) == (2026, 9)
        assert edited.status == TransactionStatus.pending


def test_confirmed_month_transaction_can_be_edited_and_deleted(client):
    area_id = seed_test()
    client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        workspace_id = db.scalar(select(Workspace.id))
        user_id = db.scalar(select(User.id).where(User.username == "vh"))
        category = Category(workspace_id=workspace_id, kind=TransactionType.expense,
                            parent_name="Moradia", name="Energia", color="#fdcb6e")
        rule = RecurrenceRule(workspace_id=workspace_id, transaction_type=TransactionType.expense,
                              frequency="monthly", description="Energia", amount=Decimal("120.00"),
                              person_id=area_id, created_by_id=user_id, is_active=True)
        db.add_all([category, rule]); db.flush()
        tx = Transaction(workspace_id=workspace_id, transaction_type=TransactionType.expense,
                         description="Energia", amount=Decimal("120.00"), transaction_date=date(2026, 7, 10),
                         competence_year=2026, competence_month=7, status=TransactionStatus.paid,
                         person_id=area_id, category_id=category.id, recurrence_rule_id=rule.id,
                         created_by_id=user_id)
        db.add(tx); db.flush()
        occurrence = RecurrenceOccurrence(recurrence_rule_id=rule.id, year=2026, month=7,
                                          status="confirmed", transaction_id=tx.id)
        db.add(occurrence); db.commit(); tx_id = tx.id; category_id = category.id; rule_id = rule.id
    page = client.get("/month?year=2026&month=7")
    assert f'data-bs-target="#manageTransaction{tx_id}"' in page.text
    assert "Editar lançamento" in page.text
    response = client.post(f"/transactions/{tx_id}/edit", data={
        "description": "Energia ajustada", "amount": "127.45", "transaction_date": "2026-07-12",
        "category_id": str(category_id), "notes": "Leitura corrigida", "return_year": "2026",
        "return_month": "7",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        edited = db.get(Transaction, tx_id)
        assert edited.description == "Energia ajustada" and edited.amount == Decimal("127.45")
        assert edited.notes == "Leitura corrigida"
    response = client.post(f"/transactions/{tx_id}/delete", data={
        "return_year": "2026", "return_month": "7",
    }, follow_redirects=False)
    assert response.headers["location"] == "/month?year=2026&month=7"
    with SessionLocal() as db:
        assert db.get(Transaction, tx_id) is None
        assert db.scalar(select(RecurrenceOccurrence).where(
            RecurrenceOccurrence.recurrence_rule_id == rule_id,
            RecurrenceOccurrence.year == 2026, RecurrenceOccurrence.month == 7,
        )) is None
    assert "Confirmar pagamento" in client.get("/month?year=2026&month=7").text


def test_superadmin_creates_account_and_refreshes_deepinfra_models(client, monkeypatch):
    seed_test(); client.post("/login", data={"username": "vh", "password": "123456"})
    response = client.post("/system/accounts", data={
        "account_name":"Amigo", "is_super_account":"on", "admin_name":"João",
        "admin_username":"joao", "admin_password":"123456",
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        account = db.scalar(select(SystemAccount).where(SystemAccount.name == "Amigo"))
        admin = db.scalar(select(User).where(User.username == "joao"))
        assert account.is_super_account is True
        assert admin.account_role == AccountRole.admin and admin.is_super_admin is False
        friend_workspace = db.scalar(select(Workspace).where(Workspace.system_account_id == account.id))
        assert db.scalar(select(func.count(Category.id)).where(Category.workspace_id == friend_workspace.id)) == 82
        account_id = account.id
    client.post("/logout")
    client.post("/login", data={"username":"joao", "password":"123456"})
    assert client.get("/system/accounts").status_code == 200
    assert client.get("/system/settings").status_code == 200
    client.post("/logout"); client.post("/login", data={"username":"vh", "password":"123456"})
    client.post("/system/settings", data={"deepinfra_api_key":"secret", "deepinfra_model":"", "telegram_bot_token":""})
    class FakeResponse:
        def raise_for_status(self): return None
        def json(self): return {"data":[{"id":"meta-llama/Llama-3"},{"id":"deepseek-ai/DeepSeek-V3"}]}
    monkeypatch.setattr("app.routers.web.httpx.get", lambda *args, **kwargs: FakeResponse())
    response = client.post("/system/deepinfra/models", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        catalog = db.scalar(select(SystemSetting.value).where(SystemSetting.key == "deepinfra_models"))
        assert "DeepSeek-V3" in catalog
        vh_account_id = db.scalar(select(SystemAccount.id).where(SystemAccount.name == "VH"))
    client.post("/system/settings", data={
        "deepinfra_api_key":"", "deepinfra_model":"deepseek-ai/DeepSeek-V3",
        "telegram_bot_token":"123456789:abcdefghijklmnopqrstuvwxyzABCDE",
    })
    class FakeChatResponse:
        def raise_for_status(self): return None
        def json(self): return {"choices":[{"message":{"content":"OK"}}]}
    monkeypatch.setattr("app.routers.web.httpx.post", lambda *args, **kwargs: FakeChatResponse())
    assert client.post("/system/deepinfra/test", follow_redirects=False).status_code == 303
    class FakeTelegramResponse:
        def raise_for_status(self): return None
        def json(self): return {"ok":True,"result":{"username":"VitoriaBot"}}
    monkeypatch.setattr("app.routers.web.httpx.get", lambda *args, **kwargs: FakeTelegramResponse())
    assert client.post("/system/telegram/test", follow_redirects=False).status_code == 303
    # A própria conta não pode ser desativada.
    client.post(f"/system/accounts/{vh_account_id}/edit", data={
        "account_name":"VH", "is_super_account":"on",
    })
    with SessionLocal() as db:
        assert db.get(SystemAccount, vh_account_id).is_active is True
    # Outra conta pode ser desativada e seus usuários deixam de autenticar.
    client.post(f"/system/accounts/{account_id}/edit", data={
        "account_name":"Amigo", "is_super_account":"on",
    })
    client.post("/logout")
    response = client.post("/login", data={"username":"joao", "password":"123456"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
