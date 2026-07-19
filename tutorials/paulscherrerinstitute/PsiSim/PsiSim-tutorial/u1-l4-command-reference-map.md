# 命令参考地图

## 1. 本讲目标

在前几讲里，你已经知道 PsiSim 把全部功能塞进一个 [`PsiSim.tcl`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) 文件，对外暴露 17 个命令，并且跑通了「config.tcl + run.tcl」的两文件工作流。但那 17 个命令到底有哪些、各自叫什么名字、文档把它们排成了什么样——这些细节我们一直刻意没展开。

本讲专门把 [`CommandRef.md`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md) 当作一张「命令地图」来读。读完本讲你应该能够：

1. 说清 `CommandRef.md` 是如何把 17 个命令分成「通用 / 配置 / 运行」三大类的，以及这三类各自包含哪些命令。
2. 拿到一个命令名（例如 `tb_run_add_arguments`），能立刻在 `PsiSim.tcl` 里定位到它对应的 `proc` 定义行和 `namespace export` 行。
3. 掌握一套「先读文档、再跳源码」的导航方法，把 `CommandRef.md` 当作进入源码的索引，而不是死记 966 行代码。

本讲只读两个文件：`CommandRef.md`（命令目录）和 `PsiSim.tcl`（命令实现）。不涉及任何运行行为，重点是「看地图」和「按图索骥」。

## 2. 前置知识

在开始前，请确认你理解下面几个概念（前几讲已建立，这里只做一句话复习）：

- **命名空间（namespace）**：PsiSim 所有命令都装在 `psi::sim` 这个命名空间里。使用前要么写全名 `psi::sim::init`，要么先 `namespace import psi::sim::*` 把命令导入到全局。
- **导出命令（exported command）**：只有被 `namespace export` 显式声明的 `proc`，才会在 `namespace import psi::sim::*` 时被带出来，成为用户可用的命令。本讲的「命令」特指导出命令。
- **回归测试生命周期**：一次完整的 PsiSim 回归，时间线上依次是「准备 → 配置 → 运行」。`CommandRef.md` 的三大分类，本质上就是按这条生命周期来切分的。
- **proc 与行号**：TCL 里用 `proc 名字 {参数} { 体 }` 定义过程。每个命令在 `PsiSim.tcl` 里都对应一个 `proc`，本讲会频繁给出 `proc` 所在的行号，方便你跳转。

一个值得提前记住的结论：`PsiSim.tcl` 里**定义的 `proc` 比 17 个导出命令多**。多出来的那些（带 `sal_` 前缀的抽象层过程、以及个别 `# Internal Function`）并不出现在 `CommandRef.md` 里——本讲会专门讲这条「文档只覆盖导出命令」的边界。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|------|------|------------------|
| [`CommandRef.md`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md) | 命令参考手册，是本讲的「地图」本身 | 文件头说明（行 1-9）、命令目录 `Command Links`（行 11-31）、三大分类的章节标题、每条命令的统一条目结构 |
| [`PsiSim.tcl`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) | 唯一源码，966 行 | 各导出命令的 `proc 定义`行与 `namespace export` 行，用于建立「命令名 → 行号」映射表；以及非导出的 `compile`、`clean_transcript` 两个边界案例 |

本讲不会深入任何 `proc` 的内部逻辑（那是单元 2、单元 3 的工作），只关心**在哪里**能找到它。

## 4. 核心概念与源码讲解

### 4.1 命令三大分类：CommandRef 的目录结构

#### 4.1.1 概念说明

`CommandRef.md` 不是一个扁平的命令列表，而是一份**有结构的手册**。它先在开头给出一张「目录」（叫 `Command Links`），再按三大分类逐条展开命令。三大分类是：

