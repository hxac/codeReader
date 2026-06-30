# 顶层接口与寄存器地址映射

## 1. 本讲目标

上一讲（u1-l3）我们学会了贯穿全工程的 Verilog 编码风格：`reg/_new/_we` 寄存器模式、`always @(posedge clk or negedge reset_n)` 时序块、`always @*` 组合块。本讲我们要用这把钥匙打开 `aes.v` 的「对外契约」——**主机（CPU/总线）到底怎么和这个 AES 核对话**。

读完本讲，你应当能够：

- 说出 `aes.v` 顶层模块每个端口的作用，理解这是一个**总线式寄存器接口**。
- 记住 `0x00 ~ 0x33` 这张地址映射表里各分区（名称/版本、CTRL、STATUS、CONFIG、KEY、BLOCK、RESULT）的职责。
- 解释 CTRL 的 `init`/`next` 触发位、STATUS 的 `ready`/`valid` 状态位、CONFIG 的 `encdec`/`keylen` 配置位分别是什么含义。
- 自己写出主机对 AES 做一次加密所需的完整地址访问序列。

本讲只覆盖**顶层 wrapper** `aes.v`，不进入 `aes_core` 内部（那是 u2-l1 的事）。掌握本讲后，你就拥有了「以主机视角」驱动整个 AES 核的能力。

## 2. 前置知识

### 2.1 什么是「总线式寄存器接口」

很多硬件加速核（如 AES、SHA、UART）并不直接暴露算法函数，而是把自己伪装成一堆**寄存器**：主机像读写内存一样，往某个地址写数据、从某个地址读结果。这种风格叫**内存映射寄存器接口**（memory-mapped register interface）。

一次典型的交互长这样：

```
主机写 地址A ← 数据X      （把数据/配置送进核）
主机写 CTRL  ← 触发位       （命令核"开始干活"）
主机轮询读 STATUS           （等 ready/valid）
主机读 地址B → 结果Y        （取回结果）
```

本讲的 `aes.v` 就是这样一个接口。它的端口非常精简，**没有**专门的 `start`/`done` 引脚，所有控制都通过写 CTRL、读 STATUS 完成。

### 2.2 本讲用到的 Verilog 回顾

- `localparam`：模块内的常量，用来给地址/位编号起名字（u1-l3 已介绍）。
- `always @*` 组合块：本讲的 `api` 块就用它做命令译码，块开头先给所有输出写默认值，避免生成锁存器。
- `reg/_new/_we` 模式：组合块算 `_new` 和 `_we`，时序块 `reg_update` 在时钟沿执行 `if (_we) _reg <= _new`。

如果你对上面任何一条还不熟，建议先回到 u1-l3 复习。

### 2.3 AES 角色回顾（来自 u1-l1）

- AES 是对称分组密码，块长固定 128 位；本核支持 128 位（10 轮）与 256 位（14 轮）两种密钥，运行时由配置位切换。
- 本核工作在 **ECB 单块模式**：一块明文进、一块密文出。
- 因此接口里需要：写密钥（最多 256 位）、写明文（128 位）、触发运算、读密文（128 位）。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，外加一个用于实践对照的测试平台：

| 文件 | 角色 | 本讲用到什么 |
|------|------|--------------|
| `rtl/aes.v` | 顶层 wrapper，**本讲主角** | 端口、地址 localparam、`api` 译码块、`reg_update` 寄存器更新块、core 实例化 |
| `rtl/tb_aes.v` | 顶层 testbench | `write_word` / `init_key` / `ecb_mode_single_block_test` 三个任务，作为"主机怎么用这套接口"的真实范例 |

> 提醒：`aes.v` 里还实例化了 `aes_core`（[rtl/aes.v:116-131](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L116-L131)），但本讲只把它当作一个"黑盒协处理器"——我们只关心主机如何通过寄存器驱动它，不关心它内部状态机。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **顶层模块端口定义**——`aes.v` 暴露给外部的 7 个信号。
2. **寄存器地址映射 localparam**——`0x00 ~ 0x33` 这张表。
3. **api 命令译码 always 块**——把"地址 + 读/写"翻译成具体动作，并配合 `reg_update` 落到寄存器。

---

### 4.1 顶层模块端口定义

#### 4.1.1 概念说明

`aes` 模块是整个工程的**对外门面**。它把内部所有复杂运算（密钥扩展、加/解密）藏起来，只对外暴露一个极简的、同步的总线接口。任何想用这个核的主机（CPU、DMA、另一个 SoC 模块），都只看得到这 7 根信号。

