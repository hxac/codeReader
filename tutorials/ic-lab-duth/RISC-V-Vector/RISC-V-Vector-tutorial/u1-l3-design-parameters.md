# 设计参数与可调旋钮

## 1. 本讲目标

本讲聚焦于整个设计的「中央配置台」—— `rtl/shared/params.sv`。读完本讲，你应当能够：

- 说出 `params.sv` 中**标量参数**和**向量参数**两大类各自的职责，以及哪些参数在本仓库里真正被使用。
- 准确区分 **TUNABLE（可调）** 与 **NOT TUNABLE（不可调）** 参数，理解「为什么有的能调、有的不能调」。
- 理解 `VECTOR_LANES`、`USE_HW_UNROLL`、`VECTOR_FWD_POINT_A/B` 这三个最关键旋钮的含义与取值约束。
- 掌握「改一个参数会牵动哪些位宽与结构」的影响边界，并能解释 README 中「当前最多支持 16 lane」的根本原因。

本讲不深入任何模块的内部实现（那是后续单元的任务），只把设计级的「旋钮面板」讲清楚，为后续阅读 `vex`、`vis`、`vmu` 等模块打好参数基础。

## 2. 前置知识

在阅读本讲前，你需要了解几个 SystemVerilog 与硬件设计的常识：

- **`localparam`**：SystemVerilog 中定义「局部常量」的关键字。`localparam int X = 8;` 定义了一个值为 8 的整型常量，它在编译期就固定，综合后不会变成任何物理连线，只用来推导位宽和规模。`params.sv` 里几乎每一行都是一个 `localparam`。
- **`$clog2(x)`**：SystemVerilog 系统函数，返回「至少需要多少位才能表示 0 ~ x-1」，即向上取整的对数。例如 `$clog2(8) = 3`，`$clog2(32) = 5`。它常被用来由「表项数」自动推导「索引位宽」，例如 `ROB_ENTRIES=8` 对应索引位宽 `$clog2(8)=3`。
- **参数化位宽**：硬件描述里常见 `logic [VECTOR_LANES-1:0] ...` 这样的写法。一旦 `VECTOR_LANES` 改变，这段连线的物理宽度就会跟着变。这正是「改一个参数会牵动一堆位宽」的根本原因。
- **TUNABLE / NOT TUNABLE**：本项目作者用注释显式地把参数分成两类。TUNABLE 表示设计允许、也预期你会去调整它（例如 lane 数）；NOT TUNABLE 则意味着改了它很可能破坏设计假设（例如数据位宽固定 32），除非你做大规模改造。
- **标量核 vs 向量数据通路**：承接 u1-l1，本仓库只公开「向量数据通路」的 RTL，标量核（主控处理器）尚未公开。因此 `params.sv` 里有一整块「标量参数」目前是「空转」的——它们存在，但本仓库的仿真不会真正用到，等标量核释出后才会启用。

> 提示：`params.sv` 在仿真（`` `ifdef MODEL_TECH``）时会先 `` `include "vmacros.sv" ``，这样它才能用上 `EX1`、`EX4_F` 这类宏名。这一点会在「4.2 向量参数」里展开。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `rtl/shared/params.sv` | 设计的中央配置文件，定义全部标量/向量参数。本讲的主角。 |
| `rtl/vector/vmacros.sv` | 向量数据通路的宏定义，包括 FU 编码、存储操作编码、以及**转发点宏** `EX1`/`EX4_F` 等。`params.sv` 里转发点参数就引用这些宏。 |
| `rtl/vector/vstructs.sv` | 向量结构体定义。其中 `DUMMY_VECTOR_LANES` 与 `VECTOR_LANES` 耦合，是「改 lane 数必须同步改」的隐藏陷阱。 |
| `rtl/vector/vex.sv` | 执行级顶层。其内部静态生成的**归约树**直接决定了「最多 16 lane」这一硬限制。 |
| `rtl/README.md` | 明确说明「只有部分参数可调」，并指出标量参数暂未被使用。 |
| `README.md` | 在 Repo State / Future Work 中写明「当前最大向量 lane 数为 16」及其原因。 |

## 4. 核心概念与源码讲解

### 4.1 标量参数

#### 4.1.1 概念说明

