# 编码与 Packing 机制

## 1. 本讲目标

上一篇（u4-l2）我们讲清楚了「原始数据如何被 Preprocessor 规范化成统一的 `messages` 结构」。本讲继续往后走一步：**这些 `messages` 是怎么变成模型能吃的 `input_ids` 的？变成 token 之后，又怎么避免「短样本被大量 padding」造成的算力浪费？**

学完本讲你应该能够：

- 说清 `EncodePreprocessor` 把一行 `messages` 编码成 token 的完整流程，以及 `return_length` / `lengths` 字段的作用。
- 理解 `PackingDataset` 如何用「装箱算法」把多条短样本拼成一条接近 `packing_length` 的长样本，以及 `binpack` 与 `sequential` 两种策略的差异。
- 说清 `padding_free` 为什么能消除 batch 内的无效 padding，它和 packing 的关系，以及 `LazyLLMDataset` 在其中扮演的「延迟编码 + 跳过坏样本」角色。
- 能够针对同一数据集分别开关 packing，定量观察样本数与平均长度的变化。

---

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义）：

- **Template 对话模板（u3-l3）**：`template.encode(messages)` 是把对话翻译成 `{input_ids, labels, loss_scale, ...}` 的唯一入口；`labels` 中非回答段被填 `-100`，回答段保留真 label。
- **RowPreprocessor 预处理器（u4-l2）**：基类编排「列映射 → 逐行 `preprocess` → 清洗 → 容错」流水线，子类只覆盖 `preprocess(row)` 钩子；`__call__` 内部最终落到 `dataset.map(self.batched_preprocess, ...)`。
- **三个数据容器**：HuggingFace 的 `Dataset`（map-style，能随机访问、能 `.map`）、`IterableDataset`（流式，只能顺序迭代）、PyTorch 的 `torch.utils.data.Dataset`（自定义 `__getitem__`/`__len__`）。

下面用到的两个直觉，先建立起来：

1. **训练算力 ≈ token 数量**。Transformer 前向的计算量基本正比于「序列长度」。如果一个 batch 里 8 条样本，最长 2048、最短 64，把它们都 pad 到 2048，那短样本就白白算了 1984 个 token 的 padding。
2. **「拼」和「不 pad」是两件事**。Packing 是在**数据预处理阶段**把多条样本拼成一条；padding_free 是在**collator 阶段**把一个 batch 内的多条样本压平。两者的目的都是「减少无效 token」，但发生的位置和手段不同。本讲会把这条界线讲清楚。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `swift/dataset/utils.py` | 定义 `EncodePreprocessor`（编码入口）、`AddLengthPreprocessor`（只追加长度列）和 `LazyLLMDataset`（延迟编码 + 跳过坏样本的运行时容器）。 |
| `swift/dataset/packing.py` | 定义 `PackingDataset`（map-style 装箱）与 `IterablePackingDataset`（流式装箱），以及核心装箱函数 `calculate_matched_group`。 |
| `swift/pipelines/train/sft.py` | `SwiftSft` 管道，在 `_encode_dataset` / `_post_process_datasets` 里把上述组件串成完整的数据准备流水线。 |
| `swift/template/base.py` | `Template.encode`（编码总入口）、`packing_row`（拼行）、`data_collator` 里 `padding_free` 的压平逻辑。 |
| `swift/dataset/preprocessor/core.py` | `RowPreprocessor` 基类，`EncodePreprocessor` 的父类，负责 `batched_preprocess` 与 `__call__`。 |
| `swift/arguments/base_args/base_args.py` | `packing` / `packing_length` / `packing_num_proc` / `packing_strategy` / `lazy_tokenize` 等参数定义与互斥校验。 |

---

## 4. 核心概念与源码讲解

### 4.1 EncodePreprocessor：把一行 messages 编码成 token

#### 4.1.1 概念说明

经过 u4-l2 之后，数据集里的每一行已经是规范的 `messages`。但模型并不认识 `messages`，它只认 `input_ids`（token id 序列）。**编码（encode）就是「messages → token 序列」这一步**，由 `template.encode` 完成（u3-l3 已深入讲解其内部 `_encode_truncated → _encode → _swift_encode` 链路）。

`EncodePreprocessor` 的职责非常薄：它是一个 `RowPreprocessor` 子类，把编码能力「挂」到预处理流水线上，让 `dataset.map` 能批量编码整个数据集。它只做一件事——对每一行调用 `template.encode(row, return_length=True)`。

这里有个关键字 `return_length=True`：它让 `encode` 在返回的字典里额外塞一个 `lengths` 字段。这个 `lengths` 是后续 packing 装箱和长度统计的**必备信息**——你得先知道每条样本多长，才能决定怎么拼。

#### 4.1.2 核心流程

编码入口的产出可以这样概括：

```
row = {'messages': [...], 'images': [...], ...}   # 规范化后的输入
        │  template.encode(row, return_length=True)
        ▼
encoded = {
    'input_ids':  [...],     # token id 序列
    'labels':     [...],     # 与 input_ids 等长，非回答段为 -100
    'loss_scale': [...],     # 每个 token 的 loss 权重（0/1 或自定义）
    'lengths':    [N],       # 本条样本的 token 长度（供统计/packing）
    ...
}
```