这套接口的设计哲学是：**用最少的引脚完成最多的控制**——没有并行数据总线、没有中断引脚、没有 DMA 请求，所有交互都靠 `cs`（片选）+ `we`（读/写选择）+ `address`（地址）+ `write_data`/`read_data`（数据）这组信号完成。

#### 4.1.2 核心流程

一次总线访问的时序可以概括为：

```
        ┌──── 写访问 (we=1) ────┐    ┌──── 读访问 (we=0) ────┐
cs=1    │  cs=1                │    │  cs=1                │
we=?    │  we=1                │    │  we=0                │
address │  address = 目标寄存器 │    │  address = 目标寄存器 │
data    │  write_data = 要写的值│    │  read_data ← 读出的值 │
        └──────────────────────┘    └──────────────────────┘
```

- `cs`（chip select）= 1 时本次访问才生效；= 0 时接口"装死"，`api` 块所有写使能保持默认 0，`read_data` 保持 0。这是一种典型的**片选门控**。
- `we`（write enable）区分读写：`we=1` 写、`we=0` 读。
- `address` 只有 8 位（`[7:0]`），所以寻址空间是 `0x00 ~ 0xFF`，但本核实际只用到了 `0x00 ~ 0x33`。
- 数据宽度固定 32 位（`[31:0]`），所以 128 位明文要分 4 次写、256 位密钥要分 8 次写。

#### 4.1.3 源码精读

端口定义就在模块声明的端口列表里：

[rtl/aes.v:9-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22) —— 顶层模块的全部对外端口：`clk`/`reset_n` 是时钟与异步低有效复位；`cs`/`we` 是控制信号；`address` 8 位、`write_data`/`read_data` 各 32 位是数据通路。

```verilog
module aes(
           input wire           clk,
           input wire           reset_n,
           input wire           cs,
           input wire           we,
           input wire  [7 : 0]  address,
           input wire  [31 : 0] write_data,
           output wire [31 : 0] read_data
          );
```

几点要特别留意：

- **没有 `init`/`next`/`start`/`done` 这类专用控制引脚**。触发加密靠"写 CTRL 寄存器的某个位"，完成通知靠"读 STATUS 寄存器的某个位"。这是本接口最重要的设计决定。
- **`read_data` 是 `wire`**，它直接被 `assign read_data = tmp_read_data;` 驱动（[rtl/aes.v:100](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L100)）。真正的读译码在 `api` 组合块里算进 `tmp_read_data`（一个 `reg`），再连出去。组合读意味着：**只要 `cs=1, we=0` 且地址稳定，`read_data` 当拍就反映对应寄存器值**，不需要等待时钟沿。
- **`reset_n` 是异步低有效**：在 `reg_update` 块里写作 `always @ (posedge clk or negedge reset_n)`，复位分支把所有寄存器清零（[rtl/aes.v:140-160](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L140-L160)）。

> 小提示：顶层 `ready_reg` 复位值是 `1'b0`（[rtl/aes.v:159](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L159)），但它每拍都跟随 `core_ready`（[rtl/aes.v:163](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L163)）；而 `aes_core` 里 `ready_reg` 复位为 `1'b1`（[rtl/aes_core.v:161](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L161)）。所以复位释放后第一个时钟沿，顶层 `ready_reg` 就会被刷成 1。读 STATUS 看到 ready=1 表示"核空闲，可以接收新命令"。

#### 4.1.4 代码实践

**实践目标**：在脑子里建立"端口 → 访问语义"的直觉。

**操作步骤**：

1. 打开 [rtl/aes.v:9-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22)。
2. 对照下面的表，给每个端口写一句话"主机视角"的说明：

| 端口 | 方向 | 主机视角的含义 |
|------|------|----------------|
| `clk` | in | 我（主机）必须和它同步时钟 |
| `reset_n` | in | 我先把它拉低再拉高，复位核 |
| `cs` | in | 我要访问时拉高，不访问时拉低 |
| `we` | in | 我拉高=写，拉低=读 |
| `address[7:0]` | in | 我给出要访问的寄存器编号 |
| `write_data[31:0]` | in | 写访问时我提供的数据 |
| `read_data[31:0]` | out | 读访问时核返回给我的数据 |

3. 思考：如果要把 256 位密钥写进去，最少需要几个时钟周期？（答案：8 次 32 位写入。）

**需要观察的现象**：当你把这张表填完，会发现**没有任何一根信号叫 `encrypt` 或 `result_valid_pin`**——全部控制都"塞"进了地址空间。这就是内存映射接口的本质。

**预期结果**：你能口头描述"一次写访问需要 `cs=1, we=1, address=X, write_data=Y` 同时有效"。

#### 4.1.5 小练习与答案

