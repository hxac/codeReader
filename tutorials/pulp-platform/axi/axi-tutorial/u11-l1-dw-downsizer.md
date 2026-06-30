# axi_dw_downsizer：宽到窄

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「宽到窄数据宽度转换」到底在转换什么：不是改一根线，而是把**一个宽 beat 拆成多个窄 beat**，并重算地址、`size`、`len`、`strobe` 与响应拍数。
- 推导 downsize 的核心数学：转换比 `conv_ratio`、对齐修正 `align_adj`、以及输出 `len` 如何由输入 `len` 算出，何时需要把一个长突发再**拆成多段 ≤256 拍**的 INCR。
- 读懂 `axi_dw_downsizer` 的读通路（`AxiMaxReads` 个并行引擎 + `id_queue` 路由 + R 拍「序列化」）与写通路（lane steering + strb 重算 + 多个 B 合并成一个）。
- 看懂 `test/tb_axi_dw_downsizer.sv` 如何用随机主从 + 双端口 monitor 自检宽度转换的正确性。

本讲依赖 **u4-l1（join/cut/multicut 与「接口外壳 + 结构体内核」范式）**，并承接 **u1-l3（AXI 协议：len/size/burst/strobe）** 与 **u2-l2（axi_pkg 的 `aligned_addr`、`resp_precedence` 等函数）**。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**宽 beat 与窄 beat。** AXI 的数据总线有一个固定宽度（如 64 位）。一个 beat（拍）就是总线上一次握手所搬运的数据，其有效载荷由 `size` 决定：`num_bytes = 2^size`。一个 64 位端口一拍最多搬 8 字节（`size=3`），一个 32 位端口一拍最多搬 4 字节（`size=2`）。

**为什么宽到窄「一拍要拆多拍」。** 假设上游（slave 端口，宽）发来一个 64 位 beat，下游（master 端口，窄）只有 32 位。这一个 64 位 beat 装的 8 字节，下游得分**两拍**才能搬完。所以宽→窄的本质是「**地址方向展开、数据方向切分、响应方向再合并**」。

**strobe（写选通）跟着重算。** 写事务每拍带一个 `wstrb`，每比特对应一个字节车道，标记这一拍里哪些字节是有效的。当 64 位 beat 被切成两个 32 位 beat 时，原来 8 比特的 `wstrb` 必须被正确地「分配」到两个 4 比特的 `wstrb` 上——这正是本讲写通路的难点。

> 术语提示：本库模块命名里的 `slv` 端口 = **上游/宽**侧（接 master 设备），`mst` 端口 = **下游/窄**侧（接 slave 设备）。downsizer 的 slave 端口比 master 端口**更宽**。这与 u6-l1 里 xbar 的「slave/master」是同一套命名。

## 3. 本讲源码地图

| 文件 | 作用 | 层级 |
|---|---|---|
| `src/axi_dw_downsizer.sv` | 宽→窄转换内核（结构体版），是本讲主角 | Level 2 |
| `src/axi_dw_converter.sv` | 统一分发器：按两端宽度选 up/down/null，并含接口外壳 `axi_dw_converter_intf` | Level 3 |
| `test/tb_axi_dw_downsizer.sv` | 64→32 的随机测试台 | — |
| `test/tb_axi_dw_pkg.sv` | 内含 `axi_dw_downsizer_monitor`，双端口黄金模型自检器 | — |
| `src/axi_pkg.sv` | 提供 `aligned_addr`、`resp_precedence`、`modifiable` 等被复用的函数 | Level 0 |

注意：测试台**不直接**例化 `axi_dw_downsizer`，而是例化统一外壳 `axi_dw_converter_intf`，由它在编译期按宽度关系落到 downsize 分支。这一点会在 4.5 详述。

## 4. 核心概念与源码讲解

### 4.1 宽到窄转换的本质与协议边界

#### 4.1.1 概念说明

`axi_dw_downsizer` 把一个**宽数据**的 slave 端口接到一个**窄数据**的 master 端口。它的职责不是「翻译协议」，AXI 五通道结构与握手规则原封不动；它要改写的是与**数据宽度耦合**的那些字段：

- `size`：上游可能用大 `size`（如 3，一拍 8 字节），下游窄端口吃不下一拍 8 字节，必须降到下游最大可承载的 `size`（`AxiMstPortMaxSize`）。
- `len`：一拍变多拍，总拍数增加，输出 `len` 比输入大。
- `addr`：INCR 突发每拍地址推进，需要按新的 `size` 重新递增。
- `wstrb`（写）：字节车道重新分配到各窄拍。
- 响应拍数：写一个宽事务可能产生**多个** B；读一个宽事务的 R 拍数也变多，需要重新组织 `last`。

模块头注释明确划定了**支持的突发类型边界**：不支持 WRAP（返回 SLVERR）；FIXED 仅支持单拍（`len==0`），多拍 FIXED 也返回 SLVERR。

