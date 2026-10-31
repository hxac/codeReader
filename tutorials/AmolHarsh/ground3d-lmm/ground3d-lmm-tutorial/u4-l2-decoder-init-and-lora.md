# Grounded_Decoder_Joint 初始化与 LoRA 配置

## 1. 本讲目标

上一讲（u4-l1）我们看到了点云特征是**如何被塞进** Qwen3-VL 的——一个挂在 `language_model` 上的前向预钩子把 `point_embeds` 用 `masked_scatter` 覆盖到 `<|point|>` 位置。但有一个关键问题当时被跳过了：`point_embeds` 本身是从哪来的？

本讲回答这个问题。我们聚焦 `Grounded_Decoder_Joint` 这个类的 `__init__`，拆解它的「出生过程」。学完本讲，你应该能够：

1. 说出 **point2token** 与 **query_proj** 两座投影桥的维度变换：256 维超点特征如何升到 LLM 的 hidden_size（如 2560），`<SEG>` 位置的隐状态又如何降回 256 维变成分割查询。
2. 解释 `_init_reason_model` 的完整初始化链：加载底座 → 冻结 → 注册 6 个特殊 token → 词表扩容 → LoRA 注入 → 参数统计。
3. 解释 `get_target_modules` 如何用 `lora_type`（如 `qkvo_all`）这个字符串筛选出要注入 LoRA 的层。
4. 亲手统计（或估算）LoRA 可训练参数占比，并解释 README 里「~400M LoRA-trainable」的主要来源。
5. 说明 FPSAggregator / FPSPositionAggregator 两个聚合器的作用：训练时超点数超过 `max_point_tokens` 就用最远点采样降采样。

## 2. 前置知识

### 2.1 LoRA：低秩适配器微调

全参微调一个 4B 模型，既要存一份完整梯度，也要为每个权重存优化器状态，显存开销巨大。**LoRA**（Low-Rank Adaptation）的思路是：冻结原权重 \( W_0 \)，只在旁边加一条低秩旁路：

\[
W' = W_0 + \frac{\alpha}{r} \cdot B A,\qquad A \in \mathbb{R}^{r \times d_{in}},\ B \in \mathbb{R}^{d_{out} \times r}
\]

- \( r \)（`lora_r`）：秩，旁路的「宽度」。本项目 Joint 变体用 r=16。
- \( \alpha \)（`lora_alpha`）：缩放系数，实际缩放倍率是 \( \alpha / r \)。Joint 变体 α=32，即缩放 2 倍。
- 每个被注入的线性层新增可训练参数 \( r \,(d_{in} + d_{out}) \) 个，远小于 \( d_{in} \times d_{out} \)。
- `lora_dropout`：旁路输入的 dropout；`lora_bias='none'` 表示不训练偏置。

实现层面，LoRA 由 HuggingFace 的 **peft** 库完成：传入一个 `LoraConfig`（指明 r、α、目标模块名列表），`get_peft_model` 会把目标 `nn.Linear` 替换成带旁路的 LoRA 层，并把非目标参数全部冻结。

### 2.2 RMSNorm

RMSNorm 是 LayerNorm 的简化版：不减均值，只用均方根归一化后乘一个可学习的缩放向量。Qwen 系列模型内部的 norm 层都是 RMSNorm。本项目中，两座投影桥的**入口处**各放了一个与底座同款实现的 RMSNorm（`Qwen3VLTextRMSNorm` / `Qwen2RMSNorm`），注意它们是**新建的**（权重从 1 开始训练），并不是从底座里搬出来的。

### 2.3 最远点采样（FPS）

FPS 是点云领域的经典降采样：每次从剩余点里挑出「离已选集合最近距离最大」的那个点，重复 K 次。形式化地，设已选集合为 \( S_k \)，下一个选点为：

\[
i_{k+1} = \arg\max_{i \notin S_k} \; \min_{j \in S_k} d(\mathbf{x}_i, \mathbf{x}_j)
\]

直觉：保证采样点尽量「铺开」覆盖整个点云，而不是聚在一起。

### 2.4 承接前两讲的认知

- u3-l3：`extract_feat` 用 scatter_mean 把逐体素特征池化成**逐超点特征**（每个场景 M 个、32 通道），并算出超点质心 `point_positions`。
- u4-l1：`PointQwen3VLForConditionalGeneration` 通过前向预钩子接收 `point_embeds`，其行数必须与文本里 `<|point|>` 占位符个数严格相等——这就是本讲聚合器存在的理由：**占位符个数是可数的，超点数是不可控的**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器主体 | `Grounded_Decoder_Joint.__init__`、`_init_reason_model`、`get_target_modules`、四个聚合器类 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置 | `model.decoder` 一段的所有 LoRA / 底座 / token 预算旋钮 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 提示词与特殊 token 常量 | `SEG_TOKEN`、`POINT_TOKEN` 的定义 |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | 点云注入钩子（上一讲） | 消费本讲产出的 `point_embeds` 与 `config.point_token_id` |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 项目说明 | 模型动物园中两个变体的 LoRA (r/α) 与 ~400M 可训练参数的说法 |

一个提醒：`query_decoder.py` 里的 `Grounded_Decoder_Eval`（[uniseg3d/query_decoder.py:1981-1982](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1981-L1982)）几乎逐行复制了本讲要讲的 `__init__`、`_init_reason_model`、`get_target_modules`（见 [uniseg3d/query_decoder.py:2064-2152](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2064-L2152)）。读懂数据流后，两份代码可以对照着看。

## 4. 核心概念与源码讲解

### 4.1 point2token / query_proj：两座维度桥

#### 4.1.1 概念说明

整个 Ground3D-LMM 有两套「语言」：

