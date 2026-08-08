# PrefixEncoder——前缀编码器

## 1. 本讲目标

本讲是「P-tuning v2 核心机制」单元的第一讲，目标只有一件事：**彻底读懂 `model/prefix_encoder.py` 这 32 行代码**。

读完本讲，你应当能够：

- 说清 PrefixEncoder 在整个 P-tuning v2 流水线里的位置——它负责把「前缀」从一串整数索引变成 Transformer 每一层都能用的 `past_key_values`。
- 区分 `prefix_projection=False`（直接查表）和 `prefix_projection=True`（两层 MLP 投影头）两种实现，并能各自算出参数量。
- 解释输出最后一维 \(2 \times L \times H\)（层数 × 2 × 隐藏维）的来源，并验证它与下游 `get_prompt` 里 `view` 的形状严格一致。

本讲只聚焦「前缀编码器」这一个零件。前缀如何被注入到主干、attention_mask 如何拼接、主干如何被冻结，是下一讲 [u2-l2 前缀注入主流程](u2-l2-prompt-injection-forward.md) 的内容。

## 2. 前置知识

本讲承接 [u1-l3 目录结构与主入口 run.py](u1-l3-entry-and-dispatch.md) 已经建立的认知：命令行经 `get_args()` 解析成四元组 `(model_args, data_args, training_args, qa_args)`，`model/utils.py` 的 `get_model` 根据 `--prefix` 开关决定走 `PREFIX_MODELS` 分支构造前缀模型。本讲要回答的，是这条分支里那个被反复实例化的小组件 `PrefixEncoder` 到底做了什么。

在进入源码前，先澄清三个容易混淆的术语：

| 术语 | 含义 |
|------|------|
| 前缀（prefix） | 一段长度为 `pre_seq_len` 的、**可训练的连续提示**。注意它不是自然语言词，而是浮点向量。 |
| `past_key_values` | Hugging Face Transformer 内部每层注意力使用的「历史 key/value」缓存。P-tuning v2 借用这个接口，把前缀伪装成「已经算好的 key/value」喂给每一层。 |
| 深度注入 | 区别于只在输入嵌入层加提示的浅层 prompt tuning，P-tuning v2 在**每一层**都注入前缀 key/value，所以输出维度才要乘上「层数 × 2」。 |

如果你还不熟悉 `torch.nn.Embedding`，这里给一句话复习：**它本质上是一张可训练的查表**，输入是整数索引，输出是该索引对应的那一行向量。本讲会大量用到这一点。

## 3. 本讲源码地图

本讲只精读一个文件，并引用三处上下游代码来定位它的位置：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| `model/prefix_encoder.py` | **本讲主角**，把前缀索引编码成 `past_key_values`。 | 全文（32 行） |
| `model/sequence_classification.py` | 上游消费者，`get_prompt` 调用 PrefixEncoder 并把输出重排成每层 key/value。 | PrefixEncoder 的实例化与 `get_prompt` |
| `model/utils.py` | 决定是否构造前缀模型、把命令行参数写进 `config`。 | `get_model` 的 prefix 分支 |
| `arguments.py` | 定义 `pre_seq_len`、`prefix_projection`、`prefix_hidden_size` 三个命令行参数。 | 三个字段定义 |

## 4. 核心概念与源码讲解

### 4.1 前缀编码器的定位与 torch.nn.Embedding

#### 4.1.1 概念说明

先回答一个问题：**前缀从哪里来？**

P-tuning v2 的前缀不是输入文本的一部分，而是一组**模型自己学出来的参数**。最自然的实现就是：准备一张形状为 `(pre_seq_len, D)` 的可训练表，前缀就是「把这张表的前 `pre_seq_len` 行全部取出来」。

这正是 `torch.nn.Embedding` 的用法：把 `0, 1, ..., pre_seq_len-1` 这串整数索引送进去，查表后得到 `(pre_seq_len, D)` 的浮点矩阵。要训练的不是输入索引（那只是固定的 `[0,1,...,P-1]`），而是**表本身**。这就是「参数高效」的起点——主干 BERT/RoBERTa 几亿参数全部冻结，只有这一小张表（外加分类头）在更新。

#### 4.1.2 核心流程

`PrefixEncoder` 的整体数据流非常简单：

