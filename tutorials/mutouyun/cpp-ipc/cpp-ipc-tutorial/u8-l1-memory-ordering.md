# 内存序、伪共享与缓存行

## 1. 本讲目标

本讲是专家层第一篇，目标是从「会用 libipc」转向「看懂 libipc 为什么这么写并发」。

读完本讲，你应当能够：

- 说清 `std::memory_order_acquire` / `release` / `acq_rel` / `relaxed` 在环形队列里各自承担的「可见性」职责，并能指认 `prod_cons.h` 里成对的 release/acquire。
- 理解**伪共享（false sharing）**的成因，解释 `alignas(cache_line_size)` 为什么把热计数器各自「关进」一条 64 字节缓存行。
- 在 CAS（compare_exchange）循环里，根据「读来做决策」还是「写来发布结果」判断该用 `acquire`、`release` 还是 `acq_rel`。
- 读懂 `conn_head_base::init()` 的**双重检查锁（DCLP）**，理解外层 `acquire`、内层 `relaxed`、收尾 `release` 各自的必要性。
- 在 `multi-multi-broadcast::push` 里，逐一标注每个原子操作的内存序，并解释**为什么 `rc_` 可以用 `relaxed` 而 `f_ct_` 必须用 `release/acquire`**。

本讲默认你已经学过 u4（无锁循环队列与生产-消费者算法）和 u3（数据通路），知道 `rd_`/`wt_`/`ct_`、`rc_`/`f_ct_`/`epoch_`、`cc_`/`cc_id` 这些游标与位图分别是干什么的。本讲**只换一个视角**：不再讲算法逻辑，而是讲它们之间的「可见性与缓存」为什么这么设计。

## 2. 前置知识

在进入源码前，先用三段白话建立直觉。

### 2.1 「先写后发」与内存可见性

现代 CPU 每个核都有自己的缓存，编译器还会重排指令。所以你在代码里写的「先 `data_[i] = x;`，再 `ready = true;`」，到另一个线程眼里**不一定**是这个顺序。

C++ 的原子操作带一个「内存序」参数，用来约束这种重排与可见性。最常用的两个是：

- **release（写端）**：把它之前（同一线程内、程序顺序在前）的所有读写「打包」，等别人 acquire 读到这次写入时，这个「包裹」里的内容对它全部可见。
- **acquire（读端）**：读到一次 release 写入后，该 release 之前的「包裹」对本线程后续操作全部可见。

于是「生产者写数据 → release 发布标记 → 消费者 acquire 读到标记 → 消费者读数据」就成了跨线程传递数据的**标准发布-订阅**姿势。用公式表达这条「先行发生（happens-before）」关系：

\[
\text{release-store} \;\xrightarrow{\;sw\;}\; \text{acquire-load}
\;\Longrightarrow\; \text{store 前的写} \;\xrightarrow{\;hb\;}\; \text{load 后的读}
\]

其中 \(sw\) 表示 synchronizes-with，\(hb\) 表示 happens-before。

### 2.2 伪共享：同一个缓存行的代价

CPU 不是按字节，而是按**缓存行（cache line）**搬数据，主流平台一条缓存行通常是 64 字节。两个核频繁写**同一条缓存行上的不同变量**时，哪怕逻辑上互不相干，硬件的缓存一致性协议（如 MESI）也会让这条缓存行在两个核之间反复失效、重新加载——这就是**伪共享**。现象是：数据明明没有共享，性能却像被锁住一样掉。

解法很简单：把两个被不同核高频写入的变量，强制放到**不同的缓存行**里，C++ 用 `alignas(64)` 把变量对齐到 64 字节边界即可（必要时尾随填充让下一个变量也落在新行）。

### 2.3 CAS：一次操作既读又写

`compare_exchange_weak(expected, desired, order)` 是「读-比较-改」三合一：读当前值、与 `expected` 比、相等就替换为 `desired`。因为它既读又写，内存序的选择要看你**主要拿它做什么**：拿读到的值来决策（要 acquire），还是拿它来发布结果（要 release），或两者都要（`acq_rel`）。

## 3. 本讲源码地图

