# 内存模型与外部接口

## 1. 本讲目标

本讲是内存子系统的第一讲，只聚焦一件事：**tiny-gpu 与「外部内存」之间的约定**——内存长什么样、有多少通道、用什么信号握手，以及在仿真里这段外部内存是谁来扮演的。

学完本讲，你应当能够：

- 说出 data 内存和 program 内存的地址位宽、数据位宽、通道数与容量。
- 读懂 `gpu.sv` 顶层的多通道 `valid/ready/data` 内存握手端口，并区分哪些是 GPU 输出、哪些是 GPU 输入。
- 解释 `test/helpers/memory.py` 里 `Memory.run()` 为什么要把信号「逐通道、逐位」拆开再拼回去，以及它是如何把响应驱动回 DUT 的。
- 理解 `Memory` 类的 `load()`/`display()` 在仿真流程里的调试作用。

本讲**不**进入内存控制器内部（那是 u3-l2 的主题），也**不**进入 LSU/取指器内部；我们只站在「GPU 的边界」上看内存这一侧。

## 2. 前置知识

在开始之前，你需要先建立下面几个直觉（若不熟，可先回看 u1-l2「仓库结构与源码地图」与 u2-l1「gpu.sv 顶层架构」）：

- **硬件描述语言里的「端口」**：`module` 像一块带引脚的芯片，端口就是引脚。`input wire` 是输入引脚（信号从外面流进来），`output wire` 是输出引脚（信号从内部流出去）。
- **打包（packed）数组与未打包（unpacked）数组**：`reg [7:0] x` 是 8 位打包在一起的一根线；`reg [7:0] x [3:0]` 是「4 个元素，每个 8 位」的未打包数组，可以理解成 4 根独立的 8 位线。多通道内存接口大量使用未打包数组——每个通道一根。
- **valid/ready 握手**：请求方拉高 `valid` 表示「我要说话」，应答方拉高 `ready` 表示「我听到了」。两边同时为高，一次事务才算成立。本讲里 GPU 是请求方，外部内存是应答方。
- **外部内存（external memory）**：tiny-gpu 不自己造存储单元，它假设程序和数据都放在「外面的」异步 DRAM 里。在仿真时，这段外部内存由 Python 的 `Memory` 类扮演。
- **cocotb 的信号读写**：Python 测试台通过 `dut.xxx` 访问 HDL 里的信号，`dut.xxx.value` 读当前值，`dut.xxx.value = ...` 写入。

> 关键认知：tiny-gpu 的 `gpu.sv` 顶层注释明确写道，它是「为外部的、多通道读写的异步内存而设计」（[src/gpu.sv:5-8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L5-L8)）。本讲就是要把这句话拆开讲清楚。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，外加一个测试台作为「使用范例」：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [src/gpu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv) | GPU 顶层 | 内存规格 `parameter`、对外的 `*_mem_*` 端口、两个 controller 实例如何接内存 |
| [test/helpers/memory.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py) | 外部内存的 Python 模型 | `Memory` 类如何绑定 DUT 信号、`run()` 如何逐通道响应、`load/display` 如何调试 |
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) | 矩阵加法内核测试 | 示范 `Memory` 如何被实例化、装填、每拍驱动 |

数据流上，三个文件的关系是：

```
test_matadd.py  ──实例化──>  Memory(dut, ...)  ──绑定──>  dut.data_mem_* / dut.program_mem_*
        |                          |                              ^
        | (每拍调用 run())          | (读写 .value)                 | 这些信号来自
        v                          v                              | gpu.sv 顶层端口
   data_memory.run()          解析请求/驱动响应 ──────────────────┘
```

## 4. 核心概念与源码讲解

### 4.1 data/program 内存规格（地址位/数据位/通道数）

#### 4.1.1 概念说明

一块内存有三个最关键的规格：

- **地址位宽 ADDR_BITS**：决定「这块内存有多少行」。地址位宽为 \(n\)，则行数为 \(2^n\)。
- **数据位宽 DATA_BITS**：决定「每一行能存多少 bit」。
- **通道数 NUM_CHANNELS**：决定「同一个周期，能同时发起几笔独立的内存事务」。这是带宽（bandwidth）的关键，通道越多，单位时间能搬运的数据越多。

