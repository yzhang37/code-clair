#!/usr/bin/env python3
"""setup.py 从原厂 Git 版本安装或更新宿主 sidecar，保留项目自己的 current。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
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
        raise SetupError(result.stderr.decode(errors="replace").strip() or "Git 操作失败。")
    return result.stdout


def repository_paths(requested: Path) -> tuple[Path, Path, Path]:
    """repository_paths() 向宿主 Git 询问工作树、共享状态及 hook 位置，兼容 .git 文件布局。"""
    repo = Path(git(requested.expanduser().resolve(), "rev-parse", "--show-toplevel").decode().strip())
    values = [Path(git(repo, "rev-parse", *arguments).decode().strip())
              for arguments in (("--git-common-dir",), ("--git-path", "hooks"))]
    common, hooks = [(value if value.is_absolute() else repo / value).resolve() for value in values]
    return repo.resolve(), common / "clarity", hooks


def read_file(path: Path) -> tuple[bytes, int] | None:
    """read_file() 只取得常规文件的原文字节和权限，拒绝链接及目录占用文件接点。"""
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise SetupError("不是可管理的常规文件：" + str(path))
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def safe_parents(path: Path) -> None:
    """safe_parents() 拒绝沿已有符号链接写入，防止普通 sidecar 路径实际指向外部资料。"""
    for parent in path.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise SetupError("安装路径经过链接或非目录：" + str(parent))


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
            and (name in {"setup.py", "README.md"} or name.startswith(("engine/", "initial/"))))


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
            raise SetupError("发行文件不是普通文件：" + name)
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
            raise SetupError("旧原厂缓存不是 bare Git 仓库：" + str(legacy))
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
            raise SetupError("原厂缓存不是 bare Git 仓库：" + str(cache))
        if git(cache, "config", "--get", "remote.origin.url").decode().strip() != source:
            raise SetupError("原厂来源与已有缓存不同，未切换安装来源。")
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
        raise SetupError("无法加载安装依赖：" + str(path))
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
                 sidecar / "Makefile"):
        if not os.path.lexists(base):
            continue
        safe_parents(base / "placeholder" if base.is_dir() else base)
        paths = base.rglob("*") if base.is_dir() else [base]
        for path in paths:
            if "__pycache__" in path.parts or path.name == ".DS_Store":
                continue
            if path.is_symlink():
                raise SetupError("旧安装包含链接，未接管：" + str(path))
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
        if all(previous.get(name) == value for name, value in existing.items() if name != "Makefile"):
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
            return existing, legacy_makefile
    raise SetupError("已有 .clarity 程序与原厂最近 200 个版本均不一致；未覆盖本地文件。")


def installed_files(sidecar: Path, manifest: dict) -> dict:
    """installed_files() 核对上次登记的每份原厂文件，修改、删除或权限变化都须先由所有者处理。"""
    files = {}
    for name, expected in manifest["files"].items():
        if not factory_path(name) and name != "Makefile":
            raise SetupError("安装记录包含越界文件：" + name)
        path = sidecar / name
        safe_parents(path)
        value = read_file(path)
        if value is None or fingerprint(value) != expected:
            raise SetupError("原厂文件有本地改动，未覆盖：" + str(path))
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
                raise SetupError("安装期间文件发生变化：" + str(path))
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
                raise SetupError("安装回读不一致：" + str(path))
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
            raise SetupError("恢复未完成；保留原文备份：" + str(backup)) from rollback_error
        shutil.rmtree(backup)
        raise error
    shutil.rmtree(backup)


@contextmanager
def installation_lock(state: Path):
    """installation_lock() 与代码审核、组合维护共用排他锁，避免替换运行中的共享引擎。"""
    state.mkdir(parents=True, exist_ok=True)
    if list(state.glob("install-backup-*")):
        raise SetupError("有上次安装遗留备份，须先核对恢复状态：" + str(state))
    lock = state / "running.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SetupError("已有审核、维护或安装在运行，或旧锁待核对：" + str(lock)) from error
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
            raise SetupError("现有运行引擎没有配置，未覆盖：" + str(runtime))
        return changes
    config = json.loads(config_file[0])
    safe_parents(runtime / "runner/install.py")
    old = load_module("clarity_old_install", runtime / "runner/install.py")
    old_files = {name: read_file(path) for name, path in old.engine_sources(runtime).items()}
    actual = old.engine_digest({name: value[0] for name, value in old_files.items()})
    if config.get("engine_digest") != actual:
        raise SetupError("现有运行引擎与配置摘要不一致，未自动覆盖。")
    new = load_module("clarity_new_install", staged / "engine/runner/install.py")
    new_files = {name: read_file(path) for name, path in new.engine_sources(staged / "engine").items()}
    for name in old_files.keys() | new_files.keys():
        if name not in old_files and read_file(runtime / name) is not None:
            raise SetupError("新运行引擎路径已被本地文件占用：" + str(runtime / name))
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
                raise SetupError("已有提交 hook 有本地改动或缺少来源，未覆盖：" + str(path))
            changes[path] = (read_file(staged / "engine/runner/commit-msg")[0], value[1])
        else:
            if value[0] != old.hook_script(name, runtime, config_path):
                raise SetupError("已有 pre-commit 有本地改动，未覆盖：" + str(path))
            changes[path] = (new.hook_script(name, runtime, config_path), value[1])
    return changes


def install(args: argparse.Namespace) -> dict:
    """install() 从固定 Git 版本准备原厂文件，再一次交付程序、Make 入口、运行快照及版本记录。

    current 已存在时从不参与替换；只有首次 install 缺少该目录时才从同版本 initial 创建。
    同一共享 Git 控制目录目前只绑定一个已登记工作树，拒绝另一工作树悄悄替换共享运行版本。
    """
    repo, state, hooks = repository_paths(Path(args.repo))
    sidecar = repo / ".clarity"
    safe_parents(sidecar / "setup.py")
    safe_parents(state / "installation.json")
    with installation_lock(state):
        manifest_path = state / "installation.json"
        previous_record = read_file(manifest_path)
        manifest = json.loads(previous_record[0]) if previous_record else None
        if manifest and (manifest.get("version") != 1 or manifest.get("worktree_root") != str(repo)):
            raise SetupError("安装记录版本不支持或已绑定另一工作树，未替换共享安装。")
        legacy = legacy_files(sidecar) if manifest is None else None
        if args.command == "update" and manifest is None and not legacy:
            raise SetupError("尚未安装 sidecar；请先执行 install。")
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
            raise SetupError("所选版本缺少独立安装所需文件：" + ", ".join(sorted(required - files.keys())))
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
            changes = {}
            for name in owned.keys() | files.keys():
                path = sidecar / name
                safe_parents(path)
                if name not in owned and read_file(path) is not None:
                    raise SetupError("新原厂路径已被本地文件占用：" + str(path))
                changes[path] = files.get(name)
            names = {entry.name for entry in repo.iterdir()}
            make_name = next((name for name in ("GNUmakefile", "makefile", "Makefile") if name in names), "Makefile")
            if manifest and make_name != manifest["host_makefile"]:
                raise SetupError("宿主生效的 Makefile 已改变，须先核对原入口。")
            host_make = repo / make_name
            original_make = read_file(host_make)
            changes[host_make] = (make.host_makefile(original_make[0] if original_make else None, legacy_makefile),
                                  original_make[1] if original_make else 0o644)
            current = sidecar / "current"
            if os.path.lexists(current):
                if current.is_symlink() or not current.is_dir():
                    raise SetupError("current 不是普通目录，未初始化：" + str(current))
            elif args.command == "install":
                for name, value in files.items():
                    if name.startswith("initial/"):
                        changes[current / name.removeprefix("initial/")] = value
            # 旧迁移仍以当前宿主为 hook 来源，只有逐文件核对成功后才允许刷新已启用快照。
            hook_manifest = manifest or {"worktree_root": str(repo)}
            changes.update(runtime_changes(state, hooks, staged, hook_manifest))
            hook_records = {}
            for name in ("commit-msg", "pre-commit"):
                path = hooks / name
                value = changes.get(path, read_file(path))
                if value is not None and new_install.is_owned_hook(path):
                    hook_records[name] = fingerprint(value)
            record = {"version": 1, "worktree_root": str(repo), "source": source, "ref": ref,
                      "commit": commit, "previous_commit": manifest["commit"] if manifest else None,
                      "previous_source": manifest["source"] if manifest else None,
                      "installed_at": datetime.now(timezone.utc).isoformat(), "host_makefile": make_name,
                      "files": {name: fingerprint(value) for name, value in sorted(files.items())},
                      "hooks": hook_records}
            changes[manifest_path] = (encoded(record), 0o600)
            apply_transaction(state, changes)
    return record


def main(argv: list[str] | None = None) -> int:
    """main() 区分 install 和 update；成功只表示软件交付已回读，不表示规则或业务代码审核通过。"""
    parser = argparse.ArgumentParser(description="安装或更新 Clarity sidecar。")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "update"):
        child = commands.add_parser(command)
        child.add_argument("--repo", default=".", help="目标宿主 Git 仓库，默认当前目录。")
        child.add_argument("--source", help="原厂 Git 地址或本地路径；首次默认 GitHub HTTPS，后续沿用已选来源；显式提供可换源。")
        child.add_argument("--ref", help="原厂 Git 分支、tag 或 commit；默认沿用已登记选择，首次为 HEAD。")
    args = parser.parse_args(argv)
    try:
        record = install(args)
    except (SetupError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print("Clarity 安装/更新未完成：" + str(error), file=sys.stderr)
        return 2
    print("Clarity 已交付：" + record["worktree_root"] + "/.clarity")
    print("原厂来源：" + record["source"])
    print("实际 commit：" + record["commit"])
    print("项目 current 已保留；首次缺失时才从 initial 创建。提交审核启用状态沿用原安装。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
