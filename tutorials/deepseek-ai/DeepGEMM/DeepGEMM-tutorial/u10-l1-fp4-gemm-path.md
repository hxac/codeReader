# FP4 GEMM 与 FP8xFP4 路径

> 前置依赖：本讲承接 [u2-l2 缩放因子 recipe 与 UE8M0 打包](u2-l2-scaling-factor-recipe-ue8m0.md)（UE8M0 打包 SF）与 [u6-l1 SM90 FP8 GEMM 1D1D](u6-l1-sm90-fp8-gemm-1d1d-entry.md)（1D1D 设备内核结构）。阅读本讲前，请确认你已经理解 recipe、UE8M0、WGMMA 与「TMA warp + math warp」分工这些概念。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **FP4（e2m1）的 packed 表示**：4 bit 一个元素、两个 nibble 打包进一个 `int8`、以及 `kPackedFP4` 为什么是 `torch.kInt8` 的别名。
- 解释 SM100 上 FP4 的 **TMA 数据类型** `16U4_ALIGN8B`（packed smem）与 `16U4_ALIGN16B`（unpacked smem）的差异，并知道 1D1D 稠密 kernel 走的是哪一条。
- 理解 **FP8xFP4 混合精度**：A、B 可以独立地是 FP8 或 FP4，以及 `gran_k ∈ {32, 128}` 两种缩放粒度在内核里如何被「一个 uint32 打包 4 个 UE8M0」的机制消费。
- 看懂 **UTCCP 加载 SF 的三段式链路**：TMA 把 UE8M0 SF 搬进共享内存 → 一个专用 warp 做 4×32 转置 → UTCCP 指令把它搬进 tensor memory（TMEM）→ 硬件 block-scaled UMMA 通过 `sf_id` 原地吸收缩放因子。
- 列出 **SM100 1D1D kernel 相对 SM90 的关键差异**（2-CTA cluster、TMEM 累加、UTCCP、硬件 SF），并能完成一次 FP8xFP4 GEMM 调用与数值校验。

## 2. 前置知识

### 2.1 为什么要 FP4

FP8 已经把每个权重/激活压到 1 字节，但大模型推理仍受显存带宽限制。**FP4（e2m1）** 进一步把每个元素压到 4 bit（1 位符号 + 2 位指数 + 1 位尾数），带宽再降一半。代价是表示精度极低：e2m1 只能表示 8 个正数值。可以在 `_dequantize_from_fp4_e2m1` 里看到完整的码表：

```python
fp4_values = torch.tensor([0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0], ...)
```

即 FP4 能表达的绝对值集合是 \(\{0, 0.5, 1, 1.5, 2, 3, 4, 6\}\)，再带一个符号位。因此 FP4 **必须配合逐块缩放因子（SF）** 才能用，这正是它与 u2-l2 的 UE8M0 体系天然耦合的原因。

### 2.2 FP8xFP4 是什么

「FP8xFP4」不是某一种新数据类型，而是指 **A 与 B 数据类型可以独立选择** 的混合精度矩阵乘。例如 Mega MoE 的 Linear1 是 `FP8(激活) × FP4(权重)`：激活频繁变化用 FP8 保精度，权重大且静态用 FP4 省带宽。DeepGEMM 的 SM100 内核把这种「任一操作数可能是 FP8 也可能是 FP4」的能力做进了同一个模板内核里。

### 2.3 SM100 的三件新硬件

SM100（Blackwell）相对 SM90（Hopper）带来了本讲反复出现的三个新机制：

- **TMEM（tensor memory）**：一块片上专用存储，UMMA（SM100 的 MMA）把累加结果写进 TMEM 而不是寄存器。
- **UTCCP**：`tcgen05` 指令家族里专门把缩放因子从共享内存拷进 TMEM 的指令，是「硬件吸收 SF」的搬运工。
- **block-scaled UMMA**：一条 `tcgen05.mma.kind::block_scaled` 指令，在乘加的同时用 SF 缩放整块，`sf_id` 在指令描述符里选择用哪个 SF。

