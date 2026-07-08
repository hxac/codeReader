# 缓冲与停顿平滑

## 1. 本讲目标

上一讲（u10-l1）我们以 `Pipeline_Skid_Buffer` 为范本，把单个 ready/valid 握手如何「流水线化、又如何在两侧解耦」讲透了。Skid buffer 只有「输出寄存器 + 缓冲寄存器」两个存储位，它解决的是**切断组合路径**的问题，而非**吸收速率失配**的问题。本讲把存储位的数量从 2 扩展到任意深度，并引入两种新的控制思想——**信用（credit）流控**与**停顿平滑（stall smoothing）**——得到一族弹性缓冲构件。

学完后，你应该能：

- 掌握 **FIFO 缓冲**（`Pipeline_FIFO_Buffer`）如何用双口 RAM + 两套地址计数器 + 「绕回位」(wrap-around bit) 检测空/满，并理解它为何能把 skid buffer 推广到任意深度、且支持循环缓冲模式。
- 掌握 **Credit 缓冲**（`Pipeline_Credit_Buffer`）如何用「信用计数器」**先验地**算出剩余容量，从而把「切断长路径的流水线寄存器」与「吸收速率失配的 FIFO」两件事合到一个模块里，并在长流水线下比 skid buffer 链省资源。
- 掌握 **停顿平滑器**（`Pipeline_Stall_Smoother`）如何用一个两态 FSM + FIFO，在「已知最大输入停顿」的前提下，让输出一旦启动就连续不断流。
- 理解 **skid buffer 链**（`Skid_Buffer_Pipeline`）作为「只打拍、不平滑」的基线，并能对照四种缓冲给出**选型表**，回答「为偶发停顿的下游该选哪种、要多深」。

## 2. 前置知识

本讲假设你已经掌握（来自依赖讲义）：

- **u10-l1 Skid Buffer**：skid buffer 只有 2 个存储位，用 EMPTY/BUSY/FULL 三态、`insert`/`remove` 两次握手完成；普通模式缓存「最早」数据、满则强制输入输出交替，CBM（循环缓冲模式）缓存「最新」数据、满仍可同拍收发。本讲的 FIFO 缓冲正是「把 skid buffer 的 2 个位推广到 N 个位」。
- **u9-l1/u9-l2 握手**：`handshake_complete = (ready && valid)`；内部状态只在握手完成拍改变；接口内禁组合环。本讲所有缓冲的流控都建立在这套规则上。
- **u8-l2 计数器与函数**：`Counter_Binary = Adder_Subtractor_Binary + Register`；`clog2(N)=⌈log₂N⌉`；可复用函数（如 `max`）放在 `.vh` 文件里用 `` `include `` 引入。本讲的地址计数器、信用计数器、停顿计数器全是 `Counter_Binary`。
- **u7-l1 RAM 推断**：`RAM_Simple_Dual_Port` 是 1 写口 + 1 读口的同步双口 RAM，读口带 `rden`。本讲的 FIFO 存储体就是它。
- **u6-l1/u6-l2 寄存器家族**：`Register`（带 `clock_enable`/`clear`）、`Register_Toggle`（翻转）、`Register_Pipeline_Simple`（深度可 0 的简单流水线寄存器）。

还需两个常识：

- **弹性缓冲（elastic buffer）**：输入输出各自做 ready/valid 握手、内部有存储的构件；两侧速率可以瞬时不同，靠内部存储把差额吸收掉，这正是「延迟无关设计」(latency-insensitive design) 的基本积木。
- **速率失配（rate mismatch）**：上游有时连发、有时停发，下游有时连收、有时停收；若中间没有缓冲，上游的停顿会直接传到下游，造成输出断流。

## 3. 本讲源码地图

本讲精读四个文件，构成一条「从 2 个位到 N 个位、再到信用与停顿」的递进线：

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| `Pipeline_FIFO_Buffer.v` | **任意深度**的弹性缓冲（skid buffer 的推广） | 讲双口 RAM 存储体、地址计数器、绕回位空/满检测、循环缓冲模式 |
| `Pipeline_Credit_Buffer.v` | **信用流控**的流水线缓冲 | 讲信用计数器如何先验算容量、PIPE_DEPTH 寄存器如何切断长路径、最小 FIFO 深度公式 |
| `Pipeline_Stall_Smoother.v` | **吸收周期性停顿**的缓冲 | 讲两态 BUFFERING/SENDING FSM、触发延迟、`MAX_STALL_CYCLES` 如何换算 FIFO 深度 |
| `Skid_Buffer_Pipeline.v` | **只打拍不平滑**的基线 | 讲 skid buffer 链的 generate 展开、与三种缓冲的选型对比 |

四个模块全部自底向上由已学过的构建块拼装：`RAM_Simple_Dual_Port`、`Counter_Binary`、`Register`、`Register_Toggle`、`Register_Pipeline_Simple`、`Pipeline_FIFO_Buffer`、`Pulse_Latch`、`Pipeline_Gate`——没有一个模块自己写触发器或算术。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**FIFO 缓冲**、**Credit 缓冲**、**停顿平滑**、**Skid Buffer 链与选型**。

### 4.1 FIFO 缓冲（Pipeline_FIFO_Buffer）

#### 4.1.1 概念说明

`Pipeline_FIFO_Buffer` 是这一族的「主力缓冲」。它的自我定位很清楚：把 ready/valid 握手两侧解耦，允许背靠背连续传输，且**输入到输出之间没有组合路径**——从而把通路流水线化、提升并发与时序。和 skid buffer 相比，它有两个本质不同：

1. **任意深度**：存储体不再只有 2 个寄存器位，而是一块深度为 `DEPTH` 的双口 RAM，**且 `DEPTH` 不必是 2 的幂**（这是它相对传统 FIFO 实现的一个特点）。
2. **平滑速率失配**：因为它能存可变数量的数据，就能吸收输入输出两侧传输速率的**不规则波动**——上游连发时先攒着，下游停收时不堵死上游；下游连收时有得给，上游停发时不饿死下游。

[Pipeline_FIFO_Buffer.v:4-15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L4-L15) —— 定位：解耦握手两侧、消除组合路径、允许任意深度、输入到输出延迟 2 拍、可当循环缓冲；并指出它能平滑传输速率的不规则、在流水线环路里存够数据以防「人为死锁」（Kahn 过程网络的有界通道）。

它还提供一个可选的 **Circular Buffer Mode（CBM，循环缓冲模式）**，语义与 skid buffer 的 CBM 完全一致：

- 普通 mode：FIFO 满了就**不再完成输入握手**，直到有一笔被读走——缓存的是「**最早**」的数据。
- CBM（`CIRCULAR_BUFFER != 0`）：输入握手**永远可以完成**，满了就直接丢弃缓冲里最旧的那笔、换成新进的——缓存的是「**最新**」的数据。

[Pipeline_FIFO_Buffer.v:26-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L26-L43) —— CBM：满了仍可同拍又收又发，因为 `input_ready` 不再依赖缓冲的空/满状态，也不依赖输出握手状态（后者会制造组合路径，被禁止）。

#### 4.1.2 核心流程

FIFO 的硬件由三部分拼成，关键的工程难点是**怎么判空/判满**：

```
存储体:  RAM_Simple_Dual_Port (1 写口 + 1 读口, 同步)
         写口接 input_data, 读口接 output_data (寄存输出, 故输出延迟 2 拍)

