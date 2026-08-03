# TaskGroup、Token 与托管代码

## 1. 本讲目标

本讲聚焦 orc-rt 中一个看起来很「小」、却支撑了整个执行端安全关闭机制的组件：**托管代码（managed code）的 Token 协议**。

读完本讲，你应该能够：

1. 说出什么是「托管代码」、为什么 `Session` 在关闭时必须等待它退出。
2. 读懂 `TaskGroup` / `Token` / `TokenSource` 三者的生命周期与「计数 + 关闭」协议。
3. 说出 `Session::callManagedCode` 这个同步包裹工具的返回值语义，以及为什么 Token **只能覆盖同步执行栈、不能跨异步延续**。
4. 对照源码，解释 `shutdown` 为什么要先排空 `ManagedCodeTaskGroup`、再调用各 `Service::onShutdown`，以及「取一个 Token 就能让 shutdown 卡住」的现象从何而来。

本讲承接 [u3-l2 生命周期状态机](u3-l2-session-lifecycle.md)：那一讲里 shutdown 的「Drain 阶段」被一带而过，本讲正是把 Drain 阶段拆开讲的。

## 2. 前置知识

- **托管代码（managed code）**：在这里它指「由 `Session` 管理其生命周期的、JIT 出来的代码」——包括真正 JIT 编译的机器码，以及为 JIT 代码加载的库代码（如动态库里的符号）。它和某些语言里「带垃圾回收的代码」无关。
- **为什么必须等它退出**：`Session` 关闭时会释放 JIT 内存、卸载动态库。如果此刻某段 JIT 代码的栈帧还活着，等控制权回到那个栈帧时，它要执行的指令或要访问的数据可能已经被释放，程序会立刻崩溃。这是典型的 use-after-free。
- **RAII**：C++ 的「资源获取即初始化」——构造时获取资源、析构时释放。`Token` 就是一个 RAII 对象。
- **`std::shared_ptr`**：带引用计数的智能指针。`TaskGroup` 用 `shared_ptr` 持有，`Token` 也持有一份，因此「只要有 Token 还在，TaskGroup 对象就不会被销毁」。
- 本讲默认你已经学过 [u3-l1 Session 对象与构造](u3-l1-session-object.md) 和 [u3-l2 生命周期状态机](u3-l2-session-lifecycle.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/orc-rt/TaskGroup.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h) | 定义 `TaskGroup`、`Token`、`TokenSource` 三个类。本讲的主角。 |
| [include/orc-rt/Session.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | `Session` 把一个 `TaskGroup` 用作 `ManagedCodeTaskGroup`，并提供 `callManagedCode` / `managedCodeTokenSource` 两个入口。 |
| [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) | shutdown 的 Drain 阶段在此实现：`waitForManagedCodeTasksThenShutdown`。 |
| [test/unit/TaskGroupTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/TaskGroupTest.cpp) | TaskGroup 的单元测试，覆盖了 Token 的拷贝/移动/关闭等各种行为。 |
| [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | `CallManagedCodeVoidFn/NonVoidFn/AsyncFn` 与 `ActiveManagedCallsDelayShutdown` 测试，是本讲代码实践的范本。 |
| [docs/Design.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md) | 「Managed code execution and shutdown」一节给出了 Token 协议的设计动机。 |

---

## 4. 核心概念与源码讲解

### 4.1 TaskGroup 与 Token 生命周期

#### 4.1.1 概念说明

`TaskGroup` 是一个**通用的「一组任务完成追踪器」**。它本身和 JIT、和 controller 没有任何关系——它只做一件事：用引用计数记录「当前还有几个未完成的任务」，并在「已关闭 + 计数归零」的那一刻触发 `OnComplete` 回调。

`Session` 借用了这个通用能力：它持有一个 `TaskGroup` 作为 `ManagedCodeTaskGroup`，每一段即将运行托管代码的逻辑都从这个组里领一个 `Token`（令牌），Token 销毁时归还。于是「组里还有 Token」就等价于「还有托管代码在栈上跑」。

围绕 `TaskGroup` 有三个角色，要严格区分：

- **`TaskGroup`**：被追踪的「组」，内部维护 `Closed` 标志、`NumTasks` 计数、`OnCompletes` 回调列表。
- **`Token`**：一个 RAII 令牌。构造时调用 `acquireToken()`（计数 +1），析构时调用 `releaseToken()`（计数 −1）。**有 Token 存在，组的计数就大于 0，`OnComplete` 就不会被触发。**
- **`TokenSource`**：对 `shared_ptr<TaskGroup>` 的一个**强引用包装**，**只**用来从中构造 `Token`，自己不持令牌、不阻止关闭。它的存在意义是：「我可以晚点再尝试领令牌，并且即使原来的 `TaskGroup` 引用没了，我也保证那个组对象还活着，领令牌的操作是良定义的。」

#### 4.1.2 核心流程

`TaskGroup` 的完整生命周期可以用下面这组不变量描述：

```
状态变量：Closed (bool), NumTasks (size_t), OnCompletes (vector)

不变量 1：OnComplete 回调「恰好触发一次」当且仅当
         Closed == true 且 NumTasks 从 >0 减到 0。
不变量 2：close() 之后，acquireToken() 永远返回 false（不再发新令牌）。
不变量 3：已经触发过 OnComplete 的组，再次 close / 再次归零都不会重复触发。
```

一次典型的「领令牌 → 关组 → 释放令牌」流程：

```
1. Token T(TG);            // acquireToken(): NumTasks 1→...  Closed 仍 false
2. TG->close();            // Closed=true，但 NumTasks>0，回调暂不触发
3. ... 执行托管代码 ...
4. }  // T 析构: releaseToken(): NumTasks →0 且 Closed → 触发 OnCompletes
```

若在步骤 1 之前就 `close()` 了（即 `CloseWithNoTokens` 场景），`close()` 内部当场就会发现 `NumTasks==0 && Closed`，**立即**触发回调。

#### 4.1.3 源码精读

先看 `TaskGroup` 的三个数据成员，它们就是全部状态：

这是 [TaskGroup.h:L219-L222](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L219-L222) ——一个互斥锁 `M`、一个关闭标志 `Closed`、一个任务计数 `NumTasks`、一个完成回调列表 `OnCompletes`。

`acquireToken()` 在持锁状态下检查 `Closed`：已关闭则拒绝（返回 `false`），否则计数加一。见 [TaskGroup.h:L155-L161](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L155-L161)：

```cpp
bool acquireToken() noexcept {
  std::scoped_lock<std::mutex> Lock(M);
  if (Closed)
    return false;
  ++NumTasks;
  return true;
}
```

`releaseToken()` 是触发回调的关键点。它先在**锁内**把计数减一，若「计数归零且已关闭」就把回调列表 `move` 出来搬到局部变量 `ToRun`；**释放锁之后**才逐个调用回调——这个「锁外执行」的细节很重要，避免回调里再次操作 `TaskGroup` 时死锁。见 [TaskGroup.h:L166-L178](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L166-L178)。

`close()` 与之同构：置 `Closed=true`，若此刻已经没有令牌就立即触发回调，否则等最后一个令牌释放时触发。见 [TaskGroup.h:L182-L193](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L182-L193)。

`addOnComplete()` 还处理了一个边界情况：**如果注册回调时组已经关闭且没有令牌，回调当场就执行**（而不是塞进列表）。见 [TaskGroup.h:L197-L208](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L197-L208)。这正是 `Session::addOnShutdown` 在「已经 shutdown」时能立即回调的底层支撑。

接下来看 `Token`。`Token` 内部只持有一个 [TaskGroup.h:L139](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L139) 的 `std::shared_ptr<TaskGroup> G`——这是整件事的灵魂：

- **领令牌**：从 `TaskGroup` 或 `TokenSource` 构造，见 [TaskGroup.h:L117-L126](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L117-L126)。注意它调用 `acquireToken()`，**只有成功才接管 `shared_ptr`**；失败时 `G` 保持空，`operator bool()` 返回 `false`。
- **复制令牌**：[TaskGroup.h:L68-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L68-L71) 拷贝构造**会再次 `acquireToken()`**——也就是计数再加一。复制可能失败（组已关闭），所以注释反复强调「拷贝后必须用 `operator bool()` 检查」。
- **销毁令牌**：[TaskGroup.h:L130-L133](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L130-L133) 析构时调用 `releaseToken()`，可能触发回调。
- **有效性检查**：[TaskGroup.h:L136](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L136) 的 `explicit operator bool()`。

由于 `Token` 持有 `shared_ptr`，**只要还有一个 Token 活着，`TaskGroup` 对象就不会被析构**——测试 `TokenKeepsTaskGroupAlive`（[TaskGroupTest.cpp:L290-L305](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/TaskGroupTest.cpp#L290-L305)）专门验证了这一点：原始 `TG` 引用超出作用域后，因为 `Token T` 还在，组对象存活、回调一直延迟到 `T` 被清空。

最后看 `TokenSource`。它的全部实现就是 [TaskGroup.h:L37-L45](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L37-L45)：持有一个 `shared_ptr<TaskGroup>`，**不暴露任何操作**，只让 `friend class Token` 能拿到它去构造令牌。测试 `TokenSourceDoesNotHoldToken`（[TaskGroupTest.cpp:L347-L355](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/TaskGroupTest.cpp#L347-L355)）证明：只持有 `TokenSource` 不领令牌，组照样会立刻完成——它「保活对象」但不「占计数」。

> **设计要点**：把「保活」和「占计数」拆成两个类型（`TokenSource` vs `Token`），是为了让 `Session` 能安全地把「领令牌的入口」以 `TokenSource` 形式交给外部，而外部即便一直攥着这个入口，也不会无意中推迟 `Session` 关闭。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Token 的「计数 + 关闭 + 回调」协议，特别是「先领令牌再关组、令牌释放才触发回调」这一核心行为。

**操作步骤**（这是一个可加入 `test/unit/TaskGroupTest.cpp` 的源码阅读型实践，对照已有测试风格）：

1. 阅读 [TaskGroupTest.cpp:L27-L39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/TaskGroupTest.cpp#L27-L39) 的 `SingleTokenThenClose`，理解最朴素的「令牌作用域 vs 回调时机」。
2. 仿照它写一段逻辑：创建 `TaskGroup`，注册一个把布尔置真的 `addOnComplete` 回调；用花括号限定 `Token` 的作用域，**在作用域内**调用 `close()`。
3. 分别在「令牌作用域内」「令牌作用域结束后」检查回调标志。

**参考代码**（示例代码，直接复用了仓库里 `SingleTokenThenClose` 的形态）：

```cpp
// 示例代码：可放进 TaskGroupTest.cpp 作为新 TEST
TEST(TaskGroupTest, MyTokenDrill) {
  bool Completed = false;
  auto TG = TaskGroup::Create();
  TG->addOnComplete([&]() { Completed = true; });

  {
    TaskGroup::Token T(TG);   // 领令牌：NumTasks = 1
    EXPECT_TRUE(T);
    TG->close();              // Closed = true，但 NumTasks > 0
    EXPECT_FALSE(Completed);  // 回调还没触发
  }                           // T 析构：NumTasks = 0 → 触发回调
  EXPECT_TRUE(Completed);
}
```

**需要观察的现象**：

- `close()` 之后、令牌析构之前，`Completed` 必须仍是 `false`。
- 令牌作用域一结束，`Completed` 立刻变 `true`。
- 再额外试一次：把 `Token T(TG);` 这一行**移到** `close()` 之后，此时 `T` 应为无效（`EXPECT_FALSE(T)`），这正是 [TaskGroupTest.cpp:L49-L54](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/TaskGroupTest.cpp#L49-L54) 的 `TokenFromClosedGroup` 验证的行为。

**预期结果**：回调被「恰好触发一次」，且时机严格落在「最后一个令牌释放」的那一刻。若想本地运行，按 [u1-l2](u1-l2-build-and-config.md) 构建 `check-orc-rt-unit` 目标即可执行这些 GoogleTest。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把一个 `Token` 长期存在某个全局容器里（比如 `std::vector<TaskGroup::Token>`），会发生什么？

> **参考答案**：`TaskGroup` 的 `NumTasks` 永远降不到 0，`OnComplete` 永远不触发。若这个组是 `Session` 的 `ManagedCodeTaskGroup`，则 `Session` 的 shutdown 会**永久卡死**在 Drain 阶段。`TaskGroup.h` 在 [TaskGroup.h:L56-L58](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L56-L58) 的注释里专门警告了这一点：「Avoid storing Tokens in long-lived data structures」。

**练习 2**：`Token` 的拷贝构造和移动构造，对 `NumTasks` 的影响有什么不同？

> **参考答案**：拷贝构造会再调一次 `acquireToken()`，`NumTasks + 1`，组里现在有两个令牌；移动构造只是 `std::swap` 内部 `shared_ptr`，`NumTasks` **不变**——令牌的所有权换了主人，但「未完成的任务数」没变。见 [TaskGroup.h:L68-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L68-L71) 与 [TaskGroup.h:L95](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L95)。`CopyToken` / `MoveToken` 两个测试分别印证。

---

### 4.2 callManagedCode 同步包裹

#### 4.2.1 概念说明

每次要运行托管代码都手动写「领令牌 → 检查有效性 → 执行 → 令牌析构」太啰嗦，而且容易漏掉「检查有效性」这一步。`Session` 提供了一个模板工具 `callManagedCode`，把这套流程封装成一个**同步调用**：

- 它尝试从 `ManagedCodeTaskGroup` 领一个令牌；
- 成功就调用你传入的函数（令牌覆盖这次同步调用），返回结果；
- 失败（说明 `Session` 已经在关闭）就**不调用**你的函数，返回一个表示「被拒绝」的值。

它的返回值分两种情况，用模板特化区分：

| 被调函数返回类型 | 调用成功返回 | 令牌被拒（未调用）返回 |
| --- | --- | --- |
| `void` | `bool` `true` | `bool` `false` |
| 非 `void` 的 `T` | `std::optional<T>`（含值） | `std::optional<T>`（`std::nullopt`） |

#### 4.2.2 核心流程

```
callManagedCode(Fn, args...)
  ├─ Token Tok(ManagedCodeTaskGroup)   // 领令牌
  ├─ if (!Tok)                         // 组已关闭
  │     return nullopt / false         // 不调 Fn，直接返回「被拒」
  └─ return Fn(args...)                // 同步调用，Tok 在本栈覆盖
                                        // Fn 返回后 Tok 析构、释放令牌
```

#### 4.2.3 源码精读

入口在 [Session.h:L471-L476](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L471-L476)：

```cpp
template <typename FnT, typename... ArgTs>
decltype(auto) callManagedCode(FnT &&Fn, ArgTs &&...Args) {
  return ManagedCodeCaller<std::invoke_result_t<FnT, ArgTs...>>::call(
      TaskGroup::Token(ManagedCodeTaskGroup), std::forward<FnT>(Fn),
      std::forward<ArgTs>(Args)...);
}
```

它先用 `std::invoke_result_t` 推出 `Fn` 的返回类型，据此选择 `ManagedCodeCaller<T>` 或 `ManagedCodeCaller<void>` 的特化，再把「一个临时构造的 `Token`」连同函数和参数一起传进去。注意 `TaskGroup::Token(ManagedCodeTaskGroup)` 是一个**临时对象**——它的生命周期延续到整条完整表达式的末尾，也就是 `call` 返回之时。这正是「令牌只覆盖这次同步调用」的实现方式。

非 void 版本的 helper 见 [Session.h:L52-L60](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L52-L60)：令牌无效返回 `std::nullopt`，否则返回 `Fn(args...)` 的结果（被 `optional` 包了一层）。

void 版本的 helper 见 [Session.h:L63-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L63-L71)：令牌无效返回 `false`，否则调用 `Fn` 后返回 `true`。

入口文档 [Session.h:L442-L470](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L442-L470) 用大段注释点明了本讲最重要的一条**边界**：令牌只覆盖 `Fn` 在本线程同步执行期间内联跑的代码；`Fn` 把工作**推迟**到它返回之后再跑（比如存起来以后执行、或丢给别的线程）的，**不在覆盖范围内**。Design.md 的 [docs/Design.md:L83-L87](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L83-L87) 用一句话总结了原因：异步续体（continuation）的入口点本身可能就是 JIT 代码，它没法在进入时再给自己套一个令牌，所以**调用续体的那一方**必须自己领一个新令牌。

> **直觉**：把令牌想象成「在当前函数栈帧上贴的一张护身符」。函数一返回、栈帧一拆，护身符就失效。你交给别人「以后再跑」的活儿，跑的时候这个栈帧早没了。

`Session` 还有另一个领令牌入口 `managedCodeTokenSource()`，见 [Session.h:L438-L440](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L438-L440)：它返回一个 `TokenSource`，供需要**手动**控制令牌作用域的场景使用（例如 4.3 节要复现的「延迟 shutdown」）。

#### 4.2.4 代码实践

**实践目标**：验证 `callManagedCode` 的两条返回路径——关闭前成功调用并拿到结果、关闭后令牌被拒返回 `std::nullopt`。

**操作步骤**：

1. 打开 [SessionTest.cpp:L530-L555](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L530-L555) 的 `CallManagedCodeNonVoidFn`，它用一个 `managedNonVoidFunction(int N){ return N + 1; }`（定义在 [SessionTest.cpp:L530](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L530)）做被调函数。
2. 用 [CommonTestUtils.h:L46-L48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L46-L48) 的 `mockExecutorProcessInfo()`、`noDispatch`、`noErrors` 三个工具构造一个最小 `Session`（`noDispatch` 定义在 [CommonTestUtils.h:L55-L58](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L55-L58)，`noErrors` 在 [CommonTestUtils.h:L30](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L30)）。
3. 关闭前调用一次，断言返回 `optional` 含 `42`；调用 `waitForShutdown(S)`（[SessionTest.cpp:L326-L331](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L326-L331)）等关闭完成；关闭后再调用一次，断言返回 `std::nullopt`。

**参考代码**（示例代码，等价于仓库里的测试）：

```cpp
// 示例代码
static int doubled(int N) { return N * 2; }

TEST(MyManagedCode, BeforeAndAfterShutdown) {
  Session S(mockExecutorProcessInfo(), noDispatch, noErrors);

  auto R1 = S.callManagedCode(doubled, 21);
  EXPECT_TRUE(R1);
  EXPECT_EQ(*R1, 42);          // 关闭前：成功，返回 42

  waitForShutdown(S);

  auto R2 = S.callManagedCode(doubled, 21);
  EXPECT_EQ(R2, std::nullopt); // 关闭后：令牌被拒，函数根本没被调用
}
```

**需要观察的现象**：关闭后的那次调用，`doubled` 函数体**不会执行**（你可以加一行 `static int gCallCount = 0; ++gCallCount;` 在 `doubled` 里验证它只被调用过一次）。这正是「令牌被拒就不调用 Fn」的体现。

**预期结果**：`R1` 含 `42`，`R2` 为 `std::nullopt`。

**补充**：void 版本的行为见 [SessionTest.cpp:L504-L528](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L504-L528) 的 `CallManagedCodeVoidFn`（关闭前返回 `true`、关闭后返回 `false`）；异步函数的边界情况见 [SessionTest.cpp:L561-L587](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L561-L587) 的 `CallManagedCodeAsyncFn`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `callManagedCode` 对 void 函数返回 `bool`、对非 void 函数返回 `std::optional<T>`，而不统一用 `std::optional`？

> **参考答案**：`std::optional<void>` 在 C++17 里不是合法类型（`optional` 要求其模板参数是对象类型，`void` 不是对象类型）。所以 void 版本无法用 `optional` 表达「调用了 / 没调用」，只能退而用 `bool`。源码用两个 `ManagedCodeCaller` 特化分别处理这两种约束，见 [Session.h:L52-L71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L52-L71)。

**练习 2**：`callManagedCodeAsyncFn` 测试里，被调函数 `managedAsyncFunction` 接收一个 `Return` 回调并在内部调用它。这个 `Return` 回调的执行，是否被 `callManagedCode` 持有的令牌覆盖？

> **参考答案**：**是**——但前提是 `Return` 在 `managedAsyncFunction` 同步返回**之前**就被调用（就像测试里那样：`Return(++*P)` 紧接着发生）。因为此时仍在 `Fn` 的同步执行栈上，令牌还在。如果 `managedAsyncFunction` 把 `Return` 存起来、等函数返回后才在别处调用，那一次调用就**不在**令牌覆盖范围内了——这正是 [docs/Design.md:L83-L87](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L83-L87) 警告的异步边界。

---

### 4.3 关闭延迟语义

#### 4.3.1 概念说明

现在把前两节拼起来。`Session` 内部持有一个 `ManagedCodeTaskGroup`（就是一个 `shared_ptr<TaskGroup>`）。`shutdown` 的三阶段里（见 [u3-l2](u3-l2-session-lifecycle.md)），**第二阶段 Drain 就是等这个组的所有令牌释放**：

1. **Detach**：断开 `ControllerAccess`，逆序通知各 `Service::onDetach`。
2. **Drain**：给 `ManagedCodeTaskGroup` 注册一个「完成后 proceedToShutdown」的回调，然后 `close()` 它。从此**不再发新令牌**；`proceedToShutdown` 会一直等到所有在飞令牌释放才被触发。
3. **逆序 onShutdown**：逐个通知 `Service::onShutdown` 释放资源。

这套设计的根本目的，正是 [docs/Design.md:L72-L74](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L72-L74) 那句话：不能在 JIT 代码还活在栈上时就把它的代码和资源释放掉。

#### 4.3.2 核心流程

Drain 阶段的状态转移（承接 [u3-l2](u3-l2-session-lifecycle.md) 的状态机）：

```
CurrentState = Detached
  └─ completeDetach() 发现 TargetState == Shutdown
       └─ waitForManagedCodeTasksThenShutdown()
             ├─ ManagedCodeTaskGroup->addOnComplete(proceedToShutdown)
             └─ ManagedCodeTaskGroup->close()      // 关组：不再发新令牌
                  │
                  │  若此刻已有令牌在飞：
                  │    onShutdown 被挂起，等待……
                  │
                  └─ 最后一个令牌 releaseToken() → OnComplete 触发
                        └─ proceedToShutdown()      // 终于推进到 Shutdown
                              └─ shutdownServices(...)  // 逆序 onShutdown
```

两个关键性质：

- **close 之后停止发新令牌**：Drain 一开始就 `close()`，所以「领令牌被拒」是 shutdown 已进入 Drain 阶段的信号。`callManagedCode` 返回 `nullopt`/`false` 即源于此。
- **挂起的是 onShutdown，不是 onDetach**：onDetach 在 Detach 阶段已经跑完了；Drain 只挡 onShutdown。这一点在 `ActiveManagedCallsDelayShutdown` 测试里看得最清楚。

#### 4.3.3 源码精读

`ManagedCodeTaskGroup` 成员的声明见 [Session.h:L628](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L628)：`std::shared_ptr<TaskGroup> ManagedCodeTaskGroup = TaskGroup::Create();`——`Session` 一构造就建好这个组。

Drain 的实现极其精简，见 [Session.cpp:L324-L327](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L324-L327)：

```cpp
void Session::waitForManagedCodeTasksThenShutdown() {
  ManagedCodeTaskGroup->addOnComplete([this]() { proceedToShutdown(); });
  ManagedCodeTaskGroup->close();
}
```

就两步：把 `proceedToShutdown` 挂为完成回调，然后 `close()`。由于 `addOnComplete` 在「已关闭且无令牌」时会**当场执行**回调（见 4.1.3），所以若此刻没有任何托管代码在飞，`proceedToShutdown` 立刻被调用、shutdown 一气呵成；否则它会被存进回调列表，等到最后一个令牌释放时才触发。

`proceedToShutdown` 见 [Session.cpp:L329-L340](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L329-L340)：把 `CurrentState` 推进到 `Shutdown`，收集所有 Service 指针，交给 `shutdownServices` 逆序通知。注意它**先在锁内改状态、收集列表，再解锁**调 `shutdownServices`——和 `releaseToken`「锁外执行回调」是同一套防死锁手法。

最后 `completeShutdown`（[Session.cpp:L353-L361](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L353-L361)）把 `TargetState` 复位为 `None` 并 `CV.notify_all()`，唤醒可能在 `~Session()` 里等待的线程（`~Session` 会阻塞到 `CurrentState==Shutdown && TargetState==None`，见 [Session.cpp:L59-L65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L59-L65)）。

`Session` 内部还有两处领令牌的「正规入口」，都体现了「领不到就妥善处理」的契约：

- `handleWrapperCall`（收到 controller 发来的 wrapper 调用）在 [Session.h:L562-L577](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L562-L577) 领令牌；领不到（组已关闭）就直接 `return` 丢弃这次调用——注释说明此时 controller 应已自行报错。
- `handleControllerCallResult`（controller 返回了某次调用的结果）在 [Session.h:L579-L599](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L579-L599) 领令牌；这里领不到属于**契约违反**（完成回调必须在断开之前、即组仍开着时派发），所以直接 `assert(false)` + `abort()`。

#### 4.3.4 代码实践

**实践目标**：手动从 `managedCodeTokenSource()` 取一个 Token，亲眼看到它如何把 `shutdown` 卡在 Drain 阶段，直到 Token 释放。

**操作步骤**：

1. 打开 [SessionTest.cpp:L466-L500](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L466-L500) 的 `ActiveManagedCallsDelayShutdown`。它用一个 `QueueingRunner`（任务排队执行器，详见 [u4-l2](u4-l2-task-dispatch.md)）和一个记录回调顺序的 `MockService`。
2. 构造 `Session` 后，先 `auto Tok = TaskGroup::Token(S.managedCodeTokenSource());` 领一个令牌。
3. 发起 `S.shutdown(...)`，**立即**检查：`MockService` 的 `onDetach` 已发生（`DetachOpIdx == 0`），但 `onShutdown` 还没发生（`ShutdownOpIdx` 仍为空）、shutdown 完成回调也没跑。
4. 此时再尝试领令牌 `TaskGroup::Token(S.managedCodeTokenSource())`，应得到一个**无效**令牌——证明组已被 `close()`。
5. 把 `Tok` 清空（`Tok = TaskGroup::Token();`），观察 `onShutdown` 立即触发、shutdown 完成回调跑完。

**参考代码**（示例代码，精简自该测试）：

```cpp
// 示例代码
TEST(MyShutdownDrain, TokenDelaysShutdown) {
  QueueingRunner<>::WorkQueue Tasks;
  Session S(mockExecutorProcessInfo(), QueueingRunner(Tasks), noErrors);

  // 1) 领一个托管代码令牌
  auto Tok = TaskGroup::Token(S.managedCodeTokenSource());
  ASSERT_TRUE(Tok);

  // 2) 发起 shutdown
  bool ShutdownComplete = false;
  S.shutdown([&]() { ShutdownComplete = true; });

  // 3) shutdown 卡在 Drain：完成回调没跑
  EXPECT_FALSE(ShutdownComplete);

  // 4) 组已被 close：新令牌领不到
  ASSERT_FALSE(TaskGroup::Token(S.managedCodeTokenSource()));

  // 5) 释放令牌 → shutdown 推进
  Tok = TaskGroup::Token();
  Tasks.runFIFOUntilEmpty();   // 抽干排队任务，让回调有机会执行
  EXPECT_TRUE(ShutdownComplete);
}
```

**需要观察的现象**：

- 第 3 步：即使已经调了 `shutdown`，`ShutdownComplete` 仍为 `false`——因为 `Tok` 还攥着，`ManagedCodeTaskGroup` 计数非零，Drain 卡住。
- 第 4 步：`waitForManagedCodeTasksThenShutdown` 已经 `close()` 了组，所以新令牌构造失败。
- 第 5 步：令牌一释放（且排队任务被抽干），`proceedToShutdown` 才被触发，shutdown 走完。

**预期结果**：`ShutdownComplete` 从 `false` 变为 `true` 的转折点，严格落在 `Tok` 被清空之后。若 `QueueingRunner` 的任务没被 `runFIFOUntilEmpty` 抽干，回调可能尚未执行——这是单线程测试模型的注意点，生产环境用线程池则无需手动抽干（待本地验证具体时序）。

#### 4.3.5 小练习与答案

**练习 1**：在 `ActiveManagedCallsDelayShutdown` 里，为什么测的是 `onShutdown` 被延迟，而不是 `onDetach` 被延迟？

> **参考答案**：因为 Detach 阶段在 Drain **之前**。`onDetach` 在 `proceedToDetach` / `detachServices` 里就跑完了（见 [u3-l2](u3-l2-session-lifecycle.md)），那时还没动 `ManagedCodeTaskGroup`。Drain 挡住的只是后续的 `proceedToShutdown` → `onShutdown`。测试里 `DetachOpIdx == 0`（已发生）而 `ShutdownOpIdx` 为空（未发生）正反映了这一先后关系。

**练习 2**：假设你在写一个 `Service`，它的 `onShutdown` 里会回调一段 JIT 代码。这会出什么问题？

> **参考答案**：会死锁或令牌领不到。`onShutdown` 在 Drain **之后**才运行，那时 `ManagedCodeTaskGroup` 已经 `close()`，任何进入托管代码的尝试（`callManagedCode` 或手动领令牌）都会失败。若 `onShutdown` 里硬要调 JIT 代码，`callManagedCode` 会返回 `nullopt`/`false`，JIT 代码根本不会执行。正确的做法是：`Service` 应在 `onDetach` 之前就把「还需要 JIT 代码配合」的收尾工作做完，`onShutdown` 只做纯资源释放。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：**实现一个「带令牌守护的同步调用 + 观察延迟」的微型实验**。

任务：

1. 用 `mockExecutorProcessInfo` / `QueueingRunner` / `noErrors` 构造一个 `Session`。
2. 写一个被调函数 `int compute(int x)`，内部做点简单计算（比如 `return x * x;`）。
3. 用 `callManagedCode(compute, 7)` 调用它，断言返回 `49`。
4. 接着从 `S.managedCodeTokenSource()` 手动领一个 `Token Tok`，**不释放**，发起 `S.shutdown(...)`，断言 shutdown 没完成。
5. 期间再调一次 `callManagedCode(compute, 7)`——注意它**仍可能返回有效值还是 nullopt？** 先不查源码猜一下，再用源码验证你的猜测（提示：`close()` 是否已经发生？）。
6. 释放 `Tok`，抽干 `QueueingRunner`，断言 shutdown 完成。

要求：

- 在第 5 步写下你的预测，再对照 [Session.cpp:L324-L327](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L324-L327) 和 [Session.h:L471-L476](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L471-L476) 解释现象。
- 把整个过程画成一张时序图：横轴是时间，分别标出 `callManagedCode` 的两次返回值、`shutdown` 发起时刻、`Tok` 释放时刻、`onShutdown` 触发时刻。

> **提示（第 5 步）**：手动领的 `Tok` 让组计数为 1，但组**尚未被 close**（`close` 发生在 `waitForManagedCodeTasksThenShutdown` 里，而那要等 Detach 跑完才到）。所以第 5 步的 `callManagedCode` 在 Detach 完成前仍能领到令牌、返回有效值；只有 Detach 完成、Drain 开始 `close()` 之后，才会返回 `nullopt`。具体落在哪个分支，取决于 `shutdown` 推进到哪一步——这正是「待本地验证」的有趣之处。

## 6. 本讲小结

- `TaskGroup` 是一个通用的「计数 + 关闭 + 完成回调」追踪器：`acquireToken`/`releaseToken` 改计数，`close()` 后不再发新令牌，计数归零时触发 `OnComplete`。
- `Token` 是 RAII 令牌，持有 `shared_ptr<TaskGroup>`，因此「有 Token 在，组对象不灭、计数非零、回调不触发」；`TokenSource` 只保活对象、不占计数。
- `Session` 把一个 `TaskGroup` 用作 `ManagedCodeTaskGroup`，每段即将运行 JIT/托管代码的逻辑都从它领令牌，从而让 `Session` 知道「还有托管代码活在栈上」。
- `Session::callManagedCode` 是同步包裹：领令牌成功就调用并返回结果（`optional<T>` 或 `bool`），失败就不调用、返回 `nullopt`/`false`。
- Token **只覆盖同步执行栈**：函数返回后交给别人「以后再跑」的活儿不在覆盖范围内，调用续体的一方必须自己领新令牌。
- shutdown 的 Drain 阶段就是 `ManagedCodeTaskGroup->close()` + 挂一个 `proceedToShutdown` 回调；任何在飞令牌都会把 `onShutdown` 推迟到令牌释放之后。

## 7. 下一步学习建议

- **[u4-l2 任务分发：DispatchFn 与 Runner](u4-l2-task-dispatch.md)**：本讲的测试里反复出现的 `QueueingRunner` 到底怎么把 `Session` 派发的 Task 排队、抽干，是下一讲的主题。理解了它，你就能解释为什么综合实践里要手动 `runFIFOUntilEmpty`。
- **[u5-x RPC 通信层](u5-l1-wrapper-function-buffer.md)**：`handleWrapperCall` 在本讲露了一脸（领令牌 → 派发 Task）。它派发的 Task 内部跑的「wrapper function」是什么、字节怎么进出，留待 RPC 单元展开。
- **重读 [u3-l2](u3-l2-session-lifecycle.md)**：现在你已懂 Drain 的内部机制，回头再看 shutdown 三阶段的状态机，会有「原来那一笔带过的地方这么精巧」的体会。
- **动手读测试**：`test/unit/TaskGroupTest.cpp` 的并发测试（`ConcurrentTokens`、`ConcurrentAcquireAndClose`）展示了 Token 协议在多线程下的正确性保证，值得逐个读一遍。
