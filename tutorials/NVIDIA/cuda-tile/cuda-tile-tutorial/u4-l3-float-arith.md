# 浮点算术与 FMA

## 1. 本讲目标

本讲聚焦 `cuda_tile` 方言 **Floating Point（浮点）分组**的操作。学完后你应该能够：

- 说出 Floating Point 分组包含哪些操作、它们能接受哪些浮点元素类型。
- 解释 `RoundingModeAttr` 的七个取值，并知道 `addf`/`mulf`/`divf`/`exp`/`sqrt`/`tanh` 各自允许哪些舍入模式、默认值是什么。
- 理解 `flush_to_zero`（FTZ）修饰符为什么只对 `f32` 有效。
- 区分「分离的 `mulf` + `addf`」与单个 `fma` 在数值精度上的差别，并能说明 `fma` 在数学上做了什么。
- 写出一段合法的浮点运算 MLIR，并能用 `cuda-tile-opt` 触发并阅读舍入模式与 FTZ 的验证报错。

本讲承接 u4-l2（整数算术）。整数那一讲建立的「舍入模式 / 修饰符」心智模型在这里会升级为「IEEE 舍入 + 近似/全精度 + flush_to_zero」三套修饰符。

## 2. 前置知识

在进入浮点操作前，先回顾几个关键概念（来自前面几讲）：

- **Tile 类型**：`tile<4x8xf32>` 是落在寄存器里、编译期形状完全确定的小矩阵（见 u3-l1）。浮点算术操作逐元素作用于整个 Tile。
- **元素类型分层**：`cuda_tile` 把浮点类型分成两档约束：
  - `CudaTile_BaseFloatTileType` = `f16` / `bf16` / `f32` / `f64`，即「基础浮点」。
  - `CudaTile_FloatTileType` 在基础浮点之外，还包含 `tf32`、各种 `fp8`、`fp4` 等低精度类型。
  多数浮点算术操作只接受「基础浮点」；低精度类型通常走张量核 MMA（见 u4-l5）或显式转换（见 u4-l4）。
- **`Pure` trait**：标记操作没有副作用、可被自由移动/消除/合并。本讲的算术操作大多带 `Pure`。
- **TableGen 操作定义**：每个操作都派生自一个分组基类（见 u2-l2），Floating Point 分组的基类是 `CudaTileFloatingPointOpDef`。
- **修饰符（modifier）**：写在操作助记符与 `:` 之间的小关键字，例如 `rounding<nearest_even>`、`flush_to_zero`、`propagate_nan`。它们是附加在操作上的属性，不影响操作「算什么」，但影响「怎么算」。

一个最小的浮点加法长这样：

```mlir
// cuda_tile.module 内省略了 cuda_tile. 前缀（见 u2-l2 的 CudaTile_DefaultDialect）
%z = addf %x, %y : tile<4xf32>
```

不加任何修饰符时，它使用默认舍入模式（`nearest_even`）。本讲要回答的核心问题是：**这些修饰符有哪些、谁能用、谁来检查合法性。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 所有操作的 TableGen 定义，包括 `addf`/`subf`/`mulf`/`divf`/`remf`/`maxf`/`minf`/`fma` 等 |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | 分组基类 `CudaTileFloatingPointOpDef` 的定义 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | `RoundingMode` 枚举与 `RoundingModeAttr` 属性定义 |
| [include/cuda_tile/Dialect/CudaTile/IR/Types.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td) | `BaseFloatTileType` / `FloatTileType` 等类型约束 |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | 操作的 C++ 验证器（`AddFOp::verify` 等）与舍入模式的自定义解析/打印 |
| [include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h) | `verifyFtz` / `verifyApprox` / `verifyDivFPModifiers` 等共享校验模板 |
| [test/Dialect/CudaTile/math_invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/math_invalid.mlir) | 浮点（含超越函数）非法用例集，是本讲实践的主要参照 |
| [test/Transforms/fuse-fma.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir) | `mulf`+`addf` 与 `fma` 的合法写法对照（FuseFMA 测试） |

## 4. 核心概念与源码讲解

### 4.1 Floating Point 分组全景与基础浮点类型

#### 4.1.1 概念说明

Floating Point 分组是 `cuda_tile` 方言十一个操作分组之一。它的基类把操作归入 `"Floating Point"` 这个 `group`（详见 u2-l2 的 `CudaTileOpDef` 元数据），用于规范文档分节和字节码分组。

分组里的操作可以分成三类：

