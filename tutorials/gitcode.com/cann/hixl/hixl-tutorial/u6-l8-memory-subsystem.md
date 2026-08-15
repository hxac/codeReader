# u6-l8 LLM-DataDist 内存子系统

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LLM-DataDist 自研内存子系统 `src/llm_datadist/memory/` 的分层组织（type / span / allocator / config / util 五个子目录各管什么）。
2. 读懂 `ScalableAllocator` 的「页（Page）→ 层（SpanLayer）→ 分裂/合并」分配算法，理解一次 `Alloc` 从请求尺寸到返回 `PageSpan` 的完整路径。
3. 理解 `PageSpan` 之间的伙伴链接（`SpanBuddyLink`）如何支撑空闲块的合并与复用。
4. 理解 `LlmMemPool` 门面如何把分配器接到 `AllocateCache`（`llm.MemPoolConfig` 选项）链路上，包括带超时的等待式分配。

本讲是单元六实现层的最后一篇深入课，承接 u6-l3「Cache 管理」中提到的「Allocate 从内存池切分（is_owned=true）」那句话——本讲就把这个内存池拆开看。

## 2. 前置知识

- **为什么需要自研内存池**：KV Cache 的分配/释放极其频繁（每生成一个 batch 就可能分配一批 tensor）。如果每次都调 `aclrtMalloc`/`aclrtFree`，代价高且会产生设备内存碎片。常见解法是：进程启动时一次性向 CANN 申请一大块内存（本项目中是 `aclrtMalloc` 一整块），之后所有小分配都在这块内存上做用户态切分——这就是内存池（memory pool）。
- **页（Page）与页长（PageLen）**：池子把内存按固定大小的「页」管理，默认页大小 64KB（`page_idem_num = 16`，即页大小的移位数）。一个块占多少页，就用 `PageLen`（`uint32_t`）表示。
- **伙伴系统（Buddy System）**：经典的物理内存管理算法（Linux 内核也用）。把内存按 2 的幂分级；分配时若当前层没有空闲块，就从更大的层「分裂」一块下来；释放时若自己的「伙伴」（buddy，同层且地址相邻的那块）也空闲，就合并成更大的块归还上层。这样可以有效抑制外部碎片。
- **`ge::MemBlock` / `ge::Allocator`**：CANN GE 框架提供的内存块与分配器抽象。`PageSpan` 继承 `ge::MemBlock`，使得本内存池能套进 GE 的通用接口。
- 建议先回顾 u6-l3 中 `CacheEntry.is_owned` 与 `ext_ref_count` 的语义：从池里切出来的 cache 由 LLM-DataDist 拥有并负责释放。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llm_datadist/memory/type/mem_size.h` | 基础类型 `MemSize`、对齐函数、`_KB/_MB/_GB` 字面量 |
| `src/llm_datadist/memory/span/span_layer_id.h` | `PageLen`/`SpanLayerId` 及「字节数 ↔ 页数/层号」换算函数 |
| `src/llm_datadist/memory/config/default_config.h` | 内存池全部默认参数（页大小、阈值、容量等） |
| `src/llm_datadist/memory/allocator/scalable_config.h` | `ScalableConfig` 配置结构体 |
| `src/llm_datadist/memory/allocator/memory_pool.h` | `MemoryPool` 抽象基类（接口面） |
| `src/llm_datadist/memory/allocator/scalable_allocator.cc/.h` | 核心分配器：分层 + 分裂 + 合并 |
| `src/llm_datadist/memory/span/span_layer.cc/.h` | 空闲块层：同页长的空闲 span 链表 |
| `src/llm_datadist/memory/span/page_span.cc/.h` | 内存块对象：持有地址/页长与伙伴链接 |
| `src/llm_datadist/memory/span/span_buddy_link.h` | 伙伴双向链接（prev/next 指针对） |
| `src/llm_datadist/memory/span/span_layer_lut.h` | 层查找表（`std::set` 快速定位最近可用层） |
| `src/llm_datadist/common/llm_mem_pool.cc` | `LlmMemPool` 门面：互斥、地址→块映射、超时等待 |
| `src/llm_datadist/cache_mgr/data_cache_engine.cc` | 池的创建入口（解析 `llm.MemPoolConfig`、整块申请、注册） |
| `src/llm_datadist/cache_mgr/cache_manager.cc` | `AllocateCache` 从池切 tensor 的消费点 |

## 4. 核心概念与源码讲解

### 4.1 memory 子系统分层总览与 MemoryPool 抽象

#### 4.1.1 概念说明

`src/llm_datadist/memory/` 是一个自包含的通用内存池库，不依赖 LLM-DataDist 其他业务代码，按职责分五个子目录：

- `type/`：纯值类型（`MemSize`、`MemAddr`、`PageLen` 等换算函数），全部是 `constexpr`，零运行时开销。
- `config/`：默认参数常量。
- `span/`：内存块对象（`PageSpan`）、空闲块层（`SpanLayer`）、伙伴链接、层查找表。
- `allocator/`：分配器本体（`ScalableAllocator`）与抽象接口（`MemoryPool`）。
- `util/`：侵入式链表（`Link`/`LinkNode`）与对象池（`object_allocator.h`）等工具。

最顶层只暴露一个极窄的接口面：

#### 4.1.2 核心流程

`MemoryPool` 抽象基类只约定四个能力：分配、释放、标识、打印状态。整个子系统的对外合同就是这四个虚函数。

#### 4.1.3 源码精读

[ memory_pool.h:19-29](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/memory_pool.h#L19-L29)：`MemoryPool` 抽象基类——`Alloc(allocator, size)` 返回 `ge::MemBlock*`、`Free(block)` 释放、`GetId()` 与 `PrintDetails(level)` 用于日志诊断。

[mem_size.h:15-40](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/type/mem_size.h#L15-L40)：定义 `MemSize`（`unsigned long long`）、512 字节对齐常量 `MEM_SIZE_ALIGN`、`_KB/_MB/_GB` 用户定义字面量，以及向上取整的对齐函数 `MemSize_GetAlignedOf`——后面所有「字节数按页对齐」都靠它。

[span_layer_id.h:20-47](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/span_layer_id.h#L20-L47)：本讲最重要的换算约定——`PageLen` 就是 `uint32_t` 的页数；`PageLen_GetLenFromSize(size, page_idem_num) = size >> page_idem_num`（字节→页数，右移即除以页大小）；`PageLen_GetMemSize` 反向；`PageLen_ForwardAddr` 求伙伴地址（当前地址向前推 N 页）。注意 `SpanLayerId` 只是 `PageLen` 的别名——**层号 = 该层每个 span 的页数**，这是理解整个分配器的钥匙。

[default_config.h:22-52](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/config/default_config.h#L22-L52)：全部默认参数——页大小 `PAGE_SIZE_IDEM_DEFAULT = 16`（2^16 = 64KB）、单层缓存阈值 8GB、层内 span 数上限 10240、可分配总内存阈值 30GB 等。这些常量被 [scalable_config.h:19-31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_config.h#L19-L31) 的 `ScalableConfig` 结构体逐字段吸收，业务侧（后面 4.4 的 `DataCacheEngine`）只覆盖 `page_idem_num` 和 `page_mem_size_total_threshold` 两个字段，其余用默认值。

#### 4.1.4 代码实践

1. **实践目标**：建立「层号 = 页数」的手感。
2. **操作步骤**：打开 `span_layer_id.h`，手动计算以下几个值（设 `page_idem_num = 16`，即 64KB 页）：
   - `SpanLayerId_GetIdFromSize(1_MB, 16)` → `1MB >> 16 = 16`，即 1MB 对应第 16 层；
   - `SpanLayerId_GetIdFromSize(64_KB, 16)` → 第 1 层；
   - `PageLen_ForwardAddr(16, 16, base)` → `base + 1MB`。
3. **需要观察的现象**：任意尺寸换算成层号后，相邻层之间尺寸恰好差 2 倍。
4. **预期结果**：能不假思索地说出「分配 1MB 会先去第 16 层找空闲 span」。
5. 也可以写一段 10 行的 `constexpr` 小测试（示例代码，非项目原有）验证换算，编译时只需 include 该头文件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SpanLayerId` 不单独定义新类型而直接 `using SpanLayerId = PageLen`？
**答案**：因为层号的语义就是「该层每个 span 包含的页数」，两者数值域和换算完全一致，复用别名既减少类型数量，也让 `GetPageLen()` 的返回值可以直接当下标去索引 `span_layers_`。

