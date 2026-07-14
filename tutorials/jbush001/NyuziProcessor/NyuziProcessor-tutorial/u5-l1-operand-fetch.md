# 操作数 fetch 与寄存器文件

## 1. 本讲目标

经过前面几讲，你已经知道指令在 Nyuzi 单核里要依次走过「取指 → 解码 → 线程选择」三级，并在 `thread_select_stage` 里被挑选出来准备发射。但一条指令真正要「干活」，必须先拿到它的输入数据——也就是操作数。本讲就聚焦流水线的下一级 **operand_fetch_stage（操作数 fetch 级）**，它是「指令」与「数据」汇合的地方。

学完本讲你应该能够：

- 说清 operand_fetch_stage 如何根据解码结果里的 `op1_src` / `op2_src` / `mask_src` 三组开关，从寄存器文件或立即数中挑出操作数。
- 理解标量寄存器如何被「广播」成 16 通道的向量，从而与向量寄存器一起参与 SIMD 运算。
- 解释向量掩码（mask）的三种来源，以及它在「屏蔽某些通道」中的作用。
- 掌握向量子周期（subcycle）机制：为什么 scatter/gather 这类指令要用 16 个子周期串行执行，而 operand_fetch 在其中扮演什么角色。
- 理解记分牌（scoreboard）如何与这一级配合，保证读寄存器时拿到的总是「已经写回」的正确值。

## 2. 前置知识

在进入源码之前，先用通俗语言把几个概念铺平。

### 2.1 标量与向量

Nyuzi 是 GPGPU，它的算力来自**向量 SIMD**：

- **标量（scalar）**：一个 32 位的数，类型是 `scalar_t`（即 `logic[31:0]`）。
- **向量（vector）**：把 16 个标量拼在一起，类型是 `vector_t`，共 \(16 \times 32 = 512\) 位（64 字节）。这个 16 就是 `NUM_VECTOR_LANES`（通道数）。

一条向量加法指令 `add_v v0, v1, v2` 会在一个周期内同时对 16 个通道做加法，相当于 16 路并行。这正是 GPU 擅长的「大批量同类计算」。

### 2.2 「广播」是什么意思

很多时候一条向量指令的一个操作数是标量，比如「把标量寄存器 s1 里的值，加到向量寄存器 v2 的每一个通道上」。这时需要把那一个 32 位标量**复制 16 份**，撑成一个 512 位的向量，让 16 个通道都能用到它。这个「复制填充」的动作就叫**广播（broadcast）**。本讲你会看到它就是一行 `{NUM_VECTOR_LANES{scalar_val}}` 的 SystemVerilog 写法。

### 2.3 掩码（mask）

掩码是一个 16 位的位图 `vector_mask_t`，每一位对应一个通道。某位为 1 表示「这个通道参与运算/写回」，为 0 表示「跳过这个通道」。这让程序可以做条件向量运算，比如「只对满足条件的像素做混合」。掩码可以从某个标量寄存器的低 16 位取，也可以是「全 1」（不过滤）。

### 2.4 子周期（subcycle）

绝大多数向量指令 1 个周期就能完成（16 通道并行）。但 **scatter/gather（散布/收集）** 这类访存指令例外：每个通道要访问的内存地址都不一样，硬件无法在一个周期里同时发 16 个不同的访存请求。于是 Nyuzi 把这样一条指令拆成 **16 个子周期** 串行执行，每个子周期只处理一个通道。`subcycle_t` 就是这个 0~15 的计数器类型。

### 2.5 记分牌（scoreboard）回顾

上一讲（u4-l3）讲过，记分牌用一张位图记录「哪些寄存器正在等待写回」。当一条新指令要读某个寄存器、而它正好在「等待写回」列表里时，记分牌会阻止这条指令发射，从而避免读到旧值（RAW 冒险）。本讲你会看到：operand_fetch 真正去读寄存器的时机，已经被记分牌保证是安全的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `hardware/core/operand_fetch_stage.sv` | **主角**。内含标量与向量寄存器文件，负责读出操作数、生成掩码、广播标量，并把子周期信号向后传。 |
| `hardware/core/scoreboard.sv` | 记分牌。在指令到达 operand_fetch 之前判断「能否安全读寄存器」，是这一级正确性的前提。 |
| `hardware/core/defines.svh` | 定义 `scalar_t` / `vector_t` / `vector_mask_t` / `subcycle_t` 类型，以及 `op1_src_t` / `op2_src_t` / `mask_src_t` 三个「来源选择」枚举，和 `decoded_instruction_t` 结构体。 |
| `hardware/core/instruction_decode_stage.sv` | 解码级，负责把指令位段翻译成上面那三个「来源」开关，供本级消费。 |
| `hardware/core/thread_select_stage.sv` | 维护子周期计数器 `current_subcycle`，决定「这条 scatter/gather 指令当前处于第几个子周期」。 |
| `hardware/core/dcache_data_stage.sv` | 真正用子周期信号挑选 scatter/gather 通道的地方，说明 operand_fetch 传下去的 `subcycle` 是给谁用的。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看寄存器文件本身怎么组织（4.1），再看操作数怎么选（4.2），然后是掩码（4.3），最后是子周期（4.4）。记分牌的内容会穿插在 4.1 和 4.4 中，因为它和这两处关系最紧密。

