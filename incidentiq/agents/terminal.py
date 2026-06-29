"""Terminal sinks: the central escalation node + the unknown path (Orchestration #3).

Both are DETERMINISTIC (no LLM, F11-2) and emit a no-command RemediationPlan
(remediation_class=none, steps=[], F11-1) — the structural guarantee that an
unknown/escalation outcome can never carry an executable action (decision #10).
"""
from __future__ import annotations

from datetime import datetime, timezone

from incidentiq import config
from incidentiq.errors import TypedError
from incidentiq.state import IncidentState, RemediationClass, RemediationPlan


def _evidence_lines(state: IncidentState) -> list[str]:
    """Shared evidence header from whatever diagnosis we managed to produce."""
    ctx = state.incident_context
    lines = [f"Incident {state.incident_id}: {ctx.alert_name} on {ctx.service}."]
    rca = state.rca_report
    if rca is not None:
        lines.append(
            f"Probable cause: {rca.probable_cause} "
            f"(root service: {rca.root_service}, confidence {rca.confidence_score:.2f})."
        )
    return lines


def synthesize_escalation(state: IncidentState) -> tuple[RemediationPlan, list[TypedError]]:
    """Flow D: Slack-ready evidence summary from typed errors. No commands.

    Returns the no-command plan AND any new TypedError to append (F11-3): a low-confidence
    escalation arrives with no upstream error, so we backfill a typed reason here.
    """
    new_errors: list[TypedError] = []
    if not state.errors:
        rca = state.rca_report
        reason = (
            f"RCA confidence {rca.confidence_score:.2f} below escalation gate "
            f"{config.RCA_ESCALATE_BELOW}"
            if rca is not None
            else "no RCA report produced"
        )
        new_errors.append(TypedError(
            node="escalation_node", kind="low_confidence", reason=reason,
            ts=datetime.now(timezone.utc),
        ))

    all_errors = list(state.errors) + new_errors
    lines = _evidence_lines(state)
    lines.append("Escalated to a human — automated diagnosis was not confident enough to act.")
    lines.append("Failures:")
    lines += [f"  - [{e.node}/{e.kind}] {e.reason}" for e in all_errors]

    references = state.rca_report.source_citations if state.rca_report else []
    plan = RemediationPlan(
        remediation_class=RemediationClass.none,
        summary="\n".join(lines),
        steps=[],                       # F11-1: structurally no commands
        references=references,
    )
    return plan, new_errors


def synthesize_unknown(state: IncidentState) -> RemediationPlan:
    """Flow C: evidence + ranked hypotheses, no commands. The honest default (FR-10)."""
    lines = _evidence_lines(state)
    lines.append("Triage confidence below the unknown gate — emitting evidence, not actions.")

    rca = state.rca_report
    hypotheses = rca.top_hypotheses if rca else []
    if hypotheses:
        lines.append("Ranked hypotheses:")
        for h in sorted(hypotheses, key=lambda x: x.rank):
            lines.append(f"  {h.rank}. {h.service}: {h.root_cause}")

    references = rca.source_citations if rca else []
    return RemediationPlan(
        remediation_class=RemediationClass.none,
        summary="\n".join(lines),
        steps=[],                       # F11-1: structurally no commands
        references=references,
    )