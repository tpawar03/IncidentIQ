import pytest
from pydantic import ValidationError

from incidentiq.state import TriageDecision
from incidentiq.contracts import IncidentType
from incidentiq.state import CommandIntent, validate_command_args

from incidentiq.state import (
    RCAReport, RetrievedChunk, RetrievedContext, Citation, ConfidenceBreakdown,
    chunk_id_context,
)


def _retrieved(*chunk_ids: str) -> RetrievedContext:
    return RetrievedContext(
        chunks=[
            RetrievedChunk(chunk_id=cid, source_doc="d", text="t",
                           semantic_score=0.9, corpus="postmortem")
            for cid in chunk_ids
        ],
        chunks_over_threshold=len(chunk_ids),
        retriever_agreement=0.5,
    )


def _rca_fields(*citation_chunk_ids: str) -> dict:
    return dict(
        probable_cause="cartservice OOM after deploy",
        root_service="cartservice",
        confidence_score=0.62,
        confidence_breakdown=ConfidenceBreakdown(
            self_consistency_agreement=0.67, retrieval_evidence_strength=0.5,
            chunks_over_threshold=len(citation_chunk_ids),
        ),
        source_citations=[Citation(claim="c", chunk_id=cid) for cid in citation_chunk_ids],
    )


def test_citations_in_retrieved_set_pass():
    retrieved = _retrieved("pm_0007", "rb_0001")
    rca = RCAReport.grounded(retrieved=retrieved, **_rca_fields("pm_0007"))
    assert rca.source_citations[0].chunk_id == "pm_0007"


def test_hallucinated_chunk_id_rejected():
    retrieved = _retrieved("pm_0007")
    with pytest.raises(ValidationError, match="not in the retrieved set"):
        RCAReport.grounded(retrieved=retrieved, **_rca_fields("pm_9999"))


def test_missing_context_fails_closed():
    # the plain constructor / context-less model_validate cannot prove grounding
    with pytest.raises(ValidationError, match="fail-closed"):
        RCAReport(**_rca_fields("pm_0007"))
    with pytest.raises(ValidationError, match="fail-closed"):
        RCAReport.model_validate(_rca_fields("pm_0007"))


def test_context_helper_extracts_ids():
    assert chunk_id_context(_retrieved("a", "b")) == {"valid_chunk_ids": {"a", "b"}}


def _triage(incident_type: IncidentType, confidence: float, **over) -> dict:
    base = dict(
        incident_type=incident_type, confidence=confidence,
        rule_prior=IncidentType.infra, rule_prior_strength=0.8,
        llm_agreed=True, rationale="r",
    )
    base.update(over)
    return base


def test_low_confidence_coerced_to_unknown():
    t = TriageDecision(**_triage(IncidentType.code_bug, 0.55))
    assert t.incident_type is IncidentType.unknown
    assert t.confidence == 0.55          # confidence + rationale preserved, only the type changes


def test_confident_type_preserved():
    t = TriageDecision(**_triage(IncidentType.code_bug, 0.91))
    assert t.incident_type is IncidentType.code_bug


def test_threshold_overridable_via_context():
    # a 0.80 triage that passes the default 0.70 gets coerced under a stricter calibrated 0.90
    t = TriageDecision.model_validate(
        _triage(IncidentType.infra, 0.80), context={"triage_threshold": 0.90}
    )
    assert t.incident_type is IncidentType.unknown


def test_coercion_is_idempotent_round_trip():
    t = TriageDecision(**_triage(IncidentType.config, 0.40))
    assert TriageDecision.model_validate(t.model_dump()) == t   # already unknown, stays stable

CATALOG = {
    "flag_rollback": {
        "args": {
            "flag_key": {"type": "string"},
            "flagd_url": {"type": "string", "default": "http://flagd:8013"},
        },
        "remediation_class": "flag_rollback",
    },
    "config_revert": {
        "args": {"commit": {"type": "string", "pattern": r"[0-9a-f]{7,40}"}},
        "remediation_class": "config_revert",
    },
}


def _cmd(command_id: str, args: dict) -> CommandIntent:
    return CommandIntent.model_validate(
        {"command_id": command_id, "args": args}, context={"catalog": CATALOG}
    )


def test_valid_command_passes():
    cmd = _cmd("flag_rollback", {"flag_key": "cartFailure"})   # flagd_url defaulted, omittable
    assert cmd.command_id == "flag_rollback"


def test_injected_command_id_rejected():
    with pytest.raises(ValidationError, match="not in the catalog"):
        _cmd("delete_everything", {})                          # CI-4: the backstop in action


def test_missing_catalog_fails_closed():
    with pytest.raises(ValidationError, match="fail-closed"):
        CommandIntent(command_id="flag_rollback", args={"flag_key": "x"})


def test_unknown_arg_rejected():
    with pytest.raises(ValidationError, match="unknown arg"):
        _cmd("flag_rollback", {"flag_key": "x", "rm_rf": "/"})


def test_pattern_violation_rejected():
    with pytest.raises(ValidationError, match="fails pattern"):
        _cmd("config_revert", {"commit": "not-a-sha"})


def test_bool_is_not_int():
    schema = {"replicas": {"type": "int"}}
    assert validate_command_args({"replicas": True}, schema) == ["arg 'replicas' must be int, got bool"]
    assert validate_command_args({"replicas": 3}, schema) == []

def test_coercion_preserves_original_guess_as_advisory():
    t = TriageDecision(**_triage(IncidentType.code_bug, 0.55))
    assert t.incident_type is IncidentType.unknown          # routing reads the safe value
    assert t.llm_incident_type_raw is IncidentType.code_bug # audit keeps what it thought

from operator import add
from datetime import datetime, timezone
from incidentiq.state import IncidentState, IncidentStatus, IncidentContext
from incidentiq.errors import TypedError


def _min_state() -> IncidentState:
    return IncidentState(
        incident_id="inc-1", status=IncidentStatus.created, raw_payload={"a": 1},
        alertmanager_fingerprint="fp-1",
        incident_context=IncidentContext(
            service="cart", alert_name="HighCpu", summary="cpu hot",
            starts_at=datetime.now(timezone.utc),
        ),
    )


def test_incident_state_round_trips():
    s = _min_state()
    # rca_report is None, so no fail-closed context is needed to re-validate
    assert IncidentState.model_validate(s.model_dump()) == s


def test_reducer_metadata_survives_pydantic():
    # the `add` reducer is attached for LangGraph and not stripped by Pydantic
    assert add in IncidentState.model_fields["errors"].metadata
    assert add in IncidentState.model_fields["trace"].metadata


def test_additive_reducer_concatenates():
    e1 = TypedError(node="a", kind="invalid_json", reason="x", ts=datetime.now(timezone.utc))
    e2 = TypedError(node="b", kind="llm_timeout", reason="y", ts=datetime.now(timezone.utc))
    assert add([e1], [e2]) == [e1, e2]   # what LangGraph does when two nodes both append