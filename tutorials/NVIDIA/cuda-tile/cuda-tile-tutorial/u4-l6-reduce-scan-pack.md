# 归约、扫描与低精度打包

## 1. 本讲目标

本讲是「进阶·计算类操作」的收尾篇。前面几讲我们学的操作（`addf`、`mulf`、`mma` 等）都是**逐元素**作用在一个 tile 上的，输入和输出形状一致。本讲要跨出「逐元素」的边界，学习三类**跨元素聚合**与**位级重组**操作：

学完后你应该能够：

- 用 `reduce` 沿某一维度把一个 tile「压扁」，写出归约求和、归约求最值的内核。
- 用 `scan` 沿某一维度做**前缀扫描**（前缀和、前缀积），并理解 `reverse` 的含义与 inclusive 语义。
- 理解 `reduce`/`scan` 的「自带 region 体」设计：归约算子不是写死在操作里的，而是由你在一个 body 区域里定义。
- 用 `pack`/`unpack` 在 `i4`、`i1`、`f4` 等**低精度类型**与 `tile<i8>` 字节数组之间无损转换，理解它在硬件对齐与紧凑存储中的作用。
- 通过 `reduce_and_scan_invalid.mlir` 学会排查这些操作的 verifier 报错。

本讲依赖 u4-l1（核心数据操作），因为你需要在归约体里用到 `addf`、`mulf` 等算术操作，并理解 `constant`、tile 形状等基本概念。

## 2. 前置知识

在阅读本讲前，先建立三个直觉。

**直觉一：归约（reduce）=「折叠」。** 想象你有一排数 \([a_0, a_1, a_2, a_3]\)，归约就是用一个二元运算 \(f\) 把它们「折叠」成一个数：

\[
\text{reduce}(f, a) = f(f(f(a_0, a_1), a_2), a_3)
\]

当 \(f\) 是加法时，这就是求和；当 \(f\) 是取最大值时，这就是求最大值。**关键点：** 归约要求 \(f\) 满足**结合律**（associativity），因为 GPU 上并行归约并不保证按 \(a_0, a_1, a_2, a_3\) 的顺序相加——只要满足结合律，无论怎么分组，结果都一样。

**直觉二：扫描（scan）=「前缀折叠」。** 归约只给出最终一个数，而扫描保留**每一步的中间结果**：

\[
\text{scan}(f, a) = [\,a_0,\; f(a_0, a_1),\; f(f(a_0, a_1), a_2),\; f(f(f(a_0, a_1), a_2), a_3)\,]
\]

这就是「前缀和」（prefix sum）。它在 CUDA 里无处不在：流式 softmax、cumsum、并行程依赖计算都靠它。

**直觉三：单位元（identity）=「中性元素」。** 归约和扫描都需要一个**单位元**作为累加的起点。单位元是「参与运算但不改变结果」的值，它是 body 中那个二元运算 \(f\) 的性质：

| 归约算子 | 单位元 |
|---|---|
| 求和 `addf` | \(0\) |
| 求积 `mulf` | \(1\) |
| 求最小值 `minf` | \(+\infty\) |
| 求最大值 `maxf` | \(-\infty\) |

如果单位元填错（比如求和却填了 1），结果就会系统性偏差。

**关于位级重组（pack/unpack）：** 这个概念和归约/扫描无关，放在同一讲是因为它同属 Core 操作分组，且都处理 tile 的「整体」而非「逐元素」。`pack` 把一个低精度 tile（如 `tile<8xi4>`，每个元素 4 位）整体重新解释为一个 `tile<i8>` 字节数组（`tile<4xi8>`），`unpack` 是其逆操作。它和 u4-l4 讲过的 `bitcast` 类似——**不改二进制位，只改解读方式**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | 操作的 TableGen 定义，是本讲的语义权威来源（`reduce`/`scan`/`pack`/`unpack` 的声明、描述、数学定义都在这里） |
| `lib/Dialect/CudaTile/IR/CudaTile.cpp` | 操作的 C++ verifier 实现，告诉我们「什么样的 IR 合法、什么样的会被拒绝」 |
| `test/Dialect/CudaTile/reduce_and_scan_invalid.mlir` | `reduce`/`scan` 的非法用例集，每条用例对应一个 verifier 报错 |
| `test/Dialect/CudaTile/ops.mlir` | `reduce`/`scan`/`pack`/`unpack` 的**合法**用例，是正确写法的范本 |
| `python/cuda_tile/dialects/cuda_tile_ops.py` | Python 高层 API，把 region 体封装成回调函数 |

## 4. 核心概念与源码讲解

### 4.1 归约操作 reduce

#### 4.1.1 概念说明

