#!/usr/bin/env python3
"""pi_entry.py 为现成 pi-acp 设置静态审核启动参数，不翻译 Pi 协议。"""

import os
import sys

from model_common import _INSTRUCTIONS


def main():
    """main() 只补审核所需隔离选项；模型与认证继续由本地 Pi 处理。

    pi-acp 只接受可执行文件路径，因此通过这个薄入口补参数；ACP/RPC 翻译仍用现成包。
    显式空追加提示防止 Pi 自动读入 APPEND_SYSTEM.md，无工具模式不允许审核执行项目代码。
    """
    binary = os.environ.get("CLARITY_PI_BINARY", "pi")
    args = [binary, *sys.argv[1:], "--no-tools", "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-context-files", "--no-approve", "--no-session",
            "--system-prompt", _INSTRUCTIONS, "--append-system-prompt", "", "--offline"]
    os.execvpe(binary, args, os.environ)


if __name__ == "__main__":
    main()
