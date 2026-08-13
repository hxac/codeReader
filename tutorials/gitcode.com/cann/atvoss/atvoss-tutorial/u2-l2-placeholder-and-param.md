# 参数、占位符与临时变量

## 1. 本讲目标

在上一篇（u2-l1）里，我们把视线放在了表达式树的**内部节点**——`UnaryOp`/`BinaryOp`/`TernaryOp`/`OpAssign`/`OpAndThen` 这些运算节点，搞清楚了「一行公式如何变成一棵 `Expression<Op...>` 嵌套类型」。但我们一直把树的**叶子节点**当作黑盒：那句

```cpp
auto in  = Atvoss::PlaceHolder<1, Tensor<Dtype>, Atvoss::ParamUsage::IN>();
auto out = Atvoss::PlaceHolder<2, Tensor<Dtype>, Atvoss::ParamUsage::OUT>();
```

里的 `PlaceHolder` 到底造出了什么？`Param` 又是怎么把「这是第几个参数、是输入还是输出」编码进类型里的？本讲就要回答这些问题。

学完本讲，你应当能够：

1. 区分两种叶子节点：`Param`（函数入参/出参占位）与 `LocalVar`（算子内部的临时变量），并说清楚它们的序号空间是否独立。
2. 掌握 `PlaceHolder` 与 `PlaceHolderTmpLike` 两个工厂函数的用法，知道后者如何「照着某个已有 `Param` 的类型」派生出一个同类型的临时变量。
3. 理解 `ParamUsage`（`IN`/`OUT`/`IN_OUT`）如何驱动框架在编译期自动收集「输入列表」与「输出列表」，以及 `IN_OUT`（原地参数）为何会同时出现在这两个列表里。

## 2. 前置知识

本讲默认你已经读过 u2-l1，知道以下事实：

- ATVOSS 表达式是「类型即结构、对象空壳」的编译期 AST；信息编码在类型里，对象因空基类优化（EBO）几乎不占空间。
- `Expression<T>` 是统一外壳，`T` 是叶子节点（`Param`/`LocalVar`）或运算节点；每一层都剥掉外壳、只留内核再套入新 `Op`，所以 `Expression` 只在最外层出现一次。

此外，你需要一点 C++ 模板元编程的背景知识：

- **类型萃取（type trait）**：用 `template <typename T> struct IsXxx : std::false_type {};` 加一个「匹配特化」 `template <...> struct IsXxx<某种具体类型> : std::true_type {};` 的写法，在编译期回答「某个类型是不是 X」。本讲会大量见到 `IsParam`、`IsLocalVar`、`IsInVar` 这种 trait。
- **`TypeList` 与列表操作**：`Atvoss::Util::TypeList<Ts...>` 是一个只用来在编译期装「一串类型」的空结构体；框架在它上面定义了 `Filter`（按谓词筛选）、`Unique`（去重）、`Concatenate`（拼接）、`Find`（查找）等操作。本讲把这些当「黑盒工具」用，细节在 `include/utils/utility.h` 里。
- **谓词（predicate）**：一个接受一个类型、返回 `true`/`false` 的模板，例如「这个 `Param` 的 `usage` 是不是 `IN`」。`Filter` 就是靠它决定哪些类型留在列表里。

> 关键直觉：用户写下的 `PlaceHolder<...>` 不是在「创建变量」，而是在「给编译器登记一张参数卡片」——卡片上写着序号、张量类型、用途。框架后续靠遍历表达式树、把这些卡片分门别类地装进「输入列表」和「输出列表」，从而知道运行时要给算子喂几个 `Tensor`、回收几个 `Tensor`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/expression/expr_template.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h) | 本讲的主战场。定义 `ParamUsage` 枚举、`Param`/`LocalVar` 叶子节点、`PlaceHolder`/`PlaceHolderTmpLike` 工厂函数，以及 `IsParam`/`IsInVar`/`InParams`/`OutParams` 等编译期收集机制。 |
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | 枚举集合，定义 `Pattern`（`AR`/`RA`/`AB`/`BA`，归约/广播方向）、`CastMode`、`MemMngPolicy`、`MemLevel`。本讲引用它是为了说明 `ParamUsage` 与这些「策略枚举」同属一类编译期标签，以及 `CastMode` 如何与 `PlaceHolderTmpLike` 配合做类型转换。 |
| [include/utils/utility.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h) | `TypeList` 工具箱，提供 `Filter`/`Unique`/`Concatenate` 等本讲收集机制所依赖的操作，当作黑盒引用。 |
| examples/abs/abs.cpp | 最简单的两参数样例（一进一出），用来观察 `PlaceHolder` 的最小写法。 |
| examples/muls/muls.cpp | 含**标量参数**与 `PlaceHolderTmpLike` 的样例，用来观察临时变量与类型转换的写法。 |
| tests/ut/host/test_arguments.cpp | 主机侧单测，里面同时出现了三个 `PlaceHolder` 加一个 `PlaceHolderTmpLike<1>(in1)`，是本讲「序号空间」问题的最佳佐证。 |

