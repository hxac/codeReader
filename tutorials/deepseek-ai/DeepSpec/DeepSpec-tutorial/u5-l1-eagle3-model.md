# Eagle3 草稿模型：TTT 训练与单层草稿头

## 1. 本讲目标

本讲是第 5 单元的第一篇，从 DSpark 切换到仓库收录的第二种草稿模型算法 Eagle3。学完后你应该能够：

1. 解释 Eagle3 的输入特征是如何构造的：把目标模型 5 个 decoder 层的隐状态沿最后一维拼接成 \(5H\) 宽的张量，再经一个无 bias 的线性层 `fc` 投影回 \(H\)（`extract_eagle3_context_feature`）。
2. 说明 `ttt_length` 与 `draft_num_hidden_layers=1` 这对设计约束：单层草稿 + 训练时链式展开 7 步（train-time test），如何用一次前向「模拟推理时的多步提议」。
3. 描述 `compile_friendly_flex_attention` 的编译缓存技巧：模块级单例、`is_torchdynamo_compiling()` 分支、`recompile_limit` 提升，以及 `q_len ≤ 128` 的编译阈值。
4. 对比 Eagle3 与 DSpark 的 `build_draft_config`，说清「为什么 Eagle3 校验 target_layer_ids 必须恰好 5 层而 DSpark 不必」。

本讲只讲**模型侧**；TTT 循环里的损失计算与 trainer 子类留给下一讲（u5-l2）。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（前几讲已建立，这里只做提醒）：

- **草稿模型的输入是目标模型的中间层隐状态**（u1-l1、u2-l4、u2-l5）：训练前会用 `prepare_target_cache.py` 把目标模型若干层输出落盘成 target cache，训练时 `batch["target_hidden_states"]` 就是宽度为 \(K \times H\) 的拼接特征（\(K\) 为抽取层数，\(H\) 为目标模型 hidden_size）。
- **DSpark 草稿模型的结构**（u4-l2）：注意力层是「双源 K/V」——上下文位置的 K/V 来自经 `fc` 投影的目标特征，草稿位的 K/V 来自草稿自身残差流。Eagle3 的做法不同，本讲会对照讲。
- **KV cache 与 `DynamicCache`**：自回归解码时每层的 K/V 缓存，`past_key_values.update(k, v, layer_idx, ...)` 会追加并返回拼接后的 K/V（u6 系列会大量用到，本讲在 TTT 循环中先见一面）。
- **flex_attention 与 BlockMask**（u4-l1）：PyTorch 提供的可编译块稀疏注意力，`create_block_mask(mask_mod, B, H, Q_LEN, KV_LEN, device)` 把一个 Python 布尔函数编译成块稀疏掩码。DSpark 用它实现「上下文 ∪ 块内」掩码；Eagle3 用它实现另一种掩码（见 4.2）。
- **torch.compile 与 dynamo**（u3-l1、u7-l2 会展开）：`torch.compile` 把 Python 函数编译成优化图；同一个函数遇到不同形状会触发**重编译**，超过 `torch._dynamo.config.recompile_limit`（默认 8）后缓存失效、回退 eager 执行。

另外说明术语：**TTT** 在本仓库语境中指 **train-time test**——训练阶段就执行测试（推理）时的链式自回归展开，让草稿模型在训练中「吃自己生成的 token」，而不是只吃真值。这个名字来自 SpecForge 的 Eagle3 实现，源码注释中标明本模块改编自 SpecForge（见 [deepspec/modeling/eagle3/qwen3/modeling.py:205-207](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L205-L207)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/modeling/eagle3/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L1-L205) | Eagle3 的**模型无关层**：5 层校验、上下文特征拼接、flex_attention 编译封装、Eagle3 专用 BlockMask 与位置 id 工具 |
| [deepspec/modeling/eagle3/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L1-L415) | 草稿模型本体 `Qwen3Eagle3Model` 及其注意力层、decoder 层（复用 HF 的 Qwen3 组件） |
| [deepspec/modeling/eagle3/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L1-L44) | `build_draft_config`：从目标 config 派生草稿 config |
| [config/eagle3/eagle3_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L1-L54) | Qwen3-4B 目标的 Eagle3 训练配置（模型超参在 `model` 字典里） |
| [deepspec/modeling/eagle3/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L354-L419) | `compute_eagle3_loss` 中的 TTT 循环——本讲只看它**如何调用模型**，损失细节在 u5-l2 |
| [deepspec/eval/eagle3/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L61-L89) | 评估侧 `_init_context`，用 `extract_eagle3_context_feature` 现场重建特征（u6-l6 展开） |
| [deepspec/modeling/dspark/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L1-L61) | DSpark 版 `build_draft_config`，作对比参照 |

Eagle3 目录组织与 DSpark 完全同构：`common.py`（模型无关）+ `<模型族>/config.py` + `<模型族>/modeling.py`，目前有 qwen3 与 gemma4 两个模型族。

## 4. 核心概念与源码讲解

### 4.1 Eagle3 上下文特征拼接

#### 4.1.1 概念说明

EAGLE 系列算法的核心观察是：**草稿模型最难自己算出来的东西，目标模型其实已经算好了**。目标模型第 \(i\) 层的隐状态浓缩了「读到当前位置为止的语义」，把它直接喂给草稿模型当输入特征，草稿只需学一个相对轻的映射就能逼近目标的下一 token 分布。

