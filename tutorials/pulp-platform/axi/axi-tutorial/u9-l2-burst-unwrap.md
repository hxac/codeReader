# axi_burst_unwrap：回卷突发展开

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 AXI **回卷突发（WRAP burst）** 的地址推进与回卷规则，以及它为何被 cache 子系统偏爱。
- 解释 `axi_burst_unwrap` 的核心思想：**把一个 WRAP 突发在地址方向展开成「至多两个 INCR 递增突发」**，且对数据完全透明。
- 手算出一个具体 WRAP 突发被 unwrap 后下游收到的 INCR 地址序列，并与原 WRAP 序列逐拍对照。
- 读懂 `src/axi_burst_unwrap.sv` 的三层结构：顶层「支持/不支持事务分流」、`axi_burst_unwrap_ax_chan` 的 `Idle/Busy` 拆分状态机、以及 W/B/R 三条响应通道的「拆/合」逻辑。

## 2. 前置知识

本讲承接 [u9-l1](u9-l1-burst-splitter.md)，假定你已经熟悉：

- AXI 五通道与 `valid/ready` 握手（见 [u1-l3](u1-l3-axi-protocol-primer.md)）。
- 突发三要素 `len`（拍数 − 1）、`size`（每拍 \(2^{size}\) 字节）、`burst`（`BURST_FIXED/INCR/WRAP`），见 [u2-l1](u2-l1-axi-pkg-types.md)。
- 「接口外壳 + 结构体内核（`req_t`/`resp_t`）」范式，以及 `axi_demux`、`axi_err_slv` 的基本作用，见 [u5-l1](u5-l1-demux-simple-and-demux.md) 与 [u6-l2](u6-l2-xbar-addr-map-decode.md)。

两个本讲会用到的术语：