- **3D 侧**（分割解码器）说 256 维语言（`d_model=256`）；
- **语言侧**（Qwen3-VL）说 hidden_size 维语言（Qwen3-VL-4B 的 `text_config.hidden_size`，以本地加载的 config 为准，下文以 2560 为例）。

超点特征要从 3D 侧进入语言侧，`<SEG>` 隐状态要从语言侧回到 3D 侧，因此需要两座方向相反的桥：

| 桥 | 方向 | 维度变换 | 作用 |
| --- | --- | --- | --- |
| `point2token` | 3D → LLM | (M, 256) → (M, 2560) | 把超点特征变成 `<\|point\|>` 位置的输入嵌入 |
| `query_proj` | LLM → 3D | (n_q, 2560) → (n_q, 256) | 把 `<SEG>` 位置隐状态变成分割解码器的查询 |

加上父类已有的两个 32→256 投影（`input_proj`、`x_mask`），一个超点特征在整个模型里的维度旅程是：

```
超点特征 (M, 32)                      ← extract_feat 的 scatter_mean 产出（u3-l3）
    │ x_mask: Linear(32→256)          ← 变成 mask_feats (M, 256)
    ├────────────────────────────► point2token (M,256→M,2560) ──► <|point|> 占位（u4-l1 钩子）
    │                                                        └─► Qwen3-VL 前向
    │                                                        └─► 最后层隐状态 (L, 2560)
    │                                                        └─► <SEG> 区间均值 (n_q, 2560)
    └──── query_proj (n_q,2560→n_q,256) ◄──────────────────────┘
         分割查询 → 6 层解码器（u5-l1）
```

#### 4.1.2 核心流程

`Grounded_Decoder_Joint.__init__` 的执行顺序：

1. 调用父类 `QueryDecoder.__init__`，构建 6 层注意力解码器骨架，并（因 `num_instance_queries=0`）先建一个 32→256 的 `query_proj`；
2. 处理 `save_pred_qa_dir` 输出目录；
3. 调 `_init_reason_model`（本讲 4.2、4.3 详解），得到 `self.reason_model`（含 LoRA 的 Qwen3-VL）、`self.processor`、`self.reason_mode_config`；
4. 按 `base_model` 分支构建 `point2token`；
5. **覆盖**父类的 `query_proj`，换成 2560→256 版本；
6. 构建两个 FPS 聚合器（4.4 详解）。

#### 4.1.3 源码精读

先看构造函数签名与配置默认值（[uniseg3d/query_decoder.py:884-899](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L884-L899)）：函数参数的默认值（`lora_r=128`、`base_model='qwen2_5_vl'`、`max_point_tokens=2500`）会被配置文件覆盖，真正的取值以 [configs/ground3dlmm_train.py:47-78](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L78) 为准。

配置里 `num_instance_queries=0, num_semantic_queries=0`（[configs/ground3dlmm_train.py:50-51](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L50-L51)），这触发父类的两个分支（[uniseg3d/query_decoder.py:481-487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L481-L487)）：

- `self.query`（可学习查询 Embedding）**不创建**——本项目没有固定查询；
- 创建一个临时的 `query_proj: Linear(32→256)→ReLU→Linear(256→256)`——因为旧版 UniSeg3D 的查询来自 32 维超点特征。

随后子类把它整体覆盖（[uniseg3d/query_decoder.py:944-947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L944-L947)）：

```python
self.query_proj = nn.Sequential(
        Qwen3VLTextRMSNorm(self.reason_mode_config.text_config.hidden_size),
        nn.Linear(self.reason_mode_config.text_config.hidden_size, d_model), nn.GELU(),
        nn.Linear(d_model, d_model))
```

这段把 `query_proj` 换成「RMSNorm → Linear(2560→256) → GELU → Linear(256→256)」。因为现在的查询来源变了——不再是超点特征，而是 LMM 最后层隐状态（2560 维）。父类那个 32→256 版本成了「过渡化石」：被创建、随即被覆盖，参数从不参与前向。真正消费它的是父类的 `_get_queries`（[uniseg3d/query_decoder.py:513-536](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L513-L536)），其中第 534 行 `self.query_proj(queries[i])` 把每条 QA 的 `<SEG>` 隐状态投影成解码器查询。

`point2token` 按 `base_model` 分两支（[uniseg3d/query_decoder.py:923-941](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L923-L941)）。qwen3_vl 分支（[uniseg3d/query_decoder.py:937-939](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L937-L939)）：

```python
self.point2token = nn.Sequential(
    Qwen3VLTextRMSNorm(d_model), nn.Linear(d_model, d_model),
    nn.GELU(), nn.Linear(d_model, self.reason_mode_config.text_config.hidden_size))
```

即「RMSNorm → Linear(256→256) → GELU → Linear(256→2560)」。注意两点：

1. 它的输入是 **256 维**的 `mask_feats`，不是 32 维原始超点特征——`forward_iter_pred` 先做 `mask_feats = [self.x_mask(y) for y in x]` 再传给 `infer_reason_train`（[uniseg3d/query_decoder.py:1867-1872](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1867-L1872)）。也就是说 `mask_feats` 一身兼两职：既是掩码预测的嵌入（u5-l1），又是 LMM 的点 token 来源。
2. `query_proj` 的 RMSNorm 固定用 `Qwen3VLTextRMSNorm`，**不随** `base_model` 分支切换（对比可见 qwen2_5_vl 分支的 `point2token` 用的是 `Qwen2RMSNorm`）。若真用 qwen2_5_vl 底座，两座桥会混用两种 RMSNorm 实现——这是一个值得注意的实现细节。

被注释掉的 `token2point`（[uniseg3d/query_decoder.py:932-935](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L932-L935)）是历史路线的遗迹：早期想再训一个 2560→256 的反向投影，后来被「`<SEG>` 隐状态 + query_proj」方案取代。

#### 4.1.4 代码实践

