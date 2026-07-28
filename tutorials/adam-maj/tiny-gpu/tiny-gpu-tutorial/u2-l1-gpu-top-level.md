# gpu.sv 顶层架构

## 1. 本讲目标

学完本讲，你应该能够：

- 理解 SystemVerilog 用 `parameter` 做参数化设计的写法，并说明 tiny-gpu 顶层都把哪些东西设计成了「可配置」。
- 读懂 `gpu` 模块的顶层端口：时钟/复位、内核启动握手、设备控制寄存器接口，以及最重要的——多通道（multi-channel）内存握手接口 `valid / ready / data`。
- 说出 `gpu` 顶层一共实例化了哪几个子模块（DCR、两个内存控制器、dispatcher、若干个 core），以及它们各自的职责。
- 跟踪一条信号路径：某个 core 里某个线程的 LSU 发出的访存请求，如何穿过 `gpu.sv` 的中间寄存器、内存控制器，最终变成对外的 `data_mem_read_valid`。

本讲只看「顶层连线」，不深入任何子模块的内部实现——那些属于后续讲义。

## 2. 前置知识

在开始之前，你需要先建立下面几个直觉（它们都在入门单元讲过，这里做一次精简回顾）：

- **硬件描述语言（HDL）与「模块」**：SystemVerilog 用 `module ... endmodule` 描述一块电路。模块像一块带引脚（端口）的芯片：内部是逻辑，端口是与外界的连线。顶层模块 `gpu` 就是把多块小芯片「焊」到一块大板子上的那块主板。
- **参数化（parameter）**：`parameter` 是编译期常量，类似 C++ 的模板参数。改一个参数就能让「地址位宽」「核数」整套电路跟着变，而不必重写代码。
- **握手接口 valid/ready/data**：硬件里两个模块传数据时，发送方拉高 `valid` 表示「我有东西要给」，接收方拉高 `ready` 表示「我准备好收了」，双方都为真时这一次传输（transaction）才算成功，数据走 `data` 线。tiny-gpu 的外部内存就是异步的，必须靠这种握手。
- **generate 循环**：用 `generate for` 在编译期「复制粘贴」出多份相同电路。tiny-gpu 的多个核、每个核里的多个线程，都是这么展开出来的。
- **顶层在整机中的位置**：`gpu` 是被仿真器/综合工具直接驱动的最外层模块。它往内连着 DCR、dispatcher、控制器、core；往外暴露着时钟、复位、start/done 和全部内存通道。如果上一讲（u1-l2）是「地图」，本讲就是把这幅地图里最上面那个大方框拆开看内部走线。

> 提示：本讲大量出现「unpackaged 数组端口」这种写法，例如 `wire [7:0] addr [3:0]`，它表示「4 套 8 位地址」。看到带方括号在最后的声明，就理解为「若干套同样的线」。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/gpu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv) | GPU 顶层模块，把所有部件连起来 | 参数、端口、4 类子模块实例化、generate 多核展开 |
| src/controller.sv | 内存控制器（被顶层实例化两次） | 仅需确认它的端口名 `consumer_*` / `mem_*` 与参数 `WRITE_ENABLE` |
| src/core.sv | 计算核心（被 generate 实例化 NUM_CORES 次） | 仅需确认它的内存端口名，便于跟踪连线 |

> 本讲引用 `controller.sv` / `core.sv` 只是为了核对端口名，不展开它们的内部实现——那是 u3、u4 两单元的内容。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 参数化设计：用 `parameter` 把内存规格与核数变成可配置项。
2. 顶层端口：控制信号与多通道内存握手接口。
3. 子模块实例化清单：DCR / 两个内存控制器 / Dispatcher。
4. 多核展开与中间连线：`generate` 循环、LSU/fetcher 到控制器的穿透寄存器。

### 4.1 参数化设计：用 parameter 把规格变成可配置项

#### 4.1.1 概念说明

真实 GPU 的内存位宽、通道数、核数都是产品定义时定死的硬件指标。tiny-gpu 把这些都抽成 `parameter`，让我们可以用一组数字描述「这块 GPU 长什么样」：地址多宽、数据多宽、有几条并发通道、有几个核、每个核能塞几个线程。这有两个好处：

