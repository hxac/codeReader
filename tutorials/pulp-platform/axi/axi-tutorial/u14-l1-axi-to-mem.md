# axi_to_mem 及其变体

## 1. 本讲目标

本讲讲解 AXI 库中「把 AXI4 总线接到 SRAM」的一族模块。学完后你应当能够：

1. 说清 `axi_to_mem` 对外暴露的 **req / gnt / rvalid 存储流接口** 的时序契约——尤其是「写请求也必须返回一个 `rvalid`」这条与普通 AXI 从端不同的规则。
2. 读懂内核 `axi_to_detailed_mem` 如何用一条「AR/AW → 元数据 → 仲裁 → 分发到 bank → 汇合 → 动态分叉到 B/R」的流式管线把 AXI 突发翻译成逐拍存储访问。
3. 区分三种「带宽增强」变体——`banked`（分体）、`interleaved`（读旁路写）、`split`（读写端口彻底分离）——它们各自多花硬件换来了什么、又受限于什么样的 SRAM 物理拓扑。
4. 根据手头 SRAM 宏的数量、位宽与端口数，在基础版与三种变体之间做出选型。

本讲承接 **u9-l1（axi_burst_splitter）**：本族模块**不自己拆突发**，而是用断言强制要求 `len>0` 时必须是 `BURST_INCR`，因此当下游 SRAM 吃不下长突发时，需要把 `axi_burst_splitter` 串在前级。

## 2. 前置知识

- **AXI4 五通道与握手**（u1-l3）：AW/W/B/AR/R，valid/ready 同高才算一拍。本族模块的 slave 端就是标准 AXI4+ATOP。
- **结构体 req_t / resp_t 与 typedef/assign 宏**（u2-l4）：内核只认 `axi_req_t`/`axi_resp_t`，接口外壳 `_intf` 用宏在 `AXI_BUS` 与结构体之间搬运。
- **突发与 INCR**（u1-l3、u9-l1）：`len = 拍数 - 1`；INCR 每拍地址加 `2^size`。本族要求多拍突发必须是 INCR。
- **流式握手原语**（common_cells）：`stream_mux`、`stream_fork`、`stream_join`、`stream_fifo`、`stream_xbar`、`rr_arb_tree`。它们都是 valid/ready 握手、和 AXI 通道握手同构，本族大量复用。
- **SRAM 宏接口**：一颗典型 SRAM（如 `tc_sram`）有 `req_i / we_i / addr_i / wdata_i / be_i / rdata_o`，往往还有固定的读延迟（请求当拍后 N 拍出数据）。本族模块就是把 AXI 适配成这种简单接口。

> 关键术语：**bank（存储体/宏）**、**beat（一拍数据）**、**outstanding（在途）**、**valid/ready 握手**、**req/gnt（请求/许可）**、**rvalid（响应有效）**。

## 3. 本讲源码地图

| 文件 | 层级 | 作用 |
| --- | --- | --- |
| [src/axi_to_detailed_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv) | Level 2 | **真正内核**。带全量边带（id/user/cache/prot/qos/region/lock/err/exokay），把 AXI 突发改写成存储流；内部还含子模块 `mem_stream_to_banks_detailed`（把一拍宽请求拆到多个 bank）。 |
| [src/axi_to_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv) | Level 3 | **精简外壳**。把 `axi_to_detailed_mem` 的 `UserWidth` 钉成 1、把 `err/exokay` 输入接地，只暴露必要的存储流端口。 |
| [src/axi_to_mem_banked.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv) | Level 4 | **分体版**。读写各用一个 `axi_to_mem`（可并行），并支持多于「一个 AXI 字宽」所需的 bank 宏，用 `stream_xbar` 按地址把请求路由到对应宏。 |
| [src/axi_to_mem_interleaved.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv) | Level 4 | **读旁路写版**。同样读写分开，但每个 bank 用 `rr_arb_tree` 在读写两路之间细粒度仲裁，让读可以越过拥塞的写。 |
| [src/axi_to_mem_split.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv) | Level 4 | **端口分离版**。读写各自独占一半存储端口，互不干扰；前提是同一个 bank 地址能从不同端口访问（如双口 SRAM）。 |
| [test/tb_axi_to_mem_banked.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv) | — | banked 版的随机回归测试台：`axi_rand_master` + 多个 `tc_sram` + `axi_scoreboard`，并统计每个 bank 的利用率。 |

> 依赖层级一目了然：`detailed_mem(L2) → to_mem(L3) → banked/interleaved/split(L4)`。三种变体都**复用** `axi_to_mem` 作为子模块，差别只在「怎么把读写分流、怎么把请求分发到宏」。

## 4. 核心概念与源码讲解

### 4.1 axi_to_detailed_mem：AR/AW → 存储流的真正内核

#### 4.1.1 概念说明

`axi_to_detailed_mem` 是这一族里**唯一真正写了 AXI 协议逻辑**的模块（另外四个要么是它的外壳，要么用它做零件）。它解决的问题是：

