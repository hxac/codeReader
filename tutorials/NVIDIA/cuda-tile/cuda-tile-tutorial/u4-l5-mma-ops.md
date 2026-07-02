# 张量核 MMA 操作

## 1. 本讲目标

CUDA Tile IR 的核心价值，是把 NVIDIA GPU **张量核（Tensor Core）** 上的「矩阵乘累加」做成 IR 层的一等操作。学完本讲，你应该能够：

1. 说清楚什么是 MMA（Matrix Multiply-Accumulate），以及它为什么是张量核计算的基本指令。
2. 读懂 `mmaf`（浮点）、`mmai`（整数）、`mmaf_scaled`（带块缩放的低精度）三类操作的定义、数据类型组合与形状约束。
3. 理解 `mmaf_scaled` 的「块缩放（block scaling）」机制，掌握 `vecSize`（K 维缩减因子）在不同精度下的取值规则。
4. 知道 Python 端如何用 `MMAConfig` / `MMAScaledConfig` 配置注册表描述合法类型组合，以及 `find_mma_config` 如何完成匹配与错误提示。

本讲承接 [u4-l3 浮点算术与 FMA](u4-l3-float-arith.md)：那里讲的是「逐元素」浮点运算，本讲则进入「整块矩阵」的硬件加速运算。

## 2. 前置知识

- **Tile 类型**：`tile<shapexelem>`，如 `tile<4x8xf16>`。详见 [u3-l1 Tile 类型与元素类型](u3-l1-tile-and-element-types.md)。本讲的所有操作数都是 2D 或 3D 的 Tile。
- **元素类型**：整数（`i8`/`i32`）、基础浮点（`f16`/`bf16`/`f32`/`f64`）、低精度浮点（`tf32`、`f8E4M3FN`、`f8E5M2`、`f4E2M1FN`）以及缩放专用类型 `f8E8M0FNU`。
- **张量核 vs CUDA Core**：CUDA Core 每次做一个标量乘加（FMA）；张量核每次做一小块矩阵（如 16×16×16）的乘累加，吞吐量高出一两个数量级。深度学习里的 GEMM（通用矩阵乘）几乎都跑在张量核上。
- **signedness（有符号性）**：整数运算要区分有符号 / 无符号，概念见 [u4-l2 整数算术](u4-l2-integer-arith.md)。

一个直觉：MMA 的数学本质就是 \[ C \mathrel{+}= A \times B \]，但「用什么精度存 A、B」「累加器用什么精度」「形状多大」这三件事，在不同代际张量核（Ampere/Hopper/Blackwell）上有不同的硬件支持组合。CUDA Tile IR 用三组操作把这套组合显式编码进 IR。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 三类 MMA 操作的 TableGen 定义（操作数类型约束、汇编格式、文档表格） |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | 三个 `verify()` 与共享的 `verifyMmaShapes` 形状校验逻辑 |
| [python/cuda_tile/dialects/cuda_tile_ops.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py) | Python 端 `MMAConfig` / `MMAScaledConfig` 注册表与 `find_mma_config` 匹配、统一 `mma()` 入口 |
| [test/Dialect/CudaTile/ops.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir) | 三类 MMA 操作的合法用例（含 `fast_acc`、批量、各类缩放） |

---

## 4. 核心概念与源码讲解

### 4.1 MMA 概念与 mmaf / mmai 操作定义

#### 4.1.1 概念说明

**MMA（Matrix Multiply-Accumulate，矩阵乘累加）** 是张量核的原子操作：给定左矩阵 \(A\)、右矩阵 \(B\) 和累加器 \(C\)，计算

\[
D_{ij} = \sum_{k=0}^{K-1} A_{ik} \cdot B_{kj} + C_{ij}
\]

也就是「矩阵乘 + 加上旧累加器」。注意它**自带加法**——这正是张量核区别于「先 `mulf` 再 `addf`」的地方：硬件在一次提交里完成乘和累加，省去中间寄存器往返，且低精度输入在高位累加器里累加，精度更好（参见 [u4-l3](u4-l3-float-arith.md) 关于 FMA 与分离 mul+add 的精度对比）。

CUDA Tile IR 把 MMA 拆成三组操作：

| 操作 | 输入 | 累加器/输出 | 典型硬件 | sinceVersion |
| --- | --- | --- | --- | --- |
| `mmaf` | 浮点 Tile | 浮点 Tile | 各代张量核 | 13.1 |
| `mmai` | `i8` Tile | `i32` Tile | INT8 张量核 | 13.1 |
| `mmaf_scaled` | 低精度浮点 + 缩放因子 | `f32` | Hopper/Blackwell | 13.3 |

它们都派生自对应分组基类：`mmaf`/`mmaf_scaled` 属 Floating Point 分组（`CudaTileFloatingPointOpDef`），`mmai` 属 Integer 分组（`CudaTileIntegerOpDef`）——分组机制见 [u2-l2](u2-l2-dialect-definition.md)。

#### 4.1.2 核心流程

三类操作共享同一套**形状约束**（2D 非批量与 3D 批量）：

- 非批量（2D）：`lhs` 形状 `M×K`，`rhs` 形状 `K×N`，`acc`/`result` 形状 `M×N`。
- 批量（3D）：`lhs` 形状 `B×M×K`，`rhs` 形状 `B×K×N`，`acc`/`result` 形状 `B×M×N`；批维 `B` 三者必须一致。