地址:    两个 Counter_Binary (写地址/读地址), 都从 0 开始, 每次 +1, 越过 DEPTH-1 绕回 0

判空/判满的难点:
         "写地址 == 读地址" 既可能是空也可能是满! 必须再带一位"绕回位"区分:
         - 写地址比读地址多绕了一圈 -> 满 (绕回位不同)
         - 读地址追上写地址 -> 空 (绕回位相同)

控制:    input_ready  = 未满 (|| CBM)
         insert       = input_valid && input_ready   (写入 + 写地址前进)
         remove       = 输出侧握手完成 || (CBM 满时插入)  (读出 + 读地址前进)
```

判空/判满用到的「绕回位」技巧，本质上等价于把地址指针**多带一位**：地址本身用 `clog2(DEPTH)` 位去寻址 RAM，再用一个 1 位寄存器记录「这个地址有没有绕回过 0」。两个指针的绕回位相同表示没绕出差（空），不同表示绕出了一整圈差（满）。

\[ \text{ADDR\_WIDTH} = \lceil \log_2 \text{DEPTH} \rceil = \text{clog2}(\text{DEPTH}) \]

这样判空/判满只比较「地址相等」与「绕回位相等/不等」两个条件，**对任意正整数 `DEPTH` 都成立**，不必要求 `DEPTH` 是 2 的幂——这正是注释反复强调的「任意深度」的来源。

#### 4.1.3 源码精读

**端口与初始化。** 标准的输入/输出握手对，加 `DEPTH`/`RAMSTYLE`/`CIRCULAR_BUFFER` 三个参数；上电 `input_ready=1`（空时先收数据）：

[Pipeline_FIFO_Buffer.v:49-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L49-L71) —— 模块与端口；`input_ready` 是 `output reg`（来自本模块逻辑），`output_data`/`output_valid` 是 `output wire`（来自子实例）。

**常量。** 由 `DEPTH` 派生地址位宽与边界常量：

[Pipeline_FIFO_Buffer.v:78-85](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L78-L85) —— `ADDR_WIDTH=clog2(DEPTH)`、`ADDR_ONE`、`ADDR_ZERO`、`ADDR_LAST=DEPTH-1`。

**数据通路：双口 RAM 存储体。** 用 `RAM_Simple_Dual_Port` 做存储，写口插数据、读口取数据，`READ_NEW_DATA=0`（读旧值）；注释特别说明**不需要写转发**逻辑：

[Pipeline_FIFO_Buffer.v:89-133](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L89-L133) —— 存储体实例；普通模式绝不会同址并发读写，CBM 下同址并发读写（满时）返回的是已存数据而非新写数据，故按各自情形引导 CAD 工具处理读写地址碰撞即可拿到最高频率。

**读/写地址计数器。** 两个 `Counter_Binary`，都是「从 0 起、每次 +1、越过 `DEPTH-1` 装回 0」（`load` 覆盖 `run`）：

[Pipeline_FIFO_Buffer.v:137-194](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L137-L194) —— 写地址与读地址计数器；注释强调深度可为任意正整数。

**绕回位。** 用两个 `Register_Toggle`，每当对应地址越过 `ADDR_LAST` 装回 0 时翻转一次这一位：

[Pipeline_FIFO_Buffer.v:198-248](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L198-L248) —— 绕回位：写地址绕过读地址则满（绕回位不同）、读地址追上写地址则空（绕回位相同）；地址永远不会互相越过。

**控制通路：空/满状态。** 把上面两个条件落成两个组合信号：

[Pipeline_FIFO_Buffer.v:260-266](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L260-L266) —— `stored_items_zero`（地址等且绕回位等）、`stored_items_max`（地址等且绕回位不等）。

**输入接口（insert）。** 未满就 ready，握手完成即写入并推进写地址，越过 `ADDR_LAST` 则装回 0 并翻转绕回位：

[Pipeline_FIFO_Buffer.v:279-289](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L279-L289) —— `input_ready = (stored_items_max == 0) || (CIRCULAR_BUFFER != 0)`；CBM 下输入恒 ready。

**输出接口（remove）。** 因为输出是寄存输出，需区分「正常取走」(`remove_normal`)、`output_valid` 为 0 但缓冲非空时「离开空闲去取」(`output_leaving_idle`)，以及 CBM 满时插入触发的「丢弃最旧」(`remove_circular`)：

[Pipeline_FIFO_Buffer.v:307-324](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L307-L324) —— remove 的三种来源与 `load_output_register` 的派生；最后一项说明 `output_valid` 必须寄存以匹配缓冲输出寄存器的延迟。

**输出有效寄存。** `output_valid` 用一个 `Register` 打一拍，与 RAM 读口的寄存输出对齐：

[Pipeline_FIFO_Buffer.v:329-341](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L329-L341) —— `output_valid` 寄存，`clock_enable = load_output_register`，`data_in = (stored_items_zero == 0)`。

#### 4.1.4 代码实践

**实践目标**：追踪「绕回位」如何区分空与满，亲手验证「地址相等」这一歧义被化解。

**操作步骤**：

1. 假设 `DEPTH=4`，则 `ADDR_WIDTH=clog2(4)=2`，地址取值 0/1/2/3，`ADDR_LAST=3`。
2. 上电：写地址=读地址=0，两个绕回位都=0 → 由 [L260-264](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L260-L266) 得 `stored_items_zero=1`（**空**）。
3. 连续写入 4 笔（下游不取）：写地址依次 1→2→3→0，**第 4 笔**写地址越过 `ADDR_LAST(3)` 装回 0、写绕回位翻转 0→1。此时写地址=0、读地址=0，但写绕回位=1、读绕回位=0 → 由 [L265](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L260-L266) 得 `stored_items_max=1`（**满**）。
4. 此时 `input_ready = (stored_items_max==0) || CBM = 0`（普通模式不再收），第 5 笔被挡住。

**需要观察的现象**：空和满时「写地址==读地址」完全相同，**唯一区别**是两个绕回位是否相等。

**预期结果**：能口述「绕回位 = 地址指针的最高有效隐含位，把它显式存下来就能在任意深度下区分空/满」。逐拍时序验证需自行搭 testbench，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个 FIFO 允许 `DEPTH` 不是 2 的幂（比如 `DEPTH=5`）？传统 FIFO 往往要求 2 的幂。

**答案**：因为判空/判满只依赖「地址是否相等」和「绕回位是否相等」，与地址空间的 2 幂性无关。地址计数器在越过 `DEPTH-1` 时靠 `load_buffer_*_addr`（见 [L287](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L281-L289) 与 [L322](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_FIFO_Buffer.v#L307-L324)）显式装回 0，而不是靠地址自然溢出绕回，所以 `DEPTH=5` 也能正确工作。

**练习 2**：为什么注释说「不需要写转发逻辑」（`READ_NEW_DATA=0`）？skid buffer 也没有写转发，原因一样吗？

**答案**：普通模式下，同一个地址永远不会被同时读写（写总是领先读、写满了就停写），所以读到的总是已稳定的旧值，不需要写转发。CBM 下满时虽会同址并发读写，但按设计此时应返回**已存数据**（最旧值被丢弃前先送出），也正好不需要转发。这与 skid buffer「同一拍不会既覆盖又读取同一寄存器」是同一类原因——都是「先把数据放好再被读」的时序约定。

---

### 4.2 Credit 缓冲（Pipeline_Credit_Buffer）

#### 4.2.1 概念说明

`Pipeline_Credit_Buffer` 解决一个 FIFO 缓冲没解决的问题：**怎么在两侧之间插很多拍流水线寄存器来切断超长组合路径**。

回顾：FIFO 缓冲能把速率失配吸收掉，但它内部只有读口寄存输出那一拍延迟，**几乎不打拍**——如果两个接口之间的组合路径很长（比如跨半个芯片），FIFO 帮不上时序。skid buffer 链能打很多拍，但**不平滑速率失配**（下游停了，停顿迟早传到上游）。Credit 缓冲把两者合一：用 `PIPE_DEPTH` 级**普通寄存器**把数据/控制通路切断，再用一个 **FIFO** 在输出侧吸收速率失配。

它的核心思想是**信用（credit）流控**：输入接口「知道」下游 FIFO 的容量与流水线的延迟，于是维护一个「还能再发几笔」的计数器——

- 每完成一次输入握手（发出一笔）→ **信用减 1**；
- 每完成一次输出握手（下游取走一笔，这个信号经流水线延迟传回）→ **信用加 1**；
- 信用降到 0 → `input_ready` 拉低，**先验地**阻止上游继续发，保证 FIFO 不会溢出、在途数据不会丢。

注意这里的根本不同：FIFO 缓冲靠**比较读写地址**（结构性）发现「满了」；Credit 缓冲靠**信用计数器**（算术性）**提前算出**「还能收几笔」。后者不需要等数据物理上堆满才反应，所以能在中间隔着很多拍流水线寄存器的情况下仍然正确流控。

[Pipeline_Credit_Buffer.v:9-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L9-L31) —— 信用缓冲概述：输入接口跟踪已发/已收的传输数从而知道剩余容量、调制何时能再收；输出侧 FIFO 用来接住「输入暂时不收时仍在途」的数据；并指出对长流水线它比 skid buffer 链省资源（尤其 FIFO 装进 BRAM 时），还可能贴合 Intel 的 hyper-register（参考 Abbas & Betz, FPL 2018）。

#### 4.2.2 核心流程

信用缓冲的内部拓扑：**三条并行的寄存器流水线 + 一个信用计数器 + 一个输出 FIFO**。

```
方向:  input ──────────────────────► output   (数据正向流)
       input ◄────────────────────── output   (信用反向流)

