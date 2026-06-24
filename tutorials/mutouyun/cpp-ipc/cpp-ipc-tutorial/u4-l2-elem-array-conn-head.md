# elem_array 与 conn_head 连接位图

## 1. 本讲目标

本讲承接 u4-l1 的队列抽象层，继续「向下钻一层」，走进真正躺在共享内存里的那个对象——`elem_array`，以及它继承来的连接管理头 `conn_head`。

学完本讲，你应当能够：

- 画出 `elem_array` 在共享内存里的三段式布局（连接头 + 策略头 + 256 槽元素块），并解释为什么是 256 槽；
- 说明 `index_of` 如何把单调递增的 32 位计数器「回绕」成槽位下标，从而实现循环数组；
- 解释 `conn_head_base::init()` 为何要用双重检查锁（DCLP）在共享内存里做一次性初始化；
- 手动演算广播模式下 `connect()` 的位运算 `curr | (curr+1)`、`disconnect()` 的 `fetch_and(~cc_id)`，以及 `conn_count()` 的位计数；
- 看懂 `sender_checker` / `receiver_checker` 的偏特化，理解发送者（一个 `bool`）与接收者（一个座位 bit）为何「不对称」。

本讲只讲「连接位图」这一层，**不**展开 `prod_cons_impl` 里的无锁读写算法（那是 u4-l3、u4-l4 的内容）。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的结论。回顾三个必须分清的「连接标识」：

| 名称 | 类型 | 存放位置 | 语义 |
|------|------|----------|------|
| `conn_head::cc_` | `std::atomic<cc_t>`（32 位） | 共享内存 | **全座位图**：所有接收者共同维护的位图，每位代表一个座位是否被占 |
| `connected_` | `circ::cc_t`（本进程的 `cc_id`） | 进程本地（`queue_conn` 成员） | **本进程座位号**：单个 bit，本接收者自己的座位 |
| `cc_id_` | 原子计数 | 共享内存另一区域 | **身份证号**：单调递增，用于过滤自发消息（u3-l1） |

本讲的主角是第一行——全座位图 `cc_`，以及围绕它的位运算。另外你需要知道：

- `cc_t = uint_t<32> = std::uint32_t`（[elem_def.h:19-20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L19-L20)），这就是广播模式「最多 32 个接收者」限制的根源（每位一个接收者，共 32 位，见 u2-l4）。
- `relat_trait<Policy>` 把策略标签萃取为三个编译期布尔：`is_multi_producer`、`is_multi_consumer`、`is_broadcast`（[def.h:56-67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L56-L67)），本讲的偏特化全部由它驱动。
- `ipc::yield(k)` 是自旋退避函数（四级阶梯：空转→PAUSE→yield→sleep），见 u3-l4、u6-l1，本讲只引用不展开。

## 3. 本讲源码地图

本讲只涉及两个头文件（均为**头文件库**，无 `.cpp`，靠模板在使用处实例化）：

| 文件 | 作用 |
|------|------|
| [src/libipc/circ/elem_def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h) | 定义连接位图的核心：`cc_t` 类型、`index_of` 回绕函数、`conn_head_base`（共享基类 + DCLP 初始化）、`conn_head<P,true>`（广播特化，位运算）与 `conn_head<P,false>`（单播特化，纯计数）。 |
| [src/libipc/circ/elem_array.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h) | 定义躺在共享内存里的 `elem_array`：继承 `conn_head`，内含策略头 `head_` 与 256 槽 `block_`，并用 `sender_checker`/`receiver_checker` 把「连接」路由到正确的实现。 |

被实例化的位置（不在本讲范围，但帮你建立全局）：[policy.h:21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L21) 把 `elem_array<prod_cons_impl<Flag>, DataSize, AlignSize>` 作为 `elems_t`，而 [queue.h:38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L38) 用 `elems_h_.acquire(name, sizeof(Elems))` 申请恰好容纳一个 `elem_array` 的共享内存。

---

## 4. 核心概念与源码讲解

### 4.1 elem_array 的内存布局

#### 4.1.1 概念说明

在 u4-l1 里，`queue_base` 持有一个指针 `elems_`，它指向共享内存里的「环形数组」。但那个指针具体指向一个什么对象？答案就是 `elem_array`。它是真正跨进程共享的那块结构体，三段拼在一起：

