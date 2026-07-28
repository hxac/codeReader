# RLHFDataset 数据加载与 tokenization

## 1. 本讲目标

上一讲（u2-l1）我们已经看到，`examples/data_preprocess/countdown.py` 会把任务样本写成一个 **parquet** 文件，每行包含 `prompt`、`data_source`、`reward_model.ground_truth`、`extra_info` 等字段。但训练框架并不能直接「吃」parquet——它需要的是 PyTorch 能理解的张量：`input_ids`、`attention_mask`、`position_ids`。

本讲就讲清楚从 **parquet 行** 到 **张量样本** 的这一步转换，它是数据进入训练主循环前的「最后一公里」。学完本讲你应该能够：

- 说清 `RLHFDataset` 如何读取 parquet、如何把每行变成一个样本；
- 解释**左填充（left padding）**为什么是 RL 生成场景的必然选择，以及 `truncation='error'` 为什么是「宁可报错也不悄悄截断」的安全设计；
- 用 `attention_mask` 推导出 `position_ids` 的数学公式；
- 看懂 `collate_fn` 如何把一个 batch 里「张量字段」和「非张量字段」分别堆叠，从而让奖励函数拿到它需要的字符串与字典。

## 2. 前置知识

- **Token 与 tokenizer**：大模型不认识文字，只认整数 token id。`tokenizer(prompt)` 把字符串变成 `input_ids`（一串整数）。本讲的 `tokenizer` 就是 Qwen2.5 的 HuggingFace tokenizer。
- **attention_mask**：一个与 `input_ids` 等长的 0/1 序列，`1` 表示「这是真实 token，参与注意力计算」，`0` 表示「这是填充（padding），请忽略」。
- **padding（填充）**：一个 batch 里的样本长短不一，但张量必须是规则的矩形，所以要把短样本「补齐」到固定长度 `max_prompt_length`。补在左边叫**左填充**，补在右边叫**右填充**。
- **position_ids**：告诉模型「这是序列里的第几个 token」。Transformer 用位置信息区分 token 顺序。
- **parquet**：一种列式存储的表格文件，上一讲 `countdown.py` 的 `to_parquet` 写出的就是它。
- **DataLoader**：PyTorch 的数据加载器，按 `batch_size` 把样本凑成一批，并用 `collate_fn` 决定「这一批怎么拼」。

承接 u2-l1：我们已知每条样本的 `prompt` 字段是一个 chat 格式的列表 `[{"role": "user", "content": "..."}]`，本讲就从这里继续。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/utils/dataset/rl_dataset.py` | 定义 `RLHFDataset`（读 parquet、逐行转张量）与 `collate_fn`（批拼装）。本讲的核心。 |
| `verl/utils/torch_functional.py` | 提供 `tokenize_and_postprocess_data`（tokenize + 填充 + 截断）和 `pad_sequence_to_length`（底层填充工具）。 |
| `verl/utils/model.py` | 提供 `compute_position_id_with_mask`，由 `attention_mask` 一行算出 `position_ids`。 |
| `verl/trainer/ppo/ray_trainer.py` | `_create_dataloader` 展示 `RLHFDataset` + `collate_fn` 如何被组装进训练器。 |

## 4. 核心概念与源码讲解

### 4.1 RLHFDataset：从 parquet 到单个样本

#### 4.1.1 概念说明

`RLHFDataset` 是 veRL 给 RL 训练准备的 PyTorch `Dataset`。它的职责很单一：**给定一个 parquet 文件和一个 tokenizer，把第 `i` 行翻译成一个含 `input_ids/attention_mask/position_ids` 的字典**。它本身不做任何训练逻辑，只负责「按行取数 + tokenize」。

「RLHF」是 Reinforcement Learning from Human Feedback 的缩写，是 veRL 沿用的命名；在 TinyZero 这种「规则奖励」场景里，它依然好用，因为下游（奖励函数、训练循环）要的就是这套统一的字段结构。

#### 4.1.2 核心流程

`RLHFDataset` 的生命周期分两阶段：

```text
构造阶段（__init__）：
  1. 接收 parquet_files（可多个）、tokenizer、max_prompt_length、truncation 等参数
  2. _download()：若文件在 HDFS，拷贝到本地缓存
  3. _read_files_and_tokenize()：用 pandas 读所有 parquet，concat 成一个 self.dataframe

