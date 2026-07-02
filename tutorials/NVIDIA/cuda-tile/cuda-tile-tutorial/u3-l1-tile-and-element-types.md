# Tile 类型与元素类型

> 本讲是「进阶·类型系统」单元的第一篇。前置讲义 [u2-l2](u2-l2-dialect-definition.md) 已经讲过 `cuda_tile` 方言的骨架与 `CudaTileOpDef` 分组机制；本讲从「类型」入手，先建立 Tile 这个核心数据结构。

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂并正确书写 Tile 类型的文本语法 `tile<4x8xf32>`，知道它由「静态形状 + 元素类型」两部分组成。
2. 说出 Tile 允许哪些元素类型（整数、浮点、指针，以及特殊的 `i4`），并能区分 `NumberType`、`TileElementType`、`IntTileType`、`FloatTileType` 等约束类的覆盖范围。
3. 解释 `maxTileNumElements`（1600 万）这个上限的硬件动机，以及 `verifyTileSize` 强制的「正、2 的幂、不超上限」三条规则。
4. 用 `cuda-tile-opt` 验证一段含多种 Tile 类型的 MLIR，并解读越界/非法形状的报错。

## 2. 前置知识

在进入源码前，先回顾三个 MLIR 基础概念（[u2-l2](u2-l2-dialect-definition.md) 已铺垫过方言层面）：

- **Type（类型）**：MLIR 里描述「值是什么」的对象。CUDA Tile 在 `cuda_tile` 方言下自定义了一组 Type，统称 `CudaTileType`。
- **TypeDef / TableGen**：类型的「声明」写在 `.td` 文件里，由 `cuda-tile-tblgen` 生成 C++ 胶水（`Types.h.inc` / `Types.cpp.inc`）。每个 Type 通常自带三件套：`parse`（文本→对象）、`print`（对象→文本）、`verify`（合法性校验）。
- **ShapedType 接口**：MLIR 内置的「有形状类型」接口（`getShape`、`getElementType`、`getNumElements` 等）。CUDA Tile 的 `TileType` 实现了它（`Types.td:126` 的 `[ShapedTypeInterface]`），所以可以被很多通用 Pass 当作「形状类型」处理。

一句话理解 Tile：**Tile 是「一块固定大小的元素集合」，是张量核（Tensor Core）上计算与寄存器分配的基本单位。** 它不是 Python 里的 ndarray，也不是 MLIR 的 `tensor`——它更像一个「编译期形状完全确定、落在寄存器里」的小矩阵。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Types.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td) | 用 TableGen 声明所有类型：元素类型别名、`TileType`、`PointerType`、各类 View 类型，以及给操作用的「Tile 约束类」。 |
| [Types.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h) | 类型工具函数声明：`maxTileNumElements` 常量、`isPointerLike`、`getI1SameShape`、解析/打印助手，以及「任何 cuda_tile 类型」的基类 `CudaTileType`。 |
| [Types.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp) | 类型的 `parse`/`print`/`verify` 实现：Tile 的形状解析、校验、以及前缀省略打印逻辑。 |
| [SharedVerifiers.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h) | 多个类型/操作共用的校验函数，重点是 `verifyTileSize`。 |
| [types.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir) | Tile/View 类型的 round-trip 测试，是本讲实践的直接参考。 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **TileType 定义**——形状与元素的静态分块。
2. **元素类型集合与类型约束类**——哪些元素合法、`i4` 为何特殊。
3. **maxTileNumElements 上限与解析/打印工具**——硬件上限、校验与文本往返。

### 4.1 TileType：张量核上的寄存器分块

#### 4.1.1 概念说明

GPU 上做矩阵乘等密集计算时，性能的关键是「把数据切成刚好能塞进寄存器/Tensor Core 的小块」。CUDA Tile 把这种「小块」直接做成 IR 的一等类型——这就是 `TileType`。

它的两条核心约束（见下方源码描述）：

- **形状完全静态**：每一维的大小在编译期就是已知常数，不能像 `tensor<?xf32>` 那样带 `?`。
- **所有元素同类型**：一块 Tile 里要么全是 `f32`，要么全是 `i32`……不能混。

