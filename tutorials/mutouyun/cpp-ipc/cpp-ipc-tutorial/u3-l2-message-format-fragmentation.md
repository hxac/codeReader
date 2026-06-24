# 消息格式与分片重组

> 本讲承接 [u3-l1](u3-l1-send-recv-data-path.md)：你已经知道 `send → push → rd_waiter_.broadcast` 与 `recv → pop → wt_waiter_.broadcast` 这条主链路。本讲回答一个更细的问题：**一条任意长度的用户消息，是怎么塞进「每个槽位只有 64 字节」的无锁队列、又在接收端被拼回原样的？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 `msg_t` 的内存布局，说清 `cc_id_` / `id_` / `remain_` / `storage_` 四个头字段各自的含义。
2. 手动推演「一条 N 字节消息会被切成几片、每片的 `remain_` 是多少」。
3. 解释接收端 `recv` 如何用 `thread_local` 的 `recv_cache`、以 `msg.id_` 为键，把分片按顺序拼回完整消息。
4. 区分两条收发路径：**分片路径**（小消息 / 兜底）与 **大消息外部存储快速路径**（`storage_` 标志），并知道它们在 `send` 里的先后关系。

## 2. 前置知识

在进入源码前，先用一段比喻建立直觉。

想象一条**很窄的传送带**，传送带上每一个格子只能放 **64 字节**。可用户要传的消息可能是 10 字节，也可能是 10 MB。怎么办？两种思路：

- **思路 A（分片）**：把长消息像切香肠一样，切成一段段 64 字节的「片段」，每段贴一个标签写明「这是第几段、后面还剩多少」，一段段放进传送带。接收方按标签把片段拼回原样。
- **思路 B（外部仓库）**：长消息不再走传送带，而是存进仓库（一块独立的共享内存），只在传送带上放一张「取货单」（仓库地址）。接收方凭单子去仓库取整块数据。

libipc 两条路径都用：**默认对超过 64 字节的消息先尝试思路 B（外部存储）；如果仓库分配失败，再退回思路 A（分片）**。本讲的核心是思路 A 的「标签格式」与「拼装逻辑」，同时讲清思路 B 的开关 `storage_` 标志，仓库本身的细节留给 [u3-l3](u3-l3-large-message-storage.md)。

你需要回顾的几个常量（来自 [def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L33-L39)）：

| 常量 | 值 | 含义 |
|------|----|----|
| `data_length` | 64 | 传送带单个格子的有效载荷大小（字节），也是分片的单位 |
| `large_msg_limit` | 64 | 大消息阈值，等于 `data_length` |
| `large_msg_align` | 1024 | 外部仓库块大小的对齐单位 |
| `invalid_value` | `uint32_t` 最大值 | recv 无限等待的哨兵 |

> 关键：`large_msg_limit == data_length`，所以「大于 64 字节」的消息才会触发大消息机制。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 库实现核心 | `msg_t` 定义、`send` 的分片循环、`recv` 的重组循环、`recv_cache` |
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | 类型宪法 | `data_length` / `large_msg_limit` 等常量 |
| [src/libipc/utility/id_pool.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h) | id 分配器 | `storage_id_t` 的类型定义（仅引用一行） |

回忆 u3-l1 的结构：`ipc.cpp` 自下而上是 `msg_t` → 全局计数器 → `conn_info_head` → `queue_generator` → `detail_impl<Policy>` → `chan_impl`。本讲只深入其中的 `msg_t`、`detail_impl::send`、`detail_impl::recv` 三块。

## 4. 核心概念与源码讲解

### 4.1 msg_t：消息头格式与 storage_ 标志

#### 4.1.1 概念说明

队列里的每一个槽位，存的是一个 `msg_t` 对象。它既要装「有效载荷」（用户数据），又要装「控制信息」：这条消息是谁发的、属于哪条原始消息、是第几片、是不是放在外部仓库。于是 `msg_t` 被设计成「**头部 + 载荷**」两段，头部是定长的「标签」，载荷是定长 64 字节的「格子」。

#### 4.1.2 内存布局

`msg_t` 用模板偏特化拆成基类与派生类：基类 `msg_t<0, AlignSize>` 只放头部四个字段，派生类 `msg_t<DataSize, AlignSize>` 再追加一段对齐的载荷存储。

基类（头部，4 个字段）：

```cpp
template <std::size_t AlignSize>
struct msg_t<0, AlignSize> {
    msg_id_t     cc_id_;    // 发送者的「身份证号」，用于过滤自己发的消息
    msg_id_t     id_;       // 这条原始消息的唯一 id，分片重组的「键」
    std::int32_t remain_;   // 本片段之后「还剩多少字节」的编码（见 4.2）
    bool         storage_;  // 是否走外部存储（大消息）快速路径
};
```
> 见 [ipc.cpp:L40-L46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L40-L46)：四个头部字段的定义。注意 `msg_id_t = std::uint32_t`（[ipc.cpp:L34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L34)），`remain_` 是**有符号** 32 位（可正可负）。

