# 表达式模板基础

## 1. 本讲目标

在上一篇（u1-l4）里，我们已经知道写一个 ATVOSS 算子时，用户唯一要动的就是 `Compute()` 里那一句 `return (out = Abs(in));`，并且知道这一行并不是「运行时赋值」，而是构造了一棵「编译期表达式」。本讲要回答的核心问题是：

- 这棵表达式树到底在 C++ 类型层面长什么样？
- 它为什么能实现「零运行时开销」？
- 运算符（`+ - * /`、`Abs`、`Sqrt`、`=`、逗号）各自对应到什么类型？
- 框架用什么手段让「新增一个算子」只需要一行宏？

学完本讲，你应当能够：

1. 用纸笔推导任意一行 ATVOSS 计算公式展开后的 `Expression<Op...>` 嵌套类型。
2. 说清楚 `UnaryOp`、`BinaryOp`、`TernaryOp`、`OpAssign`、`OpAndThen` 之间的类型关系。
3. 看懂 `DeclareUnaryOp` / `DeclareBinaryOp` 两个宏到底生成了什么，并能仿照它们理解 `math_expression.h` 里的每一个算子。

## 2. 前置知识

本讲默认你已经读过 u1-l4，知道以下事实：

- 用户在 `Compute()` 里用 `PlaceHolder<N, T, ParamUsage>` 声明入参/出参，并用 `return (out = ...)` 写公式。
- `PlaceHolder` 返回的是一个 `Expression<...>` 对象，公式里的 `=` 与数学函数都是**被重载的常量表达式（constexpr）**，它们组装出的是「类型化的表达式」，不会在运行时真正做计算。

此外，你需要一点 C++ 模板元编程的背景知识：

- **类型即数据**：在模板元编程里，信息可以编码在「类型」里而不是「对象的值」里。例如 `Param<1, Tensor<float>, IN>` 这个类型本身就携带了「序号是 1、张量类型是 float、用途是输入」三件事。
- **空基类优化（Empty Base Optimization, EBO）**：C++ 规定「空对象」也至少占 1 字节，但如果它作为**基类**被继承，编译器可以把它压缩成 0 字节。这是后面「零开销」的关键。
- **表达式模板（Expression Template）**：一种经典 C++ 技巧，用「嵌套的类型」把一个表达式（如 `a + b * c`）编码成一棵抽象语法树（AST），从而把对该表达式的处理（求值、优化、代码生成）推迟到编译期或专门的遍历器里完成。

> 关键直觉：ATVOSS 的表达式对象 **不存储任何运行时数据**。表达式树的全部结构都写在类型里；对象本身近乎「空壳」。这也是为什么后面 `BlockBuilder`、求值器、图优化 Pass 都是在「类型」上做文章，而几乎不读对象的值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/expression/expr_template.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h) | 表达式模板的核心头文件。定义 `Expression<T>`、`Param`/`LocalVar` 节点、`UnaryOp`/`BinaryOp`/`TernaryOp` 基类、`OpAssign`/`OpAndThen`、以及 `DeclareUnaryOp`/`DeclareBinaryOp` 宏。 |
| [include/operators/math_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h) | 数学算子库。基于上述核心，用「手写 + 宏」定义 `OpAdd/OpSub/OpMul/OpDiv/OpPower/OpDivs/OpCast` 以及 `Sqrt/Exp/Abs/Max`。 |
| [include/utils/utility.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h) | 类型元编程工具箱。提供 `CompressedData`/`CompressedPair`（EBO 存储）、`TypeList` 及其上的 `Filter/Concatenate/Unique/Find` 等操作。本讲把它当作「黑盒工具」引用。 |
| examples/abs/abs.cpp | 最简单的样例，`Compute()` 里只有 `return (out = Abs(in));`，本讲拿它做端到端的类型推导。 |

## 4. 核心概念与源码讲解

### 4.1 `Expression<T>` 与编译期 AST

#### 4.1.1 概念说明

`Expression<T>` 是所有 ATVOSS 表达式的**外层包装**。你可以把它理解成「给某个类型 `T` 贴上一张『我是一个表达式』的标签」。`T` 才是真正的内核，它可能是：

- 一个**叶子节点**：`Param<...>`（用户输入/输出占位）或 `LocalVar<...>`（临时变量）；
- 一个**内部节点**：`OpAdd<...>`、`OpAbs<...>`、`OpAssign<...>` 等运算节点。

整棵表达式树就是靠「`Expression<Op...< Expression里取出的内层类型 >...>`」一层层嵌套出来的。`Expression` 自身几乎不干活，它的职责只有两个：提供一个统一的容器外壳，以及承载被重载的运算符（`=`）。

#### 4.1.2 核心流程

当用户写下 `out = Abs(in)` 时，类型是这样一步步拼出来的：

```
in            : Expression< Param<1, Tensor<float>, IN> >
Abs(in)       : Expression< OpAbs< Param<1, Tensor<float>, IN> > >     ← 由 Abs 的重载包装
out = Abs(in) : Expression< OpAssign< Param<2, Tensor<float>, OUT>, OpAbs< Param<1, ...> > > >
```