`params.sv` 的第一大部分是 **Scalar Pipe Parameters（标量流水线参数）**。它们描述的是那个「尚未公开的双发射超标量标量核」的配置：指令/数据 cache 规模、L2 规模、分支预测器规模、重排序缓冲（ROB）深度等。

关键认知：**这些参数在本仓库里基本是「待命」状态**。因为标量核 RTL 没有释出，仿真时是由测试台（`vector_driver`）直接喂入「已经译码好的向量指令」，绕过了标量核的取指/译码/分支预测/cache 全过程。`rtl/README.md` 第 4 行明确说明了这一点。所以你阅读这一段时，要理解它的「设计意图」，而不必纠结它在当前仿真里是否生效——它现在大多不生效。

不过，这一段里有一个参数对**整个仿真都有全局影响**：`DATA_WIDTH = 32`（数据位宽）。向量数据通路里每个 lane 的元素宽度、寄存器堆的元素宽度、存储请求的数据宽度，都建立在这个 32 位假设之上。

#### 4.1.2 核心流程

标量参数段内部的编排逻辑是：

1. 先列 **TUNABLE PARAMETERS**（可调）：
   - 总开关：`DUAL_ISSUE`（双发射使能）、`ENABLE_LOGGING`（日志开关）。
   - **存储系统**：指令 cache（`IC_*`）、数据 cache（`DC_*`）、L2（`L2_*`）的表项数、行大小、相联度，以及`REALISTIC`（是否模拟真实主存延迟）与 `DELAY_CYCLES`（L2 响应周期数）。
   - **分支预测器**：`RAS_DEPTH`、`GSH_*`、`BTB_SIZE`。
2. 再列 **NOT !! TUNABLE PARAMETERS**（不可调）：
   - **ROB**：`ROB_ENTRIES`（重排序缓冲深度），以及由它**自动派生**的 `ROB_TICKET_W = $clog2(ROB_ENTRIES)`。
   - **固定架构位宽**：`ISTR_DW`（指令宽度 32）、`ADDR_BITS`（地址宽度 32）、`DATA_WIDTH`（数据宽度 32）、`FETCH_WIDTH`（取指宽度 64）、`R_WIDTH`（寄存器号宽度 6）、`MICROOP_W`（标量微操作码宽度 5）。
   - **CSR**：`CSR_DEPTH = 64`。

「派生参数」是一种很常见的工程写法：人只维护「表项数」这一个有意义的量，位宽交给 `$clog2` 自动算，避免「改了表项数却忘了改位宽」的不一致 bug。用公式表达：

\[
\text{ROB\_TICKET\_W} = \lceil \log_2(\text{ROB\_ENTRIES}) \rceil
\]

#### 4.1.3 源码精读

标量可调段的总开关与存储参数：

[rtl/shared/params.sv:13-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L13-L44) —— 用 `TUNABLE PARAMETERS` 注释隔出可调区，依次定义双发射开关、日志开关、各级 cache 与 L2 规模、真实延迟模型与 `DELAY_CYCLES`。

分支预测器参数：

[rtl/shared/params.sv:46-52](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L46-L52) —— `RAS_DEPTH`、`GSH_HISTORY_BITS`、`GSH_SIZE`、`BTB_SIZE`，都属于标量核的分支预测子系统。

标量不可调段（重点关注「派生位宽」与固定架构位宽）：

[rtl/shared/params.sv:54-76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L54-L76) —— 这里出现了本段最值得学习的写法：`ROB_TICKET_W = $clog2(ROB_ENTRIES)`（第 62 行），以及一连串标注 `DO NOT MODIFY` 的固定位宽 `DATA_WIDTH=32`、`ADDR_BITS=32` 等。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：学会用「表项数 × 行大小」估算 cache 容量，并理解 `REALISTIC` 延迟模型对仿真的意义。
2. **操作步骤**：
   - 在 [params.sv:25-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L25-L44) 中找到数据 cache 的三个参数 `DC_ENTRIES`、`DC_DW`、`DC_ASC`。
   - 用容量公式估算数据 cache 的总容量（位）：`DC_ENTRIES × DC_DW × DC_ASC`。
   - 找到 `REALISTIC` 与 `DELAY_CYCLES`，思考「把 `REALISTIC` 设为 0」会让 L2/主存行为变成什么样。
