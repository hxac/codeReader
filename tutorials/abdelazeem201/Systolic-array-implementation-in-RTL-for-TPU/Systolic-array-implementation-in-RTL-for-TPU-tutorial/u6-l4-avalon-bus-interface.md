# Avalon 总线接口与控制指令（busConn / matrixMultiplier）

## 1. 本讲目标

本讲是 u6 单元（扩展 TPU 架构与系统集成）的收口篇。在前几讲里，我们已经在 `top.v` 内部走通了 `inputMem → sysArr → accumTable → reluArr → outputMem` 这条数据通路。但 `top` 自己不会凭空运行——它需要被 **主机 CPU（SoC 里的 HPS ARM 核）** 驱动。

CPU 不会、也不应该去理解脉动阵列的节拍、歪斜、分块累加这些细节。CPU 只会说一种语言：**「向某个地址写一个数」「从某个地址读一个数」**。本讲解读的 `busConn.v` 就是这两种世界之间的**翻译器**——一个 Avalon-MM 总线 slave 封装，把 CPU 的「读写内存」动作翻译成 TPU 内部的「写矩阵元素 / 下控制命令 / 取结果」。

学完本讲，你应该能够：

1. 看懂 Avalon-MM slave 的标准端口（`slave_address` / `slave_read` / `slave_write` / `slave_readdata` / `slave_writedata` / `slave_byteenable`），理解它为何是「内存映射外设」。
2. 解释 `slave_address[9:8]` 这两位如何把 1024 个地址切成 **CONTROL / INPUT / WEIGHT / OUTPUT** 四段地址空间。
3. 掌握 `slave_writedata[3:0]` 解码出的 **RESET / FILL_FIFO / DRAIN_FIFO / MULTIPLY** 四条控制命令，以及控制字里 `[11:4]`、`[19:12]` 两段基地址字段的含义。
4. 理解读侧如何把 TPU 的三类 `done` 状态位和 `outputMem` 的结果数据回传给 CPU。
5. **重要**：能够对照源码确认 `busConn` 中例化 `top` 时用的端口表与 `top.v` 实际端口表**并不一致**，并解释这种「总线封装」分层带来的工程价值与当前仓库的实现差距。

---

## 2. 前置知识

在进入源码前，先用三段通俗话把背景讲清楚。

### 2.1 什么是内存映射外设（Memory-Mapped I/O）

想象 CPU 的地址空间是一条长长的街道，街道两侧是门牌号（地址）。有的门牌后面是真正的内存（DRAM），有的门牌后面是外设控制器（比如 TPU）。CPU 不需要为外设发明新的指令——它只要**往某个门牌号写数据**，外设内部的硬件就把这次「写」翻译成对应的动作（写一个矩阵元素、触发一次乘法）；CPU **从某个门牌号读数据**，外设就把内部状态或计算结果送回来。

这种「把外设当成一段特殊内存来读写」的方式就叫**内存映射 I/O**。它的最大好处是：CPU 端零专用硬件、零专用指令，用最普通的 `load/store` 指令就能驱动加速器。

### 2.2 什么是 Avalon-MM 总线

**Avalon-MM**（Memory-Mapped）是 Altera/Intel FPGA（现用于 Qsys / Platform Designer）里把「内存映射 I/O」标准化的总线协议。它定义了一组标准信号：

| 信号 | 方向（slave 视角） | 含义 |
|------|------|------|
| `address` | 输入 | 要访问的字地址 |
| `read` | 输入 | 读请求（1 拍脉冲） |
| `write` | 输入 | 写请求（1 拍脉冲） |
| `writedata` | 输入 | 写入的数据 |
| `readdata` | 输出 | 读出的数据 |
| `byteenable` | 输入 | 字节使能，标记本次写哪些字节有效 |

挂在这条总线上的从设备（slave，本讲里的 `matrixMultiplier`）只要按这套信号规矩响应，就能被 Qsys 自动生成的互连（interconnect）接到 HPS（ARM 核）上，**无需手写任何总线仲裁或握手逻辑**。本讲遇到的 slave 是「组合式响应」——读请求当拍就给出 `readdata`，不插等待周期。

> 术语速查：**slave** = 总线从设备（被动响应读写的一方）；**master** = 总线主设备（发起读写的一方，这里是 CPU）；**HPS** = FPGA 里硬化的 ARM CPU 子系统（见 u5-l4）。

### 2.3 本讲在整个项目里的位置

回顾 u6-l1 的数据通路，`top.v` 内部已经有 `master_control` 在做调度。那么 `busConn`（模块名 `matrixMultiplier`）是夹在 **CPU 与 `top` 之间**的一层：

```
 HPS (ARM CPU) ──Avalon-MM──> [ busConn / matrixMultiplier ] ──内部信号──> top (TPU 核)
   发 load/store                 地址解码 + 命令解码              master_control 调度
```

`busConn` 的职责有三：

1. **串并转换（写侧）**：CPU 一次只写 64 bit，TPU 内部却是 16 路并行存储体；`busConn` 把一次总线写「广播」到 16 路。
2. **命令解码**：CPU 往 CONTROL 地址写一个控制字，`busConn` 把它翻成 `reset` / `fill_fifo` / `multiply` 等内部脉冲。
3. **状态/数据回传（读侧）**：把 TPU 的 `done` 状态和 `outputMem` 结果打包成 64 bit 总线读数据送回 CPU。

