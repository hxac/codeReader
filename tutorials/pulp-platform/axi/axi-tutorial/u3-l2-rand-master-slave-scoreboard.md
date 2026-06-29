# 随机主从、scoreboard 与 sim_mem

## 1. 本讲目标

本讲承接 u3-l1 的「底层逐拍 driver」，把验证组件再向上抬一层：从「亲手拍一拍」升级到「让程序自动生成成千上万笔随机事务并自动判错」。学完后你应当能够：

- 说清 `axi_rand_master` / `axi_rand_slave` / `axi_lite_rand_master` / `axi_lite_rand_slave` 四个随机组件各自的角色与可调参数；
- 理解 `axi_rand_master` 如何用「在途计数 + 信号量」同时约束最大并发、ID 合法性与突发不跨 4 KiB 边界；
- 用 `axi_sim_mem` 当作一个「写进去什么、读出来就是什么」的无限 AXI 从端，并解释它的 monitor 输出与可注入错误机制；
- 用 `axi_scoreboard` 在总线上旁路搭一个「黄金内存模型」，自动比对读写数据，理解它对 `8'hxx`（未初始化字节）的宽容匹配；
- 独立拼出一个 `rand_master → DUT → axi_sim_mem` 的最小自检测试台拓扑。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **AXI4 协议基础（u1-l3）**：五个通道、valid/ready 握手、`in flight`（在途，地址已握手而响应未握手）/`pending`（挂起，valid 高 ready 低）、突发类型与响应码。
- **axi_pkg 的工具函数（u2-l1/u2-l2）**：`num_bytes`、`beat_addr`、`beat_lower_byte`/`beat_upper_byte`、`aligned_addr`、`resp_precedence`、`get_arcache`/`get_awcache` 与 `mem_type_t`。本讲的随机组件大量复用这些函数来「合法地」生成事务。
- **接口与宏（u2-l3/u2-l4）**：`AXI_BUS`/`AXI_BUS_DV` 及其 modport、`AXI_LITE`/`AXI_LITE_DV`，以及 `AXI_ASSIGN` / `AXI_ASSIGN_MONITOR` 等互连宏。
- **底层 driver（u3-l1）**：`axi_driver` / `axi_lite_driver` 用虚接口绑定、`TA`/`TT` 时序、`send_*` / `recv_*` / `mon_*` 任务、以及承载 AXI4 信号的 beat 对象。本讲的随机组件**都是包了一层 driver 的高层封装**，driver 是它们的「手脚」。

一个贯穿全讲的关键词是 **directed random verification（定向随机验证）**：我们不是纯随机地乱发事务，而是给随机过程加上**合法的约束**（地址必须在某个区间、突发不能跨 4 KiB 页、并发不超过上限、ID 必须合法），让随机出来的每一笔事务都是协议合法的，再用一个参考模型自动比对结果。这种「受约束随机 + 自检」是本库所有 `tb_*.sv` 的共同骨架。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/axi_test.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv) | 验证组件大本营。本讲关注其中四个类：`axi_rand_master`、`axi_rand_slave`、`axi_lite_rand_master`、`axi_lite_rand_slave`，以及自检用的 `axi_scoreboard`。 |
| [src/axi_sim_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv) | 仿真专用的「无限 AXI 从端存储」，按字节建表、可注入读写错误、带 monitor 输出。 |
| [test/tb_axi_to_mem_banked.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv) | 综合实践范本：`rand_master → DUT` + `AXI_ASSIGN_MONITOR` + `axi_scoreboard` 的标准三件套接线。 |
| [test/tb_axi_sim_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_sim_mem.sv) | `axi_sim_mem` 的自测台，演示「写一段再读回比对」的最小闭环。 |

> 提示：`axi_test.sv` 是一个很大的文件（两千多行），本讲只摘其中五个类的关键片段，行号均对应当前 HEAD `e55ae2a7`。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `axi_rand_master`** —— 随机激励发生器（重点）；
- **4.2 `axi_rand_slave` 与 `axi_lite_rand_*`** —— 随机响应端；
- **4.3 `axi_sim_mem`** —— 无限 AXI 存储从端；
- **4.4 `axi_scoreboard`** —— 黄金模型自检。

### 4.1 axi_rand_master：随机激励发生器

#### 4.1.1 概念说明

`axi_rand_master` 是一个 **SystemVerilog 类**，实例化后挂在一条 `AXI_BUS_DV` 虚接口上，扮演一个「会自动产生合法 AXI 事务」的主端。你只要告诉它两件事——**发多少笔读、发多少笔写**（`run(n_reads, n_writes)`），它就会自动生成地址、长度、size、burst、ID、strobe 等全部字段，并把响应收完。

它和 u3-l1 的 `axi_driver` 是**包装关系**：`axi_rand_master` 内部持有一个 `axi_driver` 句柄 `drv`，所有真正的「把信号拍出去」都委托给 `drv.send_aw()` / `drv.send_w()` 等，自己只负责「**决定下一笔发什么、什么时候发**」。

