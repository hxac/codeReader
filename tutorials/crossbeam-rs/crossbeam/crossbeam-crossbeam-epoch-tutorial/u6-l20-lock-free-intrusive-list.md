# 无锁侵入式链表 sync/list

## 1. 本讲目标

本讲带读者进入 crossbeam-epoch 的「基础设施层」：`src/sync/list.rs` 实现了一个 Michael 风格的无锁侵入式链表。它本身不直接是 EBR 的回收核心，但收集器正是用它来管理所有参与者（`Local`）的花名册——没有它，`try_advance` 就无法遍历线程、`finalize` 就无法把退会的线程从链表里摘掉。

学完本讲，你应当能够：

- 说清楚「侵入式链表」与普通容器链表的区别，以及 `IsElement` trait 如何把「数据」和「链表节点」解耦。
- 解释为什么删除一个节点要分两步：先用 `fetch_or(1)` 做逻辑标记，再由遍历者做物理 unlink。
- 读懂 `Iter::next` 在并发修改下如何用 CAS 跨过被标记节点、如何延迟回收、以及什么时候被迫 `Stalled` 重启。
- 把 `IsElement` 模式用到极致：设计一个同时挂在两条链表上的结构。

## 2. 前置知识

本讲假设你已经掌握前置讲义的内容，这里只做最小回顾：

- **tagged pointer（带标记指针）**（u2-l5/u2-l8）：堆地址按对齐对齐，低位空闲可存 tag。`fetch_or(1)` 会把最低位置 1，`Shared::tag()` 读出低位，`Shared::with_tag(0)` 把低位清零得到纯净地址。
- **CAS（compare_exchange）与 `compare_exchange_weak`**（u2-l8）：`compare_exchange(current, new, ...)` 期望原子值仍是 `current`，是则换成 `new` 并成功，否则失败并把真实当前值带回来。`weak` 版允许「假失败」。本讲的插入与物理摘除都靠它。
- **`Guard`、`defer_destroy` 与宽限期**（u3-l9/u3-l10）：`Guard` 是「线程已 pin」的凭证；`guard.defer_destroy(ptr)` 把一个 `Shared` 升级为 `Owned` 后 drop，但真正执行要等到全局 epoch 前进满 2 步（宽限期）。这是本讲能「先摘链、后回收」的安全基石。
- **`unprotected()`**（u3-l9）：返回一个不真正 pin 的假守卫，`defer` 在它下面会立即执行；本讲在 `List::drop` 和 `Local::finalize` 里会用到。

一句话提示：本讲里的 `next: Atomic<Entry>` 字段，既是「下一个节点的地址」，又用最低位兼任「本节点是否已被逻辑删除」的标记。这个一物二用是整篇的机关。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/sync/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs) | 无锁侵入式链表的完整实现：`Entry`/`List`/`IsElement`/`Iter`/`IterError`，以及单元测试。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 收集器的内部实现。其中 `Global.locals` 就是 `List<Local>`，`Local` 通过 `#[repr(C)]` 把 `entry` 放在首字段，并实现 `IsElement<Self>`。 |

链表被使用的两处关键点：

- `Global` 持有一个 `locals: List<Local>` 作为所有参与者的花名册，`try_advance` 通过 `self.locals.iter(guard)` 遍历它。
- 线程注册时 `Local::register` 会把新建的 `Local` `insert` 进这条链表；线程退出时 `Local::finalize` 会 `entry.delete(...)` 把自己标记删除，最终由别的线程在遍历时物理摘除并 `defer_destroy`。

## 4. 核心概念与源码讲解

### 4.1 Entry / List / IsElement 与侵入式设计

#### 4.1.1 概念说明

你熟悉的 `std::collections::LinkedList`（或 `Vec`）是「容器拥有节点」：容器在堆上分配一个个节点，每个节点里包着你的数据。这种结构对无锁并发很不友好——因为你没法把「数据本身」直接原子地挂进链表，数据是 `T`，链表里存的是 `Node<T>`，二者地址不同。

