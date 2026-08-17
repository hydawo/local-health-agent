"""Query agent.

`tools.py` exposes the tool contract — `query_healthkit`, `get_lab_trend`,
`get_workouts`, `search_records` — implemented on top of `store/queries.py` so
the model and the CLI cannot report different numbers. `orchestrator.py`
hand-rolls the tool-calling loop directly against the model API; no
orchestration framework (plan §4).

`backends.py` holds the two tiers: local Ollama (the default and the product)
and the opt-in Anthropic cloud tier. The loop is identical for both; only the
wire format differs.
"""

from . import backends, guardrail
from .backends import Backend, CloudBackend, CloudUnavailable, LocalBackend
from .orchestrator import Answer, Orchestrator, ToolCallRecord
from .tools import ToolContext

__all__ = [
    "Answer", "Backend", "CloudBackend", "CloudUnavailable", "LocalBackend",
    "Orchestrator", "ToolCallRecord", "ToolContext", "backends", "guardrail",
]
