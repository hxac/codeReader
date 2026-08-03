# ControllerAccess：执行端↔控制端桥

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Session::ControllerAccess` 这个嵌套抽象类的**四个核心方法**（`connect` / `disconnect` / `callController` / `sendWrapperResult`）与**一个通知回调**（`notifyDisconnected`）各自的契约。
- 解释 `notifyDisconnected` 的「恰好一次（exactly-once）」要求，以及为什么断开必须是双向安全的（控制端先掉、执行端先掉都能正确收敛）。
- 复述断开时的 **drain 协议**：为何必须在 `notifyDisconnected` 之前用 `failPendingControllerCall` 把所有在飞调用排空，并理解它与 `callController` 的串行化关系。
- 区分一条 controller 调用的**三条完成路径**——`handleControllerCallResult`（拿到正常结果）、`failPendingControllerCall`（断开 drain）、`failControllerCallInline`（断开瞬间内联失败），知道每条路径何时使用、在哪个线程、是否领 Token。

本讲承接 [u2-l1 Controller–Executor 架构全景](u2-l1-controller-executor-architecture.md)（控制端↔执行端的不对称 RPC 与 wrapper function「字节进字节出」）与 [u5-l2 Wrapper Function 签名与 call/handle](u5-l2-wrapper-function-call-handle.md)（统一异步签名、Return 回调、Caller/Serializer 解耦），把视角从「单次 wrapper 调用的收发」拉高到「承载这些调用的双向通道本身的生命周期」。

## 2. 前置知识

本讲默认你已经掌握以下概念（在前序讲义中建立）：

- **controller / executor 二分**：控制端链接 LLVM ORC 库负责编译链接，执行端链接 orc-rt 负责执行 JIT 代码。两者可同进程、可跨进程。
- **方向不对称**：控制端→执行端按**地址（address）**调用（可触达执行端任意代码）；执行端→控制端按 **tag** 调用（只能命中控制端显式登记的入口）。这是最小权限设计。
- **wrapper function 统一签名**：`void(Session, ArgBytes, Return, CallId)`——返回 `void` 意味着「完成」只能靠回调 `Return` 通知；`CallId` 用于配对调用与结果。
- **带外错误（out-of-band error）**：`WrapperFunctionBuffer` 的一个特殊状态，用于在「字节通道」上承载失败信号，成功路径零开销（见 u5-l1）。
- **Session 生命周期状态机**：`Start → Attached → Detached → Shutdown`，`CurrentState`/`TargetState` 双变量协调，detach 与 shutdown 都会断开 controller（见 u3-l2）。
- **托管代码 Token**：执行 JIT 代码前必须从 `ManagedCodeTaskGroup` 领 Token，否则 shutdown 会释放其内存导致 use-after-free（见 u4-l1）。

**一个关键直觉**：跨进程通道随时可能「掉线」。一次 controller 调用从「发出」到「结果回来」之间，可能跨越任意长时间，而通道可能在任意时刻断开。因此本讲的核心问题不是「怎么调用」，而是「**调用发出去了但通道断了，那条在飞的调用该怎么办？**」ControllerAccess 抽象的全部设计都围绕这个「半完成的调用如何安全收尾」展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/orc-rt/Session.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | `Session::ControllerAccess` 嵌套抽象类的**全部契约**：四个纯虚方法、`notifyDisconnected`、以及 Session 提供给子类调用的三条完成路径辅助函数都在这里（内联声明）。 |
| [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) | `doAttach`（驱动 `connect` 并处理其同步失败）、`handleDisconnect`（`notifyDisconnected` 的落点），以及 C ABI 入口 `orc_rt_Session_callController`。 |
| [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | `MockControllerAccess`（一个可双向调用、带 drain 的全功能实现）与 `DeadControllerAccess`（最简实现，专门演示内联失败路径），外加一组 `ControllerAccessTest` 用例。这是理解契约的最佳参考。 |
| [docs/Design.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md) | 设计文档的 `ControllerAccess` 与 `WrapperFunction` 小节，给出不对称 RPC 与 wrapper 签名的权威描述。 |

> **阅读策略**：先读 `Session.h` 里 `ControllerAccess` 类的大段注释（它们就是契约的正文），再对照 `SessionTest.cpp` 里的 `MockControllerAccess` 看「一个正确的实现长什么样」，最后用 `DeadControllerAccess` 与各 `ControllerAccessTest` 用例验证你对每条路径的理解。

## 4. 核心概念与源码讲解

### 4.1 ControllerAccess 抽象与契约

#### 4.1.1 概念说明

`ControllerAccess` 是 `Session` 的一个**嵌套抽象基类**，代表「执行端到控制端的那条通道」。它由 Session 通过 `attach` / `tryAttach` 模板构造并独占持有（见 u3-l1 的「不可拷贝、不可移动、稳定地址引用」），客户端永远不直接持有 `ControllerAccess` 对象。

它的职责是**双向 RPC**：

- **入站**：控制端调用执行端——`ControllerAccess` 收到调用后通过 `handleWrapperCall` 交给 Session 处理。
- **出站**：执行端调用控制端——Session 调用 `callController`，`ControllerAccess` 负责把请求送出去，并在结果回来时完成回调。

它有**四个必须实现的纯虚方法**和**一个必须遵守的语义约束**：

| 方法 | 方向 | 语义 |
| --- | --- | --- |
| `connect(BootstrapInfo)` | 建立 | 与控制端建立连接，把引导信息送过去 |
| `disconnect()` | 拆除 | 请求断开连接，必须先 drain 再 `notifyDisconnected` |
| `callController(OnComplete, T, ArgBytes)` | 出站 | 按向控制端发起一次 wrapper 调用 |
| `sendWrapperResult(ResultBytes, CallId)` | 入站回包 | 把某次 wrapper 调用的结果送回控制端 |
| `notifyDisconnected()`（语义约束） | 通知 | 通道断开时**恰好一次**通知 Session |

注意一个不对称：`connect` / `disconnect` 是 Session **主动调用**子类的方法（生命周期事件），而 `callController` / `sendWrapperResult` 是 Session **委托**子类去搬运字节（数据通道）。`notifyDisconnected` 则反过来——是子类**回调** Session 来宣告「通道没了」。

#### 4.1.2 核心流程

一次完整的 controller 通道生命周期：

```
Session::attach / tryAttach
        │  构造 ControllerAccessT，doAttach 存下 shared_ptr<CA>
        ▼
   CA->connect(BootstrapInfo)            ── 子类建立连接
        │  连接成功？
        │     ├─ 是 → Session 进入 Attached
        │     └─ 否 → 子类须在 connect 内调 notifyDisconnected
        │            → Session 走 detach 流程
        ▼
   [Attached 期间]
   - 控制端调执行端：CA->handleWrapperCall → Session 处理
   - 执行端调控制端：Session::callController → CA->callController
   - 结果回送：CA->sendWrapperResult
        │
        ▼
   Session::detach / shutdown，或控制端先掉线
        │  Session 调 CA->disconnect（至多一次）
        ▼
   CA->disconnect 内部 drain：把所有在飞出站调用 failPendingControllerCall
        │
        ▼
   CA->notifyDisconnected()              ── 恰好一次通知 Session
        │
        ▼
   Session::handleDisconnect → proceedToDetach → 逆序 onDetach
