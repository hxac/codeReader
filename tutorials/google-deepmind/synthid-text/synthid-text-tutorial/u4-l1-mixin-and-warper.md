# SynthIDSparseTopKMixin 与 logits warper

## 1. 本讲目标

在 u3 系列里，我们已经把水印施加的「内核」`SynthIDLogitsProcessor.watermarked_call` 彻底拆解清楚了。但你可能一直有个疑问：**这个处理器是怎么被挂进 HuggingFace 的 `model.generate(...)` 调用链里的？**毕竟用户写代码时只调了一句 `model.generate(do_sample=True, top_k=40, temperature=0.7)`，并没有手动 new 一个 logits processor。

本讲就来回答这个问题。读完本讲，你应当能够：

1. 说清楚 HuggingFace 在采样生成时「logits warper」的概念，以及 SynthID 是从哪两个方法切入去接管它的。
2. 解释 `SynthIDSparseTopKMixin._get_logits_warper` 为什么**只构造一个 warper**（即 `SynthIDLogitsProcessor`），而不是 HF 默认的那一整组 warper。
3. 理解 `temperature`、`top_k` 是如何从 `generation_config` 一路被读取、校验、再注入处理器的，以及这套「两层 fail-fast 校验」如何保护水印正确性。

本讲只覆盖 Mixin 中**构造 warper 列表**这一段（即 `_get_logits_warper` 与 `_construct_warper_list`），不展开 `_sample` 采样循环本身——那是下一讲 u4-l2 的内容。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个概念。

### 2.1 什么是 logits warper

语言模型在每一步生成时，会先输出一个长度为词表大小 `vocab_size` 的向量，叫 **logits**（每个候选 token 一个未归一化的分数）。采样解码（`do_sample=True`）在「拿 logits 去抽样出下一个 token」之前，通常会先对这个分布做几道「整形」处理，比如：

- **Temperature**：把 logits 整体除以一个温度 `T`，`T<1` 让分布更尖、`T>1` 让分布更平。
- **Top-k**：只保留分数最高的 `k` 个候选，其余全部置 `-inf`（概率清零）。
- **Top-p / nucleus**：保留累计概率达到 `p` 的最小候选集合。

HuggingFace 把这些整形步骤抽象成一个个「warper」对象，串成一个列表依次执行。构造这个列表的方法，就叫 `_get_logits_warper`。

> 直觉：可以把 logits 想象成一张「下一 token 的候选成绩单」，warper 就是「改卷老师」——先调温度、再划掉低分、再 watermark……每个老师改完递给下一个。

### 2.2 标准 HF 流程 vs SynthID 想要的流程

标准 HuggingFace 里，`_get_logits_warper` 会返回一组 warper，每个 warper 都在**稠密**的 `[batch, vocab]` 张量上工作。

但 SynthID 的水印施加（见 u3-l2）有一个关键设计：它把 **top_k 截断放在了水印逻辑内部**。`watermarked_call` 先做 `torch.topk`，只返回 `[batch, top_k]` 的稀疏分数和对应的下标映射。也就是说，SynthID 不能像普通 warper 那样「输入稠密、输出稠密」，它天然要改变张量形状。

这就产生了一个冲突：**如果还按 HF 默认的那套多 warper 流程走，行不通。**于是 SynthID 选择直接覆盖 `_get_logits_warper`，让整个 warper 列表里只剩一个 `SynthIDLogitsProcessor`，由它「一肩挑」地完成 temperature + top_k + 水印三件事。这就是本讲类名里 **Sparse** 的由来——水印只在 top_k 个稀疏候选上施加，从而把计算量从词表级别压到 top_k 级别。

> 承接 u1-l3 的「框架即分水岭」与 u3-l2：本讲依然是 PyTorch 侧（水印施加）的内容，主角是 `synthid_mixin.py`。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它会调用另一个文件的类。下表列出本讲的关键落点。

| 文件 | 角色 | 本讲关注的内容 |
| --- | --- | --- |
| `src/synthid_text/synthid_mixin.py` | HuggingFace 集成层（PyTorch） | `DEFAULT_WATERMARKING_CONFIG`、`SynthIDSparseTopKMixin`、`_get_logits_warper`、`_construct_warper_list` |
| `src/synthid_text/logits_processing.py` | 水印内核（PyTorch） | `SynthIDLogitsProcessor.__init__` 的二次参数校验（承接 u3-l1/u3-l2） |

