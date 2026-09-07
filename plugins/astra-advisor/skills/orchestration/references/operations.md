# Astra Advisor operations

This reference holds the operational details behind the short orchestration skill.
It describes capability selection and evidence rules; it does not define installed
roles, role files, task lanes, or an installer.

## Parent session

The primary session is GPT-6 Astra at whatever supported effort the user selected.
The invocation is authoritative. Do not require a particular effort, rewrite the
parent configuration, or claim a model/effort pin without runtime evidence. If the
session exposes model and effort metadata and the model is not `gpt-6-astra`, report
that mismatch as a selection prerequisite and do not claim Astra orchestration. If
metadata does not expose the model or effort, report the value as unobservable and
continue within the user's request without inventing confirmation.

After capability preflight and before the first implementation or delegation task
call, record the selected plan:

~~~text
ASTRA ROUTE
parent: <observed model or unobservable> / <observed effort or unobservable>
delegation: <none or each selected model and effort>
risk: <concise, task-specific rationale>
~~~

The declaration is a record of the current decision, not a fixed set of workflow
lanes. Update it only when new evidence changes the plan, and explain that evidence.

## Dynamic native delegation

Use the generic `collaboration.spawn_agent` only if the current environment exposes
that tool and its schema. Select a model and effort for each concrete, bounded,
independent deliverable from the task's risk, context, and available work. Pass the
chosen values explicitly:

~~~text
model: <selected supported model>
reasoning_effort: <selected supported effort>
fork_turns: none
~~~

Include a task name and a message that states the bounded ownership and expected
return. For example, this is one illustrative request shape; the model and effort
must be selected afresh for the actual task:

~~~json
{
  "task_name": "inspect_auth_boundary",
  "message": "Inspect the auth boundary in the owned files. Return findings, exact file references, and the checks you ran; do not edit outside that boundary.",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "max",
  "fork_turns": "none"
}
~~~

The example does not prescribe a model, effort, task name, or number of subagents.
Use the current tool schema for any additional required fields and reject a request
whose selected controls cannot be enforced.

Do not rely on role names, predefined TOMLs, a role-to-model table, or a fixed count
cap. Dispatch only work whose files, interfaces, and acceptance evidence are clear;
keep useful planning, implementation, integration, or verification work in the
parent session while independent subagents run. Avoid assigning the same change or
check to both parent and subagent. Preserve concurrent edits and return each
subagent's actual result and evidence to the parent.

The following is the known capability snapshot for routing. It is guidance for a
selection, not a contract that overrides live tool metadata:

| Model | Lane | Efforts known in the current snapshot |
| --- | --- | --- |
| `gpt-5.6-sol` | native | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-terra` | native | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-luna` | native; also the **bridge relay** at `low` | `low`, `medium`, `high`, `xhigh`, `max` |
| `gpt-5.5` | native (listed by the spawn schema; previous generation) | `low`, `medium`, `high`, `xhigh` |
| `claude-opus-5` | bridge | `low`, `medium`, `high`, `xhigh`, `max` (`claude --effort`) |
| `claude-sonnet-5` | bridge | `low`, `medium`, `high`, `xhigh`, `max` |
| `claude-haiku-4-5` | bridge | `low`, `medium`, `high`, `xhigh`, `max` |

Inspect the current tool metadata when selecting and invoking a subagent. A changed
live capability list wins over this snapshot. If the selected model, effort, explicit
spawn control, or required tool is unavailable, conflicting, or unobservable, fail
the affected delegation closed. Continue safe parent work when possible and report
the limitation; never silently substitute another model, effort, or tool.

## Claude bridge lane

The bridge is the only way a Claude model enters the pool. `collaboration.spawn_agent`
resolves `model` against Codex's own catalog, so a Claude id passed there must fail
closed. Instead:

1. **Preflight.** Read the live `collaboration.spawn_agent` schema and pick the cheapest
   model it lists as the bridge (currently `gpt-5.6-luna`; on 2026-09-06 the schema
   listed `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5` and
   not `gpt-5.3-codex-spark`). Confirm the sandbox permits outbound network (the bridge
   calls the Anthropic API) and `claude` resolves on PATH. Any miss fails the lane
   closed with the reason named.
2. **Spawn the bridge.** `collaboration.spawn_agent` with `model: gpt-5.6-luna`,
   `reasoning_effort: low`, `fork_turns: none`, a `task_name` such as
   `bridge_review_auth_boundary`, and a message that contains the exact command to run
   and the exact prompt to feed it. The bridge holds one of the four concurrency slots
   while it waits.
