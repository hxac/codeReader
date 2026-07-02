# 视图类型族：全局显存的分块访问

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `cuda_tile` 方言为什么需要「视图（View）」这一层抽象，它与上一讲的 `ptr<T>` 有什么分工。
- 识别视图家族的四个成员：`tensor_view`、`partition_view`、`strided_view`、`gather_scatter_view`，并能分别说出它们各自描述的访存模式。
- 读懂 `TileView` 类型接口的三个方法：`getViewIndexRank`、`getViewTileType`、`verifyIndices`，理解它们如何把「抽象坐标」转换成「具体的 tile」。
- 掌握 `tile_shape`、`dim_map`、`traversal_strides`、`sparse_dim`、`padding_value` 等参数的含义与取值。
- 亲手写出三类分块视图的合法 MLIR 类型声明。

## 2. 前置知识

本讲承接 [u3-l1（Tile 类型与元素类型）](u3-l1-tile-and-element-types.md) 与 [u3-l2（指针类型与 Token 类型）](u3-l2-pointer-and-token-types.md)。在继续之前，请确认你理解下面几个概念：

- **Tile**：落在寄存器里、编译期形状完全确定的小矩阵，例如 `tile<4x8xf32>`，是张量核计算与寄存器分配的基本单位。
- **Pointer**：有类型的全局设备显存指针，写作 `ptr<f32>`，携带「去哪里取数据」。当指针作为 Tile 的元素，得到 `tile<Nxptr<T>>` 的「指针 tile」。
- **MLIR 类型接口（TypeInterface）**：用 TableGen 给一类类型定义一组公共方法（接口），任何声明实现了该接口的类型都必须提供这些方法的实现。本讲的 `TileView` 就是一个类型接口。

**为什么需要视图？** 在 GPU 内核里，全局显存通常是一整块连续的大张量（比如一个 \(4096\times4096\) 的矩阵），而寄存器一次只能容纳一个小 tile（比如 \(16\times16\)）。所以每次访存都要回答两个问题：

1. 这块大张量在显存里的**布局**是什么（每一维的跨度 stride 是多少）？
2. 我要取的那一个 tile，在大张量里的**位置**如何用一个「坐标」来描述？

`ptr<T>` 只能回答「从哪个基地址开始」，它是一根线性的指针；而视图则回答了「形状 + 步长 + 分块方式」这整套几何关系。本讲的四类视图，正是为了把这块大张量**切割成一个个 tile**而设计的不同切割策略。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/cuda_tile/Dialect/CudaTile/IR/Types.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td) | 用 TableGen 定义全部四种视图类型及其参数、文本语法、校验声明。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td) | 定义 `TileView` 类型接口的三个方法。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Interfaces.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.h) | 接口的 C++ 头文件，引入代码生成的 `TypeInterfaces.h.inc`。 |
| [lib/Dialect/CudaTile/IR/Types.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp) | 视图类型的解析/打印/校验，以及 `TileView` 接口方法的具体实现。 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 定义 `padding_value` 枚举（zero / neg_zero / nan / pos_inf / neg_inf）。 |
| [test/Dialect/CudaTile/types.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir) | 视图类型的合法写法用例。 |
| [test/Dialect/CudaTile/view_invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir) | 视图类型的各种非法写法与预期报错。 |

---

## 4. 核心概念与源码讲解

### 4.1 TensorView：全局显存的形状与步长描述

#### 4.1.1 概念说明

`tensor_view` 是视图家族里最基础的一个，它本身**还不是一种分块策略**，而是对「一块连续全局显存」的几何描述：这块显存被看作一个多维张量，每一维有多大、相邻元素之间在内存里相隔多远（stride）。

它的三个参数是：

- **elementType**：元素类型（必须是 `CudaTile_NumberType`，即整数或浮点）。
- **shape**：每一维的大小，必须严格正（动态维度用 `?`）。
- **strides**：每一维的步长——「该维下标加 1 时，内存地址前进几个**元素**」（注意是元素个数，不是字节数）。步长必须严格正，可以动态（`?`）。

步长是 `tensor_view` 区别于普通指针的关键：同样的形状，行主序（row-major）与列主序（column-major）的步长不同；步长甚至可以让多个下标指向同一块内存。

#### 4.1.2 核心流程

读一个 `tensor_view` 时，元素 \((i, j)\) 在线性内存中的位置（以元素为单位）为：

\[
\text{offset}(i, j) = i \times \text{stride}_0 + j \times \text{stride}_1
\]

例如 `tensor_view<512x1024xf16, strides=[1024, 1]>` 是行主序（每一行 1024 个元素连续）；而 `strides=[1, 512]` 是列主序；`strides=[1, 1]` 则让 512×1024 个下标全部映射到同一个地址（重复枚举同一内存）。

