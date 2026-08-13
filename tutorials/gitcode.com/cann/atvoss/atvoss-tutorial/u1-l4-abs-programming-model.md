# 从 abs 样例看用户编程模型

## 1. 本讲目标

在前三讲里，我们已经建立了 ATVOSS 的整体地图：它是一个声明式的 Vector 算子模板库，底层用五层架构（Device>Kernel>Block>Tile>Basic）把硬件调度细节封装起来。但「声明式」到底长什么样？用户究竟要写多少代码才能得到一个能跑的算子？

本讲以仓库里最简单的样例 `abs`（取绝对值）为入口，逐行拆解 ATVOSS 的**用户编程模型**。学完本讲你应当能够：

1. 看懂一个 ATVOSS 算子的标准骨架——`Config` 结构体由哪些部分组成。
2. 理解 `Compute()` 函数里 `PlaceHolder`、`ParamUsage` 与 `return` 计算表达式的写法。
3. 学会用 `BlockBuilder → KernelBuilder → DeviceAdapter` 三级 Builder 把一份计算描述「组装」成一个可执行的 `DeviceOp`。
4. 仿照 `abs`，自己改写出一个新算子（本讲综合实践：`Neg` 取负）。

本讲**只关心用户视角的编程模型**，不深入编译期求值、DAG 构建、Tiling 切分等内部机制——那些是进阶篇和专家篇的内容。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（U1-L1 ~ U1-L3 已建立）：

- **算子（Operator）**：对张量执行的一类计算，例如对每个元素取绝对值。
- **Ascend C**：昇腾 AI Core 的算子开发语言/C++ 库，ATVOSS 最终调用的就是它的底层 API。
- **Tiling（分块）**：把一大块数据切成小块，以便装进片上高速缓存（UB，Unified Buffer）逐块计算。
- **五层架构**：Device（Host 入口）> Kernel（多核切分）> Block（单核 Tile 切分）> Tile（API 封装）> Basic（Ascend C）。
- **header-only**：整个框架是一组头文件，用户只需 `#include "atvoss.h"`。

此外，需要一点点 C++ 模板基础：结构体（struct）、模板类型形参（`typename T`）、模板的模板形参（`template <typename> class Tensor`）、`constexpr`。不熟悉也没关系，本讲会在遇到时用一句话解释。

一个关键直觉：ATVOSS 的写法很像写数学公式。`out = Abs(in)` 在代码里几乎就是字面意思——「把 out 设为 in 的绝对值」。框架在编译期会把这个公式翻译成多核并行 + Tiling 分块 + 内存搬运的真实 Kernel。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [examples/abs/abs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp) | **本讲主角**。包含 `AbsConfig`（算子描述）、`Run`（ACL 运行流程）、`main`（命令行入口）。 |
| [examples/abs/README.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/README.md) | abs 样例说明：数学公式、参数表、编译运行命令。 |
| [include/elewise/block/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h) | `BlockBuilder` 与 `DefaultBlockPolicy`/`DefaultBlockConfig`：单核 Tile 切分层。 |
| [include/elewise/kernel/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h) | `KernelBuilder` 与 `DefaultKernelPolicy`/`DefaultKernelConfig`：多核切分层。 |
| [include/elewise/device/device_adapter.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h) | `DeviceAdapter`：Host 侧入口，串起 tiling 计算与 kernel 启动。 |

辅助理解（会引用但不在本讲深入）：

| 文件 | 作用 |
|------|------|
| [include/expression/expr_template.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h) | `PlaceHolder`、`Param`、`ParamUsage`、`Expression` 的定义。 |
| [include/operators/math_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h) | `Abs` 等数学算子的表达式声明与运算符重载。 |
| [include/utils/layout/shape.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/shape.h) | `Shape<int...>`：编译期形状。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- **4.1 TileShape 与 Config 结构体**——算子的「配置清单」。
- **4.2 Compute() 与 PlaceHolder/ParamUsage**——用公式声明计算逻辑。
- **4.3 三级 Builder 组装**——把清单变成可运行的 `DeviceOp`。

### 4.1 TileShape 与 Config 结构体

#### 4.1.1 概念说明

写一个 ATVOSS 算子，第一步不是写计算，而是填一张「配置清单」。这张清单在代码里就是一个 `struct`，习惯上叫 `XxxConfig`。它要回答几个问题：