用伪代码描述校验流程：

```
校验(lhs, rhs, acc):
    rank ∈ {2, 3}                      # 由 AllRanksMatch 强制三者同秩
    若 rank == 3 (批量):
        lhs.B == rhs.B == acc.B        # 批维一致
    M, N, K 维满足: lhs.K == rhs.K, lhs.M == acc.M, rhs.N == acc.N
    再按操作各自的「元素类型组合表」校验类型
```

`mmaf` 的合法**元素类型组合**（输入类型 → 允许的累加器/输出类型）如下表，输入操作数 `lhs` 与 `rhs` 必须同元素类型：

| 输入类型 | 允许的输出（累加器）类型 |
| --- | --- |
| `f8E4M3FN` | `f16` 或 `f32` |
| `f8E5M2` | `f16` 或 `f32` |
| `f16` | `f16` 或 `f32` |
| `bf16` | `f32` |
| `tf32` | `f32` |
| `f32` | `f32` |
| `f64` | `f64` |

`mmaf` 还有一个可选修饰符 `fast_acc`（自 13.3 引入）：在 Hopper 上对 FP8 MMA 启用「更快但精度略低」的累加路径。

`mmai` 的规则更简单：输入 `lhs`/`rhs` 固定为 `i8`，累加器与结果固定为 `i32`（结果始终按有符号解释）。`i8` 的有符号性由两个属性 `signedness_lhs`、`signedness_rhs` 分别指定（`signed` 或 `unsigned`，可混合）。

#### 4.1.3 源码精读

**操作数类型约束**：`mmaf` 用两个 `CudaTile_TileOf` 约束枚举合法的输入/累加器元素类型。

[Ops.td:1452-1459](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1452-L1459) 定义了 `mmaf` 接受的输入（`f16/bf16/f32/f64/tf32/f8E4M3FN/f8E5M2`）与累加器（`f16/f32/f64`）元素类型集合。

[Ops.td:1461-1536](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1461-L1536) 是 `mmaf` 的完整定义。关键点：

- traits `[Pure, AllTypesMatch<["acc","result"]>, AllElementTypeMatch<["lhs","rhs"]>, AllRanksMatch<["lhs","rhs","acc"]>]`：无副作用、结果与累加器同类型、`lhs` 与 `rhs` 同元素类型、三者同秩。
- 数学公式写在 `description` 里：\(\text{mmaf}(A,B,C)_{ij} = \sum_{k} A_{ik} B_{kj} + C_{ij}\)。
- 支持类型表写在 `descriptionTables` 里（即上文表格的来源）。
- 汇编格式 `%lhs, %rhs, %acc (`fast_acc` $fast_acc^)? attr-dict : type(lhs), type(rhs), type(acc)`。

`mmai` 的定义类似但带两个 signedness 属性，并在汇编里直接打印 `signed`/`unsigned` 关键字：

[Ops.td:1628-1689](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1628-L1689) 定义 `mmai`，输入固定 `i8`、累加器固定 `i32`，[Ops.td:1683-1686](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1683-L1686) 的汇编格式用 `custom<Signedness>(...)` 把 `signedness_lhs signedness_rhs` 打印成 `signed signed` 这样的关键字。

**共享形状校验** `verifyMmaShapes` 是三类操作复用的模板函数，集中实现 2D/3D 形状一致性：

[CudaTile.cpp:2144-2192](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2144-L2192) 用 `batched = (rank == 3)` 区分批维，依次核对批维一致、`lhs.K==rhs.K`、`lhs.M==acc.M`、`rhs.N==acc.N`，任一不满足都 `emitOpError` 给出带具体形状的诊断信息。

`mmaf` 在形状之外还要校验元素类型组合，用一个 `AllowedMMAType` 表驱动：

[CudaTile.cpp:2194-2251](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2194-L2251) 是 `MmaFOp::verify`。它在 [CudaTile.cpp:2208-2227](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2208-L2227) 列出每个输入类型对应的合法输出集合，遍历匹配；不命中则报 `unsupported combination of element types` 或 `unsupported input element type`。

`mmai` 则最省事——元素类型已被 TableGen 类型约束锁死（`i8`→`i32`），只需复用形状校验：

[CudaTile.cpp:2390-2392](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2390-L2392) `MmaIOp::verify` 只调用 `verifyMmaShapes(*this)`。

#### 4.1.4 代码实践

**实践目标**：用 `cuda-tile-opt` 验证一段包含 `mmaf` 与 `mmai` 的 MLIR，观察汇编打印与形状校验。

操作步骤：

1. 新建文件 `mma_test.mlir`，写入以下内容（参考 `test/Dialect/CudaTile/ops.mlir` 的写法）：

