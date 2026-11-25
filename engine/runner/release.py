"""release.py 保存发布版本的数值语义和 Git 身份，不把构建日期当作产品新旧。"""

from __future__ import annotations

from datetime import datetime
from functools import total_ordering
import json
from pathlib import Path
import re


RELEASE_PATH = "engine/release.json"
_NUMBER = r"(?:0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"({_NUMBER})\.({_NUMBER})\.({_NUMBER})"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@total_ordering
class Version:
    """Version() 表示完整三段 SemVer，排序只使用核心版本和预发布标识。

    构建元数据保留供展示，但不参与相等或先后判断；身份冲突由 commit 另行核对。
    """

    def __init__(self, text: str):
        """__init__() 一次验证各数字段及 ASCII 标识，避免将补零或简写解释成另一套版本。"""
        matched = _VERSION.fullmatch(text) if isinstance(text, str) else None
        if matched is None:
            raise ValueError("Product version must be a complete SemVer with no leading zeros.")
        prerelease = tuple(matched[4].split(".")) if matched[4] else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
            raise ValueError("Numeric prerelease identifiers cannot contain leading zeros.")
        self.text = text
        self.core = tuple(int(matched[index]) for index in (1, 2, 3))
        self.prerelease = prerelease
        self.metadata = tuple(matched[5].split(".")) if matched[5] else ()

    def __str__(self):
        """__str__() 返回已验证的原始版本拼写，包含仅供追溯的构建元数据。"""
        return self.text

    def _precedence(self):
        """_precedence() 用可直接比较的数值键表达 SemVer 的稳定版及预发布优先级。

        先区分稳定版，再逐标识区分数字与文本；元组的前缀规则自然使较短预发布序列在前。
        每次构造按标识总长度计算，不按构建日期或整个版本字符串排序。
        """
        preview = (0, tuple((0, int(part)) if part.isdigit() else (1, part)
                            for part in self.prerelease)) if self.prerelease else (1,)
        return self.core, preview

    def __eq__(self, other):
        """__eq__() 按 SemVer 优先级比较，允许构建元数据不同而发布版本优先级相同。"""
        if not isinstance(other, Version):
            return NotImplemented
        return self.core == other.core and self.prerelease == other.prerelease

    def __lt__(self, other):
        """__lt__() 按三段整数、预发布标识判断先后，不把 4.10 排到 4.2 前面。"""
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() < other._precedence()


def _unique_object(pairs):
    """_unique_object() 拒绝同一发布记录的重复字段，避免不同读取者认成不同发布身份。"""
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Release metadata contains a duplicate field.")
        result[name] = value
    return result


def _reject_constant(value):
    """_reject_constant() 拒绝 JSON 之外的非有限数字，避免把不同解析器的扩展当作发布记录。"""
    raise ValueError("Release metadata cannot contain nonfinite numbers.")


