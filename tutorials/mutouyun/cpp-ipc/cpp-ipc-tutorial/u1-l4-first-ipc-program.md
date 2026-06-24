# 你的第一个 IPC 程序：route/channel 收发

## 1. 本讲目标

本讲是「动手」环节：在不用懂任何内部实现的前提下，用 libipc 的公共 API 写出**第一个真正能跨进程通信的程序**。学完后你应当能够：

- 用 `ipc::channel` / `ipc::route` 声明一条通道，并选择 `sender`（发送方）或 `receiver`（接收方）角色。
- 调用 `send` 发送 `std::string` / 原始字节 / `buff_t`，并调用 `recv` 接收 `buff_t`。
- 用 `empty()` 判断是否收到消息，用 `data()` / `size()` / `get<T>()` 读取消息内容。
- 理解 `send` / `recv` 的超时参数含义，以及超时后两者的不同行为。
- 在收到信号（如 Ctrl+C）时，用 `disconnect()` 让对端优雅退出阻塞等待。

本讲**只讲公共 API 的用法**，不讲共享内存、无锁队列等内部机制（那是 u3 之后的内容）。如果你还没读过 u1-l3 的「目录与模块地图」，建议先看一眼，知道 `ipc.h` 是聚合门面即可。

---

## 2. 前置知识

### 2.1 进程间通信（IPC）与共享内存

同一个程序里的多个线程，可以通过共享同一个地址空间直接交换数据。但**两个独立进程**的地址空间是隔离的，普通变量互不可见。IPC（Inter-Process Communication，进程间通信）就是让两个进程交换数据的机制，常见手段有管道、套接字（socket）、消息队列、共享内存等。

libipc 用的是**共享内存（shared memory）**：两个进程把同一块物理内存映射进各自的虚拟地址空间，于是写一块、另一块就能立刻读到，没有内核态的数据搬运，吞吐高、延迟低。代价是这块内存里**没有任何语言层面的“谁的内存”概念**，并发读写全靠库自己用无锁算法和同步原语来保证正确。

### 2.2 sender / receiver 与广播

libipc 把一条通道的两端抽象为两类角色：

- **sender（发送方/生产者）**：调用 `send` 往通道里写消息。
- **receiver（接收方/消费者）**：调用 `recv` 从通道里读消息。

通道默认是**广播（broadcast）**模式：一条消息发出后，**所有正在接收的 receiver 都会各自收到一份完整的副本**（而不是被某一个 receiver 抢走）。这是 libipc 与普通“单消费者队列”最大的区别，也是后面实践任务要验证的关键语义。

### 2.3 RAII 与 PIMPL

- **RAII**（Resource Acquisition Is Initialization，资源获取即初始化）：把资源的生命周期绑定到对象的生命周期——构造函数里获取资源，析构函数里释放资源。这样只要对象还在，资源就有效；对象一销毁，资源自动回收。libipc 的 `chan_wrapper` 就是 RAII 句柄。
- **PIMPL**（Pointer to IMPLementation，指向实现的指针）：对外只暴露一个不透明指针（`void*`），真正的实现细节藏在 `.cpp` 里。这样头文件里看不到私有成员，既加快编译，也隔离了平台差异。libipc 用 `handle_t = void*` 做这件事。

如果你对这些术语陌生，记住一句话即可：**`channel`/`route` 对象构造即连接，析构即断开，我们只拿来用，不必关心它内部长什么样。**

---

## 3. 本讲源码地图

本讲只涉及公共 API 头与两个官方 demo，全部位于仓库根目录下：

| 文件 | 作用 |
| --- | --- |
| `include/libipc/ipc.h` | 公共 API 聚合门面：定义 `handle_t`、`buff_t`、`sender`/`receiver`、`chan_wrapper`、`route`、`channel`。本讲的主角。 |
| `include/libipc/def.h` | 基础类型与全局常量：`default_timeout`、`invalid_value`、`relat`/`trans` 枚举。 |
| `include/libipc/buffer.h` | 消息容器 `buffer`（即 `buff_t`）的接口：`empty/data/size/get<T>`。 |
| `demo/send_recv/main.cpp` | 最小可运行 demo：按参数当 `send` 或 `recv` 进程，是本讲示例的模板。 |
| `demo/msg_que/main.cpp` | 进阶 demo：演示 `reconnect` 切换角色、`wait_for_recv` 等待接收方、`send(buff_t)` 发送原始字节。 |

