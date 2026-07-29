# cocotb 仿真测试框架

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「Python 代码如何驱动一段 SystemVerilog 硬件跑起来」这件事的原理，以及 cocotb 的 VPI 桥把 `.py` 测试台与 `.sv` 被测对象连在一起的方式。
- 读懂 `test/helpers/setup.py`，把一次内核仿真从「建时钟 → 复位 → 装内存 → 写 DCR → 拉 start」的启动序列逐步讲明白。
- 读懂 `test/helpers/memory.py` 里的 `Memory` 类，理解它如何用一个 Python 列表扮演外部 DRAM，并在每一拍响应 DUT 的多通道握手请求。
- 读懂 `test/test_matadd.py` 主循环 `while dut.done.value != 1:` 里 `data_memory.run()` / `program_memory.run()` / `ReadOnly` / `RisingEdge` 四者的执行顺序与各自作用。
- 能独立定位仿真日志、读懂输出结构，并动手扩展测试台。

本讲是把镜头从「硬件内部」转到「**测试台如何调度这些硬件**」。前置讲义已经建立了硬件视角：u3-l1 讲过外部内存由 Python 的 `Memory` 类扮演、每拍零延迟同拍响应；u4-l2 讲过 scheduler 用 `core_state` 七阶段状态机驱动 core 工作。本讲要回答的是：**是谁在一拍一拍地拨动时钟、又是谁在每一拍把外部内存的应答喂回去？**

## 2. 前置知识

进入源码前，先用通俗语言补齐 cocotb 的基础直觉。

### 2.1 什么是「HDL 仿真」

SystemVerilog（`.sv`）描述的是电路，但电路不会自己「跑」。需要一个**仿真器**（这里用 Icarus Verilog，见 u1-l3）把电路翻译成可执行的模型，再外加一个**激励源**不断给电路喂时钟、喂输入、读输出。这个激励源就是 **testbench（测试台）**。

在 tiny-gpu 里，`.sv` 文件是被测对象，业界术语叫 **DUT（Device Under Test，被测设计）**；`.py` 文件是测试台，用 cocotb 库写成。

### 2.2 为什么测试台用 Python 写

传统 testbench 也用 Verilog 写。cocotb 的贡献是：**允许你用 Python 写测试台**，再通过 VPI（Verilog Procedural Interface）这座桥，让 Python 直接读写 DUT 内部的信号。

好处是 Python 有列表、字典、字符串处理、文件 IO，写「矩阵加法期望值比对」「把指令反汇编成可读字符串」这类逻辑远比 Verilog 方便。tiny-gpu 的 `Memory` 类（用 Python 列表模拟内存）、`format_cycle`（把二进制信号翻译成可读轨迹）都得益于此。

> 回顾 u1-l3：`Makefile` 里 `vvp ... -m libcocotbvpi_icarus build/sim.vvp` 就是启动 vvp 仿真器并挂载 cocotb 的 VPI 库；`MODULE=test.test_$*` 告诉 cocotb 去 Python 的 `test.test_matadd` 模块里找被 `@cocotb.test()` 装饰的测试函数。

### 2.3 Python 协程与「触发器」

cocotb 的测试函数是 **async 协程**（`async def`），用 `await` 暂停。`await` 暂停时并不是死等，而是把控制权交还给仿真器，让仿真器推进时间、让 DUT 的逻辑跑下去，直到某个条件成立再唤醒协程。这个「唤醒条件」就是 **trigger（触发器）**，例如：

- `RisingEdge(dut.clk)`：等到 `clk` 出现一次上升沿；
- `ReadOnly()`：等到当前仿真时刻进入「只读阶段」（4.5 节详解）。

只要没遇到 `await`，协程就连续执行，仿真时间不前进。所以**「前进一拍」必须靠 `await` 一个与时钟相关的触发器**。

## 3. 本讲源码地图

本讲涉及 5 个文件，分工如下：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [test/helpers/setup.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L7-L37) | 启动序列 | 建时钟、复位、装内存、写 DCR、拉 start |
| [test/helpers/memory.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L4-L99) | 外部内存模型 | `run()` 每拍响应 DUT 读写、`load()` 灌初值、`display()` 打表 |
| [test/helpers/logger.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py#L3-L16) | 日志器 | 把字符串写入 `test/logs/log_<时间戳>.txt` |
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L8-L66) | 示例测试 | 用上面三者跑矩阵加法内核，是本讲主循环的范本 |
| [Makefile](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L5-L8) | 编译入口 | `test_%` 模式规则串起 sv2v → iverilog → vvp |