- **General Commands（通用命令）**：只有 1 个，即 `init`。它负责选择仿真器并把整个环境清零，是任何脚本里**必须最先调用**的命令。
- **Configuration Commands（配置命令）**：共 11 个，用来「登记」要编译什么、要跑哪些测试台、要抑制哪些告警。它们只修改 PsiSim 的内部状态变量，不真正触发编译或仿真。
- **Run Commands（运行命令）**：共 5 个，用来真正执行清理、编译、仿真、错误检查和交互调试，它们会消费配置阶段登记好的状态。

\(1 + 11 + 5 = 17\)，正好是 PsiSim 对外暴露的全部命令数。这条等式是本讲最重要的一组数字，请记住。

这三类不是随便分的，它们对应一次回归测试的时间线：先 `init` 重置（通用），再用配置命令把库/源/测试台登记进状态变量（配置），最后用运行命令把状态变量消费掉、真正跑起来（运行）。u1-l3 讲过的「黄金七步」命令序列，正好横穿这三个分类。

#### 4.1.2 核心流程

`CommandRef.md` 的目录是这样组织的（伪结构）：

```
CommandRef.md
├─ 文件头：说明所有命令在 psi::sim 命名空间下，需先 import
├─ # Command Links            ← 目录（TOC）
│   ├─ * General Commands      → init
│   ├─ * Configuration Commands → 11 条
│   └─ * Run Commands          → 5 条
├─ ## General Commands         → init 详解
├─ ## Configuration Commands   → 11 条详解
└─ ## Run Commands             → 5 条详解
```

也就是说，文档采用「目录 + 详解」双层结构：目录给你全景，详解给你细节。读文档的第一步永远是先看目录，建立全景，再按需跳到某条命令的详解。

把分类映射到生命周期，就是：

| 文档分类 | 数量 | 生命周期阶段 | 这一阶段的命令干什么 |
|----------|------|--------------|----------------------|
| General | 1 | 准备 | 选仿真器、清空状态 |
| Configuration | 11 | 配置 | 登记库/源/测试台/告警（只改状态） |
| Run | 5 | 执行 | 清理/编译/仿真/检查/调试（消费状态） |

#### 4.1.3 源码精读

文档开头的命名空间说明，告诉我们所有命令都挂在 `psi::sim` 下，并给出导入方法：

[CommandRef.md:1-9](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L1-L9) —— 说明命令归属 `psi::sim` 命名空间，推荐用 `namespace import psi::sim::*` 导入。这是「地图」的图例。

紧接着的 `Command Links` 就是目录本体，三大分类一目了然：

[CommandRef.md:11-31](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L11-L31) —— 这是全文最重要的导航区。你能数到 General 下 1 条、Configuration 下 11 条、Run 下 5 条，合计 17 条。点击任一链接会跳到对应详解。

三个分类的章节标题行分别是：

- [CommandRef.md:33](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L33) —— `## General Commands`
- [CommandRef.md:65](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L65) —— `## Configuration Commands`
- [CommandRef.md:348](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L348) —— `## Run Commands`

#### 4.1.4 代码实践

**实践目标**：亲手数一遍三大分类的命令数，验证 \(1+11+5=17\)。

**操作步骤**：

1. 打开 `CommandRef.md`，滚动到 `# Command Links`（约第 11 行）。
2. 在 General Commands 下数链接数，记录。
3. 在 Configuration Commands 下数链接数，记录。
4. 在 Run Commands 下数链接数，记录。
5. 把三个数加起来。

**需要观察的现象**：你会看到 General 下只有 `init` 一行；Configuration 下有从 `add_library` 到 `run_suppress` 共 11 行；Run 下有从 `clean_libraries` 到 `launch_tb` 共 5 行。

**预期结果**：\(1 + 11 + 5 = 17\)，与 PsiSim 导出命令总数一致。如果数出来不是 17，说明你漏了或多算了某条（注意 `tb_run_add_post_script` 在文档里没有独立 Usage 块，而是用一句「equal to tb_run_add_pre_script」带过，但它仍是独立的一条命令链接）。

#### 4.1.5 小练习与答案

**练习 1**：`init` 为什么被单独归为 General，而不是 Configuration？

