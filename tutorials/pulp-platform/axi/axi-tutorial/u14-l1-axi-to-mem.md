# axi_to_mem 及其变体

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 AXI4+ATOP 五通道与「简单存储接口」（`req/gnt/rvalid`）之间的鸿沟，以及 `axi_to_mem` 家族如何跨越它。
- 读懂真正的转换引擎 `axi_to_detailed_mem`：它如何把 AXI 突发拆成逐拍存储请求、如何在读写之间仲裁、如何把单条宽请求分发到多个 bank、又如何把多 bank 的响应拼回成 AXI 的 B/R。
- 理解 `axi_to_mem` 只是 `axi_to_detailed_mem` 的一个「砍端口」精简外壳，而不是另一套实现。
- 区分三个上层变体 `axi_to_mem_banked` / `axi_to_mem_interleaved` / `axi_to_mem_split` 各自面向的 SRAM 拓扑，以及它们如何用「读写拆分 + 多 bank」换取吞吐。
- 能根据手头 SRAM 的数量、端口数与位宽，选择正确的变体并配置参数。

本讲承接 **u9-l1（axi_burst_splitter）**：本族模块**不自己拆突发**，而是用断言强制要求 `len>0` 时必须是 `BURST_INCR`，因此当下游 SRAM 吃不下长突发时，需要把 `axi_burst_splitter` 串在前级。

## 2. 前置知识

本讲默认你已经掌握：

- **AXI4 五通道与握手**（见 u1-l3、u2-l3）：AW/W/B 写通路、AR/R 读通路，`valid && ready` 才算一次握手。本族模块的 slave 端就是标准 AXI4+ATOP。
- **结构体 `req_t`/`resp_t` 与 typedef/assign 宏**（见 u2-l4）：内核只认 `axi_req_t`/`axi_resp_t`，接口外壳 `_intf` 用宏在 `AXI_BUS` 与结构体之间搬运。
- **突发参数 `len`/`size`/`burst` 与地址推进函数**（见 u1-l3、u2-l2）：`num_bytes(size)` 返回每拍 \(2^{\text{size}}\) 字节，`aligned_addr` 把地址清低 `size` 位；`len = 拍数-1`，INCR 每拍地址加 `2^size`。
- **`axi_demux` / `axi_demux_simple`**（见 u5-l1、u5-l2）：按外部喂入的 `select` 把一个 slave 端口路由到多个 master 端口，并用 ID 计数器保证同 ID 保序；`UniqueIds=1` 在「同方向在途 ID 唯一」时可省掉计数器。
- **流式握手原语**（来自外部 `common_cells`）：`stream_mux`、`stream_fork`、`stream_fork_dynamic`、`stream_join`、`rr_arb_tree`、`stream_xbar`、`stream_fifo`/`fifo_v3`/`shift_reg`。它们都是 valid/ready 握手、与 AXI 通道握手同构，本族大量复用。

两个本讲要用到的新术语：

- **存储接口（memory interface）**：不是 AXI，而是一组扁平的 `req/gnt`（请求/许可）加 `rvalid/rdata`（响应有效/读数据）信号，每个 bank 一组。它更接近真实 SRAM 宏的端口。
- **bank（存储体/宏）**：一块独立的 SRAM 宏。把一片大存储拆成多个 bank，可以让不同地址的访问并行落进不同宏，从而提升带宽。

## 3. 本讲源码地图

| 文件 | 编译层级 | 作用 |
| --- | --- | --- |
| `src/axi_to_detailed_mem.sv` | Level 2 | **真正的引擎**：AXI4+ATOP → 多 bank 存储接口，含 id/user/cache/prot/qos/region/lock/err/exokay 等完整 sideband。本讲下半部分还内嵌一个子模块 `mem_stream_to_banks_detailed`，负责把单条宽请求分发到多个 bank。 |
| `src/axi_to_mem.sv` | Level 3 | **精简外壳**：把 `axi_to_detailed_mem` 包一层，固定 `UserWidth=1`、把 `err/exokay` 接 `'0`，对外只暴露最常用的那组端口。 |
| `src/axi_to_mem_banked.sv` | Level 4 | **读写分体 + bank 交叉**：先用 demux 把读写拆开，各接一个 `axi_to_mem`，再用 `stream_xbar` 把请求分发到 `MemNumBanks` 个 bank；读写可同时占满带宽。 |
| `src/axi_to_mem_interleaved.sv` | Level 4 | **每 bank 读写仲裁**：读写共享同一组 bank，每个 bank 用一棵 `rr_arb_tree` 在读写之间逐拍仲裁，允许读「绕过」写。 |
| `src/axi_to_mem_split.sv` | Level 4 | **读写端口彻底分离**：读驱动一半存储端口、写驱动另一半，要求同一 bank 可从多个物理端口访问（即多端口 SRAM）。 |
| `test/tb_axi_to_mem_banked.sv` | 测试 | 用随机主端 + `tc_sram` bank 阵列 + scoreboard 自检 `axi_to_mem_banked`，并统计每 bank 占用率。 |

> 依赖层级一目了然：`axi_to_detailed_mem(L2) → axi_to_mem(L3) → banked/interleaved/split(L4)`。三种变体都**复用** `axi_to_mem` 作为子模块，差别只在「怎么把读写分流、怎么把请求分发到宏」。