- 这个算子的数据类型是什么？（`Dtype`）
- 单核内一次处理多大的数据块？（`TileShape`）
- 单核切分用什么策略？（`blockPolicy`）
- 多核切分用什么策略？（`kernelPolicy`）
- 目标硬件是哪个？（`ArchTag`）

`TileShape` 是其中最直观、也是用户最常调整的参数：它规定了 Block 层一次 Tile 处理的元素形状。可以把它想象成「锅的大小」——锅越大，一锅炒得越多，但需要的片上内存（UB）也越多。

#### 4.1.2 核心流程

一个 `Config` 结构体的组装流程是「自顶向下填字段」：

```
Config 结构体
  ├─ Dtype            ← 选定数据类型（如 float）
  ├─ TileShape        ← 选定 Tile 形状（编译期常量）
  ├─ XxxCompute       ← 写计算公式（4.2 节细讲）
  ├─ blockPolicy      ← 单核策略（默认 DefaultBlockPolicy<TileShape>）
  ├─ kernelPolicy     ← 多核策略（默认 UniformSegment 均匀切分）
  ├─ ArchTag          ← 目标架构（DAV_3510）
  └─ BlockOp/KernelOp/DeviceOp  ← 三级 Builder 组装（4.3 节细讲）
```

`TileShape` 的类型是 `Atvoss::Shape<int...>`——一个把形状编码在**类型**里的编译期整数序列：

```cpp
// include/utils/layout/shape.h
template <int... a>
class Shape {
public:
    using Types = std::tuple<std::integral_constant<size_t, a>...>;
    using size  = std::integral_constant<size_t, sizeof...(a)>;
};
```

`Shape<32>` 表示一维、长度 32；`Shape<1, 4096>` 表示二维。因为这些数字是类型的一部分，所以 Tiling 计算可以在编译期大量展开，几乎没有运行时开销。这也是为什么 `abs` 里 `TileShape` 用 `static constexpr` 定义。

#### 4.1.3 源码精读

先看 `abs` 的 `Config` 骨架（去掉了内部 `AbsCompute`，下一节再讲）：

[examples/abs/abs.cpp:19-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L19-L45) —— 整个 `AbsConfig` 的定义。它是一个模板结构体，模板参数 `T` 就是数据类型：

```cpp
template <typename T>
struct AbsConfig {
    using Dtype = T;

    using TileShape = Atvoss::Shape<TILE_SIZE>;   // TILE_SIZE = 32

    struct AbsCompute { /* 见 4.2 节 */ };

    static constexpr Atvoss::Ele::DefaultBlockPolicy<TileShape> blockPolicy{TileShape{}};

    static constexpr Atvoss::Ele::DefaultKernelPolicy kernelPolicy{
        Atvoss::Ele::DefaultSegmentPolicy::UniformSegment};

    using ArchTag = Atvoss::Arch::DAV_3510;

    using BlockOp   = Atvoss::Ele::BlockBuilder<AbsCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig>;
    using KernelOp = Atvoss::Ele::KernelBuilder<BlockOp, kernelPolicy>;
    using DeviceOp = Atvoss::DeviceAdapter<KernelOp>;
};
```

几个要点：

- [examples/abs/abs.cpp:16](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L16) —— `TILE_SIZE = 32`，所以 abs 一次 Tile 只处理 32 个元素。这是为了样例简单；真实算子常调到几千。
- [examples/abs/abs.cpp:23](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L23) —— `TileShape = Shape<32>`，一维形状。
- [examples/abs/abs.cpp:34](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L34) —— `blockPolicy` 把 `TileShape` 交给默认的 Block 策略。
- [examples/abs/abs.cpp:36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L36) —— `kernelPolicy` 选用 `UniformSegment`（多核均匀切分）。
- [examples/abs/abs.cpp:38](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L38) —— `ArchTag = DAV_3510`，对应 ascend950 的 Vector 核心（见 U1-L2 的 SOC 映射）。

`DefaultBlockPolicy` 和 `DefaultBlockConfig` 长什么样？看 Block Builder 头文件：

[include/elewise/block/builder.h:19-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L19-L32) —— 策略保存 TileShape 与内存管理策略（默认 `AUTO`）；配置则是一次切分的结果字段：

