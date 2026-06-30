# 贡献流程与 CI

## 1. 本讲目标

本讲是 U16（验证方法学与工程实践）的最后一篇，也是整本手册的收尾。前面 u1-l4 讲过「怎么用 `make` 跑编译/仿真/综合」，u16-l3 讲过「时序、流水线与多 EDA 工具兼容的设计哲学」。本讲把视角从「使用者」切换到「贡献者」：**如果你要给 pulp-platform/axi 提一个 PR，你的改动必须跨过哪些门槛？这些门槛由谁、用什么工具、在哪个 target 上检查？**

学完本讲你应当能够：

- 复述 `CONTRIBUTING.md` 对模块命名、端口风格、`_intf` 变体的硬性要求，并知道在哪里查协作流程。
- 用「Keep a Changelog + 语义化版本」读懂 `CHANGELOG.md` 与 `VERSION`，知道一条 PR 该如何登记改动。
- 画出本库「GitLab 内部 CI + GitHub Actions 公开 CI」的双平台架构，以及二者如何通过一个轮询 Action 衔接。
- 说出一次 PR 必须通过的**四类质量门**（编译、仿真、lint、综合），每类对应的工具、Bender target 与判据。
- 理解 GitLab CI 里 `changes: compare_to: 'refs/heads/master'` 这一设计的用意。

## 2. 前置知识

本讲默认你已经掌握：

- **Bender target 的概念**（u1-l2、u1-l4）：`rtl` / `test` / `simulation` / `synthesis` / `synth_test` 是给源码分组、让不同工具只编译所需文件的「开关」。
- **测试台命名约定与日志判据**（u1-l4、u3-l3）：`tb_<dut>.sv` 命名、`Errors: 0,` 统计行、定向随机验证与回归。
- **多 EDA 工具回归的思想**（u16-l3）：同一份 RTL 用 vsim、Verilator、Synopsys DC 等多个工具分别检查，分工补盲。
- **axi_synth_bench 的作用**（u1-l4）：一个用 `for` 循环实例化大量宽度/参数配置的综合用例，供 lint 与 elaborate 快速回归可综合性。

如果你对「为什么要 lint」「为什么要 elaborate」「target 是什么」还有疑问，建议先回看 u1-l4 与 u16-l3。

## 3. 本讲源码地图

本讲涉及的文件几乎都不是 RTL，而是工程治理文件：

| 文件 | 作用 |
| --- | --- |
| `CONTRIBUTING.md` | 贡献者必须遵守的编码风格与协作规范 |
| `CHANGELOG.md` | 按「Keep a Changelog」记录每个版本的 Added/Fixed/Changed |
| `VERSION` | 单行纯文本，当前版本号 |
| `Bender.yml` | 定义源码分层与各 target 包含哪些文件（CI 检查范围的源头） |
| `.gitlab-ci.yml` | **内部** GitLab CI：编译、仿真回归、综合、verilator lint |
| `.github/workflows/gitlab-ci.yml` | GitHub Action：轮询内部 GitLab CI 结果 |
| `.github/workflows/lint.yml` | GitHub Action：公开跑 Verilator lint |
| `.github/workflows/elab.yml` | GitHub Action：公开跑 yosys-slang elaboration |
| `.github/workflows/doc.yml` | GitHub Action：用 morty 构建文档并部署到 gh-pages |
| `scripts/*.sh` | 各检查门真正调用的脚本（工具调用的落点） |
| `Makefile` | 把上述脚本包成 `compile.log` / `sim-<tb>.log` / `elab.log` 目标 |

阅读建议：先看 `CONTRIBUTING.md` 与 `CHANGELOG.md`（人读的契约），再看 `.gitlab-ci.yml`（机器执行的契约），最后用 `scripts/*.sh` 把每个 CI job 落到具体命令。

## 4. 核心概念与源码讲解

### 4.1 贡献规范与编码风格（CONTRIBUTING）

#### 4.1.1 概念说明

一个由多人协作、被下游大量项目依赖的 IP 库，必须在合入门槛上做约束，否则风格漂移和接口不一致会迅速让代码库失控。`CONTRIBUTING.md` 就是本库的「入门口岸」：它不长（只有 27 行），但每一条都是合入前会被 review 卡住的硬规则。它由两部分组成——**Coding Style**（代码风格）与 **Collaboration Guidelines**（协作流程）。

#### 4.1.2 核心流程

提交一个符合规范的模块/改动的流程：

1. 命名：所有模块名以 `axi_` 开头。
2. 端口：面向用户的模块用 SystemVerilog `struct` 作为 AXI 端口，struct 类型作为 `parameter` 传入，字段与本库 `typedef` 宏一致。
3. 接口变体（可选）：若提供接口版，命名加 `_intf` 后缀，**只做接线、不实现功能**，参数名 `ALL_CAPS`。
4. 协作：遵循 pulp-platform 的协作指南（分支、commit、PR 礼仪）。
5. 登记：在 `CHANGELOG.md` 的 `Unreleased` 段写下改动。
6. 自检：本地或 CI 跑通四类质量门。

