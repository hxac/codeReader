# 仓库结构与文件组织

## 1. 本讲目标

上一篇（u1-l1）我们已经建立了对 PsiSim 的整体认知：它是 PSI 用 TCL 写的 VHDL 回归测试框架，全部逻辑集中在一个 966 行的 `PsiSim.tcl` 里，封装在 `psi::sim` 命名空间下。本讲不再重复这些定位信息，而是带你“打开抽屉看内部”，目标是：

- **看懂仓库里到底有哪些文件**，以及每个文件各自的用途；
- **建立 `PsiSim.tcl` 的内部地图**——掌握它由哪三大区块组成、各自起止行号在哪里；
- **建立命令全景图**——能在动手读源码之前，就先按“通用 / 配置 / 运行”三类把全部命令归类，并知道每个命令对应源码里的哪个 `proc`。

学完本讲，你拿到 `PsiSim.tcl` 时不再面对一整屏代码发懵，而是能立刻定位到“我想看的功能大概在第几区块、第几行”。

## 2. 前置知识

本讲需要一点点 TCL 语言常识。如果你没接触过 TCL，下面几个概念够用了：

- **命名空间（namespace）**：TCL 用 `namespace eval foo { ... }` 把一组变量和过程圈在一起，避免和别的代码重名。PsiSim 把所有东西都放进 `psi::sim` 这个命名空间。
- **`variable` 关键字**：在命名空间内部，`variable X` 声明一个属于该命名空间的状态变量（不是局部变量）。它相当于这个框架的“全局内存”。
- **`proc`**：定义一个过程（函数）。`proc 名字 {参数} { 函数体 }`。
- **`namespace export`**：给过程打上“可导出”标记。配合 `namespace import psi::sim::*`，被导出的过程就能直接用名字调用，没导出的则是框架内部私有过程。
- **`eval`**：把一段字符串当成命令来执行。PsiSim 在拼接仿真器命令时大量用到它（后续单元会细讲）。

另外两个工程概念：

- **接口函数（Interface Function）**：面向使用者、构成公开 API 的命令，比如 `init`、`add_sources`、`run_tb`。
- **抽象层（Abstraction Layer）**：一层“中间代码”，把上层统一的接口翻译成不同后端（这里是 Modelsim / GHDL / Vivado 三种仿真器）各自的真实命令。PsiSim 称之为 **SAL（Simulator Abstraction Layer，模拟器抽象层）**，单元 3 会专门讲它。

## 3. 本讲源码地图

本讲涉及的文件几乎覆盖整个仓库，但只读“骨架”，不深挖每个过程内部：

| 文件 | 行数 | 在本讲中的作用 |
| --- | --- | --- |
| `PsiSim.tcl` | 966 | 唯一的源码文件；本讲核心是看清它的三大区块与命令分布 |
| `README.md` | 169 | 项目说明、用法、`config.tcl`/`run.tcl` 示例 |
| `CommandRef.md` | 543 | 全部 17 个命令的参考手册；本讲用它建立命令分类全景 |
| `Changelog.md` | 109 | 版本演进记录（当前 2.5.0），帮助理解仓库维护方式 |
| `License.txt` | 22 | PSI HDL Library License（LGPL + 固件例外） |
| `LGPL2_1.txt` | 354 | LGPL 2.1 协议全文 |

> 说明：`License.txt` 与 `LGPL2_1.txt` 的法律细节已在 u1-l1 讲过，本讲只在文件清单中列出，不重复。

## 4. 核心概念与源码讲解

### 4.1 仓库文件清单与用途

#### 4.1.1 概念说明

PsiSim 是一个“小而精”的项目：仓库一共只有 6 个被 Git 跟踪的文件，没有任何 `src/`、`tests/`、`examples/` 之类的子目录。这是一件好事——对初学者来说，整个项目的学习材料就是这 6 个文件。

这些文件可以分成三类：

1. **唯一源码**：`PsiSim.tcl`——框架的全部实现都在这里。
2. **用户文档**：`README.md`（入门与示例）、`CommandRef.md`（命令手册）、`Changelog.md`（变更记录）。
3. **法律文件**：`License.txt`、`LGPL2_1.txt`。

