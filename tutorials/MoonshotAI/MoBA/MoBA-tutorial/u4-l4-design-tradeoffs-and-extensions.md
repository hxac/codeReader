# 设计取舍与二次开发：从理解到改造 MoBA

## 1. 本讲目标

本讲是整套手册的收官篇。前面十讲我们逐文件读懂了 MoBA 的每一行核心代码；本讲退后一步，站在架构层面回答三个问题：

1. **为什么这样设计**——无参数 gate、`chunk_size`/`topk` 两个旋钮、全量/稀疏无缝切换，这三个关键取舍各自交换了什么。
2. **设计有什么坑**——退化路径的语义分叉、docstring 与代码的不一致等「读代码时容易想当然」的地方。
3. **怎么动手改**——给出一个分层的二次开发地图，并通过一个完整的 attention-sink 变体实践，走通「naive 原型 → efficient 镜像 → 对齐测试 → 基准测量」的改造闭环。

学完本讲，你应该能独立地为 MoBA 设计并验证一个新的块选择策略。

## 2. 前置知识

本讲默认你已读完 u3 与 u4 的前序讲义，以下结论直接使用，不再重新推导：

- **MoBA 前向管线**（u3-l1~u3-l5）：`calc_chunks` 算块元数据 → gate 打分选块 → varlen 重组 → 两路 flash-attn 用 LSE 在线合并；当前块归 self 支路（块内因果），历史整块经 top-k 进 moba 支路（非因果），因此 `moba_topk` 在高效实现里要减一。
- **反向**（u3-l6）：`MixedAttention.backward` 直接复用 `_flash_attn_varlen_backward` 两次；gate 是硬路由，梯度不经过 gate。
- **wrapper**（u4-l1）：`moba_layer` 适配 HF 接口，`q_len == kv_len` 判定 prefill 走 MoBA，decode 走全量 flash-attn。
- **测试与基准**（u4-l2、u4-l3）：naive 实现是黄金参考，`allclose + max + mean` 三层容忍度对齐；基准测试须 warmup + 双端 `cuda.synchronize`；实测加速比恒低于理论上限 \(\frac{S/2}{k \cdot c}\)（\(S\) 序列长、\(k\) 为 topk、\(c\) 为 chunk_size）。

另外两个本讲要用的术语：

- **硬路由（hard routing）**：选块决策是离散的（选中/未选中），不可导，与 softmax 那种「软」加权相对。
- **attention sink**：长上下文模型里观察到的一种现象——大量 query 的注意力集中落在序列开头的少数 token 上。本讲的综合实践就以「强制每个 query 额外关注第一个块」来模拟这一结构。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [README.md](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md) | 项目宣称的设计目标与性能数据 | 取舍的「官方说法」，逐条对照代码验证 |
| [moba/config.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py) | 仅两个int字段的配置对象 | 稀疏度的唯一入口 |
| [moba/moba_naive.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py) | 教学参考实现 | 改造时的第一个落点（黄金参考） |
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | 生产实现 | 改造时的第二个落点（必须与 naive 语义对齐） |
| [moba/wrapper.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py) | HF 适配层 | prefill/decode 混用两种注意力模式的活例子 |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | 正确性对齐测试 | 变体验证的模板 |
| [tests/test_moba_speedup.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py) | 性能基准 | 变体速度评估的模板 |

## 4. 核心概念与源码讲解

### 4.1 无参数 gate：零参数的自由与代价

#### 4.1.1 概念说明

经典 MoE（Mixture of Experts）的路由器是一个可学习线性层：`score = W·x`，`W` 是新增参数。MoBA 的 gate 则完全不同：

\[ \text{gate}(q, \text{chunk}_i) = \langle q,\ \bar{k}_i \rangle,\qquad \bar{k}_i = \frac{1}{c}\sum_{j \in \text{chunk}_i} k_j \]

即「query 与块内 K 均值的内积」。没有一行新增的 `nn.Parameter`，所以叫**无参数 gating**（README 第 14 行：*Parameter-less Gating Mechanism*）。

这个设计带来一个初看矛盾的组合，值得掰开理解：

- **好处**：零额外参数意味着零额外显存、零优化器状态、零初始化问题；而且分数复用了注意力本身的度量（内积），与 `q·k` 语义同源——「哪块 K 与我更对齐」天然就是「我该注意哪里」的合理代理。
- **代价**：README 第 21 行明确警告——MoBA **需要继续训练**，不是即插即用。原因是一条清晰的因果链：
  1. 无参数 → gate 没有独立参数去「学会适应新约束」；
  2. top-k 硬路由切断了每个 query 与未选中块的注意力通路；
  3. 预训练权重是在「全量注意力」下习得的，直接替换等于在推理时改变信息通路，输出分布必然漂移；
  4. 唯一的适应途径是继续训练 `q`/`k` 投影本身——**gate 的结构不可学习，但行为可训练**：`q`、`K` 变了，`⟨q, k̄⟩` 的排序就变了，选块行为随之演化。

#### 4.1.2 核心流程

把「继续训练」拆成训练循环里的因果链：

```text
前向：  q, k → 块内 K 均值 k̄ → gate = ⟨q, k̄⟩ (fp32)
        → top-k 硬路由（不可导，梯度断开）
        → 仅对被选中的块计算注意力（可导）
反向：  梯度只流经被选中块的注意力路径 → 更新 q/k/v 投影
下一轮： q/k 已更新 → gate 分数变化 → 选块模式变化
```

