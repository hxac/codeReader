# 核心类型与策略标签（def.h）

## 1. 本讲目标

本讲带领读者逐行读懂 libipc 的「类型宪法」—— `include/libipc/def.h`。这是一个只有 74 行的头文件，但它定义了整个库赖以运转的基础类型、全局常量、策略标签和前缀机制。

学完本讲，你应当能够：

- 说出 `byte_t`、`uint_t<N>` 等基本类型的用途，并解释 `default_timeout`、`data_length`、`large_msg_*` 等常量的含义。
- 理解 `relat`（生产者/消费者多重性）与 `trans`（传输方式）两个枚举，以及它们如何组合成不同的通道形态。
- 掌握 `wr<Rp, Rc, Ts>` 策略标签和 `relat_trait` 特质（trait）如何把「策略」翻译成「编译期布尔判定」，进而驱动底层算法选择。
- 理解 `prefix` 前缀标签如何为共享内存对象命名，从而支持多个独立 IPC 命名空间共存。

本讲只讲 `def.h` 这一层的「定义」，不展开 `route`/`channel` 的使用方式（那已在 u1-l4 讲过），也不深入底层算法（那是 u3、u4 的内容）。

## 2. 前置知识

阅读本讲前，你应当已经掌握 u1-l4 的内容：

- 库用 `ipc::channel` / `ipc::route` 两种预设类型构造通道，`route` 是单写多读广播，`channel` 是多写多读广播。
- 一个通道由 `name`（同名即同一通道）和 `mode`（`ipc::sender` / `ipc::receiver`）决定角色，构造即连接、析构即销毁。
- `send` 默认超时 `default_timeout`，`recv` 默认 `invalid_value`（无限阻塞）。

本讲要回答的关键问题是：**`route` 和 `channel` 这两个看似不同的类型，本质上是同一个模板的不同预设**。要理解这一点，就需要回到 `def.h`，看清它定义的「策略积木」。

如果你对 C++ 模板的「别名模板（alias template）」「模板特化（template specialization）」「类型萃取（type trait）」这些概念不熟，本讲会结合源码顺手解释，不必提前复习。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | 本讲主角，定义基本类型、常量、`relat`/`trans` 枚举、`wr<>` 标签、`relat_trait`、`prefix`。 |
| [include/libipc/ipc.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h) | 用 `def.h` 的 `wr<>` 拼出 `chan` 别名，并给出 `route`/`channel` 两个预设。 |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 文件末尾的「显式实例化门」决定库真正编译了哪几种 `wr<>` 组合；`prefix` 在这里参与命名。 |
| [src/libipc/prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) | 为每种 `wr<>` 提供对应的无锁队列算法特化，是 `relat`/`trans` 的真正「消费者」。 |
| [src/libipc/circ/elem_array.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h) | 用 `relat_trait` 的布尔判定选择「发送者/接收者检查器」，展示 trait 如何驱动代码分派。 |
| [src/libipc/mem/resource.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h) | 提供 `make_prefix`，把 `prefix` 标签拼接成真实的共享内存对象名。 |

记住一条主线：**`def.h` 负责定义「积木」，下游文件负责「拼装」和「消费」这些积木**。

## 4. 核心概念与源码讲解

### 4.1 基本类型与全局常量

#### 4.1.1 概念说明

任何库都需要一组贯穿全局的基础类型和常量。libipc 把它们集中放在 `def.h` 顶部，目的是让所有平台、所有模块共用同一份「度量衡」：

- **固定宽度的整数类型**：跨平台代码不能依赖 `int`、`long` 的宽度（它们在不同平台不同），而要用 `uint8_t`、`uint32_t` 这类宽度明确的类型，保证「同一份内存布局在 Linux 和 Windows 上完全一致」。这对放在共享内存里的数据结构尤其关键。
- **全局常量**：超时、消息分片长度、大消息阈值等是整个库的行为开关，集中定义避免「魔法数字」散落各处。

#### 4.1.2 核心流程

`def.h` 顶部的定义流程是「自底向上」的：

1. 先定义字节类型 `byte_t`。
2. 再用模板 `uint<N>` + 别名 `uint_t<N>` 生成 N 位无符号整数。
3. 接着用两个匿名 `enum` 把常量按「类型」分组：`uint32_t` 一组（语义值），`size_t` 一组（尺寸值）。

#### 4.1.3 源码精读

字节类型直接别名到标准库的 8 位无符号整数：

[include/libipc/def.h:13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L13) —— 定义 `byte_t`，库中所有「原始字节」都用它表示。

接着是一个「主模板 + 四个特化」的模式，用来按位宽生成整数类型：

```cpp
template <std::size_t N> struct uint;            // 主模板，只有声明
template <> struct uint<8 > { using type = std::uint8_t ; };
template <> struct uint<16> { using type = std::uint16_t; };
template <> struct uint<32> { using type = std::uint32_t; };
template <> struct uint<64> { using type = std::uint64_t; };
template <std::size_t N> using uint_t = typename uint<N>::type;
```

