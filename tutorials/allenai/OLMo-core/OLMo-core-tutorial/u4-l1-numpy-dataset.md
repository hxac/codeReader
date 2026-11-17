# u4-l1 NumpyDataset：预分词数据的内存映射加载

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 OLMo-core 的 `.npy` 预分词数据格式是什么（**无头的原始 uint 数组**），以及为什么内存映射（memory map）能让 TB 级语料加载既省内存又快。
2. 区分数据集变体家族：`NumpyFSLDataset`（定长切块）、`NumpyPaddedFSLDataset`（按文档+填充）、`NumpyPackedFSLDataset`（打包）、`NumpyInterleavedFSLDataset`（交错）、`NumpyVSLDataset`（变长课程学习），并知道各自适合什么场景。
3. 用 `NumpyFSLDatasetConfig` + `NumpyDataLoaderConfig` 这对 Config 积木组装出一个可遍历的训练数据加载器，并说清楚 `global_batch_size`（以 token 计）、`reshuffle()`、rank 切分这几个机制。

本讲是单元四（数据加载）的第一讲。前置认知：u2-l1 的「一切皆 Config」、u3-l2 的 `TransformerConfig` 工厂方法、u1-l4 的 `Trainer.fit()` 里 `train_batch` 从哪里来。

## 2. 前置知识

- **预分词（pre-tokenized）**：LLM 训练不直接吃文本。语料先用分词器（tokenizer）转成 token ID 序列（整数序列），一次性落盘；训练时反复读这些整数。这样分词只做一次，训练循环里完全没有字符串处理。
- **内存映射（memmap）**：操作系统提供的「把文件当作数组用」机制。`np.memmap` 把文件映射进进程地址空间后，访问 `arr[i]` 时内核按页把对应字节加载进内存，不需要一次性 `np.load` 整个文件。对于上百 GB 的语料，这意味着：内存占用只有实际访问到的页，且多个进程（多个 rank、多个 worker）映射同一文件时共享同一份物理页缓存。
- **EOS / PAD / BOS token**：文档结束符、填充符、文档开始符的 token ID。它们是「文档边界」信息的唯一载体——数据集就是靠扫描 EOS 位置来切分文档的。
- **dtype 与词表大小的关系**：token ID 是无符号整数。词表 ≤ 65536 时 `uint16`（每 token 2 字节）就够了；`NumpyDatasetConfig.get_dtype()` 会根据词表大小自动挑最小的能装下的类型。
- **batch size 的单位是 token**：OLMo-core 的文本数据加载器中 `global_batch_size` 指一个 batch 的 token 总数（不是实例条数）。这是 u1-l4 已出现过的约定，本讲会看到它在源码里如何落实。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/data/numpy_dataset.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py) | 数据集家族：`NumpyDatasetBase` 抽象基类、FSL/Padded/Packed/Interleaved/VSL 五种实现，以及对应的 `NumpyDatasetConfig` 配置家族 |
| [src/olmo_core/data/data_loader.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py) | 数据加载器：`NumpyDataLoaderBase`、`NumpyFSLDataLoader`、`NumpyVSLDataLoader` 与 `NumpyDataLoaderConfig` |
| [src/olmo_core/data/collator.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py) | `DataCollator`：把一组实例（dict）拼成一个 batch（堆叠 + 补齐 + 词表范围校验） |
| [src/olmo_core/data/tokenizer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/tokenizer.py) | `TokenizerConfig`：不含分词逻辑，只携带 vocab_size 与特殊 token ID 等元信息 |
| [src/olmo_core/data/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py) | 底层 IO 工具：`load_array_slice`（按字节区间读文件）、`write_array_to_disk` 等 |
| [src/olmo_core/data/types.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/types.py) | `NumpyDatasetDType` 枚举（uint8/16/32/64）、`LongDocStrategy` |
| [src/test/data/numpy_dataset_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/numpy_dataset_test.py) | 数据集测试：最小数据构造范例 |
| [src/test/data/data_loader_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/data_loader_test.py) | 加载器测试：无重复无丢失的断言写法 |

---

## 4. 核心概念与源码讲解

### 4.1 `.npy` 预分词数据格式与 `NumpyDatasetBase`

#### 4.1.1 概念说明

OLMo-core 的数据底座非常朴素：**磁盘上一组一维无符号整型数组文件，每个文件就是一条首尾相接的 token 流**（文档之间靠 EOS 分隔）。基类 `NumpyDatasetBase` 约定了所有数据集的共同职责：

- 持有一组 `paths`（本地路径或 `s3://`、`gs://`、`weka://` 等 URL）；
- 携带词表与特殊 token 元信息（`vocab_size`、`pad_token_id`、`eos_token_id`、`bos_token_id`）；
- 实现 `__len__()` / `__getitem__(index)`，返回至少含 `input_ids`（torch.LongTensor）的 dict；
- 提供 `prepare()`（预生成索引缓存的钩子）与 `fingerprint`（数据内容指纹，用于断点续训时校验数据没变）。

有一个容易踩坑的关键点：**这些 `.npy` 文件不是 `np.save()` 写出的带 128 字节头的 `.npy` 文件，而是 `np.memmap` 格式的裸字节数组（无文件头）**。读取方按「第 i 个 token = 文件第 i × itemsize 字节起、长 itemsize 字节」直接定位。如果用 `np.save()` 造数据，文件头会被当成前几个 token，数据全部错位。

#### 4.1.2 核心流程

以 `NumpyFSLDataset`（最常用的定长切块变体）为例，一次 `__getitem__(index)` 的流程：

