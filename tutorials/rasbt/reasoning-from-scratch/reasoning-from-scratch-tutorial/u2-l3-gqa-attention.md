# 分组查询注意力 GQA 与 QK-Norm

## 1. 本讲目标

上一讲我们拆开了 `TransformerBlock` 的两个基础组件（RMSNorm 与 SwiGLU 前馈网络），本讲进入块内更复杂的部件：**分组查询注意力（Grouped Query Attention, GQA）**。学完本讲，你应该能够：

1. 解释 GQA 如何让 16 个 query 头共享 8 个 KV 组，从而把 KV cache 压缩一半。
2. 跟踪张量形状从 `(b, num_tokens, d)` 到四维头形状 `(b, heads, num_tokens, head_dim)` 的完整变换过程，并理解 `repeat_interleave` 的展开逻辑。
3. 说明 `q_norm` / `k_norm`（QK-Norm）如何复用上一讲的 RMSNorm，对每个注意力头的向量做逐头归一化。
4. 理解缩放点积注意力中「除以 \(\sqrt{d_h}\)」和因果掩码 `masked_fill` 两步的数学含义。

## 2. 前置知识

### 2.1 从多头注意力（MHA）说起

Transformer 原始论文使用**多头注意力（Multi-Head Attention, MHA）**：把隐藏向量投影成 \(h\) 组 query、key、value，每组称为一个「头」，各自独立计算注意力后拼回。直觉上，不同的头可以关注不同类型的信息（有的看相邻词，有的看远处的语法结构）。

**自注意力的核心公式**（单头版本）：

\[
\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^\top}{\sqrt{d_h}}\right)V
\]

其中 \(QK^\top\) 是 query 与 key 的点积（衡量「相关性」），除以 \(\sqrt{d_h}\)（\(d_h\) 是头维度）防止点积随维度增大而过大，softmax 把分数变成概率分布，最后对 value 加权求和。

### 2.2 KV cache：为什么它值得优化

模型是**自回归**的：生成第 100 个 token 时，前 99 个 token 的 key 和 value 其实已经算过一遍。把它们缓存起来（即 **KV cache**），每生成一个新 token 只需计算新位置的 K/V，这是第 1 讲提到的「KV 缓存加速」的来源。

但缓存有代价：序列越长，缓存的 K/V 张量越大。在 MHA 里，\(h\) 个头每个都要存一份 K 和 V；如果能让多个 query 头**共享**同一份 K/V，缓存就能成倍缩小——这正是 GQA 的出发点。

### 2.3 三个关键术语

| 术语 | 含义 |
|---|---|
| **MHA**（Multi-Head Attention） | 每个 query 头配一份独立的 K/V，质量最好、缓存最大 |
| **MQA**（Multi-Query Attention） | 所有 query 头共享**一份** K/V，缓存最小、质量可能下降 |
| **GQA**（Grouped Query Attention） | 折中方案：把 query 头分组，每组共享一份 K/V |

GQA 由 Ainslie 等人在 2023 年提出，Qwen3、Llama 2/3 等主流开源模型均采用。

### 2.4 你需要携带的前置认知

- 上一讲的 **RMSNorm**：`x * rsqrt(mean(x²) + eps) * scale`，本讲会在注意力内部再次遇到它。
- 上一讲的 **Pre-Norm 残差结构**：`TransformerBlock` 先 `norm1` 再进注意力，本讲的 GQA 就是这里的 `self.att`。
- 第 2 讲的 **形状约定**：`(batch, num_tokens, emb_dim)` 三维张量贯穿全书。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会精确到其中的五个代码点：

| 代码点 | 位置 | 作用 |
|---|---|---|
| `QWEN_CONFIG_06_B` | `reasoning_from_scratch/qwen3.py` L15-L27 | GQA 的头数、KV 组数、qk_norm 开关都在这里配置 |
| `TransformerBlock.__init__` | 同文件 L108-L121 | 把配置项传入 `GroupedQueryAttention` 的装配处 |
| `GroupedQueryAttention.__init__` | 同文件 L153-L181 | 四个线性投影层 + 可选 QK-Norm 的定义 |
| `GroupedQueryAttention.forward` | 同文件 L183-L225 | 投影 → 分头 → 归一化 → RoPE → 展开 → 注意力 → 输出投影 |
| `RMSNorm` | 同文件 L323-L344 | 上一讲已精读，本讲关注它在 QK-Norm 中的复用 |

> 提示：本讲的 `forward` 中会出现 `apply_rope`（位置编码）和 `cache`（KV 缓存拼接）。RoPE 细节在下一讲（u2-l4），KV cache 生命周期在 u2-l5，本讲只需知道它们「在那里、做什么」即可。

## 4. 核心概念与源码讲解

### 4.1 GQA 的动机：让 16 个 query 头共享 8 个 KV 组

#### 4.1.1 概念说明

