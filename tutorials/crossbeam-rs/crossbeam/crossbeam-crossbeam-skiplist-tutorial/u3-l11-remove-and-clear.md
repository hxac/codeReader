# 删除与清理：remove / pop_front,back / clear

## 1. 本讲目标

本讲是「核心算法主链路」单元的最后一块。前面三讲（u3-l8 搜索、u3-l9 标记删除、u3-l10 插入）已经把**读路径**、**逻辑删除标记**和**插入**讲透了，本讲专门回答「数据怎么从跳表里彻底拿走」。

学完本讲你应当能够：

1. 说清 `SkipList::remove` 的**两步协议**——先抢引用计数（保证返回的节点活着）、再抢删除权（`mark_tower`），并解释输掉删除权时为何要 `decrement` 退计数以免泄漏。
2. 理解 `remove` 赢得删除权后的**逐层 CAS 物理摘除**，以及某层 CAS 失败时为何用一次 `search_bound` 兜底，而不是死磕重试。
3. 读懂 `pop_front` / `pop_back` 的「循环弹出」实现，看出它们其实是 `front`/`back` + `RefEntry::remove` 的组合。
4. 解释 `clear` 为何要**分批（`BATCH_SIZE = 100`）标记**并在每批之间 `guard.repin()`，以及这与 epoch 推进的代价关系。
5. 看懂 `Drop for SkipList` 与 `IntoIter` 为何能用 `epoch::unprotected()` 安全地直接释放整条链，以及 `next()` 用 `dealloc`、`Drop` 用 `finalize` 的区别。

本讲只读 `src/base.rs` 一个文件，所有链接都指向当前 HEAD `6195355`。

## 2. 前置知识

本讲默认你已经读过 u3-l8 ~ u3-l10，这里只做最小回顾：

- **逻辑删除 vs 物理摘除**：`mark_tower` 在节点所有出边指针上 `fetch_or(1)` 打 tag（逻辑删除，瞬间完成、全局可见），物理摘除才是把节点从链表里 CAS 摘下来。`is_removed()` 只看 level-0 的 tag。
- **tag 打在出边、却描述源头节点**：节点 `c` 的 level-0 后继指针 `succ.tag()==1`，意味着 `c` 已被逻辑删除——读路径据此调用 `help_unlink` 协助把 `c` 物理摘除并 `decrement`。
- **引用计数与 epoch 双闸门**：`refs_and_height` 的高位是引用计数，`try_increment` 只在计数非零时成功（防 double-free）；`decrement` 用 `fetch_sub(Release)`，归零时配 `fence(Acquire)` 再 `defer_unchecked(finalize)` 把析构推迟到 epoch 安全。
- **Guard（epoch）保护临时读，引用计数保护跨临界区长期持有**（如返回给调用方的 `RefEntry`）。
- **协作式清理（cooperative cleanup）**：任何搜索在路过被标记节点时都会顺手 `help_unlink`，这是 lock-free 活性的来源。

一个本讲要用到、前面没强调的点：**`Drop` 和 `IntoIter` 拥有独占访问**。Rust 的所有权规则保证 `Drop` 运行时没有别的 `&SkipList`，`into_iter(self)` 消费了 `self` 也不再有人持有引用。正因为「全世界只有我一个线程在碰这条表」，这两处可以绕开 epoch 直接用 `unprotected()` 加载——这是后面 4.4 的关键。

## 3. 本讲源码地图

本讲只涉及一个文件，但用到其中若干分散的函数：

| 符号 | 位置（行） | 作用 |
|---|---|---|
| `SkipList::remove` | `src/base.rs:1270-1337` | 按 key 删除，两步协议 + 逐层摘除 |
| `SkipList::pop_front` / `pop_back` | `src/base.rs:1340-1367` | 弹出最小/最大 key 的循环实现 |
| `SkipList::clear` | `src/base.rs:1370-1410` | 分批标记 + `repin` |
| `Drop for SkipList` | `src/base.rs:1413-1437` | 独占访问下整链 `finalize` |
| `IntoIterator::into_iter` | `src/base.rs:1449-1477` | 拆链为 `IntoIter` |
| `IntoIter` 结构 / `Drop` / `next` | `src/base.rs:2256-2322` | owning 迭代器的逐节点释放 |
| `RefEntry::remove` | `src/base.rs:1694-1710` | 句柄视角的删除（`pop_*` 复用它） |
| `mark_tower` | `src/base.rs:327-348` | 自顶向下打 tag、以 level-0 定胜负 |
| `RefEntry::try_acquire` / `Node::try_increment` | `src/base.rs:1670-1682` / `222-249` | 抢引用计数 |
| `decrement` / `finalize` | `src/base.rs:294-304` / `253-262` | 释放引用、归零则析构+回收 |
| `help_unlink` | `src/base.rs:759-781` | 读路径协作式摘除+`decrement` |

---

## 4. 核心概念与源码讲解

### 4.1 remove：引用抢占 + 删除权抢占的两步协议

#### 4.1.1 概念说明

`remove(key)` 要做一件看似简单的事：找到 key 对应的节点，删掉它，把删下来的 `(key, value)` 还给调用方。但在无锁并发下，这件事被拆成**两个独立的竞争**：