永久链接 base：

```
https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/
```

## 4. 核心概念与源码讲解

### 4.1 覆盖 `_get_logits_warper`：用一个 SynthID warper 替换整套标准 warper

#### 4.1.1 概念说明

`SynthIDSparseTopKMixin` 继承自 `transformers.GenerationMixin`（所有 HF 生成模型的公共基类）。HF 在采样解码时，会调用基类的 `_get_logits_warper(generation_config, ...)` 来构造 warper 列表。

SynthID 把这个方法**整体覆盖**掉，目的正如它的 docstring 所说：只构造 `SynthIDLogitsProcessor` 这一个 warper，由它在内部完成 top_k 与 temperature 缩放后再施加水印，从而「只在 top_k 个候选上做水印」以降低延迟。

这个覆盖是「把水印挂进 `generate`」的**第一道挂载点**：

- 第二道挂载点是被覆盖的 `_sample`（下一讲 u4-l2），它负责在每一步真正调用 `watermarked_call`。
- 本讲只讲第一道：warper 列表是怎么被「换血」的。

#### 4.1.2 核心流程

`_get_logits_warper` 的执行可以拆成 4 步：

```
输入: generation_config (用户在 model.generate(...) 里传的参数会被写进它)
   │
   ▼
[1] 校验 temperature: 必须非 None 且 0.0 <= t <= 1.0，否则 raise
   │
   ▼
[2] 校验 top_k: 必须非 None 且 top_k >= 1，否则 raise
   │
   ▼
[3] 把 temperature、top_k 塞进 extra_params 字典
   │
   ▼
[4] 调用 self._construct_warper_list(extra_params)，返回只含一个 warper 的列表
```

注意它的函数签名里有一个 `**unused_kw`：基类版本可能传入 `top_p`、`typical_p` 等其它采样参数，SynthID 的覆盖**直接忽略它们**（用 `**unused_kw` 吞掉），只从 `generation_config` 里读 `temperature` 和 `top_k`。这是一个容易被忽视的「陷阱」——如果你指望用 top_p 来调分布，它在 SynthID 模型里是静默失效的。

#### 4.1.3 源码精读

先看类声明和被覆盖的方法本体：

> [synthid_mixin.py:70-71](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L70-L71) —— `SynthIDSparseTopKMixin` 继承自 `transformers.GenerationMixin`，这是它能覆盖生成相关方法的根本原因。

> [synthid_mixin.py:85-127](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L85-L127) —— `_get_logits_warper` 的完整实现。它的 docstring 明确写道：「Only the SynthIDLogitsProcessor warper is constructed … This is to improve the latency impact by watermarking by only considering the top_k indices for watermarking.」（只构造 SynthIDLogitsProcessor 这一个 warper，目的是只对 top_k 个下标施水印、降低延迟）。

方法的收尾把校验好的参数交给辅助方法：

```python
# synthid_mixin.py:104-127（节选关键行）
extra_params = {}
...
extra_params["temperature"] = generation_config.temperature
...
extra_params["top_k"] = generation_config.top_k

return self._construct_warper_list(extra_params)
```

> [synthid_mixin.py:127](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L127) —— 把 `extra_params` 传给 `_construct_warper_list`，返回最终的 warper 列表。

参数校验的两个分支我们放到 4.3 节集中精读，这里先建立整体轮廓。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「SynthID 模型的 warper 列表里确实只有一个 warper」，并验证 `top_p` 会被忽略。

**操作步骤**（示例代码，需要本地装好 `synthid_text` 与 `transformers`）：

```python
# 示例代码
import transformers
from synthid_text import synthid_mixin

# 直接构造 Mixin 的「裸」实例来做验证（绕开真实模型，只看 warper 构造）
mixin = synthid_mixin.SynthIDSparseTopKMixin()

cfg = transformers.GenerationConfig(
    do_sample=True,
    temperature=0.7,   # 在 [0, 1] 区间内，合法
    top_k=40,          # >= 1，合法
    top_p=0.9,         # 故意传 top_p，看它会不会被用上
)

warpers = mixin._get_logits_warper(cfg)
print("warper 个数:", len(warpers))
print("唯一 warper 类型:", type(warpers[0]).__name__)
```

**需要观察的现象**：