注意梯度在 gate 处是**断开**的：gate 张量的消费者全部是 `topk`、`scatter_`、`isinf` 这类不可导操作，产出的是布尔掩码而不是浮点参与输出。也就是说，模型无法通过梯度直接「告诉」gate「你选错了」；它只能通过被选中块的注意力损失间接地漂移 q/k。这是硬路由的通病，也是二次开发时如果想「参数化 gate」必须正视的约束（见 4.4）。

#### 4.1.3 源码精读

**gate 权重的构造（naive 版）**——对每块 K 求均值得到代表向量：

- [moba/moba_naive.py:L43-L50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L43-L50)：逐块切片 `k_[block_start:block_end].mean(dim=0, keepdim=True)` 拼成 `[N, H, D]` 的 `key_gate_weight`。注意这里没有任何可学习参数——它是由当前输入的 K **即时推导**出来的。

**高效版的对应实现**——先 gather 成稠密候选块再求均值：

- [moba/moba_efficient.py:L335-L340](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L335-L340)：`filtered_kv[:, 0].view(...).mean(dim=1)`。与 naive 语义完全一致，只是组织方式从「Python 循环切片」变成「一次 gather + 一次规整 mean」。

**fp32 打分**——两版都刻意在 fp32 里算 gate：

- [moba/moba_naive.py:L52-L57](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L52-L57)：注释写明 `use fp32 to avoid precision issue in bf16`。
- [moba/moba_efficient.py:L341-L347](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L341-L347)：注释 `float logit on the fly for better gate logit perception`。分数只用于**排序**、不进入输出，但 bf16 的舍入误差足以让两个本应相邻的分数翻转，进而翻转 top-k 决策——选块差异会让 naive 与 efficient 的输出产生肉眼可见的分叉（u2-l1 讲过），这是对齐测试要守护的重点。

**gate 的消费者全部不可导（高效版）**：

- [moba/moba_efficient.py:L365-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L365-L371)：`torch.topk`、`gate.isinf()`、`scatter_`、`logical_and`——这条链上没有任何操作能向 gate 回传梯度，印证了 4.1.2 的「梯度断开」。

**README 的两条宣称对照**：

- [README.md:L13-L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L13-L15)：可训练块稀疏注意力、无参数 gating、全量/稀疏无缝切换三大卖点。
- [README.md:L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L21)：*requires continue training … not a drop-in sparse attention solution*。这句 Note 就是 4.1.1 因果链的官方表述。

#### 4.1.4 代码实践：观察「梯度不经过 gate」

**实践目标**：用实验证实「未被任何 query 选中的块，其 K/V 梯度为零」，从而亲眼看到硬路由切断了梯度。

**操作步骤**（GPU 环境；无 GPU 见下方替代方案）：

1. 写一个独立脚本（示例代码，非项目原有文件），复用测试的数据构造：

```python
# practice_grad_flow.py（示例代码）
import torch
from moba.moba_efficient import moba_attn_varlen
from tests.test_moba_attn import generate_data

q, k, v, cu_seqlen, max_seqlen = generate_data(
    batch=1, seqlen=1024, num_q_head=2, num_kv_head=2,
    headdim=128, dtype=torch.bfloat16)
o = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen,
                     moba_chunk_size=256, moba_topk=2)
o.backward(torch.randn_like(o))
```

2. 把 `k._grad` 沿序列维按 256 一块求和：`k._grad.abs().sum(dim=(1,2))` 后 `view(4, 256).sum(dim=1)`。
3. 同时用 u2-l2 的方法导出块选择掩码（跑一份 naive 版并保存 `need_attend`）。

**需要观察的现象**：每个块位置上的梯度范数，与「该块被多少 query 选中」正相关；从未被选中的历史块梯度接近 0（最后一列块除外——它由 self 支路覆盖，见 u3-l2）。

**预期结果**：梯度热力图与块选择热力图形状一致。**待本地验证**（需要 GPU 与 flash-attn 环境）。

**无 GPU 替代方案（源码阅读型）**：在 [moba/moba_efficient.py:L345-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L345-L371) 中列出张量 `gate` 的所有下游消费者，逐一标注每个操作是否可导，得出「不存在浮点回传路径」的结论——这同样能完成验证，且不需要运行。

#### 4.1.5 小练习与答案

**练习 1**：既然 gate 无参数，为什么说它「可训练」？

**答案**：gate 分数由 `q` 与块内 K 均值内积决定，`q`/`k` 来自模型已有的投影权重，继续训练时这些权重被更新，分数排序随之变化。即「结构零参数、行为经由 q/k 可训练」——这正是零参数设计仍然需要 continue training 才能收敛到好选块策略的机制。

**练习 2**：如果给 gate 加一个可学习的投影（如 `score = ⟨Wq, k̄⟩`），至少要面对哪两个新问题？

**答案**：① 梯度路径问题——top-k/scatter 链不可导，`W` 拿不到直接梯度，需要引入辅助损失或改成软路由（softmax 加权）；② 对齐问题——`MixedAttention` 的手工反向与 naive 参考实现都要同步扩展，且 fp32 打分、块级掩码等细节都得为新的分数函数重新核对。这也解释了原作者为什么选择无参数方案：它把「学路由」这个问题完全外包给了已经存在的 q/k 语义。

**练习 3**：两份实现为什么都坚持在 fp32 里算 gate？

**答案**：gate 分数只用于 top-k 排序，但 bf16 舍入可能让相邻分数翻转、改变选块集合；选块一变，输出差异远大于数值精度本身的量级，naive 与 efficient 的对齐测试（`allclose(atol=2e-2)`）就会被打破。

