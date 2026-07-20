# 贡献流程与发布管理

## 1. 本讲目标

本讲是手册「贡献与工具链」单元的收尾篇。前面几讲教你怎么读懂、怎么测试、怎么生成 psi_common 的组件，本讲反过来回答：**当你想把自己写的组件反哺回这个库时，要遵守哪些规矩；以及维护者如何把这些改动整理成一次发布。**

学完后你应该能够：

- 说出贡献代码必须满足的六条硬性要求，并对照检查清单自查。
- 复述 `develop` / `master` 双分支模型、特性分支命名约定与 Pull Request 评审三步流程。
- 写出符合库约定的 commit 注释（带前缀关键词）。
- 解释 `major.minor.bugfix` 三段式版本号与标签策略的判定规则。
- 理解 GitLab CI 如何用 `###ERROR###` 这个字符串充当回归测试的统一成败判据，把它和自检 TB 串成一条「提交即验证」的链路。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（前序讲义已覆盖）：

- **psi_common 的定位**：PSI 维护的通用、可复用、可综合 VHDL 库，只收录「项目无关、能完全 generic 化」的基础模块（见 u1-l1）。
- **仓库结构**：`hdl/`（源码）、`testbench/`（自检 TB）、`sim/`（回归脚本）、`doc/`（文档）、`scripts/`（工具脚本）、`generators/`（代码生成器）六大目录（见 u1-l2）。
- **自检测试台骨架**：TbGen.py 生成公共脚手架，错误时 `report "###ERROR###: ..."`（见 u11-l1）。
- **回归测试入口**：`sim/config.tcl` 是 TB 注册表，`run.tcl` / `runGhdl.tcl` 跑全库回归（见 u1-l3）。
- **命名与握手规范**：snake_case、`_i/_o/_io` 后缀、AXI-S 的 VLD/RDY 握手（见 u1-l4）。

本讲几乎不涉及具体 VHDL 实现，主要是**流程、规范与约定**。但我们会用 `git log`、`git tag`、CI 配置等真实证据来验证文档里写的规矩在仓库里到底是怎么执行的——这是本讲区别于「只读文档」的地方。

## 3. 本讲源码地图

本讲引用的关键文件如下：

| 文件 | 作用 |
|------|------|
| `doc/old/ch1_introduction/ch1_introduction.md` | §1.4「Contribute to PSI VHDL Libraries」是贡献规范的权威出处：代码质量、generic 化、自检 TB、文档、commit 注释、GIT 工作流六条全在这里。 |
| `README.md` | 仓库门面：维护者、收录范围（属于 / 不属于本库）、**标签策略（Tagging Policy）**、被脚本解析的 **Dependencies** 段。 |
| `Changelog.md` | 手工维护的版本变更日志，每个版本按「Added Features / Bugfixes / Cleaning / Changes that are not reverse compatible」分类罗列改动。 |
| `scripts/dependencies.py` | 依赖检出脚本：解析 README 的 Dependencies 段，调用 `PsiFpgaLibDependencies` 把兄弟仓库（PsiSim、psi_tb）按固定目录结构拉下来。 |
| `.gitlab-ci.yml` | GitLab CI 配置：linter + simulation 两个 stage，用 `###ERROR###` 与 `SIMULATIONS COMPLETED SUCCESSFULLY` 两个字符串判定回归成败。 |
| `scripts/ciFlow.py` | 本地版 CI 跑批脚本，与 `.gitlab-ci.yml` 共用同一套成败判据。 |

此外，我们会用 `git log` 与 `git tag` 的真实输出来佐证 commit 约定与版本策略。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**贡献规范**、**分支与 PR 流程（含 CI 门禁）**、**commit 注释约定**、**版本号与标签管理**。四者按时间顺序串成一条链：写代码（规范）→ 开分支提 PR（流程）→ 写 commit 注释（约定）→ 维护者打标签发布（版本）。

### 4.1 贡献规范与检查清单

#### 4.1.1 概念说明

「贡献规范」回答的是：**什么样的代码会被维护者接受，什么样的会被打回。** psi_common 是一个被多个项目共享的底层库，一旦某个组件被收录，它的接口就几乎成为事实标准（v3.0.0 大重构就是因为旧命名不统一，参见 u11-l3 的迁移脚本）。因此维护者对入库代码的要求比项目内代码更严格：不仅要「能跑」，还要「能被别人轻松复用、安全修改、自动验证」。

这套规范不是建议，而是**硬性要求**。文档把它列成六条逐条说明，我们可以把它整理成一张可勾选的检查清单。

#### 4.1.2 核心流程

一次合格贡献的完成态，应同时满足以下六项（缺一不可）：

```text
[ ] 1. 代码质量   readable / understandable / correct / safe
[ ] 2. 可配置性   所有编译期可变参数都用 generic 暴露
[ ] 3. 自检 TB    覆盖全部功能，仿真自动停止，错误以 ###ERROR### 开头
[ ] 4. 文档       在 doc/ 下补充对应说明
[ ] 5. 注册回归   把新 TB 登记进 sim/config.tcl，并实测能跑过
[ ] 6. 命名规范   snake_case、_i/_o/_io 后缀、接口前缀、架构名 behav/struc/rtl
```