- `warper 个数` 打印 `1`，而不是 HF 默认的多个。
- `唯一 warper 类型` 打印 `SynthIDLogitsProcessor`。

**预期结果**：列表里只有一个 `SynthIDLogitsProcessor`。这正说明 top_p 既没有变成独立的 warper、也没有进入这个 processor（processor 的构造参数里根本没有 top_p）。

> 待本地验证：如果本地 `transformers` 版本里 `GenerationConfig` 的默认字段略有差异，可能需要显式补几个字段才能成功构造；但 `len(warpers)==1` 与 `SynthIDLogitsProcessor` 这两个核心结论不受影响。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SynthID 不直接复用 HF 的标准 `TopKWarper` + 一个单独的「水印 warper」？

**参考答案**：因为标准 warper 都在稠密 `[batch, vocab]` 张量上工作，而 SynthID 的水印要从 top_k 截断开始、并返回 `[batch, top_k]` 的稀疏张量与下标映射。如果先用标准 TopKWarper 再接水印 warper，要么得让张量保持稠密（白白在水印里处理整个词表，延迟爆炸），要么破坏 warper「输入稠密、输出稠密」的契约。把三件事合并进一个 `SynthIDLogitsProcessor`，是兼顾「稀疏加速」与「接口干净」的唯一办法。

**练习 2**：`_get_logits_warper` 签名里的 `**unused_kw` 起什么作用？

**参考答案**：吞掉基类可能传入的 `top_p`、`typical_p` 等额外采样参数，使它们被静默忽略。SynthID 只信任 `generation_config.temperature` 和 `generation_config.top_k`，其它整形策略一概不参与。

---

### 4.2 `_construct_warper_list`：把默认配置与采样参数合并成一个 processor

#### 4.2.1 概念说明

`_get_logits_warper` 负责「读参数 + 校验」，而真正「造 processor」的活儿交给了一个小小的辅助方法 `_construct_warper_list`。它的职责非常单一：

- 把**静态的水印配置** `DEFAULT_WATERMARKING_CONFIG`（`ngram_len`、`keys`、`context_history_size`、`device`，见 u2-l1）与**运行时采样参数** `extra_params`（`temperature`、`top_k`）合并到一起；
- 实例化**唯一一个** `SynthIDLogitsProcessor`；
- 装进一个 `transformers.LogitsProcessorList` 返回。

可以把它理解成「组装车间」：零件来自两处（静态配置 + 动态参数），产出一个 processor。

#### 4.2.2 核心流程

```
DEFAULT_WATERMARKING_CONFIG  ──┐
   (ngram_len, keys,           │  **解包合并
    context_history_size,      ├──────────────►  SynthIDLogitsProcessor(...)
    device)                    │                        │
                              │                        ▼
extra_params ─────────────────┘              装进 LogitsProcessorList
   (temperature, top_k)                                  │
                                                         ▼
                                            返回 [SynthIDLogitsProcessor]  （长度 1）
```

注意合并用的是 Python 的 `**` 解包：

```python
SynthIDLogitsProcessor(**DEFAULT_WATERMARKING_CONFIG, **extra_params)
```

这要求两个字典的 key 不能冲突——`DEFAULT_WATERMARKING_CONFIG` 提供水印相关字段，`extra_params` 只提供 `temperature` / `top_k`，两者天然不重叠，正好对齐 `SynthIDLogitsProcessor.__init__` 的关键字参数列表。

> 承接 u2-l1：`DEFAULT_WATERMARKING_CONFIG` 是用 `immutabledict` 包起来的静态常量，所有调用共用同一份**公开**的 30 个 keys。这也是 Mixin「不适合生产」的根因——密钥公开、不可隔离。本讲不展开，详见 u2-l1 与 u7-l3。

#### 4.2.3 源码精读

> [synthid_mixin.py:73-83](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L73-L83) —— `_construct_warper_list` 全文。新建一个空的 `LogitsProcessorList`，`append` 一个 `SynthIDLogitsProcessor`，返回。

关键几行：

```python
# synthid_mixin.py:77-83
warpers = transformers.LogitsProcessorList()
warpers.append(
    logits_processing.SynthIDLogitsProcessor(
        **DEFAULT_WATERMARKING_CONFIG, **extra_params
    )
)
return warpers
```

再看一眼它依赖的静态配置本体：

