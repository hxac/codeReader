# 语言分支：LangModule 与 OpenCLIP 文本编码

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清仓库内嵌的 `open_clip/` 目录与官方 OpenCLIP 库的关系，以及它在 UniSeg3D 中被改动了哪一处关键代码（视觉塔被移除）。
2. 跟踪一句 ScanRefer 风格的自然语言描述，从字符串 → BPE 分词 → token id 序列 → 文本 Transformer → 句向量（768 维）的完整前向链路。
3. 理解 `LangModule` 的两件事：为什么用 `fix=True` 冻结整个 CLIP 文本编码器、为什么再用一个两层 MLP 把 768 维投影到 32 维。
4. 亲手验证「反向传播只更新 MLP、不触碰 CLIP」这一冻结策略确实生效。

本讲是第 4 单元「模型核心」的语言侧配套：第 u4-l1 讲我们看过 `UniSeg3D` 主模型由 `unet/decoder/lang/criterion` 四大子模块组装而成，本讲就把其中的 `lang` 拆开看透。

## 2. 前置知识

### 2.1 CLIP 与「文本塔」

CLIP（Contrastive Language-Image Pre-training）是 OpenAI 提出的图文对齐模型，用 4 亿图文对做对比学习预训练。它有两个「塔」：

- **视觉塔（vision tower）**：把图像编码成向量；
- **文本塔（text tower）**：把句子编码成向量。

两塔输出在同一个 768 维语义空间里，语义相近的图文向量彼此靠近。对 UniSeg3D 来说，它不需要处理图像，只借用文本塔来「读懂」指代分割中的描述句（如 *it is a black chair next to the desk*），并复用 CLIP 在海量文本上学到的语义知识。

### 2.2 OpenCLIP 与 LAION 权重

