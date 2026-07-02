# 视图加载与存储：掩码与越界填充

## 1. 本讲目标

上一讲（u5-l1）我们学会了用 `load_ptr_tko` / `store_ptr_tko` 读写显存：那是一种**逐元素地址**的范式——你先用 `iota` / `reshape` / `broadcast` / `offset` 算出一个「指针 tile」，里面每个元素就是一个具体地址，再带着可选的 `mask` 和 `padding` 去读写。这种方式自由、但也很「手算」：每一维的步长、对齐、边界都要你自己拼。

但 GPU 张量核程序里，访存模式常常是高度规整的：把一个大张量切成一块块 tile，按网格坐标 `(i, j)` 去取第 `i` 行第 `j` 列那一块。对这种**结构化**访存，CUDA Tile 提供了另一种更省心的范式——**视图（View）访存**：先把连续显存用一个 `tensor_view` 套上「形状 + 步长」的几何外衣，再用 `make_partition_view` 等切成 tile，最后用 `load_view_tko %view[%i, %j]` 一行就把整块 tile 拉进寄存器。本讲就围绕这套「视图访存」展开。

学完本讲，你应当能够：

1. 读懂并写出 `load_view_tko` / `store_view_tko`，理解它们如何用 **view + index** 在全局显存中定位一整块 tile，以及返回的 `tile` 与 `token` 各代表什么。
2. 区分**两种访存范式**：u5-l1 的指针 tile（带 `mask`/`padding` **操作数**）与本讲的视图（越界填充写在**视图类型**里、且**没有逐元素 mask 操作数**）。
3. 掌握 **TileView 接口**的三个方法 `getViewIndexRank` / `getViewTileType` / `verifyIndices`，理解它们如何让同一对 load/store 操作通用于 partition / strided / gather_scatter 三类视图。
4. 理解 `padding_value` 这一**视图类型属性**如何决定越界填充（load 填充、store 屏蔽），以及 `PaddingValue` 枚举（`zero` / `neg_zero` / `nan` / `pos_inf` / `neg_inf`）的取值与浮点限制。
5. 用 `get_tensor_shape` / `get_index_space_shape` 查询视图的两种「形状」，并能用 `cuda-tile-opt` 验证索引个数、类型、结果 tile 类型与视图匹配。

## 2. 前置知识

进入源码前，先用通俗语言把关键直觉建立起来。本讲假定你已学过 **u3-l3（视图类型族）** 与 **u5-l1（内存模型与 Token 顺序）**。

**视图是什么？** 这是 u3-l3 的内容：`tensor_view` 在一段连续显存（一个 `ptr<T>`）之上抽象出「逐维 shape + 逐维 stride」的几何关系；`partition_view` / `strided_view` / `gather_scatter_view` 再把 `tensor_view` 切成一块块固定大小的 tile。视图本身**不存数据**，它只描述「坐标 → 元素」的映射规则。

**「index space」是什么？** 每种视图都有一个**索引空间**：它是用一组坐标去选中「哪一块 tile」的坐标范围。对 `partition_view` 而言，索引空间就是「沿每维能切出多少块 tile」——比如把 `8192×128` 的 `tensor_view` 切成 `64×64` 的 tile，索引空间就是 `128×2`（行方向 128 块、列方向 2 块）。坐标 `(0, 1)` 就选中第 0 行块、第 1 列块那一块 `64×64` tile。

**为什么视图访存不用「指针 tile + mask」？** 这是本讲最关键的对照。指针 tile 范式里，你要为**每一个被访问的元素**算出一个地址，并用一个 `tile<i1>` 的 `mask` 逐元素决定「这个地址要不要真访问」。而视图范式里，「访问哪些元素」是由**视图的几何 + 坐标**整体决定的——你给一个坐标，视图自动算出整块 tile 的所有地址。因此视图访存**没有逐元素 mask 操作数**；而「越界怎么办」这件剩余的事，则交由**视图类型自身的 `padding_value` 属性**来回答。记住这个分工：**指针 tile 用操作数表达 mask/padding；视图用类型属性表达 padding、且不带 mask。**

**越界填充（padding）发生在哪里？** 即便坐标落在合法索引空间内，选中的那块 tile 也可能**部分超出**底层 `tensor_view` 的边界（比如张量大小不是 tile 大小的整数倍时，最右/最下那块就只有一部分是真数据）。对这种「部分越界」：
- **读（load）**：若视图设了 `padding_value`，越界元素取该填充值；若没设，越界元素值**未定义**。
- **写（store）**：越界元素**自动被屏蔽**（不写入），与是否设 `padding_value` 无关。

**Token 仍然管顺序。** `load_view_tko` / `store_view_tko` 名字里的 `_tko` 与 u5-l1 完全一致：它们默认**不受程序顺序约束**，编译器可重排；需要排序时用可选的输入/输出 `token` 串起来。内存序也复用同一套（`weak`/`relaxed`/`acquire` 用于 load，`weak`/`relaxed`/`release` 用于 store）。

