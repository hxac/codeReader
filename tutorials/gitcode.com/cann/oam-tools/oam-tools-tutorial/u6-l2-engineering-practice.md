# 工程规范：OAT 检查、pre-commit 与增量代码检查

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚向 oam-tools 提交一次合格 PR 之前，本地要过哪些工程检查门禁。
2. 理解 OAT（Open Source Audit Tool）开源合规检查的原理：`OAT.xml` 定义策略，`scripts/oat_check.sh` 驱动 `oat-py` 只扫 staged 文件。
3. 逐个说出 `.pre-commit-config.yaml` 中 6 个钩子各自的作用、版本锁定原因与 `exclude` 屏蔽策略。
4. 读懂 `scripts/incremental_codecheck.py` 如何用「ruff 全量检查 + 只保留 staged 改动行」在本地复现云端增量 codecheck 行为。
5. 独立完成：安装 pre-commit、触发一次钩子链、手动跑一次增量检查并解读输出。

## 2. 前置知识

- **pre-commit**：一个 Git 钩子管理框架。你在仓库里放一个 `.pre-commit-config.yaml`，执行 `pre-commit install` 后，它会把 Git 原生的 `.git/hooks/pre-commit` 替换为自己的调度脚本。此后每次 `git commit`，钩子链会先跑一遍，任何钩子返回非 0 都会**阻止提交**。每个钩子声明自己来自哪个仓库（`repo`/`rev`）、检查哪类文件（`types`）、跳过哪些路径（`exclude`）。
- **staged（暂存区）**：`git add` 之后、`git commit` 之前的改动。`git diff --cached` 看到的就是 staged diff。本讲的 OAT 检查和增量检查都只关心 staged 文件——这与云端 CI「只查 PR 改动」的行为对齐。
- **OAT（Open Source Audit Tool）**：开源合规审计工具，检查仓库里的文件类型是否合法（`Invalid File Type`）、源码是否带正确的 License 头（`License Header Invalid`）、License 是否兼容等。本仓用的 `oat-py` 是其 Python 实现（pip 包 `oat-py>=1.0.1`）。
- **ruff**：一个极快的 Python linter，一条命令可 `--select` 任意规则子集（如 `E501` 行超宽、`T201` 用了 print）。
- **存量违规 vs 增量违规**：本仓历史代码里有很多超长行、超大函数、print 语句（存量违规）；云端 codecheck 只对 **PR 新增/修改的行** 卡门禁（增量违规）。这是本讲最重要的背景：本地门禁如果整文件放开规则，会被存量违规「淹没」，比云端更严、堵死提交。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CONTRIBUTING.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CONTRIBUTING.md) | 贡献指南：PR 模板要求、Issue 先行原则、三类贡献场景 |
| [AGENTS.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/AGENTS.md) | 仓库工作指导：构建/测试命令、目录结构、代码风格与 pre-commit 提示 |
| [.pre-commit-config.yaml](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml) | pre-commit 钩子链定义：clang-format、OAT、ruff、pylint、增量检查、bandit、codespell |
| [OAT.xml](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/OAT.xml) | OAT 策略配置：License 白名单、版权策略、文件过滤器、License 匹配文本 |
| [scripts/oat_check.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/oat_check.sh) | OAT pre-commit 钩子脚本：装 oat-py、收集 staged 文件、跑扫描、解析报告、决定是否阻断提交 |
| [scripts/incremental_codecheck.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py) | 增量行 codecheck 钩子：ruff 全量查 + 只保留命中 staged 改动行的告警 |

## 4. 核心概念与源码讲解

### 4.1 贡献流程与规范入口：CONTRIBUTING.md 与 AGENTS.md

#### 4.1.1 概念说明

任何工程门禁都要回答一个问题：**它在守护什么流程？** 对 oam-tools 来说，流程入口有两份文档：`CONTRIBUTING.md` 面向人类贡献者，规定「先签 CLA、先提 Issue、按模板写 PR」；`AGENTS.md` 面向在仓库里工作的 agent（以及快速查阅命令的人），汇总构建、测试、风格约定。本讲的三个检查工具都是这条贡献流程里「push 之前」的本地关卡。

#### 4.1.2 核心流程

一次合格贡献的完整链路：