```

其中 `connect` 与 `disconnect` 的**失败语义**特别值得记：

- **`connect` 失败**：子类必须在返回前调用 `notifyDisconnected`，这样 Session 会走 detach 而非停在半连接状态。
- **`disconnect` 的「至多一次 + 恰好一次」组合**：Session 保证只调一次 `disconnect`；但控制端可能自己先掉线（网络中断），所以子类可能在 `disconnect` 之前就已经因远端掉线而调过 `notifyDisconnected`。此时再来一次 `disconnect` 必须是**无操作（no-op）**。无论从哪一侧触发，`notifyDisconnected` 全程**恰好一次**。

这正是真实网络通道的写照：断开永远是双向竞争的，实现必须对「双重断开」幂等。

#### 4.1.3 源码精读

**ControllerAccess 类骨架与构造**——它持有 Session 的引用，子类构造时第一个参数恒为 Session：

[include/orc-rt/Session.h:95-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L95-L99) 定义了 `ControllerAccess` 类与虚析构；[include/orc-rt/Session.h:140](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L140) 的 `ControllerAccess(Session &S) : S(S) {}` 是受保护构造，把 Session 引用绑死。

**`connect` 契约**——核心是「失败须在返回前调 `notifyDisconnected」」：

```cpp
// 如果 connect 无法建立与控制端的通信，
// ControllerAccess 实现必须在从 connect 返回前调用 notifyDisconnected。
virtual void connect(BootstrapInfo BI) = 0;
```

见 [include/orc-rt/Session.h:142-158](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L142-L158)。注意注释里另一条：`ControllerAccess` 实现不得在 `connect` 被调用之前调用 `handleWrapperCall`（即没连上就别收调用）。

Session 侧的 `doAttach` 正是为这个失败语义兜底——它调完 `connect` 后检查状态，能区分三种结局（成功 / connect 已 notifyDisconnected / 并发被请求 detach）：

[lib/executor/Session.cpp:82-126](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L82-L126) 中，`CA->connect(std::move(BI))` 之后会重新加锁判断：若 `TargetState == Attached` 则直接落户（情况 1）；若 `CurrentState >= Detached` 说明 `notifyDisconnected` 已经把 detach 跑完了（情况 2），直接 bail out；否则是并发请求 detach（情况 3），更新状态后补调 `CA->disconnect()`。

**`notifyDisconnected` 恰好一次**——它是子类回调 Session 的唯一入口，转交 `Session::handleDisconnect`：

```cpp
/// 当控制端断开时，ControllerAccess 实现必须恰好一次调用此方法，
/// 无论断开是由 disconnect、由控制端、还是由通信故障触发。
void notifyDisconnected() { S.handleDisconnect(); }
```