取样阶段（__getitem__(item)）：
  1. 从 dataframe 取第 item 行 → row_dict
  2. 弹出 prompt 字段（chat 列表），取 chat[0]['content'] 得到字符串
  3. tokenize_and_postprocess_data(...) → input_ids, attention_mask（都是 [1, max_length]）
  4. compute_position_id_with_mask(attention_mask) → position_ids
  5. 去掉第 0 维（[0]），把三者塞回 row_dict
  6. 返回 row_dict（既有张量，也有 data_source 等非张量）
```

注意一个关键设计：**tokenize 是惰性的，发生在 `__getitem__` 里，而不是构造时一次性做完**。这意味着整个 dataframe 始终在内存里以「未 tokenize 的原始行」形式存在，每次取样时才临时转换。好处是省内存、支持灵活 batch；代价是每次取样都要 tokenize 一次（countdown 这种短 prompt 完全可接受）。

#### 4.1.3 源码精读

先看构造函数，它只是「存参数 + 下载 + 读表」，不做 tokenize：

[verl/utils/dataset/rl_dataset.py:63-89](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L63-L89) —— `__init__` 保存配置后调用 `_download()` 和 `_read_files_and_tokenize()`。

读表逻辑把多个 parquet 合并：

[verl/utils/dataset/rl_dataset.py:96-115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L96-L115) —— 用 `pd.read_parquet` 逐个读取再 `pd.concat` 成一张大表 `self.dataframe`。

> ⚠️ 代码里有两处「名不副实」的细节，读源码时要警惕：
> - 第 110-113 行那段「过滤过长 prompt」的代码**被注释掉了**（注释写着 `nvm if prompt is too long`），所以第 115 行打印的 `filter dataset len` 其实和原始长度相同，并没有真正过滤。
> - 参数 `filter_prompts`（第 68、82 行）和 `chat_template_func`（第 70、85 行）虽然被保存，但**在本文件里从未被使用**。它们是历史遗留的死参数。真正防止「prompt 过长」的，是后面 `truncation='error'` 的报错机制（见 4.2）。

核心的 `__getitem__`：

[verl/utils/dataset/rl_dataset.py:120-152](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L120-L152) —— 逐行 tokenize 的全部逻辑都在这里。

逐句拆解：

```python
row_dict = self.dataframe.iloc[item].to_dict()   # 第 item 行 → dict
chat = row_dict.pop(self.prompt_key)             # 弹出 'prompt' 字段（chat 列表）
prompt_with_chat_template = chat[0]['content']   # 取第一条消息的 content 字符串
```

这里 `chat` 就是 u2-l1 里 `make_map_fn` 存进去的 `[{"role": "user", "content": "..."}]`。`chat[0]['content']` 取出 `"...<think>"` 那段指令字符串。变量名叫 `prompt_with_chat_template`，但实际上**并没有套用任何 chat template**，只是取出原始 content——名字有点误导，需注意。

接着做核心转换：

[verl/utils/dataset/rl_dataset.py:131-138](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L131-L138) —— 调用 `tokenize_and_postprocess_data` 得到 `input_ids`、`attention_mask`，再用 `compute_position_id_with_mask` 推出 `position_ids`。

```python
row_dict['input_ids'] = input_ids[0]        # 去掉 [1, L] 的第 0 维 → [L]
row_dict['attention_mask'] = attention_mask[0]
row_dict['position_ids'] = position_ids[0]
```

`[0]` 是为了去掉 batch 维（`tokenize_and_postprocess_data` 返回的是 `[1, max_length]`），让每条样本保持一维 `[max_length]`，留给 `collate_fn` 去拼成 `[batch_size, max_length]`。

最后还补了一个 `index` 字段（第 149-150 行），从 `extra_info.index` 取，没有则默认 0。这个 `index` 在后续 GRPO 分组（u5-l5）里会用来识别「同一 prompt 的多次采样」，现在先有个印象即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一条 parquet 行 → 一个样本字典」的全过程，看清返回值里既有张量也有非张量。

**操作步骤**（源码阅读型，可在本地用真实 countdown.parquet 跑，也可只跟踪逻辑）：

1. 打开 `verl/utils/dataset/rl_dataset.py:120` 的 `__getitem__`。
2. 回顾 u2-l1 中 `countdown.py` 写出的 parquet 字段：`prompt`、`data_source`、`reward_model`、`extra_info`。
3. 在脑中（或本地）执行：`row_dict = {...}` → `pop('prompt')` → `chat[0]['content']` → tokenize。

**需要观察的现象**：
- `row_dict` 一开始含有 `data_source`、`reward_model`、`extra_info` 这些**非张量**字段；
- `pop('prompt')` 后 `prompt` 字段消失；
- tokenize 后新增的 `input_ids`、`attention_mask`、`position_ids` 是**张量**；
- 最终返回的 `row_dict` 是「张量 + 非张量」混合的字典。

**预期结果**：一条样本字典大致形如（示意，字段值待本地验证）：

```python
{
  'input_ids':        tensor([pad, pad, ..., 151643, 71714, ...]),  # 形状 [max_prompt_length]
  'attention_mask':   tensor([0, 0, ..., 1, 1, 1]),                 # 形状 [max_prompt_length]
  'position_ids':     tensor([0, 0, ..., 0, 1, 2]),                 # 形状 [max_prompt_length]
  'data_source':      'countdown',                                  # 非张量：奖励路由用
  'reward_model':     {'ground_truth': {'target': 24, 'numbers': [1,2,3,4]}},
  'extra_info':       {'index': 0},
  'index':            0,
}
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RLHFDataset` 不在构造函数里一次性把整个 dataframe 全部 tokenize 好？

**答案**：因为整张表可能很大，一次性全部 tokenize 成张量会占用大量内存（每条样本都要存 `max_prompt_length` 长的多个张量）。惰性 tokenize（在 `__getitem__` 里现取现转）让内存里只保留紧凑的原始行（字符串/字典），按需生成张量，省内存且支持 shuffle。

**练习 2**：变量名 `prompt_with_chat_template` 准确吗？

**答案**：不准确。代码只是 `chat[0]['content']` 取了原始 content 字符串，并没有调用 `tokenizer.apply_chat_template` 套用 chat 模板（那段代码在第 110-113 行被注释掉了）。命名带有历史包袱，读源码时要看实际逻辑而非变量名。

---

### 4.2 tokenize_and_postprocess_data：左填充与截断

#### 4.2.1 概念说明

`tokenize_and_postprocess_data`（在 `torch_functional.py` 里，被 `__getitem__` 以别名 `verl_F` 调用）负责把一个 prompt 字符串变成定长的 `input_ids` 和 `attention_mask`。它解决两个问题：

1. **长度补齐**：真实 token 数 < `max_length` 时，用 `pad_token_id` 填充到 `max_length`；
2. **长度超限**：真实 token 数 > `max_length` 时，按 `truncation` 策略处理。

#### 4.2.2 核心流程

```text
input_data = tokenizer(prompt)           # → input_ids, attention_mask（真实长度 seq_len）
if seq_len < max_length:
    按 left_pad（默认 True）填充到 max_length
