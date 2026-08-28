# 跨线程 free：一次 CAS 推入 thread_free 链表

## 1. 本讲目标

上一讲（u5-l1）我们看到：当释放者是页的属主线程时，`mi_free` 快路径只做三行普通写——`used` 减一、块头插 `local_free`，全程零原子操作。本讲回答紧接着的问题：

- **另一个线程**释放这块内存时会发生什么？（生产者-消费者、消息队列、跨线程智能指针，都是这个形态）
- 为什么 `mi_free_block_mt` 只用**一次 CAS** 就能把块挂上页的 `xthread_free` 链表，既不需要锁，也不需要 ABA 防护？
- `xthread_free` 的**最低一位所有权位**如何让一次普通的 free「顺手」原子认领一个被遗弃（abandoned）的页？
- 拥有者线程在**什么时机**把 `thread_free` 链表收割（collect）回可分配的 `free` 链表？

学完本讲，你应该能独立读懂 `src/free.c` 的多线程释放路径，并解释 free list 多碎片化（multi-sharding）为什么让跨线程 free 依然便宜。

## 2. 前置知识

本讲假设你已学完 u5-l1（本地 free 快路径）与 u3-l2（页的三条 free list）。需要用到的概念：

- **CAS（Compare-And-Swap）**：一条原子指令，比较内存位置与期望值，相等则写入新值并返回成功，否则把内存中的当前值写回期望值变量。失败方通常重试。mimalloc 中的封装是 `mi_atomic_cas_weak_acq_rel`（见 [include/mimalloc/atomic.h:L80-L85](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L80-L85)，weak 版本允许伪失败，配合循环使用）。
- **ABA 问题**：经典无锁栈在 pop 时先读 head、读 next，再 CAS「head 仍是旧值则换成 next」。若期间 head 从 A 变成 B 又变回 A（值相同、链表结构已换），CAS 会错误成功，导致丢元素或链表接错。本讲会论证 mimalloc 的推链协议为什么天然不踩这个坑。
- **所有权契约**（u5-l1 的核心结论）：`mi_page_t` 的非原子字段（`used`、`free`、`local_free`……）只有属主线程能写；其他线程只能通过原子字段 `xthread_free` 与该页交互。这是全部分工的根基。
- **xtid 判定**：`xthread_id` 把属主线程 id 与低 2 位页标志打包存放，`_mi_prim_thread_id() ^ xthread_id` 的一次异或同时完成「是否属主」与「标志是否为零」两个判定。
- **缓存行争用**：多核 CPU 以 64 字节缓存行为单位同步，多个线程频繁原子写同一缓存行会导致缓存行在核间弹跳（ping-pong），这是无锁程序里比指令条数更主要的开销来源。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放路径本体：四分支分流、`mi_free_block_mt` 的 CAS 推链、`mi_free_try_collect_mt` 的认领善后 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 收割实现：`mi_page_thread_free_collect` 原子交换整链、`_mi_page_free_collect` 合并三条链，以及分配慢路径中的收割触发点 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_page_s` 结构、三条 free list 的不变式注释、`mi_thread_free_t` 的定义 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `mi_tf_*` 位操作助手、`mi_page_is_owned` / `mi_page_claim_ownership`（本讲作为 types.h 的延伸阅读） |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | 线程退出 / 强制回收时对所有页统一收割的入口 |

> 提醒（承接 u1-l3）：`free.c` 被 include 进 `alloc.c` 编译（`MI_IN_ALLOC_C` 守卫），所以这些 `static` 函数彼此完全内联可见。

## 4. 核心概念与源码讲解

### 4.1 分流入口：xtid 四分支中的两个跨线程分支

#### 4.1.1 概念说明

`mi_free` 拿到裸指针后，第一步通过页元数据定位（默认 64 位构建走 `MI_PAGE_META_IS_ALIGNED` 的纯算术对齐反查，见 u3-l4），第二步进入 `mi_free_nonnull` 的四分支。u5-l1 已覆盖前两个本地分支；本讲关注后两个：释放者**不是**页的属主线程（页属于另一个线程的 theap，或者页已被遗弃、不属于任何 theap）。

#### 4.1.2 核心流程

```text
xtid = 当前线程id ^ page->xthread_id        # 一次异或同时得到「属主差」与「标志」

