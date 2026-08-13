# 数据模型：Tensor、Shape、Layout 与 Patterns

## 1. 本讲目标

在前几讲里，你已经学会了用 `Compute()` 写一行计算公式、用 `ArgumentsBuilder` 组装入参、用三级 Builder 把算子跑起来。但你可能会疑惑：

- 用户传进来的「数据」到底是个什么东西？
- `Config` 里的 `TileShape` 为什么能写成 `Shape<32>` 或 `Shape<1, 32>`？这些尖括号里的数字去了哪里？
- 框架内部是怎么用一个统一的「数据包裹」把 GM 指针、临时变量、缓冲池、当前 Tile 的偏移等信息，一路传递到最底层的求值器的？

本讲就来回答这些问题。我们抽出 ATVOSS 里**描述数据**的一组基础类型，它们是后续所有调度、求值、图优化逻辑的地基。学完本讲，你应当能够：

1. 说出 `Atvoss::Tensor<T>` 的字段构成，并用它包装一个「设备指针 + 形状」。
2. 理解 `Shape<int...>` 如何把形状**编码进类型**，并能区分数值阶段（`Tensor` 的 `shape_[]`）与编译期阶段（`Shape<>`）。
3. 解释 `OperationShape` 的 `axis0/axis1/axis2` 含义，看懂 `FixedRankExtents` 与 `TailLayout` 两种 Layout 形态。
4. 列举 `patterns.h` 中的四个策略枚举 `Pattern / CastMode / MemMngPolicy / MemLevel`，并说明各自的用途。
5. 描述 `ContextData` 这个「单核单 Tile 上下文包裹」的六个字段，理解它在求值链路中扮演的角色。

## 2. 前置知识

- **编译期 vs 运行期**：C++ 模板可以把信息编码进类型，在编译阶段就确定下来，运行时零开销；而普通的变量值要到程序跑起来才确定。ATVOSS 同时使用了这两种手段——形状的「结构」在编译期定型，形状的「具体数值」在运行期从 Host 传入。
- **`std::integral_constant`**：一个把「整数常量」包装成「类型」的标准工具，`std::integral_constant<size_t, 32>` 是一个类型，但它携带的 `::value` 等于 32。它是 ATVOSS 把形状塞进类型系统的关键零件。
- **GM 与 UB**：昇腾 AI Core 里，全局内存（GM, Global Memory）是大但慢的设备显存，统一缓冲（UB, Unified Buffer）是小但快的片上内存。算子执行要把数据从 GM 搬到 UB 计算，再搬回 GM。
- **Tile（分块）**：一个核要处理的数据太多，UB 装不下，就切成一块块「Tile」循环处理。`TileShape` 描述的就是「一次 Tile 处理多少数据」。
- 本讲承接 [u1-l4 用户编程模型](u1-l4-abs-programming-model.md)（你已经见过 `TileShape`、`Tensor`、`ArgumentsBuilder`）与 [u2-l2 参数与占位符](u2-l2-placeholder-and-param.md)（你已经见过 `ParamUsage`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/utils/tensor.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h) | 定义 `Atvoss::Tensor<T>`：包装「设备指针 + 运行期形状」的轻量张量，是 Host 侧入参的载体。 |
| [include/utils/layout/shape.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/shape.h) | 定义 `Shape<int...>`：把形状编码进类型的**编译期形状**，是 `TileShape` 的底座。 |
| [include/utils/layout/layout.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h) | 定义 `OperationShape`、`FixedRankExtents`、`Layout`、`TailLayout`：把 Shape 翻译为算子内部使用的轴描述。 |
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | 集中定义四个策略枚举：`Pattern`（归约/广播方向）、`CastMode`（类型转换舍入）、`MemMngPolicy`（内存管理策略）、`MemLevel`（缓冲复用等级）。 |
| [include/common/type_def.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h) | 定义 `ContextData`：单核单 Tile 执行时一路下传的「上下文包裹」。 |

辅助理解（不在本讲源码清单内，但用于说明数据模型的实际消费场景）：

| 文件 | 作用 |
|------|------|
| [include/operators/tile_shape.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h) | 从 BlockPolicy 中萃取 `TileShape`、计算 `GetTotalElement`（一个 Tile 的元素总数）。 |
| [include/common/arch.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h) | 定义 `Arch::DAV_3510` 的硬件常量 `CORE_NUM`、`UB_SIZE`。 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | 消费 `Shape`/`Layout`/`ContextData` 的典型场所：把 TileShape 翻译成 UB 布局，并构造 ContextData。 |

## 4. 核心概念与源码讲解

### 4.1 Atvoss::Tensor：运行期设备张量

#### 4.1.1 概念说明

`Atvoss::Tensor<T>` 是用户在 **Host 侧**接触到的张量类型。它的定位非常克制：**只做「设备指针 + 形状」的包装，不持有、不分配、不释放任何内存**。内存的申请与释放由用户自己用 `aclrtMalloc`/`aclrtFree` 完成（参见 [u1-l5 运行时执行流程](u1-l5-runtime-flow.md)），Tensor 只是把那块已分配显存的地址和它的形状记录下来，交给 `ArgumentsBuilder`。

