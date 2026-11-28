"""test_acp_model.py 通过确定性 ACP 服务核对通用客户端，不调用真实模型。"""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from acp_model import AcpModel, _ReviewClient, _stop_process
from model_common import ModelFailure, _CONTEXT_INSTRUCTIONS, _INSTRUCTIONS


AVAILABLE = importlib.util.find_spec("acp") is not None and importlib.util.find_spec("jsonschema") is not None
SCHEMA = {"type": "object", "properties": {"accepted": {"type": "boolean"}},
          "required": ["accepted"], "additionalProperties": False}

SERVER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

mode, capture = sys.argv[1], Path(sys.argv[2])
session_id = "transport-fixture"
model = "fixture/default"
startup = "Synthetic adapter startup notice.\n"
answer_mode = {"banner_bad_json": "markdown", "banner_bad_schema": "schema",
               "banner_refusal": "refusal", "banner_max_tokens": "max_tokens"}.get(mode, mode)

def send(value):
    print(json.dumps(value), flush=True)

def reply(identifier, result):
    send({"jsonrpc": "2.0", "id": identifier, "result": result})

def update(update_type, **fields):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id, "update": {"sessionUpdate": update_type, **fields}}})

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
        if not mode.startswith("banner_"):
            update("agent_message_chunk", content={"type": "text", "text": "Startup information"})
        elif mode in {"banner_before_response", "banner_echo_after_early"}:
            update("agent_message_chunk", content={"type": "text", "text": startup})
        response = {"sessionId": session_id}
        if mode.startswith("banner_") and mode != "banner_missing_metadata":
            response["_meta"] = {"fixture": {"startup": None if mode == "banner_null_metadata" else
                                           1 if mode == "banner_invalid_metadata" else startup}}
        if mode != "no_model_option":
            response["configOptions"] = options()
        reply(identifier, response)
    elif method == "session/set_config_option":
        if mode == "banner_before_prompt":
            update("agent_message_chunk", content={"type": "text", "text": startup})
        if mode != "ignore_model":
            model = message["params"]["value"]
        reply(identifier, {"configOptions": options()})
    elif method == "session/prompt":
        if mode.startswith("banner_") and mode not in {"banner_before_response", "banner_before_prompt",
                                                        "banner_suffix"}:
            if mode == "banner_prefix":
                update("agent_message_chunk", content={"type": "text", "text": "Unrelated prefix."})
            if mode == "banner_split":
                update("agent_message_chunk", content={"type": "text", "text": startup[:8]})
                update("agent_message_chunk", content={"type": "text", "text": startup[8:]})
            elif mode == "banner_wrong_session":
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "other-session", "update": {"sessionUpdate": "agent_message_chunk",
                                                               "content": {"type": "text", "text": startup}}}})
            else:
                text = startup.rstrip("\n") if mode == "banner_changed" else (
                       startup + '{"accepted":true}' if mode == "banner_combined" else startup)
                fields = {"messageId": "answer"} if mode == "banner_message_id" else {}
                update("agent_message_chunk", content={"type": "text", "text": text}, **fields)
            if mode == "banner_duplicate":
                update("agent_message_chunk", content={"type": "text", "text": startup})
            if mode == "banner_answer_prefix":
                update("agent_message_chunk", content={"type": "text", "text": "Unrelated answer prefix."})
        if mode == "provider_error":
            send({"jsonrpc": "2.0", "id": identifier,
                  "error": {"code": -32000, "message": "Synthetic provider failure"}})
            continue
        if mode == "timeout":
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
            capture.with_suffix(".pid").write_text(str(child.pid))
            time.sleep(60)
        if mode in {"permission", "read", "write", "terminal"}:
            request_method = {"permission": "session/request_permission", "read": "fs/read_text_file",
                              "write": "fs/write_text_file", "terminal": "terminal/create"}[mode]
            params = {"sessionId": session_id}
            if mode == "permission":
                params.update(toolCall={"toolCallId": "tool", "title": "Try action", "kind": "execute",
                                        "status": "pending"},
                              options=[{"optionId": "yes", "name": "Allow", "kind": "allow_once"}])
            elif mode in {"read", "write"}:
                params["path"] = "/not-provided"
                if mode == "write":
                    params["content"] = "not allowed"
            else:
                params["command"] = "not-provided"
            send({"jsonrpc": "2.0", "id": "external-request", "method": request_method, "params": params})
            answer = json.loads(sys.stdin.readline())
            with capture.open("a") as saved:
                saved.write(json.dumps(answer) + "\n")
        if mode == "tool":
            update("tool_call", toolCallId="tool", title="Unexpected tool", kind="execute", status="completed")
        if mode in {"message_ids", "pi_tools", "same_id", "unmarked_progress", "invalid_final", "tool_no_final", "banner_with_tools"}:
            initial = '{"accepted":false}' if mode in {"invalid_final", "tool_no_final"} else "Inspecting related code."
            fields = {} if mode in {"pi_tools", "unmarked_progress", "banner_with_tools"} else {"messageId": "progress"}
            update("agent_message_chunk", content={"type": "text", "text": initial}, **fields)
        if mode in {"message_ids", "pi_tools", "banner_with_tools"}:
            update("plan", entries=[{"content": "Inspect related code", "priority": "medium", "status": "completed"}])
            update("session_info_update", _meta={"fixture": {"state": "working"}})
            update("usage_update", used=10, size=100)
            update("tool_call", toolCallId="context", title="Read frozen context", kind="execute", status="in_progress")
            update("tool_call_update", toolCallId="context", kind="other", status="completed",
                   rawOutput={"text": '{"not_the_answer":true}'})
        if mode == "nontext":
            update("agent_message_chunk", content={"type": "resource_link", "uri": "file:///not-provided", "name": "file"})
        body = {"duplicate": '{"accepted":true,"accepted":false}', "invalid_final": 'not JSON',
                "nan": '{"accepted":NaN}', "array": '[]', "schema": '{"accepted":"yes"}',
                "markdown": '```json\n{"accepted":true}\n```', "empty": '', "banner_only": ''}.get(answer_mode, '{"accepted":true}')
        update("agent_thought_chunk", content={"type": "text", "text": "Transport fixture reasoning"})
        fields = {"messageId": "final"} if mode in {"message_ids", "invalid_final"} else (
                 {"messageId": "progress"} if mode == "same_id" else (
                 {"messageId": "answer"} if mode == "banner_message_id" else {}))
        for text in (body[:5], body[5:]):
            if mode not in {"tool_no_final", "banner_combined"}:
                update("agent_message_chunk", content={"type": "text", "text": text}, **fields)
        if mode in {"banner_suffix", "banner_answer_suffix"}:
            text = startup if mode == "banner_suffix" else "Unrelated answer suffix."
            update("agent_message_chunk", content={"type": "text", "text": text})
        if mode == "tool_no_final":
            update("tool_call", toolCallId="last", title="Last action", kind="read", status="completed")
        if mode == "other_session":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "other-session", "update": {"sessionUpdate": "agent_message_chunk",
                                                           "content": {"type": "text", "text": "{}"}}}})
        reply(identifier, {"stopReason": answer_mode if answer_mode in {"cancelled", "max_tokens", "refusal"} else "end_turn"})
        if mode == "exit_failure":
            sys.exit(7)