[include/libipc/def.h:15-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L15-L24) —— `uint_t<N>` 让你写 `uint_t<8>`、`uint_t<32>` 得到对应宽度的无符号整数。这种写法的好处是「把位宽当参数」，后续在循环队列、连接位图里会按需取 8 位或 32 位整数（例如连接位图 `cc_t` 用 `uint_t<32>`，正是 32 接收者限制的来源——见 u2-l4）。

> 术语解释：**主模板（primary template）**只有声明、不实用；**显式特化（explicit specialization）**为特定参数（如 `8`、`16`）给出专属实现。`uint_t<N>` 是**别名模板（alias template）**，相当于给冗长的 `typename uint<N>::type` 起个短名。

接下来是两组常量。第一组是「语义值」，用 `uint32_t` 承载：

```cpp
enum : std::uint32_t {
  invalid_value   = (std::numeric_limits<std::uint32_t>::max)(),
  default_timeout = 100, // ms
};
```

[include/libipc/def.h:28-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L28-L31) —— `invalid_value` 是 `uint32_t` 的最大值，被当作「哨兵」表示「无效/无限等待」。回顾 u1-l4：`recv(tm)` 的默认参数就是 `invalid_value`，意为「一直等到有消息」。`default_timeout = 100` 毫秒是 `send`/`try_send` 的默认超时。

> 术语解释：`enum : 底层类型` 是 C++11 的「指定底层类型的强类型枚举」写法，这里用匿名 `enum` + 指定底层类型，相当于定义了一组带固定类型的常量，避免普通 `#define` 或 `int` 常量丢失类型信息。

第二组是「尺寸值」，用 `size_t` 承载：

```cpp
enum : std::size_t {
  central_cache_default_size = 1024 * 1024, ///< 1MB
  data_length     = 64,
  large_msg_limit = data_length,
  large_msg_align = 1024,
  large_msg_cache = 32,
};
```

[include/libipc/def.h:33-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L33-L39) —— 这组常量决定了消息如何存放：

- `data_length = 64`：循环队列里**每个槽位的内联数据长度**（字节）。一条消息会被切成若干个 64 字节分片入队。
- `large_msg_limit = data_length`：超过 64 字节的消息触发「大消息」处理路径。
- `large_msg_align = 1024`：大消息外部存储块按 1KB 对齐。
- `large_msg_cache = 32`：大消息存储池的缓存数量。
- `central_cache_default_size = 1MB`：内存管理子系统的中央缓存大小（u7 会详讲）。

> 这几个 `large_msg_*` 常量为什么重要？因为 libipc 对「小消息」和「大消息」走完全不同的路径：小消息直接分片塞进循环队列；大消息则单独申请一块共享内存存放，队列里只传一个引用 id（详见 u3-l3）。这一切的分界线，就是这里的常量。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `def.h` 里类型和常量的真实值，建立直观印象。

**操作步骤**：

1. 新建一个 `print_def.cpp`，内容如下（示例代码）：

```cpp
#include "libipc/def.h"
#include <cstdio>
#include <cstddef>

int main() {
    std::printf("sizeof(byte_t)        = %zu\n", sizeof(ipc::byte_t));
    std::printf("sizeof(uint_t<8>)     = %zu\n", sizeof(ipc::uint_t<8>));
    std::printf("sizeof(uint_t<32>)    = %zu\n", sizeof(ipc::uint_t<32>));
    std::printf("default_timeout       = %u ms\n", ipc::default_timeout);
    std::printf("invalid_value         = %u\n",   ipc::invalid_value);
    std::printf("data_length           = %zu\n",  ipc::data_length);
    std::printf("large_msg_limit       = %zu\n",  ipc::large_msg_limit);
    std::printf("large_msg_align       = %zu\n",  ipc::large_msg_align);
    return 0;
}
```

2. 用 `g++ -std=c++17 -Iinclude print_def.cpp -o print_def` 编译并运行（`-Iinclude` 让编译器找到 `include/libipc/def.h`）。

**需要观察的现象**：`invalid_value` 应当是 `4294967295`（即 \(2^{32}-1\)）；`data_length` 与 `large_msg_limit` 都等于 64。

**预期结果**：

```
sizeof(byte_t)        = 1
sizeof(uint_t<8>)     = 1
sizeof(uint_t<32>)    = 4
default_timeout       = 100 ms
invalid_value         = 4294967295
data_length           = 64
large_msg_limit       = 64
large_msg_align       = 1024
```

若运行环境与本文不符，以本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `invalid_value` 取 `uint32_t` 的最大值，而不是 `-1` 或 `0`？

