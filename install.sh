#!/bin/sh
set -eu

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

# 先取得发布的实际 commit，再下载不可变地址，避免 main 的下载缓存把新版入口配到旧版管理程序。
clarity_head=$(git ls-remote --exit-code https://github.com/yzhang37/code-clair.git HEAD)
clarity_commit=${clarity_head%%[[:space:]]*}
case "$clarity_commit" in
    ''|*[!0-9a-f]*) printf '%s\n' 'Cannot identify the published Clarity version.' >&2; exit 2 ;;
esac
# 只有完整下载成功才执行；所有安装/升级选择由同一管理程序处理，避免两层提示产生不同决定。
git clone --quiet --no-checkout https://github.com/yzhang37/code-clair.git "$clarity_staging/release"
git -C "$clarity_staging/release" checkout --quiet "$clarity_commit" -- \
    setup.py engine/runner/i18n.py engine/runner/locales
python3 "$clarity_staging/release/setup.py" install "$@" --repo "$clarity_target"
