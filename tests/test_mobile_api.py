from datetime import date
from decimal import Decimal
import hashlib

from app.database import SessionLocal
from app.models import (
    Account, AccountRole, AccountType, AccountingPeriod, AreaAccessLevel, Category, ChatMedia, MemberRole, Person, PersonType,
    FinancialGoal, SystemAccount, SystemSetting, TelegramConversationMessage, Transaction, TransactionStatus, TransactionType, User,
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


def test_mobile_app_can_configure_and_confirm_a_recurring_income(client):
    from app.models import Category, RecurrenceOccurrence

    ids = create_account_fixture()
    with SessionLocal() as db:
        category = Category(workspace_id=ids["workspace"], name="Salário", parent_name="Receitas",
            kind=TransactionType.income, color="#00b894")
        db.add(category)
        db.commit()
        category_id = category.id
    token = login(client)
    rule = client.post("/api/v1/recurrences", headers=auth(token), json={
        "transaction_type": "income", "description": "Salário mensal", "amount": "5200.00",
        "person_id": ids["person"], "account_id": ids["account"], "category_id": category_id,
    })
    assert rule.status_code == 201, rule.text
    assert rule.json()["account_id"] == ids["account"]
    today = date.today()
    paid = client.post(f"/api/v1/recurrences/{rule.json()['id']}/confirm", headers=auth(token), json={
        "year": today.year, "month": today.month, "amount": "5200.00",
        "transaction_date": today.isoformat(),
    })
    assert paid.status_code == 200, paid.text
    assert paid.json()["transaction"]["status"] == "paid"
    with SessionLocal() as db:
        occurrence = db.query(RecurrenceOccurrence).filter_by(recurrence_rule_id=rule.json()["id"]).one()
        assert occurrence.status == "confirmed"


def test_mobile_card_statement_payment_and_installments(client):
    from app.models import Card

    ids = create_account_fixture()
    with SessionLocal() as db:
        card = Card(workspace_id=ids["workspace"], account_id=ids["account"], name="Cartão", brand="Visa",
                    closing_day=10, due_day=20)
        db.add(card)
        db.commit()
        card_id = card.id
    token = login(client)
    today = date.today()
    purchase = client.post("/api/v1/transactions", headers=auth(token), json={
        "transaction_type": "expense", "description": "Notebook", "amount": "1000.00",
        "transaction_date": today.isoformat(), "person_id": ids["person"], "card_id": card_id,
        "payment_method": "Crédito", "category_id": ids["category"], "installments": 4,
    })
    assert purchase.status_code == 201, purchase.text
    assert purchase.json()["installment_count"] == 4
    assert [x["amount"] for x in purchase.json()["installments"]] == ["250.00"] * 4
    invoice_year = purchase.json()["competence_year"]
    invoice_month = purchase.json()["competence_month"]
    statement = client.get(f"/api/v1/cards/{card_id}/statement?year={invoice_year}&month={invoice_month}",
                           headers=auth(token))
    assert statement.status_code == 200, statement.text
    assert statement.json()["credit_total"] == "250.00"
    assert statement.json()["is_closed"] is False
    settled = client.post(f"/api/v1/cards/{card_id}/billing/confirm?year={invoice_year}&month={invoice_month}",
                          headers=auth(token))
    assert settled.status_code == 200, settled.text
    statement = client.get(f"/api/v1/cards/{card_id}/statement?year={invoice_year}&month={invoice_month}",
                           headers=auth(token)).json()
    assert statement["is_closed"] is True
    assert statement["transactions"][0]["status"] == "paid"


def test_mobile_analysis_and_assistant_history(client, monkeypatch):
    create_account_fixture()
    token = login(client)
    report = client.get("/api/v1/analysis/summary?history=6&horizon=3", headers=auth(token))
    assert report.status_code == 200, report.text
    assert len(report.json()["monthly"]) == 6
    assert len(report.json()["projection"]) == 3
    monkeypatch.setattr("app.services.telegram_agent.run_financial_agent",
        lambda db, user, text, **kwargs: f"Resposta local: {text}")
    reply = client.post("/api/v1/assistant/messages", headers=auth(token), json={"message": "Oi Vitoria"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["reply"] == "Resposta local: Oi Vitoria"
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="mobile-user").one()
        other = User(system_account_id=user.system_account_id, username="other-user", full_name="Outra pessoa",
            password_hash=hash_password("senha-forte-123"), account_role=AccountRole.member)
        db.add(other); db.flush()
        db.add_all([TelegramConversationMessage(user_id=user.id, channel=("telegram" if index % 2 else "app"),
            role="user", content=f"mensagem {index}") for index in range(105)])
        db.add(TelegramConversationMessage(user_id=other.id, channel="app", role="user", content="segredo de outra pessoa"))
        db.commit()
    first = client.get("/api/v1/assistant/history", headers=auth(token)).json()
    history = first["items"]
    assert len(history) == 20  # padrao: a mesma janela que o modelo enxerga, nao os 107 registros
    assert history[0]["content"] == "mensagem 85" and history[-1]["content"] == "mensagem 104"
    assert first["has_more"] is True and first["memory_start_id"] == history[0]["id"]
    assert all("segredo" not in item["content"] for item in history)
    older = client.get(f"/api/v1/assistant/history?before_id={history[0]['id']}&limit=50", headers=auth(token)).json()
    assert [row["content"] for row in older["items"]][-1] == "mensagem 84" and len(older["items"]) == 50
    assert older["items"][0]["content"] == "mensagem 35" and older["has_more"] is True
    assert older["memory_start_id"] == first["memory_start_id"]  # o marcador "memoria comeca aqui" nao muda ao paginar
    everything = client.get(f"/api/v1/assistant/history?before_id={older['items'][0]['id']}&limit=100", headers=auth(token)).json()
    assert everything["has_more"] is False and everything["items"][-1]["content"] == "mensagem 34"
    assert len(everything["items"]) == 35  # mensagens 0 a 34, as unicas que sobraram
    assert client.get("/api/v1/assistant/history?limit=0", headers=auth(token)).status_code == 422


def test_mobile_recurrence_write_respects_area_permissions(client):
    ids = create_account_fixture(role=AccountRole.member, read_only=True)
    token = login(client)
    response = client.post("/api/v1/recurrences", headers=auth(token), json={
        "transaction_type": "income", "description": "Salário", "amount": "2000.00",
        "person_id": ids["person"], "account_id": ids["account"],
    })
    assert response.status_code == 403


def test_mobile_vehicle_updates_and_financing_detail_exposes_contract_terms(client):
    ids = create_account_fixture()
    token = login(client)
    vehicle = client.post("/api/v1/vehicles", headers=auth(token), json={
        "name": "Carro de teste", "year": 2022, "initial_odometer_km": "32000", "fuel_type": "Flex",
    })
    assert vehicle.status_code == 201, vehicle.text
    vehicle_id = vehicle.json()["id"]
    updated = client.patch(f"/api/v1/vehicles/{vehicle_id}", headers=auth(token), json={
        "name": "Carro da família", "year": 2022, "initial_odometer_km": "32000",
        "fuel_type": "Flex", "is_active": True,
    })
    assert updated.status_code == 200
    assert client.get("/api/v1/vehicles", headers=auth(token)).json()["vehicles"][0]["name"] == "Carro da família"
    contract = client.post("/api/v1/financings", headers=auth(token), json={
        "description": "Financiamento de teste", "person_id": ids["person"],
        "total_installments": 24, "installment_amount": "850.00", "financed_amount": "18000.00",
        "outstanding_balance": "12000.00", "nominal_interest_rate": "1.2000",
        "institution": "Banco de teste", "due_day": 12, "notes": "Contrato de validação",
    })
    assert contract.status_code == 201, contract.text
    detail = client.get(f"/api/v1/financings/{contract.json()['id']}", headers=auth(token)).json()["financing"]
    assert detail["institution"] == "Banco de teste"
    assert detail["financed_amount"] == "18000.00"
    assert detail["nominal_interest_rate"] == "1.2000"
    assert detail["due_day"] == 12 and detail["notes"] == "Contrato de validação"


def test_mobile_smart_goal_tracks_savings_and_is_scoped_to_creator(client):
    ids = create_account_fixture()
    token = login(client)
    goal = client.post("/api/v1/goals", headers=auth(token), json={
        "goal_type": "purchase", "title": "Viagem", "sentence": "Viajar até dezembro",
        "target_amount": "2400.00", "weekly_amount": "200.00", "start_date": "2026-01-01",
        "end_date": "2026-03-01", "weeks": 12, "person_id": ids["person"],
    })
    assert goal.status_code == 201, goal.text
    goal_id = goal.json()["id"]
    response = client.post(f"/api/v1/goals/{goal_id}/progress", headers=auth(token), json={"amount": "2400.00"})
    assert response.status_code == 200
    assert response.json()["saved_amount"] == "2400.00" and response.json()["status"] == "completed"
    listed = client.get("/api/v1/goals", headers=auth(token)).json()["items"]
    assert len(listed) == 1 and listed[0]["sentence"] == "Viajar até dezembro"
    assert listed[0]["saved_amount"] == "2400.00"


def test_chat_smart_goal_requires_explicit_confirmation():
    from app.services.access_context import build_user_access_context, scope_to_principal_area
    from app.services.financial_tools import execute_tool
    create_account_fixture()
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="mobile-user").one()
        context = scope_to_principal_area(build_user_access_context(db, user))
        proposal = execute_tool(db, context, "preparar_meta_smart", {
            "text": "Quero juntar R$ 2.400 para uma viagem em 12 semanas",
        })
        assert proposal["status"] == "awaiting_confirmation"
        assert "R$ 200,00 por semana" in proposal["summary"]
        assert db.query(FinancialGoal).count() == 0
        saved = execute_tool(db, context, "confirmar_acao_pendente", {})
        assert saved["registered"] is True
        assert db.query(FinancialGoal).count() == 1


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


def test_mobile_bootstrap_prefers_current_open_month_and_month_can_be_closed(client):
    ids = create_account_fixture()
    token = login(client)
    today = date.today()
    with SessionLocal() as db:
        from app.models import Workspace
        workspace = db.get(Workspace, ids["workspace"])
        db.add(AccountingPeriod(workspace_id=workspace.id, year=today.year, month=max(1, today.month - 1), is_closed=True))
        db.commit()
    bootstrap = client.get("/api/v1/bootstrap", headers=auth(token)).json()
    assert bootstrap["open_month"] == {"year": today.year, "month": today.month}
    close = client.post(f"/api/v1/months/{today.year}/{today.month}/close", headers=auth(token))
    assert close.status_code == 200
    month = client.get(f"/api/v1/months/{today.year}/{today.month}", headers=auth(token)).json()
    assert month["is_closed"] is True


def test_mobile_chat_audio_and_receipt_are_private_attachments(client, monkeypatch, tmp_path):
    ids = create_account_fixture()
    token = login(client)
    monkeypatch.setenv("CHAT_MEDIA_STORAGE_DIR", str(tmp_path))

    def fake_agent(db, user, text, channel="telegram", user_message=None, stored_user_content=None, **kwargs):
        if user_message is not None:
            user_message.content = stored_user_content or text
        db.add_all([
            *([] if user_message is not None else [TelegramConversationMessage(user_id=user.id, role="user", content=text, channel=channel)]),
            TelegramConversationMessage(user_id=user.id, role="assistant", content="Posso registrar após sua confirmação.", channel=channel),
        ])
        db.commit()
        return "Posso registrar após sua confirmação."

    monkeypatch.setattr("app.services.telegram_agent.run_financial_agent", fake_agent)
    monkeypatch.setattr("app.routers.mobile_extended.transcribe_audio", lambda *args: "Paguei vinte reais no mercado")
    monkeypatch.setattr("app.routers.mobile_extended.describe_receipt", lambda *args: "Mercado, R$ 20,00, hoje")
    monkeypatch.setattr("app.routers.mobile_extended.synthesize_speech", lambda *args: (b"RIFFfake", "audio/wav"))
    with SessionLocal() as db:
        db.add_all([
            SystemSetting(key="deepinfra_api_key", value="test-key", is_secret=True),
            SystemSetting(key="audio_generation_model", value="test/tts", is_secret=False),
        ])
        db.commit()

    audio = client.post("/api/v1/assistant/media/audio", headers=auth(token),
        files={"file": ("voice.m4a", b"fake-audio", "audio/mp4")})
    assert audio.status_code == 202, audio.text
    assert audio.json()["status"] == "pending"
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    user_audio = next(row for row in history if row.get("media", [{}])[0].get("kind") == "audio_user")
    assistant = next(row for row in history if row["role"] == "assistant")
    assert user_audio["status"] == "done" and user_audio["id"] == audio.json()["id"]
    with SessionLocal() as db:  # the history keeps the transcription for the model, the app shows "Audio enviado."
        stored = db.get(TelegramConversationMessage, user_audio["id"])
        assert stored.content.endswith("Transcri\u00e7\u00e3o: Paguei vinte reais no mercado")
        assert "usu\u00e1rio enviou" not in stored.content
    assert user_audio["content"] == "Áudio enviado."
    assert user_audio["media"][0]["transcript"] == "Paguei vinte reais no mercado"
    assert any(media["kind"] == "audio_response" for media in assistant["media"])
    media_id = user_audio["media"][0]["id"]
    assert client.get(f"/api/v1/assistant/media/{media_id}", headers=auth(token)).content == b"fake-audio"
    assert client.get(f"/api/v1/assistant/media/{media_id}", headers=auth(token)).headers["cache-control"] == "private, no-store"
    with SessionLocal() as db:
        original = db.query(User).filter_by(username="mobile-user").one()
        other = User(system_account_id=original.system_account_id, username="media-other-user",
            full_name="Outra Pessoa", password_hash=hash_password("senha-forte-123"))
        db.add(other); db.flush()
        db.add(WorkspaceMember(workspace_id=ids["workspace"], user_id=other.id, role=MemberRole.admin))
        db.commit()
    other_token = client.post("/api/v1/auth/login", json={
        "username": "media-other-user", "password": "senha-forte-123",
    }).json()["access_token"]
    assert client.get(f"/api/v1/assistant/media/{media_id}", headers=auth(other_token)).status_code == 404

    image = client.post("/api/v1/assistant/media/image", headers=auth(token),
        files={"file": ("receipt.jpg", b"fake-image", "image/jpeg")})
    assert image.status_code == 202, image.text
    assert image.json()["status"] == "pending"
    image_history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    sent_image = next(row for row in image_history if row["id"] == image.json()["id"])
    assert sent_image["status"] == "done" and sent_image["content"] == "Comprovante enviado."
    assert sent_image["media"][0]["kind"] == "receipt_image"
    assert sent_image["media"][0]["transcript"].endswith("Mercado, R$ 20,00, hoje")
    latest_assistant = next(row for row in reversed(image_history) if row["role"] == "assistant")
    assert not any(media["kind"] == "audio_response" for media in latest_assistant.get("media", []))


def _audio_job_env(client, monkeypatch, tmp_path, transcribe=None):
    create_account_fixture()
    token = login(client)
    monkeypatch.setenv("CHAT_MEDIA_STORAGE_DIR", str(tmp_path))

    def fake_agent(db, user, text, channel="telegram", user_message=None, stored_user_content=None, **kwargs):
        user_message.content = stored_user_content or text
        db.add(TelegramConversationMessage(user_id=user.id, role="assistant", content="Certo.", channel=channel))
        db.commit()
        return "Certo."

    monkeypatch.setattr("app.services.telegram_agent.run_financial_agent", fake_agent)
    monkeypatch.setattr("app.routers.mobile_extended.transcribe_audio", transcribe or (lambda *args: "oi vitoria"))
    monkeypatch.setattr("app.routers.mobile_extended.synthesize_speech", lambda *args: (b"RIFFfake", "audio/wav"))
    return token


def test_audio_upload_is_idempotent_and_reply_audio_is_marked_played_once(client, monkeypatch, tmp_path):
    token = _audio_job_env(client, monkeypatch, tmp_path)
    headers = {**auth(token), "Idempotency-Key": "outbox-1"}
    first = client.post("/api/v1/assistant/media/audio", headers=headers, files={"file": ("a.m4a", b"audio", "audio/mp4")})
    again = client.post("/api/v1/assistant/media/audio", headers=headers, files={"file": ("a.m4a", b"audio", "audio/mp4")})
    assert first.status_code == 202 and again.status_code == 202
    assert again.json()["id"] == first.json()["id"] and again.json()["duplicate"] is True
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert history[0]["status"] == "done" and history[0]["client_key"] == "outbox-1"
    reply_audio = next(media for media in history[1]["media"] if media["kind"] == "audio_response")
    assert reply_audio["played"] is False
    assert client.post(f"/api/v1/assistant/media/{reply_audio['id']}/played", headers=auth(token)).status_code == 200
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert next(m for m in history[1]["media"] if m["kind"] == "audio_response")["played"] is True
    user_media = history[0]["media"][0]
    assert client.post(f"/api/v1/assistant/media/{user_media['id']}/played", headers=auth(token)).status_code == 404


def test_failed_audio_job_is_kept_and_can_be_retried(client, monkeypatch, tmp_path):
    from app.routers.mobile_extended import MediaProcessingError
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise MediaProcessingError("fora do ar")
        return "tente de novo"

    token = _audio_job_env(client, monkeypatch, tmp_path, transcribe=flaky)
    sent = client.post("/api/v1/assistant/media/audio", headers={**auth(token), "Idempotency-Key": "k-retry"},
        files={"file": ("a.m4a", b"audio", "audio/mp4")})
    assert sent.status_code == 202
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert len(history) == 1 and history[0]["status"] == "error" and history[0]["media"][0]["kind"] == "audio_user"
    retry = client.post(f"/api/v1/assistant/messages/{sent.json()['id']}/retry", headers=auth(token))
    assert retry.status_code == 202
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert [row["status"] for row in history] == ["done", "done"] and history[0]["media"][0]["transcript"] == "tente de novo"
    assert client.post("/api/v1/assistant/messages/999999/retry", headers=auth(token)).status_code == 404


def test_image_upload_is_async_idempotent_and_retry_reuses_the_cached_reading(client, monkeypatch, tmp_path):
    from app.routers.mobile_extended import MediaProcessingError
    token = _audio_job_env(client, monkeypatch, tmp_path)
    reads = []
    agent_calls = []

    def vision(*args):
        reads.append(1)
        return "Mercado, R$ 20,00, hoje"

    def failing_agent(db, user, text, **kwargs):
        agent_calls.append(text)
        if len(agent_calls) == 1:
            raise MediaProcessingError("IA fora do ar")
        kwargs["user_message"].content = kwargs.get("stored_user_content") or text
        db.add(TelegramConversationMessage(user_id=user.id, role="assistant", content="Registro?", channel="app"))
        db.commit()
        return "Registro?"

    monkeypatch.setattr("app.routers.mobile_extended.describe_receipt", vision)
    monkeypatch.setattr("app.services.telegram_agent.run_financial_agent", failing_agent)
    headers = {**auth(token), "Idempotency-Key": "img-1"}
    sent = client.post("/api/v1/assistant/media/image", headers=headers, files={"file": ("n.jpg", b"img", "image/jpeg")})
    assert sent.status_code == 202
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert len(history) == 1 and history[0]["status"] == "error" and history[0]["client_key"] == "img-1"
    assert history[0]["media"][0]["kind"] == "receipt_image" and reads == [1]
    retry = client.post(f"/api/v1/assistant/messages/{sent.json()['id']}/retry", headers=auth(token))
    assert retry.status_code == 202
    history = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert [row["status"] for row in history] == ["done", "done"] and history[0]["content"] == "Comprovante enviado."
    assert reads == [1]  # a leitura da imagem ja estava guardada: o retry nao chama o modelo de visao de novo
    again = client.post("/api/v1/assistant/media/image", headers=headers, files={"file": ("n.jpg", b"img", "image/jpeg")})
    assert again.status_code == 202 and again.json()["id"] == sent.json()["id"] and again.json()["duplicate"] is True
    assert len(agent_calls) == 2 and len(client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]) == 2


