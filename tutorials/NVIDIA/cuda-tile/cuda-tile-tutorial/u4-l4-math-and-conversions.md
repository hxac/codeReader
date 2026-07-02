# 超越函数与类型转换操作

> 适用版本：CUDA Tile IR 13.3（依赖锁定到 LLVM commit `e01244d`，HEAD `e01244d89cd38e81dde50d60fbfee07ac6d7be22`）

## 1. 本讲目标

本讲承接 u4-l3（浮点算术与 FMA），把 `cuda_tile` 方言里两类「把一个 tile 变成另一个 tile」的操作讲透：

- 一类是**超越函数与初等函数**（`exp`/`log`/`sin`/`cos`/`sqrt`/`rsqrt`/`pow`/`atan2` 等），它们对一个浮点 tile 做逐元素的非线性数学运算；
- 另一类是 **Conversions 分组**的类型转换操作（`bitcast`/`ftof`/`ftoi`/`itof`/`exti`/`trunci`/`int_to_ptr`/`ptr_to_int`/`ptr_to_ptr`），它们改变 tile 的元素类型。

学完本讲，你应当能够：

1. 说出超越函数操作支持哪些元素类型、`approx`/`full` 两种舍入模式的差异，以及「半精度在 f32 下模拟」对精度的含义。
2. 准确区分**位保持**（bitcast）与**数值保持**（ftof/ftoi/itof）两类转换的本质差别——前者不改二进制位、后者会改。
3. 解释整数位宽变换 `exti`/`trunci` 的有符号扩展/零扩展语义，以及 `int_to_ptr`/`ptr_to_int`/`ptr_to_ptr` 三者各自的约束。
4. 读懂 `test/Dialect/CudaTile/conversion.mlir` 与 `conversion_invalid.mlir`，并能据此写出合法或触发校验报错的 MLIR。

## 2. 前置知识

在进入正文前，确认你理解这几个来自前序讲义的概念：

- **Tile 与元素类型**（u3-l1）：`tile<4x8xf32>` 是「静态形状 + 元素类型」的小矩阵，落在寄存器里。本讲所有操作的输入输出都是 Tile。
- **基础浮点 vs 低精度浮点**（u4-l3）：`f16/bf16/f32/f64` 是「基础浮点」（类型约束 `CudaTile_BaseFloatTileType`），算术操作只认它们；`fp8/fp4/tf32` 属低精度，本讲的超越函数**不接受**它们直接作输入。
- **舍入模式 `RoundingModeAttr`**（u4-l3）：七取一——四个 IEEE 模式 `nearest_even/zero/negative_inf/positive_inf`、`approx`、`full`、整数用的 `nearest_int_to_zero`。本讲的 `exp`/`sqrt`/`ftof`/`ftoi` 等都会引用它。
- **操作分组基类**（u2-l2）：每个操作通过 `CudaTileFloatingPointOpDef` 或 `CudaTileConversionOpDef` 归入某个分组，并被 `group/subGroup/sinceVersion` 元数据标注。本讲涉及 Floating Point 与 Conversions 两个分组。

一句话回顾：上一讲我们让浮点 tile 做 `+ - * /` 与融合乘加 `fma`；本讲是「让浮点 tile 做高级数学运算」与「让 tile 换一种元素类型」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 操作的「单一数据源」声明。本讲的 `exp/sqrt/rsqrt/log/sin/cos/pow/atan2` 与 `bitcast/ftof/ftoi/itof/exti/trunci/int_to_ptr/ptr_to_int/ptr_to_ptr` 全部定义于此。 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | `RoundingMode`、`Signedness`、`IntegerOverflow` 等枚举属性的定义。 |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | 各转换操作的手写 `verify()` 逻辑（位宽比较、舍入模式校验等）。 |
| [test/Dialect/CudaTile/conversion.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion.mlir) | 转换操作的合法用例总览，按 `bitcast/ftof/ftoi/itof/trunci/exti` 分块。 |
| [test/Dialect/CudaTile/conversion_invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion_invalid.mlir) | 各种非法转换与对应的 `expected-error` 断言，是理解校验规则的最佳入口。 |

## 4. 核心概念与源码讲解

