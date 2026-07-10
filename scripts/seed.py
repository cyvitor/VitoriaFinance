from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AccountRole, Category, SystemAccount, TransactionType,
    User, Workspace, WorkspaceMember, MemberRole,
)
from app.security import hash_password

CATEGORIES = {
    "Moradia": ["Aluguel", "Condomínio", "Energia", "Água", "Internet", "IPTU", "Reformas", "Móveis", "Decoração"],
    "Alimentação": ["Mercado", "Restaurante", "Delivery", "Padaria", "Café"],
    "Transporte": ["Combustível", "Uber", "Táxi", "Ônibus", "Pedágio", "Seguro", "IPVA", "Oficina", "Estacionamento"],
    "Saúde": ["Plano de saúde", "Consulta", "Exames", "Medicamentos", "Odontologia", "Psicólogo", "Academia"],
    "Educação": ["Cursos", "Faculdade", "Livros", "Idiomas"], "Trabalho": ["Equipamentos", "Software", "Ferramentas"],
    "Lazer": ["Cinema", "Jogos", "Streaming", "Passeios", "Viagens"], "Família": ["Escola", "Filhos", "Presentes", "Mesada"],
    "Cuidados Pessoais": ["Beleza", "Vestuário", "Barbearia", "Cabeleireiro", "Higiene pessoal"],
    "Pets": ["Veterinário", "Banho", "Ração", "Medicamentos"],
    "Financeiro": ["Juros", "Tarifas", "IOF", "Financiamentos", "Dívidas", "Seguros"],
    "Planejamento": ["Reserva de emergência", "Viagem", "Casa", "Carro", "Casamento", "Notebook", "Eventos"],
    "Outros": ["Doações", "Impostos", "Emergências", "Reembolsos", "Outros"],
}
INCOME = ["Salário", "Freelance", "Dividendos", "Venda", "PIX", "Cashback", "Reembolso", "Aluguel", "Outros"]
COLORS = ["#6c5ce7", "#00b894", "#e17055", "#0984e3", "#fdcb6e", "#d63031", "#00cec9"]


def seed_categories(db, workspace_id: int):
        existing = set(db.execute(select(Category.parent_name, Category.name, Category.kind).where(Category.workspace_id == workspace_id)).all())
        for group, names in CATEGORIES.items():
            for i, name in enumerate(names):
                if (group, name, TransactionType.expense) not in existing:
                    db.add(Category(workspace_id=workspace_id, name=name, parent_name=group, kind=TransactionType.expense, color=COLORS[i % len(COLORS)]))
        for i, name in enumerate(INCOME):
            if ("Receitas", name, TransactionType.income) not in existing:
                db.add(Category(workspace_id=workspace_id, name=name, parent_name="Receitas", kind=TransactionType.income, color=COLORS[i % len(COLORS)]))


def seed_session(db):
        account = db.scalar(select(SystemAccount).where(SystemAccount.name == "VH"))
        if not account:
            account = SystemAccount(name="VH", is_super_account=True)
            db.add(account); db.flush()
        else:
            account.is_super_account = True
        user = db.scalar(select(User).where(User.username == "vh"))
        if not user:
            user = User(username="vh", full_name="Vitor Hugo", password_hash=hash_password("123456"),
                        system_account_id=account.id, is_super_admin=True, account_role=AccountRole.admin)
            db.add(user); db.flush()
        else:
            user.system_account_id = account.id
            user.is_super_admin = True
            user.account_role = AccountRole.admin
        membership = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
        if membership:
            workspace = membership.workspace
        else:
            workspace = Workspace(name="Principal", system_account_id=account.id)
            db.add(workspace); db.flush()
            db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=MemberRole.admin))
        seed_categories(db, workspace.id)
        db.commit()


def run():
    with SessionLocal() as db:
        seed_session(db)
    print("Seed concluído. Acesso inicial: vh / 123456")


if __name__ == "__main__": run()
