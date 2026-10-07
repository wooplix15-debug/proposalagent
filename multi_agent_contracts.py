"""Typed findings exchanged by specialists; the source register is immutable."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Requirement(Finding):
    id: str
    area: str
    requirement: str
    source: str
    source_excerpt: str
    inclusion: Literal["requested", "qualified"] = "requested"


class Task(Finding):
    specialist: Literal["solution", "delivery", "commercial", "risk"]
    focus: str
    requirement_ids: list[str] = Field(default_factory=list)


class ProjectMetadata(Finding):
    company_name: str = ""
    project_name: str = ""


class SupervisorPlan(Finding):
    tasks: list[Task]
    quality_focus: list[str] = Field(default_factory=list)
    project_metadata: ProjectMetadata = Field(default_factory=ProjectMetadata)


class Decision(Finding):
    question: str
    reason: str
    requirement_ids: list[str]


class Solution(Finding):
    requirement_ids: list[str]
    product: str
    design: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["proposed", "open"] = "proposed"


class SolutionReport(Finding):
    solutions: list[Solution] = Field(min_length=1)
    open_decisions: list[Decision] = Field(default_factory=list)


class Dependency(Finding):
    requirement_ids: list[str]
    phase: str
    description: str
    condition: str


class DeliveryReview(Finding):
    dependencies: list[Dependency]
    open_decisions: list[Decision] = Field(default_factory=list)


class CommercialReview(Finding):
    dependencies: list[Decision]


class Risk(Finding):
    requirement_ids: list[str]
    description: str
    impact: str
    mitigation: str
    status: Literal["open", "planning_assumption"] = "open"


class RiskReport(Finding):
    risks: list[Risk]


class QualityIssue(Finding):
    severity: Literal["blocking", "warning"]
    category: Literal["unsupported_claim", "contradiction", "missing_requirement"]
    path: str
    excerpt: str
    reason: str
    suggested_fix: str
    requirement_ids: list[str] = Field(default_factory=list)


class QualityReport(Finding):
    issues: list[QualityIssue]


class TextPatch(Finding):
    path: str
    replacement: str


class PatchReport(Finding):
    patches: list[TextPatch]
