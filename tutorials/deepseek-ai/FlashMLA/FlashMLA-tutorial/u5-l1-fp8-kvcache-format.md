# FP8 KV Cache 布局与量化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 FlashMLA 在 FP8+sparse 解码下，**每个 KV token 占多少字节、这些字节内部如何分区**。
- 理解「tile 级（1×128）量化 + scale 因子」的原理，会手算一个 tile 的 `scale_inv`。
- 区分 **V3.2（V32）** 与 **MODEL1** 两种 KV cache 布局的字节结构与 scale 编码差异。
- 解释为什么只有 NoPE 部分（前 512 或 448 维）被量化、而 RoPE 部分（最后 64 维）保留 bf16 不量化。
- 用 `tests/quant.py` 提供的 `quantize_k_cache` / `dequantize_k_cache` 做一次量化-反量化往返，并测量量化误差。

本讲只讲 **KV cache 在显存里的字节布局与量化方法**，不展开 kernel 内部如何反量化（那是 u5-l2 的主题）、也不展开 crossover（u5-l3）。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（来自 u1-l1、u1-l4、u2-l2）：

- **MLA（Multi-head Latent Attention）**：DeepSeek-V3/V3.2 的注意力只缓存「压缩后的潜在向量」，且 **K 与 V 同源**——V 就是 K 的前 `head_dim_v` 维。解码阶段表现为 MQA：`h_q=128` 个 query 头共享 `h_k=1` 个 KV 头，`head_dim_k=576`、`head_dim_v=512`。
- **576 = 512 NoPE + 64 RoPE**：MLA 把每个 KV 向量的前 512 维当作「不带位置编码的 NoPE 部分」，后 64 维是「带旋转位置编码（RoPE）的部分」。
- **Paged KV cache**：KV 不是一整条连续序列，而是分页存在一个池子里，张量形状为 `(num_blocks, page_block_size, h_k, head_dim)`，再用 `block_table` 把逻辑序列映射到物理块。
- **`ModelType` 枚举**（u2-l2 讲过）：在 [csrc/params.h:5-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L5-L8) 中定义了 `V32` 与 `MODEL1` 两种模型类型，由 `d_qk` 决定（576→V32，512→MODEL1），它编码了 KV cache 的字节布局。本讲要讲清楚这两种布局到底差在哪。

补充几个本讲要用到的基础概念：

