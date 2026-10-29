"""Safe default review fixtures; these make no claims about student progress."""
from app.gateway.clients import FakeLLMClient


class FakeReviewClient(FakeLLMClient):
    def __init__(self, script=(), *, responses=None):
        defaults = {
            'coordinator': {'actions': ['no_action']},
            'repo_reviewer': {'findings': [], 'feedback': []},
            'report_reviewer': {'findings': [], 'feedback': []},
            'claims_comms': {'claims': [], 'narrative': 'Fake mode; no progress judgment was made.',
                             'subject': 'Supervisor review requested',
                             'body': 'Fake-mode draft: please review the saved evidence and add specific feedback before approving.'},
        }
        defaults.update(responses or {})
        super().__init__(script, responses=defaults)
