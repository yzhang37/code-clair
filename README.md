# Clarity：仓库无关的代码审核

这是 Clarity 的原厂源码仓库。根目录直接放 `engine/`、`initial/` 和本仓库自己的 `current/`，不套 `.clarity/`；安装到其他 Git 项目后，才在宿主根目录建立普通的 `.clarity/` 目录。

| 位置 | 职责 | 安装或更新时 |
| --- | --- | --- |
| [engine/](engine/) | 审核模型、唯一 Principles、流程、角色提示词和程序 | 从选定原厂 Git commit 部署 |
| [initial/](initial/) | 未填规则与案例底稿 | 与程序一起更新 |
| [current/](current/) | 本项目自己的规则与固定案例，即 Registry | 不从原厂分发；首次缺失时从 initial 创建，已有内容保留 |
| [setup.py](setup.py) | 软件安装、更新、来源记录及 Make 集成 | 一并部署，供宿主以后自行更新 |
| [Makefile](Makefile) | 原厂离线测试入口 | 不复制到宿主；宿主审核入口由安装器生成 |

原厂和宿主的目录关系如下：

```text
code-clair/                  host-project/
├── .git/                    ├── .git/
├── setup.py                 ├── Makefile
├── README.md                └── .clarity/
├── Makefile                     ├── setup.py
├── engine/                      ├── README.md
├── initial/                     ├── Makefile
└── current/                     ├── engine/
                                ├── initial/
                                └── current/
```

宿主中的 `.clarity/` 可以由宿主 Git 跟踪，不是嵌套 Git 仓库或 submodule；安装器不会自动暂存或提交宿主文件。原厂下载副本、版本记录和审核运行状态位于 Git common dir 的 `clarity/` 中，普通仓库通常是 `.git/clarity/`。

## 安装与更新

进入你要安装的目录，执行：

```sh
curl -fsSL https://raw.githubusercontent.com/yzhang37/code-clair/main/install.sh | sh
```

安装位置始终是当前目录的 `.clarity/`。目录可以没有 Git，也可以是有业务代码、历史、构建文件和未提交修改的成熟 Git 项目，甚至是已有 Git 仓库里的一个子目录。安装器不会跳到上级 Git 根目录，也不会替用户初始化 Git。机器需要可用的 Git、curl、Python 3.10+；Git 程序用于下载版本，不代表目标目录必须已有 Git 仓库。

入口按这个顺序执行：

1. 检测当前目录是否已有符合结构的 `.clarity` 安装。
2. 已有安装时询问“更新（默认）”还是“全新安装”。
3. 选择更新后直接进入升级，保留既有 Git 归属，不再询问跟踪。
4. 全新安装且没有 Git 仓库时，直接安装文件，不管理 Git。
5. 全新安装且属于 Git 仓库时，询问“是否将 `.clarity` 加入 Git 跟踪”，默认 **no**。

“全新安装”是重新安装程序并重新选择跟踪，不是恢复出厂；已有 `current/` 仍保留。只有缺少 `current/` 时才从同版本 `initial/` 创建，原厂自己的规则与案例不分发给宿主。

选择 **no** 时，Git 项目使用本机 `info/exclude` 排除未跟踪的 sidecar，不修改共享 `.gitignore` 或已有构建入口。选择 **yes** 时，安装器将程序、模板、当前资料和生成的 Make 入口加入暂存区，不自动提交；已有业务文件的暂存状态保持，Makefile 也只暂存新增入口，不夹带工作树里的其他改动。已经由 Git 跟踪的旧文件不会因为回答 no 被自动移出 Git。

安装后直接升级：

```sh
./.clarity/update
```

升级只更新程序、模板及已启用的运行快照，保留 `current/`、配置、规则采用哈希和审核状态，也不重新询问 Git 跟踪。没有 Git 的目录同样可以升级。

首次默认从 `https://github.com/yzhang37/code-clair.git` 通过 HTTPS 获取版本。之后沿用已选来源。需要本地同步时，在安装目录执行：

```sh
./.clarity/update --source ../code-clair
```

改回 GitHub：

```sh
./.clarity/update --source https://github.com/yzhang37/code-clair.git
```

维护者也可从目标目录直接运行原厂管理程序：

```sh
python3 ../code-clair/setup.py install --source ../code-clair
```

底层 `setup.py install` 与 `setup.py update` 仍保留，支持 `--repo`、`--source`、`--ref`；显式 `--repo` 只按给定目录安装，不解析成上级工作树根。非交互安装可以用 `--track yes` 或 `--track no` 指明跟踪选择，未提供时采用提示中的默认值。来源只读取已提交的 Git 版本，记录实际完整 commit。

## 安装记录和保护边界