> 直觉总结：**视图访存 = `make_tensor_view` 套几何 → `make_*_view` 切 tile → `load_view_tko %view[坐标]` 整块搬运；越界靠视图类型里的 `padding_value` 兜底，没有逐元素 mask。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | 用 TableGen 声明本讲全部操作：`make_tensor_view`、`make_partition_view`、`make_strided_view`、`make_gather_scatter_view`、`load_view_tko`、`store_view_tko`、`get_tensor_shape`、`get_index_space_shape` |
| `include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td` | 定义 **TileView 类型接口**：`getViewIndexRank` / `getViewTileType` / `verifyIndices` 三个方法，是 load/store 通用于各类视图的关键 |
| `include/cuda_tile/Dialect/CudaTile/IR/Types.td` | 定义 `PartitionView` / `StridedView` / `GatherScatterView` 类型，含 `padding_value` 参数与越界 load/store 语义 |
| `include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td` | 定义 `PaddingValue` 枚举（`zero`/`neg_zero`/`nan`/`pos_inf`/`neg_inf`）与 `PaddingValueAttr` |
| `test/Dialect/CudaTile/memory_consistency_ops.mlir` | `load_view_tko` / `store_view_tko` 的 round-trip 测试（含 `weak` / `relaxed device` / token 写法） |
| `test/Dialect/CudaTile/view_invalid.mlir` | load/store 视图操作的**非法用例**，展示 verifier 的各种报错（索引个数/类型/结果 tile 类型/内存序/标量索引） |

## 4. 核心概念与源码讲解

### 4.1 两种访存范式：指针 tile vs 视图

#### 4.1.1 概念说明

CUDA Tile 提供两条读显存的路径，理解它们的差异是本讲的总纲：

| 维度 | 指针 tile 范式（u5-l1） | 视图范式（本讲） |
|------|------------------------|-----------------|
| 操作 | `load_ptr_tko` / `store_ptr_tko` | `load_view_tko` / `store_view_tko` |
| 输入 | 指针 tile（每个元素=一个地址） | view（几何） + 坐标 index |
| 地址来源 | 你自己用 `offset` 等算出来 | 视图按坐标 + 步长自动算 |
| 逐元素屏蔽 | 有 `mask` 操作数（`tile<i1>`） | **无 mask 操作数** |
| 越界填充 | 有 `paddingValue` 操作数 | 视图类型里的 `padding_value` 属性 |
| 适合场景 | 不规则、scatter/gather、动态地址 | 规整分块、网格遍历、张量核喂料 |

核心结论：**视图范式把「地址计算」从 IR 语句下沉到了视图类型里**，从而让 load/store 只需关心「哪一块」。这也正是它没有 `mask` 操作数的原因——是否访问某元素不再逐个指定，而是由视图几何整体决定。

#### 4.1.2 核心流程

视图访存的典型三步流水线：

```
%ptr (tile<ptr<f32>>)
  │  make_tensor_view  ── 套上 [shape, strides] 几何
  ▼
%tensor_view (tensor_view<...>)
  │  make_partition_view / make_strided_view / make_gather_scatter_view  ── 切 tile + 设 padding_value
  ▼
%view (partition_view / strided_view / gather_scatter_view)
  │  load_view_tko %view[%i, %j]  ── 坐标选中一整块 tile
  ▼
%tile (tile<...>)  +  %token
```

注意每一层都是**纯计算/纯几何变换**（`make_*_view` 都带 `Pure` 或 `NoMemoryEffect`），真正触碰显存的只有最后的 `load_view_tko` / `store_view_tko`。

#### 4.1.3 源码精读

[Ops.td:2650-2653](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2650-L2653) 定义 `load_view_tko`，摘要一句话点题：「Load a tile from a tile view」——从一个视图里取一块 tile。描述里明确：「A view is mapping from view-space indices to a particular element in the view」——视图就是「坐标 → 元素」的映射。

[Ops.td:4474-4478](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4474-L4478) 定义 `store_view_tko`，与 load 对称：「Stores a tile into a tile view」。两者描述都强调越界访问「are handled according to the semantics of partition_view」——即越界行为由视图类型说了算，而不是操作自身。

#### 4.1.4 代码实践

**实践目标**：在脑海里把两种范式「对齐」，体会视图范式省掉了哪些手算。

**操作步骤**：回顾 u5-l1 的 print 内核地址构造链 `iota → reshape+broadcast → offset → load_ptr_tko`；再对比本讲 4.1.2 的三步流水线。

**需要观察的现象**：指针范式里，「每个元素的地址」是显式 IR 值；视图范式里，地址被视图类型隐藏，IR 只出现坐标。

**预期结果**：你能口述出「同样取一块 64×64 的 tile，指针范式要 4 条语句算地址，视图范式只要 1 条 `load_view_tko %view[%i,%j]`」。

#### 4.1.5 小练习与答案

**练习 1**：同样是「读一块 tile」，为什么 `load_view_tko` 没有 `mask` 操作数，而 `load_ptr_tko` 有？

**参考答案**：`load_ptr_tko` 逐元素指定地址，因此需要 `mask` 逐元素决定「这个地址要不要访问」；`load_view_tko` 的访问范围由视图几何 + 坐标整体决定（一取就是一整块），不存在「逐元素开关」，所以没有 `mask` 操作数。