**侵入式链表（intrusive list）** 反过来：节点结构 `Entry` 被嵌入（embed）进用户数据 `T` 内部，作为 `T` 的一个字段。这样 `T` 的堆地址同时就是链表节点地址，原子操作一个字就能把它接进链表。代价是：用户必须保证 `T` 堆分配且地址不移动（crossbeam 里用 `Owned`/`Shared` 保证），并且要告诉链表「`Entry` 在 `T` 里的偏移量是多少」。

负责「在 `T` 与 `Entry` 之间互相换算地址」的就是 `IsElement` trait。它是侵入式链表的胶水：链表本身对 `T` 一无所知，全靠 `C: IsElement<T>` 这个关联实现来定位 `Entry`。

一个精妙之处在于：`IsElement` 是实现在「`T` 之外的某个类型 `C`」上的（虽然常常 `C == T`）。这样设计的目的是让**同一个 `T` 可以同时挂在多条链表上**——每条链表对应 `T` 里一个不同的 `Entry` 字段，也就对应一个不同的 `IsElement` 实现。本讲末尾的练习会让你亲手设计这种「双链表」结构。

#### 4.1.2 核心流程

侵入式链表的三个角色的职责分工：

```
T（用户数据，堆分配、不可移动）
 └─ 内嵌若干个 Entry（每个 Entry = 一个 next 指针 + 复用低位作删除标记）

IsElement<T>（胶水实现 C）
 ├─ entry_of(*const T)  -> *const Entry   // 从数据找节点
 ├─ element_of(*const Entry) -> *const T  // 从节点找数据
 └─ finalize(*const Entry, &Guard)        // 节点被物理摘除时如何回收

List<T, C>
 └─ head: Atomic<Entry>                   // 头指针，CAS 插入、遍历摘除
```

关键不变量：`Entry` 在 `T` 里的偏移是编译期固定的（靠 `#[repr(C)]` + 首字段，或手动 `offset_of!`），因此 `entry_of`/`element_of` 只是整数加减或裸指针 cast，零运行时开销。

#### 4.1.3 源码精读

**`Entry` 结构**——整条链表的最小砖块，只有一个字段：

[src/sync/list.rs:18-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L18-L23)：定义 `Entry`，其 `next: Atomic<Entry>` 字段。注释点明机关：最低位 tag=1 表示本节点已被逻辑删除。注意「删除标记」记在 **自己的 `next` 字段**上，而不是记在前驱的指针上——这是 Michael 风格的标志性设计，下文会解释为什么这样能避免并发删除断链。

**`IsElement` trait**——胶水接口：

[src/sync/list.rs:70-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L70-L95)：三个方法 `entry_of`（安全）、`element_of`（unsafe）、`finalize`（unsafe）。`element_of` 的 unsafe 契约是「调用方必须保证这个 `Entry` 确实来自一个 `T`」。

**真实用户 `Local` 的实现**——这是收集器实际用的版本，也是「零开销 cast」的最佳示例：

[src/internal.rs:291-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L291-L318)：`Local` 标注 `#[repr(C)]` 并注释「`entry` 必须是首字段」。因为 `repr(C)` 保证字段按声明顺序布局、首字段偏移为 0，所以 `*const Local` 与 `*const Entry` 在数值上完全相等。

[src/internal.rs:572-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L572-L586)：`impl IsElement<Self> for Local`。`entry_of`/`element_of` 都只是一个 `cast`，无需任何偏移计算；`finalize` 直接 `guard.defer_destroy(...)`，把摘除的 `Local` 的真正释放推迟到宽限期之后。

**`List` 结构**——只有一个 `head`：

[src/sync/list.rs:97-105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L97-L105)：`List<T, C: IsElement<T>>` 仅含 `head: Atomic<Entry>` 和 `PhantomData`。整个容器只占一个字。

**`Global` 如何使用它**：

