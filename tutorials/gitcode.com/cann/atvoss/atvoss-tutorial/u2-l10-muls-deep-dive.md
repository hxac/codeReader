# muls 样例深入：标量、Cast 与多 Compute

## 1. 本讲目标

本讲以 `examples/muls/muls.cpp` 为唯一研究对象，把前面几讲学到的「表达式模板」「PlaceHolder/LocalVar」「运算符库」「ArgumentsBuilder」串成一个**真实可运行的算子**。读完本讲，你应当能够：

1. 写出一个**标量（scalar）**参与运算的 ATVOSS 表达式，并解释它为何比张量乘法更高效。
2. 用 `Cast` + `PlaceHolderTmpLike` 在表达式内部完成「输入类型不匹配时的类型转换」（如 int32→float）。
3. 在**同一个 `Config` 内**为不同输入数据类型各定义一套 `Compute`，并用 `if constexpr` 在运行时按类型挑选正确的 `DeviceOp`。

本讲是进阶篇的收束：前 9 讲分别讲了机制，本讲让这些机制在一个样例里同台登场。

## 2. 前置知识

在进入源码前，先回顾两个本讲会反复用到的概念（细节见依赖讲义）。

- **标量 vs 张量操作数**。ATVOSS 表达式里，一个 `PlaceHolder` 既可以声明成 `Tensor<T>`（一块设备上的张量数据），也可以声明成普通 C++ 标量类型（如 `float`）。运算符重载会根据操作数「是不是标量」生成**不同**的底层指令（`Muls` 单值广播 vs `Mul` 逐元素）。详见 u2-l3。
- **Param 与 LocalVar 两套序号**。`PlaceHolder<N, ...>` 产出的是 `Param`（外部入参/出参，序号 N 对应 `ArgumentsBuilder` 的第 N 个实参）；`PlaceHolderTmpLike<N, ...>(某Param)` 产出的是 `LocalVar`（内部临时变量）。两者是**各自从 1 开始、互不干扰**的独立序号空间。详见 u2-l2。
- **逗号表达式 = 顺序执行**。`(A, B)` 在普通 C++ 里会丢弃 A，但在 ATVOSS 里 `operator,` 被重载成 `OpAndThen`，表示「先执行 A，再执行 B」，从而把多步计算串成一条流水。详见 u2-l1。

> 名词速查：`Muls` = tensor × scalar（标量乘）；`Mul` = tensor × tensor（张量逐元素乘）；`Cast` = 类型转换；`Compute` = 用户写的、只含计算公式的结构体；`DeviceOp` = 组装完成、可 `.Run()` 的算子对象。

## 3. 本讲源码地图

本讲只围绕一个样例展开，但会向上追溯到支撑它的几个框架头文件。

| 文件 | 作用 |
|------|------|
| [examples/muls/muls.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp) | 本讲主角：定义 `MulsConfig`（含两套 `Compute`）与 `Run` 执行流程 |
| [examples/muls/README.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/README.md) | 样例说明：算子公式、参数表、编译运行命令 |
| include/expression/expr_template.h | `ParamUsage` 枚举、`PlaceHolder` / `PlaceHolderTmpLike` 工厂 |
| include/operators/math_expression.h | `OpMul` / `operator*`、`OpCast` / `Cast` 的声明 |
| include/operators/math_evaluator.h | 把 `OpMul`/`OpCast` 翻译成 `AscendC::Muls`/`AscendC::Cast` 的求值器特化 |
| include/utils/patterns.h | `CastMode` 枚举（7 种舍入方式） |

## 4. 核心概念与源码讲解

### 4.1 标量参与运算：MulsCompute

#### 4.1.1 概念说明

`Muls` 算子的数学定义很简单：让一个张量的每个元素都乘以同一个标量。

\[
\text{out}_i = \text{in}_i \times s,\quad s\text{ 为标量}
\]

在 ATVOSS 里，关键问题是：**标量 `s` 在表达式里怎么表达？** 答案是——它就是一个普通的 `PlaceHolder`，只不过声明的类型不是 `Tensor<float>`，而是裸标量类型 `float`。框架会通过编译期类型萃取（`std::is_scalar_v`）自动识别「这一侧是标量」，从而生成更高效的标量广播指令，而不是浪费地把标量先展开成一个张量。

