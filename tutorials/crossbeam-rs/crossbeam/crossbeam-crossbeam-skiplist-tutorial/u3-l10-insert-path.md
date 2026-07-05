# 插入路径：insert_internal 与 random_height

> 所属单元：u3 进阶·核心算法主链路 ｜ 前置：u3-l8（搜索算法）、u3-l9（标记指针与逻辑删除）

## 1. 本讲目标

学完本讲，你应当能够：

1. 解释 `random_height` 如何用「xorshift 随机数 + 末尾零个数」生成服从几何分布的塔高，并说明它为何让跳表的期望查找复杂度为 \(O(\log n)\)。
2. 逐段讲清 `insert_internal` 的两阶段插入：先在 level 0 用一次 CAS 把新节点「物理挂入」，再自底向上把塔逐层补齐；以及失败时的重试与回退逻辑。
3. 区分四种插入语义：`insert`、`compare_insert`、`get_or_insert`、`get_or_insert_with`，理解它们如何通过同一个 `replace` 闭包复用 `insert_internal`。

本讲只读 `src/base.rs` 一个文件。它承接 u3-l8 的 `search_position`（查重）与 u3-l9 的 `mark_tower`（删除权抢占），是「读路径 → 删除标记 → 插入」三连的最后一块。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：跳表为什么需要随机高度。**
跳表（skip list）用一个「概率骨架」代替 B 树的再平衡：每个节点随机决定自己的「塔高」，塔越高越稀疏。理想情况下，约一半节点高度为 1、四分之一为 2、八分之一为 3……形成一条从稀疏到密集的多级索引。查找从最高层起步、横向快进，到层末再下降，最终在 level 0 精确定位。只要高度服从正确的概率分布，期望路径长度就是 \(O(\log n)\)，而无需任何旋转/合并。

**直觉二：无锁插入靠 CAS，靠「重试」而不是「加锁」解决竞争。**
两个线程同时插入不同 key，它们各自 CAS 同一个前驱指针只有一个会赢，输的线程不会阻塞等待，而是重新 `search_position` 拿到最新邻接点再试。这正是 lock-free 活性的来源：总有人能推进。

**直觉三：引用计数 = 塔的每一层链接 + 返回的句柄。**
节点 `Node` 把「引用计数」和「高度」压在同一个原子字段 `refs_and_height` 里（见 u2-l5）。新节点初始引用计数为 **2**：一份给 level 0 的链接，一份给要返回给调用者的 `RefEntry` 句柄；之后塔每往上长一层，引用计数 `+1`；每摘除一层 `-1`。归零才回收。这条线索把「插入」和 u2-l6 的内存回收串起来。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|------|----------|------|
| `src/base.rs` | `random_height` | 用 xorshift + `trailing_zeros` 生成几何分布的塔高，并维护 `max_height` 提示 |
| `src/base.rs` | `insert_internal` | 插入主链路：查重 → 分配节点 → level 0 CAS 安装 → 构建高层塔 → 并发删除回退 |
| `src/base.rs` | `insert` / `compare_insert` | 高层插入语义：无条件替换 / 按闭包替换 |
| `src/base.rs` | `get_or_insert` / `get_or_insert_with` | 「不存在才插入」语义，不替换旧值 |

辅助常量与结构（u2-l5 已讲，本讲复用）：

- 高度编码常量 [src/base.rs:23-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L23-L30)：`HEIGHT_BITS=5`、`MAX_HEIGHT=32`、`HEIGHT_MASK=31`。
- 搜索结果 `Position`（含 `found` / `left[]` / `right[]`）见 [src/base.rs:429-440](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L429-L440)。
- 频繁修改的「热数据」`HotData`（`seed`/`len`/`max_height`）见 [src/base.rs:442-453](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L442-L453)。

---

## 4. 核心概念与源码讲解

### 4.1 随机高度 random_height：跳表的「概率骨架」

#### 4.1.1 概念说明