1. **算术运算**：`addf`、`subf`、`mulf`、`divf`、`remf`、`maxf`、`minf`、`absf`、`negf`。
2. **融合乘加**：`fma`。
3. **超越/初等函数**：`exp`、`exp2`、`log`、`log2`、`sin`、`cos`、`tan`、`sinh`、`cosh`、`tanh`、`sqrt`、`rsqrt`、`pow`、`atan2`、`ceil`、`floor`。（超越函数详见 u4-l4，本讲只在讲到舍入模式时涉及它们。）

一个非常重要的约束是：**算术运算只接受「基础浮点」`f16/bf16/f32/f64`，不接受 `tf32/fp8/fp4`。** 这一点从类型约束 `CudaTile_BaseFloatTileType` 就能看出：

[include/cuda_tile/Dialect/CudaTile/IR/Types.td:801-803](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td#L801-L803) —— 定义「基础浮点 Tile」约束，仅含 `f16/bf16/f32/f64` 四种元素类型。

`addf`、`subf`、`mulf`、`divf`、`remf`、`maxf`、`minf` 的两个输入与一个结果都用这个约束。`math_invalid.mlir` 里大量用例（如对 `f8E5M2` 做 `absf`/`ceil`/`sin`）报的 `must be tile of f16 or bf16 or f32 or f64 values` 错误，就是这条约束触发的。

#### 4.1.2 核心流程

一个 Floating Point 算术操作的生命周期：

1. **解析（parse）**：自定义解析器读取可选的 `rounding<...>` 与 `flush_to_zero`，按操作各自允许的取值集合校验。
2. **类型校验**：TableGen 生成的 `AllTypesMatch` 约束检查 `lhs`/`rhs`/`result` 类型一致；元素类型必须落在 `BaseFloatTileType`。
3. **操作验证（verify）**：每个操作写了 `let hasVerifier = 1;`，其 C++ `verify()` 再次检查舍入模式是否合法、`flush_to_zero` 是否只用于 `f32`。
4. **打印（print）**：当舍入模式为默认值时省略不打印，保持文本简洁。

#### 4.1.3 源码精读

分组基类非常简单——它只负责打上 `"Floating Point"` 分组标签：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:150-152](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L150-L152) —— `CudaTileFloatingPointOpDef` 基类，把操作归入 `"Floating Point"` 分组。

以 `addf` 为例看一个算术操作的完整定义。注意四个要点：trait 里的 `AllTypesMatch<["lhs","rhs","result"]>` 强制三者同型；参数表里除了 `lhs`/`rhs` 还多了 `rounding_mode` 与 `flush_to_zero` 两个属性参数；汇编格式用 `custom<IEEERoundingMode>(...)` 解析舍入模式、用 `(`flush_to_zero` $flush_to_zero^)?` 可选地解析 FTZ：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:146-186](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L146-L186) —— `CudaTile_AddFOp` 完整定义，含修饰符表、参数表与汇编格式。

> 说明：`addf` 的描述里嵌入了数学公式 `\text{addf}(x, y)_i = x_i + y_i`。本讲统一用「逐元素」语义，即对 Tile 中每个对应位置的元素独立做运算，输出同形状 Tile。

`Ops.td` 顶部还集中定义了一组公共描述串，便于复用，其中两条与本讲直接相关：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:36-40](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L36-L40) —— `flush_to_zero_desc`（FTZ 把次正规数冲为零）与 `rounding_mode_desc`（操作的舍入模式）的公共描述。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手确认「哪些浮点类型被算术操作接受」。

1. **实践目标**：用 `math_invalid.mlir` 里的非法用例确认 `BaseFloatTileType` 的边界。
2. **操作步骤**：打开 `test/Dialect/CudaTile/math_invalid.mlir`，定位 `@absf_invalid_f8_element` 用例（约第 61–66 行）。
3. **需要观察的现象**：该用例对 `tile<2x4x8xf8E5M2>` 调用 `absf`，`// expected-error` 注释里写明期望报错 `op operand #0 must be tile of f16 or bf16 or f32 or f64 values`。
4. **预期结果**：可见「基础浮点」之外的类型（此处是 `fp8`）一律被算术操作拒绝；若想用 `fp8` 做运算，只能走 u4-l5 的 MMA 或 u4-l4 的显式转换。
5. 运行命令与具体输出：**待本地验证**（需先按 u1-l2 构建，再执行 `cuda-tile-opt test/Dialect/CudaTile/math_invalid.mlir -verify-diagnostics -split-input-file`）。

#### 4.1.5 小练习与答案

