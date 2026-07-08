# FP8→bf16 反量化与时钟周期分析

## 1. 本讲目标

本讲承接 u5-l1（FP8 KV cache 布局与量化），回答一个关键问题：**把 FP8 KV cache 喂给 Tensor Core 之前的那一步「反量化」，到底有多贵？贵到会不会拖垮整个 kernel？**

学完本讲你应该能够：

- 说清 H800（SM90）上为什么不能把 `fp8_e4m3` 一步转成 `bfloat16`，以及真实的指令序列是哪四步。
- 用「指令吞吐的倒数」估算单个 token 反量化大约消耗多少时钟周期（≈50 cycle）。
- 把这个数字和 64 头 MMA 的 ≈34 cycle 对比，得出 **dequantization-bound** 的结论，并理解它为何是后续 crossover 技术（u5-l3）的直接动机。

本讲是「读博客 + 读源码 + 算一笔账」三结合，不涉及 crossover 与 DSM 的实现细节（留给 u5-l3）。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些来自前置讲义的概念：

- **FP8 KV cache 的字节布局（u5-l1）**：DeepSeek-V3.2 的每个 token 占 656 字节 = 512 个 `fp8_e4m3`（NoPE 段，参与量化）+ 4 个 `fp32` scale（tile 级量化因子，tile=1×128，故 512/128=4 个）+ 64 个 `bf16`（RoPE 段，不量化）。本讲只讨论「把 512 个 fp8 反量化回 bf16」这一段。
- **MLA 解码 = MQA（u1-l1 / u3-l1）**：解码阶段 128 个 query head 共享 1 个 key head，`head_dim_k=576`、`head_dim_v=512`。一个 CTA 处理 64 个 query head，两个 CTA 合起来覆盖 128 个 head。
- **Tensor Core vs CUDA Core**：MMA（矩阵乘加）跑在 Tensor Core 上，吞吐极高；而类型转换（cvt）、逐元素乘（scale）跑在 CUDA Core 上，吞吐低得多。两者是同一 SM 内**不同的功能单元**，可以并行，但存在数据依赖。
- **compute-bound / memory-bound（u3-l1）**：用算术强度判断 bound。本讲讨论的不是访存 bound，而是「CUDA Core 上的反量化指令吞吐」这个新的瓶颈维度。

一个直觉：FP8 把 KV cache 压小了 2 倍，缓解了**显存压力**；但代价是把「反量化」这步计算从无到有地塞进了 kernel 的关键路径。本讲就是来算这笔「计算代价」的账。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [csrc/sm90/decode/sparse_fp8/components/dequant.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h) | 反量化的核心函数 `cvt_fp8x8_bf16x8` 与带 cache hint 的 gmem load 工具 |
| [csrc/sm90/decode/sparse_fp8/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h) | kernel 静态配置：`HEAD_DIM_*`、`BLOCK_M`、`QUANT_TILE_SIZE`、`NUM_SCALES`、`CLUSTER_SIZE` |
| [csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh) | 主循环里真实调用反量化的上下文（scale 加载、dequant 循环、RoPE 拼接） |
| [docs/20250929-hopper-fp8-sparse-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md) | 官方深度博客，给出四步指令序列与周期估算公式，是本讲理论分析的依据 |
| [csrc/defines.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/defines.h) | `bf16x8` / `float8` 等基础向量类型定义 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**反量化指令序列**（看清楚到底要发哪几条指令）、**时钟周期估算**（把这几条指令的代价加起来）、**dequant-bound 结论**（和 MMA 比出谁卡脖子）。

### 4.1 反量化指令序列

#### 4.1.1 概念说明

FP8 KV cache 节省了显存，但 Hopper 上的 **Tensor Core 不能直接吃 `fp8_e4m3` 做 MMA**（本 kernel 的 QK、PV 两个 GEMM 都要求输入是 `bfloat16`、输出是 `float32`，见博客第 11 行）。所以必须先把 fp8「反量化」回 bf16：

\[ \text{bf16}_i = \text{cvt\_to\_bf16}(\text{fp8}_i) \times \text{scale} \]