- **fp8_e4m3**：8 位浮点，1 位符号 + 4 位指数 + 3 位尾数，可表示的最大有限值是 **448**。它是本讲 NoPE 部分的量化目标类型。
- **UE8M0 / fp8_e8m0**：「无符号、8 位指数、0 位尾数」的格式，即只能表示 **2 的整数次幂**。本讲里它被用来编码 scale 因子（MODEL1 布局），目的是让乘法可以用廉价的指数加法/位移近似。
- **scale（缩放因子）**：量化时把一个数值范围「挤」进 fp8 的表示范围；反量化时再「拉」回来。本讲用的是「scale 的倒数」记作 `scale_inv`，下文会详细说明。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tests/quant.py` | **本讲核心**。提供 `FP8KVCacheLayout` 枚举、`quantize_k_cache`（量化）、`dequantize_k_cache`（反量化），是 KV cache 字节布局最权威、最可运行的参考实现。 |
| `flash_mla/flash_mla_interface.py` | Python 接口层。其 `flash_mla_with_kvcache` 的 docstring 文档化了 656 字节布局，并在 sparse 分支强制 `is_fp8_kvcache=True`。 |
| `docs/20250929-hopper-fp8-sparse-deep-dive.md` | 官方博客。用一段话讲清了 FP8 KVCache Format 的设计动机与 656 字节构成。 |
| `csrc/params.h` | 定义 `ModelType { V32, MODEL1 }`，是两种布局的 C++ 侧标签。 |
| `csrc/api/sparse_decode.h` | C++ 接口层。在运行时根据 `d_qk` 推断 `ModelType`，并校验 `bytes_per_token` 与 block 连续性。 |
| `csrc/sm90/decode/sparse_fp8/config.h` | kernel 侧静态配置。声明了 `HEAD_DIM_K / QUANT_TILE_SIZE / NUM_SCALES` 等随 `ModelType` 变化的常量。 |
| `csrc/sm90/decode/sparse_fp8/components/dequant.h` | kernel 侧反量化的最小片段，证明 kernel 内做的是「fp8→bf16 再乘 scale」，正好是本讲量化过程的逆运算。 |

## 4. 核心概念与源码讲解

### 4.1 量化动机与 NoPE / RoPE 拆分

#### 4.1.1 概念说明

DeepSeek-V3.2 把上下文长度从 64K 翻倍到 128K。博客里给了一笔账：单个 128K token 的请求，光 KV cache 就要占用约 8.72 GiB 显存（[docs/20250929-hopper-fp8-sparse-deep-dive.md:3](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L3)）。显存压力会导致 OOM，或者被迫用很小的 batch size、让 GPU 算力闲置。为了在「几乎不损失精度」的前提下把 KV cache 压小，FlashMLA 引入了 **FP8 KV cache**。

MLA 解码阶段每个 KV token 本来是一个 576 维的 bf16 向量，占 \(576 \times 2 = 1152\) 字节。注意这 576 维不是同质的，而是被拆成两段：

- **前 512 维 = NoPE 部分**：不带旋转位置编码的潜在表示，是注意力的主体，数值范围相对平稳，**对量化友好**。
- **后 64 维 = RoPE 部分**：带旋转位置编码（Rotary Position Embedding）的部分。RoPE 是把向量两两成对做旋转，旋转角对**绝对数值的精度很敏感**——量化引入的误差会直接扭曲位置编码，进而破坏「相对位置」信息。因此这一段**不量化**，保留 bf16。

这就是「NoPE / RoPE 拆分」的本质：**把对精度敏感的 RoPE 维摘出来原样保留，只对数值平稳的 NoPE 维做 FP8 量化**。博客的原话是：

> we apply tile-level quantization (with a tile size of \(1 \times 128\)) to the first 512 elements in each token's KV Cache. … For the remaining 64 elements (the RoPE part), we do not apply quantization as they are sensitive to precision loss.

见 [docs/20250929-hopper-fp8-sparse-deep-dive.md:9](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L9)。

#### 4.1.2 核心流程

把一个 576 维 bf16 token 变成 FP8 token 的整体流程：

```
输入: bf16 向量 x ∈ R^576
├─ NoPE 段 x[:512]  ──→  tile 级 FP8 量化  ──→  512 个 fp8_e4m3 + 4 个 scale
└─ RoPE 段 x[512:576] ──→  原样拷贝        ──→  64 个 bf16
拼接三段写入显存（见 4.3）
```

注意：因为 **K 与 V 同源**（V 就是 K 的前 `head_dim_v=512` 维），所以这份量化后的缓存**同时充当 K 和 V**——只要反量化回来，QK 和 PV 两次矩阵乘都用它。这是 MLA 能用「一份量化缓存」服务两次 GEMM 的关键。

#### 4.1.3 源码精读

Python 接口的 docstring 把 656 字节布局写成了权威说明，明确区分了三段：

```python
# flash_mla_interface.py:94-98
# - First 512 bytes: The "quantized NoPE" part, containing 512 float8_e4m3 values.
# - Next 16 bytes: Scale factors, containing 4 float32 values. The first float32
#   is the scale for the first 128 float8_e4m3 values, the second for the next 128, and so on.
# - Last 128 bytes: The "RoPE" part, containing 64 bfloat16 values. This part is not quantized for accuracy.
```

完整链接：[flash_mla/flash_mla_interface.py:92-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L92-L99)。这段注释说明：每 128 个 fp8 共用一个 fp32 scale，512/128=4，正好 4 个 scale。

而在接口层，**只要走 sparse 解码就强制 FP8**——`indices` 不为空时，`is_fp8_kvcache` 必须为真：

```python
# flash_mla_interface.py:151-154
if topk is not None:
    # Sparse attention
    assert not causal, "causal must be False when sparse attention is enabled"
    assert is_fp8_kvcache, "is_fp8_kvcache must be True when sparse attention is enabled"