**实践目标**：不下载 4B 权重，亲手验证两座桥的形状契约。

**操作步骤**（以下为示例代码，非项目原有代码；只需 PyTorch，无需 GPU）：

```python
# 示例代码：按源码结构重建两座投影桥，验证维度
import torch, torch.nn as nn
from transformers.models.qwen3_vl.modeling_qwen3_vl import Qwen3VLTextRMSNorm

d_model, hidden = 256, 2560          # hidden 以本地 Qwen3-VL-4B config 为准

point2token = nn.Sequential(          # 对应 query_decoder.py L937-939
    Qwen3VLTextRMSNorm(d_model), nn.Linear(d_model, d_model),
    nn.GELU(), nn.Linear(d_model, hidden))
query_proj = nn.Sequential(           # 对应 query_decoder.py L944-947
    Qwen3VLTextRMSNorm(hidden),
    nn.Linear(hidden, d_model), nn.GELU(),
    nn.Linear(d_model, d_model))

superpoint_feats = torch.randn(3570, d_model)   # 某场景 3570 个超点
point_embeds = point2token(superpoint_feats)
seg_hidden = torch.randn(2, hidden)             # 某条 QA 里 2 个 <SEG>
queries = query_proj(seg_hidden)
print(point_embeds.shape, queries.shape)
print('point2token 参数量:', sum(p.numel() for p in point2token.parameters()))
```

**需要观察的现象**：两个输出的形状；`point2token` 的参数量约 256×256 + 256×2560 ≈ 0.72M 量级。

**预期结果**：`point_embeds` 形状为 `torch.Size([3570, 2560])`，`queries` 形状为 `torch.Size([2, 256])`；`point_embeds.shape[0]` 恰好等于文本中 `<|point|>` 占位符个数（u4-l1 钩子的数量契约）。若 `transformers` 版本无 `Qwen3VLTextRMSNorm`，可先用 `nn.RMSNorm(d_model)` 等价替换验证形状，完整复现需按 u1-l2 装好锁定版本的环境（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `point2token` 的输入是 256 维而不是 32 维？32→256 的那一跳在哪里发生？

**答案**：`forward_iter_pred` 里 `self.x_mask(y)`（Linear(32→256)，[uniseg3d/query_decoder.py:507-509](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L507-L509)）已经把超点特征升到 256 维；`infer_reason_train` 接收的 `point_feats` 就是这份 `mask_feats`。`point2token` 因此按 `d_model=256` 设计。

**练习 2**：父类在 [uniseg3d/query_decoder.py:484-487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L484-L487) 创建的 `query_proj` 会被子类覆盖。既然如此，这段「无效构建」有什么副作用？

**答案**：（1）白白多创建一次模块对象（随后被垃圾回收，参数不进 state_dict，因为属性被覆盖）；（2）说明该类是从「查询=超点特征」的旧设计（UniSeg3D）演化到「查询=LMM 隐状态」的新设计的过渡产物。严格说被覆盖模块不会再出现在 `named_parameters()` 中，所以不影响训练，只影响代码可读性。

**练习 3**：如果把 `d_model` 从 256 改成 512，需要同步改配置里的哪些字段才能保持模型自洽？

**答案**：`model.decoder.d_model`（以及隐式依赖它的 `hidden_dim`、`num_heads` 需整除关系成立）；同时预训练 backbone 输出仍是 32 维（`in_channels`），`point2token`/`query_proj`/`x_mask`/`input_proj` 会随之自动改形状，但 **checkpoint 无法直接加载**（形状不匹配的权重会被跳过或报错），等于重新训练解码器部分。

### 4.2 `_init_reason_model` 与特殊 token 词表扩容

#### 4.2.1 概念说明

`_init_reason_model` 是解码器的「下半场初始化」：把一个冻结的 Qwen3-VL 变成能接收点云、能输出接地标记的 LMM。它解决三个问题：

1. **加载**：从 HF hub 拉起 `PointQwen3VLForConditionalGeneration`（u4-l1 讲过的钩子版底座）、config 和 processor；
2. **扩词表**：登记 6 个本项目专属的特殊 token，并把嵌入矩阵相应加行；
3. **注入 LoRA**：让冻结的底座重新可训（4.3 详解）。

特殊 token 的定义在 [uniseg3d/prompt_template.py:3-4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L3-L4)：`POINT_TOKEN = ["<|point|>"]`（1 个），`SEG_TOKEN = ["<p>", "</p>", "<SEG>", "<name>", "<sentence>"]`（5 个），合计 6 个。它们必须进词表，原因有二：不被 tokenizer 切碎（`<|point|>` 若被拆成 `<`、`|`、`point` 等多个普通 token，钩子的「按 token id 定位占位」就失效了）；每个新 token 需要自己的嵌入向量。

#### 4.2.2 核心流程

```
from_pretrained 加载 reason_model / config / processor（bf16）
        │
requires_grad_(False)          ← 先整体冻结
        │
tokenizer.add_special_tokens(6 个新 token) → new_tokens
        │
if new_tokens > 0 且 词表变长:
        resize_token_embeddings(len(tokenizer))    ← 嵌入矩阵 +6 行
        新行 ← 旧嵌入的均值初始化（输入嵌入与输出嵌入各一份）
        │
记录 seg_tokens_ids / point_token_id
把 point_token_id 写进 reason_model.config      ← 供 u4-l1 的前向重算 point_mask
```

关键点：`new_tokens > 0` 这个判断是一个**幂等性守卫**——tokenizer 里这些 token 若已存在（例如某些流程重复初始化），`add_special_tokens` 返回 0，就不会重复扩容、重复加行。

#### 4.2.3 源码精读