> 这三个 helper 是**通用工具**，任何新内核测试（如 `test_matmul.py`）都复用它们，只是传入不同的 `program` / `data` / `threads`。

## 4. 核心概念与源码讲解

本讲按「**测试台的执行顺序**」拆成 5 个最小模块，正好对应 `setup()` 的四步启动再加上主循环。建议边读边对照 [test/helpers/setup.py:7-37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L7-L37) 全文。

### 4.1 建时钟：Clock 与 RisingEdge 触发

#### 4.1.1 概念说明

数字电路靠**时钟**节拍前进。tiny-gpu 里所有 `always @(posedge clk)` 的寄存器（PC、NZP、`alu_out_reg`、DCR、dispatcher 计数器等）都只在时钟上升沿那一瞬间更新。

测试台要做的第一件事，就是给 DUT 的 `clk` 引脚制造一个永不停止的方波。cocotb 提供 `Clock` 类自动生成方波；再用 `RisingEdge(dut.clk)` 这个触发器，让协程「等到下一个上升沿再继续」，从而**和硬件同步节拍**。

#### 4.1.2 核心流程

```text
Clock(dut.clk, 25, units="us")   # 周期 25us，高 12.5us / 低 12.5us
  ↓
cocotb.start_soon(clock.start()) # 把方波生成器丢到后台，让它自己一直翻转
  ↓
主协程继续往下走（不等时钟）
  ↓
需要同步时：await RisingEdge(dut.clk)  # 暂停，直到下一次 0→1 跳变
```

关键点：`start_soon` 把时钟变成一个**独立的后台协程**，与主测试协程并发运行。如果不用 `start_soon` 而是直接 `await clock.start()`，协程会永远卡在翻转时钟上，再也回不来——所以时钟必须「分叉」出去。

#### 4.1.3 源码精读

时钟创建只有两行，在 setup 的最开头——[test/helpers/setup.py:16-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L16-L17)：

```python
clock = Clock(dut.clk, 25, units="us")
cocotb.start_soon(clock.start())
```

`Clock(dut.clk, 25, units="us")` 的第一个参数是信号句柄，第二个是**周期**（不是半周期），单位微秒。`clock.start()` 返回一个永不退出的协程，`start_soon` 把它扔进 cocotb 的事件循环，于是仿真器每 12.5us 把 `clk` 翻转一次。

VPI 桥是怎么挂上去的？见 [Makefile:5-8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L5-L8) 的 `test_%` 规则：`-s gpu` 指定顶层模块为 `gpu`（于是注入的 `dut` 就代表 `gpu`），`-m libcocotbvpi_icarus` 挂载 cocotb 的 VPI 实现，`MODULE=test.test_$*` 选定要跑的 Python 模块。

#### 4.1.4 代码实践

1. **实践目标**：验证 tiny-gpu 的仿真是「周期精确（cycle-accurate）」而非「时间精确」的。
2. **操作步骤**：
   - 打开 [test/helpers/setup.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L16-L17)，在本地副本把第 16 行的 `25` 改成 `50`（即 `Clock(dut.clk, 50, units="us")`）。
   - 运行 `make test_matadd`。
   - 打开日志末尾的 `Completed in {cycles} cycles`。
3. **需要观察的现象**：把时钟周期翻倍，仿真总耗时（墙上时间）会变长，但**报告的 `cycles` 数应当不变**。
4. **预期结果**：内核逻辑只关心「第几个上升沿」，与两次上升沿之间隔多少微秒无关。这正说明硬件是周期驱动的，时钟周期只是仿真器推进时间的颗粒度。
5. **待本地验证**：受机器与 cocotb 版本影响，绝对墙上时间无标准答案，但 `cycles` 计数与周期无关这一点应稳定复现。验证完请还原代码。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `start_soon(clock.start())` 改成 `await clock.start()`，会发生什么？

> **参考答案**：`clock.start()` 是一个无限循环协程，`await` 它会让主测试协程永远停在这里翻转时钟，后续的复位、装内存、拉 start 都不会执行，仿真会挂起或超时。这正是必须用 `start_soon`「分叉」的原因。

