# 运算符库：数学与张量算子

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `math_expression.h` 里每一类数学算子（四则运算、`Sqrt`/`Exp`/`Abs`、`Power`/`Divs`/`Cast`）的**表达式声明形态**，并能正确写出含标量的运算。
- 读懂 `tensor_expression.h` 里五个张量算子（`Alloc`/`Free`/`CopyIn`/`CopyOut`/`Copy`）的声明，理解它们表达的「内存生命周期 + 数据搬运」语义。
- 建立**「表达式声明 ↔ 求值器特化」**的核心配对认知：用户在 `Compute()` 里写下的每一个运算符，最终都会在 `math_evaluator.h` / `tensor_evaluator.h` 中匹配到一个 `Evaluator<OpAssign<...>>` 特化，并落到一条真实的 Ascend C API。

本讲只讲「运算符长什么样、声明与求值如何配对」；表达式树的递归求值细节留到 u3-l1，Tile 层的流水同步细节留到 u3-l2。

## 2. 前置知识

本讲承接 u2-l1（表达式模板基础）与 u2-l2（参数与占位符），需要你已经掌握：

- **表达式是编译期 AST**：`Compute()` 里的 `+ - * /`、`Exp()`、`Cast()` 都是被 `constexpr` 重载的函数，调用它们构造的是一棵类型嵌套的 `Expression<Op...>` 树，而不是运行时计算（见 u2-l1）。
- **叶子节点**：`Param`（外部入参/出参）和 `LocalVar`（临时变量）是表达式的叶子，由 `PlaceHolder<N, T, ParamUsage>` 与 `PlaceHolderTmpLike<N, T>(某Param)` 创建，序号 `N` 与 `ParamUsage` 决定数据流方向（见 u2-l2）。
- **`OpAssign`**：`out = ...` 触发 `Expression::operator=`，生成 `OpAssign<左值, 右值表达式>` 节点；这是后续一切求值的「入口节点」。

还需要两个本讲才引入的名词：

- **求值器（Evaluator）**：一个主模板 `Evaluator<T>`，对每一种 `Op` 做**模板特化**。特化体里的 `operator()` 才是「真正干活的代码」（调用 Ascend C API）。求值器位于 `Atvoss::Tile` 命名空间，属于 Tile 层。
- **Ascend C API**：昇腾硬件的底层 Vector 计算接口，如 `AscendC::Add`、`AscendC::Exp`、`AscendC::DataCopyPad`。ATVOSS 的「性能上限」就等于这些手写 API，因为最终调用的就是它们。

一句话直觉：**用户写的每一个运算符符号，都是一张「期票」——在编译期被登记成表达式节点，到了 Tile 层由对应的求值器特化「兑现」成一条 Ascend C 指令。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `include/operators/math_expression.h` | 数学算子的**表达式声明** | `OpAdd/OpSub/OpMul/OpDiv` + 运算符重载、`Power/Divs/Cast`、`DeclareUnaryOp(Sqrt/Exp/Abs)`、`DeclareBinaryOp(Max)` |
| `include/operators/tensor_expression.h` | 张量算子的**表达式声明** | `OpAlloc/OpFree/OpCopyIn/OpCopyOut/OpCopy` 及其 `static_assert` 约束 |
| `include/operators/math_evaluator.h` | 数学算子的**求值器特化** | 每个 `Evaluator<OpAssign<T, OpX<...>>>` 如何落到 `AscendC::Xxx` |
| `include/elewise/tile/tensor_evaluator.h` | 张量算子的**求值器特化** | `OpCopyIn/OpCopyOut` → `DataCopyPad`、`OpAlloc/OpFree` → `bufPools` |
| `include/utils/patterns.h` | 枚举定义 | `CastMode` 的 7 种取值（决定 `Cast` 的舍入方式） |
| `include/expression/expr_template.h` | 表达式基础设施 | `UnaryOp`/`BinaryOp` 基类、`DeclareUnaryOp`/`DeclareBinaryOp` 宏 |
| `docs/api/README.md` | 官方接口列表 | 对外暴露的运算符清单（表 2） |

---

## 4. 核心概念与源码讲解

### 4.1 四则运算算子与运算符重载（Add / Sub / Mul / Div）

#### 4.1.1 概念说明

四则运算是使用频率最高的一类算子。在 ATVOSS 中，`+ - * /` 这四个符号被**运算符重载**成构造表达式节点的函数：`a + b` 不会立刻做加法，而是返回一个 `Expression<OpAdd<T, U>>` 对象，把「加法意图」记录进表达式树。

这里有一个对初学者最关键的点：**同一个 `*` 符号，既能表示「张量 ⊗ 张量」，也能表示「张量 ⊗ 标量」**。这两种情况最终调用的 Ascend C 指令不同（`Mul` vs `Muls`），而这个分支是在**求值器**里用 `if constexpr` 在编译期决定的——用户写的代码完全一样。

#### 4.1.2 核心流程

