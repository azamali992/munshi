"""Lightweight MLflow-based tracing for every agent turn: which agent
handled it, what role the caller had, which tool was called, whether it
needed human approval, how long it took, and whether it errored. The
offline counterpart is eval/run_eval.py; this is the same "agent behaviour
is something you monitor and can regress-test" idea applied to the live app.

Uses MLflow's local file-store backend by default (no server needed) --
fine for a demo/single-machine deployment; a real deployment would point
MLFLOW_TRACKING_URI at a proper backend (Postgres, or a hosted MLflow
server) instead. MLflow 3.x treats the file store as "maintenance mode"
and refuses it unless explicitly opted into, which is what
MLFLOW_ALLOW_FILE_STORE does below.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import mlflow  # noqa: E402  (must come after the env var default above)

_DEFAULT_TRACKING_DIR = os.path.join(os.getcwd(), "mlruns")
_configured = False


def configure_tracking(tracking_dir: Optional[str] = None, experiment: str = "munshi") -> None:
    global _configured
    tracking_dir = tracking_dir or os.environ.get("MLFLOW_TRACKING_DIR", _DEFAULT_TRACKING_DIR)
    mlflow.set_tracking_uri(f"file:{tracking_dir}")
    mlflow.set_experiment(experiment)
    _configured = True


@dataclass
class TurnTrace:
    agent_name: str
    role: str
    user_text: str
    specialist: Optional[str] = None
    tool_called: Optional[str] = None
    required_approval: bool = False
    approval_decision: Optional[str] = None
    error: Optional[str] = None
    response_text: Optional[str] = None


@contextmanager
def trace_turn(agent_name: str, role: str, user_text: str) -> Iterator[TurnTrace]:
    """Wrap one platform turn. Never lets an observability failure break
    the platform itself -- tracing errors are swallowed, not raised."""
    trace = TurnTrace(agent_name=agent_name, role=role, user_text=user_text)
    start = time.monotonic()
    try:
        yield trace
    except Exception as exc:
        trace.error = str(exc)
        raise
    finally:
        latency_ms = (time.monotonic() - start) * 1000
        if not _configured:
            configure_tracking()
        try:
            with mlflow.start_run(run_name=f"{agent_name}-turn"):
                mlflow.log_param("agent", agent_name)
                mlflow.log_param("role", role)
                mlflow.log_param("specialist", trace.specialist or "")
                mlflow.log_metric("latency_ms", latency_ms)
                mlflow.set_tag("tool_called", trace.tool_called or "none")
                mlflow.set_tag("required_approval", str(trace.required_approval))
                mlflow.set_tag("approval_decision", trace.approval_decision or "n/a")
                mlflow.set_tag("error", trace.error or "none")
                mlflow.log_text(user_text, "user_text.txt")
                if trace.response_text:
                    mlflow.log_text(trace.response_text, "response_text.txt")
        except Exception:
            pass