```
┌─────────────────────────────────────────────────────────┐
│  conn_head<Policy>  (继承来的 base_t)                    │  ← 连接位图 cc_ / 锁 / constructed_
│  + policy_t head_   (策略头)                              │  ← 读/写游标 rd_ / wt_ / ct_
├─────────────────────────────────────────────────────────┤
│  elem_t block_[256]                                       │  ← 256 个定长槽位
└─────────────────────────────────────────────────────────┘
←——————————— head_size ———————————→←——— block_size ———→
```

- **第一段：连接头**。`elem_array` 继承自 `conn_head<Policy>`，这一段就是 4.2、4.3 要讲的连接位图 `cc_`、自旋锁 `lc_` 和初始化标志 `constructed_`。
- **第二段：策略头 `head_`**。类型是 `policy_t`（即 `prod_cons_impl<Flag>`），存放无锁算法需要的读/写游标（如 `rd_`/`wt_`/`ct_`）。这一段属于 u4-l3、u4-l4。
- **第三段：元素块 `block_[256]`**。256 个定长槽位，每个槽是一个 `elem_t`（载荷区 + 可能的读计数/提交标志）。

代码里把「前两段」的总大小记作 `head_size`，把「第三段」大小记作 `block_size`：

[src/libipc/circ/elem_array.h:27-33](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L27-L33)：

```cpp
enum : std::size_t {
    head_size  = sizeof(base_t) + sizeof(policy_t),
    data_size  = DataSize,
    elem_max   = (std::numeric_limits<uint_t<8>>::max)() + 1, // default is 255 + 1
    elem_size  = sizeof(elem_t),
    block_size = elem_size * elem_max
};
```

这里有个关键常量：`elem_max = 255 + 1 = 256`。它用 8 位无符号整数的最大值加 1，所以队列固定有 **256 个槽位**。这正是 u3-l2 中「每个槽 64 字节」、按 `data_length` 分片的基础。

#### 4.1.2 核心流程：循环数组的回绕

既然有 256 个槽，生产者/消费者的游标（`wt_`/`rd_`/`ct_`）却都是 32 位的 `u2_t`，并且**单调递增、永不回退**。那它怎么映射回 0~255 的槽位？答案是 `index_of`：

[src/libipc/circ/elem_def.h:22-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L22-L24)：

```cpp
constexpr u1_t index_of(u2_t c) noexcept {
    return static_cast<u1_t>(c);
}
```

把 32 位 `c` 强转为 8 位 `u1_t`，本质就是**截取低 8 位**，等价于对 256 取模：

\[ \text{index\_of}(c) = c \bmod 256 \]

于是游标每加到 256 的倍数就自然「绕回」槽 0，无需任何 `if` 判断——这是循环数组最简洁的实现手法。例如游标 `c = 258` 时，`index_of(258) = 2`，即落在第 2 号槽。

#### 4.1.3 源码精读

`elem_array` 的类骨架与成员（[src/libipc/circ/elem_array.h:17-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L17-L37)）：

```cpp
template <typename Policy, std::size_t DataSize,
          std::size_t AlignSize = (ipc::detail::min)(DataSize, alignof(std::max_align_t))>
class elem_array : public ipc::circ::conn_head<Policy> {
public:
    using base_t   = ipc::circ::conn_head<Policy>;
    using policy_t = Policy;
    using cursor_t = decltype(std::declval<policy_t>().cursor());
    using elem_t   = typename policy_t::template elem_t<DataSize, AlignSize>;
    // ... enum : head_size / elem_max / block_size ...
private:
    policy_t head_;
    elem_t   block_[elem_max] {};
    // ... checker 成员（见 4.4）...
};
```

要点解读：

- `base_t = conn_head<Policy>`，连接位图能力靠**继承**获得。
- `elem_t` 由策略 `policy_t::elem_t<DataSize, AlignSize>` 提供——不同 `prod_cons_impl` 特化定义不同的 `elem_t`（单播只有 `data_`，广播多了 `rc_` 读计数，多对多还多 `f_ct_` 提交标志，见 u4-l3/u4-l4）。
- `cursor()` 转发给 `head_.cursor()`：广播模式返回当前写游标，单播单写单读模式恒返回 `0`。
- `push`/`force_push`/`pop`（[elem_array.h:123-137](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L123-L137)）也只是把 `block_` 喂给 `head_` 对应方法，算法逻辑全在 `prod_cons_impl` 里，本讲不展开。