#### 4.1.2 核心流程

一个新读者拿到仓库后，推荐的阅读顺序是：

```text
README.md   ──►  知道 PsiSim 是什么、怎么用（config.tcl + run.tcl 两文件模式）
   │
   ▼
CommandRef.md ──►  把 17 个命令的分类和参数查清楚
   │
   ▼
PsiSim.tcl   ──►  对照命令手册，回到源码看每个命令到底怎么实现
   │
   ▼
Changelog.md ──►  遇到某个行为不确定时，查它是哪个版本引入/修改的
```

`PsiSim.tcl` 是被 `source` 进 TCL 解释器的，本身不需要编译、不需要打包——这也是它能“对版本控制友好”的物理基础。

#### 4.1.3 源码精读

仓库根目录的文件清单可以用只读 git 命令确认：

```text
$ git ls-files
Changelog.md
CommandRef.md
LGPL2_1.txt
License.txt
PsiSim.tcl
README.md
```

只有这 6 个文件，没有子目录。`PsiSim.tcl` 的文件头注释说明了它的来源和用途：

[PsiSim.tcl:1-12](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L1-L12) —— 版权声明与“PSI Modelsim Simulation Package”的简介，说明它用来快速创建含多次测试运行和前后脚本的回归仿真。

`README.md` 在开头给出了维护者、作者、许可证与文档入口：

[README.md:1-16](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L1-L16) —— 维护者 Patric Bucher、作者 Oliver Bründler，并指向 Changelog 与 Command Reference 两个文档。

`CommandRef.md` 开篇就强调了命名空间这个关键事实：

[CommandRef.md:1-8](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L1-L8) —— “所有命令都在 `psi::sim` 命名空间下”，必须用全限定名 `psi::sim::<命令>` 或先 `namespace import psi::sim::*` 才能调用。

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库的物理布局，而不是相信任何讲义里的描述。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`，列出所有被跟踪的文件。
2. 用 `wc -l <文件>` 数出每个文件的行数。

**需要观察的现象**：

- 输出中**没有任何子目录路径**，全部是根目录下的扁平文件。
- `PsiSim.tcl` 恰好 966 行；`CommandRef.md` 比 `PsiSim.tcl` 还长（543 行 vs 966 行并不重要，重点是文档量不小）。

**预期结果**：你会得到与上面“源码精读”中完全一致的 6 个文件清单。如果将来仓库新增了 `examples/` 或 `tests/` 目录，本实践也能第一时间暴露这种结构变化。

> 待本地验证：不同操作系统上 `wc -l` 的输出格式略有差异（行数列在前还是在后），但文件名清单一致。

#### 4.1.5 小练习与答案

**练习 1**：仓库里没有 `src/` 目录，源码直接放在根目录的 `PsiSim.tcl`。这对“版本控制友好”这个卖点有什么帮助？

**参考答案**：单文件意味着用户在自己的项目里只需 `source` 一个路径即可加载整个框架，没有目录结构耦合；脚本里只出现纯文本的 `.tcl` 文件名，不像 Modelsim 的 `.mpf` 工程或 Vivado 工程那样包含绝对路径和二进制结构，因此 Git diff 与 merge 都很干净。

**练习 2**：如果想确认某个命令是从哪个版本开始支持的，应该查哪个文件？

**参考答案**：查 `Changelog.md`。例如当前版本是 2.5.0，`Changelog.md:1-8` 记录了本版本的新功能与修复。

---

### 4.2 PsiSim.tcl 的三大分区结构总览

#### 4.2.1 概念说明

966 行代码虽然不算多，但如果没有地图，从第 1 行顺着读到第 966 行会非常低效。PsiSim 的作者用注释横幅（一整行 `###...`）把整个命名空间内部分成了三个清晰的区块：

1. **区块一：Namespace Variables（命名空间变量）**——框架的“状态内存”，保存库列表、源文件列表、测试运行列表、当前仿真器等。
2. **区块二：Simulator Abstraction Layer / SAL（模拟器抽象层）**——一组以 `sal_` 开头的**内部**过程，负责把统一接口翻译成 Modelsim/GHDL/Vivado 三种仿真器的真实命令。
3. **区块三：Interface Functions（接口函数）**——面向用户的**导出**命令，也就是 `CommandRef.md` 里列出的那些，比如 `init`、`add_sources`、`compile_files`、`run_tb`。

