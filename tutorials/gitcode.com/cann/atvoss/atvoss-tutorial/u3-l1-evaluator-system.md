# 求值器系统：从表达式到执行

## 1. 本讲目标

在 [u2-l1](u2-l1-expression-template.md) 我们学到：用户在 `Compute()` 里写的 `return (out = Sqrt(in))` 并不是一段运行时计算的代码，而是一棵**编译期表达式树**，整棵树的形状被编码进 C++ 的嵌套类型里（`Expression<OpAssign<...>>`），对象本身几乎不占空间。

那么问题来了：**这棵「类型即结构」的静态表达式树，在 NPU 的 AI Core 上是如何真正变成一条条 Ascend C 计算指令的？** 桥梁就是本讲的主角——**求值器系统（Evaluator）**。

学完本讲，你应当能够：

1. 理解 `Evaluator<T>` 用「主模板 + 偏特化」驱动的**递归求值模型**——为什么对表达式树的根节点调用一次 `Evaluator`，就能逐层向下递归、最终落到真实硬件指令。
2. 掌握叶子节点 `Param` / `LocalVar` 求值时，是如何带着序号 `N` 去 `ContextData` 里把对应的 `LocalTensor`「取出来」的。
3. 理清 `OpAssign`（赋值）、`OpAndThen`（顺序串联）两种结构节点在求值时的差别，以及 `OpAndThen` 里那行 `PipeBarrier<PIPE_V>()` 到底在同步什么。
4. 看懂 `math_evaluator.h` / `transcendental_evaluator.h` 如何为每个 `OpXxx` 提供「表达式 ↔ 指令」的特化配对。

本讲是整个专家篇的「地基」：[u3-l2](u3-l2-tile-layer-assign.md) 的 Tile 层映射、[u3-l4](u3-l4-linearizer-and-passes.md) 的线性化 Pass，都建立在「求值器如何把表达式翻译成指令」这一机制之上。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（均在前面讲义中建立）：

- **表达式模板 / 编译期 AST**：`Expression<T>` 是统一外壳，`T` 是叶子（`Param`/`LocalVar`）或运算节点（`UnaryOp`/`BinaryOp` 派生）。详见 [u2-l1](u2-l1-expression-template.md)。
- **Param 与 LocalVar**：两种叶子节点，各自有从 1 起的独立连续序号空间 `N`。`Param` 对应外部入参/出参（带 `ParamUsage`），`LocalVar` 是内部临时变量。详见 [u2-l2](u2-l2-placeholder-and-param.md)。
- **ContextData**：单核单 Tile 的上下文包裹，求值器要从这里取出张量、缓冲池和偏移信息。详见 [u2-l5](u2-l5-data-model.md)。
- **五层架构**：求值器属于最底两层 Tile/Basic 的核心机制。详见 [u1-l3](u1-l3-five-layer-architecture.md)。

如果你还不熟悉 C++ 模板偏特化，先记住一个直觉：**主模板是「默认行为」，偏特化是「针对某种具体类型的特殊处理」**。编译器看到一个 `Evaluator<X>`，会挑出「与 `X` 匹配得最具体的那个特化」来用。本讲的核心，就是 ATVOSS 为不同表达式节点准备了一整套偏特化。

补充几个本讲会用到的硬件术语：

- **PIPE（流水线）**：昇腾 AI Core 内部把数据搬运和计算拆成多条独立的硬件流水线，如 `PIPE_MTE2`（GM→UB 搬运）、`PIPE_V`（Vector 向量计算）、`PIPE_MTE3`（UB→GM 搬运）。它们可以并行推进。
- **PipeBarrier**：一条「等待某条流水线排空」的同步指令。`PipeBarrier<PIPE_V>()` 表示「等到 Vector 流水线上所有在飞指令都执行完，再往下走」。
- **LocalTensor / UB**：AI Core 片上高速缓存（Unified Buffer）里的张量，Vector 指令直接对它操作。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/evaluator/eval_base.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h) | 求值器「骨架」：`Evaluator<T>` 主模板，以及对 `Expression`、`Param`、`LocalVar`、`OpAssign`、`OpAndThen` 的偏特化，外加 `Assign` 工具函数。**本讲的核心文件。** |
| [include/operators/math_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h) | 数学算子求值器：为 `Add/Sub/Mul/Div/Exp/Abs/Sqrt/Power/Divs/Cast` 提供 `Evaluator<OpAssign<dst, OpXxx<...>>>` 特化，并定义薄包装 `XxxAssign` 调用 Ascend C 指令。 |
| [include/operators/transcendental_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h) | 归约/广播算子求值器：为 `ReduceSum`、`Broadcast` 提供特化，映射到 Ascend C 的 `ReduceSum` / `Broadcast` API。 |
| [include/common/type_def.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h) | 定义 `ContextData`——求值器取张量的「数据源」。 |
| [include/elewise/tile/tile_evaluate.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h) | Tile 层执行入口 `Evaluate<Expr>(context)`，正是它实例化了根节点的 `Evaluator`。 |
| [include/expression/expr_template.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h) | 表达式树节点定义（`Expression`/`Param`/`LocalVar`/`UnaryOp`/`BinaryOp`/`OpAssign`/`OpAndThen`）。求值器遍历的就是这棵树。 |

一句话总览：**`tile_evaluate.h` 点火 → `eval_base.h` 提供骨架与叶子/结构节点的通用求值 → `math_evaluator.h` / `transcendental_evaluator.h` 为每个具体算子补上「翻译成 Ascend C 指令」的特化**。

---

## 4. 核心概念与源码讲解

### 4.1 Evaluator\<T\> 主模板与递归求值骨架

#### 4.1.1 概念说明