加载分支按 `base_model` 二选一（[uniseg3d/query_decoder.py:959-976](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L959-L976)）。qwen3_vl 路线加载 `PointQwen3VLForConditionalGeneration`（bfloat16）与 `PatchedQwen3VLProcessor`，并取 `text_config.vocab_size` 作为原始词表大小；qwen2_5_vl 路线则对应 `PointQwen2_5_VLForConditionalGeneration` 与 `Qwen2_5_VLProcessor`，取 `config.vocab_size`。

扩容的核心六行（[uniseg3d/query_decoder.py:984-991](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L984-L991)）：

```python
new_tokens = self.processor.tokenizer.add_special_tokens(dict(additional_special_tokens=special_tokens))
if new_tokens > 0 and len(self.processor.tokenizer) > vocab_size:
    MMLogger.get_current_instance().info(f'Expanding vocab size: {vocab_size} -> {len(self.processor.tokenizer)}')
    self.reason_model.resize_token_embeddings(len(self.processor.tokenizer))
    i_emb = self.reason_model.get_input_embeddings().weight.data
    o_emb = self.reason_model.get_output_embeddings().weight.data
    i_emb[-new_tokens:] = i_emb[:-new_tokens].mean(0, keepdim=True)
    o_emb[-new_tokens:] = o_emb[:-new_tokens].mean(0, keepdim=True)
```

逐行解读：

- `add_special_tokens` 返回**真正新增**的 token 数（已存在的不计）；
- `resize_token_embeddings` 把输入嵌入与 lm_head（输出嵌入）都扩到新长度，新行默认随机初始化；
- 随后两行把新行改写为**旧嵌入的均值**——`i_emb[:-new_tokens]` 是扩容前的全部行，`mean(0)` 得到一个「平均 token」向量。这样 `<SEG>`、`<|point|>` 等新 token 从一个中性、数值稳定的起点开始训练，而不是随机噪声。

id 登记与 config 回写（[uniseg3d/query_decoder.py:993-996](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L993-L996)）：`seg_tokens_ids` 供 4.4 之后的 `get_query_tokens*` 系列定位 `<p>`/`</p>`；`self.reason_model.config.point_token_id = ...` 这一行是本讲与 u4-l1 的接合点——`PointQwen3VL` 的前向正是用 `input_ids == self.config.point_token_id` 重算 `point_mask`（见 [uniseg3d/point_qwen3_vl.py:125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L125)）。

注意整体冻结发生在扩容**之前**（[uniseg3d/query_decoder.py:978](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L978)：`self.reason_model.requires_grad_(False)`），冻结之后新 token 的嵌入行也是不可训的——这正是 4.3 里解冻循环存在的理由之一。

#### 4.2.4 代码实践

**实践目标**：验证 `add_special_tokens` 的返回值语义与 id 分配，理解幂等性守卫。

**操作步骤**（示例代码；只需 `transformers`，需联网拉取 tokenizer，约几 MB）：

```python
# 示例代码：复现词表扩容行为（不加载 4B 权重，只加载 tokenizer）
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained('Qwen/Qwen3-VL-4B-Instruct')

SPECIAL = ["<p>", "</p>", "<SEG>", "<name>", "<sentence>", "<|point|>"]
n1 = tok.add_special_tokens(dict(additional_special_tokens=SPECIAL))
n2 = tok.add_special_tokens(dict(additional_special_tokens=SPECIAL))   # 再来一次
print('第一次新增:', n1, ' 第二次新增:', n2, ' 词表长度:', len(tok))
print('<|point|> id =', tok.convert_tokens_to_ids('<|point|>'))
print('<p> id =', tok.convert_tokens_to_ids('<p>'), '</p> id =', tok.convert_tokens_to_ids('</p>'))
```

**需要观察的现象**：第一次返回 6；第二次返回 0（全部已存在）；新 token 的 id 是否都 ≥ 原始词表长度。

**预期结果**：`n1=6, n2=0`；新 token id 连续排在词表尾部（这正是 `i_emb[-new_tokens:]` 能精确命中的前提）。若网络不可用，可改用本地已下载的模型目录路径；无法下载时标注待本地验证，仅阅读 [uniseg3d/query_decoder.py:984-991](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L984-L991) 理解逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么新 token 的嵌入要用「旧嵌入均值」初始化，而不是保持 `resize_token_embeddings` 的随机初始化？

**答案**：随机初始化的行向量数值尺度可能与旧嵌入分布差异很大，训练初期会扰动 softmax 输出（尤其 lm_head 的新行直接产生 logits）。均值向量处于旧嵌入分布的中心，是一个「中性」起点，让新 token 从与旧 token 相近的数值尺度开始学习，训练更稳。

**练习 2**：`resize_token_embeddings` 之后，模型里哪两个矩阵的形状变了？这两个矩阵在 4.3 的解冻循环里会被如何处理？

**答案**：输入嵌入矩阵（`embed_tokens` 的 weight）和输出嵌入矩阵（`lm_head` 的 weight），各加 6 行。解冻循环里凡名字含 `embed_tokens` 或 `lm_head` 的参数被设为 `requires_grad=True`——即**整个**嵌入表和 lm_head（不止新 6 行）都变为可训练。

**练习 3**：如果 tokenizer 对 `<|point|>` 已经原生认识（假设未来 Qwen 版本内置了它），这段代码会发生什么？

**答案**：`add_special_tokens` 返回的 `new_tokens` 会小于 6（只统计真正新增的），若全部已存在则返回 0，扩容分支整体跳过，`resize_token_embeddings` 不执行；`convert_tokens_to_ids` 仍能拿到原生 id，后续流程照常。

### 4.3 LoRA 注入与可训练参数统计

#### 4.3.1 概念说明

冻结的 4B 底座需要两条「可训练通道」：一是 LoRA 旁路（适配注意力层的行为），二是词表扩容后的嵌入层（新 token 必须学出来）。本模块讲 `lora_type` 字符串如何变成一张注入名单、`get_peft_model` 如何执行注入、注入后又如何用一个小循环做「二次修剪」，最后看参数统计日志。