以 `out = in1 * in2` 与 `out = in * scalar` 为例：

```text
用户写法                  编译期构造的节点                 Tile 层求值器特化                  Ascend C 指令
out = in1 * in2    →   OpAssign<out, OpMul<in1,in2>>  → Evaluator<OpAssign<out,OpMul<...>>>  → AscendC::Mul
out = in  * scalar →   OpAssign<out, OpMul<in,scalar>>→ Evaluator<OpAssign<out,OpMul<...>>>  → AscendC::Muls  (因一侧为标量)
```

注意右侧两个表达式节点都是 `OpMul`，**分支发生在求值器内部**：它用 `std::is_scalar_v` 判断某一侧是否标量，再选择 `MulAssign` 或 `MulsAssign`。

#### 4.1.3 源码精读

`OpAdd` 的声明与三个 `operator+` 重载（[include/operators/math_expression.h:L21-L45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L21-L45)）：`OpAdd` 继承 `BinaryOp<T,U>`（一个空基类，用 EBO 存两个子表达式），三个重载分别覆盖「表达式 + 表达式」「表达式 + 普通值」「普通值 + 表达式」三种书写顺序，使 `scalar + in` 与 `in + scalar` 都能成立。`OpSub`/`OpMul`/`OpDiv` 的写法与 `OpAdd` 完全同构。

求值器侧的标量分派，以 `OpMul` 为例（[include/operators/math_evaluator.h:L354-L383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L354-L383)）：

- `static_assert` 拒绝「两侧都是标量」（那不是张量运算）；
- `if constexpr (std::is_scalar_v<typename U::Type>)`：左操作数是标量 → 调 `MulsAssign`（[include/operators/math_evaluator.h:L108-L113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L108-L113)）；
- 否则若右操作数是标量 → 同样 `MulsAssign`；
- 否则两侧都是张量 → `MulAssign`（[include/operators/math_evaluator.h:L94-L100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L94-L100)）。

`MulAssign` / `MulsAssign` 这两个薄封装最终分别调用 `AscendC::Mul` / `AscendC::Muls`（[include/operators/math_evaluator.h:L94-L113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L94-L113)）。`Add` 的求值器分派（`AddAssign`/`AddsAssign`）结构完全一致（[include/operators/math_evaluator.h:L272-L301](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L272-L301)）。

真实用例见 `muls.cpp` 的 `MulsCompute`：`return (out = in * scalar);`，其中 `scalar` 是一个标量 `PlaceHolder`（[examples/muls/muls.cpp:L28-L38](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L28-L38)）——这一行最终兑现成 `AscendC::Muls`。

#### 4.1.4 代码实践

**实践目标**：体会「同一符号、不同指令」的编译期分派。

1. 打开 `examples/muls/muls.cpp`，确认 `MulsCompute` 的表达式是 `out = in * scalar`。
2. 在 `math_evaluator.h` 的 `Evaluator<OpAssign<T, OpMul<U,V>>>`（[L354-L383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L354-L383)）中找到三个 `if constexpr` 分支。
3. **需要观察的现象**：`MulsCompute` 里 `scalar` 的 `PlaceHolder` 用的是裸标量类型 `ScalarDtype`（不是 `Tensor<...>`），这正是触发 `std::is_scalar_v` 为真的关键。
4. **预期结果**：能在源码中指认「muls 走的是 `MulsAssign` → `AscendC::Muls`」这一条链路；若把 `scalar` 改成另一个 `Tensor` 占位符（即 `in1 * in2`），则应改走 `MulAssign` → `AscendC::Mul`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Evaluator<OpAssign<T, OpMul<U,V>>>` 里要 `static_assert(!两侧都是标量)`？
**答案**：ATVOSS 是**张量**算子库，标量 ⊗ 标量不是它的职责；该断言在编译期就把「两个纯标量相乘」这种误用挡掉，给出清晰错误信息。

**练习 2**：`out = in1 - 5.0f` 与 `out = 5.0f - in1` 都合法吗？它们分别对应哪个 `SubsAssign` 重载？
**答案**：都合法。两者一侧均为标量，都会走 `SubsAssign`（标量版减法）。注意 `SubsAssign` 有两个重载：`dst = src - scalar` 与 `dst = scalar - src`（[include/operators/math_evaluator.h:L67-L85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L67-L85)），顺序不同指令语义不同（且仅在 `_ATVOSS_ARCH35_` 下提供）。

---

### 4.2 宏声明的一元算子：Sqrt / Exp / Abs / Max

#### 4.2.1 概念说明

`Sqrt`、`Exp`、`Abs` 这类「单输入、单输出」的数学函数，结构高度雷同：都继承 `UnaryOp<T>`，都有一个只接受单个 `Expression` 的工厂函数。为了避免重复书写，ATVOSS 用一个宏 `DeclareUnaryOp(Name)` 一次性生成「`OpName` 结构体 + `Name()` 工厂函数」。`Max` 是二元函数，用对应的 `DeclareBinaryOp(Name)` 生成。

#### 4.2.2 核心流程

```text
DeclareUnaryOp(Sqrt) 宏展开
   ├─ struct OpSqrt<T> : UnaryOp<T> { ... };          // 运算节点
   └─ constexpr auto Sqrt(Expression<T> lhs)          // 用户调用入口
        { return Expression<OpSqrt<T>>{...}; }
