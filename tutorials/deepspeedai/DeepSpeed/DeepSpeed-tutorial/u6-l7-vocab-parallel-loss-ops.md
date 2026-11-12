# VocabTP 损失算子：词表并行 logprobs/熵/KL/JSD

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「词表并行损失」要解决的问题：AutoTP 把 `lm_head` 按词表维切分后，每个 TP rank 只拿到 `[batch, seq, vocab/tp]` 的局部 logits，而 RL（policy gradient、entropy bonus）和蒸馏（KL/JSD）都需要**全词表**上的 softmax 目标——这些算子让目标在不 all-gather 完整 logits 的情况下直接算出来。
2. 描述所有算子共享的**两遍结构**：第一遍在本地 shard 上算逐行 max / sum-exp 统计，经一次 MAX all-reduce 和一次 SUM all-reduce 得到全局归一化；第二遍用全局统计逐 token 计算损失，全程只碰本地 shard。
3. 精读 `_VocabParallelLogProbs` / `_VocabParallelEntropy` / `_VocabParallelKLDiv` / `_VocabParallelJSD` 四个 autograd Function：前向只保存输入与逐 token 统计（不保存概率张量），反向按行块重算 softmax，因此反向**不需要任何通信**。
4. 解读本周期新增的广义 JSD（PR #8958，提交 `da79d7447`）：`(1-beta)*KL(student||mixture)+beta*KL(teacher||mixture)`，mixture 是**概率**混合，所以必须先全局归一化再进第二遍；beta=0/1 是独立的 KL 分支而非连续极限。
5. 了解 Triton fused 内核（`jsd_softmax_stats` / `jsd_forward` / `jsd_backward`）与 eager 行块回退的分工，以及空 shard、不等分词表、非连续 causal-shift 视图等工程约束。

本讲承接 u6-l1（AutoTP 注入）。AutoTP 负责「让 `lm_head` 的输出天然是分片的」，本讲负责「拿到分片 logits 之后怎么算损失」。

## 2. 前置知识

**softmax 的数值稳定写法。** 直接 \( \mathrm{softmax}(z)_j = e^{z_j}/\sum_k e^{z_k} \) 会溢出。标准做法是先减去行最大值 \(m\)：\( \mathrm{log\\_softmax}(z)_j = (z_j - m) - \log\sum_k e^{z_k - m} \)。本讲所有算子都围绕两个逐行统计量展开：全局最大值 `global_max`（即 \(m\)）与对数配分函数 `log_sum_exp`（即 \(\log\sum_k e^{z_k-m}\)）。它们在代码里刻意**分开保存而不合并**成一个 logsumexp——把一个小归一化项加到一个大 max 上，fp32 会把它整个舍掉（见 `_softmax_stats` 的 docstring）。

**分布式 softmax（Megatron 风格）。** 当词表被切到多个 rank 上时，每个 rank 只能看到一部分 \(z\)。关键观察是 max 和 sum-exp 都可以「先局部、后全局」：

\[ m_{\text{global}} = \max_r m_r, \qquad S_{\text{global}} = \sum_r S_r \cdot e^{m_r - m_{\text{global}}} \]

其中 \(S_r = \sum_{j \in \text{shard}_r} e^{z_j - m_r}\)。也就是说，一次 MAX all-reduce 加一次 SUM all-reduce 就够，通信量只有 `O(rows)` 而不是 `O(rows × vocab)`。u6-l5 的 ZeRO-3 参数 allgather 是「以通信换显存」；这里是同一哲学在损失函数上的版本——**以两次小 all-reduce 换掉一次大 all-gather**。

**逐 token 损失与 autograd。** 本讲的算子都返回 `[batch, seq]` 的 fp32 逐 token 张量（不帮你求和/求平均），掩码与归约留给调用方。它们各自是一个 `torch.autograd.Function`：`forward` 里做集体通信并把少量统计量存进 `ctx`，`backward` 里**重算** softmax 得到梯度。前提约定（模块 docstring 写明）：每个 TP rank 必须 backward **同一份** loss，这样每个本地 logit 的梯度可以不通信地算出来。

**几个本讲会用到的损失定义。** 设 \(q=\mathrm{softmax}(z^{\text{student}})\)、\(p=\mathrm{softmax}(z^{\text{teacher}})\)：

- 熵：\(H(q) = -\sum_j q_j \log q_j\)
- 前向 KL：\(\mathrm{KL}(p\|q) = \sum_j p_j(\log p_j - \log q_j)\)（质量覆盖型，mode-covering）
- 反向 KL：\(\mathrm{KL}(q\|p)\)（模式追逐型，on-policy 蒸馏常用）
- 广义 JSD（\(0<\beta<1\)）：\(m = (1-\beta)q + \beta p\)（**概率**的凸组合），\( \mathrm{JSD}_\beta = (1-\beta)\,\mathrm{KL}(q\|m) + \beta\,\mathrm{KL}(p\|m) \)

**Triton。** OpenAI 的 GPU 内核 DSL，用 Python 写块状（block-level）程序。本讲的 Triton 内核每次处理一行 logits，把词表维按 `BLOCK` 分块循环，统计量在寄存器里以 online-softmax 方式增量维护，因此从不物化 `[rows, vocab]` 的 fp32 中间张量。无需先学 Triton 语法，看懂内核的循环结构即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/sequence/vocab_parallel_ops.py` | 核心：4 个 autograd Function、4 个公共 API（`vocab_parallel_logprobs/entropy/kl_div/jsd`）、eager 行块回退、所有 all-reduce 协议 |
| `deepspeed/ops/triton_ops/vocab_parallel_softmax.py` | Triton 内核族：softmax 统计、entropy/KL 单遍统计、JSD 的统计/散度/反向内核、`row_layout` 原地寻址 |
| `deepspeed/sequence/cross_entropy.py` | 词表分片元数据的推断与集体校验（`_resolve_vocab_metadata`），`vocab_parallel_logprobs` 复用它 |
| `docs/_tutorials/autotp-training.md` | 用户文档：`vocab_parallel_lm_head` 配置、四个损失算子的用法示例与基准表 |
| `tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py` | 回归测试：FP64 dense oracle 对照、Triton/eager 一致性、TP1/2/4 分布式用例 |

注意：这些函数**没有**从 `deepspeed.sequence` 包的 `__init__` 导出（那里只导出 cross_entropy 相关符号，见 [deepspeed/sequence/__init__.py:L11-L12](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/__init__.py#L11-L12)），要直接从子模块导入：

```python
from deepspeed.sequence.vocab_parallel_ops import (
    vocab_parallel_logprobs, vocab_parallel_entropy, vocab_parallel_kl_div, vocab_parallel_jsd)