`random_height` 的唯一职责是：为本轮插入的新节点抽一个高度 `height ∈ [1, 32]`。它必须满足两个要求：

1. **概率正确**：高度服从「参数 \(p=1/2\) 的几何分布」，即 \(\Pr(\text{height}=h)\approx 2^{-h}\)。只有这样跳表才具备 \(O(\log n)\) 的期望复杂度。
2. **不要无谓地建高塔**：当跳表还很小（节点很少）时，抽到一个 32 层的高塔纯属浪费，搜索还得从最高层空跑下来。因此需要根据「当前最高塔」把高度往下压一压。

#### 4.1.2 核心流程

```
1. 用 xorshift(13,17,5) 从 hot_data.seed 生成下一个 32 位伪随机数 num
2. height = min(MAX_HEIGHT, num 的末尾连续 0 的个数 + 1)
3. while height >= 4 且 头节点在 level (height-2) 处没有指针:
       height -= 1              # 当前表太小，压低高度
4. 用 CAS 把 hot_data.max_height 抬到 height（只升不降）
5. 返回 height
```

#### 4.1.3 源码精读

xorshift 是 George Marsaglia 提出的一类极快伪随机算法。这里用固定的 `<<13, >>17, <<5` 三次异或移位生成下一个 32 位数；因为只用它决定高度、不用于安全用途，`Relaxed` 序就够了：

[src/base.rs:708-719](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L708-L719) — xorshift 推进种子，并用 `trailing_zeros() + 1` 把随机数映射成高度。

关键映射是 `num.trailing_zeros() as usize + 1`。一个均匀分布的 32 位数，其末尾恰好有 \(k\) 个 0 的概率是 \(2^{-(k+1)}\)（要求最低 \(k\) 位是 0、第 \(k+1\) 位是 1）。令 \(h = k+1\)，则：

\[
\Pr(\text{height}=h)=2^{-h},\qquad h=1,2,\dots,31
\]

这正是参数 \(p=\tfrac12\) 的几何分布，期望高度：

\[
\mathbb{E}[\text{height}]=\sum_{h=1}^{\infty} h\cdot 2^{-h}=2
\]

也就是说，绝大多数节点高度只有 1 或 2，只有极少数节点撑到很高。再求「最高一层至少有一个节点」的期望层数，得到约 \(\log_2 n\)；查找时每一层期望只横向走常数步，故总期望查找代价：

\[
\mathbb{E}[\text{查找代价}]=O(\log n)
\]

这就是随机高度撑起跳表性能的数学根因。

接下来是「压低高度」的小表优化：

[src/base.rs:726-734](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L726-L734) — 当 `height >= 4` 且头节点在 `height-2` 层没有任何后继（即当前还没有那么高的塔）时，把高度减 1。

注意这里用了 `epoch::unprotected()`。注释解释：**我们只是把指针加载出来判空，不会解引用它**，所以不挂 `Guard` 也是安全的（关于 `unprotected` 的安全条件，u5-l17 会系统讲解）。

最后维护 `max_height`：

[src/base.rs:737-750](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L737-L750) — 用 CAS 循环把 `max_height` 抬到 `height`，只升不降。搜索路径从 `max_height` 层起步，能减少空跑。

#### 4.1.4 代码实践

1. **目标**：直观验证「高度服从几何分布」。
2. **操作步骤**：
   - 想象 `random_height` 里去掉「压低高度」循环后的纯分布：对均匀 32 位 `num`，`height = min(32, trailing_zeros(num)+1)`。
   - 手算（或写一段独立小程序）统计 100 万个随机数的 `trailing_zeros`，画出高度频次。
3. **观察现象**：高度 1 的占比 ≈ 50%，高度 2 ≈ 25%，高度 3 ≈ 12.5%……
4. **预期结果**：频次按 \(2^{-h}\) 衰减，与理论几何分布吻合。
5. 说明：`random_height` 是 `base.rs` 的私有方法，无法直接外部调用；上述统计用任何独立 xorshift/`fastrand` 实现复现 `trailing_zeros+1` 映射即可（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为何用 `trailing_zeros + 1` 而不是直接 `num % MAX_HEIGHT + 1`？