**参考答案**：因为 `init` 的职责是「选仿真器 + 把库/源/测试台/告警等状态全部清零」，它发生在任何配置之前，是配置的前提，而不是配置本身。把它单列，是为了强调「它必须是脚本里第一个被调用的命令」。

**练习 2**：有人把 `compile_files` 误记成配置命令，请根据文档纠正。

**参考答案**：`compile_files` 出现在 `CommandRef.md` 的 `## Run Commands` 章节下（详解见第 380 行起），属于运行命令。配置命令只登记源文件（`add_sources`），真正编译是运行阶段的事。

### 4.2 命令与源码 proc 的对应关系

#### 4.2.1 概念说明

`CommandRef.md` 里的每一条命令，在 `PsiSim.tcl` 里都对应：

1. 一个 `proc 名字 {参数} { ... }` 定义（命令的实现）。
2. 紧跟其后的（或在附近）一句 `namespace export 名字`（把它声明为导出命令）。

掌握了这条对应关系，你就能从文档的命令名，一步跳到源码的实现行。这比通读 966 行高效得多。

但有两个「边界案例」必须单独记住，它们是本节的重点：

- **`compile` 与 `compile_files`**：源码里同时存在 `proc compile`（第 548 行）和 `proc compile_files`（第 609 行）。真正的实现是 `compile`，但它**没有** `namespace export`，所以不对外暴露；对外暴露的是包装函数 `compile_files`，它内部用 `eval "compile ..."` 转调真正的 `compile`。原因是 Modelsim 自带一个叫 `compile` 的命令，直接导出会与之冲突。`CommandRef.md` 因此只记载 `compile_files`。
- **`clean_transcript`**：源码第 714 行有一个 `proc clean_transcript`，上面第 713 行注释写着 `# Internal Function`，它同样没有 `namespace export`，也不出现在 `CommandRef.md` 里。它是给 `init` 和 `run_tb` 内部调用的。

记住这条边界规则：**`CommandRef.md` 只覆盖被 `namespace export` 的命令**。所有 `sal_*` 前缀的过程（13 个，构成 SAL 抽象层）和 `# Internal Function` 都不在文档里。

#### 4.2.2 核心流程

从「文档命令名」定位到「源码行号」的标准动作是：

```
1. 在 CommandRef.md 里读到命令名（例如 add_sources）
2. 在 PsiSim.tcl 里搜索 "proc add_sources"
   → 命中第 435 行：proc add_sources {directory files {args}} {
3. 往下找紧随的 "namespace export add_sources"
   → 命中第 503 行：namespace export add_sources
4. 于是这条命令的「身份」就是：
     实现 = PsiSim.tcl 第 435 行起
     导出声明 = 第 503 行
```

对 17 条命令重复这个动作，就得到本讲的核心产出——一张「命令 → 行号」映射表（见 4.2.3）。

#### 4.2.3 源码精读

下面这张表，是本讲最值得收藏的「地图」。每个命令都给出 `proc` 起始行与 `namespace export` 行，并标注它属于哪个文档分类。

**General Commands（1 条）**

| 命令 | proc 起始行 | export 行 | 源码链接 |
|------|------------|-----------|----------|
| `init` | 351 | 379 | [PsiSim.tcl:351-378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378) |

**Configuration Commands（11 条）**

| 命令 | proc 起始行 | export 行 |
|------|------------|-----------|
| `add_library` | 384 | 389 |
| `compile_suppress` | 394 | 405 |
| `run_suppress` | 410 | 421 |
| `add_sources` | 435 | 503 |
| `create_tb_run` | 622 | 644 |
| `tb_run_add_pre_script` | 651 | 659 |
| `tb_run_add_post_script` | 666 | 674 |
| `tb_run_add_arguments` | 679 | 683 |
| `tb_run_add_time_limit` | 689 | 693 |
| `tb_run_skip` | 698 | 702 |
| `add_tb_run` | 706 | 711 |

**Run Commands（5 条）**