Qwen3 0.6B 的配置是 **16 个 query 头、8 个 KV 组**（`n_heads=16, n_kv_groups=8`）。也就是说：每 **2 个** query 头组成一组，共用同一份 key 和 value。

- 「组数」= KV 的实际份数：K/V 投影的输出宽度只有 `num_kv_groups × head_dim = 8 × 128 = 1024`，而不是 MHA 的 `16 × 128 = 2048`。
- 「组大小」`group_size = num_heads // num_kv_groups = 2`：计算注意力时，每个 K/V 会被复制 2 份去配对组内的 2 个 query 头。

这带来两个直接收益（以 Qwen3 0.6B、28 层、bf16 计）：

| 指标 | MHA（假设） | GQA（实际） |
|---|---|---|
| 每层 K+V 投影参数 | 2 × 1024 × 2048 ≈ 4.19M | 2 × 1024 × 1024 ≈ 2.10M |
| 每 token 的 KV cache | 28 层 × 2 × 16 头 × 128 维 × 2 字节 ≈ 224 KiB | 28 层 × 2 × 8 组 × 128 维 × 2 字节 ≈ 112 KiB |
| 生成 4096 token 的缓存总量 | ≈ 896 MiB | ≈ 448 MiB |

注意：GQA **不减少计算量**（16 个头的注意力分数照算），省的是参数和显存——对长上下文推理（context_length 40960）尤为关键。

#### 4.1.2 核心流程

GQA 的构造逻辑可概括为：

```text
输入: d_in(隐藏宽), num_heads(16), num_kv_groups(8), head_dim(128)
约束: num_heads % num_kv_groups == 0（整除才能均分组）
推导: group_size = num_heads // num_kv_groups = 2
     d_out = num_heads × head_dim = 2048
权重:
  W_query : d_in → num_heads    × head_dim   （2048 宽）
  W_key   : d_in → num_kv_groups × head_dim   （1024 宽 ← 省的就是这里）
  W_value : d_in → num_kv_groups × head_dim   （1024 宽）
  out_proj: d_out → d_in
可选: qk_norm=True 时为 q、k 各建一个 RMSNorm(head_dim)
```

#### 4.1.3 源码精读

先看配置字典——三个本讲相关的键：`n_heads=16`、`head_dim=128`、`qk_norm=True`、`n_kv_groups=8`：

[reasoning_from_scratch/qwen3.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L15-L27)
配置字典集中声明了 GQA 的全部结构参数，注意 `head_dim=128` 是**显式指定**的（16 × 128 = 2048 > emb_dim 1024，说明 Qwen3 故意用了「头维不等于 emb_dim/头数」的设定，这点与很多早期模型不同）。

[reasoning_from_scratch/qwen3.py:L108-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L108-L118)
`TransformerBlock.__init__` 把配置逐项传入 GQA：`d_in=emb_dim`、`num_heads=n_heads`、`head_dim`、`num_kv_groups`、`qk_norm`。这是上一讲 Pre-Norm 骨架中的 `self.att`。

再看 GQA 构造函数本体：

[reasoning_from_scratch/qwen3.py:L153-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L153-L181)

逐段解读：

- **L158**：`assert num_heads % num_kv_groups == 0` —— 头数必须能被组数整除，否则「均分共享」无从谈起，构造期就报错（fail fast）。
- **L162**：`self.group_size = num_heads // num_kv_groups` —— 组大小，后面 `repeat_interleave` 的复制因子。
- **L164-L166**：若调用方不传 `head_dim`，则按 `d_in // num_heads` 推导（并断言整除）；Qwen3 实际传入 128，走另一分支。
- **L171-L173**：三个投影层。关键差异：`W_query` 输出 `num_heads × head_dim` 宽，而 `W_key`/`W_value` 只输出 `num_kv_groups × head_dim` 宽——**这行代码就是 GQA 省参数的全部秘密**。
- **L175**：`out_proj` 把拼接后的 2048 宽投回 `d_in`，形状守恒。
- **L177-L181**：`qk_norm=True` 时为 query 和 key 各建一个作用在 `head_dim` 上的 RMSNorm（4.3 节详述）；否则显式置 `None`，不产生任何参数。

#### 4.1.4 代码实践：数一数 GQA 到底省了多少参数

**实践目标**：用缩小配置实例化 GQA，通过 `state_dict` 数出每个权重张量的形状，验证「K/V 投影宽度只有 Q 的一半」。

**操作步骤**（以下为示例代码，非项目原有代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import GroupedQueryAttention

att = GroupedQueryAttention(d_in=64, num_heads=4, num_kv_groups=2, head_dim=16)
for name, p in att.state_dict().items():
    print(f"{name:20s} {tuple(p.shape)}")
