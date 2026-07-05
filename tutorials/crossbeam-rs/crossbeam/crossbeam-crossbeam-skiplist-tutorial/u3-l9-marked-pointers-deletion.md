# 标记指针与逻辑删除

## 1. 本讲目标

本讲承接 [u3-l8 搜索算法](u3-l8-search-algorithms.md)，回答无锁跳表删除的核心问题：**多个线程同时想删同一个 key，怎么在不加锁、不破坏遍历中读线程的前提下，安全地删掉一个节点？**

读完本讲你应该能够：

1. 说清楚「逻辑删除（mark）」和「物理摘除（unlink）」为什么必须分成两步。
2. 解释 `mark_tower` 为什么**自顶向下**标记，以及为什么**只有 level 0 的 tag** 决定谁赢得删除权。
3. 看懂搜索路径里对 `tag == 1` 的两类处理（`curr.tag()==1` 重启、`succ.tag()==1` 协助 `help_unlink`）。
4. 自己写一个并发删除测试，断言多线程同时删同一个 key 时**恰好只有一个线程赢得删除权**。

## 2. 前置知识

在进入本讲前，你需要先建立以下直觉（这些都在前几讲讲过，这里只做一句话回顾）：

- **跳表的塔（tower）**：每个节点除了存 key/value，还带一串变长的「下一节点」原子指针，长度就是它的高度。level 0 是每个节点都有的「脊椎」，更高层只是为了加速查找的「快捷通道」。（见 [u2-l5 Node 与 Tower 的内存布局](u2-l5-node-and-tower-layout.md)）
- **指针里藏着 tag 位**：`crossbeam-epoch` 的 `Shared<'g, Node>` 把节点指针的低位（因为指针按 ≥4 字节对齐，低位空闲）拿来当「标签位」用，最低位为 1 就代表「被删除」。`tag()` 读标签、`with_tag(0)` 清标签、`fetch_or(1)` 置位。
- **引用计数 + epoch 回收**：被物理摘除的节点不能立刻释放，因为别的线程可能正拿着它的指针在遍历。要先 `decrement` 减引用计数，归零后再交给 epoch 延迟回收（`finalize`）。（见 [u2-l6 epoch 内存回收与引用计数](u2-l6-epoch-gc-and-refcount.md)）
- **搜索的两条重启路径**：`search_position` / `search_bound` / `next_node` 都用一个 `'search: loop` 外层循环，遇到「立足点的前驱已死」就整体重启。（见 [u3-l8](u3-l8-search-algorithms.md)）

> 术语提示：本讲反复出现 **Harris/Michael 风格删除**。它指的是 Timothy Harris（2001）提出、Maged Michael（2004）改进的无锁链表删除算法：用一个标记位（mark bit）把「逻辑删除」和「物理摘除」解耦，从而在无锁下仍能安全、可线性化地删除。跳表本质上是多层链表，所以这套算法被搬到每一层。

## 3. 本讲源码地图

本讲只看一个文件 [src/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs)，但涉及其中三个层次的代码：

| 代码位置 | 作用 | 本讲角色 |
| --- | --- | --- |
| `NodeRef::mark_tower` / `is_removed` | 置标记位 / 查标记位 | 删除的「原子开关」 |
| `search_position` / `search_bound` / `next_node` 里对 `tag()==1` 的分支 | 读路径如何感知并协助删除 | 读删协作 |
| `help_unlink` | 把已标记节点从某一层 CAS 摘除 | 物理摘除的协助函数 |
| `SkipList::remove` / `Entry::remove` / `RefEntry::remove` | 三种删除入口 | 两步协议的编排 |

高层 `SkipMap::remove` / `SkipSet::remove` 只是这些 base 层方法的薄封装（见 [u4-l14](u4-l14-skipmap-wrapper.md)），本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 标记指针：用 tag 位表示「逻辑删除」

#### 4.1.1 概念说明

先想一个朴素问题：线程 A 想删节点 `n`，能不能直接把前驱 `pred` 的指针从 `n` CAS 成 `n` 的后继 `succ`，然后立刻释放 `n`？

