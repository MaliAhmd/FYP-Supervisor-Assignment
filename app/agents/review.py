"""Typed advisory agents. The controller owns all actions and side effects."""
from typing import Literal

from pydantic import Field

from app.config import ROOT
from app.gateway.types import AgentSpec, StrictOutput


class CompactOutput(StrictOutput):
    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        def compact(value):
            if isinstance(value, dict):
                return {k: compact(v) for k, v in value.items() if k != "title"}
            return [compact(v) for v in value] if isinstance(value, list) else value
        return compact(schema)


Action = Literal['review_repo', 'review_report', 'analyze_reply', 'check_prior_feedback', 'draft_followup', 'no_action']


class Plan(CompactOutput):
    actions: list[Action] = Field(max_length=6)


class Finding(CompactOutput):
    milestone_id: str | None = None
    text: str = Field(min_length=1, max_length=700)
    severity: Literal['info', 'warning', 'critical'] = 'info'
    evidence_ids: list[str] = Field(max_length=8)


class FeedbackCheck(CompactOutput):
    feedback_id: str
    status: Literal['open', 'partial', 'resolved']
    evidence_ids: list[str] = Field(max_length=8)


class RepoReview(CompactOutput):
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    feedback: list[FeedbackCheck] = Field(default_factory=list, max_length=8)


class ReportReview(RepoReview):
    pass


class Claim(CompactOutput):
    text: str = Field(min_length=1, max_length=500)
    milestone_id: str | None = None
    source_id: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    completion: bool = False
    due_date: str | None = None


class Communications(CompactOutput):
    claims: list[Claim] = Field(default_factory=list, max_length=8)
    narrative: str = Field(default='', max_length=500)
    subject: str = Field(default='', max_length=200, pattern=r'^[^\r\n]*$')
    body: str = Field(default='', max_length=2500)


def review_specs():
    definitions = [
        ('coordinator', Plan, 'fast', ('load_memory',)),
        ('repo_reviewer', RepoReview, 'smart', ('read_diff', 'check_feedback')),
        ('report_reviewer', ReportReview, 'smart', ('retrieve_passages', 'check_feedback')),
        ('claims_comms', Communications, 'smart', ('read_evidence',)),
    ]
    return [AgentSpec(name, (ROOT / 'prompts' / f'{name}.txt').read_text(encoding='utf-8'), schema, tier, allowed)
            for name, schema, tier, allowed in definitions]


def all_specs():
    from app.agents.baseline import baseline_spec
    return [baseline_spec(), *review_specs()]
