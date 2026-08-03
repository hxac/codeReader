# 任务分发：DispatchFn 与 Runner

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Session::DispatchFn` 这一个回调的职责边界——它只负责「把一个 Task 安排在何时何地运行」，而不关心 Task 的内容。
- 读懂两种「现成 Runner」：用于无线程环境与单元测试的 `QueueingRunner`，以及用于生产环境的 `ThreadPoolRunner`。
- 理解 Session 是如何把工作单元封装成 `Task` 并交给 `Dispatch` 的，以及为何 `Dispatch` 调用发生之前 Session 已经先领好了托管代码 Token。
- 能够参照测试代码，自己用 `QueueingRunner` 构造一个 Session、抽干队列、观察 Task 的执行顺序。

本讲承接 [u3-l1 Session 对象与构造](u3-l1-session-object.md) 中「构造 Session 需要三要素」的结论——`DispatchFn` 正是其中之一，并在 [u4-l1 TaskGroup、Token 与托管代码](u4-l1-taskgroup-managed-code.md) 的基础上，解释 Token 是如何「跟着 Task 走」的。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（它们在前置讲义中已建立）：

- **Session 是协调者而非执行者**：它构造时被注入三个回调（[u3-l1](u3-l1-session-object.md)），其中 `DispatchFn` 决定 Task「何时何地」运行，`ErrorReporterFn` 决定错误如何上报。
- **托管代码与 Token**（[u4-l1](u4-l1-taskgroup-managed-code.md)）：JIT 代码必须在持有 `TaskGroup::Token` 时运行，Token 在则 Session 不会关闭；Token 只覆盖同步执行栈。
- **`move_only_function`**：orc-rt 自带的「仅移动可调用对象」类型，是全库回调的通用载体（详见 [u10-l3 核心工具](u10-l3-core-utilities.md)）。本讲中 `Task` 和 `DispatchFn` 都基于它。

一个关键直觉先建立起来：**Session 不挑线程模型**。它可以把每个 Task 丢给线程池并发跑，也可以老老实实在单线程里排队跑。这套策略不是写死在 Session 里的，而是通过构造时传入的 `DispatchFn` 注入。本讲讲的两种 Runner，就是两种现成的 `DispatchFn` 策略实现。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `include/orc-rt/Session.h` | 定义 `Task`、`DispatchFn` 类型别名，以及 Session 在何处调用 `Dispatch`。 |
| `include/orc-rt/QueueingRunner.h` | 把 Task 排入调用者拥有的 `WorkQueue` 的 Runner（纯头文件模板），并提供 `runFIFOUntilEmpty`/`runLIFOUntilEmpty` 抽干方法。 |
| `include/orc-rt/ThreadPoolRunner.h` | 固定大小线程池 Runner 的接口声明。 |
| `lib/executor/ThreadPoolRunner.cpp` | 线程池 Runner 的实现（worker 循环、析构抽干）。 |
| `test/unit/CommonTestUtils.h` | 测试用的现成 `DispatchFn`：`noDispatch`、`inlineDispatch`，以及构造 Session 的占位工具。 |
| `test/unit/QueueingRunnerTest.cpp` | `QueueingRunner` 的单元测试，演示 FIFO/LIFO 与「抽干时新入队」的行为。 |
| `test/unit/SessionTest.cpp` | 用 `QueueingRunner` 构造 Session、抽干队列的真实测试范例。 |

记住一个反向定位规律：**测试文件名与被测源码一一对应**（`QueueingRunnerTest.cpp` ↔ `QueueingRunner.h`），看测试是理解 Runner 行为最快的路径。

## 4. 核心概念与源码讲解

### 4.1 DispatchFn：Session 与 Runner 之间的契约

#### 4.1.1 概念说明

`DispatchFn` 是 Session 构造三要素之一（与 `ExecutorProcessInfo`、`ErrorReporterFn` 并列，见 [u3-l1](u3-l1-session-object.md)）。它解决一个问题：

> Session 在运行过程中会不断产生「需要被执行的工作单元」——一次到来的 wrapper function 调用，或一个 controller 返回结果后的续体（continuation）。这些工作该立即跑、排队跑、还是丢到线程池跑？Session 自己**不决定**，而是把每个工作单元打包成一个 `Task`，交给 `DispatchFn`，由 `DispatchFn` 全权安排。

这是一种典型的**策略注入（dependency injection of an execution policy）**。它的好处是：同一个 Session 实现，既能用于多线程生产环境，也能用于单线程嵌入式环境，还能用于确定性的单元测试——只要换一个 `DispatchFn` 即可。

#### 4.1.2 核心流程

Session 把工作变成 Task 并交给 `Dispatch` 的流程，可以抽象为：

```text
事件（wrapper 调用 / controller 返回结果）
        │
        ▼
