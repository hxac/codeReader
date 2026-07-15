# L2 缓存四阶段流水线

## 1. 本讲目标

本讲深入 Nyuzi 内存层次的「中间层」——**L2 缓存**。在上一篇 u6-l2 中，我们看到 L1 缺失后请求会被 `l1_l2_interface` 仲裁打包成 `l2i_request` 发往 L2，并在 L2 响应回来后更新 L1、唤醒被挂起的线程。但「请求进了 L2 之后到底发生了什么」被当作黑盒跳过了。本讲就打开这个黑盒。

学完本讲，你应当能够：

1. 说清 L2 缓存**四阶段流水线**（仲裁 → 标签 → 读 → 更新）每一级的职责与数据流向。
2. 解释 L2 为什么采用**物理索引/物理标签（PI/PT）**，以及它与 L1（虚拟索引/物理标签）在地址翻译上的分工。
3. 描述一次**缓存缺失**如何被放入 fill 请求队列、经 AXI 从系统内存取回数据后**重新进入流水线开头**完成填充。
4. 描述被替换的**脏行**如何进入写回队列、经 AXI 写回内存。
5. 读懂 L2 如何在多核之间仲裁、如何用伪 LRU 替换、如何用 CAM 合并重复缺失。

---

## 2. 前置知识

在进入 L2 之前，请确认你已建立以下认知（来自前几讲）：

- **缓存行（cache line）**：Nyuzi 中一行固定 64 字节，恰好等于一个向量寄存器宽度。所有缓存都以行为单位读写。
- **组相联（set-associative）**：缓存被分成若干**组（set）**，每组有若干**路（way）**。地址被切成 `tag | set_idx | offset` 三段。
- **虚拟索引/物理标签 vs 物理索引/物理标签**：L1D 用虚拟地址索引、物理地址比对标签（VI/PT）；L2 则直接用物理地址做索引和比对（PI/PT）。本讲会讲清这种分工的根源。
- **L1 缺失后的请求包**：`l1_l2_interface` 把 L1 缺失封装成 `l2req_packet_t`（见 u6-l2），其中携带 `core`（哪个核）、`id`（核内哪个线程的 miss 队列表项）、`packet_type`（LOAD/STORE/…）、`cache_type`（I/D）、物理地址、store 掩码与数据。
- **协同仿真与周期精确**：L2 属于硬件 RTL，行为由 Verilator 周期精确仿真验证。

一个需要提前点明的关键区别：**L1 缓存是每个核私有的，而 L2 是所有核共享的**。L2 既是 L1 缺失的共同去处，也是通往系统内存（DRAM）与外设的统一出口。理解 L2，就是理解「多核如何共享一片容量更大、速度更慢的缓存」。

---

## 3. 本讲源码地图

本讲涉及的源码全部位于 `hardware/core/`，构成 L2 的完整实现：

