# ISA 总览：寄存器组与向量 SIMD

## 1. 本讲目标

本讲是 Nyuzi 指令集架构（ISA）的第一讲。读完本讲后，你应当能够：

- 说出 Nyuzi 指令集的几个硬性「常数」：32 位定长指令、32 个标量寄存器、32 个向量寄存器、16 通道 SIMD。
- 解释 `scalar_t` 与 `vector_t` 的位宽构成，以及一条向量指令如何把标量操作数广播到 16 个通道并行运算。
- 看懂 Nyuzi 指令的五大格式（寄存器算术 / 立即数算术 / 访存 / 缓存控制 / 分支），并能对照解码表说出 `op1/op2/mask/store_value` 各自来自哪个寄存器。
- 理解 `subcycle`（子周期）与 `vector mask`（向量掩码）这两个向量编程中绕不开的概念，尤其是「为什么大多数向量指令是并行的，而 scatter/gather 却要逐通道串行」。

本讲只建立 ISA 的「数据模型」与「格式地图」，**不**逐条背诵指令。具体算术/访存/分支指令的语义留给后续 u2-l2、u2-l3、u2-l4 三讲展开。

## 2. 前置知识

阅读本讲前，建议你已具备 u1-l1（项目定位与整体架构）建立的全局认知。本讲会用到的几个术语解释如下：

- **ISA（Instruction Set Architecture，指令集架构）**：软件与硬件之间的「合同」。软件按 ISA 写指令，硬件按 ISA 执行指令。Nyuzi 的特殊之处在于它的 ISA 由两份「镜像」定义——一份是 SystemVerilog（`defines.svh`，硬件用），一份是 C（`instruction-set.h`，模拟器用）。两者必须编码一致，否则硬件与模拟器跑出来的结果就对不上，协同仿真就会失败。
- **SIMD（Single Instruction Multiple Data，单指令多数据）**：一条指令同时对多个数据做同样的运算。Nyuzi 的 SIMD 宽度是 16，即一条向量指令一次处理 16 个 32 位的值。
- **标量（scalar）与向量（vector）**：标量是单个 32 位的值；向量是 16 个标量打包在一起的数据。
- **lane（通道）**：向量里的第 `i` 个元素称为第 `i` 个通道，Nyuzi 共 16 个通道，编号 0 到 15。

## 3. 本讲源码地图

