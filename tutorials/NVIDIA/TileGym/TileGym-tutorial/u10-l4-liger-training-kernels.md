# Liger 训练内核族：损失、归一化与融合算子

> 所属单元：U10 进阶机制与生态 · 第 4 讲
> 依赖讲义：u10-l1（Suites 扩展机制）、u4-l2（Autograd 集成与反向内核）

## 1. 本讲目标

本讲聚焦 TileGym 中最活跃的训练向套件 —— **liger suite** 本轮大幅扩容的「训练内核族」。读完本讲你应当能够：

- 说清 `liger.grpo_loss` 这一个算子如何在一个 `(B, L)` 的 block 内同时完成 vocab 维 logsumexp、目标 token 的 `logp`、PPO 重要性比 `coef_1`、可选 KL，以及它如何把 GRPO/DAPO/CISPO/SAPO/VESPO 等一整套策略梯度算法变体塞进同一份内核；
- 区分「融合线性交叉熵」的两条路径（single-pass 与 chunked backward-in-forward），并理解 CE/JSD/KL/TVD 这族散度损失共用的一套行级归约骨架；
- 掌握三类归一化变体内核的写法差异：`fused_add_rms_norm`（残差相加 + RMSNorm）、`poly_norm`（多项式归一化）、`dyt`（动态 tanh，其实是激活却与归一化同构）；
- 把 u4-l2 学到的「autograd 反向重计算」套路在新内核中复现：理解 swiglu/dyt 等激活内核为何在反向重新算一遍激活而不是保存中间结果。

## 2. 前置知识

本讲默认你已掌握以下概念（前序讲义已建立）：

- **Suites 命名空间机制**（u10-l1）：liger suite 的算子名带 `liger.` 前缀（如 `liger.grpo_loss`），对 dispatcher 而言点号只是普通字符、不被解析；`ops.py` 里用 `@dispatch` 声明统一签名的 stub，`cutile/` 目录下一算子一文件用 `@register_impl` 挂实现。
- **autograd.Function 封装内核**（u4-l2）：`forward`/`backward` 静态方法、`save_for_backward`、反向重计算策略、`backward` 返回值个数须与 `forward` 输入一一对应。
- **行级（row-parallel）grid**（u3 系列）：一个 block 算一行、grid 取行数的逐元素/归约内核模式，以及 `ct.gather`/`ct.scatter` 与在线（online）归约的写法。

几个本讲会用到的 RL/训练术语，先用一句话解释：

- **策略梯度（policy gradient）**：强化学习里「让好动作更可能出现」的优化方向。在 LLM 后训练（RLHF/GRPO）中，每个 token 的好坏由 `advantage`（优势）给出，损失要让高优势的 token 概率上升。
- **重要性比（importance ratio）**：`coef_1 = exp(logp - old_logp)`，即「当前策略给出该 token 的概率」相对「采样时旧策略给出该 token 的概率」的比值。PPO 用它做离线策略（off-policy）修正并加裁剪（clip）。
- **logsumexp / LSE**：`logsumexp(x) = m + log(Σ exp(xᵢ - m))`（`m=max(x)`），是 softmax 分母的对数，数值稳定。
- **KL 散度**：衡量两个分布差异的非负量；GRPO/DAPO 常用它把当前策略拉回参考策略，防止奖励作弊。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/suites/liger/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) | liger suite 的**统一接口层**：每个算子用 `@dispatch("liger.xxx")` 声明 stub，函数体只抛 `NotImplementedError`。本讲的入口都在这里。 |
| [src/tilegym/suites/liger/cutile/grpo_loss.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py) | 策略梯度损失族的 cuTile 实现：token-level 与 sequence-level（GSPO）两条前向/反向路径，含 logsumexp、重要性比、KL、多种 loss_type 分支。 |
| [src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py) | 融合线性 + 交叉熵：single-pass 与 chunked（backward-in-forward）两条路径，避免物化完整 `(BT, V)` logits。 |
| [src/tilegym/suites/liger/cutile/cross_entropy.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py) | 交叉熵内核：在线 logsumexp + 原地写梯度的「前向融合反向」；FLCE 与它复用。 |
| [src/tilegym/suites/liger/cutile/fused_add_rms_norm.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py) | 残差相加 + RMSNorm 的融合内核；反向用持久化 grid + 每 SM 局部 dW 归约。 |
| [src/tilegym/suites/liger/cutile/poly_norm.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/poly_norm.py) | 多项式归一化 PolyNorm：\(y=w_0\|\|x^3\|\|+w_1\|\|x^2\|\|+w_2\|\|x\|\|+b\)。 |
| [src/tilegym/suites/liger/cutile/dyt.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/dyt.py) | 动态 tanh 激活 \(y=\tanh(\alpha x)\gamma+\beta\)，反向用持久化 2D grid。 |
| [src/tilegym/suites/liger/cutile/swiglu.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py) | SwiGLU 激活 \(c=\text{silu}(a\cdot\text{gm})\cdot b\)；反向把 da/db 原地写回 A/B。 |
| [src/tilegym/suites/liger/cutile/kl_div.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/kl_div.py) | KL 散度损失：行级归约，对齐 chunk 走 `check_bounds=False` 快速路径。 |
| [src/tilegym/suites/liger/cutile/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py) | cuTile 实现的**注册门控**：导入即触发 `@register_impl` 注册副作用，受 `is_backend_available("cutile")` 门控。 |
| [tests/suites/liger/test_grpo_loss.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/suites/liger/test_grpo_loss.py) | grpo_loss 的正确性测试，含 PyTorch fp32 参考实现，是理解算子语义的最佳范例。 |

---

## 4. 核心概念与源码讲解

### 4.1 liger/ops.py：训练内核族的统一接口

#### 4.1.1 概念说明

u10-l1 讲过：suite 的算子名是「带点号前缀的字典键」。liger suite 把 Liger-Kernel 的一整批算子成体系接进 TileGym，由 [ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) 集中声明**统一接口**。这份文件里每一个函数都是「只抛 `NotImplementedError` 的 stub」，它存在的意义是：

