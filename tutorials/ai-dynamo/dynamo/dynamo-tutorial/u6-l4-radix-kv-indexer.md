# 基数树与 KV 索引：找到缓存的 worker

## 1. 本讲目标

上一讲（u6-l3）我们讲清了 KV 事件如何从引擎经 ZMQ 一路流到路由器。本讲回答事件的「终点站」：**路由器把这些事件积累成什么数据结构，以及一笔请求来了之后，如何用这个数据结构算出"每个 worker 已经缓存了我的多少个前缀块"**。

学完本讲你应该能够：

1. 说出 `LocalBlockHash` 与 `ExternalSequenceBlockHash` 两种块标识的分工，以及为什么基数树用前者导航、用后者反查。
2. 手画一棵 `RadixTree` 在"两个 worker 存了相同前缀 + 不同后缀"之后的形态，并解释 `edge`、`full_edge_workers`、`worker_cutoffs` 三个字段各自记录了什么。
3. 手工推演一次 `find_matches` 的返回值，并写出对应的 Rust 单元测试验证。
4. 解释 `ConcurrentRadixTree` 与 `ThreadPoolIndexer` 如何用"读内联 + 写粘滞"替代大锁。
5. 说出 `cuckoo`（CKF）与 `approximate_lru` 各自解决什么"精确基数树解决不了"的问题，以及它们的适用场景。

## 2. 前置知识

### 2.1 基数树（Radix Tree）与路径压缩

普通 trie（前缀树）每个字符占一个节点；**基数树**把"只有一个孩子的连续链"合并成一个节点，节点上存一段**边（edge）**而不是单个字符。这就是"压缩"的含义。对于 KV 缓存索引，被压缩的"字符"是**块**：

```text
不压缩：block0 → block1 → block2 → block3     （4 个节点）
压缩后：[block0, block1, block2, block3]       （1 个节点，edge 长度 4）
```

路径压缩对 KV 场景特别划算：一条长 prompt 动辄上百个块，且大部分分支只在一个点分叉（不同请求的后缀不同、前缀相同）。

### 2.2 两种块哈希（承接 u4-l3）

u4-l3 讲过块哈希的数学。这里只需要记住结论：

- **`LocalBlockHash(u64)`**：单个块内容的哈希（块内 token + LoRA/多模态盐值）。同内容同盐 ⇒ 同 local hash，**与它在序列中的位置无关**。
- **`ExternalSequenceBlockHash(u64)`**：从序列头累计到当前块的**链式哈希**，满足递推：

\[ s_0 = h_0, \qquad s_i = \mathrm{hash}(\,s_{i-1} \,\|\, h_i\,) \]

所以相同前缀 ⇒ 相同的前缀链哈希（u4-l3 的核心推论），而**相同内容不同前缀 ⇒ 不同链哈希**。源码实现见 [lib/kv-router/src/protocols.rs:L199-L217](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/protocols.rs#L199-L217)：`compute_next_seq_hash` 把递推委托给 `dynamo_tokens::compute_next_sequence_hash`——这是 kv-router、kvbm-logical 共用的唯一链递推实现（u9 会再遇到它）。

一句话分工：**local hash 负责"往哪走"（树的导航键），external sequence hash 负责"我是谁"（块的身份与反查键）**。

### 2.3 一个引擎侧不变量

`lib/kv-router/AGENTS.md` 规定：**在同一个 indexer 哈希域内，引擎发布的 local 链与 external 链是一一对应且跨 worker 一致的**——同一条 local 块链必然映射到同一条 external 链，反之亦然。这是 `RadixTree` 敢用"别的 worker 写下的边"来导航的前提：worker 8 沿着 worker 7 写出的 edge 走，走到的地方对 worker 8 也是合法的。违反该不变量被视为生产者/协议错误，热路径上不做二次校验。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/kv-router/src/indexer/README.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/README.md) | indexer 全家桶的自带文档：四种块标识、各变体对比与选型指南，**本讲最好的配套读物** |
| `lib/kv-router/src/indexer/compressed_radix.rs` | `NodeState`：单个节点的"压缩边 + 覆盖状态"，被 RadixTree 与并发压缩树共享 |
| `lib/kv-router/src/indexer/radix_tree.rs` | `RadixTree`：单线程压缩基数树（本讲主角） |
| `lib/kv-router/src/indexer/concurrent_radix_tree.rs` | `ConcurrentRadixTree`：`Arc<RwLock>` 版并发树 |
| `lib/kv-router/src/indexer/thread_pool.rs` | `ThreadPoolIndexer`：N 个写线程 + 粘滞路由，包住并发树 |
| `lib/kv-router/src/indexer/kv_indexer.rs` | `KvIndexer`：用 tokio 通道把树包成异步服务 |
| `lib/kv-router/src/indexer/cuckoo/`（入口 `cuckoo.rs`） | CKF 布谷鸟过滤器族：跨数据中心中继用的近似索引 |
| `lib/kv-router/src/indexer/approximate_lru.rs` | 路由器侧的物理容量近似模型（"这个 worker 的显存快满了"） |
| `lib/tokens/src/radix.rs` | `PositionalRadixTree`：**名字像但用途完全不同**，属于 KVBM 逻辑层（见 4.5.4） |

另外两处只做"接口对位"的文件：`lib/kv-router/src/indexer/traits.rs`（`KvIndexerInterface` / `SyncIndexer` 两个 trait）和 `lib/llm/src/kv_router/route_lookup.rs`（路由侧调用 indexer 的地方，衔接 u6-l2）。

## 4. 核心概念与源码讲解

