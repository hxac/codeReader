# jit_rad：跨域即时读回

## 1. 本讲目标

本讲承接 u4-l2「存储网关与地址空间序列化」留下的悬念——**localbus 的读侧跨域为什么难**，并给出 Bedrock 的工程化答案：`jit_rad`（Just In Time Readback Across Domains）。

读完本讲，你应该能够：

- 说清「为什么 localbus 写侧跨域容易、读侧跨域难」，以及「假装没有 CDC 问题」这种常见坏实践错在哪里。
- 复述 jit_rad 的核心策略：利用 UDP 包到来前的几百纳秒预警，把另一时钟域的 16 个字预先搬进一块双端口 `dpram`，再让 localbus 在自己的时钟域里安全地读回。
- 读懂 `jit_rad_gateway.v` 的两组接口（localbus 侧 + xfer/app_clk 侧）、`passthrough` 参数的两种行为，以及 `lb_prefill` / `xfer_snap` / `lb_error` 三个关键信号的含义。
- 会用 `cdc_snitch`（u6-l1 详讲）验证本模块的 CDC 正确性，并能解释「把 `passthrough` 设为 1 会让 CDC 检查惨败」的根本原因。
- 看懂 `jit_rad_gateway_demo.v` 是如何把 localbus 主桥、`jit_rad_gateway`、app 侧 16 选 1 多路选择器这三者连成一个可仿真、可上 UDP 端口的完整系统的。

## 2. 前置知识

本讲默认你已经掌握以下概念（若生疏，请先回看对应讲义）：

- **localbus 总线**（u2-l2）：Bedrock 自用的轻量片上总线，`lb_clk/lb_addr/lb_strobe/lb_rdata`，**无握手、无等待状态**。写侧只需把整组总线复制一份搬到目标时钟域即可；读侧因为「无握手」而很难跨域。
- **存储网关 mem_gateway / LASS 协议**（u4-l2）：用「综合期固定的读往返延迟」让无握手 localbus 能被 UDP 可靠读出。它要求延迟在综合时就被钉死为一个常数。
- **CDC 基础**（u4-l1）：单比特用两级同步器；多位数据绝不能逐位打两拍，必须「源域稳定锁存 + 跨域 gate 资格认证」。三个原子积木：`reg_tech_cdc`（带 `ASYNC_REG`/`magic_cdc` 属性的同步器原子）、`flag_xdomain`（单比特事件跨域）、`data_xdomain`（多位数据跨域）。
- **dpram**：双端口存储器，两个端口各有独立时钟，是 FPGA 里被厂家工具认可的「合法跨域原语」（会被识别为块 RAM）。

一个关键直觉（贯穿全讲）：**写侧跨域是单向「推」数据，目的域什么时候取都行；读侧跨域是「请求—响应」往返，必须约好「数据什么时候准备好」。** localbus 偏偏没有握手信号来约定这个时间——这正是 u4-l2 用「固定延迟」、本讲用「预警 + 预搬移」各自绕开它的根本动机。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `localbus/` 目录下，并依赖少量 `dsp/` 原语：

| 文件 | 作用 |
| --- | --- |
| [localbus/jit_rad.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md) | 设计文档：讲清「为什么需要 jit_rad」「整体策略」「使用前提与构建命令」。 |
| [localbus/jit_rad_gateway.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v) | **主模块**。两组接口、双域 `dpram` 缓冲、`passthrough` 参数、`lb_error` 检测。 |
| [localbus/jit_rad_gateway_demo.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v) | **演示工程**。把 localbus 主桥、`jit_rad_gateway`、app 侧 16 选 1 多路选择器、最小 localbus 寄存器实现拼成一个完整系统。 |
| [localbus/jit_rad_gateway_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_tb.v) | iverilog 测试台（WIP），生成 LASS 包并校验「同一包内重复读 16 个字得到相同结果」等语义。 |
| [dsp/dpram.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/dpram.v) | 双端口存储原语：A 口写、B 口读，两端口独立时钟。是 jit_rad 跨域搬运的载体。 |
| [dsp/flag_xdomain.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v) | 单比特事件跨域（toggle + 两级同步 + XOR 边沿检测）。主模块用它把 prefill 触发搬过时钟域。 |
| [localbus/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile) | 本目录构建脚本：`all` 目标里包含 CDC 检查产物 `jit_rad_gateway_demo_cdc.txt`。 |

> 提示：`reg_tech_cdc.v`、`mem_gateway.v`、`jxj_gate.v` 等依赖由 Makefile 的 `vpath` 从 `dsp/`、`badger/`、`board_support/` 自动搜入，本讲不展开它们的内部实现（分别见 u4-l1、u4-l2）。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先读文档理清「问题与策略」（4.1），再精读主模块的「双域缓冲机制」（4.2），最后看演示工程如何把一切组装起来并支持「原子快照」（4.3）。

### 4.1 文档先导：jit_rad 要解决的问题与整体策略

#### 4.1.1 概念说明

`jit_rad.md` 开篇点明了它要修掉的「设计缺陷」。在 LLRF（低电平 RF 控制）这类系统里，大量寄存器分布在好几个时钟域（如 `lb_clk` 控制域和 `adc_clk`/`app_clk` 应用域）。**写**这些寄存器很容易：把整组轻量 localbus 复制一份到每个目标域，在该域里就地解码、写寄存器，代价只是几拍延迟——而软件经网络控制本就不是实时的，这点延迟无所谓。

