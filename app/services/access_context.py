from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountRole, MemberRole, Person, User, UserPersonAccess, WorkspaceMember


@dataclass(frozen=True)
class UserAccessContext:
    user_id: int
    system_account_id: int | None
    workspace_ids: tuple[int, ...]
    allowed_person_ids: tuple[int, ...]
    writable_workspace_ids: tuple[int, ...]
    default_person_id: int | None
    can_write: bool


def build_user_access_context(db: Session, user: User) -> UserAccessContext:
    memberships = db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
    workspace_ids = tuple(item.workspace_id for item in memberships)
    is_admin = user.is_super_admin or user.account_role == AccountRole.admin
    if is_admin:
        person_ids = tuple(db.scalars(select(Person.id).where(
            Person.workspace_id.in_(workspace_ids), Person.is_active.is_(True)
        )).all()) if workspace_ids else ()
    else:
        person_ids = tuple(db.scalars(select(UserPersonAccess.person_id).join(
            Person, Person.id == UserPersonAccess.person_id
        ).where(
            UserPersonAccess.user_id == user.id,
            Person.workspace_id.in_(workspace_ids),
            Person.is_active.is_(True),
        )).all()) if workspace_ids else ()
    writable_workspace_ids = workspace_ids if is_admin else tuple(
        item.workspace_id for item in memberships if item.role in (MemberRole.admin, MemberRole.editor)
    )
    can_write = bool(writable_workspace_ids)
    return UserAccessContext(
        user_id=user.id,
        system_account_id=user.system_account_id,
        workspace_ids=workspace_ids,
        allowed_person_ids=person_ids,
        writable_workspace_ids=writable_workspace_ids,
        default_person_id=user.default_person_id if user.default_person_id in person_ids else None,
        can_write=can_write,
    )