回到最根本的问题：表达式树是「编译期类型」，硬件指令是「运行期动作」，二者之间怎么连起来？

ATVOSS 的答案是经典的 **CRTP 式模板特化分派**：定义一个主模板 `Evaluator<T>`，它本身不做事；然后为每一种我们关心的表达式节点类型 `X` 写一个偏特化 `Evaluator<X>`，在其中实现「这个节点该怎么求值」。当外部对根节点调用 `Evaluator<Root>{}(expr, context)` 时，编译器会自动选中匹配得最具体的那个特化。

关键在于：**运算节点的特化内部，会对自己的子表达式再次构造 `Evaluator<子节点>{}(...)`**。这就是「递归」——和你在数据结构课上写的「递归遍历一棵树」一模一样，只不过这棵树的形状写在类型里，递归展开发生在编译期、内联成一条直线，运行时零函数调用开销。

> 直觉：求值器就是一台「编译期解释器」。表达式树是程序，`Evaluator` 的各个偏特化是解释规则，`ContextData` 是运行环境（寄存器/内存）。

#### 4.1.2 核心流程

一次求值的整体流程：

```text
Tile::Evaluate<Expr>(context)                     # tile_evaluate.h 入口
   └─> Evaluator<Expr>{}(Expr{}, context)         # 对根节点构造求值器并调用
         └─> 选中 Expr 对应的偏特化
               └─> 特化内部：Evaluator<子节点>{}(...)  # 递归向下
                     └─> ... 直到叶子（Param/LocalVar）
                           └─> 从 ContextData 取出 LocalTensor
               └─> 用取到的 LocalTensor 调用一条 Ascend C 指令
```

伪代码描述主模板：

```cpp
// 主模板：默认行为是「原样返回」（给标量等平凡类型兜底）
template <typename T>
struct Evaluator {
    template <typename Context>
    decltype(auto) operator()(const T& value, Context& /*context*/) const {
        return value;   // 不关心 context，直接把值吐回去
    }
};
```

#### 4.1.3 源码精读

主模板定义在 [include/evaluator/eval_base.h:L22-L32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L22-L32)，它声明了 `using Type = T;` 并提供一个 `operator()`，默认只把传入的 `value` 原样返回。这是给「非表达式类型」（如裸标量）兜底的落点。

紧跟着是一条非常关键的「脱壳」特化 [include/evaluator/eval_base.h:L34-L36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L34-L36)：

```cpp
// Treat Evaluator<Expression<T>> as Evaluator<T>
template <typename T>
struct Evaluator<Expression<T>> : Evaluator<T> {};
```

它的作用是：**剥掉最外层的 `Expression<>` 外壳**。回忆 [u2-l1](u2-l1-expression-template.md)，`Expression<T>` 只在最外层包一次。求值器并不关心这层外壳，于是用「公开继承 `Evaluator<T>`」的方式，让 `Evaluator<Expression<T>>` 直接复用内部 `T` 的求值逻辑。这样后续所有特化都只需针对「裸节点类型」编写，不必同时写一份带外壳的版本。

真正点火的地方在 [include/elewise/tile/tile_evaluate.h:L33-L37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L33-L37)：

```cpp
template <typename Expr, typename Context>
__aicore__ inline void Evaluate(Context& context)
{
    Atvoss::Tile::Evaluator<Expr>{}(Expr{}, context);
}
```

这就是 Tile 层的执行入口：拿到表达式类型 `Expr` 和运行上下文 `context`，实例化一个临时求值器 `Evaluator<Expr>{}`，传入一个默认构造的 `Expr{}`（表达式对象是空壳，不需要数据）和 `context`，递归求值就此开始。注意 `Expr` 实际上是已经过线性化（[u3-l4](u3-l4-linearizer-and-passes.md)）后的扁平表达式，根节点通常是一个 `OpAssign` 或 `OpAndThen`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「主模板 + 脱壳特化」的分派关系。

**操作步骤**：

1. 打开 [include/evaluator/eval_base.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h)，确认主模板（L22–L32）与脱壳特化（L34–L36）的相对位置：脱壳特化必须出现在主模板**之后**。
2. 想象编译器看到 `Evaluator<Expression<OpSqrt<Param<1,...>>>>{}`，按下面的优先级链判断它最终选中哪个特化：
   - 先命中 `Evaluator<Expression<T>>`（脱壳），等价于 `Evaluator<OpSqrt<Param<1,...>>>`；
   - 由于本仓库并未给「裸 `OpSqrt`」单独写偏特化（只有 `OpAssign<dst, OpSqrt<U>>` 形式，见 4.4），它会落到主模板 `Evaluator<T>` 的兜底 `operator()`。
3. **预期现象**：这说明「孤立的 `OpSqrt` 子表达式」并不会被求值器直接执行——真正会被执行的，永远是 `dst = OpSqrt(src)` 这种 `OpAssign` 包裹的完整语句（见 4.3、4.4）。

