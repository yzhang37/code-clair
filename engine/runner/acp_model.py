"""acp_model.py 使用同一 ACP Python 客户端调用本地审核 Agent。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import os
from pathlib import Path
import signal
import tempfile

from model_common import ModelFailure, _CONTEXT_INSTRUCTIONS, _INSTRUCTIONS, _load_json


class _ReviewClient:
    """_ReviewClient 按消息边界收集回答，不提供宿主文件或终端能力。"""

    def __init__(self, events, *, allow_tools=False, startup_metadata_path=None):
        """__init__() 将过程记录与回答分开，避免启动提示或推理成为审核 JSON。"""
        self.events = events
        self.session_id = None
        self.receiving = False
        self.parts = []
        self.message_id = None
        self.after_tool = False
        self.allow_tools = allow_tools
        self.startup_metadata_path = startup_metadata_path
        self.first_messages = {}
        self.startup_text = None
        self.violation = None

    def record(self, kind, **values):
        """record() 保存已解析的 ACP 过程，保留调用证据而不解释审核结论。"""
        self.events.write(json.dumps({"type": kind, **values}, ensure_ascii=False, allow_nan=False) + "\n")
        self.events.flush()

    def start_session(self, session_id, metadata):
        """start_session() 绑定会话及调用方声明的启动通知字段，不解释任何 Agent 的文本格式。

        只把该会话首条无消息标识的完整文本与元数据逐字比较，防止将后续相同的模型正文吞掉。
        首条通知可能早于 session/new 响应；它已在回答收取前到达时也记为已识别，不重新开放豁免。
        """
        self.session_id = session_id
        if self.startup_metadata_path is None:
            return
        value = metadata
        for key in self.startup_metadata_path:
            value = value.get(key) if isinstance(value, dict) else None
        self.startup_text = value if isinstance(value, str) and value else None
        first = self.first_messages.get(session_id)
        if first is not None and self._matches_startup(first):
            self.record("adapter/startup", session_id=session_id,
                        metadata_path=self.startup_metadata_path, timing="before_session_response")

    def _matches_startup(self, value):
        """_matches_startup() 只核对声明字段与一条完整通知，不裁剪、规范化或提取 JSON。"""
        content = value.get("content", {})
        return (self.startup_text is not None and value.get("messageId") is None
                and content.get("type") == "text" and content.get("text") == self.startup_text)

    def _startup_notification(self, session_id, value):
        """_startup_notification() 保存首条候选并最多识别一次，使异步早到通知与提示内通知使用同一边界。"""
        if self.startup_metadata_path is None or session_id in self.first_messages:
            return False
        self.first_messages[session_id] = value
        if self.session_id != session_id or not self._matches_startup(value):
            return False
        self.record("adapter/startup", session_id=session_id, metadata_path=self.startup_metadata_path,
                    timing="during_prompt" if self.receiving else "before_prompt")
        return True

    async def session_update(self, session_id, update, **kwargs):
        """session_update() 按 ACP 消息标识收取末条回答，过程事件只保留为调用证据。

        没有 messageId 的 Agent 在工具调用后开始新的文字组，防止工具前的进度说明混入最终 JSON。
        同一 messageId 的分块仍归同一回答；本层不从任意文字中猜选看似合法的 JSON。
        允许工具事件只表示使用已配置的原生上下文工具，不保证 Agent 的操作系统隔离。
        """
        value = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        kind = value.get("sessionUpdate")
        category = ("tool" if kind in {"tool_call", "tool_call_update"} else
                    "plan" if kind == "plan" else
                    "message" if kind == "agent_message_chunk" else
                    "thought" if kind == "agent_thought_chunk" else "state")
        self.record("session/update", session_id=session_id, category=category, update=value)
        if self.session_id is not None and session_id != self.session_id:
            self.violation = "ACP update belongs to another session"
            return
        if kind == "agent_message_chunk" and self._startup_notification(session_id, value):
            return
        if kind in {"tool_call", "tool_call_update"}:
            if not self.allow_tools:
                self.violation = "ACP agent attempted a tool call"
            if self.receiving:
                self.after_tool = True
        elif kind == "agent_message_chunk" and self.receiving:
            content = value.get("content", {})
            if content.get("type") != "text":
                self.violation = "ACP review answer contains unsupported content"
            else:
                message_id = value.get("messageId")
                if self.parts and (message_id != self.message_id or (self.after_tool and message_id is None)):
                    self.record("answer/progress", message_id=self.message_id, text="".join(self.parts))
                    self.parts = []
                self.message_id = message_id
                self.after_tool = False
                self.parts.append(content["text"])
        elif kind not in {
            "agent_message_chunk", "agent_thought_chunk", "user_message_chunk",
            "available_commands_update", "current_mode_update", "config_option_update",
            "session_info_update", "usage_update", "plan",
        }:
            self.violation = "ACP agent emitted an unsupported review update"

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        """request_permission() 拒绝本次动作，并保留已越过静态审核边界的事实。"""
        from acp.schema import DeniedOutcome, RequestPermissionResponse

        self.violation = "ACP agent requested tool permission"
        self.record("permission/denied", session_id=session_id)
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def _deny(self, **kwargs):
        """_deny() 拒绝 Agent 的宿主访问，不将读取或执行操作委派给其他组件。"""
        from acp import RequestError

        self.violation = "ACP agent requested filesystem or terminal access"
        self.record("host-access/denied")
        raise RequestError.method_not_found("Host access is unavailable during static review")

    read_text_file = _deny
    write_text_file = _deny
    create_terminal = _deny
    terminal_output = _deny
    release_terminal = _deny
    wait_for_terminal_exit = _deny
    kill_terminal = _deny


async def _stop_process(process):
    """_stop_process() 关闭本次独立进程组，回收适配器及其启动的 Agent 子进程。"""
    if process.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # 组在关闭输入后可能已退出；不让对已结束组的 EPERM 覆盖正常回答。
            if process.returncode is not None:
                break
            raise
        if process.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1)


class AcpModel:
    """AcpModel 在新会话中通过通用 ACP 协议交换审核材料，不解析各家 CLI 输出。"""

    def __init__(self, command: list[str], model: str | None = None, timeout: float = 300,
                 *, environment: dict[str, str] | None = None, cwd: Path | None = None,
                 mcp_servers: list[dict] | None = None, allow_tools: bool = False,
                 instructions: str | None = None, max_request_chars: int | None = None,
                 startup_metadata_path: tuple[str, ...] | None = None):
        """__init__() 保存已选择的 Agent 启动方式，供应商认证和原生协议由现成适配器负责。"""
        if (not isinstance(command, list) or not command
                or any(not isinstance(item, str) or not item or "\0" in item for item in command)):
            raise ValueError("ACP command must be a nonempty list of arguments")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty string or null")
        if not isinstance(allow_tools, bool):
            raise ValueError("allow_tools must be a boolean")
        if allow_tools and cwd is None:
            raise ValueError("Context tools require a supplied frozen view directory")
        if mcp_servers is not None and (not isinstance(mcp_servers, list)
                                        or any(not isinstance(server, dict) for server in mcp_servers)):
            raise ValueError("MCP servers must be a list of stdio server specifications")
        if mcp_servers and not allow_tools:
            raise ValueError("MCP servers require context tools")
        if instructions is not None and (not isinstance(instructions, str) or not instructions.strip()):
            raise ValueError("instructions must be a nonempty string or null")
        if max_request_chars is not None and (type(max_request_chars) is not int or max_request_chars <= 0):
            raise ValueError("max_request_chars must be a positive integer or null")
        if startup_metadata_path is not None and (not isinstance(startup_metadata_path, tuple)
                                                  or not startup_metadata_path
                                                  or any(not isinstance(key, str) or not key or "\0" in key
                                                         for key in startup_metadata_path)):
            raise ValueError("startup_metadata_path must be a nonempty tuple of metadata field names or null")
        self.command = list(command)
        self.model = model
        self.timeout = timeout
        self.environment = dict(environment) if environment is not None else None
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.mcp_servers = copy.deepcopy(mcp_servers or [])
        self.allow_tools = allow_tools
        self.instructions = instructions if instructions is not None else (
            _CONTEXT_INSTRUCTIONS if allow_tools else _INSTRUCTIONS)
        self.max_request_chars = max_request_chars
        self.startup_metadata_path = startup_metadata_path

    async def _exchange(self, process, work_root, client, request):
        """_exchange() 完成握手、新建会话和一次审核，只接受正常结束的最终文字回答。

        模型仅在明确选择时覆盖；选择使用 Agent 公布的配置项，不把某一家模型名写入协议层。
        SDK 负责 ACP 帧和消息类型，本文本、完成状态及后续结果校验决定能否收取回答。
        """
        from acp import PROTOCOL_VERSION, connect_to_agent
        from acp.schema import ClientCapabilities, Implementation, McpServerStdio, TextContentBlock

        connection = connect_to_agent(client, process.stdin, process.stdout)
        try:
            initialized = await connection.initialize(
                protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="clarity", version="1"),
            )
            client.record("initialize", protocol_version=initialized.protocol_version)
            if initialized.protocol_version != PROTOCOL_VERSION:
                raise ModelFailure("ACP agent uses an unsupported protocol version")
            servers = [McpServerStdio.model_validate(server) for server in self.mcp_servers]
            session = await connection.new_session(cwd=str(work_root), mcp_servers=servers)
            client.start_session(session.session_id, session.field_meta)
            client.record("session/new", session=session.model_dump(mode="json", by_alias=True, exclude_none=True))
            if self.model is not None:
                options = [option for option in session.config_options or [] if option.category == "model"]
                if len(options) != 1:
                    raise ModelFailure("ACP agent did not advertise a model selection option")
                offered = {item.value for option in options[0].options
                           for item in (option.options if hasattr(option, "options") else [option])}
                if self.model not in offered:
                    raise ModelFailure("The selected model was not advertised by the ACP agent")
                selected = await connection.set_config_option(
                    config_id=options[0].id, session_id=session.session_id, value=self.model,
                )
                if not any(option.id == options[0].id and option.current_value == self.model
                           for option in selected.config_options):
                    raise ModelFailure("ACP agent did not confirm the selected model")
                client.record("session/model", model=self.model)
            # 启动提示不是审核回答；只在本次 prompt 发出后收取 agent_message_chunk。
            client.receiving = True
            response = await connection.prompt(
                session_id=session.session_id, prompt=[TextContentBlock(text=request)],
            )
            client.receiving = False
            client.record("session/prompt", response=response.model_dump(mode="json", by_alias=True, exclude_none=True))
            if client.violation:
                raise ModelFailure(client.violation)
            if response.stop_reason != "end_turn":
                raise ModelFailure("ACP agent did not finish the review turn")
            if client.after_tool or not client.parts or not "".join(client.parts).strip():
                raise ModelFailure("ACP agent completed without a review answer")
            if process.returncode not in (None, 0):
                raise ModelFailure("ACP agent exited unsuccessfully")
            text = "".join(client.parts)
            client.record("answer/final", message_id=client.message_id, text=text)
            return text
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=1)

    async def _invoke(self, work_root, client, request, errors):
        """_invoke() 将适配器及 Agent 放入本次进程组，使超时清理不影响其他开发会话。"""
        process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=errors, cwd=work_root, env=self.environment, start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        try:
            text = await asyncio.wait_for(self._exchange(process, work_root, client, request), timeout=self.timeout)
            # ACP 服务可保持长连接；关闭输入后先收取正常退出状态，再回收仍运行的进程。
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.25)
            if process.returncode not in (None, 0):
                raise ModelFailure("ACP agent exited unsuccessfully")
            return text
        except asyncio.TimeoutError as exc:
            raise ModelFailure("ACP agent timed out; the stage is incomplete") from exc
        finally:
            await _stop_process(process)

    def run(self, prompt: str, schema: dict, stage_dir: Path) -> dict:
        """run() 保存一次调用的材料与过程，返回满足 JSON 结构的回答；审核结论由调用者核对。

        默认创建空工作目录并拒绝工具事件；上下文模式使用调用者提供的冻结视图与原生只读工具。
        两种模式均不声明宿主文件或终端能力，Agent 自身的工具配置和访问边界仍由启动层约束。
        进程结束、协议回合完成、结构合法分别核对；任何一项不足都不转换成审核通过。
        """
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(schema, dict):
            raise ModelFailure("ACP review needs a nonempty prompt and an object schema")
        try:
            import jsonschema
            from acp import connect_to_agent
        except ImportError as exc:
            raise ModelFailure("ACP Python dependencies are unavailable") from exc
        del connect_to_agent
        stage_dir = Path(stage_dir).resolve()
        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
            names = ("schema.json", "output.json", "events.jsonl", "stderr.log", "instructions.txt")
            if any((stage_dir / name).exists() or (stage_dir / name).is_symlink() for name in names):
                raise ModelFailure("ACP stage artifacts already exist; use a fresh stage directory")
            for name, content in (("schema.json", json.dumps(schema, ensure_ascii=False, allow_nan=False)),
                                  ("instructions.txt", self.instructions)):
                with (stage_dir / name).open("x", encoding="utf-8") as output:
                    os.chmod(output.name, 0o600)
                    output.write(content)
            jsonschema.Draft202012Validator.check_schema(schema)
            request = ("Review stage: " + stage_dir.parent.name + "\n" + self.instructions + "\nResponse JSON Schema:\n" +
                       json.dumps(schema, ensure_ascii=False, allow_nan=False) + "\n\n" + prompt)
            # 启动层只为已核实有字符上限的 Agent 设置此界限；完整包装也计入，超限不截断审核证据。
            if self.max_request_chars is not None and len(request) > self.max_request_chars:
                raise ModelFailure(f"ACP review request contains {len(request)} characters, exceeding configured "
                                   f"limit {self.max_request_chars}; the stage is incomplete")
            with (stage_dir / "events.jsonl").open("x", encoding="utf-8") as events, \
                    (stage_dir / "stderr.log").open("xb") as errors:
                os.chmod(events.name, 0o600)
                os.chmod(errors.name, 0o600)
                client = _ReviewClient(events, allow_tools=self.allow_tools,
                                       startup_metadata_path=self.startup_metadata_path)
                if self.cwd is None:
                    with tempfile.TemporaryDirectory(prefix="clarity-acp-") as temporary:
                        text = asyncio.run(self._invoke(Path(temporary), client, request, errors))
                else:
                    text = asyncio.run(self._invoke(self.cwd, client, request, errors))
            with (stage_dir / "output.json").open("x", encoding="utf-8") as output:
                os.chmod(output.name, 0o600)
                output.write(text)
            result = _load_json(text)
            if not isinstance(result, dict):
                raise ModelFailure("ACP final output must be a JSON object")
            jsonschema.Draft202012Validator(schema).validate(result)
            return result
        except ModelFailure:
            raise
        except Exception as exc:
            raise ModelFailure("ACP invocation or structured answer failed; see the stage records") from exc
