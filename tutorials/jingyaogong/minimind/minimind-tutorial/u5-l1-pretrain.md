# 预训练：从 0 学会词语接龙

> 对应训练脚本：`trainer/train_pretrain.py`
> 对应数据集：`dataset/lm_dataset.py` 中的 `PretrainDataset`
> 对应公共工具：`trainer/trainer_utils.py`

## 1. 本讲目标

读完本讲后，你应该能够：

- 说清「预训练」到底在让模型学什么，以及它与 SFT、DPO、RL 的区别。
- 读懂 `train_pretrain.py` 从命令行参数到训练循环的 9 步初始化装配过程。
- 逐行解释 `train_epoch` 中**一个 step** 内部发生了什么（前向、损失、反向、梯度累积、更新、保存、日志）。
- 看懂训练日志里 `loss / logits_loss / aux_loss / lr` 四个数字分别代表什么、怎么算出来的。
- 独立用 `pretrain_t2t_mini.jsonl` 跑一段预训练、记录 loss 曲线，并用 `eval_llm.py --weight pretrain` 看续写效果。

## 2. 前置知识

本讲默认你已经掌握前置讲义的内容，这里只做最短的「接线」回顾：

- **u2-l2（数据集与标签）**：你已经知道 MiniMind 用 `-100` 或 `loss_mask` 标注「序列里哪些位置参与 loss」。本讲的 `PretrainDataset` 是其中最简单的一种——除了 padding，整段文本都参与 loss。
- **u3-l5（CausalLM 前向与交叉熵）**：你已经知道 `MiniMindForCausalLM.forward` 用位移交叉熵（`logits[:-1]` 对 `labels[1:]`）算损失，并把结果装进 `MoeCausalLMOutputWithPast(loss=..., aux_loss=...)`。本讲会调用它。
- **u4-l1（训练公共工具）**：你已经知道 `get_lr`（余弦退火）、`init_model`（加载/初始化权重）、`SkipBatchSampler`（断点续训）、`init_distributed_mode`（自动探测 DDP）。本讲直接使用它们。
- **u4-l3（DDP / 混合精度 / 梯度累积）**：你已经知道训练底座的更新块顺序 `unscale_ → clip_grad_norm_ → scaler.step → scaler.update → zero_grad`。本讲的 `train_epoch` 就是这套底座的具体实例。

两个本讲会反复用到的术语：

- **next-token prediction（下一个 token 预测）**：给定上文，预测下一个词。这是几乎所有现代自回归语言模型（GPT、Qwen、MiniMind）的预训练目标。
- **词语接龙 / 文本续写**：next-token prediction 的通俗说法。预训练的本质，就是让模型在超大规模纯文本上反复玩「词语接龙」，从而学会语言的统计规律和世界知识。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [trainer/train_pretrain.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py) | 预训练主脚本 | `train_epoch` 函数 + `__main__` 的 9 步装配 |
| [dataset/lm_dataset.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py) | 数据集定义 | `PretrainDataset` 类（jsonl → 张量） |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | 训练公共工具 | `get_lr` / `init_model` / `lm_checkpoint` / `SkipBatchSampler` / `init_distributed_mode` |
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | 模型结构 | `MiniMindForCausalLM.forward` 的损失返回（u3-l5 已讲，本讲只调用） |

数据流向一句话总结：

```
pretrain_t2t_mini.jsonl  ──PretrainDataset──▶  (input_ids, labels)
                                                       │
                                          model(input_ids, labels=labels)
                                                       │
                                  res.loss + res.aux_loss  ──▶  backward ──▶  optimizer.step
```

## 4. 核心概念与源码讲解

### 4.1 预训练范式：next-token prediction 与损失组成

#### 4.1.1 概念说明

预训练（Pretrain）是大模型生命周期的**第一阶段**，也是唯一一个**不需要任何人工标注、完全无监督**的阶段。它喂给模型的是海量纯文本（新闻、百科、小说、代码……），目标是学会一件事：**词语接龙**。