① 领取一个托管代码 Token（TaskGroup::Token）
   └─ 若领不到（组已关闭），直接放弃，不派发
        │
        ▼
② 把「真正要干的活」连同 Token 一起 move 进一个 lambda，
   这个 lambda 就是 Task（move_only_function<void()>）
        │
        ▼
③ 调用 Dispatch(Task)
        │
        ▼
④ 由具体的 Runner（QueueingRunner / ThreadPoolRunner / inlineDispatch）
   决定 Task 何时、在哪个线程上运行
        │
        ▼
⑤ Task 运行 → Token 析构 → 计数 -1
```

这里有一个极其重要的设计点，承接 [u4-l1](u4-l1-taskgroup-managed-code.md) 的「Token 只覆盖同步执行栈」：**Token 是在 ② 这一步被 `move` 进 Task lambda 的**。也就是说，无论 Task 最终在哪个线程、隔多久才运行，它都在那个被 move 进来的 Token 的覆盖之下。这就把「异步派发」与「Token 保护」解耦了——派发出去的 Task 自带 Token，不需要 Runner 懂托管代码协议。

#### 4.1.3 源码精读

先看类型定义。Session 用两个 `using` 把契约讲清楚（[include/orc-rt/Session.h:82-92](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L82-L92)）：

```cpp
/// A unit of work handed to the Session's DispatchFn for execution.
using Task = move_only_function<void()>;

/// Callback used by the Session to dispatch tasks for execution.
///
/// The Session builds a Task for each unit of work it needs run ...
/// and hands it to this callback, which is responsible for arranging
/// the task to be run inline, queued, or posted to a thread pool.
using DispatchFn = move_only_function<void(Task)>;
```

注意 `DispatchFn` 的签名是 `void(Task)`：它接收一个 Task，返回 void。它**不**返回任何结果——Task 的结果是通过别的通道（比如 `OnComplete` 回调）回传的，`Dispatch` 的唯一职责就是「确保这个 Task 最终被 `()` 调用一次」。

`DispatchFn` 是构造函数的第二个参数（[include/orc-rt/Session.h:299-300](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L299-L300)）：

```cpp
Session(ExecutorProcessInfo EPI, DispatchFn Dispatch,
        ErrorReporterFn ReportError);
```

构造函数体只做一件事：把三要素 `std::move` 存下来（[lib/executor/Session.cpp:53-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L53-L57)），所以 `Dispatch` 之后就是一个普通的成员（[include/orc-rt/Session.h:626-627](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L626-L627)）。

再看 Session 真正调用 `Dispatch` 的两个地方。第一个是处理来自 controller 的 wrapper 调用（[include/orc-rt/Session.h:562-577](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L562-L577)）：

```cpp
void handleWrapperCall(orc_rt_WrapperFunction Fn,
                       WrapperFunctionBuffer ArgBytes, uint64_t CallId) {
  TaskGroup::Token T(ManagedCodeTaskGroup);
  if (!T) {
    // 组已关闭，无法领 Token，直接放弃（不报错，见注释）
    return;
  }

  Dispatch([this, CallId, Fn, ArgBytes = std::move(ArgBytes),
            T = std::move(T)]() mutable {
    Fn(wrap(this), ArgBytes.release(), &wrapperReturn, CallId);
  });
}
```

注意 `T = std::move(T)`——Token 被 move 进了 lambda。第二个调用点是 controller 返回结果后的续体（[include/orc-rt/Session.h:579-599](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L579-L599)），模式完全一致：先领 Token，再把 `OnComplete` 与结果字节连同 Token move 进 Task 交给 `Dispatch`。

最后，`CommonTestUtils.h` 提供了两个极简的现成 `DispatchFn`，帮助你直观感受这个契约可以有多简单（[test/unit/CommonTestUtils.h:55-61](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L55-L61)）：

```cpp
inline void noDispatch(orc_rt::Session::Task T) {
  ADD_FAILURE() << "unexpected dispatch in a no-dispatch session";
  T();
}