派生类（追加 64 字节载荷）：

```cpp
template <std::size_t DataSize, std::size_t AlignSize>
struct msg_t : msg_t<0, AlignSize> {
    std::aligned_storage_t<DataSize, AlignSize> data_ {};
    msg_t(msg_id_t cc_id, msg_id_t id, std::int32_t remain,
          void const * data, std::size_t size)
        : msg_t<0, AlignSize> {cc_id, id, remain, (data == nullptr) || (size == 0)} {
        if (this->storage_) {
            if (data != nullptr)
                *reinterpret_cast<ipc::storage_id_t*>(&data_) =
                     *static_cast<ipc::storage_id_t const *>(data); // 只拷贝 storage id
        }
        else std::memcpy(&data_, data, size);                      // 普通分片：拷贝载荷
    }
};
```
> 见 [ipc.cpp:L48-L64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L48-L64)：派生 `msg_t` 与构造函数。

这里有两个**极易踩坑的设计**，请重点理解：

**① `storage_` 是由「载荷是否为空」反推出来的，而不是显式传入。** 构造函数没有 `bool storage` 形参，而是用 `(data == nullptr) || (size == 0)` 计算：

\[
\text{storage\_} \;=\; (\text{data} == \text{nullptr}) \;\lor\; (\text{size} == 0)
\]

当 `storage_` 为真时，`data_` 不再装用户数据，而是装一个 `storage_id_t`（仓库取货单号，类型为 `std::int32_t`，见 [id_pool.h:L12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L12)）。一个 bool 同时兼任「路径开关」与「载荷语义切换」。

**② `cc_id_` 与 `id_` 是两个不同的 id，切勿混淆**（这是 u3-l1 已强调、本讲会反复用到的区分）：

| 字段 | 含义 | 生命周期 | 本讲用途 |
|------|------|---------|---------|
| `cc_id_` | 发送者身份证号，全局单调递增 | 一个连接一个 | recv 里过滤「自己发给自己的消息」 |
| `id_` | 原始消息 id，全局单调递增 | 一次 `send` 一个 | **分片重组的键**，同一条消息的所有分片共享 |

#### 4.1.3 队列槽位的实际尺寸

`msg_t` 的两个模板参数由 `queue_generator` 给默认值：

```cpp
template <typename Policy,
          std::size_t DataSize  = ipc::data_length,                                   // 64
          std::size_t AlignSize = (ipc::detail::min)(DataSize, alignof(std::max_align_t))> // min(64,16)=16
struct queue_generator {
    using queue_t = ipc::queue<msg_t<DataSize, AlignSize>, Policy>;
```
> 见 [ipc.cpp:L393-L398](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L393-L398)：`DataSize` 默认 `data_length=64`，`AlignSize` 默认取 `DataSize` 与 `max_align_t` 对齐（典型 16）的较小值。

所以队列每个槽位实际是 `msg_t<64, 16>`：约 16 字节头部（4+4+4+1 加对齐填充）+ 64 字节载荷。**64 这个数字就是分片的「香肠段长度」**。

#### 4.1.4 代码实践：观察 msg_t 的真实大小

1. **实践目标**：确认一个队列槽位到底占多少字节，验证「头部 + 64 字节载荷」的布局。
2. **操作步骤**：写一段最小程序（示例代码，非项目原有），打印 `sizeof`：

   ```cpp
   // 示例代码：仅用于验证布局，非项目源码
   #include <cstdio>
   #include <type_traits>
   #include <cstdint>
   int main() {
       struct head_t { std::uint32_t cc_id_, id_; std::int32_t remain_; bool storage_; };
       struct msg_demo : head_t {
           std::aligned_storage_t<64, 16> data_;
       };
       std::printf("sizeof(head)   = %zu\n", sizeof(head_t));      // 预期 16（bool 对齐填充）
       std::printf("sizeof(msg)    = %zu\n", sizeof(msg_demo));   // 预期 16 + 64 = 80
       return 0;
   }
   ```
3. **需要观察的现象**：`sizeof(msg)` 应为 80（64 位平台，`bool` 后有 11 字节填充把头部补到 16，再加 64 字节载荷）。
4. **预期结果**：头部 16 字节、载荷 64 字节、合计 80 字节。与项目源码中 `msg_t<64,16>` 布局一致。
5. **说明**：本机字长/对齐不同可能略有差异，若结果不是 80 请以本机 `sizeof` 为准——这属于「待本地验证」的细节，不影响分片逻辑。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `msg_t` 要拆成基类 `msg_t<0, AlignSize>` 和派生类两份，而不是直接写一个结构体？
  - **答案**：基类版 `msg_t<0, AlignSize>` 只含头部，可在不关心载荷的场景（如只读头部字段）复用；派生类追加载荷存储。这是模板偏特化的常见手法，也让「头部布局」与「载荷布局」解耦。