**参考答案**：因为它要同时表达「一个不可能的有效值」和「无限等待时长」。`uint32_t` 最大值 \(2^{32}-1\) 是个不会与真实毫秒数冲突的哨兵；若用 `0`，会和「立即超时」混淆；用 `-1` 又与无符号类型语义不符。取最大值是「哨兵值」的常见取舍。

**练习 2**：`data_length` 和 `large_msg_limit` 都等于 64，作者为什么仍分开写两个名字？

**参考答案**：它们语义不同——`data_length` 是「队列槽位长度」，`large_msg_limit` 是「是否走大消息路径的阈值」。当前恰好相等（即「超过一个槽位就算大消息」），但二者是独立的「旋钮」。分开命名让代码自解释，将来若想把阈值调大，只改 `large_msg_limit` 即可，不必动队列结构。

---

### 4.2 relat / trans：关系枚举

#### 4.2.1 概念说明

一条通道上，生产者（写者）和消费者（读者）各有多少个？消息是「一对一投递」还是「一对多广播」？这两组问题决定了通道的**并发形态**。libipc 用两个枚举把答案编码成「策略」：

- `relat`（relationship，关系）：描述某一端是「单个」还是「多个」。
- `trans`（transmission，传输）：描述消息是「单播」还是「广播」。

#### 4.2.2 核心流程

两个枚举本身只是标签，真正的「组合」发生在策略模板 `wr<Rp, Rc, Ts>` 里（见 4.3）。组合空间如下：

- `relat` 有 2 个值：`single`、`multi`。
- 它要分别描述生产者端（`Rp`）和消费者端（`Rc`），所以生产者/消费者多重性有 \(2 \times 2 = 4\) 种。
- `trans` 有 2 个值：`unicast`、`broadcast`。
- 三者组合，理论上有 \(2 \times 2 \times 2 = 8\) 种通道形态。

但「理论可组合」不等于「库都实现了」。库只挑了其中几种实现（见 4.3 和综合实践）。

#### 4.2.3 源码精读

两个枚举的定义极其简洁：

```cpp
enum class relat { // multiplicity of the relationship
  single,
  multi
};

enum class trans { // transmission
  unicast,
  broadcast
};
```

[include/libipc/def.h:41-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L41-L49) —— `relat` 描述「单/多」，`trans` 描述「单播/广播」。

> 术语解释：`enum class` 是 C++11 的**强类型枚举（scoped enumeration）**。它的值不会泄漏到外层作用域，必须写 `relat::single` 而非裸 `single`；而且不会隐式转成 `int`，避免误用。注释里的 `multiplicity` 直译即「多重性」。

回顾 u1-l4：`route` 是单写多读广播，`channel` 是多写多读广播。现在可以用枚举精确表达：

| 类型 | 生产者 `Rp` | 消费者 `Rc` | 传输 `Ts` |
|------|------------|------------|----------|
| `route`   | `relat::single` | `relat::multi`  | `trans::broadcast` |
| `channel` | `relat::multi`  | `relat::multi`  | `trans::broadcast` |

对照 [include/libipc/ipc.h:219](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L219) 与 [include/libipc/ipc.h:228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L228)，你会看到 `route` 和 `channel` 正是这样用三个枚举值拼出来的别名。

#### 4.2.4 代码实践

**实践目标**：用枚举组合描述现实场景，建立「枚举值 ↔ 通道形态」的直觉。

**操作步骤**：阅读 [include/libipc/ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228)，然后回答下面两个问题，把答案写下来。

1. 「一个日志采集进程，向多个分析进程广播日志」——这对应 `route` 还是 `channel`？为什么？
2. 「多个传感器进程都要把数据发给同一个汇总进程」——这需要 `Rp`、`Rc` 各是什么？

**需要观察的现象 / 预期结果**（源码阅读型，待本地核对）：

1. 一个写者 + 多个读者 + 广播 → `Rp=single, Rc=multi, Ts=broadcast` → 正是 `route`。
2. 多个写者 + 一个读者 → `Rp=multi, Rc=single`。注意：库**没有**提供 `Rc=single` 的广播预设别名（见 4.3.3 的实例化门），所以这种形态需要自己写 `ipc::chan<ipc::relat::multi, ipc::relat::single, ipc::trans::broadcast>`，并且**不会被库实例化**（编译会因找不到 `prod_cons_impl` 特化或实例化门而失败）——这一点正是综合实践要验证的关键。

#### 4.2.5 小练习与答案

**练习 1**：`trans::unicast` 和 `trans::broadcast` 在「同一条消息会被几个消费者读到」上有何区别？

**参考答案**：`unicast` 是「一个消费者读走，消息就消失」（点对点抢占）；`broadcast` 是「每个消费者各自读一份，要等所有接收者都读完才回收」（一写多读）。这就是为什么广播模式需要 `rc_` 读计数和 epoch 协议（u4-l4），而单播不需要。