/// DispatchFn that runs tasks on the current thread.
inline void inlineDispatch(orc_rt::Session::Task T) { T(); }
```

`inlineDispatch` 只有一行：立即在当前线程调用 Task。`noDispatch` 则用于「不该派发任何 Task」的测试，一旦被调用就记录失败，但仍然 inline 跑掉 Task 以免挂死。可见，「立即同步执行」也是一种合法的 Dispatch 策略。

#### 4.1.4 代码实践

**实践目标**：亲手写一个自定义的 `DispatchFn`，验证 Session 不关心策略、只调用回调。

**操作步骤**：

1. 阅读 [include/orc-rt/Session.h:82-92](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L82-L92)，确认 `Task` 与 `DispatchFn` 的签名。
2. 编写一个「带日志的 inline Dispatch」（示例代码，非项目原有）：

```cpp
// 示例代码：一个会打印日志的 DispatchFn
#include "orc-rt/Session.h"
#include <cstdio>

void loggingInlineDispatch(orc_rt::Session::Task T) {
  std::printf("[Dispatch] 即将运行一个 Task\n");
  T();                       // 立即在当前线程运行
  std::printf("[Dispatch] Task 运行结束\n");
}
```

3. 用它构造一个 Session（沿用 `CommonTestUtils.h` 的占位工具）：

```cpp
// 示例代码
using namespace orc_rt;
Session S(mockExecutorProcessInfo(), loggingInlineDispatch, noErrors);
```

**需要观察的现象**：每次 Session 内部派发 Task 时，你都会看到 `[Dispatch]` 日志成对出现，说明你的回调被调用了。

**预期结果**：构造本身不派发 Task（构造函数不调用 `Dispatch`）。只有当 Session 真正处理 wrapper 调用或 controller 结果续体时，日志才会打印。若你暂时无法触发派发，可结合 4.2.4 的实践一起做。

> 注：上述片段为示例代码，未在仓库中运行过；行为「待本地验证」。最稳妥的做法是把它写进一个模仿 `test/unit/SessionTest.cpp` 的 GoogleTest 用例里编译运行。

#### 4.1.5 小练习与答案

**练习 1**：`DispatchFn` 的返回类型是 `void`，那 Task 执行后的「结果」是怎么传回去的？

**参考答案**：结果不通过 `Dispatch` 返回，而是在构造 Task 时把一个 `OnComplete` 回调（如 `OnControllerCallReturnFn`）move 进 lambda；Task 在执行过程中调用该回调，把结果字节传给等待方。`Dispatch` 只保证「Task 被调用一次」，不参与结果回传。

**练习 2**：如果把 `handleWrapperCall` 里的 `T = std::move(T)` 改成按值捕获 `T`（拷贝），会发生什么问题？

**参考答案**：`TaskGroup::Token` 是持有 `shared_ptr<TaskGroup>` 的 RAII 令牌，构造时计数 +1。按值拷贝会再多持一个 Token（计数多 +1），但 move 才是语义正确的「移交所有权」。更关键的是，若写成不捕获 Token，Task 在别的线程运行时就脱离了 Token 保护，违反托管代码协议，可能导致 Session 在 Task 运行期间被关闭。move 捕获保证了 Token 与 Task 同生共死。

### 4.2 QueueingRunner：把任务排队，由调用者抽干

#### 4.2.1 概念说明

`QueueingRunner` 解决的问题是：**在没有线程（或不想引入线程）的环境里，如何让 Session 的异步派发变成「单线程确定性执行」？**

它的策略很朴素：`operator()(Task)` 只把 Task `push_back` 到一个**由调用者拥有**的工作队列 `WorkQueue` 里，然后立刻返回——既不运行 Task，也不开线程。真正运行 Task 的时机，交给调用者通过两个静态方法 `runFIFOUntilEmpty` / `runLIFOUntilEmpty` 来「抽干（drain）」队列。

这种设计有两个典型用途：

1. **无线程环境**：嵌入式或受限平台，没有 `std::thread`，调用者可以在主循环里手动抽干队列。
2. **单元测试**：把异步流程「压扁」到单线程里，让执行顺序可预测、可断言。SessionTest 几乎全部用例都依赖它。

#### 4.2.2 核心流程

```text
调用者拥有一个 WorkQueue Q
        │
        ▼
构造 QueueingRunner R(Q)   // R 只持有 Q 的引用
        │
        ▼
Session 派发 → R(Task) → Q.push_back(Task)   // 只入队，不运行
        │
        ▼
调用者：QueueingRunner<>::runFIFOUntilEmpty(Q)
   while (auto Call = Q.pop_front())  // 取队首（先进先出）
       (*Call)();                      // 运行；运行中若再入队，也会被本轮抽干
        │
        ▼
