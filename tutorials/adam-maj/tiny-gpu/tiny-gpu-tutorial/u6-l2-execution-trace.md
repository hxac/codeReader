# 执行轨迹格式化与阅读

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚一条仿真「执行轨迹」（execution trace）是如何从 DUT 内部信号被翻译成日志里可读文字的。
- 读懂 `format_cycle` 的 **core → thread** 层次遍历结构，知道每个字段（PC、指令、各状态、寄存器）分别来自哪个硬件实例。
- 掌握四张「状态字符串映射表」如何镜像硬件 `localparam`，并能定位一处「定义了却从未被调用」的格式化函数。
- 理解 `format_registers` 为何要做两次「逆序」，以及它如何把颠倒的寄存器数组还原成 `R0…R15` 的阅读顺序。
- 真正用日志去**调试内核**：在某一个 cycle 里锁定一个线程，读出它的 PC / 指令 / 状态 / 寄存器，并解释它此刻在做什么、下一步会发生什么。

## 2. 前置知识

本讲是 [u6-l1 cocotb 仿真测试框架](u6-l1-cocotb-testbench.md) 的直接续篇，也用到 [u5-l1 指令集与编码](u5-l1-isa-encoding.md) 的 ISA 知识。请先确认你已经了解：

- **cocotb 的周期模型**：主循环每拍执行 `data_memory.run()` → `program_memory.run()` → `await ReadOnly()` → `format_cycle(...)` → `await RisingEdge(clk)`。`format_cycle` 跑在 **ReadOnly 阶段**，即「本拍信号已稳定、但还没翻时钟沿」的只读快照点（详见 u6-l1）。
- **DUT 信号句柄**：cocotb 通过 `dut.cores`、`core.core_instance`、`thread.register_instance` 这样的句柄访问 SystemVerilog 里 `generate` 出来的层次化对象。每个信号取值用 `.value`，转成二进制字符串用 `str(...value)`。
- **七阶段流水线与状态机**：scheduler 用 3 位 `core_state` 驱动 `IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE`（详见 [u4-l2](u4-l2-scheduler-fsm.md)）；fetcher 三态、LSU 四态（详见 [u4-l3](u4-l3-fetcher-decoder.md)、[u5-l3](u5-l3-lsu-async-memory.md)）。
- **ISA 编码**：16 位指令切成 `opcode[15:12]/rd[11:8]/rs[7:4]/rt[3:0]`，`format_instruction` 是 decoder 的逆运算（详见 u5-l1）。

一句话定位本讲：u6-l1 讲的是「**怎么把硬件跑起来、每拍驱动什么**」，本讲讲的是「**跑起来之后，日志里那一行行字是怎么生成的、又该怎么读**」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [test/helpers/format.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py) | **轨迹格式化器**。把 DUT 信号翻译成指令助记符、状态名、寄存器表，并按 core→thread 层次写日志。本讲的主角。 |
| [test/helpers/logger.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py) | **日志后端**。`Logger` 类把每条消息追加写进 `test/logs/log_<时间戳>.txt`，是所有轨迹文字的最终归宿。 |
| [src/scheduler.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv) | `core_state` 的 `localparam` 真值表，format.py 的状态映射必须与它保持一致。 |
| [src/fetcher.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv) | `fetcher_state` 三态真值表。 |
| [src/lsu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) | `lsu_state` 四态真值表。 |
| [src/controller.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv) | 内存控制器五态真值表（注意：其 Python 镜像函数当前未被调用）。 |
| [src/registers.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv) | 寄存器堆，解释 `format_registers` 逆序读取的根源。 |
| [test/test_matmul.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py) | 调用点之一：`format_cycle(dut, cycles, thread_id=1)`，只打印 thread 1。综合实践的目标内核。 |
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) | 调用点之二：`format_cycle(dut, cycles)`，打印全部使能线程。 |

---

## 4. 核心概念与源码讲解

### 4.1 日志文件写入机制：logger.py

#### 4.1.1 概念说明

仿真跑起来后，`format_cycle` 会产出大量「Cycle / Core / Thread / 寄存器」文字。这些文字**不会打印到终端**，而是全部落进一个带时间戳的日志文件里。理解这个后端，是你「能找到日志、能读懂顺序」的前提——很多人第一次跑 `make test_matmul` 后盯着空荡荡的终端发呆，就是因为没意识到输出全在文件里。

`logger.py` 只有一个 `Logger` 类和一个模块级单例 `logger`，职责极简：把任意多条消息拼成一行、追加写进文件。

#### 4.1.2 核心流程