- **可读性**：所有「魔法数字」集中在模块开头，一眼看完全机规格。
- **可扩展性**：想做一个 4 核版本，只要改 `NUM_CORES`，整套连线（包括 generate 展开的核数、控制器消费者数量）都会自动跟着变。

#### 4.1.2 核心流程

顶层参数可以分为三组：

- **数据内存（DATA_MEM_\*）**：8 位地址、8 位数据、4 条并发通道。
- **程序内存（PROGRAM_MEM_\*）**：8 位地址、16 位数据（一条指令正好 16 位）、1 条通道。
- **算力**：`NUM_CORES` 个核、每核 `THREADS_PER_BLOCK` 个线程槽位。

由此可以推出两个派生量：全机 LSU 总数与 fetcher 总数。

\[
\text{NUM\_LSUS} = \text{NUM\_CORES} \times \text{THREADS\_PER\_BLOCK}
\]

\[
\text{NUM\_FETCHERS} = \text{NUM\_CORES}
\]

默认值下（2 核、每核 4 线程），NUM_LSUS = 8，NUM_FETCHERS = 2。这两个数决定了「有多少个消费者在抢内存」，是后面控制器节流的关键。

#### 4.1.3 源码精读

参数声明紧贴模块名，全部带默认值，注释里写清了每一项的含义：

[src/gpu.sv:L10-L19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L19) —— 参数化设计，DATA_MEM_* / PROGRAM_MEM_* / NUM_CORES / THREADS_PER_BLOCK。

```systemverilog
module gpu #(
    parameter DATA_MEM_ADDR_BITS = 8,        // 256 行
    parameter DATA_MEM_DATA_BITS = 8,        // 8 位数据
    parameter DATA_MEM_NUM_CHANNELS = 4,     // 4 条并发通道
    parameter PROGRAM_MEM_ADDR_BITS = 8,
    parameter PROGRAM_MEM_DATA_BITS = 16,    // 16 位指令
    parameter PROGRAM_MEM_NUM_CHANNELS = 1,
    parameter NUM_CORES = 2,
    parameter THREADS_PER_BLOCK = 4
) ( ... )
```

派生量在模块体内用 `localparam` 计算，注意它们出现在后续连线的位宽里：

[src/gpu.sv:L57-L73](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L57-L73) —— `NUM_LSUS` 与 `NUM_FETCHERS` 的定义，以及据此展开的 LSU/fetcher 通道数组。

```systemverilog
localparam NUM_LSUS = NUM_CORES * THREADS_PER_BLOCK;
...
localparam NUM_FETCHERS = NUM_CORES;
```

> 小知识：源码里还会见到 `reg [$clog2(THREADS_PER_BLOCK):0] core_thread_count`（见 [src/gpu.sv:L55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L55)）。`$clog2(x)` 是「表示 x 至少需要几位」的对数向上取整，例如 `$clog2(4)=2`，于是 `[2:0]` 共 3 位，足够装下 0~4 的线程数。

#### 4.1.4 代码实践

1. **目标**：体会「改一个参数，整机规模跟着变」。
2. **操作步骤**：
   - 打开 [src/gpu.sv:L10-L19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L19)。
   - 在脑中（不必真改源码）把 `NUM_CORES` 从 2 改成 4，`THREADS_PER_BLOCK` 保持 4。
3. **需要观察的现象**：顺着 [src/gpu.sv:L58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L58) 的 `localparam NUM_LSUS = NUM_CORES * THREADS_PER_BLOCK;` 推算新值。
4. **预期结果**：`NUM_LSUS` 从 8 变成 16，`NUM_FETCHERS` 从 2 变成 4；data 内存控制器的 `NUM_CONSUMERS` 也会因此变成 16（见 4.3）。
5. 这是「源码阅读型实践」，不需要运行；若你想验证，可在本地改参数后 `make compile`，观察综合/仿真是否仍能通过（**待本地验证**）。

#### 4.1.5 小练习与答案

- **练习 1**：把 `DATA_MEM_ADDR_BITS` 从 8 改成 6，数据内存容量变成多少？
  - **答案**：\(2^6 = 64\) 行。
