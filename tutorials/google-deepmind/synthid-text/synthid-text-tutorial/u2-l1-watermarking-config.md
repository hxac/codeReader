# 水印配置 WatermarkingConfig 与默认配置

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `WatermarkingConfig` 每个字段的含义，以及它**如何被真正使用**（哪些字段进了构造函数、哪些只是文档遗留）。
- 解释为什么 `len(keys)` 决定水印的「层数 / 深度（depth）」，以及深度对 g 值形状的影响。
- 看懂仓库内置的静态 `DEFAULT_WATERMARKING_CONFIG`，理解它为什么是「静态」的、为什么被反复强调「不适合生产」。
- 能够回答实践题：`ngram_len=5` 为什么对应论文里的 `H=4`；把 `keys` 改成只有 3 个元素会发生什么。

本讲承接 [u1-l4 端到端流程总览](./u1-l4-end-to-end-pipeline.md)：上一讲你已经知道整条链路是「生成时埋水印、检测时重算 g 值」，本讲就来拆解这条链路最开头的那个「配置」到底装了什么。

## 2. 前置知识

在阅读本讲前，你只需要具备以下直觉（都在单元一建立过）：

- **水印配置（watermarking config）** 是一组「出厂参数」，告诉 SynthID Text 这一次要施加的是「哪一种」水印。
- **g 值** 是贯穿施加侧（PyTorch）与检测侧（JAX）的二进制指纹，形状类似 `[batch, 序列长度, depth]`。本讲会告诉你这个 `depth` 从哪儿来。
- **ngram** 是连续的若干个 token。SynthID 用「前若干个 token 作为上下文」来决定如何给下一个候选 token 打水印。
- **keys（密钥）** 是一组整数，是水印的「身份」。同一组 keys 生成的水印，只能用**针对这组 keys 训练**的检测器来识别。

> 关键原则回顾：**当 README 文档与源码冲突时，以源码为准。** 本讲会明确指出 `WatermarkingConfig` 在文档与源码之间的几处不一致。

## 3. 本讲源码地图

本讲涉及的文件很少，但每一个都要看清：

| 文件 | 在本讲中的作用 |
| --- | --- |
| [README.md](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md) | 给出 `WatermarkingConfig` 的 `TypedDict` 定义，并说明「配置 → 水印」的关系。 |
| [src/synthid_text/synthid_mixin.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py) | 定义静态的 `DEFAULT_WATERMARKING_CONFIG`，并在 Mixin 里把它注入到 logits processor。 |
| [src/synthid_text/logits_processing.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py) | `SynthIDLogitsProcessor.__init__` 才是「配置真正被消费」的地方——它告诉我们哪些字段是真正生效的。 |

> 提示：`WatermarkingConfig` 这个 `TypedDict` **并没有**作为一个类定义在 `src/` 里，它只在 README 的示例代码中出现（见 4.1.3）。仓库真正使用的「配置」是 `synthid_mixin.py` 里的那个字典常量。

## 4. 核心概念与源码讲解

### 4.1 WatermarkingConfig 字段

#### 4.1.1 概念说明

`WatermarkingConfig` 是一个描述「这次水印长什么样」的参数集合。你可以把它理解成一把锁的「图纸」：

- 它定义了水印的**结构**（用多长的上下文、分多少层、记录多少历史）。
- 它**不**包含运行时的采样参数（如 `temperature`、`top_k`）——那些在生成时才传进来。
- 一旦图纸确定，同一份输入文本 + 同一个语言模型，就会产生**同一种**水印信号。

README 用一个 `TypedDict` 描述了它的「完整版」字段结构，但要注意：**仓库实际跑起来时用不到全部字段**（详见 4.1.3 的对照）。

#### 4.1.2 核心流程

一个配置对象的生命周期是：

```text
DEFAULT_WATERMARKING_CONFIG（静态常量）
        │  作为 **kwargs 解包
        ▼
SynthIDLogitsProcessor(**config, **extra_params)   # extra_params = temperature, top_k
        │
        ▼
self.ngram_len / self.keys / self.context_history_size / self.device   # 真正被存下来使用的字段
        │
        ▼  逐 token 生成时
watermarked_call(...)  →  计算 g 值（深度 = len(keys)）→  偏置 scores
```