队列空 → 返回
```

两个抽干方向的区别只在「从哪头取」：

- `runFIFOUntilEmpty` 用 `pop_front()`：**先进先出**，先派发的先跑。
- `runLIFOUntilEmpty` 用 `pop_back()`：**后进先出**，最近派发的先跑（像栈）。

无论哪个方向，循环都会持续到队列真正为空；并且**抽干过程中新入队的 Task 也会在本轮被抽干**——这一点对「Task 派生新 Task」的链式流程至关重要。

#### 4.2.3 源码精读

先看默认的 `WorkQueue` 类型——`detail::SynchronizedDeque`，它就是一个带锁的双端队列（[include/orc-rt/QueueingRunner.h:26-54](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/QueueingRunner.h#L26-L54)）：

```cpp
template <typename T> class SynchronizedDeque {
public:
  void push_back(T V) { std::scoped_lock<std::mutex> Lock(M); Q.push_back(std::move(V)); }
  std::optional<T> pop_back()  { ... Q.pop_back();  ... }   // 空则返回 nullopt
  std::optional<T> pop_front() { ... Q.pop_front(); ... }
private:
  std::mutex M;
  std::deque<T> Q;
};
```

它自带互斥锁，所以**可以安全地并发 push 与抽干**（QueueingRunnerTest 的 `ConcurrentProducerAndDrainer` 用例正是验证这一点）。如果你完全单线程，锁的开销可以忽略。

`QueueingRunner` 本体极其精简（[include/orc-rt/QueueingRunner.h:73-102](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/QueueingRunner.h#L73-L102)）：

```cpp
template <typename WorkQueueT = detail::SynchronizedDeque<move_only_function<void()>>>
class QueueingRunner {
public:
  using WorkQueue = WorkQueueT;
  QueueingRunner(WorkQueueT &Pending) : Pending(Pending) {}

  void operator()(move_only_function<void()> Task) {
    Pending.push_back(std::move(Task));      // 只入队
  }

  static void runLIFOUntilEmpty(WorkQueueT &Q) {   // 后进先出
    while (auto Call = Q.pop_back()) (*Call)();
  }
  static void runFIFOUntilEmpty(WorkQueueT &Q) {   // 先进先出
    while (auto Call = Q.pop_front()) (*Call)();
  }
private:
  WorkQueueT &Pending;
};
```

三个要点：

1. `QueueingRunner` 只持有 `WorkQueue` 的**引用**，不拥有它——队列的生命周期由调用者管理（这正好匹配「调用者决定何时抽干」的设计）。
2. `operator()` 满足 `DispatchFn` 的签名 `void(Task)`，所以一个 `QueueingRunner` 临时对象可以直接作为 Session 的 `Dispatch` 参数传进去（下面 4.2.4 会看到）。
3. 两个 `run*UntilEmpty` 是**静态方法**，只需队列即可调用，不必持有 Runner 实例。

文件末尾还有一个推导指引（[include/orc-rt/QueueingRunner.h:104-105](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/QueueingRunner.h#L104-L105)），让你写 `QueueingRunner R(Q)` 时不必显式写模板参数。

最直观的行为验证在单元测试里。FIFO 用例（[test/unit/QueueingRunnerTest.cpp:50-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/QueueingRunnerTest.cpp#L50-L62)）入队 `0,1,2`，抽干后日志顺序仍是 `0,1,2`；LIFO 用例（[test/unit/QueueingRunnerTest.cpp:64-76](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/QueueingRunnerTest.cpp#L64-L76)）同样的入队顺序，抽干后日志变成 `2,1,0`。「抽干时新入队」的链式行为则在 [test/unit/QueueingRunnerTest.cpp:86-102](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/QueueingRunnerTest.cpp#L86-L102) 验证：第一个 Task 在体内再入队第二个 Task，一次 `runFIFOUntilEmpty` 就把两个都跑完。

#### 4.2.4 代码实践

**实践目标**：用 `QueueingRunner` 和一个 `WorkQueue` 构造 Session，发起会派生 Task 的操作，再调用 `runFIFOUntilEmpty` 抽干队列，观察 Task 被依次执行。

**操作步骤**：分两步走，先单独验证 Runner 行为，再把它接进 Session。

第一步——单独验证 Runner（最易编译运行，参考 [test/unit/QueueingRunnerTest.cpp:50-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/QueueingRunnerTest.cpp#L50-L62)）：

```cpp
// 示例代码（改编自 QueueingRunnerTest）
#include "orc-rt/QueueingRunner.h"
#include <cstdint>
#include <vector>
#include <cstdio>

