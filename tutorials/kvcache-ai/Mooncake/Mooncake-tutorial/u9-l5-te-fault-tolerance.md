# Transfer Engine 的容错与故障转移测试

> 本讲对应 `mooncake-transfer-engine/tests/fault-tolerant/` 目录下的测试框架，目标是理解 Transfer Engine（以下简称 TE）在「服务端中途崩溃」时是否还能优雅降级，而不是死锁。建议先学习本系列的 `u3-l1`（TE 架构与多传输层基础）。

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 读懂 `fault_test.py` 这个「编排型」测试框架：它是如何拉起 server、拉起 client、在传输过程中「杀掉」server，再判断 client 是「优雅退出」还是「卡死」的。
2. 说清楚 `server_test.py` / `client_test.py` 这两个角色各自做了什么，以及它们如何用 ZeroMQ（ZMQ）完成一次带外握手、再用 TE 做真正的数据传输。
3. 解释当对端（server）崩溃时，一次 `transfer_sync` 是怎样从「成功」走向「失败 / 超时」并最终把错误返回给上层调用者的——也就是 TE 的**故障检测与降级链路**。
4. 对照 `multi_transport.cpp` 指出「替代路径选择（alternative path selection）」到底发生在代码的哪一行，并理解它与「故障转移」的关系与边界。
5. 学会复现一个最小网络故障场景，并知道该观察什么现象。

---

## 2. 前置知识

在进入源码之前，先用大白话对齐几个概念：

- **Transfer Engine（TE）**：Mooncake 的高性能数据传输引擎。一次传输的发起方叫 **initiator / client**，接收方叫 **target / server**。client 把本地一段内存写到 server 的一段内存里。
- **传输层（Transport）**：TE 底层可以挂多种传输实现：TCP、RDMA、NVLink、Ascend 等。`MultiTransport` 是一个「多路复用器」，负责在提交传输请求时**挑一个合适的传输层**。
- **Segment（段）**：每个引擎会把自己注册的内存区域（segment）发布到元数据服务里，里面记录了「这段内存用什么协议访问」。client 传输前要先 `openSegment(target)`。
- **同步传输 `transfer_sync_write`**：提交一个写请求，然后循环轮询状态，直到 `COMPLETED / FAILED / TIMEOUT`，或整体超时。
- **「优雅降级」与「死锁」的区别**：
  - 优雅降级：对端没了，本端的传输调用在**有限时间**内返回一个错误码（负数），上层可以据此重试或报错。
  - 死锁/挂死：对端没了，本端永远卡在某个 `recv`、某把锁、或某个无上限的轮询里，进程不退出。这正是容错测试要防范的 bug。
- **ZeroMQ（ZMQ）**：一个轻量消息库。本测试用它做**带外控制信道**：server 用 ZMQ 把自己的 `session_id` 和缓冲区地址发给 client，真正的数据走 TE。把「控制面」和「数据面」分开，是测试编排里很常见的做法。

> 如果你还不清楚 `selectTransport`、`submitTransfer`、`getTransferStatus` 这条主线，建议先回头读 `u3-l1`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [fault_test.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py) | **测试编排器**。用 `subprocess` 拉起 server/client，杀掉 server，监控 client 是否卡死。 |
| [server_test.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/server_test.py) | **target 角色**。注册 1MB 内存，用 ZMQ 把缓冲区信息发给 client，然后常驻等待。 |
| [client_test.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/client_test.py) | **initiator 角色**。收 ZMQ 握手信息后，在 `while True` 里不断向 server 同步写数据。 |
| [transfer_engine.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/transfer_engine.py) | 测试自带的 Python 封装（薄包装），把 C++ 绑定的 `transfer_sync_write` 包成 `transfer_sync`，并在返回负数时抛异常。 |
| [multi_transport.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/multi_transport.h) / [multi_transport.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp) | **多传输层复用器**。`selectTransport` / `mp_selectTransport` 在这里挑选传输路径——本讲要对照的「替代路径选择」核心。 |
| [transfer_engine_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp) | Python 绑定层。`transferSync` 实现了「提交—轮询—重试—超时」的同步语义，是把底层故障翻译成上层错误的桥梁。 |
| [tcp_transport.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp) | TCP 传输实现。对端关闭连接时，`async_write` 收到错误，把切片标记为 `FAILED`。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**容错测试框架**、**server/client 编排**、**故障转移验证**。