**待本地验证**：上述结论是源码阅读型推断；如果你想在真机/仿真上确认，可在算子 `Compute()` 里故意写 `return Sqrt(in);`（缺 `out =`）并编译，观察框架在图构建阶段是否报错或把它当作无意义表达式处理。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Evaluator<Expression<T>>` 用「公有继承 `Evaluator<T>`」而不是重新实现一遍 `operator()`？

**参考答案**：公有继承让派生类「白嫝」基类的全部成员（包括 `operator()` 和 `using Type`），无需复制代码；同时 `Expression<T>` 与内部 `T` 的求值语义本就一致（都是「剥壳后按 `T` 处理」），用继承复用最自然。这也是 EBO（空基类优化）友好的写法。

**练习 2**：如果用户表达式树的最外层不是 `Expression<>` 包裹（理论上），脱壳特化还会生效吗？

**参考答案**：不会。脱壳特化 `Evaluator<Expression<T>>` 只在参数类型形如 `Expression<T>` 时才匹配。`tile_evaluate.h` 里传入的 `Expr` 是脱壳后的裸类型（如 `OpAssign<...>` 或 `OpAndThen<...>`），所以它直接命中对应的节点特化，根本走不到脱壳那一步。脱壳特化主要为「递归过程中可能遇到的子表达式外壳」兜底。

---

### 4.2 叶子节点求值：Param 与 LocalVar 如何从 ContextData 取张量

#### 4.2.1 概念说明

递归终将到达叶子。叶子的「求值」没有子表达式可递归，它的任务很朴素：**「我代表第 N 号张量，请把那块真实的 `LocalTensor` 交给我。」**

这块真实的张量从哪里来？从 `ContextData`。回顾 [u2-l5](u2-l5-data-model.md)，`ContextData` 是单核单 Tile 的上下文包裹，里面装着：

- `argsTensors`：本核本 Tile 的入参/出参张量（按 `Param` 序号排列）；
- `tmpTensors`：临时变量张量（按 `LocalVar` 序号排列）；
- `bufPools`：缓冲池；
- `gmOffset`、`elementNum`、`pingPong`：本 Tile 在 GM 中的偏移、元素数、双缓冲位号。

所以叶子求值本质上是「用序号 `N` 当下标，去 `ContextData` 的对应 tuple 里 `get<N-1>`」。`Param` 与 `LocalVar` 两套独立序号空间（详见 [u2-l2](u2-l2-placeholder-and-param.md)），正好对应 `ContextData` 里两个独立的 tuple。

#### 4.2.2 核心流程

```text
Evaluator<Param<N,...>>{}(param, ctx)
   └─> index = N - 1                        # 序号从 1 起，下标从 0 起
   └─> 检查 ctx.argsTensors[index] 的类型
         ├─ 类型一致  ─> 直接 get<index>(ctx.argsTensors)
         └─ 类型不一致 ─> 仅 IN 参数允许：static_cast<T>(get<index>(...))

Evaluator<LocalVar<N,...>>{}(lv, ctx)
   └─> get<N-1>(ctx.tmpTensors)             # 无类型转换分支