注意每一层都把**内层表达式剥掉 `Expression` 外壳、只留下内核类型 `T`**，再套进新的 `Op...` 里，最后重新包回 `Expression<...>`。所以表达式树是「内核类型」的嵌套，`Expression` 只在最外层出现一次。这种「外层 `Expression`、内核递归」的结构，就是一棵编译期 AST。

#### 4.1.3 源码精读

`Expression` 的定义非常短：

```cpp
template <typename T>
struct Expression {
    static_assert(!std::is_rvalue_reference_v<T>, "...Rvalue references cannot be stored");
    using Type = T;
    using RetType = typename std::conditional_t<std::is_scalar_v<T>, Util::RetTypeWrapper<T>, T>::RetType;
    using TensorType = typename std::conditional_t<std::is_scalar_v<T>, Util::TensorTypeWrapper<T>, T>::TensorType;

    T const data{};

    template <typename U>
    [[nodiscard]] constexpr auto operator=(Expression<U> u);
};
```

见 [include/expression/expr_template.h:49-60](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L49-L60)。关键点：

- `T const data{};`：唯一的成员，`const` 修饰、默认值初始化为 `{}`。对叶子/运算节点这种「空类型」，`{}` 什么都不做；这也意味着表达式对象之间**不能互相赋值**（const 成员不可赋值），符合「表达式是值不可变的类型描述」的语义。
- `RetType` / `TensorType`：用 `std::conditional_t` 区分两种情况——当 `T` 是标量（scalar，比如一个 `float`）时，从 `RetTypeWrapper<T>` 里取；否则直接从 `T` 里取。这让表达式既能装「张量节点」，也能装「标量节点」（标量参与运算时用到，见 u2-l3）。
- `operator=`：只声明、延后定义（见 4.3）。它是 `[[nodiscard]]` 的——丢弃返回值会告警，因为「赋值」本身只是个表达式构造，不写进树里就白写了。

文件里还有一个推导指引：

```cpp
template <typename T>
Expression(T&& value) -> Expression<T>;
```

见 [include/expression/expr_template.h:63-64](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L63-L64)。它让 `Expression{某对象}` 能自动把 `T` 推导为「值类型」或「左值引用类型」，框架内部构造表达式时会用到。

> **「零运行时开销」从何而来？** 留意上面 `T const data{}`：当 `T` 是 `Param`、`OpAdd` 这类「只有类型别名和 `static constexpr`、没有任何非静态数据成员」的空类时，`sizeof(Expression<T>)` 几乎为 0（受 EBO 影响）。也就是说，构造一个表达式对象根本不分配任何业务数据，整棵 AST 完全活在「类型」里，运行期连一比特的计算信息都不携带。这正是表达式模板能做到「和手写一样快」的根本原因。

> 说明：这里的 EBO 是通过 `UnaryOp` / `BinaryOp` 内部使用的 `CompressedData` / `CompressedPair` 实现的，详见 4.2.3。

#### 4.1.4 代码实践

**实践目标**：亲手把 abs 样例的一行公式翻译成类型。

**操作步骤**：

1. 打开 [examples/abs/abs.cpp:24-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L24-L32)，确认 `Compute()` 里就是：

   ```cpp
   auto in  = Atvoss::PlaceHolder<1, Tensor<Dtype>, Atvoss::ParamUsage::IN>();
   auto out = Atvoss::PlaceHolder<2, Tensor<Dtype>, Atvoss::ParamUsage::OUT>();
   return (out = Abs(in));
   ```

2. 在纸上按下表逐项填写（设 `Dtype = float`、`Tn = Tensor<float>`）：

   | 代码片段 | 推导出的类型 |
   | --- | --- |
   | `in` | `Expression< Param<1, Tn, IN> >` |
   | `out` | `Expression< Param<2, Tn, OUT> >` |
   | `Abs(in)` | `Expression< OpAbs< Param<1, Tn, IN> > >` |
   | `out = Abs(in)` | `Expression< OpAssign< Param<2, Tn, OUT>, OpAbs< Param<1, Tn, IN> > > >` |

3. 对照 4.3.3 里 `operator=` 的源码，确认最外层确实落到了 `OpAssign`。

**需要观察的现象**：类型层层嵌套、`Expression` 只出现一次。