def test_pending_audio_is_hidden_from_model_history_and_expires(client, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from app.services.telegram_agent import _conversation_history
    token = _audio_job_env(client, monkeypatch, tmp_path)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="mobile-user").one()
        db.add_all([
            TelegramConversationMessage(user_id=user.id, role="user", content="visivel", channel="app"),
            TelegramConversationMessage(user_id=user.id, role="user", content="Audio enviado.", channel="app", status="pending"),
            TelegramConversationMessage(user_id=user.id, role="user", content="velho", channel="app", status="pending",
                created_at=datetime.utcnow() - timedelta(minutes=30)),
        ])
        db.commit()
        assert [item["content"] for item in _conversation_history(db, user.id)] == ["visivel"]
    items = client.get("/api/v1/assistant/history", headers=auth(token)).json()["items"]
    assert {row["content"]: row["status"] for row in items} == {"visivel": "done", "Audio enviado.": "pending", "velho": "error"}


def test_model_history_window_is_bounded_by_message_count_and_char_budget(client):
    from app.services.telegram_agent import _conversation_history
    create_account_fixture()
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="mobile-user").one()
        db.add_all([TelegramConversationMessage(user_id=user.id, role="user", content=f"m{index:03d}" + "x" * 196,
            channel="app" if index % 2 else "telegram") for index in range(30)])
        db.add(TelegramConversationMessage(user_id=user.id, role="user", content="com erro", channel="app", status="error"))
        db.commit()
        window = _conversation_history(db, user.id)  # padrao: 20 mensagens, 8000 caracteres (20 x 200 = 4000)
        assert len(window) == 20 and window[0]["content"].startswith("m010") and window[-1]["content"].startswith("m029")
        db.add_all([SystemSetting(key="chat_history_max_messages", value="5"), SystemSetting(key="chat_history_max_chars", value="500")])
        db.commit()
        assert [item["content"][:4] for item in _conversation_history(db, user.id)] == ["m028", "m029"]  # 2 x 200 <= 500 < 3 x 200
        db.query(SystemSetting).filter_by(key="chat_history_max_chars").one().value = "60000"
        db.commit()
        assert len(_conversation_history(db, user.id)) == 5  # agora so o limite de mensagens vale
        db.add(TelegramConversationMessage(user_id=user.id, role="user", content="z" * 900, channel="app"))
        db.commit()
        db.query(SystemSetting).filter_by(key="chat_history_max_chars").one().value = "500"
        db.commit()
        assert [item["content"][:1] for item in _conversation_history(db, user.id)] == ["z"]  # a mais nova sempre entra
        db.query(SystemSetting).filter_by(key="chat_history_max_messages").one().value = "lixo"
        db.query(SystemSetting).filter_by(key="chat_history_max_chars").one().value = "60000"
        db.commit()
        assert len(_conversation_history(db, user.id)) == 20  # valor invalido volta ao padrao


