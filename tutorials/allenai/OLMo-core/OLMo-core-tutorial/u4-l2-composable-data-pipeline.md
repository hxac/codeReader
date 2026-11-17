# Composable 数据管线：TokenSource → InstanceSource → DataLoader

## 1. 本讲目标

上一讲（u4-l1）我们学习了 OLMo-core 的「第一代」数据加载体系：`NumpyDataset` 家族 + `NumpyDataLoader`。那一体系的特点是**一个类对应一种数据集变体**——FSL 定长切块、Packed 装箱、VSL 分桶……想改数据组织方式就得换一个类、换一份配置。

本讲进入 OLMo-core 官方推荐的「新一代」数据 API：`data/composable/`。它把数据管线拆成三层可以自由组合的小积木：

```
TokenSource（token 从哪来）→ InstanceSource（怎么切成定长训练样本）→ ComposableDataLoader（怎么组成 batch 喂给 Trainer）
```

学完本讲你应该能够：

1. 说清 `TokenSource`、`InstanceSource`、`ComposableDataLoader` 三层抽象各自的接口契约与职责边界。
2. 用 `split()` / `sample()` / `+` / `*` / `MixingTokenSource` 等组合子搭建「切片、采样、按比例混合、按阶段切分」的数据管线。
3. 用 `InstanceSource.visualize()` 打印源树来调试自己的数据管线，并读懂输出里每一列的含义。
4. 理解按比例混合背后的采样数学（`calculate_sample_sizes`），能手算一个混合配方的实际输出规模。
5. 理解 `ComposableDataLoader` 的洗牌策略、全局索引落盘、按数据并行 rank 切批与断点续训机制。

## 2. 前置知识

- **组合模式（composite pattern）**：让「小对象」和「由小对象组成的大对象」实现同一个接口，于是大对象可以继续被组合成更大的对象。本讲的源树（mix 套 slice 套 in-memory）就是这个模式的数据版。
- **内容指纹（fingerprint）**：对数据内容做 SHA-256 哈希得到的确定性字符串。它回答「这份数据和上次那份数据是不是同一份」，是断点续训时防止「数据变了还接着旧进度训练」的守门员（上一讲 `NumpyDataLoader` 的 state_dict 数据指纹校验也是同一思想）。
- **内存映射（memmap）回顾**：上一讲讲过的 `.npy` 裸数组 + `np.memmap` 按需读页的技术，在本讲依然到处使用（`NumpyDocumentSource` 读数据、数据加载器落盘全局索引）。
- **`work_dir`**：每个源在构造时都必须给一个**本地**缓存目录（不能是 URL），预处理产物（装箱结果、索引文件）会缓存在这里，避免重复计算。
- **`global_batch_size` 以 token 计**：上一讲建立的关键约定，本讲的 `ComposableDataLoader` 完全沿用：一个 batch 的 token 总量 = 实例数 × `sequence_length`。

另外建议手边打开 [src/olmo_core/data/composable/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py) 的模块 docstring——它本身就是一份带可运行示例的教程，本讲多个例子直接取材于此。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/data/composable/__init__.py` | 门面：再导出全部公开类 + 一份很长的模块 docstring 教程（混合、上采样、课程学习示例） |
| `src/olmo_core/data/composable/source_abc.py` | 所有「源」的公共基类 `SourceABC`：`work_dir`/`label`/`fingerprint`/`num_tokens`/`children` |
| `src/olmo_core/data/composable/token_source.py` | `TokenSource`/`DocumentSource` 抽象与 `InMemory*`/`Concatenated*` 实现，以及组合方法 `sample`/`resize`/`split`/`+`/`*` |
| `src/olmo_core/data/composable/instance_source.py` | `InstanceSource` 抽象、`Instance` 类型、`visualize()`、`split()`/`random_split()`，及对应 Config 基类 |
| `src/olmo_core/data/composable/concat_and_chunk_instance_source.py` | 最简单的实例源：无视文档边界连续切块（等价 `NumpyFSLDataset`） |
| `src/olmo_core/data/composable/packing_instance_source.py` | 文档装箱实例源：OBFD 算法把整篇文档装进定长实例（等价 `NumpyPackedFSLDataset`） |
| `src/olmo_core/data/composable/mixing_token_source.py` | 按比例混合多个 token 源 |
| `src/olmo_core/data/composable/sampling_token_source.py` / `sliced_token_source.py` | 采样（连续块 + 可控重复）与切片组合子（token 层） |
| `src/olmo_core/data/composable/sampling_instance_source.py` / `sliced_instance_source.py` | 同上，但作用在实例层 |
| `src/olmo_core/data/composable/data_loader.py` | `ComposableDataLoader` 与其 Config（注册名 `composable`）、`ShuffleStrategy` |
| `src/olmo_core/data/composable/utils.py` | 混合采样数学 `calculate_sample_sizes`、全局索引 `build_global_indices`、`set_composable_seed` |
| `src/olmo_core/data/composable/visualize.py` | 源树打印的实现 |
| `src/test/data/composable/` | 与上述模块一一对应的测试，是学习用法的最佳范例 |

建议阅读顺序：`source_abc.py` → `token_source.py` → `instance_source.py` → `mixing_token_source.py` + `utils.py` → `data_loader.py`。

## 4. 核心概念与源码讲解

### 4.1 TokenSource：把「token 数组」抽象成可组合对象

#### 4.1.1 概念说明

`TokenSource` 表示**一维 token 数组**：总长 `num_tokens`，可以按区间取值 `get_token_range(start, end)`。就这么简单。它的价值不在接口本身，而在两点：

1. **一切 token 数据都长这个样子**：内存里的 list、一串 `.npy` 文件、另一个源的切片……只要实现这 4 个成员（`num_tokens`、`get_token_range`、`fingerprint`、`children`），就能被上层统一消费。
2. **基类自带组合代数**：`+`（拼接）、`*`（按倍数缩放）、`sample()`（采样）、`split()`（切分）都是返回新源对象的纯函数，于是「取某数据前 80% 再上采样 2 倍」可以写成一行链式表达式。

`DocumentSource` 是 `TokenSource` 的特化：额外知道**文档边界**（`get_document_offsets()`）。装箱（packing）这类需要整篇文档的操作只能建立在 `DocumentSource` 上。

#### 4.1.2 核心流程

任何 `TokenSource` 的消费流程都是：

```
消费者（通常是 InstanceSource）
  └─ num_tokens               # 先问总长（决定能切多少实例）
  └─ get_token_range(s, e)    # 再按区间拉取 token（可跨子源）