---

## 4. 核心概念与源码讲解

### 4.1 `PlaceHolder` 与 `PlaceHolderTmpLike`：登记参数卡片的两个工厂

#### 4.1.1 概念说明

用户在 `Compute()` 里并不能直接写 `Param<1, ...>` 这种类型去构造对象——因为 `Param` 是一个「空结构体」，你需要的是一个被 `Expression` 包好的、能参与 `=` 与运算符重载的表达式对象。`PlaceHolder` 就是这个「把 `Param` 包成 `Expression` 再返回一个空壳对象」的工厂函数：

```cpp
auto in = Atvoss::PlaceHolder<1, Tensor<Dtype>, Atvoss::ParamUsage::IN>();
```

这一行的等价意义是「登记一张参数卡片：序号 1、张量类型 `Tensor<Dtype>`、用途是输入」，并返回一个类型为 `Expression<Param<1, Tensor<Dtype>, IN>>` 的空壳对象，供后续公式使用。

`PlaceHolderTmpLike` 则是另一种登记方式：当你需要一个**临时变量**，且希望它的张量类型「和某个已有 `Param` 一样」时，不必手写一遍类型，直接「照着」那个 `Param` 派生即可。它返回的不是 `Expression<Param<...>>`，而是 `Expression<LocalVar<...>>`——也就是说，它登记的是一张「临时变量卡片」。

#### 4.1.2 核心流程

两个工厂的类型推导流程如下：

```
PlaceHolder<1, Tensor<float>, IN>()
  → Expression< Param<1, Tensor<float>, ParamUsage::IN> >{}        // 返回 Param 卡片

PlaceHolderTmpLike<1, Tensor<half>>(in)        // in 是 Expression<Param<1, Tensor<float>, IN>>
  → Expression< LocalVar<1, Tensor<half>, Param<1,Tensor<float>,IN>> >{}   // 返回 LocalVar 卡片
```

注意两点：

1. `PlaceHolder` 的第三个模板参数 `ParamUsage` 有默认值 `IN`，所以纯输入参数可以省略它（见 4.3 节）。
2. `PlaceHolderTmpLike` 的第二个模板参数 `T` 可以省略——省略时它会**直接复用被「照着」的那个 `Param` 的张量类型**，下面源码会看到这个分支。

#### 4.1.3 源码精读

先看 `PlaceHolder`，它短到只有一行真正逻辑：[include/expression/expr_template.h:604-608](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L604-L608) 把传入的 `N`、`T`、`U`（默认 `IN`）组装成一个 `Param`，包进 `Expression` 并返回一个空壳。

再看 `PlaceHolderTmpLike`：[include/expression/expr_template.h:593-602](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L593-L602)。两个关键点：

- 第一行 `static_assert(IsParam_v<L>, ...)` 用一个 `static_assert` 强制「`LocalVar` 只能照着 `Param` 派生」——你不能拿一个临时变量再去派生临时变量。这也是为什么入参 `Expression<L>` 里那个 `L` 必须是 `Param`。
- `if constexpr (std::is_void_v<T>)` 这条分支：当用户省略第二个模板参数（即 `T = void`，默认值）时，走 `Expression<LocalVar<N, typename L::Type, L>>{}`，也就是**直接取被照着的 `Param`（`L`）的张量类型 `L::Type`**；否则用用户显式给定的 `T`。第三个模板实参 `L` 被原样记录进 `LocalVar` 的 `Like` 字段，留下「我是照着谁派生的」这条血缘信息。

真实使用见 muls 样例：[examples/muls/muls.cpp:44-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L44-L49)。其中第 47 行 `auto inTmp = Atvoss::PlaceHolderTmpLike<1, Tensor<ScalarDtype>>(in);` 显式指定了目标类型 `Tensor<ScalarDtype>`，配合下一行的 `Cast`，把 `int32` 输入搬到一个 `float` 类型的临时变量里——这是 ATVOSS 处理「输入类型与计算类型不一致」的标准写法。

> 标量也是 `PlaceHolder`：注意 muls 里 `auto scalar = Atvoss::PlaceHolder<2, ScalarDtype, Atvoss::ParamUsage::IN>();`（[examples/muls/muls.cpp:33](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L33)）。标量参数同样用 `PlaceHolder` 登记，只是第二个模板参数是裸标量类型 `ScalarDtype`（如 `float`）而不是 `Tensor<...>`。框架靠「这个类型是不是 `Tensor`」来区分标量入参与张量入参。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认 `PlaceHolder` 省略 `ParamUsage` 时的默认取值，以及 `PlaceHolderTmpLike` 省略类型时的派生行为。

**操作步骤**：