**练习 1**：`tf32` 能否作为 `mulf` 的元素类型？为什么？

> **答案**：不能。`mulf` 的输入约束是 `CudaTile_BaseFloatTileType`，只含 `f16/bf16/f32/f64`，不含 `tf32`。`tf32` 主要用于张量核 MMA（见 u4-l5）。

**练习 2**：`absf` 的输入约束和 `addf` 一样吗？它有没有 `rounding_mode` 参数？

> **答案**：元素约束一样（都是 `BaseFloatTileType`），但 `absf` 取绝对值不涉及舍入，因此**没有** `rounding_mode` 参数，汇编格式只有 `$source attr-dict : ...`。

---

### 4.2 RoundingModeAttr：舍入模式属性

#### 4.2.1 概念说明

浮点运算的结果往往不能精确落在目标类型上，必须「舍入」。`cuda_tile` 用一个统一的枚举属性 `RoundingModeAttr` 描述舍入方式，但**不同的操作只允许其中的一个子集**。理解这套属性的关键是分清三层：

- **枚举全集**：`RoundingMode` 枚举本身定义了 7 个值。
- **操作允许集**：每个操作在解析与验证时只接受其中一部分。
- **默认值**：省略修饰符时各操作补上的值不同。

#### 4.2.2 核心流程

`RoundingMode` 枚举的 7 个取值与语义如下表（顺序与源码一致）：

| 枚举值 | 文本拼写 | 语义 |
|--------|----------|------|
| `NEAREST_EVEN` | `nearest_even` | 就近舍入，平局取偶（IEEE 754 默认） |
| `ZERO` | `zero` | 向零舍入（截断） |
| `NEGATIVE_INF` | `negative_inf` | 向负无穷舍入（向下取整） |
| `POSITIVE_INF` | `positive_inf` | 向正无穷舍入（向上取整） |
| `APPROX` | `approx` | 快速近似（如除法乘以倒数、超越函数近似） |
| `FULL` | `full` | 全精度（相对快但非完全 IEEE 754 兼容） |
| `NEAREST_INT_TO_ZERO` | `nearest_int_to_zero` | 整数舍入专用，仅 `ftoi` 用（见 u4-l4） |

前四个统称 **IEEE 舍入模式**，是算术运算的主力；`approx`/`full` 是「精度换速度」的快速模式；`nearest_int_to_zero` 是浮点转整数专用，不在本讲范围。

不同操作允许的集合与默认值（本讲只列与舍入相关的）：

| 操作 | 允许的舍入模式 | 默认值（省略时） |
|------|----------------|------------------|
| `addf` / `subf` / `mulf` / `fma` | 4 个 IEEE | `nearest_even` |
| `divf` | 4 个 IEEE + `approx` + `full` | `nearest_even` |
| `sqrt` | 4 个 IEEE + `approx` | `nearest_even` |
| `exp` | `approx` + `full` | `full` |
| `tanh` | `approx` + `full` | `full` |

一个贯穿全表的规律：**打印时若舍入模式等于默认值就省略**。所以 `%z = addf %x, %y : tile<4xf32>` 等价于显式写 `rounding<nearest_even>`。

#### 4.2.3 源码精读

枚举定义在 `AttrDefs.td`，每个 `CudaTileI32EnumAttrCase` 给出描述、C++ 枚举名、整数值、引入版本和文本拼写：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:193-209](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L193-L209) —— `CudaTile_RoundingMode` 枚举，定义 7 个舍入取值（4 IEEE + approx + full + nearest_int_to_zero）。

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:211-213](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L211-L213) —— `CudaTile_RoundingModeAttr` 属性，汇编格式为 `rounding<value>`。

「操作允许集」与「默认值」由 C++ 侧的自定义解析器实现。`addf`/`subf`/`mulf`/`fma` 都用 `parseIEEERoundingMode`：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:913-932](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L913-L932) —— `parseIEEERoundingMode`：只允许 4 个 IEEE 模式，默认补 `NEAREST_EVEN`。

而 `divf` 用一个允许 `approx`/`full` 的更宽解析器：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:773-778](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L773-L778) —— `parseDivFOpRoundingMode`：在 4 个 IEEE 基础上额外允许 `approx` 和 `full`。

两个解析器背后都调用统一的 `parseRoundingModeWithModes`，它的核心逻辑是「能解析 `rounding<关键字>` 就解析，否则用传入的默认值」：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:834-885](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L834-L885) —— `parseRoundingModeWithModes`：统一处理 `rounding<...>` 的解析、符号化、校验与默认值回填。

