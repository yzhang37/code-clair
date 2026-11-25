"""test_acp_model.py 通过确定性 ACP 服务核对通用客户端，不调用真实模型。"""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from acp_model import AcpModel, _stop_process
from model_common import ModelFailure


AVAILABLE = importlib.util.find_spec("acp") is not None and importlib.util.find_spec("jsonschema") is not None
SCHEMA = {"type": "object", "properties": {"accepted": {"type": "boolean"}},
          "required": ["accepted"], "additionalProperties": False}

SERVER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

mode, capture = sys.argv[1], Path(sys.argv[2])
session_id = "transport-fixture"
model = "fixture/default"

def send(value):
    print(json.dumps(value), flush=True)

def reply(identifier, result):
    send({"jsonrpc": "2.0", "id": identifier, "result": result})

def update(kind, **fields):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id, "update": {"sessionUpdate": kind, **fields}}})

def options():
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": model, "options": [{"value": "fixture/default", "name": "Default"},
                                                   {"value": "fixture/selected", "name": "Selected"}]}]

for line in sys.stdin:
    message = json.loads(line)
    with capture.open("a") as saved:
        saved.write(json.dumps(message) + "\n")
    method = message.get("method")
    identifier = message.get("id")
    if method == "initialize":
        if mode == "bad_protocol":
            reply(identifier, {"protocolVersion": 99})
        else:
            reply(identifier, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "session/new":
        update("agent_message_chunk", content={"type": "text", "text": "Startup information"})
        response = {"sessionId": session_id}
        if mode != "no_model_option":
            response["configOptions"] = options()
        reply(identifier, response)
    elif method == "session/set_config_option":
        if mode != "ignore_model":
            model = message["params"]["value"]
        reply(identifier, {"configOptions": options()})
    elif method == "session/prompt":
        if mode == "timeout":
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
            capture.with_suffix(".pid").write_text(str(child.pid))
            time.sleep(60)
        if mode in {"permission", "read", "terminal"}:
            request_method = {"permission": "session/request_permission", "read": "fs/read_text_file",
                              "terminal": "terminal/create"}[mode]
            params = {"sessionId": session_id}
            if mode == "permission":
                params.update(toolCall={"toolCallId": "tool", "title": "Try action", "kind": "execute",
                                        "status": "pending"},
                              options=[{"optionId": "yes", "name": "Allow", "kind": "allow_once"}])
            elif mode == "read":
                params["path"] = "/not-provided"
            else:
                params["command"] = "not-provided"
            send({"jsonrpc": "2.0", "id": "external-request", "method": request_method, "params": params})
            answer = json.loads(sys.stdin.readline())
            with capture.open("a") as saved:
                saved.write(json.dumps(answer) + "\n")
        if mode == "tool":
            update("tool_call", toolCallId="tool", title="Unexpected tool", kind="execute", status="completed")
        if mode == "nontext":
            update("agent_message_chunk", content={"type": "resource_link", "uri": "file:///not-provided", "name": "file"})
        body = {"duplicate": '{"accepted":true,"accepted":false}',
                "nan": '{"accepted":NaN}', "array": '[]', "schema": '{"accepted":"yes"}',
                "markdown": '```json\n{"accepted":true}\n```', "empty": ''}.get(mode, '{"accepted":true}')
        update("agent_thought_chunk", content={"type": "text", "text": "Transport fixture reasoning"})
        for text in (body[:5], body[5:]):
            update("agent_message_chunk", content={"type": "text", "text": text})
        if mode == "other_session":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "other-session", "update": {"sessionUpdate": "agent_message_chunk",
                                                           "content": {"type": "text", "text": "{}"}}}})
        reply(identifier, {"stopReason": mode if mode in {"cancelled", "max_tokens", "refusal"} else "end_turn"})
        if mode == "exit_failure":
            sys.exit(7)
