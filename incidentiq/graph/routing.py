"""Deterministic routing functions — the confidence gates (decision #4).

TASK — to build here, per docs/AGENT_ORCHESTRATION.md §2.2:
  - Gate A (RCA): confidence < config.RCA_ESCALATE_BELOW  → escalation.
  - Gate B (triage): confidence < config.TRIAGE_UNKNOWN_BELOW → unknown (no commands).
  - Plain functions of IncidentState → next-node name; NO LLM calls, NO literals
    (read thresholds from incidentiq.config so calibration stays a one-line change, MF-1).

Nothing implemented yet — the thresholds already live in config.py waiting to be wired.
"""