**不能**，因为线程 B 可能此刻正拿着 `n` 的指针在遍历（比如迭代器刚走到 `n`），`n` 一旦被释放就是 use-after-free。无锁数据结构里，**「从链表里摘下来」和「真正释放内存」必须分开**。

crossbeam-skiplist 的做法是 Harris/Michael 风格的**两步删除**：

1. **逻辑删除（mark）**：在节点 `n` 自己所有「向前的指针」上打一个标记位（tag bit），宣告「这个节点已死，但还在链表里」。这一步是单个原子操作，瞬间完成。
2. **物理摘除（unlink）**：随后某个线程（可以是删除者本人，也可以是任何路过的读线程）把 `n` 从链表里 CAS 摘掉，并减引用计数，交给 epoch 延迟回收。

关键点：**tag 位打在「`n` 指向下一个节点」的指针上，描述的却是「`n` 自己」的死活**。也就是说，指针 `n.tower[level]` 的 tag==1，含义是「源头节点 `n` 已被逻辑删除」。这是一个反直觉但很重要的设计——后面 4.2 会看到，正是因为 tag 打在「出边」上，读路径才能用统一的方式检测到「我正立足的前驱是不是死了」。

为什么 tag 位足以表示删除？因为 `crossbeam-epoch` 的 `Shared` 指针天然带几位空闲低位（指针按至少 `usize` 的次幂对齐），最低位当 mark 位，`fetch_or(1)` 就能原子地把一个**还在正常工作的出边指针**变成**带死亡标记的指针**，而指针指向的地址不变（其他线程的遍历不会因此跳错地方，只会多读到一个 tag）。

#### 4.1.2 核心流程

`mark_tower` 的逻辑用伪代码描述：

```
fn mark_tower(self) -> bool:              # 返回值 = 我是否赢得了删除权
    height = self.height()                # 这个节点的塔高
    for level in (0..height).rev():       # 从最高层 → level 0，自顶向下
        old = self.tower[level].fetch_or(1, SeqCst)   # 置 tag 位，拿到旧值
        if level == 0 and old.tag() == 1: # level 0 之前就已被标记
            return False                  # → 有人先我一步删了，我输了
    return True                           # level 0 的 0→1 翻转是我做的，我赢了
```

要点拆解：

- **自顶向下**：先把高层指针都标记，最后才标记 level 0。这个顺序的正确性意义见 4.1.4 的实践题，这里先记结论：它保证「level 0 被标记的瞬间，整个塔已经全部冻结」，不会有「半死半活」的中间态被并发插入利用。
- **level 0 是仲裁点**：`fetch_or` 返回旧值，只有当旧值的 tag 是 0、被本线程翻成 1，本线程才算赢。所以 N 个线程并发 `mark_tower` 同一个节点，**恰好只有一个**会观察到 level 0 旧 tag==0 并把它翻成 1——这就是删除操作的**线性化点（linearization point）**。
- **`is_removed` 只看 level 0**：因为 level 0 的 tag 就是「是否已删除」的权威来源。高层有没有标记只是为了让并发插入失败、协助清理，不作为判活依据。

用「真值表」总结 level 0 的仲裁语义（`fetch_or(1)` 的返回值 old.tag() 与胜负的关系）：

| 层级 | `old.tag()` 观察值 | 含义 | `mark_tower` 返回 |
| --- | --- | --- | --- |
| level > 0 | 0 → 翻成 1 | 高层冻结成功，继续 | 继续循环 |
| level > 0 | 已经是 1 | 高层已被别人标过（无所谓） | 继续循环 |
| level 0 | 0 → 翻成 1 | **我是赢家**（线性化点） | `true` |
| level 0 | 已经是 1 | **我输了**（别人先翻的） | `false` |

#### 4.1.3 源码精读

下面是 `mark_tower` 的真实代码，注意它用 `epoch::unprotected()`——因为这里**只读 tag 位、不读指针指向的内容**，所以不需要 Guard 保护（`unprotected` 的安全性见 [u5-l17 内存序分析](u5-l17-memory-ordering.md)）：

[base.rs:326-348 — `mark_tower` 自顶向下逐层 `fetch_or(1)`，以 level0 旧 tag 决定胜负](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L326-L348)

