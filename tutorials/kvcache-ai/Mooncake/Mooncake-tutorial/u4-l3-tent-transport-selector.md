# TENT TransportSelector：声明式路径选择

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **TransportSelector** 是「做什么的选择」：它把一次传输请求的上下文（段类型、是否同机、内存类型、大小、优先级）映射到「用哪种 transport + 允许哪些 NIC」。
2. 看懂「**声明式**（declarative）」的含义：选择规则不是写死在 C++ 代码里，而是由一段 JSON `policy` 数组描述，按出现顺序首条匹配即生效。
3. 理解 **切片喷射** 的两条路径：
   - 沿「多条 transport」喷射：通过 `transport_index` / failover 在策略的 `transports` 列表里逐条回退；
   - 沿「多张 NIC」喷射：通过 `device_mask` 限定候选 NIC 集合，再由 `DeviceSelector` 按负载/拓扑把切片分配到具体网卡。
4. 掌握 **transport_loader** 的动态加载机制：transport 实例不是无条件创建，而是「编译开关 + 配置开关 + 运行时硬件探测」三者同时为真时才装载。

> 本讲是 [u4-l1 TENT 总览](u4-l1-tent-overview.md) 的后续。阅读本讲前，请确认你已了解 TENT 里 Request、Batch、Segment、Transport 这些基本概念。

---

## 2. 前置知识

### 2.1 什么是 transport（传输后端）

在 TENT 里，「transport」是一个抽象接口，代表**一种把字节从 A 搬到 B 的具体手段**。常见的 transport 有：

| 名称 | 典型用途 |
|------|----------|
| `rdma` | 跨机 RDMA 网卡（RoCE/InfiniBand）传输 |
| `tcp` | 通用 TCP 回退 |
| `shm` | 同机共享内存 |
| `nvlink` / `mnnvl` | GPU 间 NVLink（同机 / 跨机 NVLink） |
| `gds` / `io_uring` | GPU 直存文件 / 异步文件 IO（用于 file 段） |
| `ascend` / `sunrise_link` | 昇腾 NPU / 自研互联 |

它们在代码里是一个枚举，每种 transport 占一个数组下标：