```

**需要观察的现象**：

- `W_query.weight` 形状为 `(64, 64)`，即 `d_out(4×16) × d_in(64)`；
- `W_key.weight` 与 `W_value.weight` 形状为 `(32, 64)`，即 `num_kv_groups×head_dim(2×16) × d_in`——宽度恰好是 Q 投影的一半；
- `out_proj.weight` 形状为 `(64, 64)`。

**预期结果**：K/V 投影参数量各为 Q 投影的一半；若把 `num_kv_groups` 改成 1（退化为 MQA），K/V 投影会进一步缩为 `(16, 64)`。

**待本地验证**：具体打印数值以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 Qwen3 0.6B 的 `n_kv_groups` 从 8 改成 4，每 token 的 KV cache 变为原来的多少？group_size 变为多少？

**答案**：KV 组数减半 → cache 变为原来的 1/2（约 56 KiB/token）；`group_size = 16 // 4 = 4`，即每 4 个 query 头共享一份 K/V。

**练习 2**：GQA 省下了显存和参数，为什么说它「几乎不省计算量」？

**答案**：注意力分数 `queries @ keys.transpose(2,3)` 的形状是 `(b, 16, t, t)`，与 K/V 原本有几份无关（K/V 先被复制展开到 16 份再参与计算）；QKV 三个投影中只省了 K/V 两个小投影的矩阵乘，占比很小。

**练习 3**：`GroupedQueryAttention(d_in=64, num_heads=6, num_kv_groups=4)` 会发生什么？

**答案**：`6 % 4 != 0`，在 L158 的断言处直接抛 `AssertionError`。构造期报错优于运行期形状错乱。

---

### 4.2 张量形状之旅：投影 → 分头 → 展开

#### 4.2.1 概念说明

注意力计算要求 Q 和 K 的**头数一致**（点积才能逐头配对），但 GQA 的 K/V 天生只有 8 份。源码的解法分三步：

1. **投影**：线性层把 `(b, t, d_in)` 映射为 `(b, t, heads×head_dim)` 或 `(b, t, kv_groups×head_dim)`。
2. **分头**（`view` + `transpose`）：把最后一维「劈开」成（头数, 头维），再把头维换到前面，得到四维 `(b, 头数, t, head_dim)`——这是所有注意力实现的通用形状约定。
3. **展开**（`repeat_interleave`）：把 8 份 K/V 沿头维度各复制 `group_size=2` 份，变成 16 份，与 Q 对齐。

`reshape` 和 `transpose` 是理解多头注意力的第一道坎，值得在脑中反复演练。

#### 4.2.2 核心流程

以 Qwen3 实际配置（b=1, t=5, d=1024）为例的完整形状变换表：

| 步骤 | 代码 | Q 形状 | K 形状 |
|---|---|---|---|
| 输入 | `x` | — | `(1, 5, 1024)` |
| 投影 | `W_query(x)` / `W_key(x)` | `(1, 5, 2048)` | `(1, 5, 1024)` |
| 分头 view | `.view(b, t, H, dh)` | `(1, 5, 16, 128)` | `(1, 5, 8, 128)` |
| 换轴 transpose(1,2) | — | `(1, 16, 5, 128)` | `(1, 8, 5, 128)` |
| QK-Norm（4.3 节） | `q_norm(q)` | `(1, 16, 5, 128)` | `(1, 8, 5, 128)` |
| RoPE（下一讲） | `apply_rope(...)` | 不变 | 不变 |
| 缓存拼接 | `torch.cat(..., dim=2)` | — | `(1, 8, 5+旧长度, 128)` |
| **展开** | `repeat_interleave(2, dim=1)` | — | `(1, 16, 5, 128)` |
| 分数 | `q @ k^T` | `(1, 16, 5, 5)` | — |

**一个容易被忽略的关键点**：`next_cache = (keys, values)` 在 L213 赋值，而 `repeat_interleave` 在 L216-L217 执行——**缓存里存的是展开前的 8 份压缩版**。展开只服务于本次注意力计算，不进缓存。这正是 GQA「缓存减半」在代码层面的落点。

#### 4.2.3 源码精读

[reasoning_from_scratch/qwen3.py:L187-L194](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L187-L194)
投影与分头：`view(b, num_tokens, num_heads, head_dim).transpose(1, 2)` 把最后一维拆成「头 × 头维」再把头轴提到 batch 之后。注意 Q 用 `self.num_heads`，K/V 用 `self.num_kv_groups`——分头数不同，这就是 GQA。

