#!/usr/bin/env python3
"""pi_entry.py 为现成 pi-acp 选择封闭材料或受限只读取材，不翻译 Pi 协议。"""

import os
from pathlib import Path
import sys

from model_common import _INSTRUCTIONS


def main():
    """main() 只补审核所需隔离选项；模型与认证继续由本地 Pi 处理。

    pi-acp 只接受可执行文件路径，因此通过这个薄入口补参数；ACP/RPC 翻译仍用现成包。
    有冻结视图时启用四个原生读取工具和有界续读工具；显式扩展守卫不发现项目扩展或额外准则。
    无视图的材料检查保持无工具模式；显式空追加提示防止自动读入 APPEND_SYSTEM.md。
    """
    binary = os.environ.get("CLARITY_PI_BINARY", "pi")
    instructions = _INSTRUCTIONS
    tools = ["--no-tools"]
    if os.environ.get("CLARITY_CONTEXT_ROOT"):
        from model_common import _CONTEXT_INSTRUCTIONS

        instructions = _CONTEXT_INSTRUCTIONS
        tools = ["--tools", "read,grep,find,ls,clarity_read_file", "--extension",
                 str(Path(__file__).with_name("pi_context_guard.mjs"))]
    args = [binary, *sys.argv[1:], *tools, "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-context-files", "--no-approve", "--no-session",
            "--system-prompt", instructions, "--append-system-prompt", "", "--offline"]
    os.execvpe(binary, args, os.environ)


if __name__ == "__main__":
    main()
