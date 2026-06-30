# 简单端点：zero_mem / lfsr / err_slv

## 1. 本讲目标

学完本讲，读者应该能够：

- 识别本库三个最常用的「常备端点积木」`axi_err_slv`、`axi_zero_mem`、`axi_lfsr`，并说清它们各自的 AXI 行为：错误从端恒返回 DECERR/SLVERR、`/dev/zero` 读 0 写吸收、LFSR 读返回伪随机写压缩成校验。
- 读懂这三个模块各自的实现套路：`axi_err_slv` 用三组 `fifo_v3` 自行完成五通道握手与突发响应；`axi_zero_mem` 复用 `axi_to_detailed_mem`（u14-l1），把存储侧的写数据悬空、读数据硬接 `'0`；`axi_lfsr` 则复用 `axi_to_axi_lite`（u13-l1）降到 Lite 再驱动一个 LFSR 移位寄存器。
- 理解 `axi_err_slv` 在 `axi_xbar` 中的双重角色：既作「译码错误从端」兜底未映射地址，又作「被剪裁链路」的占位终端，对应 u6-l2 已建立的认知。
- 能够在一个最小 `axi_xbar` 系统里把这三个端点接线起来：未映射区域由 xbar 内置的 `axi_err_slv` 兜底，某个 master 端口挂 `axi_lfsr`，验证错误返回码与伪随机读数据。

## 2. 前置知识

在进入本讲前，读者应已了解（对应前置讲义）：

- **AXI4 五通道、握手与突发**（u1-l3）：AW/W/B/AR/R，valid/ready 同高才算握手，读事务回 `len+1` 拍 R，写事务回 1 个 B。
- **响应码**（u1-l3、u2-l1）：`RESP_OKAY/EXOKAY/SLVERR/DECERR`，本讲的 `axi_err_slv` 只允许产生 `RESP_DECERR` 或 `RESP_SLVERR`。
- **typedef / assign 宏体系与 req/resp 结构体**（u2-l4）：`axi_req_t` / `axi_resp_t`。
- **axi_xbar 的地址映射、译码错误与默认端口**（u6-l2）：未命中规则的事务被改送到「译码错误从端」，返回 DECERR；这正是本讲 `axi_err_slv` 的核心应用场景。
- **axi_to_detailed_mem 与存储侧 req/gnt/rvalid 协议**（u14-l1）：`axi_zero_mem` 直接复用它。
- **axi_to_axi_lite 桥**（u13-l1）：`axi_lfsr` 先用它把 AXI4 降到 Lite。

> 一个贯穿全讲的直觉：这三个模块都是「**只会一种固定行为**」的 AXI **Slave**——它们不需要真实存储、不译码地址、不保序任何业务数据，因此实现极简，但又是搭系统与测试台时几乎绕不开的「占位」「兜底」「激励源」三件套。可以把它们类比成 Unix 下的 `/dev/zero`、`/dev/null`、`/dev/urandom` 与错误码返回器。

## 3. 本讲源码地图

| 文件 | 编译层级 | 作用 |
|------|----------|------|
| [src/axi_err_slv.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv) | Level 3 | **错误从端**。无条件吸收事务并恒定返回 DECERR（或 SLVERR）；可选前置 `axi_atop_filter` 兼容原子操作。 |
| [src/axi_zero_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv) | Level 3 | **/dev/zero + /dev/null**。读恒返回 0、写数据被吸收（悬空），是 `axi_to_detailed_mem` 的薄包装。 |
| [src/axi_lfsr.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lfsr.sv) | Level 3 | **LFSR 从端**（AXI4 版）。先 `axi_to_axi_lite` 降到 Lite，再驱动 `axi_lite_lfsr`；读返回伪随机、写压缩成校验。 |
| [src/axi_lite_lfsr.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv) | Level 2 | **LFSR 真正实现**。包含 `axi_lite_lfsr` 主体与底层 `axi_opt_lfsr` 移位寄存器（XNOR 反馈 + 串行播种）。 |
| [src/axi_atop_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv) | Level 2 | 被 `axi_err_slv` 在 `ATOPs=1` 时例化，过滤上游原子操作（详见 u15-l1）。 |
| [src/axi_xbar_unmuxed.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv) | Level 4 | xbar 内部两处例化 `axi_err_slv`，是本讲综合实践的接入点。 |

> 关于编译层级：`axi_err_slv` 依赖 `axi_atop_filter`（Level 2）与外部 `common_cells` 的 `fifo_v3`/`counter`，故为 Level 3；`axi_zero_mem` 依赖 `axi_to_detailed_mem`（Level 2）；`axi_lfsr` 依赖 `axi_lite_lfsr`（Level 2）与 `axi_to_axi_lite`（同 Level 3，模块间引用在 SystemVerilog 中不要求严格顺序）。三者同处 Level 3，都是「在底层零件之上叠一层固定行为」的组合体。

## 4. 核心概念与源码讲解

### 4.1 axi_err_slv：恒定返回错误的 AXI 从端

#### 4.1.1 概念说明