这里的 scale 是 u5-l1 讲过的 tile 级（1×128）量化因子的倒数形式，每个 128 维 tile 共用一个 scale。

问题在于：**H800 上没有一条「fp8 → bf16」的指令**。Hopper 原生支持的 fp8 转换只到 `half`（fp16），而从 fp16 到 bf16 又得绕道 `float32`。于是「反量化」在硬件层面被拆成一条转换链，每一步都是一条真实的 PTX `cvt` 指令。这正是后面周期估算要用四项相加的根因。

#### 4.1.2 核心流程

把一个 fp8 元素变成「乘好 scale 的 bf16」，硬件需要走四步：

```
fp8_e4m3 ──(1)──> half ──(2)──> float32 ──(3)──> bf16 ──(4)──> bf16 × scale
```

| 步骤 | 操作 | 说明 |
|------|------|------|
| (1) | fp8 → half | Hopper 有原生 `cvt` 把 fp8 存储转成 fp16 |
| (2) | half → float32 | fp16 → fp32 的 `cvt`，为了下一步转 bf16 必须经过 fp32 |
| (3) | float32 → bf16 | `__float22bfloat162_rn`，**这一步最慢** |
| (4) | bf16 × scale | `HFMA2`（bf16 融合乘加，2 元素/指令），scale 已先转成 bf16 |

注意第 (4) 步：博客文字写的是「乘以 fp32 scale」，但真实代码（见 4.1.3）把 scale 先转成了 bf16，再用 `bf16x2 × bf16x2` 的 `HFMA2` 一次算两个元素。这一点对后面吞吐估算很关键——第 (4) 步走的是 bf16 乘法的高吞吐通道，不是 fp32 标量乘法。

#### 4.1.3 源码精读

反量化的核心是 [csrc/sm90/decode/sparse_fp8/components/dequant.h:L20-L34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L20-L34) 的 `cvt_fp8x8_bf16x8`，它一次处理 8 个 fp8：

```cpp
__device__ __forceinline__
bf16x8 cvt_fp8x8_bf16x8(const fp8x8 &inputs, const __nv_bfloat162 &scale_bf162) {
    #define DEQUANT_FP8x4(OUTPUT_BF16_LO, OUTPUT_BF16_HI, FP8x4) \
    { \
        float4 fp32x4 = (float4)(FP8x4);                                    /* (1)+(2): fp8->half->fp32 */ \
        OUTPUT_BF16_LO = __float22bfloat162_rn({fp32x4.x, fp32x4.y})*scale_bf162;  /* (3)+(4) */ \
        OUTPUT_BF16_HI = __float22bfloat162_rn({fp32x4.z, fp32x4.w})*scale_bf162;  /* (3)+(4) */ \
    }
    bf16x8 result;
    DEQUANT_FP8x4(result.a01, result.a23, inputs.lo);
    DEQUANT_FP8x4(result.a45, result.a67, inputs.hi);
    return result;
}
```

源码看起来只有「三行」：`(float4)` 强转、`__float22bfloat162_rn`、`*scale_bf162`。但这是 C++ 层的假象——**编译器把 `(float4)(FP8x4)` 这一步下放成两条 PTX 指令**，因为 Hopper 没有 fp8→fp32 直转指令：

- `(float4)(FP8x4)` → 先 `cvt` 把 fp8 转 half（步骤 1），再 `cvt` 把 half 转 float32（步骤 2）。
- `__float22bfloat162_rn` → 步骤 3（float32→bf16），一次处理 2 个元素。
- `*scale_bf162` → 步骤 4，`HFMA2` 一次处理 2 个 bf16 元素。

所以 C++ 的「3 行」对应硬件的「4 步指令序列」，和博客的四步模型完全对上。`fp8x8` 被拆成 `lo`/`hi` 两个 `fp8x4`，每个 `fp8x4` 又拆成两组 `fp32x2`，于是 8 个元素 = 2×2 = 4 次 `__float22bfloat162_rn` + 4 次 HFMA2。