> 给我一颗（或一组）只有 `req/gnt/addr/wdata/strb/we/rvalid/rdata` 的简单 SRAM，怎么让 AXI4 主端把它当从端访问？

它定义了一套**存储流契约**，下游 SRAM 必须遵守：

- **请求方向（AXI → 存储器）**：`mem_req_o`（请求有效，相当于 valid）、`mem_gnt_i`（存储器许可，相当于 ready）。两者同高表示一次请求被接受。载荷有 `mem_addr_o`（字节地址）、`mem_we_o`（写使能）、`mem_wdata_o`/`mem_strb_o`（写数据/字节选通）、`mem_atop_o`（原子操作码）等。
- **响应方向（存储器 → AXI）**：只有 `mem_rvalid_i`（响应有效）和 `mem_rdata_i`（读数据）。

> ⚠️ **最重要的契约**：**每一个被接受的请求（无论读或写）都必须由存储器返回恰好一个 `mem_rvalid_i` 脉冲**。写请求没有独立的「写应答」信号——写完成也靠 `rvalid` 表达。这就把读写统一成了一条简单的流式管道，但要求下游 SRAM 对写也要回一个完成信号（在测试台里通常用延迟寄存器造出来，见 4.3.4）。

模块注释里还有一条设计结论值得记住：

> If both read and write channels of the AXI4+ATOP are active, both will have an utilization of 50%.

也就是说，**内核同一时刻只服务读或写之一**（二者经一个 `stream_mux` 二选一），所以读写并发时各占一半带宽——这正是后面 `banked/interleaved/split` 要解决的痛点。

#### 4.1.2 核心流程

把内核想象成一条流水线，一拍 AXI 请求从左走到右：

```
AR/AW (含 W)
   │  ① 生成「元数据 meta_t」(addr/id/size/last/write/qos...)
   ▼
stream_mux ② 读写二选一仲裁（QoS 优先、单拍写优先、进行中突发优先、否则轮询）
   │
stream_fork ③ 一分三：去 B/R 选择位 / 去 meta 缓冲 / 去存储请求
   ├──▶ mem_stream_to_banks_detailed ④ 把一拍宽请求按数据车道拆到 NumBanks 个 bank
   │         （每个 bank 各自 req/gnt，各自回 rvalid/rdata）
   ▼
stream_join ⑤ 存储响应 与 meta 缓冲 汇合
   │
stream_fork_dynamic ⑥ 按 sel_b/sel_r 动态分叉到 B 通道或 R 通道
   ▼
B / R
```

读地址递增与「剩余拍数」由计数器 `r_cnt_q`/`w_cnt_q` 维护：新 AR/AW 握手时把 `len` 装入计数器，之后每拍把地址加上 `axi_pkg::num_bytes(size)`、计数器减一，直到归零置 `last`。这正是把 AXI 突发「展开」成逐拍存储访问的地方。

地址推进的步长用 axi_pkg 的函数（回顾 u2-l2）：

\[
\text{step} = 2^{\text{size}} \quad\text{（即每拍字节数）}
\]

#### 4.1.3 源码精读

**读通路：把 AR 展开成逐拍 meta。** 新 AR 握手时记下 `len` 到 `r_cnt_q`，之后每拍自增地址（[src/axi_to_detailed_mem.sv:170-213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L170-L213)）：

```systemverilog
// Handle new AR if there is one.
end else if (axi_req_i.ar_valid) begin
  rd_meta_d = '{ addr: ..., last: (axi_req_i.ar.len == '0), size: axi_req_i.ar.size, write: 1'b0, ... };
  rd_valid  = 1'b1;
  if (rd_ready) begin
    r_cnt_d             = axi_req_i.ar.len;   // 剩余拍数
    axi_resp_o.ar_ready = 1'b1;
  end
end
```

进行中的突发则靠 `r_cnt_q > 0` 续拍，地址每拍加 `num_bytes(size)`，最后一拍置 `last`。

**读写仲裁：QoS 优先、单拍写优先。** 读写 meta 经 `stream_mux` 二选一；仲裁策略在 [src/axi_to_detailed_mem.sv:279-321](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L279-L321)，优先级依次是：QoS 高者 → 单拍写优先于读突发 → 进行中的突发优先 → 否则轮询防饿死。注意「单拍写优先」的注释解释了原因：读突发可在 AXI 上交错，写突发不可交错，所以赶紧把单拍写送走。

**按数据车道拆 bank。** 一拍 `DataWidth` 位请求被 `mem_stream_to_banks_detailed` 拆成 `NumBanks` 路，每路 `DataWidth/NumBanks` 位（[src/axi_to_detailed_mem.sv:436-469](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L436-L469)）。注意这里的拆法是**按数据车道（position）**，不是按地址：第 `i` 个 bank 拿到的是宽字里的第 `i` 段字节（[src/axi_to_detailed_mem.sv:872-877](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L872-L877)）：