**练习 2**：`make_tensor_view` / `make_partition_view` 这些「构造视图」的操作有没有内存副作用？

**参考答案**：没有。它们都带 `NoMemoryEffect` 或 `Pure`（见 4.3 节），只做几何变换、不碰显存；真正读写显存的是 `load_view_tko` / `store_view_tko`。

### 4.2 TileView 接口：坐标到 tile 的统一转换

#### 4.2.1 概念说明

`load_view_tko` / `store_view_tko` 只有一份定义，却能吃三种视图（partition / strided / gather_scatter）。这是怎么做到的？答案是一个**类型接口（TypeInterface）**——`TileView`。它把「视图该回答的三个问题」抽象成三个方法，load/store 只管调用这三个方法，不关心具体是哪类视图：

1. **`getViewIndexRank()`**：这个视图的坐标是几维的？（决定 load/store 要传几个 index）
2. **`getViewTileType()`**：从该视图取出来的 tile 是什么类型？（决定 load 的结果类型）
3. **`verifyIndices(indexTypes)`**：给定的 index 类型合不合法？（partition/strided 要求标量；gather_scatter 要求某一维是一维 tile）

#### 4.2.2 核心流程

load_view_tko 的校验等价于：

```text
1. 操作数 view 必须实现 TileView 接口           （否则 "operand #0 must be TileView instance"）
2. index 个数 == view.getViewIndexRank()         （否则 "expected N index operands (based on view type)"）
3. 结果 tile 类型 == view.getViewTileType()      （否则 "expected tile type to be '...' (based on view type)"）
4. view.verifyIndices(indexTypes) 通过           （partition/strided：必须是标量 tile；gather_scatter：sparse_dim 维是一维 tile）
5. 所有 index 类型一致                           （否则 "expected index type 1 to be the same as other index types"）
6. 内存序合法（load: weak/relaxed/acquire）      （否则 "expect one of: weak, relaxed, or acquire"）
```

正是这套「问视图三个问题」的设计，让 load/store 与具体视图类型**解耦**——新增一种视图只需实现 `TileView` 接口即可被同一对 load/store 复用。

#### 4.2.3 源码精读

[Interfaces.td:33-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L33-L43) 定义 `TileView` 接口，描述写道：「Represents a view within a memref from which tiles can be loaded/stored. It acts as a converter from a coordinate in an abstract tile space and tiles」，并强调「Views must always access tiles of the same type no matter the index」——同一视图无论取哪个坐标，tile 类型都一样。