正向 (input→output), 各打 PIPE_DEPTH 拍:
   (1) input_data_pipe        : 把数据搬到输出
   (2) input_handshake_pipe   : 把"这笔有效"的 valid 位搬到输出 (喂给 FIFO 的 input_valid)

反向 (output→input), 也打 PIPE_DEPTH 拍:
   (3) output_handshake_pipe  : 把"下游取走了一笔"的信号搬回输入 (加信用)

信用计数器 (send_credits):
   初值 = FIFO_DEPTH_ADJUSTED (满信用)
   input 握手完成  -> 减 1
   output 握手完成 (经反向流水线传回) -> 加 1
   两者同时/都不完成 -> 不变
   信用 == 0 -> input_ready = 0 (不再收)

输出 FIFO: 接住 (1)(2) 传来的有效数据, 做输出侧 ready/valid 接口
```

这里有一个**关键的深度公式**。要让信用流控在全吞吐下正确工作（不丢数据、不提前停），输出 FIFO 的最小深度必须容纳「一个完整的信用往返」：

\[ \text{FIFO\_DEPTH\_MINIMUM} = 2 \times \text{PIPE\_DEPTH} + \underbrace{2}_{\text{FIFO 输入输出延迟}} + \underbrace{1}_{\text{信用计数器更新延迟}} \]

为什么是 `2×PIPE_DEPTH`？因为一笔数据从发出到「它的信用被还回来」要走一个**完整往返**：先正向穿过 `PIPE_DEPTH` 级寄存器到达输出 FIFO，被下游取走后，信用信号再反向穿过 `PIPE_DEPTH` 级寄存器回到输入。在这整个往返期间，输入可能还在继续发，这些在途数据全得有地方放——这就是 FIFO 最小深度的来源。若你给的 `FIFO_DEPTH` 小于这个最小值，模块会在内部自动上调（`max(FIFO_DEPTH, 最小值)`）。

#### 4.2.3 源码精读

**最小深度公式与自动上调。** 用 `max_function.vh` 把用户给的 `FIFO_DEPTH` 与最小值取大：

[Pipeline_Credit_Buffer.v:98-107](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L98-L107) —— `FIFO_DEPTH_MINIMUM = (2*PIPE_DEPTH) + FIFO_LATENCY_CYCLES(2) + COUNTER_LATENCY_CYCLES(1)`，`FIFO_DEPTH_ADJUSTED = max(FIFO_DEPTH, FIFO_DEPTH_MINIMUM)`。

注释把最小深度的四项来源列得很清楚：

[Pipeline_Credit_Buffer.v:41-57](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L41-L57) —— 四项：`PIPE_DEPTH`（接住流水线寄存器内容）、再 `PIPE_DEPTH`（让输出握手的信用在用光前传回输入）、+2（FIFO 输入输出延迟）、+1（更新信用计数器）；给更大的 `FIFO_DEPTH` 可吸收更长的停顿。

**两次握手完成。** 与 skid buffer 一样，先算两侧各自的 `handshake_complete`：

[Pipeline_Credit_Buffer.v:114-120](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L114-L120) —— `input_handshake_done`、`output_handshake_done`。

**信用计数器。** 一个 `Counter_Binary`，初值 `FIFO_DEPTH_ADJUSTED`（满信用），位宽多加 1 位以容纳「满信用 + 0」两个边界：

[Pipeline_Credit_Buffer.v:136-170](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L136-L170) —— `CREDIT_COUNTER_WIDTH = clog2(INITIAL)+1`，`INITIAL_COUNT = FIFO_DEPTH_ADJUSTED`；计数器实例 `send_credits`，输出 `credit_available`。

**三条寄存器流水线。** 全是 `Register_Pipeline_Simple`，`clock_enable` 恒 1、`clear` 恒 0（**纯常通寄存器**，匹配 Intel hyper-register 的实现）。正向两条搬数据与 valid 位，反向一条搬「下游取走了」的信用回执：

[Pipeline_Credit_Buffer.v:181-221](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L181-L221) —— 正向：`input_data_pipe`（数据）、`input_handshake_pipe`（valid 位，`PIPE_DEPTH` 级）。

[Pipeline_Credit_Buffer.v:223-244](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L223-L244) —— 反向：`output_handshake_pipe`，把输出握手完成信号经 `PIPE_DEPTH` 级寄存器传回输入侧。

**输出 FIFO。** 直接复用上一节的 `Pipeline_FIFO_Buffer` 接住正向传来的有效数据：

[Pipeline_Credit_Buffer.v:251-271](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L251-L271) —— `output_storage` 实例：`input_valid` 接 `input_handshake_done_pipelined`，`input_data` 接 `input_data_pipelined`，`input_ready` 悬空（信用计数器已保证不会溢出）。

**信用控制逻辑。** 这是 credit 的精髓——三行组合逻辑搞定加减与停收：

[Pipeline_Credit_Buffer.v:282-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L282-L286) —— `credit_up_down`（输出握手完成则加，否则减）、`credit_update`（两者一有一无时才更新）、`input_ready = (credit_available != 0)`（信用耗尽则停收）。

**复位的特殊要求。** 因为流水线寄存器是常通的（无 enable/clear），`clear` 信号本身穿不过它们，必须**手动保持足够长**：

[Pipeline_Credit_Buffer.v:59-68](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L59-L68) —— 必须把 `clear` 至少保持 `PIPE_DEPTH+1` 拍，否则会带着残留的在途数据/控制信号开工，可能导致很久之后才暴露的数据丢失。

#### 4.2.4 代码实践

**实践目标**：手算一个具体配置的最小 FIFO 深度，并对照代码确认「自动上调」生效。

**操作步骤**：

1. 设 `PIPE_DEPTH=4`、`FIFO_DEPTH=0`（让模块自己定）。
2. 由公式：`FIFO_DEPTH_MINIMUM = 2×4 + 2 + 1 = 11`。
3. 由 [L107](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L98-L107)，`FIFO_DEPTH_ADJUSTED = max(0, 11) = 11`，即内部 FIFO 实际深度 11。
4. 由 [L136-137](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L136-L137)，`CREDIT_COUNTER_INITIAL=11`，`CREDIT_COUNTER_WIDTH = clog2(11)+1 = 4+1 = 5` 位（能表示 0..15，容得下 11 与 0）。
5. 若改 `FIFO_DEPTH=16`（想多吸收 5 拍停顿），则 `FIFO_DEPTH_ADJUSTED = max(16,11) = 16`，信用初值变 16。

**需要观察的现象**：`PIPE_DEPTH` 每加 1，最小 FIFO 深度就**加 2**（往返双向各一拍）；这是「信用往返」的直接体现。

**预期结果**：能说清「为什么是 2 倍 PIPE_DEPTH 而不是 1 倍」。这是阅读/计算型实践，无需仿真。

#### 4.2.5 小练习与答案

**练习 1**：FIFO 缓冲靠「比较读写地址」发现满了，Credit 缓冲却不用地址比较，为什么？

**答案**：因为 Credit 缓冲的存储体（输出 FIFO）和「决定能不能再收」的逻辑在物理上隔着 `PIPE_DEPTH` 级寄存器，输入侧**看不到** FIFO 的实时地址。它只能靠一个**先验的**信用计数器：发一笔减一、收一笔（经反向流水线延迟传回）加一，信用归零即停。这正是「信用流控」相对「结构判满」的优势——能在有延迟的反馈回路下仍正确工作。

**练习 2**：为什么流水线寄存器刻意做成「常通、无 enable、无 clear」？

**答案**：注释明说——若这些寄存器能被输入/输出接口控制且还能满足时序，那根本就不需要这条流水线了（见 [L174-177](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L172-L177)）。保持它们最简（无控制逻辑、最少连线）既省资源、又贴合 Intel hyper-register 这类「只能纯打拍」的底层器件。代价是 `clear` 必须外部保持 `PIPE_DEPTH+1` 拍。

---

### 4.3 停顿平滑（Pipeline_Stall_Smoother）

#### 4.3.1 概念说明

`Pipeline_Stall_Smoother` 解决一个更具体的问题：**当下游（输出）必须连续不断流，而上游（输入）有周期性、已知最大时长的停顿时，怎么消除这些停顿**。

典型场景是 CDC（跨时钟域）：上游数据要跨时钟域送过来，跨域同步会引入**周期性的、固定长度的延迟空洞**（比如每隔 N 拍空一拍）。如果直接把这种带空洞的数据流喂给下游，下游就会跟着断流。停顿平滑器的做法是：**先攒够一笔「能熬过最大停顿时长」的数据，再让输出开始**；一旦开始就连续放行，靠攒下的存量熬过每一个输入停顿，直到存量耗尽再重新攒。

它有一个重要前提：**输入输出的平均速率必须相等**。停顿平滑器只能吸收「周期性/瞬时的停顿」，不能吸收「持续的速率失配」——如果上游平均比下游慢，存量迟早耗尽，停顿还是会传到下游。注释建议用 `Pulse_Generator` 检测 `output_valid` 的下降沿来发现「停顿已经传到了输出」。

[Pipeline_Stall_Smoother.v:4-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L4-L25) —— 定位：在已知最大输入停顿时长下，先缓冲足够多的数据再放行输出，保证输出一旦启动就连续不断；强调依赖平均速率相等，否则停顿不可避免地传播。

#### 4.3.2 核心流程

停顿平滑器 = **一个 FIFO + 一个两态 FSM + 两个计数器**。FSM 决定「现在该攒还是该发」，两个计数器一个数缓冲里的存量、一个数「触发后过了几拍」。

```
两态 FSM:
   STATE_BUFFERING (攒): 关闭输出门, 输入数据进 FIFO 攒着
   STATE_SENDING   (发): 打开输出门, FIFO 连续输出