本讲涉及的关键源码文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [`hardware/core/defines.svh`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 硬件侧的「ISA 圣经」。定义了 `scalar_t`、`vector_t`、寄存器索引宽度、`alu_op_t`/`memory_op_t`/`branch_type_t` 等所有指令编码枚举，以及解码后的 `decoded_instruction_t` 结构。贯穿整个硬件代码库。 |
| [`tools/emulator/instruction-set.h`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) | 模拟器侧的 ISA 定义。用 C 的 `enum` 镜像了 `defines.svh` 里的同一套编码。硬件与模拟器共享 ISA 的「单一定义」。 |
| [`hardware/core/instruction_decode_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv) | 硬件解码级。把 32 位原始指令按格式查表，填充成 `decoded_instruction_t`。文件顶部那张「寄存器端口到操作数映射表」是理解所有格式的钥匙。 |

辅助文件（用于讲清子周期与并行执行）：

| 文件 | 作用 |
| --- | --- |
| [`hardware/core/operand_fetch_stage.sv`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv) | 操作数取回级。从这里可以「亲眼看到」16 个向量通道是并行读出的，以及标量如何被广播到所有通道。 |
| [`tools/emulator/processor.c`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | 模拟器核心。其中 scatter/gather 的实现用 `subcycle` 逐通道串行访存，是理解子周期的最直观样本。 |
| [`hardware/core/config.svh`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) | 可配置参数。本讲用到 `THREADS_PER_CORE`，确认默认每核 4 个线程。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**寄存器模型**、**向量 SIMD**、**指令格式分类**。

### 4.1 寄存器模型

#### 4.1.1 概念说明

寄存器是 CPU 里离运算单元最近的「快存」，比缓存还快。Nyuzi 有两大类寄存器：

- **32 个标量寄存器** `s0`–`s31`：每个 32 位，存放单个整数或单个浮点数。和普通 RISC CPU 的通用寄存器类似。
- **32 个向量寄存器** `v0`–`v31`：每个 512 位（16 × 32），存放一整条向量。这是 Nyuzi 作为 GPGPU 的核心特征。

两类寄存器编号都是 0–31，所以寄存器索引只需 5 位（\( \log_2 32 = 5 \)）。约定 `s31`/`v31` 有特殊用途——它是链接寄存器 `RA`（Return Address），保存函数调用的返回地址。

#### 4.1.2 核心流程

寄存器在硬件里是按「线程」分体的。Nyuzi 每核默认 4 个硬件线程（`THREADS_PER_CORE = 4`），每个线程都有自己独立的一套 32 个标量寄存器和 32 个向量寄存器，线程之间互不干扰。所以一个核内的寄存器文件实际容量是：

\[
\text{标量寄存器总数} = 32 \times 4 = 128 \text{ 个} \quad (\text{默认配置})
\]

向量寄存器同理。这正是 u1-l1 里提到的「每核 4 线程」在寄存器层面的体现。访问寄存器时，硬件用 `{线程号, 寄存器号}` 拼成地址去查表。

#### 4.1.3 源码精读

寄存器模型的核心定义全在 `defines.svh` 顶部。先看「常数」：

```systemverilog
parameter NUM_VECTOR_LANES = 16;
parameter NUM_REGISTERS = 32;
```

这两行钉死了 Nyuzi 的 SIMD 宽度（16 通道）和每组寄存器数量（32 个）。详见 [defines.svh:42-43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L43)（这两行同时说明：硬件里 16 通道是写死的常量，不是可配置参数）。

接着是类型定义——这是本讲最重要的代码段：

```systemverilog
typedef logic[31:0] scalar_t;
typedef scalar_t[NUM_VECTOR_LANES - 1:0] vector_t;
...
typedef logic[4:0] register_idx_t;
typedef logic[$clog2(NUM_VECTOR_LANES) - 1:0] subcycle_t;
typedef logic[NUM_VECTOR_LANES - 1:0] vector_mask_t;
```

这几行见 [defines.svh:46-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L46-L52)。逐行拆解：

- `scalar_t`：32 位的标量类型，对应一个标量寄存器。
- `vector_t`：`scalar_t` 的数组，数组长度是 `NUM_VECTOR_LANES`（16）。所以一个向量寄存器的位宽是：

\[
\text{vector\_t 位宽} = 32 \times 16 = 512 \text{ 位} = 64 \text{ 字节}
\]

  注意「64 字节」这个数字后面会再次出现——它正好等于一个缓存行（`CACHE_LINE_BYTES`），这是 Nyuzi 刻意的设计，让整条向量能一次性塞进一个缓存行。

- `register_idx_t`：5 位，范围 0–31，用于索引标量或向量寄存器。
- `subcycle_t`：4 位（因为 \( \log_2 16 = 4 \)），范围 0–15，记录当前是第几个子周期（见 4.2）。
- `vector_mask_t`：16 位，每一位对应一个向量通道，决定该通道是否参与运算（见 4.2）。

再看链接寄存器与 NOP 指令的定义：

```systemverilog
parameter INSTRUCTION_NOP = 32'd0;
parameter REG_RA = register_idx_t'(31);
```

见 [defines.svh:77-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L77-L78)。`REG_RA = 31` 说明第 31 号寄存器被约定为返回地址；`INSTRUCTION_NOP = 0` 说明全 0 的 32 位指令就是空操作——这个约定让解码器能用 `ifd_instruction == 0` 一眼识别 NOP（见 [instruction_decode_stage.sv:236](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L236)）。

最后确认线程数。寄存器是按线程分体的，默认线程数来自配置：

```systemverilog
`define THREADS_PER_CORE 4
```

见 [config.svh:41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L41)。结合 `TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES`（[defines.svh:44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L44)），默认配置下单核 4 线程。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手算清 Nyuzi 单核寄存器文件的总容量，建立直观感受。
2. **操作步骤**：
   - 打开 `defines.svh`，确认 `NUM_REGISTERS = 32`、`NUM_VECTOR_LANES = 16`、`scalar_t` 为 32 位。
   - 打开 `config.svh`，确认 `THREADS_PER_CORE = 4`。
   - 按下表计算并填空：

| 量 | 计算式 | 结果 |
| --- | --- | --- |
| 一个标量寄存器位宽 | — | 32 位 |
| 一个向量寄存器位宽 | 32 × 16 | 512 位 = 64 字节 |
| 单线程标量寄存器总位宽 | 32 × 32 | 1024 位 |
| 单线程向量寄存器总位宽 | 32 × 512 | 16384 位 |
| 单核（4 线程）向量寄存器总容量 | 16384 × 4 | 65536 位 = 8 KiB |

3. **需要观察的现象**：把上表算完后，你会发现「单核向量寄存器就有 8 KiB」——这比很多 MCU 的 L1 缓存还大，直观说明 Nyuzi 是为大规模数据并行设计的。
4. **预期结果**：单核向量寄存器约 8 KiB；标量寄存器约 1 KiB。寄存器编号 0–31 用 5 位表示。
5. 本实践为源码阅读型，无需运行命令，结果可由定义直接推得。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NUM_VECTOR_LANES` 改成 8，`vector_t` 的位宽是多少？`vector_mask_t` 又需要几位？

**答案**：`vector_t` = 32 × 8 = 256 位；`vector_mask_t` = 8 位（每通道一位）。注意这会牵连缓存行大小（`CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4` 会变成 32 字节），所以在真实项目里改这个参数代价很大。

**练习 2**：为什么 `register_idx_t` 是 5 位而不是 6 位？

**答案**：因为每组寄存器恰好 32 个，\( \lceil \log_2 32 \rceil = 5 \)，5 位即可表示 0–31。6 位会浪费一位。

---

### 4.2 向量 SIMD 与 subcycle

#### 4.2.1 概念说明

「向量 SIMD」是 Nyuzi 最核心的算力来源。一条向量加法 `add_i v0, v1, v2` 的含义是：把 `v1` 和 `v2` 的 16 个通道**对应相加**，结果写回 `v0` 的 16 个通道。关键在于这 16 个加法是**同一周期并行**完成的，不是串行 16 次。这正是 SIMD「单指令多数据」的精髓。

围绕向量运算有三个必须分清的概念：

1. **通道并行**：算术/逻辑这类纯运算指令，16 个通道在硬件里真正并行执行（硬件用 `generate` 实例化了 16 套运算逻辑）。
2. **标量广播（broadcast）**：当一条向量指令的某个操作数是标量时，硬件把这个标量「复制」到 16 个通道，让每个通道都拿到同一个值。例如 `add_i v0, v1, s2` 表示「`v1` 的每个通道都加上 `s2`」。
3. **子周期（subcycle）**：有**一类**指令无法并行——`scatter/gather`（散布/收集）访存。这类指令每个通道要访问**不同**的内存地址，而内存系统一次只能服务一个地址，于是必须把同一条指令重发 16 次，每次只处理一个通道。这个「第几次重发」就是 `subcycle`，取值 0–15。

> ⚠️ 常见误解纠正：**并非所有向量指令都走 subcycle**。绝大多数向量算术指令是 1 个周期并行完成的；只有 scatter/gather 这类每通道地址不同的访存才需要 16 个子周期逐通道串行。后续的「代码实践」会专门验证这一点。

向量掩码（mask）则用来做**条件向量运算**：一个 16 位的 `vector_mask_t`，第 `i` 位为 1 表示第 `i` 通道参与运算并写回，为 0 表示该通道保持原值不变。这让一条指令就能表达「对满足条件的通道做运算」。

#### 4.2.2 核心流程

**并行执行的向量算术指令**（以 `add_i v0, v1, s2` 为例，掩码全 1）：

```
取指 → 解码(得知 op1=v1向量, op2=s2标量, mask=全1)
       → 操作数取回: 读 v1 的 16 个通道; 把 s2 广播成 16 份
       → 整数执行: 16 个加法器同一周期算出 16 个和
       → 写回: 按 mask 把 16 个结果写进 v0
```

这里只有 1 条指令、1 个执行周期，16 个通道同时算。

**逐通道串行的 scatter/gather 指令**（以 load gather `load_gather v0, (v1)` 为例）：

```
subcycle=0: 读 v1[0] 作为地址, 从内存取值写进 v0[0]; 重发本指令
subcycle=1: 读 v1[1] 作为地址, 从内存取值写进 v0[1]; 重发本指令
...
subcycle=15: 读 v1[15] 作为地址, 从内存取值写进 v0[15]; 完成, PC 前进
```

同一条指令被重发了 16 次（16 个子周期），每次只动一个通道。当前进行到第几个子周期，由 `CR_SUBCYCLE` 控制寄存器保存，以便在发生 trap 后能恢复到正确的通道继续。

#### 4.2.3 源码精读

**先看并行的真相**——`operand_fetch_stage.sv` 如何同时读出 16 个通道。这里用 SystemVerilog 的 `generate` 循环实例化了 16 个独立的向量寄存器存储体（每个存 32 位）：

```systemverilog
genvar lane;
generate
    for (lane = 0; lane < NUM_VECTOR_LANES; lane++)
    begin : vector_lane_gen
        sram_2r1w #(.DATA_WIDTH($bits(scalar_t)), .SIZE(32 * `THREADS_PER_CORE), ...)
            vector_registers (
                .read1_addr({ts_thread_idx, ts_instruction.vector_sel1}),
                .read1_data(vector_val1[lane]),
                ...
            );
    end
endgenerate
```

见 [operand_fetch_stage.sv:77-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L77-L97)。循环跑 16 次，生成 16 个 `sram_2r1w` 实例，即 16 套存储体。这意味着 v1 的 16 个通道能**在同一周期**被并行读出到 `vector_val1[0..15]`——这就是 SIMD 的硬件基础。

再看标量广播与掩码生成：

```systemverilog
unique case (of_instruction.op1_src)
    OP1_SRC_VECTOR1: of_operand1 = vector_val1;
    default:         of_operand1 = {NUM_VECTOR_LANES{scalar_val1}};    // 标量广播
endcase
unique case (of_instruction.op2_src)
    OP2_SRC_SCALAR2: of_operand2 = {NUM_VECTOR_LANES{scalar_val2}};    // 标量广播
    OP2_SRC_VECTOR2: of_operand2 = vector_val2;
    default:         of_operand2 = {NUM_VECTOR_LANES{of_instruction.immediate_value}}; // 立即数广播
endcase
unique case (of_instruction.mask_src)
    MASK_SRC_SCALAR1: of_mask_value = scalar_val1[NUM_VECTOR_LANES - 1:0]; // 低16位作掩码
    MASK_SRC_SCALAR2: of_mask_value = scalar_val2[NUM_VECTOR_LANES - 1:0];
    default:          of_mask_value = {NUM_VECTOR_LANES{1'b1}};           // 全1
endcase
```

见 [operand_fetch_stage.sv:121-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L121-L139)。三个 `case` 分别决定 op1、op2、mask：

- `{NUM_VECTOR_LANES{scalar_val1}}` 是 SystemVerilog 的复制语法，把一个 32 位标量复制 16 份拼成 512 位——这就是「标量广播」的字面实现。
- 掩码取自某个标量寄存器的**低 16 位**（`scalar_val1[15:0]`），每一位对应一个通道。

**再看串行的真相**——`instruction_decode_stage.sv` 如何标记「这条指令需要多个子周期」：

```systemverilog
if (ifd_instruction[31:30] == 2'b10
    && (memory_access_type == MEM_SCGATH || memory_access_type == MEM_SCGATH_M))
begin
    // Scatter/Gather access
    decoded_instr_nxt.last_subcycle = subcycle_t'(NUM_VECTOR_LANES - 1);
end
else
    decoded_instr_nxt.last_subcycle = 0;
```

见 [instruction_decode_stage.sv:410-421](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L410-L421)。`last_subcycle` 字段记录「这条指令最后一个子周期编号」：普通指令是 0（只执行一次）；只有 scatter/gather 被设成 15（要执行 16 次）。注意 `decoded_instruction_t` 里这个字段的注释明确写着「count of last subcycle, not a boolean flag」（[defines.svh:283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L283)）——它是个计数，不是布尔标志。

模拟器侧的 scatter/gather 实现把子周期循环写得最直观：

```c
lane = thread->subcycle;
virtual_address = thread->vector_reg[ptrreg][lane] + offset;
...
if (is_load) {
    ...
    if (mask & (1 << lane))
        load_value[lane] = *UINT32_PTR(..., physical_address);
    set_vector_reg(thread, destsrcreg, mask & (1 << lane), load_value);
}
...
if (++thread->subcycle == NUM_VECTOR_LANES)
    thread->subcycle = 0;      // 全部通道处理完
else
    thread->pc -= 4;           // 还没完, 回退 PC 重发本指令
```

见 [processor.c:1543-1594](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1543-L1594)。每次只取 `lane = thread->subcycle` 这一个通道的地址，做完一个通道后 `++subcycle`；只要还没到 16，就把 `pc` 减 4（指令长度 4 字节）让同一条指令再执行一次。这是「逐通道串行」最直白的代码体现。

最后，子周期状态由 `CR_SUBCYCLE` 控制寄存器保存，编号 13：

```systemverilog
CR_SUBCYCLE             = 5'd13,
```

见 [defines.svh:179](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L179)。当一个 scatter/gather 指令执行到一半（比如 subcycle=7）被中断打断，硬件会把当前 subcycle 存进 `CR_SUBCYCLE`，等中断返回后从这里继续，而不是从头重来——这保证了向量访存的可恢复性。

#### 4.2.4 代码实践（本讲主实践）

> 对应大纲实践任务：列出 `vector_t` 的位宽构成，并解释一条向量指令如何通过 subcycle 逐通道执行。

1. **实践目标**：用源码证据区分「并行向量指令」与「逐通道串行的 scatter/gather」，并算清 `vector_t` 位宽。
2. **操作步骤**：
   - **第一步——算位宽**。对照 [defines.svh:46-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L46-L47)：
     - `scalar_t = logic[31:0]` → 32 位；
     - `vector_t = scalar_t[NUM_VECTOR_LANES-1:0]`，`NUM_VECTOR_LANES = 16` → 16 × 32 = **512 位**；
     - 同步确认模拟器侧：在 [instruction-set.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) 中用 `grep -n "NUM_VECTOR_LANES"` 找到模拟器对 `NUM_VECTOR_LANES` 的定义（在 `processor.h` 中，通常为 16），确认两边一致。
   - **第二步——验证「大多数向量指令是并行的」**。阅读 [operand_fetch_stage.sv:77-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L77-L97)，确认 16 个通道通过 `generate` 并行实例化、同一周期读出。
   - **第三步——验证「只有 scatter/gather 才走 subcycle」**。阅读 [instruction_decode_stage.sv:410-421](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L410-L421)，确认只有 `MEM_SCGATH`/`MEM_SCGATH_M` 把 `last_subcycle` 设成 15，其他全是 0。
   - **第四步——跟踪一条 scatter/gather 的逐通道执行**。阅读 [processor.c:1543-1594](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1543-L1594)，画出它的状态机。
3. **需要观察的现象**：在第四步中，你会看到「`pc -= 4` 重发指令」这个关键动作——这是子周期机制的指纹。普通向量算术指令里**不会**出现 `pc -= 4` 重发。
4. **预期结果**：
   - `vector_t` = 512 位 = 64 字节。
   - 向量算术指令（如 `add_i`）1 周期、16 通道并行；**不**使用 subcycle。
   - scatter/gather 访存使用 16 个 subcycle 逐通道串行，由 `pc -= 4` 重发实现，状态存于 `CR_SUBCYCLE`。
5. 如想运行验证：在搭好环境后，可写一个含 `load_gather` 的小程序，用 `nyuzi_emulator -v` 运行，在 trace 里应能看到同一条指令的 PC 连续出现 16 次（每次 `subcycle` 递增），而一条 `add_i` 向量加法只出现一次。

#### 4.2.5 小练习与答案

**练习 1**：一条 `add_i v0, v1, s2`（向量 + 标量广播）指令，硬件里发生了几次加法？占几个执行周期？

**答案**：硬件有 16 个并行加法器，所以发生 16 次加法，但它们在同一周期并行完成，指令只占 1 个执行周期（不计流水线排队）。`s2` 通过 `{NUM_VECTOR_LANES{scalar_val2}}` 广播到 16 个通道。

**练习 2**：为什么 scatter/gather 不能像 `add_i` 那样 1 周期并行，而要 16 个子周期？

**答案**：因为 scatter/gather 的每个通道访问**不同的内存地址**（地址来自 `v_ptr[0..15]`）。内存系统每个周期只能服务一个地址请求，无法同时响应 16 个不同地址，所以必须串行处理 16 次。而 `add_i` 的 16 个通道是纯寄存器运算，没有共享的内存端口瓶颈，可以真正并行。

**练习 3**：`CR_SUBCYCLE` 控制寄存器解决了什么问题？

**答案**：它保存 scatter/gather 执行到一半时的当前通道号。若这类指令被中断打断，中断返回后能从断点通道继续，而不是从通道 0 重来，避免已写过的通道被重复写入。

---

### 4.3 指令格式分类

#### 4.3.1 概念说明

Nyuzi 所有指令都是 **32 位定长**。这一约定（和 MIPS、RISC-V 类似）让取指与 PC 计算变得简单：下一条指令的地址就是 `PC + 4`，分支目标也是按 4 字节对齐。

32 位里要塞下「操作码 + 最多三个寄存器号 + 立即数」，靠的是**指令格式（format）**的划分。Nyuzi 把指令分成五大类，每类用 32 位里的不同位段表达不同含义：

| 格式 | 名称 | 典型用途 | 识别位 |
| --- | --- | --- | --- |
| R | 寄存器算术 | `add_i v0,v1,v2`、`add_f` 等，操作数全来自寄存器 | `inst[31:29] == 110` |
| I | 立即数算术 | `add_i s0,s1,100`，第二操作数是立即数 | `inst[31] == 0` |
| M | 访存 | `load_32`/`store_32`、块向量、scatter/gather | `inst[31:30] == 10` |
| C | 缓存控制 | `cache` 指令：TLB 插入、失效、membar | `inst[31:28] == 1110` |
| B | 分支 | 条件分支、call、eret | `inst[31:28] == 1111` |

各类格式内部还会再分子格式（例如 R 格式分「标量/标量」「向量/标量」「向量/向量」及是否带掩码），以表达「操作数来自标量还是向量」。

#### 4.3.2 核心流程

解码一条指令的关键是搞清四个信息从哪来：

- **op1**：第一操作数（向量或标量）。
- **op2**：第二操作数（向量、标量或立即数）。
- **mask**：向量掩码来源。
- **store_value**：对于 store 指令，要写入内存的值。

`instruction_decode_stage.sv` 文件顶部有一张「寄存器端口到操作数映射表」，是理解所有格式的总纲：

```
// Register port to operand mapping
//                                       store
//       format           op1     op2    mask    value
// +-------------------+-------+-------+-------+-------+
// | R - scalar/scalar |   s1  |   s2  |       |       |
// | R - vector/scalar |   v1  |   s2  |  s1   |       |
// | R - vector/vector |   v1  |   v2  |  s2   |       |
// | I - scalar        |   s1  |  imm  |  n/a  |       |
// | I - vector        |   v1  |  imm  |  s2   |       |
// | M - scalar        |   s1  |  imm  |  n/a  |  s2   |
// | M - block         |   s1  |  imm  |  s2   |  v2   |
// | M - scatter/gather|   v1  |  imm  |  s2   |  v2   |
// | C                 |   s1  |  imm  |       |       |
// | B                 |   s1  |       |       |       |
// +-------------------+-------+-------+-------+-------+
```

读法举例：「R - vector/scalar」这一行表示：op1 来自向量寄存器 v1，op2 来自标量寄存器 s2，掩码来自标量寄存器 s1 的低 16 位（这正是「带掩码的向量/标量」格式）。这张表把 32 位里那些位段映射到了 `decoded_instruction_t` 的字段上。

#### 4.3.3 源码精读

格式的识别只看最高几位的特征码：

```systemverilog
assign fmt_r = ifd_instruction[31:29] == 3'b110;    // register arithmetic
assign fmt_i = ifd_instruction[31] == 1'b0;         // immediate arithmetic
assign fmt_m = ifd_instruction[31:30] == 2'b10;
```

见 [instruction_decode_stage.sv:229-231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L229-L231)。缓存控制与分支则在别处用 `inst[31:28] == 4'b1110` / `4'b1111` 识别（见 [instruction_decode_stage.sv:406-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L406-L407) 与 [378](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L378)）。

具体的「位段 → 字段」映射，由 `casez` 查表完成。表中每行是一条「最高 7 位模式 → 一组解码控制信号」的映射（格式 R 的几行）：

```systemverilog
unique casez (ifd_instruction[31:25])
    // Format R (register arithmetic)
    7'b110_000_?: dlut_out = {F, F, T, IMM_ZERO, SCLR1_4_0, SCLR2_19_15,   F, F, F, F, OP2_SRC_SCALAR2, MASK_SRC_ALL_ONES, F, F};  // 标量/标量
    7'b110_001_?: dlut_out = {F, T, T, IMM_ZERO, SCLR1_4_0, SCLR2_19_15,   T, F, F, T, OP2_SRC_SCALAR2, MASK_SRC_ALL_ONES, F, F};  // 向量/标量
    7'b110_010_?: dlut_out = {F, T, T, IMM_ZERO, SCLR1_14_10, SCLR2_19_15, T, F, F, T, OP2_SRC_SCALAR2, MASK_SRC_SCALAR1, F, F};  // 带掩码向量/标量
    7'b110_100_?: dlut_out = {F, T, T, IMM_ZERO, SCLR1_14_10, SCLR2_NONE,  T, T, F, T, OP2_SRC_VECTOR2, MASK_SRC_ALL_ONES, F, F};  // 向量/向量
    7'b110_101_?: dlut_out = {F, T, T, IMM_ZERO, SCLR1_4_0, SCLR2_14_10,   T, T, F, T, OP2_SRC_VECTOR2, MASK_SRC_SCALAR2, F, F};  // 带掩码向量/向量
    ...
```

见 [instruction_decode_stage.sv:162-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L162-L170)（R 格式）。`dlut_out` 是一个打包结构体，字段依次为：`illegal, dest_vector, has_dest, imm_loc, scalar1_loc, scalar2_loc, has_vector1, has_vector2, vector_sel2_9_5, op1_vector, op2_src, mask_src, store_value_vector, call`（结构体定义见 [instruction_decode_stage.sv:123-138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L123-L138)）。每行就是「这种 7 位模式 → 这些控制位」。

注意表中体现的几个格式细节：
- `110_000`（标量/标量）：`op1_vector=F`（op1 是标量 s1）、`OP2_SRC_SCALAR2`、`MASK_SRC_ALL_ONES`（无掩码）、`has_vector1=F`。
- `110_100`（向量/向量）：`op1_vector=T`、`OP2_SRC_VECTOR2`，两操作数都是向量，掩码全 1。
- `110_010` 与 `110_101`：掩码来自 `MASK_SRC_SCALAR1` / `MASK_SRC_SCALAR2`，这就是「带掩码」的向量格式。

立即数位段的提取也有专门逻辑。解码器根据格式选择 32 位里的不同位段并做符号扩展：

```systemverilog
unique case (dlut_out.imm_loc)
    IMM_23_15: decoded_instr_nxt.immediate_value = scalar_t'($signed(ifd_instruction[23:15]));
    IMM_23_10: decoded_instr_nxt.immediate_value = scalar_t'($signed(ifd_instruction[23:10]));
    IMM_24_15: decoded_instr_nxt.immediate_value = scalar_t'($signed(ifd_instruction[24:15]));
    ...
```

见 [instruction_decode_stage.sv:361-375](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L361-L375)。不同格式占用不同位段作为立即数：算术立即数用 `inst[23:10]` 等，访存用 `inst[24:10]`，分支偏移用 `inst[24:0]` 或 `inst[24:5]` 并左移两位（`×4`，因为指令 4 字节对齐）。

操作码字段本身（决定具体做哪种 ALU 运算）的提取：

```systemverilog
if (fmt_i)
    alu_op = alu_op_t'({1'b0, ifd_instruction[28:24]});   // 立即数格式: 5位操作码
else if (dlut_out.call)
    alu_op = OP_MOVE;                                      // call 当作 move ra,pc
else
    alu_op = alu_op_t'(ifd_instruction[25:20]);           // 寄存器格式: 6位操作码
```

见 [instruction_decode_stage.sv:336-344](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L336-L344)。注意 R 格式用 6 位操作码（`inst[25:20]`），I 格式用 5 位（`inst[28:24]`，因为腾出更多位给立即数）。操作码到运算的映射就是 `defines.svh` 里的 `alu_op_t` 枚举（[defines.svh:81-124](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L81-L124)），如 `OP_ADD_I = 6'b000101`、`OP_ADD_F = 6'b100000` 等。

一个精巧的编码细节：浮点运算的操作码最高位（`alu_op[5]`）都是 1（例如 `OP_ADD_F = 32 = 6'b100000`）。解码器借此一行判断就能把指令分流到浮点流水线还是整数流水线：

```systemverilog
if (fmt_r || fmt_i) begin
    if (alu_op[5] || alu_op == OP_MULL_I || alu_op == OP_MULH_U
         || alu_op == OP_MULH_I || alu_op == OP_FTOI)
        decoded_instr_nxt.pipeline_sel = PIPE_FLOAT_ARITH;  // 浮点/乘法走浮点单元
    else
        decoded_instr_nxt.pipeline_sel = PIPE_INT_ARITH;
end
```

见 [instruction_decode_stage.sv:382-398](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L382-L398)。因为浮点运算也借用了浮点单元里的乘法器，所以整数乘法（`OP_MULL_I` 等）也被送进浮点流水线。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用映射表为四种格式各「人造」一条指令，标出 op1/op2/mask/store_value 的来源寄存器。
2. **操作步骤**：对照 [instruction_decode_stage.sv:38-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L38-L52) 的映射表，填写下表（指令为示例写法，仅用于练习映射关系）：

| 指令（示例记法） | 格式 | op1 来源 | op2 来源 | mask 来源 | store_value |
| --- | --- | --- | --- | --- | --- |
| `add_i s0,s1,s2` | R 标量/标量 | s1 | s2 | 无（全1） | — |
| `add_i v0,v1,s2` | R 向量/标量 | v1 | s2 | 无 | — |
| `add_i v0,v1,imm` | I 向量 | v1 | imm | s2 | — |
| `store_32 (s1+imm), s2` | M 标量 store | s1 | imm | 无 | s2 |
| `store_block (s1+imm), v2` | M block store | s1 | imm | s2 | v2 |
| `store_gather (s1+imm), v2` | M scatter store | v1 | imm | s2 | v2 |

3. **需要观察的现象**：注意「block store」与「scatter store」的 store_value 都来自 `v2`（向量），但 block 的地址只有一个基地址（`s1+imm`，整块连续），而 scatter 的地址来自每个通道（`v1[lane]+imm`）。
4. **预期结果**：上表与映射表 [instruction_decode_stage.sv:38-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L38-L52) 一一对应。
5. 本实践为源码阅读型，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Nyuzi 要把指令做成 32 位定长，而不是像 x86 那样变长？

**答案**：定长指令让取指与 PC 计算极简——下一条指令恒在 `PC+4`，分支目标也按 4 字节对齐（见立即数左移两位 `×4`）。解码器只需看最高几位就能判断格式，硬件代价低。变长指令（x86）能省代码体积，但解码复杂、流水线难做，不适合 Nyuzi 这种追求简洁可综合的设计。

**练习 2**：R 格式和 I 格式的操作码位段分别是几位？为什么不同？

**答案**：R 格式 6 位（`inst[25:20]`），I 格式 5 位（`inst[28:24]`）。因为 I 格式需要更多位来容纳立即数，所以操作码缩成 5 位；R 格式操作数全来自寄存器（每个 5 位），腾得出 6 位给操作码。见 [instruction_decode_stage.sv:336-344](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L336-L344)。

**练习 3**：解码器怎么仅凭 `alu_op[5]` 就能把大多数浮点指令分流到浮点流水线？

**答案**：因为 `alu_op_t` 枚举里所有浮点运算的操作码最高位都是 1（如 `OP_ADD_F=32=6'b100000`、`OP_CMPEQ_F=48=6'b110000`），而整数运算最高位是 0。这种「浮点位=1」的编码约定让 `alu_op[5]` 一行就能区分两类，是一种巧妙的编码复用。见 [defines.svh:113-122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L113-L122) 与 [instruction_decode_stage.sv:386-392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L386-L392)。

## 5. 综合实践

把本讲三个模块串起来的小任务：**画一张「Nyuzi 向量加法从指令到写回」的完整数据流图**。

具体要求：

1. 选一条具体的向量指令：`add_i v0, v1, s2`（向量 v1 每通道加标量 s2，结果写 v0，无掩码）。
2. 在图上标出它经过的每个概念节点，并标注对应源码出处：
   - **取指**：32 位定长指令被取出。
   - **格式识别**：`fmt_r` 为真（[instruction_decode_stage.sv:229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L229)），查表得「向量/标量」子格式。
   - **操作码解码**：`alu_op = OP_ADD_I`（[defines.svh:86](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L86)），分流到整数流水线（[instruction_decode_stage.sv:392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L392)）。
   - **操作数取回**：16 通道并行读 v1（[operand_fetch_stage.sv:77-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L77-L97)）；s2 广播成 16 份（[operand_fetch_stage.sv:129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/operand_fetch_stage.sv#L129)）。
   - **执行**：16 个加法器同一周期算出 16 个和。
   - **写回**：按掩码（全 1）写入 v0 的 16 个通道。
3. 在图旁用一句话对比：如果换成 `load_gather v0, (v1)`，数据流在哪一步会变成「16 个子周期串行」？引用 [processor.c:1591-1594](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1591-L1594) 的 `pc -= 4` 重发作为依据。

这道综合实践没有标准答案图，重点是你能把「寄存器模型 → 向量 SIMD → 指令格式」三个模块的源码串成一条数据流，并能指出并行与串行的分界点。完成后，你就真正读懂了 Nyuzi ISA 的数据模型层。

## 6. 本讲小结

- Nyuzi 有 **32 个标量寄存器** + **32 个向量寄存器**，寄存器号 5 位；每核默认 4 个硬件线程，寄存器按线程分体（`THREADS_PER_CORE = 4`）。
- `scalar_t` 是 32 位；`vector_t` 是 16 个 `scalar_t` 拼成，共 **512 位（64 字节）**，正好等于一个缓存行。
- 向量 SIMD 的硬件基础是 `operand_fetch_stage` 用 `generate` 实例化的 **16 套并行通道**；标量操作数通过 `{NUM_VECTOR_LANES{...}}` **广播**到所有通道。
- 大多数向量算术指令 **1 周期、16 通道并行**；只有 **scatter/gather** 因每通道地址不同，需 **16 个 subcycle 逐通道串行**，靠 `pc -= 4` 重发实现，状态存于 `CR_SUBCYCLE`。
- 指令 **32 位定长**，分五大格式（R/I/M/C/B）；解码靠最高几位的特征码与 `casez` 查表，把位段映射到 op1/op2/mask/store_value。
- 操作码编码有巧思：浮点指令最高位为 1，使 `alu_op[5]` 一行即可把指令分流到浮点或整数流水线。

## 7. 下一步学习建议

本讲只建立了 ISA 的「数据模型与格式地图」，尚未展开具体指令的语义。建议按以下顺序继续：

1. **u2-l2 算术与比较指令**：精读 `alu_op_t` 枚举的每一类操作（整数算术/逻辑/移位/乘法/CLZ/CTZ、浮点算术与比较），理解整数与浮点两条执行路径。
2. **u2-l3 内存访问与加载存储**：精读 `memory_op_t`，搞清字节/半字/字加载存储、block 向量访存、scatter/gather、同步访存的语义与数据布局。
3. **u2-l4 分支调用与控制寄存器**：精读 `branch_type_t` 与 `control_register_t`，建立对线程号、trap、ASID、页目录、中断、性能计数等控制寄存器的整体认识。

在进入 u2-l2 前，建议你回头再翻一遍 `defines.svh` 里 `alu_op_t`、`memory_op_t`、`branch_type_t`、`control_register_t` 四个枚举，对照本讲的格式表，自己预测一下「这些操作码会出现在哪类格式里」——带着预测去读下一讲，效果最好。
