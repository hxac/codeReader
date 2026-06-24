# ipc.cpp 全景：send/recv 端到端链路

## 1. 本讲目标

前两单元（u2）我们停留在「公共 API 头文件」层面：`chan_wrapper`、`buff_t`、句柄 `handle_t`。本讲是第一次**钻进库的实现层** `src/libipc/ipc.cpp`——这是整个 libipc 最核心、也最长的一个文件。

读完本讲，你应该能够：

1. 说出 `ipc.cpp` 的整体分层（消息类型 → 连接头 → 队列生成器 → 桥接层 → `chan_impl` 转发 → 显式实例化门）。
2. 解释 `conn_info_head` 这个「每个连接的元数据盒子」里装了什么：三类 `waiter`、共享内存句柄 `acc_h_`、连接信息号 `cc_id_`。
3. 看懂 `send(data)` 一条消息是怎么从 `chan_wrapper::send` 一路转发到 `queue::push`，最后发出 `rd_waiter_.broadcast()` 唤醒接收方的；以及 `recv` 的反向路径。
4. 理解 `detail_impl<Policy>` 这一层为什么存在：它把对外模板 `chan_impl<Flag>` 和共享内存里的 `queue` 桥接起来。

本讲**只看主干**（端到端的调用链与数据结构骨架），消息分片重组、大消息外存、等待退避这三个专题分别留给 u3-l2、u3-l3、u3-l4 深入。本讲遇到这些下钻点会点到为止、并标明「详见后续讲义」。

## 2. 前置知识

在进入源码前，先用一句话复习前置讲义建立的关键认知（不重复展开）：

- **同名即同一通道**：`ipc::channel{"ipc", ipc::sender}` 与另一进程里的 `ipc::channel{"ipc", ipc::receiver}` 靠名字字符串 `"ipc"` 连到同一段共享内存（u1-l4）。
- **`route`/`channel` 是同一模板的预设**：`chan<Rp,Rc,Ts>` 由策略标签 `wr<Rp,Rc,Ts>` 驱动，`route` = 单写多读广播、`channel` = 多写多读广播（u2-l1）。
- **句柄即 `void*`**：对外只暴露 `handle_t = void*` 不透明指针，真正干活的对象藏在 `.cpp` 里（PIMPL，u2-l3）。
- **广播 32 接收者上限**来自连接位图 `cc_t = uint32`（u2-l4）。

本讲会大量出现一个新概念需要先建立直觉：**「连接信息（conn_info）」对象**。

> 一条逻辑通道（比如名字叫 `"ipc"`）背后，在**每个进程**里都有一个对应的 C++ 对象——它持有这条通道在本进程内的全部运行时状态：共享内存句柄、等待器、本进程的消息计数等。这个对象就是 `conn_info_t`。`handle_t` 这个 `void*` 指针，指向的就是它。

换句话说：**句柄 ≈ 指向本进程内连接信息对象的指针**。这是理解本讲所有代码的钥匙。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲重点 |
| --- | --- | --- |
| `src/libipc/ipc.cpp` | 库的核心实现（853 行） | 几乎全部内容都在本讲 |
| `src/libipc/policy.h` | 把策略标签 `Flag` 映射成具体的元素数组类型 | `policy::choose`、`elems_t` |
| `src/libipc/queue.h` | 共享内存上队列的封装（`queue_conn`/`queue_base`/`queue`） | `push`/`pop` 如何委托给 `elems_` |
| `src/libipc/waiter.h` | 等待/通知原语（封装 condition+mutex） | `wait_if`/`broadcast` |
| `src/libipc/circ/elem_array.h` | 循环元素数组（无锁队列容器） | `push`/`pop` 转发到 `head_` |
| `src/libipc/circ/elem_def.h` | 连接头 `conn_head`、连接位图 | `connections()`、`conn_count()` |
| `include/libipc/ipc.h` | 公共 API（`chan_impl` 声明、`chan_wrapper`） | `chan_wrapper::send/recv` 的转发 |

> 提示：`queue.h`、`waiter.h`、`elem_array.h`、`elem_def.h` 在 u3 后续讲义与 u4、u6 还会反复出现。本讲只取它们在主链路上**被调到的那一行**，不展开内部算法。

## 4. 核心概念与源码讲解

### 4.1 ipc.cpp 文件结构：从上到下六层

#### 4.1.1 概念说明

打开 `ipc.cpp`，第一眼会感觉「很长、很乱」。但它其实有非常清晰的**自下而上六层结构**，理解了这个结构，后面所有讲解都能对号入座：