[src/internal.rs:165-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L165-L174)：`Global` 的第一个字段就是 `locals: List<Local>`，注释写明这是「`Local` 的侵入式链表」。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证 `entry_of`/`element_of` 在 `Local` 上确实是「数值相等」的 cast，理解 `repr(C)` 的作用。

**操作步骤**：

1. 打开 [src/internal.rs:291-295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L291-L295)，确认 `entry` 是 `Local` 的第一个字段。
2. 对照 [src/internal.rs:572-581](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L572-L581)，注意 `entry_of` 与 `element_of` 都只调用 `ptr.cast::<...>()`，没有任何 `offset`/`add`。
3. 思考：如果删掉 `#[repr(C)]`（让编译器自由重排字段），这两个 cast 还正确吗？

**预期结果**：去掉 `repr(C)` 后，Rust 默认布局不保证 `entry` 在偏移 0，cast 会得到错误的地址——这正是那行 `// Note: entry must be the first field` 注释要防的坑。本步无需运行，属推理验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IsElement` 不直接写成 `impl<T> IsElement<T> for T where T: HasEntry`，而要单独引入一个类型参数 `C`？

**答案**：因为同一个 `T` 可能内嵌多个 `Entry`（对应多条链表），需要多个不同的 `IsElement<T>` 实现。如果实现绑死在 `T` 自身上，Rust 的 orphan 规则与「每个 trait 对每个类型只能有一个实现」的限制会让你无法为第二个 `Entry` 再写一个实现。引入独立的 `C`（往往用空标记类型或 `T` 自身）后，每个 `Entry` 对应一个 `C`，互不冲突。文件 doc 注释 [src/sync/list.rs:54-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L54-L68) 给出了双 `Entry` 结构 `B` 的示例。

**练习 2**：`List` 自己没有 `Drop` 时会泄漏吗？

**答案**：`List` 实现了 `Drop`（见 4.2），它会在析构时遍历并 `finalize` 残留节点。`insert` 的 unsafe 契约也明确要求「插入的对象必须在 List drop 之前被移除」。

### 4.2 insert 的 head CAS 与 delete 的逻辑标记

#### 4.2.1 概念说明

无锁链表要解决的核心难题是：**两个线程同时删除相邻节点，怎么避免把链表「断开」？**

朴素想法是「删节点 = 把前驱的 `next` CAS 指向后继」。但这有个致命问题：假设线程 A 正在删节点 X（要把 X 的前驱 P 的 next 指向 X 的后继 Y），与此同时线程 B 在删 Y（要把 X 的 next 指向 Y 的后继）。两个 CAS 交织后，可能出现 P.next 被改成 Y，但 X.next 同时被 B 改掉，导致 Y 被意外「绕过」丢失。

Michael 的解法是把删除拆成**两阶段**：

1. **逻辑删除（logical delete）**：把待删节点 `X` 自己的 `next` 字段最低位置 1（`fetch_or(1)`）。这只动 `X` 自己，不碰前驱，因此绝不会与别的删除发生「抢占同一个指针」的冲突。一旦标记，`X` 在语义上已不存在，但物理上还挂在链表里。
2. **物理摘除（physical unlink）**：由某个遍历者（`Iter::next`）在路过 `X` 时，把前驱 `P.next` 从 `X` CAS 成 `X` 真正的后继。因为此时 `X` 已被标记，任何想删 `X` 的线程看到标记就不会重复操作，避免了竞态。

这个设计把「删除」这一容易冲突的操作，降级成对「自己 next 字段」的一次原子位或——不需要协调前驱。代价是链表里会短暂残留「已标记未摘除」的节点，靠遍历者来清扫。

#### 4.2.2 核心流程

**插入**（`List::insert`，把节点接到 head 之后）：

```
读 head 当前的 next 值 old_next
loop {
    新节点.next = old_next            # 先把新节点缝好
    CAS(head: old_next -> 新节点):
        成功 -> break
        失败 -> old_next = 真实当前值, 重试
}
```

**逻辑删除**（`Entry::delete`）：

```
self.next.fetch_or(1, Release, guard)   # 最低位置 1，发布「我已删」
# 注意：不动前驱，立刻返回
```

`fetch_or(1)` 在 u2-l8 里讲过：它只动 tag 低位，指针高位不变；用 `val & low_bits` 限定只或上低位。`Release` 序保证「标记可见」与之前的写入构成 happens-before。

**为什么标记记在「自己的 next」而不是「前驱的 next」上**：因为「自己的 next」字段只有一个写者序列（逻辑删除只发生一次），CAS `fetch_or` 天然无冲突；而前驱的 next 可能被插入者和摘除者同时争抢，标记若放在那里就会和「先标后改」的 CAS 序列打架。记在自己身上，摘除者只要看到「目标节点的 next 被标记」就知道可以安全跨过它。

#### 4.2.3 源码精读

**`insert` 的 CAS 循环**：

[src/sync/list.rs:165-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L165-L196)：先用 `C::entry_of` 从 `container: Shared<T>` 取出内嵌 `Entry` 的裸指针，然后进入循环：把新节点的 `next` 设为读到的 `next`，再用 `compare_exchange_weak` 把 `head` 从旧值换成新节点。失败时用 `err.current` 更新本地快照重试（这正是 u2-l8 讲过的「失败归还真实当前值」的用法）。注释明确：插入点紧挨 head。

**`delete` 的逻辑标记**：

[src/sync/list.rs:143-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L143-L154)：整个函数就一行 `self.next.fetch_or(1, Release, guard)`。unsafe 契约要求「本 entry 在链表里、且尚未被删除」，并要求删后能安全地对其调用 `C::finalize`。

**真实使用：`Local::finalize` 退会**：

[src/internal.rs:529-569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L529-L569)：线程退出时，`Local::finalize`（inherent 方法）先 `ptr::read` 出 `Arc` 引用，再调 `(*this).entry.delete(unprotected())` 把自己在花名册里逻辑删除，最后 `drop(collector)`。注意顺序：**先读出引用、再标记删除**——因为标记后别的线程随时可能把 `Local` 物理摘除并回收，必须在此之前把 `Arc` 拿走（注释在 556-558 行特别强调）。

**`List::drop` 的断言**：

[src/sync/list.rs:221-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L221-L236)：链表析构时用 `unprotected()`（无并发）逐个走，`assert_eq!(succ.tag(), 1)` 强制要求每个残留节点都已被逻辑删除——否则就是用户违反了 `insert` 的「必须在 drop 前移除」契约，直接 panic。然后对每个节点调 `C::finalize`。

#### 4.2.4 代码实践（源码阅读型 + 改参数观察）

**实践目标**：体会逻辑删除的「立即返回、延迟清扫」特性。

**操作步骤**：

1. 阅读 [src/sync/list.rs:370-404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L370-L404) 的 `delete` 单元测试：插入 e1/e2/e3 后 `delete(e2)`，随后 `iter` 应当只看到 e3、e1，跳过 e2。这说明逻辑删除的节点会被遍历者**当场物理摘除**。
2. 阅读末尾同一测试 [src/sync/list.rs:397-404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L397-L404)：再删 e1、e3 后重新 `iter`，得到空。
3. （可选修改）在 `Entry::delete` 的 `fetch_or` 后加一句 `eprintln!("marked {:p}", self);`，跑 `cargo test --lib sync::list::tests::delete`，观察标记发生的时机。

**预期结果**：`delete` 调用只打印「已标记」，并不立即从内存释放；真正摘除发生在随后的 `iter` 里（4.3 会看到）。释放则还要等宽限期。**注意**：本环境若不便跑测试，可标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`insert` 为什么用 `compare_exchange_weak` 而不是 `compare_exchange`？

**答案**：因为插入在一个 `loop` 里重试，能接受 `weak` 的假失败（spurious failure）——失败了下一轮再来即可。`weak` 在某些架构上比非 weak 快（少一次一致性开销），在循环场景里是首选。这与 u2-l8 的建议一致。

**练习 2**：若 `delete` 用 `Relaxed` 而非 `Release`，会出什么问题？

**答案**：逻辑删除标记必须与该节点之前的写入构成 happens-before，否则摘除并回收它的线程（在宽限期后 drop）可能看不到节点字段的最新值。`Release` 保证「标记之前的写」对执行 `finalize`/`defer_destroy` 闭包的回收线程可见；改 `Relaxed` 会破坏这一同步。配合的是 `Iter::next` 里读 `next` 用的 `Acquire`（见 4.3）。

### 4.3 Iter::next：链接断开、finalize 延迟回收、Stalled 重启

#### 4.3.1 概念说明

`Iter` 是这条链表最有意思的部分——它同时承担三个职责：**遍历、垃圾清扫（物理摘除）、竞态自愈**。

迭代器维护两个核心指针（都是「受 Guard 保护」的 `Shared`）：

- `pred: &'g Atomic<Entry>`：指向「当前节点 `curr` 的前驱的 `next` 字段」。注意它是 `&Atomic`，即「前驱节点里那个 next 字段的地址」，CAS 它就能把 `curr` 从链表里摘掉。
- `curr: Shared<'g, Entry>`：当前正在审视的节点。