**练习 1**：`address` 是 8 位，理论上可寻址 256 个寄存器，但本核最高地址只用到 `0x33`。剩下空间去哪了？

**答案**：没用。地址空间预留了扩展余量，但本核只实现了 `0x00~0x33` 范围内的寄存器，访问未定义地址时 `api` 块走 `default` 分支，什么也不做（写被忽略、读返回 0）。这是硬件接口常见的"留白"做法。

**练习 2**：为什么 `read_data` 设计成组合读（当拍出结果），而不是寄存器读（下一拍出结果）？

**答案**：组合读让主机可以在同一个总线周期内拿到 STATUS、RESULT 等只读寄存器的值，访问延迟低、握手简单；代价是读路径上有一段组合逻辑（地址译码 + 多路选择），会进入关键路径。对于本核这种低速接口（README 称吞吐 0.06 Gbps），这个代价完全可接受。

---

### 4.2 寄存器地址映射 localparam

#### 4.2.1 概念说明

有了端口，下一步就是约定"地址编号代表哪个寄存器"。这张约定表就是**地址映射**（address map）。在 `aes.v` 里，它由一组 `localparam` 常量定义：每个 `ADDR_*` 是一个 8 位地址，每个 `*_BIT` 是寄存器内某个功能位的位号。

这张表是**主机与固件/驱动之间的契约**：驱动代码（或 testbench）必须和它严格一致，否则写错地址核就收不到数据。

#### 4.2.2 核心流程

整张地址映射可以画成一张"楼层图"：

```
地址      寄存器         方向    作用
─────────────────────────────────────────────────────────
0x00      NAME0         只读    标识 "aes "（核的名字，第一字）
0x01      NAME1         只读    标识 "    "（核的名字，第二字）
0x02      VERSION       只读    标识 "0.60"（版本号）
0x08      CTRL          读写    触发位：bit0=init, bit1=next
0x09      STATUS        只读    状态位：bit0=ready, bit1=valid
0x0a      CONFIG        读写    配置位：bit0=encdec, bit1=keylen
0x10~0x17 KEY0..KEY7    只写    256 位密钥（8 个 32 位字）
0x20~0x23 BLOCK0..BLOCK3 只写   128 位明文/密文块（4 个 32 位字）
0x30~0x33 RESULT0..RESULT3 只读 128 位结果（4 个 32 位字）
```

可以看出地址被分成几个**不连续的区段**：`0x00` 区是身份信息、`0x08` 区是控制/状态/配置、`0x10` 区是密钥、`0x20` 区是数据块、`0x30` 区是结果。每个区段之间留了空隙（例如 `0x03~0x07` 没用），方便将来插入新寄存器。

三个"控制类"寄存器最关键，它们的位定义如下（注意位号都是从 0 开始数）：

- **CTRL**（触发）：`bit0 = init`（启动密钥扩展），`bit1 = next`（启动一次加/解密）。这两个位是**脉冲触发**——主机写 1 让核开始干活，核内部自动把它清 0（见 4.3 节）。
- **STATUS**（状态）：`bit0 = ready`（核空闲，可接收命令），`bit1 = valid`（结果寄存器 RESULT 里有有效数据可读）。
- **CONFIG**（配置）：`bit0 = encdec`（0=解密 decipher，1=加密 encipher），`bit1 = keylen`（0=128 位密钥，1=256 位密钥）。

> 注意 encdec 的极性：本核约定 `encdec = 1` 表示**加密**，`encdec = 0` 表示**解密**（见 testbench 里 `AES_ENCIPHER = 1'b1`、`AES_DECIPHER = 1'b0`，[rtl/tb_aes.v:63-64](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L63-L64)）。

#### 4.2.3 源码精读

身份/版本常量（ASCII 字符串打包成 32 位整数）：

[rtl/aes.v:52-54](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L52-L54) —— 把 ASCII 字符 `"aes "`、`"    "`、`"0.60"` 编码成十六进制常量，主机读 NAME0/NAME1/VERSION 可校验"我连上的确实是这个核、这个版本"。

```verilog
localparam CORE_NAME0       = 32'h61657320; // "aes "
localparam CORE_NAME1       = 32'h20202020; // "    "
localparam CORE_VERSION     = 32'h302e3630; // "0.60"
```

> 拆解一下 `0x61657320`：`0x61='a'`、`0x65='e'`、`0x73='s'`、`0x20=' '`，正好是 `"aes "`。这是一种常见的硬件"身份寄存器"惯例。

地址与位编号常量（本讲最核心的一组）：

[rtl/aes.v:27-50](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L27-L50) —— 完整地址映射与各功能位的位号定义。这里只摘关键的触发/状态/配置位：

