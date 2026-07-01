# 跳表操作与 epoch 集成

## 1. 本讲目标

本讲承接 u7-l1（无锁跳表结构），从「节点积木」进入「在积木之上并发地增删改查」。

读完本讲你应该能够：

- 读懂 `search_bound` 与 `search_position` 的逐层下降算法，并解释它在遍历途中如何「顺手摘除」已逻辑删除的节点（help_unlink）。
- 说清一次并发 `insert` 的两阶段流程：先在 level 0 CAS 落点、再逐层补建塔，以及引用计数如何对应「每个存在的层级 + 每个 Entry 句柄」。
- 说清一次并发 `remove` 的「先标记、后摘除、最后收尾」三步走，以及为何 `mark_tower` 必须自顶向下、并以 level 0 的 tag 裁决唯一赢家。
- 指出 `guard.defer_unchecked` 在哪些点释放节点、为什么必须延迟到 epoch 宽限期之后，并理解 `SkipMap`/`SkipSet` 这两层包装如何把裸的 `base::SkipList` 变成对用户友好的接口。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自 u7-l1 与 u5 单元）：

- **跳表节点布局**（u7-l1）：`Node` 用 `#[repr(C)]` 变长尾数组，单字 `refs_and_height` 低 5 位存 `(height-1)`、高位存引用计数；`Tower` 是 ZST 占位，按 height 动态追加 `Atomic<Node>` 指针。读 `&(*node).tower` 走 `TowerRef`/`NodeRef` 以保留 provenance。
- **逻辑删除标记**（u7-l1）：删除一个节点 = 把它每一层指针的低位 tag 置 1（`fetch_or(1)`）。被标记的节点对后续读者「逻辑上不存在」，但物理指针仍在链里，需要被「摘除（unlink）」。
- **epoch 内存回收**（u5 单元）：无锁结构里，删除方不能立即 free 节点——别的线程可能正拿着它的指针在遍历。crossbeam-epoch 用 `pin()` 取得 `Guard`，用 `guard.defer_unchecked(closure)` 把销毁闭包推迟到「两个 epoch 之后」无人能再引用时才执行。`Shared<'g, T>` 的生命周期 `'g` 绑定 `Guard`。
- **指针低位标签**（u5-l2）：`Atomic<T>` 是单机器字 `AtomicPtr`，对齐指针低位恒为 0，可塞若干 bit 当 tag；`load_consume` / `compare_exchange` 整字比较，故 CAS 能同时换指针与 tag。

**一个直觉**：跳表的「无锁」并不意味着没有冲突，而是冲突永远能被「再扫一次」解决——任何线程在遍历时看到半删除的节点，都顺手帮忙把它从链里摘掉，而不是等删除方。这种「协作式清理」是本讲反复出现的主题。

跳表的期望查找代价是 \(O(\log n)\)：节点高度服从 \(p=\tfrac{1}{2}\) 的几何分布，期望层数约为 \(\log_2 n\)，因此一次逐层下降平均只比较 \(\log_2 n + O(1)\) 次。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`crossbeam-skiplist/src/base.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) | 跳表内核 `SkipList<K,V,C>`，本讲主角。导航、insert、remove、引用计数、epoch 回收都在这里。 |
| [`crossbeam-skiplist/src/map.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) | `SkipMap<K,V>` 包装：每个方法自己 `epoch::pin()` 再转调 `base`，返回给用户 `Entry`（引用计数句柄）。 |
| [`crossbeam-skiplist/src/set.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs) | `SkipSet<T>`：一层薄包装，本质是 `SkipMap<T, ()>`。 |
| [`crossbeam-skiplist/src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) | crate 文档，明确解释了「为什么需要 epoch 回收」「并发语义」「无 `get_mut`」等设计立场。 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：导航（4.1）、插入（4.2）、删除（4.3）、回收与包装（4.4）。

### 4.1 导航核心：逐层下降与协作摘除

#### 4.1.1 概念说明

跳表所有读路径（`get`、`front`、`back`、迭代）与写路径（`insert`、`remove`）都共享同一个底层动作：**从最高层往第 0 层下降，在每一层向右走到合适的位置**。这对应两条孪生函数：

- `search_bound`：返回满足某个 bound 条件的单个节点（用于读）。
- `search_position`：返回每一层的「前驱 `left[]` / 后继 `right[]`」数组，外加是否 `found`（用于 insert/remove 落点）。

