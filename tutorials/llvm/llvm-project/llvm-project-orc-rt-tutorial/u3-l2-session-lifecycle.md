# 生命周期状态机：attach / detach / shutdown

## 1. 本讲目标

上一讲我们认识了执行端根对象 `Session` 是怎么构造出来的，但只用到它的初始状态 `Start`。本讲要回答一个更关键的问题：**`Session` 从诞生到销毁，到底经历了哪些状态？这些状态在多线程下是如何被安全推进的？**

具体来说，学完本讲你应该能够：

- 说出 `Session` 的四态状态机（`Start → Attached → Detached → Shutdown`），并解释 `CurrentState` 与 `TargetState` 这两个变量的分工。
- 解释 `detach` 与 `shutdown` 在并发请求下如何用「`TargetState` 取 `max`」合并意图、如何用「抢到 `TargetState` 的线程负责推进」来避免竞争。
- 画出 `shutdown` 的三阶段流程：Detach → Drain 托管代码 → 逆序 `onShutdown`，并能在源码里指出每个阶段对应的函数。
- 理解为什么 `~Session()` 会阻塞，直到整个生命周期跑完 `completeShutdown`。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **controller / executor 二分模型**（u2-l1）：`Session` 运行在执行端，通过 `ControllerAccess` 与控制端双向通信。
- **`Session` 的构造三要素**（u3-l1）：`ExecutorProcessInfo`、`DispatchFn`、`ErrorReporterFn`，以及它持有的 `ManagedCodeTaskGroup`、`Services`、`CA` 等成员。
- **`Error` / `Expected<T>`**（u2-l3）：本讲会少量用到错误类型，但状态机本身才是主角。
- **`Service` 接口**（u3-l1 已提及，u3-l3 会展开）：`Service` 有 `onDetach(OnComplete, ShutdownRequested)` 和 `onShutdown(OnComplete)` 两个回调。

此外，你需要一点 C++ 并发基础：`std::mutex`（互斥锁）、`std::condition_variable`（条件变量，用于「等待某个条件成立」）、`std::atomic_load/store`（对 `shared_ptr` 的原子读写）。不熟悉也没关系，我们会在用到时用一句话点明它的作用。

一个核心直觉先记住：**`Session` 的生命周期是「单向单调」的**——状态只能往前走，不能回退。这就像一列只能单向行驶的火车，一旦发车就不会倒退。整个状态机的设计，本质上就是在多线程环境下保证「这列火车安全、不重复地开到终点」。

---

## 3. 本讲源码地图

本讲涉及三个核心文件：