```

## 4. 核心概念与源码讲解

### 4.1 两遍结构与全局归一化统计（all-reduce 协议）

#### 4.1.1 概念说明

所有词表并行损失共享同一个骨架。对比两条路径：

- **dense 路径**：先 all-gather 出 `[batch, seq, full_vocab]` 的 logits，再 `F.log_softmax`、组合 PyTorch 算子算损失。问题有两个：峰值显存里至少多出完整 logits（还会在 autograd 图里保留 fp32 概率等多份同形张量）；通信量 `O(rows × full_vocab)`。
- **词表并行路径**：
  1. **第一遍（统计）**：每个 rank 在本地 shard 上算逐行 `local_max` 与 `local_sum_exp`（相对本地 max），做一次 MAX all-reduce 得 `global_max`，把本地 sum-exp 重标定到全局 max（乘 \(e^{m_r - m_{\text{global}}}\)）后做一次 SUM all-reduce，取 log 得 `log_sum_exp`。
  2. **第二遍（计算）**：用 `(z - global_max) - log_sum_exp` 这个恒等式，在本地 shard 上直接得到本地那些列的 log-probability；需要跨 shard 求和的量（比如目标 token 的 logit、熵、KL 项）用一次额外的 SUM all-reduce 聚合。

通信量降为 `O(rows)` 级别的几个小张量。代价是「需要全局归一化之后才能求值」的量（比如 JSD 的非线性 mixture）必须拆成两遍读输入——这正是 #8958 在提交说明里强调的 JSD 与单遍 KL 的本质区别。

#### 4.1.2 核心流程

```
输入: 各 rank 的 logits_shard [rows, local_vocab]
第一遍:
  local_max[r]  = max_j z_ij                (逐行, 本地)
  local_sum[r]  = sum_j exp(z_ij - local_max)  (逐行, 本地)
  global_max    = all_reduce(local_max, MAX)      # 第 1 次集体通信
  rescale       = exp(local_max - global_max)     # 空 shard 恒为 0
  sum_exp       = all_reduce(local_sum * rescale, SUM)  # 第 2 次
  log_sum_exp   = log(sum_exp)
第二遍:
  本地 log_prob_ij = (z_ij - global_max_i) - log_sum_exp_i
  跨 shard 的求和项 -> 再来一次 SUM all-reduce
```

空 shard（某 rank 分到 0 列）必须照常参加两次 all-reduce（贡献 `-inf` 与 `0`），否则会把别的 rank 挂死在 collective 上——这是分布式代码里很容易踩的坑，代码里有专门处理。

#### 4.1.3 源码精读

eager 版统计函数 `_softmax_stats` 一次处理一个列表的 logits（KL/JSD 要同时算 student 和 teacher 两组统计），返回 `[len(list), rows]` 的 `global_max` 与 `log_sum_exp`：[vocab_parallel_ops.py:L71-L101](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L71-L101)。其中 L84-L89 处理空 shard：填 `-inf` 后跳过本地 `amax`（`amax` 会拒绝空维度）但**继续参加** all-reduce。docstring（L72-L77）解释了为何 max 与 lse 分开保存：把小归一化项加进大 max 会被 fp32 舍掉。

Triton 版把统计挪进内核：[vocab_parallel_ops.py:L104-L119](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L104-L119)。注意它的技巧——本地 sum-exp 是**相对本地 max** 算的，之后统一重标定到全局 max，所以仍然只需一次 MAX 和一次 SUM all-reduce。

重标定函数 `_rescale_to_global_max` 处理「本地没有有限 logit」的行：[vocab_parallel_ops.py:L122-L125](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L122-L125)。`exp(-inf - -inf)` 是 NaN，所以先判 `seen` 再决定贡献 0。

eager 路径的显存约束由行块大小 `_CHUNK_NUMEL = 1 << 24`（约 1600 万元素）控制：[vocab_parallel_ops.py:L39-L41](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L39-L41) 与 `_row_chunks`（[L65-L68](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L65-L68)）——不管处理多少 token，fp32 临时量始终有界，只有逐 token 统计量贯穿所有行。

#### 4.1.4 代码实践

**实践目标**：验证「本地统计 + 两次 all-reduce」确实等价于全词表 logsumexp。

**操作步骤**（示例代码，可直接运行，CPU 单进程即可）：

```python
# 示例代码：单进程模拟 TP=2 的全局统计（tp_group=None 时不做 all-reduce，
# 我们在 Python 层手工"扮演"那两次 all-reduce）
import torch
from deepspeed.sequence.vocab_parallel_ops import _softmax_stats

torch.manual_seed(0)
full = torch.randn(4, 10)                    # [rows, full_vocab]
shards = [full[:, :4], full[:, 4:]]          # 手工切成两个 shard

# 各 rank 的本地统计（真实代码里这一步在 GPU 上由 Triton 内核完成）
local = [_softmax_stats([shard], tp_group=None) for shard in shards]
local_max = torch.stack([stats[0][0] for stats in local])   # [2, rows]
local_lse = torch.stack([stats[1][0] for stats in local])

# 第 1 次集体通信: MAX all-reduce
global_max = local_max.max(dim=0).values
# 重标定到全局 max 后第 2 次集体通信: SUM all-reduce
sum_exp = torch.exp(local_lse - global_max).sum(dim=0)
merged_lse = torch.log(sum_exp)