### 4.1 寄存器文件的组织与读端口

#### 4.1.1 概念说明

operand_fetch_stage 模块的开头注释一句话点明了它的职责：

> Contains vector and scalar register files and fetches values from them.
> （内含向量与标量寄存器文件，并从中取值。）

也就是说，**寄存器文件 physically 就住在这个模块里**。它要同时维护：

- 32 个标量寄存器 × 每核线程数；
- 32 个向量寄存器（每个 16 通道）× 每核线程数。

回忆 u2-l1：Nyuzi 每核默认 4 个硬件线程（`THREADS_PER_CORE = 4`），寄存器是**按线程分体（banked）**的——每个线程有自己独立的一套寄存器，彼此不干扰。所以寄存器文件的总深度是 `32 * THREADS_PER_CORE`。

读端口的设计也很关键：一条指令可能同时需要两个源操作数（比如 `add` 的两个加数），所以寄存器文件需要**两个读端口**；而写回只有一个目标寄存器，所以**一个写端口**。这正是 Nyuzi 选用的 SRAM 宏 `sram_2r1w`（2 读 1 写）的含义。

#### 4.1.2 核心流程

标量寄存器文件的实例化可以概括为：

```
读端口1：地址 = {线程号, scalar_sel1}  →  scalar_val1   （当指令需要 scalar1 时使能）
读端口2：地址 = {线程号, scalar_sel2}  →  scalar_val2   （当指令需要 scalar2 时使能）
写端口 ：地址 = {线程号, 写回寄存器号} ←  写回值[0]      （当写回且非向量时使能）
```

地址的高位拼上「线程号」，正是实现「按线程分体」的方式：不同线程访问同一逻辑寄存器号时，物理上落到不同的存储单元。

向量寄存器文件则更进一步——它不是一块大 SRAM，而是**用 `generate` 循环实例化了 16 份独立的标量宽度 SRAM**，每一份对应一个通道（lane）。这样 16 个通道可以并行读写，天然匹配 SIMD 的并行结构。

#### 4.1.3 源码精读

先看类型定义。标量、向量、掩码、子周期这几个类型都集中定义在 defines.svh 的开头：

[hardware/core/defines.svh:42-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L52)：定义 `NUM_VECTOR_LANES = 16`、`scalar_t`（32 位）、`vector_t`（16 个标量拼成的数组）、`subcycle_t`（4 位，0~15）、`vector_mask_t`（16 位）。这是本讲全部数据的「尺寸基准」。

接着看标量寄存器文件的实例化。这一段是理解整个模块的钥匙：

[hardware/core/operand_fetch_stage.sv:61-75](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L61-L75)：用 `sram_2r1w` 实例化标量寄存器文件。注意三点：① 深度 `SIZE = 32 * THREADS_PER_CORE`，把所有线程的标量寄存器平铺成一张表；② 读地址是 `{ts_thread_idx, ts_instruction.scalar_sel1}`，用线程号做高位、寄存器号做低位；③ 读使能 `read1_en` 还要求 `ts_instruction.has_scalar1`——只有这条指令真的要用到 scalar1 时才打开读端口（省功耗、也避免读无意义地址）。写端口在「写回使能且不是向量写回」时打开，写入 `wb_writeback_value[0]`（标量只写第 0 通道）。

再看向量寄存器文件的实例化，它是「16 通道并行」的来源：

[hardware/core/operand_fetch_stage.sv:77-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L77-L97)：用 `generate for` 循环 16 次，每次实例化一个 `sram_2r1w`，对应一个 lane。所有 lane 的读地址相同（`{ts_thread_idx, vector_sel1}`），但各自输出到 `vector_val1[lane]`，于是 16 通道的数据同时被读出。注意写回使能里多了一个条件 `wb_writeback_mask[NUM_VECTOR_LANES - lane - 1]`——向量写回时，**只有掩码为 1 的通道才真正写入**，这正是掩码控制「哪些通道生效」在写回侧的体现（下标做了反向映射，对应硬件的通道编号约定）。

最后看一个容易忽略但很重要的细节——指令有效信号与回滚（rollback）的配合：