#### 4.1.2 核心流程

标量乘法的「声明 → 求值」链路如下：

1. **声明**：`scalar = PlaceHolder<2, float, IN>()`，类型参数是标量 `float` 而非 `Tensor<float>`。
2. **建表达式**：`in * scalar` 命中 `operator*` 的「左 Expression、右标量」重载，生成 `Expression<OpMul<Param, float>>`。
3. **求值分派**：求值器 `Evaluator<OpAssign<dst, OpMul<U,V>>>` 用 `if constexpr (std::is_scalar_v<...>)` 判定 V 侧是标量，于是调用 `MulsAssign` → `AscendC::Muls`（标量广播指令）；若两侧都是张量，则走 `MulAssign` → `AscendC::Mul`。

一句话：**你写的都是 `*`，框架替你选了最合适的指令。**

#### 4.1.3 源码精读

先看 `MulsCompute` 的完整定义——它只有 4 行有效代码：

```cpp
// examples/muls/muls.cpp
struct MulsCompute {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        auto in     = Atvoss::PlaceHolder<1, Tensor<TensorDtype>, Atvoss::ParamUsage::IN>();
        auto scalar = Atvoss::PlaceHolder<2, ScalarDtype, Atvoss::ParamUsage::IN>();
        auto out    = Atvoss::PlaceHolder<3, Tensor<TensorDtype>, Atvoss::ParamUsage::OUT>();
        return (out = in * scalar);
    };
};
```

对应源码与说明：

- [examples/muls/muls.cpp:32-36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L32-L36)：三个 `PlaceHolder` 分别声明输入张量、输入标量、输出张量。注意 `scalar` 的类型是 `ScalarDtype`（模板参数，本例为 `float`），**没有包一层 `Tensor<>`**，这正是它被识别为标量的原因。
- [examples/muls/muls.cpp:36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L36)：`out = in * scalar` 即公式 \(\text{out}_i = \text{in}_i \times s\)。

`operator*` 为何能吃下标量？看它的三个重载，第二、三个专门处理「一侧是裸标量」的情况：

- [include/operators/math_expression.h:87-91](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L87-L91)：`operator*(Expression<T> lhs, U&& rhs)`——右操作数 `U` 是标量时（如 `in * 3.0f`），直接用 `std::forward` 把标量塞进 `OpMul`，不要求它也是 `Expression`。

`OpMul` 本身不区分标量与张量，区分发生在**求值器**里：

- [include/operators/math_evaluator.h:355-383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L355-L383)：`Evaluator<OpAssign<T, OpMul<U,V>>>`。先用 `static_assert` 拒绝「两侧都是标量」（没有意义），再用 `if constexpr` 三分支：U 是标量 → `MulsAssign`；V 是标量 → `MulsAssign`；都不是标量 → `MulAssign`。
- [include/operators/math_evaluator.h:108-113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L108-L113)：`MulsAssign` 一行落到 `AscendC::Muls(dst, src, src1, count)`，这就是昇腾 Vector 单元真正的标量广播乘法指令。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「同一个 `*` 符号，标量与张量两种用法会落到不同指令」。

**步骤**：

1. 在 `math_evaluator.h` 第 366–381 行，对照三个 `if constexpr` 分支，分别写出它们调用的 `Assign` 函数名。
2. 在 `math_expression.h` 第 82–97 行，找出 `operator*` 的全部重载，标出哪个重载会被 `in * scalar`（右操作数标量）命中。

**需要观察的现象**：

- `in * scalar` 命中的是第 88 行那个重载（`U&& rhs`），生成的 `OpMul` 第二个模板参数是裸 `float`。
- 求值时由于 `std::is_scalar_v<float>` 为真，走 `MulsAssign`（标量指令），**不会**走 `MulAssign`。