1. **抢引用计数**：因为要把节点作为 `RefEntry` 返回，调用方会在 Guard 生命周期之外继续持有它，所以必须先把它的引用计数 +1，确保「哪怕别的线程已经把它物理摘除，只要我们这个 entry 没释放，它就不会被回收」。这一步用 `try_increment`——如果计数已经是 0（节点已被排队回收），就拒绝，避免 double-free。
2. **抢删除权**：多个线程可能同时对同一 key 调 `remove`，但「删除」这个语义只能发生一次。`mark_tower` 以「level-0 的 tag 从 0 翻成 1」作为线性化点，**恰好一个线程**能赢，赢得者才算真正删了。

这两步是**顺序的、且各自可失败**，于是外层套一个 `loop` 重试。这就是 Harris/Michael 无锁删除在 crossbeam-skiplist 里的具体落形。

#### 4.1.2 核心流程

```text
loop {
    search = search_position(key)          // 读路径定位，顺带协作清理
    n = search.found?                      // 没找到 → 返回 None（退出函数）
    entry = RefEntry::try_acquire(n)?      // ① 抢引用计数；失败 → continue 重搜
    if n.mark_tower() {                    // ② 抢删除权
        len -= 1                           //    赢了：len 递减
        逐层 CAS 物理摘除 n                //    优化：手动摘，失败就 search_bound 兜底
        return Some(entry)                 //    把 entry（持有 +1 引用）返回
    } else {
        n.decrement()                      //    输了删除权：退回 ① 加的那次引用，避免泄漏
        return None                        //    别人已经删了
    }
}
```

两个失败分支要特别留意：

- `try_acquire` 失败（`None`）：节点正被并发回收，**没有给计数 +1**，直接 `continue` 重搜即可，**不泄漏**。
- `mark_tower` 失败（返回 `false`）：节点已被别人标记。但**① 已经把计数 +1 了**，所以必须 `n.decrement(guard)` 把这次引用退掉，否则引用计数永远多 1，节点永远无法归零回收——这就是「输掉删除权要退计数」的原因。

#### 4.1.3 源码精读

入口与外层循环——注意第 1281 行那个把 Guard 生命周期重绑到 `&self` 的 unsafe hack（u3-l10 已解释其意图：让返回的 `RefEntry` 不绑定 Guard 的生命周期）：

[ src/base.rs:1270-1287 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1270-L1287) —— `remove` 先 `check_guard` 校验 Guard 与本表的 `Collector` 同源，再把 Guard 重绑生命周期，然后 `loop`：`search_position` 定位，`search.found?` 没命中就整体返回 `None`。

第一步「抢引用计数」：

[ src/base.rs:1289-1294 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1289-L1294) —— 调 `RefEntry::try_acquire(self, n)`，内部是 `Node::try_increment` 的 CAS；失败（节点计数已归零、正被回收）就 `continue` 重搜。

第二步「抢删除权」与 `len` 递减：

[ src/base.rs:1296-1300 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1296-L1300) —— `n.mark_tower()` 返回 `true` 表示本线程赢得了 level-0 的 0→1 翻转，是删除的线性化点；随后 `len.fetch_sub(1, Relaxed)`（`len` 只是近似值，用 `Relaxed` 足够）。

`mark_tower` 本体（自顶向下、以 level-0 定胜负）：

[ src/base.rs:327-348 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348) —— 从最高层向 level-0 逐层 `fetch_or(1)`；只有当 `level == 0` 且读回的旧 `tag == 1`（说明已被别人标记）才返回 `false`。注意它用 `epoch::unprotected()` 加载——因为我们只读 tag 位、不持有指针，安全。

输掉删除权时的「退计数」：

[ src/base.rs:1330-1334 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1330-L1334) —— `else` 分支：`n.decrement(guard)` 退回 ① 加的引用，返回 `None`。

`try_acquire` / `try_increment` 的实现，用于理解「为何失败要重搜而不是泄漏」：

[ src/base.rs:222-249 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249) —— `try_increment` 先读计数，若高位为 0（已归零）直接返回 `false`；否则 CAS 加 `1 << HEIGHT_BITS`，CAS 失败就重读重试。

#### 4.1.4 代码实践

**目标**：亲手验证「多线程并发 `remove` 同一 key，恰好一个线程赢得删除权」。

**操作步骤**（源码阅读 + 测试编写）：

1. 阅读 `tests/base.rs` 顶部的测试惯用法（`Entry` 包装 + `release_with_pin(epoch::pin)` 的 Drop），见 [ tests/base.rs:11-26 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L11-L26)。
2. 在 `tests/base.rs` 仿照现有风格新增一个测试：用一张表插入 1 个 key，然后开 N 个线程并发调 `s.remove(&key, guard)`，统计返回 `Some` 的次数。
3. 关键：每个线程内部要 `let guard = &epoch::pin();`，且拿到的 `RefEntry` 必须 `.release(guard)`（否则引用计数不归零，无法验证）。

**示例代码**（标注为「示例代码」，非项目原有）：

