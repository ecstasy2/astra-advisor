#!/usr/bin/env python3
"""Run one bounded Codex task headlessly and report it in the orchestration evidence format.

Mirror of claude_bridge.py for a Claude Code parent: the parent (or a cheap Claude
subagent acting as relay) invokes this script instead of composing `codex exec` by hand.
The script:

1. reads the task prompt from stdin (never argv);
2. runs `codex exec --json ...` with an explicit model, reasoning effort, and sandbox;
3. writes the raw JSONL event stream to --out (source of truth) and the last message
   to <out>.last.md;
4. looks up the session rollout file to report the *observed* model, effort, and
   sandbox policy (Codex's event stream does not echo them);
5. writes a cost-receipt call record next to it (<out>.call.json) for cost_receipt.py;
6. prints a verbatim `CODEX RESULT` block for the relay to pass on unchanged.

Standard library only. Exit code mirrors `codex`'s exit code; a completed run whose
turn failed exits 1.
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

# Effort sets as listed by the live collaboration.spawn_agent schema on 2026-09-06.
CODEX_MODELS: dict[str, tuple[str, ...]] = {
    "gpt-6-astra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
}
MODES = {
    # Reviewer: Codex sandbox read-only; the OS-level sandbox blocks writes.
    "review": ["-s", "read-only"],
    # Implementer: writes confined to the workspace; approvals never prompt (headless).
    "implement": ["-s", "workspace-write"],
}
REVIEW_PREAMBLE = (
    "REVIEW CONTRACT: You are a fresh, read-only reviewer for another agent's change set. "
    "Never modify files (the sandbox is read-only). Inspect the diff and evidence below, run only "
    "read-only inspection, and end your reply with exactly this block:\n"
    "ORCH REVIEW\nVERDICT: ship | fix-first | rethink\nREASON: <evidence-based reason>\n"
    "FINDINGS: <precise findings with file references, or none>\nRESIDUAL RISK: <remaining risk or none>\n\n"
    "TASK:\n"
)
USAGE_SOURCE = "codex exec --json turn.completed.usage (headless run; one invocation = one atomic record summing its turns)"
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


class BridgeError(ValueError):
    """Raised when a request cannot be dispatched with enforceable controls."""


def build_command(
    *,
    model: str,
    effort: str,
    mode: str,
    resume: str | None,
    cwd: str | None,
    last_message_path: str,
) -> list[str]:
    if model not in CODEX_MODELS:
        raise BridgeError(f"model {model!r} is not in the bridge allowlist {sorted(CODEX_MODELS)}")
    if effort not in CODEX_MODELS[model]:
        raise BridgeError(f"effort {effort!r} is not supported for {model} (choose from {list(CODEX_MODELS[model])})")
    if mode not in MODES:
        raise BridgeError(f"mode {mode!r} must be one of {sorted(MODES)}")
    argv = ["codex", "exec"]
    if resume:
        argv += ["resume", resume]
    argv += [
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", "mcp_servers={}",
        "-c", 'approval_policy="never"',
        "--skip-git-repo-check",
        "--json",
        "-o", last_message_path,
    ]
    if resume is None:
        # `codex exec resume` accepts neither --sandbox nor --cd; the session keeps its own.
        argv += MODES[mode]
        if cwd:
            argv += ["-C", cwd]
    argv.append("-")  # prompt from stdin
    return argv


def parse_events(text: str) -> dict[str, Any]:
    """Reduce a codex --json event stream to thread id, usage, final message, errors."""
    thread_id: str | None = None
    usage_total: dict[str, int] = {}
    turns = 0
    last_message: str | None = None
    errors: list[str] = []
    failed = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "turn.completed":
            turns += 1
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage_total[key] = usage_total.get(key, 0) + value
        elif kind == "turn.failed":
            failed = True
            errors.append(json.dumps(event.get("error")))
        elif kind == "error":
            errors.append(str(event.get("message")))
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                last_message = item.get("text")
            elif item.get("type") == "error":
                errors.append(str(item.get("message")))
    return {
        "thread_id": thread_id,
        "usage": usage_total if turns else None,
        "turns": turns,
        "last_message": last_message,
        "errors": errors,
        "failed": failed,
    }


def observed_from_rollout(thread_id: str | None, sessions_dir: Path = DEFAULT_SESSIONS_DIR) -> dict[str, Any]:
    """Read model / effort / sandbox from the session rollout file (the only place Codex records them)."""
    result: dict[str, Any] = {"model": None, "effort": None, "sandbox": None, "source": None}
    if not thread_id or not sessions_dir.is_dir():
        return result
    matches = sorted(sessions_dir.glob(f"**/rollout-*{thread_id}*.jsonl"))
    if not matches:
        return result
    path = matches[-1]
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "turn_context":
                    continue
                payload = event.get("payload") or {}
                result["model"] = payload.get("model") or result["model"]
                result["effort"] = payload.get("effort") or payload.get("reasoning_effort") or result["effort"]
                sandbox = payload.get("sandbox_policy")
                if isinstance(sandbox, dict):
                    result["sandbox"] = sandbox.get("type") or result["sandbox"]
    except OSError:
        return result
    result["source"] = str(path)
    return result


def result_to_call(
    parsed: dict[str, Any],
    *,
    agent_id: str,
    call_id: str,
    requested_model: str,
    requested_effort: str,
    observed: dict[str, Any],
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "call_id": call_id,
        "agent_id": agent_id,
        "model": observed.get("model") or requested_model,
        "effort": observed.get("effort") or requested_effort,
        "aggregation": "atomic",
        "context": "standard",
        "context_source": "assumed",
        "service_tier": "standard",
        "service_tier_source": "assumed",
    }
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        call["usage"] = {"kind": "unavailable", "source": USAGE_SOURCE, "reason": "no turn.completed usage event in the stream"}
        return call
    mapped: dict[str, Any] = {"kind": "observed", "source": USAGE_SOURCE}
    reasons: list[str] = []
    input_tokens = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens")
    output = usage.get("output_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    cache_write = usage.get("cache_write_input_tokens", 0)
    if isinstance(input_tokens, int) and isinstance(cached, int):
        mapped["input_tokens"] = input_tokens
        mapped["cached_input_tokens"] = min(cached, input_tokens)
    else:
        reasons.append("input/cached counts missing")
    if isinstance(output, int):
        mapped["output_tokens"] = output
        if isinstance(reasoning, int) and reasoning <= output:
            mapped["reasoning_tokens"] = reasoning
    else:
        reasons.append("output_tokens missing")
    if isinstance(cache_write, int) and cache_write > 0:
        reasons.append(
            f"{cache_write} cache_write_input_tokens reported without a 5m/1h split or a published OpenAI write rate; "
            "they are priced at the base input rate"
        )
    if reasons:
        mapped["kind"] = "partial" if any(key.endswith("_tokens") for key in mapped) else "unavailable"
        mapped["reason"] = "; ".join(reasons)
    call["usage"] = mapped
    return call


def render_result_block(
    parsed: dict[str, Any],
    *,
    requested_model: str,
    requested_effort: str,
    mode: str,
    observed: dict[str, Any],
    raw_path: str,
    exit_code: int,
) -> str:
    if parsed.get("failed") or exit_code != 0:
        status = f"failed (exit {exit_code}; {'; '.join(parsed.get('errors') or []) or 'no error detail'})"
    else:
        status = "completed"
    source = observed.get("source") or "unavailable (no rollout file found for this thread)"
    lines = [
        "CODEX RESULT",
        f"status: {status}",
        f"thread_id: {parsed.get('thread_id') or 'unavailable'}",
        f"requested: {requested_model} / {requested_effort} (mode={mode})",
        f"observed_model: {observed.get('model') or 'unobservable'}",
        f"observed_effort: {observed.get('effort') or 'unobservable'}",
        f"observed_sandbox: {observed.get('sandbox') or 'unobservable'}",
        f"evidence: {source}",
        "usage: " + json.dumps(parsed.get("usage"), sort_keys=True),
        f"raw_events: {raw_path}",
        "result:",
        parsed.get("last_message") or "",
    ]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, choices=sorted(CODEX_MODELS))
    parser.add_argument("--effort", required=True, help="reasoning effort supported by the chosen model")
    parser.add_argument("--mode", required=True, choices=sorted(MODES))
    parser.add_argument("--agent-id", required=True, help="agent identity for the cost receipt roster")
    parser.add_argument("--call-id", default=None, help="unique call id (default: <agent-id>/codex/<thread id>)")
    parser.add_argument("--out", required=True, type=Path, help="where to write the raw JSONL event stream")
    parser.add_argument("--resume", default=None, help="codex thread id to continue (follow-up tasks)")
    parser.add_argument("--cwd", default=None, help="working directory for codex (default: current)")
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("codex_bridge: empty prompt on stdin", file=sys.stderr)
        return 2
    last_path = args.out.with_suffix(".last.md")
    try:
        command = build_command(
            model=args.model, effort=args.effort, mode=args.mode, resume=args.resume,
            cwd=args.cwd, last_message_path=str(last_path),
        )
    except BridgeError as exc:
        print(f"codex_bridge: {exc}", file=sys.stderr)
        return 2
    if shutil.which("codex") is None:
        print("codex_bridge: `codex` executable not found on PATH", file=sys.stderr)
        return 127
    if args.mode == "review":
        prompt = REVIEW_PREAMBLE + prompt

    args.out.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command, input=prompt, cwd=args.cwd, env=dict(os.environ),
        capture_output=True, text=True, check=False,
    )
    args.out.write_text(completed.stdout, encoding="utf-8")
    parsed = parse_events(completed.stdout)
    if parsed["last_message"] is None and last_path.is_file():
        parsed["last_message"] = last_path.read_text(encoding="utf-8")
    if completed.returncode != 0 and completed.stderr.strip():
        parsed["errors"].append(completed.stderr.strip()[-1500:])
    observed = observed_from_rollout(parsed["thread_id"], Path(args.sessions_dir))

    call_id = args.call_id or f"{args.agent_id}/codex/{parsed['thread_id'] or 'no-thread'}"
    call = result_to_call(
        parsed, agent_id=args.agent_id, call_id=call_id,
        requested_model=args.model, requested_effort=args.effort, observed=observed,
    )
    args.out.with_suffix(".call.json").write_text(json.dumps(call, indent=2, sort_keys=True), encoding="utf-8")

    sys.stdout.write(
        render_result_block(
            parsed, requested_model=args.model, requested_effort=args.effort, mode=args.mode,
            observed=observed, raw_path=str(args.out), exit_code=completed.returncode,
        )
    )
    if completed.returncode != 0:
        return completed.returncode
    return 1 if parsed["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