本讲只盯住三个文件，按「从通用工具 → 数据结构 → 算法」的顺序读：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/libipc/utility/utility.h` | 通用工具头 | `cache_line_size` 常量定义、`make_align` 对齐函数 |
| `src/libipc/circ/elem_def.h` | 循环数组基础 | `conn_head_base::init()` 的 DCLP、`conn_head::connect()` 的 CAS 内存序 |
| `src/libipc/prod_cons.h` | 生产-消费者算法 | 四类变体里 `rd_/wt_/ct_`、`rc_/f_ct_/epoch_` 的内存序，以及大量 `alignas` |

这三个文件在 u1-l3 里已被定性为「头文件库」——它们是模板，在使用处才实例化，没有 `.cpp`。本讲读的就是这些模板里的原子操作。

## 4. 核心概念与源码讲解

### 4.1 acquire/release 可见性：环形队列的发布-订阅

#### 4.1.1 概念说明

无锁环形队列要解决的核心问题是：**生产者把数据写进某个槽，消费者怎么保证能「看见」这批数据、而不是看到旧值或半写完的值？** 答案是借助游标的 release/acquire 配对。

- 生产者写完槽 `data_[wt]` 后，用 `wt_.fetch_add(release)` 发布新写游标。`release` 把「写 data_」这件事打包进这次 fetch_add。
- 消费者先用 `wt_.load(acquire)` 读游标；一旦读到新值，`acquire` 就解开了上面的「包裹」，于是它随后读 `data_` 时一定能看到生产者写的全部内容。

反过来，消费者推进 `rd_` 也用 release，生产者读 `rd_` 用 acquire，于是生产者能正确判断「哪些槽已被消费、可以复用」。

#### 4.1.2 核心流程

最简情形（单写单读 unicast）的握手：

```
生产者 push:                      消费者 pop:
  写 data_[wt]                      读 wt_ (acquire) ←┐
  wt_.fetch_add (release) ──sw──►  读 data_[rd]      │ 包裹解开
                                    rd_.fetch_add (release)──┐
  读 rd_ (acquire) ◄─────────────────────────────────── sw ─┘
```

两个 release/acquire 对（一个 `wt_`、一个 `rd_`）撑起了双向可见性。

#### 4.1.3 源码精读

最干净的教学样本在单写单读变体的 `push`/`pop`：

[src/libipc/prod_cons.h:40-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L40-L49) —— `push`：先 `wt_.load(relaxed)`（自己独占 `wt_`，无需同步）、`rd_.load(acquire)`（看消费者的进度判断满），写完数据后 `wt_.fetch_add(release)` 发布。

```cpp
auto cur_wt = circ::index_of(wt_.load(std::memory_order_relaxed));
if (cur_wt == circ::index_of(rd_.load(std::memory_order_acquire) - 1))
    return false;                                  // full
std::forward<F>(f)(&(elems[cur_wt].data_));        // 写数据
wt_.fetch_add(1, std::memory_order_release);       // 发布
```

[src/libipc/prod_cons.h:61-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L61-L71) —— `pop`：对称反向，`wt_.load(acquire)` 是**消费者看到生产者数据的关键一步**。

```cpp
auto cur_rd = circ::index_of(rd_.load(std::memory_order_relaxed));
if (cur_rd == circ::index_of(wt_.load(std::memory_order_acquire)))
    return false;                                  // empty ← acquire 看见生产者发布的 wt_
