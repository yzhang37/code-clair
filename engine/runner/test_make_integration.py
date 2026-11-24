"""test_make_integration.py 核验宿主构建入口保留和回应原话的实际 Make 传输。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from make_integration import host_makefile, rendered_sidecar_makefile


class MakeIntegrationTests(unittest.TestCase):
    def test_new_host_and_repeat_are_identical(self):
        generated = host_makefile(None)
        self.assertEqual(generated, host_makefile(b""))
        self.assertEqual(generated, host_makefile(generated))
        self.assertEqual(generated.count(b"# Clarity sidecar begin"), 1)
        self.assertIn(b"include .clarity/Makefile\n", generated)

    def test_preserves_user_content_and_existing_default(self):
        original = b"# Owner's build\nNAME := app\nall:\n\t@printf 'HOST_DEFAULT'\n\n"
        generated = host_makefile(original)
        self.assertTrue(generated.startswith(original))
        self.assertEqual(generated, host_makefile(generated))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_makefiles(root, generated)
            completed = self.make(root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "HOST_DEFAULT")

    def test_unterminated_user_line_gets_one_boundary(self):
        original = b"# Owner's note"
        generated = host_makefile(original)
        self.assertTrue(generated.startswith(original + b"\n"))
        self.assertEqual(generated, host_makefile(generated))

    def test_owned_block_moves_to_end_preserving_other_content(self):
        original = host_makefile(None) + b"all:\n\t@true\n"
        generated = host_makefile(original)
        self.assertTrue(generated.startswith(b"all:\n\t@true\n"))
        self.assertEqual(generated, host_makefile(generated))

    def test_local_edits_inside_owned_markers_are_rejected(self):
        for added in (b"# Owner's note\n", b"custom-target:\n\t@true\n"):
            original = host_makefile(None).replace(b"include .clarity/Makefile\n",
                                                  b"include .clarity/Makefile\n" + added)
            with self.subTest(added=added), self.assertRaisesRegex(ValueError, "本地修改"):
                host_makefile(original)

    def test_bad_markers_are_rejected(self):
        for original in (
            b"# Clarity sidecar begin\n",
            b"# Clarity sidecar end\n# Clarity sidecar begin\n",
            host_makefile(None) * 2,
            b" # Clarity sidecar begin\n# Clarity sidecar end\n",
        ):
            with self.subTest(original=original), self.assertRaises(ValueError):
                host_makefile(original)

    def test_foreign_review_targets_are_rejected(self):
        for original in (
            b"review:\n\t@echo owner\n",
            b"all review-staged::\n",
            b"all \\\n review-engine &: dependency\n",
            b"review-respond: export NAME := value\n",
            host_makefile(None) + b"review-rules:\n",
        ):
            with self.subTest(original=original), self.assertRaises(ValueError):
                host_makefile(original)

    def test_mentions_and_dependencies_are_preserved(self):
        original = (b"# review: a comment\n.PHONY: all review\n"
                    b"LABEL = review: label\nreview := variable\n"
                    b"all: review\n\t@echo review:\n")
        self.assertTrue(host_makefile(original).startswith(original))

    def test_exact_legacy_is_migrated(self):
        legacy = rendered_sidecar_makefile()
        self.assertEqual(host_makefile(legacy, legacy), host_makefile(None))
        with self.assertRaises(ValueError):
            host_makefile(legacy + b"# owner edit\n", legacy)

    def test_response_reaches_python_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_makefiles(root, host_makefile(None))
            runner = root / ".clarity/engine/runner/review.py"
            runner.parent.mkdir(parents=True)
            runner.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path('received.json').write_text(json.dumps({"
                "'response': os.environ['CLARITY_RESPONSE'], 'argv': sys.argv[1:]}))\n"
            )
            response = "原话 $HOME $(shell touch MAKE_EXECUTED) `touch SHELL_EXECUTED`\n第二行"
            for target, command in (("review-respond", "respond"),
                                    ("review-engine-respond", "maintain-respond")):
                with self.subTest(target=target):
                    completed = self.make(root, target, "RESPONSE=" + response)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    received = json.loads((root / "received.json").read_text())
                    self.assertEqual(received["response"], response)
                    self.assertEqual(received["argv"], [command, "--text-env", "CLARITY_RESPONSE"])
                    self.assertFalse((root / "MAKE_EXECUTED").exists())
                    self.assertFalse((root / "SHELL_EXECUTED").exists())

    def write_makefiles(self, root, host):
        (root / "Makefile").write_bytes(host)
        (root / ".clarity").mkdir()
        (root / ".clarity/Makefile").write_bytes(rendered_sidecar_makefile())

    def make(self, root, *arguments):
        executable = shutil.which("make")
        if executable is None:
            self.skipTest("make is not available")
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "RESPONSE"}}
        return subprocess.run([executable, "--no-print-directory", *arguments], cwd=root,
                              env=environment, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