```rust
#[test]
fn concurrent_remove_single_winner() {
    let collector = epoch::default_collector().clone();
    let s = SkipList::new(collector);
    {
        let guard = &epoch::pin();
        s.insert(42, 42, guard).release(guard); // 初始计数归位
    }

    const N: usize = 8;
    let wins = AtomicUsize::new(0);
    std::thread::scope(|scope| {
        for _ in 0..N {
            scope.spawn(|| {
                let guard = &epoch::pin();
                if let Some(e) = s.remove(&42, guard) {
                    wins.fetch_add(1, Ordering::SeqCst);
                    e.release(guard); // 退引用
                }
            });
        }
    });

    assert_eq!(wins.load(Ordering::SeqCst), 1); // 恰好一个赢家
    let guard = &epoch::pin();
    assert!(s.get(&42, guard).is_none());
}
```

**需要观察的现象**：无论跑多少次，`wins` 恒为 1，且事后 `get` 查不到该 key。

**预期结果**：`remove` 的「删除权竞争」以 level-0 tag 的 0→1 翻转为线性化点，故全局只有一个赢家。

**待本地验证**：并发时序依赖线程调度，建议在 `--release` 下循环跑 100 次以放大竞态窗口。

#### 4.1.5 小练习与答案

**练习 1**：如果把第二步失败分支的 `n.decrement(guard)` 删掉，会发生什么？
**答案**：每次「输掉删除权」都会让该节点的引用计数永久多 1。由于被替换/删除的旧节点本应由 epoch 回收，计数永不归零意味着 `finalize` 永不触发，造成内存泄漏（节点的 key/value 析构也不会执行）。

**练习 2**：为什么 `len.fetch_sub` 用 `Ordering::Relaxed` 而不是 `SeqCst`？
**答案**：`len` 在文档里就被声明为「近似值」（u3-l10 已述），它不参与任何同步或线性化点，只用于粗略统计，用 `Relaxed` 足够且更快。删除的真正线性化点是 `mark_tower` 的 level-0 tag 翻转，与 `len` 的更新顺序无关。

---

### 4.2 物理摘除：逐层 CAS 与 search_bound 兜底

#### 4.2.1 概念说明

赢得 `mark_tower` 之后，节点只是**逻辑上**死了（`is_removed()` 为真、`len` 已减），但它仍然物理地挂在链表里。要真正把它摘下来，需要把每一层「前驱 → n」的指针改成「前驱 → n 的后继」。

`remove` 没有简单地「再搜一遍让读路径去清理」，而是**手动**逐层 CAS 摘除——因为 `search_position` 已经把每层的左右邻接点（`left`/`right`）收集好了（见 u3-l8 的 `Position`），手头就有现成的前驱指针，直接 CAS 通常比重新搜索快。但手动摘除可能因为并发改动而失败，失败时不会死磕，而是调一次 `search_bound` 让读路径的 `help_unlink` 去兜底。

#### 4.2.2 核心流程

```text
for level in (0 .. n.height()).rev() {        // 自顶向下，含 level 0
    succ = n.get_level(level).load(SeqCst).with_tag(0)   // 本层后继（抹掉 tag）
    ok = left[level].get_level(level)
            .compare_exchange(n, succ, SeqCst, SeqCst)   // 把 前驱→n 改成 前驱→succ
    if ok {
        n.decrement(guard)                    // 摘掉一层，退掉这一层的引用
    } else {
        search_bound(Included(key), false)    // 兜底：交给 help_unlink 清理剩余层
        break                                 // 不再手动摘，跳出循环
    }
}
return Some(entry)
```

两个设计要点：

- **自顶向下、含 level 0**：level-0 是链表骨架，必须摘；高层只是加速索引。每一层摘除成功都 `decrement` 一次，对应插入时「塔每长一层 +1」（u3-l10）的对称释放。
- **失败即兜底，不死磕**：某层 CAS 失败说明 `left[level]→n` 已被别人改过（很可能别的线程的 `help_unlink` 已经动过手）。此时不再手动重试，而是 `break` 并调用 `search_bound`——它会沿路把所有还被标记的节点（包括本节点剩余未摘的层）通过 `help_unlink` 清理干净。这是「能摘就摘，摘不干净交给读路径」的协作式清理思想。

#### 4.2.3 源码精读

逐层 CAS 摘除的主循环：

[ src/base.rs:1301-1329 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1301-L1329) —— 对 `0..n.height()` 反向遍历；用 `search.left[level]` 作为前驱，`compare_exchange` 把指向 `n` 的指针换成指向 `succ`（`n` 的本层后继，已 `with_tag(0)`）；成功就 `n.decrement(guard)`，失败就 `search_bound(...)` 兜底并 `break`。

兜底调用的 `search_bound`（u3-l8 已精读）：

[ src/base.rs:833-842 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L833-L842) —— 这里以 `Bound::Included(key)`、`upper_bound=false` 调用，定位第一个 ≥ key 的节点；途中遇到被标记节点即 `help_unlink` 摘除并 `decrement`。

`help_unlink` 本体（兜底的真正执行者）：