```

一个细节：`Param` 允许「隐式类型转换」，但**只对输入参数（`ParamUsage::IN`）放开**；输出/原地参数若类型不一致会直接 `static_assert` 报错。这是为了防止「把结果写进一个类型不匹配的输出张量」这种隐蔽 bug。

#### 4.2.3 源码精读

`LocalVar` 的特化在 [include/evaluator/eval_base.h:L38-L49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L38-L49)：

```cpp
template <std::size_t N, typename T, typename L>
struct Evaluator<LocalVar<N, T, L>> {
    using Type = T;
    template <typename Context>
    __aicore__ inline decltype(auto) operator()(LocalVar<N, T, L>, Context& context) const {
        static_assert(N > 0, "... LocalVar number starts from 1");
        return AscendC::Std::get<N - 1>(context.tmpTensors);
    }
};
```

注意几点：

- 形参 `LocalVar<N,T,L>` 没有命名（标 `/*unused*/`），再次印证叶子节点是「空壳」，求值只认编译期序号 `N`，不读运行时数据。
- `AscendC::Std::get` 是昇腾侧的 `std::get` 等价物（在 NPU kernel 里不能用标准库），用 `N-1` 从 `context.tmpTensors` 这个 tuple 里取出第 N 号临时张量。

`Param` 的特化在 [include/evaluator/eval_base.h:L51-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L51-L71)，比 `LocalVar` 多了一段类型检查与转换逻辑：

```cpp
template <std::size_t N, typename T, ParamUsage U, std::size_t R>
struct Evaluator<Param<N, T, U, R>> {
    using Type = T;
    template <typename Context>
    __aicore__ inline decltype(auto) operator()(Param<N, T, U, R>, Context& context) const {
        static_assert(N > 0, "... Param number starts from 1");
        constexpr auto index = N - 1;
        using ArgsTensors = decltype(context.argsTensors);
        using NthType = typename AscendC::Std::tuple_element<index, std::remove_reference_t<ArgsTensors>>::type;
        if constexpr (std::is_same_v<T, NthType> || /* T& / T&& 三种 */) {
            return AscendC::Std::get<index>(context.argsTensors);
        } else {
            static_assert(U == ParamUsage::IN, "... Only in-parameters allow implicit type conversions");
            return static_cast<T>(AscendC::Std::get<index>(context.argsTensors));
        }
    }
};
```

读法：

- `index = N - 1`：与 `LocalVar` 一致的「1-based 序号 → 0-based 下标」换算，呼应 [u2-l6](u2-l6-arguments-builder.md) 里 `PlaceHolder<N>` 与 `ArgumentsBuilder` 入参顺序的契约。
- 先用 `tuple_element` 取出 `argsTensors` 第 `index` 位的真实类型 `NthType`，再用 `if constexpr` 判断它和声明类型 `T` 是否一致：一致就直接返回该张量；不一致则要求 `U == IN`，否则编译期报错。
- 这里的 `argsTensors` 由 Block 层在构造 `ContextData` 时填好（详见 [u2-l9](u2-l9-block-layer.md) 的 `Process` 循环），承载的是已经 `CopyIn` 到 UB 的 `BlockTensor`。

`ContextData` 本身定义在 [include/common/type_def.h:L15-L25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L15-L25)，可以对照看到 `argsTensors` / `tmpTensors` / `bufPools` / `gmOffset` / `elementNum` / `pingPong` 六个字段的排布。

#### 4.2.4 代码实践

**实践目标**：理解「序号 → tuple 下标」的映射与类型转换约束。

**操作步骤**：

1. 假设某算子声明了 3 个 `Param`：`in1=PlaceHolder<1,...>`、`in2=PlaceHolder<2,...>`、`out=PlaceHolder<3,...,OUT>`，且 `Compute()` 里没有任何 `LocalVar`。
2. 在 [include/evaluator/eval_base.h:L51-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L51-L71) 手工代入：求值 `out`（N=3）时，`index = 2`，会去 `context.argsTensors` 取第 3 个张量。
3. 思考：如果 `out` 声明的类型 `T` 与 `argsTensors` 里第 3 位的真实类型不一致，由于 `out` 的 `usage = OUT`，会触发 `static_assert(U == ParamUsage::IN, ...)` 编译失败。
4. **预期结果**：你应当能解释「为什么输出参数不允许隐式类型转换，而输入参数允许」——因为输出要被写入，类型不匹配会导致写穿；输入只是读取参与计算，转换是安全的。

**待本地验证**：可构造一个 `out` 类型与实际不符的 Config，编译时应当看到 `Only in-parameters allow implicit type conversions` 这条断言。

#### 4.2.5 小练习与答案

**练习 1**：`Param` 序号从 1 开始，但取 tuple 用 `N-1`。为什么不直接让序号从 0 开始？

**参考答案**：为了与用户编程模型对齐——`PlaceHolder<1>`、`PlaceHolder<2>` 对应 `ArgumentsBuilder` 的第 1、第 2 个实参，1-based 对人类更直觉；tuple 下标是 0-based 的 C++ 约定，二者之间用 `N-1` 桥接（[u2-l6](u2-l6-arguments-builder.md) 也强调过这条契约）。

**练习 2**：`LocalVar` 的特化里没有类型转换分支，这背后的设计意图是什么？

**参考答案**：`LocalVar` 是框架自动分配的内部临时量（缓冲在 UB，详见 [u3-l5](u3-l5-buffer-double-buffering.md)），它的类型在编译期由 `PlaceHolderTmpLike` 派生时就与用途严格一致，运行时不存在「类型不匹配」的真实场景，所以不需要、也不应提供隐式转换——若出现不匹配，那一定是框架自身的 bug，应当让它自然编译失败暴露出来。

---

### 4.3 OpAssign 与 OpAndThen：递归求值与 PipeBarrier 同步

#### 4.3.1 概念说明

叶子节点负责「取张量」，结构节点负责「组织计算」。

- **`OpAssign<T, U>`**：赋值语句 `dst = src`。`T` 是左值（`Param`/`LocalVar`），`U` 是右值表达式。求值时分别求出两侧，再把结果「赋」给左侧。
- **`OpAndThen<T, U>`**：顺序串联，由逗号运算符 `(a, b)` 产生（详见 [u2-l1](u2-l1-expression-template.md)）。它表示「先做 a，再做 b」，`Type` 取右子树 `U` 的类型。

二者都是 `BinaryOp` 派生（见 [include/expression/expr_template.h:L406-L434](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L406-L434)），都用 `GetLhs()`/`GetRhs()` 暴露左右子树。但它们的求值语义截然不同：`OpAssign` 是「一条计算指令」，`OpAndThen` 是「两条指令之间插一道同步」。

#### 4.3.2 核心流程

`OpAssign` 的通用求值（[eval_base.h:L73-L82](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L73-L82)）：

```text
Evaluator<OpAssign<T,U>>{}(op, ctx)
   └─> Assign( Evaluator<T>{}(op.GetLhs(), ctx),    # 求左值（取出 dst 张量）
               Evaluator<U>{}(op.GetRhs(), ctx) )    # 求右值（取出 src 或递归计算）
```

`OpAndThen` 的求值（[eval_base.h:L84-L96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L84-L96)）：

```text
Evaluator<OpAndThen<T,U>>{}(op, ctx)
   └─> PipeBarrier<PIPE_V>()                         # 先等 Vector 流水线排空
   └─> ( Evaluator<T>{}(op.GetLhs(), ctx),           # 求左语句
         Evaluator<U>{}(op.GetRhs(), ctx) )          # 再求右语句（逗号是语言内置顺序点）
```

#### 4.3.3 源码精读

先看 `OpAssign` 的通用特化 [include/evaluator/eval_base.h:L73-L82](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L73-L82)：

```cpp
template <typename T, typename U>
struct Evaluator<OpAssign<T, U>> {
    using Type = void;
    template <typename Context>
    __aicore__ inline void operator()(const OpAssign<T, U>& op, Context& context) const {
        return Assign(Evaluator<T>{}(op.GetLhs(), context), Evaluator<U>{}(op.GetRhs(), context));
    }
};
```

注意：**这是「通用」版本，但大多数算子并不会走到这里**。因为 `math_evaluator.h` / `transcendental_evaluator.h` 为 `OpAssign<T, OpXxx<...>>` 提供了**更具体的偏特化**（例如 `Evaluator<OpAssign<T, OpSqrt<U>>>`），编译器会优先选中它们。通用版本主要服务于「右值是纯变量搬运」之类没有专门算子特化的场景，此时它调用 `Assign(dst, src)` 做一次普通赋值。

`Assign` 有两个重载，在 [include/evaluator/eval_base.h:L98-L108](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L98-L108)：

```cpp
template <typename Fun, class... Args>
constexpr __aicore__ inline void Assign(Fun& fun_, Args... args) { fun_(args...); }   // 仿函数：调用它

