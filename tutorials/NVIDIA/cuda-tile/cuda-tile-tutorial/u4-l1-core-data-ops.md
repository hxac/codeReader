# 核心数据操作：构造与形状变换

## 1. 本讲目标

本讲是「进阶·计算类操作」的第一篇。前几讲（u3-l1～u3-l3）我们已经把 `cuda_tile` 方言的**类型系统**讲清楚了：Tile 是落在寄存器里、编译期形状完全确定的小矩阵，Pointer 指向全局显存，View 描述分块访问几何。

但类型只是「容器」，要真正计算，还得有**往容器里放数据、改变容器形状、按条件挑选数据**的操作。这些最基础的数据搬运与形状变换操作，在 `cuda_tile` 方言里被归入 **Core 组**，本讲就逐一精读它们。

学完本讲你应当能够：

- 用 `constant` 和 `iota` 构造已知值的 Tile；
- 用 `reshape`、`broadcast`、`permute` 在不改变元素的前提下变换 Tile 的形状与维度排列；
- 用 `extract` 取子块、用 `cat` 沿维度拼接；
- 用 `select` 做按掩码的三元选择，用 `offset` 对指针 Tile 做地址偏移；
- 写出一段可被 `cuda-tile-opt` 验证通过的 `entry` 内核，并读懂 `test/Dialect/CudaTile/ops.mlir` 的测试约定。

## 2. 前置知识

- **Tile 与元素类型**（u3-l1）：Tile 写作 `tile<4x8xf32>`，由「静态形状 + 元素类型」组成；形状每维为 2 的幂，元素总数有上限。本讲所有操作都作用在 Tile 上。
- **Pointer Tile**（u3-l2）：`tile<Nxptr<f32>>` 是「一排指针」，是 `offset` 操作的输入，也是后续 `load_ptr_tko` 访存的基础。
- **Core 操作分组**（u2-l2）：`Ops.td` 用 `CudaTileCoreOpDef` 基类把本讲的这些操作归到 Core 分组，它们共享 `group = "Core"`、`sinceVersion = "13.1"` 等元数据。
- **`entry` / 默认方言**（u2-l2）：在 `entry`、`module` 等 region 内部可以省略 `cuda_tile.` 前缀，所以本讲示例里操作写作 `constant`、`broadcast`，而不是 `cuda_tile.constant`。
- **`cuda-tile-opt`**（u1-l3）：这是验证 MLIR 文本合法性的工具，能解析 + 校验 + 重新打印。`ops.mlir` 的首行 `RUN: cuda-tile-opt %s ...` 就是靠它跑起来的。本机若未构建，对应实践标注「待本地验证」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 方言操作的 TableGen 定义，本讲 9 个操作都在这里 |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h) | 手写的 C++ 操作头文件，用 include 技巧拼入代码生成的 `.inc` |
| [test/Dialect/CudaTile/ops.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir) | Core 组操作的 round-trip 测试，是写合法 IR 的最佳模板 |
| [test/Dialect/CudaTile/canonicalize.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/canonicalize.mlir) | `select`、`iota`、`offset` 的规范化（fold/canonicalize）测试 |
| [test/Dialect/CudaTile/invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir) | 非法写法的 `expected-error` 测试，看校验器会拒绝什么 |
| [README.md](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md) | `print` 内核示例，把 `iota`/`reshape`/`broadcast`/`offset` 串成一条真实地址构造链 |

## 4. 核心概念与源码讲解

本讲把 9 个 Core 组操作按职责拆成四个最小模块：

1. **构造数据**：`constant`、`iota`
2. **形状重塑与广播**：`reshape`、`broadcast`
3. **切片、拼接与重排**：`extract`、`cat`、`permute`
4. **条件选择与指针偏移**：`select`、`offset`