#### 4.3.2 核心流程

```
lora_type = 'qkvo_all'      ← 配置字符串，格式为 '<层类型>_<范围>'
        │ split('_')
layer_type='qkvo'  modules='all'
        │ 遍历 reason_model.named_modules()，按三条规则过滤
target_modules = [...]       ← 注入名单（模块全名字符串列表）
        │
LoraConfig(task_type='CAUSAL_LM', r=16, lora_alpha=32, ...)
        │
get_peft_model(reason_model, lora_config)   ← peft 注入：换层 + 冻结基座 + 打开旁路梯度
        │
二次修剪循环：
    名字含 embed_tokens/lm_head → requires_grad=True   （嵌入可训）
    名字含 visual               → requires_grad=False  （视觉塔连 LoRA 也冻结）
        │
统计并打印：Total params / Learnable params / 占比
```

#### 4.3.3 源码精读

`get_target_modules` 全文只有二十行（[uniseg3d/query_decoder.py:1025-1047](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1025-L1047)）。核心是三张「筛子」：

```python
layer_type, modules = lora_type.split('_')
assert layer_type in ('qkvo', 'linear') and modules in ('llm', 'visual', 'all')

qkvo_keys = ['q_proj', 'k_proj', 'v_proj', 'o_proj', 'attn.qkv', 'attn.proj']

target_modules = []
for n, m in model.named_modules():
    if modules == 'llm' and 'visual' in n:        # 筛子1：范围
        continue
    if modules == 'visual' and 'visual' not in n: # 筛子1：范围（另一方向）
        continue
    if layer_type == 'qkvo' and not any(n.endswith(k) for k in qkvo_keys):
        continue                                   # 筛子2：层名后缀
    if n in target_modules:
        continue
    target_modules.append(n)
```

- `modules`（第二段）决定**范围**：`llm` 只留语言塔、`visual` 只留视觉塔、`all` 全留。判据很朴素——名字里含不含 `visual`。
- `layer_type`（第一段）决定**层型**：`qkvo` 只留名字以注意力投影结尾的层。`qkvo_keys` 同时覆盖了两种命名习惯：语言塔的 `q_proj/k_proj/v_proj/o_proj`，和 Qwen-VL 视觉块注意力的 `attn.qkv/attn.proj`。若选 `linear` 则跳过后缀筛子，所有模块名都进名单（peft 实际只会对它能包装的层生效，行为以所装 peft 版本为准，待本地验证）。
- 传入的 `target_modules` 是**模块全名列表**而非后缀正则——peft 两种都支持，这里选择了精确名单。

注入与配置构造（[uniseg3d/query_decoder.py:998-1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L998-L1008)）：`LoraConfig(task_type='CAUSAL_LM', r=lora_r, lora_alpha=lora_alpha, lora_dropout=lora_dropout, bias=lora_bias, target_modules=target_modules)`，然后 `self.reason_model = get_peft_model(self.reason_model, lora_config)`——注意 `reason_model` 被整体**替换**为 peft 包装后的 `PeftModel`。

二次修剪循环（[uniseg3d/query_decoder.py:1010-1014](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1010-L1014)）：

```python
for name, param in self.reason_model.named_parameters():
    if any(k in name for k in ('embed_tokens', 'lm_head')):
        param.requires_grad = True
    elif 'visual' in name:
        param.requires_grad = False
```

这两个分支各自修正 peft 默认行为：

- **嵌入解冻**：4.2 扩容的新行初始值是均值，必须可训；代码顺手把整张 `embed_tokens` 与 `lm_head` 都解冻了（不只 6 行）。
- **视觉塔全冻**：配置用的是 `qkvo_all`，名单里含视觉塔的 `attn.qkv/attn.proj`，peft 会给它们挂上旁路；这个循环把名字含 `visual` 的**一切参数**（包括刚挂上的 LoRA 旁路）重新冻结。净效果：视觉塔零可训练参数，LoRA 实际只在语言塔注意力上生效。

参数统计（[uniseg3d/query_decoder.py:1018-1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1018-L1021)）把 `Total params / Learnable params (ratio%)` 打进日志。配置侧的旋钮在 [configs/ground3dlmm_train.py:68-75](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L68-L75)：训练用 `qwen_model_path='Qwen/Qwen3-VL-4B-Instruct'`、`lora_type='qkvo_all'`（注释还留了一句 "reduce LoRA target modules"）、`lora_r=16, lora_alpha=32, lora_dropout=0.1, lora_bias='none'`、`base_model='qwen3_vl'`。README 的模型动物园（[README.md:204-212](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L204-L212)）进一步说明：Joint 变体 r=16/α=32，3D-only 变体 r=32/α=64，底座 4.47B 参数中约 400M 可训练。

最后补一句训练配置的呼应：优化器给名字含 `reason` 的参数设了 `lr_mult=0.1`（[configs/ground3dlmm_train.py:200-206](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L200-L206)），即 LMM 侧（`decoder.reason_model.*`）以 0.1 倍学习率慢调，backbone 0.01 倍，解码器本体 1.0 倍——「谁预训练得越充分，谁动得越小心」。

#### 4.3.4 代码实践

**实践目标**：不加载 4B 权重，用一棵假模块树复现 `get_target_modules` 的筛选结果，并估算 LoRA 参数量。

**操作步骤**（示例代码；依赖 u1-l2 环境装好的 torch/transformers，无需 GPU、无需下载权重）：