文本写作 `!cuda_tile.tile<shapexelem>`，例如：

- `!cuda_tile.tile<4x8xf32>` —— 4×8 的 32 位浮点块，32 个元素。
- `!cuda_tile.tile<128xi32>` —— 一维 128 个 32 位整数。
- `!cuda_tile.tile<f32>` —— **零维（标量）Tile**，只有一个元素，没有形状维度。
- `!cuda_tile.tile<4x!cuda_tile.ptr<i8>>` —— 4 个指向 `i8` 的指针（指针也是合法元素，详见 4.2）。

> 名词解释：**标量 Tile（scalar tile / rank-0 tile）**指形状为空（`shape = []`）的 Tile，`getNumElements() == 1`。它常用于承载「单个标量值」，便于让标量也走 Tile 的统一管线。

#### 4.1.2 核心流程

一段文本 `!cuda_tile.tile<4x8xf32>` 被解析成一个 `TileType` 对象的过程：

```text
文本 !cuda_tile.tile<4x8xf32>
        │
        ▼  TileType::parse()                 (Types.cpp:176)
   ① parseLess()            消费 '<'
   ② parseDimensionList(    收集形状 dims=[4,8]，allowDynamic=false
        allowDynamic=false)   → 因此 tile 不允许出现 '?'
   ③ parseCudaTileType()    解析元素类型 f32（递归处理 ptr/tf32 等）
   ④ parseGreater()         消费 '>'
        │
        ▼  getChecked<TileType>() 触发校验
   ⑤ TileType::verify()     (Types.cpp:194)
        ├─ 若元素是 f4E2M1FN：要求元素总数为偶（2 个打包进 1 字节）
        └─ verifyTileSize()   (SharedVerifiers.h:67)
             每维：正  ∧  2 的幂  ∧  累乘不超 maxTileNumElements（带溢出守卫）
        │
        ▼  通过则被 MLIR intern（同形状+同元素 → 同一对象）
   返回不可变的 TileType{ shape=[4,8], elem=f32 }
```

校验里「元素总数」是关键量：

\[
\text{numElems} = \prod_{i} \text{shape}_i
\]

它必须满足：

\[
\text{numElems} \le \text{maxTileNumElements} = 16\,777\,216 = 2^{24}
\]

且每个维度都必须是 2 的幂：\(\text{shape}_i = 2^{k_i},\ k_i \ge 0\)。

#### 4.1.3 源码精读

**类型声明**——[Types.td:125-167](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L125-L167)：声明 `TileType`，挂上 `ShapedTypeInterface`，并给出两个参数 `shape` 与 `elementType`。

关键片段（仅保留要点）：

```tablegen
def CudaTile_TileType : CudaTileTypeDef<"Tile", "tile", "tileType", "13.1",
    [ShapedTypeInterface]> {
  let summary = "Tile type";
  let description = [{
    A tile type has a shape and an element type. The shape of the tile
    must be fully static. All elements of the tile have the same element
    type. ... Only power-of-two shape dimensions are supported.
  }];
  let parameters = (ins
    CudaTileTypeParam<...int64_t...>:$shape,
    CudaTileConstrainedTypeParam<CudaTile_TileElementType, "13.1">:$elementType
  );
  let hasCustomAssemblyFormat = 1;
  let genVerifyDecl = 1;
  let extraClassDeclaration = [{
    bool hasRank() const { return true; }
    TileType cloneWith(std::optional<ArrayRef<int64_t>> shape,
                       Type elementType) const;
  }];
}
```

要点解读：

- `mnemonic = "tile"` 决定了文本里的 `tile<...>`。
- 第 4 个参数 `"13.1"` 是 `sinceVersion`——这个类型自字节码 13.1 起存在（与 [u7-l4](u7-l4-bytecode-versioning.md) 字节码版本挂钩）。
- `hasCustomAssemblyFormat = 1` + `genVerifyDecl = 1`：手写 `parse/print/verify`（在 `Types.cpp` 里），而非用 TableGen 自动生成。
- `extraClassDeclaration` 里的 `hasRank()` 永远返回 `true`，向 `ShapedTypeInterface` 承诺「Tile 一定有 rank」。

