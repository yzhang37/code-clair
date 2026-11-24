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

安装管理使用 Python 3.10+ 和 Git，不调用模型。以下命令从本原厂仓库运行：

```sh
python3 setup.py install --repo ../x4-data
```

安装器默认使用本目录的原厂 Git 仓库和 `HEAD`，只交付已提交的版本。`--source` 可明确指定原厂 Git 路径或地址，`--ref` 可指定分支、tag 或 commit；记录中保存实际来源、选择和完整 commit，不以未提交工作树冒充发行版本。

首次安装把原厂 `setup.py`、`README.md`、`engine/`、`initial/` 放入宿主 `.clarity/`。宿主已有 `current/` 时保留原样，否则从同版本 `initial/` 建立。原厂自己的规则与案例不会进入宿主。

安装器生成 `.clarity/Makefile`，其中审核路径指向宿主 `.clarity/engine/`。宿主有效的 `GNUmakefile`、`makefile` 或 `Makefile` 末尾只增加一个标记明确的 include 块，保留原有正文及默认目标；不存在时建立 `Makefile`。显式同名审核目标冲突会拒绝安装，不执行宿主构建来探测变量、eval 或外部 include 生成的目标。

以后在宿主根目录更新：

```sh
python3 .clarity/setup.py update
```

更新沿用登记的原厂来源和版本选择，也可用 `--ref` 改选版本。它更新原厂程序和模板，删除上一版本拥有而新版已移除的文件，保留宿主 `current/`、审核记录和人的回应。本地改动过的原厂文件会使更新拒绝；新路径已被本地文件占用时也不会覆盖。

已启用审核的项目会同步 Git 控制目录中的运行引擎及自己的 hook；配置只同步 `engine_digest`，保留模型、调用上限、规则选择、规则采用哈希和其他字段。安装与更新不重新采用当前规则。首次软件安装没有审核配置时，不自动接入提交 hook。

安装记录在 `clarity/installation.json`，含来源、完整 commit、文件摘要与权限、宿主入口及 hook 记录。Git 下载缓存位于 `clarity/source/`。它们都由实际 Git common dir 定位，不假定宿主 `.git` 必须是目录。目前一个共享控制目录只登记一个工作树；另一工作树不能隐式替换该安装的共享运行版本。

已有旧版 sidecar 首次运行 `install` 时，会逐文件寻找匹配的原厂历史版本，最多核对最近 200 个祖先；不匹配便停止。旧宿主 Makefile 只有与该历史版本的纯审核入口完全一致时才整体迁移。软件更新不调用旧的审核启用安装器，以免重建用户配置。

交付前准备完整版本并检查输入，写入失败会恢复已触及文件；回读一致后才登记成功。安装与代码审核、组合维护共用锁。强制终止或回滚失败留下的锁及原文备份需要核对，不自动按时间删除，也不保证断电时的跨文件原子切换。网络获取失败保留旧部署。

`reset`、自动合并本地程序修改和远程发布尚未实现。

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