1. 打开 [include/expression/expr_template.h:604-608](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L604-L608)，确认 `PlaceHolder` 的第三个模板形参写法是 `ParamUsage U = ParamUsage::IN`。
2. 打开 [tests/ut/host/test_arguments.cpp:40-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_arguments.cpp#L40-L44)，观察第 44 行 `auto temp = Atvoss::PlaceHolderTmpLike<1>(in1);`——这里**省略了第二个模板参数**，因此 `temp` 的类型应当完全照搬 `in1` 的张量类型。

**需要观察的现象**：`temp` 与 `in1` 同为 `Tensor<float>` 派生，但 `temp` 是 `LocalVar<1, ...>` 而 `in1` 是 `Param<1, ...>`——序号都是 1，却分属两种节点。

**预期结果**：在脑中（或用 `static_assert(std::is_same_v<...>)`）验证 `temp` 的内核类型是 `LocalVar<1, Tensor<float>, Param<1, Tensor<float>, IN>>`。注意它**没有** `usage` 字段——`LocalVar` 既不是输入也不是输出，它是纯内部临时量。待本地用 host 单测验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `PlaceHolderTmpLike<1>(in1)` 的序号 `1` 改成 `2`，而代码里只声明了这一个临时变量，会怎样？

**参考答案**：这会登记一张 `LocalVar<2, ...>` 卡片。由于此时临时变量集合里只有 `LocalVar<2>` 一个成员，但序号不是从 1 开始，会在 4.4 节讲到的 `LocalVars<T>` 里触发 `static_assert`：「LocalVars must be numbered sequentially from 1」。也就是说临时变量的序号必须从 1 起连续编号。

**练习 2**：`PlaceHolderTmpLike` 的第一个模板参数 `N` 与被照着的 `Param` 的序号之间有强制相等关系吗？

**参考答案**：没有。`N` 是新 `LocalVar` 自己的序号，与 `Param` 的序号互不相干。muls 样例里写成 `PlaceHolderTmpLike<1>` 只是巧合（照着的也是 `Param<1>`），完全可以写成 `PlaceHolderTmpLike<1>(in2)`——「照着 2 号参数派生出 1 号临时变量」是合法的。

---

### 4.2 `Param` 与 `LocalVar`：两种叶子节点的结构

#### 4.2.1 概念说明

表达式树的叶子节点只有两种：

- **`Param`**：算子的**函数参数占位**。它对应运行时由 `ArgumentsBuilder` 喂进来的「外部数据」——要么是设备上的 `Tensor`，要么是一个标量。每个 `Param` 都有一张明确的「用途」标签（输入/输出/原地），框架据此决定要不要给它分配 GM 显存、要不要把结果拷回 Host。
- **`LocalVar`**：算子**内部的临时变量**。它不对应任何外部入参，只活在表达式内部，用来暂存中间结果（例如 muls 里先把 `int32` 转成 `float` 的 `inTmp`）。它没有「输入/输出」概念，因为它既不会从外部读、也不会向外部写。

两者都是「空结构体」——所有信息都在类型形参里，没有运行时数据成员，这正是 EBO 能把它们压成 0 字节的前提（见 u2-l1）。

#### 4.2.2 核心流程

两种节点的「信息编码」对照如下：

```
Param<N, T, U, RN>
  ├─ number       = N          // 序号：对应 ArgumentsBuilder 里第 N 个参数
  ├─ Type         = T          // 张量类型或标量类型
  ├─ usage        = U          // 用途：IN / OUT / IN_OUT（默认 IN）
  └─ inplaceNumber = RN        // 原地参数的「实参槽位」，默认等于 N

LocalVar<N, T, L>
  ├─ number = N                // 序号：临时变量自己的编号，从 1 起连续
  ├─ Type   = T                // 张量类型
  └─ Like   = L                // 血缘：记录它是照着哪个 Param 派生的
```

它们的对象都禁止直接 `=` 赋值：`Param`/`LocalVar` 内部都把 `operator=` 写成「永远报错的 `static_assert`」（提示你必须用 `Expression<Param>` / `Expression<LocalVar>` 来赋值）。这是为了防止用户写出 `in = ...` 这种「看起来在改入参」的误导代码——真正的赋值必须发生在 `Expression` 层（见 u2-l1 的 `OpAssign`）。

#### 4.2.3 源码精读

`Param` 的定义：[include/expression/expr_template.h:98-114](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L98-L114)。逐项说明：

- `number`（L103）即序号 `N`，是运行时与 `ArgumentsBuilder` 入参顺序对齐的唯一凭据。
- `inplaceNumber`（L105）注释写得很直白：「for IN_OUT scenario. Two `Param`s point to the same GM」——它默认等于 `N`，仅在原地参数场景下被框架用来定位「这个输入/输出共用哪一块 GM 显存」（详见 4.3 节与 [include/elewise/block/schedule.h:278-279](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L278-L279)）。
- `usage`（L106）即用途枚举，是 4.3 节的主角。
- L108-L113 的 `operator=` 是一个「永远 `static_assert` 失败」的占位，强制你走 `Expression::operator=`。

`LocalVar` 的定义：[include/expression/expr_template.h:72-87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L72-L87)。注意三处与 `Param` 的差别：

- 它有 `Like`（L78）字段，记录「血缘」；`Param` 没有这个字段。
- 它**没有** `usage` 字段——临时变量不分输入输出。
- 它**没有** `inplaceNumber`——临时变量不与外部 GM 显存挂钩。
- L81-L86 同样是一个「永远失败」的 `operator=` 占位。

两者各有对应的类型萃取 trait：[include/expression/expr_template.h:116-123](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L116-L123) 是 `IsParam`/`IsParam_v`，[include/expression/expr_template.h:89-96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L89-L96) 是 `IsLocalVar`/`IsLocalVar_v`。它们都采用经典的「主模板 `false` + 特化 `true`」写法，是后续所有收集机制的判定基础。

> **序号空间独立**：这是本讲最容易踩坑的点。`Param` 的 `number` 和 `LocalVar` 的 `number` 是**两套互不相干的编号空间**，各自从 1 起连续编号。所以 `Param<1, ...>` 和 `LocalVar<1, ...>` 可以同时存在、互不冲突——`test_arguments.cpp` 第 40、44 行正是这种共存（`in1` 是 `Param<1>`，`temp` 是 `LocalVar<1>`）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用真实样例验证「标量与张量入参都是 `Param`，临时变量是 `LocalVar`」。

**操作步骤**：

1. 打开 [examples/muls/muls.cpp:32-34](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L32-L34)，看到 `in`、`scalar`、`out` 三个 `PlaceHolder`——前两个是 `IN`、最后一个是 `OUT`，但它们的「内核」都是 `Param<...>`，只是第二个的张量类型是裸标量 `ScalarDtype`。
2. 打开 [examples/muls/muls.cpp:47](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L47)，看到 `inTmp` 用 `PlaceHolderTmpLike` 创建，其内核是 `LocalVar<1, ...>`。

**需要观察的现象**：同一个 `MulsComputePromtIn` 里同时存在 `Param<1>`（in）、`Param<2>`（scalar）、`Param<3>`（out）与 `LocalVar<1>`（inTmp）。`Param` 序号到 3，`LocalVar` 序号从 1 起，两套编号互不打架。

**预期结果**：在纸上列出这四个变量各自的「节点种类 / 序号 / 是否有 `usage`」三栏，确认 `LocalVar<1>` 与 `Param<1>` 同号但种类不同、用途字段不同。待本地用 host 单测验证。

#### 4.2.5 小练习与答案

**练习 1**：`LocalVar` 为什么没有 `usage` 字段？如果硬给它加上 `IN`，语义上会出什么问题？

**参考答案**：`usage` 表达的是「这个参数相对算子外部是输入还是输出」，它驱动框架去 GM 显存搬运数据、把结果还给 Host。`LocalVar` 是纯内部临时量，既不从外部读也不向外部写，根本没有「输入/输出」可言；给它加 `usage` 会误导框架去为它分配 GM、做多余的拷贝。

**练习 2**：`Param` 的 `operator=`（[L108-L113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L108-L113)）和 `Expression::operator=`（u2-l1 讲过的那个）有什么区别？为什么需要两个？

**参考答案**：`Param::operator=` 是「占位陷阱」，写它纯粹是为了在用户写出 `in = ...`（直接对占位符赋值）时给出清晰的编译期报错，提示必须改用 `Expression<Param>`。`Expression::operator=` 才是真正干活的那个，它把左右两边的表达式内核取出来、组装成 `OpAssign` 节点。两者一个「挡错」、一个「建树」，缺了前者，错误用法会落到模板深处变成难懂的报错。

---

### 4.3 `ParamUsage`：`IN` / `OUT` / `IN_OUT` 与原地参数

#### 4.3.1 概念说明

`ParamUsage` 是一个三值枚举，给每个 `Param` 贴上「相对外部数据流的方向」标签：

- `IN`：纯输入。框架会为它准备 GM，把 Host 数据搬进来（CopyIn），计算时只读。
- `OUT`：纯输出。框架会为它准备 GM，但不搬入数据；计算结束后把结果拷回 Host（CopyOut）。
- `IN_OUT`：原地（in-place）参数。**同一个 GM 既被读又被写**——典型场景是 `y = relu(y)` 这种「输出直接覆盖输入」的算子。它同时具备输入和输出两种身份。

这个标签看似只是给 `Param` 的一个修饰，实则是整个运行时数据搬运的「开关」：Device 层据此决定哪些参数要 `CopyIn`、哪些要 `CopyOut`，Tile 层的张量求值器也用它判断当前节点是不是需要从 GM 搬数据（见 [include/elewise/tile/tensor_evaluator.h:159](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L159) 处对 `IN`/`IN_OUT` 的判定）。

#### 4.3.2 核心流程

三种 `usage` 在「输入列表 / 输出列表」里的归属可以这样记：

| `ParamUsage` | 属于输入列表？ | 属于输出列表？ | 语义 |
| --- | --- | --- | --- |
| `IN` | ✅ | ❌ | 只读入参 |
| `OUT` | ❌ | ✅ | 只写出参 |
| `IN_OUT` | ✅ | ✅ | 原地：读写同一块 GM |

关键直觉是：`IN_OUT` 会**同时**出现在输入列表与输出列表里。这意味着框架对它既要做 CopyIn、又要做 CopyOut；但因为它指向同一块 GM，`inplaceNumber` 字段就用来告诉框架「这个原地参数实际复用的是第几个实参槽位」，避免重复分配显存。下游的 DAG 构建（u3-l3）会进一步把一个 `IN_OUT` 参数「拆」成一个 `IN` 视图加一个 `OUT` 视图，让图优化阶段仍然可以按「先读后写」的依赖去处理。

#### 4.3.3 源码精读

枚举定义本身极简：[include/expression/expr_template.h:34-39](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L34-L39)，三个值 `IN`/`OUT`/`IN_OUT`。注意它**定义在 `expr_template.h` 里**，与 `Param` 紧挨着；`include/utils/patterns.h` 里的 `Pattern`/`CastMode`/`MemMngPolicy`/`MemLevel` 是另一组「策略类枚举」（[include/utils/patterns.h:14-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L14-L44)），二者是平行的编译期标签，不要混淆——`ParamUsage` 管「数据流方向」，`Pattern` 管「归约/广播方向」。

把「方向」落实成「判定」的是三个 trait：[include/expression/expr_template.h:339-346](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L339-L346)。

- `IsInVar<U>`（L340）：`usage` 是 `IN` **或** `IN_OUT` 时为 `true`——印证了上表「`IN_OUT` 也算输入」。
- `IsOutVar<U>`（L343）：`usage` 是 `OUT` **或** `IN_OUT` 时为 `true`。
- `IsInplaceVar<U>`（L346）：只有 `IN_OUT` 为 `true`，专用于识别原地参数。

`inplaceNumber` 的实际消费点在 Block 层：[include/elewise/block/schedule.h:278-279](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L278-L279)，注释写明「We use `inplaceNumber` to adapter IN_OUT params optimization in `AUTO` Dag」，并用 `ParamType::inplaceNumber - 1` 算出它在实参元组里的下标——也就是说，原地参数在运行时复用的是它「作为输入」那一侧的 GM 指针，不会另开一块显存。Device 层的入参分发也遵循同样的「`IN`/`IN_OUT` 进输入、`OUT`/`IN_OUT` 进输出」规则：[include/elewise/device/device_adapter.h:243](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L243) 与 [include/elewise/device/device_adapter.h:250](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L250)。

> 真实样例里目前只见 `IN`/`OUT`：abs（[examples/abs/abs.cpp:28-29](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L28-L29)）、muls、rms_norm 都是用「一进一出」或「多进一出」的纯 `IN`/`OUT` 写法。`IN_OUT` 主要出现在需要原地优化的算子里，框架侧（DAG、Block 调度）已为它做好支撑，后续在 u3-l3 会看到 DAG 如何把 `IN_OUT` 拆成 `IN`+`OUT`。

#### 4.3.4 代码实践（阅读 + 推理型）

**实践目标**：验证「`ParamUsage` 默认是 `IN`」，并推断 `IN_OUT` 在输入/输出列表中的双重归属。

**操作步骤**：

1. 打开 [include/expression/expr_template.h:98](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L98)，确认 `Param` 的第三个模板形参写法是 `ParamUsage U = ParamUsage::IN`。这解释了为什么 abs 样例里 `PlaceHolder<1, Tensor<Dtype>, Atvoss::ParamUsage::IN>()` 的第三个参数其实可以省略（只是样例为了教学写全了）。
2. 对照 [include/expression/expr_template.h:340](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L340) 与 [include/expression/expr_template.h:343](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L343) 两条判定，手动代入 `usage = IN_OUT`：`IsInVar` 命中 `IN_OUT` 分支为 `true`，`IsOutVar` 命中 `IN_OUT` 分支也为 `true`。

**需要观察的现象**：一个 `IN_OUT` 参数会被 `IsInVar` 与 `IsOutVar` 同时判为 `true`。

**预期结果**：根据下一节 4.4 的 `Filter` 机制，这个参数会**同时**留在 `InParams` 与 `OutParams` 两个列表里——这就是「原地参数双重身份」在类型层面的根源。待本地用 host 单测验证。

#### 4.3.5 小练习与答案

**练习 1**：如果一个算子写成 `auto x = PlaceHolder<1, Tensor<float>, ParamUsage::IN_OUT>(); return (x = Abs(x));`，框架会为 `x` 分配几块 GM？

**参考答案**：逻辑上「只分配一块」。因为 `x` 是 `IN_OUT`，`inplaceNumber` 默认等于它的 `number`（1），Block 层用 `inplaceNumber - 1` 定位到实参元组的第 0 个槽位，复用那块 GM 既读又写。如果不走原地优化（例如被 DAG 拆成独立的 `IN` 与 `OUT` 视图），则可能表现为「读一块、写另一块」，由 `MemMngPolicy`（`AUTO`/`MANUAL`，见 patterns.h）决定是否合并。

**练习 2**：为什么 `PlaceHolder` 把 `ParamUsage` 的默认值设成 `IN` 而不是 `OUT`？

**参考答案**：因为绝大多数参数是输入。算子通常「多进一出」，输入参数远多于输出；把默认设成 `IN` 能让最常见的纯输入参数省略第三个模板实参，降低书写成本。输出参数必须显式写 `OUT`，相当于一道「这里要往外部写」的提醒。

---

### 4.4 编译期类型收集：从 AST 萃取 `InParams` / `OutParams` / `LocalVars`

#### 4.4.1 概念说明

前面三节讲的都是「如何登记一张卡片」。本节回答：**框架怎么把这些卡片从一整棵表达式树里自动收集出来？**

答案是**编译期遍历**。框架拿到用户 `Compute()` 返回的那棵 `Expression<Op...>` 类型后，并不在运行时去解析它，而是用一连串递归的模板特化，沿着 `Op` 节点的 `DataType`/`LhsType`/`RhsType` 一路往下钻，把所有遇到的 `Param` 叶子收进一个 `TypeList`，再用 `Filter` 按 `usage` 分成「输入列表」与「输出列表」。整个过程发生在编译期，零运行时开销。

这一节涉及较多模板元编程，读起来稍吃力，但结论很朴素：**你只要按规则写 `PlaceHolder`，框架就自动知道这个算子有几个输入、几个输出、几个临时变量。**

#### 4.4.2 核心流程

收集分两步走：

```
第 1 步：沿表达式树收集「全部 Param」「全部 LocalVar」
   ParamCollector<T>      递归钻 T::DataType / T::LhsType / T::RhsType
                          遇到 Param 叶子就收进 TypeList
   LocalVarCollector<T>   同理，专门收 LocalVar 叶子

第 2 步：去重 → 排序 → 按 usage 过滤
   Unique_t<...>          去掉重复登记的同一个 Param（如 in1*in1 里的 in1）
   SortedParams<...>      按 number 重新排成 1,2,3,... 顺序
   Filter_t<IsInVar, ...> 筛出输入列表  → InParams
   Filter_t<IsOutVar, ...>筛出输出列表  → OutParams
```

其中「去重」非常关键：rms_norm 里 `in1` 在表达式里被引用了多次（`in1 * in1`、`in1 / ...`），但 `in1` 只是一个输入参数，绝不能在输入列表里出现两次。`Unique_t` 负责把它折叠成一张卡片。

#### 4.4.3 源码精读

收集器位于 `Detail` 命名空间内。[include/expression/expr_template.h:185-210](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L185-L210) 是 `ParamCollector`，它用 SFINAE 分派：

- 命中 `IsParam_v<T>`（L191）：这是叶子，收进 `TypeList<T>`。
- 命中 `IsUnaryOp_v<T>`（L196）：这是一元运算节点，递归它的 `T::DataType`。
- 命中 `IsBinaryOp_v<T>`（L201）：这是二元运算节点，分别递归 `T::LhsType` 与 `T::RhsType`，再用 `Concatenate_t` 拼起来。

[include/expression/expr_template.h:153-178](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L153-L178) 的 `LocalVarCollector` 结构完全对称，只是判定换成 `IsLocalVar_v`。这套「按节点形状分派、递归子树、拼接结果」的写法，就是经典的编译期树遍历。

收集完做去重与排序（[include/expression/expr_template.h:212-237](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L212-L237) 的 `UniqueParams`/`SortedParams`），然后对外暴露两个带「连续编号校验」的入口：[include/expression/expr_template.h:320-337](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L320-L337) 的 `Params<T>` 与 [include/expression/expr_template.h:301-318](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L301-L318) 的 `LocalVars<T>`。这两个入口里各有一条 `static_assert`（L309-L311、L329-L330），强制「`Param`/`LocalVar` 必须从 1 起连续编号」——这正是 4.1.5 练习里「序号不能跳号」的报错来源。校验通过后，再交给 `SortedParams` 按 `number` 排成有序列表。

最后，按 `usage` 过滤得到输入/输出列表：[include/expression/expr_template.h:348-362](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L348-L362)。

- `InParams<T>`（L349-L351）：`Filter_t<IsInVar, Params_t<T>>`——在全部有序 `Param` 上，用 4.3 节的 `IsInVar` 留下 `IN` 与 `IN_OUT`。
- `OutParams<T>`（L357-L359）：`Filter_t<IsOutVar, Params_t<T>>`——留下 `OUT` 与 `IN_OUT`。

这里的 `Filter_t`、`Unique_t`、`Concatenate_t` 都来自 [include/utils/utility.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h)（`Filter` 见 [L532-L548](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L532-L548)、`Unique` 见 [L784-L825](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L784-L825)），它们是纯编译期的「类型列表」运算，不产生任何运行时代码。

> 还有一组「按单个 `Param`/`LocalVar` 查询」的细粒度 trait：[include/expression/expr_template.h:364-380](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L364-L380) 的 `IsInParam`/`IsOutParam`/`IsInplaceParam`，以及 [L240-L293](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L240-L293) 的 `IsParamN`/`IsLocalVarN`/`HasParamN`（回答「表达式里是否存在第 N 号 Param/LocalVar」）。它们供 DAG、Buffer 管理等更靠后的模块做精细判定，本讲知道「有这组工具」即可，细节留到 u3-l3/u3-l5。

#### 4.4.4 代码实践（推理型）

**实践目标**：手动模拟一次 `InParams`/`OutParams` 的收集过程，把抽象的元编程落实成一次具体推导。

**操作步骤**：以 abs 样例的 `Compute()`（[examples/abs/abs.cpp:28-30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L28-L30)）为对象，表达式是 `out = Abs(in)`，其顶层类型为 `Expression<OpAssign<Param<2,...,OUT>, OpAbs<Param<1,...,IN>>>>`。

1. 从顶层 `OpAssign`（它是 `BinaryOp`）出发，`ParamCollector` 递归 `LhsType`（`Param<2,...,OUT>`，叶子，收）与 `RhsType`（`OpAbs<...>`，一元节点，继续钻 `DataType` → `Param<1,...,IN>`，叶子，收）。
2. 收集结果是 `TypeList<Param<2,...>, Param<1,...>>`。
3. `Unique_t` 去重（无重复，不变）；`SortedParams` 按 `number` 排序得到 `TypeList<Param<1,...,IN>, Param<2,...,OUT>>`。
4. `Filter_t<IsInVar, ...>` 只留 `Param<1,...,IN>` → `InParams = TypeList<Param<1>>`。
5. `Filter_t<IsOutVar, ...>` 只留 `Param<2,...,OUT>` → `OutParams = TypeList<Param<2>>`。

**需要观察的现象**：尽管 `in` 在公式里被 `Abs` 包裹、处于右子树深处，`ParamCollector` 仍能沿 `DataType` 把它挖出来；最终输入列表、输出列表各一个，且顺序由 `number` 决定（而非书写顺序）。

**预期结果**：`InParams` 长度 1、`OutParams` 长度 1，与运行时 `ArgumentsBuilder{}.inputOutput(t1, t2)`（[examples/abs/abs.cpp:175](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L175)）传入的「1 个输入 + 1 个输出」严格对应。这正是「`PlaceHolder` 的序号 N」与「`ArgumentsBuilder` 的入参顺序」能对齐的根本原因。待本地用 host 单测验证。

#### 4.4.5 小练习与答案

**练习 1**：rms_norm 里 `in1` 在表达式中出现多次（如 `in1 * in1`、`in1 / Sqrt(...)`）。如果不做 `Unique_t` 去重，`InParams` 会变成什么样？会造成什么后果？

**参考答案**：`ParamCollector` 每遇到一次 `in1` 就收一次，`InParams` 里会出现多个 `Param<1, ...>` 重复项。后果是框架误以为有多个输入，运行时会试图从 `ArgumentsBuilder` 里取出多余的 `Tensor`，导致序号错位、类型不匹配甚至编译失败。去重保证了「同一个逻辑参数在列表里只占一席」。

**练习 2**：为什么 `Params<T>` 与 `LocalVars<T>` 里都要加一条「必须从 1 起连续编号」的 `static_assert`？

**参考答案**：因为后续要靠 `number` 作为「下标」去实参元组里取值（例如 Block 层 `inplaceNumber - 1`、Device 层按位置分发）。如果序号可以乱跳（比如只有 `Param<1>` 和 `Param<3>`），框架就无法用「序号 - 1」稳定地映射到元组下标。强制连续编号把「序号」变成了一个可靠的编译期下标空间，让整条「`PlaceHolder` 序号 → 实参元组位置」的链路成立。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个三输入算子的「参数登记」练习。假设要写一个算子 `out = (in1 + in2) * in3`，其中 `in1`/`in2`/`in3` 都是 `Tensor<float>` 输入，`out` 是 `Tensor<float>` 输出，并且你希望先把 `in1 + in2` 的结果存到一个与 `in1` 同类型的临时变量 `tmp` 里，再乘以 `in3`。

**任务**：

1. 写出全部 `PlaceHolder` 声明（含正确的序号 `N` 与 `ParamUsage`），以及用 `PlaceHolderTmpLike` 声明 `tmp` 的语句。
2. 用一句话说明 `LocalVar` 的序号空间与 `Param` 是否独立，并指出你的 `tmp` 序号为何可以取 1 而不与 `in1`（`Param<1>`）冲突。
3. 推断框架对这棵表达式收集出的 `InParams`、`OutParams`、`LocalVars` 各有几个元素、分别是哪些。

**参考写法**（示例代码，非项目原有代码）：

```cpp
// 示例代码：三输入算子的参数登记
auto in1 = Atvoss::PlaceHolder<1, Tensor<float>, Atvoss::ParamUsage::IN>();
auto in2 = Atvoss::PlaceHolder<2, Tensor<float>, Atvoss::ParamUsage::IN>();
auto in3 = Atvoss::PlaceHolder<3, Tensor<float>, Atvoss::ParamUsage::IN>();
auto out = Atvoss::PlaceHolder<4, Tensor<float>, Atvoss::ParamUsage::OUT>();
auto tmp = Atvoss::PlaceHolderTmpLike<1>(in1);   // 照着 in1 派生，省略类型 → 同为 Tensor<float>

return (tmp = in1 + in2, out = tmp * in3);        // 逗号串联两步顺序执行（OpAndThen，见 u2-l1）
```

**要点对照**：

- **序号空间独立**：`Param` 与 `LocalVar` 是两套互不相干的编号空间，各自从 1 起连续。因此 `tmp` 作为 `LocalVar<1, ...>` 与 `in1` 作为 `Param<1, ...>` 同号但分属两种节点，互不冲突——这正是 `test_arguments.cpp` 第 40、44 行的写法。
- **`PlaceHolderTmpLike` 省略类型**：`PlaceHolderTmpLike<1>(in1)` 没写第二个模板参数，于是走 `is_void_v<T>` 分支，复用 `in1` 的张量类型 `Tensor<float>`。
- **收集结果**：`InParams = {Param<1>, Param<2>, Param<3>}`（3 个，全 `IN`）、`OutParams = {Param<4>}`（1 个，`OUT`）、`LocalVars = {LocalVar<1>}`（1 个）。这与运行时 `ArgumentsBuilder{}.inputOutput(in1Tensor, in2Tensor, in3Tensor, outTensor)` 的「3 进 1 出」严格对应。
- **顺序执行**：`tmp = in1 + in2` 与 `out = tmp * in3` 用逗号串联成 `OpAndThen`（u2-l1），保证先算加法、再算乘法。

> 进阶：若你把 `tmp` 的序号误写成 `2`（`PlaceHolderTmpLike<2>(in1)`），而代码里只有这一个临时变量，那么 `LocalVars<T>` 的 `static_assert`（[L309-L311](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L309-L311)）会报「LocalVars must be numbered sequentially from 1」——这是本讲最常见的踩坑点，建议本地构造一个最小 host 单测复现它。

---

## 6. 本讲小结

- `PlaceHolder<N, T, ParamUsage>` 是登记「函数参数卡片」的工厂，它把 `Param<N, T, U>` 包成 `Expression` 返回；`PlaceHolderTmpLike<N, T>(某个Param)` 则登记「临时变量卡片」，返回 `Expression<LocalVar<N, T, Like>>`，省略 `T` 时直接复用被照着 `Param` 的张量类型。
- 叶子节点只有两种：`Param`（外部入参/出参，带 `usage` 与 `inplaceNumber`）与 `LocalVar`（内部临时量，带 `Like` 血缘、无 `usage`）；两者都是空结构体，信息全在类型形参里，且都内置「永远失败」的 `operator=` 占位以防误用。
- `ParamUsage` 三值 `IN`/`OUT`/`IN_OUT` 表达数据流方向，是运行时 GM 搬运（CopyIn/CopyOut）的开关；其中 `IN_OUT`（原地参数）会**同时**算作输入与输出，靠 `inplaceNumber` 复用同一块 GM。
- 框架在编译期沿表达式树递归收集所有 `Param`/`LocalVar`，经 `Unique` 去重、`SortedParams` 按序号排序、再用 `Filter` 按 `usage` 过滤，自动得到 `InParams`/`OutParams`/`LocalVars` 三个列表——这就是「写好 `PlaceHolder`，框架就自动知道算子几进几出」的底层机制。
- **序号空间独立且各自连续**：`Param` 与 `LocalVar` 是两套从 1 起的编号空间，互不冲突；任一空间内跳号都会触发 `static_assert`。序号同时是运行时与 `ArgumentsBuilder` 入参顺序对齐的唯一凭据。

## 7. 下一步学习建议

本讲把表达式树的「叶子」彻底讲透了。接下来可以按两条线推进：

- **横向扩展（算子库）**：去读 u2-l3「运算符库：数学与张量算子」与 u2-l4「归约与广播算子」，看看 `math_expression.h`/`tensor_expression.h`/`transcendental_expression.h` 里的 `OpAdd`/`OpCast`/`ReduceSum`/`Broadcast` 等内部节点是如何「夹」在这些叶子节点之间、组成完整公式的，并体会 `CastMode`/`Pattern` 这两个 `patterns.h` 枚举在算子里的真实用法。
- **纵向深入（求值）**：如果你更关心「这棵树最终怎么变成 Ascend C 指令」，可以先跳到 u3-l1「求值器系统」，看 `Evaluator<T>` 如何沿本讲描述的叶子与内部节点递归求值，以及 `Param`/`LocalVar` 求值时如何从运行时上下文里取出对应的 `Tensor`。

建议同时用本讲的「序号对齐」结论回看一遍 u1-l5 的 `ArgumentsBuilder` 链式构造与 u2-l6 的入参构造，你会发现自己已经能从「类型」层面解释清楚「为什么 `inputOutput(...)` 的参数顺序必须和 `PlaceHolder` 序号一一对应」。
