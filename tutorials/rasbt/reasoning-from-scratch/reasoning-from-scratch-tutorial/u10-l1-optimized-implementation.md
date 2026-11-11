# GPU 优化实现：SDPA 与预分配 KV Cache

## 1. 本讲目标

本讲进入单元十的工程实践专题，精读 `reasoning_from_scratch/qwen3_optimized.py`——标准实现 `qwen3.py` 的「GPU 友好版 drop-in 替换」。学完本讲，你应该能够：

1. 说出 `torch.nn.functional.scaled_dot_product_attention`（SDPA）融合内核相对手写注意力（matmul → masked_fill → softmax → matmul）在内存与速度上的优势来源。
2. 理解预分配式 `KVCache` 的 `allocate / append / view` 三步生命周期，以及「用显存换吞吐」的取舍（H100 上 5.85 GB 对 1.52 GB）。
3. 解释 `Qwen3Model(cfg, exact=False)` 默认值的含义：默认允许 SDPA 自选融合后端、容忍 bf16 舍入级差异；`exact=True` 配合 `sdpa_exact()` 强制 MATH 后端以换取与标准版逐步一致。
4. 会用 `ch02/03_optimized-LLM/compare_inference.py` 在自己的硬件上实测两版实现的 tokens/sec 与峰值显存，并据此选择实现（README 的结论：CPU 用标准版 + cache + compile，GPU 用优化版 + cache + compile）。

## 2. 前置知识

本讲假设你已完成 u2-l5（KV Cache 与因果掩码）和 u3-l3（torch.compile）。在此之上，补充四个工程概念：

- **手写注意力的中间张量**：u2-l3 见过的注意力四步 `qwen3.py` 版本是「分数矩阵 → 掩码 → softmax → 加权」四个独立算子。每一步都会在显存（HBM，高带宽但相对慢的大容量存储）里生成一个形状约为 \((b \times H \times T \times T\)（\(H\)=头数，\(T\)=序列长度）的中间张量，写进去再读出来。序列一长，这些中间张量既是显存大头，也是带宽瓶颈。
- **融合内核（fused kernel）**：把多个算子合并成一个 GPU 函数，中间结果留在片上高速缓存（SRAM/寄存器）里，不落 HBM。SDPA 内部会把「打分 + 掩码 + softmax + 加权」融合成一次（或少数几次）内核调用，并用**在线 softmax**（分块流水计算，无需先看到整行就能归一化）避免物化整个 \(T \times T\) 注意力权重矩阵。
- **后端分发（backend dispatch）**：SDPA 不是单一实现，而是一个「调度入口」。PyTorch 会按设备、dtype、mask 性质自动挑选 FlashAttention、memory-efficient（xformers 风格）、cuDNN 或朴素的 math 后端。不同后端的浮点累加顺序不同，结果允许有舍入级差异。
- **预分配（pre-allocation）与分配器抖动**：u2-l5 的标准 KV 缓存每生成一个 token 就 `torch.cat` 一次，等于「分配一块更大的新显存 + 全量拷贝旧内容」。反复申请/释放会让 PyTorch 缓存分配器反复腾挪。预分配的思路是：**一开始就按最大上下文长度把 K/V 张量开好**，之后只做原地写入（`copy_`）和零拷贝切片。

一句话直觉：标准版为「可读性」服务，优化版为「GPU 带宽与分配器」服务，两者数学上等价、权重完全同名同形。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/qwen3_optimized.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py) | 本讲主角：优化版模型，SDPA 注意力 + 预分配 KVCache + `exact` 开关 + `sdpa_exact()` 上下文管理器 + 优化版 `generate_text_basic_cache` |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 对照组：标准版手写注意力（L220-224）、列表式 KVCache（L422-437） |
| [ch02/03_optimized-LLM/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md) | 官方说明：两大差异、drop-in 用法、Mac Mini/H100 基准表与选型建议 |
| [ch02/03_optimized-LLM/compare_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py) | 五开关基准脚本：`--device/--cache/--compile/--reasoning/--optimized` |
| [tests/test_qwen3_optimized.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_optimized.py) | 等价性回归测试：用 `exact=True` 断言两版生成文本前 60 字符一致 |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | `generate_stats`（L173-199）负责打印耗时、tokens/sec 与 CUDA/XPU 峰值显存 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先建立优化版的全局地图（模块一），再依次拆解 SDPA 注意力（模块二）、预分配 KVCache（模块三）、`exact` 标志与 `sdpa_exact`（模块四）。

### 4.1 优化版全貌：换引擎、不换架构

#### 4.1.1 概念说明

`qwen3_optimized.py` 是一份 **drop-in replacement**（即插即用替换）：类名、参数名、配置字典、权重文件与 `qwen3.py` 完全一致，只换两台「发动机」——注意力计算改用 SDPA、KV 缓存改为预分配。README 把设计动机说得很直白：标准版追求「人类可读 + CPU/GPU 都不差」，优化版在 GPU 上更进一步。

因为参数名完全相同，同一份 `qwen3-0.6B-base.pth` 权重可以直接 `load_state_dict` 进两个版本；`compare_inference.py` 也正是这样切换实现的（见 4.1.3）。