xtid == 0                    → 本地 + 干净页      → mi_free_block_local      (u5-l1)
0 < xtid <= MI_PAGE_FLAG_MASK → 本地 + 特殊页      → mi_free_generic_local
(xtid & MI_PAGE_FLAG_MASK) == 0 且 xtid != 0
                             → 跨线程 + 干净页    → mi_free_block_mt          ★ 本讲主线
其余                          → 跨线程 + 特殊页    → mi_free_generic_mt        (对齐块/满队列页的兜底)
```

「干净页」指低 2 位标志（`MI_PAGE_IN_FULL_QUEUE`、`MI_PAGE_HAS_INTERIOR_POINTERS`）都为 0，块地址就是用户指针，无需额外修正。

#### 4.1.3 源码精读

四分支的完整判定在 [src/free.c:L223-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249)：

[src/free.c:L239-L248](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L239-L248) 是本讲的入口——注释写明「释放的页属于另一个线程的 theap，或是一个不属于任何 theap 的被遗弃页」，干净页直接调 `mi_free_block_mt`，`allow_collect` 参数来自 `mi_free` 的调用（恒为 `true`）。

标志位与特殊线程 id 的定义在 [include/mimalloc/types.h:L371-L386](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L371-L386)：低 2 位是页标志；线程 id 为 0 表示页被遗弃（`MI_THREADID_ABANDONED`）、为 4 表示被遗弃但已登记进 arena 的 abandoned 映射表（`MI_THREADID_ABANDONED_MAPPED`）。注意被遗弃页的 `xthread_id` 是 0/4，因此**任何**存活线程 free 它时 xtid 都不为 0，自然落入跨线程分支——这就是「遗弃页的回收入口藏在 free 里」的原因。

带内部指针（对齐分配）或驻留满队列的页走 [src/free.c:L155-L161](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L155-L161) 的 `mi_free_generic_mt`：先把用户指针修正回块起点，再进入同一个 `mi_free_block_mt`。

另有一个特殊调用者 [src/free.c:L298-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L298-L304) 的 `_mi_free_subproc_safe`：指针可能属于**另一个 subproc**（多解释器场景，u7-l4），此时 `allow_collect=false`——绝不认领别的子进程的页。

#### 4.1.4 代码实践

1. **实践目标**：确认四分支的判定条件在你的平台上真实成立。
2. **操作步骤**：在本地仓库打开 `src/free.c`，给 `mi_free_nonnull` 的四个分支各加一行 `fprintf(stderr, "branch N\n")`（本地实验副本，别提交），然后分别运行：单线程程序（应只打 branch 1）；「主线程分配、子线程释放」程序（应打 branch 3）。
3. **需要观察的现象**：跨线程释放时日志应稳定落在 branch 3（干净页），而不是 branch 4。
4. **预期结果**：普通小对象分配（无自定义对齐）的页没有内部指针、通常也不在满队列，branch 3 是跨线程释放的常态路径。具体日志输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：被遗弃页的 `xthread_id` 是 0，那么属主线程自己（如果还活着）free 自己页上的块会不会误入跨线程分支？

**答案**：不会误入「跨线程」的语义，但确实会走 branch 3 的代码路径——这是**故意**的。`xthread_id==0` 意味着页当前不属于任何 theap，即使原来的属主线程还活着，它对这页也没有独占权（非原子字段不可碰），必须走原子推链。这正是所有权契约的体现：判定依据是页当前的 `xthread_id` 值，而不是「谁曾经分配过这块内存」。

**练习 2**：为什么 `_mi_free_subproc_safe` 要传 `allow_collect=false`，而普通 `mi_free` 传 `true`？

**答案**：认领（claim）一个页意味着要对该页做收割并可能把它挂进**当前线程**的 theap 页队列；跨 subproc 认领会破坏 subproc 之间的内存隔离（页被 A 子进程的 theap 持有、却登记在 B 子进程的统计与 arena 集合里）。所以跨 subproc 释放只推链、不认领。

### 4.2 mi_free_block_mt：一次 CAS 的推链协议

#### 4.2.1 概念说明

这是本讲的核心。目标：把块 `block` 头插到页的 `xthread_free` 链表（即 `xthread_free` 字段，一个 `_Atomic(uintptr_t)`），且：

- 不加锁、不碰任何非原子字段（`used`、`free`、`local_free` 都不动）；
- 与任意多个并发释放者、以及属主线程的并发收割互不干扰；
- 顺手把「新头是否要置所有权位」编码进同一个原子字。

这本质是一个 **Treiber 栈的 push**：新节点先写好自己的 `next`，再用 CAS 把栈头换成自己。特殊之处在于栈头字里还藏着 1 个标志位。

#### 4.2.2 核心流程

```text
tf_old = relaxed 读 page->xthread_free
loop:
  block->next = tf_old 去掉标志位后的链头      # 普通写，块是自己独占的
  new_owned = allow_collect ? true : tf_old 的 owned 位
  tf_new  = block 指针 | new_owned
  若 CAS(xthread_free: tf_old → tf_new) 成功: 跳出
  否则: tf_old 已被 CAS 更新为最新值，回到 loop 重试