见 [include/orc-rt/Session.h:223-235](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L223-L235)。注释还规定：调用 `notifyDisconnected` 之后，**不得再调** `reportError`、`handleWrapperCall` 或 `handleControllerCallResult`。

Session 侧 [lib/executor/Session.cpp:270-276](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L270-L276) 的 `handleDisconnect` 把 `TargetState` 提到 `Detached`，并用 `std::atomic_exchange` 把 `CA` 置空（取出所有权），再 `proceedToDetach`——这意味着「我们自己不需要再调 disconnect 了」，因为通道已经断了。

**出站调用的入口（未 attach 的兜底）**——Session 公开的 `callController` 用原子读 `CA`，若为空则用带外错误直接完成回调：

[include/orc-rt/Session.h:483-491](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L483-L491)，关键那句：

```cpp
else
  OnComplete(WrapperFunctionBuffer::createOutOfBandError("no controller attached"));
```

这正是 u2-l1 提到的「未 attach 时立即返回带外错误 `"no controller attached"`」的来源。它把「没有通道」这一情况也统一成了「一次失败的调用」。

#### 4.1.4 代码实践

**实践目标**：验证 `connect` 失败时，子类按契约调用 `notifyDisconnected`，错误能被 reporter 捕获，且 Session 不会卡在半连接状态。

**操作步骤**（源码阅读 + 本地验证）：

1. 打开 [test/unit/SessionTest.cpp:858-871](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L858-L871) 的 `FailConnect` 用例。
2. 阅读它如何把 `OnConnect` 钩子设成 `return make_error<StringError>(ErrMsg);`，并让 reporter 断言收到 `"failed to connect"`。
3. 对照 [include/orc-rt/Session.h:155-158](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L155-L158) 的 connect 契约，理解 `MockControllerAccess::connect`（[SessionTest.cpp:155-162](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L155-L162)）里这段顺序：

   ```cpp
   if (auto Err = OnConnect(BI)) {
     reportError(std::move(Err));   // 1) 先把错误报给 Session
     notifyDisconnected();          // 2) 再宣告通道已断（触发 detach）
   }
   ```

4. 本地构建并运行该用例：

   ```bash
   # 在已配置好 LLVM runtimes 的构建目录中
   cmake --build . --target check-orc-rt-unit
   ```

**需要观察的现象**：`GotError` 为 `true`，且 `ErrMsg == "failed to connect"`；用例退出后 Session 析构正常返回（没有死锁或卡住）。

**预期结果**：用例通过。这说明 connect 失败被正确转化为一次 `reportError` + 一次 `notifyDisconnected`，Session 经 `handleDisconnect → proceedToDetach` 收敛到 Detached，析构时再走完 shutdown。

> 若你无法本地构建，可标注「待本地验证」，仅做源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connect` 失败时必须由子类自己（而不是 Session）调用 `notifyDisconnected`？

**参考答案**：因为 `connect` 是子类的方法，只有子类知道「建立连接」是否真的失败了（例如 socket 是否 bind 成功、握手是否完成）。Session 调用 `connect` 后无法判断失败原因，只能通过观察 `notifyDisconnected` 是否被调用来推断。所以契约把责任放在子类：失败就必须在返回前 `notifyDisconnected`，Session 在 `doAttach` 里据此判断该走哪条收尾路径。

**练习 2**：`disconnect` 被设计成「Session 至多调用一次，但实现必须容忍额外的并发调用」，请结合真实通信场景解释。

**参考答案**：真实通道断开是双向竞争的——可能是 Session 主动 `detach`/`shutdown` 触发 `disconnect`，也可能是远端控制端先掉线导致实现已经自发调过 `notifyDisconnected`。后一种情况下，Session 仍可能随后调用 `disconnect`（例如 shutdown 流程中），实现必须把它当作 no-op，否则就会重复 `notifyDisconnected`，破坏「恰好一次」契约。

---

### 4.2 断开时的 drain 协议

#### 4.2.1 概念说明

「drain（排空）」是断开流程里最微妙的一环。问题来自一个时间差：

- 执行端调用 `callController` 后，请求被**送进通道**，结果**还没回来**。这条调用叫 **pending（在飞）调用**，它的完成回调 `OnComplete` 还攥在 `ControllerAccess` 手里。
- 这时通道断开。这些 pending 调用**永远不会收到结果**了。
- 但它们的 `OnComplete` 回调**必须被触发**——否则发起调用的代码（可能是 JIT 代码、可能是 Session 内部逻辑）会永远等下去，或者持有已经无效的 Token。

所以 `disconnect` 不能简单粗暴地「丢掉所有 pending 调用」，而必须**逐个失败它们**。这个「把所有在飞调用排空」的过程就叫 drain。

drain 协议有三条硬约束（来自 [include/orc-rt/Session.h:181-188](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L181-L188) 的 `disconnect` 注释）：

1. **必须在 `notifyDisconnected` 之前完成**——因为 drain 用的是 `failPendingControllerCall`，它会领一个托管代码 Token 并断言 TaskGroup 仍然 open；而 `notifyDisconnected` 会触发 detach，detach 之后 TaskGroup 会被 close。所以 drain 必须赶在 close 之前。
2. **drain 必须与 `callController` 里的「断开检测」串行化**——一个正在发起、与断开竞争的调用，要么被 drain 掉（已经进 pending 表），要么在 `callController` 里被内联失败（还没进表），**绝不能两边都没管它**。
3. **drain 出来的完成回调会被派发（dispatch）而非丢弃**——`failPendingControllerCall` 在一个 fresh Token 下跑回调，回调里可能安全地触碰托管代码。

#### 4.2.2 核心流程

`disconnect` 的标准实现骨架：

```
disconnect():
    1. 在锁内：置 Shutdown 标志（之后 callController 一律走内联失败）
    2. 在锁内：等待「没有进行中的出站调用」
       （因为可能有调用已送进通道、正在等结果，它们的完成回调还攥在 pending 表里）
    3. 在锁内：把 pending 表整体搬走（ToDrain = move(PendingOut)）
    4. 解锁，逐个 failPendingControllerCall(OnComplete)  ← drain
    5. notifyDisconnected()   ← 必须在 drain 之后
