# SPSWrapperFunction：类型安全的调用

## 1. 本讲目标

上一讲（u5-2）我们已经把 wrapper function 的「字节进、字节出」流水线打通了：`WrapperFunction::handle` 负责在执行端「反序列化 → 调 handler → 序列化」，`WrapperFunction::call` 负责在调用端「序列化 → 传输 → 反序列化」。但当时我们把 **Serializer（序列化器）当成了一个抽象概念**——它知道怎么把 C++ 值变成字节、又怎么变回来，却没有给出任何具体实现。u6-1 给出了那套具体实现：SPS（Simple Packed Serialization）。

本讲要做的事，就是把这两者**焊接**起来：用一个模板 `SPSWrapperFunction<SPSSig>`，把一个「长得像函数签名」的东西（例如 `int32_t(int32_t, int32_t)`）当作类型参数，自动生成一套**编译期类型安全**的 `call` / `handle`。从此你写跨进程调用，就像写一个普通 C++ 函数一样：参数类型对不对、返回值怎么取，编译器在编译时就替你查好了。

读完本讲，你应该能够：

1. 说出 `SPSWrapperFunction<SPSSig>` 是一层多薄的封装——它只是往 `WrapperFunction::call/handle` 里**注入一个 SPS 序列化器**，核心流水线仍是 u5-2 那一套。
2. 写出 **SPS 签名语法**：例如 `int32_t(int32_t, int32_t)`、`void()`、`SPSError(bool)`、`SPSString(SPSExecutorAddr, int32_t)`，并解释「标签类型」与「宿主类型」如何自动对应。
3. 用 **`SPSWrapperFunction<Sig>::handle`** 在执行端实现一个处理器、用 **`SPSWrapperFunction<Sig>::call`** 在调用端发起一次类型安全的调用，并正确处理 `Expected<RetT>` / `Error` 形式的返回。
4. 用 **`ORC_RT_SPS_WRAPPER` 宏**把一段业务逻辑包成一个具备 C ABI 签名、可被符号表登记、可被跨进程按名字调用的具名 wrapper 函数。

---

## 2. 前置知识

本讲直接承接 **u5-2（Wrapper Function 签名与 call/handle）** 和 **u6-1（Simple Packed Serialization 原理）**。这里只做最短回顾，不重复它们的细节：