- **练习 2**：构造函数里 `storage_ = (data == nullptr) || (size == 0)`。如果一条普通分片恰好 `size==0`，会发生什么误判？
  - **答案**：会被误判为 `storage_==true`，从而把 `data_` 当作 `storage_id_t` 解析。但在实际 `send` 流程里，普通分片的 `size` 恒为 `data_length`(64) 或尾部实际字节数（≥1），不会出现 `size==0` 的普通分片；`size==0` 只在大消息路径有意制造。所以该「隐式推断」在当前调用约定下是安全的。

---

### 4.2 按 data_length 分片发送

#### 4.2.1 概念说明

`detail_impl::send` 的内层模板函数 `send(F&& gen_push, ...)` 是真正决定「切成几片」的地方。它先为整条消息申请一个全局唯一的 `msg_id`，再决定走「外部存储」还是「分片」。分片时，**所有片段共享同一个 `msg_id`**，靠 `remain_` 区分先后与是否最后一片。

#### 4.2.2 核心流程

`send` 的主干（已省略校验）：

```cpp
// 1) 为整条消息申请唯一 id（一次 send 一个）
auto msg_id   = acc->fetch_add(1, std::memory_order_relaxed);
auto try_push = std::forward<F>(gen_push)(inf, que, msg_id);

// 2) 大消息优先走外部存储（思路 B）
if (size > ipc::large_msg_limit) {            // size > 64
    auto dat = acquire_storage(inf, size, conns);
    if (dat.second != nullptr) {
        std::memcpy(dat.second, data, size);
        return try_push(static_cast<std::int32_t>(size) - data_length,
                        &(dat.first), 0);     // 只发 1 片，载荷=storage id，size=0 → storage_=true
    }
    // 仓库分配失败，落到下面的分片（思路 A）
}

// 3) 分片：先发整段 data_length，再发尾部
std::int32_t offset = 0;
for (std::int32_t i = 0; i < static_cast<std::int32_t>(size / ipc::data_length);
     ++i, offset += ipc::data_length) {
    if (!try_push(static_cast<std::int32_t>(size) - offset - data_length,   // remain_
                  static_cast<ipc::byte_t const *>(data) + offset,          // 载荷
                  ipc::data_length)) { return false; }
}
std::int32_t remain = static_cast<std::int32_t>(size) - offset;
if (remain > 0) {   // 尾部不足 data_length 的那一片
    if (!try_push(remain - static_cast<std::int32_t>(ipc::data_length),     // remain_ 为负
                  static_cast<ipc::byte_t const *>(data) + offset, remain)) { return false; }
}
return true;
```
> 见 [ipc.cpp:L527-L589](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L527-L589)：`send(F&& gen_push, ...)` 全貌；大消息快路径在 [L560-L570](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L560-L570)，分片循环在 [L572-L587](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L572-L587)。

#### 4.2.3 remain_ 的编码规则

`remain_` 不是简单的「剩余分片数」，而是一个**字节数编码**，规则如下：

- **整片段（满 64 字节）**：`remain_ = size - offset - data_length`，即「本片段之后还剩多少字节」，**为正**。
- **尾片段（不足 64 字节）**：`remain_ = remain - data_length`，其中 `remain < data_length`，故 **为负**（或恰好 0）。

接收端只需一个公式就能从 `remain_` 反推出本片段的语义：

\[
r\_size \;=\; \text{data\_length} \;+\; \text{remain\_} \;=\; \text{size} - \text{offset}
\]

即 `r_size` 等于「**从本片段起点到整条消息末尾的字节数**」。这个统一公式同时支撑了三件事（见 4.3）：

- 首片段：`r_size = size - 0 = 整条消息长度` → 用它分配重组缓冲区。
- 中间片段：`r_size` 不直接用，固定追加 `data_length`。
- 尾片段：`r_size = 尾部实际字节数` → 用它做最后一次追加。
- 判尾：`remain_ <= 0` 即代表最后一片。

#### 4.2.4 手动推演：一条 200 字节消息

> 注意：200 > 64，所以 `send` 会**先尝试外部存储快路径**。只有在 `acquire_storage` 失败时，才走到下面的分片循环。本节按「分片路径」推演，这正是练习题要回答的场景。

设 `size = 200`，`data_length = 64`。

第一步：`size / data_length = 200 / 64 = 3`（整数除法），循环执行 3 次：

