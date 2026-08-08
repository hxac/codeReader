# 构建参数与集成选项

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ZipCPU 的 `OPT_*` 系列参数是「综合期剪刀」而非「运行时开关」，并解释这意味着什么。
- 逐个说出主要参数（`OPT_MPY` / `OPT_DIV` / `OPT_CIS` / `OPT_DCACHE` / `OPT_USERMODE` / `RESET_ADDRESS` 等）的含义、默认值与取舍。
- 读懂 `zipcore.v` 的参数列表与 `generate if` 裁剪逻辑，知道「关闭某参数」在电路层面到底裁掉了什么。
- 看懂顶层封装 `zipaxil.v` 如何用自己的 `localparam` 把 `OPT_LGDCACHE` 这类「对数尺寸」参数派生成内核需要的 `OPT_DCACHE` 开关，再下传给 `zipcore`。
- 针对一块资源受限的 FPGA，挑出一组合法的 `OPT_*` 配置，并说明每项裁剪省下了什么、失去了什么。

## 2. 前置知识

本讲是「专家层」讲义，默认你已经具备以下认知（这些都在前置讲义中讲过）：

- **软核 CPU 与 RTL**：ZipCPU 是用 Verilog 写的 32 位 RISC 软核，要被「综合（synthesize）」进 FPGA 才能变成真实电路（见 u1-l1）。
- **综合期参数**：`zipcore` 用 `OPT_*` 参数配合 `generate if` 在综合时裁剪电路，关闭一个参数即不生成对应硬件（见 u3-l1）。
- **流水线与五级结构**：取指 → 译码 → 读操作数 → 执行+访存 → 写回（见 u3-l1）。
- **取指/访存模块族**：`prefetch`/`pfcache`、`memops`/`pipemem`/`dcache` 由外壳按参数选用（见 u3-l2、u3-l6）。
- **顶层封装**：`zipaxil`（AXI4-Lite）直接实例化 `zipcore`，指令/数据总线分离（见 u1-l3、u4-l3）。

两个本讲要用到、但可能你还不熟的术语：

- **综合（synthesis）**：把 Verilog 代码翻译成 FPGA 上的查找表（LUT）、触发器（FF）和块 RAM（BRAM）的过程。综合期参数在「这一步」定型，运行时无法再改。
- **LUT / FF / DSP / BRAM**：FPGA 的四种基本资源。逻辑用 LUT，寄存器用 FF，硬件乘法器叫 DSP，片上存储块叫 BRAM。面积（area）就是这些资源的占用。
- **关键路径（critical path）**：电路里最长的一组合逻辑链，决定能跑到的最高时钟频率。

## 3. 本讲源码地图

本讲涉及三个关键源文件：

| 文件 | 作用 |
| --- | --- |
| `doc/src/spec.tex` | ISA 与集成规范的权威来源。第「Integration / ZipCPU Parameters」一节逐条解释了每个参数，是本讲的「说明书」。 |
| `rtl/core/zipcore.v` | CPU 内核。开头列出全部 `OPT_*` 参数（综合期剪刀），正文用大量 `generate if` 按这些参数裁剪电路。 |
| `rtl/zipaxil.v` | AXI4-Lite 顶层封装。它定义面向集成者的参数（如 `OPT_LGDCACHE`、`RESET_ADDRESS`），用 `localparam` 派生后传给内核。 |

> 小提示：spec 的参数说明偏「理念」，`zipcore.v` / `zipaxil.v` 才是「事实标准」。当二者措辞有出入时，以 RTL 为准（本讲第 4.3 节会给出一个真实例子）。

## 4. 核心概念与源码精读

### 4.1 参数即「综合期剪刀」：OPT_* 的工作机制

#### 4.1.1 概念说明

很多 CPU 的「配置」是运行时寄存器——软件写一位就能开关某功能。ZipCPU 的 `OPT_*` 参数不是这样：它们是 **Verilog 模块的 `parameter`**，在综合时就被求值成常量，配合 `generate if` 决定「这块电路到底生不生成」。

打个比方：运行时开关像「房间里能随手关掉的灯」，综合期参数像「盖房子时就决定要不要留这间房」。一旦房子盖好（比特流烧进 FPGA），没有这间房就是没有，软件再怎么写也开不出灯来。

这样做的好处是 **零浪费**——不用的功能连一根线、一个 LUT 都不占，对「轻量」这个核心设计目标至关重要。代价是 **改配置必须重新综合**，不能在运行时调整。

#### 4.1.2 核心流程

参数从「集成者」流到「电路」要经过三步：