除了**解析期**校验，每个算术操作还有一个**验证期**校验（`hasVerifier = 1`）。`addf`/`mulf`/`subf`/`fma` 共用模板 `verifyIEEERoundingModes`，它再次确认取值属于 4 个 IEEE 模式之一：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1408-1420](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1408-L1420) —— `verifyIEEERoundingModes` 模板：取值不在 4 个 IEEE 模式内则报错。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1422-1426](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1422-L1426) —— `AddFOp::verify`：先校验 IEEE 舍入，再校验 FTZ。

> 这意味着即使你用泛型语法 `"cuda_tile.addf"(...) <{rounding_mode = ...}>` 绕过自定义解析器，验证器仍会兜底拒绝非法取值。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发一次「非法舍入模式」报错，并对照两种触发路径。
2. **操作步骤**：
   - 路径 A（解析期）：写 `%0 = addf %a, %b rounding<approx> : tile<4xf32>`。
   - 路径 B（验证期，泛型语法）：写 `%0 = "cuda_tile.addf"(%a, %b) <{rounding_mode = #cuda_tile.rounding<full>}> : (tile<4xf32>, tile<4xf32>) -> tile<4xf32>`。
3. **需要观察的现象**：路径 A 报「expected rounding mode to be one of: 'nearest_even' ...」；路径 B 报「invalid rounding mode specified, expect one of [nearest_even, zero, negative_inf, positive_inf]」。
4. **预期结果**：`approx`/`full` 是 `divf`/`sqrt`/`exp` 等的专用值，对 `addf` 在解析与验证两层都会被拒。
5. 具体输出：**待本地验证**。可参照 `math_invalid.mlir` 中 `@sqrt_invalid_rounding_mode__f16_element`（拼写错误 `pippo`）与 `@sqrt_invalid_rnd_modifier`（`sqrt` 上误用 `full`）的写法。

#### 4.2.5 小练习与答案

**练习 1**：省略 `mulf` 的 `rounding` 修饰符，等价于哪个显式写法？为什么打印时会看不到它？

> **答案**：等价于 `rounding<nearest_even>`。因为 `parseIEEERoundingMode` 的默认值是 `NEAREST_EVEN`，而打印机在取值等于默认值时省略不打印（见 `fuse-fma.mlir` 第 36 行的合法 `mulf` 就没有写修饰符）。

**练习 2**：为什么 `divf` 能用 `rounding<approx>` 而 `addf` 不能？

> **答案**：二者绑定的解析器不同。`divf` 用 `parseDivFOpRoundingMode`，允许集含 `approx`/`full`；`addf` 用 `parseIEEERoundingMode`，只允许 4 个 IEEE 模式。从硬件角度，近似除法（乘以倒数）是常见快速指令，而加法本身没有「近似」指令。

---

### 4.3 flush_to_zero 修饰符与 f32 专用约束

#### 4.3.1 概念说明

`flush_to_zero`（常缩写 **FTZ**）是一个布尔修饰符。设上后，操作的次正规（subnormal / denormal）输入与结果会被冲成「保持符号的零」。FTZ 在 GPU 上对应硬件的 FTZ 模式，可避免处理次正规数带来的显著性能损失。

`cuda_tile` 对 FTZ 有一条硬约束：**只允许 `f32`。** 对 `f16`/`bf16`/`f64` 都不允许。这是因为只有 `f32` 路径在目标硬件上有对应的 FTZ 行为。

哪些操作支持 FTZ？从 `Ops.td` 的修饰符表可以归纳：算术里的 `addf`/`subf`/`mulf`/`divf`/`maxf`/`minf`/`fma`，以及部分超越函数 `exp2`/`rsqrt`（`exp`/`sqrt` 等通过各自的 `verify` 也走 FTZ 校验）。

#### 4.3.2 核心流程

FTZ 的检查只有一条规则，但在多处复用：

1. 操作结果类型取出元素类型。
2. 若 FTZ 为真且元素类型不是 `f32`，报 `flush_to_zero modifier only supported for f32 data type, but got: '<type>'`。

这条逻辑被提炼成一个共享模板 `verifyFtz`，所有带 FTZ 的操作都调用它。

#### 4.3.3 源码精读

FTZ 校验模板就两行核心判断：

[include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h:95-107](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L95-L107) —— `verifyFtz`：FTZ 为真且元素类型非 `f32` 时报错。