1. **基础类型层**：消息格式 `msg_t`、辅助小函数（如 `make_cache`）。
2. **全局设施层**：跨进程的原子计数器 `cc_acc`、大消息外存相关的 `chunk_*` 函数（u3-l3 详讲）。
3. **连接头层**：`conn_info_head`（本讲重点 4.2）。
4. **队列生成器层**：`queue_generator`，在连接头上再叠一个 `queue` 成员。
5. **桥接实现层**：`detail_impl<Policy>`，所有 API 的真正实现（本讲重点 4.3、4.4）。
6. **公共转发层**：`ipc` 命名空间里的 `chan_impl<Flag>::xxx` 一组模板函数，它们**只做转发**，最后是显式实例化门。

这六层最关键的设计是：**第 1–5 层全部位于匿名命名空间（`namespace { ... }`），对外不可见**；只有第 6 层在 `namespace ipc` 里，通过 `chan_impl` 模板把内部实现安全地暴露出去。

#### 4.1.2 核心流程

用一个自下而上的视角看代码组织（行号为 `ipc.cpp` 内）：

```
[匿名命名空间 32–748 行]
  ├─ msg_t 消息结构                  (37–64)
  ├─ make_cache 辅助函数              (66–76)
  ├─ cc_acc 全局计数器访问            (78–94)   ── u3-l2 用
  ├─ cache_t 分片缓存                 (96–110)  ── u3-l2 用
  ├─ conn_info_head 连接头            (112–175) ★ 4.2
  ├─ chunk_* / acquire_storage ...    (177–376) ── u3-l3 用
  ├─ wait_for 模板                    (378–391) ── 4.4 / u3-l4
  ├─ queue_generator                  (393–439) ★ 4.3
  └─ detail_impl<Policy>              (441–743) ★ 4.3 / 4.4
[ipc 命名空间 750–852 行]
  ├─ policy_t 别名                    (745–746)
  ├─ chan_impl<Flag>::init_first...   (752–844) ★ 4.3（转发）
  └─ 显式实例化门                     (846–850) ★ 4.3
```

#### 4.1.3 源码精读

匿名命名空间从第 32 行开始，把所有内部实现藏起来：

[src/libipc/ipc.cpp:32-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L32-L35) —— `namespace {` 开启匿名命名空间；`msg_id_t`/`acc_t` 是消息 id 与原子计数器的类型别名。

文件结尾的**显式实例化门**是结构上最重要的一行（u2-l1 已点过名，u8-l5 会详讲）：

[src/libipc/ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850) —— 这里只对 3 种 `wr` 组合显式实例化 `chan_impl`（单写单读单播、route、channel），其余组合即便模板代码存在也链接失败。

> 这就是为什么本讲能在脑子里建立「一条主链路」：无论你用 `route` 还是 `channel`，走的都是**同一套 `detail_impl` 代码**，只是 `Policy` 不同导致底层无锁算法（u4）的分派不同。

#### 4.1.4 代码实践

**实践目标**：建立对 `ipc.cpp` 的「块状」认知，能在 5 秒内定位任意一个 API 的实现。

**操作步骤**：

1. 打开 `src/libipc/ipc.cpp`，只看左侧的 `struct`/`template`/`namespace` 关键字，不要读函数体。
2. 把第 4.1.2 节那张结构图抄一遍，在每个名字旁边标上它的起始行号。
3. 跳到文件末尾第 846–850 行，确认库只实例化了 3 种 `chan_impl`。

**需要观察的现象**：你会看到第 847、848 行被注释为 `// TBD`（单播的多消费者版本）。

**预期结果**：你能回答「`detail_impl` 在第几行？」「显式实例化门为什么只有 3 行？」这两个问题，就达成本实践。

#### 4.1.5 小练习与答案

**练习 1**：`ipc.cpp` 里为什么要把 `detail_impl` 放在匿名命名空间，而把 `chan_impl` 放在 `namespace ipc` 里？

> **参考答案**：`chan_impl` 是头文件 `ipc.h` 里声明、面向用户的模板，必须在 `namespace ipc` 中定义才能被链接器按外部符号找到；`detail_impl` 是纯内部实现细节，放匿名命名空间可以避免符号污染、保证不同翻译单元不会冲突，也向用户隐藏了真正的数据结构（配合 PIMPL 的 `handle_t = void*`）。

**练习 2**：文件末尾显式实例化了哪三种组合？被注释掉的是哪两种？

> **参考答案**：实例化了 `single/single/unicast`、`single/multi/broadcast`（route）、`multi/multi/broadcast`（channel）共 3 种；注释为 TBD 的是 `single/multi/unicast` 与 `multi/multi/unicast` 这两种单播多消费者组合。