```rust
fn mark_tower(self) -> bool {
    let height = self.height();
    for level in (0..height).rev() {
        let tag = unsafe {
            self.get_level(level)
                .fetch_or(1, Ordering::SeqCst, epoch::unprotected())
                .tag()
        };
        // 关键仲裁：只有 level0 的旧 tag 才决定胜负
        if level == 0 && tag == 1 {
            return false;
        }
    }
    true
}
```

与之配对的 `is_removed`，注意它**只 load level 0**、用 `Relaxed`（tag 位读取对排序要求不高，且只是个「尽力而为」的观测）：

[base.rs:350-361 — `is_removed` 只查 level0 的 tag](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L350-L361)

```rust
fn is_removed(self) -> bool {
    let tag = unsafe {
        self.get_level(0)
            .load(Ordering::Relaxed, epoch::unprotected())
            .tag()
    };
    tag == 1
}
```

`Entry::is_removed` 和 `RefEntry::is_removed` 都只是转调上面这个 `NodeRef::is_removed`：

[base.rs:1491-1494 — `Entry::is_removed` 转调](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1491-L1494)　[base.rs:1633-1636 — `RefEntry::is_removed` 转调](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1633-L1636)

最后注意一个常被忽略的事实：`fetch_or(1)` 之所以不会破坏指针地址，是因为 `crossbeam-epoch` 的 `Shared` 把 tag 存在指针**空闲的低位**（节点按 `usize` 对齐，低 2~3 位未用），置位只动这几位、不动真实地址。

#### 4.1.4 代码实践（源码阅读 + 文字论证）

这是本讲实践任务的第一部分（文字论证题）。

**实践目标**：用自己的话论证「为什么 `mark_tower` 必须自顶向下、且只有 level 0 决定胜负」对正确性的意义。

**操作步骤**：

1. 打开 [base.rs:326-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L326-L348)，对照下面的提示写出你的论证。
2. 阅读插入路径 [base.rs:1144-1151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1144-L1151) 和 [base.rs:1179-1188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1179-L1188)：插入新节点建塔时，会先 `load` 自己的出边指针，如果 `tag()==1` 就 `break 'build` 停止建塔；`compare_exchange` 也会因为指针被改（标记）而失败。

**需要思考的几个问题**：

- 如果改成**自底向上**（先标 level 0，再标高层），在标 level 0 之后、标高层之前的窗口里，另一个线程能否在该节点的高层成功 splice 进一个新节点？这会带来什么麻烦？
- 为什么不能「标到哪层算哪层、任何一层先标记的赢」？换句话说，如果用「最高层 tag」当判据，会遇到什么问题（提示：高层是可选的快捷通道，不是每个节点都有）？

**预期结果**：你应该能得出类似下面的结论（参考答案见 4.1.5）。

**待本地验证**：本题为推理题，无需运行命令；建议把你的论证与 4.1.5 的参考答案对照。

#### 4.1.5 小练习与答案

**练习 1**：`mark_tower` 里对 level > 0 的层，如果 `old.tag()` 已经是 1（别人标过），函数为什么**不**直接 `return false`？

**参考答案**：因为高层是不是已被标记，并不决定「这个节点现在归谁删」。可能存在这样的交错：线程 A 先标了某高层，然后被挂起；线程 B 从 level 0 开始正常标记并赢得删除权。只要 level 0 还没被标，节点在逻辑上就还活着。高层 tag==1 只是说明「这一层已冻结」，本线程仍可继续向下，最终到 level 0 用旧 tag 做唯一仲裁。

**练习 2**：`is_removed` 用 `Ordering::Relaxed` 读取 level 0 的 tag，会不会读到「过时」的值？这有问题吗？

**参考答案**：可能读到「刚被标记但本线程还没看到」的旧值（即返回 `false` 而节点其实已被标）。这是允许的竞态：`is_removed` 只是「尽力而为」的观测，文档明确「单操作原子、多操作非原子」。只要你不再把它和后续操作组合成「检查—然后行动」的逻辑，就不会破坏内存安全。真正的删除安全性由 `mark_tower` 的 CAS（`fetch_or`）和 epoch 回收保证，不依赖 `is_removed` 的精确性。

