# Orchestration (Claude Code parent) — operations

Operational detail behind the short skill. Mirror of the Codex plugin's
[operations reference](../../../../plugins/astra-advisor/skills/orchestration/references/operations.md);
where this file is silent, that one governs (receipt input contract, evidence rules,
Claude model guidance, selection rubric).

## Locating the scripts

The skill is normally installed as a symlink into `~/.claude/skills/`. Resolve the real
path first, then the repository root is three levels up from the skill directory:

~~~sh
SKILL_DIR="$(cd "$(dirname "$(readlink -f ~/.claude/skills/orchestration-claude/SKILL.md)")" && pwd)"
REPO="$(cd "$SKILL_DIR/../../.." && pwd)"
SCRIPTS="$REPO/plugins/astra-advisor/scripts"    # codex_bridge.py, claude_bridge.py, cost_receipt.py
PRICING="$REPO/plugins/astra-advisor/pricing/2026-09-06.json"
~~~

## Native lane: the Agent tool

| Control | Available | Notes |
| --- | --- | --- |
| Model | yes: `model: opus \| sonnet \| haiku \| fable` | aliases resolve to the current generation (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`, `claude-fable-5-1`) |
| Effort | no | report `unobservable` |
| Read-only | partial | `subagent_type: Explore` has no Write/Edit but keeps Bash; not enforced |
| Follow-up | yes | `SendMessage` to the agent id keeps its context |
| Usage / cost | no | record an explicit `unavailable` call per delegate |
| Observed model echo | no | requested only; say so |

Bound each delegate exactly as the Codex skill does: owned files, interfaces, expected
return, and "do not edit outside that boundary". All agents share the working tree.

## Bridge lane: `codex_bridge.py`

~~~text
printf '%s' "<prompt>" | python3 $SCRIPTS/codex_bridge.py \
  --model gpt-5.6-terra --effort high --mode implement|review \
  --agent-id <agent id> --out .orch/<task_name>.jsonl \
  [--resume <codex thread id>] [--cwd <repo root>]
~~~

| Model | Efforts (live spawn schema, 2026-09-06) | $/M in/out | Fit |
| --- | --- | --- | --- |
| `gpt-6-astra` | low–ultra | 10 / 50 | Fable-tier price; only for a user-requested top-tier adversarial review |
| `gpt-5.6-sol` | low–ultra | 4 / 20 (promotional) | high-risk implementation or strong independent review |
| `gpt-5.6-terra` | low–ultra | 2 / 12 | default OpenAI implementer/reviewer (Sonnet-priced) |
| `gpt-5.6-luna` | low–max | 0.2 / 1.2 | cheap bounded work, small-diff review, relay |

What the script enforces and reports:

- `--mode review` → `codex exec -s read-only`; the OS sandbox blocks writes. The
  `ORCH REVIEW` contract is prepended to the prompt (codex has no system-prompt flag).
- `--mode implement` → `-s workspace-write`, `approval_policy="never"` (headless never
  prompts; a blocked action fails instead of hanging).
- `--skip-git-repo-check` always (codex refuses untrusted non-git dirs otherwise);
  `mcp_servers={}` always (no MCP startup cost or auth stalls).
- Prompt on stdin only; `-` positional tells codex to read it.
- `--resume <thread_id>` continues a thread; `codex exec resume` accepts neither
  `--sandbox` nor `--cd`, so the session keeps its original sandbox and cwd.
- Evidence: Codex's `--json` stream carries `thread.started.thread_id` and
  `turn.completed.usage` but no model echo. The script reads
  `~/.codex/sessions/**/rollout-*<thread>.jsonl` `turn_context` for the **observed**
  `model`, `effort`, and `sandbox_policy.type` and prints the file path as `evidence:`.
  If no rollout file is found, fields are `unobservable`.
- Receipt mapping: `input_tokens` total, `cached_input_tokens` subset,
  `reasoning_output_tokens` → `reasoning_tokens`; non-zero `cache_write_input_tokens`
  cannot be priced (no 5m/1h split, no OpenAI write rate) → the call is `partial` with
  the reason stated. One invocation = one atomic record.
- Exit code mirrors codex; `turn.failed` → exit 1 with the error in `status:`.

Hazards: the parent's `Bash` call blocks while codex runs — for long tasks use
`run_in_background` or a `haiku` relay delegate; codex shares the working tree, so
`implement` mode edits are live immediately; a relay delegate must return the block
verbatim and the parent must read `--out` and `<out>.call.json` as the source of truth.

## Lifecycle blocks

~~~text
ORCH DELEGATE <name>
task: <bounded deliverable and owned files>
requested: <native: sonnet / unobservable> | <bridge: gpt-5.6-terra / high (mode=review)>
reason: <why this work warrants this selection>

ORCH RESULT <name> / <agent id or codex thread id or unavailable>
status: <completed, failed, interrupted, or blocked; actual evidence>
requested: <as above>
observed: <native: unobservable> | <bridge: model / effort / sandbox from rollout>
evidence: <"Agent tool result only" | rollout file path + raw events path>
~~~

## Selection rubric (mirrored)

Same shape as the Codex skill's rubric with the lanes swapped. Overridable; state the
reason in `ORCH ROUTE risk:`.

| Work shape | Risk | Default | Why |
| --- | --- | --- | --- |
| Read, summarize, locate | low | native `haiku`, or bridge `gpt-5.6-luna` / low for code-heavy input | cheapest |
| Bounded mechanical edit with tests | low | native `haiku` or `sonnet`; bridge `gpt-5.6-luna` / medium | tests verify |
| Bounded feature slice, interfaces fixed | medium | native `sonnet`; bridge `gpt-5.6-terra` / high when a second family in the diff history helps | balanced |
| Cross-cutting / architecture-sensitive | high | native `opus`; bridge `gpt-5.6-sol` / xhigh | capability first |
| Fresh review, small diff | any | bridge `gpt-5.6-luna` / high (enforced read-only) or native `Explore` on `haiku` | cheap independent eyes |
| Fresh review, substantial or risky diff | high | bridge `gpt-5.6-terra` / xhigh or `gpt-5.6-sol` / xhigh (enforced read-only, other family) | strongest independent evidence |
| Second review after `fix-first` | high | the other family from the first reviewer | uncorrelated blind spots |

Tie-breakers: enforced read-only exists only on the bridge lane here, so route a
review there when isolation matters; prefer native Claude when the task needs the
parent's exact toolset or when cost cannot be observed anyway; never bridge what the
parent finishes faster.

## Receipts

Run `python3 $SCRIPTS/cost_receipt.py INPUT.json --baseline <parent model id>`. The
comparison reprices the same observed tokens at the parent's rates (`baseline_model` in
the JSON). Because native Claude usage is unobservable in Claude Code, a receipt with
any native call is `partial`; say so plainly, and never fill in zeros.