1. **集成者设值**：在你的顶层例化 `zipaxil` / `zipsystem` 时，用 `#(.OPT_MPY(0), ...)` 覆盖默认值。
2. **顶层派生**：封装用 `localparam` 把「人类友好」的参数（如缓存大小的对数 `OPT_LGDCACHE`）算成内核要的「开/关」参数（如 `OPT_DCACHE`）。
3. **内核裁剪**：`zipcore` 里每个 `generate if (OPT_XXX)` 块在综合时被求值——条件为真则保留块内电路，为假则整块丢弃，落选功能通常改报「非法指令异常」。

条件为假时「报非法指令」是个重要细节：裁掉的功能不是「静默不可用」，而是让 CPU 在执行对应指令时陷入异常，这样软件能知道该功能没实现（少数例外见 4.2 节的 `OPT_SHIFTS`）。

#### 4.1.3 源码精读

先看 `zipcore` 的完整参数列表。这是本讲最核心的一张表：

[rtl/core/zipcore.v:L41-L61](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L41-L61) —— 内核的全部综合期参数声明。每行一个 `parameter`，注意右侧注释和默认值。

几个要点：

- `ADDRESS_WIDTH=30` 注释写「32-b word addr width」——内核内部按 **字地址** 计，30 位字地址对应 32 位字节地址空间（见 4.4 节）。
- `OPT_MPY = 0`、`IMPLEMENT_FPU = 0`：内核默认 **不** 含乘法的 DSP 实现和 FPU，但顶层封装会把它改成有意义的值（`zipaxil` 默认 `OPT_MPY=3`）。
- `OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED)`：这个参数 **派生** 自 `OPT_PIPELINED`，体现了「参数之间有依赖」。
- `OPT_CIS = 1'b1`、`OPT_USERMODE = 1'b1`、`OPT_DBGPORT = 1'b1`：内核默认开启压缩指令、用户模式、调试端口。

再看「裁剪」到底长什么样。以除法单元为例：

[rtl/core/zipcore.v:L1527-L1536](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1527-L1536) —— `generate if (OPT_DIV != 0)` 块。`OPT_DIV` 为 0 时，整个 `DIVIDE` 块（含 `div` 模块实例）不生成；执行除法指令会触发非法指令异常。

寄存器堆的大小也由参数裁剪，这是面积影响最直观的一处：

[rtl/core/zipcore.v:L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L166) —— `reg [31:0] regset [0:(OPT_USERMODE)? 31:15];`。开用户模式时是 32 个寄存器（supervisor 16 + user 16），关掉则只剩 16 个，直接省掉一半寄存器堆。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「一个参数对应一块可裁剪的电路」。

**操作步骤**：

1. 在 `rtl/core/zipcore.v` 中搜索 `generate if`，数一下总共有多少处（提示：本讲搜索结果显示约 60 处）。
2. 找到 `generate if (!OPT_DBGPORT)` 这一块。

[rtl/core/zipcore.v:L3369-L3377](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3369-L3377) —— 关闭调试端口时的 `else` 分支：`o_dbg_reg` 被钉死为 0，输入信号被收到 `unused_dbgport` 里「吃掉」以避免综合告警。

3. 对照 `generate if (OPT_DBGPORT)` 分支（紧邻其上），体会「开」与「关」两份代码的体量差异。

**需要观察的现象**：`!OPT_DBGPORT` 分支只有几行 `assign`，把输出接地、把输入「假装使用」；而 `OPT_DBGPORT` 分支是成百行的调试寄存器读写逻辑。这就是「关一个参数省下多少」的最直接证据。

