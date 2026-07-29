# 内核启动：DCR 与 Dispatcher

## 1. 本讲目标

本讲聚焦于「内核启动（kernel launch）」这一段：从主机把 `thread_count` 写进设备控制寄存器（DCR），到 dispatcher 把这堆线程切成 block、派发给空闲的 core，直到所有 block 跑完、GPU 拉高 `done`。

学完本讲你应该能够：

- 说清 **DCR** 在 tiny-gpu 里到底存了什么、怎么被写入。
- 手算 `total_blocks = (thread_count + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK` 这个向上取整公式，并算出「最后一个 block」的真实线程数。
- 读懂 dispatcher 的多核调度逻辑：`core_reset` / `core_start` 握手、block 派发循环、`blocks_done` 计数与 `done` 信号的时序。
- 用一张时间线把一次内核启动的「派发 → 执行 → 完成」周期画出来。

本讲只讲「谁指挥谁」，不进入 core 内部指令怎么执行（那是 Unit 4 的任务）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 从 CUDA 的 «grid / block / thread» 说起

如果你写过 CUDA，会熟悉这样的启动代码：

```c
my_kernel<<<numBlocks, threadsPerBlock>>>(...);
```

主机一次性告诉 GPU「这一共要跑多少线程、每多少个线程打包成一个 block」。tiny-gpu 把同样的概念做了极简实现：

- **thread_count**：本次内核一共要跑多少个线程（由主机写入 DCR）。
- **THREADS_PER_BLOCK（TPB）**：一个 core 一次能同时处理的线程数，编译期固定（默认 4）。
- **block**：把 `thread_count` 个线程每 TPB 个切一组，每组就是一个 block，交给一个 core 处理。

> 类比：thread_count 是「全场要送多少件快递」，TPB 是「一辆车最多装几件」，block 就是「装满一辆车的一批货」，core 是「一辆送货的车」。

### 2.2 block 数量需要向上取整

如果 `thread_count` 不能被 TPB 整除，最后一个 block 就装不满。例如 6 个线程、TPB=4，需要 2 个 block：第一个装 4 个，第二个只装 2 个。这就是后面那串 `(thread_count + TPB - 1) / TPB` 公式的由来。

### 2.3 握手信号：reset / start / done

dispatcher 和每个 core 之间靠三根信号「打乒乓球」：

| 信号 | 方向 | 含义 |
|---|---|---|
| `core_reset[i]` | dispatcher → core | 把 core 的调度器按回 IDLE（空闲复位） |
| `core_start[i]` | dispatcher → core | 通知 core「带着你拿到的 block_id 开始干活」 |
| `core_done[i]` | core → dispatcher | core 跑到 `RET` 指令，本 block 完成 |

一次完整来回是：dispatcher 拉高 `core_reset` → 下一拍放下 `core_reset` 并拉高 `core_start`（同时送出 `block_id`/`thread_count`）→ core 执行 → core 拉高 `core_done` → dispatcher 再次拉高 `core_reset` 准备复用。

> 承接 [u2-l1](u2-l1-gpu-top-level.md)：上一讲我们看到顶层把这些信号在 `gpu.sv` 里连好了线，本讲就看 dispatcher 这一头是怎么驱动它们的。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `src/dcr.sv` | 设备控制寄存器 | 用 8 位寄存器存 `thread_count`，写使能触发 |
| `src/dispatch.sv` | block 派发器 | 切 block、派发、计数、汇总 `done` |
| `src/gpu.sv` | 顶层 | 把 DCR 的 `thread_count` 喂给 dispatcher，把 dispatcher 的 `core_*` 信号接到 core |
| `src/scheduler.sv` | core 的调度器（只看输出） | 它产生的 `done` 就是 dispatcher 看到的 `core_done` |
| `test/helpers/setup.py` | 仿真启动脚本 | 演示「写 DCR → 拉 start」的真实时序 |