[mooncake-transfer-engine/tent/include/tent/common/types.h:46-61](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/common/types.h#L46-L61) —— 定义 `TransportType` 枚举与 `kSupportedTransportTypes` 上限。

### 2.2 为什么需要「选择」

一台机器可能同时装了 RDMA 网卡、TCP 网卡，又有 NVLink、共享内存。一次传输到底走哪条路？硬编码会导致：

- 想临时把高优流量限定到某张卡，要改代码重编译；
- GPU↔GPU 的流量误走 TCP，性能暴跌；
- 不同业务（高优 / 低优批量）无法分流。

所以 TENT 把「选路」这件事**外置成一段配置（policy）**，由 `TransportSelector` 在运行时解读。这就是「声明式」：你**声明**规则，引擎**执行**规则。

### 2.3 三层信息：拓扑、负载、策略

后续你会反复看到「Selector 依据什么决策」。本讲把信息来源分成三层：

- **策略（policy）**：用户写的 JSON 规则，决定候选 transport 与候选 NIC 集合。
- **拓扑（topology）**：机器物理结构——NIC 名称↔ID 映射、NIC 到内存的 NUMA 亲和性。
- **负载（load）**：每张 NIC 当前的在途字节数、历史带宽（EWMA）。

记住一个**关键分工**：`TransportSelector` 主要用「策略 + 拓扑」圈定**候选范围**；而「负载」由下游的 `DeviceSelector`（RDMA 传输内部）用来在候选范围内**按比例切分切片**。这个分工是本讲最容易踩的坑，第 4.2 节会专门讲清楚。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h` | `SelectionContext`/`SelectionPolicy`/`SelectionResult` 数据结构与 `TransportSelector` 类声明 |
| `mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp` | 选择核心实现：策略加载、策略匹配、可用性检查、`select()` |
| `mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp` | `TransferEngineImpl::loadTransports()`：按配置与编译开关装载各 transport |
| `mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp` | 引擎侧调用点：`getTransportType`/`resolveTransport`、切片分类提交、failover 重试 |
| `mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp` | `DeviceSelector::allocate`/`buildCandidates`：在 `device_mask` 内按负载+拓扑给切片选 NIC |
| `mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp` | `RdmaTransport::submitTransferTasks`：把 Request 切成 RdmaSlice 并调用 `allocate` |
| `mooncake-transfer-engine/tent/include/tent/transport/rdma/slice.h` | `RdmaSlice` 结构：一个切片的字段（含 `source_dev_id`） |
| `mooncake-transfer-engine/tent/tests/transport_selector_test.cpp` | 单元测试，是本讲代码实践的主要依据 |
| `docs/source/design/tent/transport-selector.md` | 官方设计文档，含完整配置示例 |

---

## 4. 核心概念与源码讲解

### 4.1 TransportSelector：策略化的声明式选择

#### 4.1.1 概念说明

`TransportSelector` 解决的问题是：**给定一次传输的上下文，返回「用哪个 transport、允许用哪些 NIC」**。

它的输入是一个 `SelectionContext`（谁发的、什么内存、多大、什么优先级），输出是一个 `SelectionResult { transport, device_mask }`。整套规则来自 JSON 配置里的 `policy` 数组，**按数组顺序，第一条匹配的 policy 生效**（first-match-wins）。

三个核心数据结构定义在头文件里：

[mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h:74-85](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L74-L85) —— `SelectionContext`：选择上下文（段类型、是否同机、本/远端内存类型、传输大小、优先级、可选的策略名绑定）。

[mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h:90-120](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L90-L120) —— `SelectionPolicy`：一条策略规则（过滤条件 + 候选 transport 列表 + 允许的设备名）。

[mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h:125-128](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L125-L128) —— `SelectionResult`：选择结果（transport 类型 + 64 位 NIC 位掩码）。

注意 `SelectionPolicy` 里大量字段是 `std::optional`：`nullopt` 表示「不关心这一项」，这就是 pattern-based（模式匹配）过滤的来源。

#### 4.1.2 核心流程

`TransportSelector::select()` 的执行过程可以用下面的伪代码概括：

```
select(context, available_transports, transport_index, hint):
    1. 遍历 policies_，找到第一条 matchesPolicy(policy, context) 为真的策略
       —— 找不到 → 返回空结果 (transport=UNSPEC)，下游判定失败
    2. 把策略里的 devices（网卡名列表）转成 device_mask 位掩码
       —— 空列表 → mask = ~0ULL（全部 NIC）
       —— 用 topology_->getNicId(name) 把名字翻成 bit 位
    3. 确定「原始候选 transport 序列 raw」：
       —— 策略显式列了 transports → 用策略的
       —— 否则 → 用 buffer 注册时声明的 buffer_transports
    4. reorderWithHint(raw, hint)：若有 hint，把 hint 提到最前；hint 不在 raw 里 → 返回 nullopt（拒绝）
    5. 在重排后的候选里，跳过 isTransportAvailable() 为假的，找到第 transport_index 个可用 transport
    6. 返回 { transport, device_mask }
```

**匹配的判定顺序**（`matchesPolicy`）很关键，它决定了「一条 policy 会不会命中这次请求」：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:247-304](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L247-L304) —— 逐项检查 `policy_name` 绑定、`segment_type`、`same_machine`、本/远端内存模式、`min/max_size`、`priority`（精确匹配）。任何一项不满足就返回 false。

> 注意优先级是**精确匹配**（`context.priority_level != policy.priority.value()` 即不命中），不是范围匹配。这正是文档里「`priority` field filters which requests match」的含义。

#### 4.1.3 源码精读

**(a) 策略加载：从 JSON 到内存结构**

构造函数触发 `loadPolicies()`：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:216-219](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L216-L219) —— 构造即加载策略。

`loadPolicies()` 先读 `config` 里的 `policy` 子树；如果为空，就回退到两套**默认策略**（保持与历史硬编码行为一致）：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:71-99](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L71-L99) —— 默认策略：file 段走 `[GDS, IOURING]`；memory 段用空 transports（表示沿用 `buffer_transports` 顺序）。

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:116-213](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L116-L213) —— 逐条解析 JSON policy，其中 `priority` 既支持字符串 `"high"/"medium"/"low"`，也支持整数 `0/1/2`（做大小写不敏感处理后映射到 `PRIO_HIGH/MEDIUM/LOW`）。

**(b) 候选 transport 序列与 fallback**

`select()` 里有一段决定「raw 候选序列」的关键三元表达式：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:407-411](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L407-L411) —— 优先用策略显式声明的 transports；否则回退到 buffer 注册时声明的 `buffer_transports`；都没有则空。

随后 `transport_index` 就是在这个序列里「数到第几个」——`transport_index=0` 取首选，`=1` 取次选（回退），以此类推。具体遍历见：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:419-435](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L419-L435) —— 跳过不可用的候选，命中第 `transport_index` 个即返回；找不到返回 UNSPEC。

**(c) transport 可用性检查：能力位 + 同机约束**

光在候选列表里还不够，还要确认该 transport「真的能干这件事」：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:306-360](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L306-L360) —— `isTransportAvailable()`：先看 transport 是否已装载，再看 `NVLINK/SHM` 必须同机，最后按本/远端内存类型组合查 transport 的能力位（`dram_to_dram`、`gpu_to_gpu`、`dram_to_gpu` 等）。

**(d) device_mask：网卡名 → 位掩码**

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:387-402](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L387-L402) —— 默认 `~0ULL`（全部 NIC）；若策略列了 `devices`，则用 `topology_->getNicId(name)` 把名字转成 ID，按位 OR 进 64 位掩码。找不到或 ID≥64 会告警。这一步把「策略」与「拓扑」第一次结合：策略给名字，拓扑给 ID。

#### 4.1.4 代码实践

**实践目标**：通过阅读单元测试，验证「file 段默认首选 GDS」「memory 段沿用 buffer_transports 顺序」这两条默认策略，并亲手改一个断言观察失败现象。

**操作步骤**：

1. 打开测试文件 [mooncake-transfer-engine/tent/tests/transport_selector_test.cpp:78-107](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/transport_selector_test.cpp#L78-L107)。这是一个 `DefaultPoliciesFileSegment` 测试：它构造一个空的 `Config`（因此走默认策略），塞入带 `dram_to_file=true` 能力位的 GDS/IOURING 假 transport，然后断言 `result.transport == GDS`。
2. 再看 [mooncake-transfer-engine/tent/tests/transport_selector_test.cpp:140-169](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/transport_selector_test.cpp#L140-L169) `DefaultPoliciesMemorySegment`：memory 段默认策略 transports 为空，于是用 `buffer_transports = {RDMA, TCP}`，断言首选是 RDMA。
3. 阅读失败转移测试 [mooncake-transfer-engine/tent/tests/transport_selector_test.cpp:367-401](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/transport_selector_test.cpp#L367-L401) `PriorityOffsetFallback`：同一个上下文，`transport_index=0` 得 RDMA，`=1` 得 TCP，`=2` 得 UNSPEC。这直接证明了「列表内逐条回退」。
4. （可选修改）把 `PriorityOffsetFallback` 里的 `selector.select(ctx, transports, 1)` 的期望从 `TCP` 临时改成 `RDMA`，重新编译运行测试。

**需要观察的现象**：第 4 步修改后，该测试用例应当**失败**，GTest 输出形如 `Expected: RDMA, Actual: TCP`。

**预期结果**：还原修改后所有用例通过。若你无法本地编译运行（依赖 RDMA/CUDA 环境），明确写「待本地验证」——不要假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：如果两条 policy 都能匹配同一个请求，会选哪一条？为什么？

> **答案**：选 JSON 数组里**靠前**的那一条。`select()` 在 [transport_selector.cpp:371-376](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L371-L376) 里 `break; // First match wins`，所以 policy 的书写顺序就是优先级。

**练习 2**：策略里 `priority` 字段填 `"low"`，来了一条 `priority_level=PRIO_HIGH` 的请求，会命中吗？

> **答案**：不会。`matchesPolicy` 对 priority 做**精确匹配**（[transport_selector.cpp:297-301](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L297-L301)），`PRIO_HIGH != PRIO_LOW`，该 policy 不命中。请求会继续尝试下一条 policy；若全部不命中则返回 UNSPEC。

**练习 3**：`devices` 字段为空数组和完全不写 `devices`，行为有区别吗？

> **答案**：没有区别。两者都使 `matching_policy->devices.empty()` 为真，进入 [transport_selector.cpp:388-389](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L388-L389) 的默认分支，`device_mask = ~0ULL`（全部 NIC）。

---

### 4.2 切片喷射：transport_index、failover 与多 NIC 分发

#### 4.2.1 概念说明

「切片喷射」是本讲的第二个核心。它其实包含**两个维度**的「喷射」，初学者很容易混为一谈，必须分开理解：

1. **沿多条 transport 喷射（跨后端回退）**：一条策略的 `transports` 是一个**有序列表**（如 `["rdma", "tcp"]`）。首选 transport 失败时，引擎把这次任务「喷射」到列表里的下一条 transport。控制这个维度的是 `transport_index` 参数和 failover 机制。
2. **沿多张 NIC 喷射（网卡的负载分担）**：当选中的 transport 是 RDMA 时，一次 Request 会被切成多个 `RdmaSlice`，这些切片被分发到**多张** RDMA 网卡上并行传输。控制这个维度的是 `device_mask`（候选 NIC 集合）和 `DeviceSelector`（按负载/拓扑在集合内分配）。

> **关键澄清（本讲最重要的点）**：`TransportSelector` **不直接决定**多张 NIC 之间的切片比例。它只通过 `device_mask` **圈定候选 NIC 集合**（用策略的 `devices` 名字 + 拓扑 `getNicId`）。真正按比例把切片分发到具体 NIC 的，是 RDMA 传输内部的 `DeviceSelector`，它才用到「负载（在途字节 + EWMA 带宽）」和「拓扑（NUMA rank）」。换句话说：**策略定范围，DeviceSelector 定比例**。

#### 4.2.2 核心流程

**A. 引擎侧如何调用 selector（首次选择）**

引擎在提交任务时，为每个（合并后的）request 调用一次 `resolveTransport(..., 0)`（index=0 即首选）：

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1265-1267](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1265-L1267) —— `resolveTransport(merged_request, 0)` 得到 `select_result`，把 `transport` 和 `device_mask` 写进 task。