```

`get_token_range` 允许区间从文档中间开始、横跨多份文档——是否尊重文档边界由消费者决定（`ConcatAndChunk` 不尊重，`Packing` 尊重）。

组合方法的分工：

| 方法 | 返回 | 语义 |
| --- | --- | --- |
| `s1 + s2` | `ConcatenatedTokenSource` | 首尾相接成一个更长的源 |
| `s.sample(max_tokens=…)` | `SamplingTokenSource` | 取出连续一段（可用 seed 随机起点） |
| `s.resize(f)` / `s * f` | `SamplingTokenSource` | 按 `num_tokens × f` 采样；`f > 1` 时通过重复实现 |
| `s.split(ratio)` | `(SlicedTokenSource, SlicedTokenSource)` | 按比例切成两个**不相交**的源 |

#### 4.1.3 源码精读

公共契约在 `SourceABC`：`work_dir` 必须是本地路径，`fingerprint` 与 `children` 是抽象成员——后者是 `visualize()` 能递归打印源树的原因。

[src/olmo_core/data/composable/source_abc.py:L11-L33](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/source_abc.py#L11-L33) 定义了 `SourceABC`：构造时校验 `work_dir` 是本地路径并记录分布式 rank 信息（`fs_local_rank` 用于「只有 0 号进程写缓存、其余进程等待」的约定，后面装箱和索引落盘都会用到）。

[src/olmo_core/data/composable/source_abc.py:L71-L86](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/source_abc.py#L71-L86) 三个抽象成员：`fingerprint`（「代表源内容有序状态的唯一确定性字符串」）、`num_tokens`、`children`。

`TokenSource` 的核心抽象方法与数据载体：

[src/olmo_core/data/composable/token_source.py:L37-L46](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L37-L46) `TokenRange` 只是一个 TypedDict：`input_ids` 必填，`label_mask` 可选（哪些位置计入损失，承接上一讲「padding 不计损失」的语义）。

[src/olmo_core/data/composable/token_source.py:L64-L74](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L64-L74) 抽象方法 `get_token_range(start_idx, end_idx)`：取 `[start, end)` 的连续 token；docstring 明确说明区间可能横跨文档边界，由消费者负责取舍。

[src/olmo_core/data/composable/token_source.py:L76-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L76-L92) `__getitem__` 支持切片与整数索引（含负索引），让 `TokenSource` 用起来像 numpy 数组：`s[10:20]`、`s[-1]` 都合法。

组合代数的实现（注意它们都是「构造新源」而非修改自身）：

[src/olmo_core/data/composable/token_source.py:L170-L184](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L170-L184) `split(ratio)`：`split_idx = int(ratio * num_tokens)`，前段 `slice(0, split_idx)`、后段 `slice(split_idx, None)`，两个切片合起来正好覆盖原源、互不重叠。

[src/olmo_core/data/composable/token_source.py:L113-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L113-L123) `__add__`：两个 `DocumentSource` 相加得到 `ConcatenatedDocumentSource`，否则得到 `ConcatenatedTokenSource`——类型信息在组合中保持。

两个最常用的叶子/容器实现：

[src/olmo_core/data/composable/token_source.py:L187-L233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L187-L233) `InMemoryTokenSource`：把 token 存在内存里（主要供测试与实验），`fingerprint` 直接对 token 字节做 SHA-256。

[src/olmo_core/data/composable/token_source.py:L236-L289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L236-L289) `DocumentSource` 及其抽象方法 `get_document_offsets()`：按顺序给出每篇文档的 `(start, end)`；`InMemoryDocumentSource` 用 EOS/BOS token 的位置推断文档边界。

最后注意 Config 侧一个容易忽略的细节：

[src/olmo_core/data/composable/token_source.py:L357-L364](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L357-L364) `TokenSourceConfig.build()` 返回的是 **`List[TokenSource]`** 而非单个源——因为一个配置里的 glob 模式（如 `gs://... /**.npy`）可能展开成成百上千个文件源，每个文件对应一个 `NumpyDocumentSource`。上层（如 `ConcatenatedTokenSourceConfig`）会再把列表合并成一个源。

#### 4.1.4 代码实践

**实践目标**：在 Python 里亲手体验 `TokenSource` 的组合代数，确认每个组合子的输出规模。

操作步骤（示例代码，可在装好 olmo-core 的环境里逐段执行；参考模块 docstring 的基础示例 [src/olmo_core/data/composable/__init__.py:L53-L68](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L53-L68)）：

```python
from olmo_core.data.composable import InMemoryTokenSource

work_dir = "/tmp/token-source-play"
s = InMemoryTokenSource(list(range(100)), work_dir=work_dir, label="demo")

print(len(s))                   # 组合子操作前的基准规模
print(s[10:20]["input_ids"])    # 切片访问
print(s[-1]["input_ids"])       # 负索引

head, tail = s.split(0.8)       # 切成 80% / 20%
half = s.sample(max_tokens=50, seed=None)   # 连续采样 50 个（seed=None → 从头取）
double = s * 2                  # 上采样 2 倍
combined = head + half          # 拼接
print(len(head), len(tail), len(half), len(double), len(combined))
combined.visualize()            # 打印源树
```

需要观察的现象与预期结果：

- 规模依次为 `100`、`[10..19]`、`[99]`、`80 20 50 200 130`。其中 `s * 2 = 200` 体现了「上采样 = 整源重复一遍」（见 4.3 的采样实现）。
- `visualize()` 输出一棵树（示意，指纹值以本机为准）：

```
ConcatenatedTokenSource(xxxxxxx): 130 tokens
├─ SlicedTokenSource(xxxxxxx): 80 tokens
│  └─ InMemoryTokenSource(xxxxxxx): 100 tokens [demo]
└─ SamplingTokenSource(xxxxxxx): 50 tokens
   └─ InMemoryTokenSource(xxxxxxx): 100 tokens [demo]
```