**预期结果**：你能口头说出「关掉 `OPT_DBGPORT` 后，调试相关的所有寄存器读写电路都不会被综合」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OPT_*` 不能做成「运行时由软件写一位来开关」？

**参考答案**：因为它们是 Verilog `parameter`，在综合时就被求值为常量并决定电路生不生成。运行时电路已经定型，没有的硬件无法凭空变出来。做成运行时开关需要保留全部硬件再加多路选择，违背 ZipCPU「轻量」目标。

**练习 2**：`zipcore.v` 里 `OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED)` 这一行说明了参数之间的什么关系？

**参考答案**：参数之间存在 **派生依赖**——`OPT_PIPELINED_BUS_ACCESS` 默认跟随 `OPT_PIPELINED`，即「非流水线模式下默认也不做流水线总线访问」。集成者可以单独覆盖它，但若不覆盖就自动跟随。

---

### 4.2 指令集类参数：决定「CPU 会哪些指令」

#### 4.2.1 概念说明

第一组参数控制 **指令集本身**：某类指令到底有没有硬件实现。这类参数直接决定「程序能不能跑」——关掉某指令后，用到它的程序会触发非法指令异常（或更糟，见 `OPT_SHIFTS`）。

spec 把它们归为「control the instruction set」一类，包含 `OPT_MPY`、`OPT_DIV`、`OPT_SHIFTS`、`OPT_CIS`、`OPT_LOCK`、`OPT_SIM`，再加上内核里的 `IMPLEMENT_FPU`。

#### 4.2.2 核心流程

这些参数的共同行为模式是：

- 关闭 → 对应执行单元不生成 → 指令在译码/执行阶段被判为非法 → 触发非法指令异常（uCC 的 bit8）。
- 但每个参数的「粒度」和「副作用」不同，需要逐一区分。

下表汇总（默认值取自 `zipaxil.v`，因为那是集成者实际面对的接口）：

| 参数 | 含义 | 关闭后果 | 面积影响 |
| --- | --- | --- | --- |
| `OPT_MPY` | 乘法实现算法（0/1/2/3/4/>4 共 6 档） | 乘法 → 非法指令 | 0=无；1–4=占用 DSP；>4=约 33 周期移位加（不占 DSP） |
| `OPT_DIV` | 除法单元 | 除法 → 非法指令 | 省掉 `div` 模块 |
| `OPT_SHIFTS` | 桶形移位器 | **不报错，只移 1 位（执行错误结果）** | 省几百 LUT |
| `OPT_CIS` | 压缩指令译码 | CIS 指令不可用 | 省译码器里的 CIS 逻辑 |
| `OPT_LOCK` | LOCK 原子指令 | LOCK → 非法指令 | 省总线锁逻辑 |
| `IMPLEMENT_FPU` | 浮点单元 | 浮点 → 非法指令 | 省掉（本就未实现的）FPU |
| `OPT_SIM` | 仅仿真用的指令 | 仿真指令退化为 NOOP/非法 | 几乎无面积 |

#### 4.2.3 源码精读

spec 对 `OPT_MPY` 的逐档说明非常细，是理解「同一功能多种实现」的范本：

[doc/src/spec.tex:L3401-L3417](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3401-L3417) —— `OPT_MPY` 的 6 档：0 不做、1–4 用 DSP 的多周期实现、>4 退回约 33 周期的移位加（`slowmpy`）。其中 `OPT_MPY=3` 是 Xilinx 7 系的「主力」，`OPT_MPY=4` 适合 Spartan 6。

`OPT_SHIFTS` 是个「危险开关」，值得单独留意：

[doc/src/spec.tex:L3423-L3440](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3423-L3440) —— 关闭 `OPT_SHIFTS` 后，移位指令 **不会** 报非法指令，而是「悄悄执行错误结果」（只移 1 位）。spec 还警告：**GCC 端口不支持此选项关闭**，所以实际几乎没人关它。

`OPT_DIV` 的裁剪在内核里长这样（已在 4.1.3 看过 `generate if (OPT_DIV != 0)`）。FPU 的裁剪与之同构：

[rtl/core/zipcore.v:L1568-L1575](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1568-L1575) —— `generate if (IMPLEMENT_FPU != 0)` 块。注意块内 FPU 实例大部分被注释掉了，说明 FPU 是预留接口、当前基本未实现，所以「关掉 FPU」几乎是零成本（它本来就没东西可省）。

#### 4.2.4 代码实践

**实践目标**：体会「同一指令，不同实现，不同代价」。

**操作步骤**：

1. 读 spec 的 `OPT_MPY` 段（链接见 4.2.3），把 6 个取值的「周期数」和「是否占 DSP」填进一张表。
2. 在 `rtl/core/` 下找到 `mpyop.v` 与 `slowmpy.v`（u3-l5 讲过），确认 `OPT_MPY` 的 6 档正是由这两个模块 + `generate if` 阶梯选出来的。

**需要观察的现象**：`OPT_MPY=1` 单周期但「关键路径不受保护」；`OPT_MPY=3` 三周期、是 7 系主力；`OPT_MPY>4` 约 33 周期、不占 DSP。代价（周期数）与收益（省 DSP / 提频率）清晰对应。

**预期结果 / 待本地验证**：你能说出「对一块没有 DSP 的 iCE40，只能选 `OPT_MPY>4`；对 Artix-7，`OPT_MPY=3` 是最优」。具体周期数可结合 u3-l5 的 `slowmpy`/`div` 分析，或「待本地验证」通过仿真计数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 spec 特别警告「不要关 `OPT_SHIFTS`」？

**参考答案**：因为关掉后移位指令 **不报错**，而是悄悄只移 1 位——程序会得到错误结果却无明显异常，极难调试；且 GCC 端口依赖移位，关掉后编译出的代码无法正确运行。

**练习 2**：`OPT_MPY=3` 和 `OPT_MPY>4`（即 `slowmpy`）相比，各自适合什么 FPGA？

**参考答案**：`OPT_MPY=3` 依赖 DSP 硬件乘法器，适合 Xilinx 7 系等有 DSP 的器件，速度快（3 周期）；`slowmpy` 用纯逻辑移位加，约 33 周期但不需要 DSP，适合 iCE40 等没有 DSP 的器件，面积也最小。

---

### 4.3 微架构与缓存类参数：决定「CPU 跑多快」

#### 4.3.1 概念说明

第二组参数控制 **微架构**：流水线深度、分支代价、指令/数据缓存。这些参数通常「不影响程序能不能跑」（ISA 不变），但决定 IPC（每周期指令数）和面积。spec 把 `OPT_PIPELINED`、`OPT_EARLY_BRANCHING`、`OPT_LGICACHE`、`OPT_LGDCACHE` 归入此类。

核心权衡是 spec 开篇那句：「LUTs and RAMs can be traded for performance」——逻辑和存储可以换性能。

#### 4.3.2 核心流程

关键概念是 **缓存的「对数尺寸」参数 `OPT_LGICACHE` / `OPT_LGDCACHE`**。它们不是简单的 0/1 开关，而是「以 2 为底的缓存大小对数」，封装据此派生出三种状态：

- 极小值 → 无缓存（基础访存控制器）。
- 中间值 → 无缓存的流水线访存器（允许在途请求，但无 tag RAM）。
- 较大值 → 真正带 tag 的缓存。

派生规则在 `zipaxil.v` 里写得很清楚（见 4.3.3）。值得注意的是 spec 文字与 RTL 阈值有细微出入，本讲特别点出。

#### 4.3.3 源码精读

先看 spec 对 `OPT_PIPELINED` 和缓存参数的说明：

[doc/src/spec.tex:L3351-L3359](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3351-L3359) —— `OPT_PIPELINED`：关掉主要简化停顿逻辑、去掉各级重复寄存器，让流水线同时只容纳一条指令。spec 明说这「不会根本改变单条指令的执行节奏」——即省的是面积，不是单指令速度。

[doc/src/spec.tex:L3377-L3395](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3377-L3395) —— `OPT_LGICACHE` / `OPT_LGDCACHE` / `OPT_LOWPOWER` 的说明。注意 spec 说数据缓存「大于 2」才指定缓存大小。

再看 RTL 实际的派生阈值（这是 spec 与代码有出入的真实例子）：

[rtl/zipaxil.v:L305-L309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L305-L309) —— `zipaxil` 的 localparam 派生：

- `OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED) && (OPT_LGDCACHE > 1)`
- `OPT_DCACHE = (OPT_LGDCACHE > 4)`
- `FETCH_LIMIT = (OPT_LGICACHE < 4) ? (1 << OPT_LGICACHE) : 16`

注意：spec 文字说数据缓存阈值是「>2」，而 **当前 RTL 用的是 `OPT_LGDCACHE > 4`** 才开启 `dcache`。这是本讲要给你的一个重要习惯——**当 spec 措辞和 RTL 不一致时，以 RTL 为准**。据此，`zipaxil` 默认 `OPT_LGDCACHE=0` 时，`OPT_DCACHE=0`、`OPT_PIPELINED_BUS_ACCESS=0`，即默认 **没有数据缓存、也没有流水线总线访问**，与 u4-l3 的结论一致。

内核侧，`OPT_DCACHE` 控制的是「清数据缓存」信号是否生成：

[rtl/core/zipcore.v:L3285-L3305](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3285-L3305) —— `generate if (OPT_DCACHE)` 生成 `r_clear_dcache` 清缓存逻辑；`else` 把 `o_clear_dcache` 接地。这印证了「关缓存 = 连清缓存的控制位都省掉」。

#### 4.3.4 代码实践

**实践目标**：把「对数尺寸」参数翻译成「实际缓存字节数」。

**操作步骤**：

1. 假设给 `zipaxil` 设 `OPT_LGDCACHE = 10`。
2. 用 4.3.3 的公式算：`OPT_DCACHE = (10 > 4) = 1`（开缓存）；`OPT_PIPELINED_BUS_ACCESS = 1 && (10>1) = 1`。
3. 缓存大小为 \( 2^{\text{OPT\_LGDCACHE}} \) 比特级，即 \( 2^{10} = 1024 \) 个单位（具体是字/行取决于 `dcache` 内部参数 `LGCACHELEN`/`LGNLINES`，见 u3-l6）。

**需要观察的现象**：阈值 `>4` 意味着 `OPT_LGDCACHE` 取 0、1、2、3、4 时都 **不开** `dcache`——这是个容易踩的坑（你以为设了 4 就有小缓存，其实没有）。

**预期结果**：你能填出下表（待本地验证具体字节数）：

| `OPT_LGDCACHE` | `OPT_DCACHE` | `OPT_PIPELINED_BUS_ACCESS` | 数据通路 |
| --- | --- | --- | --- |
| 0（默认） | 0 | 0 | 单笔 `memops`/`axilops` |
| 2 | 0 | 1 | 流水线 `pipemem`/`axilpipe`，无缓存 |
| 10 | 1 | 1 | `dcache`/`axidcache` |

#### 4.3.5 小练习与答案

**练习 1**：spec 说关 `OPT_PIPELINED`「不会根本改变单条指令的执行节奏」，那关它到底省了什么、损失了什么？

**参考答案**：省的是各级流水线寄存器与停顿逻辑（面积）；损失的是「同时处理多条指令」的能力，吞吐（IPC）下降，但单条指令从取指到写回的周期数基本不变。

**练习 2**：为什么 `OPT_LGDCACHE=4` 在当前 `zipaxil` RTL 里 **没有** 数据缓存？

**参考答案**：因为 RTL 的派生条件是 `OPT_DCACHE = (OPT_LGDCACHE > 4)`，4 不大于 4，故为 0。这跟 spec 文字「>2」不同，应以 RTL 为准（本讲 4.3.3）。

---

### 4.4 封装、环境与 I/O 类参数：决定「CPU 怎么接进系统」

#### 4.4.1 概念说明

第三组参数控制 **CPU 与外部世界的接口**：复位地址、地址宽度、上电是否暂停、有没有调试端口/用户模式/跟踪端口、时钟频率、I/O 端口形态。spec 称之为「control the wrappers, and hence the environment」。其中 `RESET_ADDRESS` 是 **几乎每个集成都必须覆盖** 的参数。

「了解时钟域与 I/O 端口配置」（本讲学习目标之一）也落在这里。

#### 4.4.2 核心流程

这组参数的行为模式：

- `RESET_ADDRESS` / `ADDRESS_WIDTH`：纯数值，直接接到 PC 寄存器初值和地址线宽度。
- `OPT_START_HALTED` / `RESET_DURATION`：决定上电后 CPU 是「立刻跑」还是「等调试器发令」。
- `OPT_DBGPORT` / `OPT_TRACE_PORT` / `OPT_USERMODE`：开/关整块子系统。
- 时钟与 I/O：不是参数而是 **物理接口约定**，由选定的封装和板子决定。

#### 4.4.3 源码精读

`RESET_ADDRESS` 是 spec 明确强调「每次都要覆盖」的参数：

[doc/src/spec.tex:L3517-L3531](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3517-L3531) —— `RESET_ADDRESS` 控制复位后取第一条指令的地址；`ADDRESS_WIDTH` 控制地址空间大小（按 **字节**），可裁窄地址寄存器省逻辑。spec 直言默认值「不太可能有用」，建议每次实现都覆盖。

内核里 `RESET_ADDRESS` 的默认值与用法：

[rtl/core/zipcore.v:L42](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L42) —— `parameter [31:0] RESET_ADDRESS=32'h010_0000`，并经 [zipcore.v:L134](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L134) 的 `RESET_BUS_ADDRESS = RESET_ADDRESS[AW+1:2]` 转成字地址。