`AddFOp::verify`（前文 4.2.3 已链接）在舍入校验之后立刻调用 `verifyFtz(*this, getFlushToZero())`。`MaxFOp`、`MinFOp`、`RsqrtOp`、`Exp2Op`、`SubFOp`、`FmaOp` 的 `verify` 同样以 `return verifyFtz(...)` 收尾。

`Ops.td` 里 FTZ 在汇编格式中是一个可选单元属性 `(`flush_to_zero` $flush_to_zero^)?`，例如 `mulf`：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3447-3452](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3447-L3452) —— `mulf` 的汇编格式：`$lhs, $rhs` 后跟可选舍入模式与可选 `flush_to_zero`。

`divf`/`sqrt` 等带 `approx` 的操作还共用一个更强的共享校验，它把 FTZ、approx、full 三者的 `f32` 约束以及互斥关系一并检查：

[include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h:121-152](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L121-L152) —— `verifyDivSqrtCommonFPModifiers`：approx/full/FTZ 互斥且都仅限 `f32`，且不能与 IEEE 舍入并存。

#### 4.3.4 代码实践

1. **实践目标**：复现「FTZ 只对 f32 有效」的报错。
2. **操作步骤**：参照 `math_invalid.mlir` 中 `@exp2_invalid_ftz_dtype`（约第 254–259 行）和 `@rsqrt_invalid_f64_element`（约第 585–590 行），写 `exp2 %arg flush_to_zero : tile<2x4x8xf16>` 或 `rsqrt %arg flush_to_zero : tile<4xf64>`。
3. **需要观察的现象**：终端打印 `flush_to_zero modifier only supported for f32 data type, but got: 'f16'`（或 `'f64'`）。
4. **预期结果**：把同一操作改成 `f32` 即可验证通过。
5. 具体输出：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`addf %a, %b flush_to_zero : tile<4xf64>` 合法吗？为什么？

> **答案**：不合法。FTZ 仅对 `f32` 有效，`verifyFtz` 检测到 `f64` 即报错。`Ops.td` 的 `addf` 修饰符表里 `f64` 列对 `flush_to_zero` 也写的是 `no`。

**练习 2**：能否对 `divf` 同时写 `rounding<nearest_even>` 和 `approx`？

> **答案**：不能。`approx` 本身就是 `divf` 的一种「舍入模式」取值（写在 `rounding<approx>` 里），而不是独立修饰符；且 `verifyDivSqrtCommonFPModifiers` 会拒绝「IEEE 舍入与 approx 并存」。`divf` 一次只能选一种舍入行为。

---

### 4.4 四则运算与 remf / maxf / minf

#### 4.4.1 概念说明

把舍入与 FTZ 两个修饰符弄清楚后，四则运算本身就很直观了——它们都是逐元素运算，输入输出同型：

| 操作 | 数学定义 | 备注 |
|------|----------|------|
| `addf` | \(\text{addf}(x,y)_i = x_i + y_i\) | 支持 4 IEEE 舍入 + FTZ |
| `subf` | \(\text{subf}(x,y)_i = x_i - y_i\) | 同上 |
| `mulf` | \(\text{mulf}(x,y)_i = x_i \times y_i\) | 同上 |
| `divf` | \(\text{divf}(x,y)_i = x_i / y_i\) | 额外支持 `approx`/`full` |
| `remf` | \(\text{remf}(x,y)_i = x_i - \text{trunc}(x_i/y_i)\times y_i\) | 截断取余，**无舍入/FTZ 参数** |
| `maxf` | 取较大值 | 有 `propagate_nan` 与 FTZ |
| `minf` | 取较小值 | 同 `maxf` |

几个要点：

- `divf` 的 `approx` 是「乘以倒数」的快速近似（在正规范围内 ULP 误差上界为 2）；`full` 是「缩放后近似」、全范围 ULP 误差 2 但非完全 IEEE 兼容。两者都仅限 `f32`。
- `remf` 用截断除法取余，结果符号跟随被除数 `lhs`，**不接受**舍入模式或 FTZ——它的语义本身已经固定为「向零截断」。
- `maxf`/`minf` 多了一个 `propagate_nan` 修饰符，控制 NaN 传播行为（IEEE 754-2019 的 `maximum` vs `maximumNumber`），并支持 FTZ。

#### 4.4.2 核心流程

以 `divf` 为例，验证流程比 `addf` 多一步：先判断舍入是否属于扩展集（含 `approx`/`full`），再调用共享的 `verifyDivFPModifiers` 检查 FTZ/approx/full 的 `f32` 约束与互斥关系。