### 4.1 超越函数与初等函数操作

#### 4.1.1 概念说明

「超越函数（transcendental function）」指不能用有限次加减乘除表达的非线性函数，如 `sin/cos/exp/log/pow`；「初等函数」则把 `sqrt/rsqrt`、`floor/ceil` 等也纳入。在 `cuda_tile` 方言里，它们都派生自 `CudaTileFloatingPointOpDef`（归入 Floating Point 分组），输入输出都用 `CudaTile_BaseFloatTileType`，即只接受基础浮点 `f16/bf16/f32/f64`。

这一类操作有两个共性，都来自一个共享的描述片段 `floating_point_math_suffix`：

> This operation is emulated in `f32` when executed on half-precision inputs (`f16` and `bf16`).

也就是说，**当输入是 `f16`/`bf16` 时，这些数学运算会在 `f32` 下模拟（emulate）执行**，而非用硬件的半精度数学指令。这是本模块最重要的精度结论：你在 `bf16` 上算 `exp`，实际得到的是「先升到 f32、做 f32 的 exp、再降回 bf16」的结果。

#### 4.1.2 核心流程

每个超越函数操作都是「逐元素（element-wise）」的纯函数（带 `Pure` trait），且输入输出形状与类型完全一致（`AllTypesMatch<["source", "result"]>`）。通用形式是：

```
%result = <opname> %source [<modifiers>] : tile<...>
```

以指数为例，逐元素语义可写作：

\[
\mathrm{exp}(x)_i = e^{x_i}, \quad i \text{ 遍历 tile 的每个元素}
\]

带 `approx`/`full` 舍入模式的操作（如 `exp`、`sqrt`）流程如下：

1. 读取 `%source`，要求是基础浮点 tile；
2. 若舍入模式为 `full`：调用 CUDA Math API 的对应库函数（仅 `f32`/`f64` 有原生实现，`f16`/`bf16` 先升 f32）；
3. 若舍入模式为 `approx`：用硬件级快速近似，例如 `exp` 用 `exp2(x * log2(e))` 实现，仅 `f32` 支持；
4. 写回与 `%source` 同形状同元素类型的 `%result`。

#### 4.1.3 源码精读

**共享的「半精度在 f32 模拟」说明**，定义在 Ops.td 顶部，被几乎所有超越函数通过 `floating_point_math_suffix` 拼接进描述：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:63-66](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L63-L66) — 这是上文「f16/bf16 在 f32 下模拟」一句话的出处，所有数学操作拼到描述末尾。

**`exp`（指数）** 是带舍入模式的代表，演示了 `approx` 与 `full` 的取舍：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1749-1800](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1749-L1800) — 默认舍入为 `full`（依赖 CUDA Math API，仅 f32/f64 原生支持，bf16/f16 标 `*` 表示模拟）；`approx` 用 `exp2` 配 `log2(e)` 缩放做硬件近似，仅 f32 支持。第 1779 行的 `CudaTile_RoundingModeAttr` 默认值正是 `RoundingMode::FULL`。

**`sqrt`（平方根）** 与 **`rsqrt`（倒数平方根）** 是最常用的两个，且展示了 `flush_to_zero`（FTZ）修饰符：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:5110-5155](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L5110-L5155) — `rsqrt` 计算 \(\mathrm{rsqrt}(x)_i = 1/\sqrt{x_i}\)，支持把次正规数冲零的 `flush_to_zero`（仅 f32）。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:5161-5187](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L5161-L5187) — `sqrt` 同时支持 `flush_to_zero`、`approx` 与四种 IEEE 舍入模式；其描述表（第 5172 行起）明确标注 bf16/f16 的舍入模式带 `*`（在 f32 模拟）。

**`log`/`sin`/`cos`** 是「无修饰符」的极简形态——只有一个操作数，不带舍入参数，靠默认行为：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2872-2891](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2872-L2891) — `log` 是自然对数 \(\ln(x)\)，仅一个 `$source` 入参，无舍入模式（仍受 f32 模拟约束）。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:4341-4358](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4341-L4358) — `sin`，结构同 `log`。`cos`（第 881 行起）与之完全对称。

