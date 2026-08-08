# 草稿模型架构与配置

## 1. 本讲目标

上一讲（u2-l1）我们画出了 `dflash_generate` 的全局控制流地图，但地图里反复出现的草稿模型 `model(...)` 始终是一个黑盒。当时我们说：草稿模型是「残缺」的——它没有自己的 `embed_tokens` 和 `lm_head`，而是借用 target 的；我们还把 `fc`、`hidden_norm`、`build_target_layer_ids` 这几样东西留到了本讲。本讲就是来**打开草稿模型这个黑盒**的。

具体来说，学完本讲你应该能够：

1. 说清 `DFlashDraftModel` 由哪些组件构成、**复用了哪些 Qwen3 原件**、又**缺了什么**（为什么是「残缺」的）。
2. 解释 `build_target_layer_ids` 如何用一条公式**在 target 的深度方向上均匀挑出若干层**，以及它为什么故意跳过最前面 1 层和最后面 3 层。
3. 读懂 `DFlashDraftModel.forward`：`noise_embedding` 直接当输入、`fc + hidden_norm` 把多层 target 隐藏状态投影压缩、再逐层走 `Qwen3DFlashDecoderLayer`。
4. 把草稿模型的 `config.json` 里的 `num_target_layers`、`num_hidden_layers`、`block_size`、`target_layer_ids` 对应到源码里的读取逻辑，并自己复算 `target_layer_ids` 与 config 对比。

本讲**只讲草稿模型自身的结构与配置**。注意力层内部如何把 target 隐藏状态当作 context 的 key/value、噪声 token 如何参与注意力——那是块扩散注意力的核心，留给下一讲 **u2-l3**。本讲我们把每一层 `Qwen3DFlashDecoderLayer` 先当成「接收 `hidden_states` 和 `target_hidden` 两个输入、产出一个新的 `hidden_states` 的盒子」来看。

## 2. 前置知识

在进入源码前，补齐三个本讲要用到的基础概念。它们承接 u1-l1 / u2-l1 已建立的心智模型，这里只讲本讲新需要的部分。

### 2.1 「隐藏状态（hidden states）」是什么

一个 Transformer 模型一次前向，除了输出最终的 logits，还会产出**每一层**的中间表示，这就是 `hidden_states`。

- 在 Transformers 里，当调用时传 `output_hidden_states=True`，返回对象会有一个 `hidden_states` 字段，它是一个**元组**。
- 这个元组的长度是「层数 + 1」：**第 0 项是嵌入层（embedding）的输出**，第 1 项是第 0 个解码层（decoder layer）的输出，第 i 项是第 i−1 个解码层的输出，依此类推。
- 每一项的形状都是 `(batch, seq_len, hidden_size)`——也就是「每个 token 在该层的一个向量」。

这一点很关键，因为本讲的 `extract_context_feature` 要从 target 的多层隐藏状态里挑出几层，而它取下标时用了一个 `offset = 1`（正是为了跳过第 0 项的嵌入层）。这一点上一讲提到过，本讲会在 4.2 里讲清楚它和 `build_target_layer_ids` 的关系。

### 2.2 为什么要「把多层 target 特征映射进 draft」

回忆 u2-l1 的块起草阶段：草稿模型不是凭空生成 token，而是「看着 target 在上下文上的中间层表示」来还原整块 token。target 的中间层表示是 `target_hidden`，它由 `extract_context_feature` 把**好几层** target 隐藏状态沿特征维**拼接**而成，所以它的特征维度是 `层数 × hidden_size`，比单层要宽。

但草稿模型自己的层只能吃「`hidden_size`」宽度的输入。于是需要一个**投影层**把这个宽向量压回 `hidden_size`——这就是本讲的 `fc`。直觉上：`fc` 是 target 与 draft 两种「表示空间」之间的**翻译器**，让 target 的多层综合判断能被 draft 的层消化。这个 `fc` 是**草稿模型在训练时自己学到的**（不是 target 的权重）。

### 2.3 「复用 Qwen3 组件」意味着什么

DFlash 的草稿模型在架构上**直接借用 Qwen3 的零件**：归一化用 `Qwen3RMSNorm`、旋转位置编码用 `Qwen3RotaryEmbedding`、MLP 用 `Qwen3MLP`，整个类还继承自 `Qwen3PreTrainedModel`。这些零件都是从 `transformers.models.qwen3.modeling_qwen3` 里 import 来的（[顶部 import 段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L7-L21)）。

这样做的好处是：草稿模型能**直接复用 Qwen3 的权重加载机制**（`from_pretrained`）、配置类（`Qwen3Config`）、以及各种注意力实现（flash_attention_2 / sdpa）。代价是草稿模型只支持 Qwen3 系（Transformers 后端还额外支持 LLaMA-3.1），这点 README 也写明了。权重加载与注意力实现的细节留给 u2-l5，本讲只关注「它复用了哪些组件来搭骨架」。

