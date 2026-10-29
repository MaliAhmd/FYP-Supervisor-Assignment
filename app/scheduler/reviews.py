"""Background review driver. One explicit batch retains its global call budget."""
from app.scheduler.polling import PollingScheduler


class ReviewRunner:
    def __init__(self, engine, batch_id):
        if not batch_id or len(batch_id) > 200:
            raise ValueError('An explicit batch ID is required')
        self.engine, self.batch_id = engine, batch_id

    def tick(self):
        with self.engine.db.transaction(write=False) as conn:
            rows = conn.execute("""SELECT c.project_id,c.id FROM cycles c JOIN projects p ON p.id=c.project_id
                WHERE c.status IN ('queued','running') AND c.trigger!='proposal_upload'
                AND p.status!='paused' AND p.baseline_confirmed_at IS NOT NULL
                AND c.rowid=(SELECT min(x.rowid) FROM cycles x WHERE x.project_id=c.project_id
                    AND x.status IN ('queued','running','pending_evidence_review') AND x.trigger!='proposal_upload')
                ORDER BY c.project_id""").fetchall()
        return [self.engine.step(r['project_id'], r['id'], batch_id=self.batch_id) for r in rows]

    def background(self, *, interval_seconds=1):
        """Caller owns start/close; no worker starts on import."""
        return PollingScheduler(self, interval_seconds=interval_seconds)