**`pow`（幂）与 `atan2`（四象限反正切）** 是少数带**两个操作数**的数学操作：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3738-3770](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3738-L3770) — `pow %src, %exp` 逐元素计算 \(\mathrm{pow}(x, y)_i = x_i^{y_i}\)，要求 `source/exponent/result` 三者类型一致、秩一致。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:360-397](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L360-L397) — `atan2 %x, %y` 由两个输入的符号判定象限，注意它的 `sinceVersion` 是 **`"13.2"`**（比多数 13.1 操作晚一个版本）。

> 小结：同一份 `floating_point_math_suffix` 把「半精度在 f32 模拟」这一关键事实注入到 `sin/cos/exp/log/sqrt/rsqrt/atan2` 等几乎所有数学操作；带舍入模式的只有 `exp`/`exp2`/`sqrt` 等少数，`log`/`sin`/`cos` 等则不暴露舍入参数。

#### 4.1.4 代码实践

**实践目标**：亲手写出 `exp` 的 `full` 与 `approx` 两种形态，并通过 `cuda-tile-opt` 验证 round-trip。

**操作步骤**：

1. 在仓库根目录创建一个临时文件 `~/math_probe.mlir`，内容如下（示例代码，仿照 Ops.td 中 `exp` 的 `mlirExamples`）：

   ```mlir
   // 示例代码
   cuda_tile.module @math_probe {
     cuda_tile.entry @e() {
       %in = constant <f32: [0.0, 1.0, 2.0, 3.0]> : !cuda_tile.tile<4xf32>
       %full = exp %in : !cuda_tile.tile<4xf32>
       %approx = exp %in rounding<approx> : !cuda_tile.tile<4xf32>
       // 用 print_tko 输出观察（需要 token，仅作 IR 合法性验证时可省略）
     }
   }
   ```

2. 运行（前提是你已按 u1-l2 构建了带 `CUDA_TILE_ENABLE_TESTING=ON` 的 `build` 目录）：

   ```bash
   build/bin/cuda-tile-opt ~/math_probe.mlir
   ```

**需要观察的现象**：终端应原样回显这段 IR（round-trip 成功），说明 `exp` 默认 `full` 与显式 `rounding<approx>` 均合法。

**预期结果**：无任何 verifier 报错，IR 正常打印。若把 `exp` 的输入改成 `tile<4xbf16>`，IR 仍合法（因为 bf16 在 f32 模拟），但你应意识到其数值是「升 f32 → exp → 降 bf16」的结果，而非硬件 bf16 数学指令。

> 待本地验证：`approx` 与 `full` 在 f32 上的具体数值差，需在能跑 GPU 的环境用 host 程序 `cuLaunchKernel` 后观察，本讲不展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么本讲列出的超越函数操作都拒绝 `fp8`/`fp4` 作为直接输入？

**答案**：这些操作的输入类型约束是 `CudaTile_BaseFloatTileType`，只覆盖 `f16/bf16/f32/f64`。低精度类型（`fp8`/`fp4`/`tf32`）需要先用下一节的 `ftof` 升到基础浮点，或走 u4-l5 的张量核 MMA 路径。

**练习 2**：`exp` 的 `approx` 模式用什么数学恒等实现？为什么只有 f32 支持？

**答案**：用 \(\mathrm{exp}(x) = 2^{x \cdot \log_2 e}\)（即 `exp2(x * log2(e))`）。它走的是硬件快速 `exp2` 近似指令，CUDA Math API 仅对 f32 提供该近似，故 bf16/f16/f64 不支持（见 Ops.td 第 1762–1763 行及描述表）。

### 4.2 Conversions 分组：总览与 `bitcast` 位级重解释

#### 4.2.1 概念说明

Conversions 分组的所有操作都派生自 `CudaTileConversionOpDef`，它们改变 tile 的**元素类型**而不改变形状（统一带 `AllShapesMatch`，即源与结果形状相同）。按「是否改动底层二进制位」可把九个操作分为三类：