```verilog
localparam ADDR_CTRL        = 8'h08;
localparam CTRL_INIT_BIT    = 0;
localparam CTRL_NEXT_BIT    = 1;

localparam ADDR_STATUS      = 8'h09;
localparam STATUS_READY_BIT = 0;
localparam STATUS_VALID_BIT = 1;

localparam ADDR_CONFIG      = 8'h0a;
localparam CTRL_ENCDEC_BIT  = 0;
localparam CTRL_KEYLEN_BIT  = 1;
```

数据区段用"首地址 + 末地址"成对定义，便于在 `api` 块里做范围判断：

[rtl/aes.v:43-50](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L43-L50) —— KEY/BLOCK/RESULT 三个数组寄存器的地址范围。注意 KEY 是 8 项（`0x10~0x17`，覆盖 256 位），BLOCK 和 RESULT 都是 4 项（`0x20~0x23`、`0x30~0x33`，覆盖 128 位）。

```verilog
localparam ADDR_KEY0        = 8'h10;
localparam ADDR_KEY7        = 8'h17;

localparam ADDR_BLOCK0      = 8'h20;
localparam ADDR_BLOCK3      = 8'h23;

localparam ADDR_RESULT0     = 8'h30;
localparam ADDR_RESULT3     = 8'h33;
```

> ⚠️ **源码阅读注意点（易错）**：`rtl/aes.v`（设计源码，权威）里 CONFIG 的位定义是 `CTRL_ENCDEC_BIT = 0`、`CTRL_KEYLEN_BIT = 1`。但 `rtl/tb_aes.v`（测试平台）里有一组**未被使用的**同名 parameter 写成了 `CTRL_ENCDEC_BIT = 2`、`CTRL_KEYLEN_BIT = 3`（[rtl/tb_aes.v:32-33](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L32-L33)）。testbench 实际驱动 CONFIG 时**并没有**用这些 parameter，而是用表达式 `(key_length << 1) + encdec`（见 [rtl/tb_aes.v:355](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L355)），算出来的值是 bit0=encdec、bit1=keylen，**与 `aes.v` 一致**。所以那组 parameter 是"死代码"，会误导人。请始终以 `rtl/aes.v` 为准。这也是 u1-l1 强调"以源码为准"的一个活生生的例子。

#### 4.2.4 代码实践

**实践目标**：把地址映射表内化成"肌肉记忆"，并用 testbench 的实际写值交叉验证 CONFIG 位定义。

**操作步骤**：

1. 打开 [rtl/aes.v:27-54](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L27-L54)，把所有 `ADDR_*` 抄成一张地址表。
2. 打开 testbench 的 `init_key` 任务 [rtl/tb_aes.v:303-333](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L303-L333)，看它如何写 CONFIG：256 位密钥时写 `8'h02`、128 位密钥时写 `8'h00`。
3. 验证：`8'h02 = 0b00000010`，bit1=1，对应 `keylen=1`（256 位）；`8'h00` 则 bit1=0、bit0=0，对应 128 位 + 解密。这与 `aes.v` 的 `CTRL_KEYLEN_BIT=1`、`CTRL_ENCDEC_BIT=0` 完全吻合。

**需要观察的现象**：testbench 写 CONFIG 用的字面值 `8'h02`/`8'h00` 与"位号定义"能一一对上，证明 `aes.v` 的位定义才是真值。

**预期结果**：你能解释为什么"配置成 AES-256 加密"应该往 CONFIG 写 `8'h03`（bit1=1 keylen，bit0=1 encdec）。提示：这正是 `ecb_mode_single_block_test` 里 `(key_length << 1) + encdec` 在 256+加密时算出的值。

#### 4.2.5 小练习与答案

**练习 1**：主机想确认自己连对了版本的 AES 核，应该读哪个地址、期望看到什么？

**答案**：读 `ADDR_VERSION = 0x02`，期望得到 `0x302e3630`，即 ASCII 字符串 `"0.60"`。也可读 `ADDR_NAME0 = 0x00` 得到 `0x61657320`（`"aes "`）做双重确认。

**练习 2**：STATUS 的 `ready` 和 `valid` 各代表什么？两者会同时为 1 吗？

**答案**：`ready`（bit0）表示核空闲、可接收下一条命令；`valid`（bit1）表示 RESULT 寄存器里存放着上一次运算的有效结果。它们可以同时为 1——核算完一块后，结果有效（valid=1），同时核本身又空闲了（ready=1），等待主机取走结果并下发下一块。

---

### 4.3 api 命令译码 always 块（配合 reg_update）

#### 4.3.1 概念说明

