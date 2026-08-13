# SVM v3 地址空间与分配器架构

## 1. 本讲目标

本讲是 SVM（共享虚拟内存）单元的收尾篇，专门回答一个问题：**当上层 Runtime 调用 `halMemAlloc` 申请一段设备内存时，那段虚拟地址（VA）到底是谁、用什么数据结构、按什么策略发出来的？**

学完后你应当能够：

- 说清 SVM v3 的「多级分配器」是如何分工的：`gen_allocator`（地基）→ `MGA`（多对齐 VA 空间）→ `cache_malloc`（小内存缓存池）→ `malloc_mng`（总调度 + handle 索引）。
- 理解 `gen_allocator` 用「区间（range）+ 空闲块（area）+ 两棵红黑树」管理任意多段不连续地址的核心思路。
- 掌握 MGA（multi-alignment gen allocator）如何用「基座池 + 子池」把 4K/64K/2M/1G 四种对齐的地址分开管理、按需扩张/收缩。
- 理解 cache 层如何用预申请的大段内存做「切块」，避免每次小分配都陷入内核，以及 `cache_recycle_seg` 如何处理「设备还在用、暂时还不了」的延迟回收。
- 结合近期一次提交，理解分配器在高并发下的性能优化思路。

本讲依赖 **u4-l1（SVM 初始化）** 与 **u4-l3（VMM 虚拟/物理地址分离）**。u4-l3 已经讲过「申请 = 先预留 VA 再 mmap、再进内核 populate 物理页」的两阶段设计；本讲聚焦于「VA 这一段地址是怎么从地址空间里切出来的」。

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉。

**比喻一：虚拟地址空间 = 一本空白的地址簿。**
SVM 在进程启动时，会预留一大段连续的虚拟地址区间（例如设备侧默认预留 256GB，Host 侧预留 2TB，见后续源码）。这段区间一开始**没有对应的物理内存**，就像一本写满了门牌号、但房子还没盖的地址簿。分配器的工作就是：在这些门牌号里划出一块给某次申请，并记下「这块归谁、多大」。

**比喻二：分配器 = 一个会切蛋糕、也会拼蛋糕的管家。**
- 「切蛋糕」：一块大的空闲地址被一次小申请切走一段，剩下两块小的（split）。
- 「拼蛋糕」：当一段地址被归还，且它左右邻居也是空闲的，就把它们合并成一块大的（merge），避免地址空间碎片化。
红黑树（rbtree）就是这个管家的账本：按地址排序一棵、按大小排序一棵，这样「按地址查」和「按大小找」都快。

**比喻三：多级分配器 = 银行柜台分级办理。**
- `gen_allocator` 是**最底层的出纳**，只会切/拼地址，不会去跟内核打交道。
- `MGA` 是**大堂经理**，把地址按对齐粒度（4K/64K/2M/1G）分到不同窗口办理，地址不够了再去找内核「批地」（mmap 预留）。
- `cache_malloc` 是**VIP 快速窗口**：预先批一大块地放着，小额申请直接从这里切，免去每次都找内核的开销。
- `malloc_mng` 是**总台**：每次申请/释放都先在它这里登记（handle 红黑树），再决定去哪个窗口。

掌握这三个比喻，下面的源码就是它们的具体实现。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/ascend_hal/svm/v3/assign/` 目录下，这是 SVM v3 的「分配子系统」。

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `assign/gen_allocator/gen_allocator.c` | 通用区间分配器（红黑树实现） | **地基**：MGA 与 cache 都复用它 |
| `assign/va_allocator/mga.c`（含 `mga.h`） | 多对齐 VA 分配器 | **大堂经理**：管理 4K/64K/2M/1G 四种对齐 |
| `assign/va_allocator/va_dev_default_allocator.c` | 设备默认 VA 分配器 | MGA 的装配者（设置 expand/shrink 回调） |
| `assign/cache_malloc/cache_allocator.c` | cache 分配器实例管理 | **VIP 窗口骨架**：每个 devid×flag 一个 ga_inst |
| `assign/cache_malloc/cache_malloc.c` | cache 分配/释放/扩张/收缩逻辑 | cache 的「大脑」：含自适应收缩策略 |
| `assign/cache_malloc/cache_recycle_seg.c` | cache 延迟回收段管理 | 处理「设备还在用」的 busy 回收 |
| `assign/malloc_mng/malloc_mng.c` | 总调度 + handle 红黑树 | **总台**：统一入口，索引所有已分配内存 |

> 说明：本讲的「分配器」只负责**虚拟地址（VA）的记账与切分**。真正把 VA 落地（mmap 到字符设备 `/dev/davinci_manager`）由 `va_reserve.c` 的 `svm_reserve_va` 完成；申请物理页由 `normal_malloc.c` 的 populate 阶段完成（详见 u4-l2、u4-l3）。本讲聚焦「VA 从哪切出来」。

## 4. 核心概念与源码讲解

### 4.1 malloc_mng：总调度与 handle 索引

#### 4.1.1 概念说明

`malloc_mng.c` 是整个分配子系统的**总台**。它做两件事：

1. **路由**：决定每次申请走 cache 快速通道还是 normal 普通通道。
2. **索引**：用一棵按地址区间排序的红黑树，登记每一次成功的申请（`handle`），这样后续用起始地址就能 O(log n) 查回这块内存的全部属性（属主设备、大小、对齐、来自 cache 与否等）。

`handle` 是 SVM 内部对「一段已分配内存」的抽象，相当于一张「内存户口卡」。它不带物理页信息（物理页由内核侧 manage），只记录 VA 维度的元数据。

#### 4.1.2 核心流程

一次 `svm_malloc` 的总流程：

```
svm_malloc(start, size, align, flag, location)
  │
  ├─ malloc_para_check()              // 校验 size/align/numa
  ├─ _svm_malloc()                    // 路由：cache 还是 normal？
  │     ├─ go_malloc_cache() ?        // 判断是否满足 cache 条件
  │     │     YES → malloc_cache()    //   走 cache 快速通道
  │     │     NO  → malloc_normal()   //   走 normal 通道（最终到 MGA）
  │     └─ 记录 is_from_cache
  ├─ handle_alloc() + handle_init()   // 建「户口卡」
  ├─ svm_mng_ops_post_malloc()        // 通知已注册的观察者（如 populate 回调）
  └─ handle_insert()                  // 插入红黑树，按 [start, start+size) 索引