**预期结果**：你应当得到一棵「叶子是两个 `Param`、中间是 `OpAbs`、根是 `OpAssign`」的两层 AST。运行命令**待本地验证**（本实践是纸笔推导，无需编译；若想验证，可写一个 `static_assert(std::is_same_v<...>)` 比较类型，但需要搭建 ATVOSS 编译环境，见 u1-l2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Expression<T>` 把唯一成员声明成 `T const data{}`，而不是 `T data{}`？

> **参考答案**：因为表达式对象描述的是「不可变的计算结构」，类型一旦定下来就不该再改。`const` 既表达了这种语义，也让编译器在 EBO 场景下放心优化；同时它顺带禁止了「把一个表达式赋给另一个同类型表达式」这种容易让人误以为是「运行时赋值」的危险操作。

**练习 2**：`Expression` 的 `operator=` 为什么标注 `[[nodiscard]]`？

> **参考答案**：`out = expr` 的返回值才是真正进入 AST 的表达式（一个 `Expression<OpAssign<...>>`）。如果你写了 `out = expr;` 却不 `return` 它，这棵树就丢了，`Compute()` 会返回默认值，算子逻辑相当于没写。`[[nodiscard]]` 让编译器在你「丢弃表达式结果」时给出告警。

---

### 4.2 `UnaryOp` / `BinaryOp` / `TernaryOp` 基类

#### 4.2.1 概念说明

运算节点（如 `OpAdd`、`OpAbs`）需要一个统一的「基座」来存放它的子表达式，并对外暴露统一的类型别名（`LhsType`/`RhsType`/`DataType`、`RetType`、`TensorType`）和统一的「这是个什么运算」的标签（`IsUnaryOp`/`IsBinaryOp`）。ATVOSS 提供三个基类：

- `UnaryOp<T>`：一元运算，存一个子表达式 `T`（如 `Abs`、`Sqrt`、`Exp`）。
- `BinaryOp<T, U>`：二元运算，存两个子表达式 `T`、`U`（如 `Add`、`Mul`、`Assign`）。
- `TernaryOp<T, U, V>`：三元运算，存三个子表达式（框架内部扩展用，本讲不展开具体算子）。

它们都是**空类型**（没有非静态数据成员意义上的「业务数据」），这是实现 EBO 的前提。

#### 4.2.2 核心流程

以 `OpAdd<Param<1>, Param<1>>` 为例：

```
OpAdd<Param<1>, Param<1>>  继承自  BinaryOp<Param<1>, Param<1>>
                                     └── 用 CompressedPair 存储 (Param<1>, Param<1>)
                                     └── 暴露 LhsType=Param<1>, RhsType=Param<1>, IsBinaryOp=void
```

后续的图分析 Pass（如 `ParamCollector`）会通过 `IsUnaryOp_v` / `IsBinaryOp_v` 判断一个节点是几元运算，再顺着 `DataType`（一元）或 `LhsType`/`RhsType`（二元）递归遍历整棵树，把所有用到的 `Param` 收集起来——这就是 u2-l2 要讲的「入参/出参收集」的基础。

#### 4.2.3 源码精读

先看 `UnaryOp`：

```cpp
template <typename T, typename R = typename std::decay_t<T>::RetType>
struct UnaryOp : private Util::CompressedData<T> {
private:
    using Storage = Util::CompressedData<T>;
public:
    static_assert(!std::is_rvalue_reference_v<T>, "...");
    using IsUnaryOp = void;
    using DataType = T;
    using TensorType = typename T::TensorType;
    using RetType = R;