这种「轻包装」设计让 ATVOSS 不去抢夺内存所有权，避免了与 ACL/PyTorch 等宿主框架在生命周期管理上的冲突。

#### 4.1.2 核心流程

一个 `Tensor<T>` 的生命周期可以概括为：

1. 用户 `aclrtMalloc` 拿到一块 Device 显存指针。
2. 用 `Tensor<T>(指针, 形状数组, 维度数)` 把指针和形状打包。
3. 喂给 `ArgumentsBuilder{}.inputOutput(...)`。
4. `DeviceOp::Run` 内部从 Tensor 里取回指针与形状，搬到 Kernel 执行。

#### 4.1.3 源码精读

`Tensor` 首先约定了一个最大维度上限 `MAX_DIMS = 8`，这与昇腾张量通用的 8 维上限一致：

[include/utils/tensor.h:19-20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L19-L20) —— 命名空间与 `MAX_DIMS` 常量。

类的私有成员只有三个：一个定长形状数组、一个 `void*` 数据指针、一个维度计数。注意形状数组是**定长 8 槽**的 `uint64_t` 数组，而不是 `std::vector`——这避免了堆分配，让 Tensor 保持轻量：

[include/utils/tensor.h:77-80](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L77-L80) —— 三个私有字段：`shape_[MAX_DIMS]`、`dataPtr_`、`dims_`。

构造函数有三个重载，覆盖「编译期已知维度（数组引用）」与「运行期才知道维度（指针 + dims）」两种用法。最常用的是第三个，abs 样例正是这样构造的：

[include/utils/tensor.h:42-48](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L42-L48) —— `(T*, int64_t*, size_t)` 重载，运行期校验维度合法性后拷贝形状。

访问器很简单：`data()` 取回类型化指针，`shape()` 取 C 数组首地址，`shape_vector()` 拷贝成 `std::vector`，`dims()` 取维度数。类里还有一个 SFINAE 标签 `IsTensor`，供后续模板判断「这是不是一个 Tensor」：

[include/utils/tensor.h:52-75](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L52-L75) —— 四个访问器与 `IsTensor` 标签。

真实用法（abs 样例）：先用 `aclrtMalloc` 拿到 `deviceInput`，再把 Host 传来的 `shape` 拷进一个 `uint64_t` 数组，构造两个 Tensor：

[examples/abs/abs.cpp:171-175](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L171-L175) —— 构造 `t1`/`t2` 两个 Tensor 并交给 ArgumentsBuilder。

> 关键结论：`Tensor` 是「数值阶段的形状容器」，形状的具体数字在运行期才填入；而下一节的 `Shape<>` 恰好相反，是「类型阶段的形状容器」。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `Atvoss::Tensor`，验证它「只记录、不分配」。

**操作步骤**：

1. 打开 `examples/abs/abs.cpp`，定位到第 171–175 行。
2. 阅读第 157–158 行 `aclrtMalloc(&rawInput, ...)`，确认 `deviceInput` 这块显存是**用户自己**申请的。
3. 想象在第 173 行后插一行（仅为阅读理解，不实际修改源码）：
   ```cpp
   // 示例代码，仅用于理解，不要写入仓库
   std::cout << "t1 dims=" << t1.dims() << " elem0=" << t1.shape()[0] << "\n";
   ```

**需要观察的现象**：`t1.dims()` 应等于 `shape.size()`（即 `--shape` 参数的维度数），`t1.shape()[0]` 应等于用户传入的第一个维度值。

**预期结果**：能复述「Tensor 没有析构函数去 free 显存（`~Tensor() = default;`，见 tensor.h:50），显存的释放由 abs.cpp 第 158 行的 `ReleaseSource` RAII 守卫负责」。

