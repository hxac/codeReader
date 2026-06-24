# 项目概览与定位：什么是 cpp-ipc (libipc)

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标不是写代码，而是建立「全局观」。读完本讲，你应该能够：

- 用一句话说清 cpp-ipc（也叫 libipc）是什么、解决什么问题。
- 说出它相比管道、套接字等传统 IPC 的核心卖点（跨平台、无锁/轻量自旋、无第三方依赖）。
- 区分 `ipc::route`（单写多读）与 `ipc::channel`（多写多读），并知道它们的本质是同一套模板的不同配置。
- 说清楚「广播模式下最多 32 个 receiver」这个限制的**真实来源**——它来自一个 32 位连接位图。
- 知道项目用什么编译、怎么构建（CMake 选项），以及在哪里查性能数据。

本讲不要求你懂无锁编程，所有概念都会从零讲起。

## 2. 前置知识

在进入项目之前，先用最朴素的方式理解三个概念。

**进程（process）与进程隔离。** 现代操作系统给每个进程一片「互相看不见」的独立内存空间。进程 A 不能直接读写进程 B 的变量，这是出于安全和稳定。但有时候进程之间又必须交换数据，于是就产生了「进程间通信（Inter-Process Communication，简称 IPC）」的需求。

**常见的 IPC 方式。** 你可能听过这些：

| 方式 | 一句话描述 |
| --- | --- |
| 管道（pipe） | 一个进程写、一个进程读的字节流，像一根水管。 |
| 套接字（socket） | 跨网络或本机的双向通信，通用但开销较大。 |
| 共享内存（shared memory） | 让两个进程把**同一块物理内存**映射进各自的地址空间，直接读写同一块地址。 |

共享内存是这几种里**理论上最快**的：数据不用在内核和用户态之间来回拷贝，写进去对方就能直接看到。libipc 选用的就是共享内存这条路线。

**「无锁（lock-free）」与「自旋（spin）」。** 多个进程/线程同时读写同一块内存时，必须保证不互相踩踏。传统做法是加锁：谁拿到锁谁才能写，其他人睡觉等待。但「睡觉→被唤醒」需要操作系统介入，开销不小。无锁编程则用 CPU 的原子指令（如比较并交换 CAS）来协调，避免了睡觉；自旋则是「稍微等一下再试」。libipc 的设计哲学是：**先短时间自旋重试，重试够了才用信号量真正阻塞**，兼顾低延迟和不烧 CPU。

**循环数组（circular array）。** 想象一个固定大小的数组，配两个下标：一个「写位置」、一个「读位置」。写到末尾就绕回头部继续写，读也一样，像一条首尾相连的跑道。它天然适合做「生产者写入 / 消费者读取」的环形缓冲区，也是 libipc 底层的数据结构。本讲先建立直觉，具体算法留到第 4 单元细讲。

> 如果你已经熟悉以上概念，可以直接跳到第 3 节。

## 3. 本讲源码地图

本讲涉及的文件不多，重点是「读懂项目自述和构建脚本」，再补两个关键头文件来佐证结论。

| 文件 | 作用 | 本讲怎么用它 |
| --- | --- | --- |
| `README.md` | 项目的「自我介绍」，英文+中文双语，列出全部核心特性 | 提炼定位、卖点、性能环境、参考资料 |
| `CMakeLists.txt` | 顶层构建脚本，定义项目版本、C++ 标准、编译选项 | 说明怎么构建、有哪些开关 |
| `include/libipc/ipc.h` | 公共 API 头文件，定义 `route`/`channel`/`chan_wrapper` | 证明 route/channel 是同一模板的不同实例 |
| `include/libipc/def.h` | 核心类型与策略标签（`relat`/`trans`/`wr<>`） | 解释 route/channel 背后的「关系」与「传输」枚举 |
| `src/libipc/circ/elem_def.h` | 循环数组的连接头部，定义连接位图 | 揭示「32 个 receiver」限制的真正来源 |

记忆口诀：**README 看定位，CMake 看构建，ipc.h/def.h 看 API，elem_def.h 看限制来源。**

## 4. 核心概念与源码讲解

### 4.1 项目定位与核心卖点

#### 4.1.1 概念说明