```text
__getitem__(index)
  ├─ 用每个文件的「全局实例区间」offsets 定位：index 属于哪个文件、是其中的第几块
  ├─ array_local_index × sequence_length = 该块在文件内的起始 token 下标
  └─ load_array_slice_into_tensor(path, start, start+L, dtype)
       └─ get_bytes_range(path, start×itemsize, L×itemsize)   # 直接按字节读文件
            └─ np.frombuffer(...) → torch.LongTensor
```

实例数量由文件大小直接算出，不需要打开文件扫描内容：

\[ \text{num\_instances} = \left\lfloor \frac{\text{file\_size\_bytes}}{\text{itemsize} \times \text{sequence\_length}} \right\rfloor \]

文件尾部凑不满一块的余数 token 会被丢弃（docstring 明确说明）。

#### 4.1.3 源码精读

先看基类构造函数，注意它只存元信息、不做任何 IO：

- [numpy_dataset.py:L209-L230](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L209-L230) —— `NumpyDatasetBase.__init__`：保存 paths、pad/eos/bos token、vocab_size、dtype；要求至少一个路径；同时用 `get_fs_local_rank()` 记录文件系统本地 rank（用于多进程共享 work_dir 时避免重复预处理的协调）。

数据内容指纹——断点续训的安全带：

- [numpy_dataset.py:L296-L307](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L296-L307) —— `fingerprint` 属性：把类名、词表/特殊 token/dtype 以及每个文件的「basename + 字节数」拼进 SHA-256。加载 checkpoint 时数据加载器会比对指纹，不一致就报错（见 4.3.3）。

`NumpyFSLDataset` 的核心读取路径：

- [numpy_dataset.py:L745-L752](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L745-L752) —— `_read_chunk_from_array`：`start_idx = index × sequence_length`，然后调用 `load_array_slice_into_tensor` 读 `[start_idx, start_idx + sequence_length)` 这一段。这就是内存映射收益的直接体现——读一个实例只碰 L × itemsize 个字节。
- [data/utils.py:L312-L330](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L312-L330) —— `load_array_slice`：把 token 区间换算成**字节区间**后调 `get_bytes_range`。本地文件是 `seek + read`；URL 文件走 HTTP Range 请求或对象存储 SDK。这解释了为什么文件不能带 `.npy` 头。
- [data/utils.py:L333-L351](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L333-L351) —— `load_array_slice_into_tensor`：把读出的无符号整数数组转成 `torch.long` 张量返回。所以模型侧拿到的 `input_ids` 永远是 int64。

实例定位与数量：

- [numpy_dataset.py:L709-L743](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L709-L743) —— `_sizes_and_offsets`：对每个文件按上面的公式算出实例数，累积得到每个文件的「全局实例区间」`[(0, n1), (n1, n1+n2), ...]`，并缓存。
- [numpy_dataset.py:L668-L707](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L668-L707) —— `__getitem__`：线性扫 `offsets` 找到 `index` 所在的文件与局部下标，读出 `input_ids`；之后按需附加 `label_mask`（监督掩码文件）、`instance_mask`（n-gram 重复过滤标记）、`metadata`、`doc_lens`（文档长度，供块对角注意力掩码用）。
- [numpy_dataset.py:L535-L570](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L535-L570) —— `NumpyFSLDataset` 的 docstring：明确「所有数组的 token 首尾相接、按 `sequence_length` 切块，文档可能被切到两个实例里；不额外添加特殊 token——想要文档间有 EOS，就让它们已经在数组里」。

测试是最可靠的用法示范：