**读**就难了。u4-l2 的 `mem_gateway` 能让 localbus 被 UDP 读出，但它要求「读往返延迟在综合时被钉死成一个常数」。当被读的寄存器在**另一个时钟域**时，你没法保证那个域的数据在固定的那几拍里正好稳定可用。文档直言当时业界的一种常见坏实践：

> 一种典型做法就是直接无视 CDC 问题，赌「在域 A 读域 B 的寄存器时它们不会被改坏」。

这本质就是把多位异步数据直接接进同步器的输入——u4-l1 已论证过这是错的。`jit_rad` 的存在就是为了把这类代码替换成 CDC 正确的版本，而且**几乎不需要改上层代码**。

#### 4.1.2 核心流程

`jit_rad` 的策略可以浓缩成一句话：**与其在读发生时去跨域抓数据，不如在读发生之前就把数据搬过来放着。**

关键在于：一个 LASS UDP 包在「真正发起读周期」之前，会先有一段可观的**预警时间（warning window）**。文档给了两个平台的实测值：

- QF2-pre：周期慢（约 20 ns），包首是 8 字节 nonce，预警约 **300 ns**。
- Packet Badger：`raw_l` 信号在 client 收到任何数据前约 **352 ns** 就已拉高。

在这段时间里，让应用侧时钟（如 `app_clk`）跑 16 圈，就能把 16 个字依次读进一块 `16×32` 的 `dpram`。之后 localbus 在 `lb_clk` 域里读这块 `dpram`，就是同域读了——CDC 问题消失。

**时序预算**（决定方案是否可行的核心不等式）：

\[
T_{\text{warn}} \;>\; N_{\text{words}} \cdot T_{\text{app}} + T_{\text{sync}}
\]

其中 \(N_{\text{words}}=16\)，\(T_{\text{app}}\) 是应用时钟周期，\(T_{\text{sync}}\) 是触发跨域同步（`flag_xdomain`）带来的固定几拍。代入实测：取 `app_clk≈91 MHz`（即测试台里用的 11 ns 周期），\(16 \times 11\text{ ns} \approx 176\text{ ns}\)，远小于 300 ns，余量充足。

整条流程用伪代码表示：

```
# 包到达前夕（lb_clk 域）
mem_gateway/jxj_gate 检测到包将至 → 拉高 lb_prefill 一拍

# 触发跨域（lb_clk → app_clk）
flag_xdomain 把 lb_prefill 跨成 app_prefill 单拍脉冲

# app_clk 域：边走地址边写 dpram（约 17 拍）
xfer_snap  ← app_prefill          # 给应用侧做「原子快照」的钩子
for addr in 0..15:
    xfer_addr  ← addr
    dpram[addr] ← mux(xfer_addr)  # 写端口 A（app_clk）

# lb_clk 域：包的真正读周期来了，直接读 dpram
lb_odata ← dpram[lb_addr]          # 读端口 B（lb_clk），同域，安全
```

**代价与取舍**（文档讲得很坦诚，必须记住）：

- **同一包内不能反复轮询某个信号**——你当然可以多次读同一地址，但每次都会得到**同一个值**，因为快照只在包首拍了一次。这正是本讲测试台要校验的语义。
- **默认不是一次性原子快照**：16 个字是 app_clk 连续 16 拍逐个采的，若其间应用寄存器在变，16 个字之间可能轻微不一致。硬件 footprint 很小，且已无改地接入 lcls2_llrf。
- **想要真正的原子快照**？模块提供了一个 `xfer_snap` 钩子，让你在快照那一拍把若干（甚至全部）寄存器同时锁存——见 4.3。

#### 4.1.3 源码精读

文档先点明读侧跨域的难点和那种「无视 CDC」的坏实践：

[localbus/jit_rad.md:16-20](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L16-L20) —— 说明读跨域比写跨域难（`mem_gateway` 需要综合期固定延迟），并点名「直接无视 CDC、赌寄存器不被改坏」的典型坏实践，本模块正是为替换它而生。

接着是核心策略段，给出预警时间与「16 字搬进 dpram」的方案：

[localbus/jit_rad.md:22-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L22-L32) —— QF2-pre 约 300 ns、Packet Badger 约 352 ns 的预警时间，足够让 app_clk 跑 16 圈把 16 个值读进 `16×32 dpram`。

代价与原子快照钩子：

[localbus/jit_rad.md:34-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L34-L42) —— 同包内重复轮询必得相同值；默认非原子快照；想要原子捕获可用 `xfer_snap` 钩子（见 demo）。

最后是使用前提：在一台装了 iverilog/verilator/yosys 的 Linux 上，`make` 会做四件事，其中第二件就是「测 CDC 正确性」：

[localbus/jit_rad.md:86-97](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L86-L97) —— `make` 在本目录会：① iverilog 语法检查（QF2 与 Badger 两种配置）；② **CDC 正确性检查**；③ 编译可挂 live UDP 端口的 Verilator 仿真器 `Vjit_rad_gateway_demo`；④ 编译并跑 iverilog 回归测试。

> 注意：文档特别提醒，`yosys` 对属性的处理在 0.38 才修好；0.37 及更早会丢失 `magic_cdc` 属性，导致 `cdc_snitch` 输出误导。本讲的 CDC 实践请用 yosys ≥ 0.38。

#### 4.1.4 代码实践