---

### 4.2 读路径如何感知并协助删除：`tag==1` 的两类处理

#### 4.2.1 概念说明

逻辑删除的 tag 打上去之后，链表里还留着这个「半死」的节点。它必须被某个线程摘掉。crossbeam-skiplist 采用**协作式清理**：任何读线程在搜索时撞到被标记的节点，都有义务顺手帮一把（`help_unlink`）。这样即使删除者本人还没来得及物理摘除，进度也能推进——这是无锁算法「锁无关（lock-free）」活性（保证系统整体前进）的关键。

回忆 4.1.1 的关键设定：**tag 打在「出边」上，描述源头节点**。这导致读路径要区分**两类** `tag==1`：

1. **`curr.tag() == 1`**：`curr` 是从前驱 `pred` 的指针里 load 出来的，它的 tag==1 说明**前驱 `pred` 自己被逻辑删除了**。既然立足点（前驱）都死了，它给的指针不可信，必须整体 `continue 'search` 重启。
2. **`succ.tag() == 1`**：`succ` 是从当前节点 `c`（活着）的出边 load 出来的，tag==1 说明**当前节点 `c` 被删除了**。这时可以尝试帮它从这一层摘除（`help_unlink`），摘成功就继续前进；摘失败（别人也在摘）就重启。

#### 4.2.2 核心流程

`help_unlink(pred, curr, succ)` 只做一件事：把 `pred` 的指针从 `curr` CAS 成 `succ.with_tag(0)`（去掉 tag 的后继）：

```
fn help_unlink(pred, curr, succ) -> Option<succ_clean>:
    r = pred.compare_exchange(curr, succ.with_tag(0), Release, Relaxed)
    if r.is_ok():
        curr.decrement(guard)        # 摘掉后减引用计数，归零则 epoch 回收
        return Some(succ.with_tag(0))
    else:
        return None                  # 别人先 CAS 了，我放弃
```

搜索主循环对 `tag` 的处理（`search_position` 和 `search_bound` 形式相同）：

```
'search: loop {
    pred = head; level = max_height
    while level >= 1 {
        level -= 1
        curr = pred.tower[level].load_consume(guard)
        if curr.tag() == 1 { continue 'search }     # 情况1：前驱死了 → 重启

        while let Some(c) = from_shared(curr) {
            succ = c.tower[level].load_consume(guard)
            if succ.tag() == 1 {                    # 情况2：c 被删 → 协助摘除
                if let Some(next) = help_unlink(pred.tower[level], c, succ) {
                    curr = next; continue           # 摘成功，继续这一层
                } else {
                    continue 'search                 # 摘失败，重启
                }
            }
            # ... 正常的比较/前进逻辑 ...
            pred = c; curr = succ
        }
    }
}
```

三条搜索函数（`search_position` / `search_bound` / `next_node`）对 `tag` 的处理高度同构，差别只在「找到后做什么」：

| 函数 | `curr.tag()==1`（前驱死） | `succ.tag()==1`（当前死） | 找到目标后 |
| --- | --- | --- | --- |
| `search_position` | `continue 'search` | `help_unlink`，成功继续 / 失败重启 | 记入 `Position.left/right` |
| `search_bound` | `continue 'search` | `help_unlink`，成功继续 / 失败重启 | 按 `upper_bound` 更新 `result` |
| `next_node` | 退化为 `search_bound` 重启 | `help_unlink`，成功继续 / 失败重启 | 返回第一个活节点 |

#### 4.2.3 源码精读

`help_unlink` 本体——注意它标记为 `#[cold]`，因为正常情况下撞到死节点是少数情况：

[base.rs:759-781 — `help_unlink`：CAS 把已标记节点从本层摘除并减引用计数](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L759-L781)

`search_bound` 里的两类 `tag` 处理（情况 1 在 871-875，情况 2 在 882-893）：

[base.rs:871-893 — `search_bound` 对 `curr.tag()` 与 `succ.tag()` 的处理](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L871-L893)

`search_position` 里完全同构的两段（情况 1 在 960-964，情况 2 在 971-982）：

