from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AccountRole, Category, MemberRole, MonthlyBudget, Person, PersonType,
    SystemAccount, TransactionType, User, UserPersonAccess, Vehicle, Workspace,
    WorkspaceMember,
)
from app.security import hash_password
from app.services.access_context import build_user_access_context, scope_to_principal_area
from app.services.financial_tools import ToolError, execute_tool


def _user_with_multiple_areas(db):
    system_account = SystemAccount(name="Chat config test")
    db.add(system_account)
    db.flush()
    user = User(system_account_id=system_account.id, username="chat-config-test", full_name="Teste",
                password_hash=hash_password("test-password"), account_role=AccountRole.member)
    workspace = Workspace(system_account_id=system_account.id, name="Casa")
    other_workspace = Workspace(system_account_id=system_account.id, name="Trabalho")
    db.add_all([user, workspace, other_workspace])
    db.flush()
    db.add_all([
        WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=MemberRole.editor),
        WorkspaceMember(user_id=user.id, workspace_id=other_workspace.id, role=MemberRole.editor),
    ])
    primary = Person(workspace_id=workspace.id, name="Pessoal", person_type=PersonType.personal)
    secondary = Person(workspace_id=other_workspace.id, name="Trabalho", person_type=PersonType.shared)
    db.add_all([primary, secondary])
    db.flush()
    user.default_person_id = primary.id
    db.add_all([
        UserPersonAccess(user_id=user.id, person_id=primary.id),
        UserPersonAccess(user_id=user.id, person_id=secondary.id),
        Category(workspace_id=workspace.id, name="Mercado", kind=TransactionType.expense),
        Category(workspace_id=other_workspace.id, name="Viagens", kind=TransactionType.expense),
        Vehicle(workspace_id=other_workspace.id, name="Veículo Trabalho"),
    ])
    db.commit()
    db.refresh(user)
    return user, primary, secondary


def test_chat_crud_requires_confirmation_and_uses_only_default_area():
    with SessionLocal() as db:
        user, primary, secondary = _user_with_multiple_areas(db)
        full_context = build_user_access_context(db, user)
        context = scope_to_principal_area(full_context)
        assert len(full_context.allowed_person_ids) == 2
        assert context.allowed_person_ids == (primary.id,)
        assert context.workspace_ids == (primary.workspace_id,)

        budget = execute_tool(db, context, "gerenciar_orcamento", {
            "operation": "create", "category": "Mercado", "amount": 900,
            "start_month": "2026-11",
        })
        assert budget["status"] == "awaiting_confirmation"
        assert db.scalar(select(MonthlyBudget)) is None

        saved = execute_tool(db, context, "confirmar_acao_pendente", {})
        assert saved["saved"] is True
        assert saved["area"] == "Pessoal"
        assert db.scalar(select(MonthlyBudget).where(MonthlyBudget.person_id == primary.id)).amount == Decimal("900.00")

        vehicles = execute_tool(db, context, "consultar_veiculos", {})
        assert [item["name"] for item in vehicles["vehicles"]] == []
        new_vehicle = execute_tool(db, context, "gerenciar_veiculo", {
            "operation": "create", "name": "Carro Pessoal", "year": 2022,
            "initial_odometer_km": 35000,
        })
        assert new_vehicle["status"] == "awaiting_confirmation"
        vehicle = execute_tool(db, context, "confirmar_acao_pendente", {})
        assert vehicle["saved"] is True
        assert db.scalar(select(Vehicle).where(Vehicle.name == "Carro Pessoal")).workspace_id == primary.workspace_id

        with pytest.raises(ToolError):
            execute_tool(db, context, "gerenciar_orcamento", {
                "operation": "create", "category": "Viagens", "area": "Trabalho",
                "amount": 500, "start_month": "2026-11",
            })