**练习 2**：为什么 `relat` 要分别给生产者和消费者各一个值，而不是合并成一个「通道多重性」枚举？

**参考答案**：因为生产者和消费者的多重性是**独立**的两个维度。「单写多读」「多写多读」「单写单读」是不同的并发问题，需要不同的无锁算法（见 u4）。把两个维度拆开，才能组合表达全部形态，也才能让 `relat_trait` 分别判定 `is_multi_producer` 和 `is_multi_consumer`。

---

### 4.3 wr<> 策略标签与 relat_trait

#### 4.3.1 概念说明

`relat` 和 `trans` 只是枚举值，如何把它们「打包」成一个类型，用来给整条通道打标签？答案就是策略标签 `wr<Rp, Rc, Ts>`。

但光有标签还不够。底层代码（循环队列）需要根据「是不是多生产者」「是不是多消费者」「是不是广播」选择不同的算法分支。这种「从策略类型推导出布尔属性」的工作，由**类型萃取（type trait）** `relat_trait` 完成。

`wr<>` 是**标签**，`relat_trait` 是**翻译器**——把标签翻译成编译期布尔常量，供模板分派使用。

#### 4.3.2 核心流程

整套机制的数据流是：

1. 用户（或 `route`/`channel` 别名）给出一个 `wr<Rp, Rc, Ts>` 标签。
2. `chan_wrapper<wr<...>>` 把标签一路传递到底层的 `prod_cons_impl<wr<...>>`（无锁队列算法）。
3. `relat_trait<wr<...>>` 从标签萃取出三个布尔值：`is_multi_producer`、`is_multi_consumer`、`is_broadcast`。
4. 循环队列的 `elem_array` 用这三个布尔值，在编译期选择「发送者检查器」「接收者检查器」的具体实现。

也就是说，**一次策略选择，会在编译期连锁地决定多处代码分支**，而没有任何运行期开销。

#### 4.3.3 源码精读

`wr` 是个空结构体，纯粹当「类型标签」用：

```cpp
template <relat Rp, relat Rc, trans Ts>
struct wr {};
```

[include/libipc/def.h:53-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L53-L54) —— `Rp`（producer）、`Rc`（consumer）、`Ts`（transmission）三个非类型模板参数打包成一个空类。它不持有任何数据，仅靠「类型本身」携带策略信息。

接着是 `relat_trait`。先看主声明和针对 `wr<>` 的特化：

```cpp
template <typename WR>
struct relat_trait;

template <relat Rp, relat Rc, trans Ts>
struct relat_trait<wr<Rp, Rc, Ts>> {
  constexpr static bool is_multi_producer = (Rp == relat::multi);
  constexpr static bool is_multi_consumer = (Rc == relat::multi);
  constexpr static bool is_broadcast      = (Ts == trans::broadcast);
};
```

[include/libipc/def.h:56-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L56-L64) —— 给定 `wr<Rp,Rc,Ts>`，trait 把三个枚举值各判等一次，得到三个编译期布尔常量。例如对 `route`（`wr<single, multi, broadcast>`）：`is_multi_producer=false`、`is_multi_consumer=true`、`is_broadcast=true`。

关键的一步——「剥壳」特化：

```cpp
template <template <typename> class Policy, typename Flag>
struct relat_trait<Policy<Flag>> : relat_trait<Flag> {};
```

[include/libipc/def.h:66-67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L66-L67) —— 这一行是整个设计的「点睛之笔」。底层实际使用的是 `prod_cons_impl<flag_t>`（一个**模板模板参数** `Policy` 套在 `Flag` 外面），而不是裸 `wr<>`。这个特化说：「只要遇到 `Policy<Flag>` 这种『外面包了一层策略模板』的形态，就剥掉外层 `Policy`，直接继承 `relat_trait<Flag>` 的结论」。于是 `relat_trait<prod_cons_impl<wr<...>>>` 自动等价于 `relat_trait<wr<...>>`，无需为每种 `Policy` 重复写特化。

> 术语解释：`template <typename> class Policy` 是**模板模板参数（template template parameter）**——它本身是一个「单参数模板」，可以接收 `prod_cons_impl` 这样的模板名作为实参。

这套 trait 在底层如何被消费？看 `elem_array`：

```cpp
sender_checker  <policy_t, relat_trait<policy_t>::is_multi_producer> s_ckr_;
receiver_checker<policy_t, relat_trait<policy_t>::is_multi_consumer> r_ckr_;
```

[src/libipc/circ/elem_array.h:95-96](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L95-L96) —— `policy_t` 就是 `prod_cons_impl<wr<...>>`。`relat_trait<policy_t>::is_multi_producer` 通过「剥壳」特化拿到布尔值，进而让 `sender_checker` 选择 `true`（多生产者，恒可连接）或 `false`（单生产者，用 `atomic_flag` 抢占唯一写者位）两种实现之一。同理 `elem_def.h` 用 `is_broadcast` 选择广播/单播的元素结构（[src/libipc/circ/elem_def.h:53](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L53)）。这就是「trait 驱动编译期分派」。