- **练习 2**：为什么程序内存通道数 `PROGRAM_MEM_NUM_CHANNELS` 默认是 1，而数据内存是 4？
  - **答案**：取指（program memory）每个核一次只取一条指令、且常常命中 cache，并发需求低；数据访存（data memory）有大量 LSU 同时抢带宽，所以给更多通道缓解压力。这是「带宽分配匹配需求」的设计取舍。

### 4.2 顶层端口：控制信号与多通道内存握手接口

#### 4.2.1 概念说明

顶层端口是这块 GPU 芯片「对外伸出的引脚」。它们分三类：

1. **公共控制**：`clk`（时钟）、`reset`（复位）。所有时序逻辑都挂在时钟上。
2. **内核生命周期**：`start`（宿主拉高表示「开始执行这个内核」）、`done`（GPU 拉高表示「全部线程跑完了」）。这就是一次 kernel launch 的握手。
3. **设备控制寄存器（DCR）接口**：宿主通过 `device_control_write_enable` + `device_control_data` 把配置（最主要是 thread_count）写进 GPU。
4. **两套外部内存接口**：程序内存（只读）与数据内存（读写），每套都是「按通道重复」的多通道握手。

#### 4.2.2 核心流程

一次内核启动的时序（与入门单元 u1-l3 的 setup.py 对应）大致是：

```text
宿主:  装载 program_mem ─┐
       装载 data_mem   ─┼─> 写 DCR(thread_count) ─> 拉高 start ────────────────────┐
GPU :                                                            执行各 block ...... ─> 拉高 done
```

每条内存通道都是一组三件套：

| 方向 | 信号 | 含义 |
| --- | --- | --- |
| GPU → 外存 | `*_read_valid` / `*_read_address` | 我想读，地址是它 |
| 外存 → GPU | `*_read_ready` / `*_read_data` | 数据就绪，给你 |
| GPU → 外存 | `*_write_valid` / `*_write_address` / `*_write_data` | 我想写这个值 |
| 外存 → GPU | `*_write_ready` | 写完成 |

「多通道」的意思是：上面的三件套被复制成 `*_NUM_CHANNELS` 份，每份独立工作，互不阻塞。

#### 4.2.3 源码精读

公共控制与内核生命周期端口在最前面：

[src/gpu.sv:L20-L29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L20-L29) —— `clk/reset`、`start/done`、DCR 写入接口。

程序内存端口（只读，所以没有 write 一组）：

[src/gpu.sv:L31-L35](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L31-L35) —— 程序内存的多通道读接口。

```systemverilog
output wire [PROGRAM_MEM_NUM_CHANNELS-1:0] program_mem_read_valid,
output wire [PROGRAM_MEM_ADDR_BITS-1:0] program_mem_read_address [PROGRAM_MEM_NUM_CHANNELS-1:0],
input wire  [PROGRAM_MEM_NUM_CHANNELS-1:0] program_mem_read_ready,
input wire  [PROGRAM_MEM_DATA_BITS-1:0] program_mem_read_data [PROGRAM_MEM_NUM_CHANNELS-1:0],
```

注意 `program_mem_read_address [PROGRAM_MEM_NUM_CHANNELS-1:0]` 这种写法：前面的 `[PROGRAM_MEM_ADDR_BITS-1:0]` 是「一套地址的位宽」，后面的方括号是「一共有几套（几个通道）」。于是这是「每通道一条 8 位地址线、共 1 通道」的二维数组端口。

数据内存端口（读+写都有）：

[src/gpu.sv:L37-L45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L37-L45) —— 数据内存的多通道读/写接口，共 4 通道。

#### 4.2.4 代码实践

1. **目标**：看懂「多通道」端口的二维数组结构。
2. **操作步骤**：阅读 [src/gpu.sv:L38-L39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L38-L39) 的 `data_mem_read_valid` 与 `data_mem_read_address`。
3. **需要观察的现象**：`data_mem_read_valid` 的位宽是 `[DATA_MEM_NUM_CHANNELS-1:0]`（4 位，每通道 1 个 valid 比特）；而 `data_mem_read_address` 同时有「前缀位宽 8」和「后缀维度 4」。
4. **预期结果**：你能解释「第 `c` 号通道的读地址」写作 `data_mem_read_address[c]`，它本身是 8 位；而「第 `c` 号通道是否发起读」是 `data_mem_read_valid[c]` 这一个比特。
5. 这是纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