```

见 [flash_mla/flash_mla_interface.py:151-160](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L151-L160)。也就是说，**本讲的 FP8 布局是 sparse 解码路径的硬性前提**，dense 解码仍走 bf16。

#### 4.1.4 代码实践

**实践目标**：用阅读理解的方式，确认「RoPE 不量化」这件事在代码里有据可查。

**操作步骤**：

1. 打开 [tests/quant.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py)，定位 `quantize_k_cache` 的 V32 分支。
2. 找到这一行：`result_k_rope_part[:] = input_k_cache[..., d_nope:]`（[tests/quant.py:41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L41)）。

**需要观察的现象**：RoPE 段是用 `[:]` 直接赋值拷贝的，**没有除以 scale、也没有 `.to(torch.float8_e4m3fn)`**；它通过 `.view(input_k_cache.dtype)` 把字节重新解释成 bf16（[tests/quant.py:40](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L40)）。

**预期结果**：你能用一句话解释——「RoPE 段在量化函数里只是字节级别的原样搬运，所以它在显存里仍是 bf16 精度」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 KV token 完全用 bf16 存（576 维），需要多少字节？V32 的 FP8 布局压缩比是多少？

**答案**：bf16 全精度 = \(576 \times 2 = 1152\) 字节；FP8 V32 布局 = 656 字节；压缩比 \(1152 / 656 \approx 1.76\text{x}\)。

**练习 2**：为什么不干脆把整段 576 维都量化成 fp8，那样能压到更小？

**答案**：后 64 维是 RoPE，旋转位置编码对数值精度敏感，量化会扭曲相对位置信息、掉精度。所以牺牲一点压缩率，把 RoPE 段保留为 bf16。

---

### 4.2 Tile 级量化与 UE8M0 scale 因子

#### 4.2.1 概念说明

「量化」要解决的问题是：bf16 的数值范围很大（约 ±65536），而 fp8_e4m3 最大只能表示 448。直接把 bf16 转成 fp8 会大量溢出截断到 ±448，误差爆炸。解决办法是给每**一小段（tile）**配一个 scale 因子，把这段数值先「缩小」进 fp8 范围，存缩放后的 fp8 + 这段共用的 scale；用的时候再把 fp8「放大」回来。

FlashMLA 用的 tile 粒度是 **\(1 \times 128\)**：也就是同一个 token 的每连续 128 个 NoPE 维共用一个 scale。512 维 NoPE → 4 个 tile → 4 个 scale。为什么用这么细的粒度？因为越细，每个 scale 越贴合自己那一段的数值范围，量化误差越小（这叫 fine-grained / tile-level quantization）。

这里有个关键设计选择：**scale 只能是 2 的整数次幂**（UE8M0 格式）。原因有二：① 反量化时「乘一个 2 的幂」可以用几乎免费的浮点指数加法实现，省指令；② MODEL1 布局下 scale 本身就用 1 字节的 `fp8_e8m0` 存储，天然只能表示 2 的幂。

代码里存的是 **scale 的倒数**，记作 `scale_inv`（名字里的 `_inv` = inverse）。这是因为量化时「除以 scale_inv」、反量化时「乘 scale_inv」，存倒数可以让两步都用同一个量、且反量化的乘法更直接。

#### 4.2.2 核心流程

对一个 128 元素的 tile，量化（encode）的数学过程：

1. 求 tile 内最大绝对值 \(m = \max_i |x_i|\)。
2. fp8_e4m3 的上限是 448，理想的 `scale_inv` 应让 \(m\) 正好映到 448：\(\text{scale\_inv}_{\text{ideal}} = m / 448\)。
3. UE8M0 约束：把 `scale_inv` **向上取整**到最近的 2 的幂：
   \[
   \text{scale\_inv} = 2^{\lceil \log_2(\max(m/448,\;10^{-4})) \rceil}
   \]
   「向上取整」是安全方向：scale_inv 变大 → 量化时除得更多 → 结果更小 → **绝不会溢出 448**。
4. 量化：\(\hat{x}_i = \text{round\_to\_fp8\_e4m3}(x_i / \text{scale\_inv})\)，结果落在 \([-448, 448]\)。
5. 存储 \(\hat{x}\)（fp8）和 `scale_inv`。

反量化（decode，kernel 内做）：
\[
\tilde{x}_i = \hat{x}_i \times \text{scale\_inv}
\]

两个误差来源：① scale 向上取整到 2 的幂（最多让真实范围只用到 ~一半）；② fp8_e4m3 的尾数只有 3 位（每个 binade 内 8 个格点，最坏相对舍入误差较大）。测试里的容差 `rel_tol=2.01/128 ≈ 1.57%`（见 `tests/test_flash_mla_sparse_decoding.py`）就反映了这种量化噪声。

#### 4.2.3 源码精读

把 scale 取整到 2 的幂的辅助函数：

```python
# tests/quant.py:17-18
def _cast_scale_inv_to_ue8m0(scales_inv, out_dtype = torch.float32):
    return torch.pow(2, torch.clamp_min(scales_inv, 1e-4).log2().ceil()).to(out_dtype)
```

见 [tests/quant.py:17-18](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L17-L18)。`clamp_min(..., 1e-4)` 是为了在全零 tile（\(m=0\)）时不让 `log2(0)=-inf`，退化成一个极小的 scale_inv。

V32 的 tile 量化主循环（每个 tile 算一个 scale、量化一段）：

```python
# tests/quant.py:43-50  （V32 分支）
for tile_idx in range(0, num_tiles):                       # num_tiles = 4
    cur_scale_factors_inv = torch.abs(
        input_k_cache[..., tile_idx*tile_size:(tile_idx+1)*tile_size]
    ).max(dim=-1).values.float() / 448.0                   # m / 448
    cur_scale_factors_inv = _cast_scale_inv_to_ue8m0(cur_scale_factors_inv)   # 取整到 2 的幂
    result_k_scale_factor[:, :, tile_idx] = cur_scale_factors_inv

    cur_scale_factors_inv.unsqueeze_(-1)
    cur_quantized_nope = (input_k_cache[..., tile_idx*tile_size:(tile_idx+1)*tile_size].float()
                          / cur_scale_factors_inv.float()).to(torch.float8_e4m3fn)   # 量化
    result_k_nope_part[..., tile_idx*tile_size:(tile_idx+1)*tile_size] = cur_quantized_nope
