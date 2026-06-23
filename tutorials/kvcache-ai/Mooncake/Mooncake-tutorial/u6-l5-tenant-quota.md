# 多租户配额（Tenant Quota）

## 1. 本讲目标

上一讲（u5-l2）我们钻进了 `MasterService` 这个「元数据大脑」，看到它维护着按租户（tenant）切分的对象元数据，并且只有一个**全局**的 `quota_bytes_` 容量上限。本讲往前再走一步：当集群里同时跑着多个团队 / 多个业务（每个都是一个 tenant），我们如何**按租户**公平、可控地瓜分这块共享的 KV Cache 容量？

这就是「多租户配额（Tenant Quota）」要解决的问题。Mooncake 在 `mooncake-store` 里提供了一个独立、可单测的记账组件 `TenantQuotaTable`。学完本讲你应该能够：

1. 说清 `TenantQuotaTable` 内部维护的每个字段（`requested / effective / used / reserved / committed_count / has_explicit_policy / over_quota`）各自代表什么、由谁写入、由谁读取。
2. 掌握 **reserve / commit / abort 三阶段记账模型**：为什么配额需要「先预留再确认」？预留失败、确认对不上账时分别会发生什么？
3. 完全理解**有效配额（effective quota）的重算**：一套基于「最大余数法」的容量分配算法如何把集群总容量 `allocatable_capacity_bytes` 在显式配额租户和默认租户之间仲裁，以及为什么它对 `unsigned __int128` 防溢出如此执着。
4. 看懂分配失败时的**回滚路径**：`Abort` 如何释放预留、`over_quota` 标志如何在容量缩水后被动点亮。
5. 诚实判断本组件当前的**集成状态**：它已经是一个被完整单测覆盖的库组件，但截至本讲 HEAD **尚未**被 `MasterService` 调用——这一点我们会讲清楚边界，不编造不存在的调用链。

> 本讲聚焦「配额记账与仲裁算法」本身。它和 `PutStart` 分配、淘汰（eviction）等流程的端到端串联，目前是规划中的集成点，本讲只点到为止。

## 2. 前置知识

本讲默认你已经学完：

- **u5-l2 MasterService**：你需要知道 Store 的元数据是按 **tenant** 命名空间切分的（`ObjectIdentity = {tenant_id, user_key}`），所有对象都归属于某个 tenant；以及 `NormalizeTenantId` 会把空字符串 tenant 归一化成 `"default"`。本讲讲的配额表，正是建立在这套 tenant 抽象之上的「容量层」。

此外，先建立两个直觉。

### 什么是「配额（quota）」与「三阶段记账」？

> 想象一个合租公寓，公共储物间一共 1000 升。三个室友 A/B/C 约定：A 要 500 升、B 要 300 升、C 随便用剩下的。
>
> 现在你要往储物间搬一个大箱子。你**不能**直接搬——你得先跟管理员「**预订**」：「我要占 80 升。」管理员在账本上记一笔 *A 预留 80*，这样别人就不会把这块空间也订走。等你真的把箱子摆好，你再「**确认**」：「80 升落实了。」管理员把 *预留* 转成 *已用*。如果你临时不搬了，你「**中止**」这次预订，管理员把 *预留* 抹掉，空间立刻释放回池子里。

这就是 reserve / commit / abort 三阶段模型。它的核心动机是：**真实的 Put 是异步两段的**（`PutStart` 分配 → 数据传输 → `PutEnd` 确认），在 `PutStart` 和 `PutEnd` 之间存在一个「我宣称要用但还没真正落账」的窗口。预留（reserved）正是用来占用这个窗口里的容量，避免多个并发请求互相超卖。

### `effective` 和 `requested` 为什么不是同一个数？

- `requested`：租户**声明想要**的容量（管理员答应给 A 500 升）。
- `effective`：租户**当前实际被授予**的容量。当集群总容量不够兑现所有人的 `requested` 时，`effective` 会按比例缩水。

`effective` 才是真正用来做「能不能再 reserve」的判断依据。本讲的核心算法，就是讲 `requested` 如何在总容量约束下被仲裁成 `effective`。

### `tl::expected<void, TenantQuotaError>` 是什么？

和 u5-l2 见过的 `tl::expected<T, ErrorCode>` 完全同构，只是错误类型换成了 `TenantQuotaError`（成功返回空值，失败返回配额错误码）。读到这种签名就读作「成功无返回，失败给一个配额错误」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-store/include/tenant_quota.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h) | `TenantQuotaTable`、`TenantQuotaState`、`TenantQuotaSnapshot`、`TenantQuotaError` 的声明 | 本讲的「骨架」：所有字段含义和公开 API 一览 |
| [mooncake-store/src/tenant_quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp) | `TenantQuotaTable` 全部方法实现，含有效配额分配算法 | 本讲的「血肉」：reserve/commit/abort 的记账逻辑、最大余数法分配 |
| [mooncake-store/tests/tenant_quota_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp) | 该组件的完整 GTest 单测 | 本讲的「行为说明书」与代码实践的蓝本：边界、异常、溢出全在这里钉死 |
| [mooncake-store/include/master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h) | `MasterService` 类声明（全局 `quota_bytes_`、`TenantState`、`NormalizeTenantId` 使用点） | 本讲的**集成上下文**：讲清配额表未来要挂在哪里、当前为何还是「孤岛」 |
| [mooncake-store/include/types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h) | `NormalizeTenantId` 定义 | 配额表和元数据层共享同一个 tenant 归一化规则 |