```

注意第 2 步「等待没有进行中调用」是为了处理一类竞态：`callController` 可能正把一条调用**加入** pending 表（此时该调用的结果还可能回来），drain 必须等它要么真的进了表（之后被 drain）、要么因为看到 Shutdown 而内联失败，才能安全地搬走 pending 表。

用条件变量表达「等到 Outstanding 归零」是一个经典的「引用计数 + 关闭标志」模式：

\[
\text{可以搬走 pending 表} \iff (\text{Shutdown} \land \text{Outstanding} = 0)
\]

#### 4.2.3 源码精读

`MockControllerAccess::disconnect` 是 drain 协议的标准范例：

[SessionTest.cpp:164-180](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L164-L180)：

```cpp
void disconnect() override {
  std::unordered_map<size_t, OnControllerCallReturn> ToDrain;
  {
    std::unique_lock<std::mutex> Lock(M);
    Shutdown = true;                       // 1) 置关闭标志，此后 callController 走内联失败
    ShutdownCV.wait(Lock, [this]() {       // 2) 等到 Outstanding==0（没有进行中的出站调用）
      return Shutdown && Outstanding == 0;
    });
    ToDrain = std::move(PendingOut);       // 3) 搬走 pending 表
  }
  for (auto &[_, OnComplete] : ToDrain)    // 4) 逐个排空
    failPendingControllerCall(std::move(OnComplete));
  notifyDisconnected();                    // 5) 通知 Session（必须在 drain 之后）
}
```

注意第 4 步在锁外执行——`failPendingControllerCall` 会通过 Session 派发 Task，若持锁则会与 worker 线程死锁。

那 `Outstanding` 这个引用计数是谁维护的？看 `ConnectGuard`：

[SessionTest.cpp:87-119](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L87-L119)，它是一个 RAII 类型：构造时 `++Outstanding`（在锁内，假设调用者已持锁），析构时 `--Outstanding`，并且当 `Shutdown && Outstanding == 0` 时 `ShutdownCV.notify_all()`。

`callController` 用它来标记「本条调用正在出站」：

[SessionTest.cpp:182-209](https://github.com/llvm/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L182-L209)，关键片段：

```cpp
if (!Shutdown) {
  CG = ConnectGuard(this);          // 计数 +1，标记「我正在出站」
  CId = CallId++;
  PendingOut[CId] = std::move(OnComplete);  // 进 pending 表
} else
  BailOut = true;
// ... 锁外 ...
if (BailOut)
  return failControllerCallInline(std::move(OnComplete));  // 内联失败