为什么这件看似简单的事如此重要？因为要准确预测「下一个词」，模型必须隐式地学会语法、常识、事实、推理风格——**一个能压低续写损失的语言模型，几乎必然已经掌握了大量「知识」**。这就是 next-token prediction 作为预训练目标的威力：

\[ \mathcal{L}_{\text{CE}} = -\frac{1}{|S|}\sum_{t \in S} \log P(x_{t+1} \mid x_{\le t};\, \theta) \]

其中 \(S\) 是参与 loss 的位置集合，\(x_{t+1}\) 是真实下一个 token，\(P(\cdot)\) 是模型经 softmax 输出的词表分布，\(\theta\) 是模型参数。

预训练与后续阶段的对比：

| 阶段 | 数据形态 | 学什么 | 监督信号 |
|------|---------|--------|---------|
| **Pretrain（本讲）** | 纯文本 `{"text": "..."}` | 通用语言能力 + 世界知识 | 整段文本自身（无标注） |
| SFT（u5-l2） | 多轮对话 | 助手回答风格 / 模板 | 只有 assistant 段参与 loss |
| DPO / RL（u7） | 偏好 / 奖励 | 对齐人类偏好 | 偏好排序 / 奖励分数 |

一句话：**预训练让模型「会说话」，后续阶段让它「说有用的话」**。

#### 4.1.2 核心流程

预训练的一个 step 可以抽象为：

1. 从 `PretrainDataset` 取出一个 batch 的 `(input_ids, labels)`。
2. 模型前向，得到每个位置对词表的预测分布。
3. 用位移交叉熵算出 `loss`（语言建模主损失）。
4. 如果是 MoE 模型，额外得到 `aux_loss`（专家负载均衡损失，u3-l4）。
5. 总损失 `total_loss = loss + aux_loss`，反向传播更新参数。

#### 4.1.3 源码精读

总损失的组合发生在 `train_epoch` 的前向之后：

[trainer/train_pretrain.py:35-38](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L35-L38) —— 这三行是本讲损失逻辑的「心脏」：在混合精度的上下文里跑一次前向，把交叉熵损失 `res.loss` 与 MoE 负载均衡损失 `res.aux_loss` 相加，再除以梯度累积步数（为了在累积梯度时让数学期望等价于大 batch）。

其中 `res` 是模型前向返回的 `MoeCausalLMOutputWithPast`，它的两个字段在模型侧是这样算出来的：

[model/model_minimind.py:250-253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L250-L253) —— `loss` 是位移交叉熵（`ignore_index=-100` 屏蔽 padding），`aux_loss` 来自躯干 `MiniMindModel`。关键设计：**Dense 模型的 `aux_loss` 是一个与 hidden_states 同设备同 dtype 的标量 0**（在 [model/model_minimind.py:231](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L231) 用 `sum(..., new_zeros)` 兜底实现），所以 `res.loss + res.aux_loss` 这一行**对 Dense / MoE 完全通用**，不需要 `if use_moe` 分支——这是 MiniMind 代码简洁性的一个缩影。

#### 4.1.4 代码实践

**实践目标**：动手验证「位移交叉熵 = 给定上文预测下一个 token」这一直觉。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 打开 [model/model_minimind.py:250-252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L250-L252)。
2. 手动在一个 4 token 的小例子上推演：设 `input_ids = [A, B, C, D]`，`labels = [A, B, C, D]`。则 `x = logits[..., :-1, :]` 取位置 0,1,2（A,B,C 的输出），`y = labels[..., 1:]` 取位置 1,2,3（B,C,D）。
3. 配对：(A 的输出 → 预测 B)、(B 的输出 → 预测 C)、(C 的输出 → 预测 D)。

**需要观察的现象**：每个位置 i 的 logits 永远在「预测下一个位置 i+1 的真实 token」，这正是 next-token prediction。

**预期结果**：你会确认模型并没有在第 0 个位置就「看到」B，而是用 A 的表示去预测 B——这就是自回归。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `labels` 里 padding 位置要设成 `-100`？