shape 与 strides 还允许「逐维动态」，动态值用 `?` 打印，C++ 端用哨兵常量 `kDynamic` 表示。

#### 4.1.3 源码精读

`TensorViewType` 的定义在 [Types.td:201-295](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L201-L295)，其中描述部分给出了行主序、列主序、重复枚举等多种步长示例。其参数声明为：

[include/cuda_tile/Dialect/CudaTile/IR/Types.td:277-281](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L277-L281) — 这段声明了 `elementType`、`shape`、`strides` 三个参数，其中 shape 与 strides 是 `int64_t` 数组，由 `extraClassDeclaration` 里的 `kDynamic` 常量支持动态维度。

校验逻辑在 [Types.cpp:364-388](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L364-L388)。它强制三条规则：shape 与 strides 秩必须相同；每一维的 shape 必须严格正（或为 `kDynamic`）；每一维的 stride 也必须严格正（或为 `kDynamic`）。`view_invalid.mlir` 中的 `strides=[-5]`、`strides=[0]`、`strides=[4,1]`（与 shape 秩不符）就是分别触发这几条规则的例子。

#### 4.1.4 代码实践

**实践目标**：理解步长如何决定内存布局。

**操作步骤**：

1. 阅读 [test/Dialect/CudaTile/types.mlir:23-40](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir#L23-L40) 里 `test_tensor_view_types` 的合法用例。
2. 在纸上画一个 \(4\times4\) 的小矩阵，分别标注 `strides=[4,1]`（行主序）与 `strides=[1,4]`（列主序）时，元素 \((1,2)\) 的线性偏移。

**预期结果**：

- 行主序：\(\text{offset}(1,2) = 1\times4 + 2\times1 = 6\)。
- 列主序：\(\text{offset}(1,2) = 1\times1 + 2\times4 = 9\)。

**待本地验证**：若已构建 `cuda-tile-opt`，可运行 `cuda-tile-opt test/Dialect/CudaTile/types.mlir | cuda-tile-opt | FileCheck test/Dialect/CudaTile/types.mlir`，确认这些 `tensor_view` 类型能 round-trip（打出来与原文一致）。

#### 4.1.5 小练习与答案

**练习 1**：`tensor_view<8x8xf32, strides=[8,1]>` 与 `tensor_view<8x8xf32, strides=[16,1]>>` 有什么本质区别？

**答案**：前者的第二维步长为 1、第一维步长为 8，是紧凑的行主序，64 个元素恰好填满连续 64 个位置；后者第一维步长为 16，意味着每两行之间隔着 8 个元素的「空洞」，同一块逻辑张量在内存里并不连续（存在间隔）。两者形状相同，但内存布局不同。

---

### 4.2 TileView 接口：统一的「索引空间 → tile」抽象

#### 4.2.1 概念说明

`tensor_view` 只描述了「这块显存长什么样」，但还没有回答「我怎么从它里面切出一个 tile」。后面三种视图（partition / strided / gather_scatter）各自定义了一种**切割策略**，把 `tensor_view` 切成一个个等大的 tile。

为了让你能用同一套操作（`load_view_tko`、`store_view_tko`、`get_index_space_shape`）去访问任何一种分块视图，CUDA Tile 抽象出了一个**类型接口** `TileView`。它的定位在接口描述里写得很清楚：

> Represents a view within a memref from which tiles can be loaded/stored. It acts as a converter from a coordinate in an abstract tile space and tiles, communicating a loading/storing strategy.

也就是说，`TileView` 是一个「坐标 → tile」的转换器：你给它一组在「抽象 tile 空间」里的坐标，它告诉你这次 load/store 会落到哪个 tile 上、这个 tile 是什么类型。

#### 4.2.2 核心流程

任何实现了 `TileView` 接口的类型，都必须回答三个问题：

| 接口方法 | 回答的问题 |
|----------|------------|
| `getViewIndexRank()` | 这个视图的「索引空间」有几维？也就是 load 时要提供几个坐标。 |
| `getViewTileType()` | 不管用哪个坐标，load 出来的 tile 都是同一种类型——它是什么？ |
| `verifyIndices(indexTypes)` | 调用方传入的索引操作数的类型，对这个视图是否合法？ |

一个贯穿全接口的关键约束（见接口描述）：**Views must always access tiles of the same type no matter the index.** 即不管坐标取什么，load 出来的 tile 类型必须恒定。这也是 `getViewTileType` 不接受坐标参数的原因。

#### 4.2.3 源码精读

`TileView` 接口的 TableGen 定义在 [Interfaces.td:33-85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L33-L85)：

- [Interfaces.td:46-53](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L46-L53) 定义 `getViewIndexRank`，返回索引空间的维数。
- [Interfaces.td:54-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L54-L64) 定义 `getViewTileType`，返回 tile 的类型（注释里有一处 FIXME：理想情况下返回类型应被约束为 `cuda_tile::TileType`，但 ODS 循环依赖让它暂时只能返回 `mlir::Type`）。
- [Interfaces.td:65-83](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L65-L83) 定义 `verifyIndices`，让不同视图各自约束自己的索引结构——例如 `GatherScatterView` 要求某个维度是一维 tile，而 `PartitionView` 要求所有索引都是标量 tile。

接口的 C++ 入口头文件 [Interfaces.h:18](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.h#L18) 只有寥寥几行，核心是引入代码生成的 `TypeInterfaces.h.inc`（由 `cuda-tile-tblgen` 从 `Interfaces.td` 生成，参见 u2-l3）。

三种分块视图在 `Types.td` 中通过 `[DeclareTypeInterfaceMethods<CudaTile_TileView>]` 声明「我会自己实现这个接口的全部方法」，具体实现都在 `Types.cpp`。以 `PartitionViewType` 为例，三个方法的实现高度统一：

[lib/Dialect/CudaTile/IR/Types.cpp:623-632](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L623-L632) — `getViewIndexRank()` 直接返回 `tile_shape` 的维数；`getViewTileType()` 用 `tile_shape` 和 `tensor_view` 的元素类型，构造出一个 `TileType::get(shape, elementType)`。

也就是说：**索引空间的维数 = tile 的维数；load 出来的 tile 形状就是 `tile_shape`，元素类型继承自底层 `tensor_view`。** 这个结论对三种分块视图都成立（你可以对比 [StridedView 的同名方法 Types.cpp:770-779](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L770-L779) 与 [GatherScatterView 的 Types.cpp:956-965](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L956-L965)，实现完全一致）。

#### 4.2.4 代码实践

**实践目标**：通过 `get_index_space_shape` 操作间接观察 `getViewIndexRank` 的作用。

**操作步骤**：

1. 阅读 [test/Dialect/CudaTile/get_shape_invalid.mlir:51-68](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/get_shape_invalid.mlir#L51-L68)。
2. 注意其中一条用例对一个 `partition_view<tile=(4x4), ...>` 调用 `get_index_space_shape` 但只给了 1 个结果，触发了报错 `expected 2 results due to view index space rank, but got 1`。

**需要观察的现象**：报错里的「2」正是来自 [CudaTile.cpp:1889](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1889) 处 `srcType.getViewIndexRank()` 的返回值——`tile=(4x4)` 是二维，所以索引空间也是二维，必须返回 2 个标量 tile。

**预期结果**：理解了「视图索引空间的维数 = tile 维数」这条规则后，你应当能预测任意视图调用 `get_index_space_shape` 时需要几个返回值。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 `strided_view<tile=(2x2x2), ...>`，它的 `getViewIndexRank()` 返回多少？`getViewTileType()` 返回什么？

**答案**：`getViewIndexRank()` 返回 3（tile_shape 是三维）；`getViewTileType()` 返回 `tile<2x2x2xf32>`（假设底层 tensor_view 元素类型是 f32）。

**练习 2**：为什么 `getViewTileType` 不接受坐标参数？

**答案**：因为接口规定「不论坐标为何，访问到的 tile 类型必须相同」。坐标只决定**取哪一个** tile，不决定 tile 的类型——形状始终是 `tile_shape`，元素类型始终来自底层 `tensor_view`。

---

### 4.3 PartitionView 与 StridedView：网格对齐 vs 参数化步进

这两个视图都把 `tensor_view` 切成「网格状」排列的 tile，但切片的方式不同。它们共享同一套通用校验逻辑，所以我们先看共性，再看差异。

#### 4.3.1 概念说明

**PartitionView（分区视图）**：一种**完美对齐、无重叠**的分割。tile 像瓷砖一样严丝合缝地铺满整个 `tensor_view`，相邻 tile 紧挨着、不重叠、不留缝。这是最常见、最规整的分块方式（例如把一个大矩阵切成供 GEMM 使用的 tile 阵列）。

**StridedView（步进视图）**：在网格分割的基础上，引入了一个**可参数化的步进因子** `traversal_strides`。它允许：

- **带间隔（strided）**：tile 之间留空，跳过一些元素（适合稀疏/抽样的访问）。
- **重叠（overlapping）**：相邻 tile 部分重叠，形成滑动窗口（适合卷积、stencil 计算）。

当 `traversal_strides` 恰好等于 `tile_shape` 时，StridedView 的行为退化为与 PartitionView 完全相同。

两者都支持两个公共参数：

- **dim_map**：一个整数数组，指定「tile 的第 i 维」对应「tensor_view 的哪一维」。默认是恒等映射 \([0,1,2,\dots]\)。它可以表达转置等维度重排：一次带 `dim_map` 的 load，等价于一次默认 load 后接一次 `permute`。
- **padding_value**：越界填充值（见 4.5 节）。

#### 4.3.2 核心流程

**PartitionView 的索引空间**。由于是完美分割，索引空间每一维的大小就是整除的结果：

\[
\text{indexSpace}[d] = \frac{\text{tvShape}[\text{dimMap}[d]]}{\text{tileShape}[d]}
\]

例如 `partition_view<tile=(4x2), tensor_view<64x16xf32, strides=[16,1]>>`（默认 dim_map \([0,1]\)）：索引空间为 \(64/4 \times 16/2 = 16\times8\)。这与 `Types.td` 描述中给出的 `!pv_2d` 例子一致。

若 `dim_map=[1,0]`（转置），则 tile 第 0 维看向 tv 第 1 维、tile 第 1 维看向 tv 第 0 维：索引空间为 \(16/4 \times 64/2 = 4\times32\)，即描述中 `!pv_2d_transposed` 的结果。

**StridedView 的索引空间**。引入步进后，边缘的「部分 tile」也会被计入索引空间。对一维情形，tile \(k\) 的起点为 \(k \times \text{traversalStride}\)，只要起点还在范围内就计为一个有效 tile：

\[
\text{indexSpace} = \left\lceil \frac{\text{tvDim}}{\text{traversalStride}} \right\rceil
\]

`Types.td` 描述给了三个一维例子：`traversal_strides=[2]`（等于 tile，索引空间 8）、`[3]`（带间隔，索引空间 6）、`[1]`（重叠滑窗，索引空间 8）。多维情形按各维独立套用，dim_map 同样重排「tile 维 ↔ tv 维」的对应关系（多维转置的精确数值见 `Types.td` 描述里 `!sv_2d_transposed` 的表格）。

**越界 tile 的处理**（两者一致）：索引本身必须落在索引空间内（否则行为未定义），但 tile 本身可以部分超出 `tensor_view` 边界——load 时超出部分取 `padding_value`（若未设置则为未指定值），store 时超出部分被屏蔽（mask 掉不写）。

#### 4.3.3 源码精读

`PartitionViewType` 定义在 [Types.td:301-478](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L301-L478)，`StridedViewType` 定义在 [Types.td:484-708](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L484-L708)。注意两者的关键差别：

- PartitionView 的参数没有 `traversal_strides`（[Types.td:469-474](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L469-L474)），它的步进隐式等于 tile_shape。
- StridedView 多了一个 `traversal_strides` 参数（[Types.td:700-704](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L700-L704)），且是 13.3 才引入的（注意 PartitionView 是 13.1，StridedView 是 13.3）。

两者都把校验委托给同一个函数 `verifyPartitionViewLike`（注意函数名虽带 Partition，实则被 strided 复用）：

[lib/Dialect/CudaTile/IR/Types.cpp:529-613](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L529-L613) — 这段是所有「网格类视图」的公共校验。它逐条检查：tile_shape 非空；tile 秩与 tensor_view 秩相同；dim_map 必须恰好映射全部 tile 维；每个 tile 维为正且为 2 的幂；dim_map 的每个目标是合法的 tensor_view 维、且不能被映射两次（即必须是一一对应的排列）；最后复用 `TileType::verify` 确认 tile 本身合法。

`view_invalid.mlir` 里能看到这些规则的对应报错：`dim_map=[0]`（少映射一维）→ "expected dim_map to map exactly all 2 dimensions"；`dim_map=[2,1]`（越界）→ "target dimension is outside of tensor view dimensions"；`dim_map=[0,0]`（重复）→ "mapped at least twice"；`tile=(5x1024)` → "must have power of two length"。

StridedView 还额外校验 traversal_strides（[Types.cpp:746-768](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L746-L768)）：数量必须等于 tile 维数，且每个必须严格正（注意它**不要求是 2 的幂**，这与 tile_shape 必须是 2 的幂形成对照——见 `Types.td` 描述的 note：tile 维须为 2 的幂，而 traversal strides 可为任意正值）。`view_invalid.mlir` 中 `traversal_strides=[1,0]` 和 `[1,-1]` 触发的就是这条规则。

**关于 dim_map 的默认值**：当你在 MLIR 文本里省略 `dim_map` 时，解析器会自动填入恒等映射：

[lib/Dialect/CudaTile/IR/Types.cpp:435-456](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L435-L456) — `parseOptionalViewDimMap` 在读不到 `dim_map` 关键字时，用 `llvm::seq(tileRank)` 生成 \([0,1,2,\dots]\)。相应地，打印端 [Types.cpp:513-517](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L513-L517) 只在 dim_map 不是恒等映射时才打印它——这就是为什么 `types.mlir` 里 `dim_map=[0]`、`dim_map=[0,1]` 的行 round-trip 后会「消失」（它们等价于默认值）。

**verifyIndices**：partition 与 strided 都要求「所有索引都是同类型的标量 tile（rank 0）」：

[lib/Dialect/CudaTile/IR/Types.cpp:634-660](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L634-L660)（Partition，Strided 的 [781-807](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L781-L807) 完全相同）。`view_invalid.mlir` 中用 `tile<1xi32>` 当索引会报 "expected index type to be a scalar tile"，用 `i32` 与 `i64` 混用会报 "to be the same as other index types"。

#### 4.3.4 代码实践

**实践目标**：为 PartitionView 与 StridedView 各写一个合法的类型声明，并解释各自适合的访存场景。

**操作步骤**：

1. 参考 [test/Dialect/CudaTile/types.mlir:52-75](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir#L52-L75) 的 partition_view 合法写法。
2. 参考 [test/Dialect/CudaTile/view_invalid.mlir:345-417](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L345-L417) 中 strided_view 的写法（这些是用非法示例展示语法骨架的）。
3. 自己写两段类型声明（示例代码，非项目原有）：

```mlir
// 示例代码：PartitionView，把 64x16 的显存完美切成 4x2 的 tile
!pv = !cuda_tile.partition_view<
  tile=(4x2),
  tensor_view<64x16xf32, strides=[16, 1]>
>

// 示例代码：StridedView，带间隔地切 2 元素 tile，每步前进 3
!sv = !cuda_tile.strided_view<
  tile=(2),
  traversal_strides=[3],
  tensor_view<16xf32, strides=[1]>
>

// 示例代码：StridedView 退化形式，traversal_strides == tile_shape，等价于 partition
!sv_partition_like = !cuda_tile.strided_view<
  tile=(2),
  traversal_strides=[2],
  tensor_view<16xf32, strides=[1]>
>
```

**需要观察的现象**：第三段 `traversal_strides=[2]` 与 tile `(2)` 相等，其行为与一个 `partition_view<tile=(2), tensor_view<16xf32, strides=[1]>>` 完全一致（这正是 `Types.td` 描述里 `!sv_1d_tra2` 的注解）。

**预期结果**：

- **PartitionView 适合**：规整、无重叠的分块，如 GEMM 中把大矩阵切成 MMA tile 网格——每个元素恰好被一个 tile 覆盖，没有冗余访存。
- **StridedView 适合**：需要跳采或滑窗的场景，如卷积/im2col 的滑窗（traversal < tile，重叠）、或跨步采样（traversal > tile，带间隔）。

**待本地验证**：构建后用 `cuda-tile-opt` 对上述片段做 round-trip，确认能解析并原样打印（dim_map 默认值会被省略）。

#### 4.3.5 小练习与答案

**练习 1**：`partition_view<tile=(2x2), tensor_view<16x16xf32, strides=[16,1]>, dim_map=[1,0]>` 的索引空间形状是多少？

**答案**：tile 第 0 维（大小 2）看向 tv 第 1 维（16），\(16/2=8\)；tile 第 1 维（大小 2）看向 tv 第 0 维（16），\(16/2=8\)。所以索引空间为 \(8\times8\)。

**练习 2**：为什么 StridedView 的 traversal_strides 允许不是 2 的幂，而 tile_shape 必须是 2 的幂？

**答案**：tile_shape 决定的是装进寄存器的 tile 形状，它要参与张量核 MMA 与寄存器分配，硬件要求每维为 2 的幂（这是 u3-l1 讲过的 Tile 约束，复用了 `TileType::verify`）。而 traversal_strides 只是一个访存的「步进几何参数」，决定相邻 tile 在显存里隔多远，不进入寄存器，所以可以是任意正值。

---

### 4.4 GatherScatterView：稀疏采集维度

#### 4.4.1 概念说明

前两种视图的索引都是「标量坐标」——每个坐标指定一个 tile 在网格里的位置。但有些算法（比如注意力机制里按 query 选 key、或按索引表收集行）需要更灵活的访问：在某个维度上，**不按固定网格取 tile，而是按一组「任意的行号」去采集**。

`gather_scatter_view` 就是为此而生：它在 `tensor_view` 上铺网格，但允许你指定其中一个维度为「**稀疏维度（sparse_dim）**」。在稀疏维度上，索引不再是标量，而是一个**一维 tile**，里面列出要采集的所有行号；其余维度仍是标量坐标。load 时即为 gather（按行号收集），store 时即为 scatter（按行号散布）。

它的参数（无 dim_map，无 traversal_strides）：

- **tile_shape**：tile 的形状。
- **tensor_view**：底层视图。
- **sparse_dim**：一个 `uint32_t`，指明哪一维是稀疏采集维度。
- **padding_value**：越界填充值（见 4.5 节）。

#### 4.4.2 核心流程

设 `sparse_dim=0`，tile_shape 为 `(16x16)`，底层 `tensor_view<1024x16xi32>`：

- 调用 `load_view_tko %view[%gatherIdx, %j]`，其中 `%gatherIdx` 是一个 `tile<16xi32>`（列出 16 个行号），`%j` 是标量 tile。
- 结果是把底层张量第 `%gatherIdx[0..15]` 行、第 `%j` 列附近的元素，按行号收集成一个 `16x16` 的 tile——这就是 gather。

反之 `store_view_tko` 把一个 `16x16` 的 tile 按 `%gatherIdx` 散布回对应的行，即 scatter。

关键约束：**稀疏维度上，索引 tile 的大小必须等于 tile_shape 在该维的大小**（上例中 gatherIdx 必须是 16 个元素），否则采集出来的 tile 形状对不上。

注意：gather_scatter_view 是 13.3 引入的（[Types.td:718](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L718)），且目前**不支持原子归约**——`invalid.mlir` 中有专门用例验证 `atomic_red_view_tko` 遇到 gather_scatter_view 会报 "gather_scatter_view is not supported; use partition_view instead"。

#### 4.4.3 源码精读

`GatherScatterViewType` 定义在 [Types.td:714-768](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L714-L768)，参数声明在 [Types.td:759-764](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L759-L764)，注意 `sparse_dim` 是 `uint32_t` 而非数组。

`getViewIndexRank` 与 `getViewTileType` 和前两者完全一致（[Types.cpp:956-965](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L956-L965)）——索引空间维数仍是 tile 维数，tile 类型仍是 `tile_shape + 元素类型`。

真正不同的是 `verifyIndices`，它体现了 gather/scatter 的特殊索引结构：

[lib/Dialect/CudaTile/IR/Types.cpp:967-1024](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L967-L1024) — 这段校验做了四件事：

1. `sparse_dim` 必须落在索引数量范围内（[978-980](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L978-L980)）。
2. 稀疏维度位置的索引**必须是一维 tile**（[983-991](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L983-L991)）。
3. 这个一维索引 tile 的大小，**必须等于 tile_shape 在 sparse_dim 处的大小**（[993-1001](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L993-L1001)）。
4. 其余维度的索引必须是同类型的标量 tile（[1003-1021](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L1003-L1021)）。

这正是接口描述里 `verifyIndices` 设计意图的体现——「GatherScatterView requires specific index structure while PartitionView requires uniform index types」。

合法的类型声明可参考 [test/Dialect/CudaTile/invalid.mlir:2388](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L2388)：`!cuda_tile.gather_scatter_view<tile=(16x16), !cuda_tile.tensor_view<1024x16xi32, strides=[16, 1]>, sparse_dim=0>`。

#### 4.4.4 代码实践

**实践目标**：为 GatherScatterView 写一个合法的类型声明，并解释它适合的访存场景。

**操作步骤**：

1. 写一段类型声明（示例代码）：

```mlir
// 示例代码：稀疏维度为第 0 维，从 1024x16 的张量里按行号采集 16x16 的 tile
!gsv = !cuda_tile.gather_scatter_view<
  tile=(16x16),
  padding_value = zero,
  tensor_view<1024x16xi32, strides=[16, 1]>,
  sparse_dim=0
>
```

2. 对照 [Types.td 描述里的语法骨架 Types.td:756](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L756) 核对参数顺序。

**需要观察的现象**：注意它的语法顺序与 partition/strided 不同——`padding_value` 紧跟 `tile=(...)` 之后，`sparse_dim` 在最后；它没有 `dim_map`。

**预期结果**：解释场景——GatherScatterView 适合**按任意索引表收集行/列**的场景，如 sparse attention 中按 query 的索引收集对应的 key/value 行，或按查表结果做 embedding lookup。它把原本需要循环 + 逐元素 ptr load 的 gather 模式，提升为一次声明式的 tile load。

**待本地验证**：用 `cuda-tile-opt` 解析上述片段，确认无报错；注意 gather_scatter_view 是 13.3 才加入的，构建时若字节码版本低于 13.3 需要留意（见 u7-l4）。

#### 4.4.5 小练习与答案

**练习 1**：对上文的 `!gsv`（sparse_dim=0，tile=(16x16)），如果 load 时稀疏维度的索引传了一个 `tile<8xi32>`，会发生什么？

**答案**：会被 [verifyIndices 第 3 条](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L993-L1001) 拦下，报类似 "expected gather/scatter index size (8) to match tile shape at gather/scatter dimension 0 (16)" 的错误。稀疏索引 tile 的大小必须严格等于 tile_shape 在 sparse_dim 的大小。

---

### 4.5 padding_value：越界填充的取值与校验

#### 4.5.1 概念说明

前面三种分块视图都允许「tile 部分超出 tensor_view 边界」——因为索引落在索引空间内是合法的，但 tile 的实际元素可能伸到张量外面（尤其是 StridedView 的边缘部分 tile，或 tensor_view 维度不是 tile 大小整数倍时）。

load 时，超出部分取什么值？由 `padding_value` 决定。它是**可选**的：设置了，越界元素返回该填充值；不设置，越界元素是未指定值（unspecified，不要依赖）。store 时则始终屏蔽越界元素（不写）。

`padding_value` 是一个枚举，共五个取值，定义在 [AttrDefs.td:503-522](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L503-L522)：

| 取值 | 含义 | 整数值 |
|------|------|--------|
| `zero` | 0 | 0 |
| `neg_zero` | 负零 | 1 |
| `nan` | NaN | 2 |
| `pos_inf` | 正无穷 | 3 |
| `neg_inf` | 负无穷 | 4 |

一条重要约束（[AttrDefs.td:516-519](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L516-L519)）：**特殊填充值 `neg_zero`、`nan`、`pos_inf`、`neg_inf` 只能用于浮点元素类型**。整数张量只能用 `zero`（因为整数没有 NaN/无穷的概念）。

#### 4.5.2 核心流程

填充校验在公共函数 `verifyPartitionViewLike` 里完成：

[lib/Dialect/CudaTile/IR/Types.cpp:593-610](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L593-L610) — 若设置了 padding_value，且取值为 `neg_zero/nan/pos_inf/neg_inf` 之一，则检查底层 tensor_view 的元素类型必须是 `FloatType`，否则报 "can only be used with floating point element types"。

`types.mlir` 里能看到全部五个取值的合法用例（[types.mlir:55-65](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir#L55-L65)），它们都用 `tensor_view<16xf32, ...>`（浮点），所以 `nan`/`neg_zero`/`pos_inf`/`neg_inf` 都合法。

`padding_value` 对原子归约也有限制：`atomic_red_view_tko` 不支持带 padding_value 的视图（见 `invalid.mlir:2399-2404` 一带的 "views with padding_value are not supported for atomic reductions"）。

#### 4.5.3 源码精读

`padding_value` 的解析与打印是可选的。它在类型语法里的位置紧跟 `tile=(...)` 之后，例如：

```mlir
partition_view<tile=(2x2), padding_value = nan, tensor_view<16x16xf32, strides=[16,1]>>
```

[lib/Dialect/CudaTile/IR/Types.cpp:506-511](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L506-L511) — `printOptionalViewPaddingValue` 只在 padding_value 非空时打印，格式为 `padding_value = <name>, `（注意末尾有逗号和空格，因为后面还跟 tensor_view）。

#### 4.5.4 代码实践

**实践目标**：体会 padding_value 对浮点与整数的差异。

**操作步骤**：

1. 写两段类型声明（示例代码），分别用浮点和整数底层：

```mlir
// 示例代码：浮点底层，nan 填充合法
!pv_f = !cuda_tile.partition_view<
  tile=(1x4),
  padding_value = nan,
  tensor_view<8x2xf32, strides=[2,1]>
>

// 示例代码：整数底层，nan 填充非法（应为 zero 或不填）
!pv_i_bad = !cuda_tile.partition_view<
  tile=(1x4),
  padding_value = nan,
  tensor_view<8x2xi32, strides=[2,1]>
>
```

2. 用 `cuda-tile-opt -verify-diagnostics` 跑第二段，对照 [verifyPartitionViewLike 的 padding 校验 Types.cpp:593-610](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L593-L610)。

**需要观察的现象**：第一段（f32 + nan）合法；第二段（i32 + nan）报 "padding_value nan can only be used with floating point element types, got i32"。把第二段改成 `padding_value = zero` 或删掉 padding_value，则合法。

**预期结果**：理解整数张量越界只能填 `zero`，浮点张量才能用 NaN/无穷等特殊值做掩码（这在注意力掩码等场景很有用——把不该看的位置填成负无穷，softmax 后自然归零）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 attention 里常用 `padding_value = neg_inf`？

**答案**：attention 在 softmax 前会把「不该 attending」的位置屏蔽掉。把这些位置填成负无穷，经过 softmax（先 exp 再归一化）后它们的权重自然变成 0，等价于掩码，且无需额外引入 mask 张量参与运算。这要求底层是浮点类型，正好满足 padding_value 对浮点的约束。

**练习 2**：如果不设 padding_value，越界 load 会得到什么？

**答案**：未指定值（unspecified）。源码描述明确：若未设置 padding_value，越界元素 "yield unspecified values"，不可依赖。因此需要确定行为时应显式设置 padding_value。

---

## 5. 综合实践

**任务**：为三种分块视图各写一个合法的 MLIR 类型声明，并用一句话解释它适合的访存场景，最后把它们的底层都指向**同一个** `tensor_view` 类型别名。

**操作步骤**：

1. 先定义一个公共的 tensor_view 别名（示例代码）：

```mlir
// 示例代码：一个 64x64 的行主序 f32 全局张量
!tv = !cuda_tile.tensor_view<64x64xf32, strides=[64, 1]>
```

2. 分别写出三种视图（示例代码）：

```mlir
// (a) PartitionView：完美切成 4x4 tile，无重叠；索引空间 16x16
!pv = !cuda_tile.partition_view<tile=(4x4), tensor_view<64x64xf32, strides=[64, 1]>>

// (b) StridedView：4x4 tile，第 1 维步进 8（带间隔），padding 为零
!sv = !cuda_tile.strided_view<
  tile=(4x4),
  traversal_strides=[4, 8],
  padding_value = zero,
  tensor_view<64x64xf32, strides=[64, 1]>
>

// (c) GatherScatterView：sparse_dim=0，按行号采集 4x4 tile
!gsv = !cuda_tile.gather_scatter_view<
  tile=(4x4),
  tensor_view<64x64xf32, strides=[64, 1]>,
  sparse_dim=0
>
```

3. 标注每个视图的关键参数，并解释：
   - `!pv` 的 `tile_shape=(4x4)`、默认 `dim_map=[0,1]`，索引空间 \(64/4 \times 64/4 = 16\times16\)。适合 GEMM 分块。
   - `!sv` 的 `traversal_strides=[4,8]`，第 1 维每隔 8 取一个 4 宽的 tile（带间隔），适合跨步采样/稀疏卷积。
   - `!gsv` 的 `sparse_dim=0`，load 时第 0 维索引用一个 `tile<4xi32>` 给出 4 个行号，适合按索引表 gather 行。

4. **可选进阶**：把三者都改成用 `dim_map=[1,0]`（gather_scatter 不支持 dim_map，所以只在 pv/sv 上做），预测索引空间形状的变化，并说明 load 结果等价于「默认 load + permute [1,0]」。

**预期结果**：你能清楚地说出三种视图在「索引形态、是否重叠、是否稀疏」三个维度上的差异，并为每种挑出合适的真实算法场景。若已构建项目，用 `cuda-tile-opt` 对这些片段做 round-trip 验证语法合法性（注意 strided_view / gather_scatter_view 需要 13.3）。

**待本地验证**：本综合实践未实际运行命令；构建产物存在时，`cuda-tile-opt` 应能解析并打印上述全部类型。

---

## 6. 本讲小结

- 视图家族用一层抽象把「全局显存的几何（`tensor_view`）」与「如何从中切 tile（分块策略）」解耦：`ptr<T>` 是线性地址，视图是形状 + 步长 + 切割方式。
- `tensor_view` 是基础视图，描述形状与逐维步长（可为动态），是其余三种视图的底层。
- `TileView` 类型接口用 `getViewIndexRank` / `getViewTileType` / `verifyIndices` 三个方法，统一了「坐标 → tile」的转换；三类分块视图的 `getViewIndexRank`/`getViewTileType` 实现完全一致，差异集中在 `verifyIndices` 与各自的额外参数。
- `partition_view` 是完美对齐、无重叠的网格分割；`strided_view` 用 `traversal_strides` 表达带间隔或重叠的滑窗；`gather_scatter_view` 用 `sparse_dim` 表达按行号的稀疏采集，索引在该维是一维 tile。
- `dim_map` 是 tile 维到 tensor_view 维的排列（默认恒等），非恒等映射等价于「默认 load + permute」；省略时自动取 \([0,1,\dots]\) 且不打印。
- `padding_value`（zero / neg_zero / nan / pos_inf / neg_inf）决定越界 load 的填充值，其中 nan/无穷/负零仅限浮点；不设则为未指定值。

## 7. 下一步学习建议

- 本讲只讲了视图**类型**本身。视图真正被使用，要靠 [u5-l2（视图加载与存储：掩码与越界填充）](u5-l2-view-load-store.md) 中的 `load_view_tko` / `store_view_tko` 操作——那里会把这些类型接上真实的访存语义、mask 与 token 排序。
- 想理解视图在字节码里如何序列化，可结合 [u7-l4（字节码版本与兼容性）](u7-l4-bytecode-versioning.md)：本讲多次提到 `strided_view`/`gather_scatter_view` 是 13.3 才加入的，`sinceVersion` 正是驱动读写器版本兼容判断的关键。
- 想从规范层面再看一遍这些类型的官方描述，可用 u2-l3 学到的 `cuda-tile-tblgen --gen-op-spec` 直接生成 `Types.td` 对应的人类可读规范。
- 若你对「索引空间形状」的精确计算感兴趣，建议在学完 u5-l2 后，跟踪一次 `load_view_tko` 的 lowering，观察视图坐标如何被翻译成字节地址。
