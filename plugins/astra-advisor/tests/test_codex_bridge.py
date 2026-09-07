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
SCRIPT = PLUGIN / "scripts" / "codex_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_bridge", SCRIPT)
assert SPEC and SPEC.loader
codex_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_bridge)

THREAD = "01a07990-6978-7381-ac11-98d79d8265b9"


def sample_events(*, failed: bool = False, cache_write: int = 0) -> str:
    """Shape observed from `codex exec --json` (codex-cli 0.153.4, 2026-09-06)."""
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Skill descriptions were shortened"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "BRIDGE-OK"}},
    ]
    if failed:
        events.append({"type": "turn.failed", "error": {"message": "boom"}})
    else:
        events.append({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 18818,
                "cached_input_tokens": 9984,
                "cache_write_input_tokens": cache_write,
                "output_tokens": 8,
                "reasoning_output_tokens": 0,
            },
        })
    return "\n".join(json.dumps(e) for e in events) + "\n"


def write_rollout(root: Path, thread: str = THREAD, model: str = "gpt-5.6-luna", effort: str = "low", sandbox: str = "read-only") -> Path:
    day = root / "2026" / "09" / "06"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-06T18-51-42-{thread}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": thread}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "model": model, "effort": effort, "sandbox_policy": {"type": sandbox}}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


class CommandTests(unittest.TestCase):
    def test_review_is_read_only_headless_and_stdin_prompt(self) -> None:
        argv = codex_bridge.build_command(model="gpt-5.6-luna", effort="low", mode="review", resume=None, cwd="/repo", last_message_path="/tmp/x.last.md")
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-luna")
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "/repo")
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_implement_is_workspace_write(self) -> None:
        argv = codex_bridge.build_command(model="gpt-5.6-terra", effort="high", mode="implement", resume=None, cwd=None, last_message_path="/tmp/x.last.md")
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertNotIn("-C", argv)

    def test_resume_omits_sandbox_and_cwd(self) -> None:
        argv = codex_bridge.build_command(model="gpt-5.6-sol", effort="xhigh", mode="review", resume=THREAD, cwd="/repo", last_message_path="/tmp/x.last.md")
        self.assertEqual(argv[:4], ["codex", "exec", "resume", THREAD])
        self.assertNotIn("-s", argv)
        self.assertNotIn("-C", argv)

    def test_effort_must_match_model(self) -> None:
        with self.assertRaisesRegex(codex_bridge.BridgeError, "effort"):
            codex_bridge.build_command(model="gpt-5.6-luna", effort="ultra", mode="review", resume=None, cwd=None, last_message_path="x")
        codex_bridge.build_command(model="gpt-5.6-sol", effort="ultra", mode="review", resume=None, cwd=None, last_message_path="x")

    def test_model_allowlist(self) -> None:
        with self.assertRaisesRegex(codex_bridge.BridgeError, "model"):
            codex_bridge.build_command(model="claude-sonnet-5", effort="low", mode="review", resume=None, cwd=None, last_message_path="x")