**解析与打印**——[Types.cpp:176-192](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L176-L192)：

```cpp
Type cuda_tile::TileType::parse(AsmParser &parser) {
  SmallVector<int64_t> dims;
  Type elementType;
  if (parser.parseLess() ||
      parser.parseDimensionList(dims, /*allowDynamic=*/false) ||  // ← 不允许 '?'
      parseCudaTileType(parser, elementType) || parser.parseGreater())
    return Type();
  return parser.getChecked<cuda_tile::TileType>(loc, ..., dims, elementType);
}

void cuda_tile::TileType::print(AsmPrinter &printer) const {
  printer << "<";
  printShapeAndElem(printer, getShape(), getElementType());  // "4x8xf32"
  printer << ">";
}
```

`parseDimensionList(dims, /*allowDynamic=*/false)` 这一行就是「形状必须全静态」的落点：传 `false` 后解析器遇到 `?` 会直接报错。打印侧的 `printShapeAndElem`（[Types.cpp:52-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L52-L60)）负责拼出 `4x8xf32` 形式；当形状为空（标量 Tile）时只打印元素类型，于是得到 `tile<f32>`。

**校验**——[Types.cpp:194-205](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L194-L205)：

```cpp
LogicalResult cuda_tile::TileType::verify(..., ArrayRef<int64_t> shape,
                                          Type elementType) {
  if (isa<Float4E2M1FNType>(elementType)) {
    // f4 必须「元素总数为偶」，因为 2 个 f4 打包进 1 字节
    if (shape.empty() ||
        llvm::all_of(shape, [](int64_t dim) { return dim % 2 != 0; }))
      return emitError() << "F4E2M1FN tiles must have an even number of elements";
  }
  return verifyTileSize(emitError, shape);
}
```

注意 f4 的判断 `all_of(dim % 2 != 0)`：当且仅当**每一维都是奇数**时报错——因为元素总数 = 各维乘积，乘积为奇当且仅当每个因子为奇；只要有一维为偶，总数即为偶。所以 `tile<2xf4E2M1FN>` 合法、`tile<3x2xf4E2M1FN>` 合法（有一维是 2）、`tile<3xf4E2M1FN>` 非法。

#### 4.1.4 代码实践

**目标**：用 `cuda-tile-opt` 验证多种 Tile 类型的合法性，并观察「非 2 的幂」时报什么错。

**操作步骤**：