```systemverilog
assign bank_req[i].addr  = align_addr(addr_i) + 7'(i * BytesPerBank);
assign bank_req[i].wdata = wdata_i[i*BitsPerBank+:BitsPerBank];   // 按位段切
assign bank_req[i].strb  = strb_i[i*BytesPerBank+:BytesPerBank];
```

> 即：一次 AXI 宽字访问会**同时**打到所有 `NumBanks` 个 bank，每个 bank 存宽字的其中一段。这是「用多个窄宏拼一个宽字」的做法。

**响应合流并动态分叉到 B/R。** 存储侧 `rvalid/rdata` 与 meta 缓冲用 `stream_join` 对齐，再用 `stream_fork_dynamic` 按 `sel_b`/`sel_r` 决定这笔响应该走 B 通道还是 R 通道（[src/axi_to_detailed_mem.sv:482-495](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L482-L495)）。其中 `sel_r` 还把原子写的读响应也算上：`sel_r = ~meta.write | meta.atop[5]`（`atop[5]` 即 `ATOP_R_RESP`，回顾 u2-l1）。

**不拆突发的硬约束。** 内核用断言强制多拍突发必须是 INCR（[src/axi_to_detailed_mem.sv:587-592](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L587-L592)）：

```systemverilog
assert property (@(posedge clk_i) axi_req_i.ar_valid && axi_req_i.ar.len > 0 |->
    axi_req_i.ar.burst == axi_pkg::BURST_INCR)
  else $error("Non-incrementing bursts are not supported!");
```

这正是本讲依赖 u9-l1 的原因：要喂 FIXED/WRAP 或超长突发，先用 `axi_burst_splitter`/`axi_burst_unwrap` 预处理。

#### 4.1.4 代码实践

**目标**：不跑仿真，靠读源码把一笔「长度 4 的 INCR 读」在内核里走一遍，验证你对七个流水段的记忆。

**步骤**：
1. 在 [src/axi_to_detailed_mem.sv:170-213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L170-L213) 确认：第一拍 AR 握手时 `r_cnt_d` 被赋成 `ar.len`（=3），`last` 为假。
2. 在同一段的「`r_cnt_q > 0`」分支确认：第 2~4 拍地址每拍加 `num_bytes(size)`，`r_cnt_q` 递减，只有当 `r_cnt_q == 8'd1` 时 `last` 才拉高。
3. 在 [src/axi_to_detailed_mem.sv:548-555](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L548-L555) 确认 R 通道的 `last` 取自 `meta_buf.last`，即上游的 `last` 经 meta 缓冲传到 R。
4. 在 [src/axi_to_detailed_mem.sv:482-495](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L482-L495) 确认：因为这笔是读（`sel_r=1, sel_b=0`），四拍响应全部走 R 通道、不走 B。

**需要观察的现象 / 预期结果**：4 笔存储请求打到 bank、产生 4 个 `rvalid`，最终 AXI 侧收到 4 拍 R，其中只有最后一拍 `r_last=1`，且全程没有 B。（纯源码阅读型实践，无需运行；行为结论由源码逻辑直接得出。）

#### 4.1.5 小练习与答案

**练习 1**：为什么内核要求「写请求也必须返回 `rvalid`」？
**答案**：因为读写共用同一条流式管道，模块靠 `rvalid` 知道「这笔请求完成了、可以回收 meta 并推进」，不区分读写。如果写不回 `rvalid`，meta 缓冲与在途计数器永远等不到回收，管线会死住。

**练习 2**：读写都活跃时为什么各只有 50% 带宽？
**答案**：内核用单个 `stream_mux` 在读写 meta 间二选一再送给存储侧，同一拍只能服务其一；长期看读写均分，故各 50%。要打破这个限制就需引入 4.3~4.5 的变体。

**练习 3**：`sel_r = ~meta.write | meta.atop[5]` 里 `atop[5]` 的作用？
**答案**：`atop[5]` 是 `ATOP_R_RESP`（u2-l1），表示这笔原子写除了 B 还要产生 R 响应；此时即便 `meta.write=1` 也要让响应对走 R 通道。

---

### 4.2 axi_to_mem：精简外壳

#### 4.2.1 概念说明

`axi_to_mem` 没有自己写任何协议逻辑，它只是 `axi_to_detailed_mem` 的一个**裁剪版**：把用不到的边带砍掉，让端口更简单。具体做法是钉死 `UserWidth = 1`，并把 detailed_mem 的 `mem_id_o / mem_user_o / mem_cache_o / mem_prot_o / mem_qos_o / mem_region_o / mem_lock_o` 等输出悬空、把 `mem_err_i / mem_exokay_i` 输入接地为 `'0`。

#### 4.2.2 核心流程