```mlir
// 示例代码
cuda_tile.module @kernels {
  testing$func @mma_f16(%a: !cuda_tile.tile<4x8xf16>,
                        %b: !cuda_tile.tile<8x16xf16>,
                        %c: !cuda_tile.tile<4x16xf32>) {
    // f16 x f16 -> f32
    %0 = mmaf %a, %b, %c : tile<4x8xf16>, tile<8x16xf16>, tile<4x16xf32>
  }
  testing$func @mma_i8(%a: !cuda_tile.tile<4x8xi8>,
                       %b: !cuda_tile.tile<8x16xi8>,
                       %c: !cuda_tile.tile<4x16xi32>) {
    // i8 x i8 -> i32，两侧均无符号
    %0 = mmai %a, %b, %c unsigned unsigned
        : tile<4x8xi8>, tile<8x16xi8>, tile<4x16xi32>
  }
}
```

> 说明：`testing$func` 受 `TILE_IR_INCLUDE_TESTS` 保护，只在开启测试时可用，便于在不写完整 `entry` 的情况下做片段验证（详见 [u2-l2](u2-l2-dialect-definition.md)）。

2. 运行 `cuda-tile-opt mma_test.mlir`。

需要观察的现象：两条 MMA 都应通过校验并被原样打印（round-trip）。

预期结果：终端回显与输入等价的 MLIR，`mmaf`/`mmai` 行不变。

3. 把 `@mma_f16` 里 `acc` 的形状从 `4x16` 改成 `4x8`（破坏 `rhs.N==acc.N`），再次运行。

预期结果：`cuda-tile-opt` 报 `shape error: dim ... of rhs and dim ... of acc must match`，与 `verifyMmaShapes` 的诊断一致。

