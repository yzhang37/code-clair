"""model.py 选择本地 Agent 的启动配置，所有审核统一使用 ACP Python 客户端。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from model_common import ModelFailure, _INSTRUCTIONS


# Codex 的静态审核设置沿用旧调用约束；只改变通信协议，不给审核增添工具或项目自动上下文。
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
ADAPTER_FILES = ("acp_model.py", "model.py", "model_common.py", "pi_entry.py", "acp_runtime.py")


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

    def run(self, prompt, schema, stage_dir):
        """run() 为现成适配器提供独立上下文，保留认证入口而不记录凭据正文。

        Pi 自己管理模型和认证；Codex 延续原先临时配置目录的静态审核约束。
        两者最后进入同一个 ACP 客户端，启动选项不承担审核语义判断。
        """
        from acp_model import AcpModel

        config = self.config
        environment = {key: value for key, value in os.environ.items()
                       if key in _ENV_KEYS or key.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN"))
                       or key.startswith(("AWS_", "AZURE_", "GOOGLE_", "CLOUDFLARE_"))
                       or key in {"COPILOT_GITHUB_TOKEN", "PI_CODING_AGENT_DIR", "PI_PACKAGE_DIR"}}
        with tempfile.TemporaryDirectory(prefix="clarity-agent-") as temporary:
            root = Path(temporary)
            if config.get("agent") == "pi":
                environment["PI_ACP_PI_COMMAND"] = str(Path(__file__).with_name("pi_entry.py"))
                environment["CLARITY_PI_BINARY"] = config.get("agent_bin", "pi")
            elif config.get("agent") == "codex":
                home = root / "codex-home"
                home.mkdir(mode=0o700)
                source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
                if source.is_file():
                    (home / "auth.json").symlink_to(source.resolve())
                environment["CODEX_HOME"] = str(home)
                environment["CODEX_PATH"] = config.get("codex_bin") or config.get("agent_bin", "codex")
                instructions = root / "instructions.txt"
                instructions.write_text(_INSTRUCTIONS)
                overrides = {"sandbox_mode": "read-only", "model_instructions_file": str(instructions)}
                for setting in _CONFIG:
                    name, value = setting.split("=", 1)
                    target = overrides
                    parts = name.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = json.loads(value)
                environment["CODEX_CONFIG"] = json.dumps(overrides)
                environment["INITIAL_AGENT_MODE"] = "read-only"
            return AcpModel(config["agent_command"], config.get("model"), config.get("timeout", 300),
                            environment=environment).run(prompt, schema, stage_dir)


def create_model(config):
    """create_model() 为代码审核、材料质量和固定回归创建同一种 ACP 调用器。"""
    return ModelClient(agent_settings(config))
