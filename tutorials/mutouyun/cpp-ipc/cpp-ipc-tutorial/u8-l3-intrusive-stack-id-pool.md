# intrusive_stack 与 id_pool 无锁结构

## 1. 本讲目标

本讲是专家层（U8）的第三篇，承接 u7-l3（block_pool 分层缓存）。在 u7-l3 里，我们把 `central_cache_pool` 的两条无锁栈、以及 chunk 存储里的 `id_pool` 都当「黑盒」用了。本讲要把这两个黑盒拆开，看清楚它们的内部实现。

学完本讲你应该能够：

- 说清楚 `concur::intrusive_stack` 如何用单个原子指针 + CAS 实现一个无锁 LIFO 栈（Treiber 栈），以及 `push`/`pop` 各自的内存序选择。
- 说清楚 `id_pool` 如何「把数组当链表用」——用一个 `next_[]` 数组 + 一个 `cursor_` 游标实现 O(1) 的 `acquire`/`release` 空闲 id 分配器。
- 解释 `max_count` 为何是 32，以及它与 `large_msg_cache`、`uint_t<8>` 两个约束的关系。
- 把这两个结构放回它们各自的子系统：`intrusive_stack` 是 `central_cache_pool` 跨线程块流转的地基，`id_pool` 是大消息 chunk 仓库分配票据（storage_id）的地基。

本讲只读两个头文件库（无 `.cpp`，靠模板在使用处实例化），不展开 central cache 的上层路由（u7-l3 已讲）与大消息外存的引用计数回收（u3-l3 已讲）。

## 2. 前置知识

阅读本讲前，你需要具备以下概念（前序讲义已建立）：

- **原子操作与 CAS**：`compare_exchange_weak/strong` 的语义——「期望值匹配则替换并返回 true，否则把期望值更新为当前值并返回 false」。参见 u8-l1。
- **release/acquire 内存序配对**：生产者用 release「发布」数据、消费者用 acquire「看到」数据。参见 u8-l1。
- **central cache 两级缓存**：thread-local `block_pool`（L1）→ 进程级 `central_cache_pool`（L2）→ 1MB monotonic（L3）。参见 u7-l3。
- **大消息外部存储**：超过 `large_msg_limit`（64B）的消息走 chunk 仓库，队列里只传一个 4 字节 `storage_id` 票据。参见 u3-l3。
- **空闲链表（free list）**：把空闲资源串成链表，分配摘头、回收挂头，O(1) 完成。

两个本讲要用到的常量（来自 `def.h`）先列在这里：

```cpp
enum : std::size_t {
  large_msg_cache = 32,   // 大消息缓存数量上限
};
```

以及 `uint_t<8>` 是 libipc 自定义的 8 位无符号整数（即 `std::uint8_t`，范围 0\~255），定义在 [include/libipc/def.h:18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L18)。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 关键符号 |
|---|---|---|
| `include/libipc/concur/intrusive_stack.h` | 无锁侵入式栈（Treiber 栈） | `intrusive_node`、`intrusive_stack::push/pop` |
| `src/libipc/utility/id_pool.h` | 数组当链表的空闲 id 分配器 | `id_pool::acquire/release/prepare`、`obj_pool` |
| `include/libipc/def.h` | 提供 `uint_t<8>` 与 `large_msg_cache` 常量 | `large_msg_cache`、`uint_t` |
| `include/libipc/mem/central_cache_pool.h` | `intrusive_stack` 的使用方（central cache） | `cached_`/`aqueired_` 两条栈 |
| `src/libipc/ipc.cpp` | `id_pool` 的使用方（chunk 仓库） | `chunk_info_t`、`acquire_storage`/`release_storage` |
| `test/concur/test_concur_intrusive_stack.cpp` | `intrusive_stack` 的单元测试 | `push_one`/`pop_many` 等 |

注意：前两个文件都是**头文件库**——它们没有对应的 `.cpp`，完全靠模板在使用处（`central_cache_pool`、`chunk_info_t`）实例化。这也是它们能在无锁、跨线程场景下被「零成本」复用的前提。

## 4. 核心概念与源码讲解

本讲拆四个最小模块：① `intrusive_stack` 的 CAS 栈；② `id_pool` 的数组当链表；③ `max_count` 限制与 id 编码；④ 二者在子系统中的角色。

### 4.1 intrusive_stack：CAS 无锁栈（Treiber 栈）

#### 4.1.1 概念说明

`concur::intrusive_stack` 是一个**无锁（lock-free）后进先出栈**。它解决的问题是：多个线程要并发地往一个公共池子里存取「空闲资源指针」，但又不想加锁（加锁会让等待方陷入内核、抬高延迟）。

它叫「侵入式（intrusive）」是因为：栈本身**不拥有**节点内存，节点由调用方分配并持有，栈只负责把节点用 `next` 指针串起来。这样节点可以嵌进别的数据结构里（比如复用 `block` 的 `next` 字段），省掉单独的节点分配。

这种「单原子头指针 + CAS 摘头/挂头」的结构在文献里叫 **Treiber 栈**（以发明者 R. K. Treiber 命名），是无锁编程里最经典的范式之一。