`fp8x8` / `fp8x16` 的定义见 [csrc/sm90/decode/sparse_fp8/components/dequant.h:L10-L18](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L10-L18)：`fp8x16` 就是两个 `fp8x8` 拼起来，正好 16 字节 = 128 bit，对应一条 128-bit 的宽 load。

再看主循环里的真实调用点 [csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh:L583-L598](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L583-L598)：

```cpp
for (int dim_idx = 0; dim_idx < HEAD_DIM_NOPE/64; dim_idx += 1) {
    fp8x16 cur_fp8x16 = load_128b_from_gmem<fp8x16, L1CacheHint::EVICT_LAST, L2PrefetchHint::B256>(gK_nope + dim_idx*64);
    bf16 scale = scales[MODEL_TYPE == ModelType::V32 ? dim_idx/2 : dim_idx];
    auto dequant_and_save_bf16x8 = [&](const fp8x8 &data, int offset) {
        bf16x8 cur_bf16x8 = cvt_fp8x8_bf16x8(data, __bfloat162bfloat162(*(__nv_bfloat16*)(&scale)));
        *(__int128_t*)(sK_nope_base + smem_offset) = *(__int128_t*)&cur_bf16x8;
        ...
    };
    dequant_and_save_bf16x8(cur_fp8x16.lo, 0);
    dequant_and_save_bf16x8(cur_fp8x16.hi, 8);
}
```

这段把 4.1.2 的指令序列落到了实处：

- 每轮 `dim_idx` 用 `load_128b_from_gmem` 一次搬 16 个 fp8（128 bit）。
- `scales[dim_idx/2]`：V3.2 下 `QUANT_TILE_SIZE=128`，所以每 2 个 64 维 tile 共用一个 scale（`dim_idx/2`）。
- `__bfloat162bfloat162(...)` 把单个 bf16 scale 广播成 `bf16x2`，喂给 `cvt_fp8x8_bf16x8` 做第 (4) 步的 HFMA2。
- 反量化结果直接写进 shared memory（`sK_nope_base`），供后续 MMA 消费。

scale 本身的加载在 [csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh:L543-L551](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L543-L551)：V3.2 一次性 load 4 个 fp32 scale（`float4` = 128 bit），再逐个 `(bf16)` 转成 bf16。

#### 4.1.4 代码实践

**实践目标**：确认 C++「3 行」与硬件「4 步」的对应关系，定位每一步的真实指令。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [dequant.h:L20-L34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L20-L34)，在 `DEQUANT_FP8x4` 宏的三行旁边各写一行注释，标注：
   - `(float4)(FP8x4)` → 步骤 (1) fp8→half + 步骤 (2) half→fp32
   - `__float22bfloat162_rn(...)` → 步骤 (3) fp32→bf16
   - `*scale_bf162` → 步骤 (4) bf16×scale（HFMA2）
2. 对照 [splitkv_mla.cuh:L585](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L585)，解释 `dim_idx/2` 里的「2」从哪来（提示：`HEAD_DIM_NOPE=512`、`QUANT_TILE_SIZE=128`、循环步长 64）。
3. （可选，待本地验证）若环境有 NVCC，可对 `cvt_fp8x8_bf16x8` 单独编译并用 `cuobjdump --dump-ptx` 查看，确认 `(float4)(FP8x4)` 确实下放成两条 `cvt` 指令。

**需要观察的现象**：C++ 源码行数（3 行）≠ PTX 指令数（4 类指令），且第 (3) 步 `cvt.f32.bf16` 是转换链里吞吐最低的一环。

**预期结果**：你能用一句话说清「为什么源码看着只有一次类型转换，周期估算却要算四次」——因为 `(float4)` 强转在 Hopper 上没有单条指令实现，必须经 half 中转。

#### 4.1.5 小练习与答案

**练习 1**：如果把 scale 直接保留为 `float32`、用 `bf16 × fp32 → bf16` 来做第 (4) 步，相比现在的「scale 先转 bf16 再 HFMA2」，吞吐会更差吗？为什么？