### 4.2 稀疏度参数 chunk_size 与 topk：一根旋钮的两端

#### 4.2.1 概念说明

MoBA 的全部可调稀疏度浓缩在两个字段里：

- [moba/config.py:L4-L7](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py#L4-L7)：`MoBAConfig` 只有 `moba_chunk_size` 与 `moba_topk`，连默认值都没有——作者强迫使用者显式决定稀疏度。

这两个参数共同决定每个 query 实际参与的 KV 数量约为 \(k \cdot c\)（\(k\) = topk，\(c\) = chunk_size；当前块可能不满，故为近似）。对照全量因果注意力的平均参与量 \(S/2\)，得到理论加速上限：

\[ \text{speedup}_{\text{theory}} \approx \frac{S/2}{k \cdot c} \]

两个参数的权衡方向截然不同：

| 参数 | 调小 | 调大 | 附带开销 |
| --- | --- | --- | --- |
| `chunk_size` | 块更细、选择更精准、浪费的无关 KV 更少 | 块更粗、gate 矩阵更小、段更少 | 块数 \(N=\lceil S/c\rceil\) 增多 → gate 矩阵 \([N,H,S]\)、varlen 段数、索引重排的 glue 开销全变大 |
| `topk` | 更稀疏、更快、信息通路更窄 | 更接近全量、质量上限更高 | \(k\cdot c \to S/2\) 时收益归零，只剩纯开销 |
| 乘积 \(k\cdot c\) | —— | —— | 决定计算量；乘积相同的不同 (k, c) 组合，速度相近但**选择模式**完全不同 |

#### 4.2.2 核心流程

两个参数在高效实现管线里的落点：

```text
chunk_size ──→ calc_chunks（块边界、候选块、过滤）      L305-L311
          ──→ gate 矩阵规模 [N, H, S]                   L345-L347
          ──→ moba 段的 KV 长度（cu_seqlen_kv 等差步长）  L415-L423
topk     ──→ 减一并钳制：min(topk-1, num_filtered_chunk) L313-L315
          ──→ topk 选择宽度                              L365
          ──→ moba_seqlen_q（每段收集的 query 数）        L379
```

两端的自动退化（详见 4.3）：

- `topk = 1` 或候选块为空 → `need_moba_attn = False` → 直接返回全量因果 flash-attn（L317-L321）。
- `topk ≥ 块数` → 选中所有因果合法块 → 数学上等价全量注意力，但**代码不会自动走捷径**，仍然付出 gate + varlen 重组 + LSE 合并的全部 glue 成本——这是一个「隐性浪费」配置点。

用 \(S=32768\) 代入公式做一张纸面推演表（README 的 40x 数据点也在其中）：

| (chunk, topk) | 每 query 参与 KV | 占 \(S/2\) 比例 | 理论加速上限（对 flash） |
| --- | --- | --- | --- |
| (2048, 3) | 6144 | 37.5% | ≈ 2.7x（README 40x 是**相对 naive** 的数字） |
| (4096, 2) | 8192 | 50% | ≈ 2x |
| (512, 3) | 1536 | 9.4% | ≈ 10.7x（但 64 块 → glue 开销可能吃掉大半） |
| (2048, 8) | 16384 | 100% | ≈ 1x（纯亏：付全部 MoBA 开销却做全量计算） |

#### 4.2.3 源码精读

**topk 的减一与钳制**：

- [moba/moba_efficient.py:L313-L315](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L315)：`moba_topk = min(moba_topk - 1, num_filtered_chunk)`。减一是因为当前块固定由 self 支路处理（u3-l1）；钳制防止 topk 超过候选块数。注意钳制上界是 `num_filtered_chunk`（已剔除各序列最后一块），所以 **topk 拉满时选中的是「全部历史整块」，加上当前块后恰好等于全量因果注意力**——这是「全量是特例」的代码依据。

**naive 版的对应钳制**：

- [moba/moba_naive.py:L63-L65](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L63-L65)：`k=min(moba_topk, num_block)`。naive 不减一，因为它的候选集包含当前块（靠 `+inf` 必选）。两版参数语义一致：「每 query 总共注意 topk 个块」。

**chunk_size 决定 moba 段的等差边界**：

- [moba/moba_efficient.py:L415-L423](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L423)：`moba_cu_seqlen_kv` 是 `arange * moba_chunk_size`——候选块全是满块（u3-l2），所以 KV 侧边界退化为等差数列。这段代码同时说明：**每个 moba 段的 KV 长度恒等于 chunk_size**，topk 的作用全部体现在 Q 侧（L379 的 `moba_seqlen_q`）。

**README 的性能宣称及其条件**：

- [README.md:L60-L62](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L60-L62)：moba_efficient 相对 moba_naive 最高 40x，条件是 32K 序列、单头、块 2048、topk 3。结合上表：该配置相对 flash-attn 的理论收益只有 ≈2.7x——40x 里绝大部分来自「naive 用 O(S²) 稠密矩阵实现」这一基线的低效，而非稀疏本身（u4-l3 的结论）。

#### 4.2.4 代码实践：纸面推演 + 参数扫描

**实践目标**：在不动代码的前提下，学会预测一组 (chunk, topk) 配置是加速还是减速；有 GPU 时再用基准验证预测。

**操作步骤**：

1. 纸面推演：对 \(S=32768\)、\(S=8192\) 两档序列，分别计算 (chunk, topk) ∈ {512, 1024, 2048, 4096} × {2, 3, 8} 的 \(\frac{k\cdot c}{S/2}\) 占比与理论加速上限，标出「理论上限 < 1.5x」的配置（这些配置大概率实测比 flash 还慢，因为 glue 开销是固定税）。
2. 特别分析三个极端：
   - (512, 8)：占比 25%，但块数 64 → gate 矩阵与段数最多；
   - (4096, 8)：\(k\cdot c = 32768 = S\)，超过 \(S/2\)，**注定负优化**；
   - (任何 chunk, 1)：触发 4.3 要讲的退化分支。
3. GPU 环境可验证：仿照 [tests/test_moba_speedup.py:L83-L85](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L83-L85) 的 `__main__`，把第 2 步挑出的 3~4 个配置逐个跑一遍（计时方法见 u4-l3：warmup + 双端 synchronize）。

**需要观察的现象**：实测加速比与 \(\frac{S/2}{k\cdot c}\) 的排序基本一致，但普遍低于上限；块数多的配置（chunk=512）偏离上限更远。

**预期结果**：负优化预测的配置实测 speedup < 1。具体数字**待本地验证**（依赖 GPU 型号，量级判断不受影响）。

#### 4.2.5 小练习与答案

**练习 1**：固定乘积 \(k\cdot c = 6144\)，比较 (2048, 3)、(6144, 1)、(768, 8) 三种配置的实际行为。

**答案**：(6144, 1) 触发 `need_moba_attn = False`（topk-1=0），直接返回**全量**因果 flash-attn——根本不做稀疏（详见 4.3 的语义分叉）；(2048, 3) 是基准配置；(768, 8) 每 query 同样注意约 6144 个 KV，计算量相近，但块数多近 3 倍，gate 矩阵、段数、索引重排开销更大，且选择粒度更细。三个配置计算预算「相同」，行为却天差地别——这就是为什么两个参数不能合并成一个。

**练习 2**：为什么高效实现的候选块数要排除各序列最后一块，而 topk 钳制恰好用这个过滤后的块数？

**答案**：最后一块对本序列任何 query 要么是当前块（归 self 支路），要么整体在其未来（块边界不跨 batch，见 u3-l2），因果上永不可自由选中，剔除零损失；剩余候选全为满块，支撑 `cu_seqlen_kv` 的等差构造。因此「自由选择」的上界就是 `num_filtered_chunk`，钳制在此才准确。

**练习 3**：把 `chunk_size` 设成大于序列长度会发生什么？

**答案**：每条序列只有一块且必是最后一块 → `filtered_chunk_indices` 为空 → `num_filtered_chunk = 0` → `moba_topk = min(topk-1, 0) = 0` → 走全量 fallback。naive 版对应 `num_block = 1`、`k = 1`。两版在「序列很短」时都退化为普通因果注意力——这正是 u1-l2 观察到「短 prompt 下各后端输出一致」的根源。

### 4.3 全量/稀疏无缝切换：架构上的自由度

#### 4.3.1 概念说明

README 的第三个卖点是 *Seamlessly Transition between Full and Sparse Attention*（[README.md:L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L15)）。它的数学根基是：**全量因果注意力是 MoBA 的极限特例**——把每个 query 的选块集合扩大到「全部因果可见块」，MoBA 输出就精确等于全量注意力（两支路覆盖的 (query, key) 对互不重叠，并集恰为全量下三角，见 u3-l5/u3-l6）。反之，滑动窗口、sink 注意力这类**预定义偏置结构**没有这个性质——它们是全量注意力的固定子集，无法通过参数平滑地逼近全量。

「无缝」在代码里有三条具体路径：

1. **框架级**：同一份权重，`attn_implementation` 在 `flash_attention_2` / `moba` / `moba_naive` 之间切换，一行代码（[examples/llama.py:L13-L18](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L13-L18)）。
2. **参数级**：`topk` 拉满 → 选中所有候选块 → 数学上的全量（但代价见 4.2 的「隐性浪费」）。
3. **自动退化**：`need_moba_attn` 为假时静默退回全量 flash-attn。

还有一个容易被忽略的活例子：**一次 `model.generate()` 内部本来就在混用两种模式**——prefill 走 MoBA 稀疏，decode 走全量因果 flash-attn（wrapper 的分支逻辑）。这种混用之所以不破坏正确性，靠的正是「全量 ⊇ 稀疏」的包含关系：两种模式对模型来说是同一注意力函数在不同稀疏度下的取值。

#### 4.3.2 核心流程

三条切换路径的触发条件与代价：

```text
路径A 框架切换:  attn_implementation="flash_attention_2"      零额外开销
路径B 参数拉满:  topk ≥ 块数 → 选满候选块                      数学=全量，但付全部 MoBA glue 开销
路径C 自动退化:  topk=1 或候选块为空 → need_moba_attn=False    直接返回 flash_attn_varlen_func(causal=True)
```

**关键陷阱（本讲最重要的发现之一）**：路径 C 在两个实现里的语义**分叉**：

- 高效版 `moba_topk=1` → `min(1-1, ·) = 0` → fallback → **全量因果注意力**。
- naive 版 `k=min(1, num_block)=1` → top-k 只选一个块，而当前块被 `+inf` 必选 → **只注意当前块**（块内局部注意力）。

也就是说 `topk=1` 时，`moba_attn_varlen` 与 `moba_attn_varlen_naive` 的输出**不等价**。测试网格恰好用 `topk ∈ [2,3,4]`，从未覆盖这个分歧点。这不是 bug（高效版选择把 topk=1 解释为「不稀疏」更安全），但二次开发时必须知道：**退化路径的语义也要在两版实现对齐**，否则你的变体在边界配置下对不上。

#### 4.3.3 源码精读

**自动退化分支**：

- [moba/moba_efficient.py:L313-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L321)：`need_moba_attn = moba_topk > 0`，为假时 `return flash_attn_varlen_func(..., causal=True)`——注意这里用的边界是**原始 `cu_seqlens`**（整条序列）而非 `cu_chunk`（块），所以返回的是全序列因果注意力。触发条件：`topk ≤ 1`，或 `topk - 1 = 0` 经钳制为 0（候选块为空，即所有序列都不足两块）。

**naive 版在 topk=1 的对立行为**：

- [moba/moba_naive.py:L58-L67](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L67)：当前块列置 `+inf`、未来块列置 `-inf` 后，`k=1` 的 top-k 只能选中 `+inf` 的当前块 → `need_attend` 仅当前块为真。两个实现、同一个参数、两种注意力——分歧确凿。

**测试网格刻意避开了 topk=1**：

- [tests/test_moba_attn.py:L41-L42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L41-L42)：`moba_topk` 参数化为 `[2, 3, 4]`。u4-l2 指出过这一点；现在我们能解释**为什么**作者避开它——不是疏忽，是这个配置下两版本就没有对齐语义。

**一次生成中的模式混用**：

- [moba/wrapper.py:L54-L75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L54-L75)：prefill 分支（`q_len == kv_len`）转 FA 布局、GQA 复制 KV 后调用 MoBA 内核。
- [moba/wrapper.py:L76-L82](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L76-L82)：decode 分支直接 `flash_attn_func(..., causal=True)` 全量注意力；第 78 行 `TODO release paged attn implementation` 表明 decode 侧的稀疏化（配合 KV cache 分页）是明确的后续方向。「无缝切换」的宣称目前依赖 prefill/decode 可以各用一种模式这一事实。

**后端注册让框架级切换零成本**：

- [moba/__init__.py:L9-L11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L9-L11)：`register_moba` 把两个内核写入 `ALL_ATTENTION_FUNCTIONS`，切换只是换一个字符串。注意 `MoBAConfig` 在注册时经 `partial` 绑定，是**进程级全局**——想换 chunk/topk 要重新注册，不支持按请求切换（u1-l2 讲过）。

#### 4.3.4 代码实践：验证 topk=1 的语义分叉

**实践目标**：证实（或在没有 GPU 时，通过推演预判）`moba_topk=1` 时两个实现输出不等价。

**操作步骤**：

1. 写一个对比脚本（示例代码）：

```python
# practice_topk1.py（示例代码）
import torch
from moba.moba_naive import moba_attn_varlen_naive
from moba.moba_efficient import moba_attn_varlen

torch.manual_seed(0)
S, H, D = 1024, 2, 128
q = torch.randn(S, H, D, device="cuda", dtype=torch.bfloat16)
k = torch.randn(S, H, D, device="cuda", dtype=torch.bfloat16)
v = torch.randn(S, H, D, device="cuda", dtype=torch.bfloat16)
cu = torch.tensor([0, S], device="cuda", dtype=torch.int32)

o_eff = moba_attn_varlen(q, k, v, cu, S, moba_chunk_size=256, moba_topk=1)
o_ref = moba_attn_varlen_naive(q, k, v, cu, S, moba_chunk_size=256, moba_topk=1)
print((o_eff - o_ref).abs().max())   # 预期远超 2e-2
```

2. 再跑一个「对照组」：`moba_topk=4`（拉满，S=1024、chunk=256 时共 4 块），此时高效版选中全部 3 个候选块 + 当前块 = 全量；naive 版 `+inf`/阈值链也会选满。预期两者重新对齐。

**需要观察的现象**：topk=1 时 `max diff` 显著大于对齐测试的容忍度（预计达到输出量级的相当比例）；topk=4 时回到 2e-2 量级。

**预期结果**：分叉成立；对照组对齐。**待本地验证**（需 GPU）。

**无 GPU 替代方案**：纯推演——在纸上分别画出 topk=1 时两版的块选择掩码：naive 是「仅对角块」，efficient 等价于「全部块」（fallback），结论相同。

#### 4.3.5 小练习与答案

**练习 1**：「全量注意力是 MoBA 的特例」在代码上如何体现？请给出一条从 MoBA 调用到全量注意力的具体参数路径。

**答案**：路径一（数学特例）：`topk ≥ 块数` 时 top-k 选中所有因果合法块，self 支路补上当前块，并集恰为全量下三角（支路互不重叠，见 L313-L314 的钳制与 u3-l6 的梯度分解）。路径二（工程捷径）：`topk=1` 直接走 L317-L321 的 fallback 返回全量 flash-attn。前者付 glue 开销、后者免费——二次开发中若需要「训练时偶尔切回全量」，应选后者。

**练习 2**：为什么说 MoBA 的「无缝切换」与 sink/window 注意力的「固定结构」有本质区别？

**答案**：sink/window 是预定义的真子集，无论参数怎么调都回不到全量，结构偏置被硬编码进计算图；MoBA 的选块集合是数据相关的、以全量为上确界，参数从 1（退化分支）到拉满连续可选，且输出接口与全量注意力同构。这正是 README「less structure」（[README.md:L24-L26](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L24-L26)）的具体含义。

**练习 3**：如果把 wrapper 的 decode 分支也换成 MoBA 内核，直接会遇到什么问题？

**答案**：decode 的 `q_len=1`、`kv_len` 很大，两者不等长，而 MoBA 内核的接口假设 `cu_seqlens` 同时描述 Q 与 KV（prefill 场景）；更重要的是 decode 有 KV cache，需要分页注意力配合（第 78 行 TODO 指向的正是这个）。此外每步只有 1 个 query，块级稀疏节省的计算有限，工程收益要先算清。

### 4.4 二次开发切入点：改哪里、怎么验证

#### 4.4.1 概念说明

MoBA 代码量极小（核心四文件），改造切入点按侵入深度分四层：

| 层级 | 改什么 | 触及文件 | 工作量 | 风险 |
| --- | --- | --- | --- | --- |
| 参数级 | chunk_size/topk 取值 | 不改代码，重新 `register_moba` | 分钟级 | 无（但注意 4.2 的负优化区） |
| 策略级 | 块选择规则（加偏置、强制块、换打分） | `moba_naive.py` + `moba_efficient.py` **成对修改** | 小时~天级 | 两版语义失配 |
| 结构级 | 参数化 gate、软路由、辅助损失 | 上述两文件 + `MixedAttention` | 天~周级 | 梯度路径需重新设计 |
| 内核级 | 融合 triton kernel 替代 flash-attn 组合 | 新文件 | 周级 | 数值对齐 + 性能双重验证 |

**关于 docstring 里的 triton**（大纲标注「待确认」，本讲以代码证据落实）：[moba/moba_efficient.py:L279](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L279) 的 docstring 写着 *"efficient version of moba implementation with triton kernels and flash-attn"*，但对整个 `moba/` 目录检索 import，只有 `torch`、`flash_attn`、`einops`、`functools`——**仓库中不存在任何 triton 代码**。当前实现完全由「flash-attn 公有/私有 API + PyTorch 张量操作」组合而成。docstring 是愿景式表述或历史遗留，阅读时以代码为准。这同时指出了最大的高阶改造方向：把 gate 打分、选块、varlen 重组、LSE 合并这段 glue 真正融合成 triton 内核。

**验证闭环**（任何层级都适用）：naive 是黄金参考，所以顺序永远是——

```text
想法 → ① naive 原型（改教学版，语义一目了然）
     → ② 设计对齐规则（两版选中集合必须逐 (query, 块) 相等）
     → ③ efficient 镜像实现
     → ④ 仿 test_moba_attn.py 做双向对齐（前向 + 梯度，三层容忍度）
     → ⑤ 仿 test_moba_speedup.py 测速度变化
```

#### 4.4.2 核心流程

策略级修改的「对齐锚点」——两版实现里语义等价的代码对（改其一必改其二）：

| 语义 | naive 位置 | efficient 位置 |
| --- | --- | --- |
| gate 权重 = 块内 K 均值 | [moba_naive.py:L43-L50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L43-L50) | [moba_efficient.py:L335-L340](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L335-L340) |
| 因果/当前块/跨批掩码 | [moba_naive.py:L58-L61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61) | [moba_efficient.py:L352-L360](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L352-L360) |
| top-k + 精确名单防平局 | [moba_naive.py:L63-L73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L63-L73) | [moba_efficient.py:L365-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L365-L371) |

naive 按 batch 逐条处理、用列写入改 gate；efficient 全批次向量化、用行掩码改 gate——**表达不同，选中集合必须相同**，这是全部对齐工作的判据。

若改造引入新超参（比如综合实践里的 sink 开关），配置的流转路径是：`MoBAConfig` 加字段（[moba/config.py:L4-L7](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py#L4-L7)）→ wrapper 透传（[moba/wrapper.py:L67-L75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L67-L75) 只显式传了现有的两个，需要同步加）→ 两个内核函数签名（[moba_naive.py:L7-L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L7-L15)、[moba_efficient.py:L270-L278](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L270-L278)）。

#### 4.4.3 源码精读：改块选择规则时的三个陷阱

**陷阱一：efficient 版不能用 `±inf` 表达「强制选中」**。选中集合由 `gate_mask = ~gate.isinf()` 与 top-k 名单取交得出：

- [moba/moba_efficient.py:L365-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L365-L371)：`+inf` 会被 `isinf()` 当成「非法」剔除，`-inf` 表示「禁选」。想表达「必选」只能给一个**大有限常数**（如 `1e4`，只要大于任何真实 gate logit 的量级即可），让它赢下 top-k。naive 版反而必须用 `+inf`（其阈值筛选 `gate >= gate_top_k_val` 依赖 `+inf` 恒成立，见 [moba_naive.py:L58-L67](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L67)）。同一语义、两种表达，这是最容易写错的点。

**陷阱二：跨 batch 的禁选掩码必须保留**。efficient 的 `-inf` 同时承担因果（`gate_chunk_end_mask`）与跨批隔离（`gate_batch_end_mask`）：

- [moba/moba_efficient.py:L352-L360](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L352-L360)：任何「强制写分」操作都必须避开已是 `-inf` 的位置，否则会把别的序列的块强塞给当前 query。naive 版按 batch 切片循环，天然无此问题。

**陷阱三：零 expert 与缓存**。改了选块规则，`moba_seqlen_q` 的分布随之变化：

- [moba/moba_efficient.py:L392-L413](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L392-L413)：出现零 expert（无 query 选中的块-头段）时 Q、KV、`cu_seqlen_kv` 三处必须同步裁剪，否则反向 NaN（u3-l4/u3-l6）。
- [moba/moba_efficient.py:L14-L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L14-L15)：`calc_chunks` 带 `lru_cache`，按张量对象身份命中——调试时复用同一 `cu_seqlens` 张量对象会拿到旧元数据，新变体若改动了分块逻辑务必绕开或清空缓存。

#### 4.4.4 代码实践：制定你的修改点对照表

**实践目标**：为「attention-sink 强制块」（综合实践的铺垫）写出两版的修改方案（纸面设计，先不动手）。

**操作步骤**：

1. 复读 4.4.2 的三对锚点代码，确认你能在不看讲义的情况下说出每对的语义。
2. 在纸上回答四个问题：
   - naive 版在哪一行之后插入强制规则，才能既不被因果循环覆盖、又能在 top-k 之前生效？（答：L61 的循环之后、L63 的 topk 之前。）
   - 强制值用 `+inf` 还是有限常数？（答：naive 用 `+inf`，见陷阱一。）
   - 「第一个块」对 naive 的当前 batch 内所有 query 是否永远合法？（答：块 0 起点为 0，对本 batch 任何 query 要么当前要么过去，永远不是未来块，可整列置 `+inf`。）
   - 单块序列（该 batch 只有 1 块）时规则是否自然退化为无操作？（答：是——该块即当前块，本就被必选。）
3. 把答案整理成「文件 → 插入点 → 代码行 → 预期选中集合变化」四列表格。

**需要观察的现象**：设计完成后，你应该能口头推出任意 (batch, chunk, topk) 下新规则与原规则的选中集合差异。

**预期结果**：得到一张可直接照抄实现的设计表——综合实践会逐条检验它。

#### 4.4.5 小练习与答案

**练习 1**：为什么说「先改 naive、后改 efficient」是正确顺序，反过来行不行？

**答案**：naive 实现按 batch 循环、逻辑直白，是验证语义的最快原型，同时它本身就是对齐测试的黄金参考——先把「新规则应该选中哪些块」在 naive 里定型，efficient 的镜像实现才有判据。反过来先改 efficient，一旦对不上无法区分是规则错了还是索引重组错了。

**练习 2**：把强制块从「第一个块」换成「分数最高的块与当前块之外再强制最后一个历史整块」，哪个实现更容易写？为什么？

**答案**：naive 容易得多——它在 batch 内逐块循环，直接对 gate 的最后一列（合法列）置 `+inf` 即可；efficient 版「最后一个历史整块」需要先判断哪些块是历史整块（块终点 ≤ query 位置且非末块），随 query 位置变化，无法用单一行掩码完成，得为每个 query 动态定位列，向量化难度显著上升。这提示了选改造方向的原则：**规则越「与 query 位置无关」，向量化越容易**。

**练习 3**：`register_moba` 的进程级全局配置对新变体的验证有什么影响？

**答案**：配置在注册时绑定，同一进程内不能按请求切换；做变体对比时（如新后端 vs 原版），应像 `tests/test_moba_attn.py` 那样**直接调用内核函数**并显式传参，绕开注册机制，避免全局状态串扰。

## 5. 综合实践：实现 attention-sink 强制块变体并完整验证

这是贯穿本讲所有知识的收官任务：让每个 query **强制额外选中其所在序列的第一个块**（模拟 attention sink），走完「naive 原型 → efficient 镜像 → 对齐测试 → 基准」全流程。

**设计决策（先想清楚再动手）**：采用「强制块占用一个 top-k 名额」的方案——每 query 仍是 `topk` 个块，只是其中一个是固定的块 0。这样总计算量与原版相同，速度对比才公平；「额外加一个名额」（总块数 topk+1）作为延伸实验。

### 步骤一：naive 原型

**不要修改仓库源码**。复制 `moba/moba_naive.py` 为新目录下的 `moba_sink_naive.py`（与 `moba/` 平级即可），在因果修正循环之后、top-k 之前插入一行（示例代码）：

```python
# moba_sink_naive.py —— 在 L61 的 for 循环结束后插入
# attention sink: 强制本序列所有 query 选中第一个块
gate[:, :, 0] = float("inf")
```

为什么安全：naive 按 batch 切片处理，块 0 的起点是该 batch 第 0 个 token，对本 batch 任何 query 要么是当前块（本就 `+inf` 必选）要么是纯过去块，绝无因果冲突；跨 batch 由切片天然隔离。前提：`topk ≥ 2`（topk=1 的语义分叉见 4.3，本实践一律取 topk ≥ 2）。

### 步骤二：efficient 镜像

复制 `moba/moba_efficient.py` 为 `moba_sink_efficient.py`，在 gate 掩码之后（原 L360 `gate.masked_fill_(...)` 后）、top-k 之前（原 L365 前）插入（示例代码）：

```python
# moba_sink_efficient.py —— gate.masked_fill_(gate_inf_mask.unsqueeze(1), -inf) 之后插入
chunk_start = cu_chunk[filtered_chunk_indices]                      # 每个候选块的全局起点
batch_start = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]  # 每个候选块所属序列的起点
is_first_chunk = chunk_start == batch_start                         # 本序列的第一个块
legal = ~torch.isinf(gate)                                          # 只改未被禁选的 (块, 头, query)
gate = torch.where(
    is_first_chunk[:, None, None] & legal,
    torch.full_like(gate, 1e4),                                     # 大有限常数，绝不能用 +inf
    gate,
)
```

逐行核对 4.4.3 的三个陷阱：

1. `1e4` 是有限值，`~isinf()` 会把它当合法候选，且量级远超任何真实内积（head_dim=128 的标准正态内积约在几十量级），必赢 top-k；若误用 `+inf`，`isinf()` 会把强制块判成非法，规则静默失效。
2. `& legal` 保证不覆盖 `-inf`——块 0 对**本序列**当前块内的 query 仍是 `-inf`（那些 query 的当前块就是块 0，归 self 支路），对**其他序列**的 query 也是 `-inf`（跨批隔离）。
3. 单块序列的块不在 `filtered_chunk_indices` 里（它同时是最后一块），`is_first_chunk` 自然不含它，规则退化为无操作——与 naive 版行为一致。

**对齐性自检**：naive 侧强制块占 `k` 个名额之一，efficient 侧占 `moba_topk-1` 个 moba 名额之一，两版每 query 总块数都等于 `topk`（当前块 + 块 0 + topk−2 个自由块）。选中集合逐 (query, 块) 相等，满足 4.4.2 的判据。

### 步骤三：对齐验证

仿照 [tests/test_moba_attn.py:L48-L100](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L48-L100) 写参数化测试（示例代码），直接调用你复制的两个变体函数：

```python
# test_moba_sink.py（示例代码）
@pytest.mark.parametrize("batch", [1, 4])
@pytest.mark.parametrize("head", [2, 4])
@pytest.mark.parametrize("seqlen", [512, 2048])
@pytest.mark.parametrize("moba_chunk_size", [128, 256])
@pytest.mark.parametrize("moba_topk", [2, 3])   # 必须从 2 起，避开 topk=1 分叉
def test_sink_alignment(batch, head, seqlen, moba_chunk_size, moba_topk):
    ...  # 与原测试同构：generate_data → 变体 efficient 前向+反向
         # → 梯度清零 → 变体 naive 前向+反向 → 三层容忍度断言
```

要点（都来自 u4-l2）：两路共用同一份 `vo_grad` 上游梯度；naive 跑之前先 `zero_()` 清梯度、快照先 `clone()`；断言沿用 `allclose(atol=rtol=2e-2)` + `max < 4e-2` + `mean < 4e-4` 三层。

### 步骤四：速度测量

仿照 [tests/test_moba_speedup.py:L44-L80](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L44-L80)，把第 65/72 行的 `moba_attn_varlen` 换成你的变体，跑三组：flash 基线、原版 MoBA、sink 变体。

### 需要观察的现象与预期结果

1. **对齐**：变体两版输出/梯度的 diff 与原版测试同量级（bf16 下 max ~1e-2 量级、mean ~1e-4 量级）。若 max 偏大，优先排查 `1e4` 是否被误写为 `+inf`、`& legal` 是否遗漏。
2. **选块模式**：用 u2-l2 的方法可视化 naive 的 `need_attend`——块 0 那一行（列）应对所有 query 恒亮，阶梯对角带不变。
3. **速度**：设计上强制块只是替换了一个自由名额，(query, 块) 对总数不变，预期 sink 变体与原版 MoBA 速度在噪声范围内持平；延伸实验（topk+1）则按块数比例 \((k+1)/k\) 略慢。
4. 以上运行结果均**待本地验证**（需要 GPU 与 flash-attn==2.6.3 环境）；无 GPU 时完成步骤一、二的代码与纸面推演，并写出预期选中集合，同样是有效完成。

## 6. 本讲小结

- **无参数 gate 的因果链**：gate 分数 = ⟨q, 块内 K 均值⟩，零新增参数；代价是结构不可学习、必须 continue training——行为经由 q/k 投影间接训练，梯度在 top-k 硬路由处断开。
- **稀疏度两旋钮**：每 query 参与 KV ≈ `topk × chunk_size`，理论加速上限 ≈ (S/2)/(k·c)；chunk 控粒度（越小选择越准、glue 越贵），topk 控宽度；乘积触及 S/2 即负优化。
- **无缝切换的三条路径**：框架级换后端字符串、参数级 topk 拉满（数学全量但付 glue 费）、`need_moba_attn=False` 自动退化（免费全量）；prefill 稀疏 + decode 全量的一次生成混用正是该设计的实例。
- **语义分叉警报**：`topk=1` 时 efficient 走 fallback 返回全量注意力，naive 只选当前块——两版本在此不等价，测试网格刻意避开；二次开发的退化路径必须显式对齐。
- **triton 传言落定**：docstring 宣称 triton kernels，但仓库无任何 triton 代码，实现全靠 flash-attn API 组合——融合内核是留给二次开发的明确空间。
- **改造闭环**：naive 原型定语义 → efficient 镜像（`+inf` vs 大有限常数、跨批 `-inf` 保留、零 expert 裁剪三陷阱）→ 三层容忍度对齐测试 → 基准测量。

## 7. 下一步学习建议

本套手册到此完结，三条继续深化的路线：

1. **读完论文与报告**：仓库根目录的 `MoBA_Tech_Report.pdf` 与 [arXiv:2502.13189](https://arxiv.org/abs/2502.13189)——重点看训练配方（continue training 的数据与步数）、混合全量/稀疏的训练策略，以及论文版 MoBA 与本仓库教学版实现上的差异（例如工业版内核与 paged attention）。
2. **完成一个真正的二次开发**：以综合实践为模板，任选方向——参数化 gate + 辅助路由损失、sink 块的 softmax 偏置（而非硬强制）、或按层动态 topk（需要把 `MoBAConfig` 从进程级全局改为可按层注入，触点在 `register_moba` 与 `moba_layer`）。
3. **横向对比同类工作**：读 flash-attn 仓库的 `flash_attn_varlen_func` 与 block-sparse 变体、以及 Slide/NSA/quest 类长上下文稀疏注意力的开源实现，比较它们与本讲总结的取舍谱系（预定义结构 vs 数据驱动路由）各自站在哪里。