它们都派生自 `CudaTileCoreOpDef`（Core 分组），绝大多数带 `Pure` trait（无副作用、可重排、可被常量折叠）。先看一个共性：这些操作的定义都遵循「`summary` + `description` + `arguments` + `results` + `assemblyFormat`」的 TableGen 五件套，`assemblyFormat` 用 `custom<CudaTileType>(...)` 来按方言习惯打印类型（例如 `tile<4x8xf32>`）。操作真正的 C++ 类不是手写的，而是由 `Ops.h` 第 53 行的 `#define GET_OP_CLASSES` 配合 `#include "Ops.h.inc"` 自动注入（这是 u2-l3 讲过的 include 技巧）。

### 4.1 构造数据：constant 与 iota

#### 4.1.1 概念说明

要计算，第一步得有数据。Core 组提供两个「无输入、凭空产生 Tile」的操作：

- **`constant`**：把一个编译期已知的标量或张量值「装」进 Tile。它有两种形态——**标量广播态**（一个值填满整个 Tile）和**稠密列表态**（逐元素列出所有值）。
- **`iota`**：生成一个一维整数序列 `[0, 1, 2, ..., n-1]`。它最常见的用途是生成「元素下标」，再配合 `offset` 算出连续地址，是构造访存地址链的起点。

#### 4.1.2 核心流程

`constant` 的流程：

1. 读取属性 `$value`（一个 `DenseTypedElementsAttr`，即稠密元素属性）。
2. 校验属性里携带的「元素类型 + 形状」与结果 Tile 类型 `type($result)` 完全一致（`AllTypesMatch<["value", "result"]>`）。
3. 用自定义打印机 `custom<DenseTypedElementsAttr>` 把属性渲染成 `<D: c>` 或 `<D: [...]>` 的文本。

`iota` 的流程：

1. 不接受任何操作数，只看结果类型 `type($result)`。
2. 校验结果必须是**一维整数 Tile**；若元素数超过该整数类型能表示的最大值（如 `512xi8`），直接报错。
3. 语义上 `iota(n)` 第 `i` 个元素就是 `i`，结果按无符号整数解释。

iota 的数学定义：

\[
\text{iota}(n)_i = i \quad \text{for } i \in [0,\, n-1]
\]

#### 4.1.3 源码精读

`constant` 操作定义在 [Ops.td:1157-1191](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1157-L1191)。关键点：参数是稠密元素属性 `$value`，结果是 `CudaTile_NumberTileType`（整数或浮点 Tile），带 `ConstantLike`、`Pure` 和 `hasFolder`（可被常量折叠）。其 `description` 明确列出两种形态：

```
- One where the value is a single constant specified by `<D: c>` ...
- One where the value is a list of constants specified by `<D: [c0, c1, ...]>` ...
```

`iota` 操作定义在 [Ops.td:2530-2550](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2530-L2550)。它没有任何操作数（`arguments` 缺省），只有结果 `CudaTile_IntTileType`，带 `hasVerifier`。注意 `.note` 提示元素数不得超过元素类型能表示的最大值。

合法写法见 `ops.mlir` 里 `entry @test()` 开头的一串常量 [ops.mlir:44-77](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L44-L77)，覆盖了标量态与稠密态：

```
%c1      = constant <i1: true>                       : tile<i1>
%c42     = constant <i8: 42>                         : tile<i8>
%c_tensor= constant <f32: [[1.0, 2.0], [4.0, 5.0]]>  : tile<2x2xf32>
```