此外，README 还明确了**收录范围**（What belongs / does not belong），它决定了你的组件是否「有资格」进这个库：

- **属于**：时钟域跨越、FIFO、厂商无关 RAM、扩展语言的 package。
- **不属于**：项目专用代码、更适合别的库的代码（如信号处理应进 `psi_fix`）、**不能完全参数化的代码**。

「不能完全参数化」这一条值得强调：它和第 2 条「可配置性」是同一枚硬币的两面——如果你写的 RAM 写死深度 1024、位宽 16，那它就不是一个合格的库组件，会被直接拒绝。

#### 4.1.3 源码精读

规范的权威出处是 [doc/old/ch1_introduction/ch1_introduction.md:71-107](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L71-L107)，这一节标题就是「Contribute to PSI VHDL Libraries」。逐条对应：

- [ch1_introduction.md:73-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L73-L74)：**Good Code Quality**——文档坦诚地说「没有硬性细则」，但要求 readable / understandable / correct / safe。
- [ch1_introduction.md:76-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L76-L77)：**Configurability**——「Only code that is written in a generic way and can easily be reused will be accepted」。这句话是整个库「全 generic 化」风格（math_pkg 编译期推导位宽、各组件 generic 暴露资源/深度/极性）的制度根源。
- [ch1_introduction.md:79-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L79-L84)：**Self-checking Test-benches**——强制要求；关键一句在第 84 行：「If an error occurs, the message reported shall start with `###ERROR###:`. This is required since the regression test script searches for this string in reports.」这行是整个 CI 链路的**契约点**，我们在 4.2 节会看到 CI 正是 grep 这个字符串。
- [ch1_introduction.md:86-87](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L86-L87)：**Documentation**——「Extend this document with proper documentation」。
- [ch1_introduction.md:89-91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L89-L91)：**New test-benches** must be added to the regression test-script——改 `sim/config.tcl`，并「Test if the regression test really runs the new test-bench and exits without errors before doing any merge requests」。

命名细则没有放在 ch1，而是放在索引文档 [doc/README.md:8-16](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L8-L16)，标题叫「Quick syntax rules to push into the library」。它把 u1-l4 讲过的规范浓缩成七条速查：snake_case、tab 转空格、`_i/_o/_io` 后缀、显式 `end entity/architecture`、同接口信号加前缀（如 `adc_`）、架构名限 `behav/struc/rtl`、结构体内部连线用 `compa2compb_` 前缀。

收录范围见 [README.md:26-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L26-L43)，其中第 28 行「Code must be written with reuse in mind. All important settings must be implemented as Generics」是核心红线，[README.md:43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L43) 的「Code that is not fully parametrizable」明确把不可参数化的代码挡在门外。

> 小贴士：自检 TB 的 `###ERROR###` 约定不只是「文档建议」，而是 CI 的硬判据。这意味着你 TB 里哪怕只有一个错误忘了加这个前缀，CI 也查不出来——这条契约要求**每个**错误分支都严格遵守，正是 u11-l1 强调的内容。

#### 4.1.4 代码实践

**实践目标**：把抽象的「贡献规范」落到一个真实组件上，练习「对照检查清单审查一个已入库组件」。

**操作步骤**：

1. 选一个较新的组件——`hdl/psi_common_sample_rate_converter.vhd`（commit `9c39e3d` 才入库，见 `git log`）。
2. 打开它的 entity，逐项核对 4.1.2 的检查清单：
   - 是否所有可变参数（通道数、抽取/插值比、位宽）都做成了 generic（第 2 条）？
   - 端口命名是否 `snake_case` + `_i/_o` 后缀 + 接口前缀（第 6 条）？
   - 是否有 `end entity;` 这样的显式结尾（第 6 条）？
3. 找它的测试平台 `testbench/psi_common_sample_rate_converter_tb/`（若存在），检查错误分支是否以 `###ERROR###:` 开头（第 3 条）。
4. 检查 `sim/config.tcl` 里是否登记了这个 TB（第 5 条）。

**需要观察的现象**：

- 一个合格的入库组件，其 entity 的 generic 区应能覆盖所有「别人可能要改」的参数；端口名应严格遵守后缀与前缀约定。
- 若某项缺失（例如该组件暂无 TB），这本身就是一个**待补全的贡献机会**——这正是文档第 3 条「mandatory」的含义。

**预期结果**：你能用检查清单对任意组件打分，并指出它「是否符合入库标准」、缺哪几项。`sample_rate_converter` 在 `Changelog.md` 顶部未出现、且 u10-l4 提到它「当前无回归 TB」，所以你大概率会发现第 3、5 条尚未满足——这就是一个真实可做的贡献点。

#### 4.1.5 小练习与答案

**练习 1**：文档说代码质量「没有硬性细则」，那维护者凭什么判断「good code quality」？