> 一个**重要且容易踩的坑**：截至本讲 HEAD（`1f7f71a`），`tenant_quota.cpp` 已经被编译进 store 库（见 [mooncake-store/src/CMakeLists.txt:19](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/CMakeLists.txt#L19)），也已被 [mooncake-store/tests/CMakeLists.txt:63](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/CMakeLists.txt#L63) 注册为单测 `tenant_quota_test`，但 **`MasterService` 里还没有任何地方调用它**（全仓搜不到 `.Reserve(` / `RecomputeEffectiveQuotas` 出现在 `master_service.cpp`）。换句话说，这是一个「内核已就绪、接线尚未完成」的组件。本讲讲清它的**自身逻辑**，并把「未来如何接到 `PutStart`」作为示例讨论，绝不把规划说成现状。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- **4.1** `TenantQuotaTable` 的数据模型与字段含义（状态字段、快照、错误码）
- **4.2** 有效配额计算（`RecomputeEffectiveQuotas` 的最大余数法仲裁）
- **4.3** reserve / commit / abort 三阶段记账模型
- **4.4** 失败回滚与多租户隔离（`over_quota`、`kAccountingMismatch`、预留是否跨租户）

### 4.1 TenantQuotaTable：数据模型与字段含义

#### 4.1.1 概念说明

`TenantQuotaTable` 是一个**纯内存、按 tenant 聚合**的记账本。它不负责分配真实内存，只负责回答两类问题：

1. **策略问题**：每个 tenant 想要多少容量？是显式配置的，还是继承默认值？
2. **会计问题**：每个 tenant 当前预留了多少、用掉了多少、是否已经超额？

它对外的全部状态都封装在两个结构体里：内部的 `TenantQuotaState`（可变、含计算中间量）和对外的 `TenantQuotaSnapshot`（只读快照、含 `tenant_id`）。先看字段表：

| 字段 | 类型 | 含义 | 谁写入 |
|---|---|---|---|
| `requested_quota_bytes` | uint64 | 租户**声明想要**的容量（显式策略值或默认值） | `UpsertTenantPolicy` / `EraseTenantPolicy` / `RecomputeEffectiveQuotas` 兜底 |
| `effective_quota_bytes` | uint64 | 租户**当前被授予**的容量，reserve 判断的真正依据 | `RecomputeEffectiveQuotas` 重算 |
| `used_bytes` | uint64 | 已**确认使用**的字节数（commit 累加，release 扣减） | `Commit`(+)/`Release`(-)/`ReleasePartial`(-) |
| `reserved_bytes` | uint64 | 已**预留但未确认**的字节数（reserve 累加，commit/abort 扣减） | `Reserve`(+)/`Commit`(-)/`Abort`(-) |
| `committed_count` | uint64 | 已确认的对象**计数**（与字节数解耦，用于上层数量级观测） | `Commit`(+)/`Release`(-) |
| `has_explicit_policy` | bool | 是否曾被显式 `UpsertTenantPolicy` 设置过 | `UpsertTenantPolicy`(true)/`EraseTenantPolicy`(false) |
| `over_quota` | bool | 派生标志：`used + reserved > effective` 是否成立 | `RefreshOverQuota`，每次记账/重算后刷新 |

> 直觉记忆：`used + reserved` = 这个租户「已经占住」的总容量；`effective - used - reserved` = 这个租户**还能再 reserve 多少**（叫作 headroom，剩余空间）。`over_quota` 为真，说明占用量已经超过了被授予量（典型场景：集群容量缩水后 `effective` 变小）。

#### 4.1.2 核心流程

一个 tenant 的「生命周期」在账本里大致这样流转：

```
        UpsertTenantPolicy(A, 500)            ← 显式设置 requested
                      │
        RecomputeEffectiveQuotas(capacity)    ← requested 仲裁成 effective
                      │
              Reserve(A, 80)  ──失败─► kQuotaExceeded（headroom 不足）
                      │成功
              reserved_bytes += 80
                      │
        ┌─────────────┴──────────────┐
   Commit(A, 80)                  Abort(A, 80)
   reserved -= 80                 reserved -= 80
   used      += 80                (空间立刻释放)
                      │
              Release(A, 80)  /  ReleasePartial(A, n)   ← 对象被删/被淘汰时回收 used
```

注意三个关键不变量（后面源码会一一印证）：

1. **`effective` 只由 `RecomputeEffectiveQuotas` 写**，记账操作（reserve/commit/...）从不改它。
2. **记账操作是幂等方向相反的**：reserve 加 reserved，commit/abort 减 reserved；commit 加 used，release 减 used。
3. **`over_quota` 是派生量**，从不手工设置，永远由 `RefreshOverQuota` 重算。

#### 4.1.3 源码精读

状态结构与快照的定义——注意两者字段几乎一一对应，区别只在 `Snapshot` 多了 `tenant_id` 且是不可变拷贝：

[TenantQuotaState 与 TenantQuotaSnapshot 字段定义:tenant_quota.h:13-32](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h#L13-L32) —— 上面表格里的七个字段全在这里声明。`Snapshot` 是给外部只读观测用的（如导出指标），`State` 是内部可变账本。

错误码枚举，区分了三种失败语义：

[TenantQuotaError 枚举:tenant_quota.h:34-40](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h#L34-L40)

- `kQuotaExceeded`：正常的「容量不够」拒绝（reserve 时 headroom 不足、或对一个从没拿到过 effective 的租户 reserve）。
- `kInvalidArgument`：参数非法（如 `UpsertTenantPolicy` 传 0）。
- `kAccountingMismatch`：**记账对不上**——调用方逻辑 bug（如 commit 的字节数比 reserved 还多）。这是比「超额」更严重的问题，会打 WARNING 日志。

公开 API 全景：

[TenantQuotaTable 公开方法:tenant_quota.h:42-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h#L42-L71) —— 一张表把「策略类」（`UpsertTenantPolicy`/`EraseTenantPolicy`/`SetDefaultRequestedQuota`）、「仲裁类」（`RecomputeEffectiveQuotas`）、「记账类」（`Reserve`/`Commit`/`Abort`/`Release`/`ReleasePartial`）、「观测类」（`GetTenantSnapshot`/`ListTenantSnapshots`）分得清清楚楚。

私有成员只有两个，极其精简：

[私有成员 tenants_ 与 default_requested_quota_bytes_:tenant_quota.h:69-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h#L69-L70) —— `std::map<std::string, TenantQuotaState> tenants_` 用 `std::map`（而非 `unordered_map`），正是为了让 `ListTenantSnapshots` 天然按 `tenant_id` 字典序输出。

#### 4.1.4 代码实践：读头文件，画字段流转图

**实践目标**：在不看实现的前提下，仅凭头文件建立对字段写入者的预测，再用源码验证。

**操作步骤**：

1. 打开 [tenant_quota.h:42-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/tenant_quota.h#L42-L71)。
2. 在纸上画一张表：行是七个字段，列是「哪个 public 方法会写它」。先**凭直觉猜**。
3. 然后翻 [tenant_quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp) 核对。

**需要观察的现象**：

- 你会发现 `effective_quota_bytes` **只**在 `RecomputeEffectiveQuotas` 里被写（其它方法对它只读）。这印证了 4.1.2 的不变量 1。
- `committed_count` 只被 `Commit`/`Release` 动，`ReleasePartial` **不动**它——这是个容易猜错的细节。

**预期结果**：你的手画表应与下表一致（√=写入）：

| 字段 | Upsert/Erase | Recompute | Reserve | Commit | Abort | Release | ReleasePartial |
|---|---|---|---|---|---|---|---|
| requested | √ | √(兜底默认) | | | | | |
| effective | | √ | | | | | |
| used | | | | √ | | √ | √ |
| reserved | | | √ | √ | √ | | |
| committed_count | | | | √ | | √ | |
| over_quota | | √(经 Refresh) | √ | √ | √ | √ | √ |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ListTenantSnapshots` 返回的快照天然按 `tenant_id` 升序排列？
**答案**：因为 `tenants_` 是 `std::map<std::string, ...>`，红黑树按键有序遍历。这点在 [tenant_quota.cpp:171-180](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L171-L180) 直接体现，单测 `ListSnapshotsSortedAndSkipsLazyEmptyTenants` 也钉死了这个顺序。

**练习 2**：`over_quota` 为真，是否一定意味着此刻有请求被拒绝？
**答案**：不一定。`over_quota` 只表示 `used + reserved > effective`。它最典型的触发场景是**容量缩水**——租户早已 commit 的数据，在 `RecomputeEffectiveQuotas` 用更小的容量重算后 `effective` 变小，于是 `used > effective`。此时 `over_quota` 是一个**告警信号**，提示该租户已经超配，下一次 reserve 一定会失败，但当前已 commit 的数据并不会被立刻删除（删除/淘汰是上层 eviction 的职责）。

---

### 4.2 有效配额计算（RecomputeEffectiveQuotas）

这是整个组件**算法含量最高**的部分：给定集群总可分配容量，如何把它公平地切给一群「显式要了配额」的租户和一群「没要配额、用默认」的租户。

#### 4.2.1 概念说明

先定义两类租户：

- **显式租户（explicit tenant）**：被 `UpsertTenantPolicy` 显式设置过 `requested_quota_bytes`，`has_explicit_policy == true`。
- **默认租户（default tenant）**：没被显式设置，继承 `default_requested_quota_bytes_`。其中又有一种特殊情况「**惰性空租户（lazy empty）**」：既无显式策略，又没有任何 used/reserved/committed——这种租户在分配时被**忽略**，不参与瓜分（避免幽灵租户稀释别人的配额）。

仲裁规则分两种情况：

- **容量够分**（所有显式租户的 `requested` 之和 ≤ 总容量）：每个显式租户拿到自己要的全额；剩下的容量在活跃的默认租户之间**均分**。
- **容量不够分**（显式租户 `requested` 之和 > 总容量）：**只有显式租户参与**，按各自 `requested` 的比例瓜分总容量；默认租户全部拿到 0。

这套「按比例分 + 凑整」用的是一个经典算法——**最大余数法（Largest Remainder Method）**，专门用来保证「分完的整数配额之和恰好等于总容量」。

#### 4.2.2 核心流程

设总容量为 \(C\)。先统计显式租户的 requested 之和 \(S = \sum_{\text{explicit}} \text{requested}_i\)。

**情形 A：\(S \le C\)（够分）**

\[
\text{effective}_i = \text{requested}_i \quad \forall\,\text{explicit } i
\]

剩余 \(R = C - S\) 在 \(n\) 个活跃默认租户间均分。每个默认租户的「公平份额」是 \(R / n\)，但 \(R\) 不一定能被 \(n\) 整除，于是用最大余数法：

\[
\text{base}_j = \left\lfloor \frac{R}{n} \right\rfloor,\qquad
\text{remainder}_j = R \bmod n
\]

先每人分 \(\text{base}_j\)，再把剩下的 \(R - n \cdot \text{base}_j\) 个字节，**按余数从大到小**、余数相同则按 `tenant_id` 字典序升序，依次给每人 +1，直到分完。

**情形 B：\(S > C\)（不够分，按比例缩）**

\[
\text{effective}_i = \left\lfloor \frac{C \cdot \text{requested}_i}{S} \right\rfloor
\]

同样用最大余数法补齐凑整（此时「余数」是 \(C \cdot \text{requested}_i \bmod S\)）。默认租户全部得 0。

伪代码：

```
RecomputeEffectiveQuotas(C):
    把租户分成 explicit_tenants / default_tenants(活跃的)
    S = Σ requested over explicit
    清零所有 effective
    if S <= C:
        每个 explicit: effective = requested
        distribute(default_tenants, capacity = C - S, 比例=否→均分)
    else:
        distribute(explicit_tenants, capacity = C, 比例=是→按 requested)
        # default_tenants 的 effective 保持 0
    对每个 state 调 RefreshOverQuota()   # 重算 over_quota 派生标志
```

`distribute` 内部统一用最大余数法，靠一个 lambda 实现，无论「均分」（分母=人数，分子=1）还是「按比例」（分母=S，分子=requested）都复用同一段代码。

**为什么必须防溢出？** 乘法 \(C \cdot \text{requested}_i\) 中两个操作数都是 `uint64_t`，直接乘会溢出（单测 `LargeValuesDoNotOverflowDuringRecompute` 用的就是 `UINT64_MAX`）。所以代码全程用 `unsigned __int128` 做中间运算。

#### 4.2.3 源码精读

整个重算入口：

[RecomputeEffectiveQuotas 实现:tenant_quota.cpp:78-159](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L78-L159) —— 开头 L80-95 完成三件事：累加显式 requested 之和 `explicit_requested_sum`、把租户分进两个列表、**把所有 `effective` 先清零**（重要：无论走哪个分支都不会漏置零），并识别惰性空租户。

惰性空租户的判定，是「默认租户不稀释活跃租户」的关键：

[IsLazyEmptyTenant 判定:tenant_quota.cpp:26-29](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L26-L29) ——「无显式策略 且 used=reserved=committed_count=0」。单测 `LazyEmptyTenantsDoNotDiluteActiveDefaultTenant` 验证：一个被 `EraseTenantPolicy` 退化成的 ghost 租户，分不到任何容量（effective=0），也不出现在 `ListTenantSnapshots` 里。

最大余数法的核心实现（注意全程 `unsigned __int128`）：

[distribute lambda：最大余数法分配:tenant_quota.cpp:97-138](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L97-L138) —— L114-117 计算 `product = capacity * numerator`、`base = product / denominator`、`remainder = product % denominator`；L122-128 按「余数降序、tenant_id 升序」排序；L130-136 把 `(capacity - assigned)` 个剩余字节依次 +1 补齐。这段同时服务「均分」（denominator=人数）和「按比例」（denominator=S）两种模式，靠 `proportional_to_requested` 开关切换分子分母。

两种情形的分发：

[情形 A 与 B 的分支:tenant_quota.cpp:140-154](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L140-L154) —— L140 `explicit_requested_sum <= allocatable_capacity_bytes` 走情形 A（显式满额 + 默认均分剩余）；`else` 走情形 B（仅显式按比例瓜分，默认为 0）。注意情形 B 里默认租户**根本没被传入 distribute**，所以它们的 effective 维持 L94 清的 0。

收尾刷新派生标志：

[重算后批量刷新 over_quota:tenant_quota.cpp:156-158](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L156-L158) —— 这就是为什么容量缩水后 `over_quota` 会自动点亮（见 4.4）。

**关键认知**：通读整段 `RecomputeEffectiveQuotas`，你会发现它**从不读取 `used_bytes` 和 `reserved_bytes`**。也就是说，有效配额的分配**只看策略（requested / 显式与否）和总容量**，完全无视当前谁预留了多少。这是本讲最重要的一个结论，4.3 的实践会专门验证它。

#### 4.2.4 代码实践：手算两个分配场景，再用单测对答案

**实践目标**：用纸笔算出两种仲裁结果，再用现成单测验证你的算法理解。

**操作步骤**：

1. **场景一（够分 + 默认均分凑整）**：总容量 `C = 203`，显式租户 `explicit` 要 100；两个活跃默认租户 `a`、`b`。手算 `a`、`b` 各得多少。
2. **场景二（不够分，按比例）**：总容量 `C = 150`，显式 `b` 要 200、`a` 要 100，另有一个活跃默认租户 `default`。手算三者各得多少。
3. 打开对应单测核对：
   - [DefaultTenantsSplitRemainderWithTenantIdTieBreak:tenant_quota_test.cpp:155-167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L155-L167)
   - [OverCapacityScalesOnlyExplicitTenants:tenant_quota_test.cpp:169-180](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L169-L180)

**需要观察的现象**：

- 场景一：`explicit=100`，剩余 `203-100=103`。`a`、`b` 各 `103/2=51`（base 各 51，assigned=102），剩 1 个字节；两人余数都是 `103%2=1`，平手，按 `tenant_id` 升序 → `a` 拿到那个 +1。结论 `a=52, b=51`。
- 场景二：显式和 `S=300 > C=150`，按比例：`a = 150*100/300 = 50`，`b = 150*200/300 = 100`，`default = 0`。

**预期结果**：与单测断言完全一致（`a=52/b=51`；`a=50/b=100/default=0`）。若你的手算与单测不符，回头检查「余数排序方向」或「是否误让默认租户参与了情形 B」。

> 说明：本实践是「源码阅读 + 单测对答案」型，不需要真实运行；如果你想真正跑一遍，可在 `mooncake-store` 构建目录执行 `ctest -R tenant_quota_test`（具体构建步骤待本地验证，参见第 5 节综合实践的构建说明）。

#### 4.2.5 小练习与答案

**练习 1**：单测 `LargestRemainderTieBreakUsesTenantId` 中，总容量只有 1，显式租户 `a`、`b` 各 requested=1。为什么结果是 `a=1, b=0` 而不是各 0.5？
**答案**：`S = 1+1 = 2 > C = 1`，走情形 B 按比例分。`base_a = base_b = floor(1*1/2) = 0`，余数都 `= 1*1 % 2 = 1`，assigned=0，剩 1 字节。余数并列时按 `tenant_id` 升序，`a` 在前，于是 `a` 拿到唯一的 +1。这正是 [tenant_quota.cpp:122-128](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L122-L128) 的 tie-break 规则。

**练习 2**：单测 `LargeValuesDoNotOverflowDuringRecompute` 用 `UINT64_MAX` 作为 requested 和 capacity，断言两人 effective 之和恰好等于 `UINT64_MAX`。如果分配算法没用 `__int128`，会出什么问题？
**答案**：`capacity * requested = UINT64_MAX^2` 远超 `uint64_t` 上限，会溢出回绕成一个错误的小数，导致 `base` 算错、分配结果荒谬。代码在 [tenant_quota.cpp:114-115](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L114-L115) 用 `unsigned __int128 product` 承接乘积，从根本上避免溢出。这也是「分完之和恰好等于总容量」这一不变量能成立的前提。

---

### 4.3 reserve / commit / abort 三阶段记账模型

#### 4.3.1 概念说明

配额不是「一次写死」的，而是顺着一次对象写入的生命周期分阶段流转。对应到 Mooncake Store 的两段式 Put（`PutStart` → 数据传输 → `PutEnd`），配额表设计了三个原语：

| 原语 | 语义 | 对应写入阶段 | 对账本的影响 |
|---|---|---|---|
| `Reserve(tenant, n)` | 「我**打算**用 n 字节，先给我占住」 | `PutStart`（分配前） | `reserved += n` |
| `Commit(tenant, n)` | 「这 n 字节**落实**了」 | `PutEnd`（成功） | `reserved -= n; used += n; committed_count++` |
| `Abort(tenant, n)` | 「我刚才的预留**作废**」 | `PutStart` 失败 / `PutRevoke` | `reserved -= n` |

另有两条回收路径（对象被删除 / 淘汰时调用，对应 `used` 的减少）：

- `Release(tenant, n)`：完整释放一个对象，`used -= n` 且 `committed_count--`。
- `ReleasePartial(tenant, n)`：只回收部分字节（如副本被部分驱逐），`used -= n` 但**不改** `committed_count`。

为什么必须用 reserved 这个中间态？因为 `PutStart` 和 `PutEnd` 之间存在时间窗口。如果只有 `used`，两个并发请求都看到「还剩 80 字节」就都去 `PutStart`，就会超卖。引入 reserved 后，第一个请求 reserve 80，headroom 立刻减 80，第二个请求的 reserve 就会被正确拒绝。这是经典的「预留即占用」防超卖模式。

#### 4.3.2 核心流程

每次 `Reserve` 的判定（称为 **headroom 检查**）是整个模型的咽喉：

\[
\text{headroom} = \text{effective} - \text{used} - \text{reserved}
\]

只有当 `used + reserved + bytes <= effective` 时才允许 reserve。注意这里用 `unsigned __int128` 做加法，避免 `used + reserved` 在极端值下溢出。

三种失败语义要分清：

1. **reserve 对一个「不存在」的租户**：返回 `kQuotaExceeded`（注意：**不是** mismatch，也**不**创建状态）。因为一个从未拿到 effective 的租户 headroom 本就是 0。
2. **reserve 超过 headroom**：返回 `kQuotaExceeded`，账本**原样不变**。
3. **commit/abort 的 n > 当前 reserved**，或 **release 的 n > 当前 used**：返回 `kAccountingMismatch`，账本**原样不变**，并打 WARNING。这代表调用方记账错乱（比如忘了 reserve 就 commit、或重复 commit），是比「超额」更严重的 bug 信号。

还有几个易忽略的细节：

- **0 字节是合法的 no-op 成功**：`Reserve/Commit/Abort/Release/ReleasePartial(tenant, 0)` 一律直接返回成功、不改账本（`Reserve` 的 0 字节例外地会 `GetOrCreateState` 创建空状态）。
- **`committed_count` 与字节数解耦**：`Release` 会 `--committed_count`，`ReleasePartial` 不会。
- **`used` 用饱和加法**：`Commit` 用 `SaturatingAdd` 累加，理论上不应溢出（reserve 已挡住），但作为防御性编程封顶在 `UINT64_MAX`。

#### 4.3.3 源码精读

`Reserve` 的咽喉逻辑：

[Reserve：headroom 检查与预留累加:tenant_quota.cpp:182-205](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L182-L205) —— L185-188 处理 0 字节（特殊地先 `GetOrCreateState`）；L190-193 对不存在的租户返回 `kQuotaExceeded` 且**不创建状态**；L196-200 是 headroom 检查，用 `unsigned __int128` 三项相加再比 `effective`；L202 成功后 `reserved_bytes += bytes` 并刷新 `over_quota`。

> 重点体会 L190-193 与 L186 的对比：`Reserve(0)` 会创建状态，`Reserve(非0)` 对不存在租户却**不**创建状态（直接拒绝）。单测 `ReserveMissingTenantDoesNotCreateStateOnFailure` 钉死了这个不对称——它防止「探测式 reserve」无意中污染账本。

`Commit` 把预留转成已用：

[Commit：预留转已用 + 饱和加法:tenant_quota.cpp:207-225](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L207-L225) —— L215-218 检查 `reserved < bytes` 报 `kAccountingMismatch`（调用方对不上账）；L220 `reserved -= bytes`；L221 用 `SaturatingAdd` 把 `used += bytes`；L222 `committed_count++`。

`Abort` 释放预留（与 commit 对称地减 reserved，但**不**动 used）：

[Abort：释放预留:tenant_quota.cpp:227-243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L227-L243) —— L235-238 同样有 mismatch 检查；L240 `reserved -= bytes`。这是「预留失败/撤销」的标准回滚路径。

`Release` 与 `ReleasePartial` 的对比（注意 `committed_count` 的差异）：

[Release：完整回收并减计数:tenant_quota.cpp:245-267](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L245-L267) —— L253-256 mismatch 检查；L258 `used -= bytes`；L259-264 `committed_count > 0` 则 `--`，否则打 WARNING（防御性，正常不该走到）。

[ReleasePartial：部分回收不动计数:tenant_quota.cpp:269-286](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L269-L286) —— L277 `DCHECK_LE` 在 debug 构建里会 abort（单测 `ReleasePartialUnderflowReportsMismatchInReleaseBuild` 用 `#ifdef NDEBUG` 区分：debug 下 `EXPECT_DEATH`，release 下返回 `kAccountingMismatch`）；L283 `used -= bytes`，**没有** `--committed_count`。

饱和加法与 mismatch 报错这两个工具函数：

[SaturatingAdd 与 AccountingMismatch:tenant_quota.cpp:19-37](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L19-L37) —— 前者封顶防溢出，后者把「对不上账」统一成 `kAccountingMismatch` 并写 WARNING 日志，便于线上定位调用方 bug。

#### 4.3.4 代码实践：跑通 reserve→commit→abort 的记账轨迹

**实践目标**：用一个最小程序验证三阶段记账对 `reserved / used / committed_count` 的精确影响。这是理解后续「失败回滚」的基础。

**操作步骤**（基于已有单测风格，下面是**示例代码**，可追加到 `tenant_quota_test.cpp` 里跑，也可独立 `main`）：

```cpp
// 示例代码：演示 reserve/commit/abort 的记账轨迹
#include "tenant_quota.h"
#include <iostream>

int main() {
    mooncake::TenantQuotaTable table;
    table.UpsertTenantPolicy("tenant-a", 100);     // 显式要 100
    table.RecomputeEffectiveQuotas(100);           // 够分 → effective=100

    // 阶段1：reserve 40
    table.Reserve("tenant-a", 40);
    auto s1 = *table.GetTenantSnapshot("tenant-a");
    std::cout << "after reserve:  reserved=" << s1.reserved_bytes
              << " used=" << s1.used_bytes << "\n";   // 预期 reserved=40, used=0

    // 阶段2：commit 40（预留转已用）
    table.Commit("tenant-a", 40);
    auto s2 = *table.GetTenantSnapshot("tenant-a");
    std::cout << "after commit:   reserved=" << s2.reserved_bytes
              << " used=" << s2.used_bytes
              << " count=" << s2.committed_count << "\n"; // 预期 reserved=0, used=40, count=1

    // 阶段3：再 reserve 50 然后 abort（释放预留）
    table.Reserve("tenant-a", 50);
    table.Abort("tenant-a", 50);
    auto s3 = *table.GetTenantSnapshot("tenant-a");
    std::cout << "after abort:    reserved=" << s3.reserved_bytes
              << " used=" << s3.used_bytes << "\n";   // 预期 reserved=0, used 不变
}
```

**需要观察的现象**：每次操作后打印 `reserved/used/committed_count`，应与注释里的预期值一致。

**预期结果**：`after reserve` → `reserved=40 used=0`；`after commit` → `reserved=0 used=40 count=1`；`after abort` → `reserved=0` 且 `used` 保持 40 不变（abort 只动 reserved，不动 used）。这套轨迹与单测 [ReserveCommitUpdatesAccounting:tenant_quota_test.cpp:260-275](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L260-L275) 和 [ReserveAbortReleasesReservation:tenant_quota_test.cpp:324-333](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L324-L333) 的断言一致。具体编译运行方式见第 5 节；未在本地实跑，断言值来自源码与单测，若实跑不符请以单测为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：某调用方写了 `Reserve(A, 50)` 成功后，**没有** commit 就直接 `Commit(A, 60)`。返回什么？账本变了吗？
**答案**：返回 `kAccountingMismatch`，账本**不变**（reserved 仍是 50）。因为 [tenant_quota.cpp:215-218](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L215-L218) 检查 `reserved(50) < bytes(60)` 成立即报错，且这是「先判定再改账本」的写法，判定失败时不会执行后面的减加操作。单测 `CommitWithoutEnoughReservationDoesNotModifyStateAndReportsMismatch` 钉死此行为。

**练习 2**：为什么 `Reserve` 对「不存在的租户」返回 `kQuotaExceeded` 而不是 `kAccountingMismatch`，并且不创建状态？
**答案**：因为这种租户从未经过 `RecomputeEffectiveQuotas`，`effective` 视为 0，headroom 为 0，任何非 0 reserve 都「超额」——这是正常的容量拒绝，不是记账错乱，所以用 `kQuotaExceeded`。不创建状态是为了避免「用一个失败的探测请求就把租户写进账本」，保持账本只反映真实活跃的租户。见 [tenant_quota.cpp:190-193](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L190-L193)。

---

### 4.4 失败回滚与多租户隔离：over_quota、AccountingMismatch 与「预留不跨租户」

#### 4.4.1 概念说明

把前两节拼起来，回答三个工程上最关心的问题：

1. **容量缩水了怎么办？** 集群有段被卸载、总可分配容量变小，已经 commit 的数据不会凭空消失，于是 `used` 可能超过新的 `effective`。`over_quota` 标志就是用来标记这种「已超额」状态的被动信号。
2. **预留失败如何回滚？** 一次 `PutStart` 可能因为下游（分配器、网络）失败而走不到 `PutEnd`。此时必须调用 `Abort` 把当初 `Reserve` 占的预留还回去，否则 headroom 被永久占住，该租户将无法再写入。
3. **租户 A 大量预留，会不会挤占租户 B？** 这是本节最重要、也最反直觉的结论：**不会。** reserved/used 是**每个租户账本内部**的量，`RecomputeEffectiveQuotas` 根本不读它们。租户之间的容量切分**只**由「策略 + 总容量」静态决定，预留只在租户自己的 headroom 里打转。

> 把第 3 点说穿：`TenantQuotaTable` 采取的是「**静态分区 + 租户内软预留**」的设计。容量先被切成若干互不相干的 `effective` 桶（静态分区），桶内的 reserve/commit/abort 只影响这个桶自己的 headroom（软预留防超卖）。一个租户再怎么疯狂 reserve，也不会让另一个租户的 `effective` 变小。如果你期待的是「全局动态抢占式」配额，那不是这个组件当前提供的语义。

#### 4.4.2 核心流程

`over_quota` 的更新点有两类：

- **每次记账操作后**：`Reserve/Commit/Abort/Release/ReleasePartial` 末尾都调 `RefreshOverQuota`。
- **每次 `RecomputeEffectiveQuotas` 后**：L156-158 遍历所有租户统一刷新。

`RefreshOverQuota` 的判定很简单，同样用 `__int128` 防溢出：

\[
\text{over\_quota} = \big(\text{used} + \text{reserved} > \text{effective}\big)
\]

「预留不跨租户」的论证链：

1. `effective_quota_bytes` 的**唯一**写入点是 `RecomputeEffectiveQuotas`（4.1 不变量 1）。
2. `RecomputeEffectiveQuotas` 的输入只有：各租户的 `requested`、`has_explicit_policy`、`allocatable_capacity_bytes`（4.2 源码精读已确认它不读 used/reserved）。
3. 因此租户 B 的 `effective` 只取决于「B 自己的策略」和「总容量 + 全体显式 requested 之和」——A 的 reserve/commit 完全不进入这个函数的视野。
4. 结论：A 的预留只会消耗 A 自己的 headroom，对 B 的 `effective` 与 headroom 零影响。

#### 4.4.3 源码精读

`over_quota` 派生计算：

[RefreshOverQuota:tenant_quota.cpp:311-315](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L311-L315) —— 三项相加用 `unsigned __int128`，再与 `effective` 比较。

容量缩水点亮 over_quota 的完整路径（推荐精读这条单测）：

[CapacityShrinkAndGrowthRefreshOverQuota:tenant_quota_test.cpp:233-245](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L233-L245) —— 租户要 100、容量 100、reserve+commit 80（used=80）。`Recompute(50)` 后 effective 缩到 50，`used(80) > effective(50)` → `over_quota=true`；`Recompute(100)` 后 effective 回到 100，`used(80) <= 100` → `over_quota=false`。这就是「容量缩水被动点亮 over_quota」的典型证据。

mismatch 不改账本的代表性单测（commit 路径）：

[CommitWithoutEnoughReservation...:tenant_quota_test.cpp:335-351](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp#L335-L351) —— reserve 5 后 commit 10，断言返回 `kAccountingMismatch` 且 `before == after`（reserved/used/count 全不变）。

#### 4.4.4 代码实践：两租户「预留不串扰」实验（对应规格要求的实践任务）

**实践目标**：验证规格里提出的核心疑问——「租户 A 预留大量配额但不 commit，租户 B 的 effective 是否会被重算挤占？abort 能否释放预留？」用真实组件给出确定答案。

**操作步骤**：在 `tenant_quota_test.cpp` 末尾追加下面这个**示例**测试，或写成独立 `main`：

```cpp
// 示例代码：两租户预留隔离实验
TEST(TenantQuotaTableDemo, ReservationDoesNotCrossAffectOtherTenant) {
    using namespace mooncake;
    TenantQuotaTable table;

    // 两个显式租户，总容量 200，各要 100 → 静态分区各 effective=100
    table.UpsertTenantPolicy("A", 100);
    table.UpsertTenantPolicy("B", 100);
    table.RecomputeEffectiveQuotas(200);
    ASSERT_EQ(table.GetTenantSnapshot("A")->effective_quota_bytes, 100u);
    ASSERT_EQ(table.GetTenantSnapshot("B")->effective_quota_bytes, 100u);

    // A 疯狂预留 90（但迟迟不 commit）
    ASSERT_TRUE(table.Reserve("A", 90).has_value());

    // 关键断言1：B 的 effective 毫发无损，仍是 100
    EXPECT_EQ(table.GetTenantSnapshot("B")->effective_quota_bytes, 100u);
    // 关键断言2：即便再调一次 Recompute，B 仍是 100（因为算法不看 reserved）
    table.RecomputeEffectiveQuotas(200);
    EXPECT_EQ(table.GetTenantSnapshot("B")->effective_quota_bytes, 100u);

    // A 自己的 headroom 被吃掉：再 reserve 11 应被拒（90+11 > 100）
    EXPECT_FALSE(table.Reserve("A", 11).has_value());
    // 但 B 完全不受影响，B 还能正常 reserve 满 100
    EXPECT_TRUE(table.Reserve("B", 100).has_value());

    // A 走 abort 回滚，释放那 90 预留
    ASSERT_TRUE(table.Abort("A", 90).has_value());
    EXPECT_EQ(table.GetTenantSnapshot("A")->reserved_bytes, 0u);
    // 现在 A 又能 reserve 了（headroom 恢复）
    EXPECT_TRUE(table.Reserve("A", 100).has_value());
}
```

**需要观察的现象**：
- A reserve 90 之后，**B 的 effective 始终是 100**，重复 `Recompute` 也不变。这直接证明「预留不串扰他租户」。
- A 的 headroom 从 100 降到 10（`Reserve(A,11)` 被拒），印证预留只在 A 桶内生效。
- `Abort(A,90)` 后 `reserved_bytes` 归零，A 恢复满额 reserve 能力，印证 abort 是预留的标准回滚路径。

**预期结果**：所有断言通过。这套断言把规格里的三个子问题都回答了：(1) A 预留大量配额**不会**重算/挤占 B 的 effective；(2) 原因是 `RecomputeEffectiveQuotas` 不读 reserved/used，容量切分是静态的；(3) `Abort` 通过 `reserved -= bytes` 释放预留、恢复 headroom。

**重要说明**：本实践的结论「B 的 effective 不受 A 预留影响」是一个**反直觉但正确**的事实。如果你最初的预期是「A 占住 90 就该让 B 少分一点」，那是因为把本组件误当成了「全局动态抢占式配额」——它实际上是「静态分区 + 租户内软预留」。这一点务必记牢。编译运行方式见第 5 节（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：如果不小心忘了在某次失败的 `PutStart` 后调用 `Abort`，账本会出现什么症状？最终会如何自愈（或不会）？
**答案**：`reserved_bytes` 会一直停留在那个值，该租户的 headroom 被永久占住，导致后续 `Reserve` 持续返回 `kQuotaExceeded`，租户「写不进新数据」。`TenantQuotaTable` 本身**没有**针对「僵尸预留」的超时清理——它只是个纯记账组件。所以自愈需要**上层**（未来的 `MasterService` 集成层）配合类似 `put_start_release_timeout_sec_` 的 reaper，在 `PutStart` 超时后主动调 `Abort`。这恰好呼应了 master_service.h 里已有的 `put_start_release_timeout_sec_` 机制（[master_service.h:1807](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1807)），是配额表未来接线时的天然搭档。

**练习 2**：`over_quota` 为 true 的租户，下一次 `Reserve(n)`（n>0）一定失败吗？
**答案**：是的。`over_quota` 为 true 意味着 `used + reserved > effective`，那么 `used + reserved + n > effective`（n>0）必然成立，[tenant_quota.cpp:196-200](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/tenant_quota.cpp#L196-L200) 的 headroom 检查必拒绝，返回 `kQuotaExceeded`。要让该租户重新可写，要么扩容（更大的 `Recompute` 容量）抬高 effective，要么 `Release`/淘汰掉部分 used 数据。

---

## 5. 综合实践：构建并运行 tenant_quota_test，追加你的两租户实验

把本讲的知识串成一条可执行的任务链。本实践综合了「字段流转、仲裁算法、三阶段记账、失败回滚、多租户隔离」全部要点。

### 实践目标

1. 在本地把 `tenant_quota_test` 这个单测目标编译并跑过，确认你对算法的理解与官方断言一致。
2. 把 4.4.4 的「两租户预留隔离」实验作为新 `TEST` 加进同一个测试文件并跑过。
3. 借此把本讲的所有断言在真实二进制上验证一遍（而非纸面推理）。

### 操作步骤

1. **定位构建系统**：确认 `tenant_quota.cpp` 在源码列表里（[mooncake-store/src/CMakeLists.txt:19](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/CMakeLists.txt#L19)），测试目标已注册（[mooncake-store/tests/CMakeLists.txt:63](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/CMakeLists.txt#L63) `add_store_test(tenant_quota_test tenant_quota_test.cpp)`）。
2. **配置并构建**（沿用项目 CMake 约定；具体顶层目录与编译器选项以仓库根的构建文档为准，**待本地验证**）：

   ```bash
   # 在仓库根目录
   cmake -S . -B build -DBUILD_STORE_TESTS=ON      # 开关名以仓库 CMake 选项为准
   cmake --build build --target tenant_quota_test
   ```

3. **跑官方单测**：

   ```bash
   (cd build && ctest -R tenant_quota_test --output-on-failure)
   ```

   预期：全部用例（含 `OverCapacityScalesOnlyExplicitTenants`、`ReserveCommitUpdatesAccounting`、`LargeValuesDoNotOverflowDuringRecompute` 等）PASS。

4. **追加你的实验**：把 4.4.4 的 `TenantQuotaTableDemo.ReservationDoesNotCrossAffectOtherTenant` 整段粘到 [tenant_quota_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/tenant_quota_test.cpp) 的 `}  // namespace` 闭合之前。重新 build + ctest，应多出一个 PASS 用例。

### 需要观察的现象

- 官方单测全绿，证明你对「够分/不够分、最大余数法、防溢出、记账幂等、mismatch 不改账本」的理解无误。
- 你的新用例 PASS，证明「预留不跨租户、abort 释放预留」在真实二进制上成立。

### 预期结果与排错

- 若 `OverCapacityScalesOnlyExplicitTenants` 失败：检查你是否误以为默认租户在「不够分」时也能分到容量（它们应得 0）。
- 若你的新用例中 `B.effective` 变成了非 100：说明你在 `Reserve("A",90)` 之后**没有**或**错误地**再次 `Recompute`——但即便 `Recompute`，B 也应仍是 100。如果真的变了，请回头重读 4.2「算法不读 reserved」这一结论。
- 若编译/ctest 命令名与本地构建环境不符：以上命令基于标准 CMake/ctest 约定推断，**请以仓库 `README` / CI 脚本（如 `scripts/run_ci_test.sh`）为准调整**（待本地验证）。

> 诚实声明：本讲的所有断言值均来自源码与官方单测的静态阅读，**未在本环境实跑**。若实跑结果与本讲描述不符，一律以源码与 `tenant_quota_test.cpp` 的实际行为为准，并欢迎据此修正本讲义。

---

## 6. 本讲小结

- **七个字段各司其职**：`requested`（想要）/`effective`（被授予，reserve 的依据）/`used`（已确认）/`reserved`（已预留未确认）/`committed_count`（对象计数）/`has_explicit_policy`（显式与否）/`over_quota`（派生超额标志）。`effective` 只由 `RecomputeEffectiveQuotas` 写，记账操作只动 used/reserved/count。
- **三阶段记账 reserve/commit/abort**：reserve 占住 headroom 防 `PutStart`↔`PutEnd` 窗口期超卖；commit 把预留转已用；abort 释放预留。Release/ReleasePartial 回收 used，二者区别在于是否减 `committed_count`。
- **有效配额 = 最大余数法静态分区**：够分则显式满额 + 默认均分剩余；不够分则仅显式按 requested 比例瓜分、默认归零。全程 `unsigned __int128` 防溢出，保证「分完之和恰等于总容量」。
- **三种失败语义**：`kQuotaExceeded`（容量不足/未知租户）、`kInvalidArgument`（0 配额策略）、`kAccountingMismatch`（记账对不上，账本不动 + WARNING）。
- **预留不跨租户**：reserved/used 是租户内量，`Recompute` 不读它们，所以 A 再怎么 reserve 也不会挤占 B 的 effective——这是「静态分区 + 租户内软预留」设计，不是全局动态抢占。
- **当前集成状态（务必记住）**：`TenantQuotaTable` 已进库、已单测，但**尚未**被 `MasterService` 调用；`MasterService` 目前只有全局 `quota_bytes_`。本组件是「内核就绪、接线待完成」状态，未来天然会挂在 `PutStart`(reserve)/`PutEnd`(commit)/`PutRevoke`+reaper(abort)/`Remove`+eviction(release) 这条链上，并与 `put_start_release_timeout_sec_` 的清理机制配合。

## 7. 下一步学习建议

- **横向对照全局配额**：回到 [master_service.h:1776](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1776) 的 `quota_bytes_` 和 `GetStorageConfig`，理解「单租户全局配额」与「多租户配额表」是两个层次的容量治理，思考它们合并后 `RecomputeEffectiveQuotas` 的容量入参应当来自哪里（很可能是 `memory_allocator` 的可分配容量，可参考 u6-l1 Store Allocator）。
- **纵向理解容量来源**：本讲的 `allocatable_capacity_bytes` 不是一个凭空给的数，它对应集群里所有内存段可用空间之和。建议接着学 **u6-l1（Store Allocator）** 和 **u6-l2（多级存储）**，看清「物理段容量 → 可分配容量 → 配额表入参」这条链。
- **追踪集成进度**：在 `master_service.cpp` 里搜 `tenant_quota` / `TenantQuotaTable`，观察它何时被真正接入 `PutStart`/`PutEnd`。一旦接线，本讲的 reserve/commit/abort 就会从「单测里的记账轨迹」变成「真实写入链路上的防超卖关卡」——届时可结合 u5-l3（Real Client）端到端复现一个「两租户写爆容量」的场景。
- **扩展阅读**：`tenant_quota_test.cpp` 是本组件最权威的行为说明书，建议通读全部 30+ 个用例，尤其关注 `LazyEmptyTenantsDoNotDiluteActiveDefaultTenant`（惰性空租户治理）和 `DefaultUnlimitedTenantDoesNotSqueezeExplicitQuota`（默认无限租户不挤占显式配额）这两个体现设计权衡的用例。