#### 4.4.3 源码精读

`divf` 的描述清楚说明了 `approx` 与 `full` 的精度含义：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1362-1410](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1362-L1410) —— `CudaTile_DivFOp`：描述 `approx`（乘倒数，ULP≤2）与 `full`（缩放近似，全范围 ULP≤2）两种快速模式。

`DivFOp::verify` 把允许集扩展到 6 个，再分派给共享校验：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1987-2003](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1987-L2003) —— `DivFOp::verify`：允许 4 IEEE + approx + full，并校验修饰符兼容性。

`remf` 与四则运算形成对照——它没有舍入/FTZ 参数，汇编格式只有 `$lhs, $rhs attr-dict : ...`：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:5077-5104](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L5077-L5104) —— `CudaTile_RemFOp`：截断取余，含特殊情形（除零返回 NaN 等），无舍入参数。

`maxf` 则展示了 `propagate_nan` 与 FTZ 两个修饰符的组合：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3120-3176](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3120-L3176) —— `CudaTile_MaxFOp`：参数含 `propagate_nan` 与 `flush_to_zero`，mlirExamples 给出三种写法对照。

#### 4.4.4 代码实践

1. **实践目标**：写出全部四则运算与 `remf` 的合法 MLIR，并对比 `divf` 三种舍入写法。
2. **操作步骤**：在 `cuda_tile.module` 内写一个 `testing$func`，对两个 `tile<4xf32>` 依次做 `addf`/`subf`/`mulf`/`divf`/`remf`；`divf` 分别用 `rounding<nearest_even>`、`rounding<approx>`、`rounding<full>` 三种。
3. **需要观察的现象**：用 `cuda-tile-opt %s` round-trip 打印，确认默认舍入被省略、`approx`/`full` 被保留。
4. **预期结果**：全部通过验证；若把任一 `divf` 的元素类型改成 `f64` 并保留 `approx`，则报 `approx modifier only supported for f32 data type`。
5. 具体输出：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`remf` 为什么没有 `rounding_mode` 参数？

> **答案**：`remf` 的语义已固定为「向零截断除法后取余」（\(\text{trunc}(x/y)\)），不存在其他舍入选择，因此参数表里没有 `rounding_mode`，汇编格式也不解析它。

**练习 2**：`maxf %a, %b` 与 `maxf %a, %b propagate_nan` 在「一个操作数为 NaN」时的结果有何不同？

> **答案**：不带 `propagate_nan` 时遵循 IEEE 754-2019 `maximumNumber`——仅当两个数都是 NaN 才返回 NaN，否则返回非 NaN 的那个；带 `propagate_nan` 时遵循 `maximum`——只要有一个是 NaN 就返回 NaN。

---

### 4.5 fma：融合乘加与精度差异

#### 4.5.1 概念说明

`fma`（fused multiply-add）一次完成「乘了再加」：

\[ \text{fma}(x, y, z)_i = x_i \times y_i + z_i \]

它接受三个同型操作数 `lhs`、`rhs`、`acc`，返回 `lhs * rhs + acc`。关键在于「融合」（fused）：**中间乘积不单独舍入**，而是在扩展精度下与 `acc` 相加后，对最终结果做**一次**舍入。

这与「分离的 `mulf` + `addf`」形成对照：

- 分离写法 `t = mulf(x,y); r = addf(t,z)`：先对乘积 `x*y` 舍入一次，再对和舍入一次，共**两次**舍入。
- 融合写法 `r = fma(x,y,z)`：只舍入**一次**。

数学上，`fma` 给出的是 `x*y+z` 的**正确舍入结果**（correctly rounded），精度通常**更高**。但这同时意味着：**`fma(x,y,z)` 一般不等于 `addf(mulf(x,y),z)`**。这是一个「非数值保持」的变换——u9-l1 的 FuseFMA Pass 正是利用这一点把 `mulf`+`addf` 合并成 `fma` 以提速，并明确声明它改变数值结果。

#### 4.5.2 核心流程

`fma` 的修饰符集合与 `addf` 一致：4 个 IEEE 舍入模式，默认 `nearest_even`，可选 FTZ（仅 `f32`）。从 `Ops.td` 的修饰符表看，`f16`/`bf16` 在非 `nearest_even` 模式下「在 f32 中模拟」（表中带 `*`），而 `nearest_even` 对四种基础浮点都是原生支持。