若 allow_collect 且 tf_old 的 owned 位为 0:      # 我们这次 CAS 恰好把无主页认领了
  进入 4.3 的善后流程 mi_free_try_collect_mt
```

关键计数语义（承接 u3-l2 的不变式）：跨线程 free **不递减 `used`**。`used` 是非原子字段，只有属主能写；这块内存「逻辑上已释放、账面上仍占用」的误差，由属主收割时一次性修正。形式化地：

\[ \text{alive} = \text{used} - |\text{thread\_free}| \]

所以只要块还挂在 `thread_free` 上，`used` 仍把它计入，`mi_page_all_free`（`used == 0`，见 [include/mimalloc/internal.h:L896-L901](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L896-L901)）为假——**正在被释放的块保证了页不会被并发释放**，无 use-after-free。

#### 4.2.3 源码精读

整个函数在 [src/free.c:L62-L97](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L62-L97)。CAS 推链的主体是 [src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87)：

```c
// push atomically on the page thread free list
mi_thread_free_t tf_new;
mi_thread_free_t tf_old = mi_atomic_load_relaxed(&page->xthread_free);
do {
  mi_block_set_next(page, block, mi_tf_block(tf_old));
  const bool new_owned = (allow_collect ? true : mi_tf_is_owned(tf_old));
  tf_new = mi_tf_create(block, new_owned);
} while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new));
```

逐行拆解：

- `mi_block_set_next` 把 `block->next` 指向旧头。在 CAS 成功前**先写 next 是安全的**：这个块上一刻还在释放者手里（程序语义保证没有其他人持有它），在 CAS 成功之前它在链表上不可见；CAS 失败后重写 next 即可，无副作用。
- `mi_tf_block` / `mi_tf_is_owned` / `mi_tf_create` 三个位操作助手在 [include/mimalloc/internal.h:L1088-L1097](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1088-L1097)：`(tf & ~1)` 取链头、`(tf & 1)==1` 判所有权、`block | owned` 打包。指针按 8 字节对齐，最低位空闲可用。
- CAS 用 `acq_rel` 序：release 保证新块的 `next` 写入对收割者可见；acquire 保证我们读到的 `tf_old`（及其链上的块）是别人发布过的完整状态。源码注释里那句 `// todo: release is enough?` 说明作者也在持续审视这里的序强度。

`xthread_free` 字段在页结构中的位置见 [include/mimalloc/types.h:L443](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L443)，其类型语义的定义在 [include/mimalloc/types.h:L388-L393](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L388-L393)：

```c
// The least-bit is set if the page is owned by the current thread. (`mi_page_is_owned`).
// Ownership is required before we can read any non-atomic fields in the page.
// This way we can push a block on the thread free list and try to claim ownership
// atomically in `free.c:mi_free_block_mt`.
typedef uintptr_t mi_thread_free_t;
```

**为什么无需锁**：唯一的共享可变状态就是 `xthread_free` 这一个字，所有修改都经由 CAS 线性化；每个释放者只写自己独占的块，属主只写自己独占的 `free/local_free/used`。没有任何需要互斥的临界区。

**为什么无需 ABA 防护**：ABA 危险存在于「pop」型算法——比较 head 值相等就信任 next。而这里：

1. 推链是「push」：CAS 的新值 `tf_new` 由我们独占的 `block` 构造，与旧值内容无关；比较失败只是说明「我看到的头过期了」，重试即可，**每次重试都重新装载最新头**，不会丢失任何并发发布。
2. 收割端（4.4 的 `mi_page_thread_free_collect`）是**整链原子交换**（head → NULL），不是逐个 pop。交换成功的一瞬间起，并发的 push 必定 CAS 失败（头已变 NULL），只能把自己的块挂到新链上——被取走的整条链从此私有，可安全遍历计数。
3. 内存生命周期有上面所述 `used` 计数兜底：链上的块让页保持「非全空」，页结构不会被释放复用，因此不需要 RCU/epoch 之类的延迟回收机制。

