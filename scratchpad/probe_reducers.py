from operator import add
from typing import Annotated
from pydantic import BaseModel
from langgraph.graph import StateGraph, START, END


class S(BaseModel):
    status: str                          # no reducer → last-write-wins
    trace: Annotated[list[str], add] = []  # additive → should accumulate


def node_a(state: S) -> dict:
    return {"status": "a", "trace": ["a ran"]}


def node_b(state: S) -> dict:
    return {"status": "b", "trace": ["b ran"]}


g = StateGraph(S)
g.add_node("a", node_a)
g.add_node("b", node_b)
g.add_edge(START, "a")
g.add_edge("a", "b")
g.add_edge("b", END)
app = g.compile()

out = app.invoke(S(status="start", trace=[]))
print(out)