```text
prefix_tokens : (batch, pre_seq_len)   # 固定索引 [0,1,...,P-1]，按 batch 复制
        │  PrefixEncoder.forward
        ▼
past_key_values : (batch, pre_seq_len, 2*L*H)   # 供下游 get_prompt 重排
```

类文档字符串明确写出了输入输出形状：

[模型/PrefixEncoder 类定义与形状说明 (model/prefix_encoder.py:L4-L11)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L4-L11) ——这段说明：输入是 `(batch-size, prefix-length)`，输出是 `(batch-size, prefix-length, 2*layers*hidden)`。

#### 4.1.3 源码精读

`__init__` 一开始读取 `prefix_projection` 开关；当它为 `False` 时，走「直接查表」分支：

[直接查表分支 (model/prefix_encoder.py:L12-L14,L23-L24)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L12-L24) ——第 14 行读开关；第 24 行直接建一张 `Embedding(pre_seq_len, num_hidden_layers * 2 * hidden_size)`，输出维度一次性铺满 `2*L*H`。

对应的 `forward` 也就一行查表：

[forward 的查表实现 (model/prefix_encoder.py:L30-L31)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L30-L31) ——`past_key_values = self.embedding(prefix)`，把整数索引直接映射成 `(batch, pre_seq_len, 2*L*H)`。

#### 4.1.4 代码实践

下面这段**示例代码**（非项目原有代码）演示如何单独验证「直接查表分支」的参数量。请在仓库根目录运行：

```python
# 示例代码
from types import SimpleNamespace
import torch
from model.prefix_encoder import PrefixEncoder

config = SimpleNamespace(
    pre_seq_len=4,
    num_hidden_layers=12,
    num_attention_heads=12,   # 注意：PrefixEncoder 本身并不使用它
    hidden_size=768,
    prefix_projection=False,
)

enc = PrefixEncoder(config)
n_param = sum(p.numel() for p in enc.parameters())
print("可训练参数量 =", n_param)
print("pre_seq_len * 2 * L * H =", config.pre_seq_len * 2 * config.num_hidden_layers * config.hidden_size)
```

1. 实践目标：确认直接查表分支的参数量恰好等于 `pre_seq_len × 2 × L × H`。
2. 操作步骤：在仓库根目录 `python -c` 或存成脚本运行。
3. 观察现象：两个打印值应当完全相等。
4. 预期结果：`73728`（即 `4 × 2 × 12 × 768`）。
5. 若环境无 torch，则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若把 `pre_seq_len` 从 4 改成 128（这也是 run_script 里常见的取值），直接查表分支的参数量变成多少？
**答**：\(128 \times 2 \times 12 \times 768 = 2{,}359{,}296\)。

**练习 2**：`prefix_tokens` 的内容固定是 `torch.arange(pre_seq_len)`，即 `[0,1,...,P-1]`。为什么不用随机浮点向量作为输入？
**答**：因为 `nn.Embedding` 的输入必须是**整数索引**；真正可训练的是查表本身（表的每一行），输入索引只是「选择取出哪些行」。固定为 `0..P-1` 就能取到全部 P 行，没有任何信息损失。

### 4.2 prefix_projection 投影头：两层 MLP

#### 4.2.1 概念说明

直接查表虽然参数最少，但有一个潜在弱点：**前缀的每一维彼此独立**，表的第 `i` 行和第 `j` 行之间没有任何交互。当任务较难或前缀较长时，研究者发现加一个小的「投影头」让前缀先经过非线性变换，往往更稳、更强。

这就是 `prefix_projection=True` 的作用：先用一张小表把索引查成 `hidden_size` 维的「词向量」，再用两层 MLP（Linear → Tanh → Linear）把它投影到 `2*L*H` 维。这本质上是 **Prefix Tuning** 论文里的重参数化（reparameterization）技巧。

#### 4.2.2 核心流程

`prefix_projection=True` 时的数据流多了一步：

```text
prefix : (batch, pre_seq_len)
   │  embedding  (pre_seq_len, hidden_size)
   ▼
prefix_tokens : (batch, pre_seq_len, hidden_size)
   │  Linear(hidden → prefix_hidden_size) → Tanh
   │  Linear(prefix_hidden_size → 2*L*H)
   ▼
past_key_values : (batch, pre_seq_len, 2*L*H)   # 输出形状与直接查表完全一致
```

注意两种分支的**输出形状完全相同**，区别只在内部参数量和表达能力。

#### 4.2.3 源码精读