`reduce` 沿 tile 的某一维度做归约，**把该维度压扁掉**。对一个 `tile<4x8xf32>`：

- `dim=0`：压掉第 0 维（大小 4），结果是 `tile<8xf32>`（每一列求一个值，共 8 列）。
- `dim=1`：压掉第 1 维（大小 8），结果是 `tile<4xf32>`（每一行求一个值，共 4 行）。

CUDA Tile 的 `reduce` 有一个特别的设计：**归约算子不是写死的枚举**（不像有些框架里写 `reduce(SUM, x)`），而是由你在操作自带的 **region 体（body）** 里定义。这意味着同一个 `reduce` 既能求和（体里写 `addf`）、求积（体里写 `mulf`），也能做更复杂的自定义聚合。

#### 4.1.2 核心流程

`reduce` 的整体执行流程是：

1. 取一个或多个形状**完全相同**的输入 tile（变长参数 `operands`）。
2. 为每个输入提供一个**单位元** `identities[i]`，其元素类型必须与 `operands[i]` 匹配。
3. 沿 `dim` 维度遍历，对每个操作数独立调用 body 里的二元函数 \(f\)，逐步累加。
4. body 接收 \(2N\) 个参数（\(N\) 为操作数个数），按 `[op_0_当前元素, op_0_累加器, op_1_当前元素, op_1_累加器, ...]` 排列。
5. body 用 `yield` 返回新的累加器值。
6. 输出 tile 的形状 = 输入形状**去掉 `dim` 那一维**。

数学上，对一个一维 tile \(X\)：

\[
\text{reduce}(X, f, \text{identity}) = f(\text{identity}, X[0]) \triangleright X[1] \triangleright \cdots \triangleright X[n-1]
\]

其中 \(\triangleright\) 表示「与下一个元素做 \(f\)」。**重要提醒：** 沿维度的归约顺序**没有保证**，但因为 \(f\) 必须满足结合律，结果在同一设备上多次运行是确定的。

> ⚠️ **只有 pure（无副作用）操作允许出现在 body 内。** 在归约体里调用 `print_tko` 等副作用操作会被 verifier 拒绝。

#### 4.1.3 源码精读

`reduce` 的定义在 Ops.td，关键字段如下：

[`include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3881-3970`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3881-L3970) — `CudaTile_ReduceOp` 的完整定义，包含其描述、数学说明、参数与 assembly 格式。

几个要点逐一看：

- 它带 `SameOperandsShape`（所有操作数形状必须一致）、`SingleBlockImplicitTerminator<"YieldOp">`（body 单块、用 `yield` 隐式终止）、`RecursiveMemoryEffects`、`InferTypeOpAdaptor`（结果类型可推断）等 trait。见 [`Ops.td:3884-3888`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3884-L3888)。

- 描述里明确单位元是 body 函数的性质：[`Ops.td:3907-3912`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3907-L3912)（求和用 0，求最小值用 +inf）。

- body 参数约定：[`Ops.td:3914-3919`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3914-L3919) —「每对参数 \(2i\) 与 \(2i+1\) 对应第 \(i\) 个输入；第一个是当前元素，第二个是累加器」。

- 归约顺序无保证：[`Ops.td:3921-3924`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3921-L3924)。