3. **需要观察的现象**：注意 `REALISTIC=1`、`DELAY_CYCLES=10` 这组组合意味着每次 L2 访问都要消耗约 10 个周期——这在后续 u4-l3（存储子系统）里会直接体现为仿真中的 stall。
4. **预期结果**：你会意识到标量参数虽然「暂未启用」，但其中的存储模型参数（`REALISTIC`/`DELAY_CYCLES`/`DATA_WIDTH`）实际上是整条向量访存链路的延迟与位宽基底。
5. 关于具体容量数值与 `REALISTIC=0` 的精确行为：**待本地验证**（结合 u4-l3 的 `main_memory.sv` 一起确认）。

#### 4.1.5 小练习与答案

**练习 1**：`ROB_ENTRIES = 8`，`ROB_TICKET_W` 的值是多少？如果把 `ROB_ENTRIES` 改成 16，`ROB_TICKET_W` 会变成多少？

> **参考答案**：`$clog2(8) = 3`，所以 `ROB_TICKET_W = 3`；改成 16 后 `$clog2(16) = 4`，`ROB_TICKET_W = 4`。这正是「派生参数」的价值——你不用手动改位宽。

**练习 2**：为什么 `DATA_WIDTH` 被标为 `NOT TUNABLE`，而 `DC_ENTRIES` 是 `TUNABLE`？

> **参考答案**：`DATA_WIDTH=32` 是贯穿全设计的架构假设（元素宽度、寄存器堆单元、ALU 运算宽度都按 32 位搭），改它等于推倒重做；而 `DC_ENTRIES` 只是 cache 的容量，增减表项不破坏 32 位假设，属于安全的可调项。

---

### 4.2 向量参数

#### 4.2.1 概念说明

`params.sv` 的第二大部分是 **Vector Pipe Parameters（向量流水线参数）**——这才是本仓库真正在用的参数。其中三个最重要的可调旋钮：

- **`VECTOR_LANES`**：向量通道数，也就是「同时并行处理的元素个数」。当前默认 8，注释明确写「currently max 16」（当前最多 16）。
- **`VECTOR_FWD_POINT_A` / `VECTOR_FWD_POINT_B`**：两个转发点（forwarding point）的位置，决定执行流水线在哪里把结果旁路回去，从而影响「冒险解除的速度」与「关键路径的长度（频率）」。
- **`USE_HW_UNROLL`**：是否启用「动态寄存器堆分配 + 硬件循环展开」这一核心创新（承接 u1-l1 的三大创新点之一）。

理解这三个旋钮，就理解了向量数据通路「性能 vs 面积 vs 频率」的取舍主线。

#### 4.2.2 核心流程

向量可调段的逻辑：

