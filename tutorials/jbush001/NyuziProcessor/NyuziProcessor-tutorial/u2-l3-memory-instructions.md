# 内存访问与加载存储

## 1. 本讲目标

本讲是 Nyuzi 指令集架构（ISA）系列的第三讲，专讲「数据如何在寄存器和内存之间流动」。

学完本讲后，你应该能够：

- 说清楚 Nyuzi 指令中 4 位 `memory_op_t` 字段如何区分字节/半字/字访存、向量块访存、scatter/gather 与同步访存。
- 理解标量访存的位宽与符号扩展规则，以及硬件如何在写回阶段做零扩展/符号扩展。
- 区分「向量块访存（block）」与「scatter/gather（散布/聚集）」两种向量访存在地址布局、对齐要求和执行周期数上的根本差异。
- 理解同步访存 `load_sync`/`store_sync` 的 LL/SC（load-linked / store-conditional）语义，以及它与普通访存、控制寄存器访问的区别。
- 能对照真实源码（硬件解码 + C 模拟器功能模型）解释一条访存指令从位段到内存副作用的完整过程。

## 2. 前置知识

在进入本讲前，你需要先建立以下概念（来自 u2-l1、u2-l2）：

- **寄存器模型**：Nyuzi 有 32 个标量寄存器 `s0–s31` 与 32 个向量寄存器 `v0–v31`。标量 `scalar_t` 是 32 位；向量 `vector_t` 由 16 个标量拼接，共 \(16 \times 32 = 512\) 位（64 字节）。
- **向量 SIMD**：16 个通道并行运算，标量操作数可广播到所有通道。
- **指令格式**：32 位定长指令，按最高几位特征码分为 R/I/M/C/B 五大格式。本讲只关心 **M 格式（memory）**，它的特征是最高两位 `[31:30] == 2'b10`。
- **硬件 vs 模拟器同构**：硬件 `hardware/core/defines.svh` 与模拟器 `tools/emulator/instruction-set.h` 用同一套数值定义 ISA，所以两者可做协同仿真互验（见 u8-l3）。本讲会同时引用两份定义。

本讲还会用到几个尺寸常量，先记住它们的关系：

\[
\text{CACHE\_LINE\_BYTES} = \text{NUM\_VECTOR\_LANES} \times 4 = 16 \times 4 = 64 \text{ 字节}
\]

即一个缓存行恰好容纳一个向量寄存器。这个巧合是 Nyuzi 向量访存设计的基石：一次向量块访存正好读写一整行缓存。

## 3. 本讲源码地图

本讲涉及的关键文件与各自作用：