**答案**：`num % N` 是均匀分布，无法形成「高塔极稀疏」的层级结构，跳表会退化成接近链表与全连接的混合，期望复杂度不再是 \(O(\log n)\)。`trailing_zeros` 借助「连续 0 的个数」天然给出几何分布，正是跳表需要的概率骨架。

**练习 2**：`max_height` 为什么「只升不降」也能保证正确性？

**答案**：`max_height` 只是搜索的「起点提示」。从偏高的层起步，最坏只是多走几个空指针后下降，不影响正确性（最终都靠 level 0 精确定位），因此无需精确回收下降值，省去了一致性维护的代价。

---

### 4.2 insert_internal 主链路（上）：查重、分配、level 0 安装

#### 4.2.1 概念说明

`insert_internal` 是整个插入的唯一实现，`insert`/`compare_insert`/`get_or_insert*` 都只是给它传不同 `replace` 闭包的薄包装。它解决一个核心问题：**如何在不持锁的情况下，把一个新节点挂进一条可能正被并发读写的多层链表。**

它采用「两阶段」策略：

- **阶段 A**：只把新节点用一次 CAS 挂进 **level 0**。level 0 是完整链表，只要这一步成功，节点就已经「对查询可见」，逻辑上插入完成。
- **阶段 B**：自底向上把 \(1..height\) 层的链接补齐。高层链接只是加速搜索的「索引」，是**可选的**——即便中途失败或被中断，跳表依然正确，只是少了几层加速。

这个「底层必做、上层可选」的划分是无锁正确性的关键：阶段 B 任何一步失败都只需「停止构建」，不会破坏不变量。

#### 4.2.2 核心流程

```
insert_internal(key, value_fn, replace, guard):
  0. check_guard；重绑 guard 生命周期（hack）
  1. search = search_position(key)         # 查重
     if search.found 存在 且 replace(found.value)==false:
         try_acquire 旧节点为 RefEntry 并直接返回（命中且不替换）
  2. value = value_fn()                     # 先算 value，避免节点已分配后 value() panic
     height = random_height()
     node = Node::alloc(height, ref_count=2); 写入 key/value
  3. len.fetch_add(1, Relaxed)              # 乐观 +1（失败路径会减回）
  4. loop {                                  # level 0 安装重试
        node.tower[0] = search.right[0]
        if CAS(search.left[0].tower[0], search.right[0] -> node) 成功:
            if 存在 search.found（被替换的旧节点）且 mark_tower 成功:
                len.fetch_sub(1)            # 旧节点逻辑删除，长度净变化归零
            break
        # CAS 失败：重新查重
        search = search_position(key)
        if 命中 且 replace==false 且 try_acquire 成功:
            finalize(新建 node); len.fetch_sub(1); 返回旧 entry
     }
  5. 进入阶段 B（见 4.3）
```

#### 4.2.3 源码精读

函数签名与 `replace` 闭包的含义：

[src/base.rs:1013-1024](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1013-L1024) — `insert_internal<K, F, CompareF>`，其中 `F: FnOnce()->V` 是值的惰性构造器，`CompareF: Fn(&V)->bool` 是「是否替换已存在节点」的判定闭包。

第 0 步的「生命周期重绑 hack」会在 4.4 与 u4-l12 详讲，这里只记住它让返回的 `RefEntry` 不被绑定到 `guard` 的生命周期。

第 1 步——查重与「命中即返回」快路径：

[src/base.rs:1033-1045](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1033-L1045) — 先 `search_position` 查 key；若命中且 `replace` 判定为「不替换」，就用 `RefEntry::try_acquire` 抢一次引用计数，成功就直接把旧节点当 entry 返回，**完全跳过分配与插入**。