[OpenCLIP](https://github.com/mlfoundations/open_clip) 是 CLIP 的开源复现，权重在 LAION 数据集上训练。本项目使用的是 `convnext_large_d_320` 结构、`laion2B-s29B-b131K-ft-soup` 标签对应的权重（29 亿样本、13.1 万步微调后的「soup」平均版），README 中要求手动下载到 `work_dirs/pretrained/convnext_large_d_320/`（见 [README.md:L155](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L155)）。

### 2.3 token、词表与因果注意力

- **token**：模型不直接读字符串，先经分词器（tokenizer）把句子切成子词单元（BPE），每个子词查词表得到一个整数 id。CLIP 词表大小 49408，序列固定长度 77，不足补 0，超长截断。
- **SOT/EOT**：每个序列以 `<start_of_text>` 开头、`<end_of_text>`（EOT，End Of Text）结尾。EOT 恰好是词表中 id 最大的 token，因此 `token_ids.argmax()` 就能定位句尾。
- **因果注意力（causal attention）**：文本 Transformer 中位置 \( i \) 只能看到位置 \( \le i \) 的 token。这样 EOT 位置的输出在注意力中「读完了整句话」，天然适合作为整句的摘要向量。

### 2.4 冻结与适配器

「冻结」即把参数的 `requires_grad` 置为 `False`，反向传播不为其计算梯度、优化器也不更新它。动机：3D 分割的训练数据（几百个场景）相对 CLIP 的预训练语料小几个数量级，若放开训练，微弱且嘈杂的梯度会破坏 CLIP 学到的语言知识。「适配器（adapter）」思路则是在冻结的大模型后接一个小的可训练投影层，只训练这个小头，既保留预训练知识又完成维度对齐——`LangModule` 的 MLP 正是这种做法。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [uniseg3d/lang_module.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py) | 语言分支总装：持冻结的文本编码器 + 可训练 MLP | `LangModule` 构造与 `forward` |
| [uniseg3d/openclip_backbone.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py) | 从 open_clip 工厂加载 CLIP 并摘出文本塔 | 六步文本前向 |
| [open_clip/factory.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py) | 内嵌 OpenCLIP 的「模型名 → JSON 配置 → 网络 → 权重」工厂 | `create_model_from_pretrained` |
| [open_clip/model.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model.py) | `CLIP` 顶层类 | 视觉塔被注释掉这一处改造 |
| [open_clip/transformer.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/transformer.py) | `TextTransformer` 文本塔本体 | 因果掩码、EOT 池化、text_projection |
| [open_clip/model_configs/convnext_large_d_320.json](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model_configs/convnext_large_d_320.json) | 模型结构超参 | 文本塔的 77/49408/768/12/16 |
| [open_clip/tokenizer.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/tokenizer.py) | BPE 分词器 | `tokenize` 补零与截断 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型 | `self.lang` 的构建与 `add_text_prompt_queries` 的消费方式 |

阅读提示（承接 u1-l3）：`openclip_backbone.py` 里的 `import open_clip` 会命中仓库根目录的内嵌 `open_clip/`，而不是 pip 装的官方库——前提是运行时项目根目录在 `sys.path` 中（`dist_train.sh` 注入 `PYTHONPATH`，或你自己 `sys.path.insert(0, '.')`）。

## 4. 核心概念与源码讲解

### 4.1 open_clip 工厂与 convnext_large_d_320 模型配置

#### 4.1.1 概念说明

OpenCLIP 维护着上百种模型结构。它不写死任何一种，而是用一套「名字 → JSON」的配置表：`model_configs/` 目录下每个 `.json` 描述一种结构（嵌入维度、视觉塔配置、文本塔配置），工厂函数按名字查表、实例化、再加载预训练权重。这与 mmengine 的 Registry 是**两套独立机制**（Registry 管「uniseg3d 自己的类怎么被配置字符串实例化」，OpenCLIP 工厂管「第三方 CLIP 网络怎么按名字搭建」），`LangModule` 恰好同时站在两者之上。

#### 4.1.2 核心流程

`create_model_from_pretrained('convnext_large_d_320', pretrained=<路径>)` 的流程：

1. 启动时扫描 `model_configs/*.json` 填充全局字典 `_MODEL_CONFIGS`（模块 import 即执行）。
2. `get_model_config(model_name)` 查表拿到结构 dict，找不到直接报错。
3. 结构 dict 里无 `custom_text`/`hf_model_name` → 走 `CLIP(**model_cfg)` 实例化。
4. 解析 `pretrained`：若是预训练标签（如 `laion2B-s29B-b131K-ft-soup`）则查 `_PRETRAINED` 表得下载地址；若是存在的本地文件路径则直接用。
5. `load_checkpoint` 把权重灌进模型，返回。

#### 4.1.3 源码精读

配置表扫描——`model_configs/` 下所有含 `embed_dim/vision_cfg/text_cfg` 三键的 JSON 进入注册表，文件名（去后缀）即模型名：

[open_clip/factory.py:L33-L54](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L33-L54)：模块加载时执行 `_rescan_model_configs()`，把每个 JSON 读入 `_MODEL_CONFIGS[cf.stem]`，因此 `'convnext_large_d_320'` 这个名字对应 `convnext_large_d_320.json`。

本项目的结构超参：

[open_clip/model_configs/convnext_large_d_320.json:L1-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model_configs/convnext_large_d_320.json#L1-L19)：`embed_dim=768`（图文共享的输出空间维度）；`vision_cfg` 用 timm 的 `convnext_large`（本项目用不到）；`text_cfg` 五个数字决定了文本塔的一切——`context_length=77`（序列长度）、`vocab_size=49408`（词表）、`width=768`（Transformer 宽度）、`heads=12`（注意力头数）、`layers=16`（层数）。

结构实例化：

[open_clip/factory.py:L185-L193](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L185-L193)：本项目配置不含 `custom_text` 等键，走 `model = CLIP(**model_cfg, cast_dtype=cast_dtype)` 分支。

权重的两种来源：

[open_clip/factory.py:L218-L236](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L218-L236)：先 `get_pretrained_cfg(model_name, pretrained)` 按标签查下载信息（[open_clip/pretrained.py:L205-L208](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/pretrained.py#L205-L208) 中 `laion2b_s29b_b131k_ft_soup` 标签指向 HuggingFace 仓库 `laion/CLIP-convnext_large_d_320.laion2B-s29B-b131K-ft-soup`）；查不到标签时退回 `os.path.exists(pretrained)` 检查本地路径——UniSeg3D 主配置传的就是本地路径 `work_dirs/pretrained/convnext_large_d_320/open_clip_pytorch_model.bin`。注意 `create_model_from_pretrained` 设置了 `require_pretrained=True`（[open_clip/factory.py:L346-L373](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L346-L373)），权重缺失会直接 `RuntimeError`，不会静默用随机初始化。

**仓库对官方 OpenCLIP 的一处关键改造**：

[open_clip/model.py:L199-L211](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model.py#L199-L211)：官方代码在此构建视觉塔 `self.visual = _build_vision_tower(...)`，本仓库把它**注释掉了**（L201），只保留文本塔的六个组件（transformer、token_embedding、positional_embedding、ln_final、text_projection、attn_mask）。视觉塔是 convnext_large，参数量远大于文本塔——跳过它省下数 GB 显存与加载时间。代价是 `model.visual` 属性不存在，所以 [open_clip/factory.py:L375-L376](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L375-L376) 的 `return_transform=False` 分支（提前返回、不构造图像预处理）是**必选项**：`return_transform=True` 的路径会访问 `model.visual` 直接崩溃。这也解释了 [uniseg3d/openclip_backbone.py:L33-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L33-L41) 为什么两个分支都传 `return_transform=False`。

分词器的选择：

[open_clip/factory.py:L77-L84](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L77-L84)：`get_tokenizer` 查模型配置，`convnext_large_d_320` 的 `text_cfg` 没有 `hf_tokenizer_name`，因此返回内置的 BPE `tokenize` 函数。

#### 4.1.4 代码实践

**实践目标**：确认内嵌 `open_clip` 的「名字 → JSON」机制真实存在，并看清 `convnext_large_d_320` 的文本塔超参。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# practice_factory.py （示例代码）
import sys; sys.path.insert(0, '.')   # 确保 import open_clip 命中内嵌目录
import open_clip
from open_clip.factory import get_model_config

print('convnext' in open_clip.list_models())          # True
cfg = get_model_config('convnext_large_d_320')
print(cfg['embed_dim'])                               # 768
print(cfg['text_cfg'])   # {'context_length': 77, 'vocab_size': 49408,
                         #  'width': 768, 'heads': 12, 'layers': 16}
print(cfg['vision_cfg']['timm_model_name'])           # convnext_large（本项目不用）
```

**需要观察的现象**：`import open_clip` 后打印 `open_clip.__file__` 应指向仓库内的 `open_clip/__init__.py`；若指向 site-packages，说明 `sys.path` 注入失败，后续读到的将是官方库（不含本仓库的改造）。

**预期结果**：输出与上面注释一致。**待本地验证**（本讲义编写环境未安装 torch，无法实际执行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把主配置中 `lang` 的 `model_name` 改成一个 `model_configs/` 里不存在的名字，程序会在哪一步、以什么方式失败？

**答案**：在 `OpenCLIPBackboneText.__init__` 调用 `open_clip.create_model_from_pretrained` 时，[open_clip/factory.py:L153-L158](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L153-L158) 查不到 `model_cfg`，记 error 日志后抛出 `RuntimeError('Model config for ... not found.')`，可用模型列表会一并打印在日志里。

**练习 2**：`create_model_from_pretrained` 为什么必须传 `return_transform=False`？

**答案**：本仓库的 `CLIP.__init__` 注释掉了视觉塔构建（[open_clip/model.py:L201](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model.py#L201)），模型没有 `visual` 属性；而 `return_transform=True` 时工厂会执行 `model.visual.image_size` 构造图像预处理（[open_clip/factory.py:L377-L385](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L377-L385)）触发 `AttributeError`。

**练习 3**：`_MODEL_CONFIGS` 是什么时候被填充的？为什么调用方不需要显式初始化？

**答案**：`open_clip/factory.py` 模块体末尾的 `_rescan_model_configs()`（[open_clip/factory.py:L54](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L54)）在 `import open_clip` 时作为模块级语句自动执行——又是「import 的副作用」这一模式（与 u2-l1 的 Registry 注册同理）。

### 4.2 OpenCLIPBackboneText 文本前向

#### 4.2.1 概念说明

`OpenCLIPBackboneText` 是一个「抽取器」：用工厂造出完整 CLIP 后，只把文本塔的五个部件摘到 `self` 上，再实现一个六步前向，把 `[B, 77]` 的 token 矩阵压成 `[B, 768]` 的句向量。它继承 mmengine 的 `BaseModule` 并注册进 MODELS，但**权重加载不走 mmengine**——构造函数里就通过 open_clip 工厂把预训练权重灌好了。

#### 4.2.2 核心流程

前向六步（记 token 矩阵为 \( T \in \mathbb{Z}^{B \times 77} \)，宽度 \( d = 768 \)）：

1. **token 嵌入**：\( E = \text{Embedding}(T) \)，形状 \([B, 77, d]\)；
2. **加位置编码**：\( E \leftarrow E + P \)，\( P \in \mathbb{R}^{77 \times d} \) 可学习；
3. **换轴**：`NLD → LND`（PyTorch `nn.MultiheadAttention` 的 batch 维约定）；
4. **16 层 Transformer**，因果掩码 \( M_{ij} = \begin{cases} 0 & j \le i \\ -\infty & j > i \end{cases} \)（加性掩码，未来位置打负无穷）；
5. **最终 LayerNorm**：\( X = \text{LN}(X) \)；
6. **EOT 池化 + 投影**：取每行 EOT 位置的向量 \( x_{\text{eot}} \)，输出 \( y = x_{\text{eot}} W_{\text{proj}} \)，其中 \( W_{\text{proj}} \in \mathbb{R}^{768 \times 768} \)。

EOT 位置由 `text_tokens.argmax(dim=-1)` 得到——BPE 词表中 `<end_of_text>` 恰是 id 最大的 token，补零位 id 为 0，`argmax` 必然命中句尾。由于因果注意力允许 EOT 看到全部前文，\( x_{\text{eot}} \) 汇总了整句语义。

#### 4.2.3 源码精读

构造：断言、分布式协调与部件抽取：

[uniseg3d/openclip_backbone.py:L24-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L24-L27)：只接受 `type='clip_pretrain'` 的 init_cfg；注意 `super().__init__(init_cfg=None)` 而把 `init_cfg` 手动存回 `self`——这是为了不让 mmengine 的 `init_weights` 机制尝试解析它不认识的 `clip_pretrain` 类型。

[uniseg3d/openclip_backbone.py:L31-L37](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L31-L37)：多卡时 rank 0 先创建（触发下载），其余进程 `dist.barrier()` 等待，避免多进程同时写缓存文件。

[uniseg3d/openclip_backbone.py:L40-L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L40-L51)：创建完整 CLIP 后摘出文本塔六件套挂到 `self`：`text_transformer`（16 层）、`text_token_embedding`（49408×768 查表）、`text_pe`（位置编码）、`text_ln_final`、`text_proj`（768×768 投影矩阵）、并把因果掩码注册为**非持久 buffer** `text_attn_mask`（不写入 checkpoint）。注意 L44 的 `self.text_tokenizer` 在本仓库运行时从未被调用——分词在数据准备阶段离线完成（u1-l4：`uniseg3d_infos_*.pkl` 里的 `text_info.text_token` 就是用同一套 OpenCLIP 分词器预计算好的），此处保留仅为调试便利。

前向：

[uniseg3d/openclip_backbone.py:L62-L72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L62-L72)：整个 `forward` 被 `@torch.no_grad()` 装饰（第一层梯度保护，配合 4.3 节的冻结构成双保险）；六步依次为查表嵌入 → 加位置编码 → `permute(1,0,2)` → `text_transformer(x, attn_mask=...)` → 换回 → `ln_final` → L71 的 EOT 池化加投影。这与官方 `CLIP.encode_text`（[open_clip/model.py:L228-L240](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model.py#L228-L240)）逐行等价，只是把 `self.transformer` 等换成 `self.text_transformer` 等别名。

文本塔本体（部件的定义处，便于对照）：

[open_clip/transformer.py:L533-L553](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/transformer.py#L533-L553)：`text_projection` 是 `nn.Parameter(torch.empty(width, output_dim))`——注意它是**矩阵参数**而非 Linear 层，前向用 `@` 相乘；`token_embedding`、`positional_embedding`、16 层 `Transformer`、`ln_final` 与因果掩码 buffer 均在此定义。`text_proj.shape == (768, 768)`，这正是 4.3 节 MLP 用 `text_proj.shape[1]` 推断输入维度的依据。

[open_clip/transformer.py:L579-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/transformer.py#L579-L585)：因果掩码构造——先全部填 \(-\infty\) 再 `triu_(1)` 把下三角清零，得到「列 > 行的位置被屏蔽」的加性掩码。

分词（离线数据侧用到，此处用于对照）：

[open_clip/tokenizer.py:L159-L188](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/tokenizer.py#L159-L188)：`tokenize` 为每句拼上 SOT/EOT，落进固定长度 77 的零矩阵，超长截断且把最后一位改回 EOT——保证「argmax 命中句尾」的约定在任何输入下都成立。

#### 4.2.4 代码实践

**实践目标**：亲手分步执行六步前向，观察每一步张量形状，理解 `[B,77] → [B,768]` 的压缩过程。

**操作步骤**（示例代码）：

```python
# practice_text_fwd.py （示例代码）
import sys; sys.path.insert(0, '.')
import torch, open_clip

model = open_clip.create_model('convnext_large_d_320', pretrained=None)  # 随机初始化即可看数据流
texts = ["it is a black chair next to the desk.",
         "the wooden table in the middle of the room.",
         "a white refrigerator against the wall."]
tokens = open_clip.get_tokenizer('convnext_large_d_320')(texts)   # [3, 77]
print('tokens:', tokens.shape, 'eot pos:', tokens.argmax(-1).tolist())

x = model.token_embedding(tokens).to(torch.float32)   # [3, 77, 768]
x = x + model.positional_embedding.to(torch.float32)
x = x.permute(1, 0, 2)                                # [77, 3, 768]
x = model.transformer(x, attn_mask=model.attn_mask)
x = x.permute(1, 0, 2)                                # [3, 77, 768]
x = model.ln_final(x)
y = x[torch.arange(x.shape[0]), tokens.argmax(dim=-1)] @ model.text_projection
print('sentence embedding:', y.shape)                 # [3, 768]
print('两把不同椅子的描述应该不相关，余弦相似度（随机权重下无语义）:',
      torch.cosine_similarity(y[0], y[1], dim=0).item())
```

**需要观察的现象**：`tokens` 每行只在句尾处有一个大 id（EOT≈49407），其余是 0（补零）；`y.shape == [3, 768]`。

**预期结果**：形状与注释一致；随机权重下三句向量两两余弦相似度是无意义的随机值（约 ±0.1）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `text_tokens.argmax(dim=-1)` 而不是固定取第 77 列来取句向量？

**答案**：补零机制下句长不同，EOT 位置随句长浮动；且 EOT 是词表最大 id、补零是 id 0，`argmax` 恰好命中 EOT（[open_clip/tokenizer.py:L177-L186](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/tokenizer.py#L177-L186)）。固定取末列取到的是补零位置的输出，不含任何句子信息。

**练习 2**：`text_attn_mask` 被注册为非持久 buffer（`persistent=False`），这带来什么效果？

**答案**：它不进入 `state_dict`，保存 checkpoint 时不落盘；且它由 `build_attention_mask` 确定性生成（[open_clip/transformer.py:L579-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/transformer.py#L579-L585)），加载时随模型构建重建，无需存储。u4-l4 讲过的类别原型 buffer 也是同样手法。

**练习 3**：前向里两次 `permute(1, 0, 2)` 的目的是什么？

**答案**：`nn.MultiheadAttention` 系列 API 要求 batch 在第 0 维（LND 布局），而嵌入输出是 `[batch, seq, dim]`（NLD），进 Transformer 前换成 LND、出来后换回，纯维度约定，不改数值。

### 4.3 LangModule 冻结与 MLP 投影

#### 4.3.1 概念说明

`LangModule` 是语言分支的总装层，解决两个问题：

1. **保知识**：`fix=True` 把整个 CLIP 文本编码器的 `requires_grad` 关掉，3D 小数据训练不破坏大规模预训练的语言知识；配合 4.2 节 `forward` 上的 `@torch.no_grad()`，梯度在两道闸门处都被切断。
2. **对维度**：CLIP 输出 768 维，而 UniSeg3D 的查询世界是 32 维的——稀疏骨干输出的超点特征是 32 通道（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L9) 的 `num_channels = 32`），解码器 `in_channels=32`。文本嵌入必须压到同维，才能作为「文本提示查询」与超点查询拼进同一序列、共享同一个解码器。MLP（768 → 768 → GELU → 32）就是这段可训练的适配器。

#### 4.3.2 核心流程

- **构造**：建 `text_encoder` → 若 `fix` 则逐参数关 `requires_grad` → 按 `text_proj.shape[1]`（=768）建 `Mlp(768 → 768 → 32)`。
- **前向**：输入是「按样本分组」的 token 列表 `text_token`（每个元素是该样本若干条描述的 `[n_i, 77]`）；逐样本调用文本编码器得 `[n_i, 768]`，再过 MLP 得 `[n_i, 32]`；空样本返回空张量；输出仍是逐样本列表。用公式表达单条描述：
  \[ y = \mathrm{CLIP\_text}(T) \in \mathbb{R}^{768}, \qquad z = W_2 \, \mathrm{GELU}(W_1 y) \in \mathbb{R}^{32} \]
- **下游消费**：主模型 `add_text_prompt_queries` 给文本嵌入加上 `type_embedding.weight[0]`（32 维「文本类型」标记，与超点查询、点提示查询的标记区分），拼到该样本查询序列尾部。

#### 4.3.3 源码精读

主模型中的构建与配置：

[uniseg3d/uniseg3d.py:L303-L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L303-L313)：主模型只拿一个 dict，`self.lang = MODELS.build(lang)`——语言分支与骨干、解码器一样由注册器组装。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L39-L46](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L39-L46)：`lang` 配置五项——`input_channels=768`（仅存值，实际维度由 `text_proj` 推断）、`model_name`、指向本地权重路径的 `init_cfg`、`fix=True`、`out_features=32`。

适配器 MLP：

[uniseg3d/lang_module.py:L13-L36](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L13-L36)：自实现的 `Mlp`——`fc1` 升/保维（768→768）+ GELU + Dropout + `fc2` 降到目标维（32）。Dropout 默认 `drop=0.0`，即训练时也不随机失活。

冻结与维度推断：

[uniseg3d/lang_module.py:L51-L56](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L51-L56)：构造 `text_encoder` 后，`fix=True` 时遍历 `named_parameters()` 全部关 `requires_grad`；随后 `self.Mlp` 的三层维度都由 `self.text_encoder.text_proj.shape[1]` 动态推出——换一个输出维度不同的 CLIP 结构（如 ViT-B-32 的 512）时 MLP 输入维会自动跟随，`input_channels` 参数反而没有参与任何层的构建（阅读时注意这一不一致）。

逐样本前向：

[uniseg3d/lang_module.py:L59-L72](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L59-L72)：`forward(text_token)` 逐样本循环；该样本无文本提示（`len==0`）时返回空张量占位；否则 `text_encoder → Mlp`。之所以逐样本循环而不整批拼接：不同样本的描述条数 \( n_i \) 不同，且下游要按样本拼接查询。

下游如何消费（文本嵌入的去处）：

[uniseg3d/uniseg3d.py:L655-L668](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L668)：`add_text_prompt_queries` 调 `self.lang(batch_inputs_dict['text_token'])` 得逐样本 `[n_i, 32]` 嵌入，加 `type_embedding.weight[0]` 后 `torch.cat` 到该样本查询序列**尾部**（配合 u4-l6 将讲的 `interaction_masks` 隔离不同提示组的自注意力）。类型嵌入表定义在 [uniseg3d/uniseg3d.py:L326-L327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L326-L327)：`Embedding(3, 32)`，三种来源（文本/超点/点提示）各占一行 32 维向量——文本嵌入压到 32 维后，这个加法才能逐元素成立。

两个阅读提示：其一，`lang_module.py` 顶部 `pack_padded_sequence/pad_packed_sequence`（L6）与 mmdet3d 的 `MLP`（L9）均为**导入而未使用**的历史残留，读代码时不要去找它们的用途；其二，`init_weights`（[uniseg3d/openclip_backbone.py:L56-L58](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L56-L58)）只打日志——CLIP 权重在 `__init__` 里就加载完了，mmengine 的初始化钩子无事可做。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：走通「三句 ScanRefer 风格描述 → 分词 → LangModule → 词嵌入」，并用梯度验证冻结策略：反向传播后 MLP 有梯度、CLIP 编码器无梯度。

**操作步骤**（示例代码，需要 u1-l2 搭好的环境与已下载的 CLIP 权重）：

```python
# practice_lang_module.py （示例代码，仓库根目录运行）
import sys; sys.path.insert(0, '.')
import torch, open_clip

texts = [
    "it is a black chair next to the desk.",
    "the wooden table in the middle of the room.",
    "a white refrigerator against the wall.",
]
tokenizer = open_clip.get_tokenizer('convnext_large_d_320')
text_token = tokenizer(texts)                 # [3, 77]
print('token 形状:', text_token.shape, 'EOT 下标:', text_token.argmax(-1).tolist())

from uniseg3d.lang_module import LangModule
lang = LangModule(
    model_name='convnext_large_d_320',
    init_cfg=dict(type='clip_pretrain',
                  checkpoint='work_dirs/pretrained/convnext_large_d_320/open_clip_pytorch_model.bin'),
    fix=True, out_features=32)

embeds = lang([text_token])                   # LangModule 接口是「逐样本列表」
print('词嵌入形状:', embeds[0].shape)          # [3, 32]

loss = embeds[0].pow(2).sum()
loss.backward()

mlp     = [(n, p.grad is not None) for n, p in lang.Mlp.parameters()]
encoder = [(n, p.grad is not None, p.requires_grad) for n, p in lang.text_encoder.parameters()]
print('MLP 各参数是否有梯度:', [g for _, g in mlp])                # [True, True, True, True]
print('编码器 requires_grad 全为 False:', all(not r for _, _, r in encoder))
print('编码器梯度全为 None:', all(g is None for _, g, _ in encoder))
```

**没有权重时的替代方案**：`LangModule` 走 `create_model_from_pretrained`（`require_pretrained=True`），权重缺失会直接报错。若只想过数据流，可用 `open_clip.create_model('convnext_large_d_320', pretrained=None)` 随机初始化，手工执行 4.2.4 的六步前向再接一个自建 `Mlp(768, 768, 32)`，梯度验证部分完全相同。

**需要观察的现象**：

1. `text_token` 形状 `[3, 77]`，每行末尾有一个远大于 0 的 id（EOT）。
2. `embeds[0]` 形状 `[3, 32]`——三句描述各自一条 32 维文本提示嵌入。
3. `lang.Mlp` 的 4 个参数（fc1.weight/fc1.bias/fc2.weight/fc2.bias）`grad` 均非 None。
4. `lang.text_encoder` 所有参数 `requires_grad=False` 且 `grad` 为 None。

**预期结果**：与上述注释一致。即使把 `fix` 改成 `False`，第 4 条中梯度仍应是 None——因为 `OpenCLIPBackboneText.forward` 带 `@torch.no_grad()`（[uniseg3d/openclip_backbone.py:L62](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L62)），梯度在计算图构建处就被切断，这是「冻结」的第二道闸门。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `out_features` 从 32 改成 64，训练还能跑通吗？会坏在哪一步？

**答案**：`LangModule` 本身能构造（MLP 变为 768→768→64），但坏在下游拼接：`add_text_prompt_queries` 里 `queries[i]`（超点查询，32 维）与 64 维文本嵌入 `torch.cat` 后张量列数不一致，且 `type_embedding.weight[0]` 是 32 维无法相加（[uniseg3d/uniseg3d.py:L666-L668](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L666-L668)），第一步加法即报维度错误。`out_features` 必须与 `num_channels=32` 及解码器 `in_channels=32` 三方一致。

**练习 2**：既然 `fix=True` 已经关了 `requires_grad`，为什么 `forward` 还要加 `@torch.no_grad()`？二者是否冗余？

**答案**：不完全冗余。`requires_grad=False` 阻止参数收到梯度，但若输入张量带梯度，计算图仍会穿过编码器构建（白耗显存与计算）；`@torch.no_grad()` 直接不建图，更快更省。同时它防御了「误设 `fix=False`」的配置失误——即使忘记冻结，梯度也进不了 CLIP。双层保护是刻意的工程冗余。

**练习 3**：`LangModule.forward` 为什么对 `len(text_token[i])==0` 的样本返回空张量而不是跳过？

**答案**：返回值 `word_embeddings` 与输入样本一一对应（按下标 `i` 对齐），下游 `add_text_prompt_queries` 遍历 `batch_text_prompts` 时用 `len(cur_text_prompts) > 0` 分支处理「该样本无文本提示」的情况并填空 GT（[uniseg3d/uniseg3d.py:L664-L681](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L664-L681)）；若跳过会导致列表长度与批内样本数不一致、后续按下标错位。训练时 `TextPromptGeneration` 保证有文本，但测试批内可能出现无文本样本，占位是必要的。

## 5. 综合实践

**任务：模拟一条指代分割样本在模型内部的「文本注入」全过程**，把本讲三个模块串起来。

在仓库根目录写一个脚本（示例代码），按顺序完成：

1. 用 `open_clip.get_tokenizer('convnext_large_d_320')` 对两句描述分词，得到 `[2, 77]` 的 `text_token`；再准备一个「空样本」的 `text_token = np.array([])` 风格的空列表，模拟批内第二个场景没有文本提示。
2. 构造 `LangModule`（有权重用权重，无权重按 4.3.4 的替代方案随机初始化），调用 `lang([token_2, token_0])`，打印两个输出的形状——应为 `[2, 32]` 与空。
3. 手工模拟 [uniseg3d/uniseg3d.py:L661-L668](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L661-L668) 的注入逻辑：`type_embedding = torch.nn.Embedding(3, 32)`，设第一个样本原有 100 条超点查询（`torch.randn(100, 32)`），把 `embed[0] + type_embedding.weight[0]` 拼接到尾部，打印拼接后形状（应为 `[102, 32]`）。
4. 反向传播 `loss = 拼接结果.sum()`，验证梯度只落在 `Mlp` 与 `type_embedding` 上、CLIP 编码器无梯度。

**预期结果**：形状分别为 `[2,77]`、`[2,32]`、`[102,32]`；梯度分布如第 4 条所述。完成后，你就把「离线分词（u3-l5）→ 文本编码（本讲）→ 查询注入（u4-l6 预习）」整条链路在 30 行代码里走了一遍。**待本地验证**。

## 6. 本讲小结

- 仓库内嵌的 `open_clip/` 是官方 OpenCLIP 的锁版本拷贝，且**注释掉了 CLIP 的视觉塔构建**（[open_clip/model.py:L201](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/model.py#L201)）——只留文本塔，因此工厂调用必须 `return_transform=False`。
- 工厂链路是「模型名 → `model_configs/*.json` → `CLIP(**cfg)` → 加载权重」：`convnext_large_d_320` 的文本塔为 77 上下文、49408 词表、768 宽、12 头、16 层，输出 768 维。
- 文本前向六步：token 查表 → 加位置编码 → LND → 16 层因果注意力 Transformer → LN → **EOT 位置池化** `@ text_projection`；EOT 是词表最大 id，`argmax` 即句尾。
- `LangModule = 冻结的 CLIP + 可训练 MLP(768→768→32)`：`fix=True` 与 `@torch.no_grad()` 构成双闸门；32 维输出与骨干超点特征、解码器 `in_channels`、`type_embedding` 四方对齐，文本嵌入加类型标记后拼进查询序列尾部。
- 文本分支的梯度只流向 MLP（和下游），CLIP 在整个 3D 训练中原封不动——这是小数据微调大预训练模型的典型适配器范式。
- 阅读陷阱：`input_channels` 参数存而不用（真实维度取自 `text_proj.shape[1]`）；`text_tokenizer` 运行时不被调用（分词离线完成）；文件顶部有多个未使用的 import。

## 7. 下一步学习建议

- 下一讲 **u4-l6「提示查询注入与结果拆分」**：本讲产出的 32 维文本嵌入如何与点提示查询、超点查询共用一个解码器，`interaction_masks` 如何隔离三组查询的自注意力，解码输出又如何按来源拆分——那里会把本讲的 `add_text_prompt_queries` 的另一半讲完。
- 回顾对照：u3-l5（数据侧 `text_token` 怎么来的）与本讲（模型侧怎么消费它）构成文本提示的完整闭环，建议两讲的实践对照着做一遍。
- 延伸阅读：u4-l4 讲的球面分类同样复用了 OpenCLIP 文本塔——`scannet_cls_embedding.pth` 类别原型就是用同一套 `convnext_large_d_320` 文本塔离线编码类名得到的，可对照理解「同一编码器，一次编码查询句、一次编码类名词表」的复用方式。