**参考答案**：因为 `F.cross_entropy(..., ignore_index=-100)` 会跳过这些位置不计入 loss。padding 是为了让一个 batch 内不同长度序列能拼成矩形张量，它不是真实文本，不该让模型学习「预测 padding」。

**练习 2**：Dense 模型下，`res.aux_loss` 的值是多少？`total_loss = res.loss + res.aux_loss` 会出错吗？

**参考答案**：值是 0（一个与 hidden_states 同 dtype 同设备的标量，靠 `new_zeros` 兜底）。不会出错，因为 0 与任何 loss 相加都不改变结果，且 dtype/设备一致，无需类型转换。

---

### 4.2 PretrainDataset：把纯文本变成训练张量

#### 4.2.1 概念说明

预训练数据长什么样？根据 README 的格式说明（[README.md:413-419](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L413-L419)），每一行就是一个 JSON，只有一个 `text` 字段：

```jsonl
{"text": "如何才能摆脱拖延症？治愈拖延症并不容易……"}
{"text": "清晨的阳光透过窗帘洒进房间……"}
```

**没有问题、没有回答、没有对话角色**——就是一坨连续文本。`PretrainDataset` 的职责，就是把这种文本变成模型能吃的 `(input_ids, labels)` 两个张量。它是所有 Dataset 类里最「懒」也最简单的一个：**整段文本都参与 loss**，只把 padding 屏蔽掉。

#### 4.2.2 核心流程

`PretrainDataset.__getitem__(index)` 的处理流程：

```
sample['text']（一段纯文本）
      │  tokenizer(add_special_tokens=False, truncation=True, max_length=max_length-2)
      ▼
tokens（中间一段 token id，不含 bos/eos，最多 max_length-2 个）
      │  首尾加 bos / eos
      ▼
[bos] + tokens + [eos]
      │  尾部补 pad 到 max_length
      ▼
input_ids（定长 max_length 的 LongTensor）
      │  clone 后把 pad 位置改 -100
      ▼
labels（与 input_ids 同形状，pad 位置为 -100）
```

为什么截断长度是 `max_length - 2`？**因为要预留 2 个位置给手动加的 bos 和 eos**，保证 `bos + 正文 + eos` 总长度恰好不超过 `max_length`。

#### 4.2.3 源码精读

[dataset/lm_dataset.py:37-55](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L37-L55) —— `PretrainDataset` 全貌。逐行解读 `__getitem__`：

- [dataset/lm_dataset.py:49](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L49)：`add_special_tokens=False` 表示**不让分词器自动加 bos/eos**——因为我们要手动控制它们的放置；`max_length=self.max_length - 2` + `truncation=True` 表示正文超长就截断，留 2 个槽位。
- [dataset/lm_dataset.py:50](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L50)：手动在首尾包上 `bos_token_id`（`<|im_start|>`）和 `eos_token_id`（`<|im_end|>`）。这给了每段文本一个明确的「开始」和「结束」信号，对后续 SFT 复用同一对标记很重要。
- [dataset/lm_dataset.py:51](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L51)：尾部补 `pad_token_id`（即 `<|endoftext|>`）到定长。
- [dataset/lm_dataset.py:53-54](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L53-L54)：`labels = input_ids.clone()` 然后**只把 pad 位置改成 -100**。注意：与 SFT 不同，这里 bos / 正文 / eos **全部保留为真实 token id**，都会参与 loss。

[dataset/lm_dataset.py:42](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L42)：用 HuggingFace `datasets.load_dataset('json', ...)` 一次性把整个 jsonl 加载进内存（内存映射 + 缓存），后续 `__getitem__` 只是按 index 取已加载的样本，速度很快。

#### 4.2.4 代码实践

**实践目标**：亲眼看到一条预训练样本的 `input_ids` 与 `labels` 长什么样。

**操作步骤**（在项目根目录新建一个临时脚本 `inspect_pretrain.py`，**不要提交它**，看完即删）：