[base.rs:960-982 — `search_position` 对 `curr.tag()` 与 `succ.tag()` 的处理](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L960-L982)

`next_node` 的特殊之处：它在 level 0 上走，如果 `pred` 的直接后继 `curr.tag()==1`（前驱 `pred` 死了），它直接回退调用 `search_bound` 重启（800 行）；正常前进时同样 `help_unlink`（806-816 行）：

[base.rs:787-823 — `next_node` 的 level0 前进与 tag 处理](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L787-L823)

最后，插入路径也复用同一套语义：建塔过程中若发现自己的出边被标记（`next.tag()==1`，1149 行），说明本节点已被并发删除，立即 `break 'build` 停止建塔——这正解释了 4.1.4 留的思考题：

[base.rs:1146-1151 — 插入建塔时撞到自己被标记则停止](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1146-L1151)

#### 4.2.4 代码实践（源码阅读型：标注重启条件）

**实践目标**：把 `search_position` 里所有会触发 `continue 'search`（外层重启）的条件找全，理解每一条为什么必须重启而不是「就地继续」。

**操作步骤**：

1. 打开 [base.rs:922-1008（`search_position` 全文）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L922-L1008)。
2. 在本地用编辑器标注（或写在纸上）以下两类条件，每条注明「为什么不能就地继续」：
   - 962-964 行：`curr.tag() == 1`；
   - 977-981 行：`help_unlink` 返回 `None`（CAS 失败）。

**需要观察的现象**：你会发现，**所有重启都源于「我立足的前驱或当前链表结构已经变了」**。因为 `search_position` 要把每一层的左右邻接点（`left[level]` / `right[level]`）一次性、一致地收集起来给 `insert`/`remove` 做 CAS 用，只要中途发现任何一层的基础（前驱指针）不可信，之前收集的 `left/right` 就全部作废，必须从 head 重新来。

**预期结果**：得到「两类重启条件 + 各自原因」的清单。结论应与 4.2.1 的论述一致。

**待本地验证**：阅读型实践，无需运行；可对照本节正文自检。

#### 4.2.5 小练习与答案

**练习 1**：`help_unlink` 用 `compare_exchange(..., Ordering::Release, Ordering::Relaxed)`，为什么**成功**分支用 `Release`？失败分支用 `Relaxed` 就够？

**参考答案**：成功分支把 `curr` 从链表「发布」式地摘掉，并紧接着 `curr.decrement`（可能触发 epoch 回收），需要 `Release` 保证「之前对 `curr` 的所有写入」在对 `curr` 的回收前对其他线程可见（配合 `decrement` 里的 `fence(Acquire)`，见 [u2-l6](u2-l6-epoch-gc-and-refcount.md)）。失败分支说明 CAS 没改成任何东西、`pred` 的指针还是别人改后的值，没有发布新内容，`Relaxed` 足矣。

**练习 2**：在 `search_bound` 中，如果 `help_unlink` 返回 `None`（CAS 失败），为什么不重试 `help_unlink` 而是 `continue 'search`？

**参考答案**：CAS 失败说明 `pred.tower[level]` 已经不是 `curr` 了——别的线程要么已经把 `curr` 摘掉、要么插入了新节点、要么 `pred` 自己被删了。此时我们手里的 `pred`/`curr`/`succ` 三元组已经过期，继续在本层原地操作没有意义，最稳妥的是从 head 整体重启搜索。

---

### 4.3 删除的两步协议：抢占引用计数 → 抢占删除权 → 物理摘除

#### 4.3.1 概念说明

把 4.1 的「打标记」和 4.2 的「读路径协助」组合起来，就得到完整的删除流程。但还差一环：`SkipList::remove(key)` 想把被删节点**作为 `RefEntry` 返回给调用者**，这意味着返回前必须先给节点**增加一个引用计数**，否则它可能在返回途中就被别人删完、回收掉。

所以删除是一个**两步协议**：

1. **抢占引用计数**：`search_position` 找到节点后，`RefEntry::try_acquire` 用 CAS 给引用计数 +1。如果节点正在被回收（计数已归零），CAS 失败，重新搜索。
2. **抢占删除权**：`mark_tower` 给整座塔打标记，返回 `true` 表示「我赢了 level 0 的 0→1 翻转」。返回 `false` 表示别人先删了——此时把刚加的引用计数减回去（`decrement`），返回 `None`。