```

宏的本质是「文本展开」，生成的内容与手写的 `OpAdd`/`operator+` 同构，只是把二元换成一元、把运算符符号换成函数名。

#### 4.2.3 源码精读

三个一元算子的声明各占一行（[include/operators/math_expression.h:L165-L169](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L165-L169)）：`DeclareUnaryOp(Sqrt); DeclareUnaryOp(Exp); DeclareUnaryOp(Abs);`。`Max` 用二元宏声明（[include/operators/math_expression.h:L200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L200)）。

宏定义本体（[include/expression/expr_template.h:L551-L567](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L551-L567)）：展开后会得到 `struct Op##Name : UnaryOp<T>` 与两个 `Name()` 重载（分别接 `Expression<T>` 与完美转发版本）。`DeclareBinaryOp` 同理，但生成三个重载以覆盖左右操作数的各种顺序（[include/expression/expr_template.h:L570-L591](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L570-L591)）。

`UnaryOp` 基类（[include/expression/expr_template.h:L383-L404](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L383-L404)）用 `CompressedData<T>` 存唯一子表达式，对外暴露 `DataType`、`TensorType`、`RetType` 与 `GetData()`，并打上 `IsUnaryOp` 标签供后续遍历器识别。

真实用例：`abs.cpp` 的 `return (out = Abs(in));`（[examples/abs/abs.cpp:L24-L32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L24-L32)）。

> 关于 `Max`：它通过 `DeclareBinaryOp(Max)` 声明为二元表达式节点 `OpMax`，但**本仓库的 `math_evaluator.h` 中并没有 `Evaluator<OpAssign<T, OpMax<U,V>>>` 特化**（待确认是否在他处实现或尚未接入）。本讲后续配对表只列出确认存在求值器特化的算子。

#### 4.2.4 代码实践

**实践目标**：理解「宏 = 同构代码生成器」。

1. 读 `DeclareUnaryOp` 宏（[expr_template.h:L551-L567](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L551-L567)）。
2. 在纸上把 `DeclareUnaryOp(Sqrt)` 手工展开，对照 `OpAdd` 的写法（[math_expression.h:L21-L33](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L21-L33)），确认两者结构一致（一个继承 `UnaryOp`、一个继承 `BinaryOp`）。
3. **预期结果**：能口述「`Abs(in)` 返回 `Expression<OpAbs<...>>`」，并理解调用 `Abs` 时**没有任何计算发生**，只是构造了一个空壳节点。

#### 4.2.5 小练习与答案

**练习 1**：若要新增一个一元算子 `Reciprocal`（取倒数），最少要写哪些声明？
**答案**：调用端只需一行 `DeclareUnaryOp(Reciprocal);` 即可获得 `OpReciprocal` 节点与 `Reciprocal()` 工厂函数。但要让它真正能跑，还需在 `math_evaluator.h` 增加 `Evaluator<OpAssign<T, OpReciprocal<U>>>` 特化并调用对应 Ascend C API——**声明和求值是两件事**。

**练习 2**：`Sqrt`、`Exp`、`Abs` 三者的求值器特化形态几乎一致，请指出它们的共同点。
**答案**：三者都是 `Evaluator<OpAssign<T, OpX<U>>>`，内部都取 `OperationShape`、对左右子树分别求值取出 `UbTensor`，再调一个 `XxxAssign` 薄封装（如 `SqrtAssign`），最终落到一条 `AscendC::Sqrt/Exp/Abs`。

---

### 4.3 带编译期参数的算子：Power / Divs / Cast

#### 4.3.1 概念说明

这三者与前面两类不同：它们**自带模板参数**，需要写成函数调用形式（`Power<2>(in)`、`Divs<WIDTH>(in)`、`Cast<float>(in)`），无法用纯运算符或简单宏表达。模板参数分两类：

- **编译期常量标量**：`Power<scalarValue>` 与 `Divs<scalarValue>` 把「指数/除数」编码在**类型**里，是编译期已知的常数，与 4.1 节「运行时传入的标量 `PlaceHolder`」不同。
- **目标类型 + 舍入模式**：`Cast<castMode, R>` 既要指定目标数据类型 `R`，又要指定 `CastMode`（舍入方式）。

#### 4.3.2 核心流程

```text
Power<2>(in)        →  Expression<OpPower<2, T>>      →  PowerAssign  → AscendC::Power(dst, src, T{2}, ...)
Divs<WIDTH>(in)     →  Expression<OpDivs<WIDTH, T>>   →  DivsAssign   → 先算 T{1}/WIDTH，再 AscendC::Muls（把除法优化成乘以倒数）
Cast<float>(in)     →  Expression<OpCast<castMode,float,T>> → CastAssign → AscendC::Cast(..., RoundMode, ...)
```