```python
# 示例代码：仅供阅读理解，非项目原有文件
import sys, os
sys.path.append('.')
from transformers import AutoTokenizer
from dataset.lm_dataset import PretrainDataset

tok = AutoTokenizer.from_pretrained('./model')
ds = PretrainDataset('./dataset/pretrain_t2t_mini.jsonl', tok, max_length=64)
input_ids, labels = ds[0]
for i, (x, y) in enumerate(zip(input_ids.tolist(), labels.tolist())):
    print(f'{i:3d}  x={tok.decode([x])!r:12s}  label={tok.decode([y]) if y!=-100 else -100!r}')
```

**需要观察的现象**：序列以 `<|im_start|>` 开头、`<|im_end|>` 结尾，末尾紧跟若干 `<|endoftext|>`（padding）。

**预期结果**：所有非 padding 位置的 `label` 与 `input_id` 完全相同；只有 padding 位置的 `label` 显示为 `-100`。这验证了「整段文本参与 loss、只屏蔽 padding」。

> 若本机无数据或无 GPU，可只阅读 [dataset/lm_dataset.py:47-55](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L47-L55) 推演上述结果，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `max_length` 设得比正文还短，会发生什么？

**参考答案**：`max_length - 2` 会进一步缩小正文截断阈值，正文会被截断到该长度，超出部分丢失；bos/eos 仍会被加上，总长度仍是 `max_length`。

**练习 2**：为什么 `PretrainDataset` 不像 `SFTDataset` 那样需要 `bos_id`/`eos_id` 锚点匹配？

**参考答案**：因为预训练目标是对**整段文本**做 next-token prediction，没有「只学回答、不学提问」的需求；而 SFT 必须只在 assistant 段算 loss，才需要靠锚点子串匹配定位 assistant 段（见 u2-l2）。

---

### 4.3 main 训练主流程：9 步初始化与装配

#### 4.3.1 概念说明

`train_pretrain.py` 的 `__main__` 部分用注释把启动流程划成了 **9 个编号步骤**（`# ========== 1. ... ==========`）。这是一种很好的工程习惯：**把「准备阶段」和「训练阶段」清晰分开**。准备阶段的产物（模型、数据、优化器、检查点状态、分布式包装）在步骤 8 的训练循环里被一次性消费。

理解这 9 步的顺序非常重要，因为其中存在**严格的依赖关系**——比如「分布式初始化（步骤 1）」必须在「加载检查点（步骤 6）」之前，才能正确换算跨 GPU 的 step；「`torch.compile`（步骤 7 上）」必须在「DDP 包装（步骤 7 下）」之前。

#### 4.3.2 核心流程

9 步的主线：

```
1. 初始化分布式 + 随机种子        ──▶ local_rank, seed
2. 目录/模型配置/探测续训检查点    ──▶ lm_config, ckp_data
3. 设置混合精度上下文             ──▶ autocast_ctx
4. 配 wandb/swanlab              ──▶ wandb
5. 模型 + 数据 + 优化器           ──▶ model, train_ds, scaler, optimizer
6. 从检查点恢复状态               ──▶ start_epoch, start_step
7. torch.compile + DDP 包装       ──▶ 包装后的 model
8. for epoch: 训练循环            ──▶ 调用 train_epoch
9. barrier + 销毁进程组           ──▶ 干净退出
```

#### 4.3.3 源码精读

下面按步骤给出关键代码点（行号均为当前 HEAD）：