[hardware/core/operand_fetch_stage.sv:99-108](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L99-L108)：本级输出的 `of_instruction_valid`，在「写回级发起了回滚且回滚的就是当前这条指令所属的线程」时被置为 0。也就是说，如果一条分支在后面被发现预测错或需要回滚，本级会把这条已经读进来、但本不该执行的指令「作废」掉，不让它继续流向执行单元。这是流水线保持精确状态的重要一环。

#### 4.1.4 代码实践

**实践目标**：搞清楚寄存器文件的「容量」与「按线程分体」。

**操作步骤**：

1. 打开 `hardware/core/config.svh`，找到 `THREADS_PER_CORE` 的默认值（应为 4）。
2. 打开 `hardware/core/operand_fetch_stage.sv` 第 61-75 行，确认标量 SRAM 的 `SIZE = 32 * THREADS_PER_CORE`。
3. 心算：标量寄存器文件共 \(32 \times 4 = 128\) 个 32 位单元；向量寄存器文件是 16 份这样的表，共 \(128 \times 16 = 2048\) 个 32 位单元。

**需要观察的现象**：理解「同一个寄存器号 s5，在线程 0 和线程 1 是两个完全独立的存储单元」，因为它们地址的高位（线程号）不同。

**预期结果**：你能用一句话解释「为什么 4 线程切换不需要保存/恢复寄存器」——因为每个线程有自己专属的物理寄存器体，硬件靠地址高位天然区分。这与传统单线程 CPU 的「上下文切换要换寄存器」形成对比，是 GPU 多线程隐藏延迟的硬件基础。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `THREADS_PER_CORE` 改成 8，标量寄存器文件的 SIZE 会变成多少？需要改 operand_fetch_stage.sv 里的代码吗？