## 3. 本讲源码地图

本讲只围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `dflash/model.py` | Transformers/PyTorch 参考实现 | `DFlashDraftModel` 的类定义、`__init__`、`build_target_layer_ids`、`forward`，以及配套的 `extract_context_feature` |

本讲要读的代码集中在文件的后半部分（草稿模型本体）和最顶部（工具函数）。先给一张「谁调用谁」的关系图，帮你定位：

```
DFlashDraftModel.__init__()          ← 读 config, 搭骨架(layers/norm/rotary/fc/hidden_norm)
   └─ build_target_layer_ids()       ← 若 config 没给 target_layer_ids, 用公式算
   └─ self.fc = Linear(层数*hidden_size → hidden_size)

DFlashDraftModel.forward(target_hidden, noise_embedding, ...)
   └─ extract_context_feature(...)   ← (在 dflash_generate 里) 把多层 target 拼成 target_hidden
   └─ target_hidden = hidden_norm(fc(target_hidden))   ← 投影压缩
   └─ for layer in self.layers:       ← 每层是 Qwen3DFlashDecoderLayer
         layer(hidden_states, target_hidden, ...)
   └─ return self.norm(hidden_states)

dflash_generate 调用链 (u2-l1 已讲):
   prefill:  target_hidden = extract_context_feature(target.hidden_states, model.target_layer_ids)
   块起草:   draft_logits = target.lm_head( model(target_hidden, noise_embedding, ...) )
```

注意图里一个贯穿本讲的事实：`model.forward(...)` 的返回值是**隐藏状态**（还没过 lm_head），随后由 `target.lm_head` 把它变成 logits——这正是 u2-l1 说的「第二次借」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应规格要求的三个模块：`__init__` 与配置读取、`build_target_layer_ids`、`forward`。

### 4.1 草稿模型骨架：`DFlashDraftModel.__init__` 与 config 读取

#### 4.1.1 概念说明

`DFlashDraftModel` 是一个**很小**的模型：它只有若干个解码层（`layers`）、一个最终归一化（`norm`）、旋转位置编码（`rotary_emb`）、一个投影层（`fc`）和一个隐藏状态归一化（`hidden_norm`）。它**没有**自己的嵌入层（`embed_tokens`）和输出头（`lm_head`）——这两样在生成时向 target 借。

构造它的全部信息都来自一个 `config` 对象（类型是 `Qwen3Config`）。DFlash 在标准 Qwen3 config 之上**额外加了几个字段**，构成它自己的配置：

| config 字段 | 含义 | 在哪用 |
|---|---|---|
| `num_hidden_layers` | **草稿模型自己的层数**（注意：是 draft 的层数，不是 target 的） | `__init__` 建多少个 `Qwen3DFlashDecoderLayer` |
| `hidden_size` | 每层隐藏维度 | 决定 `fc`、`norm` 的形状 |
| `num_target_layers` | **target（目标模型）的总层数** | 传给 `build_target_layer_ids` 决定取哪几层 |
| `block_size` | 块扩散的块大小（如 `b16` 即 16） | u2-l1 讲过，控制并行起草的宽度 |
| `dflash_config`（字典） | DFlash 专属配置的容器 | 存放 `target_layer_ids`、`mask_token_id` |

这里有个**极易混淆的命名**要特别记住：`config.num_hidden_layers` 是**草稿模型**自己的层数，而 `config.num_target_layers` 才是 **target** 的层数。在 4.2 你会看到，`build_target_layer_ids` 的第二个参数名叫 `num_draft_layers`，传进去的正是 `config.num_hidden_layers`——也就是说「草稿有几层」决定了「在 target 上均匀取几个点」。

#### 4.1.2 核心流程

构造一个草稿模型，依次做这些事：

```
1. super().__init__(config)            # 继承 Qwen3PreTrainedModel 的初始化
2. self.layers ← num_hidden_layers 个 Qwen3DFlashDecoderLayer
3. self.target_layer_ids ← config.dflash_config.get("target_layer_ids",
                                       build_target_layer_ids(num_target_layers, num_hidden_layers))
   # config 显式给了就用 config 的; 没给就用公式现算
4. self.norm ← Qwen3RMSNorm(hidden_size)
5. self.rotary_emb ← Qwen3RotaryEmbedding(config)
6. self.fc ← Linear( len(target_layer_ids) * hidden_size  →  hidden_size , bias=False)
7. self.hidden_norm ← Qwen3RMSNorm(hidden_size)
8. self.block_size ← config.block_size
9. self.mask_token_id ← config.dflash_config.get("mask_token_id", None)
10. self.post_init()                   # 继承自父类, 初始化权重
```

两个关键设计点先记住，后面逐个展开：

