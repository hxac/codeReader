# axi_inval_filter：缓存无效化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `axi_inval_filter` 解决什么问题：在没有硬件缓存一致性（cache coherence）的片上系统里，如何让一段「绕过缓存、直写内存」的 AXI 写，同步地把 L1 缓存里的旧副本废掉。
- 把它的结构拆成三块：**AW FIFO（监听）+ 全通道直通（透明）+ 无效化 FSM（副作用）**，并能对照源码指出每一块在哪几行。
- 手算「一个写突发跨越了几条 cacheline」，并推演出模块会依次吐出哪些 `inval_addr_o`。
- 把它和上一讲的 `axi_atop_filter` 归入同一类「**监听 AW + 产生副作用**」模块，同时说清两者的本质差别（一个改写 AXI 流量、一个完全不改）。
- 在带 cache 的系统图上，标出 `axi_inval_filter` 应当插在哪一侧、它的 `inval_*` 端口接到哪里。

## 2. 前置知识

本讲是 **advanced** 层级，假定你已经掌握：

- **AXI4 五通道与握手**（u1-l3）：写事务走 AW（写地址）/W（写数据）/B（写响应），读事务走 AR/R；`valid && ready` 同高才算一次握手。
- **req_t / resp_t 结构体与 typedef 宏**（u2-l4）：本模块内核只认 `req_t`/`resp_t` 和 `aw_chan_t`，不再出现裸信号。
- **fifo_v3 与 spill_register**（u7-l1）：本模块用一个 `fifo_v3`（`FALL_THROUGH=1`）缓存 AW。
- **ATOPs 与 axi_atop_filter**（u15-l1）：本讲会把 `inval_filter` 与 `atop_filter` 并列比较，二者是同一家族。

两个本讲会用到的、值得先点明的概念：

- **写旁路（write bypass）**：一笔写事务不经过某个 cache、直接落到它后面的内存，于是 cache 里那份旧数据就「过期」了——若不废掉，后续读会读到陈旧值。
- ** cacheline（缓存行）**：cache 与内存之间搬运数据的最小单位，本模块参数 `L1LineWidth` 就是**一行有多少字节**。地址低若干位是「行内偏移」，高位是「行号」。

> 术语约定：本讲里「无效化（invalidation）」指让 cache 丢弃某一行（标记为无效），下次访问该地址时被迫从内存重取；它与「清除（clean/flush，把脏行写回）」是两回事。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，辅以两处背景佐证：

| 文件 | 作用 |
| --- | --- |
| [src/axi_inval_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv) | 唯一的核心源码，148 行。监听 AW、直通其余通道、对外发 cacheline 无效化请求。 |
| [README.md:45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L45) | 仓库对本模块的一句话定位。 |
| [Bender.yml:56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L56) | 本模块处于编译 **Level 2**，与 `axi_atop_filter` 同层，只依赖更底层的类型与 `fifo_v3`。 |

注意：**本模块在 `test/` 下没有专门的 testbench**（`tb_axi_inval_filter.sv` 不存在），仓库内也没有任何 `src/` 模块例化它——它是一个**面向外部系统的叶子模块**。因此本讲的「代码实践」以源码精读与手算为主，凡涉及实际波形均标注「待本地验证」。

## 4. 核心概念与源码讲解

### 4.1 监听 + 副作用：模块定位与要解决的问题

#### 4.1.1 概念说明

设想这样一个系统：一个 RISC-V 核的 L1 指令缓存（I-cache）缓存了某段代码；同时一条 DMA 或某个非缓存主端正在往**同一段内存地址**写新代码（典型场景：**自修改代码 SMC**、运行时加载、调试器写指令）。由于没有硬件一致性网络，I-cache 不会自动知道内存被改了，核还会执行旧指令。

`axi_inval_filter` 就是插在这条「写内存」路径上的一个**窥探器**：它让写事务**原封不动地**通过，同时**偷看每一笔写的地址（AW）**，算出这笔写碰到了哪些 cacheline，然后向 L1 cache 逐行发出「这一行作废」的请求。

它与上一讲的 `axi_atop_filter` 属于同一类设计模式——**「监听 AW + 产生副作用」**：