> ⚠️ 一个必须先说清的事实：仓库里 `busConn.v` **并未被** `tpu.v` / `tpu_system.v` / `top.v` 任何文件例化（见第 5 节综合实践的核对）。它是一份**独立的参考封装**，且其内部例化 `top` 时使用的端口表与 `top.v` 实际端口表存在明显出入。本讲会忠实呈现这一差距并给出证据，而不是假装它们能直接对接。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
|------|------|------|
| [rtl/RTL_modified/busConn.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v) | **本讲主角**。Avalon-MM slave 封装 `matrixMultiplier`，做地址空间划分、命令解码、读写转换，并例化 `top`。 | 全篇精读 |
| [rtl/RTL_modified/top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v) | TPU 计算核（u6-l1 已讲）。本讲只看它的**端口表**，用来与 `busConn` 的例化端口做对照。 | 第 5 节核对端口差异 |
| [rtl/RTL_modified/software/instr_set.h](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h) | 主机侧 C 语言 API 声明（`tpu_init` / `tpu_rd_input` / `tpu_fill_fifo` / `tpu_mat_mult` 等）。 | 用来理解「总线封装」如何被软件消费 |

> 提醒：`busConn.v` 例化的 `master_control`、`memArr`、`sysArr` 等子模块源码**不在仓库内**（u6-l1 已指出）。因此 `busConn` 内部连接的若干信号行为只能靠端口与注释推断，涉及不确定处我会标注「待本地验证」。

---

## 4. 核心概念与源码讲解

### 4.1 Avalon-MM slave 端口与四类地址空间划分

#### 4.1.1 概念说明

`matrixMultiplier` 把 10 位地址 `slave_address` 切成两段：

- **高位 `[9:8]`**：2 位「地址空间选择符」，决定本次访问打交道的对象是控制寄存器、输入存储、权重存储还是输出存储。
- **低位 `[7:0]`**：8 位「段内偏移」，即在该空间内的字地址（0~255）。

四个空间用宏名定义，值就是这两位选择符本身。这样 1024 个地址被均分成 4 段，每段 256 个字。CPU 只要在不同的高位上读写，就在操作不同的 TPU 资源，彼此互不干扰——这就是「地址空间划分」的直觉。

#### 4.1.2 核心流程

地址解码的本质是一条非常简单的规则：

```
slave_address[9:8] == 00  →  CONTROL   (控制/状态)
slave_address[9:8] == 01  →  INPUT     (输入矩阵存储)
slave_address[9:8] == 10  →  WEIGHT    (权重矩阵存储)
slave_address[9:8] == 11  →  OUTPUT    (输出结果存储)
```

四个空间的大小可以这样刻画。设地址位宽 \(W = 10\)，空间选择位宽 \(k = 2\)，则每个空间的字数为：

\[
\text{每空间字数} = 2^{W-k} = 2^{10-2} = 2^{8} = 256
\]

总地址数 \(2^{10} = 1024\)，正好 4 等分。

#### 4.1.3 源码精读

四类地址偏移宏（以及四条控制命令宏）定义在文件开头：

地址空间宏与控制命令宏——[rtl/RTL_modified/busConn.v:1-12](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L1-L12)：

```verilog
`define CONTROL_OFFSET 2'b00
`define INPUT_OFFSET   2'b01
`define WEIGHT_OFFSET  2'b10
`define OUTPUT_OFFSET  2'b11

`define RESET     4'b1111
`define FILL_FIFO 4'b0001
`define DRAIN_FIFO 4'b0010
`define MULTIPLY  4'b0011
```

上面 4 个是「地址空间选择符」，下面 4 个是「控制命令操作码」，别混在一起——前者解码自 `slave_address[9:8]`，后者解码自 `slave_writedata[3:0]`（见 4.3）。

模块端口声明——[rtl/RTL_modified/busConn.v:14-39](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L14-L39)：这里声明了标准 Avalon-MM slave 的全部端口。注意三个参数：`DATA_WIDTH = 64`（总线一次传 64 bit = 8 字节）、`WIDTH_HEIGHT = 16`（阵列边长 16）、`TPU_DATA_WIDTH = WIDTH_HEIGHT*8 = 128`（TPU 内部一条存储总线宽 128 bit = 16 字节）。一个值得记的细节：`slave_byteenable` 虽然在 [L37](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L37) 声明了，但**全文件没有引用它**——意味着写操作总是整字写入，不支持字节级写入。文件头注释也特别提醒要用 **完整版（非 lightweight）Avalon**，正是因为 lightweight 版没有 `byteenable`。

写/读使能如何用高位解码——[rtl/RTL_modified/busConn.v:71-77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L71-L77)：