[src/axi_dw_downsizer.sv:18-21](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L18-L21) — 头注释声明 WRAP/多拍 FIXED 不支持。

#### 4.1.2 核心流程

downsizer 对每条通道的处理可以概括为：

```text
上游(slv, 宽) ──┐
               ├─► [按 burst 类型分流] ──► 支持? ──► downsize 引擎 ──► 下游(mst, 窄)
               │                          │
               │                          └─ 不支持 ──► axi_err_slv (回 SLVERR)
```

- **读方向（AR→R）**：用 `AxiMaxReads` 个**并行** downsize 引擎同时处理多个在途读事务；用 `id_queue` 把回来的 R 拍按 ID 路由到正确的引擎；窄 R 拍在引擎里「序列化」拼回宽 R 拍。
- **写方向（AW→W→B）**：单个写引擎顺序处理；W 拍做 lane steering；多个窄 B 用一个 FIFO 计数，只把最后一个 B 转发给上游。

#### 4.1.3 源码精读

模块参数揭示了「两侧数据宽度不同」这一关键事实——注意 W/R 通道类型各有**两个**版本（`mst_w_chan_t` vs `slv_w_chan_t`、`mst_r_chan_t` vs `slv_r_chan_t`），因为两端口的数据宽度不同，struct 的 data/strb 字段宽度也不同：

[src/axi_dw_downsizer.sv:22-48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L22-L48) — 参数表：`AxiSlvPortDataWidth`（宽）与 `AxiMstPortDataWidth`（窄）分离，W/R 通道类型也分 mst/slv 两套。

由此派生出一组贯穿全模块的常量（字节车道数、各端口最大 `size`、字节掩码、按字节分组的类型），它们是后续所有地址/数据运算的基础：

[src/axi_dw_downsizer.sv:62-74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L62-L74) — 派生常量：`AxiSlvPortStrbWidth`/`AxiMstPortStrbWidth`（字节车道数）、`AxiSlvPortMaxSize`/`AxiMstPortMaxSize`（各端口最大 size）、按字节分组的 `mst_data_t`/`slv_data_t`。

「按字节分组」的类型定义 `typedef logic [N-1:0][7:0] xxx_data_t;` 是本模块的惯用写法——它把一个宽字看成「N 个 8 位字节」的数组，从而可以用循环下标 `data[b]` 直接操作第 b 个字节车道，这正是 lane steering / 序列化循环能写得如此简洁的原因。

不支持的事务被一个 1:2 的 `axi_demux` 引到 `axi_err_slv`（返回 `RESP_SLVERR`），这条「错误支路」是 4.1.2 流程图里那一个分支的物理实现：

[src/axi_dw_downsizer.sv:188-234](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L188-L234) — `axi_err_slv`（SLVERR）+ 1:2 `axi_demux`，由 `mst_req_ar_err`/`mst_req_aw_err` 选择是否把事务送去错误从端。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 downsizer 的「错误边界」与默认配置。

1. 打开 `src/axi_dw_downsizer.sv`，找到头注释（L18-L21）。
2. 在读引擎里找到 `BURST_WRAP` 分支（L461-L465）与多拍 `BURST_FIXED` 分支（L455-L459），确认它们都置 `ar_throw_error = 1'b1`。
3. 追踪 `ar_throw_error` 如何变成 `mst_req_ar_err`（L359），再被 `i_axi_demux` 的 `slv_ar_select_i` 用作选择信号（L230）。

**预期**：你会看到 unsupported 事务并不进入正常 downsize 引擎，而是被 demux 选到第 1 路（`axi_err_req`），最终由 `axi_err_slv` 回一个 SLVERR。运行结果**待本地验证**（需要 vsim/verilator 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 downsizer 无法支持 WRAP 突发的拆分？

> **答**：WRAP 突发的地址会「回卷」到一个对齐边界，拆成多个窄拍后，窄端的地址递增序列无法再现这种回卷语义；而且 WRAP 的容器大小与起始地址强相关，宽→窄后窗口边界对不齐。库选择直接拒绝（SLVERR）而非做复杂且易错的展开。

**练习 2**：`AxiMstPortMaxSize` 的值由什么决定？

> **答**：`AxiMstPortMaxSize = $clog2(AxiMstPortStrbWidth) = $clog2(AxiMstPortDataWidth/8)`，即窄端口一拍能承载的最大字节数对应的 `size`（见 L67）。downsize 后所有事务的 `size` 都会被压到不超过这个值。

---

### 4.2 一拍拆多拍：转换比与输出 len 的数学

#### 4.2.1 概念说明

这是整个 downsizer 的「数学心脏」。给定一笔上游 INCR 突发（`addr, size, len`），下游窄端口需要多少拍？答案由两个量决定：