`OPT_USERMODE` 是 spec 参数清单里 **没有正式列出**、但 RTL 里真实存在且影响很大的参数（它管理 supervisor/user 双寄存器组，见 u2-l1、u2-l5）：

[rtl/core/zipcore.v:L55](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L55) —— `parameter [0:0] OPT_USERMODE = 1'b1`。它把寄存器堆从 16 个扩到 32 个（见 4.1.3 的 `regset` 声明），并控制一整片 `generate if (OPT_USERMODE)` 块（睡眠逻辑、模式切换、中断返回等）。

[rtl/core/zipcore.v:L2577-L2592](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2577-L2592) —— `generate if (OPT_USERMODE)` 的 `GEN_SLEEP` 块示例。关掉 `OPT_USERMODE` 后，这类块全部不生成，CPU 退化为「只有监管态」的简化机器。

时钟与 I/O 端口的权威约定在 spec 的 Clocks / I/O Ports 节：

[doc/src/spec.tex:L3540-L3559](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3540-L3559) —— 时钟：Spartan 6 实测 80 MHz，Artix-7/35T 100 MHz，Arty 板因 SDRAM MIG 限制在 81.25 MHz，有人 report 在 Kintex-7 跑到 140 MHz。**时钟域是单一的 `i_clk`**，CPU 内部没有多时钟域，这是集成时的一大简化。