```

`svm_free(start)` 则是逆操作：按 start 在红黑树里 erase 出 handle → uninit（可能触发私有资源释放）→ 通知 pre_free 观察者 → 真正释放底层内存（cache 或 normal）。

#### 4.1.3 源码精读

**handle 的结构**——一张「内存户口卡」：

[src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c:47-62](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L47-L62)

这段定义了 `handle_t`：`prop`（核心属性，含 devid/start/size/flag）、`ref`（引用计数，供并发查询用）、`is_from_cache`（释放时据此选 cache 还是 normal 通道）、`align`（释放时需要原对齐信息）、`priv`/`priv_ops`（可选的私有数据与操作表，供 VMM/IPC 等挂载扩展）。注意 `rbtree_node node` 让它本身就能挂进红黑树。

**路由判断**——要不要走 cache？

[src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c:394-399](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L394-L399)

`go_malloc_cache` 的条件是三者的「与」：不指定 NUMA、flag 允许缓存（非连续、非 gpage、非只读、非旁路 cache）、且 `svm_cache_is_support` 返回真（cache 已初始化、大小不超过阈值、对齐匹配页大小）。三者都满足才走快速通道，否则走 normal。

**真正的分发**：

[src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c:492-518](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L492-L518)

`_svm_malloc` 先算出页对齐后的 `aligned_size`，再按 `go_cache` 分流。注意第 513 行：成功后把 `is_from_cache` 回写，并**把 size 改写成对齐后的实际大小**——这正是释放时必须用 `prop.aligned_size` 而非原始 size 的原因。

**入口函数 `svm_malloc` 的善后**：

[src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c:607-661](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/malloc_mng/malloc_mng.c#L607-L661)

注意末尾的 `goto` 链：任何一步失败都要按相反顺序回滚（`ops_pre_free` → `handle_uninit` → `handle_free` → `_svm_free` 把已切的地址还回去）。这种「失败严格回滚」是分配器正确性的基石——绝不能让一段已切出但未登记的 VA 成为孤儿。

#### 4.1.4 代码实践

**实践目标**：理解 handle 红黑树如何用地址区间做索引。

**操作步骤**：
1. 打开 `malloc_mng.c`，找到 `rb_range_of_handle`（约第 107 行），看它如何把一个 handle 映射成 `[start, start+size-1]` 区间。
2. 阅读 `handle_get`（约第 191 行）与 `handle_erase`（约第 303 行）：它们都用 `rbtree_search_by_range` 按 VA 查找。
3. 对比 `svm_get_nearby_prop`（约第 857 行）：它用 `rbtree_search_upper_bound_range` 找出某 VA 的左邻右舍。

**需要观察的现象**：所有查找都以「区间」为键，而不是单纯的起始地址。这意味着两个 handle 的区间**绝不能重叠**（`_handle_insert` 用 `rbtree_insert_by_range`，重叠会返回 `DRV_ERROR_BUSY`）。

**预期结果**：你能解释「为什么 `svm_free` 只传一个起始地址就够」——因为 handle 的区间以 start 为左端点，且全局唯一。**待本地验证**：若想确认重叠插入被拒，可在 UT 里构造两个 start 相同的 handle 调用 `handle_insert`，预期第二个返回 `DRV_ERROR_BUSY`。

#### 4.1.5 小练习与答案

**练习 1**：`svm_free` 在底层 `_svm_free` 返回 `DRV_ERROR_BUSY` 时做了什么？为什么？

参考答案：它把 handle **重新插回红黑树**并返回 BUSY（见 `svm_free` 第 683-690 行）。这表示底层内存（通常是 cache 段）因设备仍在占用而暂时无法归还；handle 必须保留，等设备释放后再由延迟回收路径（如 `cache_recycle_seg`）处理，否则这段地址会丢失登记。

**练习 2**：`svm_recycle_mem_by_dev`（约第 722 行）和普通 `svm_free` 有何区别？

参考答案：它是**强制**按设备批量回收（`_svm_recycle_handle` 注释明说「No need check handle->ref, force recycle」），用在设备关闭、CRIU（检查点恢复）等需要清场的场景；普通 `svm_free` 则尊重 ref 引用计数与 busy 状态。

---

### 4.2 gen_allocator：通用区间分配器（地基）

#### 4.2.1 概念说明

`gen_allocator.c` 是整个分配子系统的**地基**。它实现一个「通用地址分配器」，特性见其头文件自述：

- 管理多段**不连续**的地址区间（range）。
- 可以动态地往里**加/删**一整段区间。
- 支持「按指定地址分配」和「按大小分配」两种模式。
- 已分配地址必须落在某个 range 内。

它**只做地址记账**，不知道什么叫 mmap、什么叫物理页。MGA 和 cache 都是在它之上「包了一层策略」。理解了它，就理解了本讲一半的代码。

#### 4.2.2 核心流程

`gen_allocator` 用两级结构管理地址：

```
ga_inst（分配器实例）
  │
  ├─ addr_range_tree   红黑树：key=range 地址区间，存所有 range
  │     │
  │     └─ ga_range（一段大区间，由 add_range 加入）
  │           │
  │           └─ addr_area_tree  红黑树：key=area 区间，存该 range 内的空闲块
  │                 ├─ ga_area（空闲块 A）
  │                 ├─ ga_area（空闲块 B）
  │                 └─ ...
  │
  └─ size_area_tree   多值红黑树：key=area 大小，跨所有 range 的空闲块
        （按大小排，方便「找一块 >= 需要大小」的空闲块）