**练习 2**：`MEM_SIZE_ALIGN = 512` 用在哪里？
**答案**：它是比页更细的对齐单位，供上层（如 cache tensor 地址对齐）使用；`ScalableAllocator` 自己的对齐用的是页大小（见 4.2 的 `GetAllocSize`）。

### 4.2 ScalableAllocator：分层分配与分裂

#### 4.2.1 概念说明

`ScalableAllocator` 是内存池的心脏，实现的是「固定总量的池 + 伙伴式分裂/合并」。它内部维护两组结构：

- `span_layers_`：一个 `vector<SpanLayer*>`，下标即层号。第 N 层里挂的都是「N 页长的空闲 span」。层是懒惰创建的（用到才建）。
- `occupied_spans_`：已分配出去的 span 的侵入式链表，用于统计与 `Finalize` 时检查是否全部归还。

分配策略一句话：**先去「尺寸正好」的层拿；没有就从更大的层分裂一块下来；释放时尽量与伙伴合并回高层**。

#### 4.2.2 核心流程

```
Alloc(allocator, size):
  1. size 超过总阈值 page_mem_size_total_threshold → 拒绝（返回 nullptr）
  2. alloc_size = max(size, 一页) 向上按页对齐
  3. fix_layer_id = alloc_size >> page_idem_num     ← 目标层号
  4. FetchLayerSpan(fix_layer_id)                    ← 本层有空闲 → 直接弹出
  5. 本层没有 → span_layer_lut_->FindFitLayerId(fix_layer_id+1, lift_max)
       在「还有存货的层」集合里找 > fix_layer_id 的最小层号
  6. FetchSplitedSpan: 从大层弹出一个 span，反复对半分裂，
       每次分裂把「低地址半块」留在中间层，「高地址半块」继续处理，
       最终返回尺寸正好 fix_layer_id 的那一块
  7. 记录 real_size（用户真实请求）与理论尺寸统计

Free(block):
  1. 从 occupied_spans_ 摘除
  2. TryMergeNext / TryMergePrev: 若相邻伙伴空闲且未在使用 → 合并
  3. FreeSpan: 把（可能已变大的）span 压回对应层
```