Git 根目录继续沿用 Git common dir 的 `clarity/` 状态，以保留已有审核配置与记录。没有 Git、或安装在仓库子目录时，本机状态在 `.clarity/.local/`；随后由用户建立 Git 时仍沿用该状态。生成的 `.clarity/.gitignore` 排除本机状态和 Python 缓存。下载缓存按来源隔离，切换失败不更改原来源。

记录包括当前及上次来源、实际 commit、部署文件的摘要和权限、Git 跟踪选择和 Make 入口。本地修改过的原厂文件会使升级拒绝；新路径已被本地文件占用时也不会覆盖。克隆已跟踪的 sidecar 而缺少本机记录时，升级先逐文件匹配原厂历史版本，最多核对最近 200 个祖先，并核对该版本生成的 Makefile 和忽略文件，不能仅凭目录名认领。

已存在的 Make 集成在升级时保留。新的独立安装在 `.clarity/Makefile` 提供目标；加入跟踪时才在宿主有效的 Makefile 中集成加载入口。原有正文和默认构建目标保留，外来同名目标及被修改的管理块会拒绝覆盖。

交付前准备完整版本，写入失败恢复原文；加入跟踪时，Git 索引也在同一事务中回读和恢复。安装与既有代码审核、组合维护使用相应排他锁。强制终止或回滚失败留下的锁及备份需要核对，不按时间自动删除；不保证断电时的跨文件原子切换。

软件安装不自动启用提交审核。代码审核仍需要 Git 快照；仅本机模式不会把 `.clarity/current/` 自动送入宿主 Git 快照，本轮未改变审核依据的取材契约。`reset` 和自动合并本地程序修改尚未实现。

## 在宿主运行审核

审核需要可用的独立 Codex CLI。填写宿主自己的正式规则和案例后，使用生成的入口首次启用提交审核：

```sh
make review-install
```

此命令仍是现有审核启用入口：它会按默认参数重建审核配置，不是软件更新命令。已有自定义配置的安装使用上面的 `setup.py update` 更新程序。

宿主日常入口：

```sh
make review
make review-staged
make review-rules-quality
make review-fixtures-quality
make review-rules
make review-engine
```

`review` 审核 HEAD，`review-staged` 审核暂存区，均不创建提交。它们读取所选 Git 快照的完整已跟踪文本，包括 `.clarity` 中的规则、案例、模板、文档和程序；diff 只帮助理解变化。未进入该快照的文件不会自动被读取。

两项 quality 检查共用同一检查器和唯一 Principles，分别检查完整 rules、完整 fixtures 的编写质量；`review-rules` 隐藏 GOLD 独立判断片段后比较固定预期，再核对理由和边界。`review-engine` 组合这三项，只形成工作树材料维护结果，不形成代码审核通过，也不改写正式资料。

Reviewer 报告审核结果，Git hook 决定是否提交。在 SourceTree 中正常暂存、填写说明和提交即可触发。若审核未通过仍要提交，在提交说明另起一行填写 `Review-Bypass: 你的一句话说明`；hook 会把全部已知问题和真实审核状态写入本次 commit message 后放行。人的说明无需 AI 批准，跳过不改变未修复或未完成状态。

代码回应使用 `make review-respond RESPONSE='实际回应原话'`，材料维护回应使用 `make review-engine-respond RESPONSE='实际回应原话'`。回应只保存到各自记录，不自动修改规则、固定预期或生成 learn 候选。

## 材料与审核边界

对象、职责和术语集中维护在 [Engine 模型](engine/README.md)；rules 和完整 fcase 的编写质量统一使用 [Principles 及教学对照](engine/principles.md)。编写或修订材料前阅读它们，按 [workflow](engine/workflow.md) 核对材料质量及规则兼容性。

`current/rules.md` 是可独立用于编写和审查代码的标准；`current/cases/` 用固定正反预期、前提、理由和边界检验规则，不提供额外代码准则。一个 Markdown 文件管理一个案例，同一判断点的补充沿用原文件。模板中的标题和未填项不是正式依据；空模板不会让代码自动通过。

审核先核对依据健康及逐 case 兼容性，再组织独立代码审核、交叉反证和汇总。完整能力、退出码、记录位置及限制见 [runner 运行说明](engine/runner/README.md)。调用故障、引用或覆盖缺口不能标为通过；固定案例通过也不证明未知代码没有误判或漏判。Pi 和自动知识整理尚未接入。

## 原厂验证

在本仓库运行 `make test`。离线测试使用临时原厂和宿主 Git 仓库及假模型，核对现有审核流程、Make 集成和安装更新，不运行目标业务或真实模型审核。原厂 Makefile 不提供宿主 `review` 目标。