```python
# 示例代码：借道类方法复现筛选逻辑
# get_target_modules 方法体内并没有用到 self，因此可以传 None 直接调用
import torch.nn as nn
from uniseg3d.query_decoder import Grounded_Decoder_Joint

class FakeAttn(nn.Module):
    def __init__(self):
        super().__init__()
        self.q_proj = nn.Linear(8, 8); self.k_proj = nn.Linear(8, 8)
        self.v_proj = nn.Linear(8, 8); self.o_proj = nn.Linear(8, 8)

class FakeBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.self_attn = FakeAttn()
        self.mlp_gate_proj = nn.Linear(8, 16)      # 非 qkvo 层，应被过滤
        self.norm = nn.LayerNorm(8)

class FakeModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.model = nn.Module()
        self.model.language_model = nn.ModuleList([FakeBlock() for _ in range(2)])
        self.model.visual = nn.ModuleList([FakeBlock() for _ in range(2)])

m = FakeModel()
for lora_type in ('qkvo_all', 'qkvo_llm', 'qkvo_visual'):
    tm = Grounded_Decoder_Joint.get_target_modules(None, m, lora_type, 'qwen3_vl')
    print(f'{lora_type:12s} -> {len(tm):2d} 个目标: {tm}')
```

**需要观察的现象**：三种 `lora_type` 的目标数量差异；`mlp_gate_proj` 和 `norm` 是否出现在名单里；视觉塔模块名（含 `visual`）在 `qkvo_llm` 下是否被剔除。

**预期结果**（以这棵 2+2 层假树为例）：`qkvo_all` 返回 16 个（2 语言块 + 2 视觉块，各 4 个投影）；`qkvo_llm` 返回 8 个（只剩 `model.language_model.*.self_attn.*_proj`）；`qkvo_visual` 返回 8 个（只剩 `model.visual.*`）。`norm`、`mlp_gate_proj` 一律不在名单。若本地 peft 环境可用，还可以把 `tm` 交给 `LoraConfig(r=16, ...)` 包一个小模型，数一数 `lora_A/lora_B` 参数个数验证 \( \sum r(d_{in}+d_{out}) \) 公式（待本地验证）。

接着做**纸面估算**（练习性质）：设 Qwen3-VL-4B 语言塔共 \( L \) 层（以本地 config 为准），注意力投影的输入输出维为 \( d_{in}, d_{out} \)，则语言塔 qkvo 的 LoRA 参数量为

\[
P_{\text{LoRA}} = L \sum_{\text{q,k,v,o}} r\,(d_{in} + d_{out})
\]

代入 Qwen3-VL-4B 的大致规模（L≈36，hidden≈2560，各投影维度以 config 为准）算出的量级只有**千万级**；而 README 写的是 ~400M 可训练——差额主要来自被解冻的**整张嵌入表与 lm_head**（词表约 15 万行 × 2560 维 ≈ 3.9 亿参数量级）。结论：本项目的「LoRA-trainable」大头其实是嵌入层，不是 LoRA 旁路本身。这个结论请用下面的真实日志核实（待本地验证）。

**观察真实日志**：任何一次训练或评估构建模型时，日志会依次打印三行（对应 [uniseg3d/query_decoder.py:986](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L986)、[L999](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L999)、[L1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1021)）：`Expanding vocab size: ...`、`LoRA target modules: [...]`、`Total params: ... Learnable params: ... (...%)`。把 `LoRA target modules` 那行的名单长度与你假树的推算规则对照即可。

#### 4.3.5 小练习与答案

**练习 1**：`lora_type='qkvo_all'` 名义上同时覆盖语言塔与视觉塔，但训练后视觉塔权重有变化吗？

**答案**：没有。虽然 `all` 让视觉塔的 `attn.qkv/attn.proj` 进了注入名单、peft 也挂上了旁路，但 [uniseg3d/query_decoder.py:1010-1014](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1010-L1014) 的循环把一切名字含 `visual` 的参数（含旁路）重新冻结，且优化器不会收到它们的梯度，因此视觉塔完全保持预训练原样。

**练习 2**：为什么解冻循环用的是 `elif`？如果把 `elif 'visual' in name` 改成独立的 `if`，会出什么问题？

**答案**：`elif` 保证了 `embed_tokens/lm_head` 的优先级。Qwen-VL 的视觉塔里也有 patch embedding 之类的模块名，若某参数名同时含 `lm_head` 与 `visual`（或未来命名变化），独立 `if` 会先解冻再冻结，最终结果取决于语句顺序；用 `elif` 明确「嵌入优先于视觉冻结」这一意图，行为更可控。

**练习 3**：把 `lora_r` 从 16 调到 32（3D-only 变体正是 r=32/α=64），LoRA 可训练参数量大约变为几倍？α 为什么要同步翻倍？

**答案**：LoRA 参数与 r 成正比，约变为 2 倍（缩放系数 α/r 保持 32/16 = 64/32 = 2 不变）。α 同步翻倍是为了保持有效缩放倍率不变，让旁路输出的数值尺度不因加宽而漂移，训练动态更可比。

### 4.4 FPS / 位置 FPS 聚合器：给点 token 设预算

#### 4.4.1 概念说明

文本里放多少个 `<|point|>` 占位符，钩子就要求多少行 `point_embeds`——而场景超点数 M 由数据决定，可能上万。语言模型的序列长度直接决定计算与显存，因此训练时必须给点 token 设上限：`max_point_tokens`（训练配置 2000，见 [configs/ground3dlmm_train.py:76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76)）。

仓库在 [uniseg3d/query_decoder.py:87-289](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L87-L289) 定义了**四个**聚合器，但 `Grounded_Decoder_Joint.__init__` 只实例化了两个：

| 聚合器 | 距离依据 | 是否被实例化 |
| --- | --- | --- |
| `ConvSetAggregator` | 1D 卷积 + 自适应池化（无距离） | 否（定义未用） |
| `PositionSoftClusterAggregator` | 与可学习原型中心的位置距离软分配 | 否（定义未用） |
| `FPSAggregator` | 特征空间 L2 距离 | 是（`point_aggregator`） |
| `FPSPositionAggregator` | XYZ 物理位置距离 | 是（`pos_aggregator`，首选） |