| 片段 | offset（起点） | `remain_ = size - offset - 64` | 载荷区间 | 载荷字节数 |
|------|----------------|--------------------------------|----------|-----------|
| F1 | 0   | 200 − 0 − 64 = **136** | [0, 64)    | 64 |
| F2 | 64  | 200 − 64 − 64 = **72** | [64, 128)  | 64 |
| F3 | 128 | 200 − 128 − 64 = **8**  | [128, 192) | 64 |

第二步：循环结束后 `offset = 192`，`remain = 200 − 192 = 8 > 0`，发尾片段：

| 片段 | offset | `remain_ = remain − 64` | 载荷区间 | 载荷字节数 |
|------|--------|-------------------------|----------|-----------|
| F4 | 192 | 8 − 64 = **−56** | [192, 200) | 8 |

**结论：切成 4 片，`remain_` 依次为 136、72、8、−56。** 四片共享同一个 `msg_id`；前三片 `remain_ > 0`，最后一片 `remain_ < 0`（负值即「我是最后一片」信号）。

校验 `r_size = 64 + remain_`：200、136、72、8——正是「从本片段到消息末尾」的字节数，逐片递减 64，完美吻合。

#### 4.2.5 小练习与答案

- **练习 1**：一条恰好 64 字节的消息（`size == data_length`）会被切成几片？`remain_` 是多少？
  - **答案**：`size/data_length = 1`，循环发 1 片，`remain_ = 64 - 0 - 64 = 0`；循环后 `remain = 64 - 64 = 0`，不进尾部分支。所以只发 **1 片**，`remain_ = 0`（`remain_ <= 0` 同时也表示它是最后一片）。

- **练习 2**：为什么大消息要先试外部存储，失败才分片？
  - **答案**：分片会让一条消息占用多个队列槽位（200 字节占 4 个），既放大无锁队列的竞争，又让接收端要做重组。外部存储只占 1 个槽位（载荷换成 storage id），整块数据放独立共享内存，吞吐更高。分片是「仓库不可用时的兜底」。

---

### 4.3 recv_cache 分片重组

#### 4.3.1 概念说明

发送端把长消息切成多片、各自独立地入队；它们到达接收端的顺序由无锁队列保证（同一条逻辑通道内按入队顺序出队）。接收端 `recv` 每次只能 `pop` 出**一个** `msg_t`，所以必须有个地方暂存「还没凑齐的半成品」——这就是 `recv_cache`。它是一个**线程局部**（`thread_local`）的映射，以 `msg.id_` 为键，把同一条消息的分片攒起来。

#### 4.3.2 核心流程：recv 的重组主循环

`recv` 在 `pop` 出一个 `msg` 后，按 `msg.id_` 在 `recv_cache` 里查表，分三种情况：

```cpp
auto& rc = inf->recv_cache();
for (;;) {
    typename queue_t::value_t msg {};
    // ... wait_for + que->pop(msg) 取出一片 ...
    if ((inf->acc() != nullptr) && (msg.cc_id_ == inf->cc_id_)) continue; // 过滤自发消息
    std::int32_t r_size = data_length + msg.remain_;   // 本片段到消息末尾的字节数
    if (r_size <= 0) { /* error */ return {}; }
    std::size_t msg_size = static_cast<std::size_t>(r_size);

    if (msg.storage_) { /* 大消息路径，见 4.4 */ }

    auto cac_it = rc.find(msg.id_);
    if (cac_it == rc.end()) {                       // 情况 A：首片
        if (msg_size <= ipc::data_length) return make_cache(msg.data_, msg_size); // 小消息直接返回
        // ... GC：缓存超 1024 项时清理过期 id ...
        rc.emplace(msg.id_, cache_t{ipc::data_length, make_cache(msg.data_, msg_size)}); // 预分配整条并写入首片
    }
    else {                                          // 情况 B：已有缓存（中间片或尾片）
        auto& cac = cac_it->second;
        if (msg.remain_ <= 0) {                     // 情况 B1：尾片
            cac.append(&(msg.data_), msg_size);     // 追加尾部字节
            auto buff = std::move(cac.buff_);       // 取走完整缓冲
            rc.erase(cac_it);                       // 清理缓存项
            return buff;
        }
        cac.append(&(msg.data_), ipc::data_length); // 情况 B2：中间片，追加满 64 字节
    }
}
```
> 见 [ipc.cpp:L629-L737](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L629-L737)：`recv` 全貌；`r_size` 计算在 [L658-L664](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L658-L664)，缓存查找/插入/追加/收尾在 [L702-L735](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L702-L735)。

支撑它的三块小结构：

**`recv_cache()` —— 线程局部映射**：