| 维度 | `axi_atop_filter`（u15-l1） | `axi_inval_filter`（本讲） |
| --- | --- | --- |
| 监听哪个通道 | AW（读 `aw_atop`） | AW（读 `aw.addr/len/size`） |
| 副作用去哪 | **改 AXI 流量本身**：吃掉原子写，在 R/B 通道注入响应 | **不改 AXI**：从一个**独立的非 AXI 端口**发 `inval_*` |
| 是否修改 AXI 语义 | 是（重写响应） | **否（完全透明）** |
| 跟踪在途写 | 用在途写计数器 | 用一个 AW FIFO（深度 = `MaxTxns`） |

一句话总结共性：**两者都内联在 AXI 路径上、都只在 AW 上取信息、都为每一笔写关联一个外部副作用、都需要一个有界结构记住「还没处理完的写」。** 一句话总结差别：**`atop_filter` 会动 AXI，`inval_filter` 一根线都不动 AXI，只额外旁路通知。**

#### 4.1.2 核心流程

```text
                ┌──────────────── axi_inval_filter ────────────────┐
  上游 master ──►│  slv_req_i                                     │
                │                                                 │
                │   ┌─────────────┐                               │
                │   │  全通道直通  │── mst_req_o ──► 下游内存/外设  │
                │   │ (AW/W/B/AR/R)│◄─ mst_resp_i                  │
                │   └──────┬──────┘                               │
                │          │ 仅 AW 被分叉                          │
                │          ▼                                      │
                │   ┌─────────────┐    ┌──────────────┐            │
                │   │   AW FIFO   │──► │ 无效化 FSM    │── inval_* ─► L1 cache
                │   │ (深度MaxTxns)│    │ (算 cacheline)│            │
                │   └─────────────┘    └──────────────┘            │
                └──────────────────────────────────────────────────┘
```

读法：

1. **写事务照常走完** AW→W→B 五通道握手，从上游直达下游内存。
2. 每当一笔 AW 在 slave 侧握手成功且 `en_i` 为高，就把这条 AW **压进**一个 FIFO。
3. 一个小 FSM 从 FIFO 队头取 AW，**按 cacheline 逐行**向 L1 发无效化地址，由 L1 的 `inval_ready_i` 控制节拍。
4. 一笔 AW 的所有行都发完后，把它从 FIFO 弹出；若 FIFO 满，就**反压**新的 AW（让 `aw_ready=0`）。

#### 4.1.3 源码精读

模块头注释把职责一句话说尽（**监听 AW，其余通道直通**）：

[src/axi_inval_filter.sv:6-8](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L6-L8) — 头注释。

四个参数里，`MaxTxns` 决定能同时挂起多少笔写（即 FIFO 深度），`L1LineWidth` 是以**字节**为单位的缓存行长度：

[src/axi_inval_filter.sv:10-18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L10-L18) — 参数声明：`MaxTxns / AddrWidth / L1LineWidth / aw_chan_t / req_t / resp_t`。

端口分三组——slave 侧、master 侧、以及那组**非 AXI 的无效化输出端口**（`inval_addr_o / inval_valid_o / inval_ready_i`）：

[src/axi_inval_filter.sv:32-36](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L32-L36) — 无效化请求输出端口，与 AXI 端口并列但独立。

注意还有一个**使能**输入 `en_i`（[L20-L23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L20-L23) 的 `en_i`）：只有需要被 L1 监听的写流才把 `en_i` 拉高，其余写直接透明通过、不发无效化。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「监听点」与「副作用出口」，建立心智模型。

1. 打开 [src/axi_inval_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv)。
2. 找到「哪一根信号是监听触发」——即 `aw_fifo_push`（见 4.2.3）。它依赖哪个通道？是否受 `en_i` 控制？
3. 找到「副作用出口」——即 `inval_addr_o / inval_valid_o`（见 4.2.3），确认它们**不**属于 `req_t/resp_t`，是独立的非 AXI 端口。
4. 对照上表，写出 `inval_filter` 与 `atop_filter` 的一处共性与一处差异。

**需要观察/回答**：监听触发依赖 **AW 通道的握手 + `en_i`**；副作用出口是**独立端口**；共性是「都只从 AW 取信息、都为每笔写关联副作用」，差异是「inval_filter 不改 AXI、atop_filter 改写响应」。

#### 4.1.5 小练习与答案

**Q1**：如果把 `en_i` 恒接 0，模块退化为什麽？

