# MiniMindConfig 与模型整体骨架

## 1. 本讲目标

前两个单元你跑通了推理、看清了分词器和数据集——但每次我们都在说「模型」「模型」，却一直没打开过模型这个黑盒。从本讲开始，我们正式进入第 3 单元：**模型结构**。

本讲是第 3 单元的第一篇，只解决一个问题：**MiniMind 的模型是怎么用一个配置对象「拼」出来的**。我们不展开任何一层内部细节（注意力、RoPE、SwiGLU、MoE 都在后面几讲），只看「骨架」。

学完本讲你应该能够：

1. 读懂 `MiniMindConfig` 的每一个字段，理解它为什么要和 `Qwen3` 生态对齐。
2. 画出 `MiniMindModel` 的数据流：`embed_tokens → layers → norm → (lm_head)`，并解释什么是 **tie_word_embeddings（权重共享）**。
3. 理解 `MiniMindForCausalLM` 如何把「模型躯干」和「语言模型头」组装到一起，并交代清楚它和 `MiniMindModel` 的分工。
4. 给定 `hidden_size` 和 `num_hidden_layers`，自己估算模型参数量，并用 `get_model_params` 验证。

本讲**不**讲：注意力的 q/k/v 投影细节（u3-l2）、RoPE 数学（u3-l3）、SwiGLU 与 MoE 路由（u3-l4）、前向损失计算（u3-l5）、generate 采样（u3-l6）。

---

## 2. 前置知识

如果下面几个词你还陌生，先花两分钟看完。

- **Decoder-Only Transformer**：当前主流大模型（GPT、LLaMA、Qwen）的结构。它一层一层堆叠，每层都做两件事——**自注意力**（让每个 token 看前面的 token）和**前馈网络**（对每个 token 单独做一次变换）。MiniMind 也是这个结构。
- **超参数（hyperparameter）**：训练前由人决定的「设置」，比如网络有多宽、有几层。它和「参数」不同——参数是训练过程学出来的数字，超参数是你开训练前就定死的。
- **Config（配置对象）**：把所有超参数打包在一起的一个 Python 对象。PyTorch / HuggingFace 的习惯是：先用 Config 描述「我要一个什么样的模型」，再把 Config 传给模型类去实例化。MiniMind 严格遵循这个习惯。
- **词表（vocab）与 embedding**：u2-l1 讲过，MiniMind 词表大小是 `6400`。模型第一步会把每个 token id 昢成一个向量，这个「id → 向量」的查找表叫 embedding 层，形状是 `[6400, hidden_size]`。
- **参数共享（tie）**：让两个本该各自独立的权重矩阵「共用同一份数字」。本讲会看到：输入端的 embedding 表和输出端的 lm_head 可以共享，从而省掉一大块参数。
- **接 u1-l3 / u2-l1**：你已经见过 `MiniMindForCausalLM` 这个名字（`eval_llm.py` 里加载的就是它），也知道词表 `6400`、`<|im_start|>`/`<|im_end|>` 这些标记。本讲把它们在「模型结构」这一层串起来。

一句话：本讲假设你已经知道「Transformer 是一层层堆出来的」「模型把 token id 变成向量再一层层算」。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | 模型本体定义，全文件就是「配置 + 一堆层 + CausalLM 外壳」 | `MiniMindConfig`（L10-L45）、`MiniMindBlock`（L178-L194）、`MiniMindModel`（L196-L232）、`MiniMindForCausalLM.__init__`（L234-L243） |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | 训练公共工具 | `get_model_params`（L18-L28），用来打印参数量 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 项目说明 | 「📌 模型 / 结构」与「模型配置」两节，里面有官方参数表 |

> 提示：`model_minimind.py` 是整个项目最长、最核心的文件。本讲只精读其中「骨架」部分（Config + Model + CausalLM 外壳），中间夹着的 `RMSNorm`/`Attention`/`FeedForward` 等细节，会在 u3-l2 ~ u3-l4 逐个拆开。

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**MiniMindConfig**（模型的「出厂参数表」）、**MiniMindModel**（模型的「躯干」）、**MiniMindForCausalLM**（「躯干 + 语言模型头」的完整外壳）。我们一个一个拆。

### 4.1 MiniMindConfig：模型的「出厂参数表」

#### 4.1.1 概念说明

想象你在车厂订一辆车，销售会递给你一张配置单：发动机排量、座位数、是否四驱……你勾选完，工厂照单生产。`MiniMindConfig` 就是 MiniMind 的这张配置单。

它解决的问题是：**一个模型类（比如 `MiniMindForCausalLM`）应该能造出「宽模型」「窄模型」「带 MoE 的模型」等不同形态，而不是把尺寸写死在代码里**。做法就是把所有可调尺寸抽出来，塞进一个 Config 对象，实例化时传进去。

MiniMind 的 Config 有两个工程取向，理解这两点就抓住了它的灵魂：