一个真实片上系统里，几乎不可能保证每个 master 发出的每个地址都被某个真实 slave 覆盖——总会有「未映射的空洞」、总会有被电源域关闭的从端、总会需要模拟一个「永远报错」的故障从端来做容错测试。如果任由这些事务挂死（没有 slave 响应），整个 AXI 网络会死锁。`axi_err_slv` 就是这个「安全网」：**它对任何发来的事务都完整地完成 AXI 协议握手，但响应码恒为错误**，从而让 master 收到一个明确可处理的错误，而不是无限等待。

它在协议层必须做到两件事，缺一不可：

1. **完整吸收**：无论突发多长、读写何种类，都要按 AXI 规矩把 AW/W/AR 全部握手完，并回**正确数量**的 B/R（写回 1 个 B、读回 `len+1` 拍 R）——否则 master 会一直等下去。
2. **错误标注**：B 与每一拍 R 的 `resp` 字段填成配置的 `Resp`（默认 `RESP_DECERR`），读数据填成一个可识别的魔数 `RespData`。

README 对它的描述只有一句话（[README.md:37](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L37)），但它的内部实现其实要自己摆平整套五通道时序。

#### 4.1.2 核心流程

```text
                ┌──────────────── axi_err_slv ────────────────┐
  (可选) ATOPs=1 │   ┌──────────────┐                          │
  slv_req ──────►│──►│ axi_atop_    │── err_req ──┐            │
                │   │ filter       │◄─ err_resp   │            │
                │   └──────────────┘              ▼            │
                │                          ┌── 写通路 ──┐ ┌── 读通路 ──┐
                │   ATOPs=0 时直连          │ w_fifo(id)│ │ r_fifo(id,len)│
                │   (纯 assign)            │   ↓吃 W拍 │ │     ↓        │
                │                          │ b_fifo(id)│ │ down-counter │
                │                          │   ↓       │ │ (从 len 减)  │
                │                          │ B:id,Resp │ │ R:id,RespData│
                │                          │           │ │   × (len+1)拍│
                │                          └───────────┘ └─────────────┘
  slv_resp ◄────│─────────────────────────────────────────────────────│
                └─────────────────────────────────────────────────────┘
```

写通路用两个 FIFO 串联：`w_fifo` 记住每笔在途写事务的 `id`（深度 = `MaxTrans`），逐拍「吃掉」W 数据直到 `w.last`，再把 `id` 转交给 `b_fifo`，由 `b_fifo` 排队回 B 响应。读通路用 `r_fifo` 记 `(id, len)`，取出后用一个**递减计数器**从 `len` 数到 0，期间每拍回一个 R，`r_current_beat==0` 的那一拍拉 `r.last`。

#### 4.1.3 源码精读

模块的参数与端口见 [src/axi_err_slv.sv:19-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L19-L35)。三个关键参数：

```systemverilog
parameter axi_pkg::resp_t       Resp     = axi_pkg::RESP_DECERR; // 错误码
parameter logic [RespWidth-1:0] RespData = 64'hCA11AB1EBADCAB1E; // 读返回魔数
parameter bit                   ATOPs    = 1'b1;  // 是否前置 atop_filter
parameter int unsigned          MaxTrans = 1;     // 在途上限（也是 FIFO 深度）
```

（[src/axi_err_slv.sv:23-27](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L23-L27)）

> 关于魔数：默认 `RespData` 是 64 位 `CA11AB1EBADCAB1E`，会按目标数据宽度**零扩展或截断**。当 xbar 把它用于 32 位数据宽度时，截断后的低 32 位正是文档里那句著名的读返回值 `32'hBADCAB1E`（[doc/axi_xbar.md:33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L33)，u6-l2 已引用）——两者完全一致，不是笔误。

第一道闸门是可选的 ATOP 过滤（[src/axi_err_slv.sv:45-62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L45-L62)）：`ATOPs=1` 时例化 `axi_atop_filter` 把原子操作改写成普通写（避免本模块还要处理 ATOP 的额外 R 响应）；`ATOPs=0` 时直接 `assign` 连通，并用一条 `assume property` 断言上游绝不发 ATOP（[L253-255](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L253-L255)）。

写通路的核心是「吃 W 拍 + 排 B」两段组合。AW 通道在 FIFO 不满时即接收并把 `aw.id` 入队（[L87-88](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L87-L88)）；W 通道逐拍接收，直到 `w.last` 这拍同时弹出 `w_fifo`、压入 `b_fifo`（[L108-121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L108-L121)）：

```systemverilog
if (!w_fifo_empty && !b_fifo_full) begin
  err_resp.w_ready = 1'b1;                 // 吃掉这一拍 W
  if (err_req.w_valid && err_req.w.last) begin
    w_fifo_pop  = 1'b1;                    // 这笔写结束，弹出其 id
    b_fifo_push = 1'b1;                    // 并入队等待回 B
  end
end
```

B 响应从 `b_fifo` 队头取 id，`resp` 填 `Resp`（[L141-152](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L141-L152)）。注意 `b_fifo` 深度写死为 2（[L125](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L125)），注释说「放两个是为了在 B 还没被取走时也能继续吃 W」。

读通路用一个 `r_busy_q` 状态位 + 递减计数器产生 `len+1` 拍 R。AR 在 `r_fifo` 不满时接收并把 `(id, len)` 入队（[L158-163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L158-L163)）；空闲时从队头取出一笔、装载计数器并进入 busy（[L213-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L213-L220)）；busy 期间每拍回 R，计数器减到 0 的那一拍拉 `r.last` 并退出 busy（[L200-212](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L200-L212)）：