> 注意 `try_acquire` 可能失败（旧节点此刻正被别人删除、引用计数已归零）。失败时不在这里处理，继续走分配路径——因为重新 search 后大概率发现它已不存在，照常插入即可。

第 2 步——先造值、再造节点：

[src/base.rs:1047-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1047-L1065) — 注释点明用意：「先算 value，这样 value() 若 panic 也不会白白分配一个节点」。`Node::alloc(height, 2)` 初始引用计数为 **2**（一份给 level 0 链接、一份给返回的 entry），`alloc` 只写 `refs_and_height` 和清零塔指针，**故意不写 key/value**，所以是 `unsafe`（u2-l5 已讲）。

第 3 步——乐观地先把 `len` 加 1：

[src/base.rs:1067-1068](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1067-L1068) — `len` 用 `Relaxed`，因为它只是个近似计数（并发下本就不精确），失败/替换路径会再减回去。

第 4 步——level 0 CAS 安装与「替换旧节点」：

[src/base.rs:1070-1094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1070-L1094) — 把新节点的 level 0 后继设为 `search.right[0]`，再用一次 SeqCst CAS 把前驱 `search.left[0]` 的 level 0 指针从 `right[0]` 改成新 `node`。**这次 CAS 成功 = 插入的线性化点（linearization point）**，此后所有线程都能查到新节点。

CAS 成功后有一段关键的「替换语义」处理（1088-1092 行）：如果当初 `search.found` 命中了一个旧节点（即本插入是要替换它），就调用 `r.mark_tower()` 给旧节点打删除标记。`mark_tower` 返回 true 表示「我抢到了旧节点的删除权」（u3-l9），此时把 `len` 减 1 抵消——新节点 +1、旧节点被逻辑删除 -1，净变化为零，与「替换」语义一致。

第 4 步失败路径——重新查重并可能放弃新节点：

[src/base.rs:1096-1127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1096-L1127) — CAS 失败说明有人抢先改了前驱指针。这里有一个**为 panic 安全而设的 `ScopeGuard`**：在重新 `search_position` 期间，若比较函数 panic，`ScopeGuard::drop` 会 `Node::finalize` 掉新建但尚未挂入的节点，避免泄漏；正常返回后 `mem::forget(sg)` 解除这层保护。重新搜索后若命中且「不替换」且 `try_acquire` 成功，就把刚分配的新节点 `finalize` 掉、`len` 减回、返回旧 entry；否则带着新的 `search` 结果回到 loop 顶端重试 level 0 CAS。

#### 4.2.4 代码实践

1. **目标**：体会「level 0 CAS 成功即线性化点」。
2. **操作步骤**：阅读上面 1070-1094 这段；在脑中（或在源码副本旁标注）把每个变量换成具体场景：插入 `key=5`，假设 `search.left[0]` 是 key=3 的节点、`search.right[0]` 是 key=7 的节点。
3. **观察现象**：CAS 把「3 → 7」改成「3 → 5（新）」，且新节点的 level 0 指向 7，于是链表变成 `3 → 5 → 7`。
4. **预期结果**：在 CAS 成功的瞬间，任何并发 `get(&5)` 都能命中新节点；任何 `get(&3)`/`get(&7)` 的遍历也会正确穿过 5。

#### 4.2.5 小练习与答案

**练习 1**：为什么新建节点的初始引用计数是 2 而不是 1？

**答案**：一份代表「level 0 这条链接本身占用的引用」，一份代表「即将返回给调用者的 `RefEntry` 句柄」。两个持有者各自释放时都会减 1，归零才回收。若只给 1，则返回 entry 后第一次摘除 level 0 链接就会把计数减到 0、提前回收，而调用者手里的 entry 就成了悬垂指针。

**练习 2**：第 4 步的 `ScopeGuard` 在防什么？