```

完整链接：[tests/quant.py:35-53](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L35-L53)。注意第 44 行除以的是 `448.0`（fp8_e4m3 上限），第 49 行量化时「除以 scale_inv」。

反量化在 kernel 里做，但 `quant.py` 也提供了 host 侧参考实现，用来验证和测试：

```python
# tests/quant.py:101-104  （dequantize_k_cache 的 V32 分支）
for tile_idx in range(0, num_tiles):
    cur_nope = input_nope[..., tile_idx*tile_size:(tile_idx+1)*tile_size].to(torch.float32)
    cur_scales = input_scale[..., tile_idx].unsqueeze(-1)
    result[..., tile_idx*tile_size:(tile_idx+1)*tile_size] = cur_nope * cur_scales   # 乘 scale_inv 还原
```

见 [tests/quant.py:101-104](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L101-L104)。这正是「fp8×scale_inv」的还原，与 kernel 内的反量化方向一致。kernel 侧的最小反量化片段可对照 [csrc/sm90/decode/sparse_fp8/components/dequant.h:20-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L20-L34)：它把 fp8 先转成 float 再乘 `scale`，逻辑互逆（kernel 侧的细节留待 u5-l2）。

#### 4.2.4 代码实践

**实践目标**：手算一个 tile 的 `scale_inv`，确认「向上取整到 2 的幂」不会让量化值溢出 448。

**操作步骤**：

1. 假设某个 tile 的 128 个 bf16 元素里，最大绝对值 \(m = 300.0\)。
2. 手算：`scale_inv_ideal = 300 / 448 ≈ 0.6696`；\(\log_2(0.6696) \approx -0.579\)；\(\lceil -0.579 \rceil = 0\)；故 `scale_inv = 2^0 = 1.0`。
3. 验证：最大元素量化后为 \(300 / 1.0 = 300 \le 448\) ✓，不溢出。
4. 再试 \(m = 500.0\)：`scale_inv_ideal = 500/448 ≈ 1.116`；\(\lceil\log_2 1.116\rceil = \lceil 0.157 \rceil = 1\)；`scale_inv = 2`；\(500/2 = 250 \le 448\) ✓。

**需要观察的现象**：无论 \(m\) 多大，向上取整到 2 的幂后 \(m / \text{scale\_inv} \le 448\) 恒成立。

**预期结果**：你能写出一句不变式——「因为 \(\text{scale\_inv} \ge m/448\)（向上取整），所以 \(m/\text{scale\_inv} \le 448\)，量化必不溢出」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 scale 必须是 2 的整数次幂？换成任意 fp32 scale 会怎样？

**答案**：2 的幂让反量化的「乘 scale」可用廉价的指数运算实现，且 MODEL1 布局里 scale 用 1 字节 e8m0 存储，物理上只能表示 2 的幂。任意 fp32 scale 虽然误差更小，但反量化更慢、存储更大，不符合「反量化已经是瓶颈」的现状（见 u5-l2）。

**练习 2**：`clamp_min(..., 1e-4)` 去掉会怎样？

**答案**：当 tile 全零（\(m=0\)）时 `scale_inv_ideal=0`，`log2(0)=-inf`，后续 `pow(2, -inf)=0`，反量化时会出现 `0 * 0` 或除零异常。clamp 把全零 tile 的 scale 钳到一个极小的 2 的幂（约 \(2^{-13}\)），保证数值安全。

---

### 4.3 V3.2（V32）布局：656 字节详解

#### 4.3.1 概念说明

V32 布局是 DeepSeek-V3/V3.1/V3.2 用的格式，也是博客和接口 docstring 主推的格式。它的元数据由 `FP8KVCacheLayout.get_meta()` 给出：

```python
# tests/quant.py:13
FP8KVCacheLayout.V32_FP8Sparse: (576, 512, 64, 128, 4)
# 即 (d=576, d_nope=512, d_rope=64, tile_size=128, num_tiles=4)
```

每个 KV token 占 **656 字节**，分三段连续存放：

| 区段 | 字节范围 | 字节数 | dtype | 内容 |
|------|---------|--------|-------|------|
| NoPE 量化段 | \([0, 512)\) | 512 | fp8_e4m3 ×512 | 量化后的前 512 维 NoPE |
| Scale 段 | \([512, 528)\) | 16 | fp32 ×4 | 4 个 `scale_inv`，每个管 128 维 |
| RoPE 段 | \([528, 656)\) | 128 | bf16 ×64 | 原样的 64 维 RoPE |

合计 \(512 + 16 + 128 = 656\) 字节。

注意一个细节：**scale 用的是 fp32**（4 字节 ×4 = 16 字节），而不是 1 字节的 e8m0。这是 V32 与 MODEL1 最显著的区别之一（4.4 会讲 MODEL1 用 1 字节 e8m0）。

#### 4.3.2 核心流程

V32 布局的「装配」流程（在 `quantize_k_cache` 里）：

```
申请 result: shape (num_blocks, block_size, 656)，dtype = fp8_e4m3
把它「字节视图」切成三段：
  result[..., :512]                    → 当 fp8_e4m3 看：写量化后的 NoPE
  result[..., 512:528].view(float32)   → 当 4 个 fp32 看：写 scale_inv
  result[..., 528:].view(bfloat16)     → 当 64 个 bf16 看：原样写 RoPE