`iota` 的合法写法见 `conversion.mlir`（[conversion.mlir:694-713](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion.mlir#L694-L713) 附近），例如 `%iota_4 = iota : tile<4xi32>`。非法写法见 `invalid.mlir`：[invalid.mlir:185-196](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/invalid.mlir#L185-L196) 给出三条 `expected-error`：

```
// expected-error @below{{expects result type to be 1-d tile}}
cuda_tile.iota : !cuda_tile.tile<i64>            // 不是 1-d
cuda_tile.iota : !cuda_tile.tile<32x64xi64>      // 不是 1-d
// expected-error @below{{the number of elements 512 exceeds the maximum value of element type 'i8'}}
cuda_tile.iota : !cuda_tile.tile<512xi8>         // 512 > i8 无符号上限 255
```

#### 4.1.4 代码实践

**实践目标**：体会 `constant` 两种形态与 `iota` 的一维约束。

操作步骤：

1. 新建 `my_const.mlir`，写入下面的内核（注意 `entry` 内省略前缀）：

   ```
   cuda_tile.module @m {
     entry @e() {
       %s  = constant <f32: 3.14> : tile<4x4xf32>          // 标量广播态：16 个 3.14
       %d  = constant <f32: [[1.0,2.0],[3.0,4.0]]> : tile<2x2xf32>  // 稠密态
       %idx = iota : tile<8xi32>                            // [0,1,2,3,4,5,6,7]
     }
   }
   ```

2. 运行 `cuda-tile-opt my_const.mlir`，观察能否解析通过并被重新打印。

需要观察的现象 / 预期结果：`cuda-tile-opt` 会把内核原样打印（round-trip）；标量态 `<f32: 3.14>` 不变，稠密态 `<f32: [[...]]>` 按浮点格式（如 `1.000000e+00`）展开。

如果再把 `%bad = iota : tile<2x2xi32>` 加进去，应触发 `expects result type to be 1-d tile` 报错。

> 本机未构建 `cuda-tile-opt`，上述运行结果为**待本地验证**；报错文案以 `invalid.mlir` 的 `expected-error` 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `iota : tile<512xi8>` 非法，而 `iota : tile<256xi8>` 合法？

**答案**：`iota` 的结果按无符号解释，`i8` 最大表示 255。`512` 个元素意味着下标会到 511，超出 `i8` 表示范围；而 256 个元素下标最大 255，刚好不超。校验逻辑见 [Ops.td:2542-2545](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2542-L2545) 的 `.note`。

**练习 2**：用 `constant` 的**标量广播态**写一个 `tile<4x8xf32>` 全 0 张量。

**答案**：`%z = constant <f32: 0.0> : tile<4x8xf32>`。一个值自动填满 32 个元素。

### 4.2 形状重塑与广播：reshape 与 broadcast

#### 4.2.1 概念说明

`reshape` 和 `broadcast` 都**不改变元素本身**，只改变 Tile 的「形状」或「哪些维被复制」，但机制不同：

- **`reshape`**：纯改形状。元素个数与元素类型必须不变，按行主序（row-major）把数据重新切分。它甚至能把标量 Tile（0-d，恰含 1 个元素）reshape 成 `size==1` 的形状。
- **`broadcast`**：把输入里**大小为 1 的维**沿该维复制拉伸到结果尺寸。它不改变秩（rank），所以想改秩得先用 `reshape`。

#### 4.2.2 核心流程

`reshape` 流程：

1. 校验 source 与 result 元素类型相同（`SameOperandsAndResultElementType`）。
2. 校验元素总数相等（标量 Tile ↔ size==1 是唯一例外）。
3. 概念上先把 source 按行主序摊平成 1-d，再按行主序卷成结果形状。

`broadcast` 流程：

1. 校验 source 与 result **秩相同**（`AllRanksMatch`）且元素类型相同。
2. 对每一维：result 维大小要么等于 source 维大小，要么 source 维大小为 1（被拉伸）。

broadcast 直觉：

\[
\text{broadcast}(x,\; idim_n,\; odim_n) = x \quad \text{（仅当 } idim_n = 1 \text{ 时该维被复制到 } odim_n\text{）}
\]

#### 4.2.3 源码精读

`reshape` 定义在 [Ops.td:4020-4079](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4020-L4079)，关键描述：

> `reshape` is only a change in the indexing of the tile. The number of elements and element type must remain unchanged. 0-d tiles (i.e., scalars) contain precisely one element and thus are the one exception where a 0-d tile can be reshaped to shape where `size(shape) == 1`.

它带 `hasVerifier`。`ops.mlir` 里有来回 reshape 的例子 [ops.mlir:336-343](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L336-L343)：

```
%c_tensor_42       = reshape %c42      : tile<i8>    -> tile<1xi8>      // 标量 -> 1维
%c_tensor_reshaped = reshape %c_tensor_42 : tile<1xi8> -> tile<i8>      // 再变回去
%c_tensor_reshaped2= reshape %c_tensor : tile<2x2xf32> -> tile<4xf32>   // 2x2 -> 4
```

`broadcast` 定义在 [Ops.td:774-799](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L774-L799)，关键描述：

> The `broadcast` operation expands each unary (`1`) dimension in the input tile by duplicating the data along that dimension. ... The operation does not change the rank of the source tile.

合法写法见 [ops.mlir:411-425](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L411-L425)，三种广播方式：

```
%0 = broadcast %arg0 : tile<1x2xf32> -> tile<2x2xf32>   // dim0: 1->2
%0 = broadcast %arg0 : tile<2x1xf32> -> tile<2x2xf32>   // dim1: 1->2
%0 = broadcast %arg0 : tile<1x1xf32> -> tile<2x2xf32>   // 两维都拉伸
```

#### 4.2.4 代码实践

**实践目标**：理解「reshape 改元素布局、broadcast 只拉伸大小为 1 的维」。

操作步骤：

1. 在 `my_const.mlir` 的 `entry` 内追加：

   ```
   %r = reshape %s : tile<4x4xf32> -> tile<8x2xf32>     // 16 个元素，换布局
   %b = broadcast %s : tile<4x4xf32> -> tile<4x4xf32>   // 无 1 维可拉，等同（合法但无意义）
   ```

2. 再写一个**故意改秩**的 broadcast，观察它是否合法：

   ```
   %bad = broadcast %s : tile<4x4xf32> -> tile<4x8xf32>  // dim1: 4->8（非 1 维被拉伸）
   ```

需要观察的现象 / 预期结果：`reshape %s -> tile<8x2xf32>` 因元素数都是 16 而合法；最后一条 `%bad` 试图把 dim1 从 4 拉到 8，但 source 该维不是 1，应被 verifier 拒绝。

> 本机未构建 `cuda-tile-opt`，运行结果为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `tile<4xi32>` 变成 `tile<2x2xi32>` 用哪个操作？变成 `tile<4x4xi32>`（元素翻倍）能用 `reshape` 吗？

**答案**：前者用 `reshape`（元素数都是 4）。后者不能用 `reshape`——元素数从 4 变 16，违反「元素数不变」。若 source 形如 `tile<1x4xi32>`，可用 `broadcast` 把 dim0 从 1 拉到 4 得到 `tile<4x4xi32>`。

**练习 2**：`broadcast` 为什么要求 source 与 result「秩相同」？

**答案**：因为 broadcast 只在**已有维度上**复制大小为 1 的维，不增删维度。要改秩必须先 `reshape`（见 [Ops.td:784-786](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L784-L786)）。

### 4.3 切片、拼接与重排：extract、cat、permute

#### 4.3.1 概念说明

这三个操作把多个 Tile「重新组织」：

- **`extract`**：从大 Tile 里切出一个**整除大小**的子块。索引 `$indices` 指定切第几块，而不是字节偏移——只能取完整的、不重叠的切片。
- **`cat`**：把两个 Tile 沿指定维度 `dim` 拼接，该维结果大小 = 两者之和。
- **`permute`**：按一个排列数组重排维度（转置的推广），元素本身不变，只是索引顺序换了。

#### 4.3.2 核心流程

`extract` 流程：

1. 校验 result 形状在每一维上**整除** source 形状（如 `tile<8xf32>` 可切成 `tile<4xf32>`，但不能 `tile<3xf32>`）。
2. `$indices` 给出每一维「切第几块」（按无符号 `i32` 解释），索引越界为**未定义行为**。

`cat` 流程：

1. 两输入在**除拼接维外**的所有维形状必须相同，元素类型相同。
2. 结果第 `dim` 维大小 = 两输入该维之和。

`cat` 数学定义：

\[
\text{cat}(x, y, dim_{cat})[\vec{i}] =
\begin{cases}
x[..., i_{cat}, ...] & \text{if } i_{cat} < d_{cat} \\
y[..., i_{cat} - d_{cat}, ...] & \text{if } i_{cat} \ge d_{cat}
\end{cases}
\]

`permute` 流程：

1. 校验 `permutation` 是 source 维度的一个合法排列（`DenseI32ArrayAttr`）。
2. result 的第 `k` 维大小 = source 的第 `permutation[k]` 维大小。

#### 4.3.3 源码精读

`extract` 定义在 [Ops.td:1695-1743](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1695-L1743)，关键约束：

> The shape of the result tile must divide the shape of the source tile evenly ... The `$indices` indicate the number of the slice to extract, but *importantly* not the offsets ... only full size slices can be extracted.

`ops.mlir` 的 extract 测试覆盖 1-d/2-d/3-d/标量/边界 [ops.mlir:436-477](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L436-L477)：

```
%0 = extract %t[%idx]            : tile<8xf32>      -> tile<4xf32>     // 1-d: 8/4=2 块
%0 = extract %arg0[%c0, %c1]     : tile<4x8xf32>    -> tile<2x4xf32>   // 2-d
%0 = extract %arg0[]             : tile<f32>        -> tile<f32>       // 标量: 空索引
```

`cat` 定义在 [Ops.td:805-875](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L805-L875)，`dim` 是 `I64Attr`。测试 [ops.mlir:826-832](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L826-L832)：

```
%0 = cat %arg0, %arg0 dim = 0 : tile<1x2xf32>, tile<1x2xf32> -> tile<2x2xf32>
```

`permute` 定义在 [Ops.td:3698-3732](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3698-L3732)。例子说明很清楚：

> if the input tile has shape `[2, 4, 8]`, and the permutation is `[2, 0, 1]`, the output tile will have shape `[8, 2, 4]`.

测试 [ops.mlir:427-433](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L427-L433)：

```
%0 = permute %arg0 [1,0] : tile<1x2xf32> -> tile<2x1xf32>   // 2-d 转置
%1 = permute %arg0 [0,1] : tile<1x2xf32> -> tile<1x2xf32>   // 恒等排列
```

#### 4.3.4 代码实践

**实践目标**：用 `extract`/`cat`/`permute` 重组一个 Tile。

操作步骤：

1. 写一个 `testing$func`（测试用函数，需构建开启 `TILE_IR_INCLUDE_TESTS`），接受 `tile<8xf32>`：

   ```
   testing$func @rearrange(%a : tile<8xf32>) {
     %c0 = constant <i32: 0> : tile<i32>
     %half = extract %a[%c0] : tile<8xf32> -> tile<4xf32>   // 取前一半
     %cat  = cat %half, %half dim = 0 : tile<4xf32>, tile<4xf32> -> tile<8xf32>
   }
   ```

2. 把 `tile<2x4xf32>` 用 `permute [1,0]` 转成 `tile<4x2xf32>`，确认形状对得上。

需要观察的现象 / 预期结果：`extract` 取出 4 个元素；`cat` 拼回 8 个；`permute [1,0]` 把两维互换。`testing$func` 仅在测试构建下可用（见 u2-l2），若未开启测试构建，可把代码包进 `entry` 并去掉 `testing$` 前缀。

> 本机未构建工具链，运行结果为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `extract %t[%c] : tile<8xf32> -> tile<3xf32>` 非法？

**答案**：3 不能整除 8。`extract` 只能取「整除大小」的完整切片，见 [Ops.td:1702-1704](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1702-L1704)。

**练习 2**：`permute [2,0,1]` 作用在 `tile<2x4x8xf16>` 上，结果形状是什么？

**答案**：`tile<8x2x4xf16>`。result 第 k 维 = source 第 `perm[k]` 维，所以结果形状 = `[shape[2], shape[0], shape[1]] = [8, 2, 4]`。

### 4.4 条件选择与指针偏移：select 与 offset

#### 4.4.1 概念说明

最后两个 Core 操作服务于「按条件取值」和「构造地址」：

- **`select`**：元素级三元选择 `cond ? x : y`。`cond` 是 `tile<i1>` 掩码，逐元素决定取 `val_if_true` 还是 `val_if_false`。它带 `hasFolder` 和 `hasCanonicalizer`，常量条件会被折叠掉。
- **`offset`**：对**指针 Tile** 做地址偏移。它把一个「按元素计」的整数偏移换算成「按地址计」的增量，是构造访存地址链的关键一步（下一步通常接 `load_ptr_tko`）。

#### 4.4.2 核心流程

`select` 流程：

1. `cond` 必须是 `tile<...xi1>`。
2. `val_if_true`、`val_if_false`、`result` 三者元素类型相同（`AllTypesMatch`）。
3. 四个 Tile 形状全部相同（`AllShapesMatch`），逐元素选择。

select 数学定义：

\[
\text{select}(\text{cond}, x, y)_i =
\begin{cases}
x_i & \text{if } \text{cond}_i = 1 \\
y_i & \text{if } \text{cond}_i = 0
\end{cases}
\]

`offset` 流程：

1. `ptr` 是指针 Tile（`CudaTile_PointerTileType`），`offset` 是整数 Tile，两者与 result 形状相同（`SameOperandsAndResultShape`）。
2. 逐元素把指针按 pointee 的存储位宽前移。

offset 数学定义：

\[
\text{offset}(\text{ptr}, \text{off})_i = \text{ptr}_i + \text{off}_i \times \text{bitwidth}
\]

其中 `ptr` 按无符号、`off` 按有符号解释，`bitwidth` 为 pointee 类型的存储位宽；乘法不得有符号溢出，加法不得无符号溢出，否则结果未定义。

#### 4.4.3 源码精读

`select` 定义在 [Ops.td:4235-4270](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4235-L4270)。`cond` 用 `CudaTile_TileOf<[CudaTile_Int1]>` 限定为 i1 Tile；带 `hasFolder`/`hasCanonicalizer`。规范化用例见 `canonicalize.mlir`，例如常量条件折叠 [canonicalize.mlir:906-907](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/canonicalize.mlir#L906-L907)：

```
%2 = select %arg0, %true, %false  : tile<i1>, tile<i1>
%3 = select %2, %cst_0_i32, %cst_1_i32 : tile<i1>, tile<i32>
```

`offset` 定义在 [Ops.td:3614-3645](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3614-L3645)。它带 `Elementwise`、`Pure`，输出与 `ptr` 同类型（`AllTypesMatch<["result","ptr"]>`）。测试覆盖 i8/i16/i32/i64 四种偏移类型 [ops.mlir:479-501](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L479-L501)：

```
%0 = offset %ptr, %idx : tile<8xptr<f32>>, tile<8xi32> -> tile<8xptr<f32>>
```

**最重要的一段**——README 的 `print` 内核把这几个操作串成了真实地址构造链 [README.md:234-246](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L234-L246)：

```
entry @example_kernel(%data_pr : tile<ptr<f32>>) {
    %offsets           = iota : tile<128xi32>                                  // [0..127]
    %data_ptr_reshaped = reshape %data_pr : tile<ptr<f32>> -> tile<1xptr<f32>> // 标量指针 -> 1x1
    %data_ptr_broadcasted = broadcast %data_ptr_reshaped
                              : tile<1xptr<f32>> -> tile<128xptr<f32>>         // 复制成 128 个相同基址
    %data_ptr_tensor   = offset %data_ptr_broadcasted, %offsets
                              : tile<128xptr<f32>>, tile<128xi32> -> tile<128xptr<f32>>  // 各加偏移
    %data, %token      = load_ptr_tko weak %data_ptr_tensor
                              : tile<128xptr<f32>> -> tile<128xf32>, token      // 用地址 tile 读数据
}
```

这条链清晰展示了「`iota` 生下标 → `reshape`+`broadcast` 把单个基址扩成指针 Tile → `offset` 算出每个元素的地址 → `load_ptr_tko` 访存」的标准模式，是理解 Core 操作如何服务于访存的样板。

#### 4.4.4 代码实践

**实践目标**：用 `select` 做掩码选择，用 `offset` 构造指针 Tile。

操作步骤：

1. `select` 实践（包进 `entry`）：

   ```
   %mask = constant <i1: true> : tile<4xi1>
   %a    = constant <f32: 1.0> : tile<4xf32>
   %b    = constant <f32: 2.0> : tile<4xf32>
   %r    = select %mask, %a, %b : tile<4xi1>, tile<4xf32>   // 全 true，结果应全为 1.0
   ```

2. `offset` 实践：复制 README 里的地址构造链（`iota`+`reshape`+`broadcast`+`offset`），先不接 `load_ptr_tko`，只验证 `offset` 能解析通过。

需要观察的现象 / 预期结果：`select` 在 `cuda-tile-opt` 下若开了 `--canonicalize`，常量条件 `%mask = true` 会被折叠成直接取 `%a`；`offset` 链各步形状对齐（`128xptr<f32>` ↔ `128xi32`）即可通过校验。

> 本机未构建工具链，常量折叠与校验结果为**待本地验证**；`select` 的折叠行为以 `canonicalize.mlir` 的 CHECK 行为准。

#### 4.4.5 小练习与答案

**练习 1**：`select` 的 `cond` 能用 `tile<4xi32>` 吗？

**答案**：不能。`cond` 被显式约束为 `CudaTile_TileOf<[CudaTile_Int1]>`，即 i1 Tile（见 [Ops.td:4259](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4259)）。比较应先用 `cmpi` 产生 i1 结果再喂给 `select`。

**练习 2**：README 例子里，为什么 `offset` 的输入指针要先 `reshape` 成 `tile<1xptr<f32>>` 再 `broadcast` 成 `tile<128xptr<f32>>`？

**答案**：`%data_pr` 是标量指针 Tile（`tile<ptr<f32>>`，含 1 个元素），而 `offset` 要求 `ptr` 与 `offset` 操作数**形状相同**（`SameOperandsAndResultShape`），`offsets` 是 `tile<128xi32>`。所以必须先把单个基址扩成 128 个相同基址的指针 Tile，形状才能对齐。`reshape` 把 0-d 变 1-d、`broadcast` 把 dim0 从 1 拉到 128，正好完成这个升维扩维。

## 5. 综合实践

把本讲学到的操作串成一个可被 `cuda-tile-opt` 验证的 `entry` 内核，完整复现并扩展 README 的地址构造模式。

**实践目标**：综合运用 `constant`、`iota`、`reshape`、`broadcast`、`permute`，写出合法 IR 并通过 round-trip 校验。

**操作步骤**：

1. 新建 `u4_l1_practice.mlir`：

   ```
   cuda_tile.module @practice {
     entry @kernel(%base : tile<ptr<f32>>) {
       // 1) 用 iota 生成 0..127 的下标
       %idx = iota : tile<128xi32>

       // 2) 用 constant 构造一个 4x4 f32 tile（稠密态）
       %m   = constant <f32: [[1.0, 2.0, 3.0, 4.0],
                              [5.0, 6.0, 7.0, 8.0],
                              [1.0, 2.0, 3.0, 4.0],
                              [5.0, 6.0, 7.0, 8.0]]> : tile<4x4xf32>

       // 3) 形状变换：reshape 成 8x2，再 permute 成 2x8
       %r   = reshape %m : tile<4x4xf32> -> tile<8x2xf32>
       %p   = permute %r [1,0] : tile<8x2xf32> -> tile<2x8xf32>

       // 4) 地址构造链（README 模式）：基址扩成 128 个，各加偏移
       %base1  = reshape %base : tile<ptr<f32>> -> tile<1xptr<f32>>
       %baseN  = broadcast %base1 : tile<1xptr<f32>> -> tile<128xptr<f32>>
       %addrs  = offset %baseN, %idx : tile<128xptr<f32>>, tile<128xi32> -> tile<128xptr<f32>>

       return
     }
   }
   ```

2. 运行 `cuda-tile-opt u4_l1_practice.mlir`，确认能被解析、校验并原样打印。

3. **对比测试**：参考 `test/Dialect/CudaTile/ops.mlir` 的 RUN 行写法，把你的文件加上 `// RUN: cuda-tile-opt %s | FileCheck %s`，再写几条 `// CHECK:` 断言（如 `// CHECK: permute {{.*}} [1, 0]`），看看 round-trip 输出是否如你所料。

**需要观察的现象 / 预期结果**：

- `reshape %m -> tile<8x2xf32>`：16 个元素不变，合法。
- `permute [1,0]`：`8x2` 转成 `2x8`，合法。
- 地址链四步形状依次为 `ptr<f32>` → `1xptr<f32>` → `128xptr<f32>` → `128xptr<f32>`，`offset` 两操作数都为 `128x...`，合法。

**如果出错**：

- `permute` 后写成 `-> tile<8x2xf32>` 会因形状不匹配报错（应为 `2x8`）。
- 把 `broadcast` 那一步漏掉、直接 `offset %base1, %idx` 会因 `1xptr<f32>` 与 `128xi32` 形状不同报错。

> 本机未构建 `cuda-tile-opt`，上述运行/校验结果均为**待本地验证**。构建方法见 u1-l2 的 Quick Start；构建完成后工具位于 `build/bin/cuda-tile-opt`。

## 6. 本讲小结

- Core 组的 9 个操作都派生自 `CudaTileCoreOpDef`，绝大多数带 `Pure` trait，是无副作用的「数据搬运与形状变换」工具。
- `constant` 用稠密元素属性产生已知值 Tile，分标量广播态与稠密列表态；`iota` 生成一维 `[0,n-1]` 整数序列，结果必须是一维整数 Tile。
- `reshape` 在元素数与元素类型不变的前提下改形状（行主序），`broadcast` 只把大小为 1 的维复制拉伸、不改秩。
- `extract` 按整除大小切完整切片（索引选块、非字节偏移），`cat` 沿 `dim` 维拼接，`permute` 按排列数组重排维度。
- `select` 是元素级 `cond ? x : y`（cond 必为 i1 Tile），`offset` 按 pointee 存储位宽把整数偏移换算成指针 Tile 的地址增量。
- README 的 `print` 内核给出了「`iota`→`reshape`→`broadcast`→`offset`→`load_ptr_tko`」的标准地址构造链，是 Core 操作服务于访存的样板。

## 7. 下一步学习建议

- **整数与浮点算术**：下一讲 u4-l2 将在 Core 操作之上引入 Integer 组（`addi`/`muli`/`divi`/`cmpi` 等）及其有符号性与溢出提示，与本讲的 `select`/`iota` 配合可表达条件计算。
- **内存与访存**：本讲只构造了地址 Tile，真正的读写是 u5-l1 的 `load_ptr_tko`/`store_ptr_tko` 与 u5-l2 的 `load_view_tko`/`store_view_tko`，建议接着读 `test/Dialect/CudaTile/memory_consistency_ops.mlir`。
- **源码延伸阅读**：想看 Core 操作的校验与折叠实现，可读 `lib/Dialect/CudaTile/IR/Ops.cpp` 中各 `Op::verify()`/`Op::fold()`，以及 `lib/Dialect/CudaTile/IR/OpsCanonicalization.td` 里 `select` 等操作的规范化模式（u9-l4 会系统讲解）。
