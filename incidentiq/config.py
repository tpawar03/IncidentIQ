"""Calibrated parameters — PLACEHOLDERS until the confidence-calibration task (MF-1).

Decision #1: confidence is composite and signal-derived. The weights and gate
thresholds below are chosen by fitting the golden-set reliability curve
(TASKS.md → "Confidence calibration"), NOT by intuition, and are NEVER written as
literals inside agent code. One import site = recalibration is a one-line change.
"""

# --- Composite-confidence weights (AGENT_ORCHESTRATION §2.2) ---
# CALIBRATION PLACEHOLDER. Base score = W_SELF*agreement + W_RET*evidence_strength.
W_SELF: float = 0.5    # weight on self-consistency agreement
W_RET: float = 0.5     # weight on retrieval evidence strength

# --- Confidence gate thresholds (AGENT_ORCHESTRATION §2.2) ---
# CALIBRATION PLACEHOLDER.
RCA_ESCALATE_BELOW: float = 0.65     # Gate A: RCA confidence < this → escalation
TRIAGE_UNKNOWN_BELOW: float = 0.70   # Gate B: triage confidence < this → unknown

# --- Self-consistency fan-out (decision #1) ---
RCA_SAMPLES: int = 3                 # N generations per incident; vote on root_service

# --- Token budget for the RCA prompt (FR-25) ---
# Qwen3-8B window is large, but we keep a safe working ceiling and reserve headroom
# for the JSON completion so the prompt + answer never overflow.
RCA_PROMPT_TOKEN_BUDGET: int = 6000