Eagle3 在此之上更进一步：不用单一层，而是**同时抽取低层、中层、高层的隐状态拼接起来**。不同深度的层携带不同粒度的信息（低层偏词法/局部，高层偏语义/全局），拼接后让草稿自己通过一个可学习的线性层决定如何混合。配置里 Qwen3-4B（36 层）用的是 `[1, 9, 17, 25, 33]`——均匀铺满整个深度：

```python
model = dict(
    target_model_name_or_path=QWEN_3_4B,
    target_layer_ids=[1, 9, 17, 25, 33],
    ttt_length=7,
    step_loss_decay=0.8,
    draft_num_hidden_layers=1,
)
```

见 [config/eagle3/eagle3_qwen3_4b.py:10-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L10-L16)，这段配置声明了 5 个抽取层、7 步 TTT、0.8 的逐步损失衰减和单层草稿。

与 DSpark 的关键区别（承接 u4-l2）：

| | DSpark | Eagle3 |
| --- | --- | --- |
| 拼接特征 \(KH\) 的用途 | 只为**上下文位置**生成 K/V（经 `fc` 投影）；草稿位 K/V 来自草稿自身残差流（双源） | 整个模型的主输入：先 `fc` 投影 \(5H \to H\)，进入残差流 |
| 每层注意力的输入 | 草稿残差流（\(H\)） | `[LN(token embedding); LN(隐状态)]` 拼接（\(2H\)） |
| 抽取层数 | 任意非空升序集合，可含 `-1`（embedding 哨兵层） | **恰好 5 层**，只能是 decoder 层 |

#### 4.1.2 核心流程

特征构造分两条路径，但产出同一种布局：

1. **训练路径**：target cache 里存的就是 \(K\) 层升序拼接的张量（u2-l4 的五段平铺协议），`batch["target_hidden_states"]` 宽度为 \(5H\)，直接就是特征。
2. **评估路径**：目标模型现场前向，拿到逐层输出列表，用 `extract_eagle3_context_feature` 现场拼出同样的 \(5H\) 张量。

无论哪条路径，进入模型后：

\[ \tilde{h}_i = W_{\mathrm{fc}} \cdot \big[\, h_i^{(l_1)};\, h_i^{(l_2)};\, h_i^{(l_3)};\, h_i^{(l_4)};\, h_i^{(l_5)} \,\big], \qquad W_{\mathrm{fc}} \in \mathbb{R}^{H \times 5H} \]

其中 \([\,;\,]\) 表示沿最后一维拼接。随后每个 decoder 层的注意力输入是：

\[ a_i = \big[\, \mathrm{LN}(e_{t_i});\ \mathrm{LN}(h_i) \,\big] \in \mathbb{R}^{2H} \]

即 token embedding 与隐状态流各过一个 RMSNorm 后拼接——**token 身份**与**语义特征**这两路信息在每一层都重新注入。

#### 4.1.3 源码精读

先看特征拼接函数，它只有一行主体：

```python
def extract_eagle3_context_feature(hidden_states, layer_ids):
    # Eagle3 v1 only consumes target decoder layers. DSpark supports -1 for
    # embeddings, but that is intentionally out of scope here.
    return torch.cat([hidden_states[layer_id + 1] for layer_id in layer_ids], dim=-1)
```

见 [deepspec/modeling/eagle3/common.py:38-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L38-L41)。`+1` 的原因是调用方传入的是 `output_hidden_states` 风格的列表——**索引 0 是 embedding 层的输出，索引 \(l+1\) 才是第 \(l\) 个 decoder 层的输出**。注释同时声明：DSpark 支持的 `-1`（embedding 哨兵层号）在 Eagle3 中刻意不支持。

评估侧的调用点在 `_init_context`，注释一句话讲清了 Eagle3 的配对约定：

```python
# Training pairs target hidden state i with token i + 1, while the
# draft RoPE position stays at i.  Keep the same convention here when
# pre-filling the draft cache from prompt hidden states.
target_hidden = extract_eagle3_context_feature(
    initial_output.hidden_states,
    self.draft_model.target_layer_ids,
)
```

见 [deepspec/eval/eagle3/evaluator.py:69-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L69-L75)：「隐状态 \(i\) 配 token \(i+1\)，RoPE 位置保持 \(i\)」。

再看模型侧的 `fc` 与投影：

```python
self.fc = nn.Linear(
    len(self.target_layer_ids) * config.hidden_size,
    config.hidden_size,
    bias=False,
)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:229-233](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L229-L233)。输入维度由 `len(target_layer_ids)` 动态决定——这就是「结构上支持任意 K 层」的地方；限制恰好 5 层的是校验函数（见综合实践）。

投影动作在 `forward` 里按**宽度自动判别**：

```python
assert hidden_states is not None, "hidden_states must be provided."
if hidden_states.size(-1) == len(self.target_layer_ids) * self.config.hidden_size:
    hidden_states = self.project_hidden_states(hidden_states)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:344-346](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L344-L346)。末维宽度等于 \(5H\) 就投影成 \(H\)；等于 \(H\)（TTT 第 2 步起传入的是草稿自己上一步的输出，见 4.2）则原样使用。因为 Eagle3 固定 \(K=5\)，\(5H \ne H\)，两种情况不会混淆。`project_hidden_states` 本体只是带形状断言的 `self.fc(hidden_states)`，见 [deepspec/modeling/eagle3/qwen3/modeling.py:264-266](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L264-L266)。

最后看 decoder 层里 \(2H\) 拼接的发生处：

