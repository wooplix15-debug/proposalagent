"""Validated, JSON-compatible contracts at graph and human-review boundaries."""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strict=True)]
Answer = Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                        min_length=1, max_length=4000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class RequirementSection(Contract):
    product: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
    requirements: list[Text]


class Question(Contract):
    id: Text
    question: Text
    reason: Text = ""
    options: list[Text] = Field(default_factory=list)


class Analysis(Contract):
    summary: Text
    requirement_sections: list[RequirementSection]
    questions: list[Question]
    commercial_categories: list[Text] = Field(default_factory=list)
    duration_estimates: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_questions(self):
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Clarification question IDs must be unique.")
        if not self.requirement_sections:
            raise ValueError("No requirements were found; supply a more detailed requirement.")
        return self


class Client(Contract):
    company_name: Text = ""
    project_name: Text = ""
    contact: Text = ""


class ScopeArea(Contract):
    area: Text = ""
    tasks: list[Text]


class ScopeModule(Contract):
    product: Text
    areas: list[ScopeArea]


class Proposal(Contract):
    client: Client
    project_introduction: Text
    scope: list[ScopeModule]
    prerequisites: list[Text] = Field(default_factory=list)
    deliverables: list[Text] = Field(default_factory=list)
    open_points: list[Text] = Field(default_factory=list)
    timeline: dict[str, Any] | None = None
    commercials: dict[str, Any] | None = None
    status: Literal["DRAFT"] = "DRAFT"


class BRDRequirement(Contract):
    id: Text
    area: Text
    requirement: Text


class SpecificationTable(Contract):
    section: Text
    title: Text
    columns: list[Text]
    rows: list[list[Text]]
    origin: Literal["source", "proposed", "organized"]
    requirement_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def rectangular(self):
        if len(self.columns) < 2 or not self.rows:
            raise ValueError("Specification tables need columns and data rows.")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("A specification table has a mismatched row width.")
        return self


class BRD(Contract):
    title: Text
    project_name: Text
    status: Literal["Draft for business review"]
    requirements: list[BRDRequirement]
    specification_tables: list[SpecificationTable]
    summary: Text
    areas: list[dict[str, Any]]
    requirements_by_area: list[dict[str, Any]]
    process_views: list[dict[str, Any]]
    current_state: list[Text]
    objectives: list[Text]
    stakeholders: list[Text]
    training_requirements: list[Text]
    open_decisions: list[dict[str, Any]]
    acceptance_criteria: list[Text]
    prepared_by: Text
    date: Text


class ClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: dict[str, Answer] = Field(default_factory=dict)


class DraftReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["accept", "cancel"]
    reviewer: Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                             min_length=1, max_length=200)]
    edited_document: dict[str, Any] | None = None


def validate_answers(analysis, answers):
    response = ClarificationResponse.model_validate({"answers": answers})
    expected = {question["id"] for question in analysis.get("questions", [])}
    if set(response.answers) != expected:
        raise ValueError("Answer each question or choose Leave open for discovery.")
    return response.answers
