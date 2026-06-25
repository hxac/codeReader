# Mega MoE 的权重变换

## 1. 本讲目标

在 [u8-l1](u8-l1-mega-moe-symm-memory.md) 中，我们建立了「Mega MoE 把 dispatch + Linear1 + SwiGLU + Linear2 + combine 融合成一个 mega-kernel」的整体认知，也知道了激活（`x` / `l1_acts` / `l2_acts`）要拷进对称缓冲 `SymmBuffer`。

但我们刻意回避了一个问题：**权重（L1/L2 weight）需要做什么准备？** 答案是：在调用 mega-kernel 之前，用户必须先用 `transform_weights_for_mega_moe` 把权重重排成 mega-kernel 所期望的特殊布局。本讲就专门拆解这一步。

学完本讲，你应当能够：

- 理解 **gate/up 交错（interleave）** 的物理含义，并能解释它为什么是「把 SwiGLU 融合进 L1 epilogue」的前提。
- 掌握 **UTCCP SF 转置** 的 4×32 数学结构，并理解它与设备侧缩放因子（SF）索引映射的镜像关系。
- 看懂 **BF16** 与 **FP8×FP4** 两条路径在权重/SF 处理上的差异：L1/L2 各做什么、不做什么。
- 能够脱离 DeepGEMM 运行时，用纯 PyTorch 手动复现这两步变换并验证形状与数值。

本讲只聚焦「权重静态变换」，**不**涉及 mega-kernel 内部的 dispatch/compute/combine overlap 流水线（那是 [u8-l4](u8-l4-mega-moe-fused-overlap.md) 的内容）。

## 2. 前置知识

### 2.1 SwiGLU 与 gate/up 拼接

FFN（前馈网络）里常用的 SwiGLU 激活定义为：

\[
\text{out} = \big(\text{SiLU}(W_{\text{gate}}\,x)\big) \odot \big(W_{\text{up}}\,x\big), \qquad \text{SiLU}(z)=\frac{z}{1+e^{-z}}
\]

也就是说，同一个输入 \(x\) 要分别乘 **gate 权重**和 **up 权重**，再把 gate 分支过 SiLU 后与 up 分支逐元素相乘。工程上为了只做一次 GEMM，会把 gate 与 up 权重沿输出通道（N 维）拼成一个大矩阵：

\[
W_{L1} = [\,W_{\text{gate}}\,|\,W_{\text{up}}\,] \in \mathbb{R}^{(2N)\times K}
\]

于是 `L1` 的输出通道数是 `2 * intermediate_hidden`（前半是 gate、后半是 up）。这正是 mega.hpp 里 `intermediate_hidden_2 == 2 * intermediate_hidden` 这条断言的由来。

### 2.2 缩放因子（SF）与打包 UE8M0

这部分在 [u2-l2](u2-l2-scaling-factor-recipe-ue8m0.md) 已详细讲过，这里只回顾与本讲直接相关的三点：

1. FP4/FP8 范围窄，必须**逐块缩放**：每个块求 `amax`、除以最大可表示值（FP4 是 6.0、FP8 是 448.0）得到缩放因子 SF。
2. SM100 用 **UE8M0**（8 位无符号指数、0 位尾数，只表示 2 的幂）存 SF，并 **4 个打包进一个 `int32`**。
3. recipe `(gran_mn, gran_k)` 描述缩放粒度。Mega MoE 固定用 `recipe=(1, 1, 32)`（见 mega.hpp），即沿 MN 轴每 1 个、沿 K 轴每 32 个元素共用一个 SF。

> 术语提醒：本讲里「SF」就是「缩放因子」；「UE8M0」就是「8 位纯指数浮点」；「打包」就是「4 个 UE8M0 塞进 1 个 int32」。

### 2.3 TMEM 与 UTCCP（Blackwell/SM100 专属）

SM100（Blackwell）在 tensor core 之外新增了一块片上存储 **TMEM（tensor memory）**，新型 MMA（UMMA / tcgen05）的累加器以及**缩放因子**都放在这里。把 SF 从共享内存搬进 TMEM 有一条专用硬件指令，DeepGEMM 里叫它 **UTCCP**（源码注释写作「UTCCP 4x32 transpose」）。它会在搬运时对数据做一个 **4×32 的转置**。本讲要讲清楚：为了让这个 4×32 硬件转置之后 SF 仍落在 TMEM 的正确位置，宿主侧必须先把权重 SF 预先做一次对应的 4×32 转置——这就是 `_transpose_sf_for_utccp` 的全部动机。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [deep_gemm/mega/__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py) | **本讲主战场**。`_interleave_weights`、`_transpose_sf_for_utccp`、`transform_weights_for_mega_moe` 三个函数都在这里。 |
| [deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh) | 设备侧 mega-kernel。用来「反向印证」：交错布局如何被 SwiGLU epilogue 消费、UTCCP 如何把 SF 搬进 TMEM、`transform_sf_token_idx` 如何与宿主转置对齐。 |
| [csrc/apis/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) | `transform_sf_into_required_layout`：把用户 SF 变换成 TMA 友好的 MN-major + 打包 UE8M0 布局。是 `_transpose_sf_for_utccp` 的**上游**。 |
| [csrc/utils/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp) | `check_sf_layout`：SF 形状/步长/对齐校验，揭示「MN-major」契约。 |
| [csrc/apis/mega.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp) | mega 入口的权重/SF 断言，固定 `kGranMN=1, kGranK=32`。 |
| [deep_gemm/utils/math.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py) | `per_token_cast_to_fp4`、`ceil_to_ue8m0`、`pack_ue8m0_to_int`：理解 SF 形状怎么来的。 |
| [tests/test_mega_moe.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py) | 端到端用例：`_cast_weights_to_fp4` + `transform_weights_for_mega_moe` 的标准调用姿势。 |