> 若本地尚未构建 `cuda-tile-opt`，构建步骤见 [u1-l2](u1-l2-repo-and-build.md)；运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mmaf` 的 `bf16` 输入只允许 `f32` 累加器，而不允许 `f16`？

参考答案：`bf16` 用 8 位指数、`f16` 用 5 位指数，把 `bf16` 的乘积累加到 `f16` 会指数溢出（动态范围不够）；硬件只提供 `bf16→f32` 累加路径，故 IR 也只允许 `f32`。

**练习 2**：`mmai` 的 `acc` 为什么固定 `i32` 且总按有符号解释，即使输入是 `unsigned`？

参考答案：`i8×i8` 的单点积最大为 \(255 \times 255 \times K\)，远超 `i8`/`i16` 表示范围，硬件用 32 位累加器；无论输入有符号与否，累加结果统一用 `i32` 解释，由前端在需要时再做饱和/截断。输入的有符号性只影响「如何把 `i8` 解读成数值」，不影响累加器位宽。

**练习 3**：把 `mmaf` 的 `lhs` 设为 `tile<4x8xf16>`、`rhs` 设为 `tile<8x16xf16>`、`acc` 设为 `tile<4x16xi32>`，校验会通过吗？为什么？

参考答案：不会通过。虽然形状满足 `M×K, K×N, M×N`，但 `f16` 输入只允许 `f16` 或 `f32` 累加器，`i32` 不在合法输出集合里，`MmaFOp::verify` 会报 `unsupported combination of element types`。

---

### 4.2 mmaf_scaled：带块缩放的低精度 MMA

#### 4.2.1 概念说明

在 Hopper（sm_90）与 Blackwell（sm_100/120）上，为了在更低比特（4bit/8bit）下保留动态范围，硬件采用**块缩放（block scaling）**：不再为每个元素存一个浮点数，而是「一块连续元素共享一个缩放因子」。代表性的微缩浮点格式有 **MXFP8**、**MXFP4**、**NVFP4**。

`mmaf_scaled` 就是为此设计：它在普通 `mmaf` 的三个操作数之外，再额外接收两个缩放因子 Tile `lhs_scale` 与 `rhs_scale`。每个缩放因子元素沿 K 维作用于一整块（长度为 `V`，即 **vecSize**，又称 block size）连续元素。其数学定义为：

\[
\text{mmaf\_scaled}(lhs, rhs, acc, sfa, sfb)_{i,j}
= \sum_{k=0}^{K-1} \big(lhs_{i,k} \cdot sfa_{i,\,k/V}\big) \times \big(rhs_{k,j} \cdot sfb_{k/V,\,j}\big) + acc_{i,j}
\]

其中 \(V\) 是缩放块的向量长度。直观理解：先把 `lhs`、`rhs` 各自按块乘上缩放因子，再做标准 MMA。该操作自 13.3 引入。

#### 4.2.2 核心流程

`mmaf_scaled` 复用 `mmaf` 的 M/N/K 形状约束，额外加上**缩放因子的形状与 vecSize 约束**。设 `lhs` 为 `M×K`：

- `lhs_scale` 形状为 `M×(K/V)`：M 维与 `lhs` 相同，K 维被 `V` 缩减。
- `rhs_scale` 形状为 `(K/V)×N`：N 维与 `rhs` 相同，K 维被 `V` 缩减。
- `acc`/结果固定为 `f32`。

合法的类型组合（取自规范表）：

| `lhs`/`rhs` 元素类型 | `lhs_scale`/`rhs_scale` 元素类型 | 输出 |
| --- | --- | --- |
| `f8E4M3FN`、`f8E5M2`、`f4E2M1FN` | `f8E8M0FNU` | `f32` |
| `f4E2M1FN` | `f8E4M3FN` | `f32` |

`vecSize`（即 `K / scaleK`）的取值规则取决于操作数与缩放类型：

| 场景 | vecSize | 含义 |
| --- | --- | --- |
| `f8` 操作数 + `e8m0` 缩放（MXFP8） | 32 | QMMA，K=32，1× |
| `f4` 操作数 + `e4m3` 缩放（NVFP4） | 16 | OMMA，K=64，4× |
| `f4` 操作数 + `e8m0` 缩放（MXFP4） | 16 或 32 | OMMA，K=64，4× 或 2× |

注意规范里的提醒：并非每种合法组合在所有架构上都高效；用 `f8E4M3FN` 作缩放类型时，缩放值必须非负，否则结果未定义。

#### 4.2.3 源码精读

**类型约束**：`mmaf_scaled` 的操作数、缩放、结果各有独立的元素类型集合。

[Ops.td:1542-1553](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1542-L1553) 定义三类 Tile 约束：操作数（`f8E4M3FN/f8E5M2/f4E2M1FN`）、缩放（`f8E4M3FN/f8E8M0FNU`）、结果（`f32`）。

[Ops.td:1555-1618](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1555-L1618) 是 `mmaf_scaled` 完整定义。traits 比普通 `mmaf` 多了两条：`lhs_scale` 与 `rhs_scale` 同元素类型、五个操作数同秩。汇编格式固定打印五个操作数 `%lhs, %rhs, %acc, %lhs_scale, %rhs_scale` 后跟五个类型。

**校验逻辑**：`MmaFScaledOp::verify` 是本讲最复杂的 verifier，分四段。

[CudaTile.cpp:2257-2384](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2257-L2384) 先复用 `verifyMmaShapes`（[CudaTile.cpp:2257-2259](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2257-L2259)），再逐段校验：

- [CudaTile.cpp:2272-2302](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2272-L2302)：缩放 Tile 的 M/N 维与 `lhs`/`rhs` 对齐（批维也要对齐）。
- [CudaTile.cpp:2305-2313](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2305-L2313)：`lhs_scale` 的 K 维与 `rhs_scale` 的 K 维一致。
- [CudaTile.cpp:2316-2336](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2316-L2336)：缩放类型与操作数类型的搭配（`e4m3` 缩放要求 `f4` 操作数；`e8m0` 缩放允许 `f8`/`f4` 操作数）。
- [CudaTile.cpp:2338-2381](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2338-L2381)：vecSize 计算 `dimKSize / scaleDimKSize` 并按精度强制造值（`f8→32`、`f4+e4m3→16`、`f4+e8m0→16或32`）。

核心是 [CudaTile.cpp:2339-2348](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2339-L2348) 计算 `lhsScaleVecSize = dimKSize / scaleDimKSize`，要求整除；随后按操作数/缩放类型分别约束该值。

#### 4.2.4 代码实践

**实践目标**：验证一组 MXFP8 的 `mmaf_scaled`，并亲手触发 vecSize 报错。

操作步骤：

1. 参考 `test/Dialect/CudaTile/ops.mlir` 中 `test_mmaf_scaled_fp8e5m2` 的写法，新建 `scaled.mlir`：

```mlir
// 示例代码：MXFP8，e8m0 缩放，vecSize = 128/4 = 32
cuda_tile.module @kernels {
  testing$func @scaled(%lhs: !cuda_tile.tile<128x128xf8E5M2>,
                       %rhs: !cuda_tile.tile<128x128xf8E5M2>,
                       %acc: !cuda_tile.tile<128x128xf32>,
                       %sfa: !cuda_tile.tile<128x4xf8E8M0FNU>,
                       %sfb: !cuda_tile.tile<4x128xf8E8M0FNU>) {
    %0 = mmaf_scaled %lhs, %rhs, %acc, %sfa, %sfb
        : !cuda_tile.tile<128x128xf8E5M2>, !cuda_tile.tile<128x128xf8E5M2>,
          !cuda_tile.tile<128x128xf32>,
          !cuda_tile.tile<128x4xf8E8M0FNU>, !cuda_tile.tile<4x128xf8E8M0FNU>
  }
}
```

2. 运行 `cuda-tile-opt scaled.mlir`。

需要观察的现象：`K=128`、`scaleK=4`，`vecSize=32`，恰好满足 MXFP8 规则，应通过校验。

预期结果：round-trip 打印，无报错。

3. 把 `%sfa` 的形状从 `128x4` 改成 `128x8`（使 `vecSize=16`），再次运行。

预期结果：`cuda-tile-opt` 报 `f8 element type requires block scale factor ... to be 32, but got 16`，对应 [CudaTile.cpp:2351-2358](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2351-L2358)。

> 运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`f4E2M1FN` 操作数配合 `f8E8M0FNU` 缩放，vecSize 可以取哪两个值？分别对应什么模式？

参考答案：`16` 或 `32`。`16` 对应 OMMA K=64 的 4× 模式（mxf4nvf4），`32` 对应 2× 模式（mxf4/mxf8f6f4）。

**练习 2**：为什么 `mmaf_scaled` 的 `acc` 和结果固定为 `f32`，而不是像 `mmaf` 那样允许 `f16`？

参考答案：低精度（4bit/8bit）输入本身精度极低，累加必须在足够宽的累加器里进行才不丢信息；硬件 QMMA/OMMA 路径都使用 32 位浮点累加，故 IR 固定 `f32`。

**练习 3**：`lhs_scale` 形状是 `M×(K/V)`，为什么不与 `lhs` 完全同形？

参考答案：缩放因子是「一块共享一个」的，沿 K 维每 `V` 个元素才有一个缩放值，故 K 维被压缩为 `K/V`；M 维是逐行独立的，所以保持 `M`。

---

### 4.3 Python 配置体系：MMAConfig / MMAScaledConfig

#### 4.3.1 概念说明

C++ 端用 `verify()` 把「合法类型组合」硬编码进 IR 校验；Python 端则需要一份**等价的、可查询的注册表**，原因有二：

1. **构造 IR 前的前置检查**：Python 的 `mma()` 在生成底层 `MmaFOp`/`MmaIOp` 之前，先自查类型组合是否合法，能在不触发 C++ 编译的前提下给出友好的、列出所有受支持配置的错误信息。
2. **配置发现**：把每种合法组合抽象成一个 `MMAConfig` 对象（带名字、操作数 dtype、累加器 dtype、signedness），用 Python 的子类自动发现机制注册，新增一种组合只要新增一个子类。

`MMAConfig` 覆盖 `mmaf` 与 `mmai` 的全部组合（共 12 种），`MMAScaledConfig` 覆盖 `mmaf_scaled` 的组合（共 5 种）。两者的匹配方法 `matches_types` 是核心。

#### 4.3.2 核心流程

注册与匹配流程：

```
模块加载 → _initialize_mma_configs()
    遍历 MMAConfig.__subclasses__()        # Python 自动发现全部子类
    实例化每一个，缓存到 _SUPPORTED_MMA_CONFIGS