```python
residual = hidden_states
hidden_states = self.hidden_norm(hidden_states)
input_embeds = self.input_layernorm(input_embeds)
hidden_states = torch.cat((input_embeds, hidden_states), dim=-1)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:184-187](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L184-L187)。残差流仍是 \(H\) 宽（第 197 行 `residual + hidden_states`），只有送进注意力的张量是 \(2H\)。相应地，注意力层的投影输入维度是 `hidden_size * 2`：

```python
input_dim = int(config.hidden_size) * 2
self.q_proj = nn.Linear(input_dim, config.num_attention_heads * self.head_dim, ...)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:58-63](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L58-L63)（`k_proj`/`v_proj` 同理，见 L64-L73）。q/k 各自还带一个 `Qwen3RMSNorm`（L79-L80），这是从 HF Qwen3 原样复用的组件。

顺带确认冻结接口与 u3-l1 一致：`initialize_embeddings_and_head` 拷贝目标模型的 embedding 与 lm_head 权重并可冻结，见 [deepspec/modeling/eagle3/qwen3/modeling.py:245-258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L245-L258)。

#### 4.1.4 代码实践

**实践目标**：亲手验证拼接顺序、宽度合同与 5 层校验的行为。

1. 操作步骤：把下面的脚本存为独立文件运行（需已 `pip install -r requirements.txt`，CPU 即可）：

```python
# 示例代码：verify_eagle3_feature.py（本讲实践编写，非项目原有文件）
import torch
from deepspec.modeling.eagle3.common import (
    extract_eagle3_context_feature,
    validate_eagle3_target_layer_ids,
)

H, L, T = 64, 6, 3  # 假装 hidden_size=64、目标模型 6 层、序列长 3

# 模拟 output_hidden_states：索引 0 是 embedding 输出，索引 l+1 是第 l 层输出
hs = [torch.randn(1, T, H) for _ in range(L + 1)]

layer_ids = [1, 3, 5]
feat = extract_eagle3_context_feature(hs, layer_ids)
print("feat shape:", tuple(feat.shape))            # (1, 3, 192) == (B, T, 3H)
assert feat.shape == (1, T, len(layer_ids) * H)
# 第 2 段确实来自 layer_ids[1]+1 = 4 号（即第 3 层输出）
assert torch.equal(feat[..., H : 2 * H], hs[layer_ids[1] + 1])

# fc 的形状合同：K*H -> H
fc = torch.nn.Linear(len(layer_ids) * H, H, bias=False)
print("projected:", tuple(fc(feat).shape))         # (1, 3, 64)

# Eagle3 v1 协议校验：必须恰好 5 层
try:
    validate_eagle3_target_layer_ids([1, 3], L)
except AssertionError as e:
    print("caught:", e)
```

2. 需要观察的现象：`feat` 的末维是 `len(layer_ids) * H`；切片断言通过说明拼接严格按 `layer_ids` 升序排列；`validate_eagle3_target_layer_ids([1, 3], 6)` 抛出 `AssertionError`，报错文案含 `Eagle3 v1 expects exactly 5 target layers`。
3. 预期结果：打印 `(1, 3, 192)`、`(1, 3, 64)` 与断言错误信息。具体报错文案以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `target_layer_ids` 从 5 层改成 3 层（并重新生成对应宽度的 target cache），模型中哪些地方的形状或行为会变？

答案：`fc` 的输入维度变为 \(3H\)（[modeling.py:229-233](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L229-L233) 按 `len(target_layer_ids)` 动态构造）；`forward` 中的宽度判别阈值同步变为 \(3H\)。但 `validate_eagle3_target_layer_ids` 会在 `build_draft_config` 入口直接断言失败（`len == 5`），所以改 3 层跑不起来——这是刻意的协议闸门，不是能力缺失。

**练习 2**：为什么 `extract_eagle3_context_feature` 用 `layer_id + 1` 而不是 `layer_id` 做索引？

答案：入参是 `output_hidden_states` 风格的列表，索引 0 存的是 embedding 层输出，第 \(l\) 个 decoder 层的输出在索引 \(l+1\)。若直接用 `layer_id` 会整体错一层，把 embedding 当第 0 层、把第 \(l-1\) 层当第 \(l\) 层。

**练习 3**：DSpark 的 `fc` 与 Eagle3 的 `fc` 都做 \(KH \to H\) 投影，语义上有什么不同？

答案：DSpark 的 `fc` 只服务于**上下文位置**的 K/V 生成（双源注意力的一部分，u4-l2）；Eagle3 的 `fc` 投影结果是**整个残差流的起点**，随后进入所有层、参与 Q/K/V 全部三路投影与残差连接。

### 4.2 单层草稿 + ttt_length：训练即测试

#### 4.2.1 概念说明

Eagle3 草稿主干只有 **1 个 decoder 层**（`draft_num_hidden_layers=1`）。敢用单层的原因正是 4.1 的特征设计：输入已经携带目标模型多层混合后的强特征，「下一个 token 是什么」这一映射剩余的不确定性大大降低，单层变换足以逼近；同时单层意味着推理时提议阶段几乎零开销，这对「草稿必须远快于目标」的投机解码是本质要求。