## 4. 核心概念与源码讲解

### 4.1 存储侧接口契约：req / gnt / rvalid 时序

#### 4.1.1 概念说明

AXI4 是一套**双方向、多通道、可突发**的协议；而真实 SRAM 宏的端口要朴素得多：给一个地址、一个写使能、一拍数据，下一拍（或若干拍后）返回读数据。`axi_to_mem` 家族要做的第一件事，就是定义一套介于两者之间的「存储接口」，让下游只要实现这套接口就能挂上来。

这套接口对**每个 bank** 都有一组独立信号（以 `axi_to_mem` 为例）：

- **请求方向（模块 → bank）**：`mem_req_o`（请求有效，相当于 valid）、`mem_addr_o`（字节地址）、`mem_wdata_o`/`mem_strb_o`（写数据/字节使能）、`mem_we_o`（写使能）、`mem_atop_o`（原子操作编码）。
- **许可方向（bank → 模块）**：`mem_gnt_i`（本拍能否接受请求，相当于 ready）。
- **响应方向（bank → 模块）**：`mem_rvalid_i`（响应有效）、`mem_rdata_i`（读数据）。

> ⚠️ **最重要的契约**：**每一个被接受的请求（无论读或写）都必须由存储器返回恰好一个 `mem_rvalid_i` 脉冲**。写事务没有独立的「写应答」信号——写完成也靠 `rvalid` 表达。这就把读写统一成了一条简单的流式管道，但要求下游 SRAM 对写也要回一个完成信号（在测试台里通常用一段延迟寄存器造出来，见 4.1.4）。

#### 4.1.2 核心流程

一次「1 拍延迟」SRAM 上的读访问，存储接口时序如下：

```
周期 T   : mem_req_o=1, mem_gnt_i=1, mem_addr_o=A, mem_we_o=0   ← 请求被接受（req && gnt）
周期 T+1 : mem_rvalid_i=1, mem_rdata_i=D(A)                     ← 响应返回
```

关键点：

1. **请求-许可握手**：`mem_req_o && mem_gnt_i` 同拍成立才算一次成功的存储请求（与 AXI 的 `valid/ready` 同构，但只有单方向）。
2. **响应延迟 = `BufDepth`/`MemLatency`**：从请求被接受到 `mem_rvalid_i` 拉高的拍数，取决于 SRAM 自身延迟。模块用这个值设定内部缓冲深度（`BufDepth`），使「请求—响应」的配对不会错位。
3. **每拍最多一个 bank 请求（每 bank）**：模块每个周期最多向每个 bank 发一个请求；AXI 的一拍宽数据若横跨多个 bank，会在同一拍**同时**向多个 bank 发请求。

#### 4.1.3 源码精读

存储接口的端口定义在 `axi_to_mem` 的端口列表里，每个信号都是 `[NumBanks-1:0]` 宽的数组——「一套接口」实际是 `NumBanks` 份副本：

[src/axi_to_mem.sv:57-75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L57-L75) — 定义 `mem_req_o`/`mem_gnt_i`/`mem_addr_o`/`mem_wdata_o`/`mem_strb_o`/`mem_atop_o`/`mem_we_o`/`mem_rvalid_i`/`mem_rdata_i` 共 9 组 bank 维度的信号，外加 `busy_o`。

每 bank 的数据宽度由参数推导，注意是「除以 bank 数」：

[src/axi_to_mem.sv:43-45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L43-L45) — `mem_data_t = logic[DataWidth/NumBanks-1:0]`、`mem_strb_t = logic[DataWidth/NumBanks/8-1:0]`。一条 AXI 宽拍被等分成 `NumBanks` 段，每段对应一个 bank。

`BufDepth` 参数的注释明确说它应等于存储响应延迟：

[src/axi_to_mem.sv:34-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L34-L35) — 「Depth of memory response buffer. This should be equal to the memory response latency.」

#### 4.1.4 代码实践（源码阅读型）

**目标**：在测试台里看清这套接口如何被一个真实 SRAM 模型驱动，尤其是「写也回 rvalid」如何被造出来。

**步骤**：

1. 打开 `test/tb_axi_to_mem_banked.sv`，定位到 `gen_tc_sram` 生成块。
2. 阅读它如何把 `mem_req[i]`/`mem_we[i]`/`mem_addr[i]`/`mem_wdata[i]`/`mem_strb[i]` 喂给 `tc_sram`，并恒置 `mem_gnt[i]=1'b1`（即 bank 永远可接受请求）。
3. 阅读它如何用一段 `TbMemLatency` 级移位寄存器把 `mem_req[i]` 延迟成 `mem_rvalid[i]`——这同时服务读和写，正是「写也回 rvalid」的实现。

**应观察的现象**：`mem_gnt` 恒为 1；`mem_rvalid` 比对应的 `mem_req` 晚 `TbMemLatency` 拍出现；写请求和读请求一样会触发 `mem_rvalid`。

**预期结果**：见 [test/tb_axi_to_mem_banked.sv:142-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L142-L177)。`mem_rvalid[i] = mem_lat_q[0]`，而 `mem_lat_q` 是 `mem_req[i]` 的 `TbMemLatency` 级延迟线——这正是 4.1.2 中「响应延迟 = SRAM 延迟」的实现。

#### 4.1.5 小练习与答案