tiny-gpu 有**两块独立的内存**，规格不同：

- **data 内存**：存运行时数据（矩阵、中间结果）。8 位地址、8 位数据、4 通道。
- **program 内存**：存指令（kernel 的二进制程序）。8 位地址、16 位数据（一条指令正好 16 位）、1 通道，且只读。

为什么分开？因为指令和数据访问模式完全不同——指令是顺序取、只读、宽位；数据是随机访问、可读写、多通道并发。分开后可以各自优化带宽与位宽。

#### 4.1.2 核心流程

两块内存的容量与吞吐能力可以用几个简单公式概括。

- 容量：\( \text{行数} = 2^{\text{ADDR\_BITS}} \)
  - data：\( 2^8 = 256 \) 行，每行 8 bit → 256 字节
  - program：\( 2^8 = 256 \) 行，每行 16 bit → 256 条指令
- 每周期最大事务数 = `NUM_CHANNELS`（data 为 4，program 为 1）

对照表如下：

| 内存 | ADDR_BITS | DATA_BITS | NUM_CHANNELS | 容量 | 每周期事务 | 可写 |
| --- | --- | --- | --- | --- | --- | --- |
| data | 8 | 8 | 4 | 256 × 8 bit | 4 笔读/写 | 是 |
| program | 8 | 16 | 1 | 256 × 16 bit | 1 笔读 | 否（只读） |

#### 4.1.3 源码精读

这八个 `parameter` 全部声明在 `gpu.sv` 顶部，是整块内存规格的「单一事实来源」：

[src/gpu.sv:10-19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L19) —— 这段参数化设计定义了 data/program 两块内存的全部规格（地址位、数据位、通道数），以及核数与每 block 线程数。注意默认值 `DATA_MEM_DATA_BITS = 8`、`PROGRAM_MEM_DATA_BITS = 16`、`DATA_MEM_NUM_CHANNELS = 4`、`PROGRAM_MEM_NUM_CHANNELS = 1`。

在 Python 侧，`Memory` 类构造函数用同样的三个维度初始化一块纯 Python 列表当存储：

[test/helpers/memory.py:5-11](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L5-L11) —— `self.memory = [0] * (2**addr_bits)` 用一个长度为 \(2^{\text{addr\_bits}}\) 的列表模拟整块内存；`channels` 记录通道数。这里两侧规格必须一致（看 test 里的实例化）。

实例化时规格一一对应，例如 data 内存（[test/test_matadd.py:29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L29)）：`Memory(dut=dut, addr_bits=8, data_bits=8, channels=4, name="data")`；program 内存（[test/test_matadd.py:11](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L11)）：`Memory(dut=dut, addr_bits=8, data_bits=16, channels=1, name="program")`。两处的 `8/8/4` 与 `8/16/1` 与 `gpu.sv` 的默认参数完全吻合。

#### 4.1.4 代码实践

1. **实践目标**：确认 HDL 规格与 Python 模型规格一致，建立「同一块内存在两侧描述」的直觉。
2. **操作步骤**：
   - 打开 [src/gpu.sv:10-19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L19)，记下 data 与 program 的 `ADDR_BITS / DATA_BITS / NUM_CHANNELS` 默认值。
   - 打开 [test/test_matadd.py:11](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L11) 和 [test/test_matadd.py:29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L29)，对照两处 `Memory(...)` 的 `addr_bits/data_bits/channels`。
   - 用 `2**8` 手算 data 内存能存多少字节。