难点不在下降本身，而在**并发删除**。当你正在第 `level` 层向右走，当前节点 `curr` 的后继指针 `succ` 可能在你两次 load 之间被别的线程打上了删除标记（`tag == 1`）。这时你不能假装没看见——一个标记节点留在链里会让导航出错。处理方式是：**任何看到标记节点的线程都顺手帮删除方把它摘掉**（help_unlink），失败了就重新从最高层开始扫。这是无锁算法经典的「help + restart」模式。

#### 4.1.2 核心流程

`search_bound` 的主循环（`'search: loop`）流程如下：

1. 从 `hot_data.max_height`（只增不减的查找起点提示，u7-l1 讲过）读出起始层。
2. 快速跳过 head 上为空的空层。
3. **逐层下降**：在每一层，从 `pred` 向右走，`curr = pred.level[l].load_consume()`：
   - 若 `curr.tag() == 1`，说明 `pred` 本身已被删除 → `continue 'search`（整体重来）。
   - 否则进入该层的向右循环：读 `succ = curr.level[l].load_consume()`：
     - 若 `succ.tag() == 1`，说明 `curr` 已被逻辑删除 → 调 `help_unlink` 试着把 `curr` 从这层摘掉；成功就继续走，失败就整体重来。
     - 否则按 bound 判定：找到第一个满足条件的节点就记为 `result` 并退出本层。
4. 走完所有层，返回 `result`。

用伪代码概括：

```
loop:  # 'search
    level = max_height (跳过空层)
    pred  = head
    result = None
    while level >= 1:
        level -= 1
        curr = pred[l].load_consume()
        if curr.tag == 1: continue loop      # pred 已删除，重来
        while curr 非空:
            succ = curr[l].load_consume()
            if succ.tag == 1:                # curr 已删除，帮忙摘除
                if help_unlink(pred[l], curr, succ): curr = 新 succ; continue
                else: continue loop          # 摘除失败，重来
            if 满足 bound: 记录 result; break   # 本层定位完成
            pred = curr; curr = succ
    return result
```

> `load_consume`（u2-l4 / u5-l2）保证「读到指针之后解引用该节点」这一条依赖链上的读不会重排到 load 之前，比 `Acquire` 更便宜。在跳表这种「读指针 → 访问节点字段」的 RCU 读路径上正合适。

#### 4.1.3 源码精读

先看 `search_bound` 本体，它就是上面伪代码的真实实现：[base.rs:833-920](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L833-L920)。注意第 873 行对「pred 被标记则整体重来」的判断，以及第 882-892 行对「succ 被标记则 help_unlink」的处理。

协作摘除发生在 `help_unlink`：[base.rs:759-781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L759-L781)。它做一次 CAS，把 `pred` 在该层指向 `curr` 的指针改成指向 `succ.with_tag(0)`（清掉 tag）：

```rust
match pred.compare_exchange(
    Shared::from(curr.ptr.as_ptr() as *const Node<K, V>), // 期望：pred → curr
    succ.with_tag(0),                                     // 改成：pred → succ(去标记)
    Ordering::Release, Ordering::Relaxed, guard,
) {
    Ok(_) => {
        unsafe { curr.decrement(guard) } // 摘除成功，curr 少一个层级引用
        Some(succ.with_tag(0))
    }
    Err(_) => None, // 有别的线程抢先改了 pred，交给它
}
```

这里 `curr.decrement(guard)` 极其关键：摘除一个层级意味着 `curr` 的引用计数减一（4.4 会展开），只有当引用计数归零，节点才会真正被回收。

`search_position` 是 `search_bound` 的「落点版」，同样逐层下降、同样 help_unlink，但每一层都把 `pred` 记进 `left[level]`、把右节点记进 `right[level]`，并记下 `found`：[base.rs:923-1008](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L923-L1008)。`get` 就是先调 `search_bound` 再用 `comparator.equivalent` 校验是否真命中：[base.rs:569-585](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L569-L585)。

#### 4.1.4 代码实践

**目标**：跟踪一次 `search_bound` 的逐层下降，验证「协作摘除」确实会被触发。

**步骤**（源码阅读型）：

1. 打开 [base.rs:833-920](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L833-L920)，对照 4.1.2 的伪代码，给每一段标上对应的行号。
2. 找到 `search_bound` 中所有 `continue 'search`（整体重来）的位置（约 874、891 行），分别说明触发条件：哪一个是「pred 被标记」、哪一个是「help_unlink 失败」。
3. 在 `help_unlink`（[base.rs:759-781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L759-L781)）旁批注：CAS 成功后为何要 `curr.decrement`（提示：引用计数对应「每个被装的层级」）。