**练习 1**：为什么写事务也要求 bank 回一个 `mem_rvalid_i`？

**参考答案**：模块内部用 `mem_rvalid_i` 来推进「在途请求计数器」与 AXI 的 B 通道（写响应）。写没有读数据，但仍需一个「完成」脉冲，否则模块无法知道何时可以回 B、何时可以释放缓冲槽，整条流水线会卡死。

**练习 2**：若把 `mem_gnt_i` 恒置 1，但 `mem_rvalid_i` 永远不回，会发生什么？

**参考答案**：请求会被无限接受（`gnt` 永远许可），但模块的 outstanding 计数器只增不减，最终触达 `BufDepth`/`MaxTrans` 上限后反压上游 AXI，整个通路停滞。这与 u7-l3 中 `axi_throttle` 的信用机制是同一类「靠计数器限并发」的思想。

---

### 4.2 核心引擎：从 axi_to_detailed_mem 到 axi_to_mem

#### 4.2.1 概念说明

`axi_to_detailed_mem` 是整个家族的**心脏**，也是这一族里**唯一真正写了 AXI 协议逻辑**的模块（另外四个要么是它的外壳，要么用它做零件）。它要做四件事：

1. **拆突发为逐拍**：AXI 一个 `len=N` 的突发有 \(N+1\) 拍，SRAM 一次只能吃一拍地址，因此要把突发展开成逐拍递增的存储请求。
2. **读写仲裁**：读（AR/R）与写（AW/W/B）共享同一条通往存储的路径，每拍只能服务其一。
3. **分发到 bank**：一拍 AXI 宽数据按数据车道等分成 `NumBanks` 段，要同时发给对应的若干 bank。
4. **拼装响应**：把各 bank 返回的数据/错误聚合成 AXI 的 R（每拍一个）和 B（每突发一个）。

`axi_to_mem` 只是把上述引擎包一层：把 `UserWidth` 钉成 1、把存储侧的 `err/exokay` 输入接 `'0`，并隐藏掉 `id/user/cache/prot/qos/region/lock` 这些「完整 sideband」端口，只留最常用的那几根。

模块头部有一句至关重要的设计提示：

[src/axi_to_detailed_mem.sv:16-18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L16-L18) — 「If both read and write channels of the AXI4+ATOP are active, both will have an utilization of 50%。」

这句话是后面三个变体存在的根本理由：**基础引擎只有一条存储路径，读写对半分带宽**。想要读写各占满带宽，就得用 banked/interleaved/split 引入并行。

#### 4.2.2 核心流程

引擎的数据流可以画成一条单向流水线，一拍 AXI 请求从左走到右：

```
AR/AW (含 W)
   │  ① 把 AR/AW 展开成逐拍 meta_t（addr/id/size/last/write/qos...）
   ▼
stream_mux ② 读写二选一仲裁
   │        优先级：QoS 高者 → 单拍写优先于读突发 → 进行中突发优先 → 否则轮询
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

读写各自的地址推进与「剩余拍数」由计数器 `r_cnt_q`/`w_cnt_q` 维护：新 AR/AW 握手时把 `len` 装入计数器，之后每拍把地址加上 `axi_pkg::num_bytes(size)`、计数器减一，直到归零置 `last`。地址推进步长为：

\[
\text{step} = 2^{\text{size}} \quad\text{（即每拍字节数）}
\]

读写仲裁的五条优先级策略（QoS 感知）：

1. 若只有一侧有效，选它。
2. 若两侧都有效，**QoS 高者先**。
3. QoS 相同时，**单拍写优先于读突发**（理由：AXI 读突发可被交错，写突发不能，所以赶紧把单拍写送走）。
4. 否则优先**正在进行的突发**（避免停顿半截的突发，省缓冲）。
5. 都不适用时**轮询**，防饿死。

#### 4.2.3 源码精读

**（a）`axi_to_mem` 是薄外壳。** 整个模块就是一次例化：

[src/axi_to_mem.sv:78-113](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L78-L113) — 例化 `axi_to_detailed_mem`，把 `UserWidth` 固定为 1（L84），把 `mem_lock_o/mem_id_o/mem_user_o/mem_cache_o/mem_prot_o/mem_qos_o/mem_region_o` 全部悬空（L101-108），`mem_err_i`/`mem_exokay_i` 接 `'0`（L111-112）。这就是「砍端口」的全部秘密——`axi_to_mem` 不写任何协议逻辑。

**（b）读写各自的 meta 生成与突发推进。** 读通路用 `r_cnt_q` 计数剩余拍数：

[src/axi_to_detailed_mem.sv:171-213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L171-L213) — 若 `r_cnt_q>0`，说明读突发进行中，地址每拍加 `axi_pkg::num_bytes(size)`（L182）并递减计数；否则尝试接一个新的 AR，握手后把 `ar.len` 装入 `r_cnt_q`（L209）。写通路 [src/axi_to_detailed_mem.sv:216-264](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L216-L264) 结构对称，区别是写要同时等 `aw_valid && w_valid` 才能前进（L239）。

**（c）QoS 感知的读写仲裁。**

[src/axi_to_detailed_mem.sv:267-321](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L267-L321) — `stream_mux`（L267-278）按 `meta_sel_d` 在读写间二选一；`always_comb`（L279-321）实现 4.2.2 的五条优先级规则，并在「有效但未就绪」时锁存选择（`sel_lock_q`，L317-319），以满足 AXI「valid 期间载荷稳定」的铁律。