[ src/base.rs:759-781 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L759-L781) —— 当 `succ.tag()==1`（说明 `curr` 已逻辑删除）时，CAS 把 `pred→curr` 改成 `pred→succ.with_tag(0)`；成功后 `curr.decrement(guard)`。

#### 4.2.4 代码实践

**目标**：用源码阅读理解「为何失败要兜底而非重试」。

**操作步骤**：

1. 在 [ src/base.rs:1301-1329 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1301-L1329) 的 `compare_exchange` 处，设想一个并发场景：线程 A 正在手动摘除节点 n 的 level-2，同时线程 B 的 `help_unlink`（在读路径中）已经把 n 的 level-2 摘掉了。
2. 思考：此时 A 的 CAS 期望值是「`left[2]` 指向 n」，但 `left[2]` 现在已经指向 n 的后继了，CAS 必然失败。
3. 追踪失败分支：A 调 `search_bound(Included(key), false)`，它重新从 head 走一遍，路过 n 时发现 n 仍被标记（level-0 tag=1），于是在 level-0 触发 `help_unlink`，把 n 彻底从骨架上摘下并 `decrement`。

**需要观察的现象**：失败分支不会重试手动 CAS，而是依赖一次完整搜索收尾。

**预期结果**：无论手动摘到第几层失败，`search_bound` 都能保证 n 最终从 level-0 摘除（因为 level-0 是骨架，搜索必经）。

#### 4.2.5 小练习与答案

**练习 1**：手动摘除是「自顶向下」，能否改成「自底向上（先 level-0）」？为什么作者选了自顶向下？
**答案**：理论上都正确，但自顶向下有一个实际好处：先摘高层能更早让并发搜索少走冤枉路（高层索引失效得更快），并且从 `search.left` 数组的高位往下用，逻辑更顺。此外 level-0 最后摘可以保证「只要还没摘 level-0，节点就还在骨架上、`decrement` 的引用语义稳定」。顺序不影响正确性（最终都会摘 level-0），属于实现取舍。

**练习 2**：兜底用的 `search_bound` 为什么传 `upper_bound=false`？
**答案**：`false` 表示「找第一个 ≥ key 的节点」。删除一个 key 后，我们关心的是「这个 key 还在不在表里、把它的残留节点清掉」，从 head 走到 ≥ key 的位置过程中，`help_unlink` 会清掉沿途所有被标记节点（含 n 本身），正适合收尾。

---

### 4.3 pop_front 与 pop_back：循环式弹出

#### 4.3.1 概念说明

`pop_front` / `pop_back` 分别弹出最小、最大 key 的节点并返回。它们没有重新实现删除逻辑，而是**复用**现成的两个能力：

- `front(guard)` / `back(guard)`：拿到首/尾节点的 `Entry`（Guard 绑定）。
- `Entry::pin()` → `RefEntry`，再 `RefEntry::remove(guard)`。

为什么需要循环？因为「拿到首节点」和「删除它」之间有竞态：可能你刚 `front` 到节点 X，X 就被别的线程删了。这时 `pin()` 可能返回 `None`（X 计数已归零），或 `remove()` 返回 `false`（X 已被标记）。两种情况都不能算成功，于是 `release` 掉这次引用、重新 `front`/`back`，直到要么弹出成功、要么表空（`front`/`back` 返回 `None`）。

#### 4.3.2 核心流程

```text
loop {
    e = front(guard)?            // 表空 → 返回 None（退出函数）
    re = e.pin()                 // Guard-bound Entry → 引用计数 RefEntry；可能 None
    match re {
        None => continue,        // 节点正被回收，重试
        Some(e) =>
            if e.remove(guard) { // 抢删除权
                return Some(e)   // 赢了，返回持有引用的 RefEntry
            } else {
                e.release(guard) // 输了删除权，退引用，重试
            }
    }
}
```

注意 `pop_*` 返回的是 `RefEntry`（base 层）/ `Entry`（map 层），即被弹出节点的引用仍由调用方持有，节点在调用方释放句柄前不会被回收——这与 `remove` 返回 `RefEntry` 的语义一致。

#### 4.3.3 源码精读

`pop_front`：

[ src/base.rs:1340-1352 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1340-L1352) —— `front(guard)?` 表空则返回 `None`；`e.pin()` 转 `RefEntry`，`remove()` 成功则返回，失败则 `release` 后重试。

`pop_back`：

[ src/base.rs:1355-1367 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1355-L1367) —— 结构与 `pop_front` 完全对称，只是用 `back(guard)` 取尾节点。

它复用的 `RefEntry::remove`（句柄视角的删除，比 `SkipList::remove` 简单——因为句柄已经持有引用，无需再 `try_acquire`）：

[ src/base.rs:1694-1710 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1694-L1710) —— 直接 `self.node.mark_tower()` 抢删除权；赢了 `len -= 1` 并 `search_bound` 触发物理摘除，返回 `true`；输了返回 `false`。注意它**不** `decrement`——因为句柄本身的引用由调用方通过 `release` 管理，删除只是改变「是否还在表里」，与「引用计数」是两套账。

`front` / `back` 本身（供对照）：