**需要观察的现象**：你会发现 `search_bound` 与 `search_position` 的下降骨架几乎一字不差，区别只在「记录单个 result」还是「记录每层 left/right」。这正是无锁跳表把读路径与写路径复用的体现。

**预期结果**：能口头复述「下降 → 遇标记 → help_unlink → 失败则整体重来」这条链。

**待本地验证**：若想动态观察，可在 `help_unlink` 的 `Ok(_)` 分支加一行 `eprintln!("helped unlink at level");`，然后跑 4.4 综合实践里的多线程 remove 程序，统计协作摘除的触发次数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `search_bound` 看到 `curr.tag() == 1`（pred 被标记）时选择「整体重来」而不是 help_unlink？

> 答案：`curr.tag() == 1` 表示的是 `pred`（我们当前的前驱）已经被删除，而非 `curr`。我们手里没有 `pred` 的前驱指针，无法把 `pred` 从链里摘掉，所以只能从 head 重新下降，让新一轮扫描自然绕开被标记的 pred。

**练习 2**：`help_unlink` 的 CAS 失败时为什么可以直接返回 `None`、不做重试？

> 答案：CAS 失败意味着「pred 的下一跳已经不是 curr」——必定有别的线程抢先改了 pred（可能就是另一个 help_unlink 或一次插入/删除）。摘除这件事已经被那个线程接管，不必抢；本线程只需整体重来（`continue 'search`），重新扫描自然会看到摘除后的结果。

### 4.2 并发插入：两阶段落点与引用计数

#### 4.2.1 概念说明

`insert`（及其内核 `insert_internal`）是无锁跳表最难的一段，难点在于：插入一个高度为 `h` 的节点，要在 `h` 个层级各做一次 CAS 安装，而这些层级彼此独立、随时有别的线程在插在删。策略是**两阶段**：

1. **必成阶段（level 0）**：第 0 层是跳表的「地基」，必须成功安装。一次 CAS 把 `pred[0] → right[0]` 改成 `pred[0] → 新节点`；失败就重新 `search_position` 再来。
2. **尽力阶段（level 1..h）**：高层只是「加速索引」，可有可无。逐层尝试安装，任何一层失败或检测到自身被并发删除，就直接停止补建——节点已经合法存在于第 0 层，功能完整，只是矮一点。

引用计数（u7-l1）在这两阶段里精确记账：新节点初始 ref_count = 2（一个给即将返回的 Entry 句柄，一个给 level 0 的链接），此后每成功装进一层，ref_count 加 1。

#### 4.2.2 核心流程

`insert_internal` 的主干：

```
search = search_position(key)          # 1. 先查，若已存在且不替换，直接返回
if search.found 且 不替换: try_acquire 返回已有 entry

node = Node::alloc(height, ref_count=2)  # 2. 分配，初始计数=2
hot_data.len.fetch_add(1)                # 3. 乐观地先 +1

loop:                                    # 4. 必成阶段：CAS 装进 level 0
    node.level[0] = right[0]
    if CAS(left[0]: right[0] -> node) 成功:
        if 旧节点 found 且 标记成功(mark_tower): len -= 1   # 替换语义
        break
    search = search_position(key)       # 失败，重扫再试

# 5. 尽力阶段：补建 level 1..height-1
for level in 1..height:
    loop:
        next = node.level[level].load()
        if next.tag == 1: 停止补建       # 自己已被并发删除
        if 后继 key 与自己等价: 重扫后重试  # 禁止两节点同 key 挂接
        CAS(node.level[level]: next -> succ)
        ref_count += 1
        if CAS(left[level]: succ -> node) 成功: break
        ref_count -= 1; 重扫            # 失败，回滚并重试

if 顶层指针被标记: search_bound 触发清理  # 兜底
return Entry{ node }
```

几个关键不变量：

- 新节点的 key/value 在装进 level 0 **之前**就写好了（`ptr::addr_of_mut!((*n).key).write(key)`），且 level 0 的 CAS 用 `SeqCst`——成功之后，其他线程通过 `load_consume` 读到这个节点，就一定能读到完整的 key/value。
- 第 0 层成功之前若 `search_position` panic，`ScopeGuard` 会在 drop 里 `Node::finalize` 同步销毁新节点，不泄漏。
- 同 key 不重复挂接：补建高层时若发现后继与自己 key 等价，会先重扫把那个（已标记的）等价后继摘掉，再重试。