这三层构成一个清晰的自上而下的调用关系：

```text
   用户脚本 (config.tcl / run.tcl)
              │  调用
              ▼
   区块三：接口函数（导出命令，公开 API）
              │  内部调用 sal_*
              ▼
   区块二：SAL 抽象层（内部过程，屏蔽仿真器差异）
              │  dispatch 到三套实现
              ▼
   Modelsim / GHDL / Vivado 实际命令
              ▲
   区块一：状态变量  ◄── 上面三层都读写这些变量
```

> 记住这张图，它就是整个 `PsiSim.tcl` 的“设计骨架”。单元 2 主要讲区块一和区块三；单元 3 专门讲区块二。

#### 4.2.2 核心流程

整个文件的外层结构是这样的（行号精确到本讲 HEAD）：

```text
PsiSim.tcl (966 行)
├── 第 1–12 行    文件头注释（版权 + 包说明）
├── 第 14 行      namespace eval psi::sim {        ← 整个框架的唯一命名空间开始
│   ├── 第 16–26 行   区块一：Namespace Variables（10 个状态变量）
│   ├── 第 28–342 行  区块二：Simulator Abstraction Layer (SAL)（13 个 sal_* 内部过程）
│   └── 第 344–965 行 区块三：Interface Functions（17 个导出命令 + 2 个未导出内部过程）
└── 第 966 行     }                                 ← namespace 结束
```

三个区块之间用注释横幅分隔，每个横幅都是三行（上下两行 `###...`，中间一行标题）。

#### 4.2.3 源码精读

**区块零（文件头）**：

[PsiSim.tcl:14](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L14) —— `namespace eval psi::sim {`，整个框架的唯一命名空间从这里开始，到第 966 行的 `}` 结束。所有变量和过程都活在这个命名空间里。

**区块一：状态变量**（第 16–26 行）：

[PsiSim.tcl:16-26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L16-L26) —— 用 `variable` 声明了 10 个状态变量。这里只看名字就能猜出用途：

| 变量 | 含义（后续讲义会逐一展开） |
| --- | --- |
| `Libraries` | 所有通过 `add_library` 创建的库名列表 |
| `CurrentLib` | 当前默认库（最近一次 `add_library` 的目标） |
| `Sources` | 所有源文件的列表，每个元素是一个描述文件的 dict |
| `ThisTbRun` | “正在配置中的”那一个测试运行 |
| `TbRuns` | 所有已确认添加的测试运行列表 |
| `CompileSuppress` | 编译阶段要抑制的消息编号串 |
| `RunSuppress` | 仿真阶段要抑制的消息编号串 |
| `Simulator` | 当前选用的仿真器（`Modelsim`/`GHDL`/`Vivado`） |
| `SimulatorVersion` | 仿真器版本号 |
| `TranscriptFile` | 日志/transcript 文件路径 |

> 注意：这里只声明了变量名，**没有赋初值**。真正的初始化发生在 `init` 命令里（见 u2-l1）。

**区块二：SAL 抽象层**（第 28–342 行）：

[PsiSim.tcl:28-31](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L28-L31) —— 注释横幅 `# Simulator Abstraction Layer (SAL)` 之后，第一个 SAL 过程 `sal_print_log` 从第 31 行开始。

[PsiSim.tcl:335-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L335-L342) —— 最后一个 SAL 过程 `sal_open_wave` 在第 342 行结束。区块二至此收尾。

这一区块共有 **13 个** `sal_*` 过程，全部以 `sal_` 前缀命名，并且**没有一个**带 `namespace export`——它们是纯内部实现，用户脚本不应直接调用。

**区块三：接口函数**（第 344–965 行）：

[PsiSim.tcl:344-351](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L344-L351) —— 注释横幅 `# Interface Functions (exported)` 之后，第一个接口函数 `init` 从第 351 行开始。

[PsiSim.tcl:965-966](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L965-L966) —— 最后一个接口函数 `launch_tb` 在第 965 行处 `namespace export launch_tb`，紧接着第 966 行就是命名空间的闭合花括号 `}`。

