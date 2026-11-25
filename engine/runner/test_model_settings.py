"""test_model_settings.py 核对已选 Agent 的保持、自定义 ACP 入口和依赖启动边界。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import acp_runtime
from model import agent_settings


class ModelSettingsTests(unittest.TestCase):
    def test_custom_acp_command_does_not_require_pi_or_codex(self):
        config = {"agent_command": ["team-review-agent", "--acp"]}
        with patch.dict(os.environ, {}, clear=True), \
                patch("model.shutil.which", side_effect=lambda name: "/team/bin/review-agent" if name == "team-review-agent" else None):
            selected = agent_settings(config)
        self.assertEqual(selected["agent"], "custom")
        self.assertEqual(selected["agent_command"], ["/team/bin/review-agent", "--acp"])
        self.assertEqual(config, {"agent_command": ["team-review-agent", "--acp"]})

    def test_explicit_agent_switch_does_not_launch_the_previous_binary(self):
        prior = {"agent": "codex", "agent_bin": "/old/codex", "codex_bin": "/old/codex",
                 "agent_command": ["/old/codex-acp"]}
        available = {"pi": "/team/bin/pi", "pi-acp": "/team/bin/pi-acp", "/old/codex": "/old/codex"}
        with patch.dict(os.environ, {"CLARITY_AGENT": "pi"}, clear=True), \
                patch("model.shutil.which", side_effect=available.get):
            selected = agent_settings(prior, from_environment=True)
        self.assertEqual(selected["agent"], "pi")
        self.assertEqual(selected["agent_bin"], "/team/bin/pi")
        self.assertEqual(selected["agent_command"], ["/team/bin/pi-acp"])
        self.assertEqual(prior["agent_bin"], "/old/codex")

    def test_existing_selection_ignores_another_sessions_environment(self):
        saved = {"agent": "custom", "agent_command": ["/team/agent", "--acp"], "model": "owner-model"}
        environment = {"CLARITY_AGENT": "pi", "CLARITY_AGENT_COMMAND": '["/other/agent"]'}
        with patch.dict(os.environ, environment, clear=True), \
                patch("model.shutil.which", side_effect=lambda name: name if name == "/team/agent" else None):
            selected = agent_settings(saved)
        self.assertEqual(selected, saved)

    def test_legacy_codex_binary_still_selects_the_common_acp_adapter(self):
        available = {"/owner/codex": "/owner/codex", "codex-acp": "/team/codex-acp"}
        with patch.dict(os.environ, {}, clear=True), patch("model.shutil.which", side_effect=available.get):
            selected = agent_settings({"codex_bin": "/owner/codex", "model": "owner-model"})
        self.assertEqual(selected["agent"], "codex")
        self.assertEqual(selected["agent_bin"], "/owner/codex")
        self.assertEqual(selected["agent_command"], ["/team/codex-acp"])
        self.assertEqual(selected["model"], "owner-model")

    def test_incomplete_cached_runtime_stops_before_reexecuting_itself(self):
        with tempfile.TemporaryDirectory(prefix="clarity-cache-test-") as temporary:
            cache = Path(temporary)
            key = f"acp-1.0.0-jsonschema-4.26.0-pathspec-1.1.1-py{sys.version_info.major}.{sys.version_info.minor}"
            cached_python = cache / "clarity" / key / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            cached_python.parent.mkdir(parents=True)
            cached_python.write_text("incomplete cached environment")
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}, clear=True), \
                    patch("acp_runtime.ready", return_value=False), \
                    patch("acp_runtime.sys.executable", str(cached_python)), \
                    patch("acp_runtime.os.execv") as execute:
                with self.assertRaisesRegex(RuntimeError, "Cached ACP dependencies are incomplete"):
                    acp_runtime.ensure_runtime()
            execute.assert_not_called()

    def test_deleted_sidecar_hook_returns_before_dependency_setup(self):
        import review

        with tempfile.TemporaryDirectory(prefix="clarity-deleted-hook-") as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            argv = ["review.py", "hook", "pre-commit", "--repo", str(repo)]
            with patch.object(sys, "argv", argv), \
                    patch("acp_runtime.ensure_runtime", side_effect=AssertionError("Deleted sidecar must not bootstrap")) as prepare:
                self.assertEqual(review.main(), 0)
            prepare.assert_not_called()
            self.assertFalse((repo / ".clarity").exists())


if __name__ == "__main__":
    unittest.main()