- **转换比 conv_ratio**：一个宽 beat 能装下的字节数，要几个窄 beat 才搬得完。
- **对齐修正 align_adj**：起始地址若没有对齐到窄端口边界，最开头会少几拍。

#### 4.2.2 核心流程

设上游 `size` 为 \(s\)（每拍 \(2^s\) 字节），窄端口字节车道数为 \(M = \text{AxiMstPortStrbWidth}\)，上游拍数为 \(L_{\text{in}}+1\)（`len+1`）。

转换比（一个宽 beat 对应几个窄 beat）：

\[
\text{conv\_ratio} = \left\lceil \frac{2^s}{M} \right\rceil = \frac{2^s + M - 1}{M}
\]

对齐修正（因起始地址未对齐到窄边界而丢掉的头部窄拍数）。设 `size_mask = 2^s - 1`，`MstPortByteMask = M - 1`：

\[
\text{align\_adj} = \frac{\text{addr}\ \&\ \text{size\_mask}\ \&\ \sim\text{MstPortByteMask}}{M}
\]

输出总拍数对应的 `len`：

\[
\text{burst\_len} = (L_{\text{in}}+1)\cdot\text{conv\_ratio} - \text{align\_adj} - 1
\]

然后按 `burst_len` 是否超过 AXI 单段上限（255，即 256 拍）分三档：

| 条件 | 处理 | 状态 |
|---|---|---|
| `conv_ratio == 1` | 无需 downsize（宽 beat 的 size 本就 ≤ 窄端口） | PASSTHROUGH |
| `conv_ratio != 1` 且 `burst_len ≤ 255` | 一段 INCR，`size` 压到 `AxiMstPortMaxSize` | INCR_DOWNSIZE |
| `conv_ratio != 1` 且 `burst_len > 255` | 拆成多段 ≤256 拍 INCR | SPLIT_INCR_DOWNSIZE |

**为什么要 SPLIT**：AXI 规范规定一笔突发的 `len ≤ 255`（最多 256 拍）。宽→窄后拍数膨胀，可能超过 256，必须切成多笔独立 INCR 突发分别发给下游。

#### 4.2.3 源码精读

这套公式在**读引擎**与**写引擎**里各写了一遍（几乎逐字相同）。读引擎 AR 方向的版本：

[src/axi_dw_downsizer.sv:410-433](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L410-L433) — INCR 分支：算 `size_mask`/`conv_ratio`/`align_adj`，得到 `burst_len`，再按 `conv_ratio` 与 255 阈值选 PASSTHROUGH / INCR_DOWNSIZE / SPLIT_INCR_DOWNSIZE。

其中 `conv_ratio != 1` 才进入真正 downsize；`conv_ratio == 1` 时事务「原样通过」（PASSTHROUGH），这是性能上的快路径——当上游 `size` 已经小到窄端口能一拍装下时，零开销直通。

SPLIT 分支的关键：第一段长度被设为 `255 - align_adj`（吃掉对齐欠款），后续每段最多 255，由状态机在 `R_SPLIT_INCR_DOWNSIZE` 里逐段触发新的 AR（见 4.3.3 的 L628-L634）。

#### 4.2.4 代码实践（手算型）

**目标**：用纸笔验证公式，建立对 `burst_len` 的直觉。

配置：64→32（`AxiSlvPortDataWidth=64`，`AxiMstPortDataWidth=32`），所以 \(M = 4\)，`AxiMstPortMaxSize = 2`。

1. 上游发一笔 INCR 读：`addr=0x0`，`size=3`（每拍 8 字节），`len=0`（1 拍）。
   - `conv_ratio = ceil(8/4) = 2`
   - `align_adj = (0 & 0xFF & ~0x3)/4 = 0`
   - `burst_len = (0+1)*2 - 0 - 1 = 1` → 输出 `len=1`（2 拍），`size=2`
2. 上游发：`addr=0x0`，`size=3`，`len=255`（256 拍，最大）。
   - `burst_len = 256*2 - 0 - 1 = 511 > 255` → SPLIT，首段 `len = 255`，后续再发一段。

**需要观察**：`size=3` 的宽事务在 32 位下游必然 `conv_ratio=2`；而 `size≤2` 的事务 `conv_ratio=1` 走 PASSTHROUGH，不产生额外拍数。

**预期**：第 1 问下游收到 2 拍 `size=2` 的读；第 2 问下游收到两笔独立 INCR（256 + 256 拍）。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：64→32 下，上游 `size=2`（4 字节/拍）的 INCR 是否会被 downsize？

> **答**：不会。`conv_ratio = ceil(4/4) = 1`，走 PASSTHROUGH，`size`、`len`、`addr` 都不变。只有 `size > AxiMstPortMaxSize`（此处 > 2）才真正触发 downsize。

**练习 2**：`align_adj` 在什么情况下非零？