```verilog
assign inputMem_wr_en  = {16{slave_write & (slave_address[9:8] == `INPUT_OFFSET)}};
assign weightMem_wr_en = {16{slave_write & (slave_address[9:8] == `WEIGHT_OFFSET)}};
assign outputMem_rd_en = {16{slave_read  & (slave_address[9:8] == `OUTPUT_OFFSET)}};
```

这段是地址空间划分的最直接体现：

- 当且仅当 `slave_write=1` 且高位 `== INPUT_OFFSET` 时，`inputMem_wr_en` 为全 1（16 路同时使能）。
- `weightMem_wr_en` 同理，只是换成 `WEIGHT_OFFSET`。
- `outputMem_rd_en` 用的是 `slave_read`（注意是**读**），对应 `OUTPUT_OFFSET`——输出空间是「读」出来的。

`{16{...}}` 是把 1 位结果复制成 16 位，因为 TPU 内部存储是 16 路并行（每路对应脉动阵列的一列）。

#### 4.1.4 代码实践

**实践目标**：亲手做几次地址解码，验证「高位选空间、低位选偏移」。

**操作步骤**（纯源码阅读型，无需运行）：

对照 [busConn.v:71-77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L71-L77)，对下列 4 个 `slave_address` 取值，分别写出 `[9:8]`（空间）、`[7:0]`（偏移）、以及哪几个 `*_en` 会被拉高：

| `slave_address`（10'h） | 二进制 | `[9:8]` 空间 | `[7:0]` 偏移 | 拉高的使能 |
|---|---|---|---|---|
| `10'h001` | `00_0000_0001` | CONTROL | `0x01` | （无 en，落入控制命令解码，见 4.3） |
| `10'h100` | `01_0000_0000` | INPUT | `0x00` | `inputMem_wr_en`（若 `slave_write=1`） |
| `10'h2FF` | `10_1111_1111` | WEIGHT | `0xFF` | `weightMem_wr_en`（若 `slave_write=1`） |
| `10'h305` | `11_0000_0101` | OUTPUT | `0x05` | `outputMem_rd_en`（若 `slave_read=1`） |

**需要观察的现象 / 预期结果**：每段空间的偏移范围都是 `0x00..0xFF`（256 个字）；改变 `[9:8]` 会让同一套 `slave_read`/`slave_write` 信号去驱动完全不同的 TPU 资源。这正是「地址空间划分」把一个扁平地址总线复用成四条逻辑通道的手法。

> 说明：本表为根据源码逻辑手算的解码结果，未上仿真器；如需验证，可在任意 Avalon testbench 里施加这些地址并观察 `*_en` 波形（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 INPUT 空间支持 512 个字而不是 256 个，需要改什么？
**答案**：需要把空间选择位从 `[9:8]` 调整为更高位（例如 `[10:9]`，地址总线扩到 11 位），让偏移位段变成 9 位（512 = \(2^9\)）。但这也意味着四类空间的基地址都要整体重新排布，且 `busConn` 里所有 `slave_address[9:8]` 的比较都要同步改成新位段。

**练习 2**：`slave_byteenable` 声明了却没用，会有什么实际影响？
**答案**：CPU 无法只写一个字里的某几个字节——每次写都覆盖整个 64 bit 字。对矩阵元素这种「整字写」的场景影响不大；但若主机想就地修改某个元素的一个字节，会误伤同字的其他字节。

---

### 4.2 写侧内存数据通路（INPUT / WEIGHT 空间）

#### 4.2.1 概念说明

CPU 通过总线写矩阵元素时，面临一个「宽度不匹配」问题：总线一次只送 64 bit（8 字节），而 TPU 内部一条存储总线宽 128 bit（`TPU_DATA_WIDTH`，16 字节，对应 16 列每列 1 字节）。`busConn` 的写侧数据通路负责把这一次总线写**扇出**成内部存储体能吃的格式：同一套地址广播给 16 路、同一份数据复制铺满内部宽总线。

#### 4.2.2 核心流程

写侧的三组信号都由 `slave_address` 与 `slave_writedata` 直接组合派生（无寄存）：

```
地址： inputMem_wr_addr  = {16{slave_address[7:0]}}   // 8bit 偏移复制 16 份 → 128bit
数据： inputMem_wr_data  = {2 {slave_writedata}}       // 64bit 数据复制 2 份 → 128bit
使能： inputMem_wr_en    = {16{slave_write & 高位==INPUT}}  // 见 4.1
```

权重侧（`weightMem_*`）与输入侧**完全对称**，只是使能条件换成 `WEIGHT_OFFSET`。

#### 4.2.3 源码精读

写/读地址的广播——[rtl/RTL_modified/busConn.v:85-91](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L85-L91)：

```verilog
assign inputMem_wr_addr   = {16{slave_address[7:0]}};
assign weightMem_wr_addr  = {16{slave_address[7:0]}};
assign outputMem_rd_addr  = {16{slave_address[7:0]}};
```

`slave_address[7:0]` 是段内偏移（8 bit），`{16{...}}` 复制成 128 bit——意思是「16 路存储体都用同一个偏移地址」。为什么 16 路共用地址？因为一次写要把 16 列的同一行位置一次性铺满，地址相同、数据不同（数据来自下面的复制）。

写数据的复制——[rtl/RTL_modified/busConn.v:99-105](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L99-L105)：

```verilog
assign inputMem_wr_data   = {2{slave_writedata}};
assign weightMem_wr_data  = {2{slave_writedata}};
```