代价对比（与 u5-l1 的本地路径）：

| | 本地 free（u5-l1） | 跨线程 free（本讲） |
| --- | --- | --- |
| 定位页 | 纯算术对齐反查 | 同左（不查 page map） |
| 数据访问 | 3 行普通写（`used`、`local_free`、`block->next`） | 1 次普通写 + 1 次 relaxed 读 + **1 次 CAS（RMW）** |
| 原子操作 | 0 | 1 |
| 阻塞/等待 | 无 | 无（失败只是自旋重试，且通常一次成功） |
| `used` 修正 | 立即 | 延迟到属主收割 |

#### 4.2.4 代码实践

1. **实践目标**：在纸面上验证 CAS 协议在竞争下的正确性。
2. **操作步骤**：写出线程 T1、T2 同时对同一页 free 块 A、B 的交错时序——假设 T1 先装载 `tf_old=NULL` 后暂停，T2 完整完成 CAS（链变为 `B→NULL`），T1 恢复。逐条记录 T1 的 CAS 为何失败、`tf_old` 如何被更新、第二次循环 `A->next` 被重写为 `B`、CAS 成功后链为 `A→B→NULL`。
3. **需要观察的现象**：任何交错下，两个块最终都在链上、顺序与线性化序一致，没有任何一步需要回滚已完成的普通写。
4. **预期结果**：协议无丢失、无重复。这是纸面推演题，结论可对照 Treiber 栈 push 的标准证明。
5. 想看真实行为的话，可在本地副本给 CAS 循环加一个失败计数器，用多线程竞争同一页观察重试率——具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `mi_atomic_cas_weak_acq_rel` 换成 `mi_atomic_cas_strong_relaxed`（只保 release），会破坏什么？

**答案**：release 语义只保证「我写进 `block->next` 的内容对拿到这个块的人可见」，但收割者随后要遍历整条链（链上其他块的 next 是更早的释放者写的）。缺了 acquire，本线程与先前发布者之间没有 happens-before 边，理论上可能读到链上其他节点的过期 `next` 值。实践中 x86 总线序会掩盖这个问题（acquire/release 在 x86 上基本免费），但在 ARM 等弱序平台上是真实风险——这也解释了源码为什么纠结于「release is enough?」而最终选了 acq_rel。

**练习 2**：一次跨线程 free 里有几次内存访问？和本地 free 比差在哪里？

**答案**：定位页的对齐反查（不访存）+ 读 `xthread_free`（relaxed load）+ 写 `block->next` + 一次 CAS RMW（隐含一次读和一次写，且争用时缓存行要在核间迁移）+ 统计（debug 构建）。相比本地路径多出的主要成本就是那次 RMW 及其缓存行效应；少了 `used` 的更新。所以跨线程 free 的成本是「常数略贵」，而不是「复杂度变高」。

### 4.3 所有权位：并发 free 顺手「认领」被遗弃页

#### 4.3.1 概念说明

低 2 位 `xthread_id` 为 0/4 的页是**被遗弃**的：原属主线程已退出（或 theap 被销毁），页上仍有存活块。这些页登记在 arena 的 abandoned 映射表里等待复用（细节在 u6-l4）。

mimalloc 的巧妙之处：`xthread_free` 的最低位被用作**所有权票根**。任何线程推链时都会把这个位置 1；如果推链前它恰好是 0（无主），那么这次 CAS 一石二鸟——既发布了块，又原子地认领了整页。认领成功后即可安全读写该页的非原子字段，做一次小规模收割。

#### 4.3.2 核心流程

```text
推链成功后（tf_old 是 CAS 前的真实旧值）:
  若 allow_collect 且 tf_old.owned == 0:        # 说明页此前无主，我们刚认领了它
    mi_free_try_collect_mt(page, block):
      小对象: _mi_page_free_collect_partly(page, block)   # 利用手上已有的链头，少做一次原子交换
      大对象: _mi_page_free_collect(page, false)
      然后依次尝试:
        1. 页全空           → 从 abandoned 表摘除并整页释放
        2. 中小对象且选项允许 → 把页 reclaim 进当前线程的 theap（受队列长度上限约束）
        3. 页不再「很满」     → reabandon-to-mapped，登记进 arena 让寻找空页的线程能看到
        4. 都不行           → mi_abandoned_page_unown_from_free: 把 owned 位再清 0，还回去
```