**预期结果**：你能用一句话说清——「写 `*` 即可，标量侧自动得到 `Muls` 指令，无需手动区分」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MulsCompute` 里的 `scalar` 误写成 `PlaceHolder<2, Tensor<ScalarDtype>, IN>()`（多包了一层 `Tensor`），表达式 `in * scalar` 还能编译吗？运行时会走哪条指令？

**答案**：仍能编译，但 `scalar` 此时是张量而非标量。求值器里 `std::is_scalar_v` 对两侧都为假，于是从 `MulsAssign`（`AscendC::Muls`，标量广播）**降级**为 `MulAssign`（`AscendC::Mul`，逐元素张量乘）。功能可能仍对，但失去了标量指令的效率优势，且运行时还必须为这个标量分配一块完整的张量显存——所以「标量就该声明成标量」。

**练习 2**：`MulsCompute` 里三个 `PlaceHolder` 的序号是 1、2、3。它们和 `ArgumentsBuilder{}.inputOutput(in, scalar, out)` 里的参数顺序是什么关系？

**答案**：严格一一对应。`PlaceHolder<N>` 对应 `inputOutput(...)` 的第 N 个实参（1-based）。即 `in`→1、`scalar`→2、`out`→3。这是 u2-l6 讲过的「序号契约」，消费端用 `std::get<N-1>` 取参。

---

### 4.2 Cast + PlaceHolderTmpLike：MulsComputePromtIn 的类型转换

#### 4.2.1 概念说明

`MulsCompute` 假设「输入张量类型 == 计算类型 == 输出类型」。但当**输入是 int32、而我们想用 float 做乘法并输出 float** 时，直接 `in * scalar` 会有类型不匹配的问题：标量乘法指令要求参与计算的两侧类型一致。

ATVOSS 的解法是：在表达式里**先把 int32 输入 Cast 成 float 的临时变量**，再用这个临时变量去做乘法。这需要两件工具配合：

- `Cast<castMode, R>(in)`：声明一个「把 `in` 转成类型 `R`」的运算节点。
- `PlaceHolderTmpLike<N, T>(in)`：照着已有 Param `in`，派生出一个**同序号空间、可指定类型**的内部临时变量（`LocalVar`），用来承接 Cast 的结果。

#### 4.2.2 核心流程

类型转换版的执行链路（注意是**两步**，用逗号表达式串联）：

1. `inTmp = PlaceHolderTmpLike<1, Tensor<float>>(in)` —— 声明一个类型为 `Tensor<float>` 的临时变量，序号 1（LocalVar 空间）。
2. `inTmp = Cast<CAST_NONE, float>(in)` —— 第一步计算：把 int32 的 `in` 转成 float，结果写入 `inTmp`。求值器把它翻译成 `AscendC::Cast(..., RoundMode::CAST_NONE, ...)`。
3. `out = inTmp * scalar` —— 第二步计算：float 的 `inTmp` 乘 float 标量，写入 `out`（此时已是 4.1 讲过的标量乘法）。
4. 两步之间用逗号 `(第1步, 第2步)` 包成 `OpAndThen`，保证「先转、后乘」的顺序。

> 为何用 `CAST_NONE`？因为 int32→float 是** widen（宽化）**转换，精度只会增加、不会丢失，所以不需要任何舍入模式。如果是 float→int32，就必须指定 `CAST_FLOOR`/`CAST_ROUND` 等舍入方式。

#### 4.2.3 源码精读

```cpp
// examples/muls/muls.cpp
struct MulsComputePromtIn {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        auto in     = Atvoss::PlaceHolder<1, Tensor<TensorDtype>, Atvoss::ParamUsage::IN>();   // int32
        auto scalar = Atvoss::PlaceHolder<2, ScalarDtype, Atvoss::ParamUsage::IN>();            // float
        auto out    = Atvoss::PlaceHolder<3, Tensor<ScalarDtype>, Atvoss::ParamUsage::OUT>();   // float
        auto inTmp  = Atvoss::PlaceHolderTmpLike<1, Tensor<ScalarDtype>>(in);                   // float 临时量
        return (inTmp = Atvoss::Cast<Atvoss::CastMode::CAST_NONE, ScalarDtype>(in),
                out  = inTmp * scalar);
    };
};
```

对应源码与说明：

- [examples/muls/muls.cpp:44-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L44-L49)：四个声明 + 一个逗号表达式。`inTmp` 用 `PlaceHolderTmpLike<1, Tensor<float>>(in)` 派生，序号 1 属于 LocalVar 空间，与 `in` 的 Param 序号 1 不冲突。
- [examples/muls/muls.cpp:49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L49)：`(inTmp = Cast<...>(in), out = inTmp * scalar)`——逗号表达式 = `OpAndThen`，先 Cast 后 Muls。

支撑这两步的框架代码：

- [include/expression/expr_template.h:593-602](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L593-L602)：`PlaceHolderTmpLike` 的实现。它先 `static_assert(IsParam_v<L>)` 强制「只能照着 Param 派生」，再根据是否显式给定类型 `T`，决定复用源 Param 的类型还是用新类型——本例给了 `Tensor<ScalarDtype>`，所以得到一个 **float 类型**的 LocalVar。
- [include/operators/math_expression.h:171-192](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L171-L192)：`OpCast` 结构体与 `Cast` 工厂函数。`OpCast` 是带两个模板参数（`castMode` + 目标类型 `R`）的 `UnaryOp`，`Cast` 默认 `castMode = CAST_ROUND`，本例显式传了 `CAST_NONE`。
- [include/utils/patterns.h:22-31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L22-L31)：`CastMode` 的 7 个取值。`CAST_NONE` 用于无舍入（宽化）转换。
- [include/operators/math_evaluator.h:546-560](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L546-L560)：`Evaluator<OpAssign<T, OpCast<castMode, R, U>>>` 把 Cast 节点翻译成 `CastAssign`。
- [include/operators/math_evaluator.h:223-242](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L223-L242)：`CastAssign` 用一串 `if constexpr` 把 `CastMode` 映射到 `AscendC::RoundMode`，最终调用 `AscendC::Cast(dst, src, roundMode, count)`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解「为什么需要 LocalVar，以及 LocalVar 序号空间独立于 Param」。

**步骤**：

1. 读 [include/expression/expr_template.h:72-79](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L72-L79) 的 `LocalVar` 定义，确认它**没有** `ParamUsage` 字段（因为它是纯内部量，不参与 GM 搬运）。
2. 对照 `MulsComputePromtIn`：`in`（Param 序号 1）、`inTmp`（LocalVar 序号 1）。两者序号都是 1 却不冲突——因为框架在收集 Param 与 LocalVar 时走的是两条独立的列表（详见 u2-l2）。

**需要观察的现象**：

- `PlaceHolderTmpLike<1, ...>(in)` 的第一个模板参数 `1` 是 **LocalVar** 的序号，不是 Param 的序号。它和 `in` 的 Param 序号 `1` 恰好相同纯属巧合，框架不会混淆。
- `inTmp` 没有出现在 `ArgumentsBuilder{}.inputOutput(...)` 里——它是编译期临时量，运行时由框架自动在 UB 上分配缓冲，无需用户传参。

**预期结果**：你能解释「Cast 结果必须落到一个 LocalVar（而非直接覆盖原 Param `in`），因为 `in` 是 int32 而 Cast 后是 float，类型不同、缓冲也不同」。

#### 4.2.5 小练习与答案

**练习 1**：把 `MulsComputePromtIn` 里的 `Cast<CAST_NONE, float>(in)` 改成 `Cast<CAST_TRUNC, float>(in)`，对 int32→float 转换有没有影响？

**答案**：没有实质影响。int32→float 是宽化转换，所有值都能精确表示，无论指定哪种 `CastMode`（`CAST_TRUNC`/`CAST_ROUND`/...）结果都一样。`CAST_NONE` 只是最贴切的语义标注。但如果反过来做 float→int32，`CAST_TRUNC`（向零取整）与 `CAST_FLOOR`（向下取整）对负数会给出不同结果，那时舍入模式就至关重要。

**练习 2**：如果删掉 `inTmp`，直接写 `out = Cast<CAST_NONE, float>(in) * scalar`，逻辑上等价吗？为什么样例仍要引入 `inTmp`？

**答案**：从最终数值看等价（都是「先转 float 再乘标量」）。引入具名 `inTmp` 的好处是**可读性与可复用**：当同一次 Cast 的结果要在后续多步计算里重复使用时，落到一个 LocalVar 上能避免重复转换、也方便框架做缓冲复用分析（u3 系列会讲）。此外，显式 LocalVar 让中间类型一目了然，便于排查「哪一步类型变了」。

---

### 4.3 单 Config 多 Compute 与 if constexpr 按类型分派

#### 4.3.1 概念说明

到这里，`MulsConfig` 里已经躺着**两套** `Compute`：`MulsCompute`（纯同类型 float 路径）和 `MulsComputePromtIn`（int32→float 转换路径）。它们共享同一套 `TileShape`、`blockPolicy`、`kernelPolicy`、`ArchTag`，只是计算表达式不同。

这是 ATVOSS 的一个重要模式：**一个算子的「形状/调度策略」是稳定的，但「计算表达式」可能随输入数据类型而变**。于是把多套 `Compute` 都放进同一个 `Config`，各自组装出独立的 `BlockOp → KernelOp → DeviceOp` 类型别名，再用 `if constexpr` 在运行时按实际数据类型挑一个执行。

#### 4.3.2 核心流程

多 Compute 分派的组装与选择流程：

1. **声明两套 Compute**：`MulsCompute`、`MulsComputePromtIn`（都在 `MulsConfig` 内）。
2. **各自组装三级 Builder**：每个 Compute 套上 `BlockBuilder → KernelBuilder → DeviceAdapter`，得到 `DeviceOp` 与 `DeviceOpPromtIn` 两个独立类型别名。
3. **运行时挑选**：在 `Run<TensorDtype, ScalarDtype>` 中用 `if constexpr (std::is_same_v<TensorDtype, float>)` 选 `DeviceOp`，`else if constexpr (... int32_t)` 选 `DeviceOpPromtIn`。
4. **`if constexpr` 的关键性质**：它不只是「运行时分支」，而是**编译期分支**——未被选中的分支**不会被实例化**。这意味着不会因为「float 专属的 `MulsCompute` 拿到 int32 输入」而产生类型错误。

> 一个容易忽略的事实：本样例的 `main()` 只调用了 `Run<int32_t, float>(options)`（见下文源码）。因此**实际只有 `DeviceOpPromtIn`（int32→float 路径）会真正执行**；`DeviceOp`（float 路径）虽被定义、也会随 `Run` 模板实例化而参与编译，但当前样例不触发它。要触发 float 路径，需要再调用一次 `Run<float, float>`。

#### 4.3.3 源码精读

两套 Compute 各自的 Builder 组装：

```cpp
// examples/muls/muls.cpp（节选自 MulsConfig）
using BlockOp = Atvoss::Ele::BlockBuilder<
    MulsCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig, Atvoss::Ele::DefaultBlockSchedule>;
