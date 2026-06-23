# 淘汰、Pin 与租约（Lease）机制

## 1. 本讲目标

上一讲（u5-l2）我们打开了 `MasterService` 这个「元数据大脑」的机盖，看清了分片元数据、锁层级、对象元数据模型与核心 RPC。本讲我们把镜头对准这本「账本」里最动态的一块：**当内存写满时，Master 如何决定扔掉谁？又如何保护那些「正在被读」「很重要」「绝对不能丢」的对象？**

学完本讲你应该能够：

1. 说清 Store 的**两层淘汰观**：`eviction_strategy.h` 里的教科书式 LRU/FIFO 接口长什么样、为什么 `BatchEvict` 被称作「near-LRU（近似 LRU）」，以及二者各自处在哪一层。
2. 画出 **`BatchEvict` 的两遍扫描**：第一遍只淘汰「无 soft pin 且租约已过期」的对象，第二遍在高/低水位线没达标时才启动，并区分「只淘汰无 pin 对象（pass A）」与「连 soft pin 也一起淘汰（pass B）」两条路径。
3. 解释 **lease / soft pin / hard pin 三种保护机制**各自的语义、默认 TTL、谁负责刷新、谁负责让它们失效，以及它们在淘汰与 `Remove` 中各自如何充当「免死金牌」。
4. 描述 **CountMinSketch** 如何用极小内存近似统计每个 key 的访问频率，如何用「计数右移」防止计数器饱和，以及它在「提升准入（promotion admission）」里如何充当频率门控。
5. 能对着源码回答这一道贯穿性问题：**一个 soft pin 对象在内存压力下何时被淘汰、何时被保护，lease 过期后又如何被清理。**

> 本讲聚焦「淘汰 + 保护 + 频率追踪」三件事本身。涉及淘汰时触发的 DRAM→SSD offload、SSD→DRAM promotion-on-hit 的**完整异步流程**是下一讲（u6-l4）的主题，本讲只在 CountMinSketch 一节点到「频率门控」为止。

---

## 2. 前置知识

本讲默认你已经学完：

- **u5-l2 MasterService**：你需要知道 `MasterService` 内部用 1024 路 `MetadataShard` 存对象元数据，每个对象由 `ObjectMetadata` 描述，副本由 `Replica` 描述并带状态机（PROCESSING/COMPLETE）。本讲反复操作的就是 `ObjectMetadata` 上的几个时间戳字段。
- **u5-l1 Store 总体架构 / u6-l1 分配器**：知道 Master 是控制面、Client 是数据面，内存按 segment 挂载、按 buffer 分配。淘汰的本质是「把已分配的 buffer 还回去」。

此外，先建立两个直觉。

### 为什么需要「淘汰（eviction）」？

KV cache 的容量是有限的（一段内存 segment 就那么大）。当新对象要写入、而剩余空间不够时，系统只有两条路：

1. **拒绝写入**（返回 `NO_AVAILABLE_HANDLE`）——简单但用户体验差，热数据可能写不进去。
2. **挑一些旧对象扔掉、腾出空间**——这就是淘汰。难点在于「扔谁」：扔错了会把马上还要用的热数据扔掉，造成 cache miss 风暴。

所以淘汰算法的核心命题是：**尽量只扔「短期内不会再被访问」的对象。** 经典答案就是 LRU（最近最少使用）、FIFO（先进先出）等。

### 为什么需要「保护（pin / lease）」？

并非所有对象都「一视同仁地可扔」。有些对象此刻**正在被客户端读**（读到一半被扔掉会读到半个对象），有些对象是**贵宾（VIP）**（比如刚加载的权重，重建成本极高），有些对象**绝对不能丢**（业务语义要求常驻）。于是需要一套机制，在淘汰时给这些对象发「免死金牌」。Mooncake 用了三种强度递增的保护：**lease（租约）< soft pin（软钉）< hard pin（硬钉）**。

理解了「扔谁」和「保护谁」是一体两面，本讲后面的所有源码都是在回答这两个问题。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-store/include/master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h) | `MasterService` 与内嵌 `ObjectMetadata` 声明 | 看 `ObjectMetadata` 的 `lease_timeout`/`soft_pin_timeout`/`hard_pinned` 三个字段、`GrantLease`/`IsLeaseExpired`/`IsSoftPinned` 等方法，以及 `BatchEvict` 的注释 |
| [mooncake-store/src/master_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp) | 方法实现 | `EvictionThreadFunc`（水位触发）、`BatchEvict`（两遍扫描）、`GetReplicaList`/`PutEnd`/`Remove`（lease 授予与检查）、`TryPushPromotionQueue`（频率门控） |
| [mooncake-store/include/eviction_strategy.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h) | `EvictionStrategy` 抽象类与 `LRUEvictionStrategy`/`FIFOEvictionStrategy` | 教科书式 LRU/FIFO 的「策略接口」样貌，理解 near-LRU 的对照基准 |
| [mooncake-store/include/count_min_sketch.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h) | `CountMinSketch` 概率频率草图 | 频率近似统计算法、自动衰减、提升准入中的频率门控 |
| [mooncake-store/include/master_config.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h) | `MasterServiceConfig` | lease TTL、soft pin TTL、`allow_evict_soft_pinned_objects`、高水位、`promotion_admission_threshold` 等参数 |
| [mooncake-store/include/types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h) | 默认常量 | `DEFAULT_DEFAULT_KV_LEASE_TTL=5000ms`、`DEFAULT_KV_SOFT_PIN_TTL_MS=30min`、`DEFAULT_EVICTION_RATIO=0.05`、`DEFAULT_EVICTION_HIGH_WATERMARK_RATIO=0.95` |
| [mooncake-store/include/replica.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h) | `ReplicateConfig` | `with_soft_pin`/`with_hard_pin` 两个写入侧开关，决定对象创建时是否带保护 |
| [mooncake-store/tests/eviction_strategy_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/eviction_strategy_test.cpp) | LRU/FIFO 单元测试 | 代码实践：跑这个测试观察 LRU/FIFO 行为 |
| [mooncake-store/tests/promotion_on_hit_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/promotion_on_hit_test.cpp) | 提升准入测试 | 代码实践：观察 CountMinSketch 频率门控的拒绝/放行 |

---

## 4. 核心概念与源码讲解

### 4.1 淘汰策略：从 LRU/FIFO 接口到 BatchEvict 两遍扫描

#### 4.1.1 概念说明

缓存淘汰有几种经典算法：

- **FIFO（First In First Out）**：谁先来谁先走。维护一个队列，新对象进队头，淘汰时弹队尾。实现最简单，但完全不考虑「是否还会被访问」——一个一直在被读的热数据，仅仅因为它来得早，也可能被扔掉。
- **LRU（Least Recently Used）**：最近最少使用的先走。每次访问都把对象「挪到队头」，淘汰时弹队尾。这样队尾一定是「最久没人碰过」的，更接近「短期内不会再被访问」的直觉。代价是每次访问都要移动节点。
- **LFU（Least Frequently Used）**：访问频率最低的先走，需要统计每个 key 被访问的次数（CountMinSketch 就和这一族有关，见 4.3）。

Mooncake 在 [mooncake-store/include/eviction_strategy.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h) 里给出了一个**教科书式的策略抽象类** `EvictionStrategy`，以及 `LRUEvictionStrategy`、`FIFOEvictionStrategy` 两个实现。它的接口很直白：`AddKey`（登记）、`UpdateKey`（访问时更新位置）、`EvictKey`（挑一个淘汰）、`RemoveKey`（显式删除）。