#### 4.3.3 源码精读

认领判定在 [src/free.c:L89-L96](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L89-L96)：

```c
// and atomically try to collect the page if it was abandoned
if (allow_collect) {
  const bool is_owned_now = !mi_tf_is_owned(tf_old);
  if (is_owned_now) {
    mi_assert_internal(mi_page_is_abandoned(page));
    mi_free_try_collect_mt(page,block);
  }
}
```

注意技巧：CAS 成功后 `tf_old` 保存的是**交换前**的值，所以 `!mi_tf_is_owned(tf_old)` 恰好表示「我这次 CAS 把无主页变成了有主」。对照 [include/mimalloc/internal.h:L1110-L1119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1110-L1119) 的 `mi_page_is_owned` / `mi_page_claim_ownership`——后者用的 `mi_atomic_or_acq_rel(&page->xthread_free, 1)` 是同一种「置位即认领」的原子原语，用在分配侧认领遗弃页时。

善后主函数 [src/free.c:L480-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L480-L515) 的三步目标写在 [src/free.c:L364-L369](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L364-L369) 的注释里：释放全空页、可认领则收回自己 theap、降到 7/8 以下则重新映射登记。三个尝试分别是：

- [src/free.c:L371-L379](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L371-L379) `mi_abandoned_page_try_free`：全空则先从 abandoned 表摘除（可能等待读者），再整页释放。
- [src/free.c:L427-L476](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L427-L476) `mi_abandoned_page_try_reclaim`：源码注释直接点明动机——「free 一个自己拥有的页可以省掉原子操作，对 larson、rbtree-ck 这类基准提升很大；但跨线程过度认领会囤内存、降低线程间复用」，因此受 `mi_option_page_reclaim_on_free` 与 `page_max_reclaim` / `page_cross_thread_max_reclaim` 队列长度上限约束，且页须来自合适的 arena。
- [src/free.c:L396-L418](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L396-L418) `mi_abandoned_page_unown_from_free`：认领了却哪条路都走不通时，CAS 把 `(expected_thread_free | owned=1)` 换回 `(同链头 | owned=0)` 归还无主状态；若期间有并发推链导致 CAS 失败且链非空，就先收割更新 `used` 再重试——这段循环是「无主 ⇄ 有主」状态机的交汇点。

#### 4.3.4 代码实践

1. **实践目标**：观察「遗弃页被 free 顺手认领/回收」的真实发生。
2. **操作步骤**：写一个程序：创建子线程 T，在其内分配一批 32B 块存进全局数组后**不释放**直接 `pthread_exit`（线程结束时其未空页被 abandon）；join 后主线程逐一 `mi_free` 这批指针，最后 `mi_stats_print_out(NULL,NULL)`。
3. **需要观察的现象**：统计输出中 abandoned / reclaim 相关计数的变化（`pages_abandoned`、`pages_reclaim_on_free` 等计数器定义见 [include/mimalloc-stats.h:L78-L79](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L78-L79)；这些 theap 级计数如何呈现到输出「待本地验证」）。
4. **预期结果**：程序正常结束、无泄漏（进程退出统计里 malloc/free 配平）；被遗弃页在主线程 free 时进入 `mi_free_try_collect_mt` 的某条出路。debug 构建下断言全过即为正确。

#### 4.3.5 小练习与答案

**练习 1**：认领为什么不做成独立的 `if (页无主) mi_page_claim_ownership(page)` 两步？

**答案**：两步就不是原子的了——判定与置位之间页可能被别人认领，或者又变无主，需要循环重试且语义混乱。把所有权位编进推链的同一个 CAS，让「发布一个块」与「尝试认领」一次线性化完成，这正是 types.h:L388-L393 注释所说的设计意图。

**练习 2**：`mi_abandoned_page_unown_from_free` 归还所有权时，为什么要带上 `expected_thread_free`（自己刚才收割后的链头）做期望值，而不是随便 CAS 置 0？

**答案**：CAS 的期望值必须精确到「链头指针 + owned 位」整个字。带上链头可以确认从自己上次观察到此刻没有并发推链；若有（CAS 失败），就必须先把新到者收割进 `used` 账目再重试，否则可能在别人还往链上挂块的时刻错误归还，造成账实不符甚至丢失块。

### 4.4 收割时机：拥有者线程何时把 thread_free 搬回 free

#### 4.4.1 概念说明

