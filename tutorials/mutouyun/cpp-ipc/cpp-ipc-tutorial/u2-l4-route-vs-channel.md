# route vs channel：并发模型与 32 接收者限制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `route` 与 `channel` 在源码层面**并不是两个独立的类**，而是同一个模板 `chan` 的两套预设参数。
- 解释「生产者/消费者多重性」（`relat::single` / `relat::multi`）如何决定一条通道能否被多个进程**同时写入**或**同时读取**。
- 区分 `trans::broadcast`（广播，一条消息发给所有接收者）与 `trans::unicast`（单播，一条消息只发给一个接收者）。
- 从位运算的角度**推导**出：为什么广播模式下一条通道最多只能挂 **32 个接收者**，并且能用 `conn_head::connect` 的源码亲手验证这个上限。

本讲承接 u2-l3（句柄生命周期），把视角从「单个句柄怎么连/怎么断」拉到「一条通道上可以挂多少个发送方、多少个接收方、它们怎么协作」。

---

## 2. 前置知识

在继续之前，请确认你已经理解下面这些概念（它们在前几讲已经建立）：

- **通道 = 同名共享内存**：两个进程用相同的 `name` 字符串构造通道，就连上了同一块共享内存（见 u1-l4）。
- **`chan_wrapper<Flag>` 与 `handle_t`**：用户面向的 RAII 句柄类型，内部用一个不透明 `void*` 句柄做类型擦除（见 u2-l3）。
- **策略标签 `wr<Rp, Rc, Ts>`**：用三个模板参数打包「生产者多重性 `Rp`、消费者多重性 `Rc`、传输方式 `Ts`」，`relat_trait` 把它萃取成三个编译期布尔（见 u2-l1）。
- **位运算基础**：按位或 `|`、按位与 `&`、按位取反 `~`、异或 `^`，以及「无符号整数加法溢出回绕到 0」这一事实。本讲第 4 节会大量用到。

> 一个关键复习点：`relat_trait<wr<Rp,Rc,Ts>>` 给出三个布尔 `is_multi_producer`、`is_multi_consumer`、`is_broadcast`（[include/libipc/def.h:59-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L59-L64)）。本讲的全部结论都可以由这三个布尔推导出来。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [include/libipc/ipc.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h) | 定义 `chan` 模板别名，以及 `route`、`channel` 两个预设；也定义 `sender`/`receiver` 角色枚举。 |
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | 定义 `relat`、`trans` 枚举与 `wr<>` 策略标签、`relat_trait` 萃取。 |
| [src/libipc/circ/elem_def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h) | 定义连接位图类型 `cc_t` 与 `conn_head`，**32 接收者限制的源头就在这里**。 |
| [src/libipc/queue.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h) | 把 `conn_head::connect` 的结果向上传递，决定第 33 个接收者是否真的「连上」。 |
| [demo/send_recv/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp) | 本讲代码实践要改造/运行的 demo。 |

---

## 4. 核心概念与源码讲解

### 4.1 route 与 channel 别名定义

#### 4.1.1 概念说明

很多 IPC 库会为「单写多读」和「多写多读」分别提供两个不同的类。libipc **不是**这样。它只有一个模板 `chan<Rp, Rc, Ts>`，而 `route` 和 `channel` 只是它的两个 `using` 别名（预设）。理解这一点非常重要：本讲后续讨论的「广播」「32 限制」对 `route` 和 `channel` **完全一样**，因为它们共享同一个 `Ts = trans::broadcast`；二者唯一的区别在生产者那一侧。

#### 4.1.2 核心流程

别名的展开链条是：

```
route    = chan<relat::single, relat::multi, trans::broadcast>
channel  = chan<relat::multi , relat::multi, trans::broadcast>
chan<Rp, Rc, Ts> = chan_wrapper< wr<Rp, Rc, Ts> >
```

也就是说：

- `route`：**单**生产者、**多**消费者、广播。典型用法是 1 个服务端推送，N 个客户端接收。
- `channel`：**多**生产者、**多**消费者、广播。多个进程可以**同时**往里写，所有接收者都能收到。