这三件事对应 SM90「软件读 FP32 SF 再乘」的升级替代。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh) | SM100 FP8/FP4 1D1D 设备内核模板，本讲主角 |
| [deep_gemm/include/deep_gemm/mma/sm100.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh) | UMMA / SF 描述符构造（`make_sf_desc`、`sf_id`） |
| [csrc/apis/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) | SF 变换：FP32 → 打包 UE8M0（int32）的派发 |
| [csrc/jit_kernels/impls/runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) | TMA 描述符构造、`kPackedFP4 → float_e2m1_unpacksmem_t`、`16U4_ALIGN8B/16B` 映射 |
| [csrc/utils/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp) | `check_ab_fp8_fp4`（FP4 形状 ×2）、`get_default_recipe` |
| [csrc/utils/math.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp) | `kPackedFP4 = torch::kInt8` 的定义 |
| [deep_gemm/utils/math.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py) | `per_token_cast_to_fp4`、`transpose_packed_fp4`（FP4 量化与转置工具） |
| [csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp) | 宿主 Runtime 类：代码生成 + TMA 描述符构造 + 启动 |
| [csrc/apis/gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | `fp8_fp4_gemm_nt` 的架构派发 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**FP4 packed 表示** → **FP8xFP4 混合精度与 SF 粒度** → **UTCCP 加载 SF**。

### 4.1 FP4 packed 表示

#### 4.1.1 概念说明

FP4 e2m1 每个元素 4 bit。但 CUDA / PyTorch 没有原生 4-bit 张量类型，最小可寻址单元是字节。于是 DeepGEMM 用 **「两个 4-bit 元素打包进一个字节」** 的方式存储 FP4：一段逻辑上的 `[M, K]` FP4 张量，物理上存成 `[M, K/2]` 的 `int8` 张量。

这里有个关键约定：PyTorch 的 `torch.int8` 就是 DeepGEMM 的 FP4。源码里它有一个专门的名字：

```cpp
// csrc/utils/math.hpp:11
constexpr auto kPackedFP4 = torch::kInt8;
```

`kPackedFP4` 之所以是 `torch::kInt8` 的别名（见 [csrc/utils/math.hpp:11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp#L11)），是因为「两个 FP4 nibble 共用一个字节」恰好和「一个 int8 字节」在内存里完全等价——库靠上下文（`scalar_type() == kPackedFP4`）来区分它到底是 int8 还是 packed FP4。这也是 u5-l1 里提到的「`kPackedFP4` 实为 `torch::kInt8` 别名」的源头。

打包规则在 `per_token_cast_to_fp4` 里：先把每个元素量化成 4-bit 码（低 4 位），再把相邻两个码拼进一个字节（低 nibble 在前、高 nibble 左移 4 位）：

```python
# deep_gemm/utils/math.py:99-100  （示意，节选关键两行）
codes2 = codes.view(m, padded_n // 2, 2)
packed = (codes2[:, :, 0] & 0x0F) | ((codes2[:, :, 1] & 0x0F) << 4)   # int8
```

缩放因子取 `amax / 6.0`（`6.0` 正是 e2m1 能表示的最大正值，见 [deep_gemm/utils/math.py:85-111](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L85-L111)）。

#### 4.1.2 核心流程：FP4 张量的「逻辑形状」与「物理形状」

因为两个元素挤进一个字节，宿主在拿到一个 `[mn, k]` 的 `kPackedFP4` 张量时，必须先把形状 **还原成元素个数** 才能参与 GEMM 维度校验。这个还原由 `check_ab_fp8_fp4` 完成：

```cpp
// csrc/utils/layout.hpp:45-52
static std::tuple<int, int> check_ab_fp8_fp4(const torch::Tensor& ab, ...) {
    auto [mn, k] = get_shape<2>(ab);
    if (ab.scalar_type() != torch::kFloat8_e4m3fn) {
        DG_HOST_ASSERT(ab.scalar_type() == kPackedFP4 and arch_major == 10);
        major == cute::UMMA::Major::K ? (k *= 2) : (mn *= 2);
    }
    return std::make_tuple(mn, k);
}
```

要点（见 [csrc/utils/layout.hpp:45-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L45-L52)）：

- **FP4 仅在 SM100（arch_major == 10）支持**；SM90 直接断言失败。
- 「×2」加在 **非主维（外维）** 上：K-major 的张量把 K 当内维，打包发生在 K 方向，所以 `k *= 2` 还原 K 的元素数；MN-major 则 `mn *= 2`。这与「两个元素沿打包方向合并」的物理事实一致。

举例：一个 K-major 的 `[M, K]` FP4 张量，物理 dtype 是 `int8`、物理形状 `[M, K/2]`；`check_ab_fp8_fp4` 把它解读回 `(M, K)` 交给后续 GEMM 逻辑。

#### 4.1.3 源码精读：TMA 如何搬运 4-bit 数据

TMA（张量内存加速器）是 SM90 引入的异步批量拷贝引擎。对 FP4，CUDA 提供了两种搬运模式，区别在于 **数据进共享内存后是保持 packed（4-bit）还是被硬件解包成 8-bit**。映射逻辑在 `aten_dtype_to_tensor_map_dtype`：

```cpp
// csrc/jit_kernels/impls/runtime_utils.hpp:86-89
#if CUDA_VERSION >= 12080
    case kPackedFP4:  return fp4_unpacked_smem ? CU_TENSOR_MAP_DATA_TYPE_16U4_ALIGN16B
                                               : CU_TENSOR_MAP_DATA_TYPE_16U4_ALIGN8B;
#endif
```

两种模式的含义（见 [csrc/jit_kernels/impls/runtime_utils.hpp:86-89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L86-L89)）：

| TMA dtype | 别名 | 行为 | 对齐要求 |
| --- | --- | --- | --- |
| `16U4_ALIGN16B` | unpacked smem | 硬件把两个 nibble 解包成两个字节进 smem | gmem 内维 `% 128 == 0` |
| `16U4_ALIGN8B` | packed smem | 保持 4-bit packed 进 smem | 较松（8B 对齐） |

> 后缀 `16B`/`8B` 指 TMA 盒子内维需要满足的字节对齐。`ALIGN16B` 更严（128 个 nibble = 64 字节），换来硬件免费解包；`ALIGN8B` 更松但 kernel 自己要处理 nibble。

本讲的稠密 1D1D kernel 走的是 **unpacked smem（ALIGN16B）** 路径。证据有两条：

1. `make_tma_a_desc`/`make_tma_b_desc` 调用 `make_tma_2d_desc` 时不传 `fp4_unpacked_smem`，取默认值 `true`（见 [csrc/jit_kernels/impls/runtime_utils.hpp:113-150](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L113-L150)，签名默认 `fp4_unpacked_smem = true`）。
2. 设备侧注入的 dtype 是 `cutlass::detail::float_e2m1_unpacksmem_t`（名字里就有 `unpacksmem`）：

```cpp
// csrc/jit_kernels/impls/runtime_utils.hpp:60
case kPackedFP4:  return "cutlass::detail::float_e2m1_unpacksmem_t";
```

这个字符串会通过 JIT 代码生成（见 4.2.3）成为设备模板的 `a_dtype_t`/`b_dtype_t`，让编译器把共享内存里的 FP4 当成「已解包元素」处理（见 [csrc/jit_kernels/impls/runtime_utils.hpp:54-63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L54-L63)）。

`make_tma_2d_desc` 里还有一处 FP4 专用的 smem 维度修正——packed smem 且开了 swizzle 时，共享内存内维要写成 `swizzle_mode * 2`（因为 packed 下每个「元素」实际占半个字节，维度按字节计要翻倍）：

```cpp
// csrc/jit_kernels/impls/runtime_utils.hpp:124-131
if (t.scalar_type() == kPackedFP4) {
    DG_HOST_ASSERT(not fp4_unpacked_smem or gmem_inner_dim % 128 == 0);  // unpacked 要 128 对齐
    if (not fp4_unpacked_smem and swizzle_mode != 0)
        smem_inner_dim = swizzle_mode * 2;                                 // packed+swizzle 修正
}
```

#### 4.1.4 代码实践：亲手打包一个 FP4 张量

1. **实践目标**：验证「两个 FP4 nibble 打包进一个 int8」与库的工具函数一致。
2. **操作步骤**：在能 `import deep_gemm` 的环境里运行下面这段脚本（示例代码，非项目原有文件）。

   ```python
   import torch
   from deep_gemm.utils.math import _dequantize_from_fp4_e2m1, per_token_cast_to_fp4

   torch.manual_seed(0)
   x = torch.randn(2, 128, device='cuda', dtype=torch.bfloat16) * 3
   packed, sf = per_token_cast_to_fp4(x, use_ue8m0=True, gran_k=128)
   print(packed.dtype, packed.shape)   # 期望: torch.int8, [2, 64]   ← K=128 元素 → 64 字节
   ```

3. **需要观察的现象**：
   - `packed.dtype` 是 `torch.int8`（即 `kPackedFP4`），`packed.shape` 是 `[2, 64]` 而非 `[2, 128]`——印证了 4.1.2 的「K 方向 ×2」。
   - `sf` 是 `int32`（4 个 UE8M0 打包成一个 int），形状 `[2, 1]`（128 个 K / (gran_k=128 × 4) = 1，见 4.2.2）。
4. **预期结果**：`packed` 每个字节的高/低 nibble 各是一个 FP4 码。
5. **若无法运行**（无 SM100 GPU 或未构建）：标注「待本地验证」，改为阅读 [_dequantize_from_fp4_e2m1](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L130-L134) 的码表 `{0,0.5,1,1.5,2,3,4,6}`，确认 e2m1 只有 8 个正值。

#### 4.1.5 小练习与答案

**练习 1**：一个 MN-major 的 `[N, K]` FP4 权重，物理上 `int8` 形状是什么？`check_ab_fp8_fp4` 返回什么？
**答案**：MN-major 把 MN 当内维打包，物理形状 `[N, K/2]`（打包发生在 N 方向需要先看清楚——实际上打包沿存储连续维）。`check_ab_fp8_fp4` 命中 `major == MN` 分支执行 `mn *= 2`，返回 `(2N, K)` 的元素级形状。

**练习 2**：为什么 `kPackedFP4` 复用 `torch::kInt8` 而不是新建一个 dtype？
**答案**：PyTorch 没有原生 4-bit 类型，最小可寻址单元是字节；两个 nibble 恰好填满一个 `int8`，内存布局完全等价。库用 `scalar_type() == kPackedFP4` 在语义层区分，避免改动 PyTorch 的类型系统。

---

### 4.2 FP8xFP4 混合精度与 SF 粒度

#### 4.2.1 概念说明

SM100 的 1D1D 内核是一个 **统一的混合精度模板**：模板参数 `a_dtype_t` 和 `b_dtype_t` 可以各自独立地是 FP8（`float_e4m3_t`）或 FP4（`float_e2m1_unpacksmem_t`）。于是同一个 kernel 既算 FP8×FP8，也算 FP8×FP4（Mega MoE 的典型配置），甚至 FP4×FP4。这一点从模板签名看得很清楚：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:31
typename a_dtype_t, typename b_dtype_t, typename cd_dtype_t,
```

而 `GemmDesc::check_validity` 在宿主侧也允许 A、B 各自是 FP8 或 FP4：

```cpp
// csrc/jit_kernels/heuristics/config.hpp:43-44
DG_HOST_ASSERT(a_dtype == torch::kFloat8_e4m3fn or a_dtype == kPackedFP4);
DG_HOST_ASSERT(b_dtype == torch::kFloat8_e4m3fn or b_dtype == kPackedFP4);
```

混合精度的另一个维度是 **缩放粒度 `gran_k`**。FP8 时代 SM90 的 recipe 固定 `(1, 1, 128)`（每 128 个 K 通道一个 SF）。SM100 放宽到 `gran_k ∈ {32, 128}`：32 是更细的粒度（类似 MXFP8），128 是更粗、更省 SF 的粒度。两种粒度在内核里走同一套「一个 uint32 打包 4 个 UE8M0」的机制，但消费方式不同。

#### 4.2.2 核心流程：一个 uint32 如何承载 4 个 UE8M0

回顾 u2-l2：SM100 把 4 个 UE8M0 缩放因子打包进一个 `int32`。问题是——这 4 个 UE8M0 是沿哪个方向排的？答案：**沿 K 方向排**。由此可以推出一个优雅的统一公式。

设每个 SF 元素（一个 uint32）覆盖的 K 元素数为：

\[
\text{K per uint32} = \text{gran\_k} \times 4
\]

因为 1 个 uint32 = 4 个 UE8M0，每个 UE8M0 缩放 `gran_k` 个 K 元素。于是 SF 的 K 维长度（[csrc/apis/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) 的 `make_tma_sf_desc` 里就用了它）为：

\[
\text{shape\_sfa\_k} = \left\lceil \frac{\text{shape\_k}}{\text{gran\_k} \times 4} \right\rceil
\]

设备内核里正是这么算的（`kGranKA * 4`）：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:126-127
const auto shape_sfa_k = math::ceil_div(shape_k, kGranKA * 4);
const auto shape_sfb_k = math::ceil_div(shape_k, kGranKB * 4);
```

代入两种粒度，并与「`BLOCK_K == 128`（见下文断言）」对照：

| `gran_k` | 一个 uint32 覆盖 K | 折合多少个 BLOCK_K(=128) 阶段 | `kNumSFAStagesPerLoad` | `sfa_id` 取法 |
| --- | --- | --- | --- | --- |
| 32  | \(32 \times 4 = 128\)  | 1 个阶段  | 1 | `kUMMAKIdx`（0–3） |
| 128 | \(128 \times 4 = 512\) | 4 个阶段  | 4 | `k_block_idx % 4` |

这两列直接对应源码（见 [sm100_fp8_fp4_gemm_1d1d.cuh:65-66](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L65-L66)）：

```cpp
constexpr uint32_t kNumSFAStagesPerLoad = kGranKA == 32 ? 1 : 4;
constexpr uint32_t kNumSFBStagesPerLoad = kGranKB == 32 ? 1 : 4;
DG_STATIC_ASSERT(kGranKA == 32 or kGranKA == 128, "Invalid granularity K for A");
```

直觉解释：SF 的 TMA 加载开销不小，所以内核尽量 **让一次 SF 加载覆盖尽量多个 k-阶段**。

- `gran_k=128`：一个 uint32 里的 4 个 UE8M0 各盖 128 K，合计 512 K = 4 个 BLOCK_K 阶段；所以 SF 每 4 个阶段才重载一次（`kNumSFAStagesPerLoad=4`），而当前阶段用第几个 UE8M0 由 `sfa_id = k_block_idx % 4` 选出。
- `gran_k=32`：一个 uint32 的 4 个 UE8M0 各盖 32 K，合计 128 K = 恰好 1 个 BLOCK_K；每个阶段都要重载 SF，而块内 4 个 `UMMA_K=32` 子段各用第 `kUMMAKIdx` 个 UE8M0。

#### 4.2.3 源码精读：代码生成如何把 dtype 灌进模板

混合精度的「任一操作数 FP8/FP4」是靠 JIT 代码生成落地的。宿主 Runtime 类 `SM100FP8FP4Gemm1D1DRuntime::generate_impl` 用 `fmt::format` 把 17 个编译期常量填进一段极薄的 `.cu` 源码，其中就包括 `a_dtype` / `b_dtype`（经 `to_string` 转成 C++ 记号）：

```cpp
// csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:60（节选自 generate_impl 的实参列表）
        to_string(args.gemm_desc.a_dtype), to_string(args.gemm_desc.b_dtype), to_string(args.gemm_desc.cd_dtype),
```

`to_string(torch::kFloat8_e4m3fn)` 得 `"cutlass::float_e4m3_t"`，`to_string(kPackedFP4)` 得 `"cutlass::detail::float_e2m1_unpacksmem_t"`（见 [runtime_utils.hpp:54-63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L54-L63)）。于是同一段模板，FP8×FP8 注入 `(e4m3_t, e4m3_t)`，FP8×FP4 注入 `(e4m3_t, float_e2m1_unpacksmem_t)`——这就是「一个 kernel 吃多种精度」的全部秘密（见 [csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:38-81](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L38-L81)）。

另一个被注入的编译期常量是 `gran_k_a` / `gran_k_b`，它们成为设备模板的 `kGranKA` / `kGranKB`，直接决定 4.2.2 里的 `kNumSFAStagesPerLoad` 与 `sfa_id` 选法。

SF 的 layout 变换则发生在更上层：`fp8_fp4_gemm_nt` 调 `transform_sf_pair_into_required_layout`，在 SM100 上把用户的 FP32 SF 变换成 **MN-major、TMA 对齐、打包成 int32（UE8M0）** 的形式（见 [csrc/apis/layout.hpp:49-58](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp#L49-L58)）：

```cpp
// csrc/apis/layout.hpp:49-54  （SM100: FP32 → 打包 UE8M0 int32）
if (sf.scalar_type() == torch::kFloat and (gran_k == 32 or gran_k == 128) and arch_major == 10) {
    DG_HOST_ASSERT(not disable_ue8m0_cast);
    const auto broadcasted = ...;
    return get_mn_major_tma_aligned_packed_ue8m0_tensor(broadcasted, psum_layout);
}
```

注意分支条件明确接受 `gran_k == 32 or gran_k == 128`——这正是 SM100 支持两种粒度的宿主侧入口。变换后 `sfa.scalar_type()` 变成 `torch::kInt`，成为派发的判据。

#### 4.2.4 源码精读：arch_major + SF dtype 双条件派发

`fp8_fp4_gemm_nt` 的派发逻辑印证了「SM90 用 FP32 SF、SM100 用打包 UE8M0(int32) SF」这一架构分水岭（见 [csrc/apis/gemm.hpp:106-123](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L106-L123)）：

```cpp
// csrc/apis/gemm.hpp:106-123
const auto [sfa, sfb, gran_k_a, gran_k_b] = layout::transform_sf_pair_into_required_layout(...);

if (arch_major == 9 and sfa.scalar_type() == torch::kFloat) {
    ...  // SM90: sm90_fp8_gemm_1d1d / 1d2d，FP32 SF
} else if (arch_major == 10 and sfa.scalar_type() == torch::kInt) {
    sm100_fp8_fp4_gemm_1d1d(a.first, sfa, b.first, sfb, c, d, m, n, k,
                            gran_k_a, gran_k_b, major_a, major_b, compiled_dims);
} else {
    DG_HOST_UNREACHABLE("Unsupported architecture or scaling factor types");
}
```

这里 `gran_k_a` / `gran_k_b` 由 `transform_sf_pair_into_required_layout` 一并返回（它是从 recipe 里解出的），再透传进设备 kernel 成为 `kGranKA` / `kGranKB`。`disable_ue8m0_cast=True` 时 SM100 会退化回 FP32 SF 路径（但稠密 1D1D 不支持，故测试里 `disable_ue8m0_cast = not use_ue8m0` 且 1D1D 强制 `use_ue8m0=True`，见 [tests/generators.py:88-91](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L88-L91)）。

#### 4.2.5 代码实践：跟踪一次 FP8xFP4 调用的派发

1. **实践目标**：把 4.2.2 的「uint32 覆盖多少 K」公式落实到真实 recipe。
2. **操作步骤**：阅读 [tests/generators.py:43-79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L43-L79) 的 `QuantConfig`，注意 SM100 多出的那个 `QuantConfig((128, 32, False, True))`——它表示 `gran_k_a=128, gran_k_b=32, A=FP8, B=FP4`。这正是 Mega MoE Linear1 的典型配置（FP8 激活 × FP4 权重）。
3. **需要观察的现象**：
   - `get_recipes()` 对这个 config 返回 `recipe_a=(1, 128), recipe_b=(1, 32)`。
   - 代入公式：SFA 的 K 维 = ⌈k / (128×4)⌉ = ⌈k/512⌉；SFB 的 K 维 = ⌈k / (32×4)⌉ = ⌈k/128⌉。两者不同，因为 A、B 粒度不同。
4. **预期结果**：手算 `k=4096` 时，SFA 沿 K 有 8 个 uint32、SFB 有 32 个 uint32。
5. 若无 SM100 机器，标注「待本地验证」，改为纯源码阅读：确认 `gran_k_a ≠ gran_k_b` 是合法的（`DG_STATIC_ASSERT(not is_k_grouped_contiguous(...) or kGranKA == kGranKB, ...)` 仅在 K-grouped 时要求相等，见 [sm100_fp8_fp4_gemm_1d1d.cuh:69](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L69)）。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `gran_k=128` 时 `kNumSFAStagesPerLoad=4`，而 `gran_k=32` 时是 1？
**答案**：一个 uint32 打包 4 个 UE8M0，覆盖 `gran_k×4` 个 K 元素。`gran_k=128` → 覆盖 512 K = 4 个 BLOCK_K(128) 阶段，所以一次 SF 加载管 4 个阶段；`gran_k=32` → 覆盖 128 K = 1 个阶段，每阶段都要重载。

**练习 2**：FP8×FP4 时 A 是 FP8、B 是 FP4，`a_dtype_t` / `b_dtype_t` 分别被 JIT 注入成什么 C++ 类型？
**答案**：`a_dtype_t = cutlass::float_e4m3_t`，`b_dtype_t = cutlass::detail::float_e2m1_unpacksmem_t`（unpacked smem 模式）。

---

### 4.3 UTCCP 加载 UE8M0 缩放因子

#### 4.3.1 概念说明

SM90 的 SF 处理是「软件」的：math 线程用 `ld_shared` 把 FP32 SF 读进寄存器，再在每次 WGMMA 后手动乘上去（见 u6-l1 的 `final_accum` 分离）。SM100 把这件事**搬进硬件**：SF 不进寄存器、不进共享内存计算路径，而是被专门搬进 **TMEM**，由 block-scaled UMMA 指令在乘加时原地吸收。

负责把 SF 从共享内存搬进 TMEM 的指令就是 **UTCCP**（可理解为 "UMMA Tensor Copy"）。但 UE8M0 在共享内存里的排列不能直接喂给 UTCCP——硬件要求的 TMEM 布局与 TMA 落盘的布局不一致，中间需要一次 **4×32 转置**。所以 SM100 1D1D kernel 的 SF 链路是 **三段式**：

\[
\underbrace{\text{gmem}}_{\text{uint32 打包 UE8M0}}
\xrightarrow[\text{warp 0}]{\text{TMA}}
\underbrace{\text{smem（TMA 布局）}}_{\text{未转置}}
\xrightarrow[\text{warp 2}]{\text{4×32 转置}}
\underbrace{\text{smem（UTCCP 布局）}}_{\text{已转置}}
\xrightarrow[\text{warp 1}]{\text{UTCCP}}
\underbrace{\text{TMEM}}_{\text{硬件读取}}
\]

这与 u8-l2 讲的 Mega MoE 权重侧 `_transpose_sf_for_utccp` 是同一套转置规则，只不过 4.3 这里发生在激活/权重的 **逐 tile** 加载流水线里。

#### 4.3.2 核心流程：五个 warp 角色与 SF 专用屏障

SM100 1D1D kernel 的线程划分比 SM90 更细。对比 u6-l1 的 SM90「1 个 TMA warp + N 个 math warp」，SM100 分成 **四类 warp 角色**（见 [sm100_fp8_fp4_gemm_1d1d.cuh:206-521](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L206-L521)）：

| warp | 角色 | 与 SF 的关系 |
| --- | --- | --- |
| `warp_idx == 0` | TMA 加载 warp | 发 TMA 把 SFA/SFB 搬进 smem，再 `full_barriers` 通知 |
| `warp_idx == 1`（leader CTA） | MMA issue warp | 发 UTCCP 把 SF 搬进 TMEM，发 block-scaled UMMA |
| `warp_idx == 2` | UTCCP 转置 warp | 对 smem 里的 SF 做 4×32 转置 |
| `warp_idx >= kNumNonEpilogueThreads/32` | Epilogue warp | 把 TMEM 累加结果经 TMA store 写回 gmem |

SF 链路专门多了一组屏障 `with_sf_full_barriers`：TMA warp 把 SF 落盘后 arrive、转置 warp 转置完 arrive、MMA warp 在 `with_sf_full_barriers[stage_idx]->wait(phase)` 等到「SF 已转置完毕」才发 UTCCP/UMMA（见 [sm100_fp8_fp4_gemm_1d1d.cuh:342](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L342)）。这正是三段式链路在生产者-消费者之间的握手。

TMEM 的列分配也把 SF 算了进去（累加器、SFA、SFB 各占一段，见 [sm100_fp8_fp4_gemm_1d1d.cuh:96-103](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L96-L103)）：

```cpp
constexpr uint32_t kNumAccumTmemCols = UMMA_N * kNumEpilogueStages;
constexpr uint32_t kNumSFATmemCols = SF_BLOCK_M / 32;
constexpr uint32_t kNumSFBTmemCols = SF_BLOCK_N / 32;
```

#### 4.3.3 源码精读：SF 的 TMA 加载

TMA warp（warp 0）在特定阶段发 SFA/SFB 的 TMA。注意它 **不开 swizzle**（`make_tma_sf_desc` 里 `swizzle_mode == 0`），且一次 TMA 拉一整列 SF：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:262-275
if (k_block_idx % kNumSFAStagesPerLoad == 0) {
    uint32_t sfa_m_idx = m_block_idx * BLOCK_M;
    uint32_t sfa_k_idx = scheduler.template get_global_idx<..., sched::IndexType::SF_K>(
        shape_sfa_k, 1, math::ceil_div(k_idx, BLOCK_K * kNumSFAStagesPerLoad));
    tma::copy<BLOCK_M, 1, 0>(&tensor_map_sfa, full_barriers[stage_idx], smem_sfa[stage_idx], sfa_m_idx, sfa_k_idx);
    num_arrival_bytes += BLOCK_M * sizeof(uint32_t);
}
```

`sfa_k_idx` 用 `k_idx / (BLOCK_K * kNumSFAStagesPerLoad)` 计算——正好对应 4.2.2 的「一次 SF 加载覆盖 `kNumSFAStagesPerLoad` 个阶段」：`gran_k=128` 时除以 `128*4=512`，`gran_k=32` 时除以 `128*1=128`。

#### 4.3.4 源码精读：4×32 转置

转置 warp（warp 2）的核心是一个 `utccp_required_smem_warp_transpose` lambda：每 128 个对齐元素里，32 个 lane 各读 4 个 uint32（`i*32 + lane_idx`），再写回 `lane_idx*4` 的位置——这就是一个 4×32 的转置：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:434-442
auto utccp_required_smem_warp_transpose = [&](const uint32_t* smem_ptr) {
    DG_STATIC_ASSERT(kNumUTCCPAlignedElems == 128, "Invalid aligned elements");
    uint32_t values[4];
    #pragma unroll
    for (uint32_t i = 0; i < 4; ++ i)
        values[i] = ptx::ld_shared(smem_ptr + i * 32 + lane_idx);
    __syncwarp();
    ptx::st_shared(smem_ptr + lane_idx * 4, values[0], values[1], values[2], values[3]);
};
```

这个置换等价于 `(idx % 32) * 4 + idx / 32`，与 Mega MoE 的 `_transpose_sf_for_utccp` 逐位一致（见 u8-l2）。转置完用 `fence_view_async_shared` 让异步代理可见，再 arrive `with_sf_full_barriers` 放行 MMA warp（见 [sm100_fp8_fp4_gemm_1d1d.cuh:444-469](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L444-L469)）。

#### 4.3.5 源码精读：UTCCP 把 SF 搬进 TMEM

MMA warp（warp 1）在确认 SF 已转置后，用 UTCCP 把它搬进 TMEM。SF 的共享内存描述符由 `make_sf_desc` 构造——它是 K-major、atom 为 `8×128bit`、`SBO=8*16`、`LBO=0`（见 [mma/sm100.cuh:42-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L42-L48)）：

```cpp
// deep_gemm/include/deep_gemm/mma/sm100.cuh:41-48
CUTLASS_DEVICE
cute::UMMA::SmemDescriptor make_sf_desc(void* smem_ptr) {
    // NOTES: the UTCCP layout is K-major by default
    // Atom size: 8 x 128 bits; {SBO, LBO} is byte stride between atoms on {MN, K}
    // Since the UTCCP we used is 128b-wide (only 1 atom on K), so LBO can be zero
    return make_smem_desc(cute::UMMA::LayoutType::SWIZZLE_NONE, smem_ptr, 8 * 16, 0);
}
```

UTCCP 指令本身按 `kNumMulticast` 选 1-CTA 或 2-CTA 版本，写入 TMEM 的 SF 列区（`kTmemStartColOfSFA + i*4`）：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:350-358
using cute_utccp_t = cute::conditional_t<kNumMulticast == 1,
    cute::SM100_UTCCP_4x32dp128bit_1cta, cute::SM100_UTCCP_4x32dp128bit_2cta>;
...
for (uint32_t i = 0; i < SF_BLOCK_M / kNumUTCCPAlignedElems; ++ i) {
    auto smem_ptr = smem_sfa[stage_idx] + i * kNumUTCCPAlignedElems;
    mma::sm100::replace_smem_desc_addr(sf_desc, smem_ptr);
    cute_utccp_t::copy(sf_desc, kTmemStartColOfSFA + i * 4);
}
```

SF 进了 TMEM 之后，block-scaled UMMA 通过指令描述符里的 `sf_id` 选择当前乘加用哪个 SF。`sf_id` 正是 4.2.2 表里的 `sfa_id`/`sfb_id`，由 `make_runtime_instr_desc_with_sf_id` 写进 64 位指令描述符的高 32 位：

```cpp
// deep_gemm/include/deep_gemm/mma/sm100.cuh:135-139
CUTLASS_DEVICE uint64_t make_runtime_instr_desc_with_sf_id(
    cute::UMMA::InstrDescriptorBlockScaled desc, const uint32_t& sfa_id, const uint32_t& sfb_id) {
    desc.a_sf_id_ = sfa_id, desc.b_sf_id_ = sfb_id;
    return static_cast<uint64_t>(static_cast<uint32_t>(desc)) << 32;
}
```

而发起 UMMA 的指令是 `SM100_MMA_MXF8F6F4_SS`（单 CTA）或 `_2x1SM_SS`（2-CTA multicast），名字里的 `MXF8F6F4` 正说明它吃 FP8/FP6/FP4 混合精度：

```cpp
// deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:372-392
using mma_t = cute::conditional_t<
    kNumMulticast == 1, ptx::SM100_MMA_MXF8F6F4_SS, ptx::SM100_MMA_MXF8F6F4_2x1SM_SS>;
...
mma_t::fma(a_desc, b_desc, accum_stage_idx * UMMA_N,
           kUMMAKIdx > 0 or k_block_idx > 0, runtime_instr_desc,
           kTmemStartColOfSFA, kTmemStartColOfSFB);
```

注意第 5 个参数 `kUMMAKIdx > 0 or k_block_idx > 0`：它告诉 UMMA **是否累加到已有结果**（第一个子段是覆盖，后续是累加）。这与 SM90「accum/final_accum 分离」对应，但累加发生在 TMEM 里、由硬件完成，不需要软件分离两个累加器。

#### 4.3.6 代码实践：阅读 UTCCP 链路的同步序列

1. **实践目标**：把 4.3.1 的三段式链路与源码里的屏障一一对应。
2. **操作步骤**：在 [sm100_fp8_fp4_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh) 里定位三处：
   - warp 0 的 SFA/SFB TMA（L262–275）发完后 `full_barriers[stage_idx]->arrive_and_expect_tx(...)`；
   - warp 2 的转置（L451–464）发完后 `with_sf_full_barriers[stage_idx]->arrive(0u)`；
   - warp 1 的 MMA（L342）开头 `with_sf_full_barriers[stage_idx]->wait(phase)`。
3. **需要观察的现象**：SF 的数据流是「TMA warp 产出 → 转置 warp 消费并产出 → MMA warp 消费」，三段用 `full_barriers` 和 `with_sf_full_barriers` 两组 mbarrier 串联，phase 翻转驱动软件流水线（见 [L197-L203](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L197-L203) 的 `advance_pipeline`）。
4. **预期结果**：能画出「TMA → full_barrier → 转置 → with_sf_full_barrier → UTCCP+UMMA」的时序图。
5. 若无法运行：标注「待本地验证」，这是一次纯源码阅读实践。

#### 4.3.7 小练习与答案

**练习 1**：为什么 SF 进共享内存后还需要一次转置，而 A/B 数据不需要？
**答案**：TMA 落盘的 SF 布局是 TMA 友好的 MN-major 列序，但 UTCCP 硬件指令期望的 TMEM 源布局是 4×32 的 K-major atom 排列。两者不一致，必须用 4×32 转置对齐；A/B 数据的布局由 UMMA 的 `SmemDescriptor` 直接描述，不需要额外转置。

**练习 2**：`sf_id`（sfa_id/sfb_id）的作用是什么？它由谁计算？
**答案**：`sf_id` 是 block-scaled UMMA 指令描述符里的字段，告诉硬件当前乘加用 TMEM 中哪一个 UE8M0 SF。它由 MMA warp 在 `issue_umma` 里按粒度计算：`gran_k=32` 用 `kUMMAKIdx`，`gran_k=128` 用 `k_block_idx % 4`（见 [L376-L377](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L376-L377)）。

---

## 5. 综合实践：对比 SM90 fp8 与 SM100 fp8_fp4 两条 GEMM 路径

本实践把三个最小模块串起来。请你以本讲学到的源码为依据，完成下面这张对比表（先自己填，再对照参考答案）。这是本讲规格里要求的代码实践任务。

### 5.1 待填写的对比表

| 维度 | SM90 FP8 路径（`sm90_fp8_gemm_1d1d`） | SM100 FP8xFP4 路径（`sm100_fp8_fp4_gemm_1d1d`） |
| --- | --- | --- |
| 支持的 dtype | ? | ? |
| SF 数据格式 | ? | ? |
| SF 如何参与计算 | ?（软件/硬件？） | ? |
| SF 加载链路 | TMA → smem → 寄存器 | ?（三段式） |
| MMA 指令 | WGMMA（`mma.async`） | ? |
| 累加器位置 | 寄存器 | ? |
| Cluster / multicast 模型 | ?（动态可关） | ?（固定 2-CTA） |
| `BLOCK_K` 约束 | 128 | ? |

### 5.2 操作步骤

1. 阅读 [sm90_fp8_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) 的内核签名与线程分工（结合 u6-l1），提取左列依据。
2. 阅读本讲的 [sm100_fp8_fp4_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh)，提取右列依据。
3. 在 SM100 机器上（可选）跑一次真实 FP8xFP4 调用并校验：

   ```python
   import torch, deep_gemm
   from deep_gemm.testing import calc_diff
   from tests.generators import generate_normal, enumerate_normal, KernelType, get_ue8m0_usage
   # 示例代码：取一个 FP8xFP4 case（B 为 FP4，需 SM100）
   # 参考 tests/test_fp8_fp4.py 的 test_gemm 写法
   ```

4. 用 `calc_diff(d, ref_d)` 校验，FP8xFP4 的阈值是 `0.01`（仅一端 FP4）或 `0.02`（双端 FP4），见 [tests/generators.py:65-70](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L65-L70)。

### 5.3 参考答案

| 维度 | SM90 FP8 路径 | SM100 FP8xFP4 路径 |
| --- | --- | --- |
| 支持的 dtype | 仅 FP8（e4m3） | FP8（e4m3）与 FP4（packed e2m1）任意混合 |
| SF 数据格式 | FP32 | 打包 UE8M0（4 个打包进一个 `int32`） |
| SF 如何参与计算 | **软件**：math 线程读 FP32 SF 再手动乘 | **硬件**：block-scaled UMMA 经 `sf_id` 原地吸收 |
| SF 加载链路 | TMA → smem → 寄存器 | TMA → smem → **warp2 的 4×32 转置** → **UTCCP** → TMEM |
| MMA 指令 | WGMMA（`mma.async`，M=64/K=32） | UMMA（`tcgen05.mma`，`SM100_MMA_MXF8F6F4_SS`） |
| 累加器位置 | 寄存器（`accum`/`final_accum` 分离） | TMEM（tensor memory，硬件累加） |
| Cluster / multicast | 1 CTA/SM；multicast **逐 CTA 动态可关** | 固定 **2-CTA cluster**；multicast 由启发式静态保证、运行时不可关 |
| `BLOCK_K` 约束 | 128（`Only support per-128-channel FP8 scaling`） | 128（`DG_STATIC_ASSERT(BLOCK_K == 128, ...)`） |

两条路径在 `BLOCK_K=128` 上的巧合并非偶然：它同时被「SF 每 128 个 K 通道一个」的粒度与 MMA 的 K 步长共同钉死（SM90 见 u6-l1，SM100 见 [sm100_fp8_fp4_gemm_1d1d.cuh:55](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L55)）。

> 验证提示：如果你有 SM100 GPU，运行 `DG_JIT_DEBUG=1 python tests/test_fp8_fp4.py`，可在控制台看到 `Making TMA desc: ...` 打印，其中 FP4 权重的 `elem size` 与 swizzle 取值会印证 4.1.3 的 ALIGN16B 路径；若无 SM100，则标注「待本地验证」，本实践以源码阅读为主。

## 6. 本讲小结

- **FP4 = 打包的 e2m1**：两个 4-bit nibble 拼进一个 `int8`，`kPackedFP4` 即 `torch::kInt8` 的别名；宿主用 `check_ab_fp8_fp4` 把物理形状「×2」还原成元素形状，且 FP4 仅在 SM100 支持。
- **TMA 给 FP4 两种搬运模式**：`16U4_ALIGN16B`（unpacked，硬件解包成字节）与 `16U4_ALIGN8B`（packed）；稠密 1D1D kernel 走 unpacked 路径，设备 dtype 注入为 `float_e2m1_unpacksmem_t`。
- **FP8xFP4 是同一份模板**：A、B 各自独立 FP8/FP4，靠 JIT 把 `to_string(dtype)` 灌进 `a_dtype_t`/`b_dtype_t`；`gran_k ∈ {32,128}` 的统一公式是「一个 uint32 覆盖 `gran_k×4` 个 K 元素」。
- **SF 粒度决定加载节奏**：`gran_k=128` 时一个 uint32 跨 4 个阶段（`kNumSFAStagesPerLoad=4`、`sfa_id=k_block_idx%4`）；`gran_k=32` 时跨 1 个阶段（每阶段重载、`sfa_id=kUMMAKIdx`）。
- **UTCCP 是 SM100 的硬件 SF 搬运工**：SF 经「TMA → 4×32 转置 → UTCCP → TMEM」三段式进入硬件，block-scaled UMMA 用 `sf_id` 原地吸收，彻底取代 SM90 的软件乘法。
- **SM100 相对 SM90 的三大升级**：TMEM 累加（替寄存器）、硬件 SF（替软件乘）、固定 2-CTA cluster（替动态可关 multicast）。

## 7. 下一步学习建议

- 想看 FP8xFP4 在真实融合场景里的用法，继续 [u8 Mega MoE 单元](u8-l1-mega-moe-symm-memory.md)：Mega MoE 的 Linear1/Linear2 就是 FP8×FP4，且其权重侧的 `_transpose_sf_for_utccp` 与本讲 4.3.4 的转置逐位一致。
- 想深入 SM100 的 epilogue（TMEM → gmem 的 store 路径），阅读 [u10-l2 Epilogue 与存储变换](u10-l2-epilogue-store-transform.md)，本讲 warp ≥ `kNumNonEpilogueThreads/32` 调用的 `sm100_store_cd` 会在那里展开。
- 想完整理解架构派发与 `disable_ue8m0_cast` 的退化路径，回顾 [u2-l3 C++ 绑定与 API 派发层](u2-l3-cpp-binding-and-dispatch.md)。
- 想看 FP4 的另一种 kernel（MQA 评分里的 FP4 KV），阅读 [u9-l1 索引器的 MQA 评分内核](u9-l1-mqa-scoring-indexer.md)，它复用了本讲的 packed FP4 + UTCCP 机制。