> [synthid_mixin.py:27-67](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L27-L67) —— `DEFAULT_WATERMARKING_CONFIG`：`ngram_len=5`、30 个 `keys`（故 depth=30，见 u2-l3）、`context_history_size=1024`、`device` 自动选 cuda/cpu。

而 `_sample` 里（u4-l2 详讲）对返回列表的拆包方式，恰好印证了「列表长度恒为 1」这个约定：

> [synthid_mixin.py:282-290](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L282-L290) —— `*regular_warpers, watermarking_logits_warper = logits_warper`：把列表拆成「前面的常规 warper」和「最后一个水印 warper」。由于本方法只 append 了一个 processor，这里 `regular_warpers` 必为空，且随后立即断言最后一个就是 `SynthIDLogitsProcessor`。

#### 4.2.4 代码实践

**实践目标**：手动调用 `_construct_warper_list`，确认它产出的列表结构与参数注入正确。

**操作步骤**（示例代码）：

```python
# 示例代码
from synthid_text import synthid_mixin, logits_processing

mixin = synthid_mixin.SynthIDSparseTopKMixin()
warpers = mixin._construct_warper_list({"temperature": 0.5, "top_k": 10})

proc = warpers[0]
print("类型:", isinstance(proc, logits_processing.SynthIDLogitsProcessor))
print("top_k 已注入:", proc.top_k)            # 期望 10
print("temperature 已注入:", proc.temperature) # 期望 0.5
print("ngram_len 来自默认配置:", proc.ngram_len) # 期望 5
print("keys 深度:", proc.keys.shape[0])         # 期望 30
```

**需要观察的现象**：`top_k` / `temperature` 来自 `extra_params`，而 `ngram_len` / `keys` 来自 `DEFAULT_WATERMARKING_CONFIG`。

**预期结果**：分别打印 `True`、`10`、`0.5`、`5`、`30`。这直观体现了「静态配置 + 动态参数」的两路合并。

> 待本地验证：若本地没有 CUDA，`device` 会自动落到 CPU，不影响以上数值结论。

#### 4.2.5 小练习与答案

**练习 1**：如果想让 SynthID 模型换一套**保密**的水印密钥，应该改哪里？

**参考答案**：`_construct_warper_list` 把 `DEFAULT_WATERMARKING_CONFIG` 硬编码进了调用。要换密钥，需要让这个方法改用一个自定义配置（而非公开的 `DEFAULT_WATERMARKING_CONFIG`）。但这超出了 Mixin 的设计意图——它本就是公开密钥的参考实现。正确做法是参考 u7-l3 的生产化路径，转向 HuggingFace Transformers 官方实现。

**练习 2**：为什么 `**DEFAULT_WATERMARKING_CONFIG` 和 `**extra_params` 不会发生 key 冲突？

**参考答案**：前者只含 `ngram_len / keys / context_history_size / device`，后者只含 `temperature / top_k`，两集合不相交；且并集恰好等于 `SynthIDLogitsProcessor.__init__` 的必填关键字参数。这是设计上刻意的职责切分。

---

### 4.3 参数校验：两层 fail-fast 如何保护水印正确性

#### 4.3.1 概念说明

`temperature` 和 `top_k` 在 SynthID 里被**校验了两次**，分别在两个层级：

| 层级 | 位置 | temperature 规则 | top_k 规则 |
| --- | --- | --- | --- |
| 第一层（宽松） | Mixin `_get_logits_warper` | 非 None 且 `0.0 <= t <= 1.0` | 非 None 且 `top_k >= 1` |
| 第二层（严格） | `SynthIDLogitsProcessor.__init__`（见 u3-l1） | `isinstance(float)` 且 `t > 0` | `isinstance(int)` 且 `top_k > 1` |

两层都是 **fail-fast**（早失败）：在生成真正开始**之前**就抛 `ValueError`，而不是等生成跑到一半才出莫名其妙的结果。

为什么要校验得这么严？因为这两个参数直接决定水印「有没有可发挥的空间」：

- `temperature=0` 等价于贪心解码，分布退化成单点，采样毫无随机性，水印的统计偏置无处施加。
- `top_k=1` 意味着每步只剩一个候选 token，水印锦标赛（见 u3-l3）没有第二个候选来「转移概率质量」，水印退化为 no-op。
- `top_k<1`（如 0）连 `torch.topk` 本身都无法执行。

所以校验不是洁癖，而是**保证水印在数学上成立**的护栏。

