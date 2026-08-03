# Wrapper Function 签名与 call/handle

## 1. 本讲目标

上一讲（u5-l1）我们搞清楚了跨进程字节装在什么容器里——`orc_rt_WrapperFunctionBuffer`，以及它如何用「带外错误」来表达失败。但容器只是「货车」，还有两个问题没回答：

1. 货车的**装卸口**长什么样？两端约定一个怎样的函数签名，才能让任何跨进程调用都走同一条通道？
2. 字节本身没有类型。执行端拿到一串 `ArgBytes` 后，怎么把它**还原成有类型的参数**、跑完业务、再把结果**打包回字节**？

本讲就来回答这两个问题，把 wrapper function 从「容器」推进到「完整的调用流水线」。

读完本讲，你应该能够：

1. 写出 `orc_rt_WrapperFunction` 这个统一 C 签名的四个参数（`Session / ArgBytes / Return / CallId`），并解释它为什么是**异步**的、`CallId` 在来回中扮演什么角色。
2. 读懂 `WrapperFunction::handle`（执行端）如何用 `Serializer` 完成「反序列化 → 调用业务 handler → 序列化结果 → `Return`」，并指出其中**三处带外错误短路**。
3. 说清 `WrapperFunction::call`（调用端）为何是 `handle` 的镜像，以及 `Caller` 这个概念如何把「真正的跨进程发送」与「序列化/反序列化」解耦。
4. 会用 `AsyncMethod` / `SyncMethod` 适配器，把一个普通的成员函数指针直接喂给 `handle`，免去手写 wrapper 样板。

---

## 2. 前置知识

本讲承接 u5-l1 的两个结论，这里只做最短回顾：

- **wrapper function「字节进、字节出」**：两端约定一个统一签名，通信层只搬字节、不解释含义；类型的还原留给序列化层。
- **带外错误（out-of-band error）**：用 `Size == 0 && ValuePtr != 0` 这个「本不可能」的缓冲状态来表达失败，调用方第一步就能在不解析结果的前提下判错。

另外补充三个本讲会用到的支撑概念（它们各自有专门讲义，这里只需知道用途）：

- **`Error` / `Expected<T>`**（u2-l3）：orc-rt 内部的错误类型。本讲里 `call` 的结果回调会收到 `Error`（表示「调用本身成败」）或 `Expected<T>`（表示「调用成功，但业务可能返回值或错误」）。
- **`move_only_function`**：仅移动的可调用对象包装（u10-l3 会详讲）。本讲里 handler 的「返回回调」就是它，因为回调常捕获只能移动的资源（如序列化器）。
- **`bind_front`**：把若干参数**预先绑定**到可调用对象的前面，剩余参数留到真正调用时再补（见 [`include/orc-rt/bind.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/bind.h)）。`handle` 用它把「返回回调」预先钉在业务 handler 前面。

> 提示：本讲刻意把 `Serializer`（序列化器）当成一个**抽象概念**来讲——它只需要提供 `.arguments()` 与 `.result()` 两个子对象。具体唯一在树内的实现是 SPS（Simple Packed Serialization），那是 u6 的主题。本讲关注的是「**不管用什么序列化方案，handle/call 如何编排整条流水线**」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`include/orc-rt-c/WrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h) | **C ABI 层**：定义统一签名 `orc_rt_WrapperFunction` 与返回回调 `orc_rt_WrapperFunctionReturn`。本讲 4.1 的主角。 |
| [`include/orc-rt/WrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h) | **C++ 层**：`WrapperFunction` 结构体提供 `call` / `handle` / `AsyncMethod` / `SyncMethod` / `handleWithAsyncMethod` / `handleWithSyncMethod`。本讲 4.2、4.3 的主角。 |
| [`docs/Design.md`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md) | **设计文档**：解释 controller↔executor 调用的不对称性（按地址 vs 按 tag）与 wrapper function 的设计意图。 |
| [`include/orc-rt/CallableTraitsHelper.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/CallableTraitsHelper.h) | （支撑）编译期提取可调用对象的返回/参数类型，`handle` 用它推导 handler 的参数元组。 |
| [`test/unit/SPSWrapperFunctionTest.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp) | （验证）用 SPS 作具体 Serializer 演示 `call`/`handle` 往返，含 `AsyncMethod`/`SyncMethod` 真实用例。 |
| [`test/unit/DirectCaller.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h) | （验证）测试用的 `Caller` 实现：把「跨进程发送」简化为「同线程直接调用」，是理解 `Caller` 概念的最佳样本。 |

---

## 4. 核心概念与源码讲解

### 4.1 统一签名与回调顺序约定

#### 4.1.1 概念说明

跨进程通信最大的敌人是「多样」。如果每个远程函数都有自己的签名，控制端就必须为每一个都生成专门的调用桩。orc-rt 的选择是**收敛到一个统一签名**：不管你要调用的远程能力是「分配内存」「加载动态库」还是「运行一段 main」，在通信层看来都是**同一个函数**——它接收一段字节（参数），返回一段字节（结果）。