## 4. 核心概念与源码讲解

### 4.1 gate/up 交错：为融合 SwiGLU 重排权重

#### 4.1.1 概念说明

「gate | up」这种「前半 gate、后半 up」的拼接方式，对**普通的、不融合的**实现很友好：算完 GEMM 得到 `[2N]` 输出，再切两半做 SwiGLU 即可。但 Mega MoE 要把 SwiGLU **就地融合进 L1 GEMM 的 epilogue**（即累加结果刚写出来、还在寄存器/TMEM 里时就顺手把 SwiGLU 算掉），这种融合要求 **gate 和它的 up 配对在物理上紧挨着**，这样一条 `SM100_TMEM_LOAD` 指令就能一次读出 `(gate, up)` 一对。

为此，`_interleave_weights` 把 `[gate | up]` 重排成「**小块交替**」：

\[
\underbrace{[\,g_0\ldots g_7\,|\,u_0\ldots u_7\,|\,g_8\ldots g_{15}\,|\,u_8\ldots u_{15}\,|\,\cdots\,]}_{\text{每 8 个一组，gate 块与 up 块交替}}
\]

默认粒度 `gran=8`，正好对应设备侧 TMEM 加载原子 `SM100_TMEM_LOAD_16dp256b1x` 一次吐出的「gate/up 对」宽度（256 bit = 8 个 float）。

> 直觉一句话：**交错不是改变数值，而是改 N 维的物理排列顺序**，让硬件按 N 顺序流式产出时，相邻两个 8-通道块恰好是同一对 (gate, up)。

#### 4.1.2 核心流程

给定权重张量 `t` 形状 `[g, n, *rest]`（`g`=expert 数，`n=2*half`，gate 占前 `half`、up 占后 `half`）：

1. 把 gate 切片 `t[:, :half]` 与 up 切片 `t[:, half:]` 各自 reshape 成 `[g, half//gran, gran, *rest]`——即沿 N 轴切成若干个 `gran` 大小的块。
2. 在新维度 2 上 `stack([gate, up])`，得到 `[g, half//gran, 2, gran, *rest]`，即每个「槽」里先 gate 块后 up 块。
3. `reshape` 回 `[g, n, *rest]`：合并成 `[gate_0..7, up_0..7, gate_8..15, up_8..15, ...]`。

#### 4.1.3 源码精读

[`_interleave_weights` — deep_gemm/mega/__init__.py:115-121](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L115-L121) 做的就是上面三步。注意第 116 行的注释精准描述了目标排列：

```python
def _interleave_weights(t: torch.Tensor, gran: int = 8) -> torch.Tensor:
    # [gate: 0..7, up: 0..7, gate: 8..15, up: 8..15, ...] instead of [gate | up]
    g, n, *rest = t.shape
    half = n // 2
    gate = t[:, :half].reshape(g, half // gran, gran, *rest)
    up = t[:, half:].reshape(g, half // gran, gran, *rest)
    return torch.empty_like(t).copy_(torch.stack([gate, up], dim=2).reshape(g, n, *rest))
```

**关键细节**：返回值用 `torch.empty_like(t).copy_(...)` 而非直接返回 `reshape` 的结果。这是因为 `stack` 后再 `reshape` 得到的张量**不连续**（中间夹了 `transpose` 等价的重排），`copy_` 强制落盘成 contiguous 的物理布局——这一点很重要，因为 mega.hpp 在入口断言 `l1_weights.is_contiguous()`（[csrc/apis/mega.hpp:202](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L202)），TMA 也要求连续。

**反向印证（设备侧消费）**：在 mega-kernel 的 L1 epilogue 里，[`sm100_fp8_fp4_mega_moe.cuh:944-945`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L944-L945) 的注释写明「SwiGLU in-place using granularity 8 interleaved weights」。随后 [`:993-1019`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L993-L1019) 直接从 MMA 累加器（TMEM load 后的 `fp32_values`）按「奇偶成对」读出 gate 与 up：

```cpp
// Apply SwiGLU: silu(gate) * up
auto fp32_values = reinterpret_cast<float2*>(raw_values);
for (uint32_t k = 0; k < 2; ++ k) {
    auto bf16_gate = __float22bfloat162_rn(fp32_values[k * 2 + 0]);  // gate 在偶位
    auto bf16_up   = __float22bfloat162_rn(fp32_values[k * 2 + 1]);  // up  在奇位
    // ... clamp ...
    // SwiGLU: gate = gate / (1 + exp(-gate));  out = gate * up * topk_weight
    ...
    activation_values[i][k] = __fmul2_rn(__fmul2_rn(gate, up), weights);
}
```