> **答**：当起始地址在 `size` 范围内、却**没有对齐到窄端口边界**时。例如 64→32、`size=3`、`addr=0x4`：`addr & 0xFF & ~0x3 = 0x4`，`align_adj = 0x4/4 = 1`，表示第一个宽 beat 的前半段落在「上一个窄字」里，下游最开头要少发 1 拍。

---

### 4.3 读通路：并行引擎与 R 拍序列化

#### 4.3.1 概念说明

读方向有两个难点：①多个读事务可能同时在途，downsizer 用 `AxiMaxReads` 个**并行引擎**同时处理它们；②每个引擎把下游返回的**多个窄 R 拍**重新拼装成上游期望的**一个宽 R 拍**——这叫「序列化（serialization）」，是宽→窄在读方向的逆操作。

#### 4.3.2 核心流程

```text
               ┌─► 引擎0 ──┐
上游 AR ──► 仲裁分发 ──┤─► 引擎1 ──┼─► 仲裁合并 ──► 下游 AR
               └─► ...   ──┘
下游 R ──► id_queue(按 ID 路由) ──► 命中引擎 ──► 序列化拼宽 ──► 上游 R
```

- **AR 仲裁**：把 `AxiMaxReads` 个引擎的 AR 请求用 `rr_arb_tree` 合并成一路发给下游（`i_mst_ar_arb`）。
- **引擎选择**：新 AR 来时，优先派给「已经在处理同 ID」的引擎（id clash），否则派给空闲引擎（`lzc` 找第一个 idle）。
- **R 路由**：下游 R 拍带 ID，用 `id_queue` 查出它属于哪个引擎，只让该引擎消费。
- **序列化**：引擎收齐足够窄 R 拍后，拼出一个宽 R 拍发给上游；`last` 只在整笔事务的最后一拍置位。

#### 4.3.3 源码精读

读通路用一个 `for (genvar t ...)` 循环例化 `AxiMaxReads` 个引擎，每个引擎是一个五状态 FSM：

[src/axi_dw_downsizer.sv:240-246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L240-L246) — 读状态机枚举：`R_IDLE`/`R_INJECT_AW`/`R_PASSTHROUGH`/`R_INCR_DOWNSIZE`/`R_SPLIT_INCR_DOWNSIZE`。其中 `R_INJECT_AW` 专门服务 ATOP 原子写产生的读响应（见 4.3 末）。

引擎派发与 ID 冲突检测：

[src/axi_dw_downsizer.sv:274-302](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L274-L302) — 用 `lzc` 找一个空闲引擎；用逐位比较 `arb_slv_ar_id == mst_ar_id[t]` 检测 ID 冲突；冲突时复用同引擎，否则用空闲引擎。

`id_queue` 负责把下游 R 拍按 ID 还原到引擎号：

[src/axi_dw_downsizer.sv:312-336](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L312-L336) — `i_read_id_queue`：AR 握手时 push「(ID → 引擎号)」，R 拍到来时按其 ID pop 出引擎号。

序列化的核心——把窄 R 拍的字节搬到宽 R 拍的对应车道：

[src/axi_dw_downsizer.sv:579-591](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L579-L591) — 序列化循环：遍历宽字每个字节车道 `b`，当下标落在「本 beat 有效区间 + 不越出窄字」时，从窄 R 拍取字节填入 `r_data[b]`。

注意 `r_req_d.r.last` 只在 `burst_len == 0`（整笔事务末拍）置位（L591），而中间窄拍即使凑满一个宽字也只是发出一个普通 R 拍——这保证上游看到的 `last` 与原始事务边界一致。

多个窄 R 拍的 `resp` 用 `resp_precedence` 合并，遵循库约定 DECERR > SLVERR > OKAY > EXOKAY：

[src/axi_dw_downsizer.sv:595-596](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L595-L596) — 用 `axi_pkg::resp_precedence` 把本拍响应与已累计响应合并。

SPLIT 状态下，当一段 256 拍 INCR 跑完但整笔事务未完，状态机自动触发下一段 AR：

[src/axi_dw_downsizer.sv:628-634](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L628-L634) — `R_SPLIT_INCR_DOWNSIZE` 下，一段结束时按剩余 `burst_len` 决定下一段长度并重发 AR。

> **关于 ATOP**：原子写 `ATOP_R_RESP` 会产生 R 响应却没有 AR。downsizer 通过 `inject_aw_into_ar` 机制把这条 AW「注入」读通路（`i_slv_ar_arb` 是 2 选 1，第二路就是注入的 AW，见 L135-L153），并在 `R_INJECT_AW` 状态用 AW 字段构造一个等价 AR 交给引擎处理。这与 u7-l2 的 isolate、u9-l1 的 splitter 处理 ATOP 的思路一脉相承。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪一笔 64→32 读的 R 拍序列化过程。