- **练习 1**：数据内存写接口包含哪几个信号？谁是 output、谁是 input？
  - **答案**：`data_mem_write_valid / data_mem_write_address / data_mem_write_data` 都是 output（GPU 发出写请求），`data_mem_write_ready` 是 input（外存回执）。
- **练习 2**：为什么程序内存接口里**没有** `write_*` 一组信号？
  - **答案**：程序内存只读——内核代码一旦装载就不再修改。这与后面 4.3 里给程序控制器传 `WRITE_ENABLE(0)` 是同一个设计意图的两种体现。

### 4.3 子模块实例化清单：DCR / 两个内存控制器 / Dispatcher

#### 4.3.1 概念说明

顶层 `gpu` 自己几乎不做运算，它的主要工作是「实例化」四个角色，并用 `wire/reg` 把它们连起来：

| 实例 | 模块 | 数量 | 职责（本讲只看连线层面） |
| --- | --- | --- | --- |
| `dcr_instance` | `dcr` | 1 | 接收宿主写入，输出 `thread_count` |
| `data_memory_controller` | `controller` | 1 | 在 8 个 LSU 与 4 条数据内存通道间仲裁中继 |
| `program_memory_controller` | `controller` | 1 | 在 2 个 fetcher 与 1 条程序内存通道间仲裁中继（只读） |
| `dispatch_instance` | `dispatch` | 1 | 把线程切成 block 派给各 core，汇总 `done` |

core 的实例化放在 4.4 单独讲，因为它在 generate 循环里。

#### 4.3.2 核心流程

顶层用一组「中间连线」把子模块对接，命名规律是「子模块侧叫 `consumer_*`/`mem_*`，顶层侧叫 `lsu_*`/`fetcher_*`/`*_mem_*`」：

```text
dcr:        device_control_* ──> [dcr] ──> thread_count ──> dispatch_instance
fetcher_*  ──> [program_memory_controller] ──> program_mem_*   (顶层对外端口)
lsu_*      ──> [ data_memory_controller ]  ──> data_mem_*      (顶层对外端口)
core_done  <── [dispatch] ──> core_start/core_reset/core_block_id/core_thread_count ──> 各 core
```

也就是说，两个控制器是「消费者（core 内的 LSU/fetcher）」与「外部内存」之间的中转站；dispatcher 则是「宿主」与「各 core」之间的调度枢纽。

#### 4.3.3 源码精读

DCR 实例：宿主写入端直接接顶层端口，输出 `thread_count` 是一根内部 wire：

[src/gpu.sv:L75-L83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L75-L83) —— DCR 实例化，把 `device_control_*` 转成 `thread_count`。

数据内存控制器：注意它的参数——消费者数 = `NUM_LSUS`（8），通道数 = `DATA_MEM_NUM_CHANNELS`（4）：

[src/gpu.sv:L85-L112](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L112) —— data 控制器实例化，左侧接 `lsu_*`，右侧接对外端口 `data_mem_*`。

程序内存控制器：消费者数 = `NUM_FETCHERS`（2），通道数 = 1，并显式传入 `WRITE_ENABLE(0)` 关闭写通路：

[src/gpu.sv:L114-L134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L114-L134) —— program 控制器实例化，只接 `fetcher_*` 的读侧，没有 `consumer_write_*` 连线。

Dispatcher：输入 `thread_count`、`start`、各 core 的 `core_done`；输出 `core_start/core_reset/core_block_id/core_thread_count` 以及对外的 `done`：

[src/gpu.sv:L136-L151](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L136-L151) —— dispatcher 实例化，连接内核启动握手与每核的 block 元数据。

> 消费者侧信号对照表（便于后续跟踪连线）：

| 控制器端口 | 顶层中间信号 | 来源/去向 |
| --- | --- | --- |
| `consumer_read_valid` | `lsu_read_valid` | core 的 LSU |
| `consumer_read_address` | `lsu_read_address` | core 的 LSU |
| `consumer_read_ready / data` | `lsu_read_ready / data` | 回送给 LSU |
| `consumer_write_*` | `lsu_write_*` | core 的 LSU |
| `mem_read_* / mem_write_*` | `data_mem_*` | 顶层对外端口 |
| （程序控制器）`consumer_read_*` | `fetcher_read_*` | core 的 fetcher |