`slave_writedata` 是 64 bit（`DATA_WIDTH`），`{2{...}}` 复制成 128 bit（`TPU_DATA_WIDTH`）。即把 8 字节的总线数据复制两份铺满 16 字节的内部总线。

> 关于这种「复制」是否合理：由于 `memArr` 存储体源码不在仓库内，无法确认其内部是否只取低半/高半字节。一种可能是 `busConn` 反映的是早期「8 字节有效、另一半预留」的设计；另一种可能是确实存在重复写入。**这一点的真实行为待本地验证**，读者可把它当作「串并转换」的一个待考证细节，而不必当作已验证结论。

#### 4.2.4 代码实践

**实践目标**：理解「广播地址 + 复制数据」如何让一次 64 bit 总线写覆盖 128 bit 内部存储。

**操作步骤**（源码阅读 + 推演）：

1. 读 [busConn.v:85-105](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L85-L105)，确认地址、数据两段都是纯组合 `assign`。
2. 假设主机执行一次「写 INPUT 空间偏移 `0x03`，数据 `64'hAABBCCDD_11223344`」。请推算：
   - `inputMem_wr_addr` 的 128 bit 值 = `{16{0x03}}`（每 8 bit 都是 `0x03`）。
   - `inputMem_wr_data` 的 128 bit 值 = `{2{64'hAABBCCDD_11223344}}` = `0xAABBCCDD_11223344_AABBCCDD_11223344`。
   - 16 路存储体都在地址 `0x03` 处收到这次写。

**需要观察的现象 / 预期结果**：一次总线写 → 16 个 bank 同址并行写入。这正是把「CPU 的窄、串行」转成「TPU 的宽、并行」的硬件桥梁。结合 4.1 的使能条件，只有当 `slave_address[9:8]==INPUT_OFFSET` 且 `slave_write==1` 时这次写才真正生效。

#### 4.2.5 小练习与答案

**练习 1**：`TPU_DATA_WIDTH` 是怎么由参数推出的？若 `WIDTH_HEIGHT` 改成 8，它会变成多少？
**答案**：`TPU_DATA_WIDTH = WIDTH_HEIGHT * 8`（见 [L29](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L29)）。`WIDTH_HEIGHT=8` 时为 64 bit——恰好等于 `DATA_WIDTH`，此时 `{2{slave_writedata}}` 的复制因子在位宽上就不再自洽（会变宽），说明这套写侧复制是**针对 16×16 阵列写死的**，并非真正的全参数化。

**练习 2**：为什么地址用 `{16{...}}` 复制 16 份、而数据用 `{2{...}}` 只复制 2 份？
**答案**：因为 16 路存储体共用同一地址（每路只存 1 字节，地址相同），所以地址复制 16 份；而总线 64 bit 需要铺满内部 128 bit，只需复制 2 份。两者复制的「份数」由各自的目标位宽决定，与 16 列没有直接关系。

---

### 4.3 控制命令解码（CONTROL 空间）

#### 4.3.1 概念说明

光把矩阵元素写进 INPUT/WEIGHT 存储还不够，TPU 不会自己动起来。主机还需要下「命令」：复位、装填 FIFO、排空 FIFO、启动一次乘法。这些命令通过**往 CONTROL 地址空间写一个控制字**来下发。`busConn` 用控制字的低 4 位 `slave_writedata[3:0]` 当操作码，再用稍高的位段携带命令参数（基地址）。

#### 4.3.2 核心流程

命令解码在一个时钟沿敏感的 `always @(posedge clk)` 块里完成（所以命令是**寄存**的，不是组合）。仅当 `slave_write==1` 且高位 `== CONTROL_OFFSET` 时才动作。控制字的位段定义如下：

```
控制字 slave_writedata（64 bit，只用到低 20 bit）:
  [3:0]    操作码 opcode     → RESET(1111)/FILL_FIFO(0001)/DRAIN_FIFO(0010)/MULTIPLY(0011)
  [11:4]   基地址 A（8 bit） → FILL_FIFO 时为权重读基地址；MULTIPLY 时为输入读基地址
  [19:12]  基地址 B（8 bit） → 仅 MULTIPLY 用，为输出写基地址
```

四条命令各自置位的内部脉冲：

| 命令（opcode） | `reset_tpu` | `fill_fifo` | `drain_fifo` | `multiply` | 附带设置的基地址 |
|---|---|---|---|---|---|
| `RESET`     `4'b1111` | 1 | 0 | 0 | 0 | 三个 `*_rd_addr_base` / `*_wr_addr_base` 全清 0 |
| `FILL_FIFO` `4'b0001` | 0 | 1 | 0 | 0 | `weightMem_rd_addr_base <= [11:4]` |
| `DRAIN_FIFO``4'b0010` | 0 | 0 | 1 | 0 | （不设基地址） |
| `MULTIPLY`  `4'b0011` | 0 | 0 | 0 | 1 | `inputMem_rd_addr_base <= [11:4]`；`outputMem_wr_addr_base <= [19:12]` |

#### 4.3.3 源码精读

命令解码 `always` 块——[rtl/RTL_modified/busConn.v:122-161](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L122-L161)：