分裂的几何含义（设从 4 页的 span 分出 1 页）：

\[ \underbrace{[0,1)}_{\text{留第1层}} \big| \underbrace{[1,2)}_{\text{留第2层(与后续合并表述一致，见下)}} \big| \underbrace{[2,3)}_{\text{留第1层}} \big| \underbrace{[3,4)}_{\text{返回给用户}} \]

实际上代码是一次分裂只切一刀：4 页 span 分裂成「2 页 + 2 页」，前 2 页留在第 2 层（ buddy 链接挂到后 2 页上），后 2 页继续分裂成 1+1，前 1 页留第 1 层，后 1 页返回。对半地址由 `PageLen_ForwardAddr(left_page_len, ...)` 算出。

#### 4.2.3 源码精读

[scalable_allocator.cc:159-181](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L159-L181)：`AllocImp` 分配主干——先 `GetAllocSize` 对齐、算出 `fix_layer_id`；`FetchLayerSpan` 精确层命中；未命中则用 LUT 向上找最近有货的层再 `FetchSplitedSpan` 分裂。成功后 `SetRealSize(size)` 记录用户真实请求（区别于按页对齐后的 `GetSize()`）。

[scalable_allocator.cc:67-69](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L67-L69)：`GetAllocSize`——小于一页按一页算，否则 `MemSize_GetAlignedOf` 向上按页对齐。这就是「分配 1 字节也占 64KB」的内部碎片来源，也是页大小要按业务块尺寸合理配置的原因。

[scalable_allocator.cc:135-157](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L135-L157)：`SplitSpan` 分裂核心——`left_page_len = fit_layer_id - fix_layer_id` 是「留在中间层的半块」的页数；用 `PageLen_ForwardAddr` 从当前 span 起点算出伙伴（高地址半块）地址，`BlockAlloc` 为其新建 `PageSpan` 元数据对象；`span->SetBuddy(*buddy_span)` 建立伙伴链接，低地址半块 `SetPageLen(left_page_len)` 后压回第 `left_page_len` 层，高地址半块 `OccupySpan(buddy_span, fix_layer_id)` 作为本次分配结果。

[scalable_allocator.cc:119-133](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L119-L133)：`FetchSplitedSpan`——注意它只在 `fit_layer_id > fix_layer_id` 时工作，且要求目标层已存在，否则返回 nullptr（分配失败）。分裂并非递归实现，而是「一层一刀」地在一条调用链上完成。