这一区块里有 **17 个** 带有 `namespace export` 的导出命令（构成公开 API），外加 **2 个** 没有导出的内部过程：`compile`（第 548 行，因与 Modelsim 自带命令重名而特意不导出，仅通过 `compile_files` 间接调用）和 `clean_transcript`（第 714 行，仅被框架内部调用）。

#### 4.2.4 代码实践

**实践目标**：亲自在 `PsiSim.tcl` 中定位三大区块的起始行号，并画出文件结构示意图（即本讲主实践任务）。

**操作步骤**：

1. 打开 `PsiSim.tcl`，分别搜索三个注释横幅：
   - `# Namespace Variables` → 应定位到第 16 行；
   - `# Simulator Abstraction Layer (SAL)` → 应定位到第 29 行（横幅第 2 行）；
   - `# Interface Functions (exported)` → 应定位到第 345 行（横幅第 2 行）。
2. 数一下每个区块里 `proc` 出现的次数：区块二应能数到 13 个 `sal_*`；区块三应能数到 17 个 `namespace export`。
3. 在编辑器里用“折叠/大纲”视图（多数编辑器能列出全部 `proc`），把结果画成一棵树。

**需要观察的现象**：

- 三个区块的注释横幅风格完全一致，都是上下两行 `###...`、中间一行标题，便于一眼区分。
- 区块三里 `compile`（第 548 行）后面紧跟的 `compile_files`（第 609 行）才有 `namespace export`——这正是“为什么不直接用 `compile`”的伏笔。

**预期结果**：你画出的示意图应与 4.2.2 节中的那棵树一致。把这棵树记在笔记里，后续读任何一篇讲义时，都能秒回“讲到的是哪个区块”。

#### 4.2.5 小练习与答案

**练习 1**：状态变量（区块一）只声明了名字、没有赋初值。那么框架的“干净初始状态”是在哪里建立的？

**参考答案**：在 `init` 命令里。`init`（第 351 行起）会把 `Libraries`、`Sources`、`TbRuns` 等重置为空列表，把 `CompileSuppress`/`RunSuppress` 清空，并设置 `Simulator`。这也是为什么 `init` 必须是脚本里第一个被调用的命令（详见 u2-l1）。

**练习 2**：SAL 里的过程为什么都以 `sal_` 开头且不导出？

**参考答案**：前缀是一种命名约定，提醒读者“这是屏蔽仿真器差异的内部实现，不属于公开 API”；不导出（无 `namespace export`）则在语言层面保证了用户脚本无法直接调用它们，所有访问都必须经过区块三的接口函数，从而保证上层接口对三种仿真器保持一致。

---

### 4.3 命令分类速览

#### 4.3.1 概念说明

在深入每个命令的实现之前，先用 `CommandRef.md` 建立一张“命令地图”是非常划算的。`CommandRef.md` 已经帮我们把 17 个导出命令分成了三类：

1. **General Commands（通用命令）**：只有 `init` 一个，负责初始化环境、选择仿真器。
2. **Configuration Commands（配置命令）**：用来“描述”仿真工程——建库、加源文件、定义测试运行、配置抑制消息等。这些命令大多只改状态变量，不真正跑仿真。
3. **Run Commands（运行命令）**：用来“执行”——清理库、编译、跑测试、检查错误、交互调试。

这个“先描述、再执行”的二分法，正好对应 `config.tcl`（主要用配置命令）和 `run.tcl`（主要用通用 + 运行命令）的两文件工作流。

#### 4.3.2 核心流程

把命令按“阶段”重新组织成下表，比 `CommandRef.md` 的原始分类更贴近一次回归测试的时间线：