> **答案**：会更差。`HFMA2` 是 bf16×bf16→bf16 的融合指令，一次处理 2 个元素，吞吐高（256 元素/周期，见 4.2）。如果改成 bf16×fp32 标量乘，会退到低吞吐的标量 fp32 通道，且失去「2 元素/指令」的并行度。所以代码宁可先损失一次 `(bf16)scales_float[i]` 的转换，也要换到 HFMA2 的高吞吐通道。

**练习 2**：`cvt_fp8x8_bf16x8` 处理 8 个 fp8，内部调用了几次 `__float22bfloat162_rn`？每次处理几个元素？

> **答案**：4 次。`fp8x8` = 2 个 `fp8x4`，每个 `fp8x4` 调 2 次 `__float22bfloat162_rn`（LO 和 HI），共 2×2=4 次，每次处理 2 个元素，4×2=8 个元素。

### 4.2 时钟周期估算

#### 4.2.1 概念说明

知道有哪四步指令后，下一步是估算它们**总共花多少时钟周期**。方法来自 NVIDIA 的 [Throughput of Native Arithmetic Instructions](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html#throughput-of-native-arithmetic-instructions) 表：每类指令有一个「每周期可执行的元素数」的吞吐上限。**吞吐的倒数 = 每个元素消耗的周期数**。把四步的「每元素周期」加起来，乘以元素数 512，就得到单 token 反量化的周期下界。

这是一个**吞吐下界估计**（at least）：它假设指令流完美无气泡、寄存器依赖不卡顿，所以真实周期只会更高。但即便这个乐观估计已经超过 MMA，结论就站得住。

#### 4.2.2 核心流程

反量化单 token（512 个 fp8）的周期下界：

\[ T_{\text{dequant}} = \left(\frac{1}{64} + \frac{1}{64} + \frac{1}{16} + \frac{1}{256}\right) \times 512 \]

四项分别对应四步的「每元素周期」（= 1 ÷ 每周期元素数）：

| 步骤 | 操作 | 每周期元素数 | 每元素周期 |
|------|------|:---:|:---:|
| (1) | fp8→half | 64 | 1/64 |
| (2) | half→fp32 | 64 | 1/64 |
| (3) | fp32→bf16 | 16 | **1/16** ← 最慢 |
| (4) | bf16×scale (HFMA2) | 256 | 1/256 |

代入：

\[ \frac{1}{64} + \frac{1}{64} + \frac{1}{16} + \frac{1}{256} = \frac{4+4+16+1}{256} = \frac{25}{256} \approx 0.0977 \]

\[ T_{\text{dequant}} = \frac{25}{256} \times 512 = 50 \text{ cycle} \]

**关键观察**：第 (3) 步 fp32→bf16（16 元素/周期）是整条转换链的瓶颈，它一项（1/16）就占了总和的约 64%（16/25）。这也解释了为什么「不能直接 fp8→bf16」如此致命——只要还得绕道 fp32 再转回 bf16，就躲不开这个 1/16。

再算 MMA 的周期。H800 单 SM 每周期 4096 MMA FLOPs（由 `989 TFlops ÷ 1830 MHz ÷ 132 SMs ≈ 4096` 反推）。一个 CTA 处理 64 个 query head，每个 K/V token 的 QK 和 PV 两个 GEMM 共：

\[ T_{\text{MMA}} = \frac{64 \times (576 + 512) \times 2}{4096} = \frac{64 \times 1088 \times 2}{4096} = \frac{139264}{4096} \approx 34 \text{ cycle} \]

其中 `(576+512)` 是 `head_dim_k + head_dim_v`（QK 用 576、PV 用 512），`×2` 是一次乘 + 一次加算两个 FLOP。

#### 4.2.3 源码精读

公式里的几个数字都能在 [config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h) 里直接对上：

[csrc/sm90/decode/sparse_fp8/config.h:L23-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L23-L29)：

```cpp
static constexpr int HEAD_DIM_K = MODEL_TYPE == ModelType::V32 ? 576 : 512;
static constexpr int HEAD_DIM_V = 512;
static constexpr int HEAD_DIM_ROPE = 64;
static constexpr int HEAD_DIM_NOPE = HEAD_DIM_K - HEAD_DIM_ROPE;   // V32: 512，即反量化的元素数
static constexpr int QUANT_TILE_SIZE = MODEL_TYPE == ModelType::V32 ? 128 : 64;
static constexpr int NUM_SCALES = MODEL_TYPE == ModelType::V32 ? 4 : 8;
```

- `HEAD_DIM_NOPE = 576 - 64 = 512` → 公式里的「512 个 fp8」。
- `QUANT_TILE_SIZE = 128`、`NUM_SCALES = 4` → 512/128 = 4 个 scale，与 `scales[dim_idx/2]` 的 `/2` 自洽（循环步长 64，每 2 步换一个 scale）。
- `HEAD_DIM_K + HEAD_DIM_V = 576 + 512 = 1088` → MMA 公式里的 `(576+512)`。

[csrc/sm90/decode/sparse_fp8/config.h:L19-L21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L19-L21) 给出 MMA 公式里「64 个 query head」的来源：

```cpp
static_assert(NUM_HEADS == 64 || NUM_HEADS == 128);
static constexpr int NUM_M_BLOCKS = NUM_HEADS / 64;
static constexpr int CLUSTER_SIZE = NUM_M_BLOCKS;   // 128 头时 = 2，对应 crossover
```

`BLOCK_M = 64`（[config.h:L32](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L32)）就是一个 CTA 处理的 query head 数，正好是 MMA 公式里的分子「64」。这也意味着：128 个 head 需要两个 CTA，为 u5-l3 的 crossover（两个 CTA 共享同一份 KV）埋下伏笔。

#### 4.2.4 代码实践

**实践目标**：亲手复现博客的周期估算，确认 50 cycle 与 34 cycle 两个数字。

**操作步骤**：

1. 算反量化：按 4.2.2 的表格，把 `1/64 + 1/64 + 1/16 + 1/256` 手算成 `25/256`，再乘 512，确认等于 50。
2. 算 MMA：代入 `64 × (576+512) × 2 / 4096`，确认等于 34。
3. 算 H800 单 SM MMA 吞吐：`989e12 / (1830e6 × 132)`，确认 ≈ 4094 ≈ 4096 FLOP/cycle。
4. 把第 (3) 步 fp32→bf16 单独的贡献算出来：`(1/16) × 512 = 32 cycle`，占 50 cycle 的 64%，确认「转换链里 fp32→bf16 是主要开销」。

**需要观察的现象**：四步指令的代价高度不均——fp32→bf16 一项就比另外三项加起来还大。

**预期结果**：`T_dequant ≈ 50 cycle > T_MMA ≈ 34 cycle`。把这个不等式写下来，它就是下一节结论的全部依据。

> 说明：以上数字直接来自博客 [docs/20250929-hopper-fp8-sparse-deep-dive.md:L25](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L25)，本实践是「按指令吞吐倒数」复现这笔账，无需 GPU 即可完成。

#### 4.2.5 小练习与答案

**练习 1**：如果未来某代 GPU 增加了「fp8→bf16 直转」指令，吞吐与现在的 fp8→half 相同（64 元素/周期），四步变一步后单 token 反量化大约多少 cycle？

> **答案**：`1/64 × 512 = 8 cycle`（只剩一步 fp8→bf16，scale 乘法 1/256×512=2 cycle 可忽略，合计约 10 cycle）。这远小于 MMA 的 34 cycle，反量化就不再是瓶颈。这也说明「没有直转指令」是当前 dequant-bound 的根本硬件原因。

**练习 2**：为什么第 (4) 步 bf16×scale 的吞吐（256 元素/周期）比第 (3) 步 fp32→bf16（16 元素/周期）高这么多倍？

> **答案**：因为第 (4) 步用的是 `HFMA2`——bf16 的融合乘加指令，每条指令处理 2 个元素，且走的是 bf16 的高吞吐向量通道（128 指令/周期 × 2 元素 = 256 元素/周期）。而第 (3) 步是标量的 `cvt.f32.bf16` 类型转换，每条指令只出 1 个元素，且转换指令本身吞吐就低。功能不同、通道不同，差距是 16 倍。

### 4.3 dequant-bound 结论

#### 4.3.1 概念说明

有了 50 cycle 和 34 cycle 两个数，现在回答核心问题：**谁卡脖子？**

反量化跑在 CUDA Core，MMA 跑在 Tensor Core，两者是同一 SM 内的不同功能单元，**可以并行**。但它们之间有数据依赖：MMA 必须等反量化把 bf16 写进 shared memory 之后才能消费。于是整个流程像一个两级流水线：

```
[反量化 50 cycle] ──> bf16 in smem ──> [MMA 34 cycle] ──> 输出
```

稳态下，这个流水线的吞吐由**较慢的那一级**决定：

\[ T_{\text{per-token}} = \max(T_{\text{dequant}}, T_{\text{MMA}}) = \max(50, 34) = 50 \text{ cycle} \]

也就是说，每个 K/V token 至少要花 50 cycle，而其中 MMA 只用了 34 cycle——**Tensor Core 有 16 cycle 在空等反量化**。这就是「dequantization-bound」：不是访存喂不饱，也不是算力不够，而是「CUDA Core 上的反量化指令吞吐」跟不上 Tensor Core 的 MMA 吞吐，把强大的 Tensor Core 晾在了一边。

#### 4.3.2 核心流程

dequant-bound 的因果链：

1. FP8 压缩 KV → 省显存，但引入反量化。
2. H800 无 fp8→bf16 直转 → 反量化要走 4 步转换链。
3. 4 步合计 ≈50 cycle > MMA 的 ≈34 cycle → 反量化成为关键路径。
4. Tensor Core 每 token 空等 ≈16 cycle → 算力利用率不足。

**破局方向**：既然单 CTA 反量化 50 cycle 太慢，而 MMA（34 cycle）又比它快，那就想办法「让反量化也降到 34 cycle 以下」。最直接的办法是**少反量化一半**——这正是 u5-l3 的 crossover：利用 MQA 下两个 CTA（各管 64 个 head）访问同一份 KV 的事实，让两个 CTA 组成 size=2 的 cluster，各反量化一半，再通过 DSM 互换，每方都拿到完整 bf16。这样：

\[ T_{\text{dequant}}^{\text{crossover}} = 50 / 2 = 25 \text{ cycle} < 34 = T_{\text{MMA}} \]

反量化不再卡脖子，kernel 从 dequant-bound 转回 MMA-bound（或接近平衡），Tensor Core 利用率回升。博客报告这一套组合拳在 `batch=128, num_heads=128, s_q=2, topk=2048` 下达到 410 TFlops，相比未用 crossover 的 250 TFlops 提升显著。

#### 4.3.3 源码精读

主循环里反量化与写 smem 的交错结构，见 [csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh:L583-L598](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L583-L598)。注意 `dequant_and_save_bf16x8` 这个 lambda：

```cpp
auto dequant_and_save_bf16x8 = [&](const fp8x8 &data, int offset) {
    int smem_offset = (dim_idx*64 + offset) * TOPK_BLOCK_SIZE;
    bf16x8 cur_bf16x8 = cvt_fp8x8_bf16x8(data, __bfloat162bfloat162(*(__nv_bfloat16*)(&scale)));
    *(__int128_t*)(sK_nope_base + smem_offset) = *(__int128_t*)&cur_bf16x8;   // 写本地 smem
    if constexpr (CLUSTER_SIZE == 2) {
        st_async_128b(sK_nope_peer_base + smem_offset, cur_bf16x8, peer_bar_k_remote_ready);  // crossover: 写对方 smem
    }
};
```

这里能看到 dequant-bound 结论的两个落点：

- **反量化在 CUDA Core 上逐块做**：循环 `HEAD_DIM_NOPE/64 = 8` 轮，每轮 16 个 fp8，正是 4.2 算的那 512 个元素的来源。
- **`CLUSTER_SIZE == 2` 分支**就是 crossover 的入口——当 `NUM_HEADS=128` 时 `CLUSTER_SIZE=2`（[config.h:L20-L21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L20-L21)），每个 CTA 只反量化一半 KV（`idx_in_cluster*(TOPK_BLOCK_SIZE/2)`，见 [splitkv_mla.cuh:L533](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L533)），再用 `st_async_128b` 把结果异步写到 peer CTA 的 smem。这把 50 cycle 砍半到 25 cycle，正好是 4.3.2 推导的破局点。crossover 的完整同步机制（cluster transaction barrier 等）留待 u5-l3 展开。

RoPE 段（64 个 bf16，不量化）的加载在 [splitkv_mla.cuh:L609-L623](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L609-L623)，它直接 `load_128b_from_gmem<bf16x8>` 搬进 smem，**不走反量化**，所以不占这 50 cycle——这也是 u5-l1 强调「RoPE 不量化」的性能考量之一。

#### 4.3.4 代码实践

**实践目标**：把 50 vs 34 的对比落到「Tensor Core 空等多少 cycle」这个具体数字上，并验证 crossover 能否翻盘。

**操作步骤**（纸笔 + 源码阅读）：

1. 计算 dequant-bound 下 Tensor Core 的空闲比例：`(50 - 34) / 50 = 32%`，即约三成时间 Tensor Core 在等数据。
2. 计算 crossover 后的情况：dequant 降到 25 cycle，此时 `max(25, 34) = 34`，Tensor Core 空闲比例 `(34-25)/34 ≈ 26%`——注意这时卡瓶颈的已经变成 MMA 自身，反量化不再是关键路径。
3. 打开 [splitkv_mla.cuh:L583-L598](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L583-L598)，数一下单个 CTA 反量化的元素数：8 轮 × 16 元素 = 128 个 fp8。这正是「每个 CTA 只反量化一半」的实锤——512 个元素被两个 CTA 平分（`idx_in_cluster*(TOPK_BLOCK_SIZE/2)`），与 4.3.2 的 `50/2=25` 对应。
4. 对照博客 [docs/20250929-hopper-fp8-sparse-deep-dive.md:L47-L50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L47-L50) 的性能数字（410 TFlops vs 250 TFlops），理解 crossover 把 dequant-bound 解开后带来的实测收益。

**需要观察的现象**：crossover 把反量化周期从 50 砍到 25，刚好跌破 MMA 的 34，瓶颈从 CUDA Core 转移回 Tensor Core。

**预期结果**：你能用一行话讲清「为什么 sparse decode kernel 在没用 crossover 时是 dequantization-bound，用了之后又回到 MMA-bound」——因为 `max(50,34)=50` 变成了 `max(25,34)=34`。

> 待本地验证：上述空闲比例是理论下界估计，真实占用还受 smem 带宽、指令调度气泡、scale 加载等影响，实际 profiling 数字会有出入。

#### 4.3.5 小练习与答案

**练习 1**：假如把 `BLOCK_M` 从 64 改成 128（一个 CTA 处理 128 个 head），MMA 周期变成多少？dequant 周期会变吗？bound 结论会怎样？

> **答案**：MMA 周期翻倍 = `128×1088×2/4096 ≈ 68 cycle`。dequant 周期不变（每 token 仍是 512 个 fp8，≈50 cycle，与 query head 数无关）。此时 `max(50, 68) = 68`，kernel 转为 MMA-bound，反量化不再是瓶颈——但代价是一个 CTA 要占满全部 128 个 head，失去了 MQA 下两个 CTA 共享 KV 做 crossover 的机会，反而可能整体更差。这也是代码坚持 `BLOCK_M=64`、用 cluster=2 的原因。

**练习 2**：博客说 crossover 后达到 410 TFlops，仍低于 dense bf16 kernel 的 640 TFlops。结合本讲的周期分析，给出一个合理解释。

> **答案**：sparse kernel 的 topk 只有 2048（每个 query 只 attend 2048 个 token），相比 dense 长上下文，prologue/epilogue 的固定开销占比更大，拉低了有效吞吐；而且 crossover 只是「把反量化降到 MMA 之下」，并没有消除反量化的 25 cycle 开销（dense bf16 根本不需要反量化）。所以即便解开了 dequant-bound，sparse+fp8 的绝对吞吐仍低于 dense+bf16。

## 5. 综合实践

把本讲三个模块串起来，做一次「从指令到 bound 判定」的完整推演：

**任务**：为 DeepSeek-V3.2 的 FP8 sparse decode 写一份一页纸的「dequant 开销审计报告」。

要求：

1. **指令序列**：列出 `cvt_fp8x8_bf16x8` 的四步硬件指令，标注每步的 C++ 源码行（引用 [dequant.h:L20-L34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L20-L34)）和「为什么必须经 half/fp32 中转」。
2. **周期账本**：用表格列出 `(1/64, 1/64, 1/16, 1/256)` 四项，算出 50 cycle，并标出占比最大的步骤（fp32→bf16，64%）。
3. **MMA 对照**：从 [config.h:L23-L32](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L23-L32) 取出 `HEAD_DIM_K/V`、`BLOCK_M`，算出 34 cycle。
4. **bound 判定**：写出 `max(50,34)=50` → dequantization-bound，给出 Tensor Core 空闲 32% 的结论。
5. **破局预案**：说明 crossover 如何把 50 降到 25，并指出代码里 `CLUSTER_SIZE==2` 分支（[splitkv_mla.cuh:L590-L592](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L590-L592)）是这一思路的落地，详细机制留给 u5-l3。

**验收标准**：报告能自洽地回答「为什么 FP8 sparse decode 在 Hopper 上是 dequant-bound，以及为什么解法是少反量化一半而不是加宽 MMA」。

## 6. 本讲小结

- H800 的 Tensor Core 不能直接吃 `fp8_e4m3`，反量化必须走 **4 步转换链**：fp8→half→fp32→bf16→×scale，其中 C++ 的 `(float4)` 强转在硬件上被拆成两条 `cvt` 指令。
- 用「指令吞吐倒数」估算：`(1/64+1/64+1/16+1/256)×512 ≈ 50 cycle`，瓶颈是 fp32→bf16 那步（16 元素/周期，占总开销 64%）。
- 64 头 MMA 每 K/V token 只需 `64×(576+512)×2/4096 ≈ 34 cycle`。
- `max(50, 34) = 50`，反量化跑在 CUDA Core、慢于 Tensor Core 的 MMA，导致 Tensor Core 每 token 空等约 16 cycle → **dequantization-bound**。
- 破局思路是「少反量化一半」：MQA 下两个 CTA 共享同一份 KV，各反量化一半再互换，把 50 砍到 25 < 34，这正是 u5-l3 crossover 的动机。
- 源码落点：反量化函数在 `dequant.h`、维度常量在 `config.h`、真实调用与 crossover 入口在 `splitkv_mla.cuh` 的主循环。

## 7. 下一步学习建议

本讲证明了「反量化是瓶颈」，并指向了「少反量化一半」的解法。下一讲 **u5-l3（Crossover 与 DSM / CTA cluster）** 将展开这个解法的完整实现：

- size=2 的 CTA cluster 如何让两个 CTA 各反量化一半 KV；
- Distributed Shared Memory（DSM）与 `st.async` 如何把本方反量化结果写到对方 smem；
- cluster transaction barrier 如何在数据交换完成时同步两个 CTA；
- 结合本讲的 25 vs 34 周期结论，理解 crossover 为何能精确地把 dequant-bound 翻盘成 MMA-bound。

建议在进入 u5-l3 前，先回到 [splitkv_mla.cuh:L569-L598](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L569-L598) 通读一遍 `bar_k_remote_ready`、`st_async_128b`、`peer_bar_k_remote_ready` 这几个符号，它们就是 crossover 同步机制的入口。如果想从更高层理解 sparse decode 接口如何派发到这条 SM90 路径，可复习 u2-l4 的 ImplBase 框架与 u5-l4 的 sparse decode 接口。