3. **需要观察的现象**：两块内存的规格参数在 HDL 与 Python 两侧数字相同。
4. **预期结果**：data = 8/8/4 → 256 字节；program = 8/16/1 → 256 条 16-bit 指令。
5. 待本地验证（若你已装好工具链，可改 `gpu.sv` 的某个参数并在 test 里同步修改，观察两侧不一致时仿真是否出错）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DATA_MEM_ADDR_BITS` 从 8 改成 10，data 内存容量变成多少？
**答**：\( 2^{10} = 1024 \) 行 × 8 bit = 1024 字节（1 KiB）。

**练习 2**：为什么 program 内存只需要 1 个通道，而 data 内存要 4 个？
**答**：program 内存只被每个核的 1 个 fetcher 顺序读取，并发度低；data 内存要服务所有核里每个线程的 LSU（共 `NUM_CORES × THREADS_PER_BLOCK` 个），并发请求多，需要更多通道来提高带宽、减少排队。

### 4.2 多通道握手接口 valid/ready/data

#### 4.2.1 概念说明

tiny-gpu 用一套统一的「多通道 valid/ready/data」接口来访问外部内存。每个通道（channel）独立工作，互不干扰。对**读事务**，三类信号的含义是：

- `*_mem_read_valid`（每通道 1 bit，打包成一根多位线）：GPU 拉高表示「本通道这一拍要读」。
- `*_mem_read_address`（每通道 `ADDR_BITS` 位，未打包数组）：本通道要读的地址。
- `*_mem_read_ready`（每通道 1 bit）：外部内存拉高表示「本通道的数据已经准备好了」。
- `*_mem_read_data`（每通道 `DATA_BITS` 位，未打包数组）：外部内存回送的数据。

写事务对称地有 `write_valid / write_address / write_data / write_ready`，其中 `write_data` 由 GPU 发出、`write_ready` 由内存回送表示「已收下」。

关键点：**方向**。从 GPU 的视角看，请求类信号（`read_valid/read_address`、`write_valid/write_address/write_data`）是 **output**（GPU 驱动给外部内存）；应答类信号（`read_ready/read_data`、`write_ready`）是 **input**（外部内存驱动回 GPU）。Python 的 `Memory` 类正好扮演「外部内存」，所以它读请求、写应答。

#### 4.2.2 核心流程

一次「读」的时序（理想化的异步内存，见 4.3）：

```
周期 N:
  GPU  -> 内存:  read_valid[ch]=1, read_address[ch]=A
  内存 -> GPU:   read_ready[ch]=1, read_data[ch]=MEM[A]   （同拍响应）
