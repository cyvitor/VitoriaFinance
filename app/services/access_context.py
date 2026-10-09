from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountRole, AreaAccessLevel, MemberRole, Person, User, UserPersonAccess, WorkspaceMember


@dataclass(frozen=True)
class UserAccessContext:
    user_id: int
    system_account_id: int | None
    workspace_ids: tuple[int, ...]
    allowed_person_ids: tuple[int, ...]
    writable_person_ids: tuple[int, ...]
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
    if is_admin:
        writable_ids = person_ids
    else:
        writable_ids = tuple(db.scalars(select(UserPersonAccess.person_id).join(
            Person, Person.id == UserPersonAccess.person_id
        ).where(
            UserPersonAccess.user_id == user.id,
            UserPersonAccess.access_level == AreaAccessLevel.write,
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
        writable_person_ids=writable_ids,
        writable_workspace_ids=writable_workspace_ids,
        default_person_id=user.default_person_id if user.default_person_id in person_ids else None,
        can_write=can_write,
    )


def scope_to_principal_area(context: UserAccessContext) -> UserAccessContext:
    """O Telegram só consulta a área principal com leitura e escrita.

    A área marcada como padrão vale quando o usuário pode lançar nela.
    Se a padrão for somente leitura, fica a única área em que ele escreve.
    Áreas somente leitura não entram nas consultas do bot.
    """
    writable = context.writable_person_ids
    principal = context.default_person_id if context.default_person_id in writable else None
    if principal is None and len(writable) == 1:
        principal = writable[0]
    if principal is None:
        return replace(context, allowed_person_ids=(), writable_person_ids=(), can_write=False)
    scoped = (principal,)
    return replace(
        context,
        allowed_person_ids=scoped,
        writable_person_ids=scoped,
        default_person_id=principal,
        can_write=context.can_write,
    )
