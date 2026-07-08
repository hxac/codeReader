# 量化算子（FP8/FP4 quantize）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 FlashInfer **量化算子**与 [u5-l3 FP4 GEMM](u5-l3-fp4-gemm.md) 的分工：本讲聚焦 `flashinfer/quantization/` 包里“把高精度张量压成低比特 + 缩放因子、并能反量化回来”这一类算子本身，而不是它们喂给谁的 GEMM。
- 区分三条量化主线：**MXFP8**（8 比特，块 32 + UE8M0）、**NVFP4**（4 比特，块 16 + E4M3 + 全局缩放）、**MXFP4**（4 比特，块 32 + UE8M0），并知道各自调哪个 API。
- 掌握**缩放粒度**的三种取法——**per-tensor**（全张量一个全局缩放）、**batched**（每个 batch 独立量化）、**grouped / groupwise**（按专家/LoRA 分组、带 mask）——以及它们对应的入口函数。
- 理解 **反量化（dequantize）往返**：`e2m1_and_ufp8sf_scale_to_float`、`mxfp4_dequantize`、`mxfp8_dequantize_host` 如何把“打包码 + 块缩放 + 全局缩放”还原成 float，并能据此分析量化误差。
- 用 `nvfp4_quantize_paged_kv_cache` / `nvfp4_kv_quantize` / `nvfp4_kv_dequantize` 把一个**分页 KV cache** 量化成 NVFP4 并反量化，理解它为何把 `head_dim` 砍半、为何对 V 的缩放做 4-token 交织。
- 完成综合实践：对同一个权重张量分别做 **mxfp4 与 nvfp4** 量化，统计量化前后的显存占用，并比较两种格式反量化后的误差分布。

本讲依赖 [u5-l2 FP8 GEMM](u5-l2-fp8-gemm.md)（FP8 缩放与 groupwise 概念）与 [u5-l3 FP4 GEMM](u5-l3-fp4-gemm.md)（E2M1 格式、NVFP4/MXFP4 配方、128×4 交织）。本讲不再重复这些细节，只在需要时链接回去。

## 2. 前置知识

[u5-l3] 已经把 FP4 的格式讲透了，这里只补三段本讲要用到的直觉。

### 2.1 量化算子的输入输出契约

无论哪种格式，FlashInfer 的量化算子都遵循同一个契约：输入一个高精度张量 \(x\)（`[M, K]`，fp16/bf16），输出一对 **(打包低比特数据 `x_q`, 块缩放 `sf`)**。反量化算子则把它们（外加可选的全局缩放 \(g\)）映射回 float。数学上：

\[ \text{量化}:\quad x \;\mapsto\; (x_q,\; s_f),\qquad \text{反量化}:\quad \hat{x} = v(x_q)\cdot s_f \cdot g_{\text{dequant}} \]

其中 \(v(x_q)\) 是把低比特码查表还原成的基础浮点值（FP4 的 E2M1 查表、FP8 直接是 `float8_e4m3fn`），\(s_f\) 是按块（block）共享的缩放，\(g_{\text{dequant}}\) 是反量化时传入的全局缩放。**量化的精度就取决于 \((x_q, s_f)\) 能多逼近 \(x\)**——这正是本讲误差实践要测的东西。

### 2.2 三种格式速记

| 格式 | 比特 | 块大小 | 块缩放格式 | 全局缩放 | 入口 |
|------|------|--------|-----------|---------|------|
| MXFP8 | 8 | 32 | UE8M0（\(2^{b-127}\)） | 无 | `mxfp8_quantize` |
| NVFP4 | 4 | 16 | E4M3 | 有（\(g=448\cdot6/\text{amax}\)） | `nvfp4_quantize` |
| MXFP4 | 4 | 32 | UE8M0 | 折叠进块缩放 | `mxfp4_quantize` |