- **WRAP（回卷）突发**：地址从起始点递增，一旦越过「回卷边界 + 容器大小」就折回回卷边界继续递增。CPU 取 cache 行时常用它——这样**关键字（critical word）先到**，其余拍环绕着补齐。
- **容器大小（container size）**：一个 WRAP 突发所能触及的地址窗口大小，等于「每拍字节数 × 拍数」。WRAP 永远不会越出这个窗口。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/axi_burst_unwrap.sv` | 本讲主角。顶层 `axi_burst_unwrap` + 内部 `axi_burst_unwrap_ax_chan`（地址拆分）+ `axi_burst_counters`（outstanding 计数）。 |
| `src/axi_pkg.sv` | 提供 `BURST_WRAP`/`BURST_INCR` 常量、`wrap_boundary()` 与 `beat_addr()` 函数——理解 WRAP 地址规则的「协议字典」，也是手算练习的黄金对照工具。 |
| `src_files.yml` | 把 `axi_burst_unwrap.sv` 归在 **Level 2**，与 `axi_burst_splitter_gran` 同层，说明它是一个独立的「突发整形」积木。 |
| `README.md` | 第 27 行一句话定位：「把 AXI4 回卷突发转换成至多两个递增突发」。 |

## 4. 核心概念与源码讲解

### 4.1 WRAP 突发的回卷地址规则

#### 4.1.1 概念说明

`BURST_INCR` 的地址每拍线性递增，非常直观；`BURST_WRAP` 则在递增到窗口顶端时「折返回窗口底部」。协议对 WRAP 有两条硬约束（见 `axi_pkg` 中 `wrap_boundary` 函数的断言与注释）：

1. **长度只能是 2/4/8/16 拍**，即 `len` 只能是 `1/3/7/15`。
2. **回卷边界（wrap boundary）** 是窗口的最低地址，按 `addr` 向下对齐到「容器大小」得到。

窗口（容器）大小为：

\[
\text{container\_size} = \text{num\_bytes}(size) \times (len + 1) = 2^{size} \times (len+1)
\]

因为 \(len+1 \in \{2,4,8,16\}\) 且 \(2^{size}\) 也是 2 的幂，所以 `container_size` 必是 2 的幂，回卷边界就是把起始地址的低位「抹零」到 `container_size` 对齐。

#### 4.1.2 核心流程

设起始地址 `addr`、回卷边界 `wb`、容器大小 `cs`，则 WRAP 的地址序列为：

1. 第 0 拍：`addr`。
2. 第 \(i\) 拍（\(i>0\)）：先按 INCR 算出 `aligned_addr(addr,size) + i*2^size`。
3. 若该地址 \(\geq wb + cs\)，则减去 `cs`（即折回窗口底部），得到回卷后的地址。
4. 直到拍完 `len+1` 拍为止。

这正是 `axi_pkg::beat_addr` 对 `BURST_WRAP` 的实现。

#### 4.1.3 源码精读

`axi_pkg` 用一个 `case (len)` 把地址按窗口对齐，等价于「乘以拍数后再对齐」——[src/axi_pkg.sv:155-161](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L155-L161)：对 `len=7`（8 拍）就是 `addr >> (size+3) << (size+3)`，即对齐到 \(2^{size+3} = 8 \times 2^{size} = \text{container\_size}\)。

回卷判定在 `beat_addr` 里——[src/axi_pkg.sv:191-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L191-L193)：当地址越过 `wrap_boundary + container_size` 时减去 `container_size`。`BURST_WRAP` 常量定义在 [src/axi_pkg.sv:87](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L87)（`2'b10`），`BURST_INCR` 在 [src/axi_pkg.sv:81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L81)（`2'b01`）。

#### 4.1.4 代码实践

**目标**：用 `axi_pkg::beat_addr` 当作「黄金参考」，手算一个 WRAP 突发的地址序列，确认你对回卷规则的理解。

**操作步骤**：

1. 取参数：`size = 3`（每拍 8 字节）、`len = 7`（共 8 拍）、起始 `addr = 0x0000_0038`。
2. 算 `container_size = 2^3 × 8 = 64 = 0x40`；`wrap_boundary = 0x38 & ~0x3F = 0x00`。
3. 逐拍套规则：第 0 拍 `0x38`；之后 `0x40, 0x48, ...` 每个都不小于 `wb+cs = 0x40`，所以统统减 `0x40` 折回。

**预期结果（待本地用任意 SV 仿真器跑 `beat_addr` 复核）**：

```
拍0: 0x38   ← 关键字（起始地址）
拍1: 0x00   ← 越过 0x40，折回窗口底
拍2: 0x08
拍3: 0x10
拍4: 0x18
拍5: 0x20
拍6: 0x28
拍7: 0x30
```

注意关键字 `0x38` 最先返回——这正是 cache 行填充想要的行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wrap_boundary` 函数里用一个 `case (len)` 而不是直接写 `addr / container_size * container_size`？

**答案**：因为 `len+1` 只能取 2/4/8/16，乘以它等价于左移 1/2/3/4 位，对齐则等价于先右移再左移同样的位数。`case` 把这件事写成显式的移位，避免综合出除法器/乘法器，纯移位即可实现。

**练习 2**：若 `addr` 恰好等于 `wrap_boundary`（起始地址已对齐到窗口），WRAP 序列还会折回吗？

**答案**：不会。此时起始地址就在窗口底，递增到 `wb + cs` 时刚好访问完整个窗口的最后一拍，没有「越过」可言。所以一个**起始在对齐边界上的 WRAP，其地址序列与同样参数的 INCR 完全相同**——这一点正是 4.2 节 unwrap 可以「零拆分直通」的根据。

---

### 4.2 unwrap 的拆分原理：一个 WRAP = 至多两个 INCR

#### 4.2.1 概念说明

很多下游吃不下 WRAP：AXI-Lite 桥、简单存储控制器、单拍外设往往只认 INCR。`axi_burst_unwrap` 的任务就是在**不改变上游看到的数据顺序与响应语义**的前提下，把 WRAP 改写成 INCR。

关键洞察是：WRAP 的地址序列，从「越过窗口顶端、折回」的那个点切开，天然就是**两段单调递增**的序列：

- **第一段（尾巴先行）**：从 `addr` 递增到窗口顶端 `wb + cs`（不含）。
- **第二段（补齐头部）**：从 `wb` 递增到 `addr`（不含）。

