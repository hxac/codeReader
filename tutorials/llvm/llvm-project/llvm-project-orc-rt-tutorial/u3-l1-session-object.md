# Session 对象与构造

## 1. 本讲目标

在前两讲里，我们已经建立起 controller/executor 的二分心智模型（u2-l1），并把 `Error` / `Expected<T>` 这套错误处理语言练熟（u2-l3）。本讲要落到执行端最核心的一个对象上——`Session`。

学完本讲，你应当能够：

- 说清构造一个 `Session` 必须提供的三要素（`ExecutorProcessInfo`、`DispatchFn`、`ErrorReporterFn`）各自的作用。
- 说出 `Session` 持有哪些核心成员，以及「错误报告回调非同步（not synchronized）」这一关键约定意味着什么。
- 读懂 `SessionTest.cpp` 里最简单的两个测试用例 `TrivialConstructionAndDestruction` 与 `ReportError`，并能照着它们写出自己的最小构造与错误上报示例。

本讲只聚焦「对象本身怎么造出来、它肚子里装了什么」；生命周期状态机（attach/detach/shutdown）是下一讲 u3-l2 的主题，这里只在「析构会触发 shutdown」这一层面点到为止。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（这些都在前置讲义里讲过，这里只做一句话回顾）：

- **Session 是执行端的根对象**：链接了 orc-rt 的进程里，所有 JIT 代码的运行时资源都挂在 `Session` 之下；它必须先于任何 JIT 代码创建、后于其全部执行完毕后销毁（见 u2-l1）。
- **Service 是资源管理抽象**：`Session` 拥有一组 `Service`，每个 `Service` 只暴露 `onDetach` / `onShutdown` 两个回调（见 u2-l1、u2-l3）。
- **Error 的检查契约**：`Error` 是一个必须被检查的一等值，失败值还要被「取走」才能析构，否则 `abort()`（见 u2-l3）。
- **move_only_function**：orc-rt 自带的「仅移动」可调用对象包装器，类似 `std::function` 但能持有 `unique_ptr` 这类不可拷贝的捕获物。本讲里几乎所有回调类型都用它。