[span_layer_lut.h:60-91](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/span_layer_lut.h#L60-L91)：`SpanLayerQuickLut`——用一个 `std::set<SpanLayerId>` 维护「还有空闲 span 的层号」；`FindFitLayerId` 就是 `lower_bound(page_len)`，即 O(log n) 找到 ≥ 目标层的最小有货层。层的增删通过 `OnLayerAddSpan`/`OnLayerRemoveSpan` 在每次 push/pop 时增量维护（只在层从空变非空、非空变空的边界时刻更新集合）。

[scalable_allocator.cc:100-117](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L100-L117)：`FetchSpanLayer`——层对象也是懒惰创建的，从 `layer_allocator_`（对象池）拿内存后 placement new 一个 `SpanLayer`，容量由 `GetlayerSpanCapacity` 决定；`uncacheable_layer_start_` 之上的超大层和第 0 层容量为 0（见 [scalable_allocator.cc:71-76](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L71-L76)），即超大块不做缓存复用。

[scalable_allocator.cc:345-366](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L345-L366)：`InitFixSizedAllocator`——池的「灌水」入口：把外部传入的一整块内存（`base_addr, size`，即 `aclrtMalloc` 出来的整块设备内存）包装成一个最大的 `PageSpan`，清零引用计数后压入对应层。此后所有分配都在这块固定内存内切分，池子永远不会向系统再要内存——**固定容量、用完即拒**。

[scalable_allocator.cc:183-206](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L183-L206)：`Alloc` 对外的壳——超过 `page_mem_size_total_threshold` 直接拒绝并打日志；成功时维护 `theory_size_`（对齐后）与 `real_theory_size_`（真实请求）两组水位统计，用于观测碎片率。

#### 4.2.4 代码实践

1. **实践目标**：验证「层号 = 页数」以及 LUT 查找行为。
2. **操作步骤**（源码阅读型实践）：
   - 设 `page_idem_num = 16`、池大小 1GB。跟踪 `Alloc(allocator, 300_KB)`：对齐后 5 页（320KB）→ `fix_layer_id = 5`；若第 5 层空，LUT 可能命中第 16 层（1MB span，初始化时整池灌入的那块）。
   - 手动模拟 `SplitSpan(5, 16, span)`：`left_page_len = 11`，伙伴地址 = `span.addr + 11 页`；11 页半块留在第 11 层，5 页半块继续分裂成 `left_page_len = 6`……直到只剩 5 页返回。
   - 注意一次 `FetchSplitedSpan` 只切一刀（4.2.2 的说明），多次分裂需要多次 `AllocImp` 循环驱动——请核对源码确认实际行为与你画的图一致。
3. **需要观察的现象**：分裂路径上每留在中间层一块，伙伴链接就多一条。
4. **预期结果**：能画出「1MB span → 满足 300KB 请求」的分裂树，标出每块的层号与地址区间。
5. 分裂-合并的完整运行为「待本地验证」（需要构造带设备内存的环境，或参考 `tests/cpp/llm_datadist/data_cache_engine_unittest.cc` 中已有的池相关用例）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AllocImp` 里查找更大层用 `FindFitLayerId(fix_layer_id + 1, config_.span_layer_lift_max)` 而不是遍历 `span_layers_`？
**答案**：`span_layers_` 是按层号索引的数组，绝大多数层可能根本没创建或为空；`SpanLayerQuickLut` 用有序集合只记录「有货的层」，`lower_bound` 一次跳到最近的有货层，避免线性扫几十万个空层。`span_layer_lift_max` 限制最多向上抬升的层数，防止为了一个小请求去拆超大块（默认不限制）。

**练习 2**：`SetRealSize` 记录的「真实尺寸」有什么用？
**答案**：用于统计——`theory_size_` 是按页对齐后的占用，`real_theory_size_` 是用户真实请求量，两者之差即内部碎片。`PrintDetails` 打印这些水位，帮助运维判断页大小配置是否合理。

**练习 3**：如果请求尺寸大于 `page_mem_size_total_threshold` 会怎样？
**答案**：`Alloc` 入口直接打 ERROR 日志返回 nullptr，不会进入分层逻辑；上层 `LlmMemPool::Alloc` 得到 nullptr，`CacheManager::Allocate` 据此报 `LLM_OUT_OF_MEMORY`（见 4.4.3）。

### 4.3 SpanLayer 与 PageSpan：伙伴链接与复用机制

#### 4.3.1 概念说明

- `PageSpan` 是「一块内存」的元数据对象：继承 `ge::MemBlock`（携带地址、大小、引用计数），额外携带 `block_addr_`、`page_len_`（页数）、`real_size_`，以及最关键的 `buddy_link_`——一个前向/后向的伙伴指针对。
- `SpanLayer` 是「同页长空闲块」的容器：内部就是一条 `Link<PageSpan>` 侵入式空闲链表，`PushSpan`/`PopSpan` 都是 O(1) 链表头操作。
- **伙伴链接（buddy link）是复用与合并的骨架**：分裂时把留在中间层的半块与继续下分的半块用 `buddy_link_` 串起来；释放时顺着 `GetPrevBuddy()`/`GetNextBuddy()` 检查邻居是否空闲，空闲则合并、恢复成分裂前的形状。

注意区分两条链：`LinkNode<PageSpan>` 的 `link_` 把 span 挂进 `SpanLayer` 的空闲链表（或 `occupied_spans_`），而 `SpanBuddyLink buddy_link_` 表达的是地址上的伙伴关系。

#### 4.3.2 核心流程

page span 的复用机制完整生命周期：

```
[初始化] 整块池内存 → 一个最大 PageSpan → 压入第 N 层（N=总页数）
[分配]   第 N 层弹出 → SplitSpan 逐层切刀：
           每一刀：低半块 SetBuddy(高半块) 后压回中间层，高半块继续
           最后一刀的高半块 → OccupySpan → 进入 occupied_spans_
[释放]   Free(span):
           TryMergeNext: next_buddy 空闲且在层中 → PickOutBuddy 摘出 →
                         span.MergeBuddy(next)（page_len 相加、缝链接）→ 销毁 next 元数据
           TryMergePrev: 对 prev_buddy 对称处理（prev 吸收 span）
           FreeSpan: 最终 span 压回「现页数」对应的层 → 等待下次复用
[复用]   下次同尺寸 Alloc 命中该层 → PopSpan 直接弹出，无需再分裂
```

#### 4.3.3 源码精读

[page_span.h:25-46](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/page_span.h#L25-L46)：`PageSpan` 类定义——多重继承 `ge::MemBlock`（GE 内存块合同）与 `LinkNode<PageSpan>`（侵入式链表节点）；`Alloc(page_len)` 设置页长并把引用计数从 0 提到 1（`AddCount`），表示「被用户占用」。

[page_span.cc:16-24](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/page_span.cc#L16-L24)：`SetBuddy`——把 `buddy_span` 插到自己的 next 位置，同时接好原本 next 的 prev 指针，是一次标准的双向链表插入，建立「低半块 ⇄ 高半块」的伙伴关系。

[page_span.cc:26-39](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/page_span.cc#L26-L39)：`MergeBuddy`——只在「我的 next 是它、它的 prev 是我」这一方向上合并：把它的 next 接到自己身上，`page_len_ += buddy.GetPageLen()`（页数相加 = 尺寸翻倍还原），链接清空后 `try_split_page_len_` 归零（「恢复到分裂前」）。

[scalable_allocator.cc:208-244](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L208-L244)：合并三部曲——`PickOutBuddy` 先做三重前置检查（非空、引用计数为 0 即空闲、所在层存在）才把伙伴从其空闲层摘出；`TryMergePrev` 让 prev 吸收自己（`prev_buddy->MergeBuddy(span)` 后销毁 span 元数据）；`TryMergeNext` 对称。合并失败（伙伴在使用中或已不存在）就保持原尺寸直接归还。

[scalable_allocator.cc:246-263](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L246-L263)：`Free`——从 `occupied_spans_` 摘除、扣减统计后，先 `TryMergeNext` 再 `TryMergePrev`（两方向都试，能合多大合多大），最后 `FreeSpan` 压回层里。

[scalable_allocator.cc:287-294](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/allocator/scalable_allocator.cc#L287-L294)：`FreeSpan`——按合并后的**当前页数**取层压入，这就是复用：下次同尺寸请求 `FetchLayerSpan` 直接命中，免去分裂。

[span_layer.h:20-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/span_layer.h#L20-L63)：`SpanLayer`——`free_link_` 是 `Link<PageSpan>` 空闲链表；`GetPageSize() = GetSize() * layer_id_`（块数 × 每块页数）供统计；`PushSpan/PopSpan/Remove` 全是 O(1)/O(n) 链表操作。

[span_layer.cc:16-28](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/span_layer.cc#L16-L28)：`Release`——池销毁时逐个弹出空闲 span 归还元数据对象池；若 span 仍有 `HasSplited()`（buddy 链非空，说明分裂关系未完全合并）则打 ERROR 日志，提示存在未归还的碎片。

[span_buddy_link.h:16-52](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/memory/span/span_buddy_link.h#L16-L52)：`SpanBuddyLink`——就是一对 `volatile` 的 prev/next 指针加判空辅助，`IsEmpty()` 表示该 span 没有未合并的伙伴。

#### 4.3.4 代码实践

1. **实践目标**：亲手推演一次「分配 → 释放 → 复用」的 span 生命周期。
2. **操作步骤**：
   - 假设池 128MB、页 64KB（共 2048 页），初始第 2048 层有一个 span。
   - 依次跟踪 `Alloc(128_KB)`（第 2 层）、`Alloc(64_KB)`（第 1 层）两次调用，画出每刀分裂后各层的 span 分布与 buddy 链接。
   - 再跟踪两次 `Free`：观察合并顺序（先 next 后 prev），确认最终是否还原成一个 2048 页的大 span 回到第 2048 层。
   - 最后再 `Alloc(128_KB)`，验证直接从第 2 层弹出（复用，零分裂）。
3. **需要观察的现象**：完全对称的 alloc/free 序列结束后，所有 span 应合并回初始大块；若中途有未释放的块，对应伙伴无法合并。
4. **预期结果**：得到一张状态表，行是「操作」，列是「各层 span 数 + buddy 关系」。
5. 若想实际运行验证，可参考 `tests/cpp/llm_datadist/data_cache_engine_unittest.cc` 的组织方式补一个最小 gtest（「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`PickOutBuddy` 为什么要求 `buddy_span->GetCount() == 0U`？
**答案**：`GetCount()` 是 `ge::MemBlock` 的引用计数。计数非 0 说明伙伴正被用户使用，绝不能拿来合并；这也解释了为什么释放路径必须等引用归零后（`Free` 被调用时）才有合并机会。

**练习 2**：`PageSpan` 同时继承 `ge::MemBlock` 和 `LinkNode<PageSpan>`，两条链各是什么用途？
**答案**：`LinkNode` 的 `link_` 用于把 span 挂进 `SpanLayer` 的空闲链表或 `occupied_spans_` 占用链表（容器关系）；`SpanBuddyLink buddy_link_` 表达地址相邻的伙伴关系（合并依据）。两者独立维护，互不干扰。

**练习 3**：`try_split_page_len_` 在 `MergeBuddy` 里被清零，注释写 "Restore to original"，它表达什么？
**答案**：完整合并意味着这块内存恢复到了分裂前的形状，之前记录的「尝试分裂到的页长」状态作废。这是一个防残留状态的清理动作，保证 span 元数据可被下一次分裂复用。

### 4.4 LlmMemPool 门面与 Cache 分配链路

#### 4.4.1 概念说明

`ScalableAllocator` 本身不是线程安全的，也不做「地址 → 块」的反查。`LlmMemPool`（位于 `src/llm_datadist/common/`，在 memory 子系统之外）是包在它外面的门面，补齐三件事：

1. **互斥**：`mu_` 保护分配/释放的串行化。
2. **地址反查**：`addr_to_mem_block_`（地址 → `ge::MemBlock*` 的 map），让用户拿裸地址 `Free`。
3. **等待式分配**：`Alloc(size, timeout_in_ms)` 在池满时挂条件变量等待别人释放，超时返回 nullptr——这正是 KV Cache「先来先得、池满排队」语义的实现。

它的内嵌类 `LlmMemAllocator` 实现 `ge::Allocator` 接口，把 GE 的 `Malloc/Free` 转发给 `ScalableAllocator`，从而使 `PageSpan`（`ge::MemBlock`）与分配器形成 GE 风格的闭环。

#### 4.4.2 核心流程

从用户选项到一次 Cache 分配的完整链路：

```
Initialize options 含 llm.MemPoolConfig = {"memory_size": N, "page_shift": 16}
  └─ DataCacheEngine::InitializeDeviceMemoryPool
       ├─ ParseMemoryPoolConfig（JSON 解析 + page_shift 范围校验 [10,31)）
       ├─ new LlmMemPool(config)
       ├─ aclrtMalloc 整块 N 字节设备内存          ← 池的物理来源
       ├─ npu_mem_pool_->Initialize(base, N)        ← InitFixSizedAllocator 灌水
       ├─ GlobalMemManager::RegisterMem(整块注册)   ← 池整块注册一次，免逐块注册（u6-l3）
       └─ cache_manager_->SetNpuMemPool(pool)

AllocateCache（Python allocate_cache_v2 驱动）
  └─ CacheManager::Allocate
       └─ 对每个 tensor: mem_pool->AllocShared(tensor_size)
            ├─ LlmMemPool::Alloc（加锁）→ allocator_.Malloc → ScalableAllocator::Alloc
            │    └─ FetchLayerSpan / FetchSplitedSpan → PageSpan
            ├─ addr_to_mem_block_[addr] = span
            └─ MakeShared：shared_ptr 析构时自动回池
```

#### 4.4.3 源码精读

[llm_mem_pool.cc:30](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L30) 与 [llm_mem_pool.cc:17-28](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L17-L28)：`LlmMemPool` 构造时把 `span_allocator_`（元数据对象池）与 config 一起交给 `ScalableAllocator`；内嵌 `LlmMemAllocator::Malloc/Free` 是 GE 接口到 `ScalableAllocator` 的一对一转发。

[llm_mem_pool.cc:39-51](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L39-L51)：`Initialize`——校验 `page_shift ∈ [10, 31)` 且页大小不超过池总大小，然后 `InitFixSizedAllocator` 灌水。池内存来自外部（`aclrtMalloc`），池本身不拥有释放权（释放由 `DataCacheEngine::Finalize` 里的 `aclrtFree` 负责）。

[llm_mem_pool.cc:53-74](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L53-L74)：`Alloc/Free`——加锁后调 `allocator_.Malloc`，成功则登记 `addr_to_mem_block_`；`Free` 按地址反查 span、调用其 `Free()`（触发 4.3 的合并回层），erase 表项并 `cv_.notify_all()` 唤醒等待者。

[llm_mem_pool.cc:76-99](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L76-L99)：带超时的 `Alloc(size, timeout_in_ms)`——循环「先试分配 → 到期返回 nullptr → 加锁后再试一次（防 lost wakeup）→ `cv_.wait_until` 等待释放通知」。这是池满时请求排队的核心机制。

[llm_mem_pool.cc:101-116](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/llm_mem_pool.cc#L101-L116)：`MakeShared/AllocShared`——返回带自定义 deleter 的 `shared_ptr<void>`，引用归零自动 `Free` 回池。Cache 的 RAII 式持有可能就建立在这上面。

[data_cache_engine.cc:35-60](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L35-L60)：`ParseMemoryPoolConfig`——JSON 格式为 `{"memory_size": <unsigned>, "page_shift": <unsigned 可选>}`，校验 page_shift 范围与可寻址页数上限（`pool_size >> page_shift` 必须小于 `uint32_t` 最大值，因为 `PageLen` 是 32 位）。

[data_cache_engine.cc:275-301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L275-L301)：`InitializeDeviceMemoryPool`——无 `llm.MemPoolConfig` 选项则整条路径跳过（对应 u6-l3 所说「C++ 公开 Allocate 当前不可用」）；有则建池、`aclrtMalloc(ACL_MEM_TYPE_HIGH_BAND_WIDTH)` 拿整块设备内存、初始化池、经 `GlobalMemManager` 整块注册（所以池内切出的每个 cache 免逐块注册）、最后把池指针交给 `CacheManager`。host 侧的 `InitializeHostMemoryPool`（[data_cache_engine.cc:303-328](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L303-L328)）结构完全对称，用 `aclrtMallocHost` 与 `llm.HostMemPoolConfig`。

[cache_manager.cc:171-192](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L171-L192)：`CacheManager::Allocate`——按 `CachePlacement` 选 npu/host 池；算出单个 tensor 字节数后循环 `num_tensors` 次 `AllocShared(tensor_size)`；任一次失败则 `LogPoolState()`（打印分配器内部各层水位）并报 `LLM_OUT_OF_MEMORY`。随后（[cache_manager.cc:193-204](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/cache_manager.cc#L193-L204)）`is_owned = true`、地址登记入 `CacheEntry`——即 u6-l3 讲过的「池切分路径」。

#### 4.4.4 代码实践

1. **实践目标**：打通「配置 → 建池 → AllocateCache 切分」的完整调用链笔记。
2. **操作步骤**：
   - 从 [data_cache_engine.cc:275](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/cache_mgr/data_cache_engine.cc#L275) 出发，自上而下抄录函数调用链直到 `scalable_allocator.cc` 的 `AllocImp`，标注每一层增加的能力（JSON 解析 / 整块申请 / 互斥 / 地址反查 / 分裂）。
   - 再看 Python 侧入口：`src/python/llm_datadist/` 中搜索 `allocate_cache_v2`，确认它是当前唯一驱动 `AllocateCache` 的公开途径（u6-l3 结论）。
   - 若有 NPU 环境：Initialize 时设 `llm.MemPoolConfig = '{"memory_size": 1073741824, "page_shift": 16}'`，随后连续 allocate 多个 cache，用 `LogPoolState`（或把日志级别调到 ERROR 触发 `PrintDetails`）观察各层 span 分布变化。
3. **需要观察的现象**：连续分配同尺寸 tensor 时，第 2 次起应命中已有层（无需分裂）；交错分配/释放不同尺寸后，层分布出现「中间层堆积」，全部释放后回落到大层。
4. **预期结果**：一份从 `llm.MemPoolConfig` 到 `PageSpan` 的调用链图 + 一份层水位变化的观察记录。
5. 无硬件环境时，实践退化为源码阅读型：调用链图照画，层水位观察标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`Alloc(size, timeout_in_ms)` 里为什么在拿到 `mu_cv_` 锁之后还要再 `Alloc` 一次？
**答案**：防 lost wakeup——若只「检查-然后-等待」，在检查完到挂起等待之间可能有别的线程释放并 notify，通知会丢失；先加锁再复查一次，保证不遗漏这段时间窗口内出现的空闲内存。

**练习 2**：为什么池要经 `GlobalMemManager::RegisterMem` 整块注册一次，而不是每个 cache 切出来后逐块注册？
**答案**：整块注册后，池内任何子区间天然落在已注册范围内，远端即可单边访问（u6-l3 的 `remote_accessible` 机制）；逐块注册会带来大量重复的注册/解注册调用与元数据开销，且池的地址本就连续。

**练习 3**：`LlmMemPool::Alloc` 的锁与 `ScalableAllocator` 内部有无锁是什么关系？
**答案**：`ScalableAllocator` 完全无锁，线程安全由外层 `LlmMemPool::mu_` 的 `lock_guard` 保证；这种「算法层无锁、门面层串行」的分工让分配器代码简单可测（单测无需并发框架）。

## 5. 综合实践

把本讲三个模块串起来做一次「纸面内存池仿真」：

1. 写一个 50 行左右的最小 C++ 程序（**示例代码，非项目原有**）：`#include "memory/allocator/scalable_allocator.h"`，构造 `SpanAllocator` + `ScalableConfig{page_idem_num = 16, page_mem_size_total_threshold = 1_GB}`，`InitFixSizedAllocator` 灌入一块 1GB 的模拟内存（host 上 `malloc` 即可，无需 NPU——分配器只操作地址区间，不触碰设备）。
2. 模拟 KV Cache 负载：循环执行 `{Alloc(1_MB) × 4, Alloc(128_KB) × 8, 逆序 Free 一半}` 若干轮。
3. 每轮结束后调 `PrintDetails(DLOG_EVENT)`，记录：占用 span 的尺寸直方图（`Using` 段）、各空闲层的块数（`Freed` 段）、`theory_size_` 与 `real_theory_size_` 的差值。
4. 回答两个问题：
   - 长期「分配-释放」循环后，中间层（如第 2、8 层）的空闲块是越来越多还是趋于稳定？为什么（伙伴合并的作用）？
   - 把 `page_shift` 从 16 改成 12（16KB 页）重跑，`theory/real` 差值如何变化？这说明页大小该如何按业务块尺寸选取？
5. 编译可参照 `tests/cpp/CMakeLists.txt` 的 include 路径组织；若不便编译，退化为纯阅读 + 手工推演，并在笔记中标注「待本地验证」。

## 6. 本讲小结

- LLM-DataDist 内存子系统按 `type / config / span / allocator / util` 五层组织，对外只暴露 `MemoryPool` 四函数抽象，是一个可独立复用的通用库。
- `ScalableAllocator` 实现固定容量池上的伙伴式分配：**层号 = 页数**，分配走「精确层命中 → LUT 找最近有货大层 → 逐刀对半分裂」，池水来自启动时一次性 `aclrtMalloc` 的整块内存，用完即拒不扩张。
- `SpanLayerQuickLut` 用 `std::set` 的 `lower_bound` 把「找更大层」做到 O(log n)，只维护非空层的增删事件。
- `PageSpan` 走两条链：`LinkNode::link_` 挂空闲/占用链表，`SpanBuddyLink` 记录地址伙伴关系；释放时先 next 后 prev 双向尝试合并，合并成功页数翻倍回到高层，等待同尺寸请求直接弹出复用。
- `LlmMemPool` 门面补齐互斥、地址反查与「池满排队」的条件变量等待（防 lost wakeup 的双检写法），并以 `AllocShared` 提供 RAII 回收。
- 池由 `llm.MemPoolConfig` / `llm.HostMemPoolConfig` 选项驱动创建（JSON：`memory_size` + 可选 `page_shift ∈ [10,31)`），整块经 `GlobalMemManager` 注册，`CacheManager::Allocate` 对每个 tensor 调 `AllocShared` 切分，`is_owned = true`。

## 7. 下一步学习建议

本讲补完了单元六最后一块实现拼图。建议：

1. 回读 [tests/cpp/llm_datadist/data_cache_engine_unittest.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/data_cache_engine_unittest.cc)，看官方测试如何构造池与断言分配行为，检验你本讲形成的模型。
2. 对比 FabricMem 的内存体系（u5-l2 的 `FabricMemAllocator` 与 VMM 三步机制），体会「整块申请 + 用户态切分」与「虚拟地址重映射」两种池化思路的差异。
3. 若关注性能，进入单元八：u8-l1 基准测试（内存池参数对 KV 传输吞吐的影响）与 u8-l3 profiling（在分配路径上埋点观测 `real/theory` 碎片率）。