跨线程释放的块躺在 `xthread_free` 上时，**既不能被分配、也没有从 `used` 账上销账**。「收割」（collect）指属主线程（或认领者）把这条链接管：原子取下整链 → 数清块数 → 修正 `used` → 挂到 `local_free`。收割是延迟的、批量的——这正是便宜的另一半来源：1 次收割摊平 N 次跨线程 free 的记账成本。

#### 4.4.2 核心流程

```text
收割 = 两级搬移:
  第一级（原子）: CAS 把 xthread_free 整链换成 NULL（保留 owned 位）      # 抢链
  第二级（私有）: 沿链数块数 count（上限 capacity 防坏环）
                  链尾接当前 local_free, 链头成为新 local_free
                  used -= count                                             # 此刻才销账
  随后（通常情形）: local_free 整体搬到 free, O(1) 指针搬移                 # 变成可分配
```

注意终点是 `local_free` 而非直接进 `free`——「`free` 弹空」因此保持为确定性事件，这是延迟回收心跳（u5-l3）的基础，也保证 `free` 链永远只有属主线程触碰。

#### 4.4.3 源码精读

**抢链**：[src/page.c:L185-L201](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L185-L201) 的 `mi_page_thread_free_collect`——装载、构造 `NULL|原owned位`、CAS 交换；链空则零成本返回。

**数链与销账**：[src/page.c:L150-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L183) 的 `mi_page_thread_collect_to_local`。两个防御性检查值得读：`count > capacity` 报「thread-free 链损坏（可能是跨线程 double free）」；`count > used` 报「元数据损坏」——坏链的块宁可滞留也不并入，防止污染扩散。

**合并三条链**：[src/page.c:L214-L243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L214-L243) 的 `_mi_page_free_collect`：先抢 thread_free，再把 `local_free` 搬进 `free`（`free==NULL` 的通常情形是 O(1)；`force` 时才做线性拼接）。

**谁在什么时机调用收割**（按重要性排序）：

1. **分配慢路径找页时**：[src/page.c:L783-L789](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L783-L789)——`mi_page_queue_find_free_ex` 扫描页队列，遇到「无立即可用块」的页先收割再判断。这是常态触发器：**属主线程下一次慢路径分配**顺带收割别的线程释放的块。注意快路径前的 [src/page.c:L204-L212](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L204-L212) `mi_page_free_quick_collect` 只搬 `local_free`、不碰 `thread_free`，避免快路径付出原子代价。
2. **线程退出 / 强制回收**：[src/theap.c:L97-L115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L97-L115) 对 theap 的每一页统一收割，全空则释放、`MI_ABANDON` 模式则遗弃。
3. **认领遗弃页时**：[src/page.c:L276-L289](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L276-L289) `_mi_theap_page_reclaim` 挂进新队列前先 `_mi_page_free_collect` 保证 `used` 账目新鲜；[src/page.c:L291-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L291-L304) `_mi_page_abandon` 遗弃前同理。
4. **本讲的 `mi_free_try_collect_mt`**（[src/free.c:L480-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L480-L515)）：认领者的小规模收割，小对象走 [src/page.c:L251-L269](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L251-L269) 的 `_mi_page_free_collect_partly`——从手上已有的 `mt_free` 位置开始收，省掉一次原子交换。

三条链的整体不变式与迁移图，权威注释在 [include/mimalloc/types.h:L398-L423](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L423)（`local_free` 与 `thread_free` 在 `free` 耗尽时迁移过去）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认「释放 ≠ 立即可复用」——块进了 `thread_free` 后要等属主的下一次收割。
2. **操作步骤**：主线程分配 100 万个 32B 块 → 启动子线程全部 `mi_free` 并 join → 主线程**睡眠 2 秒**后用 `/usr/bin/time -v`（或读取 `/proc/self/statm`）记录 RSS；随后主线程再做一轮大量分配（触发慢路径收割），再记录 RSS。
3. **需要观察的现象**：join 之后立即查看，内存占用往往并未明显回落（块还在 `thread_free`/`local_free`，页未变空也未 retire）；主线程再次分配后回落（收割修正 `used`，全空页进入 retire/释放流程）。
4. **预期结果**：RSS 呈「先持平、后回落」的两段形态。具体数值依赖平台的 purge 策略（`MIMALLOC_PURGE_DELAY`）与 retire 周期，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：收割为什么把 `thread_free` 挂到 `local_free`，而不是直接挂到 `free`？