#### 4.2.3 源码精读

分配与初始计数（注意注释解释了为何是 2）：[base.rs:1051-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1051-L1065)

```rust
// The reference count is initially two to account for:
// 1. The entry that will be returned.
// 2. The link at the level 0 of the tower.
let n = Node::<K, V>::alloc(height, 2);
ptr::addr_of_mut!((*n).key).write(key);
ptr::addr_of_mut!((*n).value).write(value);
```

必成阶段——level 0 的 CAS，外加「替换旧节点」分支：[base.rs:1070-1094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1070-L1094)。CAS 失败时用 `ScopeGuard` 包住重扫，确保 panic 也不泄漏新节点：[base.rs:1096-1108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1096-L1108)。

尽力阶段——补建高层，逐层 CAS 并增减引用计数：[base.rs:1135-1218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1135-L1218)。注意三处自保护：检测自身被标记就 `break 'build`（1149）、禁止同 key 挂接就重扫（1171-1177）、安装失败就 `fetch_sub` 回滚（1206-1208）。最后还有一个兜底：如果补建期间顶层被人标记，再扫一次触发摘除：[base.rs:1220-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1220-L1229)。

对外 `insert`（替换语义，`replace` 闭包恒为 `true`）只是 `insert_internal` 的薄壳：[base.rs:1247-1249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1247-L1249)。

#### 4.2.4 代码实践

**目标**：亲眼看到「同 key 并发插入」最终只剩一个节点、`len` 仍然正确。

**步骤**：

1. 阅读测试 [tests/map.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L134-L151)（`concurrent_insert`）：两个线程用 `Barrier` 对齐后同时 `insert(1,1)`，重复 100 轮，正是为压测「level 0 CAS 只有一个赢家」。
2. 在仓库根目录写一个临时小例子（**示例代码**，非项目自带）：

```rust
// 示例代码：放成单独的 test 或 examples/xxx.rs
use crossbeam_skiplist::SkipMap;
use crossbeam_utils::thread::scope;

let map = SkipMap::<i32, i32>::new();
scope(|s| {
    for _ in 0..4 {
        s.spawn(|_| {
            for _ in 0..1000 {
                map.insert(42, 42); // 四线程狂插同一个 key
            }
        });
    }
}).unwrap();
assert_eq!(*map.get(&42).unwrap().value(), 42);
assert_eq!(map.len(), 1, "同 key 最终只应剩一个节点"); // 待本地验证
```

3. 用 `cargo test -p crossbeam-skiplist --test map concurrent_insert -- --nocapture` 跑官方测试；再跑你自己的小例子。

**需要观察的现象**：无论如何并发，`map.len()` 恒为 1，`get(&42)` 始终命中。

**预期结果**：CAS 让「同时插入同 key」只有一个赢家；输家在 `search.found` 分支走「标记旧节点 / 返回已有 entry」路径，不会留下重复节点。

**待本地验证**：`len()` 的精确值在你自己的例子中（取决于线程调度），但同 key 唯一性必然成立。

#### 4.2.5 小练习与答案

**练习 1**：新节点的初始引用计数为什么是 2 而不是 1？

> 答案：2 对应两份独立的「持有者」：① 即将返回给调用者的 `Entry`/`RefEntry` 句柄；② 节点在第 0 层被链入跳表这一事实。两份要分别 drop/摘除，计数才能各自归零。若只有一个再装进链表，节点被用户 release 时就会被错误回收。

**练习 2**：补建高层时，某层 CAS 安装失败，代码为什么先 `fetch_sub` 再重扫，而不是直接重试 CAS？

> 答案：安装失败说明 `left[level] → succ` 这个前提已失效（pred 或 succ 变了），必须重新 `search_position` 拿到新的 `left`/`right`。在重扫之前先把「为这一层预增的引用计数」减回去，保持计数与实际挂接层数一致，避免泄漏计数导致节点永不回收。

### 4.3 并发删除：逻辑标记 + 逐层摘除 + 收尾

#### 4.3.1 概念说明

删除一个节点分三步，是无锁算法里经典的 Harris/Michael「逻辑删除 + 物理摘除」两阶段法的多层版本：