#### 4.3.2 核心流程

两层校验的触发顺序如下（构造 warper 时依次穿过）：

```
model.generate(do_sample=True, temperature=t, top_k=k)
        │
        ▼
 _get_logits_warper(generation_config)
        │
        ├─ [第一层] 校验 t ∈ [0,1]?  否 → raise ValueError（Mixin 层）
        ├─ [第一层] 校验 k >= 1 ?   否 → raise ValueError（Mixin 层）
        │
        ▼
 _construct_warper_list({"temperature": t, "top_k": k})
        │
        ▼
 SynthIDLogitsProcessor(..., temperature=t, top_k=k)
        │
        ├─ [第二层] 校验 t > 0 ?    否 → raise ValueError（Processor 层）
        └─ [第二层] 校验 k > 1 ?    否 → raise ValueError（Processor 层）
        │
        ▼
 返回唯一 warper，进入 _sample 采样循环（u4-l2）
```

注意两层规则有细微差别：第一层允许 `temperature=0.0` 和 `top_k=1`（在闭区间/下界上），但第二层会**进一步拒绝**它们。这意味着 `temperature=0.0` 或 `top_k=1` 能「混过」第一层，却会被第二层拦下。这种「外松内紧」的设计，让 Mixin 给出贴近 HF 语义的宽泛报错，而 processor 给出水印专属的精确报错。

#### 4.3.3 源码精读

先看 Mixin 第一层的 temperature 校验：

> [synthid_mixin.py:107-115](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L107-L115) —— temperature 必须非 None 且 `0.0 <= temperature <= 1.0`，否则 `raise ValueError("Invalid temperature ... Temperature should be between 0.0 and 1.0.")`。

再看 Mixin 第一层的 top_k 校验：

> [synthid_mixin.py:118-125](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L118-L125) —— top_k 必须非 None 且 `top_k >= 1`，否则 `raise ValueError("Invalid top_k ... Top_k should >= 1.")`。**这正是本讲代码实践任务的落点**：传入 `top_k < 1` 时，`top_k >= 1` 为假，`not(...)` 为真，立即在这里抛错。

然后看 processor 第二层的更严格校验（承接 u3-l1）：

> [logits_processing.py:182-192](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L182-L192) —— temperature 必须是 `float` 且**严格大于 0**；特别地，当 `temperature == 0.0` 时，报错信息还会追加一句「If you're looking for greedy decoding strategies, set `do_sample=False`.」，引导用户改用贪心解码而非采样。

> [logits_processing.py:199-200](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L199-L200) —— top_k 必须是 `int` 且**严格大于 1**，否则 `raise ValueError("`top_k` has to be > 1, but is {top_k}")`。

最后，把校验动机落到「水印主流程实际怎么用 top_k」上，就一目了然为什么 `top_k` 必须 `>= 1`（甚至 `> 1`）：

> [logits_processing.py:245-246](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L245-L246) —— `watermarked_call` 里 `scores_processed = scores / self.temperature` 后立刻 `torch.topk(scores_processed, k=self.top_k, dim=1)`。`torch.topk` 要求 `k >= 1`；而要让水印锦标赛有意义，至少需要 2 个候选（`top_k > 1`）。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：亲手触发各层校验，理解 `top_k < 1` 会在哪一层、以什么信息被拦下，并解释这种设计如何保护水印正确性。

**操作步骤**（示例代码）：

```python
# 示例代码
import transformers
from synthid_text import synthid_mixin

mixin = synthid_mixin.SynthIDSparseTopKMixin()

# 情形 A：top_k = 0 （< 1）
cfgA = transformers.GenerationConfig(do_sample=True, temperature=0.7, top_k=0)
try:
    mixin._get_logits_warper(cfgA)
except ValueError as e:
    print("A 报错:", e)

# 情形 B：top_k = 1 （能过第一层，被第二层拦）
cfgB = transformers.GenerationConfig(do_sample=True, temperature=0.7, top_k=1)
try:
    mixin._get_logits_warper(cfgB)
except ValueError as e:
    print("B 报错:", e)

# 情形 C：temperature = 0.0 （能过第一层，被第二层拦，且附贪心提示）
cfgC = transformers.GenerationConfig(do_sample=True, temperature=0.0, top_k=40)
try:
    mixin._get_logits_warper(cfgC)
except ValueError as e:
    print("C 报错:", e)
```