E4M3、UE8M0、E2M1 等术语见 [u5-l3 前置知识](u5-l3-fp4-gemm.md#2-前置知识)。一句话区分：**比特数决定省多少显存，块大小与缩放格式决定精度与动态范围**。

### 2.3 为什么要专门做“反量化”算子

低比特 GEMM（如 [u5-l3](u5-l3-fp4-gemm.md) 的 `mm_bf16_fp4`）会直接吃打包数据 + 缩放、在 kernel 内部边反量化边乘加，性能最高。但很多场景需要把量化权重**还原成 float** 看一眼：精度调试、对参考实现、把量化 checkpoint 加载进非量化模型、或在没装低比特 GEMM 后端的卡上做回退计算。于是 FlashInfer 把“反量化”也做成了一等公民算子，这就是 `e2m1_and_ufp8sf_scale_to_float` 与 `*_dequantize_host` 系列的存在意义。

> 名词速查：**sf_vec_size / block_size**（每多少个元素共享一个块缩放）、**global scale**（全张量标量）、**swizzled / 128×4 布局**（见 [u5-l3 §4.2](u5-l3-fp4-gemm.md)）、**UE8M0**（\(2^{b-127}\) 纯指数）、**PDL**（Programmatic Dependent Launch，与上游 kernel 重叠）、**`SfLayout`**（缩放因子布局枚举）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `flashinfer/quantization/fp8_quantization.py` | **MXFP8 量化**入口：`mxfp8_quantize`、`mxfp8_grouped_quantize`、`mxfp8_dequantize_host`，以及 SM100 JIT 模块工厂。 |
| `flashinfer/quantization/fp4_quantization.py` | **FP4 量化**主力（~2200 行）：`fp4_quantize`、`nvfp4_quantize`、`mxfp4_quantize`、反量化 `e2m1_and_ufp8sf_scale_to_float`/`mxfp4_dequantize`、批/分组 `nvfp4_batched_quantize`/`scaled_fp4_grouped_quantize`、KV cache 的 `nvfp4_quantize_paged_kv_cache`/`nvfp4_kv_quantize`/`nvfp4_kv_dequantize`，以及 E2M1 查表与 `SfLayout` 派发。 |
| `flashinfer/quantization/nvfp4_quantization_utils.py` | NVFP4 全局缩放计算与常量 `FLOAT4_E2M1_MAX=6.0`、`FLOAT8_E4M3_MAX=448.0`。 |
| `flashinfer/quantization/__init__.py` | 量化包的统一再导出，列出全部公开符号。 |
| `flashinfer/tllm_enums.py` | `SfLayout` 枚举（`layout_128x4`/`layout_8x4`/`layout_linear`），量化函数按它选缩放布局。 |
| `tests/utils/test_fp4_quantize.py` | FP4 量化正确性与往返测试，含 mxfp4/nvfp4 两种配方的可运行参考。 |
| `tests/utils/test_fp4_kv_quantization.py` | KV cache NVFP4 反量化测试，含纯 PyTorch 参考实现。 |

## 4. 核心概念与源码讲解

### 4.1 量化与反量化的统一框架（FP8/FP4）

#### 4.1.1 概念说明

FlashInfer 的量化算子是**分层**的。最底层是单一 kernel 入口（`fp4_quantize_sm100`、`mxfp8_quantize_sm100`），它接受一长串布尔/枚举开关，能同时表达 NVFP4 和 MXFP4；往上各包一层“配方快捷方式”——`nvfp4_quantize` 把开关设成“块 16 + E4M3 + 全局缩放”，`mxfp4_quantize` 设成“块 32 + UE8M0”，`mxfp8_quantize` 则走另一条 SM100 FP8 kernel。这种“一个底层 kernel + 多个上层配方”的设计和 [u2-l3 gen_*_module 代码生成](u2-l3-codegen-pattern.md) 的复用思路一脉相承。

反量化同理：底层 `e2m1_and_ufp8sf_scale_to_float_sm100` 用 `ufp8_type` 参数区分 E4M3（=1，给 NVFP4）与 UE8M0（=0，给 MXFP4），上层 `mxfp4_dequantize` 只是固定传 `(32, 0)` 的薄壳。

#### 4.1.2 核心流程

一条完整的“量化 → 反量化往返”流程：

1. **算全局缩放**（仅 NVFP4 需要）：\(g_{\text{encode}} = \text{FLOAT8\_E4M3\_MAX}\cdot\text{FLOAT4\_E2M1\_MAX}/\text{amax} = 448\cdot6/\text{amax}\)。
2. **量化**：kernel 把 \(x\) 先乘 \(g_{\text{encode}}\)，再按块求出块缩放 \(s_f\) 并把残差量化成低比特码，输出 `(x_q, sf)`。
3. **反量化**：\(\hat{x} = v(x_q)\cdot s_f \cdot g_{\text{dequant}}\)，其中 NVFP4 传 \(g_{\text{dequant}} = 1/g_{\text{encode}} = \text{amax}/(448\cdot6)\)，MXFP4 因全局缩放已折叠进 UE8M0 块缩放，传 \(g_{\text{dequant}}=1.0\)。

MXFP8 没有“打包”步骤（FP8 本就是 torch 原生 `float8_e4m3fn`），它的量化输出直接是 `[M, K]` 的 FP8 张量 + 每 32 元素一个 UE8M0 字节缩放，反量化 \(\hat{x} = \text{fp8\_val}\cdot 2^{s_f-127}\)。

#### 4.1.3 源码精读

**E2M1 查表与两个最大值常量**——FP4 反量化的根基：

- `_E2M1_VALUES` 直接列出 16 个 4 比特码对应的浮点值，注释写明 `bit3=sign, bits2-0=magnitude`：[fp4_quantization.py:104-109](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L104-L109)。
- 两个常量定义在工具文件，全局缩放公式直接用：[nvfp4_quantization_utils.py:26-27](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/nvfp4_quantization_utils.py#L26-L27)。
- 全局缩放 \(g=448\cdot6/\text{amax}\) 的实现（`amax==0` 返回 `finfo.max` 避免除零）：[nvfp4_quantization_utils.py:99-107](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/nvfp4_quantization_utils.py#L99-L107)。

**FP4 量化的底层入口 `fp4_quantize`**——一个 kernel 表达两种配方：

- 它强制 `sf_vec_size` 只能是 16 或 32（对应 NVFP4/MXFP4），输出打包形状 `[M, K/2]`：[fp4_quantization.py:878-879](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L878-L879)。
- 列主序输入会先转置（`stride(-2)==1` 判定），保证 kernel 总见到行主序：[fp4_quantization.py:900-903](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L900-L903)。
- 按 SM 版本选 JIT 模块并调底层 `fp4_quantize_sm100`，再按布局 reshape 缩放张量（交织布局含填充）：[fp4_quantization.py:909-928](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L909-L928)。
- 底层注册算子 `fp4_quantize_sm100` 的真正启动，把 7 个开关透传给 `module.fp4_quantize`：[fp4_quantization.py:283-315](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L283-L315)。

**两条 FP4 配方快捷方式**：

- `nvfp4_quantize` 默认 `sf_vec_size=16`、`sfLayout=layout_128x4`，CUDA 后端最终调 `fp4_quantize(..., sf_use_ue8m0=False)`：[fp4_quantization.py:1378-1386](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1378-L1386)。
- `mxfp4_quantize` 的 CUDA 路径先算 \(g=(448\cdot6)/\text{amax}\)，再调 `fp4_quantize(..., 32, True, True)`（块 32、UE8M0、交织）：[fp4_quantization.py:1489-1492](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1489-L1492)。

**MXFP8 量化入口 `mxfp8_quantize`**——走独立的 SM100 kernel，`sf_vec_size` 固定 32、块缩放 UE8M0：

- 公共 API 的 `backend` 在 `"cuda"`（稳定 JIT）与 `"cute-dsl"`（实验）间二选一：[fp8_quantization.py:171-179](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L171-L179)。
- CUDA 路径委托给 `get_mxfp8_quantization_sm100_module().mxfp8_quantize_sm100`：[fp8_quantization.py:253-263](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L253-L263)。
- 注册算子 `mxfp8_quantize_sm100` 的 GPU 分支：输出 `float8_e4m3fn` 的 `[M, K]` 与 uint8 缩放，缩放字节数随 `SfLayout` 不同（linear / 128x4 / 8x4）：[fp8_quantization.py:84-114](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L84-L114)。

**反量化往返 `e2m1_and_ufp8sf_scale_to_float`**（FP4 通用反量化）：

- 公共入口按 SM 选模块；SM<90 时退回纯 PyTorch CPU 实现：[fp4_quantization.py:1145-1169](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1145-L1169)。
- CPU 参考实现把反量化拆成四步：拆 nibble → E2M1 查表 → 解码缩放（E4M3 或 \(2^{b-127}\)）→ 逐块广播相乘，正是公式 \(\hat{x}=v\cdot s_f\cdot g\) 的直译，**只支持线性布局**：[fp4_quantization.py:119-163](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L119-L163)。
- `mxfp4_dequantize` 是固定传 `(sf_vec_size=32, ufp8_type=0, gs=1.0)` 的薄壳，与 `mxfp4_quantize` 配对：[fp4_quantization.py:1497-1524](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1497-L1524)。
- `mxfp8_dequantize_host` 反量化 MXFP8，按 `group_size`（默认 32）逐块还原：[fp8_quantization.py:449-491](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L449-L491)。

#### 4.1.4 代码实践

**目标**：用 `nvfp4_quantize` + `e2m1_and_ufp8sf_scale_to_float` 跑一次 NVFP4 往返，验证“量化 + 反量化”能把 BF16 权重还原到可接受误差，建立对本讲后续误差实践的直觉。

**步骤**：

1. 造一个 `[128, 1024]` 的 BF16 权重 `W`。
2. 算全局缩放 \(g=448\cdot6/\text{amax}\)，调 `nvfp4_quantize` 得 `q`（`[128,512]`）与 `sf`（128×4 交织）。
3. 用 `e2m1_and_ufp8sf_scale_to_float` 反量化（传 \(1/g\) 作 dequant 全局缩放），与 `W` 比较误差。

```python
# 示例代码（量化需 SM100/110/12x 的 Blackwell；反量化需 SM90+）
import torch, flashinfer
from flashinfer.quantization.nvfp4_quantization_utils import FLOAT8_E4M3_MAX, FLOAT4_E2M1_MAX

W = torch.randn(128, 1024, dtype=torch.bfloat16, device="cuda")
amax = W.float().abs().max()
g = torch.tensor([FLOAT8_E4M3_MAX * FLOAT4_E2M1_MAX / amax], dtype=torch.float32, device="cuda")

q, sf = flashinfer.nvfp4_quantize(W, g, sfLayout=flashinfer.tllm_enums.SfLayout.layout_128x4)
W_hat = flashinfer.e2m1_and_ufp8sf_scale_to_float(
    q.cpu(), sf.cpu().reshape(-1), (1.0 / g).cpu(), sf_vec_size=16,
    ufp8_type=1, is_sf_swizzled_layout=True,
).to("cuda")
print("反量化形状:", W_hat.shape, "dtype:", W_hat.dtype)
print("平均相对误差:", float((W_hat - W.float()).abs().mean() / W.float().abs().mean()))
```

**预期结果**：`W_hat` 形状 `[128, 1024]`、dtype `float32`；平均相对误差在 \(10^{-2}\) 量级（FP4 仅 16 个码，单点误差较大但统计平均尚可）。这与仓库测试 `test_e2m1_dequantization` 用 `rtol=0.3, atol=0.5` 的容限一致。具体数值**待本地验证**（取决于随机权重分布与硬件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `e2m1_and_ufp8sf_scale_to_float` 的 CPU 退路“只支持线性布局、不支持 128×4 交织”？
**答**：CPU 退路用纯 PyTorch 按朴素 `(M, K/sf_vec_size)` 行优先解释缩放，没有实现交织坐标映射；交织布局（见 [u5-l3 §4.2](u5-l3-fp4-gemm.md)）需要专门的反交织索引。要用 CPU 退路，就得在量化时传 `is_sf_swizzled_layout=False`（线性），或在 GPU（SM90+）上走有交织支持的 kernel。

**练习 2**：`mxfp4_dequantize` 为什么把全局缩放飞传成 `1.0`，而 NVFP4 反量化要传 \(1/g\)？
**答**：MXFP4 用 UE8M0（纯指数 \(2^{b-127}\)）块缩放，动态范围极大（约 \(2^{-127}\sim2^{127}\)），量化时已把全局缩放折叠进块缩放字节，反量化时块缩放本身就够还原，故 \(g_{\text{dequant}}=1\)；NVFP4 的 E4M3 块缩放范围窄（≤448），必须额外靠 \(g_{\text{dequant}}=1/g_{\text{encode}}\) 找回全局动态范围。

---

### 4.2 缩放粒度：per-tensor / batched / grouped

#### 4.2.1 概念说明

同一个量化格式，按“缩放因子覆盖多大范围”又分三种粒度，对应不同业务场景：

- **per-tensor**：整张 `[M, K]` 共享一个全局缩放 \(g\)，块缩放仍按 `sf_vec_size` 切。这是 `nvfp4_quantize` / `mxfp4_quantize` / `mxfp8_quantize` 的默认行为，适合“单个权重矩阵一次性量化”。
- **batched**：`[B, M, K]` 的每一片 batch 独立量化、各自一份缩放，入口是 `nvfp4_batched_quantize`。适合“同一批里各样本动态范围差异大、不能用一个全局缩放概括”的场景。
- **grouped / groupwise**：按专家（MoE）或 LoRA 分组，**带一个 per-group 行 mask**，只量化每组前 `mask[i]` 行，并把物理布局重排成分组 GEMM 要的形状。入口是 `mxfp8_grouped_quantize`、`scaled_fp4_grouped_quantize`。这正好对接 [u5-l4 Grouped GEMM](u5-l4-grouped-gemm.md) 与 [u6 MoE](u6-l1-moe-basics.md)。

groupwise 的核心难点不是数学（缩放公式不变），而是**布局重排**：分组 GEMM 要求权重按专家维度排布，于是量化输出要 permute 成 `[M, padded_K, B]`（B 在最后）、缩放要重排成 6D 交织瓦片。这与 [u5-l2 §4.2 groupwise 缩放](u5-l2-fp8-gemm.md) 讲的 GEMM 侧 groupwise 是同一套思想，只不过这里发生在量化阶段。

#### 4.2.2 核心流程

三种粒度的输出差异：

```
per-tensor : x_q [M, K/2],        sf (128x4 交织, 一份)
batched    : x_q [B, M, K/2],     sf [B, ceil(M/128)*128 * ceil(K/sf/4)*4]   ← 每片一份
grouped    : x_q [M, K/2, B],     sf [32,4,padded_M//128,4,padded_K//64,B]    ← B 在最后 + 6D 交织
              （且只写每组前 mask[i] 行，其余未定义）
```

grouped 的 mask 是 **int32 CUDA 张量**，给出每组有效行数。一个关键工程取舍：wrapper **不在运行期校验 mask 越界**——因为读 device 端 mask 会触发 host 同步、破坏 CUDA Graph 捕获；越界的 mask 是未定义行为（会写坏相邻组）。这是“为兼容 CUDA Graph 牺牲防御性检查”的典型例子，可对照 [u10-l1 API 日志](u10-l1-api-logging-debug.md) 的 CUDA Graph 兼容讨论。

#### 4.2.3 源码精读

**batched 量化**：

- `nvfp4_batched_quantize` 按 SM 选模块，把 `[B, M, K]` 交给底层 `fp4_batched_quantize_sm100`，返回 `[B, M, K/2]` 与每片一份的交织缩放：[fp4_quantization.py:1564-1599](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1564-L1599)。
- 底层 `fp4_batched_quantize_sm100` 为每个 batch 分配独立的 `[b, K/2]` 打包缓冲与 `[b, _compute_swizzled_layout_sf_size(...)]` 缩放缓冲：[fp4_quantization.py:398-462](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L398-L462)。
- 128×4 交织所需字节数的计算（N 填到 128、K_sf 填到 4）：[fp4_quantization.py:69-72](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L69-L72)。

**grouped FP4 量化**：

- `scaled_fp4_grouped_quantize` 按 SM 选模块，传 `(a[B,M,K], global_scale, mask)` 给底层，输出 `[M, K/2, B]`（B 在最后）与 6D 交织缩放 `[32,4,padded_M//128,4,padded_K//64,B]`：[fp4_quantization.py:1757-1796](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1757-L1796)。
- 测试 `test_scaled_fp4_grouped_quantize` 用 `permute` 把输出还原回 `[B, M, K/2]` 与 `[B, M, K/16]`，并逐专家对照单张量量化，是理解这套重排的最佳参考：[test_fp4_quantize.py:1365-1404](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_fp4_quantize.py#L1365-L1404)。

**grouped MXFP8 量化**（基于 cuTile 后端，专门喂给 masked grouped GEMM）：

- 公共入口 `mxfp8_grouped_quantize` 做大量输入校验（3D、CUDA、fp16/bf16、mask int32 同设备），要求 SM100+ 且 cuTile 可用，K 须整除 32：[fp8_quantization.py:380-446](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L380-L446)。
- 实现内部 `mxfp8_grouped_quantize_impl`：把 `[B,M,K]` view 成 `[B*M, padded_K]`，按 `mask` 构造 `problem_sizes`/`expert_offsets`/`blockscale_offsets`，调 cuTile kernel 后再 permute 缩放成 `[32,4,padded_M//128,scale_K//4,4,B]`：[fp8_quantization.py:286-344](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L286-L344)。
- docstring 明确写明 mask 越界“不校验、是调用方责任”，并解释为何（避免 host 同步破坏 CUDA Graph）：[fp8_quantization.py:392-400](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L392-L400)。

**SfLayout 枚举**——量化函数按它选缩放布局（128×4 交织 / 8×4 交织 / 线性）：[tllm_enums.py:194-201](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/tllm_enums.py#L194-L201)。

#### 4.2.4 代码实践

**目标**：以“源码阅读型实践”确认 batched 量化等价于“对每个 batch 各做一次 per-tensor 量化”，并看清 grouped 量化的布局重排。

**步骤**：

1. 读 `test_nvfp4_batched_quantize`：它对 `[B, M, N]` 调一次 `nvfp4_batched_quantize`，再循环对每个 `i` 调 `fp4_quantize(x[i], ...)`，断言两者逐片相等——这正是“batched = 逐片 per-tensor”的证明。
2. 读 `test_scaled_fp4_grouped_quantize`：注意它对输出做 `out.permute(2,0,1)` 把 `[M,K/2,B]` 还原成 `[B,M,K/2]`、对缩放做 `permute(5,2,4,0,1,3)` 还原成 `[B, M, K/16]`，再 `unswizzle_sf` 反交织、与单张量量化对照。这两个 permute 就是 grouped 重排的逆操作。
3.（可选，需 Blackwell）构造 `x = torch.randn(2, 128, 256, ...)`、`mask = torch.tensor([64, 128], int32)`，调 `scaled_fp4_grouped_quantize`，打印 `out.shape` 与 `out_scale.shape`，与上面公式对照。

**预期结果**：batched 测试中两路逐片完全相等（容限 `1e-5`）；grouped 输出形状符合 `[M, K/2, B]` 与 6D 缩放。`mask` 的作用——只前 `mask[i]` 行有效——体现在测试里只对 `out[i][:mask[i]]` 做断言。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`nvfp4_batched_quantize` 与对每个 batch 循环调 `nvfp4_quantize` 相比，有什么好处？
**答**：除了少一次 Python 循环，更重要的是**单次 kernel launch 量化整批**，省去 B 次 launch 开销；且输出缩放连续存放、可直接喂给后续批 GEMM。代价是 B、M、K 都要满足对齐约束。

**练习 2**：为什么 grouped 量化的 mask 要求是 **int32 CUDA 张量**，而不是 CPU 上的 Python list？
**答**：mask 要在 device 端被 kernel 读取以决定每组量化多少行；若放 CPU，每次传参都要 `H2D` 拷贝，且无法与上游算子的 device 输出无缝拼接。同时 wrapper 刻意不读回 mask 值做校验，正是为了避免 device→host 同步打断 CUDA Graph（见 [fp8_quantization.py:392-400](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp8_quantization.py#L392-L400)）。

---

### 4.3 KV cache 量化

#### 4.3.1 概念说明

权重是“一次性量化、反复使用”，KV cache 则不同：它在推理过程中**不断增长**（每个新 token 都要 append 一份 K/V，见 [u3-l2 Paged KV Cache](u3-l2-paged-kv-layout-append.md)）。把 KV cache 也压成 NVFP4，能让长上下文场景下的 KV 显存占用降到 1/4，从而支持更大 batch / 更长序列。FlashInfer 提供两条互补的 KV 量化路径：

- **`nvfp4_quantize_paged_kv_cache`**：把一个**已填好的分页 KV cache**（BF16/FP16）一次性离线量化成 NVFP4，产出 `(k_fp4, v_fp4)` 与 `(k_scales, v_scales)`，外加两个反量化用的全局缩放。它面向 SM100 trtllm-gen MHA kernel，**对 V 的缩放做 4-token 交织**以匹配该 kernel 的访存模式。
- **`nvfp4_kv_quantize` / `nvfp4_kv_dequantize`**：一对更底层的“逐块量化/反量化”算子，用**线性（非交织）块缩放**布局，`nvfp4_kv_dequantize` 只要 SM80+ 即可运行，适合“量化 KV 后又想回 BF16 看一眼 / 喂给非量化 attention”的回退场景。

两者的关键区别在**缩放布局**：前者为特定 Blackwell kernel 做了 V 缩放交织（与 [u5-l3 §4.2](u5-l3-fp4-gemm.md) 的 128×4 交织同源、但形态不同），后者保持朴素的 `[M, K/16]` 线性布局、可移植到 SM80+。

#### 4.3.2 核心流程

`nvfp4_quantize_paged_kv_cache` 的流程：

1. 按布局（NHD/HND，见 [u3-l2](u3-l2-paged-kv-layout-append.md)）解析 `num_pages/num_kv_heads/page_size/head_dim`。
2. 算 K/V 各自的全局缩放——注意这里公式是 \(g = \text{FLOAT8\_E4M3\_MAX}/\text{amax} = 448/\text{amax}\)，**没有乘 E2M1_MAX**（与权重 NVFP4 的 \(448\cdot6/\text{amax}\) 不同！），目的是让 kernel 输出的块缩放落在 \([0, 448/6]\) 区间。
3. 把整个 cache reshape 成 `[total_tokens, head_dim]`，调 `fp4_quantize(..., sf_vec_size=16, is_sf_swizzled_layout=False)`（**线性布局**）逐 token 量化。
4. 把打包数据 reshape 回原布局（`head_dim` 减半为 `head_dim//2`），缩放 reshape 成 `[..., head_dim//16]` 的 FP8。
5. **对 V 缩放做 4-token 交织**（K 缩放不动），匹配 trtllm-gen MHA kernel。
6. 返回 `(k_fp4, v_fp4)`、`(k_scales, v_scales)`，以及两个 `1/g`（反量化用）。

`nvfp4_kv_quantize` / `nvfp4_kv_dequantize` 则简单得多：前者把 `[M, K]` 量化成 `[M, K/2]` 打包 + `[M, K/16]` 线性 FP8 缩放（要求 SM100+ 的 `cvt.rn.satfinite.e2m1x2.f32` PTX 指令），后者把它们还原成 `[M, K]` 的指定 dtype（SM80+）。

#### 4.3.3 源码精读

**离线分页 KV cache 量化 `nvfp4_quantize_paged_kv_cache`**：

- 按布局解析四元组、算 K/V 全局缩放 \(g=448/\text{amax}\)（注意**无 \(\cdot6\)**），且对全零 cache 用 `max(amax, 1e-12)` 避免除零：[fp4_quantization.py:1650-1676](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1650-L1676)。
- reshape 成 `[total_tokens, head_dim]` 后用**线性布局**调 `fp4_quantize`：[fp4_quantization.py:1678-1689](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1678-L1689)。
- 把打包数据/缩放 reshape 回原布局（`head_dim→head_dim//2`，缩放 `→head_dim//16`，view 成 `float8_e4m3fn`）：[fp4_quantization.py:1692-1707](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1692-L1707)。
- **V 缩放的 4-token 交织**：要求 `page_size%4==0` 且 `head_dim%64==0`，按 `output[..., (t//4)*4*S + s*4 + t%4] = input[..., t*S + s]` 重排（注释指明对齐 TRT-LLM 的 V swizzle，**K 不需要**）：[fp4_quantization.py:1709-1746](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1709-L1746)。
- 返回 `(kv_cache_fp4, kv_cache_sf, 1/k_global_sf, 1/v_global_sf)`——后两个是反量化用的逆全局缩放：[fp4_quantization.py:1748-1754](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1748-L1754)。

**逐块 KV 量化/反量化对**：

- `nvfp4_kv_quantize`：要求 K 整除 16（`_NVFP4_BLOCK_SIZE=16`），输出 `[M, K/2]` 打包 + `[M, K/16]` 线性 FP8 缩放，依赖 SM100+ 的原生 FP4 转换指令：[fp4_quantization.py:2079-2116](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L2079-L2116)。
- `nvfp4_kv_dequantize`：把上述 `(fp4_data, block_scales, global_scale)` 还原成 `[M, K]` 的 bf16/fp16，**只要 SM80+**：[fp4_quantization.py:1923-1961](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1923-L1961)。
- 底层模块工厂 `get_fp4_kv_dequantization_module` 用独立 JIT 模块（`gen_fp4_kv_dequantization_module`），并注册 `nvfp4_kv_dequant` / `nvfp4_paged_kv_dequant` 两个 torch custom op：[fp4_quantization.py:1804-1860](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1804-L1860)。

**可移植性参考**：`nvfp4_kv_dequantize_paged`（带页表的反量化，喂给非 Blackwell 的 attention）挂了 `@supported_compute_capability([80,86,89,90,100,103,110,120,121])`，说明这套 KV 反量化刻意覆盖了非 Blackwell 架构：[fp4_quantization.py:1964-1969](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1964-L1969)。

**纯 PyTorch 反量化参考**——`test_fp4_kv_quantization.py` 的 `reference_dequant` 用 E2M1 查表 + FP8 缩放广播还原，是理解 KV 反量化数学最直观的样本：[test_fp4_kv_quantization.py:43-67](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_fp4_kv_quantization.py#L43-L67)。

#### 4.3.4 代码实践

**目标**：用 `nvfp4_kv_quantize` + `nvfp4_kv_dequantize` 跑一次 KV 量化往返，验证 KV cache 量化后能还原，并对照纯 PyTorch 参考实现。

**步骤**：

1. 造一个 `[M=128, K=256]` 的 BF16 张量（模拟一段 KV 的某一头）。
2. 算全局缩放（这里用 0.5 的小标量，与测试一致），调 `nvfp4_kv_quantize`。
3. 调 `nvfp4_kv_dequantize` 还原，与 `tests/utils/test_fp4_kv_quantization.py` 的 `reference_dequant` 对照。

```python
# 示例代码（量化需 SM100+；反量化 SM80+。完整可运行版本见 test_fp4_kv_quantization.py::test_nvfp4_kv_dequant）
import torch, flashinfer

M, K = 128, 256
x = torch.randn(M, K, dtype=torch.bfloat16, device="cuda")
global_scale = torch.tensor([0.5], dtype=torch.float32, device="cuda")

fp4, sf = flashinfer.nvfp4_kv_quantize(x, global_scale)   # fp4: (128,128), sf: (128,16)
x_hat = flashinfer.nvfp4_kv_dequantize(fp4, sf, global_scale, output_dtype=torch.bfloat16)
print("往返形状:", x_hat.shape, "dtype:", x_hat.dtype)
print("平均绝对误差:", float((x_hat.float() - x.float()).abs().mean()))
```

**预期结果**：`x_hat` 形状 `[128, 256]`、dtype bf16；与测试里的 `reference_dequant` 在 `atol=1e-3, rtol=1e-3` 内一致（见 `test_nvfp4_kv_dequant`）。注意这里 `global_scale=0.5` 是直接当乘子用（`reference_dequant` 公式为 `values * block_scales * global_scale`），与权重 NVFP4 的“传 \(1/g\)”约定不同，调用时务必看清每个函数 docstring 对 `global_scale` 的定义。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `nvfp4_quantize_paged_kv_cache` 的全局缩放是 \(448/\text{amax}\)，而权重 `nvfp4_quantize` 是 \(448\cdot6/\text{amax}\)？
**答**：权重路径希望块缩放（E4M3，≤448）乘 FP4 值（≤6）后正好覆盖 \(\text{amax}\cdot g = 448\cdot6\)，故 \(g=448\cdot6/\text{amax}\)；KV cache 路径为了让 kernel 输出的块缩放落在 \([0, 448/6]\)（配合下游 kernel 的固定缩放约定），故意少乘一个 6，用 \(448/\text{amax}\)。两种取法都是为了“喂给各自下游 kernel 时数值对得上”，是工程约定而非唯一正解。

**练习 2**：`nvfp4_quantize_paged_kv_cache` 为什么只对 V 缩放做 4-token 交织、K 缩放不动？
**答**：SM100 trtllm-gen MHA kernel 读取 V 缩放的访存轨迹需要按 4 个 token 交织排列（见源码注释 `output[..., (t//4)*4*S + s*4 + t%4] = input[..., t*S + s]`）；而 K 缩放 kernel 用真实 stride 读取、无需交织。所以交织是“为匹配特定 kernel 的访存模式”，与 [u5-l3](u5-l3-fp4-gemm.md) 的 128×4 权重缩放交织是同一类“为张量核定制布局”的思想，只是形态不同。

## 5. 综合实践

把本讲三个最小模块串起来，完成规格要求的实践：**对同一个 BF16 权重张量分别做 mxfp4 与 nvfp4 量化，统计量化前后显存占用，并比较两种格式反量化后的误差分布。**

1. 选一个 MLP 权重形状（例如 `M=256, K=4096`），造 BF16 权重 `W`。
2. **量化**：分别调 `mxfp4_quantize(W)` 与 `nvfp4_quantize(W, g)`（\(g=448\cdot6/\text{amax}\)，`sfLayout=layout_128x4`）。
3. **显存统计**：用 `.element_size() * .numel()` 算出 BF16、两种 FP4 打包数据、两种缩放各自的字节数，填入下表。预期打包数据都是 BF16 的 1/4（`[M, K/2]` uint8 vs `[M, K]` 的 2 字节）；缩放方面 NVFP4（块 16）比 MXFP4（块 32）多一倍缩放因子，但相对打包数据仍是小头。
4. **反量化与误差分布**：用 `mxfp4_dequantize` 还原 MXFP4、用 `e2m1_and_ufp8sf_scale_to_float(..., 1/g, 16, 1, True)` 还原 NVFP4；分别统计最大绝对误差、平均绝对误差、相对误差的中位数与 95 分位。
5. **分析**：两种格式的反量化误差分布是否接近？MXFP4 的 UE8M0（纯指数）块缩放与 NVFP4 的 E4M3 块缩放在你这个权重分布下哪个更优？（一般结论：块越小精度越高，故 NVFP4 块 16 通常误差略小；但 UE8M0 动态范围更大，对含离群值的权重更稳。）

```python
# 示例代码（量化需 Blackwell SM100/110/12x；CPU 反量化不支持 128x4 交织，故反量化也在 GPU）
import torch, flashinfer
from flashinfer.quantization.nvfp4_quantization_utils import FLOAT8_E4M3_MAX, FLOAT4_E2M1_MAX

M, K = 256, 4096
W = torch.randn(M, K, dtype=torch.bfloat16, device="cuda")
bf16_bytes = W.element_size() * W.numel()

amax = W.float().abs().max()
g = torch.tensor([FLOAT8_E4M3_MAX * FLOAT4_E2M1_MAX / amax], dtype=torch.float32, device="cuda")

# 1) 两种 FP4 量化
nv_q, nv_sf = flashinfer.nvfp4_quantize(W, g, sfLayout=flashinfer.tllm_enums.SfLayout.layout_128x4)
mx_q, mx_sf = flashinfer.mxfp4_quantize(W, backend="cuda")

def bytes_of(t): return t.element_size() * t.numel()
print(f"BF16:        {bf16_bytes} B")
print(f"NVFP4 packed:{bytes_of(nv_q)} B + sf {bytes_of(nv_sf)} B")
print(f"MXFP4 packed:{bytes_of(mx_q)} B + sf {bytes_of(mx_sf)} B")

# 2) 反量化与误差分布
W_nv = flashinfer.e2m1_and_ufp8sf_scale_to_float(
    nv_q.cpu(), nv_sf.cpu().reshape(-1), (1.0 / g).cpu(), 16, 1, True
).to("cuda").float()
W_mx = flashinfer.mxfp4_dequantize(mx_q, mx_sf).to("cuda").float()
W_f = W.float()

for name, Wh in [("NVFP4", W_nv), ("MXFP4", W_mx)]:
    err = (Wh - W_f).abs()
    rel = err / (W_f.abs() + 1e-6)
    print(f"{name}: max_abs={err.max():.4f}  mean_abs={err.mean():.4f}  "
          f"rel_p50={rel.median():.4f}  rel_p95={torch.quantile(rel.flatten(), 0.95):.4f}")
```

**预期结果**：两种 FP4 的打包数据字节数相同（都是 BF16 的约 1/4），缩放 NVFP4 ≈ 2× MXFP4；两种反量化误差在同一量级（平均相对误差 \(10^{-2}\)），但 NVFP4 因块更小通常略低、MXFP4 对离群值更稳。把 NVFP4 的 `sfLayout` 改成 `layout_linear` 还能顺便验证“反量化对线性/交织布局都支持”（8x4 布局不支持反量化）。具体数值**待本地验证**。

## 6. 本讲小结

- FlashInfer 的量化算子在 `flashinfer/quantization/` 下，遵循统一契约：输入高精度张量，输出 **(打包低比特数据 `x_q`, 块缩放 `sf`)**；反量化按 \(\hat{x} = v(x_q)\cdot s_f\cdot g_{\text{dequant}}\) 还原。底层是一个 kernel + 多个布尔/枚举开关，上层包出 `nvfp4_quantize`/`mxfp4_quantize`/`mxfp8_quantize` 等配方快捷方式。
- 三条主线：**MXFP8**（8 比特，块 32 + UE8M0，无全局缩放）、**NVFP4**（4 比特，块 16 + E4M3 + 全局缩放 \(g=448\cdot6/\text{amax}\)）、**MXFP4**（4 比特，块 32 + UE8M0，全局缩放折叠进块缩放）。反量化时 NVFP4 传 \(1/g\)、MXFP4 传 1.0。
- 缩放粒度三档：**per-tensor**（`nvfp4_quantize`/`mxfp4_quantize`/`mxfp8_quantize` 默认）、**batched**（`nvfp4_batched_quantize`，每片独立缩放）、**grouped/groupwise**（`scaled_fp4_grouped_quantize`/`mxfp8_grouped_quantize`，带 int32 mask、只量化有效行、输出重排成分组 GEMM 布局）；grouped 的 mask 越界不校验，是为兼容 CUDA Graph 而牺牲防御性检查。
- 反量化是一等公民算子：`e2m1_and_ufp8sf_scale_to_float`（FP4 通用，SM<90 退回纯 PyTorch CPU、仅线性布局）、`mxfp4_dequantize`、`mxfp8_dequantize_host`，服务于精度调试、对参考、回退计算。
- **KV cache 量化**两条路径：`nvfp4_quantize_paged_kv_cache`（离线量化整个分页 cache，全局缩放 \(448/\text{amax}\)、V 缩放做 4-token 交织喂给 SM100 trtllm-gen kernel）；`nvfp4_kv_quantize`/`nvfp4_kv_dequantize`（线性布局、逐块量化 SM100+ / 反量化 SM80+，可移植回退）。
- 全局缩放公式因下游约定而异：权重 NVFP4 用 \(448\cdot6/\text{amax}\)、KV cache 用 \(448/\text{amax}\)、`nvfp4_kv_dequantize` 直接把 `global_scale` 当乘子——调用时务必读清每个函数 docstring 的 `global_scale` 定义，别混用。

## 7. 下一步学习建议

- **接 GEMM**：量化产物最终喂给低比特 GEMM。NVFP4 权重 → [u5-l3 `mm_bf16_fp4`](u5-l3-fp4-gemm.md)；grouped FP4/MXFP8 → [u5-l4 Grouped GEMM](u5-l4-grouped-gemm.md) 与 [u6 MoE](u6-l1-moe-basics.md)；FP8 权重 → [u5-l2 FP8 GEMM](u5-l2-fp8-gemm.md)。把“量化 → 重排 → GEMM”串成完整链路。
- **per-token 激活缩放**：`nvfp4_quantize(per_token_activation=True)` 会额外返回每 token 的 FP32 缩放，用于更细粒度的激活量化。读 [fp4_quantization.py:1296-1327](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1296-L1327) 与 `nvfp4_quant_and_per_token_scale_sm100` 理解它与权重量化的区别。
- **块缩放重排细节**：本讲的 128×4 / 8×4 / 4-token 交织都是“为张量核定制布局”的实例，深入机制见 [u5-l3 §4.2 block-scale 重排](u5-l3-fp4-gemm.md) 与 `block_scale_interleave`（[fp4_quantization.py:1061-1099](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/fp4_quantization.py#L1061-L1099)）。
- **融合算子**：仓库还有把“归一化/RoPE + 量化”融合的算子（如 `rmsnorm_quant`、`rope_quantize_fp8`），见 `tests/trace/` 下相关参考正确性测试与 [u7-l2 归一化](u7-l2-normalization.md)、[u7-l3 RoPE](u7-l3-rope.md)，是“量化算子 + 其他算子 kernel 融合”的进阶样本。
- **trace 与基准**：本讲多个量化 API 都带 `@flashinfer_api(trace=...)`，可用 `fi_trace` 导出 benchmark JSON（见 [u9-l5 trace](u9-l5-trace-fi-trace.md)），用 `bench_gpu_time`（[u10-l3](u10-l3-benchmarking.md)）测量化 kernel 本身的耗时。