- **步骤 1：初始化环境和随机种子** —— [trainer/train_pretrain.py:109-112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L109-L112)：`init_distributed_mode()` 自动探测是否在 `torchrun` 下运行；若多卡则把 `device` 改成 `cuda:{local_rank}`；种子设为 `42 + rank`，**多卡时每张卡种子不同**以保留数据并行多样性（详见 u4-l1）。
- **步骤 2：配置与检查点探测** —— [trainer/train_pretrain.py:114-117](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L114-L117)：构造 `MiniMindConfig`；当 `--from_resume 1` 时，以**只读模式**调用 `lm_checkpoint`（不传 model），它会返回 `ckp_data` 或 `None`。
- **步骤 3：混合精度** —— [trainer/train_pretrain.py:119-122](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L119-L122)：默认 `bfloat16`；CPU 时退化为 `nullcontext()`（不混合精度）。这个 `autocast_ctx` 在 `train_epoch` 里被反复复用。
- **步骤 5：定义模型、数据、优化器** —— [trainer/train_pretrain.py:133-138](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L133-L138)：`init_model` 负责加载/初始化权重并打印参数量；`GradScaler` 的 `enabled=(dtype=='float16')`，bf16 时它是一个**被禁用的空壳**（scale/unscale/step 全是 no-op）。注意默认 `--from_weight none`，即**从随机初始化开始**训练。
- **步骤 6：从检查点恢复** —— [trainer/train_pretrain.py:140-147](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L140-L147)：把 `ckp_data` 里的 model/optimizer/scaler 状态装回去，并取出 `start_epoch`、`start_step`。
- **步骤 7：编译与分布式包装** —— [trainer/train_pretrain.py:149-154](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L149-L154)：顺序固定——**先 `torch.compile`，后 DDP 包装**（u4-l3 已解释为何不能反过来）。
- **步骤 8：训练循环** —— [trainer/train_pretrain.py:156-167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L156-L167)：每个 epoch 先 `set_epoch` 重洗 DistributedSampler，单卡时则用 `torch.randperm` 生成随机索引；用 `SkipBatchSampler(skip=start_step)` 跳过已训练 batch（断点续训），并把 `iters = len(loader) + skip` 传给 `train_epoch`，保证学习率曲线连续不错位。

命令行参数的默认值（[trainer/train_pretrain.py:84-107](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L84-L107)）也值得记一下，它们决定了「开箱即用」的训练规模：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--hidden_size` | 768 | 隐藏维度（决定 ~64M Dense 模型） |
| `--num_hidden_layers` | 8 | 层数 |
| `--max_seq_len` | 340 | 单样本截断长度 |
| `--batch_size` | 32 | 每卡 batch |
| `--accumulation_steps` | 8 | 梯度累积步数（有效 batch = 32×8×world_size） |
| `--learning_rate` | 5e-4 | 初始学习率（余弦退火起点） |
| `--epochs` | 2 | 训练轮数 |
| `--dtype` | bfloat16 | 混合精度类型 |
| `--data_path` | `../dataset/pretrain_t2t_mini.jsonl` | 默认 mini 数据 |
| `--from_weight` | none | 从随机初始化开始 |

#### 4.3.4 代码实践

**实践目标**：在不实际训练的前提下，验证「步骤 8 的装配顺序」并理解断点续训的 `iters` 计算。

**操作步骤**：

1. 打开 [trainer/train_pretrain.py:156-167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L156-L167)。
2. 回答：当 `skip > 0` 时，为什么传给 `train_epoch` 的 `iters` 是 `len(loader) + skip` 而不是 `len(loader)`？
3. 对照 `get_lr` 的公式（[trainer/trainer_utils.py:40-41](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L40-L41)），它用的是全局步数 `epoch * iters + step`。

**需要观察的现象 / 预期结果**：`SkipBatchSampler` 让本轮实际产出的 batch 数减少为 `len(loader)`，但全局步数必须按「未中断时应有的总步数」`len(loader) + skip` 来算，学习率才会接续在中断时的曲线上，而不是从头开始退火。这正是 u4-l1 讲过的「让 lr 曲线连续」的设计。

#### 4.3.5 小练习与答案

**练习 1**：把步骤 7 的两行（`torch.compile` 与 `DistributedDataParallel`）调换顺序，会出什么问题？

**参考答案**：`torch.compile` 期望作用于「原始模型」以追踪计算图；若先包 DDP，再 compile 一个 DDP 对象，追踪会和 DDP 的 hook/梯度同步逻辑冲突。MiniMind 因此固定「先 compile 后 DDP」。

**练习 2**：默认配置下，单卡一个 epoch 的有效 batch size 是多少？

**参考答案**：`batch_size(32) × accumulation_steps(8) × world_size(1) = 256`。

---

### 4.4 train_epoch：训练循环的最小单元

#### 4.4.1 概念说明

`train_epoch` 是整个预训练脚本里**信息密度最高**的函数。它把 u4-l3 讲的那套「DDP + 混合精度 + 梯度累积」底座完整落地。理解了它，你也就理解了 SFT、LoRA、DPO 等后续几乎所有训练脚本的循环骨架——它们的 `train_epoch` 几乎是同一份模板。

函数签名：

[trainer/train_pretrain.py:24](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L24) —— 接收「第几个 epoch、数据 loader、总步数 iters、起始步 start_step、wandb 句柄」。`start_step` 用于断点续训时让日志/保存的步数编号连续。

#### 4.4.2 核心流程

一个 step 内部的时序（以 `accumulation_steps=8` 为例）：

```
for step, (input_ids, labels) in loader:
 │
 ├─ ① 设置本步学习率（get_lr → 写回 optimizer.param_groups）
 ├─ ② 前向（autocast）→ res = model(input_ids, labels)
 ├─ ③ loss = (res.loss + res.aux_loss) / accumulation_steps
 ├─ ④ scaler.scale(loss).backward()      # 梯度累积到 .grad
 │
 ├─ ⑤ 若 step % 8 == 0：                    # 凑够一个更新块
 │      unscale_ → clip_grad_norm_ → step → update → zero_grad
 │
 ├─ ⑥ 若到 log_interval：打印 loss/logits_loss/aux_loss/lr/eta
 ├─ ⑦ 若到 save_interval（且主进程）：存推理权重 + 续训检查点
 └─ ⑧ 循环结束后：对尾部不足 8 步的累积梯度补一次更新
