# 数据搬运 T.copy 与原子写回

## 1. 本讲目标

在上一讲（u3-l1）里，我们学会了「数据该分配到哪块片上存储」——用 `T.alloc_shared` / `T.alloc_fragment` 声明 buffer，再由 `AscendInferBufferScope` pass 把它钉死到 L1 / UB / L0A / L0B / L0C。但分配只是第一步，数据还得在 **GM、L1、UB、L0A/L0B/L0C** 这些层级之间来回搬运，计算才能跑起来。

学完本讲，你应该能够：

- 说清 `T.copy(src, dst)` 这一个原语是如何根据 `src` / `dst` 的存储 scope，自动派发到 Ascend 上不同的 DMA 搬运指令的；
- 列出 `T.copy` 支持的全部搬运路径（GM↔L1、GM↔UB、L1→L0A/L0B、L0C→GM、UB↔UB、UB↔L1、L0C→UB）以及它们各自的语义；
- 理解 `T.tile.atomic_add(dst_gm, src_local)` 的原子累加语义、它的来源/目的 scope 限制，以及为什么调用前必须先清零 GM 输出；
- 把这两个原语用进一个真实的算子里（GM→UB→逐元素计算→GM，以及多 block partial sum 原子累加）。

## 2. 前置知识

本讲默认你已经读过 u3-l1（内存层级与分配原语），知道：

- Ascend 片上有 **GM（Global Memory，设备全局内存）**、**L1（属 Cube 核的片上缓存）**、**UB（Unified Buffer，属 Vector 核）**、**L0A/L0B/L0C（寄存器级，矩阵乘的输入与累加器）**。
- 在 TileLang 前端，buffer 的物理位置用 **scope 字符串**描述：

| scope 字符串 | 物理存储 | 谁在用 |
| --- | --- | --- |
| `global` | GM | 全局 |
| `shared.l1` | L1 | Cube |
| `shared.ub` | UB | Vector |
| `wmma.matrix_a` | L0A | Cube（矩阵乘左矩阵） |
| `wmma.matrix_b` | L0B | Cube（矩阵乘右矩阵） |
| `wmma.accumulator` | L0C | Cube（矩阵乘累加器） |

- 这些 scope 通常不是你手写的，而是 `AscendInferBufferScope` pass 根据 buffer 的使用上下文自动推断出来的。当本讲的 C++ 搬运 pass 运行时，每个 buffer 都已经有了确定的 scope。

还需要一点关于 Ascend DMA 的直觉：Ascend 的「搬运」不是 CPU 风格的 `memcpy`，而是由硬件 DMA 引擎发起的 `DataCopyPad` / `DataCopy` / `Load2D` 等指令。这些指令有**固定方向**（比如 GM→UB、L0C→GM），且不同方向走不同的硬件流水（MTE2、MTE3、Fix 等），所以「用什么指令」完全由 `src.scope()` 和 `dst.scope()` 决定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py) | `T.copy` 的前端实现（`npu_copy_v2`）：把 `(src, dst)` 转成 region 描述，发射 `tl.ascend_copy` intrinsic。 |
| [tilelang/language/ascend_tile.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py) | `T.tile.atomic_add` 的前端实现：发射 `tl.ascend_atomic_add` intrinsic。 |
| [src/op/ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc) | `AscendCopy` 与 `AscendAtomicAdd` 两个 C++ op 的 lowering：scope 分发、模板选择、生成对模板库的调用。 |
| [src/op/bulk_copy.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/bulk_copy.cc) | 上游 GPU/Hopper 的 `Copy` op（`tl.copy`，TMA 搬运）。本讲用来对比说明「Ascend 走的是另一条路」。 |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | Ascend C 模板库：`copy_gm_to_ub` / `copy_ub_to_gm` / `atomic_add_ub_to_gm` / `atomic_add_l0c_to_gm` 等最终生成指令的封装。 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 官方手册中搬运路径表与 `T.tile.atomic_add` 的语义说明。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`T.copy`：跨存储层级的统一搬运原语**——讲前端怎么把一句 `T.copy(src, dst)` 翻译成 TIR。
2. **搬运路径的分发：scope 到 `copy_xxx` 模板的映射**——讲 C++ lowering 怎么按 scope 派发到不同的 Ascend DMA 模板。
3. **`T.tile.atomic_add`：把本地 tensor 原子累加回 GM**——讲原子累加的语义、限制和清零前提。

### 4.1 `T.copy`：跨存储层级的统一搬运原语

#### 4.1.1 概念说明

