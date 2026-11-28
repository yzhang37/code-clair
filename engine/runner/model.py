"""model.py 选择本地 Agent 的启动配置，所有审核统一使用 ACP Python 客户端。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from model_common import ModelFailure, _INSTRUCTIONS


# 代码阶段通过只读 MCP 取得冻结事实；关闭原生命令和环境指令，避免混入实时工作树或其他规则。
# 封闭材料阶段仍沿用无工具输入；只读上下文不会授权执行项目程序、修改文件或增加判断标准。
_CONFIG = (
    'approval_policy="never"',
    "project_doc_max_bytes=0",
    "project_doc_fallback_filenames=[]",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    'web_search="disabled"',
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.hooks=false",
    "features.memories=false",
    "features.goals=false",
    "features.computer_use=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.browser_use_full_cdp_access=false",
    "features.in_app_browser=false",
    "features.image_generation=false",
    "features.workspace_dependencies=false",
    "features.skill_mcp_dependency_install=false",
    "features.tool_suggest=false",
    "features.shell_snapshot=false",
    'history.persistence="none"',
)


_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "OPENAI_API_KEY", "CODEX_API_KEY",
)


ADAPTERS = {"pi": ("pi-acp", "pi-acp@0.0.35"),
            "codex": ("codex-acp", "@agentclientprotocol/codex-acp@2.12.0")}
MODEL_FIELDS = ("agent", "agent_command", "agent_bin", "codex_bin", "model", "timeout")
ADAPTER_FILES = ("acp_model.py", "model.py", "model_common.py", "pi_entry.py", "acp_runtime.py",
                 "review_context.py", "context_server.py", "pi_context_guard.mjs")


def _restore_view_permissions(root):
    """_restore_view_permissions() 在 Agent 结束后恢复临时视图权限，使只读材料可以被回收。

    只处理本次视图中的普通路径，不跟随链接，避免清理时更改视图外文件或认证资料的权限。
    """
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)


def agent_settings(config, *, from_environment=False):
    """agent_settings() 固定实际启动选择；旧 codex_bin 配置仍指向本地 Codex，但改走 ACP。

    新用户优先使用已安装的 Pi，其后是 Codex；明确选择始终优先，不静默切换其他 Agent。
    自定义 argv 直接连接已有 ACP 服务，后续 Agent 不要求在此增加原生输出解析器。
    """
    selected = dict(config)
    requested = os.environ.get("CLARITY_AGENT") if from_environment else None
    command = os.environ.get("CLARITY_AGENT_COMMAND") if from_environment else None
    if requested and requested != selected.get("agent"):
        selected.pop("agent_command", None)
        selected.pop("agent_bin", None)
    agent = requested or selected.get("agent") or ("custom" if command or selected.get("agent_command") else
             "codex" if selected.get("codex_bin") else
             "pi" if shutil.which("pi") else "codex" if shutil.which("codex") else None)
    if command:
        selected["agent_command"] = json.loads(command)
        agent = agent or "custom"
    if agent is None:
        raise ModelFailure("No local review agent was found. Install and authenticate Pi or Codex, "
                           "or select an existing ACP agent with CLARITY_AGENT_COMMAND.")
    if agent not in {*ADAPTERS, "custom"}:
        raise ModelFailure("Unknown review agent; use pi, codex, or custom with an ACP command.")
    selected["agent"] = agent
    argv = selected.get("agent_command")
    if argv is not None:
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(item, str) or not item or "\0" in item for item in argv)):
            raise ModelFailure("ACP agent command must be a nonempty JSON array of arguments.")
        executable = shutil.which(argv[0])
        if not executable:
            raise ModelFailure("ACP agent executable was not found: " + argv[0])
        selected["agent_command"] = [executable, *argv[1:]]
        return selected
    if agent == "custom":
        raise ModelFailure("A custom ACP agent requires agent_command as an argument array.")
    binary = selected.get("agent_bin")
    if agent == "codex":
        binary = selected.get("codex_bin") or binary
    binary = shutil.which(binary or agent)
    if not binary:
        raise ModelFailure("The selected local review agent was not found: " + agent)
    # 保留启动链接的名称；部分用户安装器按 argv[0] 区分入口，不能随意改为链接最终目标。
    selected["agent_bin"] = str(Path(binary).absolute())
    executable, package = ADAPTERS[agent]
    adapter = shutil.which(executable)
    if adapter:
        selected["agent_command"] = [adapter]
    else:
        npx = shutil.which("npx")
        if not npx:
            raise ModelFailure("The ACP adapter was not found: " + executable + "; install it or provide npx.")
        selected["agent_command"] = [npx, "--yes", package]
    return selected


def model_config(config):
    """model_config() 为各材料检查交付同一份模型设置投影，避免父子输入标识遗漏 Agent 选择。"""
    return {key: config.get(key) for key in MODEL_FIELDS}


class ModelClient:
    """ModelClient 只准备 Agent 启动环境，通信和结果解析全部委派给同一个 AcpModel。"""

    def __init__(self, config):
        """__init__() 保存审核已冻结的配置，不在每个阶段重新选择另一个模型或 Agent。"""
        self.config = dict(config)

    def run(self, prompt, schema, stage_dir, *, context=None, reply_feedback=None):
        """run() 按本阶段材料准备原生 Agent 环境，通信和回答核验仍交给同一个 ACP 客户端。

        没有 context 的材料检查保持封闭输入；代码阶段把调用方已冻结的内容投影成只读视图。
        Pi 使用受路径守卫约束的原生读取工具，Codex 使用只读 MCP 工具；其他 Agent 保持封闭输入。
        独立会话及自动项目指令的隔离保持；启动配置不改变正式规则、GOLD 或完成标准。
        调用方可要求一次同会话补读或重交格式，共用原超时，避免仅因回包不合格重启整个审核。
        封闭材料阶段的反馈只允许重交结果，不授予上下文工具或额外资料访问。
        """
        config = self.config
        if context is not None and config.get("agent") not in {"pi", "codex"}:
            raise ModelFailure("Context-enabled review is available only for Pi and Codex; the stage is incomplete.")
        if reply_feedback is not None and not callable(reply_feedback):
            raise ModelFailure("Reply feedback must be callable or null.")
        from acp_model import AcpModel

        environment = {key: value for key, value in os.environ.items()
                       if key in _ENV_KEYS or key.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN"))
                       or key.startswith(("AWS_", "AZURE_", "GOOGLE_", "CLOUDFLARE_"))
                       or key in {"COPILOT_GITHUB_TOKEN", "PI_CODING_AGENT_DIR", "PI_PACKAGE_DIR"}}
        with tempfile.TemporaryDirectory(prefix="clarity-agent-") as temporary:
            root = Path(temporary)
            view = None
            instructions = _INSTRUCTIONS
            servers = None
            try:
                if context is not None:
                    from model_common import _CONTEXT_INSTRUCTIONS
                    from review_context import materialize_context

                    view = root / "view"
                    view.mkdir()
                    materialize_context(context, view)
                    instructions = _CONTEXT_INSTRUCTIONS
                    environment["CLARITY_CONTEXT_ROOT"] = str(view)
                    if config.get("agent") == "codex":
                        servers = [{"name": "clarity", "command": sys.executable,
                                    "args": [str(Path(__file__).with_name("context_server.py")),
                                             "--root", str(view), "--log",
                                             str(Path(stage_dir).resolve() / "context-access.jsonl")],
                                    "env": []}]
                if config.get("agent") == "pi":
                    environment["PI_ACP_PI_COMMAND"] = str(Path(__file__).with_name("pi_entry.py"))
                    environment["CLARITY_PI_BINARY"] = config.get("agent_bin", "pi")
                    if view is not None:
                        environment["CLARITY_CONTEXT_PYTHON"] = sys.executable
                        environment["CLARITY_CONTEXT_LOG"] = str(Path(stage_dir).resolve() / "context-access.jsonl")
                elif config.get("agent") == "codex":
                    home = root / "codex-home"
                    home.mkdir(mode=0o700)
                    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
                    if source.is_file():
                        (home / "auth.json").symlink_to(source.resolve())
                    environment["CODEX_HOME"] = str(home)
                    environment["CODEX_PATH"] = config.get("codex_bin") or config.get("agent_bin", "codex")
                    instruction_file = root / "instructions.txt"
                    instruction_file.write_text(instructions)
                    overrides = {"sandbox_mode": "read-only", "model_instructions_file": str(instruction_file)}
                    for setting in _CONFIG:
                        name, value = setting.split("=", 1)
                        target = overrides
                        parts = name.split(".")
                        for part in parts[:-1]:
                            target = target.setdefault(part, {})
                        target[parts[-1]] = json.loads(value)
                    environment["CODEX_CONFIG"] = json.dumps(overrides)
                    environment["INITIAL_AGENT_MODE"] = "read-only"
                # 已核对的 Codex 入口按一次请求的字符总量限制；不把该数值套到其他 Agent 或 token 窗口。
                # Pi 适配器声明的启动通知独立分类，避免闭合材料回合把它串入答案；其他文字仍须严格核验。
                return AcpModel(config["agent_command"], config.get("model"), config.get("timeout", 300),
                                environment=environment, cwd=view, mcp_servers=servers,
                                allow_tools=view is not None, instructions=instructions,
                                reply_feedback=reply_feedback,
                                startup_metadata_path=("piAcp", "startupInfo") if config.get("agent") == "pi" else None,
                                max_request_chars=1_048_576 if config.get("agent") == "codex" else None).run(
                                    prompt, schema, stage_dir)
            finally:
                if view is not None:
                    _restore_view_permissions(view)


def create_model(config):
    """create_model() 为代码审核、材料质量和固定回归创建同一种 ACP 调用器。"""
    return ModelClient(agent_settings(config))