#### 4.3.4 代码实践

1. **目标**：核对两个控制器实例的参数差异，理解「同一模块、不同配置」。
2. **操作步骤**：
   - 对比 [src/gpu.sv:L86-L90](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L86-L90)（data 控制器参数）与 [src/gpu.sv:L115-L120](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L115-L120)（program 控制器参数）。
3. **需要观察的现象**：`NUM_CONSUMERS`、`NUM_CHANNELS`、`WRITE_ENABLE` 三个参数在两次实例化里分别取什么值。
4. **预期结果**：
   - data 控制器：`NUM_CONSUMERS=NUM_LSUS=8`、`NUM_CHANNELS=DATA_MEM_NUM_CHANNELS=4`、`WRITE_ENABLE` 用默认值 1（可写）。
   - program 控制器：`NUM_CONSUMERS=NUM_FETCHERS=2`、`NUM_CHANNELS=1`、`WRITE_ENABLE=0`（只读）。
5. 这是阅读型实践，无需运行。

#### 4.3.5 小练习与答案

- **练习 1**：如果只看端口连线，怎么一眼区分「data 控制器」和「program 控制器」实例？
  - **答案**：data 控制器同时接了 `consumer_write_*`（来自 LSU）和 `mem_write_*`（对外）；program 控制器只接 `consumer_read_*`（来自 fetcher）和 `mem_read_*`，没有写侧。
- **练习 2**：`done` 信号最终由谁驱动？
  - **答案**：由 `dispatch_instance` 的 `.done(done)` 输出驱动（见 [src/gpu.sv:L150](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L150)）。dispatcher 收集到所有 block 完成后才拉高 `done`。

### 4.4 多核展开与中间连线：generate 循环与 LSU/fetcher 穿透寄存器

#### 4.4.1 概念说明

`NUM_CORES` 个核不是手写复制出来的，而是用一个 `generate for` 循环在编译期展开。但这里有个工程细节：由于要兼容 OpenLane EDA 流程（Verilog 2005，不能直接切片顶层信号），作者**没有**把每个 core 直接接到顶层 `lsu_*` 数组上，而是为每个 core 申明一组「本地中间信号 `core_lsu_*`」，再用一个 `always @(posedge clk)` 块把这组本地信号与顶层 `lsu_*` 数组「穿透对接」。

fetcher 侧简单得多：每核只有 1 个 fetcher，所以 `fetcher_read_valid[i]` 这类信号被 core 直接驱动，无需中间寄存器。

#### 4.4.2 核心流程

对一个 data 内存**读**请求，从某线程 LSU 出发到顶层对外端口的正向通路：

```text
core[i] 内 thread[j] 的 LSU
   │  core 端口 data_mem_read_valid[j]
   ▼
core_lsu_read_valid[j]            ← 每核本地 reg（generate 内声明）
   │  always @(posedge clk): lsu_read_valid[lsu_index] <= core_lsu_read_valid[j]
   ▼                              （lsu_index = i*THREADS_PER_BLOCK + j，注意延迟 1 拍）
lsu_read_valid[lsu_index]         ← 顶层 reg 数组
   │  data_memory_controller.consumer_read_valid(lsu_read_valid)
   ▼
[data_memory_controller]
   │  .mem_read_valid(data_mem_read_valid)
   ▼
data_mem_read_valid               ← 顶层对外 output 端口
```

回程（数据返回）走的是同一条隧道的反向，同样被那个 `always` 块寄存一拍：

```text
data_mem_read_data(对外 input) → controller.mem_read_data → lsu_read_data[lsu_index]
   │  always: core_lsu_read_data[j] <= lsu_read_data[lsu_index]
   ▼
core_lsu_read_data[j] → core 端口 data_mem_read_data[j] → LSU
```

> 关键观察：`always @(posedge clk)` 里的赋值是**寄存器赋值**，意味着 core 与控制器之间的穿透会引入 **1 个时钟周期** 的延迟。这是为了 EDA 兼容性付出的代价。

#### 4.4.3 源码精读

