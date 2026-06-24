# 进阶 demo：chat 多端聊天与 service/client

## 1. 本讲目标

前面八讲我们已经把 libipc 的「公共 API → 消息数据通路 → 无锁队列 → 共享内存 → 同步原语 → 内存管理 → 并发内部」一层层拆开。本讲换一个视角：**站在「应用层使用 libipc 搭真实业务」的角度**，逐行读懂仓库里两个最完整的进阶 demo，并提炼出可复用的设计套路。

读完本讲，你应该能够：

1. 看懂 `demo/chat` 如何用「两条同名 channel、一个发一个收」实现多端双向群聊，并理解为什么每个客户端会收到自己发的消息、从而需要自我过滤。
2. 掌握用一块共享内存里的原子计数器为每个进程分配**全局唯一 id** 的技巧，并能解释它为什么天然跨进程单调递增。
3. 看懂 `demo/linux_service` 如何用「两条命名通道、角色互换」拼出**请求-响应**模型。
4. 认识 Windows 下 `ipc::prefix{"Global\\"}` 前缀的作用：让运行在 Session 0 的服务与运行在用户会话的客户端共享同一个命名内核对象。

本讲是「专家层」里偏实践的一篇，不再展开底层算法（已在 u3–u7 讲透），而是把已有积木组装成真实可跑的程序。

## 2. 前置知识

本讲默认你已经掌握以下内容（若已生疏可回看对应讲义）：

- **route / channel 与广播语义**（u1-l4、u2-l4）：`ipc::channel` 是「多写多读广播」，一条消息会被**所有在线接收者**收到，广播模式最多 32 个接收者。
- **channel 的角色（mode）**（u2-l3）：一个 channel 对象在连接时用 `ipc::sender` 或 `ipc::receiver` 选定角色；同名即同一通道、构造即连接、析构即销毁（RAII）。
- **send(std::string) 会多发一个 `\0`**（u1-l4）：`send(string)` 实际发送 `size+1` 字节，接收端读字符串时通常要 `size()-1` 截掉末尾的 `\0`。
- **buff_t 接收**（u1-l4、u2-l2）：`recv` 返回 `buff_t`，**第一件事必须 `empty()` 判空**，空表示对端断连；读内容用 `data()` / `get<T>()` / `size()`。
- **ipc::shm::handle**（u5-l1）：RAII 共享内存句柄，`acquire(name, size)` 默认 `create|open`，`get()` 返回映射内存的 `void*`，所有进程映射同一块物理内存。
- **disconnect 唤醒**（u3-l4）：发送端 `disconnect()` 会让对端阻塞的 `recv()` 收到空 `buff_t`，从而优雅退出。

一句话回顾本讲要用的两块积木：

| 积木 | 关键性质 | 来源讲义 |
|------|----------|----------|
| `ipc::channel`（广播） | 一条消息送达所有 receiver；同名即同一通道 | u2-l4 |
| `ipc::shm::handle` | 跨进程共享一段内存；末尾自带引用计数 | u5-l1 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 关键看点 |
|------|------|----------|
| [demo/chat/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp) | 多端聊天主程序 | 两条同名 channel + 共享内存唯一 id + 自我过滤 |
| [demo/linux_service/service/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/service/main.cpp) | Linux 服务端 | 两条命名通道做请求-响应 |
| [demo/linux_service/client/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/client/main.cpp) | Linux 客户端 | 角色互换接收请求、回写响应 |
| [demo/win_service/service/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/service/main.cpp) | Windows 服务端 | `prefix{"Global\\"}` 跨会话 + 健壮 reconnect |
| [demo/win_service/client/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/client/main.cpp) | Windows 客户端 | 同样使用 Global 前缀 |

辅助理解 API 的头文件（本讲引用，不逐行展开）：

- `include/libipc/def.h` —— `prefix` 标签结构。
- `include/libipc/ipc.h` —— `channel` 别名、带 prefix 的构造、`send(string)`、`recv`、`reconnect`。
- `include/libipc/shm.h` —— `shm::handle` 的 `acquire`/`get`。
- `src/libipc/mem/resource.h` —— `make_prefix` 如何拼接出最终共享内存对象名。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块，分别对应四类典型业务模式。

---

### 4.1 chat 双向多端通信

