#!/usr/bin/env python3
"""安装仓库本地的 Clarity pre-commit hook；不会执行目标项目或启动语义审核。"""

from __future__ import annotations

# 动态安装会按文件路径加载本模块；只补同版 runner 搜索路径，不依赖宿主技术栈。
import sys
from pathlib import Path
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import tr


import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile


HOOK_MARKER = "# clarity-review-hook v1"
HOOK_NAMES = ("pre-commit",)
ENGINE_RUNNERS = ("review.py", "codex_model.py", "install.py", "rules_check.py", "rules_report.py",
                  "material_check.py", "maintenance.py", "i18n.py")
REQUIRED_PROMPTS = ("health.md", "fixtures.md", "review.md", "summarize.md", "learn.md", "rules-check.md",
                    "material-check.md")
DEFAULT_BASIS_SELECTIONS = (".clarity/current/rules.md", ".clarity/current/cases")
ENGINE_BASIS_ROOT = ".clarity/engine"


class InstallError(Exception):
    """InstallError 表示安装条件不足，调用者可据消息修复后重试。"""


def git_output(repo: Path, *arguments: str) -> str:
    """git_output() 读取 Git 的路径事实，失败时保留可操作的错误消息。"""
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise InstallError(result.stderr.strip() or tr('Git could not read repository information.'))
    return result.stdout.strip()


def repository_paths(requested_repo: Path) -> tuple[Path, Path, Path]:
    """repository_paths() 从仓库根目录询问 Git，统一普通仓库和 worktree 的路径语义。

    hooks 路径由 Git 解释 core.hooksPath；common dir 承载同一仓库共享的引擎。
    相对结果相对于此次 Git 命令的工作目录展开，不自行猜测 .git 的形状。
    """
    repo = Path(git_output(requested_repo.expanduser().resolve(), "rev-parse", "--show-toplevel")).resolve()
    common_value = Path(git_output(repo, "rev-parse", "--git-common-dir"))
    hooks_value = Path(git_output(repo, "rev-parse", "--git-path", "hooks"))
    common_dir = (repo / common_value).resolve() if not common_value.is_absolute() else common_value.resolve()
    hooks_dir = (repo / hooks_value).resolve() if not hooks_value.is_absolute() else hooks_value.resolve()
    return repo, common_dir, hooks_dir


def is_owned_hook(path: Path) -> bool:
    """is_owned_hook() 只认可本安装器的常规脚本标记，避免跟随链接覆盖别人的 hook。"""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return len(lines) >= 2 and lines[0] == "#!/bin/sh" and lines[1] == HOOK_MARKER


def check_hook_ownership(hooks_dir: Path) -> None:
    """check_hook_ownership() 在任何安装写入前检查 pre-commit 所有权，拒绝覆盖其他人的入口。"""
    conflicts = [str(hooks_dir / name) for name in HOOK_NAMES
                 if os.path.lexists(hooks_dir / name) and not is_owned_hook(hooks_dir / name)]
    if conflicts:
        raise InstallError(tr('An existing pre-commit hook is not owned by this installer; it was not overwritten:\n') + "\n".join(conflicts))


def engine_sources(source_root: Path) -> dict[str, Path]:
    """engine_sources() 仅选共享模型、编写原则、流程、角色提示词及运行模块，不复制仓库知识或模板。

    必须的角色文件先逐项验证；其他共享 Markdown 提示词也随同版引擎保存。
    本层只检查依赖可读取，不把文件齐全当作模型审查已完成。
    """
    required = ["README.md", "principles.md", "workflow.md", "runner/locales/aliases.json"]
    required.extend("prompts/" + name for name in REQUIRED_PROMPTS)
    required.extend("runner/" + name for name in ENGINE_RUNNERS)
    sources = {name: source_root / name for name in required}
    for path in (source_root / "prompts").glob("*.md"):
        sources[path.relative_to(source_root).as_posix()] = path
    for path in (source_root / "runner/locales").glob("*.json"):
        sources[path.relative_to(source_root).as_posix()] = path
    for name, path in sources.items():
        if path.is_symlink() or not path.is_file():
            raise InstallError(tr('Installation dependency is missing or is not a regular file: ') + str(path))
        try:
            path.read_bytes()
        except OSError as error:
            raise InstallError(tr('Installation dependency cannot be read: ') + name + ": " + str(error)) from error
    return dict(sorted(sources.items()))