    UnaryOp() = default;
    constexpr UnaryOp(T t) : Storage(t) {}
    constexpr const T& GetData() const { return Storage::Data(); }
};
```

见 [include/expression/expr_template.h:383-404](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L383-L404)。要点：

- `using IsUnaryOp = void;`：这是一个**标签**。后面 `IsUnaryOp<T>` 用 SFINAE 检测「`T` 里有没有 `IsUnaryOp` 这个成员」，从而判断 `T` 是不是一元运算节点，见 [include/expression/expr_template.h:133-140](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L133-L140)。
- `DataType = T`：指向唯一的子表达式。
- `RetType`：默认取 `T::RetType`，即「结果的张量类型跟随子表达式」；模板参数 `R` 允许派生类覆盖（`Cast` 就会覆盖它，因为类型转换会改变张量类型）。
- 私有继承 `CompressedData<T>`：见下文。

`BinaryOp` 结构几乎对称：

```cpp
template <typename T, typename U, typename R = typename std::decay_t<T>::RetType>
struct BinaryOp : private Util::CompressedPair<T, U> {
private:
    using Storage = Util::CompressedPair<T, U>;
public:
    static_assert(!(std::is_rvalue_reference_v<T> || std::is_rvalue_reference_v<U>), "...");
    using IsBinaryOp = void;
    using LhsType = T;
    using RhsType = U;
    using TensorType = typename T::TensorType;
    using RetType = R;
    // GetLhs() / GetRhs() 访问两个子表达式
};
```

见 [include/expression/expr_template.h:406-434](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L406-L434)。`TernaryOp` 见 [include/expression/expr_template.h:436-451](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L436-L451)，多了 `VhsType` 和第三个成员 `ths`，但为了复用 `IsBinaryOp` 的检测逻辑，它也声明了 `using IsBinaryOp = void;`（当作「广义二元」处理）。

**关于「零开销」的存储层**：`UnaryOp` 私有继承 `CompressedData<T>`、`BinaryOp` 私有继承 `CompressedPair<T, U>`。这两个工具定义在 [include/utils/utility.h:277-309](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L277-L309) 和 [include/utils/utility.h:331-378](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L331-L378)。当被存的类型是「空类」（`is_empty_v` 为真）时，它们会退化为「空基类」存储（见 [include/utils/utility.h:92-109](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L92-L109)），从而把存储压缩到 0 字节。这就是 4.1 里「`sizeof(Expression<Param<...>>)` 几乎为 0」的真正机制。

#### 4.2.4 代码实践

**实践目标**：用类型特征验证「一个运算节点确实是二元运算、且能取出左右子表达式类型」。

**操作步骤**：

1. 阅读 `IsBinaryOp` 的 SFINAE 检测：[include/expression/expr_template.h:142-149](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L142-L149)。它的原理是「能访问 `T::IsBinaryOp` 就继承 `true_type`，否则 `false_type`」。
2. 假设你已经按 4.1.4 推导出 `E = Expression<OpAssign<Param<2,...>, OpAbs<Param<1,...>>>>`。在纸上判断：
   - `E::Type`（即 `OpAssign<...>`）是二元还是一元？→ 看 `OpAssign` 的定义（见 4.3.3），它继承 `BinaryOp`，所以 `IsBinaryOp_v` 为真。
   - 它的 `LhsType` 和 `RhsType` 分别是什么？→ `Param<2,...>` 和 `OpAbs<Param<1,...>>`。
   - `RhsType`（`OpAbs<...>`）又是一元还是二元？→ `OpAbs` 由 `DeclareUnaryOp` 生成、继承 `UnaryOp`，所以 `IsUnaryOp_v` 为真，其 `DataType` 是 `Param<1,...>`。

**需要观察的现象**：用 `IsUnaryOp_v` / `IsBinaryOp_v` 配合 `DataType` / `LhsType` / `RhsType`，可以从根节点一路「走」到叶子节点，这正是后续遍历器的工作方式。

**预期结果**：你会得到一条「`OpAssign`（二元）→ `OpAbs`（一元）→ `Param<1>`（叶子）」的访问路径。运行验证**待本地验证**（同样需要 ATVOSS 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`UnaryOp` 和 `BinaryOp` 为什么用 `using IsUnaryOp = void;` / `using IsBinaryOp = void;` 这种「空成员别名」来做标签，而不是用 `enum` 或 `static constexpr bool`？

> **参考答案**：因为 SFINAE 最自然的写法就是「探测某个类型成员是否存在」（`std::void_t<typename T::IsUnaryOp>`）。用空别名做成员名，存在即代表「是」，`void_t` 探测最简洁；它不占空间、也不参与运算，纯粹是个「类型层的小旗子」。

**练习 2**：`BinaryOp` 的 `RetType` 默认是 `typename std::decay_t<T>::RetType`，也就是「跟随左操作数」。请结合 `Cast`（类型转换会改变张量类型）想一想，这个默认值在什么情况下不够用、需要派生类显式覆盖？

> **参考答案**：当一个运算的「结果张量类型」和「输入张量类型」不一致时，默认值就不对。最典型的就是 `Cast`：输入是 `Tensor<int>`、输出是 `Tensor<float>`，必须显式指定 `RetType`。看 [include/operators/math_expression.h:171-177](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L171-L177)，`OpCast` 正是把第二个模板实参显式设成了 `ReplaceTensorType<...>::Type`，覆盖了默认的 `R`。

---

### 4.3 `OpAssign` 与 `OpAndThen`：赋值与顺序执行

#### 4.3.1 概念说明

到目前为止，我们只造出了「表达式」，还没解决两个关键问题：

1. **怎么把一个表达式的结果「写」到某个变量上？** —— 这就是 `OpAssign`（赋值节点）。
2. **怎么表达「先做 A，再做 B」的顺序执行？** —— 这就是 `OpAndThen`（顺序节点），它在源码里由逗号运算符 `(a, b)` 触发。

两者本质上都是 `BinaryOp` 的特化，但语义不同：`OpAssign` 的左子树是「赋值目标」（一个 `Param` 或 `LocalVar`），右子树是「待计算的表达式」；`OpAndThen` 的左右子树是「按顺序执行的两个表达式」。

#### 4.3.2 核心流程

赋值的触发链路：

```
用户写：out = expr
        ─────────────
out 是 Expression<Param<2,...>>，expr 是 Expression<SomeOp>
        ↓ 触发 Expression<T>::operator=(Expression<U>)
        ↓ 校验左边必须是 Param/LocalVar/引用，否则 static_assert 报错
返回：Expression< OpAssign< Param<2,...>, SomeOp > >
```

顺序执行的触发链路：

```
用户写：(stmtA, stmtB)
        ────────────────
        ↓ 触发 operator,(Expression<T>, Expression<U>)