#### 4.1.2 核心流程

优化版前向流程与 u2-l5 学过的标准版骨架一致，差别集中在四处：

```text
Qwen3Model.forward(in_idx, cache)
 ├─ tok_emb 查表
 ├─ 位置游标 pos_start/pos_end 与越界检查          （与标准版相同）
 ├─ 掩码：torch.full(-inf) + triu(diagonal=1+pos_start)   ← 差异①：加性浮点掩码，一次成型
 └─ for 每层 block:
      cache.allocate(i, b)                         ← 差异②：首次进入时预分配该层 K/V 大张量
      x = block(x, mask, cos, sin, start_pos, cache, layer_idx=i, exact=self.exact)
          └─ GQA 内部: 投影→分头→QK-Norm→RoPE
                cache.append(i, k_new, v_new)      ← 差异③：原地写入，块级不再返回 (x, next_cache)
                k, v = cache.view(i)               ←        零拷贝读取已填前缀
                SDPA(q, k, v, attn_mask=mask)      ← 差异④：融合注意力替代手写四步
```

#### 4.1.3 源码精读

**构造函数多了 `exact` 开关**。[reasoning_from_scratch/qwen3_optimized.py:31-58](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L31-L58) 中 `Qwen3Model.__init__(self, cfg, exact=False)` 把 `exact` 存为实例属性并透传给每层 GQA；其余（`QWEN_CONFIG_06_B`、tok_emb、ModuleList、非持久 cos/sin buffer、`current_pos` 游标）与标准版一字不差。默认 `exact=False` 的含义在 4.4 详述。

**掩码从「布尔再切片」改为「加性 -inf 一步到位」**。[reasoning_from_scratch/qwen3_optimized.py:74-94](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L74-L94)：缓存路径下直接构造 `(num_tokens, pos_end)` 的 `-inf` 矩阵，再 `torch.triu(..., diagonal=1 + pos_start)` 取「对角线整体右移 pos_start」的上三角。这等价于标准版 [reasoning_from_scratch/qwen3.py:74-76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L74-L76) 的「先建 (pos_end, pos_end) 布尔上三角、再切 `[pos_start:pos_end, :pos_end]`」，但省掉了大矩阵中间物。语义关键不变：**新 token 只被禁止看到严格晚于它的位置**；元素值从「True=遮挡」变为「-inf=遮挡、0=放行」，因为 SDPA 的 `attn_mask` 参数约定是**加性掩码**（加到分数上，`-inf` 经 softmax 变成概率 0）。

**块循环从「元组回传」变为「原地改缓存」**。标准版 [reasoning_from_scratch/qwen3.py:92-98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98) 每层 `cache.get(i)` 取旧缓存、拿回 `new_blk_cache` 再 `cache.update(i, ...)`，因此 `TransformerBlock.forward` 必须返回 `(x, next_cache)` 二元组（[qwen3.py:136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L136)）。优化版 [qwen3_optimized.py:96-105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L96-L105) 在循环里先 `cache.allocate(i, x.size(0))`，把 `layer_idx=i` 与 `exact` 一路传下去，块本身只返回 `x`（[qwen3_optimized.py:143](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L143)）——缓存写入发生在 GQA 内部，不再需要元组上下穿梭。

**脚本侧的 drop-in 切换**。[ch02/03_optimized-LLM/compare_inference.py:60-76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L60-L76) 展示了最典型的用法：`--optimized` 时 `from reasoning_from_scratch.qwen3_optimized import Qwen3Model`，否则从 `qwen3` 导入同名类；`--optimized --cache` 时生成函数换成优化版自带的 `generate_text_basic_cache`（非流式），否则沿用 ch02 的流式版本。两套模型共用同一个 `QWEN_CONFIG_06_B` 与同一份 `.pth` 权重（[compare_inference.py:101-104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L101-L104)）。

顺带一提官方文档的小笔误：`--optimized` 开关的 help 文本写成了 "Use reasoning model variant."（[compare_inference.py:51-55](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L51-L55)），是从上面 `--reasoning` 参数复制时忘了改——读源码时别被它误导。

#### 4.1.4 代码实践

**实践目标**：用一个小配置亲手验证「优化版与标准版参数完全同名同形，权重可以原样互换」。

**操作步骤**（示例代码，CPU 即可运行）：

```python
# 文件名建议：tutorial_check_dropin.py（自己创建，不属于本仓库）
import torch
from reasoning_from_scratch.qwen3 import Qwen3Model
from reasoning_from_scratch.qwen3_optimized import Qwen3Model as Qwen3ModelOpt

cfg = {
    "vocab_size": 257, "context_length": 16, "emb_dim": 64,
    "n_heads": 4, "n_layers": 2, "hidden_dim": 128, "head_dim": 16,
    "qk_norm": True, "n_kv_groups": 2,
    "rope_base": 1_000_000.0, "dtype": torch.float32,
}
torch.manual_seed(123)
m_std = Qwen3Model(cfg)
m_opt = Qwen3ModelOpt(cfg)            # 注意：exact 默认 False

result = m_opt.load_state_dict(m_std.state_dict())
print("missing:", result.missing_keys, "unexpected:", result.unexpected_keys)

x = torch.randint(0, cfg["vocab_size"], (1, 5))
with torch.inference_mode():
    print(m_std(x).shape, m_opt(x).shape)
    print("max |diff|:", (m_std(x) - m_opt(x)).abs().max().item())
```