而 [queue.h:38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L38) 用 `sizeof(Elems)` 申请共享内存，说明 **`elem_array` 的完整 `sizeof` 就是这块共享内存的大小**，首字节对齐 `elem_array` 起始处。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「256 槽」与「游标回绕」两个结论。
2. **操作步骤**：
   - 打开 [elem_def.h:16-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L16-L24)，确认 `u1_t = uint_t<8>`、`u2_t = uint_t<32>`、`elem_max = 255+1`。
   - 在 [prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) 中搜索 `index_of(`，观察 `push`/`pop` 如何用 `circ::index_of(wt_.load(...))` 把写游标换算成 `block_` 下标。
3. **需要观察的现象**：所有下标访问都形如 `elems + circ::index_of(某游标)`，而游标本身用 `fetch_add(1)` 单调递增。
4. **预期结果**：你会看到游标可以无限增长（32 位空间足够 40 亿次收发），但物理上只在 256 个槽之间循环复用。
5. **待本地验证**：若想亲见，可参照 u3-l2 的分片练习，发一条 200 字节消息（`data_length=64`），应被切成 4 片占用 4 个连续槽位。

#### 4.1.5 小练习与答案

**练习 1**：若 `wt_` 当前值为 `513`，发送方会写入第几号槽？

**答案**：`index_of(513) = 513 % 256 = 1`，写入第 1 号槽。

**练习 2**：为什么队列槽位数恰好选 256，而不是 128 或 512？

**答案**：因为槽下标用 `u1_t`（8 位）表示，`index_of` 靠截取低 8 位回绕，自然把模数锁定为 256。选 256 既让 `index_of` 退化为一次免费 cast，又让 `elem_max` 用 `uint8_t` 最大值 `+1` 干净表达，避免分支判断。

---

### 4.2 conn_head_base 与 DCLP 一次性初始化

#### 4.2.1 概念说明

`conn_head` 的两种特化（广播 / 单播）都继承自同一个基类 `conn_head_base`。这个基类持有三样「跨进程共享、必须全局只初始化一次」的东西：

- `cc_`：连接位图原子量（本讲主角）。
- `lc_`：一把 `ipc::spin_lock`，用于初始化时互斥。
- `constructed_`：一个 `atomic<bool>`，标记「这块内存是否已经完成构造」。

为什么要专门搞一套初始化机制？因为 **共享内存由操作系统保证零初始化**（`mmap`/`CreateFileMapping` 给出的内存全 0），但 C++ 对象的「零字节」并不等于「已构造」。比如 `std::atomic` 在某些实现下需要构造才能保证内部状态正确。多个进程几乎同时 `acquire` 同一块共享内存时，必须保证**有且仅有一个进程**执行构造，其余进程直接跳过。这就是经典的 **DCLP（Double-Checked Locking Pattern，双重检查锁定模式）**。

#### 4.2.2 核心流程

`init()` 的执行流程（伪代码）：

```
第一次检查（无锁，acquire 读 constructed_）
  ├─ 若已构造 → 直接返回（快路径，绝大多数进程走这里）
  └─ 若未构造 → 加自旋锁 lc_
                  ├─ 第二次检查（锁内，relaxed 读 constructed_）
                  │    ├─ 仍未构造 → placement-new 在 this 上构造 conn_head_base
                  │    │             → store(true, release) 标记完成
                  │    └─ 已构造 → 跳过（别的进程抢先构造了）
                  └─ 释放锁
```

两次检查缺一不可：第一次检查避免每次调用都上锁（性能）；第二次检查防止多个进程同时通过第一次检查后重复构造（正确性）。

#### 4.2.3 源码精读

[src/libipc/circ/elem_def.h:26-51](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L26-L51)：

```cpp
class conn_head_base {
protected:
    std::atomic<cc_t> cc_{0};          // 连接位图
    ipc::spin_lock lc_;                // 初始化互斥锁
    std::atomic<bool> constructed_{false};
public:
    void init() {
        /* DCLP */
        if (!constructed_.load(std::memory_order_acquire)) {       // 第一次检查
            LIBIPC_UNUSED auto guard = ipc::detail::unique_lock(lc_);
            if (!constructed_.load(std::memory_order_relaxed)) {   // 第二次检查
                ::new (this) conn_head_base;                       // placement-new
                constructed_.store(true, std::memory_order_release);
            }
        }
    }
    // ...
    cc_t connections(std::memory_order order = std::memory_order_acquire) const noexcept {
        return this->cc_.load(order);
    }
};
```

要点解读：