`prefix_projection=True` 分支多建了 `self.trans` 这个 `Sequential`：

[MLP 投影头分支 (model/prefix_encoder.py:L15-L22)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L15-L22) ——第 17 行先把索引查成 `hidden_size` 维；第 18-22 行是 `Linear(hidden, prefix_hidden_size) → Tanh → Linear(prefix_hidden_size, num_hidden_layers*2*hidden_size)`，最终投影到 `2*L*H`。

对应的 `forward` 也多了一步 `self.trans`：

[forward 的 MLP 实现 (model/prefix_encoder.py:L27-L29)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L27-L29) ——先 `embedding` 再 `trans`。

三个配置字段都来自命令行（默认值见 `arguments.py`）：`--pre_seq_len` 默认 4、`--prefix_projection` 默认 `False`、`--prefix_hidden_size` 默认 512：

[三个前缀相关参数 (arguments.py:L137-L154)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L137-L154)。它们在 `get_model` 里被写进 `config`，再由 PrefixEncoder 读取：

[get_model 把参数写入 config (model/utils.py:L92-L103)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L103) ——第 95-96 行 `config.pre_seq_len`、`config.prefix_projection`、`config.prefix_hidden_size` 的赋值。

#### 4.2.4 代码实践

把上一节的**示例代码**里 `prefix_projection` 改成 `True`、并补上 `prefix_hidden_size=512`，再打印参数量：

```python
# 示例代码
config.prefix_projection = True
config.prefix_hidden_size = 512
enc_mlp = PrefixEncoder(config)
for name, p in enc_mlp.named_parameters():
    print(name, tuple(p.shape), p.numel())
print("MLP 版总参数量 =", sum(p.numel() for p in enc_mlp.parameters()))
```

1. 实践目标：观察 MLP 投影头带来的参数量膨胀。
2. 操作步骤：仅修改两个开关字段后重新实例化。
3. 观察现象：`embedding` 只有 3072 个参数，但第二个 `Linear(512→18432)` 高达数百万。
4. 预期结果：总参数量约 \(3072 + 393728 + 9455616 = 9{,}852{,}416\)（约 985 万）。
5. 对比直接查表分支的 73,728，**膨胀了上百倍**——这正是默认关闭它的原因。

> 旁注：你在 `sequence_classification.py` 里看到的 `print('total param is {}'.format(total_param)) # 9860105`，是作者在 large 模型 + 某前缀配置下跑出的参考打印值（`total param = 全部参数 − 冻结的主干参数`，主体就是这个投影头）。不必纠结精确数字，领会「投影头占了可训练参数的绝大部分」即可。

#### 4.2.5 小练习与答案

**练习 1**：`prefix_projection=True` 时把 `prefix_hidden_size` 从 512 调到 1024，参数量主要在哪一层变化？
**答**：主要在第二个 `Linear(prefix_hidden_size → 2*L*H)`，其权重从 `512×18432` 涨到 `1024×18432`（输出维 `2*L*H` 不变）；第一个 `Linear` 也从 `768×512` 涨到 `768×1024`，但增量远小于第二层。

**练习 2**：为什么项目默认 `prefix_projection=False`？
**答**：因为 P-tuning v2 的核心卖点是**参数高效**。MLP 投影头会让可训练参数从几万暴涨到近千万，违背初衷；只有在难任务上需要更强表达力时才打开它，并配合 `search_script` 一起调参。

### 4.3 输出维度 2*layers*hidden 的来源与去向

#### 4.3.1 概念说明

现在回答本讲最关键的问题：**为什么输出最后一维是 `2 × L × H`，而不是 `H`？**

回到 u1-l1 讲过的「深度提示调优」：前缀要注入到**每一层**的注意力里。而每一层注意力需要两个张量——key 和 value。所以：

- 「L」= 注入到 `num_hidden_layers` 层；
- 「2」= 每层需要 key 和 value 各一份；
- 「H」= 每份的隐藏维度是 `hidden_size`。

合起来就是 \(2 \times L \times H\)。这个数字不是随便选的，它精确等于「把所有层的 prefix key/value 拼成一整条向量」所需的长度。

#### 4.3.2 核心流程

PrefixEncoder 吐出 `(batch, pre_seq_len, 2*L*H)` 后，下游 `get_prompt` 要把它**拆回**成「每层一对 key/value」。这里给一条直觉推导（细节留到下一讲）：

