# Controller–Executor 架构全景

## 1. 本讲目标

本讲是 orc-rt 的「地图课」之一。学完后你应该能够：

- 说清楚 **controller**（控制端）与 **executor**（执行端）的分工，以及 orc-rt 到底运行在哪一侧。
- 用一张图把 `Session`、`Service`、`ControllerAccess`、`WrapperFunction`、`TaskDispatcher` 这五个核心概念的位置和数据流向画出来。
- 解释为什么这条跨进程 RPC 是**不对称**的：controller→executor 按「地址」调用，executor→controller 按「tag」调用。
- 理解 wrapper function「字节进、字节出」的统一签名为什么能成为整个通信层的基石。
- 说清「托管代码（managed code）」与 Session 关闭顺序（detach → drain → shutdown）的关系。

本讲只建立**心智模型**，不深入实现细节；具体的状态机、缓冲三态、SPS 序列化等留给后续讲义（u3、u5、u6）展开。

## 2. 前置知识

在开始前，请确认你已经了解（这些都在 u1 系列讲义中建立）：

- **orc-rt 是什么**：它是 LLVM ORC JIT 的执行端运行时，与运行在控制端的 LLVM ORC 库配套使用。
- **controller / executor 二分**：控制端链接 LLVM ORC 库（如 `LLJIT`），负责编译与链接 JIT 代码；执行端链接 orc-rt，负责执行 JIT 代码并管理其运行时资源。两者可以同处一个进程，也可以跨进程。
- **ABI 不稳定**：orc-rt 仍处实验阶段，必须与同一次构建的 ORC 库配套使用。

本讲会用到几个名词，先一句话解释：

| 名词 | 一句话解释 |
| --- | --- |
| **JIT 代码** | 在程序运行期间才生成、链接、载入到内存里执行的机器码。 |
| **RPC** | Remote Procedure Call，远程过程调用——像调用本地函数一样调用另一个进程里的函数。 |
| **wrapper function（包装函数）** | orc-rt 里一切跨端调用最终都走到的统一函数签名（详见 4.4）。 |
| **托管代码（managed code）** | 由 orc-rt 的 Session 管理其生命周期的代码，主要是 JIT 代码，也包括为 JIT 代码加载的库代码。 |

> 关键直觉：你可以把 orc-rt 想象成「跑在执行进程里、替 JIT 代码打理一切的管家」。`Session` 就是这位管家本人，`Service` 是他手下的各个工种（管内存的、管动态库的……），`ControllerAccess` 是他与控制端之间的对讲机。

## 3. 本讲源码地图

本讲涉及的源码文件不多，但都是后续多讲会反复回头的「地基」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `docs/Design.md` | 设计文档，用自然语言讲清整个架构 | 全篇概念串讲的主线 |
| `include/orc-rt/Session.h` | `Session` 类与嵌套的 `ControllerAccess` 抽象 | 根对象、RPC 桥、托管代码、关闭顺序 |
| `include/orc-rt/Service.h` | `Service` 抽象接口 | 资源管理抽象的 `onDetach` / `onShutdown` |
| `include/orc-rt/TaskGroup.h` | `TaskGroup` / `Token` / `TokenSource` | 托管代码的「在执行中」标记 |
| `include/orc-rt-c/WrapperFunction.h` | wrapper function 的 C 定义 | 统一签名的字节缓冲与函数指针类型 |
| `include/orc-rt/WrapperFunction.h` | C++ 工具 `WrapperFunction::call/handle` | 字节进字节出的调用/处理工具 |
| `test/unit/SessionTest.cpp` | Session 的单元测试 | 真实可运行的调用示例（实践用） |

> 提示：阅读 orc-rt 时，`docs/Design.md` 是最高层视角，`*.h` 是契约，`test/unit/*.cpp` 是「如何用」的活文档。三者对照读效率最高。

## 4. 核心概念与源码讲解

本讲按「自顶向下」拆成五个最小模块：先看全景与根对象 `Session`，再看它拥有的 `Service`，再看它与控制端通信的 `ControllerAccess`，再看承载一切调用的统一签名 `WrapperFunction`，最后看「托管代码」如何决定关闭顺序。

### 4.1 全景与根对象：Session

#### 4.1.1 概念说明

`Session` 是执行端 JIT 程序的**根对象**。设计文档开篇就点明了它的地位：

[docs/Design.md:L21-L31](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L21-L31) —— 这段说明了三点：`Session` 是 JIT 程序的根；它**拥有**若干 `Service`（管理 JIT 内存、unwind 信息、动态库句柄等）；它必须在加入任何 JIT 代码**之前**构造，并且必须**活过**所有 JIT 代码的执行。

一句话概括它的职责边界：

> `Session` = 执行进程里「为 JIT 代码服务的一切资源」的拥有者和协调者。