```

- **range**：一次 `add_range` 加入的大段（例如 MGA 扩张时新预留的 256GB，或 cache 预申请的 2MB 段）。
- **area**：range 内部的空闲块。一个 range 初始就是**一个占满整个 range 的 area**；每次分配会把 area 切小（split），每次释放会把相邻 area 合并（merge）。

**分配（按大小）**：在 `size_area_tree` 里找「>= size」的最小块（`upper_bound`），从它的起始切出 size，剩余部分作为新 area 留下。

**释放**：把归还的区间建成新 area 插入，再尝试与左右邻居 area 合并。

**回收整段 idle range**：当一个 range 内所有 area 都空闲（`idle_area_size == size`），可以整段交还给上层（MGA 用它来做 shrink）。

#### 4.2.3 源码精读

**核心数据结构**：

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:19-56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L19-L56)

注意三个细节：① `ga_range` 里有 `idle_area_size`，记录「该 range 当前总空闲量」，用它就能 O(1) 判断 range 是否全空闲（`ga_is_idle_range`）；② 每个 area/range 都**反向指向所属 inst/range**，方便从节点快速回溯；③ `size_area_tree` 是 `multi_rbtree`（允许同 key 多个节点），因为可能有多个大小相同的空闲块。

**按大小找空闲块**：

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:163-178](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L163-L178)

先用 `multi_rbtree_get(size)` 精确匹配，没有就用 `multi_rbtree_get_upper_bound(size)` 找「最小的不小于 size 的块」。这就是「最佳适配（best-fit）」的 O(log n) 实现，能减少切出来的碎块。

**分配时的切分**：

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:219-253](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L219-L253)

`ga_try_slice_area` 把目标 area 擦除，然后按需在左侧、右侧各建一个新 area 表示剩余部分。若 area 恰好等于 size，则什么都不留（直接 `goto free_area`）。这就是「切蛋糕」。

**释放时的合并**：

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:255-295](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L255-L295)

`ga_try_merge_area` 通过「查 `start-1` 和 `start+size` 这两个地址是否落在某空闲 area 上」来找左右邻居（`ga_range_get_area`）。若找到就擦除邻居、合并大小，最后重新插入合并后的大 area。注意合并**只发生在同一个 range 内**——跨 range 的地址不连续，自然不会误合并。

**对外分配/释放入口**：

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:600-621](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L600-L621)

`svm_ga_alloc` 按 flag 分派：固定地址（`FIXED_ADDR`）走 `ga_alloc_by_fixed_addr`，否则按大小走 `ga_alloc_by_size`。整个过程用写锁（`pthread_rwlock_wrlock`）保护树结构。

[src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c:638-669](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/gen_allocator/gen_allocator.c#L638-L669)

`svm_ga_free` 先按地址定位 range，再建 area 并尝试合并。

#### 4.2.4 代码实践

**实践目标**：在脑中跑一遍「一个新 range 被连续切分、再释放合并」的过程。

**操作步骤**：
1. 假设调用 `svm_ga_add_range(inst, 0x1000, 0x10000)`（加入一段 64KB 的 range，起始 0x1000）。此时该 range 内有一个 area：`[0x1000, 0x1000+0x10000)`，`idle_area_size = 0x10000`。
2. 调用 `svm_ga_alloc(inst, 0, &addr, 0x4000)`（申请 16KB）。跟踪 `ga_get_area_by_size` → `ga_try_slice_area`：擦除原 area，因 `area_start(0x1000) < addr(0x1000)` 不成立（左侧无剩余），右侧剩余 `[0x5000, 0x11000)` 建为新 area。返回 `addr = 0x1000`。
3. 再 `svm_ga_alloc(inst, 0, &addr, 0x4000)`：从 size_tree 找到剩余 area（大小 0xC000），切出 0x4000，返回 `addr = 0x5000`，剩 `[0x9000, 0x11000)`。
4. `svm_ga_free(inst, 0x1000, 0x4000)`：建 area `[0x1000,0x5000)`，查左邻居（无）、右邻居 `[0x5000,0x9000)` —— 但右邻居此时是**已分配**的（不在 area_tree 里），所以不合并。
5. 再 `svm_ga_free(inst, 0x5000, 0x4000)`：建 area `[0x5000,0x9000)`，右邻居 `[0x9000,0x11000)` 空闲 → 合并成 `[0x5000,0x11000)`；左邻居 `[0x1000,0x5000)` 空闲 → 再合并成 `[0x1000,0x11000)`。

**需要观察的现象**：释放顺序不影响最终结果——只要所有块都归还，最终 range 会回到「一个完整大 area」状态（`idle_area_size == size`，即 idle range）。

**预期结果**：你能画出每一步 `addr_area_tree` 与 `size_area_tree` 的形态。**待本地验证**：可写一个 UT，断言经过上述 5 步后 `svm_ga_owner_range_is_idle(inst, 0x1000)` 返回 true。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `size_area_tree` 要用 `multi_rbtree`（多值红黑树）而不用普通红黑树？

参考答案：系统中可能同时存在多个**大小完全相同**的空闲 area（例如 cache 池里切出多块等大的 4KB）。普通红黑树 key 唯一，无法存多个同 key 节点；multi_rbtree 允许同 key 链表，才能把它们都登记在按大小排序的树里。

**练习 2**：`ga_try_merge_area` 为什么要先 `ga_range_get_area(range, start-1, 1)` 和 `ga_range_get_area(range, start+size, 1)`？

参考答案：这是在用「探测一个字节」的方式找左右邻居——查 `start-1` 那一字节落在哪个 area，就是左邻居；查 `start+size` 那一字节，就是右邻居。只查同一 range 内的 area_tree，天然保证只合并连续地址。

---

### 4.3 va_allocator 与 MGA：多对齐虚拟地址空间管理

#### 4.3.1 概念说明

MGA = **multi-alignment gen allocator**（多对齐通用分配器）。它解决一个现实问题：不同大小的内存申请需要不同的对齐粒度——

- 小对象（几 KB）按 4K 对齐即可；
- 大对象（几 MB）希望按 2M 对齐，减少页表项数量；
- 超大对象（几 GB）希望按 1G 对齐。

如果把所有申请混在一个 `gen_allocator` 里切，4K 对齐的小对象会**污染** 2M/1G 对齐的边界，导致大对象找不到对齐的连续块。MGA 的做法是：**按对齐粒度分池**——4K、64K、2M、1G 各开一个 `ga_inst`（称为「子池」），再加一个「基座池」（base，最大对齐粒度，通常 1G）统一向内核要地。

地址在池子间的流动遵循两条规则：

- **扩张**：某子池不够时，向基座池申请一段（按基座对齐），再加入该子池。
- **收缩**：某子池出现整段空闲时，把它还给基座池；基座池再通过 shrink 回调把地址还给内核（`svm_release_va`）。

这样大对象在 1G 池里总能拿到 1G 对齐的块，小对象在 4K 池里切，互不干扰。

#### 4.3.2 核心流程

MGA 的结构与一次分配的流转：

```
mga_inst
  ├─ ga_inst[4K]   ga_inst[64K]   ga_inst[2M]   ga_inst[1G]   ← 子池（4 个）
  └─ ga_inst[base]                                               ← 基座池（base_align_type，通常=1G）

