# u5-l5 两种 DataCollator：常规 padding 与 flatten/packing 拼接

## 1. 本讲目标

学完本讲，你应该能够：

1. 讲清 `DataCollatorForSupervisedDataset`（常规路径）如何用 padding 把一个 batch 的变长样本对齐成矩形张量，以及 labels 为什么用 `-100` 填充。
2. 讲清 `FlattenedDataCollatorForSupervisedDataset`（拼接路径）如何把 batch 内多条样本沿序列维拼成一条长序列。
3. 解释拼接路径中 `attention_mask` 被改造成「首元素为 0 的累计序列长度向量」（cu_seqlens）的设计，以及它如何被 FlashAttention varlen 消费。
4. 区分 `data_flatten` 与 `data_packing` 两个开关：谁在什么阶段做拼接、数据文件长什么样。
5. 理解 packed 桶在 `_get_packed_item` 中的两级拼接过程。

## 2. 前置知识

- **DataCollator 是什么**：PyTorch 的 `DataLoader` 每次 yield 出一个 batch 的原始样本（`Dataset.__getitem__` 的返回值列表），但模型要求 batch 内所有张量形状一致。DataCollator 就是「把一堆形状各异的样本整形成统一 batch 字典」的函数。Hugging Face Trainer 允许通过 `data_collator` 参数注入自定义实现。
- **padding（填充）**：把短序列用特殊占位 token（`pad_token_id`）补到 batch 内最长序列的长度，使所有序列等长、能堆叠成 `(B, L_max)` 的矩形张量。
- **IGNORE_INDEX = -100**：PyTorch `CrossEntropyLoss` 的 `ignore_index` 默认值。labels 中等于 -100 的位置不参与损失计算。本框架用它在 u5-l3 中屏蔽 user 轮与视觉 token。
- **cu_seqlens（累计序列长度）**：FlashAttention varlen 接口的输入格式。给定一条拼接长序列和向量 `[0, L₁, L₁+L₂, ...]`，varlen 内核把 `[0,L₁)` 当第 1 条独立序列、`[L₁, L₁+L₂)` 当第 2 条，段与段之间**互相不可见**。这是「把多条样本塞进一条序列还不串味」的关键机制（细节在 u5-l6 展开）。
- **上一讲的产出**：u5-l3 中 `LazySupervisedDataset._get_item` 返回的单条样本是 `input_ids (1, L)`、`labels (1, L)`、`position_ids (3, 1, L)`，外加可选的 `pixel_values` / `image_grid_thw` 等。本讲的 collator 消费的就是这些字典。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 本讲主战场：两种 collator、`_get_packed_item`、`pad_and_cat`、`make_supervised_data_module` 全部在此 |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | `data_flatten` / `data_packing` 两个开关的定义（L16-L17） |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 开启任一开关时执行 `replace_qwen2_vl_attention_class()`（L141-L142），为 cumsum 掩码铺路 |
| [qwen-vl-finetune/qwenvl/train/trainer.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py) | 打补丁后的 `flash_attention_forward`：把 cumsum 向量直接当 `cu_seqlens` 用（L85-L104），u5-l6 精读 |
| [qwen-vl-finetune/scripts/sft_qwen3_4b.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh) | 官方脚本默认开启 `--data_flatten True`（L34） |

## 4. 核心概念与源码讲解

### 4.1 模块一：DataCollator 常规路径——padding 对齐

#### 4.1.1 概念说明

训练数据是变长的：一句话 30 个 token，一张图加问答可能上千个 token。若一个 batch 里长短混杂，最朴素的做法是把所有序列补齐到最长那条的长度 `L_max`，堆叠成 `(B, L_max)` 的矩形张量——这就是常规 collator 的 padding 路径。

它的优点是实现简单、语义直白；缺点是**算力浪费**：假设 batch 内两条样本长度分别是 12 和 35，padding 后两条都按 35 计算，短样本多算的 23 个位置全是无效 FLOPs。样本长度方差越大、浪费越严重——这正是 4.2 拼接路径要解决的问题。

#### 4.1.2 核心流程

```text
输入: instances = [ {input_ids(1,Lᵢ), labels(1,Lᵢ), position_ids(3,1,Lᵢ), ...}, ... ]

1. 取出 input_ids / labels / position_ids 三组列表
2. input_ids、labels 去掉 batch 维 → (Lᵢ,)
3. pad_sequence 对齐到 L_max：
   - input_ids 用 pad_token_id 填充
   - labels 用 IGNORE_INDEX(-100) 填充
4. position_ids 走 pad_and_cat：末尾补 1 对齐后沿 batch 维堆叠 → (3, B, L_max)
5. 三个张量统一截断到 tokenizer.model_max_length
6. attention_mask = input_ids != pad_token_id   ← 标准 0/1 掩码，形状 (B, L_max)
7. 收集 batch 内所有样本的 pixel_values / pixel_values_videos，
   沿 patch 维(dim=0) 拼接；grid_thw 表也沿 dim=0 拼成一张 batch 级网格表
```