状态转移:
   BUFFERING -> SENDING : 存量攒够 (== FIFO_DEPTH)  或  触发延迟已满
   SENDING   -> BUFFERING: 存量耗尽 (== 0)

两个计数器:
   buffer_occupancy : 数 FIFO 里现在有几笔 (输入握手 +1, 输出握手 -1)
   trigger_delay    : 数"触发后过了几拍" (到 FIFO_DEPTH 拍则强制开始发送)

输出门控:
   Pipeline_Gate 按 (state == SENDING) 放行/阻断输出握手
```

这里有一个**深度换算公式**。用户给的是 `MAX_STALL_CYCLES`（要能熬过的最大停顿拍数），内部换算成 FIFO 深度时要加修正：

\[ \text{FIFO\_DEPTH} = \max(\text{MAX\_STALL\_CYCLES},\ 2) + 1 \]

两个修正各有来由（见源码注释）：一是底层 FIFO 缓冲本身有 2 拍输入输出延迟，**无法表达短于 2 拍的停顿**（单拍停顿会被当成两拍处理），故 `max(..., 2)`；二是为了在「第一次缓冲输出被送出那一拍」仍有 1 个空位接纳输入（否则会在输入侧制造一个 1 拍停顿），故 `+1`。

#### 4.3.3 源码精读

**深度换算。** 用 `max` 与 `clog2` 把 `MAX_STALL_CYCLES` 换成 FIFO 深度与计数器位宽：

[Pipeline_Stall_Smoother.v:68-81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L68-L81) —— `FIFO_DEPTH = max(MAX_STALL_CYCLES, 2) + 1`；注释解释 2 拍延迟下限与 `+1` 防「首次送出那拍输入侧 1 拍停顿」。

**存量计数器。** 一个 `Counter_Binary`，位宽 `clog2(FIFO_DEPTH)+1`（要能表示到 `FIFO_DEPTH` 本身，故多一位）：

[Pipeline_Stall_Smoother.v:85-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L85-L125) —— `buffer_occupancy` 实例，输出 `items_in_buffer`。

**触发延迟计数器。** 另一个 `Counter_Binary`，数「触发后过了几拍」，到 `FIFO_DEPTH` 拍则重装并触发发送：

[Pipeline_Stall_Smoother.v:129-168](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L129-L168) —— `trigger_delay` 实例，输出 `cycles_since_trigger`。

**存储体。** 直接复用 `Pipeline_FIFO_Buffer`：

[Pipeline_Stall_Smoother.v:172-194](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L172-L194) —— `smoothing_buffer` 实例，内部 `output_*_internal` 信号先不过外部，留给下面的门控决定是否放行。

**两态 FSM。** 用一个 `Register` 存状态，初值 `STATE_BUFFERING`：

[Pipeline_Stall_Smoother.v:204-222](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L204-L222) —— 状态定义与 `state_storage` 寄存器。

**存量与触发的控制。** 存量计数器在输入握手时 +1、输出握手时 -1；触发用 `Pulse_Latch` 锁存（保证即便触发脉冲只来一拍也能被记住）：

[Pipeline_Stall_Smoother.v:238-266](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L238-L266) —— 存量计数方向与触发计数重装；`Pulse_Latch` 把 `input_trigger` 锁成电平 `trigger_latched`。

**状态转移。** 两行链式三元（最后赋值胜出）：攒满或触发延迟满则转 SENDING，存量耗尽则转回 BUFFERING：

[Pipeline_Stall_Smoother.v:271-274](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L271-L274) —— `state_next`：`(items==LAST)||(cycles==LAST) → SENDING`；`(items==0) → BUFFERING`。

**输出门控。** 用 `Pipeline_Gate` 按 `(state == STATE_SENDING)` 放行输出握手，可选地把数据也清零：

[Pipeline_Stall_Smoother.v:279-296](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L279-L296) —— `output_gate` 实例：`enable = (state == STATE_SENDING)`，BUFFERING 时阻断输出。

#### 4.3.4 代码实践

**实践目标**：为「下游每 8 拍停 1 拍的偶发停顿」选择 `MAX_STALL_CYCLES`，并推算内部 FIFO 深度。

**操作步骤**：

1. 设下游最坏每 8 拍出现一次 1 拍停顿（CDC 同步空洞）。要熬过它，`MAX_STALL_CYCLES` 至少取能覆盖空洞的值。但注意底层 FIFO 有 2 拍延迟下限（见 [L68-70](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L68-L81)），所以即便设 `MAX_STALL_CYCLES=1` 也会被当成 2。
2. 由 [L81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L68-L81)，`FIFO_DEPTH = max(1, 2) + 1 = 3`，即 3 个存储位。
3. 若想「正好用掉一块 18 Kib 的 BRAM」（注释里给的精确控深技巧，见 [L36-39](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L27-L39)），且该 BRAM 深度为 1024，则令 `MAX_STALL_CYCLES = 1024 - 1 = 1023`，内部 `FIFO_DEPTH = max(1023,2)+1 = 1024`。
4. 追踪一次启动：上电 `state=BUFFERING`，`output_gate` 关闭；输入连写 3 笔后 `items_in_buffer==3==BUFFER_COUNT_LAST`，由 [L272](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L271-L274) `state_next=SENDING`，下一拍开门连续输出。

**需要观察的现象**：输出**不是**第一笔数据到了就开始，而是**攒满**（或触发延迟满）才开始；一旦开始就连续，直到存量耗尽。

**预期结果**：能说清「`MAX_STALL_CYCLES` 与内部 FIFO 深度差 1（那个 `+1`）」以及「为什么小于 2 会被上调」。逐拍时序验证需自行搭 testbench，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：停顿平滑器要求「输入输出平均速率相等」。如果上游平均比下游慢，会发生什么？

**答案**：存量计数器会逐渐降到 0，状态由 SENDING 转回 BUFFERING（见 [L273](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L271-L274)），输出门关闭、再次攒数据——即停顿**传播到了输出**，表现为 `output_valid` 出现下降沿。注释建议用 `Pulse_Generator` 检测这个下降沿来发现「平滑失败」（见 [L21-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L4-L25)）。停顿平滑器只能吸收瞬时空洞，不能创造带宽。

**练习 2**：为什么需要「触发延迟」这条转 SENDING 的路径？光靠「攒满」不够吗？

**答案**：因为有些数据流总量不够撑满 FIFO（比如一个短包），却仍要保证「等够最大停顿时长后才输出」以免被随后的输入停顿打断。触发延迟（`trigger_delay` 计数到 `FIFO_DEPTH`）提供了「即便没攒满，时间到了也强制开始发送」的兜底，见 [L272](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L271-L274) 的 `(cycles_since_trigger == TRIGGER_COUNT_LAST)` 条件。

---

### 4.4 Skid Buffer 链与四种缓冲的选型

#### 4.4.1 概念说明

最后把 `Skid_Buffer_Pipeline` 作为「基线」摆出来。它的逻辑很简单：把 `PIPE_DEPTH` 个 `Pipeline_Skid_Buffer` 首尾串成一条链，每个 skid buffer 切断一拍组合路径，总延迟 `PIPE_DEPTH` 拍。它的注释把与 FIFO、Credit 的关系讲得很直白：

- **vs FIFO 缓冲**：skid buffer 链能打很多拍（改善时序），但**不平滑速率失配**——下游停了，停顿迟早传到上游；反过来，FIFO 能平滑速率，但**几乎不打拍**。两者各管一头。
- **vs Credit 缓冲**：Credit 缓冲**两者都管**（既打拍又平滑），且对长流水线**更省资源**——尤其当 FIFO 能装进 BRAM、或目标器件有 Intel hyper-register 这类密集寄存器资源时。

[Skid_Buffer_Pipeline.v:4-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L4-L24) —— 定位：用 `PIPE_DEPTH` 个 skid buffer 流水线化握手、提升频率、延迟 `PIPE_DEPTH` 拍；明确「不像 FIFO 那样平滑速率」，并建议「若能用 FIFO 就改用 Credit Buffer」。

#### 4.4.2 核心流程

skid buffer 链就是一个 `generate` 循环展开的串行链，与 `Register_Pipeline_Simple` 是同构的（注释说它就是 Simple Register Pipeline 的变体）：

```
generate:
  if (PIPE_DEPTH == 0)  : 输入直连输出, 零逻辑 (组合直通)
  elif (PIPE_DEPTH > 0) :
     input_stage  (第 0 个 skid buffer, 接输入端口)
     for i = 1 .. PIPE_DEPTH-1:  pipe_stage (串接到前一级输出)
     末级输出接模块输出端口