要点：配置先被「解包」进构造函数，构造函数再挑出它真正需要的几个字段存为实例属性，其余字段（如果有）会被忽略。

#### 4.1.3 源码精读

README 里给出的 `WatermarkingConfig` 是这样的（[README.md:L104-L118](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L104-L118)）：

```python
class WatermarkingConfig(TypedDict):
    ngram_len: int
    keys: Sequence[int]
    sampling_table_size: int
    sampling_table_seed: int
    context_history_size: int
    device: torch.device
```

README 还点明了配置里最关键的是 `keys`（[README.md:L95-L98](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L95-L98)）：

> `keys`: a sequence of unique integers where `len(keys)` corresponds to the number of layers in the watermarking or detection models.

但是，**真正消费配置的是构造函数**。看 [logits_processing.py:L135-L147](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L147)：

```python
def __init__(
    self,
    *,
    ngram_len: int,
    keys: Sequence[int],
    context_history_size: int,
    temperature: float,
    top_k: int,
    device: torch.device,
    skip_first_ngram_calls: bool = False,
    apply_top_k: bool = True,
    num_leaves: int = 2,
):
```

把「文档字段」和「构造函数参数」做一张对照表，就能看清谁真正生效：

| 字段（README TypedDict） | 构造函数是否接收 | 说明 |
| --- | --- | --- |
| `ngram_len` | ✅ | ngram 长度，决定上下文窗口大小。 |
| `keys` | ✅ | 水印密钥序列，`len(keys)` 即深度 depth。 |
| `context_history_size` | ✅ | 记录「已见上下文」的滑动窗口大小。 |
| `device` | ✅ | 张量所在设备（CPU/GPU）。 |
| `sampling_table_size` | ❌ | **文档里写了，但源码里完全没用到**（见下方提示）。 |
| `sampling_table_seed` | ❌ | **同上，源码中不存在**。 |
| （非配置，运行时传入）`temperature` | ✅ | 采样温度，由 `generation_config` 注入。 |
| （非配置，运行时传入）`top_k` | ✅ | 稀疏 top-k，由 `generation_config` 注入。 |
| （带默认值）`skip_first_ngram_calls` / `apply_top_k` / `num_leaves` | ✅ | 水印行为开关，有默认值，配置里通常不写。 |

> ⚠️ **以源码为准的典型案例**：`sampling_table_size` 与 `sampling_table_seed` 这两个字段在整个 `src/` 目录里都搜不到任何引用（属于历史遗留）。如果你想真正理解配置，请以构造函数签名为准：**配置实际只有 `ngram_len`、`keys`、`context_history_size`、`device` 四个会被用到的字段**，外加运行时补充的 `temperature`、`top_k`。

#### 4.1.4 代码实践

**实践目标**：亲手确认「哪些字段才真正进入 logits processor」。

**操作步骤**（源码阅读型，无需运行模型）：

1. 打开 [logits_processing.py 的 `__init__`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L202)，数一数构造函数把哪些参数存成了 `self.xxx`。
2. 对比 README 的 `WatermarkingConfig`，圈出 README 多出来的两个字段。

**需要观察的现象**：你会发现 `sampling_table_size` / `sampling_table_seed` 从未变成 `self.xxx`，也没有出现在任何方法里。

**预期结果**：构造函数只保留了 `self.ngram_len`、`self.keys`、`self.context_history_size`、`self.device`、`self.temperature`、`self.top_k`（以及 `hash_iv` 等派生量）。这就是「真实生效的配置」。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `sampling_table_size=65536` 加进配置字典再解包给 `SynthIDLogitsProcessor`，会发生什么？

**参考答案**：构造函数用的是关键字参数（`*` 之后均为仅关键字参数），且没有 `**kwargs`。多传一个未声明的关键字参数会直接抛 `TypeError: __init__() got an unexpected keyword argument 'sampling_table_size'`。这反向证明了该字段在当前实现里是无效的。

**练习 2**：`temperature` 和 `top_k` 为什么不写进 `WatermarkingConfig`，而要在生成时单独传？