#### 4.1.3 源码精读

collator 是一个只持有 tokenizer 的 dataclass：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L534-L549](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L534-L549)
这段定义了 `DataCollatorForSupervisedDataset`：先从每个 instance 取出 `input_ids`、`labels`、`position_ids` 三组列表，squeeze 掉 batch 维，然后用 `pad_sequence` 把 `input_ids` 补到等长——注意两条填充值不同：`input_ids` 用 `self.tokenizer.pad_token_id`（占位 token 本身是合法词表 id，模型能正常前向），`labels` 用 `IGNORE_INDEX`（补出来的位置不进损失）。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L550-L561](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L550-L561)
这段做三件事：① `position_ids` 走自定义的 `pad_and_cat`；② 三个张量统一右截断到 `tokenizer.model_max_length`（注意：**截断只存在于常规路径**，拼接路径没有这一步）；③ 生成标准 0/1 掩码 `attention_mask = input_ids.ne(pad_token_id)`——pad 位置为 0、真实 token 为 1，形状 `(B, L_max)`，这是模型原生语义的 attention_mask。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L520-L531](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L520-L531)
`pad_and_cat` 专管 position_ids：每条样本的 `(3, 1, Lᵢ)` 先在最后一维用**常量 1** 补到 `L_max`，再沿 `dim=1`（batch 维）拼接成 `(3, B, L_max)`。padding 位置的 position id 是多少本无所谓——这些位置是 pad token，已被 0/1 掩码挡在注意力之外，取 1 只是随便挑了个合法小值。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L562-L601](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L562-L601)
这段处理视觉张量：遍历 batch，把**含有** `pixel_values` 的样本收集起来沿 `dim=0`（patch 维）`torch.cat` 成一个大张量，`image_grid_thw` 同样沿 `dim=0` 拼成一张 batch 级网格表；视频的 `pixel_values_videos` / `video_grid_thw` 同理。因此 batch 可以混排「纯文本样本 + 带图样本 + 带视频样本」——没有视觉键的样本只是不贡献 patch 而已。最后把全部字段装进 batch 字典返回。

#### 4.1.4 代码实践

**实践目标**：亲眼看到常规 collator 输出的矩形张量与 0/1 掩码。

先搭建一个贯穿本讲的最小实验台（保存为 `collator_lab.py`，放在 `qwen-vl-finetune` 目录下运行，因为要 import `qwenvl` 包；以下为**示例代码**）：

```python
# 示例代码：qwen-vl-finetune/collator_lab.py
# 运行方式: cd qwen-vl-finetune && python collator_lab.py
import torch
from transformers import AutoProcessor

from qwenvl.data.data_processor import (
    DataCollatorForSupervisedDataset,
    FlattenedDataCollatorForSupervisedDataset,
    preprocess_qwen_visual,
)
from qwenvl.data.rope2d import get_rope_index_3

MODEL = "Qwen/Qwen3-VL-2B-Instruct"          # 只下载 processor，不加载模型权重
processor = AutoProcessor.from_pretrained(MODEL)
merge_size = processor.image_processor.merge_size   # = 2

def make_instance(conversations):
    """手工复刻 LazySupervisedDataset._get_item 的输出契约（纯文本样本）"""
    ann = {"data_path": "", "conversations": conversations}
    d = preprocess_qwen_visual([ann], processor)        # input_ids/labels 等来自 chat template
    seq_len = d["input_ids"][0].size(0)
    position_ids, _ = get_rope_index_3(                 # 纯文本路径：三行 position id 相同
        merge_size, d["input_ids"],
        image_grid_thw=None, video_grid_thw=None, second_per_grid_ts=None,
    )
    d["position_ids"] = position_ids
    d["attention_mask"] = [seq_len]                     # 对齐 data_processor.py:L427 的写法
    return d

inst1 = make_instance([
    {"from": "human", "value": "你好"},
    {"from": "gpt",   "value": "你好！有什么可以帮你？"},
])
inst2 = make_instance([
    {"from": "human", "value": "用一句话介绍长城"},
    {"from": "gpt",   "value": "长城是中国古代修建的巨型防御工事，绵延万里。"},
])

# ---- 常规 collator ----
collator = DataCollatorForSupervisedDataset(tokenizer=processor.tokenizer)
batch = collator([inst1, inst2])
for k, v in batch.items():
    print(k, None if v is None else tuple(v.shape))
print("attention_mask 内容:\n", batch["attention_mask"])
print("labels 中非 -100 的 token 数:", (batch["labels"] != -100).sum().item())
```