3. **The bridge runs the installed script** with the prompt on stdin, from the repo
   root, resolving the path relative to this installed reference:
   [`scripts/claude_bridge.py`](../../../scripts/claude_bridge.py).

   ~~~text
   printf '%s' "<prompt>" | python3 <plugin>/scripts/claude_bridge.py \
     --model claude-sonnet-5 --effort high --mode implement|review \
     --agent-id <bridge agent id> --out .astra/<task_name>.json \
     [--resume <claude session_id>] [--max-budget-usd 2.00] [--allow-tool 'Bash(npm test)']
   ~~~

   `--mode review` runs Claude in plan mode with the `ASTRA REVIEW` output contract
   appended to its system prompt; every mutating tool is blocked and
   `--permission-prompts none` denies anything that would ask, so a headless run can
   neither edit nor hang. `--mode implement` runs `acceptEdits` inside the workspace;
   shell commands need explicit `--allow-tool` rules. The script never uses
   `bypassPermissions`. Follow-up turns pass `--resume <session_id>` so Claude keeps
   its context; `followup_task` on the bridge is the native way to trigger that.
4. **Relay verbatim.** The bridge's final answer is the script's `CLAUDE RESULT` block,
   unchanged. It does not paraphrase, fix, summarize, or add work. If the script exits
   non-zero the block carries `status: failed (...)` and the bridge still relays it.
5. **Astra reads the raw JSON** at `--out` (and `<out>.call.json`) as the source of
   truth for the result text, `session_id`, observed model, and usage. The relay is a
   notification, not evidence.

Evidence: Claude Code's result JSON reports `session_id`, `modelUsage[*].canonicalModel`,
`usage.service_tier`, and token usage with the 5m/1h cache-write split. Treat the
canonical model as **observed**. Claude Code does not echo `--effort`, so report the
Claude effort as requested/unobservable, exactly as for a native subagent whose
metadata omits it. A `--mode review` run is enforced read-only by plan mode, so the
skill may claim read-only isolation for it. The bridge agent itself is a native
subagent: report its status and settings as usual.

Hazards to name in the lifecycle updates when they apply: the bridge occupies a
concurrency slot while idle (for many Claude workers, have one bridge run several
script invocations sequentially or in the background rather than spawning one bridge
per worker); all agents share the filesystem, so bound Claude's owned files exactly
as for a native subagent; the bridge's own tokens are real cost and belong in the
receipt as their own call (Luna has a published rate; if native metadata exposes no
usage, record an explicit `unavailable` call, never zero).

Lifecycle blocks for a bridge dispatch use the same shape as native ones, with the
lane made explicit:

~~~text
ASTRA DELEGATE <name>
task: <bounded deliverable and owned files>
requested: gpt-5.6-luna / low  ->  claude-sonnet-5 / high (bridge, mode=review)
reason: <why this work warrants a Claude model and this effort>

ASTRA RESULT <name> / <bridge agent ID or unavailable>
status: <completed, failed, interrupted, or blocked; actual evidence>
requested: gpt-5.6-luna / low  ->  claude-sonnet-5 / high
observed: <bridge model or unobservable>  ->  <canonicalModel from raw JSON> / unobservable
evidence: <native metadata source>  ->  <path to raw result JSON>; session <session_id>
~~~

## Evidence and review

The public spawn and thread metadata are authoritative for model and effort. Use
runtime introspection only to resolve a field that public metadata omitted, and report
the source of each value. Chosen values are not the same as runtime-confirmed values.

For substantial implementation, the parent first inspects the complete accumulated
diff and reruns the requested checks. It then starts a fresh read-only reviewer in a
new context. The reviewer can be `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-5.6-luna`
natively, or `claude-opus-5`, `claude-sonnet-5`, or `claude-haiku-4-5` through the
bridge in `--mode review`, with an effort supported by live metadata, and must receive
the exact change set, interfaces, constraints, and verification evidence. Ask it to
return:

~~~text
ASTRA REVIEW
VERDICT: ship | fix-first | rethink
REASON: <evidence-based reason>
FINDINGS: <precise findings or none>
RESIDUAL RISK: <remaining risk or none>
~~~