- **内存序配合**：快路径用 `acquire` 读 `constructed_`，与构造完成后的 `release` 写配对——保证「看到 `constructed_==true` 的进程，也能看到 placement-new 写下的所有字段」。
- **placement-new `::new (this) conn_head_base`**：在共享内存原地构造，不分配新内存。注意这会重新初始化 `cc_{0}`，但因为此刻 `cc_` 本就是 0（零初始化），所以幂等无害。
- **`connections()`** 是读位图的统一入口，默认 `acquire` 序，被 `prod_cons_impl` 的 `push`/`force_push` 调用以判断「还有没有接收者」。
- 调用方是 [queue.h:46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L46) 的 `elems->init()`——每个进程拿到 `elems` 指针后立刻调一次。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「第一个进程构造、其余进程跳过」的时序。
2. **操作步骤**：
   - 在 [queue.h:31-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L31-L48) 的 `open()` 中，确认 `acquire` 成功后**无条件**调用 `elems->init()`。
   - 设想两个进程 A、B 几乎同时启动并 `open` 同名通道，分别追踪它们的 `init()` 执行路径。
3. **需要观察的现象**：A 先进入锁、执行 placement-new 并 `store(true)`；B 要么在第一次检查就被 A 的 `release` 写挡下（直接返回），要么在锁内第二次检查时发现已构造而跳过。
4. **预期结果**：无论多少进程、何种先后到达，`conn_head_base` 的构造逻辑体**只执行一次**，但所有进程都能安全使用 `cc_`。
5. **待本地验证**：可在 `init()` 的锁内分支各加一行 `printf`（仅用于本地学习，勿提交），多进程跑 `send_recv` 观察打印次数。

#### 4.2.5 小练习与答案

**练习 1**：为什么第一次检查用 `acquire`、第二次检查（锁内）却用 `relaxed`？

**答案**：第一次检查在锁外，必须用 `acquire` 与构造方的 `release` 配对，才能在看到 `true` 的同时看到构造结果。第二次检查已在锁内，锁本身的 `acquire`/`release` 语义已经建立了必要的同步与可见性，所以读 `constructed_` 用最轻的 `relaxed` 即可。

**练习 2**：如果去掉第二次检查，会有什么后果？

**答案**：两个进程可能同时通过第一次检查（都读到 `false`），然后排队进锁。第一个构造完、置 `true`、释放锁；第二个进锁后若不再检查，就会**再构造一次**，可能把已被前者置位的 `cc_` 重置回 0，导致已连接的接收者「掉座」。

---

### 4.3 广播位运算连接（conn_head 的 broadcast 特化）

#### 4.3.1 概念说明

`conn_head` 是个主模板加两个偏特化，按 `relat_trait<P>::is_broadcast` 分派：

[src/libipc/circ/elem_def.h:53-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L53-L54)：

```cpp
template <typename P, bool = relat_trait<P>::is_broadcast>
class conn_head;
```

- `conn_head<P, true>`：**广播特化**（route / channel 走这条）。`cc_` 是**位图**，每个接收者占 1 bit，连接就是「找一个空闲 bit 并置 1」，断开就是「清掉自己的 bit」。本模块主角。
- `conn_head<P, false>`：**单播特化**。`cc_` 只是个**计数器**（`fetch_add`/`fetch_sub`），不分位，故没有 32 上限。

广播特化要做的事，用一句话概括：**在 32 位图里，用位运算无锁地抢占最低空闲位作为自己的座位号**。这正是 u2-l4 所说的「`connect` 用 `curr|(curr+1)` 抢最低空闲位」的实装现场。

#### 4.3.2 核心流程：connect / disconnect / conn_count

**connect（抢座位）** 的位运算精髓：

```
curr = cc_.load()              # 当前位图
next = curr | (curr + 1)       # 关键一步：找出最低的 0 位并置 1
若 next == curr  →  位图已满（32 位全 1），返回 0 表示失败
CAS(curr → next) 成功 →  返回 next ^ curr   # 即「被新置位的那一个 bit」
```

为什么 `curr | (curr+1)` 能「找到最低 0 位并置 1」？设 `curr` 最低的 0 位在第 `k` 位（比它低的位全为 1）。`curr + 1` 会让低 `k` 位那一串连续的 1 进位归零、并在第 `k` 位变成 1，更高位不变。于是 `curr | (curr+1)` 恰好把第 `k` 位补成 1，同时低 `k` 位仍保持 1。该位与原值的差异就是 `(curr+1) & ~curr = 1 << k`，也等于 `next ^ curr`——它就是新连接者的**单一 bit 座位号 `cc_id`**。