注意这两步的对称美：引用计数竞争失败要重试，删除权竞争失败要认输。两个 CAS 各司其职——前者保证「返回的节点不会被回收」，后者保证「恰好一个线程把节点标死」。

`Entry::remove` / `RefEntry::remove`（句柄上的删除）少了第一步，因为句柄本身已经持有引用计数，直接 `mark_tower` 即可，返回 `bool` 表示是否赢得删除权。

#### 4.3.2 核心流程

`SkipList::remove` 的两步协议伪代码：

```
fn remove(key) -> Option<RefEntry>:
    loop:
        search = search_position(key)
        n = search.found?              # 没找到 → 返回 None
        entry = try_acquire(n)?        # 第1步：抢引用计数；失败则 continue 重搜
        if n.mark_tower():             # 第2步：抢删除权（线性化点）
            len.fetch_sub(1)           # 赢了：减计数
            # 物理摘除：逐层 CAS，用 search.left[level] 把 n 换成 n 的后继
            for level in (0..n.height).rev():
                succ = n.tower[level].load().with_tag(0)
                if search.left[level].tower[level].CAS(n, succ).is_ok():
                    n.decrement()      # 摘掉一层，减一次引用
                else:
                    search_bound(...)  # CAS 失败，让读路径兜底清理，break
            return Some(entry)
        else:
            n.decrement()              # 输了：退回引用计数
            return None
```

物理摘除的兜底设计值得注意：删除者**尽量**用自己刚搜到的 `search.left/right` 手动逐层 CAS 摘除（快路径），但任何一层 CAS 失败（说明有并发插入/删除改了结构），就不再纠缠，直接调一次 `search_bound`——这次搜索会撞到被标记的 `n` 并触发 4.2 的 `help_unlink`，把剩下的层清干净（慢路径兜底）。这就是「删除者能摘就摘，摘不干净交给读路径」的分工。

`Entry::remove` / `RefEntry::remove` 更简单（句柄已持有引用，无需 `try_acquire`）：

```
fn remove(&self) -> bool:
    if self.node.mark_tower():         # 抢删除权
        len.fetch_sub(1)
        search_bound(Included(key), false)  # 触发 help_unlink 完成物理摘除
        true
    else:
        false
```

#### 4.3.3 源码精读

`SkipList::remove` 的两步协议（`try_acquire` 在 1291-1294，`mark_tower` 在 1297，逐层 CAS 摘除在 1304-1328，输者退计数在 1332）：

[base.rs:1269-1337 — `SkipList::remove`：try_acquire 抢引用 → mark_tower 抢删除权 → 逐层摘除 / 兜底 search_bound](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1269-L1337)

句柄删除——`Entry::remove` 返回 `bool`（赢得与否），赢了之后调一次 `search_bound` 让读路径帮忙摘除：

[base.rs:1530-1545 — `Entry::remove`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1530-L1545)　[base.rs:1694-1709 — `RefEntry::remove`（结构相同）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1694-L1709)

`try_acquire` 内部转调 `Node::try_increment`（CAS 加计数，拒绝已归零节点以防 double-free，详见 [u2-l6](u2-l6-epoch-gc-and-refcount.md)）：

[base.rs:222-249 — `try_increment`：只有计数非零才允许 +1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)

最后对照一个现成的串行测试，看 `entry.remove()` 的布尔语义和 `is_removed` 的配合：

[tests/map.rs:304-327 — `entry_remove` 测试：remove() 返回 true 后 is_removed() 才为 true](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L304-L327)

#### 4.3.4 代码实践（可运行：并发删除恰有一个赢家）

这是本讲实践任务的第二部分（可运行的并发测试）。

**实践目标**：用一个测试证明「N 个线程并发删同一个 key，恰好只有一个线程的删除返回成功」——即 `mark_tower` 的 level 0 仲裁确实把多个并发删除线性化成了一次。

**操作步骤**：