**需要观察的现象**：`missing_keys` 与 `unexpected_keys` 均为空列表；两个 logits 形状都是 `(1, 5, 257)`。

**预期结果**：参数名 100% 对齐（这正是 drop-in 替换的根基）；fp32 之下两版 logits 的最大差值通常为 0 或舍入量级。若在你机器上非 0，说明 SDPA 选择了非 math 后端——这正是 4.4 要讨论的话题。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果直接把优化版 `TransformerBlock` 的返回值从 `x` 改回标准版的 `(x, next_cache)`，会破坏什么？
**答案**：优化版的模型循环与 GQA 都按「块只返回 x」编写：`cache.append` 已在 GQA 内部完成，循环里没有任何 `cache.update` 调用（[qwen3_optimized.py:96-105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L96-L105)）。改回元组会让 `x = block(...)` 拿到元组，紧接着 `self.final_norm(x)` 直接报错。两种缓存管理风格不能混搭。

**练习 2**：为什么 `compare_inference.py` 里优化版配 `--cache` 时用的是「非流式」的 `generate_text_basic_cache`，而标准版配 `--cache` 用的是流式版？
**答案**：`qwen3_optimized.py` 只自带了一个生成函数 `generate_text_basic_cache`（L588-622），没有移植 ch02 的四代流式函数族；脚本只能有什么用什么，于是用 `is_streaming_generate` 标志区分两种消费方式（[compare_inference.py:66-76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L66-L76)）。这提醒我们：优化版定位是「性能基准与 GPU 推理」，不是功能全集。

### 4.2 SDPA：用融合内核替代手写注意力

#### 4.2.1 概念说明

`scaled_dot_product_attention`（缩放点积注意力，下称 SDPA）是 PyTorch 内置的注意力算子：

\[ \mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_h}} + M\right)V \]

其中 \(M\) 是加性掩码。它与手写版本算的是同一个公式，区别在于**执行方式**：手写版把公式拆成 4 个可见算子，每个算子是一次独立的内核启动，中间张量都要落 HBM；SDPA 是一个算子，内部按设备/dtype/mask 自动选择融合后端（FlashAttention、memory-efficient、cuDNN 或 math），融合后端用在线 softmax 分块计算，**从不物化 \(b \times H \times T \times T\) 的注意力权重矩阵**。省下的不只是显存，更是「写出去再读回来」的带宽——注意力这类算子是算术强度受限（memory-bound）的，带宽就是速度。

对 decode 阶段还有一层隐性收益：算子数更少意味着 Python 层调度与内核启动次数更少，单 token 步进的场景下这些固定开销占比很高。

#### 4.2.2 核心流程

优化版 GQA 的前向（对照标准版 [qwen3.py:183-225](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L183-L225)）：

1. 投影、分头、QK-Norm、RoPE——与标准版逐行相同；
2. 缓存：`cache.append(layer_idx, keys_new, values_new)` 写入预分配张量，`cache.view(layer_idx)` 读回完整前缀（4.3 详述）；
3. K/V 按 KV 组展开到 16 个头：`expand + reshape` 替代标准版的 `repeat_interleave`；
4. 掩码适配：把模型级 `(1, 1, T_q, T_k)` 掩码的 dtype 对齐到 Q 的 dtype；
5. 一次 `scaled_dot_product_attention(...)` 得到 context，再 `out_proj` 投回原宽。

#### 4.2.3 源码精读

**KV 组展开：`expand + reshape` 替代 `repeat_interleave`**。[reasoning_from_scratch/qwen3_optimized.py:222-227](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L222-L227)：先在组维插入新维度并 `expand` 到 `group_size` 倍，再 `reshape` 成 `(b, num_heads, seq, head_dim)`。标准版 [qwen3.py:216-217](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L216-L217) 的 `repeat_interleave(group_size, dim=1)` 语义相同（组 \(g\) 连续映射到头 \([g\cdot g_s,(g+1)\cdot g_s)\)）。注意 `expand` 本身是零拷贝视图，但后续 `reshape`（以及 SDPA 前的 `.contiguous()`）仍会物化一份连续副本——这一步主要是代码风格上的差异，真正省时省显存的是下面的 SDPA 与 4.3 的预分配缓存。

**掩码适配**。[reasoning_from_scratch/qwen3_optimized.py:230-238](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L230-L238)：兼容 2 维 / 3 维传入（分别补成 4 维），并把掩码 dtype 转成与 Q 一致（模型给的掩码是 `cfg["dtype"]` 即 bfloat16）。对照标准版 [qwen3.py:220-221](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L220-L221)：手写版是 `attn_scores.masked_fill(mask, -torch.inf)`——布尔掩码、True 处填 `-inf`；SDPA 版把同样的 `-inf` 直接「加」上去。