mma(lhs, rhs, acc):
    校验形状
    cfg = find_mma_config(lhs_et, rhs_et, acc_et)
        遍历缓存，找到第一个 matches_types 命中的配置
    若 cfg is None:
        抛 TypeError，并列出 get_supported_mma_configs() 全部名字
    若 acc 是整数类型 → 生成 MmaIOp
    否则              → 生成 MmaFOp
```

`MMAConfig` 全部 12 种（与 4.1 的 C++ 表一一对应，并额外编码 signedness）：

| 配置类 | 组合 | signedness |
| --- | --- | --- |
| `MMAConfig_U8_U8_S32` | `i8×i8→i32` | unsigned/unsigned |
| `MMAConfig_S8_S8_S32` | `i8×i8→i32` | signed/signed |
| `MMAConfig_E4M3_E4M3_F32` | `f8E4M3FN×f8E4M3FN→f32` | — |
| `MMAConfig_E4M3_E4M3_F16` | `f8E4M3FN×f8E4M3FN→f16` | — |
| `MMAConfig_E5M2_E5M2_F32` | `f8E5M2×f8E5M2→f32` | — |
| `MMAConfig_E5M2_E5M2_F16` | `f8E5M2×f8E5M2→f16` | — |
| `MMAConfig_F16_F16_F32` | `f16×f16→f32` | — |
| `MMAConfig_F16_F16_F16` | `f16×f16→f16` | — |
| `MMAConfig_BF16_BF16_F32` | `bf16×bf16→f32` | — |
| `MMAConfig_F32_F32_F32` | `f32×f32→f32` | — |
| `MMAConfig_TF32_TF32_F32` | `tf32×tf32→f32` | — |
| `MMAConfig_F64_F64_F64` | `f64×f64→f64` | — |

`MMAScaledConfig` 全部 5 种：

| 配置类 | 操作数 | 缩放 | scale_factor |
| --- | --- | --- | --- |
| `MMAScaledConfig_E5M2_E8M0` | `f8E5M2` | `f8E8M0FNU` | 32（MXFP8） |
| `MMAScaledConfig_E4M3_E8M0` | `f8E4M3FN` | `f8E8M0FNU` | 32（MXFP8） |
| `MMAScaledConfig_E2M1_E8M0` | `f4E2M1FN` | `f8E8M0FNU` | 32（MXFP4，2×） |
| `MMAScaledConfig_E2M1_E4M3` | `f4E2M1FN` | `f8E4M3FN` | 16（NVFP4） |
| `MMAScaledConfig_E2M1_E8M0_4X` | `f4E2M1FN` | `f8E8M0FNU` | 16（MXFP4，4×） |

#### 4.3.3 源码精读

**`MMAConfig` 基类与匹配方法**：

[cuda_tile_ops.py:455-489](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L455-L489) 定义基类，字段为 `lhs_dtype/rhs_dtype/acc_dtype/lhs_signed/rhs_signed`；`matches_types`（[cuda_tile_ops.py:480-489](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L480-L489)）把配置的 dtype 转成 MLIR 类型后与传入类型逐一比较。

具体配置类集中在 [cuda_tile_ops.py:493-638](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L493-L638)，例如 [cuda_tile_ops.py:569-578](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L569-L578) 的 `MMAConfig_F16_F16_F32`。

**自动发现与缓存**：

[cuda_tile_ops.py:645-666](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L645-L666) `_initialize_mma_configs` 用 `MMAConfig.__subclasses__()` 自动枚举全部子类并实例化，结果缓存进模块级 `_SUPPORTED_MMA_CONFIGS`，保证只构造一次。

**`MMAScaledConfig` 体系**：

[cuda_tile_ops.py:694-727](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L694-L727) 定义基类，多了 `scale_factor` 字段，默认规则见 [cuda_tile_ops.py:709-712](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L709-L712)（`e4m3` 缩放默认 16，其余默认 32）。5 个具体类见 [cuda_tile_ops.py:732-788](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L732-L788)，自动发现逻辑同构于普通配置，见 [cuda_tile_ops.py:795-816](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L795-L816)。

#### 4.3.4 代码实践

**实践目标**：直接用 Python 查询 MMA 配置注册表，列出全部受支持组合，并理解 `matches_types` 的匹配方式。

操作步骤（在已启用 Python 绑定的构建环境中，参考 [u1-l2](u1-l2-repo-and-build.md) 的 `CUDA_TILE_ENABLE_BINDINGS_PYTHON=ON`）：

```python
# 示例代码
from cuda_tile._mlir.ir import Context
from cuda_tile._mlir.dialects.cuda_tile_ops import (
    make_tile_type, Float16, Float32, Float8E4M3FN,
    find_mma_config, get_supported_mma_configs,
)