template <typename T, typename U>
__aicore__ inline void Assign(T& dst, const U& src) { dst = src; }                    // 普通：赋值
```

当右值求值结果是一个「可调用对象」（例如把一个算子当作函数对象），走第一个重载 `fun_(args...)`；当左右都是张量，走第二个重载 `dst = src` 完成拷贝。

再看 `OpAndThen` 的特化 [include/evaluator/eval_base.h:L84-L96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L84-L96)，这是本讲最值得品味的一段：

```cpp
template <typename T, typename U>
struct Evaluator<OpAndThen<T, U>> {
    using Type = typename Evaluator<U>::Type;
    template <typename Context>
    __aicore__ inline Type operator()(const OpAndThen<T, U>& op, Context& context) const {
        // operator, evaluates sequentially
        AscendC::PipeBarrier<PIPE_V>();
        return Atvoss::Tile::Evaluator<T>{}(op.GetLhs(), context), Atvoss::Tile::Evaluator<U>{}(op.GetRhs(), context);
    }
};
```

三个要点：

1. **`using Type = typename Evaluator<U>::Type`**：逗号表达式的「值」取最后一条语句（右子树），与 C++ 内建逗号运算符语义一致。
2. **`AscendC::PipeBarrier<PIPE_V>()`**：在求左、右子树之前，先插一道 **Vector 流水线屏障**。这正是本讲实践任务要回答的关键——见下方专门解释。
3. **`A, B` 那一行**：这里的逗号是 C++ 语言内建的「顺序求值」逗号运算符（不是被重载的那个，因为两侧已经是值而非 `Expression`），保证先完整求值 `Evaluator<T>{}(...)`、再求值 `Evaluator<U>{}(...)`。

**为什么 `OpAndThen` 需要 `PipeBarrier<PIPE_V>()`？**

AI Core 的多条流水线（MTE2/V/MTE3）是异步并行的：Vector 指令被「发射」后并不立即完成，硬件会继续往下取指。当用户用逗号把多条 `OpAssign` 串起来（例如 `(out1 = Exp(in1), out2 = Sqrt(in2))`），这些语句很可能读写**同一块 UB 缓冲**（双缓冲复用，见 [u3-l5](u3-l5-buffer-double-buffering.md)）。如果上一条 Vector 指令还在流水线上飞着，下一条就抢先读写同一块 buffer，就会产生 **RAW（读后写）/ WAW 竞争**。

`PipeBarrier<PIPE_V>()` 的作用就是**强制等待 Vector 流水线排空**——保证「上一条语句的所有 V 指令真正落袋」之后，才允许下一条语句开始发射。它是 ATVOSS 在「顺序语句边界」上自动插入的最小同步，用户完全无需手写。

> 小结：单个 `dst = op(src)` 不需要屏障（一条 `OpAssign` 内部，ATVOSS 通过双缓冲 ping-pong 与 bufPools 的 Mutex 锁来管理同步，见 [u3-l2](u3-l2-tile-layer-assign.md)）；**屏障只插在「语句与语句之间」的 `OpAndThen` 边界上**。

#### 4.3.4 代码实践

**实践目标**：用 `out1 = Exp(in1)` 与 `out2 = Sqrt(in2)` 两条串联语句，定位 `PipeBarrier` 的触发点。

**操作步骤**：

1. 想象 `Compute()` 里写 `return (out1 = Exp(in1), out2 = Sqrt(in2));`。
2. 由 [include/expression/expr_template.h:L533-L537](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L533-L537) 的 `operator,`，外层会变成 `Expression<OpAndThen<OpAssign<out1,OpExp<in1>>, OpAssign<out2,OpSqrt<in2>>>>`。
3. 追踪求值：根节点命中 `Evaluator<OpAndThen<...>>`（[eval_base.h:L84-L96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L84-L96)），它**先调用 `PipeBarrier<PIPE_V>()`**，然后顺序求值左、右两个 `OpAssign`。
4. **预期现象**：整段执行序列大致是 `Exp 指令 → PipeBarrier<PIPE_V> → Sqrt 指令`。也就是说，`Sqrt` 必须等到 `Exp` 在 Vector 流水线上彻底完成才能发射。
5. **待本地验证**：若能在仿真环境抓取算子的指令流，可在 `Exp` 与 `Sqrt` 之间观察到一道 Vector 同步。

#### 4.3.5 小练习与答案

**练习 1**：`Evaluator<OpAssign<T,U>>`（通用版）与 `Evaluator<OpAssign<T, OpSqrt<U>>>`（math 版）同时存在，对 `out = Sqrt(in)` 编译器选哪个？为什么？

**参考答案**：选 `Evaluator<OpAssign<T, OpSqrt<U>>>`。C++ 重载决议中，`OpAssign<T, OpSqrt<U>>` 比 `OpAssign<T, U>` 更「特殊」（对第二个模板参数进一步约束为 `OpSqrt<U>`），是更具体的偏特化，优先级更高。通用版本只在没有专门算子特化时兜底。

**练习 2**：如果把 `OpAndThen` 特化里的 `PipeBarrier<PIPE_V>()` 删掉，最可能在什么场景下出错？

**参考答案**：当相邻两条语句复用同一块 UB buffer（例如双缓冲把上一 Tile 的输出 buffer 给下一 Tile 当输入，或临时变量被复用）时，第二条语句的 V 指令可能在第一条还未写完时就抢先读取，读到脏数据或部分更新的值，产生精度错误甚至随机结果。屏障是正确性的最后保险。

---

### 4.4 math/transcendental 求值器特化：落到 Ascend C 指令

#### 4.4.1 概念说明

`eval_base.h` 只定义了「骨架 + 叶子 + 结构节点」的通用求值。真正让 `dst = Sqrt(src)` 变成一条 `AscendC::Sqrt(...)` 指令的，是 **`math_evaluator.h` 与 `transcendental_evaluator.h`** 里为每个算子写的偏特化。

这正印证了 [u2-l3](u2-l3-math-tensor-operators.md) 反复强调的「表达式声明 ↔ 求值器特化」**严格配对**原则：

> 你每声明一个 `OpXxx` 节点，框架就为 `Evaluator<OpAssign<dst, OpXxx<...>>>` 准备一个特化，把这对 `<dst 张量, src 张量, 形状>` 喂给对应的 Ascend C 指令。

这套机制有两个关键设计：

1. **二元算子的标量分派**：对 `Add/Sub/Mul/Div`，同一个 `OpAssign<T, OpXxx<U,V>>` 特化内部用 `if constexpr` 检查 `U`、`V` 谁是标量，分别走 `AddsAssign/MulsAssign/...`（标量广播指令）或 `AddAssign/MulAssign/...`（逐元素指令）。
2. **薄包装层 `XxxAssign`**：每个特化并不直接调 `AscendC::Xxx`，而是调一个同名的 `XxxAssign` 内联函数，由后者把「`OperationShape` 里的 `axis0`/`axis1`」翻译成 Ascend C 指令所需的「计算元素数」参数。这层包装隔离了表达式世界与硬件 API 世界。

#### 4.4.2 核心流程

以 `out = Sqrt(in)` 为例的完整翻译链：

```text
Evaluator<OpAssign<out, OpSqrt<in>>>{}(op, ctx)        # math_evaluator.h:L502 特化
  ├─ Dtype     = Dtype_t<out>                           # 取输出元素类型
  ├─ opShape   = GetShape<Operation::Unary>(ctx.argsTensors)   # 取本 Tile 计算形状（axis0）
  ├─ dstUb     = Evaluator<out>{}(op.GetLhs(), ctx).GetUbTensor()    # 取 dst 的 UB LocalTensor
  ├─ srcUb     = Evaluator<in>{}(op.GetRhs().GetData(), ctx).GetUbTensor()  # 取 src 的 UB LocalTensor
  └─ SqrtAssign<opShape, Dtype>(dstUb, srcUb, opShape)  # 薄包装
        └─ AscendC::Sqrt(dst, src, opShape.axis0)       # 最终硬件指令