**disconnect（归还座位）**：

```
返回值 = cc_.fetch_and(~cc_id) & ~cc_id
```

`fetch_and(~cc_id)` 原子地把 `cc_id` 那一位清零（归还），返回值是「清零后的新位图」。

**conn_count（数在线接收者）**——经典 Brian Kernighan 位计数：

```
for (cnt = 0; cur; ++cnt) cur &= cur - 1;
```

`cur & (cur-1)` 每次清掉最低一个 1，循环次数恰为置位数，即在线接收者数。

#### 4.3.3 源码精读

广播特化完整实现 [src/libipc/circ/elem_def.h:56-87](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L56-L87)：

```cpp
template <typename P>
class conn_head<P, true> : public conn_head_base {
public:
    cc_t connect() noexcept {
        for (unsigned k = 0;; ipc::yield(k)) {
            cc_t curr = this->cc_.load(std::memory_order_acquire);
            cc_t next = curr | (curr + 1);   // 找首个 0 位并置 1
            if (next == curr) {
                return 0;                     // 座位已满
            }
            if (this->cc_.compare_exchange_weak(curr, next, std::memory_order_release)) {
                return next ^ curr;           // 返回新连接者的 cc_id（单一 bit）
            }
        } // CAS 失败则 yield 退避后重试
    }

    cc_t disconnect(cc_t cc_id) noexcept {
        return this->cc_.fetch_and(~cc_id, std::memory_order_acq_rel) & ~cc_id;
    }

    bool connected(cc_t cc_id) const noexcept {
        return (this->connections() & cc_id) != 0;
    }

    std::size_t conn_count(std::memory_order order = std::memory_order_acquire) const noexcept {
        cc_t cur = this->cc_.load(order);
        cc_t cnt;
        for (cnt = 0; cur; ++cnt) cur &= cur - 1;   // Kernighan 位计数
        return cnt;
    }
};
```

要点解读：

- **CAS 循环 + `yield(k)`**：多个接收者并发连接时会竞争同一个 `cc_`，CAS 失败者经 `yield(k)` 退避（见 u3-l4）后重读重试，保证无锁安全。`connect` 的「副作用」是写 `cc_`，这与 u3-l4 的 `wait_for` 自旋谓词模式一脉相承。
- **返回 `next ^ curr`**：这是单个 bit（如 `0b0010`），后续广播读计数（u4-l4 的 `rc_` 位域）就用它标记「哪些接收者还没读」。
- **满座判断 `next == curr`**：当 `cc_ = 0xFFFFFFFF`（32 位全 1）时，`curr+1` 在无符号 32 位下**溢出回绕为 0**，于是 `next = curr | 0 = curr`，触发 `return 0`——这正是 u2-l4 第 33 个接收者连不上的根因。
- **`connected(cc_id)`**：判断某个座位号是否仍在线，广播 `pop` 在判定「该消息是否还有人在读」时会用到。

作为对照，单播特化 [src/libipc/circ/elem_def.h:89-115](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L89-L115) 完全不分位：`connect()` 就是 `fetch_add(1)+1`（返回递增计数），`conn_count()` 直接返回 `cc_` 本身。这也解释了为何单播没有 32 上限——它的 `cc_` 是计数器不是位图。

#### 4.3.4 代码实践（手动演算）

本实践对应任务规格指定的演算题。

1. **实践目标**：亲手算一遍 `connect`/`disconnect` 的位运算，确认对位图机制的理解。
2. **操作步骤与演算**：

   **初始**：`cc_ = 0b1010`（十进制 10，座位 1 和 3 已占，最低空闲位是 bit 0）。

   **connect()**：
   - `curr = 0b1010`
   - `curr + 1 = 0b1011`
   - `next = curr | (curr+1) = 0b1010 | 0b1011 = 0b1011`
   - `next != curr`，假设 CAS 成功 → `cc_` 变为 **`0b1011`**（十进制 11）
   - 返回 `cc_id = next ^ curr = 0b1011 ^ 0b1010 = 0b0001`（十进制 **1**，即 bit 0）

   **disconnect(1)**（`cc_id = 0b0001`）：
   - `~cc_id = ~0b0001 = 0b...11111110`
   - `fetch_and(~cc_id)` 把 `cc_` 从 `0b1011` 变为 `0b1011 & 0b...1110 = 0b1010`
   - `cc_` 恢复为 **`0b1010`**（十进制 10），返回值也是 `0b1010`