1. `VECTOR_ENABLED`：总开关，决定整条向量数据通路是否被编译进来。
2. `VECTOR_LANES`：通道数。它牵动一大批位宽（见 4.3）。
3. `VECTOR_FWD_POINT_A/B`：取值为宏名 `EX1`/`EX2`/`EX2_F`/`EX3`/`EX3_F`/`EX4`/`EX4_F` 之一。这些宏在 `vmacros.sv` 里定义，`params.sv` 在仿真时通过 `` `include "vmacros.sv" `` 把它们引入。
4. `USE_HW_UNROLL`：决定 `vRRM`/`VRAT`/`VRF` 这套动态寄存器机制是否启用（承接 u2-l1 中 `vector_top` 的 `generate` 分支）。

转发点的取值约定值得专门记一下（来自 `vmacros.sv`）：

| 宏名 | 数值 | 含义 |
|------|------|------|
| `EX1` | 1 | 第 1 执行级，非寄存 |
| `EX2` | 2 | 第 2 执行级，非寄存 |
| `EX2_F` | 20 | 第 2 执行级，**寄存**（flopped） |
| `EX3` | 3 | 第 3 执行级，非寄存 |
| `EX3_F` | 30 | 第 3 执行级，寄存 |
| `EX4` | 4 | 第 4 执行级，非寄存 |
| `EX4_F` | 40 | 第 4 执行级，寄存 |

带 `_F` 的变体表示该转发点经过了一级寄存器（flopped）。注释「non-flopped hurt freq」点明了取舍：**非寄存的转发点能更早把结果送回去（减少停顿），但会拉长组合逻辑路径、伤害主频**；寄存变体则相反。这是经典的「延迟 vs 频率」权衡，详细展开见 u4-l2。

默认配置 `VECTOR_FWD_POINT_A = EX1`、`VECTOR_FWD_POINT_B = EX4_F` 表示：一路转发从最早（EX1）引出以快速解除冒险，另一路从最晚但寄存过的（EX4_F）引出以保护时序。

#### 4.2.3 源码精读

向量可调段（本仓库真正在用的旋钮）：

[rtl/shared/params.sv:81-99](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L81-L99) —— 依次定义 `VECTOR_ENABLED`、`VECTOR_LANES`（注释「currently max 16」+「must also change dummy param in vstructs」）、两个转发点（注释列出全部可选宏名）、`USE_HW_UNROLL`。

仿真时引入宏的 `include`（解释为什么 `EX1`/`EX4_F` 这些宏名能用）：

[rtl/shared/params.sv:6-8](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L6-L8) —— 仅在 `` `ifdef MODEL_TECH``（仿真环境）下 `` `include "vmacros.sv" ``，从而让转发点宏可被引用。

转发点宏定义本身：

[rtl/vector/vmacros.sv:35-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L35-L44) —— 定义 `EX1=1` … `EX4_F=40`，并用注释说明 `_F` = flopped、非寄存版本会伤害频率。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把宏名「翻译」成数值，并理解默认转发点配置的取舍意图。
2. **操作步骤**：
   - 在 [params.sv:96-97](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L96-L97) 查到 `VECTOR_FWD_POINT_A = \`EX1`、`VECTOR_FWD_POINT_B = \`EX4_F`。
   - 在 [vmacros.sv:35-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L35-L44) 查到对应数值：`EX1 = 1`，`EX4_F = 40`。
   - 思考：如果两个转发点都设成 `EX1`（都不寄存），冒险解除会更快，但关键路径会怎样？
3. **需要观察的现象**：注意数值里的「整数位 = 执行级编号」「十位是否非零 = 是否寄存」这一编码规律。
4. **预期结果**：你应能解释默认配置「一早（EX1）一晚且寄存（EX4_F）」是在「快速解除冒险」与「保护主频」之间做平衡。
5. 关于改动后对仿真 cycles/频率的**量化**影响：**待本地验证**（综合或长仿真才能看出，详见 u4-l2、u4-l7）。

#### 4.2.5 小练习与答案

**练习 1**：把 `VECTOR_FWD_POINT_B` 从 `EX4_F` 改成 `EX4`，数值从多少变成多少？行为上有什么区别？

> **参考答案**：从 40 变成 4。两者都从第 4 执行级引出，但 `EX4_F`（40）经过一级寄存（时序更友好），`EX4`（4）不寄存（更快解除冒险但伤害频率）。

**练习 2**：`USE_HW_UNROLL = 0` 时，u2-l1 里提到的「弹性缓冲插入」会发生什么变化？

> **参考答案**：`vector_top` 中由 `USE_HW_UNROLL` 控制的 `generate` 分支会走另一条路径，相关弹性缓冲被旁路，动态寄存器堆分配与硬件循环展开机制关闭。详细连线见 u2-l1 / u2-l2。

---

### 4.3 可调与不可调边界

#### 4.3.1 概念说明

「可调」和「不可调」并不是作者的主观任性，而是有客观的结构原因。本模块要建立两个关键认知：

1. **改一个可调参数，往往会牵动一连串位宽与结构**。最典型的就是 `VECTOR_LANES`：它一旦改变，执行级的端口宽度、lane 例化数量、归约树规模、甚至结构体里的字段宽度都会跟着变。其中有一处「隐藏耦合」最容易踩坑——`vstructs.sv` 里的 `DUMMY_VECTOR_LANES` 必须同步修改。
2. **某些参数之所以「不可调」，是因为有硬性结构上限**。最典型的就是「最多 16 lane」。这不是随意设定的数字，而是由 `vex.sv` 里**静态生成的归约树**的级数决定的。

#### 4.3.2 核心流程