| 命令 | proc 起始行 | export 行 | 备注 |
|------|------------|-----------|------|
| `clean_libraries` | 510 | 538 | |
| `compile_files` | 609 | 613 | 包装函数；真正实现在未导出的 `compile`（548 行） |
| `run_check_errors` | 721 | 740 | |
| `run_tb` | 752 | 839 | |
| `launch_tb` | 852 | 965 | |

来看两个边界案例的源码。

`compile_files` 是一个极简的包装，它把参数拼起来后用 `eval` 转调真正的 `compile`：

[PsiSim.tcl:608-613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L608-L613) —— 注意 `proc compile`（第 548 行）之后**没有** `namespace export compile`，只有 `compile_files` 被导出。注释明说这是「to prevent name clash with modelsim 'compile'」。

`CommandRef.md` 也明确记载了这个设计取舍：

[CommandRef.md:390-391](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L390-L391) —— 文档原话：命令也可以用 `psi::sim::compile` 访问，但这个名字不导出，以防与 Modelsim 的 `compile` 冲突，因此推荐只用 `compile_files`。文档与源码在这点上是吻合的。

再看内部函数 `clean_transcript`：

[PsiSim.tcl:713-716](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L713-L716) —— 第 713 行注释 `# Internal Function`，第 714 行 `proc clean_transcript {}`，后面没有 `namespace export`，所以它不出现在 `CommandRef.md` 里，仅供 `init`（第 377 行）和 `run_tb`（第 784 行）内部调用。

#### 4.2.4 代码实践

**实践目标**：自己动手验证上表中的行号，并体会「导出 vs 不导出」的差异。

**操作步骤**：

1. 在 `PsiSim.tcl` 里搜索 `namespace export`，把命中的命令名逐一抄下来。
2. 数一下 `namespace export` 一共出现了多少次。
3. 再搜索 `proc compile `（注意 `compile` 后有个空格，避免匹配到 `compile_files` / `compile_suppress`），确认它后面没有 `namespace export compile`。
4. 搜索 `# Internal Function`，确认它下面的 `clean_transcript` 也没有导出。

**需要观察的现象**：

- `namespace export` 共出现 17 次，命令名与 `CommandRef.md` 目录完全一致。
- `proc compile`（第 548 行）存在，但其后没有对应的 `namespace export compile`。
- `proc clean_transcript`（第 714 行）被注释为内部函数，同样未导出。

**预期结果**：17 条导出命令一一对应；`compile` 与 `clean_transcript` 是源码里「存在却不导出、不在文档里」的两个案例。如果 `namespace export` 的次数不是 17，请检查是否漏数了某条。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CommandRef.md` 里找不到 `compile`，只能找到 `compile_files`？

**参考答案**：因为真正的实现 `proc compile`（第 548 行）没有被 `namespace export`，目的是避免与 Modelsim 自带的 `compile` 命令重名冲突。文档只记载对外暴露的 `compile_files`（第 609 行），它是个内部转调 `compile` 的包装函数。

**练习 2**：`sal_compile_file` 出现在 `CommandRef.md` 里吗？为什么？

**参考答案**：不出现。`sal_compile_file`（第 162 行）是 SAL 抽象层的内部过程，带 `sal_` 前缀，没有被 `namespace export`。`CommandRef.md` 只覆盖导出命令，所以所有 `sal_*` 和 `# Internal Function` 都不在文档里。

### 4.3 如何用文档驱动源码阅读

#### 4.3.1 概念说明

掌握了「三大分类」和「命令→行号映射」之后，本节讲一套可复用的工作流：**把 `CommandRef.md` 当成进入源码的索引，而不是把 966 行代码从头读到尾**。

这套工作流建立在一个观察上：`CommandRef.md` 里每条命令的详解，都遵循统一的「三段式」结构：

- **Usage**：命令的调用签名，告诉你参数怎么写（例如 `add_sources <directory> <file> [-lib <lib>] ...`）。
- **Description**：自然语言说明它干什么、有什么约束。
- **Parameters**：一张表，逐个参数列出「是否可选 + 含义」。