**操作步骤**：安装 transformers≥4.57.0 后按注释运行（首次会从 Hugging Face 下载 processor 配置）。

**需要观察的现象**：
1. `input_ids` 形状为 `(2, L_max)`，`L_max` 等于两条样本中较长者的长度；
2. `attention_mask` 是 `(2, L_max)` 的 0/1 矩阵，短样本那行的末尾有一串 0；
3. `position_ids` 形状 `(3, 2, L_max)`，且同一个位置三行数值相同（纯文本退化为普通 RoPE，承接 u5-l4）。

**预期结果**：`labels` 中非 -100 的位置恰好覆盖两条 assistant 回复（含 `<|im_end|>`），padding 区全部是 -100。具体 token 数值与 `L_max` **待本地验证**（取决于 tokenizer 对中文的分词）。

**踩坑提示**：常规路径会截断到 `tokenizer.model_max_length`。若你复用的 processor 该值很小（部分 processor 默认 512），长样本会被拦腰截断；观察 batch 形状时若发现 `L_max` 恰好等于某个整数上限，就要怀疑截断生效了。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `input_ids` 用 `pad_token_id` 填充而 `labels` 用 `-100`？统一用 `pad_token_id` 填 labels 行不行？

**答案**：`input_ids` 是模型的输入，必须全是合法词表 id，pad token 就是为此设计的合法占位符。`labels` 参与交叉熵计算，`-100` 是 `CrossEntropyLoss(ignore_index)` 的默认忽略值——padding 不是真实内容，绝不能让模型「学习预测 pad」。若用 `pad_token_id`（如 151643）填 labels，模型会被监督着在每个 pad 位置预测 pad，损失被大量无意义目标污染。

**练习 2**：常规 collator 里 `pad_and_cat` 给 position_ids 的 padding 位置填的是 1 而不是 0，会有问题吗？

**答案**：不会。这些位置对应 pad token，已被 0/1 attention_mask 从注意力中剔除，其 position id 取什么值都不参与有效计算；取 1 只是实现上的随意选择。这也提醒我们：position_ids 的正确性只对「真实 token 位置」有意义。

### 4.2 模块二：FlattenedDataCollator 拼接路径

#### 4.2.1 概念说明

拼接路径的核心想法：既然 padding 是把短样本「撑长」到最长，那不如反过来——把 batch 内所有样本**首尾相接拼成一条序列**，batch 维消失（恒为 1）。长度 12 和 35 的两条样本拼成一条 47 的序列，一个 token 都不浪费。

代价是语义问题：拼好的序列如果被模型当作「一条样本」做全局因果注意力，第二条样本就能看到第一条的内容和答案（标签泄漏）。因此拼接路径必须配套「分段注意力」——这正是模块三 cumsum attention_mask 的职责，也是 `train_qwen.py` 在开启开关时立刻执行注意力替换的原因。

数据侧的伏笔埋在 u5-l3：`_get_item` 返回的 `attention_mask` 并不是 0/1 掩码，而是一个长度列表：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L426-L427](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L426-L427)
`_get_item` 在样本字典里写入 `attention_mask = [seq_len]`——一个只含「本条样本长度」的列表。常规 collator 完全无视它（自己重新生成 0/1 掩码），只有 Flattened collator 会消费它。顺带一提，L429-L438 还 decode 了 `text` 与 `label` 两个调试字符串但并未返回，属于源码阅读时容易迷惑的「死代码」，不要在其上浪费推理。

#### 4.2.2 核心流程

```text
输入: instances = [ {input_ids(1,Lᵢ), labels(1,Lᵢ), position_ids(3,1,Lᵢ), attention_mask=[Lᵢ]}, ... ]

1. 取出四个键（比常规路径多一个 attention_mask）
2. 把各 instance 的 attention_mask 列表摊平串联 → [L₁, L₂, ..., L_B]
3. seq_lens = [0] + [L₁, ..., L_B]
4. cumsum_seq_lens = cumsum(seq_lens) → [0, L₁, L₁+L₂, ...]   ← 模块三主角
5. input_ids 沿 dim=1 拼接 → (1, ΣL)
   labels   沿 dim=1 拼接 → (1, ΣL)
   position_ids 沿 dim=2 拼接 → (3, 1, ΣL)
6. 视觉张量与常规路径完全同款：pixel_values / grid_thw 沿 dim=0 拼
7. 返回 batch，attention_mask 字段 = cumsum_seq_lens
```

注意与常规路径的两处关键差异：**没有任何 padding**，也**没有 model_max_length 截断**。

#### 4.2.3 源码精读