它解决的核心问题是：**如何让随机过程始终产出协议合法的事务**。具体有三类约束必须同时满足：

1. **地址合法**：地址落在你声明的某个内存区间内，且整段突发不能跨 4 KiB 页（AXI 协议要求，否则下游行为未定义）。
2. **并发受控**：在途（in flight）事务数不超过你设的上限，避免压垮容量有限的下游或 DUT。
3. **ID 合法**：同 ID 同方向必须保序；如果开了 ATOP/UNIQUE_IDS，还有额外的 ID 唯一性约束。

#### 4.1.2 核心流程

`run(n_reads, n_writes)` 用一个 `fork ... join` 同时启动 **6 个并行进程**，读写各管各的：

```
run(n_reads, n_writes):
  fork
    send_ars(n_reads)   # 生成并发送 AR，受 MAX_READ_TXNS 反压
    recv_rs(...)         # 收 R，收到 last 则递减在途计数
    create_aws(n_writes) # 生成 AW（受 MAX_WRITE_TXNS 反压），压入 aw_queue/w_queue
    send_aws(...)        # 从 aw_queue 取出并发 AW
    send_ws(...)         # 从 w_queue 取出，按 len 逐拍发 W（生成合法 strobe）
    recv_bs(...)         # 收 B，递减写在途计数
  join                   # 六个进程都结束后 run() 才返回
```

读、写两条流水线**完全独立并行**，靠两个 `done` 标志（`ar_done`/`aw_done`）和「在途计数归零」来判定结束。并发控制的精髓是：发送侧在发之前 `while (tot_r_flight_cnt >= MAX_READ_TXNS) rand_wait(1,1);`，接收侧每收完一笔就递减计数——这就构成了一个由软件计数实现的**反压闸门**。

#### 4.1.3 源码精读

类的参数表把所有「随机旋钮」集中在一起，其中并发与突发相关的几个最关键：

> [axi_test.sv:692-693](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L692-L693) 定义 `MAX_READ_TXNS` / `MAX_WRITE_TXNS`：分别限制读、写方向的最大在途事务数，这就是并发上限的源头。

> [axi_test.sv:702-712](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L702-L712) 定义 `AXI_MAX_BURST_LEN`（单突发最大拍数，0 表示取 AXI 上限 256）、`AXI_BURST_FIXED/INCR/WRAP`（允许哪些突发类型）、`AXI_ATOPS`/`AXI_EXCLS`（是否产生原子/独占事务）、`UNIQUE_IDS`（是否保证每个在途事务 ID 唯一）。

构造函数 `new()` 把允许的突发类型收进 `allowed_bursts` 队列，并强制至少选一种：

> [axi_test.sv:789-798](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L789-L798) 根据三个布尔开关把 `BURST_FIXED/INCR/WRAP` 推入 `allowed_bursts`，末尾 `assert(allowed_bursts.size())` 保证不会「一种突发都不许发」。

地址区间通过 `add_memory_region` 声明，事务的 cache 属性由 `mem_type_t` 经 `get_arcache/get_awcache` 翻译：

> [axi_test.sv:811-813](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L811-L813) `add_memory_region(addr_begin, addr_end, mem_type)` 把一个「前闭后闭」的区间压入 `mem_map`，之后每笔事务会先随机挑一个区间，再在该区间内随机地址。

随机生成一笔突发的核心是 `new_rand_burst`，它用一个 `forever` 循环重抽地址，**直到整段突发不跨 4 KiB 页**：

> [axi_test.sv:936-961](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L936-L961) 随机 `len`/`size`/`addr`（带区间约束），然后用 `axi_pkg::beat_addr(...,0) >> 12 == beat_addr(...,ax_len) >> 12` 判断首拍与末拍是否落在同一个 4 KiB 页；不相等就重抽。这正是「受约束随机」的典型写法。

ID 合法性由两个方法配合保证。`id_is_legal` 在持有信号量时判断当前 ID 能否用：

> [axi_test.sv:1105-1123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1105-L1123) 若开了 `AXI_ATOPS`，ID 不能与任何在途 ATOP 撞；若开了 `UNIQUE_IDS`，同方向同 ID 不得已在途。否则返回合法。

`legalize_id` 在「合法前」一直循环重抽 ID，合法后递增在途计数：

> [axi_test.sv:1127-1166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1127-L1166) 取信号量 `cnt_sem.get()` → 判合法性 → 不合法就释放、等一拍、换一个 ID 再试；合法则 `r_flight_cnt[id]++` / `w_flight_cnt[id]++` 并 `cnt_sem.put()`。信号量保证多进程读写计数不竞争。

并发反压就一行：

> [axi_test.sv:1174-1176](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1174-L1176) `send_ars` 里 `while (tot_r_flight_cnt >= MAX_READ_TXNS) rand_wait(1, 1);` —— 在途读达到上限就原地等，把节奏让给 `recv_rs` 去收响应、递减计数。

