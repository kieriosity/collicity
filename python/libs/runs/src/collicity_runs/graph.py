"""The task graph a run executes (technical design §4.1, §4.4).

For now a graph is a sequence of steps; branches, parallel blocks and for-each blocks join with
the outline builder (ASG-04). Untrusted content never chooses the path: control flow comes only
from the accepted version's graph, and content fills typed parameters (SEC-18).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,62}$")
    params: dict[str, Any] = Field(default_factory=dict)


class TaskGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[Step, ...] = ()
