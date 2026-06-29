# 仓库结构与仿真运行

## 1. 本讲目标

上一讲（u1-l1）我们从 README 建立了对「脉动阵列做矩阵乘法」的直觉，但没有真正动过手。本讲要回答一个最朴素的问题：**把这个项目下载到本地后，我该怎么把它跑起来？**

读完本讲，你应当能够：

1. 说出仓库里 `rtl/`、`tb/`、`FPGA/`、`images/` 四个目录各自的职责，以及整体只依赖两个工具：Verilator 与 GNU Make。
2. 理解 `tb/Makefile` 是如何把「编译 → 构建 → 仿真 → 看波形」串成一条流水线的，并能说出每个 `make` 目标背后实际执行的命令。
3. 独立执行 `cd tb && make all`，看懂控制台每个阶段的输出，并解释生成的 `waveform.vcd` 是怎么来的。
4. 知道 `make clean`、`make lint` 的作用，以及哪些生成文件会被 `.gitignore` 忽略。

## 2. 前置知识

在进入源码前，先用大白话过一遍本讲会用到的几个概念。如果你已经熟悉，可以跳到第 3 节。

### 2.1 什么是 RTL 与 SystemVerilog

RTL（Register Transfer Level，寄存器传输级）是描述数字电路的一种代码风格：你用代码写「在时钟上升沿，把 A 寄存器的值赋给 B 寄存器」，综合工具再把它翻译成真实的逻辑门或 FPGA 资源。SystemVerilog（缩写 SV）是写 RTL 最常用的语言之一，本项目的 `.sv` 文件就是 SystemVerilog 源码。

### 2.2 什么是 Verilator

硬件代码不能直接像 C 程序那样「运行」。要验证它对不对，需要做**仿真（simulation）**：用软件模拟电路在时钟驱动下的行为。Verilator 是一个开源仿真器，它的工作方式很特别——它不是去解释执行硬件代码，而是**先把 SystemVerilog 翻译成 C++ 代码**，再和你的 C++ 测试台一起编译成一个普通的可执行程序。跑这个程序，就相当于「运行」了硬件。

这样做的好处是速度非常快（比传统事件驱动仿真器快一到两个数量级）。所以本项目的流程可以概括成：

\[ \text{SystemVerilog RTL} \xrightarrow{\text{Verilator}} \text{C++ 代码} \xrightarrow{\text{g++}} \text{可执行程序} \xrightarrow{\text{运行}} \text{仿真结果 + 波形} \]

### 2.3 什么是 GNU Make 与 Makefile

`make` 是一个「任务编排」工具。你把任务之间的依赖关系写在一个叫 `Makefile` 的文件里，比如「想得到仿真可执行程序，必须先完成编译」，`make` 就会自动按依赖顺序一步步执行，并且**只在源文件变化时才重新生成**。本项目用 Makefile 把 Verilator 的多步流程串起来，所以我们只需敲一条 `make all` 就能跑完整个流程。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用它 |
|------|------|--------------|
| `README.md` | 项目说明书，TL;DR 段落给出运行依赖与命令 | 确认工具依赖、运行命令、改规模的入口 |
| `tb/Makefile` | 仿真流程的「编排脚本」 | 本讲核心，逐行讲解四阶段流水线 |
| `.gitignore` | 告诉 git 哪些是生成文件、不要提交 | 解释仿真产物如何管理 |

此外，本讲会提到（但不深入）这几个目录的用途：

- `rtl/`：SystemVerilog 硬件源码（`pe.sv`、`systolicArray.sv`、`topSystolicArray.sv`），下一讲起逐步精读。
- `tb/`：测试台，包含 `tb_topSystolicArray.cpp`（C++ 测试程序）和 `Makefile`。
- `FPGA/`：Intel Quartus 工程（`.qpf`/`.qsf`），用于把设计综合到真实 FPGA，将在专家层 u3-l3 讲。
- `images/`：README 用到的示意图与资源截图。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看**目录结构与运行依赖**，再精读 **Makefile 的四阶段流水线**，最后看 **clean / lint 与生成文件管理**。

### 4.1 目录结构与运行依赖

#### 4.1.1 概念说明