#### 4.1.2 核心流程

整个栈只有一个共享可变状态：头指针 `top_`。两个核心操作都是「读旧头 → 算新头 → CAS 替换」的循环：

```
push(n):                      pop():
  old = top                     old = top
  loop:                         loop:
    n.next = old                  if old == null: return null
    if CAS(top, old, n):          new = old.next
      return                        if CAS(top, old, new):
  (CAS 失败则 old 被刷新,重试)          return old
                                (CAS 失败则 old 被刷新,重试)
```

直觉上：

- **push** 是「把新节点指向当前头，再试着把头换成新节点」。如果在我操作期间别的线程已经改了头，CAS 失败，我刷新 `old` 重试。
- **pop** 是「读当前头，准备把头换成头的下一个；若头为空就返回空」。同样靠 CAS 保证只有一个线程能成功摘走某个节点。

由于 CAS 是原子的，任意时刻只有一个线程能成功修改 `top_`，所以不需要互斥锁。失败方只需重试，不会阻塞——这就是「无锁」的含义。

#### 4.1.3 源码精读

先看节点定义。[include/libipc/concur/intrusive_stack.h:13-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h#L13-L19) 定义了侵入式节点：一个 `value`（承载实际数据，比如 `block_t*`）加一个原子 `next` 指针。

```cpp
template <typename T>
struct intrusive_node {
  T value;
  std::atomic<intrusive_node *> next;
};
```

栈本体只有一个成员——原子头指针 `top_`，初值为 `nullptr`，见 [include/libipc/concur/intrusive_stack.h:30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h#L30)。栈本身被声明为不可拷贝、不可移动（删除四件套），因为它是共享并发状态，拷贝/移动没有合理语义。

`push` 的实现见 [include/libipc/concur/intrusive_stack.h:44-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h#L44-L50)：

```cpp
void push(node *n) noexcept {
  node *old_top = top_.load(std::memory_order_acquire);
  do {
    n->next.store(old_top, std::memory_order_relaxed);
  } while (!top_.compare_exchange_weak(old_top, n, std::memory_order_release
                                                 , std::memory_order_acquire));
}
```

注意三个内存序的选择（承接 u8-l1 的「最小必要强度」哲学）：

- 入口 `load(acquire)`：读到别的线程 push 发布的节点。
- `n->next.store(..., relaxed)`：写自己节点的 `next` 用 relaxed，因为它**不需要单独的可见性保证**——紧接着的 CAS 用 `release` 序，会顺带把这次 `next` 的写入「打包发布」给后续 `acquire` 的线程。
- CAS 成功用 `release`（发布新头 `n` 及其 `next`），失败用 `acquire`（重新读取最新的 `top_` 到 `old_top`，准备下一轮）。`compare_exchange_weak` 在循环里允许伪失败（spurious failure），性能比 `strong` 更好。

`pop` 的实现见 [include/libipc/concur/intrusive_stack.h:52-62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h#L52-L62)：

```cpp
node *pop() noexcept {
  node *old_top = top_.load(std::memory_order_acquire);
  do {
    if (old_top == nullptr) {
      return nullptr;                       // 空栈
    }
  } while (!top_.compare_exchange_weak(old_top, old_top->next.load(std::memory_order_relaxed)
                                              , std::memory_order_release
                                              , std::memory_order_acquire));
  return old_top;
}
```

`pop` 的 CAS 期望值是 `old_top`、目标值是 `old_top->next`（摘掉头，让第二个节点顶上）。读 `old_top->next` 用 `relaxed`，因为真正建立 happens-before 的是 CAS 自身的 `acquire`/`release`。空栈时直接返回 `nullptr`——这正是上层 `central_cache_pool::aqueire` 判断「缓存空、需要扩容」的依据。

**关于 ABA 问题（重要 caveat）**：纯 Treiber 栈在理论上有经典的 **ABA 隐患**——线程 A 读到 `top=X` 后被挂起，期间 X 被弹出、又因某种原因被重新压回（地址不变、内容可能已变），A 醒来后 CAS 仍以为没人动过而成功，可能造成数据损坏。libipc 的 `central_cache_pool` 通过**节点生命周期**来规避危险情形：节点（`node_t`）从 1MB monotonic arena 分配、**永不按节点回收给 OS**（monotonic 只整体释放，参见 u7-l2），所以一个节点地址在程序运行期间始终指向合法的 `node_t`；配合 `aqueired_` 栈做节点回收复用，把「同一个节点指针被压/弹」控制在受控路径上。这是工程上常见的实用化缓解，而非形式化的 ABA-free 证明——理解这一点有助于你在二次开发时正确使用该栈。

#### 4.1.4 代码实践

项目自带 `intrusive_stack` 的单元测试，这是最直接的实践入口。

**实践目标**：通过阅读并运行测试，验证 Treiber 栈的 LIFO 行为与节点串接方式。

**操作步骤**：

1. 打开 [test/concur/test_concur_intrusive_stack.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/concur/test_concur_intrusive_stack.cpp)。注意文件开头有 `#define private public`（[第 4 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/concur/test_concur_intrusive_stack.cpp#L4)），这是为了在测试里直接访问私有成员 `top_` 来断言链表结构。
2. 重点看 `push_many`（[第 39-55 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/concur/test_concur_intrusive_stack.cpp#L39-L55)）：依次压入 `n1,n2,n3`，断言 `top_==&n3`、`n3.next==&n2`、`n2.next==&n1`、`n1.next==nullptr`——这正是后进先出的链。
3. 构建 libipc 测试目标（前提：用 CMake 时打开 `LIBIPC_BUILD_TESTS`，参见 u1-l2）：

   ```bash
   cmake -S . -B build -DLIBIPC_BUILD_TESTS=ON
   cmake --build build -j
   ./build/test/test-ipc --gtest_filter='intrusive_stack.*'
   ```

**需要观察的现象**：`push_many`、`pop_many`、`pop_empty` 等用例全部通过；`pop_many` 弹出顺序是 `n3,n2,n1`（与压入顺序相反），印证 LIFO。

**预期结果**：所有 `intrusive_stack.*` 用例 PASS。若你的环境未编译测试，本步骤标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `push` 里写 `n->next` 用 `relaxed`，而 CAS 成功用 `release`？能不能把 `n->next.store` 也换成 `release`？

> **答案**：不需要。`n->next` 的写入紧跟在 CAS 前，CAS 成功分支用 `release` 会把之前（同一线程内）的所有写入——包括 `n->next`——一并发布给将来 `acquire` 该 `top_` 的线程。所以单独给 `n->next.store` 加 `release` 是多余的强度。换成 `release` 不会出错，但违反 u8-l1 的「最小必要强度」原则，徒增开销。

**练习 2**：`pop` 在 `do-while` 循环里判空（`old_top == nullptr` 就返回），而不是在循环外判一次。为什么？

> **答案**：因为 CAS 失败后 `old_top` 会被自动刷新为最新的 `top_`。一轮循环里 `old_top` 非空，下一轮可能因为别的线程把栈弹空而变成 `nullptr`，所以必须每轮都重新判空，否则会对着空栈解引用 `old_top->next`。

### 4.2 id_pool：数组当链表的空闲分配器

#### 4.2.1 概念说明

`id_pool` 解决的是另一类问题：在一块**共享内存**里，固定有 N 个槽位（chunk），需要给每个「正在使用」的资源分配一个唯一编号（id），用完再回收。它要满足：

- O(1) 分配与回收；
- 状态全部躺在共享内存里，多个进程映射同一块内存时能共享同一份空闲链表；
- 支持懒初始化——第一个连上的进程负责建链表，后来的进程直接用。

它的巧妙之处在于：**不单独分配链表节点，而是复用 `next_[]` 数组本身当链表**。每个数组元素既是「id 编号承载体」，又（在 `obj_pool` 里）额外承担「数据存储体」。这种「数组的下标就是指针」的技巧在嵌入式与共享内存编程里很常见，因为共享内存里不能放真正的指针（各进程映射地址不同），只能用**偏移/下标**做链接。

#### 4.2.2 核心流程

`id_pool` 用一个数组 `next_[]` 模拟链表，`cursor_` 是链表头下标。`init()` 把数组串成一条「顺序链」：

```
init():  next_[i] = i+1   (i = 0..max_count-1)
         最后 next_[max_count-1] = max_count  (哨兵:"空")
         cursor_ = 0

         链表形态: cursor_=0 -> 1 -> 2 -> ... -> max_count(空)

acquire():  若 cursor_ == max_count:  返回 -1 (满)
            id = cursor_
            cursor_ = next_[id]      // 头指针后移
            返回 id                  // LIFO: 摘头

release(id): next_[id] = cursor_     // 新头指向旧头
             cursor_ = id            // 头指针前移到 id
             // LIFO: 挂头
```

可以看到，`acquire` 摘头、`release` 挂头，本质就是一个**用数组下标实现的 LIFO 空闲栈**——和 `intrusive_stack` 用指针实现的 Treiber 栈是同构的，只不过这里「指针」换成了「数组下标」，并且因为受外层 `spin_lock` 保护（见 4.4），不需要 CAS。

#### 4.2.3 源码精读

先看承载数据的元素类型。[src/libipc/utility/id_pool.h:17-34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L17-L34) 定义了 `id_type`：

```cpp
template <std::size_t AlignSize>
struct id_type<0, AlignSize> {        // 仅当 id,无数据
    uint_t<8> id_;
    // 与 storage_id_t 互转 ...
};

template <std::size_t DataSize, std::size_t AlignSize>
struct id_type : id_type<0, AlignSize> {   // id + 数据
    std::aligned_storage_t<DataSize, AlignSize> data_;
};
```

关键点：当 `DataSize==0`（即大消息 chunk 仓库用的 `id_pool<>`）时，元素只有 1 字节的 `id_`；当 `DataSize>0`（即 `obj_pool<T>`）时，元素还附带一块对齐的 `data_` 存储——这就是「数组元素兼任数据槽」的来源。`at(id)` 返回 `&next_[id].data_`（[第 90-91 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L90-L91)），即按 id 取对应数据槽。（注意：`id_pool<>` 的 `at()` 永远不会被实例化，因为 chunk 仓库用的是 `chunk_info_t::at` 而非 `pool_.at`，所以 `DataSize==0` 时访问不存在的 `data_` 不会触发编译错误——模板成员按需实例化。）

`init()` 是全篇最精巧的一行循环，见 [src/libipc/utility/id_pool.h:61-65](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L61-L65)：

```cpp
void init() {
    for (storage_id_t i = 0; i < max_count;) {
        i = next_[i] = (i + 1);     // 一行干两件事:写链表 + 推进 i
    }
}
```

这一行同时完成「把 `next_[i]` 指向 `i+1`」和「把循环变量 `i` 推进到 `i+1`」。展开就是 `next_[0]=1, next_[1]=2, …, next_[31]=32`，其中 `32 == max_count` 正好当「空」哨兵。

`acquire`/`release` 是标准的摘头/挂头，见 [src/libipc/utility/id_pool.h:76-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L76-L88)：

```cpp
storage_id_t acquire() {
    if (empty()) return -1;
    storage_id_t id = cursor_;
    cursor_ = next_[id];     // 头指针后移
    return id;
}

bool release(storage_id_t id) {
    if (id < 0) return false;
    next_[id] = cursor_;     // 新头指向旧头
    cursor_ = static_cast<uint_t<8>>(id);  // 头指针前移
    return true;
}
```

`empty()` 判 `cursor_ == max_count`（[第 72-74 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L72-L74)）——当链表走到哨兵 `max_count` 即表示没有空闲 id。`acquire` 满了返回 `-1`，`release` 对负 id 直接返回 false，二者构成了「票据」的获取与归还。

懒初始化靠 `prepare()`/`invalid()` 配合，见 [src/libipc/utility/id_pool.h:56-70](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L56-L70)：

```cpp
void prepare() {
    if (!prepared_ && this->invalid()) this->init();
    prepared_ = true;
}
bool invalid() const {
    static id_pool inv;     // 全零的默认实例
    return std::memcmp(this, &inv, sizeof(id_pool)) == 0;
}
```

`invalid()` 把「自己」和一块全零的默认 `id_pool` 逐字节比较。全新的共享内存内容全零，与 `inv` 完全相等 → `invalid()` 返回 true → 第一个调用 `prepare()` 的进程执行 `init()` 建链表；`init()` 后字节不再全零，后续进程的 `invalid()` 返回 false，跳过初始化。这是共享内存里「首引用者初始化」的经典手法（在 chunk 仓库里还配了 `spin_lock` 兜底，见 4.4）。

#### 4.2.4 代码实践

**实践目标**：手动演算 id_pool 连续 `acquire` 3 次、`release` 第 2 个后，`cursor_` 与 `next_[]` 的变化。这是本讲指定的实践任务。

为方便演算，记 `max_count = 32`（推导见 4.3）。`init()` 后初始状态：

- `cursor_ = 0`
- `next_[0]=1, next_[1]=2, next_[2]=3, …, next_[31]=32`

**步骤 1：第 1 次 `acquire()`**

- 非空（`cursor_=0 ≠ 32`）
- `id = cursor_ = 0`
- `cursor_ = next_[0] = 1`
- 返回 `0`

**步骤 2：第 2 次 `acquire()`**

- `id = cursor_ = 1`
- `cursor_ = next_[1] = 2`
- 返回 `1`

**步骤 3：第 3 次 `acquire()`**

- `id = cursor_ = 2`
- `cursor_ = next_[2] = 3`
- 返回 `2`

此时：`cursor_ = 3`，已分配 id = {0, 1, 2}，链表形态 `cursor_=3 -> next_[3]=4 -> … -> next_[31]=32`。

**步骤 4：`release(1)`（归还第 2 个，即 id=1）**

- `id=1 ≥ 0`
- `next_[1] = cursor_ = 3`（原本 `next_[1]=2` 被改写为 3）
- `cursor_ = 1`
- 返回 `true`

此时：`cursor_ = 1`，空闲链表形态 `cursor_=1 -> next_[1]=3 -> next_[3]=4 -> … -> next_[31]=32`。id {0, 2} 仍在使用中，id 1 回到链表头部。

**需要观察的现象**：下一次 `acquire()` 会返回 `1`（LIFO——刚归还的最先被重新分配），而不是 `3`。这正是「最近归还者优先复用」的缓存友好特性。

**预期结果**：完整演算表如下——

| 时刻 | cursor_ | 关键 next_[] 变化 | 返回 |
|---|---|---|---|
| init 后 | 0 | next_[i]=i+1 | — |
| acquire#1 | 1 | — | 0 |
| acquire#2 | 2 | — | 1 |
| acquire#3 | 3 | — | 2 |
| release(1) | 1 | next_[1]: 2→3 | true |
| acquire#4 | 3 | — | 1（复用刚归还的） |

#### 4.2.5 小练习与答案

**练习 1**：`init()` 里为什么循环条件是 `i < max_count`，而 `next_[max_count-1]` 被赋值为 `max_count`？这个 `max_count` 值合法吗（它等于数组越界下标吗）？

> **答案**：循环最后一次 `i=31`（`max_count-1`），执行 `next_[31]=32` 后 `i` 变为 `32`，不再 `< 32`，循环结束。`32` 作为**值**存进 `next_[31].id_`（`uint_t<8>`，能容纳 0\~255），它是「链表到此为空」的哨兵，不是数组下标——没有任何代码拿 `32` 去索引 `next_[32]`。`empty()` 正是用 `cursor_==32` 判空。所以它不是越界下标，而是合法的哨兵值。

**练习 2**：把 `id_pool` 的 `acquire/release` 和 4.1 的 `intrusive_stack::push/pop` 对比，两者在结构上有什么同构关系？为什么 `id_pool` 不用 CAS？

> **答案**：两者都是 LIFO——`acquire` 对应 `pop`（摘头）、`release` 对应 `push`（挂头），只不过 `intrusive_stack` 用「指针」链接节点、`id_pool` 用「数组下标」链接元素。`id_pool` 不用 CAS，是因为它的调用方（chunk 仓库）用 `spin_lock` 把 `acquire`/`release` 整个保护起来了（见 4.4），是单线程进入临界区，不需要无锁；而 `intrusive_stack` 服务于跨线程的 central cache，必须在无锁下保证并发安全，所以用 CAS。

### 4.3 max_count 限制与 id 编码

#### 4.3.1 概念说明

`id_pool` 能管理多少个槽位？答案是 `max_count`，它由两个约束取最小值决定。这个数字直接决定了「同一 chunk_size 桶里最多能同时有多少条大消息在飞」，是 u3-l3 大消息外存容量的上限之一。

#### 4.3.2 核心流程

`max_count` 的计算见 [src/libipc/utility/id_pool.h:40-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L40-L48)：

```cpp
static constexpr std::size_t limited_max_count() {
    return ipc::detail::min<std::size_t>(large_msg_cache,
                                         (std::numeric_limits<uint_t<8>>::max)());
}
enum : std::size_t { max_count = limited_max_count() };
```

即：

\[
\text{max\_count} = \min(\text{large\_msg\_cache},\ 2^{8}-1) = \min(32,\ 255) = 32
\`

两个约束的来源：

- **`large_msg_cache = 32`**（[include/libipc/def.h:38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L38)）：策略常量，限制每个 chunk_size 桶缓存的大消息数量。
- **`uint_t<8>` 的最大值 255**：类型约束——`cursor_` 与 `id_` 都是 1 字节（见 [id_pool.h:51-52](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L51-L52)），所以 id 理论上限是 255。

取 `min` 后，**真正起作用的是 `large_msg_cache=32`**（32 < 255）。选 `uint_t<8>` 是为了紧凑——`next_[]` 每个元素只占 1 字节 id（外加可能的对齐填充），整张表很小。注意哨兵值 `max_count=32` 本身也必须能放进 `uint_t<8>`，32 < 255 满足。

> 小提示：这里的 32 与 u2-l4 讲的「广播 32 接收者上限」数字相同，但来源不同——后者来自连接位图 `cc_t = uint32`（每个接收者占 1 bit）；本讲的 32 来自 `large_msg_cache`。两者是独立的常量，碰巧取值相同。

#### 4.3.3 源码精读

`max_count` 决定了两个直接后果：

1. **数组大小**：`next_[max_count]`（[id_pool.h:51](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L51)），即 32 个元素。
2. **chunk 仓库内存大小**：在 [src/libipc/ipc.cpp:201-203](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L201-L203)，`chunk_info_t::chunks_mem_size(chunk_size) = id_pool<>::max_count * chunk_size`，即每个 chunk_size 桶预留 32 个 chunk 的连续内存。

id 的编码上，`storage_id_t` 是 `std::int32_t`（[id_pool.h:12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L12)），而内部 `id_` 用 `uint_t<8>`。`acquire` 满时返回 `-1`，所以用有符号的 `int32_t` 承载「合法 id（0\~31）」与「无效（-1）」两种语义。`obj_pool<T>`（[id_pool.h:94-101](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/id_pool.h#L94-L101)）继承 `id_pool<sizeof(T), alignof(T)>` 并把 `at(id)` 的返回 `reinterpret_cast` 成 `T*`，给「带数据的对象池」复用同一套分配逻辑。

#### 4.3.4 代码实践

**实践目标**：验证 `max_count` 在你的平台上的实际取值，并理解它与 `large_msg_cache` 的绑定关系。

**操作步骤**：

1. 写一个最小程序（示例代码，非项目原有代码）：

   ```cpp
   #include "libipc/utility/id_pool.h"
   #include <cstdio>
   int main() {
       printf("max_count = %zu\n", ipc::id_pool<>::max_count);
       printf("large_msg_cache = %zu\n", ipc::large_msg_cache);
       return 0;
   }
   ```

2. 用编译器直接编译（头文件库，无需链接整个 libipc；需把 `src/` 加入包含路径，因为 `id_pool.h` include 了 `libipc/platform/detail.h`）：

   ```bash
   g++ -std=c++17 -Iinclude -Isrc demo_maxcount.cpp -o demo_maxcount && ./demo_maxcount
   ```

**需要观察的现象**：输出 `max_count = 32` 与 `large_msg_cache = 32`。

**预期结果**：两者相等。若把 `def.h` 里的 `large_msg_cache` 改成更小的值（例如 16）重新编译，`max_count` 会随之变成 16——印证「`large_msg_cache` 是绑定约束」。本步骤标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `large_msg_cache` 改成 200，`max_count` 会变成多少？会有什么隐患？

> **答案**：`min(200, 255) = 200`，`max_count` 变成 200。隐患：① `next_[]` 数组变大，每个 chunk_size 桶的共享内存占用从 32 个 chunk 涨到 200 个；② 哨兵值 200 仍能放进 `uint_t<8>`（< 255），类型上尚可。但如果改成超过 255（例如 300），`min(300,255)=255`，类型约束会接管，`max_count` 被「夹」到 255，`cursor_` 仍用 `uint_t<8>` 不会溢出——这正是 `min` 里带上 `uint8_max` 的防御意义。

**练习 2**：`acquire` 满了返回 `-1`，但 `id_` 是无符号 `uint_t<8>`。这个 `-1` 是怎么在系统里流转的？

> **答案**：`acquire` 的返回类型是有符号 `storage_id_t`（`int32_t`），满时直接 `return -1`，并不经过 `id_`。`-1` 是给调用方（`acquire_storage` 等）看的「失败」信号；调用方拿到负值就走兜底逻辑（如大消息回退到分片）。合法 id（0\~31）才被存进 `next_[].id_` 的 `uint_t<8>`。`release` 也先判 `id < 0` 拦截无效值。

### 4.4 在子系统中的角色：central_cache_pool 与 chunk 存储

#### 4.4.1 概念说明

前面两节把两个结构单独讲透了。本节把它们放回各自的子系统，回答「库为什么需要它们」。

- `intrusive_stack` 是 **`central_cache_pool`（L2 缓存）** 的地基：进程内所有线程的 `block_pool`（L1）共用同一个 `central_cache_pool` 单例，块在「各线程」与「中央」之间流转，必须无锁且线程安全，这正是 Treiber 栈的用武之地。
- `id_pool` 是 **大消息 chunk 仓库** 的地基：每条大消息需要一个唯一 `storage_id` 指向仓库里的 chunk 槽，`id_pool` 负责 id 的分配与回收，且状态躺在共享内存里供多进程共享。

#### 4.4.2 核心流程

**central_cache_pool 用两条栈做块流转**（承接 u7-l3）：

```
aqueire():  block = cached_.pop()      // 从可用栈摘一个块指针
            若非空: aqueired_.push(node) // 把承载节点转入记账栈,返回块
            若空:    向 1MB monotonic 申请一个 chunk,返回首块

release(p): node = aqueired_.pop()     // 复用一个记账节点
            若空:    申请新 node
            node.value = p
            cached_.push(node)         // 把块挂回可用栈
```

**chunk 仓库用 id_pool 发票据**（承接 u3-l3）：

```
acquire_storage():  加 spin_lock → pool_.prepare() → id = pool_.acquire() → 解锁
                   chunk = chunks_mem() + chunk_size * id   // 按 id 定位 chunk
                   返回 {id, chunk->data()}

find_storage(id):   按 id 直接定位 chunk（只读,不加锁）

release_storage(id)/recycle_storage(id):
                   引用计数归零后,加 spin_lock → pool_.release(id) → 解锁
```

#### 4.4.3 源码精读

先看 `central_cache_pool` 如何使用 `intrusive_stack`，见 [include/libipc/mem/central_cache_pool.h:42-46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L42-L46)：

```cpp
using node_t = typename concur::intrusive_stack<block_t *>::node;
concur::intrusive_stack<block_t *> cached_;    // 可用块指针栈
concur::intrusive_stack<block_t *> aqueired_;  // 记账节点栈
```

`node_t` 是「值为 `block_t*` 的侵入式节点」——栈里串的不是块本身，而是**指向块的指针**。`aqueire`（[第 51-66 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L51-L66)）先 `cached_.pop()`，命中就把承载节点转入 `aqueired_` 并返回块指针；未命中则向 1MB monotonic 申请一个 chunk。`release`（[第 68-77 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L68-L77)）从 `aqueired_` 复用一个节点，装入块指针后挂回 `cached_`。两条栈的分工：`cached_` 管「可分发」，`aqueired_` 管「节点回收复用」，避免每次 release 都分配新节点。

再看 chunk 仓库如何使用 `id_pool`，见 [src/libipc/ipc.cpp:197-213](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L197-L213)：

```cpp
struct chunk_info_t {
    ipc::id_pool<> pool_;       // 空闲 id 分配器
    ipc::spin_lock lock_;       // 保护 pool_ 的自旋锁
    static /*constexpr*/ std::size_t chunks_mem_size(std::size_t chunk_size) {
        return ipc::id_pool<>::max_count * chunk_size;   // 32 个 chunk 的空间
    }
    ipc::byte_t *chunks_mem() noexcept { return reinterpret_cast<ipc::byte_t *>(this + 1); }
    chunk_t *at(std::size_t chunk_size, ipc::storage_id_t id) noexcept {
        if (id < 0) return nullptr;
        return reinterpret_cast<chunk_t *>(chunks_mem() + (chunk_size * id));  // 按 id 偏移定位
    }
};
```

`chunk_info_t` 紧挨着共享内存头部，其后跟 `max_count`（32）个 chunk 的连续区。`id_pool` 在这里**只用 acquire/release/prepare**，不用 `at()`——chunk 的定位靠 `chunks_mem() + chunk_size * id` 的指针算术。`acquire_storage`（[第 278-293 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L278-L293)）在 `spin_lock` 保护下 `prepare()` + `acquire()` 拿到 id，再按 id 写入数据；`release_storage`/`recycle_storage`（[第 307-360 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L307-L360)）在引用计数归零后 `release(id)` 归还票据。

> 对照要点：`central_cache_pool` 是**进程内**跨线程共享，用无锁的 `intrusive_stack`；`chunk_info_t` 是**跨进程**共享（躺在命名共享内存里），用 `spin_lock` 保护的 `id_pool`。前者追求极致低延迟（热路径、频繁调用），后者调用频率低（仅大消息），用简单自旋锁换取实现简单与跨进程安全。

#### 4.4.4 代码实践

**实践目标**：跟踪一条大消息从「申请票据」到「归还票据」的完整路径，把 `id_pool` 放回 chunk 仓库的上下文。

**操作步骤**（源码阅读型实践）：

1. 从 `detail_impl::send` 出发，找到大消息分支（`size > large_msg_limit`）调用 `acquire_storage` 的位置（参见 u3-l3）。
2. 读 [src/libipc/ipc.cpp:278-293](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L278-L293) 的 `acquire_storage`：它先 `info->lock_.lock()`，再 `pool_.prepare()`（首次建链表），`id = pool_.acquire()`，然后解锁。注意 `acquire` 返回 `-1` 时（仓库满），函数返回空 pair，上层会回退到分片发送。
3. 读接收端 `find_storage`（[第 295-305 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L295-L305)）：只按 id 定位 chunk，**不加锁**（只读）。
4. 读 `recycle_storage`（[第 340-360 行](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L340-L360)）：它先做引用计数 `sub_rc`，最后一个读完的接收者才在锁内 `pool_.release(id)` 归还票据。

**需要观察的现象**：`acquire` 和 `release` 始终在 `spin_lock` 临界区内成对出现；`find_storage` 不加锁。

**预期结果**：你能画出「`acquire_storage`(锁内取 id) → 写 chunk → 队列传 id → 各接收者 `find_storage`(无锁读) → 最后一人 `recycle_storage`(锁内还 id)」的时序，并解释为何只有分配/回收需要锁、而读取不需要。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `central_cache_pool` 用无锁栈、而 `chunk_info_t` 用自旋锁保护的 `id_pool`？能否互换（给 central cache 加锁、给 chunk 仓库用无锁 id_pool）？

> **答案**：选型匹配调用频率与场景。central cache 是内存分配的热路径，每个 `mem::$new` 都可能触达，必须无锁以保低延迟；且进程内跨线程场景下 CAS 栈已被验证可靠。chunk 仓库只在大消息（>64B）路径上用，调用稀疏，自旋锁的临界区极短（一次数组下标操作），开销可忽略；且它的状态要跨进程共享，用 `id_pool`（数组下标链接，进程无关）比用指针链接的栈更自然。互换并非不可行，但会牺牲 central cache 的延迟优势，或让 chunk 仓库的实现复杂化，得不偿失。

**练习 2**：`chunk_info_t` 里 `pool_` 的 `prepare()` 为什么必须放在 `lock_` 临界区内？

> **答案**：`prepare()` 会判断「是否首次」并可能执行 `init()` 改写整张 `next_[]` 表。若两个进程同时进入而未加锁，可能同时判定 `invalid()` 为 true、同时 `init()`，造成写竞争与表损坏。`spin_lock` 保证「首引用者初始化」是原子的——只有一个进程真正执行 `init()`，后来的进程看到 `prepared_=true`（或字节非全零）后跳过。

## 5. 综合实践

**任务**：把本讲两个结构与它们各自的上层串起来，画一张「资源分配全景图」，并手算两组数据。

**背景**：libipc 里有两个「分配器」——一个是内存块分配（`block_pool` → `central_cache_pool` → `intrusive_stack`），一个是 chunk 槽位分配（`chunk_info_t` → `id_pool`）。它们都用了「LIFO 空闲链表」的思想，但实现截然不同。

**操作步骤**：

1. 画一张对比图，左半边是「内存块路径」：`block_pool`(thread_local 链表) → `central_cache_pool::aqueire` → `cached_.pop()`(Treiber CAS 栈) → 1MB monotonic。右半边是「chunk 票据路径」：`acquire_storage` → `pool_.acquire()`(数组下标链表, spin_lock 保护) → 按 id 偏移定位 chunk。
2. 在图上标注两个关键差异：① 链接方式（指针 vs 数组下标）；② 并发保护（CAS 无锁 vs spin_lock）。
3. 手算两组数据：
   - **intrusive_stack 侧**：向一个 `concur::intrusive_stack<int>` 依次 push 节点 `n1,n2,n3`，画出 `top_` 与各节点 `next` 的指向（对照 4.1.4 的测试断言）。
   - **id_pool 侧**：完成 4.2.4 的演算（acquire×3 → release(1) → acquire），写出每步的 `cursor_` 与 `next_[1]`。
4. 回答收口问题：这两种结构都实现了 LIFO 空闲链表，为什么 libipc 不统一用一种？

**预期结果**：

- `intrusive_stack` 侧：`top_ → n3 → n2 → n1 → null`。
- `id_pool` 侧：`cursor_` 序列为 `0→1→2→3→(release1)→1→(acquire)→3`，`next_[1]` 从 `2` 变为 `3`。
- 收口答案要点：进程内热路径用无锁指针栈换延迟；跨进程稀疏路径用数组下标链表换「地址无关」与实现简单。两者是「同一思想、不同工程取舍」。

> 提示：若想加深理解，可对照 u7-l3 的「`mem::$new<obj>(4096)` 内存路由」与 u3-l3 的「100KB 大消息 chunk 路径」，把本讲的两张图分别嵌进去——你会发现 `intrusive_stack` 与 `id_pool` 正是那两条路径最底层的「地基」。

## 6. 本讲小结

- `concur::intrusive_stack` 是经典 **Treiber 栈**：单个原子头指针 `top_` + CAS 摘头/挂头，`push`/`pop` 都用 `compare_exchange_weak` 配 `release`/`acquire` 内存序，`n->next` 的写入靠随后的 release CAS 顺带发布。
- `id_pool` 把**数组当链表**：`next_[]` 既存 id 又（在 `obj_pool` 里）兼任数据槽，`cursor_` 是头下标，`acquire` 摘头、`release` 挂头，O(1) 完成分配回收；`init()` 用一行 `i = next_[i] = (i+1)` 同时建链表与推进循环。
- `max_count = min(large_msg_cache, uint8_max) = 32`，绑定约束是策略常量 `large_msg_cache=32`；选 `uint_t<8>` 是为紧凑存储，哨兵值 32 也放得下。这个 32 与广播接收者上限的 32 来源不同。
- `invalid()` 用「与全零实例 memcmp」实现共享内存懒初始化：首引用进程 `init()` 建链表，后续进程跳过。
- 二者的子系统角色：`intrusive_stack` 是 `central_cache_pool` 跨线程块流转的无锁地基（进程内、热路径、CAS）；`id_pool` 是 chunk 仓库分配 `storage_id` 票据的地基（跨进程、稀疏路径、`spin_lock` 保护）。
- 两个结构本质同构（都是 LIFO 空闲链表），区别只在「链接载体（指针 vs 下标）」与「并发策略（无锁 vs 自旋锁）」——这是同一思想的两种工程取舍。

## 7. 下一步学习建议

- **u8-l1（内存序、伪共享与缓存行）**：本讲对 `intrusive_stack` 的 CAS 内存序只做了最小解释，深入理解 `release`/`acquire` 如何在 Treiber 栈里建立 happens-before、以及为何 `n->next.store` 用 `relaxed` 是安全的，可回看 u8-l1 的系统讲解。
- **u8-l2（健壮锁的崩溃恢复）**：本讲的 `chunk_info_t::lock_` 是普通 `spin_lock`，它保护 `id_pool` 但**无法**应对持锁进程崩溃。跨进程的崩溃恢复需要 robust 锁——u8-l2 讲解了 `EOWNERDEAD`/`WAIT_ABANDONED` 的恢复链路，可与本讲的「`id_pool` 懒初始化 + spin_lock」对照，理解为何 chunk 仓库在崩溃后可能留下「全零以外的脏状态」需要 `clear_storage` 兜底。
- **u7-l2 / u7-l3（内存子系统）**：想把 `intrusive_stack` 放回完整的 `block_pool → central_cache_pool → 1MB monotonic` 链路，或理解 `node_t` 为何从 monotonic 分配而永不被单独回收（这是缓解 ABA 的关键），可回看这两讲。
- **u3-l3（大消息外部存储）**：本讲的 `id_pool` 是 u3-l3 chunk 仓库的底层，把两者合读可看清「票据 storage_id 的全生命周期」。
- **二次开发提示**：如果你要新增一个「跨进程、固定槽位、稀疏访问」的资源池，`id_pool` + `spin_lock` 是现成的范本；若是「进程内、高频、无锁」的场景，则参考 `intrusive_stack`，但务必评估 ABA 风险——节点生命周期管理是安全性的关键。