**（d）一条宽请求分发到多 bank。** 引擎把这件事委托给内嵌子模块 `mem_stream_to_banks_detailed`：

[src/axi_to_detailed_mem.sv:436-469](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L436-L469) — 例化 `mem_stream_to_banks_detailed`，把单条 `m2s_req`（宽拍）拆成各 bank 的 `bank_req_o/bank_addr_o/bank_wdata_o/...`，并回收各 bank 的 `bank_rvalid_i/bank_rdata_i`。

子模块内部**按数据车道（position）**把宽拍切成段，第 `i` 个 bank 拿到宽字里的第 `i` 段字节，并维护一个在途计数器限并发：

[src/axi_to_detailed_mem.sv:872-877](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L872-L877) — 每个 bank 的请求地址由 `align_addr(addr_i) + i*BytesPerBank` 算出，写数据/strb 则从宽拍的对应切片取（`wdata_i[i*BitsPerBank+:BitsPerBank]`）。即一次宽字访问会**同时**打到所有 `NumBanks` 个 bank，这是「用多个窄宏拼一个宽字」的做法。

[src/axi_to_detailed_mem.sv:846-868](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L846-L868) — 在途计数器 `cnt_q`：每次 `req_i&&gnt_o` 加 1、每次 `rvalid_o&&rready_i` 减 1；`cnt_req_ready = (cnt_q < MaxTrans) | (rvalid_o & rready_i)` 决定能否再发——这正是「缓冲深度等于响应延迟」的量化保证。

**（e）响应拼装与错误聚合。** 存储响应先与 `meta_buf` 汇合，再按 `sel_buf` 动态分发到 B 或 R：

[src/axi_to_detailed_mem.sv:335-336](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L335-L336) — `sel_b = meta.write & meta.last`（仅写突发的末拍产生 B），`sel_r = ~meta.write | meta.atop[5]`（每拍读都产生 R；`atop[5]` 即 `ATOP_R_RESP`，原子写也要产生 R，呼应 u2-l1 与 u15-l1）。

[src/axi_to_detailed_mem.sv:483-495](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L483-L495) — `stream_fork_dynamic` 按 `{sel_buf_b, sel_buf_r}` 把同一个存储响应动态送到 B 和/或 R 通道。

错误聚合按「写看 strb、读看 size+地址」分别筛选活跃 bank，`err` 用或、`exokay` 用与（最新提交 e55ae2a7 刚给「每 bank 读错误掩码」补了括号）：

[src/axi_to_detailed_mem.sv:511-514](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L511-L514) — `resp_b_err = |(m2s_resp.err & meta_buf_bank_strb)`、`resp_r_err = |(m2s_resp.err & meta_buf_size_enable)`，只统计真正活跃的 bank。写突发还把多拍错误累积进单个 B：

[src/axi_to_detailed_mem.sv:516-539](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L516-L539) — `collect_b_err_q` 在写突发中间拍不断累积，末拍写完后清零，最终合成一个 B 响应。

**（f）不拆突发的硬约束。** 内核用断言强制多拍突发必须是 INCR：

[src/axi_to_detailed_mem.sv:587-592](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L587-L592) — `ar.len > 0 |->
ar.burst == BURST_INCR`，否则报错「Non-incrementing bursts are not supported!」。这正是本讲依赖 u9-l1 的原因：要喂 FIXED/WRAP 或超长突发，先用 `axi_burst_splitter`/`axi_burst_unwrap` 预处理。

#### 4.2.4 代码实践（源码阅读型）

**目标**：不跑仿真，靠读源码把一笔「长度 4 的 INCR 读」在内核里走一遍，并验证「读写同时压满时各占 50%」的根源。

**步骤**：