**参考答案**：它们是「采样策略」参数，会随每次调用的需求变化（比如想换一种温度生成），而配置描述的是「水印身份」。把它们分离，可以让同一套水印配置在不同采样条件下复用。

---

### 4.2 DEFAULT_WATERMARKING_CONFIG

#### 4.2.1 概念说明

`DEFAULT_WATERMARKING_CONFIG` 是仓库内置的「开箱即用」配置常量。它的特点是：

- **静态（static）**：值在模块加载时就写死，整个进程里所有水印调用都用同一份。
- **不可变（immutabledict）**：用 `immutabledict` 包裹，防止运行时被意外修改。
- **只含必要字段**：只放构造函数真正需要的 4 个字段，不含文档里那两个遗留字段。

它的存在让 Notebook 示例可以「零配置」跑起来，但也正因为它是写死的、且**所有人都一样**，所以不能用于生产（详见 4.3）。

#### 4.2.2 核心流程

默认配置被使用的路径只有一条——Mixin 在构造 logits processor 时把它解包：

```text
SynthIDSparseTopKMixin._construct_warper_list(extra_params)
        │
        │  SynthIDLogitsProcessor(**DEFAULT_WATERMARKING_CONFIG, **extra_params)
        │       ↑ 静态配置             ↑ temperature / top_k
        ▼
一个带水印能力的 logits processor
```

也就是说：**无论你用的是 `SynthIDGPT2LMHeadModel` 还是 `SynthIDGemmaForCausalLM`，只要走这个 Mixin，用的都是同一份默认配置。**

#### 4.2.3 源码精读

先看默认配置本体（[synthid_mixin.py:L27-L67](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L27-L67)）：

```python
DEFAULT_WATERMARKING_CONFIG = immutabledict.immutabledict({
    "ngram_len": 5,  # This corresponds to H=4 context window size in the paper.
    "keys": [654, 400, 836, 123, 340, 443, 597, 160, ...],  # 共 30 个整数
    "context_history_size": 1024,
    "device": (torch.device("cuda:0") if torch.cuda.is_available()
               else torch.device("cpu")),
})
```

逐字段说明：

- **`ngram_len: 5`**（[L28](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L28)）：ngram 长度。注意行内注释明确写了 `This corresponds to H=4 context window size in the paper`——下文 4.2.4 会解释为什么是 5 而论文写 4。
- **`keys: [...]`**（[L29-L60](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L29-L60)）：30 个不同的整数。`len(keys) == 30`，所以这套水印的 **depth = 30**。
- **`context_history_size: 1024`**（[L61](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L61)）：去重用的滑动历史长度（本讲先记住它是个 1024 大小的窗口，4.3 与下一讲 u3-l4 会展开）。
- **`device`**（[L62-L66](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L62-L66)）：有 GPU 用 `cuda:0`，否则退回 `cpu`。

再看它如何被解包注入（[synthid_mixin.py:L73-L83](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L73-L83)）：

```python
def _construct_warper_list(self, extra_params):
    warpers = transformers.LogitsProcessorList()
    warpers.append(
        logits_processing.SynthIDLogitsProcessor(
            **DEFAULT_WATERMARKING_CONFIG, **extra_params
        )
    )
    return warpers
```

关键就在 [L80](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L80) 的 `**DEFAULT_WATERMARKING_CONFIG, **extra_params`——把静态配置和运行时的 `temperature` / `top_k` 合并后一次性喂给构造函数。因为默认配置恰好只含构造函数认识的 4 个字段，所以这里不会触发 4.1.5 练习 1 里说的 `TypeError`。

**`len(keys)` 如何变成 depth**：构造函数里 `self.keys = torch.tensor(keys, device=device)`（[logits_processing.py:L161-L162](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L161-L162)），`self.keys` 形状是 `[depth,]`。之后在 `_compute_keys` 里被重塑成 `[1, 1, depth, 1]` 并 vmap（[logits_processing.py:L443-L448](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L443-L448)），最终 g 值的最后一维就是这个 depth。`update_scores` 开头那句 `_, _, depth = g_values.shape`（[logits_processing.py:L39](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L39)）也印证了这一点。