**练习 2**：`RisingEdge(dut.clk)` 与时钟周期 25us 有什么关系？

> **参考答案**：时钟每 25us 产生一次上升沿，所以每 `await RisingEdge(dut.clk)` 一次，仿真时间前进 25us（即一个周期）。周期长短改变的是「时间刻度」，不改「第几次跳变」。

---

### 4.2 复位脉冲：reset 序列

#### 4.2.1 概念说明

上电瞬间，所有寄存器的值是未定义的（仿真里常表现为 `x`）。必须先给一个**复位脉冲**让寄存器回到已知初值，否则 PC、NZP、dispatcher 计数器等从随机态出发，行为完全不可控。

tiny-gpu 的复位是同步复位思路在测试台上的简化体现：拉高 `reset`，让时钟走一个上升沿，使所有受复位影响的寄存器在沿上采样到 `reset=1`，再把 `reset` 拉低。

#### 4.2.2 核心流程

```text
dut.reset.value = 1        # 拉高复位（ReadWrite 阶段可写）
await RisingEdge(dut.clk)  # 走一个上升沿，寄存器在此沿采样到 reset=1
dut.reset.value = 0        # 拉低复位，之后进入正常工作
```

注意中间那一次 `await RisingEdge`：它保证 `reset=1` 至少覆盖一个完整的上升沿。如果没有这一句，`reset` 可能在一个沿都没经历过的情况下就被拉低，寄存器来不及复位。

#### 4.2.3 源码精读

[test/helpers/setup.py:20-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L20-L22) —— 拉高 `reset`、等一个上升沿、再拉低。三行构成最小复位脉冲，确保 DUT 所有寄存器在进入工作前被初始化：

```python
dut.reset.value = 1
await RisingEdge(dut.clk)
dut.reset.value = 0
```

回顾 u4-l1：每个线程的 `%threadIdx`（R15）正是在复位时被写成线程号 `i`。如果跳过这一步，`%threadIdx` 将是 `x`，矩阵加法里 `i = blockIdx*blockDim + threadIdx` 会算出垃圾值。

#### 4.2.4 代码实践

1. **实践目标**：体会「没有复位会怎样」。
2. **操作步骤**：在本地副本把 [setup.py:20-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L20-L22) 的复位三行注释掉（或把 `dut.reset.value = 1` 改成 `0`），运行 `make test_matadd`。
3. **需要观察的现象**：日志里的执行轨迹出现大量 `x` 或异常寄存器值；最终 `assert result == expected` 大概率失败，或 `while dut.done.value != 1` 循环迟迟不退出。
4. **预期结果**：测试失败或挂起。**待本地验证**：因寄存器初值依赖仿真器，现象可能不同，但「无法得到正确结果」应稳定成立。看完结论后请还原代码。
5. 若不便运行，可做纯推理：寄存器不复位 → PC/core_state/计数器初值不确定 → 行为不可复现。

#### 4.2.5 小练习与答案

**练习**：为什么复位代码中间必须有 `await RisingEdge(dut.clk)`，而不能直接写 `dut.reset.value = 1; dut.reset.value = 0`？

> **参考答案**：在同一个 ReadWrite 阶段里先后写 `1` 和 `0`，cocotb 只会让 DUT 最终看到 `reset=0`，中间的 `1` 被覆盖、没有经历任何上升沿，寄存器无从采样。必须用 `await RisingEdge` 把仿真时间推进一个沿，让 `reset=1` 真正被寄存器捕获。

---

### 4.3 装填内存：load 程序与数据

#### 4.3.1 概念说明

回顾 u3-l1：tiny-gpu 对外的两块内存（data：8 位地址 / 8 位数据 / 4 通道；program：8 位地址 / 16 位指令 / 1 通道）在仿真里由 Python 的 `Memory` 类扮演。这个类的核心数据结构就是一个普通 Python 列表 `self.memory`。

`load()` 的职责是：**在仿真开始前，把内核的指令序列和数据初值写进这个列表**。这一步纯粹是 Python 内存操作，**不与时钟打交道**，因为它填的是外部模型，不是 DUT 内的寄存器。

#### 4.3.2 核心流程

```text
program_memory.load(program)   # 把 13 条 16 位指令依次写入 self.memory[0..12]
data_memory.load(data)         # 把 16 个数据初值依次写入 self.memory[0..15]
```