两个 FPS 聚合器都是**零参数**模块（`in_channels/pos_dim/temperature` 形参只是保持接口兼容，构造函数里没用到），纯采样、不学习。

#### 4.4.2 核心流程

`infer_reason_train` 中每个场景的降采样决策（[uniseg3d/query_decoder.py:1360-1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1368)）：

```
仅训练态（self.training）且 M > max_point_tokens 时：
    若 point_positions 可用（u3-l3 算的超点质心）
        → pos_aggregator(feats, positions)   # 按物理位置 FPS
    否则
        → point_aggregator(feats)            # 按特征距离 FPS 兜底
随后 point2token(...) 得到 (≤2000, 2560) 的 point_embeds
```

三个要点：**只在训练时聚合**——`infer_reason_eval` 不做任何降采样，推理时全部超点都进 LMM（所以推理更准也更慢）；**首选位置 FPS**——物理空间铺开采样对「场景覆盖」更合理，特征 FPS 只是质心缺失时的兜底；**返回的是原特征的子集**（`feats[indices]`），不产生新表征。

FPS 算法本身（[uniseg3d/query_decoder.py:188-222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L188-L222)）：

```
预计算 N×N 距离矩阵 dist_matrix = cdist(points, points)
随机选第一个点 current
distances ← 全 inf
循环 num_samples-1 次：
    distances ← min(distances, dist_matrix[current])   # 每点到已选集的最近距离
    current ← argmax(distances)                        # 取离已选集最远者
```

注意实现取舍：先算**完整 N×N 距离矩阵**再逐步取行，内存 \( O(N^2) \)。对 N=几千的超点没问题；若哪天直接对几十万原始点用它会爆内存——这也是超点池化（u3-l3）先行的价值之一。

#### 4.4.3 源码精读

实例化代码（[uniseg3d/query_decoder.py:952-955](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L952-L955)）：

```python
# Learned aggregator to cap point tokens for reasoning
self.max_point_tokens = max_point_tokens
self.point_aggregator = FPSAggregator(in_channels=d_model, out_tokens=self.max_point_tokens)
self.pos_aggregator = FPSPositionAggregator(in_channels=d_model, out_tokens=self.max_point_tokens, pos_dim=3, temperature=0.5)
```

两个聚合器的 `forward` 形状契约一致：入参 `(N, D)`（`FPSPositionAggregator` 再加 `(N, P)` 位置），出参 `(min(N, out_tokens), D)`；`n <= out_tokens` 时原样返回（[uniseg3d/query_decoder.py:181-186](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L181-L186) 与 [uniseg3d/query_decoder.py:247-253](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L247-L253)）。二者唯一的实质差别是 `fps_sample` 收到的张量：`FPSAggregator` 传特征 `x`（[uniseg3d/query_decoder.py:184-186](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L184-L186)），`FPSPositionAggregator` 传位置 `pos`（[uniseg3d/query_decoder.py:251-253](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L251-L253)）——采样依据不同，采样结果都用于索引特征。

调用侧（[uniseg3d/query_decoder.py:1360-1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1370)）：

```python
if self.training and n_points > self.max_point_tokens:
    if i_point_pos is not None and i_point_pos.shape[0] == n_points:
        i_point_feat = self.pos_aggregator(i_point_feat, i_point_pos)
        reason_str = 'pos-based FPS aggregator'
    else:
        i_point_feat = self.point_aggregator(i_point_feat)
        reason_str = 'feature-based FPS aggregator'
    n_points = i_point_feat.shape[0]

point_embeds = self.point2token(i_point_feat).to(self.reason_model.device)  # (N, D)
```

聚合后 `n_points` 被更新，随后拼 prompt 时 `<|point|>` 的重复次数用的正是这个数（u4-l3 将展开 prompt 拼接细节），保证「占位符个数 = 特征行数」的钩子契约成立。

#### 4.4.4 代码实践

**实践目标**：验证两个 FPS 聚合器的形状契约、早退分支与采样分布。

**操作步骤**（示例代码；在仓库根目录、u1-l2 环境下运行；导入 `uniseg3d.query_decoder` 需要 transformers/peft 等，但**不会**触发 4B 权重下载）：

```python
# 示例代码：直接使用仓库里的聚合器类
import torch
from uniseg3d.query_decoder import FPSAggregator, FPSPositionAggregator

agg_f = FPSAggregator(in_channels=256, out_tokens=500)
agg_p = FPSPositionAggregator(in_channels=256, out_tokens=500, pos_dim=3)

feats = torch.randn(3000, 256)          # 3000 个超点特征
pos   = torch.randn(3000, 3)            # 对应质心

out_f = agg_f(feats)
out_p = agg_p(feats, pos)
print('特征 FPS:', out_f.shape, ' 位置 FPS:', out_p.shape)

small = torch.randn(300, 256)
print('不足上限时:', agg_f(small).shape)          # 早退分支
print('FPS 零参数:', sum(p.numel() for p in agg_p.parameters()))
```

**需要观察的现象**：两个输出形状；不足上限时是否原样返回（可用 `(agg_f(small) == small).all()` 验证）；参数量是否为 0。

**预期结果**：两个输出均为 `torch.Size([500, 256])`；300 个点时输出 `(300, 256)` 且与输入完全相同；参数量为 0。另可把 `pos` 换成两团聚在一起的点云（如两簇高斯），观察位置 FPS 选出的索引在两簇间均衡分布——这正是「铺开覆盖」的直观体现（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么推理（`infer_reason_eval`）不做 FPS 聚合？这样做的代价与收益是什么？