[qwen-vl-finetune/qwenvl/data/data_processor.py:L610-L628](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L610-L628)
这是拼接路径的心脏。`itertools.chain(*(...))` 把每个 instance 的 `attention_mask` 列表摊成一个扁平的长度列表（普通样本贡献 1 个长度；packed 桶贡献「桶内每条子样本各 1 个长度」，见模块四）；随后 `[0] + lengths` 补上首元素 0，`torch.cumsum` 得到累计长度向量。三个张量的拼接维度值得对照记忆：`input_ids` / `labels` 的形状是 `(1, L)` 所以沿 `dim=1` 拼；`position_ids` 的形状是 `(3, 1, L)` 所以沿 `dim=2` 拼——都是「序列维」。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L630-L635](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L630-L635)
batch 字典组装：`attention_mask=cumsum_seq_lens`——至此 attention_mask 这个字段名下存的已经不是掩码，而是「各段边界坐标」。这是一个刻意的**语义重载**：字段名沿用 HF Trainer 的标准键（模型 forward 签名里有 `attention_mask` 参数，Trainer 会原样透传），但值的含义由打补丁后的注意力实现重新定义。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L636-L675](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L636-L675)
视觉张量部分与常规 collator 逐行同构：收集含 `pixel_values` / `pixel_values_videos` 的 instance，沿 patch 维拼接、网格表沿 `dim=0` 拼接。区别只在于这些 patch 来自「同一条拼接序列里出现的所有图」。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L678-L689](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L678-L689)
`make_supervised_data_module` 是开关裁决处：`data_flatten` 或 `data_packing` 任一为真就用 Flattened collator，否则用常规 collator。两个开关定义在 [qwen-vl-finetune/qwenvl/train/argument.py:L16-L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L16-L17)，默认均为 False。配套动作在 [qwen-vl-finetune/qwenvl/train/train_qwen.py:L141-L142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L142)：只要任一开关开启，就立即 `replace_qwen2_vl_attention_class()` 替换注意力实现——不开补丁就开拼接，数据与模型之间的「暗号」就对不上了。

#### 4.2.4 代码实践

**实践目标**：让两条短样本分别过两种 collator，直观对比输出形状与 `attention_mask` 语义。

在 `collator_lab.py` 末尾追加（**示例代码**）：

```python
# ---- 拼接 collator ----
flat_collator = FlattenedDataCollatorForSupervisedDataset(tokenizer=processor.tokenizer)
flat = flat_collator([inst1, inst2])
L1 = inst1["attention_mask"][0]
L2 = inst2["attention_mask"][0]
print("两条样本长度:", L1, L2)
for k, v in flat.items():
    print(k, None if v is None else tuple(v.shape))
print("flat attention_mask:", flat["attention_mask"])
assert flat["attention_mask"].tolist() == [0, L1, L1 + L2], "cumsum 校验失败"
print("cumsum 校验通过 ✅")

# 文本级验证：拼接序列 = 样本1 全文 + 样本2 全文
text_cat = processor.tokenizer.decode(flat["input_ids"][0], skip_special_tokens=False)
text_12 = (processor.tokenizer.decode(inst1["input_ids"][0], skip_special_tokens=False)
           + processor.tokenizer.decode(inst2["input_ids"][0], skip_special_tokens=False))
assert text_cat == text_12
print("文本拼接校验通过 ✅")
```

**操作步骤**：接续 4.1.4 运行同一脚本。

**需要观察的现象**：
1. `flat["input_ids"]` 形状是 `(1, L1+L2)`，batch 维恒为 1；
2. `flat["attention_mask"]` 是一个长度为 3 的一维张量，形如 `[0, L1, L1+L2]`（例如两条样本长 40 与 48 时即 `[0, 40, 88]`）——**首元素为 0 的累计长度向量**，而非 0/1 矩阵；
3. `flat["position_ids"]` 形状 `(3, 1, L1+L2)`，且在 `L1` 处发生「位置回卷」：第二段的 position id 重新从 0 附近开始，而不是接着第一段继续增长。

**预期结果**：两条 assert 均通过；把 `flat["attention_mask"]` 与 4.1.4 常规路径的 0/1 矩阵并排打印，语义差异一目了然。具体 `L1`/`L2` 数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：拼接路径为什么不做 `model_max_length` 截断？截断会发生什么？

**答案**：源码事实是常规路径在 L554-L556 截断、拼接路径没有对应语句。设计解读：拼接序列的长度由「batch 内样本总长」或「packing 桶容量」决定，长度控制应前移到数据侧（pack_data.py 的桶容量 ≤ model_max_length，见 u4-l4）与 batch size 选择上；若在 collator 里硬截断，会把桶尾的样本拦腰斩断，labels 与 cu_seqlens 记录的段边界不再对齐，训练语义直接损坏。

**练习 2**：官方脚本 `sft_qwen3_4b.sh` 里 `--data_flatten True` 且 `per_device_train_batch_size 4`，一步优化器更新实际喂给模型几条「拼接序列」、每条含几个原始样本？