elif seq_len > max_length:
    根据 truncation 处理：
      'left'  → 保留最后 max_length 个 token
      'right' → 保留最前 max_length 个 token
      'error' → 直接抛 NotImplementedError（默认，TinyZero 用这个）
return input_ids, attention_mask         # 形状都是 [1, max_length]
```

**为什么默认左填充？** 因为 RL 训练的下一步是**生成（generation）**：rollout 引擎会在序列右端不断追加新 token。把真实内容放在序列**右端**、填充放在**左端**，生成时只要从右边接着写即可，最后一个位置永远是「最新的 token」。如果用右填充，新 token 就得插到填充前面，处理起来极其别扭，也破坏 KV cache 的连续性。后面 u6-l4 会看到 vLLM 的 `_pre_process_inputs` 正是为了再去掉这些左填充。

**为什么 `truncation='error'`？** 这是「宁可训练崩溃，也不悄悄改数据」的安全设计。在 countdown 任务里，prompt 包含目标数和可用数字，如果偷偷截断，可能把关键数字砍掉，于是模型在一个被破坏的任务上训练，奖励信号全是错的，而你还浑然不觉。报错能让你立刻发现「prompt 设得太长 / max_prompt_length 设得太短」。`ray_trainer._create_dataloader` 正是显式传了 `truncation='error'`。

#### 4.2.3 源码精读

[verl/utils/torch_functional.py:225-266](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L225-L266) —— `tokenize_and_postprocess_data` 全文。

关键片段（保留主干）：

```python
input_data = tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
input_ids = input_data['input_ids']
attention_mask = input_data['attention_mask']
sequence_length = input_ids.shape[-1]
if sequence_length < max_length:
    input_ids = pad_sequence_to_length(input_ids, max_length,
                                       pad_token_id, left_pad=left_pad)
    attention_mask = pad_sequence_to_length(attention_mask, max_length,
                                            pad_token_id=0, left_pad=left_pad)