把这两段各封装成一个 `BURST_INCR` 突发，按「先第一段、后第二段」的顺序发给下游，下游看到的地址序列就和原 WRAP **逐拍完全一致**。若 `addr == wb`（4.1.5 练习 2 的情形），第一段就覆盖整个窗口、第二段为空，于是只需要**一个** INCR——这就是「至多两个」的由来。

#### 4.2.2 核心流程

设偏移量 \(o = addr - wb\)（已 size 对齐），两段 INCR 的参数为：

\[
\text{第一段 len} = \frac{cs - o}{2^{size}} - 1,\quad
\text{第一段 addr} = addr
\]

\[
\text{第二段 len} = \frac{o}{2^{size}} - 1,\quad
\text{第二段 addr} = wb
\]

两段拍数之和 \(= \frac{cs-o}{2^{size}} + \frac{o}{2^{size}} = \frac{cs}{2^{size}} = len+1\)，正好等于原 WRAP 的总拍数。这就是 `axi_burst_unwrap_ax_chan` 里那两条 `len` 计算式的来历。

#### 4.2.3 源码精读

`axi_burst_unwrap_ax_chan` 先把容器大小与回卷边界算出来——[src/axi_burst_unwrap.sv:473-475](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L473-L475)：

```systemverilog
assign container_size   = (ax_i.len + 1) << ax_i.size;
assign wrap_boundary = ax_i.addr & ~(AddrWidth'(container_size) - 1);
```

注意 `wrap_boundary` 用「`& ~(cs-1)`」实现对齐，这与 `axi_pkg::wrap_boundary` 的移位写法等价（因为 `cs` 是 2 的幂）。

真正的拆分在 `Idle` 态里——[src/axi_burst_unwrap.sv:489-507](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L489-L507)。当 `burst == BURST_WRAP && addr != wrap_boundary` 时需要拆，关键三行：

```systemverilog
ax_o.len   = ((wrap_boundary + container_size - ax_i.addr) >> ax_i.size) - 1; // 第一段（本拍就发）
ax_d.len   = ((ax_i.addr - wrap_boundary) >> ax_i.size) - 1;                  // 第二段（存起来下拍发）
ax_d.addr  = wrap_boundary;                                                   // 第二段从窗口底起步
```

两段都把 `burst` 改写成 `BURST_INCR`（[L492](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L492) `ax_d.burst = BURST_INCR`，第一段 `ax_o` 直接复用 `ax_d`）。第一段在 `Idle` 态就尝试送出，第二段存进 `ax_q`，等下游接走第一段后状态机进入 `Busy` 态把它送出——[src/axi_burst_unwrap.sv:523-530](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L523-L530)。

无需拆分时（`else` 分支）——[src/axi_burst_unwrap.sv:508-520](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L508-L520)：若仍是 WRAP 但 `addr == wrap_boundary`，就把 `burst` 改成 INCR、其余原样透传（[L511-L513](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L511-L513)）；非 WRAP 事务则完全透传。这就是「至多两个 INCR」中「一个」的零开销路径。

#### 4.2.4 代码实践

**目标**：用 4.2.2 的公式，手算 4.1.4 那个例子（`size=3, len=7, addr=0x38`）被 unwrap 后下游收到的 INCR 序列，验证它与原 WRAP 逐拍一致。

**操作步骤**：

1. `cs = 64`，`wb = 0x00`，偏移 `o = 0x38 - 0x00 = 0x38 = 56`。
2. 第一段：`len = (64 - 56) >> 3 - 1 = 1 - 1 = 0`（1 拍），从 `0x38` 起 → 只有 `0x38`。
3. 第二段：`len = (56) >> 3 - 1 = 7 - 1 = 6`（7 拍），从 `wb = 0x00` 起 → `0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30`。
4. 把两段首尾相接。

**预期结果（待本地验证）**：

```
第一段 INCR (len=0): 0x38
第二段 INCR (len=6): 0x00 0x08 0x10 0x18 0x20 0x28 0x30
拼接: 0x38, 0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30
```

与 4.1.4 的原 WRAP 序列逐拍完全相同——拆分正确。这个例子恰好是「关键字 1 拍 + 其余 7 拍」的 1+7 拆分，非常典型。

