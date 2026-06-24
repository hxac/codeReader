# buffer：消息容器 buff_t

## 1. 本讲目标

本讲聚焦 libipc 里唯一的「消息容器」类型——`buffer`（也就是你在 `recv` 时拿到的 `buff_t`）。读完本讲你应该能够：

- 说清 `buffer` 用 PIMPL 隐藏了什么、对外暴露了哪些字段。
- 理解 `destructor_t` 回调函数指针的含义，并能判断「哪些构造函数会持有所有权、哪些只是非拥有视图」。
- 掌握 `empty / data / size / get / to_tuple / to_vector / operator==` 这一整套只读访问接口。
- 解释 `buffer` 为什么是「只能 move、不能 copy」的，以及 move 之后为什么不会发生双重释放。
- 把 `recv` 返回的 `buff_t` 安全地转成 `std::string` 或 `std::vector<byte_t>`。
- 理解「大消息零拷贝回收」是如何靠把回收逻辑塞进 `destructor_t` 来实现的。

本讲只讲 `buffer` 本身，不涉及它背后的队列、共享内存与等待模型（那些属于 U3/U5/U6）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要一个专门的「消息容器」？** 进程间通信的核心动作是「把一块字节从 A 进程搬到 B 进程」。这块字节的来源五花八门：可能是一段栈上的字符数组、一块 `malloc` 出来的堆内存、也可能是一条大到放不进队列、必须存在独立共享内存里的「大消息」。`buffer` 的职责就是给这些来源各异的字节块套上**统一的外壳**：无论数据从哪来、要不要释放、怎么释放，对调用方而言都是同一个类型 `buff_t`，统一用 `data()` 取指针、`size()` 取长度。

**什么是「析构器回调」？** 这里的「析构器」不是 C++ 的析构函数，而是一个**函数指针** `void(*)(void*, std::size_t)`。当 `buffer` 被销毁时，库会回调这个函数指针，把「该释放的那块内存的指针」和「它的尺寸」交还给用户。这样 `buffer` 就能在不认识具体内存来源（`malloc`、共享内存、还是别的）的前提下，把释放工作**委托**回真正懂得如何释放的那一方。这是后续「大消息零拷贝回收」能成立的关键。

**什么是 PIMPL？** PIMPL = Pointer to IMPLementation（指向实现的指针）。它在头文件里只放一个前置声明的内嵌类指针（`class buffer_; buffer_* p_;`），把真正的成员变量（指针、尺寸、析构器）藏进 `.cpp` 文件里的 `buffer_` 类。好处是：头文件干净、ABI 稳定（改内部成员不会破坏二进制兼容）、对外只暴露一个不透明指针。`buffer`、`chan_wrapper`、`shm::handle` 都用了这套手法，这也是 u1-l3 里提到的「公共 API 对外只暴露 `void*` 不透明句柄」的具体落地。

> 补充术语：`byte_t` 与 `std::size_t` 都来自 u2-l1 讲过的 `def.h`；`byte_t` 是固定宽度的字节类型，跨平台布局一致。`buff_t` 只是 `buffer` 的别名（见 4.1.3）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `include/libipc/buffer.h` | `buffer` 的**公共声明**（头文件库） | 构造函数重载、`destructor_t`、访问/转换接口、PIMPL 成员 |
| `src/libipc/buffer.cpp` | `buffer` 的**实现** | 内部类 `buffer_`、析构时如何回调、move/swap/赋值 |
| `src/libipc/utility/pimpl.h` | PIMPL 的**通用辅助工具** | `make / clear / impl` 的小对象优化分派 |
| `include/libipc/ipc.h` | 把 `buffer` 绑定为 `buff_t` 并接入通道 | `using buff_t = buffer;` 与 `recv` 返回类型 |
| `src/libipc/ipc.cpp` | 真实使用场景 | 大消息 `recv` 把回收逻辑塞进 `destructor_t` |
| `demo/chat/main.cpp`、`demo/send_recv/main.cpp` | 用法示例 | `empty()` 判空、`get<>()`、`size()` 转 `std::string` |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 是整体外观（PIMPL），4.2 是构造与析构器（所有权核心），4.3 是数据访问与转换接口，4.4 是 move/swap/赋值语义。

### 4.1 buffer 是什么：消息容器与 PIMPL 外观

#### 4.1.1 概念说明

`buffer` 是 libipc 对「一段字节消息」的统一封装。它把三样东西打包在一起：