```
axi_to_mem (对外 9 类存储端口)
   └── 例化 axi_to_detailed_mem(.UserWidth(1))
           ├── 保留：req/gnt/addr/wdata/strb/atop/we/rvalid/rdata
           └── 丢弃：lock/id/user/cache/prot/qos/region、err/exokay 接 '0
```

#### 4.2.3 源码精读

整段实现就是一个例化（[src/axi_to_mem.sv:78-113](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L78-L113)），关键几行：

```systemverilog
axi_to_detailed_mem #(
  .UserWidth ( 1 ), ...
) i_axi_to_detailed_mem (
  ...
  .mem_atop_o   ( mem_atop_o   ),
  .mem_lock_o   (),          // 悬空：本模块不暴露 lock
  .mem_we_o     ( mem_we_o    ),
  .mem_id_o     (),          // 悬空：不暴露 id
  .mem_user_o   (),  .mem_cache_o(),  .mem_prot_o(),
  .mem_qos_o(),  .mem_region_o(),
  .mem_rvalid_i ( mem_rvalid_i ),
  .mem_rdata_i  ( mem_rdata_i  ),
  .mem_err_i    ('0),         // 接地：不使用错误信号
  .mem_exokay_i ('0)
);
```

模块头部注释同样写明「双活跃时各 50%」（[src/axi_to_mem.sv:16-18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L16-L18)），与内核一致。

#### 4.2.4 代码实践

**目标**：对照内核端口表，确认 `axi_to_mem` 砍掉了哪些信号。

**步骤**：把 [src/axi_to_detailed_mem.sv:63-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L63-L99)（内核的存储端口）与 [src/axi_to_mem.sv:57-75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L57-L75)（外壳的存储端口）并排比较，逐一标出「保留 / 悬空 / 接地」。

**预期结果**：保留 9 类（req/gnt/addr/wdata/strb/atop/we/rvalid/rdata）；悬空 7 个输出（lock/id/user/cache/prot/qos/region）；接地 2 个输入（err/exokay）。这正解释了为什么 detailed_mem 是 Level 2、to_mem 是 Level 3：后者依赖前者的类型与逻辑。

#### 4.2.5 小练习与答案

**练习 1**：如果我的 SRAM 需要返回读错误，该用 `axi_to_mem` 还是 `axi_to_detailed_mem`？
**答案**：必须用 `axi_to_detailed_mem`。`axi_to_mem` 把 `mem_err_i` 接成了 `'0`，错误永远传不出来；detailed_mem 才把 `mem_err_i`/`mem_exokay_i` 暴露并翻译成 `RESP_SLVERR`/`RESP_EXOKAY`。

**练习 2**：为什么把 `UserWidth` 钉成 1？
**答案**：精简使用场景下不需要 user 边带；钉成 1 让 `mem_user_t` 退化为单 bit 并被悬空，省去对外多引一根 user 端口。

---

### 4.3 axi_to_mem_banked：并行读写 + 多 bank 宏

#### 4.3.1 概念说明

`banked` 版针对内核的两个局限各下一刀：

1. **「读写各 50%」**：它用 `axi_demux` 把读写**静态分流**到两个独立的 `axi_to_mem`，于是读写可以**并行**进入各自的存储流水线。
2. **「bank 数 = 一个 AXI 字宽所需」**：内核的 `NumBanks` 只够拼出一个宽字。banked 版支持 `MemNumBanks` **多于** `BanksPerAxiChannel`（即存储总容量大于一个字），靠 `stream_xbar` 按**地址位**把请求路由到正确的宏。

代价是「more hardware」（模块注释原话），换来更高吞吐与更大容量。

它对 SRAM 拓扑的要求（见断言）：`MemNumBanks` 必须是 2 的幂，且 `MemNumBanks >= 2 * AxiDataWidth / MemDataWidth`（「2 倍」是因为读写各需一组宏）。

#### 4.3.2 核心流程

```
                AXI slave
                   │
        axi_demux ① 读写静态分流（aw_select=Write, ar_select=Read）
            ┌──────┴──────┐
        读 axi_to_mem   写 axi_to_mem   ② 各自把宽字拆成 BanksPerAxiChannel 路
            └──────┬──────┘
          每路提取 bank 选择位 (addr[BankSelOffset+:BankSelWidth])
                   │
            stream_xbar ③ 把 (2*BanksPerAxiChannel) 路按选择位路由到 MemNumBanks 个宏
                   │
            MemNumBanks 颗 tc_sram（读路径用 shift_reg 把响应延时 MemLatency 后回送）
```

两个关键派生参数（[src/axi_to_mem_banked.sv:96-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L96-L105)）：

- `BanksPerAxiChannel = AxiDataWidth / MemDataWidth`：拼一个 AXI 字需要几颗宏。
- `BankSelOffset = $clog2(MemDataWidth/8)`：字节地址里「bank 选择位」起始的位置。

bank 选择位宽 `BankSelWidth = idx_width(MemNumBanks)`，取自地址的 `[BankSelOffset +: BankSelWidth]`。这就是「跨 bank」的本质——地址在这几位上的取值决定请求落到哪颗宏：