[doc/src/spec.tex:L3560-L3610](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3560-L3610) —— I/O 端口分三类：主 Wishbone（CPU 当主设备访问内存）、从 Wishbone（调试端口，7 位地址）、以及 `i_clk`/`i_reset`/`i_ext_int`/`o_ext_int` 四根基本线。外部中断线宽度由 `EXTERNAL_INTERRUPTS` 参数决定（1–16，ZipBones 只允许 1）。

#### 4.4.4 代码实践

**实践目标**：搞清「上电后 CPU 到底干什么」由哪几个参数决定。

**操作步骤**：

1. 读 spec 的 `OPT_START_HALTED` 与 `RESET_DURATION`：

[doc/src/spec.tex:L3461-L3479](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3461-L3479) —— `OPT_START_HALTED` 决定上电是否等待调试器命令；`RESET_DURATION` 决定复位后保持多久（iCE40 等需要）。

2. 在 `zipaxil.v` 中确认 `START_HALTED` 与 `OPT_DBGPORT` 的关系：

[rtl/zipaxil.v:L60-L71](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L60-L71) —— 注意 `parameter [0:0] OPT_DBGPORT = START_HALTED`：调试端口默认跟随「是否上电暂停」。若你要上电直接跑（`START_HALTED=0`）又不放弃调试，需显式把 `OPT_DBGPORT` 设回 1。