**答案**：三个理由：(1) `free` 弹空必须是确定性事件，local_free 的缓冲使属主的「free 耗尽 → 慢路径 → 心跳计数」节律稳定，支撑延迟回收回调（u5-l3）；(2) 迁移动作统一为「local_free → free」一条路径，代码更简单；(3) 语义上两者都是「已释放但暂不可分配」，先合并同类项再一次性转正，摊薄成本。

**练习 2**：`_mi_page_free_collect_partly` 为什么「不能收 head 自己」？

**答案**：`page->xthread_free` 可能仍指向 head（我们是从 CAS 后的 `tf_old` 拿到的链头，没有再对字段做过交换）。若把 head 也摘走，字段就悬垂指向已私有的块。所以从 `head->next` 开始收；只有当 `used==1`（head 是最后一个未销账块）时才补一次完整的 `_mi_page_free_collect` 把它一并收掉。

## 5. 综合实践

**任务**：写一个双线程程序——线程 A 分配 100 万个 32B 小块，线程 B 全部 free；测量它与「同线程分配 + 同线程释放」的吞吐差异，并结合本讲源码解释 free list 多碎片化为什么让跨线程 free 依然便宜。

**示例代码**（`xthread_free.c`，非项目原有文件）：

```c
// xthread_free.c —— 需要 -pthread 与 mimalloc（debug 或 release 构建均可，见下）
#include <mimalloc.h>
#include <stdio.h>
#include <stdatomic.h>
#include <pthread.h>
#include <time.h>

#define N         (1000*1000)
#define RING_SIZE (1u << 10)          // SPSC 环形队列容量 1024

static void*          ring[RING_SIZE];
static atomic_size_t  r_head, r_tail; // 单生产者单消费者，各写一头

static double now_sec(void) {
  struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void produce(void* p) {        // 线程 A 调用
  size_t h = atomic_load_explicit(&r_head, memory_order_relaxed);
  while (h - atomic_load_explicit(&r_tail, memory_order_acquire) == RING_SIZE) { /* 满：自旋 */ }
  ring[h & (RING_SIZE - 1)] = p;
  atomic_store_explicit(&r_head, h + 1, memory_order_release);
}

static void* consumer(void* arg) {    // 线程 B：只 free
  (void)arg;
  for (size_t i = 0; i < N; i++) {
    size_t t = atomic_load_explicit(&r_tail, memory_order_relaxed);
    while (t == atomic_load_explicit(&r_head, memory_order_acquire)) { /* 空：自旋 */ }
    void* p = ring[t & (RING_SIZE - 1)];
    atomic_store_explicit(&r_tail, t + 1, memory_order_release);
    mi_free(p);                       // ★ 走 mi_free_block_mt：一次 CAS
  }
  return NULL;
}

int main(void) {
  double t0, t1;

  // 阶段一（对照）：同线程分配 + 同线程释放
  t0 = now_sec();
  void** ps = (void**)mi_malloc(N * sizeof(void*));
  for (size_t i = 0; i < N; i++) ps[i] = mi_malloc(32);
  for (size_t i = 0; i < N; i++) mi_free(ps[i]);
  mi_free(ps);
  t1 = now_sec();
  printf("same-thread : %7.1f ms (%.0f ns/对)\n", (t1-t0)*1e3, (t1-t0)*1e9/N);

  // 阶段二：线程 A 分配、线程 B 释放
  atomic_store(&r_head, 0); atomic_store(&r_tail, 0);
  pthread_t tb;
  pthread_create(&tb, NULL, consumer, NULL);
  t0 = now_sec();
  for (size_t i = 0; i < N; i++) produce(mi_malloc(32));
  pthread_join(tb, NULL);
  t1 = now_sec();
  printf("cross-thread: %7.1f ms (%.0f ns/对)\n", (t1-t0)*1e3, (t1-t0)*1e9/N);
  return 0;
}
```

**操作步骤**（在仓库根目录，构建方式承接 u1-l2）：

```bash
mkdir -p out/debug && cd out/debug && cmake ../.. -DCMAKE_BUILD_TYPE=Debug && make   # debug 库带统计
cd ../..
gcc -O2 -pthread -o xthread_free xthread_free.c -Iinclude -Lout/debug -lmimalloc-debug
MIMALLOC_SHOW_STATS=1 LD_LIBRARY_PATH=out/debug ./xthread_free
```

**需要观察的现象与预期结果**（具体数值「待本地验证」）：