- [numpy_dataset_test.py:L40-L60](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/numpy_dataset_test.py#L40-L60) —— `test_numpy_fsl_dataset`：用 `np.memmap(tmp_path / "mmap1.npy", mode="w+", dtype=np.uint16, shape=(16,))` 造 16 个 token 的文件，`sequence_length=4` 时断言 `len(ds) == 8`、`ds[7]["input_ids"] == [28..31]`。注意造数据用的是 `np.memmap` 而非 `np.save`。

#### 4.1.4 代码实践

**实践目标**：亲手造一个最小 token 文件，验证 FSL 数据集的切块公式与「无头裸数组」格式。

**操作步骤**（示例代码，可直接存成 `scratch/fsl_min.py` 用 `python` 运行，无需 GPU、无需 torchrun）：

```python
# 示例代码
from pathlib import Path
import numpy as np
from olmo_core.data import NumpyFSLDataset, TokenizerConfig

tmp = Path("/tmp/olmo-u4l1"); tmp.mkdir(exist_ok=True)

n_tokens, seq_len = 100, 8
# 关键：用 np.memmap 造「无头」裸数组，不要用 np.save！
mmap = np.memmap(tmp / "tokens.npy", mode="w+", dtype=np.uint16, shape=(n_tokens,))
mmap[:] = list(range(n_tokens))   # token ID 就是 0..99
mmap.flush()

tok = TokenizerConfig(vocab_size=32000, eos_token_id=1, pad_token_id=2)

ds = NumpyFSLDataset(
    tmp / "tokens.npy",
    sequence_length=seq_len,
    pad_token_id=tok.pad_token_id,
    eos_token_id=tok.eos_token_id,
    vocab_size=tok.vocab_size,
)
print("len(ds) =", len(ds))                      # 预期 100 // 8 = 12
print("ds[0]   =", ds[0]["input_ids"].tolist())   # 预期 [0, 1, ..., 7]
print("ds[-1]  =", ds[-1]["input_ids"].tolist())  # 预期 [88, ..., 95]（负索引等价于 ds[11]）
print("dtype of input_ids:", ds[0]["input_ids"].dtype)  # torch.int64
```

**需要观察的现象**：

1. `len(ds)` 是否等于 `100 // 8 = 12`（尾部 4 个 token 被丢弃）。
2. `ds[0]["input_ids"]` 是 `[0..7]`，`ds[11]` 是 `[88..95]`——96..99 这 4 个 token 确实没了。
3. 负索引 `ds[-1]` 与 `ds[11]` 相同（`__getitem__` 里做了 `pos_index = len(self) + index` 的换算）。
4. 把 `np.memmap(... shape=(n_tokens,))` 换成 `np.save` 再试（写入 `(np.arange(100)).astype(np.uint16)`），观察读出的第一个实例不再是 `[0..7]`——这就是文件头错位的实证。

**预期结果**：前三条与上述数值完全一致（可由 `_get_file_size_and_length` 的公式直接推出）；第 4 条现象为「首实例变成一串乱序大数」（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：一个 `uint16` 的 token 文件大小是 3000 字节，`sequence_length=128`，实例数是多少？多少 token 被丢弃？

答：`3000 // (2 × 128) = 11` 个实例（消耗 2816 字节 = 1408 个 token），文件共 1500 个 token，丢弃 `1500 - 1408 = 92` 个。

**练习 2**：为什么 `NumpyFSLDataset.__getitem__` 里要把 EOS 相关逻辑（如 `doc_lens`）做成可选项，而不是总是计算？

答：`get_document_lengths` 需要扫描整个实例找 EOS/BOS 位置，属于 CPU 工作。只有需要块对角注意力掩码（如 `generate_doc_lengths=True`，配合 Flash Attention 的 varlen 模式）时才需要；普通训练多算白费。

**练习 3**：`fingerprint` 为什么用「basename + 字节数」而不是对文件内容求哈希？

答：对 TB 级文件求内容哈希代价太高。basename + size 是一种廉价的「数据大概率没变」的近似校验；同时 `fingerprint` 也要参与缓存索引文件的命名，必须便宜。这在 [numpy_dataset.py:L288-L307](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L288-L307) 的 `fingerprint_fields` 里可见。

---

### 4.2 数据集变体家族与 `NumpyDatasetConfig`

#### 4.2.1 概念说明

同样一份 token 流，可以按不同策略组织成「实例」（instance），于是得到一个变体家族。它们全部直接继承或绕过 `NumpyFSLDataset`：

| 变体 | 实例化策略 | 一句话场景 |
| --- | --- | --- |
| `NumpyFSLDataset` | 首尾相接、固定 `sequence_length` 等分 | 预训练默认选择，最快、零预处理 |
| `NumpyPaddedFSLDataset` | 每个文档一个实例，短的补 PAD | 文档级任务/短文本，浪费在 padding 上 |
| `NumpyPackedFSLDataset` | OBFD 装箱算法把多文档打包进一个实例 | 论文 *Fewer Truncations Improve Language Modeling* 的做法，兼顾不截断与少 padding |
| `NumpyInterleavedFSLDataset` | 多文档切块后交错排列 | NanoGPT 风格的 "primordial soup" 预训练 |
| `NumpyVSLDataset` | 实例长度取 2 的幂（min→max 分桶） | 序列长度课程学习，先短后长省算力 |
| `NumpyFSLDatasetMixture` | FSL + 按比例的混合采样 | u4-l3 混合数据主题（本讲不展开） |

与 u3 系列的组件一样，**你不直接 new 这些类，而是用 Config**：抽象基类 `NumpyDatasetConfig` 携带公共字段（`tokenizer`、`paths`/`mix`、`dtype`、`work_dir`、n-gram 过滤配置等），各子类加上自己的策略字段并实现 `build()`。`work_dir` 很重要：Padded/Packed/VSL 首次构建要在本地生成「文档索引 / 打包结果 / 分桶索引」缓存文件，都放在这里。

#### 4.2.2 核心流程

`NumpyFSLDatasetConfig.build()` 的装配流程：

```text
build()
  ├─ validate()                      # sequence_length > 0 等
  ├─ _resolve_paths_metadata()       # paths / glob 展开 / DataMix 展开 → 文件列表 + metadata
  ├─ NumpyFSLDataset(*paths, sequence_length=...,
  │     pad/eos/bos/vocab_size ← 全部取自 tokenizer 配置,
  │     dtype ← get_dtype())
  └─ _finalize()                     # 若设了 work_dir 则挂到 dataset 上
```

`get_dtype()` 的自动推断：从 uint8 起逐级尝试，找到第一个满足 `vocab_size - 1 ≤ iinfo(dtype).max` 的类型。

Padded/Packed 的额外预处理发生在 `dataset.prepare()`（通常由 `NumpyDataLoaderConfig.build()` 内部调用）：

- Padded：扫描每个文件里的 EOS 边界，把「每个文档的 (start, end)」写成索引文件（`instance-indices-*.npy`）；
- Packed：调用 `pack_documents_into_instances` 做 OBFD 装箱，产出 `document-indices / instance-offsets / documents-by-instance` 三组缓存；
- 由 `fs_local_rank == 0` 的进程独占写入，其他 rank 在 `barrier()` 后直接复用缓存。

#### 4.2.3 源码精读

公共配置字段（截选）：

- [numpy_dataset.py:L2610-L2674](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2610-L2674) —— `NumpyDatasetConfig`：`tokenizer`（必填）、`paths` 或 `mix`（二选一）、`expand_glob`、`dtype`、`metadata`、`instance_filter_config`（n-gram 重复过滤）、`work_dir`（缓存目录，官方建议多个 run 共用以省时省盘）、`ignore_fingerprint_mismatch`。
- [numpy_dataset.py:L2685-L2699](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2685-L2699) —— `get_dtype()`：未显式指定 dtype 时按词表大小自动选最小无符号整型，并打日志 `Assuming dtype ...`。
- [numpy_dataset.py:L2723-L2780](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2723-L2780) —— `_resolve_paths_metadata`：`paths` 与 `mix` 互斥校验；`expand_glob=True` 时用 `deterministic_glob_directory` 展开（保证展开顺序确定，训练可复现）；mix 路径则根据 tokenizer identifier 解析出官方配方的文件清单与标签 metadata。

FSL 配置的 build：

- [numpy_dataset.py:L2826-L2855](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2826-L2855) —— `NumpyFSLDatasetConfig` 字段：`sequence_length`、`max_target_sequence_length`（序列长度热身提示：设成最终目标长度，可以让后续更大 `sequence_length` 的数据集复用同一份 token 边界）、`generate_doc_lengths`、`label_mask_paths`、`source_mixture_config`。
- [numpy_dataset.py:L2881-L2929](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2881-L2929) —— `NumpyFSLDatasetConfig.build()`：无 mixture 时走 `_resolve_paths_metadata → NumpyFSLDataset(...)`；注意 pad/eos/bos/vocab_size 全部来自 `self.tokenizer`，这就是 4.4 节 TokenizerConfig 存在的意义。

Padded 变体的读取与补齐：

- [numpy_dataset.py:L1075-L1085](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L1075-L1085) —— `NumpyPaddedFSLDataset.__getitem__`：先按 FSL 逻辑读一块（这里经由索引文件定位到「一个文档」），然后用 `F.pad` 把 `input_ids` 补到 `sequence_length`（补 `pad_token_id`），同时生成 `label_mask`（真实 token 为 True，padding 为 False）——补齐位置不计 loss 的语义就在这里产生。

Packed 变体的读取：

- [numpy_dataset.py:L1331-L1346](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L1331-L1346) —— `NumpyPackedFSLDataset.__getitem__` 中段：从缓存读出该实例包含哪些文档 ID，再逐文档按 (start, end) 从源文件捞 token。
- [numpy_dataset.py:L1388-L1401](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L1388-L1401) —— 同函数末段：`torch.cat` 拼接多文档后统一补 PAD，产出 `{"input_ids", "label_mask"}`。装箱策略与缓存复用逻辑见 [numpy_dataset.py:L1285-L1290](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L1285-L1290) 的 `prepare()` 与其调用的 `_pack_all_documents_into_instances()`。

VSL 变体：

- [numpy_dataset.py:L2204-L2231](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2204-L2231) —— `NumpyVSLDataset` docstring：实例长度取 \([2^{\log_2 min}, 2^{\log_2 max}]\) 之间的 2 的幂，配合 `VSLCurriculum`（natural / grow_p2 / grow_linear 三种课程）决定各桶 batch 的采样概率；课程学习细节在 u6 数据侧扩展时再展开。

#### 4.2.4 代码实践

**实践目标**：体会「同一份数据 + 不同 Config → 实例数量与 padding 量差异巨大」。

**操作步骤**（示例代码）：

```python
# 示例代码
from pathlib import Path
import numpy as np
from olmo_core.data import NumpyFSLDatasetConfig, NumpyPaddedFSLDatasetConfig, TokenizerConfig

tmp = Path("/tmp/olmo-u4l1"); tmp.mkdir(exist_ok=True)
rng = np.random.default_rng(0)

# 造 20 个"文档"，每个 10~30 个 token，文档间插 EOS(=1)
docs = [rng.integers(3, 32000, size=rng.integers(10, 30)).astype(np.uint16) for _ in range(20)]
flat = np.concatenate([np.concatenate([d, np.array([1], dtype=np.uint16)]) for d in docs])
mmap = np.memmap(tmp / "docs.npy", mode="w+", dtype=np.uint16, shape=flat.shape)
mmap[:] = flat; mmap.flush()

tok = TokenizerConfig(vocab_size=32000, eos_token_id=1, pad_token_id=2)

for cfg in (
    NumpyFSLDatasetConfig(tokenizer=tok, paths=[str(tmp / "docs.npy")], sequence_length=32),
    NumpyPaddedFSLDatasetConfig(tokenizer=tok, paths=[str(tmp / "docs.npy")], sequence_length=32, work_dir=str(tmp)),
):
    ds = cfg.build()
    ds.prepare()  # Padded 需要这一步生成文档索引；直接用时务必调用
    n_pad = sum((x["input_ids"] == 2).sum().item() for x in ds)
    print(f"{type(ds).__name__}: {len(ds)} 个实例, padding token 共 {n_pad} 个")
```

**需要观察的现象**：FSL 的实例数约为 `总 token 数 // 32` 且几乎没有 PAD；Padded 的实例数等于文档数（约 20），且每个实例都有 2~22 个不等的 PAD。

**预期结果**：两行输出中 Padded 的实例数恰为 20、padding 总量 = Σ(32 − 文档长 − 1)；FSL 实例数随随机数据长度而定（待本地验证，具体数值取决于随机种子生成的文档长度）。

#### 4.2.5 小练习与答案

**练习 1**：`NumpyPackedFSLDatasetConfig` 的 `long_doc_strategy` 有哪两个取值，各自牺牲什么？

答：见 [types.py:L10-L24](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/types.py#L10-L24)：`truncate`（超长文档截断，丢弃超出部分）与 `fragment`（超长文档拆成多段，不丢 token 但文档碎片化）。

**练习 2**：为什么 `NumpyPackedFSLDataset` 的类注释说它「绕过 `NumpyFSLDataset`、直接继承 `NumpyFSLDatasetBase`」？

答：FSL 的 `offsets`/`_read_chunk_from_array` 语义是「固定长度切块」，而 Packed 需要「实例→文档列表→逐文档读取」三层索引，几乎要覆写 FSL 的全部行为，继承它只会留下无用的耦合（见 [numpy_dataset.py:L1152-L1157](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L1152-L1157) 的说明）。这是「继承看行为复用，不看名字相似」的好例子。

**练习 3**：如果你要做序列长度热身（先 2048 后 4096 训练），`max_target_sequence_length` 该怎么设？

答：第一次就用 2048 训练时设 `max_target_sequence_length=4096`。它让缓存按 4096 的粒度预计算 token 边界，之后把 `sequence_length` 改成 4096 重建时能复用同一份文档排序，保证两个阶段看到的数据顺序一致（见 [numpy_dataset.py:L2832-L2841](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2832-L2841) 字段说明）。

---

### 4.3 `NumpyDataLoader`：确定性、可恢复的分布式数据加载

#### 4.3.1 概念说明

数据集回答「第 i 个实例是什么」，数据加载器回答四个更难的问题：

1. **顺序**：一个 epoch 内实例以什么顺序消费？——全局 shuffle 索引落盘，所有 rank 看到同一份（确定性）。
2. **切分**：`dp_world_size` 个数据并行 rank、`num_workers` 个加载进程之间怎么分实例才不重不漏？
3. **进度**：断点续训时从哪个 batch 继续？——`state_dict` / `load_state_dict` + 数据指纹校验。
4. **拼装**：若干实例如何变成一个 batch 张量？——委托给 `DataCollator`。

模块 docstring 一句话点题：*Distributed, deterministic, stateful data loaders used by the Trainer*（[data_loader.py:L1-L4](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L1-L4)）。

直接使用（脱离 Trainer）时有一条铁律：**每个 epoch 开始前调 `reshuffle(epoch)`，结束后调 `reset()`**，否则数据顺序错乱。

#### 4.3.2 核心流程

以 `NumpyFSLDataLoader` 为例，一次 epoch 的流水线：

```text
reshuffle(epoch=1)
  └─ build_and_save_global_indices()      # rank0: shuffle(全量实例号) → work_dir/global_indices_*.npy

__iter__() → _iter_batches()
  └─ torch.utils.data.DataLoader(_IterableDatasetWrapper, batch_size=None, num_workers=...)
       └─ _IterableDatasetWrapper.__iter__()
            ├─ get_global_indices()               # memmap 读全局顺序（各进程一致）
            ├─ _get_local_instance_indices():     # 四步裁剪
            │    ① 截断到 total_size（尾部凑不满整 batch 的丢弃）
            │    ② reshape 成 (num_batches, instances_per_batch)
            │    ③ 跳过前 batches_processed 个 batch（断点续训）
            │    ④ [worker 切片] → [dp_rank 切片: indices[:, dp_rank::dp_world_size]]
            ├─ 多线程预取实例（threaded_generator + roundrobin）
            └─ iter_batched(实例流, rank_batch_size) → collator(每 rank_batch_size 个实例) → batch
```

关键公式：

\[ \text{rank\_batch\_size} = \frac{\text{global\_batch\_size}}{\text{dp\_world\_size}}, \qquad \text{instances\_per\_batch} = \frac{\text{global\_batch\_size}}{\text{sequence\_length}} \]

`TextDataLoaderBase.__iter__` 会硬校验每个 batch 的 token 数恰为 `rank_batch_size`。

#### 4.3.3 源码精读

生命周期约定：

- [data_loader.py:L55-L82](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L55-L82) —— `DataLoaderBase` docstring：警告必须「先 `reshuffle()` 再迭代、迭代完 `reset()`」，并给出代码示例；构造参数含 `global_batch_size`、`dp_world_size`、`dp_rank`、`fs_local_rank`。
- [data_loader.py:L200-L205](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L200-L205) —— `rank_batch_size` 属性：`global_batch_size // dp_world_size`，单位 token。
- [data_loader.py:L306-L314](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L306-L314) —— `TextDataLoaderBase.__iter__`：断言 batch 的 `input_ids.numel() == rank_batch_size`，不符直接抛 `RuntimeError`。

全局顺序的生成与复用：

- [data_loader.py:L667-L690](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L667-L690) —— `NumpyFSLDataLoader._build_global_indices`：`chunk_size == 1` 时就是 `arange(len(dataset))` + 用 `get_rng(seed + epoch)` 洗牌——**同一 (seed, epoch) 在任何机器上洗出同一顺序**，这就是「确定性」的实现。
- [data_loader.py:L503-L533](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L503-L533) —— `build_and_save_global_indices`：只有 `fs_local_rank == 0` 的进程写文件，其他 rank 在 `barrier()` 之后通过 `np.memmap` 共享读。文件名里编入了 seed/epoch/数据集大小（`_global_indices_file`，[data_loader.py:L655-L665](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L655-L665)），天然缓存复用。

本地实例的裁剪（与 `__getitem__` 保持逻辑一致）：

- [data_loader.py:L720-L747](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L720-L747) —— `_get_local_instance_indices`：源码里两处 NOTE 强调「这里必须与 `__getitem__()` 的逻辑一致」；worker 采用「整 batch 轮流分配」（worker 0 拿第 0 个 batch，worker 1 拿第 1 个……），DP rank 则在 batch 内按 `indices[:, dp_rank::dp_world_size]` 取条。
- [data_loader.py:L641-L653](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L641-L653) —— `total_size` / `total_batches`：先算每 batch 实例数再向下取整，尾部不完整的 batch 丢弃；所以「无重复无丢失」成立的范围是 `total_size` 以内。

状态保存与恢复：

- [data_loader.py:L441-L478](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L441-L478) —— `state_dict` 记录数据指纹、`batches_processed`、`tokens_processed`、seed、epoch；`load_state_dict` 先比对指纹（不一致抛错，除非 `ignore_fingerprint_mismatch=True`），再恢复进度——这就是 u6-l4 断点续训时「数据侧不乱」的保证。
- [data_loader.py:L535-L542](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L535-L542) —— `reshuffle`：设置 `_epoch` 并重建全局索引；epoch 编号从 1 开始。

真正的迭代引擎：

- [data_loader.py:L1021-L1088](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L1021-L1088) —— `_IterableDatasetWrapper`：把 OLMo-core 加载器包成 torch 的 `IterableDataset`，从而免费获得 `num_workers` 多进程预取；单进程时默认开 4 条读文件的线程（`num_threads`），用 `threaded_generator` + `roundrobin` 让磁盘 IO 与 collator 重叠。
- [data_loader.py:L544-L569](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L544-L569) —— `get_mock_batch`：按 vocab 随机生成一个形状正确的假 batch，Trainer 开训前的 dry-run 预热（u1-l4 讲过的 mock batch 预热）就是用它。

Config 入口：

- [data_loader.py:L1105-L1167](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L1105-L1167) —— `NumpyDataLoaderConfig`（注册名 `"numpy"`，`DataLoaderConfig` 是 Registrable——这正是 u2-l2 讲的多态字段模式）：`build(dataset, collator=None, mesh=None, dp_process_group=None)` 内部做三件事——① 把 `work_dir` 同步给 dataset（若它还没设）；② 调用 `dataset.prepare()`（生成/复用缓存索引）；③ `wrap_numpy_dataset` 按数据集类型分发到 `NumpyFSLDataLoader` 或 `NumpyVSLDataLoader`（[data_loader.py:L388-L439](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L388-L439)），collator 未提供时默认 `DataCollator(pad_token_id=..., vocab_size=...)`。未初始化分布式时 `get_world_size()` 返回 1，因此单进程也能直接 build。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用随机 token 构造 `.npy` 数据，走完整的 `NumpyFSLDatasetConfig → NumpyDataLoaderConfig.build() → reshuffle → 迭代` 流程，打印 batch 形状并验证数据无重复、无丢失。

**操作步骤**（示例代码，存为 `scratch/loader_demo.py`，CPU 单进程即可运行）：

```python
# 示例代码
from pathlib import Path
import numpy as np
from olmo_core.data import (
    NumpyFSLDatasetConfig, NumpyDataLoaderConfig, TokenizerConfig,
)

tmp = Path("/tmp/olmo-u4l1"); tmp.mkdir(exist_ok=True)

# 1) 造数据：1024 个随机 token，uint16
n_tokens = 1024
rng = np.random.default_rng(42)
mmap = np.memmap(tmp / "tokens.npy", mode="w+", dtype=np.uint16, shape=(n_tokens,))
mmap[:] = rng.integers(0, 32000, size=n_tokens).astype(np.uint16)
mmap.flush()

# 2) 组装 Config（注意：global_batch_size 单位是 token）
tok = TokenizerConfig(vocab_size=32000, eos_token_id=1, pad_token_id=2)
ds_cfg = NumpyFSLDatasetConfig(
    tokenizer=tok, paths=[str(tmp / "tokens.npy")], sequence_length=64,
)
dl_cfg = NumpyDataLoaderConfig(
    global_batch_size=256,   # 256 token/batch → 4 个实例/batch
    seed=1234,
    work_dir=str(tmp / "work"),  # 全局索引缓存目录
)

# 3) build + 迭代
dataset = ds_cfg.build()
data_loader = dl_cfg.build(dataset=dataset)   # 内部会调 dataset.prepare()

data_loader.reshuffle(epoch=1)                # 铁律：先 reshuffle
seen = []
for i, batch in enumerate(data_loader):
    print(f"batch {i}: input_ids shape = {tuple(batch['input_ids'].shape)}, "
          f"keys = {sorted(batch.keys())}")
    seen.extend(batch["input_ids"].flatten().tolist())
data_loader.reset()                           # 铁律：后 reset

print("total_batches =", len(data_loader))    # 1024 // 256 = 4
print("无重复:", len(set(seen)) == len(seen))
print("无丢失:", len(seen) == 1024)
```

**需要观察的现象**：

1. 每个 batch 打印 `input_ids shape = (4, 64)`——因为 `256 token / 64 sequence_length = 4` 个实例（`dp_world_size=1`）。
2. batch 的 keys 里除了 `input_ids` 还有 `metadata`（因为 `include_instance_metadata` 默认 True 且 metadata 为空 dict）。
3. `total_batches == 4`，`无重复` 与 `无丢失` 均为 True（shuffle 只改顺序，不重不漏——这正是 [data_loader_test.py:L20-L84](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/data_loader_test.py#L20-L84) `test_fsl_data_loader` 用两个 rank 验证的同款断言）。
4. 把 `n_tokens` 改成 1000（不是 256 的整数倍），`total_batches` 仍是 3（`1000 // 256 = 3`，尾部 232 token 被丢弃），「无丢失」变为 False——体会 `total_size` 的取整语义。

**预期结果**：以上 4 条均可由源码公式推出；形状与 batch 数为确定性结果（待本地验证具体打印格式）。

#### 4.3.5 小练习与答案

**练习 1**：`global_batch_size=1024`、`sequence_length=256`、8 张卡纯数据并行，每个 rank 每个 batch 拿到几个实例？

答：`rank_batch_size = 1024/8 = 128` token；实例数 = `128/256 = 0.5`——构造时会直接抛 `OLMoConfigurationError`（[data_loader.py:L636-L639](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L636-L639) 校验 `rank_batch_size % sequence_length == 0`）。该配置本身不合法：global batch 至少要能被 `world_size × sequence_length` 整除。

**练习 2**：为什么全局索引要落盘成文件，而不是每个 rank 各自 shuffle 一份？

答：两个原因：① 确定性——同一 `(seed, epoch, dataset_size)` 生成的文件名相同，重启续训时直接复用，保证顺序一致；② 分布式一致性——batch 内按 rank 切片要求所有 rank 看到同一份全局顺序，由 rank0 写文件 + barrier + memmap 共享是最简单可靠的做法。

**练习 3**：`batches_processed` 在 `load_state_dict` 恢复后如何生效？

答：恢复后它被 `_get_local_instance_indices` 的第 ③ 步使用：`indices = indices[self.batches_processed:]`，即从全局顺序中跳过已消费的 batch 再继续迭代（[data_loader.py:L733-L735](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/data_loader.py#L733-L735)）。

---

### 4.4 TokenizerConfig 与 DataCollator：管线两端的契约

#### 4.4.1 概念说明

**`TokenizerConfig` 不是分词器**。训练时根本不做分词（数据早已预分词），它只是一个携带「词表元信息」的 Config：`vocab_size`、`eos_token_id`、`pad_token_id`、`bos_token_id`、`identifier`。它在数据侧有三处消费：

1. `NumpyDatasetConfig.get_dtype()` 用 `vocab_size` 推断存储 dtype；
2. 数据集构造参数 pad/eos/bos/vocab_size 全部取自它（见 4.2.3）；
3. `mix` 模式下用 `identifier` 定位官方配方中对应分词器的文件清单。

**`DataCollator` 是实例 → batch 的最后一道工序**：找 batch 内最长序列，把短的补 PAD，按字段分组堆叠成张量。它还承担一项防御性校验——若设置了 `vocab_size`，检查所有 token ID 落在 `[0, vocab_size)` 内，把「脏数据导致的 CUDA 越界」提前变成一条带定位信息的 Python 错误（对 `torch.compile` 尤其重要，否则错误信息不可读）。

#### 4.4.2 核心流程

```text
instances: [{"input_ids": LongTensor[L], "label_mask": BoolTensor[L](可选), "metadata": ..., "index": ...}, ...]
   │
   ▼ DataCollator.__call__
max_len = max(len(x) for x in instances)
对每个实例: F.pad(右/左补 pad_token_id 到 max_len)
   │
   ▼ 逐字段堆叠
{"input_ids": (B, max_len), "label_mask": (B, max_len)?, "index": (B)?, ...}
   │
   ▼ vocab_size 校验（若设置）
invalid = (ids < 0) | (ids >= vocab_size) → 有则抛 ValueError（附前 10 个越界位置）
```

#### 4.4.3 源码精读

- [tokenizer.py:L46-L75](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/tokenizer.py#L46-L75) —— `TokenizerConfig` 字段：五个元信息，`identifier` 可选（mix 模式必填）。
- [tokenizer.py:L84-L94](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/tokenizer.py#L84-L94) —— 工厂方法 `TokenizerConfig.dolma2()`：OLMo 2/3 系列使用的分词器（vocab 100278）。另有 `gpt2()`、`from_hf(identifier)`（[L134-L159](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/tokenizer.py#L134-L159)，从 HF 模型的 config.json 读取元信息）。
- [tokenizer.py:L77-L82](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/tokenizer.py#L77-L82) —— `padded_vocab_size()`：把词表补齐到 128 的倍数，模型 embedding 用它可提升吞吐（连接 u3-l2 的 `TransformerConfig.vocab_size`）。
- [collator.py:L23-L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L23-L49) —— `DataCollator` 字段：`pad_token_id`、`pad_direction`（默认右）、`label_ignore_index`（-100）、`vocab_size`（校验开关）；`__call__` 先求 `max_len`。
- [collator.py:L63-L81](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L63-L81) —— 补齐 `input_ids` 并统一转 `torch.long`。
- [collator.py:L113-L124](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L113-L124) —— `label_mask` 存在则同样补齐（padding 处补 False）；对 FSL 数据集（不带 label_mask）batch 中就没有这个键，loss 由模型侧对 PAD token 的处理决定。
- [collator.py:L148-L159](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L148-L159) —— 堆叠 + 词表范围校验：错误信息里列出越界 ID 与 `(batch_idx, pos)` 前 10 个位置。
- [collator.py:L161-L176](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L161-L176) —— 可选输出：`attention_mask`、`attention_bias`、`label_mask`、`index`、`instance_mask`、`doc_lens`/`max_doc_lens`、`metadata`——只有实例里带了才出现在 batch 里（「按需字段」设计）。

#### 4.4.4 代码实践

**实践目标**：触发并读懂 collator 的词表越界校验。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
from olmo_core.data import DataCollator

collator = DataCollator(pad_token_id=2, vocab_size=100)
instances = [
    {"input_ids": torch.tensor([5, 9, 42])},
    {"input_ids": torch.tensor([7, 130])},   # 130 >= 100，越界
]
try:
    collator(instances)
except ValueError as e:
    print("捕获预期错误：", e)
```

**需要观察的现象**：抛出 `ValueError`，消息形如 `Token IDs [130] outside valid range [0, 100). Found at (batch_idx, pos): [[1, 1]]`。

**预期结果**：如上（逻辑可由 [collator.py:L150-L159](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/collator.py#L150-L159) 直接推出；具体文案待本地验证）。再把 `vocab_size` 改成 `None` 或去掉，collator 正常返回 `input_ids` 形状 `(2, 3)`——第二行被补了一个 `pad_token_id=2`。

#### 4.4.5 小练习与答案

**练习 1**：`DataCollator` 的 `pad_direction` 何时需要 `left`？

答：右侧 padding 会把 PAD 放在序列末尾，配合因果语言模型没问题；但在生成/推理场景或某些注意力实现（如 varlen 之外的 padding 方案）中，为了方便取「最后一个非 pad 位置」，需要把 PAD 放序列开头，即 `left`。训练主线用默认 `right`。

**练习 2**：为什么 `TokenizerConfig` 是数据集 Config 的必填字段，而模型侧只是拿 `vocab_size`？

答：数据侧需要 pad/eos/bos 三个特殊 token ID 来切文档、补齐、过滤；这些 ID 只在「这套 token 流是用哪个分词器产的」语境下才有意义。让它成为数据集 Config 的一部分，等于把「数据与分词器绑定」这一事实显式化，防止拿 A 分词器的数据配 B 分词器的模型。

**练习 3**：FSL 数据集的实例没有 `label_mask`，那 padding 的 token 会计入 loss 吗？

答：FSL 根本不产生 padding（等长切块），所以不存在这个问题；这正是 FSL 对 padded 变体的核心优势。对 Padded/Packed 变体，数据集在 `__getitem__` 里生成 `label_mask`（真实 token True / padding False），模型侧用它决定哪些位置不计 loss（u3-l4 讲 LMHead 时提过的 loss 掩码路径）。

---

## 5. 综合实践

**任务：搭建一个「双变体对照」迷你数据管线，并验证加载器的无重复无丢失不变量。**

1. 用 `np.memmap` 造一个约 4096 token 的数据文件：内容为若干「随机长度文档 + EOS 分隔」，dtype `uint16`。
2. 分别用 `NumpyFSLDatasetConfig(sequence_length=128)` 与 `NumpyPackedFSLDatasetConfig(sequence_length=128)`（`work_dir` 指向同一目录）构建数据集，调用 `prepare()` 后比较 `len(ds)` 与 padding token 总量，用一句话解释差异。
3. 用同一个 `NumpyDataLoaderConfig(global_batch_size=512, seed=7, work_dir=...)` 分别 build 两个加载器，`reshuffle(epoch=1)` 后完整迭代，打印每个 batch 的 `input_ids.shape`，并把 FSL 版本的所有 token 收集起来断言 `set(seen) == set(range(vocab))` 不成立但 `len(set(seen)) == len(seen)`（提示：随机 token 有重复值，应断言的是「multiset 相等」——把造数据时的数组存下来用 `sorted(seen) == sorted(original.tolist())` 比较）。
4. 再跑第二个 epoch：`reshuffle(epoch=2)` → 迭代，收集顺序并与 epoch 1 对比，验证「token 集合相同、顺序不同」——亲眼确认 `(seed, epoch)` 决定顺序的确定性设计。

完成标志：能不看讲义说出 FSL 与 Packed 在实例数、padding 量上的差异原因，以及 `reshuffle/reset` 各自守护的不变量。

## 6. 本讲小结

- OLMo-core 的数据底座是**无头的裸 uint 数组文件**（`np.memmap` 格式，非 `np.save`），读取按字节偏移直取，内存映射让 TB 级语料的加载只触碰实际访问的页。
- 数据集家族回答「token 流如何组织成实例」：FSL 等长切块（默认）、Padded 按文档补齐、Packed OBFD 装箱、Interleaved 交错、VSL 分桶课程学习；全部通过 `NumpyDatasetConfig` 子类 + `build()` 使用，`prepare()` 负责生成/复用落盘的索引缓存。
- `NumpyDataLoader` 的三件套设计：**确定性**（`(seed, epoch)` 洗牌、全局索引落盘共享）、**可恢复**（`state_dict` + 数据指纹校验 + `batches_processed` 跳批）、**分布式切分**（batch 内按 `dp_rank::dp_world_size` 取实例，worker 按整 batch 轮流分配）。
- `global_batch_size` 的单位是 **token**；`rank_batch_size = global_batch_size / dp_world_size` 必须能被 `sequence_length` 整除，batch 形状为 `(rank_batch_size/L, L)`。
- `TokenizerConfig` 只携带词表元信息（vocab_size + 特殊 token ID），却是数据侧 dtype 推断、文档切分、补齐行为的共同来源；`DataCollator` 负责实例→batch 的补齐堆叠，并把词表越界提前变成可读错误。

## 7. 下一步学习建议

本讲讲了「单源、单数据集」的经典加载路径。下一讲 **u4-l2 Composable 数据管线** 进入 OLMo-core 目前推荐的新一代 API（`TokenSource → InstanceSource → ComposableDataLoader`），它把切片、采样、按比例混合、packing 变成可组合的算子。如果关心多源混合配方与 dolma17 等预置 mix，可接着读 **u4-l3**。延伸阅读源码：`src/olmo_core/data/utils.py`（文档索引/装箱的完整实现）与 `src/test/data/data_loader_test.py`（多 rank 一致性的测试写法，值得模仿）。