**答案**：防止「新节点已 `alloc` 但尚未挂入」时，`search_position` 内部的比较函数 panic 导致节点泄漏。`ScopeGuard` 在 panic 展栈时 `finalize` 掉它；正常路径用 `mem::forget` 关闭这层保护，把所有权交还给主流程。

---

### 4.3 insert_internal 主链路（下）：构建高层塔与并发删除回退

#### 4.3.1 概念说明

阶段 A 让新节点在 level 0 可见后，阶段 B 把 `1..height` 各层的链接补上。难点在于：**补塔期间，别的线程可能正在删除这个新节点**（比如它刚被插入就被另一路径 remove）。因此每一层都要处理三类失败：

1. **新节点自己的指针已被打删除标记**（`tag()==1`）→ 直接停止建塔。
2. **后继是「同 key 的死节点」** → 重搜让其被清理，避免把两个同 key 节点链在一起。
3. **前驱指针的 CAS 失败** → 减回引用计数，重搜再来。

记住一句反复出现的注释（源码 1168-1170 行）：**只有 level 0 真正必需，高层只是加速搜索的索引**。所以阶段 B 任何中途退出都不破坏正确性。

#### 4.3.2 核心流程

```
'build: for level in 1..height:
    loop:
        pred = search.left[level]; succ = search.right[level]
        next = node.tower[level].load()          # 新节点自己在该层当前指向
        if next.tag()==1: break 'build           # 我正被删除，停止建塔
        if succ 与新节点 equivalent（同 key 死节点）:
            search = search_position(key); continue   # 让读路径清理它
        if CAS(node.tower[level], next -> succ) 失败: break 'build   # 被标记了
        refs_and_height.fetch_add(1<<HEIGHT_BITS)     # 新增一层链接，+1 引用
        if CAS(pred.tower[level], succ -> node) 成功: break   # 本层挂入完成
        else:
            refs_and_height.fetch_sub(1<<HEIGHT_BITS) # 挂入失败，减回
            search = search_position(key)             # 重搜再来
# 收尾：若最高层指针被标记，重搜一次让读路径把我摘干净
```

#### 4.3.3 源码精读

建塔主循环的入口与「自检删除标记」：

[src/base.rs:1135-1151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1135-L1151) — 对每个 `level`，先取出前驱 `pred` 与后继 `succ`，再加载新节点自己该层的当前指针 `next`。若 `next.tag()==1`，说明别的线程已开始删除我，立即 `break 'build` 退出整个建塔。

「禁止链接同 key 节点」的特殊处理（1153-1177 行的注释很值得读）：

[src/base.rs:1153-1177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1153-L1177) — 源码注释解释了一个微妙现象：从高层往低层遍历时，可能在高层看到一个同 key 节点、到低层又找不到它（它正被删除），甚至在不同层看到两个同 key 的不同节点。把新节点链到一个「同 key 的死后继」上会引发难缠的边界情况，所以作者选择**干脆禁止把两个同 key 节点链在一起**：一旦发现 `succ` 与新节点 `equivalent`，就重搜让读路径把它清理掉，再继续本层 loop。

挂入本层并维护引用计数：

[src/base.rs:1179-1218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1179-L1218) — 两步：
1. 先 CAS 把**新节点自己**该层指针从 `next` 改成 `succ`（设置新节点的后继）。失败说明该指针被打标，`break 'build`。
2. 成功后 `fetch_add(1<<HEIGHT_BITS)` 给引用计数 +1（多了一层链接）。
3. 再 CAS 把**前驱** `pred` 该层指针从 `succ` 改成新 `node`（真正挂入）。成功则 `break` 进入上一层；失败则 `fetch_sub` 减回刚才加的引用计数，重搜后再来本层 loop。

> 这里两次 CAS 的 SeqCst 与多处 `TODO: can we use release ordering here?` 的内存序权衡留到 u5-l17 系统讨论。本讲只需理解「失败必减回引用计数」是引用计数正确性的关键。

收尾的「兜底重搜」：