3. **需要观察的现象**：connect 抢到了最低空闲位 bit 0，返回的 `cc_id` 恰是该位；disconnect 把同一位置零，位图精确复原。
4. **预期结果**：`connect` 返回 `1`、`cc_` 变 `0b1011`；`disconnect(1)` 后 `cc_` 回到 `0b1010`，与初始完全一致。
5. **延伸（待本地验证）**：可写一段小程序，用一个 `std::atomic<uint32_t>` 模拟 `cc_`，复刻 `connect`/`disconnect` 的位运算，循环连接 32 次确认第 33 次 `connect()` 返回 0。

#### 4.3.5 小练习与答案

**练习 1**：`cc_ = 0b0110`（十进制 6）时，`connect()` 返回什么、`cc_` 变成什么？

**答案**：`curr=0b0110`，`curr+1=0b0111`，`next=0b0110|0b0111=0b0111`，`cc_id=0b0111^0b0110=0b0001`（bit 0）。返回 `1`，`cc_` 变为 `0b0111`（7）。

**练习 2**：为什么 `disconnect` 用 `fetch_and(~cc_id)` 而不是直接 `cc_ &= ~cc_id`？

**答案**：`fetch_and` 是**原子**操作，保证在多个接收者并发断开时不会丢失更新；而 `cc_ &= ~cc_id` 是「读-改-写」三步，非原子，并发下可能互相覆盖。

**练习 3**：`conn_count()` 用 Kernighan 算法而不是 `__builtin_popcount`，可能出于什么考虑？

**答案**：可移植性。`__builtin_popcount` 是 GCC/Clang 内建，MSVC 没有同名接口；手写的 `cur &= cur-1` 循环纯标准 C++，跨 Linux/Windows/FreeBSD 三平台行为一致，且接收者数最多 32，循环上限很低，性能足够。

---

### 4.4 sender_checker / receiver_checker：连接的收发分派

#### 4.4.1 概念说明

`conn_head` 只提供了「接收者」的位图连接（`connect()`/`disconnect()`）。但 `elem_array` 既管发送者也管接收者，且二者**不对称**：

- **发送者**：无论几个发送者，互相之间不需要区分「谁是谁」，只要知道「有没有发送者在场」即可。所以发送者侧只占一个 `bool` 标志位（单生产者要抢、多生产者恒真）。
- **接收者**：广播模式下必须区分每个接收者（谁读了、谁没读），所以每个接收者占 `cc_` 里的一个 bit。

`elem_array` 用两个偏特化模板 `sender_checker` 和 `receiver_checker` 把「连接请求」路由到正确的实现，路由依据正是 `relat_trait` 的 `is_multi_producer` / `is_multi_consumer`。

#### 4.4.2 核心流程：四种组合

发送者检查器按 `is_multi_producer` 二选一：

| `is_multi_producer` | 实现 | `connect()` 行为 |
|---|---|---|
| `true`（多生产者，如 channel） | `sender_checker<P,true>` | 恒返回 `true`，无需抢占 |
| `false`（单生产者，如 route） | `sender_checker<P,false>` | 用 `atomic_flag::test_and_set` 抢占，抢到才返回 `true` |

接收者检查器按 `is_multi_consumer` 二选一：

| `is_multi_consumer` | 实现 | `connect()` 行为 |
|---|---|---|
| `true`（多消费者） | `receiver_checker<P,true>` | 直接调 `conn.connect()` 抢一个 bit |
| `false`（单消费者） | `receiver_checker<P,false>` | 先复用单生产者的 `atomic_flag` 抢占，成功后再调 `conn.connect()` |

于是 `elem_array::connect_receiver()` 会自动按策略走到正确的组合，调用方（`queue_base`）完全无感。

#### 4.4.3 源码精读

发送者检查器 [src/libipc/circ/elem_array.h:45-69](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L45-L69)：

```cpp
template <typename P, bool> struct sender_checker;
template <typename P>                         // 多生产者
struct sender_checker<P, true> {
    constexpr static bool connect() noexcept { return true; }   // 恒真
    constexpr static void disconnect() noexcept {}
};
template <typename P>                         // 单生产者
struct sender_checker<P, false> {
    bool connect() noexcept { return !flag_.test_and_set(std::memory_order_acq_rel); }
    void disconnect() noexcept { flag_.clear(); }
private:
    std::atomic_flag flag_ = ATOMIC_FLAG_INIT;   // 也放在共享内存，初值为 0
};
```