```

每个 skid buffer 内部仍是 u10-l1 讲过的 EMPTY/BUSY/FULL 三态机；链上每一级各自独立地「滑行刹车」，把停顿一拍一拍地向后传。

#### 4.4.3 源码精读

**深度 0 的组合直通。** `PIPE_DEPTH=0` 时直接 `assign`，不推断任何逻辑（这是 `PIPE_DEPTH` 默认 `-1` 当哨兵、而 0 在此合法的原因）：

[Skid_Buffer_Pipeline.v:57-63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L57-L63) —— `PIPE_DEPTH==0` 分支：`input_ready=output_ready`、`output_valid=input_valid`、`output_data=input_data`。

**剥出首迭代。** 为了避免 `generate` 循环里出现 `-1` 下标，把第 0 级单独写、直接接模块输入端口：

[Skid_Buffer_Pipeline.v:66-91](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L66-L91) —— 注释说明剥首迭代的原因；`input_stage` 实例接 `input_*`，输出接 `*_pipe[0]`。

**循环串接剩余级。** 从第 1 级开始，每级接前一级的 `*_pipe[i-1]`：

[Skid_Buffer_Pipeline.v:97-116](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L97-L116) —— `for` 循环实例化 `pipe_stage`，首尾相连。

**末级接输出。** 把最后一级的 `*_pipe[PIPE_DEPTH-1]` 接到模块输出端口：

[Skid_Buffer_Pipeline.v:121-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L121-L125) —— `ready_pipe[PIPE_DEPTH-1]=output_ready`，`output_valid/output_data` 取末级。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把四种缓冲摆成一张选型表，回答「为偶发停顿的下游该选哪种、要多深」，并比较 FIFO 缓冲与 Credit 缓冲的**控制差异**。

**操作步骤**：

1. 填出选型表（结合四个模块注释）：

| 构件 | 存储 | 打拍(切长路径) | 平滑速率失配 | 典型延迟 | 典型用途 |
|------|------|----------------|--------------|----------|----------|
| `Skid_Buffer_Pipeline` | `PIPE_DEPTH`×2 个寄存器位 | ✅ 强 | ❌ 否（停顿会传） | `PIPE_DEPTH` 拍 | 切断超长组合路径 |
| `Pipeline_FIFO_Buffer` | 深度 `DEPTH` 的双口 RAM | ❌ 几乎不 | ✅ 是 | 2 拍 | 解耦两侧、吸收速率波动 |
| `Pipeline_Credit_Buffer` | `PIPE_DEPTH` 寄存器 + FIFO | ✅ 强 | ✅ 是 | `PIPE_DEPTH+2` 拍 | 长流水线既打拍又缓冲（省资源） |
| `Pipeline_Stall_Smoother` | 深度 `MAX_STALL_CYCLES+1` 的 FIFO | ❌ 几乎不 | 仅吸收**已知最大**周期停顿 | 2 拍+ | 消除下游断流（前提：平均速率相等） |

2. **为偶发停顿的下游选型**：若下游每 N 拍出现一次已知最大 K 拍的停顿、且上下游平均速率相等 → 选 `Pipeline_Stall_Smoother`，令 `MAX_STALL_CYCLES ≥ K`（内部深度自动 `max(K,2)+1`）；若停顿是**持续速率失配**而非周期空洞 → 选 `Pipeline_FIFO_Buffer`（深度按要吸收的波动量定）；若同时还要切断长组合路径 → 选 `Pipeline_Credit_Buffer`。
3. **比较 FIFO 缓冲与 Credit 缓冲的控制差异**（填空）：
   - 判满方式：FIFO 用 **（A）**（比较读写地址 + 绕回位）；Credit 用 **（B）**（先验信用计数器）。
   - 切断长路径：FIFO **（C）**；Credit **（D）**（`PIPE_DEPTH` 级寄存器）。
   - 控制信号回流：FIFO 的满状态**当拍**可知（地址同域比较）；Credit 的「下游取走」信号需**经反向流水线延迟 `PIPE_DEPTH` 拍**才回到输入。

   答案：A=结构判满（地址比较）；B=信用计数（算术判满）；C=几乎不打拍；D=打拍。

**需要观察的现象**：FIFO 的「满」是**事后**发现的（数据物理堆满），Credit 的「停收」是**事前**算出的（信用归零）——这一根本差异决定了 Credit 能在反馈回路有延迟时仍正确流控。

**预期结果**：能口述「要平滑选 FIFO/Stall_Smoother，要打拍选 Skid_Pipeline，两者都要选 Credit」，并说清 FIFO 与 Credit 判满机制的结构性 vs 算术性差异。本实践为阅读/归类型，无需仿真。

#### 4.4.5 小练习与答案

**练习 1**：`Skid_Buffer_Pipeline` 的注释说它「与 Simple Register Pipeline 是变体关系」。两者哪里像、哪里不同？

**答案**：两者都是「`PIPE_DEPTH` 级首尾串接、深度可 0 时组合直通」的 `generate` 链（见 [L57-63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L57-L63)）。不同在于每一级：Simple Register Pipeline 每级是一个**无条件打拍的寄存器**（数据来了就存，没有 ready/valid 握手、不能反压）；skid buffer 链每级是一个**带 EMPTY/BUSY/FULL 状态机的 skid buffer**，能做 ready/valid 握手、能反压——代价是每级多一个缓冲寄存器和控制逻辑。

**练习 2**：什么情况下 Credit 缓冲比 skid buffer 链**更省资源**？

**答案**：当 `PIPE_DEPTH` 较大时。skid buffer 链每级要 2 个数据寄存器位 + 控制逻辑，`PIPE_DEPTH` 级就是 `2×PIPE_DEPTH` 个寄存器位加 N 份控制；Credit 缓冲把「打拍」交给 `PIPE_DEPTH` 个**纯常通寄存器**（无控制逻辑、最省），把「缓冲」集中到一个能装进 BRAM 的 FIFO（密度远高于散布的寄存器）。所以流水线越长、FIFO 越能用 BRAM 实现，Credit 越省。见注释 [L26-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Buffer.v#L9-L31) 与 [L16-20](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Skid_Buffer_Pipeline.v#L4-L24)。

---

## 5. 综合实践

把本讲四个最小模块串起来：为一个「上游跨时钟域、中间有长组合路径、下游要求连续不断流」的连接，设计缓冲方案并选定参数。

**场景**：

- 上游数据经 CDC 送入，CDC 引入**周期性空洞**：每 16 拍出现一次 1 拍停顿。
- 输入到输出之间有一条**长组合路径**，需要切 `PIPE_DEPTH=3` 拍才能满足时序。
- 下游要求一旦开始接收就**连续不断流**。

**任务**：

1. **先判断能否用单个 `Pipeline_Stall_Smoother` 搞定**。结论：不能——它能吸收 CDC 的周期空洞（`MAX_STALL_CYCLES=2` 即可，因 2 拍下限），但它**几乎不打拍**，切不了那条长组合路径。
2. **改用 `Pipeline_Credit_Buffer`**：它能同时打拍（`PIPE_DEPTH=3`）与缓冲。
   - 由公式算最小 FIFO 深度：`2×3 + 2 + 1 = 9`。
   - 但 Credit 缓冲吸收的是「瞬时停顿」，CDC 每 16 拍 1 拍空洞属于瞬时，9 拍深度足够熬过（9 > 1）。
3. **若下游「绝对不能断」且对延迟不敏感**，可在 Credit 缓冲**之后再串一个 `Pipeline_Stall_Smoother`**：前者负责切长路径 + 吸收 CDC 空洞，后者负责「攒够再放、保证连续」。
4. **对照选型表**核对你每一步的选择：是否每一项需求都落到了「能管这件事」的构件上（打拍→Skid/Credit；平滑→FIFO/Credit/Stall_Smoother；消除断流→Stall_Smoother）。

**预期结果**：能说清「单一构件往往不够，按需求把它们串起来」的工程思路，并能手算 Credit 缓冲的最小 FIFO 深度。综合与时序验证需在 CAD 工具中完成，**待本地验证**。

## 6. 本讲小结

- **FIFO 缓冲**是 skid buffer 的「任意深度」推广：双口 RAM + 两套地址计数器 + **绕回位**判空/满；支持任意正整数深度（不必 2 幂），普通模式缓存「最早」数据、CBM 缓存「最新」数据；延迟 2 拍。
- **绕回位技巧**：地址相等既可能空也可能满，用「绕回位是否相等」区分——等则空、不等则满；等价于把地址指针多带一位。
- **Credit 缓冲**把「切断长路径的 `PIPE_DEPTH` 级寄存器」与「吸收速率失配的 FIFO」合一；用**信用计数器**先验算容量（发一笔减一、收一笔加一、归零停收），而非结构判满；最小 FIFO 深度 \(2\times\text{PIPE\_DEPTH}+3\) 容纳一个完整信用往返。
- **停顿平滑器**用一个两态 BUFFERING/SENDING FSM + FIFO，在「已知最大输入停顿」下保证输出一旦启动就连续；深度 `max(MAX_STALL_CYCLES,2)+1`；前提是平均速率相等。
- **Skid Buffer 链**是「只打拍、不平滑」的基线：`PIPE_DEPTH` 个 skid buffer 串接，深度可 0 时组合直通；长流水线下 Credit 缓冲比它省资源。
- **选型口诀**：要平滑速率选 FIFO/Stall_Smoother，要切断长路径选 Skid_Pipeline，两者都要选 Credit；要消除下游断流（周期空洞）选 Stall_Smoother。FIFO 判满是结构/事后，Credit 判满是算术/事前。

## 7. 下一步学习建议

本讲把「单接口弹性缓冲」族讲全。后续可沿两条线展开：

- **分流合流家族**：下一讲 u12-l2《Fork/Join/Branch 分流与合流》与 u12-l3《Merge 仲裁合流与控制门》把弹性接口从「一对一」推广到「一对多/多对一」，复用本讲的状态机与握手思路；读 `Pipeline_Fork_Blocking`/`Pipeline_Join` 时可对照本讲 FIFO 的 `input_ready`/`output_valid` 推导。
- **跨时钟域 FIFO**：本讲的 `Pipeline_FIFO_Buffer` 是 `CDC_FIFO_Buffer` 的同步前身（注释明说派生自 Cummings SNUG 2002）。学完 u13/u14 的 CDC 理论后，可对照阅读 `CDC_FIFO_Buffer`，看 Gray 码指针如何取代本讲的「地址+绕回位」实现安全的跨域空/满检测。