[src/base.rs:1220-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1220-L1229) — 建塔过程中，别的线程可能在我建到一半时（部分高层已挂入、部分还没）开始删除我。如果最终发现我的最高层指针已被打标，就主动 `search_bound` 一次——读路径（u3-l8 的 `help_unlink`）会把我从所有层摘干净。这正是「协作式清理」的体现：插入者也参与维护链表整洁。

#### 4.3.4 代码实践

1. **目标**：理解「高层可选、失败即止」为何不破坏正确性。
2. **操作步骤**：阅读 1135-1218，找出所有「失败就 `break 'build` 或 `continue`」的分支，数一数共有几处提前退出。
3. **观察现象**：每处退出要么是「新节点已被打删除标记」（节点即将被清理），要么是「重搜再来」（本层 loop 重试）。没有任何一处会让链表出现断裂或环路。
4. **预期结果**：即使建塔被任意中断，level 0 链表始终完整连续，查询永远正确；最坏情况只是新节点矮了一截、搜索慢一点。

#### 4.3.5 小练习与答案

**练习 1**：第 3 步「给前驱 CAS 挂入」失败后，为什么要 `fetch_sub` 减回引用计数？

**答案**：因为前一步已经 `fetch_add` 给「这一层链接」加过引用计数。既然本层最终没挂入（链接不存在了），就必须把这次 +1 撤销，否则引用计数永远多 1，节点永远无法归零回收，造成内存泄漏。

**练习 2**：1220-1229 的兜底重搜为什么是 `search_bound` 而不是 `mark_tower`？

**答案**：走到这里说明新节点的指针**已经被别人打过删除标记**（`tag()==1`），删除权早已被抢占，无需也不能再 `mark_tower`。此时只需让读路径把我物理摘除，所以调用 `search_bound` 触发 `help_unlink` 协作清理即可。

---

### 4.4 四种插入语义：insert / compare_insert / get_or_insert / get_or_insert_with

#### 4.4.1 概念说明

四个高层方法共用 `insert_internal`，区别只在两个参数：

- **`value` 参数**：直接传值 `|| value`，还是传惰性构造器 `value_fn`（仅在确实要插入时才求值）。
- **`replace` 闭包**：遇到已存在 key 时，`replace(&旧值)` 返回 `true` 表示「删除旧节点、插入新节点」（替换语义），返回 `false` 表示「保留旧节点、直接返回旧 entry」（不替换语义）。

| 方法 | value 参数 | replace 闭包 | 命中已存在 key 时的行为 |
|------|-----------|--------------|------------------------|
| `insert` | `\|_ \| value` | `\|_ \| true` | 无条件删除旧值、插入新值 |
| `compare_insert` | `\|_ \| value` | `compare_fn`（用户提供） | 仅当 `compare_fn(旧值)` 为真才替换 |
| `get_or_insert` | `\|_ \| value` | `\|_ \| false` | 保留旧值、返回旧 entry |
| `get_or_insert_with` | `value_fn`（惰性） | `\|_ \| false` | 保留旧值；但 `value_fn` 可能已被调用、结果被丢弃 |

#### 4.4.2 核心流程

`replace` 在 `insert_internal` 里只被查阅两次，都是「命中旧节点」时：

- 第一次在 1036-1045：命中且 `!replace` → 尝试直接返回旧 entry，跳过分配。
- 第二次在 1088-1092 与 1110-1126：level 0 CAS 成功后若 `replace` 为真，给旧节点 `mark_tower`；或重搜后命中且 `!replace` 则放弃新节点返回旧 entry。

`compare_insert` 的文档特别强调一句（**闭包在 key 不存在时不会被调用**），这一点由源码保证——`replace` 只在 `search.found` 为 `Some` 时才被调用。

#### 4.4.3 源码精读

`insert`：无条件替换。

[src/base.rs:1243-1249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1243-L1249) — `replace` 恒为 `true`。命中旧 key 时不会走「直接返回」快路径，而是照常插入新节点，并在 level 0 CAS 成功后 `mark_tower` 旧节点，实现「先删旧再插新」。