using namespace orc_rt;

int main() {
  std::vector<uint64_t> Log;
  QueueingRunner<>::WorkQueue Q;
  QueueingRunner R(Q);

  for (uint64_t I = 0; I < 3; ++I)
    R([&, I]() { Log.push_back(I); });   // 只入队，此刻 Log 仍为空

  QueueingRunner<>::runFIFOUntilEmpty(Q); // 抽干

  for (auto V : Log) std::printf("%llu\n", (unsigned long long)V);
}
```

第二步——接进 Session（参考 SessionTest 的真实范式 [test/unit/SessionTest.cpp:622-626](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L622-L626)）：

```cpp
// 示例代码（改编自 SessionTest 的 ControllerAccessTest.Basics）
#include "orc-rt/Session.h"
#include "orc-rt/QueueingRunner.h"
#include "CommonTestUtils.h"

using namespace orc_rt;

TEST(MyTest, DrainSessionTasks) {
  QueueingRunner<>::WorkQueue Tasks;
  // QueueingRunner(Tasks) 作为 DispatchFn 传入
  Session S(mockExecutorProcessInfo(), QueueingRunner(Tasks), noErrors);

  S.attach<MockControllerAccess>(BootstrapInfo(S), postOnto(Tasks));

  QueueingRunner<>::runFIFOUntilEmpty(Tasks);  // 抽干 Session + controller 双侧工作
}
```

这里 `postOnto(Tasks)`（[test/unit/SessionTest.cpp:321-324](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L321-L324)）把 controller 侧的「模拟工作」也 push 进同一个 `Tasks` 队列，于是**一次 `runFIFOUntilEmpty` 就同时推进了 executor 侧与 controller 侧**——这是 SessionTest 把跨端异步流程压成单线程确定性的关键技巧。

**需要观察的现象**：

- 第一步中，`R(...)` 之后、`runFIFOUntilEmpty` 之前，`Log` 应为空（Task 没有立即运行）。
- 抽干后，`Log` 应按 `0,1,2`（FIFO）顺序填充。
- 把 `runFIFOUntilEmpty` 换成 `runLIFOUntilEmpty`，顺序应变为 `2,1,0`。

**预期结果**：Task 的执行被推迟到 `run*UntilEmpty` 调用，且顺序与抽干方向一致。若在抽干循环里加入「再入队」的 Task，它们也会在本轮被跑掉。

> 第一步示例可独立编译运行（需链接 orc-rt 头文件）；第二步依赖 GoogleTest 与 `MockControllerAccess` 等测试设施，建议直接在 `test/unit/` 下新增用例后用 `check-orc-rt-unit` 目标编译运行。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `QueueingRunner` 只持有 `WorkQueue` 的引用，而不自己拥有一个队列？

**参考答案**：因为「何时抽干、以何种顺序抽干」是调用者的策略决策，而非 Runner 的。若 Runner 自己拥有队列，调用者就无法在主循环里按需抽干、也无法把 controller 侧工作合并到同一个队列。引用语义把队列的控制权完整留给调用者，这正是它适合单线程/测试环境的原因。

**练习 2**：`runFIFOUntilEmpty` 的循环条件是 `while (auto Call = Q.pop_front())`。如果一个正在运行的 Task 又往 `Q` 里 push 了一个新 Task，这个新 Task 会被本轮抽干吗？为什么？

**参考答案**：会。因为 `pop_front` 每次只取一个，运行完才回到 `while` 判断；运行期间新 push 的 Task 已经在队列里，下一次 `pop_front` 就会取到它，循环不会提前结束。只有当某次 `pop_front` 返回 `nullopt`（队列真的空了）时循环才退出。

### 4.3 ThreadPoolRunner：生产环境的线程池

#### 4.3.1 概念说明

`ThreadPoolRunner` 是为**生产环境**准备的 Runner：构造时启动固定数量的 worker 线程，每个派发进来的 Task 被丢进内部队列，由空闲的 worker 取走并发执行。

它的关键性质有四条（来自头文件类注释 [include/orc-rt/ThreadPoolRunner.h:26-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ThreadPoolRunner.h#L26-L35)）：

1. **固定大小**：线程数在构造时确定，运行期不增减。
2. **Runner 必须比 Session 长寿**：Session 关闭完成前不能析构 Runner。
3. **析构会抽干残留任务**：析构时不会丢弃队列里还没跑的 Task，worker 会先把它们跑完再退出。
4. **析构开始后禁止再派发**：一旦析构启动，再调用 `operator()` 是契约违反（会被 `assert` 拦截）。

#### 4.3.2 核心流程

线程池的运作可以拆成「派发方」与「worker 方」两条线：

```text
派发方（Session 所在线程）              worker 线程 × N（构造时启动）
─────────────────────────              ─────────────────────────
operator()(Task):                      workerLoop():
  lock(M)                                 lock(M)
  assert(!Stop)                           wait(CV, !Pending.empty() || Stop)
  Pending.push_back(Task)                 if (Pending.empty() && Stop) return
  unlock(M)                               Call = move(Pending.back())
  CV.notify_one()   ──────────────▶       Pending.pop_back(); unlock(M)
                                          Call()           // 不持锁地运行
                                          （循环）
