"""review_context.py 只投影调用方已冻结的代码，不回读原项目或执行项目内容。"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from model_common import ModelFailure, _load_json


MAX_RESULTS = 100
MAX_LINES = 200
MAX_CHARS = 16_000
MAX_QUERY_CHARS = 1_000
MAX_PATH_CHARS = 4_096
EXCERPT_CHARS = 240
MAX_REPLY_FEEDBACK_GAPS = 20
# 同会话反馈总预算为 8000 字符；预留 300 给外层已验证发现的记账提示，避免补充提示使回合超限。
MAX_REPLY_FEEDBACK_CHARS = 7_700
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


def _missing_ranges(length, intervals):
    """_missing_ranges() 合并重复、重叠和相邻交付区间，再取正文字符范围内的补集。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    missing, cursor, delivered = [], 0, 0
    for start, end in merged:
        if cursor < start:
            missing.append({"start": cursor, "end": start})
        delivered += end - start
        cursor = end
    if cursor < length:
        missing.append({"start": cursor, "end": length})
    return delivered, missing


def _audit_object(pairs):
    """_audit_object() 拒绝同名字段的歧义，避免后写值覆盖先前的审计边界。"""
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Duplicate context audit field")
        record[key] = value
    return record


def _line_starts(text):
    """_line_starts() 保留既有分行边界，累计每行和文件尾在 Unicode 原文中的字符偏移。"""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _read_reply(path, version, record, start_line=1, line_count=200, start_column=1, char_limit=16000):
    """_read_reply() 共用既有有界原文与行号展示算法，供工具交付及接收端逐字段核对。"""
    _integer(start_line, "start_line", 1)
    _integer(line_count, "line_count", 1, MAX_LINES)
    _integer(start_column, "start_column", 1)
    _integer(char_limit, "char_limit", 1, MAX_CHARS)
    lines = record["text"].splitlines(keepends=True)
    if start_line > len(lines) + 1 or start_line == len(lines) + 1 and start_column != 1:
        raise ValueError("Context cursor is past the file")
    if start_line <= len(lines) and start_column > len(lines[start_line - 1]) + 1:
        raise ValueError("Context column is past the line")
    pieces, numbered_pieces, remaining = [], [], char_limit
    cursor_line, cursor_column = start_line, start_column
    end_line, end_column = start_line, start_column - 1
    stop_line = min(len(lines) + 1, start_line + line_count)
    while cursor_line < stop_line and remaining:
        line = lines[cursor_line - 1]
        piece = line[cursor_column - 1:cursor_column - 1 + remaining]
        pieces.append(piece)
        remaining -= len(piece)
        if piece:
            numbered_pieces.append(f"{cursor_line}: {piece}")
            end_line, end_column = cursor_line, cursor_column + len(piece) - 1
        cursor_column += len(piece)
        if cursor_column > len(line):
            cursor_line, cursor_column = cursor_line + 1, 1
    continuation = {"start_line": cursor_line, "start_column": cursor_column} if cursor_line <= len(lines) else None
    return {"version": version, "path": path, "oid": record["oid"], "mode": record["mode"],
            "text": "".join(pieces), "numbered_text": "".join(numbered_pieces),
            "start_line": start_line, "start_column": start_column,
            "end_line": end_line, "end_column": end_column, "next": continuation,
            "total_lines": len(lines), "truncated": continuation is not None}