```

这正是约束 2「drain 与 callController 串行化」的体现：`Shutdown` 标志是分水岭——置 `Shutdown` 之前进入的调用进了 `PendingOut`（会被 drain），之后进入的走 `BailOut`（内联失败），二者互斥且无遗漏。

> drain 用的 `failPendingControllerCall` 转发给 Session 的私有实现 [include/orc-rt/Session.h:608-611](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L608-L611)，它其实只是「以一个 `disconnectError()` 结果调 `handleControllerCallResult`」——见下一节。

#### 4.2.4 代码实践

**实践目标**：把 `MockControllerAccess::disconnect` 的 drain 拆成可复述的步骤清单，并定位每一步对应的源码行。这是本讲的主实践任务。

**操作步骤**：

1. 打开 [SessionTest.cpp:164-180](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L164-L180) 的 `disconnect`。
2. 对照下面的「drain 步骤清单」，逐行填入它对应的源码行号与作用：

   | 步骤 | 源码位置 | 作用 |
   | --- | --- | --- |
   | ① 置关闭闸门 | `Shutdown = true`（行 ~173） | 之后新发起的 `callController` 一律走内联失败，不再进 pending 表 |
   | ② 等进行中调用归零 | `ShutdownCV.wait(..., Outstanding == 0)`（行 ~174） | 等所有已进表但未完成的出站调用结束对 Outstanding 的占用 |
   | ③ 搬走 pending 表 | `ToDrain = std::move(PendingOut)`（行 ~175） | 把待排空调用移出临界区，避免持锁派发 |
   | ④ 逐个排空 | `for (...) failPendingControllerCall(...)`（行 ~177-178） | 在 fresh Token 下、TaskGroup 仍 open 时，以 `"disconnected"` 失败每条调用 |
   | ⑤ 通知 Session | `notifyDisconnected()`（行 ~179） | 触发 `handleDisconnect → proceedToDetach`，必须在 drain 之后 |

3. 接着回答一个关键问题：**步骤 ② 等待的 `Outstanding` 由谁增减？** 追到 [ConnectGuard 的析构（行 106-117）](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L106-L117)，确认它在析构时 `--Outstanding`，并在 `Shutdown && Outstanding == 0` 时唤醒 CV。

4. （可选，本地验证）在 `disconnect` 的第 ④ 步前临时加一行日志（标注「示例代码」），统计 `ToDrain.size()`，构造一条「发起 callController 后立刻 shutdown」的场景，观察 drain 是否非空。

**需要观察的现象**：如果存在与断开竞争的 pending 调用，第 ④ 步的 `ToDrain` 非空，且每条都被失败；`notifyDisconnected` 在循环结束之后才被调用。

**预期结果**：你能复述 5 个步骤及顺序，并解释「为何第 ④ 步在锁外、第 ⑤ 步在 drain 之后」。

#### 4.2.5 小练习与答案

**练习 1**：如果实现把第 ⑤ 步 `notifyDisconnected()` 误放到第 ① 步之前（先通知再 drain），会发生什么？

**参考答案**：`notifyDisconnected` 会触发 Session 的 `proceedToDetach`，最终关闭 `ManagedCodeTaskGroup`。而第 ④ 步的 `failPendingControllerCall` 会调用 `handleControllerCallResult`，后者会尝试领 Token 并断言 TaskGroup 仍然 open——此时 TaskGroup 已被 close，领 Token 失败，触发 `assert(false)` 与 `abort()`。所以顺序错误会让进程直接崩溃。

**练习 2**：为什么 drain 的第 ④ 步 `failPendingControllerCall` 要在锁**外**执行？

**参考答案**：`failPendingControllerCall` 会通过 Session 的 `Dispatch` 派发一个 Task（在 fresh Token 下运行 `OnComplete`）。若派发用的是 `QueueingRunner` 或线程池，被派发的 Task 可能回头需要访问 `MockControllerAccess` 的内部状态（例如继续触发调用、操作 pending 表），若此时仍持有 `M` 锁就会自死锁。把 drain 移到锁外是避免「派发后回调又来抢锁」的经典做法。

---

### 4.3 三种 callController 完成路径

#### 4.3.1 概念说明

每一条出站 controller 调用都关联一个**完成处理器** `OnControllerCallReturn`，它**必须被完成恰好一次**。Session 用一个不透明包装类把它交给子类，子类**不能直接调用**它，只能通过 Session 提供的**三条路径之一**来完成：

| 路径 | 何时用 | 在哪运行 | 是否领 Token | 是否经 Dispatch |
| --- | --- | --- | --- | --- |
| `handleControllerCallResult(OnComplete, ResultBytes)` | 控制端正常返回结果 | 派发到一个 fresh Task | 是（fresh） | 是 |
| `failPendingControllerCall(OnComplete)` | 断开时排空已在 pending 表的调用 | 派发到一个 fresh Task | 是（fresh） | 是（结果就是 `disconnectError()`） |
| `failControllerCallInline(OnComplete)` | `callController` 内发现正在断开，调用注定无法发出 | **内联**，在调用者栈上 | **否**（借用调用者的） | 否 |

这三条路径的设计动机是「**让完成处理器总在一个能安全触碰托管代码的上下文里运行**」：

- 前两条是**延迟完成**：结果在未来的某个时刻到达，那时原始调用者的栈早已不在，所以必须领一个**新的 Token** 来保护处理器。Session 还断言此时 TaskGroup 仍 open（见 [include/orc-rt/Session.h:584-593](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L584-L593)），因为「延迟完成必须发生在 `notifyDisconnected` 之前」。
- 第三条是**同步失败**：调用者此刻正卡在 `callController` 里，它的栈还在，它自己的 Token（如果发起方是托管代码）还在覆盖这一帧，所以处理器**不需要**领新 Token，直接内联跑即可。

`OnControllerCallReturn` 这个类型本身只是对真实回调 `OnControllerCallReturnFn = move_only_function<void(WrapperFunctionBuffer)>` 的不透明包装，子类只能 `std::move` 它、用 `operator bool` 判空，看不到内部（见 [include/orc-rt/Session.h:126-138](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L126-L138)）。这种封装强制子类走三条受控路径，杜绝「子类直接 invoke 回调却忘了领 Token」。

#### 4.3.2 核心流程

三条路径的触发时机与流向：

```
                     一条出站 controller 调用
                              │
               callController 内：检查是否正在断开？
                              │
        ┌───────── 还连着 ─────────┘  └──────── 正在断开 ────────┐
        │                                                       │
   进 pending 表                                          failControllerCallInline
        │                                                  （内联、借用 Token、
        │                                                   不经 Dispatch、
   等结果回来                                              结果="disconnected"）
        │
   ┌────┴─────────────────┐
   │                      │