最后 reshape 回 (num_blocks, block_size, 1, 656)  # h_k 维=1
```

关键技巧是 **「整块用 fp8 字节申请，再用 `.view()` 把对应字节重新解释成 float32 / bfloat16」**——这样三段在显存里物理连续，符合 kernel 一次性 TMA 加载的诉求。

#### 4.3.3 源码精读

`bytes_per_token` 的计算就是 656 的来源：

```python
# tests/quant.py:36  （V32 分支）
bytes_per_token = d_nope + num_tiles*4 + input_elem_size*d_rope
                = 512   + 4*4         + 2*64
                = 512 + 16 + 128 = 656
```

见 [tests/quant.py:35-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L35-L41)。其中 `num_tiles*4` = 4 个 fp32 scale（每个 4 字节），`input_elem_size*d_rope` = `2*64` = RoPE 段字节数。

C++ 接口层用完全相同的算式校验用户传入的张量形状（注意它把三段写得更显式）：

```cpp
// csrc/api/sparse_decode.h:289-301
int bytes_per_token;
if (d_qk == 576 && d_v == 512) {
    // V3.2 style
    bytes_per_token = 512 + 64*2 + (512/128)*4;          // = 512 + 128 + 16 = 656
} else if (d_qk == 512 && d_v == 512) {
    // MODEL1 style
    bytes_per_token = 448 + 64*2 + (448/64)*1 + 1;       // = 584，见 4.4
}
KU_CHECK_SHAPE(kv, num_blocks, page_block_size, h_kv, bytes_per_token);
TORCH_CHECK(kv.stride(1) == bytes_per_token, "The whole block must be contiguous ...");
```

见 [csrc/api/sparse_decode.h:289-304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L289-L304)。这段还强调了一个硬约束：**FP8 模式下整个 block 必须连续（`stride(1) == bytes_per_token`）**，因为 kernel 要按 656 字节为步长一次性搬运一个 token。

#### 4.3.4 代码实践（本讲主实践：量化-反量化往返测误差）

**实践目标**：用 `tests/quant.py` 的官方实现对一个小 bf16 KV cache 做量化再反量化，测量最大量化误差，并验证 656 字节布局。

**操作步骤**：

把下面这段脚本存成 `check_fp8_layout.py`，放在仓库根目录运行（需要能 `import` 到 `tests.quant`，建议在仓库根目录执行，或把 `tests` 加入路径）。

```python
# 示例代码：依赖 tests/quant.py，需在仓库根目录运行
import sys, torch
sys.path.insert(0, "tests")
from quant import quantize_k_cache, dequantize_k_cache, FP8KVCacheLayout

torch.manual_seed(0)
device = "cuda" if torch.cuda.is_available() else "cpu"

# 1. 构造一个小 bf16 KV: (num_blocks=2, block_size=64, h_k=1, d=576)
k = torch.randn(2, 64, 1, 576, dtype=torch.bfloat16, device=device)

# 2. 用 V32 布局量化
q = quantize_k_cache(k, FP8KVCacheLayout.V32_FP8Sparse)
print("量化后 shape:", q.shape, "dtype:", q.dtype)   # 期望 (2, 64, 1, 656)

# 3. 验证每 token 字节数 == 656
assert q.shape[-1] == 656, f"expected 656, got {q.shape[-1]}"