设 \(L\) 为层数、\(H\) 为隐藏维、\(n\) 为头数、\(d = H/n\) 为每头维度。注意 `2*L*H` 与 `view` 的后三维满足：

\[
\underbrace{(2L)}_{\text{n\_layer}\times2} \times \underbrace{n}_{\text{n\_head}} \times \underbrace{d}_{\text{n\_embd}=H/n} \;=\; 2L \times n \times \frac{H}{n} \;=\; 2LH
\]

所以 `view(batch, pre_seq_len, n_layer*2, n_head, n_embd)` 不改变总元素数，只是把一整条 `2*L*H` 向量重新解读成「层 × 头 × 头维」的结构。这正是输出维度必须等于 `2*L*H` 的原因——下游要靠这个等式把它拆开。

#### 4.3.3 源码精读

输出维度在两处被「写死」为 `num_hidden_layers * 2 * hidden_size`：

[输出维度写死为 2*L*H (model/prefix_encoder.py:L21,L24)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L15-L24) ——第 21 行（MLP 最后一层输出维）和第 24 行（直接查表的 Embedding 输出维）都是 `config.num_hidden_layers * 2 * config.hidden_size`。

下游 `get_prompt` 的实例化与调用、以及把输出 `view` 成 `(batch, pre_seq_len, n_layer*2, n_head, n_embd)`：

[get_prompt 调用 PrefixEncoder 并重排 (model/sequence_classification.py:L119,L130-L143)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L130-L143) ——第 132 行拿到 `(batch, pre_seq_len, 2*L*H)`；第 134-140 行 `view` 重排；这里就能看到 `n_head = num_attention_heads`、`n_embd = hidden_size // num_attention_heads`（第 115-116 行），它们在 PrefixEncoder 里**没用上**，但在 `get_prompt` 里用来拆头。

#### 4.3.4 代码实践（本讲主任务）

构造规格里指定的 dummy config，实例化 PrefixEncoder 并验证输出最后一维等于 `2*12*768`：

```python
# 示例代码
from types import SimpleNamespace
import torch
from model.prefix_encoder import PrefixEncoder

config = SimpleNamespace(
    pre_seq_len=4,
    num_hidden_layers=12,
    num_attention_heads=12,   # PrefixEncoder 不使用它，这里仅对齐全量 config
    hidden_size=768,
    prefix_projection=False,
)
encoder = PrefixEncoder(config)

# batch=2，输入就是 [0,1,2,3] 复制两份
prefix_tokens = torch.arange(config.pre_seq_len).unsqueeze(0).expand(2, -1)
print("输入 shape :", tuple(prefix_tokens.shape))   # (2, 4)

out = encoder(prefix_tokens)
print("输出 shape :", tuple(out.shape))             # (2, 4, 18432)
print("2*L*H     :", 2 * config.num_hidden_layers * config.hidden_size)  # 18432
```

1. 实践目标：验证输出 shape 为 `(2, 4, 18432)`，且最后一维恰为 `2*12*768`。
2. 操作步骤：在仓库根目录运行（`from model.prefix_encoder import PrefixEncoder` 依赖根目录作为包根）。
3. 观察现象：输入 `(2,4)`、输出 `(2,4,18432)`，两个数字相等。
4. 预期结果：`2*L*H = 2 × 12 × 768 = 18432`，与输出最后一维一致。
5. 若本地无 GPU/torch 也无妨——本任务纯 CPU、不需要预训练权重，能直接跑通；若仍无法运行则标注「待本地验证」。

> 顺带验证一个细节：dummy config 里写了 `num_attention_heads=12`，但 PrefixEncoder 全程没读它。这说明「拆头」不是编码器的职责，而是下游 `get_prompt` 的职责——这是读懂整条流水线分工的关键。

#### 4.3.5 小练习与答案

**练习 1**：输出最后一维 `2*12*768` 里的「2」代表什么？
**答**：每层注意力需要的 **key 和 value** 两个张量。

**练习 2**：若换成 BERT-large（`num_hidden_layers=24`、`hidden_size=1024`），仍取 `pre_seq_len=4`、`prefix_projection=False`，输出 shape 的最后一维是多少？
**答**：\(2 \times 24 \times 1024 = 49{,}152\)，输出 shape 为 `(2, 4, 49152)`，直接查表参数量为 \(4 \times 49152 = 196{,}608\)。

