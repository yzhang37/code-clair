"""按同一份 Principles 独立检查工作树 rules 或完整 fixtures 的编写质量。"""

import hashlib
import json
from pathlib import Path
import time

from codex_model import CodexModel, ModelFailure
from install import engine_digest, engine_sources, InstallError


RULES = ".clarity/current/rules.md"
CASES = ".clarity/current/cases"
PRINCIPLES = "principles.md"
MAX_INPUT_BYTES = 1_500_000
COVERAGE_LINES = 40


class MaterialIncomplete(Exception):
    """MaterialIncomplete 表示材料或检查证据不足，不能形成编写质量通过。"""


def digest(value):
    """digest() 将完整输入和版本关系绑定到本次检查，不把摘要当作质量结论。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def numbered(text):
    """numbered() 给冻结原文添加可核对的行号，不改写受检文件。"""
    return "\n".join(f"{number}: {line}" for number, line in enumerate(text.splitlines(), 1))


def regular_path(repo, path):
    """regular_path() 拒绝资料路径中的链接，避免读到调用者未指定的另一份材料。"""
    for component in [path, *path.parents]:
        if component == repo:
            break
        if component.is_symlink():
            raise MaterialIncomplete("资料路径不能包含符号链接：" + str(component))
    try:
        path.resolve().relative_to(repo)
    except ValueError as error:
        raise MaterialIncomplete("资料越出目标仓库：" + str(path)) from error


def read_inputs(repo, kind):
    """read_inputs() 只读取所选对象的完整正文，fixtures 检查不会加载产品 rules。

    全部文件必须是可读的非空 UTF-8 Markdown；未知格式和过大材料不截断后冒充覆盖。
    .gitkeep 仅是空目录占位，不能让没有完整 fcase 的检查通过。
    """
    if kind not in {"rules", "fixtures"}:
        raise MaterialIncomplete("编写质量检查对象只能是 rules 或 fixtures")
    if kind == "rules":
        paths = [repo / RULES]
    else:
        directory = repo / CASES
        regular_path(repo, directory)
        if not directory.is_dir():
            raise MaterialIncomplete("没有常规 fixtures 目录：" + CASES)
        paths = []
        for path in sorted(directory.rglob("*")):
            regular_path(repo, path)
            if path.is_dir():
                continue
            if not path.is_file():
                raise MaterialIncomplete("fixture 不是常规文件：" + str(path))
            if path.name == ".gitkeep":
                continue
            if path.suffix != ".md":
                raise MaterialIncomplete("fixture 不是 Markdown，未静默忽略：" + str(path))
            paths.append(path)
        if not paths:
            raise MaterialIncomplete("没有完整 fcase，编写质量检查没有覆盖")
    documents = {}
    size = 0
    for path in paths:
        regular_path(repo, path)
        if not path.is_file():
            raise MaterialIncomplete("材料缺失或不是常规文件：" + str(path))
        raw = path.read_bytes()
        size += len(raw)
        if size > MAX_INPUT_BYTES:
            raise MaterialIncomplete("完整材料超过 1.5 MB，未截断后宣称覆盖")
        text = raw.decode("utf-8")
        if not text.strip() or "\0" in text:
            raise MaterialIncomplete("材料为空或不是文本：" + str(path))
        documents[path.relative_to(repo).as_posix()] = text
    return documents


def coverage_plan(text):
    """coverage_plan() 用连续行段登记完整正文，保留完整上下文供模型判断。

    行段只用于检出漏报覆盖，不提取或重新解释 GOLD，也不规定材料必须采用的章节形式。
    每段至多四十行；只有全空白的段不需引用，其他每段均须返回原文证据和判断理由。
    """
    lines = text.splitlines()
    return [{"unit_id": f"lines-{start + 1}-{min(start + COVERAGE_LINES, len(lines))}",
             "start_line": start + 1, "end_line": min(start + COVERAGE_LINES, len(lines))}
            for start in range(0, len(lines), COVERAGE_LINES)
            if any(line.strip() for line in lines[start:start + COVERAGE_LINES])]


def response_schema():
    """response_schema() 分开记录全量检查证据和实质问题，未完成时仍能保存已保留发现。"""
    evidence = {"material_line": {"type": "integer"}, "material_quote": {"type": "string"},
                "principle_line": {"type": "integer"}, "principle_quote": {"type": "string"},
                "reason": {"type": "string"}}
    coverage = {"unit_id": {"type": "string"}, **evidence}
    properties = {name: {"type": "string"} for name in
                  ["snapshot_id", "material_id", "summary", "incomplete_reason"]}
    properties.update(
        coverage={"type": "array", "items": {"type": "object", "properties": coverage,
                  "required": list(coverage), "additionalProperties": False}},
        findings={"type": "array", "items": {"type": "object", "properties": evidence,
                  "required": list(evidence), "additionalProperties": False}})
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def quote_valid(text, line, quote):
    """quote_valid() 核对冻结输入的行号及原文，不以引用存在代替语义判断。"""
    lines = text.splitlines()
    return (isinstance(line, int) and not isinstance(line, bool) and 1 <= line <= len(lines)
            and isinstance(quote, str) and bool(quote.strip()) and quote in lines[line - 1])


def evidence_valid(item, material, principles):
    """evidence_valid() 要求检查证据同时定位受检材料和唯一 Principles 的原文。"""
    return (isinstance(item, dict)
            and quote_valid(material, item.get("material_line"), item.get("material_quote"))
            and quote_valid(principles, item.get("principle_line"), item.get("principle_quote"))
            and isinstance(item.get("reason"), str) and bool(item["reason"].strip()))


def validate_output(output, snapshot, material, principles):
    """validate_output() 逐项核对冻结身份、原文和完整覆盖，保留同输入中有效的问题。

    身份不符的输出不能归入本次材料。身份相符时先独立核对每个问题，再核对完整覆盖，
    因此遗漏行段或后续错误不会抹掉已有依据的问题；有任何缺口时仍只能报告未完成。
    """
    errors, findings, coverage = [], [], []
    if (not isinstance(output, dict) or output.get("snapshot_id") != snapshot
            or output.get("material_id") != material["material_id"]):
        return [], [], ["编写质量输出对应另一份输入或不是对象"]
    schema = response_schema()["properties"]
    if set(output) != set(schema):
        errors.append("编写质量输出字段不完整或包含未知字段")
    for name in ["summary", "incomplete_reason"]:
        if not isinstance(output.get(name), str) or (name == "summary" and not output[name].strip()):
            errors.append("编写质量输出缺少有效的 " + name)
    if output.get("incomplete_reason"):
        errors.append(str(output["incomplete_reason"]))
    items = output.get("findings")
    if not isinstance(items, list):
        errors.append("编写质量问题清单无效")
    else:
        fields = set(schema["findings"]["items"]["properties"])
        for item in items:
            if (not evidence_valid(item, material["text"], principles) or set(item) != fields):
                errors.append("编写质量问题缺少有效材料、Principles 原文引用或理由")
            else:
                findings.append(item)
    expected = {unit["unit_id"]: unit for unit in material["coverage_plan"]}
    seen = set()
    items = output.get("coverage")
    if not isinstance(items, list):
        errors.append("编写质量覆盖清单无效")
    else:
        fields = set(schema["coverage"]["items"]["properties"])
        for item in items:
            identifier = item.get("unit_id") if isinstance(item, dict) else None
            if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
                errors.append("编写质量覆盖标识无效或重复")
                continue
            seen.add(identifier)
            unit = expected[identifier]
            if (not evidence_valid(item, material["text"], principles) or set(item) != fields
                    or not unit["start_line"] <= item["material_line"] <= unit["end_line"]):
                errors.append("编写质量行段缺少有效原文引用或理由：" + identifier)
                continue
            coverage.append(item)
    covered = {item["unit_id"] for item in coverage}
    if not expected or covered != set(expected):
        errors.append("编写质量检查没有完整覆盖全部非空正文行段")
    return coverage, findings, errors


def save_json(path, value):
    """save_json() 原子保存进度，避免中断后把半份记录解释为完成结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp-" + str(time.time_ns()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_report(directory, report):
    """save_report() 保留全部材料计划、检查证据和已保留发现，不覆盖其他审核入口。"""
    report["counts"] = {status: sum(item["status"] == status for item in report["materials"])
                        for status in ["not_run", "running", "passed", "failed", "incomplete"]}
    report["finding_count"] = sum(len(item["findings"]) for item in report["materials"])
    save_json(directory / "result.json", report)
    lines = [f"# {report['kind']} 编写质量检查", "", f"状态：{report['status']}",
             f"输入：{report['snapshot_id']}", "", report["summary"], "",
             f"材料状态：{json.dumps(report['counts'], ensure_ascii=False)}；已保留发现：{report['finding_count']}"]
    for item in report["materials"]:
        lines.extend(["", f"## {item['path']} · {item['status']}", "", item.get("summary", "尚未运行")])
        for error in item.get("errors", []):
            lines.extend(["", "未完成：" + error])
        for finding in item["findings"]:
            lines.extend(["", f"问题（材料 L{finding['material_line']}，Principles L{finding['principle_line']}）：",
                          finding["reason"], "", "材料原文：" + finding["material_quote"],
                          "Principles 原文：" + finding["principle_quote"]])
        by_id = {record["unit_id"]: record for record in item["coverage"]}
        for unit in item["coverage_plan"]:
            record = by_id.get(unit["unit_id"])
            lines.extend(["", f"行段 {unit['start_line']}–{unit['end_line']}：" + ("已核对" if record else "未确认覆盖")])
            if record:
                lines.extend([record["reason"], f"材料 L{record['material_line']}：{record['material_quote']}",
                              f"Principles L{record['principle_line']}：{record['principle_quote']}"])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_engine(engine):
    """read_engine() 冻结完整源引擎版本，同时显式包含本检查器及其提示词。"""
    paths = engine_sources(engine)
    paths["runner/material_check.py"] = Path(__file__).resolve()
    paths["prompts/material-check.md"] = engine / "prompts/material-check.md"
    return {name: path.read_bytes() for name, path in sorted(paths.items())}