**先看「为什么最多 16 lane」**。归约指令（如点积、求和）需要把所有 lane 的部分结果逐步合并。`vex.sv` 用一个 `generate` 块**静态**搭出了一棵逐级减半的归约树：

- 第 1 级（EX1）：把相邻 2 个 lane 合并（步长 `k = k + 2`）。
- 第 2 级（EX2）：把相邻 4 个 lane 合并（步长 `k = k + 4`），仅当 `VECTOR_LANES > 2` 才生成。
- 第 3 级（EX3）：步长 `k = k + 8`，仅当 `VECTOR_LANES > 4` 才生成。
- 第 4 级（EX4）：步长 `k = k + 16`，仅当 `VECTOR_LANES > 8` 才生成。

也就是说，这棵树**最多只有 4 级**，因此最多能把 \(2^4 = 16\) 个 lane 归约到 1 个结果。归约树深度与 lane 数的关系为：

\[
\text{归约级数} = \lceil \log_2(\text{VECTOR\_LANES}) \rceil
\]

要支持 32 lane，就需要第 5 级（步长 `k = k + 32`），而当前 RTL 没有这一级，且 README 的 Future Work 明确说：支持 >16 lane 需要在执行流水线加背压（back-pressure）——这是一项未完成的改造。

**再看「改 lane 数会牵动什么」**。`vex.sv` 中大量信号写成 `logic [VECTOR_LANES-1:0][DATA_WIDTH-1:0] ...`，lane 数一变，这些打包信号的物理宽度立刻改变；同时 `g_vex_pipe` 循环会例化更多或更少的 `vex_pipe` lane。更隐蔽的是 `vstructs.sv`：它用了一个**独立常量** `DUMMY_VECTOR_LANES`（默认 8）来定义结构体里的 `maxvl`/`vl`/`ticket` 字段宽度。这个常量与 `VECTOR_LANES` 并不自动联动，所以 `params.sv` 第 87 行的注释才特别提醒「must also change dummy param in vstructs」。

#### 4.3.3 源码精读

「最多 16 lane」的根本来源——静态归约树（4 级，最大步长 `k = k + 16`）：

[rtl/vector/vex.sv:121-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L121-L153) —— 四个 `generate` 段 `g_rdc_ex1` ~ `g_rdc_ex4`，逐级把 lane 数减半；`g_rdc_ex3` 由 `VECTOR_LANES > 4` 守护、`g_rdc_ex4` 由 `VECTOR_LANES > 8` 守护。最后一级步长为 16，决定了上限。

`VECTOR_LANES` 如何直接驱动执行级端口位宽与 lane 例化数量：

[rtl/vector/vex.sv:27-43](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L27-L43) —— `exec_data_i`、`frw_a_data`、`frw_b_data`、`wr_data` 等端口都是 `[VECTOR_LANES-1:0][DATA_WIDTH-1:0]`，改 lane 数即改这些端口的物理位宽。

[rtl/vector/vex.sv:69-76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L69-L76) —— `for (k = 0; k < VECTOR_LANES; k++) begin : g_vex_pipe`，lane 数直接决定例化多少个 `vex_pipe`。

「改 lane 数必须同步改」的隐藏耦合——`vstructs.sv` 里的 DUMMY 常量：

[rtl/vector/vstructs.sv:9-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L9-L10) —— 定义 `DUMMY_VECTOR_LANES = 8` 与 `DUMMY_REQ_DATA_WIDTH = 512`，它们被用于结构体字段位宽（见下一条引用），与 `VECTOR_LANES` 不自动联动。

[rtl/vector/vstructs.sv:29-31](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L29-L31) —— `maxvl`/`vl` 字段宽度写作 `$clog2(32*DUMMY_VECTOR_LANES)`，可见改 `VECTOR_LANES` 时若不同步改 `DUMMY_VECTOR_LANES`，结构体字段宽度就会与实际 lane 数不一致。

向量不可调段（标注了 `default` 值，含 `VECTOR_FP_ALU`、`VECTOR_FXP_ALU`、寄存器数、各类微操作码宽度、ticket 位宽、最大请求宽度）：