---

### 4.2 conn_info_head 与 cc_id：每个连接的元数据盒子

#### 4.2.1 概念说明

如前置知识所说，**句柄 `handle_t` 指向一个「连接信息对象」**。这个对象的最底层基类就是 `conn_info_head`。可以把它理解成一个盒子，盒子里装着「这条通道在本进程运行起来所需的全部共享资源句柄」：

- **三类 `waiter`**：等待/通知原语，分别管「连接确认」「写满」「读空」三件事。
- **一个共享内存句柄 `acc_h_`**：指向一段跨进程共享的原子计数器。
- **名字信息**：`prefix_`、`name_`，用于拼出共享内存对象名。
- **连接信息号 `cc_id_`**：本连接对象的唯一编号。

为什么要把这些东西集中放一起？因为它们的**生命周期完全一致**：通道构造时一起打开、析构时一起关闭。集中管理还能保证「打开顺序、关闭顺序」不会出错。

#### 4.2.2 核心流程

`conn_info_head` 的初始化由 `init()` 完成，它做四件事：

```
init()
  ├─ 若 cc_waiter_ 无效 → open("...CC_CONN__<name>")   连接确认等待器
  ├─ 若 wt_waiter_ 无效 → open("...WT_CONN__<name>")   写满等待器（队列满时阻塞发送方）
  ├─ 若 rd_waiter_ 无效 → open("...RD_CONN__<name>")   读空等待器（队列空时阻塞接收方）
  ├─ 若 acc_h_  无效 → acquire("...AC_CONN__<name>", sizeof(acc_t))  共享原子计数器
  └─ 若 cc_id_ == 0 → 从全局计数器 fetch_add 取一个新号
```

三类 `waiter` 的名字前缀 `CC_CONN__`/`WT_CONN__`/`RD_CONN__` 是固定的，配合 `prefix_` 和 `name_` 拼成唯一的共享内存对象名——这就是「同名通道」能跨进程对上号的命名约定（详见 u6-l4）。

`cc_id_` 是本讲需要特别注意的概念，它**容易和连接位图 `cc_t` 混淆**：

| 概念 | 类型 | 来源 | 作用 |
| --- | --- | --- | --- |
| `conn_info_head::cc_id_` | `msg_id_t`（uint32） | 全局原子计数器 `acc_t` 单调递增 | 标识「是哪个连接对象发出的消息」，用于**过滤自己发的消息** |
| `connected_id()`（连接位） | `circ::cc_t`（uint32 的单个 bit） | `conn_head::connect()` 位运算抢位 | 标识「本接收者在 32 位位图中的第几位」，用于**广播读计数** |

> 简记：`cc_id_` 是**「身份证号」**（人人不同、永不回收）；连接位是**「座位号」**（共 32 个、断开后可复用）。二者完全独立。

#### 4.2.3 源码精读

`conn_info_head` 的定义与成员：

[src/libipc/ipc.cpp:112-123](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L112-L123) —— 五个成员：`prefix_`、`name_`、`cc_id_`、三个 `waiter`、共享内存句柄 `acc_h_`。注意 `cc_id_` 的注释 `// connection-info id`，强调它是「连接信息号」而非连接位。

`init()` 打开三类等待器并取号：

[src/libipc/ipc.cpp:125-143](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L125-L143) —— 前四行用 `make_prefix` 拼名并 `open`/`acquire`；第 130 行若 `cc_id_ != 0` 直接返回（避免重复取号）；第 133–142 行通过 `cc_acc(prefix_)` 拿到跨进程共享的原子计数器，`fetch_add(1)` 取一个新号，并跳过 0（因为 0 在本库表示「无效」）。

`cc_id_` 被使用的两处（一写一读，构成自过滤）：

- **发送时**（写入消息头）：见 4.4 节 `send` 把 `info->cc_id_` 塞进 `msg_t`（[ipc.cpp:598](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L598)）。
- **接收时**（比较过滤）：[src/libipc/ipc.cpp:655-657](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L655-L657) —— `if ((inf->acc() != nullptr) && (msg.cc_id_ == inf->cc_id_)) continue;`，即「这条消息是自己这个连接对象发出的，跳过」。

> 典型场景：聊天客户端（u8-l4 的 chat demo）用**同一个** channel 对象既发又收。它发出的广播会回到自己这里，靠这一行过滤掉，避免回声。

`recv_cache()` 提供 thread_local 的分片重组缓存（u3-l2 详讲）：