> ⚠️ **重要分层事实（避免初学者踩坑）**：`eviction_strategy.h` 里的 LRU/FIFO 类目前**只被单元测试使用**（见 [eviction_strategy_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/eviction_strategy_test.cpp)）。它只是在 `master_service.h:47` 被前向声明（`class EvictionStrategy;`），**并没有在生产路径里被实例化**。生产中 Master 真正执行的淘汰是 `MasterService::BatchEvict`，源码注释把它称为 **"near-LRU（近似 LRU）"**。所以本节我们先认识「理想中的 LRU/FIFO 接口长什么样」，再把主要篇幅给到真正驱动生产的 `BatchEvict`。理解了这二者的区别，你才不会在源码里到处找「LRU 链表」却找不到。

那么 `BatchEvict` 为什么叫「near-LRU」？因为它**用 `lease_timeout` 这个时间戳来近似「最近访问时间」**：每次 `Get` 都会通过 `GrantLease` 把 `lease_timeout` 续期成 `now + ttl`（见 4.2），所以「最近被访问过的对象」拥有更大的 `lease_timeout`。淘汰时按 `lease_timeout` **从小到大**排序淘汰——最小的最先被扔——这恰好等价于「最久没被访问的最先被扔」，也就是 LRU 的语义。它不维护一条物理 LRU 链表，而是直接拿已有的租约时间戳当排序键，省掉了每次访问都要移动节点的开销，代价是粒度变粗，所以叫「近似」。

#### 4.1.2 核心流程

`BatchEvict` 不是孤立触发的，它由一个后台线程 `EvictionThreadFunc` 驱动。整个流程分三层：

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 层：EvictionThreadFunc（每 10ms 醒一次）                  │
│   读取全局内存使用率 used_ratio                                │
│   if used_ratio > 高水位 OR (need_mem_eviction_ 且比例>0):    │
│       计算 target / lowerbound 两个淘汰比例                    │
│       调用 BatchEvict(target, lowerbound)                    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 2 层：BatchEvict——第一遍（First Pass）                      │
│   从随机分片开始扫所有 1024 个分片                              │
│   每个分片算出「理想淘汰数 ideal_evict_num」                    │
│   收集候选 = {租约已过期 AND 可淘汰 AND 无 soft pin}            │
│   把 soft pin 对象(若允许)收进 soft_pin_objects 暂不淘汰        │
│   用 nth_element 找阈值 target_timeout，淘汰所有                │
│       lease_timeout <= target_timeout 的无 pin 对象            │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 3 层：BatchEvict——第二遍（Second Pass，仅当第一遍没达标）    │
│   target_evict_num = ceil(总量×lowerbound) - 已淘汰 - 已释放   │
│   if target_evict_num > 0:                                   │
│     Pass A: 候选够 → 只继续淘汰「无 soft pin」对象               │
│     Pass B: 无 pin 候选不够且 soft pin 非空 →                   │
│             把所有无 pin + (差额)个 lease 最小的 soft pin 也淘汰 │
└─────────────────────────────────────────────────────────────┘
```

两个淘汰比例的含义是「**高水位线 / 低水位线**」机制：

- `evict_ratio_target`（目标比例）：第一遍**想**淘汰掉的占比，通常 ≥ 基础 `eviction_ratio`。
- `evict_ratio_lowerbound`（下限比例）：第二遍兜底要保证达到的**最低**淘汰占比。

关键设计在于：**第一遍尽量「温和」**——只动无保护对象；**只有第一遍淘汰得不够（没摸到下限），才启动第二遍**，并且第二遍会「升级」到允许动 soft pin 对象。这是一种「先礼后兵」的逐步加压策略，避免一上来就把 VIP 对象也清掉。

#### 4.1.3 源码精读

**① 教科书式 LRU/FIFO 接口**

抽象基类定义了四个核心动作：[mooncake-store/include/eviction_strategy.h:16-41](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h#L16-L41)

```cpp
class EvictionStrategy : public std::enable_shared_from_this<EvictionStrategy> {
   public:
    virtual ErrorCode AddKey(const std::string& key) = 0;       // 登记新 key
    virtual ErrorCode UpdateKey(const std::string& key) = 0;    // 访问时更新位置
    virtual ErrorCode RemoveKey(const std::string& key) { ... } // 显式删除
    virtual std::string EvictKey(void) = 0;                     // 挑一个淘汰并返回
   protected:
    std::list<std::string> all_key_list_;                       // 双向链表保序
    std::unordered_map<std::string, std::list<std::string>::iterator>
        all_key_idx_map_;                                       // key→链表节点，O(1) 定位
};
```

这是经典的「**双向链表 + 哈希表**」LRU 实现：链表保序（队头最新、队尾最旧），哈希表让 `UpdateKey`/`RemoveKey` 能在 O(1) 找到节点。LRU 与 FIFO 的差异只在 `AddKey`/`UpdateKey`/`EvictKey` 的细节：

- LRU 的 `EvictKey` 永远弹 `back()`（最久未用），且 `UpdateKey` 会把 key 移到 `front()`：[mooncake-store/include/eviction_strategy.h:56-77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h#L56-L77)
- FIFO 的 `UpdateKey` 直接 `return OK`（访问不影响顺序），`EvictKey` 同样弹 `back()`（最早进入的）：[mooncake-store/include/eviction_strategy.h:79-97](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h#L79-L97)

> 这两个类是「策略模式的样板」，理解它有助于你看懂 `BatchEvict` 想表达的 LRU 语义。但请记住：生产淘汰不走这里。

**② 后台线程：水位触发与两个比例的计算**

`EvictionThreadFunc` 是淘汰的总入口，每 10ms 醒一次检查内存水位：[mooncake-store/src/master_service.cpp:3957-3975](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3957-L3975)

```cpp
double used_ratio = MasterMetricManager::instance().get_global_mem_used_ratio();
if (used_ratio > eviction_high_watermark_ratio_ ||
    (need_mem_eviction_ && eviction_ratio_ > 0.0)) {
    double evict_ratio_target = std::max(
        eviction_ratio_,
        used_ratio - eviction_high_watermark_ratio_ + eviction_ratio_);
    double evict_ratio_lowerbound =
        std::max(evict_ratio_target * 0.5,
                 used_ratio - eviction_high_watermark_ratio_);
    BatchEvict(evict_ratio_target, evict_ratio_lowerbound);
}
```

读法：

- 触发条件有**两条**：要么**使用率突破高水位**（被动，内存真的快满了），要么**分配失败置位的 `need_mem_eviction_` 标志为真**（主动，某次 `Allocate` 失败了，请求立刻清场）。后者就是 u6-l1 分配器在分配不出 buffer 时拉响的「警报」。
- `evict_ratio_target` 取「基础比例」和「超出水位的部分 + 基础比例」的较大值——意思是**越超水位，越要多淘汰**，把使用率压回水位以下。
- `evict_ratio_lowerbound` 取 `target 的一半` 和「纯超出水位部分」的较大值——这是第二遍必须兜底达到的最低线。

> 默认参数见 [mooncake-store/include/types.h:90-91](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L90-L91)：`DEFAULT_EVICTION_RATIO=0.05`、`DEFAULT_EVICTION_HIGH_WATERMARK_RATIO=0.95`。也就是默认「内存用到 95% 才触发，每次至少清 5%」。

**③ 第一遍：只动无保护对象，按 lease_timeout 排序淘汰**

第一遍的核心循环在 [mooncake-store/src/master_service.cpp:5429-5523](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5429-L5523)。它从**随机分片** `start_idx` 开始扫，避免总是从同一个分片开始造成不均衡。对每个分片：

```cpp
const long ideal_evict_num =
    std::ceil(object_count * evict_ratio_target) - evicted_count;