```

其中 `OperationShape` 是一个带 `axis0/axis1/axis2` 的简单结构（[include/utils/layout/layout.h:L15-L19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L15-L19)）；`GetShape<Operation::Unary>(...)` 从入参张量取出本次计算的一元形状（元素总数落在 `axis0`），定义在 [include/operators/tile_shape.h:L105-L125](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L105-L125)；`Operation` 枚举（`Unary`/`Binary`/`Ternary`）在同一文件 [include/operators/tile_shape.h:L23-L28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L23-L28)。

逐元素（一元）算子用 `axis0` 当计算长度；归约/广播（二元形状）算子则用 `GetShape<Operation::Binary>` 取 `{axis0, axis1}` 二维形状。

#### 4.4.3 源码精读

**Sqrt 的求值特化** 在 [include/operators/math_evaluator.h:L502-L515](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L515)：

```cpp
template <typename T, typename U>
struct Evaluator<OpAssign<T, OpSqrt<U>>> {
    using Type = void;
    template <typename Context>
    __aicore__ inline auto operator()(const OpAssign<T, OpSqrt<U>>& op, Context& context) const {
        using Dtype = Dtype_t<T>;
        OperationShape operationShape = GetShape<Operation::Unary>(context.argsTensors);
        return Atvoss::Tile::SqrtAssign<OperationShape, Dtype>(
            Evaluator<T>{}(op.GetLhs(), context).GetUbTensor(),
            Evaluator<U>{}(op.GetRhs().GetData(), context).GetUbTensor(), operationShape);
    }
};
```

读法：

- `op.GetLhs()` 是左值 `out`（一个 `Param`），用 `Evaluator<T>` 求值（命中 4.2 的 `Param` 特化）返回一个 `BlockTensor`，再 `.GetUbTensor()` 取出它持有的 UB `LocalTensor` 作为 `dst`。
- `op.GetRhs()` 是 `OpSqrt<in>`，调用其 `GetData()`（继承自 `UnaryOp`，见 [expr_template.h:L400-L403](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L400-L403)）取出内层的 `in`，再用 `Evaluator<U>` 求值得到 `src` 张量。
- 最终落到 `SqrtAssign`。

**薄包装 `SqrtAssign`** 在 [include/operators/math_evaluator.h:L199-L204](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L199-L204)，它直接调用 `AscendC::Sqrt(dst, src, operationShape.axis0)`——这就是表达式最终变成的那条硬件指令。其他一元算子（`ExpAssign`/`AbsAssign`/`PowerAssign`）结构完全同构。

**二元算子与标量分派** 以加法为例。`Evaluator<OpAssign<T, OpAdd<U,V>>>` 在 [include/operators/math_evaluator.h:L272-L301](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L272-L301)：

```cpp
static_assert(!std::is_scalar_v<typename U::Type> || !std::is_scalar_v<typename V::Type>,
              "OpAdd's inputs not accepts all scalar types");      // 禁止两边都是标量