elif sequence_length > max_length:
    if truncation == 'error':
        raise NotImplementedError(f'{sequence_length=} is larger than {max_length}')
    ...
```

注意两个细节：
- `add_special_tokens=False`：countdown 的 prompt 模板里已经自己写好了开头标记，所以这里不再让 tokenizer 自动加 BOS 等特殊 token，避免重复。
- `input_ids` 用 `pad_token_id` 填，而 `attention_mask` 用 `0` 填（第 251 行）——这正是「填充位置 mask=0、被注意力忽略」的来源。

底层填充工具：

[verl/utils/torch_functional.py:209-219](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L209-L219) —— `pad_sequence_to_length` 用 `F.pad` 在最后一维补齐，`left_pad` 决定补左还是补右。

```python
pad_tuple = (max_seq_len - tensors.shape[-1], 0) if left_pad else (0, max_seq_len - tensors.shape[-1])
return F.pad(tensors, pad_tuple, 'constant', pad_token_id)
```

`F.pad` 的 tuple `(left, right)` 含义是「左边补多少、右边补多少」。`left_pad=True` 时 `(差值, 0)`，即左边补齐、右边不动，正是左填充。

再看 trainer 怎么显式传参：

[verl/trainer/ppo/ray_trainer.py:346-352](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L346-L352) —— `RayPPOTrainer._create_dataloader` 里实例化 `RLHFDataset`，末尾 `truncation='error'`。

#### 4.2.4 代码实践

**实践目标**：用一个短 prompt 直接调用 `tokenize_and_postprocess_data`，打印 `input_ids/attention_mask` 的形状，并肉眼确认左填充的排布。

**操作步骤**（这个函数是纯 torch + transformers，不需要完整 verl 训练栈）：

```python
# 示例代码（需本地安装 transformers 与 verl，待本地验证具体数值）
from transformers import AutoTokenizer
import verl.utils.torch_functional as verl_F
from verl.utils.model import compute_position_id_with_mask

tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")
if tok.pad_token is None:           # Qwen 默认可能没有 pad_token，需手动设
    tok.pad_token = tok.eos_token

prompt = "Using the numbers [1, 2, 3], create an equation that equals 6. <think>"
input_ids, attention_mask = verl_F.tokenize_and_postprocess_data(
    prompt=prompt, tokenizer=tok, max_length=64,
    pad_token_id=tok.pad_token_id, left_pad=True, truncation='error')
position_ids = compute_position_id_with_mask(attention_mask)