地址映射只是"纸面约定"，真正把"地址 + 读/写"翻译成硬件动作的，是 `aes.v` 里的 **`api` 组合块**（命令译码）和 **`reg_update` 时序块**（寄存器更新）。它俩正是 u1-l3 讲过的"两段式"约定在本模块的具体落地：

- `api`（`always @*`，组合）：根据 `cs/we/address` 算出每个寄存器的写使能 `_we` 和触发位的新值 `_new`，以及读数据 `tmp_read_data`。
- `reg_update`（`always @(posedge clk ...)`，时序）：在时钟沿根据 `_we` 把 `_new` 搬进 `_reg`。

可以说：**`api` 块是这套接口的"大脑"，`reg_update` 是"手"**。

#### 4.3.2 核心流程

**写译码**（`cs=1, we=1` 时）按地址分派：

```
if address == ADDR_CTRL(0x08):
    init_new = write_data[0]      # 触发密钥扩展
    next_new = write_data[1]      # 触发一次加/解密
if address == ADDR_CONFIG(0x0a):
    config_we = 1                 # 捕获 encdec/keylen 到配置寄存器
if ADDR_KEY0(0x10) <= address <= ADDR_KEY7(0x17):
    key_we = 1                    # 写某个 32 位密钥字
if ADDR_BLOCK0(0x20) <= address <= ADDR_BLOCK3(0x23):
    block_we = 1                  # 写某个 32 位明文/密文字
```

注意几个细节：

- 写 CTRL 时不是存整字，而是**抽出 bit0/bit1 作为 init/next 的脉冲**，下一拍被 `reg_update` 搬进 `init_reg`/`next_reg`，再作为 `core_init`/`core_next` 送给 core（[rtl/aes.v:107-108](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L107-L108)）。`api` 块默认把 `init_new`/`next_new` 设为 0，所以这两个信号是**单拍脉冲**：只在写 CTRL 的那一拍为 1，随后自动回 0（这正是"触发位"该有的行为）。
- 写 KEY/BLOCK 时，用地址的低位作为**数组下标**：`key_reg[address[2:0]]`（3 位选 0~7）、`block_reg[address[1:0]]`（2 位选 0~3）。所以地址本身既"选区段"又"选下标"。

**读译码**（`cs=1, we=0` 时）用 `case` + 范围判断：

```
case (address):
    ADDR_NAME0:   tmp_read_data = CORE_NAME0
    ADDR_NAME1:   tmp_read_data = CORE_NAME1
    ADDR_VERSION: tmp_read_data = CORE_VERSION
    ADDR_CTRL:    tmp_read_data = {keylen, encdec, next, init}   # 回读控制位
    ADDR_STATUS:  tmp_read_data = {valid, ready}                  # 回读状态位
    default:      tmp_read_data = 0
if ADDR_RESULT0(0x30) <= address <= ADDR_RESULT3(0x33):
    tmp_read_data = result_reg 的对应 32 位切片
```

RESULT 的切片有一个小巧的位运算，把地址映射到 `result_reg` 的位区间：

\[
\text{tmp\_read\_data} = \text{result\_reg}\big[\,(3 - (\text{address} - \text{ADDR\_RESULT0})) \times 32 \; +\!\!:\; 32\,\big]
\]

即读 `0x30`（RESULT0）拿到最高 32 位 `[127:96]`，读 `0x33`（RESULT3）拿到最低 32 位 `[31:0]`。`+:` 是 Verilog 的**变基切片**运算符，`base +: width` 表示从 `base` 起取 `width` 位。

**寄存器更新**（`reg_update`，时钟沿）把上面算出的 `_we/_new` 落地，例如：

```
if (key_we)   key_reg[address[2:0]]   <= write_data;
if (block_we) block_reg[address[1:0]] <= write_data;
init_reg <= init_new;     # 跟随脉冲
next_reg <= next_new;
```

而 128 位 `block` 和 256 位 `key` 在送给 core 前，会被打包成宽位向量（[rtl/aes.v:102-110](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L102-L110)）：

```verilog
assign core_key   = {key_reg[0], key_reg[1], ..., key_reg[7]};   // key_reg[0] 是最高字
assign core_block = {block_reg[0], block_reg[1], block_reg[2], block_reg[3]};
```

所以 `ADDR_KEY0`/`ADDR_BLOCK0` 对应**最高位字**，这与 testbench 写入顺序一致（先写高位切片）。

#### 4.3.3 源码精读

`api` 块开头先给所有输出写默认值，这是"组合块防锁存器"的标准写法（u1-l3 已强调）：

[rtl/aes.v:189-197](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L189-L197) —— `api` 块入口，把 `init_new/next_new/config_we/key_we/block_we/tmp_read_data` 全部先置默认值（0），确保任何未命中的分支都不会悬空。