def engine_digest(files: dict[str, bytes]) -> str:
    """engine_digest() 按有序相对路径和文件原文计算引擎版本，供运行器核对输入关系。

    路径和内容之间的零字节边界避免简单连接的歧义；耗时与全部引擎字节数成正比。
    摘要记录安装版本，不把可被本机用户修改的文件说成不可绕过的权限边界。
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return digest.hexdigest()


def basis_files(repo: Path, requested: list[str]) -> list[str]:
    """basis_files() 保留显式选入资料，未选择时只发现目标仓库自己的正式资料位置。

    目录按相对路径展开并去重，忽略占位文件及空文件；不扫描 proposals 或共享 knowledge。
    固定的 .clarity/engine 目录承载流程和元原则，显式选择或父目录展开都不能把它变成产品标准。
    文本是否仍是填写说明、规则是否适用，留给健康检查判断，本层不作语义通过承诺。
    """
    selections = requested or list(DEFAULT_BASIS_SELECTIONS)
    collected: set[str] = set()
    for selection in selections:
        relative = Path(selection)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise InstallError(tr('Basis must be an explicit relative path inside the repository: ') + selection)
        if relative.as_posix() == ENGINE_BASIS_ROOT or relative.as_posix().startswith(ENGINE_BASIS_ROOT + "/"):
            raise InstallError(tr('Engine materials cannot be used as product rules: ') + selection)
        selected = repo / relative
        if selected.is_symlink():
            raise InstallError(tr('Basis path cannot be a symbolic link: ') + selection)
        if not selected.exists():
            if requested:
                raise InstallError(tr('Explicitly selected basis does not exist: ') + selection)
            continue
        try:
            selected.resolve().relative_to(repo)
        except ValueError as error:
            raise InstallError(tr('Basis path is outside the target repository: ') + selection) from error
        candidates = sorted(selected.rglob("*")) if selected.is_dir() else [selected]
        for candidate in candidates:
            name = candidate.relative_to(repo).as_posix()
            if name == ENGINE_BASIS_ROOT or name.startswith(ENGINE_BASIS_ROOT + "/"):
                raise InstallError(tr('Engine materials cannot be used as product rules: ') + name)
            if candidate.is_symlink():
                raise InstallError(tr('Basis directory contains a symbolic link: ') + candidate.relative_to(repo).as_posix())
            if not candidate.is_file() or candidate.name == ".gitkeep":
                continue
            if not candidate.read_bytes().strip():
                continue
            collected.add(candidate.relative_to(repo).as_posix())
    if not collected:
        raise InstallError(tr('No valid target-repository basis files were found; create project .clarity materials or select them with --basis.'))
    return sorted(collected)


def codex_executable(requested: str | None) -> str:
    """codex_executable() 固定实际可执行文件位置，避免 hook 从另一套 PATH 误选调用器。"""
    executable = shutil.which(requested or "codex")
    if not executable:
        raise InstallError(tr('Codex CLI executable was not found; install it or provide --codex-bin PATH.'))
    return str(Path(executable).resolve())


def hook_script(name: str, engine_dir: Path, config_path: Path) -> bytes:
    """hook_script() 生成只转交给同一审核入口的 pre-commit hook，保留 Git 的索引环境。

    仓库根目录运行时从 Git 查询，因此共享 hook 的 worktree 不会固定到安装时的工作区。
    exec 直接继承 GIT_INDEX_FILE，让审核器取得 Git 实际准备提交的快照。
    """
    command = [sys.executable, str(engine_dir / "runner" / "review.py"), "hook", name]
    rendered = " ".join(shlex.quote(part) for part in command)
    script = (
        "#!/bin/sh\n"
        + HOOK_MARKER + "\n"
        + 'repo=$(git rev-parse --show-toplevel) || exit $?\n'
        + "exec " + rendered + ' --repo "$repo" --config '
        + shlex.quote(str(config_path)) + "\n"
    )
    return script.encode("utf-8")


def replace_file(path: Path, content: bytes, mode: int) -> None:
    """replace_file() 在目标目录写临时文件后替换单个文件，避免进程读到半份脚本。"""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".clarity-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def local_basis(repo: Path, selections: list[str], ignored=None) -> dict[str, dict]:
    """local_basis() 冻结明确选择的本机资料，不把未跟踪业务代码混入 Git 审核对象。

    路径成员、原文与模式共同构成快照；删除与清空留给新旧配对表达，链接及二进制不作普通正文。
    尚未改写的案例底稿不是正式 fixture，忽略它不表示已取得案例覆盖；填写后便进入候选集合。
    扫描与散列随选中路径及其正文总量增长，单次冻结的正文上限与审核输入容量一致。
    """
    result, size = {}, 0
    template = Path(__file__).resolve().parents[2] / "initial/cases/case.md"
    untouched_case = template.read_bytes() if template.is_file() else None
    if ignored is None:
        ignored = ({".clarity/current/cases/case.md": hashlib.sha256(untouched_case).hexdigest()}
                   if untouched_case is not None else {})
    for selection in selections:
        relative = Path(selection)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise InstallError(tr('Basis must be an explicit relative path inside the repository: ') + selection)
        selected = repo / relative
        paths = [selected]
        if selected.is_dir() and not selected.is_symlink():
            paths.extend(sorted(selected.rglob("*")))
        for path in paths:
            for parent in (path, *path.parents):
                if parent == repo:
                    break
                if parent.is_symlink():
                    raise InstallError(tr('Basis path cannot be a symbolic link: ') + str(parent))
            if not path.exists() or path.is_dir() or path.name == ".gitkeep":
                continue
            if not path.is_file():
                raise InstallError(tr('Installation dependency is missing or is not a regular file: ') + str(path))
            name = path.relative_to(repo).as_posix()
            if name == ENGINE_BASIS_ROOT or name.startswith(ENGINE_BASIS_ROOT + "/"):
                raise InstallError(tr('Engine materials cannot be used as product rules: ') + name)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() == ignored.get(name):
                continue
            size += len(raw)
            if size > 1_500_000 or b"\0" in raw:
                raise InstallError(tr('Local review materials are binary or exceed 1.5 MB.'))
            result[name] = {"text": raw.decode("utf-8"), "oid": "sha256:" + hashlib.sha256(raw).hexdigest(),
                            "mode": "100755" if path.stat().st_mode & 0o111 else "100644"}
    return dict(sorted(result.items()))


def prepare_review(repo: Path, project: Path | None = None) -> None:
    """prepare_review() 在明确审核时准备缺失或已失效的资料采用记录，不安装提交 hook。

    调用方须持有 running.lock；完整配置原样保留，避免把本轮候选重新采用。
    整个 current 重建后只清除旧采用关系并保留模型偏好，本次明确审核才采用当前本机原文。
    Git 的历史资料不覆盖此次采用；后续改写仍作为候选核对，资料能否使用由实际审核判断。
    """
    common = Path(git_output(repo, "rev-parse", "--git-common-dir"))
    state = (common if common.is_absolute() else repo / common).resolve() / "clarity"
    config_path = state / "config.json"
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    if not isinstance(previous, dict):
        raise InstallError(tr('Model configuration must be a JSON object'))
    if not previous.get("basis_reset") and (previous.get("basis_selections") or previous.get("basis")):
        return
    project = (project or repo).resolve()
    selections = [(project / name).relative_to(repo).as_posix() for name in DEFAULT_BASIS_SELECTIONS]
    rules = project / ".clarity/current/rules.md"
    case_template = project / ".clarity/initial/cases/case.md"
    ignored = ({selections[1] + "/case.md": hashlib.sha256(case_template.read_bytes()).hexdigest()}
               if case_template.is_file() else {})
    materials = local_basis(repo, selections, ignored)
    if not rules.is_file() or not materials.get(selections[0], {}).get("text", "").strip():
        raise InstallError(tr('First review needs project rules. Fill .clarity/current/rules.md, then run make review again.'))
    # 重建资料不是重置审核偏好；旧采用字段不能随偏好一起带回，否则会重新支配刚恢复的 current。
    preserved = {name: value for name, value in previous.items()
                 if name not in {"basis", "basis_hashes", "basis_selections", "basis_source", "basis_snapshot",
                                 "basis_worktree", "basis_ignored", "cases_root", "basis_reset"}}
    preserved["sidecar_project"] = str(project)
    args = argparse.Namespace(repo=str(repo), basis=selections, codex_bin=previous.get("codex_bin"),
                              model=previous.get("model"), reviewers=previous.get("reviewers", 2),
                              timeout=previous.get("timeout", 600))
    install(args, with_hook=False, adopted_local=materials, ignored_local=ignored, preserved_config=preserved)


def install(args: argparse.Namespace, *, with_hook=True, adopted_local=None, ignored_local=None,
            preserved_config=None) -> None:
    """install() 核对完整输入后保存外部引擎及配置，仅显式启用时写入 pre-commit。

    引擎放在 Git common dir 中，与候选 tracked 内容分离；不修改资料、Git 配置或业务代码。
    写入失败时恢复此前引擎、配置与已写 hook；本地拥有者仍可卸载或绕过此入口。
    """
    repo, common_dir, hooks_dir = repository_paths(Path(args.repo))
    if with_hook:
        check_hook_ownership(hooks_dir)
    source_root = Path(__file__).resolve().parent.parent
    files = {name: path.read_bytes() for name, path in engine_sources(source_root).items()}
    # 首次手动审核允许尚无案例目录；仍在配置保留完整选择边界，以便后续新增案例作为候选进入。
    requested = args.basis if with_hook else [name for name in args.basis if (repo / name).exists()]
    adopted_basis = basis_files(repo, requested)
    executable = codex_executable(args.codex_bin)
    clarity_dir = common_dir / "clarity"
    engine_dir = clarity_dir / "engine"
    config_path = clarity_dir / "config.json"
    if engine_dir.is_symlink() or (with_hook and engine_dir.exists() and not config_path.is_file()):
        raise InstallError(tr('Existing engine has no installer configuration or is a symbolic link; overwrite refused: ') + str(engine_dir))
    config = {**(preserved_config or {}),
        "version": 1,
        "basis": adopted_basis,
        # 绑定安装时显式采用的原文字节，运行器据此区分首次资料与同次新增候选资料。
        "basis_hashes": {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
                         for name in adopted_basis},
        # 保存选择边界，让运行器从固定 Git 树发现目录中的新增案例，不依赖安装时文件清单。
        "basis_selections": list(args.basis or DEFAULT_BASIS_SELECTIONS),
        # 两个独立首审用于之后交叉反证；这是默认编排，不是多数票放行门槛。
        "reviewers": args.reviewers,
        "codex_bin": executable,
        # 隔离审核入口不继承用户模型别名；未明确选择时使用已验证可用的 CLI 内置默认。
        "model": args.model,
        # 每次模型调用有边界，超时须报告审核未完成，不能伪装代码失败或通过。
        "timeout": args.timeout,
        "engine_digest": engine_digest(files),
    }
    if adopted_local is not None:
        config.update(basis_source="local", basis_snapshot=adopted_local,
                      basis_worktree=str(repo), basis=sorted(adopted_local))
        config["basis_hashes"] = {name: hashlib.sha256(entry["text"].encode()).hexdigest()
                                  for name, entry in adopted_local.items()}
        config["basis_ignored"] = ignored_local or {}
    if not with_hook:
        config["cases_root"] = args.basis[1].rstrip("/") + "/"
    config_bytes = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    original_hooks = {name: ((hooks_dir / name).read_bytes(), stat.S_IMODE((hooks_dir / name).stat().st_mode))
                      for name in HOOK_NAMES if with_hook and (hooks_dir / name).exists()}
    original_config = config_path.read_bytes() if config_path.exists() else None
    clarity_dir.mkdir(parents=True, exist_ok=True)
    if with_hook:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=clarity_dir) as temporary_name:
        temporary = Path(temporary_name)
        staged_engine = temporary / "engine"
        previous_engine = temporary / "previous-engine"
        for name, content in files.items():
            destination = staged_engine / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        moved_previous = False
        installed_engine = False
        written_hooks: list[str] = []
        try:
            # 再检查一次以缩短首次检查和替换间的窗口，但不声称能锁住其他本机写入者。
            if with_hook:
                check_hook_ownership(hooks_dir)
            if engine_dir.exists():
                os.replace(engine_dir, previous_engine)
                moved_previous = True
            os.replace(staged_engine, engine_dir)
            installed_engine = True
            replace_file(config_path, config_bytes, 0o600)
            for name in HOOK_NAMES if with_hook else ():
                replace_file(hooks_dir / name, hook_script(name, engine_dir, config_path), 0o755)
                written_hooks.append(name)
        except BaseException:
            for name in written_hooks:
                destination = hooks_dir / name
                if name in original_hooks:
                    content, mode = original_hooks[name]
                    replace_file(destination, content, mode)
                else:
                    destination.unlink(missing_ok=True)
            if original_config is None:
                config_path.unlink(missing_ok=True)
            else:
                replace_file(config_path, original_config, 0o600)
            if installed_engine:
                shutil.rmtree(engine_dir)
            if moved_previous:
                os.replace(previous_engine, engine_dir)
            raise
    if with_hook:
        print(tr('Local review pre-commit hook installed: ') + str(hooks_dir / "pre-commit"))
    print(tr('Shared engine and configuration: ') + str(clarity_dir))
    print(tr('Review scope: all current tracked content; installation did not run a model review.'))


def uninstall(args: argparse.Namespace) -> None:
    """uninstall() 只删除仍带本安装器标记的 pre-commit，保留引擎、规则、配置及全部运行记录。"""
    _repo, common_dir, hooks_dir = repository_paths(Path(args.repo))
    removed = []
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if is_owned_hook(hook):
            hook.unlink()
            removed.append(name)
    print(tr('Owned pre-commit hook removed: ') + str(hooks_dir / "pre-commit") + " (" + str(len(removed)) + tr(' hooks)'))
    print(tr('Rules, engine, configuration, and run records were retained: ') + str(common_dir / "clarity"))


def main(argv: list[str] | None = None) -> int:
    """main() 接受显式安装选择并返回非零失败，不把配置成功当作语义审核通过。"""
    parser = argparse.ArgumentParser(description=tr('Install or uninstall the repository-local Clarity code-review pre-commit hook.'))
    parser.add_argument("--repo", required=True, help=tr('Target Git repository directory.'))
    parser.add_argument("--basis", action="append", default=[], metavar="RELPATH", help=tr('Explicit basis file or directory in the target repository; may be repeated.'))
    parser.add_argument("--model", help=tr('Explicit Codex model; otherwise use the CLI built-in default.'))
    parser.add_argument("--codex-bin", metavar="PATH", help=tr('Codex executable; default is resolved from the current PATH.'))
    parser.add_argument("--reviewers", type=int, default=2, help=tr('Independent first reviewers, 2..8; default is 2.'))
    parser.add_argument("--timeout", type=int, default=300, help=tr('Timeout per model call in seconds; default is 300.'))
    parser.add_argument("--uninstall", action="store_true", help=tr('Uninstall only the pre-commit hook owned by this installer.'))
    args = parser.parse_args(argv)
    if not args.uninstall and (not 2 <= args.reviewers <= 8 or args.timeout <= 0):
        parser.error(tr('--reviewers must be 2..8 and --timeout must be greater than 0.'))
    try:
        uninstall(args) if args.uninstall else install(args)
    except (InstallError, OSError) as error:
        print(tr('Clarity review activation did not complete: ') + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
