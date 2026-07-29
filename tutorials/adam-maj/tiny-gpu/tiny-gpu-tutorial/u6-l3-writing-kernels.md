# 编写与仿真内核

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚在 tiny-gpu 上跑一个内核需要准备「**三样东西**」：程序内存（指令的二进制）、数据内存（初始数据）、线程数，并理解 README 里 `.threads` / `.data` 伪指令与 `test_*.py` 里 Python 列表的对应关系。
- 逐行读懂 `matadd`（矩阵加法）内核：它如何用 `%blockIdx/%blockDim/%threadIdx` 算出全局线程号 `i`，如何用 `baseA/baseB/baseC` 三段布局访问数据内存，以及 `LDR/STR` 如何与地址寄存器配合。
- 读懂 `matmul`（矩阵乘法）内核里的**循环与分支**：`CMP` + `BRn` 如何实现 `loop while k < N` 的计数循环，以及它为何能在「PC 收敛」假设下正常工作。
- 独立把一段汇编**手工编码**成 16 位二进制，并仿照 `test_matadd.py` 写出一个新的 `test_*.py`，跑通并用 `assert` 校验结果。
- 理解数据内存的 `baseA/baseB/baseC` 布局约定，以及仿真结束后如何用 `data_memory.memory[i + baseC]` 取回结果做断言。

## 2. 前置知识

本讲是 [u6-l1 cocotb 仿真测试框架](u6-l1-cocotb-testbench.md) 与 [u6-l2 执行轨迹格式化与阅读](u6-l2-execution-trace.md) 的应用篇，也用到 [u5-l1 指令集与编码](u5-l1-isa-encoding.md) 的 ISA 知识。请先确认你已经了解：

- **启动序列与主循环**：`setup()` 依次建时钟、复位、`load` 程序/数据内存、写 DCR（锁存 `thread_count`）、拉 `start`；主循环 `while dut.done.value != 1` 每拍执行 `data_memory.run()` → `program_memory.run()` → `await ReadOnly()` → `format_cycle(...)` → `await RisingEdge(clk)`（详见 u6-l1）。
- **ISA 编码**：16 位定长指令，位域 `[15:12]=opcode / [11:8]=rd / [7:4]=rs / [3:0]=rt`；`CONST` 的立即数复用 `[7:0]`，`BRnzp` 的 nzp 掩码复用 `[11:9]`、跳转目标复用 `[7:0]`（详见 u5-l1）。
- **CMP / NZP / BRnzp 的真实语义**：`CMP` 把 `alu_out[2:0]` 编码成 `{(rs-rt>0), (rs-rt==0), (rs-rt<0)}`；由于 `rs/rt` 是 8 位无符号数，`rs-rt` 不够减时回绕成大正数，导致 bit[2]（名义上的「大于/P」）实际等价于「**不相等**」，bit[1]（「相等/Z」）表示「相等」，bit[0]（「小于/N」）结构性恒为 0（详见 [u5-l2 ALU 与比较/NZP](u5-l2-alu-nzp.md)）。
- **`done` 信号**：内核每跑到一条 `RET` 指令，对应线程/core 标记完成；dispatcher 收回所有 block 后顶层 `done` 拉高，主循环退出（详见 [u2-l2](u2-l2-kernel-launch-dcr-dispatcher.md)）。