`Divs` 的优化值得留意：硬件上「除以常数」被改写为「乘以常数的倒数」，因为乘法比除法快。

#### 4.3.3 源码精读

`OpPower` / `Power`（[include/operators/math_expression.h:L125-L143](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L125-L143)）：注意模板形参是 `<auto scalarValue, typename T>`——`auto` 让 `scalarValue` 可以是 `int`/`float` 等任意值类型，且成为类型的一部分（`OpPower<2,T>` 与 `OpPower<3,T>` 是不同类型）。`OpDivs` / `Divs` 写法同构（[include/operators/math_expression.h:L145-L163](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L145-L163)）。

`OpCast`（[include/operators/math_expression.h:L171-L198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L171-L177)）：它的第二个模板实参是 `typename ReplaceTensorType<...>::Type`，即把输入张量的元素类型替换为目标类型 `R` 后的新张量类型——这正是 `Cast` 能改变 `RetType` 的原因（`out` 的类型随之改变）。`Cast` 工厂函数默认 `castMode = CastMode::CAST_ROUND`（[include/operators/math_expression.h:L188-L198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L188-L198)）。

`CastMode` 的 7 种取值（[include/utils/patterns.h:L22-L31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L22-L31)）：`CAST_NONE / CAST_RINT / CAST_FLOOR / CAST_CEIL / CAST_ROUND / CAST_TRUNC / CAST_ODD`。

求值器侧：

- `Divs` 的「除以常数 → 乘倒数」优化在 `DivsAssign`（[include/operators/math_evaluator.h:L162-L168](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L162-L168)）：`T src1 = T{1} / scalarValue; AscendC::Muls(...)`。其求值器特化见 [include/operators/math_evaluator.h:L436-L449](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L436-L449)。
- `Power` 的 `PowerAssign`（[include/operators/math_evaluator.h:L211-L216](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L211-L216)）与求值器特化（[include/operators/math_evaluator.h:L524-L537](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L524-L537)）。
- `Cast` 的 `CastAssign`：用一串 `if constexpr` 把 `CastMode` 映射到 `AscendC::RoundMode`，再调 `AscendC::Cast`（[include/operators/math_evaluator.h:L223-L242](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L223-L242)）；求值器特化见 [include/operators/math_evaluator.h:L546-L560](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L546-L560)，注意它同时取出 `DstType` 与 `SrcType`，故源与目标类型可以不同。

真实用例：`muls.cpp` 的 `MulsComputePromtIn` 用 `Cast<CastMode::CAST_NONE, ScalarDtype>(in)` 把输入转到目标类型，再借助 `PlaceHolderTmpLike` 暂存中间结果（[examples/muls/muls.cpp:L40-L51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L40-L51)）。

#### 4.3.4 代码实践

**实践目标**：区分「编译期常量标量」与「运行时标量」。

1. 对比 `muls.cpp` 里的两种「标量乘法」：
   - 4.1 节的 `MulsCompute`：`out = in * scalar`，`scalar` 是运行时 `PlaceHolder`（[muls.cpp:L28-L38](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L28-L38)）。
   - 假设改用 `Divs<WIDTH>(in)`：除数 `WIDTH` 是编译期常量。
2. **需要观察的现象**：运行时标量走 `MulsAssign`（直接 `AscendC::Muls`）；编译期常量除法走 `DivsAssign`（先算倒数再 `Muls`，少一次运行时除法）。
3. **预期结果**：能解释「为何 `Divs` 要单独设计成模板函数，而不是复用 `/` 运算符」——因为除数必须是编译期常量才能做「乘倒数」优化。

#### 4.3.5 小练习与答案

**练习 1**：`Cast<float>(in)` 中，`in` 是 `int32` 张量，求值器取出的 `DstType` 和 `SrcType` 分别是什么？调用的是哪条 Ascend C 指令？
**答案**：`DstType = float`（来自赋值左值 `T`），`SrcType = int32`（来自 `U`，即 `in` 的类型）；调用 `AscendC::Cast(dst, src, RoundMode, count)`，`RoundMode` 由 `castMode` 决定（默认 `CAST_ROUND`）。

**练习 2**：`Power<2>(in)` 与 `in * in` 都能算平方，二者在表达式树层面有何区别？
**答案**：`Power<2>(in)` 生成单节点 `OpPower<2,T>`，兑现成一条 `AscendC::Power`；`in * in` 生成 `OpMul<in,in>`，兑现成 `AscendC::Mul`。前者用硬件幂指令，后者用乘法指令，语义等价但底层指令不同。

---

### 4.4 张量算子：Alloc / Free / CopyIn / CopyOut / Copy

#### 4.4.1 概念说明