[rtl/shared/params.sv:101-111](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L101-L111) —— 例如 `VECTOR_FP_ALU=1`（有浮点 ALU，但承接 u2-l7，当前只是占位）、`VECTOR_FXP_ALU=0`（定点尚未实现）、`VECTOR_REGISTERS=32`、`VECTOR_TICKET_BITS=5`、`VECTOR_MAX_REQ_WIDTH=512`（注释「default: 256」暗示团队一度用 256）。

#### 4.3.4 代码实践（动手 + 源码阅读型）

> 这是本讲的主实践任务，直接对应学习目标里的「参数变化对位宽与结构的影响边界」。

1. **实践目标**：预测「把 `VECTOR_LANES` 从 8 改为 4」会牵动哪些模块的位宽与结构，并解释 16 lane 上限的成因。
2. **操作步骤**：
   - **步骤 A（梳理位宽影响）**：在 [vex.sv:27-43](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L27-L43) 找到所有 `[VECTOR_LANES-1:0]` 端口，记录它们在 8→4 后宽度如何变化（例如 `wr_data` 从 `8×32=256` 位变成 `4×32=128` 位）。
   - **步骤 B（梳理结构影响）**：看 [vex.sv:69-76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L69-L76) 的 `g_vex_pipe` 循环，确认会例化 4 个而非 8 个 lane。
   - **步骤 C（梳理归约树影响）**：看 [vex.sv:141-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L141-L153)，判断 `VECTOR_LANES=4` 时 `g_rdc_ex3`（`>4`）和 `g_rdc_ex4`（`>8`）是否会被综合掉。
   - **步骤 D（关键：同步改 DUMMY）**：确认必须把 [vstructs.sv:9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L9) 的 `DUMMY_VECTOR_LANES` 从 8 改成 4，否则 `maxvl`/`vl` 等字段宽度与实际 lane 数不一致（结构体位宽见 [vstructs.sv:29-31](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L29-L31)）。
   - **步骤 E（解释 16 上限）**：用 [vex.sv:121-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L121-L153) 的四级归约树，说明为何上限是 16。
3. **需要观察的现象**：注意「位宽变化（连续的打包信号变窄）」「结构变化（lane/归约级被裁剪）」「常量耦合（DUMMY 必须同步）」三种不同性质的影响。
4. **预期结果**：你应得出一张「受影响清单」——至少包含：`vex` 的打包端口与 `g_vex_pipe` 例化数、归约树级数、`vstructs` 的 `DUMMY_VECTOR_LANES`；同时能用 \( \lceil \log_2 \rceil \) 解释 16 的上限。
5. 关于 `vex_pipe`、`vrf`、`vis`、`vmu` 内部受 `VECTOR_LANES` 影响的**精确**行号清单：**待确认**（这些模块会在 u2/u3 单元逐个精读，本讲只需建立「牵动面很广」的认知）。

> 说明：本仓库不允许修改源码，因此步骤 A–E 以「在源码上标注 + 推理」的方式进行，不实际改动 `VECTOR_LANES`。若你想真正验证，应在自己 fork 的副本里改动后用 `compile_vector_simulator.do` 重新编译（编译流程见 u1-l2 / u1-l5）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 说「当前最大 lane 数为 16」？要支持 32 lane，最少需要改动什么？

> **参考答案**：因为 `vex.sv` 的归约树只有 4 级、最大步长 `k = k + 16`，最多归约 \(2^4=16\) 个 lane。支持 32 lane 至少要新增第 5 级归约（步长 `k = k + 32`），并且按 README Future Work 的说法，还需在执行流水线加背压以容纳更多 lane。

**练习 2**：把 `VECTOR_LANES` 从 8 改成 4 后，如果**忘记**同步改 `DUMMY_VECTOR_LANES`，会出现什么性质的问题？

> **参考答案**：`VECTOR_LANES` 控制的执行级端口会变成 4 lane 宽，但结构体里 `maxvl`/`vl` 等字段仍按 `DUMMY_VECTOR_LANES=8` 推导位宽，造成「实际 lane 数」与「结构体字段假定」不一致——属于会破坏仿真的隐性耦合 bug，正是 `params.sv` 注释要提醒你的原因。

**练习 3**：`VECTOR_FP_ALU` 和 `VECTOR_FXP_ALU` 都在「NOT TUNABLE」段，含义分别是什么？