1. **对齐 Qwen3 生态**。字段命名、默认值（`RMSNorm`、`SwiGLU`、`RoPE`、`GQA`、`rope_theta=1e6`、`max_position_embeddings=32768`）都向 Qwen3 看齐。代价是没有独创结构，好处是训练出来的权重能直接转成 `transformers` 格式、再喂给 `llama.cpp / ollama / vllm` 等推理框架（见 u8-l1）。
2. **为「百 M 级小模型」做取舍**。最典型的就是词表只取 `6400`（u2-l1 讲过，压缩 embedding 占比）和主线选 `dim=768, n_layers=8`（约 64M）。

> 名词解释：**GQA（Grouped-Query Attention）** 指「查询头多、KV 头少」。MiniMind 是 `q_heads=8 / kv_heads=4`，即 8 个查询头共用 4 组 KV，能在几乎不掉点的前提下省显存、加速。它的内部广播逻辑（`repeat_kv`）在 u3-l2 讲。

#### 4.1.2 核心流程

`MiniMindConfig` 的实例化流程可以概括为三步：

1. **接三个「位置参数」**：`hidden_size`（模型宽度）、`num_hidden_layers`（层数）、`use_moe`（是否用混合专家前馈层）。这是最常被改动的三个旋钮。
2. **从 `kwargs` 兜底读取其余字段**：用 `kwargs.get(key, default)` 给每个字段一个默认值——你不传就用默认，传了就覆盖。
3. **派生量就地算出来**：比如 `head_dim` 默认是 `hidden_size // num_attention_heads`；`intermediate_size`（前馈层宽度）默认用一个和 `π` 有关的公式向上取整到 64 的倍数。

伪代码：

```text
MiniMindConfig(hidden_size, num_hidden_layers, use_moe, **kwargs):
    调用父类 PretrainedConfig.__init__(**kwargs)   # 继承 HuggingFace 配置基类
    self.hidden_size        = hidden_size            # 模型宽度 d
    self.num_hidden_layers  = num_hidden_layers      # 层数 L
    self.use_moe            = use_moe                # 是否 MoE
    其余字段 = kwargs.get(字段名, 默认值)             # 一人一个默认值
    head_dim          = hidden_size // num_attention_heads   # 派生
    intermediate_size = ceil(hidden_size * π / 64) * 64       # 派生，对齐到 64
```

#### 4.1.3 源码精读

先看类的骨架与三个位置参数——它继承自 HuggingFace 的 `PretrainedConfig`，并声明 `model_type = "minimind"`（这串字符串让 `transformers` 能通过 `AutoConfig` 反序列化识别它）：

> [model/model_minimind.py:10-16](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L10-L16) —— `MiniMindConfig` 的类定义与三个位置参数：`hidden_size=768`、`num_hidden_layers=8`、`use_moe=False`，并调用父类完成 HuggingFace 配置体系的注册。

```python
class MiniMindConfig(PretrainedConfig):
    model_type = "minimind"
    def __init__(self, hidden_size=768, num_hidden_layers=8, use_moe=False, **kwargs):
        super().__init__(**kwargs)
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.use_moe = use_moe
```

接着是一组「通用超参数」，每个都给了默认值——注意力头数、KV 头数、词表大小、最大序列长度、RoPE 基频、是否权重共享等：

> [model/model_minimind.py:17-31](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L17-L31) —— 通用超参数，全部走 `kwargs.get(..., 默认值)` 模式。重点字段：`vocab_size=6400`、`num_attention_heads=8`、`num_key_value_heads=4`、`max_position_embeddings=32768`、`rope_theta=1e6`、`tie_word_embeddings=True`。

这里有三个**派生量**值得专门点出来，因为它们决定了后续每一层的矩阵形状：

> [model/model_minimind.py:23-26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L23-L26) —— 派生量：`head_dim`（每个头的维度）、`intermediate_size`（前馈层中间维度）。`intermediate_size` 用 `math.ceil(hidden_size * π / 64) * 64` 算出，并向上对齐到 64 的倍数（对齐到 64 是为了张量核心高效）。

```python
self.head_dim = kwargs.get("head_dim", self.hidden_size // self.num_attention_heads)
self.intermediate_size = kwargs.get("intermediate_size", math.ceil(hidden_size * math.pi / 64) * 64)
```

对默认 `hidden_size=768` 代入算一下：

- `head_dim = 768 // 8 = 96`
- `intermediate_size = ceil(768 × π / 64) × 64 = ceil(37.70) × 64 = 38 × 64 = 2432`

这两个数后面会反复出现在 `Attention` 和 `FeedForward` 的线性层里。

再往下是 RoPE 外推（YaRN）配置——它只在 `inference_rope_scaling=True` 时生效，用于推理时把 2048 长度的训练位置编码外推到 4 倍长度，数学细节留到 u3-l3：

> [model/model_minimind.py:31-39](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L31-L39) —— `inference_rope_scaling` 开关与 YaRN 字典（`factor=16`、`original_max_position_embeddings=2048`）。关闭时为 `None`。