只需要 `#include "libipc/ipc.h"` 一个头，就能拿到上面全部能力（`ipc.h` 已经 `#include` 了 `def.h` 和 `buffer.h`）。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**① channel 构造与模式位 → ② send/recv 基本调用 → ③ buff_t 接收与判空 → ④ disconnect 与信号处理**。

### 4.1 channel 构造与模式位

#### 4.1.1 概念说明

在 libipc 里，一条「通道」由三个维度决定：

- **生产者多重性** `Rp`：`single`（单生产者）或 `multi`（多生产者）。
- **消费者多重性** `Rc`：`single` 或 `multi`。
- **传输方式** `Ts`：`unicast`（单播，一条消息只给一个消费者）或 `broadcast`（广播，所有消费者都收到）。

库把常用组合做成了两个类型别名：`route` 和 `channel`。

#### 4.1.2 核心流程

类型层级是这样的（由模板逐步固化）：

```
chan_impl<Flag>            // 底层静态接口（init/connect/send/recv...）
      ↑
chan_wrapper<Flag>         // RAII 句柄，持有 handle_t，转发给 chan_impl
      ↑
chan<Rp,Rc,Ts>             // chan_wrapper< wr<Rp,Rc,Ts> >  的别名
      ↑
┌─────┴──────┐
route        channel        // 两个预设好的具体类型
```

- `route` = 单写多读广播：**一个**发送方，**多个**接收方，每个接收方都收到完整消息。
- `channel` = 多写多读广播：**多个**发送方，**多个**接收方，广播。

选哪个？如果你只有 1 个数据源（比如一个采集进程把数据扇出给多个处理进程），用 `route` 足够；如果多个进程都要往同一条通道里写，用 `channel`。

#### 4.1.3 源码精读

先看别名定义与角色枚举：

[include/libipc/ipc.h:15-18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L15-L18) —— 定义 `sender = 0`、`receiver = 1` 两个模式位（注意是 `unsigned` 整数，可以按位或）。

[include/libipc/def.h:41-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L41-L49) —— `relat`（生产/消费多重性）与 `trans`（单播/广播）两个枚举。

[include/libipc/ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228) —— `chan` 模板别名，以及 `route`（`single/multi/broadcast`）和 `channel`（`multi/multi/broadcast`）两个预设类型。这段就是上文的层级落地。

再看「构造即连接」的构造函数：

[include/libipc/ipc.h:62-68](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L62-L68) —— `chan_wrapper(char const * name, unsigned mode = ipc::sender)`：在初始化列表里直接调用 `this->connect(name, mode)`。也就是说，**对象构造完成的那一刻，连接就已经建立了**（如果名字非空且连接成功，`connected_` 为真）。

[include/libipc/ipc.h:135-144](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L135-L144) —— `connect(name, mode)` 的实现：先 `disconnect` 掉旧连接，再调用底层 `chan_impl::connect(&h_, name, mode_)`。注意 **`mode` 的默认值是 `ipc::sender | ipc::receiver`**（既是发送方又是接收方）；而 demo 里通常会显式只传 `ipc::sender` 或 `ipc::receiver`。

> **为什么是同名字符串？** 两个进程只要用**同一个 `name`** 构造通道，就被认为连到**同一条**共享内存通道上（名字会被加上内部前缀作为共享内存对象名）。名字相同 = 同一通道；名字不同 = 互不相干。

#### 4.1.4 代码实践

**目标**：感受「构造即连接」与「同名 = 同一通道」。

1. 阅读上面引用的构造函数与 `connect`。
2. 写两行代码（不必真的运行，先理解）：
   ```cpp
   ipc::channel producer {"ipc", ipc::sender};   // 进程 A
   ipc::channel consumer {"ipc", ipc::receiver}; // 进程 B
   ```
3. 思考：如果把进程 B 的名字改成 `"ipc2"`，A 和 B 还能通信吗？
4. **预期结果**：不能。两者会连到两条不同的通道上，B 永远收不到 A 的消息。

#### 4.1.5 小练习与答案

