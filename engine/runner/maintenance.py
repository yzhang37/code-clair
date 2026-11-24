"""按工作树材料编排质量检查与固定案例回归，保存各阶段的真实结果。"""

import hashlib
import json
from pathlib import Path
import subprocess
import time

from install import InstallError, engine_digest, engine_sources
from material_check import check_materials
from rules_check import CASES, RULES, CheckIncomplete, check_rules, read_inputs


ENGINE = Path(__file__).resolve().parents[1]
STAGES = ("rules-quality", "fixtures-quality", "rules-regression")
EXIT_STATUS = {0: "passed", 1: "failed", 2: "incomplete"}


class MaintenanceIncomplete(ValueError):
    """MaintenanceIncomplete 表示维护输入、结果或回应目标尚不能可靠核对。"""


def save(path, value):
    """save() 原子保存维护记录，避免中断留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def target_identity(repo):
    """target_identity() 询问 Git 的实际工作树和共同目录，阻止跨工作树套用回应。"""
    values = []
    for argument in ("--show-toplevel", "--git-common-dir"):
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", argument], capture_output=True, text=True)
        if result.returncode:
            raise MaintenanceIncomplete("Git 无法确认维护目标：" + result.stderr.strip())
        path = Path(result.stdout.strip())
        values.append(str((path if path.is_absolute() else Path(repo) / path).resolve()))
    return {"worktree_root": values[0], "git_common_dir": values[1]}


def freeze_inputs(repo, config_path):
    """freeze_inputs() 绑定目标、全部正文及路径、源引擎和配置原字节，用于组合前后的共同核对。

    子检查仍各自判断并保存证据；共同摘要只验证组合期间的输入关系，不替代其语义检查。
    """
    selected = read_inputs(repo)
    materials = {name: (repo / name).read_bytes() for name in selected}
    documents = {name: raw.decode("utf-8") for name, raw in materials.items()}
    engine = {name: path.read_bytes() for name, path in engine_sources(ENGINE).items()}
    config_bytes = config_path.read_bytes() if config_path.exists() else None
    config = json.loads(config_bytes) if config_bytes is not None else {}
    if not isinstance(config, dict):
        raise MaintenanceIncomplete("模型配置必须是 JSON 对象")
    return {"target_repository": target_identity(repo), "documents": documents,
            "material_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in materials.items()},
            "engine_digest": engine_digest(engine),
            "engine_files": {name: hashlib.sha256(content).hexdigest() for name, content in engine.items()},
            "config_path": str(config_path.resolve()),
            "model_config": {key: config.get(key) for key in ["codex_bin", "model", "timeout"]},
            "config_sha256": hashlib.sha256(config_bytes).hexdigest() if config_bytes is not None else None}


def validate_child_input(frozen, stage, child, snapshot):
    """validate_child_input() 将实际子输入投影到共同材料，拒绝中间换版后恢复原文的组合。

    质量检查按各自对象取完整原字节正文；回归只比较其使用的协议和提示词，不追加 Principles。
    子结果标识还须绑定这份实际输入，不能用一份输入文件冒充另一份已完成检查。
    """
    actual = hashlib.sha256(json.dumps(child, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if actual != snapshot:
        raise MaintenanceIncomplete("子检查实际输入摘要与其结果标识不一致")
    common = {"repository": frozen["target_repository"]["worktree_root"],
              "config_sha256": frozen["config_sha256"], "model_config": frozen["model_config"]}
    if stage in {"rules-quality", "fixtures-quality"}:
        kind = "rules" if stage == "rules-quality" else "fixtures"
        if kind == "rules":
            documents = {RULES: frozen["documents"][RULES]}
        else:
            documents = {path: text for path, text in frozen["documents"].items() if path.startswith(CASES + "/")}
        expected = {**common, "kind": kind, "documents": documents,
                    "engine_digest": frozen["engine_digest"], "engine_sha256": frozen["engine_files"]}
    else:
        # read_text 会统一换行，原字节摘要另行核对，避免把编码表示差异误判为正文漂移。
        documents = {path: text.replace("\r\n", "\n").replace("\r", "\n")
                     for path, text in frozen["documents"].items()}
        engine = frozen["engine_files"]
        expected = {**common, "documents": documents, "material_sha256": frozen["material_sha256"],
                    "protocol_sha256": engine["runner/rules_check.py"],
                    "adapter_sha256": engine["runner/codex_model.py"],
                    "reporter_sha256": engine["runner/rules_report.py"],
                    "prompt_sha256": engine["prompts/rules-check.md"]}
    changed = [key for key, value in expected.items() if child.get(key) != value]
    if changed:
        raise MaintenanceIncomplete("子检查实际输入与本次共同输入不一致：" + "、".join(changed))


def render(directory, report):
    """render() 汇总材料维护和精确子记录，不把组合结果称为代码审核结论。"""
    labels = {"passed": "通过", "failed": "未通过", "incomplete": "未完成", "not_run": "未运行", "running": "运行中"}
    lines = ["# Engine 材料维护：" + labels[report["status"]], "", report["summary"], "",
             "目标工作树：" + report["target_repository"]["worktree_root"],
             "维护输入：`" + report["snapshot_id"] + "`", ""]
    for stage in report["stages"]:
        lines.append("- " + stage["stage"] + "：" + labels[stage["status"]])
        if stage.get("run"):
            lines.append("  完整子记录：" + stage["run"])
        if stage.get("error"):
            lines.append("  未完成原因：" + stage["error"])
    lines += ["", "本次只组织工作树材料检查，不形成 HEAD 或暂存区的代码审核通过，也不改写规则、fixtures 或固定预期。",
              "维护记录：" + str(directory), ""]
    (directory / "report.md").write_text("\n".join(lines))
    print("\n".join(lines), flush=True)


def maintain(repo, config_path, state):
    """maintain() 依次收齐两项质量检查与固定案例回归，再核对共同输入是否保持一致。

    各阶段使用本次专属的子状态目录，避免共同 Git 目录的其他工作树覆盖最新指针。
    独立阶段失败仍继续收集其余结果；输入或调用未完成优先于普通失败，不能聚合为通过。
    """
    repo, config_path, state = Path(repo).resolve(), Path(config_path), Path(state)
    directory = state / "maintenance" / ("run-" + str(time.time_ns()))
    directory.mkdir(parents=True, exist_ok=False)
    report = {"status": "incomplete", "snapshot_id": "unresolved", "target_repository": target_identity(repo),
              "stages": [{"stage": name, "status": "not_run"} for name in STAGES], "current_stage": "",
              "summary": "材料维护尚未完成。", "input_errors": [], "interrupted": False}
    frozen = None
    try:
        frozen = freeze_inputs(repo, config_path)
        report["snapshot_id"] = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    except (MaintenanceIncomplete, CheckIncomplete, InstallError, OSError, UnicodeError, ValueError, TypeError) as error:
        report["input_errors"].append("共同输入未能冻结：" + str(error))
    save(directory / "input.json", {"snapshot_id": report["snapshot_id"], "target_repository": report["target_repository"],
                                    "frozen": frozen, "input_errors": list(report["input_errors"])})
    save(directory / "result.json", report)
    checks = directory / "checks"
    calls = [(lambda: check_materials(repo, config_path, checks, "rules"), "latest-quality-rules.json"),
             (lambda: check_materials(repo, config_path, checks, "fixtures"), "latest-quality-fixtures.json"),
             (lambda: check_rules(repo, config_path, checks), "latest-rules-check.json")]
    for stage, (call, pointer_name) in zip(report["stages"], calls):
        stage["status"] = "running"
        save(directory / "result.json", report)
        print("\nEngine 维护阶段：" + stage["stage"], flush=True)
        try:
            code = call()
            if type(code) is not int or code not in EXIT_STATUS:
                raise MaintenanceIncomplete("子检查没有返回约定的退出码")
            pointer = json.loads((checks / pointer_name).read_text())
            child_run = Path(pointer["run"]).resolve()
            if not child_run.is_relative_to(checks.resolve()):
                raise MaintenanceIncomplete("子记录不属于本次维护")
            result = json.loads((child_run / "result.json").read_text())
            if (pointer["status"] != EXIT_STATUS[code] or result["status"] != pointer["status"]
                    or result["snapshot_id"] != pointer["snapshot_id"]):
                raise MaintenanceIncomplete("子检查退出码、指针与结果不一致")
            stage.update(status=pointer["status"], child_status=pointer["status"], run=str(child_run),
                         snapshot_id=pointer["snapshot_id"], exit_code=code)
            if result.get("interrupted") is True:
                report["interrupted"] = True
                stage["interrupted"] = True
                stage["error"] = "子检查收到用户中断，后续阶段未运行"
                save(directory / "result.json", report)
                break
            if frozen is not None:
                child_input = json.loads((child_run / "input.json").read_text())
                validate_child_input(frozen, stage["stage"], child_input, pointer["snapshot_id"])
        except KeyboardInterrupt:
            report["interrupted"] = True
            stage.update(status="incomplete", error="用户中断材料维护")
            save(directory / "result.json", report)
            break
        except (MaintenanceIncomplete, CheckIncomplete, InstallError, OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            stage.update(status="incomplete", error=str(error))
        save(directory / "result.json", report)
    try:
        if frozen is None or freeze_inputs(repo, config_path) != frozen:
            raise MaintenanceIncomplete("组合期间目标、正文路径集合、材料、源引擎或配置发生变化，不能复用为同次材料维护通过")
    except (MaintenanceIncomplete, CheckIncomplete, InstallError, OSError, UnicodeError, ValueError, TypeError) as error:
        report["input_errors"].append(str(error))
    statuses = {stage["status"] for stage in report["stages"]}
    report["status"] = ("incomplete" if report["input_errors"] or statuses - {"passed", "failed"}
                        else "failed" if "failed" in statuses else "passed")
    report["current_stage"] = next((stage["stage"] for stage in report["stages"] if stage["status"] != "passed"), "")
    report["summary"] = {"passed": "两项编写质量检查与固定案例回归均通过，共同输入回读一致。",
                         "failed": "材料检查存在未通过项；各阶段结果完整保留，正式资料未修改。",
                         "incomplete": "材料维护未完成；已完成阶段与未运行项分别保留，不能据此宣称整体通过。"}[report["status"]]
    if report["input_errors"]:
        report["summary"] += " " + "；".join(report["input_errors"])
    save(directory / "result.json", report)
    render(directory, report)
    save(state / "latest-maintenance.json", {"run": str(directory), "snapshot_id": report["snapshot_id"],
         "status": report["status"], "target_repository": report["target_repository"]})
    return {"passed": 0, "failed": 1, "incomplete": 2}[report["status"]]


def respond_maintenance(repo, state, text, stage=None):
    """respond_maintenance() 原样绑定最新维护的指定阶段和问题，不改检查结果或正式依据。

    默认选择报告中首个未通过阶段；共同输入核对失败但没有阶段问题时，要求先重新维护。
    记录保留目标、维护及子检查标识和原始结果摘要，避免把回应套给其他工作树或新检查。
    """
    if not isinstance(text, str) or not text.strip():
        raise MaintenanceIncomplete("维护回应不能为空")
    pointer = json.loads((Path(state) / "latest-maintenance.json").read_text())
    identity = target_identity(Path(repo).resolve())
    if pointer["target_repository"] != identity:
        raise MaintenanceIncomplete("最新维护属于另一个工作树，未保存跨目标回应")
    directory = Path(pointer["run"]).resolve()
    if not directory.is_relative_to((Path(state) / "maintenance").resolve()):
        raise MaintenanceIncomplete("最新维护指针不属于本仓库维护记录")
    report = json.loads((directory / "result.json").read_text())
    if (report["target_repository"] != identity or report["snapshot_id"] != pointer["snapshot_id"]
            or report["status"] != pointer["status"]):
        raise MaintenanceIncomplete("最新维护指针和目标结果不一致")
    selected = stage or report["current_stage"]
    current = next((item for item in report["stages"] if item["stage"] == selected), None)
    if current is None or current["status"] not in {"failed", "incomplete"}:
        raise MaintenanceIncomplete("所选维护阶段没有可对应的失败或未完成问题；共同输入变化须重新运行维护")
    path = directory / "responses.json"
    responses = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(responses, list):
        raise MaintenanceIncomplete("维护回应记录不是列表")
    child_reference = None
    if current.get("run"):
        child_path = Path(current["run"]) / "result.json"
        child_bytes = child_path.read_bytes()
        child_result = json.loads(child_bytes)
        if (child_result["snapshot_id"] != current["snapshot_id"]
                or child_result["status"] != current.get("child_status", current["status"])):
            raise MaintenanceIncomplete("维护阶段标识与其原始结果不一致，未保存回应")
        child_reference = {"path": str(child_path), "snapshot_id": child_result["snapshot_id"],
                           "status": child_result["status"], "sha256": hashlib.sha256(child_bytes).hexdigest()}
    responses.append({"target_repository": identity, "run": str(directory), "snapshot_id": report["snapshot_id"],
                      "stage": selected, "stage_record": current, "stage_result_ref": child_reference,
                      "human_text": text, "time": time.time()})
    save(path, responses)
    print("已原样保存维护回应：" + selected + "；未更改检查结果、规则或固定预期。\n维护记录：" + str(directory))
    return 0