不变量：`pred` 所指的 `Atomic` 当前持有的值始终等于 `curr`（即 `pred` 是 `curr` 的前驱的 next 字段）。这个不变量在每条分支里都被小心维护。

每访问一个 `curr`，迭代器读它的后继 `succ = curr.next`：

- 若 `succ.tag() == 0`（`curr` 未被删除）：`curr` 是活节点，返回它，并把 `pred`/`curr` 各前进一步。
- 若 `succ.tag() == 1`（`curr` 已被逻辑删除）：尝试用 CAS 把 `pred` 从 `curr` 改成 `succ`（跨过 `curr`），成功则对 `curr` 调 `finalize`（即 `defer_destroy`，延迟回收）。这就是物理摘除。

「Stalled 重启」处理的是一种边界竞态：当你想跨过 `curr` 时，发现 `pred` 的当前值（CAS 失败带回的真实值，或 CAS 成功后的新值）本身也是被标记的——这意味着你的前驱也已被删，链表在你脚下结构已变，继续往前走不安全。此时迭代器重置到 `head`，并向调用者返回 `Err(IterError::Stalled)`，提示「我从头再来，你也得知道这次遍历被打断了」。

#### 4.3.2 核心流程

`Iter::next` 的判定树（伪代码）：

```
loop {
    若 curr 为 null -> 返回 None（到尾）

    succ = curr.next.load(Acquire)

    if succ.tag() == 1:                      # curr 被逻辑删除
        succ_clean = succ.with_tag(0)
        debug_assert!(curr.tag() == 0)       # 不能删一个已删节点之后

        match pred.CAS(curr -> succ_clean, Acquire, Acquire):
            Ok  -> C::finalize(curr)         # 成功摘除，延迟回收 curr
                    new_pred_val = succ_clean
            Err -> new_pred_val = e.current   # 失败，取真实当前值

        if new_pred_val.tag() != 0:          # 前驱也被删了
            pred, curr 重置到 head
            return Err(Stalled)              # 告诉调用者：被打断，重来

        curr = new_pred_val                  # 只前进 curr，pred 不变
        continue

    else:                                     # curr 是活节点
        pred = &curr.next                     # 前进一步
        curr = succ
        return Ok(&element_of(curr))         # 返回数据
}
```