但单层链式逐 token 提议有一个训练难题：推理时草稿的第 \(k\) 个提议 token 是以**自己前 \(k-1$ 个提议**为条件的，而朴素训练（teacher forcing）只见过真值前缀。两者分布不一致（exposure bias），提议链越长偏得越远。

Eagle3 的解法就是 **TTT（train-time test）**：训练时显式把链式展开走 `ttt_length=7` 步——

- 第 0 步：输入特征是目标模型的 \(5H\) 缓存特征，token 流是真值左移一位（位置 \(i\) 配 token \(i+1\)）；
- 第 \(k\) 步：输入特征换成**草稿自己第 \(k-1\) 步输出的隐状态**，token 流换成**草稿自己第 \(k-1\) 步预测的 token**；
- 每步产出整条序列的下一 token logits，共 7 组监督信号，逐步损失按 `step_loss_decay=0.8` 几何衰减（衰减的细节属 u5-l2）。

这样训练工况与推理工况同构：梯度直接优化「吃自己输出」时的表现。`ttt_length=7` 与 DSpark 的 `block_size=7` 在数量上呼应——两种算法一次都产出 7 个监督/提议位置，但 DSpark 靠一次并行前向 + markov 头补 token 级依赖（u4-l1、u4-l3），Eagle3 靠 7 次串行前向。

#### 4.2.2 核心流程

TTT 循环（调用方在 loss 里，模型提供支撑）的数据流：

```
初始化: past_key_values = DynamicCache()          # 空
        hidden ← target cache 的 5H 特征
        tokens ← input_ids 左移一位（位置 i 放 token i+1）
for k in 0..ttt_length-1:
    output = model(hidden, tokens, position_ids=arange(T),
                   past_key_values, use_cache=True,
                   rope_cache_step_offset=True)
    #  ↑ 本次前向的 K/V 作为第 k 个 chunk 追加进 cache
    hidden  ← output.hidden_states        # 下一步特征 = 本步草稿输出（宽度 H，不再投影）
    tokens  ← 本步草稿自己预测的 token      # 自回归自喂
    监督    ← output.draft_logits vs 目标分布的第 k 个错位切片
```

三个模型侧的关键机制：

1. **KV cache 的 chunk 布局**：每步 `q_len = T`（整条序列），步 \(k\) 开始时 `past_seen_tokens = kT`，前向后 cache 里有 \(k+1\) 个 chunk、`kv_len = (k+1)T`。注意与普通解码不同——这里**每步追加的是一整条序列的 K/V**，不是单个 token。
2. **非平凡注意力掩码**：步 \(k\) 的 query \(i\) 只能看到两类 key——
   - 第 0 个 chunk（真值特征块）中 \(j \le i\) 的位置：因果地看原始上下文；
   - 每个 chunk 中**同一索引 \(i\)** 的位置：沿 TTT 步的「垂直递推」。

   当前 chunk 内 \(j > i\) 的位置不可见——那是「提案深度更靠前的位置」，看了就是未来信息泄露。这个掩码由 `eagle3_mask_mod` 编译成 BlockMask（源码见 4.2.3）。
3. **RoPE 逐步偏移**：`rope_cache_step_offset=True` 时，RoPE 位置整体加 `past_seen_tokens // q_len`，即每步 +1。因为第 \(j\) 个 chunk 的 key 当年是以「位置 \(i + j\)」旋转的，query 以 \(i + k\) 查询，相对距离恰好是 \(k - j\)——精确复刻「链式提议每深入一步，等效生成位置推进一格」。

评估侧对同一套机制的用法（u6-l6 展开）：prefill 时用 `extend_draft_cache` 以错位 token 填充草稿 cache，之后逐 token 链式提议；由于评估时 `q_len = 1`，`past_seen_tokens % q_len == 0` 恒成立，`_prepare_attention_mask` 的定长 chunk 断言自然满足。

#### 4.2.3 源码精读

**模型构造：必填字段与单层主干。**

```python
required_fields = (
    "target_layer_ids",
    "ttt_length",
    "step_loss_decay",
)
for field in required_fields:
    assert hasattr(config, field), f"config.{field} must be provided."
...
self.layers = nn.ModuleList(
    [Qwen3Eagle3DecoderLayer(config, layer_idx)
     for layer_idx in range(config.num_hidden_layers)]
)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:213-239](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L213-L239)。`config.num_hidden_layers` 已被 `build_draft_config` 覆写为 `draft_num_hidden_layers`（见 [deepspec/modeling/eagle3/qwen3/config.py:30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L30)），配置为 1 时就是单层草稿。config 侧的约束是 `draft_num_hidden_layers >= 1`（[config.py:21-25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L21-L25)）——**代码允许大于 1**，「单层」是 Eagle3 v1 配方的取值而非硬约束（评估器则另有 `== 1` 的断言，属 u6-l6 话题）。

**forward：cache 推进与 RoPE 偏移。**

```python
q_len = int(hidden_states.shape[1])
past_seen_tokens = (
    int(past_key_values.get_seq_length())
    if past_key_values is not None
    else 0
)
cache_position = torch.arange(
    past_seen_tokens,
    past_seen_tokens + q_len,
    device=hidden_states.device,
)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:357-367](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L357-L367)。`cache_position` 是写入 DynamicCache 的物理槽位，每步前进一整个 chunk。

