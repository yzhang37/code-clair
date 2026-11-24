"""规则验收的两层终端输出和完整报告，不参与模型判定。"""

# 动态安装会按文件路径加载本模块；只补同版 runner 搜索路径，不依赖宿主技术栈。
import sys
from pathlib import Path
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import tr


import json
import time


STATUS = {"matched": tr('matched'), "mismatch": tr('mismatch'), "incomplete": tr('incomplete'),
          "not_run": tr('not run'), "running": tr('running')}
VERDICT = {"accept": tr('accept'), "reject": tr('reject'), None: tr('no valid verdict')}
TITLES = {"passed": tr('Rule verification passed'), "failed": tr('Rule verification failed'), "incomplete": tr('Rule verification incomplete')}


def emit(text):
    """emit() 将控制字符和无效 Unicode 显示为转义，外部文本不能覆盖终端状态。"""
    text = "".join(repr(char)[1:-1] if ((ord(char) < 32 and char not in "\n\t")
                                     or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF)
                   else char for char in text)
    print(text, flush=True)


def prepare_report(report, samples):
    """prepare_report() 先登记全部计划，使中断后的未运行片段也能在报告中定位。"""
    report["samples"] = [{**sample, "status": "not_run", "actual": None, "reason": "",
                          "error": "", "elapsed_seconds": None} for sample in samples]
    cases = {}
    for sample in samples:
        path = sample["case_path"]
        if path not in cases:
            cases[path] = {key: sample[key] for key in ["case_path", "case_number", "case_title"]}
    report["reason_checks"] = [{**case, "status": "not_run", "reason": "", "error": "",
                                "elapsed_seconds": None} for case in cases.values()]


def counts(report):
    """counts() 分别统计固定预期的匹配状态，反例被拒绝也计为匹配。"""
    result = {status: 0 for status in STATUS}
    for sample in report["samples"]:
        result[sample["status"]] += 1
    result.update(case_count=len(report.get("reason_checks", [])), subcase_count=len(report["samples"]))
    return result


def count_text(values):
    """count_text() 保留零值，避免把未完成或未运行吞进通过数。"""
    return " / ".join(f"{STATUS[key]} {values.get(key, 0)}" for key in
                      ["matched", "mismatch", "incomplete", "not_run", "running"])


def sample_result(sample):
    """sample_result() 把预期与实际同时展示，区分拒绝代码和验收不匹配。"""
    elapsed = sample["elapsed_seconds"]
    duration = f" ({elapsed:.1f}s)" if elapsed is not None else ""
    return (tr('[{v0}] expected {v1} / actual {v2}{v3}', v0=(STATUS[sample['status']]), v1=(VERDICT[sample['expected']]), v2=(VERDICT[sample['actual']]), v3=(duration)))


def locator(sample):
    """locator() 在进度、异常和报告中使用同一组可读编号。"""
    return f"case {sample['case_number']} / subcase {sample['subcase_number']}"


def literal(text, markdown=True):
    """literal() Markdown 用围栏保留原文，终端用缩进显示证据。"""
    if not markdown:
        return ["    " + line for line in text.splitlines()] + [""]
    fence = "````"
    while fence in text:
        fence += "`"
    return [fence + "text", text, fence, ""]