两条关键不变量贯穿始终：

- **`pred` 指向的 `Atomic` 的值 == `curr`**：成功摘除后，`pred` 的值变成了 `succ_clean`，于是把 `curr` 设为 `succ_clean`，不变量保持；失败时 `pred` 的真实值是 `e.current`，把 `curr` 设为它，不变量也保持。所以「只前进 `curr`、不动 `pred`」是对的。
- **`debug_assert!(self.curr.tag() == 0)`**（[L251](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L251)）：绝不跨过 `curr` 去删它后面的节点——因为「在已删节点后再删」会让物理摘除的 CAS 失去正确的前驱。所以遇到连续删除，必须重启。

#### 4.3.3 源码精读

**`Iter` 与 `IterError` 定义**：

[src/sync/list.rs:107-132](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L107-L132)：`Iter` 持有 `guard`/`pred`/`curr`/`head`；`IterError::Stalled` 的 doc 写明「并发线程在我正在审视的地方改了状态，后续遍历会从头重启」。

**`List::iter` 构造迭代器**：

[src/sync/list.rs:198-218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L198-L218)：`pred` 初始化为 `&self.head`，`curr` 为 `head.load(Acquire)`，`head` 字段保留用于重启。doc 的「Caveat」三条（新插入可能看不到、删除可能仍看到、可能 Stalled）是并发遍历的语义契约，务必读一遍。

