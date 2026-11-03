"""Conversational configuration tools shared by web chat and Telegram."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account, AccountType, Card, Category, Financing, FuelFillup, MonthlyBudget,
    MonthlyBudgetOccurrence, Person, RecurrenceRule, TransactionType, TelegramPendingAction, Vehicle,
    TelegramExpenseDraft,
)
from app.services.access_context import UserAccessContext
from app.services.monthly_projection import monthly_budget_positions


class ConfigurationError(ValueError):
    pass


def _resolve_person(db: Session, context: UserAccessContext, name: str | None, *, write: bool) -> Person | None:
    ids = context.writable_person_ids if write else context.allowed_person_ids
    if not ids:
        raise ConfigurationError("Não há área financeira com permissão para esta ação.")
    if not name:
        if context.default_person_id in ids:
            return db.get(Person, context.default_person_id)
        if len(ids) == 1:
            return db.get(Person, ids[0])
        raise ConfigurationError("Informe a área financeira. Áreas disponíveis: " + ", ".join(
            db.scalars(select(Person.name).where(Person.id.in_(ids)).order_by(Person.name)).all()))
    people = db.scalars(select(Person).where(Person.id.in_(ids), Person.is_active.is_(True))).all()
    term = name.casefold().strip()
    matches = [p for p in people if p.name.casefold() == term]
    if not matches:
        matches = [p for p in people if term in p.name.casefold() or p.name.casefold() in term]
    if len(matches) != 1:
        if not matches:
            raise ConfigurationError(f"Área '{name}' não encontrada ou sem permissão.")
        raise ConfigurationError("Área ambígua: " + ", ".join(p.name for p in matches))
    return matches[0]


def _active_action(db: Session, user_id: int) -> TelegramPendingAction | None:
    action = db.scalar(select(TelegramPendingAction).where(
        TelegramPendingAction.user_id == user_id,
        TelegramPendingAction.status.in_(["collecting", "awaiting_confirmation"]),
    ).order_by(TelegramPendingAction.id.desc()))
    if action and action.expires_at < datetime.utcnow():
        action.status = "expired"
        action.resolved_at = datetime.utcnow()
        db.commit()
        return None
    return action


def _number(value, field: str, *, minimum: Decimal = Decimal(0)) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as exc:
        raise ConfigurationError(f"{field}: informe um número válido.") from exc
    if result < minimum:
        raise ConfigurationError(f"{field}: o valor deve ser pelo menos {minimum}.")
    return result


def _save_pending(db: Session, context: UserAccessContext, entity: str, args: dict,
                  required: tuple[str, ...], labels: dict[str, str]) -> dict:
    if not context.can_write:
        raise ConfigurationError("Você não tem permissão para alterar configurações financeiras.")
    draft_id = db.scalar(select(TelegramExpenseDraft.id).where(
        TelegramExpenseDraft.user_id == context.user_id,
        TelegramExpenseDraft.status == "awaiting_confirmation"))
    if draft_id:
        raise ConfigurationError("Há um lançamento aguardando confirmação. Confirme ou cancele antes de iniciar outro cadastro.")
    action = _active_action(db, context.user_id)
    if action and action.action_type == "configuration_change":
        payload = json.loads(action.payload)
        incoming_operation = args.get("operation") or (payload.get("operation") if payload.get("entity") == entity else "create")
        if payload.get("entity") == entity and payload.get("operation") == incoming_operation:
            payload.update({k: v for k, v in args.items() if v is not None and v != ""})
        else:
            raise ConfigurationError("Há outra alteração aguardando resposta. Confirme, recuse ou cancele antes de iniciar outra.")
    else:
        if action:
            raise ConfigurationError("Há outra alteração aguardando resposta. Confirme, recuse ou cancele antes de iniciar outra.")
        payload = {"entity": entity, **{k: v for k, v in args.items() if v is not None and v != ""}}
        action = None
    payload.setdefault("entity", entity)
    payload["operation"] = args.get("operation") or payload.get("operation") or "create"
    required_fields = list(required)
    if entity in {"budget", "recurring_income", "financing", "account", "card", "category", "vehicle"} \
            and len(context.writable_person_ids) > 1 and not payload.get("area"):
        required_fields.append("area")
        labels = {**labels, "area": "área financeira"}
    missing = [labels.get(key, key) for key in required_fields if payload.get(key) in (None, "")]
    if not missing and entity in {"budget", "recurring_income", "financing", "account", "card", "category", "vehicle"}:
        _resolve_person(db, context, payload.get("area"), write=True)
    status = "collecting" if missing else "awaiting_confirmation"
    if action is None:
        action = TelegramPendingAction(
            user_id=context.user_id, action_type="configuration_change", status=status,
            payload=json.dumps(payload, ensure_ascii=False),
            expires_at=datetime.utcnow() + timedelta(hours=12),
        )
        db.add(action)
    else:
        action.payload = json.dumps(payload, ensure_ascii=False)
        action.status = status
    db.commit()
    return {"status": status, "entity": entity, "operation": payload["operation"],
            "missing_fields": missing, "preview": payload,
            "message": ("Preciso de: " + ", ".join(missing) + ".") if missing else
                       "Confira os dados e confirme antes de eu salvar."}


def manage_budget(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation not in {"create", "update", "deactivate", "release", "reactivate_month"}:
        raise ConfigurationError("Ação de orçamento inválida.")
    required = {"create": ("category", "amount", "start_month"),
                "update": ("category",), "deactivate": ("category",),
                "release": ("category", "reference_month"),
                "reactivate_month": ("category", "reference_month")}[operation]
    return _save_pending(db, context, "budget", args, required, {
        "category": "categoria de despesa", "amount": "valor mensal", "start_month": "mês inicial (AAAA-MM)",
        "reference_month": "mês de referência (AAAA-MM)",
    })


def manage_vehicle(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        items = db.scalars(select(Vehicle).where(Vehicle.workspace_id.in_(context.workspace_ids)).order_by(
            Vehicle.is_active.desc(), Vehicle.name)).all()
        fillups = db.scalars(select(FuelFillup).where(FuelFillup.workspace_id.in_(context.workspace_ids)).order_by(
            FuelFillup.fillup_date.desc(), FuelFillup.id.desc()).limit(20)).all()
        return {"vehicles": [{"name": v.name, "year": v.year, "fuel_type": v.fuel_type,
                              "initial_odometer_km": str(v.initial_odometer_km), "active": v.is_active}
                             for v in items],
                "recent_fillups": [{"vehicle": f.vehicle.name, "date": f.fillup_date.isoformat(),
                                    "odometer_km": str(f.odometer_km), "liters": str(f.liters),
                                    "price_per_liter": str(f.price_per_liter), "full_tank": f.is_full_tank}
                                   for f in fillups]}
    if operation not in {"create", "update", "deactivate"}:
        raise ConfigurationError("Ação de veículo inválida.")
    required = {"create": ("name",), "update": ("target",), "deactivate": ("target",)}[operation]
    return _save_pending(db, context, "vehicle", args, required, {"name": "nome do veículo", "target": "veículo"})


def query_fillups(db: Session, context: UserAccessContext, args: dict) -> dict:
    query = select(FuelFillup).where(FuelFillup.workspace_id.in_(context.workspace_ids)).order_by(
        FuelFillup.fillup_date.desc(), FuelFillup.id.desc())
    if args.get("vehicle"):
        term = str(args["vehicle"]).casefold().strip()
        vehicles = db.scalars(select(Vehicle).where(Vehicle.workspace_id.in_(context.workspace_ids))).all()
        matches = [v for v in vehicles if v.name.casefold() == term or term in v.name.casefold()]
        if len(matches) != 1:
            raise ConfigurationError("Veículo não encontrado ou ambíguo. Veículos: " + ", ".join(v.name for v in matches or vehicles))
        query = query.where(FuelFillup.vehicle_id == matches[0].id)
    if args.get("start_date"):
        try: query = query.where(FuelFillup.fillup_date >= date.fromisoformat(str(args["start_date"])))
        except ValueError as exc: raise ConfigurationError("Data inicial inválida; use AAAA-MM-DD.") from exc
    if args.get("end_date"):
        try: query = query.where(FuelFillup.fillup_date <= date.fromisoformat(str(args["end_date"])))
        except ValueError as exc: raise ConfigurationError("Data final inválida; use AAAA-MM-DD.") from exc
    items = db.scalars(query.limit(100)).all()
    return {"fillups": [{"vehicle": f.vehicle.name, "date": f.fillup_date.isoformat(),
                          "odometer_km": str(f.odometer_km), "liters": str(f.liters),
                          "price_per_liter": str(f.price_per_liter),
                          "total": f"{Decimal(f.liters) * Decimal(f.price_per_liter):.2f}",
                          "full_tank": f.is_full_tank, "station": f.station}
                         for f in items], "count": len(items)}


def manage_recurring_income(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        query = select(RecurrenceRule).where(
            RecurrenceRule.workspace_id.in_(context.workspace_ids),
            RecurrenceRule.person_id.in_(context.allowed_person_ids),
            RecurrenceRule.transaction_type == TransactionType.income,
        ).order_by(RecurrenceRule.is_active.desc(), RecurrenceRule.description)
        return {"recurring_income": [{"description": r.description, "amount": str(r.amount or 0),
                                      "area": r.person.name if r.person else None,
                                      "account": r.account.name if r.account else None,
                                      "due_day": (r.days_of_month or "").split(",")[0] or None,
                                      "active": r.is_active} for r in db.scalars(query).all()]}
    if operation not in {"create", "update", "deactivate"}:
        raise ConfigurationError("Ação de receita recorrente inválida.")
    required = {"create": ("description", "amount", "account", "due_day"),
                "update": ("target",), "deactivate": ("target",)}[operation]
    return _save_pending(db, context, "recurring_income", args, required, {
        "description": "descrição", "amount": "valor", "account": "conta de destino",
        "due_day": "dia de recebimento", "target": "receita recorrente",
    })


def manage_fixed_bill(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    query = select(RecurrenceRule).where(
        RecurrenceRule.workspace_id.in_(context.workspace_ids),
        RecurrenceRule.person_id.in_(context.allowed_person_ids),
        RecurrenceRule.transaction_type == TransactionType.expense,
        RecurrenceRule.description.is_not(None),
        RecurrenceRule.is_active.is_(True),
        ~RecurrenceRule.id.in_(select(Financing.recurrence_rule_id).where(Financing.recurrence_rule_id.is_not(None))),
    )
    if operation == "list":
        return {"fixed_bills": [{"description": r.description, "amount": str(r.amount or 0),
                                 "area": r.person.name if r.person else None,
                                 "account": r.account.name if r.account else None,
                                 "category": r.category.name if r.category else None,
                                 "due_day": r.days_of_month, "active": r.is_active}
                                for r in db.scalars(query.order_by(RecurrenceRule.description)).all()]}
    if operation not in {"create", "update", "deactivate"}:
        raise ConfigurationError("Ação de conta fixa inválida.")
    required = {"create": ("description", "amount", "due_day"),
                "update": ("target",), "deactivate": ("target",)}[operation]
    return _save_pending(db, context, "fixed_bill", args, required, {
        "description": "descrição da conta", "amount": "valor mensal", "due_day": "dia de vencimento",
        "target": "conta fixa",
    })


def manage_financing(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        items = db.scalars(select(Financing).where(
            Financing.workspace_id.in_(context.workspace_ids),
            Financing.person_id.in_(context.allowed_person_ids)).order_by(Financing.status, Financing.description)).all()
        return {"financings": [{"description": x.description, "institution": x.institution,
                                "area": x.person.name, "status": x.status,
                                "installment_amount": str(x.installment_amount),
                                "paid_installments": x.paid_installments,
                                "total_installments": x.total_installments,
                                "outstanding_balance": str(x.outstanding_balance) if x.outstanding_balance is not None else None}
                               for x in items]}
    if operation not in {"create", "update", "deactivate"}:
        raise ConfigurationError("Ação de financiamento inválida.")
    required = ("description", "total_installments", "installment_amount") if operation == "create" else ("target",)
    return _save_pending(db, context, "financing", args, required, {
        "description": "nome do contrato", "total_installments": "quantidade total de parcelas",
        "installment_amount": "valor mensal da parcela", "target": "contrato",
    })


def manage_account(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        items = db.scalars(select(Account).where(Account.workspace_id.in_(context.workspace_ids),
            Account.person_id.in_(context.allowed_person_ids), Account.is_active.is_(True)).order_by(Account.name)).all()
        return {"accounts": [{"name": a.name, "bank": a.bank_name, "type": a.account_type.value,
                              "area": a.person.name if a.person else None,
                              "initial_balance": str(a.initial_balance)} for a in items]}
    if operation != "create": raise ConfigurationError("Ação de conta inválida.")
    return _save_pending(db, context, "account", args, ("name",), {"name": "nome da conta"})


def manage_card(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        items = db.scalars(select(Card).join(Account, Account.id == Card.account_id).where(
            Card.workspace_id.in_(context.workspace_ids), Account.person_id.in_(context.allowed_person_ids),
            Card.is_active.is_(True)).order_by(Card.name)).all()
        return {"cards": [{"name": c.name, "account": c.account.name if c.account else None,
                           "area": c.account.person.name if c.account and c.account.person else None,
                           "limit": str(c.credit_limit), "closing_day": c.closing_day, "due_day": c.due_day}
                          for c in items]}
    if operation != "create": raise ConfigurationError("Ação de cartão inválida.")
    return _save_pending(db, context, "card", args, ("name", "account"),
                         {"name": "nome do cartão", "account": "conta vinculada"})


def manage_category(db: Session, context: UserAccessContext, args: dict) -> dict:
    operation = str(args.get("operation") or "create")
    if operation == "list":
        items = db.scalars(select(Category).where(Category.workspace_id.in_(context.workspace_ids)).order_by(
            Category.kind, Category.parent_name, Category.name)).all()
        return {"categories": [{"name": c.name, "parent": c.parent_name, "type": c.kind.value} for c in items]}
    if operation != "create": raise ConfigurationError("Ação de categoria inválida.")
    return _save_pending(db, context, "category", args, ("name",), {"name": "nome da categoria"})


def confirm_configuration_change(db: Session, context: UserAccessContext, action: TelegramPendingAction) -> dict:
    payload = json.loads(action.payload)
    entity, operation = payload.get("entity"), payload.get("operation")
    person = _resolve_person(db, context, payload.get("area"), write=True) if entity in {
        "budget", "recurring_income", "financing", "account", "card", "category", "vehicle"} else None
    workspace_id = person.workspace_id if person else (context.workspace_ids[0] if context.workspace_ids else None)
    if not workspace_id:
        raise ConfigurationError("Workspace não encontrado.")

    if entity == "budget":
        category_name = str(payload.get("category") or "").casefold().strip()
        categories = db.scalars(select(Category).where(
            Category.workspace_id == workspace_id, Category.kind == TransactionType.expense)).all()
        matches = [c for c in categories if c.name.casefold() == category_name or
                   f"{c.parent_name or ''} {c.name}".strip().casefold() == category_name]
        if len(matches) != 1:
            raise ConfigurationError("Categoria não encontrada ou ambígua. Use uma categoria de despesa existente.")
        budget = db.scalar(select(MonthlyBudget).where(
            MonthlyBudget.workspace_id == workspace_id, MonthlyBudget.person_id == person.id,
            MonthlyBudget.category_id == matches[0].id))
        if operation in ("create", "update"):
            amount = _number(payload.get("amount"), "Valor", minimum=Decimal("0.01")) if payload.get("amount") is not None else None
            year, month = None, None
            if payload.get("start_month") or operation == "create":
                start_month = str(payload.get("start_month") or date.today().strftime("%Y-%m"))
                try:
                    year, month = map(int, start_month.split("-", 1))
                    if not 2000 <= year <= 2100 or not 1 <= month <= 12: raise ValueError
                except ValueError as exc: raise ConfigurationError("Mês inicial inválido; use AAAA-MM.") from exc
            if budget is None:
                if amount is None: raise ConfigurationError("Informe o valor mensal do orçamento.")
                budget = MonthlyBudget(workspace_id=workspace_id, person_id=person.id,
                    category_id=matches[0].id, amount=amount, start_year=year, start_month=month,
                    include_in_projection=bool(payload.get("include_in_projection", True)),
                    created_by_id=context.user_id, is_active=True)
                db.add(budget)
            else:
                if amount is not None: budget.amount = amount
                if year is not None: budget.start_year, budget.start_month = year, month
                if payload.get("include_in_projection") is not None:
                    budget.include_in_projection = bool(payload["include_in_projection"])
                budget.is_active = True
        elif operation == "deactivate":
            if not budget: raise ConfigurationError("Não encontrei orçamento ativo para essa categoria e área.")
            budget.is_active = False
        elif operation in ("release", "reactivate_month"):
            if not budget: raise ConfigurationError("Não encontrei orçamento para essa categoria e área.")
            try:
                year, month = map(int, str(payload["reference_month"]).split("-", 1))
                if not 2000 <= year <= 2100 or not 1 <= month <= 12: raise ValueError
            except ValueError as exc: raise ConfigurationError("Mês inválido; use AAAA-MM.") from exc
            occurrence = db.scalar(select(MonthlyBudgetOccurrence).where(
                MonthlyBudgetOccurrence.monthly_budget_id == budget.id,
                MonthlyBudgetOccurrence.year == year, MonthlyBudgetOccurrence.month == month))
            if operation == "release":
                if occurrence and occurrence.status == "released": raise ConfigurationError("Esse valor já foi liberado.")
                positions = monthly_budget_positions(db, workspace_ids=(workspace_id,), person_ids=(person.id,),
                    year=year, month=month, category_id=budget.category_id)
                remaining = max(positions[0].remaining, Decimal(0)) if positions else Decimal(0)
                if not occurrence:
                    occurrence = MonthlyBudgetOccurrence(monthly_budget_id=budget.id, year=year, month=month,
                        status="released", released_amount=remaining, released_by_id=context.user_id,
                        released_at=datetime.utcnow())
                    db.add(occurrence)
                else:
                    occurrence.status, occurrence.released_amount = "released", remaining
                    occurrence.released_by_id, occurrence.released_at = context.user_id, datetime.utcnow()
            else:
                if not occurrence or occurrence.status != "released": raise ConfigurationError("Esse orçamento não está liberado neste mês.")
                db.delete(occurrence)
        db.flush()
        result = {"entity": "budget", "operation": operation, "category": matches[0].name,
                  "amount": str(budget.amount), "area": person.name, "competence": payload.get("reference_month")}
    elif entity == "vehicle":
        query = select(Vehicle).where(Vehicle.workspace_id.in_(context.workspace_ids))
        target = str(payload.get("target") or payload.get("name") or "").casefold().strip()
        vehicles = db.scalars(query).all()
        vehicle = next((v for v in vehicles if v.name.casefold() == target), None)
        if operation == "create":
            if any(v.name.casefold() == str(payload["name"]).casefold() for v in vehicles):
                raise ConfigurationError("Já existe um veículo com esse nome.")
            year = int(payload["year"]) if payload.get("year") not in (None, "") else None
            if year and not 1900 <= year <= 2200: raise ConfigurationError("Ano do veículo inválido.")
            odometer = _number(payload.get("initial_odometer_km", 0), "Quilometragem")
            vehicle = Vehicle(workspace_id=workspace_id, name=str(payload["name"]).strip(), year=year,
                initial_odometer_km=odometer or Decimal(0), fuel_type=payload.get("fuel_type"))
            db.add(vehicle)
        elif operation in ("update", "deactivate"):
            if not vehicle: raise ConfigurationError("Não encontrei esse veículo.")
            if operation == "deactivate": vehicle.is_active = False
            else:
                if not any(payload.get(field) not in (None, "") for field in ("name", "year", "fuel_type", "initial_odometer_km")):
                    raise ConfigurationError("Informe o dado do veículo que deseja alterar.")
                new_name = str(payload.get("name") or vehicle.name).strip()
                if any(v.id != vehicle.id and v.name.casefold() == new_name.casefold() for v in vehicles):
                    raise ConfigurationError("Já existe outro veículo com esse nome.")
                vehicle.name = new_name
                if payload.get("year") not in (None, ""):
                    year = int(payload["year"])
                    if not 1900 <= year <= 2200: raise ConfigurationError("Ano do veículo inválido.")
                    vehicle.year = year
                if payload.get("fuel_type") is not None: vehicle.fuel_type = payload["fuel_type"]
                if payload.get("initial_odometer_km") is not None:
                    odometer = _number(payload["initial_odometer_km"], "Quilometragem")
                    latest = db.scalar(select(FuelFillup.odometer_km).where(FuelFillup.vehicle_id == vehicle.id).order_by(FuelFillup.odometer_km.desc()))
                    if latest is not None and odometer > Decimal(latest):
                        raise ConfigurationError("Quilometragem inicial não pode superar um abastecimento já registrado.")
                    vehicle.initial_odometer_km = odometer
        result = {"entity": "vehicle", "operation": operation, "name": vehicle.name,
                  "year": vehicle.year, "fuel_type": vehicle.fuel_type}
    elif entity == "recurring_income":
        description = str(payload.get("description") or payload.get("target") or "").strip()
        rules = db.scalars(select(RecurrenceRule).where(
            RecurrenceRule.workspace_id == workspace_id, RecurrenceRule.person_id == person.id,
            RecurrenceRule.transaction_type == TransactionType.income)).all()
        rule = next((r for r in rules if (r.description or "").casefold() == description.casefold()), None)
        if operation == "create":
            account_name = str(payload.get("account") or "").casefold().strip()
            accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
                Account.person_id == person.id, Account.is_active.is_(True))).all()
            account = next((a for a in accounts if a.name.casefold() == account_name), None)
            if not account: raise ConfigurationError("Conta de destino não encontrada nessa área.")
            amount = _number(payload.get("amount"), "Valor", minimum=Decimal("0.01"))
            due_day = int(payload["due_day"])
            if not 1 <= due_day <= 31: raise ConfigurationError("Dia de recebimento deve estar entre 1 e 31.")
            category = None
            if payload.get("category"):
                category_name = str(payload["category"]).casefold().strip()
                cats = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                    Category.kind == TransactionType.income)).all()
                choices = [c for c in cats if c.name.casefold() == category_name]
                if len(choices) != 1: raise ConfigurationError("Categoria de receita não encontrada ou ambígua.")
                category = choices[0]
            rule = RecurrenceRule(workspace_id=workspace_id, frequency="monthly",
                transaction_type=TransactionType.income, days_of_month=str(due_day),
                start_date=date.today().replace(day=1), description=description,
                amount=amount, account_id=account.id, person_id=person.id,
                category_id=category.id if category else None, created_by_id=context.user_id, is_active=True)
            db.add(rule)
        elif operation in ("update", "deactivate"):
            if not rule: raise ConfigurationError("Não encontrei essa receita recorrente na área informada.")
            if operation == "deactivate": rule.is_active = False
            else:
                if payload.get("new_description"): rule.description = str(payload["new_description"]).strip()
                if payload.get("amount") is not None: rule.amount = _number(payload["amount"], "Valor", minimum=Decimal("0.01"))
                if payload.get("due_day") is not None:
                    day = int(payload["due_day"])
                    if not 1 <= day <= 31: raise ConfigurationError("Dia deve estar entre 1 e 31.")
                    rule.days_of_month = str(day)
                if payload.get("account"):
                    accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
                        Account.person_id == person.id, Account.is_active.is_(True))).all()
                    account = next((a for a in accounts if a.name.casefold() == str(payload["account"]).casefold()), None)
                    if not account: raise ConfigurationError("Conta de destino não encontrada nessa área.")
                    rule.account_id = account.id
                if payload.get("category"):
                    cats = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                        Category.kind == TransactionType.income)).all()
                    category = next((c for c in cats if c.name.casefold() == str(payload["category"]).casefold()), None)
                    if not category: raise ConfigurationError("Categoria de receita não encontrada.")
                    rule.category_id = category.id
                if "notes" in payload: rule.notes = str(payload["notes"]).strip() or None
        result = {"entity": "recurring_income", "operation": operation,
                  "description": rule.description, "amount": str(rule.amount), "area": person.name}
    elif entity == "fixed_bill":
        target = str(payload.get("target") or payload.get("description") or "").casefold().strip()
        rules = db.scalars(select(RecurrenceRule).where(
            RecurrenceRule.workspace_id == workspace_id, RecurrenceRule.person_id == person.id,
            RecurrenceRule.transaction_type == TransactionType.expense,
            RecurrenceRule.is_active.is_(True),
            ~RecurrenceRule.id.in_(select(Financing.recurrence_rule_id).where(Financing.recurrence_rule_id.is_not(None))),
        )).all()
        rule = next((r for r in rules if (r.description or "").casefold() == target), None)
        if operation == "create":
            description = str(payload["description"]).strip()
            amount = _number(payload["amount"], "Valor mensal", minimum=Decimal("0.01"))
            due_day = int(payload["due_day"])
            if not description or not 1 <= due_day <= 31: raise ConfigurationError("Informe descrição e vencimento entre 1 e 31.")
            category = None
            if payload.get("category"):
                term = str(payload["category"]).casefold().strip()
                choices = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                    Category.kind == TransactionType.expense)).all()
                matches = [c for c in choices if c.name.casefold() == term]
                if len(matches) != 1: raise ConfigurationError("Categoria de despesa não encontrada ou ambígua.")
                category = matches[0]
            account = None
            if payload.get("account"):
                accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
                    Account.person_id == person.id, Account.is_active.is_(True))).all()
                account = next((a for a in accounts if a.name.casefold() == str(payload["account"]).casefold().strip()), None)
                if not account: raise ConfigurationError("Conta não encontrada nessa área.")
            rule = RecurrenceRule(workspace_id=workspace_id, frequency="monthly",
                transaction_type=TransactionType.expense, days_of_month=str(due_day),
                description=description, amount=amount, account_id=account.id if account else None,
                person_id=person.id, category_id=category.id if category else None,
                notes=payload.get("notes"), created_by_id=context.user_id, is_active=True)
            db.add(rule)
        elif not rule:
            raise ConfigurationError("Conta fixa ativa não encontrada nessa área.")
        elif operation == "deactivate":
            rule.is_active = False
        else:
            if payload.get("description"): rule.description = str(payload["description"]).strip()
            if payload.get("amount") is not None: rule.amount = _number(payload["amount"], "Valor mensal", minimum=Decimal("0.01"))
            if payload.get("due_day") is not None:
                due_day = int(payload["due_day"])
                if not 1 <= due_day <= 31: raise ConfigurationError("Vencimento deve ser dia entre 1 e 31.")
                rule.days_of_month = str(due_day)
            if payload.get("category"):
                term = str(payload["category"]).casefold().strip()
                choices = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                    Category.kind == TransactionType.expense)).all()
                matches = [c for c in choices if c.name.casefold() == term]
                if len(matches) != 1: raise ConfigurationError("Categoria de despesa não encontrada ou ambígua.")
                rule.category_id = matches[0].id
        result = {"entity": "fixed_bill", "operation": operation, "description": rule.description,
                  "amount": str(rule.amount), "area": person.name, "due_day": rule.days_of_month}
    elif entity == "financing":
        if operation == "create":
            total = int(payload["total_installments"])
            paid = int(payload.get("paid_installments") or 0)
            amount = _number(payload["installment_amount"], "Parcela", minimum=Decimal("0.01"))
            contract_type = str(payload.get("contract_type") or "financing")
            if contract_type not in {"financing", "negotiated_debt", "personal_loan"}:
                contract_type = "financing"
            if total < 1 or paid < 0 or paid >= total:
                raise ConfigurationError("Confira as quantidades de parcelas pagas e totais.")
            due_day = int(payload["due_day"]) if payload.get("due_day") not in (None, "") else None
            if due_day is not None and not 1 <= due_day <= 31:
                raise ConfigurationError("Vencimento deve estar entre 1 e 31.")
            account = None
            if payload.get("account"):
                accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
                    Account.person_id == person.id, Account.is_active.is_(True))).all()
                account = next((a for a in accounts if a.name.casefold() == str(payload["account"]).casefold()), None)
                if not account: raise ConfigurationError("Conta não encontrada nessa área.")
            category = None
            if payload.get("category"):
                categories = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                    Category.kind == TransactionType.expense)).all()
                category = next((c for c in categories if c.name.casefold() == str(payload["category"]).casefold()), None)
                if not category: raise ConfigurationError("Categoria de despesa não encontrada.")
            rule = RecurrenceRule(workspace_id=workspace_id, frequency="monthly", transaction_type=TransactionType.expense,
                start_date=date.today().replace(day=1), days_of_month=str(due_day) if due_day else None,
                description=f"{str(payload['description']).strip()} - parcela {paid + 1}/{total}",
                amount=amount, account_id=account.id if account else None, person_id=person.id,
                category_id=category.id if category else None,
                notes=payload.get("notes"), created_by_id=context.user_id, is_active=True)
            db.add(rule)
            db.flush()
            financing = Financing(workspace_id=workspace_id, person_id=person.id, recurrence_rule_id=rule.id,
                description=str(payload["description"]).strip(), contract_type=contract_type,
                paid_installments=paid, total_installments=total, installment_amount=amount,
                financed_amount=_number(payload.get("financed_amount"), "Valor financiado"),
                outstanding_balance=_number(payload.get("outstanding_balance"), "Saldo devedor"),
                institution=payload.get("institution"), due_day=due_day, notes=payload.get("notes"), status="active")
            db.add(financing)
        elif operation == "deactivate":
            name = str(payload.get("target") or "").casefold().strip()
            candidates = db.scalars(select(Financing).where(Financing.workspace_id == workspace_id,
                Financing.person_id == person.id, Financing.status == "active")).all()
            financing = next((f for f in candidates if f.description.casefold() == name), None)
            if not financing: raise ConfigurationError("Contrato ativo não encontrado nessa área.")
            financing.status = "cancelled"
            if financing.recurrence_rule: financing.recurrence_rule.is_active = False
        else:
            name = str(payload.get("target") or "").casefold().strip()
            candidates = db.scalars(select(Financing).where(Financing.workspace_id == workspace_id,
                Financing.person_id == person.id, Financing.status == "active")).all()
            financing = next((f for f in candidates if f.description.casefold() == name), None)
            if not financing: raise ConfigurationError("Contrato ativo não encontrado nessa área.")
            if not any(payload.get(k) is not None for k in ("new_description", "institution", "financed_amount",
                    "nominal_interest_rate", "due_day", "start_date", "notes", "account", "category")):
                raise ConfigurationError("Informe o dado do contrato que deseja alterar.")
            if payload.get("new_description"):
                financing.description = str(payload["new_description"]).strip()
                if financing.recurrence_rule:
                    financing.recurrence_rule.description = f"{financing.description} - parcela {financing.paid_installments + 1}/{financing.total_installments}"
            for field in ("institution", "notes"):
                if field in payload: setattr(financing, field, str(payload[field]).strip() or None)
            if "financed_amount" in payload: financing.financed_amount = _number(payload["financed_amount"], "Valor financiado")
            if "nominal_interest_rate" in payload: financing.nominal_interest_rate = _number(payload["nominal_interest_rate"], "Taxa")
            if "due_day" in payload:
                due_day = int(payload["due_day"]) if payload["due_day"] not in (None, "") else None
                if due_day is not None and not 1 <= due_day <= 31: raise ConfigurationError("Vencimento deve estar entre 1 e 31.")
                financing.due_day = due_day
                if financing.recurrence_rule:
                    financing.recurrence_rule.days_of_month = str(due_day) if due_day else None
            if "start_date" in payload: financing.start_date = date.fromisoformat(payload["start_date"]) if payload["start_date"] else None
            rule = financing.recurrence_rule
            if rule and payload.get("account"):
                accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
                    Account.person_id == person.id, Account.is_active.is_(True))).all()
                account = next((a for a in accounts if a.name.casefold() == str(payload["account"]).casefold()), None)
                if not account: raise ConfigurationError("Conta não encontrada nessa área.")
                rule.account_id = account.id
            if rule and payload.get("category"):
                cats = db.scalars(select(Category).where(Category.workspace_id == workspace_id,
                    Category.kind == TransactionType.expense)).all()
                category = next((c for c in cats if c.name.casefold() == str(payload["category"]).casefold()), None)
                if not category: raise ConfigurationError("Categoria de despesa não encontrada.")
                rule.category_id = category.id
            if rule and ("notes" in payload): rule.notes = financing.notes
        result = {"entity": "financing", "operation": operation, "description": financing.description,
                  "area": person.name, "installment_amount": str(financing.installment_amount),
                  "total_installments": financing.total_installments}
    elif entity == "account":
        name = str(payload["name"]).strip()
        existing = db.scalar(select(Account.id).where(Account.workspace_id == workspace_id,
            Account.person_id == person.id, Account.is_active.is_(True), Account.name == name))
        if existing: raise ConfigurationError("Já existe uma conta ativa com esse nome nessa área.")
        try: account_type = AccountType(str(payload.get("account_type") or "checking"))
        except ValueError as exc: raise ConfigurationError("Tipo de conta inválido.") from exc
        account = Account(workspace_id=workspace_id, person_id=person.id, name=name,
            bank_name=str(payload.get("bank") or ""), account_type=account_type,
            initial_balance=_number(payload.get("initial_balance", 0), "Saldo inicial") or Decimal(0),
            color=str(payload.get("color") or "#6c5ce7"))
        db.add(account)
        result = {"entity": "account", "name": name, "area": person.name, "bank": account.bank_name}
    elif entity == "card":
        name = str(payload["name"]).strip()
        accounts = db.scalars(select(Account).where(Account.workspace_id == workspace_id,
            Account.person_id == person.id, Account.is_active.is_(True))).all()
        account = next((a for a in accounts if a.name.casefold() == str(payload["account"]).casefold().strip()), None)
        if not account: raise ConfigurationError("Conta vinculada não encontrada nessa área.")
        if db.scalar(select(Card.id).where(Card.workspace_id == workspace_id, Card.name == name, Card.is_active.is_(True))):
            raise ConfigurationError("Já existe cartão ativo com esse nome.")
        closing, due = int(payload.get("closing_day") or 1), int(payload.get("due_day") or 10)
        if not 1 <= closing <= 31 or not 1 <= due <= 31: raise ConfigurationError("Fechamento e vencimento devem ser dias entre 1 e 31.")
        card = Card(workspace_id=workspace_id, account_id=account.id, name=name,
            brand=str(payload.get("brand") or ""), credit_limit=_number(payload.get("credit_limit", 0), "Limite") or Decimal(0),
            closing_day=closing, due_day=due, color=str(payload.get("color") or "#1e293b"))
        db.add(card)
        result = {"entity": "card", "name": name, "account": account.name, "area": person.name,
                  "closing_day": closing, "due_day": due}
    elif entity == "category":
        name = str(payload["name"]).strip()
        try: kind = TransactionType(str(payload.get("kind") or "expense"))
        except ValueError as exc: raise ConfigurationError("Tipo deve ser receita ou despesa.") from exc
        parent = str(payload.get("parent_name") or "").strip() or None
        existing = db.scalar(select(Category.id).where(Category.workspace_id == workspace_id,
            Category.name == name, Category.kind == kind))
        if existing: raise ConfigurationError("Já existe categoria com esse nome e tipo.")
        category = Category(workspace_id=workspace_id, name=name, kind=kind, parent_name=parent,
                            color=str(payload.get("color") or "#64748b"))
        db.add(category)
        result = {"entity": "category", "name": name, "type": kind.value, "parent": parent}
    else:
        raise ConfigurationError("Tipo de cadastro não suportado.")
    action.status, action.resolved_at = "confirmed", datetime.utcnow()
    db.commit()
    return {"saved": True, **result}
