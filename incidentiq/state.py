"""Full state objects (CONTRACTS.md §2) — the models threaded through the graph.

Distinct from incidentiq/contracts.py, which holds the lean LLM-facing *draft*
schemas the grammar constrains. These full models add fields computed post-hoc
(entailment_score, confidence_score, …) that the model never emits (F-8, D-2).
"""



from __future__ import annotations

import re

from datetime import datetime, date
from enum import Enum
from typing import Annotated, Literal
from operator import add     

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from incidentiq.contracts import IncidentType  # one enum, one source of truth
from incidentiq.errors import TypedError

DEFAULT_TRIAGE_THRESHOLD = 0.70

def chunk_id_context(retrieved: "RetrievedContext") -> dict:
    """Build the validation context RCAReport needs: the set of citable chunk_ids."""
    return {"valid_chunk_ids": {c.chunk_id for c in retrieved.chunks}}

_ARG_TYPES = {"string": str, "int": int, "bool": bool}


def validate_command_args(args: dict, arg_schema: dict) -> list[str]:
    """Check args against a catalog command's arg schema. Returns a list of error strings.

    Shared by the CommandIntent validator (contract boundary) and, later, the Task-3 renderer
    (execution time) — one definition, enforced twice (defense in depth).
    """
    errors: list[str] = []
    for name in args:
        if name not in arg_schema:
            errors.append(f"unknown arg {name!r}")
    for name, spec in arg_schema.items():
        if name not in args:
            if "default" not in spec:
                errors.append(f"missing required arg {name!r}")
            continue
        value = args[name]
        expected = _ARG_TYPES.get(spec.get("type", "string"), str)
        # bool is a subclass of int — an int arg must reject a bool explicitly.
        if expected is int and isinstance(value, bool):
            errors.append(f"arg {name!r} must be int, got bool")
            continue
        if not isinstance(value, expected):
            errors.append(f"arg {name!r} must be {spec.get('type', 'string')}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"arg {name!r}={value!r} not in enum {spec['enum']}")
        if "pattern" in spec and expected is str and not re.fullmatch(spec["pattern"], value):
            errors.append(f"arg {name!r}={value!r} fails pattern {spec['pattern']!r}")
    return errors

# --- §2.1 Enums -------------------------------------------------------------

class IncidentStatus(str, Enum):
    created = "created"
    investigating = "investigating"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    resolved = "resolved"
    escalated = "escalated"
    unknown = "unknown"
    timed_out_pending_approval = "timed_out_pending_approval"
    closed_transient = "closed_transient"   # CI-1: self-resolved before investigation finished


class ApprovalChoice(str, Enum):
    approved = "approved"
    rejected = "rejected"
    edited = "edited"


class RemediationClass(str, Enum):
    flag_rollback = "flag_rollback"
    patch = "patch"
    kubectl = "kubectl"
    config_revert = "config_revert"
    none = "none"


# --- §2.2 Ingestion ---------------------------------------------------------

class Deploy(BaseModel):
    sha: str
    deployed_at: datetime
    author: str | None = None


class IncidentContext(BaseModel):
    service: str
    alert_name: str
    namespace: str | None = None
    severity: str | None = None
    summary: str                                 # from public_annotations (DATA)
    affected_endpoint: str | None = None
    traceback: str | None = None                 # null → keyword fallback
    repo_url: str | None = None
    deploy_commit: str | None = None
    last_deploys: list[Deploy] = []
    deploy_gap_minutes: int | None = None        # signal for triage prior
    alerts_truncated: bool = False               # FR-24 → confidence −0.10
    related_alerts: list[dict] = []              # alerts[1:] logged, not processed
    starts_at: datetime                          # anchors the post-mortem timeline (FR-17)
    # INVARIANT: oracle annotations (file/line) are NEVER present on this model.


# --- §2.3 Diagnosis value objects -------------------------------------------

class RetrievedChunk(BaseModel):
    chunk_id: str                                # resolvable to a real stored chunk (FR-09)
    source_doc: str
    parent_section: str | None = None            # FR-07 parent-child; never an orphan sub-chunk
    text: str                                    # treated as DATA only
    semantic_score: float
    bm25_score: float | None = None
    rerank_score: float | None = None
    corpus: Literal["postmortem", "runbook"]


class RetrievedContext(BaseModel):
    chunks: list[RetrievedChunk]                 # fused (RRF) + reranked, top-5
    chunks_over_threshold: int                   # feeds confidence (FR-06)
    retriever_agreement: float                   # overlap between BM25 & semantic
    degraded: bool = False                       # one retriever empty → graceful


class Citation(BaseModel):
    claim: str
    chunk_id: str                                # MUST exist in RetrievedContext (validated in 2c)
    entailment_score: float | None = None        # CI-3: local NLI claim↔chunk support


class Hypothesis(BaseModel):
    service: str
    root_cause: str
    rank: int


class ConfidenceBreakdown(BaseModel):            # CI-2: the "why" behind the number
    self_consistency_agreement: float            # e.g. 0.67 = 2/3 RCA samples agreed
    retrieval_evidence_strength: float           # normalized f(top-k sim, chunks_over_threshold, agreement)
    chunks_over_threshold: int
    penalties_applied: list[str] = []            # e.g. ["weak_retrieval -0.15", "truncated -0.10"]



# --- §2.5 Remediation -------------------------------------------------------

class CommandIntent(BaseModel):                  # the ONLY executable shape (FR-12/36)
    command_id: str                              # MUST exist in catalog (validated in 2e)
    args: dict[str, str | int | bool]
    approval_required: bool = True
    # Raw shell strings are structurally unrepresentable here — args is a typed dict.

    @model_validator(mode="after")
    def command_is_in_catalog(self, info: ValidationInfo) -> "CommandIntent":
        # Fail-closed: no catalog in scope → cannot prove this command is allowed → refuse.
        catalog = (info.context or {}).get("catalog")
        if catalog is None:
            raise ValueError(
                "CommandIntent requires context={'catalog': {...}} to verify the command is "
                "allowed; refusing to build an unvalidated executable intent (fail-closed)."
            )
        spec = catalog.get(self.command_id)
        if spec is None:                                    # the CI-4 backstop
            raise ValueError(f"command_id {self.command_id!r} is not in the catalog")
        errors = validate_command_args(self.args, spec.get("args", {}))
        if errors:
            raise ValueError("; ".join(errors))
        return self

class RemediationPlan(BaseModel):                # GRAMMAR-CONSTRAINED OUTPUT
    remediation_class: RemediationClass
    summary: str
    steps: list[CommandIntent] = []              # empty for unknown/escalation paths
    references: list[Citation] = []


class CodeContext(BaseModel):
    language: Literal["python", "javascript", "go", "csharp", "rust", "unsupported"]
    file_path: str | None = None
    function_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    callers: list[str] = []
    source_excerpt: str | None = None            # DATA (untrusted code)
    retrieval_ok: bool
    via: Literal["traceback", "keyword_fallback"]   # FR-13
    patch_supported: bool                        # True only for py/js/go


class Patch(BaseModel):                          # diff is DERIVED, not LLM-emitted (decision #3)
    new_function_body: str                       # the LLM output (grammar-constrained string)
    unified_diff: str                            # computed deterministically vs original
    syntax_valid: bool                           # py_compile / node --check / gofmt -e
    scope_ok: bool                               # SF-5: fix contained in the localized function
    attempts: int
    pr_url: str | None = None                    # set only after approval + PR creation


# --- §2.3 Diagnosis output --------------------------------------------------

class RCAReport(BaseModel):                       # GRAMMAR-CONSTRAINED OUTPUT
    probable_cause: str
    root_service: str
    confidence_score: float = Field(ge=0.0, le=1.0)   # COMPOSITE, set post-hoc (decision #1)
    confidence_breakdown: ConfidenceBreakdown         # CI-2: the components, for UI + audit
    llm_confidence_raw: float | None = None           # model's own number, advisory only
    self_consistency_agreement: float | None = None   # share of N=3 agreeing on root_service
    source_citations: list[Citation] = Field(min_length=1)   # every claim cited (FR-09)
    top_hypotheses: list[Hypothesis] = []
    # Validator (2c): every Citation.chunk_id ∈ retrieved chunk_ids.

    @model_validator(mode="after")
    def citations_resolve_to_retrieved_chunks(self, info: ValidationInfo) -> "RCAReport":
        # Fail-closed (D-3): without the retrieval set we CANNOT prove grounding, so refuse.
        valid_ids = (info.context or {}).get("valid_chunk_ids")
        if valid_ids is None:
            raise ValueError(
                "RCAReport requires context={'valid_chunk_ids': {...}} to verify citation "
                "grounding; refusing to build an unverifiable report (fail-closed)."
            )
        unknown = sorted({c.chunk_id for c in self.source_citations if c.chunk_id not in valid_ids})
        if unknown:
            raise ValueError(f"citations reference chunk_ids not in the retrieved set: {unknown}")
        return self

    @classmethod
    def grounded(cls, *, retrieved: "RetrievedContext", **fields) -> "RCAReport":
        """The only blessed constructor: citations are checked against `retrieved`."""
        return cls.model_validate(fields, context=chunk_id_context(retrieved))

# --- §2.4 Triage ------------------------------------------------------------

class TriageDecision(BaseModel):                  # GRAMMAR-CONSTRAINED OUTPUT
    incident_type: IncidentType
    confidence: float = Field(ge=0.0, le=1.0)
    rule_prior: IncidentType                       # deterministic pre-classification (decision #4)
    rule_prior_strength: float                     # how strong the alert signal was
    llm_agreed: bool                               # rule vs LLM agreement
    rationale: str
    llm_incident_type_raw: IncidentType | None = None   # model's pre-coercion guess; advisory only
    # Post-validator (2d): confidence < 0.70 → coerce incident_type = unknown (FR-10).

    @model_validator(mode="after")
    def coerce_unknown_below_threshold(self, info: ValidationInfo) -> "TriageDecision":
        threshold = (info.context or {}).get("triage_threshold", DEFAULT_TRIAGE_THRESHOLD)
        if self.confidence < threshold and self.incident_type is not IncidentType.unknown:
            if self.llm_incident_type_raw is None:        # don't clobber on re-validation
                self.llm_incident_type_raw = self.incident_type
            self.incident_type = IncidentType.unknown
        return self

# --- §2.6 Review / execution / post-mortem ----------------------------------

class ApprovalDecision(BaseModel):
    decision: ApprovalChoice
    notes: str | None = None
    edited_plan: RemediationPlan | None = None
    decided_at: datetime
    decided_by: str


class ExecutionStep(BaseModel):
    command_id: str
    rendered: str                                  # catalog-rendered (audit), never LLM string
    args: dict
    stdout: str
    exit_code: int
    ts: datetime


class ExecutionLog(BaseModel):
    steps: list[ExecutionStep] = []
    execution_skipped: bool = False                # timeout path (FR-28)
    # INVARIANT: append-only; written only if ApprovalDecision.decision == approved (FR-16).


class ActionItem(BaseModel):                       # SMART, structured BEFORE prose (FR-18)
    description: str
    owner: str
    due_date: date
    measurable: str


class PostMortem(BaseModel):                       # GRAMMAR-CONSTRAINED for action_items
    timeline: list[tuple[datetime, str, str]]      # (ts, actor, event) anchored on starts_at
    root_cause: str
    action_items: list[ActionItem]
    citations: list[Citation]
    markdown: str                                  # rendered last from the structured fields
    execution_skipped: bool = False
    terminal_reason: Literal["resolved", "unknown", "escalated", "timed_out"] = "resolved"


# --- §2.7 Observability -----------------------------------------------------

class AgentSpan(BaseModel):
    node: str
    start_time: datetime
    end_time: datetime
    token_count: int | None = None
    redaction_applied: bool = True                 # NFR observability

# --- §2.8 The threaded state ------------------------------------------------

class IncidentState(BaseModel):
    incident_id: str
    status: IncidentStatus
    raw_payload: dict
    alertmanager_fingerprint: str
    incident_context: IncidentContext
    retrieved_context: RetrievedContext | None = None
    rca_report: RCAReport | None = None
    triage_decision: TriageDecision | None = None
    code_context: CodeContext | None = None
    remediation_plan: RemediationPlan | None = None
    patch: Patch | None = None
    approval_decision: ApprovalDecision | None = None
    execution_log: ExecutionLog = ExecutionLog()
    errors: Annotated[list[TypedError], add] = []     # additive reducer (LangGraph reads `add`)
    trace:  Annotated[list[AgentSpan], add] = []      # additive reducer