`load(rows)` 对 `rows` 做枚举：第 `address` 个元素写入 `self.memory[address]`，相当于「地址从 0 开始顺序铺放」。

#### 4.3.3 源码精读

[test/helpers/memory.py:75-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L75-L77) —— `load` 调用 `write`，按列表下标顺序把数据写入对应地址，是 `Memory` 类灌初值的唯一入口：

```python
def load(self, rows: List[int]):
    for address, data in enumerate(rows):
        self.write(address, data)
```

注意 `load` 没有任何 `await`，它和时钟无关。再看 `Memory` 的容量是怎么定的——[test/helpers/memory.py:9](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L9)：`self.memory = [0] * (2**addr_bits)`，按地址位宽分配容量（8 位地址 → 256 个单元）。`load` 只是改写其中前若干个，其余保持 0。

`display(rows)` 则是它的展示手段——把 `self.memory` 前 `rows` 个单元打印成表格，这也是日志里「初始数据内存」「最终数据内存」两段的来源（见 4.5 节与日志的配合）。

#### 4.3.4 代码实践

1. **实践目标**：建立「列表下标 ↔ 内存地址」的直观对应。
2. **操作步骤**：
   - 打开 [test/test_matadd.py:30-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L30-L33)，看 `data` 列表：前 8 个是矩阵 A，后 8 个是矩阵 B。
   - 在本地副本把第 31 行的第一个元素从 `0` 改成 `9`（矩阵 A 的第一个数变成 9）。
   - 运行 `make test_matadd`，打开日志，找到第一段 `DATA MEMORY` 表格。
3. **需要观察的现象**：`DATA MEMORY` 表格中 `Addr 0` 的 `Data` 由 `0` 变成 `9`，其余行不变。
4. **预期结果**：因为 `load` 按下标写入，`data[0]` 落到地址 0。这印证了「列表下标就是内存地址」。验证完请还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load` 不需要 `await RisingEdge`，而复位却需要？

> **参考答案**：`load` 写的是 Python 侧的 `Memory.memory` 列表（外部模型），与 DUT 的时钟无关；复位写的是 DUT 的 `reset` 信号，必须等上升沿才能被 DUT 内部寄存器采样。

**练习 2**：program 内存的 `data_bits=16`，data 内存的 `data_bits=8`，这个差异在 `load` 时有体现吗？

> **参考答案**：`load` 本身不关心位宽，只做 `self.memory[address] = data`。位宽的差异体现在 `run()` 中解析 / 拼回多通道信号时（见 4.5 节），program 用 16 位拼、data 用 8 位拼。

---

### 4.4 写 DCR 与拉 start：把内核「点火」

#### 4.4.1 概念说明

内存装好、复位完成之后，硬件还不知道要跑多少个线程。回顾 u2-l2：**DCR（Device Control Register，设备控制寄存器）** 是一个 8 位寄存器，主机通过 `device_control_write_enable` + `device_control_data` 把 `thread_count` 写进去，dispatcher 据此把线程切成 block 派发。

写完 DCR 后还要拉高 `start`，dispatcher 才会开始派发 block。这两步合起来就是把内核「点火」。

#### 4.4.2 核心流程

```text
# 写 DCR
dut.device_control_write_enable.value = 1   # 打开写使能
dut.device_control_data.value = threads     # 送上 thread_count（如 8）
await RisingEdge(dut.clk)                   # 走一个沿，DCR 锁存
dut.device_control_write_enable.value = 0   # 关掉写使能（只写一次）