expected = torch.logsumexp(full.float(), dim=-1)
print(torch.allclose(global_max, full.max(-1).values))
print(torch.allclose(merged_lse, expected, atol=1e-6))
```

**需要观察的现象**：`global_max` 逐行等于 `full.max(-1)`；手工合并的 `merged_lse` 与 `torch.logsumexp(full, -1)` 在 fp32 容差内一致。

**预期结果**：两边逐元素接近（`torch.allclose(merged_lse, expected, atol=1e-6)`）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `global_max` 和 `log_sum_exp` 要作为两个张量分开保存，而不是合成一个 logsumexp？
**答案**：`log_sum_exp` 是相对 `global_max` 的修正量；若把 \(\log S + m\) 合成单个大数，小修正项 \( \log S \) 在 fp32 加法里会被大数 \(m\) 完全舍掉（大数吃小数），损失精度。分开保存后第二遍用 `(z - m) - lse` 两次减法，各自都在相近量级上。

**练习 2**：一个 rank 的本地 shard 为空（0 列），它对 MAX / SUM all-reduce 应该贡献什么？
**答案**：MAX 贡献 `-inf`（不拉高全局 max），SUM 贡献 `0`（不贡献概率质量）；且必须照常调用 all-reduce，否则其他 rank 会永远阻塞在 collective 上。见 [vocab_parallel_ops.py:L84-L89](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L84-L89)。

---

### 4.2 autograd Function 族：logprobs / entropy / KL

#### 4.2.1 概念说明

三个「老」算子（PR #8791/#8838 引入）各自包装成一个 autograd Function，设计完全同构：

- **前向**只把**输入 logits + 逐 token 统计**（`global_max`、`log_sum_exp`、以及损失本身）存进 `ctx.save_for_backward`——**不保存任何概率张量**。
- **反向**用保存的统计量把 softmax **重算**出来，套解析梯度公式，按行块写出梯度。因为每个 TP rank backward 的是同一份 loss，本地 logit 的梯度完全由本地列决定，**反向零通信**。
- 全部标注 `@once_differentiable`：不支持二阶微分（会直接抛错而不是悄悄算错）。

三个解析梯度公式（反向代码的注释里原文在）：

\[ \frac{\partial \log p_i}{\partial z_j} = \mathbb{1}[j{=}i] - p_j \]

\[ \frac{\partial H}{\partial z_j} = -p_j(\log p_j + H) \]

\[ \frac{\partial\, \mathrm{KL}(A\|B)}{\partial z^A_j} = p^A_j(\log p^A_j - \log p^B_j - \mathrm{KL}), \qquad \frac{\partial\, \mathrm{KL}(A\|B)}{\partial z^B_j} = p^B_j - p^A_j \]

#### 4.2.2 核心流程

**logprobs**（给定 token id 求 log 概率，policy gradient 用）：
1. 校验 index 形状（可带尾部 top-k 维）与取值范围；
2. 统计全局 `global_max/lse`；
3. `index - vocab_start_index` 落在本地的部分 `gather` 出目标 logit，非本地部分置 0，一次 SUM all-reduce 把「目标 logit」从持它的那个 rank 聚过来；
4. 减去 `global_max + lse` 得 logprob；`ignore_index` 位置输出 0。

**entropy / KL**：统计阶段额外聚合一个「概率加权和」（entropy）或「A 加权 log-ratio 和」（KL），Triton 路径把它们和 sum-exp 打包进同一次 SUM all-reduce；eager 路径单独 all-reduce 逐 token 结果。

#### 4.2.3 源码精读

`_VocabParallelLogProbs.forward`：[vocab_parallel_ops.py:L206-L232](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L206-L232)。L217-L225 是核心：`valid_index`（非 ignore）、`local_index = index - vocab_start_index`、`index_in_partition` 三个掩码决定「这个 token id 是否归我管」；归我管的 rank 取出 shifted logit，其余 rank 贡献 0，L224-L225 的 SUM all-reduce 让每个 rank 都拿到完整的目标 logit。

`_VocabParallelLogProbs.backward`：[vocab_parallel_ops.py:L234-L259](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L234-L259)。注释点明公式 `d logp(i)/d z_j = onehot(i)_j - softmax(z)_j`；L241-L243 处理全 ignore 行与全 `-inf` 行——这些行输出是常数，梯度必须**精确为 0**（乘 0 不够，因为全掩码行的 softmax 是 NaN）。eager 分支在 L251-L258 按行块重算 `exp(log_softmax)` 后 `scatter_add_` 叠加 one-hot 项。

`_VocabParallelEntropy`：[vocab_parallel_ops.py:L262-L301](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L262-L301)。Triton 路径的巧妙之处在 `_triton_entropy`（[L128-L144](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L128-L144)）：一次读入同时累计 max、sum-exp 和 \(W=\sum_j e^{z_j-m}(z_j-m)\)，归一化后 \(H = \log S - W/S\)，与统计共享同一次 MAX 和 SUM all-reduce。返回前 `entropy.clone()`（L280）是为了允许调用方对输出**原地掩码**（`masked_fill_`）而不污染 saved tensor。

`_VocabParallelKLDiv`：[vocab_parallel_ops.py:L311-L388](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L311-L388)。辅助函数 `_order_kl_arguments`（[L304-L308](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L304-L308)）实现 forward/reverse 的统一：KL(A||B) 以 A 加权，前向 KL 取 A=teacher、反向取 A=student，一个内核服务两个方向。反向 eager 路径 L370-L382 就是上面两条 KL 梯度公式；`ctx.needs_input_grad[:2]`（L345）让 detach 的 teacher 拿不到梯度张量。

数值边界处理集中在两个小函数：`_weighted_by_probs`（[L176-L182](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L176-L182)）用「log-prob 是否为 -inf」而不是「prob 是否为 0」来判零——有限的 log-prob 也可能 exp 后下溢成 0，`0 * -inf` 会把整行变 NaN；`_kl_terms`（[L185-L190](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L185-L190)）处理支撑集不匹配：A 有质量而 B 掩掉的 token，即使 \(p_A\) 下溢成 0，KL 也必须保持 +inf（对应测试 `test_kl_stays_infinite_when_target_probability_underflows`，teacher logit 为 -200 时 exp 下溢仍判 inf）。

#### 4.2.4 代码实践

**实践目标**：用仓库自带的 CPU 回归确认三个算子对 dense PyTorch 参考的值与梯度一致性。

**操作步骤**（在仓库根目录）：

```bash
PYTHONPATH=tests:. OMP_NUM_THREADS=4 pytest \
  tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py -q \
  -k "logprobs or entropy or kl_div and not TestVocabParallel and not triton"