#### 4.1.3 源码精读

**模块必须以 `axi_` 开头**——这是全库最显眼的命名铁律，也是 `tb_<dut>.sv` 命名约定能成立的前提：

[CONTRIBUTING.md:9-9](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CONTRIBUTING.md#L9-L9) — 规定所有模块名以 `axi_` 起首。

**用户面模块用 struct 端口**——这条解释了为什么本库从 v0.8.0 起把所有模块从 interface 改成 struct 端口（见 CHANGELOG 0.8.0），也让模块能在不支持 interface 的工具里使用：

[CONTRIBUTING.md:11-14](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CONTRIBUTING.md#L11-L14) — 要求 AXI 端口为 `struct`、类型作为 `parameter`、字段与 `typedef` 宏对齐。

**`_intf` 变体只接线不干活**——这条是「组合优于配置」哲学在工程纪律上的体现：接口外壳必须零逻辑，所有功能都在 struct 内核里，二者永远等价：

[CONTRIBUTING.md:16-20](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CONTRIBUTING.md#L16-L20) — `_intf` 变体的命名（`_intf` 后缀）、职责（仅接线）与参数风格（`ALL_CAPS`）三条约束。

**协作流程外包给上游指南**——本库不重复发明流程规范，而是指向 pulp-platform 组织级的贡献指南：

[CONTRIBUTING.md:25-26](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CONTRIBUTING.md#L25-L26) — 协作流程遵循 pulp-platform 的 `CONTRIBUTING.md`。

另外，CONTRIBUTING 还引用了 lowRISC 的 SystemVerilog 风格指南作为编码基底：

[CONTRIBUTING.md:5-7](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CONTRIBUTING.md#L5-L7) — 全库 SV 代码须遵循 lowRISC 风格指南。

#### 4.1.4 代码实践

**实践目标**：用 CONTRIBUTING 的三条硬规则去「验收」一个真实模块，体会规则如何落到代码里。

**操作步骤**：

1. 打开 `src/axi_isolate.sv`（或任一用户面模块），找到它的端口列表。
2. 核对三条：① 模块名是否以 `axi_` 开头？② AXI 端口是否是 `struct`（如 `axi_req_t` / `axi_resp_t`）且类型作为 `parameter`？③ 是否存在一个 `axi_isolate_intf` 只做接线的变体？
3. 打开 `src/axi_isolate_intf.sv`，确认它的函数体里**只有 `AXI_ASSIGN` 类宏的接线，没有任何 `always_ff` / 组合逻辑**。

**需要观察的现象**：`_intf` 变体的例化语句把接口信号搬到 struct 上、再例化 struct 内核，自身不产生任何寄存器或门。

**预期结果**：你能用一句话向同事说明「为什么改 `axi_isolate` 的功能时，`axi_isolate_intf` 几乎不用动」——因为接线是机械的、功能只在内核。

#### 4.1.5 小练习与答案

**Q1**：如果有人提交了一个新模块叫 `my_fifo_bridge`（不带 `axi_` 前缀），根据 CONTRIBUTING 会被卡吗？
**答**：会。第 9 行规定所有模块名必须以 `axi_` 开头，应改名为 `axi_fifo_bridge`。

**Q2**：为什么 `_intf` 变体被要求「不实现任何功能，只把接口连到 struct 端口」？
**答**：为了保证接口版与 struct 版**功能完全等价**。如果 `_intf` 里塞了逻辑，就会出现「同一模块两个行为」，违背「组合优于配置」与单一事实来源原则，也使 review 与回归无法用一套用例覆盖两种风格。

**Q3**：CONTRIBUTING 里「struct 的字段必须对应 typedef 宏定义的那些」对应到本手册哪一讲？
**答**：对应 u2-l4（typedef/assign/port 宏体系）。`include/axi/typedef.svh` 的 `AXI_TYPEDEF_*` 宏就是这里说的「我们的 typedef 宏」。

### 4.2 版本管理与变更日志（CHANGELOG / VERSION）

#### 4.2.1 概念说明

库的版本号和变更记录是下游用户「能不能安全升级」的唯一依据。本库同时维护两个文件：`VERSION`（一个单行纯文本，给机器和 Bender/FuseSoC 读）和 `CHANGELOG.md`（给人读的版本史）。二者遵循两套业界约定：**语义化版本（SemVer）** 决定版本号怎么 bump，**Keep a Changelog** 决定变更记录怎么写。理解这两套约定，是你给 PR 写对 changelog 条目的前提。

#### 4.2.2 核心流程

语义化版本把版本号拆成 `主版本.次版本.修订号`（`MAJOR.MINOR.PATCH`），对应 AXI 库的 `0.39.10` 这类形态：

\[ \text{version} = \text{MAJOR}.\text{MINOR}.\text{PATCH} \]

- 不兼容的 API 改动 → 升 MAJOR（本库处于 `0.x` 阶段，次版本号即承担「可能不兼容」语义）。
- 向后兼容的新功能 → 升 MINOR。
- 向后兼容的缺陷修复 → 升 PATCH。

Keep a Changelog 则规定每个版本段用 `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security` 这一组固定小标题，并维护一个顶部的 `Unreleased`（未发布）段收集「已合入但还没发版」的改动。

一次 PR 的登记流程：

1. 在 `CHANGELOG.md` 顶部的 `Unreleased` 段下，按改动性质选 `Added` / `Fixed` / `Changed`。
2. 写一行说明，带上 PR 号（如 `#424`）。
3. 发版时把 `Unreleased` 改成带日期的版本号，同时更新 `VERSION`。

#### 4.2.3 源码精读

**两套约定的声明**——CHANGELOG 开头就声明了它遵循的两个规范：

[CHANGELOG.md:4-5](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CHANGELOG.md#L4-L5) — 声明格式基于 Keep a Changelog、版本遵循 SemVer。

**`Unreleased` 段是 PR 写改动的落点**——当前 HEAD 处它是空的，说明 0.39.10 之后还没有已合入但未发版的改动：

[CHANGELOG.md:8-8](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CHANGELOG.md#L8-L8) — `## Unreleased` 段，PR 在此登记。

**最近一个版本与 CI 演进的痕迹**——0.39.10（2026-06-19）里能直接看到本讲关心的两条 CI 改动：

[CHANGELOG.md:10-16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CHANGELOG.md#L10-L16) — 0.39.10 段，其中 `Added` 含「GitHub Action for Verilator lint and yosys-slang elaboration. #414」。

[CHANGELOG.md:30-31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CHANGELOG.md#L30-L31) — `Changed` 段记录「Replace memora with GitLab-native artifacts and rules in CI. #424」，这正是 4.3 节要讲的 `changes: compare_to` 设计的来源。

**破坏性变更会被显式标出**——遇到不兼容改动，CHANGELOG 会单列 `### Breaking Changes` 并在文末强调向后兼容性，例如 0.39.0：

[CHANGELOG.md:224-228](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/CHANGELOG.md#L224-L228) — 0.39.0 的 Breaking Changes（`axi_demux` 删 `FallThrough`、`xbar_cfg_t` 加 `PipelineStages` 等）。

**VERSION 与 CHANGELOG 必须同步**——`VERSION` 是单行机器可读版本号，发版时与 CHANGELOG 的版本标题一同更新：

[VERSION:1-1](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/VERSION#L1-L1) — 当前版本 `0.39.10`，与 CHANGELOG 最新段一致。

#### 4.2.4 代码实践

**实践目标**：用 CHANGELOG 追溯一个 CI 行为变更的来龙去脉，练熟「从现象到 PR 号」的检索。

**操作步骤**：

1. 在 `CHANGELOG.md` 中搜索 `Verilator`，找到最早把 Verilator lint 引入 CI 的版本（提示：0.39.8 的 `#378`）。
2. 再找到 0.39.10 的 `#414`，对比两次引入的措辞差异（前者「Add linting pass to CI elaborating with Verilator」，后者「GitHub Action for Verilator lint and yosys-slang elaboration」）。
3. 搜索 `memora`，确认它在 0.39.10 被「GitLab-native artifacts and rules」取代（`#424`）。

**需要观察的现象**：同一件事（lint）在多个版本里逐步演进——先在内部 CI 引入，再把其中开源友好的部分搬到 GitHub Actions。

**预期结果**：你能复述「Verilator lint 经历了 0.39.8 引入、0.39.10 拆出 GitHub Action 并新增 yosys-slang」这条演进线。如果手头没有 grep 工具，可人工滚动 `CHANGELOG.md` 查找，结论一致。

#### 4.2.5 小练习与答案

**Q1**：`0.39.10` 比 `0.39.9` 升了哪一位？这说明 0.39.10 的改动按 SemVer 属于哪一类？
**答**：升了 PATCH 位（`.10` vs `.9`）。属于「向后兼容的缺陷修复 / 小幅增强」，没有破坏性 API 变更。

**Q2**：你提了一个 PR 修复了 `axi_demux` 的一个死锁。CHANGELOG 该写到哪个段？
**答**：写到顶部 `## Unreleased` 下的 `### Fixed` 段，一行说明 + PR 号。等下一个版本发布时再被改写为带日期的版本标题。

**Q3**：为什么 0.39.0 的 CHANGELOG 末尾要单列 `### Breaking Changes`？
**答**：因为 0.39.0 删除/改变了对外接口（如 `axi_demux` 删 `FallThrough` 参数、`xbar_cfg_t` 加字段），下游必须改代码才能升级。显式标出破坏性变更是 Keep a Changelog 与 SemVer 在 `0.x` 阶段表达「不兼容」的约定方式。

### 4.3 CI 总体架构：双平台与触发规则

#### 4.3.1 概念说明

本库的 CI 横跨**两个平台**：内部 **GitLab**（`iis-git.ee.ethz.ch`，跑需要商业 EDA 许可与 IIS 机器的重活）和公开的 **GitHub Actions**（跑开源工具，结果对社区可见）。二者不是重复，而是分工：

- **GitLab CI**（`.gitlab-ci.yml`）：编译（Questa vsim）、整库仿真回归、综合（Synopsys DC）、Verilator lint、FuseSoC+XSM。这是「重型 + 商业工具」检查的真正落点。
- **GitHub Actions**（`.github/workflows/*.yml`）：四条工作流——① 一个「桥」轮询 GitLab CI 结果回填到 GitHub；② Verilator lint；③ yosys-slang elaboration；④ 文档构建与部署。

之所以要一个「桥」Action，是因为商业工具跑在防火墙后的内部 GitLab，GitHub 上看不到结果；于是用一个公开 Action 去轮询内部 GitLab 的 pipeline 状态，再把通过/失败回填到 PR 检查里。fork 仓库因为没有访问内部 GitLab 的 secret，会自动跳过这个桥。

#### 4.3.2 核心流程

一次 push / PR 触发后的 CI 流程：

```
push 到 GitHub
   │
   ├──► GitHub Actions（公开）
   │      ├─ lint.yml      → Verilator lint（run_verilator.sh）
   │      ├─ elab.yml      → yosys-slang elaboration（run_yosys_slang.sh）
   │      ├─ doc.yml       → 文档构建（morty），master/tag 部署 gh-pages
   │      └─ gitlab-ci.yml → 轮询内部 GitLab pipeline 结果（桥）
   │              │
   │              ▼
   └──► 内部 GitLab CI（iis-git.ee.ethz.ch，镜像仓库）
          ├─ vsim（build）        → 编译仿真库，产物 build/work 作为 artifact
          ├─ synopsys_dc（build） → Synopsys DC elaborate
          ├─ verilator_lint       → Verilator lint
          ├─ fuse_xsim            → FuseSoC+XSM（允许失败）
          └─ run_vsim × N（test） → 每模块仿真回归，needs: vsim
```

**触发规则的关键设计**：GitLab CI 不用「上一条 commit」判断要不要跑某个 job，而是用「整条分支相对 master 的累计 diff」。文件顶部的大段注释专门解释了这一点。

#### 4.3.3 源码精读

**变量区钉死工具版本**——三条变量把商业工具命令和版本写死，保证可复现：

[.gitlab-ci.yml:1-4](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L1-L4) — `SYNOPSYS_DC`、`VSIM`、`VERILATOR` 三条工具变量（含版本与封装命令）。

**触发规则的核心设计注释**——这是本库 CI 最值得读的一段注释，解释了为什么用 `compare_to: 'refs/heads/master'`：

[.gitlab-ci.yml:9-21](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L9-L21) — 注释说明：`changes` 规则统一对比 `master`，使触发反映整条分支的累计 diff，防止「后一条无关 commit 把前一条失败藏在绿色 pipeline 后」。

这段注释还点明了它的历史：它取代了原先 `memora` 的内容寻址缓存（对应 CHANGELOG `#424`）。代价是依赖人工维护的依赖列表——漏写一条就会让某个测试静默不跑。

**`compare_to: master` 的实际写法**——以构建期公共规则为例，每条 `changes` 都显式 `compare_to: 'refs/heads/master'`：

[.gitlab-ci.yml:31-39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L31-L39) — `&build_common_change_rule` 锚点：改动 `.gitlab-ci.yml` / `Bender.yml` / `include/**` / `src/**` / `test/**` 时触发 build job。

**GitHub 侧的「桥」**——这条 Action 不跑任何 RTL 检查，只去内部 GitLab 轮询 pipeline 状态：

[.github/workflows/gitlab-ci.yml:10-22](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/gitlab-ci.yml#L10-L22) — 用 `pulp-platform/pulp-actions/gitlab-ci@v2.5.1` 轮询 `iis-git.ee.ethz.ch` 上 `github-mirror/axi` 的内部 pipeline，`poll-count: 1800`。

[.github/workflows/gitlab-ci.yml:16-17](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/gitlab-ci.yml#L16-L17) — fork 或来自 fork 的 PR 因缺 secret 跳过此桥。

**开源侧的三条公开工作流**——它们才是社区在 PR 上直接看到的绿色对勾：

[.github/workflows/lint.yml:14-33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/lint.yml#L14-L33) — `verilator-lint` job：装 Bender 0.30.0、setup-verilator、`bender checkout` 后跑 `scripts/run_verilator.sh`。

[.github/workflows/elab.yml:14-32](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/elab.yml#L14-L32) — `yosys-slang` job：在 `hpretl/iic-osic-tools` 容器里跑 `scripts/run_yosys_slang.sh`。

[.github/workflows/doc.yml:35-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/doc.yml#L35-L41) — 文档构建：用 morty 把 `src/*.sv` 生成文档到 `docs/`。

文档构建用了 morty（pulp-platform 自家的文档生成器），并在 push 到 master 或打 tag 时部署到 `gh-pages` 分支：

[.github/workflows/doc.yml:43-54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.github/workflows/doc.yml#L43-L54) — 仅在 push 到 master 或 `v**` tag 时，用 `JamesIves/github-pages-deploy-action@v4` 部署到 `gh-pages`。

#### 4.3.4 代码实践

**实践目标**：通过阅读配置，画一张「PR 上每个对勾分别来自哪个平台、哪条工作流」的对照表。

**操作步骤**：

1. 浏览 `.github/workflows/` 下四个 yml，记录每条的 `name:` 与触发 `on:`。
2. 打开 `.gitlab-ci.yml`，记录它定义了哪些 job（`vsim` / `synopsys_dc` / `fuse_xsim` / `verilator_lint` / `run_vsim` 矩阵 / 各 `axi_*` 专用 job）。
3. 在一张表里把「检查项 → 平台 → 工作流/job → 工具」四列对齐。

**需要观察的现象**：你会发现 Verilator lint 同时出现在 GitLab（`verilator_lint` job）和 GitHub（`lint.yml`）——这是故意的冗余，让没有内部 GitLab 访问权的社区贡献者也能在公开 PR 上看到 lint 结果。

**预期结果**：得到一张如 4.4 节汇总表那样的对照表。fork 仓库上 `gitlab-ci` 桥会显示「跳过」，其余三条公开工作流仍正常运行。

#### 4.3.5 小练习与答案

**Q1**：为什么 GitHub 上要有一个 `gitlab-ci.yml` 工作流，它自己跑 RTL 检查吗？
**答**：不跑。它是一个「桥」，用 `pulp-actions/gitlab-ci` 去轮询内部 GitLab 上镜像仓库的 pipeline 结果并回填到 GitHub PR 检查。这样商业工具（vsim、DC）在内部跑，结果却能在公开 PR 上可见。

**Q2**：fork 仓库提 PR，哪条检查会自动跳过？为什么？
**答**：`Internal CI`（gitlab-ci 桥）会跳过。因为桥需要 `GITLAB_TOKEN` 访问内部 GitLab，fork 没有这个 secret；条件 `github.repository == 'pulp-platform/axi' && ... head.repo.full_name == github.repository` 会在 fork PR 上为假。

**Q3**：`changes: compare_to: 'refs/heads/master'` 想避免的故障是什么？
**答**：避免「一条分支上先有一个会触发某测试的改动、后跟一条无关改动」时，后者让前者的 CI 触发条件失效，从而用一条绿色 pipeline 掩盖一个本应失败的测试。对比 master 取累计 diff 可让被碰过的文件持续触发其测试，直到合并。

### 4.4 质量检查四件套：编译、仿真、lint、综合

#### 4.4.1 概念说明

无论 CI 架构多复杂，一次 PR 真正要跨过的质量门可以归为**四类**，它们各盯一种缺陷：

- **编译（compile）**：源码能否被仿真器编译通过？盯语法与类型错误。工具：Questa vsim。
- **仿真（simulation）**：编译出来的库跑测试台是否 `Errors: 0`？盯功能正确性。工具：Questa vsim + 定向随机回归。
- **lint**：不跑仿真，只做静态语法/风格/可疑构造检查，且**独立于商业仿真器**，盯工具兼容性与代码异味。工具：Verilator（开源）、yosys-slang（开源前端 elaborate）。
- **综合（synthesis）**：能否被综合工具 elaborate 出来？盯可综合性（不可综合的 `assert`、初始化块等会被打回）。工具：Synopsys DC（商业）、yosys-slang。

这四类刻意用**多家工具**覆盖（u16-l3 讲过的「多工具回归」思想），因为每家工具对 SV 子集的理解不同，一家放过的问题另一家可能抓住。其中 lint 和综合都盯同一个顶层 `axi_synth_bench`，但角度不同。

#### 4.4.2 核心流程

把每个检查门映射到「job → 脚本 → bender target → 顶层/判据」：

| 检查门 | 平台 / job | 脚本 | Bender target | 顶层 / 范围 | 判据 |
| --- | --- | --- | --- | --- | --- |
| 编译 | GitLab `vsim` | `compile_vsim.sh` | `-t test -t rtl` | 全库 + 所有 TB | 编译无 `Error:` |
| 仿真 | GitLab `run_vsim` 矩阵 + 专用 job | `run_vsim.sh` | （用上一步产物） | 每 `tb_<dut>` 多配置 | 日志含 `Errors: 0,` |
| lint（公开） | GitHub `lint.yml` / GitLab `verilator_lint` | `run_verilator.sh` | `-t synthesis -t synth_test` | `axi_synth_bench` | Verilator 退出码 0 |
| 综合-elab（公开） | GitHub `elab.yml` | `run_yosys_slang.sh` | `-t synthesis -t synth_test` | `axi_synth_bench` | yosys 退出码 0 |
| 综合（商业） | GitLab `synopsys_dc` | `synth.sh` | `-t synth_test` | `axi_synth_bench` | 日志无 `error:` |
| 综合（兼容性） | GitLab `fuse_xsim`（允许失败） | 内联 FuseSoC | sim target | FuseSoC 解析 | 允许失败 |

注意三条 target 的差异：编译要「全库 + 测试台」所以用 `-t test -t rtl`；lint 与综合只关心「可综合 RTL 能否被工具吃下」，所以用 `-t synthesis -t synth_test`，顶层统一为 `axi_synth_bench`。

#### 4.4.3 源码精读

**编译：只给 axi_pkg 开严格 lint**——`compile_vsim.sh` 用 awk 给 `axi_pkg` 单独注入 `-lint -pedanticerrors`，因为它是全库根基，最该严格；其余文件不加，以免被依赖项的告警淹没：

[scripts/compile_vsim.sh:21-25](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L21-L25) — `bender script vsim -t test -t rtl` 生成编译脚本。

[scripts/compile_vsim.sh:31-37](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L31-L37) — 仅对 `axi_pkg` 注入 `-lint -pedanticerrors`。

**仿真：种子数组与「Errors: 0」判据**——`run_vsim.sh` 的核心是 `SEEDS` 数组与逐种子 `grep "Errors: 0,"`：

[scripts/run_vsim.sh:28-34](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L28-L34) — 默认 `SEEDS=(0)`（回归基线），每个种子跑一次 `vsim -sv_seed`，靠 `grep "Errors: 0,"` 判通过。

`--random-seed` 这个标志**不带参数**——它只是把 `"random"` 追加进 `SEEDS`，真正选测试的是跟在后面的位置参数。CI 里 `run_vsim.sh --random-seed $TEST_MODULE` 的读法是「追加一个随机种子 + 跑 `$TEST_MODULE` 这个测试」，而非「用模块名当种子」：

[scripts/run_vsim.sh:253-256](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L253-L256) — `--random-seed)` 分支只做 `SEEDS+=(random)` 与一次 `shift`，不消费下一个 token。

于是 CI 里每个测试最终用 `(0 random)` 两个种子各跑一遍——`0` 保证回归一致，`random` 扩展覆盖（承接 u16-l1）：

[.gitlab-ci.yml:80-81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L80-L81) — `.run_vsim` 模板：`needs: [vsim]` 复用编译产物，`run_vsim.sh --random-seed $TEST_MODULE`。

**仿真矩阵：叶子模块批量跑，共享子模块单独列依赖**——只依赖自身源文件的「叶子」模块进 `matrix` 一次性跑；依赖共享子模块的（如 `axi_cdc` 依赖 `axi_cdc_dst/src`）单列 job 并追加依赖路径，免得改了共享子模块却漏跑：

[.gitlab-ci.yml:105-121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L105-L121) — `run_vsim` 矩阵列出 12 个叶子模块。

[.gitlab-ci.yml:125-136](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L125-L136) — `axi_cdc` 专用 job，额外追加 `axi_cdc_dst.sv` / `axi_cdc_src.sv` 到触发路径。

**lint：Verilator 静态检查 axi_synth_bench**——`run_verilator.sh` 不仿真，只 `--lint-only`，并 `-Wno-fatal` 把告警降级为非致命，让 lint 聚焦「能否被工具解析」：

[scripts/run_verilator.sh:24-29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L24-L29) — `bender script verilator -t synthesis -t synth_test`，顶层 `axi_synth_bench`，`--lint-only --timing`。

**综合-elab：yosys + slang 前端**——`run_yosys_slang.sh` 用 yosys 加载 slang 前端插件读入并 elaborate 同一个顶层，`-Werror` 把告警也当错：

[scripts/run_yosys_slang.sh:21-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_yosys_slang.sh#L21-L23) — `bender script flist-plus -t synthesis -t synth_test`，`yosys -m slang ... --top axi_synth_bench`。

**综合（商业）：Synopsys DC elaborate**——`synth.sh` 生成 tcl 让 DC elaborate `axi_synth_bench`，靠 grep 判错；注意它只 elaborate 不做完整综合（不映射工艺、不报面积时序），目的是快速回归可综合性：

[scripts/synth.sh:22-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L22-L28) — 生成 `elaborate axi_synth_bench` 的 tcl，跑 DC 后 `grep -i "error:"` 判错。

**synth_test target 只含综合用例**——Bender 里 `synth_test` 只追加 `test/axi_synth_bench.sv`，这就是 lint/综合顶层的来源：

[Bender.yml:103-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L103-L105) — `synth_test` target 追加 `axi_synth_bench.sv`。

**Makefile 是这些脚本的本地入口**——本地复现 CI 的最短路径是 `make`，它把脚本包成日志目标，并用 grep 兜底判错：

[Makefile:50-59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L50-L59) — `help` 目标列出 `elab.log` / `compile.log` / `sim-#TB#.log` / `sim_all` / `clean`。

[Makefile:82-85](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L82-L85) — `sim-%.log` 规则：依赖 `compile.log`，跑 `run_vsim.sh --random-seed $*`，`grep` 查 `Error:` / `Fatal:`。

#### 4.4.4 代码实践

**实践目标**：把本讲规格里的实践任务做完——列出一次 PR 必须通过的检查，并标注每项的工具与 target。这是贡献前最实用的一张表。

**操作步骤**：

1. 读 `.gitlab-ci.yml` 与 `.github/workflows/{lint,elab}.yml`，按「编译 / 仿真 / lint / 综合」四类归档每个 job。
2. 对每个 job 追溯它调用的 `scripts/*.sh`，记录其中的 `bender script ... -t <target>` 与顶层模块。
3. 把结果填进一张表（参考 4.4.2 的表格骨架）。
4. 进阶：在本地用 `make help` 看可用目标，挑一个 TB（如 `make sim-axi_lite_regs.log`）尝试复现仿真检查（需要本地装 Questa vsim 与 Bender）。

**需要观察的现象**：

- 编译/仿真用 `-t test -t rtl`（要全库 + TB），lint/综合用 `-t synthesis -t synth_test`（只要可综合 RTL + 综合用例）。
- lint 与综合的顶层都是 `axi_synth_bench`，但 Verilator 是 `--lint-only`（不 elaborate 成网表），DC 与 yosys-slang 是真 elaborate。
- `run_vsim.sh --random-seed $TEST_MODULE` 里 `$TEST_MODULE` 是测试名而非种子值（常见误读）。

**预期结果**：得到类似下表的结论（「本地复现」一列标注是否需要商业许可）：

| 检查 | 工具 | target | 顶层/范围 | 本地复现 |
| --- | --- | --- | --- | --- |
| 编译 | Questa vsim | `test rtl` | 全库 + TB | `make compile.log`（需 vsim） |
| 仿真 | Questa vsim | （复用产物） | `tb_<dut>` 多种子 | `make sim-axi_lite_regs.log`（需 vsim） |
| lint | Verilator | `synthesis synth_test` | `axi_synth_bench` | `scripts/run_verilator.sh`（开源） |
| 综合-elab | yosys-slang | `synthesis synth_test` | `axi_synth_bench` | `scripts/run_yosys_slang.sh`（开源） |
| 综合 | Synopsys DC | `synth_test` | `axi_synth_bench` | `make elab.log`（需 DC） |

如果你本地没有 vsim/DC，可只跑两条开源检查（Verilator、yosys-slang）验证管线；若工具也缺，则标注「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：为什么编译用 `-t test -t rtl`，而 lint/综合用 `-t synthesis -t synth_test`？
**答**：编译要为仿真准备全库 RTL 加所有测试台（`test` + `rtl`）；lint/综合只关心可综合代码能否被工具解析，不需要测试台，但要一个能实例化多种配置的顶层 `axi_synth_bench`（`synth_test`），且用 `synthesis` 表明「综合语境」。

**Q2**：CI 里 `run_vsim.sh --random-seed $TEST_MODULE`，`$TEST_MODULE` 是随机种子吗？
**答**：不是。`--random-seed` 是无参标志，仅把 `"random"` 追加进 `SEEDS`；`$TEST_MODULE` 是位置参数，决定跑哪个测试。最终用 `(0 random)` 两个种子各跑一次该测试。

**Q3**：`synth.sh` 跑的是「完整综合」吗？它到底检查了什么？
**答**：不是完整综合（不映射工艺、不报时序面积）。它只做 `elaborate axi_synth_bench`，即把 RTL 展开成综合工具内部的中间表示，目的是快速发现「不可综合写法、工具不支持的构造、elaborate 期断言失败」这类问题——对应 u16-l3 讲的「elaborate 可溶性回归」。

## 5. 综合实践

**任务**：扮演一个准备提 PR 的贡献者，把本讲四节串成一份「贡献前自查清单」并实际跑通你能跑的部分。

请完成以下步骤：

1. **规范自查**：挑一个你想「改进」的模块（例如 `axi_throttle`），按 4.1 的三条规则检查它的命名、struct 端口、`_intf` 变体是否符合 CONTRIBUTING；如不符合，写一行修改建议。
2. **登记改动**：在 `CHANGELOG.md` 的 `Unreleased` 段为你的「假想改动」写一条符合 Keep a Changelog 的条目（选对 Added/Fixed/Changed，带 PR 号占位 `#XXX`）。
3. **画 CI 触发图**：列出你的改动会触发哪些 CI job（提示：`src/axi_throttle.sv` 的改动会命中哪些 `changes` 规则？它有没有专用 job？若没有，是否进 `run_vsim` 矩阵或被 `axi_*` 专用 job 覆盖？）。
4. **填四类质量门表**：按 4.4.4 的表格，写出你的 PR 必须通过的每一项检查及其工具与 target。
5. **实跑开源检查**（可选，需本地有 Bender + Verilator）：
   ```bash
   bender checkout
   scripts/run_verilator.sh
   ```
   观察 Verilator 是否以退出码 0 通过；若你装了 yosys-slang，再跑 `scripts/run_yosys_slang.sh`。
6. **本地复现仿真**（可选，需 Questa vsim）：`make compile.log && make sim-axi_throttle.log`，确认日志末尾出现 `Errors: 0,`。

**预期产出**：一份自查清单 + 一张四类质量门对照表 +（若跑了）两条开源检查的通过截图或日志结尾。若本地缺工具，第 5、6 步标注「待本地验证」即可，不要伪造运行结果。

**观察重点**：第 3 步会发现 `axi_throttle` 并不在 `.gitlab-ci.yml` 的 `run_vsim` 矩阵或专用 job 列表里——它没有专属 testbench，改动它主要触发编译、lint、综合这三类，以及（若同时改了依赖它的下游）下游模块的仿真。这正是 4.3 注释里「漏写依赖会让测试静默不跑」的风险点，值得在自查清单里记一笔。

## 6. 本讲小结

- `CONTRIBUTING.md` 用三条硬规则约束贡献：模块名以 `axi_` 开头、用户面模块用 struct 端口（类型作 `parameter`）、`_intf` 变体只接线不实现功能；协作流程指向 pulp-platform 组织级指南。
- 版本管理遵循 SemVer + Keep a Changelog：`VERSION` 是机器读的单行版本号，`CHANGELOG.md` 用 `Unreleased` 段收集 PR 改动，破坏性变更单列 `Breaking Changes`。
- CI 是「GitLab 内部（商业工具重活）+ GitHub Actions（开源工具 + 桥 + 文档）」的双平台架构；一条轮询 Action 把内部 pipeline 结果回填到公开 PR。
- GitLab CI 的触发规则统一 `compare_to: 'refs/heads/master'`，取整条分支累计 diff，防止后一条无关 commit 掩盖前一条失败（取代了旧的 memora 缓存，`#424`）。
- 一次 PR 必须跨过**四类质量门**：编译（vsim，`-t test -t rtl`）、仿真（vsim 多种子，判据 `Errors: 0,`）、lint（Verilator，`-t synthesis -t synth_test`）、综合（Synopsys DC / yosys-slang，顶层 `axi_synth_bench`）；本地入口是 `make`。
- `--random-seed` 是无参标志（追加 `"random"` 种子），`$TEST_MODULE` 是测试名而非种子——这是读 CI 脚本时最常踩的坑。

## 7. 下一步学习建议

至此整本手册的 16 个单元已全部讲完。如果你是贡献者，下一步建议：

- **动手提一个小 PR**：从 `CHANGELOG.md` 里挑一个 `Fixed` 条目（如某 lint 修复），尝试在本地用 `scripts/run_verilator.sh` 与 `make` 复现修复前后差异，走通一次完整贡献流程。
- **深读 axi_synth_bench**：本讲反复提到的综合顶层 `test/axi_synth_bench.sv` 是理解「lint/综合到底检查了哪些配置」的钥匙，建议通读它的 `for` 循环实例化。
- **回看方法学主线**：把 u16-l1（定向随机验证）、u16-l2（总线比较与 dumper）、u16-l3（时序与多 EDA 兼容）与本讲并读，你会得到一张「AXI 库如何保证质量」的完整地图——从随机激励自检，到等价性比对，到多工具静态/综合回归，再到贡献流程与 CI 把关。
- **扩展到上游**：CONTRIBUTING 指向的 pulp-platform 协作指南与 lowRISC 风格指南，以及 `common_cells` / `common_verification` 等依赖库，是进一步参与 pulp-platform 生态的下一层入口。