- **第 6 步的 `fc`**：输入维度是「`取几层 × hidden_size`」，输出是 `hidden_size`。这个「取几层」就是 `len(self.target_layer_ids)`，与 4.2 的 `build_target_layer_ids` 和 `extract_context_feature` 的拼接维度**三者必须一致**——这是整条链路能跑通的几何约束。
- **第 3 步的取值优先级**：`dflash_config.get("target_layer_ids", 公式)`。意思是 config.json 里**可以**显式写死 `target_layer_ids`，写死就用写死的；不写就退回公式 `build_target_layer_ids`。

#### 4.1.3 源码精读

先看类头，它继承了 `Qwen3PreTrainedModel`，并把 `_no_split_modules` 指向自己的解码层：

[类定义与 `_no_split_modules`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L302-L304)。两点说明：

- `config_class = Qwen3Config`：让这个模型复用 Qwen3 的配置类，所以 `from_pretrained` 能正确解析 config.json。
- `_no_split_modules = ["Qwen3DFlashDecoderLayer"]`：告诉 Transformers 的设备分布逻辑「这个类是一个不可拆分的模块单元」，在多卡 `device_map` 时按这个粒度切分。它的权重加载细节在 u2-l5 讲。

再看 `__init__` 主体：

[`__init__` 全文](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321)。逐段：

```python
self.layers = nn.ModuleList(
    [Qwen3DFlashDecoderLayer(config, layer_idx) for layer_idx in range(config.num_hidden_layers)]
)
```

草稿模型有多少层，完全由 `config.num_hidden_layers` 决定。每一层都是一个 `Qwen3DFlashDecoderLayer`（下一讲 u2-l3 会拆开它的注意力；本讲只把它当一个盒子）。注意它**不叫** `num_draft_layers`，而是用 Qwen3 原生的 `num_hidden_layers` 字段来表示草稿层数——这就是 4.1.1 提醒过的命名陷阱。

```python
self.target_layer_ids = self.config.dflash_config.get(
    "target_layer_ids", build_target_layer_ids(config.num_target_layers, config.num_hidden_layers)
)
```

这是本讲的第一个重点。`self.config.dflash_config` 是 config.json 里的一个字典（DFlash 专属配置都塞在里面）。`.get("target_layer_ids", 默认值)` 的逻辑是：**如果 config.json 显式写了 `target_layer_ids` 就用它；否则用 `build_target_layer_ids(num_target_layers, num_hidden_layers)` 现算。** 注意传给公式的两个参数：第一个是 target 的层数 `num_target_layers`，第二个是草稿自己的层数 `num_hidden_layers`。这条取值优先级，是本讲综合实践里「复算并对比」的依据。

```python
self.norm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
self.rotary_emb = Qwen3RotaryEmbedding(config)
self.fc = nn.Linear(len(self.target_layer_ids) * config.hidden_size, config.hidden_size, bias=False)
self.hidden_norm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
self.block_size = config.block_size
self.mask_token_id = self.config.dflash_config.get("mask_token_id", None)
self.post_init()
```

把这几行和「骨架组件表」对照看：

- `fc`：**本讲的第二个重点**。输入 `len(self.target_layer_ids) * hidden_size`，正是 `extract_context_feature` 拼接多层 target 隐藏状态后的宽度（见 [extract_context_feature](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39-L45) 里的 `torch.cat(selected_states, dim=-1)`）。输出 `hidden_size`，对齐草稿层的输入维度。`bias=False` 是常见做法。
- `hidden_norm`：对 `fc` 的输出再做一次 RMSNorm——在 4.3 会看到它紧跟 `fc`。
- `block_size`：直接从 `config.block_size` 读，**不在** `dflash_config` 字典里（它放在 config 顶层）。所以 README 里模型名 `Qwen3-8B-DFlash-b16` 的 `b16` 对应 `config.block_size = 16`。
- `mask_token_id`：和 `target_layer_ids` 一样走 `dflash_config.get(..., None)`，**可省略**（省略时为 `None`，由 `dflash_generate` 的调用方或别处兜底）。
- 注意**没有** `self.embed_tokens`、**没有** `self.lm_head`——这就是「残缺」的字面证据，它们在生成时向 target 借（u2-l1 的两个「借」）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用眼睛确认草稿模型「缺什么、有什么」，不需要 GPU。

**步骤**：