ctx = Context()  # MLIR 类型需要上下文

# 1) 列出全部受支持的 MMA 配置
for c in get_supported_mma_configs():
    print(c.name)

# 2) 查询 f16 x f16 -> f32 是否受支持
cfg = find_mma_config(Float16.mlir_type, Float16.mlir_type, Float32.mlir_type)
print(cfg)   # 期望: MMAConfig(f16xf16->f32)

# 3) 查询一组未注册的组合
cfg2 = find_mma_config(Float8E4M3FN.mlir_type, Float8E4M3FN.mlir_type, Float32.mlir_type)
# 注意: e4m3 x e4m3 -> f32 其实是注册过的，会命中 MMAConfig_E4M3_E4M3_F32
print(cfg2)
```

需要观察的现象：第 1 步打印出 12 个配置名；第 2、3 步返回对应的 `MMAConfig(...)` 而非 `None`。

进阶：试着调用真正的 `mma()` 入口（[cuda_tile_ops.py:2751-2843](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2751-L2843)），传入一组未注册的组合（例如自造一个 `f16×f16→f64`），观察它抛出的 `TypeError` 是否把全部受支持配置名列出来——这正是 4.4 节要讲的错误提示路径。

预期结果：`get_supported_mma_configs()` 返回 12 项；`find_mma_config` 对合法组合返回配置对象、对非法组合返回 `None`。

> 本地确切的打印顺序与异常文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `MMAConfig.__subclasses__()` 自动发现，而不是维护一个显式列表？

参考答案：自动发现让「新增一种组合 = 新增一个子类」，无需记得去列表里追加，避免遗漏；同时也让配置类定义与注册解耦，降低维护成本。代价是子类必须在被查询前已完成定义（模块加载时即满足）。

**练习 2**：`MMAScaledConfig_E2M1_E8M0` 与 `MMAScaledConfig_E2M1_E8M0_4X` 的操作数与缩放类型完全相同，注册表如何区分二者？

参考答案：靠 `scale_factor` 字段区分（前者 32，后者 16）。`matches_types` 只比类型，故两者都会命中类型匹配；具体用哪个 vecSize 由 C++ 端 `MmaFScaledOp::verify` 根据实际形状算出的 `lhsScaleVecSize` 来裁定，Python 配置主要用于「类型是否合法」的快速判断。

**练习 3**：`MMAConfig` 里 `lhs_signed`/`rhs_signed` 字段对浮点配置有意义吗？

参考答案：没有实际意义，仅对 `mmai` 的两个整数配置（`U8_U8_S32`、`S8_S8_S32`）起作用。浮点配置构造时不传 signedness，默认为 `SIGNED`，但该字段在浮点 `mma()` 路径里从不被读取。

---

### 4.4 find_mma_config 匹配逻辑与统一 mma() 入口

#### 4.4.1 概念说明

上一节定义了「配置注册表」，本节讲「如何使用它」。核心是一个统一入口 `mma(lhs, rhs, acc)`：它对**浮点与整数两类 MMA 都适用**，内部根据累加器元素类型自动分派到 `MmaFOp` 或 `MmaIOp`。这样 Python 用户只需记一个函数名。

匹配失败的体验是本节的设计亮点：当 `find_mma_config` 返回 `None`，`mma()` 不会抛一句干瘪的「类型不支持」，而是调 `get_supported_mma_configs()` 把全部合法配置名拼进错误信息，让用户立刻知道有哪些可选组合。`mmaf_scaled` 的入口 `mmaf_scaled(...)` 走完全对称的逻辑，只是改用 `find_mma_scaled_config` 与缩放配置表。

#### 4.4.2 核心流程

`mma()` 的执行流程：

```
mma(lhs, rhs, acc, signedness_lhs, signedness_rhs):
    ① 校验形状：rank∈{2,3}、三者同秩、批维一致、M/N/K 满足矩阵乘
    ② find_mma_config(lhs_et, rhs_et, acc_et)
        命中 → 得到 mma_config
        未命中 → 抛 TypeError，附 get_supported_mma_configs() 全部名字
    ③ 若 acc.element_type 是 IntegerType → 生成 MmaIOp（带 signedness 属性）
       否则                                → 生成 MmaFOp