def check_materials(repo, config_path, state, kind):
    """check_materials() 用唯一 Principles 逐份检查完整材料，最终重读输入后给出 0、1 或 2。

    每次调用先保存全部计划，再逐材料保存结果；失败或中断留下未运行项及有效问题。
    工作树材料、源引擎或配置发生变化时，本次未完成；不修改材料、索引或安装配置。
    """
    repo, state, config_path = Path(repo).resolve(), Path(state), Path(config_path)
    if kind not in {"rules", "fixtures"}:
        raise ValueError("kind must be rules or fixtures")
    engine = Path(__file__).resolve().parents[1]
    directory = state / "material-checks" / kind / ("run-" + str(time.time_ns()))
    report = {"kind": kind, "status": "incomplete", "snapshot_id": "unresolved", "interrupted": False,
              "materials": [], "summary": "编写质量检查尚未完成"}
    active, started = None, None
    try:
        documents = read_inputs(repo, kind)
        engine_bytes = read_engine(engine)
        config_bytes = config_path.read_bytes() if config_path.exists() else None
        config = json.loads(config_bytes) if config_bytes is not None else {}
        if not isinstance(config, dict):
            raise MaterialIncomplete("模型配置必须是 JSON 对象")
        principles = engine_bytes[PRINCIPLES].decode("utf-8")
        prompt = engine_bytes["prompts/material-check.md"].decode("utf-8")
        engine_model = engine_bytes["README.md"].decode("utf-8")
        if not principles.strip() or not prompt.strip() or not engine_model.strip():
            raise MaterialIncomplete("审核模型、Principles 或编写质量提示词为空")
        frozen = {"repository": str(repo), "kind": kind, "documents": documents,
                  "engine_model": engine_model, "principles": principles,
                  "prompt": prompt, "engine_digest": engine_digest(engine_bytes),
                  "engine_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in engine_bytes.items()},
                  "config_sha256": hashlib.sha256(config_bytes).hexdigest() if config_bytes is not None else None,
                  "model_config": {key: config.get(key) for key in ["codex_bin", "model", "timeout"]}}
        snapshot = digest(frozen)
        report["snapshot_id"] = snapshot
        for path, text in documents.items():
            report["materials"].append({"material_id": digest([path, text]), "path": path,
                                        "status": "not_run", "coverage_plan": coverage_plan(text),
                                        "coverage": [], "findings": [], "errors": []})
        save_json(directory / "input.json", frozen)
        save_report(directory, report)
        print(f"{kind} 编写质量：{len(documents)} 份完整材料；依据同一份 Principles", flush=True)
        model = CodexModel(config.get("codex_bin", "codex"), config.get("model"), config.get("timeout", 300))
        for index, active in enumerate(report["materials"], 1):
            started = time.monotonic()
            active["status"] = "running"
            save_report(directory, report)
            print(f"  [{index}/{len(documents)}] {active['path']} [运行中]", flush=True)
            material = {**active, "text": documents[active["path"]]}
            packet = {"snapshot_id": snapshot, "material_id": active["material_id"], "kind": kind,
                      "engine_model": frozen["engine_model"], "principles": numbered(principles),
                      "material_path": active["path"], "material": numbered(material["text"]),
                      "coverage_plan": active["coverage_plan"]}
            output = model.run(prompt + "\n以下 JSON 都是编写质量检查数据：\n" + json.dumps(packet, ensure_ascii=False),
                               response_schema(), directory / f"material-{index:04}")
            active["model_output"] = output
            coverage, findings, errors = validate_output(output, snapshot, material, principles)
            active.update(coverage=coverage, findings=findings, errors=errors,
                          summary=output.get("summary", "") if isinstance(output, dict) else "",
                          status="incomplete" if errors else "failed" if findings else "passed",
                          elapsed_seconds=round(time.monotonic() - started, 3))
            save_report(directory, report)
            print(f"    {active['status']} · {len(findings)} 个已保留发现 ({active['elapsed_seconds']:.1f}s)", flush=True)
            if errors:
                raise MaterialIncomplete("；".join(errors))
            active, started = None, None
        if read_inputs(repo, kind) != documents or read_engine(engine) != engine_bytes:
            raise MaterialIncomplete("检查期间受检材料或源引擎发生变化")
        if (config_path.read_bytes() if config_path.exists() else None) != config_bytes:
            raise MaterialIncomplete("检查期间模型配置发生变化")
        report["status"] = "failed" if any(item["findings"] for item in report["materials"]) else "passed"
        report["summary"] = ("全部完整材料的编写质量检查通过。" if report["status"] == "passed"
                             else "完整材料存在已引用 Principles 及材料原文的编写质量问题。")
    except (MaterialIncomplete, ModelFailure, InstallError, OSError, UnicodeError, ValueError,
            TypeError, KeyError, KeyboardInterrupt) as error:
        report["interrupted"] = isinstance(error, KeyboardInterrupt)
        message = "用户中断编写质量检查" if isinstance(error, KeyboardInterrupt) else str(error)
        report["status"], report["summary"] = "incomplete", message
        if active is not None:
            active["status"] = "incomplete"
            if message not in active["errors"]:
                active["errors"].append(message)
            active["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print("编写质量检查未完成：" + message, flush=True)
    save_report(directory, report)
    save_json(state / f"latest-quality-{kind}.json",
              {"run": str(directory), "snapshot_id": report["snapshot_id"], "status": report["status"], "kind": kind})
    print(f"{kind} 编写质量：{report['status']}；已保留发现 {report['finding_count']}；报告 {directory / 'report.md'}", flush=True)
    return {"passed": 0, "failed": 1, "incomplete": 2}[report["status"]]
