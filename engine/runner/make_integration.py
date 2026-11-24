"""make_integration.py 为宿主保留原 Makefile，并生成固定 .clarity 路径的审核入口。"""

from __future__ import annotations

# 动态安装会按文件路径加载本模块；只补同版 runner 搜索路径，不依赖宿主技术栈。
import sys
from pathlib import Path
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import tr


import re


REVIEW_TARGETS = (
    "review", "review-staged", "review-install", "review-rules",
    "review-rules-quality", "review-fixtures-quality", "review-engine",
    "review-respond", "review-engine-respond",
)
BEGIN_MARKER = b"# Clarity sidecar begin"
END_MARKER = b"# Clarity sidecar end"
INCLUDE_BLOCK = BEGIN_MARKER + b"\ninclude .clarity/Makefile\n" + END_MARKER + b"\n"


def rendered_sidecar_makefile() -> bytes:
    """rendered_sidecar_makefile() 生成从宿主根目录运行的九个审核目标。

    路径固定为宿主中的 .clarity，不把原厂源目录绑定到其他项目。
    回应先由 Make 按字面读取，再通过环境变量交给 Python，避免把人的原话当命令展开。
    """
    return """.PHONY: review review-staged review-install review-rules review-rules-quality review-fixtures-quality review-engine review-respond review-engine-respond

review:
	@python3 .clarity/engine/runner/review.py review --target HEAD

review-staged:
	@python3 .clarity/engine/runner/review.py review --target index

review-rules:
	@python3 .clarity/engine/runner/review.py check-rules

review-rules-quality:
	@python3 .clarity/engine/runner/review.py check-rules-quality

review-fixtures-quality:
	@python3 .clarity/engine/runner/review.py check-fixtures-quality

review-engine:
	@python3 .clarity/engine/runner/review.py maintain

# 回应是原话，必须按字面量传入环境，避免 Make 或 shell 执行其中的金额、命令及换行。
# 命令行变量本会自动导出；禁止再次展开原 RESPONSE，只导出已按字面读取的值。
unexport RESPONSE
review-respond review-engine-respond: export CLARITY_RESPONSE := $(value RESPONSE)

review-respond:
	@python3 .clarity/engine/runner/review.py respond --text-env CLARITY_RESPONSE

review-engine-respond:
	@python3 .clarity/engine/runner/review.py maintain-respond --text-env CLARITY_RESPONSE

review-install:
	@sh .clarity/engine/runner/commit-msg --install
""".encode("utf-8")


def _remove_managed_block(existing: bytes) -> bytes:
    """_remove_managed_block() 只移除唯一且原文未变的管理块，不覆盖标记内的本地修改。"""
    markers: list[tuple[bytes, int, int]] = []
    offset = 0
    for line in existing.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        if BEGIN_MARKER in body or END_MARKER in body:
            if body not in (BEGIN_MARKER, END_MARKER):
                raise ValueError(tr('Clarity Makefile markers must occupy complete lines.'))
            markers.append((body, offset, offset + len(line)))
        offset += len(line)
    if not markers:
        return existing
    if len(markers) != 2 or markers[0][0] != BEGIN_MARKER or markers[1][0] != END_MARKER:
        raise ValueError(tr('Clarity Makefile managed block is missing, duplicated, or out of order.'))
    if existing[markers[0][1]:markers[1][2]] != INCLUDE_BLOCK:
        raise ValueError(tr('Clarity Makefile managed block has local changes; it was not overwritten.'))
    return existing[:markers[0][1]] + existing[markers[1][2]:]


def _explicit_review_targets(content: bytes) -> set[str]:
    """_explicit_review_targets() 找出宿主正文直接声明的保留目标，不执行宿主 Makefile。

    续行先合成逻辑行，配方行和赋值不当作目标；这里只检查字面目标名。
    外部 include、变量展开及 eval 生成的目标须由调用方另行核对，本层不执行它们来猜测结果。
    """
    reserved = {name.encode("ascii") for name in REVIEW_TARGETS}
    conflicts: set[str] = set()
    logical = re.sub(rb"\\\r?\n", b" ", content)
    for line in logical.splitlines():
        if not line or line.startswith(b"\t"):
            continue
        declaration = line.lstrip().split(b"#", 1)[0]
        if b":" not in declaration:
            continue
        targets, tail = declaration.split(b":", 1)
        # 赋值的右侧可能包含冒号；只有目标声明的左侧参与保留名冲突检查。
        if b"=" in targets or tail.startswith((b"=", b":=", b"::=")):
            continue
        for target in targets.split():
            if target in reserved:
                conflicts.add(target.decode("ascii"))
    return conflicts


def host_makefile(existing: bytes | None, legacy: bytes | None = None) -> bytes:
    """host_makefile() 在宿主末尾维护一个 include 块，保留管理块外的原文字节。

    include 放在已有目标之后，使原宿主的默认目标继续生效；没有目标的宿主才可默认 review。
    外来同名目标和坏管理标记会失败，避免覆盖宿主构建行为。
    只有正文与调用方提供的旧版入口完全一致时，才把整份旧入口迁移为 include 块。
    """
    if existing is None or existing == b"":
        return INCLUDE_BLOCK
    if legacy is not None and existing == legacy:
        return INCLUDE_BLOCK
    preserved = _remove_managed_block(existing)
    conflicts = _explicit_review_targets(preserved)
    if conflicts:
        raise ValueError(tr('Host Makefile already defines reserved Clarity targets: ') + ", ".join(sorted(conflicts)))
    # 仅补必需的行边界，不清理用户空行；重复生成不会不断追加分隔行。
    separator = b"" if not preserved or preserved.endswith(b"\n") else b"\n"
    return preserved + separator + INCLUDE_BLOCK