#### 4.1.1 概念说明

群聊的核心矛盾是：每个参与者**既要收、又要发**，而且一条消息要送达**所有人**。

libipc 的一个 channel 对象在连接时只能扮演一个角色（`sender` 或 `receiver`）。那一个进程怎么「既能收又能发」？chat demo 的答案是：**开两条同名 channel，一条当 sender，一条当 receiver**。又因为 `ipc::channel` 是广播（多写多读），任何 sender 发出的消息都会被所有 receiver 收到。

这里有一个容易被忽视的副作用：**广播会把消息也投递回发送者自己的 receiver**。因为「所有 receiver」包括发送者自己那条 `receiver__`。所以群聊里每个人都会收到自己刚发出去的话，需要用消息里的「发送者标识」把它过滤掉——这正是 chat demo 用正则 + id 比对的原因。

#### 4.1.2 核心流程

chat demo 的拓扑是：N 个进程，每个进程持有两条同名（`"ipc-chat"`）channel。

```text
进程 1:  sender__(写) ─┐
进程 2:  sender__(写) ─┼──> 共享内存广播队列 "ipc-chat" ──> 所有 receiver__(读)
进程 3:  sender__(写) ─┘                                         │
进程 1:  receiver__(读) <────────────────────────────────────────┤
进程 2:  receiver__(读) <────────────────────────────────────────┤
进程 3:  receiver__(读) <────────────────────────────────────────┘
```

每个进程内部是「双线程」结构：

```text
主线程:  cin 读输入 -> sender__.send("c0> 你好")   (发)
收线程:  receiver__.recv() -> 解析 -> 自己的? 忽略 : 打印   (收)
退出:    主线程 cin 收到 "q" -> receiver__.disconnect()
         -> 收线程 recv 收到空 buff -> 退出循环
```

广播投递集合可以用一个集合表示：发送者 S 发出消息 m 后，所有当前在线 receiver 的集合

\[ R(m) = \{\, r \mid r \text{ 是 } \texttt{"ipc-chat"} \text{ 的在线 receiver} \,\} \]

且恒有「S 自己的 receiver」\( r_S \in R(m) \)，所以必须自我过滤。

#### 4.1.3 源码精读

先看两条同名 channel 的定义——这是整个群聊的基石：

[demo/chat/main.cpp:21-22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L21-L22) —— 一个进程开两条同名 channel，分别当 sender / receiver。名字 `"ipc-chat"`（见第 12 行常量 `name__`）相同，意味着它们连到同一条底层广播队列。

> 注意：这是**两个独立的 channel 对象**，只是恰好同名。`sender__` 只写不读，`receiver__` 只读不写。

发送侧在主线程里循环读 stdin 并广播（注意它把唯一 id 作为前缀拼进消息）：

[demo/chat/main.cpp:50-56](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L50-L56) —— `std::cin >> buf` 读一行，`sender__.send(id + "> " + buf)` 广播形如 `c0> 你好` 的消息。`send(std::string)` 内部会多发一个 `\0`（见 ipc.h 第 182-184 行 `str.size() + 1`）。

接收侧在独立线程里跑：

[demo/chat/main.cpp:30-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L30-L48) —— `receiver__.recv()` 阻塞等待；收到空 buff 说明被 disconnect、直接退出循环。否则用正则 `"(c\\d+)> (.*)"` 解析出发送者 id（第 1 组）和正文（第 2 组）。**第 37-44 行就是自我过滤**：如果第 1 组等于自己的 id，说明是自己发出去的回声——若是退出命令就跟着退出，否则跳过不打印。

接收端把 buff 还原成字符串时要砍掉末尾那个 `\0`：

[demo/chat/main.cpp:33-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L33-L35) —— `buf.get<char const *>()` 取数据指针，`buf.size() - 1` 截掉 `send(string)` 多发的那一字节。这正是 u1-l4 提到的「`send(string)` 多发 1 字节 `\0`」在接收侧的对应处理。

优雅退出靠 disconnect 唤醒对端线程：

[demo/chat/main.cpp:58-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L58-L60) —— 主线程退出输入循环后调用 `receiver__.disconnect()`，这让本进程 `recv()` 立即返回空 buff，收线程跳出循环；`r.join()` 等收线程结束后再返回。

