# FP4 GEMM（NVFP4/MXFP4，Blackwell）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 **FP4（E2M1）** 4 比特浮点格式是什么、它的取值集合与“每两个值打包成一个字节”的约定。
- 区分 **NVFP4** 与 **MXFP4** 两种量化配方：块大小（16 vs 32）、块缩放格式（FP8-E4M3 vs UE8M0）、是否带全局缩放。
- 理解 **block-scale 重排（interleave / swizzle）**：为什么 Blackwell MMA 需要 128×4 交织布局，以及 `mm_bf16_fp4` 的两个后端又如何各自把它“反交织”回自己要的形状。
- 掌握 `flashinfer.mm_bf16_fp4` 的 **BF16×FP4（W4A16）** 调用方式，以及 cuDNN 与 CuTe-DSL 两个后端的差异与选择依据。
- 用 `nvfp4_quantize` + `prepare_bf16_fp4_weights` + `mm_bf16_fp4` 跑通一次 FP4 矩阵乘，并与 BF16 参考结果比较相对误差。

本讲是 [u5-l1 GEMM 全景](u5-l1-gemm-overview.md) 与 [u5-l2 FP8 GEMM](u5-l2-fp8-gemm.md) 的延续。u5-l2 讲的是 8 比特缩放粒度的 FP8 GEMM；本讲把“比特数再砍一半”到 4 比特权重（W4A16），重点落在 **块缩放的重排** 与 **后端差异** 上。

## 2. 前置知识

在进入源码前，先用三段直觉建立认知。

### 2.1 为什么要 4 比特权重

大模型推理是 **访存带宽受限（memory-bound）** 的：每生成一个 token，都要把整套权重从显存搬进 SM。权重存得越窄，搬运的字节越少，单 token 延迟就越低。把 BF16（2 字节）压成 FP4（0.5 字节），理论上权重的显存占用和搬运量降到 1/4。代价是数值精度下降，所以必须给每个“小块”配一个缩放因子来找回动态范围——这就是 **block-scaled FP4**。

LLM 服务里最常见的两种 FP4 配方：

- **NVFP4（NVIDIA FP4）**：块大小 16，块缩放用 **FP8-E4M3**（最大值 448），外加一个全局 FP32 缩放。本讲的主角 `mm_bf16_fp4` 走的就是 NVFP4。
- **MXFP4（Microscaling FP4）**：块大小 32，块缩放用 **UE8M0**（即 8 比特纯指数 `2^(byte-127)`），无独立全局缩放。

### 2.2 FP4（E2M1）长什么样

4 比特浮点 **E2M1**：1 个符号位 + 2 个指数位 + 1 个尾数位，正半轴只有 8 个可表示值。源码里有一张查表把 4 比特码直接映射成 float：

\[ \text{code} \in \{0,0.5,1,1.5,2,3,4,6\}\quad(\text{正半轴}),\quad \text{负半轴取相反数} \]

最大可表示幅度是 **6.0**（记作 `FLOAT4_E2M1_MAX`）。两个 FP4 值“打包”进一个字节：**低 4 比特 = 偶数下标，高 4 比特 = 奇数下标**。

### 2.3 块缩放与重排（直觉）

光有 4 比特码不够：最大值才 6，真实权重可能上千。于是每 16 个连续 FP4 值共享一个 **块缩放** \(s_b\)（E4M3，最大 448），再加一个全局缩放 \(g\)，反量化时：

\[ \hat{x} = \frac{s_b \cdot v}{g},\quad v \in \{\pm0,\pm0.5,\dots,\pm6\} \]

为了让 Blackwell 的张量核（MMA）一次就能按它喜欢的访存模式取到对应的块缩放，缩放张量并不是朴素行优先存的，而是被 **交织（swizzle / interleave）** 成一种叫 **128×4** 的布局：每 512 字节一小块，装 128 行 × 4 列缩放。不同后端需要的布局不同，于是“重排块缩放”成了本讲的硬骨头。