1. **数据指针** `p`：消息内容在哪。
2. **尺寸** `s`：消息有多少字节。
3. **析构器** `d`（可选）：这块数据在 `buffer` 销毁时该如何释放。

只要这三样齐了，无论数据来自栈、堆还是共享内存，调用方拿到的都是同一个 `buff_t`，收发逻辑因此可以保持统一。

`buff_t` 并不是新类型，只是 `buffer` 的别名：

```cpp
using buff_t = buffer;   // ipc.h
```

`recv` 的返回类型就是它：`buff_t recv(ipc::handle_t h, std::uint64_t tm);`。所以你每收一条消息，都会拿到一个 `buff_t`。

#### 4.1.2 核心流程

一个 `buffer` 的一生可以概括为三阶段：

```text
[构造] 绑定 (数据指针 p, 尺寸 s, 析构器 d)
   │      └─ 依据 d 是否为空，决定「持有所有权」还是「非拥有视图」
   ▼
[访问] data() / size() / empty() / get<T>() / to_vector() …   （只读使用）
   │      └─ 期间可被 move / swap 转移给另一个 buffer
   ▼
[析构] ~buffer()  ──>  内部 ~buffer_()
                       └─ 若 d != nullptr，回调 d(该释放的指针, s)，且仅一次
```

关键不变量：

- 析构器**至多回调一次**（move 之后源对象变空，不会二次释放，见 4.4）。
- 「是否持有所有权」完全由构造时传入的 `d` 是否为空决定，与数据指针的来源无关。

#### 4.1.3 源码精读

先看公共声明的骨架——整个 `buffer` 类在头文件里只有几十行，真正干活的全藏在 `buffer_` 后面：

[include/libipc/buffer.h:13-32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L13-L32) —— 定义 `destructor_t` 函数指针类型、列出全部构造函数与析构/move 声明。注意第 15 行：

```cpp
using destructor_t = void (*)(void*, std::size_t);
```

这就是「析构器」的真身——一个普通的 C 函数指针，参数是 `(待释放指针, 尺寸)`。

[include/libipc/buffer.h:65-68](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L65-L68) —— PIMPL 的全部「对外证据」就这两行：

```cpp
class buffer_;   // 前置声明，定义在 .cpp
buffer_* p_;     // 唯一的成员：一个指向实现的指针
```

真正的成员变量（`p_ / s_ / a_ / d_`）藏在实现文件里：

[src/libipc/buffer.cpp:16-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L16-L31) —— 内部类 `buffer_`，它持有真正的四个字段，并在自己的析构函数里回调 `d_`：

```cpp
class buffer::buffer_ : public pimpl<buffer_> {
public:
    void*       p_;   // 数据指针
    std::size_t s_;   // 尺寸
    void*       a_;   // 「真正要释放的指针」别名（见 4.2 的 mem_to_free）
    buffer::destructor_t d_;   // 析构器回调
    buffer_(void* p, std::size_t s, buffer::destructor_t d, void* a)
        : p_(p), s_(s), a_(a), d_(d) {}
    ~buffer_() {
        if (d_ == nullptr) return;                 // 非拥有视图：什么都不做
        d_((a_ == nullptr) ? p_ : a_, s_);          // 否则回调一次
    }
};
```

最后，`buff_t` 这个别名、以及它在收发接口里的位置：

[include/libipc/ipc.h:13-13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L13-L13) —— `using buff_t = buffer;`。

[include/libipc/ipc.h:44-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L44-L44) —— `static buff_t recv(ipc::handle_t h, std::uint64_t tm);`，`recv` 返回的就是 `buff_t`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「`buff_t` 就是 `buffer`」，并找出所有返回 / 接收 `buff_t` 的公共接口。

**操作步骤**：

1. 在 `include/libipc/ipc.h` 中搜索 `buff_t`，列出它的定义位置与所有出现它的函数签名。
2. 对照 u1-l4，回忆 `chan_wrapper::recv()` 与 `chan_wrapper::send(buff_t const&)`。

**需要观察的现象**：`buff_t` 仅在 `ipc.h:13` 定义一次；`recv / try_recv` 返回它，`send / try_send` 有一个以 `buff_t const&` 为参数的重载（见 4.3.3）。