## 4. 核心概念与源码讲解

### 4.1 DCR：配置 thread_count

#### 4.1.1 概念说明

DCR（Device Control Register，设备控制寄存器）是主机用来「配置 GPU」的窗口。真实 GPU 有一大堆控制寄存器（时钟、电压、调度策略……），tiny-gpu 把它砍到只剩一件事：**告诉 GPU 这次内核要跑多少线程**。

注释里写得很直白：

> Used to configure high-level settings. In this minimal example, the DCR is used to configure the number of threads to run for the kernel.

#### 4.1.2 核心流程

DCR 内部就是 **一个 8 位寄存器 + 一个写使能**，时序非常简单：

```text
每个上升沿：
  若 reset            → 寄存器清 0
  否则若 write_enable → 寄存器 <= data
  否则                → 保持
输出 thread_count = 寄存器
```

因为 `thread_count` 是 8 位，所以 tiny-gpu 单次内核最多跑 256 个线程（`thread_count` 上限 = 255，加上全 0 的边界情况）。

#### 4.1.3 源码精读

DCR 的端口：一个写使能、一个 8 位数据输入、一个 8 位 `thread_count` 输出。

模块声明与端口（[src/dcr.sv:7-14](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dcr.sv#L7-L14)）：

```systemverilog
module dcr (
    input wire clk,
    input wire reset,
    input wire device_control_write_enable,
    input wire [7:0] device_control_data,
    output wire [7:0] thread_count,
);
```

核心逻辑（[src/dcr.sv:15-27](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dcr.sv#L15-L27)）——注意源码里把 register 拼成了 `device_conrol_register`，这是项目里一个真实的小拼写错误，引用时照原样保留：

```systemverilog
reg [7:0] device_conrol_register;
assign thread_count = device_conrol_register[7:0];

always @(posedge clk) begin
    if (reset) begin
        device_conrol_register <= 8'b0;
    end else begin
        if (device_control_write_enable) begin
            device_conrol_register <= device_control_data;
        end
    end
end
```

要点：

- 用非阻塞赋值 `<=`，所以 `thread_count` 在写入的**下一个**上升沿才更新（标准寄存器写时序）。
- 没有读使能——`thread_count` 是组合输出（`assign`），只要寄存器变了，下游立刻能看到新值。

顶层 `gpu.sv` 把它实例化，输出连到内部 `thread_count` 线（[src/gpu.sv:76-83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L76-L83)），这条线随后会被 dispatcher 取走。

#### 4.1.4 代码实践

**目标**：看清「写 DCR」这一步在仿真里的真实波形。

**步骤**：

1. 打开 `test/helpers/setup.py`，找到这段（[test/helpers/setup.py:30-34](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L30-L34)）：

   ```python
   dut.device_control_write_enable.value = 1
   dut.device_control_data.value = threads   # 例如 8
   await RisingEdge(dut.clk)                 # 写入沿
   dut.device_control_write_enable.value = 0
   ```

2. 运行任意一个内核仿真，例如（承接 [u1-l3](u1-l3-build-and-simulation.md) 的工具链）：

   ```bash
   make test_matadd
   ```

3. 用 GTKWave 打开生成的波形（`test` 目录下 cocotb 默认会 dump `waves.fst`/`.vcd`），定位到 `device_control_write_enable` 为 1 的那一拍，观察 `device_control_data` 与 `dut.thread_count`。

**需要观察的现象**：

- 在 `write_enable=1` 的那个上升沿，`device_control_data` 是 8（`matadd` 用 `threads=8`）。
- **下一个**上升沿，`thread_count` 才从 0 跳成 8（体现非阻塞赋值的 1 拍延迟）。

**预期结果**：`thread_count` 在写入沿的下一拍变为 8 并保持，证明 DCR 锁存成功。

> 待本地验证：波形文件名与打开方式取决于本地 cocotb/iverilog 版本；若无波形，可在 `setup.py` 写 DCR 前后各加一行 `print(dut.thread_count.value)` 来确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `device_control_write_enable` 拉高后**不放下**（保持为 1 多个周期），`thread_count` 会怎样？

**答案**：每个上升沿都会用 `device_control_data` 重新写入寄存器。只要 `device_control_data` 不变，`thread_count` 不变；若中途改了 `device_control_data`，`thread_count` 会跟着变。在本项目里 `start` 在写完 DCR 后才拉起，所以这种「一直可写」不会造成问题——但真实硬件一般会加地址/握手保护。

**练习 2**：`thread_count` 是 8 位。如果仿真时写 `threads=300` 会怎样？

**答案**：300 超过 8 位无符号上限 255，会被截断成 `300 - 256 = 44`，`thread_count` 实际为 44。dispatcher 随后会按 44 来切 block，与预期不符。所以仿真脚本必须保证 `0 ≤ threads ≤ 255`。

---

### 4.2 Dispatcher：把线程切成 block 并派发

#### 4.2.1 概念说明

`dispatch` 是 GPU 唯一的「派发单元」，坐在顶层。它的职责有三件：

1. 根据 `thread_count` 和 `THREADS_PER_BLOCK` 算出一共要派发多少个 block。
2. 把这些 block 一个个塞给**空闲**的 core（core 一旦完成上个 block 被复位，就立刻喂下一个）。
3. 数清楚已经完成了多少个 block，全部完成后拉高 `done`。

dispatcher 本身**不是一个经典的多状态状态机**，而是一个「每拍都跑一遍的循环」：每拍先看哪些 core 空闲（`core_reset=1`）就派发，再看哪些 core 完成（`core_start && core_done`）就回收。状态被摊到了若干个计数寄存器里。

#### 4.2.2 核心流程

**第一步：算 block 总数。**

\[ \text{total\_blocks} = \left\lceil \frac{\text{thread\_count}}{\text{THREADS\_PER\_BLOCK}} \right\rceil \]

硬件里没有 `ceil`，用整数除法的向上取整等价写法：

\[ \text{total\_blocks} = \frac{\text{thread\_count} + \text{THREADS\_PER\_BLOCK} - 1}{\text{THREADS\_PER\_BLOCK}} \quad (\text{整除}) \]

| thread_count | TPB | total_blocks | 各 block 线程数 |
|---:|---:|---:|---|
| 8 | 4 | 2 | 4, 4 |
| 6 | 4 | 2 | 4, 2（最后一块不满） |
| 1 | 4 | 1 | 1（最后一块严重不满） |

**第二步：派发与回收（每拍执行）。**

```text
每当时钟上升沿且 start=1：
  ① 若是 start 的第一拍（start_execution 未置位）：
        给所有 core 拉高 core_reset（一次性初始化）
  ② 若 blocks_done == total_blocks：done <= 1
  ③ 派发循环（遍历每个 core）：
        若 core_reset[i] == 1（这核刚被复位、空闲）：
            core_reset[i] <= 0
            若 blocks_dispatched < total_blocks（还有货）：
                core_start[i] <= 1
                core_block_id[i] <= blocks_dispatched
                core_thread_count[i] <= 最后一块 ? thread_count - blocks_dispatched*TPB : TPB
                blocks_dispatched = blocks_dispatched + 1   // 阻塞赋值，立即生效
  ④ 回收循环（遍历每个 core）：
        若 core_start[i] && core_done[i]（这核刚跑完一个 block）：
            core_reset[i] <= 1   // 准备复用
            core_start[i] <= 0
            blocks_done = blocks_done + 1
```

**关键时序细节（务必记住）：**

- `blocks_dispatched`、`blocks_done` 用**阻塞赋值 `=`**：在循环里一加上就立刻可见，所以同一拍内遍历到 core1 时能看到 core0 刚刚加过的值——这就是「同一拍给多个 core 依次派发 block 0、block 1」的实现手法。
- `core_reset/core_start/core_block_id/core_thread_count/done/start_execution` 用**非阻塞赋值 `<=`**：本拍计算、**下一拍**才对 core 生效。因此「完成 → 下一拍才能被重新派发」有 1 拍间隔。
- 第②步的 `blocks_done == total_blocks` 判断**在回收循环之前**，所以即便这一拍把最后一块的 `blocks_done` 加到位，`done` 也要等到**下一拍**才拉高（1 拍延迟）。

#### 4.2.3 源码精读

派发器的端口（[src/dispatch.sv:8-28](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L8-L28)）：注意 `core_block_id` 和 `core_thread_count` 是「按 core 分开」的数组端口——每个 core 各拿一份。

```systemverilog
module dispatch #(
    parameter NUM_CORES = 2,
    parameter THREADS_PER_BLOCK = 4
) (
    ...
    input wire [7:0] thread_count,
    input reg [NUM_CORES-1:0] core_done,
    output reg [NUM_CORES-1:0] core_start,
    output reg [NUM_CORES-1:0] core_reset,
    output reg [7:0] core_block_id [NUM_CORES-1:0],
    output reg [$clog2(THREADS_PER_BLOCK):0] core_thread_count [NUM_CORES-1:0],
    output reg done
);
```

**① 向上取整公式**（[src/dispatch.sv:30-31](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L30-L31)）：

```systemverilog
wire [7:0] total_blocks;
assign total_blocks = (thread_count + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK;
```

**② 三个计数器**（[src/dispatch.sv:34-36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L34-L36)）：

```systemverilog
reg [7:0] blocks_dispatched;  // 已经派出去几个
reg [7:0] blocks_done;        // 已经收回几个
reg start_execution;          // EDA hack：标记 start 的第一拍
```

**③ 复位分支**（[src/dispatch.sv:39-50](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L39-L50)）：复位时所有 core 进 `core_reset=1`、`core_start=0`，计数器清零，`core_thread_count` 给一个「安全默认值」TPB。

**④ start 首拍 hack**（[src/dispatch.sv:51-58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L51-L58)）：注释里写明这是为了在「单时钟域」里造出一个一次性的 start 触发沿：

```systemverilog
// EDA: Indirect way to get @(posedge start) without driving from 2 different clocks
if (!start_execution) begin 
    start_execution <= 1;
    for (int i = 0; i < NUM_CORES; i++) begin
        core_reset[i] <= 1;
    end
end
```

**⑤ done 判断**（[src/dispatch.sv:60-63](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L60-L63)）——位置在回收循环**之前**，所以晚 1 拍。

**⑥ 派发循环 + 最后一块的线程数**（[src/dispatch.sv:65-80](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L65-L80)），这是本模块最关键的一段：

```systemverilog
for (int i = 0; i < NUM_CORES; i++) begin
    if (core_reset[i]) begin 
        core_reset[i] <= 0;
        if (blocks_dispatched < total_blocks) begin 
            core_start[i] <= 1;
            core_block_id[i] <= blocks_dispatched;
            core_thread_count[i] <= (blocks_dispatched == total_blocks - 1) 
                ? thread_count - (blocks_dispatched * THREADS_PER_BLOCK)  // 最后一块的尾数
                : THREADS_PER_BLOCK;                                       // 满块
            blocks_dispatched = blocks_dispatched + 1;  // 阻塞，立刻给下一个 core 用
        end
    end
end
```

读法：

- 「最后一块」判定：`blocks_dispatched == total_blocks - 1`（注意此时 `blocks_dispatched` 还没自增，是即将派出的那块的编号）。
- 最后一块线程数 = `thread_count - (blocks_dispatched * TPB)`，即「总数减去前面所有满块用掉的线程」。
- `blocks_dispatched = blocks_dispatched + 1` 用阻塞赋值，保证本拍内若还有别的空闲 core，它能拿到**下一个** `block_id`。

**⑦ 回收循环**（[src/dispatch.sv:82-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L82-L89)）：

```systemverilog
for (int i = 0; i < NUM_CORES; i++) begin
    if (core_start[i] && core_done[i]) begin
        core_reset[i] <= 1;      // 复位以备复用
        core_start[i] <= 0;
        blocks_done = blocks_done + 1;  // 阻塞
    end
end
```

> 关于 `core_done`：它来自 core 内部 scheduler 跑到 `RET` 指令后拉高的 `done`（见 [src/scheduler.sv:97-101](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L97-L101)）。本讲把它当成「core 完成的信号」即可，内部状态机留给 Unit 4。

#### 4.2.4 代码实践（本讲主任务）

**目标**：手算 dispatcher 在每个周期给每个 core 分配的 `block_id` 与 `thread_count`，画出派发时间线。

**参数**：`thread_count=8`、`THREADS_PER_BLOCK=4`、`NUM_CORES=2`（即 `test_matadd.py` 的真实配置，[test/test_matadd.py:36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L36)）。

**操作步骤**：

1. 先算 `total_blocks = (8 + 4 - 1) / 4 = 11 / 4 = 2`（整除）。
2. 复位态：`core_reset=[1,1]`、`core_start=[0,0]`、`blocks_dispatched=0`、`blocks_done=0`。
3. 逐拍推演（设 `T` 为 start 首拍）：

| 拍 | 关键事件 | core0 `(block_id, tcount, start, reset)` | core1 `(block_id, tcount, start, reset)` | dispatched | done_cnt | done |
|---:|---|---|---|---:|---:|---:|
| 复位后 | 复位态 | (0, 4, 0, **1**) | (0, 4, 0, **1**) | 0 | 0 | 0 |
| **T** | start 首拍，**同时派发两块** | (**0**, 4, **1**, 0) | (**1**, 4, **1**, 0) | 2 | 0 | 0 |
| T+1 … | 两核执行 matadd 内核 | (0, 4, 1, 0) 执行中 | (1, 4, 1, 0) 执行中 | 2 | 0 | 0 |
| **F** | 两核命中 RET，`core_done=[1,1]`，回收 | (0, 4, 0, **1**) | (1, 4, 0, **1**) | 2 | **2** | 0 |
| **F+1** | `blocks_done==total_blocks` → done | (0, 4, 0, 1) | (1, 4, 0, 1) | 2 | 2 | **1** |

**需要观察的现象 / 预期结果**：

- **T 拍一次性把两个 block 全派出去**：core0 拿 block 0、core1 拿 block 1，因为复位态两核都 `core_reset=1`，派发循环遍历到 core0 时 `blocks_dispatched=0→1`（阻塞），遍历到 core1 时正好用 1 派出 block 1。
- 两块都是「满块」（各 4 个线程），所以 `core_thread_count` 都是 4；本例没有触发「最后一块尾数」分支。
- `done` 在**回收的下一拍**（F+1）才拉高，呼应前面「判断在回收循环之前」的 1 拍延迟。
- `done=1` 后仿真主循环 `while dut.done.value != 1` 退出（[test/test_matadd.py:50](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L50)）。

> 待本地验证：F 拍的确切位置取决于 core 执行 matadd 内核 + 外部内存响应耗了多少拍（见 Unit 4/6）。本表只保证「派发与回收的相对顺序与计数」正确，不保证 F 的绝对周期数。

#### 4.2.5 小练习与答案

**练习 1（最后一块尾数）**：把参数改成 `thread_count=6`、TPB=4、NUM_CORES=2。写出每个 core 拿到的 `block_id` 与 `core_thread_count`。

**答案**：`total_blocks=(6+4-1)/4=2`。T 拍同时派发：core0 ← `block_id=0, thread_count=4`（满块）；core1 ← `block_id=1, thread_count = 6 - 1*4 = 2`（最后一块尾数，走三元运算的 true 分支）。所以 block 1 只有 2 个硬件线程真正工作（core 内部用 `enable = i < thread_count` 门控，见 [src/core.sv:139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L139)）。

**练习 2（block 多于 core，体现「复用」）**：参数改成 `thread_count=16`、TPB=4、NUM_CORES=2。`total_blocks=4`，但只有 2 个 core。简述 block 2、block 3 何时被派发。

**答案**：T 拍 core0←block0、core1←block1（`blocks_dispatched=2`）。等某核（设 core0）先跑完 block0，回收循环把 `core_reset[0]=1`；**下一拍**派发循环看到 `core_reset[0]=1`，于是 core0←block2（`blocks_dispatched=3`）。core1 跑完 block1 后同理在下一拍拿到 block3。可见 core 是「完成一个、回收、复用、再喂一个」地流水化轮转，而不是等所有核一起完成。

**练习 3（阻塞 vs 非阻塞）**：如果派发循环里把 `blocks_dispatched = blocks_dispatched + 1` 改成非阻塞 `<=`，T 拍（thread_count=8）会出什么问题？

**答案**：非阻塞赋值本拍末尾才生效，循环里遍历 core1 时读到的 `blocks_dispatched` 仍是 0，于是 core0 和 core1 **都会拿到 `block_id=0`**（同一块被派两次），而且 `blocks_dispatched` 本拍只 +1（两次赋值都指向同一目标，最终值是 1）。这是典型的「混用阻塞/非阻塞」陷阱，也是源码特意用 `=` 的原因。

---

### 4.3 从 start 到 done：完整生命周期

#### 4.3.1 概念说明

DCR 和 dispatcher 不能独立工作——它们靠顶层 `gpu.sv` 连在一起，并配合仿真脚本 `setup.py` 的启动序列才能完成一次内核启动。本节把「主机视角」和「硬件视角」串起来，给你一张完整的启动时序图。

#### 4.3.2 核心流程

```text
主机(setup.py)                  DCR                  dispatcher                 cores
    |                             |                       |                        |
    |-- reset=1 ---------*--------|-----------------------|--> 所有模块进复位态      |
    |   (上升沿)         |        |                       |                        |
    |-- reset=0 -------->|        |                       |                        |
    |                    |        |                       |                        |
    |-- write_enable=1 ->|        |                       |                        |
    |   data=8           |        |                       |                        |
    |   (上升沿) --------+--> 寄存器<=8                   |                        |
    |-- write_enable=0 ->|        |                       |                        |
    |                    |        |                       |                        |
    |   thread_count=8 --+--------+---------------------->| (下一拍可见)             |
    |                    |        |                       |                        |
    |-- start=1 -------->|--------+---------------------->|                        |
    |                    |        |   start 首拍：          |                        |
    |                    |        |   派发 block0/core0 ----+--> core0 开始 FETCH     |
    |                    |        |   派发 block1/core1 ----+--> core1 开始 FETCH     |
    |                    |        |                       |                        |
    |                    |        |                  <----+-- core_done=1 (RET)      |
    |                    |        |   blocks_done++         |   (回收 → 复位)         |
    |                    |        |   ...直到 blocks_done==total_blocks              |
    |   done=1 <---------+--------+-----------------------+                        |
    |   主循环退出        |        |                       |                        |
```

#### 4.3.3 源码精读

**顶层连线**：DCR 输出的 `thread_count` 直接喂给 dispatcher（[src/gpu.sv:48](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L48) 与 [src/gpu.sv:137-151](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L137-L151)）：

```systemverilog
wire [7:0] thread_count;   // 由 DCR 驱动
...
dispatch #(.NUM_CORES(NUM_CORES), .THREADS_PER_BLOCK(THREADS_PER_BLOCK))
dispatch_instance (
    .clk(clk), .reset(reset), .start(start),
    .thread_count(thread_count),
    .core_done(core_done), .core_start(core_start), .core_reset(core_reset),
    .core_block_id(core_block_id), .core_thread_count(core_thread_count),
    .done(done)
);
```

而 `core_start/core_reset/core_done/core_block_id/core_thread_count` 这组信号，在同一份 `gpu.sv` 里又连到 generate 展开的每个 core（[src/gpu.sv:51-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L51-L55) 与 [src/gpu.sv:193-199](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L193-L199)）：dispatcher 的「指挥棒」就这样穿透到了每个 core 的 `reset/start/block_id/thread_count` 端口。

**仿真启动序列**：`setup.py` 用固定的「复位 → 写 DCR → 拉 start」三段式（[test/helpers/setup.py:19-37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L19-L37)）：

```python
dut.reset.value = 1
await RisingEdge(dut.clk)        # 复位沿
dut.reset.value = 0
...
dut.device_control_write_enable.value = 1
dut.device_control_data.value = threads
await RisingEdge(dut.clk)        # 写 DCR 沿
dut.device_control_write_enable.value = 0

dut.start.value = 1              # 启动，且此后一直保持
```

注意一个本项目约定：**`start` 一旦拉高就不再放下**（整个仿真期间 `start=1`）。dispatcher 靠 `start_execution` 标志识别「首拍」，之后每拍都靠 `core_reset`/`core_done` 来推进，所以 `start` 常驻为 1 不影响逻辑。

#### 4.3.4 代码实践

**目标**：在仿真日志里找到「内核启动」的证据链。

**步骤**：

1. 运行 `make test_matadd`，打开 `test/logs/` 下最新日志（承接 [u1-l3](u1-l3-build-and-simulation.md) 的日志机制）。
2. 翻到「执行轨迹」段（由 `format_cycle` 逐拍打印，详见 [u6-l2](u6-l2-execution-trace.md)）。
3. 定位 cycle 0：确认此时各 core 的 `core_state` 处于 IDLE、`core_reset=1`。
4. 定位随后的某一拍：某个 core 的 `core_state` 从 IDLE 跳到 FETCH——这一拍对应 dispatcher 把 `core_start` 拉高、`core_reset` 放下。

**需要观察的现象**：

- cycle 0 全部 core 在 IDLE。
- 紧接着的一拍，**两个 core 同时**进入 FETCH（因为本例两个 block 在同一拍派发）。
- 日志末尾出现 `Completed in N cycles`（[test/test_matadd.py:60](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L60)），对应 `done` 拉高、主循环退出。

**预期结果**：能从日志里读出「IDLE →（一拍后）两核同时 FETCH → … → 完成」这条主线，与 4.2.4 的时间线相互印证。

> 待本地验证：日志里 cycle 编号与 4.2.4 表中的 T/F 不直接对应（脚本 cycle 从 while 循环起算），需结合 `core_state` 字段对齐。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dispatcher 要用 `start_execution` 这个 hack，而不是直接写 `always @(posedge start)`？

**答案**：项目是单时钟域设计（只有一个 `clk`），且 `start` 在仿真里常驻为 1。若用 `posedge start` 当触发，要么需要第二个「时钟域」（综合/仿真工具链复杂），要么 `start` 一直为 1 时根本没有上升沿可触发。于是用一个寄存器 `start_execution` 在 `clk` 域里记录「我有没有处理过 start 的第一拍」，等价于造了一个一次性的启动脉冲。

**练习 2**：`done` 拉高后，如果主机不放下 `start`，会怎样？

**答案**：本项目中 `done` 一旦为 1 就保持（dispatcher 没有把 `done` 清 0 的逻辑，除非 `reset`），仿真主循环退出后就不再喂时钟相关逻辑，所以无害。但严格说，真实硬件应在 `done` 后由主机放下 `start` 并重新写 DCR 才能启动下一个内核；tiny-gpu 假设「一次仿真只跑一个内核」（呼应 [u1-l1](u1-l1-project-overview.md) 提到的简化原则）。

## 5. 综合实践

把本讲的知识串起来，完成一次「纸上调度器」推演，再到源码里印证。

**任务**：自拟一组参数 `thread_count=10`、`THREADS_PER_BLOCK=4`、`NUM_CORES=2`，完成下面全部小题。

1. 计算 `total_blocks`，并列出每个 block 的真实线程数。
2. 用一张表画出从复位到 `done=1` 的派发/回收时间线（像 4.2.4 那样），假设「两核执行任意 block 都恰好花费 3 拍」。
3. 指出哪一个 block 触发了 `core_thread_count` 的「尾数」分支，写出该分支在源码里的表达式与计算结果。
4. 在 `src/dispatch.sv` 里找到第②步 `done` 判断和第④步回收循环的相对位置，解释为什么 `done` 比「最后一个 block 完成」晚 1 拍。

**参考要点**：

1. `total_blocks=(10+4-1)/4=13/4=3`。三块线程数：block0=4，block1=4，block2=`10-2*4=2`。
2. T 拍：core0←block0(4)、core1←block1(4)，`dispatched=2`。T+3 拍两核完成回收（`done_cnt=2`），T+4 拍 core0←block2(2)、core1 空闲但无货可派（`dispatched=3`）。core0 再跑 3 拍完成，回收 `done_cnt=3`，下一拍 `done=1`。（核心：block 多于 core 时出现 core1 空闲等待。）
3. block2 是最后一块，命中 `blocks_dispatched == total_blocks - 1`（即 `2 == 2`），走 [src/dispatch.sv:73-75](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L73-L75) 的 `thread_count - (blocks_dispatched * THREADS_PER_BLOCK) = 10 - 8 = 2`。
4. 见 [src/dispatch.sv:60-63](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L60-L63) 与 [src/dispatch.sv:82-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L82-L89)：判断在前、回收在后，`blocks_done` 在回收循环里才加到位，所以 `done` 必须等到下一拍判断时才能命中。

## 6. 本讲小结

- **DCR** 是一个 8 位寄存器，主机用 `write_enable + data` 写入 `thread_count`，非阻塞赋值带来 1 拍延迟。
- block 总数用向上取整整数式 `total_blocks = (thread_count + TPB - 1) / TPB` 计算（[src/dispatch.sv:31](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L31)）。
- dispatcher 每拍跑「派发循环 + 回收循环」：`core_reset=1` 的核被派发新 block，`core_start && core_done` 的核被回收复用。
- 派发用**阻塞** `blocks_dispatched = ... + 1`，使一拍内能给多个核依次派发不同 block；控制信号用**非阻塞** `<=`，下一拍才对 core 生效。
- 最后一块的线程数走三元运算的尾数分支 `thread_count - blocks_dispatched*TPB`（[src/dispatch.sv:73-75](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L73-L75)）。
- `done` 在最后一个 block 完成、`blocks_done==total_blocks` 命中后**下一拍**才拉高，仿真主循环随即退出。

## 7. 下一步学习建议

本讲讲完了「谁来指挥 core 干活」，但还没讲「core 拿到 block 之后怎么跑」。建议接着：

- **u3（内存子系统）**：core 跑内核时离不开访存，先看 `controller.sv` 如何在外部内存与众多 LSU 之间仲裁，这会直接决定本讲时间线里「F 拍」的真实位置。
- **u4（计算核心与执行流水线）**：进入 `core.sv`，看 scheduler 的七阶段状态机如何把一个 block 从 FETCH 跑到 RET，从而产生本讲反复出现的 `core_done`。
- 复读 [src/dispatch.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv) 全文，对照本讲的拍级时间线，确认你对「阻塞 vs 非阻塞」的理解没有偏差——这是后续读懂所有 tiny-gpu 时序模块的基础。
