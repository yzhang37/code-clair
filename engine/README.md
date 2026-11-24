# Engine 的审核模型

## 判定前提

下述关系式是概念伪代码，用于说明检查对象、依据和验收方向，不是现有函数或命令。

`VRAI` / `FAUX` 表示一次已完成判断的结果。必要材料不足、无法判断、调用失败、未运行或漏检都属于“未完成”，不能转换成普通 `FAUX`，也不能从汇总中丢掉后宣称通过。没有样例时应报告没有覆盖，不能仅凭空集合的逻辑结果宣称 rules 已通过 fixtures 验收。

## 对象与术语

1. **`principle`**：一份通用的编写质量原则，同时用于检查 rules 和 fcase。只维护一份正本，不拆成两版，不复制维护，也不另建逐条适用对象登记表。
2. **`rule` / `rules`**：一条具体标准 / 标准集合，用于指导用户代码的编写和审查。rules 应自成一体，使用时不读取外部 fixtures 补标准；正文可以内嵌有助理解标准的正反例。
3. **`fixture_cases` 与 `fcase`**：`fixture_cases` 是固定测试材料集合，其中每个 `fcase` 是一个完整材料包，包含必要前提、正反样例、固定预期、理由和边界。伪代码用 `fcase.cases` 表示其中的待判样例集合，不遍历前提或答案等字段。
4. **`case`**：一个 fcase 中的单个待判样例，携带判断所需的前提。它可以是正例或反例；反例被正确拒绝，同样可以使被测 rules 通过对应测试。
5. **`GOLD_ANSWER(case)`**：取出事先确定的预期布尔判断，不根据当前 rules 的输出重新决定答案。固定预期的理由、前提和边界另保存在 fixture 中，并须核对。一次验收期间这些材料保持固定；独立维护可以纠正材料，但不能为了迁就当前 rules 而改预期。
6. **`vérifier(标准, 材料)`**：按给定标准执行一次判断。用 principle 判断 rules 或 fcase 时，检查的是材料的编写质量；用 rules 判断用户代码时，检查用户代码；用 rules 判断固定 case 时，取得样例判定，供外层与 GOLD 比较，整体验收对象是 rules。
7. **`tout(...)`**：汇总一组布尔结果是否全部成立。规则回归中汇总的是“是否匹配 GOLD”，不是要求所有样例都被接受。`FAUX == FAUX` 也是匹配；未完成不属于可直接汇总的布尔结果。
8. **`for ... in ...`**：从集合中逐项取出元素。先取 `fcase in fixture_cases`，再取 `case in fcase.cases`。每个 case 的必要前提必须随判定材料一起提供；在 rules 独立判断 case 时，GOLD 留在比较侧，不提前给判定者。principle 检查完整 fcase 时，GOLD、理由和边界都是待检查材料，须一并提供。

现有 workflow 以“一个 case”或“案例文件”描述完整材料包时，对应模型中的 **fcase**。runner 终端的 **case** 也指完整 fixture 文件；终端的 **subcase** 指独立待判片段，对应模型中的 **case**。这是名称对应，不表示旧文或程序已经更名，也不应把所有“case”一词机械替换成同一对象。

## 工作与关系

| 工作 | 使用什么 | 关系式 |
| --- | --- | --- |
| 指导编写、审查用户代码 | rules、用户代码及必要事实 | `vérifier(rules, 用户代码) = VRAI/FAUX` |
| 检查 rules 的编写质量 | 同一份 principle、rules 正文及内嵌示例 | `vérifier(principle, rules) = VRAI/FAUX` |
| 检查 fixtures 的编写质量 | 同一份 principle、各 fcase 的完整材料 | `tout(vérifier(principle, fcase) == VRAI for fcase in fixture_cases)` |
| 用固定 fixtures 检验 rules 是否漂移 | 被测 rules、固定样例及其前提、GOLD、理由和边界 | `tout(vérifier(rules, case) == GOLD_ANSWER(case) for fcase in fixture_cases for case in fcase.cases)` |
| 组织审核、维护规则与 fixtures | engine 流程 | 组织上述检查，保存结果与裁决，不增加另一套判断标准 |