一句话定位本讲：u6-l1 讲「**怎么驱动硬件**」、u6-l2 讲「**怎么读轨迹**」，本讲讲「**怎么往硬件里塞一段自己写的代码并验证它对不对**」——这是把「读硬件」彻底转化为「用硬件」的最后一公里。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) | **矩阵加法内核**的完整测试。是本讲「逐行解析」与「写新内核」的主要范本——标量乘法实践几乎照抄它的结构。 |
| [test/test_matmul.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py) | **矩阵乘法内核**的完整测试。含 `CMP`/`BRn` 循环与分支，是讲解循环控制的范本。 |
| [test/helpers/setup.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py) | `setup()` 启动序列。三要素（program/data/threads）在这里被灌进 DUT。 |
| [README.md](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md) | 给出了 `.threads` / `.data` 伪指令写法的 `matadd.asm` / `matmul.asm`，是「人读的汇编」与「机读的二进制」对照的权威来源。 |
| [src/decoder.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | opcode `localparam` 真值表，手工编码时查 opcode 的唯一事实来源。 |
| [src/alu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv) | `CMP` 模式下 `alu_out[2:0]` 的编码逻辑，解释 `BRn` 为何等价于「不相等则跳」。 |
| [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) | `BRnzp` 的 `(nzp & decoded_nzp) != 0` 判定，决定循环何时退出。 |

---

## 4. 核心概念与源码讲解

### 4.1 内核的三要素：程序、数据、线程

#### 4.1.1 概念说明

在 tiny-gpu 上跑一个内核，本质是回答三个问题：**跑什么代码？处理什么数据？开多少线程？** 这就是「内核三要素」。无论内核多简单或多复杂，测试文件永远围绕这三样东西组织。

一个容易混淆的关键点：README 里展示的 `matadd.asm` 用了 `.threads 8` 和 `.data 0 1 2 3 ...` 这样的**伪指令**，看起来像有汇编器。**但项目里并没有汇编器**——这些 `.threads` / `.data` 只是写给人看的注释式语法。真正被仿真器吃进去的，是 `test_*.py` 里的 **Python 列表**：汇编指令已经被作者**手工编码**成了 16 位整数列表，数据也被写成 Python 列表。理解「`.asm` 是给人读的、`.py` 才是给机器跑的」，是你自己写内核的第一道坎。

三要素的对应关系如下：

| README 汇编伪指令 | `test_*.py` 里的 Python | 最终去向 | 由谁灌入 |
|---|---|---|---|
| `.threads 8` | `threads = 8` | 设备控制寄存器 DCR → `thread_count` | `setup()` 写 DCR |
| `.data 0 1 2 3 ...` | `data = [0,1,2,3,...]` | 数据内存（8 位地址/8 位数据/4 通道） | `data_memory.load(data)` |
| 汇编指令正文 | `program = [0b...., 0b...., ...]` | 程序内存（8 位地址/16 位指令/1 通道，只读） | `program_memory.load(program)` |

#### 4.1.2 核心流程

一个内核从「写出来」到「跑通并校验」，流程固定为五步：

```text
1. 写汇编（纸上或注释里）        ──►  设计算法、安排寄存器、规划数据内存布局
2. 手工编码成 16 位二进制        ──►  查 decoder.sv 的 opcode 表，逐条切成 opcode/rd/rs/rt
3. 写 test_<name>.py             ──►  把 program / data / threads 三要素填进 Python 列表
4. await setup(...) + 主循环     ──►  setup 灌入三要素并拉 start；主循环每拍驱动内存直到 done
5. assert 校验 data_memory.memory ──► 从结果地址取回输出，与期望值逐项比对
```

其中第 2 步是 tiny-gpu 区别于「真实 GPU + CUDA 工具链」的地方：没有编译器帮你，**每条指令的 16 位都要你自己拼**。这也正是它能用来学习「指令到底长什么样」的价值所在。

#### 4.1.3 源码精读

三要素在 `test_matadd.py` 里的体现，是本讲后续所有内容的锚点。

**要素一：程序内存（指令的二进制列表）**

[test/test_matadd.py:11-26](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L11-L26) —— 先建一个 16 位宽、1 通道、只读的 `program` 内存模型，再把 13 条手工编码的指令塞进 `program` 列表。注意每行末尾的注释就是「人读的汇编」，二进制与注释一一对应：

```python
program_memory = Memory(dut=dut, addr_bits=8, data_bits=16, channels=1, name="program")
program = [
    0b0101000011011110, # MUL R0, %blockIdx, %blockDim
    ...
    0b1111000000000000, # RET
]
```

**要素二：数据内存（初始数据）**

[test/test_matadd.py:29-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L29-L33) —— 8 位宽、4 通道、可读写的 `data` 内存。`data` 列表的下标就是地址：`data[0..7]` 是矩阵 A，`data[8..15]` 是矩阵 B：

```python
data_memory = Memory(dut=dut, addr_bits=8, data_bits=8, channels=4, name="data")
data = [
    0, 1, 2, 3, 4, 5, 6, 7, # Matrix A (1 x 8)
    0, 1, 2, 3, 4, 5, 6, 7  # Matrix B (1 x 8)
]
```

**要素三：线程数**

[test/test_matadd.py:36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L36) —— `threads = 8`，对应 README 里的 `.threads 8`。它会被写进 DCR，决定 dispatcher 切几个 block、派给几个 core。

**三要素如何被灌入 DUT**

[test/helpers/setup.py:7-37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L7-L37) —— `setup()` 接收全部三要素，依次执行：

```python
program_memory.load(program)                      # 灌程序内存（要素一）
data_memory.load(data)                            # 灌数据内存（要素二）
dut.device_control_write_enable.value = 1
dut.device_control_data.value = threads           # 写 DCR（要素三）
await RisingEdge(dut.clk)
dut.device_control_write_enable.value = 0
dut.start.value = 1                               # 点火
```

`load` 是纯 Python 操作（往列表里按下标写值），不需要等时钟；而写 DCR 涉及硬件寄存器，必须 `await RisingEdge(clk)` 让非阻塞赋值在下一个时钟沿生效（详见 u6-l1）。

#### 4.1.4 代码实践

**目标**：确认「`.asm` 是注释、`.py` 才是真身」，并验证三要素的一一对应。

**步骤**：

1. 打开 [README.md:236-260](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L236-L260) 的 `matadd.asm`，记下 `.threads` 和两条 `.data`。
2. 打开 [test/test_matadd.py:12-36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L12-L36)，逐条核对：`program` 列表每行注释的汇编是否与 `matadd.asm` 正文一致；`data` 列表是否与两条 `.data` 一致；`threads = 8` 是否等于 `.threads 8`。
3. 随便挑一条，例如 `matadd.asm` 的 `CONST R2, #8`，去 `program` 里找注释相同的那行 `0b1001001000001000`，手工拆位验证（opcode `1001`=CONST，rd `0010`=R2，imm `00001000`=8）。

**预期结果**：三要素完全一一对应；`CONST R2, #8` 的 16 位正好拆成 `1001 0010 0000 1000`。这印证了「没有汇编器、靠手工编码」的事实。**待本地验证**：无（纯静态对照）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 里要写 `.threads` / `.data` 这种伪指令，而测试里却用 Python 列表？
**答案**：因为项目没有汇编器。`.threads` / `.data` 是写给人看的「文档式汇编」，帮助读者理解内核意图；真正喂给仿真器的是 `test_*.py` 里作者手工编码后的 `program` / `data` / `threads`。这也意味着：**改了 `.asm` 注释不会改变行为，必须同步改 `program` 列表里的二进制**。

**练习 2**：如果把 `test_matadd.py` 里的 `threads = 8` 改成 `threads = 4`，但 `program` 和 `data` 都不动，会发生什么？
**答案**：DCR 只启动 4 个线程（线程号 `i = 0,1,2,3`），于是只有 `C[0..3]`（地址 16..19）会被算出来并写回；`C[4..7]`（地址 20..23）保持初始值 0。最终断言只覆盖前 4 项时不会报错，但内存里后 4 项是错的——这正说明 `threads` 控制了「多少数据被并行处理」。

---

### 4.2 matadd 内核逐行解析

#### 4.2.1 概念说明

`matadd` 把两个 1×8 矩阵相加：`C[i] = A[i] + B[i]`，每个 `i` 由一个独立线程计算。它是 tiny-gpu 里最完整的「入门范本」，一次性展示了 SIMD 编程的三件法宝：用只读寄存器算线程号、用 `baseA/baseB/baseC` 三段布局定位数据、用 `LDR/STR` 做异步访存。

理解这段内核的核心，是抓住**一条主线**：每个线程先算出自己在结果矩阵里的全局下标 `i`，再用 `i` 去三个基地址段里分别读 A、读 B、写 C。所有线程跑的是**同一段代码**（单指令流），却因为 `%threadIdx` 不同而处理**不同的数据**（多数据）——这就是 SIMD。

#### 4.2.2 核心流程

`matadd` 单个线程（以线程号 `i` 为例）的执行逻辑：

```text
; 第 1 步：算全局线程号 i
i = blockIdx * blockDim + threadIdx

; 第 2 步：确立三段基地址
baseA = 0     ; A 起始于地址 0
baseB = 8     ; B 起始于地址 8
baseC = 16    ; C 起始于地址 16

; 第 3 步：读 A[i]、读 B[i]
a = data[baseA + i]
b = data[baseB + i]

; 第 4 步：算并写 C[i]
data[baseC + i] = a + b

; 第 5 步：结束
RET
```

数据内存布局示意（地址 → 内容）：

```text
地址:  0   1   2   3   4   5   6   7  │  8   9  10  11  12  13  14  15  │ 16  17  18 ... 23
内容:  A0  A1  A2  A3  A4  A5  A6  A7 │  B0  B1  B2  B3  B4  B5  B6  B7 │ C0  C1  C2 ... C7
       └─────── baseA=0 ─────────────┘   └──────── baseB=8 ────────────┘   └─ baseC=16 ─┘
```

每个线程 `i` 只动 `baseA+i`、`baseB+i`、`baseC+i` 三个地址，互不干扰，因此 8 个线程可以完全并行。

#### 4.2.3 源码精读

逐行看 [test/test_matadd.py:12-26](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L12-L26) 的 `program`（左边的二进制是机读、右边注释是人读，二者必须一致）：

**第 1 步：算线程号 `i`**

[test/test_matadd.py:13-14](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L13-L14) —— CUDA 经典的全局线程号映射，用到了三个只读寄存器（`%blockIdx=13`、`%blockDim=14`、`%threadIdx=15`，详见 [u5-l4](u5-l4-registers-pc.md)）：

```python
0b0101000011011110, # MUL R0, %blockIdx, %blockDim   ; R0 = blockIdx * blockDim
0b0011000000001111, # ADD R0, R0, %threadIdx         ; i = blockIdx * blockDim + threadIdx
```

matadd 只开 1 个 block（8 线程），所以 `blockIdx=0`、`blockDim=8`，`i = 0*8 + threadIdx = threadIdx`，即线程号 0..7。验证第一条编码：`0101`(MUL) `0000`(R0) `1101`(=%blockIdx) `1110`(=%blockDim) —— 位域切分与注释完全吻合。

**第 2 步：三段基地址**

[test/test_matadd.py:15-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L15-L17) —— 用 `CONST` 把三个基地址装进寄存器。`CONST` 的立即数复用 `[7:0]` 字段：

```python
0b1001000100000000, # CONST R1, #0    ; baseA
0b1001001000001000, # CONST R2, #8    ; baseB   (imm = 00001000 = 8)
0b1001001100010000, # CONST R3, #16   ; baseC   (imm = 00010000 = 16)
```

**第 3 步：读 A[i]、读 B[i]**

[test/test_matadd.py:18-21](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L18-L21) —— 先用 `ADD` 算出绝对地址放进 `R4`，再用 `LDR R4, R4` 从该地址取数**覆盖回 R4**（地址用完即弃，寄存器复用）：

```python
0b0011010000010000, # ADD R4, R1, R0   ; addr(A[i]) = baseA + i
0b0111010001000000, # LDR R4, R4       ; R4 = A[i]   (LDR rd, rs: 取 mem[rs] 写入 rd)
0b0011010100100000, # ADD R5, R2, R0   ; addr(B[i]) = baseB + i
0b0111010101010000, # LDR R5, R5       ; R5 = B[i]
```

注意 `LDR rd, rs` 的语义：`rd` 在 `[11:8]`、`rs`（地址源）在 `[7:4]`。`LDR R4, R4` 编码为 `0111`(LDR) `0100`(R4) `0100`(R4) `0000`。这两条 `LDR` 会触发 LSU 的异步访存状态机，scheduler 在 `WAIT` 阶段等内存往返（详见 [u5-l3](u5-l3-lsu-async-memory.md)）。

**第 4 步：算 C[i] 并写回**

[test/test_matadd.py:22-24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L22-L24) —— `ADD` 算出和，再用 `STR` 写到 `baseC+i`。注意 `STR rs, rt` 的语义与 `LDR` 相反：`rs` 是**地址**（`[7:4]`）、`rt` 是**要写的数据**（`[3:0]`），即「把 `rt` 写进 `mem[rs]`」：

```python
0b0011011001000101, # ADD R6, R4, R5   ; C[i] = A[i] + B[i]
0b0011011100110000, # ADD R7, R3, R0   ; addr(C[i]) = baseC + i
0b1000000001110110, # STR R7, R6       ; mem[baseC + i] = C[i]   (STR rs, rt: mem[rs] <- rt)
```

验证 `STR R7, R6` 编码：`1000`(STR) `0000`(rd 未用) `0111`(R7=地址) `0110`(R6=数据) —— 地址在 `rs` 段、数据在 `rt` 段，与注释「store C[i]」一致。

**第 5 步：结束**

[test/test_matadd.py:25](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L25) —— `0b1111000000000000` 是 `RET`（opcode `1111`）。每个线程跑到 `RET` 即标记完成；所有线程结束后 dispatcher 拉高 `done`，主循环退出。

#### 4.2.4 代码实践

**目标**：把 matadd 的「读-改-写」模式内化，亲手验证两条 `LDR` 的地址计算。

**步骤**：

1. 假设线程 `i = 5`（即 `%threadIdx = 5`）。先在纸上算：`baseA + i = 0 + 5 = 5`，`baseB + i = 8 + 5 = 13`，`baseC + i = 16 + 5 = 21`。
2. 查 [test/test_matadd.py:30-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L30-L33) 的 `data`：`A[5] = data[5] = 5`，`B[5] = data[13] = 5`，所以 `C[5] = 10`，应写入 `data_memory.memory[21]`。
3. 运行 `make test_matadd`，打开 `test/logs/` 下最新日志尾部的 `DATA MEMORY` 表，定位地址 21，确认其值为 `10`。
4. 再翻到日志头部的 `DATA MEMORY`（运行前的快照），确认地址 21 此刻还是 `0`（结果区初始为空）。

**预期结果**：运行后地址 21 = 10，运行前地址 21 = 0；地址 5 与地址 13 都是 5（A、B 的输入）。**待本地验证**：日志里地址 21 的确切数值以本机为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LDR R4, R4` 可以让 `R4`「先当地址、后当数据」，这样写安全吗？
**答案**：安全，而且是有意为之。`LDR` 在 `REQUEST` 阶段先读出 `R4`（当时是地址 `baseA+i`）送给 LSU 作访存地址；等到 `UPDATE` 阶段才把取回的数据写回 `R4`。两个阶段隔了几拍，读地址发生在前、写数据发生在后，不冲突。这种「地址寄存器用完即覆盖」是寄存器紧张时的常用技巧（tiny-gpu 只有 13 个自由寄存器）。

**练习 2**：如果把第 3 步的 `ADD R4, R1, R0`（算 A 的地址）误写成 `ADD R4, R2, R0`（用了 baseB），程序还会正常跑完吗？结果会怎样？
**答案**：会正常跑完（不会崩），但结果错：线程会去 `baseB + i` 读「A[i]」，相当于把 B 当成了 A。因为 `data` 里 A、B 数值恰好相同（都是 0..7），所以这道题的结果居然不变——这是个隐蔽的「巧合性正确」陷阱。调试时若输入数据对称，这类地址错配极难发现，这正是 u6-l2 执行轨迹的价值所在。

---

### 4.3 matmul 内核的循环与分支

#### 4.3.1 概念说明

`matmul` 把两个 2×2 矩阵相乘，每个线程算结果矩阵的一个元素 `C[row,col]`，它等于 A 的某一行与 B 的某一列的点积。与 matadd「一读一加一写」不同，matmul 需要一个**累加循环**：沿内维 `N=2` 把 `A[row,k]*B[k,col]` 逐项相加。这就第一次用到了控制流指令 `CMP` + `BRn`。

本节的重点不是矩阵乘的数学，而是**这个循环靠什么停在正确的次数**。答案藏在一个反直觉的硬件细节里：`BRn`（字面意思是「负则跳」）实际依赖的是 `CMP` 的「不相等」位，而它之所以表现为「`k < N` 则继续循环」，靠的是无符号减法的回绕特性。理解这一点，你才算真正读懂了这段内核。

#### 4.3.2 核心流程

`matmul` 单个线程（以全局线程号 `i` 为例）的逻辑：

```text
; 算位置
i = blockIdx * blockDim + threadIdx
N = 2
row = i // N          ; 整除
col = i % N           ; 取余
acc = 0
k = 0

LOOP:                  ; 循环 N 次
  a = data[baseA + row*N + k]
  b = data[baseB + k*N + col]
  acc = acc + a*b
  k = k + 1
  CMP k, N            ; 比较 k 与 N
  BRn LOOP            ; 若 k != N（实际等价于 k < N）则回到 LOOP

data[baseC + i] = acc
RET
```

四个线程（`i = 0,1,2,3`）算出的 `(row,col)` 分别是 `(0,0),(0,1),(1,0),(1,1)`，正好覆盖 2×2 结果矩阵的全部元素。

**`CMP` + `BRn` 为何等价于「`k < N` 则循环」**（承接 u5-l2）：

设 `CMP R9, R2` 即 `rs = k`、`rt = N = 2`。ALU 在比较模式下产出 [src/alu.sv:39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L39)：

\[
\text{alu\_out}[2{:}0] = \{\,(rs-rt>0),\ (rs-rt==0),\ (rs-rt<0)\,\}
\]

由于 `rs/rt` 是 8 位无符号数，`rs-rt` 在 `rs<rt` 时回绕成大正数。于是：

| k（rs） | rs-rt（8位） | (rs-rt>0) bit[2] | (rs-rt==0) bit[1] | 含义 |
|---|---|---|---|---|
| 0 | 0−2 = 254（回绕） | 1 | 0 | 不相等 |
| 1 | 1−2 = 255（回绕） | 1 | 0 | 不相等 |
| 2 | 2−2 = 0 | 0 | 1 | 相等 |

即 bit[2] 实际等价于「**k ≠ N**」，bit[1] 等价于「**k = N**」。而 `BRn` 的 nzp 掩码 `decoded_nzp = instruction[11:9] = 3'b100`（见 4.3.3 验证），它只检查 bit[2]。`pc.sv` 的判定 [src/pc.sv:44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L44) 是 `if ((nzp & decoded_nzp) != 0)` —— 于是「bit[2]=1 即 k≠N 则跳回 LOOP，bit[2]=0 即 k=N 则不跳、落到 PC+1 退出循环」。因为 `k` 单调递增到 `N`，`k≠N` 在循环区间内就等价于 `k<N`，所以注释「loop while k < N」在行为上完全正确。

#### 4.3.3 源码精读

逐段看 [test/test_matmul.py:12-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L12-L42)。

**算位置：`row` 与 `col`**

[test/test_matmul.py:13-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L13-L22) —— 先算 `i`（同 matadd），再用 `DIV` 算 `row = i // N`，用 `MUL`+`SUB` 算 `col = i % N`（硬件无取余指令，用 `i - (i//N)*N` 实现）：

```python
0b0101000011011110, # MUL R0, %blockIdx, %blockDim
0b0011000000001111, # ADD R0, R0, %threadIdx         ; i
...
0b0110011000000010, # DIV R6, R0, R2                 ; row = i // N
0b0101011101100010, # MUL R7, R6, R2                 ; R7 = row*N
0b0100011100000111, # SUB R7, R0, R7                 ; col = i - row*N = i % N
```

**累加器与循环变量初始化**

[test/test_matmul.py:23-24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L23-L24) —— `acc = 0`（R8）、`k = 0`（R9）。`LOOP` 标号是注释，对应程序地址 **12**（数一下 `program` 列表的下标：第 0..11 条之后，第 12 条正是 `MUL R10, R6, R2`）。

**循环体：取 A、取 B、累加、自增**

[test/test_matmul.py:26-36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L26-L36) —— 地址公式 `addr(A) = row*N + k + baseA`、`addr(B) = k*N + col + baseB`，每次循环累乘累加后 `k += 1`：

```python
# LOOP:                                 ; ← 程序地址 12
0b0101101001100010, #   MUL R10, R6, R2
0b0011101010101001, #   ADD R10, R10, R9
0b0011101010100011, #   ADD R10, R10, R3   ; addr(A) = row*N + k + baseA
0b0111101010100000, #   LDR R10, R10       ; A[row,k]
0b0101101110010010, #   MUL R11, R9, R2
0b0011101110110111, #   ADD R11, R11, R7
0b0011101110110100, #   ADD R11, R11, R4   ; addr(B) = k*N + col + baseB
0b0111101110110000, #   LDR R11, R11       ; B[k,col]
0b0101110010101011, #   MUL R12, R10, R11
0b0011100010001100, #   ADD R8, R8, R12    ; acc += A*B
0b0011100110010001, #   ADD R9, R9, R1     ; k += 1
```

**循环控制：`CMP` + `BRn`**

[test/test_matmul.py:37-38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L37-L38) —— 这是本节核心：

```python
0b0010000010010010, #   CMP R9, R2        ; 比较 k 与 N
0b0001100000001100, #   BRn LOOP          ; loop while k < N
```

手工拆 `BRn` 这条 `0b0001100000001100`（= 6156 = `0x180C`）验证 4.3.2 的论断。把 16 位按 4 位一组切开（最高位在最左）：

```text
位:  [15:12] [11:8] [7:4] [3:0]
值:   0001    1000   0000  1100
     └opcode┘└─rd──┘└─rs─┘└─rt─┘
```

- `opcode = [15:12] = 0001` → `BRnzp` ✓（查 [src/decoder.sv:35](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L35)）
- `nzp 掩码 = instruction[11:9]`，取 rd 字段 `1000` 的最高 3 位，即 bit 11/10/9 = `1、0、0` → `decoded_nzp = 3'b100`，只看 bit[2] ✓
- `跳转目标 = instruction[7:0] = 00001100 = 12` → 跳回程序地址 12（`LOOP`）✓

所以这条 `BRn` 的精确含义是：「若 NZP 寄存器的 bit[2] 为 1，则把 PC 设为 12」。而 bit[2] 由上一条 `CMP R9, R2` 写入，等价于 `k ≠ N`。循环体每跑一遍 `k` 加 1，直到 `k == N` 时 bit[2]=0、不跳、落到第 39 行退出循环。

**写回结果并返回**

[test/test_matmul.py:39-41](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L39-L41) —— 退出循环后把 `acc` 写到 `baseC + i`，然后 `RET`：

```python
0b0011100101010000, # ADD R9, R5, R0      ; addr(C) = baseC + i  （R9 被复用作地址寄存器）
0b1000000010011000, # STR R9, R8          ; mem[baseC + i] = acc
0b1111000000000000  # RET
```

注意退出循环后 `R9`（原是循环变量 `k`）被**复用**为结果地址寄存器——循环变量已无用，省下一个寄存器。

> **为何 matmul 不会触发「分支分歧」**：四个线程的 `k` 都从 0 走到 `N=2`，每个线程在同一个 PC 上做同样的 `CMP/BRn` 判定、走同样的跳转，所有线程的 PC 始终相同。这正是 scheduler 「PC 收敛假设」能成立的前提（详见 [u7-l1](u7-l1-scheduling-tradeoffs.md)）。README 也明确指出：matmul 的所有分支都会收敛，所以它能在当前实现上正确运行。

#### 4.3.4 代码实践

**目标**：亲手追踪线程 1（`i=1`，算 `C[0,1]`）的循环过程，验证 `CMP/BRn` 的退出时机。

**步骤**：

1. 对 `i=1`：`row = 1//2 = 0`、`col = 1%2 = 1`。所以它算 `C[0,1] = A[0,0]*B[0,1] + A[0,1]*B[1,1]`。
2. 查 [test/test_matmul.py:46-49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L46-L49) 的 `data`：`A[0,0]=data[0]=1`、`A[0,1]=data[1]=2`；`B[0,1]=data[5]=2`、`B[1,1]=data[7]=4`。
3. 纸上算：`k=0` 时 `acc = 1*2 = 2`；`k=1` 时 `acc = 2 + 2*4 = 10`；`k=2` 时 `CMP 2,2` 相等 → 退出。所以 `C[0,1] = 10`，写入 `baseC + i = 8 + 1 = 9`。
4. 运行 `make test_matmul`，打开最新日志尾部的 `DATA MEMORY`，定位地址 9，确认值为 `10`。

**预期结果**：地址 9 = 10。**待本地验证**：确切数值以本机日志为准。

#### 4.3.5 小练习与答案

**练习 1**：如果矩阵内维 `N` 从 2 改成 3（即 `CONST R2, #3`），但数据仍是 2×2，循环会怎样？
**答案**：循环会跑 `k=0,1,2` 共 3 次（因为退出条件变成 `k==3`）。但数据内存里并没有第 3 行/列的有效数据，`A[row,2]`、`B[2,col]` 会读到地址越界的初始值 0，相当于多算了一项 `A[..]*0` 或读到无关地址的值，结果大概率出错。这说明 `N` 必须与实际数据布局严格一致。

**练习 2**：把循环控制从 `CMP R9, R2` + `BRn LOOP` 改成 `CMP R9, R2` + `BRz LOOP`（`BRz` = 相等则跳，掩码 `010`），循环行为会变成什么？
**答案**：`BRz` 检查 bit[1]（相等）。于是只在 `k == N` 时才跳回 LOOP，而 `k < N` 时不跳、直接退出。结果循环体一次都不执行（首次 `k=0`，`0 != 2`，不跳 → 退出），`acc` 恒为 0。这反向印证了 `BRn`（检查 bit[2]=不相等）才是「继续循环」的正确条件。

---

### 4.4 数据内存布局与结果校验

#### 4.4.1 概念说明

内核算完之后，结果落在数据内存里——但仿真器不会主动告诉你「对不对」。校验靠两件事：一是 `data_memory.display(rows)` 把内存表格写进日志供人眼复查，二是 Python 的 `assert` 从 `data_memory.memory` 里按地址取回结果、与期望值逐项比对。`baseA/baseB/baseC` 三段布局就是为了让「输入区」和「输出区」不互相覆盖，使校验有明确的地址可查。

一个关键事实：`data_memory.memory` 是一个普通 Python 列表（[test/helpers/memory.py:9](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L9) 初始化为 `2**addr_bits` 个 0），内核的每一次 `STR` 都由 `Memory.run()` 直接写进这个列表（[test/helpers/memory.py:62-64](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L62-L64)）。所以仿真结束后，`data_memory.memory` 就是「最终数据内存」的 Python 镜像，断言直接读它即可。

#### 4.4.2 核心流程

校验的标准写法：

```text
1. 用 Python 算出 expected_results（用纯 Python 重算一遍期望值，独立于内核）
2. for i, expected in enumerate(expected_results):
       result = data_memory.memory[i + baseC]      # 从结果区按地址取回
       assert result == expected                    # 逐项比对
3. 若全部 assert 通过 → 内核正确；任一不符 → 抛异常并报告差在哪一项
```

「用 Python 重算期望值」是关键：它独立于被测的硬件内核，作为参照系。两套独立实现算出相同结果，才能说内核大概率正确。

#### 4.4.3 源码精读

**matadd 的布局与校验**

[test/test_matadd.py:47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L47) 与 [test/test_matadd.py:61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L61) —— 主循环前后各调一次 `data_memory.display(24)`，分别记录运行前/后的数据内存（写进日志的头部与尾部，详见 u6-l2）：

```python
data_memory.display(24)   # 运行前
... 主循环 ...
data_memory.display(24)   # 运行后
```

[test/test_matadd.py:63-65](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L63-L65) —— 校验。`baseC = 16`，所以结果从地址 16 开始；期望值用 Python 的 `zip` 重算 `a + b`：

```python
expected_results = [a + b for a, b in zip(data[0:8], data[8:16])]
for i, expected in enumerate(expected_results):
    result = data_memory.memory[i + 16]              # baseC = 16
    assert result == expected, f"Result mismatch at index {i}: expected {expected}, got {result}"
```

**matmul 的布局与校验**

[test/test_matmul.py:46-49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L46-L49) —— 数据布局：A 占地址 0..3，B 占地址 4..7，`baseC = 8`：

```python
data = [
    1, 2, 3, 4, # Matrix A (2 x 2)   ← 地址 0..3
    1, 2, 3, 4, # Matrix B (2 x 2)   ← 地址 4..7
]
```

[test/test_matmul.py:81-91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L81-L91) —— 期望值用嵌套列表手算点积，再从 `baseC + i` 取回比对：

```python
matrix_a = [data[0:2], data[2:4]]
matrix_b = [data[4:6], data[6:8]]
expected_results = [
    matrix_a[0][0]*matrix_b[0][0] + matrix_a[0][1]*matrix_b[1][0],  # C[0,0]
    ...  # C[0,1], C[1,0], C[1,1]
]
for i, expected in enumerate(expected_results):
    result = data_memory.memory[i + 8]                # baseC = 8
    assert result == expected, f"Result mismatch at index {i}: ..."
```

> 注意：源码 [line 90](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L90) 的注释写的是「Results start at address 9」，但代码实际用 `i + 8`（`i` 从 0 到 3，即地址 8..11）。`baseC` 在内核里被设为 8（[test/test_matmul.py:19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L19) `CONST R5, #8`），所以结果实际从地址 8 开始——注释的「9」是一处小的笔误，代码以 `i + 8` 为准。读源码时要相信代码、核对注释。

**`display` 与 `assert` 的分工**

`display` 只负责「写日志给人看」（[test/helpers/memory.py:79-99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L79-L99)），它不抛异常、不影响成败判定；真正决定「测试通过/失败」的只有 `assert`。两者互补：`display` 帮你定位「哪一项错」，`assert` 帮机器判定「对不对」。

#### 4.4.4 代码实践

**目标**：用 `display` 与 `assert` 双重确认 matmul 的结果区布局。

**步骤**：

1. 运行 `make test_matmul`，打开最新日志。
2. 看日志**尾部**的 `DATA MEMORY`，读出地址 8、9、10、11 的值（应分别为 `C[0,0], C[0,1], C[1,0], C[1,1]`）。
3. 用 Python 手算期望：`C = [[1*1+2*3, 1*2+2*4],[3*1+4*3, 3*2+4*4]] = [[7,10],[15,22]]`。
4. 核对日志里地址 8..11 是否为 `7,10,15,22`。
5. 把 `data` 里某个输入值（如 `data[0]` 从 1 改成 5）再跑一次，观察 `assert` 是否如预期报错并指出错在哪一项。

**预期结果**：地址 8..11 = `7,10,15,22`；改输入后 `assert` 抛出 `Result mismatch at index 0: ...`。**待本地验证**：断言报错的具体文案与是否改 `data` 有关。

#### 4.4.5 小练习与答案

**练习 1**：为什么校验要从 `data_memory.memory[i + baseC]` 取结果，而不是从 `data[i + baseC]`？
**答案**：因为 `data` 是**初始**数据（运行前灌入的输入），内核的 `STR` 只会修改 `data_memory.memory`（由 `Memory.run()` 写入），不会回头改 Python 的 `data` 列表。所以 `data` 永远是输入快照，`data_memory.memory` 才是运行后的最终状态。两者在 `baseC` 区一开始都是 0，但只有后者会被内核更新。

**练习 2**：如果内核忘了写 `STR`（结果没存回内存），`assert` 会怎样？
**答案**：结果区 `data_memory.memory[baseC + i]` 保持初始值 0，`assert result == expected` 中 `expected` 通常非 0，于是断言失败、抛出 `Result mismatch`。这正是 `assert` 的价值——它不会因为「内核跑完了、没报错」就判定成功，而是检查**输出数据**是否真的正确。

---

## 5. 综合实践

把本讲全部知识串起来：**自己写一个「标量乘法」内核并接入仿真**。这是本讲规格要求的代码实践任务。

### 任务

把 data 内存 **0 号地址**的值乘以 2，存到 **1 号地址**，用 **1 个线程**。即：`mem[1] = mem[0] * 2`。要求手工编码指令、仿照 `test_matadd.py` 写一个 `test/test_scalar.py` 并运行验证。

### 第 1 步：设计汇编（安排寄存器）

不涉及线程号（只用固定地址 0 和 1），所以不需要 `%blockIdx/%threadIdx`。寄存器分配如下：

```text
CONST R1, #0      ; R1 = 0（源数据地址）
LDR R2, R1        ; R2 = mem[0]
CONST R3, #2      ; R3 = 2（乘数）
MUL R4, R2, R3    ; R4 = mem[0] * 2
CONST R5, #1      ; R5 = 1（目标地址）
STR R5, R4        ; mem[1] = R4
RET               ; 结束
```

寄存器从 R1 起用（R0 留空也无妨），全部落在自由寄存器 R0–R12 内。

### 第 2 步：手工编码成 16 位二进制

查 [src/decoder.sv:34-44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44) 的 opcode 表，按 `[15:12]opcode / [11:8]rd / [7:4]rs / [3:0]rt` 逐条切位。每条都与 matadd 里同类指令的编码模式一一对照，确保无误：

| 汇编 | opcode | rd | rs/imm-hi | rt/imm-lo | 16 位编码 | 对照 matadd 同类指令 |
|---|---|---|---|---|---|---|
| `CONST R1, #0` | 1001 | 0001 | 0000 | 0000 | `1001000100000000` | [L15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L15) `CONST R1,#0` 完全相同 |
| `LDR R2, R1` | 0111 | 0010 | 0001 | 0000 | `0111001000010000` | [L19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L19) `LDR R4,R4` 同型 |
| `CONST R3, #2` | 1001 | 0011 | 0000 | 0010 | `1001001100000010` | [L16](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L16) `CONST R2,#8` 同型 |
| `MUL R4, R2, R3` | 0101 | 0100 | 0010 | 0011 | `0101010000100011` | [L13](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L13) `MUL` 同型 |
| `CONST R5, #1` | 1001 | 0101 | 0000 | 0001 | `1001010100000001` | [L15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L15) `CONST` 同型 |
| `STR R5, R4` | 1000 | 0000 | 0101 | 0100 | `1000000001010100` | [L24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L24) `STR R7,R6` 同型 |
| `RET` | 1111 | 0000 | 0000 | 0000 | `1111000000000000` | [L25](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L25) `RET` 完全相同 |

> 特别核对 `STR R5, R4`：语义是「把 R4 的值写进 `mem[R5]`」，即 `rs=R5`（地址，放 `[7:4]`）、`rt=R4`（数据，放 `[3:0]`）。编码 `1000 0000 0101 0100`：`1000`(STR) `0000`(rd) `0101`(R5=地址) `0100`(R4=数据)。与 matadd 的 `STR R7,R6`（`1000 0000 0111 0110`）同构，只是寄存器号不同。

### 第 3 步：写 test/test_scalar.py

照抄 `test_matadd.py` 的结构，只改 `program` / `data` / `threads` 三要素与断言。**示例代码**如下（这是本讲新写的测试，非项目原有文件）：

```python
import cocotb
from cocotb.triggers import RisingEdge
from .helpers.setup import setup
from .helpers.memory import Memory
from .helpers.format import format_cycle
from .helpers.logger import logger

@cocotb.test()
async def test_scalar(dut):
    # Program Memory
    program_memory = Memory(dut=dut, addr_bits=8, data_bits=16, channels=1, name="program")
    program = [
        0b1001000100000000, # CONST R1, #0   ; 源数据地址
        0b0111001000010000, # LDR R2, R1     ; R2 = mem[0]
        0b1001001100000010, # CONST R3, #2   ; 乘数
        0b0101010000100011, # MUL R4, R2, R3 ; R4 = mem[0] * 2
        0b1001010100000001, # CONST R5, #1   ; 目标地址
        0b1000000001010100, # STR R5, R4     ; mem[1] = R4
        0b1111000000000000, # RET           ; 结束
    ]

    # Data Memory
    data_memory = Memory(dut=dut, addr_bits=8, data_bits=8, channels=4, name="data")
    data = [3]  # mem[0] = 3

    # Device Control
    threads = 1

    await setup(
        dut=dut,
        program_memory=program_memory,
        program=program,
        data_memory=data_memory,
        data=data,
        threads=threads
    )

    data_memory.display(4)

    cycles = 0
    while dut.done.value != 1:
        data_memory.run()
        program_memory.run()

        await cocotb.triggers.ReadOnly()
        format_cycle(dut, cycles)

        await RisingEdge(dut.clk)
        cycles += 1

    logger.info(f"Completed in {cycles} cycles")
    data_memory.display(4)

    expected = data[0] * 2          # 3 * 2 = 6
    result = data_memory.memory[1]  # mem[1]
    assert result == expected, f"Expected {expected}, got {result}"
```

### 第 4 步：运行与验证

按 [u1-l3](u1-l3-build-and-simulation.md) 装好工具链，并确保仓库根目录已有 `build/` 目录（README 要求先 `mkdir build`）。Makefile 的 `test_%` 模式规则（[Makefile:5-8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L5-L8)）会把 `test_scalar` 自动映射到 `MODULE=test.test_scalar`，所以文件名必须是 `test/test_scalar.py`：

```bash
make test_scalar
```

### 需要观察的现象与预期结果

- 仿真正常退出（主循环因 `done` 拉高而结束），`assert` 不抛异常即代表通过。
- 打开 `test/logs/` 下最新的 `log_*.txt`，尾部 `DATA MEMORY` 表里：地址 0 = `3`（输入，未被修改），地址 1 = `6`（输出 = 3×2）。
- 日志中部的执行轨迹应能看到该单线程依次执行 `CONST → LDR → CONST → MUL → CONST → STR → RET`，其中 `LDR` 与 `STR` 会在 `LSU State` 上经历 `REQUESTING → WAITING → DONE` 的异步访存过程（对照 [u5-l3](u5-l3-lsu-async-memory.md)）。

**待本地验证**：`Completed in N cycles` 的具体周期数、日志里地址 0/1 的显示值，需以本机运行结果为准。但「`mem[1] == mem[0]*2`、断言通过」这一结论由内核语义直接决定，是确定的。

### 进阶（可选）

- 把 `data = [3]` 改成 `data = [10]`，确认地址 1 变成 `20`，体会数据与代码分离的好处。
- 把内核里的乘数 `CONST R3, #2` 改成 `CONST R3, #3`（编码 `1001001100000011`），确认地址 1 变成 `9`（3×3），验证你已能独立改编码。
- 若断言失败，按 [u6-l2](u6-l2-execution-trace.md) 的方法在日志里锁定那条指令的 EXECUTE/UPDATE 拍，对照 `ALU Out`、`LSU Out`、寄存器值，定位是编码错、地址错还是写回错。

---

## 6. 本讲小结

- 在 tiny-gpu 上跑内核需要**三要素**：程序内存（手工编码的 16 位指令列表）、数据内存（初始数据列表）、线程数（写进 DCR）。README 的 `.threads` / `.data` 只是写给人看的伪指令，真正生效的是 `test_*.py` 里的 Python 列表。
- `matadd` 的主线是「算线程号 `i` → 用 `baseA/baseB/baseC` 三段布局 → `LDR` 读、`ADD` 算、`STR` 写」；`LDR rd, rs` 取 `mem[rs]` 写 `rd`，`STR rs, rt` 把 `rt` 写进 `mem[rs]`，两者地址/数据字段方向相反。
- `matmul` 用 `CMP` + `BRn` 实现累加循环；由于 8 位无符号减法回绕，`CMP` 的 bit[2] 实际等价于「不相等」，`BRn`（掩码 `100`）据此在 `k ≠ N` 时跳回，行为上等价于「`k < N` 则循环」，`k == N` 时退出。
- 数据内存按 `baseA/baseB/baseC` 分段，输入区与输出区不重叠；校验靠 `data_memory.memory[i + baseC]` 取回结果，与 Python 独立重算的期望值 `assert` 比对——`display` 只写日志给人看，`assert` 才决定成败。
- 写新内核的五步固定流程：写汇编 → 手工编码 → 填 `test_*.py` 三要素 → `setup` + 主循环 → `assert` 校验；没有汇编器，每条指令的 16 位都要自己按 `opcode/rd/rs/rt` 切出来，并可用同类已有指令的编码交叉验证。
- 仿真器不会主动判定对错，`assert` 是唯一闸门；改输入后若 `assert` 精确报出「哪一项错」，说明测试链条（编码→灌入→运行→取回→比对）已全部打通。

## 7. 下一步学习建议

- 至此 Unit 6（仿真、内核与轨迹分析）的三讲已闭环：u6-l1 驱动硬件、u6-l2 读轨迹、本讲写内核。你可以尝试写更复杂的内核（如向量内积、一维卷积）来综合运用 `LDR/STR` 与 `CMP/BRn`。
- 进入 [Unit 7 架构取舍与扩展方向](u7-l1-scheduling-tradeoffs.md) 后，你会发现本讲的 matmul 之所以能跑，是因为它「所有线程的分支都收敛」、PC 始终一致。u7-l1 会系统讨论一旦线程间发生**分支分歧**（不同线程走不同 PC），当前 scheduler 的「PC 收敛假设」就会崩溃——并引导你设计一个会让 tiny-gpu 算错的分歧场景。
- 想深入「结果写回的最后一拍」「`STR` 如何穿过控制器落到外部内存」的读者，可回看 [u5-l4 寄存器堆与程序计数器](u5-l4-registers-pc.md) 与 [u3-l2 内存控制器](u3-l2-memory-controller.md)，把本讲内核里每条 `LDR/STR` 的访存路径在硬件层补全。