```

注意第 ② 步的匹配**同时覆盖整数与浮点配置**（因为 `MMAConfig` 注册表里两类都有），第 ③ 步才根据累加器是否整数来决定生成哪个底层 Op——这是个很巧的统一：用户用同一个 `mma()` 表达 `i8×i8→i32` 与 `f16×f16→f32`。

`mmaf_scaled()` 流程类似，多出缩放 Tile 的形状校验（与 C++ `MmaFScaledOp::verify` 一一对应，源码注释里明确写着 `mirrors verifyMmaShapes` / `mirrors MmaFScaledOp::verify`），并改用 `find_mma_scaled_config`。

#### 4.4.3 源码精读

**`find_mma_config` 与 `get_supported_mma_configs`**：

[cuda_tile_ops.py:669-676](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L669-L676) `find_mma_config` 遍历缓存配置，返回第一个 `matches_types` 命中者，否则 `None`。[cuda_tile_ops.py:679-681](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L679-L681) `get_supported_mma_configs` 直接返回缓存列表。缩放版本的 `find_mma_scaled_config` 在 [cuda_tile_ops.py:819](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L819)。

**统一入口 `mma()`**：

[cuda_tile_ops.py:2751-2843](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2751-L2843) 是完整实现。三段值得细看：

- [cuda_tile_ops.py:2762-2795](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2762-L2795)：形状校验，逻辑与 C++ `verifyMmaShapes` 同构。
- [cuda_tile_ops.py:2802-2820](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2802-L2820)：`find_mma_config` 调用与失败时拼出全部支持配置名的 `TypeError`。
- [cuda_tile_ops.py:2822-2843](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2822-L2843)：按累加器是否整数分派 `MmaIOp` / `MmaFOp`。

`mmaf_scaled()` 入口在 [cuda_tile_ops.py:2847-2953](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2847-L2953)，其中 [cuda_tile_ops.py:2920-2941](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2920-L2941) 是缩放版的「匹配 + 友好报错」。

**类型构造工具** `make_tile_type` 在 [cuda_tile_ops.py:1404-1425](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1404-L1425)，把「元素类型包装 + 形状」转成 `TileType`，是构造 MMA 操作数最常用的人口。

#### 4.4.4 代码实践

**实践目标**：构造 `f16×f16→f32` 的三个 Tile 类型，调用统一 `mma()` 入口（或直接观察 `find_mma_config`），并触发一次「未注册组合」的友好报错。

操作步骤：

```python
# 示例代码
from cuda_tile._mlir.ir import Context
from cuda_tile._mlir.dialects.cuda_tile_ops import (
    make_tile_type, Float16, Float32, Float64,
    find_mma_config,
)

ctx = Context()

# 构造 lhs: tile<4x8xf16>, rhs: tile<8x16xf16>, acc: tile<4x16xf32>
lhs_t = make_tile_type(Float16, [4, 8])
rhs_t = make_tile_type(Float16, [8, 16])
acc_t = make_tile_type(Float32, [4, 16])

# find_mma_config 接收「元素类型」，从 Tile 上取 .element_type
cfg = find_mma_config(lhs_t.element_type, rhs_t.element_type, acc_t.element_type)
print(cfg)   # 期望: MMAConfig(f16xf16->f32)