一个能「开箱即跑」的硬件项目，通常需要让读者一眼看清三件事：

1. 源码放在哪、测试放在哪；
2. 需要装哪些工具；
3. 用什么命令把项目跑起来。

本项目把这三件事做得非常克制——目录极简，依赖只有两个工具。这是我们能很快上手的基础。

#### 4.1.2 核心流程

仓库的完整目录结构如下（基于 `git ls-files` 的实际结果）：

```
2D-Systolic-Array-Multiplier/
├── README.md              # 项目说明 + TL;DR 运行方法
├── LICENSE
├── .gitignore             # 仿真生成文件忽略规则
├── rtl/                   # 硬件源码 (SystemVerilog)
│   ├── topSystolicArray.sv
│   ├── systolicArray.sv
│   └── pe.sv
├── tb/                    # 测试台
│   ├── Makefile           # 仿真流程编排（本讲主角）
│   └── tb_topSystolicArray.cpp
├── FPGA/                  # Quartus FPGA 工程（综合用）
│   ├── 2D-Systolic-Array-Multiplier.qpf
│   └── 2D-Systolic-Array-Multiplier.qsf
└── images/                # README 示意图与资源截图
```

整个项目的运行依赖，README 只列了两个：

> Requirements: `Verilator` and `GNU Make`.

也就是说：装好 Verilator（用于把 SV 翻译成 C++ 并仿真）和 GNU Make（用于编排流程），就能跑通仿真，不需要任何商业 EDA 工具。

#### 4.1.3 源码精读

README 的 TL;DR 段落直接给出了从克隆到运行的步骤：

[README.md:10-31](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L10-L31) —— 给出运行依赖、默认规模（4×4）以及一键运行命令 `cd tb && make all`。

其中两行最关键：

- 第 12 行：`Requirements: Verilator and GNU Make.` 声明了全部外部依赖。
- 第 30 行：`cd tb && make all` 是唯一的运行入口。

README 还说明了如何修改矩阵规模：改 `rtl/topSystolicArray.sv` 的参数 `N`，以及测试台里的宏 `N`。这里有一个**需要注意的小坑**：README 第 26 行把测试台文件写成了 `tb_topSystolicArray.sv`，但仓库里实际存在的文件是 `tb/tb_topSystolicArray.cpp`（注意扩展名是 `.cpp`）。改规模时认准这个 `.cpp` 文件即可，这一处是 README 的笔误。

> 提示：默认规模为 N=4（即 4×4 方阵），这是后续所有讲义的默认参数。

#### 4.1.4 代码实践

1. **实践目标**：建立对仓库布局的直观认识，找到本讲后续要用到的所有文件。
2. **操作步骤**：
   - 在仓库根目录执行 `git ls-files`（或直接 `ls -R`），对照上面的目录树核对。
   - 用编辑器打开 `README.md`，定位到 TL;DR 段落（第 10 行附近）。
3. **需要观察的现象**：确认存在 `rtl/`、`tb/`、`FPGA/`、`images/` 四个目录，且 `tb/` 下只有 `Makefile` 和 `tb_topSystolicArray.cpp` 两个被 git 跟踪的文件。
4. **预期结果**：目录结构与 4.1.2 中的树一致；没有多余的源码目录。
5. 运行结果：**待本地验证**（取决于你的本地工作树是否干净）。

#### 4.1.5 小练习与答案

**练习 1**：项目要求安装哪两个工具？为什么这两个就够了？
**答案**：Verilator 和 GNU Make。Verilator 既负责把 SystemVerilog 翻译成 C++、又负责生成可执行程序进行仿真；GNU Make 负责按依赖关系编排这些步骤。两者配合即可完成「编译 → 仿真」，不需要商业仿真器。

**练习 2**：README 说要修改规模需要改两个文件，分别是哪两个？实际测试台的扩展名是什么？
**答案**：`rtl/topSystolicArray.sv` 的参数 `N` 与 `tb/tb_topSystolicArray.cpp` 的宏 `N`。测试台实际扩展名是 `.cpp`（README 中误写为 `.sv`）。

---

### 4.2 Makefile 的 verilate→build→sim→waves 流水线

#### 4.2.1 概念说明