[reasoning_from_scratch/qwen3.py:L206-L217](https://github.com/rasbt/reasoning-for-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L206-L217)
缓存拼接与展开：若传入 `cache`（一个 `(prev_k, prev_v)` 二元组），新 K/V 沿 **dim=2（序列维）** 与历史拼接——这就是增量解码；随后 L216-L217 的 `repeat_interleave(self.group_size, dim=1)` 沿**头维**复制，`(b, 8, t, 128) → (b, 16, t, 128)`。

[reasoning_from_scratch/qwen3.py:L213-L217](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L213-L217)
注意顺序：`next_cache` 保存的是 L213 此刻的 `keys/values`——压缩版；`repeat_interleave` 是纯函数（返回新张量，不改原张量），所以缓存保持 8 份。**这是本讲最重要的一个设计细节。**

> 为什么用 `repeat_interleave` 而不是 `repeat`（tile）？`repeat_interleave(2, dim=1)` 产生 `[k0, k0, k1, k1, ...]`（组内相邻复制），保证第 0、1 号 query 头对应第 0 组 K，第 2、3 号头对应第 1 组 K——与权重加载时的头分组约定一致。若用 tile 得到 `[k0, k1, k0, k1, ...]`，头与组的配对关系就错位了。

#### 4.2.4 代码实践：亲眼看到 repeat_interleave 前后的形状

**实践目标**：按讲义规格构造 `GroupedQueryAttention(d_in=32, num_heads=4, num_kv_groups=2, head_dim=16)`，验证 keys 从 `(b, 2, t, 16)` 展开为 `(b, 4, t, 16)`。

**操作步骤**（以下为示例代码，非项目原有代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import GroupedQueryAttention, compute_rope_params

torch.manual_seed(42)
b, t = 1, 5
x = torch.randn(b, t, 32)

# forward 需要的另外两样：RoPE 表与因果掩码（下一讲/4.4 节详述）
cos, sin = compute_rope_params(head_dim=16, theta_base=1_000_000.0, context_length=64)
mask = torch.triu(torch.ones(t, t, dtype=torch.bool), diagonal=1)[None, None, :, :]

att = GroupedQueryAttention(d_in=32, num_heads=4, num_kv_groups=2, head_dim=16)
out, (k_cache, v_cache) = att(x, mask, cos, sin)
print("输出形状:", out.shape)        # 期望 (1, 5, 32)，形状守恒
print("缓存中的 keys:", k_cache.shape)  # 期望 (1, 2, 5, 16) ← 展开之前的压缩版

# 手动复现「展开」这一步（forward 内部在 L216 做的同一件事）
keys_expanded = k_cache.repeat_interleave(att.group_size, dim=1)
print("展开后的 keys:", keys_expanded.shape)  # 期望 (1, 4, 5, 16)
```

**需要观察的现象**：

1. `k_cache.shape == (1, 2, 5, 16)`——缓存确实只存 2 份（2 个 KV 组），不是 4 份；
2. `repeat_interleave(group_size=2, dim=1)` 之后变为 `(1, 4, 5, 16)`，与 query 的头数 4 对齐；
3. 沿 dim=1 相邻复制：`keys_expanded[:, 0]` 与 `keys_expanded[:, 1]` 完全相同（可用 `torch.equal` 验证）。

**预期结果**：形状断言全部成立；`out.shape == (1, 5, 32)` 与输入形状一致（形状守恒是 Pre-Norm 残差相加的前提，见上一讲）。

**待本地验证**：具体数值以本地运行为准，形状结论不依赖随机种子。

#### 4.2.5 小练习与答案

**练习 1**：`queries.view(b, t, H, dh).transpose(1, 2)` 中，如果漏掉 `transpose` 直接用 `(b, t, H, dh)` 参与点积，会发生什么？

**答案**：`queries @ keys.transpose(2, 3)` 会把 dim=2（本是时间维 t）当作头维、dim=3（本是头维）当作时间维做乘法。当 t ≠ H 时直接广播失败或形状错乱；即使碰巧形状可算，语义也完全错误（token 间被当成头配对）。这也解释了为什么 `TransformerBlock` 用 `ModuleList` 而非 `Sequential`——注意力需要精心控制轴的含义。

**练习 2**：Qwen3 0.6B 的 `d_out = 16 × 128 = 2048`，大于 `emb_dim = 1024`。投影时隐藏宽被「放大」了吗？

**答案**：是的，`W_query` 把 1024 维升到 2048 维（4 个头 × 128 也可以是任意头数×头维的组合），注意力之后 `out_proj` 再投回 1024。头维与 emb_dim 解耦，是显式传 `head_dim=128` 的效果（L164-L166 的两个分支）。

**练习 3**：缓存拼接 `torch.cat([prev_k, keys_new], dim=2)` 为什么是 dim=2 而不是 dim=1？

**答案**：四维形状约定是 `(b, 头, 序列, 头维)`，dim=2 是序列维。历史 token 在前、新 token 在后地拼在序列轴上；dim=1 是头维，拼那里等于增加头数，语义完全错误。

---

### 4.3 QK-Norm：注意力内部的一次 RMSNorm

#### 4.3.1 概念说明

Qwen3 在 GQA 内部做了一件上一代模型（如 Llama 2）不做的事：**对每个头的 query 和 key 向量各做一次 RMSNorm**，即 **QK-Norm**。

直觉解释：注意力分数是 q 与 k 的点积，如果某个头的 q/k 向量范数（长度）随训练逐渐变大，点积会跟着放大，softmax 分布趋于极端（一个位置吃掉全部注意力），梯度随之消失——这是深层模型训练不稳定的常见来源。QK-Norm 在点积**之前**把每个 q/k 都归一到相近的范数，等于给注意力分数的动态范围上了一道保险。Qwen3 官方配置 `qk_norm: True`（L23）。

它与模型级 `norm1`/`norm2` 的区别在于**作用位置**：

| | norm1 / norm2 | q_norm / k_norm |
|---|---|---|
| 作用对象 | 整个隐藏向量（`emb_dim` 维） | 每个头内的 q/k 向量（`head_dim=128` 维） |
| 作用时机 | 进注意力**之前**（Pre-Norm） | 投影、分头**之后**，RoPE 之前 |
| 归一化粒度 | 按 token | 按 token × 按头 |

#### 4.3.2 核心流程

```text
queries = W_query(x) → 分头 → (b, 16, t, 128)
keys    = W_key(x)   → 分头 → (b, 8,  t, 128)
                          ↓
q_norm(queries)   # 对最后一维（head_dim=128）做 RMSNorm，逐头独立
k_norm(keys_new)  # 同上；只归一化「新」key，历史 key 在缓存里已归一化过
                          ↓
apply_rope(...)   # 归一化之后再旋转（顺序重要）
```

RMSNorm 的数学形式（上一讲已推导，此处复述要点）：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\tfrac{1}{d}\sum_i x_i^2 + \epsilon}} \odot g
\]

其中 \(g\) 是可学习的逐维缩放（`scale` 参数），\(\epsilon = 10^{-6}\) 防除零。**归一化沿最后一维进行**——在四维头形状下即逐 token、逐头地对 128 维向量归一。

#### 4.3.3 源码精读

[reasoning_from_scratch/qwen3.py:L177-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L177-L181)
`qk_norm=True` 时创建 `RMSNorm(head_dim, eps=1e-6)`——作用维度是 **head_dim** 而非 emb_dim，这就是「逐头归一化」的代码表达；`qk_norm=False` 时两者为 `None`，前向中对应的 `if` 分支被跳过。

[reasoning_from_scratch/qwen3.py:L196-L204](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L196-L204)
前向中的应用时机：**分头之后、RoPE 之前**。`if self.q_norm:` 用模块本身做真值判断（`None` 为假），实现可选组件的零开销跳过。注意只对 `keys_new`（新产生的 key）归一化——缓存里的历史 key 在它各自的前向中已经归一化过，无需重复。

[reasoning_from_scratch/qwen3.py:L323-L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L323-L344)
被复用的 `RMSNorm`：上一讲精读过的实现，`variance = x.pow(2).mean(dim=-1, keepdim=True)` 沿**最后一维**求均方——无论输入是三维 `(b, t, d)` 还是四维 `(b, h, t, d)`，都自动归一化每个头的 128 维向量，无需任何修改。这是「归一化沿最后一维」设计带来的优雅复用。`qwen3_compatible=True` 分支先升 fp32 计算，保证低精度（bf16）输入下的数值稳定性。

[reasoning_from_scratch/qwen3.py:L581-L591](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L581-L591)
（延伸阅读）权重加载时 `q_norm.scale` / `k_norm.scale` 从 HF checkpoint 的 `model.layers.{l}.self_attn.q_norm.weight` 等键注入——证明官方 Qwen3 权重确实带这两个归一化参数，本仓库实现与之对齐。

#### 4.3.4 代码实践：对比 qk_norm=True / False 的数值差异

**实践目标**：构造两个权重相同、仅 QK-Norm 不同的注意力模块，观察注意力权重的数值差异。

**操作步骤**（以下为示例代码，非项目原有代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import (GroupedQueryAttention,
                                          compute_rope_params, apply_rope)

torch.manual_seed(123)
b, t = 1, 5
x = torch.randn(b, t, 32)
cos, sin = compute_rope_params(head_dim=16, theta_base=1_000_000.0, context_length=64)
mask = torch.triu(torch.ones(t, t, dtype=torch.bool), diagonal=1)[None, None, :, :]

# 两个模块；strict=False 让带 qk_norm 的模块沿用无 qk_norm 模块的投影权重
att_plain = GroupedQueryAttention(d_in=32, num_heads=4, num_kv_groups=2, head_dim=16, qk_norm=False)
att_qknorm = GroupedQueryAttention(d_in=32, num_heads=4, num_kv_groups=2, head_dim=16, qk_norm=True)
att_qknorm.load_state_dict(att_plain.state_dict(), strict=False)

def manual_attn_weights(att, x, mask, cos, sin):
    """复现 forward 中 L187-L222 的路径，返回注意力权重便于观察。"""
    b, t, _ = x.shape
    q = att.W_query(x).view(b, t, att.num_heads, att.head_dim).transpose(1, 2)
    k = att.W_key(x).view(b, t, att.num_kv_groups, att.head_dim).transpose(1, 2)
    if att.q_norm is not None:          # QK-Norm 分支
        q, k = att.q_norm(q), att.k_norm(k)
    q, k = apply_rope(q, cos, sin), apply_rope(k, cos, sin)
    k = k.repeat_interleave(att.group_size, dim=1)
    scores = (q @ k.transpose(2, 3)).masked_fill(mask, -torch.inf)
    return torch.softmax(scores / att.head_dim ** 0.5, dim=-1)

w_plain  = manual_attn_weights(att_plain,  x, mask, cos, sin)
w_qknorm = manual_attn_weights(att_qknorm, x, mask, cos, sin)
print("两者平均绝对差:", (w_plain - w_qknorm).abs().mean().item())
print("w_plain[0, 0, 0]:\n", w_plain[0, 0, 0])
print("w_qknorm[0, 0, 0]:\n", w_qknorm[0, 0, 0])
```

**需要观察的现象**：

1. 平均绝对差 **大于 0**——同样的投影权重，有没有 QK-Norm 会产生不同的注意力分布；
2. 两个版本每行概率都求和为 1（softmax 性质），且上三角位置（被掩码处）概率为 0；
3. QK-Norm 版本的注意力分布通常更「平缓」（更接近均匀），因为归一化压低了大范数向量的点积优势。

**预期结果**：差异不为零但幅度有限（`scale` 初始化为全 1，差异纯来自归一化本身）；具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：QK-Norm 为什么放在 RoPE **之前**而不是之后？

**答案**：RoPE 是旋转（保范数变换），归一化后的向量再旋转仍保持相近范数；若先旋转再归一化，则会破坏 RoPE 编码的相对位置信息与 `scale` 参数的配合方式，且官方 Qwen3 的算子顺序就是「投影 → QK-Norm → RoPE」，权重加载顺序（L581-L591 之前先加载投影）与之对应。本仓库与官方实现对齐。

**练习 2**：`if self.q_norm:` 为什么能直接把模块当布尔值用？

**答案**：Python 对对象做真值判断时依次查找 `__bool__` 和 `__len__`；基类 `nn.Module` 两者都没有定义（只有 `nn.Sequential`、`nn.ModuleList` 这类容器才定义 `__len__`），于是 `RMSNorm` 实例走默认规则——任何对象恒为真。所以 `qk_norm=False` 时属性是 `None`（假），`qk_norm=True` 时是模块实例（真），一个 `if` 就完成了可选子模块的分支。

> 注意：更明确、不依赖框架细节的写法是 `if self.q_norm is not None:`，效果等价。

**练习 3**：把 `qk_norm` 设为 `False`，模型还能加载官方 Qwen3 权重吗？

**答案**：不能完整加载。官方 checkpoint 含 `q_norm.weight` / `k_norm.weight`，而 `qk_norm=False` 的模型没有这两个参数去承接；反之 `qk_norm=True` 的模型加载不含这些键的旧式权重也会缺参。结构与权重必须严格配套。

---

### 4.4 缩放点积注意力与因果掩码

#### 4.4.1 概念说明

分头展开后，最后三行代码（L220-L222）完成注意力的数学核心：

\[
\text{attn\_scores} = QK^\top, \qquad
\text{attn\_scores}_{ij} = \begin{cases} -\infty & \text{若 } j > i \text{（未来位置）} \\ s_{ij} & \text{否则} \end{cases}
\]

\[
\text{attn\_weights} = \text{softmax}\left(\frac{\text{attn\_scores}}{\sqrt{d_h}}\right), \qquad
\text{context} = \text{attn\_weights} \cdot V
\]

两个设计各有其必要性：

- **缩放 \(\sqrt{d_h}\)**：假设 q、k 各分量独立同分布、均值为 0、方差为 1，则点积 \(q \cdot k = \sum_{i=1}^{d_h} q_i k_i\) 的方差是 \(d_h\)。头维 128 时分数的典型幅度约 \(\sqrt{128} \approx 11.3\)，softmax 对大分数极其敏感（指数爆炸，分布趋于 one-hot），除以 \(\sqrt{128}\) 把方差拉回 1 附近，梯度得以健康传播。
- **因果掩码**：语言模型按从左到右的顺序生成，第 \(i\) 个 token 只能看见 \(j \le i\) 的位置。`masked_fill(mask, -torch.inf)` 把「看未来」的位置填成 \(-\infty\)，softmax 之后这些位置的权重精确地变成 \(e^{-\infty}/\sum = 0\)。

#### 4.4.2 核心流程

```text
attn_scores = queries @ keys.transpose(2, 3)     # (b, 16, t, t) 相关性打分
attn_scores = attn_scores.masked_fill(mask, -inf) # 上三角（未来）置 -inf
attn_weights = softmax(attn_scores / √128, dim=-1)# 每行归一为概率分布
context = attn_weights @ values                   # (b, 16, t, 128) 加权聚合 V
context → transpose(1,2) → reshape(b, t, 2048)    # 头拼回序列维
out = out_proj(context)                           # 投回 (b, t, 1024)
```

掩码 `mask` 的形状是 `(1, 1, t, t)`：前两个 1 是为广播预留的维度，同一个掩码对 batch 和所有 16 个头生效（掩码在 `Qwen3Model.forward` 中构造，见下）。

#### 4.4.3 源码精读

[reasoning_from_scratch/qwen3.py:L220-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L220-L222)
三行核心：点积、`masked_fill(mask, -torch.inf)`、`torch.softmax(attn_scores / self.head_dim**0.5, dim=-1)`。注意两点细节：① 掩码是 **bool 张量**，`True` 的位置被填充（Qwen3Model 构造的 `torch.triu(..., diagonal=1)` 使上三角为 True，即「未来」被屏蔽）；② 缩放发生在 softmax 处（对已被掩码的 \(-\infty\) 除以正常数仍是 \(-\infty\)，顺序无影响）。

[reasoning_from_scratch/qwen3.py:L224-L225](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L224-L225)
输出聚合：`attn_weights @ values` 得到 `(b, 16, t, 128)`，`transpose(1, 2)` 把头轴换回序列轴之后（**先换轴再 reshape**，与分头时的顺序互逆），`reshape(b, num_tokens, self.d_out)` 把 16 个头拼成 2048 维，最后 `out_proj` 投回 `d_in`。至此形状守恒闭环，可安全地与残差 `shortcut` 相加（上一讲 L128）。

[reasoning_from_scratch/qwen3.py:L74-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L74-L90)
掩码的出生地：`Qwen3Model.forward` 用 `torch.triu(torch.ones(...), diagonal=1)` 造因果掩码，缓存解码时还会做 `[pos_start:pos_end, :pos_end]` 切片（u2-l5 详述）；L84-L90 的注释解释了为何补两个前导维度成 `(1, 1, t, t)`——让同一掩码沿 batch 维与 16 个头广播。

#### 4.4.4 代码实践：亲眼验证掩码与缩放的作用

**实践目标**：用 4.3.4 中的 `manual_attn_weights`（或重新执行），检查注意力权重矩阵的结构性质。

**操作步骤**（示例代码，非项目原有代码，接 4.3.4 的变量）：

```python
w = manual_attn_weights(att_plain, x, mask, cos, sin)  # (1, 4, 5, 5)

# 检查 1：上三角（未来）权重应为 0
print("未来位置权重最大值:", w[0, :, torch.triu(torch.ones(t, t, dtype=torch.bool), diagonal=1)].max().item())

# 检查 2：每行概率和为 1
print("每行和:", w.sum(dim=-1)[0, 0])

# 检查 3：不除 sqrt(head_dim) 会发生什么
scores = (att_plain.W_query(x).view(b, t, 4, 16).transpose(1, 2)
          @ att_plain.W_key(x).view(b, t, 2, 16).transpose(1, 2)
             .repeat_interleave(2, dim=1).transpose(2, 3))
w_unscaled = torch.softmax(scores.masked_fill(mask, -torch.inf), dim=-1)
print("未缩放分布的最大行熵差:", (w_unscaled.max(dim=-1).values - w.max(dim=-1).values).mean().item())
```

**需要观察的现象**：

1. 未来位置的权重为 0（或 `0.0`，-inf 经 softmax 的精确结果）；
2. 每行概率之和为 `1.0`；
3. 不除 \(\sqrt{16}=4\) 时，分布的最大概率普遍更高（分布更尖）——这就是缩放被需要的直观证据。

**预期结果**：三条全部成立；第 3 条的具体幅度**待本地验证**（head_dim=16 时缩放因子只有 4，效应比 128 维时温和，若不明显可把 `head_dim` 换成 128 重试）。

#### 4.4.5 小练习与答案

**练习 1**：`masked_fill(mask, -torch.inf)` 之后 softmax，被掩码位置得到的权重一定是 0 吗？如果某行**全部**被掩码呢？

**答案**：只要该行至少有一个未掩码位置，\(-\infty\) 的 softmax 权限就是 \(e^{-\infty}/\sum = 0\)。若整行全为 \(-\infty\)，softmax 得到 NaN（\(0/0\)）。因果掩码保证每行至少含对角线自身位置（\(j \le i\) 至少有 \(j=i\)），所以正常使用中不会触发 NaN。

**练习 2**：为什么缩放因子是 \(\sqrt{d_h}\)（head_dim）而不是 \(\sqrt{d_{out}}\)（2048）？

**答案**：点积是在**头维** 128 上做的（四维形状的最后一维），分数方差随该维度增长，故除以其平方根。2048 是 16 个头拼接后的宽度，不参与单个头的点积。

**练习 3**：`context.transpose(1, 2).reshape(b, t, d_out)` 中，把 `transpose` 和 `reshape` 顺序对调会发生什么？

**答案**：`reshape`（在需跨步时等价 `view` 失败后的拷贝）会把 `(b, 16, t, 128)` 的内存按错误顺序摊平成 `(b, t, 2048)`——头与 token 的维度假拼接，数值完全错乱且不会报错（最危险的 bug 类型）。正确的顺序是先把头轴换到倒数第二，再平坦化最后两维，与分头时的 `view(b, t, H, dh).transpose(1, 2)` 严格互逆。

---

## 5. 综合实践

**任务：不看 forward 源码，从零复现 GQA 的完整前向，并用模块输出校验。**

这是把 4.1—4.4 串起来的总装练习。步骤（示例代码，非项目原有代码）：

1. **构造组件**：沿用 4.3.4 的 `att_plain`、`x`、`mask`、`cos`、`sin`（固定随机种子保证可复现）。
2. **手写前向**：编写函数 `my_gqa_forward(att, x, mask, cos, sin)`，按顺序实现本讲的全部五个阶段：投影（L187-L189）→ 分头（L192-L194）→ QK-Norm 与 RoPE（L197-L204，无 qk_norm 时跳过）→ 展开与注意力（L216-L222）→ 聚合与输出投影（L224-L225）。
3. **校验**：调用 `att_plain(x, mask, cos, sin)` 拿到官方输出，用 `torch.allclose(my_out, official_out, atol=1e-5)` 对比。
4. **异常实验**：故意在你的复现中去掉 `masked_fill`，观察 `allclose` 失败——确认为什么掩码不可省略；再故意把 `repeat_interleave` 换成 `repeat(2, 1)`（tile 式复制），观察形状相同但数值不同——体会 4.2.3 提到的头配对错位问题。

**预期结果**：步骤 3 的 `allclose` 为 `True`（float32 下通常误差远小于 1e-5；具体容差**待本地验证**）；步骤 4 的两个变体都应失败，且失败方式不同（一个分布错、一个头配对错）。

完成本练习后，你就独立走通了一条与官方实现数值等价的 GQA 前向——下一讲把注意力中唯一被「绕过」的 `apply_rope` 补上。

## 6. 本讲小结

- **GQA 的本质**是让 `num_heads // num_kv_groups` 个 query 头共享一份 K/V：Qwen3 0.6B 中 16 头共享 8 组，K/V 投影宽度减半、每 token 的 KV cache 从约 224 KiB 降到 112 KiB，计算量几乎不变。
- **形状之旅**：`(b, t, d)` 投影 → `view + transpose` 分头为 `(b, H, t, dh)` → K/V 经 `repeat_interleave(group_size, dim=1)` 展开到与 Q 同头数；**缓存存的是展开前的压缩版**（L213 早于 L216）。
- **QK-Norm** 在分头后、RoPE 前对每个头的 128 维 q/k 向量做 RMSNorm，压制注意力分数的动态范围，是 Qwen3 区别于 Llama 2 的训练稳定性设计；RMSNorm「沿最后一维归一」的特性让它无需修改即可从三维复用到四维。
- **注意力三步**：点积打分 → `masked_fill` 因果掩码（\(-\infty\) → 权重 0）→ softmax 前除以 \(\sqrt{d_h}\) 控制分布尖度；输出经 `transpose + reshape + out_proj` 回到 `(b, t, d)`，形状守恒使残差相加成立。
- **构造期防御**：`num_heads % num_kv_groups == 0` 断言、`head_dim` 缺省时的整除断言，把配置错误拦截在实例化阶段。

## 7. 下一步学习建议

本讲的 `forward` 中有两处被有意「绕过」：

1. **RoPE**（L203-L204 调用的 `apply_rope`）：query 和 key 在注意力计算前被旋转编码了位置信息。下一讲 **u2-l4《RoPE 旋转位置编码》** 拆解 `compute_rope_params` 的逆频率与角度表、`apply_rope` 的 split-halves 旋转，并验证旋转不改变向量范数。
2. **KV cache 生命周期**（L206-L213）：`cache` 从哪来、`start_pos` 如何驱动增量解码。**u2-l5《KV Cache 与因果掩码》** 将从 `Qwen3Model.forward` 的 `current_pos` 追踪到缓存解码时掩码切片 `[pos_start:pos_end, :pos_end]` 的含义。

建议先做 u2-l4，再学 u2-l5；两者完成后，可以回头重读本讲的 4.2 节，此时 GQA 前向的每个细节你都能独立解释。