\[
\text{bank} = \text{addr}[\,\text{BankSelOffset}+\text{BankSelWidth}-1 :\ \text{BankSelOffset}\,]
\]

#### 4.3.3 源码精读

**读写静态分流。** 一个 `axi_demux`（`NoMstPorts=2`），select 写死成 `aw_select=WriteAccess`、`ar_select=ReadAccess`；因为端口是静态选定，可以放心设 `UniqueIds=1` 省掉在途计数器硬件（[src/axi_to_mem_banked.sv:140-169](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L140-L169)，`UniqueIds` 的含义回顾 u5-l2）。

**两个 axi_to_mem 各管一路**（[src/axi_to_mem_banked.sv:176-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L176-L211)）：每个把宽字拆成 `BanksPerAxiChannel` 路输出（`NumBanks(BanksPerAxiChannel)`）。注意它把 `mem_gnt_i` 接成 `inter_ready & inter_valid`——把下游的 valid/ready 握手**翻译**成 req/gnt 契约。

**按地址路由到宏 + 响应回送。** 每路从地址里切出 bank 选择位（`inter_sel`）和宏内字地址（`mem_addr_t` 段），用 `stream_xbar` 路由到 `MemNumBanks` 个宏（[src/axi_to_mem_banked.sv:213-252](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L213-L252) 与 [src/axi_to_mem_banked.sv:258-279](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L258-L279)）。读响应回来的时机是 `MemLatency` 拍后，所以用 `shift_reg` 把「请求时的 bank 选择位」延时同样拍数，再用它做多路选择把正确的宏数据送回 `axi_to_mem`（这正是内核要求的「写也回 rvalid」由 SRAM 模型配合产生）：

```systemverilog
shift_reg #(.dtype(read_sel_t), .Depth(MemLatency)) i_shift_reg_rdata_mux (...);
assign res_valid[j] = r_shift_oup.valid;
assign res_rdata[j] = mem_rdata_i[r_shift_oup.sel];   // 用延时后的 sel 选回数据
```

**对拓扑的硬约束**（[src/axi_to_mem_banked.sv:296-303](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L296-L303)）：`MemNumBanks >= 2*BanksPerAxiChannel`、`MemNumBanks` 为 2 的幂、`AxiDataWidth % MemDataWidth == 0`。

#### 4.3.4 代码实践

**目标**：在已有测试台里加一行打印，眼见为实地观察「连续读地址被分发到不同 bank」。

**步骤**（基于 [test/tb_axi_to_mem_banked.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv)）：
1. 阅读 [test/tb_axi_to_mem_banked.sv:142-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L142-L177)：每颗 `tc_sram` 的 `mem_gnt[i]=1'b1`（永远许可），`mem_rvalid[i]` 用一段 `MemLatency` 级移位寄存器造出来——这就是「写也回 rvalid」的实现。
2. 在 monitor 进程（[test/tb_axi_to_mem_banked.sv:320-324](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L320-L324)）的 `if (mem_req[i])` 分支里临时加一行（**示例代码**，非项目原有）：

```systemverilog
if (mem_req[i]) begin
  dut_busy_cnt[i]++;
  $display("%0t> bank %0d addr %0h we=%b", $time(), i, mem_addr[i], mem_we[i]); // 示例：观察分发
end
```

3. 按 u1-l4 的方法用 `make sim-axi_to_mem_banked.log`（或 `scripts/run_vsim.sh`）跑默认配置（`TbAxiDataWidth=256, TbMemDataWidth=64, TbNumBanks=8`）。

**需要观察的现象 / 预期结果**：日志里同一时刻往往有多个 bank 的 `mem_req` 同时拉高（因为一个 256 位 AXI 字拆成 4 颗 64 位宏并行访问）；而相邻的 AXI 读地址（相差一个宽字步长 32 字节）会因为地址里 bank 选择位 `[5:3]` 不同而落到不同宏——这正是「跨 bank 分发」。仿真结尾的统计段（[test/tb_axi_to_mem_banked.sv:344-352](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L344-L352)）会打印每个 bank 的利用率，随机访问下各 bank 利用率应大致均衡。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：默认配置下 `BankSelOffset` 和 `BankSelWidth` 各是多少？
**答案**：`MemDataWidth=64` → `BankSelOffset = $clog2(64/8) = 3`；`MemNumBanks=8` → `BankSelWidth = idx_width(8) = 3`。所以 bank 选择位是字节地址的 `[5:3]`。

**练习 2**：为什么 banked 要求 `MemNumBanks >= 2*BanksPerAxiChannel`，而不是 `>= BanksPerAxiChannel`？
**答案**：因为读写各用一组宏（各自一个 `axi_to_mem`），最起码要 2 倍才能让读写在物理上互不干扰地并行；多出来的宏则用来扩充存储容量。