> 顺带一提：`keys` 还会被整体喂给 SHA-256 生成不可预测的哈希初值 `hash_iv`（[logits_processing.py:L164-L174](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L164-L174)）。所以「改 keys」不只是改层数，还会改变整条哈希链的起点，等于换了一种全新水印。

#### 4.2.4 代码实践（本讲主实践）

这是本讲要求完成的核心实践题，分两小问。

**实践目标**：把「配置字段 → 水印行为」的因果彻底想清楚。

**第 1 问：解释 `ngram_len=5` 为什么对应论文里的 `H=4` 上下文窗口。**

操作步骤（源码阅读型）：

1. 看 `SynthIDState` 里 context 的形状（[logits_processing.py:L114-L118](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L114-L118)）：

   ```python
   self.context = torch.zeros((batch_size, ngram_len - 1), ...)
   ```

   context 只存 `ngram_len - 1` 个 token，也就是「前 4 个 token」。
2. 一个 ngram = 这 4 个上下文 token + 1 个候选 token = 共 5 个 token，所以 `ngram_len=5`。
3. 论文里的 `H` 指的是「上下文窗口大小」，也就是「往前看几个 token」，等于 `ngram_len - 1 = 4`。

需要观察的现象：代码里凡是和上下文长度有关的张量，第二维都是 `ngram_len - 1`；而 ngram 本身的长度是 `ngram_len`。

预期结论：

\[
\text{ngram\_len} = H + 1 \quad\Rightarrow\quad \text{ngram\_len}=5 \text{ 时 } H=4
\]

**第 2 问：如果把 `keys` 改成只有 3 个元素，会对水印产生什么影响？**

可以从三个角度回答（建议你对照源码逐条验证）：

1. **深度变小**：`depth` 从 30 变成 3，g 值形状从 `[batch, seq, 30]` 变成 `[batch, seq, 3]`。`compute_g_values` 的返回形状文档就写着 `(batch_size, input_len - (ngram_len - 1), depth)`（[logits_processing.py:L468](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L468)）。
2. **水印信号变弱**：检测器需要在 depth 个二进制位上累积证据。depth 越小，可累积的独立证据越少，检测鲁棒性（抗攻击、抗截断）下降。
3. **变成另一种全新水印**：keys 的字节会经 SHA-256 生成 `hash_iv`（见 4.2.3），keys 一变，哈希链起点就变，整条水印与原来完全不兼容。**原来针对 30-keys 训练的贝叶斯检测器失效，必须用新 keys 重新训练。**

> 说明：本题是「源码阅读 + 推理」型实践，无需真正运行模型即可作答；如果你想本地验证「depth = len(keys)」，可参考下面 4.3.4 的可运行片段。

#### 4.2.5 小练习与答案

**练习 1**：默认配置里 `context_history_size=1024`，这个 1024 在代码里对应哪个张量的哪一维？

**参考答案**：对应 `SynthIDState.context_history` 的第二维（[logits_processing.py:L119-L123](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L119-L123)），形状是 `(batch_size, context_history_size)`，用来记录「最近见过的 1024 个上下文哈希」，用于判断当前上下文是否重复。

**练习 2**：`DEFAULT_WATERMARKING_CONFIG` 为什么用 `immutabledict` 而不是普通 `dict`？

**参考答案**：因为它是全进程共享的静态常量。普通 `dict` 可能在某次调用中被误改（比如有人写了 `config['keys'].append(...)`），从而污染后续所有水印调用；`immutabledict` 在运行期阻止这种修改，保证「同一进程、同一份配置」。

---

### 4.3 静态配置的局限

#### 4.3.1 概念说明

「静态配置」指的是：配置值在代码里写死，所有使用这个库的人、所有的生成调用，都用**完全相同**的 `keys`、`ngram_len` 等。这带来两个本质问题：

- **密钥公开 = 水印可被任何人伪造/规避**：默认 keys 写在开源仓库里，任何人都知道。攻击者可以用同一组 keys 给自己的文本「贴」上水印（伪造），或据此设计规避水印的策略。
- **无法按场景隔离**：生产环境中通常需要「不同业务/不同租户用不同水印」，静态配置做不到。

因此 README 在多处反复强调：**这个 Mixin 用的是静态配置，不适合生产用途；要生产化请用 HuggingFace Transformers 官方的 SynthID Text 实现。**