1. 假设上游发 `addr=0x0, size=3, len=0` 的 INCR 读（1 个宽 beat = 8 字节）。
2. 由 4.2 知下游会回 2 个窄 R 拍（各 4 字节）。
3. 在 L580-L586 的循环里代入：`AxiSlvPortStrbWidth=8`、`AxiMstPortStrbWidth=4`、`slv_port_offset=0`、`mst_port_offset=0`。
   - 第 1 个窄 R 拍：`b=0..3` 满足条件 → 填 `r_data[0..3]`；`b=4..7` 因 `b + 0 - 0 = 4 ≮ 4` 不满足 → 不填。此时宽字未填满，不发 R。
   - 第 2 个窄 R 拍（下游地址推进）：`b=4..7` 现在满足 → 填 `r_data[4..7]`。宽字填满 + `burst_len==0` → 发出一个 `last=1` 的宽 R 拍。

**需要观察**：上游只看到 **1 个** R 拍（含全部 8 字节，`last=1`），而下游实际发了 **2 个** 窄 R 拍。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `AxiMaxReads` 个并行引擎，而不是一个？

> **答**：单引擎一次只能处理一笔在途读，会严重损失并发与吞吐。多个引擎让不同 ID（或不同事务）的读同时在下进行，每个引擎独立维护自己的地址递增与序列化缓冲。`AxiMaxReads` 即最大在途读数。

**练习 2**：`id_queue` 为什么必须按 ID 路由 R 拍？

> **答**：下游 R 拍可能**交错**到达（不同 ID 的事务响应可能乱序），而每个引擎只持有自己那笔事务的状态（当前地址、已累计字节等）。必须用 ID 把 R 拍送回正确的引擎，否则会用错引擎的地址去解码字节车道，导致数据错位。

---

### 4.4 写通路：lane steering、strb 重算与多 B 合并

#### 4.4.1 概念说明

写方向是本讲**实践任务**的重点。它做三件事：

1. **Lane steering（车道导引）**：把上游宽 W 拍的字节，按地址偏移「导」到下游窄 W 拍的正确字节车道上；一个宽 W 拍可能对应多个窄 W 拍。
2. **strb 重算**：上游 8 比特 `wstrb` 被切成多段，分别装进各窄 W 拍的 4 比特 `wstrb`。
3. **多 B 合并**：一个宽写事务在下游可能触发多笔窄突发，因而产生**多个 B**；上游只应收到**一个** B。用一个 FIFO 计数，只转发最后一个 B。

#### 4.4.2 核心流程

```text
上游 AW ──► [算 conv_ratio/burst_len, 决定状态] ──► 下游 AW（可能多笔）
上游 W  ──► [lane steering: 字节 b → 车道 b+mst_off-slv_off] ──► 下游 W（拍数膨胀）
下游 B  ──► [FIFO 计数, 只转发末个 B, resp_precedence 合并] ──► 上游 B（1 个）
```

写状态机只有四个状态（没有 INJECT_AW，因为 AW 本就在写通路里）：

| 状态 | 含义 |
|---|---|
| `W_IDLE` | 等待新 AW |
| `W_PASSTHROUGH` | `conv_ratio==1`，原样转发 |
| `W_INCR_DOWNSIZE` | 单段 INCR downsize |
| `W_SPLIT_INCR_DOWNSIZE` | 多段 INCR downsize |

#### 4.4.3 源码精读

写状态机定义：

[src/axi_dw_downsizer.sv:665-670](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L665-L670) — 写状态机枚举 `W_IDLE`/`W_PASSTHROUGH`/`W_INCR_DOWNSIZE`/`W_SPLIT_INCR_DOWNSIZE`。

**Lane steering** 的核心循环（写方向把宽字字节搬到窄字车道）：

[src/axi_dw_downsizer.sv:767-786](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L767-L786) — lane steering：算 `mst_port_offset`/`slv_port_offset`，遍历宽字字节 `b`，当目标车道 `b + mst_port_offset - slv_port_offset` 落在窄字范围内时，搬运数据与对应 strb 比特。

关键直觉：`mst_port_offset` 是起始地址在**窄字**内的字节偏移，`slv_port_offset` 是在**宽字**内的偏移。差值 `mst_port_offset - slv_port_offset` 就是「宽字车道 b 映射到窄字的哪个车道」。当这个目标车道 ≥ `AxiMstPortStrbWidth`（窄字装不下）时，剩余字节留到下一个窄 W 拍——这就是「一宽拍拆多窄拍」的物理来源。

`mst_req.w.last` 在 `aw.len == 0` 时置位（L774），即下游当前窄突发的末拍。

**多 B 合并**：`forward_b_beats_queue` 这个 1 比特 FIFO 记录「本事务还会来几个 B」。SPLIT 每多发一段就 push 一个 `0`（表示这个 B 要丢弃），事务真正结束时 push 一个 `1`（表示这个 B 才转发给上游）：

