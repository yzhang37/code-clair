#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1

# 首次入口从项目外取得管理程序；目标始终是当前目录，不要求已有 Clarity，也不创建 Git 仓库。
for clarity_dependency in git curl python3; do
    if ! command -v "$clarity_dependency" >/dev/null 2>&1; then
        printf 'Clarity installation cannot start: %s was not found.\n' "$clarity_dependency" >&2
        exit 2
    fi
done
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    printf '%s\n' 'Clarity installation requires Python 3.10 or newer.' >&2
    exit 2
fi

clarity_target=$(pwd -P)
clarity_staging=$(mktemp -d "${TMPDIR:-/tmp}/clarity-install.XXXXXX")
trap 'rm -rf "$clarity_staging"' 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# 下载标签和对象后只解释版本资料；未发布的 HEAD 不参与执行，首次安装与后续管理共用正式发布规则。
git clone --quiet --no-checkout https://github.com/yzhang37/code-clair.git "$clarity_staging/release"
git -C "$clarity_staging/release" fetch --quiet --tags origin
clarity_commit=$(python3 - "$clarity_staging/release" <<'PY'
from datetime import datetime
import json
import re
import subprocess
import sys


def git(*arguments):
    """git() 仅读取下载副本中的 Git 对象，不启动未选定发布的程序。"""
    return subprocess.check_output(["git", "-C", sys.argv[1], *arguments], stderr=subprocess.PIPE)


def unique_object(pairs):
    """unique_object() 拒绝重复发布字段，避免引导层和管理程序解释出不同身份。"""
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Release metadata contains a duplicate field.")
        record[key] = value
    return record


def reject_constant(value):
    """reject_constant() 拒绝非 JSON 数值，保持发布资料解析与正式管理程序一致。"""
    raise ValueError("Release metadata cannot contain nonfinite numbers.")


def select_manager():
    """select_manager() 先按三段整数选最高稳定标签，再核对同提交的声明和启动文件。

    引导时尚不能加载版本模块，因此这里只识别稳定标签的最小语法；完整版本逻辑由所选管理程序复核。
    无正式发布、同优先级多个标签或发布声明不匹配时停止，不能降级为执行任意 HEAD。
    """
    number = r"(0|[1-9][0-9]*)"
    stable = re.compile(r"v" + number + r"\." + number + r"\." + number
                        + r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
    candidates = []
    for tag in git("for-each-ref", "--format=%(refname:strip=2)", "refs/tags").decode("utf-8").splitlines():
        matched = stable.fullmatch(tag)
        if matched:
            candidates.append((tuple(int(matched[index]) for index in (1, 2, 3)), tag))
    if not candidates:
        raise ValueError("No stable Clarity release was found in the bootstrap source.")
    candidates.sort(key=lambda item: item[0])
    version, tag = candidates[-1]
    if len(candidates) > 1 and candidates[-2][0] == version:
        raise ValueError("Multiple stable release tags have the same version precedence.")
    commit = git("rev-parse", "--verify", "--end-of-options", "refs/tags/" + tag + "^{commit}").decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise ValueError("The selected release did not resolve to a full commit.")
    entries = [entry for entry in git("ls-tree", "-z", commit, "--", "engine/release.json").split(b"\0") if entry]
    if len(entries) != 1:
        raise ValueError("The highest stable release has no unambiguous release metadata.")
    header, name = entries[0].split(b"\t", 1)
    mode, kind, oid = header.decode("ascii").split()
    if name != b"engine/release.json" or kind != "blob" or mode not in {"100644", "100755"}:
        raise ValueError("Release metadata is not a regular file.")
    metadata = json.loads(git("cat-file", "blob", oid).decode("utf-8"),
                          object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(metadata, dict) or metadata.get("product_version") != tag[1:]:
        raise ValueError("The highest stable release tag does not match its committed release metadata.")
    stamp = metadata.get("built_at")
    if not isinstance(stamp, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", stamp) is None:
        raise ValueError("Release built_at must be an ISO 8601 UTC timestamp ending in Z.")
    datetime.fromisoformat(stamp[:-1] + "+00:00")
    required = {b"setup.py", b"engine/runner/release.py", b"engine/runner/i18n.py"}
    found = set()
    for entry in git("ls-tree", "-rz", commit, "--", "setup.py", "engine/runner/release.py",
                     "engine/runner/i18n.py", "engine/runner/locales").split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("A bootstrap manager dependency is not a regular file.")
        found.add(name)
    if not required.issubset(found):
        raise ValueError("The selected stable release lacks bootstrap manager dependencies.")
    return commit


try:
    print(select_manager())
except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as error:
    print("Cannot select a stable Clarity installer: " + str(error), file=sys.stderr)
    raise SystemExit(2)
PY
)
# 所有启动文件来自已核实的同一个 commit；实际宿主的来源与引用参数仍交给统一管理程序处理。
git -C "$clarity_staging/release" checkout --quiet "$clarity_commit" -- \
    setup.py engine/runner/release.py engine/runner/i18n.py engine/runner/locales
python3 "$clarity_staging/release/setup.py" install "$@" --repo "$clarity_target"
