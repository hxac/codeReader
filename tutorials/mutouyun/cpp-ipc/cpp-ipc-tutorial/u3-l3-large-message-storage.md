# 大消息的外部存储（chunk + id_pool + 引用计数）

## 1. 本讲目标

在 u3-l2 中我们看到：当一条消息比队列单槽（`data_length = 64` 字节）大时，库会把它**分片**成多片逐个塞进无锁队列，接收端再用 `thread_local` 的 `recv_cache` 按 `msg.id_` 重组。

但分片并不是唯一的路径。本讲要讲清楚 libipc 的**第二条大消息通路——外部存储（external storage）**：

- 为什么大消息要被挪到队列之外的「chunk 仓库」，而不是一直分片？
- 这个仓库由哪些数据结构组成：`chunk_t`、`chunk_info_t`、`id_pool`？
- 发送端如何 `acquire` 一个 chunk、写入数据、只把一个 4 字节的 `storage_id` 塞进队列？
- 接收端如何用这个 id `find` 回真实数据，做到**零拷贝**直读共享内存？
- 在广播模式下，同一条消息被多个接收者共享，如何用**位图引用计数**让「最后一个读的人」负责归还 chunk？

学完本讲，你应该能完整跟踪一条 100KB 消息从 `send` 到所有接收者 `recv` 并最终回收的内存生命周期。

## 2. 前置知识

本讲建立在 u3-l2 已建立的概念之上，请确保你已经理解：

- **`msg_t` 定长头部**：`cc_id_`（发送者身份证号）、`id_`（消息号/分片重组键）、`remain_`（本片到消息末尾的字节数）、`storage_`（是否走外部存储的标志位）。
- **队列单槽容量 `data_length = 64`**：无锁队列里每个槽最多放 64 字节载荷。
- **`buff_t` 的析构器**：`buffer(p, size, destructor)` 在析构时回调 `destructor`，这是大消息零拷贝回收的挂载点（见 u2-l2）。
- **连接位图 `cc_t` / 连接 id `cc_id`**：广播模式下每个接收者占连接位图中的 1 个 bit（见 u2-l4）。本讲会**复用这套位运算**来做引用计数，这是最关键的承接点。

另外需要一点数据结构直觉：

- **空闲链表（free list）**：用一个数组本身当链表来管理「哪些槽是空闲的」，无需额外分配节点。本讲的 `id_pool` 就是经典实现。
- **引用计数（reference counting）**：多个使用者共享同一份资源，每用完一个就「减一」，归零时才真正释放。

## 3. 本讲源码地图

本讲集中在两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 大消息仓库的全部实现：`chunk_t`/`chunk_info_t` 内存布局、`calc_chunk_size` 对齐、`acquire/find/release_storage`、`sub_rc`/`recycle_storage` 引用计数，以及 `send`/`recv` 中接入这条通路的代码。 |
| [src/libipc/utility/id_pool.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h) | 空闲链表分配器 `id_pool`，是 chunk 仓库「发牌/收牌」的核心结构。 |

还会用到 `def.h` 中的常量（已在 u2-l1 介绍）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `data_length` | `64` | 队列单槽载荷字节 |
| `large_msg_limit` | `data_length`(=64) | 超过此值就尝试走外部存储 |
| `large_msg_align` | `1024` | chunk 尺寸按 1KB 对齐 |
| `large_msg_cache` | `32` | 每个 chunk 仓库最多缓存 32 个 chunk |

> 这四个常量定义在 [include/libipc/def.h:33-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L33-L39)。

---

## 4. 核心概念与源码讲解

### 4.1 大消息为何要走「外部存储」而非分片

#### 4.1.1 概念说明

回顾 u3-l2 的分片路径：一条 N 字节的消息会被切成 ⌈N / 64⌉ 片，每一片都要单独 `push` 进无锁队列。当 N 很大（比如 1MB）时，这会带来三个问题：

1. **队列被占满**：无锁循环队列的容量是有限的（槽数固定）。一条大消息可能独占整个队列，阻塞其它消息。
2. **多次 CAS**：每一片都要走一遍无锁 `push`，多生产者/多消费者下还要抢索引，延迟随片数线性增长。
3. **接收端逐片拷贝**：接收方要把每一片 `memcpy` 拼到 `recv_cache` 里，对大消息是实打实的拷贝开销。

libipc 的对策是：**大消息不进队列本体，而是放进一个独立的「chunk 仓库」（另一块共享内存），队列里只传一个 4 字节的「票据」(`storage_id`) 指向那个 chunk。** 接收方凭票去仓库取数据，直接拿到指向共享内存的指针——**零拷贝**。

> 注意：分片路径仍然是**兜底（fallback）**。当仓库分配失败（比如 32 个 chunk 全占满）时，代码会回落到分片，保证消息仍能发出去。这一点在 4.3 节的源码里会清楚看到。

那么「大」的阈值是多少？答案是 `large_msg_limit = data_length = 64` 字节——**只要消息超过 64 字节就优先走外部存储**。这意味着在默认配置下，绝大多数真实消息（远大于 64B）都会走本讲这条通路。

#### 4.1.2 核心流程

发送端接入外部存储的整体走向（伪代码）：

```
detail_impl::send(data, size):
    if size > large_msg_limit:                     # 大消息
        (id, buf) = acquire_storage(inf, size, conns)   # 向仓库申请一个 chunk
        if buf != nullptr:
            memcpy(buf, data, size)                # 整条消息一次性拷进 chunk
            try_push(remain = size - data_length,
                     data = &id,  size = 0)        # 队列里只发 4 字节票据
            return
        # 否则回落到分片
    # 分片路径（u3-l2）
    for each 64B fragment: try_push(...)
```

接收端对称地「凭票取货」：