### 4.1 模块一：从事件到树——RadixTree 的节点结构

#### 4.1.1 概念说明

u6-l3 讲过，路由器订阅 `kv-events` 主题后收到的是规范化的 `RouterEvent`，数据体是三选一：`Stored`（存了若干块）、`Removed`（删了若干块）、`Cleared`（整个 worker/rank 清空）。indexer 的职责就是把这些事件**规约（fold）成一棵树**，这棵树回答的唯一问题是：

> "给定一段 local hash 序列，每个 worker 分别覆盖了它的前多少个块？"

#### 4.1.2 核心流程

```text
RouterEvent::Stored(worker, parent_hash, blocks)
   │
   ├─ parent_hash = Some(h)?
   │     └─ lookup[worker][h] 找到父节点 → 从父节点继续
   │        （找不到 ⇒ ParentBlockNotFound，事件被丢弃并告警）
   ├─ parent_hash = None ⇒ 从 root 开始
   │
   ├─ 沿 local hash 逐块下行：
   │     ├─ 无子节点 & 父是可扩展叶子 ⇒ append_blocks_to_leaf（边延长）
   │     ├─ 无子节点 & 不可扩展       ⇒ 新建子节点
   │     ├─ 部分匹配                   ⇒ split_node 把边劈开，再挂新尾巴
   │     └─ 完全匹配                   ⇒ 该 worker 升级为 full edge
   └─ 每写入一块，更新 lookup[worker][seq_hash] = 所在节点
```

#### 4.1.3 源码精读

先看两个结构体。[radix_tree.rs:L17-L23](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L17-L23) 定义节点外壳：

```rust
pub(crate) struct RadixBlock {
    state: NodeState,
    children: FxHashMap<LocalBlockHash, SharedRadixBlock>,
    /// Once a node has children it is never eligible for leaf extension again.
    internal: bool,
}
```

- `children` 以**首块的 local hash** 为键——这就是"local hash 负责导航"。
- `internal` 标记一旦有孩子就永远不能再被"叶扩展"（防止边无限膨胀成链）。

节点的真正内容在 [compressed_radix.rs:L64-L76](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L64-L76) 的 `NodeState`：

```rust
pub(crate) struct NodeState {
    edge: Vec<(LocalBlockHash, ExternalSequenceBlockHash)>, // 压缩边：一串块
    edge_index: FxHashMap<ExternalSequenceBlockHash, usize>, // 链哈希 → 边内位置
    worker_cutoffs: FxHashMap<WorkerWithDpRank, usize>,     // 部分覆盖：覆盖了 edge[0..k]
    full_edge_workers: FxHashSet<WorkerWithDpRank>,         // 完整覆盖整条边
}
```

理解 `worker_cutoffs` 是本讲的关键一步。一条边有 5 个块，worker A 全有、worker B 只有前 3 个：

```text
edge = [b0, b1, b2, b3, b4]
full_edge_workers = {A}
worker_cutoffs    = {B: 3}
```

判断某 worker 是否覆盖边内某位置，见 [compressed_radix.rs:L124-L127](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L124-L127) 的 `covers_pos`：要么在 full 集合，要么 `pos < cutoff`。

树本体 [radix_tree.rs:L49-L52](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L49-L52) 只有两个字段：

```rust
pub struct RadixTree {
    root: SharedRadixBlock,
    lookup: FxHashMap<WorkerWithDpRank, WorkerLookup>, // worker → 链哈希 → 节点
}
```

`lookup` 是**反查表**：`Removed` 事件只给链哈希不给位置，靠它一步跳到块所在的节点，再由 `edge_index` 找到边内偏移。这是"external hash 负责身份"的落地。

再看写入侧两个最有代表性的分支。[radix_tree.rs:L319-L351](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L319-L351)（`apply_stored` 的下行循环开头）处理"没有现成孩子"的两种情况：

```rust
let can_extend = { /* 父边非空 && 父不是 internal && 父覆盖自己的最后一块 */ };
if can_extend {
    parent.borrow_mut().state.append_blocks_to_leaf(worker, remaining);
    ...
} else {
    let child = Rc::new(RefCell::new(RadixBlock::for_blocks(remaining, worker)));
    ...
}
```

`append_blocks_to_leaf`（[compressed_radix.rs:L236-L262](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L236-L262)）里有一个容易忽略的细节：边延长后，**原来 full 覆盖的其他 worker 会被降级成 cutoff = 旧边长**——他们并没有新来的块。这正是 4.2 手工推演会看到的场景。

分叉时用 [radix_tree.rs:L470-L535](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L470-L535) 的 `split_node`：把边在某位置劈成前缀节点 + 后缀节点，后缀节点继承孩子；`cutoff >= pos` 的 worker 升级为后缀节点的 full worker，`cutoff < pos` 的留在前缀节点。最后逐 worker 修正 `lookup` 里指向的节点。

删除路径 [radix_tree.rs:L570-L654](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L570-L654) 的语义是"**截断而非摘除**"：`remove_worker_at_pos`（[compressed_radix.rs:L264-L288](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L264-L288)）把该 worker 的覆盖降到被删位置之前，返回"新暴露出来的失效哈希"供 `lookup` 清理；`clear_children_if_unreachable`（[radix_tree.rs:L42-L46](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L42-L46)）在节点对任何 worker 都不可达时清掉孩子。

#### 4.1.4 代码实践

**实践目标**：跑通本模块引用的现有单测，确认你能在本地编译并执行 kv-router crate。

**操作步骤**：

