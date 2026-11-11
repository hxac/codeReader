# 批量推理：qwen3_batched 与吞吐优化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「延迟（latency）」与「吞吐（throughput）」两种性能目标的区别，以及批处理为什么优化的是后者。
2. 解释 left-padding（左填充）与注意力掩码 `attn_mask` 如何让不同长度的 prompt 拼进一个矩形张量，且数值结果与逐条生成等价。
3. 理解 `apply_rope_with_pos_ids` 的按行位置编码接口，以及 RoPE 的相对性为什么让「整体平移的位置编号」不影响注意力分数。
4. 解释 `float32_upcast` 默认开启的原因：手写的 padding 稳定 softmax 如何避免全掩码行产生 NaN。
5. 精读 `generate_text_basic_batched_cache_stop` 的双坐标系设计（全批坐标 vs 幸存者坐标），以及 `shrink_kv_cache_inplace` 为什么只收缩还在生成的行。
6. 用真实基准数据量化批处理的吞吐收益与显存代价。

## 2. 前置知识

本讲是高级专题，假设你已学完单元二（尤其 u2-l5 的 KV Cache 与因果掩码）。这里补充几个新概念：

- **延迟与吞吐**：延迟指「一条 prompt 多快拿到答案」，吞吐指「单位时间能处理多少条 prompt」。单条生成延迟最低、最好调试；批处理牺牲一点单条延迟，换取总吞吐。附录 E 开篇明确提醒：批处理并非在所有设备上都更快——CPU 或优化不足的 GPU 上，填充与调度开销可能抵消并行收益，甚至变慢（见 [chE/chE_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chE/chE_main.ipynb) E.1 节）。
- **矩形约束与填充（padding）**：PyTorch 张量必须是规则的矩形。一个 batch 里各 prompt 长度不同（一道题 40 个 token、另一道 120 个），必须把短行补齐到最长行的长度。本讲用的是**左填充**：pad 放在行首，真实 token 靠右对齐。
- **`attn_mask`（填充掩码）**：形状 `(B, T)` 的布尔张量，`True` 表示真实 token、`False` 表示 pad。它与第 2 章的因果掩码是两回事：因果掩码挡「未来」，`attn_mask` 挡「填充」。
- **KV cache 的批维**：u2-l5 讲过 `KVCache` 是「每层一个槽位」的列表，每层存 `(K, V)` 元组，形状 `(B, G_kv, L, D)`——第一维就是批维。本讲的缓存收缩操作的就是这一维。
- **坐标系（本讲自造词）**：`_stop` 版生成函数里同时维护两套行编号——「全批坐标」（0 到 B−1，对应原始输入行）与「幸存者坐标」（还在生成的行的紧凑编号）。理解这两套坐标的换算，是读懂该函数的关键。

回顾 u2-l5 的对照点：标准 `qwen3.py` 用模型级游标 `current_pos` 追踪位置；本讲的 `qwen3_batched.py` **没有这个游标**，位置直接从缓存长度推导。这是两套实现最有趣的架构差异之一。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [reasoning_from_scratch/qwen3_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py) | 本讲主角：padding 感知的批量 Qwen3 实现，含 4 个批量生成函数与缓存收缩工具 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 标准单条实现，被 `qwen3_batched` 复用 `KVCache`、`Qwen3Tokenizer` 与下载函数；用于对照 |
| [chE/chE_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chE/chE_main.ipynb) | 附录 E「批处理与吞吐导向执行」教学 notebook：使用示例、掩码图解、各章批量脚本与基准表 |
| [tests/test_qwen3_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched.py) | 断言「批量 vs 单条」「普通 vs 流式」生成结果一致的回归测试 |
| [tests/test_qwen3_batched_stop.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched_stop.py) | 断言「普通批量 vs 逐行提前停止」两个版本 token 级一致的回归测试 |
| [ch03/02_math500-verifier-scripts/evaluate_math500_batched.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py) | 批量版 MATH-500 评测脚本，把本讲的生成函数接入第 3 章评测链路 |

## 4. 核心概念与源码讲解

### 4.1 left-padding 与三合一掩码：qwen3_batched.Qwen3Model.forward

#### 4.1.1 概念说明

批量生成要解决的核心矛盾：**张量必须矩形，prompt 长度天然不齐**。

方案的直觉有三条：

1. **pad 放左边**。生成永远发生在矩阵右端——prefill 取 `[:, -1]` 位置的 logits 作为「下一个 token」的预测，decode 时新 token 也接在右端。左填充保证对每一行，`[:, -1]` 都落在真实 token 上、新 token 与 prompt 末尾物理相邻；若用右填充，短行的 `[:, -1]` 会落在 pad 上，预测位置就错了。
2. **pad 键要挡住**。pad token 不是真实输入，不能参与 softmax 分母，否则会稀释真实 token 的注意力权重。办法是把 `attn_mask` 的 pad 列并进掩码。
3. **pad 查询要置零**。pad 位置自己的 Q/K/V 也会被算出来并写进缓存；把它们的嵌入置零后，线性投影（无 bias）输出精确零向量，缓存里的 pad 条目就是无害的零。