最后是 6 进程的总入口：

> [axi_test.sv:1298-1317](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1298-L1317) `run()` 内 `fork ... join` 启动六个任务，`ar_done`/`aw_done` 在发送完毕后置位，各接收任务再等到「发送完且在途归零」才退出，从而保证 `run()` 返回时所有响应都已收回。

#### 4.1.4 代码实践（源码阅读型 + 参数观察）

**实践目标**：看清「并发上限」如何转化为总线上的反压行为。

1. 打开 [src/axi_test.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv)，定位 `send_ars`（1169 行起）与 `recv_rs`（1187 行起），在草稿纸上画出 `tot_r_flight_cnt` 的「+1（send_ars 里 legalize_id）/-1（recv_rs 里收到 r_last）」流转。
2. 在一个已有的随机 TB（如 [test/tb_axi_isolate.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv)）里，把 `MaxAW`/`MaxAR` 从 `30` 改成 `1`，重新仿真。
3. **需要观察的现象**：AR 通道应几乎不再有「上一笔响应还没回就发下一笔」的情况，AW/W 的吞吐明显下降。
4. **预期结果**：`MAX_READ_TXNS=1` 时读事务被强制串行化，总线利用率降低但功能仍正确（无 `Error`）。
5. 若本地没有仿真器，明确标注「待本地验证」，仅完成源码追踪部分即可。

#### 4.1.5 小练习与答案

**Q1**：为什么 `legalize_id` 必须在持信号量 `cnt_sem` 时才能调用 `id_is_legal`？
**答**：因为 `id_is_legal` 读的是 `r_flight_cnt`/`w_flight_cnt`/`atop_resp_*` 这些共享计数，而发送进程与接收进程会并发修改它们；不持信号量就会读到中间态，可能放行一个其实非法的 ID，破坏「同 ID 同方向保序」。

**Q2**：若把 `AXI_MAX_BURST_LEN` 设为 `0`，单突发最多多少拍？
**答**：`new()` 里 `0` 走 `this.max_len = 255`（[781-785 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L781-L785)），即 AXI 协议上限 `len=255` 对应 256 拍。

**Q3**：`run()` 的 `fork...join` 里，为什么读和写要拆成独立进程而不是串行？
**答**：真实主端读写会交错，拆成独立进程能在同一时刻既有 AR 在发、又有 AW/B 在收，从而制造出真实负载下的握手竞争与乱序，这正是随机验证想覆盖的场景。

---

### 4.2 axi_rand_slave 与 axi_lite_rand_*：随机响应端

#### 4.2.1 概念说明

`axi_rand_slave` 是 `axi_rand_master` 的镜像：挂在一条 `AXI_BUS_DV` 上扮演**从端**，收到请求后用「随机延迟 + 随机响应数据」回敬。它的价值在于：当你只关心 DUT（位于 master 侧）的行为、不在乎下游真实存储时，可以拿它当一个**省事的下游替身**。

它有两个工作模式：

- **默认模式**：读返回纯随机数据、写直接丢弃，可选 `RAND_RESP` 随机注入 SLVERR。
- **`MAPPED` 模式**：内部维护一张字节级 `memory_q`，读时若该字节写过就回真实数据、没写过就随机并「落账」；写时按 strobe 落账。这样它就退化成一个**简易可读写存储**（但不支持 `BURST_WRAP` 和 ATOP）。

AXI-Lite 侧的 `axi_lite_rand_master` / `axi_rand_slave` 是简化版：Lite 没有 ID、没有突发、没有 ATOP，所以代码更短。但 Lite 主端额外提供了**定向**的 `write()` / `read()` 任务，便于在随机洪流之外插一两笔确定地址的事务。

#### 4.2.2 核心流程

`axi_rand_slave.run()` 同样是 `fork ... join` 启动 5 个 `forever` 进程，永不返回（测试台靠 `$finish` 结束）：

```
run():
  fork
    recv_ars()   # 收 AR，按 ID 存入 rand_id_queue（允许乱序响应）
    send_rs()    # 取一个待响应 AR，逐拍发 R，len 归零发 last
    recv_aws()   # 收 AW，存入 aw_queue；若 ATOP 有 R_RESP 还顺带压入 ar_queue
    recv_ws()    # 收 W 直到 w_last；MAPPED 模式按 strobe 落账
    send_bs()    # 发 B（可选随机 SLVERR）
  join
```

关键细节是 `ar_queue` 用的是 `rand_id_queue_pkg::rand_id_queue`（按 ID 索引的队列），所以从端**可以乱序响应不同 ID 的读**，符合 AXI「不同 ID 可乱序」的规则。

#### 4.2.3 源码精读

`MAPPED` 参数与内部存储：