#### 4.3.2 核心流程

局限是如何「传导」到用户的：

```text
DEFAULT_WATERMARKING_CONFIG（写死、公开、全仓库一致）
        │  被 Mixin 硬编码进 _construct_warper_list
        ▼
所有 SynthIDGPT2LMHeadModel / SynthIDGemmaForCausalLM 实例共用同一组 keys
        ▼
水印身份不可隔离、密钥不可保密  →  不适合生产
```

#### 4.3.3 源码精读

先看 README 里两处明确的警告。定义配置那段（[README.md:L100-L102](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L100-L102)）：

> the [mixin][synthid-mixin] class in this library uses a static configuration.

应用水印那段（[README.md:L126-L127](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L126-L127)）：

> Remember that the mix-in provided by this library uses a static watermarking configuration, making it unsuitable for production use.

而「静态」在源码里的体现，正是 4.2.3 那行硬编码（[synthid_mixin.py:L73-L83](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L73-L83)）：

```python
def _construct_warper_list(self, extra_params):
    warpers = transformers.LogitsProcessorList()
    warpers.append(
        logits_processing.SynthIDLogitsProcessor(
            **DEFAULT_WATERMARKING_CONFIG, **extra_params   # ← 永远用默认配置
        )
    )
    return warpers
```

注意：`_construct_warper_list` 只接收 `extra_params`（即 temperature/top_k），**没有任何参数让你传入自定义的 `keys` / `ngram_len`**。这就是「静态」的代码级证据——你想换 keys，就得改源码或自己 new 一个 logits processor（README 检测示例里就是这么做的，见 [README.md:L226-L230](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L226-L230)，它直接用 `synthid_mixin.DEFAULT_WATERMARKING_CONFIG` 构造 processor，绕开了 Mixin）。

> 一句话区分：**施加侧（Mixin）只能用静态配置；检测侧（README 示例）可以自己拿配置常量去构造 processor 再算 g 值**，但即便如此，keys 仍然是公开的那 30 个。

#### 4.3.4 代码实践

**实践目标**：用一个最小可运行片段，验证「depth = len(keys)」与「序列长度减少 ngram_len - 1」，从而把抽象字段变成可观察的事实。

**操作步骤**（可运行型，需已按 [u1-l2](./u1-l2-setup-and-run.md) 安装好 `synthid_text`）：

```python
# 示例代码：仅用于演示配置字段如何影响 g 值形状，不是项目原有代码
import torch
from synthid_text import logits_processing

# 直接复用仓库的静态默认配置（keys 有 30 个）
from synthid_text.synthid_mixin import DEFAULT_WATERMARKING_CONFIG

processor = logits_processing.SynthIDLogitsProcessor(
    **DEFAULT_WATERMARKING_CONFIG,
    top_k=40,
    temperature=0.5,
)

# 构造一段假的 token 序列：batch=2, 长度=20
input_ids = torch.randint(0, 1000, (2, 20))
g = processor.compute_g_values(input_ids)
print("默认配置  g.shape =", tuple(g.shape))
# 预期: (2, 20 - (5-1), 30) == (2, 16, 30)

# 再用一个只有 3 个 keys 的自定义配置对比
custom_cfg = dict(DEFAULT_WATERMARKING_CONFIG)
custom_cfg["keys"] = [1, 2, 3]
proc2 = logits_processing.SynthIDLogitsProcessor(**custom_cfg, top_k=40, temperature=0.5)
g2 = proc2.compute_g_values(input_ids)
print("keys=3    g2.shape =", tuple(g2.shape))
# 预期: (2, 16, 3)   ← depth 随 len(keys) 变化
```

**需要观察的现象**：

- `g.shape` 的最后一维恰好等于 `len(keys)`（默认 30，自定义 3）。
- 序列维从 20 缩短成 `20 - (ngram_len - 1) = 20 - 4 = 16`，与 [compute_g_values 文档](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L468) 一致。

**预期结果**：

```text
默认配置  g.shape = (2, 16, 30)
keys=3    g2.shape = (2, 16, 3)
```