'''


@unittest.skipUnless(AVAILABLE, "ACP client dependencies are not installed")
class AcpModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-acp-test-")
        self.root = Path(self.temporary.name)
        self.server = self.root / "agent.py"
        self.server.write_text(SERVER)
        self.capture = self.root / "requests.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, mode="success", model=None, timeout=3):
        stage = self.root / mode / "attempt"
        client = AcpModel([sys.executable, str(self.server), mode, str(self.capture)], model, timeout)
        result = client.run("Transport-only fixture prompt", SCHEMA, stage)
        return result, stage

    def test_sdk_handshake_collects_only_current_answer_and_saves_artifacts(self):
        result, stage = self.invoke()
        self.assertEqual(result, {"accepted": True})
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([message["method"] for message in messages],
                         ["initialize", "session/new", "session/prompt"])
        self.assertFalse(messages[0]["params"]["clientCapabilities"].get("terminal", False))
        self.assertEqual(messages[1]["params"]["mcpServers"], [])
        self.assertNotEqual(messages[1]["params"]["cwd"], str(Path.cwd()))
        prompt = messages[2]["params"]["prompt"][0]["text"]
        self.assertTrue(prompt.startswith("Review stage: success\n"))
        self.assertIn('"additionalProperties": false', prompt)
        self.assertTrue(prompt.endswith("Transport-only fixture prompt"))
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        for name in ("schema.json", "output.json", "events.jsonl", "stderr.log", "instructions.txt"):
            self.assertEqual((stage / name).stat().st_mode & 0o777, 0o600)

    def test_explicit_model_uses_and_confirms_advertised_config_option(self):
        self.assertEqual(self.invoke(model="fixture/selected")[0], {"accepted": True})
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        selection = next(message for message in messages if message.get("method") == "session/set_config_option")
        self.assertEqual(selection["params"]["value"], "fixture/selected")

    def test_explicit_model_cannot_be_silently_ignored_or_unavailable(self):
        for mode, model in (("ignore_model", "fixture/selected"), ("no_model_option", "fixture/selected"),
                            ("success", "fixture/not-offered")):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode, model)

    def test_incomplete_responses_or_invalid_json_never_become_results(self):
        for mode in ("cancelled", "max_tokens", "refusal", "duplicate", "nan", "array", "schema",
                     "markdown", "empty", "nontext", "other_session", "bad_protocol", "exit_failure"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode)

    def test_tools_and_host_requests_are_denied_even_with_valid_answer(self):
        for mode in ("tool", "permission", "read", "terminal"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode)
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        replies = [message for message in messages if message.get("id") == "external-request"]
        permission = next(message for message in replies if "result" in message)
        self.assertEqual(permission["result"]["outcome"]["outcome"], "cancelled")
        self.assertTrue(any("error" in message for message in replies))

    def test_timeout_stops_the_adapter_and_its_child(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ModelFailure, "timed out"):
            self.invoke("timeout", timeout=0.2)
        self.assertLess(time.monotonic() - started, 5)
        pid = int(self.capture.with_suffix(".pid").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("Timed-out agent left its child running")

    def test_existing_artifacts_are_preserved_without_starting_agent(self):
        stage = self.root / "occupied" / "attempt"
        stage.mkdir(parents=True)
        (stage / "output.json").write_text("owner evidence")
        client = AcpModel([sys.executable, str(self.server), "success", str(self.capture)])
        with self.assertRaisesRegex(ModelFailure, "already exist"):
            client.run("A prompt", SCHEMA, stage)
        self.assertEqual((stage / "output.json").read_text(), "owner evidence")
        self.assertFalse(self.capture.exists())

    def test_cleanly_exited_adapter_is_not_signalled_again(self):
        import asyncio

        class Finished:
            returncode = 0

        with patch("acp_model.os.killpg", side_effect=PermissionError) as signal_group:
            asyncio.run(_stop_process(Finished()))
        signal_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
