from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "scripts" / "claude_bridge.py"
SPEC = importlib.util.spec_from_file_location("claude_bridge", SCRIPT)
assert SPEC and SPEC.loader
claude_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(claude_bridge)


def sample_result(**overrides) -> dict:
    """Shape observed from `claude -p --output-format json` (Claude Code 2.1.263, 2026-09-06)."""
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "session_id": "5544f193-8451-4e8b-98d8-f9d749841809",
        "total_cost_usd": 0.0330145,
        "stop_reason": "end_turn",
        "result": "BRIDGE-OK",
        "usage": {
            "input_tokens": 9,
            "cache_creation_input_tokens": 15142,
            "cache_read_input_tokens": 13615,
            "output_tokens": 272,
            "output_tokens_details": {"thinking_tokens": 262},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 15142, "ephemeral_5m_input_tokens": 0},
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 9,
                "outputTokens": 272,
                "cacheReadInputTokens": 13615,
                "cacheCreationInputTokens": 15142,
                "costUSD": 0.0330145,
                "canonicalModel": "claude-haiku-4-5",
                "provider": "firstParty",
                "costBasis": "list",
            }
        },
    }
    result.update(overrides)
    return result


class NormalizeTests(unittest.TestCase):
    def test_maps_claude_usage_to_calculator_call(self) -> None:
        call = claude_bridge.result_to_call(
            sample_result(), agent_id="bridge-1", call_id="bridge-1/claude/1", effort="low"
        )
        self.assertEqual(call["model"], "claude-haiku-4-5")
        self.assertEqual(call["effort"], "low")
        self.assertEqual(call["aggregation"], "atomic")
        self.assertEqual(call["service_tier"], "standard")
        self.assertEqual(call["service_tier_source"], "observed")
        self.assertEqual(call["context"], "standard")
        self.assertEqual(call["context_source"], "assumed")
        usage = call["usage"]
        self.assertEqual(usage["kind"], "observed")
        self.assertIn("claude -p", usage["source"])
        self.assertEqual(usage["input_tokens"], 9 + 15142 + 13615)
        self.assertEqual(usage["cached_input_tokens"], 13615)
        self.assertEqual(usage["cache_write_5m_input_tokens"], 0)
        self.assertEqual(usage["cache_write_1h_input_tokens"], 15142)
        self.assertEqual(usage["output_tokens"], 272)
        self.assertEqual(usage["reasoning_tokens"], 262)

    def test_missing_cache_breakdown_marks_writes_unknown_as_partial(self) -> None:
        raw = sample_result()
        del raw["usage"]["cache_creation"]
        call = claude_bridge.result_to_call(raw, agent_id="b", call_id="c", effort=None)
        self.assertEqual(call["usage"]["kind"], "partial")
        self.assertNotIn("cache_write_5m_input_tokens", call["usage"])
        self.assertNotIn("cache_write_1h_input_tokens", call["usage"])
        self.assertIn("5m/1h", call["usage"]["reason"])

    def test_multiple_models_in_one_run_is_refused(self) -> None:
        raw = sample_result()
        raw["modelUsage"]["claude-sonnet-5"] = dict(raw["modelUsage"]["claude-haiku-4-5-20251001"], canonicalModel="claude-sonnet-5")
        with self.assertRaisesRegex(claude_bridge.BridgeError, "more than one model"):
            claude_bridge.result_to_call(raw, agent_id="b", call_id="c", effort=None)

    def test_missing_usage_becomes_unavailable_call(self) -> None:
        raw = sample_result()
        del raw["usage"]
        call = claude_bridge.result_to_call(raw, agent_id="b", call_id="c", effort=None)
        self.assertEqual(call["usage"]["kind"], "unavailable")
        self.assertNotIn("input_tokens", call["usage"])

    def test_non_standard_service_tier_is_kept_verbatim_for_calculator_to_reject(self) -> None:
        raw = sample_result()
        raw["usage"]["service_tier"] = "priority"
        call = claude_bridge.result_to_call(raw, agent_id="b", call_id="c", effort=None)
        self.assertEqual(call["service_tier"], "priority")

    def test_report_block_is_verbatim_and_machine_readable(self) -> None:
        block = claude_bridge.render_result_block(
            sample_result(), requested_model="claude-sonnet-5", requested_effort="low", raw_path="/tmp/x.json"
        )
        self.assertTrue(block.startswith("CLAUDE RESULT\n"))
        self.assertIn("session_id: 5544f193-8451-4e8b-98d8-f9d749841809", block)
        self.assertIn("observed_model: claude-haiku-4-5", block)  # observed differs from requested: both shown
        self.assertIn("requested: claude-sonnet-5 / low", block)
        self.assertIn("observed_effort: unobservable", block)
        self.assertIn("status: completed", block)
        self.assertIn("total_cost_usd (claude code, list basis): 0.0330145", block)
        self.assertIn("raw_json: /tmp/x.json", block)
        self.assertTrue(block.rstrip().endswith("BRIDGE-OK"))

    def test_error_result_status_failed(self) -> None:
        block = claude_bridge.render_result_block(
            sample_result(is_error=True, subtype="error_during_execution", result="boom"),
            requested_model="claude-haiku-4-5", requested_effort=None, raw_path="/tmp/x.json",
        )
        self.assertIn("status: failed (error_during_execution)", block)


