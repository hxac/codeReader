# 贡献流程与代码规范

## 1. 本讲目标

本讲是专家层「扩展实践与贡献」单元的第二篇，承接 [u9-l3 单元测试体系](u9-l3-unit-testing.md)（你已经知道如何跑测试），回答下一个问题：**当你把改动写好、测试也跑通了，要怎样把它合规地交回 asc-tools 社区？**

学完本讲，你应当能够：

- 说出 asc-tools 贡献代码的完整链路：从 Issue 讨论、本地编码、pre-commit 钩子、PR 模板到 CI 校验。
- 理解 `.pre-commit-config.yaml` 配置的五道检查关卡（基础格式、C++ clang-format、Python ruff、拼写、OAT 合规）分别在拦什么。
- 看懂 `scripts/oat_check.sh` 的「增量扫描 + 阻断提交」机制，以及 `OAT.xml` 如何声明许可证/版权/文件类型策略。
- 理解 asc-tools 不能孤立升级，它的版本必须与 runtime、ge-executor、metadef、asc-devkit 等 CANN 配套仓的主次版本号对齐。

## 2. 前置知识

在进入贡献流程前，先建立几个基础概念。

- **贡献（Contribute）**：把你的修改通过「拉取请求（Pull Request，PR）」提交回开源仓库，由维护者评审合入。asc-tools 寄居在 [gitcode.com/cann](https://gitcode.com/cann) 社区下。
- **CLA（Contributor License Agreement）**：贡献者协议。首次贡献需先签署，声明你贡献代码的知识产权归属与授权方式，这是合入的前提。
- **pre-commit**：一个 Git 钩子管理框架。它在你执行 `git commit` 时、写入提交对象之前，自动运行一组你预先配置好的检查脚本；任一脚本报错都会**阻断本次提交**。它的配置文件 `.pre-commit-config.yaml` 声明「跑哪些检查」，框架负责「在正确时机跑」。
- **OAT（Open Source Audit Tool）**：开源合规审计工具，华为内部用于扫描代码仓的许可证（License）、版权头（Copyright）、文件类型是否合规。本仓用其 Python 版本 `oat-py`。
- **冒烟测试（Smoke Test）**：提 PR 前快速验证核心链路是否跑通的轻量测试，对应仓内的 `run_presmoke.sh`。
- **版本协同**：asc-tools 是 CANN 大家族的一员，它的运行依赖 runtime（运行时）、ge-executor（图执行器）、metadef（图元定义）、asc-devkit（开发套件）等兄弟仓。这些仓必须**主次版本号一致**才能配合工作。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `CONTRIBUTING.md` | 贡献指南：四种贡献场景与 Issue/PR 流程 |
| `.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md` | PR 描述模板：描述/关联Issue/测试/文档/类型标签 |
| `.pre-commit-config.yaml` | pre-commit 钩子配置：五道检查关卡的来源与参数 |
| `.clang-format` | C++ 代码格式规则（clang-format 用） |
| `OAT.xml` | OAT 合规策略：许可证/版权/文件类型策略与豁免清单 |
| `scripts/oat_check.sh` | OAT 增量合规扫描脚本（pre-commit 调用） |
| `scripts/run_presmoke.sh` | 提交前冒烟测试脚本（跑两个样例验证） |
| `version.cmake` | 版本号与配套仓依赖声明（版本协同的核心） |
| `classify_rule.yaml` | 代码分类与责任人规则（开源/闭源划分、UT/ST 开关） |

---

## 4. 核心概念与源码讲解

### 4.1 贡献流程全景：从 Issue 到 PR

#### 4.1.1 概念说明

贡献不是「写完代码直接 push」，而是一条有门禁的流水线。asc-tools 把这条流水线分为三段：

1. **方案先行**：非简单 Bug 的改动（新增特性/接口/配置、改流程）必须先开 Issue 讨论方案，避免写了被拒。
2. **本地自检**：编码完成后，靠 pre-commit 钩子做格式与合规检查，靠冒烟脚本验证功能，确保提交是「干净且可用」的。
3. **评审合入**：按 PR 模板填写背景与测试说明，打上类型标签，等待维护者评审。

这条链路的设计哲学是**把问题前移**——在本地和 PR 阶段就拦住格式错误、合规违规、功能回归，而不是等 CI 跑完才发现。这与本手册反复强调的 asc-tools 核心价值（孪生调试把问题上板前前移）一脉相承。

#### 4.1.2 核心流程

```text
[Issue 讨论方案] ──若非简单 Bug 则必须──▶ [本地编码]
                                              │
                                              ▼
                        ┌────────────────────────────────────┐
                        │  git commit 触发 pre-commit 钩子     │
                        │  ① 基础格式 ② clang-format ③ ruff     │
                        │  ④ codespell ⑤ OAT 合规（任一失败阻断）│
                        └────────────────────────────────────┘
                                              │ 全部通过
                                              ▼
                        [可选] bash scripts/run_presmoke.sh 冒烟
                                              │
                                              ▼
              [按 PR 模板填写 → 推送分支 → 创建 PR → 维护者评审 → 合入]
```

#### 4.1.3 源码精读

贡献指南的总入口是 [CONTRIBUTING.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CONTRIBUTING.md#L1-L37)。它开篇就指向社区总入口与 CLA：

- [CONTRIBUTING.md:L3](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CONTRIBUTING.md#L3)：要求先到 `cann/community` 了解行为准则、**签署 CLA**、了解贡献流程。没有 CLA，PR 无法合入。
- [CONTRIBUTING.md:L7-L8](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CONTRIBUTING.md#L7-L8)：两条硬性前置——PR 要按模板填写；**非简单 Bug 必须先开 Issue 讨论方案**。这是「方案先行」原则的来源。

接着列出四种贡献场景（[CONTRIBUTING.md:L11-L36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CONTRIBUTING.md#L11-L36)）：Bug 修复、代码优化、文档纠错、协助他人。每种都给出对应的 Issue 类型与 `/assign` 认领机制：

```text
Bug 修复   → Bug-Report|缺陷反馈  类 Issue
代码优化   → Requirement|需求建议  类 Issue
文档纠错   → Documentation|文档反馈 类 Issue
协助他人   → 在他人 Issue 下评论 / /assign 认领
```

`/assign` 是 gitcode 的命令，在 Issue 评论框输入即可把 Issue 分配给自己。这种「先认领、后开发」的机制避免多人重复劳动。

提交 PR 时按模板填写。模板文件是 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L25)，它规定了五段：描述、关联 Issue、测试、文档更新、类型标签。其中测试段（[L8-L9](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L8-L9)）明确提到「二级冒烟、算子泛化」——这正是 `run_presmoke.sh` 要覆盖的内容。类型标签段（[L14-L24](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L14-L24)）采用约定式提交（Conventional Commits）语义：`fix/feat/perf/refactor/test/docs/ci/revert/chore`，每个 PR 至少打一个。

> 说明：「约定式提交」是一种用固定前缀标注改动性质的规范（如 `feat:` 表示新功能），便于自动生成变更日志与版本号。

最后看一眼**代码分类规则** [classify_rule.yaml](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/classify_rule.yaml#L1-L40)，它把仓库按语言分三段（`asc-tools` 默认、`python@asc-tools`、`cpp@asc-tools`），每段声明责任人（`commiter`）、团队（`team`）、源码划分（`release`/`unrelease`）与测试开关（`llt.ut_check`/`st_check`）。它会被 CI 用来判断「这个路径的改动该通知谁、跑哪些测试」。注意 `unrelease` 列出的路径（如 `cpudebug/src/regfwk/stub_backtrace.cpp`）是**闭源边界**——这一点在 [u10-l1](u10-l1-extend-api-check.md) 已详述，本讲不重复。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍「Issue → 认领 → PR 模板」的纸面流程，建立流程直觉。

**操作步骤**：

1. 打开 [CONTRIBUTING.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CONTRIBUTING.md#L11-L36)，对照四种场景，判断「你想给 cpudebug 的 fp16 仿真补一个一元运算符」属于哪一类（提示：参考最近的提交 `c6f35b0 Add unary operator-() to struct half and Bf16T`）。
2. 打开 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L25)，为这个假设改动填写一份草稿 PR 描述（不必真的提交）。
3. 查阅 `classify_rule.yaml`，确认 `cpudebug/src/acl_stub/kernel_fp16.cpp` 落在 `cpp@asc-tools` 段的 `release` 还是 `unrelease`，以及对应的 `commiter`。

**需要观察的现象**：

- 你会发现这个改动属于「代码优化」场景，对应 `Requirement|需求建议` 类 Issue。
- 类型标签应勾选 `✨ feat` 或 `♻️ refactor`（取决于是否视为新能力）。
- `kernel_fp16.cpp` 在 `release` 列表中（开源可改），而 `stub_backtrace.cpp` 在 `unrelease` 中（闭源边界）。

**预期结果**：你能用一句话说清「这个改动该开什么类型的 Issue、PR 该打什么标签、该走哪段分类规则」。本实践为纯阅读型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：CONTRIBUTING.md 为什么强调「非简单 Bug 必须先开 Issue」？

> **参考答案**：为了避免贡献者投入大量精力写出与维护者方向不符的代码后被拒绝合入。先开 Issue 讨论方案，等于先对齐设计意图，再进入编码，节省双方成本。

**练习 2**：PR 模板里的类型标签 `fix` 和 `feat` 区别是什么？

> **参考答案**：`fix` 是 Bug 修复（修复已有缺陷，不改对外行为语义），`feat` 是新功能（新增能力或对外接口）。两者都来自约定式提交规范，影响变更日志的自动归类。

---

### 4.2 pre-commit 钩子与多语言检查

#### 4.2.1 概念说明

asc-tools 是双语言项目（C++ 厚核心 + Python 薄工具，见 [u1-l2](u1-l2-directory-structure.md)）。两种语言的风格规则不同，因此 pre-commit 配置了**五道关卡**，每道只管一类问题：

| 关卡 | 工具 | 拦截对象 |
|------|------|----------|
| ① 基础格式 | pre-commit-hooks | 行尾空格、文件末尾、YAML/JSON 合法性、大文件、合并冲突标记、私钥 |
| ② C++ 格式 | clang-format (v18) | C/C++/`.asc` 源码的排版 |
| ③ Python 检查 | ruff (v0.14) | Python 的 lint + 格式化 |
| ④ 拼写 | codespell | 文档与配置中的常见拼写错误 |
| ⑤ 合规 | OAT (本地脚本) | 许可证/版权头/文件类型 |

这种「一个工具管一件事」的分工，让每道关卡的规则可独立演进、报错可独立定位。

#### 4.2.2 核心流程

pre-commit 的工作机制是：`git commit` 时，框架按 `.pre-commit-config.yaml` 中 `repos` 列表顺序，逐个拉起每个 hook，把**本次暂存（staged）的文件**传给它。hook 退出码非 0 即阻断提交。整体顺序如下：

```text
git commit
   │
   ├─▶ ① pre-commit-hooks：trailing-whitespace / end-of-file-fixer / check-yaml ...
   ├─▶ ② clang-format：对 .c/.h/.cpp/.asc 等执行 -i 原地格式化
   ├─▶ ③ ruff-check (--fix) → ruff-format：检查并格式化 .py
   ├─▶ ④ codespell：扫描非代码文件的拼写
   └─▶ ⑤ oat_check.sh：对 staged 文件做增量合规扫描
              │
              └─ 任一失败 ─▶ 提交被拒，需修复后重新 git add + commit
```

注意：`clang-format`、`ruff-check --fix`、`ruff-format` 这类带修复能力的 hook 会**直接改动你的文件**。改动后文件与暂存区不一致，pre-commit 会标记失败，你需要重新 `git add` 被修复的文件再提交。这是初学者常踩的坑。

#### 4.2.3 源码精读

配置文件是 [.pre-commit-config.yaml](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L1-L72)。

**全局设置**（[L1-L7](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L1-L7)）：要求 pre-commit 版本 ≥ 4.0.0；排除 `LICENSES/` 目录与 `.html/.csv/.svg` 文件；CI 中不自动修 PR（`autofix_prs: false`），每月自动更新依赖版本。

**关卡① 基础格式**（[L11-L22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L11-L22)）：来自 `pre-commit/pre-commit-hooks`，七个检查项：

- `trailing-whitespace`：行尾空格。
- `end-of-file-fixer`：确保文件以换行结尾。
- `check-yaml`（`--allow-multiple-documents`）：YAML 合法性，允许多文档。
- `check-added-large-files`：拦截大文件（防误提交二进制）。
- `check-merge-conflict`：拦截遗留的合并冲突标记。
- `detect-private-key`：拦截私钥泄露。
- `check-json`：JSON 合法性（排除 `.devcontainer/devcontainer.json`）。

**关卡② C++ 格式**（[L24-L34](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L24-L34)）：用 clang-format v18.1.8，匹配 `.c/.h/.cpp/.hpp/.cc/.hh/.cxx/.hxx/.asc`（**注意 `.asc` 也在内**，因为 ASC 算子源码也按 C 风格格式化），排除 `build/` 与 `tests/third_party/`，用 `--style=file` 表示读项目根的 `.clang-format` 规则，`-i` 表示原地修改。

风格规则在 [.clang-format](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.clang-format#L1-L55)：基于 Google 风格，但有关键定制——`ColumnLimit: 120`（行宽 120，比 Google 默认的 80 宽）、`SortIncludes: false`（**不自动排序 include**，因为头文件顺序在 CANN 体系里有语义）、`BreakBeforeBraces: Custom` 配 `AfterFunction: true`（函数体左大括号换行，其余不换行）、`PointerAlignment: Left`（指针 `*` 靠左）。

**关卡③ Python 检查**（[L36-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L36-L43)）：用 ruff v0.14.14，分两步——`ruff-check --fix`（lint 并自动修复，输出格式仿 github）+ `ruff-format`（格式化）。ruff 是用 Rust 写的超快 Python 工具，一个工具替代了传统的 flake8 + isort + black 组合。

**关卡④ 拼写**（[L46-L57](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L46-L57)）：用 codespell v2.4.1，`--write-changes` 自动改错。两个关键参数值得注意：

- `-L` 白名单：列出项目专有词（`CANN,cann,ASCEND,ascend,EnQue,CopyIn,...`），这些是正确拼写，不要被 codespell 当错别字改掉。
- `--skip "*.py,*.cpp,*.hpp,*.c,*.h"`：**跳过所有代码文件**，只查文档与配置。设计意图是代码里的标识符太多误报，拼写检查只盯人写的自然语言文本。

**关卡⑤ OAT 合规**（[L59-L71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.pre-commit-config.yaml#L59-L71)）：这是一个 `repo: local` 的**本地钩子**，`entry: bash scripts/oat_check.sh`，`require_serial: true`（串行只跑一次，而非每个文件跑一次），排除 `LICENSE` 文件。这是本讲 4.3 节的主角。

#### 4.2.4 代码实践

**实践目标**：在本地安装并触发 pre-commit，观察五道关卡的实际行为。

**操作步骤**：

1. 安装框架：`pip install pre-commit`（需 ≥ 4.0.0）。
2. 在仓库根执行 `pre-commit install`，把钩子装进 `.git/hooks/pre-commit`。
3. 故意制造一个格式问题：在某个 `.py` 文件末尾加几个行尾空格，或在某个 `.cpp` 文件里把缩进改乱。
4. `git add` 该文件后执行 `git commit -m "test"`，观察输出。

**需要观察的现象**：

- 行尾空格会被 `trailing-whitespace` 自动删除；`.cpp` 缩进会被 `clang-format -i` 自动按 `.clang-format` 规则修复。
- 修复后 pre-commit 报失败（因为文件被改了，与暂存区不一致），提示你重新 `git add`。
- 如果 Python 文件有 lint 问题，`ruff-check --fix` 会尝试自动修，修不掉的会列出错误。

**预期结果**：你会看到「工具自动改文件 → 提交被拒 → 重新 add → 再次提交通过」的循环。这正是带修复能力 hook 的标准交互。若本地未装 clang-format v18 或 ruff，对应 hook 会因找不到命令而报错（待本地验证环境是否齐备）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.clang-format` 要设 `SortIncludes: false`？

> **参考答案**：因为 CANN 的 C++ 头文件包含顺序往往有语义（如先公共头、再模块头、再系统头），自动重排会破坏这种约定甚至引入宏定义顺序依赖问题，所以关闭自动排序，由开发者手工保证顺序。

**练习 2**：codespell 为什么用 `--skip` 跳过所有 `.py/.cpp` 代码文件？

> **参考答案**：代码文件里大量标识符、变量名会被拼写检查器误判为错别字，产生海量误报。codespell 的价值在于检查文档与配置中「人写的自然语言」，所以只扫非代码文件，并通过 `-L` 白名单放过项目专有词。

---

### 4.3 OAT 开源合规检查

#### 4.3.1 概念说明

**OAT（Open Source Audit Tool）** 是开源合规审计工具，回答三个问题：

1. **许可证（License）**：每个源文件是否有合法的许可证头？许可证类型是否被项目策略允许？
2. **版权（Copyright）**：版权声明是否正确（如 `Huawei Technologies Co., Ltd.`）？
3. **文件类型（File Type）**：是否混入了策略不允许的二进制文件？

为什么开源项目要查这些？因为一旦把**无许可证头**或**来路不明的二进制**合入开源仓，会引发知识产权纠纷，影响整个项目的可分发性。OAT 把这种风险拦在提交前。

asc-tools 的 OAT 检查有几个特别之处：

- 它是**增量扫描**：只查本次 `git add` 暂存的文件，而非全仓，速度更快。
- 它由 pre-commit 触发，**阻断提交**而非只警告（除非你显式 `--no-verify`）。
- 它用 Python 版 `oat-py`（`python -m oat`），取代了旧的 Java 二进制版本（见脚本头注释）。

#### 4.3.2 核心流程

`oat_check.sh` 的执行分 8 步，可以用下面的流程概括：

```text
0. 定位 Python 3.7+ 解释器（找不到则跳过检查，不阻断）
1. 确保 oat-py 已安装（缺失则 pip 安装；装失败也跳过）
2. 确定仓库根与名称，准备 oat_reports/ 输出目录
3. 收集暂存文件：
     - 有参数 → 用传入的文件列表
     - 无参数 → git diff --cached --name-only --diff-filter=ACM
4. 确保 oat_reports/ 与 log/ 在 .gitignore 中
5. 拼 oat 命令：python -m oat -mode s ... 若存在 OAT.xml 则加 -oatconfig
6. 运行扫描，容忍退出码 0（成功）与 1（发现问题），其他码视为异常跳过
7. 解析报告 PlainReport_*.txt，提取「Invalid File Type」与「License Header Invalid」两段
8. 若两类问题总数 > 0 → 打印详情、阻断提交（exit 1）；否则放行
```

最关键的设计是**容错优先于阻断**：Python 缺失、oat-py 装不上、扫描异常退出，统统**跳过检查放行提交**（打印 WARNING）；只有扫描成功且确实发现合规问题时才阻断。这避免了「环境问题导致没人能提交」的死锁。

阻断的两类问题（脚本注释 [L170-L171](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L170-L171) 点明）是：

- **Invalid File Type**：混入了不允许的二进制文件。
- **License Header Invalid**：源文件缺少/错误许可证头（不含版权问题）。

#### 4.3.3 源码精读

先看**策略声明** [OAT.xml](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/OAT.xml#L1-L61)，它是 OAT 扫描的「规则书」，由 `oat_check.sh` 经 `-oatconfig` 传入（[oat_check.sh:L143-L146](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L143-L146)）。

**策略清单**（[OAT.xml:L6-L10](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/OAT.xml#L6-L10)）声明三条：

| 类型 | 名称 | 规则 | 含义 |
|------|------|------|------|
| license | CANN-2.0 | may | 允许 CANN-2.0 许可证 |
| copyright | Huawei Technologies Co., Ltd. | may | 允许华为版权声明 |
| filetype | !binary | must | 所有文件**必须非二进制** |

`rule="may"` 表示「允许出现」，`rule="must"` 表示「必须满足」。`!binary` 表示「禁止二进制」。

**豁免清单**（[OAT.xml:L12-L46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/OAT.xml#L12-L46)）：三组过滤器（`defaultPolicyFilter`、`copyrightPolicyFilter`、`binaryFileTypePolicyFilter`）列出**免查**的文件名，如 `OWNERS`、`.gitmodules`、`*.csv`、`*.yaml`、`*.xml`、`*.info`。这些是元数据/配置文件，本就不该有许可证头，故豁免。注意 `*.png` 被列入 `binaryFileTypePolicyFilter`（[L42-L44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/OAT.xml#L42-L44)），即文档图片这种二进制是允许的。

**许可证文本匹配**（[OAT.xml:L47-L58](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/OAT.xml#L47-L58)）：`licensematcher` 声明了一段名为 `CANN License` 的标准许可证文本。OAT 正是用这段文本去**匹配源文件头部**，判断该文件是否有合法的 CANN 许可证头。对比真实源码头部即可验证——[kernel_fp16.cpp:L1-L9](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L1-L9) 与 [ascendc_npuchk_report.py:L3-L11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L3-L11) 的头部文字，与 OAT.xml 中 `licensetext` 几乎逐字一致。这就是「许可证头合法」的判定依据——**每个新增源文件都必须带这段头部**，否则 OAT 会报 License Header Invalid 并阻断提交。

再看**扫描脚本** [scripts/oat_check.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L1-L275)。

脚本头部注释（[L11-L15](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L11-L15)）点明它是 Python 版（`oat-py`），取代旧的 Java 二进制版本。它有一段**自愈逻辑**（[L18-L26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L18-L26)）：如果脚本自身被 Windows 改成了 CRLF 行尾，会自动去 `\r` 后重新执行，保证跨平台可用。

**步骤 0 定位 Python**（[L33-L48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L33-L48)）：依次尝试 `python3/python/py`，校验版本 ≥ 3.7，找不到则 `exit 0`（跳过，不阻断）。

**步骤 1 安装 oat-py**（[L53-L64](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L53-L64)）：用 `importlib.util.find_spec('oat')` 检测，缺失则 `pip install oat-py>=1.0.1`，装失败同样跳过。

**步骤 3 收集暂存文件**（[L96-L116](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L96-L116)）：无参数时走 `git diff --cached --name-only --diff-filter=ACM`——只取**新增(A)/修改(C)/修改(M)**的暂存文件，这就是「增量」的来源（删除的文件不查）。文件转成绝对路径、逗号拼接成 `-f` 参数。

**步骤 4 维护 .gitignore**（[L128-L136](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L128-L136)）：确保 `oat_reports/` 和 `log/` 被忽略——这与仓库 [.gitignore:L10-L12](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitignore#L10-L12) 已有的 `oat_reports/`、`log/` 一致，避免报告误提交。

**步骤 5 拼命令**（[L141-L146](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L141-L146)）：

```text
python -m oat -mode s -s <仓库根> -r oat_reports -n <仓库名> -w 1 -f <文件列表> [-oatconfig OAT.xml]
```

其中 `-mode s` 表示 source 模式，`-w 1` 是 worker 数。

**步骤 6 容错运行**（[L154-L166](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L154-L166)）：`set +e` 关闭立即退出，捕获退出码；只有码为 0 或 1 才继续解析，其他码（如崩溃）打印 WARNING 后 `exit 0` 放行。

**步骤 8 阻断判定**（[L249-L268](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/oat_check.sh#L249-L268)）：`TOTAL_ISSUES = InvalidFileType + LicenseHeaderInvalid`，大于 0 则打印详情、提示 `git commit --no-verify` 可跳过，并 `exit 1` 阻断。

> 说明：`git commit --no-verify` 是 Git 的逃生口，跳过所有 pre-commit 钩子。社区贡献中**不应常规使用**，仅在你确信是误报时临时绕过。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次 OAT 增量扫描，观察「许可证头缺失」如何被阻断。

**操作步骤**：

1. 在仓库根新建一个测试文件 `test_oat.cpp`，**故意不写许可证头**，只写一行 `int main(){return 0;}`。
2. `git add test_oat.cpp`。
3. 手动直接调用脚本（模拟 pre-commit 行为，绕过真的提交）：

   ```bash
   bash scripts/oat_check.sh test_oat.cpp
   ```

4. 观察输出，重点看 `oat_reports/result.txt`。

**需要观察的现象**：

- 脚本会打印 `[OAT] Found N compliance issue(s)`，其中 `License Header Invalid` 计数为 1。
- 退出码为 1（阻断）。
- `result.txt` 里列出该文件的许可证头问题。

**预期结果**：你验证了「没有 CANN 许可证头的源文件会被 OAT 拦下」。随后给 `test_oat.cpp` 补上标准头部（参照 [kernel_fp16.cpp:L1-L9](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L1-L9) 的格式），重新运行应看到 `[OAT] [OK] All checks passed`。

> 注意：此实践需要本地装有 Python 3.7+ 且能 `pip install oat-py`；若环境无网络导致 oat-py 装不上，脚本会跳过检查（不阻断），此时现象为 WARNING，属于环境限制，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `oat_check.sh` 在 Python 缺失或 oat-py 装不上时选择「跳过」而非「阻断」？

> **参考答案**：为了保证贡献流程不被环境问题卡死。合规检查是「锦上添花」的门禁，若因开发者机器没装 Python 就让所有人无法提交，会形成死锁。设计上把「环境不可用」与「确实违规」区分开：前者放行并警告，后者才阻断。真正的全量合规扫描在服务端 CI 兜底。

**练习 2**：`OAT.xml` 里 `filetype !binary rule="must"` 与 `binaryFileTypePolicyFilter` 中放行 `*.png` 是什么关系？

> **参考答案**：策略要求「所有文件必须非二进制」，但文档配图 `*.png` 是合法的二进制用途，所以在 `binaryFileTypePolicyFilter` 里把 `*.png` 列为豁免。这体现了「策略从严、豁免显式」的原则——默认禁止二进制，只在白名单里允许特定类型的二进制。

---

### 4.4 配套仓版本协同约束

#### 4.4.1 概念说明

asc-tools 不是孤立项目，它是 **CANN（昇腾异构计算架构）** 大家族的一员。一个完整的 CANN 运行环境由多个仓拼装而成，asc-tools 只是其中的「调测工具集」。当 asc-tools 升级时，必须保证它与这些兄弟仓**版本兼容**，否则会出现「工具编译过了，但跑不起来」的割裂。

asc-tools 在 [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L1-L20) 里显式声明了这种依赖。理解它，是理解「为什么 asc-tools 不能随便单仓升级」的关键。

#### 4.4.2 核心流程

CANN 的版本号遵循 `主版本.次版本.修订号`（major.minor.patch），当前 asc-tools 是 `9.1.0`。版本协同的核心规则是 **主次版本号对齐**（major.minor 必须一致），仓内用占位符 `CUR_MAJOR_MINOR_VER` 表达「与当前主次版本对齐」。

```text
asc-tools 9.1.0
   │
   ├── 编译依赖 (build_dependencies)
   │      └── runtime          必须主次版本对齐 (CUR_MAJOR_MINOR_VER)
   │
   └── 运行依赖 (run_dependencies)
          ├── runtime          必须主次版本对齐
          ├── ge-executor      必须主次版本对齐   (图执行器)
          ├── metadef          必须主次版本对齐   (图元定义)
          └── asc-devkit       必须主次版本对齐   (开发套件)
```

这意味着：如果你拿到一个 asc-tools `9.1.0`，它必须搭配 `9.1.x` 的 runtime/ge-executor/metadef/asc-devkit 才能正常工作；混用 `9.1` 的工具与 `9.0` 的运行时会因接口不匹配而出错。这与 [u1-l3](u1-l3-environment-setup.md) 讲过的「asc-tools 不能独立升级、须搭配版本匹配的 CANN 包」是同一件事的源码侧佐证。

#### 4.4.3 源码精读

版本与依赖声明集中在 [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L1-L20)：

- [version.cmake:L11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11)：声明 `ASC_TOOLS_VERSION "9.1.0"`，并通过 `set_cann_package(asc-tools VERSION ...)` 把 asc-tools 注册为一个 CANN 标准包。这个版本号会被 [u9-l2](u9-l2-package-install.md) 讲过的打包流程用来拼 run 包文件名（如 `cann-asc-tools_9.1.0_xxx.run`）。
- [version.cmake:L14](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L14)：`set_cann_build_dependencies(runtime "CUR_MAJOR_MINOR_VER")`——**编译期**依赖 runtime，且主次版本必须对齐。
- [version.cmake:L16-L19](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L16-L19)：四条**运行期**依赖，runtime/ge-executor/metadef/asc-devkit 均要求 `CUR_MAJOR_MINOR_VER` 对齐。

`set_cann_build_dependencies` 与 `set_cann_run_dependencies` 是 CANN 仓体系（cann-cmake 工具链）提供的宏，它们把依赖关系写进包的元数据，供 CANN 整体安装器在装 asc-tools 时校验「兄弟仓是否都装了、版本是否匹配」。这就是为什么 [u1-l3](u1-l3-environment-setup.md) 强调「toolkit 与 ops 须装到同一路径」——同一套 CANN 安装树下的所有包共享一个主次版本。

这些宏与 `CUR_MAJOR_MINOR_VER` 占位符本身定义在闭源的 cann-cmake 工具链里，本仓不直接可见，属「待确认」其精确实现细节，但语义已由用法明确。

与版本协同配套的还有 [scripts/update_version_info/update_version_info.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/update_version_info/update_version_info.sh#L1-L48)，它在打包期把构建时间戳（`timestamp=YYYYMMDD_HHMMSSsss`）追加到版本信息文件（[L37-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/update_version_info/update_version_info.sh#L37-L45)），用于区分同版本号的不同构建产物。注意它优先用环境变量 `tagInfo` 里解析出的时间戳（[L23-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/update_version_info/update_version_info.sh#L23-L31)），保证可重现构建。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `version.cmake` 与本地 CANN 安装信息，验证版本协同关系。

**操作步骤**：

1. 打开 [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11-L19)，记录 asc-tools 的版本号（`9.1.0`）与其声明的 4 个运行依赖。
2. 若本地已装 CANN（参照 [u1-l3](u1-l3-environment-setup.md)），查看安装信息文件中的版本号，例如：

   ```bash
   cat /usr/local/Ascend/ascend-toolkit/latest/ascend_toolkit_install.info
   ```

3. 比对 asc-tools 的主次版本（`9.1`）与本地 toolkit 的主次版本是否一致。

**需要观察的现象**：

- `version.cmake` 中 asc-tools 主次版本为 `9.1`。
- 本地 toolkit 安装信息里的版本号主次部分应同为 `9.1`（如 `9.1.0.xxx`）。

**预期结果**：两者主次版本号一致，说明版本协同满足；若不一致，则该 asc-tools 与本地 CANN 不匹配，需更换匹配版本。若本地未装 CANN，此项标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`CUR_MAJOR_MINOR_VER` 这个占位符表达什么约束？为什么不是要求 patch 号也一致？

> **参考答案**：它要求依赖仓与 asc-tools 的**主版本和次版本**对齐（如都用 `9.1`），但不要求修订号（patch）一致。因为按语义化版本约定，同主次版本内只允许向后兼容的修订（Bug 修复），接口稳定；而主次版本变更意味着可能有破坏性改动，必须严格对齐。这样既保证兼容，又允许各仓独立发修订版。

**练习 2**：`set_cann_build_dependencies` 与 `set_cann_run_dependencies` 有何区别？

> **参考答案**：前者声明**编译期**依赖——构建 asc-tools 时必须存在的兄弟仓（如编译 cpudebug 需要 runtime 的头文件/库）；后者声明**运行期**依赖——用户安装并运行 asc-tools 产物时必须存在的兄弟仓。运行依赖通常比编译依赖更多（运行时还需 ge-executor、metadef、asc-devkit 协同）。

---

## 5. 综合实践

**任务**：模拟一次完整的合规贡献，把本讲四条主线串起来。

假设你要给 asc-tools 的 Python 工具 `msobjdump` 新增一个小功能（例如给 `--verbose` 输出加一行版本说明）。请完成以下步骤：

1. **方案先行**（对应 4.1）：按 CONTRIBUTING.md，判断这属于哪类贡献，写出应开的 Issue 类型与一句话方案描述。
2. **写代码并加许可证头**（对应 4.3）：新建/修改 `utils/msobjdump/msobjdump/utils.py`，确保文件头部带 CANN 许可证头（参照 [ascendc_npuchk_report.py:L3-L11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L3-L11) 的格式）。
3. **过 pre-commit**（对应 4.2）：`git add` 后 `git commit`，观察 ruff-format 是否自动格式化你的 Python 代码、OAT 是否放行（许可证头合法）。
4. **跑冒烟**（对应 4.1 提到的二级冒烟）：阅读 [scripts/run_presmoke.sh:L40-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/run_presmoke.sh#L40-L43) 与 [L100-L104](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/run_presmoke.sh#L100-L104)，说明该脚本如何用 grep `test pass|passed|[Block (5/6)]: OUTPUT = 24` 判定样例通过；若环境允许，执行 `bash scripts/run_presmoke.sh`（需先 source CANN 环境，见 [L20-L27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/run_presmoke.sh#L20-L27)）。
5. **填 PR 模板**（对应 4.1）：按 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L25) 起草描述，勾选合适类型标签，在「测试」段写明你跑了冒烟。
6. **版本自检**（对应 4.4）：确认你的改动没有改动 `version.cmake` 的版本号或依赖声明（除非确有破坏性变更，那需要先开 Issue 讨论）。

**交付物**：一份草稿 PR 描述 + 一份「我本地过了哪些检查」的清单（pre-commit 五道关卡、冒烟脚本、OAT result.txt）。

> 说明：本实践的 3、4 步依赖本地具备 pre-commit、clang-format、ruff、Python+oat-py、CANN 环境齐全；若部分缺失，对应步骤标注「待本地验证」并说明预期现象即可，不假装已运行。

## 6. 本讲小结

- asc-tools 的贡献链路是 **Issue 方案讨论 → 本地编码 → pre-commit 五道关卡 → 冒烟验证 → PR 模板评审**，核心哲学是把问题前移到本地与 PR 阶段。
- `.pre-commit-config.yaml` 配置了五道检查关卡：基础格式（pre-commit-hooks）、C++ 格式（clang-format v18，读 `.clang-format` 规则，行宽 120、不排 include）、Python（ruff 检查+格式化）、拼写（codespell 只查非代码文件）、OAT 合规（本地脚本）。
- OAT 合规由 `scripts/oat_check.sh` 实现**增量扫描**（只查 `git diff --cached` 的 ACM 文件），策略由 `OAT.xml` 声明（允许 CANN-2.0 许可证、华为版权、禁止二进制），阻断两类问题：Invalid File Type 与 License Header Invalid；环境不可用时容错放行，违规时才阻断。
- 每个源文件必须带标准 CANN 许可证头（与 `OAT.xml` 的 `licensetext` 匹配），否则 OAT 阻断提交；`git commit --no-verify` 是逃生口但不应常规使用。
- asc-tools 通过 `version.cmake` 声明与 runtime/ge-executor/metadef/asc-devkit 的**主次版本对齐**依赖（`CUR_MAJOR_MINOR_VER`），不能孤立升级，这是 [u1-l3](u1-l3-environment-setup.md)「须搭配版本匹配 CANN 包」的源码侧依据。
- `scripts/run_presmoke.sh` 是提 PR 前的二级冒烟，编译并运行两个样例，用关键字 grep 判定通过，对应 PR 模板「测试」段。

## 7. 下一步学习建议

- 本讲是学习手册的最后一篇，建议**回头做一次全链路串联**：选一个真实的小改动（如给某个 Python 工具加日志），从 [u1-l4](u1-l4-build-and-first-sample.md) 的编译、[u9-l3](u9-l3-unit-testing.md) 的测试，一路走到本讲的 pre-commit 与 OAT，完整体验一次「编译→测试→合规→提交」。
- 若你想扩展 C++ 校验逻辑，继续精读 [u10-l1 扩展 API 校验器与二次开发](u10-l1-extend-api-check.md)，那里讲清了开源/闭源边界对二次开发的具体约束——本讲的 `classify_rule.yaml` `unrelease` 列表正是那条边界的清单。
- 想深入理解打包与版本落地的工程细节，可重读 [u9-l2 打包安装与 run 包生成](u9-l2-package-install.md)，与本讲的 `version.cmake`、`update_version_info.sh` 对照，看清版本号从声明到写进 run 包文件名的完整路径。