**需要观察的现象**：

- 情形 A 的报错信息含「Invalid top_k 0 … Top_k should >= 1.」——来自 Mixin 第一层（`synthid_mixin.py:121-124`）。
- 情形 B 的报错信息含「`top_k` has to be > 1, but is 1」——来自 processor 第二层（`logits_processing.py:199-200`），说明它穿过了第一层。
- 情形 C 的报错信息含「strictly positive float」并追加「set `do_sample=False`」——来自 processor 第二层（`logits_processing.py:182-192`）。

**预期结果与解释**：

针对本讲指定的核心问题「**如果传入 `top_k < 1` 会发生什么，以及这种设计如何保护水印正确性**」——

`top_k < 1`（例如 `top_k=0`）会在**第一层**就被拦下：`_get_logits_warper` 里 `generation_config.top_k >= 1` 判定为假，`not(...)` 为真，立即抛 `ValueError("Invalid top_k 0 when sampling with watermarking. Top_k should >= 1.")`。生成过程根本不会开始。

这种 fail-fast 设计保护水印正确性的理由有两条：

1. **避免底层库的混乱报错**：`watermarked_call` 第一步就调用 `torch.topk(scores, k=top_k)`，而 `torch.topk` 本身要求 `k >= 1`。与其让 PyTorch 在生成中途抛一个和「水印」无关的、晦涩的 `RuntimeError`，不如在构造期就用清晰的水印专属信息拒绝。
2. **保证水印数学上有意义**：水印锦标赛（u3-l3）需要在候选 token 之间「转移概率质量」——把质量从 g=0 的 token 挪给 g=1 的 token。这至少需要 2 个候选。`top_k=1` 时只有一个候选，无质量可转移，水印会退化为空操作；`top_k<1` 更是连候选都没有。所以 Mixin 要求 `top_k >= 1`，processor 进一步要求 `top_k > 1`，从源头杜绝退化配置。

> 待本地验证：不同 `transformers` 版本里 `GenerationConfig` 个别字段的默认值可能略有不同；但三种情形各自命中的校验层与报错文案，与上述结论一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `temperature=0.0` 能通过 Mixin 第一层，却被 processor 第二层拒绝？

**参考答案**：第一层规则是闭区间 `0.0 <= t <= 1.0`，包含 0；第二层规则是严格 `t > 0`，不包含 0。`temperature=0.0` 对应贪心解码（分布退化为单点），采样失去随机性、水印偏置无处施加，因此 processor 必须拒绝。第一层保留 0 是为了贴近 HF「temperature 可取 0」的宽泛语义，真正的「水印红线」由 processor 划定。

**练习 2**：如果用户既不传 `temperature` 也不传 `top_k`（都是 `None`），会发生什么？

**参考答案**：第一层校验 `generation_config.temperature is not None` 与 `generation_config.top_k is not None` 都为假，`_get_logits_warper` 会因为 `temperature` 为 `None` 先抛 `ValueError("Invalid temperature None ...")`。即 Mixin 强制要求采样时必须显式给出这两个参数，绝不静默使用某个默认值——这同样是为了保证水印配置明确、可追溯。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「全流程追踪」小任务（源码阅读型，无需运行模型）。

**任务背景**：用户在 Notebook 里写了这样一句（与官方 Notebook 一致）：

```python
outputs = model.generate(
    do_sample=True,
    temperature=0.7,
    top_k=40,
    return_dict_in_generate=True,
)
```

其中 `model` 是 `SynthIDGPT2LMHeadModel` 的实例。

**请完成**：

1. 解释为什么这句 `generate` 会触发本讲的 `_get_logits_warper`，而不是 HF 基类的版本。（提示：看 `SynthIDGPT2LMHeadModel` 的多重继承与 MRO。）
2. 画出从 `do_sample=True` 到「得到一个只含 `SynthIDLogitsProcessor` 的 warper 列表」的完整调用链，标出每一层做了什么（参数读取 → 第一层校验 → 合并配置 → 实例化 processor → 第二层校验）。
3. 指出 `temperature=0.7` 与 `top_k=40` 分别在源码的哪一行被校验、又在哪一行被注入 processor。
4. 把 `top_k` 改成 `0` 再改成 `1`，分别说明会在哪一层、以什么文案报错。

**参考要点**：

