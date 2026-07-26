# fp4/int4 反量化 matmul 与量化基线生态

## 1. 本讲目标

本讲是「多精度与量化矩阵乘」单元的收束篇。前面三讲（u4-l12、u4-l13、u4-l14）已经把 TileLang 在单内核内「压缩存放权重 → 解包 → 计算」的机制讲透了。本讲把视角拉到**项目层面**，回答两个问题：

1. **TileLang 自家的两种反量化 matmul 有什么区别？** 即同一个 `3.tilelang-benchmark` 目录下的 `fp16xint4` 与 `fp16xfp4` 两个文件，分别对应什么算子形态、什么调度、什么位宽。
2. **量化算子有哪些对比基线（baseline）？** 项目为反量化 matmul 准备了 Marlin、CUTLASS（fpa_intb）、bitsandbytes（nf4）等多个成熟框架作为标尺，它们各自的定位、安装方式、入口脚本是什么。

学完后你应当能够：

- 区分 int4 与「fp4」两种 TileLang 反量化 matmul 的算子形态与调度差异；
- 说清 Marlin、CUTLASS fpa_intb、bitsandbytes nf4 三个量化基线的定位与 W4A16/nf4 覆盖；
- 为给定算子定位对应的基线入口脚本与 `install_*.sh` 依赖安装方式；
- 在 hopper 与 ampere 两个架构目录下，对比反量化算子的基线框架覆盖差异。

## 2. 前置知识

本讲默认你已经学完 u4-l12 ~ u4-l14，熟悉以下概念。为避免重复，这里只做一句话回顾并补充本讲新术语。

- **W4A16 / W4A8**：权重（Weight）用 4-bit、激活（Activation）用 16-bit 或 8-bit。权重大、激活小，所以量化权重收益最大，这是大模型推理（LLM decode）的核心场景。
- **反量化（dequantize）**：把压缩存放的低比特权重解包、还原成可参与乘加的数值。本项目的反量化 matmul 都是「边解包边算」而非先全量解包再算。
- **GEMM vs GEMV**：M 维较大时是 GEMM（算力瓶颈，用 TensorCore 块级 MMA）；M=1 时退化为 GEMV（带宽瓶颈，用线程级外积-规约）。u4-l13 已讲过这条分界。
- **块级 GEMM 五要素**（u3-l9）：`T.Kernel` 网格、`alloc_shared/fragment`、`T.copy`、`T.gemm`、`T.Pipelined`。本讲的 fp16xfp4 内核就是这套骨架。
- **线程级外积-规约**（u4-l13）：`T.alloc_local` + `T.thread_binding` + `T.tvm_thread_allreduce`。本讲的 fp16xint4 内核就是这套骨架。
- **lop3 / dp4a 快速解码**（u4-l14）：`_tir_packed_int_to_int_convert` 标量解包 vs `get_lop3_intrin_group` 注入 PTX lop3 一条指令解码；`T.dp4a` 做 4 路 INT8 点积。

本讲新术语：

- **NF4（NormalFloat 4-bit）**：QLoRA 论文提出的 4-bit 正态分布浮点格式，每个权重用 4-bit 表示但取值点是按正态分布信息论最优放置的，而非均匀量化。bitsandbytes 用它。
- **fpa_intb**：CUTLASS/TVM 对这类算子的命名，意为 **f**loat-**p**oint **a**ctivation、**int** (nibble, 4-bit) **b**ackend weight，即 W4A16。
- **groupsize / per-column scale**：量化时的缩放因子粒度。`groupsize=-1` 表示整列一个 scale（per-channel）；`groupsize=128` 表示每 128 个权重共享一个 scale（per-group）。
- **provider / 基线（baseline）**：u1-l2 已定义——同一算子的不同实现并列放在编号子目录里，`0.` 开头通常是参考基线。

## 3. 本讲源码地图

本讲涉及四个关键文件，分属两个架构目录、四个 provider：

| 文件 | 架构 | provider | 作用 |
|---|---|---|---|
| `hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py` | hopper | TileLang | 块级反量化 GEMM（2-bit 权重）内核 |
| `hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py` | hopper | TileLang | 线程级反量化 GEMV（4-bit 权重）内核（u4-l13/u4-l14 已精读） |
| `hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py` | hopper | Marlin | W4A16 Marlin 高速内核基线 |
| `ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py` | ampere | CUTLASS | TVM Relax + CUTLASS 的 W4A16 基线 |
| `ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py` | ampere | bitsandbytes | NF4 量化 matmul 基线 |

辅助文件（驱动脚本、依赖安装）：

- `hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh`：TileLang 反量化的 shape×dtype 编排脚本。
- `ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh`、`.../2.cutlass_fpa_intb_benchmark/install_dependency.sh`、`ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh`：三个基线的依赖安装脚本。

> 目录命名提醒（承接 u1-l2）：hopper 下叫 `dequantize_matmul`，ampere 下叫 `dequant_matmul`（少一个 `e`），且子目录分隔符 `-` 与 `_` 混用。定位文件时以实际目录为准。

---

## 4. 核心概念与源码讲解