**SDPA 调用本体**。[reasoning_from_scratch/qwen3_optimized.py:241-259](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L241-L259)：

```python
if exact:
    with sdpa_exact():
        context = torch.nn.functional.scaled_dot_product_attention(
            queries.contiguous(), keys.contiguous(), values.contiguous(),
            attn_mask=attn_mask, dropout_p=0.0, is_causal=False,
        )
else:
    context = torch.nn.functional.scaled_dot_product_attention(...)  # 同参数，无上下文管理器
```

四个细节：① `is_causal=False`——因果性已经编码在显式 `attn_mask` 里（缓存解码时掩码不是规则下三角，SDPA 内建的 `is_causal=True` 表达不了「带偏移的因果」），所以必须走显式掩码路径；② `.contiguous()`——`reshape` 之后的 Q/K/V 需要连续内存布局，部分融合后端对非连续输入直接拒绝；③ `exact` 分支只是把同一次调用包进 `sdpa_exact()`（4.4）；④ 缩放 \(\frac{1}{\sqrt{d_h}}\) 由 SDPA 内部完成，所以不再看到标准版 [qwen3.py:222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L222) 的 `/ self.head_dim**0.5`。

**收益实测（作者数据）**。单独开 `--cache`（不 compile）时，H100 上优化版 56 tokens/sec 对标准版 48（[ch02/03_optimized-LLM/README.md:104-107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L104-L107) 与 [README.md:83-84](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L83-L84)）；Mac Mini CPU 上 49 对 28。更大的差距通常来自与 `torch.compile` 的组合（见综合实践）。

#### 4.2.4 代码实践

**实践目标**：在纯张量层面复现「手写注意力 vs SDPA」的等价性与舍入差异，不加载任何模型。

**操作步骤**（示例代码）：

```python
# 文件名建议：tutorial_sdpa_vs_manual.py
import torch
import torch.nn.functional as F

torch.manual_seed(0)
b, H, T, d = 1, 16, 256, 128
q = torch.randn(b, H, T, d, dtype=torch.bfloat16)
k = torch.randn(b, H, T, d, dtype=torch.bfloat16)
v = torch.randn(b, H, T, d, dtype=torch.bfloat16)
add_mask = torch.triu(
    torch.full((T, T), float("-inf"), dtype=torch.bfloat16), diagonal=1
)[None, None, :, :]

# 路线 A：qwen3.py 的手写四步（布尔掩码版）
scores = q @ k.transpose(2, 3)
scores = scores.masked_fill(add_mask.to(torch.bool), -torch.inf)
weights = torch.softmax(scores / d**0.5, dim=-1)
ctx_manual = weights @ v

# 路线 B：qwen3_optimized.py 的 SDPA（加性掩码版）
ctx_sdpa = F.scaled_dot_product_attention(
    q, k, v, attn_mask=add_mask, dropout_p=0.0, is_causal=False
)

print("max |diff| =", (ctx_manual.float() - ctx_sdpa.float()).abs().max().item())
```

**需要观察的现象**：两条路线输出形状一致 `(1, 16, 256, 128)`；bf16 下最大差值为非零的小量；把三处 `bfloat16` 全改成 `float32` 后差值显著缩小甚至为 0。

**预期结果**：bf16 差异量级约在 \(10^{-2}\)～\(10^{-3}\)（取决于后端选择），fp32 下趋近 0——这就是「数学等价、浮点不等价」。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么优化版传 `is_causal=False` 却仍然是因果注意力？如果偷懒传 `is_causal=True` 会怎样？
**答案**：因果性由 `attn_mask` 编码：prefill 时它是严格下三角的 `-inf` 矩阵，缓存解码时它是「行从 pos_start 开始、对角偏移 1+pos_start」的宽矩形切片——这种「历史全可见 + 新 token 只挡未来」的形态无法用 `is_causal=True`（规则下三角）表达。若强行传 `is_causal=True`，SDPA 会按「最后一维键长」生成规则下三角掩码，缓存解码时每个新 token 只能看到最后一个键，语义完全错误。

**练习 2**：手写注意力在长序列下的显存开销大约是多少？SDPA 融合后端省在哪里？
**答案**：手写版至少物化一个 \(b \times H \times T_q \times T_k\) 的分数/权重张量（掩码、softmax 各产生一份中间结果）。以 Qwen3 0.6B 单条 4096 上下文为例：\(16 \text{头} \times 4096^2 \approx 2.7\times10^8\) 元素/层，bf16 下约 0.5 GB/层。融合后端分块计算、在线 softmax，该矩阵从不完整落显存，既省容量更省读写带宽。

### 4.3 预分配 KVCache：allocate / append / view 三步

#### 4.3.1 概念说明

回顾 u2-l5：标准版 `KVCache` 是「每层一个槽位的列表」，槽里存 `(keys, values)` 元组，GQA 每步 `torch.cat` 拼接出**新的更大的张量**（[qwen3.py:206-213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L206-L213)）。生成 \(N\) 个 token 的总拷贝量是 \(O(N^2)\)，且分配器不停申请/释放越来越大的块。