周期 N 之后: GPU（经 controller）采到 ready，收下 data。
```

对每个通道，握手成立的条件是 `valid && ready` 同拍为高。多通道就是把这套信号复制 `NUM_CHANNELS` 份并行运作。

方向与「谁是消费者」的对照：

| 信号 | 方向（GPU 视角） | 含义 |
| --- | --- | --- |
| `*_mem_read_valid` | output | 「我要读这个通道」 |
| `*_mem_read_address` | output | 「读这个地址」 |
| `*_mem_read_ready` | input | 「数据备好了」 |
| `*_mem_read_data` | input | 回送的数据 |
| `*_mem_write_valid` | output | 「我要写这个通道」 |
| `*_mem_write_address` | output | 「写这个地址」 |
| `*_mem_write_data` | output | 「写这个值」 |
| `*_mem_write_ready` | input | 「已收下」 |

#### 4.2.3 源码精读

顶层端口分两块，先看 program 内存（只读，只有 read 四类信号）：

[src/gpu.sv:31-35](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L31-L35) —— program 内存接口：`program_mem_read_valid`（output，1 位，因 `PROGRAM_MEM_NUM_CHANNELS=1`）、`program_mem_read_address`（output，未打包数组）、`program_mem_read_ready`/`program_mem_read_data`（input）。注意 program 内存没有任何 write 信号。

再看 data 内存（可读写，read + write 全套）：

[src/gpu.sv:37-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L37-L45) —— data 内存接口共 8 组信号：读 4 组、写 4 组。每个 `*_valid`/`*_ready` 都是 `DATA_MEM_NUM_CHANNELS`（=4）位打包线；每个 `*_address`/`*_data` 都是长度为 4 的未打包数组。

这两个对外的 `*_mem_*` 接口并非由 GPU 内部直接产生，而是由两个 `controller` 实例作为「中继」——controller 的 `mem_*` 侧就是顶层对外接口。data 控制器服务 8 个 LSU、4 个外部通道：

[src/gpu.sv:85-112](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L112) —— data 内存控制器实例：`NUM_CONSUMERS=NUM_LSUS`（=8 个 LSU）、`NUM_CHANNELS=DATA_MEM_NUM_CHANNELS`（=4）。它的 `consumer_*` 侧接 8 个 LSU，`mem_*` 侧接顶层的 `data_mem_*` 对外端口。

[src/gpu.sv:114-134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L114-L134) —— program 内存控制器实例：`NUM_CONSUMERS=NUM_FETCHERS`（=2 个取指器）、`NUM_CHANNELS=1`，且 `WRITE_ENABLE(0)` 关闭写路径（所以连 `mem_write_*` 端口都没接）。

> 把这段和 4.1 结合起来：顶层注释说的「external async memory with multi-channel read/write」，落到代码上就是这 8 组（data）/4 组（program）握手信号 + 两个 controller 做中继。

#### 4.2.4 代码实践

1. **实践目标**：亲手把「GPU 输出/输入方向」与「请求/应答角色」对上号。
2. **操作步骤**：
   - 在 [src/gpu.sv:37-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L37-L45) 里，逐行标注每个 data 内存信号是 `output wire` 还是 `input wire`。
   - 对每个信号判断：它是 GPU 在「提请求」还是「收应答」。
3. **需要观察的现象**：`output` 恰好都是请求类（valid/address/data-on-write），`input` 恰好都是应答类（ready/data-on-read）。
4. **预期结果**：见上方对照表。8 组信号方向与角色一一对应。
5. 待本地验证（无需运行，纯阅读即可确认）。

#### 4.2.5 小练习与答案

**练习 1**：program 内存接口为什么没有 `write_*` 信号？
**答**：因为 program 内存只读（指令不可在运行时改写），其 controller 用 `WRITE_ENABLE(0)` 关闭写路径，所以顶层连 write 端口都不声明。

**练习 2**：data 内存的 `data_mem_read_valid` 是 1 根线还是一个数组？几位？
**答**：是 1 根打包线，位宽 = `DATA_MEM_NUM_CHANNELS` = 4 位，每一位代表一个通道的 valid。

### 4.3 Python Memory.run() 的逐通道响应逻辑

#### 4.3.1 概念说明

`Memory` 类是 tiny-gpu 仿真里的「外部 DRAM」。真实 DRAM 是异步器件——给出地址，过一段时间回送数据。为了简化，本模型做成**零延迟**：在同一拍看到请求，就在同一拍把应答驱动回去（这就是顶层注释里「async memory」的仿真实现）。

难点在于：cocotb 把多通道的打包线和未打包数组都呈现为**一整串拼接的 bit 字符串**。例如 data 内存 4 通道、地址 8 位时，`mem_read_address.value` 是一个 32 位的长串（4 段 × 8 位拼接）。因此 `run()` 必须：

1. 把请求信号**拆开**成「每通道一份」；
2. 逐通道查表、生成应答；
3. 把应答**重新拼回**长串，写回 DUT。

#### 4.3.2 核心流程

`run()` 每拍执行一次，处理读和写两条路径。读路径伪代码：

```
read_valid  = 把 mem_read_valid 的位串，每 1 位切一段    → [v0, v1, v2, v3]
read_addr   = 把 mem_read_address 的位串，每 ADDR_BITS 位切一段 → [a0, a1, a2, a3]
read_ready  = [0]*4
read_data   = [0]*4
for i in 通道数:
    if read_valid[i] == 1:
        read_data[i]  = memory[ read_addr[i] ]   # 查表
        read_ready[i] = 1                        # 同拍应答