Makefile 的核心思想是「**目标（target）依赖前置产物**」。当你请求一个目标时，`make` 会先检查它的依赖是否最新，不最新就先去更新依赖，再执行本目标的命令。

本项目的 Makefile 把一次完整仿真拆成四个阶段，构成一条流水线。Makefile 第 1 行的注释直接点明了这条链：

> `make all` OR `make verilate` -> `make build` -> `make sim` -> `make waves`

#### 4.2.2 核心流程

四个阶段的职责与产物如下表：

| 阶段（make 目标） | 作用 | 依赖 | 关键产物 |
|------------------|------|------|----------|
| `verilate` | 用 Verilator 把 SV 翻译成 C++ | RTL 源码 + C++ 测试台 | `obj_dir/` 下的 C++ 源、`.stamp.verilate` 时间戳 |
| `build` | 把生成的 C++ 编译成可执行程序 | verilate 产物 | `obj_dir/VtopSystolicArray` |
| `sim` | 运行可执行程序，产生波形 | build 产物 | `waveform.vcd` |
| `waves` | 用 gtkwave 打开波形看时序 | sim 产物 | gtkwave GUI 窗口 |

它们之间的依赖链（Makefile 用「文件依赖」表达）是：

```
../rtl/topSystolicArray.sv ┐
tb_topSystolicArray.cpp    ┴──> .stamp.verilate
                                    │
                                    ▼
                          obj_dir/VtopSystolicArray
                                    │
                                    ▼
                              waveform.vcd
                                    │
                                    ▼
                          (gtkwave 查看波形)
```

> 关键直觉：`make` 用**文件的时间戳**判断是否需要重做。如果你只改了测试台 C++ 而没动 RTL，单独 `make build` 会触发 verilate（因为 `.stamp.verilate` 依赖那个 cpp），再编译；如果你什么都没改，单独 `make sim` 会发现 `waveform.vcd` 已是最新而**跳过**重跑——这正是 Make 的增量构建优势。但 `make all` 会先执行 `clean` 强制全部重来，详见 4.3 节。

#### 4.2.3 源码精读

Makefile 开头定义了两个变量，被后续所有规则复用：

[tb/Makefile:3-4](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L3-L4) —— `TOP_MODULE=topSystolicArray` 指定顶层模块名，`RTL_PATH=../rtl` 指向 RTL 源码目录。

`all` 目标把五个动作串起来，注意它**第一个依赖是 `clean`**，所以每次 `make all` 都会先清空再全跑：

[tb/Makefile:6-7](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L6-L7) —— `all: clean verilate build sim waves`。

**阶段 1：verilate**

[tb/Makefile:34-38](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L34-L38) —— 规则 `.stamp.verilate` 依赖 RTL 与测试台，变化时调用 `verilator`，完成后 `touch` 一个时间戳文件标记「已 verilate」。

这一行的 verilator 命令信息量很大，逐个 flag 解释：

| flag | 含义 |
|------|------|
| `-Wall` | 打开所有警告（warning all），严格检查 |
| `--trace` | 生成 VCD 波形记录代码（这样仿真才能 dump 出 `waveform.vcd`） |
| `--x-assign unique` / `--x-initial unique` | 对未初始化的 X 值用「唯一确定性随机」方式赋初值，便于发现 X 传播 bug |
| `-cc` | 生成 C++（而非 SystemC）形式的模型 |
| `-I../rtl/` | 头文件/包含路径，指向 RTL 目录 |
| `../rtl/topSystolicArray.sv` | 顶层 RTL 源文件 |
| `--exe tb_topSystolicArray.cpp` | 把这个 C++ 测试台一并纳入，生成可仿真的可执行程序 |

**阶段 2：build**

[tb/Makefile:29-32](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L29-L32) —— 规则 `obj_dir/VtopSystolicArray` 依赖 `.stamp.verilate`，通过 `make -C obj_dir -f VtopSystolicArray.mk VtopSystolicArray` 把 Verilator 生成的 C++ 编译链接成可执行程序。Verilator 会顺带生成一个 `.mk` 文件，这里就是复用它来驱动 g++。

**阶段 3：sim**