#### 4.2.5 小练习与答案

**练习 1**：把起始地址换成 `addr = 0x18`（其余不变），重算两段 INCR。

**答案**：`o = 0x18 = 24`。第一段 `len = (64-24)>>3 - 1 = 5-1 = 4`（5 拍）：`0x18, 0x20, 0x28, 0x30, 0x38`；第二段 `len = 24>>3 - 1 = 3-1 = 2`（3 拍）：`0x00, 0x08, 0x10`。共 8 拍，与原 WRAP（`0x18,0x20,0x28,0x30,0x38,0x00,0x08,0x10`）一致。可见偏移越大，第一段越短、第二段越长。

**练习 2**：若 `addr = 0x00`（落在回卷边界上），状态机会走哪条分支？发几个 INCR？

**答案**：走 `else`（无需拆分）分支，因为 `addr == wrap_boundary`。只发**一个** INCR，`len=7`、`addr=0x00`、`burst` 由 WRAP 改成 INCR，地址序列 `0x00..0x38` 与原 WRAP 一致。

---

### 4.3 顶层分流：哪些事务会被 unwrap，哪些会被拒

#### 4.3.1 概念说明

并非所有事务都能或都需要 unwrap。`axi_burst_unwrap` 在最前面用一只 1:2 的 `axi_demux` 把上游事务分成两路：

- **支持的事务** → 进入拆分内核（4.2 节的 `ax_chan`）。
- **不支持的事务** → 送给一个 `axi_err_slv`，回 `RESP_SLVERR`。

判定准则集中在 `txn_supported()` 函数里。注意它**只拆 WRAP**：INCR/FIXED 原样透传，不相关的 burst 类型不在此模块职责内（那是 `axi_burst_splitter` 的活，见 [u9-l1](u9-l1-burst-splitter.md)）。

#### 4.3.2 核心流程

`txn_supported(atop, burst, cache, len)` 的判定逻辑：

1. `len == 0`（单拍）→ 支持。单拍无需拆分。
2. `atop != 0`（原子操作）→ **不支持**。本模块不处理 ATOP，会回 SLVERR；若上游可能发 ATOP，需在前面加 `axi_atop_filter`（见模块头部注释 [L22-L26](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L22-L26)）。
3. WRAP 且 **非 Modifiable**（`cache` 的 Modifiable 位为 0）→ **不支持**。AXI 规范 A3.4.1 只允许拆分 Modifiable 事务，非 Modifiable 的 WRAP 必须原样送达，故本模块只能拒绝。
4. 其余（含可拆分的 Modifiable WRAP、各种 INCR/FIXED）→ 支持。

AW 与 AR 各自调一次该函数得到 `sel_*_unsupported`，驱动 demux 的选择信号。

#### 4.3.3 源码精读

`txn_supported` 函数定义在 [src/axi_burst_unwrap.sv:97-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L97-L110)，四条规则一目了然。选择信号在 [src/axi_burst_unwrap.sv:111-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L111-L114)：

```systemverilog
assign sel_aw_unsupported = ~txn_supported(slv_req_i.aw.atop, slv_req_i.aw.burst,
                                            slv_req_i.aw.cache, slv_req_i.aw.len);
assign sel_ar_unsupported = ~txn_supported('0, slv_req_i.ar.burst, ...); // AR 没有 atop
```

1:2 demux 把支持路（`act_req`）与不支持路（`unsupported_req`）分开——[src/axi_burst_unwrap.sv:68-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L68-L95)；注意输出数组写成 `{unsupported_req, act_req}`，所以 `select=0` 走 `act_req`、`select=1` 走 `unsupported_req`，与「`~txn_supported`」的极性吻合。不支持路接 `axi_err_slv`（`Resp=RESP_SLVERR`、`MaxTrans=1`）——[src/axi_burst_unwrap.sv:116-129](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L116-L129)。模块末尾的 `assume property`（[L371-L380](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L371-L380)）会在仿真期对上游违约发 `$warning`/`$fatal`，相当于一份可执行规约。

#### 4.3.4 代码实践

**目标**：通过阅读 `txn_supported`，预测几类典型事务的命运。