> **答案**：靠四条软标准——readable（可读）、understandable（易懂）、correct（正确）、safe（安全）。虽然没有 lint 规则，但命名规范（doc/README.md 七条）+ 二进程 record 设计法（u7-l1）+ 完善自检 TB 共同构成事实上的质量底线；`.gitlab-ci.yml` 里 `code-style` 阶段目前还是 `echo "...tbd..."`（见 4.2.3），说明自动化的 lint 尚未落地，质量把关目前主要靠人工 code review。

**练习 2**：为什么「不能完全参数化的代码」会被 README 明确拒收？

> **答案**：因为 psi_common 的核心定位是「可复用的通用库」。一个写死深度/位宽的 RAM 只能服务一个项目，无法被他人复用，违背了库的存在意义；维护者宁可不要，也不愿让库退化成项目专用代码的堆放地（这类代码应留在项目仓库或 `psi_fix` 等更专用的库）。

---

### 4.2 分支与 PR 流程（含 CI 门禁）

#### 4.2.1 概念说明

「分支与 PR 流程」回答的是：**改动从你手里到进入主线，要经过哪些环节、谁在什么时机介入。** psi_common 采用经典的 **`develop` / `master` 双分支模型**：`master` 只放稳定发布版本，`develop` 是日常集成分支，所有新组件都从 `develop` 拉特性分支、改完再 PR 回 `develop`。这套流程保证 `master` 任意一个 commit 都对应一个可发布的稳定点。

关键概念：

- **特性分支（feature branch）**：你干活的地方，建议以「新 block 的名字」命名。
- **Pull Request（PR）**：把特性分支合并回 `develop` 的请求，触发 code review。
- **CI 门禁**：GitLab CI 在每次提交上自动跑全库回归，用 `###ERROR###` 判定成败——这是 PR 能否合并的客观技术门槛。

#### 4.2.2 核心流程

文档用三步描述了完整流程（见 4.2.3 源码），可画成：

```text
            ┌─────────────────────────────────────────────────────┐
            │  1. 从 develop 拉特性分支 (建议以新 block 命名)        │
            │     开发者自由提交                                    │
            └────────────────────────┬────────────────────────────┘
                                     ▼
            ┌─────────────────────────────────────────────────────┐
            │  2. 完工后向 develop 发 Pull Request                  │
            │     └─ CI 自动跑回归 (###ERROR### 判据)              │
            │     └─ 维护者 code review，提意见                    │
            └────────────────────────┬────────────────────────────┘
                                     ▼
            ┌─────────────────────────────────────────────────────┐
            │  3. 意见处理完毕、CI 通过、维护者认可                  │
            │     → 合并进 develop                                  │
            │     → 仓库维护者安全删除特性分支                       │
            └────────────────────────┬────────────────────────────┘
                                     ▼
            (发布时机) develop 的稳定点 → 合并 master → 打 tag (见 4.4)
```

CI 门禁的判据很朴素：**全库回归的输出日志里，必须出现「SIMULATIONS COMPLETED SUCCESSFULLY」、且不能出现「###ERROR###」**。前者保证仿真真的跑完了（没中途崩溃），后者保证没有任何自检 TB 报错。两个字符串缺一不可——单看「没报错」是不够的，因为仿真压根没跑完时也不会有 `###ERROR###`。

#### 4.2.3 源码精读

双分支模型的文字定义在 [ch1_introduction.md:101-103](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L101-L103)。第 102 行明确：「All PSI libraries have at least two branches develop & master. The master branch is used for stable release version... The develop branch is the branch when a GIT user shall diverge from to add a new component.」第 103 行规定了「分支以新 block 命名 → PR 到 develop → review → 合并 → 维护者删分支」的三步法。

用 `git branch -a` 可以验证仓库里真的遵循这个模型：

```text
* master                              ← 当前主线（稳定）
  remotes/origin/develop              ← 日常集成分支
  remotes/origin/devel/3-wires-spi    ← 特性分支：3 线 SPI（commit bbed262）
  remotes/origin/devel/spi_le         ← 特性分支
  remotes/origin/bugfix_pulse_cc      ← 修 bug 分支
  remotes/origin/min_max_cfg          ← 以功能命名的分支
  remotes/origin/pipeline-fix         ← 以功能命名的分支
```

可以看到：实践里分支命名用 `devel/`、`bugfix_` 前缀或直接以功能名（`min_max_cfg`、`pipeline-fix`）命名，精神与文档「call the branch the name of the new block」一致——**让分支名能说清这分支在干什么**。提交信息里带 `(#62)`、`(#63)`、`(#53)` 等 PR 编号，也印证了「PR 合并」是入库的主路径。