最后是一组 **MoE 专属配置**，仅在 `use_moe=True` 时才会被用到（`MiniMindBlock` 里据此切换前馈层类型，见 4.2.3）：

> [model/model_minimind.py:40-45](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L40-L45) —— MoE 专属字段：`num_experts=4`（专家数）、`num_experts_per_tok=1`（每个 token 选几个专家，即 top-1）、`router_aux_loss_coef=5e-4`（负载均衡损失系数）。这套「4 experts / top-1」就是 README 里 `minimind-3-moe 198M-A64M` 的来源。

把上面的字段和 README 的官方表对一下，你会发现完全对得上：

| 字段 | minimind-3 (Dense) | minimind-3-moe |
|------|--------------------|----------------|
| `d_model`（hidden_size） | 768 | 768 |
| `n_layers` | 8 | 8 |
| `q_heads` / `kv_heads` | 8 / 4 | 8 / 4 |
| `len_vocab` | 6400 | 6400 |
| `max_pos` | 32768 | 32768 |
| `rope_theta` | 1e6 | 1e6 |
| `use_moe` / experts | False | True / 4 experts / top-1 |
| 参数量 | 64M | 198M-A64M |

> 数据来源：[README.md:575-578](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L575-L578) 官方参数表。`198M-A64M` 的意思是「总参数 198M、每次前向只激活 64M」——这正是 MoE 用更多专家换更高容量、同时保持低激活参数的特点。

#### 4.1.4 代码实践

**实践目标**：亲手实例化两份不同的 `MiniMindConfig`，观察默认值与覆盖值的差异，并搞清楚 `head_dim` / `intermediate_size` 这两个派生量怎么随 `hidden_size` 变化。

**操作步骤**：

1. 在仓库根目录（能 `import model` 的位置）新建一个临时脚本 `tmp_config.py`（**示例代码，不放进仓库**）：

   ```python
   # 示例代码：打印两种 Config 的关键字段
   from model.model_minimind import MiniMindConfig
   import math

   def describe(cfg, name):
       print(f'--- {name} ---')
       print(f'hidden_size        = {cfg.hidden_size}')
       print(f'num_hidden_layers  = {cfg.num_hidden_layers}')
       print(f'num_attention_heads= {cfg.num_attention_heads}')
       print(f'num_key_value_heads= {cfg.num_key_value_heads}')
       print(f'head_dim           = {cfg.head_dim}')
       print(f'intermediate_size  = {cfg.intermediate_size}  '
             f'(手算: {math.ceil(cfg.hidden_size * math.pi / 64) * 64})')
       print(f'vocab_size         = {cfg.vocab_size}')
       print(f'tie_word_embeddings= {cfg.tie_word_embeddings}')
       print(f'use_moe            = {cfg.use_moe}')

   describe(MiniMindConfig(), '默认 (minimind-3 Dense)')
   describe(MiniMindConfig(hidden_size=512, num_hidden_layers=16), '窄而深')
   describe(MiniMindConfig(use_moe=True), 'MoE 版')
   ```

2. 运行：`python tmp_config.py`（CPU 即可，本实践不碰 GPU）。

**需要观察的现象**：

- 默认配置的 `head_dim=96`、`intermediate_size=2432`，与上一节手算一致。
- 改成 `hidden_size=512` 后，`head_dim` 变成 `512//8=64`、`intermediate_size` 变成 `ceil(512×π/64)×64=26×64=1664`——证明这两个量是**从 `hidden_size` 派生**的。
- MoE 版多出 `num_experts=4`、`num_experts_per_tok=1` 等字段，而 Dense 版这些字段虽也存在但不会被使用。

**预期结果**：三个配置的关键字段都能被正确打印，派生量与手算一致。无需联网、无需权重，几秒内跑完。

> 完成后删掉 `tmp_config.py`，避免污染仓库。

#### 4.1.5 小练习与答案

**练习 1**：默认配置下，`num_attention_heads=8`、`head_dim=96`。那么 `q_proj` 这个线性层把 `hidden_size=768` 映射到多大的输出维度？

> **答**：输出维度 = `num_attention_heads × head_dim = 8 × 96 = 768`。恰好等于 `hidden_size`，这是默认配置下的巧合（因为 `head_dim` 默认就是 `hidden_size // num_attention_heads`）。

**练习 2**：`tie_word_embeddings` 默认是 `True`。请用一句话解释它省掉了哪部分参数、为什么小模型特别需要它。

> **答**：它让输入端的 `embed_tokens`（id→向量）和输出端的 `lm_head`（向量→词表 logits）共享同一份形状为 `[vocab_size, hidden_size]` 的权重，从而省掉一个 `6400×768≈490 万`参数的大矩阵。小模型参数本就紧张，这个矩阵占比很可观，所以默认开启。

**练习 3**：如果只把 `num_hidden_layers` 从 8 加到 16（其它不变），参数量大概会变成原来的多少倍？

> **答**：几乎正好 2 倍。因为层数是「线性放大器」——除 embedding / lm_head / 末层 norm 外，几乎所有参数都按层数等比例重复（每层一份）。后面 4.3.4 的实践会验证这一点。