| 阶段 | 命令 | 对应 `proc` 行号 | 所属区块 |
| --- | --- | --- | --- |
| 初始化 | `init` | 351 | 区块三 |
| 配置·库 | `add_library` | 384 | 区块三 |
| 配置·源文件 | `add_sources` | 435 | 区块三 |
| 配置·测试运行 | `create_tb_run` | 622 | 区块三 |
| 配置·测试运行 | `tb_run_add_arguments` | 679 | 区块三 |
| 配置·测试运行 | `tb_run_add_time_limit` | 689 | 区块三 |
| 配置·测试运行 | `tb_run_add_pre_script` | 651 | 区块三 |
| 配置·测试运行 | `tb_run_add_post_script` | 666 | 区块三 |
| 配置·测试运行 | `tb_run_skip` | 698 | 区块三 |
| 配置·测试运行 | `add_tb_run` | 706 | 区块三 |
| 配置·消息 | `compile_suppress` | 394 | 区块三 |
| 配置·消息 | `run_suppress` | 410 | 区块三 |
| 运行·清理 | `clean_libraries` | 510 | 区块三 |
| 运行·编译 | `compile_files` | 609 | 区块三 |
| 运行·仿真 | `run_tb` | 752 | 区块三 |
| 运行·检查 | `run_check_errors` | 721 | 区块三 |
| 运行·调试 | `launch_tb` | 852 | 区块三 |

可以看出：**所有 17 个导出命令都集中在区块三（接口函数）**，区块一、区块二不直接面向用户。这张表就是后续整个单元 2 的学习路线图。

#### 4.3.3 源码精读

`CommandRef.md` 的命令目录把三类命令列得很清楚：

[CommandRef.md:11-31](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L11-L31) —— General / Configuration / Run 三类命令的完整目录，共 17 个链接。

回到源码，以 `init` 为例，可以看到区块三里“命令注释 + proc + namespace export”的标准三段式写法：

[PsiSim.tcl:347-379](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L347-L379) —— `init` 的用法注释（说明它必须是第一个命令、`-ghdl`/`-vivado` 参数），过程体，以及末尾的 `namespace export init`。这套“注释即文档”的写法贯穿整个区块三，所以读源码本身就能看到每个命令的用法。

`compile` 与 `compile_files` 的关系值得特别留意——它是“命令分类”里唯一一个“同名陷阱”：

[PsiSim.tcl:548-613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L548-L613) —— `compile`（第 548 行）是真正干活的实现，但它**没有** `namespace export`；紧随其后的 `compile_files`（第 609 行）只是一个薄包装，把参数转交 `compile`，并 `namespace export compile_files`。这样做的理由写在了第 608 行的注释里：“Wrapper to prevent name clash with modelsim 'compile'”——因为 Modelsim 自己也有一个叫 `compile` 的命令，为了避免在 Modelsim 控制台里冲突，PsiSim 对外只暴露 `compile_files` 这个名字。

#### 4.3.4 代码实践

**实践目标**：把“文档命令”与“源码 proc”一一对应，建立可检索的索引（即本讲命令地图的动手版）。

**操作步骤**：

1. 打开 `CommandRef.md`，从第 11–31 行的目录里抄下全部 17 个命令名。
2. 对每个命令名，在 `PsiSim.tcl` 中搜索 `proc <命令名>`，记下所在行号。
3. 在 `PsiSim.tcl` 中搜索 `namespace export <命令名>`，确认它确实被导出（`compile` 应当搜不到 export，从而验证它是内部过程）。
4. 把结果填进 4.3.2 节那样的表格。

**需要观察的现象**：

- 每个导出命令的 `proc` 末尾都紧跟一行 `namespace export <命令名>`；
- `compile`（第 548 行）是唯一一个“有 proc、有完整实现、却没有 `namespace export`”的用户级命令，它的对外名字是 `compile_files`。

**预期结果**：你将得到一张与 4.3.2 节一致的“命令→行号”对照表。这张表就是后续读单元 2 各篇讲义时的“快速跳转表”。

#### 4.3.5 小练习与答案

**练习 1**：`CommandRef.md` 把命令分成 General / Configuration / Run 三类。如果按“是否真正调用仿真器命令”来分，哪些命令属于“会触发仿真器”的一类？

**参考答案**：直接或间接触发仿真器的主要是 Run 类命令：`clean_libraries`（→ `sal_clean_lib`）、`compile_files`（→ `sal_compile_file`）、`run_tb`（→ `sal_run_tb`）、`launch_tb`（→ `sal_launch_tb`/`sal_run_tb`/`sal_open_wave`）；而 `run_check_errors` 只读 transcript 文件、不调用仿真器。配置类命令只改状态变量，不碰仿真器。