### 4.1 容错测试框架（fault_test.py 的编排逻辑）

#### 4.1.1 概念说明

`fault_test.py` 不是那种「断言通过/失败」的单元测试，而是一个**故障复现探针（repro probe）**。它的设计哲学很朴素：

> 想知道系统在「对端崩溃」时会不会挂死，最直接的办法就是——真的把对端杀掉，然后盯着另一端看。

它只关心一个核心问题：**server 死掉之后，client 是在有限时间内退出（优雅降级），还是一直活着（可能死锁）？**

这种「编排 + 观测」的测试，特别适合验证并发、网络、超时这类「不出错时看不出来、一出错就 hang 住」的问题。

#### 4.1.2 核心流程

`main()` 的编排可以画成下面这个时间线：

```text
t=0     拉起 server_test.py （subprocess.Popen，stdout 重定向到管道）
t=+2s   拉起 client_test.py
t=+2s   monitor_output(server, 5s)  ┐ 同时各监控 5s，确认双方都活着、没有提前崩
        monitor_output(client, 5s)  ┘ （若任一提前退出 → 判定初始阶段失败）
t=+7s   server_process.terminate()  → 发 SIGTERM「杀掉」server
        server_process.wait(timeout=5)
t=+7s   monitor_output(client, 10s) → 关键观察：server 死后，client 还在不在？
            ├─ client 退出（poll()!=None） → 「normal behavior」（优雅降级）
            └─ client 10s 内一直活着      → 「may be blocked」（疑似挂死）
最后    cleanup()：对仍存活的进程先 terminate，超时则 kill
```

注意几个关键点：

- **它用 `subprocess.Popen` 而不是直接 import**：因为要模拟「独立进程崩溃」，必须让 server/client 跑在各自进程里，才能用 `terminate()` / `kill()` 杀掉。
- **监控靠读 stdout 管道**：`monitor_output` 在固定时间窗内反复 `readline()` + `poll()`，既打印日志，又判断进程是否已退出。
- **它不下硬性 pass/fail 断言**，而是把观测到的事实打印出来（`Client exited...` vs `Client is still running...`），由人或下游 CI 来判断。这是一种**观测型（observational）测试**。

#### 4.1.3 源码精读

`main()` 把上面时间线落地，注意「杀 server」和「观察 client」是紧挨着的两步：