cpp-ipc（库名 libipc）是一个**基于共享内存的高性能进程间通信库**，支持 Linux / Windows / FreeBSD 三个平台（x86/x64/ARM）。它的目标读者是需要「同一台机器上多个进程之间极速交换消息」的开发者——例如一个后台服务进程和一个 GUI 进程之间高频地传数据。

它的核心卖点可以归纳为一句话：**用共享内存拿到极限吞吐，同时用无锁算法和轻量自旋把延迟压到最低，并且除了 C++ 标准库之外不依赖任何第三方库。**

#### 4.1.2 核心流程

从「使用者视角」看，libipc 的典型生命周期是：

1. **创建/打开通道**：用一个字符串名字（如 `"my-channel"`）创建或打开一条通道。不同进程用**相同的名字**就能连到同一条通道，背后是一块同名的共享内存。
2. **收发消息**：发送方调用 `send(...)`，接收方调用 `recv(...)`。默认是**广播**——一个发送，所有接收方都能收到。
3. **等待与超时**：没有消息时，`recv` 不会无限傻等，可以先自旋重试、超过一定次数再用信号量阻塞，并支持超时返回。
4. **断开与清理**：通道对象析构时自动断开并回收资源（RAII）。

本讲只看「项目自述」这一层，具体的 API 调用留到 [u1-l4](u1-l4-first-ipc-program.md)。

#### 4.1.3 源码精读

README 的标题行直接给出了定位：

[README.md:9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L9)
> `A high-performance inter-process communication library using shared memory on Linux/Windows/FreeBSD.`

紧接着是一个特性清单，这是整本手册的「导航地图」，每一项都对应后面某个单元：

[README.md:11-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L11-L19)

提炼成表格，并标注它们将在哪一讲被深入：