结果类型如何推断？看 C++ 端的 `inferReturnTypes`：它遍历输入形状，**跳过 `dim` 那一维**，拼出新形状。

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:4943-4962`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4943-L4962) — `ReduceOp::inferReturnTypes`：对每个操作数，复制形状但剔除 `dim` 维度。

这正是「reduce 压扁维度」的实现。合法写法范本见 ops.mlir：

[`test/Dialect/CudaTile/ops.mlir:842-846`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L842-L846) — 对 `tile<8xf32>` 沿 dim=0 求和，body 里 `addf %arg0_in, %arg0_identity`，结果 `tile<f32>`（标量 tile）。

#### 4.1.4 代码实践

**实践目标：** 写一个对 `4x8` tile 分别按行、按列求和的 `reduce`，并用 `cuda-tile-opt` 验证。

**操作步骤：** 新建文件 `reduce_sum.mlir`，内容如下（示例代码）：

```mlir
cuda_tile.module @kernels {
  entry @reduce_sum() {
    // 一个 4x8 的 f32 tile（此处用 broadcast 构造全 1，简化常数书写）
    %one = constant <f32: 1.0> : tile<4x8xf32>
    // 沿 dim=1 压掉大小为 8 的列维 -> 每行求和，结果 tile<4xf32>
    %row_sum = reduce %one dim=1 identities=[0.0 : f32] : tile<4x8xf32> -> tile<4xf32>
      (%elem: tile<f32>, %acc: tile<f32>) {
        %s = addf %elem, %acc : tile<f32>
        yield %s : tile<f32>
      }
    // 沿 dim=0 压掉大小为 4 的行维 -> 每列求和，结果 tile<8xf32>
    %col_sum = reduce %one dim=0 identities=[0.0 : f32] : tile<4x8xf32> -> tile<8xf32>
      (%elem: tile<f32>, %acc: tile<f32>) {
        %s = addf %elem, %acc : tile<f32>
        yield %s : tile<f32>
      }
    return
  }
}
```

运行：

```bash
cuda-tile-opt reduce_sum.mlir
```

**需要观察的现象：** 两条 `reduce` 都能通过验证、原样打印；注意结果形状分别是 `tile<4xf32>` 和 `tile<8xf32>`，对应被压掉的维度不同。

**预期结果：** 无报错，IR 原样回显，证明单位元 `0.0`（与 `addf` 匹配）、body 参数对（`elem`/`acc`）、结果形状推断均正确。

> 待本地验证：`constant <f32: 1.0> : tile<4x8xf32>` 是否按广播语义填满整个 tile。若该写法不被接受，可改用 `constant <f32: [1.0,...]>` 显式列出 32 个值，或先用 `constant` 构造标量 tile 再 `broadcast`。

#### 4.1.5 小练习与答案

**练习 1：** 把上面的求和改成求每行的**乘积**，body 与单位元该怎么改？
**答案：** body 里把 `addf` 换成 `mulf`，单位元从 `0.0` 改成 `1.0`（因为乘法单位元是 1）。类型和形状不变。

**练习 2：** 若把 `identities` 写成 `[0.0 : f32, 0.0 : f32]`（两个单位元）但只有一个操作数，verifier 会报什么？
**答案：** 报 `expect identities to match the number of operands but got: 1 operands and 2 identities`。单位元个数必须等于操作数个数（见 [`reduce_and_scan_invalid.mlir:40`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L40)）。

---

### 4.2 扫描操作 scan

#### 4.2.1 概念说明

`scan` 计算**前缀扫描**：和 `reduce` 一样沿某一维度遍历，但它**保留每一步的中间累加值**，输出形状与输入形状**完全相同**。对一个一维 tile，inclusive（包含当前元素）前缀和为：

\[
\text{result}[j] = f(\text{result}[j-1],\; X[j]), \qquad \text{result}[0] = f(\text{identity},\; X[0])
\]

> ⚠️ **关于 inclusive/exclusive：** CUDA Tile 的 `scan` 实现**只有 inclusive 模式**（结果包含当前元素）。源码描述与数学公式都明确这一点（见下方源码精读）。它**没有**内置的 exclusive 模式。如需 exclusive 前缀和（结果不含当前元素），可由 inclusive 结果移位得到：exclusive\([j]\) = inclusive\([j-1]\)，且 exclusive\([0]\) = identity。这是后续 lowering 阶段的处理，IR 层只提供 inclusive。

`scan` 还比 `reduce` 多一个 **`reverse`** 布尔属性：为 `true` 时，前缀沿维度**反向**取（从最后一个元素往前累加），常用于需要「后缀和」的场景。

#### 4.2.2 核心流程

1. 取一个输入 tile（**文档限定 scan 只支持单 tile 输入**，见源码中的 warning）。
2. 提供 `dim`、`reverse`、`identities`。
3. body 同样接收 `[当前元素, 累加器]` 一对参数，第一步的累加器是 identity。
4. 输出 tile 形状 = 输入形状（**不变**），逐位置写入前缀结果。

正向（`reverse=false`）与反向（`reverse=true`）的对比，设 \(N\) 为扫描维长度：

\[
\text{scan}_{\text{fwd}}(X)[j] = \text{fold}(f, \text{identity},\; X[0], X[1], \dots, X[j])
\]

\[
\text{scan}_{\text{rev}}(X)[j] = \text{fold}(f, \text{identity},\; X[N\!-\!1], \dots, X[j])
\]

结合律同样允许编译器重排运算顺序，以在 GPU 上做高效的并行前缀扫描，但结果对同一设备是确定的。

#### 4.2.3 源码精读

[`include/cuda_tile/Dialect/CudaTile/IR/Ops.td:4135-4229`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4135-L4229) — `CudaTile_ScanOp` 完整定义。

几个关键点：

- summary 直接写明是「inclusive parallel prefix」：[`Ops.td:4139`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4139)。

- inclusive 数学定义：[`Ops.td:4164-4168`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4164-L4168)，可见 `result[0] = f(identity, X[..., 0, ...])`，包含第 0 号元素，确属 inclusive。

- `reverse` 的数学含义：[`Ops.td:4170-4176`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4170-L4176)。

- 单 tile 限制（warning）：[`Ops.td:4195-4197`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4195-L4197)。

- 参数比 `reduce` 多了 `reverse`（`BoolAttr`）：[`Ops.td:4213-4216`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4213-L4216)。

与 `reduce` 在 C++ verifier 上的**唯一关键差异**是：`scan` 调用 `verifyAggregateOp` 时传了 `requiresMatchingReturnShape=true`，而 `reduce` 没传（默认 false）。对比两处：

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:4938-4941`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4938-L4941) — `ReduceOp::verify`：`requiresMatchingReturnShape` 取默认值 false（结果形状可不同，因为维度被压扁）。

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:5076-5080`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5076-L5080) — `ScanOp::verify`：显式传 `true`，强制要求结果形状与操作数形状一致。

这正好印证了「reduce 压扁维度、scan 保留形状」。`verifyAggregateOp` 中那段 `requiresMatchingReturnShape` 检查见 [`CudaTile.cpp:4909-4924`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4909-L4924)，shape 不一致时报 `expect same type for operand at index: i and result at index: i`。

合法写法范本见 [`test/Dialect/CudaTile/ops.mlir:887-891`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L887-L891)：对 `tile<8xf32>` 沿 dim=0 做 inclusive 前缀和，输入输出都是 `tile<8xf32>`。

#### 4.2.4 代码实践

**实践目标：** 对 `4x8` tile 沿列方向做 inclusive 前缀和，并对比 `reverse=true` 的效果。

**操作步骤：** 新建 `scan_sum.mlir`（示例代码）：

```mlir
cuda_tile.module @kernels {
  entry @scan_sum() {
    %x = constant <f32: 1.0> : tile<4x8xf32>
    // 沿 dim=1 做 inclusive 前缀和：每一行变成 [x0, x0+x1, ...]
    %prefix = scan %x dim=1 reverse=false identities=[0.0 : f32]
        : tile<4x8xf32> -> tile<4x8xf32>
      (%elem: tile<f32>, %acc: tile<f32>) {
        %s = addf %elem, %acc : tile<f32>
        yield %s : tile<f32>
      }
    // 反向扫描：每一行变成 [..., x6+x7, x7]（后缀和）
    %suffix = scan %x dim=1 reverse=true identities=[0.0 : f32]
        : tile<4x8xf32> -> tile<4x8xf32>
      (%elem: tile<f32>, %acc: tile<f32>) {
        %s = addf %elem, %acc : tile<f32>
        yield %s : tile<f32>
      }
    return
  }
}
```

运行 `cuda-tile-opt scan_sum.mlir`。

**需要观察的现象：** 两条 `scan` 都验证通过；输出形状与输入完全相同（`4x8`），与 `reduce` 的「压扁」形成对比。

**预期结果：** 无报错。若把结果类型误写成 `tile<4xf32>`（像 reduce 那样），会触发 `expect same type for operand at index: 0 and result at index: 0`（见 [`reduce_and_scan_invalid.mlir:395`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L395)）。

> 待本地验证：`reverse` 在文本 IR 中的精确渲染顺序以 `cuda-tile-opt` 实际输出为准。

#### 4.2.5 小练习与答案

**练习 1：** 用 `scan` 求前缀**积**（而非前缀和），body 和单位元如何调整？
**答案：** body 用 `mulf`（可附 `rounding<nearest_even>`），单位元用 `1.0`。参考 [`Ops.td:4200-4211`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4200-L4211) 的官方示例。

**练习 2：** `scan` 没有「exclusive」开关，若算法确实需要 exclusive 前缀和，怎么办？
**答案：** 先用 `scan` 得到 inclusive 结果 \(I\)，再通过移位/`extract` 构造 exclusive：exclusive\([0]\)=identity，exclusive\([j]\)=\(I[j-1]\)。这属于 lowering 或前端层面的处理，IR 原生只给 inclusive。

**练习 3：** 把 `reverse` 误写成 `identities` 之外的非法值（例如漏写 `reverse=false`），会发生什么？
**答案：** `reverse` 是必填的 `BoolAttr` 参数，缺失会导致 assembly 解析失败（操作格式要求 `reverse = <bool>`）。

---

### 4.3 低精度打包与解包 pack / unpack

#### 4.3.1 概念说明

`pack` 和 `unpack` 处理的是**位级重组**，与归约/扫描无关，但同属 Core 分组、都作用于 tile 的「整体」。

NVIDIA 张量核支持大量**低精度类型**：`i4`（4 位整数）、`i1`（1 位）、`f4E2M1FN`（4 位浮点）、`fp8` 等。这些类型每个元素不足 1 字节，但硬件访存与全局显存通常以**字节（8 位）**为最小寻址单位。`pack` 把一个低精度 tile 整体重新解释为 `tile<i8>` 字节数组，便于存储与对齐；`unpack` 是逆操作。

核心性质（与 u4-l4 的 `bitcast` 一致）：**二进制位不变，只是解读方式改变**。但与逐元素的 `bitcast` 不同，`pack`/`unpack` 是**整块重组**，且专门处理「位宽不成整字节」的情形。

#### 4.3.2 核心流程

`pack` 的约束：

1. 输入与输出都必须是 **rank-1**（一维）tile，消除打包歧义。
2. 输入不能是 8 位类型（那种情况应直接用 `bitcast`）。
3. 输入与输出的**位宽不同**。
4. 输入与输出的**总位数（字节数）相同**。

位换算示例（两个 `i4` 元素正好装进一个 `i8` 字节）：

\[
\text{tile}<2 \times i4\rangle \;\xrightarrow{\text{pack}}\; \text{tile}<1 \times i8\rangle \qquad (2 \times 4\text{ 位} = 8\text{ 位} = 1\text{ 字节})
\]

更一般的：`tile<N×i4>` → `tile<(N/2)×i8>`；`tile<N×i1>` → `tile<(N/8)×i8>`。

`unpack` 是反向：`tile<i8>` → 低精度 tile，总字节数同样必须匹配。

#### 4.3.3 源码精读

[`include/cuda_tile/Dialect/CudaTile/IR/Ops.td:3648-3691`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3648-L3691) — `CudaTile_PackOp` 定义（sinceVersion 13.3）。

要点：

- 输入元素类型 `PackOp_UnpackedType` = `NumberTileType ∪ Int4`（允许 i4！）：[`Ops.td:3652-3653`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3652-L3653)。

- 输出固定为 `tile<i8>`：[`Ops.td:3686`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3686)。

- 描述说明「整块重组、非逐元素、输入输出 rank-1、不能是 8 位」：[`Ops.td:3658-3667`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3658-L3667)。

[`include/cuda_tile/Dialect/CudaTile/IR/Ops.td:4736-4779`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4736-L4779) — `CudaTile_UnpackOp`，结构与 `pack` 对称，输入 `tile<i8>`、输出 `UnpackOp_UnpackedType`。

C++ 端 `pack`/`unpack` 共享同一个 verifier 模板，逻辑非常清晰：

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:4612-4654`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4612-L4654) — `verifyPackUnpackTypes` 模板：依次校验 rank=1、输入输出位宽不同、双方字节对齐、总字节数相等。