'''


FEEDBACK_SERVER = r'''
import json, sys, time
from pathlib import Path

mode, capture = sys.argv[1], Path(sys.argv[2])
session_id = "feedback-session"
startup = "Declared startup.\n"
turn = 0
closed = mode.startswith("closed_")
mode = mode.removeprefix("closed_")

def send(value):
    print(json.dumps(value), flush=True)

def update(kind, **fields):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id, "update": {"sessionUpdate": kind, **fields}}})

def reply(identifier, result):
    send({"jsonrpc": "2.0", "id": identifier, "result": result})

for line in sys.stdin:
    request = json.loads(line)
    with capture.open("a") as saved:
        saved.write(json.dumps(request) + "\n")
    method, identifier = request.get("method"), request.get("id")
    if method == "initialize":
        reply(identifier, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method == "session/new":
        reply(identifier, {"sessionId": session_id, "_meta": {"fixture": {"startup": startup}}})
    elif method == "session/prompt":
        turn += 1
        if turn == 1:
            update("agent_message_chunk", content={"type": "text", "text": startup})
        if mode == "provider_error":
            send({"jsonrpc": "2.0", "id": identifier,
                  "error": {"code": -32000, "message": "Synthetic provider failure"}})
            continue
        action = mode.removeprefix("second_")
        if action in {"permission", "read", "write", "terminal"} and (not mode.startswith("second_") or turn == 2):
            params = {"sessionId": session_id}
            if action == "permission":
                method = "session/request_permission"
                params.update(toolCall={"toolCallId": "forbidden", "title": "Forbidden action", "kind": "execute"},
                              options=[{"optionId": "yes", "name": "Allow", "kind": "allow_once"}])
            elif action in {"read", "write"}:
                method = "fs/read_text_file" if action == "read" else "fs/write_text_file"
                params["path"] = "/not-provided"
                if action == "write":
                    params["content"] = "not allowed"
            else:
                method = "terminal/create"
                params["command"] = "not-provided"
            send({"jsonrpc": "2.0", "id": "forbidden", "method": method, "params": params})
            answer = json.loads(sys.stdin.readline())
            with capture.open("a") as saved:
                saved.write(json.dumps(answer) + "\n")
        if not closed or (mode == "tool_first" and turn == 1) or (mode == "tool_second" and turn == 2):
            update("tool_call", toolCallId=f"read-{turn}", title=f"Frozen body {turn}", status="completed",
                   rawOutput={"body": f"source-body-{turn}"})
        if mode == "total_timeout":
            time.sleep(0.45)
        if mode == "repeat_startup" and turn == 2:
            update("agent_message_chunk", content={"type": "text", "text": startup})
        if mode == "nontext":
            update("agent_message_chunk", content={"type": "resource_link", "uri": "file:///not-provided", "name": "file"})
        if mode == "other_session":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "another-session", "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "{}"}}}})
        body = '{"accepted":false}' if turn == 1 else '{"accepted":true}'
        if mode == "empty":
            body = ""
        if (mode == "fenced_first" and turn == 1) or (mode == "fenced_final" and turn == 2):
            body = "```json\n" + body + "\n```"
        if mode == "schema_final" and turn == 2:
            body = '{"accepted":"yes"}'
        for text in (body[:6], body[6:]):
            update("agent_message_chunk", content={"type": "text", "text": text})
        reply(identifier, {"stopReason": mode if mode in {"refusal", "max_tokens", "cancelled"} else "end_turn"})
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

    def invoke(self, mode="success", model=None, timeout=3, **kwargs):
        stage = self.root / mode / "attempt"
        client = AcpModel([sys.executable, str(self.server), mode, str(self.capture)], model, timeout, **kwargs)
        result = client.run("Transport-only fixture prompt", SCHEMA, stage)
        return result, stage

    def invoke_feedback(self, mode="success", callback=None, timeout=3, closed=False, **kwargs):
        self.server.write_text(FEEDBACK_SERVER)
        if closed:
            mode = "closed_" + mode
        options = {} if closed else {"cwd": self.root, "allow_tools": True}
        return self.invoke(mode, timeout=timeout, startup_metadata_path=("fixture", "startup"),
                           reply_feedback=callback, **options, **kwargs)

    def captured_prompts(self):
        return [message for line in self.capture.read_text().splitlines()
                if (message := json.loads(line)).get("method") == "session/prompt"]

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
        self.assertEqual((stage / "instructions.txt").read_text(), _INSTRUCTIONS)
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
                     "markdown", "empty", "nontext", "other_session", "bad_protocol", "exit_failure", "provider_error"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode)

    def test_tools_and_host_requests_are_denied_even_with_valid_answer(self):
        for mode in ("tool", "permission", "read", "write", "terminal"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode)
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        replies = [message for message in messages if message.get("id") == "external-request"]
        permission = next(message for message in replies if "result" in message)
        self.assertEqual(permission["result"]["outcome"]["outcome"], "cancelled")
        self.assertTrue(any("error" in message for message in replies))

    def test_context_uses_supplied_cwd_and_sdk_stdio_mcp_server(self):
        view = self.root / "frozen"
        view.mkdir()
        servers = [{"name": "clarity", "command": sys.executable,
                    "args": ["context_server.py", "--root", str(view)], "env": []}]
        result, stage = self.invoke("message_ids", cwd=view, mcp_servers=servers, allow_tools=True)
        self.assertEqual(result, {"accepted": True})
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        session = next(message for message in messages if message.get("method") == "session/new")
        self.assertEqual(session["params"]["cwd"], str(view.resolve()))
        self.assertEqual(session["params"]["mcpServers"], servers)
        self.assertEqual((stage / "instructions.txt").read_text(), _CONTEXT_INSTRUCTIONS)
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("category") == "tool" for event in events))
        self.assertTrue(any(event.get("category") == "plan" for event in events))
        self.assertTrue(any(event["type"] == "answer/progress" and event["message_id"] == "progress"
                            for event in events))
        self.assertEqual(events[-1], {"type": "answer/final", "message_id": "final", "text": '{"accepted":true}'})

    def test_context_tool_boundary_groups_messages_without_ids(self):
        result, stage = self.invoke("pi_tools", cwd=self.root, allow_tools=True)
        self.assertEqual(result, {"accepted": True})
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["type"] == "answer/progress" and event["text"] == "Inspecting related code."
                            for event in events))

    def test_context_does_not_choose_a_json_fragment_or_an_earlier_message(self):
        for mode in ("same_id", "unmarked_progress", "invalid_final", "tool_no_final", "duplicate", "nan", "array", "schema",
                     "markdown", "empty", "nontext", "other_session", "cancelled", "refusal", "provider_error"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode, cwd=self.root, allow_tools=True)

    def test_context_keeps_host_permission_filesystem_and_terminal_requests_denied(self):
        for mode in ("permission", "read", "write", "terminal"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode, cwd=self.root, allow_tools=True)

    def test_unknown_update_still_invalidates_review(self):
        import asyncio
        import io

        client = _ReviewClient(io.StringIO(), allow_tools=True)
        client.session_id = "transport-fixture"
        update = SimpleNamespace(model_dump=lambda **kwargs: {"sessionUpdate": "unknown_update"})
        asyncio.run(client.session_update("transport-fixture", update))
        self.assertEqual(client.violation, "ACP agent emitted an unsupported review update")

    def test_context_options_require_an_explicit_view_and_boolean_tool_choice(self):
        for options in ({"allow_tools": True}, {"allow_tools": 1}, {"mcp_servers": [{}]},
                        {"mcp_servers": "not a list"}, {"instructions": ""},
                        {"max_request_chars": 0}, {"max_request_chars": True}, {"max_request_chars": 1.5},
                        {"startup_metadata_path": []}, {"startup_metadata_path": ()},
                        {"startup_metadata_path": ("",)}, {"startup_metadata_path": (1,)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AcpModel([sys.executable], **options)

    def test_declared_whole_startup_notification_is_separate_from_closed_answer(self):
        result, stage = self.invoke("banner_during_prompt", startup_metadata_path=("fixture", "startup"))
        self.assertEqual(result, {"accepted": True})
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
        startup = [event for event in events if event["type"] == "adapter/startup"]
        self.assertEqual(startup, [{"type": "adapter/startup", "session_id": "transport-fixture",
                                    "metadata_path": ["fixture", "startup"], "timing": "during_prompt"}])
        self.assertTrue(any(event.get("update", {}).get("content", {}).get("text") ==
                            "Synthetic adapter startup notice.\n" for event in events))

    def test_startup_metadata_is_opt_in_and_never_a_prefix_or_json_parser(self):
        with self.assertRaises(ModelFailure):
            self.invoke("banner_during_prompt")
        for mode in ("banner_missing_metadata", "banner_null_metadata", "banner_invalid_metadata", "banner_changed",
                     "banner_split", "banner_combined", "banner_duplicate", "banner_prefix", "banner_suffix",
                     "banner_answer_prefix", "banner_answer_suffix", "banner_message_id", "banner_only",
                     "banner_wrong_session", "banner_echo_after_early", "banner_bad_json", "banner_bad_schema",
                     "banner_refusal", "banner_max_tokens"):
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke(mode, startup_metadata_path=("fixture", "startup"))

    def test_early_startup_is_consumed_before_response_or_before_prompt(self):
        for mode, model in (("banner_before_response", None), ("banner_before_prompt", "fixture/default")):
            with self.subTest(mode=mode):
                result, stage = self.invoke(mode, model=model, startup_metadata_path=("fixture", "startup"))
                self.assertEqual(result, {"accepted": True})
                events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
                notices = [event for event in events if event["type"] == "adapter/startup"]
                self.assertEqual(len(notices), 1)
                self.assertIn(notices[0]["timing"], {"before_session_response", "before_prompt"})

    def test_declared_startup_does_not_change_tool_or_answer_boundaries(self):
        result, stage = self.invoke("banner_with_tools", cwd=self.root, allow_tools=True,
                                    startup_metadata_path=("fixture", "startup"))
        self.assertEqual(result, {"accepted": True})
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        command = [sys.executable, str(self.server), "banner_with_tools", str(self.capture)]
        client = AcpModel(command, startup_metadata_path=("fixture", "startup"))
        with self.assertRaisesRegex(ModelFailure, "attempted a tool"):
            client.run("Closed stage", SCHEMA, self.root / "closed_banner_tools" / "attempt")

    def test_each_invocation_has_its_own_startup_consumption(self):
        command = [sys.executable, str(self.server), "banner_during_prompt", str(self.capture)]
        client = AcpModel(command, startup_metadata_path=("fixture", "startup"))
        for attempt in ("first", "second"):
            with self.subTest(attempt=attempt):
                self.assertEqual(client.run("Closed stage", SCHEMA, self.root / "private_session" / attempt),
                                 {"accepted": True})

    def test_feedback_reuses_native_session_and_keeps_turn_evidence_separate(self):
        feedback = "Read the missing frozen baseline body, then return the original response schema.\n"
        callback = Mock(return_value=feedback)
        result, stage = self.invoke_feedback(callback=callback)
        self.assertEqual(result, {"accepted": True})
        callback.assert_called_once_with('{"accepted":false}')
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([message.get("method") for message in messages],
                         ["initialize", "session/new", "session/prompt", "session/prompt"])
        prompts = self.captured_prompts()
        self.assertEqual([message["params"]["sessionId"] for message in prompts], ["feedback-session"] * 2)
        self.assertEqual(prompts[1]["params"]["prompt"], [{"type": "text", "text": feedback}])
        self.assertEqual((stage / "output.json").read_text(), '{"accepted":true}')
        events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(event["type"] == "adapter/startup" for event in events), 1)
        self.assertEqual([event["update"]["rawOutput"]["body"] for event in events
                          if event.get("category") == "tool"], ["source-body-1", "source-body-2"])
        self.assertIn({"type": "answer/provisional", "message_id": None, "text": '{"accepted":false}'}, events)
        self.assertIn({"type": "answer/feedback", "text": feedback}, events)
        self.assertEqual(events[-1], {"type": "answer/final", "message_id": None, "text": '{"accepted":true}'})

    def test_feedback_default_or_no_gap_keeps_a_single_turn(self):
        for mode, callback in (("success", None), ("no_gap", Mock(return_value=None)),
                               ("empty_feedback", Mock(return_value="")), ("blank_feedback", Mock(return_value=" \n"))):
            with self.subTest(mode=mode):
                result, _ = self.invoke_feedback(mode, callback=callback)
                self.assertEqual(result, {"accepted": False})
                if callback is not None:
                    callback.assert_called_once_with('{"accepted":false}')
        self.assertEqual(len(self.captured_prompts()), 4)

    def test_feedback_may_correct_first_raw_format_but_final_format_stays_strict(self):
        callback = Mock(return_value="Return only the original schema JSON object.")
        result, _ = self.invoke_feedback("fenced_first", callback=callback)
        self.assertEqual(result, {"accepted": True})
        callback.assert_called_once_with('```json\n{"accepted":false}\n```')
        final_callback = Mock(return_value="A gap remains.")
        with self.assertRaises(ModelFailure):
            self.invoke_feedback("fenced_final", callback=final_callback)
        final_callback.assert_called_once_with('{"accepted":false}')
        self.assertEqual((self.root / "fenced_final" / "attempt" / "output.json").read_text(),
                         '```json\n{"accepted":true}\n```')
        self.assertEqual(len(self.captured_prompts()), 4)

    def test_feedback_does_not_reset_startup_consumption_or_allow_third_prompt(self):
        callback = Mock(return_value="Read the remaining source.")
        with self.assertRaises(ModelFailure):
            self.invoke_feedback("repeat_startup", callback=callback)
        callback.assert_called_once_with('{"accepted":false}')
        self.assertEqual(len(self.captured_prompts()), 2)

    def test_feedback_never_repairs_protocol_host_permission_or_stop_failures(self):
        modes = ("permission", "read", "write", "terminal", "refusal", "max_tokens", "cancelled",
                 "empty", "nontext", "other_session", "provider_error")
        for mode in modes:
            callback = Mock(return_value="Do not use for a failed turn.")
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke_feedback(mode, callback=callback)
            callback.assert_not_called()
        self.assertEqual(len(self.captured_prompts()), len(modes))
        callback = Mock(return_value="Read remaining source.")
        with self.assertRaisesRegex(ModelFailure, "requested tool permission"):
            self.invoke_feedback("second_permission", callback=callback)
        callback.assert_called_once()
        self.assertEqual(len(self.captured_prompts()), len(modes) + 2)

    def test_feedback_shares_the_original_total_timeout(self):
        callback = Mock(return_value="Read the missing source.")
        started = time.monotonic()
        with self.assertRaisesRegex(ModelFailure, "timed out"):
            self.invoke_feedback("total_timeout", callback=callback, timeout=0.75)
        self.assertLess(time.monotonic() - started, 2)
        callback.assert_called_once_with('{"accepted":false}')
        self.assertEqual(len(self.captured_prompts()), 2)

    def test_feedback_request_uses_the_same_character_cap_without_truncation(self):
        limit = 5000
        text = "验证" * (limit // 2)
        result, _ = self.invoke_feedback("at_cap", callback=lambda reply: text, max_request_chars=limit)
        self.assertEqual(result, {"accepted": True})
        self.assertEqual(self.captured_prompts()[1]["params"]["prompt"][0]["text"], text)
        with self.assertRaisesRegex(ModelFailure, f"contains {limit + 1} characters.*limit {limit}"):
            self.invoke_feedback("over_cap", callback=lambda reply: text + "x", max_request_chars=limit)
        self.assertEqual(len(self.captured_prompts()), 3)

    def test_feedback_callback_rejects_noncallable_or_nontext_results(self):
        for options in ({"reply_feedback": "invalid"},
                        {"reply_feedback": "invalid", "cwd": self.root, "allow_tools": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AcpModel([sys.executable], **options)
        with self.assertRaisesRegex(ModelFailure, "must be text or null"):
            self.invoke_feedback(callback=lambda text: 1)
        self.assertEqual(len(self.captured_prompts()), 1)

    def test_closed_feedback_reuses_session_without_granting_tools_or_host_capabilities(self):
        feedback = "Correct the response format using the same supplied materials."
        callback = Mock(return_value=feedback)
        result, stage = self.invoke_feedback("fenced_first", callback=callback, closed=True)
        self.assertEqual(result, {"accepted": True})
        callback.assert_called_once_with('```json\n{"accepted":false}\n```')
        requests = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([request.get("method") for request in requests],
                         ["initialize", "session/new", "session/prompt", "session/prompt"])
        capabilities = requests[0]["params"]["clientCapabilities"]
        self.assertFalse(capabilities.get("terminal", False))
        self.assertFalse(capabilities.get("fs", {}).get("readTextFile", False))
        self.assertFalse(capabilities.get("fs", {}).get("writeTextFile", False))
        self.assertEqual(requests[1]["params"]["mcpServers"], [])
        self.assertNotEqual(requests[1]["params"]["cwd"], str(self.root))
        prompts = self.captured_prompts()
        self.assertEqual([prompt["params"]["sessionId"] for prompt in prompts], ["feedback-session"] * 2)
        self.assertEqual(prompts[1]["params"]["prompt"], [{"type": "text", "text": feedback}])
        self.assertEqual((stage / "instructions.txt").read_text(), _INSTRUCTIONS)
        events = [json.loads(line) for line in (stage / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(event["type"] == "answer/provisional" for event in events), 1)
        self.assertFalse(any(event.get("category") == "tool" for event in events))

    def test_closed_feedback_keeps_final_json_strict_and_stops_after_second_turn(self):
        for mode, final in (("fenced_final", '```json\n{"accepted":true}\n```'),
                            ("schema_final", '{"accepted":"yes"}')):
            before = len(self.captured_prompts()) if self.capture.exists() else 0
            callback = Mock(return_value="Return only the requested JSON object.")
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke_feedback(mode, callback=callback, closed=True)
            callback.assert_called_once_with('{"accepted":false}')
            self.assertEqual(len(self.captured_prompts()) - before, 2)
            self.assertEqual((self.root / ("closed_" + mode) / "attempt" / "output.json").read_text(), final)

    def test_closed_feedback_cannot_repair_permission_tool_or_incomplete_turns(self):
        for mode, turns in (("tool_first", 1), ("tool_second", 2), ("permission", 1),
                            ("second_permission", 2), ("read", 1), ("second_read", 2),
                            ("write", 1), ("second_write", 2), ("terminal", 1),
                            ("second_terminal", 2), ("refusal", 1), ("max_tokens", 1),
                            ("cancelled", 1), ("empty", 1), ("provider_error", 1)):
            before = len(self.captured_prompts()) if self.capture.exists() else 0
            callback = Mock(return_value="Correct the response format.")
            with self.subTest(mode=mode), self.assertRaises(ModelFailure):
                self.invoke_feedback(mode, callback=callback, closed=True)
            self.assertEqual(callback.call_count, turns - 1)
            self.assertEqual(len(self.captured_prompts()) - before, turns)

    def test_closed_feedback_uses_one_total_timeout_for_both_turns(self):
        callback = Mock(return_value="Correct the response format.")
        started = time.monotonic()
        with self.assertRaisesRegex(ModelFailure, "timed out"):
            self.invoke_feedback("total_timeout", callback=callback, timeout=0.75, closed=True)
        self.assertLess(time.monotonic() - started, 2)
        callback.assert_called_once_with('{"accepted":false}')
        self.assertEqual(len(self.captured_prompts()), 2)

    def test_custom_instructions_are_saved_and_sent(self):
        _, stage = self.invoke(instructions="Use the supplied frozen evidence.")
        self.assertEqual((stage / "instructions.txt").read_text(), "Use the supplied frozen evidence.")
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertIn("Use the supplied frozen evidence.", messages[-1]["params"]["prompt"][0]["text"])

    def test_request_character_limit_measures_full_wrapper_without_truncating_unicode(self):
        prompt = "验证" * 10
        instructions = "Explicit test instructions."
        expected = ("Review stage: capacity\n" + instructions + "\nResponse JSON Schema:\n" +
                    json.dumps(SCHEMA, ensure_ascii=False, allow_nan=False) + "\n\n" + prompt)
        command = [sys.executable, str(self.server), "success", str(self.capture)]
        client = AcpModel(command, instructions=instructions, max_request_chars=len(expected))
        self.assertEqual(client.run(prompt, SCHEMA, self.root / "capacity" / "exact"), {"accepted": True})
        messages = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual(messages[-1]["params"]["prompt"][0]["text"], expected)
        self.assertGreater(len(expected.encode("utf-8")), len(expected))
        self.capture.unlink()
        limit = len(expected) - 1
        client = AcpModel(command, instructions=instructions, max_request_chars=limit)
        with self.assertRaisesRegex(ModelFailure, f"contains {len(expected)} characters.*limit {limit}"):
            client.run(prompt, SCHEMA, self.root / "capacity" / "over")
        self.assertFalse(self.capture.exists())

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