**答**：`aw_fifo_push` 恒为 0（见 4.2.3 的 L49），FIFO 永远空，`inval_valid_o = ~empty = 0`，于是模块**完全不发无效化请求**，整段 AXI 路径退化为一根纯直通连线。`en_i` 就是这个特性的总开关。

**Q2**：为什么 `inval_filter` 不像 `atop_filter` 那样需要触碰 R/B 通道？

**答**：因为它的副作用对**外部**（L1 cache）发，不在 AXI 上回灌任何东西；写事务的 B 响应由下游内存正常产生、原样回传即可，模块无需替谁编造响应。

---

### 4.2 数据通路：AW FIFO 与全通道直通

#### 4.2.1 概念说明

本模块的 AXI 数据通路极其简单——**默认全部直通**（`mst_req_o = slv_req_i; slv_resp_o = mst_resp_i;`）。它既不改地址、不改数据、也不改响应码，连 `atop_filter` 那种「替下游回答」都没有。唯一的「扰动」是：当它自己的 AW FIFO 满了，会**临时**把 AW 通道的反压拉起来，拒绝接收新的写地址。

之所以需要这个 AW FIFO，是因为「写地址到达」和「L1 接受无效化请求」**两件事节奏不同**：

- AW 可以一拍接一拍地高速到来（上游可能连续发起多笔写）。
- L1 的 `inval_ready_i` 可能因为正在做别的事而延迟。

于是模块用一个**有界 FIFO** 把「还没来得及发完无效化」的 AW 暂存起来，保证**每一笔被接受的写都不会丢、都会被逐行无效化**。FIFO 深度就是参数 `MaxTxns`，即「允许同时在途、尚未处理完无效化的最大写突发数」。

#### 4.2.2 核心流程

```text
每个时钟沿：
  aw_fifo_push = en_i && (AW 在 slave 侧握手成功)        // 记录一笔新写
  if (FIFO 满):
      slv_aw_ready = 0, mst_aw_valid = 0                 // 暂停接收新 AW
  else:
      AW/W/B/AR/R 全部逐根直通                            // 透明转发

队头 AW 的无效化由 FSM 处理（见 4.3），处理完执行 aw_fifo_pop。
```

关键不变量：**一笔 AW 一旦进入 FIFO，就保证会为其发出完整的 cacheline 无效化序列后才弹出**；FIFO 满时的反压是 AXI 合法的背压（`valid` 期间载荷稳定），不破坏协议。

#### 4.2.3 源码精读

AW FIFO 的控制信号先声明，`push` 只在「`en_i` 有效且 AW 握手成功」时拉高——这就是监听的物理实现：

[src/axi_inval_filter.sv:44-49](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L44-L49) — `aw_fifo_push = en_i & slv_req_i.aw_valid & slv_resp_o.aw_ready`。

整段 AXI 处理只有一个 `always_comb`，默认全直通，仅在 FIFO 满时掐断 AW：

[src/axi_inval_filter.sv:61-71](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L61-L71) — 默认 `mst_req_o = slv_req_i; slv_resp_o = mst_resp_i;`；`aw_fifo_full` 时强制 `aw_ready=0 / aw_valid=0`。

FIFO 本体是一个 `FALL_THROUGH=1` 的 `fifo_v3`，存的是整条 `aw_chan_t`（含 addr/len/size 等无效化计算要用的字段）：

[src/axi_inval_filter.sv:130-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L130-L146) — `fifo_v3` 例化，深度 `MaxTxns`、类型 `aw_chan_t`、`FALL_THROUGH=1`。

> 名词解释：`FALL_THROUGH=1`（直通模式）下，FIFO 空、push 当拍就能在 `data_o` 看到数据；这样队头 `aw_fifo_data` 在多拍无效化期间保持稳定，便于 FSM 持续读它的地址。

#### 4.2.4 代码实践（源码阅读 + 推理型）

**目标**：理解反压闭环，预测 FIFO 满时的波形。

1. 阅读 [src/axi_inval_filter.sv:61-71](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L61-L71)，确认除 AW 外，W/B/AR/R 是否**任何一根**被模块修改。
2. 假设 `MaxTxns = 4`，上游连续发起 6 笔单拍写、而 L1 的 `inval_ready_i` 长期为 0。
3. 推演：第几笔 AW 之后 `aw_fifo_full` 会拉高？此后 `slv_resp_o.aw_ready` 与 `mst_req_o.aw_valid` 各是什么？