def test_history_window_settings_are_saved_validated_and_shown_in_admin(client):
    from app.services.chat_history import history_limits
    from test_web import seed_test
    seed_test(); client.post("/login", data={"username": "vh", "password": "123456"})
    page = client.get("/system/settings")
    assert page.status_code == 200
    assert 'name="chat_history_max_messages"' in page.text and 'name="chat_history_max_chars"' in page.text
    assert 'value="20"' in page.text and 'value="8000"' in page.text
    saved = client.post("/system/settings", data={"chat_history_max_messages": "12", "chat_history_max_chars": "4000"},
        follow_redirects=False)
    assert saved.status_code in (302, 303), saved.text
    with SessionLocal() as db:
        assert history_limits(db) == (12, 4000)
    invalid = client.post("/system/settings", data={"chat_history_max_messages": "1", "chat_history_max_chars": "4000"},
        follow_redirects=False)
    assert invalid.status_code in (302, 303, 400, 422)
    with SessionLocal() as db:
        assert history_limits(db) == (12, 4000)  # fora da faixa (2-200): nao grava


def test_mobile_text_chat_does_not_generate_audio_when_user_sends_text(client, monkeypatch):
    create_account_fixture()
    token = login(client)
    calls = []

    def fake_agent(db, user, text, channel="telegram", **kwargs):
        db.add_all([
            TelegramConversationMessage(user_id=user.id, role="user", content=text, channel=channel),
            TelegramConversationMessage(user_id=user.id, role="assistant", content="Resposta em texto.", channel=channel),
        ])
        db.commit()
        return "Resposta em texto."

    monkeypatch.setattr("app.services.telegram_agent.run_financial_agent", fake_agent)
    monkeypatch.setattr("app.routers.mobile_extended.synthesize_speech", lambda *args: calls.append(args))
    response = client.post("/api/v1/assistant/messages", headers=auth(token), json={"message": "Olá"})

    assert response.status_code == 200
    assert calls == []