# 4. 反量化
dq = dequantize_k_cache(q, FP8KVCacheLayout.V32_FP8Sparse)
print("反量化后 shape:", dq.shape, "dtype:", dq.dtype)  # 期望 (2, 64, 1, 576) bf16

# 5. 测误差（只比较被量化的 NoPE 前 512 维；RoPE 后 64 维应几乎无损）
err_nope = (k[..., :512].float() - dq[..., :512].float()).abs().max().item()
err_rope = (k[..., 512:].float() - dq[..., 512:].float()).abs().max().item()
print(f"NoPE 段最大绝对误差: {err_nope:.4f}")
print(f"RoPE 段最大绝对误差: {err_rope:.6f}   (理论应接近 0，因为 RoPE 不量化)")
```

**需要观察的现象**：

- 量化后最后一维正好是 **656**，dtype 是 `float8_e4m3`。
- RoPE 段误差应**接近 0**（只是 bf16 字节搬运，无精度损失）。
- NoPE 段有非零误差，量级与数值范围相关（randn 数据 |x|≈1 量级时，scale_inv 通常被取整为 1 或更小，单元素误差在 fp8_e4m3 格点间距量级）。

**预期结果**：

- `q.shape == (2, 64, 1, 656)`、`dq.shape == (2, 64, 1, 576)`。
- RoPE 误差 ≈ 0；NoPE 误差 > 0 且随数值范围增大而增大。
- 若无 GPU，本实践可在 CPU 上运行（`device="cpu"`），但 `torch.float8_e4m3fn` 与 `float8_e8m0fnu` 的 CPU 支持视 PyTorch 版本而定，**若 CPU 报 dtype 不支持，则改在 GPU 上运行；具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 V32 用 **fp32** 存 scale，而不是像 fp8 一样只花 1 字节？

**答案**：V32 是博客主推、最早落地的格式，当时选择了精度更高、实现更简单的 fp32 scale（4 个 scale 才 16 字节，占 656 的 2.4%，开销可接受）。MODEL1 为了进一步省存储、且配合 e8m0 反量化指令，才改用 1 字节 e8m0 scale（见 4.4）。

**练习 2**：接口为什么要求 FP8 模式下「整个 block 连续」（`stride(1) == bytes_per_token`）？

**答案**：kernel 一次 TMA/宽加载按 `bytes_per_token`（656）为步长取一整个 token，跨 token 不应有 stride 间隙；不连续会导致读到错位的字节，把 scale/RoPE 当成 fp8 解析。

---

### 4.4 MODEL1 布局：584 字节与 e8m0 scale

#### 4.4.1 概念说明

MODEL1 是另一种模型配置，对应 `d_qk=512`（而不是 576）。它的 MLA 维度划分不同：

```python
# tests/quant.py:14
FP8KVCacheLayout.MODEL1_FP8Sparse: (512, 448, 64, 64, 7)
# 即 (d=512, d_nope=448, d_rope=64, tile_size=64, num_tiles=7)
```

也就是 512 维 = **448 NoPE + 64 RoPE**，tile 更小（\(1 \times 64\)），所以 448 维被切成 **7 个 tile、7 个 scale**。每个 token 占 \(448 + 128 + 7 + 1 = 584\) 字节（外加 block 级对齐填充）。

MODEL1 与 V32 的两个核心差异：

1. **scale 用 1 字节的 `fp8_e8m0`（UE8M0）**，而不是 4 字节 fp32。7 个真 scale + 1 字节填充，凑成 8 字节对齐的槽位。
2. **多了一层 block 级 padding**：每个 block 的总字节要向上对齐到 576 的整数倍，便于 kernel 用对齐的宽访问。

#### 4.4.2 核心流程

MODEL1 的装配流程与 V32 思路一致（fp8 字节申请 + `.view()` 重解释），但段落顺序与 padding 不同：

```
bytes_per_token = d_nope + 2*d_rope + num_tiles + 1 = 448 + 128 + 7 + 1 = 584
size_per_block_padded = 向上对齐到 576 倍数

把 block 内字节切成：
  [: block_size*(d_nope+2*d_rope)] = [: 576]   → 前 448 字节 fp8=NoPE，后 128 字节 bf16=RoPE
  [block_size*576 :]                            → 8 字节：前 7 个 e8m0=scale，最后 1 字节=填充