| README 特性行 | 含义 | 后续讲义 |
| --- | --- | --- |
| C++17 支持，除 STL 外无依赖 | 编译门槛低、易集成 | 本讲 / [u1-l2](u1-l2-build-and-run.md) |
| 仅使用无锁或轻量自旋 | 低延迟、不长时间忙等 | 第 4、6、8 单元 |
| 底层数据结构为循环数组 | 环形缓冲区做消息通路 | 第 4 单元 |
| `route` 单写多读 / `channel` 多写多读 | 两种并发模型 | [4.2](#42-route-与-channel两种并发模型) |
| 同一通道最多 32 个 receiver | 连接位图限制 | [4.3](#43-无锁循环数组与-32-接收者限制) |
| 默认广播，可自选读写组合 | 策略可配 | 第 2、4 单元 |
| 不会长时间盲等，超时后用信号量 | 渐进退避等待 | [u3-l4](u3-l4-wait-model.md) |

再看构建脚本。顶层 `CMakeLists.txt` 第 2 行声明了项目名和版本，第 11 行锁定了 C++17 标准：

[CMakeLists.txt:1-2](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L1-L2) — `project(cpp-ipc VERSION 1.4.1)`，本讲对应版本是 1.4.1。

[CMakeLists.txt:10-11](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L10-L11) — `set(CMAKE_CXX_STANDARD 17)`，即需要 C++17 编译器。

构建开关在第 4–8 行，默认全是 `OFF`（只编译库本身，不编译测试、示例和动态库）：

[CMakeLists.txt:4-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L4-L8)

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `LIBIPC_BUILD_TESTS` | OFF | 编译单元测试（会拉取 gtest） |
| `LIBIPC_BUILD_DEMOS` | OFF | 编译各个示例程序（chat / send_recv 等） |
| `LIBIPC_BUILD_SHARED_LIBS` | OFF | 编译动态库（DLL/so），否则是静态库 |
| `LIBIPC_USE_STATIC_CRT` | OFF | Windows 下用 `/MT` 静态 CRT |
| `LIBIPC_CODECOV` | OFF | 开启覆盖率统计 |

> 小提示：第一次想跑示例，至少要把 `LIBIPC_BUILD_DEMOS` 设为 `ON`，详见 [u1-l2](u1-l2-build-and-run.md)。

#### 4.1.4 代码实践

**实践类型：源码阅读 + 表达输出**（本讲是概览，先不写代码）。

1. **目标**：把「项目定位」内化为自己的话，而不是背 README。
2. **步骤**：
   - 打开 [README.md:9-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L9-L19)。
   - 打开 [CMakeLists.txt:4-8](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/CMakeLists.txt#L4-L8)。
3. **要你做的事**：用自己的话写出 libipc 相比**管道 / 套接字** IPC 的**三个优势**（提示：从「是否拷贝数据」「是否跨平台」「依赖多少」三个角度想）。
4. **预期结果**：你应该能写出类似——① 共享内存零拷贝，吞吐远高于管道/套接字；② 一套代码跨 Linux/Windows/FreeBSD；③ 除 STL 外零第三方依赖。
5. 待本地验证：无（纯阅读理解）。

#### 4.1.5 小练习与答案

**练习 1**：libipc 默认编译出的是静态库还是动态库？怎么切换？
> **答**：默认静态库（`LIBIPC_BUILD_SHARED_LIBS` 为 `OFF`）。想得到动态库，配置 CMake 时加 `-DLIBIPC_BUILD_SHARED_LIBS=ON`。

**练习 2**：README 说「推荐 C++17 编译器」。请到 `CMakeLists.txt` 里找到对应的设置行。
> **答**：第 11 行 `set(CMAKE_CXX_STANDARD 17)`。

---

### 4.2 route 与 channel：两种并发模型

#### 4.2.1 概念说明

libipc 对外暴露两个最常用的类型：`ipc::route` 和 `ipc::channel`。它们的区别只在「有多少个发送方 / 接收方」：

- `route`：**单写多读**。只有一个生产者（发送方），但有多个消费者（接收方）。适合「一个服务广播状态，多个客户端收」。
- `channel`：**多写多读**。多个发送方都能写，多个接收方都能收。适合「群聊」这种全员都能说话的场景。

关键认知：**两者本质上是同一套模板的不同配置**，并不是两套独立实现。理解了这一点，后面的源码就不会觉得「为什么有两份代码」。

#### 4.2.2 核心流程

配置是通过两个「维度」来表达的：

1. **关系（multiplicity，`relat`）**：某一端是「单个」还是「多个」。
   - 生产者端：`relat::single` 或 `relat::multi`
   - 消费者端：`relat::single` 或 `relat::multi`
2. **传输方式（`trans`）**：`unicast`（单播，只发给一个接收者）还是 `broadcast`（广播，发给所有接收者）。

把这三个维度打包成一个「策略标签」`wr<Rp, Rc, Ts>`，喂给模板 `chan_wrapper`，就得到了一种通道类型。`route` 和 `channel` 只是两个预设好的常用组合：

```
route   = chan< single_producer, multi_consumer, broadcast >
channel = chan< multi_producer,  multi_consumer, broadcast >
```

注意两者都用了 `broadcast`——**libipc 默认就是广播**。也就是说，一条消息发出去，所有连着的接收者都会收到一份。

#### 4.2.3 源码精读

先看核心类型定义。`def.h` 里定义了两个枚举：

[def.h:41-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L41-L49) — `relat { single, multi }` 表达「数量」，`trans { unicast, broadcast }` 表达「传输方式」。

然后把这三个维度打包成一个空结构体「标签」`wr<>`，再用 `relat_trait` 在**编译期**把它解析成三个布尔常量：

[def.h:53-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L53-L64)

```cpp
template <relat Rp, relat Rc, trans Ts>
struct wr {};                       // 空标签，只用于携带类型信息

template <relat Rp, relat Rc, trans Ts>
struct relat_trait<wr<Rp, Rc, Ts>> {
  constexpr static bool is_multi_producer = (Rp == relat::multi);
  constexpr static bool is_multi_consumer = (Rc == relat::multi);
  constexpr static bool is_broadcast      = (Ts == trans::broadcast);
};
```

这种「用空模板当标签、再用 trait 在编译期提取布尔值」的手法，是 C++ 模板元编程的常见套路。它让 `route` 和 `channel` 在不产生任何运行时开销的前提下，走不同的代码分支。

最终在 `ipc.h` 里，`chan` 是一个把 `wr<>` 喂给 `chan_wrapper` 的别名，而 `route` / `channel` 只是两个预设：

[ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228)

```cpp
template <relat Rp, relat Rc, trans Ts>
using chan = chan_wrapper<ipc::wr<Rp, Rc, Ts>>;

// route：1 个生产者 → N 个消费者，广播
using route = chan<relat::single, relat::multi, trans::broadcast>;

// channel：N 个生产者 → N 个消费者，广播
using channel = chan<relat::multi, relat::multi, trans::broadcast>;
```

对比这两行，**唯一的差别就是第一个模板参数**（生产者数量 `single` vs `multi`）。`channel` 比 `route` 多了「多生产者」能力，因此底层要多处理「多个发送方并发写」的竞争，这部分逻辑会在第 4 单元（prod_cons）里展开。

顺带认识两个最基础的 API 名词，后面会反复用到。`ipc.h` 把句柄定义成不透明的 `void*`，并用一个枚举标注角色：

[ipc.h:12-18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L12-L18) — `handle_t = void*`、`buff_t = buffer`，以及 `enum { sender, receiver }` 两种角色。发送时默认带 100ms 超时（`default_timeout`，见 [def.h:30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L30)），接收默认无限等待直到有消息。

#### 4.2.4 代码实践

**实践类型：源码阅读 + 类型推导**。

1. **目标**：亲手「拆解」route 和 channel 的类型，确认它们是同一模板的不同配置。
2. **步骤**：
   - 打开 [ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228)。
   - 打开 [def.h:41-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L41-L64)。
3. **要你做的事**：写出 `ipc::route` 展开后的「完整类型」，并填表——`is_multi_producer` / `is_multi_consumer` / `is_broadcast` 各是 `true` 还是 `false`。
4. **预期结果**：`route` → `chan_wrapper<wr<single, multi, broadcast>>`，三个布尔分别为 `false / true / true`。
5. 待本地验证：无。

#### 4.2.5 小练习与答案

**练习 1**：如果想要「多生产者、单消费者、单播」，应该怎么写 `chan` 的模板参数？库里有没有直接提供这个别名？
> **答**：写成 `chan<relat::multi, relat::single, trans::unicast>`。库里**没有**提供现成别名，但你可以自己 `using` 一个。库只预设了 `route` 和 `channel` 两个最常用组合。

**练习 2**：`route` 和 `channel` 的源码定义只差一个字符（`single` vs `multi`）。这个差异在运行时会导致什么不同？
> **答**：`channel` 的生产者端是「多个」，所以底层要处理多个发送方并发写入同一队列的竞争（需要提交索引/标志协议）；`route` 只有一个发送方，这部分可以更简单。细节在第 4 单元。

---

### 4.3 无锁、循环数组与 32 接收者限制

#### 4.3.1 概念说明

这一节回答两个问题：

1. **libipc 凭什么快？** 答：无锁算法 + 轻量自旋 + 循环数组。
2. **为什么广播模式最多 32 个 receiver？** 答：因为每个接收者在连接位图里占**一个二进制位**，而位图是一个 **32 位整数**。

第二个问题尤其重要——它是本讲实践任务的核心，也是初学者最容易「知其然而不知其所以然」的点。我们从源码层面把它讲透。

#### 4.3.2 核心流程

广播模式下，libipc 需要追踪「当前有哪些接收者连着」。它用一个非常巧妙的办法：**用一个整数的每一位代表一个接收者**。

- 初始时整数是 `0`，表示没有任何接收者。
- 第 1 个接收者连上来，把**最低位**置 1 → `0b...0001`，它分到的「连接 id」就是第 0 位。
- 第 2 个接收者连上来，把**下一个空闲位**置 1 → `0b...0011`，分到第 1 位。
- 以此类推，每个接收者拿到一个**唯一的位**。

用一个 32 位整数，最多只能表示 32 个不同的位，所以**最多 32 个接收者**。发送方则没有这种限制（发送方只是往队列里写，不需要占位）。

「找到第一个 0 位并置 1」这一步，libipc 用了一个经典的位运算小技巧：`next = curr | (curr + 1)`。下面用数学说明它为什么成立。

#### 4.3.3 源码精读

先看类型定义。`circ/elem_def.h` 把连接计数的类型 `cc_t` 定义为 32 位无符号整数，并直接注释了限制：

[elem_def.h:16-20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L16-L20)

```cpp
using u1_t = ipc::uint_t<8>;
using u2_t = ipc::uint_t<32>;

/** only supports max 32 connections in broadcast mode */
using cc_t = u2_t;   // = uint32_t
```

注意那行注释 `only supports max 32 connections in broadcast mode`——这就是 README 里「最多 32 个 receiver」的**直接来源**。`cc_t`（connection count 的缩写）本质是一个 32 位位图，每一位代表一个连接槽位。

再看广播模式的连接逻辑（`conn_head` 在 `is_broadcast == true` 时的特化）：

[elem_def.h:56-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L56-L71)

```cpp
cc_t connect() noexcept {
    for (unsigned k = 0;; ipc::yield(k)) {
        cc_t curr = this->cc_.load(std::memory_order_acquire);
        cc_t next = curr | (curr + 1); // find the first 0, and set it to 1.
        if (next == curr) {
            // connection-slot is full.
            return 0;
        }
        if (this->cc_.compare_exchange_weak(curr, next, std::memory_order_release)) {
            return next ^ curr; // return connected id
        }
    }
}
```

逐行拆解这段无锁连接代码：

1. **读取当前位图** `curr`（用 `acquire` 序保证看到其它连接的最新状态）。
2. **`next = curr | (curr + 1)`**：把 `curr` 的最低一个 0 位变成 1，其余位不变。下面证明它正确。
3. **判满**：如果 `next == curr`，说明 `curr` 已经全是 1（32 位全满），没有空位了，返回 `0` 表示连接失败。
4. **CAS 抢占**：用 `compare_exchange_weak` 把 `curr` 原子地换成 `next`。如果期间有别人改过位图（`curr` 被更新），CAS 失败，`for` 循环配合 `yield(k)` 退避后重试——这就是「轻量自旋」。
5. **返回连接 id**：`next ^ curr` 正好等于被新置 1 的那一位（即这个接收者分到的位）。

**为什么 `curr | (curr + 1)` 能「找到最低的 0 位并置 1」？** 设 `curr` 形如 `...A0 111...1`（最低的若干位全是 1，再往上有且仅有一个 0）。加 1 会让这一串 1 进位，把那个 0 变成 1、低位全清 0：

\[
\text{curr} = \ldots A\,0\,\underbrace{11\ldots1}_{k\text{ 个}}
\quad\Longrightarrow\quad
\text{curr}+1 = \ldots A\,1\,\underbrace{00\ldots0}_{k\text{ 个}}
\]

再与 `curr` 取或：

\[
\text{curr}\,|\,(\text{curr}+1)
= \ldots A\,1\,1\ldots1
\]

于是「最低的 0 位」被置 1，低位那些原本就是 1 的位也保持 1，更高位完全不变。**只新增了一个 1**，正好对应新接入的接收者。

**断开连接**则是把对应位清零，用 `fetch_and(~cc_id)`：

[elem_def.h:73-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L73-L75) — 把接收者自己的那一位清掉，腾出空位给将来的连接。

> 这里出现的 `acquire`/`release`/`compare_exchange_weak`/`yield(k)` 都是无锁编程的标准工具，本讲只要知道「它们让多个进程安全地抢位」即可，内存序的细节留到 [u8-l1](u8-l1-memory-ordering.md)。

#### 4.3.4 代码实践

**实践类型：手动演算位运算**（本讲的「硬核」实践，也是综合实践任务的核心）。

1. **目标**：亲手验证「位图」机制，从而彻底理解 32 限制的来源。
2. **步骤**：假设当前连接位图 `cc_ = 0b1010`（即第 1 位和第 3 位已被占用，第 0、2 位空闲），用纸笔模拟 `connect()`：
   - 算出 `curr + 1` 和 `next = curr | (curr + 1)`。
   - 算出返回的连接 id `next ^ curr`，判断它落在第几位。
   - 再断开刚才那个连接（`fetch_and(~id)`），看 `cc_` 变回什么。
3. **需要观察的现象**：每次 `connect` 都精确地占用「最低的一个空闲位」，断开后该位释放。
4. **预期结果**：
   - `curr=0b1010` → `curr+1=0b1011` → `next=0b1011` → 连接 id `=0b0001`（第 0 位）。
   - `connect` 后 `cc_=0b1011`；断开第 0 位后 `cc_=0b1010`，恢复原状。
5. 待本地验证：无（纯位运算推导，可在任意支持 C++17 的环境里写个 `main` 打印验证，但非必需）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `cc_t` 从 `uint32_t` 改成 `uint64_t`，receiver 上限会变成多少？这种改动「免费」吗？
> **答**：理论上限变成 64。但**不是免费**的——位图格式变了，涉及 `connect`/`disconnect`、元素里的读计数位域、序列化等一大片代码都要同步调整，属于架构级改动。

**练习 2**：当 32 个位全部是 1 时，`connect()` 返回什么？调用方怎么知道失败了？
> **答**：此时 `curr+1` 溢出为 0，`next = curr | 0 = curr`，触发 `next == curr` 分支，返回 `0` 表示「连接槽已满」。第 33 个接收者会因此连接失败。

**练习 3**：`connect()` 里的 `for` 循环为什么需要 `ipc::yield(k)`？
> **答**：CAS 失败说明有其它进程刚改过位图。直接立刻重试会浪费 CPU；`yield(k)` 是一种渐进退避（先空转、再让出 CPU），降低竞争激烈时的 CPU 占用，呼应 README 的「不会长时间盲等」。

---

### 4.4 性能基准、等待哲学与参考资料

#### 4.4.1 概念说明

libipc 的设计处处体现一个理念：**在「延迟」和「CPU 占用」之间找平衡**。这体现在它的等待哲学上——「先自旋若干次，重试够了再用信号量真正阻塞」。这避免了两种极端：纯忙等会烧 CPU，纯阻塞又会有「睡→醒」的内核开销。

这一节还给出官方的性能测试环境和参考资料，方便你后续做基准对比和深入阅读。

#### 4.4.2 核心流程

等待策略可以概括为一条渐进退避曲线：

```
有消息?  ──是──> 立刻返回
   │否
   ▼
短时间自旋重试 (yield: 空转 → pause → 让出)
   │仍没有
   ▼
用信号量阻塞等待 (可超时)
   │超时
   ▼
返回失败 / 强制发送 (force_push)
```

这条曲线的具体阈值（自旋多少次才转阻塞）在 `rw_lock.h` 的 `yield`/`sleep` 里实现，本讲只建立直觉，细节见 [u6-l1](u6-l1-spin-rw-lock.md) 和 [u3-l4](u3-l4-wait-model.md)。

#### 4.4.3 源码精读

README 用一张表给出了官方基准测试的环境（同一份数据有中英两版）：

[README.md:27-36](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L27-L36)

| 项 | 值 |
| --- | --- |
| 设备 | Lenovo ThinkPad T450 |
| CPU | Intel Core i5-4300U @ 2.5 GHz |
| 内存 | 16 GB |
| 操作系统 | Windows 7 Ultimate x64 |
| 编译器 | MSVC 2017 15.9.4 |

> 注意：这是一台 2014 年的笔记本。今天的机器上数字会好得多，但相对量级（共享内存 vs 套接字的差距）依然成立。具体延迟/吞吐数据在仓库根目录的 `performance.xlsx`，测试代码在 [test](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test) 目录。

README 的第 17 行一句话点明了等待哲学，这是整本手册反复出现的主题：

[README.md:17](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L17)
> `No long time blind wait. (Semaphore will be used after a certain number of retries.)`

翻译过来就是：「不会长时间盲等，重试一定次数后改用信号量等待」。这条原则贯穿了从 `send`/`recv` 的超时，到 `rw_lock` 的退避，再到 `waiter` 的实现。

最后是参考资料清单——如果你想真正吃透无锁编程，这里是作者推荐的路标：

[README.md:38-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L38-L44)，包括 Dr Dobb's 的 Lock-Free Data Structures、CodeProject 的无锁循环队列实现、以及一篇关于「用信号量实现条件变量」的经典论文（这正是 Windows 平台 condition 的理论基础，见 [u6-l3](u6-l3-condition-semaphore.md)）。

#### 4.4.4 代码实践

**实践类型：阅读 + 资料检索**。

1. **目标**：把「等待哲学」和「可验证的性能数据」对上号。
2. **步骤**：
   - 读 [README.md:17](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L17)（等待哲学）。
   - 读 [README.md:27-36](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md#L27-L36)（测试环境）。
3. **要你做的事**：回答——「先自旋后信号量」这套策略，相比「一上来就阻塞」，在「偶尔有消息」和「长时间无消息」两种场景下各有什么好处？
4. **预期结果**：「偶尔有消息」时自旋能避免睡醒开销、拿到极低延迟；「长时间无消息」时转信号量阻塞能避免空转烧 CPU。这正是渐进退避的目的。
5. 待本地验证：无。

#### 4.4.5 小练习与答案

**练习 1**：官方性能数据存在哪个文件？测试代码在哪个目录？
> **答**：数据在仓库根目录的 `performance.xlsx`；测试代码在 `test` 目录（基准和单元测试都在里面）。

**练习 2**：README 参考资料里有一篇「用信号量实现条件变量」的论文。猜一下它对应 libipc 哪个平台的实现？
> **答**：Windows。因为 Win32 的条件变量在较老系统上不完善，libipc 用「信号量 + 计数器」自己实现了 condition，那篇论文正是这个算法的理论基础（详见 [u6-l3](u6-l3-condition-semaphore.md)）。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个**贯穿性任务**（对应本讲的 practice_task）：

> **任务**：假设你要向同事推荐 libipc。请完成两件事——
>
> **(A)** 阅读 [README.md](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/README.md)，用**自己的话**写出 libipc 相比管道/套接字 IPC 的**三个优势**。
>
> **(B)** 说明广播模式下「最多 32 个 receiver」这个限制的**来源**，要求：
> - 指出限制定义在哪个文件、哪一行（提示：[elem_def.h:16-20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L16-L20)）。
> - 解释 `cc_t` 为什么是 32 位、每个 receiver 占几位。
> - 用 [4.3.4](#434-代码实践) 的位运算推导，说明第 33 个 receiver 为什么连不上（`next == curr` 返回 0）。

**参考要点（自检用，不要直接抄）**：

(A) 三个优势示例：① 共享内存零拷贝，吞吐显著高于管道/套接字；② 一套代码跨 Linux/Windows/FreeBSD；③ 除 STL 外零第三方依赖，且默认无锁/轻量自旋，延迟低。

(B) 来源：`cc_t` 在 `src/libipc/circ/elem_def.h` 被定义为 `uint_t<32>`（即 `uint32_t`），注释明确写「only supports max 32 connections in broadcast mode」。广播模式下每个 receiver 在连接位图里占**恰好 1 位**，32 位整数最多容纳 32 个位，故上限为 32。`connect()` 用 `curr | (curr + 1)` 找最低空闲位；当 32 位全为 1 时 `curr+1` 溢出为 0，`next == curr`，函数返回 0 表示槽位已满，第 33 个 receiver 因此连接失败。sender 不占位图位，所以没有这个限制。

完成本任务后，你就真正理解了 libipc「是什么、强在哪、限制在哪」。

## 6. 本讲小结

- **libipc 是什么**：基于共享内存的高性能跨平台（Linux/Windows/FreeBSD）IPC 库，除 STL 外零依赖，要求 C++17。
- **核心卖点**：共享内存零拷贝 + 无锁/轻量自旋 + 循环数组，兼顾高吞吐与低延迟。
- **两种并发模型**：`route`（单写多读）与 `channel`（多写多读），本质是同一模板 `chan<Rp,Rc,Ts>` 的不同配置，且默认都是广播。
- **32 限制的来源**：广播模式的连接位图 `cc_t` 是 32 位整数，每个 receiver 占 1 位，`connect()` 用 `curr|(curr+1)` 抢位，满 32 位后第 33 个连不上。
- **等待哲学**：先自旋重试、够了再用信号量阻塞，不长时间盲等，支持超时。
- **构建入口**：顶层 `CMakeLists.txt` 用 `LIBIPC_BUILD_TESTS/DEMOS/SHARED_LIBS` 等开关控制产出，默认只编译静态库。

## 7. 下一步学习建议

本讲建立了全局观，但还没真正「跑起来」。建议按以下顺序继续：

1. **[u1-l2 构建与首次运行](u1-l2-build-and-run.md)**：动手用 CMake 编译 libipc 并启用 `LIBIPC_BUILD_DEMOS`，跑通第一个跨进程 demo。这是把「纸面认知」变成「能运行」的关键一步。
2. **[u1-l3 目录结构与模块地图](u1-l3-directory-structure.md)**：建立对 `include/`、`src/`、`test/`、`demo/` 分层的全局认知，知道每个模块的代码在哪。
3. **[u1-l4 第一个 IPC 程序](u1-l4-first-ipc-program.md)**：亲手写一个 route/channel 收发程序，体会 `connect/send/recv/disconnect` 的生命周期。

如果你急于了解底层，也可以在学完 u1-l4 后，直接跳到第 3 单元（[u3-l1 send/recv 数据通路](u3-l1-send-recv-data-path.md)）看一条消息从发送到接收的完整调用链。但建议**先跑通 demo 再读底层**，顺序学习体验更顺。