generate 循环骨架：外层循环变量 `i` 枚举每个核：

[src/gpu.sv:L153-L156](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L153-L156) —— `genvar i; generate for ... begin : cores`。

每核的本地中间信号（注释里解释了为什么要单独建这些信号）：

[src/gpu.sv:L157-L166](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L157-L166) —— 每核声明一组 `core_lsu_*`，用于规避 Verilog 2005 不能切片顶层信号的限制。

穿透对接的内层循环：用 `lsu_index = i * THREADS_PER_BLOCK + j` 把「核 i 的第 j 个线程」映射到顶层 `lsu_*` 数组的一个全局下标：

[src/gpu.sv:L168-L184](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L168-L184) —— 在 `always @(posedge clk)` 中双向穿透 LSU 信号，把每核本地信号接到顶层 `lsu_*`。

```systemverilog
localparam lsu_index = i * THREADS_PER_BLOCK + j;
always @(posedge clk) begin
    lsu_read_valid[lsu_index]    <= core_lsu_read_valid[j];
    lsu_read_address[lsu_index]  <= core_lsu_read_address[j];
    ...
    core_lsu_read_ready[j] <= lsu_read_ready[lsu_index];
    core_lsu_read_data[j]  <= lsu_read_data[lsu_index];
end
```

core 实例化：左侧程序内存端口直接接 `fetcher_*` 数组的第 `i` 项（每核一个 fetcher，无需中间寄存器），右侧数据内存端口接本核的 `core_lsu_*`：

[src/gpu.sv:L186-L214](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L186-L214) —— core 实例化：fetcher 侧直连 `fetcher_*[i]`，LSU 侧连 `core_lsu_*`，控制侧连 `core_reset/core_start/core_done/core_block_id/core_thread_count[i]`。

最后用 `endgenerate` 收尾：

[src/gpu.sv:L216-L217](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L216-L217) —— `endgenerate` 与 `endmodule`。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：亲手把「线程发起 data 内存读」这条正向通路走一遍，用箭头标出每个中间寄存器。
2. **操作步骤**：
   - 选定 `i=0, j=1`（第 0 核、第 1 号线程），于是 `lsu_index = 0*4 + 1 = 1`。
   - 在 [src/gpu.sv:L206-L209](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L206-L209) 找到 core 的 `data_mem_read_valid/address/ready/data` 端口，它们连到 `core_lsu_read_*`。
   - 在 [src/gpu.sv:L172-L182](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L172-L182) 的 `always` 块里，找到 `lsu_read_valid[1] <= core_lsu_read_valid[1]`。
   - 在 [src/gpu.sv:L95-L98](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L95-L98) 找到控制器把 `lsu_read_valid` 当作 `consumer_read_valid` 收下。
   - 在 [src/gpu.sv:L104-L107](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L104-L107) 找到控制器把结果从 `mem_read_*` 送到顶层 `data_mem_read_*`。
3. **需要观察的现象**：确认这条链路上一共有几处「寄存器打一拍」。重点关注 [src/gpu.sv:L172-L173](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L172-L173) 的 `<=`（非阻塞赋值，发生在时钟上升沿）。
4. **预期结果**：你能画出下面这条链（方括号里是源码位置）：

   ```text
   core0.lsu[1].data_mem_read_valid[1]
     → [L206] core_lsu_read_valid[1]
     → [L173 always@(posedge)] lsu_read_valid[1]        ← 延迟 1 拍
     → [L95]  data_memory_controller.consumer_read_valid
     → [L104] data_memory_controller.mem_read_valid
     →        data_mem_read_valid  (顶层对外端口)
   ```

5. 这条路径在控制器内部还会再经过若干状态（IDLE/READ_WAITING/READ_RELAYING），那是下一单元 u3 的内容；本讲只要确认「到 `data_mem_read_valid` 为止」即可。本实践为源码阅读型，**无需运行**。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 fetcher 侧（程序内存）不需要像 LSU 侧那样建一组 `core_fetcher_*` 中间寄存器？
  - **答案**：因为每核只有 1 个 fetcher，`fetcher_read_valid[i]` 这种「按核下标」的信号可以直接被 core 驱动/读取，不涉及「核内多线程到全局数组」的下标重排；而 LSU 侧要把「核 i 的第 j 个线程」映射到全局 `lsu_*` 的单一维下标 `lsu_index`，才需要中间信号与穿透循环。