1. **逻辑删除（mark_tower）**：自顶向下逐层 `fetch_or(1)` 给自己的指针打 tag。一旦 level 0 的 tag 被置位，这个节点对所有后续导航「逻辑上不存在」。level 0 的 tag 是唯一裁决者——谁成功置位 level 0，谁就是「真正的删除者」。
2. **物理摘除（unlink）**：把被标记的节点从每一层的链里摘掉（CAS 改前驱指针）。这一步可以由删除者自己干，也可以由任何路过的线程通过 help_unlink 干。
3. **引用计数收尾**：每摘掉一层，节点 ref_count 减一；最后一个持有者（Entry 句柄 drop、或最后一层被摘）让计数归零，才进入 epoch 延迟回收。

#### 4.3.2 核心流程

`remove(key)` 的主干：

```
loop:
    search = search_position(key)
    n = search.found?  否则返回 None
    entry = try_acquire(n)?   # 先抢引用计数，失败说明刚被删，重扫
    if n.mark_tower():        # 我赢得了「标记 level 0」→ 我是删除者
        len -= 1
        for level in (0..n.height()).rev():     # 逐层摘除
            succ = n.level[level].load().with_tag(0)
            if CAS(left[level]: n -> succ) 成功:
                n.decrement()                    # 这层摘掉了，-1
            else:
                search_bound(key)               # 被人抢先，交给它，退出
                break
        return Some(entry)
    else:
        n.decrement()           # 别人已标记，我只 release 自己抢的计数
        return None
```

`mark_tower` 自顶向下、用 level 0 裁决赢家：

```
for level in (0..height).rev():       # 从最高层往第 0 层
    tag = level[l].fetch_or(1, SeqCst).tag()
    if level == 0 && tag == 1:        # level 0 早被标记 → 我输了
        return false
return true                            # 我成功标记了 level 0 → 我是赢家
```

#### 4.3.3 源码精读

`mark_tower`：[base.rs:327-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348)。注意它用 `epoch::unprotected()`——因为这里只取 tag、不解引用指针，不需要 pin 保护。

`remove` 全貌：[base.rs:1270-1337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1270-L1337)。摘除循环里两条出路都很典型：CAS 成功就 `n.decrement(guard)`（1322 行）；某层 CAS 失败就放弃手动摘除、改用 `search_bound` 让协作摘除兜底（1325 行）。

`pop_front`/`pop_back` 是 `front`/`back` + `RefEntry::remove` 的循环重试封装：[base.rs:1340-1367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1340-L1367)。`RefEntry::remove`（也是「标记 + 触发 search_bound 摘除」）在 [base.rs:1694-1711](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1694-L1711)。

`Entry::remove`（绑定 Guard 的版本）同理：[base.rs:1530-1545](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1530-L1545)。

#### 4.3.4 代码实践

**目标**：验证「同一 key 被多线程同时 remove」时只有一个线程拿到 `Some`，其余拿到 `None`，且 `len` 准确。

**步骤**：

1. 阅读官方测试 `concurrent_remove`：[tests/map.rs:173-190](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L173-L190)。两个线程用 Barrier 对齐后同时 `remove(&1)`，重复 100 轮。
2. 在 `mark_tower` 的 `return true` 处（[base.rs:347](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L347)）旁推演：level 0 的 `fetch_or` 返回旧 tag，若旧 tag 已是 1，则 `tag == 1` 成立、返回 false——这正是「我是后来者」的判定。

**需要观察的现象**：两线程同时 `remove(&1)`，最终 `len` 归 0、`contains_key(&1)` 为 false，且不会 panic、不会 double-free。

**预期结果**：`mark_tower` 的 level 0 裁决保证只有一个线程赢得标记权，其余线程在 `remove` 的 `else` 分支 `decrement` 后返回 `None`。