```python
rope_position_ids = position_ids
if rope_cache_step_offset:
    assert int(past_seen_tokens) % int(q_len) == 0, (
        "SpecForge-style Eagle3 RoPE offset expects fixed-size TTT chunks: "
        f"past_seen_tokens={past_seen_tokens}, q_len={q_len}"
    )
    rope_position_ids = position_ids + int(past_seen_tokens) // int(q_len)
position_embeddings = self.rotary_emb(hidden_states, rope_position_ids)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:374-381](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L374-L381)。偏移量 = 已完成的 chunk 数 \(k\)，正是「TTT 每步等效多生成一个 token」的量化表达；断言要求定长 chunk（训练时 \(kT \bmod T = 0\) 恒成立）。

**掩码：垂直递推 + 首块因果。**

```python
def eagle3_mask_mod(b, h, q_idx, kv_idx):
    del h
    seq_len = seq_lengths[b]
    in_valid_query = q_idx < seq_len
    causal_mask = (q_idx >= kv_idx) & (kv_idx < seq_len)
    suffix_mask = (
        (kv_idx >= q_len)
        & ((kv_idx % q_len) < seq_len)
        & (((kv_idx - q_idx) % q_len) == 0)
    )
    return in_valid_query & (causal_mask | suffix_mask)
```

见 [deepspec/modeling/eagle3/common.py:116-126](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L116-L126)。逐项拆解：

- `causal_mask`：`kv_idx < seq_len` 限定**第 0 个 chunk**，`q_idx >= kv_idx` 是普通因果——看真值上下文；
- `suffix_mask`：`kv_idx >= q_len` 限定**后续 chunk**。把 `kv_idx = c·q_len + j` 代入 `(kv_idx − q_idx) % q_len == 0`，得 \(j \equiv q_{idx} \pmod{q\_len}\)，而两者都小于 `q_len`，故 **\(j = q_{idx}\)**——只看每个后续 chunk 的同一索引，即垂直递推对角线；
- `in_valid_query` 与 `(kv_idx % q_len) < seq_len` 处理右 padding（本仓库缓存样本无 padding，见 u2-l6，但掩码仍写成鲁棒形式）。

`seq_lengths` 由 padding 掩码求和再减去 `lck`（已完成 chunk 数）得到，见 [common.py:113-114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L113-L114)。`lck` 的来源在模型侧：

```python
if self.config._attn_implementation == "flex_attention":
    assert int(past_seen_tokens) % int(q_len) == 0, ...
    lck = int(past_seen_tokens) // int(q_len)
    return create_eagle3_attention_mask(...)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:285-297](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L285-L297)（`_prepare_attention_mask`，L274-L305）。非 flex_attention 路径退回普通 4D 因果掩码（L298-L305，实现在 [common.py:142-171](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L142-L171)）——注意普通因果掩码**表达不了**「只看后续 chunk 的对角线」，它会放开整个历史，因此那是语义不同的降级路径；训练配置强制 `flex_attention`（[config.py:6 及 L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L6-L38)）。

**调用方：TTT 循环怎么用模型。**

```python
for step_idx in range(int(ttt_length)):
    ...
    output = model(
        hidden_states=hidden_states,
        input_ids=current_input_ids,
        attention_mask=attention_mask,
        position_ids=base_position_ids,
        past_key_values=past_key_values,
        use_cache=True,
        return_logits=True,
        rope_cache_step_offset=True,
    )
    hidden_states = output.hidden_states
```

见 [deepspec/modeling/eagle3/loss.py:402-420](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L402-L420)。注意三点：`position_ids` 每步都是同一条 `arange(T)`（RoPE 偏移由模型内部加）；`hidden_states` 被循环重新赋值为草稿输出（宽度 \(H\)，触发 4.1 的「不投影」分支）；`current_input_ids` 初始为左移一位的真值（[loss.py:371](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L371)，`_shift_with_zero_padding(input_ids, left=False)` 取 `tensor[:, 1:]` 即左移），之后换成草稿自己的预测。模型返回的 `Eagle3ForwardOutput` 携带 `hidden_states / draft_logits / target_logits` 三元组，定义见 [common.py:13-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L13-L17)。

**评估侧的复用**：`extend_draft_cache` 是同一个 forward 的「只追加、只取最后一个 token」封装（返回 `output[:, -1:, :]`），见 [modeling.py:307-322](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L307-L322)；调用点在 [evaluator.py:83-89](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L83-L89)。

#### 4.2.4 代码实践

**实践目标**：不跑大模型，单独验证 Eagle3 掩码的可见性结构——「首块因果 ∪ 各块对角线」。

1. 操作步骤：运行下面的脚本（CPU 即可），先用纯 Python 复算一遍公式，再调用真实函数交叉验证：

```python
# 示例代码：eagle3_mask_grid.py（本讲实践编写，非项目原有文件）
import torch
from deepspec.modeling.eagle3.common import create_eagle3_attention_mask

q_len = 4
attention_mask = torch.ones(1, q_len)  # 无 padding，seq_len = q_len

# --- 手算：直接按公式枚举 ---
seq_len = q_len
lck = 1  # 假设已完成 1 个 chunk，kv_len = 2 * q_len
grid = torch.zeros(q_len, 2 * q_len, dtype=torch.long)
for q in range(q_len):
    for kv in range(2 * q_len):
        causal = (q >= kv) and (kv < seq_len)
        suffix = (kv >= q_len) and ((kv % q_len) < seq_len) and (((kv - q) % q_len) == 0)
        grid[q, kv] = int(q < seq_len and (causal or suffix))
print("手工复算的可见性矩阵：")
print(grid)

# --- 调用真实实现交叉验证 ---
bm = create_eagle3_attention_mask(
    attention_mask=attention_mask, q_len=q_len, kv_len=2 * q_len, lck=lck, device="cpu"
)
dense = bm.to_dense()[0, 0].to(torch.long)   # BlockMask -> 稠密 0/1
print("create_eagle3_attention_mask 的稠密化结果：")
print(dense)
assert torch.equal(grid, dense), "两份结果应逐元素一致"
```