if constexpr (std::is_scalar_v<typename U::Type>) {
    return AddsAssign<...>(dst, src_tensor, scalar, opShape);       // 左标量 → Adds（标量广播）
} else if constexpr (std::is_scalar_v<typename V::Type>) {
    return AddsAssign<...>(dst, src_tensor, scalar, opShape);       // 右标量 → Adds
} else {
    return AddAssign<...>(dst, src0_tensor, src1_tensor, opShape);  // 双张量 → Add（逐元素）
}
```

这正是 [u2-l10](u2-l10-muls-deep-dive.md) 讲过的「标量自动分派到 `Muls`/`Adds` 而非 `Mul`/`Add`」在求值器层面的实现：`is_scalar_v` 在编译期判断，三个分支只实例化命中那一个，零运行时开销。`AddAssign`/`AddsAssign` 包装分别在 [math_evaluator.h:L25-L31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L25-L31) 与 [math_evaluator.h:L39-L44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L39-L44)。

> 注意 `Divs` 的一个小巧思：`DivsAssign`（[math_evaluator.h:L162-L168](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L162-L168)）把「除以编译期常量」优化成「乘以它的倒数」再走 `Muls`，因为乘法在硬件上比除法快——常量倒数在编译期就算好了（\( \text{src1} = 1 / \text{scalarValue} \)），运行时只剩一次乘法广播。

**归约/广播求值器** 在 `transcendental_evaluator.h`，结构与数学算子完全同构，只是取**二元形状**并调用归约/广播 API。`Evaluator<OpAssign<T, OpReduceSum<pattern, U>>>` 见 [include/operators/transcendental_evaluator.h:L86-L99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L86-L99)，它调用 `ReduceSumAssign`（[transcendental_evaluator.h:L44-L55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L44-L55)），后者把 ATVOSS 的 `Pattern::AR/RA` 映射到 `AscendC::Pattern::Reduce::AR/RA` 并调用 `AscendC::ReduceSum`。`Broadcast` 同理（[transcendental_evaluator.h:L24-L37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L24-L37) 与 [L64-L77](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L64-L77)）。这部分呼应 [u2-l4](u2-l4-reduce-broadcast-operators.md) 讲过的「真正的维度变换由求值器特化完成」。

#### 4.4.4 代码实践

**实践目标**：完成规格里要求的核心追踪——写出 `out = Sqrt(in)` 求值过程中每一步匹配的特化。

**操作步骤**：

设 `in = PlaceHolder<1, Tensor<float>, IN>`、`out = PlaceHolder<2, Tensor<float>, OUT>`，用户写 `return (out = Sqrt(in));`。

1. **表达式构造**（编译期，详见 [u2-l1](u2-l1-expression-template.md)/[u2-l3](u2-l3-math-tensor-operators.md)）：
   - `Sqrt(in)` 经 `DeclareUnaryOp(Sqrt)`（[math_expression.h:L165](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L165)）生成 `Expression<OpSqrt<Param<1,...>>>`；
   - `out = Sqrt(in)` 经 `Expression::operator=`（[expr_template.h:L469-L483](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L469-L483)）生成 `Expression<OpAssign<Param<2,...>, OpSqrt<Param<1,...>>>>`。
2. **点火**：`Tile::Evaluate<OpAssign<Param<2,...>, OpSqrt<Param<1,...>>>>(context)`（[tile_evaluate.h:L33-L37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L33-L37)）。
3. **根节点特化匹配**：命中 `Evaluator<OpAssign<T, OpSqrt<U>>>`（[math_evaluator.h:L502-L515](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L515)），而**不是**通用的 `Evaluator<OpAssign<T,U>>`（因为它更具体）。
4. **左值求值**：`Evaluator<Param<2,...>>{}` 命中 `Param` 特化（[eval_base.h:L51-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L51-L71)），返回 `get<1>(context.argsTensors)`（即 `out` 的 BlockTensor），再 `.GetUbTensor()` 得到 `dst` 的 `LocalTensor`。
5. **右值求值**：`op.GetRhs().GetData()` 取出内层 `Param<1,...>`，`Evaluator<Param<1,...>>{}` 同样命中 `Param` 特化，返回 `get<0>(context.argsTensors)`（即 `in` 的 BlockTensor），`.GetUbTensor()` 得到 `src` 的 `LocalTensor`。
6. **落指令**：调用 `SqrtAssign<opShape, float>(dst, src, opShape)`（[math_evaluator.h:L199-L204](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L199-L204)），最终执行 `AscendC::Sqrt(dst, src, opShape.axis0)`。

**需要观察的现象**：整条链是「一个根特化 → 两个叶子特化 → 一条 Ascend C 指令」，中间没有任何 `OpAndThen`，因此**不会**触发 `PipeBarrier`——屏障只在多条语句串联时出现。

**回答实践任务的第二问**：`PipeBarrier<PIPE_V>()` 在 `OpAndThen`（[eval_base.h:L84-L96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L84-L96)）中的作用是——**在顺序求值左右两条子语句之前，强制排空 Vector 流水线**，确保前一条语句的所有 V 指令真正写完 UB，再让后一条语句开始发射，从而避免相邻语句复用同一 UB buffer 时的 RAW/WAW 竞争。它是 ATVOSS 在「语句边界」上自动插入的最小同步，对单个 `OpAssign`（如本例的 `out = Sqrt(in)`）不生效。

**待本地验证**：上述调用链是源码阅读型推导；若要在真机/仿真上验证，可在 `SqrtAssign` 内部临时加一行日志（注意这是修改源码，仅用于本地学习，验证后请还原），观察 `dst`/`src` 指针与 `axis0` 是否符合预期。

#### 4.4.5 小练习与答案

**练习 1**：对于 `out = in1 + in2`（`in1`、`in2` 都是张量），求值时会调用 `AddsAssign` 还是 `AddAssign`？依据是什么？

**参考答案**：调用 `AddAssign`（逐元素加）。依据是 `Evaluator<OpAssign<T, OpAdd<U,V>>>` 内的 `if constexpr`：当 `U::Type` 与 `V::Type` 都**不是标量**时，走 `else` 分支调用 `AddAssign`，最终落到 `AscendC::Add`。只有当某一侧是标量时才走 `AddsAssign`（`AscendC::Adds`）。判断发生在编译期，零运行时开销。

**练习 2**：为什么 `ReduceSum` / `Broadcast` 的求值器用 `GetShape<Operation::Binary>` 取二维形状，而 `Sqrt` 用 `GetShape<Operation::Unary>`？

**参考答案**：一元逐元素算子只需知道「本次要算多少个元素」，即一维长度 `axis0`；而归约/广播会改变张量形状（如沿行归约、沿列广播），需要 `{axis0, axis1}` 二维形状才能描述「沿哪个轴、缩放成什么样」。这与 [u2-l4](u2-l4-reduce-broadcast-operators.md) 讲的 Pattern 语义一致，也决定了归约/广播当前仅面向二维 Tile。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「**从用户公式到硬件指令**」的完整求值追踪。

**任务**：阅读 [examples/muls/muls.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp) 中的 `MulsCompute`（其公式形如 `out = in * scalar`，`in` 是张量、`scalar` 是标量），完成下面四件事：

1. **画出表达式树**：写出 `out = in * scalar` 展开后的 `Expression<OpAssign<...>>` 嵌套类型（注意标量 `scalar` 不被 `Tensor` 包裹，参考 [u2-l10](u2-l10-muls-deep-dive.md)）。
2. **定位根特化**：确认它命中 `Evaluator<OpAssign<T, OpMul<U,V>>>`（[math_evaluator.h:L354-L383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L354-L383)），并指出由于一侧是标量，实际走的是 `if constexpr` 里的哪个分支、调用的是 `MulsAssign` 还是 `MulAssign`。
3. **追踪叶子求值**：标出张量侧 `in` 经 `Evaluator<Param<...>>` 从 `context.argsTensors` 取出的过程（[eval_base.h:L51-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L51-L71)），以及标量侧经主模板兜底原样返回的过程（[eval_base.h:L22-L32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L22-L32)）。
4. **讨论同步**：`muls` 是单条语句，求值时**不会**触发 `PipeBarrier`。请说明若把它改成 `(tmp = Cast<...>(in), out = tmp * scalar)` 这种「先转后乘」的逗号串联，`PipeBarrier<PIPE_V>()` 会插在哪两条语句之间、起什么保护作用。

**预期产出**：一段文字 + 一棵类型树 + 一条「根特化 → 叶子特化 → `AscendC::Muls`」的调用链。这是检验你是否真正理解「求值器 = 编译期解释器」的试金石。

**待本地验证**：若条件允许，可用 `bash scripts/build.sh -DSOC=ascend950 muls`（参考 [u1-l2](u1-l2-directory-and-build.md)）编译运行，确认算子结果正确；求值链本身则通过阅读源码完成。

---

## 6. 本讲小结

- **求值器是一台编译期解释器**：`Evaluator<T>` 用「主模板 + 偏特化」对表达式树做递归求值，`tile_evaluate.h` 的 `Evaluate<Expr>` 是点火入口，递归在编译期内联展开，运行时零开销。
- **`Expression<>` 外壳由脱壳特化剥离**：`Evaluator<Expression<T>>` 公有继承 `Evaluator<T>`，让所有特化只针对裸节点编写。
- **叶子求值 = 用序号取张量**：`Param` 从 `context.argsTensors` 取（`get<N-1>`，仅 `IN` 参数允许隐式类型转换），`LocalVar` 从 `context.tmpTensors` 取；二者序号空间独立，呼应 [u2-l2](u2-l2-placeholder-and-param.md)。
- **`OpAssign` 是「一条指令」**：通用版走 `Assign(dst, src)`；但绝大多数算子命中 `math_evaluator.h`/`transcendental_evaluator.h` 里更具体的 `OpAssign<T, OpXxx<...>>` 特化，经薄包装 `XxxAssign` 落到 Ascend C 指令。
- **`OpAndThen` 是「语句边界 + 同步」**：它在顺序求值左右子树之前插入 `PipeBarrier<PIPE_V>()`，强制排空 Vector 流水线，避免相邻语句复用 UB buffer 时的竞争；单个 `OpAssign` 不触发屏障。
- **标量分派在编译期完成**：二元算子用 `if constexpr + is_scalar_v` 在 `Add/Mul` 与 `Adds/Muls` 间二选一，只实例化命中分支。

---

## 7. 下一步学习建议

本讲讲清了「表达式树如何被求值成指令」，但刻意把两件事留作了黑盒：

1. **`dst`/`src` 的 `LocalTensor` 从哪来、`CopyIn`/`CopyOut`/`Alloc`/`Free` 怎么求值**——这是 [u3-l2 Tile 层：Assign 函数到 Ascend C API](u3-l2-tile-layer-assign.md) 的主题，它会打开 `tensor_evaluator.h`，讲清张量算子节点如何映射到 `DataCopyPad`、`bufPools.AllocTensor` 以及 `PIPE_MTE2/MTE3/V` 的 Mutex 锁链同步。
2. **表达式树在到达求值器之前，是如何被「拍平 + 插入 Alloc/Free + 消除冗余 Cast」的**——这是 [u3-l4 表达式线性化与图优化 Pass](u3-l4-linearizer-and-passes.md) 的主题。求值器吃进去的 `Expr`，其实是图优化 Pass 加工后的产物。

建议按 [u3-l2](u3-l2-tile-layer-assign.md) → [u3-l3](u3-l3-dag-construction.md) → [u3-l4](u3-l4-linearizer-and-passes.md) 的顺序继续，你将看到一棵用户原始表达式是如何经过 DAG 构建、线性化、缓冲分配，最终以本讲描述的求值方式执行的完整闭环。