> 若你的环境因 JAX/PyTorch 版本差异报错，或无法联网安装，可标记为「待本地验证」——但结论（depth=len(keys)、序列减 ngram_len-1）已由源码直接保证，不依赖运行结果。

#### 4.3.5 小练习与答案

**练习 1**：如果不改源码，有没有办法让 Mixin 子类用一组**自定义 keys** 生成水印？

**参考答案**：走 Mixin 这条路不行——`_construct_warper_list` 把 `DEFAULT_WATERMARKING_CONFIG` 硬编码了，且没有暴露 keys 入口。变通办法是绕开 Mixin，像 README 检测示例那样自己 `SynthIDLogitsProcessor(**你的配置, top_k=..., temperature=...)`。但生产级的正确做法是使用 HuggingFace Transformers 官方实现，而不是改这个参考实现。

**练习 2**：为什么「所有人共用同一组公开 keys」会让水印既怕伪造又怕规避？

**参考答案**：因为 keys 公开，攻击者既能用同样 keys 给任意文本「盖水印」（伪造阳性），也能根据已知 keys 反推哪些 ngram 会被偏置从而刻意回避（让水印检测失效）。私有且可轮换的 keys 才是生产水印的前提，这正是静态配置的根本缺陷。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「配置体检」小任务：

1. 打开 [synthid_mixin.py 的 `DEFAULT_WATERMARKING_CONFIG`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L27-L67)，数一下 `keys` 列表的实际长度，确认 depth。
2. 填一张「字段体检表」，对每个字段写出：① 含义 ② 是否真正进入构造函数 ③ 改动它会影响什么。要求覆盖 `ngram_len`、`keys`、`context_history_size`、`device`、`sampling_table_size`、`sampling_table_seed` 六项。
3. 用一句话回答：如果团队 A 和团队 B 都直接 `pip install synthid-text` 并用 Mixin 生成文本，他们产出的水印能否被区分开？为什么？（提示：结合 4.3 的静态配置局限。）

参考要点：第 2 步应得出 `sampling_table_size` / `sampling_table_seed` 两项「不进入构造函数、改动无影响」；第 3 步应答「不能区分，因为两人用的是同一份公开静态 keys，水印身份完全相同」。

## 6. 本讲小结

- `WatermarkingConfig` 描述「这次水印长什么样」；README 的 `TypedDict` 列了 6 个字段，但**真正被构造函数使用的只有 `ngram_len`、`keys`、`context_history_size`、`device` 四个**，`sampling_table_size` / `sampling_table_seed` 是源码中不存在的历史遗留。
- `len(keys)` 决定水印**深度 depth**：默认配置有 30 个 keys，故 depth=30；g 值形状为 `[batch, seq, depth]`。
- `ngram_len=5` 对应论文 `H=4`，因为 ngram = `H` 个上下文 token + 1 个候选 token，关系是 `ngram_len = H + 1`。
- `DEFAULT_WATERMARKING_CONFIG` 是用 `immutabledict` 包裹的静态常量，被 Mixin 硬编码进 `_construct_warper_list`，所有调用共用一份。
- 静态 + 公开 keys 导致水印既不可隔离也不可保密，因此该 Mixin「不适合生产」，生产化应转向 HuggingFace Transformers 官方实现。
- 再次强化全手册原则：**文档与源码冲突时，以源码（构造函数签名）为准。**

## 7. 下一步学习建议

本讲把「配置」讲清楚了，配置里的 `keys` 和 `ngram_len` 最终都要经过哈希变成 g 值。建议接下来按顺序学习：

- **下一讲 [u2-l2 哈希函数：线性同余 accumulate_hash](./u2-l2-hashing-function.md)**：拆解 `hashing_function.py` 里的 LCG 哈希，看懂 `hash_iv` 之后那串累加是怎么算的。
- **再下一讲 [u2-l3 g 值是什么：从 ngram 到二进制位](./u2-l3-g-values.md)**：把本讲的 `keys`/`ngram_len` 与哈希函数串起来，彻底打通「ngram + keys → depth 个二进制位」的推导链。
- 如果你想先看「配置被谁调用」，可以跳到 [u3-l1 处理器初始化、状态与哈希 IV](./u3-l1-processor-init-and-state.md)，回头看本讲会更有代入感。