```bash
cargo test -p dynamo-kv-router linear_tree_dumps_as_one_event -- --nocapture
cargo test -p dynamo-kv-router rejects_self_referencing_store
```

**需要观察的现象**：第一条测试把 4 个块存成一个 worker，断言 `edge_lengths_for_test()` 返回 `vec![4]`——即整条序列被压缩成**一个节点**，且 `dump_tree_as_events()` 能还原成同一条事件（见 [radix_tree.rs:L854-L864](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L854-L864)）。第二条测试验证"自引用 store"（parent 是自己要存的块之一）会被拒绝。

**预期结果**：两条测试通过；`edge_lengths_for_test` 的断言直接证明路径压缩生效。

（若你的环境是 macOS，按仓库 AGENTS.md 的提示给 `dynamo-llm` 相关目标加 `--no-default-features`；`dynamo-kv-router` 本身不依赖 CUDA 特性。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `children` 用 `LocalBlockHash` 做键，而 `lookup` 用 `ExternalSequenceBlockHash` 做键？

**答案**：`children` 的职责是在下行时回答"下一步走哪个分支"，这只取决于下一块的内容，即 local hash；不同请求的同一前缀内容相同，local hash 相同，才能共享树结构。`lookup` 的职责是把 `Removed` 事件里的块身份翻译成树中位置，而块的全球唯一身份是链式哈希（同内容不同前缀是不同的块），所以必须用 external hash。

**练习 2**：如果一个节点 `full_edge_workers` 为空但 `worker_cutoffs` 非空，这个节点还该存在吗？

**答案**：该存在。cutoff worker 仍覆盖这条边的前缀，`has_any_workers()`（[compressed_radix.rs:L290-L292](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L290-L292)）对两个集合取"或"来判断节点是否还有价值。真正触发清理的是 `clear_children_if_unreachable`：`full_edge_workers` 为空时**清空孩子**——没有任何 worker 能完整走过这条边，后代不可达。

---

### 4.2 模块二：find_matches——一次重合度查询的完整推演

#### 4.2.1 概念说明