1. **占名字**：把 `liger.grpo_loss` 这串字符注册成全局键；
2. **立签名**：规定所有后端实现必须遵守的参数列表与 docstring；
3. **算结果**：由 dispatcher 在运行时按当前后端查 `_REGISTRY` 路由到真正的实现。

本轮 liger suite 扩容后，ops.py 暴露的算子族可粗分为四类（这也是本讲四节的来源）：

| 类别 | 算子名（节选） |
| --- | --- |
| 策略梯度损失 | `liger.grpo_loss` |
| 分类/散度损失 | `liger.cross_entropy`、`liger.fused_linear_cross_entropy`、`liger.jsd`、`liger.fused_linear_jsd`、`liger.kl_div`、`liger.tvd` |
| 归一化变体 | `liger.fused_add_rms_norm`、`liger.rms_norm`、`liger.layer_norm`、`liger.group_norm`、`liger.poly_norm` |
| 激活 | `liger.swiglu`、`liger.geglu`、`liger.dyt`、`liger.softmax`、`liger.sparsemax` |

#### 4.1.2 核心流程

stub 长这样（以 grpo_loss 为例，完整签名很长，这里只看装饰与函数体）：

```python
@dispatch("liger.grpo_loss")
def grpo_loss(logits, old_logp, ref_logp, completion_ids, advantages, ...):
    """GRPO / DAPO / BNpo / DR-GRPO / CISPO / SAPO / LUSPO / VESPO ..."""
    raise NotImplementedError(f"grpo_loss is not implemented for {get_current_backend()}")
```

参见 [src/tilegym/suites/liger/ops.py:441-467](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L441-L467)。注意它与核心 `ops.py`（u2-l1）完全同构：同一套 `@dispatch`、同一套 `_REGISTRY`、同一套 wrapper 查表。区别只在于**组织**——suite 置于 `suites/liger/` 子树、须显式 `import` 才加载、stub 多不设 `fallback_backend`（缺失即直接报错，不优雅降级）。

#### 4.1.3 源码精读

注册发生在 cuTile 实现侧，由 `cutile/__init__.py` 的导入副作用触发，受后端可用性门控：