1. 参考 [test/Dialect/CudaTile/types.mlir:1](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir#L1) 的 `RUN` 行，可知 `cuda-tile-opt` 既能解析验证、又能打印 IR。
2. 在构建目录（按 [u1-l2](u1-l2-repo-and-build.md) 配置过 `CUDA_TILE_ENABLE_TESTING=ON`）下新建 `tile_types.mlir`：

   ```mlir
   cuda_tile.module @kernels {
   testing$func @test_tile_types(
       %a0: !cuda_tile.tile<4x8xf32>,
       %a1: !cuda_tile.tile<128xi32>,
       %a2: !cuda_tile.tile<f32>,          // 标量 tile
       %a3: !cuda_tile.tile<2x2x!cuda_tile.ptr<i8>>) {
     return
   }
   }
   ```

   > 说明：`testing$func` 是测试专用入口（受 `TILE_IR_INCLUDE_TESTS` 保护，见 [u2-l2](u2-l2-dialect-definition.md)）。它仅用来「装载类型声明」，函数体留空即可。
3. 运行：`./build/bin/cuda-tile-opt tile_types.mlir`（路径按你的构建目录调整）。

**预期结果（待本地验证）**：合法时，工具原样打印这段 IR（round-trip 通过），与 [types.mlir:12-21](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/types.mlir#L12-L21) 的 `tile<2xf32>`/`tile<f32>`/`tile<2xf4E2M1FN>` 行为一致。

**延伸观察**：把上面任一维换成非 2 的幂，例如新增一行：

```mlir
%a4: !cuda_tile.tile<2x3x!cuda_tile.ptr<i8>>
```

`3` 不是 2 的幂，`verifyTileSize` 会在 [SharedVerifiers.h:80-82](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L80-L82) 报错：

```text
all dimensions must be powers of two, got [2, 3]
```

> 小提醒：规格示例里给出的 `tile<2x3xptr<i8>>` 本身就含非 2 的幂维度（3），所以它**不会**通过校验——这正好用来体会「2 的幂」这条规则。

#### 4.1.5 小练习与答案

**练习 1**：下列哪些 Tile 类型能通过校验？(a) `tile<1xf32>` (b) `tile<6xi32>` (c) `tile<4x4x4xf16>` (d) `tile<f4E2M1FN>`

> **答案**：(a) 合法——`1 = 2^0` 是 2 的幂，元素数 1 ≤ 上限。(b) 非法——`6` 不是 2 的幂。(c) 合法——三维都是 2 的幂，64 个元素。(d) 非法——f4 的标量 Tile 元素数为 1（奇），触发「must have an even number of elements」。

**练习 2**：为什么 `TileType` 要强制形状全静态、且每维是 2 的幂？

> **答案**：静态形状让编译器在编译期就能算出寄存器占用、规划张量核指令；2 的幂则是张量核硬件指令（如各类 MMA）对操作数形状的天然要求，同时便于位对齐与打包。

---

### 4.2 元素类型集合与类型约束类

#### 4.2.1 概念说明

Tile 的元素可以是哪些类型？答案是 `CudaTile_TileElementType`。理解它的关键是先理清 CUDA Tile 的**元素类型约束层级**——这是一组 TableGen 的 `AnyTypeOf`，最终会被代码生成成 C++ 的判定函数（如 `isAnyInt`、`isAnyFloat`），供操作的类型约束引用。

层级关系如下：

```text
CudaTile_NumberType        = AnyFloat ∪ AnyInt
CudaTile_AnyInt            = { i1, i8, i16, i32, i64 }        ← 注意：不含 i4
CudaTile_AnyFloat          = { f16, bf16, f32, tf32, f64,
                               f8E4M3FN, f8E5M2, f8E8M0FNU, f4E2M1FN }
CudaTile_PointerType       = ptr<pointee> ，pointee ∈ NumberType
─────────────────────────────────────────────────────────────
CudaTile_TileElementType   = NumberType ∪ PointerType ∪ Int4   ← Tile 唯一可用的元素集
```

**最重要的细节：`i4` 是「二等公民」。**

- `i4` 能作为 **Tile 的元素**（出现在 `TileElementType` 里）。
- 但 `i4` **不在** `AnyInt`，因此也不在 `NumberType`。
- 因为 `PointerType` 的 pointee 必须是 `NumberType`（[Types.td:108](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L108)），所以**你不能写 `ptr<i4>`**。

> 名词解释：**`sinceVersion`**——`Types.td` 里每个元素别名都标了版本，例如 `f8E8M0FNU` 是 `13.2`、`i4`/`f4E2M1FN` 是 `13.3`。它告诉字节码读写器：低于该版本的字节码里不应出现此类型（详见 [u7-l4](u7-l4-bytecode-versioning.md)）。

#### 4.2.2 核心流程

元素类型的声明分两层：

1. **「版本化别名」**：用 `CudaTileTypeAlias` 给 MLIR 内置类型（`I1`、`F32`、`F8E4M3FN`…）起一个带版本的名字。
2. **「约束类」**：把若干别名用 `AnyTypeOf` 组合，得到 `AnyInt`/`AnyFloat`/`NumberType`/`TileElementType` 等；这些约束既用于 Tile 的元素参数，也用于「Tile 约束类」（`IntTileType` 等）来限定操作接受的 Tile。

操作（如 `addi`、`addf`）不会直接列举元素类型，而是引用「Tile 约束类」：

| 约束类 | 允许的 Tile 元素 | 典型用途 |
| --- | --- | --- |
| `CudaTile_IntTileType` | i1, i8, i16, i32, i64 | 整数算术 `addi/muli/...` |
| `CudaTile_FloatTileType` | 全部 9 种浮点 | 浮点算术 `addf/mulf/fma` |
| `CudaTile_BaseFloatTileType` | f16, bf16, f32, f64 | 只接受「常规」浮点的操作 |
| `CudaTile_NumberTileType` | 整数 ∪ 浮点 | 通用数值操作 |
| `CudaTile_PointerTileType` | ptr | 指针运算（如 `offset`） |

注意 `IntTileType` 同样**不含 `i4`**——这与 `AnyInt` 一致；`i4` 主要出现在 MMA 等低精度场景，由专门的操作处理。

#### 4.2.3 源码精读

**整数别名与 `AnyInt`**——[Types.td:41-54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L41-L54)：

```tablegen
def CudaTile_Int1  : CudaTileTypeAlias<I1, "13.1", "i1">;
def CudaTile_Int4  : CudaTileTypeAlias<I<4>, "13.3", "i4">;   // 13.3 才引入
def CudaTile_Int8  : CudaTileTypeAlias<I8, "13.1", "i8">;
...
def CudaTile_AnyInt : AnyTypeOf<[CudaTile_Int1, CudaTile_Int8,
                                 CudaTile_Int16, CudaTile_Int32,
                                 CudaTile_Int64]> {           // ← 没有 Int4
  let cppFunctionName = "isAnyInt";
}
```

`cppFunctionName = "isAnyInt"` 让代码生成产出 `bool isAnyInt(Type)`，操作约束即可用它做运行时判定。

**浮点别名与 `AnyFloat`**——[Types.td:60-81](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L60-L81)：列出全部 9 种浮点（含 `f8E8M0FNU`@13.2、`f4E2M1FN`@13.3），`cppFunctionName = "isAnyFloat"`。

**`NumberType` 与 `TileElementType`**——[Types.td:83-86](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L83-L86) 与 [Types.td:118-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L118-L123)：

```tablegen
def CudaTile_NumberType : AnyTypeOf<[CudaTile_AnyFloat, CudaTile_AnyInt]> {
  string cppType = "::mlir::Type";
}

def CudaTile_TileElementType : AnyTypeOf<[CudaTile_NumberType,
                                          CudaTile_PointerType,
                                          CudaTile_Int4]> { ... }  // ← i4 在这里补回
```

这就是「`i4` 不在 Number、却能进 Tile」的根因：`TileElementType` 在 `NumberType ∪ PointerType` 之外，**额外**把 `Int4` 加了回来。

**指针类型**——[Types.td:92-112](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L92-L112)：`ptr<pointee>`，`pointee` 受 `CudaTileConstrainedTypeParam<CudaTile_NumberType, ...>` 约束，所以 pointee 必须是 `NumberType`（不含 `i4`、也不含嵌套指针）。

**Tile 约束类**——[Types.td:795-818](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L795-L818)：

```tablegen
def CudaTile_IntTileType : CudaTile_TileOf<[Int1, Int8, Int16, Int32, Int64]>;
def CudaTile_FloatTileType : CudaTile_TileOf<[Float16, BFloat16, Float32, Float64,
                                              TFloat32, Float8E4M3FN, Float8E5M2,
                                              Float8E8M0FNU, Float4E2M1FN]>;
def CudaTile_NumberTileType : CudaTile_TileOf<[ ... 整数 ∪ 浮点 ... ]>;
def CudaTile_PointerTileType : CudaTile_TileOf<[CudaTile_PointerType]>;
```

`CudaTile_TileOf`（[Types.td:173-181](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L173-L181)）是个容器构造器：它要求「类型必须是 `TileType`」并且「元素类型在允许列表里」。

#### 4.2.4 代码实践

**目标**：亲手验证「`i4` 可作 Tile 元素、但不可作指针 pointee」。

**操作步骤**：

1. 新建 `elem_types.mlir`：

   ```mlir
   cuda_tile.module @kernels {
   testing$func @ok(
       %a: !cuda_tile.tile<16xi4>,                 // i4 作 tile 元素：合法
       %b: !cuda_tile.ptr<i32>) {                  // ptr<i32>：合法
     return
   }
   }
   ```

   运行 `cuda-tile-opt elem_types.mlir`，应能正常 round-trip。
2. 再新建 `bad.mlir`，故意写一个非法的 `ptr<i4>`：

   ```mlir
   cuda_tile.module @kernels {
   testing$func @bad(%a: !cuda_tile.ptr<i4>) { return }
   }
   ```

   运行 `cuda-tile-opt bad.mlir`。

**预期结果（待本地验证）**：`ptr<i4>` 会因为 pointee 不满足 `NumberType` 约束而报错；`tile<16xi4>` 则通过（注意 `16` 是 2 的幂，且 f4 偶数约束只针对 `f4E2M1FN`，对 `i4` 不生效）。

#### 4.2.5 小练习与答案

**练习 1**：判断对错——「`tile<8xbf16>` 合法，因为 `bf16` 在 `AnyFloat` 里。」

> **答案**：对。`bf16` ∈ `AnyFloat` ⊂ `NumberType` ⊂ `TileElementType`，`8` 是 2 的幂，元素数 8 ≤ 上限，全部满足。

**练习 2**：为什么 `addi`（整数加）不能作用于 `tile<16xi4>`？

> **答案**：`addi` 的操作数约束是 `IntTileType`，而 `IntTileType` 列表（[Types.td:795-797](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L795-L797)）不含 `i4`。`i4` 是为低精度 MMA 打包引入的，由专门操作（见 [u4-l5](u4-l5-mma-ops.md)）处理。

---

### 4.3 maxTileNumElements 上限与解析/打印工具

#### 4.3.1 概念说明

Tile 越大越好吗？不是。Tile 的元素最终要落到 GPU **寄存器**里，过大的 Tile 会带来「灾难性的寄存器压力」，让编译器在寄存器分配阶段长时间卡顿甚至崩溃。为此 CUDA Tile 给元素总数设了一个硬上限：

\[
\text{maxTileNumElements} = 16\,777\,216 = 2^{24}
\]

这个数（约 1600 万）不是随便拍的。源码注释（[Types.h:28-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h#L28-L43)）给出的粗略估算思路是：

\[
\text{上限} \approx \text{factor}(4) \times \text{maxCTAsPerCGA}(16) \times \text{maxOnChipRegsPerCTA}(256\text{K})
\]

即允许 Tile 略大于单 CTA 的物理寄存器容量（factor > 1），但不能太大。注释还提到：即便有 slice 优化兜底，过大的 Tile 仍会导致编译时间过长、且往往因塞不进硬件而性能很差。**注意单位是「元素个数」而非「字节」**——`tile<Nxi1>` 与 `tile<Nxf64>` 都算 N 个元素。

#### 4.3.2 核心流程

`maxTileNumElements` 这个常量只在**一个地方**被强制执行：`verifyTileSize`。它在 `TileType::verify`（[Types.cpp:204](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L204)）和所有 View 类型的 `verify`（如 [Types.cpp:589](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L589)）里被调用，确保「凡是形状会落成 Tile 的地方」都受同一上限约束。

`verifyTileSize` 的三条规则（带溢出守卫，防止累乘溢出绕过上限）：

```text
对 shape 中的每一维 dim：
  ① dim > 0              （正）
  ② dim 是 2 的幂          （2 的幂）
  ③ numElems > max/ dim   （累乘前预判，防止 int64 溢出）
       是 → 报「tile would exceed the maximum of 16777216 elements」
       否 → numElems *= dim
```

第 ③ 步用 `numElems > kMaxElems / dim` 而不是 `numElems * dim > kMaxElems`，正是经典的**溢出安全写法**：先除后比，避免 `numElems * dim` 本身溢出成负数而漏判。

> 解析/打印工具：Tile 的文本解析（`TileType::parse`）和打印（`TileType::print`）已在 4.1 讲过。除此之外，`Types.h` 还提供一组「方言感知」的助手：`parseCudaTileType`/`printCudaTileType`（[Types.cpp:89-143](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L89-L143)）会在类型属于 `cuda_tile` 方言时**省略 `!cuda_tile.` 前缀**，并处理 `token` 与 builtin `token` 重名等边角情况。这正是 `tile<4x8xf32>` 里元素类型可以不写前缀的原因。

#### 4.3.3 源码精读

**常量与动机**——[Types.h:28-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h#L28-L43)：

```cpp
// Since H100 has 256KB registers, we should allow users to create tiles
// of size up to 256K elements. ... A very rough estimation for the limit
// may be something like:
// factor(4) x max-num-of-ctas-per-cga(16) x maxOnChipRegisterPerCta(256k)
int64_t constexpr maxTileNumElements = 16777216;
```

`constexpr` 意味着它是编译期常量，可直接用在 `verifyTileSize` 的 `constexpr int64_t kMaxElems` 里。

**校验实现**——[SharedVerifiers.h:63-93](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L63-L93)：

```cpp
static inline LogicalResult
verifyTileSize(function_ref<InFlightDiagnostic()> emitError, ArrayRef<int64_t> shape) {
  constexpr int64_t kMaxElems = maxTileNumElements;
  int64_t numElems = 1;
  for (int64_t dim : shape) {
    if (dim <= 0)
      return emitError() << "all dimensions must be positive constants, got " << shape;
    if (!llvm::isPowerOf2_64(static_cast<uint64_t>(dim)))
      return emitError() << "all dimensions must be powers of two, got " << shape;
    if (numElems > kMaxElems / dim)               // ← 溢出安全写法
      return emitError() << "tile would exceed the maximum of " << kMaxElems << " elements";
    numElems *= dim;
  }
  return success();
}
```

**前缀省略打印**——[Types.cpp:136-143](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L136-L143)：

```cpp
void cuda_tile::printCudaTileType(AsmPrinter &p, Type type) {
  if (isa<CudaTileDialect>(type.getDialect()) &&
      succeeded(generatedTypePrinter(type, p)))
    return;              // 属于 cuda_tile 方言 → 用生成的 printer（不带前缀）
  p.printType(type);     // 否则按标准方式（带 !dialect. 前缀）
}
```

**其他工具函数**——[Types.h:48-78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h#L48-L78) 声明了若干实用助手：

- `isPointerLike(Type)`：判断是否为指针或「指针 Tile」（递归查 Tile 的元素），实现见 [Types.cpp:40-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L40-L46)。
- `getI1SameShape(Type)`：返回「同形状、元素为 i1」的 Tile，常用于构造掩码（mask）。
- `reshapeTileTypeToRank(TileType, targetRank)`：在左侧补若干个 `1` 维，把 Tile 升到目标秩。

#### 4.3.4 代码实践

**目标**：构造一个超过 `maxTileNumElements` 的 Tile，观察「上限」报错；并验证「恰好等于上限」的边界情况。

**操作步骤**：

1. 计算：上限 \(2^{24} = 16\,777\,216\)。要构造一个**超过**它的 Tile，且每维仍是 2 的幂，最简单是 `tile<4096x8192xf32>`，因为：

   \[
   4096 \times 8192 = 2^{12} \times 2^{13} = 2^{25} = 33\,554\,432 > 2^{24}
   \]

2. 新建 `too_big.mlir`：

   ```mlir
   cuda_tile.module @kernels {
   testing$func @too_big(%a: !cuda_tile.tile<4096x8192xf32>) { return }
   }
   ```
3. 运行 `cuda-tile-opt too_big.mlir`。
4. 对照地，试一个**恰好等于上限**的：`tile<4096x4096xf32>`（\(2^{12}\times2^{12}=2^{24}\)），看是否通过。

**预期结果（待本地验证）**：

- `tile<4096x8192xf32>`：在累乘到第二维时，`numElems(4096) > kMaxElems(16777216) / 8192 (=2048)` 成立，报：

  ```text
  tile would exceed the maximum of 16777216 elements
  ```
- `tile<4096x4096xf32>`：\(4096 \le 16777216/4096 = 4096\) 不成立（4096 不大于 4096），通过。

#### 4.3.5 小练习与答案

**练习 1**：`tile<16777216xf32>`（一维，恰好 \(2^{24}\)）合法吗？`tile<33554432xf32>`（\(2^{25}\)）呢？

> **答案**：前者合法（\(2^{24} \le\) 上限）；后者非法——累乘时 `1 > 16777216 / 33554432` 不成立但之后 `numElems=33554432`，下一轮无更多维度，所以需看循环逻辑：实际上单维情况下 `numElems(1) > kMaxElems/33554432 (=0)` 为真，立即报「exceed the maximum」。结论仍是非法。

**练习 2**：为什么校验里写成 `numElems > kMaxElems / dim`，而不是 `numElems * dim > kMaxElems`？

> **答案**：为避免 `numElems * dim` 在 `int64` 下溢出（尤其当 shape 维度极大时乘积可能溢出成小正数甚至负数，从而漏判）。先除后比是数值安全的惯用写法。

---

## 5. 综合实践

把本讲三块知识串起来，做一个「元素类型清单 + 约束探索」的小任务。

**任务**：编写一个 `inventory.mlir`，在 `testing$func` 的参数里为 `TileElementType` 支持的**每一类**元素各列一个合法 Tile，并故意埋下三处错误，逐一对照源码解释报错来源。

参考骨架（请自行补全/修改）：

```mlir
cuda_tile.module @kernels {
testing$func @inventory(
    // 合法：每类各一
    %i   : !cuda_tile.tile<4xi32>,                  // AnyInt
    %f   : !cuda_tile.tile<4xf32>,                  // AnyFloat
    %p   : !cuda_tile.tile<4x!cuda_tile.ptr<i8>>,   // PointerType
    %i4  : !cuda_tile.tile<16xi4>,                  // Int4（仅 tile 元素）
    %scal: !cuda_tile.tile<f64>) {                  // 标量 tile
  return
}
}
```

然后**分三次单独实验**，每次只加一行非法声明，用 `cuda-tile-opt` 跑、记录报错、并指到本讲引用的源码行：

1. 非法 pointee：`!cuda_tile.ptr<i4>` → pointee 不满足 `NumberType`（[Types.td:108](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L108)）。
2. 非 2 的幂：`!cuda_tile.tile<3x4xf32>` → `verifyTileSize` 幂次检查（[SharedVerifiers.h:80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L80)）。
3. 超上限：`!cuda_tile.tile<4096x8192xf32>` → `verifyTileSize` 上限检查（[SharedVerifiers.h:85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L85)）。

> 评价标准：合法清单能 round-trip 通过；三处非法各自报出**不同**的错误信息，且你能分别指出对应的源码校验点，即说明你已掌握 Tile 类型与元素类型。

## 6. 本讲小结

- `TileType = 静态形状 + 元素类型`，文本为 `tile<4x8xf32>`；形状必须全静态、每维为 2 的幂，标量 Tile 写作 `tile<f32>`。
- Tile 允许的元素由 `CudaTile_TileElementType`（= `NumberType ∪ PointerType ∪ Int4`）决定；`i4` 只能作 Tile 元素，不在 `AnyInt`/`NumberType`，也不能作 `ptr` 的 pointee。
- 操作通过 `IntTileType`/`FloatTileType`/`NumberTileType`/`PointerTileType` 等「Tile 约束类」限定接受的 Tile（见 [Types.td:795-818](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L795-L818)）。
- `maxTileNumElements = 16,777,216`（\(2^{24}\)）是寄存器压力驱动的硬上限，由 `verifyTileSize`（正、2 的幂、带溢出守卫的上限检查）统一强制。
- `parse`/`print` 由 `TileType::parse/print` + `parseCudaTileType/printCudaTileType` 协作，实现「省略 `!cuda_tile.` 前缀」的可读文本。

## 7. 下一步学习建议

- **指针类型与 Token 类型**：本讲只把 `ptr` 当作 Tile 元素一笔带过，下一讲 [u3-l2 指针类型与 Token 类型](u3-l2-pointer-and-token-types.md) 会展开 `PointerType` 与用于排序的 `TokenType`，并介绍 `isPointerLike`/`getI1SameShape` 等工具的实战用法。
- **视图类型族**：`maxTileNumElements` 与 `verifyTileSize` 同样约束四类 View 的 `tile_shape`，建议学完 [u3-l3 视图类型族](u3-l3-view-types.md) 后回看 `verifyPartitionViewLike`（[Types.cpp:530-613](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Types.cpp#L530-L613)），体会「同一套 Tile 校验如何在 View 里复用」。
- **想动手验证**：本讲的实践都依赖 `cuda-tile-opt`，若尚未构建，先按 [u1-l2](u1-l2-repo-and-build.md) 配置 `CUDA_TILE_ENABLE_TESTING=ON` 的 Release 构建。