那么 `route`/`channel` 如何与 `wr` 衔接？看 `ipc.h` 的别名链：

```cpp
template <relat Rp, relat Rc, trans Ts>
using chan = chan_wrapper<ipc::wr<Rp, Rc, Ts>>;
// ...
using route   = chan<relat::single, relat::multi, trans::broadcast>;
using channel = chan<relat::multi , relat::multi, trans::broadcast>;
```

[include/libipc/ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228) —— `chan` 把 `(Rp,Rc,Ts)` 包成 `wr<>` 再塞进 `chan_wrapper`；`route`/`channel` 只是 `chan` 的两个预设。可见两者是**同一个模板的不同参数**，没有本质区别。

最后，库**真正编译了哪几种 `wr<>`**？看 `ipc.cpp` 末尾的「显式实例化门」：

```cpp
template struct chan_impl<ipc::wr<relat::single, relat::single, trans::unicast  >>;
// template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::unicast  >>; // TBD
// template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::unicast  >>; // TBD
template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::broadcast>>;
template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::broadcast>>;
```

[src/libipc/ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850) —— 只有 3 种被显式实例化：`single-single-unicast`、`single-multi-broadcast`（= `route`）、`multi-multi-broadcast`（= `channel`）。两个 `unicast` 的多读/多写变体被注释为 `TBD`（待实现）。因此，即便 `def.h` 允许 8 种组合，**用户实际能用的只有这 3 种**；自己写别的 `wr<>` 组合会因缺少实例化而链接失败。

> 术语解释：**显式实例化（explicit instantiation）**`template struct chan_impl<X>;` 命令编译器「在这里为类型 `X` 生成完整代码」。库把它集中在 `ipc.cpp`，是为了把模板的实现留在 `.cpp` 里（缩短用户编译时间、隐藏实现），代价是只暴露被实例化的那些类型——这道「门」就是 u8-l5 要讨论的架构取舍之一。

#### 4.3.4 代码实践

**实践目标**：用 `static_assert` 在编译期验证 `relat_trait` 对 `route`/`channel` 的判定，亲手体会「策略 → 布尔」的萃取。

**操作步骤**：新建 `check_trait.cpp`（示例代码）：

```cpp
#include "libipc/def.h"
#include "libipc/ipc.h"   // 取得 route / channel 别名
#include "libipc/prod_cons.h"
#include <type_traits>

// 取出 route / channel 对应的 wr 标签类型（通过它们的 chan_wrapper 模板参数）
using route_wr   = ipc::wr<ipc::relat::single, ipc::relat::multi, ipc::trans::broadcast>;
using channel_wr = ipc::wr<ipc::relat::multi , ipc::relat::multi, ipc::trans::broadcast>;

static_assert( ipc::relat_trait<route_wr>::is_multi_producer == false, "route: single producer");
static_assert( ipc::relat_trait<route_wr>::is_multi_consumer == true,  "route: multi consumer");
static_assert( ipc::relat_trait<route_wr>::is_broadcast      == true,  "route: broadcast");

static_assert( ipc::relat_trait<channel_wr>::is_multi_producer == true, "channel: multi producer");
static_assert( ipc::relat_trait<channel_wr>::is_multi_consumer == true, "channel: multi consumer");
static_assert( ipc::relat_trait<channel_wr>::is_broadcast      == true, "channel: broadcast");

int main() { return 0; }
```

用 `g++ -std=c++17 -Iinclude -Isrc/libipc check_trait.cpp -fsyntax-only` 做一次纯语法/编译检查（`-fsyntax-only` 不生成可执行文件，只验证编译期断言）。

**需要观察的现象**：编译通过，没有任何 `static_assert` 报错。

**预期结果**：编译无错误即说明三个布尔判定与预期一致。若你故意把某条断言改反（例如把 `route` 的 `is_multi_producer` 断言为 `true`），编译器会报 `static_assert failed`，从而反向印证 trait 的取值。

> 说明：若你的工具链包含完整构建环境，也可改成完整编译运行；本实践的核心是「编译期断言」，因此 `-fsyntax-only` 足矣。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 [def.h:66-67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L66-L67) 的「剥壳」特化，`elem_array.h` 里 `relat_trait<policy_t>::is_multi_producer` 还能编译通过吗？为什么？

**参考答案**：不能。`policy_t` 是 `prod_cons_impl<wr<...>>`，外面包了一层 `prod_cons_impl`。没有这个特化，编译器只会匹配 `relat_trait<wr<...>>` 的特化，而 `prod_cons_impl<wr<...>>` 跟 `wr<...>` 不是同一类型，于是落到「未定义的主模板 `relat_trait<WR>`」，访问 `is_multi_producer` 即报错。剥壳特化正是为了让 trait 能「穿透」外层策略模板。