print(input_ids.shape, attention_mask.shape, position_ids.shape)
```

**需要观察的现象**：
- 三个张量形状都应是 `torch.Size([1, 64])`；
- `attention_mask` 左边是一串 `0`（填充），右边是一串 `1`（真实 token）；
- `input_ids` 左边全是 `pad_token_id`，右边才是 prompt 的真实 token；
- `position_ids` 左边是 `0`，右边从 `0, 1, 2, ...` 递增（详见 4.3）。

**预期结果**：`torch.Size([1, 64]) torch.Size([1, 64]) torch.Size([1, 64])`（具体右侧真实 token 数取决于 tokenizer 对该 prompt 的切分，待本地验证）。可打印 `attention_mask.sum().item()` 得到真实 token 数。

> 如果把 `max_length=64` 改成一个**小于**真实 token 数的值（例如 4），并保持 `truncation='error'`，程序应抛出 `NotImplementedError: sequence_length=... is larger than max_length=4`——这正是 `error` 策略的安全效果。

#### 4.2.5 小练习与答案

**练习 1**：把 `left_pad=True` 改成 `left_pad=False`，`attention_mask` 会变成什么样？训练还能正常跑吗？

**答案**：`attention_mask` 会变成左边一串 `1`（真实 token）、右边一串 `0`（填充）。逻辑上能跑，但下游的 rollout 生成（vLLM）预期的是「内容在右端」，右填充会把生成起点放到填充中间，破坏生成。所以 RL 生成场景必须用左填充。

**练习 2**：为什么 `attention_mask` 用 `0` 填充而 `input_ids` 用 `pad_token_id` 填充？

**答案**：`input_ids` 必须是合法 token id 才能进模型嵌入层，所以用 `pad_token_id`；`attention_mask` 是「是否有效」的标志，填充位置应当被注意力忽略，所以用 `0`。两者配合，让模型既不报错又正确忽略填充。

---

### 4.3 compute_position_id_with_mask：position_ids 的生成

#### 4.3.1 概念说明

有了左填充，真实的 token 被推到了序列右端。那模型怎么知道「右端第一个真实 token 是第 0 个位置」？这就靠 `position_ids`。`compute_position_id_with_mask` 用一个极其简洁的公式，**仅凭 `attention_mask`** 就推出正确的位置编号，不需要知道填充到底在左还是在右。

#### 4.3.2 核心流程

设 `attention_mask` 为 \(m\)，长度 \(L\)。位置编号定义为：

\[
\text{position\_ids} = \mathrm{clip}\bigl(\mathrm{cumsum}(m) - 1,\ \min=0\bigr)
\]

即：先对 mask 做累加和（cumsum），减 1，再截断到最小值 0。

用左填充的例子手算（`L=6`，前 3 个是填充）：

| 下标 | 0 | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- | --- |
| `mask` m | 0 | 0 | 0 | 1 | 1 | 1 |
| `cumsum(m)` | 0 | 0 | 0 | 1 | 2 | 3 |
| `cumsum-1` | -1 | -1 | -1 | 0 | 1 | 2 |
| `clip(min=0)` | **0** | **0** | **0** | **0** | **1** | **2** |

可以看到：填充位置（下标 0/1/2）得到 `0`（反正会被 mask 忽略，无所谓），真实 token（下标 3/4/5）得到 `0, 1, 2`——**正确地从 0 开始编号**。

这个公式对右填充也成立（读者可自行验证），所以它是「填充方向无关」的，非常优雅。

#### 4.3.3 源码精读

[verl/utils/model.py:177-178](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/model.py#L177-L178) —— 整个函数就一行：

```python
def compute_position_id_with_mask(mask):
    return torch.clip(torch.cumsum(mask, dim=-1) - 1, min=0, max=None)
```

- `torch.cumsum(mask, dim=-1)`：沿序列方向累加；
- `- 1`：把「累计的真实 token 数」转成「从 0 起的位置编号」；
- `torch.clip(..., min=0)`：把填充位置产生的 `-1, -2, ...` 抬到 `0`。

在 `__getitem__` 中的调用点：

[verl/utils/dataset/rl_dataset.py:138](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L138) —— 接收上面算出的 `attention_mask`，返回同形状的 `position_ids`。

#### 4.3.4 代码实践

**实践目标**：用一个手工构造的 mask 验证公式，确认填充方向无关性。

**操作步骤**：

```python
# 示例代码
import torch
from verl.utils.model import compute_position_id_with_mask

# 左填充：3 个 pad 在前
m_left = torch.tensor([[0, 0, 0, 1, 1, 1]])
print(compute_position_id_with_mask(m_left))   # 期望 tensor([[0,0,0,0,1,2]])