`fma` 的类型约束是 `CudaTile_FmaTile`，和 `BaseFloatTileType` 一样限定在 `f16/bf16/f32/f64`：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2055-2058](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2055-L2058) —— `CudaTile_FmaTile`：限定 `f16/bf16/f32/f64` 四种基础浮点。

精度差异的直觉（以 `f32` 为例）：两个 24 位有效位的 `f32` 相乘，乘积有约 48 位有效位。分离 `mulf` 会把它截断回 24 位，丢失低位信息；`fma` 则保留完整乘积、与 `acc` 相加后只舍入一次，因此能保住那些会被截断丢掉的位。

#### 4.5.3 源码精读

`fma` 的定义与 `addf` 几乎同构，差别只在多一个 `$acc` 操作数：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2060-2097](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2060-L2097) —— `CudaTile_FmaOp`：三操作数 `lhs,rhs,acc`，含数学公式与修饰符表。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:2420-2424](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2420-L2424) —— `FmaOp::verify`：与 `addf` 一样，校验 IEEE 舍入 + FTZ。

合法写法的最佳参照是 FuseFMA 的测试，它同时给出「分离」输入与「融合」期望输出：

[test/Transforms/fuse-fma.mlir:9-20](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L9-L20) —— 用 `mulf`+`addf` 表达 `x*y+z`，FuseFMA 期望把它改写为单个 `fma`。

注意该文件第 15–16 行的写法：

```mlir
%3 = cuda_tile.mulf %0, %1 rounding<nearest_even> : !cuda_tile.tile<f32>
%4 = cuda_tile.addf %3, %2 rounding<nearest_even> : !cuda_tile.tile<f32>
```

而第 36 行的 `mulf` 省略了 `rounding`，说明默认 `nearest_even` 可不写。等价的 `fma` 直接写法是：

```mlir
// 示例代码：手工写 fma，等价于上面的 mulf+addf（但数值上可能不同）
%4 = fma %0, %1, %2 : !cuda_tile.tile<f32>
```

#### 4.5.4 代码实践

1. **实践目标**：分别用「`mulf` + `addf`」与单个「`fma`」实现 `a*b+c`，并对比舍入模式取 `nearest_even` 与（`divf` 才有的）`approx` 时的写法差异。
2. **操作步骤**：
   - 写法一（分离）：`%t = mulf %a, %b rounding<nearest_even> : tile<f32>` 然后 `%r = addf %t, %c rounding<nearest_even> : tile<f32>`。
   - 写法二（融合）：`%r = fma %a, %b, %c rounding<nearest_even> : tile<f32>`。
   - 尝试给 `addf`/`fma` 传入 `rounding<approx>`（这是 `divf` 专用值），观察报错。
3. **需要观察的现象**：写法一、二都能通过验证；对 `addf`/`fma` 用 `approx` 会被拒绝（解析期或验证期报错）。
4. **预期结果**：`fma` 是「正确舍入」的 `a*b+c`，分离写法因两次舍入一般与 `fma` 数值不等；这就是 u9-l1 FuseFMA「非数值保持」的根源。
5. 具体输出：**待本地验证**。可用 `cuda-tile-opt --fuse-fma`（见 u9-l1）确认分离写法会被改写为 `fma`。

#### 4.5.5 小练习与答案

**练习 1**：从舍入次数角度，说明为什么 `fma(a,b,c)` 通常比 `addf(mulf(a,b),c)` 更精确。

> **答案**：分离写法对乘积 `a*b` 舍入一次（截断回元素类型位宽），再对求和结果舍入一次，共两次，中间丢了低位；`fma` 在扩展精度下计算 `a*b+c` 后只舍入一次，保住了会被截断的位，因而给出 `a*b+c` 的正确舍入结果。

**练习 2**：能否写 `fma %a, %b, %c flush_to_zero : tile<4xf16>`？为什么？

> **答案**：不能。FTZ 仅对 `f32` 有效；`FmaOp::verify` 调用 `verifyFtz`，对 `f16` 会报 `flush_to_zero modifier only supported for f32 data type`。

**练习 3**：`fma` 的三个操作数类型可以不同吗？

> **答案**：不能。`fma` 带 `AllTypesMatch<["lhs","rhs","acc","result"]>` 约束，四个类型必须完全相同，且都落在 `CudaTile_FmaTile`（`f16/bf16/f32/f64`）内。

## 5. 综合实践

把本讲内容串起来，完成下面这个「浮点运算小内核」：

**任务**：在 `cuda_tile.module` 内编写一个 `testing$func`，接收三个 `tile<4xf32>` 参数 `a,b,c`，完成：