2. 需要观察的现象：两份矩阵应完全一致，且每行 `q` 的可见列为 `{0..q} ∪ {q_len + q}`：

```
行 0: 1 0 0 0 | 1 0 0 0
行 1: 1 1 0 0 | 0 1 0 0
行 2: 1 1 1 0 | 0 0 1 0
行 3: 1 1 1 1 | 0 0 0 1
```

   左半是第 0 块的因果下三角，右半只有对角线——当前块里 `j > i` 的位置（未来提案）一律不可见。
3. 预期结果：断言通过、打印上述矩阵。`BlockMask.to_dense()` 的 API 行为随 torch 版本可能有差异，若该调用报错，可只保留手工复算部分并注释说明（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`ttt_length=7`、序列长 `T` 时，TTT 循环结束后草稿 KV cache 里有几个 chunk？`kv_len` 是多少？

答案：7 个 chunk——循环体执行 7 次，每次 forward 追加一整条序列（`q_len = T`）的 K/V；最终 `kv_len = 7T`。步 \(k\) 开始时 `past_seen_tokens = kT`、`lck = k`。

**练习 2**：步 \(k\) 的 query \(i\) 能否看到**本步刚写入**的 chunk 中索引 \(j > i\) 的 key？为什么这样设计？

答案：不能。`suffix_mask` 中 `(kv_idx − q_idx) % q_len == 0` 限定当前 chunk 只暴露 \(j = i\) 的对角线。同一 chunk 内索引 \(j\) 的表征提案深度对应序列上更靠后的位置，若 query \(i\) 可见，就等于让位置 \(i\) 的第 \(k\) 步提案偷看「别处更靠前的未来」，破坏自回归性质（训练时会造成标签泄露）。

**练习 3**：`rope_cache_step_offset` 的偏移量为什么是 `past_seen_tokens // q_len`（每步 +1），而不是 `past_seen_tokens`（每步 +T）？

答案：TTT 的每一步对应链式提议**深入一个 token**，等效生成位置只推进 1；chunk 是训练时为并行处理整条序列引入的批维度概念，不是时间概念。若每步 +T，query 对历史 chunk 对角线的相对 RoPE 距离会变成 \((k-j)T\)，与推理时「逐 token、距离逐个增长」的几何完全不符。

### 4.3 flex_attention 编译包装

#### 4.3.1 概念说明

flex_attention 只有经过 `torch.compile` 才能发挥块稀疏内核的威力，但 Eagle3 的使用姿势给编译带来了两个难题：

1. **形状多变**。TTT 各步 `kv_len = (k+1)T` 不同，batch 内序列长度也不同，BlockMask 形状随之变化，dynamo 会频繁重编译。默认 `recompile_limit = 8` 很快击穿，缓存被清空后静默回退 eager，性能悄悄消失。
2. **嵌套编译**。若外层再用 `torch.compile` 包整个模型（BaseTrainer 的 `torch_compile` 开关），外层图追踪到内部又想调用「已编译的 flex_attention」时，会造成双重编译/图中断。

`common.py` 的四个函数组成一套针对性方案：**模块级单例缓存编译产物 + 追踪态判断 + 重编译上限提升**。同时训练配置里 `torch_compile=False`（[config/eagle3/eagle3_qwen3_4b.py:30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L30)，对比 DSpark 同目标配置的 `torch_compile=True`，[config/dspark/dspark_qwen3_4b.py:44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L44)）——从工程取舍上看，Eagle3 的 TTT 循环每步形状都变，整体编译极易抖动，不如只编译 flex_attention 这一个热点内核。

#### 4.3.2 核心流程

```
configure_eagle3_flex_compile()          # recompile_limit: max(原值, 64)
get_compiled_flex_attention()            # 首次: torch.compile(flex_attention) 并存入模块级全局变量
                                         # 之后: 直接返回缓存的单例
compile_friendly_flex_attention(q,k,v):
    if 正在被 dynamo 追踪:  调用原生 flex_attention     # 交给外层图
    else:                   调用编译版单例              # 独立运行时
```

`create_block_mask` 侧有一对完全对称的 `get_compiled_create_block_mask / compile_friendly_create_block_mask`。此外模型里还有一道**长度阈值**：`q_len ≤ 128` 时干脆用未编译的 `flex_attention`——短序列核本身极快，编译的固定开销与潜在重编译得不偿失；训练时 `q_len = T`（可达 4096）走编译版，评估时提议 `q_len` 很小走原生版。

#### 4.3.3 源码精读

**重编译上限与单例缓存：**

```python
def configure_eagle3_flex_compile():
    if dynamo.config.recompile_limit < 64:
        dynamo.config.recompile_limit = 64

_COMPILED_FLEX_ATTENTION = None
_COMPILED_CREATE_BLOCK_MASK = None

@torch.compiler.disable(recursive=False)
def get_compiled_flex_attention():
    global _COMPILED_FLEX_ATTENTION
    if _COMPILED_FLEX_ATTENTION is None:
        configure_eagle3_flex_compile()
        _COMPILED_FLEX_ATTENTION = torch.compile(flex_attention)
    return _COMPILED_FLEX_ATTENTION
```

见 [deepspec/modeling/eagle3/common.py:44-61](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L44-L61)。三个细节：只升不降（`< 64` 才改，尊重用户已设的更大值）；懒初始化——首次调用才编译，编译产物存模块级全局变量，进程内只编译一次；`@torch.compiler.disable(recursive=False)` 让 dynamo 不要追踪这个 getter 本身（缓存逻辑不是计算图的一部分），但**不**禁用其内部调用的 `torch.compile(flex_attention)`。