`template.encode` 内部会**聚合**所有以 `length` 结尾的字段（如 `length`、`chosen_length`、`rejected_length` 等），把它们拼成一个 `lengths` 列表，这正是 DPO 等任务需要的「chosen/rejected 两条长度」的情况。

整个编码在数据准备流水线里被这样驱动：

```
SwiftSft._prepare_dataset
   └─ _encode_dataset(...)          # 编码/写长度列
   └─ _post_process_datasets(...)   # LazyLLMDataset 包装 + 可选 Packing 包装
```

#### 4.1.3 源码精读

`EncodePreprocessor` 极其简短，核心就是一行 `template.encode`：

[swift/dataset/utils.py:115-122](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/utils.py#L115-L122) —— `EncodePreprocessor`：把编码能力挂到预处理流水线上，注意 `return_length=True`。

```python
class EncodePreprocessor(RowPreprocessor):
    def __init__(self, template: 'Template'):
        super().__init__()
        self.template = template

    def preprocess(self, row):
        return self.template.encode(row, return_length=True)
```

它有个子类 `AddLengthPreprocessor`，行为不同——它**不替换**原始 row，只在原 row 上**追加**一个 `lengths` 字段：

[swift/dataset/utils.py:125-130](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/utils.py#L125-L130) —— `AddLengthPreprocessor`：保留原始 messages，只额外写入 `lengths` 列。

```python
class AddLengthPreprocessor(EncodePreprocessor):
    def preprocess(self, row):
        encoded = super().preprocess(row)
        row['lengths'] = encoded['lengths']
        return row
```

> 区别很关键：`EncodePreprocessor` 产出的是「编码后的 dict」（含 `input_ids`），整行被替换；`AddLengthPreprocessor` 产出的是「原始 row + lengths」，messages 仍在，token 化的结果被丢弃。后者用于「只想预先知道每条样本多长、但不在此时固化 token」的场景（见 4.3 节 LazyLLMDataset）。

`template.encode` 是编码的唯一真相源，它按 `task_type` 派发不同的编码分支，并在收尾时聚合 `lengths`：

[swift/template/base.py:599-673](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L599-L673) —— `Template.encode`：编码总入口，按 `task_type` 派发，并聚合 `lengths`。

收尾聚合长度的关键片段（注意 `return_length` 为真时才写入 `lengths`）：

```python
lengths = []
for key in list(encoded.keys()):
    ...
    elif key.endswith('length'):
        value = encoded[key]
        if isinstance(value, int):
            lengths.append(value)
        elif isinstance(value, (tuple, list)):
            lengths += value
if return_length:
    encoded['lengths'] = lengths
```

`EncodePreprocessor` 继承自 `RowPreprocessor`，它的批量驱动逻辑（列映射、逐行 `preprocess`、容错丢弃坏行）都在基类里，本讲不重复，可回看 u4-l2。编码这一步能享受 `RowPreprocessor` 的容错：超长样本抛 `MaxLengthError` 时会被丢弃而不是让整条流水线崩掉。

最后看 `SwiftSft` 怎么把编码接入流水线。分两处：

[swift/pipelines/train/sft.py:298-337](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L298-L337) —— `_encode_dataset`：非流式且非延迟编码时，跑 `AddLengthPreprocessor`（split 截断策略才用 `EncodePreprocessor`）预先写入 `lengths` 列。

```python
if not args.lazy_tokenize and not args.streaming:
    # Compatible with cached_dataset, only additionally write length here.
    preprocessor_cls = EncodePreprocessor if args.truncation_strategy == 'split' else AddLengthPreprocessor
    preprocessor = preprocessor_cls(template=template)
    batch_size = 100 if args.model_meta.is_multimodal else 1000
    dataset = preprocessor(dataset, num_proc=args.dataset_num_proc, ...)
```

[swift/pipelines/train/sft.py:149-155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L149-L155) —— 流式（streaming）分支：直接用 `EncodePreprocessor` 即时编码，因为流式数据无法反复随机访问，必须一次性编码到位。

```python
elif args.streaming:
    preprocessor = EncodePreprocessor(template=template)
    dataset = preprocessor(dataset, num_proc=args.dataset_num_proc, ...)
```

#### 4.1.4 代码实践

**实践目标**：直观看到 `template.encode` 的产出与 `lengths` 字段。

**操作步骤**（源码阅读 + 最小调用，建议在安装好 ms-swift 的环境里以脚本方式运行）：

1. 准备一个最小脚本（**示例代码**，非项目原有文件）：

   ```python
   from swift.template import get_template
   from swift.llm import PtArgument, SftArguments  # 仅用于拿到一个可用 template
   from swift.template import TemplateInputs

   # 用你本地已有的任意 chat 模型 id，例如 Qwen2.5-0.5B-Instruct
   template = get_template(model_id_or_path='Qwen/Qwen2.5-0.5B-Instruct', ...)
   template.set_mode('train')

   row = {'messages': [
       {'role': 'user', 'content': '你好'},
       {'role': 'assistant', 'content': '你好！有什么可以帮你？'},
   ]}
   encoded = template.encode(row, return_length=True)
   print('input_ids:', encoded['input_ids'])
   print('labels  :', encoded['labels'])
   print('lengths :', encoded['lengths'])
   ```

2. 运行脚本。

**需要观察的现象**：

- `input_ids` 与 `labels` 等长。
- `labels` 中 `user` 段（含 system/外壳 token）对应位置为 `-100`，只有 `assistant` 回答段保留真实 token id（这是 u3-l3 讲的「只在回答上算 loss」）。
- `lengths` 是一个单元素列表，值等于 `input_ids` 的长度。

**预期结果**：能成功打印三个字段，且 `len(input_ids) == len(labels) == lengths[0]`。

> 如果本地没有 GPU/模型，这一步可改为「源码阅读型实践」：直接对照 [swift/template/base.py:599-673](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L599-L673) 走读，确认 `lengths` 是如何从所有 `*length` 字段聚合出来的。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`AddLengthPreprocessor` 调用了一次 `template.encode` 却把 token 结果丢掉、只留 `lengths`，这看起来浪费。为什么设计成这样？

**答案**：因为后续默认会把数据集包进 `LazyLLMDataset`（4.3 节），由它在训练取数时**再次**编码。预编码只是为了拿到 `lengths` 用于长度统计（`_stat_dataset`）和装箱（`PackingDataset` 读取 `lengths`）。丢弃是为了让行保持「messages 形态」，交给 LazyLLMDataset 在运行时用最新 template 状态重新编码，同时获得「跳过坏样本」的能力。

**练习 2**：流式数据为什么用 `EncodePreprocessor` 而不是 `AddLengthPreprocessor`？

**答案**：流式数据（`IterableDataset`）只能顺序迭代、不能反复随机访问，所以必须在第一次碰到时就把 token 编码到位（产出 `input_ids`），交给 `IterablePackingDataset` 处理；没有「等会儿再编码」的机会。

---

### 4.2 Packing：把多条短样本拼成一条长样本

#### 4.2.1 概念说明

假设 `max_length=2048`，而你的数据集里大量样本只有 64~128 个 token。如果每条样本单独作为一个训练样本，GPU 每次前向都要处理「一条 128 token 的样本」，序列维度严重吃不饱，吞吐很低。

**Packing（装箱）** 的思路：在数据预处理阶段，用「装箱算法（bin packing）」把多条短样本**拼**成一条长度接近 `packing_length`（默认等于 `max_length`）的长样本。这样一来，每条「训练样本」都几乎塞满 `max_length`，GPU 序列维度的利用率被拉满。

这里有个关键问题：**把 A、B、C 三条对话首尾相接拼成一条，模型会不会把 B 的开头当成 A 的回答继续生成？** 不会——因为 packing 强制开启了 `padding_free`（见 4.3），靠 `position_ids` 重置 + flash attention 的 varlen 机制让三条样本在注意力上**因果隔离**，互不可见。各自的 `labels` 也独立计算 loss。

#### 4.2.2 核心流程与装箱数学

Packing 的核心是一个**装箱函数** `calculate_matched_group`，它把一串 `(样本下标, 长度)` 项划分成若干「组（pack）」，每组总长 ≤ `packing_length`。项目提供两种策略：

- **`binpack`（默认）**：best-fit-decreasing 装箱。先按长度**降序**排序，再把每条尽量放进「剩得最少但还能装下」的组里。整体利用率高（接近理论最优），但会**打乱样本原始顺序**。对应论文 [arXiv:2404.10830](https://arxiv.org/pdf/2404.10830)。
- **`sequential`**：保序贪心（next-fit）。维护一个「当前开着的组」，下一条放不下就封箱再开新组。**保持样本原始顺序**，组边界跟着输入顺序走（需配合 `packing_num_proc=1` 才是单一全局顺序）。

装箱的目标是最大化「箱」的填充率。设数据集共有 \(n\) 条样本、第 \(i\) 条长度 \(l_i\)、单箱容量 \(L=\text{packing\_length}\)，则：

- 不做 packing 时，若每条都被 pad 到 \(L\)（最坏情况），有效 token 占比：
  \[
  \eta_{\text{nopack}} = \frac{\sum_{i=1}^{n} l_i}{n \cdot L}
  \]
- 做 packing 后，理想情况下样本被拼成 \(k \approx \left\lceil \tfrac{\sum l_i}{L} \right\rceil\) 个接近全满的箱，填充率：
  \[
  \eta_{\text{pack}} = \frac{\sum_{i=1}^{n} l_i}{k \cdot L} \;\approx\; 1
  \]

也就是说，packing 把「平均每条样本吃掉的算力」从 \(L\) 降到 \(\overline{l}\)（平均长度），在短样本密集的数据集上提速非常明显。

#### 4.2.3 源码精读

装箱函数本体，注意 `sequential` 与默认 `binpack`（调用第三方 `binpacking` 库）两条分支：

[swift/dataset/packing.py:16-47](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L16-L47) —— `calculate_matched_group`：装箱核心，`sequential` 保序贪心，默认 best-fit-decreasing。

```python
def calculate_matched_group(sequences, packing_length, is_finished=True, strategy='binpack'):
    if len(sequences) == 0:
        return [], []
    if strategy == 'sequential':
        # 保序贪心：单个开着的 pack 放不下就封箱
        packs, cur, cur_len = [], [], 0
        for item in sequences:
            seq_len = item[1]
            if cur and cur_len + seq_len > packing_length:
                packs.append(cur); cur, cur_len = [], 0
            cur.append(item); cur_len += seq_len
            ...
        ...
    # 默认：best-fit-decreasing
    import binpacking
    sequences = binpacking.to_constant_volume(sequences, packing_length, weight_pos=1)
    ...
    return sequences, ret_sequences
```

`PackingDataset`（map-style，非流式）的构造期就完成装箱。注意它一上来就**强制开启 template 的 packing 与 padding_free**：

[swift/dataset/packing.py:50-110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L50-L110) —— `PackingDataset.__init__`：强制 `padding_free=True`，多进程并行装箱，主进程收集结果并广播到各 rank。

```python
class PackingDataset(Dataset):
    PACKING_BATCH_SIZE = 1000
    def __init__(self, template, dataset, ..., packing_length=None,
                 packing_num_proc=1, packing_strategy='binpack', **kwargs):
        template.packing = True
        template.padding_free = True  # TODO: remove
        ...
        self.packing_length = packing_length or self.template.max_length
        self.packing_num_proc = min(packing_num_proc, math.ceil(len(dataset) / self.PACKING_BATCH_SIZE))
        ...
        if is_master():
            lengths = self.dataset['lengths']          # 读取预先写好的 lengths 列
            chunked_lengths = split_list(lengths, self.packing_num_proc)
            for i in range(self.packing_num_proc):
                worker = mp.Process(target=self.create_packed_idx, args=(i, offset, chunked_lengths[i]), ...)
                worker.start()
            ...  # 进度条收集 packed_idx / packed_length
```

这里有几个设计要点：

1. **强制 `padding_free=True`**：第 66-67 行直接改 template，这就是「packing 一定伴随 padding_free」的代码体现。
2. **多进程装箱**：把 `lengths` 切成 `packing_num_proc` 段，每段一个子进程独立装箱，主进程通过 `mp.Queue` 收集结果。多卡训练时再 `dist.broadcast_object_list` 把装箱结果广播给所有 rank，保证各卡看到同样的 pack 划分。
3. **依赖 `lengths` 列**：第 78 行读 `self.dataset['lengths']`，这正是 4.1 节 `AddLengthPreprocessor` 预先写入的列——所以 packing 必然要求 `lazy_tokenize=False`（见 4.3.3）。

装箱子进程的工作循环，按 `PACKING_BATCH_SIZE=1000` 分批喂给装箱函数：

[swift/dataset/packing.py:112-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L112-L126) —— `create_packed_idx`：子进程逐批装箱，把「样本下标组」放回队列。

装箱完成后，`PackingDataset` 对外的接口非常简单——`__getitem__` 返回的是**一个 list**（一个 pack 里包含多条原始样本）：

[swift/dataset/packing.py:128-134](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L128-L134) —— `__getitem__`：返回一个 pack 内所有原始样本的 list。

```python
def __getitem__(self, index):
    sequence = self.packed_idx[index]
    row = [self.dataset[i] for i in sequence]
    return row
```

注意它返回 `List[row]` 而不是单条——真正的「拼接」发生在 collator 阶段的 `packing_row`（4.3 节）。`__len__` 返回的是 **pack 的数量**（远小于原始样本数）。

流式数据用 `IterablePackingDataset`，思路类似但靠「编码 worker + 装箱缓冲区」在线完成：

[swift/dataset/packing.py:137-230](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L137-L230) —— `IterablePackingDataset`：编码子进程在线编码，主进程在缓冲区里持续装箱并 yield。

它的 `_processor` 在子进程里对每条数据即时调用 `template.encode(data, return_length=True)`：

[swift/dataset/packing.py:171-180](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L171-L180) —— `_processor`：流式场景下边读边编码，超长（`MaxLengthError`）按 `strict` 决定是否抛错。

最后看接入点——`SwiftSft._post_process_datasets` 里 packing 的分支会根据是否流式选不同类，并要求 `lazy_tokenize=False`：

[swift/pipelines/train/sft.py:138-148](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L138-L148) —— `_post_process_datasets` 的 packing 分支：按 streaming 选 `IterablePackingDataset` / `PackingDataset`。

```python
if args.packing:
    packing_dataset_cls = IterablePackingDataset if args.streaming else PackingDataset
    dataset = packing_dataset_cls(
        template, dataset, num_proc=args.dataset_num_proc,
        packing_length=args.packing_length, packing_num_proc=args.packing_num_proc,
        packing_strategy=args.packing_strategy, strict=args.strict,
        load_from_cache_file=args.load_from_cache_file)
```

参数本身的含义和互斥关系在 `base_args.py` 里有完整文档：

[swift/arguments/base_args/base_args.py:74-80](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L74-L80) —— `packing` 系列参数文档：`packing_length` 默认 `max_length`，`packing_strategy` 有 `binpack`/`sequential`。

#### 4.2.4 代码实践

**实践目标**：对同一数据集开关 packing，定量对比「训练样本数」与「平均长度」。

**操作步骤**：

1. 准备一份小数据集（**示例数据**），存成 `demo.jsonl`，每行一个 `messages`，故意做成短样本（回答几十 token）：

   ```json
   {"messages":[{"role":"user","content":"1+1=?"},{"role":"assistant","content":"2"}]}
   ```

   复制 200 行左右。

2. 跑两次训练（或只跑到数据准备阶段即可观察日志），用同一个模型、同一个 `max_length 2048`：

   ```bash
   # 关闭 packing
   swift sft --model Qwen/Qwen2.5-0.5B-Instruct --dataset demo.jsonl \
       --max_length 2048 --max_steps 1 --packing false --output_dir output/nopack

   # 开启 packing
   swift sft --model Qwen/Qwen2.5-0.5B-Instruct --dataset demo.jsonl \
       --max_length 2048 --max_steps 1 --packing true --output_dir output/pack
   ```

   > 真实多卡 packing 示例可参考 [examples/train/full/qwen2_5_32b.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/full/qwen2_5_32b.sh)（其中带 `--packing true`）。

3. 关注控制台日志中的 `Dataset Token Length:` 一行（由 `_stat_dataset` 输出），以及训练前打印的 `train_dataset:` 条数。

**需要观察的现象**：

- **关闭 packing**：`train_dataset` 条数 ≈ 200；`Dataset Token Length` 显示的是各原始样本长度（均值很小，比如几十）。
- **开启 packing**：日志会先打印 `Packing:` 进度条；之后 `Dataset Token Length` 来自 `packed_length`（每个 pack 的总长），均值接近 `packing_length`（2048）。

**预期结果**：开启 packing 后，样本数大幅下降（从 ~200 降到 ~个位数），平均长度从几十上升到接近 2048。这正好印证 4.2.2 的填充率公式：packing 把短样本「焊」成了长 pack。

**关键观察点源码**：长度统计逻辑在 `_stat_dataset`，它会区分普通数据集（读 `lengths`）与 `PackingDataset`（读 `packed_length`）：

[swift/pipelines/train/sft.py:267-278](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L267-L278) —— `_stat_dataset`：普通数据集读 `lengths`，packing 后读 `packed_length`。

> 若本地不具备运行条件，可改为「源码阅读型实践」：对照 [packing.py:50-134](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L50-L134) 理解 `packed_idx` 的长度（=pack 数）为何远小于原始样本数。**待本地验证**运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`packing_num_proc=4` 和 `packing_num_proc=1` 会让最终装箱结果不同吗？为什么？

**答案**：会不同。`packing_num_proc` 把 `lengths` 切成 N 段分别装箱，每段内部独立装箱、段与段之间不互通，所以 `binpack` 的「全局最优」被打断，pack 划分会变。文档也明确说「different values of `packing_num_proc` will result in different packed datasets」。若要可复现且全局保序，用 `packing_num_proc=1`。

**练习 2**：为什么 `binpack` 默认策略会打乱样本顺序？这对训练有影响吗？

**答案**：`binpack` 先按长度降序排序再装箱，所以 pack 内的样本不再是原始顺序。对训练影响很小：SFT 训练本来就是按 batch 随机打乱（shuffle 在 trainer 的 dataloader 里做），且 packing 下每个 pack 内部因果隔离、独立算 loss，样本拼接顺序不影响梯度。若你的下游逻辑依赖严格保序（如某些序列采样假设），可切 `--packing_strategy sequential`。

---

### 4.3 padding_free 与 LazyLLMDataset

#### 4.3.1 概念说明

Packing 解决的是「**跨样本**」的拼接（数据预处理阶段）。但即便不做 packing，一个 batch 里多条样本长度不一，传统做法要把它们都 pad 到 batch 内最长的那条，短样本就被塞了一堆 pad token，这些 pad token 也会参与前向计算（或至少占用显存）。

**padding_free** 解决的是「**batch 内**」的 padding：把一个 batch 里的多条样本**首尾压平（flatten）成一条长序列**，配以「每条样本各自从 0 开始重新计数」的 `position_ids`，再依赖 flash attention 的 **varlen（变长）注意力**让它们彼此因果隔离。这样 batch 的总长度就是「各样本长度之和」而非「条数 × 最长」，彻底消除了 batch 内 padding。

举一个直观对比例子，设一个 batch 有 3 条样本，长度分别为 100、200、400：

- 传统 padding：pad 到 400，batch 形状 `[3, 400]`，有效 token 700，浪费 500。
- padding_free：压平成长度 700 的一条，batch 形状 `[1, 700]`，无 padding。

flash attention 的 varlen 接口接受「每段的真实长度」（`cu_seqlens`）来在一段连续序列上分别做注意力，这就是 padding_free 能成立的底层依赖。所以参数文档明确要求 `--attn_impl flash_attn` 且 `transformers>=4.44`。

**LazyLLMDataset** 则是另一个维度的优化：**延迟编码 + 跳过坏样本**。它不在数据准备阶段一次性把整个数据集编码成 token，而是把「原始 dataset + `encode_func`」打包，在训练真正取数（`__getitem__`）时才编码当前这一条；遇到 `MaxLengthError`（超长）等错误时自动跳过、随机换一条，而不是让训练崩掉。

#### 4.3.2 核心流程

padding_free 在 collator 阶段发生，关键三步：

```
batch = [row0, row1, row2, ...]      # dataloader 取出的一个 batch（每条已编码）
   │  template.packing_row(batch)    # 把多条按 input_ids/labels/loss_scale 拼接，
   │                                 # 并生成「每条各自从 0 起」的 position_ids
   ▼
flattened = { input_ids: 拼接后的长序列, position_ids: [0..l0, 0..l1, 0..l2], ... }
   │  flash-attn varlen（按 position_ids 分段）
   ▼
前向计算，各段因果隔离、互不可见
```

LazyLLMDataset 的取数流程：

```
trainer.dataloader 调用 LazyLLMDataset[i]
   │  for i in range(n_try_fetch):
   │      data = dataset[idx]
   │      try: return encode_func(data)      # = template.encode，延迟到此刻
   │      except MaxLengthError: 换一条 idx 继续试
   ▼
返回一条成功编码的样本（坏样本被悄悄跳过）
```

三者的协作关系一句话概括：

> **packing 一定走 padding_free**（拼好的 pack 要压平）；**padding_free 可单独使用**（不拼接，只压平 batch）；**LazyLLMDataset 是「取数时才编码」的运行时容器**，packing 与非 packing 都可以建立在它之上。

#### 4.3.3 源码精读

先看 padding_free 的拼行逻辑——`packing_row` 把一个 batch 的多条样本按字段拼接，关键是 `position_ids` 被重新生成为「每条各自从 0 起」：

[swift/template/base.py:675-696](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L675-L696) —— `packing_row`：拼接 `input_ids`/`labels`/`loss_scale`，并按每条长度重生 `position_ids`。

```python
def packing_row(self, row):
    packed = {}
    keys = set()
    length = []
    for r in row:
        keys.update(r.keys())
        length.append(r['length'])
    for key in keys:
        ...
        elif key in {'input_ids', 'labels', 'loss_scale', 'position_ids', ...}:
            packed[key] = sum((x.get(key) or [] for x in row), start=[])   # 首尾拼接
        ...
    if 'position_ids' not in packed:
        packed['position_ids'] = sum((list(range(x)) for x in length), start=[])  # 每条从 0 重置
    ...
    return packed
```

`data_collator` 里对 `padding_free` 的处理——直接把整个 batch 压成「一条」，且断言 batch 此时只剩一行：

[swift/template/base.py:1869-1886](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1869-L1886) —— `data_collator` 的 `padding_free` 分支：先 `packing_row` 拼接，再断言只剩一条、不做任何 pad。

```python
if self.padding_free:
    batch[:] = [self.packing_row(batch)]
    assert 'position_ids' in batch[0]
...
if self.padding_free:
    assert len(batch) == 1
    for k in ['input_ids', 'channel'] + gather_keys:
        v = batch[0].get(k)
        if v is not None:
            res[k] = v if k == 'channel' else [v]
```

可以看到：`padding_free=True` 时**不再产生 `attention_mask` 的 padding**，整批就是一条无 padding 的长序列，靠 `position_ids` 的分段让模型区分各样本边界。这就是 padding_free 节省显存/算力的直接来源。

参数文档对 padding_free 的说明（含与 packing 的对比，非常重要）：

[swift/arguments/base_args/template_args.py:60-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py#L60-L64) —— `padding_free` 文档：flatten 消除 batch 内 padding；需 flash_attn；与 packing 相比无预处理开销但训练更快更稳的是 packing。

> 文档原文要点：**padding_free 无预处理开销，但 packing 训练速度更快、显存更稳**。工程选择上：嫌预处理麻烦先上 padding_free；追求极致吞吐上 packing。

再看 LazyLLMDataset——延迟编码与跳过坏样本的核心：

[swift/dataset/utils.py:57-112](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/utils.py#L57-L112) —— `LazyLLMDataset`：运行时容器，`__getitem__` 时才编码，`MaxLengthError` 自动跳过。

关键的 `__getitem__` 取数与容错循环：

```python
def __getitem__(self, idx):
    if isinstance(idx, str):
        return self.dataset[idx]
    for i in range(self.n_try_fetch):
        if i > 0:
            idx = self._idx_list[self._idx]              # 随机换一条
            self._idx = (self._idx + 1) % len(self.dataset)
        data = self.dataset[idx]
        try:
            return self.encode_func(data, return_length=True)   # 延迟编码
        except Exception as e:
            if self.strict:
                raise
            if isinstance(e, MaxLengthError):
                continue                                 # 超长，换下一条
            ...                                          # 其它错误也容错跳过
    raise ValueError('Failed to retrieve the dataset. ...')
```

注意几个设计：

1. **`encode_func` 默认就是 `template.encode`**：见 `_post_process_datasets` 第 137 行 `LazyLLMDataset(dataset, template.encode, ...)`。所以 LazyLLMDataset 复用了 4.1 节讲的全套编码逻辑，只是把它推迟到取数时刻。
2. **`idx` 为字符串时直接透传**：这正是 `PackingDataset.__init__` 里 `self.dataset['lengths']` 能读到列的原因——`LazyLLMDataset.__getitem__('lengths')` 走字符串分支，把请求转给底层 HF Dataset。
3. **`strict` 开关**：`strict=True` 时遇到错误直接抛（适合排查数据问题）；默认 `strict=False` 自动跳过坏样本，保证训练不被个别脏数据打断。

最后看 LazyLLMDataset 的接入点——默认（非流式、非 split）路径都会包一层：

[swift/pipelines/train/sft.py:136-137](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L136-L137) —— `_post_process_datasets`：非流式非 split 时包进 `LazyLLMDataset(dataset, template.encode, ...)`。

```python
if not args.streaming and args.truncation_strategy != 'split':
    dataset = LazyLLMDataset(dataset, template.encode, strict=args.strict, random_state=args.data_seed)
```

以及 `lazy_tokenize` 与 packing 的互斥校验——packing 时强制 `lazy_tokenize=False`（因为装箱需要预先知道长度）：

[swift/arguments/base_args/base_args.py:126-140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L126-L140) —— `_init_lazy_tokenize`：packing/streaming 都与 lazy_tokenize 互斥。

```python
if self.lazy_tokenize is None:
    if self.cached_dataset or self.cached_val_dataset:
        self.lazy_tokenize = False
    elif (self.model_meta is not None and self.model_meta.is_multimodal and not self.streaming
          and not self.packing and not getattr(self, 'group_by_length', False)):
        self.lazy_tokenize = True            # 仅多模态、非流式、非 packing、非 group_by_length
    else:
        self.lazy_tokenize = False
    ...
if self.lazy_tokenize:
    if self.packing:
        raise ValueError('Packing and lazy_tokenize are incompatible.')
```

> 小结整条链路：开启 packing ⇒ `lazy_tokenize=False` ⇒ `_encode_dataset` 用 `AddLengthPreprocessor` 预写 `lengths` ⇒ `_post_process_datasets` 包 `LazyLLMDataset` 再包 `PackingDataset`（读 `lengths` 装箱、强制 `padding_free`） ⇒ 训练取数时 LazyLLMDataset 延迟编码、collator 用 `packing_row` 压平。

#### 4.3.4 代码实践

**实践目标**：单独使用 padding_free（不 packing），观察 batch 内 padding 是否消失；并对照理解 `LazyLLMDataset` 的跳坏样本行为。

**操作步骤**：

1. 用同一份 `demo.jsonl`，分别跑「默认」与「padding_free」两种配置，各 1 步：

   ```bash
   # 默认（batch 内会 padding）
   swift sft --model Qwen/Qwen2.5-0.5B-Instruct --dataset demo.jsonl \
       --max_length 2048 --per_device_train_batch_size 4 --max_steps 1 \
       --attn_impl flash_attn --padding_free false --output_dir output/nofree

   # padding_free（batch 内不 padding）
   swift sft --model Qwen/Qwen2.5-0.5B-Instruct --dataset demo.jsonl \
       --max_length 2048 --per_device_train_batch_size 4 --max_steps 1 \
       --attn_impl flash_attn --padding_free true --output_dir output/free
   ```

2. 关注两处日志/现象：
   - 启动时 `template.print_inputs` 打印的第一个 batch 输入（在 `_show_dataset` 里调用）。
   - 训练时的显存占用（`nvidia-smi` 或日志里的 memory）。

**需要观察的现象**：

- `padding_free=false`：collator 会生成 `attention_mask`，短样本被 pad 到 batch 最长，`input_ids` 里有 `pad_token_id`。
- `padding_free=true`：batch 被压平，`position_ids` 呈现「0..l0, 0..l1, …」的分段重置形态，看不到 batch 维度的 padding；显存通常更低。

**预期结果**：`padding_free=true` 时 batch 的序列维度从「条数 × 最长」变成「条数之和」，无效 padding 显著减少；显存峰值低于关闭时。

3. **验证 LazyLLMDataset 跳坏样本**：故意往 `demo.jsonl` 里塞一条超长样本（远超 `max_length`），用默认配置训练，观察日志是否出现「another piece of data will be randomly selected」之类的 warning（见 [utils.py:104](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/utils.py#L104)）。再把 `--strict true` 打开，确认超长样本会直接抛错而非跳过。

> 若本地无 GPU/flash_attn，padding_free 部分可改为纯源码阅读：对照 [base.py:1869-1886](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1869-L1886) 理解「压平后 `len(batch)==1`」如何消除 padding。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 packing 一定会自动开启 padding_free？如果只 packing 不 padding_free 会怎样？

**答案**：一个 pack 是多条样本的拼接，若不 padding_free，collator 会把整条 pack 当一条普通样本去和 batch 里别的样本对齐 pad——既丢掉了 packing 的意义，又会在 pack 内部样本边界处产生错误的跨样本注意力（模型会把下一条的开头当上一条的延续）。强制 padding_free 后，靠 `position_ids` 分段 + flash-attn varlen 让 pack 内各样本因果隔离。代码见 [packing.py:66-67](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py#L66-L67)。

**练习 2**：`LazyLLMDataset.__getitem__('lengths')` 为什么能返回整列长度，而不是某一行？

**答案**：`__getitem__` 开头有 `if isinstance(idx, str): return self.dataset[idx]` 的字符串分支。当传入 `'lengths'` 这种字符串下标时，它不走编码循环，而是直接把请求透传给底层 HF Dataset 的列访问，于是返回整列。`PackingDataset.__init__` 正是靠这个机制读到 `self.dataset['lengths']`。

**练习 3**：padding_free 和 packing 该选哪个？

**答案**：看场景。padding_free 无需预处理、随开随用，适合快速实验或样本本身就较长、padding 浪费不严重时；packing 需要一次预处理装箱（短样本密集时收益巨大），训练吞吐更高、显存更稳，适合大规模正式训练。文档原文也这么建议（见 [template_args.py:63-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py#L63-L64)）。

---

## 5. 综合实践

把本讲三块内容串起来，做一个「数据效率对比」小任务。

**任务**：用同一份**短样本密集**的数据集（如 alpaca 格式，回答普遍几十~一两百 token），用同一个小模型、同一个 `max_length 2048`、同样训练 100 步，跑下面三种配置，记录每种配置的「train_dataset 条数 / 平均 token 长度 / 单步耗时 / 显存峰值」：

| 配置 | 命令关键参数 |
| --- | --- |
| A 基线 | `--padding_free false`（默认 padding） |
| B padding_free | `--padding_free true --attn_impl flash_attn` |
| C packing | `--packing true`（自动带 padding_free） |

**操作要点**：

1. 固定 `--per_device_train_batch_size`、`--learning_rate` 等无关变量，只变数据策略。
2. 训练前从日志抄下 `Dataset Token Length:` 统计（A/B 来自 `lengths`，C 来自 `packed_length`）。
3. 训练中记录每步耗时与 `nvidia-smi` 显存峰值。

**分析与预期**：

- **样本数**：A、B 相同（=原始样本数）；C 远小于 A/B（pack 数）。
- **平均长度**：A、B 接近（原始平均长度）；C 接近 `packing_length`。
- **单步耗时**：C 最快（序列维度吃满），B 次之（省了 padding 但没拼接），A 最慢。
- **显存**：C 与 B 都应低于 A；C 在短样本密集时通常最稳。

完成后，用本讲 4.2.2 的填充率公式 \( \eta = \frac{\sum l_i}{k \cdot L} \) 估算 C 的理论填充率，与你实测的 `packed_length` 均值 / `packing_length` 对比，验证装箱效率。**若本地无法跑训练，可降级为「源码阅读 + 公式推演」**：手算你的数据集在 `binpack` 下大致会得到几个 pack，与代码逻辑互相印证。**待本地验证**具体数值。

---

## 6. 本讲小结

- **编码入口统一在 `template.encode`**：`EncodePreprocessor` 把它挂到 `RowPreprocessor` 流水线上，`return_length=True` 让返回字典带上 `lengths` 字段；`AddLengthPreprocessor` 只在原行追加 `lengths`、不固化 token，服务于后续统计与装箱。
- **Packing 是「跨样本」拼接**：`calculate_matched_group` 用 `binpack`（best-fit-decreasing，会打乱顺序）或 `sequential`（保序 next-fit）把短样本装进 `packing_length` 的箱；`PackingDataset` 多进程装箱、广播到各 rank，`__getitem__` 返回一个 pack（多条样本的 list）。
- **padding_free 是「batch 内」压平**：collator 用 `packing_row` 把一个 batch 拼成一条、`position_ids` 每条从 0 重置，靠 flash-attn varlen 因果隔离，彻底消除 batch 内 padding；它**无预处理开销**，但 packing 训练更快更稳。
- **packing 强制 padding_free**：`PackingDataset.__init__` 直接置 `template.padding_free=True`，二者绑定。
- **LazyLLMDataset 是运行时容器**：取数时才调用 `template.encode`（延迟编码），遇 `MaxLengthError` 等自动跳过、随机换条（`strict=True` 时改为抛错）；其字符串下标分支还让 packing 能读到 `lengths` 列。
- **互斥关系**：packing/streaming 都与 `lazy_tokenize` 互斥；多模态默认 `lazy_tokenize=True`，开启 packing 后被强制改为 `False` 并预写 `lengths`。

---

## 7. 下一步学习建议

- **进入训练主流程**：本讲解码/packing 产出的数据集最终被 `SwiftSft` 喂给 `TrainerFactory` 选出的 Trainer。下一篇 **u5-l1（TrainerFactory 与训练器体系）** 会讲清「数据集 → Trainer → 训练循环」这一环，建议紧接着学。
- **序列并行**：padding_free 的 `position_ids` 分段思想在长文本分布式训练里会进一步演化。学完 u5 后可跳到 **u9-l2（序列并行 Ulysses 与 Ring-Attention）**，看 `sequence_parallel.prepare(..., padding_free=...)` 如何与 padding_free 协作。
- **继续读源码**：若想加深理解，建议精读 [packing.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/dataset/packing.py) 的多进程装箱与 [template/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py) 的 `data_collator`，把「数据准备 → collator → 前向」的 padding_free 全链路在脑中走通。