**预期结果**：前 4 笔 AW 进 FIFO 后第 5 笔到来前 FIFO 已满 → `aw_fifo_full=1` → `slv_resp_o.aw_ready=0`（拒绝新 AW）、`mst_req_o.aw_valid=0`（暂不下发）。第 5、6 笔被反压挂起，直到 L1 放行、FSM 弹出若干 AW 腾出空间。W/B 等通道**不受影响**，仍按下游 ready 正常流动。（实际波形**待本地验证**：可在测试台里把 `inval_ready_i` 接一个慢速发生器观察。）

#### 4.2.5 小练习与答案

**Q1**：为什么反压只掐 AW，而不掐 W？

**答**：无效化只跟「写到了哪个地址」有关，信息全在 AW 里；W 只是数据载荷。而且 AXI 规定 W 必须跟随其 AW、且同方向保序，若随意反压 W 会破坏与 AW 的配对关系。模块让 W/B 全程透明，由下游自行反压，是最安全的做法。

**Q2**：把 `MaxTxns` 设得过小（如 1）会有什么后果？

**答**：FIFO 几乎总是满，每来一笔 AW 都要等它对应的所有 cacheline 无效化请求全部发完才能收下一笔，**写吞吐被无效化节拍拖累**。`MaxTxns` 应至少覆盖「上游典型在途写数 × 每笔平均行数」带来的占用，否则 inval 通路会成为写的瓶颈。

---

### 4.3 无效化 FSM：单行与多行跨越的地址游走

#### 4.3.1 概念说明

这是本模块唯一的「算法」部分。问题很具体：**给定一笔写突发的地址、拍数、每拍字节，以及 cacheline 长度，它碰到了哪几条 cacheline？模块就向 L1 发这几条地址的无效化请求。**

先约定记号（\(L\) = `L1LineWidth`，单位字节）：

- 突发总字节数：\[ B_{\text{burst}} = (\text{len}+1)\cdot 2^{\text{size}} \] —— `(len+1)` 是拍数，\(2^{\text{size}}\) 是每拍字节数（AXI 的 `size` 就是 \(\log_2\)每拍字节数）。
- 起始地址的**行内偏移**：\( a_{\text{line}} = \text{addr} \bmod L \)，源码里写成 `addr[idx_width(L)-1:0]`。
- 当前行**还剩多少字节**：\( R = L - a_{\text{line}} \)。

判定准则只有一条：

- 若 \( R < B_{\text{burst}} \) —— 突发尾部越过了当前行边界，**跨多条行**，需要逐行多发；
- 否则—— 整笔突发落在一行内，**只发一条**（当前行）即可。

跨多行时，FSM 从「起始行」出发，每接受一次 `inval_ready_i` 就把地址偏移加上一个 \(L\)，跳到下一条行，直到偏移越过 \(B_{\text{burst}}\) 结束。这样保证**只无效化真正被写到的行**，不空发。

#### 4.3.2 核心流程

两态 FSM（`Idle` / `Invalidating`），无效化地址 = `队头AW.addr + inval_offset_q`：

```text
Idle（偏移=0）:
  if 队列非空 && inval_ready_i:
      本拍已发出「起始行」的无效化（inval_addr = addr + 0）
      if R < B_burst:                       // 跨多行
          offset ← L - a_line               // 指向下一条行
          state ← Invalidating
      else:                                 // 单行，已发完
          pop AW                            // 弹出，回到 Idle

Invalidating:
  if inval_ready_i:
      offset ← offset + L                  // 跳到下一条行
      if offset >= B_burst:                 // 已覆盖整个突发
          offset ← 0
          pop AW
          state ← Idle
```

注意两个要点：

1. **`inval_valid_o = ~aw_fifo_empty` 是组合输出**——只要队列里有 AW，无效化请求就持续有效，由 L1 的 `inval_ready_i` 控制何时被接受、FSM 何时推进。
2. **无效化由 AW 触发，与 W/B 解耦**：写数据（W）和写响应（B）何时流动都不影响无效化的发出；只要写地址被接受，无效化就开始。这对「写旁路」场景是对的——一知道要写哪，就立刻让 I-cache 那份旧副本失效。

#### 4.3.3 源码精读

无效化地址与有效的组合产生式，地址 = 队头 AW 地址 + 偏移寄存器：