**追踪态分支：**

```python
def compile_friendly_flex_attention(query, key, value, **kwargs):
    flex_attention_func = (
        flex_attention if is_torchdynamo_compiling() else get_compiled_flex_attention()
    )
    return flex_attention_func(query, key, value, **kwargs)
```

见 [common.py:64-73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L64-L73)。`is_torchdynamo_compiling()`（来自 transformers.utils）为真说明正处于某个外层编译的图追踪中，此时直接调用原生 `flex_attention`，让它作为外层图的一部分被统一编译；否则独立运行，用编译版单例。这一分支就是「嵌套编译」问题的解法。

**block mask 侧的对称实现**：`get_compiled_create_block_mask` 编译 `create_block_mask`，`compile_friendly_create_block_mask` 做同样的追踪态分支，见 [common.py:76-100](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L76-L100)。源码注释标明这套封装改编自 SpecForge 的 `flex_attention.py`。

**掩码构建处的两道阈值：**

```python
create_block_mask_func = (
    create_block_mask if int(q_len) <= 128 else compile_friendly_create_block_mask
)
```

见 [common.py:129-131](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L129-L131)。注意力内核侧还有同样的一道：

```python
flex_attention_func = (
    flex_attention
    if int(q_len) <= 128
    else compile_friendly_flex_attention
)
attn_output = flex_attention_func(
    query=q, key=k.contiguous(), value=v.contiguous(),
    block_mask=attention_mask,
    enable_gqa=True,
)
```

见 [deepspec/modeling/eagle3/qwen3/modeling.py:113-124](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L113-L124)。`enable_gqa=True` 处理 Qwen3 的 8 组 KV 头对 32 个 query 头（GQA）；`k.contiguous()`/`v.contiguous()` 保证 flex 内核的内存布局要求。另外 `mask_mod` 的 `__name__` 被改成含 `q_len/kv_len/lck` 的字符串（[common.py:128](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L128)），让不同形状的 BlockMask 在 dynamo 缓存里占据不同条目、互不驱逐。

#### 4.3.4 代码实践

**实践目标**：验证「配置生效 + 单例缓存」两件小事，并（可选）量化编译收益。

1. 操作步骤：运行下面的脚本（CPU 可跑前三项；计时项需要 GPU）：

```python
# 示例代码：flex_compile_probe.py（本讲实践编写，非项目原有文件）
import torch
import torch._dynamo as dynamo
from deepspec.modeling.eagle3.common import (
    configure_eagle3_flex_compile,
    get_compiled_flex_attention,
    get_compiled_create_block_mask,
)

print("recompile_limit before:", dynamo.config.recompile_limit)
configure_eagle3_flex_compile()
print("recompile_limit after :", dynamo.config.recompile_limit)

f1 = get_compiled_flex_attention()
f2 = get_compiled_flex_attention()
print("flex 单例缓存生效:", f1 is f2)
print("block_mask 单例缓存生效:",
      get_compiled_create_block_mask() is get_compiled_create_block_mask())
```

2. 需要观察的现象：第一次打印是 torch 默认值（常见为 8，随版本可能不同）；第二次为 64；两个「单例缓存生效」均为 `True`。
3. 预期结果：如上（默认值一项待本地验证）。
4. 可选进阶（需 GPU，待本地验证）：构造 `q_len=1024` 的 query/key/value 与一个 BlockMask，分别用原生 `flex_attention` 与 `get_compiled_flex_attention()` 各跑若干次（`torch.cuda.synchronize()` 后计时），对比预热后的单次耗时，观察编译版是否明显更快。

#### 4.3.5 小练习与答案

**练习 1**：`q_len ≤ 128` 时为什么直接用未编译的 `flex_attention`？

答案：短 query 的核执行时间本来就很短，而 `torch.compile` 有编译触发与潜在重编译的固定成本，收益覆盖不了开销。阈值设在 128 是经验值，让训练（长序列）吃编译收益、评估提议（短序列）避开编译成本。

**练习 2**：`compile_friendly_flex_attention` 里 `is_torchdynamo_compiling()` 的分支解决什么问题？

答案：防止嵌套编译。若外层已用 `torch.compile` 追踪模型，内部再调用「编译版 flex_attention」会造成图中图或追踪中断；该分支在追踪态改调原生 `flex_attention`，让它自然地成为外层图的一部分；独立运行时才取编译单例。

**练习 3**：如果不调用 `configure_eagle3_flex_compile`，TTT 训练中最可能观察到什么现象？

答案：TTT 各步 `kv_len` 不同导致 flex 内核/BlockMask 形状持续变化，dynamo 重编译次数很快超过默认上限 8，编译缓存被清空并回退 eager 执行——不报错，但块稀疏内核的性能优势悄悄丢失，表现为 step 耗时明显高于预期。

## 5. 综合实践

本讲综合实践就是规格指定的任务：**对比 eagle3 与 dspark 的 qwen3 `config.py`，列出派生字段差异，并回答「为什么 Eagle3 校验 target_layer_ids 必须恰好 5 层而 DSpark 不必」**。

### 步骤 1：制作差异表

对照阅读 [deepspec/modeling/eagle3/qwen3/config.py:9-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L9-L39) 与 [deepspec/modeling/dspark/qwen3/config.py:9-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L9-L56)，参考答案如下（建议先自己列一遍再核对）：