**答案**：DataLoader 每个 step 产出 batch_size=4 个 instance，Flattened collator 把这 4 条拼成 **1 条**拼接序列（形状 `(1, ΣL)`），即每个前向步 1 条序列、内含 4 个互不可见的段；再乘 `gradient_accumulation_steps 4`，一次优化器更新共消费 16 个原始样本、4 条拼接序列。

### 4.3 模块三：cumsum attention_mask 的设计与消费

#### 4.3.1 概念说明

cumsum 向量是拼接路径的「段边界坐标」，它存在的唯一目的就是充当 FlashAttention varlen 接口的 `cu_seqlens`。以两条样本 `[L₁=12, L₂=35]` 为例：

\[ \text{cumsum} = [0,\; 12,\; 47] \]

它把长度 47 的拼接序列切成两段：`[0,12)` 是样本一、`[12,47)` 是样本二。段内做因果注意力，**段间完全隔离**——第二条样本看不到第一条的任何 token，标签泄漏被机制性阻断。

这个设计的巧妙之处在于「语义重载」：HF 模型的 forward 签名里有 `attention_mask` 参数，Trainer 会把 batch 字典里的同名字段原样透传给模型。框架没有发明新字段名（那需要改模型签名），而是沿用旧键名、重定义其值——接收端由 monkey patch 替换后的注意力函数负责解释。

#### 4.3.2 核心流程

```text
数据侧（本讲）:
  各 instance 的 attention_mask 长度列表 → [0] + 列表 → cumsum → cu_seqlens

模型侧（u5-l6 精读，此处只看接口）:
  打补丁的 flash_attention_forward 收到 attention_mask（即 cu_seqlens）
  ├─ cu_seqlens = attention_mask                ← 直接当边界坐标用
  ├─ max_seqlen = max(cu_seqlens[i+1] - cu_seqlens[i])   ← 相邻差分的最大值
  └─ flash_attn_varlen_func(q, k, v,
          cu_seqlens_q=cu_seqlens, cu_seqlens_k=cu_seqlens,
          max_seqlen_q=max_seqlen, max_seqlen_k=max_seqlen, causal=True)
      → 每段独立因果注意力，段间零交互
```

为什么拼接 + varlen 比 padding 省算力？设 batch 内长度为 \(L_1, \dots, L_B\)，注意力计算量近似与「参与注意力的 token 数 × 序列长度」成正比：

\[ C_{\text{padding}} = B \cdot L_{\max}^2, \qquad C_{\text{varlen}} = \sum_{i=1}^{B} L_i^2 \]

取本讲例子 \(L = [12, 35]\)：padding 需 \(2 \times 35^2 = 2450\) 单位，varlen 只需 \(12^2 + 35^2 = 1369\) 单位；token 级（MLP 层）计算量也从 \(2 \times 35 = 70\) 降到 \(12+35=47\)。样本长度方差越大，收益越悬殊。注意：**前提是 varlen**——若不替换注意力、把拼接序列当单条序列做全局注意力，代价是 \((\sum_i L_i)^2\)，反而比 padding 更糟，且伴随标签泄漏。

#### 4.3.3 源码精读

[qwen-vl-finetune/qwenvl/data/data_processor.py:L615-L625](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L615-L625)
数据侧的 cumsum 构造：`chain` 摊平所有 instance 的长度列表，`[0]` 补首元素保证第一段从 0 开始，`torch.cumsum(..., dtype=torch.int32)` 输出累计边界。`int32` 是 FlashAttention 内核对 cu_seqlens 的类型要求。

[qwen-vl-finetune/qwenvl/train/trainer.py:L85-L104](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L85-L104)
消费端（预告 u5-l6）：`cu_seqlens = attention_mask` 一行直白到惊人——传进来的「掩码」被原样当作边界坐标；随后用相邻差分算出 `max_seqlen`（varlen 内核需要它预分配），最后 `flash_attn_varlen_func` 以 `causal=True` 对每段独立做因果注意力。段间隔离由此实现。同时注意 L82-L84 的 `squeeze(0)`：拼接路径 batch 维恒为 1，varlen 内核接收的是无 batch 维的二维张量。

[qwen-vl-finetune/qwenvl/train/train_qwen.py:L141-L142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L142)
契约的成立条件：`data_flatten or data_packing` 为真时才替换注意力类。数据侧产出 cumsum、模型侧按 cu_seqlens 解读，这两件事必须成对出现——这是使用本框架时最容易踩的隐式约束。

#### 4.3.4 代码实践

**实践目标**：亲手验证 cumsum 语义与「段隔离」的正确性前提。

在 `collator_lab.py` 再追加（**示例代码**）：