特别留意 `getTileSizeInBits` 如何处理**亚字节类型**：`i1` 记 1 位、`tf32` 记 32 位，其余取 `getIntOrFloatBitWidth()`（`i4` 即得 4 位）。见 [`CudaTile.cpp:4616-4626`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4616-L4626)。正因为这里正确计入 4 位，`tile<N×i4>` 的总位数才等于 `N×4`，从而能和 `tile<(N/2)×i8>` 的 `N/2×8` 对齐。

合法写法范本（注意每对 i4/i1 元素装进一个字节的比例关系）：

[`test/Dialect/CudaTile/ops.mlir:1318-1325`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1318-L1325) — `pack` 用例：`tile<16xi1>→tile<2xi8>`、`tile<64xi4>→tile<32xi8>`、`tile<64xi16>→tile<128xi8>`、`tile<64xf32>→tile<256xi8>`。

[`test/Dialect/CudaTile/ops.mlir:1309-1314`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1309-L1314) — `unpack` 用例：`tile<64xi8>→tile<512xi1>`、`tile<64xi8>→tile<128xi4>`、`tile<64xi8>→tile<128xf4E2M1FN>`。

#### 4.3.4 代码实践

**实践目标：** 把 `i4` tile 打包成 `i8` 字节数组再解包还原，验证 round-trip。