优化版把策略反过来：**构造/首次前向时就按 `max_len`（= `context_length` = 40960）把每层 K/V 一次开满**，之后 decode 只做两件便宜事——向固定位置**原地写入**新 token 的 K/V（`append`），以及返回已填前缀的**零拷贝切片**（`view`）。代价是显存占用与「实际用到多少无关、只与最大长度有关」，这就是 README 观察到的 5.85 GB 对 1.52 GB 差距的来源。

#### 4.3.2 核心流程

```text
构造: KVCache(n_layers, max_len, num_kv_groups, head_dim, device, dtype)  # 只记元数据，k/v 还是 None
allocate(layer_idx, b): 若该层未分配 → torch.empty(b, G, max_len, d_h) × 2（K 与 V）
append(layer_idx, k_new, v_new): 在 [L : L+T] 原地 copy_，然后 L += T
view(layer_idx): 返回 k[layer_idx][:, :, :L, :] —— 同一块存储的切片，不拷贝
reset(): 全部置 None，交还给分配器
```

预分配显存的闭式估算（bf16，每个元素 2 字节，K 与 V 各一份）：

\[ M = n_{layers} \times 2 \times b \times G \times L_{max} \times d_h \times 2\,\text{B} \]

代入 Qwen3 0.6B：\(28 \times 2 \times 1 \times 8 \times 40960 \times 128 \times 2\,\text{B} = 4{,}670{,}024{,}320\,\text{B} \approx 4.35\,\text{GiB}\)，加上模型本体约 1.5 GB，正好对上 README 的 5.85 GB。

#### 4.3.3 源码精读

**KVCache 类本体**。[reasoning_from_scratch/qwen3_optimized.py:407-440](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L407-L440)：

- 构造函数只记录 `max_len / num_kv_groups / head_dim / device / dtype` 等元数据，`self.k`、`self.v` 是 `None` 占位列表；
- [allocate（L418-424）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L418-L424)：首次进入某层时 `torch.empty(b, G, max_len, d_h)` 开出满长张量并置 `len=0`；再次调用是无操作（幂等）——所以模型循环每步都调它也不碍事（[qwen3_optimized.py:97-98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L97-L98)）。用 `empty` 而非 `zeros` 是安全的：`view` 只会暴露被 `append` 写过的前缀；
- [append（L426-431）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L426-L431)：`self.k[layer_idx][:, :, L:L+T, :].copy_(k_new)`——注意是对**已有张量的切片**做原地 `copy_`，只写 T 个新位置，不触碰前 L 个旧位置，也不申请新显存；
- [view（L433-435）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L433-L435)：返回 `[:, :, :L, :]` 前缀切片。这里的 `view` 是缓存类自己的方法名（「查看已填内容」），与 `torch.Tensor.view` 无关；切片共享底层存储，零拷贝。

**GQA 侧的配套改动**。[reasoning_from_scratch/qwen3_optimized.py:213-219](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L213-L219)：给了 cache 却没给 `layer_idx` 时直接 `ValueError`；否则 `cache.append(layer_idx, keys_new, values_new)` 写入、`cache.view(layer_idx)` 读出 `keys, values` 供注意力使用。对比标准版 [qwen3.py:206-213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L206-L213) 的 `torch.cat([prev_k, keys_new], dim=2)`：每步新分配 + 全量拷贝，且 `next_cache` 要一路向上回传。

**生成函数如何驱动它**。[reasoning_from_scratch/qwen3_optimized.py:588-622](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L588-L622)：优化版 `generate_text_basic_cache` 在函数内用模型配置构造满规格缓存（`max_len=model.cfg["context_length"]`，L597-604），`model.reset_kv_cache()` 清零位置游标后 prefill（L606），随后是熟悉的 argmax 循环：EOS 检查（`torch.all`，兼容 batch）、跳过最后一次前向（L617-618）。缓存生命周期与 u3-l2 学过的原则一致：**每次调用新建缓存 + 清零游标，成对出现**。

**代价与适用边界**。[ch02/03_optimized-LLM/README.md:111-117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L111-L117) 给出官方结论：优化版多数场景更快，但 Mac Mini CPU 上「标准版 + cache + compile」反超（68 对 51 tokens/sec）；带 KV cache 的优化版在 H100 上常驻 5.85 GB（标准版 1.52 GB），因为预分配直接按 40960 上下文开满——README 特别指出，若标准版真的跑满 41k 上下文，两者显存会差不多。**选型建议：CPU 用标准版（--cache --compile），GPU 用优化版（--cache --compile）。**

#### 4.3.4 代码实践

**实践目标**：独立驱动预分配 KVCache 的三步 API，验证「append 是原地写、view 是零拷贝切片」。

**操作步骤**（示例代码，不需要模型）：

