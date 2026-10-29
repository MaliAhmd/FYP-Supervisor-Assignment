import pytest

from app.db import Database, ProjectStore


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def project(db):
    return ProjectStore.create(db, "Amina Example", "amina@example.com", "fictional/amina-thesis")