```systemverilog
err_resp.r.data  = RespData;        // 每拍读数据都是魔数
err_resp.r.resp  = Resp;            // 每拍 resp 都是错误码
err_resp.r.last  = (r_current_beat == '0);
```

计数器实例见 [src/axi_err_slv.sv:231-243](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L231-L243)，`down_i=1'b1`、`d_i=r_fifo_data.len`，即从 `len` 往下数。最后一条断言把 `Resp` 限制在 `DECERR` 与 `SLVERR` 二选一（[L247-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L247-L249)）——一个「错误从端」返回 OKAY 是没有意义的。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用源码回答「`MaxTrans` 这个参数同时影响了哪些资源？为什么 xbar 把译码错误从端的 `MaxTrans` 设成 4、把被剪裁链路的设成 1？」

**步骤**：

1. 在 [src/axi_err_slv.sv:90-106](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L90-L106) 与 [L165-181](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L165-L181) 找到两处 `.DEPTH(MaxTrans)`，确认 `w_fifo` 与 `r_fifo` 的深度都由它决定。
2. 打开 [src/axi_xbar_unmuxed.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195-L211)（译码错误从端，`MaxTrans(4)`）与 [L238-251](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L238-L251)（被剪裁链路占位，`MaxTrans(1)`）。
3. 读 [L201-203](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L201-L203) 的注释。

**需要观察的现象**：`MaxTrans` 越大，`w_fifo`/`r_fifo` 越深、能同时吞下越多在途事务；但错误从端「吞完即止」，事务在这里终止、不向下游传递。

**预期结果**：译码错误从端会收到真实的、可能并发的未命中事务，故给 `MaxTrans=4` 留点并发余量；而被剪裁链路（`Connectivity[i][j]=0`）按构造**永不该收到事务**，设 1 纯属占位、省面积——这正是源码注释「Transactions terminate at this slave, so minimize resource consumption」的含义。本结论「待本地验证」：可对照综合资源报告观察两者 FIFO 深度差异。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Resp` 设成 `axi_pkg::RESP_OKAY`，综合会失败吗？行为会怎样？

**参考答案**：仿真阶段会被 [src/axi_err_slv.sv:247-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L247-L249) 的 `assert ... else $fatal` 拦下（编译期 `initial` 块），直接 `$fatal` 报错「This module may only generate RESP_DECERR or RESP_SLVERR responses!」。这是设计者的有意约束：一个名为 err_slv 的模块返回 OKAY 会误导使用者，所以用断言把这种误用挡死。

**练习 2**：一次 `len=7`（8 拍）的读事务到达 `axi_err_slv`，它会产生几拍 R？`r.last` 在第几拍拉高？

**参考答案**：产生 `len+1 = 8` 拍 R，每拍 `data=RespData`、`resp=Resp`。计数器从 `len=7` 递减到 0，`r_current_beat==0` 的那一拍（即第 8 拍）拉 `r.last` 并退出 `r_busy_q`（见 [src/axi_err_slv.sv:200-212](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L200-L212)），随后才能处理 `r_fifo` 里的下一笔读。

---

### 4.2 axi_zero_mem：读恒为 0、写被吸收的 /dev/zero

#### 4.2.1 概念说明

很多场景需要一个「永远存在、永远成功、但内容是 0」的从端：未实现的存储区占位、DMA 搬运的目的地（只写不读，写进去的数据即丢弃）、让 master 能把一段地址空间当成「黑洞」或「零填充源」而不会卡死。`axi_zero_mem` 就是这个角色，README 把它精炼成一行（[README.md:75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L75)）：

> AXI-attached /dev/zero. All reads will be zero, writes are absorbed.

它的精妙之处在于**几乎不写任何逻辑**：直接复用 u14-l1 讲过的 `axi_to_detailed_mem`（把 AXI 突发拆成存储流的通用适配器），然后把存储侧的「写数据输出」全部悬空（`/* NC */`）、把「读数据输入」硬接成 `'0`。于是同一套突发拆分、保序、ID 管理逻辑被原样借用，模块本身只负责「假装自己是一块全 0 且不可改写的存储」。

#### 4.2.2 核心流程

```text
              ┌──────── axi_zero_mem ────────┐
 axi_req_i ──►│                              │──► axi_resp_o  (读=data'0, 写=吸收)
              │   ┌────────────────────────┐ │
              │   │ i_axi_to_detailed_mem  │ │
              │   │   (u14-l1, NumBanks=1) │ │
              │   │                        │ │
              │   │  mem_req_o ──► gnt=req │ │   ← 授予信号直接回环：永远 gnt
              │   │  mem_we_o   ──► NC     │ │   ← 写数据/使能/地址 全悬空 → 写被丢弃
              │   │  mem_wdata_o──► NC     │ │
              │   │  mem_rdata_i ◄── '0    │ │   ← 读数据硬接 0
              │   │  mem_rvalid_i◄── q(gnt)│ │   ← 延迟一拍的授予当响应有效
              │   │  mem_err_i  ◄── 0      │ │
              │   └────────────────────────┘ │
              └──────────────────────────────┘
```