**练习 3**：读写分流用的 demux 为何能把 `UniqueIds` 设成 1？
**答案**：select 是静态的（写永远走端口 1、读永远走端口 0），不存在「同 ID 去往不同端口」的保序问题，因此可以删除在途 ID 计数器以省面积（原理见 u5-l2）。

---

### 4.4 axi_to_mem_interleaved：读旁路写

#### 4.4.1 概念说明

`interleaved` 版与 `banked` 一样把读写拆成两个 `axi_to_mem`，但它在**每个 bank** 上再加了一棵 `rr_arb_tree`，在「读路」和「写路」之间做**细粒度仲裁**。好处是：当某 bank 被一连串写堵住时，后续的读仍能通过仲裁插队，**读延迟不受写拥塞拖累**（模块注释：「Allows reads to bypass writes」）。

代价：每个 bank 需要一个小 FIFO 来记住「这一拍的请求当初是读还是写」，好把响应正确送回对应的读/写 `axi_to_mem`。

#### 4.4.2 核心流程

```
        AXI slave
           │
 axi_demux_simple ① 读写分流（无 spill，比 banked 更轻）
   ┌──────┴──────┐
 读 axi_to_mem  写 axi_to_mem   ② 各自输出 NumBanks 路
   └──────┬──────┘
   每个 bank i：
        rr_arb_tree ③ 在 {读路[i], 写路[i]} 间仲裁 → mem_req_o[i]
        fifo_v3     ④ 记下仲裁结果(0=读/1=写)，深度 BufDepth+1
        响应回送：按 FIFO 队头把 rvalid 分给读路或写路
```

#### 4.4.3 源码精读

**读写分流**用更轻的 `axi_demux_simple`（无 spill，`UniqueIds=1`，[src/axi_to_mem_interleaved.sv:104-123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L104-L123)）。

**每 bank 一棵仲裁树**（[src/axi_to_mem_interleaved.sv:224-239](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L224-L239)）：把读写两路在该 bank 的请求仲裁后送出 `mem_req_o[i]`，`idx_o` 输出本轮赢家是读(0)还是写(1)。

**响应回送 FIFO**（[src/axi_to_mem_interleaved.sv:242-257](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L242-L257)）：每个请求被接受时把 `arb_outcome`（读/写标记）押入 FIFO，响应到来时弹出队头，用它把 `mem_rvalid_i[i]` 分发给正确的路：

```systemverilog
assign w_mem_rvalid[i] = mem_rvalid_i[i] & !arb_outcome_head[i];  // 队头=读时给读路
assign r_mem_rvalid[i] = mem_rvalid_i[i] &  arb_outcome_head[i];  // 队头=写时给写路
```

#### 4.4.4 代码实践

**目标**：用源码理解「读如何旁路写」。

**步骤**：
1. 读 [src/axi_to_mem_interleaved.sv:224-239](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L224-L239)：仲裁器输入是 `{r_mem_req[i], w_mem_req[i]}`，输出给 `mem_req_o[i]`。
2. 构造一个心智场景：bank `i` 上写路正忙（`w_mem_req[i]=1` 持续多拍），此时读路 `r_mem_req[i]` 拉高。由于 `rr_arb_tree` 轮询，读请求会在某拍赢得仲裁先进入 SRAM，不必等写队列排空。

**需要观察的现象 / 预期结果**：读响应 `r_mem_rvalid` 能在写流未结束时出现——这就是「读旁路写」。相比之下，4.1 的内核里读写经单个 mux，读必须等写那一拍让出。（源码阅读型，行为由仲裁树轮询特性决定。）

#### 4.4.5 小练习与答案

**练习 1**：为什么每个 bank 要配一个响应回送 FIFO，而 banked 版不需要？
**答案**：interleaved 的同一个物理 bank 既要服务读路又要服务写路，响应到来时必须知道「这笔当初是读还是写」才能分发；FIFO 按请求顺序记录赢家标记。banked 版读写各用一组独立宏，物理上不共享，自然不需要。

**练习 2**：interleaved 用 `axi_demux_simple` 而非 `axi_demux`，差异是什么（回顾 u5-l1）？
**答案**：`demux_simple` 不带可选 spill 寄存器、更省面积；这里分流是静态的，不需要 spill 切路径，所以用简单版即可。

---

### 4.5 axi_to_mem_split：读写端口彻底分离

#### 4.5.1 概念说明

`split` 版走极端：**读写各自独占一半存储端口，彼此完全不共享、不仲裁**。模块注释点明前提：「This can only be used when addresses for the same bank are accessible from different memory ports.」——也就是说，同一个 bank 地址必须能从两个不同端口访问到（典型场景是**双口 SRAM**，或读、写端口物理分离的宏）。

端口总数是派生量 `NumMemPorts = 2*AxiDataWidth/MemDataWidth`：下半给读、上半给写。

#### 4.5.2 核心流程

