"""test_release.py 检查发布版本排序、工厂身份和明确的旧版过渡，不运行宿主业务。"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from release import Version, compare, identity, parse_release, select


def declaration(version="1.0.0", built_at="2026-11-25T15:30:00Z"):
    """declaration() 生成测试发行声明，时间字段刻意独立于版本顺序。"""
    return json.dumps({"product_version": version, "built_at": built_at}).encode()


class VersionTests(unittest.TestCase):
    def test_core_segments_are_numeric_and_have_exactly_three_parts(self):
        self.assertLess(Version("4.2.0"), Version("4.10.0"))
        self.assertLess(Version("1.9.99"), Version("1.10.0"))
        self.assertLess(Version("4.99.99"), Version("10.0.0"))
        self.assertEqual(Version("0.0.0").core, (0, 0, 0))
        invalid = ["4.2", "4.2.0.1", "04.2.0", "4.02.0", "4.2.00", "4.00.950", "-1.0.0",
                   "v1.0.0", " 1.0.0", "1.0.0\n", "１.0.0", "1.0.0-", "1.0.0+"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Version(value)

    def test_prerelease_order_and_numeric_identifier_validation(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                   "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        self.assertEqual([str(value) for value in sorted(Version(value) for value in reversed(ordered))], ordered)
        for value in ["1.0.0-01", "1.0.0-rc.01", "1.0.0-rc..1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Version(value)
        self.assertEqual(Version("1.0.0-rc.0").prerelease, ("rc", "0"))

    def test_build_metadata_is_retained_but_does_not_order_releases(self):
        before = Version("1.2.3+20261125093000")
        after = Version("1.2.3+20261125100000")
        self.assertEqual(before, after)
        self.assertFalse(before < after)
        self.assertEqual(str(before), "1.2.3+20261125093000")
        self.assertEqual(Version("1.2.3+001").metadata, ("001",))

    def test_release_metadata_requires_valid_utc_and_does_not_guess_legacy_fields(self):
        self.assertIsNone(parse_release(None))
        expected = {"product_version": "1.0.0", "built_at": "2026-11-25T15:30:00Z"}
        self.assertEqual(parse_release(declaration()), expected)
        self.assertEqual(parse_release(declaration(built_at="2026-11-25T15:30:00.123Z"))["built_at"],
                         "2026-11-25T15:30:00.123Z")
        invalid = [b"", b"[]", b"null", b"{}", b"\xff", b'{"product_version":"1.0.0","product_version":"2.0.0"}',
                   b'{"product_version":"1.0.0","built_at":"2026-11-25T15:30:00Z","other":NaN}',
                   declaration("1.00.0"), declaration(built_at="2026-11-25T15:30:00+00:00"),
                   declaration(built_at="2026-11-25T15:30:00"), declaration(built_at="2026-02-30T15:30:00Z")]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_release(raw)

    def test_comparison_uses_release_version_even_when_old_code_has_a_later_build_time(self):
        installed = {"kind": "release", "product_version": "4.10.0", "commit": "newer-source",
                     "built_at": "2026-11-24T00:00:00Z"}
        older = {"kind": "release", "product_version": "4.2.0", "commit": "older-source",
                 "built_at": "2026-11-25T23:59:59Z"}
        self.assertEqual(compare(installed, older, "backward"), "older")
        self.assertEqual(compare(older, installed, "forward"), "newer")
        republished_fix = {**older, "product_version": "4.10.1", "commit": "reverted-code-new-release"}
        self.assertEqual(compare(installed, republished_fix, "forward"), "newer")

    def test_same_release_number_and_different_commits_is_an_identity_conflict(self):
        installed = {"kind": "release", "product_version": "1.0.0", "commit": "one"}
        self.assertEqual(compare(installed, dict(installed)), "same")
        self.assertEqual(compare(installed, {**installed, "commit": "two"}, "forward"), "conflict")
        self.assertEqual(compare(installed, {**installed, "product_version": "1.0.0+other", "commit": "two"}),
                         "conflict")
        self.assertEqual(compare(installed, {**installed, "product_version": "2.0.0"}), "conflict")

    def test_legacy_and_development_need_verified_commit_relationships(self):
        legacy = {"kind": "legacy", "product_version": None, "built_at": None, "commit": "old"}
        release = {"kind": "release", "product_version": "1.0.0", "commit": "release"}
        development = {**release, "kind": "development", "commit": "development"}
        self.assertEqual(compare(legacy, release), "unknown")
        self.assertEqual(compare(legacy, release, "forward"), "migration")
        self.assertEqual(compare(legacy, release, "backward"), "older")
        self.assertEqual(compare(legacy, release, "diverged"), "diverged")
        self.assertEqual(compare(release, development, "forward"), "development")
        self.assertEqual(compare(release, development), "unknown")
        self.assertEqual(compare(legacy, {**legacy, "commit": "later"}, "forward"), "development")
        self.assertEqual(compare(legacy, dict(legacy)), "same")
        with self.assertRaises(ValueError):
            compare(legacy, release, "not-a-relation")

    def test_known_lower_candidate_is_older_even_without_saved_publication_proof(self):
        installed = {"kind": "unverified", "product_version": "2.0.0", "commit": "old-manager-deployment"}
        candidate = {"kind": "release", "product_version": "1.9.0", "commit": "later-source-commit"}
        for installed_kind in ("unverified", "development", "legacy", "release", "prerelease"):
            for candidate_kind in ("release", "prerelease", "development", "legacy"):
                with self.subTest(installed=installed_kind, candidate=candidate_kind):
                    self.assertEqual(compare({**installed, "kind": installed_kind},
                                             {**candidate, "kind": candidate_kind}, "forward"), "older")
        self.assertEqual(compare(installed, {**candidate, "commit": installed["commit"]}), "older")

    def test_unverified_same_number_at_another_commit_is_not_a_verified_migration(self):
        installed = {"kind": "unverified", "product_version": "2.0.0", "commit": "old-manager-deployment"}
        candidate = {"kind": "release", "product_version": "2.0.0", "commit": "later-source-commit"}
        self.assertEqual(compare(installed, candidate, "forward"), "unknown")
        self.assertEqual(compare(installed, {**candidate, "product_version": "2.0.0+other"}, "forward"), "unknown")
        self.assertEqual(compare(installed, {**candidate, "commit": installed["commit"]}), "same")
        self.assertEqual(compare(installed, {**candidate, "kind": "development"}, "forward"), "development")


class ReleaseGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-release-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.name", "Offline Release Fixture")
        self.git(self.repo, "config", "user.email", "offline-release@example.invalid")
        self.git(self.repo, "config", "commit.gpgsign", "false")

    def git(self, repo, *arguments):
        """git() 只操作本测试临时仓库，原样保留非零结果作为调用失败。"""
        completed = subprocess.run(["git", "-C", str(repo), *arguments], env=self.environment,
                                   capture_output=True, check=False)
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode(errors="replace"))
        return completed.stdout

    def commit(self, version=None, *, tag=None, annotated=False):
        """commit() 构造明确有号或无号的测试提交，不把单次提交本身当作发布。"""
        material = self.repo / "engine/release.json"
        if version is not None:
            material.parent.mkdir(exist_ok=True)
            material.write_bytes(declaration(version))
        (self.repo / "source.txt").write_text(self.git(self.repo, "rev-list", "--all").decode() or "initial")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "offline release fixture")
        commit = self.git(self.repo, "rev-parse", "HEAD").decode().strip()
        if tag:
            self.git(self.repo, "tag", *("-a", "-m", "offline annotated release") if annotated else (), tag)
        return commit

    def test_legacy_has_no_invented_product_version_or_build_time(self):
        commit = self.commit()
        expected = {"product_version": None, "built_at": None, "commit": commit, "kind": "legacy", "tag": None}
        self.assertEqual(identity(self.repo, commit, self.git), expected)
        self.assertEqual(select(self.repo, self.git, "HEAD"), {**expected, "ref": "HEAD"})
        with self.assertRaisesRegex(ValueError, "No stable"):
            select(self.repo, self.git)

    def test_exact_tag_and_metadata_match_are_required_for_a_formal_release(self):
        original = self.commit("1.0.0", tag="v1.0.0")
        formal = identity(self.repo, original, self.git)
        self.assertEqual((formal["kind"], formal["tag"]), ("release", "v1.0.0"))
        next_commit = self.commit("1.0.0")
        development = identity(self.repo, next_commit, self.git)
        self.assertEqual((development["kind"], development["tag"]), ("development", None))
        self.assertEqual(select(self.repo, self.git)["commit"], original)
        self.assertEqual(select(self.repo, self.git, "main")["commit"], next_commit)

    def test_annotated_tags_are_peeled_to_actual_commits(self):
        commit = self.commit("2.0.0", tag="v2.0.0", annotated=True)
        tag_object = self.git(self.repo, "rev-parse", "refs/tags/v2.0.0").decode().strip()
        self.assertNotEqual(tag_object, commit)
        chosen = select(self.repo, self.git)
        self.assertEqual((chosen["commit"], chosen["ref"], chosen["kind"]), (commit, "v2.0.0", "release"))

    def test_default_selects_numeric_highest_stable_not_newest_commit_or_preview(self):
        self.commit("4.2.0", tag="v4.2.0")
        stable = self.commit("4.10.0", tag="v4.10.0")
        preview = self.commit("5.0.0-rc.1", tag="v5.0.0-rc.1")
        self.git(self.repo, "tag", "v99.00.0")
        self.assertEqual(select(self.repo, self.git)["commit"], stable)
        selected_preview = select(self.repo, self.git, "v5.0.0-rc.1")
        self.assertEqual((selected_preview["kind"], selected_preview["commit"]), ("prerelease", preview))

    def test_preview_only_source_has_no_default_formal_release(self):
        self.commit("1.0.0-rc.1", tag="v1.0.0-rc.1")
        with self.assertRaisesRegex(ValueError, "No stable"):
            select(self.repo, self.git)

    def test_highest_tag_with_wrong_metadata_cannot_silently_fall_back(self):
        self.commit("1.0.0", tag="v1.0.0")
        self.commit("1.1.0", tag="v2.0.0")
        with self.assertRaisesRegex(ValueError, "does not match"):
            select(self.repo, self.git)

    def test_explicit_version_tags_also_need_matching_metadata(self):
        self.commit("1.0.0", tag="v1.0.0")
        self.git(self.repo, "tag", "v2.0.0")
        for ref in ("v2.0.0", "refs/tags/v2.0.0"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "does not match"):
                select(self.repo, self.git, ref)
        self.git(self.repo, "tag", "v1.00.0")
        with self.assertRaisesRegex(ValueError, "leading zeros"):
            select(self.repo, self.git, "v1.00.0")

    def test_explicit_legacy_tag_does_not_invent_release_metadata(self):
        commit = self.commit(tag="v0.1.0")
        selected = select(self.repo, self.git, "v0.1.0")
        self.assertEqual((selected["kind"], selected["product_version"], selected["commit"]),
                         ("legacy", None, commit))

    def test_moving_a_tag_does_not_make_two_commits_the_same_release_identity(self):
        original = self.commit("1.0.0", tag="v1.0.0")
        saved = identity(self.repo, original, self.git)
        moved = self.commit("1.0.0")
        self.git(self.repo, "tag", "-f", "v1.0.0", moved)
        current = select(self.repo, self.git)
        self.assertEqual(compare(saved, current, "forward"), "conflict")
        self.assertEqual(identity(self.repo, original, self.git)["kind"], "development")

    def test_same_precedence_build_tags_cannot_be_ranked_by_timestamp(self):
        self.commit("1.0.0+20261125090000", tag="v1.0.0+20261125090000")
        self.commit("1.0.0+20261125100000", tag="v1.0.0+20261125100000")
        with self.assertRaisesRegex(ValueError, "same version precedence"):
            select(self.repo, self.git)

    def test_malformed_or_linked_release_declaration_is_not_legacy(self):
        self.commit("1.0.0")
        material = self.repo / "engine/release.json"
        material.write_bytes(declaration("1.00.0"))
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "invalid metadata fixture")
        with self.assertRaises(ValueError):
            identity(self.repo, "HEAD", self.git)
        material.unlink()
        material.symlink_to("../source.txt")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "linked metadata fixture")
        with self.assertRaisesRegex(ValueError, "regular file"):
            identity(self.repo, "HEAD", self.git)


if __name__ == "__main__":
    unittest.main()