using BlockOpPromtIn = Atvoss::Ele::BlockBuilder<
    MulsComputePromtIn, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig, Atvoss::Ele::DefaultBlockSchedule>;
using KernelOp       = Atvoss::Ele::KernelBuilder<BlockOp, kernelPolicy, ...>;
using KernelOpPromtIn = Atvoss::Ele::KernelBuilder<BlockOpPromtIn, kernelPolicy, ...>;
using DeviceOp        = Atvoss::DeviceAdapter<KernelOp>;
using DeviceOpPromtIn = Atvoss::DeviceAdapter<KernelOpPromtIn>;
```

- [examples/muls/muls.cpp:56-68](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L56-L68)：两套 Builder 平行组装。注意两套 `BlockOp` 共用同一个 `blockPolicy`/`kernelPolicy`/`ArchTag`，唯一区别是第一个模板参数（`Compute` 结构体）。

运行时的类型分派：

```cpp
// examples/muls/muls.cpp（Run 中 Step 8）
if constexpr (std::is_same_v<TensorDtype, float>) {
    using DeviceOp = typename MulsConfig<TensorDtype, ScalarDtype>::DeviceOp;
    DeviceOp deviceOp; deviceOp.Run(arguments, stream);
} else if constexpr (std::is_same_v<TensorDtype, int32_t>) {
    using DeviceOp = typename MulsConfig<TensorDtype, ScalarDtype>::DeviceOpPromtIn;
    DeviceOp deviceOp; deviceOp.Run(arguments, stream);
}
```

- [examples/muls/muls.cpp:182-190](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L182-L190)：`if constexpr` 按输入类型挑 `DeviceOp`。由于 `if constexpr` 在模板内会丢弃未命中分支，当 `Run<int32_t, float>` 实例化时，float 分支（`MulsCompute`）整段不参与实例化——避免了「int32 输入喂给 float 专属 Compute」的类型矛盾。

最后，确认输入实参与 PlaceHolder 的对齐（呼应 4.1.5 练习 2）：

- [examples/muls/muls.cpp:176-179](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L176-L179)：`in`（int32 张量）、`scalar`（float）、`out`（float 张量）按序传给 `ArgumentsBuilder{}.inputOutput(in, scalar, out)`，与两套 Compute 里 `PlaceHolder<1/2/3>` 的序号一一对应。无论走哪套 Compute，入参顺序契约都不变。

`main` 的实际调用（决定哪条路径真跑）：

- [examples/muls/muls.cpp:217-218](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L217-L218)：`std::cout << "Start muls int32_t and float"; Run<int32_t, float>(options);` —— 只跑了 int32→float 的 `PromtIn` 路径。

#### 4.3.4 代码实践（修改 + 观察型）

**目标**：亲手加第三套 Compute，把「单 Config 多 Compute + if constexpr」这套模式用熟。

**背景**：当前 `MulsConfig` 有两套 Compute——`MulsCompute`（float 同类型）与 `MulsComputePromtIn`（int32→float）。现在要新增 `MulsComputeHalf`：**输入仍是 float，但输出固定为 half（fp16）**，即先把 float 输入 Cast 成 half，再与标量相乘，输出 half。

**操作步骤**（示例代码，仅供参照思路；完整编译结果待本地验证，因为 `half` 类型别名来自 Ascend C SDK `kernel_operator.h`，需确认本机 CANN 的确切写法）：

```cpp
// 示例代码：新增到 MulsConfig 内
struct MulsComputeHalf {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        auto in     = Atvoss::PlaceHolder<1, Tensor<float>,           Atvoss::ParamUsage::IN>();
        auto scalar = Atvoss::PlaceHolder<2, float,                   Atvoss::ParamUsage::IN>();
        auto out    = Atvoss::PlaceHolder<3, Tensor<half>,            Atvoss::ParamUsage::OUT>();
        auto inTmp  = Atvoss::PlaceHolderTmpLike<1, Tensor<half>>(in);   // float -> half 的临时量
        return (inTmp = Atvoss::Cast<Atvoss::CastMode::CAST_NONE, half>(in),
                out   = inTmp * scalar);
    }
};