def _same_json(left, right):
    """_same_json() 比较完整 JSON 值，保留布尔与整数的类型区别，不依赖对象键顺序。"""
    return (json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) ==
            json.dumps(right, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _text_content(blocks, *, acp=False):
    """_text_content() 只读取一个标准文本块，不从嵌套对象或混合载荷中猜选正文。"""
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
        raise ValueError("Invalid context transport content")
    block = blocks[0]
    if acp:
        if block.get("type") != "content" or not isinstance(block.get("content"), dict):
            raise ValueError("Invalid ACP content block")
        block = block["content"]
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise ValueError("Context transport did not contain original text")
    return block["text"]


def _completed_text(call):
    """_completed_text() 核对标准 ACP 展示正文及明确 MCP 结果副本，拒绝互相矛盾的交付证据。"""
    displayed = _text_content(call["content"], acp=True) if call.get("content") not in (None, []) else None
    raw_text = None
    raw = call.get("rawOutput")
    result = None
    if isinstance(raw, dict) and "result" in raw and set(raw) <= {"result", "error"}:
        if raw.get("error") is not None or raw["result"] is None:
            return None
        result = raw["result"]
    elif isinstance(raw, dict) and "content" in raw:
        result = raw
    if result is not None:
        if not isinstance(result, dict):
            raise ValueError("Invalid MCP tool result")
        if type(result.get("isError", False)) is not bool:
            raise ValueError("Invalid MCP tool error flag")
        if result.get("isError", False):
            return None
        if result.get("content") not in (None, []):
            raw_text = _text_content(result["content"])
        structured = result.get("structuredContent")
        text_value = raw_text if raw_text is not None else displayed
        if structured is not None and text_value is not None and not _same_json(_load_json(text_value), structured):
            raise ValueError("MCP result copies disagree")
    if displayed is not None and raw_text is not None and displayed != raw_text:
        raise ValueError("ACP displayed and raw tool text disagree")
    return displayed if displayed is not None else raw_text


def _native_target(raw_path, cwd, context):
    """_native_target() 仅按会话声明的冻结目录及原路径绑定原生读取，不访问或规范化宿主文件。"""
    if not isinstance(raw_path, str):
        return None
    if raw_path == "context.json":
        return None
    if (not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd or
            str(PurePosixPath(cwd)) != cwd or ".." in PurePosixPath(cwd).parts):
        raise ValueError("Native context reads require the recorded session directory")
    prefix = cwd.rstrip("/") + "/"
    if raw_path.startswith("/"):
        if not raw_path.startswith(prefix):
            return None
        raw_path = raw_path[len(prefix):]
    try:
        _relative_path(raw_path)
    except ValueError:
        return None
    version, separator, path = raw_path.partition("/")
    if not separator or version not in VERSIONS or path not in context.get(version, {}):
        return None
    return version, path


def _received_read(call, context, cwd):
    """_received_read() 只确认原生全文或自有有界结果与冻结正文、输入及身份逐项相等的区间。"""
    raw = call.get("rawInput")
    if not isinstance(raw, dict):
        return None
    if "server" in raw or "tool" in raw:
        if raw.get("server") != "clarity" or raw.get("tool") != "read_file" or not isinstance(raw.get("arguments"), dict):
            return None
        arguments = raw["arguments"]
    elif call.get("kind") == "read":
        target = _native_target(raw.get("path"), cwd, context)
        if target is None:
            return None
        version, path = target
        text = context[version][path]["text"]
        if _completed_text(call) != text:
            return None
        return (version, path, 1, 1, len(text), False, "native"), 0, len(text)
    else:
        arguments = raw
    allowed = {"path", "version", "start_line", "start_column", "line_count", "char_limit"}
    if set(arguments) - allowed:
        return None
    version, path = arguments.get("version", "current"), arguments.get("path")
    if not isinstance(version, str) or version not in VERSIONS or not isinstance(path, str) or path not in context.get(version, {}):
        return None
    try:
        expected = _read_reply(path, version, context[version][path],
                               **{key: value for key, value in arguments.items() if key not in {"path", "version"}})
    except (TypeError, ValueError):
        return None
    text = _completed_text(call)
    if text is None:
        return None
    try:
        body = _load_json(text)
    except (ValueError, TypeError, RecursionError):
        return None
    if not _same_json(body, expected):
        return None
    start = _line_starts(context[version][path]["text"])[expected["start_line"] - 1] + expected["start_column"] - 1
    return (version, path, expected["start_line"], expected["start_column"], len(expected["text"]), expected["truncated"], None), start, start + len(expected["text"])


def _transport_reads(context, events_path):
    """_transport_reads() 按会话与工具调用状态确认已完成正文，不将服务端生成记录当接收证明。

    同一调用的省略字段沿用已记录值，完整终态的相同通知可幂等；终态冲突、孤立更新或
    外来会话使日志无效。
    只检查明确 ACP 文本与 MCP 结果层，原生读取则绑定本次 cwd；不递归搜索 JSON。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(events_path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Unsafe context event log")
        events = []
        for line in source:
            if not line.endswith("\n"):
                raise ValueError("Unterminated context event record")
            event = _load_json(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Invalid context event record")
            events.append(event)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Context event log changed during assessment")
    sessions = [event for event in events if event["type"] == "session/new"]
    if len(sessions) != 1 or not isinstance(sessions[0].get("session"), dict):
        raise ValueError("Context event log needs one session")
    session_id = sessions[0]["session"].get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Context event log has no session identifier")
    calls = {}
    statuses = {"pending", "in_progress", "completed", "failed"}
    binding_fields = {"toolCallId", "kind", "status", "rawInput", "rawOutput", "content"}
    for event in events:
        if "session_id" in event and event["session_id"] != session_id:
            raise ValueError("Context event belongs to another session")
        if event["type"] != "session/update":
            continue
        update = event.get("update")
        if event.get("session_id") != session_id or not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
            raise ValueError("Invalid context session update")
        if update["sessionUpdate"] not in {"tool_call", "tool_call_update"}:
            continue
        identifier = update.get("toolCallId")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Context tool call has no identifier")
        values = {key: value for key, value in update.items() if key != "sessionUpdate"}
        if identifier not in calls:
            if update["sessionUpdate"] != "tool_call" or values.get("status") not in statuses:
                raise ValueError("Context tool update has no call")
            calls[identifier] = values
            continue
        previous = calls[identifier]
        if previous.get("status") in {"completed", "failed"} or update["sessionUpdate"] == "tool_call":
            if any(key in binding_fields and key in previous and not _same_json(previous[key], value)
                   for key, value in values.items()):
                raise ValueError("Conflicting context tool notification")
        merged = {**previous, **values}
        if merged.get("status") not in statuses:
            raise ValueError("Invalid context tool status")
        calls[identifier] = merged
    received = []
    for call in calls.values():
        if call.get("status") == "completed":
            read = _received_read(call, context, sessions[0].get("cwd"))
            if read is not None:
                received.append(read)
    return received


def assess_delivery(context, log_path, *, events_path=None):
    """assess_delivery() 合并原文区间，并在给定 ACP 事件时只确认实际收到的成功工具结果。

    行列按冻结原文的 splitlines 规则转换为 Unicode 字符偏移，重复和乱序读取只计
    并集；list_files、search 和模型自报路径不证明正文交付，已知空文件无正文缺口。
    missing_ranges 使用从 0 开始、不含 end 的字符区间；回执只含路径、计数和边界，
    不保存正文，也不证明模型已理解或正确审核这些正文。
    日志缺失、不安全、损坏或含无效记录均使 complete 为 False，保留有效区间供定位。
    events_path 为 None 时仅保留低层服务端生成范围，transport_confirmed 为 False；
    产品验收还须该标志为 True，不能把服务端已生成正文当成模型已收到正文。
    给定事件时，每个实际结果须与固定原文和一条服务端审计相符；失败、未完成及仅有
    服务端记录的调用不计交付，正文后的成功重读可独立补齐缺口。
    低层正文分行成本随字符数增长；每文件的 R 个区间排序合并为 O(R log R)，空间随
    行数、审计区间和回执缺口数增长。
    传输核对另载入协议载荷，空间随其大小增长；每次确认还重建所读文件的分页，成本
    随各次对应文件的正文长度累计，调用方应传入本次阶段专用日志。
    """
    receipt = {"complete": False, "audit_valid": False, "transport_confirmed": False,
               "current": {"total": 0, "complete": 0, "missing": []},
               "baseline": {"total": 0, "complete": 0, "missing": []}, "errors": []}
    files = {version: {} for version in VERSIONS}
    audit_reads = Counter()
    try:
        if not isinstance(context, dict) or set(context) - set(VERSIONS):
            raise ValueError("Invalid context maps")
        for version in VERSIONS:
            snapshot = context.get(version, {})
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid context file map")
            for path, record in snapshot.items():
                _relative_path(path)
                if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                    raise ValueError("Invalid frozen context text")
                starts = _line_starts(record["text"])
                files[version][path] = {"starts": starts, "length": len(record["text"]), "intervals": []}
    except ValueError:
        receipt["errors"].append({"line": None, "reason": "invalid_context"})
        return receipt

    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(log_path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Unsafe context audit")
            for number, line in enumerate(source, 1):
                try:
                    if not line.endswith("\n"):
                        raise ValueError("Unterminated context audit record")
                    record = json.loads(line, object_pairs_hook=_audit_object)
                    if not isinstance(record, dict) or record.get("tool") not in ("read_file", "list_files", "search"):
                        raise ValueError("Unknown context audit record")
                    version = record.get("version")
                    if not isinstance(version, str) or version not in files:
                        raise ValueError("Unknown audit version")
                    if record["tool"] in ("list_files", "search"):
                        fields = {"tool", "version", "prefix", "offset", "returned", "total"}
                        if record["tool"] == "search":
                            fields.add("query_chars")
                            _integer(record.get("query_chars"), "audit query_chars", 1, MAX_QUERY_CHARS)
                        if set(record) != fields:
                            raise ValueError("Invalid context discovery audit fields")
                        _relative_path(record["prefix"], prefix=True)
                        offset = _integer(record["offset"], "audit offset", 0)
                        total = _integer(record["total"], "audit total", 0)
                        _integer(record["returned"], "audit returned", 0, min(MAX_RESULTS, max(0, total - offset)))
                        continue
                    fields = {"tool", "version", "path", "start_line", "start_column", "returned_chars", "truncated"}
                    if set(record) - fields - {"source"}:
                        raise ValueError("Invalid context read audit fields")
                    path = _relative_path(record.get("path"))
                    if path not in files[version]:
                        raise ValueError("Unknown audit path")
                    material = files[version][path]
                    starts, length = material["starts"], material["length"]
                    start_line = _integer(record.get("start_line"), "audit start_line", 1, len(starts))
                    line_length = starts[start_line] - starts[start_line - 1] if start_line < len(starts) else 0
                    start_column = _integer(record.get("start_column"), "audit start_column", 1, line_length + 1)
                    start = starts[start_line - 1] + start_column - 1
                    count = _integer(record.get("returned_chars"), "audit returned_chars", 0, length - start)
                    end = start + count
                    if type(record.get("truncated")) is not bool or record["truncated"] != (end < length):
                        raise ValueError("Inconsistent audit continuation")
                    if "source" in record and (record["source"] != "native" or start != 0 or end != length):
                        raise ValueError("Invalid native full-text confirmation")
                    audit_reads[(version, path, start_line, start_column, count, record["truncated"], record.get("source"))] += 1
                    if count:
                        material["intervals"].append((start, end))
                except (ValueError, RecursionError):
                    # 损坏行不能被当成没有发生；仍检查后续完整行，保留已确认的交付范围。
                    receipt["errors"].append({"line": number, "reason": "invalid_audit_record"})
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                receipt["errors"].append({"line": None, "reason": "audit_changed_during_read"})
    except (OSError, TypeError, ValueError, UnicodeError):
        receipt["errors"].append({"line": None, "reason": "audit_unavailable_or_unsafe"})

    if events_path is not None:
        for snapshot in files.values():
            for material in snapshot.values():
                material["intervals"] = []
        try:
            for key, start, end in _transport_reads(context, events_path):
                if audit_reads[key] <= 0:
                    raise ValueError("Received context read has no server audit")
                audit_reads[key] -= 1
                version, path = key[:2]
                if start < end:
                    files[version][path]["intervals"].append((start, end))
            receipt["transport_confirmed"] = True
        except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
            receipt["errors"].append({"line": None, "reason": "transport_events_invalid_or_unavailable"})

    for version, snapshot in files.items():
        summary = receipt[version]
        summary["total"] = len(snapshot)
        for path, material in sorted(snapshot.items()):
            delivered, missing = _missing_ranges(material["length"], material["intervals"])
            if missing:
                summary["missing"].append({"path": path, "total_chars": material["length"],
                                           "delivered_chars": delivered, "missing_ranges": missing})
            else:
                summary["complete"] += 1
    receipt["audit_valid"] = not receipt["errors"]
    receipt["complete"] = receipt["audit_valid"] and all(receipt[version]["complete"] == receipt[version]["total"]
                                                          for version in VERSIONS)
    return receipt


def make_reply_feedback(context, log_path, response_schema, *, events_path=None) -> Callable[[str], str | None]:
    """make_reply_feedback() 为同一审核会话生成一次草稿格式或正文交付缺口的反馈回调。

    回调复用正式入口的严格整份 JSON 读取、对象限制和 schema 校验，只在结构与正文
    交付均满足时返回 None；这不验证发现、引用或审核结论，最终仍交原有外层校验器。
    审计不可用是技术故障，抛出 ModelFailure，不让模型通过改写日志补认交付。
    指定 events_path 时只使用已确认收到的工具正文，不能以服务端生成范围替代接收。
    缺口坐标沿用交付核对的原文分行与 Unicode 偏移，行列从 1 开始、终点不包含在内。
    反馈最多展示 20 个缺口且总长不超过 7700 字符，不附正文或 schema；过长路径整项
    略去而不截断标识，仍报告总缺口，避免反馈再次占满会话上下文。
    """
    try:
        import jsonschema
    except ImportError as error:
        raise ModelFailure("Response schema validation dependencies are unavailable") from error
    if not isinstance(response_schema, dict):
        raise ModelFailure("Reply feedback requires an object response schema")
    try:
        jsonschema.Draft202012Validator.check_schema(response_schema)
        validator = jsonschema.Draft202012Validator(response_schema)
    except jsonschema.SchemaError as error:
        raise ModelFailure("Reply feedback response schema is invalid") from error

    def feedback(raw_reply: str) -> str | None:
        """feedback() 重读本阶段审计后核对完整草稿，不提取代码块或把草稿发现当成已验证结果。"""
        delivery = assess_delivery(context, log_path, events_path=events_path)
        if not delivery["audit_valid"]:
            raise ModelFailure("Context audit is invalid; source-delivery feedback cannot be generated")
        valid_format = True
        try:
            result = _load_json(raw_reply)
            if not isinstance(result, dict):
                valid_format = False
            else:
                validator.validate(result)
        except (ValueError, TypeError, RecursionError, jsonschema.ValidationError):
            valid_format = False
        if valid_format and delivery["complete"]:
            return None

        parts = ["The first reply is not an accepted complete review."]
        if not valid_format:
            parts.append("The whole draft failed strict JSON/object/response-schema validation.")
        total_gaps = sum(len(item["missing_ranges"]) for version in VERSIONS for item in delivery[version]["missing"])
        missing_files = sum(len(delivery[version]["missing"]) for version in VERSIONS)
        if total_gaps:
            parts.append(f"Source delivery is incomplete: {total_gaps} missing ranges across {missing_files} file versions.")
        else:
            parts.append("Source delivery is complete; correct the reply format.")
        footer = ("Continue in this same Agent session. Obtain every missing current/baseline source range, "
                  "including gaps omitted below; use the original paths and one-based Unicode start_line/start_column "
                  "with read_file (Codex MCP) or clarity_read_file (Pi) and follow its next cursors. "
                  "Actual read caps: line_count <= 200 and char_limit <= 16000; Codex list_files limit <= 100. "
                  "Return one strict JSON object matching the supplied "
                  "response schema, without code fences or surrounding prose. Original review instructions and "
                  "final outer validation still apply.")
        rows = []
        header = f"Showing {MAX_REPLY_FEEDBACK_GAPS} of {total_gaps} missing ranges; end coordinates are exclusive:"
        used = len("\n".join(parts + [header, footer]))
        for version in VERSIONS:
            for item in delivery[version]["missing"]:
                if len(rows) == MAX_REPLY_FEEDBACK_GAPS:
                    break
                path = item["path"]
                starts = _line_starts(context[version][path]["text"])
                quoted_path = json.dumps(path, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
                for gap in item["missing_ranges"]:
                    if len(rows) == MAX_REPLY_FEEDBACK_GAPS:
                        break
                    start_index = bisect_right(starts, gap["start"]) - 1
                    end_index = bisect_right(starts, gap["end"]) - 1
                    row = (f"- version={version} path={quoted_path} "
                           f"start_line={start_index + 1} start_column={gap['start'] - starts[start_index] + 1} "
                           f"end_line={end_index + 1} end_column={gap['end'] - starts[end_index] + 1} "
                           f"missing_chars={gap['end'] - gap['start']}")
                    if used + len(row) + 1 <= MAX_REPLY_FEEDBACK_CHARS:
                        rows.append(row)
                        used += len(row) + 1
        if total_gaps:
            parts.append(f"Showing {len(rows)} of {total_gaps} missing ranges; end coordinates are exclusive:")
            parts.extend(rows)
        parts.append(footer)
        return "\n".join(parts)

    return feedback


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

    def record_native_read(self, path, version, observed_text):
        """record_native_read() 只为可信原生工具确认与固定正文逐字符相等的完整读取。

        截断、前后缀和换行改写都返回 False，不以长度或近似匹配补认交付；调用方可改用
        read_file 明确分页取全。
        成功时只记录全文件范围的 read_file 元数据；路径越界、投影变化或审计写入失败
        仍传播错误，不记录正文，此入口不作为模型可调用工具暴露。
        """
        text, _ = self._text(path, version)
        if not isinstance(observed_text, str) or observed_text != text:
            return False
        self._audit("read_file", version=version, path=path, start_line=1, start_column=1,
                    returned_chars=len(text), truncated=False, source="native")
        return True

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
        numbered_text 复用同一游标给实际片段加原始行号，帮助引用定位；前缀只属于展示，
        原文证据仍是 text，长行续页的起始列仍由 start_column 表示。
        每次核验完整投影并按现有审核器的 splitlines 规则分行，成本随本文件长度增长。
        """
        _integer(start_line, "start_line", 1)
        _integer(line_count, "line_count", 1, MAX_LINES)
        _integer(start_column, "start_column", 1)
        _integer(char_limit, "char_limit", 1, MAX_CHARS)
        text, record = self._text(path, version)
        result = _read_reply(path, version, {**record, "text": text}, start_line, line_count, start_column, char_limit)
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