```python
# 文件名建议：tutorial_prealloc_cache.py
import torch
from reasoning_from_scratch.qwen3_optimized import KVCache

cache = KVCache(n_layers=2, max_len=10, num_kv_groups=2,
                head_dim=4, device="cpu", dtype=torch.float32)

cache.allocate(0, b=1)                     # 第一次：开张量
print("shape:", cache.k[0].shape)          # 期待 (1, 2, 10, 4)
cache.allocate(0, b=1)                     # 第二次：幂等，无操作

k1 = torch.randn(1, 2, 3, 4)
cache.append(0, k1, torch.randn(1, 2, 3, 4))
k2 = torch.randn(1, 2, 2, 4)
cache.append(0, k2, torch.randn(1, 2, 2, 4))

k_view, v_view = cache.view(0)
print("view shape:", k_view.shape)         # 期待 (1, 2, 5, 4)
print("prefix equal:", torch.equal(k_view[:, :, :3], k1))   # True
print("suffix equal:", torch.equal(k_view[:, :, 3:], k2))   # True
print("storage shared:", k_view.data_ptr() == cache.k[0].data_ptr())  # True
cache.reset()
print("after reset:", cache.k[0], cache.len[0])             # None, 0
```

**需要观察的现象**：allocate 幂等；两次 append 后 `len=5`；`view` 的前 3 个位置等于第一次写入的 `k1`、后 2 个等于 `k2`；`data_ptr` 相同证明 view 与底仓共享存储；reset 后引用释放。

**预期结果**：如上注释所示，CPU 上可直接验证，无需 GPU。另请手算一遍 4.3.2 的显存公式并写下你自己的数字（约 4.35 GiB），与 README 的 5.85 GB（含模型）对照。

#### 4.3.5 小练习与答案

**练习 1**：标准版与优化版缓存里存的 K/V 分别是 repeat_interleave 展开前还是展开后的？谁的缓存更省？
**答案**：两版都存**展开前**的压缩版（优化版在 append 之前、标准版在 cat 之前完成 RoPE，展开都发生在读出缓存之后），因此每 token 缓存量都是 \(G \times d_h\) 而非 \(H \times d_h\)。差别不在「每 token 存多少」而在「预留多少」：标准版按需增长，优化版一次性开满 `max_len`。
**练习 2**：如果把 `max_len` 从 40960 改成 8192，会有什么影响？
**答案**：预分配显存从约 4.35 GiB 降到约 0.87 GiB（线性缩放），但引入不一致：模型级的越界检查以 `cfg["context_length"]`（40960）为准，而缓存容量只有 8192。生成到总长超过 8192 时，`append` 里 `self.k[layer_idx][:, :, L:L+T, :]` 的切片已到尽头、返回空张量，`copy_` 因形状不匹配直接抛 RuntimeError——错误比应有的时机早得多、且信息难懂。正确做法是让 `max_len` 始终等于 `cfg["context_length"]`，与优化版生成函数保持一致。

### 4.4 exact 标志与 sdpa_exact：控制舍入差异

#### 4.4.1 概念说明

SDPA 的融合后端为了速度会重排浮点运算（分块累加、在线 softmax），结果与「手写逐步算」在 bf16 下存在舍入级差异——通常不影响生成质量，但带来两个麻烦：

1. **可测试性**：想断言「优化版与标准版生成完全相同的文本」，微小差异经几百步自回归放大就可能让两条轨迹分叉；
2. **可复现性**：不同硬件选不同后端，同一个模型在不同机器上输出可能不同。

`exact` 标志就是为此准备的开关：`exact=True` 时用 `sdpa_exact()` 上下文管理器**强制 SDPA 走 math 后端**（朴素实现，运算顺序与手写版一致），换取与标准版的逐步一致；默认 `exact=False` 则让 PyTorch 自选最快后端，速度优先、容忍舍入差异。

#### 4.4.2 核心流程

```text
Qwen3Model(cfg, exact=False)            # 构造时决定，模型级属性
  └─ forward: block(..., exact=self.exact)   # 逐层透传
       └─ GQA.forward(..., exact=False)
            if exact: with sdpa_exact(): SDPA(...)   # 强制 MATH 后端
            else:     SDPA(...)                      # 自选后端（Flash / Mem-efficient / ...）

sdpa_exact():
  首选 torch.nn.attention.sdpa_kernel(SDPBackend.MATH)     # PyTorch 2.3+ 的新 API
  失败则回退 torch.backends.cuda.sdp_kernel(
      enable_flash=False, enable_mem_efficient=False, enable_math=True)  # 旧版弃用 API
```

#### 4.4.3 源码精读

**`sdpa_exact` 上下文管理器**。[reasoning_from_scratch/qwen3_optimized.py:625-636](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L625-L636)：`try` 里走新 API `sdpa_kernel(SDPBackend.MATH)`——从 `torch.nn.attention` 导入，把 SDPA 的后端白名单临时收窄为 math 一个；`except Exception` 里回退到已弃用的 `torch.backends.cuda.sdp_kernel(enable_flash=False, enable_mem_efficient=False, enable_math=True)`，照顾旧版 PyTorch。`contextlib.contextmanager` 让它可以用 `with sdpa_exact():` 包住单次注意力调用，离开作用域自动恢复全局后端偏好。