Treat `ship` as the only accepting verdict for substantial implementation. On
`fix-first`, the parent makes the correction, reruns verification, and obtains a new
fresh review. On `rethink`, revise the plan before claiming completion. The reviewer
must not edit files or implement its own fixes. Capture actual sandbox and permission
metadata when the host exposes them; do not claim enforced read-only isolation unless
it was observed.

## ChatGPT app and cloud boundaries

Native Codex subagents in the ChatGPT app are usable when the exposed tool schema
provides the needed controls. Separate app tasks require an explicit user request.
For an explicit Codex app project task, `mcp__codex_app__create_thread` supports
`model` and `thinking`; call `mcp__codex_app__list_projects` first, use a worktree by
default when the selected project is a Git repository, and use local otherwise.
Follow any explicit starting-state request exactly.

ChatGPT Work cloud `create_thread` does not accept `model` or `thinking`; omit both.
Cloud work therefore cannot currently promise arbitrary model or effort control. Do
not dispatch an incompatible model-pinned request there by default, and do not fake
cloud model control with an API key, a nested CLI, or a fabricated tool. A future
native work tool is usable only once its schema exposes the required controls. The
Claude bridge lane is a separate, documented local lane with its own enforceable
controls and evidence; it is not a cloud workaround and must not be used to pretend
that cloud work ran on a pinned model.

## Reporting

For each delegation and review, report the selected model/effort, the evidence source,
the bounded deliverable, and the actual result. Keep chosen-but-unconfirmed values
separate from runtime-confirmed values. A parent acceptance claim requires its own
diff inspection and requested checks; a subagent's assertion alone is insufficient.

## Automatic lifecycle updates

Emit these updates in the user's conversation, not only in an internal log. They
apply to each implementer and each fresh reviewer, including failed dispatches:

~~~text
ASTRA DELEGATE <name>
task: <bounded deliverable and owned files>
requested: <model> / <effort>
reason: <why this work warrants this selection>

ASTRA RESULT <name> / <agent ID or unavailable>
status: <completed, failed, interrupted, or blocked; actual evidence>
requested: <model> / <effort>
observed: <model or unobservable> / <effort or unobservable>
evidence: <runtime metadata source or unavailable>
~~~

Do not equate a successful dispatch with completed work. Keep a record of agent IDs,
requested settings, runtime observations, result evidence, and any usage source.
Native metadata may expose neither realized settings nor billing-grade usage; say so.
No API keys, external inference CLIs, billing-account queries, or dashboard are needed.

## API-equivalent receipt policy

Every task completion requires a visible receipt, including a task with no delegation
or no accessible token telemetry. The calculator is Python standard library only:
[calculator](../../../scripts/cost_receipt.py),
[pricing snapshot](../../../pricing/2026-09-06.json) (the earlier
[2026-09-04 snapshot](../../../pricing/2026-09-04.json) remains for historical receipts).
Resolve these paths relative to this installed reference, not a guessed cache version.

Use only non-overlapping observed usage with an explicit source. Cumulative telemetry
snapshots are not additive calls. Never sum a parent-inclusive aggregate with child
totals. Do not turn message lengths into claimed observed usage. Missing usage or
rates must remain unavailable, and partial coverage must state which work is missing.
Whole-task coverage requires every parent and subagent call, including failed attempts,
bridge agents, Claude calls, review, corrections, and final parent work. If the final
response's tokens cannot yet be observed, identify the receipt's cutoff and do not
claim whole-task completeness.

Cached input is a subset of total input. Anthropic cache writes are also a subset of
total input, reported separately as `cache_write_5m_input_tokens` and
`cache_write_1h_input_tokens` and priced at the model's published 1.25x / 2x write
rates; a call that reports non-zero writes on a model with no write rate is
`unavailable`, not discounted. Output already contains reasoning tokens; never add
them a second time. Explicit per-call standard short-context eligibility is required;
unknown or unsupported long-context or service-tier pricing must not silently inherit
standard rates. Effort is recorded without a rate multiplier.

The snapshot records USD per million tokens and official source URLs with per-model
verification dates supplied by the recording coordinator (OpenAI rows 2026-09-04,
Anthropic rows 2026-09-06). It is a historical snapshot, not a live-price guarantee;
Sol rates are promotional. Disclose the snapshot date and freshness when showing
an estimate. Use a newly verified versioned snapshot if current prices are required.
Do not silently change historical receipts.