mga_va_alloc(align, size, va)
  │
  ├─ align → align_type（4K/64K/2M/1G）
  ├─ 加写锁
  ├─ mga_alloc(): 先在子池 ga_inst[align_type] 里 svm_ga_alloc
  │     ├─ 成功 → 返回
  │     └─ 失败：
  │           ├─ 若就是基座池本身 → mga_shrink_all_sub_ga（挤其他子池的空闲段回基座）再试
  │           └─ 否则 → mga_expand_sub_ga_once（向基座要一段加入本子池）再试
  └─ 仍失败且 total_size < expand_thres → mga_expand（向内核 svm_reserve_va 要新地，加入基座）
```

释放 `mga_va_free` 是镜像：在对应子池 `svm_ga_free`，若总量超过 shrink 阈值，先把各子池空闲段收回基座，再把基座的一段 idle range 通过 shrink 回调还给内核。

#### 4.3.3 源码精读

**MGA 实例结构**：

[ src/ascend_hal/svm/v3/assign/va_allocator/mga.c:25-33](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L25-L33)

`ga_inst[MGA_ALIGN_TYPE_MAX]` 就是 4 个子池；`base_align_type` 记录基座是哪种对齐；`rwlock` 保护整个实例（注意：MGA 的常规分配**直接拿写锁**，见后文）。对齐类型枚举见第 17-23 行（4K/64K/2M/1G）。

**初始化：建 4 个子池 + 基座**：

[src/ascend_hal/svm/v3/assign/va_allocator/mga.c:73-102](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L73-L102)

`mga_inst_init` 给 4 种对齐各创建一个 `ga_inst`（第 89-99 行的循环），`gran_size` 就是各档对齐大小（见 `mga_align_type_to_size`，第 35-43 行）。`base_align_type` 由 `attr->max_align_size` 决定（第 83 行），通常就是 1G。

**分配主逻辑**：

[src/ascend_hal/svm/v3/assign/va_allocator/mga.c:271-295](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L271-L295)

`mga_alloc` 的三段式：① 先在目标子池直接 alloc；② 失败时，若目标就是基座池，则 `mga_shrink_all_sub_ga`（把所有子池的 idle range 收回基座）再试；③ 若是普通子池，则 `mga_expand_sub_ga_once`（向基座要一段，加入本子池）；若仍失败，先全量收缩再扩张一次。最后第 294 行再 alloc 一次。

**扩张子池**（向基座借地）：

[src/ascend_hal/svm/v3/assign/va_allocator/mga.c:191-209](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L191-L209)

`mga_expand_sub_ga_once` 先把请求大小按基座对齐向上取整（第 195-196 行），在基座池 `svm_ga_alloc` 出一段，再加入目标子池（`svm_ga_add_range`）。注意「按基座对齐取整」保证了切给子池的段在基座眼里是规整的一块，将来能干净地归还。

**带扩张阈值的对外入口**：

[src/ascend_hal/svm/v3/assign/va_allocator/mga.c:302-319](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L302-L319)

`_mga_va_alloc` 加写锁后调用 `mga_alloc`；若失败且 `total_size < expand_thres`，才触发 `mga_expand`——即**向内核申请新地**（`attr.expand` 回调）。`expand_thres` 是个上限：地址空间够大时不再无脑扩张，避免占太多虚拟地址。

**MGA 的装配者**——`va_dev_default_allocator.c` 决定 expand/shrink 回调具体做什么：

[src/ascend_hal/svm/v3/assign/va_allocator/va_dev_default_allocator.c:132-150](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_dev_default_allocator.c#L132-L150)

这里填的 `attr.expand = va_dev_default_dev_va_expand`（设备）或 `va_dev_default_host_va_expand`（Host），`attr.shrink = va_dev_default_va_shrink`。设备侧 `max_align_size = SVM_MGA_MAX_GRAN`（1G），Host 侧只有 2M。`expand_gran`（每次扩张粒度）= 预留大小（设备 256GB / Host 2TB）。

**扩张回调真正做的事——mmap**：

[src/ascend_hal/svm/v3/assign/va_allocator/va_reserve.c:738-765](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/va_reserve.c#L738-L765)

`svm_reserve_va` 最终走到 `svm_cmd_mmap`（见 va_reserve.c 第 412 行附近）把这段 VA mmap 到字符设备 `/dev/davinci_manager`。至此「地址记账」与「地址落地」就接上了：MGA 只管切地址，切到没有时就调 expand 回调，回调去做真正的 mmap。这正是 u4-l3 强调的「MGA 只做地址记账，mmap 由 expand 回调完成」。

#### 4.3.4 代码实践

**实践目标**：弄清「子池—基座池—内核」三级扩张关系。

**操作步骤**：
1. 在 `mga.c` 里找到 `mga_shrink_all_sub_ga`（第 220 行）与 `mga_expand_sub_ga_once`（第 191 行），分别描述它们在「子池↔基座」之间的地址流动方向。
2. 在 `va_dev_default_allocator.c` 里找到 `va_dev_default_mga_inst_create`，记录设备侧与 Host 侧的四个 attr 字段（`max_align_size`、`expand_gran`、`expand_thres`、`shrink_thres`）取值差异。
3. 跟踪 `va_dev_default_alloc`（第 182 行）→ `mga_va_alloc` → `_mga_va_alloc`，确认一条「申请 2MB（按 2M 对齐）内存」会落在 `ga_inst[MGA_ALIGN_TYPE_2M]` 子池。

**需要观察的现象**：设备侧预留 256GB，按 1G 基座对齐。第一次申请 2MB 时，2M 子池为空 → 向基座（1G）要一段 → 基座也为空 → `mga_expand` → `svm_reserve_va` → mmap 一整段（粒度 256GB 量级，但实际按需）。

**预期结果**：你能解释「为什么第一次大内存申请特别慢、后续快」——第一次要 mmap 落地，后续在已 mmap 的 range 内切地址是纯用户态红黑树操作。**待本地验证**：在有 NPU 的环境下，可用性能采样对比首次与后续同规格 `halMemAlloc` 的耗时。

#### 4.3.5 小练习与答案

**练习 1**：`mga_expand_sub_ga_once` 为什么要把 `size` 按 `base_align_type` 向上取整（`svm_align_up(size, align)`）？

参考答案：因为向基座池借的段，将来要能整段归还给基座池。基座池的粒度是 `base_align_type`（如 1G），只有借出的段本身是 1G 对齐的整数倍，归还时才能作为基座池里一个规整的空闲 range 重新登记，否则基座池会出现非对齐碎片。

**练习 2**：MGA 常规分配路径（`_mga_va_alloc`）为什么直接用**写锁**，而 `gen_allocator` 内部也用读写锁？

参考答案：因为 MGA 的分配可能触发「子池↔基座」的地址搬运（扩张/收缩），这会修改多个 ga_inst 的树结构，必须独占整个 MGA 实例。注释 `/* To ensure the expand is for cur thread */`（第 306 行）点明了这一点：扩张决策必须对当前线程生效，不能与其他线程的扩张并发。这是用「粗粒度写锁」换取「分配策略正确性」的取舍。

---

### 4.4 cache_malloc + cache_recycle_seg：小内存缓存池与延迟回收

#### 4.4.1 概念说明

经过 4.3 节，normal 路径已经能分配任意大小的 VA。但每次小分配都要：① 经 MGA 切地址；② 若触扩张还要 mmap；③ 进内核 populate 物理页。对高频的小对象（比如几 KB 的 tensor 元数据），这套开销太大。

`cache_malloc` 就是为此设计的 **VIP 快速通道**：它**预先**通过 normal 路径申请好一大段「已经切好地址、已经 populate 物理页」的内存（一个 cache 段），之后的小额申请直接在这段里用 `gen_allocator` 切地址——**纯用户态红黑树操作，不进内核**。这就像 4.2 节的「管家」预先屯好一批蛋糕，小额订单直接从库存切。

`cache_recycle_seg` 是 cache 的「善后小组」：当 cache 想把一段空闲内存还给底层（normal_free）时，若内核返回 BUSY（说明设备还在用这段物理页，暂时不能解除映射），这段地址会被登记到 recycle_seg 红黑树里，等设备用完后再异步释放。

#### 4.4.2 核心流程

```
svm_cache_malloc(devid, flag, align, va, size)
  │
  ├─ cache_get_allocator() → 取出对应 devid×flag 的 cache_allocator（内含一个 ga_inst）
  ├─ cache_malloc():
  │     ├─ _cache_malloc(): svm_ga_alloc 直接在池里切（快路径）
  │     └─ 若 OOM：cache_expand_once()
  │           ├─ cache_expand() → svm_normal_malloc()  ← 找 normal 要一大块（VA+物理页）
  │           ├─ svm_ga_add_range() 把新段加入 cache 的 ga_inst
  │           └─ 重试 _cache_malloc
  └─ 更新统计