两个关键赋值决定了它的全部行为：授予信号 `zero_mem_gnt = zero_mem_req`（请求即授予，[src/axi_zero_mem.sv:99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L99)）让存储侧「永远接受」；而 `mem_rdata_i = '0`（[L94](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L94)）让所有读返回 0。

> 头部文档（[src/axi_zero_mem.sv:20-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L20-L23)）还点出一条带宽性质：当 AXI 两侧的读、写通道同时活跃时，由于内部只有一条存储流路径，两者利用率各为 50%。这是 `axi_to_detailed_mem` 单口存储模型的固有特性。

#### 4.2.3 源码精读

模块只例化了一个 `axi_to_detailed_mem`（[src/axi_zero_mem.sv:63-97](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L63-L97)），并固定 `NumBanks=1`。注意它的存储侧端口连接方式——所有「向存储写」的输出都被悬空：

```systemverilog
.mem_addr_o   ( /* NC */ ),   // 地址不算了
.mem_wdata_o  ( /* NC */ ),   // 写数据丢弃
.mem_strb_o   ( /* NC */ ),
.mem_we_o     ( /* NC */ ),   // 写使能丢弃 → 写操作到此为止，不落任何存储
...
.mem_rdata_i  ( '0        ),  // 读数据恒为 0
.mem_err_i    ( 1'b0      ),
.mem_exokay_i ( 1'b0      )
```

（[src/axi_zero_mem.sv:81-96](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L81-L96)）

授予与响应有效信号的三行赋值是全模块唯一的「逻辑」（[src/axi_zero_mem.sv:99-102](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L99-L102)）：

