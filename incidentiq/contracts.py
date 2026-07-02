"""LLM-facing output contracts (the 'draft' schemas we grammar-constrain).

These hold ONLY what the model legitimately decides. Computed fields
(composite confidence, deterministic rule priors) are layered on later by
the graph node to build the full RCAReport/TriageDecision state objects.
See F-8 in plans/TASK_01_*.md and decisions #1/#4.
"""

from enum import Enum

from pydantic import BaseModel, Field


class IncidentType(str, Enum):
    infra = "infra"
    config = "config"
    code_bug = "code_bug"
    unknown = "unknown"


class Citation(BaseModel):
    claim: str = Field(description="A single specific factual claim made in the diagnosis.")
    chunk_id: str = Field(
        description="ID of the evidence chunk supporting this claim; must be one of the provided chunk IDs."
    )


class Hypothesis(BaseModel):
    service: str = Field(description="The service this hypothesis blames.")
    root_cause: str = Field(max_length=300, description="Concise candidate root cause.")
    rank: int = Field(description="1 = most likely. Lower is more likely.")


class RCADraft(BaseModel):
    """What the model emits for diagnosis. Confidence is NOT decided here (decision #1)."""

    probable_cause: str = Field(
        max_length=600, description="Concise root-cause explanation, 1-3 sentences. No rambling."
    )
    root_service: str = Field(description="The single service most likely responsible.")
    source_citations: list[Citation] = Field(
        min_length=1, description="Evidence for the diagnosis; at least one citation required."
    )
    top_hypotheses: list[Hypothesis] = Field(
        default_factory=list, description="Ranked alternative explanations, most likely first."
    )
    llm_confidence_raw: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="The model's own rough confidence; ADVISORY ONLY, never used for routing.",
    )


class TriageDraft(BaseModel):
    """What the model emits for triage. rule_prior / llm_agreed come from the rule layer (decision #4)."""

    incident_type: IncidentType = Field(description="Best-fit category for this incident.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the classification.")
    rationale: str = Field(max_length=400, description="Brief justification, 1-2 sentences.")

class RemediationDraft(BaseModel):
    """What the model emits to remediate: a catalog command_id + its args (FR-12, doc line 249).

    The model picks an id from the presented MENU and fills args — it NEVER writes shell.
    Validated against the real catalog before becoming a CommandIntent (the CI-4 backstop)."""

    command_id: str = Field(description="Exactly one command_id chosen from the provided MENU.")
    args: dict[str, str | int | bool] = Field(
        default_factory=dict, description="Arguments for the chosen command, matching its schema."
    )
    summary: str = Field(max_length=400, description="One-line justification for the chosen action.")


class PatchDraft(BaseModel):
    """What the model emits to fix a code bug: the WHOLE corrected function, nothing else (decision #3).

    The model is only ever asked to rewrite one function correctly — it never produces a diff or
    line numbers (small models get those wrong). The unified diff + syntax check are computed
    deterministically from this body afterwards. `new_function_body` must be a drop-in replacement
    for the localized function (same name/signature), preserving its original indentation."""

    new_function_body: str = Field(
        description="The complete corrected function, from its declaration to its closing line. "
                    "Same name and signature as the original; original indentation preserved. "
                    "No surrounding code, no explanation, no markdown fences."
    )
    summary: str = Field(max_length=400, description="One-line description of what the fix changes and why.")