```
detail_impl::recv():
    pop 一条 msg
    if msg.storage_:                               # 这是大消息票据
        id = *(storage_id_t*)&msg.data_           # 读出 4 字节票据
        buf = find_storage(id, inf, msg_size)      # 凭票取回共享内存指针
        return buff_t{buf, msg_size, 回收析构器}    # 零拷贝，析构时引用计数回收
    # 否则按分片重组（u3-l2）
```

下面我们逐块拆解仓库本身。

---

### 4.2 chunk 内存布局与 chunk_size 的 1KB 对齐

#### 4.2.1 概念说明

「chunk」就是仓库里存放**一条**大消息的存储单元。一个 chunk 由两部分组成：

- **头部**：一个 `std::atomic<cc_t>`（`cc_t` 是 32 位连接位图），记录「这条消息还需要被哪些接收者读取」。这就是引用计数的载体。
- **载荷区**：紧跟在头部之后，存放真正的消息字节。

仓库把**同一尺寸的 chunk 们**排成一排，放在一块连续的共享内存里，用数组下标（`storage_id`）来寻址。为了让每块内存的边界都落在整齐的地址上（方便对齐访问、方便按 `chunk_size` 做下标乘法），所有 chunk 的尺寸都被向上取整到 **1KB (`large_msg_align = 1024`) 的整数倍**。

#### 4.2.2 核心流程：calc_chunk_size

chunk 尺寸由一个嵌套的对齐公式计算：

```cpp
IPC_CONSTEXPR_ std::size_t align_chunk_size(std::size_t size) noexcept {
    return (((size - 1) / ipc::large_msg_align) + 1) * ipc::large_msg_align;
}

IPC_CONSTEXPR_ std::size_t calc_chunk_size(std::size_t size) noexcept {
    return ipc::make_align(alignof(std::max_align_t), align_chunk_size(
           ipc::make_align(alignof(std::max_align_t), sizeof(std::atomic<ipc::circ::cc_t>)) + size));
}
```

> 见 [src/libipc/ipc.cpp:177-184](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L177-L184)。