**`Iter::next` 主体**（本讲的重头戏）：

[src/sync/list.rs:238-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L238-L299)：完整 `Iterator` 实现。几个要点对应到行号：

- [L243](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L243)：用 `Acquire` 读后继，与 `delete` 的 `Release` 配对，确保看到标记时也能看到该节点此前的全部写入。
- [L245-247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L245-L247)：`succ.tag()==1` 判定删除，并用 `with_tag(0)` 拿到纯净后继地址。
- [L254-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L254-L256)：物理摘除的 CAS——把 `pred`（前驱的 next 字段）从 `curr` 换成 `succ_clean`，`Acquire` 用于成功和失败两条路径。
- [L262-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L262-L264)：摘除成功后 `C::finalize(curr, guard)`，对 `Local` 而言就是 `defer_destroy`——真正释放推迟到宽限期。
- [L277-282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L277-L282)：前驱也被标记 → 重置到 head 并返回 `Err(Stalled)`。
- [L289-293](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L289-L293)：活节点分支，前进一步并通过 `C::element_of` 把 `Entry` 还原成 `T` 返回。

**调用方如何处理 `Stalled`**——看收集器自己怎么用：

[src/internal.rs:249-270](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L249-L270)：`try_advance` 遍历 `self.locals.iter(guard)`，遇到 `Err(Stalled)` 就直接 `return global_epoch`（放弃本轮推进，把工作让给那个赢了的线程）；遇到 `Ok(local)` 就检查它的 epoch 是否阻碍推进。这是 `Stalled` 在真实代码里的标准用法——**调用者必须显式处理 `Result`**。

#### 4.3.4 代码实践（必做：读测试 + 画时序）

**实践目标**：理解「已被逻辑删除但尚未物理 unlink」的节点在并发下如何被 `Iter` 处理。

**操作步骤**：

1. 阅读 [src/sync/list.rs:406-449](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L406-L449) 的 `insert_delete_multi` 测试：`THREADS=8` 个线程各插 `ITERS=512` 个 `Entry`，再全部 `delete`；线程退出后主线程 `iter` 断言链表为空。
2. 画一张时序图，聚焦其中一个节点 `X`：
   - 线程 A 调 `X.delete()` → `X.next.fetch_or(1)`，此时 `X` 还挂在链表里，`head -> ... -> P -> X -> Y -> ...`。
   - 主线程（或另一线程）`iter` 走到 `pred=P 的 next 字段, curr=X`，读 `succ = X.next`，发现 `succ.tag()==1`。
   - `iter` 用 `pred.CAS(curr=X -> succ=Y_clean)`：成功则 `X` 被物理摘除，`defer_destroy(X)`；失败则用带回的真实值重试或重启。
3. 在图上标出三个时刻：①逻辑删除后、②CAS 摘除成功瞬间、③宽限期后真正释放。说明在 ①~② 之间 `X` 仍可被其他持有 `Shared` 的线程安全解引用（因为还没释放），这正是 EBR 提供的保证。

