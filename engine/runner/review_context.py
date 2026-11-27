"""review_context.py 只投影调用方已冻结的代码，不回读原项目或执行项目内容。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat


MAX_RESULTS = 100
MAX_LINES = 200
MAX_CHARS = 16_000
MAX_QUERY_CHARS = 1_000
MAX_PATH_CHARS = 4_096
EXCERPT_CHARS = 240
VERSIONS = {"current": "current_files", "baseline": "baseline_changed_files"}


def _relative_path(path, *, prefix=False):
    """_relative_path() 拒绝路径别名，使查询只能命中清单中原样记录的相对路径。"""
    if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or "\0" in path or "\\" in path or PureWindowsPath(path).drive:
        raise ValueError("Context paths must be relative POSIX paths")
    if prefix and path == "":
        return path
    candidate = path[:-1] if prefix and path.endswith("/") else path
    if not candidate or any(part in {"", ".", ".."} for part in candidate.split("/")):
        raise ValueError("Context paths must be canonical relative paths")
    return path


def _integer(value, name, minimum, maximum=None):
    """_integer() 明确拒绝越界参数，不让静默截断改变调用方的续读位置。"""
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise ValueError("Invalid context " + name)
    return value


def materialize_context(context, root):
    """materialize_context() 将新旧冻结正文原样写入空目录并返回路径清单，不修改输入。

    原路径、对象标识和原模式保存在清单；投影文件统一为 0444，目录为 0555，
    避免原可执行位让模型启动项目程序。
    所有路径与记录先校验，正文散列用于查询时识别被替换的投影；不读取原项目。
    工作量随正文总字节及路径数增长，排序按每版路径数计算；调用方在模型结束后
    恢复目录权限并清理，权限本身不等同于操作系统级沙箱。
    """
    if not isinstance(context, dict) or set(context) - set(VERSIONS):
        raise ValueError("Context must contain only current and baseline snapshots")
    prepared = {}
    manifest = {key: {} for key in VERSIONS.values()}
    for version, manifest_key in VERSIONS.items():
        files = context.get(version, {})
        if not isinstance(files, dict):
            raise ValueError("Context snapshot must be a file map")
        prepared[version] = {}
        for path, record in files.items():
            _relative_path(path)
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                raise ValueError("Context file must contain frozen text")
            if record.get("mode") not in ("100644", "100755"):
                raise ValueError("Context accepts only ordinary text files")
            if not isinstance(record.get("oid"), str) or not record["oid"]:
                raise ValueError("Context file must retain its object identifier")
            raw = record["text"].encode("utf-8")
            prepared[version][path] = raw
            manifest[manifest_key][path] = {
                "view_path": version + "/" + path, "oid": record["oid"], "mode": record["mode"],
                "lines": len(record["text"].splitlines()), "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
        for path in files:
            parts = path.split("/")
            if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
                raise ValueError("Context file paths conflict with a directory")
    root = Path(root).absolute()
    if root.is_symlink() or root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Context projection requires an empty ordinary directory")
    root.mkdir(exist_ok=True)
    directories = {root}
    for version, files in prepared.items():
        folder = root / version
        folder.mkdir()
        directories.add(folder)
        for path, raw in sorted(files.items()):
            target = folder / path
            target.parent.mkdir(parents=True, exist_ok=True)
            directories.update(parent for parent in target.parents if parent == root or root in parent.parents)
            target.write_bytes(raw)
            target.chmod(0o444)
    manifest_path = root / "context.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o444)
    for folder in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        folder.chmod(0o555)
    return manifest


class ContextStore:
    """ContextStore 只按固定清单读取投影，查询不发现宿主文件，也不提供执行或改写入口。"""

    def __init__(self, root, log_path=None):
        """__init__() 校验清单与输出审计位置，把模型可见范围固定为已声明的新旧文件。"""
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Context root must be an ordinary directory")
        self.log_path = None
        if log_path is not None:
            log = Path(log_path).absolute()
            if log.is_symlink() or log.resolve() == self.root.resolve() or self.root.resolve() in log.resolve().parents:
                raise ValueError("Context audit log must be outside the projection")
            self.log_path = log
        try:
            manifest = json.loads(self._read_bytes("context.json").decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid context manifest") from error
        if not isinstance(manifest, dict) or set(manifest) != set(VERSIONS.values()):
            raise ValueError("Invalid context manifest maps")
        self.files = {}
        for version, key in VERSIONS.items():
            files = manifest[key]
            if not isinstance(files, dict):
                raise ValueError("Invalid context manifest file map")
            for path, record in files.items():
                _relative_path(path)
                if not isinstance(record, dict) or record.get("view_path") != version + "/" + path:
                    raise ValueError("Invalid context manifest view path")
                if record.get("mode") not in ("100644", "100755") or not isinstance(record.get("oid"), str) or not record["oid"]:
                    raise ValueError("Invalid context manifest file identity")
                _integer(record.get("lines"), "manifest lines", 0)
                _integer(record.get("bytes"), "manifest bytes", 0)
                checksum = record.get("sha256")
                if not isinstance(checksum, str) or len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
                    raise ValueError("Invalid context manifest checksum")
            self.files[version] = files

    def _read_bytes(self, relative, limit=None):
        """_read_bytes() 逐级用不跟随链接的目录描述符打开投影，拒绝链接及特殊文件。

        各级目录均相对于已打开的父目录定位，避免检查之后换成符号链接时越过边界。
        文件须只有一个硬链接，防止把宿主文件作为清单路径的替身；查询不等待 FIFO。
        """
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            descriptor = os.open(self.root, flags)
            parts = relative.split("/")
            for part in parts[:-1]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(child, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("Context file must be an unlinked ordinary projection")
                return source.read() if limit is None else source.read(limit + 1)
        except OSError as error:
            raise ValueError("Context file is missing or unsafe") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _version(self, version):
        """_version() 只接受显式的新旧版本名称，不把未知版本回退成当前正文。"""
        if not isinstance(version, str) or version not in self.files:
            raise ValueError("Unknown context version")
        return self.files[version]

    def _text(self, path, version):
        """_text() 先查原路径清单再读投影，并核对完整字节以拒绝变动或截断的证据。"""
        _relative_path(path)
        files = self._version(version)
        if path not in files:
            raise ValueError("Unknown context path")
        record = files[path]
        raw = self._read_bytes(record["view_path"], record["bytes"])
        if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("Frozen context file changed")
        text = raw.decode("utf-8")
        if len(text.splitlines()) != record["lines"]:
            raise ValueError("Frozen context line count changed")
        return text, record

    def _audit(self, tool, **values):
        """_audit() 只在调用方指定的视图外记录查询边界，不记录搜索正文或文件载荷。"""
        if self.log_path is not None:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(self.log_path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                metadata = os.fstat(output.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("Context audit log must be an ordinary file")
                output.write(json.dumps({"tool": tool, **values}, ensure_ascii=False) + "\n")

    def list_files(self, version="current", prefix="", offset=0, limit=100):
        """list_files() 按原路径排序分页，不扫描投影目录或列出未声明的文件。"""
        files = self._version(version)
        _relative_path(prefix, prefix=True)
        _integer(offset, "offset", 0)
        _integer(limit, "limit", 1, MAX_RESULTS)
        paths = sorted(path for path in files if path.startswith(prefix))
        page = [{"path": path, **{key: files[path][key] for key in ("view_path", "oid", "mode", "lines", "bytes")}}
                for path in paths[offset:offset + limit]]
        next_offset = offset + len(page) if offset + len(page) < len(paths) else None
        self._audit("list_files", version=version, prefix=prefix, offset=offset, returned=len(page), total=len(paths))
        return {"version": version, "prefix": prefix, "files": page, "offset": offset,
                "next_offset": next_offset, "total": len(paths)}

    def read_file(self, path, version="current", start_line=1, line_count=200, start_column=1, char_limit=16000):
        """read_file() 返回保留换行的有界原文，并给出下一字符游标，长行也可完整续读。

        行列均从 1 开始，列按 Unicode 字符计；行数和字符数任一用尽就停，next 指向
        尚未交付的正文，而不是把长行后半截丢弃。
        每次核验完整投影并按现有审核器的 splitlines 规则分行，成本随本文件长度增长。
        """
        _integer(start_line, "start_line", 1)
        _integer(line_count, "line_count", 1, MAX_LINES)
        _integer(start_column, "start_column", 1)
        _integer(char_limit, "char_limit", 1, MAX_CHARS)
        text, record = self._text(path, version)
        lines = text.splitlines(keepends=True)
        if start_line > len(lines) + 1 or start_line == len(lines) + 1 and start_column != 1:
            raise ValueError("Context cursor is past the file")
        if start_line <= len(lines) and start_column > len(lines[start_line - 1]) + 1:
            raise ValueError("Context column is past the line")
        pieces, remaining = [], char_limit
        cursor_line, cursor_column = start_line, start_column
        end_line, end_column = start_line, start_column - 1
        stop_line = min(len(lines) + 1, start_line + line_count)
        while cursor_line < stop_line and remaining:
            line = lines[cursor_line - 1]
            piece = line[cursor_column - 1:cursor_column - 1 + remaining]
            pieces.append(piece)
            remaining -= len(piece)
            if piece:
                end_line, end_column = cursor_line, cursor_column + len(piece) - 1
            cursor_column += len(piece)
            if cursor_column > len(line):
                cursor_line, cursor_column = cursor_line + 1, 1
        continuation = {"start_line": cursor_line, "start_column": cursor_column} if cursor_line <= len(lines) else None
        result = {"version": version, "path": path, "oid": record["oid"], "mode": record["mode"],
                  "text": "".join(pieces), "start_line": start_line, "start_column": start_column,
                  "end_line": end_line, "end_column": end_column, "next": continuation,
                  "total_lines": len(lines), "truncated": continuation is not None}
        self._audit("read_file", version=version, path=path, start_line=start_line, start_column=start_column,
                    returned_chars=len(result["text"]), truncated=result["truncated"])
        return result

    def search(self, query, version="current", prefix="", offset=0, limit=100):
        """search() 按路径、行号分页做区分大小写的字面搜索，每个命中行只交付首个位置。

        摘录围绕首个命中截取，防止超长行绕过输出上限；完整证据须用 read_file 续读。
        扫描所选文件的全部行以报告精确 total，只保留当前页，因此结果内存受 limit 约束；
        字符查找成本由 Python 字符串实现决定，不接受正则表达式或可执行查询。
        """
        files = self._version(version)
        _relative_path(prefix, prefix=True)
        _integer(offset, "offset", 0)
        _integer(limit, "limit", 1, MAX_RESULTS)
        if not isinstance(query, str) or not query or len(query) > MAX_QUERY_CHARS or query.splitlines() != [query]:
            raise ValueError("Context search requires one nonempty bounded literal line")
        matches, total = [], 0
        for path in sorted(path for path in files if path.startswith(prefix)):
            text, _ = self._text(path, version)
            for number, line in enumerate(text.splitlines(), 1):
                column = line.find(query)
                if column < 0:
                    continue
                if offset <= total < offset + limit:
                    # 首个命中前保留少量邻文；即使命中很靠后，也不以行首摘录隐藏命中位置。
                    start = max(0, column - 60)
                    excerpt = line[start:start + EXCERPT_CHARS]
                    matches.append({"path": path, "line": number, "column": column + 1,
                                    "text": excerpt, "match_length": len(query), "excerpt_start_column": start + 1,
                                    "excerpt_truncated": start > 0 or start + len(excerpt) < len(line)})
                total += 1
        next_offset = offset + len(matches) if offset + len(matches) < total else None
        self._audit("search", version=version, prefix=prefix, query_chars=len(query), offset=offset,
                    returned=len(matches), total=total)
        return {"version": version, "query": query, "prefix": prefix, "matches": matches,
                "offset": offset, "next_offset": next_offset, "total": total}