**练习 3**：`get_prompt` 里 `view(batch, pre_seq_len, n_layer*2, n_head, n_embd)` 的后三维乘积是否等于 `2*L*H`？为什么这样设计？
**答**：\( (2L) \times n \times (H/n) = 2LH \)，与最后一维一致，保证 `view` 不改变元素总数。这样设计是为了把编码器吐出的「一整条 `2*L*H` 向量」精确拆成「层 × 头 × 头维」，再经 `permute + split(2)` 还原成 HF 期望的「每层一对 key/value」结构（详见下一讲）。

## 5. 综合实践

把本讲三个模块串起来：**一次性对比两种分支的输出形状与参数量，并用一句话总结参数高效的含义。**

请在仓库根目录运行下面的**示例代码**：

```python
# 示例代码
from types import SimpleNamespace
import torch
from model.prefix_encoder import PrefixEncoder

def build(prefix_projection, prefix_hidden_size=512):
    cfg = SimpleNamespace(
        pre_seq_len=4, num_hidden_layers=12, num_attention_heads=12,
        hidden_size=768, prefix_projection=prefix_projection,
        prefix_hidden_size=prefix_hidden_size,
    )
    enc = PrefixEncoder(cfg)
    x = torch.arange(cfg.pre_seq_len).unsqueeze(0).expand(2, -1)
    out = enc(x)
    params = sum(p.numel() for p in enc.parameters())
    return tuple(out.shape), params

shape_a, param_a = build(False)
shape_b, param_b = build(True)
print(f"直接查表 : shape={shape_a}, 可训练参数={param_a:,}")
print(f"MLP 投影 : shape={shape_b}, 可训练参数={param_b:,}")
print(f"输出形状是否一致: {shape_a == shape_b}")
print(f"参数量倍数      : {param_b // param_a}")
```

任务要求：

1. 运行后记录两组 `(shape, 参数量)`。
2. 解释为什么两种分支的**输出形状相同**（答：都按 `2*L*H` 输出，差异只在内部实现）。
3. 解释为什么参数量相差上百倍，并联系 u1-l1「参数高效微调」的概念，说明 P-tuning v2 默认选 `prefix_projection=False` 的合理性。
4. 预期输出：两条 shape 均为 `(2, 4, 18432)`；直接查表约 7.4 万参数，MLP 投影约 985 万参数，倍数约为 133。

## 6. 本讲小结

- `PrefixEncoder` 是 P-tuning v2 流水线里**唯一负责「造前缀」的零件**：把固定索引 `[0..P-1]` 编码成 `(batch, pre_seq_len, 2*L*H)` 的 `past_key_values`。
- 两种实现：`prefix_projection=False` 直接用一张 `Embedding(pre_seq_len, 2*L*H)` 查表，参数最少（默认）；`prefix_projection=True` 额外加 `Linear→Tanh→Linear` 两层 MLP，表达力更强但参数暴涨上百倍。
- 输出维度 `2*L*H` 不是任意值，而是「层数 L × (key+value 两个) × 隐藏维 H」，正好等于「把所有层的 prefix key/value 拼成一整条向量」的长度。
- 这个维度被下游 `get_prompt` 用 `view(batch, pre_seq_len, n_layer*2, n_head, n_embd)` 重新解读，依赖恒等式 \(2L \times n \times (H/n) = 2LH\)——这正是它能在不改变元素数的前提下被拆回「每层每头 key/value」的原因。
- 注意分工：PrefixEncoder **不负责拆头**（不读 `num_attention_heads`），拆头是下一讲 `get_prompt` 的事。

## 7. 下一步学习建议

你已经能造出形状正确的 `past_key_values`，但它怎么真正进入冻结的 BERT/RoBERTa？下一讲 [u2-l2 前缀注入主流程：get_prompt 与 forward](u2-l2-prompt-injection-forward.md) 会精读 `get_prompt` 里的 `view → permute([2,0,3,1,4]) → split(2)` 重排、`attention_mask` 为什么要在前面拼 `pre_seq_len` 个 1，以及主干 `requires_grad=False` 的事实。

在进入下一讲前，建议你回到本讲的 4.3.4 示例，把 `pre_seq_len` 改成 128、把 `num_hidden_layers` 改成 24，亲手算一遍输出最后一维，确保你对 `2*L*H` 这个公式已经形成肌肉记忆。
