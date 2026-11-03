from datetime import date
from decimal import Decimal
import hashlib

from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, AreaAccessLevel, Category, MemberRole, Person, PersonType,
    SystemAccount, Transaction, TransactionStatus, TransactionType, User,
    UserPersonAccess, Workspace, WorkspaceMember,
)
from app.security import hash_password


def create_account_fixture(*, role=AccountRole.admin, read_only=False):
    with SessionLocal() as db:
        system = SystemAccount(name="Mobile Test", is_active=True)
        db.add(system)
        db.flush()
        workspace = Workspace(system_account_id=system.id, name="Principal")
        db.add(workspace)
        db.flush()
        person = Person(workspace_id=workspace.id, name="Pessoal", person_type=PersonType.personal)
        other_person = Person(workspace_id=workspace.id, name="Compartilhado", person_type=PersonType.shared)
        user = User(
            system_account_id=system.id,
            username="mobile-user",
            full_name="Pessoa Mobile",
            password_hash=hash_password("senha-forte-123"),
            account_role=role,
            is_super_admin=(role == AccountRole.admin),
        )
        db.add_all([person, other_person, user])
        db.flush()
        user.default_person_id = person.id
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=MemberRole.admin))
        account = Account(
            workspace_id=workspace.id, person_id=person.id, name="Conta principal",
            account_type=AccountType.checking, initial_balance=Decimal("250.00"),
        )
        other_account = Account(
            workspace_id=workspace.id, person_id=other_person.id, name="Conta compartilhada",
            account_type=AccountType.checking,
        )
        category = Category(
            workspace_id=workspace.id, name="Mercado", parent_name="Alimentação",
            kind=TransactionType.expense, color="#00b894",
        )
        db.add_all([account, other_account, category])
        db.flush()
        if role == AccountRole.member:
            db.add_all([
                UserPersonAccess(user_id=user.id, person_id=person.id,
                                 access_level=AreaAccessLevel.read if read_only else AreaAccessLevel.write),
            ])
        db.commit()
        return {"workspace": workspace.id, "person": person.id, "other_person": other_person.id, "account": account.id,
                "other_account": other_account.id, "category": category.id}


def login(client):
    response = client.post("/api/v1/auth/login", json={
        "username": "mobile-user", "password": "senha-forte-123", "device_name": "pytest",
    })
    assert response.status_code == 200
    return response.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_mobile_api_login_bootstrap_and_revocable_hashed_token(client):
    create_account_fixture()
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={
        "username": "mobile-user", "password": "wrong",
    }).status_code == 401

    token = login(client)
    with SessionLocal() as db:
        from app.models import MobileApiSession
        saved = db.query(MobileApiSession).one()
        assert saved.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert saved.token_hash != token

    me = client.get("/api/v1/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["user"]["full_name"] == "Pessoa Mobile"

    bootstrap = client.get("/api/v1/bootstrap", headers=auth(token)).json()
    assert {area["name"] for area in bootstrap["areas"]} == {"Pessoal", "Compartilhado"}
    assert {account["name"] for account in bootstrap["accounts"]} == {"Conta principal", "Conta compartilhada"}
    assert any(item["name"] == "Mercado" for item in bootstrap["categories"])
    assert len(client.get("/api/v1/areas", headers=auth(token)).json()["items"]) == 2
    assert len(client.get(f"/api/v1/accounts?area_id={bootstrap['selected_area_id']}", headers=auth(token)).json()["items"]) == 1
    assert any(item["name"] == "Mercado" for item in client.get(
        "/api/v1/categories?kind=expense", headers=auth(token),
    ).json()["items"])

    logout = client.post("/api/v1/auth/logout", headers=auth(token))
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 401


def test_mobile_api_creates_expense_and_updates_dashboard_and_month(client):
    ids = create_account_fixture()
    token = login(client)
    today = date.today()
    created = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense",
        "description": "Compra no mercado",
        "amount": "35.90",
        "transaction_date": today.isoformat(),
        "person_id": ids["person"],
        "account_id": ids["account"],
        "category_id": ids["category"],
        "status": "paid",
    })
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["amount"] == "35.90"
    assert saved["category_name"] == "Mercado"
    assert saved["status"] == "paid"

    dashboard = client.get(f"/api/v1/dashboard?area_id={ids['person']}", headers=auth(token)).json()
    assert dashboard["balance"] == "214.10"
    assert dashboard["month"]["expenses"] == "35.90"
    assert dashboard["recent_transactions"][0]["description"] == "Compra no mercado"

    month = client.get(f"/api/v1/months/{today.year}/{today.month}?area_id={ids['person']}",
                       headers=auth(token))
    assert month.status_code == 200
    assert month.json()["transactions"][0]["id"] == saved["id"]
    assert month.json()["expenses"] == "35.90"

    transactions = client.get(
        f"/api/v1/transactions?year={today.year}&month={today.month}", headers=auth(token),
    ).json()
    assert transactions["items"][0]["id"] == saved["id"]


