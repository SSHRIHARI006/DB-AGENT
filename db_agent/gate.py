"""Human-in-the-loop gate decision types.

The worker calls a ``pre_execute_hook`` before executing a tool. The hook
returns a :class:`GateDecision`; the worker then proceeds, retries, or aborts
accordingly. Every gated decision carries an ``approved_via`` audit field so
the rollback/query history can distinguish a manually-reviewed risky
mutation from one auto-approved by ``--yes`` during scripting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class GateDecision:
    action: Literal["approve", "deny_retry", "deny_abort"]
    message: str = ""
    approved_via: Literal["manual", "auto_flag"] = field(default="manual")

    @classmethod
    def approve(cls, *, approved_via: Literal["manual", "auto_flag"] = "manual", message: str = "") -> "GateDecision":
        return cls(action="approve", message=message, approved_via=approved_via)

    @classmethod
    def deny_retry(cls, message: str) -> "GateDecision":
        return cls(action="deny_retry", message=message)

    @classmethod
    def deny_abort(cls, message: str) -> "GateDecision":
        return cls(action="deny_abort", message=message)