| 类别 | 操作 | 是否改位 | 是否改数值 |
| --- | --- | --- | --- |
| 位保持（reinterpret） | `bitcast` | 否 | 否（位模式不变） |
| 数值保持（value-convert） | `ftof` / `ftoi` / `itof` | 是 | 尽量保持（带舍入） |
| 位宽变换（int-only） | `exti` / `trunci` | 是 | 整数域保持或截断 |
| 指针互转 | `int_to_ptr` / `ptr_to_int` / `ptr_to_ptr` | 否（64 位搬运/重解释） | 否 |

本模块先讲 `bitcast`，它是理解「位保持 vs 数值保持」这组核心对立的钥匙。

`bitcast` 把 tile 从一种元素类型「重解释」为另一种，**底层二进制位完全不变**。例如把 `i32` 的 32 个 bit 直接当作 IEEE-754 `f32` 来读。它的硬约束是：**源与结果的元素位宽必须相等**，且**不能用于指针类型**。

#### 4.2.2 核心流程

```
%result = bitcast %source : tile<SrcShape x SrcElem> -> tile<SrcShape x DstElem>
```

校验三步走（形状由 TableGen 的 `AllShapesMatch` 保证，位宽由手写 `verify()` 保证）：

1. **形状一致**：源与结果的形状必须相同（编译期由 trait 强制）。
2. **元素位宽相等**：`SrcElem` 与 `DstElem` 的 `getIntOrFloatBitWidth()` 必须相同。
3. **类型域**：源、结果都只能是 `CudaTile_NumberTileType`（整数或浮点 tile），**不能是指针 tile**——指针要交给 `ptr_to_int`/`int_to_ptr`。

`bitcast` 的「数值不变」用一个小例子说明：把整数 `i32 = 0x40490FDB`（约 3.141593 的位模式）bitcast 成 `f32`，得到的 `f32` 值就是 3.141593；位没动，只是解释方式变了。这与下一节的 `itof`（会把整数 3 转成浮点 3.0、改写位模式）截然不同。

#### 4.2.3 源码精读

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:748-768](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L748-L768) — `bitcast` 声明：第 757 行明确「只允许同位宽的非指针类型（如 i32→f32）」，指针须用 `ptr_to_int`/`int_to_ptr`；源结果类型约束同为 `CudaTile_NumberTileType`。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1659-1672](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1659-L1672) — 手写校验：取源、结果的元素类型，比较 `getIntOrFloatBitWidth()`，相等才放行，否则报 `"types must be equal width"`。