前面三类都是「计算」算子（在 UB 上做 Vector 运算）。本节的五个张量算子不是数学计算，而是描述**数据搬运与内存生命周期**：

- `CopyIn`：GM（全局显存）→ UB（核内统一缓冲），即把输入搬进计算核。
- `CopyOut`：UB → GM，把结果写回显存。
- `Copy`：UB → UB，纯核内搬运（如把一个变量赋给另一个同类型变量时由 `OpAssign` 自动插入）。
- `Alloc`：为参数/临时变量申请一块 UB 缓冲。
- `Free`：释放该缓冲。

理解它们的关键：**用户通常不直接写这五个算子**。它们由框架在「图构建 / 线性化」阶段根据用户的 `Compute()` 表达式**自动插入**（见 u3-l3、u3-l4）。但它们的「声明形态」与求值器配对，和数学算子遵循同一套机制。

#### 4.4.2 核心流程

```text
框架自动插入                  表达式节点              Tile 层求值器特化              Ascend C 指令
输入首次被使用      →  OpCopyIn<in>          → Evaluator<OpCopyIn<T>>     → DataCopyPad (GM→UB)
输出最终写回        →  OpCopyOut<out>       → Evaluator<OpCopyOut<T>>    → DataCopyPad (UB→GM)
纯变量搬运 out=var  →  OpAssign<out,OpCopy<var>> → Evaluator<OpAssign<T,OpCopy<U>>> → DataCopy (UB→UB)
缓冲生命周期        →  OpAlloc / OpFree      → Evaluator<OpAlloc/OpFree>  → bufPools.AllocTensor / 释放
```

每个张量算子的声明都带 `static_assert`，限制它只能作用于特定 `ParamUsage` 的 `Param`——例如 `CopyIn` 只能用于 `IN`/`IN_OUT`，`CopyOut` 只能用于 `OUT`/`IN_OUT`。

#### 4.4.3 源码精读

`OpCopyIn` 与 `CopyIn` 工厂，含 `static_assert` 约束（`IsParam_v<T>` 且 `usage == IN/IN_OUT`）（[include/operators/tensor_expression.h:L60-L79](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L60-L79)）。`OpCopyOut` 的约束则要求 `usage == OUT/IN_OUT`（[include/operators/tensor_expression.h:L81-L100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L81-L100)）。`OpAlloc`/`OpFree`（[include/operators/tensor_expression.h:L18-L58](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L18-L58)）。`OpCopy`（[include/operators/tensor_expression.h:L102-L107](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L102-L107)）。

求值器侧（注意：**张量算子的求值器在 Tile 层的 `tensor_evaluator.h`，不在 `math_evaluator.h`**）：

- `CopyIn` 底层封装：构造 `DataCopyExtParams`/`DataCopyPadExtParams`，调用 `AscendC::DataCopyPad`（[include/elewise/tile/tensor_evaluator.h:L38-L44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L38-L44)）；求值器特化 `Evaluator<OpCopyIn<T>>` 还包含 `Mutex::Lock<PIPE_MTE2>`/`Unlock` 同步（[include/elewise/tile/tensor_evaluator.h:L61-L85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L61-L85)）。
- `CopyOut` 求值器特化，带 `PIPE_MTE3` 同步（[include/elewise/tile/tensor_evaluator.h:L89-L113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L89-L113)）。
- UB→UB 的 `Evaluator<OpAssign<T, OpCopy<U>>>` → `CopyAssign` → `AscendC::DataCopy`（[include/elewise/tile/tensor_evaluator.h:L25-L30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L25-L30) 与 [L117-L131](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L117-L131)）。
- `OpAlloc` 求值器特化：按参数是「临时变量 / 输入 / 输出」三类，从 `bufPools` 取不同缓冲 id 并 `AllocTensor`（[include/elewise/tile/tensor_evaluator.h:L135-L177](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L135-L177)）。`OpFree` 特化负责释放（[include/elewise/tile/tensor_evaluator.h:L181-L200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L181-L200)）。

> `Mutex` 锁、`PIPE_MTE2/MTE3/V` 流水同步、`pingPong` 双缓冲 id 是 u3-l2（Tile 层）的核心主题，本讲只点到「这些算子最终会触发对应同步」为止。

#### 4.4.4 代码实践

**实践目标**：在表达式层确认 `CopyIn/CopyOut` 的使用约束（源码阅读型）。