---

### 4.2 MiniMindModel：模型的「躯干」

#### 4.2.1 概念说明

有了 Config，就能搭模型。`MiniMindModel` 是 MiniMind 的**躯干（backbone）**：它接收 token id，输出「最后一层的隐藏状态向量」——但**不负责把向量变成词表上的概率**。最后那一步（语言模型头）是 `MiniMindForCausalLM` 的事。

为什么要分两层（Model 和 ForCausalLM）？这是 HuggingFace 的惯例，好处是**躯干可复用**：同一套躯干既能接「语言模型头」做续写（本项目的用法），也能接「奖励模型头」做打分（u7-l3 的 `LMForRewardModel` 就是这么干的）。把「特征提取」和「任务头」解耦，是工程上的清晰分工。

#### 4.2.2 核心流程

`MiniMindModel` 的数据流是经典的 Decoder-Only 五段式：

```text
input_ids [B, T]
   │  embed_tokens: id → 向量
   ▼
hidden_states [B, T, d]
   │  layers: 逐层做 注意力 + 前馈（× L 次）
   ▼
hidden_states [B, T, d]
   │  norm: 最后一层 RMSNorm
   ▼
hidden_states [B, T, d]   ← 躯干的输出，交给 lm_head
```

`forward` 里还有一个**贯穿全模型的细节**：把每一层产生的 KV Cache 收集起来（`presents`），供自回归生成时复用；以及把 MoE 的 `aux_loss` 汇总求和。这两点本讲只点到，细节在 u3-l2（KV Cache）和 u3-l4（aux_loss）。

#### 4.2.3 源码精读

先看 `__init__`，它就是把 Config 里那些字段「物化」成 `nn.Module` 的过程：

> [model/model_minimind.py:196-207](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L196-L207) —— `MiniMindModel.__init__`：建 embedding、建 L 个 block、建末层 norm，并预计算 RoPE 的 cos/sin 表缓存为 buffer。

```python
class MiniMindModel(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.vocab_size, self.num_hidden_layers = config.vocab_size, config.num_hidden_layers
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.dropout = nn.Dropout(config.dropout)
        self.layers = nn.ModuleList([MiniMindBlock(l, config) for l in range(self.num_hidden_layers)])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        freqs_cos, freqs_sin = precompute_freqs_cis(...)   # RoPE 预计算，见 u3-l3
        self.register_buffer("freqs_cos", freqs_cos, persistent=False)
        self.register_buffer("freqs_sin", freqs_sin, persistent=False)
```

四点解读：

1. **`embed_tokens`**：`[6400, 768]` 的查找表，把 token id 变成 768 维向量。这是输入侧的入口。
2. **`layers`**：用列表推导式造 `L` 个 `MiniMindBlock`。注意每个 block 都拿到了自己的序号 `l`——这个序号在某些结构里用来做层级相关的逻辑（比如层 drop），MiniMind 目前主要是占位。
3. **`norm`**：**整个模型只有这一个「最后」的 RMSNorm**（Pre-Norm 结构的特点：每个 block 内部还有自己的 norm，但 block 之间没有）。
4. **`register_buffer(..., persistent=False)`**：把 RoPE 的 cos/sin 表存成 buffer（跟着 `.to(device)` 走、但不写进 state_dict）。`persistent=False` 是为了不污染权重文件——这些表可以从 Config 重新算出来，没必要存。

> 关于 `MiniMindBlock`：本讲只看它「怎么组装」，不看内部细节——
> [model/model_minimind.py:178-194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L178-L194) —— 每个 block = `input_layernorm + Attention + 残差 + post_attention_layernorm + MLP + 残差`。关键是第 184 行的 `self.mlp = FeedForward(config) if not config.use_moe else MOEFeedForward(config)`——**Config 的 `use_moe` 在这里第一次真正改变了结构**：同一套代码，传不传 `use_moe=True` 会拼出 Dense 或 MoE 两种前馈层。Attention / FeedForward 的内部数学分别在 u3-l2、u3-l4 拆。

再看 `forward`，把上面的数据流落实：

> [model/model_minimind.py:209-232](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L209-L232) —— `MiniMindModel.forward`：embedding → 循环跑 L 层（收集 KV Cache）→ 末层 norm → 汇总 aux_loss → 返回三元组 `(hidden_states, presents, aux_loss)`。

```python
def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, **kwargs):
    ...
    hidden_states = self.dropout(self.embed_tokens(input_ids))           # id → 向量
    ...
    presents = []
    for layer, past_key_value in zip(self.layers, past_key_values):
        hidden_states, present = layer(hidden_states, position_embeddings, ...)
        presents.append(present)                                         # 收集每层 KV Cache
    hidden_states = self.norm(hidden_states)                             # 末层 norm
    aux_loss = sum([l.mlp.aux_loss for l in self.layers if isinstance(l.mlp, MOEFeedForward)], ...)
    return hidden_states, presents, aux_loss
```