**待本地验证**：如果想量化，可给 `mark_tower` 的 `return true/false` 各加一行计数日志，跑 100 轮统计「赢家 vs 输家」的比例。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mark_tower` 必须自顶向下，而不是自底向上？

> 答案：自顶向下保证「当一个节点在 level 0 被打上标记时，它所有更高层都已经被标记」。这样后续导航在任何一层看到标记都能一致地判断「此节点已删除」。若自底向上，先标记 level 0 再标高层，会出现「level 0 已标但高层未标」的中间窗口，高层读者可能把节点当作存活节点向右走，造成判断不一致。此外 level 0 的 tag 是 CAS 裁决赢家的唯一依据，必须最后置位。

**练习 2**：`remove` 逐层摘除时，某层 CAS 失败后为何直接 `break` 并调 `search_bound`，而不是重试该层 CAS？

> 答案：某层 `left[level]: n -> succ` 失败，说明 pred 的下一跳已经变了（可能别的线程已帮你摘了这一层、或 pred 本身被删）。与其猜测，不如交给 `search_bound` 的协作摘除机制统一清理——它会在下降途中 help_unlink 掉所有残留层。引用计数不会因此泄漏，因为每一层的摘除（无论谁干的）都会 `decrement`。

### 4.4 epoch 延迟回收与 SkipMap/SkipSet 包装

#### 4.4.1 概念说明

前面三节反复出现 `n.decrement(guard)`。现在回答最后一个问题：**节点到底何时、由谁、用什么方式真正释放？**

答案串联 u5 单元：节点的引用计数归零那一刻，并不立即 free，而是把 `Node::finalize`（drop key/value + deallocate）作为闭包交给 `guard.defer_unchecked`，推迟到「当前 epoch + 2」之后无人能再持有该节点指针时执行。为什么必须延迟？因为别的线程可能正拿着这个节点的 `Shared` 指针在 `search_bound` 里向右走——它虽然读到了「已标记」状态，但解引用节点字段（读 key 做比较）这一步可能尚未完成；若此刻 free，就是 use-after-free。epoch 的两代宽限期（u5-l5）正好覆盖这种「读指针到读完字段」的窗口。

包装层 `SkipMap`/`SkipSet` 则是把「手动 pin、手动传 Guard」的 `base::SkipList` 包成「打开即用」的接口：每个方法自己 `epoch::pin()`、自己释放引用计数。

#### 4.4.2 核心流程

**引用计数回收**。`Node::decrement` 是所有写操作的收尾原语：

```
fetch_sub(1<<5, Release)
if 旧值 >> 5 == 1:        # 减之前高位恰好是 1 → 减完归零
    fence(Acquire)         # 与 Release 配对，保证「归零」前的所有写对回收方可见
    guard.defer_unchecked(move || Node::finalize(ptr))  # 推迟销毁
```

引用计数语义（u7-l1）：ref_count = (节点被装进的层数) + (指向它的 Entry/RefEntry 句柄数)。所以一次完整的 remove = 标记 + 每层 `decrement` + 调用方 release Entry 时再 `decrement` 一次，计数才会归零。

**SkipMap 包装**。`SkipMap` 只是一个壳：

```
pub struct SkipMap<K,V,C> { inner: base::SkipList<K,V,C> }

impl SkipMap {
    fn insert(&self, k, v) -> Entry {
        let guard = &epoch::pin();           // 自己 pin
        Entry::new(self.inner.insert(k, v, guard))  // 转调 base
    }
}
```

返回的 `map::Entry` 用 `ManuallyDrop<base::RefEntry>` 持有引用计数，`Drop` 时 `release_with_pin(epoch::pin)` 释放。

**SkipSet 包装**。更薄，本质是 `SkipMap<T, ()>`：

```
pub struct SkipSet<T,C> { inner: map::SkipMap<T, (), C> }
```

#### 4.4.3 源码精读

引用计数加法 `try_increment`（拒绝零计数，防止「复活」即将回收的节点导致 double-free）：[base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)。

引用计数减法 `decrement`——**这是全文件唯二的两处 `defer_unchecked` 之一**：[base.rs:294-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L294-L304)。另一处是 `decrement_with_pin`（drop 时可能已离开 pin 上下文，故按需 pin）：[base.rs:309-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L309-L324)，其 `defer_unchecked` 在 [base.rs:322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L322)。

被推迟执行的闭包本体 `Node::finalize`（drop key/value + deallocate，不跑任何其它析构）：[base.rs:252-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L252-L262)。

> 注意区分两类销毁：**延迟销毁**（`defer_unchecked`，进 epoch 垃圾袋）只发生在并发路径的 `decrement`/`decrement_with_pin`；**同步销毁**（直接调 `Node::finalize`）发生在「此刻无并发」的确定性路径，例如 insert 重扫 panic 的 `ScopeGuard::drop`（[base.rs:1099-1104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1099-L1104)）、insert 发现 key 已存在而销毁新节点（[base.rs:1117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1117)）、`SkipList::drop`（[base.rs:1431](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1431)）与 `IntoIter::drop`（[base.rs:2277](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2277)）。同步销毁无需 epoch 保护，因为那时只有当前线程能访问节点。

`SkipMap` 包装的典型方法（insert / get / remove）：[map.rs:403-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L403-L406)、[map.rs:271-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278)、[map.rs:456-463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L456-L463)。读路径用 `try_pin_loop` 重试到 `pin()` 成功：[base.rs:2332-2341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2332-L2341)。

`map::Entry` 用 `ManuallyDrop` 持有并在 Drop 时 `release_with_pin(epoch::pin)`：[map.rs:597-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L597-L630)。

`SkipSet` 的极薄包装：[set.rs:24-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L24-L26)。

crate 文档对「为何需要 epoch 回收」有最直白的说明：[lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125)。

#### 4.4.4 代码实践

**目标**：用 `SkipMap` 在多线程并发 insert/get/remove，验证最终状态一致；并在源码里定位所有 `defer_unchecked` 调用点，说明每处释放的对象与触发时机。

**步骤一（动手验证）**：在仓库根目录建一个临时 example 或 test（**示例代码**）：

```rust
// 示例代码
use crossbeam_skiplist::SkipMap;
use crossbeam_utils::thread::scope;