这个统一签名就是 `orc_rt_WrapperFunction`。它有四个参数，各司其职：

- **`Session`**：会话引用，让 handler 知道自己服务于哪个 `Session`（一个进程可有多个）。
- **`ArgBytes`**：序列化后的参数字节（装在 u5-l1 讲的缓冲里）。
- **`Return`**：**返回回调**——一个函数指针，handler 干完活后用它把结果字节送回去。
- **`CallId`**：一次具体调用的**不透明上下文**（一个 `uint64_t`），由调用方提供，handler 必须原样回传给 `Return`，用来把结果与那次调用配对。

最关键的设计是：这个函数**返回 `void`**。也就是说，它一返回并不代表「调用完成」——完成只能通过调用 `Return` 来通知。这是**异步**签名：

```
调用方 ──(S, ArgBytes, Return, CallId)──► wrapper 函数（立即返回 void）
                                              │ （可能稍后才完成）
                                              ▼
                                        Return(S, ResultBytes, CallId) ──► 调用方
```

为什么要异步？因为执行端收到调用后，往往要把它当成一个 **Task** 丢进任务分发器（u4-l2）稍后执行，甚至要等 I/O。同步签名会逼着通信层阻塞，而异步签名让「接收」与「完成」彻底解耦——handler 只需在真正完成时调用 `Return` 即可。

#### 4.1.2 核心流程

把一次跨进程调用拆成「约定」层面的事实，只有四条：

1. **统一入口**：所有跨端调用都长成 `void(Session, ArgBytes, Return, CallId)`。
2. **异步完成**：函数返回 `void`；成功/失败/结果**只能**通过 `Return` 报告。
3. **CallId 回环**：调用方生成 `CallId`，随调用送出；handler **不解读**它，只在 `Return` 时原样塞回。调用方据此把结果关联回那次调用。
4. **方向不对称**（见 Design.md）：controller→executor 按**地址**调用 wrapper（可触达任意代码）；executor→controller 按 **tag** 调用（只能命中显式登记的入口，体现最小权限）。但**两端的签名完全相同**——不对称的只是「怎么找到那个函数」，不是「函数长什么样」。

#### 4.1.3 源码精读

先看返回回调 `orc_rt_WrapperFunctionReturn`——注意它的参数顺序是 `(S, ResultBytes, CallId)`：

[include/orc-rt-c/WrapperFunction.h:54-59](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L54-L59) —— `Return` 接收会话引用、结果字节缓冲、以及那个 `CallId`。注释明确称它为 "Asynchronous return function"。

再看统一签名本体 `orc_rt_WrapperFunction`：

[include/orc-rt-c/WrapperFunction.h:61-72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L61-L72) —— 注释逐个解释了四个参数：`Session` 持有会话引用、`ArgBytes` 装序列化参数、`Return` 指向返回函数、`CallId` 是本次调用的上下文。

> **一个容易踩坑的细节**：[docs/Design.md:105-111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L105-L111) 给出的签名是 `void(Session, CallId, Return, ArgBytes)`，与头文件**不一致**——文档保留的是旧版参数顺序，**以头文件为准**（实际是 `Session, ArgBytes, Return, CallId`）。同理文档里 `Return` 写成 `(Session, CallId, ResultBytes)`，而头文件是 `(Session, ResultBytes, CallId)`。这是文档落后于代码的典型例子，读源码时务必对齐头文件。

方向不对称（最小权限）的设计意图，在 Design.md 的 ControllerAccess 小节讲得最清楚：

[docs/Design.md:38-47](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L38-L47) —— controller→executor 指定 wrapper 的**地址**（可调用执行端任意代码）；executor→controller 指定 **tag**（执行端地址关联到 controller 中登记的 handler）。这保证执行端只能调用 controller **故意暴露**的入口。注意：这种不对称只影响「如何定位函数」，两端函数签名仍然相同。

`CallId` 是「不透明上下文」最生动的例子在测试工具 `DirectCaller` 里。它把一个堆上对象的指针**编码进 `CallId`**，再在 `Return` 里解码回来：

