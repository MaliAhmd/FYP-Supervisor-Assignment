"""Credential-free Phase 2 smoke demo; no email or GitHub activity."""

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway import AgentSpec, Gateway
from app.gateway.types import GatewayDemoOutput


def main():
    parser = argparse.ArgumentParser(description="SupervisorAI fake gateway smoke demo")
    parser.add_argument("--database", type=Path, default=Path("data/gateway_demo.sqlite3"))
    args = parser.parse_args()
    settings = Settings(_env_file=None, database_path=args.database, llm_mode="fake", github_mode="fake", email_mode="test")
    db = Database(settings.database_path)
    db.migrate()
    matches = [item for item in ProjectStore.list_projects(db) if item["repo_full_name"] == "fictional/gateway-demo"]
    project = ProjectStore(db, matches[0]["id"]) if matches else ProjectStore.create(
        db, "Gateway Demo Student", "gateway-demo@example.com", "fictional/gateway-demo",
    )
    spec = AgentSpec("repo_reviewer", "Describe the evidence without asserting completion.", GatewayDemoOutput)
    with Gateway(db, settings, [spec]) as gateway:
        cycle = project.create_cycle("gateway-demo-sha-1")
        job = gateway.submit(project.project_id, cycle, "gateway-demo-batch", "repo_reviewer", data={"changed_files": ["main.py"]})
        first = gateway.wait(project.project_id, job)
    # A fresh service reads all state from SQLite and reuses the saved response.
    with Gateway(db, settings, [spec]) as restarted:
        cycle = project.create_cycle("gateway-demo-sha-2")
        job = restarted.submit(project.project_id, cycle, "gateway-demo-batch", "repo_reviewer", data={"changed_files": ["main.py"]})
        second = restarted.wait(project.project_id, job)
        print(json.dumps({"first": first.model_dump(mode="json"), "after_restart": second.model_dump(mode="json"),
                          "meter": restarted.status(project.project_id)}, indent=2))
        if first.status != "completed" or second.status != "completed":
            raise SystemExit("Gateway demo did not complete; inspect persisted reasons above")
    with db.transaction(write=False) as conn:
        calls = [dict(row) for row in conn.execute(
            "SELECT agent,model,outcome,cache_hit,tokens_in,tokens_out FROM llm_calls WHERE project_id=? ORDER BY created_at,id",
            (project.project_id,),
        )]
    print(json.dumps({"calls": calls}, indent=2))


if __name__ == "__main__":
    main()