[src/axi_dw_downsizer.sv:739-754](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L739-L754) — B 通路：`forward_b_beat_o==1` 才把 B 转发给上游并 pop；否则吞掉下游 B（`b_ready=1`、不转发）。同时用 `resp_precedence` 合并各段响应。

FIFO 本体：

[src/axi_dw_downsizer.sv:682-698](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L682-L698) — `i_forward_b_beats_queue`：深度 `AxiMaxReads`、FALL_THROUGH 的 1 比特 FIFO，计数每事务待处理的 B。

AW 方向的 burst 公式与 4.2 完全一致（同一套 `conv_ratio`/`align_adj`/`burst_len`），只是作用在 `aw` 上：

[src/axi_dw_downsizer.sv:871-894](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L871-L894) — 写 AW 的 INCR 分支：与读侧 AR 公式逐字相同的 conv_ratio/align_adj/burst_len 计算与三档分流。

#### 4.4.4 代码实践（本讲主实践）

**目标**：把一个 64 位写事务转成 32 位下游事务，验证每拍数据与 strobe 的对应关系正确。

**配置**：64→32，故 `AxiSlvPortStrbWidth=8`、`AxiMstPortStrbWidth=4`、`AxiMstPortMaxSize=2`。

**操作步骤（手算 + 源码对照）**：

1. 构造一笔上游 INCR 写：`addr = 0x0`，`size = 3`（8 字节/拍），`len = 0`（1 个宽 W 拍）。宽 W 拍数据假设为 `data = 0x00_01_02_03_04_05_06_07`（字节 0 在低字节），`strb = 8'b1111_1111`（全有效）。
2. 由 4.2：`conv_ratio = 2`，`burst_len = 1`，下游收到一笔 `size=2`、`len=1`（2 拍）的 INCR 写。
3. 代入 lane steering 循环（L778-L786），`slv_port_offset = 0`、`mst_port_offset = 0`：
   - **窄 W 拍 0**：`b=0..3` → 目标车道 `0..3`，搬 `data[0..3] = 03,02,01,00`，`strb = 4'b1111`。`b=4..7` 目标车道 `4..7 ≥ 4` → 不搬，留到下一拍。
   - **窄 W 拍 1**：地址推进后 `mst_port_offset` 回到 0（新窄字），`b=4..7` → 目标车道 `0..3`，搬 `data[4..7] = 07,06,05,04`，`strb = 4'b1111`，`last=1`。
4. 把你的手算结果与 `tb_axi_dw_pkg.sv` 里 `axi_dw_downsizer_monitor` 的 `mon_slv_port_w`（L1104-L1180）对比——monitor 用 `axi_pkg::beat_lower_byte`/`beat_upper_byte` 独立算出期望的窄 W 拍，正是用来检查 downsizer 算得对不对的黄金模型。

**需要观察**：
- 下游收到 **2 个** 窄 W 拍，各自的 4 字节与 4 比特 strb 与手算一致。
- 下游因为这是一笔（虽被拆成 2 拍但仍是）单段 INCR 突发，只会回 **1 个** B，该 B 被原样转发给上游（`forward_b_beat_o==1`）。

**预期结果**：上游 1 个宽 W 拍 → 下游 2 个窄 W 拍，字节顺序与 strb 完全对应；上游收到 1 个 B。**实际仿真待本地验证**（可运行 `make sim-tb_axi_dw_downsizer.log` 或 `vsim -voptargs=+acc work.tb_axi_dw_downsizer`，见 L211）。

#### 4.4.5 小练习与答案

**练习 1**：若上游 `strb = 8'b0000_1111`（只有低 4 字节有效），下游两个窄 W 拍的 strb 分别是多少？

> **答**：拍 0 搬 `b=0..3` → `strb = 4'b1111`；拍 1 搬 `b=4..7`，但这 4 字节在上游 strb 里是 `0000`（高位）→ `strb = 4'b0000`。注意 lane steering 是按字节搬 strb 的（L783），所以无效字节会正确地体现为下游对应窄拍的 strb=0。

**练习 2**：为什么 SPLIT 写要 push 一个 `0` 到 `forward_b_beats_queue`？

> **答**：SPLIT 把一个宽写拆成**多笔**下游窄突发，每笔各回一个 B。除了最后一笔的 B 要转发给上游，中间各笔的 B 必须被吞掉（否则上游会收到多个 B，违反「一个写事务一个 B」）。push `0` 就是标记「下一个到来的 B 要丢弃」，只有事务末段 push 的 `1` 才允许转发（L826-L836）。

---

### 4.5 测试台 tb_axi_dw_downsizer 与自检 monitor

#### 4.5.1 概念说明

`tb_axi_dw_downsizer` 是一个**定向随机**测试台（directed random，见 u3-l3/u16-l1）：用随机主端发大量合法事务，用一个独立的双端口 monitor 作为「黄金模型」自检 downsizer 的输出是否正确。它不直接例化 `axi_dw_downsizer`，而是例化统一外壳 `axi_dw_converter_intf`——因为后者会按宽度自动落到 downsize 分支，这正是用户实际使用的方式。