`k * 2 + 0` 恒为 gate、`k * 2 + 1` 恒为 up——这正是 8-粒度交错保证的：相邻两块恰好是同一对 (gate, up)。如果权重仍是 `[gate | up]`，这段代码就会把两个 gate（或两个 up）配到一起，结果全错。

#### 4.1.4 代码实践

**实践目标**：用 CPU 上的小张量验证「交错 = 纯 N 维排列重排，数值不变」，并手写一个等价的逐元素索引映射做对照。

**操作步骤**（示例代码，可直接在任意带 PyTorch 的环境运行，无需编译 DeepGEMM）：

```python
# 示例代码：把源码里的纯 PyTorch 函数原样复制下来，脱离 _C 也能跑
import torch

def _interleave_weights(t, gran=8):
    g, n, *rest = t.shape
    half = n // 2
    gate = t[:, :half].reshape(g, half // gran, gran, *rest)
    up   = t[:, half:].reshape(g, half // gran, gran, *rest)
    return torch.empty_like(t).copy_(torch.stack([gate, up], dim=2).reshape(g, n, *rest))

# 1) 构造 mock L1 权重 [g=2, n=32(=2*16), k=8]，n 必须是 2*gran 的倍数
torch.manual_seed(0)
w = torch.arange(2 * 32 * 8).reshape(2, 32, 8)
out = _interleave_weights(w, gran=8)

# 2) 手写等价索引映射：原 N=j 属于 gate(0..15) 还是 up(16..31)？
def ref_interleave(t, gran=8):
    g, n, *rest = t.shape
    half = n // 2
    res = torch.empty_like(t)
    for gi in range(g):
        for new_j in range(n):
            chunk = new_j // (2 * gran)         # 第几个 [gate,up] 大块
            pos   = new_j % (2 * gran)          # 大块内位置
            if pos < gran:                       # 前半 → gate
                src_j = chunk * gran + pos
                src = t[gi, src_j]               # 来自 gate 段（前 half）
            else:                                # 后半 → up
                src_j = chunk * gran + (pos - gran)
                src = t[gi, half + src_j]        # 来自 up 段
            res[gi, new_j] = src
    return res

assert torch.equal(out, ref_interleave(w))
print("interleave OK, shape", tuple(out.shape))
print("gate chunk [0:8]  =", out[0, 0:8, 0].tolist())   # 应等于原 gate 前 8 行
print("up   chunk [8:16] =", out[0, 8:16, 0].tolist())  # 应等于原 up 前 8 行
```

**需要观察的现象**：
- 输出形状不变（仍 `[2, 32, 8]`）。
- 输出 `[0:8]` 段等于输入 gate 段 `w[0, 0:8]`；`[8:16]` 段等于输入 up 段 `w[0, 16:24]`。
- `assert torch.equal(out, ref_interleave(w))` 通过。

**预期结果**：交错后 N 维顺序变为 `[gate0..7, up0..7, gate8..15, up8..15]`，集合相同（数值集合不变）、仅顺序变化。

### 4.2 UTCCP SF 转置：让缩放因子喂进 TMEM

#### 4.2.1 概念说明

FP8×FP4 路径下，权重是 FP4、其 SF 是打包 UE8M0（`int32`）。设备侧做 scaled MMA 时，要用 UTCCP 指令把 SF 从共享内存搬进 TMEM。UTCCP 这条硬件指令在搬运过程中会对一个 **4×32** 的小块做转置。为了让「搬进 TMEM 之后」SF 仍对齐到正确的通道，宿主侧必须先在全局内存里把权重 SF 按 **每个 128 元素块内做 4×32 转置** 预先排好。

> 为什么是 128？因为 `4 × 32 = 128`，这是 UTCCP 一次处理的基本单元，源码里写作 `kNumUTCCPAlignedElems = 128`，并要求 `SF_BLOCK_M` 对齐到 128（见下文源码）。

注意：这一步**只对权重 SF 做**。原因是权重是静态的（模型加载后不变），值得一次性把转置烘焙进数据里；而激活 SF（`l1_acts_sf`/`l2_acts_sf`）是动态的，设备侧改用**读时索引映射** `transform_sf_token_idx` 来表达同一个 4×32 置换（详见 4.2.3，两者数学完全一致）。

#### 4.2.2 核心流程（4×32 转置的数学）

设权重 SF 形状为 `[num_groups, mn, packed_sf_k]`（`mn` = 输出通道数、`packed_sf_k = k // (gran_k · 4)`，因为 4 个 UE8M0 打包进一个 int32）。函数要求 `mn % 128 == 0`。

对每一个 128 大小的 `mn` 段，把它视为一个 `4 × 32` 的矩阵（按行优先铺平：行号 `a = idx // 32`、列号 `b = idx % 32`），然后**转置**成 `32 × 4`。段内逻辑下标 `idx ∈ [0, 128)` 映射到新的物理位置：