[fault_test.py:35-69](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py#L35-L69) — 拉起 server → 等 2s → 拉起 client → 同时监控 5s → `terminate()` 杀 server → 用 `monitor_output(..., timeout=10)` 观察 client 死后的行为，并据此打印「优雅退出」还是「疑似挂死」。

其中杀 server 这一段很关键：

[fault_test.py:54-66](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py#L54-L66) — `terminate()` 给 server 发 SIGTERM，`wait(timeout=5)` 等它退出；随后用 10s 窗口判断 client 是否还活着。

监控函数本身是一个「带截止时间的轮询」：

[fault_test.py:18-33](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py#L18-L33) — `monitor_output`：循环里先 `poll()`（进程是否已退出），再非阻塞地 `readline()` 读一行日志；返回 `True` 表示「窗口结束时进程还活着」，`False` 表示「进程已退出」。这个返回值就是上层判断优雅降级的依据。

清理逻辑体现了「先礼后兵」的资源回收，避免测试本身残留僵尸进程：

[fault_test.py:71-81](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py#L71-L81) — 对每个仍存活的进程先 `terminate()`，`wait(timeout=5)` 超时则升级为 `kill()`（SIGKILL）。

#### 4.1.4 代码实践

**实践目标**：先把 `fault_test.py` 当成「剧本」读懂，不运行也能复述它在做什么。

**操作步骤**：

1. 打开 [fault_test.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/fault_test.py)。
2. 用笔/注释标出三个时刻：`# T1 拉起双方`、`# T2 杀 server`、`# T3 观察 client`。
3. 思考：如果要把「观测」升级成「断言」，你会把断言加在哪一行？（提示：第 63–66 行的 `if client_alive` 分支。）

**需要观察的现象**：你会注意到这个脚本**没有任何 `assert`**，也不会以非零码退出——它只打印结论。

**预期结果**：能口述出「这个测试的本质是：杀掉 server，看 client 会不会自己退出」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fault_test.py` 要用 `subprocess.Popen` 分别拉起 server/client，而不是在同一个进程里跑两个线程？

> **答案**：因为测试要模拟「对端进程整体崩溃」。只有独立进程才能被 `terminate()` / `kill()` 干净地杀掉，从而逼真地复现「server 没了」的网络故障；同一进程内的线程无法用这种方式「杀死」，也无法复现连接断开、端口关闭等真实现象。

**练习 2**：`monitor_output` 返回 `True` 代表「进程还活着」，那在「杀掉 server 之后」这个返回值代表好现象还是坏现象？

> **答案**：是**坏现象**（疑似挂死）。`fault_test.py:63-66` 据此打印 `Client is still running after server death - it may be blocked`。理想情况下 client 应当在有限时间内退出（返回 `False`）。

---

### 4.2 server / client 编排（两个角色各做什么）

#### 4.2.1 概念说明

要让一次 TE 传输跑起来，client 必须知道三件事：**对端的 session_id（定位对端引擎）、对端缓冲区地址 `ptr`、缓冲区长度 `len`**。这些信息在 TE 里通常通过元数据服务交换，但在测试里，为了简单可控，`server_test.py` 直接用一条 ZMQ 消息把这些信息**带外**发给 client。

于是形成两条信道：

- **控制面（ZMQ，tcp://localhost:5555）**：传 `session_id` / `ptr` / `len` 这种「元信息」，只为握手。
- **数据面（TE 自身的传输层）**：传真正的 1MB 数据。

> 注意：这里 ZMQ 只是个测试辅助手段，和 TE 的容错行为无关。真正会被「杀掉」、会触发容错链路的，是 TE 数据面所连接的那个 server 进程。

#### 4.2.2 核心流程

**server 侧（`server_test.py`）**：

```text
建 ZMQ PUSH socket，bind 5555
创建 MooncakeTransferEngine(localhost:10010, gpu=0)
分配 1MB numpy 缓冲区 → register 到 TE
socket.send_json({session_id, ptr, len})   # 把握手信息发给 client
while True: input()                          # 常驻，等被（测试）杀掉
```

**client 侧（`client_test.py`）**：

```text
建 ZMQ PULL socket，connect 5555
recv_json() → 拿到 {session_id, ptr, len}
创建 MooncakeTransferEngine(localhost:10011, gpu=0)
分配 1MB（全填 1）→ register
while True:                                  # 关键：死循环不停地写
    transfer_sync(server_session_id, client_ptr, server_ptr, len)
    打印成功/失败
```

client 之所以写成一个**死循环**，是为了保证「杀 server」时，几乎必然正好处于一次传输过程中——这正是复现「传输途中对端崩溃」所需要的。

#### 4.2.3 源码精读

server 把握手信息打包发出去，然后常驻：

[server_test.py:12-35](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/server_test.py#L12-L35) — 创建引擎、`register(server_ptr, server_len)` 注册 1MB 内存、用 `socket.send_json(...)` 把 `{session_id, ptr, len}` 发给 client。`session_id` 形如 `localhost:10010:<rpc_port>`（见下方封装）。

[server_test.py:38-47](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/server_test.py#L38-L47) — `while True: input(...)` 让 server 常驻，直到被 `fault_test.py` 的 `terminate()` 杀掉；`finally` 里做 `deregister` 清理。

client 收到握手信息后进入**死循环传输**：

[client_test.py:21-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/client_test.py#L21-L50) — 创建 client 引擎、注册 1MB（`np.ones`）、然后 `while True` 不断调用 `transfer_sync(...)` 向 server 写。

注意 client 这里调用的 `client_engine.transfer_sync` 不是 Mooncake 官方包的方法，而是同目录 `transfer_engine.py` 里的薄封装：

[transfer_engine.py:60-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/transfer_engine.py#L60-L71) — `transfer_sync` 调用底层 `transfer_sync_write`，**当返回值 `< 0` 时主动 `raise RuntimeError`**。这一点对理解「client 为何会退出」至关重要：底层一报错，封装就抛异常，而 `client_test.py` 的 `while True` 没有 `try/except`，于是 client 进程随即崩溃退出。

`session_id` 的构造也在这里，它把 hostname 和 RPC 端口拼起来作为对端定位符：

[transfer_engine.py:26-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/transfer_engine.py#L26-L30) — `self.session_id = f"{hostname}:{rpc_port}"`，这就是 server 通过 ZMQ 发给 client 的那个 `session_id`。

#### 4.2.4 代码实践

**实践目标**：理解「控制面（ZMQ）」与「数据面（TE）」的分离，并定位 client 退出的直接原因。

**操作步骤**：

1. 读 [client_test.py:39-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/client_test.py#L39-L50)，找到那个 `while True`。
2. 读 [transfer_engine.py:60-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tests/fault-tolerant/transfer_engine.py#L60-L71)，确认 `ret < 0` 会 `raise`。
3. 在 `client_test.py` 的 `while True` 体外包一层 `try/except RuntimeError as e: print(e); break`（**仅作为本地观察用，不要提交**），重跑后观察：client 是否会打印一连串失败后「正常 break」而不是崩溃。

**需要观察的现象**：原本（无 try/except）client 会在第一次底层报错时直接崩溃退出；加上捕获后，你能看到失败被打印出来、循环被主动结束。

**预期结果**：理解到「client 退出 = 底层 `transfer_sync_write` 返回了负数」，而负数来自 4.3 节要讲的故障链路。

> 是否真的能跑通取决于本地是否装好 `pyzmq`、`mooncake` 包以及设备环境，**运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：server 用 `np.zeros`（全 0），client 用 `np.ones`（全 1），数据流方向是「谁写到谁」？写完后 server 那块内存应该是什么？

> **答案**：方向是 **client → server（write）**：client 把自己的 `client_ptr`（全 1）写到 server 的 `server_ptr`。所以传输成功后，server 那 1MB 应当从全 0 变成全 1。（注意这是 WRITE 语义：source=本地 client 缓冲，target=对端 server 缓冲。）

**练习 2**：为什么 client 要写成 `while True` 不停地传，而不是只传一次？

> **答案**：为了让「杀 server」这一刀大概率落在「正在传输」的窗口里。如果只传一次、传完就退，测试根本来不及在传输途中杀掉对端，也就无法复现「传输途中崩溃」这个目标场景。

---

### 4.3 故障转移验证（替代路径选择 + 故障如何上浮）

#### 4.3.1 概念说明

这是本讲最核心的一节，要分清两件**不同**的事，初学者很容易混淆：

1. **替代路径选择（alternative path selection）**：在**提交传输请求那一刻**，`MultiTransport` 根据「对端 segment 声明了哪些协议」挑一个传输层来用。这是**提交时的一次性决策**，发生在 `selectTransport` / `mp_selectTransport`。
2. **故障检测与降级（failure detection & graceful degradation）**：传输过程中对端崩了，底层把切片标记为 `FAILED/TIMEOUT`，同步封装 `transferSync` 据此**重试若干次**，重试用尽或整体超时后**返回负数**给上层。

> ⚠️ 关键边界：在 `multi_transport.cpp` 这条（legacy）主线路径里，**没有「传输失败后自动切到另一种协议」的运行时故障转移**。所谓「替代路径」指的是「一个 segment 可声明多个协议（逗号分隔），提交时可在其中任选一个」，以及 `transferSync` 在失败后**遍历本地多个 context（RNIC）重试**。失败最终是以「错误码 + 上层重试」的形式上浮，而不是引擎内部悄悄换一条路继续传。

所以本测试验证的「容错」，准确说是：**TE 在对端崩溃时，不会死锁，而是在有限时间内把失败以错误码形式回报给调用者**——这就是「优雅降级」。

#### 4.3.2 核心流程

把一次「杀掉 server」的故障，从最底层一直追到 client 退出，链路如下：

```text
[底层] server 进程被 SIGTERM → TCP socket 关闭
       client 端 async_write 收到 asio error_code
       → on_finalize(TransferStatusEnum::FAILED)            # 切片标记失败
[传输层] getTransferStatus 聚合切片状态 → 整体 FAILED / TIMEOUT
         （slice_timeout 触发时也会变成 TIMEOUT）
[同步封装 transferSync]
       内层 while：状态==FAILED/TIMEOUT → 跳出内层 → 外层 for retry++
       外层 for：retry 上限 = numContexts()+1（遍历本地所有 RNIC）
                 + 整体超时 = transfer_timeout_nsec_ + length （默认 ~30s）
       用尽/超时 → return -1
[Python 封装] ret<0 → raise RuntimeError
[client_test.py] while True 无 try/except → 进程崩溃退出
[测试] monitor_output 看到进程退出 → 「normal behavior」（优雅降级）
```

而**提交时**的路径选择（与上面的故障链路是两个阶段）：

```text
transferSync 构造 TransferRequest{target_id=handle, ...}
  → engine_->submitTransfer(batch, {entry})
    → MultiTransport::submitTransfer
      → 对每个 request：selectTransport(request, &transport)
            · 取 target segment 的 protocol
            · 在 transport_map_ 里查到对应 Transport*
      → 按 transport 分组，调用各 Transport::submitTransferTask
```

其中 `selectTransport` 处理「单协议」segment；当一个 segment 声明了「逗号分隔的多协议」时，走 `mp_selectTransport`（需要编译期 `ENABLE_MULTI_PROTOCOL`），由调用方传入 `preferred_proto`，在「对端支持的多协议集合」与「本地已安装的传输」的**交集**里挑一个。

#### 4.3.3 源码精读

**先看「替代路径选择」到底在哪几行**——这是本节要对照 `multi_transport.cpp` 回答的核心问题。

`MultiTransport` 的头文件声明了两个选择函数：

[multi_transport.h:70-77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/multi_transport.h#L70-L77) — `selectTransport`（单协议）与 `mp_selectTransport`（多协议，受 `ENABLE_MULTI_PROTOCOL` 保护）的私有声明。

**单协议选择**：拿对端 segment 的协议，直接在已安装的传输表里查：

[multi_transport.cpp:442-464](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464) — `selectTransport`：`proto = target_segment_desc->protocol;`，若 `transport_map_` 里没有该协议就返回 `NotSupportedTransport`，否则把对应的 `Transport*` 交给上层去真正提交任务。

**多协议选择**：解析逗号分隔的协议列表，在「对端支持的集合」与「本地已安装」里挑出 `preferred_proto`：

[multi_transport.cpp:466-505](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L466-L505) — `mp_selectTransport`：用 `std::getline(ss, item, ',')` 把 `target_segment_desc->protocol` 拆成多个协议；先校验 `preferred_proto` 在本地 `transport_map_` 中存在，再校验它在「对端支持列表」里，二者都满足才选中。这就是「一个 segment 声明多条路、提交时挑一条」的替代路径选择点。

> 对比两个函数可以看清边界：路径选择发生在 **submit 之前**、依据是**对端 segment 的元数据**；它本身**不感知**对端是否还活着。对端崩溃的感知发生在传输进行中，靠的是下面的故障检测。

**submitTransfer 如何使用选中的 transport**（路径选择 → 真正提交）：

[multi_transport.cpp:110-149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L110-L149) — 对每个 request 调用 `selectTransport` 拿到 `transport`，按 `transport` 分组聚合到 `submit_tasks`，最后逐个 `entry.first->submitTransferTask(...)`。任一组失败会以 `overall_status` 返回（注意这里只是「提交」失败，不等同于「传输」失败）。

**故障如何变成 FAILED/TIMEOUT**：`getTransferStatus` 里有一个「切片超时」判定，把长时间没进展的切片升级为 `TIMEOUT`：

[multi_transport.cpp:204-220](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L204-L220) — `checkSliceTimeout`  lambda：当 `current_ts - slice->ts > slice_timeout * 1e9` 时判定超时。`slice_timeout` 默认 `-1`（关闭），可用环境变量 `MC_SLICE_TIMEOUT`（秒）打开（见 [config.cpp:288-292](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/config.cpp#L288-L292)、[config.h:64](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/config.h#L64)）。这是「对端不响应」被上浮成 `TIMEOUT` 的机制之一。

[multi_transport.cpp:228-239](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L228-L239) — 若 task 有 transport，先委托 transport 自己的 `getTransferStatus` 去轮询完成事件，再叠加 `checkSliceTimeout`：状态是 `WAITING` 且切片超时 → 升级为 `TIMEOUT`。

**TCP 层「对端关闭」如何变成 FAILED**：当 server 被杀、连接断开，client 的 `async_write` 回调会收到非空 `error_code`，于是把这次传输 finalize 为失败：

[tcp_transport.cpp:529-543](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L529-L543) — `async_write` 回调里 `if (ec) { ... asio::post(... on_finalize(TransferStatusEnum::FAILED) ...) }`。这就是「对端崩溃 → 切片 FAILED」最直接的来源（类似逻辑也出现在 [tcp_transport.cpp:348](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L348) 等多处 finalize 点）。

**同步封装 transferSync：把 FAILED/TIMEOUT 翻译成「重试 + 超时 + 返回负数」**——这是连接底层与 client 退出的关键：

[transfer_engine_py.cpp:418-446](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L418-L446) — 注释里点明了一种「替代路径」式重试的初衷：一次 `transferSync` 会遍历本地多个 RNIC context（`max_retry = numContexts() + 1`）。每次构造 `TransferRequest` 并提交（`engine_->submitTransfer`）。

[transfer_engine_py.cpp:447-459](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L447-L459) — 若 `submitTransfer` 本身失败，调用 `CheckSegmentStatus`；只有它不 OK（典型如 BAREX，见 [transfer_engine_impl.cpp:531-544](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L531-L544)，普通 RDMA/TCP 直接返回 OK）才会 `closeSegment` 并清掉 handle 缓存；无论哪种，都 `return -1`。

[transfer_engine_py.cpp:461-488](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L461-L488) — 内层轮询：`COMPLETED`→返回 0；`FAILED/TIMEOUT`→跳出内层进入下一次重试；同时有「整体超时」`transfer_timeout_nsec_ + length`（1GiB/s 的带宽估算兜底），超时则 `return -1`。重试用尽也 `return -1`。

整体超时的默认值由来：

[transfer_engine_py.cpp:104-111](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L104-L111) — 默认 `transfer_timeout_nsec_ = 30s`；可用环境变量 `MC_TRANSFER_TIMEOUT`（最小取 5s）覆盖。这决定了「对端崩溃后，client 最多卡多久才一定返回」。

把上面串起来：**对端崩溃 → 切片 FAILED → transferSync 重试用尽/超时 → 返回 -1 → Python 封装抛异常 → client 进程退出**。`fault_test.py` 看到的「client exited」就是这个链路走通的体现，即「优雅降级」。

#### 4.3.4 代码实践

**实践目标**：亲手复现「传输途中杀 server」，观察 client 是否优雅退出；并用 `MC_TRANSFER_TIMEOUT` 量化「最多卡多久」。

**前置条件**：本机已安装 `mooncake`（含 `mooncake.engine`）、`pyzmq`、`numpy`，且 TE 在本机能以 TCP/本地方式初始化。若不具备，请退化为「源码阅读型实践」（见下）。

**操作步骤（可运行）**：

1. 进入目录：`cd mooncake-transfer-engine/tests/fault-tolerant`
2. 先单独起 server，再起 client，确认正常传输能跑通：
   - 终端 A：`python3 server_test.py`
   - 终端 B：`python3 client_test.py`（应看到 `Transfer successful!` 刷屏）
3. 在终端 A 对 server 做 `Ctrl-C` 或 `kill <pid>`，模拟崩溃；回到终端 B 观察 client：
   - 预期：client 在**有限时间**内停止成功刷屏，转而报错/退出（优雅降级）。
4. 一键编排复现：`python3 fault_test.py`，重点看末尾打印是
   `Client exited after server death - normal behavior`（健康）
   还是
   `Client is still running after server death - it may be blocked`（疑似挂死）。
5. 量化兜底超时：`MC_TRANSFER_TIMEOUT=5 python3 client_test.py`，再杀 server，观察 client 最多约 5s 后一定返回错误（而非无限挂起）。

**需要观察的现象**：

- 杀 server 后，client 的 `Transfer successful!` 停止出现；最终报错或退出。
- 用 `MC_TRANSFER_TIMEOUT=5` 时，client「卡住」的时长被限制在数秒量级。

**预期结果**：

- 健康表现：client 不会永远挂起，最终把失败以错误码/异常形式上浮并退出；`fault_test.py` 报告 `normal behavior`。
- 如果观察到 client 永久不退出、CPU 占用为 0（典型死锁特征），说明存在回归 bug——这正是该测试要拦截的对象。

> 受本地环境（是否真有可用设备/端口、`pyzmq` 版本等）影响，**具体耗时与是否能在 10s 监测窗口内退出，待本地验证**。

**对照 multi_transport.cpp 的问题**：「替代路径选择发生在何处？」

> 在 [multi_transport.cpp:442-464](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464)（`selectTransport`，单协议）与 [multi_transport.cpp:466-505](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L466-L505)（`mp_selectTransport`，多协议）。注意它是**提交前**依据**对端 segment 元数据**的一次性选择；真正的「失败重试/遍历本地 context」在 [transfer_engine_py.cpp:423-426](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L423-L426)。

**源码阅读型实践（无法运行时）**：

1. 跟踪一条调用链：`client_test.py: transfer_sync` → `transfer_engine.py: transfer_sync_write` → `transfer_engine_py.cpp: transferSync` → `submitTransfer` → `MultiTransport::submitTransfer` → `selectTransport`。在每一处标注「这步是路径选择、提交、还是轮询」。
2. 用 [tcp_transport.cpp:529-543](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L529-L543) 的 `if (ec)` 分支解释：「为什么 server 一被杀，client 这次传输就会变 FAILED」。把这个 `ec`（asio error_code）想象成「写一个已关闭的 socket 收到的错误」。

#### 4.3.5 小练习与答案

**练习 1**：`selectTransport` 和 `mp_selectTransport` 的输入依据分别是什么？为什么说它们「不感知对端是否还活着」？

> **答案**：依据都是**对端 segment 的元数据**（`target_segment_desc->protocol`），即对端**注册时声明**的协议；`mp_selectTransport` 额外接收调用方指定的 `preferred_proto`。它们只看「声明了什么 / 本地装了什么」，并不发起任何探活，所以对「对端进程是否还在」一无所知——存活感知要等传输进行中由底层异步错误/超时来提供。

**练习 2**：假设 `MC_TRANSFER_TIMEOUT` 没设置、`slice_timeout` 也是默认 `-1`，server 被杀后，client 这次 `transfer_sync` 一定能很快返回吗？

> **答案**：不一定快，但**一定有界**。快慢取决于底层多快感知到连接断开：TCP 本地杀进程通常会让 `async_write` 很快收到错误 → 很快 `FAILED` → 重试几次后返回 -1（较快）；但若遇到不可达地址导致 `connect()` 卡住，则有 `handshake_connect_timeout`（默认 5s，见 [config.h:60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/config.h#L60)）兜底；最坏由整体超时 `transfer_timeout_nsec_`（默认 30s）兜底返回 -1。所以「有界」是优雅降级的核心保证，「无界挂起」则是该测试要拦截的回归。

**练习 3**：本讲所说的「故障转移（failover）」与「在一个 segment 的多协议里挑一个」是不是同一回事？

> **答案**：不是。「多协议里挑一个」是**提交时**的静态路径选择（`mp_selectTransport`）；本测试验证的「容错/故障转移」更接近「**失败检测 + 有界重试 + 错误上浮**」——对端崩了，引擎不会无限挂起，而是把失败回报给上层，由上层决定是否换目标重试。在 `multi_transport.cpp` 这条主线里，没有「传输失败后自动改用另一种协议继续」的运行时切换。

---

## 5. 综合实践

**任务：把 `fault_test.py` 从「观测型」改造成「带断言的回归测试」（仅本地练习，不要提交到仓库）。**

要求：

1. 复制一份 `fault_test_local.py`（**只写在 `Mooncake-tutorial/` 目录下，不要改动源码**）。
2. 在杀掉 server、监控 client 之后，加入明确判定：
   - 若 client 在 `timeout=10` 内退出（`monitor_output` 返回 `False`）→ 打印 `PASS: graceful degradation`，脚本以退出码 0 结束。
   - 若 client 仍存活（返回 `True`）→ 打印 `FAIL: client may be deadlocked`，脚本以退出码 1 结束。
3. 用 `sys.exit(0/1)` 把结论变成可被 CI 捕获的退出码。
4. （进阶）把 `MC_TRANSFER_TIMEOUT` 通过 `subprocess` 的 `env=` 注入给 client 子进程，验证调小超时后 client 退出更迅速。

**验收标准**：

- 你能解释：为什么「client 在有限时间内退出」就等价于「TE 在该故障下优雅降级」。
- 你能指出：判定所依赖的链路是 `tcp_transport 的 on_finalize(FAILED)` → `getTransferStatus` → `transferSync` 重试/超时 → `return -1`。
- 你能回答：如果将来引入「运行时多协议自动故障转移」，它会插在这条链路的哪一步（提示：在 `transferSync` 收到 `FAILED` 之后、`return -1` 之前，换一个 `preferred_proto` 重新 `submit`）。

> 该改造仅用于学习理解，**运行与具体退出码待本地验证**。

---

## 6. 本讲小结

- `fault_test.py` 是一个**观测型编排探针**：拉起 server/client → 在传输途中 `terminate()` 杀 server → 看 client 是「有限时间内退出（优雅降级）」还是「一直活着（疑似死锁）」。
- `server_test.py` / `client_test.py` 用 **ZMQ 做控制面握手**（传 `session_id/ptr/len`），用 **TE 做数据面传输**；client 写成 `while True` 是为了让「杀 server」落在传输途中。
- **替代路径选择**发生在 `multi_transport.cpp` 的 `selectTransport`（单协议，L442）/ `mp_selectTransport`（多协议，L466），依据是**对端 segment 的元数据**，属于**提交前的一次性决策**，不感知对端存活。
- **故障上浮链路**：对端关闭 → TCP `async_write` 收到 `error_code` → `on_finalize(FAILED)` → `getTransferStatus` 聚合（叠加 `slice_timeout` 可升级为 `TIMEOUT`）→ `transferSync` 重试遍历本地 context + 整体超时（默认 30s，`MC_TRANSFER_TIMEOUT` 可调）→ `return -1` → Python 封装抛异常 → client 退出。
- 本测试验证的「容错」= **故障有界、不死锁、以错误码回报**；它**不等于**「运行时自动切换到另一种协议」——后者在 `multi_transport.cpp` 主线路径里目前不存在。
- 调优把手：`MC_TRANSFER_TIMEOUT`（同步传输整体超时）、`MC_SLICE_TIMEOUT`（单切片超时，默认关）、`handshake_connect_timeout`（连接握手超时）。

---

## 7. 下一步学习建议

- **横向对比「下一代」引擎 tent 的 failover**：阅读 [tent/tests/engine_failover_e2e_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/tests/engine_failover_e2e_test.cpp)，看看 tent 是否提供了更显式的多协议/多路径故障转移，与本讲的「有界重试」模型做对比。
- **深入传输层故障语义**：对照 [tcp_transport.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp) 与 RDMA 传输，理解不同传输「感知对端死亡」的速度差异（TCP 的 RST/EOF vs RDMA 的重试耗尽/路径迁移），以及它们如何影响 `transferSync` 的实际耗时。
- **回到主线 API**：结合 `u3-l1` 与 Python API 讲义，把 `transfer_sync_write` / `submitTransfer` / `getTransferStatus` 的异步语义串起来，理解「为什么上层应用应当处理负返回值而不是假设永远成功」。
- **扩展测试场景**：尝试在 `fault_test.py` 思路基础上，复现「网络分区（iptables 丢包）」「延迟杀 server（传输进行到一半）」等更复杂故障，观察 `slice_timeout` 打开后的 `TIMEOUT` 路径。
