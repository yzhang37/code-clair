"""test_review_context.py 核对冻结正文、只读路径边界和可完整续读的有界查询。"""

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from review_context import ContextStore, EXCERPT_CHARS, materialize_context


def frozen(text, oid="recorded-oid", mode="100644"):
    return {"text": text, "oid": oid, "mode": mode}


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-context-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "view"
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.restore_permissions)

    def restore_permissions(self):
        if self.root.is_symlink() or not self.root.is_dir():
            return
        self.root.chmod(0o700)
        for folder, directories, files in os.walk(self.root):
            for name in directories:
                path = Path(folder) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(folder) / name
                if not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode) and path.stat().st_nlink == 1:
                    path.chmod(0o600)

    def store(self, current=None, baseline=None, log_path=None):
        materialize_context({"current": current or {}, "baseline": baseline or {}}, self.root)
        return ContextStore(self.root, log_path)

    def test_materialization_preserves_frozen_text_identity_and_input(self):
        text = "\ufeff身份 = 'é'\r\n\r\nlast line"
        context = {"current": {"有 空格/run.py": frozen(text, "git-object", "100755")},
                   "baseline": {"removed.py": frozen("old\n", "old-object")}}
        original = copy.deepcopy(context)
        before_mode = stat.S_IMODE(self.base.stat().st_mode)
        manifest = materialize_context(context, self.root)
        self.assertEqual(context, original)
        path = self.root / "current/有 空格/run.py"
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        record = manifest["current_files"]["有 空格/run.py"]
        self.assertEqual({key: record[key] for key in ("view_path", "oid", "mode", "lines", "bytes")},
                         {"view_path": "current/有 空格/run.py", "oid": "git-object", "mode": "100755",
                          "lines": 3, "bytes": len(text.encode("utf-8"))})
        self.assertEqual(json.loads((self.root / "context.json").read_text()), manifest)
        self.assertGreater(len((self.root / "context.json").read_text().splitlines()), 10)
        for folder, directories, files in os.walk(self.root):
            self.assertEqual(stat.S_IMODE(Path(folder).stat().st_mode), 0o555)
            for name in files:
                self.assertEqual(stat.S_IMODE((Path(folder) / name).stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), before_mode)

    def test_manifest_scope_and_explicit_versions_include_ignored_paths(self):
        store = self.store({".gitignore": frozen("ignored/\n"), "ignored/tracked.py": frozen("CURRENT\n"),
                            "new.py": frozen("new")}, {"removed.py": frozen("BASELINE\n")})
        self.assertEqual([record["path"] for record in store.list_files()["files"]],
                         [".gitignore", "ignored/tracked.py", "new.py"])
        self.assertEqual(store.read_file("ignored/tracked.py")["text"], "CURRENT\n")
        self.assertEqual(store.search("CURRENT")["matches"][0]["path"], "ignored/tracked.py")
        self.assertEqual(store.read_file("removed.py", version="baseline")["text"], "BASELINE\n")
        with self.assertRaisesRegex(ValueError, "Unknown context path"):
            store.read_file("removed.py")
        self.assertEqual(store.search("BASELINE")["total"], 0)

    def test_listing_has_stable_prefix_pagination_and_no_body(self):
        store = self.store({"src/b.py": frozen("b"), "other.py": frozen("outside prefix"), "src/a.py": frozen("a")})
        first = store.list_files(prefix="src/", limit=1)
        second = store.list_files(prefix="src/", offset=first["next_offset"], limit=1)
        self.assertEqual([first["files"][0]["path"], second["files"][0]["path"]], ["src/a.py", "src/b.py"])
        self.assertEqual(first["total"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("text", first["files"][0])
        self.assertEqual(store.list_files(offset=100)["files"], [])
        json.dumps(first)

    def test_unlisted_file_never_enters_queries(self):
        store = self.store({"known.py": frozen("known")})
        (self.root / "current").chmod(0o700)
        (self.root / "current/unlisted.py").write_text("HOST_SECRET_MARKER")
        with self.assertRaisesRegex(ValueError, "Unknown context path"):
            store.read_file("unlisted.py")
        self.assertEqual(store.search("HOST_SECRET_MARKER")["total"], 0)
        self.assertEqual(store.list_files()["total"], 1)

    def test_read_pages_reconstruct_long_unicode_lines_and_original_newlines(self):
        text = "界" * 20_001 + "\r\nsecond\nthird"
        store = self.store({"long.py": frozen(text)})
        pieces, cursor = [], {"start_line": 1, "start_column": 1}
        while cursor is not None:
            reply = store.read_file("long.py", **cursor, line_count=2, char_limit=701)
            self.assertLessEqual(len(reply["text"]), 701)
            pieces.append(reply["text"])
            cursor = reply["next"]
        self.assertEqual("".join(pieces), text)
        first = store.read_file("long.py", char_limit=16_000)
        self.assertEqual(first["next"], {"start_line": 1, "start_column": 16_001})
        self.assertTrue(first["truncated"])
        self.assertEqual(first["end_column"], 16_000)
        self.assertEqual(first["total_lines"], 3)

    def test_read_line_boundary_and_empty_file_are_explicit(self):
        store = self.store({"lines.py": frozen("one\ntwo\nthree\n"), "empty.py": frozen("")})
        first = store.read_file("lines.py", line_count=1)
        self.assertEqual(first["text"], "one\n")
        self.assertEqual(first["next"], {"start_line": 2, "start_column": 1})
        self.assertEqual(store.read_file("lines.py", start_line=2, start_column=2)["text"], "wo\nthree\n")
        self.assertEqual(store.read_file("lines.py", start_line=4)["text"], "")
        empty = store.read_file("empty.py")
        self.assertEqual((empty["text"], empty["total_lines"], empty["next"], empty["truncated"]), ("", 0, None, False))
        with self.assertRaises(ValueError):
            store.read_file("lines.py", start_column=6)
        with self.assertRaises(ValueError):
            store.read_file("lines.py", start_line=5)
        with self.assertRaises(ValueError):
            store.read_file("empty.py", start_column=2)

    def test_default_read_enforces_line_limit_with_a_continuation(self):
        store = self.store({"many.py": frozen("x\n" * 401)})
        reply = store.read_file("many.py")
        self.assertEqual(reply["text"], "x\n" * 200)
        self.assertEqual(reply["next"], {"start_line": 201, "start_column": 1})

    def test_search_is_literal_and_pages_matching_lines(self):
        store = self.store({"b.py": frozen("a.*b repeated a.*b\n"), "a.py": frozen("a.*b\naxxb\na.*b\n")})
        first = store.search("a.*b", limit=1)
        second = store.search("a.*b", offset=first["next_offset"], limit=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual([(match["path"], match["line"], match["column"]) for match in first["matches"] + second["matches"]],
                         [("a.py", 1, 1), ("a.py", 3, 1), ("b.py", 1, 1)])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(store.search("A.*B")["total"], 0)
        self.assertEqual(store.search("a.*b", prefix="b")["total"], 1)

    def test_search_excerpts_reveal_late_matches_without_returning_long_lines(self):
        store = self.store({"long.py": frozen("x" * 1000 + "needle" + "y" * 1000)})
        match = store.search("needle")["matches"][0]
        self.assertEqual(match["column"], 1001)
        self.assertEqual(match["excerpt_start_column"], 941)
        self.assertEqual(match["match_length"], 6)
        self.assertEqual(len(match["text"]), EXCERPT_CHARS)
        self.assertIn("needle", match["text"])
        self.assertTrue(match["excerpt_truncated"])

    def test_bounds_and_types_are_rejected_instead_of_silently_clamped(self):
        store = self.store({"a.py": frozen("text")})
        for kwargs in ({"limit": 101}, {"limit": 0}, {"offset": -1}, {"limit": True}, {"offset": "0"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.list_files(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.search("text", **kwargs)
        for kwargs in ({"line_count": 201}, {"char_limit": 16_001}, {"char_limit": 0}, {"start_line": 0}, {"start_column": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.read_file("a.py", **kwargs)
        for query in ("", "x" * 1001, "one\ntwo", "text\n", None):
            with self.subTest(query=query), self.assertRaises(ValueError):
                store.search(query)
        for version in ("HEAD", "../current", None, []):
            with self.subTest(version=version), self.assertRaises(ValueError):
                store.list_files(version=version)

    def test_traversal_absolute_alias_and_unknown_paths_are_denied(self):
        store = self.store({"nested/a.py": frozen("inside")})
        for path in ("../outside", "/etc/passwd", "nested/../nested/a.py", "./nested/a.py", "nested//a.py",
                     "nested/a.py/", "current/nested/a.py", "C:/outside", "C:\\outside", "nested\\a.py", "", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                store.read_file(path)
        for prefix in ("../", "/", "nested/../", "C:/", "nested\\"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                store.list_files(prefix=prefix)
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                store.search("inside", prefix=prefix)

    def test_projected_symlink_hardlink_and_special_file_substitutes_are_denied(self):
        store = self.store({"nested/a.py": frozen("inside")})
        outside = self.base / "outside"
        outside.write_text("OUTSIDE_SECRET")
        path = self.root / "current/nested/a.py"
        path.parent.chmod(0o700)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            store.read_file("nested/a.py")
        path.unlink()
        os.link(outside, path)
        with self.assertRaisesRegex(ValueError, "ordinary projection"):
            store.read_file("nested/a.py")
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "ordinary projection"):
            store.read_file("nested/a.py")
        self.assertEqual(outside.read_text(), "OUTSIDE_SECRET")

    def test_parent_directory_symlink_cannot_redirect_a_declared_path(self):
        store = self.store({"nested/a.py": frozen("inside")})
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.py").write_text("OUTSIDE_SECRET")
        folder = self.root / "current/nested"
        self.restore_permissions()
        (folder / "a.py").unlink()
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            store.read_file("nested/a.py")

    def test_same_size_projection_rewrite_is_not_accepted_as_frozen_text(self):
        store = self.store({"a.py": frozen("original")})
        path = self.root / "current/a.py"
        path.chmod(0o600)
        path.write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            store.read_file("a.py")

    def test_unsafe_or_conflicting_input_is_rejected_before_projection(self):
        for files in ({"../escape": frozen("x")}, {"/absolute": frozen("x")}, {"link": frozen("x", mode="120000")},
                      {"a": frozen("x"), "a/child": frozen("y")}, {"missing-text": {"oid": "id", "mode": "100644"}}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                materialize_context({"current": files}, self.root)
            self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            materialize_context({"unexpected": {}}, self.root)

    def test_nonempty_or_symlink_roots_are_not_reused(self):
        self.root.mkdir()
        existing = self.root / "existing"
        existing.write_text("preserve")
        with self.assertRaises(ValueError):
            materialize_context({"current": {}}, self.root)
        self.assertEqual(existing.read_text(), "preserve")
        existing.unlink()
        self.root.rmdir()
        self.root.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            materialize_context({"current": {}}, self.root)
        with self.assertRaises(ValueError):
            ContextStore(self.root)

    def test_manifest_path_escape_is_rejected(self):
        self.store({"a.py": frozen("inside")})
        path = self.root / "context.json"
        manifest = json.loads(path.read_text())
        manifest["current_files"]["a.py"]["view_path"] = "../../outside"
        path.chmod(0o600)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "view path"):
            ContextStore(self.root)

    def test_optional_audit_is_outside_view_and_has_no_source_or_search_payload(self):
        query = "SECRET_QUERY_SYNTHETIC_MARKER"
        log = self.base / "audit.jsonl"
        store = self.store({"a.py": frozen(query + "\n")}, log_path=log)
        store.list_files()
        store.read_file("a.py")
        store.search(query)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([row["tool"] for row in rows], ["list_files", "read_file", "search"])
        self.assertEqual(rows[-1]["query_chars"], len(query))
        self.assertEqual(rows[-1]["total"], 1)
        self.assertNotIn(query, log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "outside"):
            ContextStore(self.root, self.root / "audit.jsonl")
        alias = self.base / "log-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            ContextStore(self.root, alias / "audit.jsonl")

    def test_caller_can_restore_permissions_and_remove_projection_after_use(self):
        store = self.store({"nested/a.py": frozen("code")})
        self.assertEqual(store.read_file("nested/a.py")["text"], "code")
        self.restore_permissions()
        for folder, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(folder) / name).unlink()
            for name in directories:
                (Path(folder) / name).rmdir()
        self.root.rmdir()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