注意它返回的是 **`hidden_states`（向量）而不是 logits（词表概率）**——这正是「躯干」的边界。把向量变成词表上的 logits，是下一节 `MiniMindForCausalLM` 的活。

> 名词解释：**残差连接（residual）**：`hidden_states += residual`，把「层的输入」加到「层的输出」上。它让信息可以「跳过」一层直达后方，是堆深网络（L=8 甚至更深）能训得动的关键。`MiniMindBlock` 里两处残差（attention 后、MLP 后），见 u3-l2/u3-l4。

#### 4.2.4 代码实践

**实践目标**：直接调用 `MiniMindModel`，验证它只输出向量、且形状随 `hidden_size` / `num_hidden_layers` 变化。

**操作步骤**（在仓库根目录运行 Python，**示例代码**）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindModel

cfg = MiniMindConfig()                  # dim=768, layers=8
backbone = MiniMindModel(cfg).eval()

input_ids = torch.randint(0, cfg.vocab_size, (2, 16))   # [batch=2, seq=16]
with torch.no_grad():
    hidden_states, presents, aux_loss = backbone(input_ids, use_cache=True)

print('hidden_states:', hidden_states.shape)   # 期望 [2, 16, 768]
print('KV Cache 层数:', len(presents))         # 期望 8（每层一份）
print('最后一维 == hidden_size:', hidden_states.shape[-1] == cfg.hidden_size)  # True
```

**需要观察的现象**：

- `hidden_states` 形状是 `[2, 16, 768]`——`最后一维` 恰好是 `hidden_size`，不是 `vocab_size`。这证明躯干**没有**把向量映射回词表。
- `presents` 是长度为 8 的列表，对应 8 层各自的 KV Cache。

**预期结果**：打印出 `torch.Size([2, 16, 768])`、`8`、`True`。CPU 上瞬间完成。如果你把 Config 改成 `hidden_size=512, num_hidden_layers=16`，会得到 `[2, 16, 512]` 和 16。

#### 4.2.5 小练习与答案

**练习 1**：`MiniMindModel` 返回的 `hidden_states` 形状最后一维是 768，而不是词表大小 6400。为什么？

> **答**：因为躯干的职责是「特征提取」，输出的是语义向量。把向量映射到词表大小（6400 维 logits）是语言模型头 `lm_head` 的工作，在 `MiniMindForCausalLM` 里完成。这种「躯干 + 任务头」的分层是 HuggingFace 惯例，便于复用躯干。

**练习 2**：`register_buffer("freqs_cos", ..., persistent=False)` 里 `persistent=False` 的含义是什么？如果改成 `True` 会怎样？

> **答**：`persistent=False` 表示这个 buffer **不会被写进 `state_dict`**——保存权重时不存它。原因是 RoPE 的 cos/sin 表可以由 Config（`head_dim`、`max_position_embeddings`、`rope_theta`）完全确定，存了会冗余。改成 `True` 会让 `.pth` 文件变大、且在跨配置加载时容易产生 key 冲突。

---

### 4.3 MiniMindForCausalLM：「躯干 + 语言模型头」的完整外壳

#### 4.3.1 概念说明

`MiniMindForCausalLM` 是你真正会用到的「完整模型」——`eval_llm.py` 加载的是它，所有 `train_*.py` 训练的也是它。它在 `MiniMindModel` 躯干之上做了三件事：

1. **加上语言模型头 `lm_head`**：把 768 维向量映射成 6400 维 logits（词表上每个词的「分数」）。
2. **（可选）做权重共享 tie**：让 `lm_head` 和 `embed_tokens` 共用同一份权重。
3. **继承 `GenerationMixin`**：白嫖 HuggingFace 的生成接口约定，再配合项目自己写的 `generate`（u3-l6）完成自回归采样。

> 名词解释：**Causal LM（因果语言模型）**：即「根据前面的 token 预测下一个 token」的模型，`Causal` 指「只能看左边、不能看右边」的因果掩码。`ForCausalLM` 这个后缀就是「带语言模型头的因果模型」。

#### 4.3.2 核心流程

实例化与一次前向的流程：

```text
MiniMindForCausalLM(config):
    self.model  = MiniMindModel(config)        # 躯干
    self.lm_head = Linear(hidden_size, vocab_size, bias=False)   # 语言模型头
    if tie_word_embeddings:
        embed_tokens.weight = lm_head.weight    # 共享同一份 [vocab, hidden] 权重

forward(input_ids, labels=None):
    hidden_states = self.model(input_ids)       # 躯干出向量
    logits = self.lm_head(hidden_states)        # 向量 → 词表 logits
    if labels is not None:
        loss = 交叉熵(logits[:-1], labels[1:])   # 位移预测下一个 token
    return (loss, logits, ...)