```cpp
auto& recv_cache() {
    thread_local ipc::unordered_map<msg_id_t, cache_t> tls;
    return tls;
}
```
> 见 [ipc.cpp:L171-L174](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L171-L174)：`thread_local` 映射，键是 `msg.id_`。因为是线程局部，广播模式下每个接收线程各自重组、互不干扰。

**`cache_t` —— 半成品容器**：

```cpp
struct cache_t {
    std::size_t fill_;        // 已写入字节数（写指针）
    ipc::buff_t buff_;        // 预分配的整条消息缓冲
    void append(void const * data, std::size_t size) {
        if (fill_ >= buff_.size() || data == nullptr || size == 0) return;
        auto new_fill = (ipc::detail::min)(fill_ + size, buff_.size());
        std::memcpy(static_cast<ipc::byte_t*>(buff_.data()) + fill_, data, new_fill - fill_);
        fill_ = new_fill;
    }
};
```
> 见 [ipc.cpp:L96-L110](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L96-L110)：`fill_` 是写入偏移，`append` 把分片载荷追加到 `fill_` 处，并用 `buff_.size()` 封顶防止越界。

**`make_cache` —— 预分配缓冲并写入首片**：

```cpp
template <typename T>
ipc::buff_t make_cache(T &data, std::size_t size) {
    auto *ptr = ipc::mem::$new<void>(size);                       // 按整条长度分配
    std::memcpy(ptr, &data, (ipc::detail::min)(sizeof(data), size)); // 只拷贝 min(载荷槽, size)
    return { ptr, size, [](void *p, std::size_t) noexcept { ipc::mem::$delete(p); } };
}
```
> 见 [ipc.cpp:L66-L76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L66-L76)。注意它**按 `size` 分配整条缓冲，但只拷贝 `min(sizeof(data), size)` 字节**——首片时 `sizeof(data_)=64`，所以只先填 64 字节，其余留给后续 `append`。

#### 4.3.3 重组推演：接续 4.2 的 200 字节消息

把 4.2 推出的 4 个分片依次喂给 `recv`：

| 步骤 | 收到片段 | `r_size = 64+remain_` | `recv_cache` 动作 | `fill_` | 结果 |
|------|---------|----------------------|-------------------|---------|------|
| 1 | F1 (remain_=136) | 200 | 查无 → 分配 200 字节缓冲，写入首 64 字节，缓存键=`msg.id_` | 64 | 继续循环 |
| 2 | F2 (remain_=72)  | 136 | 命中缓存，`remain_>0` → `append` 64 字节 | 128 | 继续循环 |
| 3 | F3 (remain_=8)   | 72  | 命中缓存，`remain_>0` → `append` 64 字节 | 192 | 继续循环 |
| 4 | F4 (remain_=−56) | 8   | 命中缓存，`remain_≤0` → `append(msg_size=8)` → 取走缓冲、删缓存项 | 200 | **返回 200 字节 buff_t** |

四片拼回完整的 200 字节消息。可以看到：

- **首片的 `r_size`(=200) 决定了缓冲区总大小**——这正是 `remain_` 编码的妙用：第一片的 `remain_` 恰好编码了「整条消息长度 − data_length」，加上 `data_length` 就是总长。
- **尾片的 `r_size`(=8) 决定了最后一次追加多少字节**——尾部不足 64 的零头。
- **中间片一律追加 64 字节**，与 `r_size` 无关。

#### 4.3.4 缓存的垃圾回收（GC）

如果某条消息的分片永远凑不齐（比如发送端中途崩溃），缓存项会泄漏。`recv` 在插入首片前做了一次简单 GC：

```cpp
// gc
if (rc.size() > 1024) {
    std::vector<msg_id_t> need_del;
    for (auto const & pair : rc) {
        auto cmp = std::minmax(msg.id_, pair.first);
        if (cmp.second - cmp.first > 8192) need_del.push_back(pair.first);
    }
    for (auto id : need_del) rc.erase(id);
}
```
> 见 [ipc.cpp:L708-L718](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L708-L718)：当缓存超过 1024 项，删除与当前 `msg.id_` 相差超过 8192 的「陈旧」id。因为 `msg.id_` 是全局单调递增的，差距过大即可认定对方已不可能再补齐该片。

此外，连接彻底断开时 `disconnect_receiver` 会清空整个 `recv_cache`（[ipc.cpp:L431-L437](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L431-L437)）。

#### 4.3.5 代码实践：跟踪一条 200 字节消息的重组

