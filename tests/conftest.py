import os
os.environ["DATABASE_URL"] = "sqlite:///./test_vitoria.db"
os.environ["SECRET_KEY"] = "test-secret"

import pytest
from sqlalchemy import event
from fastapi.testclient import TestClient
from app.database import Base, engine
from app.main import app


@event.listens_for(engine, "connect")
def _sqlite_sem_fsync(dbapi_connection, _record):
    # Cada teste recria o schema inteiro; com fsync em disco lento (runner em
    # container/LXC) isso levava ~28 min contra ~1 min sem fsync. O banco de
    # teste e descartavel, entao a durabilidade nao importa.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.execute("PRAGMA journal_mode=MEMORY")
    cursor.close()


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(): return TestClient(app)