此外，文档还有两个高频出现的「模式」，一旦认出它们，读文档的速度会大幅提升：

- **「必须夹在 X 和 Y 之间」约束**：很多配置命令的 Description 里写着「This command must be called between the `create_tb_run` and the `add_tb_run` commands」。这是一种**两段式编程模型**的标志——先 `create_tb_run` 开个头，中间用若干 `tb_run_add_*` 命令填细节，最后 `add_tb_run` 收尾提交。
- **交叉引用（cross-reference）**：文档常用 `[commandB](#commandB)` 的形式引用另一条命令。例如 `tb_run_add_post_script` 的整段说明就是一句「equal to `tb_run_add_pre_script`」，意思是两者用法完全一样，只是一个在前、一个在后。

最后，文档末尾偶尔会附「工作流小节」，给出某条命令的推荐使用步骤，例如 `launch_tb` 条目末尾的 `GHDL/GTK Workflow`，这就是文档自带的「最佳实践」。

#### 4.3.2 核心流程

「文档驱动源码阅读」的标准动作：

```
1. 明确你想了解的命令（例如 run_check_errors）。
2. 在 CommandRef.md 的 Command Links 里点它的链接，跳到详解。
3. 读 Usage → 知道签名；读 Parameters 表 → 知道参数。
4. 注意 Description 里的约束（如「夹在 X 和 Y 之间」）。
5. 用 4.2 节的映射表，跳到 PsiSim.tcl 里对应的 proc 行。
6. 对照文档描述，只读该 proc 的关键几行（参数解析 + 主逻辑）。
7. 想知道它调用了哪些底层过程，再顺着 proc 里的 sal_* 往下挖（这是单元 3 的事）。
```

这样做的好处是：你每次只读一个命令的相关片段，永远有文档作为「在做什么」的锚点，不会在 966 行里迷路。

#### 4.3.3 源码精读

先看一条命令详解的统一三段式结构，以 `init` 为例：

[CommandRef.md:35-63](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L35-L63) —— 依次是 `### init`、`**Usage**`（`init [-ghdl|-vivado]`）、`**Description**`（说明它会清空环境、应最先调用）、`**Parameters**`（一张表列出 `-ghdl`、`-vivado` 两个可选参数）。所有 17 条命令都套用这个模板。

再看「必须夹在 create_tb_run 和 add_tb_run 之间」这个约束模式，它在多条命令里重复出现，是识别两段式编程模型的信号：

[CommandRef.md:212](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L212) —— `tb_run_add_arguments` 的 Description 明确写「must be called between the `create_tb_run` and the `add_tb_run` commands」。同样的措辞也出现在 `tb_run_add_time_limit`（第 237 行）、`tb_run_add_pre_script`（第 262 行）、`tb_run_skip`（第 301 行）。

这个约束在源码里是怎么落地的？看 `create_tb_run` 如何初始化一个「半成品」字典，再由 `add_tb_run` 把它收进 `TbRuns` 列表：

[PsiSim.tcl:622-643](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L622-L643) —— `create_tb_run` 创建并初始化 `ThisTbRun` 字典（含 `TB_NAME`、`TB_LIB`、`TB_ARGS`、各种脚本字段、`TIME_LIMIT`、`SKIP`），它只是一个「当前正在编辑」的半成品。

[PsiSim.tcl:706-711](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L706-L711) —— `add_tb_run` 把当前的 `ThisTbRun` 追加到 `TbRuns` 列表里，完成提交。中间那些 `tb_run_add_*` 命令之所以「必须夹在两者之间」，正是因为它们操作的就是这个共享的 `ThisTbRun` 半成品。文档的约束和源码的设计在这里严丝合缝。

最后看一个「文档自带工作流」的例子，`launch_tb` 条目末尾的 GHDL/GTKWave 调试步骤：