def test_mobile_month_includes_budget_usage_and_projected_balance(client):
    ids = create_account_fixture()
    token = login(client)
    today = date.today()
    for description, amount, state in (("Pago", "35.90", "paid"), ("Pendente", "14.10", "pending")):
        response = client.post("/api/v1/transactions", headers=auth(token), json={
            "transaction_type": "expense", "description": description, "amount": amount,
            "transaction_date": today.isoformat(), "status": state,
            "person_id": ids["person"], "account_id": ids["account"],
            "category_id": ids["category"],
        })
        assert response.status_code == 201, response.text

    budget = client.post("/api/v1/budgets", headers=auth(token), json={
        "person_id": ids["person"], "category_id": ids["category"], "amount": "100.00",
        "start_year": today.year, "start_month": today.month, "include_in_projection": True,
    })
    assert budget.status_code == 201, budget.text
    budget_id = budget.json()["id"]

    response = client.get(f"/api/v1/months/{today.year}/{today.month}?area_id={ids['person']}",
                          headers=auth(token))
    assert response.status_code == 200
    month = response.json()
    assert month["opening_balance"] == "250.00"
    assert month["closing_balance"] == "214.10"
    assert month["projected_balance"] == "150.00"
    assert month["budget_projection_remaining"] == "50.00"
    assert month["budgets"] == [{
        "id": budget_id, "person_id": ids["person"], "person_name": "Pessoal",
        "category_id": ids["category"], "category_name": "Mercado", "parent_name": "Alimentação",
        "start_year": today.year, "start_month": today.month,
        "planned": "100.00", "paid": "35.90", "pending": "14.10", "spent": "50.00",
        "remaining": "50.00", "percent": 50, "exceeded": False, "released": False,
        "released_amount": "0.00", "include_in_projection": True,
    }]

    updated = client.patch(f"/api/v1/budgets/{budget_id}", headers=auth(token), json={
        "amount": "120.00", "start_year": today.year, "start_month": today.month,
        "include_in_projection": False,
    })
    assert updated.status_code == 200
    assert updated.json()["amount"] == "120.00"
    assert client.delete(f"/api/v1/budgets/{budget_id}", headers=auth(token)).status_code == 204


def test_mobile_api_blocks_unassigned_areas_and_read_only_writes(client):
    ids = create_account_fixture(role=AccountRole.member, read_only=True)
    token = login(client)
    bootstrap = client.get("/api/v1/bootstrap", headers=auth(token)).json()
    assert [area["id"] for area in bootstrap["areas"]] == [ids["person"]]

    forbidden_read = client.get(f"/api/v1/dashboard?area_id={ids['other_person']}", headers=auth(token))
    assert forbidden_read.status_code == 403

    forbidden_write = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Sem permissão", "amount": "10.00",
        "transaction_date": date.today().isoformat(), "person_id": ids["person"],
        "account_id": ids["account"],
    })
    assert forbidden_write.status_code == 403

    forbidden_budget = client.post("/api/v1/budgets", headers=auth(token), json={
        "person_id": ids["person"], "category_id": ids["category"], "amount": "100.00",
        "start_year": date.today().year, "start_month": date.today().month,
        "include_in_projection": True,
    })
    assert forbidden_budget.status_code == 403

    invalid_account = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Outra área", "amount": "10.00",
        "transaction_date": date.today().isoformat(), "person_id": ids["person"],
        "account_id": ids["other_account"],
    })
    assert invalid_account.status_code == 403


def test_mobile_api_rejects_invalid_transaction_payload(client):
    ids = create_account_fixture()
    token = login(client)
    response = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Valor inválido", "amount": "0",
        "transaction_date": date.today().isoformat(), "person_id": ids["person"],
    })
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(Transaction).count() == 0


def test_mobile_api_rejects_account_from_another_area(client):
    ids = create_account_fixture()
    token = login(client)
    response = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Conta trocada", "amount": "19.00",
        "transaction_date": date.today().isoformat(), "person_id": ids["person"],
        "account_id": ids["other_account"],
    })
    assert response.status_code == 403


def test_mobile_api_credit_card_purchase_is_pending(client):
    from app.models import Card

    ids = create_account_fixture()
    with SessionLocal() as db:
        card = Card(workspace_id=ids["workspace"], account_id=ids["account"], name="Cartão", brand="Visa",
                    closing_day=10, due_day=20)
        db.add(card)
        db.commit()
        card_id = card.id
    token = login(client)
    response = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Compra crédito", "amount": "70.00",
        "transaction_date": date.today().isoformat(), "person_id": ids["person"],
        "card_id": card_id, "payment_method": "Crédito", "category_id": ids["category"],
    })
    assert response.status_code == 201, response.text
    assert response.json()["status"] == TransactionStatus.pending.value


def test_mobile_api_transfer_appears_in_both_areas_without_changing_balance(client):
    ids = create_account_fixture()
    token = login(client)
    today = date.today()
    response = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "transfer", "description": "Reserva", "amount": "45.00",
        "transaction_date": today.isoformat(), "account_id": ids["account"],
        "destination_account_id": ids["other_account"],
    })
    assert response.status_code == 201, response.text
    transaction_id = response.json()["id"]
    for area_id in (ids["person"], ids["other_person"]):
        month = client.get(f"/api/v1/months/{today.year}/{today.month}?area_id={area_id}",
                           headers=auth(token)).json()
        assert any(item["id"] == transaction_id for item in month["transactions"])
        assert month["net"] == "0.00"


def test_changing_web_password_revokes_mobile_tokens(client):
    create_account_fixture()
    token = login(client)
    client.post("/login", data={"username": "mobile-user", "password": "senha-forte-123"})
    response = client.post("/profile/password", data={
        "current_password": "senha-forte-123",
        "new_password": "senha-nova-forte-123",
        "confirm_password": "senha-nova-forte-123",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 401