**需要观察的现象**：`OPT_DBGPORT` 默认派生自 `START_HALTED`，二者耦合——这是个容易忽略的细节。

**预期结果**：你能说出「想让 CPU 上电立即从 `RESET_ADDRESS` 跑，但仍保留调试端口，必须同时设 `START_HALTED=0` 和 `OPT_DBGPORT=1`」。

#### 4.4.5 小练习与答案

**练习 1**：`RESET_ADDRESS` 在内核里为什么默认是 `32'h010_0000`，且 spec 建议每次都覆盖？

**参考答案**：它只是个占位默认值，对应「你的 ROM/flash 实际启动地址」几乎不可能正好是它。spec 明确说默认值「不太可能有用」，故每次集成都要按板子的启动存储位置覆盖。

**练习 2**：关掉 `OPT_USERMODE` 后，CPU 还能正常跑没有操作系统的裸机程序吗？

**参考答案**：能。关掉 `OPT_USERMODE` 后 CPU 只剩监管态、16 个寄存器，相当于一台「没有用户态保护」的简化机器。裸机程序本就在监管态运行，不受影响；但任何依赖 user/supervisor 切换（中断双寄存器组、操作系统隔离）的代码将无法工作。

---

### 4.5 顶层 zipaxil 如何派生并下传参数

#### 4.5.1 概念说明

集成者通常 **不直接例化 `zipcore`**，而是例化某个封装（`zipaxil` / `zipsystem` / ...）。封装定义一套「面向集成者」的参数，其中一部分与内核同名同义直接透传，另一部分（如 `OPT_LGDCACHE`）是「高层抽象」，由封装用 `localparam` 派生成内核需要的开关再下传。

理解这条「参数传递链」，你才能知道在封装层改一个值，最终在内核里触发了哪些 `generate if`。

#### 4.5.2 核心流程

`zipaxil` 的参数处理分两步：

1. **声明面向集成者的参数**：`OPT_LGICACHE`、`OPT_LGDCACHE`、`RESET_ADDRESS`、`OPT_MPY` 等。
2. **派生 + 下传**：用 `localparam` 算出 `OPT_DCACHE`、`OPT_MEMPIPE`、`FETCH_LIMIT`，再把它们连同透传参数一起填进 `zipcore #(...) thecore(...)` 的参数端口。

注意命名映射：封装的 `OPT_FPU` 对应内核的 `IMPLEMENT_FPU`；封装的 `START_HALTED` 对应内核的 `OPT_START_HALTED`——不是一一同名。

#### 4.5.3 源码精读

先看封装的参数声明全集：

