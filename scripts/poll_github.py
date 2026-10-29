"""Run the polling trigger against already onboarded active projects."""

import argparse
import json
import time

from app.config import Settings
from app.db import Database
from app.integrations.github import create_github_reader
from app.scheduler.polling import PollingScheduler, PollingTrigger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One polling/debounce pass, then exit")
    args = parser.parse_args()
    settings = Settings()
    if settings.github_mode != "live":
        parser.error("This runner requires GITHUB_MODE=live. For offline testing: python -m scripts.github_demo")
    db = Database(settings.database_path)
    db.migrate()
    reader = create_github_reader(settings)
    trigger = PollingTrigger(db, settings, reader)
    try:
        if args.once:
            print(json.dumps(trigger.tick(), indent=2))
            return
        scheduler = PollingScheduler(trigger)
        scheduler.start()
        print("Polling active projects; press Ctrl+C to stop. Reviews are queued, not executed.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.close(timeout=settings.request_timeout_seconds * 3 + 30)
    finally:
        reader.close()


if __name__ == "__main__":
    main()