```

这里有一个贯穿所有训练阶段的**关键细节——位移（shift）**：用第 `t` 个位置的 logits 去预测第 `t+1` 个 token，所以要把 logits 和 labels 错一位对齐（`x = logits[..., :-1, :]`、`y = labels[..., 1:]`）。这会在 u3-l5 详细展开，本讲只点出它的存在。

#### 4.3.3 源码精读

先看类定义和 `__init__`，重点在最后三行——`lm_head` 的建立与 `tie`：

> [model/model_minimind.py:234-243](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L234-L243) —— `MiniMindForCausalLM`：声明 `config_class`、`_tied_weights_keys`（告诉 HF 哪两个权重是绑定的），建立躯干与 lm_head，并在 `tie_word_embeddings=True` 时把二者权重指到同一块内存。

```python
class MiniMindForCausalLM(PreTrainedModel, GenerationMixin):
    config_class = MiniMindConfig
    _tied_weights_keys = {"lm_head.weight": "model.embed_tokens.weight"}
    def __init__(self, config: MiniMindConfig = None):
        self.config = config or MiniMindConfig()
        super().__init__(self.config)
        self.model = MiniMindModel(self.config)
        self.lm_head = nn.Linear(self.config.hidden_size, self.config.vocab_size, bias=False)
        if self.config.tie_word_embeddings:
            self.model.embed_tokens.weight = self.lm_head.weight   # ← 权重共享
        self.post_init()
```

逐行解读：

- **`config_class = MiniMindConfig`**：告诉 `transformers`「我这个模型类配的是 `MiniMindConfig`」，这样 `AutoModelForCausalLM.from_pretrained(...)` 能自动把 config 和 model 配上。
- **`_tied_weights_keys = {...}`**：声明 `lm_head.weight` 和 `model.embed_tokens.weight` 是同一份权重。`transformers` 在保存/加载时会据此做去重，避免存两份。
- **`self.model = MiniMindModel(...)`**：把上一节的躯干装进来。
- **`self.lm_head = nn.Linear(hidden_size, vocab_size, bias=False)`**：`[768] → [6400]`，注意 `bias=False`——大模型几乎都不用偏置，省参数且对归一化更友好。
- **`self.model.embed_tokens.weight = self.lm_head.weight`**：这是 tie 的真正动作——**让两个名字指向同一块显存**，改一个另一个跟着变。
- **`self.post_init()`**：`PreTrainedModel` 提供的标准初始化收尾（权重初始化、tie 校验等）。

再看 `forward` 怎么把躯干和头串起来：

> [model/model_minimind.py:245-253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L245-L253) —— `forward`：调用躯干拿 `hidden_states`，用 `lm_head` 映射成 logits，带 labels 时算位移交叉熵损失；返回 `MoeCausalLMOutputWithPast`（包含 `loss` / `aux_loss` / `logits` / KV Cache）。

```python
def forward(self, input_ids, attention_mask=None, past_key_values=None, use_cache=False, logits_to_keep=0, labels=None, **kwargs):
    hidden_states, past_key_values, aux_loss = self.model(input_ids, attention_mask, past_key_values, use_cache, **kwargs)
    slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
    logits = self.lm_head(hidden_states[:, slice_indices, :])
    loss = None
    if labels is not None:
        x, y = logits[..., :-1, :].contiguous(), labels[..., 1:].contiguous()
        loss = F.cross_entropy(x.view(-1, x.size(-1)), y.view(-1), ignore_index=-100)
    return MoeCausalLMOutputWithPast(loss=loss, aux_loss=aux_loss, logits=logits, past_key_values=past_key_values, hidden_states=hidden_states)
```

两点解读：

- **`logits_to_keep` 切片**：推理时往往只需要最后一个位置的 logits（用来采下一个 token），用切片只对最后几位算 `lm_head`，省算力。细节在 u3-l5 / u3-l6。
- **返回类型 `MoeCausalLMOutputWithPast`**：借自 `transformers`，一个带名字字段的容器（`loss` / `aux_loss` / `logits` / `past_key_values` / `hidden_states`）。即使是 Dense 模型也用这个 MoE 风格的返回类型，统一了下游代码。

最后是参数量统计的「官方尺子」——`get_model_params`：

> [trainer/trainer_utils.py:18-28](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L18-L28) —— `get_model_params(model, config)`：用 `p.numel()` 求总参数；对 MoE，额外算出「激活参数」（base + 被选中专家），于是能打印 `198M-A64M` 这种「总量-激活量」格式。

```python
def get_model_params(model, config):
    total = sum(p.numel() for p in model.parameters()) / 1e6
    n_routed = getattr(config, 'n_routed_experts', getattr(config, 'num_experts', 0))
    n_active = getattr(config, 'num_experts_per_tok', 0)
    ...
    if active < total: Logger(f'Model Params: {total:.2f}M-A{active:.2f}M')
    else: Logger(f'Model Params: {total:.2f}M')