合法用例可对照测试文件：`i8 ↔ f8E4M3FN`、`i16 ↔ f16/bf16`、`i32 ↔ f32`、`i64 ↔ f64`（同位宽）均合法，见 [test/Dialect/CudaTile/conversion.mlir:10-18](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion.mlir#L10-L18)（i8 一组）与第 70–82 行（i32↔f32）。

非法用例与对应报错，见 [test/Dialect/CudaTile/conversion_invalid.mlir:3-19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion_invalid.mlir#L3-L19)：`tile<4xi16> -> tile<2xi32>` 触发形状不一致；`tile<i32> -> tile<i16>` 触发位宽不等。

#### 4.2.4 代码实践

**实践目标**：构造一个合法 bitcast 与一个非法（跨位宽）bitcast，对照观察。

**操作步骤**：

1. 写 `~/bc.mlir`（示例代码）：

   ```mlir
   // 示例代码
   cuda_tile.module @bc {
     cuda_tile.entry @ok() {
       %ci = constant <i32: [1, 2, 3, 4]> : !cuda_tile.tile<4xi32>
       // 合法：i32 与 f32 都是 32 位
       %cf = bitcast %ci : !cuda_tile.tile<4xi32> -> !cuda_tile.tile<4xf32>
     }
   }
   ```

2. 跑 `build/bin/cuda-tile-opt ~/bc.mlir`，确认合法。
3. 把目标类型改成 `tile<4xi16>`（跨位宽），再跑。

**需要观察的现象**：第 3 步应报 `op types must be equal width`（与 conversion_invalid.mlir 第 16 行一致）。

**预期结果**：合法版本 round-trip 通过；非法版本被 verifier 拒绝并给出精确错误串。

#### 4.2.5 小练习与答案

**练习 1**：能否用 `bitcast` 把 `tile<ptr<f32>>` 重解释成 `tile<ptr<i8>>`？

**答案**：不能。`bitcast` 的类型域是 `NumberTileType`，指针须用 `ptr_to_ptr`（见 4.4 节）。

**练习 2**：`bitcast %x : tile<2xi16> -> tile<2xf16>` 与 `itof %x : tile<2xi16> -> tile<2xf16>`（假设后者合法）得到的 `f16` 值是否相同？

**答案**：一般不同。`bitcast` 直接把 i16 的 16 个 bit 当作 f16 读（位不变）；`itof` 会把整数数值（如 5）转成对应的浮点 5.0（位模式完全重写）。

### 4.3 数值保持的数值类型转换：`ftof` / `ftoi` / `itof`

#### 4.3.1 概念说明

这一组转换**改变二进制位、但尽量保持数值**，并伴随舍入。三者都要求形状一致（`AllShapesMatch`），且都带 `signedness`（ftoi/itof）或 `rounding_mode`（ftof/ftoi/itof）属性。

- **`ftof`**：浮点→浮点，要求源类型 ≠ 结果类型（禁止 no-op），按舍入模式取整。
- **`ftoi`**：浮点→整数，保持数值、向零取整到最近整数。
- **`itof`**：整数→浮点，保持数值、舍入到最近的浮点数。

它们与 `bitcast` 的对照是本模块第二个核心对立：`bitcast` 保位不保值，`ftof/ftoi/itof` 保值（带舍入）不保位。

#### 4.3.2 核心流程

`ftoi` 的数值语义（向零取整到最近整数）：

\[
\mathrm{ftoi}(x)_i = \mathrm{trunc\_to\_int}(x_i)
\]

校验要点（均来自手写 `verify()`）：

- `ftof`：源结果类型不同（非 no-op）；默认 `nearest_even`，但若目标是 `f8E8M0FNU` 只允许 `zero`/`positive_inf`。
- `ftoi`：舍入模式**只能是** `nearest_int_to_zero`；必须有 `signedness`。
- `itof`：舍入模式**只能是** `nearest_even`；**不能**把整数转成 `f8E8M0FNU`（须先转到别的浮点类型）。

注意两处 **undefined behavior / 特殊值**警示（Ops.td 描述原文）：

- `ftoi`：若输入是 `Inf`/`NaN`，或舍入后超出目标整数范围，返回值是 UB（用户须自行保证输入有限且在范围内）。
- `itof`：若整数舍入后超出目标浮点范围，转成 `Inf`（支持 Inf 的类型）或 `NaN`。

#### 4.3.3 源码精读

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2103-2128](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2103-L2128) — `ftof`：第 2110 行「源类型必须不同于结果类型」；第 2113–2114 行说明转到 `f8E8M0FNU` 只支持 `zero`/`positive_inf`，其余只支持 `nearest_even`。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2134-2174](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2134-L2174) — `ftoi`：第 2140–2141 行点明「与 bitcast 不同，这里保持数值、向零取整」；第 2144 行「只支持 `nearest_int_to_zero`」；第 2146–2152 行是 Inf/NaN/越界即 UB 的警告。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2611-2644](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2611-L2644) — `itof`：第 2616–2617 行「与 bitcast 不同，保持数值、舍入到最近浮点」；第 2619–2623 行是越界→Inf/NaN 的警告。

手写校验逻辑集中在一处：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:2630-2661](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2630-L2661) — `FToIOp::verify`（舍入必须 `nearest_int_to_zero`）、`FToFOp::verify`（非 no-op + 按目标类型限定舍入模式）。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:2126-2137](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2126-L2137) — `IToFOp::verify`：禁止目标是 `f8E8M0FNU`，且舍入必须 `nearest_even`。

舍入模式与有符号性的枚举定义见 [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:193-209](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L193-L209)（七种舍入）与 [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:45-52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L45-L52)（`signed`/`unsigned`）。