| 文件 | 作用 |
|------|------|
| [l2_cache.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv) | L2 顶层，把四阶段流水线与总线接口连起来，输出性能事件。 |
| [l2_cache_arb_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv) | **仲裁级**：从多核请求与「重启请求」中选一个进入流水线。 |
| [l2_cache_tag_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv) | **标签级**：用物理地址查标签 SRAM、读 LRU、读 dirty/valid 位。 |
| [l2_cache_read_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv) | **读级**：判定命中、读数据 SRAM、驱动标签/LRU/dirty 更新、维护 LL/SC 同步状态、产出性能脉冲。 |
| [l2_cache_update_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv) | **更新级**：应用 store 掩码合并数据、写回数据 SRAM、向核广播响应包。 |
| [l2_axi_bus_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv) | **总线接口**：维护 fill 请求队列与脏行写回队列，驱动 AXI4 状态机，fill 完成后把请求重新喂回流水线开头。 |
| [l2_cache_pending_miss_cam.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_pending_miss_cam.sv) | 用 CAM（内容寻址内存）检测「重复缺失」，节省内存带宽、避免数据互相覆盖。 |
| [cache_lru.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv) | 伪 LRU 替换算法（L1D/L2 共用）。 |
| [defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | L2 相关类型与常量（`l2req_packet_t`、`l2_tag_t` 等）。 |
| [config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) | L2 容量参数（`L2_WAYS`、`L2_SETS`、`AXI_DATA_WIDTH`）。 |

此外，单元测试 [tests/unit/test_l2_cache.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_l2_cache.sv) 提供了可运行的验证用例，本讲实践会用到它。

---

## 4. 核心概念与源码讲解

先建立全局：L2 缓存的顶层把四条流水级串成一条线，再挂上一个总线接口负责「缺失填充」与「脏行写回」。L2 顶部的源码注释精炼地描述了整条路径：

> [l2_cache.sv:L21-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L21-L37) —— L2 是四阶段流水线（仲裁/标签/读/更新）；检测到缺失（在读级之后）就把请求放入 fill 请求队列，由系统内存接口取回数据后**从流水线开头重新启动**该请求；若被替换的行是脏的，读级会把它读出来放入写回队列。L2 是物理索引/物理标签，地址翻译由 L1 的 TLB 完成。

我们可以把整条 L2 想象成一台「带回收口的传送带」：

```
                    ┌─────────────────────────────── L2 四阶段流水线 ───────────────────────────────┐
                    │                                                                              │
   多核请求 ──▶ ① 仲裁 ──▶ ② 标签 ──▶ ③ 读 ──▶ ④ 更新 ──▶ 响应包广播给核
                    ▲                                              │
                    │                                              │ 命中？
        重启请求     │                                              ├─ 命中：④ 更新数据并回 ACK
        （fill 完成）│                                              └─ 缺失：放入 fill 队列 ─┐
                    │                                                                      ▼
                    │                                                          ┌─────────────────────┐
                    │                                                          │  l2_axi_bus_interface│
                    │                                                          │  fill 队列 / 写回队列 │
                    │                                                          │  + AXI4 状态机       │
                    └────────── fill 数据回来，重启 ──────────────────────────┤                     │
                                       （脏行另走写回队列写回内存）            └─────────────────────┘
```

下面五个最小模块分别拆开五个部分：仲裁、标签、读、更新，以及「缺失填充与脏行写回」（总线接口）。每个模块都对应一条源码线。

---

### 4.1 仲裁阶段（l2_cache_arb_stage）

#### 4.1.1 概念说明

L2 是**所有核共享**的资源，但它的流水线每个周期只能吃进**一条请求**。仲裁级（arbitrate）要解决的问题是：当多个核同时发出 L2 请求时，本周期该放哪一条进流水线？

这里有一个隐藏的第二类「请求」——**重启请求（restarted request）**。当一次缺失的 fill 数据从内存回来后，总线接口需要把这条请求**重新喂回流水线开头**，让它带着新数据走一遍标签/读/更新，真正把数据写进 L2。这种重启请求与普通的多核请求在仲裁级汇合，需要排个优先级。

设计选择是：**重启请求优先**。原因是如果不优先处理重启请求，它们会堆积在总线接口的队列里，而队列容量有限，可能导致 fill 完成后无处回灌、形成反压甚至死锁。注释明确说明了这一点。

#### 4.1.2 核心流程

```
本周期输入：
  - l2i_request_valid[NUM_CORES]   各核是否有新请求
  - l2i_request[NUM_CORES]         各核的请求包
  - l2bi_request_valid             总线接口是否有重启请求
  - l2bi_stall                     总线接口队列快满了，要求停手

仲裁逻辑（组合）：
  can_accept = !l2bi_request_valid && !l2bi_stall   // 没有重启请求且总线不反压，才接受新请求
  if (l2bi_request_valid):
      grant = 重启请求          // 重启请求绝对优先
  else if (NUM_CORES > 1):
      grant = rr_arbiter(各核请求位)   // 轮询仲裁选一个核
  else:
      grant = 唯一核的请求

输出：把选中的请求寄存一拍，得到 l2a_request 送入标签级
      对被选中的核拉高 l2_ready（握手成功）
```

注意 `can_accept_request` 把 `l2bi_stall` 也算进去了——当 fill 队列或写回队列「快满」时，仲裁级会停止接受新请求，给总线接口腾出消化时间。

#### 4.1.3 源码精读

仲裁的核心是「重启优先 + 多核轮询」：

> [l2_cache_arb_stage.sv:L53-L59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L53-L59) —— 定义 `can_accept_request`（没有重启请求且总线不反压）和 `restarted_flush`（判断重启请求是不是一次 flush 的第二趟）。

> [l2_cache_arb_stage.sv:L74-L92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L74-L92) —— 用 `generate` 区分多核与单核：多核时实例化 `rr_arbiter`（轮询仲裁器）在 `l2i_request_valid` 上公平选一个，再用 `oh_to_idx` 把独热码转成核号索引取出请求包；单核时直接旁路。

> [l2_cache_arb_stage.sv:L94-L111](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L94-L111) —— 寄存逻辑：有重启请求（`l2bi_request_valid`）就把重启请求与从内存带回的数据送入下一级，并标记 `l2a_l2_fill`（这是要填充的请求）；否则送入新选中的核请求，`l2a_l2_fill` 清零。`l2a_l2_fill` 这个标志会贯穿整条流水线，告诉后续各级「这是 fill 数据，请写入缓存」。

> [l2_cache_arb_stage.sv:L113-L136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L113-L136) —— `l2a_request_valid` 的生成同样遵循「重启优先」：有重启请求时一定有效（并断言重启的不能是 invalidate 类，因为它们不会缺失、不会被重启）；否则当存在核请求且 `can_accept_request` 时有效。

一个反直觉的细节：注释（[L24-L27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L24-L27)）特别强调 `l2_ready` 组合依赖于请求包里的 valid 位，所以 **valid 位绝不能再依赖 `l2_ready`**，否则会形成组合环路。这是握手设计中的经典陷阱。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「重启请求优先」在代码里如何压制多核请求。

**操作步骤**：

1. 打开 [l2_cache_arb_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv)。
2. 定位 `can_accept_request`（L58），注意它把 `l2bi_request_valid` 放在最前面做短路求值。
3. 追踪 `l2_ready[request_idx]`（L65）：它等于 `grant_oh[i] && can_accept_request`。也就是说，**只要总线接口在重启，所有核的 `l2_ready` 都会被 `can_accept_request=0` 拉低**，即使某核恰好被 `rr_arbiter` 选中。

**需要观察的现象 / 预期结果**：理解即使两个核同时请求、且 `rr_arbiter` 这周期选中了核 0，只要本周期有重启请求，核 0 的 `l2_ready` 仍为 0，它必须等下一周期。这就是「重启优先」的具体表现。

#### 4.1.5 小练习与答案

**练习 1**：为什么重启请求必须比多核请求优先？如果反过来（多核优先），最坏会发生什么？

> **答案**：重启请求是 fill 数据已经从内存取回、只差「写回 L2 并回 ACK」的请求。若被多核请求长期抢占，它们会滞留在总线接口的 fill 队列里；队列有限，最终触发 `l2bi_stall` 反压，而反压又会阻止新的 fill 完成回灌，可能造成吞吐塌缩甚至死锁。优先重启请求可保证队列及时排空。

**练习 2**：单核（`NUM_CORES=1`）配置下，`rr_arbiter` 还会被实例化吗？

> **答案**：不会。代码用 `generate if (\`NUM_CORES > 1)` 分支处理（[L70-L91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_arb_stage.sv#L70-L91)）。单核时直接 `assign grant_oh[0] = l2i_request_valid[0]`，省去仲裁器开销。

---

### 4.2 标签阶段（l2_cache_tag_stage）

#### 4.2.1 概念说明

请求进入流水线后，标签级（tag）要做的是**用物理地址查缓存**：这条地址对应的缓存行在不在 L2 里？如果在，在哪一路（way）？

这里要回到本讲的第二个关键概念——**物理索引/物理标签（PI/PT）**。回忆 L1D 是 VI/PT：用虚拟地址的组索引位去查 SRAM（省一拍，因为不必等 TLB），再用物理 tag 比对。而 L2 干脆**完全用物理地址**：索引和标签都是物理的。

为什么 L2 可以这么干？因为**地址翻译已经在 L1 那一层做完了**。当请求到达 L2 时，它携带的 `l2_addr_t` 已经是物理地址（由 L1 的 TLB 翻译好，见 u7-l1）。L2 不需要 TLB，也就没有 VI/PT 那套「为避免别名而限制组数」的约束。注释在 [l2_cache.sv:L35-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L35-L37) 一句话点明了这种分工：**L2 是物理索引/物理标签，所有地址都是物理的；地址翻译由 L1 的 TLB 完成**。

> **术语澄清**：物理地址（physical address）是经过 MMU 翻译后、真实指向内存的地址；虚拟地址（virtual address）是程序看到的地址。L1 在两者之间做翻译，L2 只认物理地址。

标签级除了查标签，还要顺带读两样东西：**LRU 信息**（决定缺失时替换哪一路）和**每路的 dirty/valid 位**（决定被替换的行要不要写回、这一路到底有没有效数据）。这些都在本级一次性读出，供下一级（读级）做判定。

#### 4.2.2 核心流程

```
本级输入：l2a_request（含物理地址 .address = {tag, set_idx}）、l2a_l2_fill 标志
本级动作（全部是 SRAM 读，结果下一拍可见）：
  for 每一路 way in [0, L2_WAYS):
      读 tag[way][set_idx]        → 送读级比对
      读 dirty[way][set_idx]      → 送读级判断是否需写回
      读 valid[way][set_idx]      → 送读级判断是否有效
  读 LRU[set_idx]                 → 若是 fill，算出 fill_way（要替换的路）

同时：把请求包、data_from_memory、l2_fill 标志寄存一拍传给读级。
```

物理地址在 L2 里的切分（以默认配置 `L2_SETS=256`、缓存行 64 字节为例）：

```
物理地址(32 位) = [ tag(18 位) | set_idx(8 位) | offset(6 位) ]
                                  │                └── 行内字节偏移（L2 不关心，因为按整行操作）
                                  └── 选中 256 组中的一组
                   └── 与组内 8 路的 tag 比对，判定命中
```

对应类型定义见 [defines.svh:L336-L342](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L336-L342)：`l2_tag_t` 位宽 = `32 - (offset_width + clog2(L2_SETS))` = `32 - (6 + 8)` = 18 位。L2 默认容量 = `L2_WAYS × L2_SETS × 64` = `8 × 256 × 64` = 128 KiB（[config.svh:L46-L47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L46-L47)）。

#### 4.2.3 源码精读

标签级用 `generate` 给每一路实例化一组 SRAM，再实例化一个共用的 LRU 模块：

> [l2_cache_tag_stage.sv:L65-L76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv#L65-L76) —— 实例化 `cache_lru`：`fill_en`/`fill_set` 在本级给出（若是 fill 请求），下一拍由模块返回 `fill_way`（要替换的路）；`access_en` 在请求有效时给出，配合读级回传的 `update_en`/`update_way` 把命中路移到 MRU。

> [l2_cache_tag_stage.sv:L82-L136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv#L82-L136) —— 每一路用两块 `sram_1r1w` 分别存 **tag** 和 **dirty 标志**，valid 位则用普通触发器数组 `line_valid` 存。读地址都是 `set_idx`，写地址/写值由读级回传（`l2r_update_*`）。

> [l2_cache_tag_stage.sv:L124-L134](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv#L124-L134) —— **valid 旁路（bypass）**：如果本级正在读的 `set_idx` 恰好等于读级这周期要写的 `set_idx`，直接把写值 `l2r_update_tag_valid` 当作读结果，避免读到陈旧数据。这与 sram_1r1w 的 `READ_DURING_WRITE("NEW_DATA")` 配合，保证流水线内「同地址写后读」的正确性。

一个值得注意的约束在文件开头：[L62](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv#L62) 用 `assert` 检查 `L2_SETS` 必须是 2 的幂——这是组索引位宽取 `clog2` 的前提。

#### 4.2.4 代码实践

**实践目标**：验证物理地址到 `{tag, set_idx}` 的切分，并理解 L2 不需要 TLB。

**操作步骤**：

1. 读 [defines.svh:L336-L345](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L336-L345) 中 `l2_tag_t`、`l2_set_idx_t`、`l2_addr_t`、`cache_line_index_t` 的定义。
2. 手算：在默认配置下，物理地址 `0x0000_4000`（即 16384）的 `set_idx` 和 `tag` 各是多少？（提示：先右移 6 位得到 cache line index，再取低 8 位为 set_idx，其余为 tag。）
3. 全局搜索 L2 目录里是否出现 TLB 相关字样：你会发现在 `l2_cache*.sv` 中**完全没有 TLB**，印证「L2 不翻译地址」。

**需要观察的现象 / 预期结果**：`0x4000 >> 6 = 0x100 = 256`，其低 8 位 `set_idx = 0`，高位 `tag = 1`。预期你会确认 L2 源码里没有任何 TLB 实例。

#### 4.2.5 小练习与答案

**练习 1**：为什么 L1D 必须限制 `L1D_SETS ≤ 64`，而 L2 的 `L2_SETS=256` 却没有这个限制？

> **答案**：L1D 是 VI/PT，组索引位必须落在 12 位页内偏移内（offset 6 位 + set_idx ≤ 12 位，故 set 数 ≤ 64），否则同一物理地址会因虚拟地址不同而落到不同组，产生别名。L2 是 PI/PT，索引直接来自物理地址，物理地址到组的映射是唯一的，天然无别名，所以组数不受页大小约束。

**练习 2**：标签级为什么要把 valid 位存在触发器里，而 tag/dirty 存在 SRAM 里？

> **答案**：valid 位只有 1 比特且需要 reset 时整体清零（[L113-L122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_tag_stage.sv#L113-L122) 用 reset 循环清零），用触发器数组便于复位；tag 较宽、数量大，用 SRAM 宏更省面积，这也是可综合实现的常规做法。

---

### 4.3 读阶段（l2_cache_read_stage）

#### 4.3.1 概念说明

读级（read）是 L2 的「大脑」，承担最重的判定工作。它在本级拿到标签级读出的各路 tag/valid/dirty，做四件事：

1. **判定命中**：有没有一路的 tag 匹配且 valid？命中则读出该路数据。
2. **驱动元数据更新**：决定要不要改 dirty 位（store 置脏、flush 清脏）、要不要改 tag（fill 置有效、invalidate 置无效）、要不要更新 LRU（命中 load/store 才更新）。
3. **读出待写回的脏数据**：若是 fill 且要替换的路是脏的，或是一次 flush 命中，本级要把旧行数据读出来交给总线接口去写回内存。
4. **维护同步访存（LL/SC）状态**：L2 在缓存行粒度上跟踪 `load_sync`/`store_sync`，判断 `store_sync` 是否成功。
5. **产出性能脉冲**：统计 L2 命中、缺失。

这一级还实例化了 L2 的**数据 SRAM**（整行 512 比特），是 L2 里最大的一块存储。

> **为什么命中判定放在读级而不是标签级？** 因为 SRAM 读是异步的：标签级用地址去读 tag SRAM，结果**下一拍**才到读级。所以「比对 tag」这件事天然落在读级。这是组相联缓存的标准两拍查表结构。

#### 4.3.2 核心流程

```
本级输入：l2t_tag[L2_WAYS]、l2t_valid[L2_WAYS]、l2t_dirty[L2_WAYS]、l2t_fill_way、l2t_l2_fill...

1. 命中判定（组合，对每一路并行）：
   hit_way_oh[way] = (request.address.tag == l2t_tag[way]) && l2t_valid[way]
   cache_hit = (|hit_way_oh) && request_valid
   hit_way_idx = oh_to_idx(hit_way_oh)        // 命中的路号

2. 读数据 SRAM：
   read_addr = { l2t_l2_fill ? fill_way : hit_way_idx , set_idx }
   （fill 时读「将被替换的路」的旧数据，用于写回；命中时读命中路的数据）

3. 元数据更新信号（回送给标签级下一拍写）：
   - dirty：fill 时按 store 与否初始化；命中 store/flush 时改写
   - tag：fill 时置有效；dinvalidate 命中时置无效
   - LRU：命中 load/store 才把命中路移到 MRU

4. 写回判定：
   writeback_way = flush ? hit_way_idx : fill_way
   needs_writeback = l2t_dirty[writeback_way] && l2t_valid[writeback_way]

5. 同步访存（LL/SC）：
   can_store_sync = (记录的 load_sync 地址 == 本次地址) && 有效 && 是 STORE_SYNC
```

#### 4.3.3 源码精读

命中判定用 `generate` 对每一路并行比较：

> [l2_cache_read_stage.sv:L124-L136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L124-L136) —— 每路 `hit_way_oh[way]` 比较 tag 与 valid，再 `|` 归约得 `cache_hit`，用 `oh_to_idx` 得到命中路号。注意 [L255](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L255) 的断言：一次最多只能有一路命中（`$onehot0(hit_way_oh)`），保证缓存内容不冲突。

> [l2_cache_read_stage.sv:L146-L157](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L146-L157) —— **数据 SRAM**：`SIZE = L2_WAYS * L2_SETS`（默认 8×256=2048 行），每行 512 比特。读地址由 `read_address`（[L140-L141](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L140-L141)）给出，fill 时读 `fill_way`、命中时读 `hit_way_idx`。这就是「fill 时读旧脏数据用于写回」的读口。

> [l2_cache_read_stage.sv:L164-L179](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L164-L179) —— **dirty 更新**：`update_dirty` 在 fill 或（命中且 store/flush）时成立；`l2r_update_dirty_value = store`（flush 时 store 为 0，即清脏）。这只把脏位变化告诉标签级，真正改写发生在标签级的 SRAM 写口。

> [l2_cache_read_stage.sv:L185-L197](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L185-L197) —— **tag 更新**：fill 时把新行置有效（`tag_valid = !dinvalidate`），`tag_value = request.address.tag`；dinvalidate 命中时置无效。配合标签级的写口完成「装入新行 / 作废旧行」。

> [l2_cache_read_stage.sv:L208-L212](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L208-L212) —— **LL/SC 同步判定**：`can_store_sync` 要求「该线程此前 `load_sync` 记录的地址 == 本次 `store_sync` 地址」且记录仍有效。`request_sync_slot` 用 `{core, id}` 唯一标识一个硬件线程，保证多核多线程互不串扰。

> [l2_cache_read_stage.sv:L262-L292](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L262-L292) —— **同步状态机维护**：`LOAD_SYNC` 记录监视地址；普通 `STORE` 或成功的 `STORE_SYNC` 会**作废所有监视同一缓存行的线程**的记录（注释 [L273-L274](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L273-L274) 强调：只有 store 真正成功才作废，否则线程会活锁）。这与 u10-l1 讲的 LL/SC 语义直接对应。

> [l2_cache_read_stage.sv:L294-L296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L294-L296) —— **性能脉冲**：`l2r_perf_l2_miss` / `l2r_perf_l2_hit`，只对真正的 load/store（非 fill）统计，喂给顶层 `l2_perf_events`。

读级还有一处精巧设计：**fill 与命中不能同周期发生**（[L252](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv#L252) 断言 `!l2t_l2_fill || !cache_hit`）。因为 fill 意味着「这行原本不在缓存里」，不可能同时又命中。

#### 4.3.4 代码实践

**实践目标**：理清一次「命中 load」与一次「store 改脏位」在读级分别触发了哪些更新信号。

**操作步骤**：

1. 打开 [l2_cache_read_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_read_stage.sv)。
2. 假设一次命中的 `L2REQ_LOAD`：查 `update_dirty`（L166）、`update_tag`（L185）、`l2r_update_lru_en`（L202）分别是什么值。
3. 假设一次命中的 `L2REQ_STORE`：再查这三个信号。
4. 对照标签级（4.2.3）的写口，确认这些信号下一拍分别改写了 dirty/tag/LRU。

**需要观察的现象 / 预期结果**：
- 命中 load：`update_dirty=0`、`update_tag=0`、`l2r_update_lru_en=1`（只更新 LRU）。
- 命中 store：`update_dirty=1`（且 `dirty_value=1`，置脏）、`update_tag=0`、`l2r_update_lru_en=1`。
- 你会确认「load 不改 dirty/tag，只动 LRU；store 额外置脏」。

#### 4.3.5 小练习与答案

**练习 1**：`read_address` 在 fill 时为什么选 `fill_way` 而不是 `hit_way_idx`？

> **答案**：fill 表示缓存缺失后正在装入新行，此时没有命中路（`hit_way_oh` 全 0）。选 `fill_way`（LRU 选出的替换路）是为了读出**即将被覆盖的旧行数据**——若它是脏的，要把这份数据写回内存，否则数据会丢失。

**练习 2**：读级断言「fill 与命中不能同周期」，违反它会意味着什么？

> **答案**：意味着硬件状态不一致——既然是 fill（缺失回填），该地址本不该在缓存里；若同时命中，说明标签或 LRU 逻辑出错，可能造成数据被错误覆盖或重复填充。

---

### 4.4 更新阶段（l2_cache_update_stage）

#### 4.4.1 概念说明

更新级（update）是 L2 流水线的最后一站，职责有二：

1. **更新数据 SRAM**：若是 fill 或命中 store，把数据真正写进缓存。对 store，还要按 `store_mask` 把新数据**按字节合并**进旧行（不是整行覆盖）。
2. **向核广播响应包**：根据请求类型生成 `l2rsp_packet_t`（LOAD_ACK/STORE_ACK/FLUSH_ACK/…），带上数据、核号、线程 id，交给 `l1_l2_interface` 路由回正确的核与线程（见 u6-l2）。

这一级把「写缓存」和「回 ACK」合并处理：当一次命中 store 走到更新级时，它一边把合并后的数据写回数据 SRAM，一边把同样的数据作为响应返回给核（这样核拿到的就是最新值）。

#### 4.4.2 核心流程

```
本级输入：l2r_data（命中读出的旧行）、l2r_data_from_memory（fill 带回的新行）、
         l2r_request（含 store_mask、data）、l2r_cache_hit、l2r_l2_fill...

1. 选基准数据：
   original_data = l2r_l2_fill ? l2r_data_from_memory : l2r_data
   （fill 用内存取回的新行；命中 store 用读出的旧行）

2. 按字节合并（generate 展开成 64 个字节通道）：
   for 字节 b in [0, 64):
       write_data[b] = (store_mask[b] && update_data) ? request.data[b] : original_data[b]
   即：store_mask 为 1 的字节用新数据，其余保留基准数据。

3. 写数据 SRAM：
   write_en = request_valid && (fill || (命中 && (store || store_sync)))
   write_addr = l2r_hit_cache_idx     // {way, set} 索引

4. 生成响应包类型（always_comb 查表）：
   LOAD/LOAD_SYNC → L2RSP_LOAD_ACK
   STORE/STORE_SYNC → L2RSP_STORE_ACK
   FLUSH → L2RSP_FLUSH_ACK
   ...

5. 决定何时拉高 l2_response_valid（见源码精读）。
```

#### 4.4.3 源码精读

数据合并是更新级最核心的逻辑，用 `generate` 把 64 字节逐通道处理：

> [l2_cache_update_stage.sv:L58-L70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L58-L70) —— `original_data` 在 fill 与非 fill 间二选一；每个字节通道按 `store_mask[b] && update_data` 决定取新数据还是基准数据。`update_data`（[L59-L60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L59-L60)）只在普通 store 或成功的 store_sync 时为真，保证失败的同步 store 不改数据。

> [l2_cache_update_stage.sv:L72-L75](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L72-L75) —— **写使能**：只在 fill 或命中 store/store_sync 时写；`write_addr` 用 `l2r_hit_cache_idx`（读级算出的 `{way, set}`）。

> [l2_cache_update_stage.sv:L78-L101](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L78-L101) —— **响应类型查表**：用 `unique case` 把请求类型映射成响应类型。

> [l2_cache_update_stage.sv:L106-L107](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L106-L107) —— **flush 完成判定**：一次 flush 要么第一趟就发现数据不在缓存（无需写回），要么是写回完成后的第二趟（`restarted_flush`），才算完成、才回 FLUSH_ACK。这就是 flush 为何要在总线接口走两趟。

> [l2_cache_update_stage.sv:L115-L129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L115-L129) —— **响应有效条件**：命中且非 flush、或 fill、或 flush 完成、或 invalidate 类，才拉高 `l2_response_valid`。注意 flush 在第一趟命中且需要写回时**不**回 ACK——它要先去总线接口写回脏数据，第二趟回来才完成。

> [l2_cache_update_stage.sv:L135-L145](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L135-L145) —— 响应包字段：`status` 对 store_sync 设为成功标志、其余为 1；`data` 直接用 `l2u_write_data`（合并后的最新数据）；`core`/`id` 原样回传，供 L1 侧路由。

#### 4.4.4 代码实践

**实践目标**：用单元测试里的常量手算一次 store 合并，验证 `write_data` 的生成。

**操作步骤**：

1. 打开单元测试 [tests/unit/test_l2_cache.sv:L28-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_l2_cache.sv#L28-L37)，里面有现成的 `DATA1`、`STORE_DATA1`、`STORE_MASK1`、`STORE_RESULT1`。
2. 把 `STORE_MASK1` 看成 64 个字节的位图（每个比特对应一个字节）。`STORE_MASK1` 的低若干位为 1 表示这些字节用 `STORE_DATA1` 覆盖 `DATA1`。
3. 模拟更新级的 [L66-L68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L66-L68) 逻辑，确认 `STORE_RESULT1` 正是 `DATA1` 与 `STORE_DATA1` 按 `STORE_MASK1` 合并的结果。

**需要观察的现象 / 预期结果**：合并后只有 mask 为 1 的字节被替换，其余保持 `DATA1`，结果与 `STORE_RESULT1` 一致——这正是 store 部分写（partial store）在 L2 的实现方式。

#### 4.4.5 小练习与答案

**练习 1**：一次 fill（缺失回填）走到更新级时，`original_data` 取哪个？为什么？

> **答案**：取 `l2r_data_from_memory`（从系统内存取回的整行新数据）。因为 fill 是把内存的数据装入缓存，基准就是内存内容；此时通常 `store_mask` 不起作用（除非是带写的大块 store），整行写入。

**练习 2**：为什么 flush 命中且需要写回时，第一趟不回 ACK？

> **答案**：因为脏数据还没写回内存，flush 尚未真正完成。第一趟把脏行送进总线接口的写回队列，等 AXI 写回完成后，请求以 `restarted_flush` 身份重新进入流水线走第二趟，此时 `completed_flush` 成立才回 FLUSH_ACK（[L106-L107](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_update_stage.sv#L106-L107)）。

---

### 4.5 缺失填充与脏行写回（l2_axi_bus_interface + 辅助模块）

#### 4.5.1 概念说明

前四级处理的是「命中」和「元数据维护」。但缓存总会缺失，被替换的行也可能是脏的。这两件事都涉及**访问 L2 之外的系统内存**，而系统内存挂在 **AXI4 总线**上。`l2_axi_bus_interface` 就是 L2 与 AXI4 之间的桥梁，它维护两个队列与一台状态机：

- **fill 请求队列（pending_fill_fifo）**：缓存缺失时，把请求入队；状态机经 AXI 读内存，取回整行数据后，把请求**重新喂回仲裁级**（带上 `l2_fill` 标志），让它重走流水线完成填充。
- **写回队列（pending_writeback_fifo）**：被替换/被 flush 的脏行入队；状态机经 AXI 把脏数据写回内存。

这台状态机还处理两个微妙问题：

1. **重复缺失合并**：如果两个请求缺失同一缓存行，不该发两次内存读。`l2_cache_pending_miss_cam` 用一个 CAM（内容寻址内存）记录所有正在 fill 的行，命中即标记 `collided_miss`，跳过内存读直接重灌。
2. **写回优先于读**：状态机在 IDLE 时**先处理写回再处理 fill**（[L219-L223](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L219-L223)），避免读到陈旧数据。

> **术语澄清**：AXI4 是 ARM 提出的片上总线协议，分读地址/读数据/写地址/写数据/写响应五个通道，支持突发传输（burst）——一次地址、连续多个数据拍。Nyuzi 的 AXI 数据宽度默认 32 位（[config.svh:L48](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L48)），一个 64 字节缓存行需要 `512/32 = 16` 拍突发。

#### 4.5.2 核心流程

一次缺失的完整生命周期（与本讲综合实践呼应）：

```
① 读级发现缺失（!cache_hit && !l2_fill && 是 LOAD/STORE...）
   → enqueue_fill_request=1，请求入 fill 队列
   → pending_miss_cam 记录该行「正在 fill」

② 总线状态机 IDLE：先看写回队列，再看 fill 队列
   → 若是 fill：STATE_READ_ISSUE_ADDRESS → STATE_READ_TRANSFER（16 拍收数据）
   → 期间若 CAM 报 collided_miss：直接跳到 READ_COMPLETE（不读内存）

③ STATE_READ_COMPLETE：
   → fill_dequeue_en=1，把 {collided_miss, request} 送给仲裁级
   → l2bi_request_valid=1，仲裁级（4.1）以「重启请求」优先接纳

④ 请求带 l2_fill=1 重走 仲裁→标签→读→更新：
   → 标签级算出 fill_way（LRU 替换路）
   → 读级读出 fill_way 的旧数据；若旧行 dirty && valid → needs_writeback=1
   → 更新级把内存取回的数据写入 fill_way，回 LOAD_ACK 给核
   → pending_miss_cam 见到 l2_fill，清除该行的「正在 fill」记录

⑤ 若第④步发现旧行脏：
   → enqueue_writeback_request=1，{旧地址, 旧数据} 入写回队列
   → 状态机下一轮 IDLE 时优先处理写回：
     STATE_WRITE_ISSUE_ADDRESS → STATE_WRITE_TRANSFER（16 拍写内存）
   → 写回完成，dequeue；若是 flush 触发的写回，还会重启一次 flush 请求
```

#### 4.5.3 源码精读

入队判定是总线接口的入口：

> [l2_axi_bus_interface.sv:L119-L129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L119-L129) —— `enqueue_writeback_request` 在「flush 命中首趟」或「fill 替换脏行」时成立；`enqueue_fill_request` 在真正缺失（非命中、非 fill）且是 LOAD/STORE(含 SYNC) 时成立。

> [l2_axi_bus_interface.sv:L131-L134](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L131-L134) —— 实例化 `l2_cache_pending_miss_cam`，输入当前请求地址，输出 `duplicate_request`（即 `l2bi_collided_miss`）。

> [l2_cache_pending_miss_cam.sv:L61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_pending_miss_cam.sv#L61) —— `duplicate_request = cam_hit && !l2r_l2_fill`：只有「正在 fill 且本次不是回填」才算重复，避免误判。

> [l2_cache_pending_miss_cam.sv:L63-L76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_pending_miss_cam.sv#L63-L76) —— CAM 的更新规则：缺失时写入一个空表项；回填（`l2r_l2_fill`）命中时清除该表项。注释 [L27-L36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_pending_miss_cam.sv#L27-L36) 解释 `QUEUE_SIZE` 必须 ≥ 总线接口队列 + 流水线级数，否则会漏检。

两个 FIFO 用 `sync_fifo` 实现，并设了 `ALMOST_FULL_THRESHOLD` 做提前反压：

> [l2_axi_bus_interface.sv:L142-L177](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L142-L177) —— `pending_writeback_fifo` 与 `pending_fill_fifo`，容量 `FIFO_SIZE=8`，`ALMOST_FULL_THRESHOLD = FIFO_SIZE - L2REQ_LATENCY`（[L85-L90](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L85-L90)）。`L2REQ_LATENCY=4` 是流水线级数，提前 4 拍反压是为了不让已经在流水线里的请求冲爆 FIFO。反压信号汇成 `l2bi_stall`（[L180](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L180)），回传给仲裁级。

> [l2_axi_bus_interface.sv:L91-L92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L91-L92) —— `BURST_BEATS = CACHE_LINE_BITS / AXI_DATA_WIDTH = 512/32 = 16`，即一行需要 16 拍突发；突发长度寄存器减 1 写入 AXI（[L184-L185](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L184-L185)，AXI 规范 length = burst-1）。

> [l2_axi_bus_interface.sv:L213-L296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L213-L296) —— **AXI 状态机**：IDLE 时**写回优先**（[L219-L223](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L219-L223)）；fill 分两路——若是 collided miss 或「整行 store」，直接 `STATE_READ_COMPLETE`（[L226-L243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L226-L243)），否则走读地址→读传输。`STATE_READ_COMPLETE`（[L289-L294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L289-L294)）里 `fill_dequeue_en=1` 把请求回灌仲裁级。

> [l2_axi_bus_interface.sv:L307-L323](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L307-L323) —— 把回灌请求接成 `l2bi_request_valid`：正常 fill 由 `fill_dequeue_en` 触发；若是写回触发的 flush 重启，则强行构造一个 `L2REQ_FLUSH` 重启请求（[L310-L320](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L310-L320)）。

最后看伪 LRU 替换（L2 与 L1D 共用同一模块）：

> [cache_lru.sv:L40-L46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L40-L46) —— 关键设计：**fill 优先于 access**。当同一周期既有 fill 又有 access 时，fill 赢。这既能避免连续 fill 互相驱逐刚装入的行（低效），也能避免两个线程来回驱逐对方行（活锁）。

> [cache_lru.sv:L97-L113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L97-L113) —— **伪 LRU**：用一个二叉树形的标志位记录「指向最久未用路的路径」。每次访问/填充把沿途标志翻转。它比严格 LRU 简单得多（8 路只需 7 位），又足够接近 LRU 效果。路数被硬编码为 1/2/4/8（[L50](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L50)、[L94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L94) 断言）。

#### 4.5.4 代码实践

**实践目标**：定位「缺失入队 → 内存读 → 重灌」的三个关键代码点，画出数据流。

**操作步骤**：

1. 在 [l2_axi_bus_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv) 中找到三处：
   - **入队**：`enqueue_fill_request`（L123）何时为 1？
   - **读内存**：`STATE_READ_ISSUE_ADDRESS` → `STATE_READ_TRANSFER`（L271-L287）如何把 16 拍数据收进 `fill_buffer`（L367-L368）？
   - **重灌**：`STATE_READ_COMPLETE`（L289-L294）如何经 `fill_dequeue_en` 把请求送回仲裁级？
2. 追踪 `fill_buffer` 如何拼成 `l2bi_data_from_memory`（L193-L200），再经仲裁级的 `l2a_data_from_memory`（4.1.3）一路传到更新级的 `l2r_data_from_memory`（4.4.3）。
3. 对比脏行写回路径：`enqueue_writeback_request`（L120）→ `pending_writeback_fifo` → `STATE_WRITE_*`。

**需要观察的现象 / 预期结果**：你会看到 fill 数据走的是一条「总线接口 → 仲裁级（带 `l2_fill` 标志）→ 标签级 → 读级 → 更新级」的完整二次旅程，而脏行写回是另一条独立的「写回队列 → AXI 写通道」旅程，两条路在状态机里以「写回优先」协调。

#### 4.5.5 小练习与答案

**练习 1**：`collided_miss`（重复缺失）为什么能直接跳到 `READ_COMPLETE` 而不读内存？

> **答案**：因为另一条请求正在为同一缓存行做 fill，内存数据马上会被装入 L2。本请求只需等那行填好后重新走一遍流水线（多半会命中，或再次缺失则再来一轮），无需重复占用宝贵的内存带宽。这也避免两路并发 fill 互相覆盖数据。

**练习 2**：为什么 `ALMOST_FULL_THRESHOLD = FIFO_SIZE - L2REQ_LATENCY`，而不是队列真满了才反压？

> **答案**：从仲裁级接受请求到请求真正进入队列要经过若干流水线拍（`L2REQ_LATENCY=4`）。如果等队列「现在」满了才反压，那已经在这 4 拍里进入流水线的请求仍会涌入，导致队列溢出。提前 `L2REQ_LATENCY` 拍反压，恰好让「在途」请求落位后队列刚好不满。

---

## 5. 综合实践

本讲综合实践要求你**绘制 L2 四阶段流水线图**，并标出一次缺失如何被填充、脏行如何被写回。这是把五个最小模块串起来的最好方式。

### 实践目标

把本讲的全局图（见第 4 节开头的 ASCII 图）按你自己的理解重画一遍，并补上「信号名」「队列名」「状态名」等具体细节，做到能对着图向别人讲清一次 L2 缺失的完整生命周期。

### 操作步骤

1. **画骨架**：画出仲裁 → 标签 → 读 → 更新四级，标注每级的输入/输出信号前缀（`l2a_`、`l2t_`、`l2r_`、`l2u_`）。这是 [l2_cache.sv:L107-L112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L107-L112) 的连线。
2. **画总线接口**：在更新级右侧画出 `l2_axi_bus_interface`，里面包含 `pending_fill_fifo`、`pending_writeback_fifo`、AXI 状态机、`pending_miss_cam`。
3. **标缺失填充回路**：用一条彩色箭头从**读级**（`enqueue_fill_request`）指向 fill 队列，再从 `STATE_READ_COMPLETE` 指回**仲裁级**（`l2bi_request_valid` + `l2_fill` 标志），形成一个闭环。在闭环旁注明：数据经 `fill_buffer → l2bi_data_from_memory → ... → l2r_data_from_memory` 最终在更新级写入。
4. **标脏行写回路**：用另一条颜色箭头从读级（`needs_writeback` + `enqueue_writeback_request`）指向写回队列，再经 `STATE_WRITE_*` 指向 AXI 写通道。注明「写回优先于 fill」。
5. **运行单元测试验证理解**：在仓库已构建（`cmake . && make`）的前提下，进入单元测试目录跑 L2 相关用例（命令在「待本地验证」说明里）。观察测试如何驱动请求、检查响应。

### 需要观察的现象 / 预期结果

- 你的图上应清楚体现：**命中请求**走 4 级即出 ACK；**缺失请求**走 4 级后转入总线接口，经内存往返后**从仲裁级重新进入**走第二趟 4 级才完成；**脏行**在第二趟被识别并送入独立的写回路径。
- 单元测试若通过，会输出 `PASS`（见 [tests/unit/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py) 末尾逻辑：输出含 `PASS` 才算通过）。

### 运行命令（待本地验证）

> ⚠️ 以下命令需在本地或容器中、且已执行 `scripts/setup_tools.sh` 安装好 Verilator 与工具链、并完成 `cmake . && make` 后才能运行。本讲义未在当前环境实际执行，结果**待本地验证**。

```bash
# 1. 进入单元测试目录（runtest.py 用 os.listdir('.') 扫描当前目录的 .sv 文件）
cd tests/unit

# 2. 只跑 L2 缓存相关用例（可按文件名过滤）
python3 runtest.py test_l2_cache.sv test_l2_cache_atomic.sv test_l2_cache_pending_miss_cam.sv test_l2_axi_handshake.sv

# 期望：每个用例后跟 verilator 目标，输出 PASS
```

如果只想看注册了哪些用例而不运行，可加 `--list`：

```bash
python3 runtest.py --list
```

> 说明：`runtest.py` 用 Verilator 把单个 `.sv` 模块连同其依赖编译成可执行模型，跑若干周期后在输出里查找 `PASS` 字样判定通过（见 [tests/unit/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py)）。它属于 u15 讲义的「单元测试」层级，对 L2 这种独立模块尤其合适——可见内部信号、周期精确。

---

## 6. 本讲小结

- **L2 是四阶段流水线**：仲裁（选请求）→ 标签（查 PI/PT 标签、读 LRU/dirty/valid）→ 读（判命中、读数据、驱动元数据更新、维护 LL/SC）→ 更新（按字节合并 store、写数据 SRAM、广播响应包）。
- **L2 是物理索引/物理标签**：地址翻译已在 L1 的 TLB 完成，L2 不需要 TLB，因此 `L2_SETS` 不受页大小约束（默认 256 组、8 路、128 KiB）。
- **缺失不是终点，而是「走第二趟」**：读级检测到缺失后，请求进入 fill 队列，经 AXI 从内存取回整行（16 拍 32 位突发），再**从仲裁级重新进入流水线**（带 `l2_fill` 标志）完成填充。
- **脏行走独立的写回路径**：被替换/被 flush 的脏行进入写回队列，经 AXI 写回内存；状态机**写回优先于 fill** 以避免读到陈旧数据。
- **仲裁与替换的关键策略**：重启请求优先于多核请求（防队列积压）；伪 LRU 中 fill 优先于 access（防连续驱逐与活锁）；CAM 合并重复缺失（省带宽、防覆盖）。
- **反压用提前量**：两个 FIFO 都设 `ALMOST_FULL_THRESHOLD = FIFO_SIZE - L2REQ_LATENCY`，提前 4 拍反压，避免在途请求冲爆队列。

---

## 7. 下一步学习建议

- **向外**：读 [l2_axi_bus_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv) 与 [io_interconnect.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv)，进入 u6-l4「AXI 总线与 IO 互连」，看 L2 如何接到 AXI4 系统总线、IO 访问如何与非缓存外设打交道。
- **向并发**：本讲的 LL/SC 同步状态（`can_store_sync`、`load_sync_address`）是 u10-l1「同步内存操作 LL/SC 与 membar」的硬件基础，建议接着读 [l1_store_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv) 把整条原子操作链补全。
- **向多核**：本讲的 `rr_arbiter` 多核仲裁只是入口，u10-l3「多核与 L2 仲裁」会展开多核共享 L2 的完整机制与 multicore 测试约束。
- **向验证**：动手把本讲的 `test_l2_cache.sv` 系列单元测试跑起来（见综合实践），并对比 u15 讲义里「单元测试 vs 整机测试」的定位差异，理解为何 L2 适合用周期精确的单元测试验证。
- **源码延伸**：若对伪 LRU 的位运算细节感兴趣，可逐路推演 [cache_lru.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv) 中 8 路情形的 `fill_way` 与 `update_flags` 真值表，体会「树形伪 LRU」的简洁。