for (...每个对象...) {
    if (it->second.IsHardPinned()) continue;                 // hard pin：永不淘汰
    if (!it->second.IsLeaseExpired(now) ||
        !can_evict_replicas(it->second)) continue;            // 租约未过期/无可淘汰副本：跳过
    if (!it->second.IsSoftPinned(now)) {                     // 无 soft pin：当候选
        if (ideal_evict_num > 0) candidates.push_back(it->second.lease_timeout);
        else no_pin_objects.push_back(it->second.lease_timeout);
    } else if (allow_evict_soft_pinned_objects_) {            // soft pin：仅登记，暂不淘汰
        soft_pin_objects.push_back(it->second.lease_timeout);
    }
}
```

注意三个候选集合的分工：

| 集合 | 装的对象 | 第一遍会淘汰吗 |
|---|---|---|
| `candidates` | 无 soft pin、租约已过期、本分片还要继续淘汰 | ✅ 本遍立即淘汰 |
| `no_pin_objects` | 无 soft pin、租约已过期，但本分片配额已满 | ❌ 留给第二遍 |
| `soft_pin_objects` | 带 soft pin（且 `allow_evict_soft_pinned_objects_=true`） | ❌ 留给第二遍 pass B |

随后用 `std::nth_element` 在 `candidates` 里**线性时间**找到第 `ideal_evict_num` 小的 `lease_timeout` 作为阈值，然后淘汰所有 `lease_timeout <= 阈值` 的对象：[mooncake-store/src/master_service.cpp:5479-5519](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5479-L5519)

```cpp
std::nth_element(candidates.begin(),
                 candidates.begin() + (evict_num - 1),
                 candidates.end());            // 第 evict_num 小的元素就位
auto target_timeout = candidates[evict_num - 1];
// 遍历该分片，淘汰所有 lease_timeout <= target_timeout 的无 pin 对象
```

> 这就是「near-LRU」的落点：**按 `lease_timeout` 升序淘汰**。`lease_timeout` 最小 = 续期最久没发生 = 最久没被 Get = LRU 里该先走的对象。`nth_element` 而非完整 `sort`，是为了把这一步压在 O(n)。

**④ 第二遍：兜底下限，区分 Pass A / Pass B**

第一遍跑完、释放了一批过期副本后，计算第二遍还要不要再补淘汰多少：[mooncake-store/src/master_service.cpp:5529-5545](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5529-L5545)

```cpp
long target_evict_num = std::ceil(object_count * evict_ratio_lowerbound) -
                        evicted_count - released_discarded_cnt;
target_evict_num = std::min(target_evict_num,
    (long)no_pin_objects.size() + (long)soft_pin_objects.size());