**需要观察的现象**：`X` 经历「在链表里且被标记」→「不在链表里但内存还在」→「内存被回收」三态。第二态的长度由宽限期决定，与本讲的链表逻辑无关——链表只负责第一、二态之间的切换。

**预期结果**：能画出上述三态时序，并解释「为什么摘除后不能立刻 free」——因为别的遍历者可能还握着指向 `X` 的 `curr`。本步为源码阅读型，无需运行；若要验证，可参考 4.3.5 的练习。

#### 4.3.5 小练习与答案

**练习 1**：物理摘除的 CAS 成功后，为什么调用的是 `C::finalize`（→ `defer_destroy`）而不是直接 `drop`？

**答案**：因为这一刻可能还有别的线程正握着指向 `curr` 的 `Shared`（比如另一个并发 `Iter`）。直接 `drop` 会 use-after-free。`defer_destroy` 把释放推迟到宽限期之后（全局 epoch 前进满 2 步），届时所有可能见过 `curr` 的线程都已离开临界区，才安全。这正是 EBR 与无锁链表配合的核心价值。

**练习 2**：什么情况下 `Iter::next` 会返回 `Err(Stalled)`？调用者必须做什么？

**答案**：当迭代器试图跨过一个被标记的 `curr` 时，发现前驱的 `next` 当前值（无论 CAS 成功后的新值还是失败带回的真实值）也是被标记的（`tag != 0`）——即前驱也已被删，链表局部结构已不可信。此时迭代器重置到 `head` 并返回 `Stalled`。调用者**必须**显式处理这个 `Result`：要么像 `try_advance` 那样放弃本轮操作，要么重新开始遍历。忽略它（比如 `unwrap`）会丢数据或 panic。

**练习 3（设计题，承接综合实践）**：把一个含两个 `Entry` 的结构同时挂到两条 `List` 上，需要几个 `IsElement` 实现？

**答案**：两个——每个 `Entry` 字段对应一个 `IsElement<T>` 实现，`entry_of`/`element_of` 用各自的 `offset_of!` 算偏移。`List<T, C1>` 与 `List<T, C2>` 用不同的 `C` 类型参数区分。详见综合实践。

## 5. 综合实践

**任务**：设计一个能同时挂在两条无锁链表上的结构，并写出两套 `IsElement` 实现。这是把 4.1 的「侵入式 + IsElement」模式用到极致。

参考 [src/sync/list.rs:54-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L54-L68) 的文档示例 `struct B { entry1, entry2, data }`，设计如下（**示例代码**，不在本仓库中，仅供你练习理解）：

```rust
// 示例代码：演示双链表 IsElement 模式，非项目原有代码
use crossbeam_epoch::{Atomic, Entry /* 仅示意，实际 Entry 是 pub(crate) */};
// 注意：crossbeam-epoch 的 Entry/IsElement/List 都是 pub(crate)，外部无法直接使用。
// 本例仅用于理解模式；要在 crate 内复用，需在自己的库里重写等价结构。

struct Worker {
    entry_by_id: Entry,   // 挂到「按 id 索引」的链表
    entry_by_score: Entry,// 挂到「按 score 排序」的链表
    id: u64,
    score: u32,
}

// 标记类型，用于区分两套实现（IsElement 实现在独立类型上）
struct ById;
struct ByScore;

impl IsElement<Worker> for ById {
    fn entry_of(w: *const Worker) -> *const Entry {
        // entry_by_id 的地址 = Worker 基址 + 其偏移
        unsafe { core::ptr::addr_of!((*w).entry_by_id) }
    }
    unsafe fn element_of(e: *const Entry) -> *const Worker {
        // 反向：用容器在crate里的 offset_of! 宏算回基址
        // 伪代码： (e as usize - offset_of!(Worker, entry_by_id)) as *const Worker
        todo!("用 memoffset::offset_of! 计算")
    }
    unsafe fn finalize(e: *const Entry, guard: &Guard) {
        unsafe { guard.defer_destroy(Shared::from(Self::element_of(e))) }
    }
}

impl IsElement<Worker> for ByScore { /* 同理，指向 entry_by_score */ }
```