一个执行进程里可以有多个 `Session`，此时每个 `Session` 只需活过「加到它自己名下」的 JIT 代码（见同一文档说明）。

#### 4.1.2 核心流程

`Session` 的一生大致这样推进（细节在 u3 展开，这里只看骨架）：

```text
构造 Session(进程信息, Dispatch回调, 错误上报回调)
        │
        ├── addService(...) / createService(...)   注册各 Service（内存、动态库……）
        │
        ├── attach<ControllerAccessT>(BootstrapInfo)  接上控制端
        │        │
        │        └── 进入 Attached 状态，开始双向 RPC
        │
        ├── （JIT 代码运行期间：托管代码进出栈、收发 wrapper 调用）
        │
        ├── detach()    断开控制端，通知所有 Service::onDetach
        │
        └── shutdown()  等托管代码排空 → 逆序调用 Service::onShutdown → 析构
```

注意状态机有四态：`Start → Attached → Detached → Shutdown`，定义在头文件里（本讲先用它定位概念，状态机细节见 u3-l2）。

#### 4.1.3 源码精读

`Session` 类的注释一句话点题：

[include/orc-rt/Session.h:L48-L49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L48-L49) —— 标注 `Session` 代表一个 ORC 执行端会话。

构造函数签名揭示了它需要的三要素：

[include/orc-rt/Session.h:L299-L300](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L299-L300) —— 三个参数分别是：`ExecutorProcessInfo`（进程信息，如目标三元组与页大小）、`DispatchFn`（运行 Task 的回调）、`ErrorReporterFn`（错误上报回调）。

`Session` 持有的核心成员集中体现在类末尾：

[include/orc-rt/Session.h:L626-L636](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L626-L636) —— 这里能看到它持有 `ManagedCodeTaskGroup`（托管代码任务组）、`CA`（`shared_ptr<ControllerAccess>`，控制端桥）、`Services`（拥有的 Service 列表）以及 `CurrentState/TargetState`（状态机协调）。

> 顺带一提：`Session` 是**不可拷贝也不可移动**的（[include/orc-rt/Session.h:L303-L306](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L303-L306)）。这符合「根对象」的身份——它被多处引用（包括被 C ABI 以不透明指针形式持有），地址必须稳定。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试，确认「构造 Session 需要哪三样东西」。

**操作步骤**：

1. 打开 `test/unit/SessionTest.cpp`，搜索 `Session S(`，观察测试里是如何构造 `Session` 的。
2. 典型写法形如（来自 `ValidCallToController` 测试附近）：

   ```cpp
   QueueingRunner<>::WorkQueue Tasks;
   Session S(mockExecutorProcessInfo(), QueueingRunner(Tasks), noErrors);
   ```

3. 把这三个实参分别对应到 `Session` 构造函数的三个形参：
   - `mockExecutorProcessInfo()` → `ExecutorProcessInfo EPI`
   - `QueueingRunner(Tasks)` → `DispatchFn Dispatch`
   - `noErrors` → `ErrorReporterFn ReportError`

**需要观察的现象**：你会看到几乎每个 Session 相关测试都以这三件套开头，说明这是使用 `Session` 的最小必要输入。

**预期结果**：能用一句话说出「构造一个 Session 必须告诉它：我在什么进程里、谁来跑 Task、错误往哪里报」。

#### 4.1.5 小练习与答案

**练习 1**：`Session` 为什么必须「先于任何 JIT 代码构造、后于其全部执行销毁」？

> **参考答案**：`Session` 拥有 JIT 代码赖以运行的资源（JIT 内存、unwind 信息等）。若 `Session` 先于 JIT 代码销毁，这些资源被释放，仍在栈上的 JIT 代码一旦返回就会访问已释放内存而崩溃；若在加入 JIT 代码之前没有 `Session`，则这些资源无处归属、JIT 代码也无人托管。详见 [docs/Design.md:L27-L28](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L27-L28)。

**练习 2**：一个进程里能有多个 `Session` 吗？如果有，各自的存活约束是什么？

> **参考答案**：能。每个 `Session` 只需活过「加到它名下」的那部分 JIT 代码的执行。见 [docs/Design.md:L30-L31](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L30-L31)。

---

### 4.2 Service：Session 拥有的资源管理抽象

#### 4.2.1 概念说明

`Service` 是一个抽象接口，代表「替 Session 管理某类资源或提供某类服务」的对象。典型例子是内存管理器、动态库加载器。它的核心特征是：**由 Session 拥有，并在 controller detach 与 Session shutdown 时被通知**。

[include/orc-rt/Service.h:L21-L25](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L21-L25) —— 头文件注释把这三点说得很清楚。

#### 4.2.2 核心流程