**练习 1**：`route` 和 `channel` 在模板参数上的唯一区别是什么？
**答**：生产者多重性 `Rp`。`route` 是 `relat::single`（单生产者），`channel` 是 `relat::multi`（多生产者）；两者都是 `relat::multi` 消费者 + `trans::broadcast`。

**练习 2**：`connect` 的 `mode` 参数默认值是什么？为什么 demo 里反而要显式写 `ipc::sender` / `ipc::receiver`？
**答**：默认是 `ipc::sender | ipc::receiver`（读写都开）。demo 里显式指定单一角色，是为了让职责清晰，也便于演示纯发送/纯接收的行为。

---

### 4.2 send/recv 基本调用

#### 4.2.1 概念说明

连上通道之后，发送方调 `send`，接收方调 `recv`。`chan_wrapper` 为 `send` 提供了**三个重载**，让我们既能发送 `std::string`，也能发送原始字节指针，还能发送现成的 `buff_t`。`recv` 只有一种形式：返回一个 `buff_t`。

#### 4.2.2 核心流程

```
发送方:                                   接收方:
channel ch {N, ipc::sender};              channel ch {N, ipc::receiver};
ch.send("hello");          ──写共享内存──>  buff_t b = ch.recv();
                                          if (!b.empty()) auto s = b.size();
```

`send` / `recv` 都带一个超时参数 `tm`（单位：毫秒），但语义不同，务必区分：

| 调用 | 默认 `tm` | 超时后的行为 |
| --- | --- | --- |
| `send(...)` | `default_timeout` = 100 ms | 超时会调用 `force_push` **强制发送**（可能覆盖未被消费的旧消息），并返回 `true`。 |
| `try_send(...)` | `default_timeout` = 100 ms | 超时**直接返回 `false`**，不强制发送。 |
| `recv(...)` | `invalid_value` | `invalid_value` 表示“不主动超时”，一直阻塞等待，直到有消息或对端断开。也可传具体毫秒数做超时等待，超时返回**空** `buff_t`。 |

> 这里先建立一个直觉：`send` 是「**我一定要发出去**」，发不动就硬塞；`try_send` 是「**试一下，不行就算了**」。`recv` 默认会耐心等下去。

#### 4.2.3 源码精读

先看常量定义：

[include/libipc/def.h:28-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L28-L31) —— `invalid_value = uint32 最大值`、`default_timeout = 100`（毫秒）。注意它们是 `std::uint32_t`，所以 `tm` 的类型是 `std::uint64_t`，传 `0` 是「立即」、传 `invalid_value` 是「无限等待」。

再看三个 `send` 重载：

[include/libipc/ipc.h:176-184](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L176-L184) ——
- `send(void const*, size, tm)`：最底层，直接发送原始字节；
- `send(buff_t const&, tm)`：转发到上面那个，用 `buff.data()/buff.size()`；
- `send(std::string const&, tm)`：注意它发的是 `str.c_str()` 且长度是 **`str.size() + 1`**——**多发的那个字节是字符串结尾的 `\0`**，这样接收端把 `data()` 当 C 字符串读时正好有终止符。

然后是 `recv` / `try_recv`：

[include/libipc/ipc.h:199-205](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L199-L205) —— `recv(tm = invalid_value)` 与 `try_recv()`，都返回 `buff_t`。

最后看真实用法。`send_recv` demo 里：

[demo/send_recv/main.cpp:17-26](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L17-L26) —— 发送方：构造一个 `size` 个 `'A'` 的 `std::string`，循环 `ipc.send(buffer, 0)`（`tm=0` 表示发不动就立即 force_push），并打印发送大小。

[demo/send_recv/main.cpp:28-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L28-L40) —— 接收方：`recv(interval)` 带毫秒超时，循环里用 `recv.empty()` 判断是否还要继续等。

`msg_que` demo 还演示了「等不到接收方就先 `wait_for_recv`」的模式：