1. 在仓库根目录的 `tests/` 下新建一个测试文件，例如 `tests/u3l9_concurrent_remove.rs`（**注意**：本讲约定不修改源码与现有测试，这里只是「示例代码」，你应在自己的副本里运行）：

   ```rust
   // 示例代码：tests/u3l9_concurrent_remove.rs
   use std::sync::atomic::{AtomicUsize, Ordering};
   use std::sync::{Arc, Barrier};
   use std::iter;

   use crossbeam_skiplist::SkipMap;
   use crossbeam_utils::thread;

   #[test]
   fn concurrent_remove_exactly_one_wins() {
       const THREADS: usize = 4;
       for _ in 0..100 {
           // 预置一个 key=1 的节点
           let map: SkipMap<i32, i32> = iter::once((1, 1)).collect();
           let map = Arc::new(map);
           let barrier = Barrier::new(THREADS);
           let winners = Arc::new(AtomicUsize::new(0));

           thread::scope(|s| {
               for _ in 0..THREADS {
                   let map = map.clone();
                   let winners = winners.clone();
                   s.spawn(move |_| {
                       barrier.wait(); // 尽量让各线程同时开删
                       // remove 返回 Some <=> 这次 mark_tower 赢得 level0 标记
                       if map.remove(&1).is_some() {
                           winners.fetch_add(1, Ordering::Relaxed);
                       }
                   });
               }
           })
           .unwrap();

           // 断言：4 个并发删除里，恰好 1 个赢得删除权
           assert_eq!(winners.load(Ordering::Relaxed), 1);
           assert!(map.is_empty());
       }
   }
   ```

2. 运行：

   ```bash
   cargo test --test u3l9_concurrent_remove -- --nocapture
   ```

**需要观察的现象**：

- 测试稳定通过，`winners` 恒为 1，说明无论线程如何交错，`mark_tower` 的 level 0 `fetch_or` 总是恰好把一个线程的 0→1 翻转视为「赢」。
- 外层 `for _ in 0..100` 是为了把并发交错的概率放大，模仿现有 `concurrent_remove` 测试（[tests/map.rs:174-190](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L174-L190)）的写法。

**预期结果**：100 轮全部 `winners == 1`、`map.is_empty() == true`。

**变体（推荐）**：把线程数从 4 改成 8、16，把 key 从单值改成「8 个 key、每 key 4 线程竞争」，统计每个 key 恰好有 1 个赢家。如果某轮出现 `winners == 0` 或 `winners > 1`，那才是真正的 bug（但按算法保证不应发生）。

**待本地验证**：作者未在本地实际运行上述命令；上述测试模仿现有 `concurrent_remove` / `concurrent_insert` 的结构，逻辑上断言应成立，请你本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`SkipList::remove` 在 `mark_tower` 返回 `false`（输了）时，为什么要 `n.decrement(guard)` 再返回 `None`？不减会怎样？

**参考答案**：因为在 `mark_tower` 之前调用了 `try_acquire` 给节点加了 1 个引用计数（为了能把它作为 `RefEntry` 返回）。既然输了删除权，这个引用就没用了，必须减回去，否则节点的引用计数永远多 1，最终 `finalize`（epoch 回收）永远不会被触发，造成内存泄漏。`decrement` 减到 0 才会安排回收，这里只是把「多加的那一份」还掉。

**练习 2**：`SkipList::remove` 在逐层 CAS 摘除时，为什么某一层 CAS 失败要 `break` 并调 `search_bound`，而不是重试那一层？

**参考答案**：某一层 `pred.tower[level].CAS(n, succ)` 失败，说明 `pred.tower[level]` 已经不指向 `n` 了——结构已被并发改动。重试本层没有可靠的 `pred`/`succ`；而调用 `search_bound` 会重新遍历，撞到仍未摘除的、被标记的 `n`，由读路径的 `help_unlink` 把剩余各层统一清掉。把「难缠的收尾」交给已经验证过正确性的 `help_unlink`，比删除者自己手写重试更简单、更不易错。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「单节点删除全过程追踪」任务：

**任务**：构造一个 `SkipMap<i32, Arc<Tracker>>`（`Tracker` 内部包一个 `Arc<AtomicUsize>` 计数器，`Drop` 时自减），完成下列追踪：