```verilog
always @*
  begin : api
    init_new      = 1'b0;
    next_new      = 1'b0;
    config_we     = 1'b0;
    key_we        = 1'b0;
    block_we      = 1'b0;
    tmp_read_data = 32'h0;
```

写译码部分（`cs=1, we=1` 时）：

[rtl/aes.v:198-216](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L198-L216) —— 按地址把写访问分发到 CTRL（抽出 init/next 位）、CONFIG（置 config_we）、KEY 范围（置 key_we）、BLOCK 范围（置 block_we）。

```verilog
if (cs)
  begin
    if (we)
      begin
        if (address == ADDR_CTRL)
          begin
            init_new = write_data[CTRL_INIT_BIT];
            next_new = write_data[CTRL_NEXT_BIT];
          end
        if (address == ADDR_CONFIG)
          config_we = 1'b1;
        if ((address >= ADDR_KEY0) && (address <= ADDR_KEY7))
          key_we = 1'b1;
        if ((address >= ADDR_BLOCK0) && (address <= ADDR_BLOCK3))
          block_we = 1'b1;
      end
```

读译码部分（`cs=1, we=0` 时）：

[rtl/aes.v:218-234](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L218-L234) —— 用 `case` 回读身份/版本/CTRL/STATUS，再用范围判断回读 RESULT 切片。CTRL 回读把当前 `init/next/encdec/keylen` 打包回低 4 位；STATUS 回读把 `valid/ready` 打包回低 2 位。

```verilog
    else
      begin
        case (address)
          ADDR_NAME0:   tmp_read_data = CORE_NAME0;
          ADDR_NAME1:   tmp_read_data = CORE_NAME1;
          ADDR_VERSION: tmp_read_data = CORE_VERSION;
          ADDR_CTRL:    tmp_read_data = {28'h0, keylen_reg, encdec_reg, next_reg, init_reg};
          ADDR_STATUS:  tmp_read_data = {30'h0, valid_reg, ready_reg};
          default:      begin end
        endcase
        if ((address >= ADDR_RESULT0) && (address <= ADDR_RESULT3))
          tmp_read_data = result_reg[(3 - (address - ADDR_RESULT0)) * 32 +: 32];
      end
```

> 看回读的位拼接：STATUS 是 `{30'h0, valid_reg, ready_reg}`，所以 `ready_reg` 落在 bit0、`valid_reg` 落在 bit1，与 `STATUS_READY_BIT=0`、`STATUS_VALID_BIT=1` 对应；CTRL 是 `{28'h0, keylen_reg, encdec_reg, next_reg, init_reg}`，所以 init 在 bit0、next 在 bit1、encdec 在 bit2、keylen 在 bit3——**回读布局与 CONFIG 的写入布局并不完全相同**（CONFIG 写入时 encdec 在 bit0、keylen 在 bit1），这是两套不同寄存器，不要混淆。

`reg_update` 把写使能落地（节选 KEY/BLOCK/CONFIG 部分）：

[rtl/aes.v:169-179](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L169-L179) —— 时序块里：CONFIG 命中时把 `write_data[0]/[1]` 存进 `encdec_reg/keylen_reg`；KEY 命中时用 `address[2:0]` 选下标写 `key_reg`；BLOCK 命中时用 `address[1:0]` 选下标写 `block_reg`。

```verilog
if (config_we)
  begin
    encdec_reg <= write_data[CTRL_ENCDEC_BIT];
    keylen_reg <= write_data[CTRL_KEYLEN_BIT];
  end
if (key_we)
  key_reg[address[2 : 0]] <= write_data;
if (block_we)
  block_reg[address[1 : 0]] <= write_data;
```

core 实例化把这些寄存器接给内部协处理器：

[rtl/aes.v:116-131](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L116-L131) —— 把 `core_init`/`core_next`/`core_key`/`core_block`/`core_keylen`/`core_encdec` 作为输入送进 `aes_core`，core 算完后通过 `core_result`/`core_valid`/`core_ready` 回报，本模块再用 `reg_update` 把它们搬进 `result_reg`/`valid_reg`/`ready_reg` 供主机读取。

#### 4.3.4 代码实践

**实践目标**：写出主机做一次 AES-128 加密的完整地址访问序列，并用 testbench 的 `init_key` + `ecb_mode_single_block_test` 交叉验证每一步。

**操作步骤**：

1. 打开 [rtl/aes.v:198-216](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L198-L216)（写译码）和 [rtl/aes.v:169-179](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L169-L179)（寄存器落地）。
2. 仿照 testbench 的 `init_key`（[rtl/tb_aes.v:303-333](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L303-L333)）与 `ecb_mode_single_block_test`（[rtl/tb_aes.v:341-377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377)），写出下面这条主机访问序列（用 NIST AES-128 的标准明文 `0x6bc1...172a`、密钥 `0x2b7e...4f3c`）：