let map = SkipMap::<i32, i32>::new();
scope(|s| {
    s.spawn(|_| { for i in 0..2000 { map.insert(i, i * 10); } });            // 写
    s.spawn(|_| { for i in 0..2000 { let _ = map.get(&(i % 2000)); } });      // 读
    s.spawn(|_| { for i in 0..2000 { if i % 2 == 0 { map.remove(&i); } } }); // 删偶数
}).unwrap();

// 偶数键应已删除，奇数键应保留
for i in 0..2000 {
    if i % 2 == 0 {
        assert!(map.get(&i).is_none(), "偶数 key {} 应被删除", i);
    } else {
        assert_eq!(*map.get(&i).unwrap().value(), i * 10, "奇数 key {} 应保留", i);
    }
}
println!("len ≈ {}", map.len()); // 待本地验证：应为 1000（松弁读可能瞬时偏移）
```

跑：`cargo run --example <名字> -p crossbeam-skiplist`（或写成 `#[test]` 用 `cargo test`）。

**步骤二（源码阅读）**：在 [base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) 中搜索 `defer_unchecked`，应只命中两处：`decrement`（[base.rs:302](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L302)）与 `decrement_with_pin`（[base.rs:322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L322)）。再搜索 `Node::finalize`（直接调用），列出所有「同步销毁」点（1102、1117、1431、2277）。

**需要观察的现象**：程序不 panic、不段错误；偶数键全删、奇数键全留。

**预期结果**：四类同步销毁 + 一类延迟销毁（`defer_unchecked`）共同覆盖所有节点释放路径；延迟销毁的闭包统一是 `Node::finalize(self.ptr)`，释放对象是「引用计数归零的 Node」。

**待本地验证**：由于 epoch 回收由后台周期性 `collect` 驱动（u5-l5），节点的实际析构时刻取决于 pin 的节奏；若想观察延迟效果，可给 `Node::finalize` 加 `eprintln!("finalize key dropped {:?}", ...)`，对比「remove 返回」与「finalize 打印」的时间差。

#### 4.4.5 小练习与答案

**练习 1**：`decrement` 里 `fetch_sub` 用 `Release`，随后又 `fence(Acquire)`，这一对为什么是回收安全的关键？

> 答案：删除方在把节点摘下链表、改写各层指针时用 Release 语义发布；回收方要等到 ref_count 归零才执行 finalize。`fetch_sub(Release)` 保证「本线程对节点的所有写」对随后读到归零计数的回收线程可见；`fence(Acquire)` 则保证回收线程在执行 finalize（drop key/value）前，看到该节点历史上所有的初始化与修改。少了这对同步，finalize 可能读到半初始化的字段或与并发写竞争，造成 UB。

**练习 2**：为什么 `SkipList::drop`、`IntoIter::drop` 里的销毁用直接 `Node::finalize`，而不是 `defer_unchecked`？

> 答案：drop 整个 `SkipList` / 消费迭代器时，所有权已经独占——此刻没有别的线程还能访问这些节点（注释 `unprotected loads are okay because this function is the only one currently using the skip list` 即此意）。既然没有并发读者，就不需要 epoch 宽限期，直接同步销毁既简单又能在 drop 时立即释放内存，而不是「不知何年何月」地等全局 collector 回收。