收到结果              通道断开（drain）
   │                      │
handleControllerCallResult   failPendingControllerCall
（派发、领 fresh Token、      （派发、领 fresh Token、
  结果=控制端返回）           结果="disconnected"）
```

三个出口都**恰好触发一次** `OnComplete`，区别只在「结果内容」「是否派发」「是否领 Token」。

#### 4.3.3 源码精读

**三条路径的契约正文**就在 `OnControllerCallReturn` 上方那段大注释里：

[include/orc-rt/Session.h:102-125](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L102-L125)，逐条说明了三条路径各自「谁触发、在哪跑、Token 怎么来」。结尾那句点题：「Together these guarantee the handler always runs exactly once -- with a result or a disconnect error.」

**路径 1：`handleControllerCallResult`**（控制端正常回包）——子类的 `sendWrapperResult`/`returnFromController` 收到结果后调用：

[include/orc-rt/Session.h:245-255](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L245-L255) 是转发壳；真正的实现在 [include/orc-rt/Session.h:579-599](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L579-L599)：

```cpp
TaskGroup::Token T(ManagedCodeTaskGroup);
if (!T) {
  // 契约违反：延迟完成必须早于 notifyDisconnected（此时组仍 open）；
  // 同步失败必须用 failControllerCallInline。到这里说明契约被破坏，
  // 放任继续会在可能已 teardown 的 Session 上无 Token 跑处理器。
  assert(false && "handleControllerCallResult on a closed ManagedCodeTaskGroup");
  abort();
}
Dispatch([OnComplete = std::move(OnComplete.Wrapped),
         ResultBytes = std::move(ResultBytes),
         T = std::move(T)]() mutable { OnComplete(std::move(ResultBytes)); });