[src/axi_inval_filter.sv:52-55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L52-L55) — `inval_addr_o = aw_fifo_data.addr + inval_offset_q; inval_valid_o = ~aw_fifo_empty;`。

> 这里用到的 `idx_width` 来自 [src/axi_inval_filter.sv:38](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L38) 的 `import cf_math_pkg::idx_width;`。`idx_width(L)` 返回寻址 \(L\) 个字节所需的位数，即 \(\lceil\log_2 L\rceil\)，所以 `addr[idx_width(L)-1:0]` 正是「行内字节偏移」。

FSM 的状态枚举与组合逻辑主体：

[src/axi_inval_filter.sv:77-102](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L77-L102) — `Idle` 分支：判定跨行条件 \((L - a_{\text{line}}) < B_{\text{burst}}\)，跨行则置 `offset = L - a_line` 并进入 `Invalidating`，否则直接 `pop`。

[src/axi_inval_filter.sv:105-116](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L105-L116) — `Invalidating` 分支：每接受一次就 `offset += L`，当 `offset >= B_burst` 时清零、`pop`、回 `Idle`。

状态与偏移寄存器：

[src/axi_inval_filter.sv:120-128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L120-L128) — 异步复位、同步更新的 `state_q` 与 `inval_offset_q`。

#### 4.3.4 代码实践（手算型）

**目标**：用一组具体参数手算无效化地址序列，验证你对 FSM 的理解。

设 `L1LineWidth = 64`（\(L=64\)，64 字节行），一笔写突发：

- `addr = 'h40`（即 64，正好落在第 1 条行的起点），`size = 2`（每拍 4 字节），`len = 31`（32 拍）。

请按步骤推演：

1. 算 \(B_{\text{burst}} = (31+1)\cdot 2^{2} = 128\) 字节。
2. 算 \(a_{\text{line}} = 64 \bmod 64 = 0\)，\(R = 64 - 0 = 64\)。
3. 比较 \(R < B_{\text{burst}}\)？即 \(64 < 128\) → **是**，跨多行。
4. 推演 FSM：`Idle` 发 `'h40`（第 1 条行），置 `offset = 64-0 = 64`，进 `Invalidating`；接受一次后 `offset = 64+64 = 128`，因 \(128 \ge 128\) → 结束。

**预期结果**：模块对外只发 **2 个**无效化地址：`'h40` 和 `'h40+'h40 = 'h80`，分别对应被写到的第 1、第 2 条 cacheline。

再试一个**单行**的例子自检：`addr='h4C, size=2, len=0`（一笔 4 字节单拍写，落在第 0 条行内）→ \(B_{\text{burst}}=4\)，\(a_{\text{line}}=0x0C=12\)，\(R=64-12=52\)，\(52 < 4\)? 否 → 单行，只发 `'h4C` 一次即 `pop`。（以上手算结论的波形**待本地验证**。）

#### 4.3.5 小练习与答案

**Q1**：若 `L1LineWidth` 不是 2 的幂（比如 60），这套位切片 `addr[idx_width(L)-1:0]` 还成立吗？

**答**：不成立。代码用位切片直接取低 \(\lceil\log_2 L\rceil\) 位作为行内偏移，**默认 \(L\) 是 2 的幂**（这也是真实 cacheline 的普遍前提）。若 \(L\) 非 2 的幂，地址到行号的映射须改成除法/取模，模块并不支持；使用时务必让 `L1LineWidth` 为 2 的幂且 \(\ge 2\)。

**Q2**：为什么 `Invalidating` 的结束判断用「`offset >= B_burst`」而不是「`offset > 某个固定值`」？

**答**：因为突发的实际跨度 \(B_{\text{burst}}\) 随 `len/size` 变化，是运行期数据，不是编译期常量。把游走终止条件绑在「偏移是否已覆盖整个突发字节数」上，才能对任意长度/粒度的写都恰好无效化被写到的行——不多发、不少发。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个**系统部署说明**任务（这是本讲指定的实践）。

**背景系统**：一个 RISC-V 核，含 L1 I-cache（缓存指令内存）；另有一个 DMA 引擎会向**同一段指令内存**搬运新代码（自修改代码场景）。系统**没有**硬件 cache 一致性。请写一段工程说明（配文字框图），回答：