- `visualize` 依赖 `rich` 渲染颜色（`rich` 是核心依赖，无需额外安装），图标需要 Nerd Font 才不乱码，可用 `visualize(icons=False)` 关闭。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SourceABC` 要把 `fingerprint` 设计成抽象属性，而不是用对象的内存地址或 Python 的 `id()`？
**答案**：指纹的用途是跨进程、跨重启地确认「数据内容相同」（4.4 的 `load_state_dict` 会用它校验断点续训的数据一致性，[data_loader.py:L367-L385](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L367-L385)）。内存地址每次运行都变，无法担当此任；它必须是**内容**的确定性哈希，且组合源要把子源指纹拼接进自己的指纹（如 [token_source.py:L516-L522](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/token_source.py#L516-L522)），子源变了父源指纹就变。

**练习 2**：`TokenSourceConfig.build()` 为什么返回列表，而后面会看到 `InstanceSourceConfig.build()` 只返回单个对象？
**答案**：token 源配置常含 glob 路径，一个配置自然展开成多个文件源（每个 `.npy` 一个源）；而实例源已经是「切好片的最终形态」，Config 的 `build` 把（可能多个的）token 源吸收进一个 `InstanceSource`，例如 `ConcatAndChunkInstanceSourceConfig.build()` 内部先展平所有子配置再构造单一实例源（[concat_and_chunk_instance_source.py:L61-L68](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/concat_and_chunk_instance_source.py#L61-L68)）。

**练习 3**：`s.split(0.8)` 返回的两个源与原源 `s` 是什么关系？会复制数据吗？
**答案**：是两个 `SlicedTokenSource` 包装器，持有对同一个 `s` 的引用和不同的 `slice`，不复制任何 token 数据；读取时才按区间向 `s` 转发请求。

### 4.2 InstanceSource：把 token 流切成定长训练样本

#### 4.2.1 概念说明

语言模型训练吃的是**定长 batch**：每个样本恰好 `sequence_length` 个 token。`InstanceSource` 就是「定长样本的数组」：`len()` 给样本数，`__getitem__(idx)` 返回一个 `Instance`（`input_ids` + 可选 `label_mask`）。它是 token 世界与训练世界之间的适配器。

两大「token → 实例」生成器对应上一讲 `NumpyFSLDataset` 与 `NumpyPackedFSLDataset` 的 composable 版：

- **`ConcatAndChunkInstanceSource`**：把所有子源的 token 首尾相接、无视文档边界地每 `sequence_length` 个切一片。简单、快、零 padding，但一篇文档会被切断在任意位置。
- **`PackingInstanceSource`**：只接受 `DocumentSource`，用 OBFD（Optimized Best-Fit Decreasing）装箱算法把**整篇文档**装进实例，装不满用 pad 补齐。文档不跨实例、不截断（长文档按 `LongDocStrategy` 处理），代价是需要预处理装箱并产生少量 padding。

在此之上还有一层「实例级组合子」：`Concatenated / Sliced / Sampling / Mixing InstanceSource`，语义与 token 层同名组合子一致，只是操作单位从 token 变成了实例。

#### 4.2.2 核心流程

`ConcatAndChunk` 的实例数计算（多退少补的「只退」）：

\[ \text{num\_instances} = \sum_{\text{source}} \left\lfloor \frac{\text{source.num\_tokens}}{\text{max\_sequence\_length}} \right\rfloor \times \frac{\text{max\_sequence\_length}}{\text{sequence\_length}} \]

即：先按 `max_sequence_length` 对齐丢尾，再按倍数细分。`max_sequence_length` 是为「训练中途加大序列长度仍保持数据顺序不变」预留的参数（见 4.4.2）。

`Packing` 的流程是构造期一次性预处理：

```
PackingInstanceSource.__init__
  └─ fs_local_rank == 0 的进程：
       对每个 source group（连续 source_group_size 个源）：
         读出全部文档 → InstancePacker(OBFD) 装箱 → 把
         document_indices / instance_offsets / docs_by_instance 三张表写成 .npy
  └─ barrier()（其余进程等待）
之后 __getitem__(idx)：按 offset 定位三张表，按需 memmap 读取该实例的文档与 token
```

`split` 的切分点按 `max_sequence_length` 对齐，保证改序列长度后两个 split 仍然互斥且覆盖。

#### 4.2.3 源码精读

[src/olmo_core/data/composable/instance_source.py:L25-L34](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/instance_source.py#L25-L34) `Instance` TypedDict：定长样本的数据载体。

[src/olmo_core/data/composable/instance_source.py:L91-L103](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/instance_source.py#L91-L103) 接口核心：`num_tokens = len(self) * sequence_length`；子类只需实现 `__len__` 与 `__getitem__`。

[src/olmo_core/data/composable/concat_and_chunk_instance_source.py:L100-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/concat_and_chunk_instance_source.py#L100-L108) 上面公式的直接翻译。注意 `100` 个 token、`sequence_length=10` 时得 10 个实例；若是 102 个 token，仍然 10 个实例（尾部 2 个 token 被丢弃）。

[src/olmo_core/data/composable/packing_instance_source.py:L97-L117](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/packing_instance_source.py#L97-L117) `PackingInstanceSource` 的类 docstring：使用 OBFD 算法（论文 *Fewer Truncations Improve Language Modeling*），产出实例全部恰好 `sequence_length` 个 token，不足处 padding；默认每个源单独装箱（可并行、Dolma 源文件够大），`source_group_size > 1` 可把连续多个源合在一起装。

[src/olmo_core/data/composable/packing_instance_source.py:L151-L154](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/packing_instance_source.py#L151-L154) 装箱发生在**构造函数里**，且只有 `fs_local_rank == 0` 的进程执行、其余 `barrier()` 等待——这就是 `SourceABC` 里 `fs_local_rank` 的用途。

[src/olmo_core/data/composable/instance_source.py:L179-L212](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/instance_source.py#L179-L212) `split()` 与 `random_split()`：切分点先按 `max_sequence_length` 取整（`seed=None` 时顺序切分）；`random_split` 强制带 seed，交给 `SlicedInstanceSource` 做实例级洗牌后切片（[sliced_instance_source.py:L34-L44](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sliced_instance_source.py#L34-L44)：先用 `build_global_indices` 生成（可洗牌的）全局索引再取 slice）。

[src/olmo_core/data/composable/instance_source.py:L214-L225](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/instance_source.py#L214-L225) `visualize()`：本讲最重要的调试入口，递归打印源树。实现见 [src/olmo_core/data/composable/visualize.py:L9-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/visualize.py#L9-L92)，其中 [L58-L85](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/visualize.py#L58-L85) 有个小优化：**类型、label 都相同的多个叶子子源会折叠成一行 `XXX x 474: 21.4B tokens`**——这就是模块 docstring 里 `NumpyDocumentSource x 474` 的来历。

#### 4.2.4 代码实践

**实践一（动手）**：确认 `ConcatAndChunkInstanceSource` 的切块行为与 `split` 的规模。

```python
from olmo_core.data.composable import ConcatAndChunkInstanceSource, InMemoryTokenSource

work_dir = "/tmp/chunk-play"
tokens = InMemoryTokenSource(list(range(100)), work_dir=work_dir)
source = ConcatAndChunkInstanceSource(tokens, sequence_length=10, work_dir=work_dir)