返回：Expression< OpAndThen<T, U> >
```

注意 `OpAndThen` 的返回类型沿用了 C++ 逗号表达式的语义——「整个逗号表达式的值是最后一个子表达式」，所以它的 `RetType` 取的是右子树 `U` 的 `RetType`。

#### 4.3.3 源码精读

`OpAssign` 极简，直接继承 `BinaryOp`：

```cpp
template <typename T, typename U>
struct OpAssign : BinaryOp<T, U> {
    OpAssign() = default;
    constexpr OpAssign(T t, U u) : BinaryOp<T, U>(t, u) {}
};
```

见 [include/expression/expr_template.h:453-458](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L453-L458)。配合一个特征 `IsOpAssign`（[include/expression/expr_template.h:460-467](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L460-L467)），后续求值器可以专门识别它。

赋值的真正逻辑在 `Expression<T>::operator=`：

```cpp
template <typename T>
template <typename U>
__host_aicore__ constexpr auto Expression<T>::operator=(Expression<U> u)
{
    static_assert(
        (IsParam_v<T> || IsLocalVar_v<T> || std::is_lvalue_reference_v<T>),
        "...Only a Param, LocalVar, or reference can appear on the left side of assignment");
    if constexpr (IsLocalVar_v<U> || IsParam_v<U>) {
        constexpr auto result = Atvoss::OpCopy<U>(u.data);
        return Expression<OpAssign<T, std::decay_t<decltype(result)>>>{{data, u.data}};
    } else {
        return Expression<OpAssign<T, U>>{{data, u.data}};
    }
}
```

见 [include/expression/expr_template.h:469-483](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L469-L483)。两点解读：

1. **左值校验**：`static_assert` 强制左边只能是 `Param`、`LocalVar` 或引用，杜绝「把一个临时表达式赋给另一个临时表达式」这种无意义写法。
2. **`OpCopy` 分支**：当右边**只是一个裸的 `Param`/`LocalVar`**（比如 `out = in`，纯变量搬运、没有任何运算）时，框架会插入一个 `OpCopy` 节点（`OpCopy` 在 [include/expression/expr_template.h:30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L30) 前置声明，实现在 tensor 算子头里），表达「这里需要一次显式拷贝」。其余情况（右边是带运算的表达式）直接生成 `OpAssign<T, U>`。

`OpAndThen` 同样简洁：

```cpp
template <typename T, typename U>
struct OpAndThen : BinaryOp<T, U, typename U::RetType> {
    OpAndThen() = default;
    constexpr OpAndThen(T t, U u) : BinaryOp<T, U, typename U::RetType>(t, u) {}
};
```

见 [include/expression/expr_template.h:491-496](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L491-L496)。注意第三个模板实参显式传了 `typename U::RetType`——让 `OpAndThen` 的 `RetType` 跟随右子树，复刻 C++ 逗号表达式的求值规则。

逗号运算符的重载：

```cpp
template <typename T, typename U>
__host_aicore__ constexpr auto operator,(Expression<T> t, Expression<U> u)
{
    return Expression<OpAndThen<T, U>>{{t.data, u.data}};
}
```

见 [include/expression/expr_template.h:533-537](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L533-L537)。同时，为了防止写出危险表达式，`math_expression.h` 里**删除**了「左操作数是 `Expression`、右操作数不是」的逗号重载：

```cpp
template <typename T, typename U>
__host_aicore__ constexpr auto operator,(Expression<T> t, U&& u) = delete;
```

见 [include/operators/math_expression.h:18-19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L18-L19)。这样像 `(Expression{2}, 3)` 这种「逗号右边混入普通值」的写法会直接编译失败，避免逗号被意外降级成 C++ 内置语义。

**嵌套逗号的展开**：多个语句 `(a, b, c)` 会层层套成 `OpAndThen<OpAndThen<A, B>, C>`。框架用 `FlattenAtOpAndThen`（[include/expression/expr_template.h:540-548](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L540-L548)）把它递归拍平成一个 `TypeList<A, B, C>`，供后续线性化 Pass（u3-l4）按顺序处理。

#### 4.3.4 代码实践

**实践目标**：理解「顺序执行」在表达式层是如何编码的。

**操作步骤**：

1. 假设一个 `Compute()` 里有两条语句：

   ```cpp
   // 示例代码（仅用于推导，不是项目原有代码）
   auto in   = PlaceHolder<1, Tensor<float>, IN>();
   auto tmp  = PlaceHolderTmpLike<1, Tensor<float>>(in);   // LocalVar，见 u2-l2
   auto out  = PlaceHolder<2, Tensor<float>, OUT>();
   return (tmp = Sqrt(in), out = Abs(tmp));               // 两条语句用逗号串联
   ```

2. 先分别求出两条赋值语句的类型：
   - `tmp = Sqrt(in)` → `Expression< OpAssign<LocalVar<1,...>, OpSqrt<Param<1,...>>> >`，记为 `S1`。
   - `out = Abs(tmp)` → `Expression< OpAssign<Param<2,...>, OpAbs<LocalVar<1,...>>> >`，记为 `S2`。
3. 再求 `(S1, S2)`：根据 [include/expression/expr_template.h:534-536](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L534-L536)，结果是：

   ```
   Expression< OpAndThen< OpAssign<LocalVar<1>, OpSqrt<Param<1>>>,
                          OpAssign<Param<2>,   OpAbs<LocalVar<1>>> > >
   ```

4. 思考：如果误写成 `(Sqrt(in), 3)`，会发生什么？→ 命中 `math_expression.h:19` 的 `= delete`，编译失败。

**需要观察的现象**：两条语句的先后顺序被「左嵌套」地保留在类型里（`S1` 在外层 `OpAndThen` 的左边）。

**预期结果**：你应当能用 `FlattenAtOpAndThen` 的递归规则（左、右各拍平再拼接）推出，上面的嵌套类型会被拍平成 `TypeList< OpAssign<...Sqrt...>, OpAssign<...Abs...> >`，顺序与书写一致。运行验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`out = in`（左右都是裸 `Param`）和 `out = Abs(in)`（右边是运算）生成的 `OpAssign` 第二个模板参数有什么不同？

> **参考答案**：前者会走 `operator=` 里的 `if constexpr` 分支，插入 `OpCopy<Param<1>>`，得到 `OpAssign<Param<2>, OpCopy<Param<1>>>`；后者走 `else` 分支，直接得到 `OpAssign<Param<2>, OpAbs<Param<1>>>`。区别在于：纯变量搬运会被显式标记成一次「拷贝」操作，方便后续搬运 Pass 识别。

**练习 2**：为什么 `OpAndThen` 要把 `RetType` 显式设成 `U::RetType`，而不沿用 `BinaryOp` 默认的「左操作数」？

> **参考答案**：因为逗号表达式 `(a, b)` 在 C++ 里的值是 `b`。后续如果有人把 `OpAndThen` 的整体结果再喂给另一个运算，应当拿「最后一条语句」的结果类型来参与推导，而不是第一条。显式传 `typename U::RetType` 正是为了复刻这一语义。

---

### 4.4 `DeclareUnaryOp` / `DeclareBinaryOp` 宏

#### 4.4.1 概念说明

如果每新增一个算子都要手写「`OpXxx` 结构体 + 三四个运算符重载」，重复劳动会非常多。ATVOSS 用两个宏把这套样板封装起来：

- `DeclareUnaryOp(Name)`：生成一元算子 `OpName` 及其工厂函数 `Name(...)`。
- `DeclareBinaryOp(Name)`：生成二元算子 `OpName` 及其工厂函数 `Name(...)`（覆盖三种参数组合）。

在 `math_expression.h` 里，`Sqrt`、`Exp`、`Abs` 用宏声明，`Max` 也用宏声明；而 `Add/Sub/Mul/Div` 因为要用 C++ 的运算符符号（`+ - * /`）而非函数名，选择手写。两者对比着看，宏的作用就一目了然。

#### 4.4.2 核心流程

以 `DeclareUnaryOp(Sqrt)` 为例，宏展开后等价于：

```cpp
// 1. 运算节点：继承 UnaryOp，得到 IsUnaryOp 标签、DataType、GetData() 等
template <typename T>
struct OpSqrt : UnaryOp<T> {
    OpSqrt() = default;
    constexpr OpSqrt(T t) : UnaryOp<T>(t) {}
};