```
# A. 初始化阶段（init_key 做的事）：先写密钥、再配置、再触发 init
write 0x10 ← 0x2b7e1516   # KEY0 = key[255:224]（128 位密钥只用到低 4 个字，高 4 个写 0）
write 0x11 ← 0x28aed2a6   # KEY1
write 0x12 ← 0xabf71588   # KEY2
write 0x13 ← 0x09cf4f3c   # KEY3
write 0x14 ← 0x00000000   # KEY4..KEY7 = 0（AES-128 不用）
...
write 0x0a ← 0x00         # CONFIG: keylen=0(128位), encdec=0
write 0x08 ← 0x01         # CTRL:  bit0(init)=1  → 触发密钥扩展
                          # （轮询 STATUS.ready==1 表示扩展完成）

# B. 运算阶段（ecb_mode_single_block_test 做的事）：写明文、配置方向、触发 next
write 0x20 ← 0x6bc1bee2   # BLOCK0 = plaintext[127:96]
write 0x21 ← 0x2e409f96   # BLOCK1
write 0x22 ← 0xe93d7e11   # BLOCK2
write 0x23 ← 0x7393172a   # BLOCK3
write 0x0a ← 0x01         # CONFIG: keylen=0(128位), encdec=1(加密)
write 0x08 ← 0x02         # CTRL:  bit1(next)=1   → 触发一次加密
                          # （轮询 STATUS.valid==1 表示结果就绪）

# C. 取结果阶段（read_result 做的事）
read  0x30 → RESULT0 = result[127:96]
read  0x31 → RESULT1 = result[95:64]
read  0x32 → RESULT2 = result[63:32]
read  0x33 → RESULT3 = result[31:0]
                          # 拼起来应为 0x3ad77bb40d7a3660a89ecaf32466ef97（NIST 期望值）
```

3. 对照 testbench 里 `ecb_mode_single_block_test` 对 `ADDR_CONFIG` 的写值 `(8'h00 + (key_length << 1) + encdec)`（[rtl/tb_aes.v:355](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L355)）：当 `key_length=0`（128 位）、`encdec=1`（加密）时它算出 `0x01`，正是上面第 B 步写的 `0x0a ← 0x01`。

**需要观察的现象**：

- 写 CTRL=`0x01` 后，下一拍 `init_reg` 变 1，再下一拍因为 `init_new` 回 0，`init_reg` 又变回 0——这就是"单拍脉冲触发"。如果你用波形工具（u1-l5 会讲）观察 `dut.init_reg`，会看到一个宽度仅 1 个时钟周期的高电平。
- 写 CTRL=`0x02` 后，`next_reg` 出现同样的单拍脉冲，core 开始加密，若干周期后 STATUS 的 valid 位置 1。
- 读 RESULT0..3 拼出的 128 位与 NIST 期望值 `0x3ad77bb40d7a3660a89ecaf32466ef97` 相等。

**预期结果**：你能不查源码地复述"写密钥 → 写配置 → 写 CTRL.init → 写明文 → 写配置 → 写 CTRL.next → 读 RESULT"这条主线，并知道每一步对应哪个地址。

**待本地验证**：实际的"密钥扩展需要多少周期""加密需要多少周期"取决于 core 内部状态机（u2-l1/u2-l5 会精确给出），本讲先用 testbench 里的 `#(100 * CLK_PERIOD)` 等待（[rtl/tb_aes.v:331](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L331)、[rtl/tb_aes.v:358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L358)）作为"足够长"的保守等待，精确周期数留到进阶篇再讨论。

#### 4.3.5 小练习与答案

**练习 1**：为什么写 CTRL 时 `api` 块只抽出 `write_data[0]` 和 `write_data[1]`，而不是把整个 `write_data` 存进一个 CTRL 寄存器？

**答案**：因为 init/next 是**触发脉冲**，不是持久配置。`api` 块把它们赋给 `init_new`/`next_new`，而这两个 `_new` 信号每拍默认是 0，只有在写 CTRL 的那一拍才会变成主机给的值；`reg_update` 每拍都执行 `init_reg <= init_new`，于是 `init_reg` 只在那一拍为 1，自动形成单拍脉冲。如果把整字存进寄存器，触发位就会一直为 1，核会反复重启运算。

**练习 2**：写 KEY 时，`key_reg[address[2:0]]` 用了地址的低 3 位作下标。为什么 KEY 需要 3 位、BLOCK 只需要 2 位？