#### 4.5.2 核心流程

```text
axi_rand_master(64位) ──► axi_dw_converter_intf(64→32) ──► axi_rand_slave(32位)
        │                                                          │
        └──────── axi_dw_downsizer_monitor (双端口监听) ───────────┘
                        │
                        └─► Tests Failed: 0 即通过
```

- 主端 64 位、从端 32 位，宽度差驱动 `axi_dw_converter` 选择 downsize 分支。
- monitor 同时监听上下游两端，用 `beat_lower_byte`/`beat_upper_byte` 等函数独立计算「期望的下游事务」，再与实际比对。
- 每笔事务检查若干字段（id/addr/len/burst/size/cache/data/strb/last），统计 Expected/Conducted/Failed。

#### 4.5.3 源码精读

DUT 是统一外壳，宽度参数 64/32 决定了内部走 downsize：

[test/tb_axi_dw_downsizer.sv:145-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L145-L157) — 例化 `axi_dw_converter_intf`，`AXI_SLV_PORT_DATA_WIDTH=64`、`AXI_MST_PORT_DATA_WIDTH=32`，故 `axi_dw_converter` 选 downsize 分支。

`axi_dw_converter` 的三路编译期分发（等宽直通 / 升宽 / 降宽），downsize 分支即本讲主角：

[src/axi_dw_converter.sv:46-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L46-L109) — 三路 `if` 分发：等宽 `assign` 直通、`MstPortDataWidth > SlvPortDataWidth` 用 upsizer、`<` 用 downsizer（L81-L109）。

激励与停止：主端跑 200 读 + 200 写，开启 ATOP、关闭 FIXED 突发（因 downsizer 对多拍 FIXED 返回 SLVERR，会干扰随机激励）：

[test/tb_axi_dw_downsizer.sv:99-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L99-L110) — `axi_rand_master` 配置：`MAX_READ/WRITE_TXNS=8`、`AXI_BURST_FIXED(1'b0)`、`AXI_ATOPS(1'b1)`。

[test/tb_axi_dw_downsizer.sv:174-178](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L174-L178) — `master_drv.run(200, 200)` 发 200 读 + 200 写，`slave_drv.run()` 做随机从端，`join_any` 任一结束即收尾。

monitor 的结果是唯一的通过判据——`print_result` 打印三类计数，`tests_failed > 0` 时 `$error`：

[test/tb_axi_dw_downsizer.sv:189-209](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L189-L209) — monitor 进程：`eos` 置位后调 `print_result()` 并 `$stop`。

黄金模型 `axi_dw_downsizer_monitor` 在 `mon_slv_port_aw` 里**独立重算**下游期望的 AW（用与 4.2 相同的 `conv_ratio`/`aligned_adjustment` 公式，但是用软件写一遍），用来交叉验证 RTL：