// 2. 工厂函数：接收一个 Expression，返回包好的 Expression<OpSqrt<...>>
template <typename T>
constexpr auto Sqrt(Expression<T> lhs) {
    return Expression<OpSqrt<T>>{{lhs.data}};
}

// 3. （重载）接收一个非 Expression 的左值/右值
template <typename T>
constexpr auto Sqrt(T&& lhs) {
    return Expression<OpSqrt<T>>{{std::forward<T>(lhs)}};
}
```

所以写 `Sqrt(in)` 就会返回 `Expression<OpSqrt<Param<1,...>>>`。二元版 `DeclareBinaryOp(Name)` 结构相同，只是子表达式从 1 个变 2 个、工厂函数多一种参数组合。

#### 4.4.3 源码精读

两个宏的定义：

```cpp
// declare unary op
#define DeclareUnaryOp(Name)                                    \
    template <typename T>                                       \
    struct Op##Name : UnaryOp<T> {                              \
        Op##Name() = default;                                   \
        constexpr Op##Name(T t) : UnaryOp<T>(t)                 \
        {}                                                      \
    };                                                          \
    template <typename T>                                       \
    __host_aicore__ constexpr auto Name(Expression<T> lhs)      \
    {                                                           \
        return Expression<Op##Name<T>>{{lhs.data}};             \
    }                                                           \
    template <typename T>                                       \
    __host_aicore__ constexpr auto Name(T&& lhs)                \
    {                                                           \
        return Expression<Op##Name<T>>{{std::forward<T>(lhs)}}; \
    }
```

见 [include/expression/expr_template.h:551-567](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L551-L567)。`DeclareBinaryOp` 见 [include/expression/expr_template.h:570-591](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L570-L591)，要点一致：生成 `Op##Name : BinaryOp<T, U>`，并提供「`(Expr, Expr)` / `(Expr, value)` / `(value, Expr)`」三种重载。

注意几个细节：

- `__host_aicore__`：这是昇腾 C++ 的属性，表示该函数「既能在 Host（CPU）编译、也能在 AI Core（Device）编译」。因为表达式构造发生在 `Compute()` 里，而 `Compute()` 既要被 Host 侧的 tiling 计算实例化、又要被 Device 侧的 kernel 代码实例化。
- 两个工厂函数重载（一个吃 `Expression<T>`、一个吃 `T&&`）：是为了兼容「参数已经是表达式」和「参数是裸 `Param`/`LocalVar`（不是表达式外壳）」两种调用方式。注意它们都会重新包成 `Expression<Op...>`，所以最终返回值形式统一。