- 第 1 问：`SynthIDGPT2LMHeadModel(SynthIDSparseTopKMixin, transformers.GPT2LMHeadModel)`（见 [synthid_mixin.py:396-399](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L396-L399)）。按 Python MRO，`SynthIDSparseTopKMixin` 排在 `GPT2LMHeadModel`（及其基类 `GenerationMixin`）之前，因此它的 `_get_logits_warper` / `_sample` 优先被调用。
- 第 2 问调用链：`generate(do_sample=True)` →（HF 内部，对采样分支）→ `_get_logits_warper(generation_config)`（读取 `temperature=0.7`、`top_k=40`）→ 第一层校验通过 → `_construct_warper_list({"temperature":0.7,"top_k":40})` → `SynthIDLogitsProcessor(**DEFAULT_WATERMARKING_CONFIG, **extra_params)` → 第二层校验通过 → 返回长度为 1 的 `LogitsProcessorList`。之后这个列表会传给被覆盖的 `_sample`（u4-l2）。
- 第 3 问：`temperature` 在 [synthid_mixin.py:107-115](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L107-L115) 校验、[synthid_mixin.py:115](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L115) 注入；`top_k` 在 [synthid_mixin.py:118-125](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L118-L125) 校验、[synthid_mixin.py:125](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L125) 注入；processor 内的二次校验在 [logits_processing.py:182-200](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L182-L200)。
- 第 4 问：`top_k=0` 命中 Mixin 第一层（[synthid_mixin.py:121-124](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L121-L124)），文案为「Invalid top_k 0 when sampling with watermarking. Top_k should >= 1.」；`top_k=1` 穿过第一层、命中 processor 第二层（[logits_processing.py:199-200](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L199-L200)），文案为「`top_k` has to be > 1, but is 1」。

## 6. 本讲小结

- `SynthIDSparseTopKMixin` 继承 `transformers.GenerationMixin`，通过覆盖 `_get_logits_warper` 把 HF 默认的「一整组 warper」换成**唯一一个** `SynthIDLogitsProcessor`，由它内部完成 temperature + top_k + 水印。
- 只构造一个 warper 的根本原因，是 SynthID 要做**稀疏 top_k**：水印只在 top_k 个候选上施加（返回 `[batch, top_k]`），把计算量从词表级压到 top_k 级，降低延迟——这正是类名里 Sparse 的含义。
- `_construct_warper_list` 是「组装车间」：把静态的 `DEFAULT_WATERMARKING_CONFIG` 与运行时的 `extra_params`（temperature / top_k）用 `**` 合并，实例化出唯一的 processor 装进 `LogitsProcessorList`。
- `temperature` / `top_k` 被**两层 fail-fast** 校验：Mixin 第一层较宽（`0<=t<=1`、`top_k>=1`），processor 第二层更严（`t>0`、`top_k>1`），都在生成开始前就抛 `ValueError`。
- 传入 `top_k<1` 会在第一层被拦下（文案 `Top_k should >= 1`）；这种早失败既避免了 `torch.topk` 的底层混乱报错，也杜绝了「候选不足导致水印退化」的非法配置，是保护水印正确性的护栏。
- 一个易被忽视的细节：`**unused_kw` 会吞掉 `top_p` 等其它采样参数，使它们在 SynthID 模型里**静默失效**。

## 7. 下一步学习建议

本讲只讲完了「warper 列表是怎么被构造出来的」这第一道挂载点。但 warper 列表只是个**清单**，真正在每一步生成里去调用 `watermarked_call` 的，是被覆盖的 `_sample` 采样循环。

**下一讲 u4-l2（重写 `_sample` 采样循环）**将回答：

- `_sample` 是如何「最小改动」地从 HuggingFace 复制过来的？
- 它如何拆包 warper 列表、调用 `watermarked_call` 拿到三元组 `(scores, indices_mapping, unwatermarked_scores)`？
- 采样出的稀疏下标又是如何用 `torch.vmap(torch.take)` 回映成稠密 token id 的？

建议阅读顺序：先重读本讲的 4.1.3 与综合实践（建立 warper 列表「长度恒为 1」的印象），再带着「这个列表是怎么被消费的」这个问题进入 u4-l2。之后 u4-l3 会把 `SynthIDGPT2LMHeadModel` / `SynthIDGemmaForCausalLM` 的多重继承用法讲透，完成整个 HuggingFace 集成单元。