第一行的关系式表达审查；辅助写作使用同一份 rules 作为写作依据，不表示该判定函数负责生成代码。

这些工作对应的现有入口、组合方式及实现边界统一见 [runner 的能力表](runner/README.md#已确认要求与当前实现)。

检查 fcase 的编写质量，不是要求其中所有样例都符合 rules。用固定 fixtures 验收时，被测的是 **rules**：正例应被接受，反例应被拒绝，两者都应匹配各自的 GOLD。不能反向要求 fixtures 迁就 rules，也不能把外部 fixtures 作为审查用户代码的第二套准则。

漂移关系式只表达布尔结果比较。只有判定匹配且既定理由、前提和边界的核对也通过，才能报告该项验收通过。结果仅对应本次已检查的固定材料和范围，不能据此证明所有未知情境都无缺口。

## 图形关系

```text
principle ──检查编写质量──▶ rules
principle ──检查编写质量──▶ fixtures

rules ──指导编写与审查──▶ 实际代码

fixtures + GOLD ──检验是否漂移──▶ rules
```

两个 principle 箭头引用同一份原则。箭头表示用途，不表示谁能覆盖事实；rules 约束代码质量，不能把规则期望冒充实际代码已有的行为或保证。

## 文档位置与当前实现

| 位置 | 维护内容 |
| --- | --- |
| 本文件 | 审核对象、职责关系、图和术语的正文，不在其他文件复制维护 |
| [principles.md](principles.md) | 同时检查 rules 和完整 fcase 的唯一 Principles 正文，含已采用的教学对照及理由 |
| [workflow.md](workflow.md) | 执行顺序、权限、输入与结果处理；材料编写质量使用同一份 Principles |
| [sop.md](sop.md) | 外层操作者使用的 SOP 与可复制 prompt |
| [prompts/](prompts/) | 各角色的执行指令 |
| [runner/README.md](runner/README.md) | 实际命令、运行方式、已核实能力和限制 |

Principles 正文及教学对照集中在 principles.md；workflow、角色提示与入口只说明如何使用，不另存一版编写原则。若现行材料之间有实质冲突，应报告具体位置及冲突，不能凭文件名、入口位置或新旧顺序自行覆盖，也不能再复制一份来回避冲突。

安装器将本文件与 principles.md 一并复制、绑定到引擎版本。普通代码审核的健康阶段显式接收两份正文，用同一份 Principles 分别检查正式和候选 rules、完整 fcase 的编写质量，并核对规则冲突与效力。代码首审、交叉反证和汇总接收本页模型以理解职责；代码问题仍只引用正式 rules，不把模型或 Principles 当作产品准则。

工作树 rules 和完整 fixtures 已有独立编写质量检查，复用同一个 `check_materials` 和本目录唯一 Principles；fixtures 检查不加载产品 rules。固定案例回归先隐藏 GOLD 独立判断片段，再比较预期、核对理由和边界，判别输入不加入 Principles。材料维护入口组合这两项质量检查与固定回归，保存各阶段结果及人的回应，不自动改写正式资料或整理 learn 候选。普通代码审核的带预期 fixtures 兼容核对仍单独留账；上述材料维护不形成 HEAD 或暂存区的代码审核结论。实际命令与记录边界由 runner 说明维护。

源文件修改不会自动更新已有安装快照。独立材料检查和组合维护使用宿主源工作树，不要求安装版本匹配；普通代码审核与 hook 保留各自的版本核对边界。原厂提交新版后，宿主通过外层 `setup.py update` 同步程序与已有运行快照，保留配置和资料采用记录；审核首次启用及具体加载行为由 runner 说明维护。文档存在不代表某次语义审核已执行或通过。