```python
# ---- cumsum 语义观察 ----
am = flat["attention_mask"]
lens = [int(am[i+1] - am[i]) for i in range(len(am) - 1)]
print("各段长度（差分恢复）:", lens)
assert lens == [L1, L2], "差分应还原原始长度"
assert flat["input_ids"].shape[1] == int(am[-1]), "总长 == cumsum 末元素"

# labels 的段隔离：每段各自保留自己的 assistant 回复
n1 = (inst1["labels"][0] != -100).sum().item()
n2 = (inst2["labels"][0] != -100).sum().item()
n_flat = (flat["labels"][0] != -100).sum().item()
assert n_flat == n1 + n2, "拼接不应丢失/新增监督位"
print(f"监督位: 样本1={n1}, 样本2={n2}, 拼接后={n_flat} ✅")
```

**操作步骤**：继续运行同一脚本。

**需要观察的现象**：
1. cumsum 差分 `am[i+1]-am[i]` 恰好还原 `[L1, L2]`——这正是 trainer.py 里 `max_seqlen` 的计算方式；
2. `input_ids` 总长等于 cumsum 末元素；
3. 拼接前后 labels 的非 -100 监督位数守恒。

**预期结果**：三条 assert 全部通过。若要进一步验证「段间注意力隔离」，需要真实模型 + flash_attn 环境，属于 u5-l6 的实践范围，本讲**待本地验证**（可先记下断言）。

#### 4.3.5 小练习与答案

**练习 1**：若开启 `data_flatten` 但忘记执行 `replace_qwen2_vl_attention_class()`（假设人为删掉 train_qwen.py L141-L142），会发生什么？

**答案**：两重错误。语义上，拼接序列被当作单条样本做全局因果注意力，后段样本能看到前段样本的问题与答案，造成训练标签泄漏；接口上，未打补丁的注意力实现期望 0/1 掩码（或以其构造 4D 因果掩码），收到长度为 B+1 的 cumsum 向量后，掩码维度与序列长度不匹配，轻则报错、重则广播出错误结果（具体行为随 transformers 版本而异，待本地验证）。结论：数据侧 cumsum 与模型侧 varlen 补丁是同一契约的两半，必须成对存在。

**练习 2**：cumsum 向量为什么要补首元素 0？为什么用 `int32` 而不是默认的 `int64`？

**答案**：FlashAttention varlen 接口用「相邻边界之差」界定每段，第一段的起点必须显式给出，`[0] + lens` 保证差分后第一条段长就是 `L₁`；不补 0 的话第一段会被漏掉或算错。`dtype=torch.int32` 是 flash_attn 内核对 cu_seqlens 的类型约定（ trainer.py 中同样直接使用该向量），显式指定避免类型不匹配。

### 4.4 模块四：packed 样本的递归处理与两个开关的区分

#### 4.4.1 概念说明

先厘清两个开关（本讲学习目标中最易混淆的点）：

| | `data_flatten` | `data_packing` |
| --- | --- | --- |
| 定义位置 | [argument.py:L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L16) | [argument.py:L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L17) |
| 拼接发生的阶段 | **collator 阶段**（运行时，把 batch 内 B 条独立样本拼成 1 条） | **离线装箱**（u4-l4 的 pack_data.py 按 token 预算把样本装进桶）+ `__getitem__` 阶段桶内拼接 + collator 阶段桶间拼接 |
| 标注文件形态 | 普通的单层样本列表 | 两层列表：外层是桶，内层是桶内样本 |
| 使用的 collator | FlattenedDataCollator | FlattenedDataCollator（同一个） |
| 是否需要 varlen 补丁 | 是 | 是 |
| 长度控制手段 | 靠 batch size 与样本天然长度 | 靠 pack 桶容量（装箱时保证 ≤ model_max_length） |

一句话总结：**flatten 是「运行时把一个 batch 拼起来」，packing 是「离线先把样本装进桶，运行时桶内桶间再拼」**。两者殊途同归于同一个 Flattened collator 与同一套 cu_seqlens 机制。

packing 模式下 dataset 的取样函数在初始化时就换掉了：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L304-L307](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L304-L307)
`data_packing` 为真时 `item_fn = _get_packed_item`，否则 `_get_item`。`__getitem__`（L345-L387）统一通过 `self.item_fn(sources)` 取样，重试容错逻辑对两种模式一视同仁。

#### 4.4.2 核心流程

`_get_packed_item` 收到的 `sources` 是一个桶（样本字典的列表）：

```text
输入: sources = [ann1, ann2, ann3]        ← 一个 pack 桶（两层列表的内层展开）

1. 对每个 ann 调 _get_item([ann])          ← 递归复用单样本路径
   得到 data_list = [d1, d2, d3]
2. input_ids     沿 dim=1 拼 → (1, L1+L2+L3)   ← 桶内第一级拼接
   labels        沿 dim=1 拼
   position_ids  沿 dim=2 拼
   attention_mask = [L1, L2, L3]               ← 桶内每段各记一个长度
3. pixel_values / image_grid_thw 沿 dim=0 跨子样本拼接（有图才进字典）
   pixel_values_videos / video_grid_thw 同理
4. 返回「一个桶 = 一条拼接序列」的 instance 给 DataLoader

随后 collator 阶段（模块二）: 多个桶的长度列表再被 chain + cumsum → 第二级拼接
```