> [axi_test.sv:1338-1342](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1338-L1342) 注释说明 `MAPPED` 模式开启一个随机初始化的内部存储、不支持 `BURST_WRAP`、响应恒为 `RESP_OKAY`。

> [axi_test.sv:1364](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1364) `byte_t memory_q[addr_t];` —— 用关联数组按字节地址存数据，未写过的地址不存在。

收 AR 时按 ID 入队（允许乱序）：

> [axi_test.sv:1397-1408](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1397-L1408) `recv_ars` 调 `drv.recv_ar`，再 `ar_queue.push(ar_beat.ax_id, ar_beat)`；`MAPPED` 下还 `assert` 不允许 `BURST_WRAP`。

`send_rs` 在 `MAPPED` 模式下按字节查表回数据：

> [axi_test.sv:1426-1437](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1426-L1437) 遍历总线宽度内的每个字节：若 `memory_q.exists(byte_addr)` 就回真实数据，否则把随机数据写入 `memory_q`（「先读后落账」），并把响应强制成 `RESP_OKAY`。

AXI-Lite 主端的定向访问任务，是「在随机之外插确定事务」的标准写法：

> [axi_test.sv:1698-1720](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1698-L1720) `write()` 用 `fork` 同时发 AW 与 W 再 `join`，然后 `recv_b`；`read()` 先 `send_ar` 再 `recv_r`。两者都把响应通过 `output` 参数交还给调用者，便于断言。

Lite 从端的 `run()` 与完整版同构：

> [axi_test.sv:1844-1852](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1844-L1852) 五个 `forever` 进程：`recv_ars/send_rs/recv_aws/recv_ws/send_bs`，但因 Lite 无 ID，队列是普通 `$` 队列、按到达顺序响应。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解「默认随机」与「`MAPPED` 落账」两种模式在**同一笔先写后读**下的行为差异。

1. 阅读 [send_rs 的 MAPPED 分支](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1426-L1437) 与 [recv_ws 的落账逻辑](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1486-L1497)。
2. 在脑中模拟：对同一地址先写 `0xAB`（strobe 命中）、再读。
3. **预期结果**：`MAPPED=1` 时读回 `0xAB`；`MAPPED=0`（默认）时读回的是全新随机值，与之前写入无关。
4. 结论：需要「写后读一致」请用 `MAPPED` 或下一节的 `axi_sim_mem`；只想制造随机流量则用默认模式。**待本地验证**：可用一个最小 TB 实际跑两种参数对比。

#### 4.2.5 小练习与答案

**Q1**：`axi_rand_slave` 默认模式为什么不适合做「写后读一致性」验证？
**答**：默认模式写直接丢弃、读返回随机数据，两者没有关联；只有 `MAPPED=1` 才维护 `memory_q` 把写入和读出关联起来。

**Q2**：从端如何能对「先到的 AR 后响应、后到的 AR 先响应」？
**答**：因为 `ar_queue` 是 `rand_id_queue`（按 ID 索引），`send_rs` 调 `ar_queue.peek()` 取的是「当前选中的 ID」而非「最早到达的」，只要两个 AR 的 ID 不同，回序就可与到达顺序不同——这合法，因为 AXI 允许不同 ID 乱序。

**Q3**：Lite 版 `rand_master` 相比完整版省掉了什么并发约束？
**答**：Lite 没有 ID，也就没有「同 ID 保序」「ATOP 唯一性」等问题，所以 `axi_lite_rand_master` 里没有 `flight_cnt`/`legalize_id` 那一套，只有简单的 `MAX_READ_TXNS`/`MAX_WRITE_TXNS` 计数与队列。

---

### 4.3 axi_sim_mem：无限 AXI 存储从端

#### 4.3.1 概念说明

`axi_sim_mem` 是一个**仿真专用**模块（不可综合），扮演一个「**写到哪、读到哪**」的忠实 AXI 从端存储。它和 `axi_rand_slave` 的 `MAPPED` 模式目的相似，但能力更强、更接近真实存储：

- **按字节建表**：内部 `logic [7:0] mem[addr_t]`，用关联数组实现「无限」地址空间，只占用真正访问过的地址。
- **可注入错误**：另有 `rerr[]`/`werr[]` 两张表，可按地址预置响应码，模拟 SLVERR/DECERR；配合 `ClearErrOnAccess` 可做成「一次性错误」。
- **未初始化字节可控**：`WarnUninitialized` 决定读未写字节时是否 `$warning`；`UninitializedData` 决定返回什么（`"undefined"`/`"zeros"`/`"ones"`/`"random"`）。
- **monitor 输出**：把每一笔实际发生的读/写（地址、数据、ID、beat 计数、last）在**下一拍**送到外部端口，供 scoreboard 或覆盖率采集使用。
- **多端口**：`NumPorts` 可挂多个 AXI 接口，共享同一存储。

> 重要约定（见模块头注释）：**该模块不支持 ATOPs**。要测原子操作请用别的从端。

#### 4.3.2 核心流程