def test_whisper_transcription_uses_documented_audio_field_and_segment_fallback(monkeypatch):
    from app.services import chat_media

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "", "segments": [{"text": " Paguei"}, {"text": " vinte reais."}]}

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(chat_media.httpx, "post", fake_post)
    monkeypatch.setattr(chat_media, "_model", lambda db, key: "openai/whisper-large-v3-turbo")
    monkeypatch.setattr(chat_media, "_api_key", lambda db: "test-key")

    assert chat_media.transcribe_audio(None, b"audio-bytes", "voice.m4a", "audio/mp4") == "Paguei vinte reais."
    assert captured["url"] == "https://api.deepinfra.com/v1/inference/openai/whisper-large-v3-turbo"
    assert "audio" in captured["files"]
    assert "file" not in captured["files"]


def test_kokoro_tts_uses_portuguese_voice(monkeypatch):
    from app.services import chat_media

    captured = {}

    class FakeResponse:
        headers = {"content-type": "audio/mpeg"}
        content = b"mp3-data"

        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(chat_media.httpx, "post", fake_post)
    monkeypatch.setattr(chat_media, "setting", lambda db, key: {
        "audio_generation_model": "hexgrad/Kokoro-82M",
        "deepinfra_api_key": "test-key",
    }.get(key))

    assert chat_media.synthesize_speech(None, "Olá, Vitória.") == (b"mp3-data", "audio/mpeg")
    assert captured["url"] == "https://api.deepinfra.com/v1/audio/speech"
    assert captured["json"]["voice"] == "pf_dora"