```

随后打开测试文件阅读三个参考实现：[test_vocab_parallel_ops.py:L18-L35](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L18-L35)（`reference_logprobs/reference_entropy/reference_kl` 全部是 `F.log_softmax` + gather/kl_div 的朴素组合）。

**需要观察的现象**：单进程 CPU 下全部通过；`test_detached_teacher_gets_no_gradient`（[L334-L341](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L334-L341)）断言 `teacher.grad is None`。

**预期结果**：值与梯度 `torch.testing.assert_close` 全部通过（这正是它们作为契约测试的存在意义）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`vocab_parallel_logprobs` 里目标 token 的 logit 是怎么跨 shard 汇聚的？为什么每个 rank 不能只 gather 自己本地的？
**答案**：每个 token id 只落在某一个 shard 的区间 `[vocab_start, vocab_end)` 内；持有它的 rank 把 `logit - global_max` 写入、其他 rank 写 0，一次 SUM all-reduce 后所有 rank 都拿到该值（[vocab_parallel_ops.py:L217-L225](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L217-L225)），再统一减 `log_sum_exp`。

**练习 2**：反向为什么可以完全不做通信？
**答案**：约定所有 TP rank 对**同一份**复制损失做 backward。以 logprobs 为例，梯度 \(\mathbb{1}[j=i] - p_j\) 里 \(p_j\) 由本地列 + 已保存的全局统计算出，one-hot 项只落在持有目标 id 的本地列上；跨 shard 的求和已在 loss 值本身（前向）完成，反向对每个本地 logit 是闭式的。

**练习 3**：全 `-inf` 行（padding）为什么梯度必须显式置 0 而不是乘 0？
**答案**：全 `-inf` 行的 softmax 是 NaN，`NaN * 0 = NaN`，会把 NaN 注入共享 logits 的梯度（见注释 [vocab_parallel_ops.py:L241-L243](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L241-L243)）；所以用 `zero_grad_row` 掩码直接覆盖为 0，对应测试 `test_ignored_fully_masked_row_gets_zero_gradient`。

---

### 4.3 广义 JSD 与 beta 端点约定（PR #8958）

#### 4.3.1 概念说明

JSD 是 GKD 式蒸馏的标准目标。本周期（提交 `da79d7447`，#8958）补上了缺失的 interior-beta 广义 JSD。它的特殊之处：**mixture 是概率的凸组合** \(m=(1-\beta)q+\beta p\)，不是 logit 的组合。对分片 logits 各自做局部 softmax 再混合，得到的是**另一个目标**——所以必须先完成全局归一化（4.1 的统计阶段），再进第二遍求值。这是 JSD 与 KL 的本质实现差异：KL 的单遍统计就能携带全部信息，JSD 的非线性 mixture 要求两遍读输入。

**beta 端点约定**（与 Core 已有的 `generalized_jsd_loss` 兼容）是显式分支：

- `beta=0` → 前向 KL（teacher||student）
- `beta=1` → 反向 KL（student||teacher）
- \(0<\beta<1\) → interior JSD

注意 \(\beta \to 0\) 时 interior 表达式的连续极限并不是 \(\mathrm{KL}(p\|q)\)（mixture 退化时 KL(q||m) 项的系数也趋于 0），所以端点是**独立选择的语义**而非极限——docstring 与文档都专门强调了这一点。

#### 4.3.2 核心流程

```
forward(student, teacher, beta):
  统计: student/teacher 的 global_max + log_sum_exp   (4.1 的协议)
  逐行块:
    log_q = (z_q - gmax_q) - lse_q        # 全局归一化后的 log 概率
    log_p = (z_p - gmax_p) - lse_p
    log_m = logaddexp(log_q + log(1-beta), log_p + log(beta))   # mixture
    E_q[log q - log m]  -> kl_to_mixture[0]   # KL(q||m) 的期望项
    E_p[log p - log m]  -> kl_to_mixture[1]   # KL(p||m) 的期望项
  一次 SUM all-reduce 聚合两个期望   (打包成一个 [2, rows] 张量)
  return (1-beta)*kl[0] + beta*kl[1]
backward:
  d/dz_q = (1-beta) * q_j * (log_ratio_q - KL(q||m)) * upstream
  d/dz_p = beta     * p_j * (log_ratio_p - KL(p||m)) * upstream
  （穿过 mixture 的导数在两个 KL 项之间相消, 见 4.3.3）
```

`logaddexp` 保证 mixture 在极小概率下仍然精确（\(\log m\) 直接由 log 域组合）。

#### 4.3.3 源码精读

公共入口与校验：[vocab_parallel_ops.py:L540-L572](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L540-L572)。L559-L568 依次校验形状相同、有词表维、同设备、浮点、`0<=beta<=1`；L569-L570 是端点分支——直接委托给 `vocab_parallel_kl_div(reverse=(beta==1))`。

eager 的 mixture 数学在 `_jsd_log_ratios`：[vocab_parallel_ops.py:L391-L395](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L391-L395)，一行 `logaddexp` 完成 log 域混合。

`_VocabParallelJSD.forward`：[vocab_parallel_ops.py:L410-L436](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L410-L436)。两条路径汇合于 L431-L432：两条 KL-to-mixture 期望**打包进一个 `[2, rows]` 张量**做单次 SUM all-reduce；注释（L424-L425）说明原因——反向的 softmax Jacobian 需要这两条期望，打包后 backward 不再需要任何通信，即使 student 和 teacher 都要梯度。

`_VocabParallelJSD.backward`：[vocab_parallel_ops.py:L438-L470](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L438-L470)。L458 的注释「Derivatives through the mixture cancel between its two KL terms」是这条梯度的关键：把 loss 对 \(z_q\) 微分时，\(m\) 也依赖 \(z_q\)，但两条 KL 项里穿过 \(m\) 的贡献恰好抵消，剩下的就是 4.3.2 的公式（L459-L464 按 `(1-beta)`/`beta` 加权、乘 upstream、掩掉 undefined 行）。

性能量级（引自 PR #8958 提交说明中的 exploratory A/B：B70，N=1024，V=151936，BF16，beta=0.3，冻结 teacher，前向+反向）：

| TP | 实现 | 峰值显存 GiB/rank | 中位耗时 ms |
| --- | --- | ---: | ---: |
| 2 | gather 双方 logits + dense PyTorch JSD | 6.671 | 112.17 |
| 2 | 词表并行 eager JSD | 1.057 | 58.38 |
| 2 | 词表并行 Triton JSD | **0.435** | **3.08** |
| 4 | 词表并行 eager JSD | 0.841 | 31.14 |
| 4 | 词表并行 Triton JSD | 0.217 | 3.41 |

即该场景下 Triton 版对 dense 版约 30 倍以上提速（112.17 → 3.08 ms）、峰值显存约 1/15（6.671 → 0.435 GiB），且与既有 Triton reverse KL 同档（3.29 ms）。此表来自 PR 描述，**待本地验证**。

#### 4.3.4 代码实践

**实践目标**：用 FP64 dense oracle 验证 `vocab_parallel_jsd(beta=0.3)` 的值与梯度。

**操作步骤**（示例代码，单进程 CPU）：

```python
# 示例代码
import torch
from deepspeed.sequence.vocab_parallel_ops import vocab_parallel_jsd
import torch.nn.functional as F