# 点火
dut.start.value = 1                          # 拉高 start，dispatcher 开始派 block
```

回顾 u2-l2 的细节：DCR 用**非阻塞赋值**写入，所以 `thread_count` 要到下一个时钟沿才对 dispatcher 生效。这正是这里 `await RisingEdge` 的意义——确保 `thread_count` 已被 DCR 锁存，再继续。

#### 4.4.3 源码精读

[test/helpers/setup.py:31-34](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L31-L34) —— 写使能拉高、送上 `threads`、等一个上升沿让 DCR 锁存、再拉低写使能。四行完成「告诉 GPU 这次跑几个线程」：

```python
dut.device_control_write_enable.value = 1
dut.device_control_data.value = threads
await RisingEdge(dut.clk)
dut.device_control_write_enable.value = 0
```

`device_control_data` 是 8 位（见 [src/gpu.sv:28-29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L28-L29) 的 `input wire [7:0] device_control_data`），与 DCR 的 8 位寄存器宽度吻合。

[test/helpers/setup.py:37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L37) —— 拉高 `start`，dispatch 状态机随即开始把 block 派发给空闲 core。注意这里**没有**在 `start=1` 之后再 `await RisingEdge`：setup 函数到此返回，把控制权交回 `test_matadd` 的主循环。第一拍要不要等、怎么等，由主循环决定（见 4.5）。

#### 4.4.4 代码实践

1. **实践目标**：观察 `threads` 与最终写入结果数量的关系。
2. **操作步骤**：在本地副本把 [test/test_matadd.py:36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L36) 的 `threads = 8` 改成 `threads = 4`，运行 `make test_matadd`。
3. **需要观察的现象**：日志末尾的断言会失败，因为只有前 4 个线程跑了（只算出 C[0..3]），地址 20–23 的结果仍为 0，与期望不符。
4. **预期结果**：报 `Result mismatch at index 4: expected ...`。这印证 DCR 的 `thread_count` 直接决定有多少线程参与计算——少给线程，就少算结果。看完后还原为 8。
5. **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么写 DCR 之后要 `await RisingEdge`，而拉 `start` 之后 setup 直接返回、不等沿？

> **参考答案**：DCR 用非阻塞赋值，`thread_count` 下一拍才生效，必须等沿确认锁存；而 `start` 一旦拉高，dispatcher 会在随后到来的沿上自然开始派发，主循环本身就会 `await RisingEdge`，所以 setup 不必在这里多等一拍——交给主循环即可。

**练习 2**：如果不把 `write_enable` 拉回 0，会发生什么？

> **参考答案**：DCR 会持续被写，`thread_count` 在后续每个沿都被刷新成 `device_control_data` 上的值。若该值未变则无碍，但任何毛刺都会改写 `thread_count`，属于不安全的写法，故应当「写一次即关使能」。

---

### 4.5 主循环：while not done 与每拍的协同

这是本讲的核心，也是本讲综合实践任务所在。它把前面所有部件串成一个「一拍一拍推进仿真」的循环。

#### 4.5.1 概念说明

setup 返回后，内核已经点火，但仿真并不会自己前进。测试台必须：

1. **每一拍都驱动外部内存**——因为 DUT 的 fetcher 要取指令、LSU 要读写数据，它们的请求都指向 Python 的 `Memory`。如果某拍不调 `run()`，DUT 就收不到应答、卡在 WAIT/FETCHING。
2. **每一拍都记录轨迹**——把 DUT 内部信号快照写进日志，供事后阅读（u6-l2 专题讲轨迹格式）。
3. **每一拍都推进时钟**——靠 `await RisingEdge` 让 DUT 的寄存器更新。
4. **判断何时结束**——轮询顶层 `done` 信号，为 1 则退出。

要正确完成这四件事，必须理解 cocotb 在**一个仿真周期（一个时钟周期）内**的内部阶段划分。

#### 4.5.2 核心流程：cocotb 的阶段模型

一个时钟周期内，cocotb 把时间分成若干阶段，对本讲最重要的是两个：

```text
posedge clk（上升沿）
   │  DUT 的 always @(posedge clk) 触发，寄存器更新
   ▼
ReadWrite 阶段（可读可写）
   │  cocotb 协程恢复；run() 在此写 mem_read_ready / mem_read_data
   ▼
（组合逻辑传播 + 非阻塞赋值更新）
   ▼
ReadOnly 阶段（只读，不可写）
   │  所有写入已沉淀，信号稳定；format_cycle 在此读快照
   ▼
（下一个 posedge）→ 下一周期
```

- **ReadWrite 阶段**：协程可以读、也可以写信号。`Memory.run()` 写应答信号必须在这一阶段，因为它要「修改」DUT 的输入。
- **ReadOnly 阶段**：当前周期所有写入都已传播完毕，信号值稳定。此时**只允许读，禁止写**（强行写会抛异常）。适合做「拍快照」。

于是主循环被设计成固定的四步：

```text
while dut.done.value != 1:
    ① data_memory.run()            # ReadWrite：给 data 内存喂应答
    ② program_memory.run()         # ReadWrite：给 program 内存喂应答（取指！）
    ③ await ReadOnly()             # 等到 ReadOnly，信号稳定
    ④ format_cycle(dut, cycles)    # 只读：把本拍 DUT 内部状态写进日志
    ⑤ await RisingEdge(dut.clk)    # 推进到下一个上升沿
    ⑥ cycles += 1