> 补充：本进程 `disconnect()` 只影响**自己**的 receiver（让自己退出阻塞），并不会断开整条通道——其他进程照常收发。通道真正的销毁发生在最后一个对象的析构（u2-l3 的引用计数清理）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「广播会回声给自己」这一关键现象。

**操作步骤**（默认你已按 u1-l2 用 `LIBIPC_BUILD_DEMOS=ON` 构建出 `chat` 可执行文件）：

1. 开三个终端，**依次**（间隔 1-2 秒，确保 id 递增）各启动一个 `chat`：
   ```bash
   ./chat   # 终端 A，应打印 "c0 is ready."
   ./chat   # 终端 B，应打印 "c1 is ready."
   ./chat   # 终端 C，应打印 "c2 is ready."
   ```
2. 在终端 A 输入 `hello` 并回车。
3. 观察三个终端各自的输出。
4. 在终端 A 输入 `q` 退出，再观察 B、C。

**需要观察的现象**：

- 步骤 2 后：**B 和 C 都打印 `c0> hello`**，但 A 自己**不打印**这条（被自我过滤掉）——这证明广播确实送到了 A 的 receiver，只是被正则 + id 过滤了。
- 步骤 4 后：A 的收线程退出；但 B、C 仍能正常聊天——证明 A 的 disconnect 没有影响通道本身。

**预期结果**：B、C 输出 `c0> hello`，A 无此行输出；A 退出后 B↔C 仍互通。

> 若无法本地运行，记为「待本地验证」，可改为源码阅读：在 main.cpp 第 37 行 `if (std::regex_match(...))` 处设想「如果删掉这个 if 分支」，A 会看到什么？（答：A 会重复打印自己发的 `c0> hello`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 chat demo 必须开**两条** channel（sender + receiver），而不是用一条 channel 同时收发？

> **答案**：一个 channel 对象在连接时只能锁定一个角色（`sender` 或 `receiver`，见 ipc.h 的 `mode`）。要全双工，就得开两个对象；又因为同名即同一通道，两条同名 channel 自然连到同一条广播队列，于是「自己写的」和「别人写的」都进同一个队列，再靠 receiver 这一侧读出来。

**练习 2**：如果把第 38 行的自我过滤 `if (mid.str(1) == id)` 整段删掉，群里会发生什么？

> **答案**：每个客户端会把自己发出去的消息原样打印一次。因为广播投递集合包含发送者自己的 receiver，过滤逻辑是唯一阻止「回声」的屏障。功能仍可用，只是 UI 上自己说的话会显示两次（一次是你输入时、一次是收线程打印）。

---

### 4.2 共享内存唯一 id

#### 4.2.1 概念说明

群聊里每个客户端需要一个**全局唯一、跨进程一致**的标识（`c0`、`c1`、`c2`…）。在单进程里用一个静态计数器就行，但多进程下每个进程的静态变量是各自的、互不相通。

chat demo 的解法非常优雅：**把一个 `std::atomic<std::size_t>` 放进共享内存**。所有进程映射同一块内存，对这个原子量做 `fetch_add`，天然得到跨进程单调递增的唯一 id。第一个进程负责创建（`create|open` 默认模式），后续进程打开同一块。

这是 u5-l1 学过的 `ipc::shm::handle` 的直接应用：`get()` 返回的就是所有进程共享的那块内存的指针。

#### 4.2.2 核心流程

```text
进程 A (先启动):
  handle acquire("__CHAT_ACC_STORAGE__", 8B)  -> 创建新共享内存, 计数器初值 0
  get() -> ptr; ptr->fetch_add(1) -> 返回 0  (id="c0"); 计数器变 1

进程 B (后启动):
  handle acquire("__CHAT_ACC_STORAGE__", 8B)  -> 打开已有共享内存, 同一块物理内存
  get() -> ptr; ptr->fetch_add(1) -> 返回 1  (id="c1"); 计数器变 2
```

id 分配是一个标准的原子自增取旧值操作：

\[ \text{id}_k = \text{fetch\_add}(\text{counter}, 1) \]

`fetch_add` 返回**加之前的值**，保证每个进程拿到的 id 互不相同。用 `memory_order_relaxed` 即可——这里只关心原子性（不丢不重），不依赖它建立与其他操作的 happens-before 顺序。