每个端口在一个 `initial` 里 `fork` 出 **5 个并行 `forever` 进程**，分别守护五个通道，进程间靠三个软件队列（`aw_queue`/`ar_queue`/`b_queue`）解耦：

```
wait(rst_ni);
fork
  AW 进程: 每拍置 aw_ready，握手则把 aw 压入 aw_queue
  W  进程: 仅当 aw_queue 非空才置 w_ready；握手则按 strobe 写 mem、累加 w_cnt
           到 len 则组一个 B beat 压入 b_queue
  B  进程: b_queue 非空则发 b_valid，握手后出队
  AR 进程: 每拍置 ar_ready，握手则压入 ar_queue
  R  进程: ar_queue 非空则逐拍发 R，按地址从 mem 读出（未初始化按策略填），
           到 len 发 last 并出队
join
```

时序上沿用 u3-l1 的 `TA/TT` 思想：进程在每个 `posedge clk_i` 后先 `#ApplDelay` 施加 ready/valid（application），再 `#(AcqDelay-ApplDelay)` 采样对端（acquisition）。两个时间参数保证「施加」与「采样」不撞在同一时刻。

#### 4.3.3 源码精读

模块端口与参数，注意 `NumPorts` 与错误/延迟旋钮：

> [axi_sim_mem.sv:27-93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L27-L93) 参数含 `AddrWidth/DataWidth/IdWidth/UserWidth/NumPorts`、类型参数 `axi_req_t/axi_rsp_t`、`WarnUninitialized/UninitializedData/ClearErrOnAccess`，以及 `ApplDelay/AcqDelay` 两个时间参数；端口是结构化的 `axi_req_i[NumPorts]`/`axi_rsp_o[NumPorts]` 加一组 `mon_w_*`/`mon_r_*` 监视输出。

核心存储与错误表（按字节、关联数组）：

> [axi_sim_mem.sv:118-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L118-L120) `logic [7:0] mem[addr_t];` 是数据表；`rerr[addr_t]`/`werr[addr_t]` 默认 `RESP_OKAY`，可按地址预置错误响应码。

W 通道按 strobe 逐字节写，并用 `resp_precedence` 累积本次突发的错误：

> [axi_sim_mem.sv:171-182](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L171-L182) 遍历本 beat 命中的字节范围（由 `beat_lower_byte`/`beat_upper_byte` 算出），仅当 `w.strb[i_byte]` 为 1 才写 `mem`，同时 `error_happened = resp_precedence(werr[addr], error_happened)` —— 把多个字节的错误按库约定的优先级（DECERR>SLVERR>OKAY>EXOKAY）合并进最终 B 响应。

R 通道对未初始化字节的处理，是 `axi_sim_mem` 最有教学价值的一段：

> [axi_sim_mem.sv:243-270](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L243-L270) 若 `!mem.exists(byte_addr)`：可选 `$warning`，再按 `UninitializedData` 在 `"random"/"ones"/"zeros"/默认('x)` 之间选择回填值；已存在的字节则直接读出。响应码同样用 `resp_precedence(rerr[addr], resp)` 合并。

monitor 输出刻意推迟一拍，是为了与 ATI（application/test time）时序兼容：

> [axi_sim_mem.sv:301-339](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L301-L339) 注释解释：写是否发生要到 `AcqDelay` 之后才能确定，无法在本拍 ATI 时序内给出，故把 `mon_w_*`/`mon_r_*` 统一在**下一拍** `<= #ApplDelay` 输出。

为了方便在不使用结构体的测试台里直接挂 `AXI_BUS`，模块还提供了接口外壳：