> 名词速查：**E2M1**（4 比特浮点）、**E4M3 / UE4M3**（8 比特浮点 / 无符号版）、**UE8M0**（8 比特纯指数）、**block_size / sf_vec_size**（每多少个元素共享一个缩放）、**global scale**（全张量一个标量）、**W4A16**（权重 4 比特、激活 16 比特）、**compute capability**（见 u5-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `flashinfer/gemm/gemm_bf16_fp4.py` | BF16×FP4 的**公共 API 与派发器**：`prepare_bf16_fp4_weights`、`mm_bf16_fp4`，以及两个后端共享的 `_unswizzle_sf_128x4`。 |
| `flashinfer/gemm/gemm_bf16_fp4_cudnn.py` | **cuDNN 后端**：用 cuDNN frontend 构造 `dequant → matmul → scale` 计算图，并按 M（token 数）做自动调优。 |
| `flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py` | **CuTe-DSL 后端**：把权重重打包成 int32 瓦片、缩放重排成 S0E5M3，再启动 Blackwell 的 CuTe-DSL kernel。 |
| `flashinfer/quantization/fp4_quantization.py` | **量化入口**：`fp4_quantize`、`nvfp4_quantize`、`mxfp4_quantize`，以及 E2M1 查表。 |
| `flashinfer/quantization/nvfp4_quantization_utils.py` | NVFP4 的全局缩放计算与 `FLOAT4_E2M1_MAX / FLOAT8_E4M3_MAX` 常量。 |
| `include/flashinfer/gemm/fp4_gemm_cutlass.h` | **W4A4（双 FP4）CUTLASS runner 接口**：说明“块缩放是交织的”，是本讲块缩放重排概念的另一处权威出处。 |
| `csrc/fp4_gemm_cutlass.cu` | W4A4 的 CUTLASS launcher / TVM-FFI 绑定（`fp4_gemm`、`fp4_gemm_tactic_num`），注释里给出了输入张量形状与全局缩放公式。 |

> ⚠️ 一个容易混淆的点：仓库里有**两个** FP4 GEMM。本讲的 `mm_bf16_fp4` 是 **W4A16**（激活 BF16、权重 FP4），后端是 cuDNN / CuTe-DSL；而 `mm_fp4`（定义在 `gemm_base.py`）是 **W4A4**（A 和 B 都是 FP4），其中一个后端 `cutlass` 才用到 `fp4_gemm_cutlass.h/.cu`。它们共享同一套“E2M1 + 交织块缩放”的数据表示，所以本讲把 `.h/.cu` 当作块缩放重排的参考样本来读，但**实现主体是 `mm_bf16_fp4` 那三个 Python 文件**。

## 4. 核心概念与源码讲解

### 4.1 NVFP4 / MXFP4：两种 4 比特量化格式

#### 4.1.1 概念说明

FP4 GEMM 的输入必须先量化。FlashInfer 的量化入口在 `flashinfer/quantization/fp4_quantization.py`，统一函数是 `fp4_quantize`，再在上面包出 `nvfp4_quantize` 和 `mxfp4_quantize` 两个“配方快捷方式”。两者差异如下：

| 维度 | NVFP4 | MXFP4 |
|------|-------|-------|
| 块大小 `sf_vec_size` | 16 | 32 |
| 块缩放格式 | FP8-E4M3（`sf_use_ue8m0=False`） | UE8M0（`sf_use_ue8m0=True`） |
| 全局缩放 | 有，FP32 标量 | 无（折叠进块缩放） |
| `mm_bf16_fp4` 是否使用 | ✅ 是（cuDNN 硬编码 `use_nvfp4=True`） | ❌ 否 |

#### 4.1.2 核心流程

NVFP4 反量化的数学（块大小 16、E4M3 块缩放、全局缩放 \(g\)）：

\[ \hat{x} = \frac{s_b \cdot v}{g},\qquad v\in\{\pm0,\pm0.5,\pm1,\pm1.5,\pm2,\pm3,\pm4,\pm6\} \]

其中 \(g\) 的取法是把整张张量的最大幅度映射到“块缩放最大值 × FP4 最大值”：

\[ g = \frac{\text{FLOAT8\_E4M3\_MAX} \cdot \text{FLOAT4\_E2M1\_MAX}}{\text{amax}} = \frac{448 \times 6}{\text{amax}} \]

MXFP4 没有独立全局缩放，块缩放用纯指数 \(2^{b-127}\)，块大小翻倍到 32。

#### 4.1.3 源码精读