```verilog
always @(posedge clk) begin
    if ((slave_write == 1) && (slave_address[9:8] == `CONTROL_OFFSET)) begin
        case (slave_writedata[3:0])
            `RESET:     begin reset_tpu<=1; ...; 三个 base 全 <= 0;            end
            `FILL_FIFO: begin fill_fifo<=1; weightMem_rd_addr_base <= {16{slave_writedata[11:4]}}; end
            `DRAIN_FIFO:begin drain_fifo<=1;                                             end
            `MULTIPLY:  begin multiply<=1;
                                  inputMem_rd_addr_base  <= {16{slave_writedata[11:4]}};
                                  outputMem_wr_addr_base <= {16{slave_writedata[19:12]}}; end
        endcase
    end
end
```

读这段要注意三点：

1. **互斥的单热点（one-hot 风格）**：每条命令把目标脉冲置 1、其余三个显式置 0（例如 RESET 分支里 `fill_fifo<=0; drain_fifo<=0; multiply<=0`）。这意味着任一时刻四个命令脉冲只有一个为 1，避免误触发。
2. **基地址同样广播**：`{16{slave_writedata[11:4]}}` 把 8 bit 基地址复制 16 份成 128 bit，与 4.2 的地址广播同构——TPU 内部 16 路共用一个基地址。
3. **寄存而非组合**：因为是 `posedge clk`，命令脉冲会在写入的**下一拍**生效并保持，直到下一条命令覆盖。这与 4.1/4.2 的组合 `assign` 形成对比——控制类信号需要稳定保持，数据类信号要即时响应。

`RESET` 分支尤其值得注意：它不仅置 `reset_tpu=1`，还把 `inputMem_rd_addr_base` / `weightMem_rd_addr_base` / `outputMem_wr_addr_base` 三个基地址一并清零，确保复位后从一个干净已知的状态开始。

> 与软件 API 的呼应：[software/instr_set.h](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h) 里的 C 函数正好对应这几条命令——`tpu_init`→RESET、`tpu_fill_fifo`→FILL_FIFO、`tpu_mat_mult`→MULTIPLY。但要注意软件 API 还定义了 `tpu_store_outputs`（带 `activate`/`clear`/`accum_row`/`accum_col` 参数），这些在 `busConn` 的 4 条命令里**没有对应**——这是 `busConn` 与 `top.v` 边界不一致的早期信号之一（见第 5 节）。

#### 4.3.4 代码实践

**实践目标**：能手算给定控制字会触发哪条命令、设置什么基地址。

**操作步骤**（源码阅读 + 手算）：

对照 [busConn.v:125-159](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L125-L159)，对下列 4 个 `slave_writedata`（假设同时 `slave_write=1` 且地址在 CONTROL 空间），分别写出命中的命令与基地址设置：

| `slave_writedata`（64'h） | `[3:0]` | 命令 | 基地址设置 |
|---|---|---|---|
| `64'h0000_0000_0000_000F` | `1111` | RESET | 三个 base 全清 0 |
| `64'h0000_0000_0000_00A1` | `0001` | FILL_FIFO | `weightMem_rd_addr_base = {16{0x0A}}` |
| `64'h0000_0000_0000_0002` | `0010` | DRAIN_FIFO | （不设基地址） |
| `64'h0000_0000_0006_0053` | `0011` | MULTIPLY | `inputMem_rd_addr_base = {16{0x05}}`；`outputMem_wr_addr_base = {16{0x06}}` |

**需要观察的现象 / 预期结果**：

- 第 4 行验证了 `[11:4]` 与 `[19:12]` 两个位段的拆分：`0x60053` 中，`[3:0]=0x3`（MULTIPLY）、`[11:4]=0x05`、`[19:12]=0x06`。
- 每条命令写入后**下一拍**内部脉冲才翻转（因为是寄存器）。

**预期结果**：四个控制字分别正确触发四条命令。如需验证波形，可在仿真里施加这些写事务并观察 `reset_tpu/fill_fifo/drain_fifo/multiply` 四个寄存器（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么命令解码用 `always @(posedge clk)` 而写数据通路（4.2）用组合 `assign`？
**答案**：命令脉冲（`multiply` 等）需要**保持多个周期**，让 TPU 内部完成一整次乘法（耗时数十~数百拍），所以必须寄存；而矩阵元素数据是「写一次就进存储」的瞬时动作，组合直通即可，无需保持。

**练习 2**：如果主机写了一个操作码 `4'b0101`（不在四条已定义命令里），会发生什么？
**答案**：`case` 里没有匹配项，也没有 `default`，所以四个命令寄存器与基地址都**保持上一拍的原值不变**。这是一种「忽略未知命令」的隐式行为，但也意味着写错操作码不会报错——属于设计上的静默容错（或隐患）。

---

### 4.4 总线读回逻辑（CONTROL 状态 + OUTPUT 数据）

#### 4.4.1 概念说明

主机下完命令后，需要知道两件事：**(a) TPU 做完了没？**、**(b) 做完了把结果给我**。读侧逻辑就是回答这两个问题的。它是一个纯组合 `always @(*)` 块，依据本次读访问落在哪个地址空间，决定 `slave_readdata` 上回传什么：

