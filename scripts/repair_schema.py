"""Repara diferenças pontuais em bases criadas durante a fase de desenvolvimento."""
from sqlalchemy import inspect

from app.database import engine


def run():
    inspector = inspect(engine)
    changes = 0
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "default_person_id" not in user_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN default_person_id INTEGER NULL")
            if engine.dialect.name == "mysql":
                connection.exec_driver_sql(
                    "ALTER TABLE users ADD CONSTRAINT fk_users_default_person "
                    "FOREIGN KEY (default_person_id) REFERENCES people(id) ON DELETE SET NULL"
                )
        print("Coluna users.default_person_id adicionada.")
        changes += 1
    account_columns = {column["name"] for column in inspect(engine).get_columns("system_accounts")}
    if "is_super_account" not in account_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE system_accounts ADD COLUMN is_super_account BOOLEAN NOT NULL DEFAULT FALSE"
            )
        print("Coluna system_accounts.is_super_account adicionada.")
        changes += 1
    with engine.begin() as connection:
        result = connection.exec_driver_sql(
            "UPDATE system_accounts SET is_super_account = TRUE "
            "WHERE name = 'VH' AND is_super_account = FALSE"
        )
        if result.rowcount:
            print("Conta VH marcada como super conta.")
            changes += 1
    if not changes:
        print("Estrutura já está atualizada; nenhuma alteração necessária.")


if __name__ == "__main__":
    run()