**操作步骤**：对下表每一行，先自己判断「支持/不支持」，再对照函数确认。

| 事务 | atop | burst | cache(Modifiable) | len | 预测 |
|------|------|-------|------|-----|------|
| A | 0 | WRAP | 1 | 7 | ? |
| B | 0 | WRAP | 0 | 7 | ? |
| C | 非零 | WRAP | 1 | 7 | ? |
| D | 0 | INCR | 1 | 15 | ? |
| E | 0 | WRAP | 1 | 0 | ? |

**预期结果**：A 支持（进入拆分，可能 1 或 2 个 INCR）；B 不支持（非 Modifiable WRAP，回 SLVERR）；C 不支持（ATOP，回 SLVERR）；D 支持（INCR 直接透传，不拆）；E 支持（单拍，直接透传）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi_err_slv` 的 `MaxTrans` 只配 1？

**答案**：注释 [L122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L122) 写明「拆分突发意味着这是一条低速总线」。不支持的事务本就是异常路径，吞吐无关紧要，`MaxTrans=1` 足够，省面积。

**练习 2**：模块头部注释建议「上游可能发 ATOP 时，前面加 `axi_atop_filter`」。为什么不能直接让本模块处理 ATOP？

**答案**：ATOP 的原子读改写会在 AW 通道触发，却又要在 R 通道产生读响应（`ATOP_R_RESP`，见 [u2-l1](u2-l1-axi-pkg-types.md)）。把一个 ATOP WRAP 拆成两段 INCR 会破坏原子语义与响应规则，无法协议合规地处理，所以只能拒绝；上游应先用 `axi_atop_filter` 把 ATOP 滤掉。

---

### 4.4 响应通道的拆合与 outstanding 跟踪

#### 4.4.1 概念说明

4.2 节只解决了「地址方向」的 WRAP→INCR 改写。但 AXI 是五通道双向的：地址拆成两段后，**响应方向也要相应地「合」回去**，否则上游会收到多余的 B 响应或错误的 `last` 标志。三条响应通道各自的处理：

- **W（写数据，上游→下游）**：上游一个 WRAP 写突发只发一组 W 拍、只在末拍带 `last`。拆成两段 INCR 后，下游期望**每段各自有一个 `last`**。所以 W 通道要在第一段末尾**插入一个额外的 `last`**，第二段沿用上游原 `last`。
- **B（写响应，下游→上游）**：两段 INCR 各回一个 B，但上游只期望**一个** B。所以要把两个 B **合并**成一个（并按 `b.resp[1]` 累积错误改写成 SLVERR）。
- **R（读响应，下游→上游）**：两段 INCR 各在自己的末拍带 `last`，但上游只期望**一个** `last`（在整个 WRAP 的末拍）。所以要**抑制中间段的 `last`**，只保留最后一拍的 `last`。

此外，由于响应可以相对请求乱序返回，模块需要按 AXI ID 跟踪每笔在途事务「还剩几拍」，才能正确判断 `last` 该插在哪、合在哪。

#### 4.4.2 核心流程

跟踪机制由 `axi_burst_counters` 提供：

- 每个计数器在请求握手时被「装载」一个长度（`alloc_len`），之后每收到一拍响应就 `dec` 减一，减到 0 表示该突发响应完毕。
- 计数器与 AXI ID 通过一只 `id_queue` 关联：请求时按 `alloc_id` 入队、记下用的是哪个空闲计数器；响应到达时按 `cnt_id`（即响应里的 ID）出队、找到对应计数器读出「剩余拍数 `cnt_len`」。
- 空闲计数器用 `lzc`（前导零检测）分配，保证不冲突。

各通道 FSM 用 `cnt_len` 判断当前拍是否落在段边界：

- W 通道 `WReady/WWait/WFeedthrough`：`w_last_d = act_req.w.last | (w_cnt_len == 0)`——上游原 `last` 或第一段计数到 0，都触发下游 `last`。
- B 通道 `BReady/BWait`：只有 `b_cnt_len == 0`（两段都收完）才向上游送出合并后的 B；中间的 B 直接吸收。
- R 通道 `RFeedthrough/RWait`：`r_last_d = (r_cnt_len == 0)`——只有整个 WRAP 的最后一拍才向上游置 `last`，其余拍的 `last` 被 `act_resp.r.last = 1'b0`（[L315](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L315)）抹掉。

