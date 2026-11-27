"""acp_runtime.py 在用户缓存准备固定版本的 ACP Python 依赖，不修改宿主技术栈。"""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import venv


REQUIREMENTS = ("agent-client-protocol==1.0.0", "jsonschema==4.26.0", "pathspec==1.1.1", "mcp==1.30.0")


def ready():
    """ready() 检查当前解释器的客户端版本，避免升级依赖后悄悄更换协议实现。"""
    try:
        return all(importlib.metadata.version(item.split("==")[0]) == item.split("==")[1]
                   for item in REQUIREMENTS)
    except importlib.metadata.PackageNotFoundError:
        return False


def ensure_runtime():
    """ensure_runtime() 仅在审核首次需要时准备私有环境，并以同一入口重新执行。

    使用现成 SDK 需要第三方包；独立缓存避免把这些包装进用户的项目或全局 Python。
    创建锁只保护这个可重建环境，不承担安装资料或审核状态的恢复职责。
    """
    if ready():
        return
    requested = os.environ.get("CLARITY_PYTHON")
    if requested:
        python = Path(requested).expanduser().absolute()
        if python == Path(sys.executable).absolute():
            raise RuntimeError("The selected Clarity Python lacks the required ACP dependencies.")
        os.execv(str(python), [str(python), *sys.argv])
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "clarity"
    cache.mkdir(parents=True, exist_ok=True)
    # 只读事实工具复用固定 MCP SDK；独立缓存防止旧环境缺服务端依赖而混用两个运行版本。
    root = cache / f"acp-1.0.0-jsonschema-4.26.0-pathspec-1.1.1-mcp-1.30.0-py{sys.version_info.major}.{sys.version_info.minor}"
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.absolute() == Path(sys.executable).absolute():
        raise RuntimeError("Cached ACP dependencies are incomplete. Remove " + str(root) + " and retry setup.")
    lock = root.with_suffix(root.suffix + ".lock")
    deadline = time.monotonic() + 180
    while not python.is_file():
        try:
            lock.mkdir()
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("ACP dependency setup is busy; retry after the other setup finishes.")
            time.sleep(0.1)
            continue
        try:
            if python.is_file():
                break
            print("Preparing Clarity's ACP Python client dependencies...", flush=True)
            with tempfile.TemporaryDirectory(prefix=".acp-", dir=cache) as temporary:
                staged = Path(temporary) / "environment"
                venv.EnvBuilder(with_pip=True).create(staged)
                executable = staged / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                result = subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check",
                                         *REQUIREMENTS], capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError("Could not prepare the ACP Python client (pip exit " +
                                       str(result.returncode) + "). Check network access and Python venv/pip support.")
                staged.rename(root)
        finally:
            lock.rmdir()
    os.execv(str(python), [str(python), *sys.argv])


if __name__ == "__main__":
    ensure_runtime()
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