两者的消费者侧都是 `relat::multi`，传输方式都是 `trans::broadcast`——这就是为什么 32 接收者限制对两者都成立。

#### 4.1.3 源码精读

模板别名 `chan` 把策略标签 `wr` 喂给 `chan_wrapper`：

> [include/libipc/ipc.h:208-209](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L209) —— 定义 `chan` 模板别名，它是 `route`/`channel` 的共同基底。

两个预设（注意各自上方的文档注释，把语义讲得很直白）：

> [include/libipc/ipc.h:211-219](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L211-L219) —— `route` 的定义与注释。注释里写明 *"A route could only be used in 1 to N (one producer/writer to multi consumers/readers)"*，对应 `relat::single` 生产者。

> [include/libipc/ipc.h:221-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L221-L228) —— `channel` 的定义与注释。注释里写明 *"You could use multi producers/writers for sending messages to a channel"*，对应 `relat::multi` 生产者。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `route` 与 `channel` 确实只是同一个模板的不同参数。

**操作步骤**：

1. 新建一个最小 C++ 文件（示例代码，不属于库本身）：

   ```cpp
   // 示例代码：确认 route/channel 是 chan 的预设
   #include "libipc/ipc.h"
   #include <type_traits>

   static_assert(std::is_same_v<ipc::route,
       ipc::chan<ipc::relat::single, ipc::relat::multi, ipc::trans::broadcast>>);
   static_assert(std::is_same_v<ipc::channel,
       ipc::chan<ipc::relat::multi, ipc::relat::multi, ipc::trans::broadcast>>);
   int main() { return 0; }
   ```

2. 编译它（链接 `ipc` 库，参考 u1-l2 的构建方式）。

**需要观察的现象**：`static_assert` 全部通过编译。

**预期结果**：编译成功、无断言失败——证明 `route`/`channel` 与你手写的 `chan<...>` 是同一个类型。

#### 4.1.5 小练习与答案

**练习 1**：如果想定义一条「单生产者、单消费者、广播」的通道，应该怎么写别名？

**参考答案**：`ipc::chan<ipc::relat::single, ipc::relat::single, ipc::trans::broadcast>`。注意库**没有**为它预设别名，而且根据 u2-l1 讲过的「显式实例化门」，这种组合默认不会被编译进库（会链接失败），除非你自己补实例化。

**练习 2**：`route` 和 `channel` 有几个模板参数值不同？

**参考答案**：只有 1 个——生产者多重性 `Rp`（`single` vs `multi`）。消费者多重性 `Rc` 都是 `multi`，传输方式 `Ts` 都是 `broadcast`。

---

### 4.2 生产者/消费者多重性

#### 4.2.1 概念说明

`relat` 枚举只有两个值：`single`（单个）和 `multi`（多个）。它被用在两个位置：

- `Rp`（producer）：这条通道允许多少个进程**同时发送**？`single` = 只能有一个发送方，`multi` = 可以有多个发送方并发写。
- `Rc`（consumer）：这条通道允许挂多少个**接收者**？`single` = 一个接收者，`multi` = 多个接收者。

直觉上：`route` 像「**一个**广播台 → 许多收音机」，`channel` 像「**许多**人对讲机 → 许多人收听」。两者的「许多收听者」是相同的，差别只在发送端是 1 个还是多个。

#### 4.2.2 核心流程

`Rp`/`Rc` 的取值会在底层选择不同的无锁环形队列算法（这些算法在 u4 会详细讲，这里只建立直觉）：

| 组合 | 生产者 | 消费者 | 底层算法要点 |
| --- | --- | --- | --- |
| single-single | 1 | 1 | 最简单的环形队列，读写各一个游标 |
| single-multi | 1 | 多 | 多个接收者用 CAS 抢占同一个读游标 |
| multi-multi | 多 | 多 | 多个发送者需要「提交索引 + 提交标志」协议 |

关键直觉：**生产者是 multi 时，底层必须处理「多个发送者并发写同一个槽」的竞争**，所以多了一个提交（commit）协议；**消费者是 multi 时，必须能区分「每个接收者各自读到哪了」**，这正是下一节广播机制要解决的问题，也是 32 限制的根源。