**练习 2**：为什么用户文档推荐使用 `compile_files`，而源码里却有一个 `compile`？

**参考答案**：`compile` 是真正的实现过程，但它与 Modelsim 自带的 `compile` 命令重名。为了让 PsiSim 的命令在 Modelsim 控制台里也能安全使用，作者刻意不给 `compile` 加 `namespace export`，而是提供 `compile_files` 这个包装（第 609 行）作为对外名字，内部再 `eval "compile ..."` 调用真正的 `compile`。

---

## 5. 综合实践

**任务**：为 `PsiSim.tcl` 制作一张“一页纸架构地图”，把本讲三个模块的知识串起来。

要求你在一张图（手绘或文本均可）上同时体现：

1. **仓库层**：6 个文件，标注每个文件的一句话用途（参考 4.1 节）。
2. **文件内部层**：`PsiSim.tcl` 的三大区块及其精确行号区间，并标出“状态变量 / SAL / 接口函数”三层之间的调用方向（参考 4.2 节那棵树）。
3. **命令层**：在“接口函数”区块里，按“初始化 / 配置 / 运行”三个阶段把 17 个命令排成时间线，并各挑一个命令标注它的 `proc` 行号（参考 4.3 节）。

**检查清单**：

- [ ] 图上能一眼看出 `PsiSim.tcl` 是唯一源码、其余是文档/法律文件；
- [ ] 图上能看到“上层接口函数 → 调用 → 下层 SAL → 屏蔽 → 三种仿真器”的纵向关系，以及所有层都读写“状态变量”的横向关系；
- [ ] 图上能看出 `init` 一定排在最前、`run_check_errors` 排在 `run_tb` 之后；
- [ ] 图上特别标注了 `compile`（内部，第 548 行）与 `compile_files`（导出，第 609 行）的区别。

完成后，这张地图就是你阅读后续所有讲义时的“总目录”。每读一篇，都可以把对应命令在地图上点亮一处。

> 待本地验证：本实践是源码阅读型任务，不涉及运行仿真器，无需 Modelsim/GHDL/Vivado 环境即可完成。

## 6. 本讲小结

- PsiSim 仓库只有 6 个文件、无子目录：唯一源码 `PsiSim.tcl`（966 行）+ 三个文档（`README.md`/`CommandRef.md`/`Changelog.md`）+ 两个许可证文件。
- `PsiSim.tcl` 全部内容包裹在 `namespace eval psi::sim { ... }`（第 14–966 行）之中。
- 文件内部分三大区块：**状态变量**（第 16–26 行，10 个变量）、**SAL 抽象层**（第 28–342 行，13 个 `sal_*` 内部过程）、**接口函数**（第 344–965 行，17 个导出命令 + 2 个内部过程）。
- 三层之间的调用关系是：用户脚本 → 接口函数 → SAL → 实际仿真器命令；所有层共享状态变量。
- 17 个导出命令按时间线可分为：初始化（`init`）、配置（建库/加源/定义测试运行/消息抑制）、运行（清理/编译/仿真/检查/调试）。
- `compile`（第 548 行）因与 Modelsim 命令重名而特意不导出，对外只暴露包装 `compile_files`（第 609 行）——这是读源码时要留意的唯一“同名陷阱”。

## 7. 下一步学习建议

本讲建立了“地图”，下一篇 **u1-l3（两文件工作流与首次运行）** 将带你**走第一条路线**：用 `README.md` 里的示例把 `config.tcl` + `run.tcl` 跑起来，亲眼看到一次完整的“init → source 配置 → compile_files → run_tb → run_check_errors”流程。

在进入单元 2 之前，建议你：

- 把本讲的“一页纸架构地图”放在手边；
- 重读 `CommandRef.md` 的命令目录（[CommandRef.md:11-31](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L11-L31)），对 17 个命令的名字混个脸熟即可，不必现在记住参数；
- 单元 2 将按“状态变量 → Sources 模型 → TbRuns 模型 → 消息抑制 → 编译 → 运行 → 错误检查”的顺序，逐一拆解区块三里的配置与运行命令——那张命令地图就是单元 2 的目录。