```

学习率调度公式（u4-l1 已推导，这里给出曲线端点）：

\[ \text{lr}(s) = \text{lr}_0 \cdot \left(0.1 + 0.45\left(1 + \cos\frac{\pi s}{S}\right)\right),\quad \text{lr}(0)=\text{lr}_0,\ \text{lr}(S)=0.1\,\text{lr}_0 \]

即学习率从 `lr` 余弦平滑下降到 `0.1×lr`，**不是降到 0**。

#### 4.4.3 源码精读

逐块解读 `train_epoch`：

- **设置学习率** —— [trainer/train_pretrain.py:31-33](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L31-L33)：全局步数 `epoch * iters + step` 驱动 `get_lr`，结果**手动写回** `optimizer.param_groups`（MiniMind 不用 `lr_scheduler`）。每一步都更新一次。
- **前向 + 损失** —— [trainer/train_pretrain.py:35-38](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L35-L38)：已在 4.1.3 解读。注意 `autocast_ctx` 只包住前向，**反向 `backward` 在 autocast 之外**（标准做法）。
- **反向** —— [trainer/train_pretrain.py:40](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L40)：`scaler.scale(loss).backward()`，梯度累加进 `.grad`。
- **更新块** —— [trainer/train_pretrain.py:42-49](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L42-L49)：每 `accumulation_steps` 步执行一次。顺序严格为 `unscale_ → clip_grad_norm_ → step → update → zero_grad`（u4-l3 强调过，顺序不能乱）。
- **日志打印** —— [trainer/train_pretrain.py:51-59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L51-L59)：这是本讲最重要的「读日志」环节，见下方专门解读。
- **保存检查点** —— [trainer/train_pretrain.py:61-71](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L61-L71)：仅主进程执行；先 `model.eval()` 再切回 `model.train()`；推理权重保存为 fp16；剥 `.module`（DDP）和 `._orig_mod`（compile）拿到原始 state_dict。
- **尾部补更新** —— [trainer/train_pretrain.py:75-80](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L75-L80)：一个 epoch 的总步数未必是 `accumulation_steps` 的整数倍，最后不足 8 步累积的梯度不会在循环内被 step 掉，需要在这里补一次更新，否则最后一小块梯度会被浪费。

**重点：怎么读预训练日志**

[trainer/train_pretrain.py:53-56](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L53-L56) 定义了日志里四个关键数字的来源：

```
current_loss       = loss.item() * accumulation_steps   # 还原成「未除以累积步数」的真实损失
current_aux_loss   = res.aux_loss.item()                # MoE 负载均衡损失（Dense 为 0）
current_logits_loss = current_loss - current_aux_loss   # 纯语言建模损失
current_lr         = optimizer.param_groups[-1]['lr']   # 当前学习率
```

打印行（[trainer/train_pretrain.py:58](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L58)）形如：

```
Epoch:[1/2](100/5000), loss: 7.8421, logits_loss: 7.8421, aux_loss: 0.0000, lr: 0.00049985, epoch_time: 23.4min
```

读法：

- **`logits_loss`**：纯 next-token prediction 交叉熵，**预训练真正关心的指标**，应随训练单调下降（约从 ~10 降到 ~3，对应 README 给出的 [pretrain loss 曲线](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/images/pretrain_loss.jpg)）。
- **`aux_loss`**：Dense 模型恒为 0；MoE 模型是专家负载均衡损失（u3-l4），量级很小（受 `router_aux_loss_coef` 缩放）。
- **`loss`**：`logits_loss + aux_loss`，是实际反传的总损失。
- **`lr`**：当前学习率，应平滑下降（余弦退火）。

**重点：保存的权重叫什么名字**

[trainer/train_pretrain.py:63-68](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L63-L68) 决定了输出文件名：

```
out/pretrain_768.pth            # Dense，hidden_size=768
out/pretrain_768_moe.pth        # MoE，追加 _moe 后缀
```

命名规则 `{save_weight}_{hidden_size}{_moe?}.pth` 是全项目统一的，`eval_llm.py --weight pretrain` 就是靠它定位文件（见 u1-l3）。

#### 4.4.4 代码实践

**实践目标**：跑几步真实预训练，亲眼看到日志四件套的变化。

**操作步骤**：

1. 确保已按 u1-l2 装好环境，并把 `pretrain_t2t_mini.jsonl` 放进 `./dataset/`。
2. 为快速观察，故意把训练规模调小（这些只是 CLI 参数，不改源码）：

   ```bash
   cd trainer
   python train_pretrain.py --epochs 1 --batch_size 4 --max_seq_len 128 --log_interval 10 --save_interval 9999
   ```

   > 若有多卡，可改用 `torchrun --nproc_per_node 1 train_pretrain.py ...`（u4-l3）。
3. 观察终端前若干行日志。

**需要观察的现象**：每 10 步打印一行，`logits_loss` 从 ~10 附近开始缓慢下降；`aux_loss` 始终为 `0.0000`（Dense）；`lr` 从 `5e-4` 起步、随步数极缓慢下降。

**预期结果**：跑满几十步后 `logits_loss` 明显低于初始值。若显存不足，进一步调小 `--batch_size` / `--max_seq_len`。

> 若本机无法训练（无 GPU / 无数据），标注「待本地验证」，改为阅读 [trainer/train_pretrain.py:51-59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L51-L59) 推演日志格式。

#### 4.4.5 小练习与答案

**练习 1**：日志里的 `current_loss` 为什么要乘回 `accumulation_steps`？

**参考答案**：因为前向算出的 `loss` 被除以了 `accumulation_steps`（为了梯度累积时数学期望与大 batch 等价）。打印时乘回去，还原成「这一批数据的真实交叉熵」，便于跨配置比较。

**练习 2**：为什么保存权重前要 `model.eval()`，保存后又要 `model.train()`？

**参考答案**：`eval()` 会关闭 dropout 等随机行为，保证保存的 state_dict 与推理时行为一致；保存完切回 `train()` 让训练继续在训练模式下进行。这俩调用不影响权重数值，只切换模块状态。

**练习 3**：尾部补更新（[trainer/train_pretrain.py:75-80](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L75-L80)）如果删掉，会损失什么？

**参考答案**：一个 epoch 末尾不足 `accumulation_steps` 步累积的那部分梯度永远不会被 `step` 消费，相当于白白丢弃了最后几个 batch 的训练信号；对长训练影响小，但对短训练或末尾数据重要的场景不可忽略。

---

## 5. 综合实践：跑通预训练 + 续写评测

把本讲三个模块串起来，完成一次端到端的「训练 → 评测」闭环。

### 实践目标

用最小代价从随机初始化训练出一个**能续写中文**的微型语言模型，并用 `eval_llm.py` 验证它学到了「词语接龙」能力。

### 操作步骤

1. **准备数据**（u1-l2）：把 `pretrain_t2t_mini.jsonl` 放进 `./dataset/`。
2. **启动训练**（在 `trainer/` 下）：

   ```bash
   cd trainer
   # 单卡（推荐先小规模冒烟）
   python train_pretrain.py --epochs 1 --batch_size 8 --max_seq_len 256 \
       --log_interval 50 --save_interval 1000
   # 或多卡
   torchrun --nproc_per_node 1 train_pretrain.py --epochs 1
   ```

3. **记录 loss 曲线**：把日志里的 `logits_loss` 抽数据画图（横轴 step、纵轴 loss），应看到单调下降趋势。可对照官方曲线图 [images/pretrain_loss.jpg](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/images/pretrain_loss.jpg)。
4. **续写评测**（回到项目根目录）：

   ```bash
   python eval_llm.py --weight pretrain
   ```

   `eval_llm.py` 会按命名规则找到 `out/pretrain_768.pth`（u1-l3）。
5. **观察输出**：尝试输入「为什么天空是蓝色的」「解释什么是机器学习」，对比 README 给出的参考续写（[README.md:694-698](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L694-L698)）。

### 需要观察的现象

- 训练阶段：`logits_loss` 从约 10 量级下降；前几百步下降快，之后趋缓。
- 评测阶段：模型能产出**通顺但未必准确**的中文续写——这正是「预训练 = 会说话但不会回答问题」的典型表现。

### 预期结果

预训练模型擅长**续写**而非**对话**：你给它「为什么天空是蓝色的」，它大概率会接着往下写一段相关文字，而不是像助手那样回答你。这种「会说话但不会聊天」的状态，正是 SFT 阶段（u5-l2）要解决的问题。

> 若算力/数据受限，至少完成 4.4.4 的冒烟训练（几百步即可看到 loss 下降），续写评测标注「待本地验证」。

## 6. 本讲小结

- 预训练是**唯一无监督**的阶段，目标是 next-token prediction（词语接龙），让模型学会通用语言能力和世界知识。
- `PretrainDataset` 是最简单的 Dataset：首尾加 bos/eos、尾部 pad、把 pad 标 `-100`，**整段正文都参与 loss**。
- `train_pretrain.py` 的 `__main__` 是清晰的 **9 步装配**，步骤间有严格依赖（如先 compile 后 DDP、先分布式初始化后加载检查点）。
- `train_epoch` 是全项目训练循环的模板：前向 `res.loss + res.aux_loss`、除以累积步数、反向、每 N 步 `unscale→clip→step→update→zero_grad`、尾部补更新。
- 日志四件套：`loss = logits_loss + aux_loss`；Dense 模型 `aux_loss` 恒为 0；`logits_loss` 是真正要盯的指标。
- 输出权重命名 `pretrain_{hidden_size}{_moe?}.pth`，被 `eval_llm.py --weight pretrain` 复用。

## 7. 下一步学习建议

- **横向对照**：读完本讲后，直接打开 [trainer/train_full_sft.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_full_sft.py)，你会发现它的 `train_epoch` 和 9 步装配与预训练几乎一模一样——唯一的本质区别是数据集换成 `SFTDataset`、`--from_weight pretrain`（基于预训练权重继续训）。这就是下一讲 **u5-l2 全参数监督微调** 的内容。
- **深入损失**：若你对 MoE 的 `aux_loss` 感兴趣，回看 u3-l4 的 `MOEFeedForward` 与 `router_aux_loss_coef`。
- **深入优化器底座**：若你对梯度累积 / 混合精度的细节还想再确认，回看 u4-l3。
- **断点续训实操**：用 `--from_resume 1` 中断再重启，对照 u4-l2 验证 `SkipBatchSampler` 与 `lm_checkpoint` 的协作。