CI 门禁的实现在 [.gitlab-ci.yml:19-41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/.gitlab-ci.yml#L19-L41)。Modelsim 这一段（[L19-30](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/.gitlab-ci.yml#L19-L30)）就是上面说的双字符串判据的落地：

```yaml
script:
  - tool modelsim_2020.4
  - cd sim
  - vsim -c -do ci.do -logfile Transcript.transcript
  - grep -Fq "SIMULATIONS COMPLETED SUCCESSFULLY" Transcript.transcript   # 必须出现
  - (! grep -Fq "###ERROR###" Transcript.transcript)                       # 必须不出现
```

第 30 行的 `(! grep -Fq "###ERROR###" ...)` 取反退出码：grep 找到字符串返回 0、`!` 取反为非 0，使该步失败、CI 红灯。GHDL 段（[L32-41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/.gitlab-ci.yml#L32-L41)）结构完全相同，只是仿真器换成 `ghdl_3.0.0`、入口换成 `runGhdl.tcl`。

`before_script`（[L5-9](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/.gitlab-ci.yml#L5-L9)）揭示了 CI 环境如何拿到仿真依赖：用 `git force-clone` 把 PsiSim 和 psi_tb 克隆到固定的相对路径——这正是 u1-l3 讲的「工作副本结构」在 CI 里的自动化重建。

值得一提的是 `code-style` 阶段（[L11-17](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/.gitlab-ci.yml#L11-L17)）：`allow_failure: true` 加 `echo "...tbd..."`，说明**自动化代码风格检查尚未实现**，目前允许它失败、不阻塞流水线。也就是说，命名规范现阶段靠人工 review 把关，未来这块会补上。

本地版 CI 跑批脚本 [scripts/ciFlow.py:14-23](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/ciFlow.py#L14-L23) 把同一套判据复刻到本地：找到 `###ERROR###` 退出 −1、找不到「成功」串退出 −2、否则 0。这让你在推送前就能用和 CI 完全相同的逻辑自测。

> 小贴士：CI 的双字符串判据与贡献规范第 3 条（自检 TB 错误以 `###ERROR###` 开头）是**同一个契约的两端**——TB 端负责「正确地喊错」，CI 端负责「听到喊错就红灯」。两端必须用同一个字符串，这正是为何文档反复强调这个前缀。

#### 4.2.4 代码实践

**实践目标**：在本地用与 CI 相同的判据跑一次回归，体验「PR 合并前的技术门禁」。

**操作步骤**：

1. 按 u1-l3 把工作副本结构摆好（PsiSim、psi_tb、psi_common 互为兄弟目录）。
2. 进入 `sim/` 目录，跑 Modelsim 回归：`source ./run.tcl`（或 GHDL：`tclsh runGhdl.tcl`）。
3. 跑完后，在日志里手动执行 CI 的两条 grep：
   - `grep -Fq "SIMULATIONS COMPLETED SUCCESSFULLY" <日志>` —— 期望找到。
   - `grep -Fq "###ERROR###" <日志>` —— 期望找不到。
4. 若本地装了依赖包 `PsiFpgaLibDependencies`，可直接 `python scripts/ciFlow.py`，它会替你完成步骤 3 并用退出码表达结果。

**需要观察的现象**：

- 一次干净的回归，日志末尾应打印「SIMULATIONS COMPLETED SUCCESSFULLY」，且全文无 `###ERROR###`。
- 若某 TB 因环境缺失被 `tb_run_skip` 跳过（如 Vivado 专属 TB，见 u1-l3），不影响这条判据。

**预期结果**：你能在本地复现 CI 的绿灯条件。若结果异常（比如出现 `###ERROR###`），说明你本地工作副本结构或依赖版本有问题——这正是 CI 要拦截的情况。**待本地验证**（取决于你是否装好 Modelsim/GHDL 与依赖）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CI 用「成功串存在 **且** 错误串不存在」两个条件，而不是只看「错误串不存在」？

> **答案**：因为仿真中途崩溃或脚本异常退出时，日志里既不会有 `###ERROR###`（没跑到自检）、也不会有「SIMULATIONS COMPLETED SUCCESSFULLY」（没正常结束）。只看「无错误串」会把这种崩溃误判为通过。成功串是「确实跑完了」的正面证据，二者合起来才能区分「真通过」与「没跑完」。

**练习 2**：特性分支合进 `develop` 后被删掉，为什么强调「by the repository maintainer」？

> **答案**：删分支是不可逆操作（虽然 reflog 可短期恢复）。由维护者统一执行，可避免贡献者误删未完全合并的分支、或与正在进行的其他 PR 冲突；同时维护者会在删除前确认合并已生效、CI 已绿、review 意见已处理完毕，起到最后一道把关作用。

---

### 4.3 commit 注释约定

#### 4.3.1 概念说明

「commit 注释约定」回答的是：**每一次提交的第一行该写什么，才能让维护者一眼看懂、让 Changelog 有据可查、让后人能检索。** psi_common 要求 commit 注释以一个**大写关键词前缀**开头，相当于给每条提交打上类型标签。这和 Angular / Conventional Commits 的思路一致，只是关键词集合是 PSI 自己定义的。

约定有两个价值：一是维护者合并 PR 时能快速浏览改动性质；二是发版时写 `Changelog.md` 时可以直接按前缀归类（`FEATURE` → Added Features，`BUGFIX` → Bugfixes）。

#### 4.3.2 核心流程

文档定义了六个标准前缀（见 4.3.3），可整理成表：

| 前缀 | 含义 | 典型场景 |
|------|------|----------|
| `FEATURE` | 给库新增功能 | 新增一个组件、给 package 加函数/类型/常量 |
| `GIT`     | Git 相关操作 | 合并、分支操作等 |
| `BUGFIX`  | 修复缺陷 | 修一个已发现的 bug |
| `DOCU`    | 文档相关 | 改文档、补注释 |
| `DEVEL`   | 开发中（未完成） | 工作进行中的中间提交 |
| `TB`      | 测试平台相关 | 改 TB、加 TB |

格式约定：**前缀在第一行开头，后跟简短描述**。文档原话是「add a short description at first of your commit annotation」。

> 现实提醒（来自真实 `git log`）：仓库历史里实际出现的前缀比文档定义的六个要多——还有 `ADD`、`DOC`、`CI`、`TYPO`、`MODIF`、`NAME`、`PRBS` 等，甚至有大小写不一致（`DOC:` vs `Doc:`）。这说明实践已经漂移。**作为新贡献者，建议严格使用文档定义的六个前缀**（`FEATURE/GIT/BUGFIX/DOCU/DEVEL/TB`），避免再造新词，保持与文档一致。

#### 4.3.3 源码精读

commit 约定的权威出处是 [ch1_introduction.md:93-99](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L93-L99)。第 93 行总述要求，第 94-99 行逐条列出六个前缀及含义（见上表）。

用真实 `git log --oneline` 可以看到这套约定在实践中的样子，也能看到漂移：

```text
9c39e3d FEATURE: Add Sample Rate Converter simple (#63)   ← 标准 FEATURE，带 PR 号
bbed262 DEVEL:spi_tri_o port added ... (#62)              ← 标准 DEVEL（注意冒号后无空格，格式略糙）
bf3c80b BUGFIX: Fix a/val lib consistency error ...       ← 标准 BUGFIX
3e0a527 DOC: fixed broken link triggers                   ← 漂移：文档用了 DOC 而非 DOCU
98c2fcc ADD: add new strobe generator ...                 ← 漂移：新增用 ADD 而非 FEATURE
012308b CI: Fix tool loader                               ← 漂移：出现了文档未定义的 CI
8df502c Bugfixes: reset polarity simple cc ...            ← 漂移：大小写/复数都不一致
```

从这些真实例子能读出三点：(1) `FEATURE`/`BUGFIX`/`DEVEL` 是高频且符合文档的主流派；(2) 带 `(#NN)` 的提交说明走的是 PR 合并流程，与 4.2 的 PR 模型吻合；(3) 文档之外的 `ADD`/`DOC`/`CI`/`TYPO`/`MODIF`/`NAME` 是历史沉淀的「方言」，新代码宜回归文档定义。维护者 Benoît Stef（见 [README.md:6-7](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L6-L7)）在合并时是最终把关人。

#### 4.3.4 代码实践

**实践目标**：把仓库里几条「漂移」的真实 commit 改写成符合文档约定的形式，熟练掌握前缀用法。

**操作步骤**：

1. 选三条漂移提交作为改写对象：
   - `98c2fcc ADD: add new strobe generator with clock cycle count, no tb will come later`
   - `3e0a527 DOC: fixed broken link triggers`
   - `012308b CI: Fix tool loader`
2. 对照 4.3.2 的六前缀表，判断每条**本该**用哪个前缀：
   - 新增 strobe generator（一个新组件）→ 应为 `FEATURE`。
   - 修文档里的死链 → 应为 `DOCU`。
   - 改 CI 脚本（工具链维护）→ 文档六前缀里没有 `CI`；最接近的是 `DEVEL`（开发/工具）或直接归到工具链维护（实践中保留了 `CI`，但新贡献者可写 `DEVEL` 并在描述里说明）。
3. 为每条写出符合约定的注释，第一行格式统一为「`前缀: 简短描述`」，冒号后加一个空格。
4. （可选）用 `git log --oneline | grep -E "^(FEATURE|BUGFIX|DEVEL|DOCU|TB|GIT):"` 统计当前库里有多少条严格符合文档、多少条漂移，量化「约定的执行率」。

**需要观察的现象**：

- 改写后的注释第一眼就能看出改动类型，可被 `grep` 按前缀批量筛选——这正是维护者写 Changelog 时的检索方式。
- 统计会发现严格符合文档六前缀的提交是主流，漂移是少数——约定整体被执行，但有松动。

**预期结果**：你能对任意一条 commit 判断它「是否符合约定」并给出规范改写。注意：这是**练习书写格式**，不要真的去重写已推送的历史（那会改写哈希、影响他人），仅在本地理解层面练习。

#### 4.3.5 小练习与答案

**练习 1**：你给 `psi_common_math_pkg` 新增了一个函数 `nearest_pow2`，又在同一次提交里改了它的 TB，commit 注释该怎么写？

> **答案**：按文档归类，新增 package 元素属于 `FEATURE`（文档原话：「a component or a package element (i.e. function, procedure, type and constant)」）。主类型是新增功能，所以第一行用 `FEATURE:` 开头，描述里可同时提到函数与 TB，例如：`FEATURE: Add nearest_pow2 to math_pkg and extend its TB`。无需为同一提交写两个前缀，取**主要**改动性质即可。（仓库里 `2c6e2dc` 正是这条提交，前缀用的就是 `FEATURE`。）

**练习 2**：为什么 `DEVEL`（开发中）这种「未完成」的提交也被允许进库？

> **答案**：特性分支上允许中间提交（`DEVEL` 标明「工作未完成」），便于开发者频繁保存进度、备份半成品。关键是这些 `DEVEL` 提交发生在特性分支上，不会直接进 `develop`；只有当特性分支被 PR 合并、且合并前已完工、CI 通过时，才会进入集成主线。`DEVEL` 标签让维护者 review 时一眼识别哪些是「临时快照」。

---

### 4.4 版本号与标签管理

#### 4.4.1 概念说明

「版本与标签」回答的是：**改动积累到什么程度该发版、发版号怎么定、怎么标记。** psi_common 用三段式版本号 `major.minor.bugfix`，并在稳定点上打 git tag。这套规则回答了三个问题：用户能不能无痛升级（看 major）、有没有新功能（看 minor）、是不是只修了 bug（看 bugfix）。

关键概念：

- **三段式版本号**：`major.minor.bugfix`，每段递增的含义由「是否向后兼容」「是否新增功能」「是否只修 bug」决定。
- **标签（tag）**：打在 `master` 稳定 commit 上的命名标记，对应一次发布。
- **Changelog**：手工维护的版本日志，发版时由维护者按版本号追加一段。
- **Dependencies 段**：README 里一段**机器解析**的依赖声明，`scripts/dependencies.py` 读取它来检出依赖——格式不能乱改。

#### 4.4.2 核心流程

版本号递增规则（见 [README.md:46-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L46-L50)）可表为：

| 触发条件 | 递增的段 | 例子（本库真实版本） |
|----------|----------|----------------------|
| 改动**不**完全向后兼容 | `major` | 2.x.x → **3**.0.0（统一命名风格、不兼容，附迁移脚本） |
| 新增功能（向后兼容） | `minor` | 3.0.0 → 3.**1**.0（如加新组件） |
| 仅修复 bug（无功能变化） | `bugfix` | 3.0.0 → 3.0.**1**（只修 pulse_cc/async_fifo 极性等） |

判定逻辑可写成伪代码：

```text
if 改动破坏向后兼容:
    major += 1; minor = 0; bugfix = 0
elif 新增功能:
    minor += 1; bugfix = 0
elif 仅修 bug:
    bugfix += 1
```

发版的完整动作：

```text
1. develop 上积累的改动稳定后 → 合并到 master
2. 维护者按上表决定新版本号
3. 用该版本号在 master 对应 commit 上打 git tag
4. 在 Changelog.md 顶部追加一段，按 Added Features / Bugfixes / ... 分类罗列改动
```

#### 4.4.3 源码精读

标签策略的权威定义在 [README.md:45-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L45-L50)。第 46 行「Stable releases are tagged in the form *major*.*minor*.*bugfix*」定义了三段式，第 48-50 行三条分别对应 major/minor/bugfix 的递增条件（见上表）。

用 `git tag --list` 可以验证仓库实际打了哪些版本标签。当前共有 29 个标签，从 `2.0.0` 到 `3.0.4`，命名严格遵循 `major.minor.bugfix`。用 `git log --tags --simplify-by-decoration` 可以看到发版节奏：

```text
2024-08-07  (tag: 3.0.4)
2024-06-06  (tag: 3.0.3)
2024-05-31  (tag: 3.0.1)
2023-04-14  (tag: 3.0.0)    ← 大重构、不兼容升 major，附迁移脚本（见 u11-l3）
2021-09-07  (tag: 2.17.1)
2021-08-19  (tag: 2.17.0)
...
```

可以看到 `3.0.0` 到 `3.0.4` 之间间隔约一年多、随后半年内连发三个 bugfix/小版本，符合「大版本后用 minor/bugfix 持续修补」的常见模式。

`Changelog.md` 是发版的纸面产物。最新版 [Changelog.md:1-6](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L1-L6) 记录了 `3.0.1`，归类为 Bugfixes（pulse_cc TB、async_fifo 复位极性等）；[Changelog.md:8-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L8-L13) 记录了 `3.0.0`，归类为 Cleaning（统一代码风格、加迁移脚本）+ Added Features（新增 pwm）。注意 Changelog 是**手工**维护的——实际标签已到 `3.0.4`，而 Changelog 顶部只到 `3.0.1`，说明这份日志会**滞后于标签**，发版时维护者需补写。这是手工日志的固有维护成本，也是 review 时要留意的点。

依赖声明与检出的机器契约：README 的 Dependencies 段被特殊标记为「机器解析区」——[README.md:52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L52) 的 HTML 注释 `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->` 是开始标记，[README.md:65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L65) 的 `<!-- END OF PARSED SECTION -->` 是结束标记。中间 [README.md:54-64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L54-L64) 声明了 PsiSim（≥2.1.0）与 psi_tb（≥3.0.0）两个依赖及其目录结构。

这段之所以「不能改格式」，是因为 [scripts/dependencies.py:7](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/dependencies.py#L7) 直接 `Parse.FromReadme(THIS_DIR + "/../README.md")`——脚本靠这段文本的固定缩进与结构来提取依赖清单（[scripts/dependencies.py:1-10](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/dependencies.py#L1-L10) 全文仅 10 行，核心就这一句解析加一句 `Actions.ExecMain` 执行检出）。如果你改了这段的缩进或顺序，依赖检出脚本就会解析失败——这是「文档即数据契约」的典型案例，也是发版/改文档时容易踩的坑。

> 小贴士：版本号递增的判定完全靠维护者人工评估「是否向后兼容」。`3.0.0` 升 major 就是因为「统一命名风格」破坏了所有旧例化的兼容性，维护者同时提供了 `migration_from_v2_to_v3_db.json` 迁移脚本（u11-l3）来降低升级成本——这是「不兼容升 major」时负责任的做法。

#### 4.4.4 代码实践

**实践目标**：用真实标签与 Changelog 练习「给定一组改动，判定该发什么版本号」。

**操作步骤**：

1. 列出全部标签并排序：`git tag --list`（共 29 个）。
2. 查看两个相邻标签之间的改动，判断版本号递增是否合理。例如对比 `3.0.0 → 3.0.1`：
   - `git log 3.0.0..3.0.1 --oneline`
   - 对照 [Changelog.md:1-6](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L1-L6)：改动都是 TB 与复位极性修复，无新功能、无接口破坏 → 递增 `bugfix`（3.0.0 → 3.0.**1**），合理。
3. 做一个假设练习：假如你在 `3.0.4` 之后新增了一个组件 `sample_rate_converter`（向后兼容、新功能），又只修了一个 TB 的小 bug，按规则该发什么号？
   - 新功能 → minor +1 → `3.1.0`。
   - 随后只修 bug → bugfix +1 → `3.1.1`。
4. 检验机器契约：打开 README，确认 Dependencies 段（[README.md:54-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L54-L65)）前后有两个 HTML 注释标记；若你装了 `PsiFpgaLibDependencies`，运行 `python scripts/dependencies.py -help` 查看检出用法（参见 [README.md:67-73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L67-L73)）。

**需要观察的现象**：

- 相邻 tag 间的 commit 应与 Changelog 该版本段的描述对得上（若对不上，说明 Changelog 漏写，是维护瑕疵）。
- Dependencies 段格式一旦被破坏，`dependencies.py` 会解析失败——印证「文档即契约」。

**预期结果**：你能对任意一组改动判定正确的版本号递增段，并解释为什么 `3.0.0` 是 major、`3.0.1` 是 bugfix。同时理解 README 的 Dependencies 段是脚本解析的数据源、不可随意改格式。

#### 4.4.5 小练习与答案

**练习 1**：维护者把 `psi_common_min_max_mean` 改名为 `psi_common_min_max_sum`（见 Changelog 2.17.1），这次改动应该升 major、minor 还是 bugfix？

> **答案**：组件改名属于**破坏向后兼容**（所有旧例化与 use 引用都会失效），按规则应升 `major`。但 Changelog 里它被归在 2.17.1（一个 bugfix 段）——这反映了现实中「破坏性程度较小」的改名有时被当作次要改动处理。严格按文档规则应是 major；这种文档规则与历史实践的偏差，正是发版判定需要维护者人工裁决的地方。

**练习 2**：为什么 README 的 Dependencies 段要用 HTML 注释把「机器解析区」框起来？

> **答案**：因为这段文本身兼二职——既是给人看的依赖说明，也是 `scripts/dependencies.py` 解析的数据源。HTML 注释 `<!-- DO NOT CHANGE FORMAT ... -->` 是给人看的警告（「别改格式，脚本要读」），`<!-- END OF PARSED SECTION -->` 标明解析边界。把数据契约嵌在文档里、用注释划定边界，是一种轻量的「文档即配置」做法，代价是改文档时必须意识到这段会被机器读取。

---

## 5. 综合实践

把本讲四个模块串起来，模拟一次**完整的贡献到发布**流程。本实践是「源码阅读 + 流程推演」型，不要求你真的改源码，而是用真实仓库状态演练判断力。

**场景**：假设你要给 psi_common 贡献一个新组件 `psi_common_strobe_generator_cfg`（按周期数可配置的选通发生器，HEAD `98c2fcc` 刚加入，commit 信息为 `ADD: add new strobe generator with clock cycle count, no tb will come later`）。

**任务**：按本讲四个模块，完成以下推演：

1. **贡献规范（4.1）**：对照检查清单，这条 commit 信息里「no tb will come later」暴露了哪几条尚未满足？至少指出第 3 条（自检 TB）与第 5 条（注册回归）。该组件若要合格入库，还需补什么？
2. **分支与 PR（4.2）**：你应该从哪个分支拉特性分支？分支该叫什么名字（参考仓库现有 `devel/*` 命名）？提 PR 的目标分支是哪个？合并前 CI 会用哪两个字符串判定成败？
3. **commit 约定（4.3）**：现有 commit 信息 `ADD: ...` 用了文档未定义的 `ADD`。请按文档六前缀，把它改写成规范形式（提示：新增组件应为 `FEATURE`）。如果之后补了 TB，那条提交又该用什么前缀？
4. **版本与标签（4.4）**：该组件最终合并进 `develop` 并随下次发布进入 `master`。若此次发布**只**新增了这一个组件、无接口破坏，版本号应从当前最新 tag 怎么变？应在 `Changelog.md` 哪个分类下记录？

**参考答案要点**：

1. 缺自检 TB（第 3 条强制 mandatory）与注册回归（第 5 条，需登记 `sim/config.tcl`）；还需补文档（第 4 条）。当前状态是 `DEVEL`（未完成），不满足入库门槛。
2. 从 `develop` 拉分支，命名如 `devel/strobe_gen_cfg`；PR 目标是 `develop`；CI 判据为「SIMULATIONS COMPLETED SUCCESSFULLY 存在 **且** `###ERROR###` 不存在」。
3. 改写为 `FEATURE: Add strobe_generator_cfg (cycle-count based)`；补 TB 的提交用 `TB:` 前缀。
4. 仅新增组件、向后兼容 → 升 `minor`（如 `3.0.4` → `3.1.0`）；Changelog 记在「Added Features」分类下（参照 [Changelog.md:11-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L11-L13) 新增 pwm 的写法）。

完成此综合实践，意味着你已经把「规范 → 流程 → 约定 → 发布」四环打通，具备向 psi_common 贡献代码的全流程认知。

## 6. 本讲小结

- **贡献有六条硬性要求**：代码质量、generic 化、自检 TB、文档、注册回归、命名规范；其中「自检 TB 错误以 `###ERROR###:` 开头」是贯穿 CI 链路的契约点。
- **双分支模型**：`master` 放稳定发布、`develop` 是集成分支；特性分支从 `develop` 拉、以功能/block 命名、PR 回 `develop`，经 review 与 CI 通过后由维护者合并并删分支。
- **CI 门禁用双字符串判据**：日志里必须出现「SIMULATIONS COMPLETED SUCCESSFULLY」且不出现「###ERROR###」；这要求与自检 TB 的 `###ERROR###` 约定互为表里，本地可用 `scripts/ciFlow.py` 复现。
- **commit 注释用前缀**：文档定义 `FEATURE/GIT/BUGFIX/DOCU/DEVEL/TB` 六个标准前缀；真实历史存在 `ADD/DOC/CI/TYPO` 等漂移，新贡献者宜回归文档定义。
- **三段式版本号**：`major.minor.bugfix`，分别对应「不兼容升 major、新增功能升 minor、仅修 bug 升 bugfix」，发版时打 git tag 并在 `Changelog.md` 补段（手工维护，可能滞后于 tag）。
- **README 的 Dependencies 段是机器解析的数据契约**：被 `scripts/dependencies.py` 读取以检出 PsiSim/psi_tb，格式不可随意改动——这是「文档即配置」的典型与发版/改文档时的踩坑点。

## 7. 下一步学习建议

本讲是 psi_common 手册的收官篇。建议你按以下方向继续：

- **动手做一次真实贡献**：挑一个「无 TB」或「TB 未注册」的组件（如 `sample_rate_converter`、`pwm`，见 u10-l4），按本讲检查清单补齐自检 TB 与回归注册，走一遍 PR 流程。
- **重读迁移工具**：结合 u11-l3 的 `migration_from_v2_to_v3_db.json`，体会 `3.0.0` 升 major 时维护者如何用脚本降低不兼容升级的痛苦——这是「负责任地升 major」的工程范例。
- **通读全库 Changelog**：从 `Changelog.md` 的 `V1.00` 到 `3.0.x` 通读一遍，能从版本演进里看出整个库的功能生长脉络（FIFO → CDC → AXI → 杂项），帮你建立组件间的历史依赖直觉。
- **横向比较同类库**：把 psi_common 的贡献/发布规范与 `psi_fix`、`psi_tb`（同属 PSI）对照，你会发现它们共享同一套分支模型与 `###ERROR###` 约定——这是 PSI FPGA 库家族的一致工程文化。
- **回到任意组件讲义复用本讲框架**：今后读任何组件，都用本讲的「检查清单」自查它是否合格入库——这会把「读源码」升级为「评审源码」。

> 至此，从「认识项目」（U1）到「贡献与发布」（U11），psi_common 学习手册的完整路线已走完。祝你在 FPGA/ASIC 工程实践中用好这套库。