1. **实践目标**：在真实源码上走一遍「200 字节 → 4 片 → 重组」的完整链路，验证 4.3.3 的推演。
2. **操作步骤**（源码阅读型实践）：
   - 打开 [ipc.cpp:L527-L589](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L527-L589) 的 `send`，假设 `acquire_storage` 返回空（即走分片路径），令 `size=200`，手动填出 4.2.4 表格里的 4 个 `remain_`。
   - 再打开 [ipc.cpp:L629-L737](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L629-L737) 的 `recv`，对这 4 个分片逐片走 `r_size = 64 + remain_` 与 `recv_cache` 三分支，对照 4.3.3 表格。
   - 进阶：在 `cache_t::append`（[L104-L109](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L104-L109)）入口加一行日志（示例：`log.debug("append id=", ..., " fill=", fill_, " +", size);`），重新编译 `send_recv` demo，发送一条 200 字节消息，观察接收端日志里 `fill_` 从 64→128→192→200 的递增。
3. **需要观察的现象**：日志应显示首片写入 64 字节，随后每次 +64，最后一次 +8，最终 `fill_==200` 并返回。
4. **预期结果**：接收端打印出的消息长度为 200，内容与发送端一致；`fill_` 序列为 64、128、192、200。
5. **说明**：若你实际用 demo 发 200 字节，多数情况下会命中**外部存储快路径**（4.4）而非分片——要强制走分片路径，需要在调试器里让 `acquire_storage` 返回空，或仅做纸上推演。这一点属于「待本地验证」。

#### 4.3.6 小练习与答案

- **练习 1**：为什么 `recv_cache` 用 `thread_local` 而不是普通成员？
  - **答案**：多个接收线程可能各自在 `recv` 中并发重组不同的消息；`thread_local` 让每个线程拥有独立的缓存映射，无需加锁、互不污染。键 `msg.id_` 全局唯一，所以同一消息不会被两个线程同时重组。

- **练习 2**：首片到达时，`recv` 怎么知道整条消息有多长、该分配多大缓冲？
  - **答案**：靠首片的 `remain_`。首片的 `r_size = data_length + remain_ = size - 0 = 整条长度`，`make_cache(msg.data_, msg_size)` 就按这个 `msg_size` 一次性分配好整条缓冲，后续只需追加。这是 `remain_` 编码「到消息末尾的字节数」的直接收益。

---

### 4.4 storage_ 大消息标志与快速路径

#### 4.4.1 概念说明

`storage_` 是 `msg_t` 头部那个 bool，它是一个**路径开关**：为真表示这条 `msg_t` 的 `data_` 不装用户数据，而装一个 `storage_id_t`（仓库取货单号），真正的数据躺在独立的大块共享内存里。本节只讲「标志如何选路」，仓库本身的分配/对齐/引用计数回收是 [u3-l3](u3-l3-large-message-storage.md) 的主题。

#### 4.4.2 发送端：把大消息整块存进仓库，只发 1 片

回顾 4.2.2 的快路径：

```cpp
if (size > ipc::large_msg_limit) {            // size > 64
    auto   dat = acquire_storage(inf, size, conns);   // 向仓库要一块，返回 {id, buf}
    void * buf = dat.second;
    if (buf != nullptr) {
        std::memcpy(buf, data, size);                // 整块拷进仓库
        return try_push(static_cast<std::int32_t>(size) - data_length,
                        &(dat.first), 0);            // 只入队 1 片：载荷=取货单号, size=0
    }
    // 仓库满了 → 落到分片路径
}
```
> 见 [ipc.cpp:L560-L570](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L560-L570)：大消息快路径。

注意 `try_push` 的第三个实参是 `0`（`size=0`）。回到 4.1.2，构造函数因此算出 `storage_ = (data != nullptr) || (size == 0) = true`，并把 `&(dat.first)`（取货单号）当作 `storage_id_t` 拷进 `data_`。**于是一整条大消息，在队列里只占一个 80 字节的槽位**，载荷区那 64 字节里只放了一个 4 字节的 id。

#### 4.4.3 接收端：凭取货单号去仓库取整块数据

`recv` 在算出 `r_size` 后优先检查 `storage_`：

```cpp
if (msg.storage_) {
    ipc::storage_id_t buf_id = *reinterpret_cast<ipc::storage_id_t*>(&msg.data_); // 取出取货单号
    void* buf = find_storage(buf_id, inf, msg_size);                              // 去仓库定位整块
    if (buf != nullptr) {
        // 包装成 buff_t，并绑定一个「引用计数回收」析构器
        return ipc::buff_t{buf, msg_size, [](void* p_info, std::size_t size) {
            auto r_info = static_cast<recycle_t *>(p_info);
            recycle_storage<flag_t>(r_info->storage_id, r_info->inf, size,
                                     r_info->curr_conns, r_info->conn_id);
        }, r_info};
    }
    // 仓库里找不到 → 丢弃本片，继续循环
}
```
> 见 [ipc.cpp:L666-L701](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L666-L701)：`recv` 的大消息分支。`find_storage` 在 [L295-L305](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L295-L305)。