class ParseTests(unittest.TestCase):
    def test_parse_events_reduces_stream(self) -> None:
        parsed = codex_bridge.parse_events(sample_events())
        self.assertEqual(parsed["thread_id"], THREAD)
        self.assertEqual(parsed["usage"]["input_tokens"], 18818)
        self.assertEqual(parsed["last_message"], "BRIDGE-OK")
        self.assertFalse(parsed["failed"])
        self.assertEqual(parsed["errors"], ["Skill descriptions were shortened"])

    def test_parse_events_marks_turn_failed(self) -> None:
        parsed = codex_bridge.parse_events(sample_events(failed=True))
        self.assertTrue(parsed["failed"])
        self.assertIsNone(parsed["usage"])

    def test_observed_from_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_rollout(root)
            observed = codex_bridge.observed_from_rollout(THREAD, root)
            self.assertEqual(observed["model"], "gpt-5.6-luna")
            self.assertEqual(observed["effort"], "low")
            self.assertEqual(observed["sandbox"], "read-only")
            self.assertEqual(observed["source"], str(path))
            self.assertIsNone(codex_bridge.observed_from_rollout("nope", root)["model"])

    def test_result_to_call_prefers_observed_and_maps_usage(self) -> None:
        parsed = codex_bridge.parse_events(sample_events())
        call = codex_bridge.result_to_call(
            parsed, agent_id="b", call_id="c", requested_model="gpt-5.6-luna", requested_effort="low",
            observed={"model": "gpt-5.6-luna", "effort": "low", "sandbox": "read-only", "source": "/x"},
        )
        self.assertEqual(call["model"], "gpt-5.6-luna")
        self.assertEqual(call["effort"], "low")
        usage = call["usage"]
        self.assertEqual(usage["kind"], "observed")
        self.assertEqual(usage["input_tokens"], 18818)
        self.assertEqual(usage["cached_input_tokens"], 9984)
        self.assertEqual(usage["output_tokens"], 8)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertNotIn("cache_write_5m_input_tokens", usage)

    def test_openai_cache_write_tokens_make_call_partial(self) -> None:
        parsed = codex_bridge.parse_events(sample_events(cache_write=500))
        call = codex_bridge.result_to_call(parsed, agent_id="b", call_id="c", requested_model="gpt-5.6-luna", requested_effort="low", observed={})
        self.assertEqual(call["usage"]["kind"], "partial")
        self.assertIn("cache_write_input_tokens", call["usage"]["reason"])

    def test_render_block(self) -> None:
        parsed = codex_bridge.parse_events(sample_events())
        block = codex_bridge.render_result_block(
            parsed, requested_model="gpt-5.6-luna", requested_effort="low", mode="review",
            observed={"model": "gpt-5.6-luna", "effort": "low", "sandbox": "read-only", "source": "/x.jsonl"},
            raw_path="/tmp/e.jsonl", exit_code=0,
        )
        self.assertTrue(block.startswith("CODEX RESULT\n"))
        self.assertIn("status: completed", block)
        self.assertIn(f"thread_id: {THREAD}", block)
        self.assertIn("observed_sandbox: read-only", block)
        self.assertIn("evidence: /x.jsonl", block)
        self.assertTrue(block.rstrip().endswith("BRIDGE-OK"))


class CliTests(unittest.TestCase):
    def _fake_codex(self, directory: Path, body: str) -> dict:
        fake_bin = directory / "bin"
        fake_bin.mkdir()
        fake = fake_bin / "codex"
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        return env

    def test_cli_writes_events_call_json_and_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._fake_codex(root, "#!/bin/sh\ncat >/dev/null\nprintf '%s' \"$FAKE_CODEX_EVENTS\"\n")
            env["FAKE_CODEX_EVENTS"] = sample_events()
            sessions = root / "sessions"
            write_rollout(sessions)
            out = root / "task.jsonl"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--model", "gpt-5.6-luna", "--effort", "low", "--mode", "review",
                 "--agent-id", "bridge-9", "--out", str(out), "--sessions-dir", str(sessions)],
                input="Say BRIDGE-OK.\n", env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.startswith("CODEX RESULT\n"), completed.stdout)
            self.assertIn("observed_model: gpt-5.6-luna", completed.stdout)
            self.assertTrue(out.is_file())
            call = json.loads(out.with_suffix(".call.json").read_text(encoding="utf-8"))
            self.assertEqual(call["agent_id"], "bridge-9")
            self.assertEqual(call["call_id"], f"bridge-9/codex/{THREAD}")

    def test_cli_nonzero_when_codex_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self._fake_codex(root, "#!/bin/sh\ncat >/dev/null\necho 'Not inside a trusted directory' >&2\nexit 1\n")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--model", "gpt-5.6-luna", "--effort", "low", "--mode", "review",
                 "--agent-id", "b", "--out", str(root / "t.jsonl"), "--sessions-dir", str(root / "none")],
                input="x\n", env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("status: failed", completed.stdout)
            self.assertIn("Not inside a trusted directory", completed.stdout)


if __name__ == "__main__":
    unittest.main()