```

顺序的必然性：

- ①② 必须在 ③ 之前——`run()` 要写信号，而 ReadOnly 之后禁止写。
- ④ 必须在 ③ 之后——读快照要等信号稳定，否则可能读到组合逻辑半传播的中间值。
- ⑤ 在最后——把这一拍的成果「锁」进 DUT 寄存器，进入下一拍。

一句话概括：先**喂**（run，写应答），再**看**（ReadOnly + format，读快照），最后**走**（RisingEdge，推进）。

#### 4.5.3 源码精读

先看主循环本体——[test/test_matadd.py:49-60](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L49-L60)：

```python
cycles = 0
while dut.done.value != 1:
    data_memory.run()
    program_memory.run()

    await cocotb.triggers.ReadOnly()
    format_cycle(dut, cycles)
    
    await RisingEdge(dut.clk)
    cycles += 1

logger.info(f"Completed in {cycles} cycles")
```

- 循环条件 `dut.done.value != 1`：`done` 由 dispatcher 在所有 block 回收后拉高（回顾 u2-l2）。一旦看到 `done==1`，循环退出，打印总周期数。
- 注意循环里调用的次序：`data_memory.run()` 在前、`program_memory.run()` 在后，但二者都在同一个 ReadWrite 阶段、写的是两套互不相干的信号（`data_mem_*` vs `program_mem_*`），**执行次序不影响结果**。真正重要的是它们都在 `ReadOnly` 之前。
- 注意 `ReadOnly` 在本文件里用全限定名 `cocotb.triggers.ReadOnly()`（[test_matadd.py:54](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L54)），而 `RisingEdge` 在文件头 `from cocotb.triggers import RisingEdge` 导入后直接用——两种写法等价，只是导入风格不同。
- 循环退出后，[test/test_matadd.py:63-66](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L63-L66) 用 `assert` 校验 data 内存第 16 行起的结果是否等于 `A[i]+B[i]`。这个断言才是 cocotb 判定测试通过/失败的依据。

再看 `Memory.run()` 到底做了什么（回顾 u3-l1 的多通道握手）——[test/helpers/memory.py:24-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L24-L45)：

```python
mem_read_valid = [
    int(str(self.mem_read_valid.value)[i:i+1], 2)            # 每通道 1 位
    for i in range(0, len(str(self.mem_read_valid.value)), 1)
]
mem_read_address = [
    int(str(self.mem_read_address.value)[i:i+self.addr_bits], 2)  # 每通道 addr_bits 位
    for i in range(0, len(str(self.mem_read_address.value)), self.addr_bits)
]
...
for i in range(self.channels):
    if mem_read_valid[i] == 1:
        mem_read_data[i] = self.memory[mem_read_address[i]]   # 查表
        mem_read_ready[i] = 1