```cpp
struct DefaultBlockConfig {
    uint32_t wholeLoop = 0;    // 完整 Tile 的循环次数（不含尾块）
    uint32_t tileCnt = 0;      // 尾块处理的元素数（完整块时为 0）
    uint32_t basicNum = 0;     // 每个完整 Tile 处理的元素数
    uint32_t totalElemCnt = 0; // 当前 block 要处理的元素总数
};

template <typename Shape>
struct DefaultBlockPolicy {
    using TileShape = Shape;
    Shape tileShape{};
    Atvoss::MemMngPolicy memPolicy = Atvoss::MemMngPolicy::AUTO;
};
```

`MemMngPolicy` 取值见 [include/utils/patterns.h:33-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L33-L37)（`MANUAL` / `AUTO`）。本讲只需知道默认是 `AUTO`，缓冲由框架自动管理；二者的区别留到专家篇 U3-L8 讲。

#### 4.1.4 代码实践

**实践目标**：理解 `TileShape` 对运行行为的影响。

**操作步骤**：

1. 打开 `examples/abs/abs.cpp`，定位到 [第 16 行 `TILE_SIZE`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L16)。
2. 阅读它如何被 [第 23 行 `TileShape`](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L23) 使用。
3. 在脑中推演：若把 `TILE_SIZE` 从 `32` 改成 `4096`（不动其他代码），单核内一次 Tile 处理的元素数变大，整个输入被切成的 Tile 数量会变少。

**需要观察的现象**：Tile 数量变少意味着 Block 层的 `wholeLoop` 变小（更少次循环），但单个 Tile 占用的 UB 空间变大。

**预期结果**：逻辑上输出不变（取绝对值的结果与分块方式无关），但执行时的循环次数与内存占用会变化。具体数值**待本地验证**（需要 Ascend 真机或 cannsim 才能运行）。

#### 4.1.5 小练习与答案

**练习 1**：`Shape<32>` 和 `Shape<1, 32>` 有什么区别？

**答案**：维度不同。`Shape<32>` 是一维（长度 32），`Shape<1, 32>` 是二维（1 行 32 列）。ATVOSS 用维度数来表达归约/广播的轴方向（详见进阶篇 U2-L4）。abs 是逐元素运算，用一维即可。

**练习 2**：为什么 `blockPolicy` 和 `kernelPolicy` 要用 `static constexpr` 声明？

**答案**：因为它们要作为**非类型模板实参**传给 `BlockBuilder` / `KernelBuilder`（见 [abs.cpp:40-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L40-L42)）。C++ 要求非类型模板实参必须是编译期常量且具备静态链接，`static constexpr` 正好满足。

### 4.2 Compute() 与 PlaceHolder / ParamUsage

#### 4.2.1 概念说明

`Config` 里的 `AbsCompute` 是用户**唯一需要动脑写计算逻辑**的地方。它只有一个核心成员函数 `Compute()`，里面做两件事：

1. 用 `PlaceHolder` 声明算子的「入参 / 出参」——也就是公式里的变量。
2. 用 `return` 一行表达式描述计算——也就是公式本身。

`PlaceHolder` 顾名思义是「占位符」：它不代表真实数据，而是代表「这里有一个第 N 号参数」。真实的张量数据在运行时才由框架注入。`ParamUsage` 则标注这个参数是输入（`IN`）、输出（`OUT`）还是既输入又输出（`IN_OUT`）。

数学上 abs 要表达的是：

\[
\operatorname{Abs}(\text{in}) = |\text{in}|
\]

在 ATVOSS 里这几乎逐字写成 `out = Abs(in)`。

#### 4.2.2 核心流程

`Compute()` 的执行（编译期）流程：

```
Compute() 被框架调用（传入具体的 Tensor 类型）
  │
  ├─ 1. 声明 in  = PlaceHolder<1, Tensor<float>, IN>()   // 1 号入参
  ├─ 2. 声明 out = PlaceHolder<2, Tensor<float>, OUT>()  // 2 号出参
  │
  └─ 3. return (out = Abs(in))
            │
            └─ 这不是普通赋值，而是用运算符重载构造一棵「表达式 AST」
                 Expression<OpAssign<Param<2,...>, OpAbs<Param<1,...>>>>
```

注意三点：

- `PlaceHolder` 的第一个模板参数是**序号 N**（1、2、3…），它决定参数在 `ArgumentsBuilder` 里的顺序（见 4.3 节与 U2-L6）。
- `out = Abs(in)` **不是运行时赋值**，而是构造了一个类型化的表达式对象。`=` 和 `Abs()` 都是经过重载的 `constexpr` 函数。
- 同一个 `Compute()` 会被**复用两次**：一次在 Host 侧（用于分析参数、做 Tiling），一次在 Device 侧（用于真正计算）。这就是为什么 `Tensor` 要写成「模板的模板形参」——让框架在不同阶段替换成不同的张量类型。

