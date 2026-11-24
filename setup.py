#!/usr/bin/env python3
"""setup.py 从原厂 Git 版本安装或更新宿主 sidecar，保留项目自己的 current。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile


# 兼容旧版下载入口只取得 setup.py 的情况；界面资源尚未取得时以英文原始消息回退。
try:
    from engine.runner.i18n import tr
except ModuleNotFoundError:
    def tr(message: str, **values) -> str:
        """tr() 在独立启动尚无界面包时回退英文，不改变安装选择或资料内容。"""
        return message.format(**values) if values else message


# 普通宿主用户只维护自己的项目；首次安装从原厂 HTTPS 获取版本，避免要求他们另养源码工作树。
# 维护者及需要本地同步的用户仍可用 --source 明确选入自己的 Git 仓库，既有安装继续沿用其已选来源。
DEFAULT_SOURCE = "https://github.com/yzhang37/code-clair.git"


class SetupError(Exception):
    """SetupError 表示交付条件不足，不代表目标项目代码有问题。"""


def git(directory: Path, *arguments: str) -> bytes:
    """git() 用显式目录取得版本事实，不继承提交时临时索引或其他仓库的路径覆盖。"""
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(["git", "-C", str(directory), *arguments],
                            capture_output=True, env=environment)
    if result.returncode:
        raise SetupError(result.stderr.decode(errors="replace").strip() or tr('Git operation failed.'))
    return result.stdout


def repository_paths(requested: Path) -> tuple[Path, Path, Path | None]:
    """repository_paths() 固定用户指定目录；Git 只提供本地状态位置，不改变安装目标或创建仓库。

    已有 Git 根目录沿用原来的控制目录，以保留审核配置和历史；其他目录把本机状态留在 sidecar 内。
    """
    repo = requested.expanduser().resolve()
    if not repo.is_dir():
        raise SetupError(tr('Installation target directory does not exist: ') + str(repo))
    root = git_root(repo)
    if root != repo:
        return repo, repo / ".clarity/.local", None
    values = [Path(git(repo, "rev-parse", *arguments).decode().strip())
              for arguments in (("--git-common-dir",), ("--git-path", "hooks"))]
    common, hooks = [(value if value.is_absolute() else repo / value).resolve() for value in values]
    local = repo / ".clarity/.local"
    state = local if (local / "installation.json").is_file() else common / "clarity"
    return repo.resolve(), state, hooks


def git_root(target: Path) -> Path | None:
    """git_root() 只检测当前目录所属 Git 工作树，不把没有 Git 当作软件无法安装。"""
    try:
        return Path(git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    except SetupError:
        return None


def existing_installation(target: Path) -> bool:
    """existing_installation() 按程序、模型和模板的常规文件结构识别已有安装，不用目录是否为空判定。"""
    sidecar = target / ".clarity"
    required = ("engine/README.md", "engine/principles.md", "engine/workflow.md",
                "engine/runner/install.py", "engine/runner/review.py", "engine/runner/codex_model.py",
                "initial/rules.md", "initial/cases/case.md")
    return (not sidecar.is_symlink() and all((sidecar / name).is_file()
            and not (sidecar / name).is_symlink() for name in required))


def ask(message: str, choices: dict[str, str], default: str) -> str:
    """ask() 在终端读取一次明确选择；回车或非交互输入结束采用显示的默认值。

    下载入口可能占用标准输入，因此优先从控制终端读取；没有控制终端时仍支持管道和自动化输入。
    """
    try:
        terminal = open("/dev/tty", "r", encoding="utf-8")
    except OSError:
        terminal = None
    try:
        while True:
            print(message, end="", flush=True)
            answer = (terminal.readline() if terminal else sys.stdin.readline()).strip().lower()
            if not answer:
                return default
            if answer in choices:
                return choices[answer]
            print(tr('Enter one of the displayed choices, or press Enter for the default.'))
    finally:
        if terminal:
            terminal.close()


def sidecar_ignore() -> bytes:
    """sidecar_ignore() 将本机缓存和运行记录排除在跟踪之外，Git 以后才建立时也不会误收这些状态。"""
    return b"/.local/\n__pycache__/\n*.py[cod]\n.DS_Store\n"


def read_file(path: Path) -> tuple[bytes, int] | None:
    """read_file() 只取得常规文件的原文字节和权限，拒绝链接及目录占用文件接点。"""
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise SetupError(tr('Not a manageable regular file: ') + str(path))
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def safe_parents(path: Path) -> None:
    """safe_parents() 拒绝沿已有符号链接写入，防止普通 sidecar 路径实际指向外部资料。"""
    for parent in path.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise SetupError(tr('Installation path traverses a link or non-directory: ') + str(parent))


def fingerprint(value: tuple[bytes, int]) -> dict:
    """fingerprint() 将正文与权限一并登记，使本地改动不会在下次更新时被静默覆盖。"""
    content, mode = value
    return {"sha256": hashlib.sha256(content).hexdigest(), "mode": mode}


def encoded(value: dict) -> bytes:
    """encoded() 保存可直接阅读的安装事实，不把记录写成新的规则或审核依据。"""
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def factory_path(name: str) -> bool:
    """factory_path() 限定原厂拥有的文件；current 和宿主 Makefile 不进入原厂部署清单。"""
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts
            and (name in {"setup.py", "README.md", "update"} or name.startswith(("engine/", "initial/"))))


def snapshot(cache: Path, commit: str) -> dict[str, tuple[bytes, int]]:
    """snapshot() 从固定 commit 读取发行文件，排除原厂自己的 current、缓存和宿主入口。

    只接受普通 Git blob；工作树未提交修改和符号链接都不会混入这份版本。
    """
    files = {}
    for entry in git(cache, "ls-tree", "-rz", commit).split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not factory_path(name):
            continue
        mode, kind, oid = header.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise SetupError(tr('Release entry is not a regular file: ') + name)
        files[name] = (git(cache, "cat-file", "blob", oid), 0o755 if mode == "100755" else 0o644)
    return files


def source_cache(state: Path, source: str) -> Path:
    """source_cache() 按来源隔离下载副本，显式换源失败时仍保留原来源和可运行部署。

    旧版单一 source 缓存只在原始 origin 与所选来源一致时复用；不修改它的 remote 来冒充新来源。
    比较原始配置而非 Git 重写后的传输地址，使用户的 HTTPS 代理或 insteadOf 设置不改变登记来源。
    """
    legacy = state / "source"
    safe_parents(legacy / "HEAD")
    if legacy.exists():
        if git(legacy, "rev-parse", "--is-bare-repository").strip() != b"true":
            raise SetupError(tr('Legacy upstream cache is not a bare Git repository: ') + str(legacy))
        legacy_source = git(legacy, "config", "--get", "remote.origin.url").decode().strip()
    else:
        legacy_source = None
    if legacy_source == source:
        cache = legacy
    else:
        cache = state / "sources" / hashlib.sha256(source.encode("utf-8")).hexdigest()
        safe_parents(cache / "HEAD")
        cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        if git(cache, "rev-parse", "--is-bare-repository").strip() != b"true":
            raise SetupError(tr('Upstream cache is not a bare Git repository: ') + str(cache))
        if git(cache, "config", "--get", "remote.origin.url").decode().strip() != source:
            raise SetupError(tr('Upstream source differs from the existing cache; the source was not switched.'))
        git(cache, "fetch", "--quiet", "--prune", "origin")
    else:
        with tempfile.TemporaryDirectory(prefix=".source-", dir=state) as temporary:
            staged = Path(temporary) / "source"
            git(state, "clone", "--quiet", "--mirror", "--", source, str(staged))
            staged.replace(cache)
    return cache


def load_module(name: str, path: Path):
    """load_module() 加载这次准备交付的版本，避免用另一版本的部署清单或 Make 模板。"""
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise SetupError(tr('Cannot load installation dependency: ') + str(path))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def materialize(root: Path, files: dict[str, tuple[bytes, int]]) -> None:
    """materialize() 在临时目录准备完整版本，交付前先验证共享引擎清单能正常读取。"""
    for name, (content, mode) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)


def legacy_files(sidecar: Path) -> dict[str, tuple[bytes, int]]:
    """legacy_files() 收集未登记旧安装的程序和模板，不取得 current 的所有权。

    Python 缓存和 Finder 元数据不是原厂文件，不删除也不当作可覆盖文件。
    """
    files = {}
    for base in (sidecar / "engine", sidecar / "initial", sidecar / "README.md", sidecar / "setup.py",
                 sidecar / "Makefile", sidecar / ".gitignore", sidecar / "update"):
        if not os.path.lexists(base):
            continue
        safe_parents(base / "placeholder" if base.is_dir() else base)
        paths = base.rglob("*") if base.is_dir() else [base]
        for path in paths:
            if "__pycache__" in path.parts or path.name == ".DS_Store":
                continue
            if path.is_symlink():
                raise SetupError(tr('Legacy installation contains a link; ownership was not adopted: ') + str(path))
            if path.is_file():
                files[path.relative_to(sidecar).as_posix()] = read_file(path)
    return files


def adopt_legacy(cache: Path, commit: str, existing: dict) -> tuple[dict, bytes | None]:
    """adopt_legacy() 仅接管与原厂某个历史版本逐文件一致的旧程序，保留可证明的所有权边界。

    首次迁移最多核对最近 200 个祖先；无匹配便失败，不用 hook 标记授权覆盖任意本地改动。
    已生成的 Makefile 还须与同一历史版本的生成器逐字节及权限匹配，不能仅凭管理标记认领。
    """
    if not existing:
        return {}, None
    ancestors = git(cache, "rev-list", "--max-count=200", commit).decode().splitlines()
    for ancestor in ancestors:
        previous = snapshot(cache, ancestor)
        if all(previous.get(name) == value for name, value in existing.items() if name not in {"Makefile", ".gitignore"}):
            if ".gitignore" in existing:
                with tempfile.TemporaryDirectory(prefix=".legacy-", dir=cache.parent) as temporary:
                    root = Path(temporary)
                    materialize(root, previous)
                    old_setup = load_module("clarity_legacy_setup", root / "setup.py")
                    if not hasattr(old_setup, "sidecar_ignore") or existing[".gitignore"] != (old_setup.sidecar_ignore(), 0o644):
                        continue
            if "Makefile" in existing:
                if "engine/runner/make_integration.py" not in previous:
                    continue
                with tempfile.TemporaryDirectory(prefix=".legacy-", dir=cache.parent) as temporary:
                    root = Path(temporary)
                    materialize(root, previous)
                    make = load_module("clarity_legacy_make", root / "engine/runner/make_integration.py")
                    if existing["Makefile"] != (make.rendered_sidecar_makefile(), 0o644):
                        continue
            listing = git(cache, "ls-tree", ancestor, "--", "Makefile")
            legacy_makefile = git(cache, "show", ancestor + ":Makefile") if listing else None
            if legacy_makefile and b".clarity/engine/runner/" not in legacy_makefile:
                legacy_makefile = None
            return existing, legacy_makefile
    raise SetupError(tr('Existing .clarity files match none of the last 200 upstream versions; local files were not overwritten.'))


def installed_files(sidecar: Path, manifest: dict) -> dict:
    """installed_files() 核对上次登记的每份原厂文件，修改、删除或权限变化都须先由所有者处理。"""
    files = {}
    for name, expected in manifest["files"].items():
        if not factory_path(name) and name not in {"Makefile", ".gitignore"}:
            raise SetupError(tr('Installation record contains an out-of-scope file: ') + name)
        path = sidecar / name
        safe_parents(path)
        value = read_file(path)
        if value is None or fingerprint(value) != expected:
            raise SetupError(tr('Factory file has local changes; it was not overwritten: ') + str(path))
        files[name] = value
    return files


def write_file(path: Path, value: tuple[bytes, int]) -> None:
    """write_file() 用同目录临时文件替换单份正文，避免正常读者取得半份文件。"""
    content, mode = value
    descriptor, temporary_name = tempfile.mkstemp(prefix=".clarity-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_transaction(state: Path, changes: dict[Path, tuple[bytes, int] | None]) -> None:
    """apply_transaction() 先保存全部原文再替换文件；异常时恢复已触及的文件和新建目录。

    多份文件不是一次文件系统原子切换，因此先持有审核共用锁；独立质量检查仍会自行核对输入变化。
    回滚失败保留磁盘备份，强制终止后的残留也要求人工核对，不把旧锁按年龄自动删除。
    """
    originals = {}
    for path, value in changes.items():
        safe_parents(path)
        original = read_file(path)
        if original != value:
            originals[path] = original
    if not originals:
        return
    backup = Path(tempfile.mkdtemp(prefix="install-backup-", dir=state))
    records = []
    for index, (path, original) in enumerate(originals.items()):
        records.append({"path": str(path), "backup": str(index) if original is not None else None,
                        "mode": original[1] if original is not None else None})
        if original is not None:
            (backup / str(index)).write_bytes(original[0])
    (backup / "files.json").write_bytes(encoded({"files": records}))
    touched = []
    created_directories = []
    try:
        for path, original in originals.items():
            if read_file(path) != original:
                raise SetupError(tr('File changed during installation: ') + str(path))
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            touched.append(path)
            if changes[path] is None:
                path.unlink()
            else:
                write_file(path, changes[path])
        # 安装记录最后写入，但它仍须与所有实际正文一起回读，不能只凭写操作返回成功。
        for path in originals:
            if read_file(path) != changes[path]:
                raise SetupError(tr('Installation read-back differs: ') + str(path))
    except BaseException as error:
        try:
            for path in reversed(touched):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    write_file(path, original)
            for directory in reversed(created_directories):
                directory.rmdir()
        except BaseException as rollback_error:
            raise SetupError(tr('Recovery did not complete; original-file backups were retained at: ') + str(backup)) from rollback_error
        shutil.rmtree(backup)
        raise error
    shutil.rmtree(backup)


@contextmanager
def installation_lock(state: Path):
    """installation_lock() 与代码审核、组合维护共用排他锁，避免替换运行中的共享引擎。"""
    state.mkdir(parents=True, exist_ok=True)
    if list(state.glob("install-backup-*")):
        raise SetupError(tr('Backups from an earlier installation remain; inspect recovery status first: ') + str(state))
    lock = state / "running.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SetupError(tr('A review, maintenance, or installation is running, or an old lock needs inspection: ') + str(lock)) from error
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def runtime_changes(state: Path, hooks: Path, staged: Path, manifest: dict | None) -> dict:
    """runtime_changes() 更新已启用安装的运行快照与自己的 hook，完整保留审核配置中的其他字段。

    basis_hashes 表示所有者曾采用的原文，重算会把软件更新变成重新采用依据，因此只更新 engine_digest。
    无配置的首次安装只部署软件和底稿，不能以未填模板自动启用提交审核。
    """
    changes = {}
    config_path = state / "config.json"
    config_file = read_file(config_path)
    runtime = state / "engine"
    if config_file is None:
        if runtime.exists():
            raise SetupError(tr('Existing runtime engine has no configuration; it was not overwritten: ') + str(runtime))
        return changes
    config = json.loads(config_file[0])
    safe_parents(runtime / "runner/install.py")
    old = load_module("clarity_old_install", runtime / "runner/install.py")
    old_files = {name: read_file(path) for name, path in old.engine_sources(runtime).items()}
    actual = old.engine_digest({name: value[0] for name, value in old_files.items()})
    if config.get("engine_digest") != actual:
        raise SetupError(tr('Existing runtime engine and configuration digest differ; nothing was overwritten automatically.'))
    new = load_module("clarity_new_install", staged / "engine/runner/install.py")
    new_files = {name: read_file(path) for name, path in new.engine_sources(staged / "engine").items()}
    for name in old_files.keys() | new_files.keys():
        if name not in old_files and read_file(runtime / name) is not None:
            raise SetupError(tr('A new runtime-engine path is occupied by a local file: ') + str(runtime / name))
        changes[runtime / name] = new_files.get(name)
    updated_config = dict(config)
    updated_config["engine_digest"] = new.engine_digest({name: value[0] for name, value in new_files.items()})
    changes[config_path] = (encoded(updated_config), config_file[1])
    for name in ("commit-msg", "pre-commit"):
        path = hooks / name
        value = read_file(path)
        if value is None or not old.is_owned_hook(path):
            continue
        if name == "commit-msg":
            old_hook = read_file(Path(manifest["worktree_root"]) / ".clarity/engine/runner/commit-msg") if manifest else None
            expected = manifest.get("hooks", {}).get(name) if manifest else None
            if expected is not None:
                matches = fingerprint(value) == expected
            else:
                matches = old_hook is not None and value[0] == old_hook[0]
            if not matches:
                raise SetupError(tr('Existing commit hook has local changes or no verified source; it was not overwritten: ') + str(path))
            changes[path] = (read_file(staged / "engine/runner/commit-msg")[0], value[1])
        else:
            if value[0] != old.hook_script(name, runtime, config_path):
                raise SetupError(tr('Existing pre-commit has local changes; it was not overwritten: ') + str(path))
            changes[path] = (new.hook_script(name, runtime, config_path), value[1])
    return changes


def local_exclude(target: Path, root: Path, ignored: bool = True) -> tuple[Path, tuple[bytes, int]]:
    """local_exclude() 管理此 sidecar 的本地忽略行；用户选择加入跟踪时移除此行，其他规则保持原样。"""
    value = Path(git(root, "rev-parse", "--git-path", "info/exclude").decode().strip())
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    relative = (target / ".clarity").relative_to(root).as_posix()
    if "\n" in relative or "\r" in relative:
        raise SetupError(tr('Git local exclusions do not support installation paths containing line breaks.'))
    pattern = "/" + "".join("\\" + ch if ch in "\\*?[] !#" else ch for ch in relative) + "/"
    original = read_file(path)
    content, mode = original if original else (b"", 0o644)
    line = pattern.encode() + b"\n"
    if not ignored:
        content = b"".join(part for part in content.splitlines(keepends=True) if part.rstrip(b"\r\n") != line.rstrip(b"\n"))
    elif line.rstrip(b"\n") not in content.splitlines():
        content += (b"" if not content or content.endswith(b"\n") else b"\n") + line
    return path, (content, mode)


@contextmanager
def tracking_index(root: Path, target: Path, state: Path, changes: dict, host_make=None, make=None, legacy=None):
    """tracking_index() 只暂存用户选中的 sidecar，保留其他文件的暂存状态且不创建提交。

    新索引在临时文件中按即将交付的字节准备，再纳入同一次回滚事务；Git 索引锁阻止并发暂存覆盖。
    本机缓存不进入清单；Makefile 从原暂存版本仅加入安装入口，不夹带其工作树业务改动。
    """
    raw = Path(git(root, "rev-parse", "--git-path", "index").decode().strip())
    index = raw if raw.is_absolute() else root / raw
    safe_parents(index)
    lock = index.with_name(index.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SetupError(tr('Git is staging another operation; its index was not changed.')) from error
    os.close(descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix=".index-", dir=state) as temporary:
            staged = Path(temporary) / "index"
            original = read_file(index)
            if original:
                staged.write_bytes(original[0])
            environment = dict(os.environ)
            for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
                environment.pop(name, None)
            environment["GIT_INDEX_FILE"] = str(staged)

            def index_git(*arguments, data=None):
                """index_git() 只向临时索引送入确定字节，不从尚未写入的工作树重新取材。"""
                result = subprocess.run(["git", "-C", str(root), *arguments], input=data,
                                        capture_output=True, env=environment)
                if result.returncode:
                    raise SetupError(result.stderr.decode(errors="replace").strip())
                return result.stdout

            if not original:
                index_git("read-tree", "--empty")
            entries = {path: value for path, value in changes.items()
                       if path.is_relative_to(target / ".clarity")
                       and not path.is_relative_to(target / ".clarity/.local")}
            current = target / ".clarity/current"
            if current.is_dir():
                for path in current.rglob("*"):
                    if path.is_file() and path not in entries and "__pycache__" not in path.parts and path.name != ".DS_Store":
                        entries[path] = read_file(path)
            if host_make is not None:
                relative = host_make.relative_to(root).as_posix()
                indexed = index_git("ls-files", "--stage", "--", relative)
                if indexed and (len(indexed.splitlines()) != 1 or indexed.split(b"\t", 1)[0].split()[2] != b"0"):
                    raise SetupError(tr('Host Makefile has unresolved Git conflicts; it was not staged automatically.'))
                content = index_git("show", ":" + relative) if indexed else None
                mode = 0o755 if indexed.startswith(b"100755 ") else 0o644
                entries[host_make] = (make.host_makefile(content, legacy), mode)
            commands = []
            for path, value in entries.items():
                relative = path.relative_to(root).as_posix()
                content, mode = value if value else (b"", 0)
                oid = index_git("hash-object", "-w", "--stdin", data=content).decode().strip()
                git_mode = ("100755" if mode & 0o111 else "100644") if value else "0"
                commands.append((git_mode + " " + oid + "\t" + relative).encode() + b"\0")
            index_git("update-index", "-z", "--index-info", data=b"".join(commands))
            changes[index] = (staged.read_bytes(), original[1] if original else 0o644)
            yield
    finally:
        lock.unlink(missing_ok=True)


def install(args: argparse.Namespace) -> dict:
    """install() 从固定 Git 版本准备原厂文件，再一次交付程序、Make 入口、运行快照及版本记录。

    current 已存在时从不参与替换；只有首次 install 缺少该目录时才从同版本 initial 创建。
    同一共享 Git 控制目录目前只绑定一个已登记工作树，拒绝另一工作树悄悄替换共享运行版本。
    """
    repo, state, hooks = repository_paths(Path(args.repo))
    root = git_root(repo)
    already_tracked = bool(git(root, "ls-files", "--", (repo / ".clarity").relative_to(root).as_posix())) if root else False
    review_state = state
    if root == repo:
        common = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
        review_state = (common if common.is_absolute() else root / common).resolve() / "clarity"
    sidecar = repo / ".clarity"
    safe_parents(sidecar / "setup.py")
    safe_parents(state / "installation.json")
    with ExitStack() as locks:
        locks.enter_context(installation_lock(state))
        if review_state != state and (review_state / "config.json").is_file():
            locks.enter_context(installation_lock(review_state))
        manifest_path = state / "installation.json"
        previous_record = read_file(manifest_path)
        manifest = json.loads(previous_record[0]) if previous_record else None
        if manifest and (manifest.get("version") != 1 or manifest.get("worktree_root") != str(repo)):
            raise SetupError(tr('Unsupported installation record or a different bound worktree; shared installation was not replaced.'))
        legacy = legacy_files(sidecar) if manifest is None else None
        if args.command == "update" and manifest is None and not legacy:
            raise SetupError(tr('No sidecar is installed; run install first.'))
        source = args.source or (manifest["source"] if manifest else DEFAULT_SOURCE)
        if Path(source).expanduser().exists():
            source = str(Path(source).expanduser().resolve())
        ref = args.ref or (manifest["ref"] if manifest else "HEAD")
        cache = source_cache(state, source)
        commit = git(cache, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
        files = snapshot(cache, commit)
        required = {"setup.py", "README.md", "initial/rules.md", "initial/cases/case.md",
                    "engine/runner/install.py", "engine/runner/make_integration.py", "engine/runner/commit-msg"}
        if not required.issubset(files):
            raise SetupError(tr('Selected version lacks required installation files: ') + ", ".join(sorted(required - files.keys())))
        if manifest:
            owned = installed_files(sidecar, manifest)
            legacy_makefile = None
        else:
            owned, legacy_makefile = adopt_legacy(cache, commit, legacy)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=state) as temporary:
            staged = Path(temporary)
            materialize(staged, files)
            new_install = load_module("clarity_release_install", staged / "engine/runner/install.py")
            new_install.engine_sources(staged / "engine")
            make = load_module("clarity_release_make", staged / "engine/runner/make_integration.py")
            files["Makefile"] = (make.rendered_sidecar_makefile(), 0o644)
            files[".gitignore"] = (sidecar_ignore(), 0o644)
            changes = {}
            for name in owned.keys() | files.keys():
                path = sidecar / name
                safe_parents(path)
                if name not in owned and read_file(path) is not None:
                    raise SetupError(tr('A new factory path is occupied by a local file: ') + str(path))
                changes[path] = files.get(name)
            make_name = manifest.get("host_makefile") if manifest else None
            promote = args.command == "install" and getattr(args, "track", None) == "yes" and root is not None
            names = {entry.name for entry in repo.iterdir()}
            effective = next((name for name in ("GNUmakefile", "makefile", "Makefile") if name in names), "Makefile")
            original_make = read_file(repo / effective)
            # 审核入口与 Git 归属分开：默认不跟踪仍须可直接运行 make review；只有 promote 才改索引。
            if make_name and effective != make_name:
                raise SetupError(tr('The effective host Makefile has changed; check the original entry first.'))
            make_name = effective
            host_make = repo / make_name
            changes[host_make] = (make.host_makefile(original_make[0] if original_make else None, legacy_makefile),
                                  original_make[1] if original_make else 0o644)
            current = sidecar / "current"
            if os.path.lexists(current):
                if current.is_symlink() or not current.is_dir():
                    raise SetupError(tr('current is not a regular directory; it was not initialized: ') + str(current))
            elif args.command == "install":
                for name, value in files.items():
                    if name.startswith("initial/"):
                        changes[current / name.removeprefix("initial/")] = value
            # 旧迁移仍以当前宿主为 hook 来源，只有逐文件核对成功后才允许刷新已启用快照。
            hook_manifest = manifest or {"worktree_root": str(repo)}
            if hooks is not None:
                changes.update(runtime_changes(review_state, hooks, staged, hook_manifest))
            hook_records = {}
            for name in ("commit-msg", "pre-commit"):
                if hooks is None:
                    continue
                path = hooks / name
                value = changes.get(path, read_file(path))
                if value is not None and new_install.is_owned_hook(path):
                    hook_records[name] = fingerprint(value)
            record = {"version": 1, "worktree_root": str(repo), "source": source, "ref": ref,
                      "commit": commit, "previous_commit": manifest["commit"] if manifest else None,
                      "previous_source": manifest["source"] if manifest else None,
                      "installed_at": datetime.now(timezone.utc).isoformat(), "host_makefile": make_name,
                      "track_git": manifest.get("track_git", already_tracked) if args.command == "update" and manifest else bool(promote or already_tracked),
                      "files": {name: fingerprint(value) for name, value in sorted(files.items())},
                      "hooks": hook_records}
            changes[manifest_path] = (encoded(record), 0o600)
            if args.command == "install" and root is not None and not promote and not already_tracked:
                exclude, value = local_exclude(repo, root)
                changes[exclude] = value
            if promote:
                exclude, value = local_exclude(repo, root, ignored=False)
                if exclude.exists():
                    changes[exclude] = value
                with tracking_index(root, repo, state, changes, repo / make_name if make_name else None, make, legacy_makefile):
                    apply_transaction(state, changes)
            else:
                apply_transaction(state, changes)
    return record


def main(argv: list[str] | None = None) -> int:
    """main() 先检测安装并处理升级；只有全新安装才询问 Git 跟踪，不替用户建立仓库。

    重新安装程序也保留已有 current；恢复出厂不属于这里的选择。
    """
    parser = argparse.ArgumentParser(description=tr('Install or update the Clarity sidecar.'))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "update"):
        child = commands.add_parser(command)
        child.add_argument("--repo", default=".", help=tr('Installation directory; default is the current directory, never the parent Git root.'))
        child.add_argument("--source", help=tr('Upstream Git URL or local path; initially GitHub HTTPS, then reuse the selected source; supply explicitly to switch.'))
        child.add_argument("--ref", help=tr('Upstream Git branch, tag, or commit; reuse the recorded selection, initially HEAD.'))
        if command == "install":
            child.add_argument("--track", choices=("yes", "no"), help=tr('Add a fresh installation to host Git tracking; prompt by default, Enter means no.'))
    args = parser.parse_args(argv)
    try:
        target = Path(args.repo).expanduser().resolve()
        if args.command == "update":
            if not existing_installation(target):
                raise SetupError(tr('No structurally valid Clarity installation exists here; use the first-install entry.'))
        elif existing_installation(target):
            choice = ask(tr('Clarity detected: [1] Update (default)  [2] Fresh installation (keep current): '),
                         {"1": "update", "update": "update", "更新": "update", "2": "install", "install": "install", "全新安装": "install",
                          tr("update").casefold(): "update", tr("fresh installation").casefold(): "install"}, "update")
            if choice == "update":
                args.command = "update"
                return deliver(args)
        if args.command == "install" and git_root(target) is not None and args.track is None:
            args.track = ask(tr('Add .clarity to Git tracking? [y/N]: '),
                             {"y": "yes", "yes": "yes", "是": "yes", "n": "no", "no": "no", "否": "no",
                              tr("yes").casefold(): "yes", tr("no").casefold(): "no"}, "no")
        return deliver(args)
    except (SetupError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(tr('Clarity installation/update did not complete: ') + str(error), file=sys.stderr)
        return 2


def deliver(args: argparse.Namespace) -> int:
    """deliver() 执行已经选定的安装或升级，成功仅表示软件交付，不宣称代码审核已启用或通过。"""
    try:
        record = install(args)
    except (SetupError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(tr('Clarity installation/update did not complete: ') + str(error), file=sys.stderr)
        return 2
    print(tr('Clarity installed at: ') + record["worktree_root"] + "/.clarity")
    print(tr('Upstream source: ') + record["source"])
    print(tr('Actual commit: ') + record["commit"])
    print(tr('Project current was preserved; initial is copied only when current is missing on install. Review activation is unchanged.'))
    print(tr('Start a code review with: make review'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