### 4.1 fp4 vs int4：两种 TileLang 反量化 matmul

#### 4.1.1 概念说明

`3.tilelang-benchmark` 目录下放着两个看起来很像、实则截然不同的内核文件：

- `benchmark_tilelang_matmul_fp16xint4.py`：u4-l13、u4-l14 已精读。它是 **GEMV**（M=1），采用**线程级外积-规约**调度，权重是 **4-bit**（`num_bits=4`），存于 `int8`（每字节 2 个权重），激活是 `float16`（W4A16），解包走 `_tir_packed_int_to_int_convert` / lop3，点积可选 `T.dp4a`。
- `benchmark_tilelang_matmul_fp16xfp4.py`：本讲重点。它是 **GEMM**（M 默认 16384），采用**块级 `T.gemm`**（TensorCore MMA）调度，权重在代码里实为 **2-bit**（`num_bits=2`），存于 `uint8`（每字节 4 个权重），解包走自定义的 `_tir_u8_to_u2_to_u8`。

> **重要：以代码为准（再次提醒）。** 这个文件名叫 `fp16xfp4`，文件内注释也写「Use half-precision for input data」，但实际代码里 `dtype = "int8"`、`accum_dtype = "int32"`、`num_bits = 2`——即激活是 int8、权重是 2-bit、int32 累加，是一个 **W2A8** 整数反量化 GEMM，既不是 fp16 激活也不是 4-bit 权重。「fp4」在大模型量化语境里通常指 E2M1 等 4-bit 浮点格式，但本文件的代码并未实现浮点 4-bit，只是用 2-bit 整数占用文件名。这是本项目「注释/文件名与代码不符」现象的又一例（与 u3-l8、u3-l9、u4-l12 同源），分析时一律以代码为准。

把两者放在一起对比，能直观看到 TileLang 「结构与精度解耦」的设计：**同一个目录、同一类算子（反量化 matmul），换一个算子形态（GEMM↔GEMV）就换一整套调度骨架**。

#### 4.1.2 核心流程

`fp16xfp4` 内核走的是 u3-l9 块级 GEMM 五要素，只是多了一个「加载压缩权重 → 逐元素解包 → 再做 `T.gemm`」的步骤。每个 block 的数据流：

```text
全局 A(int8) ──T.copy──> A_shared
全局 B(uint8, 压缩) ──T.copy──> B_shared ──T.copy──> B_local(uint8)
                                                   │
                              T.Parallel 逐元素 _tir_u8_to_u2_to_u8 解包
                                                   ▼
                                       B_dequantize_local(int8)
                                                   │
                              T.copy 到 B_dequantize_prev_local
                                                   ▼
           C_local(int32) <── T.gemm(B_dequant_prev, A_shared, transpose_B=True)   ← 沿 K 循环累加
                   │
           T.copy → C_shared(int8) → T.copy → 全局 C
```

关键点：

1. **权重压缩比**：2-bit 权重每字节塞 4 个（`num_elems_per_byte = 8 // 2 = 4`），所以 B 的全局/shared 形状在 K 维被压缩 4 倍。
2. **解包是 `T.Parallel`**：在 fragment 上对 `(block_N, block_K)` 每个元素并行解包，把 `uint8` 还原成 `int8`，**之后**才进 `T.gemm`。这与 fp16xint4 的「标量/lop3 解包后直接线程内点积」形成对照——这里解包完还要喂给 TensorCore MMA。
3. **`T.gemm` 的 `transpose_B=True`**：B 在代码里按 `(N, K)` 存，GEMM 做的是 `C += B_dequant @ A^T` 的等价形式（与 u3-l9 一致），不改数值。

解包函数本身极简——把一个 `uint8` 按位置右移再掩码，取出 2-bit：

\[ \text{decode}(\text{val}, \text{pos}) = (\text{val} \gg (\text{pos} \times 2))\ \&\ \text{0b11} \]

#### 4.1.3 源码精读

**解包函数** `_tir_u8_to_u2_to_u8`：断言只支持 2-bit、`int8` 输出、`uint8` 输入，核心就是「右移 `pos*nbit` 位 + 与 `0b11` 掩码」。[benchmark_tilelang_matmul_fp16xfp4.py:16-21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L16-L21) 这段代码说明：解包纯靠整数移位，不依赖 lop3/dp4a 等硬件内联函数，与 fp16xint4 的可加速解码路径不同。

**精度与压缩配置**（注意与文件名/注释的出入）：[benchmark_tilelang_matmul_fp16xfp4.py:205-212](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L205-L212)

```python
dtype = "int8"                 # 注释说 half-precision，实为 int8
accum_dtype = "int32"
num_bits = 2                   # 文件名 fp4，实为 2-bit
num_elems_per_byte = 8 // num_bits   # = 4
storage_dtype = "uint8"
B_shape = (N, K // num_elems_per_byte)          # K 维压缩 4 倍
B_shared_shape = (block_N, block_K // num_elems_per_byte)
B_dequantize_shared_shape = (block_N, block_K)  # 解包后还原
```