#### 4.2.3 源码精读

先看 `Compute()` 本体：

[examples/abs/abs.cpp:24-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L24-L32) —— `AbsCompute` 的全部内容：

```cpp
struct AbsCompute {
    template <template <typename> class Tensor>
    __host_aicore__ constexpr auto Compute() const
    {
        auto in  = Atvoss::PlaceHolder<1, Tensor<Dtype>, Atvoss::ParamUsage::IN>();
        auto out = Atvoss::PlaceHolder<2, Tensor<Dtype>, Atvoss::ParamUsage::OUT>();
        return (out = Abs(in));
    };
};
```

逐行解读：

- `template <template <typename> class Tensor>` —— `Tensor` 是一个「单参数模板」，框架会把它替换为具体的张量包装类型（Host 侧的 `DeviceTensor`、Device 侧的 `LocalTensor`）。
- `__host_aicore__` —— 表示这个函数既能在 Host CPU 上编译运行，也能在 AI Core 上编译运行。
- `PlaceHolder<1, Tensor<Dtype>, IN>()` —— 声明 1 号参数，类型是 `Tensor<float>`，用途为输入。

`PlaceHolder` 的定义非常薄，它就是 `Param` 的语法糖：

[include/expression/expr_template.h:604-608](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L604-L608) —— `PlaceHolder` 返回一个包装了 `Param` 的 `Expression`：

```cpp
template <std::size_t N, typename T, ParamUsage U = ParamUsage::IN>
__host_aicore__ constexpr auto PlaceHolder()
{
    return Expression<Param<N, T, U>>{};
}
```

`Param` 与 `ParamUsage` 的定义：

[include/expression/expr_template.h:34-39](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L34-L39) —— `ParamUsage` 是个三值枚举：

```cpp
enum class ParamUsage {
    IN,
    OUT,
    IN_OUT,
};
```

[include/expression/expr_template.h:98-114](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L98-L114) —— `Param` 记录序号 `number`、用途 `usage`，以及为 `IN_OUT` 场景准备的 `inplaceNumber`（让两个 Param 指向同一块 GM）：

```cpp
template <std::size_t N, typename T, ParamUsage U = ParamUsage::IN, std::size_t RN = N>
struct Param {
    using Type = T;
    static constexpr std::size_t number = N;
    static constexpr std::size_t inplaceNumber = RN; // IN_OUT 时两个 Param 指向同一块 GM
    static constexpr ParamUsage usage = U;
    ...
};
```

那么 `out = Abs(in)` 是怎么变成表达式的？关键在于 `Expression` 重载了 `operator=`：

[include/expression/expr_template.h:470-483](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L470-L483) —— 赋值被重载为构造 `OpAssign`：

```cpp
template <typename T>
template <typename U>
__host_aicore__ constexpr auto Expression<T>::operator=(Expression<U> u)
{
    ...
    return Expression<OpAssign<T, U>>{{data, u.data}};
}
```

而 `Abs` 是用宏 `DeclareUnaryOp(Abs)` 生成的。该宏在 [include/expression/expr_template.h:551-567](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L551-L567) 定义，会生成一个 `OpAbs` 结构体和一个同名的 `Abs()` 工厂函数：

```cpp
#define DeclareUnaryOp(Name)                          \
    template <typename T>                             \
    struct Op##Name : UnaryOp<T> { ... };             \
    template <typename T>                             \
    __host_aicore__ constexpr auto Name(Expression<T> lhs) { \
        return Expression<Op##Name<T>>{{lhs.data}};   \
    } ...
```

[include/operators/math_expression.h:169](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L169) —— 一行 `DeclareUnaryOp(Abs);` 就生成了上面这一切。

于是 `out = Abs(in)` 在编译期被逐步归约为：

\[
\texttt{Expression<OpAssign<Param<2,...>,\ OpAbs<Param<1,...>>>}
\]

这个嵌套类型就是「计算公式」的编译期表示，后续整个框架都建立在它之上。

#### 4.2.4 代码实践

**实践目标**：把一个数学公式翻译成 ATVOSS 表达式，并推断其 AST 类型。

**操作步骤**：