**答案**：KEY 区段有 8 个字（`0x10~0x17`），低 3 位能区分 0~7；BLOCK 区段只有 4 个字（`0x20~0x23`），低 2 位能区分 0~3。位数由数组大小决定：KEY 是 256 位 = 8×32 位，BLOCK 是 128 位 = 4×32 位。

**练习 3**：主机如何区分"核还在忙"和"核空闲"？

**答案**：读 STATUS（`0x09`）的 bit0（ready）。`ready=1` 表示核空闲、可接收新命令；触发 init 或 next 后应轮询 STATUS，等 ready 再次变 1 才能下发下一条命令。bit1（valid）则用来判断 RESULT 是否可读。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个"主机驱动手册"小任务：

**任务**：假设你要写一份给固件工程师的《AES 核寄存器使用手册》，请基于本讲源码，产出以下三样东西：

1. **一张端口表**：列出 `aes` 模块 7 个端口的方向、位宽、含义（取自 [rtl/aes.v:9-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22)）。
2. **一张地址映射表**：列出 `0x00~0x33` 全部寄存器的地址、名称、读写方向、位定义（取自 [rtl/aes.v:27-54](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L27-L54)）。务必标注 CONFIG 的位定义以 `aes.v` 为准，并提醒读者 testbench 里的同名 parameter 不可信。
3. **一段使用流程伪代码**：用 C 风格伪代码写出"AES-128 加密一个 16 字节块"的完整调用序列，包括轮询 STATUS 等待 ready/valid（取自 4.3.4 的访问序列）。

完成后，对照 testbench 的 `init_key`（[rtl/tb_aes.v:303-333](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L303-L333)）和 `ecb_mode_single_block_test`（[rtl/tb_aes.v:341-377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377)）自查：你的伪代码是不是和 testbench 实际做的事一一对应？如果有出入，以 testbench 为准修正。

> 这个任务把"端口 → 地址映射 → 命令译码 → 访问序列"整条链路打通，是后续 u1-l5（看仿真波形）和 u2-l1（进入 core 内部）的最好准备。

## 6. 本讲小结

- `aes.v` 是顶层 wrapper，对外只暴露 7 个信号：`clk/reset_n/cs/we/address[7:0]/write_data[31:0]/read_data[31:0]`，构成一个**内存映射的 32 位总线接口**。
- 地址映射 `0x00~0x33` 分为五个区段：身份信息（NAME/VERSION）、控制/状态/配置（CTRL/STATUS/CONFIG）、密钥（KEY0..KEY7，256 位）、数据块（BLOCK0..BLOCK3，128 位）、结果（RESULT0..RESULT3，128 位）。
- CTRL 的 `init`（bit0）/`next`（bit1）是**单拍脉冲触发位**；STATUS 的 `ready`（bit0）/`valid`（bit1）是状态回读；CONFIG 的 `encdec`（bit0）/`keylen`（bit1）是持久配置。
- `api` 组合块（`always @*`）做命令译码、`reg_update` 时序块（`posedge clk`）把译码结果搬进寄存器——正是 u1-l3「两段式」约定的落地。
- 一次加密的访问主线：**写密钥 → 写配置 → 写 CTRL.init（触发密钥扩展）→ 写明文 → 写配置 → 写 CTRL.next（触发运算）→ 读 RESULT**，与 testbench 的 `init_key` + `ecb_mode_single_block_test` 完全一致。
- 权威位定义永远以 `rtl/aes.v` 为准；`tb_aes.v` 里有一组未使用的、与之冲突的 parameter，是会误导人的"死代码"。

## 7. 下一步学习建议

本讲你掌握了**主机视角**：怎么用、怎么读。下一步该切到**内部视角**，看主机写下的那些值是如何被处理的：

- **u1-l5（运行仿真与阅读波形）**：先把工程跑起来，亲眼看到本讲描述的"写 CTRL → init_reg 单拍脉冲 → ready/valid 变化 → 读 RESULT"在波形上是什么样子。这是把本讲抽象序列"具象化"的最好方式。
- **u2-l1（aes_core 顶层控制与状态机）**：进入 `aes_core`，看 `core_init`/`core_next` 收到脉冲后，内部 IDLE/INIT/NEXT 状态机如何调度密钥扩展与加/解密——也就是本讲一直当作"黑盒"的那个协处理器内部。
- 建议的源码预读：在进入 u2-l1 前，可以先扫一眼 [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v)，只看它的端口（与本讲 `aes.v:116-131` 的实例化端口一一对应）和 `CTRL_IDLE/CTRL_INIT/CTRL_NEXT` 三个状态常量，建立"接口→内部"的衔接感。