```

注意 Token `T` 被 move 进了 Task lambda——这与 u4-l2 讲的「Token 随 Task 移动」一脉相承：无论 Task 在哪个线程跑，处理器都被 Token 覆盖。

**路径 2：`failPendingControllerCall`**（断开 drain）——它就是「以 disconnect 错误走路径 1」：

[include/orc-rt/Session.h:257-266](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L257-L266) 是转发壳；实现在 [include/orc-rt/Session.h:608-611](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L608-L611)，它复用了 `handleControllerCallResult` + `disconnectError()`：

```cpp
void failPendingControllerCall(
    ControllerAccess::OnControllerCallReturn OnComplete) {
  handleControllerCallResult(std::move(OnComplete), disconnectError());
}
```

注释特意指出它「不接收 result，所以失败值不可能被写错」——这是一种用类型签名防错的设计。`disconnectError()`（[include/orc-rt/Session.h:604-606](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L604-L606)）返回 `createOutOfBandError("disconnected")`，这是路径 2 与路径 3 共用的「断开错误」常量。

**路径 3：`failControllerCallInline`**（断开瞬间内联失败）——这是唯一**不领 Token、不经 Dispatch** 的路径：

[include/orc-rt/Session.h:268-280](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L268-L280) 是转发壳；实现在 [include/orc-rt/Session.h:613-619](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L613-L619)：

```cpp
void failControllerCallInline(
    ControllerAccess::OnControllerCallReturn OnComplete) {
  // 内联运行，在调用者栈上，不带 Token：
  // 仅对来自 callController 内的同步失败合法，此时调用者（及其 Token，
  // 若有）仍在栈上。见 callController。
  OnComplete.Wrapped(disconnectError());
}
```

它**只允许在 `callController` 内部**调用——因为只有那时调用者的栈还在，借用其 Token 才安全。一个已经被成功入队（进 pending 表）的调用绝不能用这条路径，必须用路径 2。

**最简范例 `DeadControllerAccess`**——专门演示路径 3：

[SessionTest.cpp:632-643](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L632-L643)：

```cpp
class DeadControllerAccess : public Session::ControllerAccess {
public:
  DeadControllerAccess(Session &S) : ControllerAccess(S) {}
  void connect(BootstrapInfo) override {}
  void disconnect() override { notifyDisconnected(); }
  void callController(OnControllerCallReturn OnComplete,
                      orc_rt_ControllerHandlerTag, WrapperFunctionBuffer) override {
    failControllerCallInline(std::move(OnComplete));   // 唯一用路径 3
  }
  void sendWrapperResult(WrapperFunctionBuffer, uint64_t) override {}
};
```

它的 `callController` 永远直接走路径 3——因为「连接从一开始就不存在」。配套测试 [SessionTest.cpp:645-668](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L645-L668) 的 `SynchronousCallControllerFailureRunsInline` 用 `noDispatch`（[CommonTestUtils.h:55-58](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L55-L58)，派发就 `ADD_FAILURE`）作为 Session 的 `Dispatch`，**反向证明**路径 3 没有走 Dispatch，而是在 `callController` 返回前就内联完成了处理器，且错误字符串恰为 `"disconnected"`。

**对照：路径 1 的真实用例** `ValidCallToController`——控制端正常返回结果：

[SessionTest.cpp:730-745](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L730-L745) 用 `controllerCaller` 发起一次 `add_sps_wrapper`（41+1），结果经 `MockControllerAccess` 模拟的「控制端」回包，最终在 `returnFromController`（[SessionTest.cpp:235-249](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L235-L249)）里走 `handleControllerCallResult` 完成回调，断言 `Result == 42`。

#### 4.3.4 代码实践

**实践目标**：用一个**永不连上**的 ControllerAccess，亲手验证路径 3（内联失败）的行为：处理器在 `callController` 返回前就跑完、错误为 `"disconnected"`、且**没有**任何 Task 被 Dispatch。

**操作步骤**（基于现有测试改写）：

1. 阅读 [SessionTest.cpp:632-668](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L632-L668) 的 `DeadControllerAccess` 与 `SynchronousCallControllerFailureRunsInline`。
2. 在你的本地工作副本里**新建一个测试用例**（示例代码，不要改动已有用例）：

   ```cpp
   // 示例代码：演示路径 3（failControllerCallInline）
   TEST(ControllerAccessTest, MyInlineFailureCheck) {
     // noDispatch：一旦有 Task 被 Dispatch 就 ADD_FAILURE，
     // 因此它同时充当「必须内联、不经任务队列」的检查。
     Session S(mockExecutorProcessInfo(), noDispatch, noErrors);
     S.attach<DeadControllerAccess>(BootstrapInfo(S));

     bool HandlerRanBeforeReturn = false;
     std::string ErrMsg;
     S.callController(
         [&](WrapperFunctionBuffer Result) {
           // 若这里在 callController 返回前执行，下面的置位会生效
           HandlerRanBeforeReturn = true;
           if (const char *E = Result.getOutOfBandError())
             ErrMsg = E;
         },
         /*T=*/nullptr, WrapperFunctionBuffer());

     EXPECT_TRUE(HandlerRanBeforeReturn);  // 内联完成
     EXPECT_EQ(ErrMsg, "disconnected");    // 路径 3 的固定错误
   }
   ```

3. 构建 `check-orcrt-unit`（或对应的 GoogleTest 目标）并运行该用例。

**需要观察的现象**：

- `HandlerRanBeforeReturn == true`——处理器在 `callController` 返回前就跑完了。
- `ErrMsg == "disconnected"`——来自 `disconnectError()`。
- 用例通过，意味着 `noDispatch` 没有触发 `ADD_FAILURE`，证明路径 3 完全没经 Dispatch。

**预期结果**：用例通过。若你把 `DeadControllerAccess::callController` 里的 `failControllerCallInline` 误改成 `failPendingControllerCall`（路径 2），用例会因 `noDispatch` 触发 `ADD_FAILURE` 而失败——这正是三条路径「是否经 Dispatch」差异的可观测证据。

> 若无法本地构建，标注「待本地验证」，仅完成源码阅读部分。

#### 4.3.5 小练习与答案

**练习 1**：路径 2（`failPendingControllerCall`）和路径 3（`failControllerCallInline`）都产出 `"disconnected"` 错误，它们最本质的区别是什么？

**参考答案**：区别在于「调用是否曾被成功入队」。路径 2 处理的是**已经进了 pending 表、正在等结果**的调用——断开时它注定等不到结果，所以在 fresh Token 下、经 Dispatch 派发完成。路径 3 处理的是 `callController` 进来时**就已经在断开**、根本没机会入队的调用——此时调用者栈还在，借用调用者的 Token 内联完成即可，不领新 Token、不经 Dispatch。判据是 `callController` 里的「是否正在断开」检查（如 `MockControllerAccess` 的 `Shutdown` 标志）。

**练习 2**：为什么 `OnControllerCallReturn` 把真实回调藏成 private，只让子类 `std::move` 与 `operator bool`？

**参考答案**：为了强制子类只能通过 Session 提供的三条受控路径完成回调。如果回调直接暴露，子类可能在不领 Token 的上下文里随手 invoke 它，导致处理器跑进已 teardown 的托管代码而 use-after-free。把 invoke 能力收归 Session，由 Session 负责在每条路径上正确地领/借 Token 并（必要时）派发，就把「安全地完成一次调用」这件复杂的事集中到一处实现。

**练习 3**：`handleControllerCallResult` 里若领 Token 失败（TaskGroup 已 close）会 `abort()`。结合 drain 协议，解释这个 assert 在保护什么不变量。

**参考答案**：它保护「延迟完成必须发生在 `notifyDisconnected` 之前」这条不变量。drain 协议规定排空（路径 2，底层就是 `handleControllerCallResult`）必须在 `notifyDisconnected` 之前完成，而 `notifyDisconnected` 会触发 detach 进而 close 掉 `ManagedCodeTaskGroup`。所以如果在 close 之后还能走到 `handleControllerCallResult`，说明某条延迟完成被错误地推迟到了 detach 之后——这会让处理器在无 Token、Session 可能已 teardown 的状态下运行。assert 把这种契约违例变成显式崩溃，而不是隐蔽的内存错误。

## 5. 综合实践

把三条路径与 drain 协议串起来，做一个「**断开竞争**」的端到端阅读实验。

**场景**：执行端发起一条 controller 调用，与此同时 Session 收到 `shutdown`。这条调用要么被 drain（路径 2），要么被内联失败（路径 3），取决于它与断开的先后。

**任务**：

1. 在 `MockControllerAccess` 里，定位决定「走路径 2 还是路径 3」的那个标志（`Shutdown`）与那个临界区（[SessionTest.cpp:190-199](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L190-L199)）。
2. 写一段说明，论证**任意一条 controller 调用在断开期间不可能既不被 drain 也不被内联失败**（即 drain 与 `callController` 的串行化无遗漏）。提示：`Shutdown` 的置位与 `PendingOut` 的插入在**同一把锁 `M`** 下；`Shutdown` 一旦为真，新调用一律 `BailOut`（路径 3），而 `Shutdown` 为假时插入的调用一定进了 `PendingOut`（路径 2 drain）。
3. 把 `disconnect` 的步骤清单（4.2.4 的五步）与 `callController` 的分支（4.3.2 的流程图）画在**同一张时序图**上，标出 `M` 锁覆盖的范围、`ShutdownCV` 的 wait/notify、以及三条路径各自触发 `OnComplete` 的位置。
4. 进阶：解释为什么 `Outstanding` 引用计数 + `ShutdownCV` 是必要的——若 `disconnect` 不等待 `Outstanding==0` 直接搬走 `PendingOut`，会丢掉哪类调用？（提示：一条正在 `callController` 锁内、即将插入 `PendingOut` 但还没插入的调用。）

完成本任务后，你应当能用一句话回答：「一条 controller 调用在断开期间会发生什么？」——答案是「它要么被 drain 掉、要么被内联失败，二者互斥且穷尽，处理器恰好被完成一次」。

## 6. 本讲小结

- `ControllerAccess` 是 Session 的嵌套抽象，承载执行端↔控制端的双向 RPC，由 Session 通过 `attach`/`tryAttach` 独占持有；它有四个纯虚方法（`connect`/`disconnect`/`callController`/`sendWrapperResult`）和一个语义约束 `notifyDisconnected`。
- `notifyDisconnected` 必须**恰好一次**触发，且要容忍双向断开（Session 主动断 + 远端先掉）：远端先掉时，后续 Session 发起的 `disconnect` 必须是 no-op。
- `connect` 失败时，子类须在返回前调 `notifyDisconnected`，Session 的 `doAttach` 据此走 detach 收尾。
- **drain 协议**：`disconnect` 必须在 `notifyDisconnected` 之前，用 `failPendingControllerCall` 排空所有在飞调用；drain 须与 `callController` 的断开检测串行化，保证「要么被 drain、要么被内联失败」，无遗漏。
- 一条 controller 调用的完成处理器 `OnControllerCallReturn` 必须**恰好被完成一次**，且只能经三条路径之一：`handleControllerCallResult`（正常结果，fresh Token + Dispatch）、`failPendingControllerCall`（断开 drain，等价于以 `"disconnected"` 走前者）、`failControllerCallInline`（断开瞬间内联失败，借用调用者 Token、不经 Dispatch）。
- 三条路径的区分本质是「调用是否曾被入队」与「完成时调用者栈是否还在」；Session 用私有化 `OnControllerCallReturn` 的回调、加 assert 与 `noDispatch` 测试，把这套安全约束落到了代码里。

## 7. 下一步学习建议

- **进入真实实现**：本讲的 `MockControllerAccess` 是测试用的「双端都在本进程」的实现。下一站读 [include/orc-rt/InProcessControllerAccess.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/InProcessControllerAccess.h) 与对应 [u8-l2 InProcessControllerAccess：同进程桥](u8-l2-in-process-controller-access.md)，看一个生产用的、用函数指针表而非 IPC 的 ControllerAccess 如何落实这些契约。
- **向上一层**：ControllerAccess 把字节搬运出去后，字节如何被解释？继续 [u6 序列化方案：SPS](u6-l1-simple-packed-serialization.md)，理解 `callController` 的 `ArgBytes`/结果字节是如何用 SPS 方案做类型安全序列化的（呼应 [u6-l2 SPSWrapperFunction](u6-l2-sps-wrapper-function.md) 的 `controllerCaller`）。
- **扩展实践**：当你想自己写一个 ControllerAccess（例如基于某种真实传输），直接进入 [u11-l2 编写自定义 ControllerAccess](u11-l2-custom-controller-access.md)，那里会要求你完整实现四个方法并遵守 drain 契约——本讲是其前置。
- **回看状态机**：若你对 `notifyDisconnected → handleDisconnect → proceedToDetach` 这条链如何嵌入 Session 状态机仍有疑问，重读 [u3-l2 生命周期状态机](u3-l2-session-lifecycle.md) 的 `proceedToDetach` 一节。