接收者检查器 [src/libipc/circ/elem_array.h:71-93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L71-L93)：

```cpp
template <typename P>                         // 多消费者
struct receiver_checker<P, true> {
    constexpr static cc_t connect(base_t &conn) noexcept { return conn.connect(); }
    constexpr static cc_t disconnect(base_t &conn, cc_t cc_id) noexcept { return conn.disconnect(cc_id); }
};
template <typename P>                         // 单消费者：复用单生产者的 atomic_flag 抢占
struct receiver_checker<P, false> : protected sender_checker<P, false> {
    cc_t connect(base_t &conn) noexcept {
        return sender_checker<P, false>::connect() ? conn.connect() : 0;  // 先抢 flag，再抢 bit
    }
    cc_t disconnect(base_t &conn, cc_t cc_id) noexcept {
        sender_checker<P, false>::disconnect();
        return conn.disconnect(cc_id);
    }
};
```

最后，成员实例化与对外接口 [src/libipc/circ/elem_array.h:95-117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L95-L117)：

```cpp
sender_checker  <policy_t, relat_trait<policy_t>::is_multi_producer> s_ckr_;
receiver_checker<policy_t, relat_trait<policy_t>::is_multi_consumer> r_ckr_;

using base_t::connect;      // 私有化基类的 connect，强制走 checker
using base_t::disconnect;
public:
    bool connect_sender()   noexcept { return s_ckr_.connect(); }
    void disconnect_sender() noexcept { return s_ckr_.disconnect(); }
    cc_t  connect_receiver()    noexcept { return r_ckr_.connect(*this); }
    cc_t  disconnect_receiver(cc_t cc_id) noexcept { return r_ckr_.disconnect(*this, cc_id); }
```

要点解读：

- `elem_array` 通过 `using base_t::connect;`（放在 `private` 区，[elem_array.h:98-100](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L98-L100)）把基类的裸 `connect`/`disconnect` **藏起来**，外部只能走 `connect_sender`/`connect_receiver`，强制经过 checker 的多重性判定。
- `relat_trait<policy_t>` 能直接工作，是因为 def.h 的「剥壳」特化 `relat_trait<Policy<Flag>> : relat_trait<Flag>`（[def.h:66-67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L66-L67)）把 `prod_cons_impl<Flag>` 外层剥掉，拿到最内层 `wr<...>` 的布尔。
- 单消费者 `receiver_checker<P,false>` 继承 `sender_checker<P,false>`，复用其 `atomic_flag`——这是「单消费者」天然只有一个竞争者时的轻量化处理。
- `force_push` 在某些特化里会调 `disconnect_receiver(~cc_t(0))`（见 [prod_cons.h:57](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L57)），这里的「全 1 哨兵」会在单播特化里触发「清空所有连接」分支（[elem_def.h:96-101](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L96-L101)），是 `force_push` 驱逐失效读者的入口。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：确认 route 与 channel 分别走到哪条 checker 分支。
2. **操作步骤**：
   - 回顾 u2-l1/u2-l4：`route = wr<single, multi, broadcast>`，`channel = wr<multi, multi, broadcast>`。
   - 对 `route`：`is_multi_producer=false`、`is_multi_consumer=true`。推出 `s_ckr_ = sender_checker<P,false>`（单生产者抢占）、`r_ckr_ = receiver_checker<P,true>`（直接抢 bit）。
   - 对 `channel`：`is_multi_producer=true`、`is_multi_consumer=true`。推出 `s_ckr_ = sender_checker<P,true>`（恒真）、`r_ckr_ = receiver_checker<P,true>`。
3. **需要观察的现象**：route 的第二个发送者 `connect_sender()` 会因 `atomic_flag` 已被置位而返回 `false`；channel 的任意多个发送者都返回 `true`。
4. **预期结果**：route 实际只允许一个发送者（与 u2-l4「单写多读」一致），channel 允许多个。
5. **待本地验证**：起两个进程都拿 `ipc::route("x", ipc::sender)`，第二个应连不上或行为受限（route 单生产者语义）。

#### 4.4.5 小练习与答案

**练习 1**：为什么单消费者 `receiver_checker<P,false>` 要先抢 `atomic_flag` 再抢 bit？

**答案**：单消费者意味着「只允许一个接收者」。`atomic_flag` 的 `test_and_set` 保证只有一个接收者能通过（返回 `true`），其余直接返回 `0`（连不上），从而把「单消费者」这一多重性约束强制落实在连接层，而不只是依赖使用者的自觉。