```text
import 时 ──► logger = Logger(level="debug")
                      │
                      └─► filename = test/logs/log_<YYYYMMDDHHMMSS>.txt  （此刻固定）
                      └─► level = "debug"

每次调用 logger.debug(...) 或 logger.info(...):
   1. 把所有参数 str() 后用空格拼成一整行
   2. 以 "a"（追加）模式打开 filename
   3. 写入「该行 + "\n"」后立即关闭
```

要点：

- **文件名在 import 时就钉死**：`Logger.__init__` 用 `datetime.now()` 生成时间戳。一旦 `format.py` / `memory.py` 执行了 `from .logger import logger`，这个文件名就不再变了。
- **纯追加、纯文件**：没有任何 `print`，终端看不到轨迹。必须去 `test/logs/` 翻最新的 `log_*.txt`。
- **每次调用都重开文件**：用 `"a"` 模式每次 open/write/close，效率不高但保证了「写入顺序严格等于调用顺序」，且进程中途被杀也不丢已写内容。

#### 4.1.3 源码精读

[test/helpers/logger.py:3-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py#L3-L17) —— `Logger` 类与单例。注意 `debug` 只是 `info` 的别名（当 level 为 debug 时直接转发），所以 `format_cycle` 里大量 `logger.debug(...)` 与 `Memory.display()` 里的 `logger.info(...)` 最终走同一条写文件路径、进同一个文件：

```python
def debug(self, *messages):
    if self.level == "debug":
        self.info(*messages)

def info(self, *messages):
    full_message = ' '.join(str(message) for message in messages)
    with open(self.filename, "a") as log_file:
        log_file.write(full_message + "\n")
```

因此一份日志文件的内容，按写入顺序天然分成三段（与 [test/test_matmul.py:63-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L63-L77) 的调用顺序一一对应）：

| 段落 | 来源 | 内容 |
|---|---|---|
| 头部 | `data_memory.display(12)`（line 63） | `DATA MEMORY` 表格——内核运行**前**的数据内存 |
| 中部 | 主循环里的 `format_cycle(...)`（line 71，每拍一次） | 全部 Cycle 轨迹 |
| 尾部 | `"Completed in N cycles"`（line 76）+ `data_memory.display(12)`（line 77） | 总周期数 + 运行**后**的数据内存 |

#### 4.1.4 代码实践

**目标**：确认「轨迹只进文件、不进终端」，并学会定位最新日志。

**步骤**：

1. 按 [u1-l3](u1-l3-build-and-simulation.md) 装好工具链后运行 `make test_matmul`。
2. 观察终端：你会看到 iverilog/cocotb 的编译与仿真进度，但**看不到**任何 `Cycle 0 / Thread 1 / ...` 文字。
3. 列出 `test/logs/` 目录，找到时间戳最新的 `log_*.txt`（文件名形如 `log_20260728153012.txt`）。
4. 打开它，确认三段结构：开头 `DATA MEMORY`、中间大量 `====...Cycle N...====`、结尾 `Completed in ... cycles` + 第二个 `DATA MEMORY`。

**预期结果**：终端无轨迹文字；最新日志文件包含完整三段。**待本地验证**：具体时间戳与周期数取决于本机运行时刻与内核耗时。

#### 4.1.5 小练习与答案

**练习 1**：为什么连续跑两次 `make test_matmul`，旧的轨迹不会被覆盖？
**答案**：因为文件名带 `datetime.now()` 时间戳，每次 import `logger` 都生成新文件名，两次运行写进不同文件。旧日志会一直堆积，需自己清理。

**练习 2**：如果把 `Logger.__init__` 的默认 `level` 改成 `"info"`，`format_cycle` 产生的轨迹会消失吗？
**答案**：会消失。`format_cycle` 全程用 `logger.debug(...)`，而 `debug` 仅在 `level=="debug"` 时才转发到 `info`（即写文件）。改成 `"info"` 后 `debug` 调用全部变成空操作，只剩 `display()` 和 `Completed...` 这些直接调 `info` 的内容。这是控制轨迹详略的一个隐藏开关。

---

### 4.2 format_instruction：把指令渲染进轨迹（承接 u5-l1）

#### 4.2.1 概念说明

u5-l1 已经讲过 `format_instruction` 是 decoder 的逆运算——把 16 位二进制翻回 `ADD R0, R0, %threadIdx` 这样的助记符。本讲不再重复那张 opcode→字符串的映射表，而是从「**它在轨迹里如何被使用**」这一新角度切入，并指出一个读轨迹时一定会撞见的显示陷阱。

关键认知：在 `format_cycle` 里，`instruction` **每个 core 只读一次**，然后被该 core 下所有线程共享。这正是 SIMD「单指令流」在日志层的体现——同一个 core 的所有线程，某一拍执行的是**同一条**指令。

#### 4.2.2 核心流程

```text
format_cycle 进入一个 core:
   instruction = str(core.core_instance.instruction.value)   # 整个 core 只取一次
   for thread in core.core_instance.threads:
       ...
       logger.debug("Instruction:", format_instruction(instruction))   # 每个线程都打印同一条
```

`format_instruction(instruction)` 内部把 16 字符二进制串切成字段并查表：

1. `opcode = instruction[0:4]`、`rd = instruction[4:8]`、`rs = instruction[8:12]`、`rt = instruction[12:16]`（注意是对**字符串**切片，MSB 在前）。
2. 用 `format_register` 把寄存器号翻译成 `R0`/`%blockIdx` 等名字。
3. 按 opcode 拼 f-string；对 `BRnzp` 额外拼 nzp 位与立即数。

#### 4.2.3 源码精读

`instruction` 只读一次、全 core 共享：

[test/helpers/format.py:107](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L107) —— `instruction = str(core.core_instance.instruction.value)`，在 `for thread` 循环**之外**。

[test/helpers/format.py:127](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L127) —— `logger.debug("Instruction:", format_instruction(instruction))`。

**显示陷阱：BRnzp 的 nzp 段总是空白。** 看 [test/helpers/format.py:19-21](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L19-L21)：

```python
n = "N" if instruction[4] == 1 else ""
z = "Z" if instruction[5] == 1 else ""
p = "P" if instruction[6] == 1 else ""
```

`instruction` 是字符串，`instruction[4]` 是一个**单字符字符串**（`"0"` 或 `"1"`），而代码拿它和**整数** `1` 比较。在 Python 里 `"1" == 1` 恒为 `False`，于是 `n/z/p` 三个变量永远是空串 `""`。结果是一条 `BRnzp` 在日志里永远渲染成：

```text
Instruction: BRnzp , #12
```

注意 `BRnzp` 和逗号之间是空的。这只是**显示层**的瑕疵，硬件里真正的 nzp 判定发生在 `pc.sv` / NZP 寄存器（见 [u5-l4](u5-l4-registers-pc.md)），不受影响。但你读轨迹时不要被这个空字段误导成「分支条件丢失」。

#### 4.2.4 代码实践

**目标**：在真实日志里确认「同一 core 的所有线程共享同一条 Instruction」，并观察 BRnzp 的空 nzp 段。

**步骤**：

1. 跑 `make test_matadd`（matadd 用 `format_cycle(dut, cycles)`，打印**全部**使能线程，便于对比）。
2. 打开最新日志，定位一个 `Core State` 不是 `IDLE` 的 Cycle。
3. 在该 Cycle 内，对比 Core 下 Thread 0 / Thread 1 / ... 的 `Instruction:` 行。
4. 再搜 `BRnzp`（matadd 无分支，可改用 matmul），观察其 nzp 段。

**预期结果**：同一 Cycle 内所有线程的 `Instruction` 完全相同（单指令流）；`BRnzp` 行形如 `BRnzp , #<imm>`，nzp 段为空。**待本地验证**：具体 imm 值与所在 Cycle 编号。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `instruction` 在 `format_cycle` 里放在线程循环之外读取，而不是每个线程各读一次？
**答案**：因为一个 core 只有**一个** fetcher/decoder，产出一套共享的 `instruction` 与 `decoded_*` 控制信号，全 core 广播给所有线程（见 [u4-l1](u4-l1-core-anatomy.md)）。每个线程读到的必然相同，放外面既省事也准确反映了 SIMD 的「单指令流」结构。

**练习 2**：若想让 BRnzp 正确显示 nzp（例如显示成 `BRnzp NZ, #12`），最小改动是什么？
**答案**：把 `instruction[4] == 1` 改成 `instruction[4] == "1"`（同理 5、6 位），即拿字符串和字符串比。这是纯 Python 字符串比较的修正，不碰硬件。

---

### 4.3 状态字符串映射：把硬件 localparam 翻成人话

#### 4.3.1 概念说明

`core_state`、`fetcher_state`、`lsu_state` 这些信号在硬件里是一串二进制（如 `101`），日志里直接打 `101` 你根本看不出是 `EXECUTE` 还是 `WAIT`。于是 format.py 给每类状态配了一张「二进制串 → 英文名」的字典，让你一眼看懂当前停在流水线的哪一拍、LSU 是否在等内存。

这四张表本质上是各硬件模块 `localparam` 的**手工副本**——这是理解与维护的一个关键点：Python 侧和 SystemVerilog 侧各存一份真理，改了一边忘了另一边就会对不上。

#### 4.3.2 核心流程

四个函数结构完全一样：接收状态的二进制**字符串**，查字典返回名字。调用点统一形如 `format_core_state(str(core.core_instance.core_state.value))`——先用 `str()` 把信号值变成 `"101"` 这样的串再去查。

| Python 函数 | 硬件真值表来源 | 映射 |
|---|---|---|
| `format_core_state` | scheduler.sv 的 `localparam` | 000/001/010/011/100/101/110/111 → IDLE/FETCH/DECODE/REQUEST/WAIT/EXECUTE/UPDATE/DONE |
| `format_fetcher_state` | fetcher.sv 的 `localparam` | 000/001/010 → IDLE/FETCHING/FETCHED |
| `format_lsu_state` | lsu.sv 的 `localparam` | 00/01/10/11 → IDLE/REQUESTING/WAITING/DONE |
| `format_memory_controller_state` | controller.sv 的 `localparam` | 000/010/011/100/101 → IDLE/READ_WAITING/WRITE_WAITING/READ_RELAYING/WRITE_RELAYING |

#### 4.3.3 源码精读

`core_state` 映射，与 scheduler 真值表逐位对应：

[test/helpers/format.py:48-59](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L48-L59) —— 对照 [src/scheduler.sv:40-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L40-L47) 的 `localparam IDLE=3'b000 ... DONE=3'b111`，二者完全一致。

LSU 与 fetcher 同理：[test/helpers/format.py:61-76](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L61-L76) 分别镜像 [src/fetcher.sv:28-30](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L28-L30) 与 [src/lsu.sv:38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L38)。

**重要发现：`format_memory_controller_state` 是「死代码」。** 它确实定义在 [test/helpers/format.py:78-86](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L78-L86)，且严格镜像了 [src/controller.sv:38-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L38-L42) 的五态——但用 grep 全仓搜索后可以确认：**它从未被任何地方调用**（`format_cycle` 只打印了 core/fetcher/lsu 三种状态，没有 controller）。也就是说，当前轨迹里看不到内存控制器处于 `READ_WAITING` 还是 `READ_RELAYING`。这是读源码时必须诚实标注的一点：函数存在 ≠ 函数生效。

#### 4.3.4 代码实践

**目标**：用 grep 验证「死代码」结论，并体会「Python 与 SV 双份真值表」的维护风险。

**步骤**：

1. 在仓库根目录执行（只读搜索，不改任何文件）：
   ```bash
   grep -rn "format_memory_controller_state" test/
   ```
2. 观察输出：只有 `test/helpers/format.py:78` 的 `def` 一行，没有任何调用点。
3. 再对比 `format_core_state` 的搜索结果，会看到 `format.py` 里既有定义也有调用。

**预期结果**：`format_memory_controller_state` 仅 1 处命中（定义）；`format_core_state` 多处命中（定义 + `format_cycle` 内调用）。结论：控制器状态虽备好翻译器，却没接进轨迹。

#### 4.3.5 小练习与答案

**练习 1**：如果有人把 scheduler.sv 里的 `EXECUTE` 从 `3'b101` 改成 `3'b100`，format.py 不改，仿真时会怎样？
**答案**：硬件 `core_state` 实际会出现 `100`，但 Python 字典里 `100` 映射的是 `WAIT`，于是日志会把 `EXECUTE` 拍误显示成 `WAIT`。更糟的是若出现字典里没有的键（比如新增状态）会直接 `KeyError` 崩掉仿真。这正是「双份真值表」的维护代价。

**练习 2**：为什么 `format_core_state` 等函数都要求传入**字符串**而不是 int？
**答案**：因为调用方用 `str(core_state.value)` 把信号转成了 `"101"` 这样的二进制串，字典的 key 也写成 `"000"`/`"101"` 等字符串。若传 int `5`，字典查 `"5"` 查不到就 KeyError。这是 cocotb 信号值天然是二进制字符串导致的约定。

---

### 4.4 format_registers 的逆序处理

#### 4.4.1 概念说明

寄存器堆有 16 个寄存器（R0–R12 自由，R13–R15 是 `%blockIdx/%blockDim/%threadIdx`）。日志里若直接按 cocotb 遍历顺序打印，你会发现 `%threadIdx` 跑到了最前面、`R0` 排到最后——读起来很别扭。`format_registers` 的全部存在意义，就是把这股「倒着来的数据」纠正成 `R0, R1, ..., %threadIdx` 的自然阅读顺序。

理解这段代码的关键是一个反直觉的事实：**cocotb 遍历 `registers[15:0]` 时，数据是倒序到达的**——列表第 0 个元素其实是硬件里的 `registers[15]`。

#### 4.4.2 核心流程

数据流向：

```text
硬件:  registers[0]=R0, registers[1]=R1, ..., registers[15]=%threadIdx
            │  cocotb 遍历 unpacked array（声明为 [15:0]）
            ▼
Python 列表（倒序）:  list[0]=registers[15], list[1]=registers[14], ..., list[15]=registers[0]
            │  format_registers 处理
            ▼
最终字符串（正序）:   "R0 = .., R1 = .., ..., %blockIdx = .., %blockDim = .., %threadIdx = .."
```

纠正靠两步：

1. **重新标号**：列表下标 \(i\) 对应的真实寄存器号是 \( \text{reg\_idx} = 15 - i \)。即 `list[0]` 标成 `R15`、`list[15]` 标成 `R0`。
2. **整体反转**：把标好号的字符串列表 `.reverse()`，顺序从 `[R15, R14, ..., R0]` 翻成 `[R0, R1, ..., R15]`。

#### 4.4.3 源码精读

[test/helpers/format.py:88-95](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L88-L95) —— 注意第 92 行那条注释 `# Register data is provided in reverse order`，作者自己也标注了数据是反的：

```python
for i, reg_value in enumerate(registers):
    decimal_value = int(reg_value, 2)
    reg_idx = 15 - i                 # 数据是倒序的，用 15-i 还原真实寄存器号
    formatted_registers.append(f"{format_register(reg_idx)} = {decimal_value}")
formatted_registers.reverse()        # 再整体反转，让显示从 R0 升序到 R15
return ', '.join(formatted_registers)
```

数据来源（调用方）：

[test/helpers/format.py:131](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L131) —— `format_registers([str(item.value) for item in thread.register_instance.registers])`，遍历该线程的寄存器数组、逐个转成二进制串。底层 `registers[15:0]` 的声明见 [src/registers.sv:45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L45)（`reg [7:0] registers[15:0];`）。

> 旁注：`int(reg_value, 2)` 假设每个值都是合法二进制串。复位瞬间的 `'x'`/`'z'` 会让它抛 `ValueError`，所以轨迹只在稳态、使能线程上打印才安全。

#### 4.4.4 代码实践

**目标**：动手推演「两次逆序」如何还原顺序，建立对这段代码的直觉。

**步骤**：

1. 假设某线程寄存器值（硬件视角）：`R0=0, R1=1, R2=2, ... R15=9`（只看前几个）。
2. 模拟 cocotb 倒序遍历：Python 列表 = `[reg15, reg14, ..., reg0]` = `[9, 8, ..., 0]`（仅示意，值为对应寄存器号的镜像，便于跟踪）。
3. 套用 `reg_idx = 15 - i`：`list[0]` → 标 `R15`、值 9；`list[1]` → 标 `R14`、值 8；……；`list[15]` → 标 `R0`、值 0。
4. 执行 `.reverse()`：得到 `[R0=0, R1=1, ..., R15=9]`。
5. 与硬件真值对比，确认完全一致。

**预期结果**：经过 `reg_idx=15-i` 标号 + `.reverse()` 后，显示顺序与硬件 `registers[0..15]` 完全一致。这两个动作缺一不可：只标号不反转，会得到 `R15, R14, ..., R0` 的降序；只反转不标号，会把 `list[0]` 错认成 `R0`。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能直接 `enumerate` 出来就当 `R0, R1, ...` 打印？
**答案**：因为 cocotb 遍历 `registers[15:0]` 时，第 0 个元素是 `registers[15]`（即 `%threadIdx`）。直接当 `R0` 打印会把 `%threadIdx` 的值标成 `R0`，全表错位。`reg_idx=15-i` 就是用来纠正这个偏移的。

**练习 2**：如果把 `.reverse()` 那一行删掉，日志里寄存器表的顺序会变成什么样？
**答案**：会变成 `%threadIdx, %blockDim, %blockIdx, R12, ..., R0` 的**降序**（因为标号阶段产出的是 `[R15, R14, ..., R0]`）。值本身没错（标号已修正），只是阅读顺序从升序变成了降序，读起来别扭但不算 bug。

---

### 4.5 format_cycle：core → thread 的层次遍历与选择性输出

#### 4.5.1 概念说明

前面三节讲的都是「原子翻译器」：单个指令、单个状态、单个寄存器表怎么变可读。`format_cycle` 是把这些原子翻译器**组装起来**的总调度——它按 `cycle → core → thread → 字段` 的层次遍历 DUT，决定打印哪些核、哪些线程、哪些字段。理解这个层次，你就能在日志里精准定位「第 N 拍、第 K 个线程、正在做什么」。

#### 4.5.2 核心流程

```text
format_cycle(dut, cycle_id, thread_id=None):
  写 Cycle 横幅
  for core in dut.cores:                         # 遍历所有核
      if 该核未被分配线程: continue                # 跳过空闲核
      写 Core 横幅
      instruction = core 的当前指令（只读一次）
      for thread in core.threads:                # 遍历核内线程
          if thread 未使能: continue              # 跳过被 enable 门控冻结的线程
          idx = blockIdx * blockDim + threadIdx  # 算全局线程号
          if thread_id 不是 None 且 idx != thread_id: continue   # 按 thread_id 过滤
          读 rs/rt/alu_out/lsu_out/constant
          写 Thread 横幅
          写 PC / Instruction / Core State / Fetcher State / LSU State / Registers / RS,RT
          按 reg_input_mux 二选一写: ALU Out 或 LSU Out 或 Constant
      写 Core Done
```

两个过滤维度决定了日志体量：

- **核级过滤**：`thread_count <= core.i * THREADS_PER_BLOCK` 的核直接跳过（注释自承「Not exactly accurate, but good enough for now」）。
- **线程级过滤**：`thread_id` 参数。`format_cycle(dut, cycles)`（matadd）打印全部使能线程；`format_cycle(dut, cycles, thread_id=1)`（matmul）只打印全局线程号 `idx==1` 的那一个——所以 matmul 的日志比 matadd 小得多。

全局线程号公式是 CUDA 经典映射：

\[
\text{idx} = \text{blockIdx} \times \text{blockDim} + \text{threadIdx}
\]

#### 4.5.3 源码精读

层次遍历骨架：

[test/helpers/format.py:100-108](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L100-L108) —— 外层 `for core in dut.cores`，核内 `for thread in core.core_instance.threads`。`instruction` 在 line 107 读一次、放循环外。

核级与线程级过滤：

[test/helpers/format.py:101-103](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L101-L103) —— 跳过未分配线程的核（注释承认是近似）。

[test/helpers/format.py:109](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L109) —— `if int(thread.i.value) < int(str(core.core_instance.thread_count.value), 2)`，跳过被 `enable` 门控冻结的线程（硬件侧见 [u4-l1](u4-l1-core-anatomy.md) 的 `enable = (i < thread_count)`）。

[test/helpers/format.py:110-113](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L110-L113) —— 算全局 `idx`。`thread_id` 过滤在 [line 123](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L123)：`if (thread_id is None or thread_id == idx)`。

逐字段输出（核心区）：

[test/helpers/format.py:124-132](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L124-L132) —— 依次写 PC、Instruction、Core State、Fetcher State、LSU State、Registers、RS/RT。

**选择性输出：根据 `reg_input_mux` 只打印「即将写回的那个值」**：

[test/helpers/format.py:134-139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L134-L139)：

```python
if reg_input_mux == 0:   logger.debug("ALU Out:", alu_out)      # 算术类
if reg_input_mux == 1:   logger.debug("LSU Out:", lsu_out)      # LDR
if reg_input_mux == 2:   logger.debug("Constant:", constant)    # CONST
```

这里的 `0/1/2` 正是 [src/registers.sv:40-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L40-L42) 的 `ARITHMETIC=2'b00 / MEMORY=2'b01 / CONSTANT=2'b10`。所以这一行告诉你「**下一个 UPDATE 拍，什么值会被写进寄存器**」——调试时极有价值。

> **读轨迹陷阱**：这段判断只看 `reg_input_mux`，不看 `reg_write_enable`。而 decoder 对 `STR/CMP/BRnzp/NOP/RET` 这类**不写寄存器**的指令会让 `reg_input_mux` 保持默认 `0`，于是它们也会打印一行 `ALU Out`——但这个值并不会真正写回（对 CMP 它其实是 N/Z/P 编码位，对 STR/BR 则是无意义残留）。所以看到 `ALU Out` 时，必须结合 `Instruction` 判断它是否真有意义。

每核收尾：

[test/helpers/format.py:141](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L141) —— `logger.debug("Core Done:", str(core.core_instance.done.value))`，在线程循环之外，所以每个（被打印的）核每拍出现一次。

**字段速查表**（读轨迹时按此对照）：

| 日志字段 | 来源实例 | 层次 | 反映什么 |
|---|---|---|---|
| `PC` | `core.core_instance.current_pc` | core 共享 | 当前指令地址（单指令流） |
| `Instruction` | `core.core_instance.instruction` | core 共享 | 经 `format_instruction` 反汇编 |
| `Core State` | `core.core_instance.core_state` | core 共享 | 七阶段流水线当前拍 |
| `Fetcher State` | `core.core_instance.fetcher_state` | core 共享 | 取指 FSM |
| `LSU State` | `thread.lsu_instance.lsu_state` | 每线程 | 该线程访存 FSM |
| `Registers` | `thread.register_instance.registers` | 每线程 | 该线程全套寄存器 |
| `RS, RT` | `thread.register_instance.rs/rt` | 每线程 | 本指令的源操作数 |
| `ALU Out/LSU Out/Constant` | `thread.alu/lsu` 或 core decoder | 混合 | 即将写回的值 |
| `Core Done` | `core.core_instance.done` | core 共享 | 该 block 是否跑完 RET |

#### 4.5.4 代码实践

**目标**：用 `thread_id` 过滤的差异，亲见 matmul 日志为何「只有 Thread 1」。

**步骤**：

1. 分别跑 `make test_matadd` 与 `make test_matmul`，各打开最新日志。
2. 在 matadd 日志的某个 Cycle 内数一数 Thread 横幅数量。
3. 在 matmul 日志的某个 Cycle 内数一数 Thread 横幅数量。
4. 核对调用点：[test/test_matadd.py:55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L55) 的 `format_cycle(dut, cycles)` vs [test/test_matmul.py:71](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L71) 的 `format_cycle(dut, cycles, thread_id=1)`。

**预期结果**：matadd 每个 Cycle 有多个 Thread（使能线程数=4）；matmul 每个 Cycle 只有 `Thread 1`。差异完全由 `thread_id` 参数控制。**待本地验证**：matadd 实际打印的线程数取决于 `threads` 配置。

#### 4.5.5 小练习与答案

**练习 1**：matmul 里 `thread_id=1` 的「1」指的是硬件 `THREAD_ID`，还是全局 `idx`？
**答案**：是全局 `idx = blockIdx * blockDim + threadIdx`。见 [line 110-113](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L110-L113) 与 [line 123](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L123)。matmul 单 block、4 线程，所以 `idx==1` 恰好对应 `threadIdx==1`；但多 block 时二者就不同了。

**练习 2**：为什么 `PC`、`Core State` 这些字段在「core 共享」列，而 `Registers`、`LSU State` 在「每线程」列？
**答案**：因为 core 只有一套 fetcher/decoder/scheduler（单指令流），产出的 `current_pc/core_state/instruction` 全核广播共享；而 ALU/LSU/registers/PC 是每线程各一份（多数据）。日志字段的共享/私有属性，正是 SIMD 架构在文本层的直接投影（见 [u4-l1](u4-l1-core-anatomy.md)）。

---

## 5. 综合实践

把本讲全部知识串起来：**运行 `make test_matmul`，在日志里锁定 thread 1 执行某条具体指令的那一拍，读出它的全貌并解释它在做什么**。这是本讲规格要求的代码实践任务。

### 背景

[test/test_matmul.py:12-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L12-L42) 是 2×2 矩阵乘内核，数据内存里 A=`[1,2,3,4]`、B=`[1,2,3,4]`（见 [line 46-49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L46-L49)），共 4 个线程，每线程算一个输出元素。由于测试用 `format_cycle(dut, cycles, thread_id=1)`，日志里**每个 Cycle 只会出现 Thread 1**。

线程 1（`idx=1`）算的是 `C[0,1]`。它的前两条指令（PC=0、PC=1）是确定性的，不依赖任何循环或内存延迟，最适合做轨迹阅读练习：

- PC=0：`MUL R0, %blockIdx, %blockDim` —— R0 = 0 × 4 = 0（线程 1 在 block 0）。
- PC=1：`ADD R0, R0, %threadIdx` —— R0 = 0 + 1 = 1，即全局线程号 `i = 1`。

### 操作步骤

1. 运行 `make test_matmul`，打开 `test/logs/` 下最新的 `log_*.txt`。
2. 确认每个 Cycle 的 Thread 横幅都是 `Thread 1`（验证 `thread_id` 过滤生效）。
3. 在日志里搜索字符串 `Instruction: ADD R0, R0, %threadIdx`（即 PC=1 那条）。它会出现在连续的好几个 Cycle 里（同一条指令要走过 FETCH→…→UPDATE 多拍）。
4. 在这些 Cycle 中，找到 `Core State: EXECUTE` 的那一拍。
5. 在该 Cycle 的 `Thread 1` 块里，读出并报告以下全部字段：
   - `PC` —— 应为 `1`。
   - `Instruction` —— `ADD R0, R0, %threadIdx`。
   - `Core State` —— `EXECUTE`。
   - `Fetcher State` —— `FETCHED`（取指已完成、尚未回 IDLE）。
   - `LSU State` —— `IDLE`（ADD 不访存）。
   - `Registers` —— 注意此刻 `R0` 仍为 `0`（写回要等到下一拍 UPDATE）。
   - `RS = ?, RT = ?` —— `RS` 是 R0 的当前值 `0`，`RT` 是 `%threadIdx` 的值 `1`。
   - 选择性输出那一行 —— `reg_input_mux==0`，应显示 `ALU Out: 1`（即 0+1 的结果）。
6. 翻到紧接着的下一个 Cycle，确认 `Core State: UPDATE`，且 `Registers` 里 `R0` 已变成 `1`。

### 需要观察的现象与预期结果

- 该 Cycle thread 1 正处于 `ADD R0, R0, %threadIdx` 的 **EXECUTE** 拍：ALU 已算出 `0 + 1 = 1`（见 `ALU Out`），但尚未写回，所以 `R0` 此拍仍是 `0`、`RS=0`、`RT=1`。
- 下一拍（UPDATE）`R0` 更新为 `1`，即 thread 1 拿到了自己的全局编号 `i=1`。
- 这条指令不触访存，所以 `LSU State` 全程 `IDLE`，scheduler 不会在 `WAIT` 多停留。

**待本地验证**：具体的 Cycle 编号、`Fetcher State` 在 EXECUTE 拍的确切取值、以及 `ALU Out` 的打印数值，需以本机日志为准——因为 FETCH/WAIT 的拍数依赖程序内存的响应时序，无法凭源码静态算出。但「PC=1、指令为 ADD、EXECUTE 拍 R0 仍为 0、ALU Out 为 1、UPDATE 拍 R0 变 1」这一结构结论，由内核语义与 `%threadIdx=1` 直接决定，是确定的。

### 进阶（可选）

把目标换成 `Instruction: LDR R10, R10`（加载矩阵 A 的元素）。找到 `Core State: REQUEST` 或 `WAIT`、`LSU State: REQUESTING`/`WAITING` 的拍，再找到 `LSU State: DONE` 的拍，观察 `LSU Out` 何时出现 R10 的加载值。这能让你直观看到一条异步访存指令如何跨多拍、如何被 scheduler 的 `WAIT` 拖住——把 [u5-l3](u5-l3-lsu-async-memory.md) 的 LSU 状态机和本讲的轨迹阅读对上号。

---

## 6. 本讲小结

- 所有轨迹文字**只进文件**（`test/logs/log_<时间戳>.txt`），终端看不到；文件名在 import 时钉死，旧日志不覆盖、会堆积。
- `format_instruction` 在 `format_cycle` 里**每个 core 只读一次**、全 core 共享，体现 SIMD 单指令流；其 `nzp` 段因 `instruction[4] == 1`（字符串与整数比较）恒为空，是纯显示瑕疵。
- 四张状态映射表是各硬件 `localparam` 的手工副本；其中 `format_memory_controller_state` 虽定义却**从未被调用**，当前轨迹看不到控制器状态。
- `format_registers` 靠 `reg_idx = 15 - i` 标号 + `.reverse()` 两步，把 cocotb 倒序遍历的寄存器数组还原成 `R0…R15` 升序。
- `format_cycle` 按 `cycle → core → thread → 字段` 四层遍历，用「核级 / 线程级 / thread_id」三重过滤控制日志体量，并用 `reg_input_mux` 选择性打印「即将写回的值」。
- 读轨迹时字段天然分成「core 共享（PC/Core State/Instruction/Fetcher State）」与「每线程（Registers/LSU State/RS/RT）」两组，正是 SIMD 架构的文本投影。

## 7. 下一步学习建议

- 下一讲 [u6-l3 编写与仿真内核](u6-l3-writing-kernels.md) 会让你**自己写一个新内核**并接入 `test_*.py`。届时你会反向用到本讲知识：内核跑不对时，靠 `format_cycle` 的轨迹定位「是取指错、译码错、还是写回错」。
- 想深入「为什么 WAIT 会拖好几拍」「controller 如何中继」的读者，可回看 [u5-l3 LSU 异步访存](u5-l3-lsu-async-memory.md) 与 [u3-l2 内存控制器](u3-l2-memory-controller.md)，并把它们的 FSM 对照本讲日志里 `LSU State` / 控制器状态（若你把 `format_memory_controller_state` 接进 `format_cycle`，就能在轨迹里直接看到它）。
- 进阶练习：尝试在本讲指出的两处「读轨迹陷阱」（BRnzp 空 nzp、不写寄存器指令误显 `ALU Out`）做最小修复，体会「Python 测试台与 SV 硬件各存一份真值表」带来的维护成本——这正是 [u7 架构取舍与扩展方向](u7-l1-scheduling-tradeoffs.md) 会系统讨论的话题。