**预期结果**：你会确认「整个库对外只有 `buffer` 这一种消息容器类型」，所有收发都绕着它转。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `buffer` 要用 PIMPL，而不是直接把 `p_ / s_ / d_` 写在头文件里？
**参考答案**：一是 ABI 稳定——将来给 `buffer_` 增减字段不会改变 `buffer` 的sizeof，调用方无需重新编译；二是头文件干净、不暴露内部细节（比如 `a_` 这个别名字段的存在）；三是和库其它类型（`chan_wrapper`、`shm::handle`）保持一致的「不透明句柄」风格。

**练习 2**：`buffer::buffer_` 这个内部类为什么 `public` 继承自 `pimpl<buffer_>`？
**参考答案**：为了复用 `pimpl` 提供的 `make / clear` 静态工具（见 4.2.3）。`pimpl<T>` 本身没有数据成员，继承它只是「借」来一组工厂方法，不会增加 `buffer_` 的体积。

---

### 4.2 构造与析构器 destructor_t：资源所有权

这是本讲最核心的模块——理解了它，你就理解了 `buffer` 如何安全地「持有并释放」任意来源的内存。

#### 4.2.1 概念说明

`buffer` 一共有六个构造入口，但按「是否持有所有权」可以分成两大类：

| 类别 | 构造函数 | 是否回调析构器 | 典型用途 |
| --- | --- | --- | --- |
| **持有所有权** | `buffer(p, s, d)`、`buffer(p, s, d, mem_to_free)` | 是（`d` 非空时） | 持有 `malloc` 内存、持有大消息共享内存块 |
| **非拥有视图** | `buffer()`、`buffer(p, s)`、`buffer(byte_t(&)[N])`、`buffer(char&)` | 否（`d` 为空） | 包装栈数组、临时把已有内存塞进去发送 |

记忆口诀：**只有传了 `destructor_t d` 的构造才「拥有」这块内存**；其余都只是「借看一眼」，数据生命周期由外部管理。

最需要专门理解的是带 `mem_to_free` 的四参数构造：

```cpp
buffer(void* p, std::size_t s, destructor_t d, void* mem_to_free);
```

它解决一个现实问题：**`buffer` 暴露给用户的数据指针 `p`，和「真正需要被释放的指针」可能不是同一个**。比如大消息场景里，`p` 指向共享内存中某条消息的数据起点，但析构时要释放 / 回收的是一块「附带引用计数元数据的控制块」。这时把控制块指针传给 `mem_to_free`，析构器收到的就是控制块而不是 `p`，而用户访问 `data()` 拿到的依然是干净的 `p`。4.2.3 会用真实代码印证这一点。

#### 4.2.2 核心流程

构造时的分派关系（都汇聚到内部 `buffer_` 的四字段）：

```text
buffer()                          ──委托──> buffer_(nullptr,0,nullptr,nullptr)
buffer(p,s)                       ──委托──> buffer_(p,s,nullptr,nullptr)   // d=nullptr
buffer(p,s,d)                     ──make──> buffer_(p,s,d,    nullptr)     // a=nullptr
buffer(p,s,d,mem_to_free)         ──make──> buffer_(p,s,d,    mem_to_free) // a=mem_to_free
buffer(byte_t(&data)[N])          ──委托──> buffer(data, sizeof(data))     // 非拥有
buffer(char& c)                   ──委托──> buffer(&c, 1)                  // 非拥有
```

析构时的回调规则（`~buffer_()`）：

```text
d_ == nullptr ?  ──是──> 直接返回，不释放任何东西（非拥有视图）
               ──否──> 回调 d_( a_ ? a_ : p_ , s_ )   // 优先用 a_，否则用 p_
```

也就是说，析构器拿到的「待释放指针」 = `a_`（若有）否则 `p_`，而尺寸始终是 `s_`。

#### 4.2.3 源码精读

先看公共构造重载：

[include/libipc/buffer.h:17-29](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L17-L29) —— 六个构造入口；第 20–22 行的注释明确解释了 `mem_to_free` 的用途：「当 `p` 指向某个更大的、需要整体释放的分配块内部时，用 `mem_to_free` 指定真正要传给析构器的指针」。

再看构造如何委托到内部类，以及析构如何触发回调：

[src/libipc/buffer.cpp:33-51](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L33-L51) —— 全部构造函数的实现。注意两条委托链：`buffer()` 委托到全空的四参数构造；`buffer(p,s)` 委托到 `buffer(p,s,nullptr)`（无析构器 → 非拥有）。三参数构造把 `a_` 设为 `nullptr`，只有四参数构造才会填入 `a_ = mem_to_free`。