def reference_jsd(s, t, beta):           # 仿照测试里的 FP64 dense 参考
    log_q = F.log_softmax(s.double(), dim=-1)
    log_p = F.log_softmax(t.double(), dim=-1)
    mixture = (1 - beta) * log_q.exp() + beta * log_p.exp()
    return ((1 - beta) * F.kl_div(mixture.log(), log_q, reduction="none", log_target=True) +
            beta * F.kl_div(mixture.log(), log_p, reduction="none", log_target=True)).sum(-1)

torch.manual_seed(0)
s = torch.randn(2, 5, 17, requires_grad=True)
t = torch.randn(2, 5, 17)
actual = vocab_parallel_jsd(s, t, beta=0.3)
expected = reference_jsd(s.detach(), t, 0.3)
print(torch.allclose(actual.double(), expected, atol=1e-6, rtol=1e-5))   # 值
w = torch.randn(2, 5)
(actual * w).sum().backward()
es = s.detach().clone().requires_grad_(True)
(reference_jsd(es, t, 0.3) * w).sum().backward()
print(torch.allclose(s.grad.double(), es.grad, atol=1e-6, rtol=1e-5))    # student 梯度
```

**需要观察的现象**：两个 `allclose` 均为 True；`actual.dtype == torch.float32`；teacher 未 requires_grad 时 `t.grad is None`。

**预期结果**：fp32 输入对 FP64 参考在 `atol=1e-6, rtol=1e-5` 内一致（与 [test_vocab_parallel_ops.py:L113-L131](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L113-L131) 的容差一致）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`beta=0` 时调用 `vocab_parallel_jsd` 实际执行什么代码？
**答案**：不会进 `_VocabParallelJSD`，而是在 [vocab_parallel_ops.py:L569-L570](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L569-L570) 直接转投 `vocab_parallel_kl_div(reverse=(beta==1))`：beta=0 是前向 KL，beta=1 是反向 KL。

**练习 2**：为什么 interior JSD 必须「两遍读输入」，而 KL 单遍就够？
**答案**：KL 的被积项是 log-ratio，可以先在本地以任意中心累计加权和再全局重标定（线性）；JSD 的 mixture \(m=(1-\beta)q+\beta p\) 是**概率的非线性函数**，而概率只有知道全局配分函数后才有定义——所以第一遍必须先完成归一化统计，第二遍才能求值 mixture。

**练习 3**：把 KL(q||m) 与 KL(p||m) 的期望打包成一个张量做一次 all-reduce，除了省一次集体通信还有什么好处？
**答案**：backward 需要 `kl_to_mixture[0]` 和 `[1]` 两项（梯度公式里的常数项），前向打包聚合后它们已保存在 `ctx` 里，反向**完全无通信**——即使 student 与 teacher 都需要梯度（见 [vocab_parallel_ops.py:L423-L432](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L423-L432) 的注释）。

---

### 4.4 JSD 的 Triton 内核：统计 / 散度 / 反向

#### 4.4.1 概念说明

eager 行块回退每块要发射十几个独立的 memory-bound PyTorch 内核（exp、sub、mul、sum……），每个 logit 被读多遍。Triton 内核把每遍的逻辑fuse成「每个 logit 只读一次」：每行一个 program，词表维按 `BLOCK` 循环，统计量在寄存器里增量维护。JSD 有三个内核，恰好对应三个阶段：

1. **`jsd_softmax_stats`**：一次同时读 student/teacher 两行，产出两份本地 (max, sum-exp)；
2. **`jsd_forward`**：拿到全局统计后第二遍读入，产出两条 KL-to-mixture 期望的本地部分；
3. **`jsd_backward`**：第三遍读入，在寄存器里重算概率并直接写出梯度。

#### 4.4.2 核心流程

`jsd_softmax_stats` 内核里的 **online rescaling** 是理解所有统计内核的钥匙。每条 lane 维护「到目前所见块为止」的 running max 与 running sum；当新块的 max 抬高时，旧 sum 乘 \(e^{m_{old}-m_{new}}\) 重标定：

\[ S \leftarrow S \cdot e^{m - m'} + e^{z - m'} \qquad (m' = \max(m, z)) \]

跨块循环结束后，各 lane 再统一重标定到行 max 并归约。`jsd_forward` 则每块直接算 `log_q`/`log_p` → `_jsd_ratios` 求 log-ratio → 概率加权和累计。`jsd_backward` 用 `NEED_Q_GRAD`/`NEED_P_GRAD` 两个 `tl.constexpr` 让编译器在编译期裁剪掉不需要的梯度写出分支。

#### 4.4.3 源码精读

`_jsd_softmax_stats_kernel`：[vocab_parallel_softmax.py:L37-L65](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L37-L65)。L51-L56 是 online rescaling 本体：`next_q_max` 抬高时旧 `q_sum` 按 `exp(q_max - next_q_max)` 缩放；`tl.where(next_q_max == -inf, 0, ...)` 防 `exp(-inf- -inf)` 的 NaN。L62-L65 把 student/teacher 两份 (max, sum) 分别写进 `max_ptr`/`sum_ptr` 的前后两段。

`_jsd_ratios`（log 域 mixture）：[vocab_parallel_softmax.py:L67-L73](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L67-L73)。手写 `max + log(exp(a-max)+exp(b-max))` 的 logaddexp，避免 log 域溢出。

`_jsd_forward_kernel`：[vocab_parallel_softmax.py:L75-L96](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L75-L96)。L90-L94：每块算出两条 log-ratio，`exp(log_q) * q_ratio` 概率加权累计；`mask & (log_q != -inf)` 掩掉被 mask 的 token（对应 eager 的 `_weighted_by_probs`），NaN 概率不进累加器。

`_jsd_backward_kernel`：[vocab_parallel_softmax.py:L98-L129](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L98-L129)。L122-L129 就是 4.3 的梯度公式逐块直译：`(1-beta)*exp(log_q)*(q_ratio - q_kl)*upstream`；概率在寄存器里重算，**从不物化完整 fp32 概率张量**；`undefined` 行（任一分布全 -inf）整行写 0。

Python 包装层：`jsd_softmax_stats`（[L474-L489](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L474-L489)）、`jsd_forward`（[L465-L471](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L465-L471)）、`jsd_backward`（[L492-L517](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L492-L517)，把 `math.log(beta)`/`math.log1p(-beta)` 预计算成标量传给内核）。统一由 `_launch`（[L363-L369](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L363-L369)）发射：空 token 批直接返回（循环零次、统计内核写 -inf/0）；`BLOCK` 取 `min(4096, next_pow2(vocab))` 且不低于 16。

在 `vocab_parallel_ops` 侧的接线：forward 里 `jsd_softmax_stats` 的输出被 `_triton_softmax_stats` 通过 `local_stats` 参数**复用**（[vocab_parallel_ops.py:L416-L419](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L416-L419)），避免统计算两遍。

统计内核的块上限刻意更小：`_STATS_MAX_BLOCK = 2048`（[vocab_parallel_softmax.py:L29-L33](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L29-L33)）——单遍统计内核每 lane 最多维护 6 个运行值，小块让它们留在寄存器里（注释：H100 上 KL 统计比 4096 块快约 1.2 倍）。

#### 4.4.4 代码实践

**实践目标**：确认 Triton 路径与 eager 路径在边界输入（半 mask 词表、全 -inf 行、极端 logit、三种布局）下数值一致。

**操作步骤**：先读 [test_vocab_parallel_ops.py:L176-L225](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L176-L225)（`test_jsd_triton_matches_eager`），注意它如何构造 `sequence_slice`（`padded[:, :-1]`，外圈填 NaN——读到任何越界位置都会在输出里暴露）与 `strided_vocab`（`padded[:, ::2]`）两种布局；然后（有 GPU 的机器上）：

```bash
PYTHONPATH=tests:. OMP_NUM_THREADS=4 pytest \
  tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py -q -k jsd_triton