```systemverilog
assign zero_mem_gnt          = zero_mem_req;               // 有请求就授予
assign zero_mem_valid_req_d  = zero_mem_gnt & zero_mem_req;// 授予当响应
`FF(zero_mem_valid_req_q, zero_mem_valid_req_d, '0, clk_i, rst_ni) // 打一拍
```

`zero_mem_valid_req_q` 把「本拍授予」寄存一拍后回喂给 `mem_rvalid_i`，给 `axi_to_detailed_mem` 一个一拍的响应节拍——这就是 `BufDepth` 参数（[L40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L40)）描述的「应等于存储响应延迟」的具体来源。参数 `AddrWidth` 决定可寻址范围（[L29-32](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L29-L32)），但它只影响 `mem_addr_o` 的位宽——由于地址被悬空，这个范围其实只是「看起来有多大」，并不占用任何真实存储资源。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用源码印证「`axi_zero_mem` 自身不维护任何存储阵列，写数据真的进了黑洞」。

**步骤**：

1. 通读 [src/axi_zero_mem.sv:60-104](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_zero_mem.sv#L60-L104) 全文。
2. 数一数模块体内有没有声明任何 `logic [...] mem [...]` 之类的存储数组？有没有把 `mem_wdata_o` 接到任何寄存器？
3. 对照 u14-l1 的 `axi_to_detailed_mem` 存储侧端口表（[src/axi_to_detailed_mem.sv:64-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L64-L99)），确认 `mem_wdata_o`/`mem_we_o`/`mem_strb_o` 这些「写方向」输出在本模块里全部是 `/* NC */`。

**需要观察的现象**：模块里**没有任何存储器声明**，写方向的存储侧输出全部悬空，唯一被「使用」的存储侧输入是 `mem_rdata_i`（接 `'0`）与 `mem_rvalid_i`（接寄存后的授予）。

**预期结果**：确认写数据被彻底丢弃（这就是「write data goes into nothingness」的物理实现），而读数据因为 `mem_rdata_i='0` 恒为 0。综合后该模块几乎不占存储资源，只有少量控制寄存器与 `axi_to_detailed_mem` 的内部缓冲（深度由 `BufDepth` 决定）。

#### 4.2.5 小练习与答案

**练习 1**：既然 `mem_we_o` 被悬空、写数据被丢弃，那为什么写事务还能正确返回一个 B 响应、让 master 不至于挂死？

**参考答案**：因为 `axi_to_detailed_mem`（u14-l1）本身要求「**无论读还是写，存储侧都必须回一个 `mem_rvalid_i` 作为完成信号**」（见 [src/axi_to_detailed_mem.sv:91-93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L91-L93) 的注释）。`axi_zero_mem` 用 `zero_mem_gnt = zero_mem_req` 永远授予，再把授予信号打一拍回喂 `mem_rvalid_i`，于是写事务也能拿到完成信号、从而在 AXI 侧回 B。写数据本身虽然丢弃，但「完成」这件事被忠实完成了。

**练习 2**：把 `mem_rdata_i` 从 `'0` 改成一个固定魔数 `64'hDEAD_BEEF...`，行为会如何变化？它和 `axi_err_slv` 的 `RespData` 有何异同？

**参考答案**：读会返回那个固定魔数而非 0，但 `resp` 仍是 `RESP_OKAY`（因为 `mem_err_i=0`）。与 `axi_err_slv` 的 `RespData` 相比：两者都把一个可识别魔数塞进读数据，但 `axi_zero_mem` 返回 OKAY（语义是「正常但全 0」），`axi_err_slv` 返回 DECERR/SLVERR（语义是「错误」）。所以魔数相同不等于语义相同——响应码才是区分「正常占位」与「错误兜底」的关键。

---

### 4.3 axi_lfsr：读返回伪随机、写压缩成校验的 LFSR 从端

#### 4.3.1 概念说明

测试一个 AXI 主端或一段互联时，常需要一个「**返回值不可预测但协议合法**」的从端——如果下游永远返回固定数据，主端的某些 bug（如错误地缓存了响应、把上一拍数据当成这一拍）可能被掩盖。`axi_lfsr` 用一个**线性反馈移位寄存器（LFSR）**充当这种从端：每次读返回当前的 LFSR 状态（一串伪随机数），每次写则把写数据「折叠」进 LFSR 状态（相当于计算一个校验和/摘要）。

README 把它的行为概括为（[README.md:49](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L49)）：

> AXI4-attached LFSR; read returns pseudo-random data, writes are compressed into a checksum.

它的实现同样遵循本库「组合优于配置」的哲学：先用 `axi_to_axi_lite`（u13-l1）把完整 AXI4 降到 AXI4-Lite，再交给真正干活的 `axi_lite_lfsr`。这样做的原因是——LFSR 的行为本质上是「单拍逐字」的（每个 W 拍折叠一次、每个 R 拍吐一个伪随机数），而 AXI4-Lite 正好是单拍模型，降到 Lite 后核心逻辑大幅简化，无需处理突发拆分（那部分由 `axi_to_axi_lite` 内部的 burst splitter 代劳）。

#### 4.3.2 核心流程

```text
            ┌──────────────── axi_lfsr (Level 3) ────────────────┐
 axi_req ──►│                                                     │──► axi_rsp
            │   ┌──────────────────┐    lite_req    ┌───────────┐ │
            │   │ i_axi_to_axi_lite│ ─────────────► │ i_axi_    │ │
            │   │ (u13-l1: 拆突发  │ ◄───────────── │ lite_lfsr │ │
            │   │  降到 Lite)      │    lite_rsp    │ (Level 2) │ │
            │   └──────────────────┘                └─────┬─────┘ │
            │                                             │       │
            │   串行播种口 w_ser_*/r_ser_* ────────────────┘       │
            │   (移位写入 LFSR 初值)                              │
            └─────────────────────────────────────────────────────┘
                                  │
              i_axi_lite_lfsr 内部：
              W 通道 → i_axi_opt_lfsr_w (压缩模式): reg = (reg>>1) ^ wdata  ← 写折叠成校验
              R 通道 → i_axi_opt_lfsr_r (生成模式): reg = (reg>>1) ^ 反馈   ← 读吐伪随机
              B 通道 → RESP_OKAY；读 R.resp = OKAY
```

底层 `axi_opt_lfsr` 是一个标准的 XNOR 反馈 LFSR：在每个时钟沿把寄存器右移一位，最高位填入反馈函数。反馈抽头（tap）取自经典 LFSR 表，按位宽 8/16/32/64/…/1024 选用（[src/axi_lite_lfsr.sv:178-188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L178-L188)）。其状态转移可写成（生成模式下，位宽 \(W\)，反馈函数 \(f\)）：

\[
\text{reg}^{(t+1)}[i] = \text{reg}^{(t)}[i+1],\quad i=0\ldots W-2;\qquad
\text{reg}^{(t+1)}[W\!-\!1] = f(\text{reg}^{(t)})
\]

而在压缩模式（写）下，反馈位还会额外异或上写数据的对应位，从而把写数据「吸收」进状态。

#### 4.3.3 源码精读

`axi_lfsr` 顶层只做两件事：例化 `axi_to_axi_lite`（[src/axi_lfsr.sv:81-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lfsr.sv#L81-L101)）与 `axi_lite_lfsr`（[L103-119](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lfsr.sv#L103-L119)），两者用 Lite 结构体 `axi_lite_req`/`axi_lite_rsp` 直连。桥的并发上限设为 2（[L86-87](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lfsr.sv#L86-L87)），注释「We only have 1 cycle latency; 2 is enough」。模块还引出一组**串行播种端口** `w_ser_*`/`r_ser_*`（[L43-53](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lfsr.sv#L43-L53)），用于在不经过 AXI 的情况下逐位移入 LFSR 初值——这让测试可以确定性地设置伪随机序列起点。

真正干活的是 `axi_lite_lfsr`（[src/axi_lite_lfsr.sv:18-137](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L18-L137)）。它的 AW/AR 通道几乎「无视」地址（`aw_ready = !w_ser_en_i`，[L63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L63)；`ar_ready = !w_ser_en_i`，[L117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L117)）——无论访问哪个地址，行为都一样。

写通路例化一个**压缩模式**的 `axi_opt_lfsr`（`inp_en_i = w_lfsr_en`，[src/axi_lite_lfsr.sv:66-78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L66-L78)），在每个被接受的 W 拍把写数据异或折叠进移位寄存器：

```systemverilog
assign w_lfsr_en = req_i.w_valid & rsp_o.w_ready;
// 仅 strobe 使能的字节用写数据，其余字节保留旧状态（gen_data_strb_connect, L83-93）
w_data_in[i*8+:8] = (req_i.w.strb[i]) ? req_i.w.data[i*8+:8]
                                      : w_data_out[i*8+:8];
```

压缩移位规则在底层 `axi_opt_lfsr` 的 [src/axi_lite_lfsr.sv:192-195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L192-L195)：`reg_d[i] = reg_q[i+1] ^ data_i[i]`。B 响应恒为 `RESP_OKAY`，用一个深度 2 的 `stream_fifo` 排队（[L96-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L96-L114)）。

读通路例化一个**生成模式**的 `axi_opt_lfsr`（`inp_en_i = 1'b0`，[src/axi_lite_lfsr.sv:120-132](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L120-L132)），`data_o` 直接接到 `rsp_o.r.data`，于是每个被接受的 R 拍都吐出当前 LFSR 状态、并推进到下一拍：

```systemverilog
assign rsp_o.r.resp  = axi_pkg::RESP_OKAY;
assign rsp_o.r_valid = !r_ser_en_i;
assign r_lfsr_en     = req_i.r_ready & rsp_o.r_valid;   // 握手才推进
```

（[src/axi_lite_lfsr.sv:133-135](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L133-L135)）

底层 LFSR 的反馈抽头按位宽查表（[src/axi_lite_lfsr.sv:178-188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L178-L188)），例如 32 位取 `{32, 30, 26, 25}`（即 XNOR 第 32/30/26/25 级）；串行播种与三态（压缩/生成/串行）的选通逻辑在 [L203-215](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L203-L215)。状态寄存器用 `FFL`（带 load 使能的寄存器宏）在 `en_i | ser_en_i` 时更新（[L223](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L223)）。

#### 4.3.4 代码实践（参数微调 + 行为推演型）

**目标**：理解「写压缩成校验、读返回伪随机」这两条行为，以及串行播种端口如何让伪随机序列变得可复现。

**步骤**：

1. 在 [src/axi_lite_lfsr.sv:79-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L79-L80) 确认 `w_lfsr_en = req_i.w_valid & rsp_o.w_ready`——即「每个被接受的 W 拍推进一次压缩」。
2. 跟踪一次写：写数据经 `gen_data_strb_connect`（[L83-93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L83-L93)）按 strobe 选择性送入 `i_axi_opt_lfsr_w` 的 `data_i`，再在 [L192-195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L192-L195) 被异或折叠进状态。
3. 假设用串行端口 `r_ser_en_i=1` 配合 `r_ser_data_i` 移入一个固定初值（如全 1），之后再 `r_ser_en_i=0` 连读两拍。推演：两拍读数据是否相同？第二拍是否等于第一拍经一次 LFSR 推进的结果？

**需要观察的现象**：每次写都会改变写侧 LFSR 状态（但写状态不影响读侧——两者是**两个独立的 LFSR**，见 [L66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L66) 与 [L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L120)）；每次读都会推进读侧 LFSR，故连续两次读得到**不同**的伪随机数。

**预期结果**：固定初值下读序列**完全确定、可复现**——这正是 LFSR「伪随机」而非「真随机」的特性，也是串行播种端口存在的意义：让测试可以复现同一串「随机」响应。本结论「待本地验证」：可在仿真里给 `r_ser_*` 移入初值后连读，对照两次波形确认第二拍等于第一拍的一次 LFSR 推进。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi_lfsr` 要先降到 AXI4-Lite 再驱动 LFSR，而不是直接在完整 AXI4 上写？

**参考答案**：因为 LFSR 的核心是「每个 W 拍折叠一次、每个 R 拍吐一个数」的单拍逐字模型，与 AXI4-Lite 的无突发单拍事务天然吻合。直接在完整 AXI4 上实现就得自己处理突发拆分、多拍 W 的逐拍折叠、`len+1` 拍 R 的逐拍推进与保序——而这些 `axi_to_axi_lite`（u13-l1）内部的 burst splitter 已经替它做完了。这是「组合优于配置」的又一次体现：把难题交给已存在的桥，自己只做独特的 LFSR 部分。

**练习 2**：`axi_lfsr` 的读侧与写侧是同一个 LFSR 还是两个？这意味着「先写一段数据，再读」能否读回刚才写入的校验和？

**参考答案**：是**两个独立的** LFSR——`i_axi_opt_lfsr_w`（[src/axi_lite_lfsr.sv:66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L66)）服务写、`i_axi_opt_lfsr_r`（[L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_lfsr.sv#L120)）服务读，两者状态互不相通。因此**不能**通过「先写后读」读回写侧的校验和——写折叠进的是写 LFSR，读吐出的是读 LFSR 的独立伪随机序列。若想让两者关联，需在模块外自行把写侧状态搬到读侧（本模块未提供此通路）。

---

### 4.4 三者对比与选型

#### 4.4.1 概念说明

把这三个端点放在一起对比，能帮你在搭系统时快速选对积木。它们的共同点是：**都是不译码地址、不维护业务存储的固定行为 Slave**，差异在于「对一笔事务返回什么」。

#### 4.4.2 对比表

| 维度 | `axi_err_slv` | `axi_zero_mem` | `axi_lfsr` |
|------|---------------|----------------|------------|
| 读返回数据 | 固定魔数 `RespData`（默认 `…BADCAB1E`） | 恒为 `0` | 伪随机（LFSR 状态） |
| 响应码 `resp` | **DECERR / SLVERR**（错误） | **OKAY** | **OKAY** |
| 写数据去向 | 吸收（仅用于触发 B） | 吸收（进黑洞） | 折叠进写侧 LFSR（成校验） |
| 是否需要真实存储 | 否 | 否（假装有，实则 0） | 否（只有移位寄存器） |
| 实现套路 | 自行用 `fifo_v3` 摆五通道 | 复用 `axi_to_detailed_mem` | 复用 `axi_to_axi_lite` + LFSR |
| 典型用途 | 兜底未映射地址、模拟故障从端 | 占位/DMA 写黑洞/零填充源 | 不可预测但合法的激励下游 |
| 在 xbar 中的角色 | **内置**作译码错误从端（u6-l2） | 外挂在某 master 端口 | 外挂在某 master 端口 |

#### 4.4.3 代码实践（选型判断型）

**目标**：给定三个真实工程场景，判断该用哪个端点。

**步骤**：对下面每个场景，从上表中选出最合适的模块并说明理由。

1. 一个 DMA 引擎配置好了向地址 `0x4000_0000` 搬 1 KiB 数据，但该地址在 SoC 地址地图里尚未分配给任何外设——你希望 DMA 收到一个明确错误而不是挂死。
2. 你在验证一个 AXI cache 控制器，需要一个下游，它的返回值每次都不同，好暴露「缓存命中/缺失判断错误」的 bug。
3. 你设计了一个加速器，它会把中间结果写到一个「只写不读」的丢弃缓冲区，你不想为这块缓冲区分配真实 SRAM。

**预期结果**：

1. **`axi_err_slv`**（或直接由 xbar 内置的译码错误从端处理，Resp=DECERR）。语义是「错误」，DMA 能据 `resp` 报错并恢复。
2. **`axi_lfsr`**。返回值伪随机、每次不同，最易暴露缓存逻辑 bug。
3. **`axi_zero_mem`**。写被吸收、不占存储，正是「丢弃缓冲区」。

> 关键区分点永远是**响应码**：要「错误」用 `err_slv`，要「正常但内容特殊」用 `zero_mem`/`lfsr`。

## 5. 综合实践

**任务**：搭一个最小 `axi_xbar` 系统——某个 slave 端口挂 `axi_lfsr` 作激励源，未映射地址由 xbar 内置的 `axi_err_slv` 兜底，验证「访问 lfsr 端口得到 OKAY + 伪随机数据」「访问未映射地址得到 DECERR + 魔数」。

> 说明：本库**没有**为 `axi_lfsr`/`axi_zero_mem`/`axi_err_slv` 单独提供测试台（`test/` 下不存在 `tb_axi_lfsr.sv` 等文件，检索可确认）。`axi_err_slv` 的行为在 `tb_axi_xbar.sv` 里通过 xbar 译码错误路径被间接覆盖。因此本实践为**自建最小测试台**，下方代码是「示例代码」，需本地仿真器运行，部分细节「待本地验证」。

### 5.1 拓扑

```text
                              ┌──────────── axi_xbar (u6) ────────────┐
   rand_master (axi_test) ───►│ slave[0]                               │
                              │   ├─ 命中规则 → master[0] → axi_lfsr   │── 读=伪随机(OKAY)
                              │   └─ 未命中   → 内置 axi_err_slv       │── 读=魔数(DECERR)/B=DECERR
                              │             (slv_reqs[i][NoMstPorts])  │
                              └────────────────────────────────────────┘
```

关键认知（承接 u6-l2）：xbar 的「译码错误从端」是**自动内置**的——见 [src/axi_xbar_unmuxed.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195-L211)，你无需手动例化 `axi_err_slv`，只要某笔事务的地址不命中任何规则，它就会落到那个内置错误从端并返回 DECERR。所以本实践只需：例化 xbar + 把 `axi_lfsr` 挂到 master[0] + 配一张只覆盖部分地址区间的映射表。

### 5.2 操作步骤

1. **声明类型**：用 `AXI_TYPEDEF_ALL` 生成完整 AXI4 的 `axi_req_t`/`axi_resp_t`（参考 u2-l4）。注意 xbar 要求 master 端口 ID 宽度 = slave 端口 ID 宽度 + ⌈log₂(NoSlvPorts)⌉（u6-l1），配置 `xbar_cfg_t` 时让 `AxiIdWidthSlvPorts` 足够。
2. **例化 xbar**：`axi_xbar_intf #(.Cfg(...), .axi_req_t(...), .axi_resp_t(...), .rule_t(axi_pkg::xbar_rule_64_t))`，1 个 slave 端口、1 个 master 端口。地址映射只覆盖 `0x0000_0000–0x0000_1000` 指向 master[0]，其余地址留给内置错误从端。
3. **挂载 lfsr**：把 `axi_lfsr`（`DataWidth`/`AddrWidth`/`IdWidth`/`UserWidth` 与 xbar master 端口对齐）的 `req_i`/`rsp_o` 接到 xbar 的 master[0] 输出。串行播种口 `*_ser_*` 先不用，接 `'0`。
4. **挂载主端**：用 `axi_test::axi_rand_master`（u3-l2）驱动 xbar 的 slave[0]。
5. **定向读 lfsr 区**：用 rand_master 的定向读接口（或直接 `axi_driver` 的 `send_ar`/`recv_r`），对 `0x0000_0000` 连读两拍。断言：`r.resp == RESP_OKAY` 且两拍 `r.data` **不相等**（伪随机推进）。
6. **定向读未映射区**：对 `0xFFFF_F000`（不在规则内）发一次读。断言：`r.resp == RESP_DECERR`，且 `r.data` 的低 32 位为 `32'hBADCAB1E`（截断自 `RespData`，见 4.1.3）。
7. **判定**：累计错误数，`$display("Errors: %0d", errors)`，以 `Errors: 0` 为通过（与 u1-l4 的日志判成败一致）。

### 5.3 地址映射表示例代码

下列为**示例代码**（非仓库原有文件），仅示意映射表写法，省略类型声明与时钟复位，「待本地验证」：

```systemverilog
// 示例代码：xbar 地址映射规则——只覆盖低 4 KiB，其余交给内置 axi_err_slv
xbar_rule_64_t [0:0] addr_map;
assign addr_map[0] = '{ idx: 0,                  // 命中则送往 master[0] (axi_lfsr)
                        start_addr: 64'h0000_0000,
                        end_addr:   64'h0000_1000, // 前闭后开 (u6-l2)
                        default:    1'b0 };
// 不在 [0x0, 0x1000) 区间的地址 → xbar 自动改送内置译码错误从端 → DECERR
```

### 5.4 需要观察的现象与预期结果

- **访问 lfsr 区（命中）**：B/R 的 `resp` 为 `RESP_OKAY`；连读两拍数据不同（读侧 LFSR 每握手推进一次，见 4.3.3）。
- **访问未映射区（未命中）**：读 `resp` 为 `RESP_DECERR`、数据低 32 位为 `0xBADCAB1E`；写则回一个 `resp=DECERR` 的 B。这正是 xbar 内置 `axi_err_slv`（`Resp=RESP_DECERR`、`RespData` 默认值截断）的输出，对应 [doc/axi_xbar.md:33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L33) 的描述。
- **判定**：若两类断言全部满足、最终 `Errors: 0`，则系统接线正确，三个端点的行为得到验证。

> 进阶：把第 5 步改为「先写 lfsr 区再读」，确认读到的仍是读侧 LFSR 的独立伪随机序列、与写无关（对应 4.3.5 练习 2）；再用串行端口 `r_ser_*` 给读侧 LFSR 移入固定初值，复现同一串伪随机数，体会「伪随机可复现」。

## 6. 本讲小结

- `axi_err_slv`（Level 3）是错误从端：用 `w_fifo`/`b_fifo`/`r_fifo` 三组 `fifo_v3` 自行完成五通道握手，写回 1 个 B、读回 `len+1` 拍 R，`resp` 恒为 `Resp`（仅允许 DECERR/SLVERR）、读数据为魔数 `RespData`；可选前置 `axi_atop_filter` 兼容原子操作。
- `axi_zero_mem`（Level 3）是 `/dev/zero`：复用 `axi_to_detailed_mem`，把写方向存储侧输出全部悬空（写进黑洞）、读数据硬接 `'0`、授予信号回环（`gnt=req`）——自身几乎不写逻辑，也不占任何业务存储。
- `axi_lfsr`（Level 3）是伪随机/校验从端：先 `axi_to_axi_lite` 降到 Lite，再用 `axi_lite_lfsr` 驱动**两个独立的** `axi_opt_lfsr`——写侧压缩模式把写数据异或折叠成校验、读侧生成模式吐出可复现的伪随机序列，读写互不影响。
- 三者共同点：都是不译码地址、不维护业务存储的固定行为 Slave；选型关键看**响应码**——错误用 `err_slv`、正常但特殊内容用 `zero_mem`/`lfsr`。
- `axi_err_slv` 在 `axi_xbar` 中是**内置**的译码错误从端（[src/axi_xbar_unmuxed.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195-L211)），未命中规则的事务自动返回 DECERR + `BADCAB1E`，承接 u6-l2 的地址映射认知。

## 7. 下一步学习建议

- **补全综合实践**：按第 5 节自建测试台并跑通，重点观察「未命中地址 → DECERR」与「命中 lfsr → OKAY 但每次不同」两类波形，把本讲的三个端点与 u6 的 xbar 真正连起来。
- **深入 `axi_atop_filter`**（u15-l1）：`axi_err_slv` 在 `ATOPs=1` 时前置了它。学完原子操作后回看本模块，理解为何错误从端也要处理 ATOP——因为不支持的下游若收到原子写会出错，而错误从端恰恰要兜住「上游以为能发、下游其实不能处理」的一切。
- **进入异构网络与系统级设计**（u15-l4）：把 `axi_err_slv`/`axi_zero_mem` 作为「占位/兜底」、`axi_lfsr` 作为「激励源」织进一个跨时钟域、跨数据/ID 宽度的完整片上网络，体会这三个常备积木在真实系统里的接线位置。