if (target_evict_num > 0) {          // 只有第一遍没达标才进第二遍
    if (target_evict_num <= (long)no_pin_objects.size()) {
        // Pass A：无 pin 候选够用，只继续淘汰无 pin 对象
    } else if (!soft_pin_objects.empty()) {
        // Pass B：无 pin 不够，连 soft pin 也淘汰一部分
    }
}
```

- **Pass A**（[5545-5598](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5545-L5598)）：在 `no_pin_objects` 里 `nth_element` 找阈值，继续淘汰无 soft pin 对象。逻辑与第一遍同构。
- **Pass B**（[5599-5664](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5599-L5664)）：无 pin 候选不够，差额 `soft_pin_evict_num = target - no_pin.size()` 由 soft pin 对象补。同样对 `soft_pin_objects` 做 `nth_element`，淘汰那些 `lease_timeout` 最小的 soft pin 对象（即「soft pin 里最久没被访问的」），并连带把所有无 pin 对象也清掉。

**这正是 soft pin 的「软」之处**：它在第一遍和 Pass A 里受保护，但**在低水位线长期不达标、且 `allow_evict_soft_pinned_objects_=true` 时，会被 Pass B 牺牲掉**。相对地，hard pin 在所有遍的 `IsHardPinned()` 检查里都被 `continue` 跳过，**任何情况下都不淘汰**。

**⑤ 淘汰的尾声：标志复位与指标上报**

一轮结束后，根据是否真的清出了空间，决定要不要清掉 `need_mem_eviction_`「警报」并上报指标：[mooncake-store/src/master_service.cpp:5679-5696](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5679-L5696)

```cpp
if (evicted_count > 0 || released_discarded_cnt > 0 || offload_deferred_count > 0) {
    need_mem_eviction_ = false;                       // 清场成功，解除警报
    MasterMetricManager::instance().inc_eviction_success(evicted_count, total_freed_size);
} else {
    if (object_count == 0) need_mem_eviction_ = false; // 没对象可清，也别再触发
    MasterMetricManager::instance().inc_eviction_fail();
}
```

#### 4.1.4 代码实践

**实践目标**：亲手观察 LRU 与 FIFO 在「访问后」的行为差异，建立对 near-LRU 排序直觉的对照基准。

**操作步骤**：

1. 打开 [mooncake-store/tests/eviction_strategy_test.cpp:45-75](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/eviction_strategy_test.cpp#L45-L75) 阅读 `EvictKey` 测试。它的关键断言是：依次 `AddKey("key1")`、`AddKey("key2")` 后，第一次 `EvictKey()` 返回 `"key1"`（最早进入的）；接着加入 key3/key4，并对 key2/key3 调用 `UpdateKey`，第二次 `EvictKey()` 期望返回 `"key4"`。

2. 在仓库构建目录里编译并只跑这一个测试（**构建命令依你的环境而定，以下为典型形式，待本地验证**）：

   ```bash
   # 假设你在仓库根目录已按 u1-l3 配好构建目录 build/
   cmake --build build --target eviction_strategy_test
   ./build/mooncake-store/tests/eviction_strategy_test --gtest_filter='EvictionStrategyTest.EvictKey'
   ```

3. 把 `LRUEvictionStrategy` 改成 `FIFOEvictionStrategy`，重跑同一断言。

**需要观察的现象**：

- LRU 下，因为对 key2/key3 做了 `UpdateKey`（移到队头），最旧的就变成了 key4，所以淘汰 key4，断言通过。
- FIFO 下，`UpdateKey` 是空操作，顺序仍按进入时间，最旧的是 key2——此时这个针对 LRU 写的断言**会失败**。这正是两种策略的本质差别。

**预期结果**：LRU 测试通过；改成 FIFO 后 `EXPECT_EQ(evicted_key, "key4")` 失败、实际淘汰的是 key2（或取决于 FIFO 对重复 AddKey 的处理）。如果构建/运行环境不具备，可改为「纯阅读型实践」：对照 [eviction_strategy.h:67-76](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h#L67-L76) 的 `EvictKey` 与 [56-65](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/eviction_strategy.h#L56-L65) 的 `UpdateKey`，口算出每个 key 在链表里的最终位置，推断 `EvictKey` 的返回值。

#### 4.1.5 小练习与答案

**练习 1**：`BatchEvict` 第一遍为什么不直接 `std::sort` 所有候选再淘汰前 N 个，而用 `std::nth_element`？

**参考答案**：`BatchEvict` 只需要「找出 `lease_timeout` 第 N 小的阈值」，不需要完整排序。`std::nth_element` 是 O(n)，而 `std::sort` 是 O(n log n)；当分片里候选对象很多时，这一步是热路径，用 `nth_element` 能显著降低淘汰扫描的 CPU 开销。完整排序得到的额外顺序信息在这里用不上。

**练习 2**：为什么 `EvictionThreadFunc` 要从「随机分片 `start_idx`」开始扫描，而不是固定从 0 号分片开始？

**参考答案**：若固定从 0 号开始，前面分片的对象会被反复优先淘汰，造成分片间淘汰不均衡——靠前的分片总在被清、靠后的分片几乎不受影响。随机起点让每个分片在长期统计上被「平等对待」，避免长期偏斜。

**练习 3**：假设第一遍已经淘汰够了（达到 `evict_ratio_lowerbound`），第二遍还会运行吗？

**参考答案**：不会。第二遍的入口条件是 `target_evict_num > 0`，而 `target_evict_num = ceil(总量×lowerbound) - 已淘汰 - 已释放`。第一遍若已达标，这个值为 0（或负），第二遍被跳过。这正是「先礼后兵」：第一遍够用就不去碰 soft pin。

---

### 4.2 三种对象保护机制：Lease、Soft Pin、Hard Pin

#### 4.2.1 概念说明

在 4.1 我们看到淘汰会跳过「被保护」的对象。Mooncake 的保护分三种，**强度递增、语义不同**：

| 机制 | 字段 | 默认 TTL | 谁来授予/刷新 | 强度 | 淘汰时表现 |
|---|---|---|---|---|---|
| **Lease（硬租约）** | `lease_timeout` | 5000ms（5 秒） | 每次 `Get`/`ExistKey` 自动 `GrantLease` | 弱（短时） | 租约未过期 → 跳过；过期 → 可淘汰 |
| **Soft Pin（软钉）** | `soft_pin_timeout`（optional） | 30 分钟 | 创建时按 `with_soft_pin` 决定；`GrantLease` 一并续期 | 中（长时、可破） | Pass 1/Pass A 保护；Pass B 在压力下可淘汰 |
| **Hard Pin（硬钉）** | `hard_pinned`（const bool） | 永久 | 创建时按 `with_hard_pin` 决定，**不可变** | 强（绝对） | 任何遍都 `continue` 跳过，永不淘汰 |

直觉上：

- **Lease** 是「**我正在用，别动**」的短期占位符。客户端每次 `Get` 都给对象续一段短租约（默认 5 秒），保证在读取期间对象不会被淘汰或删除。租约一过期，对象就回到「可被淘汰」的普通状态。它同时也是 `Remove` 的门禁：租约未过期时，非 `force` 的 `Remove` 会被拒绝（返回 `OBJECT_HAS_LEASE`）。
- **Soft Pin** 是「**这是 VIP，尽量留着**」的长期偏好。比如刚加载的大模型权重，重建成本极高，值得给个 30 分钟的「软保护」。说它「软」，是因为在内存长期紧张、低水位线摸不到时，系统**可以违背**它（Pass B）。
- **Hard Pin** 是「**绝对不能丢**」的契约。一旦创建时打上，就再也无法取消，淘汰器在任何情况下都无视它。适合那些业务上必须常驻、丢失即故障的对象。

> 一个对象可以**同时**拥有 lease + soft pin（VIP 对象在 5 秒租约之外，还多一层 30 分钟软保护），但 hard pin 与 soft pin 是创建时二选一的开关（见 `ReplicateConfig`）。

#### 4.2.2 核心流程

三种机制的生命周期可以画成一条时间线：

```
对象创建（PutStart→PutEnd）
  │  PutEnd 调用 GrantLease(0, soft_ttl)
  │   → lease_timeout = now        （初始无硬租约，立即可淘汰）
  │   → 若 with_soft_pin: soft_pin_timeout = now + 30min
  │
  ├─ 客户端 Get（命中）
  │    GrantLease(lease_ttl, soft_ttl)
  │     → lease_timeout  = max(旧值, now + 5s)   （只增不减）
  │     → soft_pin_timeout = max(旧值, now + 30min)（若有）
  │
  ├─ 淘汰器扫描（BatchEvict）
  │    IsHardPinned()        → true: 永远跳过
  │    IsLeaseExpired(now)   → false: 跳过（硬租约保护中）
  │    IsSoftPinned(now)     → true: Pass1/A 跳过，PassB 压力下可淘汰
  │
  ├─ Remove(key, force=false)
  │    IsLeaseExpired() → false: 拒绝(OBJECT_HAS_LEASE)
  │                     → true:  进一步检查副本就绪/复制任务，通过才删
  │
  └─ lease 自然过期 / soft pin 自然过期
       → 对象回归「可被普通淘汰」状态，等下一轮 BatchEvict 清理
```

几个关键性质：

1. **GrantLease 只增不减**：续期用 `std::max`，绝不会把一个还剩很久的租约缩短。这避免「并发 Get 之间互相把对方的租约缩短」的竞态。
2. **PutEnd 的特殊授予**：`PutEnd` 调 `GrantLease(0, soft_ttl)`——传 `ttl=0` 意味着 `lease_timeout = max(旧值, now)`，即写完那一刻**没有硬租约**（写完不等于马上要读），但如果有 soft pin 则立刻生效。这是刻意设计：刚写完的对象默认「可淘汰」，要等第一次 `Get` 才获得 5 秒硬租约。
3. **租约刷新的触发**：客户端会判断 `NeedsLeaseRefresh`（当剩余租约 ≤ 一半时需要刷新），主动在读取路径上续期，避免长读过程中租约过期。

#### 4.2.3 源码精读

**① 三个字段与构造：谁、何时、给谁打上保护**

字段声明与注释一语道破三者关系：[mooncake-store/include/master_service.h:859-864](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L859-L864)

```cpp
mutable std::chrono::system_clock::time_point lease_timeout
    GUARDED_BY(lock);                    // hard lease
mutable std::optional<std::chrono::system_clock::time_point>
    soft_pin_timeout GUARDED_BY(lock);   // optional soft pin, only set for vip objects