```

**需要观察的现象**：三种 dtype × 三种布局 × 三个 beta 全部通过；无 Triton/非加速器设备时这些用例 `pytest.skip`（L180-L182 的探测）。

**预期结果**：GPU 上 15 个参数组合全部通过；CPU 上跳过（属预期行为）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：online rescaling 里 `tl.where(next_max == -inf, 0.0, sum*exp(...)+exp(...))` 这个分支在防什么？
**答案**：一条 lane 如果至今只见过 `-inf`（空列或全 mask 块），`exp(-inf - -inf)=exp(nan 输入)=NaN` 会污染 running sum；此时保持 sum 为 0，等真正见到有限值再开始累计（[vocab_parallel_softmax.py:L53-L56](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L53-L56)）。

**练习 2**：`_jsd_backward_kernel` 为什么用 `NEED_Q_GRAD: tl.constexpr` 而不是运行时 if？
**答案**：`tl.constexpr` 在编译期特化内核：teacher 被 detach 时整段 teacher 梯度计算与写出被裁剪掉，不付出无用的读写；两个标志组合出 4 个特化版本。Python 侧对应 `ctx.needs_input_grad[:2]`（[vocab_parallel_ops.py:L447](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L447)）。

**练习 3**：为什么反向内核敢在寄存器里重算概率，而不像常见 fused softmax 那样保存概率缓冲？
**答案**：保存 `[rows, local_vocab]` 的 fp32 概率正是 dense 路径显存爆炸的来源；这里前向已把 `global_max/lse/kl_to_mixture` 存进 ctx（每行几个标量），反向逐块 `exp(log_prob)` 重算即可，峰值显存与词表宽度解耦。

---

### 4.5 eager 行块回退、tp_group 参数与工程约束

#### 4.5.1 概念说明

**分派规则。** Triton 可用（`deepspeed.HAS_TRITON` 且加速器支持且张量在加速器上）走 fused 内核，否则 eager 行块。JSD 有一个 XPU 特例：XPU 的全局 Triton 开关仍关闭，但若安装的 Triton 带激活的 Intel 后端，则单独放行 JSD（#8958 的改动之一），不动其他算子的分派。

**tp_group 语义。** 所有算子接受 `tp_group=None`：`pg_utils.is_parallel(None)` 返回 False，集体通信被跳过，于是单进程也能用同一份代码（[`pg_utils.is_parallel` 的 docstring](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/comm/pg_utils.py#L16-L28) 明确 `None` 表示「缺省的并行维」，绝不会被解释成 world group）。传入真实进程组后，约定的硬约束是：**student 与 teacher 必须同序分片**（同 TP 组、同词表区间），且每个 rank 回传同一份逐 token 损失。

**分片元数据。** `vocab_parallel_logprobs` 需要知道本 rank 的 `[vocab_start_index, vocab_end_index)` 才能判断 token id 归谁管。默认每次调用都走 `_resolve_vocab_metadata` 做一次**集体校验**（支持不等分 shard，但必须显式给边界）；固定分片的调用方可以校验一次后把 `global_vocab_size` 连同显式边界传入以跳过该集体操作（值被直接信任），`index` 的范围检查仍然每次都做。

#### 4.5.2 核心流程

```
调用 vocab_parallel_*(logits_shard, ..., tp_group):
  _use_triton(logits)?  ── 是 ──> _triton_rows: row_layout 可原地读则免拷贝
        │                         否则 contiguous() 后读
        否
        ↓
  _flatten: reshape 成 [rows, local_vocab] + _row_chunks 行块循环
  所有 all-reduce 前都有 pg_utils.is_parallel(tp_group) 守卫