合法用例对照 [test/Dialect/CudaTile/conversion.mlir:230-350](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion.mlir#L230-L350)（`@ftoi` 与 `@itof`），其中第 327 行展示了 `ftoi ... unsigned rounding<nearest_int_to_zero>` 的显式写法。

#### 4.3.4 代码实践

**实践目标**：用 `ftoi` 把一段 f32 tile 转成 i32，并观察舍入与有符号性如何书写。

**操作步骤**：

1. 写 `~/cv.mlir`（示例代码）：

   ```mlir
   // 示例代码
   cuda_tile.module @cv {
     cuda_tile.entry @e() {
       %cf = constant <f32: [[1.0, 2.0], [3.0, 4.0]]> : !cuda_tile.tile<2x2xf32>
       %ci = ftoi %cf signed : !cuda_tile.tile<2x2xf32> -> !cuda_tile.tile<2x2xi32>
     }
   }
   ```

2. 跑 `build/bin/cuda-tile-opt ~/cv.mlir`。
3. 把 `signed` 删掉再跑，观察报错。

**需要观察的现象**：第 3 步应报 `expected signedness to be one of: {'signed', 'unsigned'}`（与 conversion_invalid.mlir 第 135–138 行一致）。

**预期结果**：带 `signed` 的版本合法；缺省 `signedness` 的版本在解析期即被拒绝。

> 待本地验证：把 `ftoi` 的舍入显式写成 `rounding<nearest_even>`，应在 verifier 阶段报「Only 'nearest_int_to_zero' is supported」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ftof` 禁止源类型与结果类型相同？

**答案**：相同类型就是 no-op，没有意义且会引入冗余 IR。校验在 `FToFOp::verify` 第 2643 行直接报 `"converting tiles must not be a no-op"`（对照 conversion_invalid.mlir 第 96 行）。

**练习 2**：想把一个 `i8` tile 转成 `f8E8M0FNU` tile，直接用 `itof` 行吗？

**答案**：不行。`IToFOp::verify` 会拒绝并提示「please first convert to another float type」。正确做法是先用 `itof` 转到 `f32`/`f16`，再用 `ftof` 降到 `f8E8M0FNU`（此时舍入只能选 `zero` 或 `positive_inf`）。

### 4.4 位宽变换与指针转换：`exti` / `trunci` / `int_to_ptr` / `ptr_to_int` / `ptr_to_ptr`

#### 4.4.1 概念说明

剩下五个操作分两组：

**整数位宽变换**（只在整数域）：

- `exti`：把整数 tile 扩展到**严格更宽**的位宽。`unsigned` 做零扩展、`signed` 做符号扩展。
- `trunci`：把整数 tile 截断到**严格更窄**的位宽，直接丢弃高位。可选 `overflow` 提示（`NSW`/`NUW`/`NW`）。

**指针互转**（始终 64 位）：

- `int_to_ptr`：`tile<i64>` → `tile<ptr<T>>`，整数按**无符号**解释为地址。
- `ptr_to_int`：`tile<ptr<T>>` → `tile<i64>`，逆操作。
- `ptr_to_ptr`：`tile<ptr<A>>` → `tile<ptr<B>>`，只改 pointee 类型、不改地址位，禁止指针↔非指针。

`int_to_ptr`/`ptr_to_int` 固定走 `i64`，这与 GPU 64 位地址空间一致；指针与整数的转换被特意拆成独立操作，是为了「让编译器未来能对指针来源（pointer provenance）做推理」（见 `ptr_to_ptr` 描述原文）。

#### 4.4.2 核心流程

`exti`（符号扩展 vs 零扩展）：

\[
\mathrm{exti}_{\text{signed}}(x): \text{复制符号位到高位};\quad
\mathrm{exti}_{\text{unsigned}}(x): \text{高位补 0}
\]

`trunci`：直接保留低位、丢弃高位；若带 `NSW`/`NUW`/`NW`，则声明被丢弃的位满足特定条件（编译期假设）。

校验要点：

- `exti`：`to.getWidth() > from.getWidth()`，否则报「extending to smaller or identical integer」。
- `trunci`：`to.getWidth() < from.getWidth()`，否则报「truncating to larger or identical integer」。
- `int_to_ptr`：源必须是 `i64`；结果是指针 tile。
- `ptr_to_int`：结果必须是 `i64`；源是指针 tile。
- `ptr_to_ptr`：源、结果都是指针 tile。

#### 4.4.3 源码精读

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1857-1885](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1857-L1885) — `exti`：第 1862–1865 行说明 unsigned 零扩展、signed 符号扩展；带 `SignednessAttr`。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2581-2605](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2581-L2605) — `trunci`：第 2588–2592 行解释 `overflow` 属性（NSW 要求被截断位全等于结果符号位，NUW 要求被截断位全零）；默认 `NONE`。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:2502-2524](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2502-L2524) — `int_to_ptr`：第 2510 行「源按无符号整数解释」；源类型约束 `CudaTile_IntTileInt64Type`（即 i64）。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3828-3850](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3828-L3850) — `ptr_to_int`：结果固定 `i64`，且「结果应按无符号解释」。

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3856-3878](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3856-L3878) — `ptr_to_ptr`：第 3863 行「禁止指针与非指针互转」，第 3866 行点明「拆成独立操作是为了未来对指针来源做推理」。

手写校验：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:2029-2037](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2029-L2037) — `ExtIOp::verify`：`to.getWidth() <= from.getWidth()` 即报错。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:5521-5526](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5521-L5526) — `TruncIOp::verify`：方向相反，`to.getWidth() >= from.getWidth()` 即报错。

指针互转的合法链路见 [test/Dialect/CudaTile/conversion.mlir:105-121](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion.mlir#L105-L121)（`int_to_ptr` → `ptr_to_int` → `ptr_to_ptr`）。非法用例见 [test/Dialect/CudaTile/conversion_invalid.mlir:23-37](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/conversion_invalid.mlir#L23-L37)：`int_to_ptr` 的源不是 i64、`ptr_to_int` 的结果不是 i64 都会被类型约束拒绝。

#### 4.4.4 代码实践

**实践目标**：构造一条「i64 → ptr<i8> → ptr<f64>」的合法链路，并用非法源类型触发报错。

**操作步骤**：

1. 写 `~/ptr.mlir`（示例代码，仿 conversion.mlir 第 105–121 行）：

   ```mlir
   // 示例代码
   cuda_tile.module @ptr {
     cuda_tile.entry @e() {
       %ci = constant <i64: [1, 2, 3, 4]> : !cuda_tile.tile<4xi64>
       %cp = int_to_ptr %ci : !cuda_tile.tile<4xi64> -> !cuda_tile.tile<4xptr<i8>>
       %cp2 = ptr_to_ptr %cp : !cuda_tile.tile<4xptr<i8>> -> !cuda_tile.tile<4xptr<f64>>
     }
   }
   ```

2. 跑 `build/bin/cuda-tile-opt ~/ptr.mlir`，确认合法。
3. 把 `int_to_ptr` 的源改成 `tile<4xi32>` 再跑。

**需要观察的现象**：第 3 步应报 `operand #0 must be tile of i64 values`（与 conversion_invalid.mlir 第 25 行一致）。

**预期结果**：合法链路 round-trip 通过；非法源类型被类型约束在解析期拒绝。

#### 4.4.5 小练习与答案

**练习 1**：`trunci` 默认不带 `overflow`，与带 `NUW` 有何区别？

**答案**：默认 `NONE` 表示编译器对被丢弃的高位不做任何假设；带 `NUW`（no unsigned wrap）则声明被截断的高位全为零——这是编译期假设，运行时违反即未定义行为，可换取更优代码（见 AttrDefs.td 第 68 行与 Ops.td 第 2588–2592 行）。

**练习 2**：把一个 `tile<ptr<f32>>` 改成 `tile<i64>`，该用 `bitcast` 还是 `ptr_to_int`？

**答案**：必须用 `ptr_to_int`。`bitcast` 的类型域不含指针，校验会拒绝；`ptr_to_int` 专为指针→i64 设计。

## 5. 综合实践

把本讲的三类操作串成一条数据变换流水线，完整跑通一次。

**任务**：对一段 `f32` tile 依次做 `exp` → `sqrt`（指数后再开方，近似还原幅值），再用 `ftoi` 转成 `i32`，最后用 `bitcast` 把这串 `i32` 位模式重解释回 `f32`；并额外构造一个跨位宽 `bitcast` 验证它会报错。

**参考实现**（示例代码）：

```mlir
// 示例代码
cuda_tile.module @pipeline {
  cuda_tile.entry @e() {
    %in   = constant <f32: [0.0, 1.0, 2.0, 3.0]> : !cuda_tile.tile<4xf32>
    %e    = exp   %in                       : !cuda_tile.tile<4xf32>
    %s    = sqrt  %e                        : !cuda_tile.tile<4xf32>
    %i    = ftoi  %s signed                : !cuda_tile.tile<4xf32> -> !cuda_tile.tile<4xi32>
    %back = bitcast %i                      : !cuda_tile.tile<4xi32> -> !cuda_tile.tile<4xf32>

    // 以下是应当被拒绝的写法（跨位宽 bitcast），单独放在另一个 entry 里用 -verify-diagnostics 观察：
    // %bad = bitcast %i : !cuda_tile.tile<4xi32> -> !cuda_tile.tile<4xi16>  // 报 "types must be equal width"
  }
}
```

**操作步骤**：

1. 把合法部分存为 `~/pipe.mlir`，运行 `build/bin/cuda-tile-opt ~/pipe.mlir`，确认 round-trip 通过。
2. 取消注释的非法 `bitcast`，改用 `build/bin/cuda-tile-opt ~/pipe.mlir -verify-diagnostics` 运行，确认报 `op types must be equal width`。
3. 思考：`%back` 的值与 `%in` 相同吗？为什么？

**预期结果 / 待本地验证**：合法 IR 通过；非法 bitcast 被拒。`%back` 通常**不等于** `%in`：`exp→sqrt` 后数值已变（\(\sqrt{e^x}\neq x\)），再经 `ftoi` 向零取整丢失小数部分，最后 `bitcast` 只是把 `i32` 位原样当成 `f32` 读。这条链路恰好演示了「数值保持（exp/sqrt/ftoi）」与「位保持（bitcast）」两类操作的交替。

## 6. 本讲小结

- 超越函数（`exp/log/sin/cos/sqrt/rsqrt/pow/atan2` 等）都属 Floating Point 分组，只接受基础浮点 `f16/bf16/f32/f64`，且 **`f16`/`bf16` 在 `f32` 下模拟**（`floating_point_math_suffix`）；带舍入模式的只有 `exp`/`sqrt` 等，`approx` 多为 f32 专享。
- Conversions 分组按「是否改位」分三类：`bitcast` 位保持、`ftof/ftoi/itof` 数值保持（带舍入）、`exti/trunci` 整数位宽变换、`int_to_ptr/ptr_to_int/ptr_to_ptr` 指针互转。
- `bitcast` 要求**同位宽、非指针**，是「位不变」的重解释；`ftoi/itof/ftof` 是「数值不变、位重写」的真转换，各有严格的舍入模式约束（`ftoi` 仅 `nearest_int_to_zero`、`itof` 仅 `nearest_even`、`ftof` 视目标类型而定）。
- `exti`/`trunci` 严格改变位宽（一个只能变宽、一个只能变窄），`overflow` 提示是编译期假设。
- 指针互转固定走 `i64`，`int_to_ptr`/`ptr_to_int`/`ptr_to_ptr` 各司其职，拆分是为了支持未来对指针来源（provenance）的推理。
- 所有转换的校验规则都能在 `conversion.mlir`（合法）与 `conversion_invalid.mlir`（非法 + `expected-error`）里找到对照。

## 7. 下一步学习建议

- **u4-l5（张量核 MMA 操作）**：本讲的转换操作是 MMA 的「数据准备工具」——`ftof` 把高精度权重降到 `fp8`/`fp4`、`bitcast` 在低精度格式间重解释，都是 MMA 前后的常见配套。
- **u4-l6（归约、扫描与低精度打包）**：`pack`/`unpack` 与本讲的 `bitcast`/`trunci` 在低精度紧凑存储上互为补充，建议对照阅读。
- **u5-l1（内存模型与 Token 顺序）**：本讲的 `int_to_ptr`/`ptr_to_ptr` 产出的指针 tile 正是 `load_ptr_tko`/`store_ptr_tko` 的输入，承接关系紧密。
- 想深入校验细节，可继续读 [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) 中各 `::verify()` 函数，与 `conversion_invalid.mlir` 的每个 `expected-error` 一一对应。