\[
\text{new\_idx} = (idx \bmod 32) \times 4 + \big\lfloor idx / 32 \big\rfloor
\]

源码用 `reshape + transpose + reshape` 三连实现这一置换：

```python
result = (sf.reshape(num_groups, -1, 4, 32, packed_sf_k)   # mn 拆成 (mn//128, 4, 32)
            .transpose(2, 3)                               # 4<->32 转置
            .reshape(num_groups, mn, packed_sf_k))         # 合并回去
```

#### 4.2.3 源码精读

**宿主侧**：[`_transpose_sf_for_utccp` — deep_gemm/mega/__init__.py:124-130](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L124-L130)

```python
def _transpose_sf_for_utccp(sf: torch.Tensor) -> torch.Tensor:
    num_groups, mn, packed_sf_k = sf.shape
    assert sf.dtype == torch.int and mn % 128 == 0
    result = (sf.reshape(num_groups, -1, 4, 32, packed_sf_k)
                .transpose(2, 3)
                .reshape(num_groups, mn, packed_sf_k))
    return torch.empty_like(sf).copy_(result)
```

两个断言很关键：`sf.dtype == torch.int`（必须是打包 UE8M0 的 int32，不是 FP32）、`mn % 128 == 0`（UTCCP 对齐要求）。同样用 `empty_like(...).copy_(...)` 落成 contiguous 布局。

**设备侧的镜像**：[`sm100_fp8_fp4_mega_moe.cuh:125-134`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L125-L134)

```cpp
constexpr uint32_t kNumUTCCPAlignedElems = 128;
DG_STATIC_ASSERT(SF_BLOCK_M == math::constexpr_align(BLOCK_M, kNumUTCCPAlignedElems), "Invalid SF_BLOCK_M");
...
// UTCCP 4x32 transpose index mapping within each 128-element group
const auto transform_sf_token_idx = [](const uint32_t& token_idx_in_expert) {
    const uint32_t idx = token_idx_in_expert % BLOCK_M;
    return token_idx_in_expert / BLOCK_M * SF_BLOCK_M +
           (idx & ~127u) + (idx & 31u) * 4 + ((idx >> 5) & 3u);
};
```

把位运算翻译成算术：对 `idx ∈ [0,128)`，`(idx & ~127u)=0`，`(idx & 31u)*4 = (idx % 32)*4`，`((idx >> 5) & 3u) = idx // 32`。合起来正是：

\[
(idx \bmod 32)\times 4 + \big\lfloor idx / 32 \big\rfloor
\]

**与 4.2.2 的公式逐字符相同**——这就是「宿主转置」与「设备索引映射」描述同一个 4×32 置换的铁证。区别只在落地方式：权重在宿主侧烘焙进数据，激活在设备侧用这个 lambda 在寻址时即时换算。

**UTCCP 的实际消费点**：[`sm100_fp8_fp4_mega_moe.cuh:833-846`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L833-L846)

```cpp
// UTCCP copy SFA and SFB to TMEM
using cute_utccp_t = cute::SM100_UTCCP_4x32dp128bit_2cta;
for (uint32_t i = 0; i < SF_BLOCK_M / kNumUTCCPAlignedElems; ++ i) {
    auto smem_ptr = shared_storage.smem_sfa[stage_idx]
                  + umma_k_block_idx * SF_BLOCK_M + i * kNumUTCCPAlignedElems;
    mma::sm100::replace_smem_desc_addr(sf_desc, smem_ptr);
    cute_utccp_t::copy(sf_desc, kTmemStartColOfSFA + i * 4);   // 注意目标 TMEM 列 += 4
}
// SFB 同理 ...
```

`SM100_UTCCP_4x32dp128bit_2cta` 即「4×32、128 位深、2-CTA 版」的 UTCCP 指令；它每轮吃 128 个 SF 元素，写到 TMEM 的 `kTmemStartColOfSFA + i*4` 列。因为宿主已把权重 SF 预转置过，这条「搬+转」指令之后 SF 就稳稳落在 scaled UMMA 要读的 TMEM 列上。

#### 4.2.4 代码实践

**实践目标**：手写 `_transpose_sf_for_utccp` 的逐元素等价实现，验证 `(idx%32)*4 + (idx//32)` 这个置换（示例代码，CPU 可跑）。