```

这段代码的逻辑是：Dense 模型 `active == total`，打印一行 `Model Params: 64.xxM`；MoE 模型 `active < total`，打印两段 `Model Params: 198.xxM-A64.xxM`。你在 `eval_llm.py` 启动时看到的那行参数量，就是它打印的（u1-l3 已见过）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：这是本讲的核心实践——修改 `MiniMindConfig` 的 `hidden_size` 与 `num_hidden_layers`，实例化完整模型，用 `get_model_params` 打印两种配置的总参数量与差异，亲手验证「层数是线性放大器」这个结论。

**操作步骤**：

1. 在仓库根目录新建临时脚本 `tmp_params.py`（**示例代码**）：

   ```python
   # 示例代码：对比两种配置的参数量
   import sys, os
   sys.path.append(os.path.abspath('.'))
   from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
   from trainer.trainer_utils import get_model_params

   def build_and_count(hidden_size, num_hidden_layers, tag):
       cfg = MiniMindConfig(hidden_size=hidden_size, num_hidden_layers=num_hidden_layers)
       model = MiniMindForCausalLM(cfg)
       get_model_params(model, cfg)                       # 打印 Model Params: ...M
       total = sum(p.numel() for p in model.parameters()) # 自己再算一次，方便相减
       print(f'[{tag}] hidden={hidden_size}, layers={num_hidden_layers}, '
             f'总参数={total/1e6:.2f}M')
       return total

   n_default = build_and_count(768, 8,  '默认 minimind-3')
   n_deep    = build_and_count(768, 16, '层数翻倍')
   n_thin    = build_and_count(512, 8,  '更窄')

   print(f'\n默认 vs 层数翻倍 差异: {(n_deep - n_default)/1e6:+.2f}M  '
         f'(倍率 {n_deep/n_default:.2f})')
   print(f'默认 vs 更窄      差异: {(n_thin - n_default)/1e6:+.2f}M')
   ```

2. 运行：`python tmp_params.py`（CPU 即可，仅实例化、不做前向，几秒完成）。

**需要观察的现象**：

- 第一行 `get_model_params` 会打印类似 `Model Params: 63.92M`——这正是 README 说的 minimind-3 `64M`。
- `层数翻倍` 那次的总参数约为默认的 **2 倍**（倍率接近 2.0），证明层数对参数量是近似线性放大。
- `更窄`（hidden=512）那次总参数明显小于默认——宽度对参数量是**平方**关系（FFN 占大头，约为 `3 × d × intermediate_size`，而 `intermediate_size` 又正比于 `d`）。

**预期结果**（手算估值，实际数值以本地运行为准）：

| 配置 | 总参数（手算估值） |
|------|--------------------|
| `768, 8`（默认） | ≈ 63.9M（README 标称 64M） |
| `768, 16`（层数翻倍） | ≈ 122M（约为默认 2 倍，因为 embedding 那块不随层数翻倍，所以略低于 2 倍） |
| `512, 8`（更窄） | ≈ 30M |

> 说明：上表「手算估值」是把 embedding、L 层的 attention+FFN、末层 norm 都加起来得到的，仅作参照。**精确小数以你本地 `python tmp_params.py` 的输出为准**（待本地验证）。

3. 完成后删掉 `tmp_params.py`。

#### 4.3.5 小练习与答案

**练习 1**：在 `MiniMindForCausalLM.__init__` 里，去掉 `if self.config.tie_word_embeddings: self.model.embed_tokens.weight = self.lm_head.weight` 这一行（即不 tie），默认配置下参数量会变多多少？

> **答**：会多出一个 `lm_head` 的权重 `vocab_size × hidden_size = 6400 × 768 ≈ 491 万 ≈ 4.9M`。因为不 tie 时，`embed_tokens` 和 `lm_head` 各自独立持有一份 `[6400, 768]` 的矩阵。

**练习 2**：`MiniMindForCausalLM` 同时继承了 `PreTrainedModel` 和 `GenerationMixin`。前者我们已经用到（`config_class`、`post_init`），后者带来了什么能力？

> **答**：`GenerationMixin` 是 HuggingFace 的「生成接口」基类，带来 `model.generate(...)` 这套自回归生成的标准协议（输入 input_ids、输出 generated_ids）。MiniMind 自己重写了 `generate`（u3-l6），但继承 `GenerationMixin` 让它能与 `transformers` 生态的 streamer、`GenerationConfig` 等工具无缝配合。

**练习 3**：`forward` 里 `x, y = logits[..., :-1, :], labels[..., 1:]` 为什么要这样「错一位」？

> **答**：因为目标是「用第 `t` 个位置预测第 `t+1` 个 token」。`logits[..., :-1, :]` 是前 `T-1` 个位置的预测（位置 0~T-2），`labels[..., 1:]` 是后 `T-1` 个位置的真值（位置 1~T-1）。两者错位对齐后做交叉熵，每个位置都在练习「预测下一个词」。`ignore_index=-100` 让被掩码的位置（u2-l2 讲过的 padding / 提问段）不参与 loss。

---

## 5. 综合实践

把本讲三个模块串起来，设计你自己的「minimind 变体」。

**任务**：在不改动 `model_minimind.py` 任何源码的前提下，仅通过构造不同的 `MiniMindConfig`，定制一个**参数量尽量接近 30M** 的小模型，并验证它能跑通一次前向。

**要求**：

1. 自行挑选 `hidden_size` 与 `num_hidden_layers` 的组合（提示：参考 4.3.4 的「更窄」实验，`hidden=512, layers=8` 约为 30M，你也可以尝试 `hidden=448 / layers=10` 之类的组合），目标是用 `get_model_params` 打印出 `≈30M`。
2. 用 `MiniMindForCausalLM(cfg)` 实例化完整模型。
3. 喂一段假的 `input_ids`（随机整数，`batch=1, seq=32`）和对应 `labels`，调用 `model.forward(...)`，确认能返回 `loss`（一个标量）和 `logits`（形状最后一维是 `vocab_size=6400`）。
4. 把你选的组合、打印出的参数量、`loss` 是否为标量、`logits` 最后一维是否为 6400，记录下来。

**示例代码**（**示例代码，不放进仓库**）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from trainer.trainer_utils import get_model_params

cfg = MiniMindConfig(hidden_size=512, num_hidden_layers=8)   # 你的组合
model = MiniMindForCausalLM(cfg).eval()
get_model_params(model, cfg)

input_ids = torch.randint(0, cfg.vocab_size, (1, 32))
labels = input_ids.clone()
with torch.no_grad():
    out = model(input_ids, labels=labels)
print('loss:', out.loss.item(), '  logits:', tuple(out.logits.shape))
# 期望: loss 是一个浮点数；logits 最后一维 == 6400
```