[tb/Makefile:24-27](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L24-L27) —— 规则 `waveform.vcd` 依赖可执行程序，运行 `./obj_dir/VtopSystolicArray +verilator+rand+reset+2`。这里 `+verilator+rand+reset+2` 是一个 Verilator 运行时参数：它让仿真在复位阶段用种子 2 产生确定性随机值，配合 verilate 阶段的 `--x-initial unique`，确保未初始化寄存器不会因为全是 0 而掩盖潜在 bug。这一步会真正「跑硬件」，并因 `--trace` 而写出 `waveform.vcd`。

**阶段 4：waves**

[tb/Makefile:18-22](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L18-L22) —— `waves` 目标同样依赖 `waveform.vcd`（此时该文件已被 sim 阶段生成），命令是 `gtkwave waveform.vcd`，弹出图形界面查看波形。

> 注意：`sim` 和 `waves` 都依赖 `waveform.vcd`。在 `make all` 的链路里，`sim` 先生成 vcd，`waves` 随后只是打开它，不会重复跑仿真。**`waves` 阶段需要本地装了 gtkwave**；在无图形界面的服务器上，`make sim`（只到生成 vcd 为止）通常是更稳妥的选择。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到四阶段流水线依次执行，并理解每个 `make` 目标触发哪条命令。
2. **操作步骤**：
   - 先单独跑 `make verilate`，观察控制台打印 `### VERILATING ###` 及 verilator 命令；确认生成了 `obj_dir/` 目录与 `.stamp.verilate`。
   - 再 `make build`，看到 `### BUILDING SIM ###`，确认生成 `obj_dir/VtopSystolicArray`。
   - 再 `make sim`，看到 `### SIMULATING ###`，并在控制台打印出输入矩阵与期望结果矩阵；确认生成 `waveform.vcd`。
   - （可选）`make waves` 打开波形（需要 gtkwave）。
3. **需要观察的现象**：每个阶段开头都有 `### XXX ###` 横幅提示当前阶段；上一阶段没做时，`make` 会自动补做依赖（例如直接 `make sim` 会先 verilate、build）。
4. **预期结果**：`tb/` 下依次出现 `obj_dir/`、`obj_dir/VtopSystolicArray`、`waveform.vcd`；仿真正常时控制台打印输入/输出矩阵且不报错。
5. 运行结果：**待本地验证**（具体矩阵数值由测试台随机生成，每次可能不同）。

#### 4.2.5 小练习与答案

**练习 1**：如果我只想快速检查 RTL 语法、不想生成可执行程序，应该用哪个 make 目标？为什么它最快？
**答案**：用 `make lint`（见 4.3 节）或单独 `make verilate`。`lint` 用 `--lint-only` 只做语法/警告检查不产出代码；`verilate` 只到生成 C++ 为止，不编译不仿真，所以比 `make all` 快很多。

**练习 2**：为什么 `make sim` 单独执行时，verilate 和 build 也会自动发生？
**答案**：因为 `waveform.vcd` 这个目标依赖 `obj_dir/VtopSystolicArray`，后者又依赖 `.stamp.verilate`。`make` 沿依赖链向上检查，发现这些前置产物不存在或不新鲜，就会自动先执行它们，从而 verilate→build→sim 依次完成。

**练习 3**：sim 阶段命令里的 `+verilator+rand+reset+2` 起什么作用？
**答案**：它是 Verilator 运行时参数，让仿真在复位时用种子 2 产生确定性随机初值，配合 `--x-initial unique`，使未初始化寄存器带有非平凡初值，从而更容易暴露 X 传播相关的问题。

---

### 4.3 clean / lint 目标与生成文件管理

#### 4.3.1 概念说明

仿真会产生大量「中间产物」：Verilator 生成的 C++ 文件、编译出的可执行程序、波形文件……这些都不应该提交到 git。一个干净的项目会用两种手段管理它们：

1. **`make clean`**：一键删除所有生成产物，让工作树回到「只有源码」的干净状态。
2. **`.gitignore`**：即使产物存在，也告诉 git 不要追踪它们。

此外，`make lint` 提供了一个轻量的「只检查不生成」入口，方便在写代码时快速发现 RTL 的语法或风格问题。

#### 4.3.2 核心流程

生成文件的生命周期如下：

