# FCR 流控寄存器与原子更新

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **FCR（Flow Control Register，流控寄存器）** 里 `pause` 和 `idle` 两个比特各是什么方向、谁写谁读，以及它们为什么必须成对出现。
- 解释**为什么不能直接拉低 AXI-Stream 的 `TREADY`（即所谓的 stall）来暂停数据面**，而必须另造一套「优雅暂停」机制。
- 读懂控制面软件写 `pause=1`、轮询 `idle=1`、改表、再写 `pause=0` 的 **8 步原子更新握手**，并把这套握手和 `dpe_multiplexer` 的状态机逐一对应。
- 理解项目为什么**放弃**了 PeakRDL 的 Write-Buffered Register（WBR）方案，转而用 FCR 流控来实现路由表/密钥表的原子更新——这是一个典型的「用时间换面积」的工程取舍。

本讲承接 [u3-l3](u3-l3-cpu-fifo-axis-csr.md)（CPU FIFO 把 128 位 AXIS 拆成 32 位 CSR）。那一讲解决了「包级」通信；本讲解决的是**「表级」通信的安全性**：CPU 在改路由表/密钥表时，如何保证正在线速转发的数据面不会读到「改了一半」的脏表项。

## 2. 前置知识

在进入源码前，先用通俗语言把三个概念讲清楚。

**原子更新（Atomic Update）**
所谓「原子」，借用的是化学里「不可再分」的意思。一张路由表由很多字段（目的 IP、掩码、peer、出口接口）组成，CPU 不可能在**一个时钟周期**内把它们全部改完，只能一个字段一个字段地写。如果在 CPU 写到一半时，数据面正好来查这张表，就会读到「IP 已经是新的、但 peer 还是旧的」这种自相矛盾的表项，转发行为就不可预测。**原子更新的目标是：对外观察者（数据面）要么永远看到旧表，要么永远看到新表，永远看不到中间态。**

**AXI-Stream 的握手（TVALID/TREADY）**
本项目的数据面用 AXI-Stream（AXIS）总线搬包。AXIS 的握手规则很简单：每个时钟周期，发送方拉高 `TVALID` 表示「我手上有个有效字节」，接收方拉高 `TREADY` 表示「我愿意收」；二者**在同一拍同时为 1**，这一次传输才算完成。接收方不拉 `TREADY`（俗称 stall / 反压）就能让发送方等待。这套机制天然带有「流控」味道，所以初学者很容易想：「要暂停数据面，把末级的 `TREADY` 拉低不就行了？」——本讲会解释为什么这条路走不通。

**Write-Buffered Register（WBR）**
PeakRDL/SystemRDL 提供的一种字段修饰，作用是给寄存器加一层「影子寄存器」：软件写入时只更新影子值，等所有字段都写完，再用一次原子提交把影子值整体刷到真实寄存器。它的语义完美契合「原子更新」，但代价是面积——后面会算账。