[src/libipc/ipc.cpp:171-174](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L171-L174) —— 每个**线程**一份 `unordered_map<msg_id_t, cache_t>`，因为分片重组是「读了一半」的中间状态，必须按线程隔离。

#### 4.2.4 代码实践

**实践目标**：亲手追踪 `cc_id_` 的「取号 → 写入消息头 → 接收过滤」闭环，确认它和连接位是两回事。

**操作步骤**：

1. 在 `ipc.cpp` 搜索 `cc_id_`（注意带下划线，避免误中 `cc_id`），数一下出现次数与位置。
2. 对照第 4.2.2 节的表格，把每处出现归类为「身份证号」还是「座位号」。
3. 单独搜索 `connected_id`（无下划线结尾），看它出现在哪里——应当只在 `recv` 的大消息回收路径（ipc.cpp:679）用到。

**需要观察的现象**：`cc_id_` 在 `conn_info_head::init()` 取值、在 `send` 写入消息、在 `recv` 比较；`connected_id()` 则来自 `queue_conn`，是大消息引用计数回收用的座位号。

**预期结果**：你能用一句话向别人解释「为什么 libipc 里有两个名字很像的 id」，就达成本实践。这一区分在 u3-l3（大消息引用计数）会再次用到。

#### 4.2.5 小练习与答案

**练习 1**：`conn_info_head` 里为什么需要**三类** `waiter`，而不是一个？

> **参考答案**：因为要等待的事件本质不同：`cc_waiter_` 等「有没有接收者连上来」（连接确认），`wt_waiter_` 等「队列腾出空位」（发送方在队列满时阻塞），`rd_waiter_` 等「有新消息到了」（接收方在队列空时阻塞）。三者的唤醒时机和唤醒方都不同，分开才能精准唤醒、互不干扰（u3-l4 详讲退避策略）。

**练习 2**：`cc_id_` 为什么不允许等于 0？

> **参考答案**：因为代码里多处用 0 表示「无效 / 尚未取号」的哨兵（例如 `init()` 第 130 行用 `cc_id_ != 0` 判断是否已取号）。如果允许 0 作为合法编号，就无法区分「还没取号」和「恰好取到 0」，所以第 139–142 行在取到 0 时再 `fetch_add` 一次跳过它。

---

### 4.3 detail_impl 桥接层：把对外模板接到共享内存队列上

#### 4.3.1 概念说明

回顾调用链的「两端」：

- **上端**是头文件里的 `chan_impl<Flag>`——一组**纯静态函数声明**（u2-l3 见过），用户通过 `chan_wrapper::send` 间接调用它。
- **下端**是共享内存里的 `queue`——真正存放消息的无锁循环队列（u4 详讲）。

这俩中间隔着一道「类型」鸿沟：`chan_impl` 的模板参数是策略标签 `Flag`（如 `wr<single,multi,broadcast>`），而 `queue` 需要的是「具体元素数组类型」。**`detail_impl<Policy>` 就是填平这道鸿沟的桥**。它做了三件事：

1. 用 `Policy` 推导出 `queue_t`（具体的队列类型）和 `conn_info_t`（具体的连接信息类型）。
2. 提供一对小工具 `info_of(h)` / `queue_of(h)`，把 `void*` 句柄安全地 cast 回真实类型。
3. 把所有 API（connect/send/recv/...）一次性实现完，供 `chan_impl` 转发。

为什么不让 `chan_impl` 直接干活？因为 `chan_impl` 在头文件里、要被用户 include，把实现塞头文件会暴露内部结构、拖慢编译。用一个桥把实现集中在 `.cpp`，既保持头文件干净，又能在文件末尾用显式实例化门精确控制「编译哪几种组合」。

#### 4.3.2 核心流程

类型推导的链条（自上而下）：

```
Flag  (策略标签，如 wr<single,multi,broadcast>)
  │
  │  policy_t<Flag> = policy::choose<circ::elem_array, Flag>      (ipc.cpp:746)
  ▼
Policy
  │  Policy::elems_t<sizeof(T), alignof(T)>
  │    = circ::elem_array<prod_cons_impl<flag_t>, DataSize, AlignSize>   (policy.h:21)
  ▼
queue<msg_t, Policy>                                              (queue_generator:398)
```

而 `Policy` 是怎么从 `Flag` 来的？关键就在 `policy.h` 这个只有 25 行的小文件：

[src/libipc/policy.h:16-22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L16-L22) —— `choose` 把 `Flag` 喂给 `prod_cons_impl`，再包进 `circ::elem_array`，得到元素类型 `elems_t`。这一行是「策略标签 → 具体无锁队列算法」的接驳点（`prod_cons_impl` 的四套算法在 u4 展开）。