```text
签署 CLA（cann-community）
  → 非简单 bug 修复？先提 Issue 讨论方案
  → 本地改代码
  → git commit 触发 pre-commit 钩子链（本讲主角）
  → git push
  → 云端 CI：codecheck（增量）+ 门禁测试 + OAT 流水线
  → 按模板填写 PR 描述 → 评审合入
```

#### 4.1.3 源码精读

CONTRIBUTING.md 明确了两条硬性要求——按模板填 PR、以及「新增特性/接口/配置/改流程必须先 Issue 讨论」，见 [CONTRIBUTING.md:L5-L8](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CONTRIBUTING.md#L5-L8)：这段规定了「Issue 先行」原则，避免代码因方案未对齐被拒。

三类贡献场景（Bug 修复、文档纠错、帮他人解 Issue）都通过 `/assign` 评论认领 Issue，见 [CONTRIBUTING.md:L12-L29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CONTRIBUTING.md#L12-L29)。

AGENTS.md 的「开发规范」小节给出了代码风格的权威来源：C/C++ 用 `.clang-format`、Python 遵循 PEP 8，并提示项目已配置 pre-commit，见 [AGENTS.md:L70-L76](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/AGENTS.md#L70-L76)。

#### 4.1.4 代码实践

1. **实践目标**：建立贡献流程的全局图景。
2. **操作步骤**：通读 CONTRIBUTING.md 全文（约 30 行）与 AGENTS.md 的「构建命令」「开发规范」两节；打开 gitcode.com/cann/community 首页浏览 Issue/PR 流程说明。
3. **需要观察的现象**：注意 CONTRIBUTING.md 中没有出现任何具体检查命令——工程检查细节全部下沉到了 `.pre-commit-config.yaml` 和 `scripts/`。
4. **预期结果**：能回答「我要新增一个配置参数，能不能直接提 PR？」（答案：不能，必须先 Issue 讨论，见 L8）。

#### 4.1.5 小练习与答案

**练习 1**：为什么本仓要求「非简单 bug 修复先提 Issue」？
**答案**：避免方案未经维护者对齐导致代码被拒绝合入（CONTRIBUTING.md L8 原文：『以避免您的代码被拒绝合入』）；不确定是否算简单修复时也应 Issue 讨论。

**练习 2**：AGENTS.md 和 CONTRIBUTING.md 的读者有何不同？
**答案**：CONTRIBUTING.md 面向人类社区贡献者，讲流程礼仪；AGENTS.md 面向在仓库内工作的 agent/开发者，汇总构建命令、目录结构和风格约定，是「快速上手卡片」。

### 4.2 OAT 开源合规检查：OAT.xml + oat_check.sh

#### 4.2.1 概念说明

开源仓库必须保证每个文件「来路干净」：文件类型合法、带正确的 Apache 2.0 License 头与华为版权声明。OAT 就是做这件事的审计工具。本仓的接入分两层：

- `OAT.xml`：**策略层**——声明允许什么 License、版权主体是谁、哪些文件免检。
- `scripts/oat_check.sh`：**驱动层**——一个自愈型 shell 脚本，作为 pre-commit 本地钩子运行，只检查本次 staged 的文件（增量模式），发现合规问题就 `exit 1` 阻断提交。

#### 4.2.2 核心流程

oat_check.sh 的执行流水线（8 步，脚本内自带编号注释）：

```text
0. 自愈：检测到自身含 Windows CRLF → 自动转 LF 并重跑
1. 定位 Python 3.7+ 解释器（python3/python/py 依次尝试）
   └─ 找不到 → 告警后 exit 0 放行（不阻断提交）
2. 确保 oat-py 已安装（缺失则 pip 安装，失败也放行）
3. 收集 staged 文件：git diff --cached --name-only --diff-filter=ACM
   （ACM = Added/Copied/Modified，不含删除）
4. 确保 oat_reports/ 存在且写入 .gitignore
5. 组装 oat 命令参数（bash 数组防注入），仓库根有 OAT.xml 则带上 -oatconfig
6. 运行 oat 扫描；退出码非 0/1 视为工具异常 → 放行
7. 解析报告 PlainReport_<repo>.txt，抽取两类问题：
   Invalid File Type + License Header Invalid → 汇总写 result.txt
8. 两类问题数之和 > 0 → 打印详情与修复指引，exit 1 阻断提交
```

注意一个贯穿始终的设计取向：**工具自身失败永远不阻断提交**（exit 0 放行），只有「确凿的合规问题」才阻断。

#### 4.2.3 源码精读

OAT.xml 的策略核心是两条 policyitem：允许 Apache-2.0 License、版权主体为华为，见 [OAT.xml:L21-L27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/OAT.xml#L21-L27)：这段定义了仓库的 License 白名单与版权策略，任何不匹配的文件会进报告。

`defaultPolicyFilter` 过滤器把 LICENSE、`*.info`、`*.xml`、`*.csv`、`*.yaml` 等文件类型排除在 License 头检查之外，见 [OAT.xml:L43-L51](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/OAT.xml#L43-L51)：这些文件类型没有源码注释语法，无法携带 License 头，故免检。

`binaryFileTypePolicyFilter` 把 `*.dll`/`*.so`/`*.a`/`*.pyc` 排除在二进制文件检查之外，见 [OAT.xml:L76-L82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/OAT.xml#L76-L82)。

脚本头部的 CRLF 自愈逻辑见 [scripts/oat_check.sh:L24-L32](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/oat_check.sh#L24-L32)：先用 `sed 's/\r//'` 生成 LF 版本再与原文件 diff，不一致说明脚本被 Windows 编辑器改出了 CRLF，自动转 LF 后 `exec` 重跑自身——钩子在 Windows Git Bash 下也不会失效。

staged 文件收集只取新增/复制/修改，见 [scripts/oat_check.sh:L103-L108](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/oat_check.sh#L103-L108)：`git diff --cached --name-only --diff-filter=ACM` 是「增量模式」的落点，已删除的文件不查。

用 bash 数组而非 eval 拼字符串组装参数，见 [scripts/oat_check.sh:L145-L154](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/oat_check.sh#L145-L154)：注释写明动机——路径含空格或 shell 元字符时防止参数被拆分甚至命令注入；同时检测仓库根的 OAT.xml 存在才附加 `-oatconfig`。

报告解析与提交阻断见 [scripts/oat_check.sh:L221-L276](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/oat_check.sh#L221-L276)：从 `PlainReport_<repo>.txt` 里 grep 出 `Invalid File Type Total Count:` 与 `License Header Invalid Total Count:` 两个计数，求和大于 0 就打印 result.txt 路径并 `exit 1`；脚本还提示了逃生通道 `git commit --no-verify`。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 OAT 检查阻断一次提交。
2. **操作步骤**：
   - `pip3 install "oat-py>=1.0.1"`；
   - 在仓库里新建一个测试文件，例如 `bad_type.tmp`，内容随便写几行，**不加任何 License 头**；再新建一个 `no_header.py`，内容只有 `print("hi")`；
   - `git add bad_type.tmp no_header.py && bash scripts/oat_check.sh bad_type.tmp no_header.py`（脚本支持直接传文件参数模拟手动测试，见 L85-L101）；
   - 检查完 `rm bad_type.tmp no_header.py && git reset` 清理。
3. **需要观察的现象**：终端输出 `[OAT] Found N compliance issue(s)` 与两类问题计数；`oat_reports/result.txt` 里出现 `OAT Scan Result Summary` 摘要。
4. **预期结果**：非法文件类型（`.tmp` 不在白名单）和缺失 License 头的 `.py` 分别被计入两个 Count，脚本退出码为 1。若 oat-py 安装失败，脚本只会 WARNING 后放行——也请记录这个现象。**待本地验证**（具体计数取决于 oat-py 版本的文件类型表）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 oat_check.sh 在 Python 缺失、oat-py 装不上、oat 退出码异常时都选择 `exit 0` 放行？
**答案**：门禁的定位是「确凿问题才阻断」。工具自身故障（环境缺依赖、版本冲突）不是贡献者的合规问题，阻断会把所有提交堵死；所以只有解析出真实的 Invalid File Type / License Header Invalid 才 `exit 1`。代价是这些故障会被静默跳过，所以保留了手动重跑命令的提示。

**练习 2**：`--diff-filter=ACM` 为什么要排除 D（Deleted）？
**答案**：被删除的文件不再存在于工作区，也不存在「引入不合规文件」的风险；OAT 检查的对象是即将进入仓库的文件内容。

**练习 3**：一个新提交的 `config.yaml` 忘了 License 头会被 OAT 卡住吗？
**答案**：不会。OAT.xml 的 `defaultPolicyFilter` 明确把 `*.yaml` 列为 License 头检查的豁免类型（OAT.xml L48）。

### 4.3 pre-commit 钩子链：.pre-commit-config.yaml

#### 4.3.1 概念说明

`.pre-commit-config.yaml` 是本地门禁的「总装配表」。它定义了 6 个钩子，覆盖 C/C++ 格式、开源合规、Python lint、Python 静态检查、增量规则检查、Python 安全和拼写。理解这份文件的关键有三点：

1. **`minimum_pre_commit_version: "3.2.0"`**：配置用了 `stages: [pre-commit]` 新 stage 名，旧版 pre-commit 会报非法 stage 导致全部钩子失效。
2. **版本锁定的现实原因**：CI 的 pre-commit 环境是 Python 3.9，pylint 4.x / bandit 1.9.x 要求 Python ≥ 3.10，装不上会让整个任务报错，所以分别钉在 v3.3.9 和 1.8.6。
3. **`exclude` 的「存量豁免」策略**：存量代码不符合新规则，一旦钩子扫到就卡门禁；所以先屏蔽 `src/{asys,msaicerr}`、`test/`、`scripts/` 等已有目录，**只对新增目录/新 src 子包生效**，存量整改后再逐个摘掉 exclude。

#### 4.3.2 核心流程

`git commit` 时钩子链依次执行：

```text
1. clang-format   C/C++ 文件按 .clang-format 格式化（自动改写，需重新 git add）
                  exclude: experiment/、src/{hccl_test,msaicerr,msprof,third_party}/、test/
2. oat-check      本地钩子，跑 scripts/oat_check.sh（见 4.2）
3. ruff           Python 格式+基础 lint，--fix 自动修；不启用 ruff-format
                  exclude: .claude/、cmake/、experiment/、scripts/、skills/、src/{asys,msaicerr}/、test/
4. pylint         --disable=C,R,import-error,no-name-in-module,wrong-import-order，行宽 120
                  exclude 同 ruff
5. incremental-codecheck  本地钩子，跑 scripts/incremental_codecheck.py（见 4.4）
                          隔离环境额外装 ruff==0.9.9；types: [python]；require_serial: true
6. bandit         Python 安全检查，-ll 只报 medium 及以上；exclude 同 ruff
7. codespell      拼写检查，跳过二进制产物，ignore-words-list 收录项目专有词
```

任何一个钩子非 0 退出都会中断提交。

#### 4.3.3 源码精读

clang-format 钩子及其存量目录屏蔽说明见 [.pre-commit-config.yaml:L6-L25](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L6-L25)：注释写得很清楚——流水线会对改动命中的 C/C++ 文件跑 clang-format，但存量代码不符合 `.clang-format` 风格，「一改到就被卡」，因此先屏蔽四大存量 src 目录。

OAT 本地钩子的接线方式见 [.pre-commit-config.yaml:L27-L37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L27-L37)：`repo: local` + `language: system` 表示不拉远程钩子仓，直接以本机 `bash scripts/oat_check.sh` 为入口，`pass_filenames: true` 把 staged 文件名作为参数传进去（正好对接 4.2 中脚本的手动传参分支）。

ruff 钩子刻意不启用 ruff-format 的原因见 [.pre-commit-config.yaml:L39-L47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L39-L47)：ruff-format 会重排被碰到的**整个文件**，把不属于本次改动的存量代码写进 diff——既违反「精准修改」原则，又让云端增量 codecheck 把重排行当新增行误报。

pylint 的版本与参数裁剪见 [.pre-commit-config.yaml:L64-L75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L64-L75)：pylint 隔离环境不装项目依赖，`import-error`/`no-name-in-module` 必关；C/R 类是「与华为规范无关的命名/重构噪声」也一并关闭。

增量 codecheck 钩子见 [.pre-commit-config.yaml:L92-L106](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L92-L106)：注释解释了它存在的理由——上面的 pylint 整文件检查且关掉了 C/R，无法对行宽/超大函数/staticmethod 预警；此钩子补齐这一层。`require_serial: true` 保证串行执行（钩子内部自己按文件循环），`additional_dependencies: [ruff==0.9.9]` 让 pre-commit 在隔离环境里装好 ruff。

codespell 的专有词豁免表见 [.pre-commit-config.yaml:L149-L158](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/.pre-commit-config.yaml#L149-L158)：`cann,oam,msprof,aicerr,hccl,tbe,...` 都是本项目专有缩写，不进字典；后续遇新误判词追加即可。

#### 4.3.4 代码实践

1. **实践目标**：装好 pre-commit，触发一次完整钩子链，记录每个钩子的行为。
2. **操作步骤**：
   - `pip3 install "pre-commit>=3.2.0"`，然后在仓库根执行 `pre-commit install`；
   - 挑一个**未被 exclude 屏蔽**的文件做小改动（例如在 `README.md` 末尾加一个空行，或新建 `src/mytest/foo.py`），`git add` 后 `git commit -m "test: trigger hooks"`；
   - 首次运行 pre-commit 会克隆各钩子仓并构建隔离环境，耗时较长属正常现象；
   - 观察完成后 `git reset --soft HEAD^`（或放弃提交）恢复现场。
3. **需要观察的现象**：终端逐个打印钩子名与 pass/fail 状态；clang-format/ruff 若自动修复了文件，提交会被中断并提示重新 `git add`。
4. **预期结果**：得到一张「钩子名 → 检查对象 → 本次是否触发 → 结果」的记录表。若在 `src/asys/` 下改 Python 文件，ruff/pylint/bandit/incremental-codecheck 四个钩子会因 exclude 显示 skipped，而 OAT 和 codespell 仍然生效——这正好验证了存量豁免策略。**待本地验证**（各钩子首次安装可能因网络失败，失败信息也应记录）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 pylint 钩子要 `--disable=C,R`？
**答案**：pylint 隔离环境不装项目依赖，import-error/no-name-in-module 必关（否则满屏误报）；C（convention）/R（refactor）类是命名/重构建议，与云端华为 codecheck 规则无关，属于纯噪声。原则是「本地不比云端更严」。

**练习 2**：如果把 `src/msaicerr/` 从 ruff 的 exclude 中摘掉，会发生什么？
**答案**：msaicerr 的存量 Python 代码（大量 print、超长行等）会立刻命中规则，任何碰到该目录的提交都被 ruff/pylint/bandit 卡住——这正是 exclude 注释里「存量目录整改后再逐个摘除」要防范的场景。摘除前必须先完成该目录的存量整改。

**练习 3**：`require_serial: true` 对 incremental-codecheck 有什么意义？
**答案**：强制钩子串行执行。该钩子内部自己按文件循环跑 ruff 并汇总命中结果，并行多进程反而会争抢资源、打乱输出顺序。

### 4.4 增量代码检查：scripts/incremental_codecheck.py

#### 4.4.1 概念说明

这是本仓最有「工程巧思」的一个脚本。它解决的问题：云端 codecheck 是**增量**的（只查 PR 改动的行），但本地 ruff/pylint 对整文件检查——本仓存量遍地超长行/超大函数/print，整文件放开会被存量违规淹没，「比云端更严、堵死提交」。脚本的解法：

> **ruff 全量检查 → 只保留命中【本次 staged 改动行】的告警。**

这样既在 push 前预警自己引入的违规，又不被存量违规阻塞，在本地精确复现云端增量行为。

#### 4.4.2 核心流程

```text
入口 main(argv)：pre-commit 传入 staged 文件名列表
  ├─ 过滤出 .py 文件，无则直接返回 0
  ├─ 对每个文件：
  │    ├─ staged_added_lines(path)
  │    │    git diff --cached --unified=0 解析 @@ hunk 头
  │    │    逐行扫描：+ 开头（非 +++）→ 记录行号；其余上下文行 → 行号 +1
  │    │    返回「新增/修改行号集合」
  │    ├─ ruff_msgs(path)
  │    │    sys.executable -m ruff check --preview --select=E501,T201,S607,
  │    │      PLR0915,PLR6301,PLR1722 --line-length=120 --output-format=concise
  │    │    解析 concise 输出 "path:line:col: CODE message" → [(行号, 文本)]
  │    └─ 两者求交集：告警行号 ∈ 改动行号集合 → 记为命中
  ├─ 有命中 → 逐条 warning 打印 + 返回 1（阻断提交）
  └─ 无命中 → 返回 0
```

规则集与云端华为规范的映射（脚本 docstring 中给出）：

| ruff 规则 | 含义 | 对应云端规则 |
|-----------|------|--------------|
| E501 | line-too-long | G.FMT.02 行宽不超过 120 |
| PLR0915 | too-many-statements | 超大函数 |
| PLR6301 | no-self-use（preview） | G.CLS.07 应为 staticmethod/classmethod |
| T201 | print | G.LOG.02 使用 logging 而非 print |
| S607 | start-process-partial-path | G.EDV.05 调外部程序用绝对路径 |
| PLR1722 | sys-exit-alias | G.ERR.11 相关（避免裸 sys.exit/exit） |

#### 4.4.3 源码精读

脚本 docstring 完整陈述了设计动机与规则映射，见 [scripts/incremental_codecheck.py:L18-L35](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py#L18-L35)：注意最后一句——重复代码 R0801 是跨文件块级检查，ruff 不查；华为其他专有规则 ruff 无对应时「仍以云端结果为准」；新增可查规则往 `RULES` 追加即可。

`staged_added_lines()` 解析 staged diff 收集新增行号，见 [scripts/incremental_codecheck.py:L48-L74](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py#L48-L74)：从 `@@ -a,b +c,d @@` hunk 头取新文件起始行号，`+` 开头的行记入集合、上下文行推进行号计数；用 `shutil.which("git")` 取绝对路径调 git，本身就是对 G.EDV.05 规则的遵守。

`ruff_msgs()` 的工具错误兜底判断见 [scripts/incremental_codecheck.py:L96-L106](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py#L96-L106)：ruff 退出码 0/1 是正常结果，≥2 是工具错误；但 `python -m ruff` 在模块缺失时也可能返回 1——若按「有诊断」解析空 stdout 会**静默放行**，所以用「stdout 为空且 stderr 非空」二次兜底，判为工具错误时直接 raise，宁可门禁报错也不静默放行。这与 oat_check.sh 的「失败放行」策略形成有趣对比：OAT 失败放行是怕堵死所有人，这里失败报错是怕静默漏检自己引入的违规。

主流程的「交集过滤」见 [scripts/incremental_codecheck.py:L116-L134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py#L116-L134)：`if lineno in added` 一行就是整个「增量」语义的落点；命中则逐条 warning 并返回 1，最后附注「仅检查你改动的行，不含存量违规」。

#### 4.4.4 代码实践

1. **实践目标**：手动执行一次增量检查，并制造一次「自己引入违规被抓获」的体验。
2. **操作步骤**：
   - 准备：`pip3 install ruff==0.9.9`（与钩子的 additional_dependencies 一致）；
   - 选一个未被 exclude 屏蔽的 Python 文件位置（如新建 `src/mycheck_demo/demo.py`），写入 10 行左右代码，其中**故意加一行**超过 120 列的注释和一句 `print("hello")`；
   - `git add src/mycheck_demo/demo.py`；
   - 手动执行：`python3 scripts/incremental_codecheck.py src/mycheck_demo/demo.py`；
   - 再做对照实验：把 demo.py 改成完全合规的代码，重新 add 后再跑一次；
   - 清理：`rm -rf src/mycheck_demo && git reset`。
3. **需要观察的现象**：第一次运行输出 `[incremental-codecheck] 本次改动行命中云端规则，请修复：` 及形如 `src/mycheck_demo/demo.py:3: ... E501 line-too-long` 与 `T201` 的告警，退出码 1；第二次运行无输出、退出码 0。
4. **预期结果**：只有**你新写的行**上的违规被报告。可以再进一步：在 demo.py 里 `git add` 后又改动未 add 的行，验证未 staged 的违规不会被报（增量以 staged diff 为准）。**待本地验证**（E501 的具体列号以本地输出为准）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ruff_msgs()` 用 `sys.executable -m ruff` 而不是直接调 `ruff` 命令？
**答案**：`sys.executable` 是绝对路径，不依赖 PATH（这本身就满足 G.EDV.05「外部程序用绝对路径」）；且 `python -m` 方式保证用的是当前解释器环境里的 ruff，避免多环境错配。

**练习 2**：`--preview` 参数为什么必须加？加了会不会带来其他 preview 规则的噪声？
**答案**：`PLR6301`（no-self-use）属于 ruff 的 preview 规则，不加 `--preview` 不会生效；但配合 `--select` 显式限定规则列表后，只报选中的规则，不会引入其他 preview 规则噪声（脚本注释注明「已实测」）。

**练习 3**：如果一个告警出现在你改动的文件里、但落在你没有改过的行上，这个脚本会怎么处理？这正确吗？
**答案**：不会报告——`if lineno in added` 过滤掉了。这是正确的设计：该行违规属于存量问题，云端增量 codecheck 同样不查它；本地如果报了，就是「比云端更严」，会堵死提交。

## 5. 综合实践

**任务：完成一次「门禁全绿」的模拟贡献。**

1. 在仓库里新建目录 `src/check_demo/`，添加一个 Python 文件 `demo.py`，内容包括：模块级 docstring、完整 Apache 2.0 License 头（照抄 [scripts/incremental_codecheck.py:L2-L17](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/incremental_codecheck.py#L2-L17) 的头部格式）、一个用 `logging` 而非 print 的小函数（用 `staticmethod` 修饰）。
2. `pip3 install "pre-commit>=3.2.0" "oat-py>=1.0.1" "ruff==0.9.9"` 并 `pre-commit install`。
3. `git add src/check_demo/demo.py && git commit -m "demo: pass all gates"`。
4. 对照记录 6 个钩子（clang-format 对 .py 不触发，其余逐个）的输出：OAT 是否认可你的 License 头（对照 OAT.xml 的 licensetext）？ruff/pylint/bandit 是否有告警？incremental-codecheck 是否通过？codespell 是否有误判词？
5. 迭代修复直到提交成功，最后整理成一张「钩子 → 首次结果 → 修复动作 → 最终结果」表格；完成后 `git reset` 清理，不产生真实提交。
6. 无本地环境时，可改为纸面推演：对 demo.py 逐条核对 OAT.xml 策略与 RULES 六条规则，写出预期结论（标注「待本地验证」）。

## 6. 本讲小结

- oam-tools 的本地工程门禁由 `.pre-commit-config.yaml` 统一编排 6 个钩子：clang-format、OAT、ruff、pylint、incremental-codecheck、bandit、codespell，任一失败即阻断提交。
- OAT 检查分两层：`OAT.xml` 定义 License/版权策略与文件豁免（yaml/csv/xml 等免检 License 头），`scripts/oat_check.sh` 只扫 staged 的 ACM 文件、只对 Invalid File Type 与 License Header Invalid 两类**确凿问题**阻断，工具自身故障一律放行。
- 钩子链大量使用 `exclude` 做「存量豁免」：新规则只对新增目录生效，存量目录整改后再摘除；版本锁定（pylint 3.3.9、bandit 1.8.6）源于 CI 环境 Python 3.9 的兼容约束。
- `incremental_codecheck.py` 用「ruff 全量检查 + 告警行号 ∈ staged 改动行号集合」在本地复现云端增量 codecheck，六条 ruff 规则一一映射华为云端规范（E501→G.FMT.02、T201→G.LOG.02 等）。
- 一次合格贡献 = CLA + Issue 先行（非简单修复）+ 本地钩子全绿 + 云端 CI 通过 + 按模板写 PR。

## 7. 下一步学习建议

- 下一讲 u6-l3「打包与安装升级：.run 包生命周期」将进入发版链路：`scripts/package` 如何打出 .run 包、install/upgrade/uninstall 三组 ST 如何验证包行为。
- 建议继续阅读：`scripts/run_tests.sh`（u6-l1 已讲，与本讲门禁共同构成「本地自证」体系）、gitcode.com/cann/community 的 PR 模板与门禁说明，以及 `.clang-format`（C/C++ 钩子的规则来源）。
- 若想深入增量检查，可尝试给 `scripts/incremental_codecheck.py` 的 `RULES` 追加一条 ruff 规则（如 `SIM101`），并按其 docstring 的指引在本地验证后再提 PR——这本身就是一次完整的贡献演练。