这里有两个要点（细节交给 u3-l3）：

- **零拷贝**：返回的 `buff_t` 直接指向仓库里的那块内存（`buf`），不做任何 `memcpy`，用户拿到的就是发送端写入的同一块共享内存。
- **引用计数回收**：广播模式下多个接收者会读同一块仓库数据。析构器里 `recycle_storage` 用连接位图做引用计数，**只有最后一个接收者释放时才真正归还仓库**（`sub_rc` 见 [ipc.cpp:L340-L360](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L340-L360)）。

#### 4.4.4 force_push 时的 storage_ 清理

当队列满、`send` 用 `force_push` 强行挤掉旧消息时，被挤掉的那条消息如果是大消息（`storage_=true`），其指向的仓库块必须被释放，否则泄漏。这由 `clear_message` 负责：

```cpp
template <typename MsgT>
bool clear_message(conn_info_head *inf, void* p) {
    auto msg = static_cast<MsgT*>(p);
    if (msg->storage_) {
        std::int32_t r_size = static_cast<std::int32_t>(ipc::data_length) + msg->remain_;
        release_storage(*reinterpret_cast<ipc::storage_id_t*>(&msg->data_), inf, r_size);
    }
    return true;
}
```
> 见 [ipc.cpp:L362-L376](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L362-L376)：`force_push` 的清理回调（调用点在 [L601-L605](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L601-L605)）。它复用了同样的 `r_size = data_length + remain_` 公式来还原大消息尺寸。

#### 4.4.5 两条路径对照

| 维度 | 分片路径（4.2 / 4.3） | 外部存储快路径（4.4） |
|------|----------------------|---------------------|
| 触发条件 | `size <= 64`，或 `size > 64` 但仓库分配失败 | `size > 64` 且仓库分配成功 |
| 占用槽位数 | ⌈size / 64⌉（200 字节占 4 个） | 恒为 1 |
| `storage_` | false | true |
| `data_` 内容 | 用户数据片段 | `storage_id_t` 取货单号 |
| 接收端动作 | `recv_cache` 按 `id_` 重组 | `find_storage` 直接取整块，零拷贝 |
| 回收 | 每个 `buff_t` 析构释放自己的重组缓冲 | 引用计数，最后接收者归还仓库 |

#### 4.4.6 代码实践：观察 storage_ 路径的触发

1. **实践目标**：用真实 demo 验证「大于 64 字节的消息走 storage_ 快路径」。
2. **操作步骤**：
   - 按 [u1-l2](u1-l2-build-and-run.md) 编译并启用 `LIBIPC_BUILD_DEMOS`，运行 `send_recv`。
   - 修改 `demo/send_recv`（或另写一个小程序），发送一条 **1000 字节**的消息（远大于 64）。
   - 在 `recv` 的大消息分支入口（[ipc.cpp:L666](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L666) 附近）临时加一行日志，打印 `msg.storage_` 与 `buf_id`。
3. **需要观察的现象**：日志显示 `msg.storage_ == 1`，且接收端拿到的 `buff_t::size() == 1000`，内容正确。
4. **预期结果**：1000 字节消息命中快路径，队列里只入队 1 个携带 `storage_id` 的槽位；`recv` 经 `find_storage` 取回整块 1000 字节。
5. **说明**：仓库（chunk）机制、`acquire_storage`/`calc_chunk_size`/`id_pool` 的内部细节请继续学习 [u3-l3](u3-l3-large-message-storage.md)。本步若无法运行 demo，则按源码阅读理解即可。

#### 4.4.7 小练习与答案

- **练习 1**：快路径里 `try_push` 为什么传 `size=0`？
  - **答案**：为了让 `msg_t` 构造函数算出 `storage_ = (data==nullptr) || (size==0) = true`，从而把传入的 `&(dat.first)` 当作 `storage_id_t` 写进 `data_`，而不是当作用户数据 memcpy。`size=0` 是「请把载荷当取货单号」的隐式信号。

- **练习 2**：大消息快路径返回的 `buff_t` 和分片路径返回的 `buff_t`，所有权语义有何不同？
  - **答案**：分片路径返回的是 `make_cache` 新分配并拷贝出来的缓冲，`buff_t` 析构时释放它（独占所有权）。快路径返回的 `buff_t` 指向**仓库里的共享内存**，析构时只做引用计数减一（`recycle_storage`），不能直接 free，因为别的接收者可能还在读。

---

## 5. 综合实践

**任务：做一张「消息从 send 到 recv 的分片/重组全流程图」，并用三种长度自测。**