# 右填充：3 个 pad 在后
m_right = torch.tensor([[1, 1, 1, 0, 0, 0]])
print(compute_position_id_with_mask(m_right))  # 期望 tensor([[0,1,2,2,2,2]])
```

**需要观察的现象**：左填充下，真实 token 得到 `0,1,2`；右填充下，真实 token 同样得到 `0,1,2`（填充位置都是 `2`，但会被 attention_mask 忽略）。

**预期结果**：与上表一致（待本地验证）。两种填充方向下，真实 token 的位置编号都正确从 0 递增。

#### 4.3.5 小练习与答案

**练习**：如果不做 `clip(min=0)`，左填充下 `position_ids` 会是什么？会出什么问题？

**答案**：会得到 `[-1, -1, -1, 0, 1, 2]`。负的 position id 对绝大多数模型没有训练意义，可能导致位置嵌入查表越界或产生未定义行为。`clip(min=0)` 把填充位置的安全地钳到 0，反正这些位置会被 attention_mask 忽略，不影响真实 token。

---

### 4.4 collate_fn：张量与非张量的分组堆叠

#### 4.4.1 概念说明

`DataLoader` 每次取一个 batch（比如 32 条样本，每条是 `__getitem__` 返回的字典），但默认的拼装方式搞不定我们的数据——因为我们的样本是「混合类型」：`input_ids` 是张量，但 `data_source` 是字符串、`reward_model` 是嵌套字典、`extra_info` 也是字典。PyTorch 默认 `default_collate` 遇到这种异构数据容易报错。

`collate_fn` 就是自定义的「批拼装函数」。它的核心思想：**按字段类型分流**——张量字段用 `torch.stack` 堆成 `[batch, seq_len]`，非张量字段用 `np.array(..., dtype=object)` 装成对象数组。

#### 4.4.2 核心流程

```text
for 每条样本 data in data_list:
    for 每个 (key, val):
        if isinstance(val, torch.Tensor):  tensors[key].append(val)
        else:                               non_tensors[key].append(val)

for key in tensors:    tensors[key] = torch.stack(val, dim=0)      # → [batch_size, seq_len]
for key in non_tensors: non_tensors[key] = np.array(val, dtype=object)  # → 对象数组

return {**tensors, **non_tensors}  # 合并成一个 dict
```

为什么非张量要用 `dtype=object`？因为像 `extra_info` 这种字段，每条样本里可能是不同长度/结构的字典，普通 numpy 数组要求等长等形，会报错；`dtype=object` 让 numpy 退化为「装 Python 对象的容器」，什么形状都能塞。这样下游奖励函数（u2-l4）就能从 batch 里取出每条样本的 `data_source` 字符串和 `ground_truth` 字典。

#### 4.4.3 源码精读

[verl/utils/dataset/rl_dataset.py:31-55](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py#L31-L55) —— `collate_fn` 全文。

关键三步：

```python
for data in data_list:               # 遍历 batch 内每条样本
    for key, val in data.items():
        if isinstance(val, torch.Tensor):
            tensors[key].append(val)   # 张量进 tensors
        else:
            non_tensors[key].append(val)  # 其余进 non_tensors

tensors[key] = torch.stack(val, dim=0)          # 张量：堆叠成 [batch, seq_len]
non_tensors[key] = np.array(val, dtype=object)  # 非张量：对象数组
```

trainer 中把它绑给 DataLoader：

[verl/trainer/ppo/ray_trainer.py:353-357](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L353-L357) —— `DataLoader(..., collate_fn=collate_fn)`。

#### 4.4.4 代码实践

**实践目标**：用一个含「张量 + 字符串 + 字典」的小 batch 手动调用 `collate_fn`，看输出结构。

**操作步骤**：

```python
# 示例代码
import torch
from verl.utils.dataset.rl_dataset import collate_fn