// 对应的三级 Builder（与现有两套平行）
using BlockOpHalf   = Atvoss::Ele::BlockBuilder<
    MulsComputeHalf, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig, Atvoss::Ele::DefaultBlockSchedule>;
using KernelOpHalf  = Atvoss::Ele::KernelBuilder<BlockOpHalf, kernelPolicy, Atvoss::Ele::DefaultKernelConfig, Atvoss::Ele::DefaultKernelSchedule>;
using DeviceOpHalf  = Atvoss::DeviceAdapter<KernelOpHalf>;
```

随后在 `Run` 中加第三个分支，并新增一次 `Run<float, half>` 调用来真正触发它：

```cpp
// 示例代码：Run 中 Step 8 追加分支
else if constexpr (std::is_same_v<ScalarDtype, half>) {
    using DeviceOp = typename MulsConfig<TensorDtype, ScalarDtype>::DeviceOpHalf;
    DeviceOp deviceOp; deviceOp.Run(arguments, stream);
}
```

**需要观察的现象**：

1. 三套 Compute 的表达式差异应能这样概括：
   - `MulsCompute`：`out = in * scalar`（全程 float，无 Cast）。
   - `MulsComputePromtIn`：`inTmp = Cast(int32→float); out = inTmp * scalar`（输入侧一次宽化 Cast）。
   - `MulsComputeHalf`：`inTmp = Cast(float→half); out = inTmp * scalar`（输入侧一次窄化 Cast，输出 half）。
2. 三者共用同一套 `PlaceHolder<1/2/3>` 序号与同一套 `blockPolicy`/`kernelPolicy`，区别只在 `Compute()` 的 return 表达式与各自的 Builder 类型别名。

**预期结果 / 待本地验证**：

- 编译是否通过取决于本机 CANN 是否提供 `half`（或 `__fp16`）类型别名，以及 `AscendC::Muls` 是否支持 half 目标类型——这些需在本地 `bash scripts/build.sh -DSOC=ascend950 muls` 后确认。
- 若能跑通，由于 golden 值 `9.0` 可被 half 精确表示，精度校验仍应 `passed`；但若把输入改成 `3.1` 这类 half 无法精确表示的值，则会观察到精度误差，这正是 half 路径与 float 路径的差异所在。

> 如果你暂无昇腾环境，本实践可降级为「源码阅读型」：只写出 `MulsComputeHalf` 的表达式与 Builder 别名，并口头说明它会命中哪个求值器特化（`Evaluator<OpAssign<T, OpCast<...>>>` → `AscendC::Cast`，再 `Evaluator<OpAssign<out, OpMul<inTmp, scalar>>>` → `AscendC::Muls`），不必实际编译。

#### 4.3.5 小练习与答案

**练习 1**：为什么必须用 `if constexpr` 而不是普通 `if`？如果把 `if constexpr` 换成运行时 `if`，会出什么问题？

**答案**：普通 `if` 的两个分支都会被**实例化和类型检查**。当 `Run<int32_t, float>` 实例化时，普通 `if` 的 float 分支里 `MulsConfig<...>::DeviceOp`（基于 `MulsCompute`，假设输入是 float）也会被编译；而 `MulsCompute` 的表达式与 int32 输入类型不匹配，会导致编译失败或非预期类型推导。`if constexpr` 则在编译期丢弃未命中分支，保证「每个类型实例化时只看到与之匹配的那套 Compute」。

**练习 2**：本样例 `main` 只调了 `Run<int32_t, float>`，那么 `DeviceOp`（float 路径）到底有没有被编译进二进制？

**答案**：取决于编译器是否实例化 `Run<float, ...>`。由于 `main` 从未调用 `Run<float, ...>`，该模板特化不会被实例化，因此 `MulsCompute`/`DeviceOp`（float 路径）实际上**不会**进入最终二进制——它们目前只是「写好备用」的死代码。要让 float 路径生效，需要在 `main` 里追加一次 `Run<float, float>(options)` 调用。

**练习 3**：`MulsComputePromtIn` 用了 `out = PlaceHolder<3, Tensor<ScalarDtype>, OUT>()`，而 `MulsCompute` 用的是 `Tensor<TensorDtype>`。在 `Run<int32_t, float>` 场景下，两者的输出类型分别是什么？

**答案**：`ScalarDtype = float`、`TensorDtype = int32_t`。故 `MulsComputePromtIn` 的 `out` 是 `Tensor<float>`（输出 float），而 `MulsCompute` 的 `out` 会是 `Tensor<int32_t>`（输出 int32）——这也正是 int32 输入不能直接用 `MulsCompute` 的另一个原因：它的输出类型会是 int32，与「想要 float 输出」不符。

## 5. 综合实践

把本讲三个要点串起来，完成一次「带类型转换的标量算子」端到端阅读。

**任务**：以 `MulsComputePromtIn` 为对象，画出它从**用户表达式**到**Ascend C 指令**的完整翻译链，并标注每一步的源码位置。

**要求**：

1. 写出 `(inTmp = Cast<CAST_NONE, float>(in), out = inTmp * scalar)` 这条逗号表达式展开后的**两步操作**。
2. 第一步 `inTmp = Cast<...>(in)`：指出它匹配 [include/operators/math_evaluator.h:546-560](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L546-L560) 的哪个求值器特化，最终落到哪条 Ascend C 指令、用的哪个 `RoundMode`。
3. 第二步 `out = inTmp * scalar`：指出它匹配 [include/operators/math_evaluator.h:355-383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L355-L383) 的哪条 `if constexpr` 分支，为何走 `MulsAssign` 而非 `MulAssign`。
4. 说明 `inTmp` 作为 LocalVar，其缓冲是在哪一层被分配的（提示：回顾 u2-l9 的 UB 三段划分与 Block 层 `Process` 循环；深入分配细节见 u3-l2/u3-l5）。

**参考答案要点**：

- 两步操作：① `OpAssign<inTmp, OpCast<CAST_NONE, float, in>>`；② `OpAssign<out, OpMul<inTmp, scalar>>`，由 `OpAndThen` 串联（保证先 Cast 后 Muls）。
- 第一步 → `Evaluator<OpAssign<T, OpCast<CAST_NONE, float, U>>>` → `CastAssign` → `AscendC::Cast(dst, src, RoundMode::CAST_NONE, count)`。
- 第二步 → `Evaluator<OpAssign<T, OpMul<U,V>>>` 中 `std::is_scalar_v<V::Type>`（scalar 是 float 标量）为真 → `MulsAssign` → `AscendC::Muls(dst, src, src1, count)`。
- `inTmp` 是 LocalVar，框架在编译期沿表达式树收集到 `LocalVars` 列表，运行时在 Block 层的 UB 上按 LocalVar 序号分配缓冲（位于 UB 的「计算/CALC」区，详见 u2-l9/u3-l5），用户无需在 `ArgumentsBuilder` 中传它。

## 6. 本讲小结

- **标量即标量**：标量操作数直接用 `PlaceHolder<N, 标量类型, IN>()` 声明（不包 `Tensor<>`），`operator*` 的标量重载 + 求值器的 `is_scalar_v` 分派会自动落到高效的 `AscendC::Muls`（标量广播）指令。
- **Cast + LocalVar 解类型不匹配**：当输入类型与计算类型不一致（如 int32→float），用 `Cast<castMode, R>(in)` 声明转换、用 `PlaceHolderTmpLike<N, T>(in)` 接住结果，二者用逗号表达式 `OpAndThen` 串联成「先转后算」。
- **LocalVar 与 Param 序号独立**：`inTmp` 用 `PlaceHolderTmpLike<1,...>` 产生的序号 1 属于 LocalVar 空间，与 Param `in` 的序号 1 互不干扰，且 LocalVar 不进 `ArgumentsBuilder`、缓冲由框架自动分配。
- **单 Config 多 Compute**：一个 `Config` 可容纳多套 `Compute`，各自组装出独立的 `DeviceOp` 类型别名，共享同一套 `TileShape`/调度策略。
- **`if constexpr` 是编译期分派**：按输入数据类型挑选 `DeviceOp`，未命中分支不实例化，从根上避免「类型不匹配的 Compute 被编译」。
- **样例现状**：`main` 当前只跑 `Run<int32_t, float>`，真正执行的是 `DeviceOpPromtIn`（int32→float 路径）；float 同类型路径已写好但未被触发。

## 7. 下一步学习建议

本讲把「表达式声明」与「求值器分派」在样例层面打通，但求值器内部「如何沿表达式树递归、如何与 PIPE 同步、如何分配缓冲」尚未展开。建议接下来：

1. **u3-l1 求值器系统**：深入 `eval_base.h`，理解 `Evaluator<T>` 主模板与对 `Param`/`LocalVar`/`OpAssign`/`OpAndThen` 的特化如何递归求值——这是本讲反复引用的「求值器特化」的真正实现。
2. **u3-l2 Tile 层 Assign**：看 `OpCopyIn`/`OpCopyOut`/`OpAlloc`/`OpFree` 如何在 Tile 层与 `AscendC::DataCopyPad`、缓冲池、`PIPE_MTE2/MTE3/V` 同步协作，理解 `inTmp` 这类 LocalVar 的缓冲是何时 Alloc/Free 的。
3. **u3-l4 表达式线性化与图优化 Pass**：本讲的逗号表达式 `(A, B)` 在真正执行前还会被「线性化」「Cast 消除」「Alloc/Free 插入」等 Pass 处理，理解框架如何把用户写的两步表达式整理成最优指令序列。

若想马上动手，可回到 4.3.4 的实践，在本地环境尝试编译 `MulsComputeHalf`，验证 half 路径的精度行为。