def parse_release(raw: bytes | None) -> dict | None:
    """parse_release() 读取唯一发布声明；缺文件表示旧版，存在但错误的声明不能当作旧版略过。

    构建时间只接受带 Z 的 UTC 时间并保留原值；安装时间、机器时区和本次查询时间均不填入它。
    """
    if raw is None:
        return None
    if not isinstance(raw, bytes):
        raise ValueError("Release metadata must be UTF-8 JSON bytes.")
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Release metadata must be valid UTF-8 JSON.") from error
    if not isinstance(record, dict):
        raise ValueError("Release metadata must be a JSON object.")
    version = record.get("product_version")
    Version(version)
    built_at = record.get("built_at")
    if not isinstance(built_at, str) or _UTC_TIME.fullmatch(built_at) is None:
        raise ValueError("Release built_at must be an ISO 8601 UTC timestamp ending in Z.")
    try:
        datetime.fromisoformat(built_at[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("Release built_at is not a valid UTC timestamp.") from error
    return {"product_version": version, "built_at": built_at}


def _resolve(cache: Path, ref: str, git) -> str:
    """_resolve() 由 Git 解析实际提交并剥离注释标签，不把标签对象的 hash 当成程序提交。"""
    commit = git(cache, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode("ascii").strip()
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("Git did not return a full commit identity.")
    return commit


def _tags(cache: Path, git) -> list[str]:
    """_tags() 只列已有标签名，保持下载、来源选择和缓存刷新由外层安装器负责。"""
    return git(cache, "for-each-ref", "--format=%(refname:strip=2)", "refs/tags").decode("utf-8").splitlines()


def identity(cache: Path, commit: str, git) -> dict:
    """identity() 将所选提交、工厂声明和精确版本标签相互核对，区分正式、预览、开发与旧版。

    未打对应标签的提交即使沿用上次版本文件，也只是开发构建；没有版本文件时保留未知字段。
    本函数只读已有 Git 对象，不能承诺标签在远端未被移动；安装记录的旧 commit 用于之后比较。
    """
    commit = _resolve(cache, commit, git)
    result = {"product_version": None, "built_at": None, "commit": commit, "kind": "legacy", "tag": None}
    entries = git(cache, "ls-tree", "-z", commit, "--", RELEASE_PATH).split(b"\0")
    entries = [entry for entry in entries if entry]
    if not entries:
        return result
    if len(entries) != 1:
        raise ValueError("Selected commit has ambiguous release metadata.")
    header, name = entries[0].split(b"\t", 1)
    mode, kind, oid = header.decode("ascii").split()
    if name.decode("utf-8") != RELEASE_PATH or kind != "blob" or mode not in {"100644", "100755"}:
        raise ValueError("Release metadata is not a regular file.")
    material = parse_release(git(cache, "cat-file", "blob", oid))
    result.update(material, kind="development")
    tag = "v" + material["product_version"]
    if tag in _tags(cache, git) and _resolve(cache, "refs/tags/" + tag, git) == commit:
        result.update(kind="prerelease" if Version(material["product_version"]).prerelease else "release", tag=tag)
    return result


def select(cache: Path, git, ref: str | None = None) -> dict:
    """select() 选择显式 Git 引用，或按 SemVer 数值找最高稳定标签，并核对其发布声明。

    默认不退回分支 HEAD，也不因最高标签损坏而悄悄安装次高版；相同优先级的多个稳定标签须消歧。
    标签共解析一次版本，排序成本为 O(T log T) 次版本比较；只有最高候选才读取完整发布声明。
    """
    if ref is not None:
        if not isinstance(ref, str) or not ref:
            raise ValueError("Git reference must be a nonempty string.")
        result = identity(cache, _resolve(cache, ref, git), git)
        tag = ref.removeprefix("refs/tags/")
        # 任意分支和旧版引用仍可选；已有版本声明时，显式版本标签也必须与声明一致。
        if result["product_version"] is not None and tag in _tags(cache, git) and tag.startswith("v") and tag[1:2].isdigit():
            tagged_version = Version(tag[1:])
            if result["tag"] != tag or result["product_version"] != str(tagged_version):
                raise ValueError("The selected release tag does not match its committed release metadata.")
        return {**result, "ref": ref}
    candidates = []
    for tag in _tags(cache, git):
        if not tag.startswith("v"):
            continue
        try:
            version = Version(tag[1:])
        except ValueError:
            continue
        if not version.prerelease:
            candidates.append((version, tag))
    if not candidates:
        raise ValueError("No stable Clarity release was found in the selected source.")
    candidates.sort(key=lambda candidate: candidate[0])
    version, tag = candidates[-1]
    if len(candidates) > 1 and candidates[-2][0] == version:
        raise ValueError("Multiple stable release tags have the same version precedence.")
    result = identity(cache, _resolve(cache, "refs/tags/" + tag, git), git)
    if result["kind"] != "release" or result["tag"] != tag or result["product_version"] != str(version):
        raise ValueError("The highest stable release tag does not match its committed release metadata.")
    return {**result, "ref": tag}


def compare(installed: dict, candidate: dict, relation: str | None = None) -> str:
    """compare() 只给出候选相对已安装版本的事实，部署或显式降级由外层安装器决定。

    已知版本号较低的候选一律算旧版，不以发布证明缺失绕过降级判断；两个正式身份的同号异提交是冲突。
    缺少正式证明的同号异提交不推断为新发布；开发或无号旧版的其余判断使用外层确认的提交关系。
    forward/backward 以候选相对已安装提交为方向，时间戳不进入比较，也不从未知身份编造版本。
    """
    if relation not in {None, "forward", "backward", "diverged", "unknown"}:
        raise ValueError("Unknown candidate commit relation.")
    installed_version = Version(installed["product_version"]) if installed.get("product_version") is not None else None
    candidate_version = Version(candidate["product_version"]) if candidate.get("product_version") is not None else None
    installed_formal = installed.get("kind") in {"release", "prerelease"} and installed_version is not None
    candidate_formal = candidate.get("kind") in {"release", "prerelease"} and candidate_version is not None
    # 旧管理器可能已部署有号程序却未保存标签证明；已知的较低版本不能因此伪装成首次迁移。
    if installed_version is not None and candidate_version is not None and candidate_version < installed_version:
        return "older"
    same_commit = bool(installed.get("commit")) and installed.get("commit") == candidate.get("commit")
    if same_commit:
        if installed_formal and candidate_formal and installed["product_version"] != candidate["product_version"]:
            return "conflict"
        return "same"
    if installed_formal and candidate_formal:
        if candidate_version == installed_version:
            return "conflict" if installed.get("commit") and candidate.get("commit") else "unknown"
        return "newer" if installed_version < candidate_version else "older"
    if installed.get("kind") == "unverified" and candidate_formal and installed_version == candidate_version:
        return "unknown"
    if relation == "backward":
        return "older"
    if relation == "diverged":
        return "diverged"
    if relation == "forward":
        return "migration" if candidate_formal else "development"
    return "unknown"