`Service` 接口只有两个虚函数，对应生命周期里的两个时机：

```text
Session 运行中（controller 在线）
        │
        ├── controller 断开 / Session.detach()  ──► onDetach(OnComplete, ShutdownRequested)
        │       （此后 controller 不会再发请求；可丢弃只为服务 controller 的记账）
        │
        └── Session.shutdown()                    ──► onShutdown(OnComplete)
                （所有托管代码已结束；在这里释放一切资源）
```

关键约定（来自 [docs/Design.md:L53-L68](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L53-L68)）：

- `onDetach` **总是**在 `onShutdown` 之前被调用，无论 Session 如何走到 shutdown——**即使从来没有 controller 接入过**。
- 很多 Service 的 `onDetach` 可以是空操作（no-op），它只是个「可以丢弃细粒度记账」的机会。

#### 4.2.3 源码精读

`Service` 接口本身极简：

[include/orc-rt/Service.h:L25-L56](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L25-L56) —— 两个纯虚函数：`onDetach(OnCompleteFn, bool ShutdownRequested)` 与 `onShutdown(OnCompleteFn)`。注意 `OnCompleteFn` 是 `move_only_function<void()>`（仅移动的可调用对象），意味着回调只能被「移动」走、恰好执行一次。

`onDetach` 的契约（[include/orc-rt/Service.h:L31-L50](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L31-L50)）特别强调三点：

1. 恰好被调用一次，且一定在 `onShutdown` 之前。
2. 触发场景有三：controller 主动断开、`Session::detach()` 被调用、Session shutdown（哪怕从没接入 controller）。
3. 若 `ShutdownRequested == true`，说明 shutdown 已经在排队，会在所有 Service 收到 detach 通知后继续。

注册 Service 有三种方式（都定义在 `Session` 上）：

- [include/orc-rt/Session.h:L322-L328](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L322-L328) `addService`：把一个已构造好的 `unique_ptr<Service>` 注册进去。
- [include/orc-rt/Session.h:L332-L335](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L332-L335) `createService`：原地构造（`make_unique`）后注册，构造不会失败时用。
- [include/orc-rt/Session.h:L343-L349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L343-L349) `tryCreateService`：通过 `ServiceT::Create(...)` 工厂构造，返回 `Expected`，适合构造可能失败的情况。

#### 4.2.4 代码实践

**实践目标**：阅读 `Service` 接口，确认「逆序关闭」与「两个回调」的约定。

**操作步骤**：

1. 打开 [include/orc-rt/Service.h:L50](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L50) 与 [include/orc-rt/Service.h:L55](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L55)，确认接口确实只有这两个虚函数。
2. 在 `Session.h` 里找到 `shutdown` 的文档注释（[include/orc-rt/Session.h:L406-L417](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L406-L417)），阅读它的三阶段说明。
3. 注意第 3 步明确写道："Calls `onShutdown` on all Services **in reverse order**"——即按注册的**逆序**关闭。

**需要观察的现象**：shutdown 文档把关闭拆成三步：① Detach（若未 detach）→ 通知所有 `onDetach`；② Drain（等托管代码结束）；③ 逆序调用 `onShutdown`。

**预期结果**：能复述三阶段，并指出「逆序」是为了让后注册的、可能依赖前者资源的 Service 先被释放（类似栈式析构）。

> 「逆序关闭」的精确实现与 `MultipleServices` 测试验证见 u3-l3，本讲只需建立这个印象。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 Service 的资源「只为服务 controller 请求」而存在，它应该在哪个回调里丢弃这些记账？

> **参考答案**：在 `onDetach`。因为 `onDetach` 之后 controller 不会再发请求，此时丢弃只为 controller 服务的细粒度记账正合适；很多 Service 的 `onDetach` 因此是 no-op。见 [docs/Design.md:L60-L64](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L60-L64)。

**练习 2**：`onDetach` 的第二个参数 `ShutdownRequested` 为 `true` 意味着什么？Service 此时应当如何反应？

> **参考答案**：意味着 shutdown 已经在排队，会在所有 Service 收到 detach 通知后继续推进。Service 可以据此决定是否跳过某些「只在长期在线时有意义」的清理。见 [include/orc-rt/Service.h:L43-L49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L43-L49)。

---

### 4.3 ControllerAccess：不对称的双向 RPC 桥

#### 4.3.1 概念说明

`ControllerAccess` 是 `Session` 与控制端之间的双向 RPC 抽象。它支持两个方向的调用，但这两个方向**不对称**——这是 orc-rt 架构里最关键、也最容易被忽略的一点。设计文档原话：

[docs/Design.md:L42-L47](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L42-L47) —— 翻译成要点：

