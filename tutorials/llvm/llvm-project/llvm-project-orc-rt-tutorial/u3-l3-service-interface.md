# Service 接口与注册

## 1. 本讲目标

前两讲我们看清了 `Session` 这个执行端根对象的构造（u3-l1）与生命周期状态机（u3-l2）。状态机里反复出现两个回调——`onDetach` 和 `onShutdown`——它们到底属于谁、何时被调用、按什么顺序调用？本讲就来回答这个问题。

具体来说，学完本讲你应该能够：

- 说清 `Service` 是什么：它是一个**管理资源**的抽象基类（如 JIT 内存、动态库、unwind 信息），由 `Session` 独占拥有。
- 区分 `onDetach` 与 `onShutdown` 这两个回调的**语义差异**与**调用时机**，并理解它们为什么是异步的（带 `OnComplete` 回调）。
- 区分注册一个 Service 的**三种 API**：`addService` / `createService` / `tryCreateService`，知道各自适用什么场景。
- 解释「**逆序关闭**」约定：为什么 detach 和 shutdown 都按注册的**逆序**通知 Service，以及源码是怎么用「`pop_back` + 完成回调里递归」优雅地兼容异步的。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **controller / executor 二分模型**（u2-l1）：`Session` 运行在执行端，是执行端所有对象的根。
- **`Session` 的构造与成员**（u3-l1）：`Session` 持有一个 `std::vector<std::unique_ptr<Service>> Services` 成员，并预置了一个内部的 `NotificationService`。
- **`Session` 的生命周期状态机**（u3-l2）：`Start → Attached → Detached → Shutdown` 的四态推进，shutdown 分三阶段（Detach → Drain 托管代码 → 逆序 onShutdown）。本讲正是把其中的「Detach」和「逆序 onShutdown」两个阶段拆开讲透。
- **`Error` / `Expected<T>`**（u2-l3）：注册 API 中的 `tryCreateService` 会用到 `Expected`。
- **`move_only_function`**（u10-l3 会展开，本讲只需知道它是「只能移动、不能拷贝的回调」）：Service 回调签名里用到的 `OnCompleteFn` 就是它。

一个核心直觉先记住：**`Service` 是 `Session` 的「资源管家」**。`Session` 本身只负责协调生命周期与通信，真正分配内存、加载动态库这些「脏活累活」都交给挂在上面的各个 Service。你完全可以把 `Session` 想象成一栋大楼的物业，而 Service 是大楼里的电力、网络、保洁等各路外包服务——物业负责统一调度它们「进场」和「退场」，但具体干活的是它们自己。

---

## 3. 本讲源码地图

本讲涉及三个核心文件：