所以 packing 模式的段边界信息经过两级收集：`_get_packed_item` 先在桶内攒出 `[L1, L2, L3]`，Flattened collator 再把 batch 内各桶的列表串起来统一 cumsum。**最终 cu_seqlens 精确到每一条原始样本**，段隔离粒度不因装箱而丢失。

#### 4.4.3 源码精读

[qwen-vl-finetune/qwenvl/data/data_processor.py:L442-L459](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L442-L459)
函数入口先做类型分诊。注意 L444-L448 的 `isinstance(sources, dict)` 分支里引用了未定义的变量名 `source`（L445）——若真有一个未装箱的 dict 样本混进 packing 模式，这一行会抛 `NameError`，随后被 `__getitem__` 的重试机制吞掉并跳到相邻样本（u5-l3 讲过的静默容错）。实践中 `_pack.json` 的条目都是列表，该分支基本是死代码，但阅读时不要被它迷惑——正常路径走 L450 起的 list 分支：逐个样本包成 `[source]` 后 `assert len(source) == 1`（两处 FIXME 注释表明作者对「为什么要再包一层」存疑），递归调用 `_get_item`。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L461-L472](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L461-L472)
桶内第一级拼接：三个张量沿序列维 cat（维度号与模块二一致）；`attention_mask` 则收集每个子样本的 `d["attention_mask"][0]`（即各自长度），组成列表 `[L1, L2, ...]`——这正是模块二里 `itertools.chain` 能摊平的对象。若列表为空则显式置 `None`。

[qwen-vl-finetune/qwenvl/data/data_processor.py:L474-L517](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L474-L517)
视觉张量拼接：只要桶内**任何**子样本有 `pixel_values`，就把有图的子样本沿 `dim=0`（patch 维）cat 成桶级大张量，`image_grid_thw` 同步拼表；视频走对称的 L496-L516。与 collator 的区别：这里拼的是「桶内子样本」，collator 拼的是「batch 内各桶」——两级拼接的视觉张量维度操作完全相同。

#### 4.4.4 代码实践

**实践目标**：手工模拟一个 pack 桶，验证「桶内拼接 + collator 二级拼接」后 cu_seqlens 精确到每条原始样本。

在 `collator_lab.py` 末尾追加（**示例代码**）：

```python
# ---- 模拟 pack 桶：复刻 _get_packed_item 的桶内拼接（L461-L472）----
def make_bucket(instances):
    return {
        "input_ids":     torch.cat([d["input_ids"]     for d in instances], dim=1),
        "labels":        torch.cat([d["labels"]        for d in instances], dim=1),
        "position_ids":  torch.cat([d["position_ids"]  for d in instances], dim=2),
        "attention_mask":[d["attention_mask"][0] for d in instances],  # [L1, L2, ...]
    }

inst3 = make_instance([
    {"from": "human", "value": "1+1=?"},
    {"from": "gpt",   "value": "1+1=2。"},
])
bucket = make_bucket([inst1, inst2])          # 桶内两条子样本
flat2 = flat_collator([bucket, inst3])        # batch = [桶, 独立样本]

L3 = inst3["attention_mask"][0]
print("二级拼接 attention_mask:", flat2["attention_mask"])
assert flat2["attention_mask"].tolist() == [0, L1, L1+L2, L1+L2+L3]
assert flat2["input_ids"].shape[1] == L1 + L2 + L3
print("packing 两级拼接校验通过 ✅ 段粒度 = 每条原始样本")
```

**操作步骤**：继续运行同一脚本。

**需要观察的现象**：`flat2["attention_mask"]` 形如 `[0, L1, L1+L2, L1+L2+L3]`——batch 只有 2 个 instance（1 桶 + 1 独立样本），但 cumsum 长度是 4，因为桶自己在 `attention_mask` 里贡献了 2 个长度。

**预期结果**：assert 通过；对照 4.2.4 的 `[0, L1, L1+L2]`，体会 `itertools.chain` 摊平嵌套长度的作用。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`data_flatten=True`（未开 packing）时，一个 batch 的段数是多少？`data_packing=True` 且 batch_size=2、每桶平均 8 条子样本时呢？

**答案**：flatten：段数 = batch 内样本数 = `per_device_train_batch_size`，每段是一条完整原始样本。packing：段数 = 2 个桶 × 各自子样本数之和（平均约 16 段），cumsum 长度 = 段数 + 1。两种模式下 batch 张量的 batch 维都是 1。