```
        AXI slave
           │
 axi_demux_simple ① 读写分流
   ┌──────┴──────┐
 读 axi_to_mem        写 axi_to_mem
 (NumBanks=NumMemPorts/2)  (NumBanks=NumMemPorts/2)
   │                       │
   ▼                       ▼
 mem_*_o[下半]        mem_*_o[上半]   ② 各占一半物理端口，互不干扰
```

#### 4.5.3 源码精读

派生端口数（[src/axi_to_mem_split.sv:41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L41)）：`NumMemPorts = 2*AxiDataWidth/MemDataWidth`。

两个 `axi_to_mem` 各拿一半端口切片：读用 `[NumMemPorts/2-1:0]`，写用 `[NumMemPorts-1:NumMemPorts/2]`（[src/axi_to_mem_split.sv:111-163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L111-L163)）。除此之外没有仲裁、没有路由 xbar——结构最直白。

注意一个细节：读路 `axi_to_mem` 的 `HideStrb` 写死为 `1'b0`（读不写数据，strb 无意义），写路才用传入的 `HideStrb`。

#### 4.5.4 代码实践

**目标**：验证你对「端口对半分」的理解。

**步骤**：设 `AxiDataWidth=64, MemDataWidth=32`，则 `NumMemPorts = 2*64/32 = 4`。读 `axi_to_mem` 的 `NumBanks = 4/2 = 2`，占用 `mem_req_o[1:0]`；写 `axi_to_mem` 占用 `mem_req_o[3:2]`。在 [src/axi_to_mem_split.sv:127-135](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L127-L135) 与 [src/axi_to_mem_split.sv:154-162](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L154-L162) 对照端口切片范围确认。

**需要观察的现象 / 预期结果**：读请求只会让 `mem_req_o[1:0]` 翻转，写请求只会让 `mem_req_o[3:2]` 翻转，两侧在时间上完全独立、可在同一拍同时活跃。（源码阅读型。）

#### 4.5.5 小练习与答案

**练习 1**：split 版为什么不能用在普通单口 SRAM 上？
**答案**：单口 SRAM 同一地址同一时刻只能一笔访问；split 把同一 bank 地址既暴露给读端口又暴露给写端口，若两者同时访问同地址，单口宏无法服务。需要双口宏或读/写端口分离的存储器。

**练习 2**：相对 interleaved，split 牺牲了什么、换来了什么？
**答案**：split 需要双口存储器（更贵/更稀缺的宏），但换来读写**完全无仲裁**地并行、时序最确定；interleaved 用单口宏 + 仲裁，面积/工艺更友好，但读写仍需在每个 bank 上竞争。

---

### 4.6 变体选型总览

把四兄弟放在一起对比（基础版 + 三变体）：

| 模块 | 读写关系 | bank/端口含义 | 对 SRAM 拓扑的要求 | 相对开销 | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| `axi_to_mem` | 读写共享一条流，各 50% | `NumBanks` = 拼一个宽字所需宏数，按数据车道拆 | 单组窄宏即可 | 最小 | 带宽要求不高、面积优先 |
| `axi_to_mem_banked` | 读写静态分流，可并行 | `MemNumBanks` 可大于一字所需，按地址位路由 | 2 的幂、≥2 倍宽字所需 | 中（多一组宏 + xbar） | 大容量、读写并发、地址散布 |
| `axi_to_mem_interleaved` | 每 bank 读写细粒度仲裁，读可旁路写 | 同 `axi_to_mem`（`NumBanks` 按车道） | 单组宏即可 | 中（每 bank 仲裁 + FIFO） | 读延迟敏感、写拥塞重 |
| `axi_to_mem_split` | 读写各占一半端口，完全独立 | `NumMemPorts = 2*宽字所需`，对半分 | **双口 / 读写端口分离** | 端口多、需特殊宏 | 有双口 SRAM、要确定性最高并行 |

**选型口诀**：
- 先问「下游能并行服务读写吗？」不能 → 用基础 `axi_to_mem`（或配合 `axi_burst_splitter`）。
- 能并行，且地址空间远大于一个宽字 → `banked`（多宏 + 地址路由）。
- 容量不大但怕写堵读 → `interleaved`（读旁路写）。
- 手里有双口宏、追求极致并行 → `split`。

需要全量边带（错误码、EXOKAY、user 等）时，把上述里的 `axi_to_mem` 换成 `axi_to_detailed_mem`。

## 5. 综合实践

> **任务**：用 `axi_to_mem_banked` 把 AXI 主端接到 **2 颗** SRAM 模型上，发起**跨 bank 的连续读**，验证地址被正确分发到两个 bank。

**为什么是 2 颗**：默认测试台 `TbNumBanks=8` 信号较多不易观察；缩到 2 颗后 bank 选择位只有 1 位，「跨 bank」即「相邻读地址落到不同宏」，现象最干净。