E2M1 查表与两个关键常量：

- `flashinfer/quantization/fp4_quantization.py` 中 `_E2M1_VALUES` 直接给出 16 个 4 比特码对应的浮点值，注释写明 `bit3=sign, bits2-0=magnitude`：[fp4_quantization.py:104-109](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L104-L109)。
- 两个最大值常量定义在工具文件里，全局缩放公式直接用到它们：[nvfp4_quantization_utils.py:26-27](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/nvfp4_quantization_utils.py#L26-L27)。
- 全局缩放 \(g = 448\times6/\text{amax}\) 的实现：[nvfp4_quantization_utils.py:99-107](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/nvfp4_quantization_utils.py#L99-L107)（`amax==0` 时返回 `finfo.max` 避免除零）。

两条配方的快捷入口：

- `nvfp4_quantize` 默认 `sf_vec_size=16`、`sfLayout=layout_128x4`（即 128×4 交织），CUDA 后端最终调到底层 `fp4_quantize(..., sf_use_ue8m0=False)`：[fp4_quantization.py:1378-1386](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1378-L1386)。
- `mxfp4_quantize` 的 CUDA 路径：先算 \(g=(448\times6)/\text{amax}\)，再调 `fp4_quantize(..., 32, True, True)`（块大小 32、UE8M0、交织）：[fp4_quantization.py:1489-1492](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1489-L1492)。

底层 `fp4_quantize` 强制 `sf_vec_size` 只能是 16 或 32（对应 NVFP4 / MXFP4 两种块大小），输出打包 FP4 形状 `[M, K/2]`：[fp4_quantization.py:878-879](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L878-L879)。

#### 4.1.4 代码实践

**目标**：用纯 PyTorch 的 CPU 路径验证“FP4 码 + 块缩放 + 全局缩放”确实能反量化回原值，建立对格式的直觉。

**步骤**：

1. 构造一个 `[4, 32]` 的 BF16 小张量 `W`。
2. 调 `nvfp4_quantize` 得到 `b_fp4`（形状 `[4,16]`）与 `b_sf`（128×4 交织）。
3. 用库自带的反量化 `e2m1_and_ufp8sf_scale_to_float` 把它还原，与 `W` 比较相对误差。

```python
# 示例代码（需要在 SM90+ 的 GPU 上运行量化内核；CPU 反量化仅支持线性布局）
import torch, flashinfer
from flashinfer.quantization.nvfp4_quantization_utils import make_nvfp4_global_scale

W = torch.randn(4, 32, dtype=torch.bfloat16, device="cuda")
g = make_nvfp4_global_scale(W, per_token_activation=False)   # shape [1] fp32
b_fp4, b_sf = flashinfer.nvfp4_quantize(
    W, g, sfLayout=flashinfer.tllm_enums.SfLayout.layout_128x4
)
# b_fp4: (4, 16) uint8 ; b_sf: 128x4-swizzled E4M3 scales
W_hat = flashinfer.e2m1_and_ufp8sf_scale_to_float(
    b_fp4.cpu(), b_sf.cpu().reshape(-1), g.cpu(), 16, 1, True
)
rel = (W_hat.cuda() - W.float()).abs().mean() / W.float().abs().mean()
print("NVFP4 反量化相对误差:", float(rel))
```

**预期结果**：相对误差在 \(10^{-2}\) 量级（FP4 只有 16 个码，单元素误差较大，但统计平均尚可接受）。具体数值**待本地验证**（取决于随机权重分布）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mxfp4_quantize` 用 UE8M0（纯指数）而不是 E4M3？
**答**：块大小 32 的统计特性更稳，纯指数缩放（\(2^{b-127}\)）实现简单、解码快，且 MXFP4 规范本身就用 power-of-two 缩放；而 NVFP4 块更小（16）、需要更高分辨率，故用 E4M3。

**练习 2**：把 `W` 的 `amax` 翻倍，全局缩放 \(g\) 会怎么变？
**答**：\(g=448\times6/\text{amax}\)，`amax` 翻倍则 \(g\) 减半，以保持 \(g\cdot\text{amax}=448\times6\)（正好填满块缩放与 FP4 的联合动态范围）。

---

### 4.2 block-scale 重排（interleave / unswizzle）

#### 4.2.1 概念说明

这是本讲最核心、也最容易绊倒人的部分。Blackwell 的 MMA 指令取块缩放时有**特定的访存轨迹**：它希望连续线程读到的缩放因子在显存里也连续、且避开 bank conflict。于是 NVFP4 量化默认输出的并不是朴素 `(N, K_sf)` 行优先，而是 **128×4 交织布局（swizzled）**：

- 把缩放张量切成许多 **512 字节的小块**，每块装 **128 个 N 行 × 4 个 K_sf 列**。
- 行方向按 32 为单位、再按 4 分组交错排列（具体见下方偏移公式）。
- N 方向填充到 128 的倍数，K_sf 方向填充到 4 的倍数。

问题来了：`mm_bf16_fp4` 的两个后端**都不直接吃 128×4 交织布局**——cuDNN 要线性 `(N, K_sf)` 的 E4M3，CuTe-DSL 要转置后的 `(K_sf, N)` 且改成 S0E5M3。所以“重排块缩放”分两步：先用公共函数 `_unswizzle_sf_128x4` 反交织回线性，再由各后端按需二次加工。

#### 4.2.2 核心流程

`_unswizzle_sf_128x4` 的反交织，关键是把**逻辑坐标 `(n, k_sf)` 映射到 512 字节小块里的字节偏移**。docstring 给出的映射为：

\[
\begin{aligned}
\text{offset} ={}& \big((n//128)\cdot \text{sf\_pad\_blocks} + (k_{sf}//4)\big)\times 512 \\
&+ (n\bmod 32)\times 16 \\
&+ \big((n\bmod 128)//32\big)\times 4 \\
&+ (k_{sf}\bmod 4)
\end{aligned}
\]

其中 `sf_pad_blocks = ceil(k_sf / 4)`。用 PyTorch 的 `meshgrid` 一次性算出所有 `(n, k_sf)` 的偏移，再 `gather` 即可还原线性 `(N, K_sf)` 字节张量。

各后端拿到线性缩放后的二次加工：

| 后端 | 权重 | 块缩放 |
|------|------|--------|
| cuDNN | 原样 `(N, K/2)` uint8 | 线性 `(N, K_sf)` E4M3（view 成 `float8_e4m3fn`） |
| CuTe-DSL | 重打包成 `(K//16, N*2)` int32 瓦片 | `(K_sf, N)` 且 **E4M3 → S0E5M3** 重排 |

#### 4.2.3 源码精读

- 128×4 交织布局所需字节数的计算：N 填到 128、K_sf 填到 4，再乘 512（每块 128×4=512 字节）：[fp4_quantization.py:69-72](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L69-L72)。`prepare_bf16_fp4_weights` 正是用它校验传入的 `b_descale` 够不够大：[gemm_bf16_fp4.py:175-182](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L175-L182)。

- 公共反交织函数 `_unswizzle_sf_128x4` 的偏移公式实现（纯 PyTorch `meshgrid + gather`，无自定义 kernel）：[gemm_bf16_fp4.py:294-303](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L294-L303)。

- **W4A4 侧的权威注释**：`fp4_gemm_cutlass.h` 的类注释直接写明 “Block scaling factor are interleaved.”，并指出激活/输出按行主序、权重按列主序：[fp4_gemm_cutlass.h:30-40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/gemm/fp4_gemm_cutlass.h#L30-L40)。这是“块缩放必须交织”这一约定最直接的出处。

- cuDNN 后端的二次加工 `_prepare_cudnn`：反交织后直接 `.view(torch.float8_e4m3fn)`，权重原样返回：[gemm_bf16_fp4_cudnn.py:504-509](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cudnn.py#L504-L509)。

- CuTe-DSL 后端的二次加工 `_prepare_cute_dsl`：反交织 → 转置成 `(K_sf, N)` → E4M3 改写成 S0E5M3，权重另调 `_cute_dsl_pack_fp4_weight` 重打包：[gemm_bf16_fp4_cute_dsl.py:270-280](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L270-L280)。

- S0E5M3 重排的实现：把 E4M3 字节经 fp16 中转，取出高位字节作为 S0E5M3，**仅是为了让 kernel 内解码更快**：[gemm_bf16_fp4_cute_dsl.py:180-186](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L180-L186)。

- 权重重打包 `_cute_dsl_pack_fp4_weight`：按 16×64 瓦片、模拟 MMA 的 `tc_gen5_mma` 线程取值轨迹，把每 4 个字节组成一个 int32（小端）：[gemm_bf16_fp4_cute_dsl.py:194-251](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L194-L251)。

#### 4.2.4 代码实践

**目标**：以“源码阅读型实践”确认两个后端对同一份 128×4 交织缩放做了不同的二次加工。

**步骤**：

1. 读 `_unswizzle_sf_128x4` 的 docstring 与偏移公式，手动验证：对 `n=0, k_sf=0`，偏移 = `0*...*512 + 0 + 0 + 0 = 0`；对 `n=1, k_sf=0`，偏移 = `1*16 = 16`（即第 1 行不在第 1 字节，而在第 16 字节——这正是“交织”的体现）。
2. 对比 `_prepare_cudnn` 与 `_prepare_cute_dsl` 的返回：前者缩放是 `(N, K_sf)`、后者是 `(K_sf, N)` 且 dtype 是 `uint8`（S0E5M3）。
3. 在两张后端上跑同一个 GEMM（见 4.3.4），输出应近似一致（数值差异来自不同的反量化/累加实现）。

**预期结果**：两个后端结果的最大相对差异在 \(10^{-3}\sim10^{-2}\) 量级。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cuDNN 后端“不支持” 128×4 交织布局，非得先反交织？
**答**：cuDNN 的 `block_scale_dequantize` 节点期望块缩放是它自己定义的、`reordering_type=tensor_reordering.NONE` 的线性布局（见 `_bf16_fp4_b_descale_layout` 返回 `NONE`），它不认识 NVIDIA 自家的 128×4 交织，所以必须先还原成线性。

**练习 2**：`_unswizzle_sf_128x4` 为什么要 `sf_pad_blocks = (k_sf + 3) // 4`？
**答**：每个 128 行的 N 块里，K_sf 方向按 4 列对齐填充；`sf_pad_blocks` 是该 N 块内填充后的 K_sf 块数，用于把 `(n//128, k_sf//4)` 正确换算成第几个 512 字节小块。

---

### 4.3 后端选择：cuDNN vs CuTe-DSL

#### 4.3.1 概念说明

`mm_bf16_fp4` 不像注意力的 `backend="auto"` 那样会自动二选一——`backend` 是**必填关键字参数**（`Literal["cudnn", "cute-dsl"]`）。两个后端都用 `@backend_requirement` + `@supported_compute_capability` 做声明式校验（这套机制见 [u5-l1](u5-l1-gemm-overview.md)），且都**仅支持 Blackwell（SM100/103/110/120/121）**。选择依据主要是：

- **是否装了 cuDNN**（`nvidia-cudnn-cu12` + `nvidia-cudnn-frontend`，且 backend 版本 ≥ 92301）→ 用 cuDNN。
- **是否装了 CuTe-DSL**（`nvidia-cutlass-dsl`）→ 用 CuTe-DSL。
- **输出 dtype 是否与激活一致**：CuTe-DSL 要求 `out_dtype == a.dtype`，否则报 `NotImplementedError` 并提示改用 cuDNN。

两个后端的本质差异：

| 维度 | cuDNN 后端 | CuTe-DSL 后端 |
|------|-----------|---------------|
| 实现方式 | cuDNN frontend 计算图（`dequant→matmul→scale`） | Blackwell 自研 CuTe-DSL kernel |
| 权重预处理 | 几乎不动（uint8） | 重打包成 int32 瓦片 |
| 缩放格式 | E4M3 线性 | S0E5M3 转置 |
| 调优对象 | cuDNN execution plan（含 override-shape） | CTA tile / atom_layout / swizzle |
| `out_dtype` 灵活性 | 可与 `a.dtype` 不同 | 必须与 `a.dtype` 相同 |

#### 4.3.2 核心流程

两条调用链（公共部分相同，仅 `_compute_*` 分叉）：

```
mm_bf16_fp4(a, b_p, sf_p, alpha, backend=...)
  ├─ backend="cudnn"   → _compute_cudnn  → 构图/执行 cuDNN 计算图（AutoTuner 按 M 选 plan）
  └─ backend="cute-dsl" → _compute_cute_dsl → 选 tile → 启动 compiled CuTe-DSL kernel
```

两个后端都接 `AutoTuner.choose_one`，按 **M（token 数）** 分桶调优（DynamicTensorSpec），这正是推理场景下 M 频繁变化的需求。

#### 4.3.3 源码精读

**公共校验与架构门控**：

- 问题尺寸公共校验：`a` 必须 2-D 且 BF16，`block_size` 必须 16，`out_dtype` 只能 bf16/fp16：[gemm_bf16_fp4.py:31-57](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L31-L57)。
- 两个后端都挂 `@supported_compute_capability([100, 103, 110, 120, 121])`（仅 Blackwell）：cuDNN 侧 [gemm_bf16_fp4.py:60-61](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L60-L61)，CuTe-DSL 侧 [gemm_bf16_fp4.py:94](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L94)。
- **dtype 门控**是区分两后端的最直接信号：cuDNN 期望 `b.dtype == uint8`，CuTe-DSL 期望 `b.dtype == int32`（即来自对应的 `prepare_bf16_fp4_weights(backend=...)`）：[gemm_bf16_fp4.py:78-82](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L78-L82) 与 [gemm_bf16_fp4.py:107-111](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L107-L111)。
- cuDNN 额外的版本门控：`_CUDNN_BF16_FP4_MIN_BACKEND_VERSION = 92301`，低于则抛 `ValueError`（注意是 ValueError，这样 `auto` 启发式会跳过该后端而不是终止）：[gemm_bf16_fp4.py:85-90](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L85-L90)。

**派发器**：`mm_bf16_fp4` 按 `backend` 字符串 if/elif 到 `_compute_cudnn` / `_compute_cute_dsl`：[gemm_bf16_fp4.py:256-266](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L256-L266)。

**cuDNN 计算图**（`_build_bf16_fp4_graph_common`）：三段——`block_scale_dequantize(B)` → `matmul(A, dequant_B)` → 可选 `mul(·, global_scale)`；`scale_type` 由 `use_nvfp4` 决定（True → `FP8_E4M3`，本路径恒 True），权重张量类型为 `cudnn.data_type.FP4_E2M1`：[gemm_bf16_fp4_cudnn.py:65-103](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cudnn.py#L65-L103)，缩放类型选择见 [gemm_bf16_fp4_cudnn.py:125](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cudnn.py#L125)，FP4 数据类型见 [gemm_bf16_fp4_cudnn.py:142](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cudnn.py#L142)。

**CuTe-DSL tile 选择**（`_select_bf16_fp4_tile_shape`）：按 M 选 `tile_M`（M≤16 用 16 + atom `(1,2,1)`；M≤32 用 32；否则 64），按 K 选 `tile_K`（K 整除 128 用 128 否则 64）：[gemm_bf16_fp4_cute_dsl.py:42-77](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L42-L77)。CuTe-DSL 的 `out_dtype == a.dtype` 硬约束见 [gemm_bf16_fp4_cute_dsl.py:449-454](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L449-L454)。

**对照：W4A4 的 CUTLASS runner**（`fp4_gemm`，本讲仅作参考）：接口注释给出双 FP4 输入形状与“块缩放交织”约定：[fp4_gemm_cutlass.cu:90-96](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/csrc/fp4_gemm_cutlass.cu#L90-L96)；`FP4GemmType` 枚举只声明了 `W4A4_NVFP4_NVFP4` 一种：[fp4_gemm_cutlass.h:59-61](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/gemm/fp4_gemm_cutlass.h#L59-L61)。

#### 4.3.4 代码实践

**目标**：完成实践任务——对一个 BF16 权重做 NVFP4 量化并重排块缩放，再用 `mm_bf16_fp4` 做矩阵乘，与 BF16 参考结果比较相对误差。这是本讲的**主实践**。

**关键点**：cuDNN 的 `block_scale_dequantize` 产出 \(s_b\cdot v\)（不含除以 \(g\)），所以 GEMM 结果 ≈ \(a@(W\cdot g)\)；要还原 \(a@W\)，需令 `alpha = 1/g`。两个后端的 `prepare_bf16_fp4_weights` 都把 `alpha` 原样返回并在 compute 时乘上。

**步骤**：

1. 造激活 `a`（M,K）BF16 与权重 `W`（N,K）BF16。
2. 算全局缩放 \(g=(448\times6)/\text{amax}\)，调 `nvfp4_quantize` 得 `b`(N,K/2) 与 `b_sf`(128×4)。
3. `prepare_bf16_fp4_weights(b, b_sf, alpha=1/g, backend=...)` 得到后端专用三元组。
4. `mm_bf16_fp4(a, b_p, sf_p, alpha_p, backend=...)` 得 `(M,N)`。
5. 与 BF16 参考 `a @ W.t()` 比相对误差；两个后端互相对比。

```python
# 示例代码（需要 Blackwell GPU：SM100/103/110/120/121）
import torch, flashinfer
from flashinfer.quantization.nvfp4_quantization_utils import FLOAT8_E4M3_MAX, FLOAT4_E2M1_MAX

M, N, K = 128, 512, 1024
a = torch.randn(M, K, dtype=torch.bfloat16, device="cuda") * 0.1
W = torch.randn(N, K, dtype=torch.bfloat16, device="cuda") * 0.1

# 1) NVFP4 量化（128x4 交织块缩放）
amax = W.float().abs().max()
g = torch.tensor([FLOAT8_E4M3_MAX * FLOAT4_E2M1_MAX / amax], dtype=torch.float32, device="cuda")
b_fp4, b_sf = flashinfer.nvfp4_quantize(
    W, g, sfLayout=flashinfer.tllm_enums.SfLayout.layout_128x4
)

ref = (a.float() @ W.float().t())   # BF16 参考

for backend in ("cudnn", "cute-dsl"):
    b_p, sf_p, alpha_p = flashinfer.prepare_bf16_fp4_weights(
        b_fp4, b_sf, alpha=1.0 / g, backend=backend
    )
    out = flashinfer.mm_bf16_fp4(a, b_p, sf_p, alpha_p, backend=backend)
    rel = (out.float() - ref).abs().mean() / ref.abs().mean()
    print(f"[{backend}] 相对误差 = {float(rel):.4f}")
```

**预期结果**：两个后端的相对误差都在 \(10^{-2}\) 量级，且彼此接近。`ref` 用 BF16 参考故误差主要来自 FP4 量化本身（而非 GEMM）。具体数值**待本地验证**。

**观察现象**：

- 若把 `backend="cute-dsl"` 但 `out_dtype` 设成与 `a.dtype` 不同的值，会触发 `NotImplementedError`，提示改用 cuDNN（对应 [gemm_bf16_fp4_cute_dsl.py:449-454](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4_cute_dsl.py#L449-L454)）。
- 若把两后端的 `b_p` 张量混用（把 cuDNN 的 uint8 权重喂给 cute-dsl），会因 dtype 校验失败而报错，印证 [gemm_bf16_fp4.py:78-82](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L78-L82) 与 [gemm_bf16_fp4.py:107-111](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_bf16_fp4.py#L107-L111) 的 dtype 门控。

#### 4.3.5 小练习与答案

**练习 1**：`mm_bf16_fp4` 为什么不让 `backend="auto"` 自动选？
**答**：两个后端的**预处理产物不通用**（uint8 vs int32、E4M3 vs S0E5M3），权重在 `prepare_bf16_fp4_weights` 阶段就必须按后端定型，且通常在模型加载时一次性预处理好；运行期再“自动选”会破坏“预处理一次、推理多次”的约定，所以要求用户显式指定。

**练习 2**：为什么 CuTe-DSL 要求 `out_dtype == a.dtype`？
**答**：CuTe-DSL kernel 的 epilogue 直接按激活 dtype 写出，未实现 dtype 转换分支；需要不同输出 dtype 时走 cuDNN（它的计算图在 FLOAT 上累加，可以灵活 cast 输出）。

## 5. 综合实践

把本讲三个最小模块串成一个端到端小任务：**“BF16 权重 → NVFP4 量化 → 双后端重排 → GEMM → 误差分析”**。

1. 选一个真实形状（例如 MLP 的 down projection：`M=2048, N=4096, K=16384`），造 BF16 权重 `W` 与激活 `a`。
2. 用 `nvfp4_quantize(sfLayout=layout_128x4)` 量化，**手动**用 `_compute_swizzled_layout_sf_size` 算出 `b_sf` 应有的字节数，与实际 `b_sf.numel()` 核对，理解 128×4 填充规则。
3. 分别用 cuDNN 与 CuTe-DSL 走完 `prepare_bf16_fp4_weights → mm_bf16_fp4`，打印两个后端**各自的预处理后权重 dtype / 形状**（uint8 vs int32、是否转置），印证 4.2 的“二次加工”差异。
4. 用 `bench_gpu_time`（见 [u10-l3](u10-l3-benchmarking.md)）测两个后端的 kernel 时间，结合 `_select_bf16_fp4_tile_shape` 解释：为什么 M 很小（如 M=1 decode）时 CuTe-DSL 倾向 `tile_M=16 + atom(1,2,1)`。
5. 报告三组相对误差：FP4-vs-BF16（量化误差）、两个后端互比（实现差异）。

> 这个任务把“格式（NVFP4）→ 重排（unswizzle + 二次加工）→ 后端（cuDNN/CuTe-DSL）→ 精度/性能”全链路打通。注意 M、N、K 都要满足对齐约束（K 是 16/32 倍数，cuDNN/CuTe-DSL 还有各自的 N/K 对齐要求）。

## 6. 本讲小结

- FP4（E2M1）是 4 比特浮点，正半轴只有 8 个值（最大 6.0），两个值打包进一个字节；**NVFP4**（块 16 + E4M3 缩放 + 全局缩放）与 **MXFP4**（块 32 + UE8M0）是两种主流配方，`mm_bf16_fp4` 走的是 NVFP4。
- 块缩放默认以 **128×4 交织布局**存放，是为了匹配 Blackwell MMA 的访存轨迹；两个后端都不直接吃它，需先用公共 `_unswizzle_sf_128x4` 反交织回线性 `(N, K_sf)`。
- cuDNN 后端几乎不动权重（uint8）、缩放保持 E4M3 线性；CuTe-DSL 后端把权重重打包成 int32 瓦片、缩放转置并改成 S0E5M3——**dtype（uint8 vs int32）是区分两后端的最直接信号**。
- `mm_bf16_fp4` 是 **W4A16** 路径（激活 BF16、权重 FP4），仅支持 Blackwell（SM100/103/110/120/121）；`backend` 是必填项，cuDNN 还要求 backend 版本 ≥ 92301。
- 全局缩放 \(g=448\times6/\text{amax}\)；因 cuDNN 反量化不含除以 \(g\)，调用时通常要传 `alpha=1/g` 才能与 BF16 参考对齐。
- 仓库里还有一个 **W4A4** 的 `mm_fp4`（双 FP4），其 `cutlass` 后端正是 `fp4_gemm_cutlass.h/.cu`，与 `mm_bf16_fp4` 共享“E2M1 + 交织块缩放”表示但实现不同，别混淆。

## 7. 下一步学习建议

- **量化算子全貌**：继续读 [u5-l5 量化算子（FP8/FP4 quantize）](u5-l5-quantization-ops.md)，把 NVFP4/MXFP4 量化、反量化、KV cache 量化（`nvfp4_quantize_paged_kv_cache`）与 per-token 激活缩放串起来。
- **Grouped FP4 GEMM**：MoE 场景下的多专家 FP4 矩阵乘见 `group_gemm_nvfp4_nt_groupwise` / `group_gemm_mxfp4_nt_groupwise`（见 [u5-l4 Grouped GEMM](u5-l4-grouped-gemm.md) 与 [u6 MoE 单元](u6-l1-moe-basics.md)）。
- **自动调优机制**：两个后端都接 `AutoTuner.choose_one` 按 M 分桶调优，深入读 `flashinfer/autotuner.py`（见 [u10-l2 自动调优](u10-l2-autotuning.md)）。
- **W4A4 对照**：想理解双 FP4 路径，读 `include/flashinfer/gemm/fp4_gemm_cutlass_template.h`（本讲引用的 `fp4_gemm_cutlass.h/.cu` 的模板实现）与 `gemm_base.py` 中 `mm_fp4` 的多后端派发。