[Interfaces.td:45-83](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Interfaces.td#L45-L83) 声明三个接口方法：`getViewIndexRank`（返回 `size_t`）、`getViewTileType`（返回 `::mlir::Type`）、`verifyIndices`（接收诊断回调与 index 类型区间，返回 `LogicalResult`）。`verifyIndices` 的注释点明了各视图的差异：「GatherScatterView requires specific index structure while PartitionView requires uniform index types」。

[Ops.td:2743-2744](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2743-L2744) `load_view_tko` 的 `view` 操作数类型约束为 `CudaTile_TileView`（即接口本身），`index` 为 `Variadic<CudaTile_IntTileType>`——这就是「任何 TileView 实例都能喂进来」的落地。

#### 4.2.4 代码实践

**实践目标**：从非法用例反推 verifier 如何调用 `TileView` 接口。

**操作步骤**：阅读 [view_invalid.mlir:421-450](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L421-L450) 的三个 load_view_tko 非法用例。

**需要观察的现象**：分别对应「结果 tile 类型与视图不符」「index 个数与视图索引秩不符」「view 操作数根本不是 TileView」。

**预期结果**：你能把三条报错分别对应到 4.2.2 流程的第 3、2、1 步。**待本地验证**：用 `cuda-tile-opt %s -verify-diagnostics` 实跑确认报错文本。

#### 4.2.5 小练习与答案

**练习 1**：如果给 `partition_view`（秩为 2）的 `load_view_tko` 只传 1 个 index，verifier 会调用 `TileView` 的哪个方法、报什么错？

**参考答案**：会比对 `index.size()` 与 `view.getViewIndexRank()`，报「expected 2 index operands (based on view type), got 1」（见 [view_invalid.mlir:436](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L436)）。

**练习 2**：`getViewTileType` 为什么重要？

**参考答案**：它决定了 load 必须返回什么类型的 tile。verifier 据此检查结果类型，比如对 `partition_view<tile=(1024x1024), tensor_view<...f32>>`，结果必须是 `tile<1024x1024xf32>`，写成 `tile<8xf32>` 就会报「expected tile type to be '...' (based on view type)」（见 [view_invalid.mlir:425](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L425)）。

### 4.3 构造视图：make_tensor_view 与三类 make_*_view

#### 4.3.1 概念说明

视图不能凭空出现，要一步步构造：

- **`make_tensor_view`**：把一个标量指针 tile（`tile<ptr<T>>`）+ shape + strides，包装成 `tensor_view`。shape/strides 可以是静态字面量，也可以是动态的 tile 值（对应类型里的 `?`）。
- **`make_partition_view`**（13.1）：在 `tensor_view` 上做**无重叠网格切割**，每块 tile 大小固定。
- **`make_strided_view`**（13.3）：带 `traversal_strides` 的切割，允许**重叠或带间隔**的滑窗。
- **`make_gather_scatter_view`**（13.3）：指定一个 `sparse_dim`，在该维上按**行号稀疏采集**。

三者都在**返回类型的注解里**写明 tile 大小（以及 padding/dim_map 等），构造操作本身只是把 `tensor_view` 「转交」给更具体的视图类型。

#### 4.3.2 核心流程

```
make_tensor_view %base, shape=[S...], strides=[T...]
        : tensor_view<S...xT, strides=[T...]>
            │
            ├── make_partition_view %tv : partition_view<tile=(...), tensor_view<...>, dim_map=?, padding_value=?>
            ├── make_strided_view    %tv : strided_view<tile=(...), traversal_strides=[...], tensor_view<...>, dim_map=?, padding_value=?>
            └── make_gather_scatter_view %tv : gather_scatter_view<tile=(...), tensor_view<...>, sparse_dim=?, padding_value=?>
```

注意 `padding_value` 是写在这些视图**类型**里的（可选），而不是构造操作的额外操作数。

#### 4.3.3 源码精读

[Ops.td:3064-3078](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3064-L3078) 定义 `make_tensor_view`，带 `NoMemoryEffect`；描述说明 shape/strides 既可静态反映在类型里，也可动态（类型中显为 `?`），且「dynamicShape and dynamicStrides are interpreted as unsigned integers」。

[Ops.td:3106-3110](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3106-L3110) `make_tensor_view` 的操作数：`base`（标量指针 tile）+ `dynamicShape`（变长整数 tile）+ `dynamicStrides`（变长整数 tile），结果是单个 `TensorViewType`。

[Ops.td:4785-4803](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4785-L4803) 定义 `make_partition_view`（13.1，`Pure`），明确「The resulting partition view can be loaded from using load_view_tko and stored to using store_view_tko」，并指出「view memory options act on the computed index space of the partition view」。

[Ops.td:4847-4866](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4847-L4866) 定义 `make_strided_view`（13.3，`Pure`），多出 `traversal_strides` 参数描述「带间隔或重叠滑窗」。

[Ops.td:4914-4933](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4914-L4933) 定义 `make_gather_scatter_view`（13.3，`Pure`），多出 `sparse_dim`：「the dimension along which to gather/scatter」。

#### 4.3.4 代码实践

**实践目标**：写出三种视图的合法构造语句，体会「类型注解承载几何」。

**操作步骤**：参考 [Ops.td:4805-4834](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4805-L4834)（partition）、[Ops.td:4868-4901](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4868-L4901)（strided）、[Ops.td:4935-4953](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4935-L4953)（gather_scatter）的官方示例，各抄写一条到一个 `.mlir` 文件。

**需要观察的现象**：tile 大小、traversal_strides、sparse_dim 都出现在返回**类型**里，而不是操作数里。

**预期结果**：`cuda-tile-opt` 能正常 round-trip 这三条语句。**待本地验证**：实跑 `cuda-tile-opt %s` 确认无报错。

#### 4.3.5 小练习与答案

**练习 1**：`make_strided_view` 比 `make_partition_view` 多哪个关键参数？它解决什么问题？

**参考答案**：多 `traversal_strides`。它描述遍历父 `tensor_view` 时的步进因子，允许 tile 之间**重叠或带间隔**（滑窗），而 partition view 是步长恰好等于 tile 大小、严格无重叠的特例。

**练习 2**：为什么 `make_tensor_view` 的 shape/strides 可以是动态的？

**参考答案**：为了支持运行时才知道形状/步长的全局张量。动态值以 tile 操作数传入，类型里相应维度记为 `?`（见 [Ops.td:3097-3101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3097-L3101) 的 `?x?xf32, strides=[?,?]` 示例）。

### 4.4 load_view_tko / store_view_tko：用 view[index] 读写 tile

#### 4.4.1 概念说明

有了视图，读写就是「给坐标，取/存整块」：

- **`load_view_tko`**：`%tile, %tok = load_view_tko <ordering> %view[%i, %j, ...] [token=%t] : <视图类型>, <index类型> -> <tile类型>, token`。返回**两个**结果：数据 tile 与一个 token。
- **`store_view_tko`**：`%tok = store_view_tko <ordering> %tile, %view[%i, %j, ...] [token=%t] : <tile类型>, <视图类型>, <index类型> -> token`。先写要存的 tile，再写视图与坐标，只返回一个 token。

两者都带 `AttrSizedOperandSegments`（因为 index 是变长的、token 是可选的），都用 `hasCustomAssemblyFormat` 自定义汇编格式。内存序与 u5-l1 完全同源：load 允许 `weak`/`relaxed`/`acquire`，store 允许 `weak`/`relaxed`/`release`；`weak` 与 `memory_scope` 互斥，其余须显式给 scope（如 `relaxed device`）。

**坐标语义**：index 解释为**无符号整数**；个数必须等于视图的索引秩；对 partition/strided，每个 index 是**标量 tile**（0 秩，如 `tile<i32>`）；对 gather_scatter，`sparse_dim` 那一维的 index 是**一维 tile**（如 `tile<8xi32>`），其余仍是标量。

#### 4.4.2 核心流程

以 partition_view 为例的读 + 写：

```
%c0 = constant <i32: 0> : tile<i32>
%tile, %tok = load_view_tko weak %view[%c0, %c0]
    : partition_view<tile=(64x64), tensor_view<8192x128xf32, strides=[128,1]>>, tile<i32>
    -> tile<64x64xf32>, token
%tok2 = store_view_tko weak %tile, %view[%c0, %c0]
    : tile<64x64xf32>, partition_view<...>, tile<i32> -> token
```

gather_scatter 的坐标更特殊（sparse_dim 维是一维 tile）：

```
%gather_indices = constant <i32: [0,10,20,30,40,50,60,70]> : tile<8xi32>   // sparse_dim=0 这一维
%col_idx        = constant <i32: 0> : tile<i32>                            // 另一维仍为标量
%g, %t = load_view_tko weak %gsview[%gather_indices, %col_idx]
    : gather_scatter_view<tile=(64x64), ..., sparse_dim=0>, tile<8xi32>, tile<i32>
    -> tile<64x64xf32>, token
```

#### 4.4.3 源码精读

[Ops.td:2736-2748](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2736-L2748) `load_view_tko` 的参数与结果：`memory_ordering_semantics`（限定 `OnlyVariants<["WEAK","RELAXED","ACQUIRE"]>`）、可选 `memory_scope`、`view`（TileView）、`index`（变长 IntTile）、可选 `token`、可选 `optimization_hints`；结果为 `tile` + `result_token`。注意：**没有 `mask`、没有 `paddingValue` 操作数**。

[Ops.td:2686-2706](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2686-L2706) `load_view_tko` 的官方示例：展示 `weak %view[%c0,%c0]`、`weak %view[%c0,%c1]`、带 `token = %token`、以及动态坐标 `%index` 四种写法。

[Ops.td:2718-2731](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2718-L2731) gather 示例：清楚标注 sparse_dim=0 维的 index 必须是一维 `tile<8xi32>`、另一维是标量 `tile<i32>`。

[Ops.td:4571-4584](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4571-L4584) `store_view_tko` 的参数：ordering（限定 `["WEAK","RELAXED","RELEASE"]>`）、可选 scope、`tile`、`view`、`index`、可选 `token`、可选 hints；结果只有 `result_token`。`index` 描述明确：「For GatherScatterView, the first index can be a 1D tensor」。

[memory_consistency_ops.mlir:113-126](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L113-L126) `tiled_view_load` 用例：在 8192×8192×64 的 `partition_view` 上用三个标量坐标 `[%0, %0, %0]` 读取，对比 `weak`（无 scope）与 `relaxed device`（带 scope）两种写法的 CHECK 行。

[memory_consistency_ops.mlir:130-141](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/memory_consistency_ops.mlir#L130-L141) `tiled_view_store` 用例：把 `tile<8xf32>` 存进一维 `partition_view`，单标量坐标 `[%0]`。

#### 4.4.4 代码实践

**实践目标**：手写一段合法的 partition_view 读 + store，并用非法用例对照学习。

**操作步骤**：

1. 新建 `view_rw.mlir`，写入一个 `cuda_tile.module` + `entry`，内部按 4.4.2 的模板写 `make_tensor_view → make_partition_view → load_view_tko → store_view_tko`。
2. 运行 `cuda-tile-opt view_rw.mlir`（round-trip）。
3. 故意把 index 写成 `tile<1xi32>`（秩 1 而非标量），观察报错。

**需要观察的现象**：合法版无输出变化（round-trip 通过）；非法版应在 store 那行报「expected index type to be a scalar tile」。

**预期结果**：对照 [view_invalid.mlir:562-568](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L562-L568) 与 [view_invalid.mlir:583-589](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L583-L589) 确认报错文本一致（标量索引要求 + store 内存序要求）。**待本地验证**：需本地构建出 `cuda-tile-opt` 才能实跑。

#### 4.4.5 小练习与答案

**练习 1**：`store_view_tko` 用了 `acquire` 内存序，verifier 为什么拒绝？

**参考答案**：`acquire` 是「读」语义，store 只允许 `weak`/`relaxed`/`release`。报错见 [view_invalid.mlir:586](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/view_invalid.mlir#L586)：「expect one of: weak, relaxed, or release, but got: acquire」。这与 C++ 内存模型一致（见 u5-l1）。

**练习 2**：`gather_scatter_view`（sparse_dim=0）的 load，第一个 index 为什么是 `tile<8xi32>` 而不是标量？

**参考答案**：sparse_dim 维是「按行号稀疏采集」的维度，需要一次性给出要采集的多个行号，所以该维 index 必须是一维 tile；其余维仍按通常规则用标量坐标（见 [Ops.td:2718-2731](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2718-L2731)）。

### 4.5 越界填充 padding_value：视图类型属性与 PaddingValueAttr

#### 4.5.1 概念说明

本节是本讲最重要的「纠偏」点。先把结论摆清楚：

- **视图访存没有 `mask` 操作数**，也没有 `paddingValue` **操作数**。这两者是 u5-l1 **指针**访存（`load_ptr_tko`）的特征。
- 视图访存的「越界填充」由**视图类型自身的 `padding_value` 属性**决定，在 `make_*_view` 的返回类型里设置。
- 即便坐标合法，选中的 tile 也可能**部分超出**底层 `tensor_view`（张量尺寸非 tile 整数倍时）。此时：
  - **load**：设了 `padding_value` → 越界元素取该值；没设 → 越界元素**未定义**。
  - **store**：越界元素**始终被屏蔽**（不写入），与 `padding_value` 无关。

`PaddingValue` 枚举有 5 个取值：`zero`、`neg_zero`、`nan`、`pos_inf`、`neg_inf`。其中后 4 个是「特殊值」，**只能用于浮点元素类型**。

#### 4.5.2 核心流程

padding_value 的生命周期：

```
make_partition_view %tv
    : partition_view<tile=(1x4), padding_value=nan, tensor_view<8x2xf32, strides=[2,1]>>
                                                    ↑
                                  在视图「类型」里设定，伴随视图存在
        │
        ▼
load_view_tko weak %pv[%i, %j] : ... -> tile<1x4xf32>, token
        │  取出的 tile 中，超出 8×2 tensor_view 的列 → 填 NaN
        ▼
store_view_tko weak %tile, %pv[%i, %j] : ... -> token
           存入时，超出的列 → 自动不写
```

> 类比：`padding_value` 像是给视图配的一副「越界眼镜」——它不是某次 load/store 的临时参数，而是这副眼镜本身的属性；戴上它看（load），越界处就是设定值；用它写（store），越界处一律忽略。

#### 4.5.3 源码精读

[AttrDefs.td:503-519](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L503-L519) 定义 `PaddingValue` 枚举：`zero(0)`、`neg_zero(1)`、`nan(2)`、`pos_inf(3)`、`neg_inf(4)`，并明确「special padding values (neg_zero, nan, pos_inf, neg_inf) can only be used with floating-point element types」。

[Types.td:440-443](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L440-L443) PartitionView 的越界语义：「Load operations: If padding_value is set, out-of-bounds tile elements yield the padding value. If not set, out-of-bounds elements yield unspecified values.」「Store operations: Out-of-bounds tile elements are masked during stores.」

[Types.td:461-466](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L461-L466) 给出 padding_value=nan 的直观图示：一个 `8×2` 的 tensor_view 配 `tile=(1×4)`，右半两列全是填充的 NaN。

[Types.td:469-474](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L469-L474) PartitionView 的类型参数：`tile_shape`、`tensor_view`、`dim_map`、`padding_value`（`OptionalParameter`，即可选）。

[Types.td:670-673](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L670-L673) StridedView 的越界语义与 PartitionView **完全一致**（load 填充/未定义、store 屏蔽）。

[Types.td:745-757](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L745-L757) GatherScatterView 同样有可选 `padding_value`，取值列表与上述 5 个一致。

作为对照，回顾 u5-l1 的指针访存：[Ops.td:2842-2843](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2842-L2843) `load_ptr_tko` 才有 `mask`（`Optional<TileOf<Int1>>`）与 `paddingValue`（`Optional<NumberTileType>`）**操作数**——这正是两种范式最直观的差异。

#### 4.5.4 代码实践

**实践目标**：亲手设一个带 `padding_value=zero` 的 partition_view 并读写，体会「填充在类型里、屏蔽在 store 里」。

> 注意：本实践按规格要求包含「mask」一词，但**视图访存本身没有 mask 操作数**——这是 u5-l1 指针访存的特性。因此这里的「屏蔽」语义由 store 对越界元素的自动屏蔽 + padding_value 共同体现。若你确实需要逐元素屏蔽，应改用 `load_ptr_tko`/`store_ptr_tko`（见 u5-l1）。

**操作步骤**：

1. 新建文件 `view_padding.mlir`，写入：

   ```mlir
   cuda_tile.module @module {
     entry @example(%ptr: tile<ptr<f32>>) {
       // 一个 8x2 的 tensor_view，但 tile 宽度取 4，于是右半两列必然越界
       %tv = make_tensor_view %ptr, shape=[8, 2], strides=[2, 1]
             : tensor_view<8x2xf32, strides=[2,1]>
       // padding_value=zero：越界的 load 元素填 0
       %pv = make_partition_view %tv :
             partition_view<tile=(1x4), padding_value=zero, tensor_view<8x2xf32, strides=[2,1]>>
       %c0 = constant <i32: 0> : tile<i32>
       // 读：结果 tile<1x4xf32> 中第 2、3 列（越界）应为 0
       %tile, %tok = load_view_tko weak %pv[%c0, %c0]
             : partition_view<tile=(1x4), padding_value=zero, tensor_view<8x2xf32, strides=[2,1]>>, tile<i32>
             -> tile<1x4xf32>, token
       // 写：越界的第 2、3 列自动屏蔽，不会写出 tensor_view 边界
       %tok2 = store_view_tko weak %tile, %pv[%c0, %c0]
             : tile<1x4xf32>, partition_view<tile=(1x4), padding_value=zero, tensor_view<8x2xf32, strides=[2,1]>>, tile<i32> -> token
       return
     }
   }
   ```

2. 用 `get_tensor_shape` 查询视图形状（见 4.6）：在 load 前加 `%d0, %d1 = get_tensor_shape %tv : tensor_view<8x2xf32, strides=[2,1]> -> tile<i64>`，验证得到 `8` 与 `2`。
3. 运行 `cuda-tile-opt view_padding.mlir` 验证 round-trip。

**需要观察的现象**：合法 round-trip；若把 `padding_value=zero` 改成 `padding_value=nan` 仍合法（f32）；若把元素类型改成 `i32` 并用 `padding_value=nan`，应被拒绝（特殊值仅限浮点）。

**预期结果**：round-trip 通过；整数 + nan 的组合报错。**待本地验证**：实际报错文本需本地构建后用 `cuda-tile-opt -verify-diagnostics` 确认。

#### 4.5.5 小练习与答案

**练习 1**：为什么说「`padding_value` 是视图属性而非 load/store 操作数」？请用源码位置佐证。

**参考答案**：`padding_value` 出现在 `PartitionView`/`StridedView`/`GatherScatterView` 的**类型参数**里（[Types.td:473](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L473)、[Types.td:704](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L704)、[Types.td:763](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L763)），而 `load_view_tko` 的操作数里根本没有 padding 字段（[Ops.td:2736-2748](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2736-L2748)）。

**练习 2**：load 时没设 `padding_value`，越界元素的值是什么？store 时呢？

**参考答案**：load 没设则越界元素**未定义**（unspecified）；store 时越界元素**总是被屏蔽**（masked during stores），与是否设 `padding_value` 无关（[Types.td:440-443](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L440-L443)）。

**练习 3**：能否给一个 `tile<i32>` 元素类型的视图设 `padding_value=nan`？

**参考答案**：不能。`nan`/`pos_inf`/`neg_inf`/`neg_zero` 这 4 个特殊值仅限浮点元素类型（[AttrDefs.td:516-519](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L516-L519)）；整数类型只能用 `zero`。

### 4.6 查询视图形状：get_tensor_shape 与 get_index_space_shape

#### 4.6.1 概念说明

视图有两种「形状」，对应两个查询操作（都带 `NoMemoryEffect`，是纯查询）：

- **`get_tensor_shape`**：返回底层 `tensor_view` 的**张量形状**（每维多少个元素）。输入必须是 `tensor_view`。
- **`get_index_space_shape`**：返回视图的**索引空间形状**（沿每维能取多少块 tile）。输入是任意 `TileView`（partition/strided/gather_scatter）。

两者都返回**多个标量整数 tile**（个数=维数），值按无符号解释；并警告：若某维尺寸装不进结果 tile 的元素类型，行为未定义。

#### 4.6.2 核心流程

```
%tv = make_tensor_view %base, shape=[8,2], strides=[2,1] : tensor_view<8x2xf32, strides=[2,1]>
%d0, %d1 = get_tensor_shape %tv : tensor_view<8x2xf32, strides=[2,1]> -> tile<i64>   // d0=8, d1=2

%pv = make_partition_view %tv : partition_view<tile=(1x4), tensor_view<8x2xf32, strides=[2,1]>>
%i0, %i1 = get_index_space_shape %pv : partition_view<...> -> tile<i64>              // i0=8, i1=0(整除后)
```

> 直觉：`get_tensor_shape` 问「底层数据多大」，`get_index_space_shape` 问「能切出几块」。对 partition_view，索引空间每维 ≈ `tensor_shape // tile_shape`（向下取整，余数部分要靠 padding 处理）。

#### 4.6.3 源码精读

[Ops.td:1314-1327](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1314-L1327) 定义 `get_tensor_shape`：「returns the shape of the tensor backing the provided tensor view」，并警告维度装不下结果元素类型时未定义。

[Ops.td:1346-1355](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1346-L1355) 其示例：`%dim0, %dim1 = get_tensor_shape %tensor_view : tensor_view<32x32xf32, strides=[32,1]> -> tile<i64>`。

[Ops.td:1264-1280](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1264-L1280) 定义 `get_index_space_shape`：「returns the shape of the index space of src」，结果秩等于视图索引秩，值按无符号解释，同样有装不下的未定义警告。

[Ops.td:1295-1307](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1295-L1307) 其示例：对 2×2×4 的 partition_view，`%dim0, %dim1, %dim2 = get_index_space_shape %partition_view : ... -> tile<i64>`。

#### 4.6.4 代码实践

**实践目标**：用两个查询操作算出「能切几块」，与手算对照。

**操作步骤**：在 4.5.4 的 `view_padding.mlir` 里，对 `tensor_view<8x2>` 调 `get_tensor_shape`（期望 8、2），对 `partition_view<tile=(1x4)>` 调 `get_index_space_shape`（期望行向 8 块、列向 0 块——因为 2<4，列向整除为 0，正说明该维必然越界、需 padding）。

**需要观察的现象**：列向索引空间为 0，直观印证了「tile 宽 4 > tensor 宽 2，必然部分越界」。

**预期结果**：与 4.5 设的 `padding_value=zero` 呼应——越界的列正是靠它填充。**待本地验证**：实跑需本地 `cuda-tile-opt`。

#### 4.6.5 小练习与答案

**练习 1**：`get_tensor_shape` 与 `get_index_space_shape` 的输入类型有何不同？

**参考答案**：`get_tensor_shape` 只接 `TensorViewType`（问底层数据形状）；`get_index_space_shape` 接任意 `TileView`（partition/strided/gather_scatter，问索引空间形状）。

**练习 2**：这两个操作有内存副作用吗？结果值如何解释？

**参考答案**：没有，都带 `NoMemoryEffect`，是纯查询；结果按**无符号整数**解释，且若某维尺寸超出结果 tile 元素类型能表示的范围，行为未定义（[Ops.td:1276-1279](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1276-L1279)、[Ops.td:1323-1326](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1323-L1326)）。

## 5. 综合实践

把本讲所有内容串起来，实现一个「分块求和」的雏形：把一个 `tensor_view` 按 `partition_view` 切块，逐块 load、累加、写回。

**任务**：

1. 定义 `tensor_view<8x4xf32, strides=[4,1]>`，并切成 `partition_view<tile=(2x4), padding_value=zero, ...>`（行向 4 块、列向 1 块）。
2. 用 `get_index_space_shape` 算出索引空间，确认行向有 4 块。
3. 写出 4 次 `load_view_tko weak %pv[%r, %c0]`（`%r` 取 0..3，`%c0` 恒为 0），每次取 `tile<2x4xf32>`。
4. 用 `addf`（u4-l3）把 4 块累加成一块 `tile<2x4xf32>`。
5. 用 `store_view_tko weak %sum, %pv[%c0, %c0]` 把累加结果写回第 0 块；用 `make_token`/`join_tokens`（u5-l1）保证 4 次 load 全部完成后才做累加、累加完成后才做 store。
6. 用 `cuda-tile-opt` 验证整段 IR 合法。

**验收要点**：

- 每条 `load_view_tko` 的结果类型必须严格是 `tile<2x4xf32>`（由 `getViewTileType` 决定）。
- token 依赖链必须把 store 排到最后。
- 所有 index 必须是标量 `tile<i32>`，类型一致。
- `padding_value=zero` 在本例中其实用不到（4 块都完整），但设上无害；可尝试把 `tensor_view` 改成 `8x3`、tile 改成 `(2x4)` 来触发真正的越界填充，观察是否仍能通过验证（store 越界屏蔽、load 填零）。

**待本地验证**：本任务需本地构建 `cuda-tile-opt` 才能实跑；无法构建时，可作为「源码阅读型实践」——只写 IR 并人工核对每条语句的类型与 token 链是否符合本讲规则。

## 6. 本讲小结

- **两种访存范式**：指针 tile（`load_ptr_tko`，带 `mask`/`paddingValue` **操作数**）vs 视图（`load_view_tko`，无 mask 操作数、padding 写在视图类型里）。
- **视图访存三步**：`make_tensor_view` 套几何 → `make_partition/strided/gather_scatter_view` 切 tile → `load_view_tko %view[坐标]` 整块读写；前三步都是纯几何变换，只有最后一步碰显存。
- **TileView 接口**用 `getViewIndexRank`/`getViewTileType`/`verifyIndices` 三个方法，让同一对 load/store 通用于三类视图，并把「结果 tile 类型」「index 个数」「index 结构」的校验下放到视图自身。
- **坐标语义**：index 为无符号整数；partition/strided 要求标量 tile，gather_scatter 的 sparse_dim 维要求一维 tile；index 类型须一致。
- **越界填充**是**视图类型属性**（`padding_value`，枚举 `zero`/`neg_zero`/`nan`/`pos_inf`/`neg_inf`，后 4 个仅限浮点）：load 设了则填充、没设则未定义；store 越界元素总是屏蔽。
- **两个查询**：`get_tensor_shape`（底层数据形状，输入 tensor_view）、`get_index_space_shape`（索引空间形状，输入任意 TileView），均无副作用、结果按无符号解释。

## 7. 下一步学习建议

- 本讲的 `padding_value` 与内存序只是视图语义的一部分；若想理解**原子**视图访存（分布式梯度累加等场景），请继续学习 **u5-l3（原子操作与内存序/作用域）**，那里讲 `atomic_red_view_tko` 如何在视图上做原子归约（注意它不支持带 `padding_value` 的视图）。
- 若想看视图操作如何被**编译期变换**优化（如循环分块、循环分裂对视图遍历的影响），可预习 **u9（优化器与变换 Pass）**，特别是 LoopSplit 对 for + view 遍历模式的作用。
- 若想从 Python 端程序化构造视图与 load/store，可参考 **u10-l2（Python 绑定架构）** 中 `cuda_tile_ops.py` 对视图类型与访存操作的封装。
- 建议同步阅读源码：`test/Dialect/CudaTile/view_invalid.mlir`（verifier 报错全集）与 `test/Dialect/CudaTile/memory_consistency_ops.mlir`（合法写法全集），两者对照是掌握视图访存最快的路径。