```python
# 示例代码
import torch

def _transpose_sf_for_utccp(sf):
    num_groups, mn, packed_sf_k = sf.shape
    assert sf.dtype == torch.int and mn % 128 == 0
    result = (sf.reshape(num_groups, -1, 4, 32, packed_sf_k)
                .transpose(2, 3).reshape(num_groups, mn, packed_sf_k))
    return torch.empty_like(sf).copy_(result)

def ref_utccp(sf):
    # 逐元素实现设备侧 transform_sf_token_idx 的同一个置换
    num_groups, mn, packed_sf_k = sf.shape
    res = torch.empty_like(sf)
    for g in range(num_groups):
        for base in range(0, mn, 128):                 # 每个 128-段
            for idx in range(128):
                new = (idx % 32) * 4 + (idx // 32)     # 与设备 lambda 完全一致
                res[g, base + new] = sf[g, base + idx]
    return res

# mock SF：[num_groups=1, mn=256, packed_sf_k=3]，mn 必须是 128 的倍数
sf = torch.arange(1 * 256 * 3, dtype=torch.int).reshape(1, 256, 3)
out = _transpose_sf_for_utccp(sf)
assert torch.equal(out, ref_utccp(sf))
print("UTCCP transpose OK, shape", tuple(out.shape))
# 看 128-段内前几个位置的置换：idx=0->0, idx=1->4, idx=32->1, idx=33->5
print("src[0,0], src[0,1], src[0,32], src[0,33] =",
      sf[0, 0, 0].item(), sf[0, 1, 0].item(), sf[0, 32, 0].item(), sf[0, 33, 0].item())
print("dst[0,0], dst[0,4], dst[0,1], dst[0,5] =",
      out[0, 0, 0].item(), out[0, 4, 0].item(), out[0, 1, 0].item(), out[0, 5, 0].item())
```

**需要观察的现象**：
- `assert` 通过，形状不变。
- `src[0,32]` 的值跑到了 `dst[0,1]`（因为 `(32%32)*4 + 32//32 = 0 + 1 = 1`）；`src[0,1]` 跑到 `dst[0,4]`（`(1%32)*4 + 0 = 4`）。

**预期结果**：置换与设备侧 `transform_sf_token_idx` 逐位一致，证明宿主烘焙 == 设备寻址换算。

#### 4.2.5 小练习与答案

**练习 1**：若 `mn = 256`，第 100 个元素（`idx=100`）转置后落到哪个位置？
**答**：\((100 \bmod 32)\times 4 + \lfloor 100/32 \rfloor = 4 \times 4 + 3 = 19\)。即 `dst[100] ← src[19]`，等价地 `src[100] → dst[19]`。

**练习 2**：为什么 `_transpose_sf_for_utccp` 要断言 `mn % 128 == 0`？如果只断言 `mn % 32 == 0` 会怎样？
**答**：因为 UTCCP 的基本搬运单元是 `4×32 = 128` 个元素（`kNumUTCCPAlignedElems = 128`），且设备侧 `SF_BLOCK_M` 必须对齐到 128。若只对齐到 32，段尾会出现不完整的 4×32 块，UTCCP 无法整块搬运，SF 就无法正确落进 TMEM。

### 4.3 BF16 与 FP8×FP4 两条路径的差异

#### 4.3.1 概念说明

`transform_weights_for_mega_moe` 的分支依据是**入参类型**：权重以 `tuple` 传入（`(weight, sf)`）走 FP8×FP4 路径，以单个 `Tensor` 传入走 BF16 路径。两条路径对 L1/L2 的处理并不对称，关键差异有三：

| 处理项 | BF16 路径 | FP8×FP4 路径 |
| --- | --- | --- |
| L1 weight | 交错 gate/up | 交错 gate/up |
| L1 SF | ——（无 SF） | 先交错 gate/up，再 UTCCP 转置 |
| L2 weight | **不变** | **不变** |
| L2 SF | —— | UTCCP 转置（**不**交错） |

为什么 L2 永远不交错、却仍要（在 FP8 路径下）转置 SF？

- **L2 不交错**：L2 之后没有 SwiGLU（它是 FFN 的最后一层，直接出 BF16 输出 `y`），输出通道不存在 gate/up 配对需求。
- **L2 SF 仍转置**：L2 同样是 FP4 权重，其 SF 同样要经 UTCCP 搬进 TMEM，所以同样需要 4×32 预转置；只是没有 gate/up 拆分，所以不需要先交错。
- **L1 SF 要先交错再转置**：因为 L1 weight 先做了 gate/up 交错，SF 的 mn 维（对应输出通道）必须跟着交错，才能与 weight 的物理排列保持逐块对齐；对齐之后再做 UTCCP 转置。

#### 4.3.2 核心流程

`transform_weights_for_mega_moe(l1_weights, l2_weights)` 的判定与派发：

```
if l1_weights 是 tuple:            # FP8×FP4
    L1_w  = interleave(l1[0])
    L1_sf = utccp_transpose(interleave(l1[1]))   # 先交错、后转置
    L2    = (l2[0], utccp_transpose(l2[1]))      # 只转置 SF
else:                              # BF16
    L1 = interleave(l1)                          # 只交错
    L2 = l2                                      # 完全不变
```

#### 4.3.3 源码精读

[`transform_weights_for_mega_moe` — deep_gemm/mega/__init__.py:133-151](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L133-L151)