析构：
  lock(M); Stop = true; unlock(M)
  CV.notify_all()    ──────────────▶  唤醒所有 worker；各抽干后 return
  join 所有 worker
```

注意一个容易忽略的细节：worker 从 `Pending.back()` 取任务（[lib/executor/ThreadPoolRunner.cpp:56-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L56-L57)），所以 **ThreadPoolRunner 实际是后进先出（LIFO）调度**，而非 FIFO。这与 `QueueingRunner::runLIFOUntilEmpty` 的取法一致。LIFO 在线程池里常被采用，因为它对缓存更友好——最近入队的任务更可能还在 cache 里。

#### 4.3.3 源码精读

头文件声明了接口与成员（[include/orc-rt/ThreadPoolRunner.h:36-61](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ThreadPoolRunner.h#L36-L61)）。注意它**四项特殊成员函数全 delete**——不可拷贝、不可移动，必须以稳定地址持有。核心成员是 worker 线程数组、一把互斥锁、一个条件变量、`Stop` 标志和 `Pending` 队列。

构造函数启动 worker（[lib/executor/ThreadPoolRunner.cpp:20-24](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L20-L24)）：

```cpp
ThreadPoolRunner::ThreadPoolRunner(size_t NumThreads) {
  Workers.reserve(NumThreads);
  for (size_t I = 0; I < NumThreads; ++I)
    Workers.emplace_back([this]() { workerLoop(); });
}
```

`operator()` 是「派发方」入口（[lib/executor/ThreadPoolRunner.cpp:36-44](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L36-L44)）：

```cpp
void ThreadPoolRunner::operator()(move_only_function<void()> Task) {
  {
    std::scoped_lock<std::mutex> Lock(M);
    assert(!Stop && "operator() called on ThreadPoolRunner after destruction begun");
    Pending.push_back(std::move(Task));
  }
  CV.notify_one();
}
```

加锁、断言未停止、入队、解锁，然后唤醒**一个**等待中的 worker（`notify_one`，因为只多了一个任务）。

worker 循环是「消费方」（[lib/executor/ThreadPoolRunner.cpp:46-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L46-L62)）：

```cpp
void ThreadPoolRunner::workerLoop() {
  while (true) {
    move_only_function<void()> Call;
    {
      std::unique_lock<std::mutex> Lock(M);
      CV.wait(Lock, [this]() { return !Pending.empty() || Stop; });
      if (Pending.empty() && Stop)
        return;                       // 停止且无残留 → 退出
      Call = std::move(Pending.back());
      Pending.pop_back();
    }                                 // 解锁后再运行，避免长时间持锁
    Call();
  }
}
```

三个关键点：

1. `CV.wait(Lock, pred)` 用谓词形式等待，避免虚假唤醒：醒来时要么有任务、要么被要求停止。
2. **退出条件是「`Pending` 空 **且** `Stop`」**——这保证了析构时残留任务会被跑完。
3. `Call()` 在锁外运行，这样长时间任务不会阻塞其他 worker 取任务。

析构函数负责优雅关闭（[lib/executor/ThreadPoolRunner.cpp:26-34](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L26-L34)）：

```cpp
ThreadPoolRunner::~ThreadPoolRunner() {
  {
    std::scoped_lock<std::mutex> Lock(M);
    Stop = true;
  }
  CV.notify_all();                    // 唤醒所有 worker
  for (auto &Worker : Workers)
    Worker.join();                    // 等它们抽干残留并退出
}
```

设 `Stop`、唤醒全部、join 全部。结合 workerLoop 的退出条件，join 返回时队列一定已被抽干。

> 提醒：本讲仓库（`orc-rt`）的单元测试目录里**没有** `ThreadPoolRunnerTest.cpp`（可用 `Glob` 自行确认），所以 ThreadPoolRunner 的行为没有像 QueueingRunner 那样被逐用例覆盖。理解它主要靠阅读上述实现。

#### 4.3.4 代码实践

**实践目标**：在脑中（或纸面上）走通一次 `ThreadPoolRunner` 的派发—消费—关闭流程，并理解它与 Session 的生命周期约束。

**操作步骤**（源码阅读型实践）：

1. 对照 [lib/executor/ThreadPoolRunner.cpp:46-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L46-L62)，回答：worker 在 `Stop=true` 但 `Pending` 非空时会立刻退出吗？
2. 阅读 [include/orc-rt/ThreadPoolRunner.h:26-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ThreadPoolRunner.h#L26-L35) 的类注释，列出「Runner 必须比 Session 长寿」和「析构后禁止派发」这两条约束各自是为了避免什么。
3. 在纸上面画一张时序图：Session 在线程 A 调用 `Dispatch(Task)` → `ThreadPoolRunner::operator()` 入队 → `notify_one` → worker 线程 B 醒来取走 Task → 在 B 上运行（连同被 move 进来的 Token）。

**需要观察的现象**：worker 取任务的顺序是 LIFO（`Pending.back()`）；Task 在锁外运行；析构抽干残留后才 join 返回。

**预期结果**：

1. 不会。退出条件是 `Pending.empty() && Stop`，二者**同时**成立才退出；`Stop` 为真但仍有任务时，会继续把任务取完。
2. 「Runner 必须比 Session 长寿」：Session 在 shutdown 完成前仍可能派发 Task（如处理中的 controller 结果续体），若 Runner 先死，Task 就无人执行。「析构后禁止派发」：析构后 `Pending` 与 worker 已不复存在，再派发会访问已释放资源，故用 `assert(!Stop)` 把违反挡在调试期。
3. 时序图应清楚体现：**Task 最终运行的线程（worker B）与派发它的线程（A）不同**，但 Token 因被 move 进 Task 而**随 Task 跨线程移动**，仍覆盖 Task 的执行——这正是 [u4-l1](u4-l1-taskgroup-managed-code.md) 强调的「调用续体的一方须自领新令牌/令牌随续体移动」在 ThreadPoolRunner 场景下的体现。

> 本实践为源码阅读型，无需编译运行；结论「待本地验证」的部分仅指若你想用真实多线程用例复现，需自行编写（仓库未提供现成用例）。

#### 4.3.5 小练习与答案

**练习 1**：worker 循环里 `Call()` 为什么必须放在锁的外面（先 `Pending.pop_back()` 再解锁，最后才 `Call()`）？

**参考答案**：为了不在运行 Task 期间持有 `M`。Task 可能执行很久（甚至再派发新 Task），若全程持锁，其他 worker 取不到任务、`operator()` 也无法入队，整个线程池会串行化甚至死锁。把 `Call()` 放在解锁之后，使多个 worker 能真正并发执行 Task。

**练习 2**：假设你把 `ThreadPoolRunner` 的 worker 取任务方式从 `Pending.back()` 改成 `Pending.front()`（变 FIFO），功能上会出错吗？为什么项目选择了 LIFO？

**参考答案**：功能上不会出错——任何取出顺序都能保证「每个 Task 被运行一次」。选择 LIFO 通常是出于**缓存友好**与**降低尾延迟**的考虑：最近入队的任务相关数据更可能在 cache 中；并且当任务有依赖链时，先跑下游（较新）任务能让上游（较旧）任务的结果尽快被消费。这是常见的线程池调度启发式，而非正确性要求。

## 5. 综合实践

把本讲三个模块串起来：实现一个**会「先排队、再切到线程池」的两阶段 Dispatch**，加深对策略注入的理解。

**任务描述**：写一个 `DispatchFn`，它内部持有一个 `QueueingRunner` 风格的队列。当标志位 `Coalesce=true` 时，所有 Task 只入队不运行（像 QueueingRunner）；当外部把 `Coalesce` 翻成 `false` 并调用一个 `flush` 时，把队列里积压的 Task 全部转交给一个 `ThreadPoolRunner` 并发执行。

**参考实现骨架**（示例代码，非项目原有）：

```cpp
// 示例代码：两阶段 Dispatch
#include "orc-rt/ThreadPoolRunner.h"
#include "orc-rt/move_only_function.h"
#include <deque>
#include <mutex>