[rtl/zipaxil.v:L51-L82](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L51-L82) —— `zipaxil` 的 `module ... #(...)` 参数列表。对比内核，你会发现封装多了 `C_DBG_ADDR_WIDTH`、`C_AXI_DATA_WIDTH`、`SWAP_WSTRB`、`RESET_DURATION` 等总线/封装层参数；并把 `OPT_SIM`/`OPT_CLKGATE` 用 `ifdef VERILATOR` 区分（仿真时默认开 `OPT_SIM`）。

派生逻辑（4.3.3 已引用）：

[rtl/zipaxil.v:L305-L309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L305-L309) —— 把对数尺寸参数派生成开关。

下传给内核的关键片段：

[rtl/zipaxil.v:L837-L860](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L837-L860) —— `zipcore #(...) thecore(...)` 的参数端口连接。可清晰看到：

- `.RESET_ADDRESS({ {(32-ADDRESS_WIDTH){1'b0}}, RESET_ADDRESS })`：把封装的 `RESET_ADDRESS`（宽度为 `ADDRESS_WIDTH`）零扩展成内核要的 32 位。
- `.ADDRESS_WIDTH(ADDRESS_WIDTH-2)`：封装按 **字节地址** 宽度，内核按 **字地址**（减 2 位）。
- `.OPT_DCACHE(OPT_DCACHE)`：下传派生值。
- `.IMPLEMENT_FPU(OPT_FPU)`：名字映射（封装 `OPT_FPU` → 内核 `IMPLEMENT_FPU`）。
- `.OPT_START_HALTED(START_HALTED)`：名字映射。

注意这段在一个 `ifdef FORMAL ... else` 里——形式化验证模式下，`zipaxil` 用一个 `fdebug` 替身代替真实 `zipcore`（见 u5-l2），这是另一处「条件编译裁剪」。

#### 4.5.4 代码实践

**实践目标**：跟踪一个参数从封装层到内核层的完整路径。

**操作步骤**：

1. 选参数 `OPT_MPY`。
2. 在 `zipaxil.v` 参数表确认它叫 `OPT_MPY`（[L62](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L62)）。
3. 在内核实例化处确认它透传为 `.OPT_MPY(OPT_MPY)`（[L844](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L844)）。
4. 在 `zipcore.v` 确认它最终进入 `mpyop` 的 `generate if` 阶梯（u3-l5 讲过）。
5. 再选 `OPT_LGDCACHE` 做对比——它 **不能** 直接透传，而要先经 `localparam OPT_DCACHE` 派生。

**需要观察的现象**：`OPT_MPY` 是「直接透传」，`OPT_LGDCACHE` 是「派生后透传」。两种参数传递模式你都能识别。

**预期结果**：你能画出「集成者设 `OPT_LGDCACHE=10` → `localparam OPT_DCACHE=1` → `zipcore.OPT_DCACHE=1` → `generate if (OPT_DCACHE)` 生成清缓存逻辑」这条链。

#### 4.5.5 小练习与答案

**练习 1**：为什么封装用 `OPT_LGDCACHE`（对数尺寸）而内核用 `OPT_DCACHE`（0/1 开关）？

**参考答案**：封装面向集成者，用「缓存多大」（对数尺寸）更直观；内核只关心「有没有数据缓存」这个开关来裁剪电路。两者粒度不同，故封装层做一次派生转换。

**练习 2**：封装的 `OPT_FPU` 和内核的哪个参数相连？为什么名字不一样？

**参考答案**：连到内核的 `IMPLEMENT_FPU`（见 [zipaxil.v:L847](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L847)）。命名不一致是历史遗留——内核用 `IMPLEMENT_FPU`，封装对外统一用 `OPT_*` 前缀，集成者只需关心封装层的统一命名。

---

## 5. 综合实践

**任务**：为一块资源极度受限的 FPGA（例如 iCE40，无 DSP、无块 RAM、LUT 紧张）设计一组 `zipaxil` 参数，目标是用最小面积跑通一个 **不用乘除、不用浮点、不用操作系统** 的简单控制程序。

请完成下面这张「裁剪决策表」，每一行都要写出：设成什么值、对应的 `generate if` 块或电路被裁掉、省下了什么资源、失去了什么能力。第一行已示例。