```
源码 (rtl/*.sv, tb/*.cpp)
        │  make verilate
        ▼
obj_dir/ (生成 C++)  ──┐
        │ make build   │ make clean
        ▼              │
obj_dir/VtopSystolicArray ├──> 全部删除，回到干净状态
        │ make sim     │
        ▼              │
waveform.vcd ──────────┘
        │ make waves
        ▼
   gtkwave (查看)
```

而 `.gitignore` 则保证这条链路上产生的 `obj_dir/`、`waveform.vcd`、`.gtkw`（gtkwave 信号列表）等永远不进版本库。

#### 4.3.3 源码精读

**clean 目标**

[tb/Makefile:44-48](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L44-L48) —— 删除 `.stamp.*` 时间戳、`obj_dir/` 和 `waveform.vcd`。

注意三点：

1. `clean` 是 `all` 的**第一个**依赖，所以 `make all` ≡ 「先 clean 再跑完整流程」，每次都是全新构建。
2. `clean` 用 `rm -rf`，没有 `-i` 之类确认，执行要谨慎（不过它只删 `tb/` 内的生成物，不碰源码）。
3. 它**没有**删 `tb/*.gtkw`（gtkwave 的信号保存文件）——如果你存了波形配置，clean 不会清掉它。

**lint 目标**

[tb/Makefile:40-42](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L40-L42) —— 用 `verilator --lint-only` 只做语法与警告检查，不生成任何 C++，也不需要测试台。这是改完 RTL 后最快的「自检」方式。

**.gitignore 规则**