[test/tb_axi_dw_pkg.sv:994-1009](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_pkg.sv#L994-L1009) — monitor 的 INCR downsize 期望计算：`num_beats = (len+1)*conv_ratio - aligned_adjustment`，与 RTL 的 `burst_len` 公式同构，单段（≤256）或多段 SPLIT 分别压入期望队列。

> 这种「RTL 与软件黄金模型各自独立算一遍再比对」的验证哲学，正是 u3-l2 的 scoreboard 思路与 u16-l1 定向随机方法学的体现。

#### 4.5.4 代码实践（运行型）

**目标**：跑通测试台并读懂其结果判据。

1. 按 u1-l4 的方法，在仓库根目录运行：
   ```bash
   make sim-tb_axi_dw_downsizer.log
   ```
   或直接（见 [test/tb_axi_dw_downsizer.sv:211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L211)）：
   ```bash
   vsim -voptargs=+acc work.tb_axi_dw_downsizer
   ```
2. 仿真结束后查看 monitor 打印的：
   ```
   Tests Expected:  <N>
   Tests Conducted: <N>
   Tests Failed:    <N>
   ```
3. 判据：`Tests Failed: 0` 即通过；同时整个仿真日志里不应出现 `Error:` / `Fatal:`（与 u1-l4 的日志判据一致）。

**需要观察**：因为主端开 ATOP、随机的 size/addr，你会看到大量不同 `conv_ratio`、不同对齐、SPLIT 与非 SPLIT 的事务被覆盖；monitor 的 `Tests Conducted` 会是一个很大的数。

**预期结果**：`Tests Failed: 0`。**待本地验证**（本环境无 vsim/verilator，未实际运行）。

#### 4.5.5 小练习与答案

**练习 1**：为什么主端配置 `AXI_BURST_FIXED(1'b0)`？

> **答**：downsizer 对多拍 FIXED 返回 SLVERR（见 4.1）。若随机主端发出多拍 FIXED，会产生大量「合法激励 → SLVERR」的情况，使激励空间被错误响应占据、降低有效覆盖。关闭 FIXED 让随机事务集中在 downsizer 真正支持的 INCR 上。

**练习 2**：monitor 为什么需要**同时**监听上下游两端，而不是只看一端？

> **答**：宽度转换的正确性是「**两端一致**」：给定上游某事务，下游应看到某组确定的事务，反之亦然。只看一端无法判断转换对错。monitor 在上游记录「实际发生」的事务，独立算出下游「应该发生」的事务，再到下游端逐字段比对，才能确认每一个 beat 都没丢、没多、没错位。

---

## 5. 综合实践

把本讲知识串起来，完成一个「**跟踪一笔 64→32 写事务的完整生命周期**」的任务。

**场景**：上游发一笔 INCR 写，`addr=0x8`，`size=3`，`len=1`（2 个宽 W 拍），`strb` 两拍均为 `8'b1111_1111`。

**要求**：

1. **算下游 AW**：用 4.2 的公式计算 `conv_ratio`、`align_adj`、`burst_len`，写出下游 AW 的 `size`/`len`/`addr`，并判断是否进入 SPLIT。
   - 提示：`addr=0x8` 在 `size=3` 下，`addr & 0xFF & ~0x3 = 0x8`，`align_adj = 0x8/4 = 2`。
2. **算下游 W 拍数与每拍的 strb**：用 4.4 的 lane steering，列出下游每个窄 W 拍的字节来源与 strb。
3. **算下游 B 数与上游 B 数**：用 4.4 的多 B 合并逻辑，判断下游回几个 B、上游最终收到几个 B。
4. **对照源码核验**：把你的结论与 `axi_dw_downsizer.sv` 的写通路（L762-L934）逐段对照，确认每个推断都有源码支撑。
5. （可选）运行 `tb_axi_dw_downsizer`，确认大批随机事务下 `Tests Failed: 0`。

**参考答案要点**：
1. `conv_ratio=2`；`align_adj=2`；`burst_len = (1+1)*2 - 2 - 1 = 1`，下游 `size=2`、`len=1`、不 SPLIT。首段从 `addr=0x8` 出发。
2. 上游 2 个宽拍 = 16 字节；扣除头部 `align_adj=2` 拍窄字对齐欠款后，下游共发 `burst_len+1 = 2` 个窄 W 拍（注：`align_adj` 已在 `burst_len` 里扣除，体现为起始窄字的部分填充与整体拍数）。每个窄 W 拍的 strb 由 lane steering 按字节映射得到，有效字节对应位为 1。
3. 单段 INCR → 下游回 1 个 B → `forward_b_beats_queue` 末值 `1` → 上游收到 1 个 B。

> 综合实践的精确字节级结果建议在本地用波形或 monitor 日志核验；公式与状态判断部分可由本讲源码直接推出。

## 6. 本讲小结

- **宽→窄的本质**是「一个宽 beat 拆成多个窄 beat」，要重算 `size`/`len`/`addr`/`strobe` 与响应拍数，而非简单连线。
- **核心数学**：`conv_ratio = ceil(2^size / MstPortStrbWidth)`；`burst_len = (len+1)*conv_ratio - align_adj - 1`；`conv_ratio==1` 走 PASSTHROUGH，否则按是否超过 256 拍决定 INCR_DOWNSIZE 或 SPLIT。
- **读通路**用 `AxiMaxReads` 个并行引擎 + `id_queue` 路由 + R 拍序列化（拼窄为宽）；**写通路**用 lane steering（拆宽为窄）+ strb 重算 + FIFO 合并多 B 为一 B。
- 不支持 **WRAP** 与**多拍 FIXED**，统一送 `axi_err_slv` 回 SLVERR；`resp_precedence` 负责跨拍/跨段响应合并。
- 模块是**结构体内核**，用户通常通过统一外壳 `axi_dw_converter_intf`（→ `axi_dw_converter` 按宽度三路分发）使用它；测试台用双端口 monitor 作黄金模型自检。

## 7. 下一步学习建议

- **u11-l2（axi_dw_upsizer：窄到宽）**：对称的窄→宽转换，序列化与 lane steering 方向相反，建议对照阅读以巩固「字节车道映射」的直觉。
- **u11-l3（axi_dw_converter：自动选择）**：深入统一分发器如何按两端宽度编译期选 up/down/null，理解它在异构网络里作为「数据宽度胶水」的角色。
- **延伸源码**：可顺便看 `src/axi_lite_dw_converter.sv`（AXI-Lite 版本的轻量宽度转换，无突发/ID 复杂度），对比完整版去掉哪些机制。
- **方法学**：回看 u3-l2 的 scoreboard 与 u16-l1 的定向随机验证，体会 `tb_axi_dw_pkg` 这种「RTL 与软件黄金模型独立双算」的验证范式在本讲的具体落地。