[src/tilegym/suites/liger/cutile/__init__.py:7-17](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py#L7-L17) 逐个 `from . import xxx` 导入算子模块——这一句执行时，各模块文件末尾的 `@register_impl("liger.xxx", backend="cutile")` 装饰器就会把实现挂进 `_REGISTRY["liger.xxx"]["cutile"]`。而 `suites/liger/__init__.py` 的这行做了门控：

```python
if is_backend_available("cutile"):
    from . import cutile as _cutile_impl
```

即「cuTile 后端不可用时整族实现都不注册」。这正是 u10-l1 强调的「注册是导入副作用、受门控、随运行环境而变」。

#### 4.1.4 代码实践

**实践目标**：确认 liger 命名空间算子名的注册形态。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   python -c "
   import tilegym
   from tilegym.suites import liger
   from tilegym.backend import get_registry_info
   for name in ['liger.grpo_loss','liger.cross_entropy','liger.swiglu','liger.dyt']:
       print(name, '->', get_registry_info(name))
   "
   ```

**需要观察的现象**：每个名字都对应一个字典，键为 `default` 与当前可用后端（如 `cutile`）。若 cuTile 后端不可用，相关后端键会缺失，调用即抛 stub 的 `NotImplementedError`。

**预期结果**：`liger.` 前缀对 dispatcher 完全透明，它就是普通字符串键；与你看到的 `get_registry_info('softmax')` 形态一致，只是名字带点。**待本地验证**（取决于机器是否装好 cuTile 后端）。

#### 4.1.5 小练习与答案

**练习**：为什么 liger 的 stub 几乎都不设 `fallback_backend`？请对照核心 ops.py 里 rms_norm 把 `fallback_backend="triton"` 的设计说明差异。

**参考答案**：核心 ops.py 里部分工具算子（rms_norm/rope/dropout）有完整 triton 实现可降级，故设 `fallback_backend="triton"` 优雅降级；而 liger suite 的这些训练损失/激活内核只有 cuTile 实现（个别有 triton），没有可靠的兜底实现，设 fallback 反而会静默跑错——缺失即直接 `NotImplementedError` 是更安全的策略。

---

### 4.2 策略梯度损失族：grpo_loss

#### 4.2.1 概念说明

`liger.grpo_loss` 是 liger suite 里**单个算子覆盖面最广**的一个：一个签名同时支持 GRPO、DAPO、BNPO、DR-GRPO、CISPO、SAPO、LUSPO、VESPO 八种策略梯度算法变体（见 [grpo_loss.py:6-9](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L6-L9) 的文件 docstring）。

要理解它，先看它的数学骨架。对每个（batch b, token 位置 l），给定：

- `logits[b, l+1, :]`：模型对该位置全词表 N 的打分（注意是 `L+1`，多了前缀位，见 4.2.4）；
- `old_logp[b, l]`：采样时旧策略给出该 token 的对数概率；
- `advantages[b]`：该样本的优势值；
- `completion_ids[b, l]`：实际生成的 token id。

算法先算「当前策略给出该 token 的对数概率」：

\[
\text{logp}_{b,l} = \frac{\text{logits}[b,l,\text{id}_{b,l}]}{T} - \text{lse}_{b,l},\qquad
\text{lse}_{b,l} = \text{logsumexp}_n\!\left(\frac{\text{logits}[b,l,n]}{T}\right)
\]

其中 T 是温度。再算重要性比与 PPO 损失（GRPO 形式）：

\[
\text{coef}_1 = \exp(\text{logp} - \text{old\_logp}),\qquad
\text{coef}_2 = \text{clip}(\text{coef}_1,\;1-\epsilon_{\text{low}},\;1+\epsilon_{\text{high}})
\]

\[
\text{loss}_{\text{grpo}} = -\min(\text{coef}_1\cdot A,\;\text{coef}_2\cdot A)
\]

可选的 KL 惩罚（`beta != 0` 时）把策略拉回参考策略：

\[
\text{kl} = \exp(\text{ref\_logp} - \text{logp}) - (\text{ref\_logp} - \text{logp}) - 1
\]

最终 `per_token_loss = loss_grpo + beta * kl`。八种 loss_type 的区别只在「`min` 之外怎么用 coef 与 advantage」这一个旋钮上。

#### 4.2.2 核心流程

前向内核 `_grpo_loss_fwd_ct` 的 grid 是 `(B, L, 1)`，即**一个 block 算一个 (batch, token) 位置**。一个 block 内依次完成五件事：

1. **（可选）掩码**：`completion_mask==0` 的位置直接 `return`；
2. **在线 logsumexp**：把 N 维 vocab 切成 `n_chunks = ceil(N/BLOCK_N)` 块，用 `m_i / l_i` 两标量的在线算法逐块累加，全程走 `exp2(x * LOG2E)` 的快速路径；
3. **目标 logp**：用 `gather` 取出 `logits[行, id]` 这一个标量，减去 lse 得 `logp`；
4. **coef 与 loss**：按 `loss_type` 选四条分支之一算 `per_token_loss` 与 `is_clipped`；
5. **（可选）KL**：若 `beta != 0`，算 KL 并累加进 loss。

每个 block 最后 scatter 三个标量：`loss[b,l]`、`lse_cache[b,l]`、`is_clipped[b,l]`。反向内核 grid 同样是 `(B, L, 1)`，从缓存的 `lse` 重算 logp 与 coef，再把 `dlogp` 展开成整行 vocab 的 `dlogits`。

#### 4.2.3 源码精读

**前向内核的在线 logsumexp**（[grpo_loss.py:106-124](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L106-L124)）：

```python
m_i = ct.full((), -math.inf, dtype=ct.float32)
l_i = ct.full((), 0.0, dtype=ct.float32)
for ci in range(n_chunks):
    col_idx = ct.add(ct.arange(BLOCK_N, dtype=ct.int32), ci * BLOCK_N)
    logits = ct.astype(ct.gather(logits_input, (logits_row, col_idx),
                     check_bounds=True, padding_value=-math.inf, latency=3), ct.float32)
    logits_scaled = logits * inv_temperature
    chunk_max = ct.max(logits_scaled, 0, keepdims=False)
    new_m = ct.maximum(m_i, chunk_max)
    alpha = ct.exp2(ct.mul(m_i - new_m, LOG2E))                # 平移旧基准到新基准
    l_i = ct.add(ct.mul(l_i, alpha),
                 ct.sum(ct.exp2(ct.mul(logits_scaled - new_m, LOG2E)), 0))
    m_i = new_m
lse = m_i + ct.log(l_i)
```

这正是 u6-l1 Flash 注意力里讲过的 **online softmax** 折叠技巧，只是这里归约的是 vocab 维而非 key 维：每来一块，用 `alpha` 把旧累加器 `l_i` 平移到新基准 `new_m`，再并入本块的 `exp` 之和。最终 `lse = m + log(l)`。全程用 `exp2(x * LOG2E)`（`LOG2E = log₂e`）走 GPU 的 EX2 快速指令。

**目标 logp**（[grpo_loss.py:127-132](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L127-L132)）：用一个 1 元素 `gather`（`idx_tile = arange(1) + idx`）把目标 token 的 logit 取成标量：

```python
idx_raw = ct.load(input_ids, (off_b, off_l), shape=())
idx = ct.astype(idx_raw, ct.int32)
idx_tile = ct.add(ct.arange(1, dtype=ct.int32), idx)
x_tile = ct.astype(ct.gather(logits_input, (logits_row, idx_tile), check_bounds=False), ct.float32)
x = ct.sum(x_tile, 0, keepdims=False) * inv_temperature
logp = x - lse
```

**重要性比与 GRPO 损失分支**（[grpo_loss.py:139-156](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L139-L156)）：

```python
coef_1 = ct.exp(logp - old_logp)
advantage = ct.astype(ct.load(advantages, off_b, shape=()), ct.float32)
if loss_type == 0:  # GRPO: 标准 PPO 裁剪
    coef_2_low = ct.maximum(coef_1, ct.full((), 1.0 - eps_low, ...))
    coef_2_high = ct.minimum(coef_2_low, ct.full((), 1.0 + eps_high, ...))
    ...
    per_token_loss = -ct.minimum(coef_1_for_loss * advantage, coef_2_high * advantage)
```

注意 `coef_2 = clip(coef_1, 1-eps_low, 1+eps_high)` 是「先 max 后 min」两步实现的。八种 loss_type 映射成内核里的四个整数分支（`_LOSS_TYPE_GRPO/CISPO/SAPO/VESPO`，见 [grpo_loss.py:37-51](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L37-L51)）：GRPO/DAPO/BNPO/DR-GRPO/LUSPO 都复用 `_LOSS_TYPE_GRPO` 的 PPO 裁剪（差别只在主机侧的 reduction 方式），CISPO/SAPO/VESPO 各占一个分支。`loss_type` 与 `beta` 都是 `ct.Constant`，编译期分支会被消去，每种配置编译出独立的特化内核。

**可选 KL 惩罚**（[grpo_loss.py:193-200](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L193-L200)）：

```python
if beta != 0.0:
    ref_logp = ct.astype(ct.load(ref_logp_input, (off_b, off_l), shape=()), ct.float32)
    kl = ct.exp(ref_logp - logp) - (ref_logp - logp) - 1.0
    if use_bias_correction_kl:        # DeepSeek-V3.2 的 IS 修正 KL
        kl = kl * coef_1
    per_token_loss = per_token_loss + beta * kl
    ct.scatter(kl_output, (off_b, off_l), ct.astype(kl, kl_output.dtype))
```

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：跟踪 grpo_loss 前向 grid=(B,L) 内核如何在一个 block 内完成四件事，并理解反向为何从缓存的 lse 重算 logp、`DLOGITS[:, -1, :]=0` 的语义。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/tilegym/suites/liger/cutile/grpo_loss.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py)，在 `_grpo_loss_fwd_ct`（[L54](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L54)）里依次标注：①在线 logsumexp（L106-124）；②目标 logp（L127-132）；③`coef_1 = exp(logp - old_logp)`（L139）；④可选 KL（L193-200）。
2. 打开测试 [test_grpo_loss.py:15-73](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/suites/liger/test_grpo_loss.py#L15-L73)，对照 PyTorch fp32 参考实现 `_reference_grpo_loss`，确认内核每一步与之逐字对应。

**需要观察的现象 / 需要回答的问题**：

- **(a) 反向为何从缓存的 lse 重算 logp，而不保存完整 logits？** 看 `_grpo_loss_bwd_ct` 的 [L257-264](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L257-L264)：它从 `lse_cache` 取 lse，再用 `gather` 取出目标 token 的 logit 重算 `logp = x - lse`。
- **(b) `DLOGITS[:, -1, :] = 0` 的语义是什么？** 看 [_grpo_loss_backward_ct 的 L562-563](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L562-L563)。

**预期结果（参考答案）**：

- **(a)**：logits 张量是 `(B, L+1, N)`，对大词表（如 N=128k、B×L 上万）而言**完整 logits 本来就常驻显存**（它是前向的输入、反向还要用来算 `dlogits[j] = (indicator(j==idx) - prob[j]) * dlogp`，见反向 [L331-336](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L331-L336)），所以无需「保存」——它没被释放。反向只需把 vocab 维 logsumexp 的结果 `lse` 缓存下来（每 token 一个标量，`(B,L)` 远小于 `(B,L,N)`），就能重算出标量 `logp`，进而重算 `coef_1` 与 `dlogp`。这是一种「存 lse 这个小标量、复用 logits 这个大张量」的折中：既省了重算 logsumexp 的算力，又不必额外物化任何激活。
- **(b)**：logits 形状是 `(B, L+1, N)`，但真正参与损失计算的只有前 `L` 个位置（每个位置预测下一个 token）。最后一个位置 `[:, -1, :]` 对应「前缀位」，**没有对应的 completion token**（参考 [grpo_loss.py:18](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L18) 注释 "last position has no completion token"）。反向内核 grid 是 `(B, L)`，永远不会写最后一行，故 `dlogits[:, -1, :]` 仍是 `empty` 出来的脏值；主机侧显式置零是为了让它对 autograd 图无害（梯度不污染前缀位）。

**待本地验证**：若机器有 cuTile 后端，可跑 `pytest tests/suites/liger/test_grpo_loss.py -x` 确认前向/反向数值正确。

#### 4.2.5 小练习与答案

**练习 1**：grpo_loss 的前向内核 grid 是 `(B, L, 1)`，即每 block 只算一个 token 位置。这与「一块算一行」的归一化内核（u4-l3）相比，为什么这里不能用「一块算一整条序列」？

**参考答案**：因为每个 token 位置都要在 vocab 维 N（往往 128k）上做一次完整 logsumexp，计算量极大且独立；若一块算一整条序列，单 block 要串行做 `L` 次 vocab 归约，并行度太低。`(B, L)` grid 把并行度撑到 `B*L`（成千上万），让 GPU 的 SM 充分喂满；同时一个 block 内只循环 vocab 块、数据局部性好。

**练习 2**：`importance_sampling_level="sequence"`（GSPO）与默认的 `"token"` 有何区别？看 `_grpo_loss_forward_seq_ct` 与主机侧 `_grpo_loss_forward_seq_ct`（[L813](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L813)）。

**参考答案**：token-level 把 `coef_1` 当作每 token 独立的标量在内核里算；sequence-level（GSPO）则在**主机侧**先用 PyTorch 算出每条序列的 per-sequence 重要性权重 `coef_1 = exp(Σ(log_ratio * mask) / seq_len)`（[L836-843](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L836-L843)），再作为 `(B,)` 张量传入内核。内核因此不必每 token 重算 ratio，只用预计算好的 per-sequence 系数。代价是序列级 IS 不支持 cispo/sapo/vespo（见 forward 的 [L1008-1012](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L1008-L1012) 断言）。

---

### 4.3 融合线性交叉熵 / 交叉熵 / JSD / KL / TVD

#### 4.3.1 概念说明

这族「分类与散度损失」共享同一套骨架：**一个 block 算一个 token 行、在 vocab 维做在线归约**。差别只在「分数从哪来」「目标分布是什么」：

- `liger.cross_entropy`：输入已是 `(BT, V)` 的 logits，目标是对的真实 token id。它做了一件 Liger 特色的事——**前向就算好梯度、原地写回 logits**（即 `d(logits)/d(loss)`），反向只需按 `grad_output` 缩放。
- `liger.fused_linear_cross_entropy`（FLCE）：输入是隐藏态 `(BT, H)` + 词表权重 `(V, H)`，**不物化**完整 `(BT, V)` logits，把「最后的线性投影 + CE」融合，省下巨量 logits 显存。
- `liger.jsd / liger.fused_linear_jsd`：Jensen-Shannon 散度，知识蒸馏里学生 vs 教师分布的距离，`beta=0.5` 时对称。
- `liger.kl_div`：KL 散度 `KL(y_true || y_pred)`，`y_pred` 是 log 概率。
- `liger.tvd`：总变差距离 `0.5*|P-Q|`，比 KL 更「硬」的分布距离。

它们都接 `label_smoothing`、`ignore_index`、`softcap`（logit 软限幅）等 Liger 风格的旋钮。

#### 4.3.2 核心流程

**交叉熵内核** `_liger_cross_entropy_kernel`（grid=`(BT,1,1)`）一个 block 做两阶段：

1. **阶段一·在线 logsumexp**：与 grpo_loss 同款的 `m/d` 折叠，沿 vocab 分块累加，同时维护 argmax（用于 token accuracy / predicted token）。得到 `lse`。
2. **阶段二·原地写梯度**（`HAS_GRADIENTS=1` 时）：再循环一遍 vocab 块，算 `softmax = exp2(x*LOG2E - m*LOG2E) / d`，按链式法则写回 `grad = (softmax - onehot(y)) * 归一化因子 + z_loss 项`，**直接覆盖输入 logits 缓冲**。

这样 CE 内核一次启动就同时产出了 loss 和 `d_logits`，反向几乎免费。

**FLCE** 则在 CE 之上多了一层「要不要物化 logits」的决策：按 `BT*V*sizeof` 是否超过 4GB 选两条路径。

#### 4.3.3 源码精读

CE 内核在线 logsumexp 与原地写梯度，见 [cross_entropy.py:195-317](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L195-L317)。原地写梯度的核心几行（[L288-296](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L288-L296)）：

```python
softmax_tile = ct.exp2(input_tile * LOG2E + neg_m_log2e, flush_to_zero=True) * inv_d
is_y = ct.equal(col_idx, y_int32)
if not HAS_WEIGHT:
    grad_tile = ct.add(softmax_tile, ct.mul(2.0*lse_square_scale*lse, softmax_tile))
    grad_tile = ct.sub(grad_tile, eps)
    grad_tile = ct.where(is_y, ct.sub(grad_tile, 1.0 - label_smoothing), grad_tile)
    ...
ct.scatter(input, (row_idx, col_idx), ct.astype(grad_tile, input.dtype), check_bounds=True)
```

这就是经典 CE 梯度 `softmax(x) - onehot(y)`（加上 label smoothing 的 `-eps` 与 z-loss 的 `2*lse²*scale*softmax` 项），**写回 `input` 自己**。autograd 的 backward（[L500-506](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L500-L506)）因此只需把保存的 `_input`（已被改写成 `d_logits`）按 `grad_output` 缩放即可。

**FLCE 的两条路径**（[fused_linear_cross_entropy.py:10-28](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py#L10-L28) 的文件 docstring 是最好的说明）：

| 路径 | 触发条件 | 做什么 | 反向 |
| --- | --- | --- | --- |
| **single-pass** | `BT*V*sizeof ≤ 4GB` | 一次大 GEMM 物化完整 logits，一次 CE 内核原地写 `d_logits` | 保存 `d_logits`，反向做两次大 matmul 算 `grad_input`/`grad_weight` |
| **chunked + backward-in-forward** | `> 4GB` | 把 BT 切成 chunk（每 chunk logits ≤ 1GB），每 chunk：GEMM→CE 写 `d_logit_chunk`→**立刻**算 `grad_input_slice` 与累加 `grad_weight`，然后丢弃 logits_chunk | 反向几乎免费，只按 `grad_output` 缩放已算好的梯度 |

chunked 路径的关键代码（[L271-313](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py#L271-L313)）：

```python
for chunk_id in range(num_chunks):
    logits_chunk = _input_chunk @ weight.t()        # 1. 算一小段 logits
    _ce(logits_chunk, ..., need_grads)              # 2. CE 原地把 logits_chunk 改成 d_logit_chunk
    grad_input_saved[start:end] = grad_logits_chunk.to(_input.dtype) @ weight  # 3. grad_input
    grad_weight_f32 += grad_logits_chunk.float().t() @ _input_chunk.float()    # 4. grad_weight 累加
    # logits_chunk 在迭代末尾出作用域 → 显存释放，峰值是 O(chunk×V) 而非 O(BT×V)
```

这是「**backward-in-forward**」模式：梯度在前向就算完了，反向只做标量缩放。代价是前向多做了两次大 K 的 matmul，换来的是 logits 峰值显存从 `(BT, V)` 降到 `(chunk, V)`——对大词表长序列训练至关重要。

KL 散度与 TVD 的行级归约骨架同源，差别只在每元素的损失公式与是否对齐 chunk。KL 的「ALIGNED 优化」很典型（[kl_div.py:17-22](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/kl_div.py#L17-L22)）：把前 `N_FULL_CHUNKS` 个整块用 `check_bounds=False`（走硬件 TMA），只让最后一个尾块做软件边界检查，这是 u3-l2 讲过的「对齐 chunk 省谓词掩码」优化在真实内核里的落地。

#### 4.3.4 代码实践

**实践目标**：理解 CE「前向融合反向」与 FLCE「backward-in-forward」两种融合策略的显存含义。

**操作步骤**：

1. 读 [fused_linear_cross_entropy.py:227-318](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py#L227-L318) 的两条 if 分支，回答：当 `BT=32768, V=128256, dtype=bf16` 时会走哪条？logits 峰值显存各是多少？
2. 读 `_chunk_size_for`（[L52-58](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_linear_cross_entropy.py#L52-L58)），确认 chunk_size 怎么算。

**需要观察的现象**：`BT*V*2 = 32768*128256*2 ≈ 8.4GB > 4GB`，故走 chunked 路径；`_chunk_size_for` 给出 `chunk_size=4096`（注释里也明说 `For V=128256, bf16: gives chunk_size=4096`），单 chunk logits 约 `4096*128256*2 ≈ 1.05GB`。

**预期结果**：single-pass 峰值约 8.4GB（+反向再算两次大 matmul 的中间量），chunked 峰值约 1.05GB——省了约 8 倍 logits 显存。**待本地验证**。

#### 4.3.5 小练习与答案

**练习**：CE 内核把梯度写回 `input` 自己（`ct.scatter(input, ...)`），这会不会破坏 autograd？看 `CrossEntropyCuTileFunction.forward` 的 [L492-493](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L492-L493)。

**参考答案**：不会。前向在 `input_requires_grad` 时 `ctx.save_for_backward(_input.detach())`，保存的是被改写成 `d_logits` 后的张量；autograd 图里记录的是这个 Function 节点，反向直接复用保存的 `d_logits`。对外层调用者而言，输入 logits 的语义本来就是「前向消费、反向产出梯度」，原地改写它等于把同一个缓冲复用作 `d_logits`，省了一份 `(BT,V)` 显存。注意：这要求调用方在前向后不再读原 logits——Liger 风格的「in-place gradient」隐含契约。

---

### 4.4 归一化变体：fused_add_rms_norm / poly_norm / dyt

#### 4.4.1 概念说明

liger suite 的归一化族不只有「标准」RMSNorm/LayerNorm，还有三个变体值得单独看，它们示范了「同一行级归约骨架如何承载不同数学」：

- **`liger.fused_add_rms_norm`**：把 transformer 解码层里最常见的「残差相加 + RMSNorm」三步（`S=X+R; R=S; Y=rmsnorm(S)`）融合成一个内核，省一次访存。
- **`liger.poly_norm`**：PolyCom 论文的多项式归一化，用 \(x, x^2, x^3\) 三个范数的多项式组合替代单一线性变换：
  \[
  y = w_0\cdot\frac{x^3}{\sqrt{\text{mean}(x^6)+\varepsilon}} + w_1\cdot\frac{x^2}{\sqrt{\text{mean}(x^4)+\varepsilon}} + w_2\cdot\frac{x}{\sqrt{\text{mean}(x^2)+\varepsilon}} + b
  \]
- **`liger.dyt`**（Dynamic Tanh）：严格说是激活而非归一化（论文用它替代 LayerNorm），\(y=\tanh(\alpha x)\gamma+\beta\)，但内核结构与归一化同构（逐元素 + 行级反向），故放在这里对照。

#### 4.4.2 核心流程

三者前向都是 **row-parallel**（一块算一行）。差别在反向的调度：

- `fused_add_rms_norm` 反向用**持久化 grid-stride**（grid=`(sm_count,)`，每 SM 一个 block 跨步处理多行），每 SM 维护一个寄存器内的 `dW_acc` 累加器，循环结束只写一次 `dW_partial[sm_id, :]`，主机侧 `dW = dW_partial.sum(0)`。这是 u5-l2 持久化调度的范本。
- `poly_norm` 反向用 `ct.atomic_add` 把四元梯度 `[dW0, dW1, dW2, dB]` 直接原子累加进一个 `(4,)` 缓冲，省掉主机侧的归约链。
- `dyt` 反向用**持久化 2D grid**（`(num_col_blocks, NUM_SMS)`），每个 block 写到唯一的 `(start_row_id, col)` 位置，无原子、主机侧 `sum(0)`。

三种「跨行归约权重梯度」的写法（持久化累加 / 原子 / 唯一位置散列）是本节最值得对比的工程范式。

#### 4.4.3 源码精读

**fused_add_rms_norm 前向**（grid=`(n_rows,1,1)`，一块一行，两遍：[fused_add_rms_norm.py:113-145](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L113-L145) 是单遍快路径）：

```python
S_tile = ct.add(ct.gather(X, (row_idx, col_idx), ...), ct.gather(R, (row_idx, col_idx), ...))  # S = X + R
ct.scatter(S, (row_idx, col_idx), S_tile, ...)               # 写回残差 S
...
rstd = ct.rsqrt(ct.sum(ct.mul(S_tile, S_tile), 0) / n_cols + eps)   # RMSNorm 的 rstd
ct.scatter(RSTD, row_idx, rstd)
S_tile = ct.astype(ct.mul(S_tile, rstd), X.dtype)
ct.scatter(Y, (row_idx, col_idx), ct.mul(S_tile, ct.add(W_tile, offset)), ...)  # Y = S*rstd*(W+offset)
```

三步融合进一个内核：算 `S=X+R` 并写回（残差分支要拿到更新后的 S）、算 rstd 并缓存、算 `Y`。`offset` 参数让同一内核服务 Llama（offset=0）与 Gemma（offset=1，即 `Y=S*rstd*(W+1)`）——u4-l3 讲过的「仿射统一写成 `y=x̂·(offset+w)`」在此复现。

**fused_add_rms_norm 反向持久化**（[L224-318](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L224-L318)）的核心是「每 SM 一个 block、寄存器累加 dW、只写一次」：

```python
sm_id = ct.bid(0)
dW_acc = ct.full((BLOCK_SIZE,), 0.0, dtype=ct.float32)   # 寄存器内累加器
for i in range(num_iters):                                 # grid-stride 跨多行
    row_idx = sm_id * num_iters + i
    ...
    dW_acc = ct.add(dW_acc, <本行的 dW 贡献>)
ct.scatter(dW_partial, (sm_id, col_idx), dW_acc, ...)      # 整个 SM 只写一次
```

主机侧（[L586](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L586)）`dW = dW_partial.sum(dim=0)`。`dW_partial` 形状 `(sm_count, n_cols)` 远小于 `(n_rows, n_cols)`，这就是持久化调度省显存与省 launch 的关键。另注意反向还分「1-chunk」与「2-chunk」两个内核（`_BWD_MAX_CHUNK_SIZE=4096` 为界，[L539-540](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L539-L540)）：列宽大于 4096 时把列拆 lo/hi 两半以压低寄存器压力。

**poly_norm 的多项式展开**（[poly_norm.py:73-84](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/poly_norm.py#L73-L84)）用了一个巧妙的复用：算 `x², x⁴`，则 `mean(x²)`、`mean(x⁴)`、`mean(x⁶)=mean(x⁴·x²)` 三个范数只需两次乘法就能同时拿到：

```python
x2 = x_f32 * x_f32      # x^2
x4 = x2 * x2            # x^4
sum_sq_1 = sum_sq_1 + x2          # Σx²
sum_sq_2 = sum_sq_2 + x4          # Σx⁴
sum_sq_3 = sum_sq_3 + x4 * x2     # Σx⁶
```

输出用 Horner 形式避免物化 `x³` tile（[L106-108](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/poly_norm.py#L106-L108)）：`y = x²·(w0r3·x + w1r2) + w2r1·x + b`。反向把 `[dW0,dW1,dW2,dB]` 用 `ct.atomic_add` 累加进 `(4,)` 张量（[L243-246](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/poly_norm.py#L243-L246)），省掉主机侧归约链。

**dyt 的持久化 2D 反向**（[dyt.py:90-136](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/dyt.py#L90-L136)）：grid=`(num_col_blocks, NUM_SMS)`，第二维是「起始行」，每 block 跨步 `start_row_id, start_row_id+NUM_SMS, ...` 处理多行，并把每行的 `dg/db/da` 贡献累加进寄存器 tile，最后 scatter 到唯一的 `(start_row_id, col)`——因每 SM 写不同行号，无竞争，主机侧 `sum(0)` 即得最终梯度。

#### 4.4.4 代码实践

**实践目标**：对比三种「跨行归约权重梯度」的写法。

**操作步骤**：在三个文件里分别找出梯度归约的那一行：

1. `fused_add_rms_norm.py`：[L318](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L318) `ct.scatter(dW_partial, (sm_id, col_idx), dW_acc)` + 主机 `sum(0)`；
2. `poly_norm.py`：[L243-246](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/poly_norm.py#L243-L246) `ct.atomic_add(dwdb_output, ...)`；
3. `dyt.py`：[L126-136](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/dyt.py#L126-L136) 写到唯一 `(start_row_id, col)` + 主机 `sum(0)`。

**需要观察的现象**：三者都避免了「每行一个 `(N,)` 梯度、主机侧大归约」的朴素写法，但手段不同——持久化寄存器累加（farn）、原子（poly）、唯一位置散列（dyt）。

**预期结果**：理解「权重梯度需要跨所有行归约」这一共性需求下的三种工程解。

#### 4.4.5 小练习与答案

**练习**：`fused_add_rms_norm` 反向为什么 grid 取 `(sm_count,)` 而不是 `(n_rows,)`（一块一行）？用「每 SM 一个 block 独占寄存器文件」解释。

**参考答案**：反向每行需要同时驻留 `dY / S / W / dS_out` 等多个 tile，寄存器压力大。取 grid=`(sm_count,)`、每 SM 恰一个 block，保证该 block 独占整份 256KB 寄存器文件（文件 docstring [L18-26](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/fused_add_rms_norm.py#L18-L26) 说「each block owns the full 256KB register file」），从而能把 `dW_acc` 常驻寄存器跨多行累加、整个 SM 只写一次 dW。若用一块一行，grid 是 `n_rows`（远大于 SM 数），多个 block 抢同一 SM 的寄存器、占用率被迫降低，且每行都要单独写 dW、归约代价大。

---

### 4.5 激活内核与 autograd 反向重计算：swiglu / dyt

#### 4.5.1 概念说明

激活内核（swiglu/dyt/geglu）是 u4-l1「逐元素内核模式」在 liger suite 的延续。本节重点不是激活公式本身，而是它们如何落实 u4-l2 的 **autograd 反向重计算** 套路：前向不保存中间激活，反向从原始输入重算。

- **`liger.swiglu`**：\(c = \text{silu}(a\cdot\text{gate\_multiplier})\cdot b\cdot\text{down\_multiplier}\)，其中 \(\text{silu}(x)=x\cdot\sigma(x)\)。这是 SwiGLU MLP 的核心。
- **`liger.dyt`**：\(y=\tanh(\alpha x)\gamma+\beta\)（前节已见）。

两者前向都只 `save_for_backward` 原始输入（swiglu 存 `a, b`；dyt 存 `x, alpha, gamma, beta`），反向重新算一遍激活。

#### 4.5.2 核心流程

swiglu 的反向链式法则（[swiglu.py:153-157](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L153-L157) 注释）：

\[
c = \text{silu}(a\cdot g_m)\cdot b,\quad
\frac{\partial c}{\partial b} = \text{silu}(a\cdot g_m)\cdot 1,\quad
\frac{\partial c}{\partial a} = \bigl[\text{silu}'(a g_m)\cdot g_m\bigr]\cdot b
\]

其中 \(\text{silu}'(u)=\text{silu}(u)\cdot(1-\sigma(u))+\sigma(u)\)。反向内核从保存的 `a, b` 重算 `sig_a`、`silu_a`，再算 `da, db`。

一个性能细节：swiglu 前向用 `exp2(-a*LOG2E)` 算 sigmoid（走 EX2 快速指令，配 `occupancy=1` 才能正确 lowering），反向却**故意不用 exp2**（[swiglu.py:29-31](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L29-L31) 注释解释：反向循环内有 scatter，加 `occupancy=1` 有挂起风险，而 exp2 不配 occupancy=1 会翻倍 MUFU 指令数，所以反向退回普通 `exp`）。这是「前向/反向可用不同近似」的真实工程取舍——与 u4-l2 强调的「前向反向须用同一近似」对照看：这里**数值上仍是同一个 sigmoid**，只是用了不同指令实现，精度差异在反向容差（1e-2）内。

#### 4.5.3 源码精读

swiglu 反向重计算（[swiglu.py:161-177](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L161-L177)，aligned 路径）：

```python
dc = ct.astype(ct.gather(DC, (row_idx, col_idx), ...), ct.float32)
a = ct.astype(ct.gather(A, (row_idx, col_idx), ...), ct.float32)   # A 保存的是原始 a
b = ct.astype(ct.gather(B, (row_idx, col_idx), ...), ct.float32)
a_scaled = a * gate_multiplier
sig_a = ct.truediv(1.0, 1.0 + ct.exp(0.0 - a_scaled), rounding_mode=ct.RoundingMode.APPROX)  # 重算 sigmoid
silu_a = a_scaled * sig_a
db = dc * silu_a
da = dc * (silu_a * (1.0 - sig_a) + sig_a) * b * gate_multiplier
ct.scatter(A, (row_idx, col_idx), ct.astype(da, A.dtype), ...)     # da 原地写回 A
ct.scatter(B, (row_idx, col_idx), ct.astype(db, B.dtype), ...)     # db 原地写回 B
```

两个内存优化叠加：① 反向重算 sigmoid，不存激活（u4-l2 套路）；② `da/db` 原地写回保存的 `A/B` 缓冲，再省一份输出。autograd 包装见 `SwiGLUCuTileFunction`（[L237-291](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L237-L291)）：`forward` 存 `a, b`；`backward` 返回 `(da, db, None, None)`——四个返回值对应 forward 的四个输入 `(a, b, gate_multiplier, down_multiplier)`，后两个是 Python float 故梯度为 `None`（这是 u4-l2 讲过的「返回值个数须与 forward 输入一一对应」）。

dyt 反向同理从 `x` 重算 `tanh(alpha*x)`（[dyt.py:108-122](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/dyt.py#L108-L122)），且反向用 `rounding_mode=RMd.APPROX` 的近似 tanh（注释说「~1.6x faster, 2-4 ULP off; well within bwd tolerance 1e-2」）——又一个「反向用更快近似、在容差内」的实例。

#### 4.5.4 代码实践

**实践目标**：验证 swiglu 反向重计算与原地写回。

**操作步骤**：

1. 读 `SwiGLUCuTileFunction.forward`（[L245-271](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L245-L271)）：确认 `ctx.save_for_backward(a, b)` 只存了输入、没存 `silu_a` 等中间量。
2. 读 backward（[L273-291](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L273-L291)）：确认它从 `a, b` 重算 sigmoid。
3. 若有 cuTile 后端，跑：
   ```bash
   pytest tests/suites/liger/test_swiglu.py -x
   ```

**需要观察的现象**：前向保存的只有 `a, b`；反向内核里能找到 `sig_a = ... ct.exp(0.0 - a_scaled)` 的重算。

**预期结果**：相比「前向存 silu_a 这个 `(M,N)` 激活」，重计算省下一个完整激活张量的显存，代价是多一次 sigmoid 计算——对显存受限的训练典型是净收益。**待本地验证**。

#### 4.5.5 小练习与答案

**练习**：swiglu 的 `down_multiplier` 为什么不在内核里、而在 Python wrapper 层应用？看 [swiglu.py:14-18](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/swiglu.py#L14-L18) 注释。

**参考答案**：`gate_multiplier` 是 `ct.Constant[float]`、烤进内核（编译期折叠掉 `gate_multiplier=1.0` 时的 FMUL），但 `down_multiplier` 只是输出/梯度的整体标量缩放，前向 `c_out = c * down_multiplier`、反向 `dc = dc * down_multiplier`，在 Python 层一次乘法即可，不必污染内核签名与触发重新编译。这是「编译期常量 vs 运行期标量」的分界。

---

## 5. 综合实践

把本讲四节串起来，做一个「读懂一份 liger 训练内核并对照参考实现」的完整练习。

**任务**：选择 `liger.grpo_loss`，完成下面这份「内核阅读报告」。

1. **接口层**：在 [ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) 找到 `liger.grpo_loss` 的 stub，列出它的关键参数与 `loss_type` 支持的取值。
2. **注册层**：确认 [grpo_loss.py:1227](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L1227) 的 `@register_impl` 与 [cutile/__init__.py:15](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py#L15) 的导入如何把实现挂进 `_REGISTRY`。
3. **前向内核**：在 `_grpo_loss_fwd_ct`（[L54](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L54)）里画出 grid=(B,L) 一个 block 的五步流程（掩码 → logsumexp → logp → loss 分支 → KL）。
4. **autograd 层**：在 `GrpoLossCuTileFunction`（[L970](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L970)）里确认 `forward` 保存了什么（`logits, old_logp, ..., lse`）、`backward` 返回了多少个值（22 个输入 → `dlogits` + 21 个 `None`，见 [L1223](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L1223)）。
5. **对照参考**：把 [test_grpo_loss.py:15-73](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/suites/liger/test_grpo_loss.py#L15-L73) 的 `_reference_grpo_loss` 与内核逐行对照，标出哪些行是「数学等价的 Python 表述」。

**验收标准**：你能不看源码，向同伴讲清「一次 `liger.grpo_loss(logits, old_logp, ...)` 调用，从前向 stub 到反向 `dlogits` 的完整数据流」，并指出 `DLOGITS[:, -1, :]=0` 在哪一行、为什么必须置零。

---

## 6. 本讲小结

- **统一接口**：liger suite 的训练内核族通过 [ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) 的 `@dispatch("liger.xxx")` stub 暴露，cuTile 实现靠 `cutile/__init__.py` 的导入副作用注册——与核心 ops 同构，只是带点号前缀、须显式 import。
- **grpo_loss 是「一个算子 = 一族算法」的典范**：grid=(B,L) 的前向 block 内串起在线 logsumexp、目标 logp、PPO 重要性比 `coef_1=exp(logp-old_logp)`、四种 loss_type 分支、可选 KL；八种策略梯度算法共用同一份内核骨架。
- **反向重算 lse 而非存激活**：grpo_loss 反向从缓存的 `lse` 标量重算 `logp`，复用本来就常驻的 logits 大张量；CE 内核则更进一步「前向就算好梯度原地写回」，反向几乎免费。
- **FLCE 的两条路径**：small（≤4GB）走 single-pass 大 GEMM，large 走 chunked backward-in-forward，把 logits 峰值显存从 `(BT,V)` 降到 `(chunk,V)`。
- **归一化族示范三种「跨行归约权重梯度」范式**：fused_add_rms_norm 的持久化寄存器累加、poly_norm 的 `atomic_add` 进 `(4,)` 缓冲、dyt 的唯一位置散列 + 主机 `sum(0)`。
- **激活内核延续 u4-l2 的反向重计算**：swiglu/dyt 前向只存原始输入、反向重算激活，并叠加「da/db 原地写回」「反向用更快近似（在容差内）」等内存与速度优化。

## 7. 下一步学习建议

- **跑通 liger 测试**：`pytest tests/suites/liger/ -x` 是检验本讲所有内核正确性的最直接方式，每个 `test_*.py` 都带 PyTorch fp32 参考，是理解算子语义的最佳伴侣。
- **回看 u4-l2 与 u5-l2**：本讲的反向重计算、持久化调度、原地写梯度都源自那两讲建立的工程范式，对照阅读能看清「套路如何复用」。
- **续读其他 liger 算子**：`liger.fused_linear_jsd`（知识蒸馏）、`liger.tiled_mlp`（长序列分片 MLP）、`liger.multi_token_attention`（多 token 注意力）在本讲骨架之上各有扩展，可作为进阶练习。
- **关注 RL 算法变体**：若你对 GRPO/DAPO/CISPO/SAPO/VESPO 的算法细节感兴趣，可结合 [grpo_loss.py:6-22](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L6-L22) 的文件 docstring 与各 loss_type 分支，把数学公式与内核实现一一对应。