`resolveTransport` 内部会调用 `getTransportType`，后者构建 `SelectionContext` 并把一切交给 `TransportSelector::select()`：

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:914-916](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L914-L916) —— 把 `transport_list_`、`transport_index`、`hint` 一并传给 selector。

**B. 沿多 transport 喷射（failover）**

当某次传输 `FAILED` 时，`updateTaskStatusAfterPoll` 触发 `resubmitTransferTask`，它**递增 transport_index**，把任务喷射到下一条 transport：

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1390-1403](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1390-L1403) —— `++task.failover_count` 先做上限保护（`max_failover_attempts_`，默认 3）；随后 `task.xport_priority = task.failover_count`，再用这个新 index 调 `resolveTransport`，得到下一条候选 transport。

这就是「喷射到多条 transport」的物理实现：**每次失败，index +1，重新 select，落到候选列表的下一个元素**。

**C. 沿多 NIC 喷射（DeviceSelector 分配比例）**

在 RDMA 传输内部，`submitTransferTasks` 先把 request 切成 N 个 slice，再调 `DeviceSelector::allocate(..., device_mask)` 给每个 slice 选一张 NIC：

[mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp:435-438](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp#L435-L438) —— `allocate(request.length, num_slices, block_size, source_location, slice_dev_ids, request.priority, batch->device_mask)`，注意最后一个参数就是 selector 算出的 `device_mask`。

[mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp:463-464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp#L463-L464) —— 把 `allocate` 返回的逐切片 NIC 列表写进 `slice->source_dev_id`。

分配的核心打分公式在 `DeviceSelector::buildCandidates` 里，它综合了**负载**与**拓扑**：

[mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:123-147](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L123-L147) —— 对每张候选 NIC 计算 `predicted_time = (inflight + slice_bytes) / ewma_bw`（负载：在途字节越多、历史带宽越低，预测完成时间越长），再乘以 `numa_tier_weights[rank]`（拓扑：跨 NUMA 的 NIC 加惩罚），得分越低越优先；并叠加随机抖动避免羊群效应。

而 `device_mask` 在两处把「不在策略允许范围内」的 NIC 滤掉：

[mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:150-163](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L150-L163) —— 第一遍先按拓扑 rank 遍历 `entry->device_list[rank]`，跳过 `device_mask` 未置位的 NIC；若过滤后为空，再回退到全部允许的 NIC（[quota.cpp:166-174](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L166-L174)）。

把负载量化成「优先」程度，可以用一个简单公式理解 DeviceSelector 的倾向（分母越大越快、分子越大越忙则越靠后）：

\[
\text{score} = \underbrace{\frac{\text{inflight} + \text{slice\_bytes}}{\text{ewma\_bw}}}_{\text{负载项：预测完成时间}} \times \underbrace{w_{\text{numa}}(\text{rank})}_{\text{拓扑项：NUMA 惩罚}} + \text{jitter}
\]

得分最低的 NIC 优先承接切片，于是**切片分配比例**自然偏向「更空闲、NUMA 更近」的网卡。

#### 4.2.3 源码精读

**切片是什么**：一个 `RdmaSlice` 代表 request 的一段连续字节区间，附带它要走的源/目标 NIC：

[mooncake-transfer-engine/tent/include/tent/transport/rdma/slice.h:72-100](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/slice.h#L72-L100) —— `RdmaSlice` 含 `source_addr/target_addr/length`、`source_dev_id/target_dev_id`、所属 `task`、优先级等。多个 slice 共同归属一个 `RdmaTask`，全部成功 task 才算成功（见同文件 `updateSliceStatus`）。

**hint（单请求钉选）如何叠加在策略之上**：`reorderWithHint` 把 hint 提到候选列表最前；failover 时引擎传入「排除 hint」的语义，保证失败后不会回到同一条 transport：

[mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp:444-457](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L444-L457) —— hint 为 UNSPEC 直接返回原序列；否则把 hint 放首位、其余跟在后面；若 hint 不在 raw 里则返回 `nullopt`（拒绝，下游转 FAILED）。

对应的语义可对照测试 [transport_selector_test.cpp:554-587](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/transport_selector_test.cpp#L554-L587) `FailoverFromHintAdvancesByIndex`：策略序列 `[TCP, RDMA]`、hint=RDMA 时，index=0 得 RDMA，index=1 得 TCP（不会再回到 RDMA），index=2 得 UNSPEC。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「跨多 NIC 传输」的完整决策链，回答 spec 提出的问题——Selector 依据哪些信息决定切片分配比例。这是一道**源码阅读型实践**，不需要运行。

**操作步骤**：

1. 从策略侧出发：阅读 [transport-selector.cpp:387-402](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L387-L402)，确认 `device_mask` 由「策略 `devices` 名单」+「拓扑 `getNicId`」共同决定。这一步只用到了**策略 + 拓扑**。
2. 跨进 RDMA 传输：阅读 [rdma_transport.cpp:435-464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp#L435-L464)，确认 `device_mask` 被原样传给 `DeviceSelector::allocate`，并最终落到每个 slice 的 `source_dev_id`。
3. 看真正的「比例」决策：阅读 [quota.cpp:123-179](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L123-L179)，把打分公式里的每一项对应到信息来源。
4. 写出你的结论（见下方「预期结果」）。

**需要观察的现象**：你应当能在三个文件里分别找到「策略」「拓扑」「负载」三类信息的具体使用点，并意识到它们**分散在两个组件**里。

**预期结果**：用下面这张表组织你的答案（这正是 spec 要你描述的内容）：

| 信息层 | 来源 | 用在哪一步 | 决定了什么 |
|--------|------|-----------|-----------|
| 策略 policy | 用户 JSON `policy[].devices` / `transports` | `TransportSelector::select` | 候选 transport 列表、候选 NIC 名单 |
| 拓扑 topology | `Topology::getNicId` / `entry->device_list[rank]`（NUMA 亲和） | `select`（名→ID）、`buildCandidates`（NUMA rank） | NIC 位掩码、跨 NUMA 的惩罚权重 \( w_{\text{numa}} \) |
| 负载 load | `dev.getInflightBytes()` / `dev.getEwmaBandwidth()` | `DeviceSelector::buildCandidates` | 预测完成时间，决定切片在各 NIC 间的**比例** |
| QoS priority | `request.priority` + `enable_priority_filtering` | `buildCandidates` 的第一遍过滤 | 高优请求可能排除正在服务低优的 NIC |

结论一句话：**TransportSelector 用「策略 + 拓扑」圈定候选 NIC 集合（device_mask），DeviceSelector 再用「负载（在途字节+EWMA 带宽）+ 拓扑（NUMA rank）+ QoS」在该集合内按预测完成时间给切片定比例。**

#### 4.2.5 小练习与答案

**练习 1**：`max_failover_attempts_` 默认是 3。一个候选列表为 `[rdma, tcp]` 的策略，最多能尝试几次不同的 transport？

> **答案**：最多 2 种 transport，但 failover 计数上限是 3。首次用 index=0（rdma），失败后 index=1（tcp），再失败 index=2（UNSPEC）。当 `++task.failover_count > 3` 时彻底放弃（[transfer_engine_impl.cpp:1390-1395](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1390-L1395)）。所以「尝试次数」受候选列表长度和 failover 上限**双重**约束。

**练习 2**：如果策略 `devices` 只写了 `["mlx5_0"]`，但机器上 mlx5_0 此刻在途字节非常高，切片还会分到别的 NIC 吗？

> **答案**：不会。`device_mask` 只置了 mlx5_0 这一位，`buildCandidates` 会把其余 NIC 全部滤掉（[quota.cpp:153](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L153)）。策略的硬性约束优先于负载优化——这正是「策略定范围」的体现。

**练习 3**：为什么 `buildCandidates` 的打分要加 `jitter`（随机抖动）？

> **答案**：若所有切片都严格按相同分数选「当前最优」NIC，会出现羊群效应—— everyone 同时涌向同一张卡，反而把它打满。加抖动（[quota.cpp:139-140](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L139-L140)）让并发请求在分数相近时分散选择，起到负载均衡的作用。

---

### 4.3 transport_loader：transport 的动态加载

#### 4.3.1 概念说明

`TransportSelector` 决定「用哪个」，但「有哪些可选」是由 **transport_loader** 决定的。它负责在引擎启动时，把 transport 实例**按需创建**并填进 `transport_list_` 数组。

这里的「动态」体现在**三重门槛**必须同时通过，一个 transport 才会被装载：

1. **编译开关**：例如 `USE_RDMA`、`USE_CUDA`、`USE_GDS`。没有相应硬件/依赖，编译期就不会包含对应代码。
2. **配置开关**：例如 `transports/rdma/enable`，默认值因 transport 而异（rdma/tcp 默认 true，shm/gds 默认 false）。
3. **运行时硬件探测**：例如 RDMA 要求 `topology_->getNicCount(NIC_RDMA) > 0`；CUDA 下 MNNVL 与 NVLink 二选一（由环境变量 `MC_ENABLE_MNNVL` 控制）。

#### 4.3.2 核心流程

```
TransferEngineImpl::loadTransports():
  对每种 transport:
    if 编译宏未定义:        跳过            # 编译期
    elif 配置 enable == false: 跳过          # 配置期
    elif 运行时硬件不满足:    跳过            # 运行期
    else:
      transport_list_[TYPE] = make_shared<XxxTransport>()
```

装载完成后，`transport_list_` 就是一个「稀疏数组」：只有被装载的下标非空。`TransportSelector::isTransportAvailable` 里的 `if (!transport) return false;` 正是在检查这个数组（[transport_selector.cpp:316-319](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L316-L319)）。

#### 4.3.3 源码精读

`loadTransports()` 的全貌在 transport_loader.cpp：

[mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp:47-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L47-L98) —— 逐个 transport 判断「编译开关 + 配置开关 + 硬件探测」，三者满足才 `make_shared`。

几个值得注意的细节：

- **TCP/SHM**：[transport_loader.cpp:48-53](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L48-L53) TCP 默认开（`enable=true`），SHM 默认关（`enable=false`）；SHM 旁有一句 `TODO affect the end-to-end performance because it is not numa aware`，提示其尚未做 NUMA 优化。
- **RDMA**：[transport_loader.cpp:55-60](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L55-L60) 除了配置开关，还要 `topology_->getNicCount(Topology::NIC_RDMA)` 非零——典型的运行时硬件探测。
- **MNNVL vs NVLink**：[transport_loader.cpp:67-76](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L67-L76) 二者互斥，由环境变量 `MC_ENABLE_MNNVL` 决定装哪一个。
- **文件头部的条件 include**：[transport_loader.cpp:19-42](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L19-L42) 各种 `#ifdef USE_XXX` 把对应 transport 的头文件包进来——编译开关的第一道关卡就在这里。

> 这个文件名是 `transport_loader.cpp`，但它实现的是 `TransferEngineImpl::loadTransports()`（include 的是 `transfer_engine_impl.h`）。这正是「装载逻辑单独成文件」的组织方式。

#### 4.3.4 代码实践

**实践目标**：搞清楚「我想启用 SHM transport，需要满足哪些条件」，并对照默认配置验证。

**操作步骤**：

1. 阅读 [transport_loader.cpp:52-53](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L52-L53)，确认 SHM 默认 `enable=false`。
2. 反推启用 SHM 的三个条件：① 编译时定义了 `USE_XXX`（SHM 走 SHM 路径，对应头文件 [transport_loader.cpp:16](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L16) 已无条件包含 `shm_transport.h`，门槛较低）；② 配置 `transports/shm/enable=true`；③ 代码里 SHM 没有额外硬件探测，所以前两条满足即可。
3. 对照 RDMA 的三条件（[transport_loader.cpp:55-60](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L55-L60)）：`USE_RDMA` + `transports/rdma/enable=true` + `getNicCount(NIC_RDMA)>0`。

**需要观察的现象**：你会注意到不同 transport 的「门槛数量」不一样——TCP 最宽松，RDMA 多一道硬件探测，GDS 默认关闭。

**预期结果**：能复述每种 transport 的启用条件；若要实际改配置运行，写「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：一台没有 RDMA 网卡的机器，配置里写 `transports/rdma/enable=true`，会装载 RdmaTransport 吗？

> **答案**：不会。虽然配置开关为真，但 [transport_loader.cpp:56-57](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L56-L57) 还要求 `getNicCount(NIC_RDMA)` 非零。硬件探测是最后一道闸。

**练习 2**：为什么 SHM 和 NVLINK 在 selector 里被特别限制为「必须同机」？

> **答案**：因为 SHM（共享内存）和 NVLink 本质上是**单机内**的互联介质，无法跨机。`isTransportAvailable` 在 [transport_selector.cpp:322-324](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L322-L324) 显式判断 `(type==NVLINK||type==SHM) && !context.same_machine` 时返回 false。loader 装了也没用，selector 会在跨机请求里把它排除。

**练习 3**：设置环境变量 `MC_ENABLE_MNNVL` 后，NVLink transport 还会装载吗？

> **答案**：不会，二者互斥。[transport_loader.cpp:68-75](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L68-L75) 是 `if (enable_mnnvl) {...} else {...}` 的二选一结构，开了 MNNVL 就走 MNNVL 分支，NVLink 分支被跳过。

---

## 5. 综合实践

**任务**：为下面这个真实场景，写出完整的「选路 + 喷射」决策过程，并指出每个决策点对应的源码位置。

> 场景：节点 A 有一张 CPU 内存 buffer，要写到节点 B 的 CPU 内存 buffer。机器配了 3 张 RDMA 网卡 `mlx5_0/mlx5_1/mlx5_2`，其中 `mlx5_2` 跨 NUMA。配置 policy 如下：
> ```json
> { "policy": [ { "name": "p1", "segment_type": "memory",
>     "devices": ["mlx5_0","mlx5_1","mlx5_2"], "transports": ["rdma","tcp"] } ] }
> ```
> 传输过程中 rdma 失败了一次。

**要求**：

1. 说明 `loadTransports` 装载了哪些 transport（[transport_loader.cpp:55-60](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L55-L60)、[48-49](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_loader.cpp#L48-L49)）。
2. 说明首次 `select(index=0)` 命中哪条 policy、得到哪个 transport、`device_mask` 是多少（[transport_selector.cpp:371-402](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L371-L402)）。
3. 说明 request 进入 RDMA 后，3 张 NIC 的切片比例由哪些量决定（[quota.cpp:123-147](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L123-L147)），尤其指出 mlx5_2 因跨 NUMA 会乘以更大的 \( w_{\text{numa}} \)。
4. 说明 rdma 失败后，failover 如何把任务喷射到 tcp（[transfer_engine_impl.cpp:1390-1403](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1390-L1403)）。

**参考答案要点**：

- 装载：TCP（默认开）+ RDMA（编译开 + 配置开 + 有 NIC）。`device_mask = mlx5_0|mlx5_1|mlx5_2` 三位置位。
- 首选 transport = rdma（`transports[0]`）。切片在 3 张卡间按 `predicted_time × numa_weight` 分配，mlx5_2 因 rank>0 吃 NUMA 惩罚，承接比例更低。
- rdma 失败 → `failover_count=1`，`xport_priority=1`，`resolveTransport(index=1)` 返回 tcp，任务被喷射到 TCP transport 继续传输；若 tcp 再失败且超出 `max_failover_attempts_`，任务最终 FAILED。

---

## 6. 本讲小结

- **TransportSelector 是声明式选路器**：输入 `SelectionContext`，按 JSON `policy` 数组首条匹配，输出 `{ transport, device_mask }`；无 policy 时回退两套默认策略。
- **匹配是 pattern-based 且 priority 精确匹配**：`optional` 字段为「不关心」，`segment_type/same_machine/内存类型/大小/priority` 逐项过滤。
- **「切片喷射」有两个维度**：沿多条 transport（`transport_index` + failover）回退；沿多张 NIC（`device_mask` + `DeviceSelector`）负载分担。**两者不要混淆**。
- **策略定范围，DeviceSelector 定比例**：TransportSelector 用策略+拓扑圈定候选 NIC；真正按负载（在途字节+EWMA）和拓扑（NUMA rank）给切片定比例的是 RDMA 内部的 DeviceSelector。
- **transport_loader 三重门槛**：编译开关 + 配置开关 + 运行时硬件探测，三者皆真才装载；装载结果是一个稀疏的 `transport_list_` 数组。
- **hint 是单请求级钉选**：叠加在策略之上，failover 时会被排除，避免回到失败的同一条 transport。

---

## 7. 下一步学习建议

1. **深入 DeviceSelector 的比例算法**：本讲只点到 `selectMultiPath`/`selectSinglePath`，建议接着读 [quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp) 的多路径选择与 EWMA 带宽更新，理解「比例」如何在运行时动态收敛。
2. **阅读 QoS 设计文档**：`docs/source/design/tent/qos.md` 讲解优先级如何贯穿 TransportSelector → DeviceSelector → Worker 调度队列，是本讲 priority 维度的完整闭环。
3. **看一个真实 transport 的实现**：以 `rdma_transport.cpp` 的 `submitTransferTasks` 为入口，跟踪切片如何进入 worker 队列、如何被 post 到具体 QP，把「Selector 选完之后」的执行路径补全。
4. **动手写一条 policy**：参照 [docs/source/design/tent/transport-selector.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/transport-selector.md) 的 Complete Example，为某种业务（如「高优小消息走 NVLink+SHM」）设计一条 policy，并预测它会命中哪条规则。