**答案**：推理追求精度，序列长度只影响单场景耗时，不产生梯度与优化器开销，所以让全部超点进 LMM 换取更完整的场景信息；代价是显存与耗时随 M 线性增长（注意力则是随序列长度平方）。训练时则必须省——batch 内多个长序列会迅速吃满显存。

**练习 2**：`FPSAggregator` 的距离矩阵是 `torch.cdist(x, x)`，形状 N×N。若某场景 M=8000 个超点、特征 256 维、bfloat16 存储，这个矩阵大约占多少显存？

**答案**：\( 8000 \times 8000 \times 2 \) 字节 ≈ 1.22 亿字节 ≈ 122 MB（还要乘上中间计算的精度，实际峰值更高）。说明该实现的适用规模是「几千」级——恰好与超点数（而非原始点数）匹配，这也是为什么聚合器放在超点池化之后。

**练习 3**：`ConvSetAggregator` 和 `PositionSoftClusterAggregator` 在仓库里定义了却没被实例化。这类「死代码」对一个想做二次开发的读者有什么价值？

**答案**：它们是备选方案的存档——卷积+池化式聚合（与顺序无关性弱一些但可学习）和可学习原型软聚类（能产生「新」表征而非子集）。如果你想试验「可学习的 token 压缩」，这两个类提供了与 FPS 聚合器**相同调用约定**的现成起点，改 `__init__` 里一行实例化即可接入。

## 5. 综合实践

**任务：写一份「解码器初始化体检报告」。** 不加载 4B 权重、不跑训练，仅凭源码与轻量脚本完成以下三件事，并把结果整理成一张表：

1. **维度契约表**：沿 4.1 的实践，列出张量从 `(M, 32)` 超点特征到 `(n_q, 256)` 分割查询途经的每次形状变化（模块名、输入形状、输出形状、参数量），标注每一步对应的源码行号。
2. **注入名单对比**：沿 4.3 的实践，用假模块树分别跑 `qkvo_llm / qkvo_visual / qkvo_all / linear_llm` 四种 `lora_type`，记录目标模块数量；再用一行文字解释「配置选 `qkvo_all` 但视觉塔实际零可训练参数」这一现象（答案在解冻循环）。
3. **参数占比估算**：用公式 \( P = L \sum r(d_{in}+d_{out}) + (\text{词表行数} \times \text{hidden}) \times k \)（\( k \) 取决于嵌入是否共享权重，1 或 2，以本地 config 为准）估算可训练参数，与 README 的 ~400M 对照，写明哪一项占大头。

**操作步骤**：

1. 在 u1-l2 搭好的环境中（仓库根目录）新建一个独立脚本目录（不要放进 `uniseg3d/`），把 4.1、4.3、4.4 三个实践的代码合并进去，逐段打印；
2. 运行脚本，记录全部输出；
3. 打开 [configs/ground3dlmm_train.py:47-78](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L78)，把表中每个数字与配置字段一一对应（`d_model`、`in_channels`、`lora_r`、`max_point_tokens`…）；
4. 如条件允许（GPU + 权重），另跑一次 `tools/test.py` 的干跑（配置加载阶段即可，`--cfg-options` 技巧见 u1-l4），从日志里抓出 `Expanding vocab size`、`LoRA target modules`、`Total params` 三行，与你第 3 步的估算互相印证。

**预期结果**：一份能回答「256 与 hidden_size 在哪里互译」「LoRA 到底训了哪些层」「~400M 可训练参数主要是什么」三个问题的自查表。所有需要权重或网络的步骤都允许标注「待本地验证」，源码推演部分应当能独立完成。

## 6. 本讲小结

- **两座桥**：`point2token` 把 256 维超点特征（实为 `x_mask` 后的 `mask_feats`）升维到 LMM hidden_size，成为 `<|point|>` 嵌入；`query_proj` 把 `<SEG>` 位置 2560 维隐状态降回 256 维成为分割查询——父类旧版 32→256 的 `query_proj` 会被子类覆盖，是设计演化的化石。
- **词表扩容**：6 个特殊 token（`<p> </p> <SEG> <name> <sentence> <|point|>`）经 `add_special_tokens` 登记、`resize_token_embeddings` 加行、新行以旧嵌入均值初始化；`point_token_id` 回写进 config，是 u4-l1 钩子定位占位的依据；`new_tokens > 0` 保证幂等。
- **LoRA 注入**：`lora_type='qkvo_all'` 拆成「层型 + 范围」两段筛子生成注入名单；`get_peft_model` 注入后还有一个二次修剪循环——解冻 `embed_tokens/lm_head`、全冻 `visual`，净效果是 LoRA 只在语言塔注意力上生效。
- **参数账本**：可训练参数的大头是解冻的嵌入/lm_head（~3.9 亿量级），LoRA 旁路本身只是千万量级；训练日志的 `Total/Learnable params` 行可直接核实。
- **token 预算**：训练且超点数超过 `max_point_tokens`（配置 2000）时，用 `FPSPositionAggregator`（质心可用，首选）或 `FPSAggregator`（兜底）做零参数最远点采样；推理不聚合。四个聚合器类中两个是未启用的备选方案。

## 7. 下一步学习建议

本讲把解码器的「出厂设置」讲完了：桥、词表、LoRA、预算。下一讲 **u4-l3（训练分支 `infer_reason_train`：构造多轮对话与标签）** 将把这些组件串成真实的训练前向：QA 对如何组织成 chat messages、`apply_chat_template` 与 processor 编码的产物、`point_mask` 的对齐、`preprocess_chatml` 如何让语言损失只算在 assistant 回复上，以及本讲的聚合器在其中的触发时机。建议先自己通读 [uniseg3d/query_decoder.py:1325-1439](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1325-L1439)，带着「`point_embeds` 和 `<|point|>` 占位符是在哪一行对上号的」这个问题去读，会顺畅很多。