1. 插入 1 个键值对 `(7, Arc::new(Tracker::new(counter)))`，此时 `counter == 1`。
2. 用 `get(&7)` 拿到句柄 `e`（不 drop），再在另一个线程里调用 `map.remove(&7)`。
3. 主线程观察：
   - `remove` 返回 `Some`（删除权被该线程赢得，对应 4.3 的 `mark_tower` 返回 `true`）；
   - 但此时 `counter` **可能仍是 1**——为什么？（提示：`e` 还活着 → 引用计数未归零 → `finalize` 未执行 → `Tracker` 未 drop。这正是 4.1.1「物理摘除 ≠ 立即释放」的体现。）
   - `e.is_removed()` 此时为 `true`（对应 4.1.3 的 `is_removed` 看 level 0 tag）。
4. `drop(e)` 后，主动推进 epoch 回收（可通过多次 `epoch::pin()`/`collect` 或干脆 drop 整个 `map`），再观察 `counter` 变为 0——这对应 [u2-l6](u2-l6-epoch-gc-and-refcount.md) 的延迟回收链路。

**输出**：把每一步的 `counter` 值、`is_removed()` 值、以及对应的源码行号（4.1.3 / 4.3.3 的链接）写成一张时序表，说明「逻辑删除（mark）」和「物理释放（finalize）」之间为什么可能相隔任意长时间。

**待本地验证**：epoch 何时真正回收取决于全局 `Collector`，可能需要多次 pin 或程序结束才能看到 `counter` 归零；若观察不到，可改为 drop 整个 `map` 后退出作用域来强制回收。

## 6. 本讲小结

- **逻辑删除 ≠ 物理摘除**：删除分两步——先在节点自己所有出边指针上 `fetch_or(1)` 打 tag（mark），再由删除者或任何读线程把它从链表 CAS 摘掉（unlink），最后 `decrement` 触发 epoch 延迟回收。
- **tag 打在「出边」、描述「源头节点」**：`c.tower[level]` 的 tag==1 表示 `c` 自己已死。这导致读路径区分两类情况：`curr.tag()==1`（立足的前驱死了，重启）与 `succ.tag()==1`（当前节点死了，`help_unlink` 协助摘除）。
- **`mark_tower` 自顶向下、level 0 仲裁**：自顶向下保证「level 0 标记瞬间整塔已冻结」；level 0 的 0→1 翻转是删除的线性化点，恰好一个线程赢得删除权。
- **`is_removed` 只看 level 0**，且用 `Relaxed`——它是「尽力而为」的观测，删除安全性不依赖它。
- **`SkipList::remove` 是两步协议**：先 `try_acquire` 抢引用计数（保证返回的节点不被回收），再 `mark_tower` 抢删除权；输了要 `decrement` 退计数，否则泄漏。
- **协作式清理保证 lock-free 活性**：删除者能摘就摘，摘不干净交给读路径的 `help_unlink` 兜底，系统整体始终前进。

## 7. 下一步学习建议

- **下一讲 [u3-l10 插入路径](u3-l10-insert-path.md)**：本讲多次提到「插入建塔时撞到被标记的指针就 `break 'build`」，下一讲会完整精读 `insert_internal` 与 `random_height`，看插入如何与逻辑删除交错。
- **[u3-l11 删除与清理](u3-l11-remove-and-clear.md)**：本讲只讲了单节点 `remove`，`pop_front/pop_back/clear`（分批 + `guard.repin()`）和 `Drop` 的批量回收留到下一讲。
- **回看 [u3-l8 搜索算法](u3-l8-search-algorithms.md)**：本讲把 `search_position/search_bound/next_node` 里 `tag` 的分支讲透了，建议回头对照 4.2 的两类处理重读 u3-l8 的「Harris/Michael 逻辑删除」段落，理解会更立体。
- **进阶 [u5-l17 内存序分析](u5-l17-memory-ordering.md)**：本讲刻意回避了 `mark_tower` 为何用 `SeqCst`、`help_unlink` 为何用 `Release`，这些内存序权衡是专家层的内容，留到 u5-l17 系统分析。