`find_matches` 是整棵树存在的意义：输入一笔请求的 local hash 序列，输出 `OverlapScores`（`FxHashMap<WorkerWithDpRank, u32>`，见 [protocols.rs:L1634-L1636](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/protocols.rs#L1634-L1636)），值是该 worker 覆盖的**块数**。u6-l2 讲过打分公式把这份重合度当"可抵扣的 prefill 成本"，所以这里返回的数字直接影响路由决策。

#### 4.2.2 核心流程

`find_match_details_with_options`（[radix_tree.rs:L97-L223](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L97-L223)）是一个"**活跃集单调收缩**"的扫描：

```text
active ← 第一个节点上覆盖该边的 worker 全集
循环（沿序列下行，逐节点消费边长）:
    edge_match_len = 本节点边与查询序列的可匹配长度
    对 active 中每个 worker:
        覆盖本节点全边          → 留在 active，分数暂时不写
        覆盖前 cutoff 块        → 掉队，分数 = matched_depth + min(cutoff, edge_match_len)
        本节点根本不覆盖         → 掉队，分数 = matched_depth（之前的深度）
    matched_depth += edge_match_len
    提前退出条件：active 空 / 边没走完 / 走完整条序列 / (early_exit 且只剩 1 个 worker)
最终：还留在 active 里的 worker，分数 = matched_depth
```

注意"掉队才写分、幸存者最后统一写分"的技巧：幸存者的分数就是最终深度，不必逐节点更新。

#### 4.2.3 源码精读

首个节点的处理在 [radix_tree.rs:L146-L161](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L146-L161)：

```rust
if first_node {
    active.clone_from(&node.state.full_edge_workers);
    for (&worker, &cutoff) in &node.state.worker_cutoffs {
        let contribution = cutoff.min(edge_match_len);
        if contribution == 0 { continue; }
        details.overlap_scores.scores.insert(worker, contribution as u32);
        ...
    }
    first_node = false;
}
```

后续节点的"收缩"分支在 [radix_tree.rs:L163-L191](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L163-L191)：`active.retain` 的闭包返回 `false` 即把该 worker 从活跃集移除，三种命运（全边覆盖 / cutoff 覆盖 / 不覆盖）分别对应练习里的三条打分路径。

现在做本讲最重要的**手工推演**，它就是综合实践的验收标准：

```text
事件 1：worker 7 存 [11, 12]        （make_store_event(7, &[11, 12])）
事件 2：worker 8 存 [11, 12, 13]    （make_store_event(8, &[11, 12, 13])）

树形态推演：
· 事件 1 → root 下新建节点 N，edge = [(11,s0),(12,s1)]，full = {7}
· 事件 2 → root.children[11] 命中 N，边完全匹配(2 块) → 8 升级为 full
           remaining = [13]，N 无 13 号孩子且 N 是可扩展叶子
           → append_blocks_to_leaf：edge = [(11,s0),(12,s1),(13,s2)]
           → 8 仍是 full；7 被降级 worker_cutoffs[7] = 2（旧边长）

最终：单节点，edge 长度 3，full_edge_workers = {8}，worker_cutoffs = {7: 2}

查询 [11, 12, 13, 14]：
· 首节点：active = {8}；cutoff worker 7：min(2, 3) = 2 → scores[7] = 2
· matched_depth = 3；sequence 还有第 4 块 14，但 N 没有 14 号孩子 → 终止
· 幸存者：scores[8] = 3

期望输出：{7: 2, 8: 3}
```

这个推演与仓库现有测试 [radix_tree.rs:L867-L902](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L867-L902) 的断言一致（该测试用同样的两个事件，从 `owner_prefix_blocks` 角度断言 `(7,2)` 与 `(8,3)`）——我们只是换了从 `overlap_scores` 角度验证。

另一个值得读的函数是 `dump_tree_as_events`（[radix_tree.rs:L718-L799](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L718-L799)）：BFS 遍历树，把每个节点还原成每个 worker 一条 `Stored` 事件（cutoff worker 只拿前缀块）。它是**快照/重放**的基础——测试 `compact_dump_preserves_order_and_replays`（[radix_tree.rs:L976-L1065](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L976-L1065)）证明了"dump 出的事件重放进新树得到同样的树"，且压缩 dump 比逐块 dump 的 JSON 更小。这份能力在路由器重启恢复时被复用（u6-l3 讲的 Resync 快照纪律）。

#### 4.2.4 代码实践

**实践目标**：不写代码，先用日志验证推演。

**操作步骤**：给上面的两条 store 事件加一层观察——在测试里调用 `tree.dump_tree_as_events()` 并打印。

```rust
// 示例代码（放在 radix_tree.rs 现有 #[cfg(test)] mod tests 里运行）
let mut tree = RadixTree::new();
tree.apply_event(crate::test_utils::make_store_event(7, &[11, 12])).unwrap();
tree.apply_event(crate::test_utils::make_store_event(8, &[11, 12, 13])).unwrap();
println!("{:#?}", tree.dump_tree_as_events());
assert_eq!(tree.edge_lengths_for_test(), vec![3]);   // 三个块压成一个节点
```

**需要观察的现象**：dump 出 2 条 `Stored` 事件——worker 8 一条含 3 块、worker 7 一条只含前 2 块（`blocks[..cutoff]`，见 [compressed_radix.rs:L29-L37](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/compressed_radix.rs#L29-L37) 的 `append_dump_events`）。

**预期结果**：`edge_lengths_for_test() == vec![3]` 证明压缩后单节点；dump 事件块数 3 + 2 证明 cutoff 语义正确。

#### 4.2.5 小练习与答案

**练习 1**：查询 `[11, 12, 13, 14, 15]`（比 4.2.3 多一块）时分数变吗？

**答案**：不变，仍是 `{7: 2, 8: 3}`。多出来的第 5 块只会让下行多失败一次（树里最深只到 13），`matched_depth` 封顶在 3。重合度度量的是"缓存里已有的前缀"，不是请求长度。

**练习 2**：`early_exit = true` 在什么场景下值得打开？

**答案**：当调用方只需要"找到一个够好的 worker"而不需要全体精确分数时。循环条件 `early_exit && active.len() == 1`（[radix_tree.rs:L196-L202](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L196-L202)）在只剩一个候选时立即停止深扫。u6-l2 提过的 `best_worker_id` 纯查询端点就是这类消费者；而需要给所有候选打分再交给调度器 softmax 的主路径必须传 `false`。

---

### 4.3 模块三：ConcurrentRadixTree 与 ThreadPoolIndexer——读内联、写粘滞

#### 4.3.1 概念说明

`RadixTree` 用 `Rc<RefCell<...>>`，**不是 `Send`**，只能活在单线程里。而真实路由器里：KV 事件（写）来自多个订阅任务，`find_matches`（读）来自每笔请求。直接加大锁会把读写互相堵死。

Dynamo 的解法分两层：

1. `ConcurrentRadixTree` 把节点换成 `Arc<RwLock<Block>>`，读之间真正并行；
2. `ThreadPoolIndexer` 再把**写**按 `(WorkerId, dp_rank)` 粘滞路由到固定的 N 个 OS 线程，让同一 worker 的写在同一线程内串行化，天然无写-写竞争。

#### 4.3.2 核心流程

```text
             ┌────────────────────────────────────┐
 find_matches ──→ 调用线程内联执行（只拿读锁）      │
             │      Arc<ConcurrentRadixTree>       │
 KV 事件 ──→ flume[0] → 线程 0（worker 0,3,…）──→ │
           ──→ flume[1] → 线程 1（worker 1,4,…）──→ │   写按 worker 粘滞
           ──→ flume[2] → 线程 2（worker 2,5,…）──→ │
             └────────────────────────────────────┘
```

死锁预防纪律：**总是先锁父再锁子**（hand-over-hand）。

#### 4.3.3 源码精读

模块头注释把设计说得非常直白，见 [concurrent_radix_tree.rs:L4-L22](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/concurrent_radix_tree.rs#L4-L22)。节点结构 [concurrent_radix_tree.rs:L46-L55](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/concurrent_radix_tree.rs#L46-L55)：

```rust
struct Block {
    children: FxHashMap<LocalBlockHash, SharedBlock>,
    workers: FxHashSet<WorkerWithDpRank>,      // 每个节点一个集合——没有压缩边
    block_hash: Option<ExternalSequenceBlockHash>,
}
```

注意与 `RadixTree` 的关键差异：**这里每个节点只代表一个块**（`block_hash` 是 `Option<单哈希>`），没有 `edge`/`cutoffs`。代价是节点更多、指针追逐更多；换来的是读写路径都极简单。README 的对比表给出结论：CRT 指针局部性差但逻辑简单，适合配线程池用；真正带压缩边的并发版本是 `ConcurrentRadixTreeCompressed`（在 `concurrent_radix_tree_compressed/` 目录，被 approximate_lru 的集成测试引用为 `ConcurrentRadixTreeCompressed::new()`）。

读路径 [concurrent_radix_tree.rs:L179-L275](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/concurrent_radix_tree.rs#L179-L275) `find_matches_impl` 的核心是一个**乐观假设 + 保守回退**：干净树里子节点的 worker 集合必然是父集合的子集（下行只减不增），所以 `child_count == active_count` 时可以跳过成员比对直接前进；只有数量不等才做 `reconcile_active_workers` 全量交集（[concurrent_radix_tree.rs:L220-L257](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/concurrent_radix_tree.rs#L220-L257) 的注释解释了为什么数量相等也可能瞬时不一致——`apply_removed` 不级联到后代，晚到的删除事件会造成暂时脏读，被接受为有界的路由质量退化）。

写路径的 hand-over-hand 在 [concurrent_radix_tree.rs:L341-L369](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/concurrent_radix_tree.rs#L341-L369)：每轮循环先锁父、把上一轮的 child 插入 worker、再看孩子，锁在块作用域结束时释放，避免同一节点锁两次。

线程池侧，[thread_pool.rs:L37](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/thread_pool.rs#L37) 的注释一句话点题："Spawns N OS threads for processing write events (sticky-routed by `(WorkerId, dp_rank)`)"，粘滞映射表在 [thread_pool.rs:L59](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/thread_pool.rs#L59)。

indexer 家族的选型指南在 [README.md:L354-L408](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/README.md#L354-L408)，浓缩成决策树：

```text
单 worker / worker 侧本地索引（LocalKvIndexer）        → RadixTree
< ~1000 worker，无需分片                                → ThreadPoolIndexer<ConcurrentRadixTree>（CRTC，默认）
≥ ~1000 worker 或需要单分片查询                          → BranchShardedIndexer<CRTC>（BSI，牺牲近似剪枝换分片正确性）
```

#### 4.3.4 代码实践

**实践目标**：验证并发树与单线程树在同样事件序列下给出同样的重合度答案。

**操作步骤**：

```bash
cargo test -p dynamo-kv-router --lib indexer
```

`indexer/tests.rs`（[tests.rs:L225-L227](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/tests.rs#L225-L227)）把 `RadixTree`、`ThreadPoolIndexer<ConcurrentRadixTree>` 等变体放进同一个测试矩阵，对同一组事件断言相同行为。

**需要观察的现象**：同一批测试在多个 indexer 变体上全部通过（测试矩阵通过参数化后端复用断言）。

**预期结果**：全部通过。这证明"并发化"没有改变索引的语义，只是换了执行模型。

#### 4.3.5 小练习与答案

**练习 1**：为什么写要按 worker 粘滞，而不是按块哈希取模分片？

**答案**：一个 worker 的所有写共享同一份 `lookup[worker]` 反查表，且它的子树写入往往集中在同几条前缀分支上。按 worker 粘滞保证这些状态只被一个线程触碰，免锁；按块哈希分片则同一 worker 的相邻块会落到不同线程，反而制造跨线程竞争和 `ParentBlockNotFound` 式的乱序风险（子块先于父块落账）。

**练习 2**：`ConcurrentRadixTree` 的文档承认它"不填充 legacy `OverlapScores.frequencies` 字段"。为什么可以留着空字段？

**答案**：`frequencies` 是旧线格式字段，为了兼容保留在结构体里但已无消费者；新代码只读 `scores`。这与 lib/llm AGENTS.md 的 N-2 兼容哲学一致： tolerated legacy field（容忍的旧字段）继续存在但不再是权威。

---

### 4.4 模块四：KvIndexer——把树包成异步服务

#### 4.4.1 概念说明

裸树不能直接给 tokio 异步世界用：`RadixTree` 的写需要 `&mut self`，而多个异步任务都持有它。`KvIndexer` 的做法是经典的 **actor 化**：树独占一个任务，外界通过一组 mpsc 通道投递请求，树的拥有者 `select!` 这些通道逐条处理。

#### 4.4.2 核心流程

```text
外部任务                        KvIndexer 内部任务（独占 RadixTree）
────────                        ──────────────────────────────
event_tx.send(RouterEvent)  ──→ mutation_rx ─┐
routing_tx.send(决策)       ──→ routing_rx   ├→ select! 循环 → trie.apply_event(...)
match_tx.send(序列)         ──→ match_rx     │→ trie.find_matches() → oneshot 回传
remove_worker_tx            ──→ ...          ┘
```

注意"两个方向的不对称"：**事件（写）fire-and-forget，查询（读）带 oneshot 应答**。

#### 4.4.3 源码精读

事件应用统一入口 [kv_indexer.rs:L24-L42](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L24-L42) `apply_event_with_counters`：调用树、按 `EventKind` 记指标，TRACE 级别时打印树规模——这是 u6-l3 讲的洪峰观测点之一。

u6-l2 讲过"路由决策写回 indexer"，落点就是 [kv_indexer.rs:L44-L101](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L44-L101) 的 `apply_routing_decision_with_prune_tracking`：把"请求被发往 worker X"翻译成一条合成的 `Stored` 事件写进树（**乐观预记**：假设引擎会为这笔请求生成这些块），同时刷新 TTL 剪枝簿记。L86-L89 的注释值得细读：TTL 是"自最近一次预测插入起算"，与 approximate-LRU 的引用计数是完全独立的两套生命周期。

写通道的消息枚举 [kv_indexer.rs:L136-L147](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L136-L147)：

```rust
enum MutationRequest {
    Event(RouterEvent),
    EventWithAck { event: RouterEvent, resp: oneshot::Sender<bool> },
    ResetWorkerDpRank { worker_id, dp_rank, resp },
}
```

`EventWithAck` 对应公开方法 `apply_event_and_wait`（[kv_indexer.rs:L842-L860](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L842-L860)）——测试和恢复逻辑用它确认事件真正落账后才继续。

读侧的 trait 实现 `find_matches`（[kv_indexer.rs:L697-L732](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L697-L732)）就是"发 MatchRequest + 等 oneshot"；更高层的 `find_matches_for_request`（[kv_indexer.rs:L734-L758](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L734-L758)）接收**原始 token**，自己调 `compute_block_hash_for_seq` 算块哈希再查——这衔接了 u4-l3 的分词/块化链路：路由侧只需 token 就能查树。

而 lib/llm 侧的真正调用点在 [route_lookup.rs:L124-L135](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/route_lookup.rs#L124-L135)：`query_retained` 调 `indexer.find_matches_by_tier_ref_with_options`，并且带 `kv_router.find_matches` tracing span、与 shared cache 查询 `join!` 并行——这就是 u6-l2 观测三件套里 indexer 耗时指标的来源（`metrics.rs` 里的 `indexer_find_matches` 直方图）。

对外 trait 由 [traits.rs:L67](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/traits.rs#L67) 的 `KvIndexerInterface`（异步、面向 actor 后端）与 [traits.rs:L209](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/traits.rs#L209) 的 `SyncIndexer`（同步、面向线程池后端）双轨定义，路由器持有一个统一的 `Indexer` 枚举择一而用。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——把"一笔请求触发一次 find_matches"的完整调用链画出来。

**操作步骤**：

1. 从 [route_lookup.rs:L124-L135](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/route_lookup.rs#L124-L135) 出发，`find_matches_by_tier_ref_with_options` 是 `Indexer` 枚举上的方法，找到它分派到 `KvIndexer::find_matches` 的分支。
2. 顺着 `MatchRequest` 的 oneshot 应答，进入 `kv_indexer.rs` 的 `select!` 循环，找到最终调用 `trie.find_match_details_with_options` 的那一行。
3. 把途经的每一跳（函数名 + 文件）记成清单。

**需要观察的现象**：链路应穿越"lib/llm 路由层 → 通道 → lib/kv-router actor → RadixTree"四个边界。

**预期结果**：得到一条 5–7 跳的调用链清单；对照 u6-l2 的"[ROUTING_INPUT] → Formula → [ROUTING] Best"日志序列，确认 indexer 查询发生在打分之前。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `apply_event` 是 fire-and-forget 而 `find_matches` 要等应答？

**答案**：事件是**事实**的追加，顺序重要但单条不阻塞决策——丢了或晚了只会让索引暂时陈旧（fail-open，u6-l3 讲过普通事件正是这个语义）。而查询的返回值是路由决策的直接输入，必须拿到本次树状态下的真实分数才能选 worker，所以必须同步等待 oneshot。

**练习 2**：`process_routing_decision_with_hashes` 在配置了 approximate-LRU 时会报错（[kv_indexer.rs:L869-L873](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/kv_indexer.rs#L869-L873)）。为什么？

**答案**：approximate-LRU 模式下，块的生命周期由请求租约（acquire/materialize/release）驱动而非"路由决策即落账"，两条记账路径混用会双计。所以该模式强制走"被接纳请求的 attempt"来登记块，见下一节。

---

### 4.5 模块五：两条"近似"路线——cuckoo 与 approximate_lru

#### 4.5.1 概念说明

精确基数树回答"**谁有这些块**"，但有两个它不回答的问题：

1. **跨数据中心/跨中继的聚合索引太贵**：每个 DC 的每台机器都往一张全局树里写事件，事件量爆炸。CKF（cuckoo filter）用固定容量的近似成员过滤器替代精确树，接受假阳性换带宽与内存。
2. **worker 的物理容量**：树只知道"逻辑上有哪些块"，不知道"这个 worker 显存只装得下 1000 个块，快满了"。`approximate_lru` 在路由器侧维护一个容量模型，把"哪些块该被当成已淘汰"近似出来并合成 `Removed` 事件喂给树。

#### 4.5.2 核心流程

**CKF（cuckoo/）**：D=16 条 lane 的转置布谷鸟表。插入一个块 → 经确定性寻址（seed）定位候选桶 → 桶满则"踢"出旧条目挪窝（最多 `max_kicks` 次，超限整体回滚）。前缀查询用"试探深度 + 指数/二分回扫 + 线性验证窗"的有界搜索（`verification_window` 默认 2）。查询是**advisory**的：容量压力造成的空洞可能产生非单调前缀证据，设计上明确不修复、不重试。

**approximate_lru**：每 worker 一份 `RankLruState`（引用计数的块副本表）。请求到达 → `Acquire`（前缀命中复用副本、失配后新建）→ 流式输出期间 `Materialize`（补记输出块）→ 请求结束 `Release`（引用清零的副本进入 inactive，按 `release_epoch` 与位置逆序排队）→ `reconcile`（超容量时按序驱逐，并合成 `Removed` 事件写回基数树）。

#### 4.5.3 源码精读

CKF 的常量与配置在 [cuckoo.rs:L45-L55](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/cuckoo.rs#L45-L55)（`CKF_LANE_COUNT = 16`、`MAX_KICKS = 4096`、默认 `max_kicks = 500`）与 [cuckoo.rs:L79-L112](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/cuckoo.rs#L79-L112) 的 `CkfConfig`。桶数公式在 [cuckoo.rs:L160-L169](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/cuckoo.rs#L160-L169)：`buckets = next_pow2(max(5N+15)/16, 2)`——即每桶 16 个槽、约 5/16 ≈ 31% 的目标负载因子，给踢挪留余量。它的消费者不在路由主路径，而在 `lib/llm/src/kv_dc_relay/`（跨 DC 中继，`pool_registry.rs` 用 `CkfConfig::new(expected_unique_blocks)` 构造）。

approximate_lru 的模块头注释（[approximate_lru.rs:L4-L18](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L4-L18)）明确它是"Router-owned physical-capacity model for local approximate indexing"，刻意不依赖实验性的 `aisimulate-core`。对外命令面 [approximate_lru.rs:L107-L138](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L107-L138)：`SetCapacity / ResetRank / Acquire / Materialize / Release / Stats`。

驱逐排序键 [approximate_lru.rs:L436-L441](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L436-L441)：

```rust
struct InactiveKey {
    release_epoch: u64,          // 谁更早被释放谁先走（LRU 的"时间"轴）
    reverse_position: Reverse<usize>, // 同批内：位置越靠后（后缀）越先走
    copy_id: BlockCopyId,
}
```

这个二元组就是"**先淘汰后缀、再淘汰前缀**"的物理直觉的精确表达：后缀块几乎没有再命中价值，前缀块是共享资产。`reconcile`（[approximate_lru.rs:L621-L647](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L621-L647)）只在 `resident_blocks() > capacity` 时从 `inactive` 的 BTreeSet 头部弹出驱逐，且只有某哈希的**最后一个副本**被驱逐时才产出 `Removed` 事件（去重语义，对应测试 `duplicate_physical_hash_is_removed_from_radix_only_after_final_copy`）。

`Acquire` 的前缀复用逻辑 [approximate_lru.rs:L512-L537](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L512-L537)：沿块序列前进，命中已有副本则引用 +1，首次失配后 `prefix_hit = false`，其后全部新建副本——与基数树的前缀匹配语义一一对应。没配置容量的 worker 会落到 `TtlFallback`（纯 TTL 剪枝，[approximate_lru.rs:L875-L891](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L875-L891)），测试 `missing_capacity_pins_rank_to_ttl_until_reset` 固定了该行为。

#### 4.5.4 一个重要的"避坑"说明：lib/tokens 的 PositionalRadixTree 不是本讲这棵树

`lib/tokens/src/radix.rs` 里也有一个 `PositionalRadixTree`（[radix.rs:L12-L19](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/tokens/src/radix.rs#L12-L19)），但它是一个 **按位置分桶的稀疏 map**（`DashMap<u64 /*position*/, FxHashMap<K, V>>`），服务于 u4-l3 讲过的 `PositionalLineageHash` 场景，使用者是 **kvbm-logical 的块注册表**（`lib/kvbm-logical/src/registry/mod.rs`，u9-l3 的领地）。它与 kv-router 的路径压缩基数树只是共享了"radix"这个词。搜索源码时看到两个 radix 不要混淆。

#### 4.5.5 代码实践

**实践目标**：用现成单测验证"先淘汰后缀再淘汰前缀"的驱逐顺序。

**操作步骤**：

```bash
cargo test -p dynamo-kv-router equal_release_epoch_evicts_suffix_before_prefix
cargo test -p dynamo-kv-router vllm_prefix_cache_lifecycle
```

**需要观察的现象**：第一条测试在容量从 3 收缩到 2 时，被驱逐的是块 3（最靠后的块）。第二条测试完整重演了 vLLM 前缀缓存文档的五步生命周期（共享前缀 → 输出补齐 → 第二请求部分命中 → 释放 → 容量压力驱逐），见 [approximate_lru.rs:L1216-L1364](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/approximate_lru.rs#L1216-L1364) 的逐步注释与断言。

**预期结果**：两条通过。第二条里断言的驱逐顺序 `[13, 12, 20, 11]` 值得对照 4.5.3 的 `InactiveKey` 手工解释一遍。

#### 4.5.6 小练习与答案

**练习 1**：CKF 与基数树分别适合"谁有块"这个问题的哪个尺度？

**答案**：基数树适合**单路由域内**（一个集群的 worker 集合）的精确回答，是打分的输入；CKF 适合**跨 DC 中继**的聚合概要——带宽和内存是硬约束，允许假阳性（多查一次远端）但不允许精确同步的成本。一个是决策依据，一个是可达性草图。

**练习 2**：approximate_lru 驱逐块后为什么要合成 `Removed` 事件而不是直接改树？

**答案**：因为 approximate_lru 是容量**模型**，基数树才是索引**本体**，两者解耦（模块头 L10-L14 的 NOTE 明确了这个分层）。合成事件让所有变更走同一条 `apply_event` 入口，保持单一事实来源与统一指标/告警；代价是淘汰是"最终一致"的——树可能短暂高估重合度，这正是"approximate"的含义。

## 5. 综合实践

把本讲全部内容串成一个可运行的验收测试（本讲必做的核心实践）。

**实践目标**：写一个 Rust 单元测试，向 `RadixTree` 插入两个 worker 的相同前缀与不同后缀，验证 `find_matches` 返回的重合块数与 4.2.3 的手工计算一致。

**操作步骤**：

1. 在你自己的克隆里编辑 `lib/kv-router/src/indexer/radix_tree.rs`，滚到文件底部的 `#[cfg(test)] mod tests`（[radix_tree.rs:L826-L834](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/indexer/radix_tree.rs#L826-L834) 已 `use crate::test_utils::{create_remove_event, create_store_event, make_store_event, snapshot_events};`，直接可用）。
2. 加入下面这个测试（**示例代码**，为讲义新写，风格对齐现有测试）：

```rust
#[test]
fn two_workers_same_prefix_different_suffix_score_by_depth() {
    let mut tree = RadixTree::new();
    // worker 7 缓存前缀 [11, 12]；worker 8 缓存 [11, 12, 13]（同前缀 + 更长后缀）
    tree.apply_event(make_store_event(7, &[11, 12])).unwrap();
    tree.apply_event(make_store_event(8, &[11, 12, 13])).unwrap();

    // 一笔新请求 [11, 12, 13, 14]：前缀与两者都重合，第 4 块谁都没有
    let scores = tree.find_matches(
        vec![LocalBlockHash(11), LocalBlockHash(12), LocalBlockHash(13), LocalBlockHash(14)],
        false,
    );

    assert_eq!(scores.scores.get(&WorkerWithDpRank::new(7, 0)), Some(&2));
    assert_eq!(scores.scores.get(&WorkerWithDpRank::new(8, 0)), Some(&3));
    assert_eq!(scores.scores.len(), 2);
    // 路径压缩：三个块压成一个节点
    assert_eq!(tree.edge_lengths_for_test(), vec![3]);
}
```

3. 运行：

```bash
cargo test -p dynamo-kv-router two_workers_same_prefix_different_suffix
```

**需要观察的现象 / 预期结果**：测试通过。逐条对照手工计算：

| 断言 | 手工依据（4.2.3） |
|------|--------------------|
| `7 → 2` | worker 7 被 `append_blocks_to_leaf` 降级为 `worker_cutoffs[7] = 2`，首节点打分 `min(2, 3) = 2` |
| `8 → 3` | worker 8 是 `full_edge_workers`，幸存到 `matched_depth = 3` |
| `edge_lengths == [3]` | 两次 store 经"完全匹配 + 叶扩展"合并为单节点 |

**进阶（可选）**：把查询换成 `[11, 99]`（第二块就分叉），预期 `{7: 1, 8: 1}`——两者都在第二块掉队，得分为 1；再插入 `make_store_event_with_parent` 构造真正的分叉树，验证 `split_node` 路径。若想在 crate 外做，`RadixTree` 与 `test_utils` 均为公开导出（`lib.rs` 的 `pub mod indexer` / `pub mod test_utils`），可在 `lib/kv-router/tests/` 下写集成测试达到同样效果。

## 6. 本讲小结

- **两种哈希两种职责**：`LocalBlockHash` 做树的导航键（同内容同哈希才能共享前缀），`ExternalSequenceBlockHash` 做块身份与反查键（链式递推 \( s_i = \mathrm{hash}(s_{i-1}\|h_i) \)）；引擎保证两链在同域内一一对应，热路径因此免校验。
- **压缩边 + 覆盖状态** 是 `NodeState` 的全部：`edge` 存一串块，`full_edge_workers` 存整边覆盖者，`worker_cutoffs` 存前缀覆盖者；叶扩展会把旧的 full worker 降级为 cutoff，这正是多 worker 共享前缀时的树形态。
- **`find_matches` 是活跃集单调收缩扫描**：掉队即写分、幸存者按最终深度统一写分；`early_exit` 只服务"找一个就够"的查询方。
- **并发方案是"读内联 + 写粘滞"**：`ConcurrentRadixTree` 乐观假设子集关系、数量相等跳过比对；`ThreadPoolIndexer` 按 `(WorkerId, dp_rank)` 把写钉在固定线程，配合 hand-over-hand 锁序防死锁。选型上 CRTC 是默认，超大规模用 BSI 换分片正确性。
- **`KvIndexer` 是 actor 化封装**：事件 fire-and-forget、查询带 oneshot 应答、路由决策可乐观预记成合成 `Stored` 事件（approximate-LRU 模式下禁止）。
- **两条近似路线各管一段**：CKF 用 16-lane 布谷鸟过滤器做跨 DC 的 advisory 可达性草图；approximate_lru 用引用计数 + `release_epoch`/位置逆序的驱逐键在路由器侧近似物理容量，并合成 `Removed` 事件维持"树是唯一事实来源"。注意 `lib/tokens::PositionalRadixTree` 属于 KVBM，与本讲的树只是同名。

## 7. 下一步学习建议

下一讲 **u6-l5（filter–score–pick 策略框架与自定义路由插件）** 会把本讲的 `OverlapScores` 接到策略框架的"score"段：重合度如何被折算成成本信用、又如何与负载、显存余量合成最终得分。建议先读 `lib/kv-router/src/services/selection/core/mod.rs`，并带着一个问题去：**"本讲返回的 `MatchDetails.last_matched_hashes`（最后一个命中块的链哈希）会在哪个环节被用来做会话亲和或续写定位？"**

若想先横向巩固，推荐两条支线：

1. `lib/kv-router/src/indexer/lower_tier.rs` 与 `lower_tier_indexers.rs`——本讲多处出现的 `find_matches_by_tier_*` 的"分层"部分：GPU/CPU/磁盘各层缓存如何叠进同一次查询（呼应 u6-l2 的层加权 1.0/0.75/0.25）。
2. `lib/kv-router/src/indexer/branch_sharded.rs`——BSI 分片树的锚点机制，看它如何解决"续写落在分片 A、查询落在分片 B"的浅前缀失效问题。

u9（KVBM）则会遇到链式哈希的另一端消费者：`kvbm-logical` 用同一个 `PositionalRadixTree` 与 `SequenceHash` 管理块注册表——届时回看本讲 4.5.4 的避坑说明会更清晰。