#### 4.4.3 源码精读

AW 与 AR 各例化一个 `axi_burst_unwrap_ax_chan`——[src/axi_burst_unwrap.sv:137-159](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L137-L159)（AW）与 [src/axi_burst_unwrap.sv:278-300](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L278-L300)（AR），靠参数 `AwChan` 区分读/写。两者都向外暴露两组计数器接口：`cnt_len_o[1:0]`、`cnt_req/dec/gnt`，分别服务「主计数（按上游总长跟踪响应）」和「W 段内计数」。

`ax_chan` 内部例化两个 `axi_burst_counters`——[src/axi_burst_unwrap.sv:423-465](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L423-L465)。注意装载长度的差别在 [L443](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L443)：AR 方向装 `ax_i.len`（上游总拍数，用于重建 R 的 `last`）；AW 方向装 `split_len`（拆分时为 1，即数 2 个 B 来合并；不拆时为 0，即数 1 个 B）。

`axi_burst_counters` 的计数器阵列、`lzc` 分配与 `id_queue` 关联在 [src/axi_burst_unwrap.sv:565-634](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L565-L634)；`cnt_len_o` 由「计数器当前值 − 1」得到剩余拍数（[L627-L628](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L627-L628)），当且仅当 `cnt_len_o==0` 且本拍 `dec` 时弹出 `id_queue`（[L630](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L630)）。

