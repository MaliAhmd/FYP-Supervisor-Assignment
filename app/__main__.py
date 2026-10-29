"""Phase 1 bootstrap: python -m app init-db | status."""

import argparse
import json

from app.config import Settings
from app.db import Database, ProjectStore
from app.logging_config import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="SupervisorAI local database bootstrap")
    parser.add_argument("command", choices=["init-db", "status"])
    args = parser.parse_args()
    settings = Settings()
    configure_logging()
    db = Database(settings.database_path)
    if args.command == "init-db":
        db.migrate()
        print(f"Database initialized: {db.path}")
        return
    if not db.path.exists():
        parser.error("Database not initialized. Run: python -m app init-db")
    with db.transaction(write=False) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    print(json.dumps({
        "database": str(db.path), "journal_mode": journal_mode,
        "migrations": versions, "project_count": len(ProjectStore.list_projects(db)),
        "llm_mode": settings.llm_mode, "github_mode": settings.github_mode,
        "email_mode": settings.email_mode,
    }, indent=2))


if __name__ == "__main__":
    main()