**练习 2**：`elem_array` 把 `base_t::connect` 放进 `private` 区的目的是什么？

**答案**：强制外部不能直接调裸 `conn_head::connect()`，必须走 `connect_receiver()`/`connect_sender()`，从而保证每次连接都经过 `checker` 的多重性判定，避免绕过单生产者/单消费者约束。

---

## 5. 综合实践

把本讲四块知识串成一个端到端的「连接生命周期」追踪任务。

**任务**：以 `ipc::channel("demo-conn", ipc::receiver)` 为例，从「进程拿到共享内存指针」一路追到「拿到一个 `cc_id` 座位号」，画出完整调用链与每一步发生的位运算。

**建议步骤**：

1. 从 [queue.h:38-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L38-L47) 的 `open()` 出发：`acquire(name, sizeof(Elems))` → `elems->init()`。
2. 标注 `init()` 走的 DCLP 路径（首进程构造、后续进程跳过，对应 4.2）。
3. 进入 `queue_base` 的接收者连接逻辑，找到 `connect_receiver()` 调用点（对应 4.4）。
4. 因 channel 是 `multi/multi/broadcast`，`connect_receiver` → `receiver_checker<P,true>::connect` → `conn_head<P,true>::connect`（对应 4.3）。
5. 手算一次 `connect()`：假设你是第一个接收者，`cc_` 从 `0` 出发，写出 `curr`、`next`、`cc_id` 的值。

**预期结论**：首接收者 `curr=0`，`curr+1=1`，`next=0|1=1`，`cc_id=1^0=1`（bit 0），`cc_` 变为 `0b0001`。你的座位号就是 `0b0001`，后续广播消息的读计数位图里，bit 0 就代表「你」。把这条链路连同位运算写进一张时序图，本讲就真正贯通了。

## 6. 本讲小结

- `elem_array` 是躺在共享内存里的三段式结构：连接头 `conn_head` + 策略头 `head_` + 256 槽 `block_`；`sizeof(elem_array)` 即共享内存大小。
- 256 个槽源于 `elem_max = uint8_max + 1`，循环回绕靠 `index_of(c) = static_cast<u1_t>(c)` 即 `c % 256`，零分支。
- `conn_head_base::init()` 用 DCLP（双重检查锁 + `acquire`/`release`）保证多进程下共享内存里的对象**只被构造一次**。
- 广播特化 `conn_head<P,true>` 用位运算管理座位：`connect` 用 `curr|(curr+1)` 抢最低空闲位、CAS 循环 + `yield` 退避、返回 `next^curr` 作为单一 bit 的 `cc_id`；`disconnect` 用 `fetch_and(~cc_id)` 归还；满座时 `curr+1` 回绕为 0 → `next==curr` → 返回 0，这就是 32 上限的根因。
- 单播特化 `conn_head<P,false>` 不分位、纯计数，故无 32 限制；`conn_count` 用 Kernighan 算法数位。
- `sender_checker`/`receiver_checker` 按 `is_multi_producer`/`is_multi_consumer` 偏特化，体现「发送者占一个 bool、接收者占一个 bit」的不对称设计；`elem_array` 把基类裸 `connect` 私有化，强制连接走 checker。

## 7. 下一步学习建议

本讲只解决了「连接位图」这一层，完全没有展开读写游标本身的无锁算法。接下来：

- **u4-l3（prod_cons 单播变体）**：进入 `prod_cons_impl`，看 `single-single` 最简环形队列如何用 `rd_`/`wt_` 判满判空，以及 `single-multi` 如何用 CAS 抢占 `rd_`、`multi-multi` 如何用 `ct_` 提交索引 + `f_ct_` 标志协议协调多生产者。建议重点关注本讲提到的 `alignas(cache_line_size)` 与内存序，它们会在那里大量出现。
- **u4-l4（prod_cons 广播变体）**：看本讲返回的 `cc_id` 如何被装进 `elem_t::rc_` 读计数位域（连接掩码 + epoch），实现「一写多读」下每个接收者各自标记「已读」、最后一人回收槽位。这是本讲连接位图与无锁队列的合流点。
- 复习 **u8-l1（内存序、伪共享与缓存行）**：等读完 u4-l3/u4-l4，再回来对照本讲 DCLP 与 CAS 中的 `acquire`/`release`/`relaxed` 选择，会有更深的体会。