1. 读 `CopyIn` 的 `static_assert`（[tensor_expression.h:L70-L71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L70-L71)）与 `CopyOut` 的 `static_assert`（[tensor_expression.h:L91-L92](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h#L91-L92)）。
2. **需要观察的现象**：`CopyIn` 断言要求 `usage == IN || IN_OUT`；`CopyOut` 断言要求 `usage == OUT || IN_OUT`。二者的交集恰好是 `IN_OUT`（原地参数既要搬入也要搬出）。
3. **预期结果**：能解释「为何一个纯 `OUT` 参数不允许 `CopyIn`」——它没有来自 GM 的输入数据可搬；同理纯 `IN` 参数不允许 `CopyOut`。

#### 4.4.5 小练习与答案

**练习 1**：用户在 `Compute()` 里几乎没有直接写 `CopyIn`/`CopyOut`，那它们是从哪里来的？
**答案**：由框架在图构建（u3-l3 `FullAutoDag` 的 `InplaceParamsProcessor` / CopyIn-CopyOut 插入）与线性化（u3-l4 的 `AllocInserter`/`FreeInserter`）阶段，根据每个 `Param` 的 `ParamUsage` 自动插入。用户只描述「算什么」，框架负责「数据怎么搬」。

**练习 2**：`OpCopy` 与 `OpCopyIn`/`OpCopyOut` 有何不同？
**答案**：`OpCopyIn`/`OpCopyOut` 跨 GM↔UB 边界（用 `DataCopyPad`）；`OpCopy` 是 UB→UB 核内搬运（用 `DataCopy`），通常在 `out = 某变量` 这种纯变量赋值时由 `Expression::operator=` 自动插入（见 u2-l1 的 `OpCopy` 插入逻辑）。

---

### 4.5 表达式声明 ↔ 求值器特化的配对关系

#### 4.5.1 概念说明

本讲最核心的认知：ATVOSS 的运算符系统是**严格配对**的——`math_expression.h` / `tensor_expression.h` 里每声明一个 `OpXxx` 节点，`math_evaluator.h` / `tensor_evaluator.h` 里就（应当）有一个 `Evaluator<OpAssign<dst, OpXxx<...>>>` 特化与之对应，把该节点翻译成一条（或一组）Ascend C 指令。

理解这种配对，就能做到「看到用户表达式，预判底层指令」。这也解释了 u2-l1 反复强调的「零运行时开销」：表达式节点是编译期类型，求值器特化也是编译期匹配，运行时只剩下 `AscendC::Xxx` 这一条指令，与手写 Ascend C 完全一致。

#### 4.5.2 核心流程

求值发生在表达式被**线性化、展平**之后（u3-l4）：任何嵌套表达式最终都被改写成一组 `OpAssign<左值, OpXxx<...>>` 序列。求值器对每个 `OpAssign` 做模板特化匹配：

```text
OpAssign<out, OpExp<in>>             匹配 Evaluator<OpAssign<T, OpExp<U>>>       → ExpAssign   → AscendC::Exp
OpAssign<out, OpSqrt<in>>            匹配 Evaluator<OpAssign<T, OpSqrt<U>>>      → SqrtAssign  → AscendC::Sqrt
OpAssign<out, OpAdd<t1, t2>>         匹配 Evaluator<OpAssign<T, OpAdd<U,V>>>     → AddAssign   → AscendC::Add
OpAssign<out, OpCast<m,R,in>>        匹配 Evaluator<OpAssign<T, OpCast<m,R,U>>>  → CastAssign  → AscendC::Cast
```

注意：求值器特化的对象是 **`OpAssign<左值, 某Op>` 这个整体**，而不是孤立的 `OpAdd`。这是因为展平后每个运算都形如「把某 Op 的结果赋给某左值」。`math_evaluator.h` 里还有一个不带 `OpAssign` 的 `Evaluator<OpAdd<T,U>>`（[include/operators/math_evaluator.h:L253-L263](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L253-L263)），它用于 `OpAdd` 作为「子表达式返回值」被内联求值的场景，日常配对请以 `OpAssign` 版本为准。

#### 4.5.3 源码精读：完整配对表

下表把本讲所有运算符的「声明 → 求值器特化 → Ascend C 指令」三段链路一次性对齐：

| 用户写法 | 表达式节点 | 求值器特化（math/tensor_evaluator.h） | Ascend C 指令 |
|---------|-----------|---------------------------------------|--------------|
| `a + b` / `a + s` | `OpAdd` | [Evaluator<OpAssign<T,OpAdd<U,V>>>](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L272-L301) | `Add` / `Adds`（标量） |
| `a - b` | `OpSub` | [L310-L345](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L310-L345) | `Sub` / `Subs`（标量, ARCH35） |
| `a * b` / `a * s` | `OpMul` | [L354-L383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L354-L383) | `Mul` / `Muls`（标量） |
| `a / b` | `OpDiv` | [L392-L427](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L392-L427) | `Div` / `Divs`（标量, ARCH35） |
| `Divs<K>(a)` | `OpDivs<K,T>` | [L436-L449](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L436-L449) | `Muls`（乘倒数优化） |
| `Exp(a)` | `OpExp` | [L458-L471](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L458-L471) | `Exp` |
| `Abs(a)` | `OpAbs` | [L480-L493](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L480-L493) | `Abs` |
| `Sqrt(a)` | `OpSqrt` | [L502-L515](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L515) | `Sqrt` |
| `Power<K>(a)` | `OpPower<K,T>` | [L524-L537](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L524-L537) | `Power` |
| `Cast<R>(a)` | `OpCast<m,R,T>` | [L546-L560](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L546-L560) | `Cast` |
| `CopyIn(p)` | `OpCopyIn` | [tensor_evaluator.h:L61-L85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L61-L85) | `DataCopyPad`（GM→UB） |
| `CopyOut(p)` | `OpCopyOut` | [tensor_evaluator.h:L89-L113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L89-L113) | `DataCopyPad`（UB→GM） |
| `out = var` | `OpAssign<T,OpCopy<U>>` | [tensor_evaluator.h:L117-L131](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L117-L131) | `DataCopy`（UB→UB） |
| `Alloc/Free` | `OpAlloc/OpFree` | [tensor_evaluator.h:L135-L200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L135-L200) | `bufPools.AllocTensor`/释放 |

官方对外暴露的运算符清单见 `docs/api/README.md` 的「表 2」（[docs/api/README.md:L138-L188](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/api/README.md#L138-L188)）：列为用户接口的有 `+ - * /`、`Exp`、`Power`、`Sqrt`、`Cast`、`Abs`。`Divs`、`Max` 等虽在源码中存在，但未列入该对外表。

#### 4.5.4 代码实践

**实践目标**：把「配对表」用在真实表达式上。这是本讲核心实践的预热，详细实现见第 5 节。

针对 `out = Exp(in1) + Sqrt(in2)`，先在纸上推导（暂不写代码）：

1. 该表达式会被展平成三条 `OpAssign`：
   - `t1 = Exp(in1)` → 匹配 `Evaluator<OpAssign<T, OpExp<U>>>` → `ExpAssign` → `AscendC::Exp`
   - `t2 = Sqrt(in2)` → 匹配 `Evaluator<OpAssign<T, OpSqrt<U>>>` → `SqrtAssign` → `AscendC::Sqrt`
   - `out = t1 + t2` → 匹配 `Evaluator<OpAssign<T, OpAdd<U,V>>>` → `AddAssign` → `AscendC::Add`（两侧都是张量，非标量）
2. **预期结果**：能口述「这一个用户表达式最终生成 3 条 Vector 指令」，并指出 `t1`、`t2` 是框架自动生成的 `LocalVar` 中间量。

#### 4.5.5 小练习与答案

**练习 1**：如果只声明了 `OpXxx` 但忘了写对应的 `Evaluator<OpAssign<T, OpXxx<U>>>` 特化，会发生什么？
**答案**：会命中 `Evaluator<T>` 主模板（未定义或无 `operator()`），在编译期报模板错误——算子「能写但不能跑」。这正是 4.2 节 `Max` 当前可能的状态：声明存在但 `math_evaluator.h` 中无对应特化。

**练习 2**：为什么 `Evaluator<OpAssign<T, OpAdd<U,V>>>` 要比 `Evaluator<OpAdd<T,U>>` 更「靠底层」？
**答案**：`OpAssign` 版本对应「展平后的最终执行语句」，直接调 `AscendC::Add`；而裸 `OpAdd<T,U>` 版本（[L253-L263](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L253-L263)）用于 `OpAdd` 作为子表达式被内联求值、返回一个中间表达式的场景，是「向上回接」的桥接求值器。

---

## 5. 综合实践

**任务**：用 ATVOSS 表达式实现 `out = Exp(in1) + Sqrt(in2)`，并指出每个运算符最终匹配到 `math_evaluator.h` 中的哪个 `Evaluator` 特化；再用 `Cast` 把 `in1` 从 `int32` 转为 `float`。

**步骤 1：编写 `Compute`（示例代码，参照 `abs.cpp` 骨架改写）**

下面是符合 ATVOSS 写法的示例代码（非项目原有文件，请自行放入一个新的 `Config` 结构体中）：

```cpp
// 示例代码：out = Exp(in1) + Sqrt(in2)
struct ExpSqrtCompute {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        // 两个输入、一个输出；序号 N 与 ArgumentsBuilder 的入参顺序一一对应
        auto in1 = Atvoss::PlaceHolder<1, Tensor<float>, Atvoss::ParamUsage::IN>();
        auto in2 = Atvoss::PlaceHolder<2, Tensor<float>, Atvoss::ParamUsage::IN>();
        auto out = Atvoss::PlaceHolder<3, Tensor<float>, Atvoss::ParamUsage::OUT>();
        // 一行公式；框架会自动插入 LocalVar 中间量并展平
        return (out = Exp(in1) + Sqrt(in2));
    }
};
```

**步骤 2：逐运算符指认配对**

- `Exp(in1)` → 展平后 `t1 = Exp(in1)` → [`Evaluator<OpAssign<T, OpExp<U>>>`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L458-L471) → `ExpAssign` → `AscendC::Exp`。
- `Sqrt(in2)` → 展平后 `t2 = Sqrt(in2)` → [`Evaluator<OpAssign<T, OpSqrt<U>>>`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L515) → `SqrtAssign` → `AscendC::Sqrt`。
- `+` → 展平后 `out = t1 + t2`（两侧均为张量）→ [`Evaluator<OpAssign<T, OpAdd<U,V>>>`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L272-L301) 的「else」分支 → `AddAssign` → `AscendC::Add`。

**步骤 3：加入 `Cast`（int32 → float）**

参照 `muls.cpp` 的 `MulsComputePromtIn` 模式（[examples/muls/muls.cpp:L40-L51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L40-L51)）：先用 `PlaceHolderTmpLike` 声明一个 `float` 类型的临时变量，再用 `Cast` 把 `int32` 输入转进去。

```cpp
// 示例代码：把 int32 的 in1 转成 float 后再做 Exp
auto in1   = Atvoss::PlaceHolder<1, Tensor<int32_t>, Atvoss::ParamUsage::IN>();
auto in1F  = Atvoss::PlaceHolderTmpLike<1, Tensor<float>>(in1);   // 与 in1 同序号空间的 LocalVar
// ...
return (in1F = Atvoss::Cast<Atvoss::CastMode::CAST_NONE, float>(in1),  // int32 -> float
        out = Exp(in1F) + Sqrt(in2));
```

- `Cast<float>(in1)` → [`Evaluator<OpAssign<T, OpCast<castMode, R, U>>>`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L546-L560) → `CastAssign` → `AscendC::Cast`（`DstType=float`，`SrcType=int32_t`，`RoundMode` 由 `CAST_NONE` 决定）。

**需要观察的现象与预期结果**：
- 逗号表达式 `(A, B)` 被 `OpAndThen` 串联（见 u2-l1），先执行 `Cast` 再执行 `Exp+Sqrt`，顺序得到保证。
- 整条表达式最终生成约 4 条 Vector 指令：`Cast`、`Exp`、`Sqrt`、`Add`。
- 若环境允许编译，可用 `bash scripts/build.sh -DSOC=ascend950 <your_example>` 验证能编译通过；运行结果**待本地验证**（需要真实硬件或 cannsim 仿真，参见 u1-l2）。

> 若你尚未熟悉「三级 Builder 组装 + `main()` 调用样板」，请回到 u1-l4、u1-l5 复习 `BlockBuilder → KernelBuilder → DeviceAdapter` 与 ACL 运行时流程，再回来把本节的 `Compute` 嵌进完整样例。

## 6. 本讲小结

- ATVOSS 运算符分两大类：**数学算子**（`math_expression.h`）描述「算什么」，**张量算子**（`tensor_expression.h`）描述「数据怎么搬、缓冲怎么管」。
- 四则运算 `+ - * /` 用运算符重载生成 `OpAdd/OpSub/OpMul/OpDiv` 节点；**同一个符号在求值器里用 `if constexpr` 按是否含标量，分派到 `Mul/Muls` 等不同 Ascend C 指令**——这是「极简编程」的关键。
- `Sqrt/Exp/Abs` 由 `DeclareUnaryOp` 宏一行声明，`Max` 由 `DeclareBinaryOp` 声明；声明只生成表达式节点，**不保证可执行**（如 `Max` 暂无求值器特化）。
- `Power/Divs` 把常量编码进类型（`auto scalarValue`），`Divs` 还会把「除以常数」优化成「乘以倒数」；`Cast<castMode, R>` 同时指定目标类型与 7 种 `CastMode` 舍入方式。
- 张量算子 `Alloc/Free/CopyIn/CopyOut/Copy` 通常由框架自动插入，分别落到 `bufPools.AllocTensor`、`DataCopyPad`（GM↔UB）、`DataCopy`（UB→UB），并触发 `PIPE_MTE2/MTE3/V` 同步。
- **核心心智模型**：每声明一个 `OpXxx`，就应有一个 `Evaluator<OpAssign<dst, OpXxx<...>>>` 特化与之配对，最终翻译成一条 Ascend C 指令——这就是「表达式声明 ↔ 求值器特化」的严格配对，也是「零运行时开销」的根源。

## 7. 下一步学习建议

- **u3-l1（求值器系统）**：本讲把求值器当成「配对表」使用，下一讲将深入 `Evaluator<T>` 主模板与递归求值机制，解释 `Param/LocalVar` 如何从 `ContextData` 取出张量、`OpAssign/OpAndThen` 如何递归求值。
- **u3-l2（Tile 层 Assign）**：本讲提到的 `Mutex` 锁、`PIPE_MTE2/MTE3/V` 同步、`pingPong` 双缓冲 id，将在那里完整展开。
- **u2-l4（归约与广播算子）**：若你关心 `ReduceSum/Broadcast` 这类跨轴运算符，它们的表达式与求值器在 `transcendental_*.h`，与本讲的逐元素算子结构平行。
- 建议同步阅读 `docs/api/README.md` 的两张接口表，把「官方对外接口」与「源码中全部算子」做一次对照，加深对「声明 vs 可用」边界的理解。