std::forward<F>(f)(&(elems[cur_rd].data_));        // 读数据（包裹已解开，安全）
std::forward<R>(out)(true);
rd_.fetch_add(1, std::memory_order_release);       // 发布消费者进度
```

要点：`wt_.fetch_add(release)`（push 的 L47）与 `wt_.load(acquire)`（pop 的 L64）构成一对；`rd_.fetch_add(release)`（pop 的 L69）与 `rd_.load(acquire)`（push 的 L43）构成另一对。把任何一边的 release 换成 relaxed，消费者就可能读到没写完的槽。

#### 4.1.4 代码实践

**目标**：亲手验证 release/acquire 配对是「谁配谁」。

**步骤**：
1. 打开 [src/libipc/prod_cons.h:40-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L40-L49)，在 `push` 里找到唯一一个 `release`（L47）。
2. 在 [src/libipc/prod_cons.h:61-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L61-L71) 的 `pop` 里找到唯一一个配对它的 `acquire`（L64）。
3. 用笔把这两处连一条线，标注「data_ 的可见性由这条线保证」。

**预期结果**：你会看到 `wt_` 上恰好一 release 一 acquire；`rd_` 上同样一 release（L69）一 acquire（L43），两组对称。**待本地验证**：若你把 push 的 `wt_.fetch_add` 改成 `relaxed` 重编，在高并发压测下消费者可能出现读到半新半旧槽位的现象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `push` 里 `wt_.load` 用 `relaxed` 而不是 `acquire`？
**答**：单写者模型下 `wt_` 只有本生产者线程会写、会推进，读它纯粹是读「自己的」游标，不依赖其他线程的发布，故无需 acquire，relaxed 足够。

**练习 2**：把 `pop` 里 L64 的 `wt_.load(acquire)` 误改成 `relaxed`，最坏后果是什么？
**答**：消费者可能读到一个尚未被生产者 release 发布的 `wt_` 值，进而去读一个还没写完的 `data_` 槽——即读到未初始化或半写入的数据，且在弱内存模型（如 ARM）上更容易复现。

### 4.2 cache_line_size 对齐：消灭伪共享

#### 4.2.1 概念说明

环形队列里 `rd_`、`wt_`、`ct_`、`epoch_` 这些游标是**被不同核高频写**的热点。如果它们恰好落在同一条 64 字节缓存行上，生产者核写 `wt_`、消费者核写 `rd_`，就会让这条缓存行在两个核之间反复失效重载——典型的伪共享，吞吐会被白白吃掉。

libipc 用一个常量统一描述缓存行宽度，并对每个热游标单独 `alignas`：

[src/libipc/utility/utility.h:38-45](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/utility.h#L38-L45) —— 注释写得很直白：「Minimum offset between two objects to avoid false sharing.」

```cpp
// Minimum offset between two objects to avoid false sharing.
enum {
// #if __cplusplus >= 201703L
//     cache_line_size = std::hardware_destructive_interference_size
// #else
    cache_line_size = 64
// #endif
};
```

注释揭示了设计意图：C++17 起标准库提供 `std::hardware_destructive_interference_size`（专为「避免伪共享的最小偏移」而设），但为兼容更低标准，libipc 直接取经验值 64。同文件还有一个配套的对齐工具 [src/libipc/utility/utility.h:47-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/utility.h#L47-L50) `make_align`，把任意尺寸向上取整到 2 的幂对齐。

#### 4.2.2 核心流程

`alignas(cache_line_size)` 会让编译器在该成员前面插入足够的填充字节，使其地址落在 64 的倍数上；又因为这些热游标之间各自带 `alignas`，它们被**彼此隔开到不同缓存行**。两条缓存行的代价公式大致是：

\[
\text{伪共享代价} \propto \frac{\text{两个核的写频率}}{\text{缓存行大小}}
\]

把热点分到不同行后，分子里的「互相失效」消失，分母的约束也就解除。

#### 4.2.3 源码精读

`prod_cons.h` 里凡是会被「另一个核/进程高频写」的游标，都单独对齐。单写单读变体两个游标各占一行：

[src/libipc/prod_cons.h:33-34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L33-L34) —— `rd_`（消费者写）与 `wt_`（生产者写）各 `alignas(cache_line_size)`，避免两者伪共享。

```cpp
alignas(cache_line_size) std::atomic<circ::u2_t> rd_; // read index
alignas(cache_line_size) std::atomic<circ::u2_t> wt_; // write index
```

多写多读变体新增的提交游标 `ct_` 与纪元 `epoch_` 同样隔离：[src/libipc/prod_cons.h:314-315](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L314-L315)。注意 L212 的 `epoch_` 虽是「only one writer」非原子（单写者无需原子），也照常对齐——因为它仍会被多读者读到，需与相邻的 `wt_` 隔行。

#### 4.2.4 代码实践

**目标**：清点 libipc 在无锁算法里为防伪共享设了多少道「隔断」。

**步骤**：运行只读检索（在仓库根目录）查找所有 `alignas(cache_line_size)`，确认它们全部集中在 `prod_cons.h`。

**预期结果**：你会得到 7 处——单写单读变体的 `rd_/wt_`（L33、L34）、多写多读单播的 `ct_`（L117）、单写多读广播的 `wt_/epoch_`（L211、L212）、多写多读广播的 `ct_/epoch_`（L314、L315）。每一处都对应一个会被异核高频写的热点。**待本地验证**：注释掉任意一处 `alignas` 后做广播压测（参考 u1-l2 构建 demo），吞吐应下降。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `conn_head_base` 里的 `cc_`（连接位图）没有 `alignas`？
**答**：`cc_` 的写只发生在 `connect`/`disconnect`，频率远低于 `rd_/wt_` 这种每条消息都写的游标，伪共享影响小；而且 `cc_` 与同类的 `constructed_` 主要是被多进程「读多写少」访问，不值得为它单独开一条缓存行。

**练习 2**：若把 `cache_line_size` 从 64 改成 16，会怎样？
**答**：在 64 字节缓存行的真实 CPU 上，16 字节对齐不足以把两个热点隔到不同缓存行（它们仍可能落在同一条 64 字节行内），伪共享重现，性能退化。

### 4.3 CAS 内存序选择：读决策用 acquire，写发布用 release

#### 4.3.1 概念说明

CAS（`compare_exchange_weak/strong`）和 `fetch_add/fetch_and` 这类「读-改-写」操作，内存序怎么选，取决于这次操作在协议里扮演的角色：

- 读出来的值要**用来做判断**（比如「当前连接位图是不是我预期」）→ 倾向 `acquire`，确保看到别人发布的最新状态。
- 这次写入是**向别人发布结果**（比如「我成功占了一个连接位」）→ 倾向 `release`。
- 既要把当前状态读准、又要把自己这次更新发布出去 → `acq_rel`（读-改-写同时完成 acquire 与 release）。
- 只是为了原子地改个值，没人依赖它的可见性传递 → `relaxed`。

#### 4.3.2 核心流程

以连接位图 `cc_` 的 `connect` 为例，CAS 自旋循环的决策流：

```
读 cc_ (acquire) → 算 next → CAS(cc_, next, release)
                              └ 成功：发布「我占了 next^curr 这一位」给所有进程
                              └ 失败：yield 退避后重读