```

注意 MODEL1 把 **NoPE 与 RoPE 连续放在前 576 字节**（恰好等于 V32 的 `head_dim_k`，对齐到这个粒度），scale 放在最后；而 V32 是「NoPE | scale | RoPE」的顺序。

#### 4.4.3 源码精读

MODEL1 的 `bytes_per_token` 与 block padding：

```python
# tests/quant.py:56-58  （MODEL1 分支）
bytes_per_token = d_nope + 2*d_rope + num_tiles + 1     # 448 + 128 + 7 + 1 = 584
size_per_block_padded = (block_size*bytes_per_token + 576-1) // 576 * 576
```

见 [tests/quant.py:55-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L55-L62)。`2*d_rope` 是因为 RoPE 用 bf16（每元素 2 字节，64 个 = 128 字节）。

MODEL1 的 scale 是 **e8m0**（1 字节 UE8M0），且只取 7 个、第 8 字节留作对齐填充：

```python
# tests/quant.py:62
result_k_scale_factor = result[:, block_size*(d_nope+2*d_rope):].view(num_blocks, block_size, 8)[:, :, :7].view(torch.float8_e8m0fnu)
```

见 [tests/quant.py:62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L62)。`.view(..., 8)[:, :, :7]` 先按 8 字节槽位看、再切掉最后 1 字节填充，正好对应 kernel 配置里的 `NUM_SCALES = 8  // For MODEL1: 7 ... + 1 padding`：

```cpp
// csrc/sm90/decode/sparse_fp8/config.h:28-29
static constexpr int QUANT_TILE_SIZE = MODEL_TYPE == ModelType::V32 ? 128 : 64;
static constexpr int NUM_SCALES = MODEL_TYPE == ModelType::V32 ? 4 : 8;  // For MODEL1: 7 fp8_e4m3 + 1 padding
```

见 [csrc/sm90/decode/sparse_fp8/config.h:23-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L23-L29)。注意：`head_dim_k` 也随 `ModelType` 变（V32=576，MODEL1=512），tile 大小（128 vs 64）和 scale 数量（4 vs 8 槽位）一起联动。

C++ 接口层对 MODEL1 用了相同的 584 字节算式校验（[csrc/api/sparse_decode.h:293-295](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L293-L295)）：`448 + 64*2 + (448/64)*1 + 1 = 448 + 128 + 7 + 1 = 584`。而 `ModelType` 本身由 `d_qk` 在运行时推断（[csrc/api/sparse_decode.h:318-325](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L318-L325)），并写进 `SparseAttnDecodeParams.model_type`（[csrc/params.h:69](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L69)），驱动 kernel 选择正确的模板特化。

#### 4.4.4 代码实践

**实践目标**：对比 V32 与 MODEL1 两种布局，把差异固化成一张表。

**操作步骤**：

1. 读 [tests/quant.py:10-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L10-L15) 的 `get_meta`，把两种布局的 `(d, d_nope, d_rope, tile_size, num_tiles)` 抄下来。
2. 读 [tests/quant.py:36](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L36) 和 [tests/quant.py:56](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L56) 的 `bytes_per_token` 算式。
3. 完成下表：

| 维度 | V32 | MODEL1 |
|------|-----|--------|
| `d` / `d_qk` | 576 | 512 |
| `d_nope` | 512 | 448 |
| `d_rope` | 64 | 64 |
| `tile_size` | 128 | 64 |
| `num_tiles`（真 scale 数） | 4 | 7 |
| scale dtype | fp32 | fp8_e8m0 |
| scale 槽位字节数 | 16 | 8（7+1 pad） |
| 段落顺序 | NoPE→scale→RoPE | NoPE→RoPE→scale |
| 每 token 字节 | 656 | 584 |
| block padding | 无显式 padding | 对齐到 576 倍数 |

**需要观察的现象**：两种布局「同构但参数不同」——同样的「NoPE 量化 + RoPE 原样 + scale」三件套，只是维度划分、tile 粒度、scale 编码、段落顺序不同。

**预期结果**：你能不看代码复述这张表，并能解释「为什么 MODEL1 要把 scale 放最后、并做 block padding」——为了把 NoPE+RoPE 凑成 576 字节的对齐块、scale 紧随其后，方便 kernel 用对齐宽访问。

#### 4.4.5 小练习与答案

**练习 1**：MODEL1 的 scale 槽位是 8 字节，但只有 7 个真 scale，第 8 字节为什么不能省？

**答案**：8 字节是对齐的自然粒度（也是 e8m0 scale 与 fp8 数据混排时便于按 8 字节整体搬）。少 1 字节会破坏对齐，得不偿失；多 1 字节填充开销可忽略（584 中占 0.17%）。

**练习 2**：同一个 `quantize_k_cache` 函数，靠什么区分走 V32 还是 MODEL1 分支？

