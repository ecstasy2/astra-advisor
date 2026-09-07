#!/usr/bin/env python3
"""Run one bounded Claude Code task headlessly and report it in Astra's evidence format.

A cheap native Codex subagent (the "bridge") invokes this script instead of composing
`claude -p` by hand. The script:

1. reads the task prompt from stdin (never argv, so variadic flags cannot swallow it);
2. runs `claude -p --output-format json ...` with the explicit model and effort;
3. writes the raw result JSON to --out (source of truth, unparaphrased);
4. writes a cost-receipt call record next to it (<out>.call.json) mapped to the
   cost_receipt.py schema, including Anthropic 5m/1h cache-write tokens;
5. prints a verbatim `CLAUDE RESULT` block for the bridge to relay unchanged.

Standard library only. Exit code mirrors `claude`'s exit code; a successful run
whose result reports is_error exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

BRIDGE_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The Anthropic API rejects `effort` for these models; Claude Code accepts the flag but
# cannot enforce it, so the bridge refuses to record a requested effort for them.
NO_EFFORT_MODELS = ("claude-haiku-4-5",)
MODES = {
    # Reviewer: plan mode blocks every mutating tool; nothing can be approved headlessly.
    "review": ["--permission-mode", "plan"],
    # Implementer: file edits auto-accepted inside the workspace; Bash needs explicit allow rules.
    "implement": ["--permission-mode", "acceptEdits"],
}
REVIEW_SYSTEM_PROMPT = (
    "You are a fresh, read-only reviewer for another agent's change set. Never modify files. "
    "Inspect the diff and evidence you were given, run only read-only inspection, and end your "
    "reply with exactly this block:\n"
    "ASTRA REVIEW\nVERDICT: ship | fix-first | rethink\nREASON: <evidence-based reason>\n"
    "FINDINGS: <precise findings with file references, or none>\nRESIDUAL RISK: <remaining risk or none>"
)
USAGE_SOURCE = "claude -p --output-format json result.usage (Claude Code headless run; one invocation = one atomic record summing its internal API iterations)"


class BridgeError(ValueError):
    """Raised when a request cannot be dispatched with enforceable controls."""


def child_env(env: dict[str, str]) -> dict[str, str]:
    """Environment for the nested `claude` process (drop nested-session markers)."""
    child = dict(env)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        child.pop(key, None)
    return child


def build_command(
    *,
    model: str,
    effort: str | None,
    mode: str,
    resume: str | None,
    max_budget_usd: str | None,
    extra_allowed_tools: list[str],
) -> list[str]:
    if model not in BRIDGE_MODELS:
        raise BridgeError(f"model {model!r} is not in the bridge allowlist {list(BRIDGE_MODELS)}")
    if effort is not None and effort not in EFFORTS:
        raise BridgeError(f"effort {effort!r} is not supported by `claude --effort` (choose from {list(EFFORTS)})")
    if effort is not None and model in NO_EFFORT_MODELS:
        raise BridgeError(
            f"model {model!r} does not accept an effort level at the API; omit --effort and report it as n/a"
        )
    if mode not in MODES:
        raise BridgeError(f"mode {mode!r} must be one of {sorted(MODES)}")
    argv = ["claude", "-p", "--output-format", "json", "--permission-prompts", "none", "--model", model]
    argv += MODES[mode]
    if effort is not None:
        argv += ["--effort", effort]
    if resume:
        argv += ["--resume", resume]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    if mode == "review":
        argv += ["--append-system-prompt", REVIEW_SYSTEM_PROMPT]
    if extra_allowed_tools:
        if mode == "review":
            raise BridgeError("review mode is read-only; extra allowed tools are not permitted")
        argv += ["--allowedTools", *extra_allowed_tools]
    return argv


def _observed_model(result: dict[str, Any]) -> str:
    model_usage = result.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return "unobservable"
    canonical = {
        (entry.get("canonicalModel") or key) if isinstance(entry, dict) else key
        for key, entry in model_usage.items()
    }
    if len(canonical) > 1:
        raise BridgeError(f"result reports more than one model ({sorted(canonical)}); split it before pricing")
    return canonical.pop()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def result_to_call(result: dict[str, Any], *, agent_id: str, call_id: str, effort: str | None) -> dict[str, Any]:
    """Map a Claude Code JSON result onto one cost_receipt.py atomic call."""
    model = _observed_model(result)
    raw_usage = result.get("usage")
    tier = raw_usage.get("service_tier") if isinstance(raw_usage, dict) else None
    call: dict[str, Any] = {
        "call_id": call_id,
        "agent_id": agent_id,
        "model": model,
        "effort": effort,
        "aggregation": "atomic",
        "context": "standard",
        "context_source": "assumed",
        "service_tier": tier if isinstance(tier, str) else "standard",
        "service_tier_source": "observed" if isinstance(tier, str) else "assumed",
    }
    if not isinstance(raw_usage, dict):
        call["usage"] = {
            "kind": "unavailable",
            "source": USAGE_SOURCE,
            "reason": "claude result JSON carried no usage object",
        }
        return call

    uncached = _int_or_none(raw_usage.get("input_tokens"))
    cache_read = _int_or_none(raw_usage.get("cache_read_input_tokens"))
    cache_creation = _int_or_none(raw_usage.get("cache_creation_input_tokens"))
    output = _int_or_none(raw_usage.get("output_tokens"))
    details = raw_usage.get("output_tokens_details")
    thinking = _int_or_none(details.get("thinking_tokens")) if isinstance(details, dict) else None
    breakdown = raw_usage.get("cache_creation")
    write_5m = _int_or_none(breakdown.get("ephemeral_5m_input_tokens")) if isinstance(breakdown, dict) else None
    write_1h = _int_or_none(breakdown.get("ephemeral_1h_input_tokens")) if isinstance(breakdown, dict) else None

    usage: dict[str, Any] = {"kind": "observed", "source": USAGE_SOURCE}
    reasons: list[str] = []
    if uncached is None or cache_read is None or cache_creation is None:
        reasons.append("input/cache_read/cache_creation counts incomplete")
    else:
        usage["input_tokens"] = uncached + cache_read + cache_creation
        usage["cached_input_tokens"] = cache_read
        if write_5m is not None and write_1h is not None and write_5m + write_1h == cache_creation:
            usage["cache_write_5m_input_tokens"] = write_5m
            usage["cache_write_1h_input_tokens"] = write_1h
        elif cache_creation > 0:
            reasons.append(
                f"{cache_creation} cache-write tokens are included in input_tokens but the 5m/1h split was not "
                "reported, so they are priced at the base input rate (understates the routed price)"
            )
        else:
            usage["cache_write_5m_input_tokens"] = 0
            usage["cache_write_1h_input_tokens"] = 0
    if output is None:
        reasons.append("output_tokens missing")
    else:
        usage["output_tokens"] = output
        if thinking is not None and thinking <= output:
            usage["reasoning_tokens"] = thinking
    if reasons:
        usage["kind"] = "partial" if any(key.endswith("_tokens") for key in usage) else "unavailable"
        usage["reason"] = "; ".join(reasons)
    call["usage"] = usage
    return call


def render_result_block(
    result: dict[str, Any], *, requested_model: str, requested_effort: str | None, raw_path: str
) -> str:
    is_error = bool(result.get("is_error"))
    subtype = result.get("subtype") or "unknown"
    status = "completed" if not is_error and subtype == "success" else f"failed ({subtype})"
    try:
        observed = _observed_model(result)
    except BridgeError as exc:
        observed = f"ambiguous ({exc})"
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    breakdown = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    lines = [
        "CLAUDE RESULT",
        f"status: {status}",
        f"session_id: {result.get('session_id', 'unavailable')}",
        f"requested: {requested_model} / {requested_effort or 'n/a'}",
        f"observed_model: {observed}",
        (
            "observed_effort: n/a (this model does not accept an effort level at the API)"
            if requested_model in NO_EFFORT_MODELS
            else "observed_effort: unobservable (Claude Code does not echo --effort in its result JSON)"
        ),
        f"num_turns: {result.get('num_turns', 'unavailable')}",
        "usage: "
        + json.dumps(
            {
                "input_tokens": usage.get("input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_write_5m": breakdown.get("ephemeral_5m_input_tokens"),
                "cache_write_1h": breakdown.get("ephemeral_1h_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "service_tier": usage.get("service_tier"),
            },
            sort_keys=True,
        ),
        f"total_cost_usd (claude code, list basis): {result.get('total_cost_usd', 'unavailable')}",
        f"raw_json: {raw_path}",
        "result:",
        str(result.get("result", "")),
    ]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, choices=BRIDGE_MODELS)
    parser.add_argument("--effort", choices=EFFORTS, default=None)
    parser.add_argument("--mode", required=True, choices=sorted(MODES))
    parser.add_argument("--agent-id", required=True, help="bridge agent identity for the cost receipt roster")
    parser.add_argument("--call-id", default=None, help="unique call id (default: <agent-id>/claude/<session or n>)")
    parser.add_argument("--out", required=True, type=Path, help="where to write the raw claude result JSON")
    parser.add_argument("--resume", default=None, help="claude session id to continue (follow-up tasks)")
    parser.add_argument("--max-budget-usd", default=None, help="hard USD cap passed to claude")
    parser.add_argument(
        "--allow-tool", action="append", default=[], metavar="RULE",
        help="implement mode only: extra --allowedTools rule, e.g. 'Bash(npm test)' (repeatable)",
    )
    parser.add_argument("--cwd", default=None, help="working directory for claude (default: current)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("claude_bridge: empty prompt on stdin", file=sys.stderr)
        return 2
    try:
        command = build_command(
            model=args.model, effort=args.effort, mode=args.mode, resume=args.resume,
            max_budget_usd=args.max_budget_usd, extra_allowed_tools=args.allow_tool,
        )
    except BridgeError as exc:
        print(f"claude_bridge: {exc}", file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        print("claude_bridge: `claude` executable not found on PATH", file=sys.stderr)
        return 127

    completed = subprocess.run(
        command, input=prompt, env=child_env(os.environ), cwd=args.cwd,
        capture_output=True, text=True, check=False,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(completed.stdout, encoding="utf-8")

    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("top-level JSON is not an object")
    except ValueError as exc:
        result = {
            "is_error": True,
            "subtype": f"exit {completed.returncode}; non-JSON output ({exc})",
            "result": (completed.stderr or completed.stdout)[-2000:],
        }

    call_id = args.call_id or f"{args.agent_id}/claude/{result.get('session_id', 'no-session')}"
    try:
        call = result_to_call(result, agent_id=args.agent_id, call_id=call_id, effort=args.effort)
    except BridgeError as exc:
        call = {
            "call_id": call_id, "agent_id": args.agent_id, "model": "ambiguous", "effort": args.effort,
            "aggregation": "atomic", "context": "standard", "context_source": "assumed",
            "service_tier": "standard", "service_tier_source": "assumed",
            "usage": {"kind": "unavailable", "source": USAGE_SOURCE, "reason": str(exc)},
        }
    args.out.with_suffix(".call.json").write_text(json.dumps(call, indent=2, sort_keys=True), encoding="utf-8")

    sys.stdout.write(
        render_result_block(result, requested_model=args.model, requested_effort=args.effort, raw_path=str(args.out))
    )
    if completed.returncode != 0:
        return completed.returncode
    return 1 if result.get("is_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
