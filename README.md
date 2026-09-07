# Astra Advisor

**GPT-6 Astra plans the work, chooses useful bounded delegation dynamically, and
owns verification and acceptance.**

Astra Advisor is a Codex plugin for capability-routed software delivery. Give Astra
the goal, constraints, and repository context; it decides whether independent work
should run alongside the parent session and chooses a supported subagent model and
effort for each bounded deliverable — a native GPT-5.6 subagent, or a Claude model
(`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`) reached through a cheap
native bridge subagent (0.3.0).

## Cloud limitation

ChatGPT Work cloud `create_thread` must omit `model` and
`thinking`, so it cannot currently promise arbitrary model or effort control. Astra
does not dispatch a model-pinned request there by default. Native Codex subagents are usable
where the current tool schema exposes the needed controls.

## Go deeper

I write [Attention Heads](https://attentionheads.substack.com/) — deep,
evidence-backed writing on AI, cognition, and agentic engineering. The **Agentic
Engineering Field Notes** series covers the craft of using AI. [Subscribe](https://attentionheads.substack.com/subscribe?utm_source=github&utm_medium=readme&utm_campaign=astra-advisor)
to get new posts in your inbox.

## Quick start

Install the plugin in a current Codex CLI or ChatGPT desktop app with plugins
enabled. Start a fresh task after installation and select GPT-6 Astra at any effort
supported by the current Codex host:

~~~sh
codex plugin marketplace add DannyMac180/astra-advisor --ref main
codex plugin add astra-advisor@astra-advisor
~~~

Start a task with:

~~~text
Use $astra-advisor:orchestration to plan, build, verify, and review this work.
~~~

## How routing works

Astra remains the architect and acceptance owner in the primary GPT-6 Astra session
at the effort selected by the user. After capability preflight, Astra records the
parent model and effort as observed or unobservable before implementation or
delegation begins. The skill never changes the parent session.

When delegation helps, Astra uses the exposed generic `collaboration.spawn_agent`
tool with an explicit `model`, `reasoning_effort`, and `fork_turns: none`. It chooses
among `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` from the task's risk,
context, and independent work. There are no predefined role TOMLs, companion
installer, role-to-model mapping, or fixed subagent count cap. Astra gives each
subagent a concrete bounded deliverable and continues useful parent work while it
runs.

Live tool metadata is authoritative. The current documented effort snapshot is:

| Model | Lane | Known efforts |
| --- | --- | --- |
| `gpt-5.6-sol` | native | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-terra` | native | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-luna` | native; also the bridge relay at `low` | `low`, `medium`, `high`, `xhigh`, `max` |
| `gpt-5.5` | native (previous generation, listed by the spawn schema) | `low`, `medium`, `high`, `xhigh` |
| `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5` | bridge | `low`, `medium`, `high`, `xhigh`, `max` |

## Claude bridge lane (0.3.0)

`collaboration.spawn_agent` only accepts the models its live schema lists (on
2026-09-06: Astra, Sol, Terra, Luna, GPT-5.5 — not `gpt-5.3-codex-spark`), so a Claude
model cannot be spawned natively. Instead Astra spawns the cheapest listed model
(`gpt-5.6-luna`) at `low` effort as a bridge whose only job is to run
[`scripts/claude_bridge.py`](plugins/astra-advisor/scripts/claude_bridge.py) and
relay its `CLAUDE RESULT` block verbatim. The script runs Claude Code headlessly
(`claude -p --output-format json --model <id> --effort <level>`), writes the raw
result JSON to disk as the source of truth, and emits a receipt-ready call record with
Anthropic's 5-minute / 1-hour cache-write split. `--mode review` runs Claude in plan
mode (enforced read-only, `ASTRA REVIEW` contract appended); `--mode implement` uses
`acceptEdits` with explicit `--allow-tool` rules. The script never uses
`bypassPermissions`, denies every permission prompt so a headless run cannot hang,
and exits non-zero on failure. Follow-ups reuse Claude's `session_id` via `--resume`.

Requirements: `claude` CLI on PATH, a sandbox that allows outbound network, and the
bridge model present in the live spawn schema. If any is missing the lane fails closed. A bridge holds one native concurrency slot while it waits; for many
Claude workers, one bridge should run several script invocations rather than one
bridge per worker.

Try the script directly (prompt on stdin, never argv):

~~~sh
printf 'Summarize README.md in two sentences.' | python3 plugins/astra-advisor/scripts/claude_bridge.py \
  --model claude-haiku-4-5 --effort low --mode review --agent-id demo --out /tmp/astra/demo.json
~~~

If a selected model, effort, control, or tool is unavailable, conflicting, or
unobservable, Astra fails that delegation closed and reports the limitation. It does
not silently substitute a model, effort, role, or fabricated tool. Chosen values and
runtime-confirmed values are reported separately.

For substantial implementation, Astra inspects the complete diff and reruns the
requested checks, then sends the accumulated change set to a fresh read-only
reviewer. The reviewer can be any of the three native models at a live-supported
effort, or a Claude model through the bridge in review mode. Astra accepts the work only after the reviewer returns `ship`; `fix-first`
requires a new parent verification and fresh review, while `rethink` requires a
revised plan.

## Live visibility and cost receipts (0.2.0)

Every delegation announces its name, bounded task, selected model and reasoning
effort, and selection reason. Its result reports actual status and runtime-observed
settings, or explicitly says those settings are unobservable. These updates also
cover fresh reviewers. A requested setting is not proof of the realized setting.

Every task ends with an API-equivalent cost receipt. When native tools expose token
usage, the receipt estimates its USD price using the versioned snapshot and compares
that same token workload repriced entirely at Astra. It separates whole-task,
delegated-only, and partial coverage. Missing parent or reviewer usage prevents a
whole-task claim. Without observed usage, the receipt says why it is unavailable.

The difference is a **same-token API price comparison**. It does not measure what an
all-Astra run would actually consume, actual net task savings, quality, speed, or a
change to ChatGPT subscription charges or usage credits. No subagents means no
delegation savings. Reasoning effort does not multiply the token price.

The [pricing snapshot](plugins/astra-advisor/pricing/2026-09-06.json) records official
source URLs and standard short-context USD rates per million tokens with per-model
verification dates (OpenAI rows September 4, 2026; Anthropic rows September 6, 2026).
The [2026-09-04 snapshot](plugins/astra-advisor/pricing/2026-09-04.json) is kept for
historical receipts. These are historical estimates; Sol pricing is promotional and
may change. Anthropic cache writes are priced at the
published 1.25x (5-minute) and 2x (1-hour) write rates from the
`cache_write_5m_input_tokens` / `cache_write_1h_input_tokens` fields; a call that
reports writes on a model without write rates is unavailable, not discounted. The
calculator rejects unsupported long-context and service-tier cases instead of assuming
standard rates. It conservatively supports at most 128,000 input tokens per call
unless the model entry carries `max_standard_input_tokens` mirrored from its official
page (Claude Opus 5 / Sonnet 5: 1,000,000; Haiku 4.5: 200,000); these are
implementation support boundaries, not claimed official pricing thresholds.

Try the clearly labeled illustrative workloads (not receipts for your task):

~~~sh
python3 plugins/astra-advisor/scripts/cost_receipt.py plugins/astra-advisor/examples/illustrative-usage.json
python3 plugins/astra-advisor/scripts/cost_receipt.py plugins/astra-advisor/examples/illustrative-bridge-usage.json
sh plugins/astra-advisor/scripts/verify.sh
~~~

The calculator emits JSON and accepts `--pricing PATH` for another verified snapshot.
Its input lists agents and unique atomic calls, usage provenance, coverage assertions,
and explicit pricing eligibility. It validates cached-input and reasoning-output
subsets, refuses overlapping aggregates, and keeps unknown usage separate from zero.
See the [operations reference](plugins/astra-advisor/skills/orchestration/references/operations.md)
for the input contract and receipt policy.

## ChatGPT app tasks

Separate app tasks require an explicit user request. For an explicit Codex app task,
`mcp__codex_app__create_thread` supports `model` and `thinking`; call
`mcp__codex_app__list_projects` first for project targets, use a worktree by default
for Git projects, and use local otherwise. Cloud `create_thread` omits both controls,
so the bounded limitation above applies. Do not fake cloud model control with an API
key, nested CLI, or invented tool; the Claude bridge lane is a separate documented
local lane, not a cloud workaround.

## Updating

~~~sh
codex plugin marketplace upgrade astra-advisor
codex plugin add astra-advisor@astra-advisor
~~~

For local development, install this checkout as a marketplace:

~~~sh
cd /absolute/path/to/astra-advisor
codex plugin marketplace add /absolute/path/to/astra-advisor
codex plugin add astra-advisor@astra-advisor
~~~

For operational details, read
[the orchestration operations reference](plugins/astra-advisor/skills/orchestration/references/operations.md).