print(len(source), source.num_tokens)   # 实例数 / token 数
print(source[0]["input_ids"])           # 第一个实例
print(source[9]["input_ids"])           # 最后一个实例
train, held = source.split(0.8)
print(len(train), len(held))            # 切分规模
train.visualize()
```

预期结果：`10 100`、`[0..9]`、`[90..99]`、`8 2`；`visualize` 树为 `SlicedInstanceSource(8 个实例的 token 数) → ConcatAndChunk → InMemory`（对应模块 docstring 中 [L70-L84](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L70-L84) 的示例输出结构）。

**实践二（阅读 + 运行测试）**：阅读 [src/test/data/composable/packing_instance_source_test.py:L20-L70](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/packing_instance_source_test.py#L20-L70)。该测试用 `np.memmap` 写出两个小 `.npy`（用 EOS=0 分隔文档），装箱到 `sequence_length=8`，然后逐实例断言。重点看三处：

1. `len(instances) == 6`（L52）：27+14=41 个 token 装箱后不是简单的 `41//8`，因为装箱按文档组合、不足补 pad。
2. `instances[1]["input_ids"] == [1,2,3,4,5,0,-1,-1]` 且 `label_mask` 后两位为 `False`（L57-L58）：pad 位置（`pad_token_id=-1`）不计损失——把上一讲的 `label_mask` 语义落到实例层面。
3. `long_doc_strategy` 参数化（L20）：`truncate` 丢弃超长文档的多余 token；`fragment` 把长文档拆片、不丢 token（枚举定义见 [src/olmo_core/data/types.py:L10-L24](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/types.py#L10-L24)）。

运行方式：`pytest -v src/test/data/composable/packing_instance_source_test.py`（纯 CPU，无需 GPU 与真实数据）。

#### 4.2.5 小练习与答案

**练习 1**：101 个 token、`sequence_length=10`，`ConcatAndChunkInstanceSource` 产出几个实例？第 101 个 token 去哪了？
**答案**：10 个实例；按公式 `\lfloor 101/10 \rfloor × 1 = 10`，最后一个 token 被丢弃（「只退不补」）。

**练习 2**：`InstanceSource.split(0.8)` 的切分点为什么要用 `max_sequence_length` 而不是 `sequence_length` 参与对齐计算（[instance_source.py:L192-L195](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/instance_source.py#L192-L195)）？
**答案**：库支持训练中途把 `sequence_length` 调大（`max_sequence_length` 固定）。切分点按 `max_sequence_length` 对齐后，无论当前 `sequence_length` 是多少，两个 split 的**token 边界**保持一致，数据顺序与集合都不变，断点续训才能对上。

**练习 3**：什么时候应该选 `Packing` 而不是 `ConcatAndChunk`？
**答案**：当任务对「文档完整性」敏感时（如指令数据、代码文件、需要文档内注意力的场景），用 `Packing` 保证整篇文档待在同一个实例里、不中途截断；代价是需要 `DocumentSource`、构造期装箱开销和少量 padding token。纯预训练语料通常 `ConcatAndChunk` 就够（这也是 OLMo 预训练主配方沿用 FSL 风格的原因）。

### 4.3 MixingTokenSource：按比例混合的采样数学

#### 4.3.1 概念说明

真实训练配方几乎都是「多种数据按比例混合」：75% 网页 + 25% 代码。`MixingTokenSource`（以及对应的 `MixingDocumentSource`、`MixingInstanceSource`）就是比例混合组合子：给一组 `(source, ratio)` 规格，构造出一个按目标比例采样的复合源。

它解决的核心矛盾是：**目标比例和各源的自然体量往往不匹配**。想要 3:1，但 A 源只有 1000 个 token、B 源也是 1000 个——严格 3:1 需要 1500 个 A token，多出来的 500 个从哪来？要么重复 A（受 `max_repetition_factor` 上限约束），要么整体缩小 B 的配额。这个「重复多少、缩小多少」的决策就是 `calculate_sample_sizes` 的职责。

`MixingTokenSource` 的实现策略是：把每个子源包成一个 `SamplingTokenSource`（采连续块、可控重复），再首尾拼接。所以它的源树永远是「Mix 下挂若干 Sampling，Sampling 下挂原源」。

#### 4.3.2 核心流程

设子源可用量 \( n_i \)、目标比例 \( \rho_i \)（不要求和为 1）、混合目标总量 \( T \)（未指定时 \( T = \sum_i n_i \)）。`calculate_sample_sizes` 的计算步骤：

\[ r_i = \frac{\rho_i}{\sum_j \rho_j} \qquad \text{（归一化，和不为 1 时发 UserWarning）} \]

\[ s_i^{*} = T \cdot r_i \qquad \text{（理想采样量）} \]

\[ \text{need}_i = \max\left(\frac{s_i^{*}}{n_i},\ 1\right) \qquad \text{rep}_i = \min(\text{max\_rep}_i,\ \text{need}_i) \qquad n_i' = n_i \cdot \text{rep}_i \]

\[ \alpha = \min\left(1,\ \min_i \frac{n_i'}{s_i^{*}}\right) \qquad \text{（整体收缩因子）} \]

\[ s_i = \lfloor \alpha \cdot s_i^{*} \rfloor \]

直觉：先看每个源「欠多少」（需要重复几倍），重复额度用不完就把**所有**源按同一因子收缩，从而保持实际比例贴合目标比例（代码里有断言 `actual_ratios ≈ ratios`）。给了 `num_tokens`（strict 模式）却凑不够时直接抛 `OLMoConfigurationError`，错误信息会指出该放开哪个源的重复上限。

**手算例子 A（等大源 3:1，不允许重复）**：\( n = (1000, 1000) \)，\( \rho = (0.75, 0.25) \)，\( T = 2000 \)。得 \( s^* = (1500, 500) \)，\( \text{need} = (1.5, 1.0) \)，\( \text{rep} = (1.0, 1.0) \)（A 的重复需求被上限 1.0 压回），\( \alpha = \min(1, 1000/1500) = 2/3 \)，最终 \( s = (1000, 333) \)——总量缩水到 1333，比例 1000:333 ≈ 3.0003:1。

**手算例子 B（给 A 放开重复上限 2.0）**：\( \text{rep}_A = \min(2.0, 1.5) = 1.5 \)，\( n_A' = 1500 \)，\( \alpha = 1 \)，最终 \( s = (1500, 500) \)——恰好 2000 token，A 被重复了一半。

#### 4.3.3 源码精读

[src/olmo_core/data/composable/mixing_token_source.py:L86-L113](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_token_source.py#L86-L113) `MixingTokenSourceSpec`：一个 `(source, ratio, max_repetition_factor, label)` 规格；ratio 必须为正、重复因子至少 1.0（1.0 = 不允许重复，2.0 = 每个 token 最多见两次）。

[src/olmo_core/data/composable/mixing_token_source.py:L156-L207](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_token_source.py#L156-L207) 构造流程：[L171-L180](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_token_source.py#L171-L180) 调 `calculate_sample_sizes` 得到每源采样量；[L184-L195](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/mixing_token_source.py#L184-L195) 把每个子源包成 `SamplingTokenSource(max_tokens=s_i, seed=seed+i)`；最后拼成 `ConcatenatedTokenSource`。因此 **mix 的 token 布局是「A 的采样块在前、B 的在后」的串行结构**——这个事实是综合实践中「坑」的根源。

[src/olmo_core/data/composable/utils.py:L122-L225](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L122-L225) `calculate_sample_sizes` 全文：[L152-L170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L152-L170) 归一化并发警告（模块 docstring 里能看到警告的样子，[__init__.py:L259-L266](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L259-L266)）；[L172-L192](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L172-L192) 上面公式的实现；[L205-L223](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L205-L223) strict 模式凑不够目标量时的报错（提示「降目标量或提高某源的重复因子」）。

[src/olmo_core/data/composable/sampling_token_source.py:L101-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_token_source.py#L101-L139) `SamplingTokenSource` 的采样实现：先「展开」子级 mix/sampling 源以保持分布，再按份额整源重复（`n_repetitions`）+ 剩余部分取连续块（`seed=None` 时从头取，有 seed 时随机起点，[L126-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/sampling_token_source.py#L126-L139)）。注意 docstring 的忠告：token 级采样「快而脏」，要保分布优先用文档级（`SamplingDocumentSource`）。

[src/test/data/composable/mixing_token_source_test.py:L24-L41](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/mixing_token_source_test.py#L24-L41) 官方测试正好是「例子 B」的 8/16 token 迷你版：A(8) 上限 2.0、B(16)，各 0.5 → 采样 (12, 12)，断言 A 的输出是 `[0..8) + [0..4)`（整源 + 前 4 个重复）——可以直接用来验证你的手算。

#### 4.3.4 代码实践

**实践目标**：先手算、再运行，验证你对 `calculate_sample_sizes` 的理解。

操作步骤：

1. 手算例子 A 与例子 B（见 4.3.2），把预测的 \( s \) 写下来。
2. 运行下面的示例代码（CPU 即可）：

```python
from olmo_core.data.composable import InMemoryTokenSource, MixingTokenSource

work_dir = "/tmp/mix-play"
a = InMemoryTokenSource(list(range(1000)), work_dir=work_dir, label="A")
b = InMemoryTokenSource(list(range(1000, 2000)), work_dir=work_dir, label="B")

# 例子 A：不允许重复
mix_a = MixingTokenSource(
    MixingTokenSource.Spec(source=a, ratio=0.75, label="A"),
    MixingTokenSource.Spec(source=b, ratio=0.25, label="B"),
    work_dir=work_dir, seed=None,
)
print(len(mix_a))                              # 预测：1333
for s in mix_a.sampled_sources:                # 每个子源实际被采了多少
    print(s.label, len(s), len(s) / len(mix_a))

# 例子 B：A 允许重复至多 2 倍
mix_b = MixingTokenSource(
    MixingTokenSource.Spec(source=a, ratio=0.75, label="A", max_repetition_factor=2.0),
    MixingTokenSource.Spec(source=b, ratio=0.25, label="B"),
    work_dir=work_dir, seed=None,
)
print(len(mix_b))                              # 预测：2000
mix_b.visualize()
```

需要观察的现象与预期结果：`len(mix_a) == 1333`（A=1000、B=333，比例 ≈ 0.7502/0.2498）；`len(mix_b) == 2000`（A=1500、B=500，恰好 3:1）。`sampled_sources` 两个 `SamplingTokenSource` 的长度即各源实际采样量。随后再跑 `pytest -v src/test/data/composable/mixing_token_source_test.py` 对照官方断言。（预期值由公式推出，具体数值待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：ratio 写成 `3.0` 和 `1.0`（和为 4）会发生什么？
**答案**：不报错。`calculate_sample_sizes` 先归一化成 0.75/0.25 再计算，同时发出 `UserWarning: Target mixing ratios don't sum to 1...`（[utils.py:L152-L170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L152-L170)）。所以「3:1」既可以直接写 0.75/0.25，也可以写 3/1（容忍一个警告）。

**练习 2**：`max_repetition_factor=1.0` 时，若某源体量不足以支撑目标比例，mix 的总量与比例分别怎么变？
**答案**：不能重复（rep=1），只能以「瓶颈源」为准整体收缩：\( \alpha < 1 \)，所有源同步缩量，实际比例仍贴合目标（这就是例子 A：总量 2000→1333，比例仍 ≈3:1）。

**练习 3**：`MixingTokenSource` 构造好后，子源的 token 在 mix 里是怎样排布的？这对后续按顺序 `split` 有什么影响？
**答案**：各源的采样块**按 spec 顺序串行拼接**（4.3.3 源码）。若先 mix 再顺序 `split(0.8)`，前 80% 可能几乎全是排在前面的源（见综合实践的「坑」）。

### 4.4 ComposableDataLoader：接上 Trainer 的最后一公里

#### 4.4.1 概念说明

`InstanceSource` 只是「样本数组」，`Trainer` 需要的是：epoch 开始时洗牌、按数据并行 rank 切分 batch、能 `state_dict()` 断点续训、能出 mock batch 预热。这些就是 `ComposableDataLoader` 的职责——它是 u1-l4 五块积木中 `data_loader` 那一块的 composable 实现。

两个与前面单元的连接点：

- 它的 Config 以 `@DataLoaderConfig.register("composable")` 注册（u2-l2 的 Registrable 机制），所以在 `ExperimentConfig` 的多态 `data_loader` 字段里，YAML 中写 `type: composable` 即可选中它。
- 与 `NumpyDataLoaderConfig` 不同，`ComposableDataLoaderConfig.build()` 把实例源作为**参数**传入而不是 Config 字段——因为源树可能是运行时由 glob 展开构建的巨大对象图，Config 只携带 tokenizer、batch size、线程/进程数等「小」配置。

#### 4.4.2 核心流程

一个 epoch 的完整生命周期：

```
Trainer（每个 epoch 开始）
  └─ data_loader.reshuffle(epoch)                      # 继承自基类的契约
       └─ build_and_save_global_indices()
            ├─ fs_local_rank==0：若索引文件不存在则 _build_global_indices()
            │   并以 uint32 memmap 落盘（文件名编码 seed/epoch/策略/规模）
            └─ barrier()

迭代 data_loader（__iter__ → _iter_batches）
  └─ torch.utils.data.DataLoader(num_workers 个进程)
       └─ _IterableDataLoaderWrapper.__iter__
            ├─ get_local_indices()：全局索引 → 裁尾对齐批 → 跳过已处理批
            │     → 按进程 worker 轮流取批 → 按 dp_rank 切出本 rank 份额
            ├─（可选）num_threads 个预取线程 + roundrobin 合并
            ├─ get_instance(idx)：定位到源 → 源内取实例 → 附加 metadata/doc_lens/过滤标记
            └─ collator：每 rank_batch_size 个实例堆叠成一个 batch（pad + 词表校验）

epoch 结束 → data_loader.reset()（batches_processed 清零）
```

三种洗牌策略 `ShuffleStrategy`（[data_loader.py:L52-L65](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L52-L65)）：

| 策略 | 语义 | 典型用途 |
| --- | --- | --- |
| `inter_source`（默认） | 所有源混成一个池全局洗牌 | 常规训练 |
| `intra_source` | 每个源**内部**洗牌，源之间保持传入顺序 | 课程学习（先学 mix1 再学 mix2） |
| `interleaved_source` | 源内洗牌后逐源轮流交错取实例 | 源间严格交替 |

`sources_per_epoch`（每个 epoch 消耗几个源，按顺序分组轮转）是课程学习的另一半：把两个阶段的 mix 按序传入，配 `intra_source` 或 `sources_per_epoch=1`，即可实现「前一半训练用配方一、后一半用配方二」——模块 docstring 的 Curriculum learning 一节（[__init__.py:L300-L447](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L300-L447)）有完整可抄的例子。

#### 4.4.3 源码精读

[src/olmo_core/data/composable/data_loader.py:L68-L88](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L68-L88) `ComposableDataLoaderConfig`：注册名 `composable`；字段覆盖 tokenizer、`global_batch_size`（token 计）、seed、shuffle 策略、`sources_per_epoch`、worker/线程数、`generate_doc_lengths`（生成文档长度供文档内 mask 用）、`instance_filter_config`（周期性重复 n-gram 过滤，[L43-L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L43-L49)）等。

[src/olmo_core/data/composable/data_loader.py:L99-L163](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L99-L163) `build(*sources, mesh=None, dp_process_group=None, ...)`：实例源作为参数传入；可从 `DeviceMesh` 取数据并行进程组（连回 u7-l1 的 DP 主题）；默认构造 `DataCollator(pad_token_id, vocab_size=tokenizer.padded_vocab_size())`。

[src/olmo_core/data/composable/data_loader.py:L220-L299](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L220-L299) 构造期校验：所有源 `sequence_length`/`max_sequence_length` 必须一致；`global_batch_size` 必须是 `sequence_length` 的整数倍；`sources_per_epoch` 必须能整除源数。[L295-L299](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L295-L299) 构造结束时 0 号进程自动 `visualize()` 每个源——训练日志里那棵数据源树就是这里打印的。

[src/olmo_core/data/composable/data_loader.py:L584-L627](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L584-L627) `_build_global_indices` 按策略生成排列：`inter_source` 把当 epoch 全部源的实例当成一个池，用 `seed + epoch` 洗牌；`intra_source` 逐源洗牌再加偏移拼接；`interleaved_source` 按最小源规模交错。底层 [utils.py:L228-L266](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/utils.py#L228-L266) 的 `build_global_indices` 以 `max_sequence_length // sequence_length` 为块整块洗牌——这正是「中途改序列长度、数据顺序不变」的机关。

[src/olmo_core/data/composable/data_loader.py:L562-L582](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L562-L582) 索引落盘：只有 `fs_local_rank==0` 写、写完 `barrier()`；文件名由 [L513-L525](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L513-L525) 的字段（seed/epoch/策略/规模/seq_len）拼出，参数相同则复用旧文件。忘记 `reshuffle()` 就迭代会在 [L527-L532](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L527-L532) 报 "did you forget to call 'reshuffle()'?"。

[src/olmo_core/data/composable/data_loader.py:L534-L560](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L534-L560) `get_local_indices` 是切批核心四步：裁尾使总实例数被每批实例数整除 → reshape 成 `(全局批数, 每批实例数)` → 按 `batches_processed` 跳过已处理批 → 先按 worker 轮流取整批、再 `indices[:, dp_rank :: dp_world_size]` 切出本 rank 份额。**不重不漏**的关键就在这两层按批/按列的切分。

[src/olmo_core/data/composable/data_loader.py:L469-L511](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L469-L511) `get_instance`：线性扫描定位全局索引落在哪个源，取出实例后可附加 `metadata={"source": label}`（源 label 会跟着 batch 走，方便按数据来源分析 loss）、`doc_lens`、`instance_mask`（重复 n-gram 过滤）。

[src/olmo_core/data/composable/data_loader.py:L346-L418](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L346-L418) 断点续训：`state_dict` 存指纹、已处理 token 数、epoch 等；`load_state_dict` 校验指纹一致（[L367-L385](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L367-L385)），特别地允许「**追加**新源」——只要旧源指纹是前缀且旧源还没被迭代过；换 `max_sequence_length`、shuffle 策略则直接拒绝。恢复进度按 token 数换算：`batches_processed = tokens_processed // global_batch_size`（[L402-L404](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L402-L404)），所以中途改 batch size 也能对上进度。

[src/olmo_core/data/composable/data_loader.py:L630-L680](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L630-L680) `_IterableDataLoaderWrapper`：把「取实例」包装成 torch 的 IterableDataset 以获得多进程加载；单进程时默认开 4 个预取线程，各线程取交错的实例流再 roundrobin 合并，保证流水线跑在训练前面；最后 `iter_batched` 按 `rank_batch_size` 组批交给 collator。

#### 4.4.4 代码实践

**实践目标**：完整走一遍「构造 → reshuffle → 迭代一个 epoch → reset」，对照官方测试确认行为。

操作步骤（示例代码，改写自 [src/test/data/composable/data_loader_test.py:L15-L53](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/data_loader_test.py#L15-L53)）：

```python
from olmo_core.data.collator import DataCollator
from olmo_core.data.composable import (
    ComposableDataLoader, ConcatAndChunkInstanceSource, InMemoryTokenSource,
)
from olmo_core.data.tokenizer import TokenizerConfig

work_dir = "/tmp/loader-play"
tokens1 = InMemoryTokenSource(list(range(64)), work_dir=work_dir)
tokens2 = InMemoryTokenSource(list(range(64, 128)), work_dir=work_dir)
instances = ConcatAndChunkInstanceSource(tokens1, tokens2, sequence_length=8, work_dir=work_dir)

tokenizer = TokenizerConfig(vocab_size=32000, eos_token_id=0, pad_token_id=-1)
dl = ComposableDataLoader(
    instances,
    collator=DataCollator(pad_token_id=tokenizer.pad_token_id),
    tokenizer=tokenizer,
    work_dir=work_dir,
    global_batch_size=16,   # 16 token / 8 = 每批 2 个实例
    num_workers=0,
    num_threads=0,
)
print(dl.total_instances, dl.total_batches)   # 16, 8

dl.reshuffle()                 # 必须先落盘全局索引
seen, n_batches = set(), 0
for batch in dl:
    assert batch["input_ids"].shape == (2, 8)   # rank_batch_size=16 → 2x8
    seen.update(batch["input_ids"].flatten().tolist())
    n_batches += 1
print(n_batches, len(seen))    # 期望 8 批、128 个 token 各出现一次
print(len(list(dl)))           # 不 reset 再迭代 → 0 批
dl.reset()
```

需要观察的现象与预期结果：构造时终端先打印源树（rank 0 的 `visualize`）；`total_batches == 8`；默认 `inter_source` 洗牌下每个 token 恰好出现一次（`seen == set(range(128))`，与官方断言 [L48](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/data_loader_test.py#L48) 一致）；epoch 内重复迭代不再产出。可再跑 `pytest -v "src/test/data/composable/data_loader_test.py"`，其中 [L56-L110](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/data_loader_test.py#L56-L110) 演示了「迭代一半 → 存 state_dict → 重建 loader → 无缝续完 epoch」的完整断言。（以上预期与官方测试对齐，具体顺序受 seed 影响待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：想实现「前 500 步用数据配方一、之后用配方二」，`sources_per_epoch` 与 `ShuffleStrategy.intra_source` 分别扮演什么角色？
**答案**：把 mix1、mix2 按顺序传给 loader。`sources_per_epoch=1` 让每个 epoch 只消费一个源（第 1 个 epoch 用 mix1、第 2 个用 mix2，[L305-L310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L305-L310) 的分组轮转）；`intra_source` 则是在同一个 epoch 内保持源间顺序、只做源内洗牌。两者都是课程学习的工具，前者以 epoch 为切换粒度，后者保证阶段内顺序稳定（模块 docstring [L430-L447](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L430-L447) 同时展示了两种写法）。

**练习 2**：全局索引为什么要落盘成文件，而不是放在内存里？
**答案**：一是规模——数十亿 token 的配方对应数百万实例，`uint32` 数组虽不大但没必要重复生成；二是**确定性复用**——文件名编码了 seed/epoch/策略，同参数重启直接复用，保证断点续训与新进程的数据顺序完全一致；三是多进程共享——数据加载 worker 各自 memmap 同一文件，而不是每进程复制一份。

**练习 3**：`load_state_dict` 为什么「允许追加源，但要求旧源还没被迭代过」？
**答案**：追加源只影响后续 epoch 的分组；若旧源已被消费过，恢复后的 epoch 编号与源分组的对应关系会错位（旧进度对应的是旧分组方案），数据顺序无法保证，所以直接拒绝（[L367-L381](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/data_loader.py#L367-L381)）。

## 5. 综合实践

**任务**：搭建「两个数据源、按 3:1 混合、切出前 80% 做训练」的完整管线，用 `visualize()` 打印源树并**验证采样比例**。过程中会遇到一个真实的坑并修复它——这正是本讲想教给你的核心直觉。

**技巧说明**：两个源的 token id 取值区间刻意不重叠（A 用 0–2999，B 用 3000–3999），这样事后按取值范围就能判断每个 token 来自哪个源（官方测试也是这么做的，[data_loader_test.py:L17-L18](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/data/composable/data_loader_test.py#L17-L18)）。

完整示例代码（CPU 可跑）：

```python
from olmo_core.data.collator import DataCollator
from olmo_core.data.composable import (
    ComposableDataLoader, ConcatAndChunkInstanceSource,
    InMemoryTokenSource, MixingTokenSource, set_composable_seed,
)
from olmo_core.data.tokenizer import TokenizerConfig

WORK_DIR = "/tmp/composable-demo"
SEQ_LEN = 10
set_composable_seed(42)   # 设全局种子：所有未显式给 seed 的组合子从此可复现

src_a = InMemoryTokenSource(list(range(0, 3000)),    work_dir=WORK_DIR, label="A")
src_b = InMemoryTokenSource(list(range(3000, 4000)), work_dir=WORK_DIR, label="B")

# ---------- 版本 1（有坑）：先混合，再顺序切 80% ----------
mix = MixingTokenSource(
    MixingTokenSource.Spec(source=src_a, ratio=0.75, label="A"),
    MixingTokenSource.Spec(source=src_b, ratio=0.25, label="B"),
    work_dir=WORK_DIR, seed=None,     # seed=None：每源取开头连续块，行为最可预测
)
naive = ConcatAndChunkInstanceSource(mix, sequence_length=SEQ_LEN, work_dir=WORK_DIR)
naive_train, naive_held = naive.split(0.8)          # split 默认 seed=None → 顺序切
naive_train.visualize()

def count_provenance(source):
    n_a = n_b = 0
    for inst in source:              # InstanceSource 本身可迭代
        for t in inst["input_ids"]:
            n_a, n_b = (n_a + 1, n_b) if t < 3000 else (n_a, n_b + 1)
    return n_a, n_b

print("版本 1 训练源:", count_provenance(naive_train))

# ---------- 版本 2（修复）：先切 80%，再混合 ----------
a_train, _ = src_a.split(0.8)        # 2400 tokens
b_train, _ = src_b.split(0.8)        # 800 tokens
train_mix = MixingTokenSource(
    MixingTokenSource.Spec(source=a_train, ratio=0.75, label="A"),
    MixingTokenSource.Spec(source=b_train, ratio=0.25, label="B"),
    work_dir=WORK_DIR, seed=None,
)
train_source = ConcatAndChunkInstanceSource(
    train_mix, sequence_length=SEQ_LEN, work_dir=WORK_DIR
)
train_source.visualize()
print("版本 2 训练源:", count_provenance(train_source), "实例数:", len(train_source))

# ---------- 挂上 DataLoader 跑一个 epoch ----------
tokenizer = TokenizerConfig(vocab_size=32000, eos_token_id=0, pad_token_id=-1)
dl = ComposableDataLoader(
    train_source,
    collator=DataCollator(pad_token_id=tokenizer.pad_token_id),
    tokenizer=tokenizer,
    work_dir=WORK_DIR,
    global_batch_size=40,            # 每批 40/10 = 4 个实例
    num_workers=0,
    num_threads=0,
)
print("total_batches:", dl.total_batches)

dl.reshuffle()
n_a = n_b = n_batches = 0
for batch in dl:
    assert batch["input_ids"].shape == (4, SEQ_LEN)
    for t in batch["input_ids"].flatten().tolist():
        n_a, n_b = (n_a + 1, n_b) if t < 3000 else (n_a, n_b + 1)
    n_batches += 1
print(f"epoch 完成: batches={n_batches}, A={n_a}, B={n_b}")
dl.reset()
```

**为什么版本 1 是坑**：mix 内部是「A 的 3000 个 token 在前、B 的 1000 个在后」的串行布局（4.3.3），`split(0.8)` 顺序取前 3200 个 token，结果是 A 全量 3000 + B 只有 200——训练源实际比例 93.75% : 6.25%，和 3:1 相去甚远；而且被「切」的只有 B。预期打印：`版本 1 训练源: (3000, 200)`。

**版本 2 的预期结果**：`版本 2 训练源: (2400, 800)`，实例数 320——精确 3:1（2400/800）。这也是模块 docstring 课程学习示例（[__init__.py:L313-L318](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L313-L318)）先 `random_split` 再混合的原因：**切分要发生在混合之前（或用 `random_split` 做实例级随机切分），否则比例被串行布局破坏**。

版本 2 的源树预期如下（示意；指纹与你的运行不同，`SlicedTokenSource` 不带 label）：

```
ConcatAndChunkInstanceSource(xxxxxxx): 3.2K tokens
└─ MixingTokenSource(xxxxxxx): 3.2K tokens
   ├─ SamplingTokenSource(xxxxxxx): 2.4K tokens [A]
   │  └─ SlicedTokenSource(xxxxxxx): 2.4K tokens
   │     └─ InMemoryTokenSource(xxxxxxx): 3.0K tokens [A]
   └─ SamplingTokenSource(xxxxxxx): 800 tokens [B]
      └─ SlicedTokenSource(xxxxxxx): 800 tokens
         └─ InMemoryTokenSource(xxxxxxx): 1.0K tokens [B]
```

DataLoader 部分预期：`total_batches: 80`（320 实例 ÷ 每批 4 实例），一个 epoch 结束打印 `batches=80, A=2400, B=800`——即使默认 `inter_source` 全局洗牌，每个 token 在 epoch 内仍恰好出现一次，所以来源计数不变。

（以上预期值均由源码逻辑与 4.3.2 的公式推出，具体打印顺序受 seed 影响与指纹值，待本地验证。）

**延伸挑战**（可选）：把 `split(0.8)` 换成 `random_split(0.8)` 重建版本 1，观察训练源比例是否回到约 3:1（随机实例切分下比例是期望意义下的 3:1，带超几何方差）；再用 `mix.resize(3.0)` 模拟「3 个 epoch 的数据量」，`visualize()` 应看到 mix 外面多包一层 `SamplingInstanceSource`（对照模块 docstring 的 up-sampling 示例 [__init__.py:L267-L298](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/composable/__init__.py#L267-L298)）。

## 6. 本讲小结

- composable API 用三层抽象取代「一类一变体」：`TokenSource`（token 数组）→ `InstanceSource`（定长样本数组）→ `ComposableDataLoader`（batch 供给），每层都靠 `+`/`*`/`sample`/`split`/`Mixing` 组合子自由拼装，`SourceABC` 的 `fingerprint`/`children` 契约贯穿全树。
- `TokenSource` 只需实现 `num_tokens`/`get_token_range`/`fingerprint`/`children`；`DocumentSource` 额外提供文档边界，是装箱的前提；`TokenSourceConfig.build()` 返回列表以容纳 glob 展开的多个文件源。
- `InstanceSource` 的两大生成器：`ConcatAndChunk`（无视边界切块、零 padding）与 `Packing`（OBFD 整文档装箱、pad 位置 `label_mask=False`）；`visualize()` 是调试源树的第一工具，同型叶子会折叠成 `x N`。
- 混合比例与源体量的矛盾由 `calculate_sample_sizes` 解决：归一化 → 理想量 → 重复需求（受 `max_repetition_factor` 约束）→ 整体收缩因子 α → 整数化；**先混合再顺序切分会破坏比例，切分要在混合之前**。
- `ComposableDataLoader` 以 `type: composable` 注册进 DataLoader 体系；`reshuffle()` 生成并落盘 uint32 全局索引（seed/epoch/策略编码进文件名），`get_local_indices()` 通过「裁尾 → 按批 reshape → 跳已处理 → worker 轮流 → dp_rank 切列」实现不重不漏；`state_dict` 靠指纹校验、按 token 数换算进度，支持改 batch size 与追加源。
- `ShuffleStrategy`（inter/intra/interleaved）与 `sources_per_epoch` 组合即可声明式地表达课程学习。

## 7. 下一步学习建议

- **下一讲（u4-l3）**：数据混合的「配方文件」层——`source_mixture.py` 与 `data/mixes/` 里的官方预置配方（dolma17 等）。本讲的 `MixingInstanceSource` + ratio 是机制，mixes 是把机制固化为可复用配置的工程层。
- **回看连接点**：u6-l1 训练循环会在每个 epoch 调 `reshuffle()`、在断点处调 `load_state_dict()`；u7-l1 数据并行里的 dp 进程组正是 `ComposableDataLoaderConfig.build(mesh=...)` 消费的维度。
- **源码延伸阅读**：`mixing_instance_source.py` / `sampling_instance_source.py`（实例级组合子，混合数学与 token 层共用 `calculate_sample_sizes`）；`numpy_document_source.py`（如何把上一讲的 `.npy` 内存映射接入 composable 世界，含 `from_source_groups` 批量构建）。
- **动手方向**：把综合实践的管线换成 `PackingInstanceSource`（需要给源加 EOS 并改用 `InMemoryDocumentSource` 或 `.npy` 文件），对比同一份语料两种切块方式的实例数、padding 量与 `label_mask` 分布。
