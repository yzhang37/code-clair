"""test_acp_fixture.py 将已有确定性语义夹具接到真实 ACP 传输。

旧夹具的判断、引文和故障预期保持原样；这层只转换测试进程输入输出。
"""


FAKE_ACP_PRELUDE = r'''
import json as _acp_json
from os import environ as _acp_environment
from pathlib import Path as _AcpPath
import subprocess as _acp_subprocess
import sys as _acp_sys
import tempfile as _acp_tempfile

if "--output-last-message" not in _acp_sys.argv:
    _acp_model = "offline-fixture"
    _acp_models = ["offline-fixture", "offline-owner-choice", "offline-rules-check",
                   "offline-material-check", "offline-maintenance", "another-offline-model"]

    _acp_fixture_mode = _acp_json.loads(_AcpPath(__file__).with_suffix(".json").read_text()).get("mode", "normal")
    _acp_models.append("offline-fixture-" + _acp_fixture_mode)
    _acp_initial_prompt = None
    _acp_turn = 0

    def _acp_options():
        """_acp_options() 声明离线模型标识，使真正的 ACP 选择确认路径参与测试。"""
        return [{"id": "model", "name": "Model", "type": "select", "category": "model",
                 "currentValue": _acp_model,
                 "options": [{"value": name, "name": name} for name in _acp_models]}]

    def _acp_emit(record):
        """_acp_emit() 每次提交一条完整 JSON-RPC 记录，避免多阶段调用等待缓冲输出。"""
        print(_acp_json.dumps(record, ensure_ascii=False), flush=True)

    for _acp_line in _acp_sys.stdin:
        _acp_request = _acp_json.loads(_acp_line)
        if "id" not in _acp_request:
            continue
        _acp_method = _acp_request["method"]
        _acp_params = _acp_request.get("params", {})
        if _acp_method == "initialize":
            _acp_result = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": [],
                           "agentInfo": {"name": "offline-review-fixture", "version": "1"}}
        elif _acp_method == "session/new":
            _acp_initial_prompt = None
            _acp_turn = 0
            _acp_result = {"sessionId": "offline-session", "configOptions": _acp_options()}
        elif _acp_method == "session/set_config_option":
            _acp_model = _acp_params["value"]
            _acp_result = {"configOptions": _acp_options()}
        elif _acp_method == "session/prompt":
            _acp_prompt = "\n".join(block["text"] for block in _acp_params["prompt"] if block["type"] == "text")
            _acp_turn += 1
            _acp_feedback = ""
            if _acp_initial_prompt is None:
                _acp_schema = _acp_json.JSONDecoder().raw_decode(
                    _acp_prompt.split("Response JSON Schema:\n", 1)[1])[0]
                _acp_stage = _acp_prompt.split("Review stage: ", 1)[1].splitlines()[0]
                _acp_initial_prompt = _acp_prompt
            else:
                _acp_feedback = _acp_prompt
                _acp_prompt = _acp_initial_prompt
            with _acp_tempfile.TemporaryDirectory(prefix="clarity-acp-semantic-fixture-") as _acp_temporary:
                _acp_directory = _AcpPath(_acp_temporary) / _acp_stage / "attempt"
                _acp_directory.mkdir(parents=True)
                _acp_output = _acp_directory / "output.json"
                _acp_schema_path = _acp_directory / "schema.json"
                _acp_schema_path.write_text(_acp_json.dumps(_acp_schema, ensure_ascii=False))
                _acp_process = _acp_subprocess.run(
                    [_acp_sys.executable, __file__, "--output-last-message", str(_acp_output),
                     "--output-schema", str(_acp_schema_path), "--model", _acp_model],
                    input=_acp_prompt, text=True, capture_output=True,
                    env=dict(_acp_environment, CLARITY_FIXTURE_TURN=str(_acp_turn),
                             CLARITY_FIXTURE_FEEDBACK=_acp_feedback))
                if _acp_process.returncode:
                    _acp_emit({"jsonrpc": "2.0", "id": _acp_request["id"], "error": {
                        "code": -32000, "message": "Offline model fixture exited with status " + str(_acp_process.returncode)}})
                    continue
                for _acp_event_line in _acp_process.stdout.splitlines():
                    _acp_event = _acp_json.loads(_acp_event_line)
                    if _acp_event.get("item", {}).get("type") == "command_execution":
                        _acp_emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                            "sessionId": "offline-session", "update": {"sessionUpdate": "tool_call",
                            "toolCallId": "forbidden", "title": "Forbidden fixture tool",
                            "kind": "execute", "status": "in_progress"}}})
                _acp_emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "offline-session", "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": _acp_output.read_text()}}}})
            _acp_result = {"stopReason": "end_turn"}
        else:
            _acp_emit({"jsonrpc": "2.0", "id": _acp_request["id"], "error": {
                "code": -32601, "message": "Unsupported offline ACP method: " + _acp_method}})
            continue
        _acp_emit({"jsonrpc": "2.0", "id": _acp_request["id"], "result": _acp_result})
    _acp_sys.exit(0)
'''