## 5. 综合实践

**任务**：把本讲四条主线串起来——导航、插入、删除、epoch 回收——做一个「并发计数表」小实验。

**要求**：

1. 用 `SkipMap<i32, AtomicI64>`（值用原子整数，因为跳表不提供 `get_mut`，见 [lib.rs:86-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L86-L100) 的解释）。
2. 启动 N 个线程（如 8 个），每个线程对若干 key 做 `get_or_insert` 创建、再对对应 `Entry::value()` 的 `AtomicI64` 做 `fetch_add` 累加，最后随机 `remove` 一些 key。
3. 在 `scope` 返回后，遍历 `map.iter()`，校验每个存活 key 的累加值等于「期望总和」。

**关注点**（对应本讲四个模块）：

- **导航**：`get_or_insert` 内部就是 4.1 的 `search_position` + 4.2 的 `insert_internal`；并发同 key 只会创建一个节点（4.2 的 CAS 保证）。
- **删除**：被 `remove` 的 key 在 `iter()` 中不再出现（4.3 的 mark_tower + 协作摘除）。
- **回收**：那些被删又被读过的节点，不会立即析构，要等 epoch 宽限期——用 `Rc`/`Arc` 的弱引用计数或加日志观察 finalize 时机（4.4）。

**预期结果**：最终状态确定——存活的 key 集合与各自累加值都对得上，无丢失、无重复、无 panic。

**待本地验证**：epoch 回收是非确定性的，不要在断言里依赖「析构已发生」；只断言「逻辑状态一致」。可以用 `cargo test -p crossbeam-skiplist` 跑全量测试，或用 `cargo +nightly miri test -p crossbeam-skiplist`（u7-l3 会讲）做无 UB 校验。

## 6. 本讲小结

- **导航复用**：`search_bound`（读）与 `search_position`（写）共享「逐层下降 + 协作摘除」骨架；任何线程看到被标记的节点都通过 `help_unlink` 顺手摘掉，失败则整体重来——这是无锁跳表处理并发删除的核心范式。
- **插入两阶段**：level 0 CAS 必成（地基），高层 CAS 尽力补建（加速索引）；新节点初始引用计数为 2（Entry 句柄 + level 0 链接），每成功装一层 +1，体现「计数 = 层数 + 句柄数」。
- **删除三步走**：`mark_tower` 自顶向下逻辑标记、以 level 0 tag 裁决唯一赢家；赢家逐层 unlink 并 `decrement`，输家只 `decrement` 自己抢的计数。
- **引用计数归零 → epoch 延迟回收**：全文件唯二的 `defer_unchecked` 在 `decrement`/`decrement_with_pin`，统一推迟 `Node::finalize` 到两代宽限期后，杜绝 use-after-free；确定性独占路径（drop、panic 清理）则同步 `finalize`。
- **包装层**：`SkipMap` 是「每个方法自己 `epoch::pin()` + 返回引用计数 `Entry`」的薄壳；`SkipSet` 是 `SkipMap<T, ()>` 的更薄壳。
- **可线性化与协作**：单个操作原子，跨操作不原子（[lib.rs:43-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L43-L84)）；无锁不等于无逻辑竞态，但绝不破坏内存安全。

## 7. 下一步学习建议

- **u7-l3（测试、loom 与并发正确性）**：本讲的「协作摘除」「两阶段插入」「引用计数回收」都是极易出微妙 bug 的地方。下一讲带你用 loom（状态空间模型检查）、miri（UB 检测）、tsan（数据竞争检测）去验证这些代码，并在 [crossbeam-skiplist/tests/](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests) 的并发测试与 loom 测试里看实战。
- **回看 u5 单元**：若你对 `defer_unchecked` 的「两代宽限期」仍有疑惑，重读 u5-l5（internal：epoch 推进与垃圾回收）的 `try_advance` / `collect` 协议，会把本讲 4.4 的「为何延迟两代」彻底讲透。
- **深入 Harris/Michael 删除法**：本讲的 mark + unlink 是该经典算法在多层跳表上的推广，可对照原始论文理解 level 0 tag 为何能保证可线性化。
- **阅读 comparator 抽象**：跳表支持自定义 `Comparator`（[map.rs:17-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L17-L30)），这是 `search_bound` 里 `above_lower_bound`/`below_upper_bound` 与 `equivalent` 判定的来源，值得作为扩展阅读。