`qwen3_batched.Qwen3Model` 与标准版同名同构（可以直接 `load_state_dict` 同一份权重），差异全部集中在「感知 padding」上。它的 docstring 还交代了 `float32_upcast` 参数（4.3 节详解）。

#### 4.1.2 核心流程

`forward` 的流程在标准版骨架上多了三处批量处理：

```text
输入 in_idx (B, T)，可选 cache 与 attn_mask
 1. 嵌入查表 → x (B, T, D)
 2. 位置起点：pos_start = 缓存第 0 层 K 的长度 L_prev（无缓存则 0）
    位置终点：pos_end = pos_start + T，超过 context_length 抛 ValueError
 3. 因果掩码：base = 严格上三角 (pos_end, pos_end)
    causal4d = base[pos_start:pos_end, :pos_end]   ← 新 token 只被禁止看到严格晚于它的位置
 4. 填充掩码（若 attn_mask 含 False）：
    kpm = (attn_mask[:, :pos_end] == 0) 展开成 (B, 1, 1, pos_end)
    mask = causal4d | kpm                            ← 两者取或，一次 masked_fill 全挡掉
 5. 按行位置编码：pos_ids = arange(pos_start, pos_end) 广播到 (B, T)
 6. pad 查询置零：x = x * qmask（qmask 取 attn_mask 的 [pos_start:pos_end] 段）
 7. 逐层跑 TransformerBlock，传入 mask、cos、sin、cache、pos_ids
 8. final_norm → out_head → logits (B, T, vocab)
```

注意掩码语义沿用了全书约定：`True` 表示「屏蔽」，`masked_fill` 填 `-inf`。

#### 4.1.3 源码精读

先看位置起点的推导——这里没有 `current_pos` 游标，**缓存长度即位置**：

- [reasoning_from_scratch/qwen3_batched.py:L82-L93](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L82-L93)：从第 0 层缓存的 K 张量读出 `pos_start = prev_k0.size(2)`，再算 `pos_end` 并做上下文越界检查。对比标准版由 `model.current_pos` 显式记录（u2-l5），批量版让缓存自身成为唯一事实来源——这也解释了为何它的 `reset_kv_cache` 是空操作（见 [L128-L130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L128-L130)，仅为兼容旧生成函数的调用习惯而保留）。

- [reasoning_from_scratch/qwen3_batched.py:L95-L99](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L95-L99)：构造 `(pos_end, pos_end)` 严格上三角后切片 `[pos_start:pos_end, :pos_end]`，得到形状 `(num_tokens, pos_end)` 的行切块——行是「本次输入的 query」，列是「缓存里的全部 key」。prefill 时它还原为完整上三角；decode 时（`num_tokens=1`）是一行宽度 `pos_end` 的宽行。这与 u2-l5 讲过的切片语义完全一致。

- [reasoning_from_scratch/qwen3_batched.py:L101-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L101-L107)：`has_pad` 先判断本批是否真的含 pad（整批等长时跳过合并，省一次广播）；含 pad 时把 `attn_mask` 取反后 `view(B, 1, 1, pos_end)`，与因果掩码取或。注意 `kpm` 沿 key 列方向展开——**pad 是按「列」被挡的**，同一行内所有 query 都看不到这些列。

- [reasoning_from_scratch/qwen3_batched.py:L109-L114](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L109-L114)：`pos_ids_current` 用 `arange(pos_start, pos_end)` 广播出 `(B, num_tokens)`（为什么共享编号也正确，见 4.2 节）；随后 `qmask` 把 pad 位置的嵌入乘零。由于所有投影线性层都无 bias，`W_key·0 = 0`，pad 位置写入缓存的 K/V 是精确零向量——即使掩码逻辑有疏漏，零键也不会注入任何信息，属于纵深防御。

#### 4.1.4 代码实践

**实践目标**：不加载权重，徒手复现掩码合并，看清「因果切片 + 填充列」合成后的样子。

1. 操作步骤（示例代码，仅需 torch）：

```python
import torch

pos_end, pos_start, B = 5, 3, 2          # 模拟 decode 第 3 步：缓存已有 3 个 token
base = torch.triu(torch.ones(pos_end, pos_end, dtype=torch.bool), diagonal=1)
causal4d = base[pos_start:pos_end, :pos_end][None, None]     # (1,1,2,5)
attn_mask = torch.tensor([[True, True, True, True, True],    # 行 0：无 pad
                          [False, False, True, True, True]])  # 行 1：行首 2 个 pad
kpm = (attn_mask[:, :pos_end] == 0).view(B, 1, 1, pos_end)
mask = causal4d | kpm
print(mask.shape); print(mask[0, 0].int()); print(mask[1, 0].int())
```

2. 需要观察的现象：两行的掩码差异恰好只出现在前两列。
3. 预期结果：形状 `(2, 1, 2, 5)`；行 0（无 pad）两个 query 的可见列为 `{0..3}` 与 `{0..4}`（因果限制）；行 1 的可见列被 `kpm` 再砍掉 0、1 两列，第一个 query 只能看到列 2、3。pad 只能减少可见列，永远不会放开因果限制。
4. 此实践不依赖权重与网络，可直接运行核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么本实现选择左填充而不是右填充？给出至少两条理由。