[.gitignore:1-15](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/.gitignore#L1-L15) —— 列出全部被忽略的生成物。

逐条对照：

| `.gitignore` 规则 | 对应的生成物 | 产生阶段 |
|------------------|--------------|----------|
| `/.vscode` | VSCode 编辑器配置 | （编辑器产生） |
| `/tb/obj_dir` | Verilator 生成的 C++ 与可执行程序 | verilate / build |
| `tb/waveform.vcd` | 仿真波形 | sim |
| `tb/*.gtkw` | gtkwave 保存的信号列表 | waves（手动保存） |
| `tb/*.verilate` | Verilator 相关生成文件 | verilate |

> 小细节：Makefile 用的时间戳文件叫 `.stamp.verilate`，文件名形如 `*.verilate`（`*` 匹配 `.stamp`），因此它也落在 `tb/*.verilate` 这条忽略规则里，不会被提交。

#### 4.3.4 代码实践

1. **实践目标**：验证 `clean` 与 `.gitignore` 的行为一致——clean 删掉的东西，正是 git 不追踪的东西。
2. **操作步骤**：
   - 先执行 `cd tb && make all`（或至少 `make sim`）生成全部产物。
   - 在仓库根目录执行 `git status`，确认工作树「干净」（无新增待提交文件），说明所有产物都被忽略。
   - 执行 `cd tb && make clean`，再用 `ls` 确认 `obj_dir/`、`waveform.vcd`、`.stamp.verilate` 都已消失。
3. **需要观察的现象**：`git status` 在有大量生成文件时仍显示 nothing to commit；`make clean` 后 `tb/` 只剩 `Makefile` 和 `tb_topSystolicArray.cpp`。
4. **预期结果**：生成产物与 `.gitignore` 列表完全对应；clean 后目录回到「源码+Makefile」的干净状态。
5. 运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make all` 每次都像「全新构建」，而单独 `make build` 却可能什么都不做？
**答案**：因为 `all` 依赖 `clean`，第一步就把 `.stamp.verilate`、`obj_dir/` 等删掉，后续阶段判定产物缺失便全部重跑；而单独 `make build` 不会 clean，若源码没变，`make` 看到 `obj_dir/VtopSystolicArray` 比依赖新，就直接跳过。

**练习 2**：`.gitignore` 为什么要忽略 `tb/obj_dir`？如果不忽略会怎样？
**答案**：`obj_dir/` 是 Verilator 自动生成的中间产物（成百上千个 C++ 文件、可执行程序等），会频繁变化且与平台/工具版本相关。提交它们会污染版本库、制造无意义的 diff，所以必须忽略。

**练习 3**：`make lint` 与 `make verilate` 都调用 verilator，最大区别是什么？
**答案**：`lint` 用 `--lint-only`，只做语法/警告检查，**不生成任何代码**，也不依赖测试台；`verilate`（`-cc ... --exe ...`）会真正把 SV 翻译成 C++ 并纳入测试台，为后续 build 做准备。前者更快，适合写代码时随手自检。

## 5. 综合实践

把本讲三个模块串起来，完成一次「完整的仿真 + 产物管理」体验：

1. **克隆并进入项目**（若已克隆则跳过）：`git clone https://github.com/tms4517/2D-Systolic-Array-Multiplier.git && cd 2D-Systolic-Array-Multiplier`。
2. **确认依赖**：在终端执行 `verilator --version` 与 `make --version`，确认两个工具都已安装。
3. **跑完整流程**：执行 `cd tb && make all`，按本讲 4.2.3 的说明，把控制台里每个 `### XXX ###` 横幅对应到 verilate/build/sim/waves 四个阶段，并记录每个阶段实际触发的命令。
4. **核对产物**：在仓库根目录 `git status`，确认生成文件全部被忽略（工作树干净）；再确认 `tb/waveform.vcd` 已生成。
5. **（可选）看波形**：执行 `make waves`（需 gtkwave），观察 `o_validResult` 拉高时 `o_c` 出现结果矩阵的过程。
6. **清理**：执行 `make clean`，确认 `tb/` 恢复到只有 `Makefile` 与 `tb_topSystolicArray.cpp`。

**验收标准**：能复述四阶段流水线的依赖链、能说出每个 `make` 目标对应的命令、能解释 `make all` 为何每次全量重跑、能说明哪些产物被 `.gitignore` 忽略。

> 说明：以上命令的精确输出（尤其是仿真打印的矩阵数值）依赖于本地工具版本与随机种子，因此矩阵内容标注为**待本地验证**；但四阶段的触发顺序、产物文件名、依赖关系是 Makefile 决定的，结果稳定可预期。

## 6. 本讲小结

- 项目目录极简：`rtl/`（硬件源码）、`tb/`（测试台+Makefile）、`FPGA/`（Quartus 工程）、`images/`（示意图），外部依赖只有 Verilator 与 GNU Make。
- `tb/Makefile` 把仿真拆成 verilate→build→sim→waves 四阶段，靠「文件依赖 + 时间戳」实现增量构建；命令入口是 `cd tb && make all`。
- verilate 阶段的 `verilator -Wall --trace ... -cc ... --exe ...` 把 SV 翻译成 C++；build 用生成的 `.mk` 编译出可执行程序；sim 运行它产出 `waveform.vcd`；waves 用 gtkwave 查看。
- `sim` 命令里的 `+verilator+rand+reset+2` 与 `--x-initial unique` 配合，让未初始化寄存器带确定性随机初值，更易暴露 X 传播问题。
- `make all` 因首依赖 `clean` 而每次全量重跑；`make lint` 用 `--lint-only` 提供最快的 RTL 自检。
- `.gitignore` 忽略 `obj_dir/`、`waveform.vcd`、`*.gtkw`、`*.verilate` 等全部生成物，与 `clean` 删除范围基本对应。

## 7. 下一步学习建议

现在你已经能跑通仿真，接下来该看「仿真到底在跑什么硬件」。建议按以下顺序继续：

1. **u1-l3 顶层接口与数据流总览**：打开 `rtl/topSystolicArray.sv`，从端口表理解 `i_validInput → 变换 → 阵列 → o_validResult` 的整体通路，把本讲看到的波形信号对应到源码。
2. 之后进入进阶层（u2），自底向上精读 `pe.sv`（处理单元）、`systolicArray.sv`（阵列互联）、`topSystolicArray.sv`（矩阵变换与控制）。
3. 想深入理解测试台本身（如何生成随机矩阵、如何比对结果），可提前阅读 `tb/tb_topSystolicArray.cpp`，对应专家层 u3-l1。

> 一个好习惯：从本讲起，每次读完一个模块都回到 `make sim` 跑一次仿真，用波形对照源码加深理解——这正是本项目「源码 + 波形双对照」的设计初衷。
