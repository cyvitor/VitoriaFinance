import argparse

from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.models import AccountingPeriod, RecurrenceOccurrence, RecurrenceRule, Transaction


def run(confirm: bool) -> None:
    if not confirm:
        raise SystemExit(
            "Operação cancelada. Execute com --confirm para apagar todos os lançamentos."
        )
    with SessionLocal() as db:
        total = db.scalar(select(func.count(Transaction.id))) or 0
        db.execute(delete(RecurrenceOccurrence))
        db.execute(delete(Transaction))
        db.execute(delete(RecurrenceRule))
        db.execute(delete(AccountingPeriod))
        db.commit()
    print(f"Limpeza concluída: {total} lançamento(s) removido(s).")
    print("Usuários, contas, cartões, pessoas e categorias foram preservados.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Limpa os dados financeiros do VitoriaFinance.")
    parser.add_argument("--confirm", action="store_true", help="Confirma a exclusão definitiva.")
    args = parser.parse_args()
    run(args.confirm)