svm_cache_free(devid, flag, align, va, size)
  │
  ├─ _cache_free(): svm_ga_free 把地址还给池（合并 area）
  ├─ cache_strategy_update(): 自适应调整收缩阈值
  └─ 若空闲过多（cache_should_shrink）：
        cache_shrink_once():
          ├─ svm_ga_recycle_one_idle_range() 取一段完全空闲的 range
          ├─ svm_normal_free() 还给底层
          │     └─ 返回 BUSY → cache_recycle_add_seg() 登记到延迟回收表
          └─ 继续直到空闲量回到阈值以下
```

#### 4.4.3 源码精读

**cache 分配器的全局表**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c:24-35](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c#L24-L35)

`g_ca[SVM_MAX_DEV_NUM][CACHE_TYPE_MAX]` 是一个二维全局表：每个设备、每种 cache 类型（普通页/大页/P2P/master UVA 等）各有一个 `cache_allocator`。`constructor` 在库加载时清零。`cache_get_allocator`（第 109-114 行）就是按 devid+flag 查这张表。

**cache 策略——决定何时扩张/收缩**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c:37-69](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c#L37-L69)

`cache_strategy_pack` 设定：Host 设备的 `alloc_thres`（单次允许走 cache 的最大尺寸）=16MB；设备侧普通页 16MB、大页 32MB；扩张粒度统一 2MB。超过 `alloc_thres` 的大申请不会走 cache（回到 4.1 的 `go_malloc_cache` 判断），而是直接走 normal。

**创建一个 cache 分配器实例**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c:71-100](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_allocator.c#L71-L100)

每个 cache_allocator 内部**只有一个 ga_inst**（第 87-93 行），粒度 = 页大小。注意它和 MGA 的区别：MGA 有 4 个 ga_inst（多对齐），cache 只有 1 个（单一对齐，因为小对象都按页大小对齐）。

**cache 分配的快路径与扩张循环**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c:133-142](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c#L133-L142)

`_cache_malloc` 就是 `svm_ga_alloc` + 统计更新。这就是「纯用户态切地址」的快路径。

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c:234-252](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c#L234-L252)

`cache_malloc` 的扩张循环：快路径 OOM 时，按 `expand_granularity`（2MB）对齐算出扩张大小，`cache_expand_once` 向 normal 要一段加入池，再重试。注意第 247 行注释「Might be alloced by other threads, should retry」——多线程并发时，扩张出来的段可能被别的线程抢走，所以要循环重试。

**扩张的底层——找 normal 要地**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c:156-181](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c#L156-L181)

`cache_malloc_raw` 调用 `svm_normal_malloc`。回顾 [src/ascend_hal/svm/v3/assign/normal_malloc/normal_malloc.c:127-150](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/normal_malloc/normal_malloc.c#L127-L150)：normal_malloc 在不带 `VA_ONLY`/`POPULATE_ONLY` 标志时，会同时做 `normal_va_alloc`（走 MGA 切 VA + mmap）和 `normal_mem_populate`（进内核申请物理页）。所以 cache 扩张得到的是**地址和物理页都齐全**的内存，之后在池内切地址就无需再碰内核。

**自适应收缩阈值**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c:56-67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c#L56-L67)

`cache_strategy_update` 在每次 free 后重算 `shrink_thres_cur`：取「2 倍当前已分配」与「（峰值+当前）/2」的较大者，且不低于默认值。这是典型的**自适应缓存策略**——业务高峰时峰值高、阈值随之抬高，缓存多留一些；低谷时阈值回落，触发收缩把内存还给系统。

**收缩与延迟回收**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c:212-232](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_malloc.c#L212-L232)

`cache_shrink_once` 用 `svm_ga_recycle_one_idle_range` 从池里取一段完全空闲的 range，调 `svm_normal_free` 还给底层。关键在第 226-228 行：若 normal_free 返回 `DRV_ERROR_BUSY`（设备仍在用这段物理页，不能立刻解除映射），就把这段地址登记到 `cache_recycle_add_seg`，视为「本次收缩成功但延迟释放」。这就是 cache 与 cache_recycle_seg 的协作点。

**cache_recycle_seg——延迟回收表**：

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_recycle_seg.c:41-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_recycle_seg.c#L41-L66)

`cache_recycle_add_seg` 把 {start, size, align, devid, flag} 存进一棵按地址区间排序的红黑树。它记录的是「物理上还不能释放、但逻辑上已空闲」的段。

[src/ascend_hal/svm/v3/assign/cache_malloc/cache_recycle_seg.c:97-110](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/cache_malloc/cache_recycle_seg.c#L97-L110)

`svm_cache_recycle_seg_release` 是对外释放入口：从表里删段、重新调 `svm_normal_free`。当设备侧确认不再占用（例如任务完成、解除引用）后，由上层路径调用它完成最终释放。`_cache_recycle_seg_clear_by_dev`（第 149 行）则在设备关闭时批量清场。

#### 4.4.4 代码实践

**实践目标**：把「cache 快速通道」与「MGA/normal 慢速通道」在脑中接成一张完整的图。

**操作步骤**：
1. 从 `malloc_mng.c` 的 `_svm_malloc` 出发，画两条线：
   - 「走 cache」线：`svm_cache_malloc` → `cache_malloc` → `_cache_malloc`（快）→ 池不够 → `cache_expand_once` → `svm_normal_malloc` → `mga_va_alloc` → `svm_reserve_va`（mmap）。
   - 「走 normal」线：`svm_normal_malloc` → `normal_va_alloc` → `svm_alloc_va` → `va_dev_default_alloc` → `mga_va_alloc` → `svm_reserve_va`（mmap）。
2. 标注哪一步进内核（mmap / populate），哪一步纯用户态。
3. 在 `cache_malloc.c` 的 `cache_free`（第 254 行）里找到 `cache_try_shrink` → `cache_shrink_once` → BUSY 分支，连到 `cache_recycle_seg.c` 的 `cache_recycle_add_seg`。

**需要观察的现象**：两条线最终都汇到 `mga_va_alloc` 与 `svm_reserve_va`——也就是说 **cache 不是 MGA 的替代品，而是架在 normal（MGA）之上的一层「切块缓存」**。cache 段本身就是用 normal 申请来的。

**预期结果**：你能回答「为什么 cache 能加速」——小申请在已 populate 的 cache 段内切地址（纯红黑树，无 syscall），把昂贵的 mmap+populate 摊销到一次大段扩张上。**待本地验证**：构造「连续 1000 次 4KB 申请」与「一次性 4MB 申请」对比耗时；前者应明显更快（命中 cache），后者走 normal。

#### 4.4.5 小练习与答案

**练习 1**：`svm_cache_is_support`（cache_malloc.c 第 278 行）的三个条件分别防什么？

参考答案：① `ca != NULL`：该 devid×flag 的 cache 尚未初始化则不能用；② `size <= alloc_thres`：太大的申请不进 cache（否则池子会被单次大申请耗尽，失去切块意义）；③ `align == alloc_gran`：对齐必须等于 cache 的页粒度（cache 是单一对齐池，不处理其它对齐）。

**练习 2**：cache 收缩时遇到 BUSY 为什么不报错，而是把段交给 recycle_seg？

参考答案：BUSY 表示设备侧仍在引用这段物理页（如 DMA 未完成、引用计数未归零）。此时强制解除映射会导致设备访问失败。把段登记到 recycle_seg、延迟到设备释放后再 free，是在「尽快归还内存」与「保证设备正确性」之间的安全折中——段逻辑上已空闲（可被 cache 忽略），物理释放推迟。

---

### 4.5 分配回收的高效设计与并发性能

#### 4.5.1 概念说明

前三节讲了「正确性」。本节讲「为什么快」，并把视角扩展到**高并发**场景——这正是本讲实践任务要求结合近期一次提交来理解的重点。

SVM v3 分配器的快，来自三个设计：

1. **纯用户态切地址**：cache 命中时，分配只是一次红黑树查找 + 切分（`svm_ga_alloc`），完全没有 syscall。
2. **摊销式扩张**：昂贵的 mmap + populate 被摊到「按 2MB / 256GB 粒度一次扩张」上，单位小申请的均摊成本极低。
3. **分层锁**：`gen_allocator` 内部用读写锁；MGA 用实例级写锁保证策略正确；cache 用各自分配器的锁，**不同 devid/flag 的 cache 互不干扰**，天然并行。

但要特别说明一个**容易混淆**的点：实践任务提到的提交 **「Remove the unnecessary heaplist rescan in svm VA alloc」（commit `e61bf4d`）改的是 v2（`src/ascend_hal/svm/v2/devmm/`）的 heaplist 分配器，不是本讲的 v3 mga/cache**。v2 用的是「heaplist（堆链表）」模型，与 v3 的「红黑树 + MGA」是两套实现。之所以在 v3 讲义里提到它，是因为它揭示的**并发优化思想**是通用的，理解它能帮助你看懂 v3 里类似的取舍。

#### 4.5.2 核心流程（提交 e61bf4d 的优化思路）

v2 的 heaplist 分配器原本在高并发下有个性能陷阱：

```
devmm_alloc_from_normal_heap（优化前）
  ├─ 拿读锁 → 扫描 heaplist 尝试分配
  ├─ 失败 → 升级为写锁
  └─ 在写锁下「重新扫描整个 heaplist」 ← 痛点：冗余且 O(堆数) 全扫