- **controller → executor**：按 wrapper function 的**地址**调用。也就是说，controller 可以调用 executor 里的任意代码。
- **executor → controller**：按 **tag** 调用。tag 是「executor 进程里的地址」，但它只是个**句柄**，对应控制端里某个被刻意暴露出来的处理器。

这种不对称体现了**最小权限**思想：执行端不能随心所欲地调用控制端的任意函数，只能调用那些被「登记为 tag」的入口。

另外，`ControllerAccess` 可以在 Session 结束**之前**就被 detach——此时 JIT 代码仍可继续执行，只是不再收到 controller 的调用，也无法再向 controller 发起调用（[docs/Design.md:L49-L51](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L49-L51)）。

#### 4.3.2 核心流程

`ControllerAccess` 是 `Session` 的**嵌套类**，定义了执行端与控制端之间的契约。两个方向的数据流：

```text
方向 A：controller → executor（按地址）
   controller 拿到 executor 里某个 wrapper function 的地址
        │  handleWrapperCall(Fn, ArgBytes, CallId)
        ▼
   Session 取一个 managed-code Token，把工作封装成 Task 交给 Dispatch 运行

方向 B：executor → controller（按 tag）
   执行端代码  Session::callController(OnComplete, Tag, ArgBytes)
        │  若已 attach：委托给 ControllerAccess::callController 排队等结果
        │  若未 attach / 已断开：直接返回 out-of-band 错误 "no controller attached"
        ▼
   结果回来后，通过 OnControllerCallReturn 回调（恰执行一次）
```

#### 4.3.3 源码精读

`ControllerAccess` 是嵌套在 `Session` 内的抽象基类：

[include/orc-rt/Session.h:L95-L99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L95-L99) —— 它持有对所在 `Session` 的引用（构造时传入 `Session &S`）。

四个核心纯虚/接口方法：

- [include/orc-rt/Session.h:L158](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L158) `connect(BootstrapInfo)` —— 与控制端建立连接。
- [include/orc-rt/Session.h:L189](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L189) `disconnect()` —— 发起断开（必须恰一次地 `notifyDisconnected`，并排空所有 pending 调用）。
- [include/orc-rt/Session.h:L215-L217](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L215-L217) `callController(OnComplete, Tag, ArgBytes)` —— 执行端向控制端发起调用（方向 B）。
- [include/orc-rt/Session.h:L220-L221](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L220-L221) `sendWrapperResult(ResultBytes, CallId)` —— 把某个 wrapper 调用的结果发回控制端。

`Session::callController` 的内联实现体现了「未接入就立即失败」的语义：

[include/orc-rt/Session.h:L483-L491](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L483-L491) —— 它用 `std::atomic_load(&CA)` 读取当前的控制端桥；若有，委托给它；若无，直接用 `createOutOfBandError("no controller attached")` 完成回调。注意这里用的是**原子加载**，因为 `CA` 这个 `shared_ptr` 可能在断开时被另一线程改写。

`tag` 的类型只是一个不透明指针：