1. 阅读 [abs.cpp:30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L30) 的 `return (out = Abs(in));`。
2. 假设你想实现「输出等于输入加上输入自身」，即 \(\text{out} = \text{in} + \text{in}\)。在草稿纸上写出对应的表达式：`return (out = (in + in));`
3. 查 [include/operators/math_expression.h:29-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L29-L45)，确认 `operator+` 会把 `in + in` 归约为 `Expression<OpAdd<Param<1,...>, Param<1,...>>>`。

**需要观察的现象**：`+`、`=` 都不是 C++ 内建语义，而是被 ATVOSS 重载成了「类型构造器」。

**预期结果**：最终 `out = in + in` 的 AST 类型为 `Expression<OpAssign<Param<2,...>, OpAdd<Param<1,...>, Param<1,...>>>>`。本任务为源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `out` 的 `ParamUsage` 误写成 `IN` 会怎样？

**答案**：框架会根据 `usage` 收集入参/出参集合（见 [device_adapter.h:241-251](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L241-L251) 的 `GetOutParamsImpl`）。把 `out` 标成 `IN` 会导致它被当成输入，输出张量不会被正确回写，运行结果错误。

**练习 2**：`PlaceHolder<1, ...>` 和 `PlaceHolder<2, ...>` 里的数字 1、2 有什么含义？可以随便填吗？

**答案**：这是参数的**序号**，决定它在参数列表中的位置，必须与运行时 `ArgumentsBuilder{}.inputOutput(t1, t2)` 的顺序一一对应（[abs.cpp:175](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L175) 里 `t1` 对应 1 号、`t2` 对应 2 号）。不能随便填，且一般要连续、不重复。

**练习 3**：为什么 `Compute()` 要写成模板函数，并把 `Tensor` 写成 `template <typename> class Tensor`？

**答案**：为了让同一份计算表达式既能被 Host 侧（做参数分析、Tiling，注入 `DeviceTensor`）复用，也能被 Device 侧（真正执行计算，注入 Ascend C 的 `LocalTensor`）复用。用「模板的模板形参」让框架在各阶段替换具体的张量包装类型。

### 4.3 BlockOp / KernelOp / DeviceOp 三级 Builder 组装

#### 4.3.1 概念说明

4.1 节填好了「配置清单」，4.2 节写好了「计算公式」。本节看最后一步：如何用一个**三层套娃**把这两样东西组装成一个可以 `.Run()` 的 `DeviceOp`。

这三层正好对应五层架构里的 Block、Kernel、Device 三层（Tile 和 Basic 层在内部，用户不直接接触）：

- `BlockBuilder` 包装「单核内怎么按 Tile 切分」。
- `KernelBuilder` 包装「多核之间怎么切分」，它内部持有上一个 `BlockOp`。
- `DeviceAdapter` 包装「Host 侧怎么准备参数、算 Tiling、启动 Kernel」，它内部持有上一个 `KernelOp`。

每一层都接受上一层的结果作为模板参数，层层递进。

#### 4.3.2 核心流程

组装与运行的流程：

```
【组装（编译期）】
AbsCompute                                                  （用户写的公式）
   └─ BlockBuilder<AbsCompute, ArchTag, blockPolicy, ...>   → BlockOp
        └─ KernelBuilder<BlockOp, kernelPolicy>             → KernelOp
             └─ DeviceAdapter<KernelOp>                     → DeviceOp（类型别名）

【运行（运行期，Host 侧）】
DeviceOp deviceOp;
deviceOp.Run(arguments, stream);
   └─ DeviceAdapter::Run 内部三步：
        1. PrepareParams    —— 把 arguments 里的张量整理成参数 tuple
        2. CalculateTiling  —— 算出 kernelParam（核间）与 blockParam（核内 Tile）
        3. LaunchKernelWithDataTuple —— 用 <<<blockNum>>> 启动真实 Kernel
```

#### 4.3.3 源码精读

先看 `abs` 的组装三行：

[examples/abs/abs.cpp:40-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L40-L44) —— 三级 Builder 一层套一层：

```cpp
using BlockOp   = Atvoss::Ele::BlockBuilder<AbsCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig>;
using KernelOp  = Atvoss::Ele::KernelBuilder<BlockOp, kernelPolicy>;
using DeviceOp  = Atvoss::DeviceAdapter<KernelOp>;
```

逐层看 Builder 的模板签名。

**BlockBuilder**（单核 Tile 层）：