def exception_details(report, markdown=True):
    """exception_details() 集中展示错判和未完成证据，不推测规则为何出错。"""
    lines, heading = [], "### " if markdown else "--- "
    for sample in report["samples"]:
        if sample["status"] not in {"mismatch", "incomplete"}:
            continue
        lines += [f"{heading}{locator(sample)}：{sample['subcase_title']}", "",
                  sample_result(sample), "", tr('Location: {v0}:{v1}', v0=(sample['case_path']), v1=(sample['case_line'])), "",
                  tr('Original sample: '), ""] + literal(sample["content"], markdown)
        lines += [tr('Check objective and factual context (actually sent to the model): '), ""]
        lines += literal("\n".join(sample["context"]), markdown) if sample["context"] else [tr('(none)'), ""]
        output = sample.get("model_output")
        evidence = output if isinstance(output, dict) else sample
        if evidence.get("rule_quote"):
            lines += [tr('Model-cited rule (frozen snapshot): .clarity/current/rules.md:{v0}', v0=(evidence.get('rule_line', '?'))), ""]
            lines += literal(str(evidence["rule_quote"]), markdown)
        if sample["status"] == "incomplete":
            lines += [tr('Incomplete reason: ') + sample["error"], ""]
            if isinstance(output, dict):
                lines += [tr('Raw output has no valid verdict; its quotations are not fully verified.'), ""]
        reason = evidence.get("reason")
        if reason:
            lines += [tr('Original model rationale: '), ""] + literal(str(reason), markdown)
        if isinstance(output, dict) and output.get("incomplete_reason"):
            lines += [tr('Model incomplete reason: '), ""] + literal(str(output["incomplete_reason"]), markdown)
    by_id = {sample["sample_id"]: sample for sample in report["samples"]}
    for check in report.get("reason_checks", []):
        if check["status"] not in {"mismatch", "incomplete"}:
            continue
        lines += [tr('{v0}case {v1}: rationale check {v2}', v0=(heading), v1=(check['case_number']), v2=(STATUS[check['status']])), "",
                  tr('Location: {v0}', v0=(check['case_path'])), "", check["error"] or check["reason"], ""]
        output = check.get("model_output")
        if not isinstance(output, dict):
            continue
        if check["status"] == "incomplete":
            lines += [tr('Raw output has no valid rationale check; its quotations are not fully verified.'), ""]
            if output.get("summary"):
                lines += [tr('Model verification summary: '), ""] + literal(str(output["summary"]), markdown)
        checks = output.get("checks", [])
        for item in checks if isinstance(checks, list) else []:
            if not isinstance(item, dict) or item.get("aligned") is not False:
                continue
            identifier = item.get("sample_id")
            sample = by_id.get(identifier) if isinstance(identifier, str) else None
            name = locator(sample) if sample else str(item.get("sample_id", tr('unknown sample')))
            lines += [f"- {name}：{item.get('reason', '')}", "",
                      tr('Case quotation: {v0}:{v1}', v0=(check['case_path']), v1=(item.get('case_line', '?'))), ""]
            lines += literal(str(item.get("case_quote", "")), markdown)
    return lines


def summary_lines(report, directory):
    """summary_lines() 分开报告标签判定和理由核对，任一阶段未完成都不能称为通过。"""
    totals = report["summary_counts"]
    reason_counts = {key: 0 for key in STATUS}
    for check in report.get("reason_checks", []):
        reason_counts[check["status"]] += 1
    scope = (tr('Scope: {v0} cases / {v1} subcases', v0=(totals['case_count']), v1=(totals['subcase_count']))
             if report["samples"] else tr('Scope: sample collection is incomplete; a complete plan has not been established'))
    lines = [TITLES[report["status"]], report["summary"], "", scope,
             tr('Sample classification: ') + count_text(totals), tr('Rationale verification (per case): ') + count_text(reason_counts)]
    if report.get("reason_check_note"):
        lines.append(report["reason_check_note"])
    if report.get("display_preview"):
        lines += [tr('Historical raw records: ') + report["display_preview"]["source_run"]]
    abnormal = [sample for sample in report["samples"] if sample["status"] in {"mismatch", "incomplete"}]
    if abnormal:
        lines += ["", tr('Exception index: ')]
        lines += [f"- {locator(sample)} [{STATUS[sample['status']]}] {sample['case_path']}:{sample['case_line']}"
                  for sample in abnormal]
    lines += ["", tr('Snapshot: ') + report["snapshot_id"], tr('Full report: ') + str(directory / "report.md"),
              tr('Structured records: ') + str(directory / "result.json"), ""]
    return lines


def save_report(directory, report):
    """save_report() 原子替换进度快照，保留全部计划、原始输出和两层结果。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report["summary_counts"] = counts(report)
    lines = ["# " + TITLES[report["status"]], "", report["summary"], "", "## case / subcase", ""]
    for case in report.get("reason_checks", []):
        lines += [f"case {case['case_number']}：{case['case_title']}", ""]
        for sample in report["samples"]:
            if sample["case_path"] == case["case_path"]:
                lines += [f"  - subcase {sample['subcase_number']}：{sample['subcase_title']} — {sample_result(sample)}"]
        lines += ["", tr('Rationale check: [{v0}] {v1}', v0=(STATUS[case['status']]), v1=(case['reason'] or case['error'])), ""]
    details = exception_details(report)
    if details:
        lines += [tr('## Exception details'), ""] + details
    lines += [tr('## Summary'), ""] + summary_lines(report, directory)
    for name, text in [("result.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                       ("report.md", "\n".join(lines))]:
        destination = directory / name
        temporary = destination.with_suffix(".tmp-" + str(time.time_ns()))
        temporary.write_text(text, encoding="utf-8", errors="backslashreplace")
        temporary.replace(destination)


def finish_report(directory, report):
    """finish_report() 终端集中输出异常详情与摘要，完整两层树保存在报告中。"""
    save_report(directory, report)
    details = exception_details(report, markdown=False)
    if details:
        emit(tr('\nException details\n\n') + "\n".join(details))
    emit("\n" + "\n".join(summary_lines(report, directory)))