[ src/base.rs:538-557 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L538-L557) —— `front` 用 `next_node` 从 head 找第一个活节点；`back` 用 `search_bound(Unbounded, true)` 找最后一个 ≤ 无界的节点（即最大 key）。

#### 4.3.4 代码实践

**目标**：用项目自带的 doctest 体验 `pop_back` 的有序弹出。

**操作步骤**：

1. 阅读 map 层 `pop_back` 的 doctest，见 [ src/map.rs:514-517 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L514-L517)，它演示了连续 `pop_back` 会按 key 降序返回。
2. 运行该 doctest：`cargo test --doc pop_back`（或 `cargo test --doc`）。

**需要观察的现象**：连续 `pop_back` 返回 `12 → 7 → 6`，最后 `is_empty()` 为真。

**预期结果**：每次弹出都返回当前最大 key 的句柄，体现 `pop_*` = `back` + `remove` 的组合语义。

#### 4.3.5 小练习与答案

**练习 1**：`pop_front` 里 `e.pin()` 返回 `None` 时为何直接 `continue` 而不 `release`？
**答案**：`pin()` 失败意味着 `try_increment` 失败，即节点的引用计数已经归零、正被回收——此时**没有**成功获取引用，自然没有东西需要 `release`。直接重新 `front` 即可。

**练习 2**：`pop_*` 的循环会不会无限自旋（活锁）？
**答案**：在极端高竞争下理论上有「反复抢不到」的可能，但实际上每次失败都意味着有别的线程成功推进了删除，系统整体在前进（这是 lock-free 的活性保证，区别于 deadlock）。只要持续有 `insert`/成功删除，循环必然终止；表空时 `front`/`back` 返回 `None` 也会立即退出。

---

### 4.4 clear：分批标记与 guard.repin()

#### 4.4.1 概念说明

`clear` 要清空整张表。朴素想法是「从头遍历，每个节点 `mark_tower` + `decrement`」，但这有两个问题：

1. **单次遍历会顺带物理摘除，可能很久**：如果表有 10 万个节点，一次从头走到尾、逐个 `help_unlink`，整个过程都在同一个 epoch「pin」里。
2. **长时间 pin 会拖累全局回收**：crossbeam-epoch 的回收依赖「epoch 推进」——只有当所有线程都离开旧 epoch 后，旧 epoch 的垃圾才能被真正释放。一个线程长时间 pin 在某个 epoch，会**卡住整个 `Collector` 的垃圾回收进度**。base 层的 `SkipList` 用外部传入的 `Collector`，而高层 `SkipMap` 用 `epoch::default_collector()`——**全局默认 Collector**。也就是说，一张 `SkipMap` 的 `clear` 若长时间 pin，会拖慢进程里所有使用默认 Collector 的并发数据结构（别的 `SkipMap`、`crossbeam-epoch` 用户）的垃圾回收。

`clear` 的解法是**分批（`BATCH_SIZE = 100`）+ 每批之间 `guard.repin()`**：

- 每批只标记 100 个节点（`mark_tower`），**不**在 `clear` 内部 `decrement`，物理摘除交给下一批开头那次 `lower_bound` 搜索里的 `help_unlink`。
- 每批结束后 `guard.repin()`：先解 pin、再重新 pin，相当于「跨过一个 epoch 边界」，让 Collector 有机会推进 epoch、回收之前积累的垃圾。

#### 4.4.2 核心流程

```text
const BATCH_SIZE = 100
loop {
    { // 内层作用域，让 entry 在 repin 前释放
        entry = lower_bound(Unbounded)     // 找首个节点；顺带 help_unlink 上一批标记的节点
        for _ in 0..BATCH_SIZE {
            e = entry?                     // 表空 → return（结束 clear）
            next = e.next()                // 先拿后继，再删当前
            if e.node.mark_tower() {       // 只标记，不 decrement
                len -= 1
            }
            entry = next
        }
    }
    guard.repin()                          // 释放并重新 pin，让 epoch 推进
}
```

三个关键细节：

- **`lower_bound(Unbounded)` 一石二鸟**：它既是「找本批起点」，又在搜索途中 `help_unlink` 掉**上一批**标记的节点（完成物理摘除 + `decrement`）。源码注释明说：「Search for the first entry in order to unlink all the preceding entries we have removed.」
- **只标记、不 `decrement`**：`clear` 内部从不调用 `decrement`，全部委托给读路径的 `help_unlink`。这是「协作式清理」的极致体现——`clear` 只负责「宣布死亡」，「收尸」交给搜索。
- **`next` 必须在 `mark_tower` 之前取**：因为标记后，从 `e` 出发的 `next()` 走的是 `next_node`，它会因为 `e` 的出边 tag=1 而回退到一次完整 `search_bound`，行为虽仍正确但更慢；先取好 `next` 游标更直接。

#### 4.4.3 源码精读

`clear` 主体：

[ src/base.rs:1370-1410 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1370-L1410) —— 注意签名是 `&mut Guard`（唯一一处需要可变 Guard 的公开方法，因为 `repin` 需要 `&mut`）；`BATCH_SIZE = 100`；内层块用 `lower_bound(Unbounded)` 取起点，循环 100 次只 `mark_tower` + `len -= 1`；块结束后 `guard.repin()`。