`compare_insert`：按闭包替换。

[src/base.rs:1251-1267](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1251-L1267) — 把用户的 `compare_fn` 原样作为 `replace` 传入。典型用法是「只在值变大/变新时才覆盖」，实现抢占式更新（见下方实践与 tests/map.rs 的 `concurrent_compare_and_insert`）。

`get_or_insert` 与 `get_or_insert_with`：不替换。

[src/base.rs:629-646](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L629-L646) — 两者 `replace` 恒为 `false`。区别在于值来源：`get_or_insert` 用 `|| value`（值已算好），`get_or_insert_with` 直接把 `FnOnce()->V` 当构造器传入（惰性求值）。

`get_or_insert_with` 的文档有一段重要警告：

[src/base.rs:634-640](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L634-L640) — 闭包**可能被调用但其结果被丢弃**。因为 `insert_internal` 第 2 步会执行 `let value = value();`（[src/base.rs:1047-1048](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1047-L1048)），随后若并发地有人先插入了同 key，本线程最终走到 1110-1126「不替换」分支，返回旧 entry 并 `finalize` 掉新节点，刚算出的 `value` 被丢弃。因此**闭包不应有累加计数器、修改共享状态等副作用**，否则会出现「副作用发生了但结果没进表」。

#### 4.4.4 代码实践

1. **目标**：体会 `compare_insert` 的「抢占式更新」语义。
2. **操作步骤**：阅读 `tests/map.rs` 的 `concurrent_compare_and_insert`（[tests/map.rs:153-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L153-L170)）。它启动 100 个线程，每个线程都 `compare_insert(1, i, |j| j < &i)`——只有当旧值严格小于自己的 `i` 时才覆盖。
3. **观察现象**：无论线程以何种顺序完成，最终 `key=1` 的值一定是 99（即 `len-1`）。
4. **预期结果**：`assert_eq!(*set.get(&1).unwrap().value(), len - 1)` 通过。这是因为 `|j| j < &i` 构成严格的「单调递增」替换条件，最终留下最大值。
5. 说明：这是高层 `SkipMap` 测试，可直接 `cargo test concurrent_compare_and_insert` 运行（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`get_or_insert_with(f)` 的闭包 `f` 在什么情况下会被调用但其返回值被丢弃？

**答案**：当本线程 `search_position` 未命中、于是执行了 `let value = value();` 求值，但在随后的 level 0 CAS 重试中发现**别的线程已抢先插入了同 key**，于是走「不替换」分支返回旧 entry，新构造的 `value` 被 `finalize` 丢弃。所以 `f` 不应有副作用。

**练习 2**：用 `insert` 连续两次插入同一个 key、不同 value，跳表的 `len` 如何变化？

**答案**：`len` 净变化为 0。第一次插入 `len +1`；第二次插入时 level 0 CAS 成功 `len +1`，紧接着 `mark_tower` 旧节点成功 `len -1`，净变化为 0。最终表里只有新值，`len` 反映「有效节点数」。

---

## 5. 综合实践

把本讲的「随机高度概率分布」与「并发插入正确性」串成一个小任务。

**任务**：编写一个多线程并发 `insert` 独立 key 的测试，验证最终 `len` 等于 key 总数；并附带一段文字解释为何随机高度让跳表期望复杂度为 \(O(\log n)\)。

**参考实现（示例代码，放到 `tests/` 下新增文件或临时 main）**：

```rust
// 示例代码：未在仓库中运行过，需本地验证
use crossbeam_skiplist::SkipMap;
use std::sync::Arc;
use std::thread;

#[test]
fn concurrent_insert_distinct_keys() {
    const THREADS: usize = 8;
    const PER_THREAD: usize = 5_000;

    let map = Arc::new(SkipMap::<u64, u64>::new());
    let mut handles = Vec::new();
    for t in 0..THREADS {
        let map = map.clone();
        handles.push(thread::spawn(move || {
            // 每个线程负责一段互不重叠的 key 区间，避免重复 key
            let base = (t as u64) * (PER_THREAD as u64);
            for i in 0..PER_THREAD {
                let k = base + i as u64;
                map.insert(k, k * 2);
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }

    assert_eq!(map.len(), (THREADS * PER_THREAD) as u64);
}
```