...
self.mem_read_data.value = int(''.join(format(d, '0'+str(self.data_bits)+'b') for d in mem_read_data), 2)
self.mem_read_ready.value = int(''.join(format(r, '01b') for r in mem_read_ready), 2)
```

`run()` 逐位 / 逐段解析 cocotb 把多通道信号**拼接成的长串**（因 cocotb 将 unpacked 数组序列化为一维二进制），还原出每通道的 `valid` 与 `address`；随后对每个有请求的通道从 `self.memory` 取数，把 `read_data` 与 `read_ready` 按同样布局拼回，写回 DUT。这就是 u3-l1 所说「零延迟同拍响应」的实现。

> 为什么 cocotb 要把多通道信号拼成长串？因为 iverilog 经 VPI 暴露的是一维向量，cocotb 把 `data_mem_read_valid[3:0]` 这样的 4 位数组当成一个 4 位二进制串返回。`run()` 里 `[i:i+1]`（valid 每通道 1 位）、`[i:i+addr_bits]`（address 每通道 8 位）的切片，正是在把这个长串按固定步长拆回每通道。列表下标 `i` 即通道号，是 `Memory` 内部的契约。

写回部分（仅 data 内存，program 只读）——[test/helpers/memory.py:47-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L47-L69) 对写请求做同样的拆解，把 `write_data` 写进 `self.memory[write_address]`，并回送 `write_ready`。program 内存在 `__init__` 时就因 `name != "program"` 跳过了写信号句柄的获取——[test/helpers/memory.py:18-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L18-L22)，与顶层 program 控制器 `WRITE_ENABLE=0` 的只读设定（u3-l1）互相对应，故 `run()` 里这段写逻辑也不会为 program 执行。

最后是日志落盘——[test/helpers/logger.py:12-15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py#L12-L15)：`info()` 把消息拼成一行，追加写入日志文件。`format_cycle`（用 `logger.debug`）和 `display`（用 `logger.info`）都通过这个 `logger` 输出，且 `level="debug"` 保证 debug 消息也会落盘。文件名带启动时刻的时间戳——[test/helpers/logger.py:4-5](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py#L4-L5)，所以每次 `make test_*` 都生成一个新文件，互不覆盖。正因如此，仿真时控制台看不到轨迹，必须去 `test/logs/` 翻日志。

#### 4.5.4 代码实践（本讲核心实践任务）

1. **实践目标**：吃透主循环里 `data_memory.run()` / `program_memory.run()` / `ReadOnly` / `RisingEdge` 四者在每个周期的执行顺序与各自作用。这是本讲规格指定的核心任务。
2. **操作步骤**：
   - 打开 [test/test_matadd.py:49-58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L49-L58) 的主循环。
   - 对照 4.5.2 的阶段模型，为下面四者各写一句话，说明「它在哪个阶段触发」「为什么放在这个位置」：
     - `data_memory.run()` / `program_memory.run()`
     - `await cocotb.triggers.ReadOnly()`
     - `format_cycle(dut, cycles)`
     - `await RisingEdge(dut.clk)`
   - 验证你的理解：在本地副本把第 54 行 `await cocotb.triggers.ReadOnly()` 临时**移到** `data_memory.run()` 之前（即先 ReadOnly 再 run），运行 `make test_matadd`。
3. **需要观察的现象**：步骤 3 会直接抛出 cocotb 异常，类似 `Unable to set ... while in ReadOnly phase`（不同版本措辞略异）。因为 ReadOnly 阶段禁止写信号，而 `run()` 紧跟其后要写 `mem_read_data` / `mem_read_ready`。
4. **预期结果**：
   - 步骤 2 参考答案——
     - `data_memory.run()` / `program_memory.run()`：在 **ReadWrite 阶段**执行；作用是**响应 DUT 本拍发出的内存请求**（fetcher 取指、LSU 读写数据），把 `ready`/`data` 写回 DUT。必须在 ReadOnly 之前，因为它们要写信号。
     - `ReadOnly()`：把协程挂起，直到当前周期进入 **ReadOnly 阶段**；作用是**保证后续读到的信号已全部沉淀稳定**。
     - `format_cycle`：在 **ReadOnly 阶段**执行；作用是**只读地快照** DUT 内部 PC、寄存器、各状态机值，写进日志。必须等 ReadOnly 才能读到一致快照。
     - `RisingEdge(dut.clk)`：把仿真推进到**下一个上升沿**；作用是**让 DUT 寄存器锁存本拍结果**，进入下一周期，同时让 `cycles += 1`。
   - 步骤 3 会因 ReadOnly 写违规而失败，反向证明「run() 必须在 ReadOnly 之前」。
5. **待本地验证**：异常确切措辞依 cocotb 版本而定，但「ReadOnly 阶段写信号必失败」这一行为稳定。验证完请还原代码。

#### 4.5.5 小练习与答案

**练习 1**：如果删掉循环里的 `program_memory.run()`，第一条指令就取不回来——请解释这会表现在日志的哪里。

> **参考答案**：fetcher 永远卡在 `FETCHING`（拿不到 `mem_read_ready`），scheduler 卡在 `FETCH`，`core_state` 不再前进；日志里会看到连续多个 cycle 的 `Core State: FETCH`、`Fetcher State: FETCHING`，`done` 永不为 1，循环直到 cocotb 超时。

**练习 2**：循环退出条件是 `dut.done.value != 1`。`done` 是哪来的？为什么可以在 `while` 顶部直接读它？

> **参考答案**：`done` 是顶层 `gpu.sv` 输出的内核完成信号，由 dispatcher 在收回所有 block 后拉高（回顾 u2-l2）。它是一个寄存器输出，在 `await RisingEdge` 之后值已稳定，所以在 `while` 顶部（处于新周期的 ReadWrite 阶段）直接读到的就是上一沿锁存的结果，读出来是可靠的。

**练习 3**：为什么 `data_memory.run()` 和 `program_memory.run()` 谁先谁后无所谓？

> **参考答案**：两者写的是完全不同的信号集（`data_mem_*` vs `program_mem_*`），没有数据依赖，且都在同一个 ReadWrite 阶段、仿真时间都不前进，所以次序不影响仿真结果。真正有约束的是它们都必须早于 `ReadOnly()`。

---

## 5. 综合实践

把本讲的「启动序列 + 主循环」整条链路在脑子里跑一遍，并用日志佐证。

**任务**：以 `make test_matadd` 为对象，完成下列追踪。

1. **画启动时序**：在一张纸上，横轴是时钟上升沿序号（0、1、2…），按 [setup.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L7-L37) 的执行顺序标注：
   - 哪个沿上 `reset=1` 被采样；
   - 哪个沿上 DCR 锁存 `thread_count=8`；
   - 从哪个沿起 `start=1` 生效、主循环开始接管。
2. **数周期**：运行 `make test_matadd`，从日志末尾读取 `Completed in {cycles} cycles`，记下数值。
3. **定位「第一拍」**：在日志开头找到 `Cycle 0`，确认此时 `Core State` 大概率是 `IDLE` 或刚进入 `FETCH`，且 program 内存已能回送第一条指令 `MUL R0, %blockIdx, %blockDim`（反汇编见 u5-l1）。
4. **定位「最后一拍」**：找到 `done` 拉高前最后一个 cycle，确认 core 的 scheduler 跑到 `RET` 并进入 `DONE`（回顾 u4-l2）。
5. **解释协同**：用自己的话写出「为什么 `run()` 必须每拍都调、为什么 `format_cycle` 必须在 ReadOnly 之后」。

**预期结果**：你应当能用一张时序图 + 三句话，把「Python 测试台如何一拍一拍地把 tiny-gpu 推过整个内核生命周期」讲清楚。若某一步对不上日志，回到 4.5.2 的阶段模型核对。若本地未装工具链，第 2–4 步标注「待本地验证」，但第 1、5 步可仅凭源码完成。

## 6. 本讲小结

- cocotb 让 **Python 当测试台**、`.sv` 当 DUT，两者经 VPI 桥（`libcocotbvpi_icarus`）通信；`Makefile` 的 `test_%` 用 `MODULE=test.test_$*` 指定要跑的 Python 测试模块，`@cocotb.test()` 装饰的 `async def` 即一个用例。
- `setup()` 的启动序列是固定五步：**建时钟 → 复位脉冲 → load 内存 → 写 DCR → 拉 start**，其中凡涉及 DUT 寄存器（reset、DCR）的都要 `await RisingEdge` 锁存，而 `load` 纯 Python 操作不需等时钟。
- 时钟必须用 `start_soon` **分叉到后台**；`RisingEdge(dut.clk)` 是主协程与硬件同步节拍的唯一手段。
- 一个仿真周期分 **ReadWrite（可读写）→ ReadOnly（只读）** 两阶段；`Memory.run()` 在前者写应答、`format_cycle` 在后者读快照，顺序不可颠倒。
- 主循环 `while dut.done.value != 1` 每拍执行 `run() → ReadOnly → format_cycle → RisingEdge`，既驱动外部内存、又记录轨迹、又推进时钟，直到 dispatcher 拉高 `done`；循环后的 `assert` 才是测试通过/失败的真正判定。
- 日志由 `logger.py` 统一写入带时间戳的 `test/logs/log_*.txt`，内存表（`display`）与执行轨迹（`format_cycle`）都经它落盘，控制台不输出轨迹。

## 7. 下一步学习建议

- 想读懂 `format_cycle` 在日志里到底吐出什么、怎么用它调试内核？继续学 **u6-l2 执行轨迹格式化与阅读**。
- 想自己写一个新内核并接入这套测试框架？继续学 **u6-l3 编写与仿真内核**，那里会以 `test_matadd.py` 为模板教你新增 `test_*.py`。
- 如果对 `Memory.run()` 里多通道信号的拼接切片还不够熟，可回看 **u3-l1 内存模型与外部接口**；对 `done` / scheduler 七阶段的来源，可回看 **u4-l2 Scheduler 核心状态机**。