**练习 2**：`route` 和 `channel` 的 `relat_trait` 只在哪一个布尔值上不同？

**参考答案**：只在 `is_multi_producer` 上不同（`route`=false，`channel`=true）。`is_multi_consumer` 和 `is_broadcast` 都是 true。这一个布尔之差，底层就会选用不同的「发送者检查器」和无锁算法（u4）。

---

### 4.4 prefix：前缀标签

#### 4.4.1 概念说明

libipc 的通道靠 `name` 字符串区分。但同名通道背后，其实创建了好几个共享内存对象（等待器、累加器、队列等）。如果两套互不相关的程序碰巧都用了叫 `"ipc"` 的通道，就会互相干扰。

`prefix` 标签就是为解决这个问题而设的「命名空间前缀」：给一组通道加一个公共前缀，让它们的底层共享内存对象名都带上这个前缀，从而与其他前缀的通道隔离开。

#### 4.4.2 核心流程

`prefix` 的使用路径是：

1. 用户构造通道时传入 `ipc::prefix{某字符串}`。
2. 实现层把 `prefix`、固定分隔符、各组件标签（如 `CC_CONN__`、`QU_CONN__`）和 `name` 拼接，生成最终的共享内存对象名。
3. 所有同名 + 同前缀的进程因此连到同一组共享内存对象；换一个前缀则完全隔离。

#### 4.4.3 源码精读

`prefix` 结构体极其简单，只持有一个 C 字符串指针：

```cpp
struct prefix {
  char const *str;
};
```

[include/libipc/def.h:70-72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L70-L72) —— 它是个「带标签的字符串包装」，作用是让函数重载能区分「普通名字」和「带前缀的名字」。

在公共 API 侧，`chan_wrapper` 提供了接收 `prefix` 的连接重载：

```cpp
bool connect(prefix pref, char const * name, unsigned mode = ipc::sender | ipc::receiver);
```

[include/libipc/ipc.h:140-144](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b2c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L140-L144) —— 带前缀的 `connect` 与普通 `connect` 并存，多传一个 `prefix` 参数。`chan_impl` 也对应有带 `prefix` 的静态接口（[include/libipc/ipc.h:25](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L25) 与 [include/libipc/ipc.h:38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L38)）。

`prefix` 如何变成共享内存名？看实现层。`conn_info_head` 把 `prefix` 存下来，并在打开每个底层对象时参与命名：

```cpp
conn_info_head(char const * prefix, char const * name)
    : prefix_{ipc::make_string(prefix)}
    , name_  {ipc::make_string(name)}, cc_id_ {}
{}
// ...
void init() {
    if (!cc_waiter_.valid()) cc_waiter_.open(ipc::make_prefix(prefix_, "CC_CONN__", name_).c_str());
    if (!wt_waiter_.valid()) wt_waiter_.open(ipc::make_prefix(prefix_, "WT_CONN__", name_).c_str());
    if (!rd_waiter_.valid()) rd_waiter_.open(ipc::make_prefix(prefix_, "RD_CONN__", name_).c_str());
    if (!acc_h_.valid())     acc_h_.acquire(ipc::make_prefix(prefix_, "AC_CONN__", name_).c_str(), sizeof(acc_t));
    // ...
}
```

[src/libipc/ipc.cpp:120-129](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L120-L129) —— 每个底层对象（连接确认等待器 `CC_CONN__`、写满等待器 `WT_CONN__`、读空等待器 `RD_CONN__`、累加器 `AC_CONN__`）的名字，都是 `prefix_` + 组件标签 + `name_` 拼出来的。改变 `prefix_`，这一整套对象名就全部改变，从而实现隔离。

拼接函数 `make_prefix` 用一个固定分隔符 `__IPC_SHM__` 把多段字符串接起来：

```cpp
/// \brief Combine prefix from a list of strings.
template <typename A1, typename... A>
inline std::string make_prefix(A1 &&prefix, A &&...args) {
  return ipc::fmt(std::forward<A1>(prefix), "__IPC_SHM__", std::forward<A>(args)...);
}
```

[src/libipc/mem/resource.h:33-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L33-L37) —— `make_prefix(pref, "CC_CONN__", name)` 会产出形如 `pref__IPC_SHM__CC_CONN____IPC_SHM__name` 的字符串。`__IPC_SHM__` 这个特殊分隔符既能避免与用户名字冲突，也便于清理时反查（`clear_storage` 用同样的拼接逻辑定位并删除这些对象，见 [src/libipc/ipc.cpp:152-158](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L152-L158)）。