**两处默认值**。[qwen3_optimized.py:32](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L32) 的 `Qwen3Model.__init__(self, cfg, exact=False)` 与 [L190](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L190) 的 GQA 形参 `exact=False` 都是「快但不保证位级一致」。`qwen3_optimized.load_model_and_tokenizer`（[L639-677](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py#L639-L677)）内部构造模型时也未传 `exact`，即生产/推理路径默认快路径。

**测试如何使用它**。[tests/test_qwen3_optimized.py:87-165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_optimized.py#L87-L165) 的 `test_qwen3_vs_optimized_qwen3` 用同一个权重文件分别构造标准版与 `Qwen3ModelOptimized(QWEN_CONFIG_06_B, exact=True)`（[L110-118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_optimized.py#L110-L118)），各自生成 12 个 token，然后断言解码文本**前 60 个字符完全相等**（[L154-165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_optimized.py#L154-L165)）。这正是 `exact=True` 的存在意义：把「数学等价」提升为「逐步位级一致」，测试才可能这么严。该测试同时参数化 base 与 reasoning 两种权重，且默认被 `SKIP_EXPENSIVE=1` 跳过（需下载真实权重，CI 中标为 expensive）。

#### 4.4.4 代码实践

**实践目标**：观察 `exact=True/False` 在同一模型上的输出差异，并确认 `sdpa_exact` 的 API 可用性。

**操作步骤**（示例代码）：

```python
# 文件名建议：tutorial_exact_flag.py
import torch
from reasoning_from_scratch.qwen3_optimized import Qwen3Model

cfg = {  # 与 4.1.4 相同的迷你配置
    "vocab_size": 257, "context_length": 16, "emb_dim": 64,
    "n_heads": 4, "n_layers": 2, "hidden_dim": 128, "head_dim": 16,
    "qk_norm": True, "n_kv_groups": 2,
    "rope_base": 1_000_000.0, "dtype": torch.bfloat16,
}
torch.manual_seed(7)
m_fast = Qwen3Model(cfg, exact=False)
m_exact = Qwen3Model(cfg, exact=True)
m_exact.load_state_dict(m_fast.state_dict())

x = torch.randint(0, cfg["vocab_size"], (1, 8))
with torch.inference_mode():
    print("max |diff| =", (m_fast(x) - m_exact(x)).abs().max().item())

# 检查 sdpa_exact 依赖的新 API 是否存在
from torch.nn.attention import sdpa_kernel, SDPBackend
print("sdpa_kernel OK, backends:", [b.name for b in SDPBackend][:3])
```

**需要观察的现象**：`max |diff|` 在 CPU 上可能为 0（CPU 上可选后端少，默认可能就是 math），在 CUDA 上预期为 bf16 舍入量级的小量；`sdpa_kernel` 导入成功（PyTorch ≥ 2.3）。

**预期结果**：差异量级与硬件、dtype、PyTorch 版本都相关，具体数值**待本地验证**。关键结论不是数字大小，而是：`exact=True` 提供的是「与手写实现逐步一致」的**保证**，`exact=False` 只是「通常差异很小」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compare_inference.py` 与 `load_model_and_tokenizer` 都用默认 `exact=False`，而测试用 `exact=True`？
**答案**：基准测速要代表真实部署路径——生产路径追求最快后端，位级一致性既做不到也无必要；而等价性测试要的是确定性断言（前 60 字符相等），必须掐掉后端自由度。一个开关服务两种场景，这正是把 `exact` 做成构造参数而非全局配置的好处。
**练习 2**：`sdpa_exact` 的 `except Exception` 分支为什么必要？
**答案**：`torch.nn.attention.sdpa_kernel` 与 `SDPBackend` 是较新 PyTorch 才有的 API；在旧版本上导入即抛异常。上下文管理器捕获后回退到（已弃用但长期存在的）`torch.backends.cuda.sdp_kernel`，用三个布尔开关达到同样的「只允许 math 后端」效果。这是兼容多版本依赖库的常见手法。

## 5. 综合实践

把本讲三个模块串成一份完整的「优化版 vs 标准版」评估报告。分三步：

**第一步：差异清单（源码阅读型，必做）。**
并排打开 [qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) 与 [qwen3_optimized.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py)（编辑器分屏或 diff 工具），聚焦 `GroupedQueryAttention` 与 `KVCache` 两个类，写一份**不少于 5 条**的差异清单，每条注明两边的行号。参考维度（你的表述应自成一句，不要照抄）：

1. 注意力实现：手写四步（qwen3.py:220-224）对 SDPA 融合调用（qwen3_optimized.py:241-259）；
2. 掩码形态：布尔掩码 + masked_fill 对加性 -inf 掩码 + attn_mask（两版模型级 forward 的构造方式也不同）；
3. K/V 组展开：repeat_interleave 对 expand + reshape（qwen3.py:216-217 对 qwen3_optimized.py:222-227）；
4. KVCache：列表 + torch.cat 每步新分配（qwen3.py:422-437, 206-213）对预分配 + copy_ 原地写（qwen3_optimized.py:407-440, 213-219）；
5. 缓存管理位置：块返回 `(x, next_cache)`、模型循环 get/update，对 GQA 内部 append/view、块只返回 x（qwen3_optimized.py:130-143）；
6. 新增参数：`layer_idx`（GQA 必填校验）与 `exact`（qwen3_optimized.py:32, 190, 214-215）。

**第二步：运行基准（需要已下载权重，约 1.2 GB）。**
base 模型对默认提示词大约生成 50 个 token 后 EOS 停止，正好对应任务要求：

```bash
# 标准版 + KV cache
uv run ch02/03_optimized-LLM/compare_inference.py --device cpu --cache
# 优化版 + KV cache
uv run ch02/03_optimized-LLM/compare_inference.py --device cpu --cache --optimized
# 有 NVIDIA GPU 时（把 cpu 换成 cuda），再加 --compile 各跑一轮
```

每轮脚本自动跑 3 次迭代并打印 Time、tokens/sec；在 CUDA/XPU 上还会打印 `Max CUDA memory allocated`（来自 [reasoning_from_scratch/ch02.py:173-199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L173-L199) 的 `generate_stats`）。把两版的 tokens/sec 与峰值显存记入表格。CPU 环境没有显存统计行，只能记录耗时与吞吐——如实注明即可。

**第三步：写结论（两段话）。**
① 用自己的话解释 `qwen3_optimized.Qwen3Model(cfg)` 中 `exact=False` 的默认含义（参考 4.4：速度优先、自选融合后端、与手写版存在 bf16 舍入级差异；等价性测试才需要 `exact=True`）；② 对照 README 的选型建议（[ch02/03_optimized-LLM/README.md:111-117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L111-L117)）说明你的实测支持还是偏离官方结论（CPU 上标准版 + compile 可能反超；GPU 上优化版 + cache + compile 最快但常驻约 5.85 GB 显存）。

**预期结果**：一份含差异清单、实测数据表、两段结论的 Markdown 报告。若你的环境无法下载权重或无 GPU，第二步可降级为「阅读型」：通读 [compare_inference.py:119-164](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L119-L164) 并引用 README 两张基准表（L69-107）代替实测，明确标注「引用作者数据，待本地验证」。

## 6. 本讲小结

- `qwen3_optimized.py` 是 `qwen3.py` 的 drop-in 替换：参数名、配置、权重完全同名同形，只换两台发动机——SDPA 融合注意力与预分配 KVCache；`compare_inference.py` 用 `--optimized` 一个开关切换两套实现。
- SDPA 把「打分 → 掩码 → softmax → 加权」融合为单算子并自动分发后端（Flash / mem-efficient / cuDNN / math），不物化 \(T \times T\) 注意力矩阵，省显存更省带宽；掩码从布尔 + `masked_fill` 改为加性 `-inf` + `attn_mask`，且必须 `is_causal=False`（显式掩码才能表达缓存解码的偏移因果）。
- 预分配 KVCache 三步：`allocate` 首次按 `max_len=context_length` 开满张量（幂等）、`append` 对切片原地 `copy_` 只写新 token、`view` 返回零拷贝前缀；彻底消除 decode 期每步 `torch.cat` 的重复分配与 \(O(N^2)\) 拷贝，代价是常驻约 4.35 GiB 缓存显存（H100 实测总占用 5.85 GB 对 1.52 GB）。
- 缓存管理位置随之下沉：GQA 内部 `append/view`，块只返回 `x`，因此 GQA 需要 `layer_idx`（缺失即 ValueError）；模型循环改为 `cache.allocate(i, b)`。
- `exact=False`（默认）= 速度优先，容忍融合后端的 bf16 舍入差异；`exact=True` 配合 `sdpa_exact()`（新 API `sdpa_kernel(SDPBackend.MATH)`，旧版回退 `sdp_kernel`）强制 math 后端，换取与标准版逐步一致，供 `tests/test_qwen3_optimized.py` 做前 60 字符相等的严格断言。
- 官方选型：CPU 用标准版 + `--cache --compile`（68 对 51 tokens/sec 反超），GPU 用优化版 + `--cache --compile`（H100 177 tokens/sec）。

## 7. 下一步学习建议

- **下一讲 u10-l2（批量推理：qwen3_batched 与吞吐优化）**：本讲的预分配缓存是「单条序列」的优化，下一讲把整个 batch 一起喂进模型——会遇到 left-padding、按行位置编码、序列提前结束后收缩缓存（`shrink_kv_cache_inplace`）等新问题，其中 SDPA 与掩码的知识直接复用。
- 若想补齐性能主线：回看 u3-l3（torch.compile）与本讲的组合效应，注意 `compare_inference.py:106-113` 的版本守护（PyTorch ≥ 2.8 需设 `allow_unspec_int_on_nn_module`）正是为 `current_pos` 这类整型属性的自增而设。
- 延伸阅读源码：`tests/test_qwen3_optimized.py` 的另一个测试（L39-84）用 transformers 的 `Qwen3ForCausalLM` 验证 from-scratch 实现的等价性，是 u9-l5 生态互通话题的伏笔；PyTorch 官方文档中 `torch.nn.attention` 模块与 SDPA 后端教程可深化 4.2/4.4 的理解。