如果你对「半开区间地址」「out-of-band 错误」等术语还有印象模糊，也无妨——本讲几乎不涉及它们，重点是「造一个对象」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [include/orc-rt/Session.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | `Session` 类的声明，含构造函数签名、回调类型别名、成员变量。是本讲的主战场。 |
| [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) | `Session` 的实现，含构造/析构函数体与私有的 `NotificationService`。 |
| [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | Session 的单元测试，本讲精读其中最简单的两个用例。 |
| [test/unit/CommonTestUtils.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h) | 测试用的小工具：`mockExecutorProcessInfo()`、`noDispatch`、`noErrors`。 |
| [include/orc-rt/ExecutorProcessInfo.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/ExecutorProcessInfo.h) | 构造三要素之一，描述执行进程的目标三元组与页大小。 |
| [include/orc-rt/Service.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h) | `Service` 抽象接口，理解 `Services` 成员的元素类型。 |

> 阅读提示：orc-rt 的测试名与被测源码一一对应（见 u1-l3），`SessionTest.cpp` 就是反向定位 `Session` 行为的最好索引。本讲会反复在「测试断言 ↔ 源码行为」之间来回对照。

## 4. 核心概念与源码讲解

### 4.1 构造 Session 的三要素与成员布局

#### 4.1.1 概念说明

`Session` 是执行端的根对象，但它自己并不「干活」——它是一个**协调者（coordinator）**：持有资源、串起回调、管理生命周期。因此，造一个 `Session` 本质上是把三样东西**交给**它：

1. **进程信息（ExecutorProcessInfo）**：告诉 Session「我跑在什么机器上」——目标三元组（triple，如 `arm64-apple-darwin`）和内存页大小（page size）。这两项决定了后续 Service（如内存映射、动态库加载）要按什么平台的规则行事。
2. **调度回调（DispatchFn）**：Session 自己不创建线程，它把每一个工作单元（`Task`）打包好，丢给这个回调，由回调决定是「立即在当前线程跑」「丢进队列」还是「投到线程池」。
3. **错误报告回调（ErrorReporterFn）**：当 Session 在服务 JIT 代码的过程中产生错误（比如内存管理请求无法满足），就通过这个回调把 `Error` 抛出来。**谁来消费、怎么消费，完全由调用方决定**。

这三者之所以用回调而不是固定接口，是为了让 orc-rt 保持「运行时不预设执行模型」的灵活度：测试里可以用「什么都不做」的占位回调，生产环境可以接真实的线程池与日志后端。

#### 4.1.2 核心流程

构造一个 `Session` 的心智流程：

```text
调用方准备三要素
   │
   ├─ ExecutorProcessInfo（triple + pageSize）
   ├─ DispatchFn（怎么跑 Task）
   └─ ErrorReporterFn（错误往哪报）
   │
   ▼
Session 构造函数
   │
   ├─ std::move 存下 EPI / Dispatch / ReportError 三个成员
   ├─ 创建内部 NotificationService 并存为首个 Service
   │     （构造期就保证「通知服务」已就位）
   └─ ManagedCodeTaskGroup / CA / Services 等成员取默认值
   │
   ▼
得到一个处于 Start 状态、尚未 attach 任何 controller 的 Session
```

注意一个容易忽略的细节：构造函数里就已经调用 `createService<NotificationService>()`，也就是说**一个刚造出来的 Session 内部已经有一个 Service 了**（用于托管 `addOnDetach` / `addOnShutdown` 注册的回调）。这一点后面会回到源码精读里印证。

#### 4.1.3 源码精读

先看构造函数的**声明**与文档注释。头文件里有一大段说明，关键三行在末尾：

[include/orc-rt/Session.h:286-300](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L286-L300) —— 这是构造函数的文档与签名。要点有三：`ReportError` 用于上报「服务 JIT 代码时产生的错误」（JIT 程序**内部**的错误一般对 orc-rt 不可见）；`Dispatch` 负责把 Session 产生的 Task 安排去执行；最后一行明确写了 reporter 的并发约定。

这段注释里最关键的一句是关于 reporter 的：

> Note that entry into the reporter is not synchronized: it may be called from multiple threads concurrently.

我们把它单拎出来看实现，因为「非同步」是本讲的硬约定之一。构造函数的实现非常短：

[lib/executor/Session.cpp:53-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L53-L57) —— 构造函数体。它做的事：把三个参数 `std::move` 进对应成员，然后通过 `createService<NotificationService>()` 创建一个内部通知服务，把返回的引用绑定到 `Notifiers` 成员上。

为什么 `Notifiers` 是个引用？看成员声明就明白了：

[include/orc-rt/Session.h:626-637](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L626-L637) —— `Session` 的私有成员一览。对照下表理解每个成员的角色：

| 成员 | 类型 | 角色 |
| --- | --- | --- |
| `EPI` | `ExecutorProcessInfo` | 进程信息（triple / pageSize）。 |
| `Dispatch` | `DispatchFn` | 调度回调，决定 Task 怎么跑。 |
| `ManagedCodeTaskGroup` | `shared_ptr<TaskGroup>` | 托管代码令牌桶，构造时即 `TaskGroup::Create()`。 |
| `CA` | `shared_ptr<ControllerAccess>` | 控制端桥，构造时为空，attach 后才填。 |
| `ReportError` | `ErrorReporterFn` | 错误上报回调。 |
| `M` / `CV` | `mutex` / `condition_variable` | 保护状态机的锁与条件变量。 |
| `CurrentState` / `TargetState` | `State` | 当前态 / 目标态，初始 `Start` / `None`。 |
| `Services` | `vector<unique_ptr<Service>>` | 所有被拥有的 Service。 |
| `Notifiers` | `NotificationService &` | 指向 `Services` 里那个内部通知服务的引用。 |

这里 `Notifiers` 是引用类型（`NotificationService &Notifiers;`），而它在成员初始化列表里被 `createService<NotificationService>()` 的返回值初始化——`createService` 返回的是它刚塞进 `Services` 的那个对象的引用，所以 `Notifiers` 永远指向 `Services` 容器里的一个有效对象。这就是「构造期就保证通知服务已就位」的实现手法。

`State` 枚举定义了 Session 的四态（本讲只用到 Start，其余留给 u3-l2）：

[include/orc-rt/Session.h:518-533](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L518-L533) —— `State` 枚举：`None`（占位）/ `Start`（初态）/ `Attached` / `Detached` / `Shutdown`。

最后看两个语义保证：

[include/orc-rt/Session.h:302-306](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L302-L306) —— `Session` 显式 `= delete` 了拷贝与移动构造/赋值。**Session 不可拷贝、不可移动**。这意味着 `Session` 必须以引用或指针传递，不能被放进会触發移动的容器里。这与「它是执行端全局根对象」的定位一致：一个进程里它就该待在原地不动。

[lib/executor/Session.cpp:59-65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L59-L65) —— 析构函数。它先调 `shutdown()`（若尚未关闭则触发关闭），再用条件变量 `CV` **阻塞等待**，直到 `CurrentState == Shutdown && TargetState == None`。这正是前置讲义里那句「Session 析构会阻塞直到生命周期完成」的源码出处。本讲你只需记住结论：**只要 `Session` 对象离开作用域，它一定会把自己关干净才让你往下走**。

#### 4.1.4 代码实践

**实践目标**：亲手用三个最简单的占位回调构造一个 `Session`，并验证它能正常析构。

**操作步骤**：

1. 打开 `test/unit/SessionTest.cpp`，找到 `TrivialConstructionAndDestruction` 测试（下文 4.3 会精读）。它只有一行：

   ```cpp
   Session S(mockExecutorProcessInfo(), noDispatch, noErrors);
   ```

2. 仿照它，在本地一个 `.cpp`（或临时测试）里写一段最小代码（**示例代码**，需链接 `orc-rt-executor` 与 GoogleTest）：

   ```cpp
   // 示例代码：最小 Session 构造
   #include "orc-rt/Session.h"
   #include "orc-rt/Error.h"
   #include "orc-rt/ExecutorProcessInfo.h"
   #include "orc-rt/move_only_function.h"

   using namespace orc_rt;

   int main() {
     // 三要素：进程信息 / 不调度 / 错误直接吞掉
     Session S(
       ExecutorProcessInfo("arm64-apple-darwin", 16384), // ExecutorProcessInfo
       [](Session::Task T) { T(); },                    // DispatchFn：直接在当前线程跑
       [](Error Err) { cantFail(std::move(Err)); }      // ErrorReporterFn：吞掉
     );
     // S 离开作用域 → 析构触发 shutdown → 阻塞至生命周期结束
     return 0;
   }
   ```

**需要观察的现象**：

- 程序正常退出，不 abort、不挂起。
- 因为 `Dispatch` 直接 `T()` 而非排队，若构造期没有任何派生 Task（本例就没有），则 `Dispatch` 实际不会被调用。

**预期结果**：进程返回码 0。析构里 `shutdown()` 把一个还没 attach 的 Session 从 `Start` 推到 `Detached` 再到 `Shutdown`，`CV.wait` 随即解除阻塞。

> 若无法本地编译链接 orc-rt，可标注「待本地验证」，转而做下文 4.3 的源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Session` 把拷贝和移动都 `delete` 掉？如果允许移动会发生什么问题？

**参考答案**：因为 `Session` 是执行端的全局协调者，持有 `mutex`、`condition_variable`、若干 `shared_ptr` 与一个引用成员 `Notifiers`。允许移动会让「持有 Session 引用/指针的 Service、回调、Task」瞬间指向失效地址，而 `Notifiers` 这个引用成员本身就无法移动后自洽。删除移动/拷贝强制所有持有者用稳定地址引用它，避免悬空。

**练习 2**：构造函数里 `Notifiers(createService<NotificationService>())` 这一行，`createService` 返回的是什么？为什么能直接绑定到一个引用成员上？

**参考答案**：`createService<NotificationService>()` 在内部把一个 `NotificationService` 用 `unique_ptr` 塞进 `Services` 容器，并返回**指向该对象的引用**（`NotificationService&`）。因此 `Notifiers` 这个引用成员绑定的是 `Services` 里那个长期存活的对象，只要 Session 活着它就有效。

---

### 4.2 reportError 与 Dispatch：两个回调通道

#### 4.2.1 概念说明

构造三要素里，有两个是**回调通道**：`DispatchFn`（任务调度）和 `ErrorReporterFn`（错误上报）。它们职责完全不同，但有一个共同点：Session **不规定它们在哪里、由谁运行**，只规定「我丢给你一个东西，你决定怎么处理」。

- **DispatchFn**：「字节进、字节出」的 wrapper function 调用、controller 返回结果的续体（continuation）等，都会被 Session 封装成一个个 `Task`（类型是 `move_only_function<void()>`），然后交给 `Dispatch`。`Dispatch` 可以同步执行、入队、或投到线程池——这就是 u4-l2 会讲的 `QueueingRunner`（测试用）与 `ThreadPoolRunner`（生产用）的接入点。
- **ErrorReporterFn**：服务 JIT 代码时产生的错误经此上报。**关键约定：进入 reporter 不是同步的（not synchronized）**——它可能被多个线程并发调用。这意味着你写的 reporter 必须自己保证线程安全（比如要写共享状态就得加锁）。

还有一个公开的便捷方法 `reportError`，它就是简单地把 `Error` move 进 `ReportError` 回调：

[include/orc-rt/Session.h:318-319](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L318-L319) —— `void reportError(Error Err) { ReportError(std::move(Err)); }`。注意 `ControllerAccess` 子类也有一个同名受保护方法 `reportError`，内部就是调它（见 [Session.h:192](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L192)），让自定义 ControllerAccess 能顺手把错误喂给同一个 reporter。

#### 4.2.2 核心流程

两个回调的使用流向：

```text
─── DispatchFn 通道 ──────────────────────────────
 Session 内部产生工作单元
   (wrapper call / controller 结果续体)
        │  封装为 Task = move_only_function<void()>
        ▼
   Dispatch(Task)  ──►  由调用方决定: 同步跑 / 入队 / 投线程池

─── ErrorReporterFn 通道 ────────────────────────
 Session 服务 JIT 代码时遇到错误
        │  构造 Error（失败值）
        ▼
   reportError(Err)  ──►  ReportError(Err)
                             │
                             ▼
            调用方提供的 reporter（可能被多线程并发调用！）
```

两个通道互不耦合：Dispatch 决定「任务何时跑」，Reporter 决定「错误往哪报」。即便你给了个「什么都不做」的 `noDispatch`，reporter 仍能正常工作。

#### 4.2.3 源码精读

先看回调类型的别名定义。它们都基于 `move_only_function`：

[include/orc-rt/Session.h:74-76](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L74-L76) —— `ErrorReporterFn`、`OnDetachFn`、`OnShutdownFn` 的类型别名。`ErrorReporterFn = move_only_function<void(Error)>`，签名很直接：吃一个 `Error`，不返回任何东西（错误已被它「接手」，由它负责检查/消费）。

再看 `Task` 与 `DispatchFn` 的定义，以及一段重要的文档注释：

[include/orc-rt/Session.h:82-92](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L82-L92) —— `Task = move_only_function<void()>`，`DispatchFn = move_only_function<void(Task)>`。注释说清了 Session 会为「收到的 wrapper-function 调用」和「controller 返回结果的续体」各建一个 Task 交给 `Dispatch`，后者负责把它「inline 跑、入队、或投线程池」。

还要认识一个配套的小工具类 `ReportErrorsViaSession`，它把「通过某个 Session 上报错误」包装成一个可调用对象，方便在需要函数对象的地方直接用：

[include/orc-rt/Session.h:642-649](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L642-L649) —— `ReportErrorsViaSession`：构造时存一个 `Session&`，`operator()` 调用 `S.reportError(std::move(Err))`。它解决的是「我有一个要求 `void(Error)` 的接口，但我手里只有 `Session&`」的适配问题。

顺便看一眼进程信息的访问器，它是三要素里唯一「可读出」的成员：

[include/orc-rt/Session.h:314-316](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L314-L316) —— `processInfo()` 返回 `EPI` 的 const 引用，让外部能拿到 triple 与 page size。`Dispatch` 和 `ReportError` 是「只写」的（一旦 move 进 Session 就由 Session 内部使用），但 `EPI` 可以被读回。

#### 4.2.4 代码实践

**实践目标**：体会「reporter 非同步」的含义——写一个会被多线程并发触发的 reporter，观察它若不加锁会发生什么。

**操作步骤**：

1. 阅读下文 4.3 的 `ReportError` 测试，理解最简 reporter 写法。
2. （**示例代码**，待本地验证）假设你用一个真实线程池作为 `Dispatch`，多个 wrapper call 并发失败时都会调 `reportError`。若你的 reporter 要把错误消息累计到一个 `std::vector<std::string>`，就**必须**给这个 vector 加锁，例如借用测试里的 `AccumulateErrors` 思路：

   ```cpp
   // 示例代码：线程安全的累计式 reporter
   std::mutex ErrMx;
   std::vector<std::string> ErrMsgs;
   auto reporter = [&](Error Err) {
     std::lock_guard<std::mutex> L(ErrMx);            // 必须加锁：reporter 可能并发
     ErrMsgs.push_back(toString(std::move(Err)));
   };
   ```

**需要观察的现象**：去掉 `lock_guard` 后，在高并发下偶发崩溃或消息丢失；加上后稳定。

**预期结果**：加锁版本下，`ErrMsgs.size()` 等于实际上报的错误数。**待本地验证**（需要真实多线程 Dispatch 才能复现竞争）。

> 这个实践点出的正是头文件里那句 `entry into the reporter is not synchronized` 的工程含义：reporter 的线程安全由**调用方**负责，Session 不替你加锁。

#### 4.2.5 小练习与答案

**练习 1**：`DispatchFn` 和 `ErrorReporterFn` 都接收一个 `move_only_function` 包装的入参。为什么不用 `std::function`？

**参考答案**：因为 `Task` 和 `Error` 都可能持有不可拷贝的资源（`Task` 闭包常捕获 `unique_ptr`、`shared_ptr`、`WrapperFunctionBuffer` 等；`Error` 本身就是不可拷贝的）。`std::function` 要求可调用对象可拷贝，而 orc-rt 自带的 `move_only_function`（见 u10-l3）放宽为仅移动，从而能承载这些捕获物。

**练习 2**：`ReportErrorsViaSession` 这个工具类解决了什么问题？举一个使用场景。

**参考答案**：它把 `Session&` 适配成 `void(Error)` 的可调用对象。场景：某个 API 要求传入一个 `move_only_function<void(Error)>` 作为错误汇槽，而你手里只有一个 `Session&`，不想手写 lambda，就可以 `ReportErrorsViaSession(S)` 直接作为那个汇槽传入。下文 4.3 的 `ReportErrorsViaSession` 测试正是验证它「等价于手写 lambda 调 `S.reportError`」。

---

### 4.3 从 TrivialConstructionAndDestruction 读懂最小构造

#### 4.3.1 概念说明

读源码最有效的方式之一是「读测试」——测试用最少的代码展示了对象的最小可用形态。`SessionTest.cpp` 里的 `TrivialConstructionAndDestruction` 与 `ReportError` 就是这样的「最小样例」：

- 前者证明：**只给三要素，构造和析构都能干净地完成**，不需要 attach、不需要 Service、不需要线程。
- 后者证明：**`reportError` 真的会把 `Error` 送到你注册的 reporter**。

这两个用例依赖 `CommonTestUtils.h` 里的三个占位工具，理解了它们，你就能看懂 `SessionTest.cpp` 里几乎所有测试的构造行。

#### 4.3.2 核心流程

读测试的固定套路：

```text
1. 看 Session 构造行，识别三要素分别用了什么工具
       mockExecutorProcessInfo()  ──► ExecutorProcessInfo
       noDispatch                 ──► DispatchFn
       noErrors                   ──► ErrorReporterFn
2. 对照 CommonTestUtils.h 看每个工具的真实行为
3. 看构造之后调用了 Session 的哪个公开方法（reportError / addService / ...）
4. 看断言期望了什么结果（EXPECT_*）
```

#### 4.3.3 源码精读

先看最简的构造测试：

[test/unit/SessionTest.cpp:333-335](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L333-L335) —— `TrivialConstructionAndDestruction`。整个测试体就一行构造语句：进入作用域构造、离开作用域析构。**没有任何 `EXPECT_*` 断言**——能不崩溃地走完构造与析构，本身就是它在断言的事。这也间接验证了「析构会触发 shutdown 并阻塞至完成」在「从未 attach」的退化情形下依然安全。

它用的三个工具定义在 `CommonTestUtils.h`：

[test/unit/CommonTestUtils.h:46-48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L46-L48) —— `mockExecutorProcessInfo()` 返回一个写死的 `ExecutorProcessInfo("arm64-apple-darwin", 16384)`。注意 triple 和 page size 都是**任意但合法**的假值，测试不依赖真实平台。

[test/unit/CommonTestUtils.h:55-58](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L55-L58) —— `noDispatch`：它先记一条测试失败（`ADD_FAILURE`），然后**仍然把 Task 跑掉**。设计很巧妙——「不该被调度」时一旦被调度就报失败，但又不会让调用方挂死（因为它把 Task 跑了，依赖结果的代码能解阻塞）。

[test/unit/CommonTestUtils.h:30](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L30) —— `noErrors`：`cantFail(std::move(Err))`，即「吞掉并检查」。它满足 `Error` 的检查契约（见 u2-l3），把任何错误就地消费掉。

再看 `ReportError` 测试，它展示了「自定义 reporter + 调 `reportError` + 断言收到消息」的标准范式：

[test/unit/SessionTest.cpp:337-349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L337-L349) —— `ReportError`。逐行拆解：

1. 先造一个成功的 `Error` 并 `cantFail` 它——这是为了把外层 `E` 「复位」到已检查的成功态，后面才好重新赋值。
2. 构造 Session，reporter 是一个 lambda `[&](Error Err) { E = std::move(Err); }`——**把收到的错误 move 到外层 `E`**。这就是「把错误转成变量保存」的标准手法。
3. 调 `S.reportError(make_error<StringError>("foo"))`——构造一个内容为 `"foo"` 的字符串错误并上报。
4. 断言：`if (E)` 为真表示收到失败值，`toString(std::move(E))` 应等于 `"foo"`；若 `E` 为假则记一条 `Missing error value` 失败。

紧随其后的 `ReportErrorsViaSession` 测试用完全相同的骨架，只是把 reporter 换成了对 `ReportErrorsViaSession(S)` 的调用：

[test/unit/SessionTest.cpp:351-364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L351-L364) —— 它证明 `ReportErrorsViaSession(S)` 与「手写 lambda 调 `S.reportError`」行为一致。

这两个测试合起来，就是本讲代码实践的「官方模板」。

#### 4.3.4 代码实践

**实践目标**：照着 `ReportError` 测试，构造一个 Session，注册一个「把错误转成字符串保存」的 reporter，调用 `reportError` 并断言收到正确消息。

**操作步骤**（**示例代码**，需在已配置 orc-rt 构建的环境里编译为 GoogleTest 用例）：

1. 在 `test/unit/` 下新建一个临时测试文件（或直接在本地实验工程里），写入：

   ```cpp
   // 示例代码：自定义 reporter 验证
   #include "orc-rt/Session.h"
   #include "orc-rt/Error.h"
   #include "orc-rt/ExecutorProcessInfo.h"
   #include "orc-rt/move_only_function.h"
   #include "gtest/gtest.h"
   #include <string>

   using namespace orc_rt;

   TEST(MySessionDemo, ReportErrorCapturesMessage) {
     std::string Captured;                 // 用字符串保存错误消息
     Error E = Error::success();
     cantFail(std::move(E));               // 复位到已检查成功态

     Session S(
       ExecutorProcessInfo("arm64-apple-darwin", 16384),
       [](Session::Task T) { T(); },       // inline 调度
       [&](Error Err) { Captured = toString(std::move(Err)); }
     );

     S.reportError(make_error<StringError>("hello orc-rt"));

     EXPECT_EQ(Captured, "hello orc-rt");  // 断言收到正确消息
   }
   ```

2. 用构建 orc-rt 时同样的工具链编译并运行（参见 u1-l2 的 `check-orc-rt-unit` 目标）。

**需要观察的现象**：

- 测试通过：`Captured` 被赋值为 `"hello orc-rt"`。
- 若把 reporter 改成什么都不做的 `[](Error){}`，由于 `Error` 未被检查/消费，析构时会 `abort()`——这正好印证 u2-l3 的检查契约。

**预期结果**：`EXPECT_EQ` 成立。若无法本地编译，标注「待本地验证」，并改为**源码阅读型实践**：对照 [SessionTest.cpp:337-349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L337-L349) 逐行复述：reporter 如何把 `Err` move 到外层 `E`、`reportError` 如何把 `"foo"` 送达、断言如何区分「收到失败值」与「丢失错误值」三种状态。

#### 4.3.5 小练习与答案

**练习 1**：`TrivialConstructionAndDestruction` 里没有任何 `EXPECT_*`，它到底在验证什么？

**参考答案**：它在验证「最小构造 + 自动析构」不会崩溃或挂起。能平安走过构造（含内部 `NotificationService` 的创建）与析构（含 `shutdown()` 触发与 `CV.wait` 阻塞至生命周期完成），本身就是被验证的行为；这是一种「冒烟测试」式的断言。

**练习 2**：`noDispatch` 被调用时会先 `ADD_FAILURE()` 再 `T()`。为什么不直接 `return;` 忽略 Task？

**参考答案**：因为忽略 Task 会让所有「依赖该 Task 结果才解阻塞」的调用方永久挂死。`noDispatch` 的语义是「在这个不该被调度的测试里，被调度就是错」——所以记一条失败表明出了问题——但又要把 Task 跑掉，让调用方能继续走完、释放资源（含托管代码 Token），避免测试卡死。这是「报错但不卡死」的稳健设计。

**练习 3**：在 `ReportError` 测试里，为什么开头要先 `Error E = Error::success(); cantFail(std::move(E));`，而不是直接声明 `Error E;`？

**参考答案**：`Error` 没有默认构造出一个「可安全析构」的状态——一个未经检查的 `Error` 析构会 `abort()`。开头先造成功值并用 `cantFail` 把它「检查并消费」掉，是为了把外层 `E` 置于一个明确的已检查状态，随后才能安全地用 `E = std::move(Err)` 重新赋值而不触发析构检查。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务：

> **任务**：写一个最小的「错误日志 Session」演示。
>
> 1. 用写死的 `ExecutorProcessInfo("arm64-apple-darwin", 16384)` 作为进程信息（参考 `mockExecutorProcessInfo`）。
> 2. 写一个 `DispatchFn`，把收到的 Task 直接在当前线程执行（即 `inlineDispatch` 的写法，见 [CommonTestUtils.h:61](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h#L61)）。
> 3. 写一个 reporter，把每条错误消息追加到一个 `std::vector<std::string>`，并**加锁**（体会「reporter 非同步」的约定）。
> 4. 构造 Session 后，连续调用三次 `reportError`，分别上报 `"err1"` / `"err2"` / `"err3"`。
> 5. 断言 vector 的大小为 3、内容依次为这三条。
> 6. 让 Session 离开作用域正常析构，确认程序干净退出。

完成后，请回答：你的 reporter 若**不加锁**，在当前这个单线程 inline 调度的例子里会出问题吗？为什么？（提示：本例不会，因为 reporter 实际上只被一个线程调用；但约定之所以是「非同步」，是因为**生产环境下** `Dispatch` 可能是多线程线程池，那时就会出问题——这正是 4.2.4 实践想点出的。）

这个任务把「三要素构造」「reportError 通道」「reporter 非同步约定」「析构触发 shutdown」四件事一次性串了起来。如果你能写出并解释清楚，说明本讲的核心已经掌握。

## 6. 本讲小结

- `Session` 是执行端的**协调者**：构造它要交给它三要素——`ExecutorProcessInfo`（进程 triple/page size）、`DispatchFn`（怎么跑 Task）、`ErrorReporterFn`（错误往哪报）。
- 构造函数体极简：`std::move` 存下三要素，并通过 `createService<NotificationService>()` 在内部预先放入一个通知服务，`Notifiers` 成员以引用绑定到它。
- `Session` **不可拷贝、不可移动**（四特殊成员全 `= delete`），所有持有者必须用稳定地址引用它。
- 析构会**触发 shutdown 并阻塞**等待生命周期结束（`CurrentState == Shutdown && TargetState == None`），所以 Session 离开作用域时一定会把自己关干净。
- 两个回调通道职责分明：`DispatchFn` 决定 Task 何时跑、`ErrorReporterFn` 决定错误往哪报；**reporter 的进入不是同步的**，可能被多线程并发调用，线程安全由调用方负责。
- 读测试是最好的入门：`TrivialConstructionAndDestruction` 验证最小构造/析构安全；`ReportError` 给出「自定义 reporter + `reportError` + 断言」的标准范式，两者都依赖 `CommonTestUtils.h` 里的 `mockExecutorProcessInfo` / `noDispatch` / `noErrors` 三个占位工具。

## 7. 下一步学习建议

本讲只讲了「Session 这个对象怎么造、肚子里装了什么」，刻意避开了它最复杂的部分——生命周期状态机。建议：

1. **紧接着学 u3-l2《生命周期状态机：attach / detach / shutdown》**：在那里你会看到 `State` 枚举的 `Start → Attached → Detached → Shutdown` 如何在 `TargetState` / `CurrentState` 的双变量协调下推进，以及 shutdown 的三阶段（Detach → Drain 托管代码 → 逆序 onShutdown）。
2. **再学 u3-l3《Service 接口与注册》**：把本讲只点到的 `Services` 容器展开，看 `addService` / `createService` / `tryCreateService` 三种注册方式的差异与逆序关闭约定。
3. 想提前看「DispatchFn 在真实场景里怎么被实现」的，可先翻 u4-l2 的 `QueueingRunner`（测试用）与 `ThreadPoolRunner`（生产用）。
4. 想验证自己理解的，回到 `SessionTest.cpp`，尝试在不看答案的前提下读懂 `SingleService`、`MultipleServices` 两个用例——它们是 u3-l3 的预热。