> 小结：`prefix` 本身只是个字符串包装，但通过 `make_prefix` 的统一拼接，它变成了「命名空间」。这正是 Windows 服务端 demo 用 `Global\` 之类前缀实现跨会话通信的基础（u5-l4、u8-l4 会用到）。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读，理解「同名 + 不同前缀」如何产生互不干扰的两套共享内存对象。

**操作步骤**：

1. 阅读 [src/libipc/ipc.cpp:120-129](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L120-L129) 与 [src/libipc/mem/resource.h:33-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L33-L37)。
2. 手动推演两个通道在「读空等待器」上的最终共享内存名：
   - 通道 A：`ipc::channel(ipc::prefix{"appA"}, "log", ipc::receiver)`
   - 通道 B：`ipc::channel(ipc::prefix{"appB"}, "log", ipc::receiver)`
3. 写出二者 `rd_waiter_` 对应的对象名。

**需要观察的现象 / 预期结果**（推演型，待本地核对）：

- 通道 A 的读空等待器名（`RD_CONN__` 组件）应为：`appA__IPC_SHM__RD_CONN____IPC_SHM__log`
- 通道 B 的读空等待器名应为：`appB__IPC_SHM__RD_CONN____IPC_SHM__log`

二者前缀不同 → 对象名不同 → 连到不同的共享内存 → 互不干扰。这正是 `prefix` 的隔离价值。

#### 4.4.5 小练习与答案

**练习 1**：如果两个进程一个用 `prefix{"appA"}`、另一个不带 `prefix`（即 `prefix` 为空串）连接同名通道 `"log"`，它们能通信吗？

**参考答案**：不能正常通信（除非空串前缀恰好与某方一致）。`make_string(nullptr)` 或空串会得到空 `std::string`，于是对象名变成 `__IPC_SHM__RD_CONN____IPC_SHM__log`，而带 `appA` 前缀的一方是 `appA__IPC_SHM__...`，二者名字不同，连到不同对象。要让双方通信，必须用**一致的前缀**（包括都用空前缀）。

**练习 2**：`prefix` 为什么设计成一个只含 `char const*` 的结构体，而不是直接用 `std::string` 或裸 `const char*`？

**参考答案**：用结构体是为了**函数重载分派**——`connect(prefix, name, mode)` 和 `connect(name, mode)` 能靠参数类型区分，避免歧义；同时把「这是一个前缀」的语义显式编码进类型，比裸 `const char*` 更安全、更自文档化。用 `char const*` 而非 `std::string` 则是为了保持头文件轻量（`def.h` 不引入 `<string>`），符合该头文件「只依赖 `<cstddef>` 等最小标准库」的风格。

---

## 5. 综合实践

本讲的核心实践任务是：**列出 `relat` 与 `trans` 的全部组合，用 `chan` 别名语法写出对应的通道类型定义，并标注哪些被库实例化**。

### 5.1 全部 8 种组合

生产者 `Rp`、消费者 `Rc` 各有 `single`/`multi` 两种，传输 `Ts` 有 `unicast`/`broadcast` 两种，共 \(2 \times 2 \times 2 = 8\) 种组合。逐一写出 `chan` 别名（示例代码）：

```cpp
#include "libipc/def.h"
#include "libipc/ipc.h"

// ---- unicast：单播 ----
using u_ss = ipc::chan<ipc::relat::single, ipc::relat::single, ipc::trans::unicast>; // 单写单读单播
using u_sm = ipc::chan<ipc::relat::single, ipc::relat::multi , ipc::trans::unicast>; // 单写多读单播
using u_ms = ipc::chan<ipc::relat::multi , ipc::relat::single, ipc::trans::unicast>; // 多写单读单播
using u_mm = ipc::chan<ipc::relat::multi , ipc::relat::multi , ipc::trans::unicast>; // 多写多读单播