- 读 **CONTROL** 空间 → 回传 3 个状态位（三类 `done`）。
- 读 **OUTPUT** 空间 → 回传 `outputMem` 的数据。
- 读 **INPUT / WEIGHT** → 回传 0（这两个空间只写不读）。

#### 4.4.2 核心流程

```
读访问来了 → 看 slave_address[9:8]:
  CONTROL → slave_readdata = { 61'd0, output_done, fifo_to_arr_done, mem_to_fifo_done }
  OUTPUT  → slave_readdata = outputMem_rd_data[63:0]
  其它     → slave_readdata = 0
```

三个状态位的语义（来自 `top` 的反馈，见 [busConn.v:169-171](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L169-L171) 与 [L207-L209](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L207-L209)）：

| 状态位 | 含义 |
|---|---|
| `mem_to_fifo_done` | 权重从 weightMem 装进 weightFifo 完成（对应 FILL_FIFO） |
| `fifo_to_arr_done` | FIFO 里的权重已排入阵列完成（对应 DRAIN_FIFO 的进度） |
| `output_done` | 结果已写入 outputMem，可以读了 |

主机典型用法是「**轮询（poll）**」：循环读 CONTROL，直到某个 `done` 位为 1，再继续下一步。

#### 4.4.3 源码精读

读回 `always` 块——[rtl/RTL_modified/busConn.v:173-181](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L173-L181)：