那么 `p_->make(...)` 和 `~buffer()` 里的 `p_->clear()` 到底做了什么？这要回到 PIMPL 辅助工具：

[src/libipc/utility/pimpl.h:14-62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/pimpl.h#L14-L62) —— `pimpl` 根据 `sizeof(T)` 与 `sizeof(T*)` 的比较，在「内嵌（小对象优化）」与「堆分配」之间二选一。`buffer_` 含四个指针字段（在 64 位下约 32 字节），远大于一个指针（8 字节），因此命中 `IsImplUncomfortable` 分支：`make` 走 `mem::$new<buffer_>`（堆分配），`clear` 走 `mem::$delete`（释放）。

> `mem::$new / mem::$delete` 是 libipc 自带的类型擦除分配器，属于 U7 的内容，本讲只需知道「它等价于在堆上构造并最终销毁 `buffer_`」。

于是销毁链路是：

```text
~buffer()                          // buffer.cpp:58-60
  └─ p_->clear()                   // pimpl.h:59-61
       └─ clear_impl(...)          // pimpl.h:43-45（uncomfortable 分支）
            └─ mem::$delete(p)     // 销毁并释放 buffer_
                 └─ ~buffer_()     // buffer.cpp:27-30
                      └─ d_(a_?a_:p_, s_)   // ← 析构器在这里被回调，且仅一次
```

最精彩的实战例子在 `recv` 处理大消息时——它正好用到了四参数构造，把「回收共享内存块」的逻辑整个塞进析构器：

[src/libipc/ipc.cpp:670-695](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L670-L695) —— 大消息 `recv` 的核心。逐段看：

- 先用 `mem::$new<recycle_t>(...)` 在堆上建一个控制块 `r_info`，里面记录 `storage_id`、连接信息、当前连接数等「回收所需的全部上下文」。
- 关键一行（第 685 行）：

  ```cpp
  return ipc::buff_t{buf, msg_size, [](void* p_info, std::size_t size) {
      auto r_info = static_cast<recycle_t *>(p_info);
      auto finally = ipc::guard([r_info] { ipc::mem::$delete(r_info); });
      recycle_storage<flag_t>(r_info->storage_id, r_info->inf, size,
                              r_info->curr_conns, r_info->conn_id);
  }, r_info};
  ```

  这里 `buf` 是用户要读的数据指针（`p`），`r_info` 是传给第四个参数的 `mem_to_free`（即 `a_`）。于是用户 `data()` 拿到的是干净的 `buf`；而当用户的 `buff_t` 析构时，析构器收到的 `p_info` 正是 `r_info`，据此完成「引用计数减一、最后一个读取者回收共享内存块」。

- 第 683 行的退化分支：若 `mem::$new<recycle_t>` 失败，则 `return ipc::buff_t{buf, msg_size};`（两参数、无析构器）——此时**不回收**，注释也写明 `// no recycle`。

这就是「零拷贝」的代价与精妙所在：数据从不被拷出共享内存，回收动作被延迟到用户用完 `buff_t` 并析构它的那一刻，靠 `destructor_t` 精准触发。

#### 4.2.4 代码实践（可运行）

**实践目标**：亲手用 `buffer(void* p, std::size_t s, destructor_t d)` 持有一块 `malloc` 内存，验证析构器在 `buffer` 析构时**恰好被回调一次**。

**操作步骤**：

1. 确保已按 u1-l2 构建 libipc（得到 `ipc` 库目标与 `include/` 头文件目录）。
2. 新建一个 `buf_own.cpp`，内容如下（**示例代码**，非项目原有文件）：

   ```cpp
   // 示例代码：验证 buffer 析构器只回调一次
   #include "libipc/buffer.h"
   #include <cstdlib>
   #include <cstring>
   #include <cstdio>

   static int g_free_count = 0;   // 统计析构器被调用的次数

   // 析构器：签名必须严格匹配 void(*)(void*, std::size_t)
   static void my_free(void* p, std::size_t /*s*/) {
       std::printf("destructor fired, p=%p\n", p);
       std::free(p);
       ++g_free_count;
   }

   int main() {
       {
           // 1) 申请一块自己的内存
           char* raw = static_cast<char*>(std::malloc(16));
           std::memcpy(raw, "hello", 6);

           // 2) 用「带析构器」的三参数构造，把所有权交给 buff_t
           ipc::buff_t b{raw, 16, my_free};

           std::printf("size=%zu, empty=%d, data=%s\n",
                       b.size(), b.empty(), static_cast<char*>(b.data()));
           // 3) 离开作用域，b 析构 -> ~buffer_() -> my_free(raw, 16)
       }
       std::printf("after scope, g_free_count=%d\n", g_free_count);
       return g_free_count == 1 ? 0 : 1;
   }
   ```

3. 编译链接（路径按你本机的构建结果替换）：

   ```bash
   g++ -std=c++17 buf_own.cpp -Iinclude -Llib -lipc -pthread -o buf_own
   ```

4. 运行 `./buf_own`。

**需要观察的现象**：

- 进入作用域时打印 `size=16, empty=0, data=hello`。
- 离开作用域时打印一行 `destructor fired, p=0x...`（地址即 `raw`）。
- 末尾 `after scope, g_free_count=1`。

**预期结果**：`g_free_count == 1`，程序返回 0。这说明析构器**只被回调一次**，内存被释放一次，没有泄漏也没有双重释放。

> 如果暂时无法在本机编译，可先做「源码阅读型」验证：对照 `buffer.cpp:27-30` 的 `~buffer_()`，手动推理 `b` 析构时 `d_` 非空、`a_` 为空，因此会回调 `d_(p_, s_)` 一次。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把上面实践里的构造改成 `ipc::buff_t b{raw, 16};`（两参数），运行结果会怎样？为什么？
**参考答案**：`g_free_count` 会是 0，`raw` 会泄漏。因为两参数构造委托到 `buffer(p,s,nullptr)`，`d_` 为空，`~buffer_()` 在第 28 行直接 `return`，不会调用 `my_free`。两参数构造是「非拥有视图」，不会替你释放内存。

**练习 2**：在 ipc.cpp 的大消息分支里，如果把第四个参数 `r_info` 去掉（即改用三参数构造 `buff_t{buf, msg_size, lambda}`），会发生什么？
**参考答案**：析构器收到的指针将变成 `p_`（即 `buf`，数据指针）而非 `r_info`，lambda 里 `static_cast<recycle_t*>(p_info)` 会把数据指针强转成控制块，属于未定义行为，回收逻辑彻底失效。这正是 `mem_to_free` 存在的意义——把「用户看到的数据指针」与「析构器需要的控制块指针」解耦。

**练习 3**：`buffer::destructor_t` 为什么是函数指针 `void(*)(void*, size_t)`，而不是 `std::function`？
**参考答案**：函数指针体积固定（一个字）、零分配、可安全地按值存进 `buffer_`，跨进程语义清晰。而大消息分支实际传的是一个无捕获 lambda——无捕获 lambda 可隐式转换为函数指针，正好匹配该签名，既保持了类型简单，又获得了就地编写回收逻辑的便利。

---

### 4.3 数据访问与转换接口

#### 4.3.1 概念说明

拿到 `buff_t` 后，绝大多数操作都是**只读访问**。`buffer` 提供了一组轻量接口：

| 接口 | 作用 | 备注 |
| --- | --- | --- |
| `empty()` | 是否为空（指针为空或尺寸为 0） | `recv` 超时 / 对端断开时返回空 buffer，**第一件事必判空** |
| `data()` / `data() const` | 返回数据指针 | 非 const 版返回 `void*`，const 版返回 `void const*` |
| `size()` | 返回字节数 | |
| `get<T>()` | 把 `data()` 直接 `reinterpret` 成 `T` | 主要用于 `get<char const*>()` 这类指针 reinterpret |
| `to_tuple()` | 返回 `(data, size)` 二元组 | 方便 `auto [p, n] = buf.to_tuple();` |
| `to_vector()` | 拷贝出一份 `std::vector<byte_t>` | **会拷贝**，离开共享内存后仍安全 |
| `operator== / !=` | 按尺寸 + `memcmp` 比较内容 | |

注意 `to_vector()` 会**拷贝**数据——它把共享内存里的字节复制进一个属于当前进程的 `vector`。这对大消息尤其有用：拷贝完即可立刻让原 `buff_t` 析构、尽早回收共享内存块。

#### 4.3.2 核心流程

只读访问路径很直接：

```text
buff_t b = chan.recv();
if (b.empty()) { /* 超时或对端断开，退出 */ }
void const* p = b.data();          // 或 b.get<char const*>()
std::size_t n = b.size();
// 需要一份独立副本时：
std::vector<ipc::byte_t> copy = b.to_vector();
// 需要比较两条消息时：
if (b1 == b2) { ... }              // size 相等且 memcmp 为 0
```

#### 4.3.3 源码精读

访问与转换接口几乎都在头文件里内联实现：

[include/libipc/buffer.h:37-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L37-L60) —— `empty / data / size` 的声明，以及 `get<T>`、`to_tuple`、`to_vector` 的内联实现。注意 `to_vector` 的写法：

```cpp
std::vector<byte_t> to_vector() const {
    return { get<byte_t const *>(), get<byte_t const *>() + size() };
}
```

它用「起始指针、结束指针」构造 `vector`，等价于一次逐字节拷贝。

`empty / data / size` 的真正实现（委托到内部 `buffer_` 字段）在：

[src/libipc/buffer.cpp:71-85](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L71-L85) —— 注意 `empty()` 的判定是「`p_` 为空 **或** `s_` 为 0」，二者满足其一即视为空。

内容比较的语义：

[src/libipc/buffer.cpp:8-14](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L8-L14) —— `operator==` 先比 `size()`，再用 `std::memcmp` 比内容；`operator!=` 取反。

真实用法示例（chat demo 把收到的消息转成 `std::string`）：

[demo/chat/main.cpp:33-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L33-L35) —— 先 `empty()` 判空作为退出条件，再用 `get<char const*>()` 配合 `size()-1`（去掉 u1-l4 提到的多发一个 `\0`）构造字符串：

```cpp
ipc::buff_t buf = receiver__.recv();
if (buf.empty()) break;                                   // quit
std::string dat { buf.get<char const *>(), buf.size() - 1 };
```

而发送端的「非拥有」用法：

[demo/msg_que/main.cpp:72-72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/msg_que/main.cpp#L72-L72) —— `que__.send(ipc::buff_t(buff__, sz));` 用两参数构造，只是临时把已有缓冲区包起来发送，不持有所有权。对应的 `send(buff_t const&)` 重载见 [include/libipc/ipc.h:179-181](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L179-L181)，它只读取 `data()/size()`，并不接管所有权。

#### 4.3.4 代码实践（源码阅读 + 小改）

**实践目标**：把一条 `recv` 到的 `buff_t` 同时转成 `std::string` 和 `std::vector<byte_t>`，并理解两者是否拷贝。

**操作步骤**：

1. 阅读 [demo/chat/main.cpp:33-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L33-L35) 与 [demo/send_recv/main.cpp:32-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L32-L38)（`recv.empty()` 轮询 + `recv.size()`）。
2. 在任意一个 receiver 示例里，于 `buf.empty()` 判空之后加一行（**示例代码**）：

   ```cpp
   auto v = buf.to_vector();                       // 拷贝出独立副本
   std::string s { buf.get<char const*>(), buf.size() };
   ```

3. 思考：此时 `v` / `s` 与 `buf.data()` 指向的内存是什么关系？

**需要观察的现象 / 预期结果**：`v` 和 `s` 都各自持有一份拷贝，与 `buf` 解耦；即使随后让 `buf` 立刻析构（触发大消息回收），`v` / `s` 依然有效。这就是 `to_vector()` 存在的价值——把数据「搬出」共享内存。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`empty()` 为什么把「尺寸为 0 但指针非空」也算空？
**参考答案**：见 `buffer.cpp:72`，判定是 `p_ == nullptr || s_ == 0`。一条 0 字节的消息没有任何可读内容，对调用方而言与「没有消息」无异，因此统一判为空。这也让 `recv` 在「超时返回空 buffer」与「对端发了一条空消息」两种情况下都能用同一个 `empty()` 退出条件处理。

**练习 2**：`to_tuple()` 与 `to_vector()` 的本质区别是什么？
**参考答案**：`to_tuple()` 返回的是 `(data指针, size)`，**不拷贝**数据，元组里的指针仍指向 buffer 持有的内存，buffer 析构后即失效；`to_vector()` 会**拷贝**出一份独立的 `vector<byte_t>`，与 buffer 生命周期无关。

---

### 4.4 move / swap / 赋值：move-only 语义

#### 4.4.1 概念说明

`buffer` 持有可能需要释放的资源（堆内存或共享内存块），因此它被设计成**只能 move、不能 copy** 的类型（move-only）。

- **没有拷贝构造**：因为声明了 move 构造，拷贝构造被隐式删除。
- **move 构造**：把源对象的内容「偷」过来，源对象随后变成空 buffer。
- **swap**：交换两个 buffer 的内部指针，O(1)，不涉及数据拷贝。
- **赋值 `operator=`**：参数按值传递（`buffer rhs`），内部用 swap 实现——这是经典的 copy-and-swap 手法（此处实为 move-and-swap）。

之所以能避免「双重释放」，关键在于 move 之后源对象的 `d_` 变成 `nullptr`：源对象析构时 `~buffer_()` 在第 28 行直接 `return`，不再回调析构器。

#### 4.4.2 核心流程

```text
move 构造:  buffer b2{std::move(b1)};
            └─ b2 先默认构造（空），再 swap(b1)
            └─ 结果: b2 持有原数据; b1 变成空 buffer（d_=nullptr）

赋值:       b2 = std::move(b1);     // 或 b2 = make_buff();
            └─ 参数 rhs 由实参 move-construct 而来
            └─ swap(*this, rhs)     // this 拿到 rhs 的内容
            └─ 函数返回时 rhs 析构 → 释放 this 原来持有的旧资源

swap:       b1.swap(b2);
            └─ 仅交换两者的 p_ 指针
```

赋值采用「按值传参」的妙处：旧资源的释放被自然地推迟到 `rhs`（这个局部副本）析构的时刻，异常安全且无需额外重载。

#### 4.4.3 源码精读

move / swap / 赋值 / 析构的声明：

[include/libipc/buffer.h:31-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L31-L35) —— 注意只有 `buffer(buffer&& rhs)`，没有 `buffer(buffer const&)`；`operator=` 的参数是**按值**的 `buffer rhs`。

它们的实现：

[src/libipc/buffer.cpp:53-69](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L53-L69) —— 逐行看：

```cpp
buffer::buffer(buffer&& rhs) : buffer() { swap(rhs); }   // 先空，再偷

void buffer::swap(buffer& rhs) { std::swap(p_, rhs.p_); } // 只换指针

buffer& buffer::operator=(buffer rhs) { swap(rhs); return *this; } // move-and-swap
```

`~buffer()` 的实现见 [src/libipc/buffer.cpp:58-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/buffer.cpp#L58-L60)，它调用 `p_->clear()`，最终触发 4.2.3 描述的销毁链路与析构器回调。

把 move + 析构连起来看：move 之后 `b1` 的 `p_` 指向一个「全空」的 `buffer_`（`d_=nullptr`），所以 `b1` 后续析构时不会回调析构器；真正持有数据的 `b2` 析构时才回调一次。**整个生命周期内析构器只触发一次**，既不泄漏也不重复释放。

#### 4.4.4 代码实践（源码阅读 + 小改）

**实践目标**：验证 move 之后源对象变空、析构器只触发一次。

**操作步骤**：在 4.2.4 的程序基础上扩展（**示例代码**）：

```cpp
int hits = 0;
auto dtor = [](void* p, std::size_t){ std::free(p); ++hits; };
// 注意：带捕获的 lambda 不能转函数指针；实践时请用 4.2.4 的全局 my_free，
// 并通过它内部的自增计数器观察 hits。
char* raw = static_cast<char*>(std::malloc(8));
{
    ipc::buff_t a{raw, 8, my_free};
    ipc::buff_t b{std::move(a)};   // move
    std::printf("a.empty=%d after move\n", a.empty());   // 预期 1（空）
    std::printf("b.size=%zu\n", b.size());               // 预期 8
}   // a、b 在此析构；只有 b 持有数据，故析构器只触发 1 次
std::printf("hits=%d\n", hits);     // 预期 1
```

> 由于 `destructor_t` 是函数指针，上面的带捕获 lambda 仅为示意；请沿用 4.2.4 的全局 `my_free`（它已自增 `g_free_count`）来统计命中次数。

**需要观察的现象 / 预期结果**：move 之后 `a.empty()` 为真、`b.size()==8`；作用域结束时析构器计数仍为 1。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `buffer` 不提供拷贝构造？
**参考答案**：`buffer` 可能持有「需要按特定方式释放」的资源（如大消息共享内存块，回收要走引用计数）。盲目拷贝会导致两份 `buffer` 都认为自己「负责释放」，从而双重回收 / 双重释放。因此它被设计为 move-only，所有权在任意时刻唯一。

**练习 2**：`operator=(buffer rhs)` 为什么按值传参，而不是按 `buffer&&` 传引用？
**参考答案**：按值传参时，`rhs` 本身就是「由实参 move 而来的临时副本」，函数内 `swap(*this, rhs)` 把目标内容换给 `this`，旧内容换给 `rhs`；函数返回时 `rhs` 析构，自然释放旧资源。这样只需写一个赋值运算符就同时覆盖「右值赋值」场景，且异常安全（构造 `rhs` 在函数外完成，swap 不抛异常）。

**练习 3**：`buffer` 的 `swap` 为什么是 O(1)？
**参考答案**：因为 PIMPL——两个 `buffer` 各自只有一个 `buffer_* p_` 成员，`swap` 只交换这一个指针，与消息大小无关。

## 5. 综合实践

把本讲四个模块串起来，完成一个「自定义所有权 buffer 的完整生命周期」小任务（**示例代码**）：

1. **构造（4.2）**：`std::malloc(32)` 一块内存，写入 `"libipc-buffer"`，用三参数构造包成 `buff_t owner{raw, 32, my_free}`（`my_free` 内部 `std::free` 并自增计数）。
2. **访问（4.3）**：打印 `owner.size()`、`owner.empty()`，用 `owner.get<char const*>()` 打印内容；再 `auto vec = owner.to_vector();` 拷贝一份。
3. **move（4.4）**：`ipc::buff_t other{std::move(owner)};`，确认 `owner.empty()` 变为真、`other` 持有数据。
4. **析构（4.2）**：让 `other` 离开作用域，确认 `my_free` 恰好被调用一次；并确认步骤 2 拷出的 `vec` 在 `other` 析构后**依然可读**（因为 `to_vector` 是独立拷贝）。

预期：整个过程析构器计数最终为 1，`vec` 在最后仍能打印出 `libipc-buffer`。这个任务同时验证了「所有权绑定 → 只读访问 → move 转移 → 单次回收 → 拷贝独立」五件事，正是 `buffer` 设计的全部要点。

## 6. 本讲小结

- `buff_t` 只是 `buffer` 的别名，是 libipc 唯一的统一消息容器，由「数据指针 + 尺寸 + 可选析构器」三要素构成，对外用 PIMPL 隐藏内部字段。
- **是否持有所有权由构造函数决定**：只有 `buffer(p,s,d)` 与 `buffer(p,s,d,mem_to_free)` 这两个带析构器的构造才「拥有」内存；其余（含数组/字符/两参数）都是非拥有视图。
- 四参数构造里的 `mem_to_free` 把「用户看到的数据指针」与「析构器要释放的指针」解耦，这是大消息零拷贝回收能落地的基础——回收逻辑被整个塞进 `destructor_t`，在用户 `buff_t` 析构时精准触发。
- 销毁链路为 `~buffer() → p_->clear() → mem::$delete → ~buffer_() → d_(a_?a_:p_, s_)`，析构器至多回调一次。
- 数据访问以只读为主：`empty()` 必须先判空，`to_vector()` 会拷贝出独立副本，`operator==` 按 `size + memcmp` 比较。
- `buffer` 是 move-only 类型，move/swap/赋值都建立在「交换一个 `p_` 指针」之上，move 后源对象变空（`d_==nullptr`），因此不会双重释放。

## 7. 下一步学习建议

- 想看 `buff_t` 在收发主链路里如何被产生和消费，进入 **U3**：先读 u3-l1（`ipc.cpp` 全景与 send/recv 链路），再看 u3-l3（大消息外部存储，本讲 4.2.3 提到的 `recycle_storage` 在那里详细展开）。
- 想理解 `mem::$new / mem::$delete` 这个「类型擦除分配器」的内部机制，进入 **U7**（u7-l1 起）。
- 想了解 `buffer` 析构时回调的 `recycle_storage` 如何做跨接收者引用计数回收，直接读 [src/libipc/ipc.cpp:340-345](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L340-L345) 起的 `recycle_storage` 实现，它是 u3-l3 的核心。
- 下一讲 **u2-l3** 将转向 `chan_wrapper / chan_impl` 的句柄生命周期，把「消息容器」放回「通道」语境中，讲解 connect/reconnect/disconnect 与资源清理。