```

`disconnect` 是读-改-写且两端都重要，故用 `acq_rel`。

#### 4.3.3 源码精读

广播模式抢连接位 [src/libipc/circ/elem_def.h:59-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L59-L71)：

```cpp
cc_t connect() noexcept {
    for (unsigned k = 0;; ipc::yield(k)) {
        cc_t curr = this->cc_.load(std::memory_order_acquire);        // 读取来决策
        cc_t next = curr | (curr + 1);                                // 找最低 0 位
        if (next == curr) return 0;                                   // 满座
        if (this->cc_.compare_exchange_weak(curr, next,
                std::memory_order_release)) {                         // 成功=发布新位图
            return next ^ curr;                                       // 返回单一 bit 的 cc_id
        }
    }
}
```

- `load(acquire)`：要看清当前所有进程发布的连接状态才能正确抢位。
- `CAS(..., release)`：抢位成功就是把「新位图」发布给全网，release 保证这个新位图对后续 acquire 可见。

断连 [src/libipc/circ/elem_def.h:73-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L73-L75) 用 `fetch_and(acq_rel)`，因为归还一位既要先看清当前位图（acquire）又要发布新位图（release），RMW 一把梭所以 `acq_rel`。

对比单播模式 [src/libipc/circ/elem_def.h:92-93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L92-L93) 的 `fetch_add(relaxed)`：单播只数连接数、不分位、不依赖跨进程可见性传递具体哪一位，所以 relaxed 就够。

再看多写多读单播抢提交槽 [src/libipc/prod_cons.h:122-132](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L122-L132)：`ct_.load(relaxed)`（只是取一个起点值）、`rd_.load(acquire)`（看消费者进度判断满）、`ct_.CAS(acq_rel)`（抢槽既要看清又要发布「这槽我占了」），是 `acq_rel` 的典型场景。

#### 4.3.4 代码实践

**目标**：用「读决策 / 写发布 / 两者」的三分法，给 `conn_head` 的三个方法归类。

**步骤**：
1. 读 [src/libipc/circ/elem_def.h:59-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L59-L71) 的 `connect`，回答：`load` 是「读决策」还是「写发布」？`CAS` 呢？
2. 读 [src/libipc/circ/elem_def.h:73-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L73-L75) 的 `disconnect`，解释为何选 `acq_rel` 而非 `release`。
3. 读 [src/libipc/circ/elem_def.h:81-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L81-L86) 的 `conn_count`，注意它默认 `acquire` 读 `cc_`——为什么数位也用 acquire。

**预期结果**：`connect` 的 load=acquire（决策）、CAS=release（发布）；`disconnect` 的 fetch_and 同时决策+发布故 acq_rel；`conn_count` 虽只读但要看到最新位图才能数准，故默认 acquire。

#### 4.3.5 小练习与答案

**练习 1**：把 `connect` 里的 `CAS` 从 `release` 误改成 `relaxed`，会出什么问题？
**答**：抢位成功的进程发布的新位图，可能不能及时对其他进程的 `load(acquire)` 可见，导致两个进程都以为自己抢到了「同一个最低空闲位」，连接位图状态不一致。

**练习 2**：单播 `connect` 的 `fetch_add(relaxed)`（L93）为何不需要 acquire/release？
**答**：单播连接只是维护一个计数，`cc_id` 由 `fetch_add` 的返回值天然唯一（每个调用者拿到不同的递增值），不依赖跨进程的可见性传递来保证正确性，relaxed 即可。

### 4.4 DCLP 双检锁：共享内存对象的一次性构造

#### 4.4.1 概念说明

`elem_array`（循环数组本体）躺在共享内存里，多个进程会先后 `mmap` 同一块内存。第一个进程要负责对这块裸内存做 placement-new 初始化，**其余进程必须看到这个已构造好的对象**，绝不能重复构造。这就是经典的**双重检查锁（Double-Checked Locking Pattern, DCLP）**场景。

DCLP 的难点在于「第一次检查」不能裸读指针——没有同步的话，后续进程可能看到「构造已开始但未完成」的中间态。解法是用一个原子标志 `constructed_` 配合正确的内存序。

#### 4.4.2 核心流程

`conn_head_base::init()` 的四步：

```
① 外层 load(acquire):   已构造？→ 是，直接用（快路径，99% 走这里）
② 加锁 unique_lock(lc_)
③ 内层 load(relaxed):   再查一次，防并发重复构造
④ placement-new + store(release):  构造完，发布给所有后来者
```

为什么外层 acquire、内层 relaxed、收尾 release？这是本模块的精髓，见下。

#### 4.4.3 源码精读

[src/libipc/circ/elem_def.h:33-42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L33-L42)：

```cpp
void init() {
    /* DCLP */
    if (!constructed_.load(std::memory_order_acquire)) {              // ① 外层：acquire
        LIBIPC_UNUSED auto guard = ipc::detail::unique_lock(lc_);     // ② 加锁
        if (!constructed_.load(std::memory_order_relaxed)) {          // ③ 内层：relaxed
            ::new (this) conn_head_base;                              // ④ 真正构造
            constructed_.store(true, std::memory_order_release);      // ⑤ 发布
        }
    }
}
```

逐条解释内存序的必要性：

- **外层 `acquire`（L35）**：这是「别人能安全使用对象」的总闸。它与构造者第 ⑤ 步的 `release` 配对——只要读到 `true`，就 guaranteed 看到构造者 placement-new 写下去的所有字段。若改成 `relaxed`，可能读到 `true` 却看到半构造的对象。
- **内层 `relaxed`（L37）**：此时已持锁 `lc_`，同一把锁保证了「同一时刻只有一个进程在构造」，不会有别的写者，故无需更强的序；`relaxed` 既正确又省开销。
- **收尾 `release`（L39）**：把 placement-new 的全部写入打包发布给所有未来的外层 `acquire` 读者。这是 DCLP 的「另一半」。

注意 `lc_` 是 `ipc::spin_lock`（[src/libipc/circ/elem_def.h:29](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L29)），它是跨进程共享的自旋锁，本身也提供 happens-before——但内存序的选择让 DCLP 在「锁外快路径」上也安全。

#### 4.4.4 代码实践

**目标**：体会 DCLP 各内存序「省不得、也多不得」。

**步骤**：
1. 读 [src/libipc/circ/elem_def.h:33-42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L33-L42)，把 ①③⑤ 三处内存序分别记成「闸门读 / 锁内复查 / 构造发布」。
2. 假设把 ① 改成 `relaxed`：在纸上推演「进程 A 正在构造、进程 B 何时能错读」。
3. 假设把 ③ 也改成 `acquire`：判断是否影响正确性、是否浪费。

**预期结果**：① 改 relaxed 会破坏快路径安全性（可能用半构造对象）；③ 改 acquire 仍正确但多余（持锁后无并发写者）。结论：现写法是「最小必要强度」。**待本地验证**：在弱内存模型机器上，若 ① 误用 relaxed，多进程同时启动时偶发 `elem_array` 字段读到 0。

#### 4.4.5 小练习与答案

**练习 1**：既然 `lc_` 这把锁已经提供了同步，为什么外层 `load` 还需要 `acquire` 而不能 `relaxed`？
**答**：外层检查是**锁外**快路径，绝大多数进程根本不进锁。它们的安全性完全靠 `constructed_` 的 acquire/release 配对，与锁无关。锁只保护「谁真正执行构造」这一临界区。

**练习 2**：第 ⑤ 步 `store(release)` 能否改成 `store(relaxed)`？
**答**：不能。release 是把 placement-new 的写入「发布」出去的唯一手段；改成 relaxed 后，外层 acquire 的读者即使读到 `true`，也不保证能看到构造好的字段，DCLP 失效。

## 5. 综合实践：解剖 multi-multi-broadcast 的 push

这是本讲的收口任务，把前面四个模块串起来。请聚焦 `channel`（多写多读广播）真正在跑的 `push`：

[src/libipc/prod_cons.h:329-364](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L329-L364)

**目标**：逐一标注该函数里每个原子操作的内存序，并回答核心问题——**为什么 `rc_` 用 `relaxed` 而 `f_ct_` 用 `release/acquire`？**

### 5.1 操作步骤

1. 打开 [src/libipc/prod_cons.h:329-364](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L329-L364)，按下表逐行填内存序（行号已给出）：

| 行号 | 操作 | 变量 | 内存序 | 角色 |
| --- | --- | --- | --- | --- |
| L333 | `load` | `epoch_` | `acquire` | 读纪元，感知并发 force_push |
| L335 | `connections()` | `cc_`（经 `acquire` 默认，但此处显式传 `relaxed`） | `relaxed` | 取当前连接位图做决策 |
| L337 | `load` | `ct_` | `relaxed` | 取提交游标起点 |
| L339 | `load` | `rc_` | **`relaxed`** | 看旧消息是否被读空（回收决策） |
| L345 | `load` | `f_ct_` | **`acquire`** | 看槽位提交标志，判断满 |
| L351-352 | `CAS` | `rc_` | **`relaxed`** | 预约槽位（写入新 rc） |
| L353 | `CAS` | `epoch_` | **`acq_rel`** | 校验纪元未被并发改动 |
| L359 | `store` | `ct_` | `release` | 发布提交游标 |
| L362 | `store` | `f_ct_` | **`release`** | 发布「本槽数据已就绪」 |

2. 对照 [src/libipc/prod_cons.h:405-432](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L405-L432) 的 `pop`，找到配对的 `f_ct_.load(acquire)`（L408）——消费方靠它确认数据可读。

### 5.2 核心问题：为什么 rc_ 用 relaxed，f_ct_ 用 release/acquire？

**`f_ct_`（提交标志）是数据的「发布通道」，所以必须 release/acquire：**

生产者在 CAS 循环 `break` 后，执行 `ct_.store(release)` → 写 `data_` → `f_ct_.store(release)`（L362）。消费者 `pop` 先 `f_ct_.load(acquire)`（L408），只有读到 `~cur` 才认为数据就绪、才去读 `data_`（L413）。这正是一对标准的 release/acquire：

\[
\underbrace{\text{写 data_} \to f_{ct}\text{.store(release)}}_{\text{生产者}}
\;\xrightarrow{sw}\;
\underbrace{f_{ct}\text{.load(acquire)} \to \text{读 data_}}_{\text{消费者}}
\]

若 `f_ct_` 不用 release/acquire，消费者可能读到没写完的槽——这是消息正确性的命脉，**强度绝不能降**。

**`rc_`（读计数位图）只管「槽位回收」，不承载消息字节，所以能 relaxed：**

- 在 `push` 里，`rc_` 只被用来算 `rem_cc`（还有哪些读者没读完），决定「这个旧槽能不能复用」。这是一次**回收决策**，不是数据发布。
- 读到**陈旧的** `rc_`（relaxed 允许）只会让生产者**更保守**——以为槽还没被读空而返回 false / 重试，绝不会「以为读空了就提前覆盖」。保守只会拖慢，不会出错；重试一轮自然读到新值。
- 真正保证多生产者之间「预约有序、不互相踩」的同步，被**委托给了 L353 的 `epoch_` CAS(acq_rel)**——它与 `rc_` CAS 在同一个 `&&` 里：`rc_` CAS 成功且 `epoch_` 未被并发改动才 `break`。`acq_rel` 撑起了跨进程的预约握手，`rc_` 只要「原子地改对那几位」即可，故 relaxed 足够。

一句话总结这次分工：**`f_ct_` 管「数据有没有写好让人看」，必须强序；`rc_` 管「旧槽有没有被读空可以复用」，陈旧无害，故用 relaxed，把同步成本让给 `epoch_` 的 acq_rel。**

### 5.3 需要观察的现象

- `rc_` 在 `push` 里两次都是 `relaxed`（L339、L352），而同一变量在 `pop` 里却是 `acquire`/`release`（L415、L426）——印证「内存序是**按使用点**而非按变量定的」。
- `epoch_` 在 `push` 里 `acquire`(L333) + `acq_rel`(L353)，在 `force_push` 里 `release`(L371)——它是并发 force_push 的「纪元护栏」。

**待本地验证**：以两个进程高频对 `channel` 收发（参考 u1-l4 的广播示例），用 `perf c2c` 或伪共享探测工具观察缓存行命中率；正常情况下因 `alignas` 隔行，`ct_`/`epoch_` 不会与 `rc_`/`f_ct_` 互相踢缓存行。

## 6. 本讲小结

- **可见性靠 release/acquire 配对**：`prod_cons.h` 的 SPSC 变体里，`wt_` 与 `rd_` 各自一 release 一 acquire，是数据跨核可见的命脉；改 relaxed 会让消费者读到半写槽。
- **防伪共享靠 `alignas(cache_line_size)`**：`utility.h` 把缓存行宽定义为 64，`prod_cons.h` 对每个被异核高频写的游标（`rd_/wt_/ct_/epoch_`）单独对齐，共 7 处。
- **CAS 内存序按角色选**：读来做决策用 acquire、写来发布用 release、读改写都要用 acq_rel；典型样本是 `conn_head::connect` 的 `acquire+release`、`disconnect` 的 `acq_rel`、单播 `connect` 的 `relaxed`。
- **DCLP 的三档序**：`conn_head_base::init()` 外层 acquire（快路径安全闸）、内层 relaxed（持锁无并发）、收尾 release（发布构造结果），缺一不可、强度最小。
- **分工原则**：`multi-multi-broadcast::push` 里 `f_ct_` 用 release/acquire 扛数据发布，`rc_` 用 relaxed 做无害保守的回收决策，把强同步让给 `epoch_` 的 acq_rel——「内存序按使用点定，强序只给真正承载可见性的操作」。
- **整体哲学**：libipc 的无锁代码不是「处处最强序」，而是「最小必要强度 + 把同步成本集中到少数发布点」，这正是它低延迟的底层原因。

## 7. 下一步学习建议

- 本讲的 `epoch_` acq_rel 与 `force_push` 紧密相关，建议继续读 **u8-l2 健壮锁的崩溃恢复**，看跨进程锁如何在持有者进程死亡后由 `EOWNERDEAD`/`WAIT_ABANDONED` 恢复——那里同样大量出现 acquire/release 与一致性恢复的配合。
- 想把 CAS 栈与空闲链表看透，可读 **u8-l3 intrusive_stack 与 id_pool 无锁结构**：`intrusive_stack` 的 Treiber CAS 栈与本讲讲的 CAS 内存序选择一脉相承，`id_pool` 的 `next_[]` 数组当链表思路也在 u3-l3 的大消息外存里用过。
- 如果想从「测试侧」验证内存序的正确性，可读 **u8-l5 测试体系、扩展点与架构取舍**，看 `test_ipc_channel` 如何在多发送/多接收、超时、强制发送等场景下压测这些无锁路径。