`BATCH_SIZE` 常量与 repin 注释：

[ src/base.rs:1373-1408 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1373-L1408) —— 注释明确解释了动机：分批是为了「不让最后一次搜索把所有节点一次性 unlink，从而避免当前线程被长时间 pin」；`repin` 是为了「不让线程长时间停留在同一个 epoch」。

`help_unlink`（`clear` 标记节点的真正「收尸人」）：

[ src/base.rs:759-781 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L759-L781) —— 上一批标记的节点，其出边 tag=1；下一批 `lower_bound` 搜索路过时，`succ.tag()==1` 触发 `help_unlink`，CAS 摘除并 `curr.decrement(guard)`。

#### 4.4.4 代码实践（本讲主实践）

**目标**：(a) 解释 `BATCH_SIZE` 与 `repin` 的设计动机；(b) 写一个插入 10 万条后 `clear` 的测试，并用「Drop 计数」验证节点最终被回收。

**操作步骤**：

1. **阅读理解**：在 [ src/base.rs:1373-1408 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1373-L1408) 读注释，用自己的话写一段说明：为何分批 + repin。（参考答案见本节末。）
2. **测试编写**：仿照 `tests/base.rs` 的 `drops` 测试（[ tests/base.rs:858-904 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L858-L904)）——用**局部 `Collector`**（而非默认全局），自定义带 `Drop` 计数的 key/value 类型，`clear` 后 `handle.pin().flush()` 强制推进 epoch，再断言计数器。

**示例代码**（标注为「示例代码」，非项目原有）：

```rust
#[test]
fn clear_reclaims_all_nodes() {
    static KEYS: AtomicUsize = AtomicUsize::new(0);

    let collector = epoch::Collector::new();
    let handle = collector.register();
    {
        let guard = &mut handle.pin();
        struct K(i32);
        impl Drop for K { fn drop(&mut self) { KEYS.fetch_add(1, Ordering::SeqCst); } }

        let s = SkipList::new(collector.clone());
        for x in 0..100_000 { s.insert(K(x), x, guard).release(guard); }
        assert_eq!(KEYS.load(Ordering::SeqCst), 0); // 还没回收

        s.clear(guard);
        assert!(s.is_empty());
        assert_eq!(s.len(), 0);
        assert_eq!(KEYS.load(Ordering::SeqCst), 0); // clear 标记了，但 epoch 没推进，未析构
    }
    // 离开作用域 + 多次 flush，强制 epoch 推进并回收
    handle.pin().flush();
    handle.pin().flush();
    assert_eq!(KEYS.load(Ordering::SeqCst), 100_000); // 全部 key 已析构
}
```

**需要观察的现象**：`clear` 刚返回时 `KEYS` 仍是 0（节点被标记、摘除，但 epoch 未推进，`finalize` 未执行）；经过 `flush()` 推进 epoch 后，`KEYS` 跳到 100000，说明全部节点确实被回收。

**预期结果**：印证了「`clear` 只负责标记，真正的析构由 epoch 延迟执行」。

**待本地验证**：`flush()` 推进 epoch 的时机依赖 collector 内部状态，必要时可多次 `flush()` 或在循环中 `flush()` 直到计数达标。

**关于 `BATCH_SIZE` 与 `repin` 的参考解释**：epoch-based reclamation 要求「旧 epoch 的所有 pin 都释放后」才能回收该 epoch 的垃圾。`clear` 若用单个 pin 贯穿 10 万次标记，这整个窗口里当前线程都钉在某一 epoch，导致该 `Collector`（高层 `SkipMap` 用的是进程级默认 Collector）无法推进、垃圾堆积，影响所有并发用户。分批 100 个并把 `Guard` `repin`（解 pin 再重 pin），等于在批次之间「跨过 epoch 边界」，让 Collector 有机会推进并回收前面累积的垃圾，把单次 pin 的时长限制在 100 个节点的处理时间内。

#### 4.4.5 小练习与答案

**练习 1**：`clear` 为什么不直接对每个节点 `decrement`，而要依赖 `help_unlink`？
**答案**：直接 `decrement` 需要先物理摘除（否则节点还被链着、引用语义不对）。复用 `help_unlink` 既摘除又 `decrement`，避免在 `clear` 里重写一套摘除逻辑，符合「协作式清理」的统一模型；而且 `lower_bound` 搜索天然会路过这些节点，顺带清理几乎零额外成本。

**练习 2**：把 `BATCH_SIZE` 调成 1，会对正确性有影响吗？对性能呢？
**答案**：正确性不受影响（每个节点仍被标记、最终被 `help_unlink` 清理）。性能会下降：每标记 1 个节点就要 `repin` 一次（pin/unpin 有原子操作开销），且每批开头的 `lower_bound` 搜索次数大增。`BATCH_SIZE=100` 是「单次 pin 时长」与「repin/搜索开销」之间的折中。

---

### 4.5 Drop for SkipList 与 IntoIter：独占访问下的 unprotected 清理