#### 4.2.3 源码精读

核心就这一个函数，只有两行实质代码：

[demo/chat/main.cpp:16-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L16-L19) —— `static ipc::shm::handle g_shm { "__CHAT_ACC_STORAGE__", sizeof(std::atomic<std::size_t>) }` 创建一块名字为 `"__CHAT_ACC_STORAGE__"`、大小恰好容下一个原子计数器的共享内存（`static` 保证进程内只建一次）。`g_shm.get()` 取出 `void*`，cast 成 `std::atomic<std::size_t>*` 后 `fetch_add(1, relaxed)` 取号。

> 名字 `"__CHAT_ACC_STORAGE__"` 任意取，但**所有进程必须用同一个字符串**才能映射到同一块内存。这和 channel 的「同名即同一通道」是同一个道理。

拿到 id 后拼成可读标识：

[demo/chat/main.cpp:27](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/main.cpp#L27) —— `id = id__ + std::to_string(calc_unique_id())`，其中 `id__ = "c"`（第 14 行），于是得到 `c0`、`c1`、`c2`…。这个 id 既用于消息前缀（供 4.1.3 的正则解析），也用于自我过滤比对。

底层 `shm::handle` 的接口可对照 [include/libipc/shm.h:49-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L49-L82)：构造即 `acquire`（默认 `create | open`，见第 13-16 行常量、第 52 行构造签名），`get()`（第 74 行）返回映射指针。详见 u5-l1。

> 进阶提示：这个计数器是「只增不减」的。如果反复重启 chat 进程，id 会持续变大（c100、c101…），不会回收旧号——对群聊而言无所谓。若你的业务需要回收，得自己加释放逻辑或换 id_pool（见 u8-l3）。

#### 4.2.4 代码实践

**实践目标**：验证「共享内存计数器跨进程递增」。

**操作步骤**：

1. 在 chat demo 的 `main()` 第一行（第 27 行 `id` 计算之后）临时加一句打印：
   ```cpp
   std::cout << "[debug] got unique id raw = " << calc_unique_id() << std::endl;
   ```
   （示例代码，仅供调试，勿提交。）
2. 依次启动 3 个 chat 进程，记下每个打印的 raw id。
3. 全部退出后再启动 1 个，看 raw id 是多少。

**需要观察的现象**：

- 第一次依次启动：raw id 分别是 `0, 1, 2`（如果你按步骤 1 加了**额外**一次调用，会变成 `0,1 / 2,3 / 4,5`，因为多调用了一次——这本身能加深你对 `fetch_add` 返回旧值的理解）。
- 全部退出后再启动：raw id **不会归零**（比如继续是 3）。

**预期结果**：跨进程 id 单调递增且互不重复；进程全部退出后计数器残留在共享内存里，下次启动接着递增。

> 想清零可调用 `ipc::shm::handle::clear_storage("__CHAT_ACC_STORAGE__")`（u5-l1 的按名清理）。记为「待本地验证」若环境不允许运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么这里用 `std::memory_order_relaxed` 而不是 `seq_cst`？

> **答案**：我们只要求「计数器自增不丢不重」（原子性），不要求这次自增与其它读写建立全局顺序。`relaxed` 足够且开销最小。`seq_cst` 在这里只会白白增加成本，不影响正确性。

**练习 2**：如果两个进程**同时**第一次启动（竞态创建），会不会都拿到 id=0？

> **答案**：不会。`acquire` 默认 `create|open`，底层（POSIX 用 `O_CREAT|O_EXCL`、Windows 用 `ERROR_ALREADY_EXISTS`，见 u5-l3/u5-l4）保证只有一方真正「创建」，另一方「打开」同一块内存。随后两次 `fetch_add` 对**同一个**原子量操作，必然返回 0 和 1。

---

### 4.3 service/client 请求-响应

#### 4.3.1 概念说明

聊天是「多对多广播」，但很多业务的形状是「一问一答」：客户端发请求、服务端处理后回响应。libipc 没有内置 RPC，但用**两条命名广播 channel** 就能拼出请求-响应：

- 一条通道「服务端写、客户端读」传**请求**；
- 一条通道「客户端写、服务端读」传**响应**。

关键技巧是：**两条通道在服务端和客户端用的是同一个名字，但角色互换**。这样服务端往「请求通道」里写的，正好被客户端的「请求通道」接收端读到；客户端往「响应通道」里写的，正好被服务端读到。

注意：这个 demo 是「1 服务 ↔ 1 客户」。因为底层是广播，若有多个客户端，**每个客户端都会收到同一个请求**（demo 没有做请求归属/去重）。要做多客户端 RPC，需要自己在消息里带请求 id 并由对应客户端认领。

#### 4.3.2 核心流程

```text
请求通道 "service ipc r":   service(sender) ──写──> client(receiver)
响应通道 "service ipc w":   service(receiver) <──写── client(sender)

服务端循环:  send("Hello, World!") 到 r -> recv(1000ms) 从 w 等响应 -> sleep 3s
客户端循环:  recv() 从 r 收请求      -> send("Copy.")    到 w
```

时序：

```text
service:  send(r,"Hello, World!")  ──┐
                                     ▼ 请求通道 r
client:                          recv(r) 收到 "Hello, World!"
client:   send(w,"Copy.")         ──┐
                                     ▼ 响应通道 w
service:                          recv(w,1000) 收到 "Copy."  -> sleep 3s -> 下一轮
```

#### 4.3.3 源码精读

**服务端**（Linux）定义两条通道，一条写、一条读：

[demo/linux_service/service/main.cpp:13-14](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/service/main.cpp#L13-L14) —— `ipc_r{"service ipc r", ipc::sender}` 服务端在「r」通道上**发送**请求；`ipc_w{"service ipc w", ipc::receiver}` 服务端在「w」通道上**接收**响应。（变量名 `ipc_r`/`ipc_w` 偏向客户端视角命名，容易绕，记住「r 通道走请求、w 通道走响应」即可。）

服务端主循环是「先发后收」：

[demo/linux_service/service/main.cpp:16-30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/service/main.cpp#L16-L30) —— `ipc_r.send("Hello, World!")` 发请求；成功后 `ipc_w.recv(1000)` **带 1000ms 超时**等响应（超时返回空 buff、打印 recv error）；随后 `sleep_for(3s)` 节流，进入下一轮。

**客户端**（Linux）用**同样两个名字**、但角色互换：

[demo/linux_service/client/main.cpp:11-12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/client/main.cpp#L11-L12) —— `ipc_r{"service ipc r", ipc::receiver}` 客户端在「r」通道上**接收**请求；`ipc_w{"service ipc w", ipc::sender}` 客户端在「w」通道上**发送**响应。

客户端循环是「先收后回」：

[demo/linux_service/client/main.cpp:13-25](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/linux_service/client/main.cpp#L13-L25) —— `ipc_r.recv()` 阻塞收请求；收到空 buff（服务端不存在/断连）则 `return -1` 直接退出；否则打印并用 `while(!ipc_w.send("Copy."))` 循环重发响应（发送失败睡 1 秒重试）。

> 对比 chat demo 的优雅断连处理，这里的 Linux 客户端偏脆弱：`recv()` 一旦返回空就 `return -1` 整个进程退出（第 17 行）。如果你的服务端比客户端晚启动，客户端会立即退出。Windows 版（4.4）用 `reconnect` 改进了这一点。

服务端的 `send` 成功不代表客户端已收到——它只表示消息已进入共享内存队列。但因为「r」通道此刻只有一个 receiver（客户端），广播即等价于定向投递，效果上就是「发给客户端」。

#### 4.3.4 代码实践

**实践目标**：跑通请求-响应，并体会「谁先启动」的脆弱性。

**操作步骤**：

1. 构建并先启动**服务端**，再启动**客户端**：
   ```bash
   ./linux_service_service &   # 服务端先起，每 3 秒发一次
   ./linux_service_client      # 客户端收请求、回 "Copy."
   ```
2. 观察双方输出约 10 秒。
3. 全部停掉，**反过来先启动客户端**，观察客户端是否立即退出。

**需要观察的现象**：

- 步骤 2：服务端每 3 秒打印 `send [Hello, World!]` 与 `recv [Copy.]`；客户端每轮打印 `recv: [Hello, World!]` 与 `send [Copy.]`。
- 步骤 3：客户端 `recv()` 立即返回空（没有 sender 在「r」上），打印 `message recv error` 并 `return -1` 退出。

**预期结果**：先服务端后客户端能稳定跑；先客户端后服务端则客户端秒退——这正是 4.4 要用 reconnect 修复的痛点。

> 记为「待本地验证」若环境受限；可改为源码阅读：把 client 第 17 行的 `return -1` 改成 `continue`，并在第 16 行后加 `Sleep`，即可让它等服务端上线（这正是 win 版的思路）。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要**两条**通道，而不是用一条通道同时传请求和响应？

> **答案**：一条广播通道是「同向」的——所有 receiver 收同一份数据。若请求和响应走同一条通道，服务端的 sender 发出的响应，会被自己的 receiver（如果它同时收）以及所有客户端都收到，造成回声与混淆。拆成两条命名通道、角色互换，能让请求单向流到客户端、响应单向流回服务端，方向清晰、互不干扰。

**练习 2**：如果同时启动**两个**客户端，会发生什么？

> **答案**：两个客户端都会从「r」通道收到**同一个**请求（广播），于是都打印 `Hello, World!`、都往「w」通道回 `Copy.`。服务端那侧 `recv(1000)` 只能取到其中一条 `Copy.`（另一条留在队列里或被下一轮消费）。即「1 请求 → N 响应」，语义混乱。多客户端场景必须在消息里带请求 id 做认领。

---

### 4.4 Global 前缀跨会话

#### 4.4.1 概念说明

Windows 把命名内核对象（如共享内存 `CreateFileMapping` 的对象）放在**按会话隔离**的命名空间里。一个 Windows 服务通常运行在 **Session 0**（系统会话，无桌面），而用户程序运行在 **Session 1+**（用户会话）。如果双方都用裸名字 `"service ipc r"`，系统会在各自会话的命名空间里创建**两个不同的对象**，于是永远连不上。

解决办法是给名字加 `Global\` 前缀，把对象放进**会话无关的全局命名空间**——这样 Session 0 的服务和用户会话的客户端就能共享同一个命名对象。这是 u5-l4 讲过的 Windows 共享内存后端特性，在 demo 里通过 libipc 的 `ipc::prefix{"Global\\"}` 透传到底层。

`prefix` 不是 channel 名字的一部分，而是一个独立的**第一参数**（标签），libipc 用它在拼最终共享内存对象名时加在前面。

#### 4.4.2 核心流程

```text
ipc::prefix{"Global\\"}  +  channel 名字 "service ipc r"
        │                            │
        ▼                            ▼
make_prefix 拼接:  "Global\__IPC_SHM__service ipc r"  (及该通道的各子对象名)
        │
        ▼
Windows: CreateFileMapping/OpenFileMapping 用这个带 "Global\" 的名字
        -> 落入全局内核命名空间 -> Session 0 服务 与 用户会话客户端 共享同一对象
```

服务与客户端必须**都用相同的 prefix**，否则仍会落在不同命名空间。

#### 4.4.3 源码精读

`prefix` 的定义极其简单——就是个字符串包装标签：

[include/libipc/def.h:70-72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L70-L72) —— `struct prefix { char const *str; };`。它本身不做任何事，只是把一段前缀字符串「打包」传给 channel。

channel 提供一个**接收 prefix 作为第一参数**的构造重载：

[include/libipc/ipc.h:66-68](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L66-L68) —— `chan_wrapper(prefix pref, char const * name, unsigned mode = ipc::sender)`。所以 `ipc::channel{ipc::prefix{"Global\\"}, "service ipc r", ipc::sender}` 是合法调用：第一参是 prefix、第二参是名字、第三参是角色。

prefix 与名字在内部如何合成最终共享内存对象名：

[src/libipc/mem/resource.h:35-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L35-L37) —— `make_prefix(prefix, ...args)` 用分隔符 `"__IPC_SHM__"` 把 prefix 与后续标签拼接，最终形如 `Global\__IPC_SHM__service ipc r`。channel 连接时所有依赖的共享内存对象（队列、各 waiter、计数器，见 [src/libipc/ipc.cpp:126-129](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L126-L129)）都会带上这个前缀，确保整条通道全部落在全局命名空间。

Windows 服务端（运行在服务线程里）这样建通道：

[demo/win_service/service/main.cpp:167-168](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/service/main.cpp#L167-L168) —— `ServiceWorkerThread` 里用 `ipc::prefix{"Global\\"}` 创建两条通道，其余请求-响应逻辑与 Linux 版完全一致（发 `Hello, World!`、`recv(1000)` 等响应、`Sleep(3000)` 节流，见第 171-188 行）。

Windows 客户端用**相同的 prefix**、角色互换：

[demo/win_service/client/main.cpp:15-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/client/main.cpp#L15-L16) —— 同样 `ipc::prefix{"Global\\"}`，客户端在「r」收、在「w」发。

此外，Windows 客户端用 **reconnect 循环**修复了 4.3 的「谁先启动」痛点：

[demo/win_service/client/main.cpp:17-42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/client/main.cpp#L17-L42) —— 收请求前先 `ipc_r.reconnect(ipc::receiver)`，失败则 `Sleep(1000)` 重试（第 18-21 行），直到服务端那条通道就绪；`recv()` 返回空时 `disconnect()` 后重新进入重试（第 23-26 行），而不是像 Linux 版那样 `return`。这让客户端能容忍「服务后启动 / 中途重启」。`reconnect` 的语义见 u2-l3：在同一对象上按新 mode 重新连接。

> 为什么 Linux demo 不需要 `Global\`？因为 POSIX 共享内存（`shm_open`）创建的是 `/dev/shm/` 下的全局文件，**没有会话隔离**，所有进程天然共享同名对象（见 u5-l3）。会话隔离是 Windows 内核对象命名空间独有的概念。

#### 4.4.4 代码实践

**实践目标**：理解 prefix 的拼接结果与「双方必须一致」的要求（本实践为源码阅读型，因为完整验证需 Windows 服务环境）。

**操作步骤**：

1. 打开 [src/libipc/mem/resource.h:35-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L35-L37)，手算 `make_prefix("Global\\", "service ipc r")` 的结果字符串。
2. 打开 [demo/win_service/service/main.cpp:167](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/service/main.cpp#L167) 与 [demo/win_service/client/main.cpp:15](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/client/main.cpp#L15)，确认两端的 prefix 字符串是否逐字相同。
3. 设想：把客户端的 prefix 改成 `ipc::prefix{"Local\\"}`，服务端仍是 `"Global\\"`，会发生什么？

**需要观察的现象（推理）**：

- 步骤 1：结果应为 `Global\__IPC_SHM__service ipc r`（prefix 与名字间插 `"__IPC_SHM__"` 分隔符）。
- 步骤 2：两端 prefix 均为 `"Global\\"`，逐字一致——这是它们能连上的前提。
- 步骤 3：服务端对象落在全局命名空间、客户端落在本地会话命名空间，**两者是完全不同的对象**，客户端 `recv()` 永远收不到服务端的消息。

**预期结果**：prefix 经 `make_prefix` 与 `__IPC_SHM__` 拼接成全局名；服务端与客户端必须用**完全相同**的 prefix 字符串，否则落在不同命名空间而无法通信。

> 完整端到端验证需在 Windows 上把 service 注册为系统服务（Session 0）再跑客户端，记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`prefix` 是 channel 的「名字」的一部分吗？把 `prefix{"Global\\"}` 放进 channel 构造，会让通道的名字变成 `"Global\\service ipc r"` 吗？

> **答案**：不是，名字仍是 `"service ipc r"`。`prefix` 是独立的标签参数，libipc 内部用 `make_prefix` 把 prefix、`__IPC_SHM__` 分隔符与名字（及各子对象后缀）**拼接成共享内存对象名**，但 `channel::name()` 返回的仍是裸名字。prefix 只影响底层共享内存对象的命名空间归属，不改业务名。

**练习 2**：Linux 版的 service/client 没有用任何 prefix，为什么也能跨「不同启动方式」的进程通信？

> **答案**：POSIX 共享内存用 `/dev/shm/` 下的文件、无会话隔离，所有进程按相同名字即可映射到同一对象（u5-l3）。会话隔离是 Windows 内核对象命名空间的特性，故只有 Windows 跨 Session（服务 ↔ 用户程序）才需要 `Global\` 前缀。

---

## 5. 综合实践

把本讲四块内容串起来，做一个**带用户名的群聊**（在 chat demo 基础上扩展）。要求：

1. **唯一标识**：复用 `calc_unique_id()` 分配一个数字序号（4.2），保证群内不重名。
2. **用户名输入**：启动时让用户 `std::getline(std::cin, nickname)` 输入昵称，组合成唯一标签 `nickname#序号`（例如 `alice#0`）。这样既可读（昵称）又唯一（序号）。
3. **带名广播**：发送格式改为 `nickname#序号> 正文`，仍走两条同名 channel（4.1）。
4. **自我过滤**：接收端正则改为匹配 `([^>]+#\\d+)> (.*)`，第 1 组是完整标签；若等于自己的标签则跳过打印（避免回声），退出命令仍按 `q` 处理。
5. **（可选，进阶）请求-响应扩展**：在群聊之外，再开一对「私聊」命名通道 `pm-<目标标签>`，用 4.3 的请求-响应模型实现两个指定用户间的点对点消息。

**验证方法**：开三个终端，分别以 alice/bob/carol 登录；alice 发 `hi`，bob 和 carol 应看到 `alice#0> hi`，alice 自己不重复看到；输入 `q` 退出不影响他人。

**提示**：

- 自我过滤的「唯一性」必须依赖 `#序号` 那部分（昵称可能撞名），所以正则提取后要拿**完整标签**和自己比，而不是只比昵称。
- 退出时记得 `receiver__.disconnect()` 唤醒自己的收线程，再 `join`，结构照搬 chat demo 第 58-60 行。
- 不要在源码目录里直接改 `demo/chat/main.cpp`（本讲禁止改源码）；请把新程序放到你自己的工作目录，`#include "libipc/ipc.h"` 并链接 `ipc` 目标即可（参考 [demo/chat/CMakeLists.txt](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/chat/CMakeLists.txt) 的写法）。

## 6. 本讲小结

- **chat 群聊**靠「两条同名 channel、一 sender 一 receiver」实现全双工；广播会把消息回声给自己，必须用消息里的发送者 id 做自我过滤。
- **唯一 id** 只需把一个 `std::atomic` 放进 `ipc::shm::handle`，跨进程 `fetch_add` 即得单调递增唯一号；`relaxed` 内存序足矣。
- **请求-响应**用两条命名广播通道、角色互换拼出——服务端「写 r 读 w」、客户端「读 r 写 w」；本质是把两条单向通道对偶起来。
- **Global 前缀**是 Windows 跨 Session（服务 ↔ 用户程序）通信的必备：`ipc::prefix{"Global\\"}` 经 `make_prefix` 拼进底层共享内存对象名，使其落入会话无关的全局命名空间；服务端与客户端的 prefix 必须逐字一致。
- demo 的健壮性差异值得借鉴：Linux 客户端 `recv` 空即退出（脆弱），Windows 客户端用 `reconnect` 重试（健壮），后者能容忍服务端后启动/中途重启。
- 三个 demo 共同印证了 libipc 的用法哲学：**同名即同一通道、构造即连接、析构即销毁**；复杂业务（群聊、RPC、跨会话服务）都是这几条原语的组合。

## 7. 下一步学习建议

- **若想理解群聊里「为什么最多 32 人、第 33 人连不上」**：回看 u2-l4 与 u4-l2 的连接位图 `cc_t`，并动手做「启动 33 个 receiver」实验。
- **若想理解请求-响应里 `send` 的超时/`force_push` 行为**：阅读 u3-l1（send→push→broadcast 链路）与 u3-l4（等待模型与退避）。
- **若要把共享内存唯一 id 升级为「可回收的票据」**：学习 u8-l3 的 `id_pool`（数组当链表的 O(1) 分配/回收）。
- **若想在 Windows 上做更稳健的跨会话服务**：结合 u6-l2/u8-l2 的健壮互斥量，处理服务进程崩溃后共享内存对象残留的清理（`clear_storage`）。
- **测试与扩展**：阅读 u8-l5，了解如何用 gtest 测试 channel，以及 `ipc.cpp` 末尾的模板实例化门——它会告诉你新增一种通道策略需要做哪些工作。