> [axi_sim_mem.sv:358-434](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L358-L434) `axi_sim_mem_intf` 用 `AXI_TYPEDEF_ALL` 生成 req/resp 类型，再用 `AXI_ASSIGN_TO_REQ`/`AXI_ASSIGN_FROM_RESP` 把 `AXI_BUS.Slave` 与内核结构体互连，是 testbench 里最常用的形态（[tb_axi_sim_mem.sv:63-89](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_sim_mem.sv#L63-L89) 即如此例化）。

#### 4.3.4 代码实践（配置观察型）

**实践目标**：让 `axi_sim_mem` 在读未初始化字节时给出可预测的值并告警。

1. 阅读 [R 通道未初始化处理](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L243-L270)。
2. 以 [test/tb_axi_sim_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_sim_mem.sv) 为模板，把例化参数改为 `WARN_UNINITIALIZED(1'b1)`、`UNINITIALIZED_DATA("zeros")`，并删掉其中的「先写后读」、改成「直接对一个从未写过的地址发起读」。
3. **需要观察的现象**：仿真日志应出现 `Access to non-initialized byte ...` 的 `$warning`，且读回数据为全 0。
4. **预期结果**：改回 `"random"` 则每次读回不同随机值；保持默认 `"undefined"`（`'x`）则读回不确定值。**待本地验证**（取决于仿真器是否开启告警打印）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `mon_w_*` 要在写发生的**下一拍**才输出，而不是当拍？
**答**：因为写是否真正握手要等到 `AcqDelay` 采样后才能确定，当拍内无法满足外部 monitor 的 ATI 时序（采样需要在沿后稳定），故推迟一拍以保证信号干净（[301-305 行注释](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L301-L305)）。

**Q2**：一笔写突发命中了 4 个字节，其中 1 个字节的 `werr` 预置为 `SLVERR`、另 3 个为 `OKAY`，最终 B 响应是什么？
**答**：`resp_precedence` 会取优先级更高的那个，`SLVERR` 优先于 `OKAY`，所以 B 响应为 `SLVERR`（[178 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L178)）。

**Q3**：能否用 `axi_sim_mem` 测带 `ATOP_R_RESP` 的原子读改写？
**答**：不能。模块头注释明确「does not support atomic operations (ATOPs)」，这类事务需用别的从端或先经 `axi_atop_filter`（见 u15-l1）。

---

### 4.4 axi_scoreboard：黄金模型自检

#### 4.4.1 概念说明

`axi_scoreboard` 是一个**旁路监听 + 自动比对**的类。它不驱动任何信号，只做两件事：

1. **采样**：像 u3-l1 的 `mon_*` 任务一样，旁观总线上每一次 `valid && ready` 握手，把 AW/W/B/AR/R 的载荷存进内部队列；
2. **比对**：内部维护一张「黄金内存」`memory_q`，**用采样的 AW/W 更新预期值、用采样的 AR/R 比对实际值**，不一致就 `$warning`/`$error`。

它通常挂在 **DUT 的主端侧总线**（即激励与 DUT 之间）上，用一个 `AXI_ASSIGN_MONITOR` 把 `AXI_BUS` 复制成一份只读的 `AXI_BUS_DV` 给它监听。这样 scoreboard 能同时看到「主端发了什么」和「DUT 回了什么」，从而自检 DUT 是否正确转发/改写了事务。

它支持三类可独立开关的检查（`check_e` 枚举）：`ReadCheck`（读数据比对）、`BRespCheck`（B 响应 ID 比对）、`RRespCheck`（R 响应 ID 与 last 比对）。

#### 4.4.2 核心流程

调用一次 `monitor()` 后，内部用 `fork ... join_none` 永久启动若干监听与处理进程：

```
monitor():
  fork (join_none)                 # 通道采样 + 写处理
    mon_aw();  mon_w();  mon_b();  # 把握手载荷压入 *_sample 队列
    handle_write();                # 用 AW+W 更新 memory_q，并把 aw 压入 b_queue[id]
    mon_ar();  mon_r();
  join_none
  for each id in 0..2^IW-1:        # 每个 ID 一对处理进程（按 ID 保序）
    fork (join_none)
      handle_write_resp(id);       # 收到 B 后，按 resp 决定保留/回滚 memory_q
      handle_read(id);             # 用 AR+R 比对 memory_q，不一致则告警
    join_none
```

这里有个精妙设计：`memory_q[addr]` 不是单值，而是**一个字节栈 `$`**。因为同一地址可能被多次写入、又被多次读取，且未初始化字节用 `8'hxx` 表示「任意值」。比对时不是严格相等，而是：

\[ \text{匹配} \iff \exists\, x \in \text{memory\_q}[addr],\ x = 8'\text{hxx} \lor x = \text{act\_data} \]

即「只要期望集合里有一个等于实际值、或是 `xx`（don't-care）」就算通过。这让 scoreboard 能优雅处理「读到从未写过的字节」这类合法的不确定场景。

#### 4.4.3 源码精读

类参数与三类检查枚举：

> [axi_test.sv:1951-1973](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1951-L1973) 参数为 `IW/AW/DW/UW/TT`；`check_e` 定义 `ReadCheck/BRespCheck/RRespCheck` 三种检查；`BUS_SIZE = $clog2(DW/8)` 用于把字节地址对齐到总线宽度。

黄金内存模型——注意是「字节地址 → 字节栈」的二维结构：

> [axi_test.sv:1990](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1990) `protected byte_t memory_q [axi_addr_t][$];` 每个（总线对齐的）字节地址挂一个字节队列，记录该字节的「历次期望值」。

`handle_write` 用 AW + W 更新黄金内存，并按 ID 记账等 B 回来：

> [axi_test.sv:2027-2068](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2027-L2068) 先 `assert` 只支持 `BURST_INCR` 或单拍、不支持 ATOP（否则 `$warning`）；按 `beat_addr` 算每拍地址，未初始化的位置先压入 `8'bxxxxxxxx`；再按 `w_strb` 决定每个字节是「用新数据覆盖」还是「保留旧值」，最后把 aw 压入 `b_queue[ax_id]` 等 B 响应。

读比对的核心——带 `8'hxx` 通配的集合查找：

> [axi_test.sv:2133-2151](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2133-L2151) 仅当 `ReadCheck` 开启且响应为 `OKAY/EXOKAY` 时比对；`exp_data.find with (item === 8'hxx || item === act_data)` 在期望字节集合里找「任意值或相等值」，找不到才 `$warning` 报「Unexpected RData」并打印期望/实际。

`monitor()` 一次性派生所有监听与处理进程（`join_none` 不阻塞）：

> [axi_test.sv:2264-2280](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2264-L2280) 先派生 5 个通道监听 + `handle_write`，再 `for` 每个 ID 派生 `handle_write_resp(id)` 与 `handle_read(id)`。注释强调「只在复位后调用一次」。

检查开关与一键全开：

> [axi_test.sv:2316-2324](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2316-L2324) `enable_all_checks()` 把 `check_en` 置全 1，`disable_all_checks()` 置 0；另可单独开关读/B/R 检查。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：弄懂「为什么读一个从未写过的地址，scoreboard 不会误报错」。

1. 打开 [handle_read](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2101-L2159)，跟踪 [2127-2131 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2127-L2131)：若 `memory_q` 不存在该地址，会先压入若干个 `8'bxxxxxxxx`。
2. 再看 [2140 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2140) 的 `find with (item === 8'hxx || item === act_data)`。
3. **预期理解**：因为期望集合里含 `8'hxx`，`find` 必然命中，所以不会误报；只有当某字节**曾被写入确定值**、而读回值与之不符时才报错。这正是 scoreboard 能容忍「合法不确定」的关键。

#### 4.4.5 小练习与答案

**Q1**：scoreboard 为什么挂在 **master 侧**（激励与 DUT 之间）而不是 slave 侧？
**答**：它要同时看到「主端发了什么请求」（据此更新黄金内存）和「DUT 回了什么响应」（据此比对），两侧都在 master 侧总线上可见；挂在 slave 侧只能看到经 DUT 改写后的流量，无法重建预期。

**Q2**：`memory_q[addr]` 为什么是「字节栈」而不是单个字节？
**答**：同一地址可能被多次写入、且存在「写入与对应 B 响应」之间的时序差；用栈可以记录每一拍的期望值，配合 `handle_write_resp` 在收到 B 后按响应是否 OKAY 决定「出栈确认」还是「回滚」，从而正确处理并发与错误响应。

**Q3**：如果 DUT 是一个会把数据取反的「恶意」模块，scoreboard 会不会报错？
**答**：会。DUT 回的实际数据 `act_data` 与黄金内存里的期望值（曾被写入的原值）不等、且期望值不是 `8'hxx`，`find` 命不中，触发 `Unexpected RData` 告警——这正是自检的意义。

---

## 5. 综合实践：rand_master → DUT → axi_sim_mem + scoreboard 最小自检拓扑

把本讲四个组件串成一个完整的「定向随机 + 自检」测试台。范本是 [test/tb_axi_to_mem_banked.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv)，它把 `axi_rand_master`、`axi_sim_mem`（此处下游是真实 SRAM，但思路完全一致）和 `axi_scoreboard` 三件套接在一起。我们以它为蓝本，搭一个**最简版**（DUT 用 `axi_join` 直通，专注验证组件接线）。

**拓扑**：

```
axi_rand_master ──(AXI_BUS_DV)── axi_join ── axi_sim_mem
        │                            │
        └──── AXI_ASSIGN_MONITOR ────┘
                     │
              axi_scoreboard（旁路监听 + 自检）
```

**操作步骤**：

1. **声明接口与时序参数**（沿用 u3-l1 的 `CyclTime/ApplTime/TestTime` 约定，`0 ≤ TA < TT < T_clk`）：

   ```sv
   localparam time CyclTime = 10ns, ApplTime = 2ns, TestTime = 8ns;
   localparam int unsigned AxiIdWidth=4, AxiAddrWidth=32, AxiDataWidth=64, AxiUserWidth=5;
   AXI_BUS #(...)     axi_master(), axi_slave();
   AXI_BUS_DV #(...)  master_dv(clk);
   `AXI_ASSIGN(axi_master, master_dv)        // DV ↔ 可综合接口
   ```

2. **typedef 随机主端**，并设置并发上限与一个地址区间：

   ```sv
   typedef axi_test::axi_rand_master #(
     .AW(AxiAddrWidth), .DW(AxiDataWidth), .IW(AxiIdWidth), .UW(AxiUserWidth),
     .TA(ApplTime), .TT(TestTime),
     .MAX_READ_TXNS(10), .MAX_WRITE_TXNS(10)   // 限制在途并发
   ) rand_master_t;
   ```

   参考 [tb_axi_isolate.sv:41-54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv#L41-L54) 的参数化写法。

3. **DUT 直通 + 下游 sim_mem**：用 `axi_join_intf` 把 `axi_master` 连到 `axi_slave`，再把 `axi_slave` 接到 `axi_sim_mem_intf`（参考 [tb_axi_sim_mem.sv:63-89](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_sim_mem.sv#L63-L89) 的例化）。

4. **挂 scoreboard**：复制一份只读 DV 给它监听，并启动检查：

   ```sv
   AXI_BUS_DV #(...) monitor_dv(clk);
   `AXI_ASSIGN_MONITOR(monitor_dv, axi_master)   // 监听主端侧总线
   typedef axi_test::axi_scoreboard #(.IW(AxiIdWidth),.AW(AxiAddrWidth),
                                      .DW(AxiDataWidth),.UW(AxiUserWidth),.TT(TestTime)) sb_t;
   sb_t sb = new(monitor_dv);
   initial begin
     sb.enable_all_checks();
     @(posedge rst_n);
     sb.monitor();          // 派生监听进程，只在复位后调用一次
     wait(end_of_sim);
   end
   ```

   这正是 [tb_axi_to_mem_banked.sv:394-416](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L394-L416) 的标准三件套。

5. **驱动主端**：声明区间、复位、跑随机事务、置位 `end_of_sim`：

   ```sv
   initial begin
     static rand_master_t mst = new(master_dv);
     end_of_sim <= 1'b0;
     mst.add_memory_region(32'h0000_0000, 32'h0000_FFFF, axi_pkg::DEVICE_NONBUFFERABLE);
     mst.reset();
     @(posedge rst_n);
     mst.run(200, 200);        // 200 笔随机读 + 200 笔随机写
     end_of_sim <= 1'b1;
   end
   ```

   对照 [tb_axi_to_mem_banked.sv:128-139](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_mem_banked.sv#L128-L139)。

**需要观察的现象与预期结果**：

- 仿真日志末尾出现 `Errors: 0,`（见 u1-l4 关于「日志内容当通过判据」的说明），且**没有** `Unexpected RData` 之类的 scoreboard 告警；
- 把下游 `axi_sim_mem` 换成一个故意改写数据的模块（例如把 R 数据取反），重新仿真，scoreboard 应**立即报错** `Unexpected RData ... Exp Data ... Act Data ...`。
- **待本地验证**：具体告警文本与仿真器有关；若没有仿真器，至少完成源码接线审查，确认 `AXI_ASSIGN_MONITOR` 监听的是主端侧、scoreboard 在复位后只 `monitor()` 一次。

> 进阶：把 `axi_join` 换成 `axi_delayer`（u4-l3）或 `axi_isolate`（u7-l2）作为 DUT，重复上面的随机回归，即可用同一套激励 + scoreboard 暴露不同模块的时序/死锁问题。

---

## 6. 本讲小结

- `axi_rand_master` 是「会自动生成合法事务」的主端类，核心是**受约束随机**：地址限区间、突发不跨 4 KiB 页、并发受 `MAX_READ_TXNS/MAX_WRITE_TXNS` 反压、ID 由 `legalize_id` + 信号量保证合法与保序。
- `axi_rand_slave` 与 `axi_lite_rand_*` 是镜像的随机从端；`MAPPED` 模式让它具备「写后读一致」的简易存储能力，Lite 主端额外提供定向 `write()`/`read()` 便于插入确定事务。
- `axi_sim_mem` 是仿真专用的**无限忠实存储**：按字节关联数组建表、可按地址注入 `rerr/werr` 错误、可配置未初始化字节策略、提供下一拍的 monitor 输出，但不支持 ATOP。
- `axi_scoreboard` 是**旁路黄金模型**：监听主端侧总线，用 AW/W 维护 `memory_q`、用 AR/R 自动比对，靠 `8'hxx` 通配优雅容忍「合法不确定」，三类检查可独立开关。
- 四者构成「**受约束随机激励 + 黄金模型自检**」的标准骨架，这正是全库 `tb_*.sv` 的共同范式，也是 directed random verification 的落地形态。
- 这些组件都是 `axi_test` `package` 内的**类**，靠虚接口绑定 + `fork...join` 多进程协作，本身不可综合，只用于仿真。

## 7. 下一步学习建议

- **u3-l3（编写并运行一个测试台）**：以 `tb_axi_lite_regs.sv` 为完整范本，把本讲的随机组件 + scoreboard 放进一个真实 testbench 的时钟/复位/`end_of_sim` 骨架里，端到端跑通一次。
- **回到 u1-l4 的回归方法**：把本讲的拓扑与 `run_vsim.sh` 的多 `sv_seed` 随机种子回归结合起来，体会「同一 TB、不同种子、多次跑」对覆盖率的放大作用（u16-l1 会系统讲）。
- **进阶阅读**：`axi_test.sv` 里还有 `axi_monitor`（被动记录事务，[1856 行起](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1856)）、`axi_file_master`（按文件回放事务序列，[2377 行起](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L2377)），可在需要波形级调试或确定性回放时再深入。