> **参考答案**：`VECTOR_FP_ALU=1` 表示设计中**存在**浮点 ALU（但承接 u2-l7，它当前只是占位实现）；`VECTOR_FXP_ALU=0` 表示定点 ALU **尚未实现**。它们不是「性能旋钮」而是「特性开关/现状标注」，故归入不可调段。

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「参数面板速读 + 影响追踪」小任务：

> **任务：为 `params.sv` 建立一张「参数档案表」，并追踪 `VECTOR_LANES` 的影响链。**

1. **分类**：通读 [params.sv:1-111](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L1-L111)，把每个 `localparam` 填入下表四列之一：`标量·可调` / `标量·不可调` / `向量·可调` / `向量·不可调`，并标注它在本仓库仿真里「是否真正生效」。
2. **挑旋钮**：从「向量·可调」里挑出 `VECTOR_LANES`、`VECTOR_FWD_POINT_A/B`、`USE_HW_UNROLL`，各写一句话说明它调的是什么。
3. **追影响**：按 4.3.4 的步骤，画出 `VECTOR_LANES: 8 → 4` 的影响链（至少包含 `vex` 端口位宽、`g_vex_pipe` 例化数、归约树级数、`vstructs` 的 `DUMMY_VECTOR_LANES`）。
4. **解释上限**：用归约树公式 \( \lceil \log_2(\text{LANES}) \rceil \) 解释「最多 16 lane」，并指出要突破该上限需要的改造方向（对照 README 的 Future Work）。

完成后，你应当能脱稿回答：「这个设计的哪些参数我能放心调？调 `VECTOR_LANES` 要同步改哪里？为什么不能直接配 32 lane？」

## 6. 本讲小结

- `params.sv` 是全设计的中央配置台，按「标量 / 向量」×「可调 / 不可调」分成四块；其中**标量参数在本仓库大多待命**（标量核未释出），真正生效的是**向量参数**与全局位宽 `DATA_WIDTH`。
- 三个最关键的向量旋钮是 `VECTOR_LANES`（通道数）、`VECTOR_FWD_POINT_A/B`（转发点，取自 `vmacros.sv` 的 `EX1`/`EX4_F` 等宏）、`USE_HW_UNROLL`（硬件循环展开开关）。
- 转发点宏带 `_F` 表示寄存（flopped）变体：非寄存版更快但伤主频，寄存版更慢但时序更稳，是经典的「延迟 vs 频率」权衡。
- 「不可调」分两类：一是改了会破坏架构假设（如 `DATA_WIDTH=32`），二是有硬性结构上限（如 `VECTOR_LANES` 最多 16）。
- 「最多 16 lane」的根因是 `vex.sv` 中静态生成的**四级归约树**（最大步长 `k = k + 16`，\( \lceil \log_2 \rceil = 4 \)）；突破上限需新增归约级并加执行流水背压。
- 改 `VECTOR_LANES` 牵动面很广，且存在隐藏耦合：必须同步修改 `vstructs.sv` 里的 `DUMMY_VECTOR_LANES`，否则结构体字段宽度会与实际 lane 数不一致。

## 7. 下一步学习建议

- 本讲只把「旋钮面板」讲清楚，**没有展开旋钮背后的结构**。建议接下来读 **u1-l4（共享类型与宏定义）**，把 `vstructs.sv` / `vmacros.sv` / `structs.sv` 里的结构体字段和 FU 编码补全，这样你会更深刻地理解「为什么 `DUMMY_VECTOR_LANES` 控制的是字段位宽」。
- 想亲眼看参数对仿真的影响，进入 **u1-l5（端到端跑通仿真）**，用 `sim_generator.py` 配合不同 `VECTOR_LANES` 跑一次（该脚本把 `VECTOR_LANES` 作为命令行参数，是合法的「不改 RTL 也能换 lane 数」的途径）。
- 想搞懂「转发点」到底转发的是什么、对频率/冒险解除的具体影响，留到 **u2-l7（vEX 与 vex_pipe 执行流水）** 与 **u4-l2（转发网络与变延迟执行）** 再深入。
- 想理解「为什么最多 16 lane」背后的归约树全貌，读 **u2-l8（整数 ALU 与跨 lane 归约树）**，那里会逐级拆解 [vex.sv:121-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L121-L153) 这棵树。