# 未注册组合：f16 x f16 -> f64
bad = find_mma_config(Float16.mlir_type, Float16.mlir_type, Float64.mlir_type)
print(bad)   # 期望: None
```

> 若要在真正的 IR 构建上下文里调用 `mma(lhs, rhs, acc)` 生成 `MmaFOp`，需要先建立 `cuda_tile.module`/`entry` 与插入点；最稳妥的做法是用 `Module.parse()` 解析一段含 `mmaf` 的文本（参考 `test/python/cuda_tile_public_bindings.py` 的写法）。

需要观察的现象：合法组合返回配置对象；非法组合返回 `None`。若直接调用 `mma(...)` 传非法组合，则抛出 `TypeError` 且消息中列出 `f16xf16->f32, bf16xbf16->f32, ...` 等全部配置名。

预期结果：与上述注释一致。

> 确切的 Python 运行输出待本地验证（依赖带 Python 绑定的构建，见 [u1-l2](u1-l2-repo-and-build.md)）。

#### 4.4.5 小练习与答案

**练习 1**：`mma()` 如何决定生成 `MmaIOp` 还是 `MmaFOp`？为什么这样做是安全的？

参考答案：看 `acc.element_type` 是否为 `IntegerType`（[cuda_tile_ops.py:2822](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2822)）。安全的原因是第 ② 步 `find_mma_config` 已用全表（含 `i8×i8→i32` 两个整数配置）匹配过，能命中整数累加器的只有那两个整数配置，此时累加器必然是 `i32`（IntegerType），分派一致；浮点配置的累加器都是浮点，不会误进整数分支。

**练习 2**：假如 `find_mma_config` 命中了一个配置，但用户传的形状不满足矩阵乘约束，会发生什么？

参考答案：`mma()` 在 [cuda_tile_ops.py:2762-2795](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2762-L2795) 先做形状校验，会在调用 `find_mma_config` 之前就抛 `ValueError`（如 `dim ... of lhs and dim ... of rhs must match`）。形状校验先于类型校验。

**练习 3**：为什么不直接在 Python 端复用 C++ 的 `verify()`，而要维护一份独立的配置表？

参考答案：Python 端构造 IR 时，C++ 对象可能尚未生成（或希望在不触发完整编译的前提下提前拦截），独立的配置表让 Python 能在「生成 Op 之前」就给出面向人类、列出全部可选项的错误信息；同时它也是文档化的单一数据源（与 C++ verifier 一一对应，源码注释里写明 `mirrors ...`）。

---

## 5. 综合实践

把本讲三类 MMA 串起来：写一个最小的 `cuda_tile.module`，用同一段 `M=4, K=8, N=16` 的形状，分别用 `mmaf`（`f16×f16→f32`）、`mmai`（`i8×i8→i32`，一侧 signed 一侧 unsigned）和 `mmaf_scaled`（MXFP8，需自行设计缩放 Tile 形状使 `vecSize=32`）各做一次乘累加。

具体步骤：

1. 计算三种情形各自的操作数/累加器/缩放 Tile 形状与类型，填入下表（待本地验证）：

   | 操作 | lhs | rhs | acc | lhs_scale | rhs_scale |
   | --- | --- | --- | --- | --- | --- |
   | `mmaf` | `tile<4x8xf16>` | `tile<8x16xf16>` | `tile<4x16xf32>` | — | — |
   | `mmai` | `tile<4x8xi8>` | ? | ? | — | — |
   | `mmaf_scaled` | `tile<4x8xf8E5M2>` | ? | ? | ? | ? |

2. 把三段写成 `cuda_tile.module`，用 `cuda-tile-opt` 验证全部通过。
3. 故意把 `mmaf_scaled` 的 `rhs_scale` 形状写错（让 K 维不整除），观察 `MmaFScaledOp::verify` 的诊断。
4. （进阶）在 Python 端用 `get_supported_mma_configs()` 与 `get_supported_mma_scaled_configs()` 打印两张表，与自己写下的形状/类型对照，确认理解一致。

参考答案要点：
- `mmai`：`rhs = tile<8x16xi8>`、`acc = tile<4x16xi32>`，可写 `mmai ... signed unsigned`。
- `mmaf_scaled`（MXFP8，`vecSize=32`，K=8 不满足 32 的约束，需把 K 调到 32 的倍数，例如 K=32）：`lhs = tile<4x32xf8E5M2>`、`rhs = tile<32x16xf8E5M2>`、`acc = tile<4x16xf32>`、`lhs_scale = tile<4x1xf8E8M0FNU>`（`32/32=1`）、`rhs_scale = tile<1x16xf8E8M0FNU>`。

> 提示：MXFP8 要求 `vecSize=32`，故 K 必须是 32 的倍数；本练习意在让你亲手体会到「形状与 vecSize 的耦合」。

## 6. 本讲小结

- **MMA 是张量核的原子操作**：一次完成 \(C \mathrel{+}= A \times B\)，自带累加，比分离的 `mulf`+`addf` 精度更高、吞吐更大。
- **三类操作按精度分工**：`mmaf`（浮点，7 种输入类型组合）、`mmai`（`i8×i8→i32`，signedness 可混合）、`mmaf_scaled`（块缩放低精度，固定 `f32` 累加，13.3 引入）。
- **形状约束统一**：三者共享 `verifyMmaShapes`，要求 2D（`M×K, K×N, M×N`）或 3D 批量形态。
- **块缩放的关键是 vecSize**：`f8+e8m0` 要求 32，`f4+e4m3` 要求 16，`f4+e8m0` 允许 16 或 32；`MmaFScaledOp::verify` 强制这些取值。
- **Python 注册表与 C++ verifier 对偶**：`MMAConfig`/`MMAScaledConfig` 用子类自动发现登记全部合法组合，`find_mma_config` 做匹配，统一入口 `mma()` 按累加器是否整数分派 `MmaIOp`/`MmaFOp`。
- **失败即文档**：匹配失败时把全部受支持配置名拼进错误信息，降低排错成本。

## 7. 下一步学习建议

- 本讲的 MMA 操作只「定义」了矩阵乘累加的语义，真正的访存（把全局显存数据搬进 Tile 喂给 MMA）在 [u5-l1 内存模型与 Token 顺序](u5-l1-memory-model-tokens.md) 与 [u5-l2 视图加载与存储](u5-l2-view-load-store.md) 讲解，建议接着读，理解一个完整 GEMM 的 load→mma→store 链路。
- 想了解这些操作如何被序列化进字节码、以及 `sinceVersion`（如 `mmaf_scaled` 的 13.3）如何影响版本兼容，可阅读 [u7-l4 字节码版本与兼容性](u7-l4-bytecode-versioning.md)。
- 想看优化器如何围绕 MMA 做变换（如 FuseFMA），见 [u9-l1 Pass 框架与 FuseFMA 融合](u9-l1-passes-and-fusefma.md)。
- 建议继续精读 [test/Dialect/CudaTile/ops.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir) 中 `mma1`~`test_mmaf_scaled_*` 全部用例，作为本讲的「标准答案集」。