[include/elewise/block/builder.h:42-59](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L42-L59) —— 接收 `Compute`、架构、策略、配置，并把真正的切分逻辑委托给 `Schedule`：

```cpp
template <typename Compute, typename ArchTagcfg = Atvoss::Arch::DAV_3510,
          const auto& Policy = defaultBlockPolicy, typename ScheduleCfg = DefaultBlockConfig,
          template <typename, const auto&, typename, typename> class Schedule = DefaultBlockSchedule>
class BlockBuilder {
public:
    using ScheduleClz = Schedule<Compute, Policy, ScheduleCfg, ArchTagcfg>;
    using BlockTileShape = typename ScheduleClz::TileShape;
    template <typename ArgTup>
    __aicore__ inline void Run(ScheduleCfg& cfg, ArgTup& argTuple) {
        ScheduleClz schedule;
        schedule.Run(cfg, argTuple);   // 真正的 Tile 循环在 Schedule 里
    }
};
```

注意 `Run` 被 `#if !defined(__ATVOSS_HOST_ONLY__)` 包裹——它只在 Device 侧编译，因为 Tile 循环只能在 AI Core 上跑。

**KernelBuilder**（多核层）：

[include/elewise/kernel/builder.h:39-68](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L39-L68) —— 接收上一个 `BlockOp`，并通过 `OpParam` 把 kernel 与 block 两层配置打包在一起：

```cpp
template <typename BlockOp, const auto& Policy = defaultKernelPolicy,
          typename ScheduleCfg = DefaultKernelConfig,
          template <typename, const auto&, typename> class Schedule = DefaultKernelSchedule>
class KernelBuilder {
public:
    struct OpParam {
        ScheduleCfg kernelParam;                       // 核间切分结果
        typename BlockOp::ScheduleCfgClz blockParam;   // 核内 Tile 切分结果
    };
    template <typename OpParam, typename... Args>
    __aicore__ inline void Run(OpParam& cfg, Args... args) {
        ScheduleClz schedule;
        schedule.Run(cfg, args...);
    }
};
```

`DefaultKernelConfig` 的字段（[builder.h:16-22](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L16-L22)）就是多核切分的输出：`blockNum`（启动核数）、`unitNumPerCore`（每核单元数）、`moreUnitCoreNum`（多处理一个单元的核数）、`tailNum`（尾数）、`unitNum`（每单元元素数）。这些在进阶篇 U2-L8 会详讲。

**DeviceAdapter**（Host 入口）：

[include/elewise/device/device_adapter.h:76-90](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L76-L90) —— 顶层入口，对外只暴露一个 `Run`：

```cpp
template <typename KernelOp>
class DeviceAdapter {
public:
    using OpParam = typename KernelOp::ScheduleCfgClz;   // 就是上面的 KernelOp::OpParam
    ...
    template <typename Args>
    int64_t Run(const Args& arguments, aclrtStream stream = nullptr);
};
```

`Run` 的三步主流程：

[include/elewise/device/device_adapter.h:97-124](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L97-L124) —— `PrepareParams → CalculateTiling → LaunchKernelWithDataTuple`：

```cpp
template <typename Args>
int64_t Run(const Args& arguments, aclrtStream stream = nullptr) {
    ...
    auto argTuple = std::get<0>(arguments);
    auto params = PrepareParams<Params>(argTuple);          // 1. 整理参数

    OpParam opParam;
    CalculateTiling<KernelOp>(arguments, opParam);          // 2. 算核间+核内 tiling

    auto convertArgs = ConvertArgs<Params>(params, argTuple);
    LaunchKernelWithDataTuple<KernelOp>(                    // 3. 启动 kernel
        opParam.kernelParam.blockNum, stream, opParam, convertArgs);
    return 0;
}
```

第 3 步最终调用的是跨 Host/Device 边界的唯一跳板——用 `<<<blockNum>>>` 三尖括号语法启动的 `KernelCustom`：

[include/elewise/device/device_adapter.h:37-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L37-L42) —— `KernelCustom` 是真正的 kernel 函数，`blockNum` 决定启动多少个核：

```cpp
template <class KernelOp, typename OpParam, typename ArgTuple>
__global__ __aicore__ void KernelCustom(OpParam cfg, ArgTuple args) {
    KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY);
    KernelWrapper<KernelOp, OpParam, ArgTuple>(cfg, args, ...);
}
```

