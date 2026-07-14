from decimal import Decimal

from app.database import SessionLocal
from sqlalchemy import func, select
from app.models import Account, AccountRole, AccountType, Card, Category, Financing, Person, PersonType, SystemAccount, SystemSetting, User, Workspace, WorkspaceMember, MemberRole, RecurrenceRule, RecurrenceOccurrence, Transaction, TransactionStatus, TransactionType
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
    response = client.post("/login", data={"username": "vh", "password": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/").status_code == 200
    response = client.post("/profile/password", data={"current_password":"123456", "new_password":"novasenha8", "confirm_password":"novasenha8"}, follow_redirects=False)
    assert response.status_code == 303


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
        financing = db.scalar(select(Financing)); rule_id = financing.recurrence_rule_id
        assert financing.paid_installments == 162 and financing.total_installments == 360
    client.post(f"/recurrences/{rule_id}/confirm", data={"year":"2026", "month":"7",
        "confirmed_amount":"1097.16", "payment_source":f"account:{account_id}"})
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
    common = {"transaction_type":"expense", "transaction_date":"2026-07-10", "description":"Compra",
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
    assert "Valor: maior para menor" in detail.text
    assert "Data/hora: recentes" in detail.text
    assert 'data-sort-target="creditExpenses"' in detail.text
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
        "category_id": str(category_id),
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        parcels = db.scalars(select(Transaction).where(Transaction.description.like("Notebook%"))).all()
        assert len(parcels) == 3
        assert sum((item.amount for item in parcels), Decimal(0)) == Decimal("100.00")
        assert all(item.status == TransactionStatus.pending for item in parcels)
        assert all(item.category_id == category_id for item in parcels)
        first_parcel_id = parcels[0].id
    response = client.post(f"/cards/{card_id}/expenses/{first_parcel_id}/edit", data={
        "description": "Notebook ajustado", "amount": "34.00", "category_id": str(category_id),
    }, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        edited = db.get(Transaction, first_parcel_id)
        assert edited.description == "Notebook ajustado"
        assert edited.amount == Decimal("34.00")


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