class CommandTests(unittest.TestCase):
    def test_review_mode_is_read_only_and_denies_prompts(self) -> None:
        argv = claude_bridge.build_command(
            model="claude-sonnet-5", effort="high", mode="review", resume=None, max_budget_usd=None, extra_allowed_tools=[]
        )
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("--permission-prompts", argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("Edit", " ".join(argv))

    def test_implement_mode_allows_edits_without_bypass(self) -> None:
        argv = claude_bridge.build_command(
            model="claude-opus-5", effort=None, mode="implement", resume="abc", max_budget_usd="2.50", extra_allowed_tools=["Bash(npm test)"]
        )
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("--effort", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.50")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("Bash(npm test)", argv)

    def test_effort_must_be_supported(self) -> None:
        with self.assertRaisesRegex(claude_bridge.BridgeError, "effort"):
            claude_bridge.build_command(model="claude-opus-5", effort="ultra", mode="review", resume=None, max_budget_usd=None, extra_allowed_tools=[])

    def test_haiku_refuses_effort_and_reports_na(self) -> None:
        with self.assertRaisesRegex(claude_bridge.BridgeError, "does not accept an effort"):
            claude_bridge.build_command(model="claude-haiku-4-5", effort="low", mode="review", resume=None, max_budget_usd=None, extra_allowed_tools=[])
        argv = claude_bridge.build_command(model="claude-haiku-4-5", effort=None, mode="review", resume=None, max_budget_usd=None, extra_allowed_tools=[])
        self.assertNotIn("--effort", argv)
        block = claude_bridge.render_result_block(
            sample_result(), requested_model="claude-haiku-4-5", requested_effort=None, raw_path="/tmp/x.json"
        )
        self.assertIn("observed_effort: n/a", block)

    def test_model_must_be_in_bridge_allowlist(self) -> None:
        with self.assertRaisesRegex(claude_bridge.BridgeError, "model"):
            claude_bridge.build_command(model="gpt-6-astra", effort=None, mode="review", resume=None, max_budget_usd=None, extra_allowed_tools=[])

    def test_child_env_drops_nested_claude_markers(self) -> None:
        env = claude_bridge.child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin"})
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertEqual(env["PATH"], "/bin")


class CliTests(unittest.TestCase):
    def test_cli_with_fake_claude_writes_raw_json_and_call_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            fake = fake_bin / "claude"
            fake.write_text(
                "#!/bin/sh\ncat >/dev/null\nprintf '%s' \"$FAKE_CLAUDE_JSON\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            out = Path(directory) / "task.json"
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["FAKE_CLAUDE_JSON"] = json.dumps(sample_result())
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--model", "claude-haiku-4-5", "--mode", "review",
                    "--agent-id", "bridge-7", "--out", str(out),
                ],
                input="Say BRIDGE-OK.\n",
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.startswith("CLAUDE RESULT\n"), completed.stdout)
            self.assertTrue(out.is_file())
            raw = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(raw["session_id"], "5544f193-8451-4e8b-98d8-f9d749841809")
            call_path = out.with_suffix(".call.json")
            self.assertTrue(call_path.is_file())
            call = json.loads(call_path.read_text(encoding="utf-8"))
            self.assertEqual(call["agent_id"], "bridge-7")
            self.assertEqual(call["model"], "claude-haiku-4-5")

    def test_cli_nonzero_exit_when_claude_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            fake = fake_bin / "claude"
            fake.write_text("#!/bin/sh\ncat >/dev/null\necho 'not json' >&2\nexit 3\n", encoding="utf-8")
            fake.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--model", "claude-haiku-4-5", "--mode", "review", "--agent-id", "b", "--out", str(Path(directory) / "t.json")],
                input="x\n", env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 3)
            self.assertIn("CLAUDE RESULT", completed.stdout)
            self.assertIn("status: failed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