- **练习 2**：把 `lsu_index = i * THREADS_PER_BLOCK + j` 改成 `i + j * NUM_CORES` 会怎样？
  - **答案**：下标映射会变成按「线程优先」排列，只要正反两个方向（请求向与回程向）都用同一公式，逻辑上仍能一一对应、不会错连；但全局下标顺序变了，会影响控制器轮询消费者的先后顺序。源码选的是「核优先」排列。

## 5. 综合实践

**任务：画出 `gpu` 顶层的「信号走线全景图」，并用一次内核启动串起所有部件。**

1. 在一张图上画出这些方框：`dcr`、`data_memory_controller`、`program_memory_controller`、`dispatch`、以及 `NUM_CORES` 个 `core`。
2. 用三种颜色的线分别标出：
   - **控制流**（红色）：`clk / reset / start / done / device_control_* / thread_count / core_start / core_reset / core_block_id / core_thread_count / core_done`。
   - **程序内存流**（蓝色）：`fetcher_read_*` ↔ `program_mem_*`。
   - **数据内存流**（绿色）：`core_lsu_*` → `lsu_*` → `data_memory_controller` → `data_mem_*`，注意标出 4.4 里那个 1 拍寄存器。
3. 在图上用文字注释回答：一次 kernel launch 中，`thread_count` 是从哪个模块流向哪个模块的？`done` 又是从哪里汇总出来的？
4. 自检：你的图里 data 控制器的「消费者侧」应连接到 8 个 LSU（NUM_LSUS），「内存侧」连接到 4 条对外通道；program 控制器对应 2 个 fetcher 与 1 条通道。若数量对不上，回到 4.1/4.3 复核参数。

预期成果：一张能在后续讲义（dispatcher、内存控制器、core）里反复对照回看的「顶层走线图」。本实践为设计/阅读型，**无需运行仿真**。

## 6. 本讲小结

- `gpu` 顶层用 8 个 `parameter` 把数据内存规格、程序内存规格、核数、每核线程数全部参数化，并用 `localparam` 派生出 `NUM_LSUS`、`NUM_FETCHERS` 两个规模量。
- 顶层端口分四类：公共控制（`clk/reset`）、内核生命周期（`start/done`）、DCR 写入、以及两套**多通道** `valid/ready/data` 内存握手接口；程序内存只读、数据内存可读写。
- 顶层实例化了 4 类子模块：`dcr`（产出 thread_count）、两个 `controller`（分别服务 8 个 LSU/4 通道 与 2 个 fetcher/1 通道，后者 `WRITE_ENABLE=0`）、`dispatch`（派发 block、汇总 done）。
- `NUM_CORES` 个 core 由 `generate for` 循环展开；为兼容 Verilog 2005，每核先接到本地 `core_lsu_*`，再用一个 `always @(posedge clk)` 穿透对接到顶层 `lsu_*` 数组（按 `i*THREADS_PER_BLOCK+j` 重排下标）。
- LSU 侧的穿透是寄存的，会引入 1 拍延迟；fetcher 侧每核只有 1 个，直接对接、无中间寄存器。
- 本讲的全部内容都只看「连线」，没有进入任何子模块内部——子模块的实现从下一讲开始拆。

## 7. 下一步学习建议

- 想知道 `thread_count` 是怎么被切成 block 并派给各 core 的，接着看 **u2-l2 内核启动：DCR 与 Dispatcher**，它会拆开 `dcr.sv` 与 `dispatch.sv` 的内部状态机。
- 想知道两个 `controller` 实例内部是怎么在「8 个消费者 vs 4 条通道」之间仲裁的，进入 **u3 单元（内存子系统）**，先看 u3-l1 内存模型与外部接口，再看 u3-l2 内存控制器。
- 想知道每个 core 内部长什么样、fetcher/LSU 之外还有哪些部件，进入 **u4 单元（计算核心与执行流水线）**。
- 建议在进入下一讲前，先把本讲「综合实践」的走线图画出来，后续每讲都可以把新学的子模块内部「贴」到这张图的对应方框里，逐步把整机补全。
