import { execFile } from "node:child_process";
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);
const UNICODE_SPACES = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/g;
const READ_FILE = "clarity_read_file";
const executeFile = promisify(execFile);
const TRUSTED_RUNNER = fileURLToPath(new URL("./", import.meta.url));
const READ_CODE = `import json, sys
sys.path.insert(0, sys.argv[1])
from review_context import ContextStore
try:
    result = ContextStore(sys.argv[2], sys.argv[3] or None).read_file(**json.loads(sys.argv[4]))
except (ValueError, TypeError, OSError, UnicodeError) as error:
    result = {"error": str(error)}
print(json.dumps(result, ensure_ascii=False, allow_nan=False))
`;

/** resolveInputPath() 解析路径别名而不替换文件名，使守卫检查保留冻结路径的身份。 */
function resolveInputPath(value, cwd) {
    let path = value;
    if (path.startsWith("@")) path = path.slice(1);
    if (path === "~") path = homedir();
    else if (path.startsWith("~/") || (process.platform === "win32" && path.startsWith("~\\"))) {
        path = join(homedir(), path.slice(2));
    }
    if (path.startsWith("file://")) path = fileURLToPath(path);
    return resolve(cwd, path);
}

/**
 * contextGuard() 在 Pi 原生工具执行前限定只读工具及冻结视图，保留原生的查阅和上下文循环。
 * 视图由调用方投影，只有选定的普通文件且不可由 Agent 写入；真实路径检查拒绝目录跳转和外部链接。
 * 原生工具无法交付的长行由同一个 ContextStore 有界续读，不改原文，也不开放项目程序执行。
 * 这是文件工具的访问边界，不声称约束任意扩展、恶意本机程序或其他进程的并发修改。
 */
export default function contextGuard(pi) {
    const configured = process.env.CLARITY_CONTEXT_ROOT;
    if (!configured) throw new Error("Clarity frozen context root is required");
    const root = realpathSync(configured);
    const python = process.env.CLARITY_CONTEXT_PYTHON;
    if (!python || !isAbsolute(python)) throw new Error("Clarity managed Python is required");
    const audit = process.env.CLARITY_CONTEXT_LOG ?? "";

    pi.registerTool({
        name: READ_FILE,
        label: "Read frozen original text",
        description: "Read bounded original frozen text when native read truncates a long line or normalizes a filename. " +
            "Use the exact original repository-relative path from context.json and explicit current/baseline version; " +
            "never use an absolute path or current/ or baseline/ view prefix. Lines and Unicode-character columns are " +
            "one-based. Text retains original newlines. Continue with next.start_line and next.start_column until next " +
            "is null when complete text is needed. Cite original path/line/text, not pagination metadata.",
        parameters: {
            type: "object", required: ["path"], additionalProperties: false,
            properties: {
                path: { type: "string", minLength: 1, maxLength: 4096 },
                version: { type: "string", enum: ["current", "baseline"] },
                start_line: { type: "integer", minimum: 1 },
                line_count: { type: "integer", minimum: 1, maximum: 200 },
                start_column: { type: "integer", minimum: 1 },
                char_limit: { type: "integer", minimum: 1, maximum: 16000 },
            },
        },
        /** execute() 只运行固定隔离 Python 和可信模块，模型参数仅作为 JSON 数据交给原文校验器。 */
        async execute(_id, params, signal) {
            const { stdout } = await executeFile(python,
                ["-I", "-S", "-B", "-c", READ_CODE, TRUSTED_RUNNER, root, audit, JSON.stringify(params)],
                { signal, timeout: 10000, maxBuffer: 256 * 1024, encoding: "utf8", shell: false });
            const result = JSON.parse(stdout);
            if (Object.hasOwn(result, "error")) throw new Error(result.error);
            return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
        },
    });

    /** guardCall() 先规范化并检查目标，再把相同绝对路径交给原生工具，越界请求只返回工具错误。 */
    pi.on("tool_call", async function guardCall(event, ctx) {
        // 续读工具接受原清单路径和版本，由 ContextStore 映射；不能套用原生工具的投影路径重写。
        if (event.toolName === READ_FILE) return;
        if (!READ_ONLY_TOOLS.has(event.toolName)) {
            return { block: true, reason: "Only read-only frozen snapshot tools are enabled" };
        }
        const raw = event.input.path ?? ".";
        if (typeof raw !== "string" || !raw) {
            return { block: true, reason: "Frozen snapshot path must be a nonempty string" };
        }
        // Pi 会把这些字符改成普通空格；必须在定位前拒绝，避免先找到另一个同名近似文件。
        if (raw.replace(UNICODE_SPACES, " ") !== raw) {
            return { block: true, reason: "Path cannot be read without changing its snapshot identity" };
        }
        let target;
        try {
            target = realpathSync(resolveInputPath(raw, ctx.cwd));
        } catch {
            return { block: true, reason: "Path is unavailable in the frozen snapshot" };
        }
        const tail = relative(root, target);
        if (tail === ".." || tail.startsWith(`..${sep}`) || isAbsolute(tail)) {
            return { block: true, reason: "Path is outside the frozen snapshot" };
        }
        // Pi 会再次规范化路径；不能让规范化后的另一个文件替代刚核对的固定原文。
        if (target.replace(UNICODE_SPACES, " ") !== target) {
            return { block: true, reason: "Path cannot be read without changing its snapshot identity" };
        }
        event.input.path = target;
    });
}