1. 打开 [model.py#L306-L321](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321) 的 `__init__`。
2. 列一张表，左边写「草稿模型 `self.xxx` 有的组件」，右边对照一个标准 Qwen3 模型会有的组件，找出**两个缺失**的组件。
3. 回答：`fc` 的输入维度公式是什么？为什么必须等于 `len(self.target_layer_ids) * hidden_size`？

**预期结果**：缺失的是 `embed_tokens` 与 `lm_head`；`fc` 输入维度 = `len(target_layer_ids) * hidden_size`，因为 `extract_context_feature` 会把这么多层、每层 `hidden_size` 宽的向量沿最后一维拼接（见 4.2.3 与 [extract_context_feature](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39-L45)）。

**进阶（需 GPU/需安装 transformers extra）**：按 README 的 Transformers Quick Start 加载 `draft` 后，打印 `(name, tuple(p.shape))`：

```python
# 示例代码: 列出草稿模型顶层参数组件
for name, _ in draft.named_children():
    print(name)
# 期望看到: layers, norm, rotary_emb, fc, hidden_norm
# 期望: 看不到 embed_tokens / lm_head
```

具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：`config.num_hidden_layers` 和 `config.num_target_layers` 分别指谁的层数？为什么容易搞混？

**答案**：`num_hidden_layers` 是**草稿模型自己**的层数（因为草稿继承 Qwen3，沿用了这个原生字段名）；`num_target_layers` 是 **target（目标模型）** 的层数。容易搞混，是因为「hidden layers」这个名字听起来像是描述一个「完整的」模型，但在这里它描述的恰恰是那个很小的草稿，而 target 的层数反而被放到了一个 DFlash 自定义字段里。

**Q2**：为什么 `target_layer_ids` 和 `mask_token_id` 都用 `dflash_config.get(...)` 读取，而 `block_size` 直接写 `config.block_size`？

**答案**：`dflash_config` 是 DFlash 专属配置的嵌套字典，里面放**可省略**、有默认逻辑的字段（`target_layer_ids` 省略时用公式算、`mask_token_id` 省略时为 `None`）；`block_size` 是必填项，直接放在 config 顶层（`config.block_size`），模型名 `b16` 即取它。所以「放在 dflash_config 里」≈「有兜底逻辑的可选项」。

---

### 4.2 `build_target_layer_ids`：决定从 target 取哪几层

#### 4.2.1 概念说明

草稿模型要看 target 的中间层表示，但 target 有几十层，**全部**取太贵、也冗余。`build_target_layer_ids` 解决的问题是：**给定 target 一共有 `num_target_layers` 层、草稿想取 `num_draft_layers` 层，应该挑 target 的哪几层？**

它的策略很朴素：**在 target 的深度方向上把这几层均匀铺开**，并且故意**避开最前面 1 层和最后面 3 层**。直觉是：

- 最前面的层贴近嵌入层，语义还比较「原始」；
- 最后面的层贴近 lm_head，已经偏向「预测下一个 token」；
- 中间层通常被认为携带更丰富、更抽象的表示，适合作为草稿的上下文参考。

注意：函数返回的是「层下标」，而真正取隐藏状态时还有个 `offset = 1`（因为隐藏状态元组的第 0 项是嵌入层）。所以「下标 k」最终取的是 target 第 k 个解码层的输出。

#### 4.2.2 核心流程

函数对两种情况分别处理：

```
build_target_layer_ids(num_target_layers, num_draft_layers):
  if num_draft_layers == 1:
      return [num_target_layers // 2]          # 只取一层: 取正中间
  # 取多层: 在 [1, num_target_layers - 3] 区间内均匀取 num_draft_layers 个点
  start = 1
  end   = num_target_layers - 3
  span  = end - start
  return [ round(start + i * span / (num_draft_layers - 1))  for i in 0..num_draft_layers-1 ]
```

把区间端点和「采样点」画出来（设取 `D = num_draft_layers` 层）：

```
target 层:  0   1   2   ...                    ...  L-4  L-3  L-2  L-1   (L = num_target_layers)
            |   |<---------------- span ---------------->|    |    |
           跳过  起点start=1                          终点end=L-3   跳过最后3层
                 ·    ·    ·    ·    ·   ← 在 [start, end] 上均匀取 D 个点 (含两端)
```

每个采样点的位置是一条**线性插值**公式。设 \(L\) 为 `num_target_layers`、\(D\) 为 `num_draft_layers`，第 \(i\) 个点（\(i = 0, 1, \dots, D-1\)）的层下标为：

\[
\text{layer}_i = \mathrm{round}\!\left( 1 \;+\; \frac{i \cdot (L - 4)}{D - 1} \right), \qquad D > 1
\]

其中 `span = end - start = (L - 3) - 1 = L - 4`。当 \(D = 1\) 时单独走 `[L // 2]`，取正中。

> **一个易踩的坑**：这里用的是 Python 内置 `round()`，它做的是**银行家舍入（round-half-to-even）**，即 `round(0.5)=0`、`round(1.5)=2`、`round(2.5)=2`。如果你手动复算时遇到正好 `.5` 的情况，要按这个规则，否则会和 config 里的值差 1。源码用的是 `int(round(...))`。

#### 4.2.3 源码精读

[build_target_layer_ids 全文](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L27-L36)：

```python
def build_target_layer_ids(num_target_layers: int, num_draft_layers: int):
    if num_draft_layers == 1:
        return [num_target_layers // 2]
    start = 1
    end = num_target_layers - 3
    span = end - start
    return [
        int(round(start + (i * span) / (num_draft_layers - 1)))
        for i in range(num_draft_layers)
    ]
```

逐行：

- 单层特例 `[num_target_layers // 2]`：只取一层时取 target 的正中间层。
- `start = 1`、`end = num_target_layers - 3`：采样区间的左右端点。**左端跳过第 0 层**（贴近嵌入），**右端跳过最后 3 层**（贴近 lm_head）。
- 列表推导里 `i * span / (num_draft_layers - 1)`：当 `i=0` 时为 `start`，当 `i = num_draft_layers - 1` 时为 `start + span = end`，中间均匀插值——典型的「在闭区间上等距取 D 个点」。
- `int(round(...))`：四舍五入到整数层下标（注意上面说的银行家舍入）。

**它和 `extract_context_feature` 的衔接**：函数返回的是「层下标」，但要真正拿到隐藏状态，还要过 [extract_context_feature](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39-L45)：

```python
def extract_context_feature(hidden_states, layer_ids):
    offset = 1
    selected_states = [hidden_states[layer_id + offset] for layer_id in layer_ids]
    return torch.cat(selected_states, dim=-1)
```

那个 `offset = 1` 就是「跳过 hidden_states 元组的第 0 项（嵌入层）」——所以 `target_layer_ids` 里的下标 `k` 实际取的是 target 第 `k` 个解码层的输出。最后 `torch.cat(..., dim=-1)` 把这几层沿特征维拼接，输出宽度 = `len(layer_ids) * hidden_size`，**正好喂给 `__init__` 里的 `self.fc`**。这就是 4.1 反复强调的「三者维度必须一致」。

> 在 `dflash_generate` 里，这两个函数是这样联动的（u2-l1 已讲）：prefill 时 [`target_hidden = extract_context_feature(output.hidden_states, model.target_layer_ids)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L98-L99)；块起草时把 `target_hidden` 喂给 `model(...)`（即 `forward`），在里面被 `fc + hidden_norm` 处理。

#### 4.2.4 代码实践（纯计算型，无需 GPU）

**目标**：手算一组 `target_layer_ids`，验证你对公式的理解。

**步骤**：用纸笔或一个纯 Python 脚本（**不用** import dflash，避免触发 torch/transformers），把上面的函数体抄进去，对下面三组输入算结果：

```
(1) num_target_layers = 32, num_draft_layers = 1   → 期望 [16]
(2) num_target_layers = 32, num_draft_layers = 2   → 期望 [1, 29]
(3) num_target_layers = 32, num_draft_layers = 4   → 期望 [1, 10, 20, 29]   (注意 round)
```

**需要观察的现象**：

- 第 (1) 组取正中。
- 第 (2) 组落在区间 `[1, 29]` 的两端。
- 第 (3) 组四个点应在 `[1, 29]` 内大致等距：`start + i*28/3` 即 `1, 10.33→10, 19.67→20, 29`（`round(10.33)=10`，`round(19.67)=20`）。

**预期结果**：与上面括号里的期望一致。如果某一步遇到正好 `.5` 的情形，记得用银行家舍入。

> 这些数字是**假设性**输入，仅用于练习公式。真实草稿模型的 `num_target_layers` / `num_hidden_layers` 以其 config.json 为准，「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：为什么采样区间右端是 `num_target_layers - 3` 而不是 `num_target_layers - 1`？

**答案**：为了**跳过 target 最后 3 层**。最后几层贴近 lm_head，表示已偏向「预测下一个 token」，作为「上下文特征」喂给草稿未必好；代码用 `end = num_target_layers - 3` 一次性把这 3 层排除在候选之外。

**Q2**：假设 `num_target_layers = 32`、`num_draft_layers = 2`，手算结果是什么？并解释这两个数的含义。

**答案**：`[1, 29]`。含义是：草稿有 2 层（`num_draft_layers` 来自 `config.num_hidden_layers`），要从 32 层的 target（`num_target_layers`）里取 2 层作为上下文，分别取 target 的第 1 层和第 29 层（再经 `offset=1` 映射到隐藏状态元组）。注意 `num_target_layers` 指 target、`num_draft_layers` 指 draft，别搞反。

---

### 4.3 `DFlashDraftModel.forward`：fc 投影 + 逐层 + 返回隐藏状态

#### 4.3.1 概念说明

`forward` 是草稿模型「真正干活」的地方：它接收两路输入——**噪声嵌入** `noise_embedding`（u2-l1 说过，这是 target 的 `embed_tokens` 把一块 mask token 变成的嵌入，即「第一次借」的产物）和**目标隐藏状态** `target_hidden`（多层 target 特征拼接而成）——然后产出一块新的隐藏状态。

它内部做三件事：

1. **投影压缩**：用 `fc + hidden_norm` 把宽宽的 `target_hidden`（`层数×hidden_size`）压成 `hidden_size`，对齐草稿层的维度。
2. **逐层去噪**：让 `noise_embedding` 作为初始 `hidden_states`，依次穿过每个 `Qwen3DFlashDecoderLayer`；每一层同时看到「本块的噪声」和「投影后的 target 上下文」（下一讲 u2-l3 讲它们如何在注意力里拼起来）。
3. **返回隐藏状态**：最终过一个 `norm`，返回**未过 lm_head 的隐藏状态**——之后由 `target.lm_head` 转成 logits（「第二次借」）。

一个值得注意的细节：`forward` 的返回类型注解写的是 `CausalLMOutputWithPast`，但函数体实际 `return self.norm(hidden_states)`，返回的是一个**裸 Tensor**。阅读时以函数体为准（注解与实现不完全一致，属源码的小瑕疵，不影响运行）。

#### 4.3.2 核心流程

把 `forward` 抽象成数据流（设取了 `T = len(target_layer_ids)` 层，块大小 `B`）：

```
输入:
  noise_embedding : (1, B, hidden_size)        # 来自 target.embed_tokens(mask 块)
  target_hidden   : (1, ctx, T*hidden_size)    # 来自 extract_context_feature(多层拼接)

  hidden_states = noise_embedding
  target_hidden = hidden_norm( fc(target_hidden) )    # (1, ctx, T*hidden_size) → (1, ctx, hidden_size)
  position_embeddings = rotary_emb(hidden_states, position_ids)

  for layer in self.layers:                            # 每层:
      hidden_states = layer(                           #   吃 (hidden_states, target_hidden)
          hidden_states = hidden_states,               #   吐新的 hidden_states
          target_hidden = target_hidden,
          position_embeddings = position_embeddings,
          ...
      )

  return norm(hidden_states)                           # (1, B, hidden_size), 还没过 lm_head
```

几何约束再强调一次：`fc` 的输入维度 `T * hidden_size` 必须等于 `extract_context_feature` 拼接后的宽度；`fc` 的输出 `hidden_size` 必须等于草稿层期望的输入宽度。这两个约束分别在 `__init__` 和 `extract_context_feature` 里被同一组 `target_layer_ids` 保证一致。

#### 4.3.3 源码精读

[forward 全文](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L323-L347)：

```python
def forward(
    self,
    position_ids: torch.LongTensor,
    attention_mask: Optional[torch.Tensor] = None,
    noise_embedding: Optional[torch.Tensor] = None,
    target_hidden: Optional[torch.Tensor] = None,
    past_key_values: Optional[Cache] = None,
    use_cache: bool = False,
    **kwargs,
) -> CausalLMOutputWithPast:
    hidden_states = noise_embedding
    target_hidden = self.hidden_norm(self.fc(target_hidden))
    position_embeddings = self.rotary_emb(hidden_states, position_ids)
    for layer in self.layers:
        hidden_states = layer(
            hidden_states=hidden_states,
            target_hidden=target_hidden,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_values,
            use_cache=use_cache,
            position_embeddings=position_embeddings,
            **kwargs,
        )
    return self.norm(hidden_states)
```

逐行：

- `hidden_states = noise_embedding`：草稿模型**没有自己的嵌入层**，所以直接拿外面（target 的 `embed_tokens`）算好的嵌入当输入。对比标准 Qwen3 模型会先 `hidden_states = self.embed_tokens(input_ids)`——这里被省掉了，这就是「残缺」在 `forward` 里的体现。
- `target_hidden = self.hidden_norm(self.fc(target_hidden))`：**本讲的核心一行**。`fc` 把多层拼接的 target 特征（`T*hidden_size`）线性投影回 `hidden_size`，`hidden_norm` 再做 RMSNorm 归一化。投影后的 `target_hidden` 形状变成 `(batch, ctx, hidden_size)`，供每一层的注意力当 context 使用。
- `position_embeddings = self.rotary_emb(hidden_states, position_ids)`：用草稿自己的旋转位置编码算位置嵌入，注意它是基于 `hidden_states`（噪声嵌入）的形状来算的。
- `for layer in self.layers:`：每个 `Qwen3DFlashDecoderLayer` 同时接收 `hidden_states`（本块噪声）和 `target_hidden`（投影后的上下文）。**这两路如何在注意力里拼成 key/value** 是下一讲 u2-l3 的主题；本讲只确认「每层都同时拿到这两个输入」。
- `return self.norm(hidden_states)`：最终归一化后返回。注意返回的是**隐藏状态**，不是 logits——所以 `dflash_generate` 里要再接一个 `target.lm_head(...)`（[块起草段 L112-L119](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L112-L119)）。

再看一眼调用方（u2-l1 已讲，这里只关注与本讲的衔接）：在 [块起草段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L124)，`model(target_hidden=target_hidden, noise_embedding=noise_embedding, ...)` 的返回值被 `[:, 1 - block_size :, :]` 切片后喂给 `target.lm_head`。这正好印证：`forward` 输出形状是 `(1, B, hidden_size)`，切片丢掉第一个位置（锚点）、保留后 `B-1` 个，再过 lm_head 得到草稿 logits。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪 `forward` 内部两路张量的形状变化，验证 `fc` 的「翻译」作用。

**步骤**：

1. 打开 [forward](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L323-L347)，在脑中（或纸上）给每个中间量标形状。假设 `hidden_size=H`、`block_size=B`、ctx 长度 `C`、取了 `T` 层 target。
2. 回答：
   - `noise_embedding` 的形状？→ `(1, B, H)`
   - 进 `forward` 前 `target_hidden`（由 `extract_context_feature` 拼好）的形状？→ `(1, C, T*H)`
   - `self.fc(target_hidden)` 之后？→ `(1, C, H)`
   - `return self.norm(hidden_states)` 的形状？→ `(1, B, H)`
3. 解释：为什么 `target_hidden` 进 `fc` 前是 `T*H` 宽，出来变成 `H` 宽？

**预期结果**：`fc` 把「多层拼接的宽表示」线性压回草稿能吃的 `H` 宽，这正是 2.2 说的「target 与 draft 表示空间之间的翻译器」。无需运行即可推理得出。

**进阶（需 GPU）**：在 `target_hidden = self.hidden_norm(self.fc(target_hidden))` 这一行**之后**插一句打印（示例代码，非项目原有）：

```python
print(f"[forward] fc out shape = {target_hidden.shape}")  # 期望 (1, C, H)
```

跑一次小生成，确认形状与你手算一致。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**Q1**：`forward` 的第一行是 `hidden_states = noise_embedding`，而不是 `self.embed_tokens(input_ids)`。这说明了什么？

**答案**：草稿模型没有自己的嵌入层（`__init__` 里也确实没有 `embed_tokens`）。它直接消费外部（target 的 `embed_tokens`）算好的嵌入，这是 u2-l1 说的「第一次借」在代码里的直接体现。这也意味着 `forward` 的输入是已经嵌入好的 `noise_embedding`，而不是 token id。

**Q2**：`return self.norm(hidden_states)` 返回的是 logits 吗？如果不是，那 logits 在哪产生？

**答案**：不是。它返回的是**归一化后的隐藏状态**（`CausalLMOutputWithPast` 的注解与实现不完全一致，以函数体为准）。logits 在调用方由 **target 的 `lm_head`** 产生——见 [块起草段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L112-L119) 的 `target.lm_head(model(...))`，这是「第二次借」。

**Q3**：如果把 `self.fc` 的输入维度误设成 `hidden_size`（而不是 `T*hidden_size`），运行时会怎样？

**答案**：`fc(target_hidden)` 会因维度不匹配报错。因为 `target_hidden` 是 `extract_context_feature` 沿特征维拼接 `T` 层得到的，宽度是 `T*hidden_size`；`fc` 的输入维度必须与之严格相等。这正是「三者维度一致」约束的体现。

## 5. 综合实践

把三个最小模块串起来，完成规格要求的主实践：**读懂一个真实草稿模型的 config，并用公式复算 `target_layer_ids` 与 config 对比**。

**任务**：读取 `z-lab/Qwen3-8B-DFlash-b16`（或任意一个带 `-DFlash` 的草稿模型）的 `config.json`，找到 `num_target_layers`、`num_hidden_layers`、`block_size`、`target_layer_ids`，再用 `build_target_layer_ids` 复算并对比。

**操作步骤**：

1. **拿到 config.json**（无需 GPU，二选一）：
   - 在 Hugging Face 该模型页面的 Files 里直接查看 `config.json`；或
   - 用命令行下载（示例命令）：
     ```bash
     # 示例命令
     huggingface-cli download z-lab/Qwen3-8B-DFlash-b16 config.json --local-dir ./cfg_check
     ```
2. **找出四个字段**，记录它们的值：
   - 顶层：`num_hidden_layers`（草稿层数）、`num_target_layers`（target 层数）、`block_size`。
   - 在 `dflash_config` 字典里：`target_layer_ids`（若存在）、`mask_token_id`（若存在）。
3. **复算**。有两种方式：
   - **方式 A（需已装 transformers extra）**：`from dflash.model import build_target_layer_ids` 后调用 `build_target_layer_ids(num_target_layers, num_hidden_layers)`。注意 import `dflash.model` 会触发顶层的 `import torch` / `from transformers ...`，所以需要先 `uv pip install -e ".[transformers]"`。
   - **方式 B（无需任何依赖，推荐）**：把 [build_target_layer_ids](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L27-L36) 这 10 行函数原样抄进一个独立 `.py` 文件，直接调用：
     ```python
     # 示例代码 (从 dflash/model.py 抄出, 纯 Python 无依赖)
     def build_target_layer_ids(num_target_layers, num_draft_layers):
         if num_draft_layers == 1:
             return [num_target_layers // 2]
         start, end = 1, num_target_layers - 3
         span = end - start
         return [int(round(start + (i * span) / (num_draft_layers - 1)))
                 for i in range(num_draft_layers)]

     # 把下面两个数换成 config.json 里的真实值
     print(build_target_layer_ids(num_target_layers=<见config>, num_draft_layers=<见config>))
     ```
4. **对比**：把复算结果与 config 里 `dflash_config.target_layer_ids`（若存在）比较。

**需要观察并解释的现象**：

| 情况 | 现象 | 解释 |
|---|---|---|
| config **有** `target_layer_ids` | 复算值 == config 值 | 说明 config 里写死的值正是用同一条公式生成的；`__init__` 走 `dflash_config.get` 的「显式优先」分支，直接用 config 值 |
| config **没有** `target_layer_ids` | 模型加载后 `draft.target_layer_ids` == 复算值 | `__init__` 走兜底分支，调用 `build_target_layer_ids` 现算 |
| 二者不一致 | （理论上不应发生） | 提示该草稿可能手工指定了非均匀的层选择 |

**预期结果**：复算值与 config（或模型属性）一致；`block_size` 与模型名 `b16` 的 `16` 对应。具体数值「待本地验证」。

> 若你想顺带验证整条维度链：记下 `T = len(target_layer_ids)`，确认 `fc` 的输入维度（可在加载后看 `draft.fc.weight.shape[1]`）等于 `T * hidden_size`——这把本讲三个模块（`build_target_layer_ids` → `extract_context_feature` → `fc`）的维度一致性一次打通。

## 6. 本讲小结

- `DFlashDraftModel` 是个**小而残缺**的模型：有 `layers` / `norm` / `rotary_emb` / `fc` / `hidden_norm`，但**没有** `embed_tokens` 和 `lm_head`——这两个在生成时向 target 借（u2-l1 的两个「借」）。
- 它**复用 Qwen3 组件**（`Qwen3RMSNorm` / `Qwen3RotaryEmbedding` / `Qwen3MLP` / `Qwen3PreTrainedModel`），从而直接获得 `from_pretrained` 权重加载能力（细节在 u2-l5）。
- 配置上：`num_hidden_layers` 是**草稿自己的层数**（易混！），`num_target_layers` 是 **target 的层数**；`block_size` 放 config 顶层（必填），`target_layer_ids` 与 `mask_token_id` 放 `dflash_config` 字典里（可省略、有兜底）。
- `build_target_layer_ids` 在 target 深度上**等距采样**若干层，区间为 `[1, num_target_layers - 3]`（跳过最前 1 层、最后 3 层）；单层时取正中；用的是 Python `round`（银行家舍入）。
- `forward` 的核心一行是 `target_hidden = hidden_norm(fc(target_hidden))`：`fc` 把多层拼接的宽表示（`层数×hidden_size`）**投影压缩**回 `hidden_size`，是 target 与 draft 两种表示空间之间的「翻译器」。
- `forward` 返回的是**隐藏状态**（非 logits），之后由 `target.lm_head` 转 logits；`extract_context_feature` 的 `offset=1` 把「层下标」映射到隐藏状态元组的正确位置。

## 7. 下一步学习建议

本讲打开了草稿模型这个黑盒，但**故意把每一层内部当盒子**：`Qwen3DFlashDecoderLayer` 里的 `Qwen3DFlashAttention` 同时拿到 `hidden_states`（噪声）和 `target_hidden`（上下文）后，到底怎么把它们拼成注意力的 key/value、`is_causal=False` 为什么、`k_ctx`/`k_noise` 的拼接顺序意味着什么——这些是块扩散能并行起草的关键，正是下一讲 **u2-l3（DFlash 注意力与块扩散机制）** 的主题。

建议阅读顺序：

- **u2-l3**：拆开 `Qwen3DFlashAttention.forward`，看 target 上下文如何作为注意力的 key/value，噪声 token 如何参与注意力实现并行去噪；并细看 `extract_context_feature` 的多层拼接与 `offset`。
- **u2-l4**：回到 `dflash_generate` 的验证接受循环，细读 `sample()` 的 argmax / multinomial 分支与 KV cache 裁剪。
- **u2-l5**：把本讲提到的「复用 Qwen3 + `from_pretrained`」补全——`_no_split_modules` 的作用、flash_attention_2 / sdpa 的选择与回退。

读完 u2-l3，你就能完整解释「一块 mask token 是怎样在草稿模型内部、看着 target 的上下文，被并行还原成候选 token 的」——这正是块扩散相对逐 token 起草的优势所在。