**答案**：SIZE 会变成 \(32 \times 8 = 256\)。**不需要**改 operand_fetch_stage.sv 的代码，因为它用的是 `` `THREADS_PER_CORE `` 宏参数，会自动随之变化。这正是参数化设计的便利。

**练习 2**：向量寄存器文件为什么不用一块 512 位宽的大 SRAM，而要拆成 16 份标量宽度的 SRAM？

**答案**：因为 SIMD 要求 16 个通道**并行**读写。拆成 16 份独立 SRAM 后，每份各自提供 2 读 1 写端口，16 通道可同时访问；若用单块大 SRAM，要么带宽不够，要么需要极多的读写端口，面积和功耗都不划算。

---

### 4.2 操作数选择与标量广播

#### 4.2.1 概念说明

寄存器文件读出来的原始数据有四种：`scalar_val1`、`scalar_val2`、`vector_val1`、`vector_val2`。但一条具体指令到底用哪个作为它的「操作数 1」「操作数 2」？这取决于解码级填好的两个开关：

- `op1_src`：操作数 1 的来源（向量1 还是 标量1）。
- `op2_src`：操作数 2 的来源（标量2、向量2、还是立即数）。

注意一个关键点：**执行单元（整数/浮点/访存）统一只认 `vector_t`（512 位向量）**。所以哪怕一条指令的操作数是标量，operand_fetch 也必须把它「撑成」向量形式送下去——这就是广播。这样设计的好处是执行单元不必区分标量/向量，逻辑大大简化。

`store_value`（要写入内存的值）也类似：向量 store 要送整个向量，标量 store 要把标量广播成向量再送（访存单元再取第 0 通道或按需处理）。

#### 4.2.2 核心流程

操作数选择可以总结成下面这张「开关 → 输出」表：

| 输出 | 开关 | 取值 | 来源 |
|------|------|------|------|
| `of_operand1` | `op1_src` | `OP1_SRC_VECTOR1` | `vector_val1` |
| `of_operand1` | `op1_src` | `OP1_SRC_SCALAR1` | 广播 `scalar_val1`（16 份） |
| `of_operand2` | `op2_src` | `OP2_SRC_SCALAR2` | 广播 `scalar_val2` |
| `of_operand2` | `op2_src` | `OP2_SRC_VECTOR2` | `vector_val2` |
| `of_operand2` | `op2_src` | `OP2_SRC_IMMEDIATE` | 广播 `immediate_value` |
| `of_store_value` | `store_value_vector` | 1（真） | `vector_val2` |
| `of_store_value` | `store_value_vector` | 0（假） | 广播 `scalar_val2` |

广播的「数学」很简单：把一个 32 位标量 \(x\) 复制 16 份，得到向量 \((x, x, \dots, x)\)，即 \[ \text{broadcast}(x) = \underbrace{(x, x, \ldots, x)}_{16 \text{ 个}} \] 这样它与另一个向量做逐通道运算时，每个通道都用到同一个 \(x\)。

#### 4.2.3 源码精读

先看三个「来源」枚举的定义，它们就是上表开关的取值：

[hardware/core/defines.svh:216-231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L216-L231)：定义 `mask_src_t`、`op1_src_t`、`op2_src_t` 三个枚举。注意 `op1_src_t` 只有两个值（向量1 / 标量1），`op2_src_t` 有三个值（标量2 / 向量2 / 立即数）——这反映了 Nyuzi 指令格式里「操作数 1 只能来自第一组寄存器，操作数 2 还可以是立即数」的编码约定。

这三个开关是解码级填好的。看解码级如何设置 `op1_src`：

[hardware/core/instruction_decode_stage.sv:351-359](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L351-L359)：解码级根据查表结果 `dlut_out.op1_vector` 决定操作数 1 是向量还是标量，`op2_src` 和 `mask_src` 直接来自查表输出 `dlut_out`。也就是说，「这条指令的操作数从哪来」这件事在解码时已经定死，本级只是照着开关连线。

现在看本级的「选择 + 广播」核心逻辑，这是整个模块最精彩的三段 `unique case`：

[hardware/core/operand_fetch_stage.sv:121-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L121-L132)：第一个 `case(op1_src)` 选操作数 1，当来源是标量时用 `{NUM_VECTOR_LANES{scalar_val1}}` 把标量广播成向量；第二个 `case(op2_src)` 选操作数 2，立即数来源时广播 `of_instruction.immediate_value`。这里的 `{N{x}}` 是 SystemVerilog 的复制拼接运算符，就是把 `x` 重复 N 次。

再看 store_value 的生成，它在 case 之前单独写：

[hardware/core/operand_fetch_stage.sv:117-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L117-L119)：`store_value_vector` 为真时送整个 `vector_val2`（向量 store），否则把 `scalar_val2` 广播成向量（标量 store，高位补 0）。注意这里的广播写法 `{{NUM_VECTOR_LANES - 1{32'd0}}, scalar_val2}`——只复制 1 份标量、其余 15 个通道填 0，因为标量 store 实际只用一个通道的值。

> **反直觉点**：执行单元完全不关心操作数「本来」是标量还是向量。对它而言，所有操作数都是 512 位向量。标量指令之所以表现为标量，只是因为它的两个操作数都被广播成了「16 通道全相同」的向量，运算结果自然也是 16 通道相同——程序只取第 0 通道即可。这种「标量即退化的向量」的统一视角，是 Nyuzi 简化执行单元的关键设计。

#### 4.2.4 代码实践

**实践目标**：追踪一条向量加法指令 `add_v v0, v1, s2`（v1 是向量、s2 是标量）在 operand_fetch_stage 里 op1/op2 的来源，说明标量 s2 如何被广播。

**操作步骤**：

1. 在 `hardware/core/operand_fetch_stage.sv` 第 121-132 行找到两个 `case` 块。
2. 对 `add_v v0, v1, s2`：操作数 1 是 v1（向量），所以 `op1_src = OP1_SRC_VECTOR1`，`of_operand1 = vector_val1`；操作数 2 是 s2（标量），所以 `op2_src = OP2_SRC_SCALAR2`，`of_operand2 = {16{scalar_val2}}`。
3. 画出这条指令的数据流：`vector_sel1` 选出 `vector_val1`（16 通道）→ 直接送 op1；`scalar_sel2` 选出 `scalar_val2`（1 个标量）→ 复制 16 份 → 送 op2。两者都送到整数执行级的 16 个通道做并行加法。

**需要观察的现象**：op1 是「天生的」16 通道向量（直接来自向量寄存器文件），op2 是「人造的」16 通道向量（由 1 个标量广播而来）。它们在执行单元看来没有区别。

**预期结果**：你能指出 `{NUM_VECTOR_LANES{scalar_val2}}` 这一行（operand_fetch_stage.sv 第 129 行）就是「标量广播」的发生地，并用一句话解释：广播让标量与向量能在同一套 SIMD 执行单元里混合运算。

> 待本地验证：如果环境允许，可写一段汇编 `add_v v0, v1, s2`（先给 v1 各通道赋不同值、s2 赋某常数），用模拟器 `nyuzi_emulator -v` 运行，观察 v0 的 16 个通道是否都等于「v1 各通道 + s2」，从而验证广播确实发生。

#### 4.2.5 小练习与答案

**练习 1**：一条 `add_i s0, s1, s2`（纯标量加法）在本级会发生广播吗？结果送下去是几位？

**答案**：会。op1 来自 `OP1_SRC_SCALAR1`，op2 来自 `OP2_SRC_SCALAR2`，两者都被广播成 512 位向量（16 通道全相同）送入整数执行单元。执行后 16 个通道结果相同，程序只关心第 0 通道。所以即使是「标量指令」，在硬件里也走的是向量通路。

**练习 2**：为什么 `op1_src` 没有「立即数」选项，而 `op2_src` 有？

**答案**：这是 Nyuzi 指令格式（u2-l1 讲过的 R/I/M/C/B 格式）决定的。立即数只能出现在操作数 2 的位置（I 格式指令），操作数 1 永远来自寄存器。所以 `op1_src_t` 只需要「向量1 / 标量1」两个值。

---

### 4.3 掩码生成

#### 4.3.1 概念说明

掩码 `of_mask_value` 是一个 16 位信号，它和操作数一起送到执行单元，用来控制「这次运算哪些通道真正生效」。比如 `add_v` 带掩码时，只有掩码为 1 的通道会执行加法并写回，掩码为 0 的通道保持原值不变。

掩码有三种来源，由 `mask_src` 开关选择：

- `MASK_SRC_SCALAR1`：取标量寄存器 1 的低 16 位作为掩码。
- `MASK_SRC_SCALAR2`：取标量寄存器 2 的低 16 位作为掩码。
- `MASK_SRC_ALL_ONES`：全 1，即「不过滤，所有通道都参与」。

绝大多数没有显式掩码的指令，解码级会把 `mask_src` 设成 `MASK_SRC_ALL_ONES`，于是所有通道都执行。

#### 4.3.2 核心流程

掩码生成的逻辑很短：

```
case (mask_src)
  MASK_SRC_SCALAR1:  of_mask_value = scalar_val1 的低 16 位
  MASK_SRC_SCALAR2:  of_mask_value = scalar_val2 的低 16 位
  MASK_SRC_ALL_ONES: of_mask_value = 16'b1111111111111111
endcase
```

注意掩码**只能来自标量寄存器**（不能来自向量寄存器），因为 16 位掩码正好对应一个标量的低半部分。这也是为什么指令格式里掩码字段引用的是标量寄存器号。

#### 4.3.3 源码精读

看本级生成掩码的代码，紧接在操作数选择之后：

[hardware/core/operand_fetch_stage.sv:134-138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L134-L138)：第三个 `case(mask_src)`。前两种情况用 `scalar_val1[NUM_VECTOR_LANES - 1:0]` / `scalar_val2[NUM_VECTOR_LANES - 1:0]` 取标量的低 16 位（`NUM_VECTOR_LANES - 1:0` 即 15:0）；默认情况（`MASK_SRC_ALL_ONES`）输出 `{NUM_VECTOR_LANES{1'b1}}`，即 16 个 1。

掩码的「消费」发生在两处：一是执行单元据它决定哪些通道参与运算；二是写回侧。回头看一下 4.1.3 里向量寄存器文件写端口的使能条件 `wb_writeback_mask[NUM_VECTOR_LANES - lane - 1]`——那就是掩码在写回侧的作用：**只有掩码为 1 的通道才会被真正写入向量寄存器**。所以掩码从「生成」（本级）到「生效」（写回级）形成了一个完整闭环。

再看解码级如何决定掩码来源，有个优化值得注意：

[hardware/core/instruction_decode_stage.sv:288-292](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L288-L292)：这里有一处与 `has_scalar1` 相关的判断（当掩码来源是 `MASK_SRC_SCALAR1` 时需要确保读了 scalar1）。它体现了「掩码来源」与「是否要打开对应读端口」之间的联动——如果掩码要用 scalar1，那标量寄存器文件的 read1 端口就必须打开，否则 scalar_val1 是垃圾值。

#### 4.3.4 代码实践

**实践目标**：理解「全 1 掩码」与「标量掩码」的区别，以及掩码如何屏蔽通道。

**操作步骤**：

1. 在 operand_fetch_stage.sv 第 134-138 行找到掩码生成的 `case`。
2. 设想一条带掩码的向量加法 `add_v v0, v1, v2, s3`（s3 的低 16 位作掩码），其中 s3 = `0b0000_0000_0000_1111`（低 4 位为 1）。
3. 推演：`mask_src = MASK_SRC_SCALAR2`（假设掩码来自第二标量），`of_mask_value = 16'b0000000000001111`。只有 v0 的低 4 个通道会被加法结果覆盖，高 12 个通道保持原值。

**需要观察的现象**：掩码是一个「逐通道开关」，与操作数一同送入执行单元，执行单元在每个通道独立判断「这一位是 1 吗？是则算，否则跳过」。

**预期结果**：你能解释为什么 GPU 的条件渲染（比如只更新深度测试通过的像素）可以用一条掩码向量指令完成——掩码让一条指令同时处理「参与」和「不参与」两种通道，无需分支。

#### 4.3.5 小练习与答案

**练习 1**：掩码可以从向量寄存器取吗？为什么？

**答案**：不能。掩码固定是 16 位，正好等于一个标量的低 16 位，所以 `mask_src` 只有「标量1 / 标量2 / 全1」三种来源。从向量寄存器取掩码没有对应的编码。

**练习 2**：一条没有写掩码寄存器的普通 `add_v v0,v1,v2`，它的 `of_mask_value` 是什么？

**答案**：是全 1（`16'hffff`）。解码级会把这种指令的 `mask_src` 设为 `MASK_SRC_ALL_ONES`，于是本级第 137 行输出 `{NUM_VECTOR_LANES{1'b1}}`，所有通道都参与。

---

### 4.4 向量子周期处理

#### 4.4.1 概念说明

本讲最后一个、也是最微妙的模块：子周期。

绝大多数向量指令，operand_fetch 一次性读出整个向量（16 通道并行），执行单元一个周期搞定。但 **scatter/gather** 不行——它每个通道要访问的内存地址都不同，硬件无法在同一周期发起 16 个独立访存。Nyuzi 的解法是：把一条 scatter/gather 指令**重发 16 次**，每次只处理一个通道，这个「次」就叫一个**子周期（subcycle）**。

这里要特别澄清一个**容易误解的点**：在 operand_fetch_stage 里，`subcycle` 信号其实**没有参与操作数选择**。本级做的事情是：

1. 像普通指令一样，把整个向量操作数（比如 gather 的「地址指针向量」）一次性读出来；
2. 把 `subcycle` 信号**原样向后传递**（`of_subcycle <= ts_subcycle`）；
3. 真正「按子周期挑通道」的工作，留给下游的 dcache_data_stage 去做。

换句话说，operand_fetch 对子周期是「透明」的——它每个子周期都读同样的整个向量，只是把「现在是第几个子周期」这个信息接力传下去。

#### 4.4.2 核心流程

子周期的完整生命周期跨越三个阶段，用伪代码表示：

```
# 解码级：标记这条指令需要几个子周期
if 指令是 scatter/gather:
    last_subcycle = 15      # 要走 16 个子周期（0..15）
else:
    last_subcycle = 0       # 普通指令只 1 个子周期

# 线程选择级：维护每线程的 current_subcycle 计数器
每个周期:
    if 当前指令被发射且 current_subcycle == last_subcycle:
        current_subcycle = 0          # 这条指令的所有子周期走完，出队
    elif 当前指令被发射:
        current_subcycle += 1         # 还没走完，下个周期继续发同一条指令

# 操作数 fetch 级：透传
    of_subcycle = current_subcycle    # 把「第几个子周期」传给 dcache

# dcache 数据级：按子周期挑通道
    scgath_lane = ~dt_subcycle        # 这个子周期处理哪个 lane
    只对该 lane 发起访存
```

这里还有一条与记分牌相关的重要规则：**记分牌只在第一个子周期（subcycle == 0）检查**。原因在源码注释里说得很清楚——记分牌只跟踪到「寄存器」粒度，不到「通道」粒度。gather 会向同一个目标寄存器写回 16 次（每子周期写一个通道），如果在第 2~16 个子周期还查记分牌，目标寄存器的「忙碌位」会挡住自己，导致 gather 走不下去。所以从第 2 个子周期起，记分牌检查被跳过。

#### 4.4.3 源码精读

先看类型与解码级的标记：

[hardware/core/defines.svh:51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L51)：`subcycle_t` 是 4 位（\( \lceil \log_2 16 \rceil = 4 \)），刚好表示 0~15。

[hardware/core/instruction_decode_stage.sv:410-421](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L410-L421)：解码级判断指令是否是 scatter/gather（`MEM_SCGATH` / `MEM_SCGATH_M`），是则 `last_subcycle = NUM_VECTOR_LANES - 1 = 15`，否则为 0。这把「这条指令要走几个子周期」在解码时就钉死了。

接着看线程选择级如何驱动子周期计数器：

[hardware/core/thread_select_stage.sv:164-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L164-L188)：这段是子周期的「发动机」。第 169-170 行的 `can_issue` 条件里，`(scoreboard_can_issue || current_subcycle[thread_idx] != 0)` 正是上面说的「只在第一个子周期查记分牌」——当 `current_subcycle != 0` 时，即使记分牌说不能发，也照样发（因为已经在走 scatter/gather 的中途）。第 175-176 行定义「是否是最后一个子周期」，第 184-187 行的计数器在每个发射周期自增，直到 `last_subcycle` 后归零。

而指令 FIFO 的「出队」也绑在最后一个子周期上：

[hardware/core/thread_select_stage.sv:131](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L131)：`dequeue_en(issue_last_subcycle[thread_idx])`——只有走完最后一个子周期，这条 scatter/gather 指令才真正离开 FIFO；中间各子周期它都留在队头，被反复读取、带着不同的 `current_subcycle` 重发。

现在回到本级，看它如何「透传」子周期：

[hardware/core/operand_fetch_stage.sv:110-115](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L110-L115)：本级用一段 `always_ff` 把 `ts_instruction`、`ts_thread_idx`、`ts_subcycle` 原样寄存一拍，变成 `of_*` 输出。注意这里**没有任何用 subcycle 选操作数的逻辑**——证实了「operand_fetch 对子周期透明」。

最后看子周期真正被消费的地方，在 dcache 数据级：

[hardware/core/dcache_data_stage.sv:173-187](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L173-L187)：`scgath_lane = ~dt_subcycle` 用子周期号选出当前要处理的通道；`idx_to_oh_subcycle` 把子周期号转成独热码 `subcycle_mask`，再用 `(dt_mask_value & subcycle_mask) != 0` 判断「当前子周期对应的通道是否被掩码启用」。这就解释了 operand_fetch 传下去的 `of_subcycle` 最终是给谁用的——给 dcache 用来挑 scatter/gather 的通道。注释还点出一个关键点：如果某通道被掩码屏蔽，硬件会忽略它的指针，**不会因为无效指针触发异常**。

> 把整条链串起来：解码级标 `last_subcycle=15` → 线程选择级把同一条指令发 16 次、`current_subcycle` 从 0 走到 15 → 操作数 fetch 级每次都读同一个完整向量、把 subcycle 透传 → dcache 按 `~subcycle` 挑通道、一次只访存一个地址 → 写回级把该通道结果写回目标寄存器的对应 lane（靠 4.1.3 里的掩码写使能）。16 次之后，整条 scatter/gather 才算完成。

#### 4.4.4 代码实践

**实践目标**：跟踪一条 gather load（从非连续地址收集数据到向量寄存器）在 operand_fetch 与下游的子周期流转，验证「本级只透传、不挑通道」。

**操作步骤**：

1. 在 operand_fetch_stage.sv 第 110-115 行确认 `of_subcycle <= ts_subcycle`，本级没有用 subcycle 做任何选择。
2. 在 thread_select_stage.sv 第 178-188 行确认：对于 gather（`last_subcycle = 15`），`current_subcycle` 从 0 自增到 15，每周期发一次，第 16 次（subcycle==15）后归零并出队。
3. 在 dcache_data_stage.sv 第 173 行确认 `scgath_lane = ~dt_subcycle`，即「子周期 0 处理 lane 15、子周期 1 处 lane 14……」，每个子周期只对一个 lane 发起访存。
4. 画出时序：同一个 PC 的 gather 指令，在连续 16 个（对该线程的）发射周期里，分别带着 subcycle=0,1,…,15 流过 operand_fetch，操作数每次都一样（同一个指针向量），但 dcache 每次挑不同 lane 的地址去访存。

**需要观察的现象**：operand_fetch 在这 16 个子周期里输出的 `of_operand1/of_operand2/of_mask_value` 完全相同（因为读的是同一个向量），只有 `of_subcycle` 在变。变化的 subcycle 被 dcache 用来挑通道。

**预期结果**：你能用一句话说明「为什么 operand_fetch 不需要在 16 个子周期里分别读不同通道」——因为它读的是 gather 的「地址指针向量」，本就是一个完整向量；真正「每次只取一个地址」的工作由 dcache 用 subcycle 完成。本级保持简单：读整个向量 + 透传 subcycle。

> 待本地验证：若有随机测试环境，可跑一个 gather 场景的 cosimulation（见 u8-l3），在 trace 里观察同一 PC 是否出现 16 次、每次 subcycle 递增。

#### 4.4.5 小练习与答案

**练习 1**：为什么记分牌只在 `subcycle == 0` 时检查？如果每个子周期都检查会怎样？

**答案**：因为 gather 向同一个目标寄存器写回 16 次。若每子周期都查记分牌，从第 2 个子周期起，目标寄存器的「忙碌位」已被自己置位，会挡住自己继续发射，导致 gather 永远走不完（死锁/严重延迟）。所以只在第一个子周期检查，后续子周期无条件继续。

**练习 2**：对于一条普通向量加法 `add_v`，`last_subcycle` 是多少？它会经过几个子周期？

**答案**：`last_subcycle = 0`，只经过 1 个子周期（`current_subcycle` 始终为 0）。普通向量指令 16 通道并行，一个周期完成，不需要子周期拆分。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「数据流追踪」小任务。

**任务**：给定下面这条指令（带标量掩码的向量加法）：

```
add_v v0, v1, s2, s3     ; v1 + 广播(s2)，掩码来自 s3，结果写回 v0
```

请按下面的步骤，把数据在 operand_fetch_stage 里的完整旅程讲清楚：

1. **寄存器文件读出**：标量文件读端口 1/2 分别读出哪两个值？向量文件读出什么？（提示：参考 4.1.3 的实例化代码，注意哪些读端口会被 `has_*` 使能打开。）
2. **操作数选择与广播**：`op1_src` 和 `op2_src` 各是什么？哪个操作数发生了广播？广播发生在源码第几行？（提示：4.2.3，operand_fetch_stage.sv 第 121-132 行。）
3. **掩码生成**：`mask_src` 是什么？`of_mask_value` 如何由 s3 得到？这个掩码最终在哪里「生效」？（提示：4.3，写回侧的 `wb_writeback_mask`。）
4. **子周期**：这条指令的 `last_subcycle` 是几？需要 16 个子周期吗？为什么？（提示：4.4，它不是 scatter/gather。）
5. **记分牌协同**：假设上一条指令还在写 v1，这条 `add_v` 能立刻发射吗？为什么？（提示：参考 scoreboard.sv，目标/源寄存器的忙碌位。）

**预期产出**：一张标注了「读端口 → 操作数选择 → 广播 → 掩码 → 送往执行单元」的数据流图，并附文字说明这条指令只走 1 个子周期、且会被记分牌正确地等到 v1 写回后才发射。

**参考答案要点**：

1. 标量文件读端口 1 读 s3（掩码来源 `MASK_SRC_SCALAR2`，假设这里掩码来自第二标量），读端口 2 读 s2（op2 标量来源）；向量文件读出 `vector_val1`（v1）。具体哪个端口读哪个，取决于解码级对 `scalar_sel1/scalar_sel2` 的赋值。
2. `op1_src = OP1_SRC_VECTOR1`（v1，不广播），`op2_src = OP2_SRC_SCALAR2`（s2，**广播**为 16 通道）。广播发生在第 129 行 `{NUM_VECTOR_LANES{scalar_val2}}`。
3. `mask_src = MASK_SRC_SCALAR2`，`of_mask_value = scalar_val2[15:0]`（即 s3 的低 16 位，若掩码来自 s3 则对应那个标量寄存器）。掩码在写回侧通过 `wb_writeback_mask[lane]` 控制哪些通道真正写入 v0。
4. `last_subcycle = 0`，只需 1 个子周期——因为这不是 scatter/gather，16 通道并行加法一个周期完成。
5. 不能立刻发射。scoreboard.sv 第 152 行 `scoreboard_can_issue = (scoreboard_regs & dep_bitmap) == 0`，v1 在 `dep_bitmap` 里（它是源寄存器），而它的忙碌位已被上一条指令置位，所以 `&` 结果非零，`can_issue` 为假，必须等 v1 写回、忙碌位清除后才能发射。

## 6. 本讲小结

- **寄存器文件住在 operand_fetch_stage 里**：标量文件是 1 块 `sram_2r1w`（深度 `32*线程数`），向量文件是 16 块 `sram_2r1w`（每块一通道），靠地址高位「线程号」实现按线程分体。
- **操作数选择由解码级预置的三个开关决定**：`op1_src`（向量1/标量1）、`op2_src`（标量2/向量2/立即数）、`mask_src`（标量1/标量2/全1），本级用三个 `unique case` 照开关连线。
- **标量通过广播变成向量**：`{NUM_VECTOR_LANES{scalar}}` 把一个标量复制 16 份，使标量与向量能在同一套 SIMD 执行单元里混合运算——「标量即退化的向量」。
- **掩码是逐通道开关**：16 位掩码来自标量低 16 位或全 1，从本级生成、在写回侧通过 `wb_writeback_mask` 控制哪些通道真正写入。
- **子周期只服务于 scatter/gather**：这类指令被重发 16 次，operand_fetch 每次读同一个完整向量并把 `subcycle` 透传，真正挑通道的工作在 dcache 用 `~subcycle` 完成。
- **记分牌保证读安全**：本级敢放心读寄存器，是因为记分牌已确保源寄存器不在「等待写回」状态；且记分牌只在第一个子周期检查，避免 gather 反复写同一寄存器时自我阻塞。

## 7. 下一步学习建议

本讲产出的是「打包好的操作数 + 掩码 + 子周期」，它们接下来要分流到三条执行路径。建议接着学：

- **u5-l2 整数执行单元**：看 `int_execute_stage` 如何消费 `of_operand1/of_operand2/of_mask_value`，对 16 通道并行做整数 ALU 运算、解析分支并触发回滚。本讲的「广播」在那里变成「每个通道独立算」。
- **u5-l3 浮点五级流水线**：看浮点路径如何接收同样的操作数，但走 5 级流水。本讲的 `pipeline_sel` 字段就是分流到这条路径的开关。
- **u6-l1 L1 数据缓存**：本讲的 `of_store_value` 和 `of_subcycle` 在那里被真正用于访存；scatter/gather 的逐通道访存细节在 `dcache_data_stage` 展开。
- 若想再巩固「寄存器写回」这一侧，可回看 u4-l3 中 scoreboard 与 writeback_allocate 的内容，与本讲 4.1.3 的写端口使能对照，形成「读—算—写」的完整闭环。