const bool hard_pinned{false};           // immutable, set at creation
```

注意 `hard_pinned` 是 `const`——注释明说「immutable, set at creation」，所以一旦创建无法取消。构造函数依据 `enable_soft_pin`/`enable_hard_pin` 两个入参决定初值：[mooncake-store/include/master_service.h:814-839](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L814-L839)

```cpp
ObjectMetadata(... bool enable_soft_pin, bool enable_hard_pin = false, ...)
    : ..., lease_timeout(), soft_pin_timeout(std::nullopt),
      hard_pinned(enable_hard_pin), ... {
    if (enable_soft_pin) {
        soft_pin_timeout.emplace();      // 只有 VIP 对象才有 soft pin
        ...
    }
}
```

这两个开关来自写入侧的 `ReplicateConfig`：[mooncake-store/include/replica.h:84-85](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L84-L85)

```cpp
bool with_soft_pin{false};
bool with_hard_pin{false};  // Hard pin: object cannot be evicted
```

并一路传到 `AllocateAndInsertMetadata` 构造 `ObjectMetadata` 时：[mooncake-store/src/master_service.cpp:1772-1776](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1772-L1776)（`config.with_soft_pin, config.with_hard_pin`）。

**② GrantLease：只增不减的续期**

`GrantLease` 是 lease 与 soft pin 的共同刷新入口，核心是两个 `std::max`：[mooncake-store/include/master_service.h:1023-1034](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1023-L1034)

```cpp
void GrantLease(const uint64_t ttl, const uint64_t soft_ttl) const {
    SpinLocker locker(&lock);
    auto now = std::chrono::system_clock::now();
    lease_timeout =
        std::max(lease_timeout, now + std::chrono::milliseconds(ttl));
    if (soft_pin_timeout) {
        soft_pin_timeout =
            std::max(*soft_pin_timeout, now + std::chrono::milliseconds(soft_ttl));
    }
}
```

读法：`ttl` 是硬租约时长（`Get` 时传 `default_kv_lease_ttl_`=5s，`PutEnd` 传 0），`soft_ttl` 是软钉时长（`default_kv_soft_pin_ttl_`=30min）。`max` 保证「只往后推、不往前缩」。

**③ 三个判定方法：过期 / 软钉 / 硬钉**

淘汰器反复调用的就是这三个只读判定：[mooncake-store/include/master_service.h:1049-1073](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1049-L1073)

```cpp
bool IsLeaseExpired() const { ... return now >= lease_timeout; }      // 硬租约是否过期
bool IsSoftPinned() const { ... return soft_pin_timeout && now < *soft_pin_timeout; } // 是否在软钉期
bool IsHardPinned() const { return hard_pinned; }                     // 是否硬钉（永久）
```

它们都带一个接收外部 `now` 的重载（如 `IsLeaseExpired(now)`），让 `BatchEvict` 在一轮里复用同一个时间戳，避免「扫描到一半时钟跳变」导致判定不一致。

**④ Get 时授予、PutEnd 时归零、Remove 时门禁**

- `GetReplicaList` 命中后立刻授予硬租约，注释点明用意「so it will not be removed when the client is reading it」：[mooncake-store/src/master_service.cpp:1481-1489](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1481-L1489)
- `PutEnd` 写完调用 `GrantLease(0, default_kv_soft_pin_ttl_)`，注释解释「写完初始无租约，若有 soft pin 则置位」：[mooncake-store/src/master_service.cpp:1976-1979](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1976-L1979)
- `Remove` 把 lease 当门禁，非 `force` 且租约未过期直接拒绝：[mooncake-store/src/master_service.cpp:2962-2965](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2962-L2965)

```cpp
if (!force && !metadata.IsLeaseExpired()) {
    return tl::make_unexpected(ErrorCode::OBJECT_HAS_LEASE);
}
```

> 注意 `force=true` 会**跳过 lease 检查**（但仍要检查副本就绪和复制任务，注释 [2967-2972](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2967-L2972) 解释了为何不连副本检查也跳过：并发 put/copy/move 下直接删太危险）。`force` **不能**绕过 `BatchEvict` 对 soft/hard pin 的处理——`force` 只作用于显式 `Remove`，与自动淘汰是两条独立路径。

**⑤ 默认 TTL 与开关从哪来**

默认值集中在 [mooncake-store/include/types.h:85-89](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L85-L89)：

```cpp
static constexpr uint64_t DEFAULT_DEFAULT_KV_LEASE_TTL = 5000;        // 5 秒
static constexpr uint64_t DEFAULT_KV_SOFT_PIN_TTL_MS = 30 * 60 * 1000;// 30 分钟
static constexpr bool DEFAULT_ALLOW_EVICT_SOFT_PINNED_OBJECTS = true; // 默认允许 PassB 动 soft pin
```

`MasterService` 持有这几个配置成员：[mooncake-store/include/master_service.h:1383-1385](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1383-L1385)

```cpp
const uint64_t default_kv_lease_ttl_;     // in milliseconds
const uint64_t default_kv_soft_pin_ttl_;  // in milliseconds
const bool allow_evict_soft_pinned_objects_;
```

它们在配置结构里声明为必填字段：[mooncake-store/include/master_config.h:37-39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L37-L39)。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：这是本讲规格指定的实践——阅读 `ObjectMetadata` 的 lease/pin 字段与 `BatchEvict` 注释，描述一个 **soft pin 对象**在内存压力下「何时被淘汰、何时被保护」，以及「lease 过期后如何被清理」。请先自己用纸笔推演，再对照下面的参考。

**操作步骤**：

1. 打开 `master_service.h`，定位 `BatchEvict` 的注释块：[mooncake-store/include/master_service.h:769-777](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L769-L777)。注释明确写了「两遍、第一遍只淘汰无 soft pin、第二遍优先无 soft pin 但在 `allow_evict_soft_pinned_objects_` 为真时也允许淘汰 soft pin」。
2. 定位三个字段 [859-864](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L859-L864) 与判定方法 [1049-1073](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1049-L1073)。
3. 回到 `BatchEvict` 实现，跟踪一个 soft pin 对象在第一遍 [5462-5470](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5462-L5470)、第二遍 Pass B [5635-5637](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5635-L5637) 的走向。

**参考推演（soft pin 对象的命运）**：

- **被保护的情形**：只要内存压力没到「低水位线长期不达标」，第一遍淘汰无 pin 对象就够了。此时 soft pin 对象在第一遍被收进 `soft_pin_objects` 但不淘汰（[5468-5470](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5468-L5470)），`target_evict_num` 一旦 ≤ `no_pin_objects.size()` 就走 Pass A，soft pin 对象全程不动。另外，若配置 `allow_evict_soft_pinned_objects_=false`，soft pin 对象在收集阶段就被直接跳过（连 `soft_pin_objects` 都不进），相当于**在任何情况下都受保护**（仅次于 hard pin）。
- **被淘汰的情形**：当第一遍淘汰量没摸到 `evict_ratio_lowerbound`、且无 pin 候选不够、且 `allow_evict_soft_pinned_objects_=true` 时，进入 Pass B。soft pin 对象中 `lease_timeout` 最小的那批（差额 `soft_pin_evict_num` 个）被淘汰（[5606-5614](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5606-L5614)）。也就是说：**同样是 soft pin，`lease_timeout` 小（更久没被 Get 续期）的先被牺牲**——这把 near-LRU 的思路也用到了 soft pin 内部的排序上。

**lease 过期后如何被清理**：

- lease 过期本身**不会**主动删除对象，它只是让对象「重新可被淘汰」。真正的清理发生在下一轮 `BatchEvict`：因为 `IsLeaseExpired(now)` 变 true，对象从「被租约保护」变成「无 pin 候选」（前提是它也没 soft pin，或 soft pin 也过期了），于是在第一遍被收进 `candidates` 并按 `lease_timeout` 排序淘汰。
- 此外，`EvictionThreadFunc` 还有一条「**久未淘汰则清理过期 processing key / 复制任务**」的旁路：当连续 `put_start_release_timeout_sec_` 没触发过淘汰时，调用 `DiscardExpiredProcessingReplicas` 丢弃超时的 processing 副本、`ReleaseExpiredDiscardedReplicas` 释放已丢弃副本占的内存（[3976-3988](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3976-L3988)）。这是处理「写了一半超时」的垃圾回收，与 lease 过期清理互补。
- 对于**显式删除**：`Remove(key, force=false)` 在 lease 未过期时返回 `OBJECT_HAS_LEASE`（[2962-2965](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2962-L2965)）；过期后才放行（还要过副本就绪、复制任务两关）。所以 lease 过期 = 显式删除与自动淘汰两扇门同时解锁。

**预期结果**：你能不看答案复述出「soft pin 在 Pass1/PassA 受保护、PassB 压力下按 lease_timeout 升序被牺牲；lease 过期不主动删，而是等下一轮 BatchEvict 当作无 pin 候选清掉，或被 force=false 之外的 Remove 在过期后放行」。

#### 4.2.5 小练习与答案

**练习 1**：一个对象创建时 `with_soft_pin=true`、`with_hard_pin=false`，从未被 `Get` 过。它的 `lease_timeout` 和 `soft_pin_timeout` 分别是什么状态？它能被立刻淘汰吗？

**参考答案**：`PutEnd` 调用 `GrantLease(0, soft_ttl)`，`ttl=0` 使 `lease_timeout = max(初值, now)`——即「已过期」状态（无硬租约）；`soft_pin_timeout` 被置为 `now + 30min`（在保护期内）。它**不能**被立刻淘汰，因为 `IsSoftPinned(now)` 为 true：第一遍会跳过它，只有进入 Pass B（且 `allow_evict_soft_pinned_objects_=true`）才可能被淘汰。换句话说，soft pin 让这个「从未被读」的对象仍享有 30 分钟的软保护。

**练习 2**：为什么 `GrantLease` 用 `std::max` 而不是直接赋值 `now + ttl`？

**参考答案**：用 `max` 保证「只增不减」。若直接赋值，两个并发的 `Get` 可能出现「后到的 Get 把一个还剩 4 秒的租约覆盖成 now+5s」——表面看没问题，但更危险的是在高并发下可能出现「续期时间戳回退」，让原本受保护的对象意外提前进入可淘汰窗口。`max` 让续期是单调递增的，消除这类竞态，也符合「租约代表最迟的安全删除时间，只能往后推」的语义。

**练习 3**：`Remove(key, force=true)` 能不能删掉一个 hard pin 对象？能不能删掉一个 soft pin 对象？

**参考答案**：能，两种都能删。`force=true` 跳过的是 `IsLeaseExpired` 检查（[2962](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2962)），而 soft/hard pin 的保护**只作用于自动淘汰（BatchEvict）**，不作用于显式 `Remove`。`Remove` 路径里没有对 `IsSoftPinned`/`IsHardPinned` 的检查。所以 hard pin 的「绝对不能丢」是相对于**淘汰器**而言的；显式删除（尤其是 `force`）是管理员的明确意志，系统会服从。仍要过的两关是「副本全部 COMPLETE」和「无在途复制任务」。

---

### 4.3 CountMinSketch：访问频率追踪与提升准入

#### 4.3.1 概念说明

`CountMinSketch`（Count-Min 草图）是一种**概率型数据结构**，用来在极小内存下**近似统计每个 key 的访问频率**。

为什么不用一个普通的 `unordered_map<key, count>`？因为 KV cache 里 key 数量可能上百万，给每个 key 存一个精确计数器内存开销太大，而且我们往往**只关心「这个 key 够不够热」**，不需要精确次数。CountMinSketch 用一个「宽 × 深」的二维计数器数组，配合多个哈希函数，做到：

- **空间固定**：只占 `width × depth` 个计数器，与 key 总数无关。Mooncake 默认 `4096 × 4 = 16384` 个 `uint8_t`，仅 16KB。
- **只高估、不低估**：由于多个 key 可能哈希到同一个格子（冲突），统计值可能偏大，但绝不会偏小。这对「频率门控」是安全的——宁可误放行，不会误拒绝一个真热 key。
- **可衰减**：计数器会周期性「右移减半」，让旧访问的影响随时间淡出，反映**近期**热度。

在 Mooncake 里，它服务于 **promotion-on-hit（命中提升）** 的**频率门控**：当一个只存在 SSD（LOCAL_DISK）副本的 key 被 `Get` 命中时，系统考虑把它「提升」回 DRAM。但提升有成本（SSD 读 + RDMA 写），不能对每个冷 key 都做。于是用 CountMinSketch 统计该 key 被访问的次数，**只有累计访问次数达到阈值，才认为它够热、值得提升**。

> 提升的完整异步流程（心跳下发、AllocStart、refcnt 钉住源副本等）属于 **u6-l4**。本节只讲 CountMinSketch 这个数据结构本身，以及它在准入链路里作为「第一道门（频率门）」的角色。

#### 4.3.2 核心流程

Count-Min 的数学原理很简单。给定 `depth` 个独立的哈希函数 \(h_0, h_1, \dots, h_{d-1}\)，每个把 key 映射到 \([0, width)\)：

- **increment(key)**：对每一行 \(i\)，把 `table[i][h_i(key)]` 加 1；返回所有行中该 key 命中格子的**最小值**作为估计频率。

  \[
  \widehat{f}(key) = \min_{i=0}^{d-1} \text{table}[i][\,h_i(key)\,]
  \]

  取 min 是因为冲突只会让某个格子偏大，取多行最小值能尽可能抵消高估。

- **count(key)**：同上但不自增，只读返回 min。

- **decay（衰减）**：把所有格子右移 1（除以 2 下取整）。这让历史计数按指数衰减，近期访问权重更高。

Mooncake 额外做了一条**自动衰减**：用一个 `total_increments_` 计数器累计自增次数，一旦达到 `width × depth`（默认 16384），就自动 `decay` 一次并清零计数器。这保证 `uint8_t` 计数器不会一直累加到 255 饱和而失去区分度。

在准入链路里，CountMinSketch 是 `TryPushPromotionQueue` 的**第一道门**：

```
Get 命中一个 only-LOCAL_DISK 的 key
  └─ TryPushPromotionQueue:
       ① 频率门: freq = sketch.increment(key)
                  if freq < promotion_admission_threshold_: 拒绝(频率不足)  ← 本节重点
       ② 水位门: if 全局内存使用率 >= 高水位: 拒绝(内存紧张)
       ③ 去重门: 已有在途提升任务 / 已出现 MEMORY 副本: 跳过
       ④ 上限门: 在途任务数 >= promotion_queue_limit_: 拒绝(队列满)
       ⑤ 通过 → refcnt 钉住源副本，登记 PromotionTask，入队