**答案**：靠传入的 `kvcache_layout: FP8KVCacheLayout` 枚举（[tests/quant.py:6-8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L6-L8)）。`get_meta()` 给出不同的 `(d, d_nope, d_rope, tile_size, num_tiles)`，函数内 `if kvcache_layout == V32_FP8Sparse / MODEL1_FP8Sparse` 二选一（[tests/quant.py:35](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L35) 与 [tests/quant.py:55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L55)）。而在 kernel 侧，对应的标签是 `ModelType`，由 `d_qk` 推断。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「布局理解 + 量化验证」的小任务：

**任务**：给定一段模拟的 bf16 KV cache，分别针对 V32 和 MODEL1 两种布局，回答三个问题——压缩比、scale 个数与编码、最大量化误差。

**步骤**：

1. 构造 `k = torch.randn(4, 128, 1, 576, dtype=bfloat16)`（V32 用）和 `k1 = torch.randn(4, 128, 1, 512, dtype=bfloat16)`（MODEL1 用，注意最后一维是 512）。
2. 用 `quantize_k_cache(k, V32_FP8Sparse)` 和 `quantize_k_cache(k1, MODEL1_FP8Sparse)` 分别量化，打印两者最后一维（应分别为 656、584，MODEL1 还会有 block padding，可用 `q1.element_size() * q1.numel()` 看实际字节数）。
3. 分别反量化，计算 NoPE 段（V32 前 512、MODEL1 前 448）的最大绝对误差，比较哪种布局误差更大，并猜测原因（提示：MODEL1 的 tile 更小、scale 更密，理论上误差应更小；但 scale 用 e8m0 的 2 的幂约束又可能抵消部分收益）。
4. 写一句话结论：**「压缩比 vs 量化精度」两种布局各偏向哪一端**。

**预期结果（待本地验证具体数值）**：

- V32：656 字节/token，4 个 fp32 scale，压缩比 ≈ 1.76x。
- MODEL1：584 字节/token（不含 block padding），7 个 e8m0 scale，压缩比 ≈ \(512 \times 2 / 584 \approx 1.75\)x（不含 padding；含 padding 后略降）。
- 两者压缩比接近，但 scale 编码与段落顺序不同，反映了「精度 vs 存储 vs 反量化代价」的不同取舍。

> 提示：若想顺手把本讲的量化结果真正喂给 kernel，可参考 `tests/test_flash_mla_sparse_decoding.py` 里 `RawTestParam(d_qk=576,...)` 与 `d_qk=512,...` 的用例（[tests/test_flash_mla_sparse_decoding.py:26-27](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L26-L27)），它们会触发 V32 / MODEL1 两条路径。完整端到端复现留待 u9-l3。

## 6. 本讲小结

- FlashMLA 的 FP8 KV cache 把每个 MLA token（576 或 512 维 bf16）压成 **656（V32）或 584（MODEL1）字节**，压缩比约 1.75x，缓解 128K 长上下文的显存压力。
- 采用 **tile 级（\(1\times128\) 或 \(1\times64\)）量化**：每段 NoPE 配一个 scale，`scale_inv = 2^{\lceil\log_2(\max/448)\rceil}` 向上取整到 2 的幂（UE8M0），保证量化值不溢出 fp8_e4m3 的 448 上限。
- **NoPE 量化、RoPE 不量化**：RoPE 段对精度敏感，原样以 bf16 保留，这是「拆分」的核心动机。
- **V32 vs MODEL1** 的差异集中在：`d_qk`（576/512）、tile 大小（128/64）、scale 编码（fp32/e8m0）、scale 数量（4/7+1pad）、段落顺序与 block padding。
- 接口层强制 sparse 解码走 FP8（`is_fp8_kvcache` 必须为真），并要求整个 block 连续（`stride(1)==bytes_per_token`）；`ModelType` 由 `d_qk` 在运行时推断，驱动 kernel 模板特化。
- `tests/quant.py` 是 KV cache 字节布局最权威、可运行的参考实现；`quantize_k_cache`/`dequantize_k_cache` 是验证布局与测量误差的直接工具。

## 7. 下一步学习建议

- **u5-l2（FP8→bf16 反量化与时钟周期分析）**：本讲只定义了「字节布局怎么存」，u5-l2 接着讲 kernel 把它「读出来反量化」要花多少时钟周期，并解释为什么 sparse decode kernel 是 **dequantization-bound**——那是 crossover 技术（u5-l3）出现的直接动机。
- **u5-l3（Crossover 与 DSM）**：在反量化成为瓶颈后，如何用 CTA cluster + 分布式共享内存让两个 CTA 各反量化一半再互换。
- 旁读建议：可先扫一眼 [csrc/sm90/decode/sparse_fp8/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h) 里 `QUANT_TILE_SIZE / NUM_SCALES / HEAD_DIM_K` 如何随 `ModelType` 联动，巩固「布局参数如何流进 kernel」的直觉。