[include/orc-rt-c/CoreTypes.h:L34](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/CoreTypes.h#L34) —— `orc_rt_ControllerHandlerTag` 就是 `void *`。执行端把某个地址 reinterpret 成 tag 传给 `callController`，由控制端把它映射到真实处理器。

#### 4.3.4 代码实践

**实践目标**：从真实测试里看到「执行端按 tag 调用控制端」的代码，并观察「未 attach 时调用失败」的现象。

**操作步骤**：

1. 打开 [test/unit/SessionTest.cpp:L730-L745](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L730-L745)，阅读 `ValidCallToController` 测试。注意它如何取得 caller：

   ```cpp
   SPSWrapperFunction<int32_t(int32_t, int32_t)>::call(
       S.controllerCaller(
           reinterpret_cast<orc_rt_ControllerHandlerTag>(add_sps_wrapper)),
       [&](Expected<int32_t> R) { Result = cantFail(std::move(R)); }, 41, 1);
   ```

   这里 `add_sps_wrapper`（一个 wrapper 函数地址）被 reinterpret 成 tag，传给 `controllerCaller`。这就是「executor → controller 按 tag 调用」。

2. 再打开 [test/unit/SessionTest.cpp:L747-L763](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L747-L763)，阅读 `CallToControllerBeforeAttach`。该测试**不** attach 就发起调用。

**需要观察的现象**：`CallToControllerBeforeAttach` 断言结果是字符串 `"no controller attached"`——正好对应 4.3.3 里 `Session::callController` 走的 else 分支。

**预期结果**：能解释「为什么未 attach 时调用会得到一个带外错误，而不是崩溃或挂起」——因为 `callController` 用原子读检查 `CA`，为空就用 `createOutOfBandError` 同步完成回调。

#### 4.3.5 小练习与答案

**练习 1**：为什么 controller→executor 能「按地址调用任意代码」，而 executor→controller 只能「按 tag 调用」？

> **参考答案**：出于最小权限。controller 是可信的编排者，需要能调度 executor 的任意 wrapper；而 executor（尤其运行着 JIT 代码）不应能随意触达 controller 的任意函数，只能调用那些被显式登记为 tag 的入口。见 [docs/Design.md:L42-L47](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L42-L47)。

**练习 2**：`Session::callController` 读取 `CA` 时为什么用 `std::atomic_load` 而不是普通读取？

> **参考答案**：因为 `CA`（`shared_ptr<ControllerAccess>`）可能在另一线程因断开而被改写/置空。原子加载保证了「读引用计数」这一步本身是线程安全的，不会在并发断开时读到半更新的指针。见 [include/orc-rt/Session.h:L486-L487](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L486-L487)。

---

### 4.4 WrapperFunction：统一的「字节进、字节出」签名

#### 4.4.1 概念说明

无论哪个方向、无论参数多复杂，orc-rt 里一切跨端调用最终都收敛到**同一个 C 函数签名**：接收一段字节（参数）、返回一段字节（结果）。设计文档称之为 wrapper function：

[docs/Design.md:L103-L131](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L103-L131) —— 给出了统一签名与两个关键 C 类型。

为什么要有这样一个统一签名？因为 `ControllerAccess` **不应该关心字节里装的是什么**——它只负责把字节从一端搬到另一端；字节的解释（参数是什么类型、怎么编码）完全交给被调用的函数自己。这让通信层与业务层彻底解耦：

- 通信层（`ControllerAccess`）只认字节；
- 业务层（wrapper 函数 + 序列化方案）决定字节的含义。

> 直觉：把 wrapper function 想成「一根只传字节的水管」。水管两端的人各自带「翻译器」（序列化方案），把结构化数据翻成字节塞进去、再把出来的字节翻回结构化数据。下一讲（u5、u6）会专门讲这根水管和翻译器。

#### 4.4.2 核心流程

wrapper function 的统一 C 签名是异步的——它不直接返回结果，而是通过一个 `Return` 回调把结果送回去：

```text
调用方：把参数序列化成 ArgBytes ──► wrapper function(Session, ArgBytes, Return, CallId)
                                                       │
                                  （业务逻辑：反序列化 → 执行 → 序列化结果）
                                                       ▼
                                  Return(Session, ResultBytes, CallId) ──► 调用方拿到结果
```

其中 `ArgBytes` / `ResultBytes` 都是 `orc_rt_WrapperFunctionBuffer`——一个带三种内部状态的小缓冲（small / large / out-of-band error），细节在 u5-l1 展开。

#### 4.4.3 源码精读

统一 C 签名与回调类型定义在 C 头文件：

[include/orc-rt-c/WrapperFunction.h:L69-L72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L69-L72) —— 这就是 `orc_rt_WrapperFunction` 函数指针类型：`void(SessionRef, ArgBytes, Return, CallId)`。

[include/orc-rt-c/WrapperFunction.h:L57-L59](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L57-L59) —— 异步返回回调 `orc_rt_WrapperFunctionReturn`。

承载字节的 `orc_rt_WrapperFunctionBuffer` 是个 C-SmallVector，带 out-of-band 错误态：

[include/orc-rt-c/WrapperFunction.h:L32-L52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L32-L52) —— 注释说明了它的三态判定：`Size==0 且 ValuePtr 非空` = 带外错误；`Size <= sizeof(Data.Value)` = small（就地存）；`Size > sizeof(Data.Value)` = large（malloc 分配）。本讲只需知道「字节缓冲还能顺带携带一个错误字符串」。

C++ 侧提供了 `WrapperFunction` 工具结构体，封装 `call`（发起调用）与 `handle`（处理调用）：

[include/orc-rt/WrapperFunction.h:L211-L213](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L211-L213) —— 工具入口。

`handle` 的骨架（反序列化 → 执行 → 序列化）：

[include/orc-rt/WrapperFunction.h:L372-L398](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L372-L398) —— 注意它一进来就检查 `ArgBytes.getOutOfBandError()`：如果调用方送来的就是错误，则原样短路返回；否则才进入反序列化。这就是「带外错误」的短路路径。

#### 4.4.4 代码实践

**实践目标**：读懂一个最小的 wrapper 处理器，并指出「字节进、字节出」体现在哪里。

**操作步骤**：

1. 打开 [test/unit/SessionTest.cpp:L670-L679](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L670-L679)，阅读 `add_sps_wrapper`：

   ```cpp
   static void add_sps_wrapper(orc_rt_SessionRef S,
                               orc_rt_WrapperFunctionBuffer ArgBytes,
                               orc_rt_WrapperFunctionReturn Return,
                               uint64_t CallId) {
     SPSWrapperFunction<int32_t(int32_t, int32_t)>::handle(
         S, ArgBytes, Return, CallId,
         [](move_only_function<void(int32_t)> Return, int32_t X, int32_t Y) {
           Return(X + Y);
         });
   }
   ```

2. 把它的签名与 4.4.3 的 `orc_rt_WrapperFunction` 类型逐字对齐：参数顺序就是 `(S, ArgBytes, Return, CallId)`。

3. 注意它把「反序列化两个 int32、相加、再把结果序列化回去」的细节全部委托给了 `SPSWrapperFunction<...>::handle`——这正是「水管+翻译器」里翻译器的工作。

**需要观察的现象**：函数体里看不到任何手动拼字节/拆字节的代码，全是结构化的 `int32_t`。说明字节编解码被序列化层（SPS）吸收了。

**预期结果**：能指出「字节进字节出」的统一签名体现在函数签名本身，而具体类型的还原交给 SPS（u6 详讲）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ControllerAccess` 不去解释 `ArgBytes` 里装的是什么？

> **参考答案**：为了解耦。`ControllerAccess` 只负责搬运字节，业务含义由 wrapper 函数自己用序列化方案解释。这样通信层可以保持简单、稳定，而业务层可以自由演化。见 [docs/Design.md:L38-L41](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L38-L41)。

**练习 2**：`WrapperFunction::handle` 在一开始为什么要先检查 `ArgBytes.getOutOfBandError()`？

> **参考答案**：如果调用方送来的 `ArgBytes` 本身就是一个带外错误（比如序列化失败、或上一跳的错误透传），就没有「正常参数」可反序列化了。此时原样把错误通过 `Return` 短路回去，避免对错误缓冲做无意义的反序列化。见 [include/orc-rt/WrapperFunction.h:L383-L384](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L383-L384)。

---

### 4.5 托管代码与关闭顺序

#### 4.5.1 概念说明

「托管代码（managed code）」指由 `Session` 管理其生命周期的代码——主要是 JIT 代码，也包括为 JIT 代码加载的库代码。本模块解决一个关键问题：

> **Session 的拆除绝不能在 JIT 代码还活在栈上时进行**——否则一旦控制权返回到那些栈帧，就会访问到已释放的内存而崩溃。

设计文档用了一整节讲这件事：

[docs/Design.md:L70-L93](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L70-L93)。

机制是：`Session` 持有一个托管代码 `TaskGroup`。凡是准备运行 JIT 代码的地方，先从该组取一个 **Token** 来「标定」这段执行；`TaskGroup` 会延迟 Session 的 shutdown，直到所有 Token 被释放。

#### 4.5.2 核心流程

Token 与关闭的关系（设计文档 L72-L93 的精炼）：

```text
运行 JIT 代码前：取 Token ──► TaskGroup 计数 +1
        │  （Token 存活期间 = JIT 代码可能还在栈上）
        ▼
JIT 代码返回 / Token 离开作用域 ──► 计数 -1

shutdown 请求到来：
   ① Detach（断开 controller、通知 Service::onDetach）
   ② Drain：等 ManagedCodeTaskGroup 计数归零（所有 Token 释放）
   ③ 逆序调用 Service::onShutdown
```

两条重要约束（来自 [docs/Design.md:L83-L93](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L83-L93)）：

1. **一个 Token 只覆盖一段同步执行栈，不覆盖一整串异步操作**。异步续体被调用时，调用方必须重新取一个 Token 来标定。
2. **一旦 shutdown 被请求，取 Token 就会失败**——即便调用方已经持有 Token，嵌套/恢复调用也可能失败。因此每个进入 JIT 代码的调用方都必须有能力在取不到 Token 时中止并展开。

#### 4.5.3 源码精读

`Token` 是 `TaskGroup` 的嵌套类，构造时可能失败，必须用 `operator bool()` 检查：

[include/orc-rt/TaskGroup.h:L47-L60](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L47-L60) —— 注释明确：构造可能因 group 已关闭而失败，必须检查。

取/还 Token 的实现：

[include/orc-rt/TaskGroup.h:L155-L161](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L155-L161) `acquireToken` —— 若 group 已 `Closed` 则返回 false（取不到）。

[include/orc-rt/TaskGroup.h:L166-L178](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/TaskGroup.h#L166-L178) `releaseToken` —— 计数减一；若已 close 且计数归零，触发 `OnComplete` 回调（即「排空了，可以继续 shutdown」）。

`Session` 暴露的便捷入口：

[include/orc-rt/Session.h:L471-L476](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L471-L476) `callManagedCode` —— 自动取 Token、调用函数、释放 Token。Token 只在同步调用期间持有；函数返回后延迟到别处的工作**不**被覆盖。

[include/orc-rt/Session.h:L438-L440](https://github.com/llvm-llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L438-L440) `managedCodeTokenSource` —— 需要手动取 Token 时（如异步续体）从这取 `TokenSource`。

shutdown 的三阶段在 `Session::shutdown` 的文档里写得最完整：

[include/orc-rt/Session.h:L406-L417](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L406-L417) —— ① Detach；② Drain（等托管代码）；③ 逆序 `onShutdown`。

#### 4.5.4 代码实践

**实践目标**：在源码里找到「取不到 Token 就放弃」的实例，体会「取 Token 可能失败」这一约束。

**操作步骤**：

1. 打开 [include/orc-rt/Session.h:L562-L577](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L562-L577)，阅读 `Session::handleWrapperCall`。注意它一进来就构造 `TaskGroup::Token T(ManagedCodeTaskGroup)`，紧接着 `if (!T) return;`——这就是「取不到 Token 就直接放弃」。
2. 阅读它后面的注释：解释了为什么取不到 Token 时不返回错误——`ManagedCodeTaskGroup` 只在 detach 之后才关闭，此时 controller 应当已经发了错误信号，且执行端也无通道回传错误。

**需要观察的现象**：`handleWrapperCall` 把 `Token` 连同调用一起 move 进一个 Task，交给 `Dispatch` 运行。这意味着「正在处理某个 wrapper 调用」期间，Token 一直存活，从而推迟 shutdown。

**预期结果**：能说清「为什么 shutdown 必须先 drain 托管代码」——因为只要还有 Token 存活（还有 JIT/托管代码在栈上或在 Task 队列里），就不能安全释放资源。

> 本讲只建立「Token 延迟 shutdown」的直觉；状态机层面 detach/shutdown 如何与 drain 串接、`callManagedCode` 返回 `std::nullopt` 的细节，见 u3-l2 与 u4-l1。

#### 4.5.5 小练习与答案

**练习 1**：为什么一个 Token **不能**覆盖一整串异步操作的链？

> **参考答案**：Token 只覆盖「取它那一刻起到同步调用返回」这段在当前栈上的执行。异步续体被调用时，往往是在另一个栈、另一个时刻被唤起——原来的 Token 早已随那次同步调用返回而释放。续体的入口点本身可能就是 JIT 代码，它无法给自己套 Token，所以必须由唤起续体的人重新取一个 Token。见 [docs/Design.md:L83-L87](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L83-L87)。

**练习 2**：`shutdown` 被请求之后，新发起的托管代码调用会怎样？

> **参考答案**：取 Token 会失败（`ManagedCodeTaskGroup` 已关闭），`callManagedCode` 会返回 `std::nullopt`（或 void 版本的 `false`）且不调用目标函数；`handleWrapperCall` 则直接 `return` 放弃。调用方必须检查并展开。见 [docs/Design.md:L89-L93](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L89-L93)。

---

## 5. 综合实践：画一张 Controller–Executor 架构图

这是本讲的主实践任务。目标是把上述五个概念在一张图上对齐位置与数据流向，形成可以长期回忆的「一页纸心智模型」。

### 实践目标

画出 controller 进程、executor 进程、`Session`、`ControllerAccess`、若干 `Service`，并用箭头标出两个方向的 RPC。

### 操作步骤

1. 在纸上或任意画图工具里，画两个大方框，分别写 **controller 进程** 和 **executor 进程**。
2. 在 executor 进程方框里，画一个大圆/方框写 `Session`（根对象）。
3. 在 `Session` 下方画几个小方框代表它**拥有**的 `Service`（例如 `SimpleNativeMemoryMap`、`NativeDylibManager`），用「实心箭头（拥有）」从 `Session` 指向它们。
4. 在 `Session` 旁边画一个 `ControllerAccess`，用线连到 `Session`（它是 `Session` 的成员 `CA`）。
5. 用两种**不同颜色/线型**的箭头表示两个方向：
   - **方向 A（controller → executor，按地址）**：从 controller 进程出发，指向 executor 里某个 wrapper function 的地址。旁注「按地址」。
   - **方向 B（executor → controller，按 tag）**：从 `Session::callController` 出发，指向 controller 进程，旁注「按 tag」。注意箭头从 executor 里的某个地址（被 reinterpret 成 tag）出发。
6. 在两个方向上各标一个小「字节缓冲」图标，旁注 `WrapperFunctionBuffer`，提示「一切调用都收敛成字节」。
7. 在 `Session` 内部标出 `ManagedCodeTaskGroup`，并在它与「shutdown 三阶段」之间画一条「Token 存活 ⇒ 延迟 shutdown」的关系线。
8. 在图的角落写一句图例：实心箭头 = 拥有；虚线箭头 = RPC 调用；颜色 A = 按地址；颜色 B = 按 tag。

### 需要观察的现象（自查清单）

完成图后，用下面几条自检，每条都应能在图上找到对应：

- [ ] `Session` 是 executor 进程里的根，`Service` 由它拥有。
- [ ] 两个方向的箭头颜色/线型不同，且方向 A 标了「按地址」、方向 B 标了「按 tag」。
- [ ] 两个方向都经过了「字节缓冲」（wrapper function 统一签名）。
- [ ] `ControllerAccess` 横跨在 `Session` 与 controller 进程之间。
- [ ] 托管代码 `TaskGroup` 与 shutdown 的「drain」阶段相连。

### 预期结果

得到一张类似下文的文字版示意图（你可以把自己的图与它对照）：

```text
        ┌──────────── controller 进程 (链接 LLVM ORC 库, 如 LLJIT) ───────────┐
        │                                                                      │
        │   ExecutionSession   <──────────── 按tag调用 ────────────┐           │
        │       │                                                 │           │
        │       │ 按地址调用                                      │           │
        │       ▼                                                 │           │
        └───────┼─────────────────────────────────────────────────┘           │
                │                                                              │
   wrapper bytes│                          wrapper bytes (按tag,带回结果)      │
                ▼                                                              │
        ┌──── executor 进程 (链接 orc-rt) ────────────────────────────┐       │
        │                                                              │       │
        │   ┌─── ControllerAccess ───┐  (成员 CA, atomic 管理)         │       │
        │   │  connect/disconnect/   │ ◄──────────────────────────────┘       │
        │   │  callController(tag)/  │   方向B: executor→controller, 按tag     │
        │   │  sendWrapperResult     │                                         │
        │   └────────────┬───────────┘                                         │
        │                │ 拥有                                                │
        │   ┌────────────▼─────────────────────────── Session (根对象) ──┐    │
        │   │  ExecutorProcessInfo / DispatchFn / ErrorReporterFn        │    │
        │   │  ManagedCodeTaskGroup  ──► Token 存活 ⇒ 延迟 shutdown       │    │
        │   │  owns ─► Service: SimpleNativeMemoryMap                     │    │
        │   │          Service: NativeDylibManager  (逆序 onShutdown)     │    │
        │   │  State: Start → Attached → Detached → Shutdown              │    │
        │   └────────────────────────────────────────────────────────────┘    │
        └──────────────────────────────────────────────────────────────────────┘
```

> 说明：方向 A「controller → executor 按地址」在上图中由 controller 的 `按地址调用` 出发、向下进入 executor 的 wrapper function；方向 B「executor → controller 按 tag」由 `ControllerAccess::callController` 出发、向上回到 controller。两段都只搬运 `WrapperFunctionBuffer` 字节。

## 6. 本讲小结

- **controller / executor 二分**：控制端链接 LLVM ORC 库负责编译链接，执行端链接 orc-rt 负责执行与资源管理；两者可同进程可跨进程。
- **`Session` 是根**：执行端一切资源的拥有者与协调者，必须先于 JIT 代码构造、后于其执行销毁，且不可拷贝/移动。
- **`Service` 是资源抽象**：由 `Session` 拥有，只有 `onDetach` / `onShutdown` 两个回调；`onDetach` 总在 `onShutdown` 前，逆序关闭。
- **`ControllerAccess` 是不对称双向 RPC 桥**：controller→executor 按地址（可调任意代码），executor→controller 按 tag（只能调被刻意暴露的入口）；未 attach 时 `callController` 立即返回 `"no controller attached"` 带外错误。
- **`WrapperFunction` 是统一签名**：一切跨端调用都收敛成「字节进、字节出」的异步 C 签名，通信层只搬字节、不解释含义。
- **托管代码与关闭顺序**：`TaskGroup` + `Token` 标定「JIT 代码在执行」，从而把 shutdown 推迟到所有 Token 释放；shutdown 三阶段为 Detach → Drain → 逆序 onShutdown。

## 7. 下一步学习建议

本讲建立的是全景心智模型，接下来的学习建议：

- 想深入 **`Session` 的构造细节与生命周期状态机**：进入 u3-l1（Session 对象与构造）、u3-l2（attach / detach / shutdown 状态机）。
- 想深入 **`Service` 接口与注册**：进入 u3-l3（Service 接口与注册）。
- 想深入 **RPC 通信层**：进入 u5 系列（WrapperFunctionBuffer 的三态、call/handle、ControllerAccess 的 drain 协议）。
- 想深入 **托管代码与任务分发**：进入 u4-l1（TaskGroup / Token）、u4-l2（DispatchFn 与 Runner）。
- 在进入下一篇前，建议先回头确认本讲「综合实践」的架构图你能不看资料默画出来——它是后续所有讲义的「坐标系」。
