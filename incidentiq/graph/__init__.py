"""Application layer: the LangGraph workflow that wires the agent nodes together.

Orchestration only — this package knows the nodes (agents/) and the domain (state.py),
but the nodes never import from here. Dependencies point inward (Domain ← Application).
See docs/AGENT_ORCHESTRATION.md §1 for the node/edge map.
"""