#### 4.5.1 概念说明

前面所有删除都发生在「并发表还在被别人访问」的前提下，所以必须 pin Guard、必须 `decrement`、必须 `defer_unchecked` 推迟析构。但有两处拥有**独占访问**（exclusive access），可以绕开这些机制直接、立即释放：

1. **`Drop for SkipList`**：Rust 所有权保证 `drop` 运行时没有任何 `&SkipList` 借用活着——表已经被独占。此时没有任何别的线程能碰它。
2. **`IntoIterator::into_iter(self)`**：消费 `self`，之后原变量不可用，同样独占。

正因为独占，这两处都用 `epoch::unprotected()` 加载指针——它**跳过 epoch pin**，直接读。这在并发场景下是 use-after-free 的温床，但独占场景下完全安全。

两个 owning 路径在「如何释放单个节点」上有细微差别：

- `Drop` 和 `IntoIter::Drop` 用 `Node::finalize`（先析构 key/value，再 dealloc），因为节点还**拥有** key/value。
- `IntoIter::next` 用 `Node::dealloc`（只还内存），因为它已经用 `ptr::read` 把 key/value **搬走**交还给调用方了，节点不再拥有它们。

#### 4.5.2 核心流程

**Drop for SkipList**：

```text
node = head.get_level(0).load(Relaxed, unprotected)   // 不 pin
while let Some(n) = node {
    next = n.get_level(0).load(Relaxed, unprotected)   // 先存下一个
    Node::finalize(n)                                   // 析构 key/value + dealloc（立即）
    node = next
}
```

**IntoIter**：`into_iter` 先把整条链从 head 上「拆下来」（head 各层全置 null），只保留 level-0 链头给 `IntoIter`。之后 `next` 逐个吐出 `(K, V)` 并 `dealloc` 节点；若迭代提前放弃，`Drop` 兜底 `finalize` 剩余节点。

```text
// into_iter
front = head.get_level(0).load(Relaxed, unprotected).as_raw()
for level in 0..MAX_HEIGHT: head.get_level(level).store(null, Relaxed)   // 整链脱钩
IntoIter { node: front }

// IntoIter::next
loop {
    if node.is_null(): return None
    key   = ptr::read(&node.key)     // 搬走所有权
    value = ptr::read(&node.value)
    next  = node.get_level(0).load(Relaxed, unprotected)
    Node::dealloc(node)              // 只还内存（key/value 已被搬走）
    node = next
    if next.tag() == 0 { return Some((key, value)) }   // 跳过被标记(已删)节点
}
```

#### 4.5.3 源码精读

`Drop for SkipList`：

[ src/base.rs:1413-1437 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437) —— 从 head 的 level-0 出发，`unprotected()` 加载，逐节点 `Node::finalize`。注释点明：「Unprotected loads are okay because this function is the only one currently using the skip list.」

`into_iter`（拆链）：

[ src/base.rs:1449-1477 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1449-L1477) —— 取出 level-0 首节点裸指针，然后把 head 的**所有层**（`0..MAX_HEIGHT`）都 `store(null)`，等于把整条链从 head 上一次性摘下；返回的 `IntoIter` 持有这条脱钩链。

`IntoIter` 结构与 `Drop`：

[ src/base.rs:2256-2283 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2256-L2283) —— 结构只存当前裸指针；`Drop` 遍历剩余节点，对每个调 `Node::finalize`（这些节点仍拥有 key/value，必须析构）。注释强调「可以不经 defer 直接 finalize，因为给出去的 key/value 引用不会比 SkipList 活得久」。

`IntoIter::next`：

[ src/base.rs:2285-2322 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2285-L2322) —— `ptr::read` 搬走 key/value，`Node::dealloc`（注意是 `dealloc` 而非 `finalize`），跳过 `next.tag()!=0` 的被标记节点。

对照 `finalize` 与 `dealloc` 的区别：

[ src/base.rs:251-262 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262) —— `finalize` 先 `drop_in_place(key)`、`drop_in_place(value)` 再 `dealloc`。
[ src/base.rs:186-195 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L186-L195) —— `dealloc` 只按布局归还内存，**不运行析构**。

#### 4.5.4 代码实践

**目标**：验证 `into_iter` 会消费表并析构所有节点。

**操作步骤**：

1. 阅读 `tests/base.rs::drops`（[ tests/base.rs:858-904 ](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L858-L904)）：它用 `drop(s)` 走 `Drop for SkipList` 路径，`flush()` 后断言 key/value 析构计数。
2. 仿写一个 `into_iter` 版本：插入若干带 Drop 计数的 key，然后用 `for (k, v) in s { ... }` 消费它（注意 base 层 `SkipList` 的 `into_iter` 需要 `K: Send + 'static` 等约束已满足），观察计数。

**需要观察的现象**：迭代过程中每个 `(K, V)` 被取出时，对应节点立即 `dealloc`；若中途 `break`，剩余节点由 `IntoIter::Drop` 的 `finalize` 析构。

**预期结果**：无论是否完整迭代，所有节点的 key/value 最终都被析构，计数与插入数一致。