#### 4.3.3 源码精读

`detail_impl` 的类型别名与句柄转换工具：

[src/libipc/ipc.cpp:444-455](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L444-L455) —— `info_of(h)` 把 `void*` cast 成 `conn_info_t*`；`queue_of(h)` 取出其中的 `que_` 成员，并处理空指针。整段库代码里，凡是从句柄取队列，走的都是这两个函数。

`queue_generator` 在 `conn_info_head` 上叠一个 `queue`：

[src/libipc/ipc.cpp:400-415](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L400-L415) —— `conn_info_t` 继承 `conn_info_head` 并新增 `que_`；它的 `init()` 先调基类 `init()` 打开三类 waiter 与计数器，再 `que_.open(...)` 打开名为 `QU_CONN__<name>__<DataSize>__<AlignSize>` 的共享内存（队列本体）。注意名字里带了 `DataSize`/`AlignSize`——不同消息尺寸的队列会落到不同的共享内存段。

转发层（`chan_impl` 的每个函数都只有一行，转给 `detail_impl`）：

[src/libipc/ipc.cpp:826-829](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L826-L829) —— `chan_impl<Flag>::send` 的全部实现就是 `return detail_impl<policy_t<Flag>>::send(h, data, size, tm);`。可以扫一眼 752–844 行，每个函数都是这个模式：**剥一层壳、转发**。

#### 4.3.4 代码实践

**实践目标**：亲手走通「转发」链条，确认 `chan_wrapper::send` 最终落到的就是 `detail_impl::send`。

**操作步骤**：

1. 打开 `include/libipc/ipc.h` 第 176–178 行，看 `chan_wrapper::send(void const*, size_t, tm)` 调的是 `detail_t::send(h_, data, size, tm)`，其中 `detail_t = chan_impl<Flag>`。
2. 跳到 `ipc.cpp` 第 826–829 行，确认 `chan_impl<Flag>::send` 转发到 `detail_impl<policy_t<Flag>>::send`。
3. 再看 `ipc.cpp` 第 745–746 行 `policy_t` 的定义，回答：`policy_t<Flag>` 用到了 `policy.h` 里的哪个结构？

**需要观察的现象**：从用户代码 `ipc.send(...)` 到 `detail_impl::send`，中间正好经过两次「剥壳转发」：`chan_wrapper` → `chan_impl` → `detail_impl`。

**预期结果**：你能画出 `Flag` → `policy_t<Flag>` → `detail_impl<policy_t<Flag>>` 这条类型推导链。这是后续读懂 connect/recv 实现的前提。

#### 4.3.5 小练习与答案

**练习 1**：`info_of(h)` 和 `queue_of(h)` 为什么都用 `static_cast`/成员访问而不做动态类型检查？

> **参考答案**：因为句柄 `h` 的真实类型在**构造时**就已确定（`connect` 里 `*ph = ipc::mem::$new<conn_info_t>(...)`，见 ipc.cpp:462），且 `detail_impl<Policy>` 是按 `Policy` 实例化的，同一条通道两端用的是同一个 `Policy`，类型是封闭可知的。PIMPL 的前提就是「创建者知道真实类型」，所以静态 cast 是安全的，也省去了虚函数/RTTI 的开销。

**练习 2**：队列共享内存的名字为什么要把 `DataSize` 和 `AlignSize` 拼进去？

> **参考答案**：因为 `msg_t` 的尺寸由 `DataSize`（即 `data_length=64`）决定，元素数组 `elem_array` 的内存布局随之而定。把尺寸拼进名字，可以保证「不同消息尺寸的队列互不覆盖」，也便于 `clear_storage` 按名字精确清理（见 ipc.cpp:422–428）。

---

### 4.4 send/recv 主链路：一条消息的往返

#### 4.4.1 概念说明

前面三节都是「骨架」，本节终于来到血肉：**一条消息从 `send` 到 `recv` 的完整往返**。这是本讲的核心。

先建立整体直觉。发送方的主链路：

```
chan_wrapper::send(data,size)
  └─ chan_impl<Flag>::send            (转发)
       └─ detail_impl::send(h,...,tm) (4 参重载，构造一个 try_push 闭包)
            └─ detail_impl::send(F&& gen_push, h, data, size)  (模板，真正干活)
                 ├─ 取 conns = elems->connections()   若 0 则无接收者，失败
                 ├─ 取 msg_id = acc->fetch_add(1)
                 ├─ 大消息? → acquire_storage 外存 + 1 片    (u3-l3)
                 └─ 否则按 data_length 分片，每片:
                      try_push(remain, data_ptr, size)
                        └─ wait_for(wt_waiter_, [&]{return !que->push(...);}, tm)
                             └─ 超时则 que->force_push(...)   (u3-l2)
                        └─ info->rd_waiter_.broadcast()   ★ 唤醒接收方
```