```

升级写锁后为什么要重扫？因为读锁期间可能有别的线程往 heaplist 加了新堆、或释放了内存，原本失败的位置现在也许能成功。但**大多数情况下什么也没变**，这次全堆重扫是纯浪费，堆很多时非常耗时。

提交 `e61bf4d` 的做法是给 `devmm_heap_list` 加一个 `version` 计数器：

- 任何会改变 heaplist 的操作（加堆、删堆、释放内存）都用 `__sync_fetch_and_add(&heap_list->version, 1)` 把版本号 +1。
- 分配线程在读锁期间记下 `version`；升级写锁后，比较当前 version：
  - 若 `cur_version == version`（锁升级期间没有任何改动）→ **跳过重扫**，直接分配新堆。
  - 若不等 → 才重扫 heaplist。

这样「绝大多数没变化的场景」就省掉了一次全堆扫描，高并发下「原堆分配失败、需要新堆」的路径性能大幅提升。

#### 4.5.3 源码精读

**v2：读锁记版本、写锁按版本决定是否重扫**：

[src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c:1622-1651](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c#L1622-L1651)

`devmm_alloc_from_normal_heap` 在读锁期间取版本号 `version = __sync_fetch_and_add(&heap_list->version, 0)`（原子读，第 1637 行）；升级写锁后把它传给 `devmm_alloc_from_heaplist(..., version)`（第 1650 行）。

[src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c:1594-1620](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c#L1594-L1620)

`devmm_alloc_from_heaplist` 现在多了一个 `version` 参数：`if (unlikely(cur_version != version))` 才重扫 heaplist（`_devmm_alloc_from_heaplist`），否则跳过重扫，直接 `devmm_alloc_com_heap` 建新堆分配。这正是「版本号护航、跳过冗余扫描」的核心。

**v2：版本号在每次 heaplist 变更时自增**（举两处为例）：

[src/ascend_hal/svm/v2/devmm/devmm_virt_base_heap.c:210-216](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/devmm/devmm_virt_base_heap.c#L210-L216)

新堆加入链表后 `heap_list->heap_cnt++` 紧跟 `__sync_fetch_and_add(&heap_list->version, 1)`。

[src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c:1136-1143](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c#L1136-L1143)

`devmm_free_to_normal_heap` 释放成功后也自增 version（第 1142 行），并附注释说明「释放产生了空闲空间，分配新堆前需要重扫」。

**回到 v3：为什么 v3 的 MGA 不需要这个优化？**

[src/ascend_hal/svm/v3/assign/va_allocator/mga.c:302-319](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/assign/va_allocator/mga.c#L302-L319)

v3 的 `_mga_va_alloc` **直接拿写锁**（`pthread_rwlock_wrlock`），不存在「读锁升级写锁」这一步，因此也就没有「升级后要不要重扫」的问题。v3 选择了不同的并发取舍：用稍粗的写锁换取策略正确性，同时靠 **cache 层（4.4 节）承担高频小分配**——cache 的 ga_inst 操作粒度小、不同 devid/flag 互不干扰，从而把大部分并发压力从 MGA 卸载到 cache。换句话说，v3 是用「分层（cache 卸载）」解决并发，v2 那次提交是用「版本号跳过冗余扫描」解决并发，**思路不同但目标一致**：减少无谓的全量扫描。

#### 4.5.4 代码实践

**实践目标**：把 v2 提交的优化思想，迁移到对 v3 分配器性能特性的理解上。

**操作步骤**：
1. 用 `git show e61bf4d -- src/ascend_hal/svm/v2/devmm/devmm_virt_interface.c` 查看完整 diff，确认本次改动**只动 v2**，并定位 `version` 字段的引入。
2. 在 v3 侧用 `grep -rn "rwlock" src/ascend_hal/svm/v3/assign/` 统计各分配器分别用什么锁粒度（ga_inst 一把、mga_inst 一把、每个 cache_allocator 一把、malloc_mng 全局一把）。
3. 对比 v3 的 `_mga_va_alloc`（全程写锁）与 v2 的 `devmm_alloc_from_normal_heap`（读锁→写锁升级）两种并发模型，写下各自的优缺点。

**需要观察的现象**：v3 把锁按「设备/类型」天然分桶（`g_ca[devid][type]`、`g_dev_default_mga_inst[devid]`），不同设备的分配互不阻塞——这是 v3 并发性的主要来源，而非锁内部的巧妙升级。

**预期结果**：你能用一句话说清两者的区别——「v2 用版本号避免锁升级后的冗余 heaplist 重扫；v3 用 cache 分层把高频小分配的并发压力从写锁 MGA 上卸载走」。**待本地验证**：若可编译 v2，可构造多线程并发分配压测，对比该提交前后「分配新堆」路径的耗时下降幅度。

#### 4.5.5 小练习与答案

**练习 1**：提交 `e61bf4d` 为什么要用 `__sync_fetch_and_add` 而不是普通 `version++`？

参考答案：因为 version 被多个线程并发读写（分配线程读、变更线程写）。`__sync_fetch_and_add` 是原子操作，保证「读-改-写」不可分割，避免在读写锁释放前后出现「读到半新半旧值」的竞态。即使写操作本身已在写锁内，但**读 version 发生在读锁内**（可能与另一 CPU 上的写并发），所以读端也必须用原子读（`__sync_fetch_and_add(&v, 0)`）。

**练习 2**：如果 v3 未来也想给 MGA 加「读多写少」优化（读锁快路径 + 写锁扩张），需要解决什么问题？

参考答案：需要解决「读锁期间多个线程同时发现地址不够、都去扩张」的冗余扩张问题——这正是 v2 那次提交用 version 解决的同类问题（v2 注释提到的「avoid 冗余新堆」）。v3 目前的写锁方案天然避免了它，但代价是并发度低；若改读写锁，就要引入类似 version 的机制或「double-check」来防止并发扩张风暴。

---

## 5. 综合实践

**任务**：为「一次 4KB 设备内存申请」绘制一张完整的分配器协作流程图，并标注每一步发生在哪个文件、用哪把锁、是否进内核。

**要求覆盖的要点**：

1. 从 `halMemAlloc`（上层 Runtime 入口）出发，经 `svm_malloc`（malloc_mng.c）路由。
2. 判断走 cache（假设满足 `go_malloc_cache`），进入 `svm_cache_malloc` → `cache_malloc` → `_cache_malloc` → `svm_ga_alloc`（cache_allocator.c 的 ga_inst）。
3. 标出**快路径**（cache 池命中：纯用户态红黑树，无 syscall）。
4. 标出**慢路径**（cache 池不足：`cache_expand_once` → `svm_normal_malloc` → `normal_va_alloc` → `svm_alloc_va` → `va_dev_default_alloc` → `mga_va_alloc` → 可能 `mga_expand` → `svm_reserve_va` → `svm_cmd_mmap` 进内核；以及 `normal_mem_populate` 进内核申请物理页）。
5. 在释放路径上，画出 `svm_free` → `_svm_free` → `svm_cache_free` → `cache_free`；若触发收缩且 `svm_normal_free` 返回 BUSY，画出落到 `cache_recycle_add_seg` 的延迟回收分支。
6. 在图旁用表格列出涉及的锁：`mng.rwlock`（handle 树）、`ga_inst->rwlock`、`mga_inst->rwlock`、`ca->rwlock`、`g_rwlock`（recycle_seg 树），并说明各自保护的范围。

**进阶思考**（写进图的备注）：

- 这次申请命中的是 cache 的 ga_inst，**没有**经过 MGA；MGA 只在 cache 扩张（向 normal 要地）时才被间接使用。
- 若把申请大小改成 64MB（超过 `alloc_thres` 16MB），流程会在哪一步分叉？（答：`go_malloc_cache` 返回 false，直接走 normal → MGA。）

**交付物**：一张流程图（手绘或工具均可）+ 一张锁对照表。本实践为**源码阅读型实践**，无需运行；若需验证某条边，可在对应函数处加日志（仅本地调试，勿提交）观察调用顺序。**待本地验证**：在有 NPU 的环境可加日志实测调用链。

## 6. 本讲小结

- SVM v3 的分配子系统是**四层协作**：`malloc_mng`（总台，handle 红黑树索引 + cache/normal 路由）→ `cache_malloc`（小内存缓存池，纯用户态切地址）→ `normal_malloc`/`va_allocator`（普通通道，VA + 物理页）→ `gen_allocator`（地基，红黑树区间/空闲块管理）。
- `gen_allocator` 用「range（区间）+ area（空闲块）」两级结构，配「按地址」与「按大小」两棵红黑树，实现 O(log n) 的 best-fit 分配、split/merge、idle range 回收；它是 MGA 与 cache 共同复用的底层。
- **MGA**（multi-alignment）把地址按 4K/64K/2M/1G 分到 4 个子池 + 1 个基座池，子池不够向基座借、基座不够向内核要（`svm_reserve_va` 做 mmap）；它只做地址记账，落地的 mmap 由 expand 回调完成。
- **cache** 预先通过 normal 申请「地址 + 物理页」齐全的大段，之后小申请在段内用单个 ga_inst 切地址，免去每次 mmap + populate；收缩遇 BUSY 时由 **cache_recycle_seg** 延迟回收。
- 分配回收的高效来自「纯用户态切地址 + 摊销式扩张 + 分层分桶锁」；并发优化上，v3 用 cache 分层卸载高频小分配，而近期提交 `e61bf4d`（**作用于 v2 heaplist**）则用 version 计数器跳过锁升级后的冗余全堆重扫——两者思路不同但目标一致。
- handle 红黑树是跨 cache/normal 两条通道的**唯一权威索引**，按地址区间存放、禁止重叠；`svm_free` 仅凭起始地址即可定位，靠的就是这个区间唯一性。

## 7. 下一步学习建议

- **横向对比 v2 与 v3**：阅读 `src/ascend_hal/svm/v2/devmm/` 下的 `devmm_virt_interface.c`、`devmm_virt_base_heap.c`，对比 v2 的「heaplist + 树」与 v3 的「MGA + cache」两套分配模型的设计差异，理解为何 v3 引入分层 cache。
- **深入物理页管理**：本讲只讲了 VA 切分。物理页的申请/回收在内核侧 `mpl`（memory populate layer）完成，可跟踪 `svm_mpl_client_populate` → ioctl 的内核侧实现（sdk_driver 层），这属于单元 6 的范畴。
- **继续 SVM 余下主题**：本讲是 SVM 单元（u4）的收尾。建议接下来进入单元 5（DMC/DMS 设备维护），或回头重读 u4-l4（内存拷贝与共享），体会 cache 层对高频小拷贝的加速作用。
- **动手验证**：若条件允许，在 `svm_ga_alloc`、`mga_va_alloc`、`svm_cache_malloc` 三处各加一行 `svm_info` 日志，跑一个混合大小的分配 workload，观察 cache 命中率与 MGA 扩张频率，把本讲的流程图变成实测数据。