**实践目标**：用文档给的数字亲手验证「16 字搬移」确实塞得进预警窗口，建立对方案可行性的直觉。

**操作步骤**：

1. 打开 [localbus/jit_rad.md:22-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L22-L32)，记下两个预警时间（300 ns / 352 ns）和 nonce 字节数（8）。
2. 打开测试台 [localbus/jit_rad_gateway_tb.v:22-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_tb.v#L22-L24)，读出 `app_clk` 的周期（`#6; app_clk=1; #5; app_clk=0;` → 11 ns/周期，约 91 MHz）。
3. 用上面的不等式算：填满 16 字 ≈ \(16 \times 11 = 176\) ns（源码注释说「a bit more than 17 cycles」，再算上 `flag_xdomain` 同步的 2~3 拍也就 ≈ 200 ns）。

**需要观察的现象 / 预期结果**：

- 两个平台的预警时间（300 ns、352 ns）都 > 200 ns，余量分别为约 100 ns、150 ns，方案成立。
- 结论写一句：**jit_rad 的可行性完全建立在「预警时间 > 填充时间」这个不等式上**；若哪天包首 nonce 变短或 app_clk 变慢，这个余量就被压缩——这正是 `lb_error` 要守护的边界（见 4.2）。

> 本实践为源码阅读+数值核算型，无需运行仿真即可完成；若想跑数字可随手用 `python3 -c "print(16*11, 300-16*11)"`。

#### 4.1.5 小练习与答案

**练习 1**：为什么「写侧跨域用复制整组 localbus」可行，而「读侧跨域」却不能照搬同样的办法？

> **答**：写侧是单向「推」数据，目的域只要在自己时钟里把写译码做掉即可，慢几拍无所谓；读侧是「请求—响应」往返，需要约定「数据在哪一拍准备好」，而 localbus 没有握手信号来做这个约定，`mem_gateway` 又要求综合期固定延迟——另一时钟域的数据无法保证在固定拍上稳定。

**练习 2**：文档说「同一包内不能反复轮询一个信号，否则必得相同值」。这与 `mem_gateway`（u4-l2）的「固定延迟读」有什么本质区别？

> **答**：`mem_gateway` 的固定延迟读每次都在读「当下」的总线值（只是延迟固定），所以同一包内多次读能看到变化；而 jit_rad 在包首就把 16 个字**快照进 dpram**，之后整包读的都是这份静态快照，故必相同。前者是「延迟固定」，后者是「先快照后读」。

---

### 4.2 主模块 jit_rad_gateway：双域 dpram 缓冲与 passthrough

#### 4.2.1 概念说明

`jit_rad_gateway` 是整个机制的载体。它有**两组接口**，分属两个时钟域：

- **localbus 侧（`lb_clk` 域）**：`lb_clk`、`lb_addr[3:0]`、`lb_strobe`、`lb_odata[31:0]`，外加控制信号 `lb_prefill`（输入，触发搬移）、`lb_error`（输出，报告时序违约）。注意地址只有 4 位——正好寻址 16 个字。
- **xfer / app 侧（`app_clk` 域）**：`app_clk`（输入）、`xfer_clk`（输出，正常接到 `app_clk`）、`xfer_strobe`、`xfer_addr[3:0]`、`xfer_odata[31:0]`（外部 16 选 1 多路选择器的结果）、`xfer_snap`（输出，原子快照钩子）。

把两个域粘起来的是一块 `16×32` 的 `dpram`：A 口在 `app_clk` 写、B 口在 `lb_clk` 读。`dpram` 是厂家工具认可的「合法跨域原语」（会被识别为块 RAM），所以数据跨域这一步是合规的——前提是「写完成之后再读」，而这点由预警窗口 + `lb_error` 共同保证。

模块还有一个参数 `passthrough`，它用一个 `generate if` 在两种实现间二选一：

- `passthrough = 0`（**缓冲模式，CDC 正确**）：真正把两个域隔开，走 dpram 缓冲。
- `passthrough = 1`（**直通模式，零开销但不解决 CDC**）：把 xfer 信号直接接到 localbus，复刻「老式坏 CDC」行为。

> ⚠️ 一个容易踩的坑：源码里 [jit_rad_gateway.v:13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L13) 的参数默认值是 `passthrough = 1`（直通/坏 CDC），而文档措辞「默认启用完整功能」与代码并不一致——**真正正确的用法是显式置 `passthrough = 0`**。演示工程正是通过自己的 `passthrough=0` 默认值覆盖了它（见 4.3）。以源码为准。

#### 4.2.2 核心流程

**缓冲模式（`passthrough = 0`）的状态机**：

```
1. 触发跨域：lb_prefill(lb_clk) --flag_xdomain--> app_prefill(app_clk 单拍脉冲)
2. xfer_snap = app_prefill                              # 给应用侧的原子快照钩子
3. app_clk 域计数器 addr1 从 0 走到 15 再回 0（约 17 拍）：
     xfer_addr = addr1                                  # 驱动外部多路选择器
     xfer_strobe = app_prefill | (addr1 != 0)           # 走完 16 个地址
     dpram.A口 写入 mux(addr1) 的结果
4. lb_clk 域：lb_strobe 来时，dpram.B口[lb_addr] → lb_odata（同域读，安全）
5. lb_error = (lb_prefill | lb_strobe) & (lb_pending | lb_running)
     —— 若「上一笔搬移还没完」就又来 prefill 或读，即报错
```

三个控制信号的含义务必记牢：

- **`lb_prefill`**（输入）：localbus 主桥在「包到达前夕」发出的单拍脉冲，意思是「趁现在，赶紧把 16 个字搬过来」。它来自 `mem_gateway`（`control_prefill`）或 `jxj_gate`。
- **`xfer_snap`**（输出）：`app_prefill` 在 app_clk 域的镜像脉冲，标示「快照时刻」。应用侧可在这一拍**同时**锁存多个寄存器，实现真正的原子快照（4.3 演示）。
- **`lb_error`**（输出）：时序假设的「守夜人」。若 prefill 到来后、dpram 还没填完（`lb_pending` 或 `lb_running` 仍有效）期间又来了 prefill 或 strobe，就拉高——说明预警窗口不够用了。

**直通模式（`passthrough = 1`）**则把上面整套机制短路掉：`xfer_clk = lb_clk`、`xfer_addr = lb_addr`、`lb_odata = xfer_odata`、`lb_error = 0`、`xfer_snap = 1`。后果在 4.2.4 实践里亲眼验证。

#### 4.2.3 源码精读

模块的端口与参数——注意 `passthrough = 1` 这个「危险默认值」：

[localbus/jit_rad_gateway.v:11-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L11-L30) —— 声明 `dw=32`、`passthrough=1`，以及 localbus 侧（`lb_*`）与 xfer/app 侧（`xfer_*`、`app_clk`）两组接口、`lb_prefill`/`lb_error` 控制位。

直通分支：零开销、不解决 CDC（这就是「老式坏实践」的等价物）：

[localbus/jit_rad_gateway.v:32-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L32-L39) —— `assign xfer_clk=lb_clk; xfer_addr=lb_addr; lb_odata=xfer_odata; lb_error=0; xfer_snap=1;`。把外部多路选择器的地址/时钟都直接交给 localbus 域，数据再组合地回送——多比特异步跨越，无任何同步器。

缓冲分支的入口：把 `xfer_clk` 真正切到 `app_clk`，并用 `flag_xdomain` 把 prefill 触发安全地搬过域：

[localbus/jit_rad_gateway.v:45-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L45-L50) —— `assign xfer_clk = app_clk;` 实例化 `flag_xdomain trig`，把 `lb_prefill`（lb_clk）跨成 `app_prefill`（app_clk），并 `assign xfer_snap = app_prefill;`。

> 旁证 `flag_xdomain` 的实现：[dsp/flag_xdomain.v:9-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flag_xdomain.v#L9-L21) 用 toggle → `reg_tech_cdc` 两级同步 → XOR 边沿检测，把单比特脉冲安全地搬到目的域。`reg_tech_cdc` 内部带 `ASYNC_REG="TRUE"` 与 `magic_cdc` 属性（[dsp/reg_tech_cdc.v:18-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_tech_cdc.v#L18-L24)），这正是 `cdc_snitch` 识别「合法跨域点」的锚点（u6-l1）。

app_clk 域的计数器与 dpram 写入——这就是「16 拍搬 16 字」的主体：

[localbus/jit_rad_gateway.v:52-66](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L52-L66) —— `addr1` 计数器在 `app_prefill | app_running` 时自增，走遍 16 个地址；`xfer_addr = addr1` 驱动外部多路选择器；结果写入 `dpram` A 口（`clka=app_clk`）。其中 `dpram #(.aw(4), .dw(dw)) buff` 这一行实例化了那块 `16×32` 双端口存储。

`dpram` 本体：A 口写、B 口读、两端口独立时钟——合法的跨域载体：

[dsp/dpram.v:4-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/dpram.v#L4-L17) —— 端口声明：`clka/addra/douta/dina/wena`（A 口可写可读）与 `clkb/addrb/doutb`（B 口只读），两端口时钟独立。写入逻辑 [dsp/dpram.v:38-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/dpram.v#L38-L44)：A 口 `posedge clka` 写 `mem[addra]`，B 口 `posedge clkb` 仅寄存读地址——读侧完全同步于 `lb_clk`。

最后是 `lb_error` 的守夜逻辑：

[localbus/jit_rad_gateway.v:68-74](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L68-L74) —— `lb_pending` 在 prefill 到来时置 1、在 app 侧开始运行（`lb_running`）时清 0；`lb_running` 是 app 侧 `app_running` 单比特跨域后的镜像；`lb_error = (lb_prefill | lb_strobe) & (lb_pending | lb_running)`，即「搬移未完成期间又来 prefill 或读」即报错。

#### 4.2.4 代码实践（本讲核心实践之一）

**实践目标**：亲手让 CDC 检查失败，从而真正理解「直通模式为什么是坏 CDC」。

**操作步骤**：

1. 先确认基线正确：在仓库根运行
   ```bash
   make -C localbus jit_rad_gateway_demo_cdc.txt
   ```
   （这是 `all` 目标里的 CDC 产物，见 [localbus/Makefile:11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L11)。它由 `top_rules.mk` 的 `%_cdc.txt` 模式规则生成：先 `yosys` 出 `jit_rad_gateway_demo_yosys.json`，再跑 `cdc_snitch.py`，见 [build-tools/top_rules.mk:159-164](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L159-L164)。）此刻 `passthrough=0`，应通过。
2. 把演示工程的 `passthrough` 改成 1：编辑 [localbus/jit_rad_gateway_demo.v:4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L4)，把 `parameter passthrough=0` 改为 `parameter passthrough=1`。
   > 为什么改 demo 而不是文档说的「改 jit_rad_gateway.v 的默认值」？因为 demo 在 [第 73 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L73) 用 `#(.passthrough(passthrough))` **显式覆盖**了主模块的默认值——CDC 检查跑的是 demo 这个顶层，所以真正生效的是 demo 的参数。
3. 重新生成并查看：`make -C localbus jit_rad_gateway_demo_cdc.txt`，然后查看产物文件内容（`cdc_snitch` 会列出违规跨域路径）。

**需要观察的现象 / 预期结果**：

- CDC 检查**惨败**（文档原话："It will fail badly!"，见 [localbus/jit_rad.md:104-105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L104-L105)），报告存在未被 `magic_cdc` 同步器保护的跨域路径。

**说明原因（要点）**：

- 直通模式下 [jit_rad_gateway.v:34-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L34-L37) 把 `xfer_clk=lb_clk`、`xfer_addr=lb_addr`、`lb_odata=xfer_odata`。
- 于是外部 16 选 1 多路选择器（demo 里 `always @(posedge xfer_clk)`，读的是 **app_clk 域、且持续在变**的 `aset[]` 寄存器，见 4.3）现在被 **lb_clk** 采样——这是一个多位（32 位数据 + 4 位地址）的异步跨越，中间没有任何 `flag_xdomain` / `reg_tech_cdc` / `dpram` 这类受认可的保护。
- `cdc_snitch` 看到跨时钟域的位级路径没有 `magic_cdc` 锚点，判定违规。缓冲模式之所以能过，正是因为数据走 `dpram`、触发走 `flag_xdomain`，全是被认可的原语。

> 若本机未装 yosys（≥0.38）或 cdc_snitch 链路不全，本步骤为「待本地验证」；此时可改为源码阅读型实践：对照 [jit_rad_gateway.v:32-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L32-L39) 与 [:45-66](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L45-L66)，自行列举直通模式有哪几条「无保护的跨域位路径」，从而预测 cdc_snitch 会报什么。

#### 4.2.5 小练习与答案

**练习 1**：缓冲模式下，数据跨域走 `dpram`、触发跨域走 `flag_xdomain`。为什么触发可以只用 `flag_xdomain`（单比特），而数据必须用 `dpram`？

> **答**：触发是一个「事件」（某一拍发生），单比特，用 toggle+同步器的 `flag_xdomain` 即可安全跨越；数据是 32 位多位总线，逐位打两拍会因各比特跌落时刻不同而撕裂，必须用「源域稳定写入 + 目的域安全读出」的双端口 `dpram`，且靠预警窗口保证「写完再读」。

**练习 2**：`lb_error` 的表达式是 `(lb_prefill | lb_strobe) & (lb_pending | lb_running)`。请用自然语言翻译它在「报警」什么。

> **答**：当一次搬移还在进行中（`lb_pending`=已请求但 app 侧尚未开跑，或 `lb_running`=app 侧正在填 dpram）时，如果 localbus 又发来新的 prefill 或发起读 strobe，就报警——因为这意味预警窗口被压缩、dpram 可能还没填好就被读，会读到脏数据。

**练习 3**：源码里 `jit_rad_gateway.v` 的 `passthrough` 默认值是 1（坏 CDC），但 demo 又把它覆盖成 0。这种「默认值不安全、靠调用方纠正」的设计有什么利弊？

> **答**：好处是模块单独存在时零开销、便于快速原型；坏处是「默认即危险」，调用方一旦忘记显式置 0 就会悄悄引入 CDC 违规，且只会在跑 cdc_snitch 时才暴露。这也是为什么 Bedrock 要把 cdc_snitch 放进 CI（u6-l1）——靠形式化检查兜住这类「默认值陷阱」。

---

### 4.3 演示工程 jit_rad_gateway_demo：xfer 多路选择器、原子快照与自检

#### 4.3.1 概念说明

光有 `jit_rad_gateway` 还跑不起来——它需要三样外部配合：① 一个 localbus 主桥（产生包、发 prefill/strobe）；② 一个 app_clk 域的「16 选 1 多路选择器」（按 `xfer_addr` 把 16 个应用寄存器之一送到 `xfer_odata`）；③ 一点点最小 localbus 寄存器实现（让总线有东西可读、可写）。`jit_rad_gateway_demo.v` 就是把这三样和 `jit_rad_gateway` 拼在一起的**完整可仿真系统**，并且：

- 主桥可二选一：`mem_gateway`（Packet Badger 路径，默认）或 `jxj_gate`（QF2-pre 路径，`+define+QF2`），由预处理宏切换，对应文档说的两个预警时间来源。
- 演示了**原子快照**：用 `xfer_snap` 在快照那一拍同时锁存 `test_1`/`test_2`，得到一份一致的多寄存器快照。
- 内置**自检计数器**：`err_cnt`（累计 `lb_error` 次数）和 `pack_cnt`（累计 `lb_prefill` 即包数），拼成 `xfer_status` 暴露给 localbus，便于软件查 «有没有发生过时序违约»。

#### 4.3.2 核心流程

一个 LASS 包在 demo 里的完整旅程：

```
[网络侧 net_idata]（字节流）
   │  posedge lb_clk 打一拍 → net_idata_r（标 magic_cdc，告诉 cdc_snitch 这是受控入口）
   ▼
mem_gateway(.idata=net_idata_r, .raw_l/raw_s/len_c)   # Packet Badger 路径
   │  解出 localbus 周期：lb_addr/lb_strobe/lb_rd/lb_dout(写数据)
   │  并产出 control_prefill → lb_prefill（包首预警）
   ▼
┌─────────────────── lb_clk 域读多路选择器 ───────────────────┐
│ casez(lb_addr_r):                                            │
│   24'h????0? : lb_din ← reg_bank_0   (hello/xfer_status)     │
│   24'h????6? : lb_din ← lb_reg_bank_6(jit_rad 的 16 字快照)  │
└──────────────────────────────────────────────────────────────┘
   ▲ lb_reg_bank_6 来自：
   │
jit_rad_gateway xfer_bank_6(.lb_addr=lb_addr[3:0], .lb_odata=lb_reg_bank_6,
                             .lb_prefill=lb_prefill, .lb_error=lb_error, ...)
   │ xfer_clk/xfer_strobe/xfer_addr/xfer_snap → app_clk 域
   ▼
┌─────────────────── app_clk 域 16 选 1 多路选择器 ───────────────────┐
│ always @(posedge xfer_clk) if (xfer_strobe)                          │
│   case(xfer_addr) reg_bank_6 ← aset[0..15] 或 test_1/test_2  ──→ xfer_odata │
└──────────────────────────────────────────────────────────────────────┘
   │ aset[] 在 app_clk 域持续变化（可被设为 static 或 dynamic 序列）
   │ xfer_snap 那一拍：test_1←aset[1], test_2←aset[2]（原子快照）
```

地址映射要点：localbus 地址低 4 位为 `0x?` 走 `reg_bank_0`（hello 字符串 + 状态），低 4 位为 `0x6?`（即 `0x60`–`0x6f`）走 jit_rad 的 16 字快照。测试台正是连读 `0x60`–`0x6f` 这 16 个字。

#### 4.3.3 源码精读

主桥（Packet Badger 路径）——把网络字节流解成 localbus 周期，并产出 prefill：

[localbus/jit_rad_gateway_demo.v:57-66](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L57-L66) —— 实例化 `mem_gateway badgergate`（`n_lat=10`），输入 `len_c_r/raw_l_r/raw_s_r/net_idata_r`，输出 `lb_addr/lb_strobe/lb_rd/lb_dout`，并把 `control_prefill` 接成 `lb_prefill`。网络入口 `net_idata_r` 在 [第 26-27 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L26-L27) 被标了 `magic_cdc`，告诉 `cdc_snitch` 这是「受控的跨域入口」、不要误报。

主模块的实例化——注意它把 demo 的 `passthrough` 透传进去：

[localbus/jit_rad_gateway_demo.v:73-79](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L73-L79) —— `jit_rad_gateway #(.passthrough(passthrough)) xfer_bank_6`，localbus 侧接 `lb_addr[3:0]/lb_strobe/lb_odata→lb_reg_bank_6/lb_prefill/lb_error`，app 侧接 `app_clk/xfer_clk/xfer_strobe/xfer_addr/xfer_odata←reg_bank_6/xfer_snap`。

app_clk 域的 16 选 1 多路选择器——这就是文档说的「external 16-in 32-bit wide multiplexer」：

[localbus/jit_rad_gateway_demo.v:110-131](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L110-L131) —— `always @(posedge xfer_clk) if (xfer_strobe) case(xfer_addr)` 把 `aset[0..15]`（及 `test_1/test_2`）之一送到 `reg_bank_6`（即 `xfer_odata`）。`aset[]` 本身是 app_clk 域、持续变化的寄存器组（[第 156-165 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L156-L165)）。

lb_clk 域的读多路选择器——决定每个 localbus 地址读谁：

[localbus/jit_rad_gateway_demo.v:133-139](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L133-L139) —— `casez(lb_addr_r)`：低 4 位 `0?` 选 `reg_bank_0`，`6?` 选 jit_rad 输出 `lb_reg_bank_6`，其余给 `0xfaceface` 占位。

原子快照钩子的用法——在 `xfer_snap` 那一拍**同时**锁存多个寄存器：

[localbus/jit_rad_gateway_demo.v:145-149](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L145-L149) —— `always @(posedge app_clk) if (xfer_snap) begin test_1<=aset[1]; test_2<=aset[2]; end`。注释强调：想要多寄存器一致快照就用这个钩子（但别对 register 0 这么做，它本身已在 snap 拍被采）。

自检计数器——`err_cnt`/`pack_cnt` 拼成可被软件读取的状态：

[localbus/jit_rad_gateway_demo.v:182-188](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L182-L188) —— `lb_error` 累加进 `err_cnt`、`lb_prefill` 累加进 `pack_cnt`，合成 `xfer_status = {12'b0, err_cnt, pack_cnt}`，注释说「比看起来更重要」，因为它让软件能监控时序违约。

测试台对快照语义的校验——同一包内读两遍 `0x60`–`0x6f` 必须相同：

[localbus/jit_rad_gateway_tb.v:46-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_tb.v#L46-L50) 与 [localbus/jit_rad_gateway_tb.v:104-109](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_tb.v#L104-L109) —— 包内先读 16 字（`0x60`–`0x6f`）、再读 5 字、写 `0x100`、又读一遍 16 字；若两遍结果不一致则 `fail=1`。这把文档「同包内重复读必得相同值」的语义落成了断言。

#### 4.3.4 代码实践（本讲核心实践之二）

**实践目标**：把 localbus 主桥、`jit_rad_gateway`、app 侧 16 选 1 多路选择器这三者的连接关系画清楚，从而在脑中建立「包→prefill→快照→读回」的完整数据通路。

**操作步骤**：

1. 通读 [localbus/jit_rad_gateway_demo.v:69-79](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L69-L79)，列出 `jit_rad_gateway` 实例 `xfer_bank_6` 的每个端口连到了 demo 里的哪根线。
2. 对照 [:110-131](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L110-L131)（app 侧 mux）与 [:133-139](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L133-L139)（lb 侧读 mux）。
3. 画一张三框图：**localbus 主桥（mem_gateway）** ↔ **jit_rad_gateway** ↔ **app 侧 16 选 1 mux（aset[]）**，标注每个箭头的信号名、位宽、所属时钟域。

**需要观察的现象 / 预期结果（参考答案图）**：

```
 ┌──────────────────┐  lb_addr[23:0], lb_strobe, lb_rd, lb_dout[31:0]   ┌──────────────────────┐
 │  mem_gateway     │ ─────────────────────────────────────────────────▶ │  lb_clk 域读 mux      │
 │  (lb_clk 主桥)   │ ◀───────────────────────────────────────────────── │  casez(lb_addr_r)     │
 │                  │   lb_prefill(=control_prefill), lb_odata 回送       │  → reg_bank_0         │
 │  net_idata(net)  │                                                     │  → lb_reg_bank_6 ◀────┐
 └──────────────────┘                                                     └──────────────────────┘ │
        │ lb_prefill                                                                                │
        ▼                                                                                           │
 ┌─────────────────────────────────────── jit_rad_gateway (xfer_bank_6) ──────────────────────────┐ │
 │ lb_clk 侧: lb_addr[3:0], lb_strobe, lb_prefill, lb_error ──┐                                    │
 │                          │ dpram.B口(lb_clk)读 → lb_odata = lb_reg_bank_6 ────────────────────┘ │
 │                          │                                                                      │
 │ app_clk 侧: xfer_clk(=app_clk), xfer_snap, xfer_strobe, xfer_addr[3:0] ──────┐                  │
 │   触发: lb_prefill --flag_xdomain--> app_prefill = xfer_snap                 │                  │
 │   计数: addr1 走 0..15, dpram.A口(app_clk)写 ← xfer_odata                    │                  │
 └──────────────────────────────────────────────────────────────────────────────┼──────────────────┘
                                                                                │ xfer_addr/strobe/clk
                                                                                ▼
                                                       ┌────────────────────────────────┐
                                                       │ app_clk 域 16 选 1 mux          │
                                                       │ always @(posedge xfer_clk)      │
                                                       │   case(xfer_addr)               │
                                                       │     reg_bank_6 ← aset[0..15]    │
                                                       │                ← test_1/test_2  │
                                                       │   → xfer_odata ─────────────────┼─▶ 回 jit_rad 的 dpram.A口
                                                       │ xfer_snap 拍: test_1<=aset[1]   │
                                                       │              test_2<=aset[2]    │
                                                       └────────────────────────────────┘
```

**要点自查**：

- `lb_prefill` 由主桥产生、进入 `jit_rad_gateway`，经 `flag_xdomain` 跨到 app_clk 域变成 `xfer_snap`/`app_prefill`，驱动 app 侧 mux 走 16 个地址。
- app 侧 mux 的输出 `xfer_odata` 回流进 `jit_rad_gateway` 的 dpram A 口；localbus 读时从 dpram B 口取出 `lb_reg_bank_6`，再经 lb_clk 读 mux 送到 `lb_din`。
- 两个 mux 处于不同时钟域：lb_clk 读 mux 在 `lb_clk`，app 侧 16 选 1 mux 在 `xfer_clk`（=app_clk）。把它们隔开的正是 `jit_rad_gateway` 的 dpram。

> 本实践为源码阅读+画图型，无需运行仿真。若想动态验证，可跑 `make -C localbus jit_rad_gateway_check`（依赖 `jit_rad_gateway_tb`，见 [Makefile:11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L11)），观察测试台对 `0x60`–`0x6f` 两次读取一致性的断言是否 PASS——这部分回归检查文档标注为 WIP，结果以本地实际为准。

#### 4.3.5 小练习与答案

**练习 1**：demo 里 localbus 地址 `0x60`–`0x6f` 为什么读到的就是 app_clk 域 `aset[]` 的快照？请沿着信号追一遍。

> **答**：`0x6?` 命中 lb_clk 读 mux 的 `24'h????6?` 分支，选 `lb_reg_bank_6`；它是 `jit_rad_gateway` 的 `lb_odata`，即 dpram B 口（lb_clk）按 `lb_addr[3:0]` 读出的值；dpram A 口（app_clk）在 prefill 后被 app 侧 16 选 1 mux 的输出 `xfer_odata`（按 `xfer_addr` 取自 `aset[]`）逐地址填满。所以 `0x60`–`0x6f` 对应 `aset[0]`–`aset[15]` 的快照。

**练习 2**：`xfer_snap` 钩子解决的是 4.1.2 里提到的哪个「代价」？它为什么能让多个寄存器「一致」？

> **答**：解决「默认非原子快照」——16 个字本是连续 16 拍逐个采的，若 `aset[]` 在变则字与字之间可能不一致。`xfer_snap` 是 prefill 到达 app_clk 域的**同一拍**脉冲，在该拍用一条 `always @(posedge app_clk) if (xfer_snap)` 同时锁存 `test_1`/`test_2` 等，于是这些寄存器被「定格」在同一边沿，彼此一致。

**练习 3**：`xfer_status = {12'b0, err_cnt, pack_cnt}` 被设计成可被 localbus 读取（地址低 4 位 `0x4`）。软件读它有什么用？

> **答**：`pack_cnt` 是已处理包数、`err_cnt` 是 `lb_error` 累计次数。软件周期性读它，就能监控「预警窗口是否够用」——若 `err_cnt` 不为 0，说明发生过「搬移未完成就被读」，需要排查 app_clk 频率、包首 nonce 长度或 localbus 节奏。这是把 4.1.2 那个时序不等式的「违约事件」暴露给软件的反馈通道。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯通任务。

**任务**：以「一次 LASS UDP 读 `0x60`–`0x6f`」为线索，写一份时序说明，覆盖以下要点（能用图就画图）：

1. **预警与触发**：包到达前夕，`mem_gateway` 何时拉高 `lb_prefill`？`jit_rad_gateway` 如何经 `flag_xdomain` 把它变成 app_clk 域的 `xfer_snap`/`app_prefill`？（对应 4.1、4.2）
2. **填充 dpram**：app_clk 域计数器走 16 个地址、把 `aset[]` 经 16 选 1 mux 写进 dpram A 口，约 17 拍。（对应 4.2、4.3）
3. **安全读回**：localbus 真正的读周期到达时，dpram B 口（lb_clk）按 `lb_addr[3:0]` 读出 `lb_reg_bank_6`，经 lb_clk 读 mux 送 `lb_din`。为什么这一步「没有 CDC 问题」？（对应 4.3）
4. **违约监控**：若 app_clk 异常变慢导致填不完，`lb_error` 如何拉高、`err_cnt` 如何累加、软件又如何经 `xfer_status` 看到？（对应 4.2、4.3）
5. **CDC 合规性**：把 `passthrough` 改成 1 会破坏上面哪一环？为什么 `cdc_snitch` 会报错？（对应 4.2 实践）

**进阶（可选，需 Verilator + 网络）**：按 [localbus/jit_rad.md:107-119](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L107-L119) 的指引，一个终端 `make -C localbus live`，另一个终端 `PYTHONPATH=$BEDROCK/badger sh localbus/stim.sh` 发包，再用 `gtkwave xfer_demo.vcd xfer_demo.gtkw` 看波形，亲眼观察 prefill → dpram 填充 → 读回的真实波形。若工具不全，此项标注「待本地验证」即可。

## 6. 本讲小结

- **读侧跨域难**的根因：localbus 无握手，无法约定「数据哪拍准备好」，而 `mem_gateway` 又要求综合期固定延迟——另一域的数据无法保证在固定拍稳定。
- **jit_rad 的策略**：用 UDP 包到来前 300~352 ns 的预警窗口，把 app_clk 域的 16 个字预先搬进一块 `16×32` 双端口 `dpram`，之后 localbus 在 lb_clk 域同域读回，CDC 问题消失。可行性等价于 \(T_{\text{warn}} > 16\cdot T_{\text{app}} + T_{\text{sync}}\)。
- **主模块 `jit_rad_gateway`** 有两组接口（lb 侧 + xfer/app 侧），用 `dpram` 跨数据、`flag_xdomain` 跨触发；`lb_prefill` 触发搬移、`xfer_snap` 提供原子快照钩子、`lb_error` 守护时序违约。
- **`passthrough` 参数**：`=0`（缓冲，CDC 正确，demo 默认）走 dpram；`=1`（直通，主模块的默认值）直接连通两域、复刻坏 CDC，会被 `cdc_snitch` 判违规——这是本讲亲手验证的核心结论。
- **演示工程**把 `mem_gateway`/`jxj_gate` 主桥、`jit_rad_gateway`、app_clk 域 16 选 1 mux 拼成完整系统，并用 `xfer_status`（`err_cnt`/`pack_cnt`）把时序违约暴露给软件。
- **语义边界**：同一包内重复读必得相同值（快照静态）；默认非原子，可用 `xfer_snap` 实现多寄存器一致快照。

## 7. 下一步学习建议

- **u6-l1 cdc_snitch：形式化跨域验证**——本讲反复出现的 `magic_cdc` 属性、`%_cdc.txt` 模式规则、yosys 流程都在那里详讲。学完后你会真正读懂 4.2.4 实践里 cdc_snitch 报告的每一行。
- **u6-l3 cmoc：低电平 RF 控制器（LLRF）**——`jit_rad` 最初就是为修掉 lcls2_llrf `application_top.v` 的 CDC 缺陷而生（见 [jit_rad_gateway.v:3-4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L3-L4) 注释）。cmoc 是它的「真实用户」，去看 `cryomodule.v` 如何用 `data_xdomain` 处理写侧、又如何用这类机制处理读侧。
- **延伸阅读**：本目录的 `jit_rad_gateway_tb.v`（回归测试，文档标注 WIP）和 `xfer_sim.cpp`（Verilator + live UDP），是把本讲从「读源码」推进到「跑起来」的下一步；配合 `badger/` 的 Packet Badger（u4-l4）理解 `raw_l` 那 352 ns 预警从何而来。