```verilog
always @(*) begin
    slave_readdata = 64'h0;                  // 默认 0，避免锁存器
    case(slave_address[9:8])
        `CONTROL_OFFSET: slave_readdata = { 61'd0, output_done, fifo_to_arr_done, mem_to_fifo_done};
        `OUTPUT_OFFSET: slave_readdata = outputMem_rd_data[63:0];
        default:         slave_readdata = 64'h0;
    endcase
end
```

三个要点：

1. **块开头先赋默认值 0**（[L174](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L174)），这是组合 `always` 块防推断锁存器的标准写法——否则 `case` 未覆盖的分支会生成电平敏感的锁存器。
2. **CONTROL 读回是状态拼包**：`{61'd0, output_done, fifo_to_arr_done, mem_to_fifo_done}` = 61+1+1+1 = 64 bit，三个 `done` 放在最低 3 位，主机用一个掩码 `& 0x7` 就能读出状态。
3. **OUTPUT 读回只取低 64 bit**：`outputMem_rd_data[63:0]`。注意 `outputMem_rd_data` 在 `busConn` 里声明为 128 bit（[L102](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L102)），但只把低半送回总线。这与 `top.v` 里 `outputMem_rd_data` 实为 256 bit（见 [top.v:70](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L70)）的不一致，是第 5 节要核对的重点之一。

#### 4.4.4 代码实践

**实践目标**：理解主机如何用「轮询 + 读数据」完成一次完整取结果。

**操作步骤**（源码阅读 + 伪代码）：

1. 读 [busConn.v:173-181](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L173-L181) 与 [software/instr_set.h:99-113](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h#L99-L113)（`tpu_wr_outputs` 的注释）。
2. 写出主机取回一个结果元素的伪代码（**示例代码**，非项目原有）：

```c
// 示例代码：主机轮询状态后读回 OUTPUT 空间偏移 i 处的一个字
#define TPU_CONTROL  0x000   // slave_address[9:8]=00
#define TPU_OUTPUT   0x300   // slave_address[9:8]=11，偏移从 0 开始

while ((*(volatile uint64_t*)TPU_CONTROL & 0x4) == 0) {
    // 轮询 bit2 = output_done
}
uint64_t word = *(volatile uint64_t*)(TPU_OUTPUT + i * 8);  // 读回 outputMem_rd_data[63:0]
```

**需要观察的现象 / 预期结果**：

- 轮询循环里读 CONTROL，当 `output_done`（bit2）拉高才退出。
- 读 OUTPUT 偏移 `i` 得到 `outputMem_rd_data` 的低 64 bit。
- 因为总线只有 64 bit、而 `outputMem_rd_data` 在 `top.v` 里是 256 bit，所以**一次只能取回 1/4 的结果带宽**，需多次读（这是当前 `busConn` 设计的带宽瓶颈）。

> 说明：上述轮询掩码位次依据源码拼包顺序推断，`i*8` 的步长假设 8 字节字对齐，**待本地验证**实际 HPS 地址映射。

#### 4.4.5 小练习与答案

**练习 1**：为什么读回块要写成组合 `always @(*)` 而不是寄存式？
**答案**：Avalon 读是「当拍请求、当拍响应」的快速路径，组合直通能让 `slave_readdata` 在 `slave_read` 拉高的同一拍就有效；若寄存会多一拍延迟，需要 `waitrequest` 握手配合，本设计没用到。

**练习 2**：若主机想区分「装填完成」和「乘法完成」，分别读哪个状态位？
**答案**：装填完成看 `mem_to_fifo_done`（bit0，FILL_FIFO 的完成信号），乘法进度看 `fifo_to_arr_done`（bit1）与 `output_done`（bit2）。三者都从 CONTROL 空间一次性读回，靠位掩码区分。

---

## 5. 综合实践：整理主机访问表，并核对 busConn↔top.v 的端口差异

本实践把全讲串起来，分为两部分。

### 5.1 第一部分：主机访问表

对照 4.1~4.4 的源码，整理出完整的主机访问表。对每个地址空间，列出读/写含义与对应的 TPU 内部信号：

| 地址空间（`[9:8]`） | 基地址示例 | 主机**写**含义 | 触发的内部信号 | 主机**读**含义 | 回传内容 |
|---|---|---|---|---|---|
| **CONTROL** `00` | `0x000` | 下发控制命令（按 `writedata[3:0]` 解码） | `reset_tpu`/`fill_fifo`/`drain_fifo`/`multiply` + 各 `*_addr_base` | 读状态 | `{output_done, fifo_to_arr_done, mem_to_fifo_done}`（低 3 位） |
| **INPUT** `01` | `0x100` | 写输入矩阵元素（一次 64 bit，广播 16 路） | `inputMem_wr_en` / `_wr_addr` / `_wr_data` | 读（无效） | `0` |
| **WEIGHT** `10` | `0x200` | 写权重矩阵元素（广播 16 路） | `weightMem_wr_en` / `_wr_addr` / `_wr_data` | 读（无效） | `0` |
| **OUTPUT** `11` | `0x300` | 写（无效） | — | 读结果矩阵元素 | `outputMem_rd_data[63:0]` |

这张表的本质是：**一条 Avalon 总线被地址高位复用成了 4 条逻辑通道**——一条命令通道（CONTROL，双向）、两条写通道（INPUT/WEIGHT，只写）、一条读通道（OUTPUT，只读）。CPU 完全不需要专用指令，靠普通 `load/store` 加不同地址就能驱动整个 TPU。

### 5.2 第二部分：核对 busConn 例化 `top` 的端口 vs `top.v` 实际端口

这是本讲最重要的「求真」环节。`busConn` 在 [rtl/RTL_modified/busConn.v:189-210](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L189-L210) 例化了 `top`，但把它和 `top.v` 的实际端口表 [rtl/RTL_modified/top.v:18-34](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L18-L34) 逐项对比，会发现**大量对不上**：

**A. busConn 驱动了 `top` 不存在的端口**（在 `top.v` 里要么是内部 wire、要么根本没有）：

| busConn 例化端口 | 在 top.v 里的真实身份 |
|---|---|
| `.inputMem_wr_en` | 内部 wire，由 `master_control` 驱动（[top.v:147](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L147)、[top.v:200](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L200)），**非端口** |
| `.inputMem_wr_addr` / `.weightMem_wr_addr` | 内部由 `master_control` 的 `bus_to_mem_addr` 驱动，**非端口** |
| `.inputMem_rd_addr_base` / `.weightMem_rd_addr_base` / `.outputMem_wr_addr_base` | **top.v 完全没有这些信号** |
| `.fill_fifo` / `.drain_fifo` | **top.v 没有这两个端口** |
| `.active` | **top.v 没有 `active` 端口**（它有 `start` + `opcode`） |
| `.mem_to_fifo_done` / `.fifo_to_arr_done` / `.output_done` | **top.v 没有这三个输出端口**（它的输出是 `done` / `fifo_ready`） |

**B. top.v 要求的端口，busConn 完全没驱动**：

| top.v 实际端口 | 作用 | busConn 是否驱动 |
|---|---|---|
| `start` / `opcode[2:0]` | 触发 + 操作码（驱动 `master_control`） | 否 |
| `dim_1` / `dim_2` / `dim_3` | 矩阵尺寸 | 否 |
| `addr_1` | 地址参数 | 否 |
| `accum_table_submat_row_in` / `accum_table_submat_col_in` | 累加表子矩阵索引（分块累加用） | 否 |

**C. 唯一匹配的端口**（5 个）：`clk` / `reset` / `inputMem_wr_data` / `weightMem_wr_data` / `outputMem_rd_data`——但其中 `outputMem_rd_data` 还有**位宽不一致**：`top.v` 声明 256 bit（[top.v:70](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L70)），`busConn` 声明 128 bit（[busConn.v:102](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L102)）。

**D. 额外佐证**：在整个仓库里搜索 `matrixMultiplier`，它**只被定义在 `busConn.v`**，没有被 `tpu.v` / `tpu_system.v` / `top.v` 任何文件例化。这说明 `busConn.v` 是一份**尚未接入实际 SoC 顶层**的独立参考封装。

#### 如何解释这种不一致

两条线索拼出一个合理判断（标注为推断，**待确认**）：

- `busConn.v` 的例化端口表（直接驱动存储体使能/地址/数据 + 几个命令脉冲）反映的是一种**「总线寄存器组放在 busConn、直接驱动存储体」**的边界划分——总线解码逻辑在外层。
- `top.v` 的端口表（`opcode` / `dim_*` / `accum_*` + 内部 `master_control` 统一调度）反映的是另一种**「总线解码逻辑应该由 master_control 在 top 内部完成」**的边界划分——外层只送高层语义。

两者是同一 TPU 的**两种不同接口切法**，仓库里分别保留了各自的快照，但**没有互相对齐**。换言之，本讲标题里的「busConn 驱动 TPU」在当前提交下**不能直接综合**——需要二选一：要么按 `busConn` 的端口表重写 `top`（让存储体使能/地址成为端口），要么按 `top.v` 的端口表重写 `busConn`（让它送 `opcode/dim_*/accum_*` 而非裸存储信号）。`software/instr_set.h` 里带 `accum_row/accum_col/activate/clear` 参数的 `tpu_mat_mult` / `tpu_store_outputs` 更贴近 `top.v` 的 opcode 模型，说明**软件 API 与 `top.v` 是同一代的**，而 `busConn.v` 偏旧。

#### 分析：这种「总线封装」分层带来的好处

即便 `busConn` 与 `top.v` 当前端口不一致，这种「在计算核外包一层总线 slave」的**架构思想**本身是正确且有价值的，值得读者吸收：

1. **解耦计算细节与总线协议**：脉动阵列的歪斜、节拍、分块累加（见 u6-l1~u6-l3）极其复杂，而 Avalon 只是「读写地址」——封装层让 CPU 完全不必理解前者。
2. **串并转换**：CPU 的 64 bit 串行流 ↔ TPU 的 128/256 bit 并行宽总线，封装层负责扇出（写）与切片（读）。
3. **复用 SoC 基础设施**：只要符合 Avalon-MM slave 规范，Qsys/Platform Designer 就能自动把它接到 HPS 的桥上，带来自动仲裁、地址译码、时钟域穿越——**零手写互连代码**。
4. **接口稳定、内核可演进**：只要 slave 端的地址/命令语义不变，TPU 内部（`top` 及以下）可以随便重构（这正是 `top.v` 内部一直在演进、而外层 API `instr_set.h` 保持稳定的根因）。

一句话：**封装层的价值在于「让复杂的硬件核看起来像一个简单的内存器件」**，无论当前两份文件是否已对齐，这一思想都是把 TPU 真正「用得起」的关键一课。

---

## 6. 本讲小结

- `matrixMultiplier`（`busConn.v`）是一个 Avalon-MM slave，用 `slave_address[9:8]` 把 1024 个地址切成 **CONTROL / INPUT / WEIGHT / OUTPUT** 四段，每段 256 字。
- **写侧**用组合 `assign` 把一次 64 bit 总线写「广播」成 16 路 128 bit 内部存储写（地址复制 16 份、数据复制 2 份），完成串并转换。
- **命令解码**在 CONTROL 空间用 `slave_writedata[3:0]` 选 **RESET / FILL_FIFO / DRAIN_FIFO / MULTIPLY** 四条命令，并用 `[11:4]`、`[19:12]` 携带基地址；命令寄存保持，互斥单热点。
- **读侧**用组合 `always @(*)` 回传：CONTROL 返回 3 个 `done` 状态位、OUTPUT 返回 `outputMem_rd_data[63:0]`、其余返回 0；开头赋默认值防锁存器。
- **求真结论**：`busConn` 例化 `top` 的端口表与 `top.v` 实际端口表**严重不一致**（存储使能/地址在 `top.v` 里是内部 wire、`fill_fifo`/`opcode` 等端口缺失、`outputMem_rd_data` 位宽 256 vs 128），且 `matrixMultiplier` 未被任何顶层例化——`busConn.v` 是一份**尚未与 `top.v` 对齐**的独立参考封装。
- **架构价值**：总线封装层的核心收益是「让复杂硬件核看起来像简单内存器件」，解耦计算细节与总线协议、完成串并转换、复用 Qsys 互连——这一思想独立于当前的实现差距。

---

## 7. 下一步学习建议

- **回到软件侧**：精读 [software/instr_set.h](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/software/instr_set.h) 的完整 API，尝试写出 `tpu_init → tpu_rd_weight → tpu_fill_fifo → tpu_mat_mult → tpu_store_outputs → tpu_wr_outputs` 的完整调用序列，并标注每一步对应本讲的哪个地址空间 / 命令。你会更清楚地看到软件 API 与 `top.v`（opcode 模型）的对应关系。
- **补齐 SoC 顶层**：结合 u5-l4 重读 `tpu.v` / `tpu_system.v`，思考：若要把 `matrixMultiplier` 真正接进 Qsys 系统，需要补哪些 Avalon 端口（如 `waitrequest`、`readdatavalid`、`chipselect`）？当前 `busConn` 缺哪些标准 slave 信号？
- **尝试对齐两份接口**（进阶练习，**只读分析、勿改源码**）：选定一种边界划分（推荐 `top.v` 的 opcode 模型，因为它与软件 API 一致），在草稿纸上重新设计一个 `busConn'`，让它把 Avalon 写事务翻译成 `opcode + dim_* + accum_*` 送进 `top`，而不是直接驱动存储体使能。这是把本讲从「读懂」推向「能改」的关键一步。
- **横向对照**：把本讲的 Avalon-MM 封装思路，与核心 `rtl/tpu_top.v`（u1-l3）那种「直接暴露 SRAM 端口、由外部 testbench 驱动」的接口做对比，体会「裸端口」与「总线封装」两种风格在可集成性上的巨大差距。