答案：① 生成发生在右端：prefill 取 `[:, -1]` 的 logits，左填充保证它对每一行都是真实 token 的下一位置预测；② decode 阶段新 token 拼在整批右端，左填充使每行「prompt 末 token → 生成 token」物理相邻，行内相对位置连续；③ 若右填充，pad 夹在 prompt 与生成 token 中间，既污染 `[:, -1]` 的语义，又打乱行内相对距离。

**练习 2**：`has_pad` 为 False 时（整批等长），掩码退回纯因果切片。等长 prompt 的批量采样（第 4 章自一致性的 batched 脚本）还需要 `qwen3_batched` 吗？

答案：不需要。附录 E.5 明确说明：`self_consistency_math500_batched.py` 把同一 prompt 重复 n 次、天然等长无 padding，因此直接用标准 `qwen3.Qwen3Model`。`qwen3_batched` 只在「混入不同长度 prompt」时才有必要。

**练习 3**：pad 位置的嵌入被置零后，它在缓存中的 K/V 是什么？为什么说这是「无害」的？

答案：精确零向量。因为 K/V 投影是无 bias 的线性层，零输入得零输出；RMSNorm 与 RoPE 作用于零向量仍是零。零键即使意外参与注意力也只贡献零值，且 `kpm` 掩码本就把它挡在 softmax 之外——双保险。

### 4.2 按行位置编码：apply_rope_with_pos_ids 与 RoPE 相对性

#### 4.2.1 概念说明

left-padding 带来一个位置编码疑问：短行真实 token 的「绝对位置」被 pad 推后了。比如行首有 3 个 pad，第一个真实 token 在矩阵第 3 列——它该用位置 3 的角度旋转，还是位置 0？

本实现的回答是：**不做行级修正，统一用列号当位置号**（`arange(pos_start, pos_end)` 广播到所有行）。这之所以正确，靠的是 u2-l4 建立的 RoPE 相对性：注意力分数 \( q_m^\top k_n \) 只依赖位置差 \( m - n \)。把某行**所有** token 的位置号同时加上常数 \( P \)（该行的 pad 数），任意两 token 的位置差不变：

\[ (P + i) - (P + j) = i - j \]

于是「整体平移」不改变行内任何注意力分数；pad 位置虽然占用了前 \( P \) 个位置号，但它们被掩码挡住、被置零，从不参与有效注意。最终批量输出与「位置从 0 开始的单条生成」数学等价——测试也逐 token 断言了这一点（见 4.2.3）。

`apply_rope_with_pos_ids` 本身则是一个**支持按行位置的通用接口**：`cos[pos_ids]` 用 `(B, L)` 的位置矩阵做花式索引，天然允许每行取不同的位置序列。当前 `forward` 传入的是共享 `arange`，但接口为「真正需要行级位置」的场景（例如右填充或更复杂的拼接）留好了能力。

#### 4.2.2 核心流程

```text
apply_rope_with_pos_ids(x, cos, sin, pos_ids):
  x: (B, H, L, D)   pos_ids: (B, L)
 1. cos_sel = cos[pos_ids] → (B, L, D)，再 unsqueeze(1) → (B, 1, L, D) 广播到所有头
 2. split-halves 旋转：x1 = 前半, x2 = 后半, rotated = cat(-x2, x1)
 3. x_rotated = x * cos_sel + rotated * sin_sel，保持原 dtype 返回
```

与标准版 `qwen3.apply_rope(x, cos, sin, offset)` 的对比：标准版用一个标量 `offset` 整体平移（单条 decode 够用）；批量版把「平移量」泛化成每行每位置的矩阵 `pos_ids`。旋转公式本身（split-halves 风格、正交变换保范数）与 u2-l4 完全一致。

#### 4.2.3 源码精读

- [reasoning_from_scratch/qwen3_batched.py:L301-L311](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L301-L311)：`cos[pos_ids]` 按 `(B, L)` 索引取行，`unsqueeze(1)` 对齐头维；随后是与标准版 [qwen3.py:L302](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L302) 相同的 split-halves 旋转。

- [reasoning_from_scratch/qwen3_batched.py:L236-L238](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L236-L238)：GQA 前向中 Q、K 用**同一个** `pos_ids` 旋转——这正是相对性论证的前提：query 与 key 必须处于同一套（哪怕整体平移过的）坐标系。