# 把 read_data（每段 DATA_BITS 位）和 read_ready（每段 1 位）拼回长串，写回 DUT
```

写路径同理，只不过把 `memory[addr] = data`（写入存储）并回送 `write_ready`。program 内存（`name == "program"`）跳过整段写逻辑，因为它只读。

**为什么要「逐位/逐段解析」**？因为多通道接口在 cocotb 里没有现成的「第 i 通道」访问器，只有一整串拼接 bit；要拿到第 i 通道的 valid/address，只能按位宽手工切片。请求和应答使用**同一套拼接布局**，所以拆开与拼回是对称的，通道对应关系不会错乱。

#### 4.3.3 源码精读

先看构造函数如何用 `getattr` 按命名约定绑定 DUT 信号：

[test/helpers/memory.py:13-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L13-L22) —— 用 `getattr(dut, f"{name}_mem_read_valid")` 等绑定到 `dut.data_mem_read_valid` / `dut.program_mem_read_valid` 等顶层端口（`name` 是 `"data"` 或 `"program"`）。注意第 18 行 `if name != "program"`：只有 data 内存才绑定 write 接口，与 controller 的 `WRITE_ENABLE` 对应。

读路径的「逐位切 valid」：

[test/helpers/memory.py:25-28](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L25-L28) —— 把 `mem_read_valid` 的位串按步长 1 切，得到每通道 1 位的 valid 列表。

「按 ADDR_BITS 切 address」：

[test/helpers/memory.py:30-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L30-L33) —— 把 address 位串按步长 `addr_bits` 切，得到每通道一个地址整数。data 内存就是切成 4 段 × 8 位。

逐通道查表与应答、再拼回：

[test/helpers/memory.py:34-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L34-L45) —— 第 37-42 行的 `for i in range(self.channels)` 是核心：某通道 valid 为 1，就用该通道地址查 `self.memory`，把结果放进 `mem_read_data[i]` 并置 `mem_read_ready[i]=1`；否则保持 0。第 44-45 行把每通道的 data（各 `DATA_BITS` 位）和 ready（各 1 位）重新 join 成一整串并 `int(..., 2)` 转成整数，写回 DUT 的 `mem_read_data.value` / `mem_read_ready.value`。这就是「read_ready/read_data 如何被驱动回 DUT」的答案。

写路径（仅 data 内存）：

[test/helpers/memory.py:47-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L47-L69) —— 与读对称：切 valid/address/data → 对 valid 通道执行 `self.memory[addr] = data`（真正写入存储）→ 回送 `mem_write_ready`。program 内存因第 47 行的 `if name != "program"` 而整段跳过。

最后看 `run()` 在主循环里被怎么调用：

[test/test_matadd.py:50-58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L50-L58) —— 每拍（`while not done`）先调 `data_memory.run()` 和 `program_memory.run()` 驱动外部内存，再 `await RisingEdge(clk)` 推进一拍。所以 `run()` 的频率正好是「每时钟周期一次」，与 controller 的握手节拍对齐。

#### 4.3.4 代码实践（本讲主任务）

> 阅读本任务对应规格：**阅读 `memory.py` 的 `run()`，解释为什么每个周期要逐位解析 valid 与 address，并说明 read_ready/read_data 是如何被驱动回 DUT 的。**

1. **实践目标**：用自己的话把 `run()` 的「拆—查—拼」三步讲清楚。
2. **操作步骤**：
   - 打开 [test/helpers/memory.py:24-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L24-L45)。
   - 准备回答这两个问题：
     - **Q1（为什么逐位/逐段解析）**：因为 cocotb 把多通道的打包线和未打包数组都序列化成一整串拼接 bit，没有「第 i 通道」的直达访问器；要得到第 i 通道的 valid（1 位）和 address（`ADDR_BITS` 位），只能按位宽对长串切片。
     - **Q2（read_ready/read_data 如何驱动回 DUT）**：逐通道算好应答后，把每通道的 data 用 `format(d, '0{DATA_BITS}b')`、每通道的 ready 用 `format(r, '01b')` 拼成一整串，`int(..., 2)` 转整数，再赋给 `self.mem_read_data.value` / `self.mem_read_ready.value`。请求侧的「拆」和应答侧的「拼」用的是同一套拼接布局，所以通道对应关系一致。
   - 进阶观察：在 [test/helpers/memory.py:44-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L44-L45) 处的 `''.join(...)` 上，确认 data 拼回时每段宽度是 `self.data_bits`，ready 拼回时每段宽度是 1。
3. **需要观察的现象**：`run()` 内部对 valid/address 是「解析」（读 → 切片），对 ready/data 是「驱动回」（计算 → 拼接 → 写 `.value`）。
4. **预期结果**：你能复述 Q1、Q2 两个问题的答案，并指出对应的代码行。
5. 待本地验证（若想实测：可在 `run()` 第 44 行前 `print` 一次 `mem_read_data`，跑一次 `make test_matadd`，观察每个周期哪些通道被点亮——但本项目默认无 print，属可选增强）。

#### 4.3.5 小练习与答案

**练习 1**：如果 data 内存的通道数从 4 改成 8（且 `gpu.sv` 同步改 `DATA_MEM_NUM_CHANNELS=8`），`run()` 里哪些切片步长会变、哪些不变？
**答**：valid 的步长仍是 1（每位一通道，只是列表变长到 8）；address 的步长仍是 `addr_bits`（8 位，不变），只是切出的段数从 4 变 8；ready 拼回每段仍是 1 位；data 拼回每段仍是 `data_bits`（8 位）。即「步长不变、段数变多」。

**练习 2**：为什么 `run()` 对读是「同拍响应」？这对仿真意味着什么？
**答**：因为第 37-42 行在观测到 valid 的同一拍就把 ready/data 算好并写回，没有引入额外周期延迟。它把外部内存建模成理想的异步内存（零延迟），所以本模型的带宽瓶颈只来自通道数（`NUM_CHANNELS`）和 controller 的仲裁，而不来自 DRAM 延迟——这是相对真实硬件的简化。

### 4.4 load()/display() 调试方法

#### 4.4.1 概念说明

`Memory` 类除了扮演运行时内存，还提供两个调试方法：

- `load(rows)`：在仿真启动前，把一段数据列表「灌」进内存，模拟「主机把程序/数据装进 DRAM」的过程。
- `display(rows, decimal=True)`：把内存前若干行打印成表格，用于在日志里观察「初始数据」和「最终数据」。

这两个方法不动 DUT 的握手信号，纯粹操作 Python 侧的 `self.memory` 列表。它们是连接「仿真流程」与「可观察输出」的桥梁。

#### 4.4.2 核心流程

装填与展示的流程：

```
load(rows):       for 地址, 数据 in enumerate(rows): memory[地址] = 数据
write(addr,data): memory[addr] = data   （load 的单点版本，带越界保护）
display(rows):    打印表头 → 打印前 rows 行 → 表尾（decimal 或 16 位二进制）
```

在测试里的典型用法（见 test_matadd.py）：
1. `setup()` 里 `program_memory.load(program)` / `data_memory.load(data)` 灌入初始内容；
2. 内核跑完后 `data_memory.display(24)` 打印最终内存，肉眼或断言核对结果。

#### 4.4.3 源码精读

[test/helpers/memory.py:71-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L71-L77) —— `write()` 带越界保护（`address < len(self.memory)`）；`load()` 用 `enumerate` 把列表下标当地址、元素当数据，逐个 `write`，所以 `load([a,b,c])` 会写到地址 0、1、2。

[test/helpers/memory.py:79-98](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L79-L98) —— `display()` 打印一张 `Addr | Data` 表格：标题为 `{NAME} MEMORY`（如 `DATA MEMORY`）；`decimal=True` 打印十进制，否则打印 16 位二进制；只打印前 `rows` 行。这套表格正是 u1-l3 提到的仿真日志里「初始/最终数据内存」两段的来源。

实际调用点：

[test/test_matadd.py:47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L47) 和 [test/test_matadd.py:61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L61) —— 内核运行前后各 `data_memory.display(24)` 一次，分别对应日志里的「初始数据内存」和「最终数据内存」。

#### 4.4.4 代码实践

1. **实践目标**：体会 `load`/`display` 如何把「内存内容」变成「可观察的日志」。
2. **操作步骤**：
   - 在 [test/test_matadd.py:30-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L30-L33) 看 data 初始值：前 8 个是矩阵 A，后 8 个是矩阵 B。
   - 跟踪 `data_memory.load(data)`（在 [setup.py:28](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L28) 内）把这些值灌进地址 0–15。
   - 运行 `make test_matadd` 后打开 `test/logs/` 下最新日志，找到两次 `DATA MEMORY` 表格。
3. **需要观察的现象**：第一次 `DATA MEMORY` 表里地址 0–15 是输入（A、B），地址 16+ 为 0；第二次表里地址 16–23 变成了 \(A[i]+B[i]\)。
4. **预期结果**：最终内存地址 16–23 分别为 `0,2,4,6,8,10,12,14`（对应 [test/test_matadd.py:63-66](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L63-L66) 的断言）。
5. 待本地验证（需要装好 iverilog/cocotb/sv2v 工具链，见 u1-l3）。

#### 4.4.5 小练习与答案

**练习 1**：`load([9, 8, 7])` 之后，`display` 会看到地址 0、1、2 分别是什么？
**答**：地址 0 = 9，地址 1 = 8，地址 2 = 7（`enumerate` 把列表下标当地址）。

**练习 2**：`display(rows, decimal=False)` 对 program 内存尤其有用，为什么？
**答**：program 内存每行是一条 16 位指令；`decimal=False` 会按 16 位二进制打印，正好对应 ISA 的位域编码，便于把内存内容与指令二进制对照阅读（配合 u5-l1 的 ISA 讲义）。

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个**「追踪一次 data 内存读的全链路」**任务：

1. **装填**：阅读 [test/test_matadd.py:29-33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L29-L33)，确认 data 内存的规格（8/8/4）与初始内容。
2. **接口**：在 [src/gpu.sv:37-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L37-L45) 找到 `data_mem_read_valid`（4 位打包）和 `data_mem_read_address`（4 段未打包数组）。说明哪一个是 GPU 输出、哪一个会收到应答。
3. **中继**：指出这两个信号由 [src/gpu.sv:85-112](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L112) 的 data 内存控制器驱动到顶层（本讲只到「控制器是中继」，控制器内部留待 u3-l2）。
4. **响应**：当某通道 `data_mem_read_valid[i]=1` 且 `data_mem_read_address[i]=A`，[test/helpers/memory.py:37-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L37-L45) 会把 `memory[A]` 放进 `mem_read_data[i]`、置 `mem_read_ready[i]=1`，再拼回长串写回 DUT。
5. **产出**：画一条「GPU(controller) → 顶层端口 → cocotb → Memory.run() → 写回顶层端口 → GPU」的闭环箭头图，标注每一段用的信号名和方向。

完成后，你就把「规格 → 接口 → 中继 → 外部模型 → 回写」这一整条外部内存链路打通了。

## 6. 本讲小结

- tiny-gpu 有两块独立内存：data（8 位地址/8 位数据/4 通道，可读写）与 program（8 位地址/16 位指令/1 通道，只读），规格全部由 [src/gpu.sv:10-19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L10-L19) 的 `parameter` 定义。
- 对外接口是统一的「多通道 valid/ready/data」握手：请求类信号（valid/address/data-on-write）是 GPU 输出，应答类信号（ready/data-on-read）是 GPU 输入。
- 两块内存各有一个 `controller` 实例作中继：data 控制器服务 8 个 LSU、4 通道；program 控制器服务 2 个 fetcher、1 通道且 `WRITE_ENABLE=0`。
- 仿真里外部内存由 [test/helpers/memory.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py) 的 `Memory` 类扮演，每拍 `run()` 一次，零延迟同拍响应。
- `run()` 必须逐位/逐段解析 valid 与 address，是因为 cocotb 把多通道信号序列化成拼接长串；应答用同一套布局拼回写回 DUT。
- `load()` 灌入初始内容、`display()` 打印内存表格，二者是仿真可观察性的来源（对应日志的「初始/最终数据内存」两段）。

## 7. 下一步学习建议

本讲只站在「GPU 边界」看了外部内存这一侧。下一步建议：

- **u3-l2 内存控制器：带宽节流**——下钻到 [src/controller.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv) 内部，看它如何在「8 个 LSU 消费者」与「4 个外部通道」之间做仲裁、排队与中继，理解本讲反复提到的「带宽瓶颈」从何而来。
- 若想先看内存被谁使用，可跳读 u5-l3「LSU 异步访存」，看 LSU 如何发出本讲接口里的 `consumer_read_valid`，再经 controller 到达本讲的 `*_mem_*` 端口。
- 复习 u2-l1 有助于把本讲的 `*_mem_*` 顶层端口和 `consumer_*`/`lsu_*`/`fetcher_*` 内部信号在一张图上对齐。