| 文件 | 角色 |
| --- | --- |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 硬件侧 ISA「字典」：定义 `memory_op_t`、`decoded_instruction_t`、缓存行尺寸、控制寄存器编号等贯穿全项目的类型与常量。 |
| [hardware/core/instruction_decode_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv) | 硬件解码级：把 32 位 M 格式指令拆成 `decoded_instruction_t`，决定操作数、掩码、存储值来自哪些寄存器。 |
| [hardware/core/dcache_data_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv) | 硬件 L1 数据缓存访问级：判定对齐、IO 与缓存路由、按访存类型生成字节写掩码、处理同步访存的挂起。 |
| [hardware/core/writeback_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv) | 硬件写回级：对字节/半字 load 做零扩展或符号扩展，把结果写回寄存器。 |
| [tools/emulator/instruction-set.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) | 模拟器侧 ISA「字典」：`enum memory_op` 与硬件 `memory_op_t` 数值一一对应。 |
| [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | 模拟器功能参考实现：标量/块/scatter-gather/控制寄存器四类访存函数，是理解语义最直观的入口。 |
| [tests/core/isa/load_store.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S) | 各种 load/store 组合的定向功能测试，展示了真实汇编助记符的用法。 |
| [tests/core/isa/atomic.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/atomic.S) | `load_sync`/`store_sync` 的成功与失败用例。 |

> 阅读建议：先看模拟器 `processor.c`（最直白的功能语义），再看硬件 `defines.svh` 的编码定义与 `instruction_decode_stage.sv` 的映射表，最后用 `load_store.S` / `atomic.S` 把助记符和语义对上号。

## 4. 核心概念与源码讲解

所有访存指令都属于 **M 格式**。一条 M 格式指令的位段布局如下（以无掩码标量版为例）：

```
 31  30  29   28 25   24 .......... 10    9 5      4 0
+-------+----+--------+----------------+--------+--------+
| 1 0   | L  |  op    |  有符号偏移 imm | rd/sv  |  rs1   |
+-------+----+--------+----------------+--------+--------+
  格式    方向  memory_op_t        目的/源     基址
         1=load                    寄存器      指针寄存器
         0=store
```

解码级用三句话就定下了这条指令的「身份」：

[hardware/core/instruction_decode_stage.sv:400-405](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L400-L405) 这段把位段映射成解码结构体的关键字段：`[31:30]==2'b10` 表示是访存指令；`[29]` 区分 load/store；`[28:25]` 这 4 位就是下面要详细讲的 `memory_op_t`。

```systemverilog
assign memory_access_type = memory_op_t'(ifd_instruction[28:25]);
assign decoded_instr_nxt.memory_access_type = memory_access_type;
assign decoded_instr_nxt.memory_access = ifd_instruction[31:30] == 2'b10
    && !has_trap;
assign decoded_instr_nxt.load = ifd_instruction[29]
    && fmt_m;
```

而 4 位的 `memory_op_t` 一共有 11 个取值，决定了「这是一次怎样的访存」：

[hardware/core/defines.svh:127-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L127-L139) 这是本讲最核心的一张编码表。本讲的四个最小模块就对应表里的四个分组：

```systemverilog
typedef enum logic[3:0] {
    MEM_B       = 4'b0000,  // 字节 (8 bit)
    MEM_BX      = 4'b0001,  // 字节，符号扩展
    MEM_S       = 4'b0010,  // 半字 (16 bit)
    MEM_SX      = 4'b0011,  // 半字，符号扩展
    MEM_L       = 4'b0100,  // 字 (32 bit)
    MEM_SYNC    = 4'b0101,  // 同步访存 (LL/SC)
    MEM_CONTROL_REG = 4'b0110,  // 控制寄存器访问
    MEM_BLOCK   = 4'b0111,  // 向量块访存
    MEM_BLOCK_M = 4'b1000,  // 向量块访存（带掩码）
    MEM_SCGATH  = 4'b1101,  // scatter/gather
    MEM_SCGATH_M= 4'b1110   // scatter/gather（带掩码）
} memory_op_t;
```

模拟器侧的 [tools/emulator/instruction-set.h:92-105](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L92-L105) 用 C 的 `enum memory_op` 给出了**完全相同的数值**（例如 `MEM_LONG = 4`、`MEM_BLOCK_VECTOR = 7`、`MEM_SCGATH = 13`），这就是「硬件与模拟器同构」的体现。

下面按四个最小模块逐一展开。

---

### 4.1 标量访存

#### 4.1.1 概念说明

标量访存是最朴素的内存访问：一次读或写一个标量寄存器宽度的数据，地址由「基址寄存器 + 有符号立即数偏移」计算。它涵盖 `MEM_B`/`MEM_BX`（字节）、`MEM_S`/`MEM_SX`（半字）、`MEM_L`（字）六种尺寸/扩展组合，以及同样走 M 格式但语义特殊的 `MEM_CONTROL_REG`（控制寄存器访问）。

两个关键点：

1. **位宽与符号扩展**：`MEM_B`/`MEM_S` 是「零扩展」（把读出的 8/16 位当作无符号，高位补 0），`MEM_BX`/`MEM_SX` 是「符号扩展」（按最高位复制到高位）。编译器会根据 C 类型（`uint8_t` vs `int8_t` 等）选择正确的变体。
2. **对齐要求**：字访问必须 4 字节对齐，半字必须 2 字节对齐，否则触发 `TT_UNALIGNED_ACCESS` 异常。

`MEM_CONTROL_REG` 在编码上属于访存家族（4'b0110），但它不访问内存，而是读写控制寄存器（如线程号、中断、TLB 等，见 u2-l4）。把它和普通访存放在一起区分，正是本讲学习目标之一。

#### 4.1.2 核心流程

一条标量 load/store 的处理流程：

```
1. 解码：从 [28:25] 取出 memory_op，确定尺寸/扩展类型；
         基址 = s[rs1]，偏移 = 符号扩展后的 imm，rd = [9:5]。
2. 计算虚拟地址 = 基址 + 偏移。
3. 对齐检查：按 access_size 判定，未对齐 → TT_UNALIGNED_ACCESS。
4. 地址翻译（MMU/TLB，本讲不展开，见 u7-l1）。
5. 路由：
   - 地址落在 0xffff???? → IO 区域（外设寄存器，非缓存）；
   - 否则 → L1 数据缓存（dcache）。
6. load：从缓存/IO 取出数据，按类型零扩展或符号扩展，写回 rd。
   store：按类型生成字节写掩码，写入缓存行对应字节。
```

#### 4.1.3 源码精读

**模拟器侧的功能语义**最直观。先看尺寸与对齐：

[tools/emulator/processor.c:1253-1275](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1253-L1275) 模拟器用 `access_size` 表达三种宽度（1/2/4 字节），随后做取模对齐检查。注意：`MEM_B` 和 `MEM_BX` 共用 `access_size=1`，符号扩展的差别发生在后面读出数据时。

```c
switch (op) {
    case MEM_BYTE:
    case MEM_BYTE_SEXT:   access_size = 1; break;
    case MEM_SHORT:
    case MEM_SHORT_EXT:   access_size = 2; break;
    default:              access_size = 4;
}
if ((virtual_address % access_size) != 0) {
    raise_trap(thread, virtual_address, TT_UNALIGNED_ACCESS, !is_load, true, 0);
    return;
}
```

再看 load 时如何做符号扩展——零扩展与符号扩展的区别就一行代码：

[tools/emulator/processor.c:1303-1317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1303-L1317) `MEM_BYTE` 先转 `uint8_t` 再转 `uint32_t`（零扩展）；`MEM_BYTE_SEXT` 多转一次 `int32_t`（符号扩展）。半字同理。

```c
case MEM_BYTE:
    value = (uint32_t) *UINT8_PTR(..., physical_address);          // 零扩展
    break;
case MEM_BYTE_SEXT:
    value = (uint32_t)(int32_t) *INT8_PTR(..., physical_address);  // 符号扩展
    break;
...
case MEM_SHORT_EXT:
    value = (uint32_t)(int32_t) *INT16_PTR(..., physical_address); // 半字符号扩展
    break;
```

**硬件侧**对应的符号扩展发生在写回级，而不是 dcache。dcache 一次读出整行，写回级再按类型挑字节并扩展：

[hardware/core/writeback_stage.sv:421-425](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L421-L425) 注意 `$signed(...)` 的有无正是零扩展与符号扩展的分水岭，与模拟器的两行代码一一对应。

```systemverilog
MEM_B:  writeback_value_nxt[0] = scalar_t'(byte_aligned);               // 零扩展
MEM_BX: writeback_value_nxt[0] = scalar_t'($signed(byte_aligned));      // 符号扩展
MEM_S:  writeback_value_nxt[0] = scalar_t'(half_aligned);
MEM_SX: writeback_value_nxt[0] = scalar_t'($signed(half_aligned));
```

**硬件对齐检查**则位于 dcache 访问级，按访存类型分别判定哪些地址位必须为 0：

[hardware/core/dcache_data_stage.sv:258-266](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L258-L266) 半字要求 `offset[0]==0`（2 字节对齐）；字与同步/scatter 要求 `offset[1:0]==0`（4 字节对齐）。

```systemverilog
MEM_S, MEM_SX: unaligned_address = dt_request_paddr.offset[0];
MEM_L, MEM_SYNC, MEM_SCGATH, MEM_SCGATH_M: unaligned_address = |dt_request_paddr.offset[1:0];
MEM_BLOCK, MEM_BLOCK_M: unaligned_address = dt_request_paddr.offset != 0;
```

**store 的字节写掩码**也按尺寸生成——这是 store 区别于 load 的地方：store 只改缓存行中对应的字节，其余字节保持不变：

[hardware/core/dcache_data_stage.sv:412-440](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L412-L440) 字节 store 根据地址低 2 位选择 4 个字节中的一个（`4'b1000`/`0100`/`0010`/`0001`）；半字 store 选择高半字或低半字；字 store 则 4 个字节全写。

```systemverilog
MEM_B, MEM_BX: begin
    dd_store_data = {CACHE_LINE_WORDS * 4{dt_store_value[0][7:0]}};
    case (dt_request_paddr.offset[1:0])
        2'd0: byte_store_mask = 4'b1000;
        ...
    endcase
end
MEM_L, MEM_SYNC: begin
    byte_store_mask = 4'b1111;   // 字 store：4 字节全写
    ...
end
```

**关于控制寄存器访问**：`MEM_CONTROL_REG` 虽然在 M 格式里，但不查 TLB、不命中缓存、要求 supervisor 特权。模拟器把它单独分派：

[tools/emulator/processor.c:1597-1613](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1597-L1613) 先检查 supervisor 权限，再按方向调用读/写控制寄存器。对应的汇编助记符是 `getcr`/`setcr`，详见 u2-l4。

#### 4.1.4 代码实践

**实践目标**：通过阅读定向测试，确认标量 load 的位宽与符号扩展行为。

**操作步骤**：

1. 打开 [tests/core/isa/load_store.S:38-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S#L38-L71)。
2. 注意数据定义 `testvar1: .long 0x1234abcd`（小端序在内存中字节为 `cd ab 34 12`）。
3. 对照每条 `load_u8`/`load_s8`/`load_u16`/`load_s16`/`load_32` 后面的 `assert_reg` 期望值：
   - `load_u8 s2, (s1)` → 期望 `0xcd`（零扩展）。
   - `load_s8 s6, (s1)` → 期望 `0xffffffcd`（符号扩展，最高位 1）。
   - `load_s8 s8, 2(s1)` → 期望 `0x34`（`0x34` 最高位 0，符号扩展后不变）。
   - `load_32 s14, (s1)` → 期望 `0x1234abcd`（整个字）。

**需要观察的现象 / 预期结果**：`0xcd`（二进制 `1100 1101`，最高位为 1）在 `load_s8` 下被符号扩展成 `0xffffffcd`，而在 `load_u8` 下是 `0x000000cd`；`0x34` 最高位为 0，两种扩展结果相同。这正是 `MEM_BX` 与 `MEM_B` 的唯一差别。

**运行验证（可选）**：若已按 u1-l2 搭好环境，可在仓库根目录执行 `make` 后单独跑该测试：

```bash
# 在构建目录中，针对 emulator 目标运行 load_store 定向测试
python3 tests/core/isa/runtest.py    # 待本地验证确切脚本入口
```

> 若无法运行，本任务作为「源码阅读型实践」同样成立：通过 `assert_reg` 的期望值即可推断符号扩展规则。

#### 4.1.5 小练习与答案

**练习 1**：内存地址 `0x1000` 处按小端序存放字节 `80 7f 00 ff`。执行 `load_s16`（半字符号扩展）从 `0x1000` 读取，结果是多少？执行 `load_u16` 呢？

**答案**：小端序下 `0x1000` 处的半字是 `0x7f80`。`load_s16`：`0x7f80` 最高位（第 15 位）为 0，符号扩展后仍为 `0x00007f80`；`load_u16` 零扩展同样是 `0x00007f80`。两者相同，因为最高位是 0。若从 `0x1002` 读半字 `0xff00`，则 `load_s16` 得 `0xffffff00`，`load_u16` 得 `0x0000ff00`。

**练习 2**：为什么 dcache 对齐检查里，`MEM_L` 检查的是 `offset[1:0]` 两位都为 0，而 `MEM_S` 只检查 `offset[0]`？

**答案**：字（4 字节）访问要求地址是 4 的倍数，故低 2 位必须为 0；半字（2 字节）访问只要求 2 的倍数，故最低位为 0 即可（`offset[1]` 可为 0 或 1）。

---

### 4.2 向量块访存（block）

#### 4.2.1 概念说明

向量块访存（`MEM_BLOCK`/`MEM_BLOCK_M`）一次把**整个向量寄存器**（16 个字 = 64 字节）与内存中一段**连续**的 64 字节整体搬运。它的汇编助记符是 `load_v`/`store_v`（无掩码）和 `load_v_mask`/`store_v_mask`（带掩码）。

它解决的问题是：当数据本就以 64 字节为单位连续存放时（例如像素块、矩阵列、一个向量数组元素），用一条指令搬完，而不是循环 16 次。这正是 GPGPU「数据并行」在访存端的体现。

关键约束：

- **64 字节对齐**：基址 + 偏移必须是 64 的倍数（恰好对齐到一个缓存行）。
- **连续布局**：lane 0 对应最低地址的字，lane 15 对应最高地址的字，地址步长为 4。
- 掩码版本用 `MASK_SRC_SCALAR2`：由一个标量寄存器的低 16 位控制哪些 lane 参与读写。

#### 4.2.2 核心流程

```
1. 解码：op = MEM_BLOCK 或 MEM_BLOCK_M。
   - 无掩码：mask = 0xffff，偏移取自 [24:10]。
   - 带掩码：mask = s[maskreg]（[14:10]），偏移取自 [24:15]（位段让给 maskreg）。
2. 虚拟地址 = s[ptrreg] + 偏移。
3. 对齐检查：地址低 6 位必须全 0（64 字节对齐）。
4. load：从地址处连续读 16 个字 → 一次性填满向量寄存器（被掩码关掉的 lane 不更新）。
   store：把向量寄存器的 16 个字连续写入（被掩码关掉的 lane 保持内存原值）。
```

注意位段的取舍：带掩码版本需要额外编码 mask 寄存器号，因此偏移立即数从 15 位缩短到 10 位。

#### 4.2.3 源码精读

**解码级**把 M 格式的块访存拆出来。先看解码级的「寄存器端口 → 操作数」总映射表：

[hardware/core/instruction_decode_stage.sv:38-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L38-L52) 这是理解所有 M 格式变体的钥匙。注意 block 行：`op1=s1`（基址指针）、`mask=s2`、`store_value=v2`（要存的向量）。

```
//       format           op1     op2    mask    store value
// | M - block         |   s1  |  imm  |  s2   |  v2   |
// | M - scatter/gather|   v1  |  imm  |  s2   |  v2   |
```

对应的解码表条目（store 与 load 各一组）：

[hardware/core/instruction_decode_stage.sv:187-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L187-L188) `MEM_BLOCK` store：`has_vector2=1, store_value_vector=1` 表示存储值来自向量寄存器 v2；`SCLR2_NONE` 表示无掩码寄存器，偏移用满 15 位。下一行 `MEM_BLOCK_M` store 则把 `SCLR2_14_10` 和 `MASK_SRC_SCALAR2` 打开，引入掩码寄存器。

```systemverilog
7'b10_0_0111: dlut_out = {..., IMM_24_10, SCLR1_4_0, SCLR2_NONE,  F,T,T,F, OP2_SRC_IMMEDIATE, MASK_SRC_ALL_ONES,  T, F}; // MEM_BLOCK store
7'b10_0_1000: dlut_out = {..., IMM_24_15, SCLR1_4_0, SCLR2_14_10, F,T,T,F, OP2_SRC_IMMEDIATE, MASK_SRC_SCALAR2,   T, F}; // MEM_BLOCK_M store
```

**模拟器**实现了块访存的完整语义，最清楚不过：

[tools/emulator/processor.c:1455-1484](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1455-L1484) 对齐检查用 `NUM_VECTOR_LANES * 4 - 1 = 63` 作掩码（即 64 字节对齐）；load 时连续读 16 个字填进 `load_value` 数组，再用 `set_vector_reg` 配合掩码写回。

```c
virtual_address = thread->scalar_reg[ptrreg] + offset;
if ((virtual_address & (NUM_VECTOR_LANES * 4 - 1)) != 0) {
    raise_trap(..., TT_UNALIGNED_ACCESS, ...);
    return;
}
...
block_ptr = UINT32_PTR(thread->core->proc->memory, physical_address);
if (is_load) {
    uint32_t load_value[NUM_VECTOR_LANES];
    for (lane = 0; lane < NUM_VECTOR_LANES; lane++)
        load_value[lane] = block_ptr[lane];        // 连续读 16 个字
    set_vector_reg(thread, destsrcreg, mask, load_value);
}
```

**硬件**侧，块访存的「16 个字」恰好等于一行缓存，所以 store 的写掩码直接把整个 `word_store_mask` 设成 lane 掩码：

[hardware/core/dcache_data_stage.sv:375-393](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L375-L393) `MEM_BLOCK` 把 `word_store_mask` 设为 `dt_mask_value`（16 位 lane 掩码），意味着每个 lane 控制缓存行中一个字的写使能。这正是一条指令改写整行的硬件基础。

```systemverilog
MEM_BLOCK, MEM_BLOCK_M:    // Block vector access
    word_store_mask = dt_mask_value;
```

#### 4.2.4 代码实践

**实践目标**：用汇编对比「标量循环搬 16 个字」与「一条 `load_v`/`store_v` 搬 16 个字」的指令数差异。

**操作步骤**（示例代码，基于真实助记符，参照 [tests/core/isa/load_store.S:108-110](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S#L108-L110) 的用法）：

```asm
# 假设 s10 = 源地址（64 字节对齐），s11 = 目的地址（64 字节对齐）

# —— 方式 A：标量循环，搬 16 个字 ——
    move  s0, 16            # 循环计数
1:  load_32 s1, (s10)
    store_32 s1, (s11)
    add_i s10, s10, 4
    add_i s11, s11, 4
    sub_i s0, s0, 1
    bnz   s0, 1b
    # 共约 16 × 6 = 96 条指令

# —— 方式 B：向量块访存，两条指令搬完 ——
    load_v  v1, (s10)       # 一条指令读 16 个字进 v1
    store_v v1, (s11)       # 一条指令写 16 个字
    # 共 2 条指令
```

**需要观察的现象 / 预期结果**：方式 A 每个字需要约 6 条指令（load、store、两个指针自增、计数自减、分支），16 个字约 96 条；方式 B 只要 2 条。两者搬运的数据量相同（64 字节），但指令数相差近 50 倍——这正是向量 SIMD 在数据搬运上的收益。

**运行验证（可选）**：用模拟器的 `-v` 跟踪模式运行后，统计两种实现的指令条数（`-v` 每行对应一条指令的副作用，详见 u8-l1）。

#### 4.2.5 小练习与答案

**练习 1**：为什么块访存要求 64 字节对齐，而字访存只要求 4 字节对齐？

**答案**：块访存一次读写一整个向量（64 字节），且这 64 字节要落在一行缓存里以便用 `word_store_mask` 一次性写使能。若不 64 字节对齐，它会跨越两行缓存，破坏「一条指令 = 一行」的简洁模型；同时 `dt_request_paddr.offset != 0` 即判为未对齐。字访存只动 4 字节，4 字节对齐即可。

**练习 2**：`store_v_mask v1, s0, (s10)` 中，`s0` 的作用是什么？若 `s0 = 0x000f`（低 4 位为 1），会发生什么？

**答案**：`s0` 的低 16 位是 lane 掩码。`0x000f` 表示只有 lane 0–3 参与写，内存中对应这 4 个字的位置被 `v1` 的 lane 0–3 覆盖，其余 12 个字保持原值。模拟器 [processor.c:1501-1505](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1501-L1505) 用 `if (mask & (1 << lane))` 逐 lane 决定是否写入。

---

### 4.3 scatter/gather（散布/聚集）

#### 4.3.1 概念说明

scatter/gather（`MEM_SCGATH`/`MEM_SCGATH_M`）是另一种向量访存，但它解决的是**非连续**地址的问题：每个 lane 要访问的地址各不相同，这些地址由一个**向量指针寄存器**逐 lane 给出。

- **gather（聚集，load）**：从 16 个不同地址各读一个字，拼成一个向量。
- **scatter（散布，store）**：把一个向量的 16 个字分别写到 16 个不同地址。

汇编助记符：`load_gath`/`load_gath_mask` 和 `store_scat`/`store_scat_mask`。典型用途：按索引数组重排数据、稀疏矩阵运算、顶点索引寻址。

与块访存的关键差异：

| 维度 | block（块） | scatter/gather |
| --- | --- | --- |
| 地址来源 | 标量基址 + 偏移，**连续** | **向量**指针，每 lane 一个地址 |
| 对齐 | 64 字节 | 每个被访地址 4 字节（被掩码的 lane 不检查） |
| 执行 | 1 周期读/写整行 | **16 个 subcycle 逐 lane 串行** |
| 缓存友好 | 极好（整行命中） | 差（16 个地址可能分散在多行） |

#### 4.3.2 核心流程

scatter/gather 最特别的地方是它需要 **16 个子周期（subcycle）**逐通道完成，因为 16 个地址互不相同，无法像 block 那样一次读整行。机制如下：

```
设当前 subcycle = k（0..15）：
1. 取第 k 个 lane 的指针：addr_k = v[ptrreg][k] + 偏移。
2. 若该 lane 被掩码关闭 → 跳过（不访问、不触发对齐/缺页异常）。
3. 否则对该地址做 4 字节对齐检查与翻译。
4. load：从 addr_k 读一个字，只写入结果向量的第 k 个 lane。
   store：把 v[srcreg][k] 写到 addr_k。
5. subcycle++。若未到 16，则 PC 回退一条指令（pc -= 4）重发同一条指令，
   进入下一个 subcycle；若到 16，则结束，PC 正常前进。
```

PC 回退的实现细节见 u2-l1：subcycle 状态保存在控制寄存器 `CR_SUBCYCLE`（编号 13）里。

#### 4.3.3 源码精读

**解码级**为 scatter/gather 设置 `last_subcycle = 15`，告诉流水线这条指令要重复 16 次：

[hardware/core/instruction_decode_stage.sv:410-421](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L410-L421) 只有 scatter/gather 把 `last_subcycle` 设成 `NUM_VECTOR_LANES - 1 = 15`，其余访存都是 0（即单周期完成）。

```systemverilog
if (ifd_instruction[31:30] == 2'b10
    && (memory_access_type == MEM_SCGATH
    || memory_access_type == MEM_SCGATH_M))
begin
    decoded_instr_nxt.last_subcycle = subcycle_t'(NUM_VECTOR_LANES - 1);  // 15
end
else
    decoded_instr_nxt.last_subcycle = 0;
```

**模拟器**把「逐 lane + PC 回退」写得非常清楚：

[tools/emulator/processor.c:1543-1544](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1543-L1544) 注意指针来自**向量**寄存器 `vector_reg[ptrreg][lane]`，而不是标量——这是 scatter/gather 与 block 的本质区别。当前 lane 由 `thread->subcycle` 决定。

```c
lane = thread->subcycle;
virtual_address = thread->vector_reg[ptrreg][lane] + offset;
if ((mask & (1 << lane)) && (virtual_address & 3) != 0) {
    raise_trap(..., TT_UNALIGNED_ACCESS, ...);   // 仅检查未掩码 lane
    return;
}
```

[tools/emulator/processor.c:1563-1570](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1563-L1570) load 时只读一个字，且只写回当前 lane（掩码也只含当前 lane 的位），其余 lane 在本 subcycle 保持不变。

```c
if (is_load) {
    uint32_t load_value[NUM_VECTOR_LANES];
    memset(load_value, 0, NUM_VECTOR_LANES * sizeof(uint32_t));
    if (mask & (1 << lane))
        load_value[lane] = *UINT32_PTR(..., physical_address);
    set_vector_reg(thread, destsrcreg, mask & (1 << lane), load_value);
}
```

[tools/emulator/processor.c:1591-1594](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1591-L1594) subcycle 自增；未到 16 就把 `pc -= 4`，让下一条「指令」其实是同一条指令的下一个 subcycle。

```c
if (++thread->subcycle == NUM_VECTOR_LANES)
    thread->subcycle = 0;     // 全部 lane 完成，正常前进
else
    thread->pc -= 4;          // 重发本指令，处理下一个 lane
```

**硬件**侧，scatter/gather 的 store 在每个 subcycle 只写一个字，因此写掩码是「单 lane」的：

[hardware/core/dcache_data_stage.sv:382-388](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L382-L388) 把当前 subcycle 转成 one-hot 的 `cache_lane_mask`，只命中缓存行里对应的那一个字。

```systemverilog
MEM_SCGATH, MEM_SCGATH_M:
begin
    if ((dt_mask_value & subcycle_mask) != 0)
        word_store_mask = cache_lane_mask;   // 只写当前 lane 对应的字
    else
        word_store_mask = 0;                 // 该 lane 被掩码，不写
end
```

**被掩码 lane 不触发异常**是一个重要的设计点：

[hardware/core/dcache_data_stage.sv:185-187](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L185-L187) 注释明说：被掩码的 lane 必须忽略其指针，否则一个无效（未对齐/未映射）的指针会误触发异常。这正是 [tests/core/isa/load_store.S:187-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S#L187-L188) 里最后一个 lane 故意填了未对齐地址 `3` 却不报错的原因。

```systemverilog
assign lane_enabled = !dt_instruction.memory_access
    || dt_instruction.memory_access_type != MEM_SCGATH_M
    || (dt_mask_value & subcycle_mask) != 0;
```

#### 4.3.4 代码实践

**实践目标**：通过 gather 实现「按索引数组重排一个向量」。

**操作步骤**：阅读 [tests/core/isa/load_store.S:141-157](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S#L141-L157)。该测试的做法是：

1. `load_v v4, (shuffle_idx1)`：把 16 个字节偏移索引装进向量 `v4`（例如 `{56, 40, 0, 4, ...}`）。
2. `add_i v4, v4, s1`：把每个索引加上基址 `s1`，得到 16 个完整地址（标量 `s1` 广播到所有 lane）。
3. `load_gath v6, (v4)`：从这 16 个地址各读一个字，聚集成 `v6`。
4. `assert_vector_reg v6, expected_result1`：校验重排结果。

**需要观察的现象 / 预期结果**：`load_gath` 用**向量** `v4` 作指针，每个 lane 走自己的地址。注意第 2 步 `add_i v4, v4, s1` 是「向量 + 标量」运算，标量 `s1` 自动广播到 16 个 lane（见 u2-l2、u5-l1）。最终 `v6` 的每个 lane 是按索引重新排列后的数据。

#### 4.3.5 小练习与答案

**练习 1**：一次 `load_gath` 在模拟器里实际执行了多少条「指令」？为什么？

**答案**：16 条。因为每个 subcycle 只能处理一个 lane 的地址（16 个地址互不相同，无法像 block 那样一次读整行），所以通过 15 次 `pc -= 4` 重发，加上第 16 次正常前进，共 16 次。这也是 scatter/gather 远慢于 block 的原因。

**练习 2**：如果 gather 的某个 lane 指针指向未映射地址，但该 lane 被 mask 关闭，会发生什么？

**答案**：不触发异常。硬件用 `lane_enabled` 信号在掩码关闭时直接忽略该 lane 的指针（[dcache_data_stage.sv:185-187](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L185-L187)），模拟器也只在 `mask & (1<<lane)` 为真时才检查对齐与翻译。这避免了无效指针误触异常。

---

### 4.4 同步访存（LL/SC）

#### 4.4.1 概念说明

同步访存（`MEM_SYNC`）实现的是 **load-linked / store-conditional（LL/SC）** 原语，用于多线程/多核间的无锁同步（自旋锁、原子计数）。汇编助记符：`load_sync`/`store_sync`。它只有 32 位（字）一种宽度。

LL/SC 的语义：

- `load_sync rd, (addr)`：像普通 load 一样读一个字到 `rd`，**同时**记录下「正在监视这个缓存行」。
- `store_sync rd, (addr)`：**条件**存储。只有当「自上次 `load_sync` 以来，该缓存行没有被其他线程/核写过」时，存储才成功：内存被更新，`rd` 被置为 **1**（成功标志）。否则存储失败：内存不变，`rd` 被置为 **0**。

LL/SC 比单一的 `atomic-add` 类指令更灵活：任何读-改-写序列都可以用「`load_sync` → 计算 → `store_sync` → 判断成功否、失败则重试」来实现，且天然支持任意大小的临界区。

#### 4.4.2 核心流程

```
load_sync：
  1. 读 addr 处的字 → rd。
  2. 记录 last_sync_load_addr = addr 所在缓存行号。

store_sync（写回 rd 表示成功/失败）：
  if (addr 所在缓存行 == last_sync_load_addr) 且 该行未被外部写失效：
      内存[addr] = rd 的原值；
      rd = 1;                       // 成功
  else：
      内存不变；
      rd = 0;                       // 失败（需软件重试）
```

关键：任何**其他**对该缓存行的 store 都会使其失效，从而使正在进行的 `store_sync` 失败。这是通过 `invalidate_sync_address` 在每次普通 store 后清除同步监视来实现的。

#### 4.4.3 源码精读

**模拟器**的 LL/SC 实现直观体现了语义。先看 load：

[tools/emulator/processor.c:1319-1322](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1319-L1322) `load_sync` 读出值，并把目标缓存行号记进 `last_sync_load_addr`。

```c
case MEM_SYNC:
    value = *UINT32_PTR(..., physical_address);
    thread->last_sync_load_addr = physical_address / CACHE_LINE_LENGTH;  // 记录监视的行
    break;
```

再看 store 的成功/失败判定：

[tools/emulator/processor.c:1375-1393](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1375-L1393) 若目标仍在监视的同一行，则写入内存并把寄存器置 1；否则不写内存、寄存器置 0。注释还点出一个协同仿真的限制：`store_sync` 有「置寄存器 + 写内存」两个副作用，而 cosim 每条指令只能记录一个，因此这里手动写寄存器、只把内存写记为副作用。

```c
case MEM_SYNC:
    if (physical_address / CACHE_LINE_LENGTH == thread->last_sync_load_addr) {
        thread->scalar_reg[destsrcreg] = 1;                        // 成功标志
        *UINT32_PTR(..., physical_address) = value_to_store;       // 真正写入
        did_write = true;
    } else {
        thread->scalar_reg[destsrcreg] = 0;                        // 失败，内存不变
    }
    break;
```

**失效机制**：每次普通 store（标量、块、scatter）成功后都会调用 `invalidate_sync_address`，把同核所有线程监视中的缓存行清掉，从而让竞争者的 `store_sync` 失败。例如块 store：

[tools/emulator/processor.c:1507](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1507) 块 store 结尾调用 `invalidate_sync_address(thread->core, physical_address)`，使受影响缓存行上的 `store_sync` 失败。

**硬件**侧，LL/SC 需要两次穿过 dcache：第一次（load_sync）向 L2「注册」监视请求，第二次（重新发射的 load/store）取回结果。dcache 用 `dd_load_sync_pending` 位图区分这是第一次还是第二次：

[hardware/core/dcache_data_stage.sv:517-540](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L517-L540) 注释说明：第一次 `load_sync` 即使数据在缓存中也视为 miss，以便向 L2 注册请求；`load_sync_pending` 在两次请求间翻转。

```systemverilog
// Always treat the first synchronized load as a cache miss, even if data is
// present. This is to register request with L2 cache...
cached_load_req && sync_access_req && dt_thread_idx == ...
    dd_load_sync_pending[thread_idx] <= !dd_load_sync_pending[thread_idx];
```

正因为同步访存需要「两次发射」，解码级会**抑制中间的中断**，以免在两次之间被打断而破坏原子性：

[hardware/core/instruction_decode_stage.sv:272-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L272-L281) 当 `dd_load_sync_pending` 或 `sq_store_sync_pending` 置位时，从可投递中断里把这些位屏蔽掉，确保 LL/SC 两次发射之间不会被中断切开。

```systemverilog
assign masked_interrupt_flags = cr_interrupt_pending & cr_interrupt_en
    & ~ior_pending & ~dd_load_sync_pending & ~sq_store_sync_pending;
```

#### 4.4.4 代码实践

**实践目标**：通过阅读定向测试，验证 `store_sync` 的成功与失败两种结果。

**操作步骤**：阅读 [tests/core/isa/atomic.S:31-50](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/atomic.S#L31-L50)。测试分两段：

1. **成功路径**（L34–L40）：`load_sync` 读取后立即 `store_sync`，期间无其他写，故 `assert_reg s2, 1`（成功），随后 `load_32` 确认内存已被改写。
2. **失败路径**（L43–L49）：`load_sync` 后，插入一条 `store_32 s4, 4(s0)`——它写到**同一缓存行**（偏移 +4 仍在同一 64 字节行内），使监视失效；随后的 `store_sync` 因此失败，`assert_reg s2, 0`，且 `load_32` 确认内存**未**被 `store_sync` 改动。

**需要观察的现象 / 预期结果**：成功路径里 `store_sync` 写回寄存器为 1、内存改变；失败路径里写回为 0、内存不变。注意失败路径那条「干扰写」用的是 `store_32` 到偏移 +4（同行不同字），它通过 `invalidate_sync_address` 触发了失败。

> 用 LL/SC 实现自旋锁与原子操作、以及在多核竞争下的回滚行为，属于 u10-l1 的内容；本讲只要求理解单线程下的成功/失败语义。

#### 4.4.5 小练习与答案

**练习 1**：`store_sync` 失败时，目标内存地址的内容会被修改吗？目的寄存器的值是什么？

**答案**：不会修改内存。目的寄存器被置为 0（失败标志）。只有成功时才写入内存并把寄存器置 1（见 [processor.c:1375-1393](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1375-L1393)）。

**练习 2**：为什么 LL/SC 的「监视粒度」是缓存行而不是单个字？这会带来什么影响？

**答案**：因为硬件以缓存行为一致性单位（其他核/线程的写以行为粒度失效本地缓存）。副作用是「伪共享」：同一缓存行里**相邻**字被别的线程写，也会让本线程针对另一个字的 `store_sync` 失败——atomic.S 失败路径正是利用 `store_32` 到偏移 +4（同行）来触发失效的。

**练习 3（思考）**：为什么解码级要在 `dd_load_sync_pending` 期间屏蔽中断？

**答案**：LL/SC 的 load 和 store 之间维护着「监视状态」（硬件在 L2 注册了请求）。若其间插入中断并跳到中断处理程序，处理程序里的任意访存都可能破坏该状态，导致恢复后 `store_sync` 行为不可预测。屏蔽中断保证两次发射原子完成（见 [instruction_decode_stage.sv:272-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L272-L281)）。

---

## 5. 综合实践

设计一个小任务，把本讲四个模块串起来：**用三种方式把一段连续内存「转置重排」后写到目标缓冲，并分析各自的指令数与适用场景。**

背景：源缓冲 `src` 是 64 字节连续数据（16 个字），按下标 `0,1,2,…,15` 排列。目标是按索引数组 `idx[16]`（值为 `{56, 40, 0, 4, …}` 这种字节偏移）从 `src` 中「打乱重排」后，连续写到 `dst`。

**任务步骤**：

1. **用 block 访存把 `src` 整体读进一个向量**（1 条 `load_v`）。这验证你对块访存「连续、64 字节对齐」的理解。
2. **用 gather 按索引重排**：先 `load_v` 读索引数组，`add_i` 加上 `src` 基址广播，再 `load_gath` 聚集（16 个 subcycle）。这验证 scatter/gather「逐 lane、向量指针」的特性。
3. **用 block 访存把重排结果连续写出**（1 条 `store_v`）。
4. **对比**：如果改用标量 `load_32`/`store_32` 循环完成同样的 gather（按 `idx[i]` 逐个读、逐个写），大约需要多少条指令？为什么 gather 版本虽然在 subcycle 上要 16 次，却仍比标量循环紧凑？

**参考实现骨架**（示例汇编，基于真实助记符，可对照 [tests/core/isa/load_store.S:141-174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/load_store.S#L141-L174)）：

```asm
    lea    s0, src               # s0 = 源基址（64B 对齐）
    lea    s1, idx                # s1 = 索引数组基址
    lea    s2, dst                # s2 = 目的基址（64B 对齐）

    load_v v0, (s1)               # v0 = 16 个字节偏移索引
    add_i  v1, v0, s0             # 每个索引 + 源基址（标量广播）→ 16 个源地址
    load_gath v2, (v1)            # gather：按 16 个地址重排读入 v2（16 subcycle）
    store_v v2, (s2)              # block：把重排结果连续写出（1 周期）
```

**预期分析**：

- block load/store 各 1 条指令，gather 16 个 subcycle（对外表现为一条指令重复 16 次）。
- 标量循环版需要显式的索引计算、地址自增、循环分支与计数，每处理一个元素约 5–7 条指令，16 个元素约 80–112 条；而向量化版本把「并行读 16 个地址」压进了一条 `load_gath` 的 16 个 subcycle（硬件自动 PC 回退，无需软件循环开销）。
- 适用场景：连续大块搬运用 block（缓存友好、最快）；非连续/索引寻址用 scatter/gather（灵活但慢）；32 位原子同步用 `load_sync`/`store_sync`。

> 运行验证（可选）：若已搭好环境，可仿照 `tests/core/isa/` 下现有测试的写法，用 `assert_vector_reg` 校验 `dst` 内容，并通过 `make` 运行（具体入口待本地验证）。

## 6. 本讲小结

- Nyuzi 所有访存指令都是 **M 格式**，由 4 位 `memory_op_t`（指令 `[28:25]`）区分类型，`[29]` 区分 load/store，地址 = 基址寄存器 + 符号扩展偏移。
- **标量访存**（`MEM_B/BX/S/SX/L`）按 8/16/32 位读写，零扩展 vs 符号扩展的差别由变体决定，硬件在写回级用 `$signed` 实现；字需 4 字节对齐、半字需 2 字节对齐。`MEM_CONTROL_REG` 同属 M 格式但不访内存，读写控制寄存器且需 supervisor 权限。
- **向量块访存**（`MEM_BLOCK[_M]`）一次搬整个 64 字节向量，要求 64 字节对齐，恰好占一行缓存，是最快的数据搬运方式；带掩码版本用标量寄存器低 16 位选 lane。
- **scatter/gather**（`MEM_SCGATH[_M]`）用向量指针给每 lane 一个地址，处理非连续访问；需 16 个 subcycle 逐 lane 完成（PC 回退重发），被掩码的 lane 不触发异常。
- **同步访存**（`MEM_SYNC`）实现 LL/SC：`load_sync` 记录监视的缓存行，`store_sync` 在该行未被外部写失效时才成功（寄存器置 1 并写入），否则失败（寄存器置 0、内存不变）；两次发射之间硬件屏蔽中断以保证原子性。
- 硬件（`defines.svh`）与模拟器（`instruction-set.h`）用相同数值定义这套编码，是协同仿真互验的基础。

## 7. 下一步学习建议

- **u2-l4 分支调用与控制寄存器**：深入 `control_register_t` 全表与 `getcr`/`setcr`，理解 `MEM_CONTROL_REG` 背后的完整控制寄存器机制（线程号、中断、TLB、性能计数等）。
- **u5-l1 操作数 fetch 与寄存器文件**：看 `operand_fetch_stage` 如何根据 `op1_src`/`op2_src`/`mask_src` 选择操作数，理解标量如何广播到向量 lane（本讲 gather 示例里 `add_i v4, v4, s1` 的来源）。
- **u6 缓存与内存层次**：本讲只到「dcache 命中/未命中」的边界；u6-l1 起，深入 L1D 虚拟索引/物理标签、L1-L2 队列、L2 四级流水与 AXI 总线，看一次 load miss 如何挂起线程并在 L2 响应后唤醒。
- **u7-l1 软件管理 TLB**：本讲多次出现「地址翻译」却未展开，u7-l1 讲清 TLB 表项、ASID 与 TLB miss trap 的完整流程。
- **u10-l1 同步内存操作 LL/SC 与 membar**：把本讲的 `load_sync`/`store_sync` 放到多核竞争场景，讲自旋锁实现、`store_sync` 失败回滚与 `membar` 内存排序。