[CommandRef.md:537-543](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L537-L543) —— 文档直接给出迭代调试的推荐步骤：第一次带 `-wave -show` 打开 GTKWave，之后不带 `-show` 重跑、再在 GTKWave 里 `File → Reload Waveform`。读到这里你不必去翻源码，文档已经把「怎么用」讲清楚了。

#### 4.3.4 代码实践

**实践目标**：用「文档驱动」的方法，独立读懂一条你尚未接触过的命令。

**操作步骤**：

1. 挑一条命令，例如 `run_check_errors`（先别看本讲以外资料）。
2. 在 `CommandRef.md` 的 `Command Links` 里点 `run_check_errors`，跳到它的详解（约第 467 行起）。
3. 读 Usage、Description、Parameters 三段，记下：它的必填参数是什么？文档建议用什么「特征明显的错误模式」？
4. 用 4.2 节的映射表，跳到 `PsiSim.tcl` 第 721 行的 `proc run_check_errors`。
5. 只读这一个 `proc`（第 721-739 行），对照文档描述，确认它确实是「读 transcript、用正则找错误串」。

**需要观察的现象**：

- 文档说推荐用 `###ERROR###` 这种「distinctive error pattern」，并解释用 `Error` 可能误报（因为它可能出现在非错误消息里，如 `Checking ... zero Position Error`）。
- 源码里 `run_check_errors` 用 `regexp -nocase $errorString $transcriptContent` 做匹配，与文档描述一致。

**预期结果**：你不需要通读全文，仅靠「文档详解 + 单个 proc」就能讲清 `run_check_errors` 的输入、行为和推荐用法。这就证明了「文档驱动源码阅读」的有效性。注：本实践属于「源码阅读型实践」，无需运行任何仿真器。

#### 4.3.5 小练习与答案

**练习 1**：`tb_run_add_post_script` 在 `CommandRef.md` 里没有独立的 Usage 块，你怎么知道它的参数？

**参考答案**：因为它的整段说明就是一句「equal to `tb_run_add_pre_script`」。所以它的参数与 `tb_run_add_pre_script` 完全一致：`<cmd> [<args>] [<path>]`（见第 256 行的 Usage）。这是文档用交叉引用省篇幅的典型手法。

**练习 2**：请用一句话概括「文档驱动源码阅读」相比「通读 PsiSim.tcl」的优势。

**参考答案**：文档提供了命令的分类、签名、参数和约束作为锚点，让你可以按需跳到对应 `proc` 的行号，只读相关片段，而不必在 966 行里漫无目的地顺序阅读。

## 5. 综合实践

本任务把本讲三个模块串起来，产出一张属于你自己的「命令地图」。

**任务**：浏览 `CommandRef.md`，把全部 17 个命令分成「配置阶段」和「运行阶段」两类，填入一张表；并在 `PsiSim.tcl` 中为每个命令标注其 `proc` 起始行号。

**操作步骤**：

1. 打开 `CommandRef.md` 的 `Command Links`（第 11-31 行），把 17 条命令抄下来。
2. 按下面规则归入两阶段（注意 `CommandRef.md` 原始是三类，这里要合并成两类）：
   - **配置阶段**：所有 General + Configuration 命令，即 `init` 以及 `add_library`、`add_sources`、`compile_suppress`、`run_suppress`、`create_tb_run`、`tb_run_add_arguments`、`tb_run_add_time_limit`、`tb_run_add_pre_script`、`tb_run_add_post_script`、`tb_run_skip`、`add_tb_run`（共 12 条；`init` 是配置阶段最开头的「重置/准备」命令，必须早于一切配置命令）。
   - **运行阶段**：所有 Run 命令，即 `clean_libraries`、`compile_files`、`run_tb`、`run_check_errors`、`launch_tb`（共 5 条）。
3. 对每条命令，用本讲 4.2.3 节的映射表（或自己搜索 `proc`）补上 `PsiSim.tcl` 的行号。
4. 在表里标注两个边界案例：`compile_files`（真正实现是未导出的 `compile`，第 548 行）、以及不在文档里的内部函数 `clean_transcript`（第 714 行）。