**待本地验证**：base 层 `IntoIter` 直接产出 `(K,V)`，对带自定义 `Drop` 的 key 类型需自行确认所有权转移链路。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `Drop for SkipList` 能用 `Ordering::Relaxed` 加载，而并发删除路径要用 `SeqCst`？
**答案**：`Drop` 拥有独占访问，没有别的线程并发写这些指针，不存在「同步可见性」需求，`Relaxed` 足够。并发路径里多个线程同时读写指针并依赖其顺序来保证正确性（如 `mark_tower` 的胜负判定），因此需要更强的序。

**练习 2**：`IntoIter::next` 用 `dealloc`，而 `IntoIter::Drop` 用 `finalize`，为什么不能统一？
**答案**：`next` 在 `dealloc` 之前已经用 `ptr::read` 把 key/value 的所有权移交给返回的 `(K, V)`，节点内存里那些字段已是「搬空的壳」，不能再 `drop_in_place`（会重复释放/二次析构），所以只 `dealloc` 内存。`Drop` 路径的剩余节点从未被 `ptr::read`，仍完整拥有 key/value，必须 `finalize` 析构它们。

---

## 5. 综合实践

把本讲的四块串起来：**插入 → 选择性 remove → clear → 验证回收**。

**任务**：写一个测试，用局部 `Collector` 和带 Drop 计数的 key 类型：

1. 向 `SkipList` 插入 1000 个 key（`0..1000`）。
2. 多线程并发删除其中 key 为偶数的 500 个（每个线程负责一段区间，用 `remove`），断言成功删除次数 = 500。
3. 调 `clear` 清掉剩余 500 个。
4. `flush()` 推进 epoch 后，断言 key 的析构计数 = 1000（即 remove 删的 500 + clear 清的 500 全部回收）。
5. 用文字说明：第 2 步里 `remove` 的两步协议如何保证「每个偶数 key 恰被删一次」，第 3 步 `clear` 的分批 + repin 如何避免拖累 epoch。

**验收标准**：

- 删除成功次数恰为 500（无重复删、无遗漏）。
- 最终析构计数恰为 1000。
- 能用本讲术语（线性化点、协作式清理、独占访问、epoch 推进）解释每一步。

**提示**：

- 并发 `remove` 时，每个线程持自己的 `epoch::pin()` Guard；返回的 `RefEntry` 要 `.release(guard)`，否则引用不归零、影响后续析构计数。
- `clear` 需要 `&mut Guard`：`let guard = &mut handle.pin();`
- 析构计数要在多次 `handle.pin().flush()` 之后才稳定。

## 6. 本讲小结

- `remove` 是**两步协议**：先 `try_acquire` 抢引用计数（保证返回的节点活着），再 `mark_tower` 抢删除权（以 level-0 tag 0→1 为线性化点，恰一赢家）；输掉删除权必须 `decrement` 退计数，否则泄漏。
- 赢得删除权后，`remove` 用 `search_position` 留下的 `left` 数组**手动逐层 CAS 摘除**（每层成功 `decrement` 一次）；某层失败不重试，直接 `search_bound` 让 `help_unlink` 兜底——典型的协作式清理。
- `pop_front` / `pop_back` = `front`/`back` + `pin` + `RefEntry::remove` 的循环；处理「拿到节点却被并发删了」的竞态靠重试。
- `clear` **只标记不 decrement**，物理摘除委托给下一批 `lower_bound` 中的 `help_unlink`；用 `BATCH_SIZE=100` 分批并在批间 `guard.repin()`，避免长时间 pin 卡住（默认全局）Collector 的 epoch 推进。
- `Drop for SkipList` 和 `IntoIter` 拥有**独占访问**，故能用 `epoch::unprotected()` + `Relaxed` 直接、立即释放整条链；`finalize`（析构+回收）用于仍拥有 key/value 的节点，`dealloc`（仅回收）用于已被 `ptr::read` 搬空的节点。

## 7. 下一步学习建议

本讲完结了第三单元（核心算法主链路）。至此你已经读透了 base 层的读、插、删三大链路。建议：

1. **进入第四单元**：先读 u4-l12（`Entry` 与 `RefEntry` 的双生命周期），理解本讲反复出现的 `try_acquire`/`pin`/`release` 在句柄层面的完整图景，以及 `let guard = &*(guard as *const _)` 这个生命周期重绑 hack。
2. **再读 u4-l13**（五种迭代器），其中 `IntoIter` 的细节本讲已铺垫，可与 `Iter`/`RefIter`/`Range`/`RefRange` 对比「Guard 绑定 vs 引用计数」两类迭代器的代价。
3. **u5-l17**（内存序分析）会把本讲里散见的 `Relaxed`/`Release`/`Acquire`/`SeqCst` 选择系统化，建议届时回看本讲的 `mark_tower`（`SeqCst`）、`len`（`Relaxed`）、`decrement`（`Release`+`Acquire fence`）三处作为案例。
4. 若想动手验证，可先用 `cargo +nightly miri test` 跑本讲提到的 `drops`/`clear` 测试，确认这些 `unsafe` 块（尤其 `unprotected()` 与 `ptr::read`）无未定义行为。
