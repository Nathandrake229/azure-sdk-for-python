# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
"""Sample 23 — In-Container Timing Traces (stdout waterfall contract).

Shows how to emit the two single-line JSON stdout records that the Foundry
hosted-agent **latency-benchmarking skill** harvests to build the *in-container*
layer of its waterfall report:

* ``STARTUP_TIMING {json}`` — once at startup: cold container-readiness legs
  (SDK import + host construction).
* ``AGENT_TIMING {json}``   — once per request: in-container request legs
  (admission → first event → terminal → total).

Everything here uses only **public** hooks — module-level ``perf_counter``
stamps and marks inside your own handler — so any agent can adopt the contract
without touching the SDK internals. Emit is a single ``print(..., flush=True)``
(~18 µs), gated behind ``SDK_TIMING=1`` so production pays nothing.

Retrieval is off the request path: the skill reads container **stdout** via
``azd ai agent monitor <agent> --session-id <x-agent-session-id> --type console``
and parses the ``AGENT_TIMING`` / ``STARTUP_TIMING`` lines, then aggregates
p50/p95 per leg (cold/warm). Any subset of the leg fields is accepted, so a
partially-instrumented agent still works. On Foundry Hosted Agents the
``AGENT_*`` / ``FOUNDRY_*`` env names are reserved, hence the ``SDK_*`` gate.

Store-internal legs (``prefetch_ms`` conversation-history read, ``persist_ms``
response write) live inside the SDK and are not visible to handler code, so this
user-space example omits them (the contract is null-safe); an SDK that marks them
would simply add the fields.

Usage::

    SDK_TIMING=1 python sample_23_in_container_timing.py
    # -> STARTUP_TIMING {"import_ms": ..., "construct_ms": ..., "total_startup_ms": ...}

    curl -N -X POST http://localhost:8088/responses \
        -H "Content-Type: application/json" \
        -d '{"model": "echo", "input": "hello", "stream": true}'
    # (server stdout) AGENT_TIMING {"req_index": 0, "container_cold": true,
    #   "admission_ms": ..., "to_first_event_ms": ..., "first_to_terminal_ms": ...,
    #   "total_container_ms": ...}
"""

import time

# Stamp T0 BEFORE the (heavy) SDK import so ``import_ms`` captures it.
_T0_NS = time.perf_counter_ns()

import asyncio
import contextvars
import itertools
import json
import os

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)

_IMPORTED_NS = time.perf_counter_ns()

# --------------------------------------------------------------------------- #
# Minimal, self-contained timing helper (the portable stdout contract).
# Gate on SDK_TIMING=1. Marks are perf_counter_ns kept in a ContextVar so each
# concurrent request has its own isolated set.
# --------------------------------------------------------------------------- #
_ENABLED = os.environ.get("SDK_TIMING", "") == "1"
_REQ_MARKS: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar("req_marks", default=None)
_REQ_COUNTER = itertools.count()


def _begin() -> None:
    if _ENABLED:
        _REQ_MARKS.set({"start": time.perf_counter_ns()})


def _mark(name: str) -> None:
    marks = _REQ_MARKS.get()
    if _ENABLED and marks is not None and name not in marks:
        marks[name] = time.perf_counter_ns()


def _emit_request() -> None:
    """Print one ``AGENT_TIMING`` line for the current request (best-effort)."""
    if not _ENABLED:
        return
    marks = _REQ_MARKS.get()
    if not marks:
        return

    def ms(a: str, b: str) -> "float | None":
        return round((marks[b] - marks[a]) / 1e6, 3) if a in marks and b in marks else None

    req_index = next(_REQ_COUNTER)
    record = {
        "req_index": req_index,
        "container_cold": req_index == 0,
        "protocol": "responses",
        "admission_ms": ms("start", "admission"),
        "to_first_event_ms": ms("admission", "first_event"),
        "first_to_terminal_ms": ms("first_event", "terminal"),
        "total_container_ms": ms("start", "terminal"),
    }
    print("AGENT_TIMING " + json.dumps(record), flush=True)


def _emit_startup() -> None:
    """Print the one-shot ``STARTUP_TIMING`` line (import + construct legs)."""
    if not _ENABLED:
        return

    def ms(a: int, b: int) -> float:
        return round((b - a) / 1e6, 3)

    record = {
        "import_ms": ms(_T0_NS, _IMPORTED_NS),
        "construct_ms": ms(_IMPORTED_NS, _CONSTRUCTED_NS),
        "total_startup_ms": ms(_T0_NS, _CONSTRUCTED_NS),
    }
    print("STARTUP_TIMING " + json.dumps(record), flush=True)


# --------------------------------------------------------------------------- #
# The agent — a plain streaming echo, instrumented with the marks above.
# --------------------------------------------------------------------------- #
app = ResponsesAgentServerHost()
# Host construction is the second cold-start leg; stamp right after it.
_CONSTRUCTED_NS = time.perf_counter_ns()


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    """Stream the echoed input token-by-token, emitting AGENT_TIMING per request."""
    _begin()
    user_text = await context.get_input_text() or "world"
    _mark("admission")

    async def generate_tokens():
        first = True
        for token in [f"Echo: {user_text}"]:
            if first:
                _mark("first_event")
                first = False
            yield token
        _mark("terminal")
        _emit_request()

    return TextResponse(context, request, text=generate_tokens())


def main() -> None:
    _emit_startup()
    app.run()


if __name__ == "__main__":
    main()