```python
def transform_weights_for_mega_moe(l1_weights, l2_weights, activation='swiglu'):
    assert activation == 'swiglu', ...
    if isinstance(l1_weights, tuple):
        # FP8: interleave gate/up for weight and SF, then transpose L1 SF for UTCCP
        l1_w  = _interleave_weights(l1_weights[0])
        l1_sf = _transpose_sf_for_utccp(_interleave_weights(l1_weights[1]))
        l1_transformed = (l1_w, l1_sf)
        # L2: only transpose SF for UTCCP
        l2_transformed = (l2_weights[0], _transpose_sf_for_utccp(l2_weights[1]))
    else:
        # BF16: L1 interleave gate/up, L2 unchanged
        l1_transformed = _interleave_weights(l1_weights)
        l2_transformed = l2_weights
    return l1_transformed, l2_transformed
```

注意 L1 SF 那一行的**顺序**：`_transpose_sf_for_utccp(_interleave_weights(...))`——先交错、后转置。这是因为转置是「按 128-段的物理位置」操作的，必须等交错把 SF 重排成与 weight 相同的物理顺序后，再做 4×32 转置，两者不可交换。

**上游契约（SF 形状从哪来）**：在测试里，[`tests/test_mega_moe.py:62-69`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L62-L69) 的 `_cast_weights_to_fp4` 先用 `per_token_cast_to_fp4(..., use_ue8m0=True, gran_k=32)` 得到 FP4 weight 与 FP32 SF，再 `transform_sf_into_required_layout(w_sf, n, k, (1, 32), num_groups)` 把 SF 变成 **MN-major 的打包 UE8M0（int32）**。只有经过这一步，SF 的 `dtype` 才是 `torch.int`、`mn` 才满足 `% 128 == 0`，`_transpose_sf_for_utccp` 的断言才能通过。

```python
def _cast_weights_to_fp4(bf16_weights):
    num_groups, n, k = bf16_weights.shape
    w = torch.empty((num_groups, n, k // 2), device='cuda', dtype=torch.int8)       # FP4 打包
    w_sf = torch.empty((num_groups, n, k // 32), device='cuda', dtype=torch.float)  # 先 FP32
    for i in range(num_groups):
        w[i], w_sf[i] = per_token_cast_to_fp4(bf16_weights[i], use_ue8m0=True, gran_k=32)
    w_sf = deep_gemm.transform_sf_into_required_layout(w_sf, n, k, (1, 32), num_groups)  # → int32/MN-major
    return w, w_sf
```