**待本地验证**：因本实践依赖真机/仿真环境，具体打印数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Tensor` 的形状数组用定长 `uint64_t shape_[MAX_DIMS]` 而不是 `std::vector<uint64_t>`？

> **参考答案**：定长数组在栈上分配、无堆开销，构造/析构零成本；Tensor 是一个会被频繁构造的轻量包装，避免堆分配能减小 Host 侧开销。`MAX_DIMS=8` 与昇腾张量维度上限一致，足以覆盖合法输入。

**练习 2**：`Tensor::data()` 返回 `T*`，但成员 `dataPtr_` 是 `void*`。为什么要存成 `void*`？

> **参考答案**：`void*` 让同一个存储能容纳任意元素类型的指针，避免模板实例化间的不必要耦合；对外通过 `static_cast<T*>(dataPtr_)` 恢复类型，既安全又通用。

### 4.2 Shape<int...>：编译期形状

#### 4.2.1 概念说明

`Shape<int... a>` 与 `Tensor` 是一对镜像：`Tensor` 在运行期记录形状的**数值**，`Shape` 在编译期把形状编码进**类型**。你在 `Config` 里写的 `using TileShape = Atvoss::Shape<32>;` 或 `Atvoss::Shape<1, 32>;`，那些数字 `32`、`1`、`32` 并不作为变量存在，而是变成了类型 `Shape<32>` / `Shape<1, 32>` 的一部分。

这样做的好处是：框架能在编译期就知道「一次 Tile 要处理多少元素」「这是个一维还是二维 Tile」，从而提前算好 UB 布局、循环次数、对齐等参数，运行时零开销。

#### 4.2.2 核心流程

`Shape<int... a>` 内部用 `std::tuple` 把每个维度包装成 `std::integral_constant<size_t, a>`：

- 维度数（rank）= `sizeof...(a)`，存为 `Shape::size`。
- 第 N 维的值 = `Shape::get_type<N>::value`。

也就是：尖括号里的整数列，被翻译成一个「编译期整数序列」。

#### 4.2.3 源码精读

整个 `Shape` 的定义极简，核心就是把可变参数 `a...` 装进 `std::tuple`：

[include/utils/layout/shape.h:18-27](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/shape.h#L18-L27) —— `Shape` 类模板：`Types` 是 integral_constant 的 tuple，`get_type<N>` 取第 N 维，`size` 是维度数。

两个要点：

1. `template <int... a>` 用的是 **有符号 int**，但 `integral_constant` 的第一参数是 **`size_t`**（无符号）。所以 `Shape<-1>` 虽然语法上能写，但语义上维度必须为正，调度层会 `static_assert` 拦截（见 4.3.3）。
2. `Shape` **没有任何运行期数据成员**——它是一个空类型，所有信息都在类型本身。这就是「零运行时开销」的体现。

ATVOSS 内部用一组萃取工具来读取 Shape。例如 `Shape_t<T>` 从 BlockPolicy 里取出它的 `TileShape`，`ShapeSize` 读取维度数：

[include/operators/tile_shape.h:43-65](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L43-L65) —— `ShapeImpl`/`Shape_t` 取出 `T::TileShape`，`ShapeSize` 读取 rank。

而「一个 Tile 一共多少元素」由 `GetTotalElement` 在编译期累乘所有维度得到：

[include/operators/tile_shape.h:72-102](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L72-L102) —— `GetTotal` 递归累乘，`GetTotalElement` 是对外入口。

举两个具体例子（本讲后面会反复用到）：

- abs 的 `Shape<32>`（一维）：累乘 → \( 32 \)，所以一个 Tile 处理 32 个元素。
- rms_norm 的 `Shape<1, 32>`（二维）：累乘 → \( 1 \times 32 = 32 \)。

二者的元素总数相同，但**轴结构不同**——这会直接影响归约/广播的方向（详见 [u2-l4](u2-l4-reduce-broadcast-operators.md)）。

#### 4.2.4 代码实践

**实践目标**：在纸上推导两种 TileShape 在类型系统里的展开。

**操作步骤**：

1. 对照 shape.h:21，写出 `Shape<32>::Types` 的等价形式。
2. 对照 shape.h:21，写出 `Shape<1, 32>::Types` 的等价形式。
3. 对照 tile_shape.h:73-89，手算 `GetTotal<0, Shape<1,32>>(1,1)` 的递归过程。

**需要观察的现象**：

- `Shape<32>::Types` 应为 `std::tuple<std::integral_constant<size_t, 32>>`，`size` = 1。
- `Shape<1, 32>::Types` 应为 `std::tuple<integral_constant<size_t,1>, integral_constant<size_t,32>>`，`size` = 2。
- `GetTotal<0, Shape<1,32>>(1,1)`：N=0 取 axis0=1，defaultSize=1；N<1 递归 N=1 取 axis1=32，defaultSize=32；返回 32。

**预期结果**：能复述「`Shape` 的 rank 就是 `sizeof...(a)`；元素总数是各维度之积」。

#### 4.2.5 小练习与答案

**练习 1**：`Shape<8, 4, 2>` 的 `size` 是几？`get_type<2>::value` 是几？

> **参考答案**：`size` = 3（三个维度），`get_type<2>::value` = 2（第 3 维的值，下标从 0 起）。

**练习 2**：为什么 ATVOSS 把 TileShape 设计成编译期类型，而不是像 `Tensor` 那样运行期传值？

> **参考答案**：TileShape 决定的是「调度结构」——UB 布局、Tile 循环次数、对齐——这些都是算子**编译**时就该定下来的，定下来后就能生成最优的 Kernel 代码。把它放进类型系统，框架能在编译期完成全部推导，运行时不再判断、零开销；而 `Tensor` 的形状是用户**每次调用**都可能变化的真实数据，只能在运行期承载。

### 4.3 Layout 与 OperationShape：把 Shape 翻译成轴

#### 4.3.1 概念说明

`Shape<int...>` 是「给用户写的」编译期形状，但它只是一个整数序列，没有「轴」的概念。框架内部（尤其是 Tile/Basic 层调用 Ascend C API 时）需要一个更具体的轴描述：这是几维操作？每个轴多长？`Layout` 这一层就是做这个翻译的。

核心结构是 `OperationShape`——一个有三个字段 `axis0 / axis1 / axis2` 的简单结构体，约定：

- 一元运算（Unary，如 `Abs`）用 `axis0` 表示元素总数，记为「UNARY_SHAPE」。
- 二元/二维运算（Binary，如归约、广播）用 `{axis0, axis1}` 表示行×列，记为「BINARY_SHAPE」。
- `axis2` 目前预留给更高维（三元运算）。

这与 [u2-l4](u2-l4-reduce-broadcast-operators.md) 里讲的「axis0 为行(HEIGHT)、axis1 为列(WIDTH)」约定完全一致。

#### 4.3.2 核心流程

ATVOSS 提供两种「从 Shape 得到 OperationShape」的方式：

1. **`FixedRankExtents`（编译期）**：维度在编译期已知，直接把 `Shape` 的整数填进 `OperationShape` 的静态常量。这是逐元素算子的默认路径。
2. **`TailLayout`（运行期）**：维度在运行期才知道（比如尾 Tile 的剩余元素数），通过构造函数把数值填进去，运行时用 `GetUnaryShape()`/`GetBinaryShape()` 取。

二者都挂在 `Layout<Shape, Stride>` 这个外壳下，`Layout` 本身只是把 `ShapeType`/`StrideType` 起两个别名，真正的信息在 `Shape` 那一栏。

#### 4.3.3 源码精读

`OperationShape` 三个字段默认都为 1：

[include/utils/layout/layout.h:15-19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L15-L19) —— `OperationShape`：`axis0/axis1/axis2`，默认 1。

`FixedRankExtents<TotalCnt, Axis0, Axis1>` 用三个模板参数生成两个静态 `OperationShape` 常量：一元形状用 `TotalCnt`，二元形状用 `{Axis0, Axis1}`：

[include/utils/layout/layout.h:21-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L21-L25) —— `FixedRankExtents`：编译期产出 `UNARY_SHAPE` 与 `BINARY_SHAPE`。

`Layout` 是个只起别名作用的外壳，把 Shape 与 Stride 类型绑定起来：

[include/utils/layout/layout.h:27-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L27-L32) —— `Layout` 模板，导出 `ShapeType`/`StrideType`。

`TailLayout` 则是运行期可变的版本，构造函数接收尾数与两轴数值，存为成员，运行时再取：

[include/utils/layout/layout.h:37-63](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L37-L63) —— `TailLayout`：构造函数填值，`GetUnaryShape()`/`GetBinaryShape()` 取值。

那么 `Shape<...>` 是怎么变成 `FixedRankExtents<...>` 的？答案在 Block 调度层。调度层先从 `TileShape` 读取维度数与各轴值，再据此实例化 `FixedRankExtents`：

[include/elewise/block/schedule.h:95-121](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L95-L121) —— 从 `TileShape` 萃取 `ShapeSize`，用 `GetLayoutAxis0/1` 取出 axis0/axis1，组装出 `BlockTensorTile` 的 `Layout<FixedRankExtents<BASIC_BLOCK, axis0, axis1>>`。

注意这段里的两条 `static_assert`：**TileShape 的维度数不能超过 2**，且 **axis0/axis1 必须大于 0**。这解释了为什么 `Shape<1,32>` 合法而 `Shape<2,3,4>` 会在编译期直接报错：

[include/elewise/block/schedule.h:98-117](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L98-L117) —— `GetLayoutAxis0/1` 中的维度上限与正值校验。

> 关键结论：`Shape` 是用户视角的「整数序列」，`Layout/FixedRankExtents` 是框架视角的「带语义的轴」。二者通过调度层的 `GetLayoutAxis0/1` 桥接，桥接时强制 rank ≤ 2。

#### 4.3.4 代码实践

**实践目标**：跟踪 abs 与 rms_norm 两个样例的 Shape → Layout 翻译。

**操作步骤**：

1. 在 `examples/abs/abs.cpp:23` 找到 `using TileShape = Atvoss::Shape<TILE_SIZE>;`（`TILE_SIZE=32`，见第 16 行），这是一维。
2. 在 `examples/rms_norm/rms_norm.cpp:33` 找到 `using TileShape = Atvoss::Shape<HEIGHT, WIDTH>;`（`HEIGHT=1, WIDTH=32`，见第 24-25 行），这是二维。
3. 对照 schedule.h:98-117，分别推导两个样例的 `ShapeSize::value`、`GetLayoutAxis0()`、`GetLayoutAxis1()`。

**需要观察的现象**：

| 样例 | TileShape | ShapeSize | axis0 | axis1 |
|------|-----------|-----------|-------|-------|
| abs | `Shape<32>` | 1 | `BASIC_BLOCK`（=32） | 1 |
| rms_norm | `Shape<1,32>` | 2 | 1 | 32 |

**预期结果**：能说出「一维 Shape 走 `BASIC_BLOCK` 分支，二维 Shape 走 `get_type<0/1>` 分支」，并理解 abs 的 `axis1=1` 意味着它本质被当作单行处理。

#### 4.3.5 小练习与答案

**练习 1**：`FixedRankExtents<32, 1, 32>` 的 `UNARY_SHAPE` 和 `BINARY_SHAPE` 分别是什么？

> **参考答案**：`UNARY_SHAPE = OperationShape{32}`（即 axis0=32，axis1/axis2 仍为默认 1），`BINARY_SHAPE = OperationShape{1, 32}`（axis0=1，axis1=32）。

**练习 2**：如果我写 `using TileShape = Atvoss::Shape<2, 3, 4>;`，会发生什么？

> **参考答案**：编译失败。schedule.h:100 的 `static_assert(ShapeSize::value <= 2, ...)` 会触发，报错信息提示「Tile shape can not be greater than 2!」。ATVOSS 的 TileShape 当前只支持一维或二维。

### 4.4 策略枚举：Pattern / CastMode / MemMngPolicy / MemLevel

#### 4.4.1 概念说明

`patterns.h` 是 ATVOSS 的「策略枚举集合」，四个枚举集中描述了**算子行为开关**。它们都是 `enum class`（强类型枚举），不会和其他名字冲突，也不会被隐式转成 int 乱用。

- **`Pattern`**：归约（`ReduceSum`）与广播（`Broadcast`）的方向标签，取值 `AR/RA/AB/BA`。这一节只做罗列，详细语义（axis0=行、axis1=列，'A'保持/'R'压维/'B'扩维）已在 [u2-l4 归约与广播算子](u2-l4-reduce-broadcast-operators.md) 讲透，本讲不再重复。
- **`CastMode`**：类型转换（`Cast`）时的**舍入方式**，共 7 档。
- **`MemMngPolicy`**：缓冲/内存**管理策略**，决定 DAG 用全自动还是手动构建。
- **`MemLevel`**：缓冲**复用等级**，决定在 UB 紧张时让多少中间量共享同一块缓冲。

#### 4.4.2 核心流程

这四个枚举本身只是常量，真正的逻辑散布在各消费方：

- `Pattern` → `transcendental_evaluator.h` 把它映射到 Ascend C 的归约/广播 API（见 u2-l4）。
- `CastMode` → `math_evaluator.h` 在执行 `Cast` 时选择对应的 Ascend C 舍入模式（见 [u2-l3 运算符库](u2-l3-math-tensor-operators.md)）。
- `MemMngPolicy` → Block 调度层据此选择 `FullAutoDag`（AUTO）或 `ManualDag`（MANUAL）。
- `MemLevel` → 图构建层据此控制缓冲列表的合并程度，缓解 UB 压力。

#### 4.4.3 源码精读

四个枚举集中定义在一个 47 行的小文件里：

[include/utils/patterns.h:13-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L13-L45) —— `Pattern`、`CastMode`、`MemMngPolicy`、`MemLevel` 四个强类型枚举。

逐一看：

- `Pattern`（14-20 行）：`AR / RA / AB / BA`，详见 u2-l4。
- `CastMode`（22-31 行）：`CAST_NONE`（不做特殊舍入）+ 6 种舍入方式：`CAST_RINT`（四舍五入到偶数，默认浮点舍入）、`CAST_FLOOR`（向下取整）、`CAST_CEIL`（向上取整）、`CAST_ROUND`（四舍五入）、`CAST_TRUNC`（截断）、`CAST_ODD`（向奇数舍入）。它配合 `Cast<目标类型, CastMode>(x)` 使用，处理如 float→half/int 时的精度损失策略。
- `MemMngPolicy`（33-37 行）：`MANUAL = 0` 与 `AUTO`，底层类型 `uint8_t`。它是 BlockPolicy 的一部分（见 4.5.3 与 schedule.h:123 的 `Policy.memPolicy`）。
- `MemLevel`（39-44 行）：`LEVEL_0 / LEVEL_1 / LEVEL_2`，三级缓冲复用等级。

`MemMngPolicy` 的消费点很直观——调度层用它选 DAG 构建器：

[include/elewise/block/schedule.h:36-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L36-L44) —— `DagSelector`：`AUTO` 选 `FullAutoDag`（框架自动插 CopyIn/CopyOut/Alloc/Free），`MANUAL` 选 `ManualDag`（由用户/自定义逻辑负责）。

`MemLevel` 的消费点在图构建层，`dag.h` 默认用 `LEVEL_0`，并通过 `ChooseBufferLevel` 在 UB 缓冲数超限时自动降级：

[include/elewise/graph/dag.h:442-471](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L442-L471) —— `FullAutoDag` 的 `memOpt` 默认 `LEVEL_0`，`ChooseBufferLevel` 根据节点数在 LEVEL_0/1/2 间选择。

直观理解三个 `MemLevel`（细节留到 [u3-l5 Buffer 管理与双缓冲](u3-l5-buffer-double-buffering.md)）：

- `LEVEL_2`：复用最激进，最多缓冲、能重叠的都开（含 ping/pong 双缓冲），性能最高但 UB 占用大。
- `LEVEL_1`：中等复用，关掉部分 pong 缓冲。
- `LEVEL_0`：最保守，缓冲数最少，UB 压力大时用它保通过。

#### 4.4.4 代码实践

**实践目标**：在源码中定位四个枚举的消费点，建立「枚举 → 行为」映射。

**操作步骤**：

1. 打开 `include/utils/patterns.h`，把 14-45 行的四个枚举抄在笔记上。
2. 在仓库内搜索 `MemMngPolicy::AUTO`、`MemMngPolicy::MANUAL` 的出现位置（重点看 schedule.h:42）。
3. 在 `include/elewise/graph/dag.h` 内搜索 `MemLevel`，观察 `ChooseBufferLevel`（463-471 行）如何在三级之间分支。
4. 在 `docs/api/README.md`（若存在）或 `include/operators/math_expression.h` 中查找 `CastMode`，确认它作为 `Cast` 的模板参数出现。

**需要观察的现象**：每个枚举都不是「定义了就放着」，而是被具体的 `if constexpr` / 模板特化消费，从而切换算子行为。

**预期结果**：能填写下表（待本地对照源码确认）：

| 枚举 | 取值 | 消费点（文件） |
|------|------|----------------|
| Pattern | AR/RA/AB/BA | transcendental_evaluator.h |
| CastMode | CAST_NONE/RINT/FLOOR/... | math_evaluator.h |
| MemMngPolicy | MANUAL/AUTO | block/schedule.h（DagSelector） |
| MemLevel | LEVEL_0/1/2 | graph/dag.h（ChooseBufferLevel） |

#### 4.4.5 小练习与答案

**练习 1**：`MemMngPolicy::AUTO` 与 `::MANUAL` 的本质区别是什么？

> **参考答案**：`AUTO` 让框架（`FullAutoDag`）自动分析表达式、自动插入 `OpCopyIn/OpCopyOut/OpAlloc/OpFree`，用户只写计算公式；`MANUAL` 则改用 `ManualDag`，把内存搬运与缓冲管理的责任交给（自定义的）手动逻辑，适合需要精细控制缓冲的场景（如双缓冲冗余测试，见 u3-l8）。

**练习 2**：为什么这四个枚举都用 `enum class` 而不是普通 `enum`？

> **参考答案**：`enum class` 是强类型、有作用域的枚举，不会污染外层命名空间，也不会被隐式转成 `int`，避免不同枚举间或与整数的误用（例如把 `Pattern::AR` 当成 `MemLevel` 传）。这对一个重模板、重编译期分派的库尤其重要。

### 4.5 ContextData：单核单 Tile 的上下文包裹

#### 4.5.1 概念说明

前面四节都是「静态描述数据」。`ContextData` 则是**运行期**的「快递箱」：当 Block 调度层把一个核的任务切成一个个 Tile、逐个送去求值时，它把「这次 Tile 执行需要的全部上下文」打包成一个对象，递给最底层的求值器 `Evaluator<Expr>`。

可以这样理解：表达式树（`Expr`）是「做什么」的食谱，`ContextData` 是「现在手头有什么食材、在哪口锅里做、做到第几轮」的现场信息。求值器拿着食谱读现场信息，逐条翻译成 Ascend C 指令。

#### 4.5.2 核心流程

`ContextData` 的组装发生在 Block 调度的 `Process` 循环里，**每个 Tile 构造一次**：

1. 准备好本核的入参张量 `blockTensorsTile`、临时变量 `blockLocalVars`、缓冲池 `bufPools_`。
2. 在 Tile 循环中，把「当前 Tile 在 GM 的偏移 `gmOffset`、本 Tile 元素数 `elementNum`、ping/pong 标志」连同上述三者打包成 `ContextData`。
3. 把这个包裹传给 `Tile::Evaluate<Expr>(context)`，驱动整条表达式执行。

#### 4.5.3 源码精读

`ContextData` 是一个带四个模板参数的结构体（前三个是张量/缓冲类型，第四个是缓冲 ID 映射，默认 `void`）：

[include/common/type_def.h:15-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L15-L25) —— `ContextData` 的六个字段与 `BuffMaps` 别名。

六个字段的含义：

| 字段 | 类型 | 含义 |
|------|------|------|
| `argsTensors` | `OriginalArgs` | 入参/出参张量元组（`BlockTensor` 的 tuple，每个含 GM 地址与 UB LocalTensor） |
| `tmpTensors` | `LocalVars` | 临时变量张量元组 |
| `bufPools` | `BufPools&` | 缓冲池（引用，负责 UB 的 Alloc/Free），整 Block 共享 |
| `gmOffset` | `uint64_t` | 本 Tile 在 GM 中的起始偏移（元素数） |
| `elementNum` | `uint64_t` | 本 Tile 要处理的元素数（完整 Tile = `BASIC_BLOCK`，尾 Tile 可能更小） |
| `pingPong` | `uint32_t` | 双缓冲轮换标志（0 或 1，用于选 ping/pong 缓冲） |

文件里还有一个 deduction guide（推导指引），让你能用 `ContextData(a, b, c)` 这种简写构造、让编译器自动推出模板参数：

[include/common/type_def.h:27-28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L27-L28) —— 构造推导指引。

真正**构造** `ContextData` 的地方在 Block 调度的 `Process`：

[include/elewise/block/schedule.h:236-247](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L236-L247) —— Tile 循环：每个 Tile 用 `i * BASIC_BLOCK` 算 `gmOffset`，用 `BASIC_BLOCK`（或尾 Tile 的 `tileCnt`）做 `elementNum`，用 `i & 1` 产生 `pingPong`，组装出 `context` 后调用 `Evaluate`。

注意 `pingPong = i & 1`：它在 0/1 间交替。求值器在搬数据、分配缓冲时会拿它去选「ping 缓冲」还是「pong 缓冲」，从而让「第 i 次的输入搬运」与「第 i-1 次的计算」重叠，隐藏 MTE2 延迟（这是双缓冲的精髓，详见 u3-l5）。

那么这些字段是怎么被消费的？以 `OpCopyIn` 求值为例：

[include/elewise/tile/tensor_evaluator.h:64-84](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L64-L84) —— `OpCopyIn` 求值：用 `context.pingPong` 经 `GetBufferId` 算出缓冲 ID，再调用 `obj.CopyIn(context.gmOffset, context.elementNum)` 把 GM 数据搬进 UB。

可以看到 `ContextData` 的三个运行期字段在这里各司其职：`pingPong` 决定用哪个缓冲槽，`gmOffset`/`elementNum` 决定从 GM 哪里搬、搬多少。

> 关键结论：`ContextData` 是连接「Block 调度」与「Tile 求值」的统一数据通道。它把六类信息打包，让求值器无需关心上层怎么切分，只管读着包裹执行表达式。

#### 4.5.4 代码实践

**实践目标**：跟踪 `ContextData` 从构造到消费的一手路径。

**操作步骤**：

1. 打开 schedule.h:236-247，看清 `context` 的六个构造实参分别来自哪里。
2. 打开 tensor_evaluator.h:64-84（`OpCopyIn`）与 88 行起（`OpCopyOut`），观察 `context.pingPong`、`context.gmOffset`、`context.elementNum` 各自被谁使用。
3. 用一句话记录每个字段的作用。

**需要观察的现象**：

- `gmOffset` 仅在 `obj.CopyIn(context.gmOffset, context.elementNum)` 这类搬运调用里用作 GM 偏移。
- `pingPong` 先经 `GetBufferId<...>(context.pingPong)` 换算成 `bufferId`，再用作 Mutex 锁的 ID（`Mutex::Lock<PIPE_MTE2>(bufferId)`）。
- `elementNum` 同时影响搬运长度。

**预期结果**：能画出「`Process` 构造 ContextData → `Evaluate` → 各 Op 求值器读 context 字段」的数据流箭头图。

**待本地验证**：若开启 `AscendC::printf`（tensor_evaluator.h 中被注释的调试行），可在仿真日志中观察到 `pingPong` 在 0/1 间交替、`bufferId` 随之变化的现象，具体输出待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `bufPools` 是引用（`BufPools&`），而 `argsTensors`/`tmpTensors` 是值？

> **参考答案**：`bufPools_` 是整个 Block 共享的缓冲池（绑定 `TPipe`，生命周期贯穿所有 Tile），用引用避免每个 Tile 拷贝一份；而 `argsTensors`/`tmpTensors` 在循环外构造一次、每个 Tile 传值（其实是轻量的 `BlockTensor`，内部主要是指针与 LocalTensor 句柄），开销很小且更安全。

**练习 2**：把 `pingPong = i & 1` 改成 `pingPong = 0`（永远只用 ping 缓冲），会发生什么？

> **参考答案**：双缓冲退化为单缓冲，第 i 次的输入搬运与第 i-1 次的计算无法重叠（要等同一块缓冲空闲），MTE2 搬运延迟无法被隐藏，性能下降但结果不变。这正说明了 `pingPong` 字段存在的意义是驱动双缓冲流水。

## 5. 综合实践

把本讲的五个模块串起来，完成一个「为 RMSNorm 设计 TileShape 并追踪数据模型」的小任务。

**任务背景**：rms_norm 算子的数学公式为对输入 `x`（二维，按行）做

\[
\mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{W}\sum_{j} x_j^2}}
\]

即「每行平方和求平均 → 开方 → 用它除该行每个元素」。这要求一次 Tile 同时处理「一整行」，否则归约结果不完整。

**步骤**：

1. **设计 TileShape**：打开 `examples/rms_norm/rms_norm.cpp`，确认第 24-25 行的 `HEIGHT=1, WIDTH=32` 与第 33 行 `using TileShape = Atvoss::Shape<HEIGHT, WIDTH>;`。说明 `axis0=1`（一次处理 1 行）、`axis1=32`（一行 32 列）分别对应什么。如果把 `WIDTH` 设成比真实列数小的值（如 8），归约会发生什么？
2. **走通数据模型链**：对照本讲 4.2–4.4，写出 rms_norm 的 `Shape<1,32>` 经过 `ShapeSize`/`get_type` → `GetLayoutAxis0/1` → `FixedRankExtents<32, 1, 32>` 的翻译结果。
3. **定位策略枚举**：在 rms_norm.cpp 第 49-54 行找到 `blockPolicy`/`kernelPolicy`，再到 schedule.h:123 确认 `Policy.memPolicy` 默认是 `MemMngPolicy` 的哪一个取值（提示：看 `DefaultBlockPolicy` 的默认值），并说明它会让 DAG 走 `FullAutoDag` 还是 `ManualDag`。
4. **追踪 ContextData**：对照 4.5.3，假设单核 `totalElemCnt=64`（即 2 个 Tile），手算两次循环的 `gmOffset`、`elementNum`、`pingPong`。

**预期结果（参考）**：

- 步骤 1：`axis0` 对应 HEIGHT（一次 Tile 处理的行数），`axis1` 对应 WIDTH（列数，即归约轴长度）。`WIDTH` 小于真实列数会导致一次 Tile 只归约部分列，结果错误（这也是 rms_norm 要求 Tile 必须覆盖整行的原因）。
- 步骤 2：`ShapeSize=2`，`axis0=1`，`axis1=32`，`FixedRankExtents<32, 1, 32>`。
- 步骤 3：默认 `AUTO`（具体待对照 `DefaultBlockPolicy` 源码确认），走 `FullAutoDag`，框架自动插 Copy/Alloc/Free。
- 步骤 4：`BASIC_BLOCK = 1×32 = 32`，`wholeLoop = 64/32 = 2`，`tileCnt = 0`。第 0 次：`gmOffset=0, elementNum=32, pingPong=0`；第 1 次：`gmOffset=32, elementNum=32, pingPong=1`。

## 6. 本讲小结

- `Atvoss::Tensor<T>` 是 Host 侧的**轻量包装**，只存「设备指针 + 运行期形状」，不持有内存；形状用定长 `uint64_t[8]` 存储，最大 8 维。
- `Shape<int...>` 把形状**编码进类型**，是 `TileShape` 的底座；元素总数 = 各维度之积，由 `GetTotalElement` 在编译期累乘得到。
- `Layout/OperationShape` 把 `Shape` 翻译成带语义的轴（`axis0/axis1/axis2`），一元用 `axis0`、二元用 `{axis0,axis1}`；调度层强制 `TileShape` 维度 ≤ 2。
- `patterns.h` 汇集四个策略枚举：`Pattern`（归约/广播方向，详见 u2-l4）、`CastMode`（舍入方式）、`MemMngPolicy`（AUTO/MANUAL，选 DAG）、`MemLevel`（三级缓冲复用）。
- `ContextData` 是单核单 Tile 的**上下文包裹**，把入参张量、临时变量、缓冲池、`gmOffset`、`elementNum`、`pingPong` 六类信息一路下传给求值器，是连接 Block 调度与 Tile 求值的统一数据通道。
- 贯穿全讲的主线：ATVOSS 用「编译期 `Shape<>` + 运行期 `Tensor`」两套形状表示，用 `ContextData` 在切分与求值之间递送现场信息，用枚举开关在不同维度切换行为。

## 7. 下一步学习建议

- 想看 `MemMngPolicy::AUTO` 选出的 `FullAutoDag` 到底怎么把用户表达式变成带依赖的算子序列？请进入 [u3-l3 计算图构建：DAG 与 Bind](u3-l3-dag-construction.md)。
- 想搞清 `MemLevel` 三级如何控制缓冲复用、`pingPong` 如何驱动双缓冲流水？请进入 [u3-l5 Buffer 管理与双缓冲](u3-l5-buffer-double-buffering.md)。
- 想了解 `gmOffset`/`elementNum` 所在的 Tile 循环上层——单核任务怎么从 Kernel 层分下来？请进入 [u2-l9 Block 层：单核 Tile 切分与流水](u2-l9-block-layer.md)。
- 想完整看一个二维 TileShape + 归约/广播的端到端样例？请进入 [u3-l7 rms_norm 样例：表达式级联](u3-l7-rmsnorm-cascade.md)。