**练习 2**：为什么 `_get_packed_item` 不把桶内子样本的 `attention_mask` 相加成一个总长，而是保留每段长度？

**答案**：段边界必须精确到每条原始样本，varlen 注意力才能做到「桶内样本彼此隔离」。若只记总长，整个桶会被当成一条序列做全局注意力，桶内后段样本能看到前段样本的问答——恰恰丢掉了 packing 想保住的训练正确性。collator 端 `itertools.chain` 摊平 + cumsum 的设计，就是为了把任意嵌套层级的段长度无损地传递给注意力内核。

**练习 3**：想在不改数据文件的前提下提高训练吞吐，选 flatten 还是 packing？若样本长度极度不均衡（10 到 4000 token）呢？

**答案**：改一个启动开关即可的是 flatten——零离线成本，立竿见影消除 batch 内 padding 浪费。但长度极度不均衡时 flatten 的拼接总长方差大（一个 batch 可能撞出超长序列），且「长短样本装进同一 batch」的组合不受控；packing 靠离线装箱把每桶总长控制在预算内（约 4096 token），长度均匀、填充率高，是多模态长样本场景的更优解（代价是要跑 pack_data.py 预处理，见 u4-l4）。

## 5. 综合实践

**任务：用一份 20 行以内的对照实验，量化 padding 与 flatten 的浪费率。**

1. **准备**：复用 `collator_lab.py` 的实验台，把 `inst1`/`inst2` 换成一短一长两条样本（如 3 字问答 vs 一段 100 字左右的描述+长回复）。
2. **测量 padding 浪费**：过常规 collator，记录 `L_max`、batch 形状 `(2, L_max)`，计算 padding 无效 token 数 \( 2L_{\max} - (L_1+L_2) \) 与占比。
3. **测量 flatten 收益**：过 Flattened collator，确认 `input_ids` 总长恰为 \(L_1+L_2\)，无任何填充；用 4.3.2 的公式分别算两种路径的注意力计算量 \( 2L_{\max}^2 \) 与 \( L_1^2 + L_2^2 \)，写出节省百分比。
4. **验证正确性不变**：对比两种路径下 labels 非 -100 监督位的总数（应相等）、以及 flatten 路径 position_ids 在段边界处的回卷现象（第二段三行 position id 从小值重新开始）。
5. **记录**：把形状、attention_mask、浪费率、注意力节省率写成一张表，存入你的学习笔记，并在表下用自己的话回答——「为什么 flatten 必须搭配 varlen 注意力才能省算力？只拼接不打补丁会怎样？」

预期成果：一张能量化对比两种 collator 的实验表 + 一段对 cumsum 语义的独立解释。运行数值部分**待本地验证**。

## 6. 本讲小结

- 常规 `DataCollatorForSupervisedDataset`：`pad_sequence` 把 batch 对齐成 `(B, L_max)` 矩形张量，input 填 `pad_token_id`、labels 填 `-100`、attention_mask 是标准 0/1 掩码，并且是唯一做 `model_max_length` 截断的路径。
- `FlattenedDataCollatorForSupervisedDataset`：把 batch 内样本沿序列维拼成 `(1, ΣL)` 单条序列，无 padding、无截断，视觉张量照常跨样本沿 patch 维拼接。
- attention_mask 被语义重载：拼接路径下它变身为「首元素 0 的累计段长向量」（cu_seqlens），由打补丁的 `flash_attn_varlen_func` 按 `causal=True` 逐段做注意力，段间零交互——这是拼接不泄漏标签的机制保证。
- `data_flatten` 是运行时 batch 级拼接；`data_packing` 是离线装箱 + `_get_packed_item` 桶内拼接 + collator 桶间拼接的两级方案；两者共用同一个 Flattened collator，且都必须搭配 `replace_qwen2_vl_attention_class()`。
- 收益来自消项：注意力计算量从 \(B \cdot L_{\max}^2\) 降为 \(\sum_i L_i^2\)，token 级计算量从 \(B \cdot L_{\max}\) 降为 \(\sum_i L_i\)，样本长度方差越大收益越高。

## 7. 下一步学习建议

下一讲 **u5-l6（trainer.py：FlashAttention varlen 替换与 monkey patch）** 是本讲的自然续篇：本讲只展示了 `cu_seqlens = attention_mask` 这一行消费端接口，u5-l6 将逐行精读 `flash_attention_forward` 的完整实现、`replace_qwen2_vl_attention_class` 如何跨四类模型模块做运行时替换、`create_causal_mask` 为何被短路，以及 `create_optimizer` 的多参数组改造。建议阅读顺序：先重读本讲 4.3.3 引用的 trainer.py L85-L104，再进入下一讲全文。之后可回看 u4-l4 的 pack_data.py，体会「离线装箱预算 ≤ model_max_length」与本讲「拼接路径不截断」之间的呼应。