最后回到 `abs` 看它怎么真正调用 `DeviceOp`：

[examples/abs/abs.cpp:178-180](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L178-L180) —— 构造 `DeviceOp` 实例并 `.Run()`：

```cpp
using DeviceOp = typename AbsConfig<T>::DeviceOp;
DeviceOp deviceOp;
deviceOp.Run(arguments, stream);
```

其中 `arguments` 来自 [abs.cpp:175](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L175) 的 `ArgumentsBuilder{}.inputOutput(t1, t2).build()`。注意 `t1`（输入）对应 `PlaceHolder<1>`、`t2`（输出）对应 `PlaceHolder<2>`——这就是 4.2 节强调的「序号必须对齐」。关于 `ArgumentsBuilder` 与整个 Host 侧 ACL 流程，下一讲 U1-L5 会专门讲。

> 小结一句：用户写的只有 `AbsCompute`；`BlockBuilder/KernelBuilder/DeviceAdapter` 三个 Builder 的模板参数像「俄罗斯套娃」一样把计算描述层层包裹，最终暴露出一个简单的 `.Run(arguments, stream)`。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `deviceOp.Run(arguments, stream)` 的调用链。

**操作步骤**：

1. 从 [abs.cpp:180](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L180) 的 `deviceOp.Run(arguments, stream)` 出发。
2. 跳到 [device_adapter.h:98](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L98) 的 `DeviceAdapter::Run`。
3. 依次定位三步：`PrepareParams`（L106）、`CalculateTiling`（L110）、`LaunchKernelWithDataTuple`（L121）。
4. 在 `LaunchKernelWithDataTuple` 内（[L60-70](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L60-L70)）找到 `KernelCustom<...><<<blockNum, nullptr, stream>>>(...)`，确认 `blockNum` 来自 `opParam.kernelParam.blockNum`。

**需要观察的现象**：tiling 的计算结果（`opParam`）是如何一路传递到真实 kernel 启动的。

**预期结果**：你能画出一条从 `deviceOp.Run` → `PrepareParams/CalculateTiling/LaunchKernelWithDataTuple` → `KernelCustom<<<blockNum>>>` 的调用链。本任务为源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`BlockOp`、`KernelOp`、`DeviceOp` 这三个类型别名，能不能调换定义顺序，比如先定义 `DeviceOp` 再定义 `KernelOp`？

**答案**：不能。它们是层层依赖的：`KernelBuilder` 的第一个模板参数是 `BlockOp`，`DeviceAdapter` 的第一个模板参数是 `KernelOp`。必须按 `BlockOp → KernelOp → DeviceOp` 的顺序定义（与 [abs.cpp:40-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L40-L44) 一致）。

**练习 2**：`DeviceAdapter::Run` 里的 `CalculateTiling` 同时计算了 `kernelParam` 和 `blockParam`（见 [device_adapter.h:128-140](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L128-L140)）。为什么要在 Host 侧一次性算完两层 tiling，而不是在 Device 侧现算？

**答案**：因为 Tiling 需要访问完整的输入形状与硬件信息（核数、UB 大小等），这些只在 Host 侧 readily 可得；且 tiling 结果要作为 kernel 启动参数（如 `blockNum`）传给 `<<<...>>>`，必须先于 kernel 启动算好。在 Host 一次算完，kernel 内部只需读取自己的那一份偏移即可。（细节见 U1-L3、U2-L7。）

## 5. 综合实践

**任务**：参照 `abs`，把算子改写成 `Neg`（取负），并组装出可运行的 `DeviceOp`。

数学定义：

\[
\operatorname{Neg}(\text{in}) = -\text{in}
\]

**操作步骤**：

1. 复制 `examples/abs/abs.cpp` 为一份草稿（不要改原文件），把 `AbsConfig` 改名为 `NegConfig`，把内部的 `AbsCompute` 改名为 `NegCompute`。
2. 修改 `Compute()` 里的 `return`。**关键提示**：先查 [include/operators/math_expression.h:55-71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L55-L71)，你会发现 ATVOSS **只重载了二元 `operator-`**（`Expr-Expr`、`Expr-标量`、`标量-Expr`），**没有提供一元负号** `operator-`。因此最稳的写法是用二元减法：

   ```cpp
   // 示例代码（非项目原有）
   return (out = (0 - in));   // 0 是标量左值，匹配 operator-(T&&, Expression<U>)
   ```

   这会把表达式归约为 `Expression<OpAssign<Param<2,...>, OpSub<int, Param<1,...>>>>`。