宏的实际使用处：

```cpp
DeclareUnaryOp(Sqrt);
DeclareUnaryOp(Exp);
DeclareUnaryOp(Abs);
...
DeclareBinaryOp(Max);
```

见 [include/operators/math_expression.h:165-169](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L165-L169) 和 [include/operators/math_expression.h:200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L200)。

**对比：为什么 `Add/Sub/Mul/Div` 不用宏？** 因为它们要绑定 C++ 运算符符号。看 `OpAdd` 及其重载：

```cpp
template <typename T, typename U>
struct OpAdd : BinaryOp<T, U> {
    OpAdd() = default;
    constexpr OpAdd(T t, U u) : BinaryOp<T, U>(t, u) {}
};

template <typename T, typename U>
__host_aicore__ constexpr auto operator+(Expression<T> lhs, Expression<U> rhs)
{
    return Expression<OpAdd<T, U>>{{lhs.data, rhs.data}};
}
// ... 还有 operator+(Expression, T&&) 和 operator+(T&&, Expression) 两个重载
```

见 [include/operators/math_expression.h:21-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L21-L45)。结构其实和宏生成的二元算子一模一样，只是函数名从 `Name` 换成了 `operator+`。`OpSub/OpMul/OpDiv` 见 [include/operators/math_expression.h:47-123](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L47-L123)，完全同构。

> 还有一类「带模板参数」的算子，宏没法直接覆盖，必须手写：
> - `Power<scalarValue>(...)`、`Divs<scalarValue>(...)`：标量作为**非类型模板参数**参与，见 [include/operators/math_expression.h:125-163](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L125-L163)。
> - `Cast<castMode, R>(...)`：带类型转换目标 `R` 与舍入模式 `CastMode`（取值见 [include/utils/patterns.h:22-30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L22-L30)），见 [include/operators/math_expression.h:171-198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L171-L198)。

#### 4.4.4 代码实践

**实践目标**：把一个「手写二元算子」与「宏声明」对照，验证它们等价。

**操作步骤**：

1. 打开 [include/operators/math_expression.h:21-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L21-L45)（`OpAdd` + 三个 `operator+` 重载）。
2. 把 [include/expression/expr_template.h:570-591](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L570-L591) 的 `DeclareBinaryOp` 宏里 `Name` 全部替换成 `Add`，逐行对照。
3. 你会发现：`struct OpAdd : BinaryOp<T,U>` ↔ 宏里的 `struct Op##Name : BinaryOp<T, U>`；三个 `operator+` ↔ 宏里的三个 `Name(...)` 工厂函数。**唯一区别**是函数名 `Add` vs `operator+`。
4. 进阶思考：如果让你新增一个逐元素「取负」算子 `Neg`（数学上 `Neg(x) = 0 - x`），你会选哪种写法？
   - **复用减法**：直接 `return (out = ScalarConst - in);`（最省事，但需要标量入参，u2-l3 详谈）。
   - **手写一元算子**：照抄 `OpAdd` 改名 `OpNeg`、继承 `UnaryOp`、再写一个工厂函数 `Neg(...)`，或直接 `DeclareUnaryOp(Neg)`。后者只需一行宏（前提是你愿意接受「`Neg` 以函数形式而非运算符形式调用」）。

**需要观察的现象**：宏展开后的代码与手写算子在「结构、重载数量、返回类型」上完全一致。

**预期结果**：你应当得出结论——宏只是「省去重复敲键盘」的工具，没有引入任何新机制；理解了手写的 `OpAdd`，就理解了宏声明的 `Abs/Sqrt/Max`。本实践为纸笔阅读型，**待本地验证**（若要真的加一个 `Neg` 算子并编译，需搭建 u1-l2 的环境）。

#### 4.4.5 小练习与答案

**练习 1**：`DeclareUnaryOp` 生成的工厂函数为什么要有两个重载（一个吃 `Expression<T>`、一个吃 `T&&`）？

> **参考答案**：因为调用点可能传入两种东西——已经被包成 `Expression` 的子表达式（如 `Abs(in)`，`in` 是 `Expression<Param<...>>`），或者尚未包成 `Expression` 的裸值（在某些内部构造路径里）。两个重载确保无论传哪种，最终都能产出统一的 `Expression<OpName<...>>`。

**练习 2**：如果想新增一个二元函数式算子 `Min`（逐元素取小），最少要写多少代码？

> **参考答案**：一行 `DeclareBinaryOp(Min);` 即可（和 `Max` 完全对称，见 [include/operators/math_expression.h:200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L200)）。它会自动生成 `OpMin` 和 `Min(...)` 工厂函数。当然，要让它真正在 NPU 上跑起来，还需在求值器里补一个 `Evaluator<OpMin<...>>` 特化（见 u3-l1），本讲只覆盖「表达式声明」这一层。

---

## 5. 综合实践

把本讲四个模块串起来，做一个完整的「纸笔推导」任务。

**任务**：给定下面这个虚构的 `Compute()`（示例代码，仅用于推导，不是项目原有代码）：