**验收标准**：

- 打印的参数量在 30M 上下（±5M 都算成功，重在理解旋钮的作用）。
- `out.loss` 是一个标量（能 `.item()`）。
- `out.logits` 形状为 `[1, 32, 6400]`（最后一维 = `vocab_size`）。

> 这是「源码阅读 + 配置实验」型实践，CPU 即可完成，无需训练、无需下载权重。它的意义在于：你证明了**同一份 `model_minimind.py` 能造出任意尺寸的模型**，这正是 Config 抽象的价值。

---

## 6. 本讲小结

- `MiniMindConfig` 是模型的「出厂参数表」，继承 `PretrainedConfig`，靠 `hidden_size / num_hidden_layers / use_moe` 三个位置参数 + 一堆 `kwargs.get(..., 默认值)` 描述模型形态；它对齐 Qwen3 生态，默认值给出约 64M 的 Dense 模型。
- 两个派生量很关键：`head_dim = hidden_size // num_attention_heads`、`intermediate_size = ceil(hidden_size × π / 64) × 64`，它们决定了每一层矩阵的形状。
- `MiniMindModel` 是躯干：`embed_tokens → L 个 MiniMindBlock → 末层 norm`，输出的是「隐藏状态向量」而不是词表 logits；`use_moe` 在 `MiniMindBlock` 里第一次真正改变结构（切换 `FeedForward` / `MOEFeedForward`）。
- `MiniMindForCausalLM` 是完整外壳：躯干 + `lm_head`，通过 `tie_word_embeddings` 让输入端 embedding 与输出端 lm_head **共享同一份 `[vocab, hidden]` 权重**，省下约 4.9M 参数。
- `forward` 用「位移交叉熵」训练（`logits[:-1]` 对 `labels[1:]`），返回 `MoeCausalLMOutputWithPast` 容器；`get_model_params` 是官方参数尺子，对 MoE 能打印「总量-激活量」格式。
- 层数是参数量的「线性放大器」（L 翻倍 ≈ 参数翻倍），宽度则是「平方」关系——这是 README 「模型配置」一节讨论 `dim vs n_layers` 取舍的物理基础。

---

## 7. 下一步学习建议

本讲只搭好了「骨架」，骨架里每一块骨头都还没拆。建议按下面的顺序继续第 3 单元：

1. **u3-l2 RMSNorm 与 GQA 注意力（含 KV Cache）**：拆开本讲里一笔带过的 `RMSNorm`、`Attention` 的 q/k/v 投影、`q_norm`/`k_norm`、`repeat_kv` 广播与 KV Cache 拼接。
2. **u3-l3 RoPE 旋转位置编码与 YaRN 长度外推**：搞懂本讲 `MiniMindModel.__init__` 里 `precompute_freqs_cis` 那两行到底预计算了什么，以及 `inference_rope_scaling` 怎么把 2048 外推到 4 倍长度。
3. **u3-l4 SwiGLU 前馈网络与 MoE 路由**：拆开 `FeedForward` 与 `MOEFeedForward`，理解 `use_moe=True` 时 `198M-A64M` 是怎么来的，以及 `aux_loss` 怎么缓解专家负载不均。
4. **u3-l5 CausalLM 前向传播与交叉熵损失**：精读本讲 `forward` 里被跳过的 `logits_to_keep` 切片、位移交叉熵、`loss = CE + aux_loss` 的组合。

读完 u3-l2 ~ u3-l5，再回来看 `MiniMindModel.forward`，你会发现当初「先跳过」的每一行都能对上号——那时候本讲的「骨架」就长出了血肉。