**K 循环内的「加载-解包-累加」三步**：[benchmark_tilelang_matmul_fp16xfp4.py:254-275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L254-L275)

```python
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
    T.copy(A[by * block_M, k * block_K], A_shared)
    T.copy(B[bx * block_N, k * block_K // num_elems_per_byte], B_shared)  # 压缩量
    T.copy(B_shared, B_local)
    for i, j in T.Parallel(block_N, block_K):           # 逐元素并行解包
        B_dequantize_local[i, j] = _tir_u8_to_u2_to_u8(
            num_bits, B_local[i, j // num_elems_per_byte], j % num_elems_per_byte, dtype=dtype)
    T.copy(B_dequantize_local, B_dequantize_prev_local)
    T.gemm(B_dequantize_prev_local, A_shared, C_local, transpose_B=True, policy=policy)
```

注意 `B[...]` 的第二维下标是 `k * block_K // num_elems_per_byte`——因为存储是压缩的，下标也按压缩比缩放。

**默认 shape 是 GEMM**（不是 GEMV）：[benchmark_tilelang_matmul_fp16xfp4.py:288-290](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L288-L290) 中 `--m/--n/--k` 默认都是 `16384`，对照 fp16xint4 默认 `M=1`（[benchmark_tilelang_matmul_fp16xint4.py:158](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L158)），即可一眼区分 GEMM 与 GEMV 形态。

**驱动脚本默认跑 int4 而非 fp4**：编排脚本 [benchmark_tilelang_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L1-L40) 里，fp4 那一行被注释掉了，实际执行的是 `fp16xint4.py`：

```bash
# cmd="python ./benchmark_tilelang_matmul_fp16xfp4.py --m ${m} --n ${n} --k ${k}"
cmd="python ./benchmark_tilelang_matmul_fp16xint4.py --m ${m} --n ${n} --k ${k}"
```

这意味着默认配置下 TileLang 反量化跑的是 W4A16 GEMV，fp16xfp4（W2A8 GEMM）需要手动改脚本才会运行。读脚本必查真实生效的命令（承接 u1-l3）。

把两个内核对照成表：

| 维度 | `fp16xint4`（u4-l13/14） | `fp16xfp4`（本讲） |
|---|---|---|
| 算子形态 | GEMV（M=1） | GEMM（M 默认 16384） |
| 调度骨架 | 线程级外积-规约 + `tvm_thread_allreduce` | 块级五要素（`T.Kernel`/`T.gemm`/`T.Pipelined`） |
| 激活 dtype | `float16` | `int8`（注释说 half） |
| 权重位宽 | 4-bit | 2-bit（文件名 fp4） |
| `storage_dtype` | `int8`（2 elem/byte） | `uint8`（4 elem/byte） |
| `accum_dtype` | `float16` | `int32` |
| 解包函数 | `_tir_packed_int_to_int_convert` / lop3 | `_tir_u8_to_u2_to_u8`（纯移位） |
| 调优入口 | `AutoTuner.from_kernel`（显式） | `@autotune`（装饰器） |
| 算力单元 | 标量乘加 / `T.dp4a` | TensorCore MMA |

#### 4.1.4 代码实践

**实践目标**：在不运行的前提下，通过对比两个文件的精度配置，理解「结构与精度解耦」并印证「以代码为准」。

**操作步骤**：

1. 打开 `benchmark_tilelang_matmul_fp16xfp4.py`，定位第 205-209 行的 `dtype/accum_dtype/num_bits/storage_dtype` 五行配置。
2. 打开 `benchmark_tilelang_matmul_fp16xint4.py`，定位第 159-163 行 `main()` 内的 `in_dtype/out_dtype/accum_dtype/num_bits/storage_dtype`。
3. 把两者的「文件名宣称」「注释宣称」「代码实际」三列填入下表：

| 文件 | 文件名宣称 | 注释宣称 | 代码实际（dtype/num_bits） |
|---|---|---|---|
| fp16xint4 | W4A16 | — | （待你填写） |
| fp16xfp4 | fp16 × fp4 | half-precision | （待你填写） |

**需要观察的现象**：fp16xfp4 的「文件名 + 注释」与「代码」是否一致？fp16xint4 是否一致？

**预期结果**：fp16xint4 文件名与代码基本一致（都是 W4A16，激活 fp16、权重 4-bit）；fp16xfp4 的文件名/注释（fp16、fp4）与代码（int8、2-bit）**不符**，实际是 W2A8。

**待本地验证**：若你手头有 GPU 与 tilelang 环境，可分别运行两个脚本，对比 `Best TFlops` 量级——GEMM（M=16384）的 TFlops 应远高于 GEMV（M=1），因为 GEMV 受带宽限制、算术强度极低。

#### 4.1.5 小练习与答案

**练习 1**：`fp16xfp4` 内核里 `num_elems_per_byte = 4`，那么 `B` 的全局形状 `(N, K // 4)` 相比未压缩的 `(N, K)` 节省了多少显存？

**答案**：节省为原来的 1/4，即权重显存压缩到 25%（2-bit 相对 8-bit 的压缩比 = 2/8 = 1/4）。