// ---- broadcast：广播 ----
using b_ss = ipc::chan<ipc::relat::single, ipc::relat::single, ipc::trans::broadcast>; // 单写单读广播
using b_sm = ipc::chan<ipc::relat::single, ipc::relat::multi , ipc::trans::broadcast>; // 单写多读广播 = route
using b_ms = ipc::chan<ipc::relat::multi , ipc::relat::single, ipc::trans::broadcast>; // 多写单读广播
using b_mm = ipc::chan<ipc::relat::multi , ipc::relat::multi , ipc::trans::broadcast>; // 多写多读广播 = channel
```

### 5.2 标注：库实例化与公开别名情况

对照 [src/libipc/ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850) 的显式实例化门，以及 [src/libipc/prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) 是否提供对应 `prod_cons_impl` 特化，得到下表：

| 别名 | wr 组合 | 公开预设 | 库是否实例化 | prod_cons 是否有特化 |
|------|---------|----------|--------------|---------------------|
| `u_ss` | `single, single, unicast`   | 无 | ✅ 是（ipc.cpp:846） | ✅ 是（prod_cons.h:26） |
| `u_sm` | `single, multi, unicast`    | 无 | ❌ 否（注释为 TBD，ipc.cpp:847） | ✅ 是（prod_cons.h:75-76） |
| `u_ms` | `multi, single, unicast`    | 无 | ❌ 否（未列出） | ❌ 否 |
| `u_mm` | `multi, multi, unicast`     | 无 | ❌ 否（注释为 TBD，ipc.cpp:848） | ✅ 是（prod_cons.h:106-107） |
| `b_ss` | `single, single, broadcast` | 无 | ❌ 否（未列出） | ❌ 否 |
| `b_sm` | `single, multi, broadcast`  | **`route`**   | ✅ 是（ipc.cpp:849） | ✅ 是（prod_cons.h:196） |
| `b_ms` | `multi, single, broadcast`  | 无 | ❌ 否（未列出） | ❌ 否 |
| `b_mm` | `multi, multi, broadcast`   | **`channel`** | ✅ 是（ipc.cpp:850） | ✅ 是（prod_cons.h:294） |

### 5.3 结论与验证

**关键结论**：

1. **只有 3 种组合被库实例化**：`u_ss`、`b_sm`（= `route`）、`b_mm`（= `channel`）。
2. **只有 2 种有公开预设别名**：`route` 和 `channel`，它们都是广播（`broadcast`）形态。
3. 即便 `prod_cons.h` 为 `u_sm`、`u_mm` 提供了无锁算法特化，但因为 `ipc.cpp` 没有为它们显式实例化 `chan_impl`，用户直接用这两个 `chan` 别名会**链接失败**（`chan_impl<...>` 的成员函数找不到定义）。
4. `u_ms`、`b_ss`、`b_ms` 三种既无 `prod_cons` 特化、也无实例化，完全不支持。

**验证方式（待本地验证）**：把上面 8 个 `using` 放进一个 `.cpp`，仅作类型定义不会立即报错；但若进一步对未被实例化的别名（如 `u_sm`）调用 `send`/`recv` 并链接 `libipc`，链接器会报「undefined reference to `ipc::chan_impl<...>::send`」之类的错误，从而反向印证「实例化门」的限制。

> 思考题（接 u8-l5）：要启用 `u_sm`（单写多读单播），需要做两件事——在 `ipc.cpp:847` 取消注释，并确认 `prod_cons_impl<wr<single,multi,unicast>>` 的算法已经实现（已有特化）。评估工作量时，要重点检查单播语义下「多消费者」的抢占/回收逻辑是否完备。这部分留到 u4、u8-l5 深入。

## 6. 本讲小结

- `def.h` 是 libipc 的「类型宪法」，集中定义基本类型、常量、策略标签和前缀，全库共用。
- `byte_t`、`uint_t<N>` 提供跨平台固定宽度整数；`invalid_value`（无限等待哨兵）、`default_timeout`（100ms）、`data_length`（64B 分片长度）、`large_msg_*`（大消息阈值/对齐/缓存）是贯穿全库的关键常量。
- `relat`（single/multi）描述生产者或消费者端的多重性，`trans`（unicast/broadcast）描述传输方式，二者组合出通道的并发形态。
- `wr<Rp, Rc, Ts>` 是空体策略标签；`relat_trait` 把它翻译成 `is_multi_producer`/`is_multi_consumer`/`is_broadcast` 三个编译期布尔，并通过「剥壳」特化穿透 `Policy<Flag>` 外层，驱动底层编译期分派。
- `route` = `chan<single, multi, broadcast>`，`channel` = `chan<multi, multi, broadcast>`——二者是同一模板的不同预设；`ipc.cpp` 的显式实例化门决定库只编译 3 种 `wr` 组合。
- `prefix` 是字符串包装的「命名空间前缀」，经 `make_prefix` 用 `__IPC_SHM__` 分隔符与组件标签拼接成共享内存对象名，实现不同前缀通道的隔离。

## 7. 下一步学习建议

本讲只讲了 `def.h` 的「定义」，还没有展开「使用」与「实现」。建议按以下顺序继续：

1. **u2-l2（buffer）**：先看消息容器 `buff_t`，理解消息数据如何承载、析构器如何回收——它是 `def.h` 之外另一个基础公共类型。
2. **u2-l3（句柄生命周期）**：看 `chan_wrapper` 如何把 `wr<>` 标签与 connect/send/recv 串成完整生命周期。
3. **u2-l4（route vs channel）**：把本讲的「32 接收者限制」与连接位图结合，彻底打通「策略标签 → 连接位运算」的链路。
4. 进入 u3、u4 后，你会看到本讲的 `relat_trait` 布尔值如何真正决定无锁队列的算法分支——届时回头重读 4.3，会有更深的体会。

建议在进入下一讲前，确保自己能脱口说出 `route` 与 `channel` 各自的 `(Rp, Rc, Ts)` 三元组，以及二者唯一不同的那个布尔值。