**操作步骤与预期**：

1. 把上面的测试加入 `tests/map.rs`（或新文件），运行 `cargo test concurrent_insert_distinct_keys`。
2. 由于各线程 key 互不重叠，不会触发「替换旧值」分支，`len` 的 `fetch_add/fetch_sub` 不会互相抵消，最终 `len` 应严格等于 `THREADS * PER_THREAD`。
3. 多跑几次确认稳定（并发测试建议循环 50~100 次，可参考 `tests/map.rs:135-151` 的 `concurrent_insert` 用 `for _ in 0..100` 放大竞态）。

**文字解释要点（写在测试注释里）**：

- `random_height` 用 `trailing_zeros + 1` 让 \(\Pr(\text{height}=h)=2^{-h}\)，期望高度为 2，最高层约为 \(\log_2 n\)。
- 因此搜索在每层期望横向走常数步、共 \(O(\log n)\) 层，总期望代价 \(O(\log n)\)。
- 并发下每个线程独立 CAS 自己的前驱指针，输者重试，最终所有 key 都被挂入 level 0，`len` 正确。

> 若想观察「重复 key 抢占」，可改成所有线程插入同一批 key，并对照 `concurrent_insert`（[tests/map.rs:135-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L135-L151)），此时 `len` 因替换抵消而远小于插入次数。

## 6. 本讲小结

- `random_height` 用 xorshift 生成随机数、`trailing_zeros+1` 映射成服从 \(p=\tfrac12\) 几何分布的高度，期望高度为 2，撑起跳表 \(O(\log n)\) 的期望复杂度；并用「当前最高塔」把高度压低、用 CAS 维护只升不降的 `max_height` 提示。
- `insert_internal` 采用**两阶段**策略：阶段 A 用一次 level 0 SeqCst CAS 把新节点挂入（线性化点），阶段 B 自底向上把 `1..height` 层链接补齐；高层链接是可选的加速索引，任意中断都不破坏正确性。
- 新节点初始引用计数为 2（level 0 链接 + 返回的 entry），塔每长一层 `+1`、每摘一层 `-1`，归零才回收——把插入与 u2-l6 的 epoch 回收联系起来。
- 「替换语义」由 `replace` 闭包驱动：命中旧 key 时若 `replace==true`，在新节点 level 0 CAS 成功后给旧节点 `mark_tower` 并 `len -1`，实现净替换。
- `insert`/`compare_insert`/`get_or_insert`/`get_or_insert_with` 四者只是给 `insert_internal` 传不同 `replace` 与 `value` 参数的薄包装；`get_or_insert_with` 的闭包可能被求值后丢弃，不应有副作用。
- 阶段 B 用协作式清理（`search_bound` 兜底 + `help_unlink`）应对「建塔中途被并发删除」，是 lock-free 活性的体现。

## 7. 下一步学习建议

- 顺读 **u3-l11（remove / pop_front,back / clear）**：它从「读者/竞争者」视角再次使用 `mark_tower` 与引用计数，与本章的「插入者视角」互为镜像，读完二者即可完整掌握跳表写路径。
- 阅读 **u5-l17（内存序分析）**：本章多处 `TODO: can we use release ordering here?` 标注的 SeqCst 选择，将在那里系统讨论能否放宽。
- 想验证本章 `unsafe` 的正确性，可用 `cargo +nightly miri test` 跑 `tests/base.rs` 的单线程用例（如 `insert`，[tests/base.rs:57-71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L57-L71)），确认无未定义行为——这是 u5-l19 的实践内容。