- **统一异步 C 签名**：所有跨进程调用最终都收敛成 `orc_rt_WrapperFunction`，即 `void(Session, ArgBytes, Return, CallId)`。返回 `void` 不代表「没有返回值」，而是「完成靠回调 `Return(S, ResultBytes, CallId)` 通知」（见 [include/orc-rt-c/WrapperFunction.h:L69-L72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/WrapperFunction.h#L69-L72)）。
- **handle / call 的分工**：`handle` 是**执行端**把收到的 `ArgBytes` 还原成参数、调你的 handler、再把结果序列化回去；`call` 是**调用端**把参数序列化、经 Caller 传输、再把结果还原回来。两者互为镜像（u5-2）。
- **SPS 三件套**：`SPSSerializationTraits<Tag, ConcreteT>` 提供 `size / serialize / deserialize`；`SPSArgList<Tag...>` 用递归把多个字段粘合成「先量后写」的两阶段序列化（u6-1）。
- **标签与具体类型分离**：标签（如 `SPSString`、`SPSExecutorAddr`）描述「线上长什么样」，宿主类型（如 `std::string`、`ExecutorAddr`）描述「内存里是什么 C++ 类型」，二者不必相同（u6-1）。

本讲独有的一个关键认知先放在这里：

- **SPS 签名就是 C++ 函数类型**。当我们写 `SPSWrapperFunction<int32_t(int32_t, int32_t)>` 时，模板参数 `int32_t(int32_t, int32_t)` 是一个**函数类型**（返回 `int32_t`、接受两个 `int32_t`）。模板会从中拆出「返回标签」与「参数标签列表」，再用它们去装配序列化器。所以 SPS 签名的语法和普通 C 函数原型几乎一模一样，只是必要时用 SPS 标签（如 `SPSString`、`SPSExecutorAddr`、`SPSError`）代替裸 C++ 类型。

---

## 3. 本讲源码地图

本讲围绕一个纯头文件展开，配两个测试文件：

| 文件 | 作用 |
|------|------|
| [`include/orc-rt/SPSWrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h) | **本讲主角**：定义 `ORC_RT_SPS_WRAPPER` 宏、`WFSPSHelper`（带透明转换的序列化/反序列化助手）、`WrapperFunctionSPSSerializer`（从签名拆出参数/返回的序列化器）、`SPSWrapperFunction<SPSSig>`（对外暴露的 `call`/`handle`）。 |
| [`include/orc-rt/WrapperFunction.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h) | **底层**：`WrapperFunction::call` / `handle` 的真正实现，以及 `AsyncMethod` / `SyncMethod` 两个消除「按对象地址调成员方法」样板的适配器。本讲反复回到这里看「字节到底怎么流」。 |
| [`test/unit/SPSWrapperFunctionTest.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp) | **离 SPS 最近的测试**：用 `DirectCaller`（无 Session、同线程回环）把 `call`/`handle` 串起来，覆盖宏、lambda、函数指针、`Error`/`Expected` 透明转换、成员方法适配器等多种写法。是本讲「最小可运行例子」的来源。 |
| [`test/unit/SessionTest.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp) | **接入 Session 的范例**：其中的 `add_sps_wrapper` 与 `ValidCallToController` 展示了如何用一个真实的 `controllerCaller`（经 `Session::callController`）发起一次 SPS 调用。本讲的综合实践即以此为蓝本。 |
| [`include/orc-rt/Session.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h) | （支撑）定义 `ControllerCaller` 与 `controllerCaller`，即「符合 Caller 约定、但走 Session 真实通道」的调用器。 |

> 提示：本讲的主角文件只有 ~150 行，几乎没有运行时逻辑，全靠模板在编译期把类型拼好。读不懂时，重点不是逐行抠实现，而是抓住「签名 → 序列化器 → WrapperFunction」这条装配链。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，自顶向下：

1. **`SPSWrapperFunction` 模板**——对外的薄封装，把 SPS 序列化器注入 `WrapperFunction`。
2. **`handle` / `call` 的类型安全封装**——执行端如何把字节还原成强类型参数、调用端如何把结果还原成 `Expected<RetT>`，以及 `Error` / `Expected` 的透明转换。
3. **`ORC_RT_SPS_WRAPPER` 宏**——把上述能力包成一个具名、C ABI 签名、可登记进符号表的 wrapper 函数。

### 4.1 SPSWrapperFunction 模板

#### 4.1.1 概念说明

`SPSWrapperFunction` 想解决的问题很朴素：u5-2 的 `WrapperFunction::call` / `handle` 把序列化工作**外包**给一个 `Serializer` 参数，但调用者每次都得自己手搓这个序列化器——又啰嗦又容易写错。SPS 既然已经能给任意类型做序列化（u6-1），那只要再给一个「签名」，就能源码生成出对应的序列化器。

于是有了这个模板：

```cpp
template <typename SPSSig> struct SPSWrapperFunction { ... };
```

它的模板参数 `SPSSig` 是一个**函数类型**，例如 `int32_t(int32_t, int32_t)`。模板内部会从这个函数类型里拆出「返回标签」和「参数标签列表」，分别装配出「结果的序列化器」和「参数的序列化器」，然后把它们喂给 `WrapperFunction`。

一句话总结：**`SPSWrapperFunction` 本身不搬一字节，它只负责「按签名生成 SPS 序列化器，并把它交给 `WrapperFunction`」**。真正搬字节的还是 u5-2 那条流水线。

#### 4.1.2 核心流程

整条装配链可以这样描述（以 `SPSWrapperFunction<int32_t(int32_t, int32_t)>` 为例）：

1. **拆签名**：偏特化 `WrapperFunctionSPSSerializer<int32_t(int32_t, int32_t)>` 把签名拆成「返回标签 `int32_t`」与「参数标签 `<int32_t, int32_t>`」。
2. **造序列化器**：对参数标签造一个 `WFSPSHelper<int32_t, int32_t>`（负责参数的序列化/反序列化），对返回标签造一个 `WFSPSHelper<int32_t>`（负责结果）。
3. **委托**：`SPSWrapperFunction::call/handle` 把这个序列化器连同你给的 Caller / Handler 一起，原样转交给 `WrapperFunction::call/handle`。
4. **执行**（u5-2 的流水线）：`call` 用序列化器把参数打包成 `ArgBytes` → 经 Caller 传输 → 拿到 `ResultBytes` → 反序列化成 `Expected<int32_t>` 交给你的回调；`handle` 反过来走。

关键在于第 1、2 步全是**编译期**完成的，没有任何运行时开销——你写 `int32_t(int32_t, int32_t)`，编译器就知道要序列化两个 `int32_t`、反序列化一个 `int32_t`。

#### 4.1.3 源码精读

先看对外结构 `SPSWrapperFunction`，它只有两个对外方法，且各自都只是一行委托：

[include/orc-rt/SPSWrapperFunction.h:L123-L149](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L123-L149) —— 定义 `SPSWrapperFunction<SPSSig>`，`call` 与 `handle` 都是把 `WrapperFunctionSPSSerializer<SPSSig>()` 作为序列化器传给底层 `WrapperFunction`，其余参数原样转发。

其中 `call` 的核心是这一行，把签名生成的序列化器塞进 `WrapperFunction::call`：

[include/orc-rt/SPSWrapperFunction.h:L124-L129](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L124-L129) —— `SPSWrapperFunction::call` 委托给 `WrapperFunction::call`，第二个实参 `WrapperFunctionSPSSerializer<SPSSig>()` 就是「按签名现造的 SPS 序列化器」。

那么「按签名现造」具体怎么做？看偏特化：

[include/orc-rt/SPSWrapperFunction.h:L114-L118](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L114-L118) —— `WrapperFunctionSPSSizer<SPSRetT(SPSArgTs...)>` 偏特化：`arguments()` 返回 `WFSPSHelper<SPSArgTs...>`（负责所有参数），`result()` 返回 `WFSPSHelper<SPSRetT>`（负责返回值）。这就是「拆签名」的落点。

而 `WFSPSHelper` 内部，序列化无非是「按 SPSArgList 算大小 → 分配缓冲 → 逐个写」，反序列化是「逐个读 → 还原成 tuple」：

[include/orc-rt/SPSWrapperFunction.h:L33-L42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L33-L42) —— `serializeImpl`：先用 `SPSArgList<SPSArgTs...>::size(...)` 算出总字节数并分配缓冲，再用 `SPSArgList::serialize(...)` 把参数逐个写进去（u6-1 的「先量后写」两阶段）。

> 这几段代码印证了 4.1.1 的结论：`SPSWrapperFunction` 不做任何运行时决策，它把 u6-1 的 SPS 工具与 u5-2 的 `WrapperFunction` 用模板胶水粘到一起。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手验证「签名 → 序列化器 → WrapperFunction」这条装配链确实只有一层委托。

1. **实践目标**：确认 `SPSWrapperFunction::call` 没有任何额外的运行时逻辑，只是转发。
2. **操作步骤**：
   - 打开 [include/orc-rt/SPSWrapperFunction.h:L124-L129](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L124-L129)，找到 `call` 调用 `WrapperFunction::call(...)` 的那一行。
   - 顺着第二个实参 `WrapperFunctionSPSSerializer<SPSSig>()` 跳到偏特化（L114-L118），看清 `arguments()` / `result()` 各返回什么。
   - 再跳进 `WrapperFunction::call` 的定义 [include/orc-rt/WrapperFunction.h:L340-L366](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L340-L366)，确认它对 `Z`（序列化器）只调了 `Z.arguments().serialize(...)`。
3. **需要观察的现象**：三个文件跳转下来，你会发现 `SPSWrapperFunction` 这一层的代码量极少，真正的搬字节逻辑全在 `WrapperFunction` 和 SPS 里。
4. **预期结果**：你能用一句话向别人解释「`SPSWrapperFunction` = `WrapperFunction` + 按签名生成的 SPS 序列化器」。

#### 4.1.5 小练习与答案

**练习 1**：如果把签名写成 `int32_t(int32_t, int32_t)`，那么 `WrapperFunctionSPSSizer` 偏特化里 `SPSRetT` 和 `SPSArgTs...` 分别被推导成什么？

**参考答案**：`SPSRetT = int32_t`，`SPSArgTs... = int32_t, int32_t`。因此 `result()` 返回 `WFSPSHelper<int32_t>`，`arguments()` 返回 `WFSPSHelper<int32_t, int32_t>`。

**练习 2**：为什么 `SPSWrapperFunction` 的 `handle` 提供了两个重载（一个收 `WrapperFunctionBuffer`、一个收 `orc_rt_WrapperFunctionBuffer`）？

**参考答案**：因为 wrapper function 的 C ABI 入口（见 4.3）拿到的是 C 结构 `orc_rt_WrapperFunctionBuffer`，而 C++ 业务代码更习惯用 RAII 的 `WrapperFunctionBuffer`。第二个重载用 `WrapperFunctionBuffer(ArgBytes)` 把 C 结构包成 C++ 对象再转调第一个，免去调用方手动转换（见 [include/orc-rt/SPSWrapperFunction.h:L142-L148](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L142-L148)）。

---

### 4.2 handle / call 的类型安全封装

#### 4.2.1 概念说明

模板装配好序列化器之后，真正的「类型安全」体现在两端：

- **执行端 `handle`**：你写一个 handler，它的参数类型就是你想要的 C++ 类型（如 `int32_t X, int32_t Y`）。`handle` 会把 `ArgBytes` 反序列化成这些类型的值，再喂给你的 handler；handler 算完结果，通过一个**返回回调** `Return(...)` 把结果交还，`handle` 再把它序列化回去。
- **调用端 `call`**：你直接传 C++ 实参（如 `41, 1`），`call` 帮你序列化、传输；结果回来时，你的 `ResultHandler` 收到的是**强类型**的 `Expected<RetT>`（如 `Expected<int32_t>`）——成功就拿到值，失败就拿到 `Error`，二者必居其一。

这里有两个对初学者特别友好的设计，值得单独点出：

1. **`Error` / `Expected<T>` 的透明转换**。你的业务函数天然返回 `Error` 或 `Expected<T>`（u2-3），但 SPS 只认自己的标签 `SPSError` / `SPSExpected<T>`（[include/orc-rt/SimplePackedSerialization.h:L625](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SimplePackedSerialization.h#L625)）。`WFSPSHelper` 内部有一组 `Serializable<>` 特化，在「序列化前」自动把 `Error` 包成 `SPSSerializableError`、在「反序列化后」自动还原回 `Error`，你**完全不用手写转换**。
2. **handler 可以按引用接收参数**。`handle` 并不要求 handler 按值收参数——按值、按 `&`、按 `const &`、按 `&&` 都可以，它会按 handler 声明的形参类型完美转发。这意味着你可以用 `const std::string&` 避免一次拷贝。

#### 4.2.2 核心流程

**执行端 `handle` 的完整时序**（最终落到 `WrapperFunction::handle`）：

```
收到 ArgBytes
   │
   ├─ 若 ArgBytes 是带外错误?  → 直接 Return 透传（短路，不反序列化）
   │
   ├─ Z.arguments().deserialize<ArgTuple>(ArgBytes)
   │      成功? → 用 std::apply 把参数喂给 handler，
   │             handler 的第一个实参是一个 StructuredYield（返回回调）
   │      失败? → Return 一个 "Could not deserialize ..." 的带外错误
   │
   └─ handler 内部算完结果 → 调 Return(结果)
        StructuredYield 把结果序列化成 ResultBytes → Return 回传给调用端
           序列化失败? → Return "Could not serialize ..." 的带外错误
```

注意三处**带外错误短路**（u5-1 的「先查带外错误再取数据」一脉相承）：上游错误透传、反序列化失败、序列化失败。这三条短路保证「无论哪一步出错，调用端都能拿到一个明确的错误，而不是拿到半截坏数据」。

**调用端 `call` 的完整时序**（最终落到 `WrapperFunction::call`）：

```
serialize 参数 → ArgBytes
   │
   ├─ 序列化失败? → 直接给 ResultHandler 一个 StringError（连 Caller 都不调）
   │
   ├─ Caller(结果回调, ArgBytes)
   │      Caller 负责把 ArgBytes 运到对端、再把结果字节运回来
   │
   └─ 结果回调拿到 ResultBytes
          是带外错误? → RH(make_error<StringError>(ErrMsg))
          否则        → ResultDeserializer 把字节还原成 Expected<RetT> → RH(...)
```

调用端的返回类型由 **`ResultHandler` 自己声明的形参**决定：若你写 `[&](Expected<int32_t> R){...}`，则 `RetT = int32_t`，结果被还原成 `Expected<int32_t>`；若你写 `[&](Error Err){...}`，说明你只关心成败、不要值。这与签名的返回标签必须对齐，否则编译不过——这就是「类型安全」的另一面。

#### 4.2.3 源码精读

先看执行端 `WrapperFunction::handle`，它是上面时序的落点：

[include/orc-rt/WrapperFunction.h:L372-L398](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L372-L398) —— `handle` 的三段：先查带外错误并透传（L383-L384），再反序列化参数；成功则用 `std::apply` + `forwardArgsAsRequested` 把参数喂给 handler，并把一个 `StructuredYield` 作为返回回调绑在 handler 最前（L386-L391）；反序列化失败则返回带外错误（L392-L397）。

其中「按 handler 形参类型转发」靠的是 `WFHandlerTraits` 与 `forwardArgsAsRequested`——它读取 handler 的形参类型，再决定每个参数按值 / `&` / `const&` / `&&` 传递（这就是 4.2.1 第 2 点的落点）。

返回回调 `StructuredYield` 负责把结果序列化后回传：

[include/orc-rt/WrapperFunction.h:L159-L174](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L159-L174) —— `StructuredYield<tuple<RetT>>`：handler 调 `Return(value)` 时，它用 `Z.result().serialize(...)` 把结果序列化，成功就 `Return`，失败就回传「Could not serialize」带外错误。对 `void` 返回（`tuple<>`）的特化则回传一个空缓冲（L176-L184）。

再看调用端 `WrapperFunction::call`：

[include/orc-rt/WrapperFunction.h:L340-L366](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L340-L366) —— `call`：先用 `Z.arguments().serialize(...)` 把参数打包；序列化失败直接给 `ResultHandler` 一个 `StringError`（L363-L365）；否则构造一个闭包作为「结果回调」交给 `Caller`。闭包里：若是带外错误则 `RH(make_error<StringError>(...))`（L356-L357），否则用 `ResultDeserializer` 还原成强类型结果再 `RH(...)`（L358-L360）。

`ResultDeserializer` 把字节还原成 `Expected<RetT>`：

[include/orc-rt/WrapperFunction.h:L188-L199](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L188-L199) —— `ResultDeserializer<tuple<Expected<T>>>`：反序列化成功则用 `ForceExpectedSuccessValue` 构造一个成功的 `Expected<T>`，失败则返回 `make_error<StringError>("Could not deserialize result")`。这正是 `call` 端「结果一定是 `Expected<RetT>`」的来源。

最后看 **`Error` / `Expected` 的透明转换**——这是「业务函数不用改返回类型」的关键。`WFSPSHelper::serialize` 在序列化前会把每个实参过一遍 `Serializable<decay_t<Arg>>::to(...)`：

[include/orc-rt/SPSWrapperFunction.h:L90-L94](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L90-L94) —— `serialize` 把每个实参先用 `Serializable<...>::to(...)` 转成「可序列化形态」再交给 `serializeImpl`。

而 `Serializable<Error>` / `Serializable<Expected<T>>` 两个特化正是「桥」：

[include/orc-rt/SPSWrapperFunction.h:L50-L66](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L50-L66) —— `Serializable<Error>` 把 `Error` 转成 `SPSSerializableError`（SPS 认识的形态），反方向用 `toError()` 还原；`Serializable<Expected<T>>` 同理走 `SPSSerializableExpected<T>`。于是你写 `SPSError(bool)` 的签名、handler 里直接用 `Error`，两端自动对接，零样板。

#### 4.2.4 代码实践

这是一个**可直接运行**的最小例子，沿用项目自带测试的写法（`DirectCaller` 在同线程内把 `call` 与 `handle` 串起来，不需要 Session）。

1. **实践目标**：亲手写一个「两数相加」的 SPS wrapper，并从调用端断言拿到 `42`，体会「参数/返回值全程强类型」。
2. **操作步骤**：阅读 [test/unit/SPSWrapperFunctionTest.cpp:L55-L72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L55-L72)。它定义了一个用 lambda 实现的执行端处理器 `add_via_lambda_sps_wrapper`，并在 `BinaryOpViaLambda` 测试里用 `SPSWrapperFunction<int32_t(int32_t, int32_t)>::call(...)` 发起调用、断言 `Result == 42`。把这段逻辑抄进一个新测试，把 `Return(X + Y)` 改成 `Return(X - Y)`、把实参从 `41, 1` 改成 `50, 8`。
3. **需要观察的现象**：编译期——签名 `int32_t(int32_t, int32_t)` 决定了 handler 必须收两个 `int32_t`、返回回调收一个 `int32_t`，类型不匹配会直接编译失败；运行期——`ResultHandler` 收到的是 `Expected<int32_t>`，用 `cantFail` 取出值。
4. **预期结果**：修改后断言应为 `EXPECT_EQ(Result, 42)`（50 − 8 = 42）。构建并运行 `check-orc-rt-unit`，该测试通过。

> 若想进一步体会「透明转换」，可参考 [test/unit/SPSWrapperFunctionTest.cpp:L119-L154](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L119-L154)：签名写成 `SPSError(bool)`，handler 里直接 `return Error` / `make_error<StringError>(...)`，调用端收到 `Expected<Error>`——全程不用手写 `Error ↔ SPSError` 的转换。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `handle` 的 handler 第一个参数总是一个「返回回调」（如 `move_only_function<void(int32_t)> Return`），而不是直接 `return` 结果？

**参考答案**：因为 wrapper function 的底层是**异步**签名 `void(...)`——它返回 `void`，「完成」只能靠回调 `Return(S, ResultBytes, CallId)` 通知（u5-2）。所以 handler 不能 `return` 结果，而必须通过返回回调把结果交还；返回回调内部再做序列化与回传。

**练习 2**：调用端 `ResultHandler` 的形参为什么是 `Expected<int32_t>` 而不是 `int32_t`？

**参考答案**：因为跨进程调用可能失败（序列化失败、反序列化失败、对端返回带外错误等）。`Expected<int32_t>` 把「值」与「错误」统一成一个返回类型，遵循 orc-rt「错误必须被检查」的契约（u2-3）：调用方必须用 `cantFail` / `takeError` 显式处理，绝无静默吞错的可能。

**练习 3**：`ExecutorAddr` 作为参数时，签名该怎么写？为什么调用时要传 `ExecutorAddr::fromPtr(...)` 而不是裸指针？

**参考答案**：签名用标签 `SPSExecutorAddr`（如 `int32_t(SPSExecutorAddr, int32_t, int32_t)`）。调用时传 `ExecutorAddr::fromPtr(A.get())`：`ExecutorAddr` 是「类型安全的地址整数」（u2-2），`fromPtr` 把指针打包成可在跨进程序列化中安全搬运的 `uint64`，避免裸指针的类型与宽度陷阱。范例见 [test/unit/SPSWrapperFunctionTest.cpp:L284-L298](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L284-L298)。

---

### 4.3 ORC_RT_SPS_WRAPPER 宏

#### 4.3.1 概念说明

4.2 里每次写执行端处理器，都要手写一个「C ABI 签名的外壳函数」，再在里面转调 `SPSWrapperFunction::handle`——这段外壳代码每次几乎一模一样，纯属样板。`ORC_RT_SPS_WRAPPER` 宏就是来消灭这段样板的。

它的用法长这样（取自测试）：

```cpp
ORC_RT_SPS_WRAPPER(add_via_function_sps_wrapper,    // 生成的外壳函数名
                   int32_t(int32_t, int32_t),       // SPS 签名
                   add_via_function);               // 真正的业务函数
```

这一行就展开成一个**具备 C ABI 签名**的具名函数 `add_via_function_sps_wrapper`，它的函数体只是 `SPSWrapperFunction<int32_t(int32_t,int32_t)>::handle(..., add_via_function)`。

为什么要强调「C ABI 签名」？因为执行端的能力最终要以**符号**形式登记进符号表（`SimpleSymbolTable`），再经 `BootstrapInfo` 暴露给控制端（u5-3、u7-1、u8-1）。符号表存的是 `orc_rt_WrapperFunction` 这种 C 函数指针，所以外壳函数必须长成 `void(Session, ArgBytes, Return, CallId)` 的样子——宏正是替你保证这一点。

#### 4.3.2 核心流程

宏展开后的执行端处理器，其行为与 4.2 的 `handle` 完全一致，区别只在「外壳由宏生成」：

1. **登记**：把宏生成的函数指针（如 `&add_via_function_sps_wrapper`）作为符号加入 `SimpleSymbolTable`，或经 `controllerCaller` 按 tag 调用。
2. **被调用**：对端按地址 / tag 调到这个外壳函数，传入 `ArgBytes`。
3. **转调**：外壳函数体调用 `SPSWrapperFunction<SPSSig>::handle(S, ArgBytes, Return, CallId, Handle)`。
4. **执行**：`handle` 按 4.2.2 的时序反序列化、调业务函数、序列化、回传。

> 设计要点：宏把「业务函数」与「SPS 签名」绑在一起，**业务函数本身对 wrapper function 一无所知**——它只是一个普通的 `void(move_only_function<void(int32_t)>, int32_t, int32_t)`。这使得同一份业务逻辑既能直接本地调用，也能经 wrapper 跨进程调用。

#### 4.3.3 源码精读

宏的定义只有几行，但信息量集中：

[include/orc-rt/SPSWrapperFunction.h:L21-L26](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/SPSWrapperFunction.h#L21-L26) —— `ORC_RT_SPS_WRAPPER(Name, SPSSig, Handle)` 展开成一个 `static void Name(orc_rt_SessionRef, orc_rt_WrapperFunctionBuffer, orc_rt_WrapperFunctionReturn, uint64_t)`，函数体一行：`SPSWrapperFunction<SPSSig>::handle(S, ArgBytes, Return, CallId, Handle)`。

注意三个细节：

- **外壳签名用的是 C 类型**（`orc_rt_SessionRef` / `orc_rt_WrapperFunctionBuffer` / `orc_rt_WrapperFunctionReturn`），这正是它能被符号表登记、被 C 端按函数指针调用的原因。
- **`Handle` 可以是任意可调用对象**：普通函数指针、lambda、甚至 `WrapperFunction::handleWithAsyncMethod(&Cls::method)` 这种适配器（见 4.3.4）。
- **宏刻意放在 `using namespace orc_rt;` 之前**：测试里有一句注释「This macro use has been deliberately moved above the `using namespace orc_rt;` ... to check that its expansion works from other namespaces」——说明宏展开用的是全限定名 `orc_rt::SPSWrapperFunction`，因此**在任何命名空间里都能用**，不依赖 `using`。

测试里的实际用法（业务函数 + 宏）：

[test/unit/SPSWrapperFunctionTest.cpp:L23-L32](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L23-L32) —— `add_via_function` 是普通业务函数（签名 `void(move_only_function<void(int32_t)>, int32_t, int32_t)`），紧跟一行 `ORC_RT_SPS_WRAPPER(add_via_function_sps_wrapper, int32_t(int32_t,int32_t), add_via_function)` 把它包成具名 wrapper。随后 `BinaryOpViaFunction` 测试直接调用这个外壳符号。

#### 4.3.4 代码实践

这个实践演示「`Handle` 不必是普通函数，也可以是成员方法适配器」，并验证 `ExecutorAddr` 参数。

1. **实践目标**：理解 `ORC_RT_SPS_WRAPPER` 的 `Handle` 形参可以是 `WrapperFunction::handleWithAsyncMethod / handleWithSyncMethod` 返回的适配器，从而把「按对象地址调成员方法」也纳入 SPS。
2. **操作步骤**：阅读 [test/unit/SPSWrapperFunctionTest.cpp:L269-L298](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SPSWrapperFunctionTest.cpp#L269-L298)。`Adder` 类有同步方法 `addSync` 和异步方法 `addAsync`；外壳 `adder_add_async_sps_wrapper` 用签名 `int32_t(SPSExecutorAddr, int32_t, int32_t)` + `handleWithAsyncMethod(&Adder::addAsync)`，把「对象地址 + 两数」转发成 `(A->*addAsync)(Return, X, Y)`。
3. **需要观察的现象**：签名首个实参标签是 `SPSExecutorAddr`，调用端传 `ExecutorAddr::fromPtr(A.get())`（u2-2）；适配器内部用 `Obj.toPtr<ClassT*>()` 把地址还原成对象指针再调成员方法（见 [include/orc-rt/WrapperFunction.h:L226-L236](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/WrapperFunction.h#L226-L236)）。
4. **预期结果**：`HandleWtihAsyncMethod` 与 `HandleWithSyncMethod` 两个测试都断言 `Result == 42`，构建运行 `check-orc-rt-unit` 通过。这两个测试印证：成员方法也能零样板地变成跨进程可调用符号。

#### 4.3.5 小练习与答案

**练习 1**：为什么宏生成的外壳函数要声明为 `static`？

**参考答案**：`static` 限定其在翻译单元内可见，避免多个文件里同名 wrapper 产生链接冲突。最终它以**函数指针**的形式（取地址 `&name` 或 `reinterpret_cast` 成 tag）登记进符号表，是否 `static` 不影响「按地址调用」。

**练习 2**：`ORC_RT_SPS_WRAPPER` 与「手写一个外壳函数再调 `SPSWrapperFunction::handle`」相比，省掉了什么？

**参考答案**：省掉了 (1) 手写那段固定的 C ABI 函数签名 `void(orc_rt_SessionRef, orc_rt_WrapperFunctionBuffer, orc_rt_WrapperFunctionReturn, uint64_t)`，(2) 手写那一行 `SPSWrapperFunction<SPSSig>::handle(...)` 转调。宏把它们压成一行声明，且保证签名与签名里的 `SPSSig` 始终一致，杜绝「外壳签名与 SPS 签名对不上」的低级错误。

---

## 5. 综合实践

把三个模块串起来：实现一个 **SPS 签名为 `int32_t(int32_t, int32_t)` 的「乘法」wrapper**，并写一个调用方**经 `controllerCaller` 真实通道**（而非 `DirectCaller`）发起调用、断言结果。这是本讲规格指定的综合任务，蓝本是 `SessionTest` 的 `add_sps_wrapper` + `ValidCallToController`。

### 步骤 1：定义执行端处理器（乘法）

仿照 [test/unit/SessionTest.cpp:L670-L679](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L670-L679) 的 `add_sps_wrapper`，把 `Return(X + Y)` 换成 `Return(X * Y)`：

```cpp
// 示例代码：基于 SessionTest::add_sps_wrapper 改写
static void mul_sps_wrapper(orc_rt_SessionRef S,
                            orc_rt_WrapperFunctionBuffer ArgBytes,
                            orc_rt_WrapperFunctionReturn Return,
                            uint64_t CallId) {
  SPSWrapperFunction<int32_t(int32_t, int32_t)>::handle(
      S, ArgBytes, Return, CallId,
      [](move_only_function<void(int32_t)> Return, int32_t X, int32_t Y) {
        Return(X * Y);   // 唯一改动：加法变乘法
      });
}
```

> 这是「示例代码」，但每一行都与 `add_sps_wrapper` 同构，只是把运算从 `+` 换成 `*`。

### 步骤 2：写调用方（经 controllerCaller）

仿照 [test/unit/SessionTest.cpp:L730-L745](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L730-L745) 的 `ValidCallToController`：

```cpp
// 示例代码：基于 ValidCallToController 改写
TEST(ControllerAccessTest, MulViaController) {
  QueueingRunner<>::WorkQueue Tasks;
  Session S(mockExecutorProcessInfo(), QueueingRunner(Tasks), noErrors);
  S.attach<MockControllerAccess>(BootstrapInfo(S), postOnto(Tasks));

  int32_t Result = 0;
  SPSWrapperFunction<int32_t(int32_t, int32_t)>::call(
      S.controllerCaller(
          reinterpret_cast<orc_rt_ControllerHandlerTag>(mul_sps_wrapper)),
      [&](Expected<int32_t> R) { Result = cantFail(std::move(R)); },
      6, 7);

  QueueingRunner<>::runFIFOUntilEmpty(Tasks);   // 抽干异步队列，让回调跑完

  EXPECT_EQ(Result, 42);   // 6 * 7 = 42
}
```

这里 `S.controllerCaller(...)` 返回一个 `ControllerCaller`，它符合 `WrapperFunction::call` 对 Caller 的约定，内部最终走 [include/orc-rt/Session.h:L483-L491](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L483-L491) 的 `Session::callController`——这才是「真实通道」，与 4.2 里同线程回环的 `DirectCaller` 不同。

### 步骤 3：验证与观察

1. **类型安全**：把签名误写成 `int32_t(int32_t)`（少一个参数），或把回调形参写成 `Expected<int64_t>`，观察编译器报错——这能在编译期挡住签名不匹配。
2. **失败路径**：参考 [test/unit/SessionTest.cpp:L747-L763](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/SessionTest.cpp#L747-L763) 的 `CallToControllerBeforeAttach`：在 `attach` **之前**发起调用，`ResultHandler` 应收到 `Expected<int32_t>` 的失败态，`toString(...)` 应为 `"no controller attached"`（因为 `callController` 在未 attach 时直接构造带外错误，见 [include/orc-rt/Session.h:L486-L490](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt/Session.h#L486-L490)）。
3. **运行**：构建并运行 `check-orc-rt-unit`，确认 `MulViaController` 通过。

**预期结果**：`Result == 42`；未 attach 时拿到 `"no controller attached"` 错误。

> 待本地验证：以上新测试需在你的本地构建中编译并运行确认（蓝本 `ValidCallToController` / `CallToControllerBeforeAttach` 均为项目既有用例，可作对照）。

---

## 6. 本讲小结

- `SPSWrapperFunction<SPSSig>` 是一层**极薄**的模板封装：它把「SPS 签名（一个 C++ 函数类型）」拆成返回/参数标签，据此装配出 SPS 序列化器，再注入 u5-2 的 `WrapperFunction::call/handle`，自身不搬一字节。
- **类型安全**体现在两端：执行端 `handle` 把 `ArgBytes` 反序列化成 handler 声明的强类型参数、把结果序列化回传；调用端 `call` 把强类型实参序列化、把结果还原成 `Expected<RetT>` 交给回调——签名不匹配会在编译期暴露。
- **三处带外错误短路**（上游错误透传、反序列化失败、序列化失败）贯穿 `handle`，保证「出错也有明确错误、绝不返回半截坏数据」。
- `Error` / `Expected<T>` 的**透明转换**（`Serializable<>` 特化）让业务函数直接用熟悉的 `Error`/`Expected<T>` 返回，SPS 标签 `SPSError`/`SPSExpected<T>` 与之自动对接，零样板。
- handler 可按**值 / `&` / `const&` / `&&`** 接收参数（`forwardArgsAsRequested`），既能避免多余拷贝，也支持 `ExecutorAddr` 这类带标签指针参数。
- `ORC_RT_SPS_WRAPPER` 宏把任意业务可调用对象包成一个**具备 C ABI 签名、可登记进符号表**的具名 wrapper 函数，是「暴露执行端能力为跨进程符号」的标准入口（为 u6-3 的 sps-ci 铺路）。

---

## 7. 下一步学习建议

本讲让 SPS 调用在「单机、同线程 / 经 Session 通道」的层面变得类型安全。接下来：

- **u6-3（控制接口 sps-ci）**：本讲的 `ORC_RT_SPS_WRAPPER` 生成的具名 wrapper，正是 sps-ci 用 `addAll` / `addCall` 批量登记进 `SimpleSymbolTable` 的素材。下一讲会讲清楚「这些 wrapper 如何聚合成一张跨进程可调用的符号表」，并以 `call_void_void` / `call_main` 为例串起「定义 wrapper → 登记符号 → controller 按名调用」的完整链路。
- **u8-1（BootstrapInfo）**：符号表最终怎么经 `BootstrapInfo` 在 `connect` 时传给 controller，让 controller 第一次就能按名字找到这些 wrapper——这是本讲「具名 wrapper」的归宿。
- **u7-1（SimpleSymbolTable）**：想理解 `addUnique` 的「冲突即报错」语义，以及符号表如何同时服务于 Service API 暴露与 BootstrapInfo，可作为补充阅读。
