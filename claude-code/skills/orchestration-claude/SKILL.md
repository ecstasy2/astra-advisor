---
name: orchestration-claude
description: Plan, route, implement, verify, and review substantial work from a Claude Code parent session, delegating to native Claude subagents (Agent tool) and to OpenAI Codex models through a headless codex bridge. Mirror of the Codex astra-advisor orchestration skill.
---

# Orchestration (Claude Code parent)

Act as the architect and acceptance owner. The parent is this Claude Code session on
whatever model the harness reports; the skill never changes it. The parent owns intent,
architecture, decomposition, delegation decisions, parent verification, and acceptance.
Report the parent model as the harness states it and its effort as unobservable unless
the harness exposes one; never invent confirmation.

After capability preflight and before the first implementation or delegation task
call, emit a short, machine-auditable declaration:

~~~text
ORCH ROUTE
parent: <harness-stated model or unobservable> / <effort or unobservable>
delegation: <none, or each native Claude model, or bridge -> <codex model>/<effort>>
risk: <concise, task-specific rationale>
~~~

Read [the operations reference](references/operations.md) before the first delegation.

## Native delegation (Claude models)

Use the `Agent` tool. Each delegate gets an explicit `model` (`opus`, `sonnet`, `haiku`;
`fable` only when the user asks for Fable-tier work), a concrete bounded independent
deliverable, and owned files. `subagent_type: general-purpose` for implementation;
`subagent_type: Explore` for read-mostly review (it lacks Write/Edit but keeps Bash, so
it is not enforced read-only — say so). Keep useful planning, integration, and
verification in the parent while delegates run. Do not duplicate the parent's work in a
delegate. The Agent tool exposes no token usage, model echo, or effort control: report
those as unobservable and record them as explicit `unavailable` receipt calls.

## Codex bridge lane (OpenAI models)

`gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` join the pool through
`scripts/codex_bridge.py` (in `plugins/astra-advisor/scripts/` of the repository this
skill lives in; resolve the skill directory's real path first). The parent runs it
directly with `Bash`, or hands it to a `haiku` relay delegate whose only job is to run
the command and return the `CODEX RESULT` block verbatim. The script runs `codex exec
--json` with an explicit `--model`, `--effort`, and sandbox (`--mode review` = OS-level
read-only sandbox with the `ORCH REVIEW` contract prepended; `--mode implement` =
workspace-write), writes the raw event stream and a receipt-ready call record, and
reads the session rollout file to report the **observed** model, effort, and sandbox.
Use the lane when an OpenAI model is the better fit or when a fresh reviewer from a
different model family adds independent evidence. Requires `codex` on PATH with a
logged-in ChatGPT account; fail the delegation closed if it is missing or the model or
effort is not in the script's allowlist. Never substitute silently.

## Fresh review gate

For a substantial implementation, the parent inspects the complete diff and reruns
the requested checks before starting a fresh review in a new context. The reviewer may
be a native Claude delegate (`Explore`, read-mostly) or a Codex model through the
bridge in `review` mode (enforced read-only). Prefer a different model family from the
implementer. Give it the actual change set and evidence and require:

~~~text
ORCH REVIEW
VERDICT: ship | fix-first | rethink
REASON: <evidence-based reason>
FINDINGS: <precise findings or none>
RESIDUAL RISK: <remaining risk or none>
~~~

Accept only after `ship`. After `fix-first`, the parent fixes, re-verifies, and gets a
new fresh review, preferably from the other model family. Reviewers never fix their own
findings.

## Live delegation and completion receipts

Show a short user-visible lifecycle update for **every** delegation before dispatch
(`ORCH DELEGATE`) and on completion or failure (`ORCH RESULT`): task name, bounded
ownership, requested model/effort, reason; then agent or thread id, actual status,
observed model/effort/sandbox with evidence source, or `unobservable`. A submitted
request is not runtime confirmation.

At the completion of **every task**, emit an `API-EQUIVALENT COST RECEIPT` with
`scripts/cost_receipt.py --baseline <parent claude model id>` (for example
`--baseline claude-sonnet-5`), or a precise unavailable status. Native Claude delegates
and the parent have no observable usage in Claude Code: list them as explicit
`unavailable` calls, so receipts are honestly `partial` unless every in-scope call ran
through the bridge. Never invent tokens, rates, or savings. Unknown is not zero.