1. **两阶段吞吐同一量级**。cross-thread 每对多付一次 CAS（RMW），但无锁、O(1)、不碰 `used`——预期差距是常数倍以内，而不是数量级。debug 构建里 padding 校验与 `MI_DEBUG_FREED` 填充会放大两阶段共同的开销，建议再用 release 构建对比纯分配器成本。
2. **统计输出**：`MIMALLOC_SHOW_STATS=1` 的进程级统计中 malloc/free 配平（阶段一 + 阶段二）。注意 debug 构建下 `mi_stat_free` 在**释放线程**的默认 theap 上记账（[src/free.c:L746-L779](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L746-L779)），两个 theap 的统计最终合并进同一个 heap，进程总量仍然正确。
3. **用源码解释「为什么便宜」**（这是本实践的交付物，写进你的实验记录）：
   - **推链端零锁零等待**：B 的每次 free = 读页（算术反查）+ relaxed load + 写 `block->next` + 一次 CAS（4.2）。
   - **记账延迟摊平**：`used` 不动，1 次收割修正 N 次释放的账（4.4）。
   - **争用被页级分片打散**：A 顺序分配把 100 万个 32B 块摊到约 \( 10^6 \times 32 / 65536 \approx 489 \) 个 64KiB 页上，每页一条独立的 `xthread_free`，B 的 CAS 落在数百个不同缓存行间轮转；而 A 分配用的是新页的 `free` 链与 `pages_free_direct`，与 B 释放的旧页几乎不相交——两个线程各自顺路，几乎无真争用。若 mimalloc 只有一条全局 free list（配一把锁），所有 free 将串行打同一个缓存行。
4. **延伸实验**：把块尺寸改成 4KiB（每页仅 16 块、页数更多）或把 B 改成两个线程，观察吞吐变化，验证「争用点数量 = 活跃页数」的推断。

## 6. 本讲小结

- 跨线程 free 走 `mi_free_nonnull` 的第 3/4 分支（xtid 异或判定），常态是干净页直达 `mi_free_block_mt`。
- `mi_free_block_mt` 是 Treiber 栈 push：先写块内 `next`，再一次 `mi_atomic_cas_weak_acq_rel` 把新头发布到 `xthread_free`；失败重试、成功即线性化。无锁；push+整链交换的组合使它无 ABA 风险，`used` 计数兜底保证页不会被并发释放。
- `xthread_free` 最低位是所有权位：推链与「认领无主页」在同一个 CAS 里完成，认领成功后经 `mi_free_try_collect_mt` 依次尝试释放、reclaim、reabandon-to-mapped，都不行再把 owned 位 CAS 回 0。
- 跨线程 free 不递减 `used`、不碰任何非原子字段——这是属主分配快路径保持零原子的前提；账目由收割修正：\(\text{alive}=\text{used}-|\text{thread\_free}|\)。
- 收割 = 原子抢整链 + 数链销账 + 挂 `local_free`，触发点在属主的分配慢路径、线程退出/强制回收、遗弃页认领，以及跨线程 free 的顺手认领；1 次收割摊平 N 次 free。
- 多碎片化的本质：把「跨线程释放」从一个全局争用点（一把锁一条链）变成每页一个争用点，争用点数量随活跃页数线性扩展，因此跨线程 free 依然便宜。

## 7. 下一步学习建议

- **u5-l3（延迟释放与心跳）**：本讲反复出现的「`thread_free`/`local_free` 先挂到不可分配状态」正是为单调心跳服务的，下一讲讲 `mi_register_deferred_free` 如何被运行时系统（Koka/Lean）在安全点使用。
- **u6-l4（abandon 与 reclaim）**：本讲 4.3 只讲了 free 侧的认领；arena 级 `pages_abandoned` 位图、`mi_arena_try_claim_abandoned` 的分配侧认领在那一讲展开。
- **源码延伸**：通读 [src/page.c:L150-L269](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L269) 的整段收割实现，再对照 [src/free.c:L364-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L364-L515) 的遗弃页善后，两段合起来就是完整的「页在有主/无主之间的状态机」。
- 想挑战的读者可以思考：如果两个线程**交替**分配并互相释放同一批块（larson 基准的模式），本讲的认领与 reclaim 上限（`mi_option_page_reclaim_on_free`、`page_max_reclaim`）会如何防止内存被囤住？带着这个问题去读 `mi_abandoned_page_try_reclaim` 的注释。