#### 4.2.3 源码精读

`relat` 枚举的定义：

> [include/libipc/def.h:41-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L41-L44) —— `relat` 枚举，注释点明它是 *"multiplicity of the relationship"*（关系的多重性）。

策略标签 `wr` 把 `Rp/Rc/Ts` 三个值打包成一个类型：

> [include/libipc/def.h:53-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L53-L54) —— `wr<Rp, Rc, Ts>` 模板，名字 `wr` 可理解为 «**w**riter/**r**eader» 策略。

`relat_trait` 把这三个值翻译成编译期布尔：

> [include/libipc/def.h:59-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L59-L64) —— `is_multi_producer`、`is_multi_consumer`、`is_broadcast` 三个布尔，正是底层算法分派的依据。

代入 `route`（`single`/`multi`/`broadcast`）：`is_multi_producer = false`、`is_multi_consumer = true`、`is_broadcast = true`。代入 `channel`（`multi`/`multi`/`broadcast`）：三者全是 `true`。所以二者消费者侧行为一致，差别只在发送端是否允许多写者。

#### 4.2.4 代码实践

**实践目标**：用编译期布尔确认 `route` 不允许并发发送、`channel` 允许。

**操作步骤**：

```cpp
// 示例代码：萃取两个预设的生产者/消费者布尔
#include "libipc/ipc.h"
#include "libipc/def.h"
using namespace ipc;

// route: 单生产者
static_assert(!relat_trait<wr<relat::single, relat::multi, trans::broadcast>>::is_multi_producer);
// channel: 多生产者
static_assert( relat_trait<wr<relat::multi , relat::multi, trans::broadcast>>::is_multi_producer);
// 两者都是多消费者
static_assert(relat_trait<route::policy... >::is_multi_consumer); // 见下方说明
```

> 说明：`chan_wrapper` 并未直接把 `Flag` 暴露为公开 typedef，所以上面第三行更多是「读源码理解」而非可直接编译的断言。可编译的前两行已经足以说明问题。

**需要观察的现象**：前两个 `static_assert` 通过。

**预期结果**：编译通过，确认 `route` 的 `is_multi_producer` 为 `false`，`channel` 为 `true`。

#### 4.2.5 小练习与答案

**练习 1**：既然 `route` 是单生产者，如果我开两个进程都向同一条 `route` 发送，会发生什么？

**参考答案**：语义上 `route` 只承诺单生产者；底层 single-producer 算法没有为多写者竞争做保护（没有提交协议）。多进程并发写同一条 `route` 属于误用，可能导致数据错乱。需要多写者时应使用 `channel`。**待本地验证**具体现象（消息交叉/损坏），但其「不安全」的结论由算法结构决定。

**练习 2**：`route` 和 `channel` 的 `is_multi_consumer` 分别是什么？

**参考答案**：都是 `true`——这正是它们都能挂多个接收者、都受 32 限制影响的原因。

---

### 4.3 广播与单播

#### 4.3.1 概念说明

`trans` 枚举也有两个值：

- `broadcast`（广播）：一条消息会被**所有**当前连接的接收者各收到一份。
- `unicast`（单播）：一条消息只会被**一个**接收者取走（类似工作队列，谁抢到算谁的）。

`route` 和 `channel` **都是 `broadcast`**。所以本讲讨论的「每个接收者都收到」是广播语义；如果你需要「任务分发给空闲工人」的单播语义，库底层算法已经特化了 `unicast` 版本，但公开别名没有直接暴露（且部分组合标为 TBD，见 u8-l5）。

#### 4.3.2 核心流程

广播与单播在「连接管理」上的实现差异，是理解 32 限制的关键：

- **广播**：库必须能**单独区分每一个接收者**（因为要确认「这条消息是不是所有接收者都读过了，读过了才能回收」）。所以每个接收者占用连接位图里的 **1 个 bit**。
- **单播**：接收者之间没有区别，消息被任意一个取走即可。所以连接信息只是一个**计数器**，不需要为每个接收者单独留 bit。

这个「广播需要按位区分接收者」的需求，直接决定了下一节的位图设计。

#### 4.3.3 源码精读

`trans` 枚举：

> [include/libipc/def.h:46-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L46-L49) —— `trans` 枚举，注释点明它是 *"transmission"*（传输方式）。

`is_broadcast` 由 `Ts` 决定（[include/libipc/def.h:63](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L63)）。这个布尔随后被用来选择 `conn_head` 的特化版本：

> [src/libipc/circ/elem_def.h:53-57](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L53-L57) —— `conn_head` 的主模板声明，第二个模板参数默认值是 `relat_trait<P>::is_broadcast`。`true` 走广播特化（位图），`false` 走非广播特化（计数器）。

对比两种 `connect` 实现，就能直观看到广播与单播的差异。

广播特化的 `connect`（位运算，下一节细讲）：

> [src/libipc/circ/elem_def.h:59-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L59-L71) —— 广播模式的 `connect`：在位图里找最低的空闲 bit。

非广播特化的 `connect`（纯计数器）：

> [src/libipc/circ/elem_def.h:92-94](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L92-L94) —— 单播模式的 `connect`：`fetch_add(1)`，单纯自增，**没有 32 的位图上限**（只受 `cc_t` 即 `uint32` 的数值范围限制）。

> [src/libipc/circ/elem_def.h:107-110](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L107-L110) —— 单播模式的 `connected`，注释明确写到 *"In non-broadcast mode, connection tags are only used for counting."*（非广播模式下连接标签只用于计数）。

#### 4.3.4 代码实践

**实践目标**：确认 `route`/`channel` 走的是广播（位图）特化而非单播（计数器）特化。

**操作步骤**：阅读 [elem_def.h:53-57](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L53-L57)，回答：当 `P` 是 `route` 或 `channel` 对应的策略时，`relat_trait<P>::is_broadcast` 是什么？这会让编译器选择 `conn_head<P, ?>` 的哪一个特化？

**需要观察的现象**：纯源码阅读，无需运行。

**预期结果**：`is_broadcast == true` → 选择 `conn_head<P, true>`（第 57 行起的广播特化），也就是**位图版** `connect`。这把 `route`/`channel` 直接绑死到了「位图 → 32 上限」这条路径上。

#### 4.3.5 小练习与答案

**练习 1**：为什么单播模式不需要 32 接收者限制？

**参考答案**：单播模式下连接标签只用于计数（见 [elem_def.h:108](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L108) 注释），`connect` 是 `fetch_add(1)` 自增，不占用独立的 bit，因此没有「位数 = 接收者数」的约束。

**练习 2**：库公开的 `route`/`channel` 别名里，`Ts` 能不能改成 `unicast`？

**参考答案**：从模板语法上可以写 `chan<..., trans::unicast>`，但库的显式实例化门（见 u2-l1、u8-l5）默认没有为单播的多消费者组合编译代码，单写多读/多写多读的单播版本被标为 TBD，直接使用会链接失败。

---

### 4.4 32 连接位图限制

#### 4.4.1 概念说明

这是本讲的核心。广播模式下，libipc 用**一个 32 位整数** `cc_` 当作「连接位图」：第 `i` 位为 1，表示第 `i` 个接收者槽位被占用。因为只有 32 个 bit，所以一条广播通道**最多挂 32 个接收者**，第 33 个会连接失败。

这个限制**不是**随手定的魔法数字，而是 `cc_t` 类型宽度的直接后果：

\[ \text{最大接收者数} = \text{cc\_t 的位宽} = 32 \]

#### 4.4.2 核心流程

连接位图类型定义在这里，注释已经把上限写明了：

> [src/libipc/circ/elem_def.h:16-20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L16-L20) —— `cc_t = uint_t<32>`，注释 *"only supports max 32 connections in broadcast mode"*（广播模式最多 32 个连接）。

广播 `connect` 的核心是这一行位运算：

```cpp
cc_t next = curr | (curr + 1); // find the first 0, and set it to 1.
```

它的作用是「找到 `curr` 里**最低的那个 0 位**，把它置 1，其余位不变」。证明如下：设 `curr` 最低的 0 在第 \(p\) 位，那么第 \(0..p-1\) 位全是 1。计算 \(curr+1\) 时，进位会一路传递：第 \(0..p-1\) 位清零、第 \(p\) 位置 1、更高位不变。于是

\[ \text{next} = curr \lor (curr+1) \]

会让第 \(0..p-1\) 位保持 1（因为 \(curr\) 这些位原本就是 1），第 \(p\) 位变成 1（被 \(curr+1\) 置位），更高位不变——正好就是把最低 0 位置 1。

每个接收者拿到的「连接 id」`cc_id` 是一个**单独的 bit**（1、2、4、8、…、\(2^{31}\)），由异或得到：`return next ^ curr`（只有被置位的那一位不同）。

完整的连接过程：

1. 读取当前位图 `curr = cc_.load()`。
2. 计算 `next = curr | (curr + 1)`（置最低 0 位）。
3. 若 `next == curr`，说明**没有 0 位可置**（32 位全满），返回 `0` 表示失败。
4. 否则用 CAS（`compare_exchange_weak`）把 `cc_` 从 `curr` 改成 `next`；若并发冲突就重试。
5. 成功后返回 `next ^ curr`，即新分配的那个 bit。

我们手动演算前几次连接（只看最低几位）：

| 第几次连接 | `curr`（连接前） | `curr+1` | `next = curr\|(curr+1)` | 返回 `cc_id = next^curr` | 占用的位 |
| :-: | :-: | :-: | :-: | :-: | :-: |
| 1 | `...0000` (0) | `...0001` (1) | `...0001` (1) | 1 | bit 0 |
| 2 | `...0001` (1) | `...0010` (2) | `...0011` (3) | 2 | bit 1 |
| 3 | `...0011` (3) | `...0100` (4) | `...0111` (7) | 4 | bit 2 |
| 4 | `...0111` (7) | `...1000` (8) | `...1111` (15) | 8 | bit 3 |
| … | … | … | … | … | … |
| 32 | `0111...1`（31 个 1） | `1000...0` | `1111...1`（32 个 1） | \(2^{31}\) | bit 31 |
| **33** | `1111...1`（32 个 1） | `0000...0`（**溢出回绕**） | `1111...1`（= `curr`） | —— | **无位可分，返回 0** |

第 33 次连接的关键：`cc_` 已是 32 个全 1，\(curr+1\) 在 32 位无符号整数里**溢出回绕成 0**，于是 \(next = curr \lor 0 = curr\)，触发 `next == curr` 分支，返回 `0`——连接失败。

断开连接则反向清除该 bit，槽位立即被下一次 `connect` 复用（因为算法总是找最低 0 位）。

#### 4.4.3 源码精读

广播 `connect` 的完整实现（含 CAS 自旋重试，处理多个接收者同时连接的竞争）：

> [src/libipc/circ/elem_def.h:59-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L59-L71) —— `connect()`：`curr | (curr+1)` 找最低 0 位；`next == curr` 时返回 0（已满）；CAS 成功后返回 `next ^ curr`（新分配的 bit）。`ipc::yield(k)` 是渐进退避（见 u6-l1）。

断开连接，清除对应 bit：

> [src/libipc/circ/elem_def.h:73-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L73-L75) —— `disconnect(cc_id)`：`fetch_and(~cc_id)` 把该接收者的 bit 清零，归还槽位。

查询当前连接数（经典位计数 / Brian Kernighan 算法）：

> [src/libipc/circ/elem_def.h:81-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L81-L86) —— `conn_count()`：循环执行 `cur &= cur - 1`（每次清掉最低的一个 1），循环次数就是已连接接收者数。

那么「`connect` 返回 0」如何向上传递成「第 33 个接收者收不到消息」？追踪调用链：

> [src/libipc/circ/elem_array.h:111-113](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L111-L113) —— `connect_receiver()` 直接返回 `conn_head::connect()` 的结果（`cc_id`，或 0）。

> [src/libipc/queue.h:76-85](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L76-L85) —— `queue_conn::connect` 把 `connect_receiver()` 的返回值存进 `connected_`；当 `cc_id == 0` 时，后续 `connected(elems)`（即 `connections() & 0 != 0`）为 `false`。

> [src/libipc/queue.h:159-166](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L159-L166) —— `queue_base::connect` 据此返回 `false`：第 33 个接收者的连接在队列层就被判定为未连上。

因为第 33 个接收者在位图里没有 bit，发送方广播时构造的「读计数位图」里也不会包含它，所以它**永远收不到消息**——这正是下一个实践要观察的现象。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手验证一条广播通道最多 32 个接收者，第 33 个连接失败，并用 `conn_head::connect` 的位运算解释原因。

**操作步骤**：

1. 按 u1-l2 构建 libipc 并打开 demo（`LIBIPC_BUILD_DEMOS=ON`），得到 `send_recv` 可执行文件。
2. 启动 1 个发送进程（每 500ms 发一条 16 字节消息）：

   ```bash
   ./send_recv send 16 500
   ```

3. 打开 33 个终端（或用脚本起 33 个进程），每个运行：

   ```bash
   ./send_recv recv 5000
   ```

4. 观察每个接收进程的输出。参考接收循环 [demo/send_recv/main.cpp:28-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L28-L40)：它会反复打印 `recv waiting...`，收到消息才打印 `recv size: 17`。

**需要观察的现象**：

- 前 32 个接收进程：都会周期性打印 `recv size: 17`，说明它们在广播列表里、能收到消息。
- 第 33 个接收进程：**只**打印 `recv waiting... 1`、`recv waiting... 2`、…，**永远不打印** `recv size:`，说明它没被加入广播位图。

**预期结果**：恰好 32 个接收者能收到广播，第 33 个静默失败（连不上、收不到）。这与 `connect` 在位图全满时返回 0、`queue_base::connect` 返回 `false` 的代码路径一致。

> 如果不方便开 33 个终端，可改写一个最小程序（示例代码）：在一个进程里循环构造 33 个 `ipc::channel{"bench", ipc::receiver}`，用 `chan.wait_for_recv` 或自行计数能成功收到数据的句柄数量，观察上限停在 32。具体计数的返回语义**待本地验证**，但「最多 32」这一上限由位图位宽决定、是确定的。

**结合源码的解释**（写进你的实验报告）：当第 33 个接收者调用 `connect` 时，`cc_` 已是 32 位全 1（`0xFFFFFFFF`）。`curr + 1` 在 `uint32` 中溢出回绕为 0，`next = curr | 0 = curr`，命中 `next == curr` 分支，`connect` 返回 0；这个 0 一路传到 `queue_base::connect` 使其返回 `false`，接收者未被登记进位图，因而收不到任何广播。

#### 4.4.5 小练习与答案

**练习 1**：手动演算：当前 `cc_ = 0b1010`（即 bit 1 和 bit 3 被占用，bit 0、bit 2 空闲），下一次 `connect()` 返回什么 `cc_id`？`cc_` 变成什么？

**参考答案**：`curr = 0b1010`，`curr+1 = 0b1011`，`next = 0b1010 | 0b1011 = 0b1011`；返回 `next ^ curr = 0b1011 ^ 0b1010 = 0b0001 = 1`（分配 bit 0，即最低空闲位）。`cc_` 变成 `0b1011`。这印证了「总是找最低 0 位」。

**练习 2**：接上题，如果现在 `disconnect(1)`（归还 bit 0），`cc_` 变成什么？下一次 `connect` 会分配哪个 bit？

**参考答案**：`disconnect` 执行 `cc_ & ~1 = 0b1011 & 0b...1110 = 0b1010`，`cc_` 回到 `0b1010`。下一次 `connect` 又会找到 bit 0（最低 0 位），返回 `cc_id = 1`。可见断开后槽位可被立即复用——位图不会因为频繁连断而「永久占用」。

**练习 3**：为什么 `connect` 用 CAS 循环（`compare_exchange_weak`）而不是直接 `cc_ = next`？

**参考答案**：多个接收者可能**同时**连接，直接赋值会覆盖彼此的修改、导致两个接收者拿到同一个 bit。CAS 循环保证「读 `curr` → 算 `next` → 写回」是原子的：若写入时发现 `cc_` 已被别人改过（`curr` 失效），就重新加载并重试，确保每个 bit 只分给一个接收者。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个综合小任务：

**任务**：写一份「连接位图行为观察报告」，验证「32 上限」与「断开后槽位可复用」两个性质。

1. **阅读型准备**：对照 [elem_def.h:59-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L59-L86)，在纸上预演 `cc_` 从 0 开始连续 `connect` 32 次的过程，确认第 32 次后 `cc_ = 0xFFFFFFFF`、第 33 次 `connect` 返回 0。
2. **运行型验证**：按 4.4.4 的方法启动 1 个 sender + 33 个 receiver，记录「能收到消息的接收者数量」，确认等于 32。
3. **复用性验证**：保持 sender 运行，先关掉其中一个**能收到消息**的 receiver（Ctrl+C 触发 `disconnect`，参考 [demo/send_recv/main.cpp:47-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L47-L54) 的信号处理），再启动之前失败的第 33 个 receiver。
4. **结论**：观察那个原本失败的第 33 个 receiver 现在是否能收到消息。

**预期结论**：第 3 步断开一个接收者后，位图释放出一个 bit；原本失败的第 33 个 receiver 这次能成功连上并收到广播。这同时证明了三件事——①广播最多 32 个接收者；②`disconnect` 会归还 bit；③`connect` 总是复用最低空闲位。**步骤 3、4 的具体可观察时序待本地验证**，但「断开后可重新连入」由 `disconnect` 的 `fetch_and(~cc_id)` 与 `connect` 的「找最低 0 位」共同保证。

---

## 6. 本讲小结

- `route` 与 `channel` **不是两个类**，而是同一个模板 `chan<Rp,Rc,Ts>` 的两个 `using` 预设；二者都是**多消费者 + 广播**，唯一区别在生产者侧（`route` 单写、`channel` 多写）。
- `relat::single/multi` 决定生产者/消费者多重性，`trans::broadcast/unicast` 决定传输方式；`relat_trait` 把它们翻译成 `is_multi_producer`/`is_multi_consumer`/`is_broadcast` 三个编译期布尔，驱动底层分派。
- 广播需要**按位区分每个接收者**（以便确认消息是否被所有接收者读过），因此用位图；单播只需计数，故无此约束。
- 32 接收者上限的根源是连接位图类型 `cc_t = uint32`：每个接收者占 1 个 bit，全满后 `curr+1` 溢出回绕，`connect` 返回 0 即连接失败。
- `connect` 用 `curr | (curr+1)` 找最低空闲位、用 CAS 循环保证并发安全；`disconnect` 用 `fetch_and(~cc_id)` 归还 bit，槽位可被立即复用。

---

## 7. 下一步学习建议

本讲只解释了「为什么是 32」和「连接位图怎么管理接收者」，但还没有回答：

- 广播时，一条消息怎么知道「哪些接收者已经读过了、可以回收」？→ 这涉及消息元素里的 `rc_` 读计数位图，请进入 **u4（无锁循环队列与生产-消费者算法）**，重点看 u4-l4（prod_cons 广播变体）。
- 生产者是 `multi` 时，多个发送者并发写同一个槽怎么不冲突？→ 同样在 u4，看 u4-l3 的「提交索引 + 提交标志」协议。
- `connect` 里的 `ipc::yield(k)` 是什么、为什么不是一上来就睡眠？→ 见 **u6-l1（spin_lock、rw_lock 与 yield/sleep 退避）**。

建议阅读顺序：先 u4-l1/l2 建立队列与元素数组的整体观，再 u4-l3/l4 看单播与广播算法的对比，届时本讲的「位图」「32 限制」会自然嵌入完整的收发链路中。