For a bridge dispatch, the receipt lists two agents and two calls: the bridge
(`role: delegate` or `reviewer`, model `gpt-5.6-luna`, native telemetry or an
explicit unavailable call) and the Claude run (same role, the `<out>.call.json` record
the script wrote). The script maps Claude Code's `usage` onto the calculator schema:
`input_tokens` = uncached + cache reads + cache writes, `cached_input_tokens` = cache
reads, the 5m/1h split from `usage.cache_creation`, `reasoning_tokens` from
`output_tokens_details.thinking_tokens`, `service_tier` observed. One script invocation
is one atomic record (it sums that run's internal API iterations without overlap). The
same-token Astra repricing charges cache-write tokens at Astra's base input rate and
says so in its limitations.

~~~text
API-EQUIVALENT COST RECEIPT
usage: <observed source and cutoff, partial, or unavailable with reason>
scope: <whole task only if complete; delegated-only or observed subset otherwise>
pricing: <snapshot date; historical USD estimate; Sol promotional if applicable>
routed: <USD estimate or unavailable>
same-token Astra repricing: <USD or unavailable>
same-token API price difference: <USD and percentage where valid, or unavailable>
limits: This is not a measured all-Astra counterfactual, actual net task savings,
        or a change in ChatGPT subscription charges or usage credits.
~~~

When no subagents ran, state `no delegation savings`. When no usage is exposed,
state `unavailable: native tools did not expose observed token usage`; never show
zero cost. Keep any illustrative fixture result visibly separate from live usage.

## Calculator input and execution

Run `python3 cost_receipt.py INPUT.json [--pricing PATH]` using the installed
calculator path above. It emits a JSON receipt; exit 0 includes calculated, partial,
and unavailable outcomes, while invalid input or pricing exits 2. Inspect the
receipt status instead of treating exit 0 as proof of complete usage.

The version 1 input contains:

- `schema_version: 1`, `task_id`, and `coverage` with `scope` (`whole_task` or
  `delegated_only`), `agent_roster_complete`, and `final_parent_usage_cutoff` booleans.
- `agents`: unique `agent_id`, `role` (`parent`, `delegate`, or `reviewer`), and
  `calls_complete`. Declare missing agents rather than omitting them to improve coverage.
- `calls`: globally unique `call_id`, declared `agent_id`, `model`, optional `effort`,
  and `aggregation: "atomic"`. Supply `usage.kind`, a non-empty `usage.source`, and
  `input_tokens`, `cached_input_tokens`, and `output_tokens` when known. Optional
  `cache_write_5m_input_tokens` / `cache_write_1h_input_tokens` (Anthropic) are
  subsets of input alongside cached input. Optional `reasoning_tokens` is already
  included in output. Missing values stay unknown.
- Each call also declares `context: "standard"` and `service_tier: "standard"`, with
  `context_source` and `service_tier_source` set to `observed` or `assumed`. If runtime
  tier metadata is null, a clearly disclosed standard-price scenario is permitted;
  never relabel that assumption as observed billing. Known nonstandard regimes
  are unsupported. Do not assume a workload eligible when evidence contradicts it.

See the [illustrative input](../../../examples/illustrative-usage.json) for an
executable fixture, distinct from observed task usage. Receipts preserve assumptions,
usage provenance, and per-agent coverage. Delegated-only scope includes reviewers;
whole-task scope needs an authoritative complete roster, complete calls for each
agent, a parent, and final parent usage. Solo work does not require an invented
reviewer. False completeness flags keep the result partial or unavailable.

For cumulative native telemetry, retain each snapshot as source evidence, skip exact
repeats, and derive atomic records only when the cumulative delta matches the
reported last-call usage for every token field. If events are missing, counters reset,
or aggregate ownership is unclear, mark that coverage unavailable rather than
inventing calls. Keep preparation-turn usage separate from the implementation turn
when that is the declared task scope.

The bundled calculator conservatively caps each call at 128,000 input tokens unless
the model's pricing entry carries `max_standard_input_tokens` mirrored from its
official page (Claude Opus 5 and Sonnet 5: 1,000,000 at standard rates; Claude Haiku
4.5: 200,000). These are implementation support boundaries, not independent pricing
claims. Missing cache counts remain unknown; provide an explicit zero only when
supported by the usage source. Unknown usage fields are rejected.

See the [illustrative bridge input](../../../examples/illustrative-bridge-usage.json)
for an executable fixture of a parent + Luna bridge + Claude reviewer receipt.