**预期产出**（参考格式，行号请自行核对）：

| 阶段 | 命令 | proc 起始行 | 备注 |
|------|------|------------|------|
| 配置 | init | 351 | 必须最先调用 |
| 配置 | add_library | 384 | |
| 配置 | add_sources | 435 | 支持 glob |
| 配置 | compile_suppress | 394 | |
| 配置 | run_suppress | 410 | |
| 配置 | create_tb_run | 622 | 两段式的「开头」 |
| 配置 | tb_run_add_arguments | 679 | 夹在 create/add 之间 |
| 配置 | tb_run_add_time_limit | 689 | 夹在 create/add 之间 |
| 配置 | tb_run_add_pre_script | 651 | 夹在 create/add 之间 |
| 配置 | tb_run_add_post_script | 666 | 夹在 create/add 之间 |
| 配置 | tb_run_skip | 698 | 夹在 create/add 之间 |
| 配置 | add_tb_run | 706 | 两段式的「提交」 |
| 运行 | clean_libraries | 510 | |
| 运行 | compile_files | 609 | 内部转调 compile(548) |
| 运行 | run_tb | 752 | |
| 运行 | run_check_errors | 721 | |
| 运行 | launch_tb | 852 | 仅 Modelsim+GHDL |

完成后，\(12 + 5 = 17\) 应当成立。这张表就是你后续学习单元 2（配置阶段命令的内部实现）和单元 3（运行阶段命令经 SAL 调用仿真器）时的「路标」。

## 6. 本讲小结

- `CommandRef.md` 采用「目录（Command Links）+ 详解」双层结构，把 17 个导出命令分成 General（1 条 `init`）、Configuration（11 条）、Run（5 条）三大类，\(\,1+11+5=17\,\)。
- 三大分类对应回归测试的生命周期：准备（`init` 重置）→ 配置（登记库/源/测试台）→ 执行（清理/编译/仿真/检查/调试）。
- 每条命令在 `PsiSim.tcl` 里都对应一个 `proc 定义`行和一句 `namespace export` 行；本讲给出了一张完整的「命令 → 行号」映射表。
- 两个边界案例：`compile`（第 548 行）因与 Modelsim 自带命令冲突而**不导出**，对外只暴露包装函数 `compile_files`（第 609 行）；`clean_transcript`（第 714 行）是 `# Internal Function`，也不导出。两者都不出现在文档里。
- 文档每条命令详解遵循 Usage / Description / Parameters 三段式；「必须夹在 create_tb_run 与 add_tb_run 之间」是识别两段式编程模型的信号，它由源码里的共享半成品字典 `ThisTbRun` 落地。
- 正确的读码姿势是「文档驱动」：先用文档建立命令全景和约束，再按映射表跳到单个 `proc` 的行号，按需阅读，而不是通读 966 行。

## 7. 下一步学习建议

本讲建立的是「命令全景与导航能力」，你现在已经知道每条命令叫什么、在哪一行。接下来建议：

1. **进入单元 2（进阶）**：挑一个配置阶段的命令（建议从 `init` 与状态变量、再到 `add_sources`/`create_tb_run`），深入读它的 `proc` 内部实现，理解配置命令如何把信息写进 `Sources`、`TbRuns` 等状态变量。本讲的映射表就是你跳转的起点。
2. **留意两段式编程模型**：后续读到 `create_tb_run` → `tb_run_add_*` → `add_tb_run` 时，回想本讲指出的「夹在中间」约束，你会更清楚 `ThisTbRun` 这个半成品字典的生命周期。
3. **为单元 3（SAL）做准备**：运行阶段的 `compile_files`、`run_tb` 等命令，内部都会调用 `sal_*` 过程。本讲已经告诉你这些 `sal_*` 不在文档里，单元 3 会专门讲它们如何屏蔽 Modelsim/GHDL/Vivado 的差异。