1. **`axi_inval_filter` 应部署在哪一侧？** 提示：它要能窥探到「所有会改到 I-cache 所缓存内存的写」。画出 `DMA → ? → 指令内存`，标出模块插在哪、`inval_*` 三根线接到 I-cache 的哪个端口（参考 [src/axi_inval_filter.sv:32-36](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_inval_filter.sv#L32-L36) 的端口含义）。
2. **它如何与正常写事务协作？** 说明：写数据/响应是否受影响？无效化由哪个通道触发、与 W/B 是否同步？`en_i` 在这里应接什么（提示：只有 DMA 这类「写旁路」流才需要触发无效化，核自身普通的、经过 I-cache 的写**不应**重复触发）。
3. **反压边界**：当 I-cache 的 `inval_ready_i` 很慢时，写吞吐会受什么影响？`MaxTxns` 该怎么取（参考 4.2.5）？

**参考要点（写完再对照）**：

```
DMA_master ──► [axi_inval_filter] ──► 指令内存(SRAM/下游)
                     │
                     └──inval_addr_o / inval_valid_o──► L1 I-cache 无效化输入
                     <──inval_ready_i◄──
        en_i = '1' （仅这条 DMA 写路径需要触发）
```

- 模块插在 **DMA 写路径到指令内存之间**，写事务**完全透明**地通过（W/B 不受影响，由下游内存正常处理）。
- 无效化**由 AW 触发**（地址一被接受就开始），与 W/B 不同步——这正是「尽早让旧指令副本失效」所需要的。
- `en_i` 只在 DMA 这条旁路路径上拉高；核自己经 I-cache 的写不应再触发（否则重复无效化）。
- I-cache 慢时，AW FIFO 会涨，满后反压新 AW → DMA 写被节流；`MaxTxns` 应 ≥ DMA 典型在途写数，避免 inval 通路成为吞吐瓶颈。

> 进阶（可选，**待本地验证**）：参考 u3 的 `rand_master`/`axi_sim_mem` 搭一个 `rand_master → axi_inval_filter → axi_sim_mem` 的最小台，把 `inval_*` 接到一个自制的「行无效化记分板」，对若干随机写后断言「被写到的每一行都恰好收到一次无效化、未被写到的行收不到」——这相当于为这个无官方 TB 的模块补一台定向随机测试台。

## 6. 本讲小结

- `axi_inval_filter` 解决**无硬件一致性系统**中的写旁路一致性：让绕过 cache 直写内存的 AXI 写，同步废掉 L1 里的旧 cacheline。
- 它与 `axi_atop_filter` 同属「**监听 AW + 产生副作用**」家族，但本质区别是：**inval_filter 不改一根 AXI 线**（纯透明），只从一个**独立的非 AXI 端口**旁路通知 L1。
- 结构三块：**AW FIFO（`fifo_v3`，深度 `MaxTxns`，受 `en_i` 控制）+ 全通道直通（FIFO 满时才反压 AW）+ 两态无效化 FSM**。
- FSM 用 \(B_{\text{burst}}=(\text{len}+1)\cdot2^{\text{size}}\) 与行内偏移判定**单行/多行跨越**，多行时按 `L1LineWidth` 步进逐行发无效化地址，恰好覆盖被写到的行。
- 无效化由 **AW 触发、与 W/B 解耦**；FIFO 满时通过合法的 AXI 背压（`aw_ready=0`）暂停新写，`MaxTxns` 直接决定写吞吐能否不被 inval 节拍拖累。
- 这是一个**叶子模块**：仓库内无内部例化、无官方 testbench，理解它主要靠源码精读与手算。

## 7. 下一步学习建议

- **横向对比**：回到 [src/axi_atop_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv)（u15-l1），把两个模块的「在途写跟踪结构」（FIFO vs 计数器）和「副作用是否改 AXI」逐项列表，巩固「监听+副作用」这一设计模式。
- **纵向进阶**：继续 u15-l3（`axi_xp` 与 `interleaved_xbar`），看更复杂的互连如何在拓扑层处理一致性/路由；再到 u15-l4（异构网络），把 `inval_filter` 与 `cdc/dw/iw_converter/isolate` 一起部署进一个完整 SoC 互联。
- **验证补全**：本模块无官方 TB。建议参照 u3-l3 的 `tb_axi_lite_regs.sv` 骨架，自行为 `axi_inval_filter` 写一台定向随机测试台（综合实践第 3 步已给出思路），这也是对本库贡献 PR 的好入口（参见 u16-l4 的贡献流程）。