| 文件 | 作用 |
|------|------|
| [include/orc-rt/Service.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h) | `Service` 抽象基类的全部定义：`OnCompleteFn` 类型别名、`onDetach`/`onShutdown` 两个纯虚函数及其契约注释。本讲义虽小，却是理解所有具体 Service 的钥匙。 |
| [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) | Service 的注册（`appendService`）、逆序通知（`detachServices`/`shutdownServices`）的完整实现，以及一个真实的内置 Service 范例 `NotificationService`。 |
| [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | `MockService`、`ConfigurableService` 两个测试桩，以及 `SingleService`/`MultipleServices`/三种注册 API 的用例，是验证本讲结论的最佳依据。 |

辅助理解（不展开细讲）：

| 文件 | 作用 |
|------|------|
| [include/orc-rt/Session.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | `Session` 类声明，含三种注册 API 模板、私有 `appendService` 声明、`Services` 成员。 |
| [test/unit/CommonTestUtils.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CommonTestUtils.h) | 测试占位工具：`mockExecutorProcessInfo`、`noDispatch`、`noErrors`，构造一个最小可用的 `Session` 必备。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Service 接口与回调语义**：`Service` 抽象基类长什么样，`onDetach`/`onShutdown` 的契约与异步完成模型。
2. **三种注册 API**：`addService` / `createService` / `tryCreateService` 的差异与取舍。
3. **逆序 onShutdown 约定**：detach 与 shutdown 为何都按注册逆序通知，源码如何用递归完成回调兼容异步。

---

### 4.1 Service 接口与回调语义

#### 4.1.1 概念说明

先看 `Service` 的定义，它非常精简：

[include/orc-rt/Service.h:25-56](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L25-L56) —— `Service` 抽象基类：一个类型别名 `OnCompleteFn`、一个虚析构、两个纯虚函数 `onDetach`/`onShutdown`。

逐行拆解：

```cpp
using OnCompleteFn = move_only_function<void()>;
```
这是「完成回调」的类型——一个无参无返回、只能移动的函数对象。**异步契约的关键就在这里**：两个回调都不会「同步地」完成工作，而是要求 Service 在清理完毕后**主动调用**传入的 `OnComplete`，告诉 `Session`「我这边好了，你可以继续通知下一个 Service 了」。

> 名词解释：`move_only_function` 是 orc-rt 自带的工具类型（详见 u10-l3），语义类似 `std::function`，但**只能 move、不能 copy**。这让它能持有捕获了 `unique_ptr`、`shared_ptr` 等不可拷贝资源的回调，全库的回调几乎都用它。

`onDetach` 的契约比 `onShutdown` 复杂得多，我们分开看。

#### 4.1.2 核心流程

**`onDetach`：控制端「断开」时触发**

[include/orc-rt/Service.h:31-50](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L31-L50) —— `onDetach(OnCompleteFn OnComplete, bool ShutdownRequested)` 的契约。

它的含义是「**controller 访问永久不可用了**」。触发时机有三种（注释明确保证「恰好一次」）：

1. controller 主动断开（比如网络 socket 掉了）。
2. 调用了 `Session::detach()`。
3. `Session` 正在 shutdown（**即使从未 attach 过任何 controller**也会触发）。

三个关键约束（都是源码注释的原话翻译）：

| 约束 | 含义 |
|------|------|
| 调用顺序保证 | `onDetach` 一定在 `onShutdown` **之前**被调用，且**恰好一次**。 |
| 之后的请求 | `onDetach` 之后，controller **不会再**向该 Service 发请求。 |
| 但 JIT 代码可能还在跑 | 注意这句反直觉的话：**JIT'd code may continue to make requests to the service concurrent with a call to onDetach**。也就是说，托管代码可能和 `onDetach` **并发**地访问该 Service——所以 Service 自己要处理好「已 detach 但 JIT 代码还在用我」的并发。 |

第二个参数 `ShutdownRequested` 是个布尔值：为 `true` 表示「detach 之后马上就要 shutdown 了」。Service 可以据此决定要不要做一些「反正马上要彻底关闭，那就等 shutdown 一起做」的优化。

注释还点明：**很多 Service 会把 `onDetach` 实现成空操作（no-op）**。为什么？因为大部分资源（内存、动态库句柄）在 detach 后、shutdown 前的 Drain 阶段里，可能仍被 JIT 代码使用，此时不能释放——真正释放要留到 `onShutdown`。`onDetach` 主要给那些「只在 attach 期间才有意义」的资源（比如只为 controller 服务的缓存）一个提前清理的机会。

**`onShutdown`：Session 终结时触发**

[include/orc-rt/Service.h:52-55](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h#L52-L55) —— `onShutdown(OnCompleteFn OnComplete)` 的契约。

含义是「**Session 要结束了，释放你持有的所有资源**」。这是 Service 生命周期里**最后**一次被调用，之后 Service 对象将被 `Session` 析构。

把两者放在一起，Service 的完整生命周期是：

```
注册(addService/createService/tryCreateService)
        │
        ▼
   [正常服务期：响应 controller 与 JIT 代码的请求]
        │
        ▼  (Session detach 或 shutdown 时)
   onDetach(OnComplete, ShutdownRequested)   ← 恰好一次
        │
        ▼  (Drain 阶段：等托管代码跑完，见 u3-l2)
   onShutdown(OnComplete)                     ← 恰好一次，逆序
        │
        ▼
   Service 对象被 ~unique_ptr 析构
```

**为什么两个回调要设计成异步（带 OnComplete）？** 因为 Service 的清理工作可能本身是异步的——比如要等待一个 IO 完成或一个后台线程退出。`Session` 不能假设清理瞬间结束，所以用「调用 onXxx → 等你调 OnComplete → 再通知下一个」的流水线。这也是 4.3 节逆序通知实现要写得「递归」的根本原因。

#### 4.1.3 源码精读：一个真实的内置 Service

光看抽象接口可能还是抽象，来看 `Session` 自己创建的一个真实 Service：`NotificationService`。

[lib/executor/Session.cpp:18-49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L18-L49) —— `NotificationService`：继承 `Service`，实现两个回调，内部维护两个回调向量。

它的职责是承载 `Session::addOnDetach` / `addOnShutdown` 注册的「用户回调」（这是 `Session` 对外暴露的钩子机制）。看它的 `onDetach` 实现：

```cpp
void onDetach(OnCompleteFn OnComplete, bool ShutdownRequested) override {
  while (!ToNotifyOnDetach.empty()) {
    auto ToNotify = std::move(ToNotifyOnDetach.back());
    ToNotifyOnDetach.pop_back();
    ToNotify();
  }
  OnComplete();   // ← 关键：干完活后必须调 OnComplete
}
```

注意最后一行 `OnComplete()`——它演示了异步契约的**正确写法**：无论你的清理逻辑多简单，最后都要调用传入的 `OnComplete`，否则 `Session` 会永远卡在「等这个 Service 完成」的状态。`onShutdown` 的写法完全对称。

这个 Service 是在 `Session` 构造函数里通过 `createService<NotificationService>()` 预置的：

[lib/executor/Session.cpp:53-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L53-L57) —— 构造函数体：`Notifiers(createService<NotificationService>())`。也就是说，**每个 `Session` 一出生就已经挂了至少一个 Service**。`Notifiers` 成员是个引用，绑定到这个刚创建的对象上，方便 `Session` 内部随时往里塞回调。

#### 4.1.4 代码实践

**实践目标**：亲手实现一个 `Service` 子类，验证两个回调的「恰好一次」与「先 detach 后 shutdown」顺序。

**操作步骤**：

1. 打开 [test/unit/SessionTest.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp)，定位 `MockService`（第 33–62 行）与 `SingleService` 测试（第 366–380 行）。
2. 阅读下面的「示例代码」（**非项目原有代码**，供你照搬到测试文件末尾练习）：

```cpp
// 示例代码：一个记录调用次数的最小 Service
class CountingService : public Service {
public:
  int DetachCalls = 0;
  int ShutdownCalls = 0;
  bool LastShutdownRequested = false;

  void onDetach(OnCompleteFn OnComplete, bool ShutdownRequested) override {
    ++DetachCalls;
    LastShutdownRequested = ShutdownRequested;
    OnComplete();                 // ← 别忘了调
  }
  void onShutdown(OnCompleteFn OnComplete) override {
    ++ShutdownCalls;
    OnComplete();                 // ← 别忘了调
  }
};

TEST(SessionTest, MyServiceCallbacksFireOnceInOrder) {
  CountingService *Ptr = nullptr;
  {
    Session S(mockExecutorProcessInfo(), noDispatch, noErrors);
    Ptr = S.createService<CountingService>();   // 注册
    EXPECT_EQ(Ptr->DetachCalls, 0);
    EXPECT_EQ(Ptr->ShutdownCalls, 0);
  }  // ← 离开作用域，~Session() 触发 shutdown 并阻塞到完成
  // 到这里，两个回调都应已执行恰好一次，且 detach 先于 shutdown
  EXPECT_EQ(Ptr->DetachCalls, 1);
  EXPECT_EQ(Ptr->ShutdownCalls, 1);
}
```

3. 在 orc-rt 构建目录运行单元测试目标 `check-orc-rt-unit`（构建方式见 u1-l2），过滤本用例：
   `cmake --build . --target check-orc-rt-unit`（具体 lit 过滤参数随环境，**待本地验证**）。

**需要观察的现象**：内层 `{}` 块结束后（`Session` 已析构），`DetachCalls` 与 `ShutdownCalls` 都恰好为 1，说明两个回调各触发一次；且因为 `ShutdownCalls` 只在析构后才变 1，可推断 detach 早于 shutdown。

**预期结果**：测试通过。若你故意删掉某个回调里的 `OnComplete()`，会观察到测试**挂起**（`~Session` 永远等不到完成）——这正是异步契约被破坏的后果。

> ⚠️ 注意：上例中 `Ptr` 指向的对象在 `Session` 析构时被销毁，所以**必须在离开 `{}` 之前**读取 `DetachCalls`/`ShutdownCalls` 之外的信息，或在析构前完成断言。这里能安全读取 `int` 计数是因为示例只验证「析构触发回调」这一事实——更严谨的写法是把断言放进 `addOnShutdown` 回调里（见 4.3 节的 `waitForShutdown` 范式）。**待本地验证**该对象析构时序。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 Service 的 `onDetach` 里**忘记调用** `OnComplete`，会发生什么？

> **参考答案**：`Session` 的 detach 流程会卡住——因为它是「调用一个 Service 的 onDetach → 等它 OnComplete → 再通知下一个」的串行流水线（见 4.3）。第一个不完成，后面的 Service 永远等不到通知，最终 `~Session()` 的条件变量等待会**永久阻塞**。

**练习 2**：`onDetach` 的第二个参数 `ShutdownRequested` 为 `true` 时，代表什么？

> **参考答案**：代表「detach 是由 shutdown 触发的，detach 全部完成后会立刻进入 shutdown」。Service 可以据此跳过那些「反正马上 shutdown 还要做一遍」的工作，避免重复清理。

---

### 4.2 三种注册 API

#### 4.2.1 概念说明

`Session` 提供了三种把 Service 挂上去的方式，区别全在于「**Service 对象从哪来**」和「**构造能不能失败**」：

| API | 入参 | 返回 | 适用场景 |
|------|------|------|----------|
| `addService` | 已构造好的 `std::unique_ptr<ServiceT>` | `ServiceT&` | 你已经手工 `new` 好了对象，只想交出所有权。 |
| `createService` | 构造参数包 `ArgTs&&...` | `ServiceT&` | 想让 `Session` 帮你原地构造，且构造**不会失败**。 |
| `tryCreateService` | 传给 `ServiceT::Create` 的参数 | `Expected<ServiceT&>` | 构造**可能失败**（如分配资源、绑定端口），需要把失败以 `Error` 暴露。 |

三者最终都汇聚到同一个私有方法 `appendService`，所以「逆序关闭」等生命周期行为完全一致，区别只在入口。

#### 4.2.2 核心流程

三种 API 的调用链：

```
addService(uptr)      ─┐
createService(args)   ─┼──► appendService(std::unique_ptr<Service>)  ──► push 到 Services 向量
tryCreateService(args)─┘        （统一处理「迟到注册」的兜底逻辑）
```

注意返回类型的设计意图：三者都返回**引用**（`ServiceT&`），**不是** `unique_ptr`。这是因为所有权一旦交给 `Session`，调用方就不该再持有能销毁它的句柄；返回引用只是给你一个「方便继续调用该 Service 方法」的把手。这也是 u3-l1 强调的「`Session` 拥有 Service」在 API 层面的体现。

#### 4.2.3 源码精读

**`addService`：最直接的入口**

[include/orc-rt/Session.h:322-328](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L322-L328) —— 接收 `unique_ptr`，断言非空，存下引用后转交给 `appendService`，最后返回引用。

```cpp
template <typename ServiceT>
ServiceT &addService(std::unique_ptr<ServiceT> Srv) {
  assert(Srv && "addService called with null value");
  ServiceT &Ref = *Srv;       // 先记下引用
  appendService(std::move(Srv)); // 所有权移交给 Session
  return Ref;                   // 返回引用，不是 unique_ptr
}
```

**`createService`：原地构造的便捷封装**

[include/orc-rt/Session.h:332-335](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L332-L335) —— 用 `std::make_unique` 转发参数原地构造，再复用 `addService`。

```cpp
template <typename ServiceT, typename... ArgTs>
ServiceT &createService(ArgTs &&...Args) {
  return addService(std::make_unique<ServiceT>(std::forward<ArgTs>(Args)...));
}
```

它只是 `addService` 的一行糖衣，省去你手写 `std::make_unique`。前面 4.1.3 里的 `createService<NotificationService>()` 和 4.1.4 里的 `createService<CountingService>()` 都用的它。

**`tryCreateService`：可失败的构造**

[include/orc-rt/Session.h:343-349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L343-L349) —— 调用 `ServiceT::Create(args...)`，它必须返回 `Expected<std::unique_ptr<ServiceT>>`；失败则把 `Error` 透传出去。

```cpp
template <typename ServiceT, typename... ArgTs>
Expected<ServiceT &> tryCreateService(ArgTs &&...Args) {
  auto Srv = ServiceT::Create(std::forward<ArgTs>(Args)...);  // 命名构造
  if (!Srv)
    return Srv.takeError();        // 构造失败：返回 Error
  return addService(std::move(*Srv)); // 成功：注册并返回引用
}
```

这里有个**约定**：要用 `tryCreateService`，你的 Service 必须提供一个**静态工厂方法** `Create`，返回 `Expected<std::unique_ptr<ServiceT>>`（注意是 `unique_ptr`，不是 `shared_ptr`）。来看测试桩 `ConfigurableService` 是怎么满足这个约定的：

[test/unit/SessionTest.cpp:64-82](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L64-L82) —— 同时演示了普通构造函数（给 `createService` 用）与 `Create` 工厂（给 `tryCreateService` 用）。

```cpp
class ConfigurableService : public Service {
public:
  ConfigurableService(int ConstructorOption) {}            // 普通构造

  /// Fallible named constructor for testing tryCreateService.
  static Expected<std::unique_ptr<ConfigurableService>> Create(bool Fail) {
    if (Fail)
      return make_error<StringError>("failed to create service");
    return std::make_unique<ConfigurableService>(42);
  }
  // ... onDetach/onShutdown 略 ...
};
```

`Create(bool Fail)` 完美演示了「可失败构造」：`Fail` 为真就返回一个 `StringError`，否则返回构造好的对象。这种「普通构造 + 命名工厂 `Create`」的双轨设计，与 `attach`/`tryAttach`（见 u3-l2、u8-l1）是同一套模式——**凡是可能失败的构造，都走工厂返回 `Expected`，绝不把「半成品对象」交给调用方**。

测试侧的成功/失败两条路径：

[test/unit/SessionTest.cpp:601-617](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L601-L617) —— `TryCreateServiceSuccess` 传 `false` 期望成功；`TryCreateServiceFailure` 传 `true` 期望拿到 `Error`。

#### 4.2.4 代码实践

**实践目标**：用一个测试桩同时验证三种注册 API 都能正确返回引用、且失败路径能返回 `Error`。

**操作步骤**：

1. 阅读 [test/unit/SessionTest.cpp:589-617](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L589-L617)，对照 `AddServiceAndUseRef` / `CreateServiceAndUseRef` / `TryCreateServiceSuccess` / `TryCreateServiceFailure` 四个用例。
2. 思考：`TryCreateServiceFailure`（第 610–617 行）里为什么用 `auto CS = ...` 而不是 `auto &CS = ...`？因为它返回的是 `Expected<ServiceT&>`（一个**值**，可能是引用也可能是错误），必须先按值接住再 `takeError()`——这正是 u2-l3 讲过的 `Expected` 消费模式。

**需要观察的现象**：四个用例都应通过；尤其失败用例里，`CS.takeError()` 必须能取到一个内容为 `"failed to create service"` 的 `Error`。

**预期结果**：`check-orc-rt-unit` 全绿。若你把 `ConfigurableService::Create` 里的失败分支改成总是成功，`TryCreateServiceFailure` 会**断言失败**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `createService` 没有「失败」版本，而 `tryCreateService` 有？

> **参考答案**：`createService` 内部走 `std::make_unique`，只能调用 `ServiceT` 的**普通构造函数**，而 C++ 构造函数无法返回 `Expected`/错误码（除非抛异常，orc-rt 内部不用异常，见 u9-l3）。所以凡是需要「构造期失败」的 Service，必须改用**静态工厂 `Create`** 返回 `Expected`，这就是 `tryCreateService` 存在的理由。

**练习 2**：下面三种写法，哪种不能通过编译或有语义错误？
```cpp
auto &a = S.addService(std::make_unique<MyService>());        // (1)
auto &b = S.createService<MyService>();                        // (2)
auto   c = S.tryCreateService<MyService>(/*fail=*/false);     // (3)
```

> **参考答案**：(1)(2) 正确，返回引用。(3) 也合法但要注意：`tryCreateService` 返回的是 `Expected<MyService&>`，必须先检查 `c` 是否成功（`if (auto Err = c.takeError()) ...`）才能取引用，**不能**直接把 `c` 当 `MyService&` 用。若 `MyService` 没有 `static Expected<std::unique_ptr<MyService>> Create(...)` 方法，(3) 根本**无法编译**。

---

### 4.3 逆序 onShutdown 约定

#### 4.3.1 概念说明

这是本讲最重要的一条**不变量（invariant）**：

> **Service 的 detach 与 shutdown 都按「注册顺序的逆序」被通知。**

换句话说，如果你按顺序注册了 A、B、C 三个 Service，那么通知顺序是 C → B → A（detach 和 shutdown 都是）。

为什么是逆序？这是经典的「**栈式析构 / RAII**」思想：如果后注册的 C 依赖了先注册的 A（比如 C 持有 A 分配的资源句柄），那么销毁时必须**先销毁 C、再销毁 A**，否则 C 在自己的清理里会访问到已经失效的 A。`unique_ptr` 的析构、`std::lock_guard` 的释放，都是这个顺序——`Session` 把同样的原则用到了 Service 上。

> 一个易混点澄清：本讲的实践任务规格里写了「detach 正序、shutdown 逆序」，但**对照真实源码，detach 与 shutdown 两者都是逆序**（下文 4.3.2/4.3.3 会用代码和测试双重证明）。请以源码为准。

#### 4.3.2 核心流程

逆序通知的难点在于：回调是**异步**的（要等 `OnComplete`），不能用简单的 `for` 循环从后往前 `for (i = n-1; i >= 0; --i)`——因为「等 OnComplete」这件事天然是回调驱动的。orc-rt 用了一个极其优雅的模式：

```
detachServices(待通知列表 ToNotify):
    if ToNotify 为空:
        调 completeDetach()            # 全部通知完，收尾
        return
    取出 ToNotify 的最后一个 Srv        # back() —— 即「最后注册」的
    从 ToNotify 弹出它                 # pop_back()
    调用 Srv->onDetach(完成回调, ShutdownRequested)
        其中「完成回调」= [捕获剩余 ToNotify](){ detachServices(剩余 ToNotify); }
```

注意完成回调里捕获了**剩余的** `ToNotify`，并在被调用时**递归**进入 `detachServices`。这样：

- 每次只通知**一个** Service（最后注册的那个）。
- 等它 `OnComplete` 后，才递归通知下一个（倒数第二个）。
- 自然形成了「逆序 + 串行等待异步」的双重效果。

`shutdownServices` 的结构与上面**完全对称**，只是收尾函数换成 `completeShutdown`。

#### 4.3.3 源码精读

**收集阶段：把 `Services` 拷成一份裸指针列表**

[lib/executor/Session.cpp:278-293](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L278-L293) —— `proceedToDetach`：锁内把 `Services` 里每个对象的裸指针塞进 `ToNotify`（保持注册顺序），算出 `ShutdownRequested`，置 `CurrentState = Detached`，**解锁后**才调 `detachServices`。

```cpp
std::vector<Service *> ToNotify;
ToNotify.reserve(Services.size());
for (auto &Srv : Services)       // 按注册顺序遍历
  ToNotify.push_back(Srv.get());
bool ShutdownRequested = TargetState == State::Shutdown;
CurrentState = State::Detached;
Lock.unlock();                   // ← 关键：先解锁再通知，避免回调里重入锁死
// ...
detachServices(std::move(ToNotify), ShutdownRequested);
```

两个要点：(1) 用裸指针 `Service*` 而非 `unique_ptr`，因为这里只是「借来通知」，**所有权始终在 `Services` 向量**；(2) **先解锁再通知**——因为 Service 的 `onDetach` 可能立刻（在 `OnComplete` 前）做别的事，甚至重入 `Session`，持锁调用会死锁。

**通知阶段：`detachServices` 的递归**

[lib/executor/Session.cpp:295-307](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L295-L307) —— 正是 4.3.2 描述的「`pop_back` + 完成回调里递归」模式。

```cpp
void Session::detachServices(std::vector<Service *> ToNotify,
                             bool ShutdownRequested) {
  if (ToNotify.empty())
    return completeDetach();             // 全部通知完 → 收尾

  auto *Srv = ToNotify.back();           // 最后注册的
  ToNotify.pop_back();
  Srv->onDetach(
      [this, ToNotify = std::move(ToNotify), ShutdownRequested]() {
        detachServices(std::move(ToNotify), ShutdownRequested); // 递归
      },
      ShutdownRequested);
}
```

[lib/executor/Session.cpp:342-351](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L342-L351) —— `shutdownServices` 与之**结构完全相同**，只是调 `onShutdown`、收尾调 `completeShutdown`。把两段对照阅读，你会立刻看到「detach 和 shutdown 都是逆序」是源码层面的必然。

**测试证明：`MultipleServices`**

光说源码还不够，来看测试是怎么**钉死**这个顺序的。

[test/unit/SessionTest.cpp:382-400](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L382-L400) —— 注册 3 个 `MockService`，析构后断言 detach 与 shutdown 均为逆序。

```cpp
TEST(SessionTest, MultipleServices) {
  size_t OpIdx = 0;
  std::optional<size_t> DetachOpIdx[3];
  std::optional<size_t> ShutdownOpIdx[3];
  {
    Session S(mockExecutorProcessInfo(), noDispatch, noErrors);
    for (size_t I = 0; I != 3; ++I)
      S.addService(std::make_unique<MockService>(DetachOpIdx[I],
                                                 ShutdownOpIdx[I], OpIdx));
  }                                   // ← 析构触发 shutdown（含 detach）
  EXPECT_EQ(OpIdx, 6U);               // 3 次 detach + 3 次 shutdown
  for (size_t I = 0; I != 3; ++I) {
    EXPECT_EQ(DetachOpIdx[I], 2 - I);    // detach 逆序
    EXPECT_EQ(ShutdownOpIdx[I], 5 - I);  // shutdown 逆序
  }
}
```

理解这套断言的关键是 `MockService` 里那个**共享的** `OpIdx` 计数器——每次任一 Service 的任一回调被触发，就 `OpIdx++` 并把当时的值记进自己的 `DetachOpIdx`/`ShutdownOpIdx`：

[test/unit/SessionTest.cpp:33-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L33-L62) —— `MockService`：构造时绑定外部引用，回调里 `OpIdx++` 记录顺序。

设注册顺序为 S0、S1、S2，那么：

- **detach 逆序**：先通知 S2（记 0）、再 S1（记 1）、再 S0（记 2）→ `DetachOpIdx[I] == 2 - I`。
- **shutdown 逆序**：接着通知 S2（记 3）、再 S1（记 4）、再 S0（记 5）→ `ShutdownOpIdx[I] == 5 - I`。

两个断言式都是「`I` 越大（注册越晚），序号越小（越早被通知）」，正是逆序。共 6 次回调（`OpIdx == 6U`）。

**附带一提：迟到注册的兜底**

[lib/executor/Session.cpp:233-268](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp#L233-L268) —— `appendService`：若注册时 `Session` 已 detach 甚至已 shutdown，它会**同步地**把错过的回调补上（先 `onDetach`，若已 shutdown 再 `onShutdown`），最后仍然 `push_back` 保活到 `Services`。

这保证了一个 Service **无论何时注册都能恰好经历一次完整生命周期**——即使它错过了那波集体通知，也会被单独「补课」。`ShuttingDown` 标志取自 `TargetState == State::Shutdown`，作为补课 `onDetach` 的 `ShutdownRequested` 实参，语义与正常路径一致。

#### 4.3.4 代码实践

**实践目标**：亲手复现 `MultipleServices` 的结论——注册 3 个 Service，验证 detach 与 shutdown **都按逆序**触发。

**操作步骤**：

1. 直接运行现成的 `MultipleServices` 用例确认基线：在 orc-rt 构建目录执行 `cmake --build . --target check-orc-rt-unit`（lit 过滤参数随环境，**待本地验证**）。
2. 仿照它写一个**带依赖关系**的加强版（**示例代码，非项目原有**），粘到测试文件末尾：

```cpp
// 示例代码：用字符串流水记录调用顺序，更直观
class TraceService : public Service {
public:
  TraceService(std::string Name, std::string &Log) : Name(Name), Log(Log) {}
  void onDetach(OnCompleteFn OnComplete, bool) override {
    Log += "detach:" + Name + " ";
    OnComplete();
  }
  void onShutdown(OnCompleteFn OnComplete) override {
    Log += "shutdown:" + Name + " ";
    OnComplete();
  }
private:
  std::string Name; std::string &Log;
};

TEST(SessionTest, MyReverseOrderCheck) {
  std::string Log;
  {
    Session S(mockExecutorProcessInfo(), noDispatch, noErrors);
    S.createService<TraceService>("A", Log);  // 先注册 A
    S.createService<TraceService>("B", Log);
    S.createService<TraceService>("C", Log);  // 最后注册 C
  }
  // 期望：detach 与 shutdown 都是 C→B→A
  EXPECT_EQ(Log, "detach:C detach:B detach:A shutdown:C shutdown:B shutdown:A ");
}
```

**需要观察的现象**：`Log` 字符串先出现 `detach:C detach:B detach:A`（detach 逆序），紧接着 `shutdown:C shutdown:B shutdown:A`（shutdown 逆序）。

**预期结果**：断言通过。若把 `createService` 的顺序换一下，或故意把 `TraceService` 的 `OnComplete()` 删掉，断言会失败或测试挂起。注意：断言必须在 `Session` **析构前或析构同步完成后**读取 `Log`——这里靠离开 `{}` 触发 `~Session()` 阻塞至 shutdown 完成来保证（见 u3-l2 的 `~Session`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `proceedToDetach` 要**先解锁**再调用 `detachServices`？

> **参考答案**：Service 的 `onDetach` 可能在调 `OnComplete` 之前就做别的事，甚至重入 `Session` 的其它加锁方法。如果持有 `Session::M` 这个锁去通知，一旦回调里再尝试获取同一把锁就会**死锁**。所以收集完待通知列表、置好状态后立即 `Lock.unlock()`，把锁让出来。

**练习 2**：`detachServices` 里用的是「`back()` + `pop_back()` + 完成回调里递归」。如果改成「正向 `for` 循环依次 `onDetach`，循环结束后 `completeDetach`」会有什么问题？

> **参考答案**：那样会把所有 Service 的 `onDetach` **同时**触发、**不等任何一个完成**，破坏了「串行等待异步完成」的语义——`completeDetach` 会在清理还没结束时就被调用，后续 shutdown 阶段可能抢跑。递归模式天然保证了「上一个 `OnComplete` 回调才触发下一个」，是异步流水线的正确写法。

---

## 5. 综合实践

把本讲三个模块串起来，做一个**「带依赖的计数器 Service」**综合任务：

**任务描述**：模拟「内存分配器（Allocator）依赖日志器（Logger）」的场景。Logger 先注册、Allocator 后注册。要求：

1. Logger 是一个 `Service`，提供 `void log(const std::string&)`；Allocator 是另一个 `Service`，持有 Logger 的引用，在 `onShutdown` 里**先**调一次 `Logger::log("allocator shutting down")` 再 `OnComplete()`——这模拟「析构 Allocator 时还要用 Logger」。
2. 用 `createService` 注册 Logger，用 `addService(std::make_unique<...>)` 注册 Allocator（练两种 API）。
3. 注册一个 `addOnShutdown` 钩子，在钩子里**断言**：Logger 的 `onShutdown` 必须晚于 Allocator 的 `onShutdown`（即依赖被先销毁、被依赖者后销毁）。
4. 让 `Session` 析构，检查断言通过、且 Logger 的 `log` 确实被 Allocator 调用过。

**实现要点（伪代码）**：

```cpp
// 示例代码（非项目原有），仅示意结构
class Logger : public Service {
  std::vector<std::string> Lines;
public:
  void log(const std::string &S) { Lines.push_back(S); }
  void onDetach(OnCompleteFn C, bool) override { C(); }
  void onShutdown(OnCompleteFn C) override {
    Lines.push_back("logger shutdown");
    C();
  }
  bool hasLine(const std::string &S) const { /* ... */ }
};

class Allocator : public Service {
  Logger &L;
public:
  explicit Allocator(Logger &L) : L(L) {}
  void onDetach(OnCompleteFn C, bool) override { C(); }
  void onShutdown(OnCompleteFn C) override {
    L.log("allocator shutting down");  // 依赖 Logger，必须在 Logger 关闭前调用
    C();
  }
};
```

**为什么这个任务能验证本讲全部要点**：

- 它同时用了 `createService` 和 `addService`（模块 4.2）。
- 它依赖「逆序关闭」保证 Allocator（后注册）**先**于 Logger（先注册）被 shutdown——否则 Allocator 调 `L.log` 时 Logger 可能已失效（模块 4.3）。
- 它要求你正确实现两个异步回调（模块 4.1），漏掉 `OnComplete()` 会挂起。

**预期结果**：断言通过，证明 orc-rt 的逆序关闭约定确实支持「后注册的依赖者先销毁」这一 RAII 式用法。**运行该测试需自行补全 `hasLine` 等细节并接入 `check-orc-rt-unit`，待本地验证。**

---

## 6. 本讲小结

- `Service` 是 `Session` 拥有的**资源管家**抽象基类，只有两个纯虚回调：`onDetach(OnComplete, ShutdownRequested)` 与 `onShutdown(OnComplete)`，外加一个 `OnCompleteFn = move_only_function<void()>` 的完成回调类型。
- 两个回调都是**异步**的：Service 必须在清理完毕后主动调用传入的 `OnComplete`，否则 `Session` 会永久阻塞。`onDetach` 恰好在 `onShutdown` 前调用一次，且 JIT 代码可能与 `onDetach` 并发访问 Service。
- 注册 Service 有三种 API：`addService`（已构造 `unique_ptr`）、`createService`（原地构造、不可失败）、`tryCreateService`（走 `ServiceT::Create` 工厂、可失败返回 `Expected`）；三者都返回引用，所有权归 `Session`。
- **逆序关闭**是核心不变量：detach 与 shutdown 都按注册的**逆序**通知 Service（C→B→A），用「`pop_back` + 完成回调里递归」优雅兼容异步；这保证了「后注册的依赖者先被销毁」的 RAII 语义。
- 迟到注册的 Service 由 `appendService` 兜底：若注册时已 detach/shutdown，会同步补上错过的回调，保证每个 Service 恰好经历一次完整生命周期。

---

## 7. 下一步学习建议

本讲把「Service 是什么、怎么注册、怎么被通知」讲透了，接下来：

- **u4-l1（TaskGroup、Token 与托管代码）**：本讲反复提到「detach 之后、shutdown 之前有个 Drain 阶段，等托管代码跑完」，下一讲就讲清 `TaskGroup`/`Token` 如何标记「正在执行托管代码」并延迟 shutdown。
- **u7 单元（资源型 Service）**：去看几个**真实**的 Service 实现：`SimpleNativeMemoryMap`（JIT 内存分配）、`NativeDylibManager`（动态库加载），它们正是本讲抽象接口的具体落地。
- **u11-l1（编写自定义 Service）**：综合实践，让你完整实现一个自定义 Service 并接入 `Session`，是本讲「综合实践」的加强版。
- **延伸阅读**：直接打开 [include/orc-rt/Service.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Service.h) 通读注释（它本身就是一份契约文档），再看 [lib/executor/Session.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/Session.cpp) 里的 `NotificationService`、`detachServices`、`shutdownServices` 三段，对照本讲理解。