```

`row_layout` 认两种可原地寻址的布局：连续张量，以及沿序列维切的 `logits[:, :-1]`（causal-shift 视图，无法免拷贝地 reshape 成二维）。其他布局（比如词表维带 stride 的视图）先 `contiguous()`。

#### 4.5.3 源码精读

分派与行准备：`_use_triton`（[vocab_parallel_ops.py:L44-L48](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L44-L48)）、`_triton_rows`（[L51-L56](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L51-L56)）、`_flatten`（[L198-L201](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L198-L201)，注释说明空 shard 下 `reshape(-1, 0)` 有歧义、必须显式给行数）。

XPU 特例的分派：`_use_jsd_triton`（[vocab_parallel_ops.py:L398-L407](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L398-L407)）在常规判断之外探测 `triton.backends` 里是否有激活的 Intel 后端，只放行 JSD 这一个算子。

`row_layout` 的原地寻址：[vocab_parallel_softmax.py:L334-L356](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L334-L356)。把前导维度合并成最多两级行 stride（`inner/outer/row`），配合内核里的 `_row_start`（[L131-L133](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/vocab_parallel_softmax.py#L131-L133)）直接按地址访问任意行——`logits[:, :-1]` 因此不用先拷贝成二维视图。

API 层校验与元数据：[vocab_parallel_ops.py:L493-L510](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L493-L510)（index 形状、`vocab_start/end` 必须成对、`global_vocab_size` 必须伴随显式边界、index 范围检查）；底层集体校验在 [cross_entropy.py:L143-L195](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/cross_entropy.py#L143-L195)：`_validate_vocab_shard_bounds` all-gather 各 rank 的 `(start, end, size)` 验证它们构成从 0 开始的连续不重叠划分且无空 shard（L160-L162 的报错正是空 shard 测试所断言的 "empty vocabulary shard"）；`_resolve_vocab_metadata` 的 docstring 解释了为何不做进程级缓存（会钉住已销毁的进程组）。

关闭 Triton 的开关是模块级属性：`deepspeed/__init__.py` 用 `__getattr__` 惰性计算 `HAS_TRITON`，且**允许赋值覆盖**——`deepspeed.HAS_TRITON = False` 即可强制全程 eager（[deepspeed/__init__.py:L36-L40](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/__init__.py#L36-L40) 的注释写明这一用法）。

用法侧的完整示例在用户文档：[autotp-training.md:L373-L419](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/autotp-training.md#L373-L419)，从 `model.lm_head`（AutoTP 装的 `VocabParallelLinear`）取 `mp_group` 与词表边界，覆盖 policy-gradient / 蒸馏 / top-k teacher 三类负载。

#### 4.5.4 代码实践

**实践目标**：验证 eager 回退与 Triton 路径数值等价，并体会 `tp_group=None` 的单进程语义。

**操作步骤**（示例代码，CPU 即可运行）：

```python
# 示例代码：CPU 上必然走 eager; 若在 GPU 上, 再设 deepspeed.HAS_TRITON=False 对比
import torch
import deepspeed
from deepspeed.sequence.vocab_parallel_ops import vocab_parallel_jsd

torch.manual_seed(0)
s = torch.randn(2, 4, 19, requires_grad=True)[:, :-1]   # 非连续 causal-shift 视图
t = torch.randn(2, 4, 19)[:, :-1]

def run():
    x = s.detach().clone().requires_grad_(True)
    out = vocab_parallel_jsd(x, t, beta=0.5, tp_group=None)  # None -> 不通信
    out.sum().backward()
    return out.detach(), x.grad

eager_out, eager_grad = run()
if torch.cuda.is_available():
    deepspeed.HAS_TRITON = False   # 强制 eager, 对照 GPU Triton 结果