| 参数 | 设定值 | 裁掉的电路（引用源码行） | 省下什么 | 失去什么 |
| --- | --- | --- | --- | --- |
| `IMPLEMENT_FPU`（封装 `OPT_FPU`） | 0 | [zipcore.v:L1568](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1568) 的 FPU 块 | 本就未实现，几乎无 | 无（本就不可用） |
| `OPT_MPY` | ？ | ？ | ？ | ？ |
| `OPT_DIV` | ？ | [zipcore.v:L1527](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1527) | ？ | ？ |
| `OPT_DCACHE`（经 `OPT_LGDCACHE`） | ？ | [zipcore.v:L3285](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3285) | ？ | ？ |
| `OPT_USERMODE` | ？ | [zipcore.v:L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L166) 的 regset + [L2577](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2577) 等 | ？ | ？ |
| `OPT_PIPELINED` | ？ | [zipcore.v:L401](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L401) 等多处 | ？ | ？ |
| `OPT_SHIFTS` | ？ | ？ | ？ | ？（提示：危险，见 4.2） |
| `RESET_ADDRESS` | ？（自定） | —— | —— | —— |

**完成后的自检问题**：

1. 你的配置里 `OPT_LGDCACHE` 取多少才能让 `OPT_DCACHE=0`？写出推导（用 [zipaxil.v:L307](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L307) 的公式）。
2. 你关了 `OPT_USERMODE` 后，中断还能用吗？（提示：回顾 u2-l5，双寄存器组是中断模型的核心。）
3. 你为什么 **不该** 关 `OPT_SHIFTS`，即使它很省？（提示：GCC 依赖 + 静默错误。）

**预期结果**：得到一套自洽的最小配置，例如 `OPT_FPU=0, OPT_MPY>4（或 0）, OPT_DIV=0, OPT_LGDCACHE=0, OPT_USERMODE=0, OPT_PIPELINED=0, OPT_SHIFTS=1（保留！）, RESET_ADDRESS=<你的 ROM 地址>`。并能用源码行号证明每一项「裁掉了我说的那块电路」。若你手头有 iCE40 工具链，可「待本地验证」综合后比较 LUT 占用；否则完成上表与自检即达成本实践目标。

## 6. 本讲小结

- ZipCPU 的 `OPT_*` 是 **综合期剪刀**：Verilog `parameter` 配合 `generate if`，在综合时决定电路生不生成，关闭即零占资源，代价是改配置必须重新综合。
- 指令集类参数（`OPT_MPY`/`OPT_DIV`/`OPT_CIS`/`OPT_LOCK`/`IMPLEMENT_FPU`）决定「会哪些指令」，关闭通常触发非法指令异常；但 `OPT_SHIFTS` 是危险的例外——静默执行错误结果，且 GCC 依赖它，不要关。
- 微架构类参数（`OPT_PIPELINED`/`OPT_EARLY_BRANCHING`/`OPT_LGICACHE`/`OPT_LGDCACHE`）用 LUT 和 RAM 换 IPC，spec 名言「LUTs and RAMs can be traded for performance」。
- 封装层用「对数尺寸」参数（`OPT_LGDCACHE`）经 `localparam` 派生出内核的 0/1 开关（`OPT_DCACHE`）；spec 文字与 RTL 阈值偶有出入（如 dcache 阈值 spec 说 >2、RTL 是 >4），**以 RTL 为准**。
- 环境/I/O 参数中 `RESET_ADDRESS` 几乎每次集成都要覆盖；`OPT_DBGPORT` 默认派生自 `START_HALTED`，二者耦合；`OPT_USERMODE` 控制 supervisor/user 双寄存器组（16→32 个寄存器），关闭可大幅省面积但失去中断双组与操作系统支持。
- ZipCPU 是 **单时钟域**（单一 `i_clk`），实测 Spartan 6 约 80 MHz、Artix-7 约 100 MHz，I/O 分主 Wishbone、从调试 Wishbone、及 clk/reset/int 四类基本线。

## 7. 下一步学习建议

- **接着读 u5-l7（自定义 SoC 集成）**：把本讲的参数选型用到一次真实集成中——用 `zipaxil` 作 CPU、`wbxbar`/`addrdecode` 搭总线、挂 RAM/ROM/UART，用 `RESET_ADDRESS` 和地址译码把整个系统跑通。
- **回看 u3-l1、u3-l5**：对照 `zipcore.v` 的 `generate if` 全集，确认本讲提到的每处裁剪在流水线/乘除单元里的具体位置。
- **查阅 spec.tex 的 Integration 全章**：本讲只精读了 Parameters / Clocks / I/O Ports 三节，整合时还需读「Connecting to the CPU」一节了解调试端口在系统死锁时的「带外逃生」作用（u5-l1 已展开）。
- **动手综合对比**：若有 FPGA 工具链，分别用「全开」和「最小」两套 `OPT_*` 综合 `zipaxil`，比较 LUT/FF/BRAM 占用与最高频率，直观体会「面积换性能」。