batch = [
    {'input_ids': torch.tensor([0,0,1,2,3]), 'data_source': 'countdown',
     'reward_model': {'ground_truth': {'target': 6}}},
    {'input_ids': torch.tensor([0,0,4,5,6]), 'data_source': 'countdown',
     'reward_model': {'ground_truth': {'target': 24}}},
]
out = collate_fn(batch)
print(type(out['input_ids']), out['input_ids'].shape)   # <class 'torch.Tensor'> torch.Size([2, 5])
print(type(out['data_source']), out['data_source'])      # <class 'numpy.ndarray'> ['countdown' 'countdown']
print(type(out['reward_model']), out['reward_model'])    # ndarray(dtype=object) 装着两个 dict
```

**需要观察的现象**：
- `input_ids` 变成 `torch.Tensor`，形状 `[2, 5]`（batch=2，seq=5）；
- `data_source` 变成 numpy 字符串数组，可直接用 `out['data_source'][i]` 取值；
- `reward_model` 变成 `dtype=object` 的对象数组，`out['reward_model'][i]` 仍是原始字典。

**预期结果**：如上（待本地验证）。这个分流正是后续奖励函数能按 `data_source` 路由、按 `ground_truth` 判分的前提。

#### 4.4.5 小练习与答案

**练习 1**：如果用 PyTorch 默认的 `default_collate`（不传自定义 `collate_fn`），这个 batch 会怎样？

**答案**：默认 collate 会对所有字段做统一的堆叠规则，遇到 `reward_model` 这种「各条样本结构不完全一致」的嵌套字典，很容易抛错或得到无法用的结构。自定义 `collate_fn` 的目的就是规避这一点，把异构字段安全地装进 object 数组。

**练习 2**：为什么 `data_source`、`reward_model` 这些非张量字段必须一路带到 batch 里，而不是只保留 `input_ids` 等张量？

**答案**：因为奖励函数（`RewardManager`）需要用 `data_source` 字符串来路由到对应的 `compute_score`（u4-l1），并用 `reward_model.ground_truth` 来判分（u2-l4）。如果只留张量，奖励阶段就丢失了「正确答案」和「任务类型」，无法算奖励，RL 也就无从训练。

---

## 5. 综合实践

把本讲的四块串起来，完成一次「端到端」的 mini 数据管线追踪。

**任务**：假设你已经用 u2-l1 的方法生成了一个小 `countdown.parquet`（哪怕只有 4 行）。请完成下面的事，并把每一步的输出形状/类型记下来：

1. 构造一个 `RLHFDataset`（`max_prompt_length=128`，`truncation='error'`），打印 `len(dataset)`。
2. 取第 0 条样本：`sample = dataset[0]`，列出它的所有 key，标注哪些是 `torch.Tensor`、哪些不是。
3. 手动把前 2 条样本喂给 `collate_fn`：`batch = collate_fn([dataset[0], dataset[1]])`，打印 `batch['input_ids'].shape`、`batch['attention_mask'].shape`、`batch['position_ids'].shape`，以及 `batch['data_source']`。
4. 验证 4.3 的公式：取 `batch['attention_mask'][0]`，手动计算 `clip(cumsum-1, min=0)`，确认它和 `batch['position_ids'][0]` 完全相等。
5. **故障演练**：把 `max_prompt_length` 改成一个明显小于真实 prompt 长度的值（例如 4），重新 `dataset[0]`，确认会抛 `NotImplementedError`，并解释为什么这个报错是「好事」。

**预期成果**：你能用一句话说清「一行 parquet → 一个张量样本 → 一个 batch」的完整链路，并理解每个张量的形状与填充方向是怎么来的。具体数值标记「待本地验证」。

## 6. 本讲小结

- `RLHFDataset` 在构造时只读 parquet 成 `self.dataframe`，**惰性 tokenize** 发生在 `__getitem__`，把第 `i` 行转成含 `input_ids/attention_mask/position_ids` 的字典。
- `tokenize_and_postprocess_data` 先 tokenize，再**左填充**到 `max_prompt_length`；`truncation='error'` 让过长 prompt 直接报错，避免悄悄截断破坏任务。
- `compute_position_id_with_mask` 用 `clip(cumsum(mask)-1, min=0)` 一行从 `attention_mask` 推出 `position_ids`，**与填充方向无关**，真实 token 总是从位置 0 开始。
- `collate_fn` 按「是否张量」分流：张量 `torch.stack` 成 `[batch, seq_len]`，非张量（`data_source`、`reward_model` 等）装进 `dtype=object` 的 numpy 数组，让奖励函数仍能取到任务类型与正确答案。
- 左填充是 RL 生成场景的必然选择：rollout 引擎在序列右端追加 token，内容必须在右端。
- 读源码要警惕「名不副实」：`prompt_with_chat_template` 没真套模板，`filter_prompts`/`chat_template_func` 是死参数。

## 7. 下一步学习建议

本讲产出的是「一个 batch 的张量样本」。接下来：

- **u3-l1（DataProto 数据传输协议）**：这些 batch 字段会被包装进 `DataProto`，在 driver 与各 worker 之间被 `chunk`/`concat`/`repeat`，建议紧接着学，理解张量与非张量如何统一流转。
- **u4-l3（fit 训练主循环）**：你会看到 `input_ids/attention_mask/position_ids` 如何被送进 rollout 引擎做生成，本讲的左填充在那里被 `_pre_process_inputs` 去掉（u6-l4）。
- 建议回头再读一遍 [verl/utils/dataset/rl_dataset.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py)（仅 150 行），它是理解整个数据流的钥匙。