1. 通读 [ipc.cpp:L527-L589](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L527-L589)（send）与 [ipc.cpp:L629-L737](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L629-L737)（recv）。
2. 画一张流程图，标注三个决策点：
   - `size > large_msg_limit(64)`？→ 尝试外部存储；
   - 外部存储成功？→ 走 storage_ 单片路径；否则 → 走分片路径；
   - `recv` 中 `msg.storage_`？→ 走 `find_storage`；否则 → 走 `recv_cache` 重组。
3. 对下面三种长度，分别写出**分片路径下**的「片数 + 每片 `remain_` + `recv` 端 `fill_` 序列」，再判断**实际**会走哪条路径：
   - **30 字节**（小于 64）
   - **64 字节**（恰好等于 64）
   - **300 字节**（大于 64）

参考答案（分片路径推演）：

- **30 字节**：`30/64=0`，循环不发；`remain=30>0` 发 1 片，`remain_=30-64=-34`。共 1 片。recv：`r_size=64+(-34)=30 <= 64`，首片即 `msg_size<=data_length` → 直接 `make_cache` 返回 30 字节，不进缓存。**实际路径**：30 ≤ 64，不走大消息，直接分片单片。
- **64 字节**：`64/64=1` 发 1 片 `remain_=64-0-64=0`；循环后 `remain=0` 不发尾部。共 1 片。recv：`r_size=64+0=64`，`msg_size<=64` 直接返回。**实际路径**：64 不大于 64，分片单片。
- **300 字节**：`300/64=4`，循环发 4 片，`remain_` = 300-0-64=**236**、300-64-64=**172**、300-128-64=**108**、300-192-64=**44**；循环后 `offset=256`，`remain=300-256=44>0`，尾片 `remain_=44-64=**-20**`。共 **5 片**。recv 的 `fill_` 序列：首片 `r_size=300` 分配 300 字节写入 64 → 64；之后 +64 →128、+64→192、+64→256；尾片 `r_size=64+(-20)=44`，+44 →300，返回 300 字节。**实际路径**：300 > 64，**优先走外部存储快路径**（单片 + storage_id）；只有仓库分配失败时才落到上述 5 片分片。

把这张图和三组答案与同组同学对照，若一致即说明你已掌握 `msg_t` 格式、`remain_` 编码与 `recv_cache` 重组。

## 6. 本讲小结

- `msg_t` = 定长头部（`cc_id_`/`id_`/`remain_`/`storage_`）+ 64 字节载荷槽；队列每槽是 `msg_t<64,16>`。`cc_id_` 是发送者身份证（过滤自发消息），`id_` 是原始消息号（**重组的键**），二者不可混。
- `send` 先为整条消息申请唯一 `msg_id`；`size > 64` 时优先走**外部存储快路径**（只发 1 片、载荷换成 `storage_id`、`storage_=true`），仓库失败才**分片**。
- 分片规则：满 64 字节的片段 `remain_ = size - offset - 64`（正），尾片段 `remain_ = remain - 64`（负）；统一公式 `r_size = data_length + remain_ = 从本片到消息末尾的字节数`。
- `recv` 用 `thread_local` 的 `recv_cache` 以 `msg.id_` 为键重组：首片按 `r_size` 预分配整条缓冲、中间片追加 64、尾片（`remain_<=0`）追加 `r_size` 后取走并删缓存项。200 字节 → 4 片（`remain_` 136/72/8/−56）→ `fill_` 64/128/192/200。
- `storage_` 标志由构造时 `size==0 || data==nullptr` 隐式推断，切换「载荷是数据还是取货单号」；接收端 `find_storage` 零拷贝取回整块，析构走引用计数回收；`force_push` 挤掉大消息时由 `clear_message` 释放仓库块。
- 缓存有两条防泄漏机制：超 1024 项时按 id 差距 > 8192 做 GC；连接断开时 `disconnect_receiver` 清空整个 `recv_cache`。

## 7. 下一步学习建议

本讲只打开了 `storage_` 这个开关，仓库内部还没展开。建议：

1. 学习 **[u3-l3 大消息的外部存储](u3-l3-large-message-storage.md)**：深入 `chunk_t` / `chunk_info_t` / `id_pool`、`acquire_storage` / `find_storage` / `release_storage`、`calc_chunk_size` 的 1KB 对齐，以及 `recycle_storage` 的跨接收者引用计数——把本讲 4.4 的「取货单号」补全。
2. 学习 **[u3-l4 等待模型](u3-l4-wait-model.md)**：搞清 `recv` 重组循环里 `wait_for(rd_waiter_, ...)` 与 `send` 里 `wait_for(wt_waiter_, ...)` 的「先自旋后阻塞」退避，理解分片/大消息路径背后的等待语义。
3. 若对底层无锁队列的 `push`/`pop`/`force_push` 如何调度这些 `msg_t` 槽位感兴趣，可预习 **[u4 无锁循环队列](u4-l1-queue-abstraction.md)**。