print(eager_out.shape, eager_grad.shape)
```

**需要观察的现象**：`tp_group=None` 时无任何分布式初始化也能运行；`[:, :-1]` 视图不报错；GPU 机器上切换 `deepspeed.HAS_TRITON` 前后两次输出逐元素一致（容差内）。

**预期结果**：输出形状 `[2, 3]`、梯度形状同 logits；两条路径一致（对照测试 `test_triton_kernels_match_eager_path`，[test_vocab_parallel_ops.py:L556-L574](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L556-L574)）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：teacher 和 student 的词表分片「顺序不同」（比如 teacher 按倒序分片）会发生什么？代码有没有防？
**答案**：结果会**静默错**——各 rank 拿到的「同列」其实不是同一个 token。API 只能校验形状/设备相同（[vocab_parallel_ops.py:L559-L566](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L559-L566)），无法看出顺序，所以这是写进 docstring 的调用方契约：同 TP 组、同序分片。

**练习 2**：`vocab_parallel_logprobs` 的 `global_vocab_size` 参数什么时候值得传？
**答案**：对固定分片反复调用时（训练循环里每步一次）。首次让它自己走 `_resolve_vocab_metadata` 集体校验拿到 `global_vocab_size`，之后连同显式 `vocab_start_index/vocab_end_index` 传入即可跳过每次的集体校验；但 `index` 范围检查仍在每次调用执行（[vocab_parallel_ops.py:L488-L510](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L488-L510) 的 docstring 与实现）。

**练习 3**：为什么输出张量要在返回前 `clone()`（entropy/KL 都这么做）？
**答案**：调用方常对逐 token 输出**原地掩码**（`loss.masked_fill_(pad, 0)`）；返回值若与 backward 需要的 saved tensor 同名共享存储，原地写会破坏梯度（[vocab_parallel_ops.py:L278-L280](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/vocab_parallel_ops.py#L278-L280) 注释，对应测试 `test_outputs_support_in_place_masking`）。

---

## 5. 综合实践

**任务**：把 4 个模块串起来，完成一次「FP64 dense oracle × TP1/TP2 × 边界输入 × eager/Triton」的完整验证，复现 `TestVocabParallelJSDTP` 的核心断言。

**步骤**：

1. **单进程 oracle**（CPU）：写一个 FP64 dense 参考实现（可直接抄 [test_vocab_parallel_ops.py:L38-L47](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L38-L47) 的 `reference_jsd`），对 `vocab_parallel_jsd(beta=0.3)` 验证逐 token 输出与 student 梯度（`atol=1e-6, rtol=1e-5`）。输入故意用 `torch.randn(2, 4, 19)[:, :-1]` 制造非连续 causal-shift 视图。
2. **边界输入**：
   - 空本地 shard 与不等分词表：参考 `test_uneven_shards_match_full_vocabulary`（[test_vocab_parallel_ops.py:L657-L689](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L657-L689)，用 `get_shard_size_list(17, 2, AutoTPMeta(), "lm_head")` 得到 9/8 的不等分）与 `test_empty_shard_does_not_desynchronize_ranks`（[L711-L747](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L711-L747)）；
   - 支撑集不交集（student/teacher 各持一半词表且互补）：`test_empty_shard_and_fully_masked_local_support`（[L784-L798](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L784-L798)），期望 JSD = `log(2)`、双方梯度为 0；
   - 大公共偏移（student +1e8、teacher -1e8，softmax 平移不变性必须保住）：`test_jsd_independent_logit_shifts`（[L147-L158](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L147-L158)）。
3. **TP=2 分布式**：仿照 `TestVocabParallelJSDTP.test_loss_and_gradients_match_dense`（[L753-L782](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py#L753-L782)），用 `torch.tensor_split(student, tp_size, dim=-1)[rank]` 切片、`tp_group=dist.get_world_group()`，断言本地梯度等于 FP64 全量梯度的对应切片。也可以直接运行仓库的分布式用例（PR #8958 验证时使用的命令）：

   ```bash
   CUDA_VISIBLE_DEVICES=0,1 OMP_NUM_THREADS=4 PYTHONPATH=tests:. \
     pytest --forked tests/unit/v1/sequence_parallelism/test_vocab_parallel_ops.py -q \
     -k 'TestVocabParallelJSDTP and (0.2 or empty or optimizer)'
   ```

4. **eager 回退**：设 `deepspeed.HAS_TRITON = False` 重跑第 1 步，确认数值不变（GPU 机器上才有意义；CPU 本来就是 eager）。
5. **（可选，需 GPU）端到端**：按 [autotp-training.md:L171-L180](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/autotp-training.md#L171-L180) 开 `tensor_parallel.vocab_parallel_lm_head: true`，用一个小模型（如 sshleifer/tiny-gpt2）跑前向，对 `model(input_ids).logits`（此时只有本地 shard）调用 `vocab_parallel_jsd`，用 `torch.cuda.max_memory_allocated` 对比关闭该开关（gather 全词表 + dense softmax）时 lm_head 处的峰值显存。

**预期结果**：1-4 步的所有断言与测试同名用例一致；第 5 步预期能观察到峰值显存显著下降（量级可对照第 4.3.3 节 PR 表：gather 路径约为词表并行路径的十余倍）。全部待本地验证。

## 6. 本讲小结

- **两遍结构**：本地 (max, sum-exp) 统计 → 一次 MAX + 一次 SUM all-reduce 得全局归一化 → 第二遍在本地 shard 上算逐 token 损失；通信量 `O(rows)`，从不 all-gather 完整 logits。
- **四个 autograd Function**（logprobs/entropy/KL/JSD）共享同一设计：前向只存输入与逐 token 统计，反向按行块（eager）或寄存器（Triton）**重算** softmax，反向零通信；前提是每个 TP rank backward 同一份 loss。
- **数值细节是正确性的主体**：max 与 lse 分开存防大数吃小数；`-inf` 掩码行的 NaN 用 log-prob 判零而不是概率；支撑集不匹配时 KL 保持 +inf；全 -inf 行梯度精确置 0。
- **广义 JSD（#8958）**：mixture 是概率的非线性组合，必须全局归一化后再求值（两遍输入）；两条 KL-to-mixture 期望打包成一次 SUM all-reduce，反向穿过 mixture 的导数相消；beta=0/1 是显式委托给 KL 的独立端点分支。
- **Triton 与 eager 双路径同语义**：统计内核用 online rescaling 单遍读入；XPU 有单独放行 JSD 的后端探测；`deepspeed.HAS_TRITON = False` 可强制 eager 对照。
- **工程契约**：student/teacher 必须同序分片；`tp_group=None` 表示单进程语义；`vocab_parallel_logprobs` 的分片元数据可校验一次后显式传入以省掉每次的集体校验；温度、掩码、归约全部留给调用方。

## 7. 下一步学习建议

- **向 LM head 方向延伸**：读 `deepspeed/sequence/cross_entropy.py` 的 `vocab_parallel_cross_entropy`（本讲的姊妹算子，多了 `sp_group` 与 reduction 语义）与 `deepspeed/sequence/linear_cross_entropy.py` 的 `vocab_parallel_linear_cross_entropy`——后者连本地 logits 都不物化，把 head 投影fuse进损失，是「词表并行损失」谱系的显存下限。
- **向 AutoTP 注入方向回看**：对照 `docs/_tutorials/autotp-training.md` 的 "Vocabulary-parallel LM Loss" 一节（L165 起），理解 `VocabParallelLinear` 如何提供 `mp_group`/`vocab_start_index`/`vocab_end_index`，把本讲算子的输入端补全。
- **向 Triton 内核工程深入**：以 `vocab_parallel_softmax.py` 为模板，阅读 `deepspeed/ops/triton_ops/` 下其他内核（如 group_gemm），体会 `tl.constexpr` 特化与块大小调优的通用手法；u9-l1/l2 的 OpBuilder 体系则是 Triton 之外的另一条内核工程线。
- **动手方向**：尝试把本讲的 `vocab_parallel_kl_div(reverse=True)` 接进一个最小的 policy-gradient 循环（用 u2-l1 的 SimpleModel 即可），体会「逐 token 输出 + 调用方掩码归约」的 API 风格在真实 loss 组装里的用法。