接收方的反向链路：

```
chan_wrapper::recv(tm)
  └─ chan_impl<Flag>::recv             (转发)
       └─ detail_impl::recv(h, tm)
            └─ 循环:
                 wait_for(rd_waiter_, [&]{ ... return !que->pop(msg); }, tm)
                 info->wt_waiter_.broadcast()   ★ 唤醒被 wt_waiter_ 阻塞的发送方
                 if (msg.cc_id_ == inf->cc_id_) continue;  过滤自己
                 ── 大消息? → find_storage + 回收闭包 (u3-l3)
                 ── 否则分片重组 (recv_cache)        (u3-l2)
```

注意两个对称的「唤醒」：

- 发送方 push 完后 `rd_waiter_.broadcast()` ——叫醒「等消息」的接收方。
- 接收方 pop 完后 `wt_waiter_.broadcast()` ——叫醒「等空位」的发送方。

这两句就是整个通道「活起来」的脉搏。

#### 4.4.2 核心流程

**`detail_impl::send` 有两个重载**，分工明确：

- **模板版** `send(F&& gen_push, h, data, size)`（ipc.cpp:526–589）：干所有脏活——校验、取 id、分片、调 `try_push`。它不知道「push 失败要不要强制」，所以把「如何 push」抽成参数 `gen_push`。
- **四参版** `send(h, data, size, tm)`（ipc.cpp:591–611）：构造那个 `gen_push` 闭包，闭包里封装了「先正常 push，超时就 force_push」的策略。

这种「把策略做成闭包传进去」的写法，让 `send` 和 `try_send`（ipc.cpp:613–627）能共用同一个模板：`try_send` 的闭包超时直接返回 false、不 force_push。

分片的数量由消息大小与槽位 `data_length`（=64 字节，u2-l1）决定：

\[ N_{frag} = \left\lceil \frac{size}{data\_length} \right\rceil \]

例如 200 字节消息、`data_length=64`，会被切成 4 片（64×3 + 8）。分片与重组的细节属于 u3-l2，本讲只确认「主链路里确实有这个循环」。

#### 4.4.3 源码精读

发送主链路的核心——模板版 `send`（节选关键校验与分片循环）：

[src/libipc/ipc.cpp:546-559](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L546-L559) —— 先 `que->elems()->connections(...)` 拿到当前接收者位图（`elem_def.h` 的 [connections()](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L48)），若为 0 说明没人接收、直接失败；再用全局计数器 `acc->fetch_add(1)` 算一个新 `msg_id`；最后 `gen_push(inf, que, msg_id)` 得到分片用的 `try_push` 闭包。

[src/libipc/ipc.cpp:560-588](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L560-L588) —— 大消息走 `acquire_storage` 外存（u3-l3）；否则按 `data_length` 分片，循环调 `try_push(remain, ptr, size)`。注意每片的 `remain` 是「**本片之后还剩多少**」（负值表示这是最后一片且未填满），这是 u3-l2 重组的依据。

`try_push` 闭包里的「正常 push → 超时 force_push → 唤醒接收方」三段式：

[src/libipc/ipc.cpp:593-609](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L593-L609) —— `wait_for(info->wt_waiter_, [&]{ return !que->push(...); }, tm)` 反复尝试 push、队列满就阻塞在 `wt_waiter_` 上；超时则 `que->force_push(...)` 强制挤入（附带 `clear_message` 清理被挤掉的消息）；最后 **`info->rd_waiter_.broadcast();`**（第 607 行）唤醒所有等消息的接收方。

`queue::push` 一路委托到无锁队列（u4 详讲内部算法）：

[src/libipc/queue.h:184-190](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L184-L190) —— `queue_base::push` 把「构造消息 + 放进槽位」拆成两步：先调 `prep(p)` 预检，通过才 `::new (p) T(...)` 定位构造，再交给 `elems_->push`。

[src/libipc/circ/elem_array.h:123-126](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L123-L126) —— `elem_array::push` 再转给 `head_.push(...)`，即 `prod_cons_impl::push`（u4 的无锁算法入口）。

接收主链路：

