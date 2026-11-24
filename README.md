# 仓库无关的代码审核

本目录按资料的状态和维护责任分为三部分。目标仓库的语言、业务架构和部署主机不决定审核器的实现语言，也不要求为了检查代码而运行业务程序。

```text
.clarity/
├── README.md
├── current/
│   ├── rules.md
│   └── cases/
├── initial/
│   ├── rules.md
│   └── cases/
│       └── case.md
└── engine/
    ├── README.md
    ├── principles.md
    ├── workflow.md
    ├── prompts/
    └── runner/
```

| 位置 | 职责 | 何时变化 |
| --- | --- | --- |
| [current/](current/) | `rules.md` 的当前标准及 `cases/` 的固定正反预期 | 规则或案例变化时核对兼容性；按判据的实际需要修订规则 |
| [initial/](initial/) | 同结构的未填规则与案例底稿，没有正式依据或历史资料 | 只在初始文档格式调整时修改，实际材料不回写 |
| [engine/](engine/) | 审核模型、Principles、共享流程、角色指令与运行程序 | 检查机制升级时修改，日常添加案例不改程序 |
| 本文件 | 目录职责及使用入口 | 结构或用法变化时更新，不维护案例清单或规则副本 |

审核对象、职责关系和术语集中维护在 [engine 入口](engine/README.md)；rules 和完整 fcase 的编写质量统一使用 [Principles](engine/principles.md)，正文和教学对照只在那里维护。

## 维护当前依据

[rules.md](current/rules.md) 是可直接用于 AI 辅助 skills、编码 prompts 和代码审查的自包含标准；[cases/](current/cases/) 用固定正反预期及其前提、理由和边界约束规则，一个 Markdown 文件管理一个案例。数字编号稳定，同一判断点的后续补充沿用原文件，不重复建例。

编写或修订材料前阅读同一份 [Principles 及教学对照](engine/principles.md)，依 [workflow](engine/workflow.md) 核对材料质量及规则兼容性。对象与检验方向由 engine 模型统一说明；本入口不再复制编写要求或案例索引。

## 使用初始状态

新仓库可以复制 `initial/` 的资料布局为自己的 `current/`，再填写规则、按主题命名单案例文件。底稿的标题和未填项不是正式依据；空模板不会让代码自动通过。当前仓库已经填入的资料不会自动进入其他仓库。

检查器及模板更新不覆盖所有者的当前资料。审核模型没有自动改写规则、fixtures 或底稿的职责。

## 运行审核

在仓库根目录使用现有 [Makefile](../Makefile)：

```sh
make review
make review-staged
```

工作树材料可独立检查或组合维护：

```sh
make review-rules-quality
make review-fixtures-quality
make review-rules
make review-engine
```

前两项共用同一检查器和 Principles，分别检查完整 rules、完整 fixtures 的编写质量；`review-rules` 保留隐藏 GOLD 的固定案例回归。`review-engine` 依次组合这三项，只形成材料维护结果。这些入口直接使用源工作树，不要求先暂存或与安装快照版本相符，不形成代码审核通过，也不改写正式资料。

`make review` 审核 HEAD；`make review-staged` 审核暂存区。两者读取所选 Git 快照的完整已跟踪文本，包括 `.clarity` 自身的规则、案例、模板、文档和审核程序；diff 只辅助理解变化，不限制审核范围。未进入该快照的文件不会自动被读取。两者不创建提交。

Reviewer 报告审核结果，Git hook 决定是否提交。[commit-msg](engine/runner/commit-msg) 实现了[共同工作流](engine/workflow.md#审核结果与提交决定)中的本次跳过处理；跳过逻辑由 hook 负责。

运行一次 `make review-install` 接入当前仓库。之后在 SourceTree 中正常暂存文件、填写提交说明、点击提交；审核未通过时，hook 输出会显示在 SourceTree 的提交错误结果中。若仍要提交，在提交说明另起一行填写 `Review-Bypass: 你的说明`，再提交即可；全部已知问题和真实审核状态会写入本次 commit message。人的说明无需 AI 批准，问题新旧不限，跳过不改变未修复或未完成状态。

代码回应用 `make review-respond RESPONSE='实际回应原话'`，材料维护回应用 `make review-engine-respond RESPONSE='实际回应原话'`。回应只保存到各自记录，不自动修改规则、固定预期或生成 learn 候选。

完整能力表、命令参数、记录位置、退出码及源树与安装快照的区别统一见 [运行说明](engine/runner/README.md)，共同流程见 [workflow.md](engine/workflow.md)。修改源引擎后，已有安装须用 `make review-install` 刷新；普通代码审核仍要求源引擎与安装配置版本相符，hook 使用安装快照。当前使用独立 Codex CLI，Pi 尚未接入。

审核先核对依据健康及逐 case 的规则兼容性，再组织独立代码审核、交叉反证和汇总。代码阶段只把 rules 作为准则，接收同快照的验收状态；完整 fixtures 记录单独保存。cases 仍属于完整待审文件，不因此成为规则。带预期的兼容核对与独立 `review-rules` 都检验规则，不提供额外代码判据，也不证明真实代码没有漏审。调用故障、覆盖或引用缺口不能标为审核通过。

运行输出、问题和人的回应保存在 Git 控制目录的 `clarity/` 中，普通仓库通常是 `.git/clarity/`，与上述输入分开。语义审核仍有局限；实际行为和未实现边界以运行说明为准。