**配置推导**（请自行用 [src/axi_to_mem_banked.sv:96-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L96-L105) 的公式核对）：
- 要让 `MemNumBanks=2` 满足断言 `MemNumBanks >= 2*BanksPerAxiChannel`，需 `BanksPerAxiChannel <= 1`，即 `AxiDataWidth == MemDataWidth`。
- 取 `TbAxiDataWidth=64, TbMemDataWidth=64, TbNumBanks=2`：则 `BanksPerAxiChannel=1`、`BankSelOffset=$clog2(64/8)=3`、`BankSelWidth=idx_width(2)=1`。
- 于是 bank 选择位 = 字节地址的 `bit[3]`。两个相邻 64 位（8 字节）字地址 `0x00` 与 `0x08` 在 `bit[3]` 上分别为 0 和 1 → **必然落到不同 bank**。

**操作步骤**：
1. 复制 `test/tb_axi_to_mem_banked.sv` 为一份本地实验台（**不要改原文件**），或直接在 elaboration 时用参数覆盖：`vsim -gTbAxiDataWidth=64 -gTbMemDataWidth=64 -gTbNumBanks=2 ...`（具体命令语法以本地 EDA 工具为准，**待本地验证**）。
2. 仿照 4.3.4，在 monitor 的 `if (mem_req[i])` 分支加一行打印：`$display("%0t bank %0d addr %0h", $time(), i, mem_addr[i]);`。
3. 把激励改成定向：让 `axi_rand_master` 只发**连续递增地址**的读（或临时把 `TbNumReads` 调小、`TbNumWrites=0`），从 `StartAddr` 起步长 8 字节连读若干笔。
4. 用 u1-l4 的流程跑仿真：`make sim-axi_to_mem_banked.log`。

**需要观察的现象**：
- 打印应出现 `bank 0 addr ...` 与 `bank 1 addr ...` **交替**——第一笔（地址 `bit[3]=0`）进 bank 0、第二笔（`bit[3]=1`）进 bank 1，依此类推。
- `axi_scoreboard`（[test/tb_axi_to_mem_banked.sv:403-416](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L403-L416)）应在日志里报 `Errors: 0`，说明数据虽被拆到两 bank，读回仍正确。

**预期结果**：地址被按 `bit[3]` 交替分发到两颗 SRAM，功能与单颗宽 SRAM 等价（scoreboard 通过）。具体波形与耗时**待本地验证**。

**反思题**：如果把步长从 8 字节改成 16 字节，相邻两笔还会落到不同 bank 吗？（提示：16 字节 = `0x10`，`bit[3]=0` 不变，`bit[4]` 才翻转，而 `bit[4]` 不在 bank 选择位里 → 两笔会落到**同一个** bank。这能帮你确认「bank 选择只看 `BankSelOffset` 起的 `BankSelWidth` 位」。）

## 6. 本讲小结

- `axi_to_mem` 族的统一对外接口是 **req/gnt/rvalid 存储流**；最关键的契约是「**每个请求（含写）都必须回一个 `rvalid`**」，下游 SRAM 模型需配合产生。
- 真正的协议逻辑全在 **`axi_to_detailed_mem`**（Level 2）：它用「AR/AW → meta → 读写 mux 仲裁 → fork → 按车道拆 bank → join → 动态分叉到 B/R」一条流式管线完成翻译，且**不拆突发**（强制 INCR）。
- `axi_to_mem`（Level 3）只是 detailed_mem 的精简外壳：钉 `UserWidth=1`、悬空边带、`err/exokay` 接地。
- 三种变体（均 Level 4）针对内核「读写各 50%、bank 数受限于一个宽字」做增强：**banked**（读写分流 + 多宏地址路由）、**interleaved**（每 bank 读写仲裁、读旁路写）、**split**（读写各占一半端口、需双口宏）。
- 选型看三件事：是否需要读写并行、存储容量是否大于一个宽字、SRAM 是否双口；需要错误码等全边带时换用 detailed_mem。
- 本族不拆突发，长/FIXED/WRAP 突发需前级接 `axi_burst_splitter`（u9-l1）。

## 7. 下一步学习建议

- **u14-l2（axi_from_mem / lite_from_mem）**：本族是「AXI 主端 → SRAM 从端」，下一讲讲反方向「SRAM-like 接口作为发起方 → 产生 AXI 请求」，二者在 DMA 搬运场景里成对出现。
- **u14-l3（zero_mem / lfsr / err_slv）**：本讲的实践需要 SRAM 模型，下一讲会讲库自带的几种现成端点（包括读回 0/伪随机/恒错误），可作为更轻量的下游。
- **延伸阅读**：想看清「按车道拆 bank」的细节，精读 [src/axi_to_detailed_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv) 末尾的子模块 `mem_stream_to_banks_detailed`（`align_addr`、零选通隐藏 `HideStrb`、响应 FIFO）；想看清「地址路由」则精读 banked 版的 `stream_xbar` 接线与 `shift_reg` 响应回送。