[src/libipc/ipc.cpp:642-657](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L642-L657) —— `wait_for(inf->rd_waiter_, [que,&msg,&h]{ ... return !que->pop(msg); }, tm)` 反复尝试 pop、队列空就阻塞在 `rd_waiter_` 上（注意谓词里还顺手处理了「断线重连」）；pop 成功后 **`inf->wt_waiter_.broadcast();`**（第 654 行）唤醒被队列满阻塞的发送方；接着第 655–657 行用 `cc_id_` 过滤自己发的消息。

[src/libipc/queue.h:200-208](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L200-L208) —— `queue_base::pop` 同样委托给 `elems_->pop`，并移动构造出 `item`。

最后，支撑上述「反复尝试 + 阻塞」的是 `wait_for` 模板（u3-l4 详讲退避）：

[src/libipc/ipc.cpp:378-391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L378-L391) —— `tm==0` 时只试一次不阻塞；否则循环里先 `ipc::sleep(k, ...)` 做若干次忙等/退避，达到阈值才在 `waiter.wait_if(pred, tm)` 上真正阻塞。

而 `waiter` 的 `broadcast`/`wait_if` 定义在 [waiter.h:64-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L64-L88)（u6-l4 详讲其 condition+mutex 内部）。

#### 4.4.4 代码实践

**实践目标**：定位发送链路上「两个 broadcast」各自唤醒谁，并解释 `wait_for` 为何不会一上来就阻塞。

**操作步骤**：

1. 在 `ipc.cpp` 搜索 `rd_waiter_.broadcast` 与 `wt_waiter_.broadcast`，各应在 `send`（第 607 行）与 `recv`（第 654 行）出现一次。
2. 跟着 `rd_waiter_` 往回找：它在 `recv` 的 `wait_for`（第 645 行）里被 `wait_if` 阻塞，在 `send` 的第 607 行被 `broadcast` 唤醒——确认这是一对「生产者唤醒 / 消费者阻塞」。
3. 读 `wait_for`（第 378–391 行）的循环结构，注意 `ipc::sleep(k, ...)` 里的 `k` 是递增的。

**需要观察的现象**：`wait_for` 的循环里，`k` 从 0 开始递增，只有当 `sleep` 内部决定「退避够了」才会真正调 `waiter.wait_if` 阻塞。

**预期结果**：你能回答「为什么 libipc 延迟低」——因为短时争用走的是自旋/退避（`sleep(k)`），只有持续抢不到才落到信号量阻塞。这正是 u1-l1 提到的「先自旋后阻塞」哲学在源码里的落点（退避阈值在 u3-l4 / u6-l1 展开）。

#### 4.4.5 小练习与答案

**练习 1**：`send`（四参版）和 `try_send` 共用同一个模板版 `send(F&& gen_push, ...)`，二者唯一的区别在哪？

> **参考答案**：区别只在他们传入的 `gen_push` 闭包对「push 超时」的处理：`send` 的闭包在 `wait_for` 超时后会调 `que->force_push(...)` 强制发送并返回 true（[ipc.cpp:600-606](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L600-L606)）；`try_send` 的闭包超时直接返回 false、不强制（[ipc.cpp:616-622](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L616-L622)）。这正对应 u1-l4 讲过的「send 超时走 force_push、try_send 超时返回 false」。

**练习 2**：如果 `connections()` 返回 0（没有任何接收者），`send` 会怎样？

> **参考答案**：在 [ipc.cpp:547-550](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L547-L550) 直接打日志 `there is no receiver on this connection.` 并返回 false，不会进入分片循环、也不会 broadcast。所以「先启动接收方再发送」是这个库的基本使用前提。

**练习 3**：为什么 `recv` 在 pop 成功后要 `wt_waiter_.broadcast()`？

> **参考答案**：因为发送方在队列满时会被阻塞在 `wt_waiter_` 上（见 send 闭包第 595 行）。接收方 pop 走一个消息就意味着队列腾出了一个空位，必须 broadcast 唤醒可能正在等待空位的发送方继续 push，否则发送方会一直阻塞、形成「队列有空位却没人推进」的死等。

---

## 5. 综合实践

**任务**：画出 `ipc.send(data, size)` 从 `chan_wrapper::send` 到 `queue` push、再到 `rd_waiter_.broadcast()` 的**函数调用时序图**。这是本讲对应的实践任务，用来把四节内容串成一条线。

**操作步骤**：