`transform_sf_into_required_layout` 在 SM100 上走的是 [`csrc/apis/layout.hpp:48-58`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp#L48-L58) 的「FP32+gran_k=32 → 打包 UE8M0、MN-major、TMA 对齐」分支，由 `get_mn_major_tma_aligned_packed_ue8m0_tensor` 完成。这正好是 [u2-l2](u2-l2-scaling-factor-recipe-ue8m0.md) 讲过的 SF 变换——本讲只是它的**下游特化**：在通用 SF 布局之上，再叠加一层 UTCCP 4×32 转置。

**下游契约（mega.hpp 校验）**：变换后的权重喂给 mega-kernel 前，[`csrc/apis/mega.hpp:204-209`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L204-L209) 用 `check_sf_layout(..., kGranMN=1, kGranK=32, ..., torch::kInt)` 复核 SF 是 MN-major、TMA 对齐的 int32：

```cpp
constexpr int kGranMN = 1, kGranK = 32;
check_sf_layout(l1_weights_sf, intermediate_hidden * 2, hidden, kGranMN, kGranK,
                num_experts_per_rank, true, false, torch::kInt);   // tma_stride_check=true
check_sf_layout(l2_weights_sf, hidden, intermediate_hidden, kGranMN, kGranK,
                num_experts_per_rank, true, false, torch::kInt);
```

注意 L1 的 `mn = intermediate_hidden * 2`（gate+up），L2 的 `mn = hidden`——这解释了为什么 L1 SF 必须先交错（它的 mn 轴对应 `2*intermediate_hidden` 个通道、有 gate/up 之分），而 L2 SF 的 mn 轴对应 `hidden`、无 gate/up、不需交错。

#### 4.3.4 代码实践

**实践目标**：复现 `transform_weights_for_mega_moe` 的两条分支，用 mock 数据验证「L1 交错+转置、L2 仅转置」与「BF16 仅 L1 交错」的差异（示例代码，CPU 可跑）。

```python
# 示例代码
import torch
# 复用 4.1.4、4.2.4 里定义的 _interleave_weights / _transpose_sf_for_utccp

def transform_weights_for_mega_moe(l1_weights, l2_weights):
    if isinstance(l1_weights, tuple):                       # FP8×FP4
        l1_w  = _interleave_weights(l1_weights[0])
        l1_sf = _transpose_sf_for_utccp(_interleave_weights(l1_weights[1]))
        l2 = (l2_weights[0], _transpose_sf_for_utccp(l2_weights[1]))
        return (l1_w, l1_sf), l2
    else:                                                   # BF16
        return _interleave_weights(l1_weights), l2_weights

# --- FP8×FP4 分支 ---
G, N1, K1 = 1, 256, 128          # L1: N1=2*intermediate_hidden, 需 %128 且 %16
l1_w  = torch.zeros((G, N1, K1 // 2), dtype=torch.int8)     # mock FP4 packed
l1_sf = torch.zeros((G, N1, K1 // 128), dtype=torch.int)    # packed SF: k//(gran_k*4)
l2_w  = torch.zeros((G, 64, 32), dtype=torch.int8)          # L2 weight
l2_sf = torch.zeros((G, 64, 1), dtype=torch.int)            # L2 SF, mn=64 不是128的倍数 → 会断言失败!
```

**需要观察的现象与思考**（这一步重在源码阅读，**不**建议硬跑断言失败的分支）：
- 上面 mock 故意把 L2 的 `mn=64` 设成非 128 倍数。运行会触发 `_transpose_sf_for_utccp` 的 `assert mn % 128 == 0` 失败。
- 这正好对应真实约束：mega.hpp 里 [`hidden % 128 == 0 and intermediate_hidden % 128 == 0`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L115)——L2 的 `mn=hidden` 必须 `% 128 == 0`。把 L2 `mn` 改成 128 即可通过。
- 通过后检查：L1 SF 的总元素数不变（纯置换）、L2 weight 与输入逐元素相等（`torch.equal`）、L2 SF 仅做 4×32 转置。

**预期结果**：把 L2 `mn` 改为 128 后，FP8 分支返回 `(l1_w, l1_sf)`、`(l2_w, l2_sf_t)`，其中 `l2_w` 未变、`l2_sf_t` 是 `l2_sf` 的 4×32 转置；BF16 分支则只对 L1 交错、L2 原样返回。

> 若无法本地运行（如无 PyTorch），上述结论标注为「待本地验证」，但 `_interleave_weights`/`_transpose_sf_for_utccp` 是纯 PyTorch、CPU 可跑，通常无需特殊环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么 L1 SF 是「先交错再转置」，而不能「先转置再交错」？
**答**：4×32 转置是按**当前物理位置**的 128-段操作的；交错改变了 mn 轴的物理排列。若先转置再交错，转置作用在的是未交错的排列上，与 weight 交错后的排列错位，SF 与 weight 不再逐块对齐，结果错误。两者不可交换。

**练习 2**：BF16 路径下 L2 完全不变，是否意味着 BF16 的 L2 不需要任何预处理就能直接喂给 mega-kernel？
**答**：是的。BF16 无 SF、L2 之后无 SwiGLU，故 L2 既不需要交错也不需要转置，`transform_weights_for_mega_moe` 对它直接 `return l2_weights`。L1 仍需交错以支持融合 SwiGLU。

**练习 3**：假如某个模型的 FFN 把 SwiGLU 换成了不带 gate/up 结构的 ReLU，`_interleave_weights` 还需要调用吗？
**答**：不需要。交错存在的唯一理由是「把 gate/up 配对以便融合 SwiGLU」。若激活不再有 gate/up 配对（如纯 ReLU 单分支），L1 输出通道没有 gate/up 之分，交错反而会破坏通道顺序。不过当前 `transform_weights_for_mega_moe` 硬断言 `activation == 'swiglu'`，尚不支持其它激活。

## 5. 综合实践

**任务**：完整走一遍「BF16 权重 → FP4 权重 + SF → mega 友好布局」的宿主侧流水线（不含真正量化，重在布局变换链），并用断言验证每一步的形状/类型契约。这是把本讲三个最小模块（gate/up 交错、UTCCP SF 转置、两条路径差异）串起来的收尾任务。

```python
# 示例代码（CPU，无需编译 DeepGEMM）
import torch

# 1) 两个变换函数（原样取自源码）
def _interleave_weights(t, gran=8):
    g, n, *rest = t.shape
    half = n // 2
    gate = t[:, :half].reshape(g, half // gran, gran, *rest)
    up   = t[:, half:].reshape(g, half // gran, gran, *rest)
    return torch.empty_like(t).copy_(torch.stack([gate, up], dim=2).reshape(g, n, *rest))

def _transpose_sf_for_utccp(sf):
    num_groups, mn, packed_sf_k = sf.shape
    assert sf.dtype == torch.int and mn % 128 == 0
    result = (sf.reshape(num_groups, -1, 4, 32, packed_sf_k).transpose(2, 3)
                .reshape(num_groups, mn, packed_sf_k))
    return torch.empty_like(sf).copy_(result)

def transform_weights_for_mega_moe(l1_weights, l2_weights):
    if isinstance(l1_weights, tuple):
        l1 = (_interleave_weights(l1_weights[0]),
              _transpose_sf_for_utccp(_interleave_weights(l1_weights[1])))
        l2 = (l2_weights[0], _transpose_sf_for_utccp(l2_weights[1]))
    else:
        l1 = _interleave_weights(l1_weights)
        l2 = l2_weights
    return l1, l2

# 2) 构造一组「合法」的 mock 配置（满足 mega.hpp 的对齐约束）
G = 2
hidden = 128            # 必须 %128==0
intermediate_hidden = 128
# L1: [G, 2*intermediate_hidden, hidden/2] packed FP4 ; SF: [G, 2*intermediate_hidden, hidden/128]
l1_w  = torch.randint(-128, 127, (G, 2 * intermediate_hidden, hidden // 2), dtype=torch.int8)
l1_sf = torch.randint(1, 1 << 30, (G, 2 * intermediate_hidden, hidden // 128), dtype=torch.int)
# L2: [G, hidden, intermediate_hidden/2] ; SF: [G, hidden, intermediate_hidden/128]
l2_w  = torch.randint(-128, 127, (G, hidden, intermediate_hidden // 2), dtype=torch.int8)
l2_sf = torch.randint(1, 1 << 30, (G, hidden, intermediate_hidden // 128), dtype=torch.int)

# 3) 跑 FP8×FP4 分支
(l1t_w, l1t_sf), (l2t_w, l2t_sf) = transform_weights_for_mega_moe((l1_w, l1_sf), (l2_w, l2_sf))

# 4) 逐契约校验
assert l1t_w.shape  == l1_w.shape  and l1t_w.dtype  == torch.int8
assert l1t_sf.shape == l1_sf.shape and l1t_sf.dtype == torch.int   # 形状不变、纯置换
assert torch.equal(l2t_w, l2_w)                                    # L2 weight 不变
assert l2t_sf.shape == l2_sf.shape and not torch.equal(l2t_sf, l2_sf)  # L2 SF 被转置
# 数值集合不变（交错/转置都是置换）
assert torch.sort(l1t_w.flatten()).values.equal(torch.sort(l1_w.flatten()).values)
assert torch.sort(l1t_sf.flatten()).values.equal(torch.sort(l1_sf.flatten()).values)
print("FP8×FP4 pipeline contracts all satisfied.")

# 5) 再跑 BF16 分支做对比
l1_bf = torch.randn(G, 2 * intermediate_hidden, hidden)
l2_bf = torch.randn(G, hidden, intermediate_hidden)
l1t, l2t = transform_weights_for_mega_moe(l1_bf, l2_bf)
assert torch.equal(l2t, l2_bf)                 # BF16 的 L2 完全不变
assert not torch.equal(l1t, l1_bf)             # BF16 的 L1 被交错
print("BF16 pipeline contracts all satisfied.")
```

**预期结果**：所有断言通过；两条路径的差异（L1 交错、L2 仅在 FP8 下转置 SF）以断言形式被精确刻画。若你在真实 SM100 环境，可进一步把 `l1t/l2t` 喂给 `deep_gemm.fp8_fp4_mega_moe`（参考 [tests/test_mega_moe.py:99-120](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L99-L120) 的调用姿势）跑端到端 correctness。

## 6. 本讲小结

- **gate/up 交错**（`_interleave_weights`，粒度 8）把 `[gate | up]` 重排成 `[gate₀..₇, up₀..₇, gate₈..₁₅, up₈..₁₅, …]`，是「把 SwiGLU 融合进 L1 epilogue」的物理前提——设备侧按奇偶成对读出 gate/up 并就地算 `silu(gate)*up`。
- **UTCCP SF 转置**（`_transpose_sf_for_utccp`）在每个 128 元素段内做 4×32 转置，置换式为 \((idx\bmod 32)\times 4 + \lfloor idx/32\rfloor\)；它与设备侧 `transform_sf_token_idx` 逐位一致，使 UTCCP 硬件指令能把权重 SF 正确搬进 TMEM 供 scaled UMMA 读取。
- **两条路径差异**：BF16 只对 L1 交错、L2 不变；FP8×FP4 对 L1 权重与 SF 都交错（SF 再转置），对 L2 仅转置 SF、权重不变。根因是 SwiGLU 只发生在 L1 之后，而 SF 转置只取决于「是否用 UTCCP」。
- 变换链是 **`per_token_cast_to_fp4` → `transform_sf_into_required_layout`（MN-major 打包 UE8M0）→ `transform_weights_for_mega_moe`（交错 + UTCCP 转置）**，最终由 mega.hpp 的 `check_sf_layout` 复核。
- 所有变换都是**纯排列、数值不变**，且都通过 `empty_like(...).copy_(...)` 落成 contiguous 布局，以满足 TMA 与 `is_contiguous()` 断言。

## 7. 下一步学习建议

- 本讲把权重布局准备到位后，下一步自然是看 mega-kernel **内部**如何用这些布局：建议进入 [u8-l3 Mega MoE 调度器与 wave 调度](u8-l3-mega-moe-wave-scheduler.md)，理解 `MegaMoEScheduler` 的 BlockPhase 状态机与 2-CTA cluster 约束。
- 之后阅读 [u8-l4 融合 mega 内核与通信重叠](u8-l4-mega-moe-fused-overlap.md)，重点对照 [`sm100_fp8_fp4_mega_moe.cuh:833-846`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L833-L846) 的 UTCCP 消费点与本讲的宿主转置，把「宿主烘焙 ↔ 设备消费」的闭环彻底打通。
- 若想加深对 SF 上游布局的理解，可回顾 [u2-l2 缩放因子 recipe 与 UE8M0 打包](u2-l2-scaling-factor-recipe-ue8m0.md) 与 [u4-l2 TMA 描述符与 swizzle](u4-l2-tma-descriptors-swizzle.md)。