1. 在 [src/axi_to_detailed_mem.sv:171-213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L171-L213) 确认：第一拍 AR 握手时 `r_cnt_d` 被赋成 `ar.len`（=3），`last` 为假。
2. 在同一段「`r_cnt_q > 0`」分支确认：第 2~4 拍地址每拍加 `num_bytes(size)`，`r_cnt_q` 递减，只有当 `r_cnt_q == 8'd1` 时 `last` 才拉高。
3. 在 [src/axi_to_detailed_mem.sv:267-278](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_detailed_mem.sv#L267-L278) 确认通往存储的路径只有一棵 `stream_mux`（2 选 1）。自问：如果上游同时有长读突发和长写突发，每拍只能有一个 meta 被选通送往存储，读写带宽之和是否被这「单条物理路径」卡死？

**预期结论**：4 笔存储请求打到 bank、产生 4 个 `rvalid`，最终 AXI 侧收到 4 拍 R，只有最后一拍 `r_last=1`、全程没有 B。同时，「单条 mux 路径」正是读写各 50% 的根源，是后续 4.3、4.4 用「拆读写到不同 bank/端口」来突破的瓶颈。（纯源码阅读型实践，行为结论由源码逻辑直接得出。）

#### 4.2.5 小练习与答案

**练习 1**：`axi_to_mem`（Level 3）比 `axi_to_detailed_mem`（Level 2）高一级，却代码更短，为什么？

**参考答案**：因为 `axi_to_mem` 不实现任何协议逻辑，只例化 `axi_to_detailed_mem` 并裁剪端口（固定 `UserWidth=1`、`err/exokay` 接 0）。它依赖后者，故层级更高；代码更短是因为它只是「插座」。

**练习 2**：`mem_stream_to_banks_detailed` 里的 `cnt_req_ready` 为什么要有 `| (rvalid_o & rready_i)` 这一项？

**参考答案**：它允许「本拍正好有一个响应被消费」时，即使计数器已到 `MaxTrans` 上限也能再发一个新请求，避免「响应回了却还要空一拍才能发下一请求」的吞吐损失——本质上是用同拍的「一进一出」抵消计数。

**练习 3**：`sel_r = ~meta.write | meta.atop[5]` 里 `atop[5]` 的作用？

**参考答案**：`atop[5]` 是 `ATOP_R_RESP`（u2-l1），表示这笔原子写除了 B 还要产生 R 响应；此时即便 `meta.write=1` 也要让响应对走 R 通道。

---

### 4.3 axi_to_mem_banked：读写分体 + bank 交叉

#### 4.3.1 概念说明

`axi_to_mem_banked` 专门解决基础引擎的两个局限：

1. **「读写各 50%」**：它用 `axi_demux` 把读写**静态分流**到两个独立的 `axi_to_mem`，于是读写可以**并行**进入各自的存储流水线，各占满带宽。
2. **「bank 数只够拼一个宽字」**：基础引擎的 `NumBanks` 只够拼出一个宽字。banked 版支持 `MemNumBanks` **多于** `BanksPerAxiChannel`（即存储总容量大于一个字），靠 `stream_xbar` 按**地址位**把请求路由到正确的宏。

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

两个关键派生参数：

- 每个字宽 `MemDataWidth`，占 \( \text{MemDataWidth}/8 \) 字节，故字节地址里低位 \( \text{BankSelOffset}=\$clog2(\text{MemDataWidth}/8) \) 之下是「字内字节」。
- bank 选择位就在其上，宽度 \( \text{BankSelWidth}=\text{idx\_width}(\text{MemNumBanks}) \)：

\[
\text{bank} = \text{addr}[\,\text{BankSelOffset}+\text{BankSelWidth}-1 :\ \text{BankSelOffset}\,]
\]

去掉 bank 选择位后剩下的才是送给 SRAM 的字地址。这就是「跨 bank」的本质——地址在这几位上的取值决定请求落到哪颗宏。

#### 4.3.3 源码精读

**（a）关键派生参数。**

[src/axi_to_mem_banked.sv:96-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L96-L105) — `BanksPerAxiChannel = AxiDataWidth/MemDataWidth`、`BankSelOffset = $clog2(MemDataWidth/8)`、`BankSelWidth = cf_math_pkg::idx_width(MemNumBanks)`。

**（b）静态拆分读写。**

[src/axi_to_mem_banked.sv:140-169](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L140-L169) — 例化 `axi_demux`（`NoMstPorts=2`），`slv_ar_select_i=ReadAccess`、`slv_aw_select_i=WriteAccess`；因选择为常量，置 `UniqueIds=1'b1` 省硬件（原理见 u5-l2），并开五通道 spill。

**（c）两侧各一个 `axi_to_mem` + bank 选择 + 响应移位寄存器。**

[src/axi_to_mem_banked.sv:176-253](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L176-L253) — 每个 `axi_to_mem` 的 `NumBanks=BanksPerAxiChannel`（L192）；`inter_sel[i][j] = req_addr[j][BankSelOffset+:BankSelWidth]`（L215）取 bank 号；送给 SRAM 的字地址是 `req_addr[j][(BankSelOffset+BankSelWidth)+:MemAddrWidth]`（L220）；`shift_reg#Depth(MemLatency)`（L243-251）把「读选择」延迟后用于 `res_rdata[j] = mem_rdata_i[r_shift_oup.sel]`（L240）。

注意 grant 的翻译：`mem_gnt_i = inter_ready & inter_valid`（L203），把 xbar 的 `valid/ready` 翻译成存储接口的 `req/gnt` 契约。读响应回来的时机是 `MemLatency` 拍后，所以用 `shift_reg` 把「请求时的 bank 选择位」延时同样拍数再做多路选择——这正是内核要求的「写也回 rvalid」由 SRAM 模型配合产生。

**（d）stream_xbar 路由到物理 bank。**

[src/axi_to_mem_banked.sv:258-279](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L258-L279) — `stream_xbar` 的 `NumInp = 2*BanksPerAxiChannel`、`NumOut = MemNumBanks`，按 `inter_sel` 把每路子请求送到目标 bank。

**（e）对拓扑的硬约束。**

[src/axi_to_mem_banked.sv:296-303](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L296-L303) — 强制 `MemNumBanks >= 2*AxiDataWidth/MemDataWidth`、`MemNumBanks` 为 2 的幂、`AxiDataWidth%MemDataWidth==0`。

#### 4.3.4 代码实践

见本讲第 5 节「综合实践」——它正是围绕 `axi_to_mem_banked` 的两 bank 跨 bank 读设计。

#### 4.3.5 小练习与答案

**练习 1**：为什么 banked 的 demux 可以安全地设 `UniqueIds=1'b1`，而通用 `axi_demux` 默认不行？

**参考答案**：banked 的读写拆分是**静态常量**（AR 恒走读端口、AW 恒走写端口），同一方向的事务永远去同一端口，天然满足 u5-l2 中 `UniqueIds` 的前提（同方向在途 ID 唯一 / 同 ID 必去同端口），故可删掉整组 ID 计数器省面积。

**练习 2**：若 `AxiDataWidth=64`、`MemDataWidth=32`，最少需要几个 bank？

**参考答案**：`BanksPerAxiChannel = 64/32 = 2`，约束要求 `MemNumBanks >= 2*2 = 4`，且为 2 的幂，故最少 **4** 个 bank。

**练习 3**：为什么 banked 要求 `MemNumBanks >= 2*BanksPerAxiChannel`，而不是 `>= BanksPerAxiChannel`？

**参考答案**：因为读写各用一组宏（各自一个 `axi_to_mem`），最起码要 2 倍才能让读写在物理上互不干扰地并行；多出来的宏则用来扩充存储容量。

---

### 4.4 axi_to_mem_interleaved 与 axi_to_mem_split：读写分离的两条路线

#### 4.4.1 概念说明

这两个变体都把读写**拆给两个 `axi_to_mem`**，区别在于「读写两侧如何共享物理 bank」：

- **`axi_to_mem_interleaved`（每 bank 仲裁）**：读写两侧共享**同一组** `NumBanks` 个 bank。每个 bank 前面挂一棵 2 输入 `rr_arb_tree`，在「来自读路的请求」与「来自写路的请求」之间逐拍仲裁。好处是 bank 数可以等于 `NumBanks`（不必翻倍），且**允许读在写拥塞时「绕过」写**插入执行（模块注释：「Allows reads to bypass writes」）；代价是同一 bank 同拍仍只能服务一侧，且要额外存「这一笔是读还是写」以便把响应送回正确的那一侧。
- **`axi_to_mem_split`（端口分离）**：读路独占一半物理端口、写路独占另一半，**完全不相争**。它要求 SRAM 是「多端口」的——同一 bank 的同一地址必须能从读端口和写端口分别访问（典型如双端口 SRAM）。端口总数 `NumMemPorts = 2*AxiDataWidth/MemDataWidth`。

一句话区分：interleaved 是「共享 bank、按拍仲裁」，split 是「独占端口、互不干涉」。

#### 4.4.2 核心流程

**interleaved** 的每个 bank 内部：

```
读路 axi_to_mem 的 bank[i] 请求 ─┐
                                  ├─ rr_arb_tree(2) ─► 物理 bank[i]
写路 axi_to_mem 的 bank[i] 请求 ─┘
        ▲                                                │
        │ 仲裁结果押入 fifo_v3，rvalid 时弹出            │
        └──────────── 用弹出值把 rvalid/rdata 路由回读路或写路 ◄─┘
```

因为读写共享同一物理 bank 的响应线 `mem_rvalid_i[i]/mem_rdata_i[i]`，必须用一个 FIFO 记录「每个在途请求当初是读还是写」，响应返回时按队头还原。

**split** 则简单直接：读路 `axi_to_mem` 的 `NumMemPorts/2` 个输出直接连到物理端口 `[NumMemPorts/2-1:0]`，写路连到 `[NumMemPorts-1:NumMemPorts/2]`。读写各自是完整的 `axi_to_mem`，互不知道对方存在。

#### 4.4.3 源码精读

**interleaved：拆读写 + 每 bank 仲裁 + 响应回放。**

[src/axi_to_mem_interleaved.sv:104-123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L104-L123) — 用 `axi_demux_simple` 静态拆分读写（`slv_ar_select_i=1'b0`、`slv_aw_select_i=1'b1`），同样设 `UniqueIds=1'b1`。

[src/axi_to_mem_interleaved.sv:125-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L125-L177) — 两个 `axi_to_mem`，各 `NumBanks` 宽，分别服务读路与写路。

[src/axi_to_mem_interleaved.sv:224-257](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L224-L257) — 每个 bank 一棵 `rr_arb_tree`（`NumIn=2`）仲裁读写，`idx_o` 输出本轮赢家是读(0)还是写(1)；`fifo_v3`（深度 `BufDepth+1`）押入 `arb_outcome[i]`，在 `mem_rvalid_i[i]` 时弹出 `arb_outcome_head[i]`；再由 `w_mem_rvalid = mem_rvalid & !arb_outcome_head`、`r_mem_rvalid = mem_rvalid & arb_outcome_head`（[L214-215](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_interleaved.sv#L214-L215)）把响应分别送回写路或读路。

**split：拆读写 + 各自独占一半端口。**

[src/axi_to_mem_split.sv:16-18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L16-L18) — 模块头部明确前提：「This can only be used when addresses for the same bank are accessible from different memory ports.」（同一 bank 的地址要能从不同端口访问，即多端口 SRAM。）

[src/axi_to_mem_split.sv:88-107](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L88-L107) — 同样用 `axi_demux_simple` 静态拆分读写。

[src/axi_to_mem_split.sv:41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L41) — 端口数推导：`NumMemPorts = 2*AxiDataWidth/MemDataWidth`，读写各占一半。

[src/axi_to_mem_split.sv:111-163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_split.sv#L111-L163) — 读路 `axi_to_mem`（`NumBanks=NumMemPorts/2`）驱动低端口的 `[NumMemPorts/2-1:0]` 切片（L127-135），写路驱动高端口的 `[NumMemPorts-1:NumMemPorts/2]` 切片（L154-162）。除此之外没有仲裁、没有路由 xbar——结构最直白。注意读路的 `HideStrb` 写死为 `1'b0`（L119，读不写数据 strb 无意义），写路才用传入的 `HideStrb`（L146）。

#### 4.4.4 代码实践（源码阅读 + 对比型）

**目标**：用一张表把三个变体的「读写如何共享 bank」彻底分清。

**步骤**：

1. 在 `axi_to_mem_banked.sv` 找 `stream_xbar`（共享 bank，但读写各自有独立 `axi_to_mem` 路径，靠 bank 数翻倍避免冲突）。
2. 在 `axi_to_mem_interleaved.sv` 找 `rr_arb_tree`（共享 bank，靠逐拍仲裁）。
3. 在 `axi_to_mem_split.sv` 找端口切片 `[NumMemPorts/2-1:0]` 与 `[NumMemPorts-1:NumMemPorts/2]`（独占端口）。

**预期结论**：填出下表（见 4.4.5 答案）。

#### 4.4.5 小练习与答案

**练习 1**：完成下表的「读写共享方式」与「所需 SRAM 特性」两列。

| 变体 | 读写共享方式 | 所需 SRAM 特性 |
| --- | --- | --- |
| `axi_to_mem_banked` | ? | ? |
| `axi_to_mem_interleaved` | ? | ? |
| `axi_to_mem_split` | ? | ? |

**参考答案**：

| 变体 | 读写共享方式 | 所需 SRAM 特性 |
| --- | --- | --- |
| `axi_to_mem_banked` | 读写各一条 `axi_to_mem`，经 `stream_xbar` 落到 `MemNumBanks>=2*BanksPerAxiChannel` 个共享 bank | 单端口 SRAM 即可，但 bank 数要够多（≥2 倍 `BanksPerAxiChannel`，且为 2 的幂） |
| `axi_to_mem_interleaved` | 读写共享 `NumBanks` 个 bank，每 bank 一棵 `rr_arb_tree` 逐拍仲裁 | 单端口 SRAM；bank 数不必翻倍，读可在写拥塞时插入 |
| `axi_to_mem_split` | 读写各独占一半物理端口，互不相争 | **多端口** SRAM（同一地址可从不同端口访问） |

**练习 2**：interleaved 为什么必须用 FIFO 存「仲裁胜出方」，而 split 不需要？

**参考答案**：interleaved 的读写共享同一物理 bank 的 `mem_rvalid_i/mem_rdata_i`，响应返回时无从直接判断它属于读还是写，必须按请求顺序回放当初的仲裁结果。split 的读写走完全不同的物理端口，响应天然从各自的端口回来，无需额外记录。

**练习 3**：split 版为什么不能用在普通单口 SRAM 上？

**参考答案**：单口 SRAM 同一地址同一时刻只能一笔访问；split 把同一 bank 地址既暴露给读端口又暴露给写端口，若两者同时访问同地址，单口宏无法服务。需要双口宏或读/写端口分离的存储器。

---

### 4.5 变体选型总览

把基础版与三个变体放在一起对比：

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
- 需要全量边带（错误码、EXOKAY、user 等）时，把上述里的 `axi_to_mem` 换成 `axi_to_detailed_mem`。

## 5. 综合实践

**任务**：用 `axi_to_mem_banked` 把 AXI 主端接到 **2 颗** SRAM 模型上，发起**跨 bank 的连续读**，验证地址被正确分发到两个 bank。

**目标**：把 4.1 的接口契约、4.3 的 bank 映射规则、本家族的验证范式串成一条线。

**为什么是 2 颗**：默认测试台 `TbNumBanks=8` 信号较多不易观察；缩到 2 颗后 bank 选择位只有 1 位，「跨 bank」即「相邻读地址落到不同宏」，现象最干净。

**配置推导**（请先自行用 [src/axi_to_mem_banked.sv:96-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L96-L105) 的公式与 [src/axi_to_mem_banked.sv:296-303](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem_banked.sv#L296-L303) 的断言核对约束）：

- 要让 `MemNumBanks=2` 满足断言 `MemNumBanks >= 2*BanksPerAxiChannel`，需 `BanksPerAxiChannel <= 1`，即 `AxiDataWidth == MemDataWidth`。
- 取 `TbAxiDataWidth=64`、`TbMemDataWidth=64`、`TbNumBanks=2`：则 `BanksPerAxiChannel=1`、`BankSelOffset=$clog2(64/8)=3`、`BankSelWidth=idx_width(2)=1`。
- 校验：`MemNumBanks(2) >= 2*1 = 2` ✓；`MemNumBanks` 为 2 的幂 ✓；`AxiDataWidth % MemDataWidth == 0` ✓。
- 故 bank 号 = 字节地址的 `bit[3]`。两个相邻 64 位（8 字节）字地址 `0x00` 与 `0x08` 在 `bit[3]` 上分别为 0 和 1 → **必然落到不同 bank**。

**操作步骤**：

1. 复制 `test/tb_axi_to_mem_banked.sv` 为一份本地实验台（**不要改原文件**），或在 elaboration 时用参数覆盖：`vsim -gTbAxiDataWidth=64 -gTbMemDataWidth=64 -gTbNumBanks=2 ...`（具体命令语法以本地 EDA 工具为准，**待本地验证**）。
2. 仿照 4.1.4，在 monitor 进程 [test/tb_axi_to_mem_banked.sv:320-324](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L320-L324) 的 `if (mem_req[i])` 分支里临时加一行打印（**示例代码**，非项目原有）：

   ```systemverilog
   if (mem_req[i]) begin
     dut_busy_cnt[i]++;
     $display("%0t> bank %0d addr %0h we=%b", $time(), i, mem_addr[i], mem_we[i]); // 示例：观察分发
   end
   ```
3. 把激励改成定向：让 `axi_rand_master` 只发**连续递增地址**的读（或临时把 `TbNumReads` 调小、`TbNumWrites=0`），从 `StartAddr` 起步长 8 字节连读若干笔（地址 `0x0, 0x8, 0x10, 0x18, ...`）。
4. 按 u1-l4 的流程跑仿真：`make sim-tb_axi_to_mem_banked.log`（或 `scripts/run_vsim.sh`）。

**需要观察的现象**：

- 打印应出现 `bank 0 addr ...` 与 `bank 1 addr ...` **交替**——第一笔（地址 `bit[3]=0`）进 bank 0、第二笔（`bit[3]=1`）进 bank 1，依此类推。
- `axi_scoreboard`（[test/tb_axi_to_mem_banked.sv:403-416](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L403-L416)）应在日志里报 `Errors: 0`，说明数据虽被拆到两 bank，读回仍正确。

**预期结果**：地址被按 `bit[3]` 交替分发到两颗 SRAM，功能与单颗宽 SRAM 等价（scoreboard 通过）。具体波形与耗时**待本地验证**；上述地址映射结论是据 4.3.2 的公式与源码推导得到的，不依赖运行结果。

**反思题**：如果把步长从 8 字节改成 16 字节，相邻两笔还会落到不同 bank 吗？

> 提示：16 字节 = `0x10`，`bit[3]=0` 不变、`bit[4]` 才翻转，而 `bit[4]` 不在 bank 选择位（只有 `bit[3]`）里 → 两笔会落到**同一个** bank。这能帮你确认「bank 选择只看 `BankSelOffset` 起的 `BankSelWidth` 位」。

## 6. 本讲小结

- `axi_to_mem` 族的统一对外接口是 **req/gnt/rvalid 存储流**；最关键的契约是「**每个请求（含写）都必须回一个 `rvalid`**」，下游 SRAM 模型需配合产生。
- 真正的协议逻辑全在 **`axi_to_detailed_mem`**（Level 2）：它用「AR/AW → meta → 读写 mux 仲裁 → fork → 按车道拆 bank → join → 动态分叉到 B/R」一条流式管线完成翻译，且**不拆突发**（强制 INCR）。
- `axi_to_mem`（Level 3）只是 detailed_mem 的精简外壳：钉 `UserWidth=1`、悬空边带、`err/exokay` 接地。
- 三种变体（均 Level 4）针对内核「读写各 50%、bank 数受限于一个宽字」做增强：**banked**（读写分流 + 多宏地址路由）、**interleaved**（每 bank 读写仲裁、读旁路写）、**split**（读写各占一半端口、需双口宏）。
- 三个变体的 demux 都因「读写静态拆分」而可设 `UniqueIds=1'b1` 省掉 ID 计数器，这是 u5-l2 理论在真实模块里的直接应用。
- 本族不拆突发，长/FIXED/WRAP 突发需前级接 `axi_burst_splitter`（u9-l1）；选型看三件事：是否需要读写并行、存储容量是否大于一个宽字、SRAM 是否双口。

## 7. 下一步学习建议

- **存储端点族的另一侧（u14-l2）**：阅读 `src/axi_from_mem.sv` 与 `src/axi_lite_from_mem.sv`，它们是 `axi_to_mem` 的「反向」——让一个 SRAM-like 接口作为发起方产生 AXI 请求，常用于 DMA 搬运前端，与本族成对出现。
- **更简单的端点（u14-l3）**：阅读 `src/axi_zero_mem.sv`、`src/axi_lfsr.sv`、`src/axi_err_slv.sv`，它们是不需要真实 SRAM 的占位/激励/错误端点，适合快速搭测试拓扑。
- **协议视角回看**：本讲的 `mem_atop_o` 和 `atop[5]=ATOP_R_RESP` 将在 u15-l1（ATOPs 与 `axi_atop_filter`）系统展开，届时你会更理解为何存储接口也要把 `atop` 一路透传。
- **验证方法学**：本讲的 `tb_axi_to_mem_banked` 用到了 `axi_scoreboard` 与随机主端，其回归方法可在 u16-l1（定向随机验证方法学）中进一步学习。
- **延伸精读**：想看清「按车道拆 bank」的细节，精读 `src/axi_to_detailed_mem.sv` 末尾的子模块 `mem_stream_to_banks_detailed`（`align_addr`、零选通隐藏 `HideStrb`、响应 FIFO）；想看清「地址路由」则精读 banked 版的 `stream_xbar` 接线与 `shift_reg` 响应回送。