1. 准备一张纸或任意画图工具，画出「发送方进程」和「接收方进程」两条竖直的生命线（时间从上往下）。
2. 在**发送方**生命线上，按调用顺序从上到下依次标出下列函数（每条都注明文件:行号）：
   - `chan_wrapper::send(void const*, size_t, tm)` — `include/libipc/ipc.h:176`
   - `chan_impl<Flag>::send` — `src/libipc/ipc.cpp:827`
   - `detail_impl::send(h, data, size, tm)`（4 参） — `ipc.cpp:591`
   - `detail_impl::send(F&& gen_push, h, data, size)`（模板） — `ipc.cpp:527`
   - `connections()` / `acc->fetch_add` — `ipc.cpp:546-558`
   - `try_push` 闭包 — `ipc.cpp:593`
   - `wait_for(wt_waiter_, ...)` — `ipc.cpp:595`
   - `que->push(...)` — `queue.h:185` → `elem_array.h:124` → `prod_cons_impl::push`
   - `rd_waiter_.broadcast()` — `ipc.cpp:607`
3. 从发送方的 `rd_waiter_.broadcast()` 画一条**跨进程的横向箭头**指向接收方生命线，标注「唤醒」。
4. 在**接收方**生命线上对应位置标出：`recv` → `wait_for(rd_waiter_, ...)`（`ipc.cpp:645`，此前一直阻塞在这里）→ 被唤醒 → `que->pop(msg)` → `wt_waiter_.broadcast()`（`ipc.cpp:654`）→ 自过滤 / 重组。
5. 再从接收方的 `wt_waiter_.broadcast()` 画一条横向箭头指回发送方，标注「队列腾位，唤醒等待空位的发送方」。

**需要观察的现象**：整张图应呈现两个对称的横向「唤醒」箭头（rd 方向、wt 方向），它们正是通道双向解阻塞的关键。

**预期结果**：你得到一张能独立向他人讲解的时序图，覆盖 `chan_wrapper → chan_impl → detail_impl → queue → elem_array → waiter.broadcast` 的完整往返链。如果某一段画不出来，回到对应章节（4.3 的转发、4.4 的链路）重读。

> 说明：本实践为源码阅读型实践，不需要运行程序；如想运行验证，可参考 u1-l2 启动 `send_recv` demo 并对照本图观察。

## 6. 本讲小结

- `ipc.cpp` 是分层组织的基础类型 → `conn_info_head` → `queue_generator` → `detail_impl` → `chan_impl` 转发 → 显式实例化门；前 5 层在匿名命名空间，只有 `chan_impl` 对外暴露。
- **句柄 `handle_t` 指向一个 `conn_info_t` 对象**（继承自 `conn_info_head`），它持有三类 `waiter`（`cc_`/`wt_`/`rd_`）、共享计数器句柄 `acc_h_`、名字信息和连接信息号 `cc_id_`。
- `cc_id_`（身份证号，单调递增、用于过滤自己发的消息）与连接位 `connected_id()`（座位号、32 选 1、用于广播读计数）是**两个不同的 id**，切勿混淆。
- `detail_impl<Policy>` 是桥：用 `Policy` 推导出 `queue_t`/`conn_info_t`，用 `info_of`/`queue_of` 把 `void*` cast 回真实类型，并把全部 API 实现集中在此；`chan_impl` 的每个函数只做一行转发。
- `send` 主链路：`chan_wrapper::send` → `chan_impl::send` → `detail_impl::send` → 分片循环 → `try_push` → `wait_for(wt_waiter_, ...)` + `que->push` → **`rd_waiter_.broadcast()`**。
- `recv` 是对称的反向链：`wait_for(rd_waiter_, ...)` + `que->pop` → **`wt_waiter_.broadcast()`** → 自过滤（`cc_id_` 比较）→ 分片重组或大消息外存。

## 7. 下一步学习建议

本讲建立了「主干」，但故意绕开了三个下钻点。建议按以下顺序继续：

1. **u3-l2 消息格式与分片重组**：深入 `msg_t` 的四个头部字段、`remain_` 的编码方式，以及 `recv` 如何用 thread_local `recv_cache` 按 `msg.id_` 把多片拼回完整消息。
2. **u3-l3 大消息外部存储**：当 `size > large_msg_limit` 时，消息不再分片，而是走 `chunk_t`/`id_pool` 外存与跨接收者引用计数回收（`recycle_storage`）——这里会再次用到本讲区分的「座位号」`connected_id()`。
3. **u3-l4 等待模型**：本讲多次出现的 `wait_for` 和 `sleep(k, ...)` 的退避阈值到底是怎么定的、三类 `waiter` 各自何时阻塞与唤醒。

读完 u3 三篇后续，再进入 u4（无锁循环队列与生产-消费者算法），就能把本讲里「`que->push`/`que->pop` 之后到底发生了什么」彻底看透。
