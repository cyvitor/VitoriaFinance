from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AccountRole, User, UserPersonAccess, WorkspaceMember


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active or (user.system_account and not user.system_account.is_active):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def current_workspace_id(request: Request, user: User, db: Session) -> int:
    workspace_id = request.session.get("workspace_id")
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    if not membership:
        membership = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
        if not membership:
            raise HTTPException(status_code=403, detail="Usuário sem workspace")
        request.session["workspace_id"] = membership.workspace_id
    return membership.workspace_id


def allowed_person_ids(user: User, db: Session) -> list[int] | None:
    """Administradores veem tudo; membros veem apenas áreas explicitamente liberadas."""
    if user.is_super_admin or user.account_role == AccountRole.admin:
        return None
    return list(db.scalars(select(UserPersonAccess.person_id).where(UserPersonAccess.user_id == user.id)).all())