> 涉及的关键术语：**FCR**、**pause/idle**、**原子更新**、**stall/反压**、**WBR**、**per-packet 轮询**、**skid buffer（滑窗缓冲）**。其中 skid buffer 在 [u4-l1](u4-l1-dpe-overview-axis.md) 会细讲，这里只需知道它是 AXIS 上用来「吸收一拍反压」的小缓冲即可。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | FCR 寄存器的**单一真源**：声明 `pause`/`idle` 两个比特的读写方向。 |
| [dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) | 数据面入口的多路复用器，**真正执行 pause→idle 状态机**的硬件。 |
| [dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | 数据面引擎顶层，把 FCR 的 `pause`/`idle` 接到多路复用器，并实例化两张表。 |
| [dpe_dummy_switch.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv) | Phase1 PoC 里替代完整处理链的「直通交换」，理解 `idle` 当前来源时要用到。 |
| [main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp) | 控制面固件，`config_routes`/`config_cryptokeys` 等函数里**真实调用** pause/idle 握手。 |
| [2.sw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md) | 软件理论手册，给出 8 步原子更新流程与 WBR 取舍的设计理由。 |

阅读建议：先看 `csr.rdl` 认识两个比特，再看 `2.sw/README.md` 的 8 步叙事建立直觉，最后用 `dpe_multiplexer.sv` 的状态机验证每一步，并用 `main.cpp` 看软件真的怎么写。

## 4. 核心概念与源码讲解

### 4.1 FCR pause/idle：流控寄存器的两面

#### 4.1.1 概念说明

FCR 是一个**只有 2 个有效比特**的寄存器，但这两个比特的方向恰好相反，构成了一个完整的「请求—应答」握手：

- `pause`：**控制位**。CPU 写、硬件读。CPU 把它写 1，意思是「请数据面停下」。
- `idle`：**状态位**。硬件写、CPU 读。硬件把它写 1，意思是「我已经停下了，你可以安全改表了」。

这就像打电话调度：`pause` 是你按下对讲机说「等一下」，`idle` 是对方回你「好，我停了」。两个方向缺一不可——只有 `pause` 没有 `idle`，CPU 不知道什么时候改表才安全；只有 `idle` 没有 `pause`，硬件不知道为什么要停。

#### 4.1.2 核心流程

整个握手的时序骨架可以用一句话概括：

```
CPU: pause = 1   ──轮询──>   (等待)   ──读到──>   idle == 1   ──>   安全改表   ──>   pause = 0
HW :           收到 pause，完成当前包，清空本级 datapath ──> idle = 1                                              恢复接包
```

关键点：**`idle` 永远不会先于 `pause` 出现**。硬件只有在确认 `pause` 已经生效、并且自己真的闲下来之后，才会把 `idle` 拉起。CPU 必须通过轮询 `idle` 来确认，而不能假设「写完 `pause` 等 N 拍就行」。

#### 4.1.3 源码精读

FCR 的规格在 `csr.rdl` 里只有寥寥几行，却把两个比特的方向定死了：

```systemrdl
reg {
   name = "csr.dpe.fcr";
   desc = "DPE Flow Control Register";

   field {
      name = "csr.dpe.fcr.pause";
      desc = "Pauses DPE";
      sw = rw;          // CPU 可读可写
      hw = r;           // 硬件只读
   } pause[1:1] = 0;

   field {
      name = "csr.dpe.fcr.idle";
      desc = "Indicates that all stages of DPE have been succesfully paused";
      sw = r;           // CPU 只读
      hw = w;           // 硬件可写
   } idle[0:0] = 0;
} fcr;
```

参见 [csr.rdl:507-524](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L507-L524)：这是 FCR 的单一真源。

回忆 [u3-l1](u3-l1-systemrdl-spec.md) 讲过的「读写方向口诀」——**谁是写者谁就是数据源**：
- `pause` 字段 `sw=rw; hw=r`：CPU 是写者，所以 `pause` 的值来自 CPU 总线，硬件只能读它来决定要不要停。
- `idle` 字段 `sw=r; hw=w`：硬件是写者，所以 `idle` 的值由数据面产生，CPU 只能读它来确认状态。

PeakRDL 就根据这两个方向，在生成的 RTL（`csr.sv`）里把 `pause` 接成「CPU 写入 → 硬件读取」的通路，把 `idle` 接成「硬件写回 → CPU 读取」的通路。在 `dpe.sv` 顶层，这对通路被直接连到了多路复用器上：

```systemverilog
// DPE multiplexer
   dpe_multiplexer u_dpe_multiplexer (
      .pause                  (from_csr.dpe.fcr.pause.value),   // CPU 写的 pause → 喂给 mux
      .is_idle                (to_csr.dpe.fcr.idle.next),       // mux 算出的 idle → 写回 CSR
      ...
   );
```

参见 [dpe.sv:67-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L76)。注意 SystemRDL/ PeakRDL 的命名约定（详见 [u3-l2](u3-l2-peakrdl-generation.md)）：读硬件信号用 `.value`，写回硬件用 `.next`。所以 `pause.value` 是「CPU 已经写进来的值」，`idle.next` 是「我要在下一拍写回去的值」。

#### 4.1.4 代码实践

**实践目标**：亲手验证 FCR 的读写方向与 HAL 调用形式。

**操作步骤**：
1. 打开 [csr.rdl:507-524](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L507-L524)，确认 `pause` 是 `sw=rw;hw=r`、`idle` 是 `sw=r;hw=w`。
2. 打开 [main.cpp:395-403](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L395-L403)，看 `show_routes()` 函数是怎么调用 HAL 的。

**需要观察的现象**：软件里 `pause(1)` / `pause(0)` 是带参数的写调用，而 `idle()` 是无参数、返回 bool 的读调用——这正好对应「CPU 写 pause、CPU 读 idle」。

**预期结果**：你会看到形如下面的真实代码（不是示例，是项目原文）：

```cpp
csr->dpe->fcr->pause(1);      // 写 pause=1（控制位，CPU 发起）
while (!csr->dpe->fcr->idle()); // 轮询读 idle，直到为 1（状态位，硬件应答）
... 读/改表 ...
csr->dpe->fcr->pause(0);      // 写 pause=0，恢复
```

> 待本地验证：若你想看 HAL 里 `pause()`/`idle()` 这两个方法的底层实现，可在 `3.build/csr_build/` 下运行 `make -f MakefileCSR` 后查看生成的 `csr_hw.h`（硬件 HAL）或 `csr_cosim.h`（协同仿真 HAL），方法体的本质就是一次带地址偏移的总线 store/load。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `pause` 字段误写成 `sw=r; hw=w`，系统会发生什么？

**参考答案**：CPU 将**无法发起暂停**——`pause` 变成硬件写、CPU 只读，CPU 写 `pause(1)` 不会真正落到寄存器，多路复用器永远收不到暂停请求，`idle` 永远为 0，原子更新握手死锁。这正是「读写方向必须匹配数据流向」的硬性要求。

**练习 2**：为什么 `idle` 不设计成 `singlepulse`（写后一拍自动清零，见 [u3-l1](u3-l1-systemrdl-spec.md)）？

**参考答案**：`idle` 是电平型状态，需要**持续为 1** 让 CPU 在改表的多个周期内都能读到「现在安全」。若做成 singlepulse，它只在硬件置位的下一拍闪一下就清零，CPU 轮询时极易错过，反而需要更复杂的边沿检测。`pause` 才是「触发型」语义，但它由 CPU 主动写、主动清，也不需要 singlepulse。

---

### 4.2 为何不能用 AXI stall（TREADY）做暂停

#### 4.2.1 概念说明

这是本讲最关键的「为什么」。很多初学者会想：AXIS 既然能用 `TREADY=0` 反压，那 CPU 想暂停数据面时，直接让末级把 `TREADY` 拉低不就行了？**答案是不行**，原因藏在一个朴素的原则里：

> **一个已经进入数据面的包，必须按照它进入时有效的规则被完整处理。**

路由表是规则的载体。如果一个包已经进了流水线、正走到「查路由表」这一级，这时你为了改表而去 stall 它，会出现两种糟糕情况：

1. **包被卡在查表级**：表的内容在你改到一半时被这个包读到，于是这个包按「半新半旧」的表转发，行为错误。
2. **为了不读脏表而 stall 整条流水线**：但这会让已经在流水线里、且不依赖该表的包（比如已查完表正往封装级走的包）也被冻住，造成不必要的停顿和潜在死锁。

换句话说，stall 是**逐拍的反压**，它控制的是「下一拍要不要继续搬数据」，控制粒度太细、太局部，无法表达「请把整条流水线优雅地排空到一个一致状态」这种语义。我们需要的是**包粒度的、全局可见的、有应答的**暂停——这正是 FCR 提供的。

#### 4.2.2 核心流程

FCR 暂停与 AXIS stall 的对比：

| 维度 | AXIS stall（`TREADY=0`） | FCR 暂停（`pause`/`idle`） |
| --- | --- | --- |
| 控制粒度 | 逐拍（cycle-level） | 逐包（packet-level） |
| 作用范围 | 单个接口、局部反压 | 整条数据面、全局可见 |
| 当前在飞的包 | 可能被卡在任意一级 | **允许处理完**，再停 |
| 有无应答 | 无（发送方只能等） | 有（`idle` 回报状态） |
| 适合的场景 | 上下游速率失配 | 改全局共享状态（表） |

#### 4.2.3 源码精读

项目文档把这一点说得很直白。在 [2.sw/README.md:256](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L256) 里有这段关键论述（译文要点）：

> 然而，这样的暂停**不能用 AXI 协议自带的 stall 机制**（即在流水线末端拉低 `TREADY`）来实现，因为**一个已经进入 DPE 的包，必须按它进入时有效的规则被处理完**。

接着看 `dpe_multiplexer` 是如何「优雅」地实现这一点的——它不是一收到 `pause` 就立刻掐断，而是**把当前正在服务的那个包送完**，才退回 IDLE。见状态机里的 `S*` 态（服务态）：

```systemverilog
S0: begin
   if (from_cpu.tlast && from_cpu.tvalid && to_dpe_sbuff.tready) begin
      next_state = pause ? IDLE : R1;   // 本包送完(tlast)后，才看 pause
   end
end
```

参见 [dpe_multiplexer.sv:93-97](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L93-L97)。注意 `S0` 态只有在 `tlast`（包尾）那一拍才会检查 `pause`——也就是说，**一旦开始服务一个包，就一定把它完整送完**，绝不会在中途因为 `pause` 而截断。这正是「按进入时规则处理完」原则在 RTL 里的落地。相比之下，如果用 `TREADY` 反压，包就可能被冻结在任意 beat 上。

#### 4.2.4 代码实践

**实践目标**：在源码里找到「pause 只在包边界生效」的全部证据。

**操作步骤**：
1. 打开 [dpe_multiplexer.sv:82-149](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L82-L149) 的状态转移段。
2. 分别在 `R0`（轮询态）和 `S0`（服务态）里搜索 `pause` 关键字。

**需要观察的现象**：
- 在 `R0` 里，`pause` 出现在「没有有效包」的分支：`else if (pause) next_state = IDLE;`（[L89](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L89)）——空闲时收到 pause，立即退回 IDLE。
- 在 `S0` 里，`pause` 只出现在 `tlast` 条件成立之后（[L95](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L95)）——忙碌时收到 pause，必须等包送完。

**预期结果**：你会确认一个结论——**`pause` 永远不会在 `tlast` 之前中断一个正在服务的包**。这就是 FCR 与 stall 的本质区别：stall 是「立刻停」，FCR 是「送完当前包再停」。

#### 4.2.5 小练习与答案

**练习 1**：假设把 `S0` 态的转移改成 `if (pause) next_state = IDLE;`（删掉 `tlast` 条件），会对系统造成什么危害？

**参考答案**：一个包可能被**从中间截断**——它已经按旧表查了路由，却被提前丢弃或滞留，下游 demux 可能收到一个没有 `tlast` 的残包，触发各种对齐与状态错误。这违背了「包必须按进入时规则完整处理」的原则。

**练习 2**：既然 stall 不能用来改表，那数据面正常运行时上下游速率失配该怎么办？

**参考答案**：靠 AXIS 自带的 `TREADY` 反压 + 各级 FIFO/skid buffer（见 [u4-l1](u4-l1-dpe-overview-axis.md)）。也就是说，**stall 和 FCR 各管一件事**：stall 管逐拍的瞬时反压（正常流量整形），FCR 管全局状态的原子切换（改表）。二者并不冲突，而是互补。

---

### 4.3 WBR 替代方案：用流控握手换面积

#### 4.3.1 概念说明

讲清楚了「为什么要暂停」，下一个问题是「暂停之后怎么保证原子」。PeakRDL 其实自带一个语义层的答案：**Write-Buffered Register（WBR）**。它的思路是给每个需要原子更新的字段配一个影子寄存器：

- 软件逐字段写入时，只更新影子值（旧值仍在工作）；
- 全部字段写完，软件触发一次「提交」，影子值在一个时钟周期内整体替换工作值；
- 数据面要么看到全旧的值，要么看到全新的值，永不看到中间态。

语义上 WBR 完美。但项目**没有采用它**，转而用 FCR 流控。原因是面积。详见 [2.sw/README.md:256](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L256) 的论述（译文要点）：

> 实现 1 比特 WBR 存储需要 **3 个触发器**：一个存当前值、一个存未来值（影子）、一个作写使能信号。

#### 4.3.2 核心流程

我们来算一下这笔账。本项目需要原子更新的两张表都很大：

- 路由表 `routing_table`：64 条目，每条含 IP(32)、mask(32)、peer_idx、dst 等字段。
- 密钥表 `cryptokey_table`：64 条目，每条含本地/远端身份、256 位加密密钥、256 位解密密钥、收发计数器等，字段极多（见 [u4-l6](u4-l6-routing-cryptokey-tdpram.md)）。

如果给这两张表的每个比特都加 WBR，触发器数量会膨胀到原来的 3 倍。设一张表原始存储需要 \(B\) 个比特，则 WBR 成本为：

\[ C_{\text{WBR}} = 3B \]

而 FCR 方案的成本几乎是常数：

\[ C_{\text{FCR}} = 2 \;\text{（pause + idle 两个比特）} + \text{少量状态机寄存器} \]

二者之比：

\[ \frac{C_{\text{WBR}}}{C_{\text{FCR}}} \approx \frac{3B}{2} \]

当 \(B\) 高达数千比特（两张表合计）时，这个比值非常可观。FCR 用「暂停数据面几十上百个周期」的时间代价，换掉了「给每个比特配影子寄存器」的面积代价——这是一笔典型的**时间换面积**交易，而且非常划算：改表是极低频事件（握手时、定时轮换密钥时），暂停带来的吞吐损失可忽略，省下的触发器却能显著降低 FPGA 占用、改善时序收敛。

#### 4.3.3 源码精读

项目里 WBR 没有出现在最终 RTL 中，但它的「痕迹」还留在构建流程里。回忆 [u3-l2](u3-l2-peakrdl-generation.md) 提到：`MakefileCSR` 在把 `csr.rdl` 喂给 `systemrdl-compiler` 之前，会用 `sed` 过滤掉两个它不认的修饰符，其中一个就是 `buffer_writes`（即 WBR 的触发条件）。这印证了设计者曾认真评估过 WBR 路线，最终选择用流控取而代之。

而 FCR 方案在 RTL 侧的体现非常轻量——就是 `dpe.sv` 里那两根线：

```systemverilog
.pause (from_csr.dpe.fcr.pause.value),  // 1 根线：CPU 的暂停请求
.is_idle (to_csr.dpe.fcr.idle.next)     // 1 根线：数据面的应答
```

参见 [dpe.sv:68-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L68-L69)。两张大表本身（`u_routing_table`、`u_cryptokey_table`）则用普通的双口 RAM（`tdp_ram`）实现，没有任何影子寄存器——原子性完全由「暂停期间才允许写」这个协议保证，而不是由硬件结构保证。

#### 4.3.4 代码实践

**实践目标**：在 `dpe.sv` 里确认两张表是普通 RAM、没有 WBR 影子逻辑。

**操作步骤**：
1. 打开 [dpe.sv:105-139](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L139)。
2. 阅读两个 `tdp_ram` 实例（`u_routing_table`、`u_cryptokey_table`）。

**需要观察的现象**：这两个实例的端口只有标准的 `we/addr/din/dout`——写使能直接由 CSR 请求驱动（`from_csr.routing_table.req & from_csr.routing_table.req_is_wr`），没有任何「影子写入 / 原子提交」的额外端口。

**预期结果**：你能得出结论：**表本身的硬件是「非原子」的**——任何周期写使能有效都会立刻改内容。原子性是**靠软件先 `pause`、等 `idle`、再写表**这套协议在系统层保证的，而不是靠 RAM 自身。

> 待本地验证：可对照 [tdp_ram.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/tdp_ram.sv) 的实现，确认它是一个朴素的双口寄存器阵列，不含缓冲写逻辑。

#### 4.3.5 小练习与答案

**练习 1**：假如某张表只有 8 个比特、且改它的频率很高（每个包都要改一次），FCR 方案还划算吗？

**参考答案**：不一定。FCR 每次改表都要付出「暂停→排空→恢复」的固定时间开销（至少几十拍）。如果表很小（WBR 成本 \(3 \times 8 = 24\) 个触发器，可以接受）且改得频繁（暂停开销被频繁触发），WBR 的「无暂停、硬件原子」反而更优。FCR 的优势在「表大 + 改得稀」的场景——正好契合本项目的路由表/密钥表。

**练习 2**：为什么设计者要在 `MakefileCSR` 里用 `sed` 删掉 `buffer_writes`，而不是直接在 `csr.rdl` 里不写它？

**参考答案**：因为 `csr.rdl` 是**单一真源**，可能要同时服务于「需要 WBR 文档说明」和「实际生成不带 WBR 的 RTL」两个目的。用构建期的 `sed` 过滤，既保留了规格文档里对 WBR 的设计意图记录，又能让实际综合的 RTL 走 FCR 路线，是单一真源与多产物管线（见 [u3-l2](u3-l2-peakrdl-generation.md)）的灵活运用。

---

### 4.4 原子更新握手：8 步流程与 mux 状态机

#### 4.4.1 概念说明

前面三节分别讲了「用什么（FCR）」「为什么不用 stall」「为什么不用 WBR」。本节把它们串成一条**完整的 8 步握手**——这是控制面软件改表时实际遵循的协议，也是本讲的综合主线。

这套握手的设计目标可以用一句话概括：**让数据面「优雅地」从「线速转发」过渡到「完全静止」，在静止窗口里改表，再「优雅地」恢复转发。**「优雅」二字体现在三点：不丢正在处理的包、不读到半成品表、CPU 能确切知道何时安全。

#### 4.4.2 核心流程

[2.sw/README.md:260-268](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L260-L268) 给出了 8 步流程。下面把它整理成「软件动作 ↔ 硬件状态」的对照表：

| 步骤 | CPU（控制面）动作 | DPE（数据面）状态变化 |
| --- | --- | --- |
| 1 | 写 `fcr.pause = 1` | 正在服务的当前队列/包继续处理 |
| 2 | （轮询 `fcr`） | 多路复用器在**包边界**进入 IDLE |
| 3 | （继续轮询） | 第一级送完包、清空本级 datapath，拉低 `TVALID`，进 IDLE |
| 4 | 读到 `fcr.idle == 1` | 所有需暂停的组件均已 IDLE，DPE 静止 |
| 5 | 多周期写表（路由表/密钥表） | 表内容更新（此时数据面不查表，安全） |
| 6 | 写 `fcr.pause = 0` | 多路复用器准备恢复接包 |
| 7 | （无需轮询） | mux 回到默认轮询，从下一队列开始接包 |
| 8 | （恢复正常工作） | 随着新包到来，各级逐渐回到活动态 |

把第 1–4 步抽象成状态机视角：

```
          pause=1
  [正常转发] ───────► [完成当前包] ───────► [排空datapath] ───────► [全idle]
     ▲                                                                  │
     │                            pause=0                               │ idle=1
     └──────────────────────────────────────────────────────────────────┘
                              (CPU 在此窗口内改表)
```

#### 4.4.3 源码精读

现在用 `dpe_multiplexer` 的状态机逐一验证。它的状态枚举是：

```systemverilog
typedef enum logic [3:0] {
   IDLE,
   R0, S0,   // CPU
   R1, S1,   // ETH1
   R2, S2,   // ETH2
   R3, S3,   // ETH3
   R4, S4    // ETH4
} state_t;
```

参见 [dpe_multiplexer.sv:56-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L56-L63)。这里有 5 对 `(R_i, S_i)`：`R_i`（Round）是轮询态，检查第 `i` 路输入有没有包；`S_i`（Serve）是服务态，把第 `i` 路的包一个 beat 一个 beat 送出去，直到 `tlast`。`IDLE` 既是上电复位态，**也是暂停时的静止态**——README 叙事里说的「PAUSED 状态」，在 RTL 里就是 `IDLE`。

**IDLE 态的进入与等待**（对应步骤 1–2）：

```systemverilog
IDLE: begin
   if (!pause) next_state = R0;   // 只有 pause 撤销，才重新开始轮询
end
```

参见 [dpe_multiplexer.sv:83-85](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L83-L85)。一旦 `pause` 生效，mux 最终会回到这里「卡住」，直到 `pause=0`。

**轮询态对 pause 的响应**（对应步骤 2–3），以 `R0` 为例：

```systemverilog
R0: begin
   if (from_cpu.tvalid && to_dpe_sbuff.tready)       next_state = S0;   // 有包就开始服务
   else if (pause)                                   next_state = IDLE; // 空闲且收到pause→退回
   else if (!from_cpu.tvalid && to_dpe_sbuff.tready) next_state = R1;   // 没包就轮下一路
end
```

参见 [dpe_multiplexer.sv:87-91](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L87-L91)。注意优先级：**只有当前路没有有效包时，`pause` 才会把 mux 拉回 IDLE**；如果当前路正好有包，会先进入 `S0` 把它服务完。

**服务态对 pause 的响应**（对应步骤 3），以 `S0` 为例：

```systemverilog
S0: begin
   if (from_cpu.tlast && from_cpu.tvalid && to_dpe_sbuff.tready) begin
      next_state = pause ? IDLE : R1;   // 包尾这一拍：若pause则停下，否则轮下一路
   end
end
```

参见 [dpe_multiplexer.sv:93-97](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L93-L97)。这就是「优雅」的核心：**一个包一旦开始送，就一定送到 `tlast`**；`pause` 只在包与包之间的间隙生效。其余 `S1..S4` 完全对称（见 [L105-145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L105-L145)）。

**`idle` 的产生**（对应步骤 4）：

```systemverilog
IDLE: begin
   is_idle = !to_dpe.tvalid;   // 本级已不再向下游送有效数据
end
```

参见 [dpe_multiplexer.sv:171-173](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L171-L173)。`to_dpe` 是经过 skid buffer 之后的输出。`is_idle` 不仅要求 mux 进入 `IDLE` 态，还要求**最后一拍有效数据已经离开 skid buffer**（`to_dpe.tvalid==0`）。这保证了「本级 datapath 已清空」，CPU 读到 `idle==1` 时可以放心改表。

> **关于 Phase1 PoC 现状的诚实说明**：在当前 HEAD 的 `dpe.sv` 中，`fcr.idle` **只**由多路复用器的 `is_idle` 驱动（[dpe.sv:69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L69)），并没有把多个流水级的 idle 信号「与」起来。这是因为当前实际编入 `top.filelist` 的是 `dpe_dummy_switch`（纯组合直通 + skid buffer，见 [dpe_dummy_switch.sv:72-123](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L72-L123)），整条链是 `mux → dummy_switch → demux`，mux 的 idle 就足以代表全链静止。README 描述的「所有组件都进入 IDLE」是**完整设计（含 `dpe_egress_ip_lookup` 等级）的意图**——届时每级都会各自产生 idle，再相与汇总到 `fcr.idle`。这一现状与 [u2-l1](u2-l1-hw-sw-partition.md)/[u4-l5](u4-l5-wg-encap-decap.md) 所述的 Phase1 PoC 一致。

**软件侧的真实调用**（对应全部 8 步）。以 `config_routes()` 为例，它是最完整的实例——先暂停、再交互式改表、再恢复：

```cpp
void config_routes(volatile csr_vp_t* csr) {
   ...
   csr->dpe->fcr->pause(1);            // 步骤 1
   while (!csr->dpe->fcr->idle());     // 步骤 2-4：轮询直到 idle

   ... 与用户交互，读取/修改 routing_table 各字段 ...  // 步骤 5

   csr->dpe->fcr->pause(0);            // 步骤 6-8
}
```

参见 [main.cpp:406-461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L406-L461)。注意步骤 5 里对表的写操作（如 [`entry->ip->ip(uip)` 等](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L451-L454)）全都夹在 `pause(1)` 与 `pause(0)` 之间——这就是原子窗口。同样的模式在 `show_routes`（[L395-403](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L395-L403)）、`show_cryptokeys`（[L529-537](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L529-L537)）、`config_cryptokeys`（[L543-L777](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L543-L544)）里反复出现。甚至连「只读展示」表项的 `show_*` 函数也做了 pause/idle——因为读一张正在被数据面并发读写的表，同样可能读到不一致的中间态，所以读也要进原子窗口。

> **一个值得注意的细节**：`show_routes`/`show_cryptokeys` 这类**只读**函数也做了 `pause(1)/pause(0)`。这说明设计者把「CPU 与数据面并发访问同一张表」本身视为需要互斥的场景——不只是写，连读都要在静止窗口里做，才能保证读到的是一个一致的快照。

#### 4.4.4 代码实践

**实践目标**：用时序图描述 CPU 写 `pause=1` 到检测 `idle=1` 期间 DPE 各级状态的变化，以及更新完成后 `pause=0` 的恢复过程（对应任务要求）。

**操作步骤**：

1. **先读真实的软件模板**。打开 [main.cpp:394-404](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L394-L404) 的 `show_routes()`，把它当作你的时序图「软件侧」的脚本。

2. **画出下面的时序图**（用纸笔或任意画图工具），横轴为 80 MHz 数据面时钟周期，纵轴分以下几行：`fcr.pause`、`mux.state`、`to_dpe.tvalid`、`fcr.idle`、`CPU 动作`。按如下场景填充：
   - **场景**：`pause=1` 到来时，mux 正处于 `S0` 态服务一个来自 CPU 的 3-beat 包（还没到 `tlast`），下游 `to_dpe` 当前正在送第 2 个 beat。
   - **要求**：
     - 标出 `S0` 必须先走到 `tlast`（第 3 个 beat 送出）才转入 `IDLE`，体现「包不被截断」。
     - 标出进入 `IDLE` 后，还要等 skid buffer 里最后那个 beat 排出（`to_dpe.tvalid` 从 1 变 0），`is_idle` 才置 1，进而 `fcr.idle=1`。
     - 标出 CPU 在 `idle=1` 后开始写表（若干周期），写完写 `pause=0`。
     - 标出 `pause=0` 后，`IDLE` 态在下一拍迁移到 `R0`，重新开始轮询。

3. **参考时序图骨架**（请你补全每个信号的具体跳变拍位）：

   ```
   周期        t0    t1    t2    t3    t4    t5    t6   ...   tK   tK+1 ...
   pause       0     1     1     1     1     1     1          1     0
   mux.state   S0    S0    S0    IDLE  IDLE  IDLE  IDLE       IDLE  R0
   (正在送)    beat2 beat3(tlast)
   to_dpe.vld  1     1     1     1     0     0     0          0     ...
   idle        0     0     0     0     1     1     1          1     0
   CPU动作           写pause=1                  读到idle=1,开始改表 ...  写pause=0
   ```

   关键填空点：
   - `t3` 为什么从 `S0` 进 `IDLE`？（答：`tlast` 已在 `t2` 出现，且 `pause=1`，故 `S0 → IDLE`）
   - `t4` 的 `idle` 为什么才变 1？（答：`t3` 时 `to_dpe.tvalid` 仍为 1，skid buffer 里还有最后一个 beat；到 `t4` 排空后 `to_dpe.tvalid=0`，`is_idle=1`）
   - `tK+1` 为什么能离开 `IDLE`？（答：`IDLE: if(!pause) next_state=R0`，`pause=0` 后立即恢复轮询）

**需要观察的现象**：你应该能从图上看出三个「优雅」特性——(a) `pause` 与 `tlast` 之间没有竞态，包总是送完；(b) `idle` 的置位严格晚于 datapath 排空；(c) 恢复接包不丢任何已到达的包（因为输入 FIFO 还在，mux 一恢复就继续轮询服务）。

**预期结果**：得到一张能解释「为什么 CPU 必须用 `while(!idle())` 轮询、而不能写死等待固定周期」的时序图——因为从 `pause=1` 到 `idle=1` 的拍数**取决于当时在飞的包有多长**，是个变量。

> 待本地验证：上述拍位关系可用 `4.sim/` 下的协同仿真（见 [u7-l2](u7-l2-vproc-cosim.md)）实测。在 `VUserMain0.cpp` 里对 `fcr.pause` 写 1 后，用 `VRead` 轮询 `fcr.idle`，并在波形里量出 `pause=1` 上升沿到 `idle=1` 上升沿的实际周期数，与你手画的时序图对照。

#### 4.4.5 小练习与答案

**练习 1**：CPU 写完 `pause=1` 后，如果直接开始写表、**不**轮询 `idle`，最坏会发生什么？

**参考答案**：此时 mux 可能仍在 `S_i` 态送一个长包，数据面正活跃地查表。CPU 的写会立刻改变表内容，数据面可能在同一个包的处理过程中读到「前几个字段是旧表、后几个字段是新表」的撕裂视图，导致错路由/错密钥。`while(!idle())` 这一行就是为了排除这个窗口。

**练习 2**：从 `pause=1` 到 `idle=1` 的延迟是固定的吗？受什么影响？

**参考答案**：不固定。它取决于 `pause` 到来时数据面里**在飞包的长度**和**流水线深度**。最短情况（无在飞包）只需排空 skid buffer 的 1–2 拍；最长情况可能要等一个最大长度包（jumbo frame 可达上千 beat）送完。这正是为什么必须用轮询而不是固定延迟。

**练习 3**：步骤 6 写完 `pause=0` 后，CPU 为什么不需要再等一个「恢复就绪」的应答？

**参考答案**：因为恢复方向是**无副作用、必然成功**的——`IDLE` 态只要看到 `!pause` 就迁回 `R0`，输入 FIFO 里堆积的包会被正常服务，不会丢失。而暂停方向有副作用（要保证不读脏表），所以必须有 `idle` 应答；恢复方向没有这种一致性强约束，因此单向写 `pause=0` 即可。

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个**端到端的「改一张路由表项」走查**任务。

**任务**：假设用户通过 UART CLI 执行 `config routes`，要修改路由表第 5 条目的目的 IP。请你以「系统观察者」的视角，写出从用户敲回车到表项更新完成之间，**控制面与数据面之间发生的全部关键事件**，并标注每件事发生在本讲的哪个模块（FCR/stall/WBR/握手）。

**建议的产出形式**：一张两列表格，左列是事件（按时间顺序），右列是对应的「机制 + 源码位置」。至少应覆盖：

1. 固件进入 `config_routes()`，先做 `pause(1)`（对应 4.1 / 4.4）。
2. 数据面 mux 把当前包送完、排空、置 `idle=1`（对应 4.2 / 4.4）。
3. CPU 轮询到 `idle=1`，进入安全窗口（对应 4.4）。
4. CPU 逐字段写 `routing_table.entry[5]`（说明为什么此刻写是原子的——对应 4.3）。
5. CPU 写 `pause=0)`，mux 恢复轮询（对应 4.4）。

**进阶**：思考并回答——如果在第 4 步（CPU 写表过程中）恰好有一个广播包（`tuser_dst == DPE_ADDR_BCAST`，见 [u4-l3](u4-l3-demultiplexer.md)）从某个网口到达，它的命运如何？为什么它不会破坏这次原子更新？

> 参考思路：到达的包只会进入输入 FIFO 等待，mux 此时在 `IDLE` 不接包（`IDLE` 态下所有 `from_*` 的 `tready` 都为默认 0，见 [dpe_multiplexer.sv:164-168](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L164-L168)），FIFO 会反压上游 PHY/MAC（正常 stall，对应 4.2 的「stall 管正常反压」）。等 `pause=0)` 恢复后，它才被 mux 读出处理，此时看到的是已经一致的新表。这正是 stall 与 FCR 分工协作的生动例子。

## 6. 本讲小结

- **FCR 是一个 2 比特寄存器**：`pause`（CPU 写、硬件读）发起暂停请求，`idle`（硬件写、CPU 读）回报静止状态，二者构成请求—应答握手，方向由 `csr.rdl` 的 `sw/hw` 属性定死。
- **不能用 AXIS 的 `TREADY` stall 来改表**：stall 是逐拍、局部的反压，无法保证「已在飞的包按进入时规则处理完」；FCR 在**包边界**生效，当前包必定送完才停。
- **项目放弃了 WBR（Write-Buffered Register）**：因为 WBR 每比特要 3 个触发器，对两张大表（路由表/密钥表）面积代价过高；FCR 用「暂停—改表—恢复」的时间开销换面积，对低频改表场景非常划算。
- **原子更新是 8 步握手**：`pause=1` → mux 完成当前包 → 排空 datapath → `idle=1` → CPU 改表 → `pause=0` → 恢复轮询；CPU 必须用 `while(!idle())` 轮询，因为等待拍数随在飞包长度变化。
- **`IDLE` 状态兼作复位态与暂停态**：README 叙事里的「PAUSED 状态」在 RTL 里就是 `IDLE`；当前 Phase1 PoC 中 `fcr.idle` 仅由 mux 产生（因链路是 `dummy_switch` 直通），完整设计会汇总各流水级的 idle。
- **连只读展示表项也要 pause/idle**：`show_routes`/`show_cryptokeys` 同样进原子窗口，说明 CPU 与数据面对表的并发访问需整体互斥，读也要看一致快照。

## 7. 下一步学习建议

本讲把「控制面如何安全地改数据面的表」讲透了。接下来的学习方向：

- **深入表的硬件实现**：本讲多次提到 `routing_table`/`cryptokey_table` 由 `tdp_ram` 实现、并以 SystemRDL 的 `external regfile` 声明。建议接着学 [u4-l6 路由表与密钥表的 tdp_ram 实现](u4-l6-routing-cryptokey-tdpram.md)，看 external regfile 如何生成 req/ack 握手、双口 RAM 的 A/B 端如何分别服务 CPU 与数据面。
- **理解表的真正使用者**：路由表的查找逻辑在 [u4-l4 TCAM 最长前缀路由查找](u4-l4-tcam-ip-lookup.md)；密钥表由加解密流水线使用，见 [u5 ChaCha20-Poly1305 加密硬件](u5-l1-aead-chacha-poly-theory.md)。理解了「谁在读表」，你会更明白本讲「为什么改表必须原子」。
- **看软件如何编排整套握手**：[u6-l4 软件控制流：收发包与表更新](u6-l4-sw-control-flow.md) 会把 WireGuard 握手完成后、CPU 经 HAL 更新 cryptokey 表（含 FCR pause/idle）的完整调用序列串起来，是本讲 `main.cpp` 片段的「前情上下文」。
- **用仿真亲眼看一次 pause/idle**：本讲的时序图实践可以在 [u7-l2 VProc 虚拟处理器协同仿真](u7-l2-vproc-cosim.md) 里实测，在 `VUserMain0.cpp` 里对 FCR 做一次写—轮询—读，配合波形验证你对状态机的理解。