1. 用分离写法计算 `t1 = a*b + c`（`mulf` + `addf`，舍入 `nearest_even`，开启 FTZ）。
2. 用融合写法计算 `t2 = fma(a,b,c)`（舍入 `nearest_even`，开启 FTZ）。
3. 用 `divf` 的 `approx` 模式计算 `t3 = a / b`。
4. 用 `maxf` 计算 `t4 = max(t1, t2)`（带 `propagate_nan`）。

要求：

- 用 `cuda-tile-opt %s` 做 round-trip 验证全部合法。
- 故意把第 3 步的元素类型改成 `f64` 再跑一次，确认 `approx` 仅限 `f32` 的报错。
- 用 `cuda-tile-opt --pass-pipeline='builtin.module(cuda_tile.module(cuda_tile.testing$func(fuse-fma)))'` 跑第 1 步，观察 `mulf`+`addf` 是否被改写为 `fma`，并思考：改写后 `t1` 与手写的 `t2` 在数值上是否一定相同？为什么？

参考写法（**示例代码**，需放入合法的 `module`/`func` 包裹中，参照 `fuse-fma.mlir` 的结构）：

```mlir
// 示例代码：仅展示操作行，省略 module/func 包裹与 constant 构造
%ab  = mulf %a, %b rounding<nearest_even> flush_to_zero : tile<4xf32>
%t1  = addf %ab, %c rounding<nearest_even> flush_to_zero : tile<4xf32>
%t2  = fma %a, %b, %c rounding<nearest_even> flush_to_zero : tile<4xf32>
%t3  = divf %a, %b rounding<approx> : tile<4xf32>
%t4  = maxf %t1, %t2 propagate_nan : tile<4xf32>
```

思考题提示：FuseFMA 把分离 `mulf`+`addf` 改写为 `fma` 后，`t1` 变成「一次舍入」语义，与原来「两次舍入」不同；而手写的 `t2` 本就是一次舍入。所以改写后 `t1` 与 `t2` 在数学语义上一致（都是正确舍入的 `a*b+c`），但 `t1` 相对改写前的自己数值可能变化。这正是 FuseFMA「非数值保持」的含义（详见 u9-l1）。

## 6. 本讲小结

- Floating Point 分组的算术操作（`addf`/`subf`/`mulf`/`divf`/`remf`/`maxf`/`minf`/`fma`）只接受「基础浮点」`f16/bf16/f32/f64`，低精度类型需走 MMA 或显式转换。
- 舍入由统一的 `RoundingModeAttr` 描述（7 个取值），但每个操作只允许一个子集：`addf`/`mulf`/`subf`/`fma` 仅 4 个 IEEE 模式；`divf` 额外允许 `approx`/`full`；`exp`/`tanh` 只允许 `approx`/`full`。默认值各异，打印时省略默认值。
- `flush_to_zero` 是仅对 `f32` 有效的修饰符，由共享模板 `verifyFtz` 在验证期统一强制。
- `divf` 的 `approx`/`full`、`sqrt` 的 `approx` 也都仅限 `f32`，且不能与 IEEE 舍入并存（`verifyDivSqrtCommonFPModifiers`）。
- `fma` 是「融合乘加」，只舍入一次，给出 `a*b+c` 的正确舍入结果；它一般不等于分离的 `mulf`+`addf`（两次舍入），这是 u9-l1 FuseFMA「非数值保持」的根源。

## 7. 下一步学习建议

- **u4-l4（超越函数与类型转换）**：本讲多次提到 `exp`/`sqrt`/`tanh` 的舍入规则，下一讲会完整覆盖这些超越函数，以及 `ftof`/`ftoi`/`itof` 等跨类型转换操作（其中 `ftoi` 用到本讲的 `nearest_int_to_zero` 模式）。
- **u4-l5（张量核 MMA）**：本讲的 `fma` 是「标量/Tile 级」融合乘加；当输入是低精度类型（`fp8`/`tf32`）时，矩阵乘累加走 `mmaf`/`mmai`/`mmaf_scaled`，那是另一套硬件路径。
- **u9-l1（FuseFMA）**：想看 `mulf`+`addf` 如何被自动改写为 `fma`、以及为什么它是「非数值保持」的，直接阅读 `lib/Dialect/CudaTile/Transforms/FuseFMA.cpp` 与 `test/Transforms/fuse-fma.mlir`。
- 想巩固舍入模式的心智模型，建议再通读一遍 `math_invalid.mlir`，把每条 `expected-error` 与本讲对应到具体的解析器/验证器函数。