3. `Dtype`、`TileShape`、`blockPolicy`、`kernelPolicy`、`ArchTag` 都可保持与 abs 一致。
4. 三级 Builder 同样照搬：

   ```cpp
   // 示例代码（非项目原有）
   using BlockOp  = Atvoss::Ele::BlockBuilder<NegCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig>;
   using KernelOp = Atvoss::Ele::KernelBuilder<BlockOp, kernelPolicy>;
   using DeviceOp = Atvoss::DeviceAdapter<KernelOp>;
   ```

5. 修改 `Run` 里的期望输出（golden）：abs 的 golden 是 `1.5f`（[abs.cpp:188](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L188)），因为输入是 `-1.5f` 取绝对值；Neg 的输入 `-1.5f` 取负后应为 `1.5f`——巧合相同，但请你**想清楚为什么**，并尝试把输入改成 `2.5f` 重新推算 golden。
6. （可选）按 U1-L2 的方式编译：`bash scripts/build.sh -DSOC=ascend950 <your_neg_target>`。

**需要观察的现象 / 预期结果**：

- 你应当发现：相比 `Abs`，真正需要改的只有两处——`Compute()` 里的 `return` 表达式，以及 golden 校验值。`Config` 的其余部分（TileShape、Policy、三级 Builder）完全不变。这正是 ATVOSS「极简编程」的体现：**算子的差异被压缩到一行公式**。
- 关于 `0 - in` 能否在 ascend950 上正确编译并产出 `-in` 的结果：**待本地验证**（本环境无 Ascend 工具链）。

**思考题（选做）**：如果你偏要写 `-in`（一元负号）会怎样？结合 math_expression.h 的重载，预测编译器会报什么错，并说明该错误反映了 ATVOSS 表达式系统的哪个设计选择。

## 6. 本讲小结

- 一个 ATVOSS 算子的标准骨架是 `XxxConfig` 结构体，里面包含 `Dtype`、`TileShape`、`XxxCompute`、`blockPolicy`、`kernelPolicy`、`ArchTag` 以及三级 Builder 别名。
- `TileShape = Atvoss::Shape<int...>` 把分块形状编码在**类型**里，是编译期常量，决定了 Block 层一次 Tile 处理多大块。
- `Compute()` 是用户唯一要写计算逻辑的地方：用 `PlaceHolder<N, Tensor<T>, ParamUsage>` 声明入参/出参，用 `return (out = ...)` 写公式；`=` 和数学函数都是被重载的 `constexpr`，构造的是编译期表达式 AST，而非运行时赋值。
- `PlaceHolder` 的序号 `N` 必须与运行时 `ArgumentsBuilder{}.inputOutput(...)` 的参数顺序一一对应；`ParamUsage`（IN/OUT/IN_OUT）驱动入参/出参的收集。
- 三级 Builder 像套娃：`BlockBuilder<Compute>` → `KernelBuilder<BlockOp>` → `DeviceAdapter<KernelOp>`，最终暴露 `deviceOp.Run(arguments, stream)`。
- `DeviceAdapter::Run` 内部三步：`PrepareParams`（整理参数）→ `CalculateTiling`（算核间 + 核内 tiling）→ `LaunchKernelWithDataTuple`（用 `<<<blockNum>>>` 启动 `KernelCustom`）。用户完全不用手写这些。

## 7. 下一步学习建议

本讲让你学会了「写一个算子的样子」，但还有两块缺口：

1. **Host 侧运行时怎么跑起来**：`ArgumentsBuilder` 的 `inputOutput/build` 到底产出什么？ACL 资源（aclInit/SetDevice/CreateStream/Malloc/Memcpy）的标准样板是什么？输出怎么校验？→ 下一讲 **U1-L5「算子运行时执行流程：ACL 与 Device 调用」** 会以 `muls` 为模板补齐这块。
2. **进阶：表达式系统与分层调度的内部机制**：`out = Abs(in)` 这棵 AST 是怎么变成 Ascend C API 的？三级 Builder 内部到底切了什么？→ 进入进阶篇 **U2**：先读 **U2-L1「表达式模板基础」**，再按顺序读 **U2-L7~L9** 的 Device/Kernel/Block 三层调度。

建议在进入下一讲前，先把本讲的「综合实践：Neg」在本地跑通——亲手改一个算子，胜过读十遍文档。