其中 `make_align(align, size) = (size + align - 1) & ~(align - 1)`，即把 `size` 向上取整到 `align` 的整数倍（见 [utility.h:47-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/utility.h#L47-L50)）。

从内到外拆解这个公式：

1. `sizeof(std::atomic<cc_t>)`：头部原子量本身的大小，`cc_t` 是 `uint32`，故为 **4 字节**。
2. `make_align(max_align, 4)`：把头部向上对齐到 `max_align_t`（通常 8 字节）→ **8 字节**。这就是 chunk 头部实际占用的槽位。
3. `+ size`：头部槽位 + 消息字节数 = chunk 至少需要的原始字节数。
4. `align_chunk_size(...)`：再把整体向上取整到 **1024 字节**的整数倍。
5. 最外层 `make_align(max_align, ...)`：最后再对齐一次到 `max_align_t`。由于 1024 已经是 8/16 的倍数，这一步在此通常是 no-op。

本质上：**chunk_size = 把「8 字节头 + 消息字节数」向上取整到 1KB 的整数倍。**

#### 4.2.3 源码精读：chunk_t 与 chunk_info_t

`chunk_t` 用两段 `reinterpret_cast` 把同一块内存「看作」头部 + 载荷：

```cpp
struct chunk_t {
    std::atomic<ipc::circ::cc_t> &conns() noexcept {
        return *reinterpret_cast<std::atomic<ipc::circ::cc_t> *>(this);   // 头部就是 chunk 起始处
    }
    void *data() noexcept {
        return reinterpret_cast<ipc::byte_t *>(this)                      // 跳过头部 8 字节
             + ipc::make_align(alignof(std::max_align_t), sizeof(std::atomic<ipc::circ::cc_t>));
    }
};
```

> 见 [src/libipc/ipc.cpp:186-195](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L186-L195)。`conns()` 取头部原子量，`data()` 返回载荷区起点（即 chunk 起点 + 8 字节）。

`chunk_info_t` 是「仓库管理员」：它持有一个 `id_pool`（记录哪些 chunk 空闲）、一把 `spin_lock`（保护并发分配），以及紧跟其后的 32 个 chunk 的连续内存：

```cpp
struct chunk_info_t {
    ipc::id_pool<> pool_;
    ipc::spin_lock lock_;

    IPC_CONSTEXPR_ static std::size_t chunks_mem_size(std::size_t chunk_size) noexcept {
        return ipc::id_pool<>::max_count * chunk_size;          // 32 * chunk_size
    }
    ipc::byte_t *chunks_mem() noexcept {
        return reinterpret_cast<ipc::byte_t *>(this + 1);       // chunk 数组紧跟在 info 之后
    }
    chunk_t *at(std::size_t chunk_size, ipc::storage_id_t id) noexcept {
        if (id < 0) return nullptr;
        return reinterpret_cast<chunk_t *>(chunks_mem() + (chunk_size * id));  // 按下标寻址
    }
};
```

> 见 [src/libipc/ipc.cpp:197-213](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L197-L213)。

于是**某种 chunk_size 对应的共享内存布局**是：

```
[ chunk_info_t (pool_ + lock_) ][ chunk_0 ][ chunk_1 ] ... [ chunk_31 ]
                                  ↑ id=0      ↑ id=1            ↑ id=31
```

整块区域大小 = `sizeof(chunk_info_t) + 32 * chunk_size`。

注意一个关键设计：**chunk_size 不同的消息会落到不同的共享内存区域**。每种尺寸都有自己的 `chunk_info_t` 和 32 个槽。仓库按 `chunk_size` 分桶（见 4.3.2 的 `chunk_storage_info`）。

#### 4.2.4 代码实践：手算一条 100KB 消息的 chunk_size

1. **实践目标**：用 `calc_chunk_size` 公式手动算出 100KB 消息对应的 chunk 尺寸，理解 1KB 对齐的效果。
2. **操作步骤**：取 `size = 100 * 1024 = 102400`，按 4.2.2 的五步代入（假设头部对齐到 8 字节）。
3. **演算过程**：
   - 头部：`make_align(8, 4)` = `8`
   - 原始字节：`8 + 102400` = `102408`
   - 1KB 对齐：`((102408 - 1) / 1024 + 1) * 1024` = `((102407 / 1024) + 1) * 1024`。因为 `1024 × 100 = 102400 ≤ 102407 < 1024 × 101`，整数除法 `102407 / 1024 = 100`，所以 = `(100 + 1) × 1024` = `101 × 1024` = **`103424`**
   - 最外层对齐：`103424` 已是 8 的倍数 → `103424`
4. **预期结果**：100KB 的消息，chunk_size = **103424 字节（约 101KB）**。多出来的 ~1KB 来自「8 字节头部 + 向上取整到 1KB 边界」。这就直观体现了「1KB 对齐」：载荷的边界永远落在 1024 的整数倍上。
5. **平台说明**：以上假设 `alignof(std::max_align_t) == 8`（典型 64 位 Linux）。若你的平台是 16（如部分含 `long double` 的工具链），头部槽位变为 16 字节，最终结果仍是 1KB 对齐的某个倍数，结论不变。可在本机用 `printf("%zu", alignof(std::max_align_t));` 确认。

> 待本地验证：可在 `test/` 下写一行打印 `calc_chunk_size(102400)` 的程序，对比手算结果（注意 `calc_chunk_size` 在匿名命名空间内，需在 `ipc.cpp` 同翻译单元内调用，或自行复刻公式验证）。

#### 4.2.5 小练习与答案

**练习 1**：一条刚好 64 字节的消息会走外部存储吗？
**答案**：不会。判定条件是 `size > large_msg_limit`，而 `large_msg_limit = 64`，64 不大于 64，故走分片路径（对 64 字节而言就是单片）。

**练习 2**：两条消息分别是 500 字节和 900 字节，它们的 chunk 是否落在同一块共享内存里？
**答案**：不会。500 字节和 900 字节经过 `calc_chunk_size` 后都向上取整到 1024 字节——等等，这里要小心：500→`make_align(8,4)+500 = 508`→`align_chunk_size(508)=1024`；900→`908`→`align_chunk_size(908)=2048`。两者 chunk_size 不同（1024 vs 2048），所以分属两块不同的共享内存区域（见 4.3.2 按 chunk_size 分桶）。这也说明：**只要消息字节数跨越了 1KB 边界，就会进不同的桶**。

---

### 4.3 id_pool：用数组当链表的空闲槽分配器

#### 4.3.1 概念说明

仓库有 32 个 chunk 槽，需要回答两个问题：**哪些槽是空闲的？** 和 **如何 O(1) 分配/回收一个槽？** libipc 用经典的「intrusive free list（侵入式空闲链表）」解决——**复用 `next_[]` 数组本身来存链表的「下一个」指针**，不额外分配节点。

`id_pool` 是一个**纯数据结构**，本身不带锁（并发安全由外层 `chunk_info_t::lock_` 保证）。它被放进共享内存，所以还需要一个「首次进入时初始化」的机制（`prepare`/`init`/`invalid`）。

#### 4.3.2 核心流程

`id_pool` 的状态由三个字段刻画：

```cpp
id_type<DataSize, AlignSize> next_[max_count];   // 既存数据，又当链表节点
uint_t<8> cursor_ = 0;                           // 链表头指针（指向下一个空闲槽下标）
bool prepared_ = false;                           // 是否已初始化过
```

> 见 [id_pool.h:50-53](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L50-L53)。

`max_count` 的上限同时受 `large_msg_cache (32)` 和 `uint8` 的取值范围（255）约束，取较小者：

```cpp
static constexpr std::size_t limited_max_count() {
    return ipc::detail::min<std::size_t>(large_msg_cache, (std::numeric_limits<uint_t<8>>::max)());
}
enum : std::size_t { max_count = limited_max_count() };   // = min(32, 255) = 32
```

> 见 [id_pool.h:40-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L40-L48)。这也是「每桶最多 32 个 chunk」的来源——同时受 `uint8` cursor 范围和 `large_msg_cache` 双重夹紧。

**初始化（首次进入共享内存时）**：把每个槽指向下一个，形成 `0 → 1 → 2 → ... → 31 → 32(=max_count, 哨兵)` 的链：

```cpp
void init() {
    for (storage_id_t i = 0; i < max_count;) {
        i = next_[i] = (i + 1);          // next_[i] = i+1，i 再自增
    }
}
```

> 见 [id_pool.h:61-65](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L61-L65)。循环结束后 `next_[31] = 32 = max_count`，即链表末尾哨兵。

`prepare()` 负责懒初始化——只有当这块共享内存「看起来还是全零（未初始化）」时才 `init()`：

```cpp
void prepare() {
    if (!prepared_ && this->invalid()) this->init();
    prepared_ = true;
}
bool invalid() const {
    static id_pool inv;
    return std::memcmp(this, &inv, sizeof(id_pool)) == 0;   // 与全零实例逐字节比较
}
```

> 见 [id_pool.h:56-70](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L56-L70)。新建的共享内存由系统清零，所以首个进程进入时 `invalid()` 为真，触发 `init()`；后续进程进入时 `prepared_` 已被置位，跳过。这是一种简易的「共享内存首次初始化」协议（无需额外的构造函数调用，因为共享内存不走 C++ 构造）。

**分配 acquire**：摘下链表头，cursor 前进一步：

```cpp
bool empty() const { return cursor_ == max_count; }          // cursor 到哨兵 = 空
storage_id_t acquire() {
    if (empty()) return -1;                                   // 满了，返回无效 id
    storage_id_t id = cursor_;                               // 取链表头
    cursor_ = next_[id];                                     // 头指针前进到下一个
    return id;
}
```

> 见 [id_pool.h:72-81](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L72-L81)。`-1` 作为「分配失败」的哨兵贯穿整个仓库（`storage_id_t = int32`，可表示 -1）。

**回收 release**：把槽插回链表头（LIFO）：

```cpp
bool release(storage_id_t id) {
    if (id < 0) return false;
    next_[id] = cursor_;                                     // 让被回收槽指向旧头
    cursor_ = static_cast<uint_t<8>>(id);                    // 头指针退回被回收槽
    return true;
}
```

> 见 [id_pool.h:83-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L83-L88)。这就是一个栈式（后进先出）的空闲链表。

#### 4.3.3 源码精读：send/recv 两侧如何调到 id_pool

发送端 `acquire_storage` 在持锁状态下调用 `prepare()` + `acquire()`：

```cpp
std::pair<ipc::storage_id_t, void*> acquire_storage(conn_info_head *inf, std::size_t size, ipc::circ::cc_t conns) {
    std::size_t chunk_size = calc_chunk_size(size);
    auto info = chunk_storage_info(inf, chunk_size);
    if (info == nullptr) return {};

    info->lock_.lock();
    info->pool_.prepare();          // 首次进入时初始化链表
    auto id = info->pool_.acquire();// 摘一个空闲槽
    info->lock_.unlock();

    auto chunk = info->at(chunk_size, id);
    if (chunk == nullptr) return {};
    chunk->conns().store(conns, std::memory_order_relaxed);   // 记录"要被哪些接收者读"
    return { id, chunk->data() };
}
```

> 见 [src/libipc/ipc.cpp:278-293](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L278-L293)。注意 `conns` 是发送时刻的连接位图（即所有应读此消息的接收者），它被写进 chunk 头部，是后续引用计数的初值。

接收端 `find_storage` 只读不分配，直接按下标取 chunk：

```cpp
void *find_storage(ipc::storage_id_t id, conn_info_head *inf, std::size_t size) {
    ...
    std::size_t chunk_size = calc_chunk_size(size);
    auto info = chunk_storage_info(inf, chunk_size);
    if (info == nullptr) return nullptr;
    return info->at(chunk_size, id)->data();
}
```

> 见 [src/libipc/ipc.cpp:295-305](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L295-L305)。`find` 不加锁、不动 `cursor_`，因为多个接收者读同一个 chunk 是只读操作，互不干扰。

而 `chunk_storage_info` 是「按 chunk_size 找到对应仓库」的分桶入口——同一进程内每种 chunk_size 对应一个 `chunk_handle_t`，它管理名为 `make_prefix(pref, "CHUNK_INFO__", chunk_size)` 的共享内存：

```cpp
chunk_info_t *chunk_storage_info(conn_info_head *inf, std::size_t chunk_size) {
    auto &storages = chunk_storages();                          // map<size_t, handle>
    ...
    if ((it = storages.find(chunk_size)) == storages.end()) {   # 没有则建一个
        ... storages.emplace(chunk_size, ...);
    }
    return it->second->get_info(inf, chunk_size);               # 打开/复用对应共享内存
}
```

> 见 [src/libipc/ipc.cpp:258-276](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L258-L276)；`get_info` 内部 `acquire` 共享内存见 [ipc.cpp:220-250](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L220-L250)。

#### 4.3.4 代码实践：手动演算 id_pool 的链表变化

1. **实践目标**：用纸笔跟踪 `id_pool` 连续 `acquire` 与 `release` 后，`cursor_` 与 `next_[]` 的状态变化，确认它是 LIFO 栈。
2. **操作步骤**：从 `init()` 后的初始态出发，依次执行 `acquire()` 三次、再 `release(1)` 一次（注意是回收下标 1，不是最后分配的）。
3. **演算过程**（初始态 `cursor_=0`，`next_=[1,2,3,...,31,32]`）：
   - `acquire()`：id=0，`cursor_=next_[0]=1`
   - `acquire()`：id=1，`cursor_=next_[1]=2`
   - `acquire()`：id=2，`cursor_=next_[2]=3`
   - 此时空闲链表头是 `cursor_=3`，即 `3→4→...→31→32`。
   - `release(1)`：`next_[1]=cursor_=3`，`cursor_=1`。空闲链表变为 `1→3→4→...→31→32`。
4. **预期结果**：下次 `acquire()` 会返回 **1**（刚回收的那个），而不是 3——印证 LIFO。
5. **结论**：`id_pool` 用 `next_[]` 自身充当链表节点，`cursor_` 是头指针，分配/回收都是 O(1) 且无需动态分配节点。这套结构在 u8-l3 还会以更通用的形式（`intrusive_stack`）再次出现。

#### 4.3.5 小练习与答案

**练习 1**：连续 `acquire()` 多少次后 `id_pool` 会返回 -1？
**答案**：`max_count = 32` 次。第 32 次取走下标 31，`cursor_` 变为 `next_[31] = 32 = max_count`，`empty()` 为真，第 33 次 `acquire()` 返回 -1。这与 u2-l4 的「32 接收者上限」是**不同的**两套 32——一个是连接位图的位宽，一个是 chunk 仓库的容量，两者数值相同纯属巧合（都源于 `large_msg_cache = 32` 与位宽 32）。

**练习 2**：为什么 `find_storage` 不需要加锁，而 `acquire_storage`/`release_storage` 必须加锁？
**答案**：`find` 只读 chunk 的载荷区（数据已由发送方 `memcpy` 写完），多个接收者并发读同一块只读内存互不影响；而 `acquire`/`release` 会修改 `cursor_` 和 `next_[]`（链表结构），必须用 `chunk_info_t::lock_` 串行化，否则会破坏链表。

---

### 4.4 acquire / find / release 存储：发送与接收的凭票存取

#### 4.4.1 概念说明

现在把 `id_pool` 与 `chunk` 串起来，看一条大消息在 `send`/`recv` 中如何「凭票存取」。核心编码技巧是：**队列里只传 4 字节的 `storage_id`，却要能让接收端还原出原始消息大小。** libipc 用 `msg_t` 的 `storage_` 标志位 + `remain_` 字段配合实现了这一点。

回顾 `msg_t` 构造函数（[ipc.cpp:48-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L48-L64)）：

```cpp
msg_t(msg_id_t cc_id, msg_id_t id, std::int32_t remain, void const * data, std::size_t size)
    : msg_t<0, AlignSize> {cc_id, id, remain, (data == nullptr) || (size == 0)} {
    if (this->storage_) {
        if (data != nullptr) {
            *reinterpret_cast<ipc::storage_id_t*>(&data_) =                // 只拷 4 字节票据
                 *static_cast<ipc::storage_id_t const *>(data);
        }
    }
    else std::memcpy(&data_, data, size);                                  // 普通分片：整块拷
}
```

当 `size == 0` 时 `storage_ = true`，载荷区只装 4 字节的 `storage_id`。这就是「票据」的编码方式。

#### 4.4.2 核心流程：send 的大消息分支

```cpp
if (size > ipc::large_msg_limit) {
    auto   dat = acquire_storage(inf, size, conns);        // 申请 chunk，返回 {id, data_ptr}
    void * buf = dat.second;
    if (buf != nullptr) {
        std::memcpy(buf, data, size);                      // 整条消息一次性写进 chunk
        return try_push(static_cast<std::int32_t>(size)    // remain = size - data_length
                        - static_cast<std::int32_t>(ipc::data_length),
                        &(dat.first), 0);                  // data=&id, size=0 → 触发 storage_=true
    }
    // try using message fragment   ← 仓库满时回落到分片
}
```

> 见 [src/libipc/ipc.cpp:560-570](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L560-L570)。

注意 `remain_` 被设为 `size - data_length`。接收端按 u3-l2 的统一公式还原消息尺寸：

\[ r\_size = \text{data\_length} + \text{remain\_} = \text{data\_length} + (size - \text{data\_length}) = size \]

于是接收方拿到的 `msg_size` 恰好等于原始消息字节数，再用这个 `msg_size` 去 `find_storage`（因为 `calc_chunk_size` 依赖它定位正确的桶）。

#### 4.4.3 核心流程：recv 的大消息分支

```cpp
if (msg.storage_) {
    ipc::storage_id_t buf_id = *reinterpret_cast<ipc::storage_id_t*>(&msg.data_);   // 读 4 字节票据
    void* buf = find_storage(buf_id, inf, msg_size);                               // 凭票取货
    if (buf != nullptr) {
        // 组装一个 recycle_t，把回收所需上下文打包
        struct recycle_t {
            ipc::storage_id_t storage_id;
            conn_info_t *     inf;
            ipc::circ::cc_t   curr_conns;
            ipc::circ::cc_t   conn_id;
        } *r_info = ipc::mem::$new<recycle_t>(recycle_t{
            buf_id, inf,
            que->elems()->connections(std::memory_order_relaxed),    // 当前连接位图
            que->connected_id()                                      // 本接收者的单 bit id
        });
        ...
        return ipc::buff_t{buf, msg_size, [回收lambda], r_info};     // 零拷贝：直接指向共享内存
    }
}
```

> 见 [src/libipc/ipc.cpp:666-701](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L666-L701)。

这里的精妙之处在于：返回的 `buff_t` **直接持有指向共享内存的指针 `buf`**，没有拷贝。而 `buff_t` 的析构器（第三个参数）被绑定成一个 lambda，在 `buff_t` 析构时执行引用计数回收（见 4.5）。

`recycle_t` 打包了回收所需的全部上下文：`storage_id`（归还哪个 chunk）、`inf`（哪个连接/前缀）、`curr_conns`（回收时刻的连接位图，用于剔除已断连的接收者）、`conn_id`（本接收者的座位号，用于清除自己的引用位）。

#### 4.4.4 源码精读：release_storage 与 clear_message 兜底

除了「正常读完后回收」，还有一条**不带引用计数**的直接回收路径，用于消息被强制驱逐时：

```cpp
void release_storage(ipc::storage_id_t id, conn_info_head *inf, std::size_t size) {
    ...
    std::size_t chunk_size = calc_chunk_size(size);
    auto info = chunk_storage_info(inf, chunk_size);
    if (info == nullptr) return;
    info->lock_.lock();
    info->pool_.release(id);            // 直接归还，不查引用计数
    info->lock_.unlock();
}
```

> 见 [src/libipc/ipc.cpp:307-319](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L307-L319)。

它被 `clear_message` 调用——当队列因超时走 `force_push` 覆盖最旧消息时，被覆盖的大消息票据会触发 `clear_message`，若该消息带 `storage_` 标志，就立即把对应 chunk 直接归还（因为这条消息要被丢弃了，无需等接收者读）：

```cpp
template <typename MsgT>
bool clear_message(conn_info_head *inf, void* p) {
    auto msg = static_cast<MsgT*>(p);
    if (msg->storage_) {
        std::int32_t r_size = static_cast<std::int32_t>(ipc::data_length) + msg->remain_;
        ...
        release_storage(*reinterpret_cast<ipc::storage_id_t*>(&msg->data_), inf, r_size);
    }
    return true;
}
```

> 见 [src/libipc/ipc.cpp:362-376](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L362-L376)。`clear_message` 在 `send` 的 `force_push` 回调里被注册（[ipc.cpp:601-605](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L601-L605)）。

#### 4.4.5 代码实践：跟踪一条 100KB 消息的「存」与「取」

1. **实践目标**：把 4.4.2 / 4.4.3 的流程套用到 100KB 消息上，完整说出票据如何生成、如何还原。
2. **操作步骤**：假设发送方调用 `chan.send(data, 102400)`。
3. **存（发送端）**：
   - `size = 102400 > large_msg_limit(64)` → 进入大消息分支。
   - `acquire_storage(inf, 102400, conns)`：`chunk_size = calc_chunk_size(102400) = 103424`（见 4.2.4）；在 103424 这个桶里 `acquire()` 一个空闲槽，假设得到 `id = 0`；把 `conns`（发送时刻连接位图）写入 chunk 头部；返回 `{0, chunk->data()}`。
   - `memcpy(buf, data, 102400)`：整条 100KB 消息一次性写进 chunk 载荷区。
   - `try_push(remain = 102400 - 64 = 102336, data = &id(指向 0), size = 0)`：队列里只塞一片，载荷是 4 字节的 `0`，`storage_ = true`。
4. **取（接收端）**：
   - `pop` 出该片，`msg.storage_ == true`。
   - `buf_id = 0`（读回票据）。
   - `msg_size = data_length + remain_ = 64 + 102336 = 102400`（还原原始尺寸）。
   - `find_storage(0, inf, 102400)`：用同样的 `calc_chunk_size(102400)=103424` 定位到同一个桶，按下标 0 取回 chunk 的 `data()` 指针。
   - 返回 `buff_t{buf, 102400, 回收析构器}`：调用方拿到的就是**直接指向共享内存 100KB 数据的零拷贝视图**。
5. **预期结果**：接收方读到的 `buff_t::size() == 102400`，`buff_t::data()` 指向共享内存中发送方写入的那 100KB；整个过程队列里实际只搬运了一个 4 字节票据 + 一个 64 字节槽。
6. **待本地验证**：可参考 `demo/send_recv` 写两个进程，发送方 `send` 一段 `102400` 字节的 buffer，接收方 `recv` 后打印 `buff.size()` 与首尾若干字节，确认内容一致。

#### 4.4.6 小练习与答案

**练习 1**：为什么 `try_push` 传给 `msg_t` 的 `size` 是 0，却能让接收端还原出 102400？
**答案**：`size == 0` 触发 `storage_ = true`，载荷区只放 4 字节票据；原始尺寸通过 `remain_ = size - data_length` 携带，接收端用 `r_size = data_length + remain_ = size` 还原。两个字段分工：`storage_` 标记「这是票据」，`remain_` 携带「真实大小」。

**练习 2**：如果仓库 32 个 chunk 全部占满，`acquire_storage` 返回什么？发送方会怎样？
**答案**：`pool_.acquire()` 返回 -1，`info->at(chunk_size, -1)` 返回 `nullptr`，`acquire_storage` 返回 `{}`（空 pair），`buf == nullptr`。发送方不报错，而是**回落到分片路径**（`if (buf != nullptr)` 不成立，继续执行后面的 for 循环分片）。这是大消息通路的关键容错设计。

---

### 4.5 recycle_storage：广播下的位图引用计数回收

#### 4.5.1 概念说明

这是本讲最精妙的部分。在**广播模式**下，一条大消息只存了一份在 chunk 里，却要被**所有**接收者读取。那么 chunk 什么时候才能归还？显然不能第一个接收者读完就归还（后面的接收者还没读），也不能永远不归还（内存泄漏）。答案是**引用计数**——但 libipc 没有用传统的整数计数器，而是**复用了 u2-l4 的连接位图**：

> chunk 头部那个 `atomic<cc_t>`，初值是「所有应读此消息的接收者」的位图。每个接收者读完，就**清除自己的那一位**。当位图变为全 0，说明我是最后一个读的人，由我负责归还 chunk。

这就是 `sub_rc`（subtract reference count）的语义。它对 `trans::unicast` 和 `trans::broadcast` 有两种特化：

#### 4.5.2 核心流程：sub_rc 的两种特化

单播特化——只有一个接收者，读完即归，恒为「最后一个」：

```cpp
template <ipc::relat Rp, ipc::relat Rc>
bool sub_rc(ipc::wr<Rp, Rc, ipc::trans::unicast>,
            std::atomic<ipc::circ::cc_t> &/*conns*/, ipc::circ::cc_t /*curr_conns*/, ipc::circ::cc_t /*conn_id*/) noexcept {
    return true;            // 单播：无需引用计数，直接放行回收
}
```

> 见 [src/libipc/ipc.cpp:321-325](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L321-L325)。

广播特化——CAS 清除自己的位，并判断是否为最后一人：

```cpp
template <ipc::relat Rp, ipc::relat Rc>
bool sub_rc(ipc::wr<Rp, Rc, ipc::trans::broadcast>,
            std::atomic<ipc::circ::cc_t> &conns, ipc::circ::cc_t curr_conns, ipc::circ::cc_t conn_id) noexcept {
    auto last_conns = curr_conns & ~conn_id;        // 清掉自己那位后的"目标"位图
    for (unsigned k = 0;;) {
        auto chunk_conns = conns.load(std::memory_order_acquire);
        if (conns.compare_exchange_weak(chunk_conns, chunk_conns & last_conns, std::memory_order_release)) {
            return (chunk_conns & last_conns) == 0; // CAS 成功后，若结果为 0 → 我是最后一人
        }
        ipc::yield(k);                              // CAS 失败，退避重试
    }
}
```

> 见 [src/libipc/ipc.cpp:327-338](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L327-L338)。

关键细节：

- `last_conns = curr_conns & ~conn_id`：以「回收时刻的连接位图」为基准，清掉本接收者的位。`conn_id` 是本接收者的单 bit 座位号（`que->connected_id()`）。
- CAS 把 chunk 头部从 `chunk_conns` 更新为 `chunk_conns & last_conns`。注意这里 `& last_conns` 同时还会**剔除那些在发送后已断连的接收者**（`curr_conns` 是回收时刻的实时位图，比发送时的 `conns` 初值可能更小），避免消息因「等一个已经消失的接收者」而永远无法回收。
- 返回 `(chunk_conns & last_conns) == 0`：CAS 成功后，若剩余位图为 0，说明本接收者是清完后使计数归零的人，由它负责归还。
- 内存序：`load` 用 `acquire`、CAS 成功用 `release`，保证 chunk 载荷数据的读操作（在 `buff_t` 析构前完成）对后续回收可见，且位图修改对其它接收者及时可见（内存序细节留待 u8-l1）。

#### 4.5.3 源码精读：recycle_storage 编排

`recycle_storage` 是 `buff_t` 析构器的实际执行体：先 `sub_rc` 减引用，归零才真正 `release` 归还 pool：

```cpp
template <typename Flag>
void recycle_storage(ipc::storage_id_t id, conn_info_head *inf, std::size_t size,
                     ipc::circ::cc_t curr_conns, ipc::circ::cc_t conn_id) {
    ...
    std::size_t chunk_size = calc_chunk_size(size);
    auto info = chunk_storage_info(inf, chunk_size);
    if (info == nullptr) return;
    auto chunk = info->at(chunk_size, id);
    if (chunk == nullptr) return;

    if (!sub_rc(Flag{}, chunk->conns(), curr_conns, conn_id)) {
        return;                             // 还有别的接收者没读完，不归还
    }
    info->lock_.lock();
    info->pool_.release(id);                // 我是最后一人，归还 chunk
    info->lock_.unlock();
}
```

> 见 [src/libipc/ipc.cpp:340-360](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L340-L360)。`Flag` 是通道的策略标签（如 `wr<single, multi, broadcast>`），编译期决定走单播还是广播特化。

而它在 `recv` 中被挂载为 `buff_t` 的析构器（4.4.3 的 lambda）：

```cpp
return ipc::buff_t{buf, msg_size, [](void* p_info, std::size_t size) {
    auto r_info = static_cast<recycle_t *>(p_info);
    LIBIPC_UNUSED auto finally = ipc::guard([r_info] { ipc::mem::$delete(r_info); });  // 释放 recycle_t
    recycle_storage<flag_t>(r_info->storage_id, r_info->inf, size,
                            r_info->curr_conns, r_info->conn_id);
}, r_info};
```

> 见 [src/libipc/ipc.cpp:685-695](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L685-L695)。当用户代码中那个 `buff_t` 离开作用域析构时，这个 lambda 才被回调，进而触发引用计数回收。`finally` 守卫确保 `recycle_t` 本身（进程本地堆上 `$new` 出来的）也被释放。

#### 4.5.4 核心流程：广播回收时序

把整条广播大消息的生命周期画出来（假设 3 个接收者 R1/R2/R3，座位号分别为 bit0/bit1/bit2）：

```
发送方:
  chunk 头部 conns = 0b0111 (= curr_conns, R1|R2|R3)   ← acquire_storage 写入
  队列发 1 片票据 (storage_id, storage_=true)

R1 recv → find_storage → buff_t{...} → 用户用完 → ~buff_t → recycle_storage
  sub_rc: last_conns = 0111 & ~0001 = 0110; CAS conns: 0111→0111&0110=0110
          返回 (0110 & 0110)==0111? 否 → 不归还        ← 还剩 R2/R3

R2 recv → ... → ~buff_t → recycle_storage
  sub_rc: last_conns = 0111 & ~0010 = 0101; CAS: 0110→0110&0101=0100
          返回 (0110 & 0101)==0100? 否 → 不归还        ← 还剩 R3

R3 recv → ... → ~buff_t → recycle_storage
  sub_rc: last_conns = 0111 & ~0100 = 0011; CAS: 0100→0100&0011=0000
          返回 (0100 & 0011)==0? 是 → pool_.release(id) ← 最后一人归还 chunk
```

> 说明：上述 `curr_conns` 取 R1/R2/R3 各自回收时刻的连接位图，这里为简化假设三者均仍在线（`curr_conns = 0111`）。若某接收者在读之前断连，`curr_conns & last_conns` 会顺带剔除它，避免悬挂。

#### 4.5.5 代码实践：解释广播回收路径

1. **实践目标**：结合 4.5.4 的时序，说明为什么必须用 CAS 而不能用普通读-改-写。
2. **操作步骤**：阅读 [ipc.cpp:327-338](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L327-L338) 的 `sub_rc` 广播特化。
3. **需要观察的现象**：`chunk_conns` 先 `load`，再尝试 `compare_exchange_weak` 把它改成 `chunk_conns & last_conns`；若期间有别的接收者改了头部，CAS 失败、`yield(k)` 退避后重试。
4. **预期结果/解释**：多个接收者会**并发**清除各自的位（R1 清 bit0、R2 清 bit1……）。若用非原子的读-改-写，两个接收者的写会互相覆盖（丢失其中一次清除），导致计数永远到不了 0、chunk 永远不归还（内存泄漏）。CAS 保证「读到的值与写回时一致」，并发清除互不丢失。`yield(k)` 的分级退避策略在 u6-l1 详述。
5. **待本地验证**：可设计一个广播 channel，1 发送 + 3 接收，循环收发大消息数千次，观察进程驻留内存是否稳定（不持续增长）——若 CAS 有丢失，内存会随次数单调上升。

#### 4.5.6 小练习与答案

**练习 1**：单播模式下，chunk 头部的 `conns` 是否会被修改？为什么？
**答案**：不会被 `sub_rc` 修改。单播特化（[ipc.cpp:321-325](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L321-L325)）直接 `return true`，连参数都没用。因为单播只有一个接收者，读完即归，无需位图引用计数。`acquire_storage` 写入的 `conns` 初值在单播下实际未被使用（但发送方仍会写入，因为 `acquire_storage` 对单播/广播通用）。

**练习 2**：如果某个接收者进程在读大消息之前崩溃了，chunk 会泄漏吗？
**答案**：不会。`sub_rc` 用 `last_conns = curr_conns & ~conn_id`，其中 `curr_conns` 是**回收时刻**的实时连接位图。崩溃的接收者会从连接位图中消失（u2-l4 的 `disconnect` 清位），所以存活接收者回收时 `curr_conns` 已不含崩溃者，CAS 会顺带清掉它的位，最后一个存活接收者仍能让位图归零并归还 chunk。这正是引用计数用「动态位图」而非「静态计数」的额外收益。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个「大消息仓库可视化」跟踪任务。

**任务**：基于 `demo/send_recv`（或 `demo/msg_que`），改造出一个 1 发 3 收的广播程序，发送一条 **100KB** 的消息（内容可填充为可识别的模式，如递增字节 `0,1,2,...,255,0,1,...`），每个接收者 `recv` 后做以下检查并打印日志：

1. **尺寸**：`buff.size() == 102400`。
2. **内容**：抽样校验首、中、尾若干字节是否符合发送模式（验证零拷贝读取正确）。
3. **生命周期**：让 3 个接收者**依次**（而非同时）销毁各自的 `buff_t`，并在每个销毁点打印一条「buff destroyed」日志。
4. **回收观察**：结合本讲源码，回答——

   - 这条消息的 `chunk_size` 是多少？（答：103424，见 4.2.4）
   - 它落在哪个共享内存区域？名字是什么？（答：`make_prefix(pref, "CHUNK_INFO__", 103424)`，见 4.3.3）
   - 第 1、2 个接收者销毁 buff 时，`sub_rc` 各返回什么？第 3 个返回什么？谁真正触发了 `pool_.release`？（答：false、false、true，第 3 个触发归还，见 4.5.4）

5. **进阶（可选）**：把消息改成 **64KB + 1 字节**（= 65537），重新计算它的 `chunk_size`（应为 `make_align(8, 65537+8)` 向上取整到 1KB = 66604? 请手算验证），确认它落进一个**不同的桶**（与 100KB 的桶不同）。

> 这个任务覆盖了：chunk_size 1KB 对齐（4.2）、id_pool 分配（4.3）、send/recv 凭票存取与零拷贝（4.4）、广播位图引用计数回收（4.5）全部四个最小模块。

## 6. 本讲小结

- **两条大消息通路**：超过 `large_msg_limit`(=64B) 的消息**优先**走外部存储（chunk 仓库），仓库分配失败才**回落**到分片（u3-l2）。外部存储让队列只传 4 字节票据，实现大消息零拷贝。
- **chunk 内存布局**：每个 chunk = `8` 字节头部（`atomic<cc_t>` 引用计数位图，对齐到 `max_align_t`）+ 载荷区；整体尺寸由 `calc_chunk_size` 向上取整到 **1KB** 整数倍。`chunk_info_t`（`id_pool` + `spin_lock`）后跟 32 个 chunk 排成数组，按下标 `storage_id` 寻址；不同 chunk_size 分属不同共享内存区域。
- **id_pool 空闲链表**：复用 `next_[]` 数组当链表节点，`cursor_` 当头指针，`acquire`/`release` 都是 O(1) 的 LIFO 栈操作；`max_count = min(large_msg_cache, uint8_max) = 32`；`prepare`/`invalid` 实现共享内存懒初始化。
- **凭票存取编码**：`msg_t` 构造时 `size==0` → `storage_=true`，载荷只装 4 字节 `storage_id`；原始尺寸由 `remain_ = size - data_length` 携带，接收端用 `r_size = data_length + remain_` 还原，从而正确定位 chunk 桶。
- **广播引用计数回收**：chunk 头部位图初值 = 发送时刻连接位图；每个接收者读完用 CAS 清自己的位（`sub_rc`），位图归零的最后一人负责 `pool_.release` 归还；单播特化恒返回 true。`buff_t` 的析构器挂载 `recycle_storage`，实现「零拷贝读 + 用完自动回收」。
- **容错与清理**：仓库满 → 回落分片；`force_push` 驱逐旧消息 → `clear_message` 调 `release_storage` 直接归还（不走引用计数）；接收者崩溃 → 动态 `curr_conns` 自动剔除其位，不泄漏。

## 7. 下一步学习建议

本讲讲完了「大消息如何存与回收」，但留下几个钩子，建议按序继续：

- **u3-l4 等待模型**：本讲多次出现 `wait_for`、`rd_waiter_.broadcast()`、`yield(k)`，它们属于 channel 的等待/唤醒体系，下一讲会讲清「先自旋后阻塞」的退避设计。
- **u4 无锁队列**：本讲的 `try_push`/`pop` 委托给底层 `queue`，票据最终如何在循环数组里被生产-消费，需要 u4 的 `elem_array` 与 `prod_cons` 算法来回答。
- **u6-l1 / u8-l1 内存序**：本讲中 `sub_rc` 的 CAS 为何用 `acquire`/`release`、`acquire_storage` 为何用 `relaxed`，涉及 C++ 内存模型，留待进阶讲义深入。
- **u8-l3 无锁基础结构**：`id_pool` 的「数组当链表」思路，在 `concur::intrusive_stack`（CAS 栈）中会以更通用的并发形式再次出现，届时可对比二者的异同。