[demo/msg_que/main.cpp:70-80](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/msg_que/main.cpp#L70-L80) —— 发送失败时打印提示并 `que__.wait_for_recv(1)`，阻塞等待至少 1 个接收方上线再继续。

#### 4.2.4 代码实践

**目标**：亲手跑通收发，观察 `send` 的字符串长度。

1. 按 u1-l2 的方法构建并开启 `LIBIPC_BUILD_DEMOS`。
2. 开两个终端：
   ```
   # 终端 1（接收，间隔 200ms 轮询）
   ./bin/send_recv recv 200
   # 终端 2（发送，每条 16 字节，间隔 500ms）
   ./bin/send_recv send 16 500
   ```
3. **需要观察的现象**：接收端打印 `recv size: 17`（发送端打印 `send size: 17`）。
4. **预期结果 / 解释**：发送的是 16 个字符的 `std::string`，但 `send(std::string)` 实际发了 `size()+1 = 17` 字节（含 `\0`）。这正是上面 4.2.3 看到的 `str.size() + 1`。
5. 如果你的构建参数或路径不同导致无法运行，请标注「待本地验证」，但**这个 `+1` 的结论来自源码，可独立确认**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `send(std::string)` 发送的字节数是 `str.size() + 1`？
**答**：为了把结尾的 `\0` 一起发出去，接收端可以直接把 `data()` 当 C 字符串用。

**练习 2**：`send` 超时会强制发送，`try_send` 超时会直接失败——请各举一个适用场景。
**答**：`send` 适合「这条消息绝不能丢」（如关键控制指令），宁可覆盖也要发出去；`try_send` 适合「丢了也没关系、别阻塞主循环」（如高频遥测采样，发不动就丢弃本帧）。

---

### 4.3 buff_t 接收与判空

#### 4.3.1 概念说明

`recv` 不直接返回 `std::string`，而是返回一个 `buff_t`（即 `buffer`）。原因是消息可能是**任意二进制数据**，而且（后续 u3 会讲）大消息可能是跨多片重组、甚至带外部存储的。`buff_t` 是承载这一切的统一容器。

对初学者来说，掌握三个方法就够用：

- `empty()`：是否为空（没收到有效消息）。
- `data()`：指向数据的指针（`void*`）。
- `size()`：数据字节数。

要把数据当具体类型用，用模板 `get<T>()`，它等价于 `T(data())`——把指针强转成 `T`。

#### 4.3.2 核心流程

```
buff_t b = ch.recv(tm);
if (b.empty()) { /* 超时或对端断开，没有消息 */ }
else {
    auto p = b.get<char const *>();  // 当 C 字符串
    std::size_t n = b.size();        // 字节数
    auto v = b.to_vector();          // 也可以拷成 std::vector<byte_t>
}
```

> **关键判断**：`recv` 之后**第一件事永远是 `empty()` 检查**。空可能意味着「超时没等到」，也可能意味着「对端断开了连接」。不检查就用 `data()`/`size()`，逻辑上是不安全的。

#### 4.3.3 源码精读

`buff_t` 的类型别名在门面里：

[include/libipc/ipc.h:12-13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L12-L13) —— `using handle_t = void*;` 和 `using buff_t = buffer;`。所以我们用的 `buff_t` 就是 `buffer` 类。

`buffer` 的核心访问接口：

[include/libipc/buffer.h:37-45](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L37-L45) —— `empty()`、两个 `data()`（const 与非 const 重载）、`size()`。

[include/libipc/buffer.h:42-43](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L42-L43) —— 模板 `get<T>()`：`return T(data());`，即把数据指针强转成 `T`。

[include/libipc/buffer.h:55-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/buffer.h#L55-L60) —— `to_vector()`：把数据拷贝成 `std::vector<byte_t>`（注意是拷贝，不是视图）。

真实判空用法见 demo：

[demo/msg_que/main.cpp:99-103](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/msg_que/main.cpp#L99-L103) —— `auto msg = que__.recv(); if (msg.empty()) break;`，收到空就退出循环（这里 `recv()` 默认无限等待，返回空通常意味着对端断开）。

#### 4.3.4 代码实践

**目标**：把收到的二进制消息读出来，分别按「C 字符串」和「vector」两种方式查看。

1. 在 4.2.4 的接收端基础上，把打印语句改成：
   ```cpp
   buff_t b = ch.recv(200);
   if (b.empty()) continue;
   std::cout << "as string: " << b.get<char const *>() << "\n";
   std::cout << "as vector size: " << b.to_vector().size() << "\n";
   ```
2. **需要观察的现象**：`as string` 能正确打印发送的文本；`as vector size` 等于发送字节数。
3. **预期结果**：因为发送端 `send(std::string)` 带了 `\0`，`get<char const*>()` 按字符串打印会正常截断；`to_vector().size()` 与 `b.size()` 相等。
4. 若本地未运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习**：`buff_t::to_vector()` 和直接用 `data()`+`size()` 有什么区别？
**答**：`to_vector()` 会把数据**拷贝**一份到一个新的 `std::vector<byte_t>` 里，之后 `buff_t` 的生命周期结束也不影响这个 vector；而 `data()` 返回的是**指向内部缓冲的指针**，`buff_t` 一旦析构或 move，指针就悬空了。需要长期持有数据用 `to_vector()`，临时读取用 `data()`。

---

### 4.4 disconnect 与信号处理

#### 4.4.1 概念说明

`recv` 默认是**阻塞等待**的（`tm = invalid_value`）。如果对端进程突然退出（比如被 Ctrl+C 杀掉），接收方不该永远卡在那里。libipc 的设计是：**当发送方调用 `disconnect()`（或其句柄析构）时，正在 `recv` 的接收方会被唤醒，并收到一个空 `buff_t`**，从而能跳出循环、优雅退出。

所以「让对端退出等待」的关键动作就是 `disconnect()`。而为了让本进程在收到 Ctrl+C 时也主动 `disconnect()`，demo 用了 C 信号处理函数 `signal()`。

#### 4.4.2 核心流程

```
进程 A (发送):                 进程 B (接收):
send(...)                       buff_t b = ch.recv();   // 阻塞
                                ...
收到 SIGINT ──> signal handler
  is_quit = true
  ch.disconnect() ───────────>  recv 被唤醒，返回空 buff_t
                                if (b.empty()) break;   // 优雅退出
```

要点：
1. 用一个 `std::atomic<bool> is_quit__` 作为「该退出了」的全局标志，业务循环每次迭代检查它。
2. 注册信号处理函数：收到信号时把 `is_quit__` 置真，并调用 `disconnect()`。
3. 业务循环里 `recv` 返回空 + `is_quit__` 为真，就 `break`。

> **为什么要在信号里调用 `disconnect()`？** 因为信号可能打断 `recv` 的系统调用，但不一定能让 `recv` 优雅返回空。主动 `disconnect()` 修改共享内存里的连接状态，是「通知对端」的可靠方式。

#### 4.4.3 源码精读

`disconnect` 与析构的关系：

[include/libipc/ipc.h:75-77](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L75-L77) —— 析构函数 `~chan_wrapper()` 调用 `detail_t::destroy(h_)`。也就是说，**局部 `channel` 对象离开作用域时会自动清理连接**（RAII）。

[include/libipc/ipc.h:155-159](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L155-L159) —— `disconnect()`：先检查 `valid()`，再调底层 `disconnect(h_)`，并把 `connected_` 置假。它**只断开连接、不销毁句柄**，之后还能用 `connect`/`reconnect` 重新连。

`send_recv` demo 的信号处理是本模块的标准模板：

[demo/send_recv/main.cpp:14-15](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L14-L15) —— 全局原子 `is_quit__` 和指向当前通道的裸指针 `ipc__`（裸指针是为了在 C 信号函数里能访问到对象）。

[demo/send_recv/main.cpp:47-61](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L47-L61) —— `exit` lambda 作为信号处理函数：置 `is_quit__` 为真，若 `ipc__` 非空则 `disconnect()`；并用 `signal()` 注册到 `SIGINT/SIGABRT/SIGSEGV/SIGTERM`（Windows 还额外注册 `SIGBREAK`，POSIX 注册 `SIGHUP`）。

[demo/send_recv/main.cpp:31-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L31-L37) —— 接收循环里每次 `recv` 后都检查 `is_quit__`，一旦为真立即 `return`，避免收到信号后还在死等。

#### 4.4.4 代码实践

**目标**：验证「发送方 `disconnect()` 能唤醒阻塞的接收方」。

1. 按 4.2.4 启动接收端和发送端，让通信跑起来。
2. 在发送端终端按 **Ctrl+C** 触发 `SIGINT`。
3. **需要观察的现象**：发送端执行 `exit` lambda、调用 `disconnect()` 后退出；接收端的 `recv` 不再卡住，返回空并因 `is_quit__` 检查退出循环（或因对端断开而 `recv.empty()` 跳出）。
4. **预期结果**：两端都能干净退出，没有进程残留。如果接收端仍卡住，检查是否漏了信号注册或 `is_quit__` 检查。
5. 行为与系统、shell 对 Ctrl+C 的传递有关，若现象不符请标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`disconnect()` 和直接让 `channel` 对象析构，效果一样吗？
**答**：都能断开连接，但有区别。析构调用 `destroy(h_)`，会销毁句柄，对象之后不可再用；`disconnect()` 只断开连接、保留句柄，之后还能 `connect`/`reconnect` 重连。需要「断开后重连」就用 `disconnect()`。

**练习 2**：为什么 demo 要用一个全局裸指针 `ipc__` 指向通道对象，而不是直接用全局对象？
**答**：因为 C 的 `signal()` 处理函数是普通函数指针，无法捕获作用域变量，也不方便访问非全局对象。用一个全局指针，信号函数里就能 `ipc__->disconnect()`。这是 C 风格信号处理的常见妥协（更现代的 C++ 写法可参考 `std::atomic` + `std::condition_variable`，但跨进程场景下 libipc 自己的 waiter 更合适，这属于 u6 的内容）。

---

## 5. 综合实践：写一个 route 广播小程序

把本讲四个模块串起来，完成规格里要求的核心任务：**进程 A 用 `route` 广播，进程 B 与进程 C 同时接收并打印，验证广播语义。**

> 为什么用 `route` 而不是 `channel`？因为这里只有**一个**发送方，`route`（单写多读广播）正好匹配，且更轻量。两个接收方会各自收到完整消息——这正是「广播」要验证的点。

### 5.1 实践目标

- 巩固：构造通道 + 选择角色、`send(std::string)`、`recv` + `empty()` 判空、信号处理 + `disconnect()`。
- 验证：一条消息发出后，**两个**接收进程都打印出**相同**的内容。

### 5.2 示例代码

下面是一份可直接编译的最小程序（**示例代码**，结构参考 `demo/send_recv/main.cpp`）。保存为 `bcast.cpp`：

```cpp
// 示例代码：route 广播演示
#include <signal.h>
#include <iostream>
#include <string>
#include <chrono>
#include <thread>
#include <atomic>

#include "libipc/ipc.h"

namespace {
std::atomic<bool> is_quit__ {false};
ipc::route *ipc__ = nullptr;   // 全局指针，供信号函数访问

// 发送方：单生产者，往 "ipc-bcast" 通道里每隔 200ms 发一条带序号的消息
void do_bcast() {
    ipc::route ipc {"ipc-bcast", ipc::sender};
    ipc__ = &ipc;
    for (int i = 1; !is_quit__.load(std::memory_order_acquire); ++i) {
        std::string msg = "hello " + std::to_string(i);
        ipc.send(msg);                       // 默认 default_timeout 超时会 force_push
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
}

// 接收方：多消费者之一，阻塞 recv，空则退出
void do_recv(const char *who) {
    ipc::route ipc {"ipc-bcast", ipc::receiver};
    ipc__ = &ipc;
    while (!is_quit__.load(std::memory_order_acquire)) {
        ipc::buff_t recv;
        for (int k = 1; recv.empty(); ++k) {
            recv = ipc.recv(500);            // 500ms 超时轮询
            if (is_quit__.load(std::memory_order_acquire)) return;
        }
        std::cout << who << " recv: " << recv.get<char const *>() << "\n";
    }
}
} // namespace

int main(int argc, char ** argv) {
    if (argc < 2) {
        std::cerr << "usage: " << argv[0] << " bcast | recv [name]\n";
        return -1;
    }
    auto exit = [](int) {
        is_quit__.store(true, std::memory_order_release);
        if (ipc__ != nullptr) ipc__->disconnect();   // 唤醒对端的阻塞 recv
    };
    ::signal(SIGINT , exit);
    ::signal(SIGTERM , exit);

    std::string role {argv[1]};
    if      (role == "bcast") do_bcast();
    else if (role == "recv")  do_recv(argc > 2 ? argv[2] : "B");
    return 0;
}
```

编译（假设已按 u1-l2 构建 `ipc` 库，头文件在 `include/`、库在 `lib/`）：

```sh
g++ -std=c++17 bcast.cpp -Iinclude -Llib -lipc -lpthread -lrt -o bcast
```

### 5.3 操作步骤

1. 开**三个**终端，依次启动：
   ```
   # 终端 B（接收方 1）
   ./bcast recv B
   # 终端 C（接收方 2）
   ./bcast recv C
   # 终端 A（发送方，最后启动）
   ./bcast bcast
   ```
   > 顺序很重要：**先起接收方再起发送方**。否则发送方一开始 `send` 时还没有接收方，消息会被 force_push 覆盖（见 4.2.2）。若想发送方也能先启动，可像 `msg_que` 那样在发送前 `wait_for_recv(1)` 等接收方上线。

2. 观察三个终端的输出。

### 5.4 需要观察的现象

- 终端 B 输出形如 `B recv: hello 1`、`B recv: hello 2` ……
- 终端 C 输出形如 `C recv: hello 1`、`C recv: hello 2` ……
- **两边序号一致且都连续**：说明同一条消息被两个接收方各收到一份。

### 5.5 预期结果与验证点

| 检查点 | 预期 | 对应知识点 |
| --- | --- | --- |
| 两个接收方都打印 | 通过 | 广播语义（4.1） |
| 两边内容、序号相同 | 通过 | `send(std::string)` + `get<char const*>()`（4.2/4.3） |
| Ctrl+C 发送方后，接收方在 500ms 内停止 | 通过 | `disconnect()` 唤醒阻塞 `recv`（4.4） |

### 5.6 进阶尝试（可选）

- 把 `ipc::route` 改成 `ipc::channel`，再开**两个**发送方，观察多生产者是否都能把消息送达所有接收方。
- 把接收方构造改成 `ipc::route ipc {"ipc-bcast"}`（不传 mode，默认 `sender | receiver`），看它能否同时收发。

如果本机环境无法运行三进程，请对每一步标注「待本地验证」，但 5.5 中「广播 = 每个接收方各一份」这一结论由 `route` 的 `trans::broadcast` 定义保证，可独立确认。

---

## 6. 本讲小结

- `route`（单写多读广播）和 `channel`（多写多读广播）都是 `chan<Rp,Rc,Ts>` 的预设；**同名 `name` = 同一条共享内存通道**。
- `chan_wrapper` 构造即连接（RAII），析构即销毁；`mode` 用 `ipc::sender` / `ipc::receiver` 指定角色。
- `send` 有三个重载（指针 / `buff_t` / `std::string`），其中 `send(std::string)` 会多发 1 字节 `\0`；超时会 `force_push` 强制发送，而 `try_send` 超时则直接返回 `false`。
- `recv` 返回 `buff_t`，**第一件事必须 `empty()` 判空**；读数据用 `data()`/`size()`，按类型用 `get<T>()`，要拷贝用 `to_vector()`。
- 默认 `default_timeout = 100ms`（用于 `send`）、`invalid_value`（用于 `recv`，表示无限等待）。
- 优雅退出靠信号处理 + `disconnect()`：`disconnect()` 会唤醒对端阻塞的 `recv`，使其收到空 `buff_t` 而退出。

---

## 7. 下一步学习建议

到这里你已经能用 libipc 收发消息了，但**为什么同名能通信、消息到底存在哪、`recv` 是怎么“等到”消息的**——这些内部机制都还没展开。建议下一步：

- **u2-l1（核心类型与策略标签）**：深入 `def.h` 的 `relat`/`trans`/`wr<>`，彻底搞懂 `route`/`channel` 的模板参数是怎么驱动不同行为的。
- **u2-l2（buffer）**：了解 `buff_t` 的析构器回调与 move 语义，为大消息零拷贝回收打基础。
- **u3-l1（send/recv 端到端链路）**：第一次进入 `ipc.cpp`，跟踪一条消息从 `send` 到 `recv` 在共享内存里走过的完整路径。

想先动手验证本讲的广播语义，就先把第 5 节的小程序跑起来；想知其所以然，就进入 u2。