using namespace orc_rt;

class TwoStageDispatch {
public:
  explicit TwoStageDispatch(ThreadPoolRunner &Pool) : Pool(Pool) {}

  // 作为 Session 的 DispatchFn 使用
  void operator()(move_only_function<void()> Task) {
    std::scoped_lock<std::mutex> L(M);
    if (Coalesce) {
      Q.push_back(std::move(Task));   // 阶段一：只入队
    } else {
      Pool(std::move(Task));          // 阶段二：直接进线程池
    }
  }

  void flush() {                      // 把积压的 Task 转交线程池
    std::deque<move_only_function<void()>> Snapshot;
    {
      std::scoped_lock<std::mutex> L(M);
      Coalesce = false;
      Snapshot.swap(Q);
    }
    while (!Snapshot.empty()) {
      Pool(std::move(Snapshot.front()));
      Snapshot.pop_front();
    }
  }

private:
  ThreadPoolRunner &Pool;
  std::mutex M;
  bool Coalesce = true;
  std::deque<move_only_function<void()>> Q;
};
```

**验证思路**：

1. 在 `Coalesce=true` 期间派发若干 Task，确认它们都未执行（积压在 `Q`）。
2. 调用 `flush()`，确认积压的 Task 被线程池并发跑完。
3. `flush()` 之后再派发的 Task 应直接进入线程池（阶段二）。
4. 把这个 `TwoStageDispatch` 作为 `DispatchFn` 传入 Session（参考 4.2.4 第二步的构造范式），观察 Session 派发的 Task 也遵循同样的两阶段策略。

**预期结果**：同一个 Session，仅凭换一个 `DispatchFn`，就从「批量延迟执行」切换到了「立即并发执行」——这正是 `DispatchFn` 作为策略注入点的价值。运行结果「待本地验证」。

## 6. 本讲小结

- **`DispatchFn` 是策略注入点**：Session 把每个工作单元封装成 `Task`（`move_only_function<void()>`），交给 `DispatchFn` 决定何时何地运行；Session 自身不挑线程模型（[Session.h:82-92](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L82-L92)）。
- **Token 随 Task 移动**：Session 在调用 `Dispatch` 之前先领好托管代码 Token，并把它 `move` 进 Task lambda，使 Task 无论在哪个线程运行都处于 Token 保护之下（[Session.h:562-577](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L562-L577)）。
- **`QueueingRunner` 只入队、由调用者抽干**：适合无线程环境与单元测试；提供 FIFO / LIFO 两种抽干方向，且抽干时会一并跑掉期间新入队的 Task（[QueueingRunner.h:73-102](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/QueueingRunner.h#L73-L102)）。
- **`ThreadPoolRunner` 是固定线程池**：worker 用条件变量等待任务，从 `Pending.back()` 取（LIFO），Task 在锁外运行；析构会抽干残留任务再 join（[ThreadPoolRunner.cpp:26-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/ThreadPoolRunner.cpp#L26-L62)）。
- **生命周期铁律**：Runner 必须比 Session 长寿；Session 必须先 shutdown 完毕，才能析构 Runner；析构开始后禁止再派发（[ThreadPoolRunner.h:26-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ThreadPoolRunner.h#L26-L35)）。
- **测试范式**：SessionTest 用 `QueueingRunner(Tasks)` 作 Dispatch、再用 `runFIFOUntilEmpty(Tasks)` 抽干，把跨端异步流程压成单线程确定性流程（[SessionTest.cpp:622-626](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L622-L626)）。

## 7. 下一步学习建议

- 想看 Task 真正「从哪来」的完整链路，进入 **[u5-l2 Wrapper Function 签名与 call/handle](u5-l2-wrapper-function-call-handle.md)**——`handleWrapperCall` 正是 Task 的主要来源之一。
- 想理解 controller 结果续体如何安全完成，进入 **[u5-l3 ControllerAccess：执行端↔控制端桥](u5-l3-controller-access.md)**，它解释了 `handleControllerCallResult` / `failPendingControllerCall` 等三条完成路径。
- 想深入 Session 关闭时为何要等待托管代码 Token 清空（即 shutdown 的 Drain 阶段），回顾 **[u3-l2 生命周期状态机](u3-l2-session-lifecycle.md)** 与 **[u4-l1 TaskGroup、Token 与托管代码](u4-l1-taskgroup-managed-code.md)**。
- 若你想了解 `move_only_function` 这个贯穿全库的可调用对象类型是如何实现的，可预习 **[u10-l3 核心工具：move_only_function 等](u10-l3-core-utilities.md)**。