| 文件 | 作用 |
|------|------|
| [include/orc-rt/Session.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | `Session` 类的声明，含 `State` 枚举、`CurrentState`/`TargetState` 成员、`attach`/`detach`/`shutdown` 公开接口，以及私有的状态推进方法声明。 |
| [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) | 状态机的全部实现：`doAttach`、`detach`、`shutdown`、`proceedToDetach`、`proceedToShutdown` 等。 |
| [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | 生命周期相关的 GoogleTest 用例，是理解状态机行为的最佳入口。 |

辅助理解（不展开细讲）：

| 文件 | 作用 |
|------|------|
| [include/orc-rt/TaskGroup.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h) | `TaskGroup`/`Token`，「托管代码」计数的载体，shutdown 第二阶段 Drain 就靠它。 |
| [include/orc-rt/Service.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h) | `Service` 抽象接口，定义了 `onDetach`/`onShutdown` 回调签名。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **State 枚举与状态转移**：四态状态机长什么样，`CurrentState`/`TargetState` 如何分工。
2. **detach / shutdown 的并发处理**：多线程同时请求关闭时如何不打架。
3. **关闭顺序与等待**：shutdown 三阶段的完整调用链，以及析构为何阻塞。

---

### 4.1 State 枚举与状态转移

#### 4.1.1 概念说明

`Session` 用一个枚举描述自己处在生命周期的哪一步。先看定义：

[include/orc-rt/Session.h:518-533](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L518-L533) —— 定义了 `None / Start / Attached / Detached / Shutdown` 五个枚举值。

注意：`State` 是一个**有序**枚举（`None < Start < Attached < Detached < Shutdown`），这个顺序在后面至关重要——代码会直接用 `>=`、`std::max` 来比较状态，靠的就是这个隐含的数值递增。

各状态含义：

| 状态 | 含义 |
|------|------|
| `None` | **占位符**，专门用作「没有待处理请求」。不是 `Session` 真正经历的状态。 |
| `Start` | 初始态。`Session` 刚构造好，还没有 `ControllerAccess` 被挂上来。 |
| `Attached` | `ControllerAccess` 已挂上，与控制端的连接已建立。 |
| `Detached` | 控制端已断开，所有 `Service` 都已收到 `onDetach` 通知。 |
| `Shutdown` | 终态。托管代码已排空，所有 `Service` 都已收到 `onShutdown`。 |

这里有一个非常关键的设计：`Session` **同时维护两个状态变量**，而不是一个：

[include/orc-rt/Session.h:634-635](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L634-L635) —— `CurrentState = State::Start`（初值）、`TargetState = State::None`（初值）。

理解这两个变量的分工是本讲的核心：

- **`CurrentState`（当前态）= 已经发生的事实**。比如 `CurrentState == Detached` 意味着「`onDetach` 已经全部跑完」。
- **`TargetState`（目标态）= 被要求达到的意图**。`TargetState == None` 表示「目前没有人在推进关闭」；`TargetState == Shutdown` 表示「有人要求 shutdown」。

两者之间的「落差」就是「正在进行的工作」。比如 `CurrentState == Attached` 而 `TargetState == Shutdown`，意味着「shutdown 已经被请求，正在推进中，但还没跑到 Detached/Shutdown」。

#### 4.1.2 核心流程

`CurrentState` 的合法转移是**单调向前**的：

```
                attach() 成功
        ┌──────────────────────────┐
        ▼                           │
      Start ──────────► Attached ──► Detached ──► Shutdown
        │                  ▲              │            ▲
        │                  │              │            │
        └──── (无 attach    │              └────────────┘
              直接 shutdown)│                 proceedToShutdown
                proceedToDetach
                (TmpCA=nullptr)
```

要点：

1. `Start` 可以直接跳到 `Attached`（正常 attach），也可以直接跳到 `Detached`（没 attach 就 shutdown/detach）。
2. 一旦离开 `Start`，状态只能逐级前进：`Attached → Detached → Shutdown`，不能跳级、不能回退。
3. `Shutdown` 是唯一终态，到达后 `TargetState` 被重置为 `None`，析构函数的条件变量才会被唤醒。

而 `TargetState` 的角色更像一个「意图寄存器」：它由请求方写入（`detach` 写 `Detached`、`shutdown` 写 `Shutdown`），由推进方在到达目标后清零（写回 `None`）。

#### 4.1.3 源码精读

最典型的状态转移发生在 `doAttach` 里。attach 是唯一可能让 `CurrentState` 从 `Start` 前进的方法，它要处理三种并发情形。先看它如何「抢状态」：

[lib/executor/Session.cpp:67-82](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L67-L82) —— 在锁内检查：只有当 `CurrentState == Start` **且** `TargetState == None`（即「还没人请求关闭」）时才接受 attach，否则直接 `return`，连 `ControllerAccess` 的所有权都不接。这段同时把 `CA` 原子存入，并把 `TargetState` 设为 `Attached`，表示「我正在尝试连接」。

注意第 78-79 行：`TargetState` 被设成 `Attached`，而不是 `None`。这是一个精妙的占位——它表示「attach 这件事正在进行中」，这样如果此时别的线程并发调用 `detach`/`shutdown`，它们会看到 `TargetState != None`，从而走「合并意图」而非「自己驱动」的路径（详见 4.2）。

随后释放锁去真正建立连接 `CA->connect(BI)`，连接回来后再加锁判断结果。源码里用大段注释列出了三种情形：

[lib/executor/Session.cpp:88-126](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L88-L126) —— 三种情形：

1. **连接成功且无并发请求**（第 104-108 行）：`TargetState == Attached`，说明没人插队。把 `CurrentState` 推进到 `Attached`，`TargetState` 清回 `None`，完事。
2. **连接失败**（第 114-115 行）：`connect` 内部必定已经调了 `notifyDisconnected`（进而调 `handleDisconnect`），它已经把 `CurrentState` 推进到 `Detached` 了，这里只需 `return`。
3. **连接成功但并发被请求 detach/shutdown**（第 117-125 行）：`TargetState >= Detached` 但 `CurrentState` 还在 `Start`/`Attached`。先把 `CurrentState` 设成 `Attached`，然后落到 `CA->disconnect()` 去启动断开流程。

> 小贴士：`std::atomic_load(&this->CA)` / `std::atomic_store` 是 C++ 对 `shared_ptr` 的原子读写，保证在「一个线程正在读 `CA` 准备调用」时，另一个线程不会正好把它置空导致悬空指针。

#### 4.1.4 代码实践

**实践目标**：理解 `doAttach` 的三种情形，能用自己的话复述。

**操作步骤**：

1. 打开 [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp)，定位 `doAttach`（第 67 行起）。
2. 阅读第 88-126 行的注释和代码。
3. 对照 [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) 中的 `ControllerAccessTest::FailConnect`（第 858-871 行）——它对应情形 (2)：`OnConnect` 返回错误，于是 `connect` 内部调 `reportError` + `notifyDisconnected`。

**需要观察的现象**：在 `FailConnect` 里，`OnConnect` 返回 `make_error<StringError>("failed to connect")`，最终 `GotError` 为 true。

**预期结果**：情形 (2) 命中，`CurrentState` 经由 `handleDisconnect → proceedToDetach` 被推进到 `Detached`，`doAttach` 在第 115 行 `return`，不再做任何事。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `State` 枚举的数值顺序（`None < Start < Attached < Detached < Shutdown`）对状态机的正确性很重要？

> **参考答案**：因为代码大量使用 `>=` 比较和 `std::max` 合并。比如 `completeDetach` 用 `if (TargetState == State::Detached)` 判断「只需 detach」，否则「还要继续 shutdown」；`detach`/`shutdown` 用 `std::max(TargetState, ...)` 把更强的意图盖过更弱的。如果数值顺序乱了，这些比较就会得出错误结论。

**练习 2**：attach 成功后（情形 1），`TargetState` 被设成了什么值？为什么不是 `Attached`？

> **参考答案**：被设回 `None`（第 106 行）。因为 `TargetState` 表示「待处理的请求」，attach 一旦完成、`CurrentState` 已是 `Attached`，就不再有待处理的转移了，所以 `TargetState` 必须清零，好让后续的 `detach`/`shutdown` 能正常抢到驱动权。

---

### 4.2 detach / shutdown 的并发处理

#### 4.2.1 概念说明

现在难点来了：如果**多个线程同时**调用 `detach` 或 `shutdown`，甚至 `ControllerAccess` 自己检测到远端掉线也并发地通过 `notifyDisconnected` 触发断开，状态机怎么保证不会「两个线程同时推进、互相踩踏」？

orc-rt 的解法非常优雅，核心是两条规则：

**规则一：第一个把 `TargetState` 从 `None` 改成非 `None` 的线程，成为「驱动者（driver）」**，它负责一气呵成地把状态推进到目标。后来者只做一件事：把自己的意图用 `std::max` 合并进 `TargetState`，然后立刻返回。

**规则二：意图是「单调偏序」的**。`detach` 想要「至少到 `Detached`」，`shutdown` 想要「至少到 `Shutdown`」。由于 `Shutdown > Detached`，一个 shutdown 请求天然包含了一个 detach 请求。所以用 `std::max` 合并是完全正确的——「我要至少这么远」。

这套机制等价于一个「自选领袖（leader election）」协议：用 `TargetState == None` 这一个布尔条件充当「当前有没有领袖」的标志，抢到它的人就是领袖，没抢到的人只需投票（合并意图）。不需要单独的「谁在驱动」变量。

#### 4.2.2 核心流程

以 `shutdown` 为例的并发合并逻辑（伪代码）：

```
shutdown(OnShutdown):
    addOnShutdown(OnShutdown)          # 先登记回调（线程安全）
    lock(M)
    if TargetState != None:            # 已有驱动者
        TargetState = max(TargetState, Shutdown)   # 只合并意图
        return                         # 不自己推进
    if CurrentState == Shutdown:       # 已经到终态（冗余请求）
        return                         # 回调已在 addOnShutdown 里就地执行
    TargetState = Shutdown             # 我抢到驱动权！
    根据 CurrentState 分三种入口推进：
        Start   -> proceedToDetach(null)        # 从未 attach，直接拆
        Attached-> 取出 CA，调 CA->disconnect()  # 先断控制器
        Detached-> waitForManagedCodeTasksThenShutdown()  # 已拆，直接排空
```

`detach` 的结构几乎一模一样，只是把 `Shutdown` 换成 `Detached`。

驱动者一旦抢到，就会沿着这条链一路跑到底：

```
proceedToDetach  →  detachServices(逆序 onDetach)  →  completeDetach
                                                              │
                                     ┌────────────────────────┘
                                     ▼ (若 TargetState==Shutdown)
                          waitForManagedCodeTasksThenShutdown
                                     │ (等所有 Token 释放)
                                     ▼
                          proceedToShutdown → shutdownServices(逆序 onShutdown)
                                     │
                                     ▼
                          completeShutdown  (TargetState=None, CV.notify_all)
```

关键点：`completeDetach` 会检查 `TargetState`。如果当初只是 `detach`，`TargetState == Detached`，到这里清零、结束；如果中途有人并发调了 `shutdown` 把 `TargetState` 顶到了 `Shutdown`，那么 `completeDetach` 看到 `TargetState == Shutdown`，**不停下**，继续走到排空和 `onShutdown`。这就是「合并意图」如何在驱动链里生效。

#### 4.2.3 源码精读

先看 `detach` 如何实现「抢驱动权 + 合并意图」：

[lib/executor/Session.cpp:128-161](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L128-L161) —— 三段式：

- 第 136-139 行：`TargetState != None`，说明已有驱动者，用 `std::max(TargetState, State::Detached)` 合并意图后 `return`。
- 第 142-143 行：`CurrentState >= Detached`，说明已经拆过了，这是冗余请求，`return`。
- 第 146 行起：自己成为驱动者，置 `TargetState = Detached`。若当前是 `Attached`，取出 `CA` 后在锁外调 `CA->disconnect()`；若当前是 `Start`（从没 attach 过），直接 `proceedToDetach` 并传一个空的 `CA`。

`shutdown` 的骨架完全对称：

[lib/executor/Session.cpp:163-203](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L163-L203) —— 注意第 185-199 行的 `switch(CurrentState)` 三入口：

- `Start`：`proceedToDetach(Lock, nullptr)`（没有控制器要断）。
- `Attached`：取出 `CA`，锁外 `TmpCA->disconnect()`。
- `Detached`：已经拆过了，直接 `waitForManagedCodeTasksThenShutdown()`（解锁后）。

还有一条并发路径：**远端主动掉线**。`ControllerAccess` 实现检测到连接丢失时会调 `notifyDisconnected()`，它最终调到 `Session::handleDisconnect`：

[lib/executor/Session.cpp:270-276](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L270-L276) —— 它把 `TargetState` 至少抬到 `Detached`，再用 `std::atomic_exchange` 把 `CA` 置空（原子地「取出并清空」），然后 `proceedToDetach`。注意这里**没有**走「抢驱动权」的 `TargetState == None` 判断，而是直接推进——因为 `handleDisconnect` 一定是在连接已经建立（`CurrentState <= Attached`）的上下文里被调用的，它本身就是合法的驱动者。`proceedToDetach` 之前的 `TargetState = std::max(TargetState, Detached)` 保证意图不会被丢。

> 小贴士：`std::atomic_exchange(&this->CA, {})` 原子地「返回旧值并把成员置空」，确保只有一个线程拿到非空的 `CA` 去断开，避免重复 `disconnect`。

#### 4.2.4 代码实践

**实践目标**：通过现成测试，观察「在 `onDetach` 回调里发起 `shutdown`」这种并发场景的正确行为。

**操作步骤**：

1. 打开 [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp)，阅读 `ScheduleShutdownFromOnDetachHandler`（第 402-423 行）。
2. 关注它注册的回调顺序：两个 `addOnDetach`（一个计数、一个调 `S.shutdown()`、再一个计数），以及一个 `addOnShutdown`，后者断言 `OnDetachHandlersRun == 2`。
3. 阅读 `RedundantAsyncShutdown`（第 425-438 行）：先 `waitForShutdown(S)` 跑完一次 shutdown，再 `S.shutdown([&]{ RedundantCallbackRan = true; })`，验证冗余回调也会被执行。

**需要观察的现象**：

- 在 `ScheduleShutdownFromOnDetachHandler` 中，第二个 `onDetach` 回调里调 `S.shutdown()` 时，detach 还没跑完（`TargetState` 还是 `Detached`）。`shutdown` 走到第 171-174 行的「已有驱动者」分支，把 `TargetState` 用 `max` 顶到 `Shutdown`。于是驱动链不会在 `completeDetach` 停下，会继续跑到 `onShutdown`。
- 关键不变量：**所有 `onDetach` 回调都先于任何 `onShutdown` 回调执行**，所以 `addOnShutdown` 里的断言 `OnDetachHandlersRun == 2` 成立。

**预期结果**：`OnShutdownHandlerRun` 为 true，测试通过。

> 如何本地验证：按 u1-l2 讲的方法构建 `check-orc-rt-unit`，运行 `SessionTest.ScheduleShutdownFromOnDetachHandler` 与 `SessionTest.RedundantAsyncShutdown`。若未配置构建环境，标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：线程 A 调了 `detach()`，正在驱动；线程 B 此刻调 `shutdown()`。B 会做什么？最终状态机会停在 `Detached` 还是 `Shutdown`？

> **参考答案**：B 看到 `TargetState != None`（A 已把它设成 `Detached`），于是执行 `TargetState = max(TargetState, Shutdown)`，把 `TargetState` 顶到 `Shutdown`，然后立即返回。A 继续驱动，跑到 `completeDetach` 时检查 `TargetState == Shutdown`（不是 `Detached`），于是不停下，继续排空托管代码并执行 `onShutdown`，最终停在 `Shutdown`。

**练习 2**：为什么 `handleDisconnect`（远端掉线路径）不需要先判断 `TargetState == None` 就直接 `proceedToDetach`？这样会不会和并发的 `detach()` 重复断开？

> **参考答案**：`handleDisconnect` 用 `std::atomic_exchange(&CA, {})` 原子地取出 `CA` 并置空。如果并发的 `detach()` 也想断开，它要么在 `exchange` 之前拿到非空 `CA`（那么由它来 `disconnect`，`handleDisconnect` 的 `exchange` 拿到空，`TmpCA` 为空无害），要么在之后拿到空（`detach` 的第 150-152 行取到的 `TmpCA` 为空时其实会走 `Start` 分支或断言）。`ControllerAccess::disconnect` 的契约也要求实现方能容忍「重复/并发的 disconnect 当作 no-op」。所以不会真正重复断开。

---

### 4.3 关闭顺序与等待

#### 4.3.1 概念说明

前两个模块讲清了「状态怎么转移、并发怎么协调」，本模块讲「驱动者抢到之后，具体按什么顺序做什么」。

`shutdown` 的公开文档把整个过程明确分成三阶段（这正是本讲第一个实践任务要标注的）：

[include/orc-rt/Session.h:406-417](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L406-L417) —— 官方对三阶段的说明。

三阶段是：

1. **Detach（拆控制器 + 通知 Service）**：若尚未 detach，先断开 `ControllerAccess`，然后对每个 `Service` 调 `onDetach(OnComplete, ShutdownRequested)`。
2. **Drain（排空托管代码）**：等待所有「正在执行托管代码」的任务结束。托管代码（JIT 出来的机器码、或代 JIT 代码加载的库代码）在执行时持有 `ManagedCodeTaskGroup` 的 Token；只要还有 Token 没释放，就不能进入下一阶段，否则可能在托管代码还在某条栈上跑的时候就把 `Session` 拆了，造成 use-after-free。
3. **Shutdown services（逆序 `onShutdown`）**：对所有 `Service` 按**注册的逆序**调 `onShutdown(OnComplete)`，释放它们持有的资源。

为什么 Service 要逆序关闭？这与 C++ 成员/全局对象的析构顺序惯例一致：后注册的 Service 往往依赖先注册的，拆的时候要先拆依赖方、再拆被依赖方。

还有一个贯穿全局的约定：**`~Session()` 会阻塞，直到生命周期彻底结束**。

[include/orc-rt/Session.h:308-312](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L308-L312) —— 析构「会触发 shutdown（若尚未发生），并阻塞到生命周期完成」。

原因：`Session` 拥有 `Services`、`ManagedCodeTaskGroup` 等成员。如果析构函数直接返回、对象内存被回收，而此刻某条别的线程的栈上还跑着托管代码、引用着这些成员，就会访问已释放内存。所以析构必须等到「所有 `onShutdown` 跑完、`completeShutdown` 把 `TargetState` 清零」，才允许对象消失。

#### 4.3.2 核心流程

把驱动链与三阶段的对应关系画清楚：

```
shutdown() 抢到驱动权
        │
        ▼
【阶段1: Detach】
  Attached? -> CA->disconnect() ─┐  (disconnect 最终触发 notifyDisconnected)
  Start?    -> proceedToDetach(null)
                                 │
  handleDisconnect / proceedToDetach  ←─┘
        │
        ├─ CurrentState = Detached
        ├─ 丢弃 CA（TmpCA.reset()）
        └─ detachServices(逆序对每个 Service 调 onDetach)
                │
                ▼ (全部 onDetach 完成)
          completeDetach()
                │
                ├─ if TargetState==Detached: TargetState=None; 结束（纯 detach）
                └─ if TargetState==Shutdown: 继续 ▼
        │
        ▼
【阶段2: Drain】
  waitForManagedCodeTasksThenShutdown()
        ├─ ManagedCodeTaskGroup->addOnComplete( proceedToShutdown )
        └─ ManagedCodeTaskGroup->close()      # 不再发新 Token
                │
                │  (等待 NumTasks 归零：即所有托管代码 Token 释放)
                ▼
【阶段3: Shutdown services】
  proceedToShutdown()
        ├─ CurrentState = Shutdown
        └─ shutdownServices(逆序对每个 Service 调 onShutdown)
                │
                ▼ (全部 onShutdown 完成)
          completeShutdown()
                ├─ TargetState = None
                └─ CV.notify_all()    # 唤醒 ~Session 里等待的线程
```

注意两个「逆序」都用了同一种递归手法（见 4.3.3）。还要注意：阶段 2 的 `close()` 只是「关闭发 Token 的窗口」，并不阻塞——真正的「等待」靠 `addOnComplete` 注册的回调：`TaskGroup` 在「已 close 且计数归零」时才会调这个回调，从而触发 `proceedToShutdown`。

#### 4.3.3 源码精读

**阶段 1 的核心：`proceedToDetach` 与逆序 `onDetach`**

[lib/executor/Session.cpp:278-293](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L278-L293) —— 快照所有 Service 指针到 `ToNotify`，记录 `ShutdownRequested = (TargetState == Shutdown)`（这个布尔会传给每个 `Service::onDetach`，让它们知道「接下来是不是要 shutdown」），把 `CurrentState` 设成 `Detached`，解锁，丢弃控制器 `TmpCA.reset()`，然后 `detachServices`。

逆序是怎么实现的？看递归：

[lib/executor/Session.cpp:295-307](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L295-L307) —— 每次 `pop_back()`（取最后一个 = 最晚注册的 = 最该先拆的），调 `Srv->onDetach(...)`，把「剩下的 Service 列表」塞进 `onDetach` 的完成回调里。也就是说：**当前 Service 的 `onDetach` 调用 `OnComplete()` 之后，才会递归处理前一个 Service**。这样即使 `onDetach` 是异步的（完成回调在将来某刻才调），顺序依然严格保持。列表空了就 `completeDetach()`。

[lib/executor/Session.cpp:309-322](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L309-L322) —— `completeDetach`：若是纯 `detach`（`TargetState == Detached`），清零 `TargetState` 结束；若是 `shutdown`（`TargetState == Shutdown`），进入阶段 2。

**阶段 2 的核心：排空托管代码**

[lib/executor/Session.cpp:324-327](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L324-L327) —— 只有短短两行：注册「排空后调 `proceedToShutdown`」的回调，然后 `close()` 关闭发 Token 的窗口。

为什么这两行能实现「等待」？要看 `TaskGroup` 的语义：

[include/orc-rt/TaskGroup.h:197-208](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L197-L208) —— `addOnComplete`：若 `TaskGroup` 还没 close 或还有任务（`NumTasks > 0`），把回调存进列表；否则**立即执行**回调。

[include/orc-rt/TaskGroup.h:166-178](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L166-L178) —— `releaseToken`：每次 Token 析构就 `--NumTasks`，一旦 `NumTasks == 0 && Closed`，就把所有 `OnComplete` 回调取出来执行。这就是「最后一个 Token 释放时触发 `proceedToShutdown`」的机制。

换句话说，阶段 2 的「等待」是**事件驱动**而非**忙等或 `CV.wait`**：`proceedToShutdown` 被推迟到最后一个托管 Token 释放的那一刻。如果此刻没有任何托管代码在跑，`close()` 时 `NumTasks` 已为 0，回调近乎立即触发。

**阶段 3 的核心：逆序 `onShutdown` 与唤醒析构**

[lib/executor/Session.cpp:329-340](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L329-L340) —— `proceedToShutdown`：加锁快照 Service、把 `CurrentState` 设成 `Shutdown`、解锁，然后 `shutdownServices`。

[lib/executor/Session.cpp:342-351](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L342-L351) —— `shutdownServices` 与 `detachServices` 完全同构的递归逆序。

[lib/executor/Session.cpp:353-361](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L353-L361) —— `completeShutdown`：把 `TargetState` 清回 `None`，`CV.notify_all()` 唤醒等待者。

**析构如何等待**：

[lib/executor/Session.cpp:59-65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L59-L65) —— `~Session`：先调 `shutdown()`（保证至少开始关闭），再用条件变量 `CV.wait` 等待 `CurrentState == Shutdown && TargetState == None`。这正是 `completeShutdown` 达到的条件。所以析构一定会阻塞到三阶段全部完成。

> 小贴士：`CV.wait(Lock, 谓词)` 是条件变量的「谓词等待」用法——它会循环检查谓词、不满足就睡眠，避免「惊群」和「虚假唤醒」带来的逻辑错误。这里谓词就是「生命周期彻底结束」。

#### 4.3.4 代码实践

**实践目标 1（源码标注）**：对照 `Session.cpp`，给 `shutdown` 的三阶段标注代码位置。

**操作步骤**：在下表中填入行号（答案见下）：

| 阶段 | 触发入口 / 实现函数 | 文件:行号 |
|------|---------------------|-----------|
| 阶段 1 Detach（断控制器） | `shutdown` 里取 `CA` 并调 `TmpCA->disconnect()` | `Session.cpp: ____` |
| 阶段 1 Detach（从未 attach） | `shutdown` 里 `proceedToDetach(Lock, nullptr)` | `Session.cpp: ____` |
| 阶段 1 Detach（通知 Service） | `proceedToDetach` → `detachServices` 逆序 `onDetach` | `Session.cpp: ____ ~ ____` |
| 阶段 2 Drain | `waitForManagedCodeTasksThenShutdown`（`addOnComplete` + `close`） | `Session.cpp: ____ ~ ____` |
| 阶段 3 Shutdown services | `proceedToShutdown` → `shutdownServices` 逆序 `onShutdown` | `Session.cpp: ____ ~ ____` |
| 收尾唤醒 | `completeShutdown`（`TargetState=None` + `CV.notify_all`） | `Session.cpp: ____ ~ ____` |

**参考答案**：202；187；295–307；324–327；342–351；353–361。（断控制器入口对应 `Session.cpp:202` 的 `TmpCA->disconnect()`；`Start` 分支对应 `Session.cpp:187`；其余如上。）

---

**实践目标 2（编写测试）**：编写一个测试——在持有 `ManagedCode Token` 时发起 `shutdown`，验证 `onShutdown` 会被延迟，直到 Token 释放。

这是本讲第二个核心实践。 orc-rt 已经有一个几乎完全对应的测试 `ActiveManagedCallsDelayShutdown`，我们先读懂它，再把它当作模板。

**操作步骤**：

1. 阅读 [test/unit/SessionTest.cpp:466-500](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L466-L500)（`ActiveManagedCallsDelayShutdown`）。
2. 按下面四步理解它的断言：
   - **第 479-480 行**：从 `S.managedCodeTokenSource()` 取一个 Token `Tok`，断言成功（说明 shutdown 还没开始，Token 窗口还开着）。
   - **第 484 行**：发起 `S.shutdown([&]{ ShutdownComplete = true; })`。
   - **第 487-489 行**：断言 `DetachOpIdx == 0`（阶段 1 已完成）、`ShutdownOpIdx` 仍为空（阶段 3 还没开始）、`ShutdownComplete` 仍为 false。**这就是「onShutdown 被延迟」的证据**。
   - **第 493 行**：再取一个 Token，断言**失败**——因为 `waitForManagedCodeTasksThenShutdown` 已经调了 `close()`，窗口关了。
   - **第 495 行**：`Tok = TaskGroup::Token();` 把持有的 Token 重置（释放）。
   - **第 497-499 行**：此刻最后一个 Token 释放，`releaseToken` 触发 `addOnComplete` 回调，`proceedToShutdown` 被调，`onShutdown` 终于执行；于是 `ShutdownOpIdx == 1`、`ShutdownComplete == true`。
3. **自己动手**：在 `SessionTest.cpp` 里仿照它新增一个测试（示例代码，非项目原有代码）：

```cpp
// 示例代码：复现「持有 Token 时 shutdown 被延迟」
TEST(SessionTest, MyTokenDelaysShutdown) {
  QueueingRunner<>::WorkQueue Tasks;
  Session S(mockExecutorProcessInfo(), QueueingRunner(Tasks), noErrors);

  size_t OpIdx = 0;
  std::optional<size_t> DetachOpIdx, ShutdownOpIdx;
  S.createService<MockService>(DetachOpIdx, ShutdownOpIdx, OpIdx);

  // 1. 持有一个托管 Token
  auto Tok = TaskGroup::Token(S.managedCodeTokenSource());
  ASSERT_TRUE(Tok);

  // 2. 发起 shutdown
  bool ShutdownComplete = false;
  S.shutdown([&]() { ShutdownComplete = true; });

  // 3. 阶段1已发生，阶段3被阻塞
  EXPECT_EQ(DetachOpIdx, 0U);
  EXPECT_FALSE(ShutdownOpIdx);
  EXPECT_FALSE(ShutdownComplete);

  // 4. 释放 Token —— onShutdown 才会运行
  Tok = TaskGroup::Token();
  EXPECT_EQ(ShutdownOpIdx, 1U);
  EXPECT_TRUE(ShutdownComplete);
}
```

**需要观察的现象**：在第 3 步，`ShutdownOpIdx` 为空、`ShutdownComplete` 为 false，证明 `onShutdown` 被卡在阶段 2；第 4 步释放 Token 后，二者才更新。

**预期结果**：测试通过，与 `ActiveManagedCallsDelayShutdown` 行为一致。

**如果无法确定运行结果**：上述断言与现有测试 `ActiveManagedCallsDelayShutdown` 一一对应，行为可预期；若未配置构建环境，标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设阶段 2 不存在（即 `completeDetach` 之后直接 `proceedToShutdown`，不等托管代码），会出什么问题？

> **参考答案**：如果在某条线程的栈上还有托管代码（JIT 代码）正在执行，而这些代码可能回调 `Session` 持有的某个 `Service`（比如请求分配内存）。一旦跳过 Drain 直接 `onShutdown` 并销毁资源，那段还在跑的托管代码就会访问已释放的 `Service`/内存，导致 use-after-free。Drain 阶段就是用 Token 计数保证「没有任何托管代码在栈上」之后才继续。

**练习 2**：`proceedToDetach` 在解锁后才执行 `TmpCA.reset()`（丢弃控制器，第 289 行）。为什么要在锁外做这件事？

> **参考答案**：`shared_ptr` 的析构（可能触发 `ControllerAccess` 的析构及其内部清理）可能耗时，甚至可能回调进 `Session`。在持锁状态下做这种「可能重入、可能慢」的操作容易导致死锁或长时间持锁。把 `CurrentState` 设好、解锁之后再 `reset`，是「持锁时间最小化」的标准做法。

**练习 3**：`~Session()` 里 `CV.wait` 的谓词是 `CurrentState == Shutdown && TargetState == None`。如果只判 `CurrentState == Shutdown` 会怎样？

> **参考答案**：不够。`proceedToShutdown` 一进来就把 `CurrentState = Shutdown`（第 336 行），但此时 `onShutdown` 可能还没跑完、`TargetState` 还是 `Shutdown`。若析构只等 `CurrentState == Shutdown`，可能在 `onShutdown` 还在进行时就销毁对象。必须同时要求 `TargetState == None`——它只在 `completeShutdown`（全部 `onShutdown` 跑完后）才被清零，这才是「真正结束」的标志。

---

## 5. 综合实践

把三个模块串起来，完成下面这个贯穿任务。

**任务**：在一张纸上画出 `Session` 从构造到析构的「完整时序」，并标注每一步对应的源码函数与行号。具体要求：

1. 画出一个 `Session` 的生命周期：构造（`Start`）→ `attach`（`Attached`）→ `shutdown`（经三阶段到 `Shutdown`）→ 析构返回。
2. 在 `shutdown` 段内，标出三阶段的边界函数：`proceedToDetach` / `detachServices` / `completeDetach` / `waitForManagedCodeTasksThenShutdown` / `proceedToShutdown` / `shutdownServices` / `completeShutdown`，并注明每个函数在 [Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) 的行号。
3. 在图上额外画一条「并发线」：表示在 `onDetach` 执行期间，另一个线程调了 `shutdown()`。用箭头标出它走了 4.2 的「合并意图」分支（`TargetState = max(...)`），并解释为什么驱动链不会在 `completeDetach` 停下。
4. 在图上再画一个「持有 Token」的场景：在阶段 2，`close()` 已调用但 `NumTasks > 0`，用一条「等待」线表示 `proceedToShutdown` 被推迟到 Token 释放。

**自检问题**（做完后回答）：

- 析构函数在哪一行、用什么谓词等待生命周期结束？
- 如果 `attach` 从未被调用过，`shutdown` 会走 `switch` 的哪个 `case`？对应的 `proceedToDetach` 第二参数是什么？

**参考答案要点**：析构在 `Session.cpp:59-65`，谓词 `CurrentState == Shutdown && TargetState == None`；从未 attach 时走 `case State::Start:`（`Session.cpp:186-188`），`proceedToDetach(Lock, nullptr)` 的第二参数是空 `shared_ptr`（没有控制器要断）。

---

## 6. 本讲小结

- `Session` 用一个**单调向前**的四态状态机（`Start → Attached → Detached → Shutdown`）描述生命周期，外加 `None` 作为「无待处理请求」的占位符。
- 核心是**两个状态变量**：`CurrentState`（已发生的事实）与 `TargetState`（被要求的意图）。两者的落差就是「正在进行的工作」。
- 并发协调靠「抢驱动权」：第一个把 `TargetState` 从 `None` 改非 `None` 的线程成为驱动者，负责一气呵成推进；后来者只用 `std::max` 合并意图后返回。`shutdown` 的意图天然包含 `detach`。
- `shutdown` 分三阶段：**Detach**（断控制器 + 逆序 `onDetach`）→ **Drain**（关 Token 窗口、等所有托管 Token 释放）→ **逆序 `onShutdown`**。两个逆序都用「`pop_back` + 在完成回调里递归」的手法保证严格顺序、且兼容异步 `onDetach`/`onShutdown`。
- 阶段 2 的「等待」是**事件驱动**的：`TaskGroup::addOnComplete` 注册的 `proceedToShutdown`，只在「已 close 且计数归零」时由最后一个 `releaseToken` 触发。
- `~Session()` 会调 `shutdown()` 并用条件变量阻塞到 `CurrentState == Shutdown && TargetState == None`，保证对象内存被回收时绝无托管代码还在引用它。

---

## 7. 下一步学习建议

本讲把 `Session` 的「骨架」——状态机——讲透了。接下来：

- **u3-l3 Service 接口与注册**：本讲多次提到 `onDetach`/`onShutdown`，下一讲会正式展开 `Service` 抽象、三种注册方式（`addService`/`createService`/`tryCreateService`）与逆序关闭的来龙去脉。建议先读 [include/orc-rt/Service.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h)。
- **u4-l1 TaskGroup、Token 与托管代码**：本讲只把 `TaskGroup` 当作「计数器」用，下一讲会深入 Token 的获取/释放/`close` 协议，以及 `callManagedCode` 的同步包裹语义。
- **u5-l3 ControllerAccess**：本讲的 `disconnect`/`notifyDisconnected` 只是骨架调用，真正的 `ControllerAccess` 契约（断开时排空 pending 调用的 drain 协议、三条完成路径）留到 RPC 通信层讲。
- 继续阅读 [docs/Design.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md) 中「Managed code execution and shutdown」一节，对照本讲的状态机加深理解。
