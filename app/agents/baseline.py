"""Proposal extraction schema. Only the supervisor can make this authoritative."""

from datetime import date
from pydantic import Field, field_validator

from app.config import ROOT
from app.gateway import AgentSpec, StrictOutput


class BaselineMilestone(StrictOutput):
    title: str = Field(min_length=1, max_length=200)
    completion_criteria: str = Field(min_length=1, max_length=1000)
    deadline: str | None = None
    source_pages: list[int] = Field(default_factory=list)

    @field_validator("title", "completion_criteria")
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value.strip()

    @field_validator("deadline")
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("Deadline must be YYYY-MM-DD")
        return value

    @field_validator("source_pages")
    @classmethod
    def valid_pages(cls, values):
        if any(page < 1 for page in values):
            raise ValueError("Page numbers start at 1")
        return sorted(set(values))


class BaselineDraft(StrictOutput):
    objectives: list[str] = Field(default_factory=list)
    milestones: list[BaselineMilestone] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("objectives", "warnings")
    @classmethod
    def meaningful_items(cls, values):
        if any(not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("Entries must be nonblank and at most 1000 characters")
        return [value.strip() for value in values]


def baseline_spec() -> AgentSpec:
    return AgentSpec("baseline", (ROOT / "prompts" / "baseline.txt").read_text(encoding="utf-8"),
                     BaselineDraft, tier="fast", allowed_tools=())