**操作步骤：** 新建 `pack_unpack.mlir`，用函数参数风格（沿用 ops.mlir 的写法，避免对 i4 `constant` 语法的假设），示例代码如下：

```mlir
cuda_tile.module @kernels {
  testing$func @pack_roundtrip(%lo: !cuda_tile.tile<8xi4>) {
    // 8 个 i4 元素 = 32 位 = 4 字节
    %packed = pack %lo : tile<8xi4> -> tile<4xi8>
    // 逆操作：4 字节还原回 8 个 i4
    %back = unpack %packed : tile<4xi8> -> tile<8xi4>
    yield
  }
}
```

运行 `cuda-tile-opt pack_unpack.mlir`。

**需要观察的现象：** `pack` 把 `tile<8xi4>` 变成 `tile<4xi8>`（每两个 i4 装进一个 i8），`unpack` 又还原为 `tile<8xi4>`。两个操作的字节总数都是 4，符合 `verifyPackUnpackTypes` 的「总字节数相等」约束。

**预期结果：** 无报错。因为 `pack`/`unpack` 都是「不改位、只改解读」的纯重组，round-trip 在位级别严格可逆：`unpack(pack(x))` 的二进制位与 `x` 完全一致。

> 待本地验证：`testing$func` 与 `yield` 仅在开启测试（`TILE_IR_INCLUDE_TESTS`）时可用；若用普通构建，请把外层换成 `entry` 并把 `%lo` 改为 `constant` 构造（参考 ops.mlir 中 `pack` 的常数写法）。