W/B/R 三条 FSM 分别在 [L167-L215](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L167-L215)、[L223-L270](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L223-L270)、[L308-L351](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_unwrap.sv#L308-L351)；`WWait`/`BWait`/`RWait` 态的存在都是为了在下游/上游暂时不就绪时**保住已计算的 `last` 与待送响应**，满足 AXI「`valid` 期间载荷稳定」的铁律。

#### 4.4.4 代码实践

**目标**：跟踪一笔 WRAP 写事务在响应方向的「拆/合」全过程。

**操作步骤**：取 4.2.4 的拆分结果（第一段 1 拍、第二段 7 拍，共 8 个 W 拍），逐拍填表。

| 上游 W 拍 # | 上游 `last` | `w_cnt_len`（第二计数器剩余） | 下游 `last`（= `w_last_d`） | 说明 |
|------|------|------|------|------|
| 0 (0x38) | 0 | →0 | 1 | 第一段只有 1 拍，`w_cnt_len==0` 触发额外 `last` |
| 1 (0x00) | 0 | 6 | 0 | 第二段第 1 拍 |
| ... | 0 | ... | 0 | 第二段中间拍 |
| 7 (0x30) | 1 | 0 | 1 | 上游原 `last`，下游也 `last` |

**预期结果（待本地验证）**：下游共看到 **2 个 `last`**（第 0 拍与第 7 拍），对应两段 INCR；下游回 **2 个 B**，被 B 通道合并成 **1 个 B** 送回上游（`b_cnt_len` 从 1 减到 0 才送出）。R 通道同理：若这是读，下游两段各 1 个 `last`，被 R 通道抹掉中间那个，上游只在第 7 拍看到 1 个 `last`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AW 方向的主计数器装载 `split_len`（拆分时为 1）而不是固定的「2」？

**答案**：因为不拆分的事务（INCR、或落在边界上的 WRAP）只产生 1 个 B，此时 `split_len=0`、计数器装 0（即计 1 个 B）。只有真正拆成两段时才装 1（计 2 个 B）。用同一个变量统一表达「要等几个 B 才合并」，避免特判。

**练习 2**：R 通道为何要显式写 `act_resp.r.last = 1'b0`（默认值）？

**答案**：下游两段 INCR 各自会在末拍带 `last`，若直接透传，上游就会在第一段末尾看到一个错误的 `last`，误以为整个 WRAP 结束了。所以默认把 `last` 抹零，只在 `r_cnt_len==0`（整个 WRAP 的真正末拍）时才由 `r_last_d` 置 1——这正是「抑制中间段 `last`，只保留末拍」的实现。

---

## 5. 综合实践

把 4.1～4.4 串起来，完成一次「纸面端到端 unwrap」。

**任务**：设计一笔 cache 行填充读，参数为 `size=3`（8 字节/拍）、`len=7`（8 拍 WRAP）、起始地址 `addr = 0x38`，下游是一个只支持 INCR 的存储端。

**要求**：

1. **算原始 WRAP 序列**：用 4.1 的规则列出 8 拍地址（提示：可用 `axi_pkg::beat_addr` 在仿真里复核）。
2. **算 unwrap 后的下游序列**：用 4.2 的两段公式，写出下游收到的 INCR 突发个数、每段的 `addr/len/burst`，以及拼接后的地址序列，确认与第 1 步逐拍一致。
3. **预测响应方向**：说明下游 R 通道会回几个带 `last` 的拍、R 通道 FSM 会向上游送几个 `last`、分别在哪一拍。
4. （可选，待本地验证）仿照 [u3-l3](u3-l3-write-a-testbench.md) 的拓扑，搭一个 `axi_rand_master → axi_burst_unwrap → axi_sim_mem` 的最小测试台，让 rand_master 发 WRAP 读，用 scoreboard 验证读回数据顺序与直接读 sim_mem 一致。本仓库没有现成的 `tb_axi_burst_unwrap`，这一步需要你自行编写测试台；若手边无仿真器，第 1～3 步的手算对照已是合格的「源码阅读型实践」。

**参考答案要点**：

- WRAP 序列：`0x38, 0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30`。
- 下游收 **2 个 INCR**：第一段 `addr=0x38, len=0`（1 拍）；第二段 `addr=0x00, len=6`（7 拍）；拼接序列与 WRAP 完全相同。
- R 响应：下游两段末拍各带一个 `last`（共 2 个），但 R 通道 FSM 抑制第一段的 `last`，上游只在第 8 拍（`0x30`）看到 1 个 `last`。

## 6. 本讲小结

- `axi_burst_unwrap` 把 **WRAP 回卷突发**改写成 **至多两个 INCR 递增突发**，让不支持回卷的下游也能服务 cache 行填充类访问。
- 拆分的数学核心：WRAP 序列从「越过窗口顶端折回」处切开，恰好是两段单调递增；第一段从 `addr` 到 `wb+cs`，第二段从 `wb` 到 `addr`。落在回卷边界上的 WRAP 等价于单个 INCR，走零开销直通。
- 顶层用 1:2 `axi_demux` 按 `txn_supported()` 分流：单拍、Modifiable 的 WRAP、INCR/FIXED 进入拆分内核；ATOP 与非 Modifiable WRAP 被拒，回 `RESP_SLVERR`。
- 地址方向的拆分由 `axi_burst_unwrap_ax_chan` 的 `Idle/Busy` 状态机完成；响应方向由 W/B/R 三条 FSM 配合 `axi_burst_counters`（按 AXI ID 跟踪在途拍数）完成「W 插 last、B 合并、R 抑制中间 last」。
- 整个变换对**数据与顺序完全透明**：拼接后的地址序列与原 WRAP 逐拍一致，这是它能在任意 INCR 下游前透明插入的根本保证。

## 7. 下一步学习建议

- 阅读 [u9-l3](u9-l3-axi-serializer.md) 的 `axi_serializer`：它把不同 ID 的事务串行化为同一 ID，常与 `axi_burst_unwrap`/`axi_burst_splitter` 串联，喂给只能处理单 ID、单拍/INCR 的窄下游。
- 对比 [u9-l1](u9-l1-burst-splitter.md) 的 `axi_burst_splitter`：两者都是「地址方向拆、响应方向合」，但 splitter 按固定/可配粒度拆 INCR，unwrap 专门把 WRAP 展开成 INCR——理解它们的分工有助于在真实 cache 子系统里选型。
- 若想看 WRAP 在更大场景中的角色，可继续阅读 `axi_dw_downsizer`/`axi_dw_upsizer`（[u11](u11-l1-dw-downsizer.md)），它们在宽度转换时也会重算突发地址，复用了 `axi_pkg` 同一套地址函数。