[test/unit/DirectCaller.h:58-65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h#L58-L65) —— `DirectCaller` 把「结果处理器」包成一个堆对象，把它的指针转成 `uint64_t` 当作 `CallId` 传给 wrapper 函数。

[test/unit/DirectCaller.h:25-32](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h#L25-L32) —— 静态 `send`（即 `Return`）拿到 `CallId`，把它**还原成指针**，从而找到当初那个结果处理器并投递结果。这就是 `CallId` 的全部含义：**一个由调用方定义、handler 原样回传、用来配对调用与结果的令牌**。

#### 4.1.4 代码实践

**实践目标**：通过 `DirectCaller` 亲眼看清 `CallId` 的「来回回环」机制。

**操作步骤**：

1. 打开 [test/unit/DirectCaller.h:18-70](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h#L18-L70)，对照阅读 `DirectCaller::operator()`（构造并发送调用）与 `DirectResultSender::send`（接收结果）。
2. 在纸上画出一次调用的 `CallId` 旅行：
   - 谁创建了那个堆上的 `DirectResultSender`？
   - 它的指针如何变成 `CallId`？
   - wrapper 函数（`Fn`）收到 `CallId` 后**有没有**解读它？
   - `Return`（即静态 `send`）如何把 `CallId` 还原回那个对象？

**需要观察的现象 / 预期结果**：你会发现 wrapper 函数本身对 `CallId` **完全不关心**——它只是把 `CallId` 透传给自己的 `Return` 回调。真正「读懂」`CallId` 的是调用方提供的 `Return` 实现。这正是「不透明上下文」的含义：通信契约只要求「原样回传」，不规定它的内容。

> 待本地验证：若你想看真实运行，可构建并运行 `check-orc-rt-unit`（见 u1-l2），其中 `SPSWrapperFunctionUtilsTest` 系列正是用 `DirectCaller` 驱动的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `orc_rt_WrapperFunction` 返回 `void`，而不是返回 `orc_rt_WrapperFunctionBuffer`（结果字节）？

> **答案**：因为它是**异步**签名。handler 可能把工作当成 Task 丢进分发器稍后才做（u4-l2），甚至要等异步 I/O。若要求它同步返回结果，通信层就得阻塞，与 orc-rt 的任务模型冲突。用 `void` + `Return` 回调，把「接收调用」与「完成调用」在时间上彻底分开。

**练习 2**：`CallId` 是 `uint64_t`。在 `DirectCaller` 里它装的是一个指针；换成真实跨进程场景，它会装什么？

> **答案**：跨进程时 `CallId` 通常是调用方自己维护的一个**调用编号/句柄**（比如一个递增序号或槽位索引），用来在结果回来时查到「这次调用对应的等待者与结果回调」。它的内容完全由调用方约定，handler 与通信层都不解读它——这正是「不透明」的价值。

---

### 4.2 handle 的反序列化-执行-序列化（与 call 的镜像）

#### 4.2.1 概念说明

统一签名解决了「通道」问题，但留下一个麻烦：handler 拿到的是一串无类型的 `ArgBytes`，业务逻辑却需要的是 `int32_t`、`std::string` 这样的具体参数。如果每个 wrapper 都要手写「拆字节 → 取参数 → 调业务 → 拼字节 → 回传」，样板代码会爆炸。

`WrapperFunction::handle` 就是来消灭这套样板的。它位于**执行端**，把「字节 ↔ 类型」的转换抽出来交给一个 `Serializer`，自己只负责编排：

```
ArgBytes ──(Serializer.arguments().deserialize)──► 类型化参数
                                                        │
                                                        ▼
                                                  业务 handler(args...)
                                                        │ 调用「返回回调」yield(R)
                                                        ▼
                                    Serializer.result().serialize(R) ──► ResultBytes ──► Return
```

关键设计：handler **不直接调用 `Return`**，而是调用一个叫 **`yield`** 的回调。`handle` 在背后把 `yield` 实现成「序列化结果 → 调 `Return`」。这样一来：

- handler 只关心业务和「我要返回什么值」，不必关心字节怎么拼、`Return` 怎么调。
- 序列化失败的错误处理（结果太大装不下等）由 `handle` 统一兜底，自动转成带外错误。

对称地，调用端有 `WrapperFunction::call`，它是 `handle` 的**镜像**：把参数序列化成 `ArgBytes`，通过一个 `Caller` 发出去，等结果回来再反序列化成值。两者共用同一个 `Serializer` 类型，一正一反构成闭环。

#### 4.2.2 核心流程

`handle` 的完整时序如下（★ 标出三处**带外错误短路**）：

```
【执行端 handle】
 1. 收到 ArgBytes
 2. ★ if ArgBytes 自身是带外错误 ──► Return(S, 原样ArgBytes, CallId)   // 短路①：透传上游错误
 3.   Args = Serializer.arguments().deserialize<ArgTuple>(ArgBytes)
 4. ★ if 反序列化失败 ──► Return(S, OOB("Could not deserialize..."), CallId)  // 短路②
 5.   构造 yield = StructuredYield(S, Return, CallId, Z)
 6.   用 bind_front 把 yield 绑到业务 handler 前面，再用 Args 调用它
 7.   业务 handler 执行，结束时调用 yield(R)
 8.     yield 内部：ResultBytes = Serializer.result().serialize(R)
 9. ★   if 序列化失败 ──► Return(S, OOB("Could not serialize result..."), CallId) // 短路③
10.     否则 ──► Return(S, ResultBytes, CallId)
```

三处短路本质都是同一个思想（u5-l1 已建立）：**一旦在字节层判定为失败，就不再触碰业务/类型逻辑，直接回带外错误**。注意短路①很特殊——它把 `ArgBytes` **原样**回传，等于把上游的错误透传给调用方。

调用端 `call` 是镜像，时序对称：

```
【调用端 call】
 1. ArgBytes = Serializer.arguments().serialize(Args...)   // 失败 → 直接 RH(make_error)，根本不发
 2. Caller( 结果回调, ArgBytes )                            // Caller 负责真正发出去并等结果
 3.   结果回调收到 ResultBytes：
 4. ★ if ResultBytes 是带外错误 ──► RH(make_error<StringError>(ErrMsg))   // 短路
 5.     否则 ──► RH(ResultDeserializer::deserialize(ResultBytes, Z))       // 还原成值
```

两个要点：其一，`call` **不自己发字节**——它把发送这件事委托给 `Caller`（测试里是 `DirectCaller`，生产里是 `ControllerAccess`），于是「序列化」与「传输」被干净解耦；其二，`call` 在边界处用 `make_error<StringError>` 把通信层的带外错误字符串**提升**成 orc-rt 内部的 `Error` 类型（衔接 u2-l3）。

#### 4.2.3 源码精读

先看执行端 `handle` 的本体——对照上面时序的每一步：

[include/orc-rt/WrapperFunction.h:372-398](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L372-L398) —— 这是本模块最核心的一段，务必逐行读。其中：
- 第 383-384 行是**短路①**（`ArgBytes` 自带带外错误则原样 `Return`）；
- 第 386 行用 `Serializer.arguments().deserialize<ArgTuple>` 还原参数；
- 第 392-397 行是**短路②**（反序列化失败则回带外错误 `"Could not deserialize wrapper function arg data"`）；
- 第 387-391 行把 `yield` 与 handler 绑在一起调用业务。

`handle` 怎么知道 handler 的参数类型？靠编译期反射。`WFHandlerTraits` 用 `CallableTraitsHelper` 从 handler 的 `operator()` 签名里抽出参数元组：

[include/orc-rt/WrapperFunction.h:114-142](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L114-L142) —— `WFHandlerTraitsImpl` 把 handler 签名 `void(Return, ArgTs...)` 拆成 `YieldType`（返回回调的类型）与 `ArgTupleType`（业务参数元组）；`forwardArgsAsRequested` 则把反序列化出来的值按 handler **声明的引用类别**（值/引用/const引用/右值引用）转发出去。

> 支撑件 [`CallableTraitsHelper.h:27-34`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/CallableTraitsHelper.h#L27-L34) 是怎么「看穿」一个 lambda 的：它递归地取 `decltype(&T::operator())`，把 lambda 当成「带 `operator()` 的类」，从而套用成员函数指针的特化。这是 orc-rt 自带的轻量反射，不依赖任何编译器扩展。

`yield` 的两个特化分别处理「有返回值」与「无返回值」的 handler：

[include/orc-rt/WrapperFunction.h:159-174](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L159-L174) —— `StructuredYield<tuple<RetT>>`：handler 调 `yield(R)` 时，先 `Serializer.result().serialize(R)`，成功就 `Return` 结果字节；失败就 `Return` 带外错误 `"Could not serialize wrapper function result data"`（**短路③**）。

[include/orc-rt/WrapperFunction.h:176-184](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L176-L184) —— `StructuredYield<tuple<>>`：无返回值的 handler 调 `yield()` 时，回一个 **empty** 缓冲（`Size==0 && ValuePtr==0`，即「成功但无数据」，区别于带外错误）。

再看调用端 `call`——注意它如何把发送委托给 `Caller`：

[include/orc-rt/WrapperFunction.h:340-366](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L340-L366) —— 先 `Serializer.arguments().serialize(Args...)` 得到 `ArgBytes`（失败直接 `RH(make_error)`，连发都不发）；然后调用 `C(结果回调lambda, ArgBytes)`。结果回调里（第 356-361 行）是**短路④**：若是带外错误，用 `make_error<StringError>(ErrMsg)` 提升成内部 `Error` 交给 `RH`；否则交给 `ResultDeserializer` 还原成值。

`ResultDeserializer` 决定结果字节怎么变回 C++ 值，有两个特化：

[include/orc-rt/WrapperFunction.h:186-207](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L186-L207) —— 期望 `Expected<T>` 时，把字节反序列化成 `tuple<T>` 再包成 `Expected<T>`（失败则 `make_error<StringError>`）；期望 `Error` 时，断言结果缓冲为空（`Error::success()` 的 wire 表示就是「啥都没有」）。这两个特化正是 `call` 结果回调里 `Expected<int32_t>` / `Error` 的来源——与 `handle` 的 `yield` 严格对称。

最后看真实往返的测试样本。`add_via_lambda_sps_wrapper` 是执行端 handler，`BinaryOpViaLambda` 是调用端：

[test/unit/SPSWrapperFunctionTest.cpp:55-64](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L55-L64) —— 执行端：用 SPS 作 Serializer，`handle` 反序列化出 `X, Y`，业务是 `Return(X + Y)`（这里的 `Return` 正是 `yield`）。

[test/unit/SPSWrapperFunctionTest.cpp:66-72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L66-L72) —— 调用端：`call` 经 `DirectCaller` 发出 `41, 1`，结果回调收到 `Expected<int32_t>`，断言为 `42`。这里 `SPSWrapperFunction<...>::call/handle`（u6）是 `WrapperFunction::call/handle` 的类型安全外层，但底层编排完全就是本讲讲的这套。

#### 4.2.4 代码实践

**实践目标**（本讲指定的核心实践）：阅读 `WrapperFunction::handle` 实现，画出「自定义 Serializer 下」的完整调用时序，并标出带外错误的短路路径。

**操作步骤**：

1. 打开 [include/orc-rt/WrapperFunction.h:372-398](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L372-L398) 与 [159-184](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L159-L184)。
2. 在纸上画出如下时序图，每一步标注对应的源码行号与所用 `Serializer` 子对象（`.arguments()` 还是 `.result()`）：

   ```
   ArgBytes 到达
     │ ① 查 getOutOfBandError()        → 行 383-384
     │ ② Z.arguments().deserialize     → 行 386
     │ ③ bind_front(H, yield) + apply  → 行 387-391
     │ ④ H 执行，调 yield(R)            → StructuredYield operator() 行 164-173
     │ ⑤ Z.result().serialize(R)       → 行 165
     │ ⑥ Return(S, ResultBytes, CallId) → 行 166
   ```

3. 在图上用红色标出**三处短路**：
   - 短路①：`ArgBytes` 自带带外错误 → 行 383-384（原样回传）。
   - 短路②：反序列化参数失败 → 行 392-397（回 `"Could not deserialize wrapper function arg data"`）。
   - 短路③：序列化结果失败 → 行 168-172（回 `"Could not serialize wrapper function result data"`）。

**需要观察的现象 / 预期结果**：你应该能清楚地说出——三处短路的共同点是「**一旦判定失败，绝不再进入业务 handler 或类型还原**」，且前两处在进入 handler 之前，第三处在 handler 已返回之后。这与 u5-l1「先查带外错误再取数据」的硬契约一脉相承。

> 待本地验证：想观察短路②的真实触发，可构造一个「参数字节格式故意写错」的 `ArgBytes` 喂给某个 `handle`（例如给期望 `int32_t` 的 handler 喂 0 字节），预期它会回带外错误而非崩溃。

#### 4.2.5 小练习与答案

**练习 1**：`handle` 为什么不让业务 handler 直接调用 `Return`，而要绕一层 `yield`？

> **答案**：因为「把结果变成字节」是序列化层的事，不该让业务关心。`yield` 把 `serialize → Return` 封装起来，让 handler 只写 `yield(X + Y)`；同时序列化失败（短路③）也能由 `yield` 统一兜底成带外错误，业务代码里看不到任何错误处理样板。

**练习 2**：短路①（`ArgBytes` 自带带外错误时**原样回传**）有什么现实意义？谁会送来一个带错误的 `ArgBytes`？

> **答案**：这对应「**链式调用**」场景——某个 wrapper 内部又去调用了另一个 wrapper，若那个内部调用失败（结果是带外错误），当前 wrapper 可以把它原封不动地透传回自己的调用方，而不必懂得如何解析它。原样回传保证了错误信息在多层转发中不失真。

**练习 3**：`call` 里（[第 352-365 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L352-L365)）有两处 `make_error<StringError>`，它们分别在什么时机触发？

> **答案**：第一处在第 363-365 行——**序列化参数**就失败了（连调用都没发出去），回 `"Could not serialize wrapper function call arguments"`；第二处在第 356-357 行——调用已发出、但**返回的是带外错误**，把错误字符串提升成 `StringError`。前者是本地序列化失败，后者是对端报告的失败。

---

### 4.3 AsyncMethod / SyncMethod 适配器

#### 4.3.1 概念说明

执行端的大量 wrapper 其实是在「调用某个对象的方法」。例如「分配内存」是调用 `SimpleNativeMemoryMap` 对象的 `initialize` 方法，「查找符号」是调用 `NativeDylibManager` 对象的 `lookup` 方法。这些方法的签名千差万别，但都遵循一个共同模式：

1. 从参数里取出**对象地址**（一个 `ExecutorAddr`），还原成 `ClassT*`；
2. 调用它的某个成员方法，传入剩余参数；
3. 把返回值交回。

`AsyncMethod` 与 `SyncMethod` 就是来把这三步**模板化**的。给定一个成员函数指针，它们产出一个可直接喂给 `handle` 的可调用对象，于是你不必手写 wrapper，只要写一行：

```cpp
WrapperFunction::handleWithAsyncMethod(&MyClass::myMethod)
```

两者的区别在于被包装方法**如何返回结果**：

- **AsyncMethod**：被包装方法是异步的——它返回 `void`，并**自己**接收一个「返回回调」作为第一个参数，干完活后调用它。适配器只负责把地址还原成对象、转发回调与参数。
- **SyncMethod**：被包装方法是同步的——它**直接 `return` 结果**。适配器调用它、拿到返回值后，**替它**调用返回回调。

之所以需要两种，是因为 orc-rt 里既有「立刻能算出结果」的简单方法（用 `SyncMethod` 更顺手），也有「要等异步操作」的方法（必须用 `AsyncMethod`，让方法自己掌握何时回调）。

#### 4.3.2 核心流程

两个适配器的调用算子都长成 `operator()(Return, Obj, Args...)`——注意 **`Obj` 是第二个参数**，类型是 `ExecutorAddr`：

```
【AsyncMethod::operator()(Return, Obj, Args...)】
   Obj.toPtr<ClassT*>()           // 把执行端地址还原成对象指针
   (obj->*M)(Return, Args...)     // 调用异步方法，把返回回调与参数透传
                                   //   方法自己决定何时调 Return

【SyncMethod::operator()(Return, Obj, Args...)】
   Obj.toPtr<ClassT*>()           // 还原对象指针
   R = (obj->*M)(Args...)         // 调用同步方法，拿到返回值
   Return(R)                      // 适配器替它调返回回调
```

这解释了一个看似奇怪的约定：用这两个适配器的 wrapper，其 SPS 签名的**第一个实参总是 `SPSExecutorAddr`**（对象地址），例如 `int32_t(SPSExecutorAddr, int32_t, int32_t)`。那个 `SPSExecutorAddr` 就是 `Obj`——它随参数字节一起从 controller 传过来，在执行端被还原成「要调用哪个对象」。

> 把这与 4.1 的「方向不对称」对照：controller 调用 executor 时按**地址**定位 wrapper 函数；而这个 wrapper 函数内部，又用**第二个参数里的 `ExecutorAddr`** 定位「服务对象」。两层寻址，都靠地址，但层级不同。

#### 4.3.3 源码精读

`AsyncMethod` 适配器：

[include/orc-rt/WrapperFunction.h:226-236](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L226-L236) —— 构造时存下成员指针 `M`；`operator()` 把 `Obj` 经 `toPtr<ClassT*>()` 还原成对象，再用 `(obj->*M)(Return, Args...)` 调用，把返回回调与参数**原样转发**。方法本身是异步的，所以适配器不碰 `Return`。

工厂函数 `handleWithAsyncMethod`（顺手消除模板参数）：

[include/orc-rt/WrapperFunction.h:268-272](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L268-L272) —— 传入成员指针，靠函数参数推导出 `ClassT/ReturnT/ArgTs...`，省得你手写 `AsyncMethod<MyClass, ...>`。

`SyncMethod` 适配器——注意它**替**方法调用了 `Return`：

[include/orc-rt/WrapperFunction.h:286-298](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L286-L298) —— `operator()` 还原对象、调用 `(obj->*M)(Args...)` 拿到返回值 `R`，再 `Return(R)`。与 `AsyncMethod` 的区别就在最后这一步：同步方法不接收返回回调，适配器代为投递结果。

工厂函数 `handleWithSyncMethod`：

[include/orc-rt/WrapperFunction.h:329-333](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L329-L333) —— 同样靠成员指针推导模板参数。

测试里有最直观的对照——同一个 `Adder` 类既有同步 `addSync` 又有异步 `addAsync`：

[test/unit/SPSWrapperFunctionTest.cpp:269-278](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L269-L278) —— `addSync` 直接 `return X + Y`；`addAsync` 接收返回回调 `Return`，内部调 `Return(addSync(X, Y))`。两者算的是同一件事，只是返回方式不同。

两个 wrapper 各用一个适配器，**业务代码完全没写序列化**：

[test/unit/SPSWrapperFunctionTest.cpp:280-307](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L280-L307) —— `adder_add_async_sps_wrapper` 用 `handleWithAsyncMethod(&Adder::addAsync)`，`adder_add_sync_sps_wrapper` 用 `handleWithSyncMethod(&Adder::addSync)`。注意签名都是 `int32_t(SPSExecutorAddr, int32_t, int32_t)`——第一个 `SPSExecutorAddr` 就是对象地址。

两个测试用例验证它们结果一致：

[test/unit/SPSWrapperFunctionTest.cpp:289-318](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L289-L318) —— `HandleWtihAsyncMethod` 与 `HandleWithSyncMethod` 都传入 `ExecutorAddr::fromPtr(A.get())` 与 `41, 1`，都断言结果为 `42`。这说明对调用方而言，异步/同步是**透明**的——都是 `call` + `Expected<int32_t>` 回调。

生产代码里也是同款用法。`NativeDylibManager` 的 `load` / `lookup` 都用 `handleWithAsyncMethod` 一行注册成跨进程 API：

[lib/executor/sps-ci/NativeDylibManagerSPSCI.cpp:50-60](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/sps-ci/NativeDylibManagerSPSCI.cpp#L50-L60) —— `ORC_RT_SPS_WRAPPER` 宏把 `WrapperFunction::handleWithAsyncMethod(&NativeDylibManager::load)` 包成一个具名 wrapper 函数，签名里的 `SPSExecutorAddr` 即 dylib manager 对象地址。整个 SPS-CI 注册机制（u6-l3）正是建立在本讲的 `handle` + 适配器之上。

#### 4.3.4 代码实践

**实践目标**：亲手把一个新方法用适配器暴露成 wrapper，体会「样板被消除」的感觉。

**操作步骤**（示例代码，基于 `SPSWrapperFunctionTest.cpp` 修改，非项目原有代码）：

1. 在 `Adder` 类（[第 269-278 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L269-L278)）里加一个同步方法与一个异步方法：

   ```cpp
   // 示例代码：在 Adder 中新增两个方法
   int32_t subSync(int32_t X, int32_t Y) { return X - Y; }
   void subAsync(move_only_function<void(int32_t)> Return, int32_t X, int32_t Y) {
     Return(subSync(X, Y));
   }
   ```

2. 仿照 [第 300-307 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L300-L307)，各写一个 wrapper（注意签名第一个参数仍是 `SPSExecutorAddr`）：

   ```cpp
   // 示例代码：用适配器一行注册 wrapper
   static void adder_sub_sync_sps_wrapper(orc_rt_SessionRef S,
                                          orc_rt_WrapperFunctionBuffer ArgBytes,
                                          orc_rt_WrapperFunctionReturn Return,
                                          uint64_t CallId) {
     SPSWrapperFunction<int32_t(SPSExecutorAddr, int32_t, int32_t)>::handle(
         S, ArgBytes, Return, CallId,
         WrapperFunction::handleWithSyncMethod(&Adder::subSync));
   }
   ```

3. 仿照 [第 309-318 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L309-L318) 写一个测试，传入 `ExecutorAddr::fromPtr(A.get()), 41, 1`，断言结果为 `40`。

**需要观察的现象 / 预期结果**：你会发现 wrapper 函数体里**没有任何序列化/反序列化代码**——`handle` + `handleWithSyncMethod` 把这些全包了，你只声明了「用哪个方法、签名是什么」。同步方法用 `SyncMethod`（不必自己写回调），异步方法用 `AsyncMethod`（自己调回调）。

> 待本地验证：需先按 u1-l2 构建 `check-orc-rt-unit` 目标。加入新测试后重新编译运行，预期两个新用例（同步/异步）都通过且结果为 `40`。

#### 4.3.5 小练习与答案

**练习 1**：用 `handleWithSyncMethod` 包装的方法，其 SPS 签名为什么第一个实参必须是 `SPSExecutorAddr`？

> **答案**：因为 `SyncMethod::operator()` 的第二个参数是 `ExecutorAddr Obj`，它会被 `toPtr<ClassT*>()` 还原成对象指针再去调成员方法。这个 `Obj` 必须从参数字节里反序列化出来，所以签名里要有一个对应位置——SPS 用 `SPSExecutorAddr` 标记「这一位是个执行端地址」。controller 调用时把对象指针序列化进去，执行端还原出来。

**练习 2**：如果一个成员方法需要**异步**完成（比如要等一次内存映射操作），该用 `AsyncMethod` 还是 `SyncMethod`？为什么？

> **答案**：必须用 `AsyncMethod`。`SyncMethod` 会在方法返回后**立刻**用返回值调用 `Return`，可方法若要异步完成，返回时还没有结果。`AsyncMethod` 把返回回调交给方法自己，方法可以在任意时机（包括异步操作完成后）才调用它，契合 orc-rt 的异步 wrapper 签名（返回 `void`，靠 `Return` 通知完成）。

**练习 3**：`AsyncMethod` 与 `SyncMethod` 的 `operator()` 都是 `void` 返回。这跟 wrapper function「返回 `void`」是巧合吗？

> **答案**：不是巧合，是必然。`handle` 要求业务 handler 的返回回调「返回 `void`」（见 [第 379-380 行](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L379-L380) 的 `static_assert`）。适配器作为 handler 的同形替代物，其 `operator()` 也必须返回 `void`——真正的「结果回传」是靠调用 `Return`/`yield` 完成的，而不是靠返回值。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**端到端调用链标注**」任务，把抽象的流水线落回真实代码。

**任务**：以 [`SPSWrapperFunctionTest.cpp` 的 `BinaryOpViaLambda`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L66-L72)（调用端）与 [`add_via_lambda_sps_wrapper`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L55-L64)（执行端）为一对样本，绘制一次 `41 + 1 → 42` 的完整时序，**每一步标注它属于本讲的哪个概念、对应哪段源码**。

**要求**：

1. 标出调用端的四步：序列化参数 → 经 `Caller`（`DirectCaller`）发送 → 收到结果 → 判带外错误/反序列化（对应 [WrapperFunction.h:340-366](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L340-L366)）。
2. 标出统一签名层：`DirectCaller::operator()` 如何拼出 `(S, ArgBytes, Return, CallId)` 四元组并调用 wrapper 函数（对应 [DirectCaller.h:58-65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h#L58-L65)），以及 `CallId` 如何在 `Return` 里回环（[DirectCaller.h:25-32](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/DirectCaller.h#L25-L32)）。
3. 标出执行端 `handle` 的六步：查带外错误（短路①）→ 反序列化参数（短路②）→ 绑 `yield` 调 handler → handler 执行 `Return(X+Y)` → 序列化结果（短路③）→ `Return`（对应 [WrapperFunction.h:372-398](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L372-L398) 与 [159-174](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L159-L174)）。
4. 用三种颜色标出**三处带外错误短路**，并各写一句话说明「若此处失败，结果会是什么错误串」。
5. 进阶（可选）：把 `BinaryOpViaLambda` 改成「`Y` 传一个会触发反序列化失败的值」（例如直接构造一个空的 `ArgBytes` 喂给 `add_via_lambda_sps_wrapper` 的 `handle`），观察它走短路②并回 `"Could not deserialize wrapper function arg data"`。

**预期结果**：画出一张从「调用端序列化」到「执行端 `Return`」再到「调用端反序列化」的闭环图，能指着图说清「字节在哪里被赋予类型、在哪里被还原回字节、在哪里可能短路成错误」。这张图就是本讲的全部内容。

---

## 6. 本讲小结

- `orc_rt_WrapperFunction` 是两端**统一**的异步 C 签名 `void(Session, ArgBytes, Return, CallId)`：返回 `void` 意味着「完成」只能靠调用 `Return(S, ResultBytes, CallId)` 通知；`CallId` 是调用方提供、handler 原样回传的不透明上下文，用来配对调用与结果。
- 调用方向**不对称**（controller→executor 按地址、executor→controller 按 tag），但**两端签名相同**；不对称只影响「如何定位函数」。注意 docs/Design.md 的签名文字已过时，以头文件为准。
- `WrapperFunction::handle`（执行端）用 `Serializer` 编排「反序列化参数 → 调 handler → 序列化结果 → `Return`」，并用 `yield` 回调把「结果序列化」从业务里剥离；它有**三处带外错误短路**（上游错误透传、反序列化失败、序列化结果失败），与 u5-l1「先查带外错误再取数据」一脉相承。
- `WrapperFunction::call`（调用端）是 `handle` 的镜像：序列化参数 → 委托 `Caller` 发送 → 收到结果后判带外错误或反序列化。`Caller`（测试用 `DirectCaller`、生产用 `ControllerAccess`）把「传输」与「序列化」解耦；调用端在边界处用 `make_error<StringError>` 把带外错误提升成内部 `Error`。
- `AsyncMethod` / `SyncMethod` 适配器把「按对象地址调用成员方法」这一高频模式模板化：给一个成员指针即可产出可喂给 `handle` 的可调用对象，`SPSExecutorAddr` 总是签名的第一个实参；区别在于异步方法**自己**调返回回调，同步方法由适配器**代调**。
- 业务代码因此几乎不写序列化样板——这也是为什么 `NativeDylibManager::load/lookup` 等生产 API 能用一行 `handleWithAsyncMethod` 暴露成跨进程符号。

---

## 7. 下一步学习建议

本讲把 `Serializer` 当成抽象概念用，刻意没讲「字节到底是什么格式」。接下来的讲义：

- **u6-l1 Simple Packed Serialization 原理**：讲解 `Serializer` 的唯一在树实现——SPS 的 wire 格式（小端原语、序列为「长度 + 元素」）、`SPSOutputBuffer`/`SPSInputBuffer`、以及如何用 `SPSSerializationTraits` 为自定义类型实现 `size/serialize/deserialize`。读完你会真正理解本讲里 `Z.arguments().serialize(...)` 背后发生了什么。
- **u6-l2 SPSWrapperFunction：类型安全的调用**：讲解 `SPSWrapperFunction<SPSSig>` 如何在 `WrapperFunction::call/handle` 之上提供**类型安全**外层（本讲测试里用的 `SPSWrapperFunction<int32_t(int32_t,int32_t)>::call/handle` 正是它），并引入 `ORC_RT_SPS_WRAPPER` 宏。
- **u6-l3 控制接口（sps-ci）**：讲解如何把 SPS wrapper 注册成跨进程可调用符号表（本讲提到的 `NativeDylibManagerSPSCI.cpp` 就是范例），把 `handle` + 适配器放回 controller↔executor RPC 的完整大图里。
- 建议同时回看 [docs/Design.md 的 WrapperFunction 小节](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Design.md#L103-L131)，对照本讲修正其中过时的签名顺序，加深记忆。