**练习要点**：

1. **两个 `Entry` 必须不同字段**，否则同一个节点无法同时存在于两条链表（一个 `Entry` 的 `next` 只能串一条）。
2. **`entry_of` 的偏移计算**：`Local` 靠 `repr(C)` + 首字段「零偏移」省掉了计算（[src/internal.rs:573-576](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L573-L576)）；双字段场景下两个 `Entry` 至多一个在首字段，另一个必须用 `offset_of!`（如 [`memoffset`](https://docs.rs/memoffset)）显式算偏移。
3. **`finalize` 一律走 `defer_destroy`**：物理摘除后绝不立刻 free，交给宽限期。两条链表各自摘除时都会调 `defer_destroy`——但 `Worker` 只有一份内存，`defer_destroy` 是「升级为 Owned 后 drop」，**第二次调用会是双重释放**。这是双链表设计的真实陷阱，实践中要让「从两条链表都摘除」这一回收时机被单一所有者管理（例如只有其中一条链表的 `finalize` 真正 `defer_destroy`，另一条只 unlink）。思考你会如何避免双重 free。

> 提示：crossbeam 内部之所以没踩这个坑，是因为每个 `Local` 只挂在**一条** `locals` 链表上（`C = Self`，单 `Entry`）。多链表是 `IsElement` 设计预留的能力，但回收责任需要使用者自己协调。

## 6. 本讲小结

- **侵入式链表**把 `Entry` 嵌进用户数据 `T`，使 `T` 的堆地址就是节点地址，`IsElement` trait 负责二者间的零开销换算；`Local` 靠 `#[repr(C)]` + 首字段让换算退化成裸 cast。
- **删除分两阶段**：`Entry::delete` 用 `fetch_or(1, Release)` 在「自己的 next」上打标记（逻辑删除），不动前驱，从而避免并发删除断链；物理摘除交给遍历者。
- **`Iter::next` 三职责**：遍历活节点、用 CAS 跨过并 `defer_destroy` 被标记节点（物理摘除 + 延迟回收）、在前驱也被删时返回 `Err(Stalled)` 并从头重启。
- **回收安全性来自 EBR**：物理摘除后绝不立刻 free，而是 `defer_destroy` 推迟到宽限期之后，保证别的并发遍历者不会 use-after-free。
- **调用者必须处理 `Stalled`**：`try_advance` 遇到 `Stalled` 就放弃本轮推进，把工作让给赢的线程——这是无锁遍历「让步」语义的体现。
- `List::drop` 用 `unprotected()` 做单线程清扫，并断言所有残留节点都已被标记删除，强制 `insert` 的「drop 前移除」契约。

## 7. 下一步学习建议

下一篇 **u6-l21 Michael-Scott 无锁队列 sync/queue** 会讲 `src/sync/queue.rs`，它是同一层（`src/sync/`）的另一个无锁数据结构，但用的是「哨兵节点 + head/tail 双指针 + helping」的套路，比链表更复杂，回收也走 `defer_destroy`。建议：

1. 先回顾本讲的「逻辑删除/物理摘除/延迟回收」三元组，队列的 `pop` 同样依赖 `defer_destroy`。
2. 阅读 `Global::collect` 里用到的 `queue.try_pop_if`（[src/internal.rs:217-225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L217-L225)），那是队列「条件弹出」的真实调用点，与本讲 `Iter` 的「条件摘除」是对偶关系。
3. 如果你对本讲的内存模型细节（`Acquire`/`Release`/宽限期）还想深挖，可回头看 u5-l17（epoch 表示）与 u5-l18（pin/unpin 内存屏障）。
