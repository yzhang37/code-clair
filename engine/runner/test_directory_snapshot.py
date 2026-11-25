"""test_directory_snapshot.py 用普通目录核对直接取材、排除范围和快照变化。"""

import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from directory_snapshot import read_snapshot
from make_integration import INCLUDE_BLOCK


class DirectorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-directory-input-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_empty_and_metadata_only_directories_have_no_user_text(self):
        self.assertEqual(read_snapshot(self.root), {"files": {}, "excluded": {}})
        self.write(".clarity/current/rules.md", b"Project rules.\n")
        self.write(".git/config", b"Git metadata.\n")
        self.write("Makefile", INCLUDE_BLOCK)
        snapshot = read_snapshot(self.root)
        self.assertEqual(snapshot["files"], {})
        self.assertEqual(snapshot["excluded"], {".clarity": "clarity_metadata", ".git": "git_metadata",
                                                  "Makefile": "generated_make_entry"})

    def test_untracked_nested_unicode_and_custom_makefile_are_preserved(self):
        raw = "def 身份(value):\n    return value\n".encode()
        code = self.write("有 空格/identity.py", raw)
        code.chmod(0o755)
        make = b"build:\n\t@echo ordinary project\n\n" + INCLUDE_BLOCK
        self.write("Makefile", make)
        snapshot = read_snapshot(self.root)
        self.assertEqual(set(snapshot["files"]), {"有 空格/identity.py", "Makefile"})
        self.assertEqual(snapshot["files"]["有 空格/identity.py"], {
            "text": raw.decode(), "oid": "sha256:" + hashlib.sha256(raw).hexdigest(), "mode": "100755"})
        self.assertEqual(snapshot["files"]["Makefile"]["text"], make.decode())
        self.assertFalse((self.root / ".git").exists())

    def test_project_ignores_and_nested_negation_apply_without_git(self):
        self.write(".gitignore", b"*.tmp\n*.log\nignored/\n!keep.tmp\n")
        self.write("discard.tmp", b"discard")
        self.write("keep.tmp", b"keep")
        self.write("ignored/hidden.py", b"not selected")
        self.write("src/.gitignore", b"*.log\n!keep.log\n")
        self.write("src/discard.log", b"discard")
        self.write("src/keep.log", b"keep")
        snapshot = read_snapshot(self.root)
        self.assertEqual(set(snapshot["files"]), {".gitignore", "keep.tmp", "src/.gitignore", "src/keep.log"})
        self.assertEqual(snapshot["excluded"], {"discard.tmp": "gitignore", "ignored/": "gitignore",
                                                  "src/discard.log": "gitignore"})

    def test_links_do_not_read_outside_and_binary_exclusions_are_visible(self):
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("OUTSIDE_MUST_NOT_BE_READ")
        (self.root / ".gitignore").symlink_to(outside)
        (self.root / "outside-link.txt").symlink_to(outside)
        (self.root / "directory-link").symlink_to(outside.parent, target_is_directory=True)
        self.write("image.bin", b"\x00binary")
        self.write("legacy.txt", b"\xff\xfelegacy")
        self.write("code.py", b"value = 1\n")
        snapshot = read_snapshot(self.root)
        self.assertEqual(set(snapshot["files"]), {"code.py"})
        self.assertEqual(snapshot["excluded"], {".gitignore": "symbolic_link", "outside-link.txt": "symbolic_link", "directory-link": "symbolic_link",
                                                  "image.bin": "binary_or_non_utf8", "legacy.txt": "binary_or_non_utf8"})
        self.assertEqual(outside.read_text(), "OUTSIDE_MUST_NOT_BE_READ")

    def test_snapshot_changes_when_text_mode_or_selected_paths_change(self):
        code = self.write("code.py", b"value = 1\n")
        first = read_snapshot(self.root)
        code.write_text("value = 2\n")
        second = read_snapshot(self.root)
        self.assertNotEqual(first, second)
        code.chmod(0o755)
        third = read_snapshot(self.root)
        self.assertNotEqual(second, third)
        code.unlink()
        self.assertNotEqual(third, read_snapshot(self.root))

    def test_a_file_changing_during_read_does_not_form_a_snapshot(self):
        self.write("code.py", b"value = 1\n")
        before = SimpleNamespace(st_size=10, st_mtime_ns=1, st_mode=0o100644)
        after = SimpleNamespace(st_size=10, st_mtime_ns=2, st_mode=0o100644)
        with patch("directory_snapshot.os.fstat", side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, "changed while it was being read"):
                read_snapshot(self.root)

    def test_oversized_text_is_rejected_instead_of_truncated_or_excluded(self):
        self.write("code.py", b"x" * 17)
        with self.assertRaisesRegex(ValueError, "capacity"):
            read_snapshot(self.root, max_bytes=16)
        self.write("code.py", b"x" * 10)
        self.write("other.py", b"y" * 10)
        with self.assertRaisesRegex(ValueError, "capacity"):
            read_snapshot(self.root, max_bytes=16)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_special_files_are_not_opened(self):
        os.mkfifo(self.root / "pipe")
        self.assertEqual(read_snapshot(self.root), {"files": {}, "excluded": {"pipe": "unsupported_file_type"}})


if __name__ == "__main__":
    unittest.main()