**练习 2**：为什么 `fp16xfp4` 的解包用 `T.Parallel` 而 `fp16xint4` 用 `T.serial`/`T.vectorized`？

**答案**：`fp16xfp4` 是块级 GEMM，解包后要喂给 TensorCore，整个 `(block_N, block_K)` fragment 需要一次性并行解包完毕；`fp16xint4` 是线程级 GEMV，每个线程只负责 `micro_size_k` 长度的一段，按线程内顺序解包即可，用 `serial`/`vectorized` 更贴合线程私有寄存器的工作方式。

**练习 3**：把 `fp16xfp4` 的 `num_bits` 从 2 改成 4（并相应让 `B_shape` 用 `K // 2`），内核是否还能正确运行？

**答案**：不能直接运行——`_tir_u8_to_u2_to_u8` 内部 `assert nbit == 2`（[第 17 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xfp4.py#L16-L21)），它写死只支持 2-bit。改 4-bit 需要换用 `_tir_packed_int_to_int_convert`（即 fp16xint4 的解包函数）。这正说明两个文件的解包函数是按位宽特化的。

---

### 4.2 Marlin 基线：W4A16 专用高速内核

#### 4.2.1 概念说明

**Marlin** 是一个为 W4A16 大模型推理高度优化的反量化 GEMM/GEMV 内核（源于论文 *Marlin: Mixed-Precision Auto-Regressive Parallel Inference*，IST-DASLab 开源）。它的特点是：

- **专为 W4A16 写死**：权重固定 4-bit、激活固定 fp16，不追求通用性，只追求这一种格式下的极致吞吐。
- **权重预先打包**：权重在进入内核前被重排成利于内存合并的布局（16 个 4-bit 权重交错打包进一个 int32），运行时不再做布局变换。
- **per-channel scale**：每列权重一个缩放因子 `s`，`groupsize=-1` 表示整列一个 scale。

在本项目里，Marlin 是 TileLang W4A16 内核（fp16xint4）的**直接对标基线**——同样的 W4A16、同样的 GEMV 形态（shapes 全是 M=1），用来回答「TileLang 自己写的反量化 GEMV 比 Marlin 这种专项内核快还是慢」。

#### 4.2.2 核心流程

`benchmark_marlin.py` 的流程很直白：

```text
对每个 shape (m=1, n, k):
  1. get_problem: 生成 fp16 激活 A、int32 打包权重 B、fp16 scale s、输出 C
  2. 分配 workspace（Marlin 内核需要的临时显存）
  3. benchmark_quant: 反复调用 marlin.mul(A, B, C, s, workspace, ...) 计时
  4. 打印 latency(ms)、TFLOP/s、GB/s
```

权重打包格式：`B = torch.randint(..., size=(k * n // 8,))`——一维 int32 张量，每个 int32 装 8 个 4-bit 权重（\(8 \times 4 = 32\) 位），所以总元素数 \(= k \cdot n / 8\)。

性能度量同时报告算力（TFLOP/s）与带宽（GB/s），因为 GEMV 是带宽受限场景：

\[ \text{TFLOP/s} = \frac{2 \cdot m \cdot k \cdot n}{t} \times 10^{-12}, \qquad \text{GB/s} = \frac{2|A| + 4|B| + 2|C| + 2|s|}{t} \times 10^{-9} \]

其中 `4|B|` 是因为 B 是 int32（每元素 4 字节）。

#### 4.2.3 源码精读

**shapes 全是 GEMV**（M=1）：[benchmark_marlin.py:10-15](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L10-L15)，与 TileLang fp16xint4 的 GEMV 形态对齐，保证对比公平。

**权重打包成 int32**：[benchmark_marlin.py:46-56](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L46-L56)

```python
B = torch.randint(low=-(2**31), high=2**31, size=(k * n // 8,), device=dev)  # int32, 8 权重/元素
s = torch.zeros((k // groupsize, n), dtype=torch.half, device=dev)            # per-channel scale
```

**核心调用 `marlin.mul`**：[benchmark_marlin.py:34-43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L34-L43)

```python
workspace = torch.zeros(C.shape[1] // 128 * 16, device=torch.device("cuda:0"))
res = benchmark(lambda: marlin.mul(A, B, C, s, workspace, thread_k, thread_n, sms))
return {"s": res, "TFLOP/s": 2*A.numel()*C.shape[1]/res/10**12, "GB/s": ...}
```

`workspace` 大小与 N 强相关（`N // 128 * 16`），是 Marlin 内核分裂规约用的中间显存；`thread_k/thread_n/sms` 传 `-1` 让内核自选默认值。

**SM 数硬编码以省一次查询**：[benchmark_marlin.py:59-69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L59-L69) 按设备名匹配 A100=108、A10=72、3090=82、A6000=84，未知则 `-1`。注释说明这是为了避免内核内查询 SM 数的微小开销。

**计时策略**：[benchmark_marlin.py:18-31](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L18-L31) 用 `time.time()` + `torch.cuda.synchronize`，循环内**不**同步（模拟真实推理隐藏 launch 开销），且每次基准后 `time.sleep(1.0)` 给 GPU 降温防降频。这与 cuBLAS 的 chrono 自适应重复（u2-l5）、Triton/TileLang 的 `do_bench`（u2-l6）是第三种计时风格，跨框架对比前须意识到单位与口径差异。

**依赖安装**（ampere 目录下的同名脚本）：[install_marlin.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh) 直接 `git clone` IST-DASLab/marlin 后 `pip install -e .`，并设 `TORCH_CUDA_ARCH_LIST="8.0"`（ampere）。

> 注意：本文件 `import marlin`，但 `hopper_benchmark/.../5.marlin-benchmark/marlin/` 子目录是空的（见 `ls`），说明 Marlin 源码不在本仓库内，必须先跑 `install_marlin.sh` 拉取并编译。这是它与「开箱即用」的 bitsandbytes 的关键区别。

#### 4.2.4 代码实践

**实践目标**：理解 Marlin 的权重打包格式与 workspace 的来源。

**操作步骤**：

1. 在 [benchmark_marlin.py:35](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/5.marlin-benchmark/benchmark_marlin.py#L34-L36) 找到 `workspace` 的分配式 `C.shape[1] // 128 * 16`。
2. 对 shapes 里的 `(1, 28672, 8192)`，手算 `workspace` 有多少个元素、占多少字节（float32）。
3. 解释为什么 workspace 大小取决于 `C.shape[1]`（即 N）而非 M 或 K。

**需要观察的现象**：workspace 元素数与 N 的关系是否线性？

**预期结果**：N=28672 时，workspace 元素数 = 28672 // 128 * 16 = 224 * 16 = 3584 个，float32 占 14336 字节。线性于 N：因为 Marlin 把输出列方向按 128 分块做分裂规约，每 128 列需要 16 个中间累加槽，所以槽总数正比于 N。

**待本地验证**：若改动 `thread_n` 不再传 `-1`，workspace 需求是否会变（取决于 Marlin 内核对 `thread_n` 的解释）。

#### 4.2.5 小练习与答案

**练习 1**：Marlin 的 `B` 为什么是 `size=(k * n // 8,)` 而不是 `// 16` 或 `// 32`？

**答案**：B 是 int32，每元素 32 位；4-bit 权重每个占 4 位，所以一个 int32 装 \(32/4 = 8\) 个权重，总 int32 数 = \(k \cdot n / 8\)。

**练习 2**：Marlin 计时为什么循环内不同步、每轮后还 `sleep(1.0)`？

**答案**：循环内不同步是为了让多个 kernel launch 排队，模拟真实自回归推理中 launch 开销被掩盖的场景；`sleep(1.0)` 是给 GPU 降温，避免连续跑导致 thermal throttling（降频）污染后续测量。

---

### 4.3 CUTLASS fpa_intb 基线：TVM Relax + CUTLASS

#### 4.3.1 概念说明

这个基线和前几个都不一样——它**不是手写内核**，而是用 **TVM Relax**（TVM 的图级 IR）描述「量化 + 反量化 + matmul」的完整流程，再让 TVM 把其中的 matmul **编译调度交给 NVIDIA CUTLASS** 后端。`fpa_intb` 就是 TVM/CUTLASS 对这类算子的命名约定：**f**loat-**p**oint **a**ctivation、**int** (4-bit nibble) **b**ackend weight，即 W4A16。

它的价值在于：对照 TileLang「自己用 DSL 写整个反量化内核」，看「让编译器（TVM+CUTLASS）自动生成」能达到什么性能。本项目用它作为 ampere（A100）架构上的 W4A16 基线之一。

#### 4.3.2 核心流程

`cutlass_fpa_intb.py` 在一个 `@I.ir_module` 里定义了三个函数，组成「encode → preprocess → decode → matmul」流水线：

```text
encode(A_fp16): 逐列求 max_abs → 算 scale → 把 fp16 权重量化打包成 int4 (每字节 2 个, N//2 列)
                ↓
cutlass.ft_preprocess_weight: 把 int4 权重重排成 CUTLASS 期望的布局
                ↓
decode(packed_int4, scale): 逐元素移位解包 int4 → fp16，乘 scale 还原
                ↓
R.matmul(x_fp16, decoded_fp16): 主 matmul，被 partition_for_cutlass 交给 CUTLASS 群组 GEMM
```

关键在于 `decode` 的解包逻辑——它用一连串位运算把 4-bit 整数「解码」回 fp16，等价于先把它映射到 `[-8, 7]` 的对称整数范围（左移 28 再算术右移 28 做符号扩展），再乘 scale。

#### 4.3.3 源码精读

**decode 解包**（int4 → fp16，含符号扩展）：[cutlass_fpa_intb.py:50-80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py#L50-L80)

```python
decode_1[v_i, v_j] = (
    T.Cast("float16",
        T.shift_right(T.shift_left(
            T.bitwise_and(T.shift_right(T.Cast("int32", A[v_i, v_j//2]), T.Cast("int32", v_j%2)*4), 15), 28), 28))
    * B[v_j])    # B 是 per-column scale
```

这里 `v_j % 2 * 4` 选出高/低 nibble，`& 15` 取 4-bit，`<<28 >>28`（算术右移）做符号扩展到 32 位有符号整数，最后 `Cast` 成 fp16 并乘 scale。注意 `A` 形状是 `(K, N // 2)`（每字节 2 个权重，与 Marlin 的 8 权重/int32 不同、与 TileLang int4 的 2 权重/byte 相同）。

**encode 量化**（fp16 → int4 + scale）：[cutlass_fpa_intb.py:82-151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py#L82-L151)，核心是三步：逐列求 `max_abs_value`、算 `scale = max(max_abs, 0.0001) * 0.125`（乘 1/8 把范围压到 `[-8,7]`）、把 `round(A/scale)` 限幅到 `[-8,7]` 后按 nibble 打包。

**主 matmul 走 CUTLASS**：[cutlass_fpa_intb.py:189-195](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py#L189-L195)

```python
mod = partition_for_cutlass(Module)
mod = relax.transform.RunCodegen({"cutlass": {"sm": 80, "find_first_valid": False}}, entry_functions=["main"])(mod)
```

`partition_for_cutlass` 把图里符合 CUTLASS 群组 GEMM 模式的 `R.matmul` 挑出来，`RunCodegen` 用 CUTLASS（`sm=80` 即 A100）生成实际内核。

**编译目标 a100 + 计时**：[cutlass_fpa_intb.py:221-245](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py#L221-L245) 用 `relax.build(mod_deploy, target="nvidia/nvidia-a100")`，warmup 5 次、计时 10 次、`dev.sync()` 屏障，输出 `cost`（ms）。

**依赖安装（最重的一个）**：[install_dependency.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh) 完整编译一个带 CUTLASS 后端的 TVM：

```bash
git clone https://github.com/apache/tvm --recursive cutlass_fpa_intb_tvm
git checkout 2bf3a0a4287069ac55ee3304c285b08592d3d1bc   # 钉死特定 commit
cmake -DCMAKE_CUDA_ARCHITECTURES="80" ..
# set(USE_CUDA ON) / USE_LLVM / USE_CUBLAS / USE_CUTLASS ON
make -j 16
```

注意它 `git checkout` 到一个**特定 commit**——这是为了复现性，保证 CUTLASS 群组 GEMM 的代码生成行为固定。`benchmark.sh` 还要把 TVM 的 `python/` 加进 `PYTHONPATH` 才能运行。

#### 4.3.4 代码实践

**实践目标**：读懂 decode 的符号扩展魔法，并对比它与 TileLang int4 解包的区别。

**操作步骤**：

1. 在 [cutlass_fpa_intb.py:62-80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/cutlass_fpa_intb.py#L62-L80) 取一个 4-bit 值，比如 `0b1100`（=12）。
2. 手算：`12 & 15 = 12`，`12 << 28`（32 位）再算术 `>> 28` 得多少？再 Cast 成 fp16、乘 scale。
3. 对比 TileLang fp16xint4 的 `_tir_packed_int_to_int_convert`（u4-l14），两者如何实现「把 4-bit 当有符号整数」？

**需要观察的现象**：`<<28 >>28` 能否正确把 `0b1100` 解释为 `-4`？

**预期结果**：`12 << 28` = `0xC0000000`（最高有效 nibble 的符号位为 1），算术右移 28 位做符号扩展得到 `-4`。这就是「4-bit 无符号码 12 ↔ 有符号值 -4」的转换。TileLang 的 `_tir_packed_int_to_int_convert` 内部也做类似的符号处理，只是封装在标量函数里。

#### 4.3.5 小练习与答案

**练习 1**：为什么 encode 里 `scale` 要乘 `0.125`（即除以 8）？

**答案**：4-bit 有符号整数范围是 `[-8, 7]`。为了让 `round(A/scale)` 落在这个范围内，scale 必须 ≥ `max_abs / 8`，所以预设 `scale = max_abs * (1/8) = max_abs * 0.125`，把权重最大幅值映射到 4-bit 的满量程。

**练习 2**：这个基线为什么必须 `git checkout` 到特定 TVM commit？

**答案**：CUTLASS 群组 GEMM 的代码生成（`partition_for_cutlass` + `RunCodegen`）在不同 TVM 版本里行为会变（接口、调度、支持的 dtype/shape）。钉死 commit 是为了保证脚本里的 IR 描述与代码生成器版本匹配，否则可能编译失败或性能不可复现。

---

### 4.4 bitsandbytes nf4 基线：QLoRA 的 NF4 量化

#### 4.4.1 概念说明

**bitsandbytes** 是最常见的「开箱即用」量化库（QLoRA 的官方实现），它提供 **NF4（NormalFloat 4-bit）** 量化。NF4 与前面所有 int4 都不同：

- int4 是**均匀**量化（16 个等间距电平）；
- NF4 是**非均匀**量化——16 个电平按标准正态分布的信息论最优分位点放置，因为大模型权重近似服从正态分布，NF4 在相同 4-bit 预算下量化误差更小。

NF4 通常配合 **blockwise 量化**（`blocksize=128`）：每 128 个权重一组，各自有自己的缩放因子，让量化适应权重幅值的局部变化。

在本项目里，bitsandbytes 是 ampere 架构上 W4A16 的「易用性标杆」——一行 `pip install`、一行 `bnb.matmul_4bit` 就能跑，用来对照 TileLang / Marlin / CUTLASS 这些需要更多工程投入的方案。

#### 4.4.2 核心流程

```text
对每个 shape (M, N, K):
  1. 生成 fp16 激活 A、fp16 权重 B
  2. F.quantize_nf4(B, blocksize=128): 把 B 量化成 NF4 + quant_state(含 scale/shape/dtype 等)
  3. warmup 10 次 bnb.matmul_4bit(A, B_nf4.t(), quant_state)
  4. 计时 100 次求平均
  5. 打印 "{M}_{N}_{K}: {ms} ms"
```

注意 shapes 里既有 M=1（GEMV）也有 M=8192/16384（GEMM），所以 bitsandbytes 这个基线覆盖了两种形态（[nf4_benchmark.py:10-37](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L10-L37)）。

#### 4.4.3 源码精读

**环境变量**：[nf4_benchmark.py:1-8](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L1-L8) 必须在 import 前设 `os.environ["BNB_CUDA_VERSION"] = "120"`，否则 bitsandbytes 找不到对应 CUDA 版本的预编译内核。

**量化与反量化 matmul**：[nf4_benchmark.py:46-48](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L46-L48)

```python
B_nf4, state_nf4 = F.quantize_nf4(B, blocksize=128)              # 量化 + blockwise scale
out = bnb.matmul_4bit(A, B_nf4.t(), quant_state=state_nf4)       # 边反量化边乘
```

`quantize_nf4` 返回压缩权重和 `state`（注释 [第 45 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L45-L46) 说明 state 含 absmax/input_shape/dtype/blocksize/quant_type/datatype）。`matmul_4bit` 内部完成 NF4 解包 + matmul，对用户屏蔽了反量化细节——这正是「易用性」的代价：你无法像 TileLang 那样细控调度。

**计时**：[nf4_benchmark.py:54-68](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L54-L68) warmup 10、iter 100、`torch.cuda.synchronize` 屏障，输出 ms。口径与 Marlin（`time.time()`+sleep）、CUTLASS（5/10 次）又不同。

**依赖安装（最轻的一个）**：[install_bitsandbytes.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh) 只有一行 `pip install bitsandbytes`。对照 CUTLASS 的完整 TVM 编译、Marlin 的 git clone+编译，bitsandbytes 走 PyPI 预编译 wheel，安装成本最低。

#### 4.4.4 代码实践

**实践目标**：理解 blockwise 量化的 scale 数量。

**操作步骤**：

1. 取 [nf4_benchmark.py:46](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/dequant_matmul/3.bitsandbytes_benchmark/nf4_benchmark.py#L46-L48) 的 `F.quantize_nf4(B, blocksize=128)`，其中 B 形状 `(N, K) = (16384, 16384)`。
2. 手算：blocksize=128 时，整列 K=16384 被分成多少个 block？每个 block 一个 absmax/scale，所以一列有多少个 scale？整张 B 有多少个 scale？
3. 对比 Marlin 的 `groupsize=-1`（per-channel，每列 1 个 scale），bitsandbytes 的 scale 数量是它的多少倍？

**需要观察的现象**：blockwise 量化的 scale 总数与 `(N, K, blocksize)` 的关系。

**预期结果**：每列 `K/blocksize = 16384/128 = 128` 个 block，所以一列 128 个 scale，整张 B 有 `N * 128 = 16384 * 128 = 2097152` 个 scale。相对 Marlin 的 per-channel（每列 1 个 = 16384 个 scale），bitsandbytes 用了 128 倍的 scale 存储，换取更细的量化粒度与更低误差。

**待本地验证**：把 `blocksize` 从 128 改成 64，量化误差应减小但 scale 存储翻倍——可观察 `matmul_4bit` 输出与 fp16 参考（`torch.matmul(A, B.t())`）的最大绝对误差变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BNB_CUDA_VERSION` 必须在 `import bitsandbytes` **之前**设置？

**答案**：bitsandbytes 在 import 时会根据这个环境变量加载对应 CUDA 版本编译的 .so 内核；import 之后才设变量，库已经按默认/错误版本初始化，无法生效。这是 Python 全局环境变量影响 C 扩展加载顺序的常见陷阱。

**练习 2**：NF4 相对普通 int4 的核心优势是什么？代价是什么？

**答案**：优势是非均匀电平按正态分布最优放置，量化误差更小（适合大模型权重分布）；代价是解包时需要查表（电平不是均匀间距，不能纯移位还原），解码比 int4 的纯移位略复杂，且需要额外的 NF4 电平常数表。

---

## 5. 综合实践

**任务**：完成一张「反量化 matmul 的 provider × 架构覆盖表」，并用一句话给每个基线写定位。

**步骤**：

1. 用 `ls -d` 列出两个目录下的编号子目录：
   - `hopper_benchmark/dequantize_matmul/`
   - `ampere_benchmark/dequant_matmul/`
2. 把结果填入下表（已在讲义里给出答案，建议你先自己 `ls` 再对照）：

| provider | hopper `dequantize_matmul` | ampere `dequant_matmul` | 定位（一句话） |
|---|---|---|---|
| cuBLAS | ✓（`0.`） | ✓（`0.`） | 通用参考基线（fp16，非真量化） |
| Triton | ✓（`1.`） | ✗ | 指针式块级内核基线 |
| Marlin | ✓（`5.`） | ✓（`1.`） | W4A16 专项高速内核 |
| CUTLASS fpa_intb | ✗ | ✓（`2.`） | TVM Relax 自动调度 W4A16 |
| bitsandbytes nf4 | ✗ | ✓（`3.`） | 易用性标杆，NF4 blockwise |
| BitBLAS | ✓（`4.`） | ✓（`4.`） | TileLang 的前身/姊妹量化库 |
| TileLang | ✓（`3.`，int4+fp4 两版） | ✗ | 本项目主角，DSL 自写反量化 |

3. 回答三个问题：
   - 哪些 provider 在两个架构都出现？（cuBLAS、Marlin、BitBLAS）
   - 哪些只在 hopper？（Triton、TileLang）
   - 哪些只在 ampere？（CUTLASS fpa_intb、bitsandbytes）
4. 进一步思考：为什么 TileLang 的反量化只在 hopper 出现，而 ampere 上 TileLang 的位置被 BitBLAS / CUTLASS / bitsandbytes 占据？（提示：ampere 是较早架构，彼时 TileLang 尚未成熟，量化基线主要用更成熟的库；hopper 是新架构，用来展示 TileLang 自家内核的竞争力。）

**预期结果**：你能一眼看出「同一算子在不同架构上的基线生态是不一样的」，这是 u7-l24「跨架构适配」要展开的主题。同时你会注意到编号也不统一——hopper 的 TileLang 是 `3.`、BitBLAS 是 `4.`、Marlin 是 `5.`（缺 `2.`），ampere 的 Marlin 是 `1.`、CUTLASS 是 `2.`、bitsandbytes 是 `3.`、BitBLAS 是 `4.`，印证 u1-l2「编号决定运行顺序、且命名不一致」的结论。

## 6. 本讲小结

- TileLang 在 `3.tilelang-benchmark` 下放了两套反量化 matmul：`fp16xint4`（GEMV、线程级规约、4-bit、W4A16）与 `fp16xfp4`（GEMM、块级 `T.gemm`、2-bit、文件名/注释与代码不符实为 W2A8）——同一类算子、两种调度骨架，体现「结构与精度解耦」。
- `fp16xfp4` 的解包函数 `_tir_u8_to_u2_to_u8` 写死 2-bit、纯移位；默认驱动脚本里 fp4 行被注释、实际跑 int4——又一处「以代码为准」。
- **Marlin** 是 W4A16 专项高速内核，权重预打包成 int32（8 权重/元素）、per-channel scale、计时带 `sleep` 降温；需 `git clone` 编译。
- **CUTLASS fpa_intb** 用 TVM Relax 描述流程、由 CUTLASS 生成群组 GEMM，decode 用 `<<28 >>28` 做符号扩展；依赖最重（编译带 CUTLASS 后端的 TVM，钉死 commit）。
- **bitsandbytes nf4** 是非均匀 NF4 + blockwise(128) 量化，`pip install` 即用、易用性最高、但不可细控调度。
- 三个基线的计时口径各不相同（Marlin `time.time`+sleep、CUTLASS 5/10 次、bnb 10/100 次），跨框架对比前必须统一单位与口径。
- 反量化算子的基线生态按架构分布不同：cuBLAS/Marlin/BitBLAS 两架构都有，Triton/TileLang 仅 hopper，CUTLASS/bitsandbytes 仅 ampere。

## 7. 下一步学习建议

本讲收束了「多精度与量化矩阵乘」单元。接下来：

- 若你想看 **TileLang 的反量化算子在更大算子里的真实用途**，进入第 5 单元 **Attention 系列**（u5-l16 起），那里 FlashAttention 会复用本单元的块级 GEMM 骨架。
- 若你对 **量化基线的全局生态**（不止反量化，还包括 DeepGEMM、aiter/CK、Ladder、FA3 等）感兴趣，直接跳到 u7-l23「对比基线生态总览」，本讲是它的量化算子分论。
- 若你想理解 **为什么同一算子在不同架构上基线不同**（ampere vs hopper vs cdna 的 target、SM、TensorCore 差异），进入 u7-l24「跨架构适配」。
- 建议延伸阅读：在 ampere 的 `contiguous_dequant_matmul/` 目录下还有一套 Marlin/CUTLASS/BitBLAS 的「contiguous」变体（权重连续存放而非压缩），可对照本讲的压缩存放版本，理解两种权重布局的取舍。
