"""Deterministic routing functions — the confidence gates (decision #4).

Gate A (after RCA):    composite confidence < config.RCA_ESCALATE_BELOW → escalation.
Gate B (after triage): confidence < config.TRIAGE_UNKNOWN_BELOW → unknown (no commands).

Pure functions of typed IncidentState → next-node name. No LLM, no literals — the
thresholds live in incidentiq.config so calibration (MF-1) stays a one-line change.
This is what makes every branch unit-testable (FR-11) and the safety story auditable.
"""

from __future__ import annotations

from incidentiq import config
from incidentiq.contracts import IncidentType
from incidentiq.state import IncidentState


def route_after_rca(state: IncidentState) -> str:
    """Gate A: low composite RCA confidence (or no report) → escalation, else triage."""
    report = state.rca_report
    # No report means an upstream typed error already fired (rca_node caught LLMCallError).
    if report is None or report.confidence_score < config.RCA_ESCALATE_BELOW:
        return "escalation_node"
    return "triage_router"


# Gate B targets: incident_type → remediation entry node (AGENT_ORCHESTRATION §2.1).
_TYPE_TO_NODE = {
    IncidentType.infra: "runbook_executor",
    IncidentType.config: "config_diff_analyzer",
    IncidentType.code_bug: "ast_code_retriever",
}


def route_after_triage(state: IncidentState) -> str:
    """Gate B: low triage confidence → unknown_path; else route by incident_type.

    `unknown` is the DEFAULT branch — a missing decision or an unmapped type can never
    silently fall through to infra and trigger runbooks (FR-10, 'never infra default').
    """
    decision = state.triage_decision
    if decision is None or decision.confidence < config.TRIAGE_UNKNOWN_BELOW:
        return "unknown_path"
    return _TYPE_TO_NODE.get(decision.incident_type, "unknown_path")

def route_after_remediation(state: IncidentState) -> str:
    """After a path agent: a usable plan → human checkpoint (HITL); else the escalation sink.

    Reuses the SINGLE escalation_node (Task 11) for remediation failures too — a missing or
    empty plan means the agent could not produce a catalog action, which is an escalation.
    """
    plan = state.remediation_plan
    if plan is None or not plan.steps:
        return "escalation_node"
    return "human_checkpoint"