```cpp
// 示例代码
auto in1 = PlaceHolder<1, Tensor<float>, IN>();
auto in2 = PlaceHolder<2, Tensor<float>, IN>();
auto out = PlaceHolder<3, Tensor<float>, OUT>();
return (out = (in1 + in1) * in2);
```

要求：

1. **逐层推导最终返回值的完整 C++ 类型**，写到最外层 `Expression<...>`。
2. **画出这棵 AST**：标注每个内部节点是 `OpAdd`/`OpMul`/`OpAssign` 中的哪一个、属于一元还是二元、叶子节点是哪些 `Param`。
3. **指出每一层对应的 Op 结构体来源**：`OpAdd`、`OpMul` 来自 [include/operators/math_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h)（手写运算符重载），`OpAssign` 来自 [include/expression/expr_template.h:453-458](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L453-L458)。
4. **追问**：如果把公式改成 `(out = in1 + in1, out = out * in2)`（两条语句用逗号串联，复用同一个 `out`），最外层会从 `OpAssign` 变成什么？拍平后的 `TypeList` 长什么样？

**参考答案要点**：

1. 完整类型（记 `Pn = Param<n, Tensor<float>, 对应usage>`，其中 `in1` 是 `P1/IN`、`in2` 是 `P2/IN`、`out` 是 `P3/OUT`）：

   ```
   Expression<
     OpAssign< P3,
               OpMul< OpAdd<P1, P1>,
                      P2 > > >
   ```

2. AST 示意：

   ```
   OpAssign (二元)
   ├── 左: Param<3> (out)
   └── 右: OpMul (二元)
           ├── 左: OpAdd (二元)
           │       ├── Param<1>
           │       └── Param<1>
           └── 右: Param<2>
   ```

3. `OpAdd`、`OpMul` 见 [include/operators/math_expression.h:21-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L21-L45) 与 [include/operators/math_expression.h:73-97](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L73-L97)。

4. 改写后最外层变成 `OpAndThen<OpAssign<P3, OpAdd<P1,P1>>, OpAssign<P3, OpMul<P3, P2>>>`；用 `FlattenAtOpAndThen` 拍平后得到 `TypeList< OpAssign<P3, OpAdd<P1,P1>>, OpAssign<P3, OpMul<P3,P2>> >`——这正是 u3-l4 线性化 Pass 的输入。

## 6. 本讲小结

- ATVOSS 的表达式是「**类型即结构、对象空壳**」的编译期 AST：整棵树的形态编码在 `Expression<T>` 的嵌套类型里，对象本身因 EBO 几乎不占空间，这是「零运行时开销」的根源。
- `Expression<T>` 是统一外壳；叶子节点是 `Param`/`LocalVar`，内部运算节点继承 `UnaryOp`/`BinaryOp`/`TernaryOp`，后者用 `CompressedData`/`CompressedPair` 存子表达式并对齐 EBO。
- 每种节点都用一个「空成员别名」打标签（`IsUnaryOp`/`IsBinaryOp`/`IsOpAssign`），配合 `DataType`/`LhsType`/`RhsType` 暴露子树，供后续遍历器递归处理。
- `OpAssign`（由 `operator=` 触发）负责把表达式结果写到 `Param`/`LocalVar`；`OpAndThen`（由逗号 `operator,` 触发）负责串联顺序执行，其 `RetType` 取右子树，复刻 C++ 逗号语义。
- `DeclareUnaryOp`/`DeclareBinaryOp` 两个宏只是「省键盘」的样板生成器，与手写的 `OpAdd`/`operator+` 同构；带模板参数的算子（`Power`/`Divs`/`Cast`）仍需手写。
- 在 `math_expression.h` 里，`+ - * /` 走手写运算符重载，`Abs/Sqrt/Exp/Max` 走宏，二者底层完全一致。

## 7. 下一步学习建议

本讲把「表达式长什么样」讲清楚了，但还有几个紧邻的问题没有回答：

- **叶子节点 `Param`/`LocalVar` 的细节**：`Param<N, T, Usage, RN>` 的 `RN` 是做什么的？`Usage`（IN/OUT/IN_OUT）如何被自动收集成入参/出参列表？`PlaceHolderTmpLike` 又怎么基于一个 `Param` 派生同类型临时变量？→ 进入 **u2-l2 参数、占位符与临时变量**。
- **每个 Op 最终怎么变成 Ascend C 指令**：表达式只是「描述」，谁来「执行」？→ 进入 **u3-l1 求值器系统**，那里会讲 `Evaluator<OpAssign<...>>` 等模板特化如何递归地把 AST 翻译成硬件调用。
- **运算符的全景**：本讲只覆盖了数学算子的「声明形态」，标量怎么参与、`Cast` 的舍入模式怎么选、张量搬运算子（`OpCopyIn/OpCopyOut`）长什么样？→ 进入 **u2-l3 运算符库：数学与张量算子**。

建议阅读顺序：u2-l2（把叶子节点补全）→ u2-l3（把算子库补全）→ u3-l1（从「声明」走到「执行」）。