```

阈值 `promotion_admission_threshold_` 默认 2（[master_config.h:110](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L110)），并在配置解析时被 **clamp 到 [1, 255]**——下界 1 是因为 `uint8_t` 频率最小为 1，阈值 0 会让门控形同虚设；上界 255 是因为 `uint8_t` 最大就是 255，阈值超过它会让任何 key 都永远过不了门。

#### 4.3.3 源码精读

**① 数据结构与构造**

紧凑的二维计数器表：[mooncake-store/include/count_min_sketch.h:14-20](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L14-L20)

```cpp
class CountMinSketch {
   public:
    explicit CountMinSketch(size_t width = 4096, size_t depth = 4)
        : width_(...), depth_(...),
          table_(depth_, std::vector<uint8_t>(width_, 0)),
          total_increments_(0) {}
```

默认 `4096 × 4`，每个格子是 `uint8_t`（最大 255）。整个表约 16KB，相比给百万 key 各存计数器省了几个数量级。类自带 `mutable std::mutex mu_`，所以可以从任意 `GetReplicaList` 调用方直接调用而无需外加锁（注释见 [master_service.h:1759-1762](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1759-L1762)）。

**② increment：多行自增 + 返回 min + 自动衰减**

核心方法：[mooncake-store/include/count_min_sketch.h:25-39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L25-L39)

```cpp
uint8_t increment(const std::string &key) {
    std::lock_guard<std::mutex> lock(mu_);
    uint8_t min_val = UINT8_MAX;
    for (size_t i = 0; i < depth_; ++i) {
        size_t idx = hash(key, i) % width_;
        if (table_[i][idx] < UINT8_MAX) ++table_[i][idx];   // 饱和保护：到 255 不再加
        min_val = std::min(min_val, table_[i][idx]);
    }
    if (++total_increments_ >= width_ * depth_) {
        decayLocked();                                       // 自动衰减，防饱和
    }
    return min_val;
}
```

读法：

- 每行用 `hash(key, i)` 算一个独立位置，自增（到 255 封顶），取所有行的最小值作为估计频率。
- 累计自增满 `width*depth` 次就触发一次全局衰减，保证计数器不会普遍顶到 255 而失去分辨力。

**③ decay：全局右移减半**

衰减就是把每个格子除以 2（历史打五折）：[mooncake-store/include/count_min_sketch.h:72-79](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L72-L79)

```cpp
void decayLocked() {
    for (size_t i = 0; i < depth_; ++i)
        for (size_t j = 0; j < width_; ++j)
            table_[i][j] >>= 1;     // 右移1位 = 除以2下取整
    total_increments_ = 0;
}
```

效果：一次衰减后，一个原本计数 10 的 key 变成 5。若它此后不再被访问，几次衰减后就趋近 0，自然「过气」；若它持续被访问，每次 increment 又加回来，维持高计数。这正是「反映近期热度」的机制。

**④ 多个独立哈希：种子扰动**

为了让 `depth` 行的哈希尽量独立（否则取 min 没有意义），用行号 `seed` 扰动 `std::hash`：[mooncake-store/include/count_min_sketch.h:62-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L62-L70)

```cpp
size_t hash(const std::string &key, size_t seed) const {
    size_t h = std::hash<std::string>{}(key);
    h ^= seed * 0x9e3779b97f4a7c15ULL + 0x517cc1b727220a95ULL;   // 黄金分割常数扰动
    h ^= (h >> 33); h *= 0xff51afd7ed558ccdULL; h ^= (h >> 33);  // avalanche
    return h;
}
```

**⑤ 准入第一道门：频率比较**

`TryPushPromotionQueue` 里，CountMinSketch 的 `increment` 返回值直接和阈值比较：[mooncake-store/src/master_service.cpp:3600-3604](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3600-L3604)

```cpp
const uint8_t freq = promotion_sketch_->increment(admission_key);
if (freq < promotion_admission_threshold_) {
    MasterMetricManager::instance().inc_promotion_rejected_frequency();  // 记一笔「频率拒绝」
    return;
}
```

注意是 **`increment` 而非 `count`**——即「这次访问本身也计数」，返回的是包含本次访问后的估计频率。阈值默认 2 意味着「一个 only-SSD 的 key 要被 Get 命中至少两次，才认为它够热、值得花成本提升回 DRAM」。紧随其后的第二道门是水位检查（[3609-3614](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3609-L3614)）：DRAM 已经在淘汰压力下时，即便 key 够热也不提升，避免「边淘汰边提升」的对冲。

**⑥ sketch 只在启用时构造**

`promotion_sketch_` 是 `unique_ptr`，仅当 `promotion_on_hit_=true` 时才在 Master 启动时构造（[master_service.h:1747-1762](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1747-L1762)）；`TryPushPromotionQueue` 开头就检查 `if (!promotion_on_hit_ || !promotion_sketch_) return;`（[3588-3590](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3588-L3590)）。未启用 promotion-on-hit 时完全不消耗这 16KB，也完全不介入 Get 路径。

#### 4.3.4 代码实践

**实践目标**：观察 CountMinSketch 频率门控「第一次访问被拒、第二次访问放行」的行为，并理解阈值上/下界 clamp 的意义。

**操作步骤**：

1. 阅读 [mooncake-store/tests/promotion_on_hit_test.cpp:1957-1997](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/promotion_on_hit_test.cpp#L1957-L1997)。这个测试把 `promotion_admission_threshold` 设为 **2**，然后：
   - 第一次 `Get(k_a)`：`increment` 返回 1，`1 < 2` → 被频率门拒绝，断言 `promotion_rejected_frequency` 计数 +1。
   - 对 `k_b` 第一次 Get 同样被拒；**第二次** `Get(k_b)`：`increment` 返回 2，`2 >= 2` → 通过频率门（随后可能被后续 cap 门拒绝，但这与频率无关）。
2. 再看阈值边界测试 [1573-1605](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/promotion_on_hit_test.cpp#L1573-L1605)：设 `threshold=0` 应被 clamp 到 1（否则频率门形同虚设）；设 `threshold=1000`（>255）应被 clamp 到 255（否则任何 key 都永远过不了门）。
3. 在构建目录跑这个测试（**命令待本地验证**）：

   ```bash
   cmake --build build --target promotion_on_hit_test
   ./build/mooncake-store/tests/promotion_on_hit_test \
       --gtest_filter='*Frequency*:*ThresholdClamp*'
   ```

**需要观察的现象**：

- threshold=2 时，每个 key 的「首次 Get」必然使 `promotion_rejected_frequency` 自增；「第二次 Get」才可能进入提升流程。
- threshold 被越界赋值时，测试断言 clamp 后的值，而非原始值。

**预期结果**：测试通过；你能在日志/指标里看到 `promotion_rejected_frequency` 的增量与 Get 次数的对应关系。若无运行环境，可改为「纯阅读型实践」：对着 [count_min_sketch.h:25-39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L25-L39) 手算一个全新 key 在连续 increment 下返回值的序列（1, 2, 3, …），并解释为何阈值设 1 等于「首次即放行」、设 255 等于「几乎永不放行」。

#### 4.3.5 小练习与答案

**练习 1**：CountMinSketch 的估计频率为什么「只高估、不低估」？这对频率门控有什么好处？

**参考答案**：因为多个 key 会哈希到同一行同一格子（冲突），某个 key 的格子计数里可能混入了别的 key 的访问，所以 `table[i][h_i(key)]` 只会偏大；取多行的 `min` 已经是在尽量抵消高估，但仍可能因每行都有冲突而整体偏大，绝不会偏小（自己每次访问必然让自己的每行格子 +1）。对频率门控的好处是「安全」：一个真正冷的 key 几乎不可能被误判为够热（因为它自己的计数确实低），也就不会被误提升、浪费 SSD 读和 RDMA 写的带宽；最坏情况只是个别冲突严重的 key 被略提前放行，代价可控。

**练习 2**：如果不做自动衰减（去掉 `total_increments_` 与 `decayLocked`），系统长时间运行后会出现什么问题？

**参考答案**：`uint8_t` 计数器会逐渐顶到 255（饱和）。一旦大量格子饱和，所有 key 的估计频率都接近 255，CountMinSketch 失去区分冷热的能力——频率门要么形同虚设（所有 key 都 ≥ 阈值），要么需要把阈值调到接近 255 才能拦住。自动衰减让旧访问按指数淡出，使计数器始终工作在未饱和区间，能持续反映「近期」热度而非「历史累计」热度。

**练习 3**：为什么频率门用 `increment`（自增后返回）而不是 `count`（只读）？用 `count` 会有什么不同？

**参考答案**：用 `increment` 意味着「当前这次 Get 本身也算一次访问」——返回值是包含本次在内的估计频率。若改用 `count`（只读不自增），则第一次 Get 返回 0、第二次才返回第一次后的值，等于把放行阈值「延后一次」，且 `TryPushPromotionQueue` 还得单独再调一次 `increment` 来记账，多一次加锁。用 `increment` 一步完成「记账 + 判定」，更简洁，语义也更直观：阈值 N = 「累计命中 N 次即放行」。

---

## 5. 综合实践

**任务：用一张「对象命运表」把淘汰、保护、频率三件事串起来。**

设想 Master 里有下面 5 个对象（同一分片，假设全部内存副本、refcnt=0、副本 COMPLETE），`now = T`：

| key | lease_timeout | soft_pin_timeout | hard_pinned | CountMinSketch 频率（仅 promotion 用） |
|---|---|---|---|---|
| A | T−10s（已过期） | 无 | false | — |
| B | T+4s（未过期） | 无 | false | — |
| C | T−5s（已过期） | T+20min（软钉中） | false | — |
| D | T−1s（已过期） | T+20min（软钉中） | false | — |
| E | T+1000s | — | **true** | — |

此刻全局内存使用率突破 95%，`EvictionThreadFunc` 触发 `BatchEvict`，参数简化为 `evict_ratio_target` 需淘汰 3 个、`evict_ratio_lowerbound` 需淘汰 2 个，`allow_evict_soft_pinned_objects_=true`。

请完成：

1. **第一遍**：哪些对象进入 `candidates`、哪些进入 `soft_pin_objects`、哪些被直接跳过？第一遍最多能淘汰几个？（提示：E 永远跳过；B 租约未过期跳过；A 是无 pin 候选；C、D 是 soft pin。）
2. **第二遍**：第一遍只淘汰了 A（1 个，未达 lowerbound=2），且 `no_pin_objects` 已空、`soft_pin_objects={C,D}`。会走 Pass A 还是 Pass B？被牺牲的 soft pin 是 C 还是 D？为什么？
3. **保护机制**：如果 `allow_evict_soft_pinned_objects_=false`，结论有何变化？E 在任何配置下会被淘汰吗？
4. **频率维度（迁移场景）**：假设 A、C 其实是 only-LOCAL_DISK 的冷对象被反复 Get。若开启 promotion-on-hit、阈值=2，A 被累计 Get 1 次、C 被累计 Get 3 次。谁的频率门会放行？放行后还要过哪几道门（水位/去重/上限）才真正入队提升？

**参考答案要点**：

1. `candidates={A}`，`soft_pin_objects={C,D}`，跳过 B（租约未过期）、E（hard pin）。第一遍只能淘汰 A（1 个）。
2. 走 **Pass B**（无 pin 候选已空，需 soft pin 补差额 1 个）。C、D 中 `lease_timeout` 更小的是 C（T−5s < D 的 T−1s），所以 **C 被淘汰**，D 保留。这印证「soft pin 内部也按 near-LRU（lease_timeout 升序）牺牲」。
3. 若 `allow_evict_soft_pinned_objects_=false`，C、D 在收集阶段就被跳过、不进 `soft_pin_objects`，第二遍无候选可补，`target_evict_num` 被夹到 0，**soft pin 全部保留**。E 在任何配置下都因 `IsHardPinned()` 永不淘汰。
4. A 频率=1 < 2 被拒；C 频率=3 ≥ 2 **通过频率门**。通过后还要依次过：水位门（DRAM 使用率是否 < 高水位）、去重门（是否已有在途任务/已出现 MEMORY 副本）、上限门（在途任务数是否 < `promotion_queue_limit_`），全部通过才 refcnt 钉住源 LOCAL_DISK 副本、登记 `PromotionTask` 入队（后续异步流程见 u6-l4）。

> 这个练习把「near-LRU 排序淘汰」「三重保护强度递增」「CountMinSketch 频率门控」三块拼在同一张表里。能独立完成它，说明你已经把本讲的核心机制内化。

---

## 6. 本讲小结

- Mooncake 的淘汰有两层观：`eviction_strategy.h` 的 LRU/FIFO 是教科书式策略接口（目前由单元测试驱动）；生产中 Master 真正执行的是 `BatchEvict`，它用 `lease_timeout` 当排序键实现 **near-LRU**，省掉了物理 LRU 链表的移动开销。
- `BatchEvict` 是**两遍扫描 + 高/低水位线**：第一遍只淘汰「无 soft pin 且租约过期」的对象；只有第一遍没摸到 `evict_ratio_lowerbound` 才启动第二遍，Pass A 继续清无 pin 对象，Pass B 在 `allow_evict_soft_pinned_objects_=true` 时连 soft pin 也按 `lease_timeout` 升序牺牲一部分。
- 三种保护强度递增：**lease**（默认 5s，每次 Get 续期，过期才可淘汰/删除）< **soft pin**（默认 30min，VIP 对象，Pass B 压力下可破）< **hard pin**（创建即定、不可变，永不淘汰）。`GrantLease` 用 `std::max` 只增不减；`PutEnd` 用 `GrantLease(0, soft_ttl)` 让新对象默认无硬租约。
- lease 过期**不主动删除**对象，只是把它从「受保护」变回「可淘汰」，真正的清理发生在下一轮 `BatchEvict` 或过期副本的 GC 旁路；显式 `Remove(force=false)` 则把 lease 当门禁，过期才放行（force 可绕过 lease 但绕不过副本就绪/复制任务检查）。
- `CountMinSketch` 用 `width×depth` 个 `uint8_t`（默认 16KB）近似统计访问频率，多行取 min、只高估不低估，靠累计触发 `decay`（全局右移减半）防饱和；它在 promotion-on-hit 里充当**第一道频率门**，`increment` 返回值 ≥ `promotion_admission_threshold_`（默认 2，clamp 到 [1,255]）才放行。
- `EvictionThreadFunc` 每 10ms 检查一次：使用率破高水位或分配失败置位 `need_mem_eviction_` 即触发淘汰，越超水位淘汰目标越大；成功清场后复位警报并上报指标。

---

## 7. 下一步学习建议

- **下一讲 u6-l4《Offload 与 Promotion-on-hit》**：本讲只讲了 CountMinSketch 作为「频率门」这一段，提升任务的**完整异步流程**（心跳下发 `PromotionObjectHeartbeat`、`PromotionAllocStart` 分配 DRAM、refcnt 钉住源副本、`NotifyPromotionSuccess` 翻转副本状态、reaper 兜底回收）以及 DRAM→SSD 的 offload-on-evict 都在下一讲展开。强烈建议紧接着学。
- **延伸阅读源码**：
  - [mooncake-store/src/master_service.cpp 的 `TryPushPromotionQueue`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3587-L3689) 四道准入门（频率/水位/去重/上限）的完整实现。
  - [master_service.h 的 `PromotionTask` 注释](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1121-L1149)，理解 holder 授权与 reaper TTL 的设计。
  - [types.h 的默认常量区](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L85-L97)，对照本讲所有 TTL/水位默认值。
- **动手建议**：把 `eviction_strategy_test` 与 `promotion_on_hit_test` 各跑一遍，前者巩固 LRU/FIFO 直觉，后者用 `--gtest_filter` 跑频率/水位/上限三类拒绝用例，对照本讲的「四道门」逐个验证。
