from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assessment(Record):
    model: str
    purpose: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)
    unfinished_probability: float = Field(ge=0, le=1)
    insufficient_context_probability: float = Field(ge=0, le=1)


class Item(Record):
    id: str
    repo: str
    common_dir: str
    kind: Literal["branch", "stash"]
    name: str
    oid: str
    age_days: int
    subject: str
    base: str | None = None
    base_oid: str | None = None
    merged: bool | None = None
    unique_commits: int | None = None
    upstream: str | None = None
    upstream_exists: bool | None = None
    protected: bool = False
    in_use: bool = False
    files: list[str] = Field(default_factory=list)
    diff_stat: str = ""
    commits: list[str] = Field(default_factory=list)
    recommendation: Literal["keep", "review", "cleanup-candidate"] = "review"
    reasons: list[str] = Field(default_factory=list)
    assessment: Assessment | None = None
    ai_error: str | None = None


class Scan(Record):
    version: Literal[1] = 1
    id: str
    created_at: str
    roots: list[str]
    protected_patterns: list[str]
    stale_days: int
    items: list[Item]
    warnings: list[str] = Field(default_factory=list)


class BatchEntry(Record):
    item: Item
    backup_ref: str
    status: Literal["pending", "backed-up", "deleted", "restored", "failed"] = "pending"
    error: str | None = None


class Batch(Record):
    version: Literal[1] = 1
    id: str
    scan_id: str
    created_at: str
    protected_patterns: list[str]
    entries: list[BatchEntry]