**对照错误写法：** 若把 `pack` 写成 `tile<8xi8> -> tile<8xi8>`（输入也是 8 位），verifier 会报 `expects source and result to have different element type widths`；若总字节数不匹配（如 `tile<8xi4> -> tile<8xi8>`，32 位 ≠ 64 位），会报 `expects source and result to have the same size in bytes`。这两条信息来自 [`CudaTile.cpp:4633-4651`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4633-L4651)。

#### 4.3.5 小练习与答案

**练习 1：** `pack tile<64xi16>` 的结果类型是什么？为什么？
**答案：** `tile<128xi8>`。`64×16 位 = 1024 位 = 128 字节`，输出是 `tile<i8>`，故 128 个 i8。对照 [`ops.mlir:1322-1323`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/ops.mlir#L1322-L1323)。

**练习 2：** 为什么 `pack` 要求输入输出都是 rank-1？
**答案：** 多维 tile 的字节排布存在行主序/列主序歧义，强制 rank-1 可消除打包顺序的二义性（见 [`Ops.td:3664-3666`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L3664-L3666)）。需要打包多维数据时，应先 `reshape` 成一维。

**练习 3：** `pack`/`unpack` 与 `bitcast` 有何异同？
**答案：** 相同点：都不改二进制位、都是位级重解释。不同点：`bitcast` 要求**同位宽**且逐元素；`pack`/`unpack` 处理**不同位宽**（含亚字节类型如 i4/i1）、整块重组、且输入输出固定为一维与 `tile<i8>`。

---

### 4.4 错误排查：reduce_and_scan_invalid 测试

#### 4.4.1 概念说明

`test/Dialect/CudaTile/reduce_and_scan_invalid.mlir` 是一份「反面教材」集合。它用 FileCheck 的 `expected-error` 机制，把 `reduce`/`scan` 几乎所有会被 verifier 拒绝的写法各列一条。读懂这份测试，等于掌握了这两个操作的**全部合法性约束**。

这份测试的结构是：每段用 `// -----` 分隔，每段顶部用 `// expected-error @below{{...}}` 声明「下面这行操作应当报这个错」，再用 `-verify-diagnostics` 让 `cuda-tile-opt` 校验报错文本是否匹配。

#### 4.4.2 核心流程

`reduce`/`scan` 的 verifier 由两个共享函数实现：

- `verifyAggregateOp`：检查**操作本身**（操作数/结果/单位元/dim 的数量与类型关系）。
- `verifyAggregateOpRegions`：检查 **body region**（块参数数量、rank-0、配对类型一致、terminator 一致、体内只允许 pure 操作）。

报错信息按这两层组织，便于定位。

#### 4.4.3 源码精读

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:4751-4850`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4751-L4850) — `verifyAggregateOpRegions`：逐条对应测试里的报错：

- 块参数数量必须为 \(2N\)：报 `expect 2 block arguments but got: ...`（scan，[`reduce_and_scan_invalid.mlir:247`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L247)）。reduce 对应「region with 1 blocks」约束（[`reduce_and_scan_invalid.mlir:18`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L18)）。
- 每个块参数必须是 **0-rank tile**：报 `expect 0-rank tile type at index: i`（[`reduce_and_scan_invalid.mlir:52`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L52) 与 [`:64`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L64)）。
- 成对参数元素类型一致：报 `expect same element type for block argument at index: 2i and 2i+1`（[`reduce_and_scan_invalid.mlir:76`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L76)）。
- 块参数类型须匹配操作数类型：报 `expect same type for operand at index: i and block argument ...`（[`reduce_and_scan_invalid.mlir:116`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L116)）。
- terminator 操作数数量/类型：报 `expect number of terminators operands ... to match ...`（[`reduce_and_scan_invalid.mlir:130`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L130)）。
- 体内只允许 pure 操作：报 `only pure operations are allowed inside 'cuda_tile.reduce'`（[`reduce_and_scan_invalid.mlir:463`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L463)）。

[`lib/Dialect/CudaTile/IR/CudaTile.cpp:4854-4927`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4854-L4927) — `verifyAggregateOp`：对应操作级报错：

- 操作数与结果数量一致：`expect same number of operands and results`（[`reduce_and_scan_invalid.mlir:5`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L5)）。
- 单位元数量与操作数一致：`expect identities to match the number of operands`（[`reduce_and_scan_invalid.mlir:40`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L40)）。
- 单位元类型须匹配操作数元素类型：`expect same type for operand at index: i and identity at index: i`（[`reduce_and_scan_invalid.mlir:188`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L188)）。
- 所有操作数同形状：`requires the same shape for all operands`（由 `SameOperandsShape` trait 产出，[`reduce_and_scan_invalid.mlir:158`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L158)）。
- dim 非负：`attribute 'dim' failed to satisfy constraint: ... non-negative`（[`reduce_and_scan_invalid.mlir:203`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L203)）。
- dim 不越界：`dimension (10) is out of bound [0, 1)`（[`reduce_and_scan_invalid.mlir:218`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L218)）。

#### 4.4.4 代码实践

**实践目标：** 自己构造一条非法 `reduce`，**先预测** verifier 报错，再用 `-verify-diagnostics` 验证预测。

**操作步骤：** 新建 `my_invalid.mlir`（示例代码），故意把单位元类型写错（`f32` 操作数配 `i32` 单位元）：

```mlir
// RUN: cuda-tile-opt %s -verify-diagnostics -allow-unregistered-dialect -split-input-file

cuda_tile.module @kernels {
  testing$func @bad_reduce(%arg0: !cuda_tile.tile<8xi32>) {
    // 预测报错：expect same type for operand at index: 0 and identity at index: 0
    //          but got: 'i32' and 'f32'
    %0 = cuda_tile.reduce %arg0 dim=0 identities=[0.0 : f32]
        : !cuda_tile.tile<8xi32> -> !cuda_tile.tile<i32>
      (%iter_arg : !cuda_tile.tile<i32>, %prev : !cuda_tile.tile<i32>) {
        cuda_tile.yield %iter_arg : !cuda_tile.tile<i32>
      }
  }
}
```

运行 `cuda-tile-opt my_invalid.mlir -verify-diagnostics`。

**需要观察的现象：** 工具会比对 `expected-error` 注释与实际报错；若你的预测文本与实际一致则测试通过，否则报告 mismatch。

**预期结果：** 实际报错为 `expect same type for operand at index: 0 and identity at index: 0 but got: 'i32' and 'f32'`，与 `reduce_and_scan_invalid.mlir:188` 同源（[`CudaTile.cpp:4888-4894`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4888-L4894)）。

> 待本地验证：`testing$func` 需开启 `TILE_IR_INCLUDE_TESTS`；用 `entry` 时可去掉 `testing$` 前缀并把函数体调整为合法的 entry 形态来观察同类报错。

#### 4.4.5 小练习与答案

**练习 1：** 在 `reduce` 的 body 里写一个 `print_tko`，会触发什么报错？出自哪段源码？
**答案：** 报 `'cuda_tile.print_tko' op only pure operations are allowed inside 'cuda_tile.reduce'`，由 `verifyAggregateOpRegions` 末尾对体内每个操作做 `isPure` 检查产生（[`CudaTile.cpp:4843-4848`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4843-L4848)），测试见 [`reduce_and_scan_invalid.mlir:463`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/reduce_and_scan_invalid.mlir#L463)。

**练习 2：** 为什么 `scan` 报「operand/result 形状不一致」而 `reduce` 不会？
**答案：** 因为 `ScanOp::verify` 传了 `requiresMatchingReturnShape=true`，强制结果形状等于操作数形状；`ReduceOp` 不传（默认 false），它通过 `inferReturnTypes` 压掉 `dim` 维，结果形状本就不同。见 [`CudaTile.cpp:4938-4941`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L4938-L4941) 与 [`CudaTile.cpp:5076-5080`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5076-L5080)。

---

## 5. 综合实践

把本讲三个主题串成一个任务：**对一组整数数据做「行求和 → 行前缀和 → 打包存储」**。

设计一个内核（示例代码，用于 `cuda-tile-opt` 验证合法性）：

```mlir
cuda_tile.module @kernels {
  entry @pipeline() {
    // 1) 构造一个 4x8 的 i32 数据 tile（用 iota+广播或 constant 列举）
    %data = constant <i32: 1> : tile<4x8xi32>
    // 2) 每行求和：压掉 dim=1（大小 8），得到 tile<4xi32>
    %row_sum = reduce %data dim=1 identities=[0 : i32]
        : tile<4x8xi32> -> tile<4xi32>
      (%e: tile<i32>, %acc: tile<i32>) {
        %s = addi %e, %acc : tile<i32>
        yield %s : tile<i32>
      }
    // 3) 对这 4 个行和做 inclusive 前缀和（仍为 tile<4xi32>）
    %prefix = scan %row_sum dim=0 reverse=false identities=[0 : i32]
        : tile<4xi32> -> tile<4xi32>
      (%e: tile<i32>, %acc: tile<i32>) {
        %s = addi %e, %acc : tile<i32>
        yield %s : tile<i32>
      }
    // 4) 这一步留给读者：把 %prefix（i32）reshape 成一维后用 pack 存为字节。
    //    注意 pack 要求 rank-1，且 i32→i8 需先 reshape 到一维：
    //      %flat = reshape %prefix : tile<4xi32> -> tile<4xi32>   // 已是一维
    //      %bytes = pack %flat : tile<4xi32> -> tile<16xi8>
    return
  }
}
```

实践要点：

1. 第 2 步 `reduce` 用整数加法 `addi`，单位元 `0`，结果压成 `tile<4xi32>`。
2. 第 3 步 `scan` 保留形状，得到行和的前缀和。
3. 第 4 步把第 3 步结果（已是 rank-1 的 `tile<4xi32>`）`pack` 成 `tile<16xi8>`（\(4 \times 32\) 位 \(= 128\) 位 \(= 16\) 字节）。
4. 用 `cuda-tile-opt` 跑通整段，确认每一步的类型与形状推断都正确。
5. 进阶：尝试把 `reduce` 的 `dim` 改成越界值、或把某步的单位元写错，对照 4.4 节预测 verifier 报错。

> 待本地验证：`constant <i32: 1> : tile<4x8xi32>` 是否按广播填充。若不接受，请用显式 32 元素列表或 `broadcast` 构造。

## 6. 本讲小结

- `reduce` 沿维度把 tile「压扁」，结果形状 = 输入形状去掉 `dim` 维；归约算子由 **body region** 定义，不写死在操作里。
- `scan` 做 **inclusive** 前缀扫描，结果形状与输入**相同**；多一个 `reverse` 属性做反向（后缀）扫描；文档限定单 tile 输入。**无内置 exclusive 模式**，需由 inclusive 移位派生。
- 单位元（identity）是 body 二元运算的性质：求和用 0、求积用 1、最小值用 +inf、最大值用 -inf；单位元个数与元素类型都必须与操作数一一匹配。
- `reduce`/`scan` 的 body 接收 `[当前元素, 累加器]` 配对参数（共 \(2N\) 个），都必须是 **0-rank tile**，且体内**只允许 pure 操作**；归约/扫描顺序不保证，但同设备结果确定。
- `pack`/`unpack`（sinceVersion 13.3）是**整块位级重组**：在低精度 tile（含 `i4`、`i1`、`f4` 等）与 `tile<i8>` 字节数组之间无损转换；要求 rank-1、位宽不同、总字节数相等——服务于低精度紧凑存储与硬件字节对齐。
- `reduce_and_scan_invalid.mlir` 覆盖了 `verifyAggregateOp`（操作级）与 `verifyAggregateOpRegions`（body 级）的全部报错路径，是排查写法错误的最佳索引。

## 7. 下一步学习建议

- **进入内存与控制流（第 5 单元）：** `reduce`/`scan` 产出的标量或 tile 结果，下一步通常要写回显存。建议阅读 u5-l1（内存模型与 Token 顺序）与 u5-l2（视图加载与存储），把本讲的归约结果接上 `store_view_tko`。
- **回顾 FMA 与精度（u4-l3、u4-l5）：** 本讲的 `scan` 体里若用 `mulf`，可对照 u4-l3 的舍入模式属性（如官方 scan 示例里的 `rounding<nearest_even>`），理解浮点前缀积的精度取舍；低精度 `pack`/`unpack` 则与 u4-l5 的 `mmaf_scaled` 块缩放（e8m0/e4m3）紧密配合。
- **动手读 Python API：** 看 [`python/cuda_tile/dialects/cuda_tile_ops.py:4322-4380`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L4322-L4380)，理解 `reduce`/`scan` 如何把 body region 封装成 Python 回调，这是后续 u10-l2（Python 绑定）的预习。
- **测试体系预习：** 本讲反复用到的 `-verify-diagnostics`、`expected-error`、`// -----` 分隔，属于 lit/FileCheck 体系，u10-l3 会系统讲解。