- [tests/test_qwen3_batched.py:L57-L138](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched.py#L57-L138)：等价性测试。三条不同长度 prompt 左填充成批，用批量模型逐条跑 `generate_text_basic_cache` 与批量跑 `generate_text_basic_batched_cache`，断言解码文本一致（Torch 2.11 的 MPS 上因近并列 token 易翻转，只比较前 60 个字符，见 [L42-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched.py#L42-L54)）。这条测试就是「共享列号 + 掩码 = 单条等价」命题的机器验证。

#### 4.2.4 代码实践

**实践目标**：用小张量直接验证两个性质——旋转保范数、Q/K 同步平移不改变注意力分数。

1. 操作步骤（示例代码，无需权重）：

```python
import torch
from reasoning_from_scratch.qwen3_batched import apply_rope_with_pos_ids, compute_rope_params

cos, sin = compute_rope_params(head_dim=8, theta_base=10_000, context_length=16)
torch.manual_seed(0)
x = torch.randn(1, 1, 3, 8)                       # (B, H, L, D)

pos0 = torch.tensor([[0, 1, 2]])
pos1 = pos0 + 2                                    # 模拟行首多了 2 个 pad 的整体平移
r0 = apply_rope_with_pos_ids(x, cos, sin, pos0)
r1 = apply_rope_with_pos_ids(x, cos, sin, pos1)

print(torch.allclose(r0.norm(dim=-1), x.norm(dim=-1), atol=1e-5))   # 性质 1
s0 = r0 @ r0.transpose(-1, -2)
s1 = r1 @ r1.transpose(-1, -2)
print(torch.allclose(s0, s1, atol=1e-5))                            # 性质 2
```

2. 需要观察的现象：两个 `allclose` 的输出。
3. 预期结果：均为 `True`（性质 1 由旋转的正交性精确成立；性质 2 在浮点误差内成立）。若个别元素略超容差，可放宽 `atol` 观察误差量级。
4. 具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：批量版为什么必须让 Q 和 K 使用同一个 `pos_ids`？

答案：RoPE 的注意力分数只依赖位置差。若 Q 用平移后的位置而 K 用原始位置（或各自平移量不同），同一对 token 的位置差会被人为改变，注意力分数随之改变，批量结果就不再等价于单条生成。

**练习 2**：既然 `apply_rope_with_pos_ids` 支持每行不同位置，能否改成「每行真实 token 从 0 开始编号」的精确行级位置？两种方案等价吗？

答案：能，且数学上与「共享列号 + 整体平移」等价——因为行级编号等于把每行位置整体减去该行 pad 数，位置差不变。共享列号胜在实现简单：一个 `arange` 广播即可，无需维护每行偏移。

**练习 3**：decode 阶段（`num_tokens=1`）`pos_ids` 是多少？它与该行的平移量一致吗？

答案：是共享的 `pos_start`（缓存长度）。对行首有 \( P \) 个 pad 的行，这相当于「pad 数 + 真实 prompt 长 + 已生成步数」，与该行 prefill 时的平移坐标系一致——新 token 一直待在同一套平移坐标系里，相对关系始终正确。

### 4.3 数值稳定性：float32_upcast 与 padding 稳定 softmax

#### 4.3.1 概念说明

`Qwen3Model(cfg, float32_upcast=True)` 默认把注意力打分与 softmax 全路径放在 float32。docstring 给出动机：批量实现把因果掩码与填充掩码合并，比单条路径「数值上更脆弱」，长序列或重填充批次尤甚；float32 让结果与无 padding 的单条版等价，代价是更慢、更占显存，可用 `float32_upcast=False` 退出（[reasoning_from_scratch/qwen3_batched.py:L28-L45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L28-L45)）。

脆弱性来自两处：

1. **全掩码行的 NaN 风险**。pad 查询行在因果+填充合并掩码下可能整行全 `True`，`masked_fill(-inf)` 后整行 `-inf`。`torch.softmax` 对全 `-inf` 行算出 \( 0/0 = \text{NaN} \)，NaN 会沿残差流污染整行乃至后续层。
2. **bf16 的精度风险**。模型权重是 bfloat16（约 7 位尾数）。批量批次打分矩阵更大、近并列的 top logits 更多，微小舍入差在贪心 argmax 处翻转选 token，并在后续自回归步骤中放大成完全不同的文本——这正是测试注释里「几个 token 后轻微发散」的来源（[tests/test_qwen3_batched.py:L141-L146](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched.py#L141-L146)）。

对策是**手写 padding 稳定 softmax**：不减 `torch.softmax`，而是显式做减行最大值、二次掩码、分母钳制三步。

#### 4.3.2 核心流程

```text
GQA 打分与 softmax（float32_upcast=True 时）：
 1. Q、K 升 float32 后做矩阵乘，除以 √head_dim
 2. masked_fill(mask, -inf)
 3. row_max = 每行最大值；若为 -inf（全掩码行）替换为 0
 4. exp_scores = exp(scores - row_max)，再做一次 masked_fill(mask, 0.0)
 5. denom = 行求和，clamp(min=tiny) 防除零
 6. attn_weights = exp_scores / denom —— 全掩码行得到精确全零
 7. 权重转回 values 的 dtype（bf16），与 V 相乘得 context
```

数学上，减行最大值是标准的 log-sum-exp 稳定化技巧；对全掩码行，步骤 3 与 5 联手把「NaN」改写成「0 / tiny = 0」。

#### 4.3.3 源码精读

- [reasoning_from_scratch/qwen3_batched.py:L251-L259](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L251-L259)：`score_dtype` 由 `float32_upcast` 决定，Q/K 升精度后打分、缩放、`masked_fill(-inf)`——注释明言用 `-inf` 是为了掩码位置经 softmax 后**恰好为零**。

- [reasoning_from_scratch/qwen3_batched.py:L261-L276](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L261-L276)：手写稳定 softmax 主体；最后 `attn_weights.to(values.dtype)` 只在打分路径被升精度时把权重降回 V 的精度，保证 context 矩阵乘在原 dtype 完成。

- [tests/test_qwen3_batched.py:L141-L210](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched.py#L141-L210)：非常值得精读的测试工程——为了让**标准版模型**与批量版 bit 级一致，测试用 monkeypatch 把标准版 `GroupedQueryAttention.forward` 替换成同样「fp32 打分 + 同一套稳定 softmax」的实现（`new_forward`），否则两者的浮点微差会在若干 token 后发散。它示范了一个通用原则：**等价性断言要求两端数值路径完全同构，而非仅仅数学等价**。

#### 4.3.4 代码实践

**实践目标**：亲眼对比 `torch.softmax` 与手写稳定 softmax 对「全 `-inf` 行」的处理。

1. 操作步骤（示例代码，仅需 torch）：

```python
import torch
row = torch.full((1, 4), -torch.inf)                 # 模拟全掩码的 pad 查询行
print(torch.softmax(row, dim=-1))                    # 原生 softmax

keep = torch.isfinite(row)
row_max = torch.where(keep, row, torch.zeros_like(row))
e = torch.exp(row - row_max).masked_fill(~keep, 0.0)  # 模拟源码 L264-L265
w = e / e.sum(dim=-1, keepdim=True).clamp(min=torch.finfo(e.dtype).tiny)
print(w)                                             # 手写稳定 softmax
```

2. 需要观察的现象：两次输出的差异。
3. 预期结果：`torch.softmax` 输出 `nan`；手写版输出全 `0`。若你的 torch 版本行为不同，记录实际输出。
4. 此实践不依赖权重，可直接运行核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `row_max` 要用 `torch.where(torch.isfinite(...), ..., 0)` 兜底？直接用 `-inf` 会怎样？

答案：全掩码行的 `row_max` 是 `-inf`，`exp(-inf - (-inf)) = exp(nan)` 会产生 NaN。把 `-inf` 的行最大值替换为 0 后，`exp(-inf - 0) = 0`，配合分母钳制得到干净的全零行。

**练习 2**：`float32_upcast=True` 会改变模型的参数 dtype 吗？

答案：不会。权重仍按配置的 bfloat16 存储；只有注意力打分与 softmax 这段**中间计算**临时升到 float32，算完注意力权重后立刻转回原 dtype（L271）。开销是速度与峰值显存，收益是与单条版数值一致。

**练习 3**：既然手写 softmax 已经解决 NaN，为什么还要默认开 `float32_upcast`？

答案：NaN 只是最严重的失败模式；bf16 打分还有「近并列 logits 翻转 argmax」的慢性病，会让批量与单条生成在若干 token 后分岔。fp32 打分把舍入误差压到可忽略，等价性测试才能严格成立。docstring 把它定位为「批量生成与评测更安全的默认值」。

### 4.4 逐行提前停止：generate_text_basic_batched_cache_stop 与 shrink_kv_cache_inplace

#### 4.4.1 概念说明

普通批量版 `generate_text_basic_batched_cache`（[L338-L402](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L338-L402)）的问题：不同序列在不同时刻产出 EOS，但张量必须保持矩形，所以**已结束的行也得继续陪跑**——每步把它们的新 token 强制写成 EOS（[L389-L392](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L389-L392)），白白消耗算力与显存。

`generate_text_basic_batched_cache_stop`（docstring 自述见 [L474-L479](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L474-L479)）的思路：**已结束的行直接退出计算批次**。它维护两套坐标系：

- 全批坐标 `finished_full` / `active_idx`：长度 B，回答「原始第几行是否结束、还剩哪些行」；
- 幸存者坐标 `keep_mask_active` / `cur_attn_active` / `cache`：长度随结束行移出而递减，是真正参与前向的紧凑批次。

每一步把幸存者的 argmax 结果用 `index_copy_` 散射回全批坐标的 `step_full`（已结束行填 EOS），对外输出形状恒为 `(B, 1)`；对内前向的批维则逐步缩小。附录 E 用一张图对比了两个版本（普通版全批陪跑、`_stop` 版批次瘦身）。

配套的 `shrink_kv_cache_inplace` 负责把缓存也瘦下来：注意力从不跨行交互，删掉第 b 行的缓存条目不影响其他行的数值结果；而已结束行的缓存永远不会再被消费，留着只是显存包袱。

#### 4.4.2 核心流程

```text
generate_text_basic_batched_cache_stop:
 0. 由 pad_id 推导 attn_mask；新建 KVCache；整批 prefill 一次，取 [:, -1]
 1. 初始化：finished_full 全 False；active_idx = [0..B)；cur_attn_active = attn_mask
 2. 循环 max_new_tokens 步：
    a. 幸存者 argmax → next_token_active (B_active, 1)
    b. 造全批 step_full (B,1)：已结束行填 EOS，index_copy_ 写入幸存者结果；append
    c. 记账：刚刚产出 EOS 的幸存行 → finished_full 置 True
    d. 若全部结束 → break
    e. keep_mask_active = ~newly_finished；无幸存者 → break
    f. 过滤：next_token、active_idx、cur_attn_active 只留幸存者；
       attn 掩码尾部追加一列 1（新 token 对所有行都是真实 token）
    g. shrink_kv_cache_inplace：逐层把 K/V 沿批维索引 keep_mask_active
    h. 仅对幸存者前向一步
 3. 拼接全部 step_full 返回 (B, L_generated)——含每行自己的 EOS
```

注意输出语义：触发 EOS 的那一步在 break 之前已 append，因此 **EOS 本身含在返回序列里**；decode 消费方按第一个 EOS 截断（附录 E 示例即如此）。

#### 4.4.3 源码精读

- [reasoning_from_scratch/qwen3_batched.py:L485-L498](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L485-L498)：`attn_mask` 可由 `pad_id` 自动推导（`token_ids != pad_id`）；prefill 一次后初始化两套坐标。对比普通版 [L352-L368](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L352-L368)，那里只有一个全批 `finished` 向量，已结束行继续参与前向。

- [reasoning_from_scratch/qwen3_batched.py:L500-L509](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L500-L509)：双坐标系的核心三行——argmax 出的是**幸存者坐标**的 token，`torch.full` 造全批形状的 `step_full`（填 EOS 或 0），`index_copy_(0, active_idx, ...)` 散射回原位。输出永远矩形，计算批次却在缩。

- [reasoning_from_scratch/qwen3_batched.py:L513-L537](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L513-L537)：记账与幸存者过滤。`index_put_` 只更新 `active_idx` 指向的行（已移出的行状态不变、也无需变）；`eos_token_id=None` 时 `newly_finished_active` 恒 False，整个函数退化为普通批量版。`keep_mask_active` 若是零维张量有单独分支（B_active=1 被压缩成标量的边界情况）。

- [reasoning_from_scratch/qwen3_batched.py:L540-L551](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L540-L551)：三件事同步收缩到幸存者坐标——attn 掩码先按行过滤再**尾部追加一列 1**（生成 token 永远真实）；缓存收缩；最后只对幸存者前向。掩码、缓存、输入三者的批维必须同时是 `B_surv`，这是不变式。

- [reasoning_from_scratch/qwen3_batched.py:L452-L462](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L452-L462)：`shrink_kv_cache_inplace` 全文——逐层取 `(K, V)`，`K[keep_mask]` / `V[keep_mask]` 沿第 0 维（批维）布尔索引后写回。注意「inplace」指**原地修改 cache 对象**（布尔索引本身会拷贝出新张量）；跳过 `None` 层是防御未填充的层。复用的 `KVCache` 来自标准版 [qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)，接口只有 `get/update`，天然兼容这种批维改写。

- [tests/test_qwen3_batched_stop.py:L34-L108](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched_stop.py#L34-L108)：`_stop` 版与普通版对同一批输入产出**token 级一致**的结果（`reg_toks == stop_toks` 逐行断言）——瘦身只省算力，不改输出。[L111-L196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched_stop.py#L111-L196) 对流式变体 `generate_text_basic_batched_stream_cache_stop`（[qwen3_batched.py:L560-L637](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L560-L637)）做同样断言。

最后用附录 E.8 的真实基准量化收益（H100 / DGX Spark，MATH-500 全量 500 题，数据来自 [chE/chE_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chE/chE_main.ipynb) E.8 节）：

| 脚本 | batch | RAM | H100 耗时 | 加速比 | DGX Spark 耗时 |
|---|---|---|---|---|---|
| evaluate_math500.py | 单条 | 1.8 GB | 90.0 min | — | 174.7 min |
| evaluate_math500_batched.py | 64 | 23.39 GB | 16.0 min | ≈5.6× | 108.4 min（≈1.6×） |
| self_consistency_math500(_batched).py | 3 | 1.79→2.45 GB | 252→129 min | ≈2.0× | 340.8→243.3 min |
| rlvr_grpo_original_no_kl(_batched).py | 4 | 43.35→44.91 GB | 68→19 min | ≈3.6× | 63.7→23.1 min |
| distill(_batched).py | 4 | 8.29→8.34 GB | 10.9→9.1 min | ≈1.2× | 32.8→28.2 min |

可见：加速比取决于「生成占主导程度 + 批内长度是否齐整」，评测类（长生成、异长 prompt）收益最大；蒸馏（已有前向为主）收益最小。显存是主要代价（batch 64 时 RAM 从 1.8 GB 涨到 23.39 GB）。另外 [evaluate_math500_batched.py:L198-L205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L198-L205) 默认导入的就是 `_stop` 版，`--disable_efficient_mode` 才切回普通版——作者把「逐行提前停止」当批量评测的默认高效路径。

#### 4.4.4 代码实践

**实践目标**：不看权重，直接操作假缓存验证 `shrink_kv_cache_inplace` 的行为。

1. 操作步骤（示例代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import KVCache
from reasoning_from_scratch.qwen3_batched import shrink_kv_cache_inplace

cache = KVCache(n_layers=1)
K = torch.arange(16).float().reshape(2, 1, 8, 1)      # (B=2, G_kv, L, D)
cache.update(0, (K, K.clone()))
shrink_kv_cache_inplace(cache, torch.tensor([True, False]), n_layers=1)
print(cache.get(0)[0].shape)                           # 期望 torch.Size([1, 1, 8, 1])
print(cache.get(0)[0].flatten()[:4])                   # 期望保留行 0 的前 4 个值 0,1,2,3
```

2. 需要观察的现象：收缩前后批维从 2 变 1，且保留的是 `keep_mask` 为 True 的**第 0 行**内容。
3. 预期结果：形状 `[1, 1, 8, 1]`，值为行 0 原值。此实践不依赖权重，可直接运行核对。
4. 思考题（答案见练习 3）：`keep_mask` 的长度应该等于全批 B 还是幸存者 B_active？

#### 4.4.5 小练习与答案

**练习 1**：为什么 `shrink_kv_cache_inplace` 只收缩「还在生成」的行是安全的？

答案：三个理由。① 已结束行的缓存永远不会再次前向——它们的剩余输出位由 `step_full` 的 EOS 填充产生，不再消费缓存；② 自注意力的计算在批维上各行独立，K/V 沿批维切片不改变任何幸存行的数值；③ 删除死行让后续每步前向的批维变小，直接省算力与显存。

**练习 2**：普通批量版把已结束行的新 token 强制写成 EOS（L389-L392）是为了什么？`_stop` 版还需要这个技巧吗？

答案：为了维持张量矩形——已结束行必须有个占位 token 才能和幸存行拼成 `(B, 1)` 参与下一步前向。`_stop` 版把占位技巧移到了输出侧：已结束行不进前向，`step_full` 直接以 EOS 填充（L505-L508），对外仍保持 `(B, 1)` 矩形输出，对内不再浪费计算。

**练习 3**：若误把**全批坐标**的 `finished` 掩码（长度 B）传给 `shrink_kv_cache_inplace` 会怎样？

答案：布尔索引要求索引长度等于被索引维的大小。第二轮起缓存批维已是 `B_active < B`，长度 B 的掩码会直接抛索引错误；即使长度碰巧相等（第一轮），语义也是错位的——会把「原始行号」当「幸存者行号」用，可能删错行。

**练习 4**：`_stop` 版返回的 token 序列里包含 EOS 吗？消费方应如何处理？

答案：包含——触发 EOS 的那步在 break 检查前已 append。消费方应按每行**第一个** EOS 截断后再 decode（附录 E 示例对每行做 `(row == eos).nonzero()` 切片），其后的 EOS 全是占位填充。

## 5. 综合实践

**任务**：对 4 条不同长度的 prompt 做一次批量生成，验证逐行提前停止的正确性，并实测「逐条 ×4」对「批量 ×1」的加速比。完整脚本（示例代码，仿照 [tests/test_qwen3_batched_stop.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_batched_stop.py) 与附录 E.2 的用法编写）：

```python
import time
import torch

from reasoning_from_scratch.ch02 import get_device, generate_text_basic_cache
from reasoning_from_scratch.ch03 import render_prompt
from reasoning_from_scratch.qwen3_batched import (
    load_model_and_tokenizer,
    generate_text_basic_batched_cache_stop,
)

device = get_device()
model, tokenizer = load_model_and_tokenizer(
    which_model="base", device=device, use_compile=False,
)

problems = [
    "1+1?",
    "2+2?",
    "What is 12*11?",
    "Explain what a prime number is, then list all primes below 20.",
]
prompts = [render_prompt(p) for p in problems]
tokenized = [tokenizer.encode(p) for p in prompts]
pad_id = tokenizer.pad_token_id
max_len = max(len(t) for t in tokenized)

# ---- 左填充成批 + attn_mask ----
left_padded = [[pad_id] * (max_len - len(t)) + t for t in tokenized]
attn_mask = [[0] * (max_len - len(t)) + [1] * len(t) for t in tokenized]
input_ids = torch.tensor(left_padded, dtype=torch.long, device=device)
attn_mask = torch.tensor(attn_mask, dtype=torch.bool, device=device)

def sync():
    if device.type == "cuda":
        torch.cuda.synchronize()          # GPU 异步，计时前先同步（承接 u3-l2）

def cut_at_eos(row):
    pos = (row == tokenizer.eos_token_id).nonzero(as_tuple=True)[0]
    return row[:pos[0]] if len(pos) else row

# ---- 路线 A：逐条生成 4 次（批量模型 + 单条生成函数，与等价性测试同款组合）----
sync(); t0 = time.perf_counter()
texts_single = []
for p in prompts:
    ids = torch.tensor(tokenizer.encode(p), dtype=torch.long, device=device).unsqueeze(0)
    out = generate_text_basic_cache(
        model=model, token_ids=ids,
        max_new_tokens=64, eos_token_id=tokenizer.eos_token_id,
    )
    texts_single.append(tokenizer.decode(out[0].tolist()))
sync(); t_single = time.perf_counter() - t0

# ---- 路线 B：批量生成 1 次（逐行提前停止版）----
sync(); t0 = time.perf_counter()
gen = generate_text_basic_batched_cache_stop(
    model=model, token_ids=input_ids, max_new_tokens=64,
    eos_token_id=tokenizer.eos_token_id, attn_mask=attn_mask, pad_id=pad_id,
)
sync(); t_batch = time.perf_counter() - t0

texts_batch = [tokenizer.decode(cut_at_eos(row).tolist()) for row in gen]

# ---- 验证与报告 ----
for i, (a, b) in enumerate(zip(texts_single, texts_batch)):
    print(f"[{i}] 单条==批量: {a == b}  | {b!r}")
eos_cols = (gen == tokenizer.eos_token_id).nonzero()
print("各行首个 EOS 列号:", {int(r): int(c) for r, c in eos_cols.tolist()})
print("逐条合计 {t_single:.2f}s | 批量 {t_batch:.2f}s | 加速比 {t_single / t_batch:.2f}x")
```

**操作步骤**：

1. 按上述脚本保存为 `batched_bench.py`（放在你的实验目录，不要写进仓库源码目录），在已安装本包的环境运行（`uv run` 或 `python`）。首次运行会下载 0.6B base 权重（约 1.5 GB）。
2. 观察三组输出：① 每行「单条 == 批量」是否为 True；② 各行首个 EOS 出现的列号是否不同（短问题早停、长问题晚停）；③ 两条路线的耗时与加速比。
3. 检查「逐行停止」是否真生效：打印 `gen` 的每行，确认第一个 EOS 之后整行都是 EOS 填充；对照 4 个不同的 EOS 列号，体会「同行内 EOS 后全是填充、行与行之间停止步数各异」。
4. 回答本讲的定性问题（对照 4.4.3 的源码）：`shrink_kv_cache_inplace` 为何只收缩还在生成的行？用你自己的话写 3 句，对照练习 1 的答案修正。

**预期结果**：

- 正确性：4 行均应「单条 == 批量」为 True（若你的设备浮点路径导致个别行在近并列 token处分岔，记录差异并对照 4.3 节解释——测试在 Torch 2.11 MPS 上就只敢比前 60 个字符）。
- EOS 行为：各行首个 EOS 列号大概率不同，其后全为 EOS。
- 加速比：**待本地验证**，且强烈依赖设备——GPU 上参考附录 E.8 的方向（H100 上批量评测约 5.6×），CPU 上可能无加速甚至变慢（附录 E.1 的明确警告）；4 条的小批加上 prefill 占比高，加速比会低于大基准。把你的实测数与 E.8 表格放在一起讨论差异来源（批大小、长度齐整度、prefill/decode 占比）。

## 6. 本讲小结

- 批处理优化的是吞吐而非延迟；收益不是免费的——显存上涨（batch 64 时 1.8→23.39 GB），CPU 或弱 GPU 上可能反而变慢。
- left-padding 让「生成永远在右端」对每行成立：`[:, -1]` 总是真实 token 的下一位置预测，新 token 与 prompt 物理相邻；pad 键被 `attn_mask` 并入掩码挡在 softmax 之外，pad 查询行嵌入置零，使其缓存条目成为无害的精确零。
- 位置编号共享列号即可：RoPE 注意力分数只依赖位置差，某行整体平移 \( P \) 不改变行内差值；`apply_rope_with_pos_ids` 的 `(B, L)` 花式索引还保留了真正按行编码的接口能力。
- `float32_upcast=True` 默认开启：手写稳定 softmax（行最大值兜底 + 分母钳制）消灭全掩码行的 NaN，fp32 打分抑制 bf16 近并列翻转；等价性测试为此把单条版 patch 成完全相同的数值路径。
- `generate_text_basic_batched_cache_stop` 用双坐标系（全批输出、幸存者计算）实现逐行提前停止：`index_copy_` 散射保持输出矩形，`shrink_kv_cache_inplace` 沿批维删除死行使后续前向瘦身；两版输出 token 级一致，测试锁死。

## 7. 下一步学习建议

- **u10-l1（GPU 优化实现）**：本讲的 `qwen3_batched` 仍是手写注意力 + 列表式缓存；`qwen3_optimized.py` 用 SDPA 融合内核与预分配 KVCache 进一步压榨单卡性能，两讲合起来就是完整的推理性能专题。
- **重读 u4-l4 的批量评测**：`evaluate_math500_batched.py` 默认就用本讲的 `_stop` 生成函数（L198-L205），可回头把第 3 章评测链路与本讲的吞吐数据串成一条线。
- **延伸阅读源码**：`generate_text_basic_batched_stream_cache_stop`（[qwen3_batched.py:L560-L637](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_batched.py#L560-L637)）把逐行停止与流式输出合在一起，`yield` 发生在缓存收缩之前——值得对照 4.4 节自己推一遍每步的张量形状。
- 若你关注训练侧吞吐，可对照 ch06 的 `sample_responses_batched`（等长 rollout 复制成批，无需 padding）体会「等长批」与「异长批」两条工程路线的分野。
