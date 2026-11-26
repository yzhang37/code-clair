"""directory_snapshot.py 直接冻结项目目录的文本，不要求 Git 仓库或已跟踪文件。"""

from __future__ import annotations

import codecs
import hashlib
import os
from pathlib import Path
import stat

from make_integration import INCLUDE_BLOCK, LEGACY_INCLUDE_BLOCK


MAX_BYTES = 1_500_000


def _text_file(path, limit):
    """_text_file() 读取有界 UTF-8 正文，不沿文件链接取材，也不阻塞在特殊文件上。

    二进制和其他编码交回调用方记录排除；超限文本及读取期间变化使快照未完成。
    读取上限多一字节，用于区分恰好达到容量和正文仍有余量，避免截断后当成完整输入。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Review input changed to a non-regular file: " + str(path))
        raw = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Review input changed while it was being read: " + str(path))
    if b"\0" in raw:
        return None
    try:
        if len(raw) > limit:
            # 上限可能落在 UTF-8 字符中间；未读完的末字符不应把超限文本误判成二进制。
            codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
            raise ValueError("Directory text exceeds the review input capacity: " + str(path))
        return raw.decode("utf-8"), raw, before.st_mode
    except UnicodeDecodeError:
        return None


def _ignored(path, directory, rules):
    """_ignored() 按各级 .gitignore 的路径作用域与后写优先级判断，不另造模式语法。"""
    ignored = False
    for base, specification in rules:
        relative = path.relative_to(base).as_posix() + ("/" if directory else "")
        matched = specification.check_file(relative).include
        if matched is not None:
            ignored = matched
    return ignored


def read_snapshot(root, *, max_bytes=MAX_BYTES):
    """read_snapshot() 冻结目录内未排除的普通 UTF-8 文件，并明确交付所有排除路径。

    .clarity、.git 和原样生成的 Make 入口属于工具资料，不作为用户代码；项目的
    .gitignore 由现成 pathspec 解释。符号链接、特殊文件和二进制只记录排除原因，
    调用方须如实说明范围，不能把空 files 或排除项宣称为完整代码审核通过。
    每个目录只遍历一次；排序按该目录项目数计算，读取和散列按纳入正文总字节计算。
    忽略匹配对每条路径依次检查各级规则，其成本由路径、模式数量及 pathspec 决定。
    """
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("Directory review capacity must be a positive byte count")
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Review target must be a directory: " + str(root))
    files, excluded = {}, {}
    total = 0

    def visit(folder, inherited):
        """visit() 保持当前目录的忽略作用域；排除目录直接剪枝，不读取其子项目。"""
        nonlocal total
        rules = inherited
        ignore_file = folder / ".gitignore"
        if not ignore_file.is_symlink() and ignore_file.is_file():
            from pathspec import GitIgnoreSpec

            material = _text_file(ignore_file, max_bytes)
            if material is None:
                raise ValueError("Project .gitignore is not UTF-8 text: " + str(ignore_file))
            # Git 将文件开头的 UTF-8 BOM 当编码标记；只在规则解析时移除，防止第一条排除失效。
            # 冻结到 files 的 .gitignore 原文仍保留 BOM，不改写用户文件或读取被排除目录的正文。
            patterns = material[0].removeprefix("\ufeff")
            rules = [*inherited, (folder, GitIgnoreSpec.from_lines(patterns.splitlines()))]
        with os.scandir(folder) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
        for entry in entries:
            path = folder / entry.name
            name = path.relative_to(root).as_posix()
            if entry.name in {".clarity", ".git"}:
                excluded[name] = "clarity_metadata" if entry.name == ".clarity" else "git_metadata"
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                excluded[name] = "symbolic_link"
                continue
            directory = stat.S_ISDIR(metadata.st_mode)
            if _ignored(path, directory, rules):
                excluded[name + ("/" if directory else "")] = "gitignore"
                continue
            if directory:
                visit(path, rules)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                excluded[name] = "unsupported_file_type"
                continue
            material = _text_file(path, max_bytes)
            if material is None:
                excluded[name] = "binary_or_non_utf8"
                continue
            text, raw, mode = material
            if entry.name in {"Makefile", "makefile", "GNUmakefile"} and raw in {INCLUDE_BLOCK, LEGACY_INCLUDE_BLOCK}:
                excluded[name] = "generated_make_entry"
                continue
            total += len(raw)
            if total > max_bytes:
                raise ValueError("Directory text exceeds the review input capacity; nothing was truncated")
            files[name] = {"text": text, "oid": "sha256:" + hashlib.sha256(raw).hexdigest(),
                           "mode": "100755" if mode & 0o111 else "100644"}

    visit(root, [])
    return {"files": dict(sorted(files.items())), "excluded": dict(sorted(excluded.items()))}