| 方面 | DSpark `build_draft_config` | Eagle3 `build_draft_config` |
| --- | --- | --- |
| 层号校验 | `validate_target_layer_ids`：非空、升序、每层在 `{-1} ∪ [0, L-1]`（`-1` 表 embedding，须最前），**层数任意**（[dspark/common.py:59-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75)） | `validate_eagle3_target_layer_ids`：**恰好 5 层**、升序、每层在 `[0, L-1]`（不接受 `-1`）（[eagle3/common.py:20-35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L20-L35)） |
| 输入超参 | `num_draft_layers`、`block_size`、`mask_token_id`、`num_anchors`、`markov_rank`、`markov_head_type`、`confidence_head_alpha`、`confidence_head_with_markov` | `ttt_length`（≥1）、`step_loss_decay`（>0）、`draft_num_hidden_layers`（≥1） |
| `architectures`（评估分发键，u1-l3） | `["Qwen3DSparkModel"]` | `["Qwen3Eagle3Model"]` |
| `num_hidden_layers` 覆写为 | `num_draft_layers`（默认配置 5） | `draft_num_hidden_layers`（默认配置 1） |
| Eagle3 独有写入 | — | `target_model_name_or_path`、`ttt_length`、`step_loss_decay`、`draft_num_hidden_layers` |
| DSpark 独有写入 | `block_size`、`mask_token_id`、`num_anchors`、markov/confidence 系列开关 | — |
| 相同部分 | 都是 `copy.deepcopy(target_config)` 后覆写；都写 `num_target_layers`、`layer_types=["full_attention"]*层数`、`tie_word_embeddings=False`、`_attn_implementation="flex_attention"`、`target_layer_ids` | 同左 |

另可对比两份训练配置 [config/eagle3/eagle3_qwen3_4b.py:18-31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L18-L31) 与 [config/dspark/dspark_qwen3_4b.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45)：lr、warmup、batch、epochs 完全一致，唯二差异是 `trainer_cls` 与 `torch_compile`（DSpark True / Eagle3 False，原因见 4.3.1）。

### 步骤 2：回答「为什么恰好 5 层」

参考答案分三层：

1. **结构上并非必须**：`fc` 的输入维度按 `len(target_layer_ids)` 动态构造（[modeling.py:229-233](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L229-L233)），3 层、7 层都能搭出合法模型。断言是**协议闸门**而非能力边界——错误信息本身就写着 `Eagle3 v1 expects exactly 5 target layers`（[common.py:22-25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L22-L25)）。
2. **上下游有隐式合同**：target cache 的 hidden 段宽度按层数 \(K\) 落盘（u2-l4 的 6L+2LH(K+1) 字节协议），训练前 `validate_train_cache` 会核对（u3-l1）；评估侧 `extract_eagle3_context_feature` 又用同一份列表从活体目标模型重建特征（[evaluator.py:72-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L72-L75)）。把 K 钉死为 5，等于把「配置 → 缓存协议 → 模型 → 评估」四处的一致性提前到 `build_draft_config` 入口一次性锁定，层数不匹配立刻失败而不是训练中途爆形状。
3. **算法谱系不同**：5 层低中高混采是 EAGLE-3 论文的 v1 配方（本实现改编自 SpecForge，注释多处标明），DeepSpec 选择原样冻结该协议；DSpark 则把抽取层数当作自由超参（它还额外支持 `-1` embedding 层），由调参者决定。

## 6. 本讲小结

- Eagle3 的输入特征是目标模型 **5 个 decoder 层隐状态的拼接**（\(5H\)），由 `extract_eagle3_context_feature` 在评估侧现场构造、由 target cache 在训练侧直接提供，再经无 bias 的 `fc` 投影回 \(H\)；`forward` 按末维宽度自动判别是否需要投影。
- 每个 decoder 层的注意力输入是 `[LN(token embedding); LN(隐状态)]` 的 \(2H\) 拼接——与 DSpark 的双源 K/V 是两种不同的特征融合姿势。
- **单层草稿 + ttt_length=7**：TTT 循环把推理时的链式自回归提议搬进训练，第 \(k\) 步吃自己第 \(k-1\) 步的隐状态与预测 token，KV cache 每步追加一整个 chunk。
- Eagle3 注意力掩码 = **第 0 块因果 ∪ 各块同索引对角线**，RoPE 位置每步 +1（`rope_cache_step_offset`），二者共同复刻「垂直递推 + 等效生成位置逐步推进」的链式几何。
- `compile_friendly_flex_attention` 用**模块级单例 + `is_torchdynamo_compiling()` 分支 + recompile_limit 提到 64** 解决形状多变与嵌套编译两大难题；`q_len ≤ 128` 走未编译路径以避开编译固定开销。

## 7. 下一步学习建议

本讲只讲了 Eagle3 的**模型侧**。下一讲 u5-l2（`u5-l2-eagle3-loss-and-trainer.md`）接着精读 `compute_eagle3_loss`：TTT 各步的软交叉熵（Triton 融合的 `FusedLogSoftmaxLoss`）、`step_loss_decay=0.8` 的逐步衰减、精确的「隐状态 \(i\) 配 token \(i+1\)」切片对齐，以及 `Qwen3Eagle3Trainer` 如何复用 BaseTrainer 只重写 `build_models` 与 `run_batch`。读完 u5-l2 后，建议再预先浏览 [deepspec/eval/eagle3/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L61-L89) 的 `_init_context`，体会本讲的 `extend_draft_cache` 与错位配对约定在推理侧如何落地（u6-l6 正式展开）。
