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


# --- Triage rule-prior tuning (decision #4) ---
# A deploy within this window strengthens a deploy-correlated prior (code_bug / config).
RECENT_DEPLOY_MINUTES: int = 60

# --- Triage hybrid-confidence knobs (decision #4) — MF-1 placeholders ---
TRIAGE_AGREE_BONUS: float = 0.10       # rule↔LLM concur → corroboration boost
TRIAGE_DISAGREE_PENALTY: float = 0.30  # rule↔LLM conflict → drop toward the unknown gate

# --- AST code retriever (Task 13) ---
# Repo-relative in dev; the deployed container mounts /repos/cache over this path (FR-26).
AST_CLONE_CACHE_ROOT: str = ".cache/repos"
AST_CLONE_TIMEOUT_SECONDS: float = 20.0   # per git subprocess call (fetch, then checkout)