你可能会问：Ascend 上有 GM→L1、GM→UB、L0C→GM 等十几种不同方向的搬运，为什么前端只暴露一个 `T.copy(src, dst)`？

答案和上一讲的内存分配思路一致——**让开发者只描述语义，物理细节交给编译器**。`T.copy` 不需要你指明「这是一次 GM 到 UB 的搬运」，编译器会自己看 `src` 和 `dst` 的 scope 推断出来：

```
T.copy(A[bx, k], A_L1)   # src=global, dst=shared.l1  → copy_gm_to_l1
T.copy(C_L0, C[bx, by])  # src=wmma.accumulator, dst=global → copy_l0c_to_gm
T.copy(c_ub, C[bx, by])  # src=shared.ub, dst=global → copy_ub_to_gm
```

这样写 kernel 时，你只需关心「数据要从哪块 buffer 搬到哪块 buffer」，搬运指令的选择是自动的。

> ⚠️ 一个容易混淆的点：在主仓 tile-lang（GPU）里，`T.copy` 处理的是 `tl.copy`，由 `bulk_copy.cc` 的 `Copy` op 用 Hopper TMA 指令 lowering（见 [src/op/bulk_copy.cc:92-272](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/bulk_copy.cc#L92-L272)）。**在这个 Ascend 分支里**，`T.copy` 被重新绑定到了 `npu_copy_v2`，发射的是另一个 intrinsic `tl.ascend_copy`，由 `ascend.cc` 的 `AscendCopy` op 处理（见下方 4.1.3）。两者同名但走完全不同的代码路径。

#### 4.1.2 核心流程

`T.copy(src, dst)` 在前端的处理流程：

```text
1. 校验 src / dst 的 shape（整体搬运时维度须一致；跨 CV 搬运允许一维差 2 倍）。
2. 推断搬运范围 extent：取 src.extent 与 dst.extent 逐维 max。
3. 把 src / dst 各自转成一个 "region" 描述符（buffer 起点 + 各维范围 + 读/写属性）。
4. 发射 TIR intrinsic：tl.ascend_copy(src_region, dst_region, enable_relu, transpose, pad_value, tmp, ...)。
   —— 后续由 C++ 的 AscendCopy op 进一步 lowering（见 4.2）。
```

关键点：**切片只给起点，大小由目标 buffer 决定**。比如 `T.copy(A[bx * block_M, k * block_K], A_L1)` 里，源切片 `[bx*block_M, k*block_K]` 只指明了 GM 中的起点坐标，真正搬多少由目标 `A_L1` 的 shape 决定。这正是上一讲（u1-l4）GEMM 例子里 `T.copy` 的写法。

#### 4.1.3 源码精读

**① `T.copy` 在前端被绑定到 `npu_copy_v2`**

在 `tilelang/language/__init__.py` 里，同一行先导入通用的 `copy`，又用 `npu_copy_v2 as copy` 把它覆盖掉，所以这个分支里 `T.copy` 实际就是 `npu_copy_v2`：

[查看 `__init__.py` 的导入覆盖](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L53)——`from .copy_op import copy, c2d_im2col, npu_copy_v2 as copy`，后者覆盖前者。

**② `npu_copy_v2` 的关键参数与 region 构造**

`npu_copy_v2` 的签名和几个 Ascend 专用参数（[copy_op.py:257-309](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py#L257-L309)）值得记住：

- `transpose`：仅用于 L1→L0（`copy_l1_to_l0`）时是否转置 L1；
- `pad_value`：GM→UB 搬运时，填充 UB tail 无效区域的值（配合 tail mask，详见 u6-l6）。默认 0；
- `tmp`：A5 平台 UB→L1 搬运时做 ND→Nz 格式转换的临时 buffer；
- `unit_flag` / `real_k` / `real_n`：高级运行时参数，分别控制 fixpipe 单元标志和 L1→L0 的运行时收缩长度，**仅当显式传入时才会追加到 intrinsic 参数**，不传则保持 6 参数的旧行为不变。

其核心是把 `src` / `dst` 转成 region 描述，然后发射 `tl.ascend_copy`：

```python
# copy_op.py（节选，示例代码：来自项目源码，已简化）
src = _to_region(src, "r")
dst = _to_region(dst, "w")
copy_args = [src, dst, enable_relu, transpose, pad_value_expr, tmp_region]
# ... 可选地追加 unit_flag / real_k / real_n
return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_copy"), *copy_args)
```

[查看发射 `tl.ascend_copy` 的 return 语句](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py#L400)——`return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_copy"), *copy_args)`。

**③ 跨 CV 搬运的 shape 校验**

当 `src` 是 UB、`dst` 是 L1（UB→L1），或 `src` 是 L0C、`dst` 是 UB（L0C→UB）时——也就是数据要从 Vector 核跨到 Cube 核——前端会做一次特殊校验，允许某一维相差 2 倍（因为 `vid` 会把一维切成两半）：

[查看 `_is_cross_cv_copy` 与 `_check_cross_cv_shapes`](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py#L197-L204)——`(shared.ub → shared.l1)` 或 `(wmma.accumulator → shared.ub)` 判定为跨 CV。

#### 4.1.4 代码实践

**实践目标**：读懂并改造一个 GM→UB→逐元素计算→GM 的 kernel，观察 `T.copy` 的方向是如何由 scope 决定的。

**操作步骤**：

1. 打开 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)。
2. 找到 kernel 体的这三行搬运与计算：

   ```python
   with T.Scope("V"):
       T.copy(A[bx * block_M + vid * block_M // VEC_NUM, by * block_N], a_ub)  # GM → UB
       T.copy(B[...], b_ub)                                                    # GM → UB
       T.barrier_all()
       T.tile.add(c_ub, a_ub, b_ub)                                            # UB 上计算
       T.barrier_all()
       T.copy(c_ub, C[...])                                                    # UB → GM
   ```

3. 运行：

   ```bash
   python examples/elementwise/elementwise_add.py
   ```

**需要观察的现象**：程序应打印 `init successful!` 和 `Kernel Output Match!`。

**预期结果**：`a_ub`、`b_ub` 是 `T.alloc_ub` 分配的，scope 为 `shared.ub`；`A`、`B`、`C` 是参数，scope 为 `global`。因此前两句 `T.copy` 自动派发为 GM→UB（`copy_gm_to_ub`），末句派发为 UB→GM（`copy_ub_to_gm`）。你**没有**写任何「这是 GM 到 UB」的字样，方向完全由 scope 推断。

> 如果没有真实 NPU 环境，无法运行，请标注「待本地验证」，但你可以用 `func.get_kernel_source()` 打印生成的 Ascend C 代码，在其中搜索 `copy_gm_to_ub` / `copy_ub_to_gm` 调用来确认派发结果（这部分不依赖设备）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `a_ub` 改成用 `T.alloc_L1` 分配（即 scope 变成 `shared.l1`），`T.copy(A[...], a_ub)` 还能编译过吗？会派发到哪个模板？

**参考答案**：能编译。此时 `src=global`、`dst=shared.l1`，会从 GM→UB 的 `copy_gm_to_ub` 改派发为 GM→L1 的 `copy_gm_to_l1`（见 4.2.3 的 scope 分发表）。但注意：`T.alloc_L1` 属于 Cube 域，而后续 `T.tile.add` 是 Vector 计算，所以这个改动会破坏「在同一块 buffer 上计算」的语义，运行期很可能出错——这说明 `T.copy` 的派发虽然自动，但 buffer 的 scope 必须和计算域匹配。

**练习 2**：`T.copy` 的源切片只写了起点坐标（如 `A[bx*block_M, by*block_N]`），搬运的数据量由谁决定？

**参考答案**：由目标 buffer `dst` 的 shape 决定。前端在 [copy_op.py:126-138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/copy_op.py#L126-L138) 取 `src_extent` 与 `dst_extent` 逐维 `max` 作为搬运范围 `extent`。

---

### 4.2 搬运路径的分发：scope 到 `copy_xxx` 模板的映射

#### 4.2.1 概念说明

`tl.ascend_copy` 只是一个 TIR intrinsic，它本身不携带「用哪条 DMA 指令」的信息。真正决定指令的是 C++ op `AscendCopy::Lower`：它读出 `src.scope()` 和 `dst.scope()`，按一个 if-else 链选出一个**模板函数名**（如 `tl::ascend::copy_gm_to_ub`），然后把 src/dst 的指针、有效行列数等参数填进去，生成一条 `call_extern` 调用。

换句话说：**前端 `T.copy` 只管「从哪到哪」，C++ lowering 负责「用什么硬件指令」**。这一层分发是 `T.copy` 能用统一接口覆盖所有搬运路径的关键。

#### 4.2.2 核心流程

`AscendCopy::Lower` 的工作分两步：

```text
步骤一：按 scope 选模板名（一个 if-else 链）
  global → shared.l1     ⇒ "tl::ascend::copy_gm_to_l1"
  shared.l1 → matrix_a   ⇒ "tl::ascend::copy_l1_to_l0a"
  shared.l1 → matrix_b   ⇒ "tl::ascend::copy_l1_to_l0b"
  wmma.accumulator → global ⇒ "tl::ascend::copy_l0c_to_gm"
  global → shared.ub     ⇒ "tl::ascend::copy_gm_to_ub"
  shared.ub → global     ⇒ "tl::ascend::copy_ub_to_gm"
  shared.ub → shared.l1  ⇒ "tl::ascend::copy_ub_to_l1"   （跨 CV）
  wmma.accumulator → shared.ub ⇒ "tl::ascend::copy_l0c_to_ub" （跨 CV）
  其余 (UB→UB 等)        ⇒ "tl::ascend::copy_ub_to_ub"

步骤二：组装参数
  - layout 变换：若 buffer 有标注布局（u4-l4），用 layout_map 前传索引。
  - 计算 validRow / validCol：处理 tail 非对齐 tile 的有效区域（u6-l6 详讲）。
  - 生成 call_extern("<模板名>", src_ptr, dst_ptr, strideN, validRow, validCol, ...)
```

官方手册把这套支持矩阵总结成一张表（[Programming Guide 4.1.2](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L627-L637)）：

| src | dst | 说明 |
| --- | --- | --- |
| GM | L1 | GM 搬到 L1 Buffer |
| L1 | L0A | L1 搬到 L0A，Cube 左矩阵 |
| L1 | L0B | L1 搬到 L0B，Cube 右矩阵 |
| L0C | GM | L0C 搬到 GM |
| GM | UB | GM 搬到 UB |
| UB | GM | UB 搬到 GM |
| UB | UB | UB 内拷贝 |
| UB | L1 | UB 搬到 L1（跨 CV） |

> 这张表里没有「L0C→UB」一行，但源码里它是支持的（作为跨 CV 搬运的一种，见 4.2.3），手册在另一处（[Programming Guide: copy_l0c_to_ub](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1247-L1259)）单独说明了 `copy_l0c_to_ub` 这条路径。

#### 4.2.3 源码精读

**① scope 分发的 if-else 链**

这是 `AscendCopy::Lower` 的核心，位于 [src/op/ascend.cc:211-317](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L211-L317)。关键片段（示例代码：项目源码节选）：

```cpp
// src/op/ascend.cc（节选）
if (src.scope() == "global" && dst.scope() == "shared.l1") {
  ss << "copy_gm_to_l1";          config.gm2l1 = true;
} else if (src.scope() == "shared.l1" && dst.scope() == "wmma.matrix_a") {
  ss << "copy_l1_to_l0a";         config.l12l0 = true;
} else if (src.scope() == "shared.l1" && dst.scope() == "wmma.matrix_b") {
  ss << "copy_l1_to_l0b";         config.l12l0 = true;
} else if (src.scope() == "wmma.accumulator" && dst.scope() == "global") {
  ss << "copy_l0c_to_gm";         config.l0c2gm = true;
} else if (src.scope() == "shared.ub" || dst.scope() == "shared.ub") {
  // 进一步细分为 gm2ub / ub2gm / ub2l1 / l0c2ub / ub2ub
  ...
} else {
  LOG(FATAL) << "Unsupported scope: ...";   // 不支持的路径直接报错
}
```

注意最后一个分支：**遇到不支持的 scope 组合，编译期直接 `LOG(FATAL)`**。这就是为什么你不会在运行时才收到「不支持这种搬运」的错误——它在 lowering 阶段就拦住了。

**② 跨 CV 搬运的细分**

当 `src` 或 `dst` 之一是 `shared.ub` 时，进入 [ascend.cc:228-313](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L228-L313) 的细分分支，按另一端的 scope 再分：

- `global → shared.ub`：`copy_gm_to_ub`
- `shared.ub → global`：`copy_ub_to_gm`
- `shared.ub → shared.l1`：`copy_ub_to_l1`（设 `virtual_channel = true`）
- `wmma.accumulator → shared.ub`：`copy_l0c_to_ub`（设 `virtual_channel = true`）
- 其余：`copy_ub_to_ub`

其中 `copy_l0c_to_ub` 和 `copy_ub_to_l1` 这两条「跨 CV」路径是 Ascend 的特殊机制——它们表面上是从 Cube 的 L0C 直接到 Vector 的 UB，但物理上硬件不能直接搬，需要经 GM/L2 中转。这正是 u5-l4「Workspace 消除」要讲的内容，本讲只点到为止。

**③ 模板库：真正的 DMA 指令在这里**

`AscendCopy::Lower` 只生成形如 `call_extern("tl::ascend::copy_ub_to_gm", ...)` 的调用，真正的指令封装在模板库 [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 里。例如 UB→GM（[common.h:251-260](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L251-L260)）：

```cpp
// common.h（节选）
template <typename T, uint32_t srcN, uint32_t srcM = 1>
CATLASS_DEVICE void copy_ub_to_gm(GlobalTensor<T> dstTensor,
                                  LocalTensor<T> srcTensor,
                                  uint32_t realdstN = 1,
                                  uint32_t maskShapeM = srcM,
                                  uint32_t maskShapeN = srcN) {
  AscendC::DataCopyExtParams dataCopyParams(
      maskShapeM, maskShapeN * sizeof(T),
      (srcN - maskShapeN) * sizeof(T) / 32,
      (realdstN - maskShapeN) * sizeof(T), 0);
  AscendC::DataCopyPad(dstTensor, srcTensor, dataCopyParams);  // 真正的 DMA 指令
}
```

可以看到，`copy_ub_to_gm` 最终调的是 Ascend C 的 `AscendC::DataCopyPad`。这就是 `T.copy` 这句 Python 代码最终的物理落点。

**④ 与 GPU 路径的对照**

作为对照，GPU/Hopper 上 `tl.copy` 由 `Copy` op 处理（注册于 [elem.cc:472](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/elem.cc#L472)），其 `LowerBulkCopy` 用 TMA 指令 lowering（[bulk_copy.cc:92-272](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/bulk_copy.cc#L92-L272)）。两套 op 结构对称（都是 `Lower` + 模板库），但指令体系完全不同：GPU 是 TMA，Ascend 是 `DataCopyPad`。这也解释了为什么本讲要单独列 `bulk_copy.cc`——它是 `T.copy` 在「另一条后端」上的对应实现。

#### 4.2.4 代码实践

**实践目标**：通过生成的 Ascend C 代码，亲眼看到 scope → 模板的派发。

**操作步骤**：

1. 用一个最小脚本调用上一讲的 GEMM（或 elementwise_add），在 `func = ...` 之后加一行：

   ```python
   print(func.get_kernel_source())
   ```

2. 在打印出的 C++ 源码里，搜索以下模板名，记录它们各自对应的 `T.copy` 语句：
   - `copy_gm_to_l1`
   - `copy_l1_to_l0a` / `copy_l1_to_l0b`
   - `copy_l0c_to_gm`
   - `copy_gm_to_ub` / `copy_ub_to_gm`

**需要观察的现象**：GEMM 例子里你应该能看到 GM→L1、L1→L0A/L0B、L0C→GM 这几条搬运；elementwise 例子里则是 GM→UB 与 UB→GM。

**预期结果**：每条 `T.copy` 在生成的源码里都恰好对应一个 `tl::ascend::copy_xxx<...>(...)` 调用，证明派发正确。**待本地验证**（取决于你的设备/仿真环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AscendCopy::Lower` 遇到不支持的 scope 组合时用 `LOG(FATAL)` 而不是静默跳过？

**参考答案**：因为搬运是算子正确性的基础——如果静默跳过，数据就根本没搬，后续计算读到的是未初始化的 buffer，bug 极难定位。`LOG(FATAL)` 让错误在编译期（lowering 阶段）就暴露，符合「尽早失败」的工程原则。

**练习 2**：`copy_l0c_to_gm` 和 `copy_ub_to_gm` 都是「片上→GM」，它们底层用的 Ascend C 指令一样吗？

**参考答案**：不一样。`copy_ub_to_gm` 用 `AscendC::DataCopyPad`（走 Vector 的 MTE3 流水），而 `copy_l0c_to_gm` 走 Cube 的 Fixpipe（fixpipe）流水，因为 L0C 的结果要经 fixpipe 才能写回 GM。这属于 u4-l2「同步原语」会细讲的 pipe 区分。

---

### 4.3 `T.tile.atomic_add`：把本地 tensor 原子累加回 GM

#### 4.3.1 概念说明

普通 `T.copy(local, gm_dst)` 是**覆盖写**：GM 目标位置的原值被冲掉。但有一类算子——比如把多个 block 算出的 partial sum 累加到同一个 GM 输出（`Output[seg] += row`）——需要的是**读-改-写**的累加语义。这就是 `T.tile.atomic_add(dst_gm, src_local)` 的用途。

它和普通 `T.copy` 有三点关键区别：

1. **方向固定**：dst 必须是 GM（`global`），src 必须是本地 tensor（当前支持 `shared.ub` 和 `wmma.accumulator`）。不支持 GM→GM 或 UB→UB。
2. **原子累加**：底层开启 DMA 的 atomic add 模式，多个 core 同时写同一块 GM 时不会互相覆盖，而是累加。
3. **必须先清零**：因为是「累加」而不是「赋值」，如果 GM 输出里还有上一次的脏数据，结果就错了。

官方手册的原话（[Programming Guide:4.1.2](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L638)）：

> `T.tile.atomic_add(dst_gm, src_local)` 是 Ascend 专属的原子累加写回原语……适合多个 block/core 将 partial result 累加到同一 GM 输出的场景。**若业务语义是从 0 开始累加，调用前或 kernel 内需要先清零 GM 输出。**

#### 4.3.2 核心流程

`T.tile.atomic_add(dst, src)` 的端到端流程：

```text
前端 (ascend_tile.py:atomic_add)
  1. 校验：dst.scope() == "global"，src.scope() ∈ {shared.ub, wmma.accumulator}（否则报错）。
  2. 把 dst / src 转成 region，发射 tl.ascend_atomic_add(dst_region, src_region)。

C++ lowering (ascend.cc:AscendAtomicAdd::Lower)
  3. 再次 ICHECK 确认 scope 组合与 dtype 一致。
  4. 按 src.scope 选模板：
       shared.ub        → "tl::ascend::atomic_add_ub_to_gm"
       wmma.accumulator → "tl::ascend::atomic_add_l0c_to_gm"
  5. 生成 call_extern("<模板>", src_ptr, dst_ptr, strideN, validRow, validCol)。

模板库 (common.h)
  6. atomic_add_ub_to_gm: SetAtomicAdd<T>() → copy_ub_to_gm(...) → disable_dma_atomic_compat()
     atomic_add_l0c_to_gm: SetAtomicAdd<T2>() → copy_l0c_to_gm(...) → disable_dma_atomic_compat()
```

这里的关键技巧是：**atomic_add 其实就是「先 `SetAtomicAdd` 开启原子模式，再做一次普通搬运，再关闭原子模式」**。模板库复用了 `copy_ub_to_gm` / `copy_l0c_to_gm`，只是在外面包了一层原子开关。

#### 4.3.3 源码精读

**① 前端 `T.tile.atomic_add` 的 scope 校验**

[ascend_tile.py:200-205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L200-L205) 在前端就拦住了非法组合：

```python
# ascend_tile.py（节选，示例代码）
if dst_scope != "global":
    raise ValueError(f"... dst scope must be global, got {dst_scope}.")
if src_scope == "global":
    raise ValueError(f"... src scope must be local, got global.")
```

注意它的报错信息 `_ATOMIC_ADD_V1_ERR = "T.tile.atomic_add V1 only supports local tensor -> GM atomic add."`（[ascend_tile.py:91](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L91)）——这透露了它叫 **V1**，意味着只实现了「local tensor → GM」这一种语义，**不支持** GPU 风格全局 `T.atomic_add` 的 `return_prev`、`memory_order`、常量 src 等高级用法。

**② C++ lowering 的 scope 与 dtype 校验**

[ascend.cc:747-761](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L747-L761) 在 C++ 侧再做一次断言（双重保险）：

```cpp
// ascend.cc（节选）
ICHECK(dst.scope() == "global") << "... requires global dst, got " << dst.scope();
ICHECK(src.scope() == "shared.ub" || src.scope() == "wmma.accumulator")
    << "... requires UB/shared or L0C/wmma.accumulator src, got " << src.scope();
ICHECK(src->dtype == dst->dtype) << "... requires src and dst dtype to match ...";
```

**③ 模板选择**

[ascend.cc:780-798](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L780-L798)：按 src.scope 在 `atomic_add_ub_to_gm` 和 `atomic_add_l0c_to_gm` 之间二选一：

```cpp
// ascend.cc（节选）
if (src.scope() == "shared.ub") {
  ss << "tl::ascend::atomic_add_ub_to_gm<" << get_dtype(dst) << ", " << atomic_tmpl_n << ... << ">";
} else if (src.scope() == "wmma.accumulator") {
  ss << "tl::ascend::atomic_add_l0c_to_gm<" << ... << ">";
}
```

**④ 模板库：atomic = SetAtomicAdd + copy + 关闭**

最终指令在 [common.h:262-283](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L262-L283)。UB→GM 的原子累加（[common.h:262-271](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L262-L271)）：

```cpp
// common.h（节选）
template <typename T, uint32_t srcN, uint32_t srcM = 1>
CATLASS_DEVICE void atomic_add_ub_to_gm(GlobalTensor<T> dstTensor,
                                        LocalTensor<T> srcTensor, ...) {
  AscendC::SetAtomicAdd<T>();                 // ① 开启 DMA 原子 add 模式
  copy_ub_to_gm<T, srcN, srcM>(dstTensor, srcTensor, ...);  // ② 复用普通搬运
  disable_dma_atomic_compat();                // ③ 关闭原子模式
}
```

第 ③ 步的 `disable_dma_atomic_compat`（[common.h:47-53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L47-L53)）会按 CANN 版本走不同关闭接口：

```cpp
CATLASS_DEVICE void disable_dma_atomic_compat() {
#if defined(CANN_MAJOR) && CANN_MAJOR >= 9
  AscendC::DisableDmaAtomic();   // CANN 9.x
#else
  AscendC::SetAtomicNone();      // CANN 8.5
#endif
}
```

这正是手册（[Programming Guide:2176](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2176)）所说「CANN 9.x 用 `DisableDmaAtomic()`，CANN 8.5 走 `SetAtomicNone()`」的来源。**关闭原子模式很重要**：如果不关，后续同 kernel 内的非原子 `copy_ub_to_gm` 也会变成原子累加，污染结果。

**⑤ op 注册**

这两个 op 通过 `TIR_REGISTER_TL_OP` 注册到 intrinsic 名（[ascend.cc:963-971](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L963-L971)）：`tl.ascend_copy` → `AscendCopy`，`tl.ascend_atomic_add` → `AscendAtomicAdd`。

#### 4.3.4 代码实践

**实践目标**：用 `T.tile.atomic_add` 把多个 block 的 partial sum 累加到同一 GM 输出，并验证「必须先清零」。

**操作步骤**：

1. 打开官方示例 [examples/unsorted_segment_sum/unsorted_segment_sum.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/unsorted_segment_sum/unsorted_segment_sum.py)，重点看 `atomic_add_kernel`（[L24-52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/unsorted_segment_sum/unsorted_segment_sum.py#L24-L52)）。核心是这一句：

   ```python
   # 把当前行的数据，原子累加到它所属 segment 的输出位置
   T.tile.atomic_add(Output[seg_ub[0], d_blk * block_D], row_ub)
   ```

   这里很多行（不同 block）可能属于同一个 segment，所以它们会**原子累加到 `Output` 的同一行**。

2. 注意调用 kernel **之前**，宿主侧已经把 `Output` 清零了（[unsorted_segment_sum.py:247](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/unsorted_segment_sum/unsorted_segment_sum.py#L247)）：

   ```python
   output = torch.zeros(num_segments, D_padded, dtype=compute_dtype, device=data_2d.device)
   ```

3. 运行示例并观察是否输出 `Kernel Output Match!`。

4. **破坏性实验**：把第 247 行改成 `torch.ones(...)`（即不清零，给个非 0 初值），再跑一次。

**需要观察的现象**：清零版本应正确匹配；`torch.ones` 版本的输出会比参考值**每个元素都大 1**（因为累加到了一个非 0 初值上）。

**预期结果**：这验证了「atomic_add 是累加而非赋值，必须先清零 GM 输出」。**待本地验证**（需 NPU 或 camodel 仿真环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `atomic_add_ub_to_gm` 在搬运之后必须调用 `disable_dma_atomic_compat()`？

**参考答案**：因为 `SetAtomicAdd<T>()` 是一个**持续生效的模式开关**，开了之后同一 kernel 内后续所有 DMA 搬运都会变成原子累加。如果不关，紧随其后的普通 `copy_ub_to_gm`（覆盖写）也会变成累加，导致结果错误。所以每次 atomic_add 用完必须立刻关闭。

**练习 2**：`T.tile.atomic_add` 支持把 GM 的一块累加到另一块 GM 吗？为什么？

**参考答案**：不支持。前端（[ascend_tile.py:204-205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend_tile.py#L204-L205)）和 C++（[ascend.cc:749](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L749)）都强制 src 必须是本地 tensor（UB 或 L0C）。原因是 Ascend 的 DMA atomic add 只在「片上→GM」方向有硬件支持，底层指令（`copy_ub_to_gm` / `copy_l0c_to_gm`）的源就是 `LocalTensor`。

**练习 3**：手册里说 `T.tile.atomic_add` 是「V1」，这暗示了什么？

**参考答案**：暗示它目前只实现了「local tensor → GM 的 DMA 原子累加」这一种语义，是功能受限的第一版。它不支持主仓 GPU 风格 `T.atomic_add` 的 `return_prev`（返回旧值）、`memory_order`、`use_tma`、常量 src 或任意表达式 src 等高级特性（[Programming Guide:2174](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2174)）。后续版本可能会扩展。

---

## 5. 综合实践

把本讲的两个原语串起来，自己写一个**带原子累加的 elementwise 归约算子**。

**任务**：实现一个 kernel，输入是形状 `[N, D]` 的矩阵 `A` 和一个长度 `D` 的向量 `v`，输出 `out[j] = Σ_i (A[i, j] + v[j])`，即「先对每行加 `v`，再沿第 0 维求和」。要求：

- 每个 block 负责若干行，先把它们搬进 UB（用 `T.copy`：GM→UB）；
- 在 UB 上做加法（用 `T.tile.add`），再对该 block 内的行求部分和（可循环累加到一个 UB 累加器）；
- 用 `T.tile.atomic_add` 把每个 block 的部分和**原子累加**到 GM 输出 `out[j]`（因为不同 block 都要写 `out`）；
- **调用 kernel 前用 `torch.zeros` 清零 `out`**。

**提示**：

- 参照 `elementwise_add.py` 的 GM→UB→计算→GM 结构；
- 参照 `unsorted_segment_sum.py` 的 `T.tile.atomic_add(Output[...], acc_ub)` 写法；
- dtype 选 `float32`，`block_D` 选 16 的倍数；
- 别忘了清零输出，否则结果会偏。

**验证**：与 `torch.sum(A + v, dim=0)` 对比（`torch.testing.assert_close`）。

**预期结果**：清零时匹配；故意不清零则每个元素偏一个常数。这一步把「`T.copy` 的 scope 自动派发」与「`T.tile.atomic_add` 的累加语义」用在了同一个算子里。**待本地验证**。

---

## 6. 本讲小结

- `T.copy(src, dst)` 是**统一的搬运原语**：前端只声明「从哪到哪」，搬运方向（用哪条 DMA 指令）由 `src.scope()` 和 `dst.scope()` 自动推断，覆盖 GM↔L1、GM↔UB、L1→L0A/L0B、L0C→GM、UB↔UB、UB↔L1、L0C→UB 等全部路径。
- 在这个 Ascend 分支里，`T.copy` 实际是 `npu_copy_v2`，发射 `tl.ascend_copy` intrinsic；GPU/Hopper 的 `tl.copy` 由 `bulk_copy.cc` 的 `Copy` op 处理，两者同名但走不同代码路径。
- C++ 的 `AscendCopy::Lower`（[ascend.cc:211-317](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L211-L317)）用一个 scope if-else 链选出 `tl::ascend::copy_xxx` 模板，最终落到模板库 [common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 里的 `DataCopyPad` 等真实指令；不支持的组合直接 `LOG(FATAL)`。
- `T.tile.atomic_add(dst_gm, src_local)` 是**原子累加写回**：dst 必须 GM，src 必须本地 tensor（UB 或 L0C），底层是 `SetAtomicAdd` + 普通 copy + `disable_dma_atomic_compat` 三件套。
- atomic_add 是**累加而非赋值**，业务若要从 0 累加，调用前必须清零 GM 输出；它在搬运后必须关闭原子模式，否则会污染同 kernel 内的后续搬运。

## 7. 下一步学习建议

- **下一讲（u3-l3）矩阵计算 `gemm_v0` / `mma`**：本讲的 L1→L0A/L0B、L0C→GM 搬运是 GEMM 的输入/输出环节，下一讲会把搬运和 `T.gemm_v0` / `T.mma` 计算串成完整 GEMM。
- **u3-l4 Reduce 原语**：本讲的 `copy_ub_to_ub`（含 Cast）和 atomic_add 经常和 reduce 一起出现（如 FlashAttention 的 online softmax）。
- **u4-l2 / u4-l3 同步原语与自动同步**：本讲提到 GM↔UB、L0C→GM 走不同硬件流水（MTE2/MTE3/Fix），这些流水之间需要 `set_flag/wait_flag` 同步——下一单元会讲怎么手写或自动插入这些同步。
- **u5-l4 Workspace 消除**：本讲提到的 `copy_l0c_to_ub`、`copy_ub_to_l1` 这两条「跨 CV」搬运，物理上需要经 GM/L2 中转，u5-l4 会讲编译器如何自动消除这些中转开销。
- **u6-l6 LowerTileOp 与 Tail Mask**：本讲搬运参数里的 `validRow` / `validCol` / `pad_value` 是为非对齐 tail tile 准备的，u6-l6 会讲 `AscendTailMaskPropagation` 如何改写它们。
