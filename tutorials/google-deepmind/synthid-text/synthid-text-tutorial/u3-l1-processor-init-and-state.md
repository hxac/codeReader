# 处理器初始化、状态与哈希 IV

## 1. 本讲目标

本讲正式进入「水印施加侧」的源码内核 `logits_processing.py`，但只聚焦一个点：**水印处理器在开始工作之前，要把自己初始化成什么样**。

学完本讲，你应当能够：

1. 说清 `SynthIDLogitsProcessor` 的构造函数接收哪些参数、为什么用「纯关键字参数」。
2. 解释一串可见的 `keys` 是如何通过 SHA-256 被压成一个「不可预测的初始向量」`hash_iv` 的，以及它为什么必须落在 int64 范围内。
3. 描述 `SynthIDState` 维护的三个字段 `context` / `context_history` / `num_calls` 各自的形状与用途。
4. 理解 `temperature` 与 `top_k` 两条校验规则的设计意图，并能解释为什么 `temperature=0.0` 或 `top_k=1` 会让水印失效。

本讲是单元三（水印施加机制）的第一讲，只讲「开机准备」，**不讲** `watermarked_call` 的逐 token 主流程（那是 u3-l2）。

---

## 2. 前置知识

本讲默认你已经学过：

- **u2-l1 水印配置**：知道 `WatermarkingConfig` 真正生效的字段是 `ngram_len`、`keys`、`context_history_size`、`device`；`len(keys)` 决定水印「深度 depth」（默认 30）；`ngram_len=5` 对应论文 `H=4`，即 `ngram_len = H + 1`。
- **u2-l2 哈希函数**：知道 `accumulate_hash(current_hash, data)` 是一个改编自 LCG 的可累积哈希，签名是 `f(x, data[T]) = f(f(x, data[:T-1]), data[T])`。它的**第一个参数 `current_hash` 就是初始哈希状态**——本讲要讲的 `hash_iv`，正是喂给这个位置的初值。

一句话回顾：水印的每一个二进制 g 值，都来自「一段 ngram + 一把密钥」被 `accumulate_hash` 搅拌后的结果。而搅拌的起点，就是 `hash_iv`。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，外加上一讲已读过的哈希工具作为对照。

| 文件 | 作用 | 本讲用到哪部分 |
| --- | --- | --- |
| `src/synthid_text/logits_processing.py` | 水印施加内核：处理器类、状态类、g 值与掩码计算 | 构造函数、`hash_iv`、`SynthIDState`、参数校验 |
| `src/synthid_text/hashing_function.py` | 提供 LCG 可累积哈希 `accumulate_hash` | 仅对照其签名，确认 `hash_iv` 是它的初值 |

永久链接速查：

- 处理器构造函数：[logits_processing.py:L135-L147](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L147)
- `hash_iv` 生成：[logits_processing.py:L164-L174](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L164-L174)
- `SynthIDState` 类：[logits_processing.py:L96-L124](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L96-L124)
- `temperature` 校验：[logits_processing.py:L182-L192](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L182-L192)
- `top_k` 校验：[logits_processing.py:L199-L200](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L199-L200)

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：构造函数与 `hash_iv`、`SynthIDState` 结构、参数校验逻辑。

### 4.1 构造函数与 hash_iv

#### 4.1.1 概念说明

`SynthIDLogitsProcessor` 是一个挂进 HuggingFace 生成流程的 **logits processor**（继承自 `transformers.LogitsProcessor`）。它的职责是：在模型每生成一个 token 之前，拿到当前的 `scores`（词表上的 logits），用水印信息对其进行偏置。

但在偏置之前，处理器需要先「认定身份」——它要知道：

- 这次水印用多长的 ngram（`ngram_len`）；
- 用哪几把密钥（`keys`），这决定了水印深度；
- 采样时用多大的温度、取多少个候选（`temperature`、`top_k`）；
- 在哪个设备上算（`device`）。

其中最关键、也最容易被忽略的一步，是把**人类可读的 `keys` 列表**转换成一个**看起来随机的 64 位整数** `hash_iv`，作为整条哈希链的起点。`hash_iv` 就是上一讲 `accumulate_hash(current_hash, ...)` 里那个 `current_hash` 的初始值。

> 为什么不直接拿 `keys` 当初值？因为 `keys` 通常是 `0, 1, 2, ...` 这种结构简单、可预测的整数（默认配置里就是连续整数）。如果直接用它当哈希起点，哈希链的前几步会很有规律，容易被猜到。SHA-256 把它「打散」成一个伪随机的初值，注释里特别强调：**Very important to have an unpredictable IV**（必须有一个不可预测的 IV）。

#### 4.1.2 核心流程

构造函数把 `keys` 变成 `hash_iv` 共三步：

1. 把 `keys` 张量转成 int64（`.to(torch.long)`），再转成原始字节（`.numpy().tobytes()`）。
2. 对这段字节做 SHA-256，得到 32 字节（256 bit）的摘要。
3. 把这 32 字节按**大端序**（big-endian）解释成一个巨大整数，再对 int64 的最大值取模，压回 64 位以内。

数学上，第 3 步是：

\[
\text{hash\_iv} = \left(\sum_{i=0}^{31} b_i \cdot 256^{\,31-i}\right) \bmod \left(2^{63}-1\right)
\]

其中 \(b_i\) 是 SHA-256 摘要的第 \(i\) 个字节，\(2^{63}-1\) 即 `torch.iinfo(torch.int64).max`。取模是为了让结果能安全放进 `torch.long`，避免后续哈希运算里因为整数宽度不一致而出错。

需要注意：构造函数里**不会**立刻创建 `SynthIDState`。`self.state = None`，真正的状态是在第一次 `watermarked_call` 时才惰性初始化的（见 4.2）。

#### 4.1.3 源码精读

先看构造函数签名——注意 `def __init__(self, *, ...)` 里的星号，它表示**所有参数都是纯关键字参数**，调用时必须写 `ngram_len=...` 而不能按位置传，这是一种防止参数写错位置的保护：

[logits_processing.py:L135-L147](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L135-L147) —— 处理器构造函数，参数全部关键字化。

接着是 `keys` 与 `hash_iv` 的核心几行：

```python
self.ngram_len = ngram_len
self.keys = torch.tensor(keys, device=device)

# Hash the keys to a string to be used as initialization vector (IV)
# for the hash function. Very important to have an unpredictable IV.
self.hash_iv = hashlib.sha256(
    self.keys.to(torch.long).numpy().tobytes()
).digest()

# Assuming that the platform supports int64.
torch_long_max = torch.iinfo(torch.int64).max
self.hash_iv = (
    int.from_bytes(self.hash_iv, byteorder="big") % torch_long_max
)
```

这段对应 [logits_processing.py:L161-L174](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L161-L174) —— 把 `keys` 压成一个落在 int64 内、看起来随机的 `hash_iv`。

几个要点：

- `self.keys = torch.tensor(keys, device=device)`：把传入的 Python 整数列表变成张量，后续会沿 depth 维参与哈希。
- `.to(torch.long).numpy().tobytes()`：先确保是 int64，再转字节，保证不同机器上字节布局一致。
- `hashlib.sha256(...).digest()`：返回 32 字节摘要；注意这里覆盖了上一步同名的 `self.hash_iv`（第一次是字节串，第二次是 int）。
- `int.from_bytes(..., byteorder="big") % torch_long_max`：大端序解释后取模，落到 `[0, 2^63-1)`。

最后，`self.state = None` 标记「状态尚未创建」，见 [logits_processing.py:L177](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L177)。

那么 `hash_iv` 之后去哪了？它会被 `compute_ngram_keys` / `_compute_keys` 用作初始 `current_hash`：

[logits_processing.py:L419-L429](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L419-L429) —— 用 `torch.full((batch_size,), self.hash_iv, ...)` 为每个 batch 条目填上同一个 `hash_iv`，作为 `accumulate_hash` 的起点。对照 [hashing_function.py:L21-L26](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L21-L26) 的 `accumulate_hash(current_hash, data)` 签名，就能确认 `hash_iv` 正是那个 `current_hash` 初值。

#### 4.1.4 代码实践

实践目标：验证 `hash_iv` 的「确定性」与「对 keys 敏感」两条性质。

操作步骤：

```python
# 示例代码（非项目原有代码，仅用于演示）
import torch
from synthid_text.logits_processing import SynthIDLogitsProcessor

def make(keys):
    return SynthIDLogitsProcessor(
        ngram_len=5,
        keys=keys,
        context_history_size=2048,
        temperature=1.0,
        top_k=40,
        device=torch.device("cpu"),
    )

p1 = make(list(range(30)))
p2 = make(list(range(30)))   # 完全相同的 keys
p3 = make(list(range(1, 31)))# keys 平移一位

print("p1.hash_iv =", p1.hash_iv)
print("p2.hash_iv =", p2.hash_iv)
print("p3.hash_iv =", p3.hash_iv)
print("int64 上界 =", torch.iinfo(torch.int64).max)
print("p1 < 上界？", p1.hash_iv < torch.iinfo(torch.int64).max)
```

需要观察的现象：

1. `p1.hash_iv == p2.hash_iv`：相同 keys 得到相同 IV（确定性）。
2. `p1.hash_iv != p3.hash_iv`：keys 稍变，IV 完全不同（雪崩效应）。
3. `p1.hash_iv` 远小于 \(2^{63}-1\)，是一个看似随机的正整数。

预期结果：前两条应为 `True`，第三条应成立。具体的 `hash_iv` 数值「待本地验证」（取决于 `keys` 字节与 SHA-256 输出，但确定性关系可从源码断定）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `keys` 从 `list(range(30))` 改成 `list(range(1, 31))`（整体加 1），`hash_iv` 会怎么变？

参考答案：因为 SHA-256 对输入极其敏感（雪崩效应），即使输入字节只差一个比特，输出的 32 字节摘要也会几乎完全不同，因此 `hash_iv` 会变成一个与原来毫无关系的全新整数——这正是「换 keys 等于换一种全新水印」的底层原因。

**练习 2**：为什么要对 \(2^{63}-1\) 取模，而不是直接用 SHA-256 摘要？

参考答案：SHA-256 摘要是 256 bit，而后续哈希运算都在 `torch.int64`（64 bit，含符号位）上做。不取模会导致整数宽度不匹配、溢出行为不可控。取模把它安全地压进 int64 正数范围，且取模不破坏「不可预测性」。

---

### 4.2 SynthIDState 结构

#### 4.2.1 概念说明

`SynthIDState` 是水印处理器的「运行时记忆」。处理器每被调用一次（生成一个 token），就要读写这份记忆。它记住三件事：

1. **`context`**：当前用来预测下一个 token 的上下文，即最近的 `ngram_len - 1` 个 token。它正是「ngram 里的前 H 个 token」（\(H = \text{ngram\_len} - 1\)）。
2. **`context_history`**：一个滑动窗口，记录**最近见过的若干个上下文的哈希值**，用来判断「这个上下文是不是刚刚已经出现过了」。重复上下文不应重复施水印（这是 u3-l4 的主题）。
3. **`num_calls`**：处理器被调用的次数计数器，配合 `skip_first_ngram_calls` 使用——序列开头还没攒够一个完整 ngram 时，可以跳过水印。

理解这三者的关键，是搞清「ngram 由什么组成」：一个 ngram = `ngram_len - 1` 个上下文 token + 1 个候选 token。所以 `context` 的长度是 `ngram_len - 1` 而不是 `ngram_len`。

#### 4.2.2 核心流程

`SynthIDState.__init__` 做的事情很朴素，就是分配三块张量并置零：

1. `context`：形状 `[batch_size, ngram_len - 1]`，dtype `int64`，全零。
2. `context_history`：形状 `[batch_size, context_history_size]`，dtype `int64`，全零。
3. `num_calls`：Python 整数 `0`。

它**不**在处理器构造时创建，而是由处理器的 `_init_state(batch_size)` 在第一次 `watermarked_call` 时按需创建。这样设计是因为构造处理器时还不知道 `batch_size`（要等真正生成时才确定）。

`context` 会在每次调用后「滑动」：把刚生成的那一个 token 追加到末尾，再丢掉最前面的一个，保持长度恒为 `ngram_len - 1`。这一滑动逻辑写在 `watermarked_call` 里，属于 u3-l2，本讲只点到为止。

#### 4.2.3 源码精读

`SynthIDState` 类定义见 [logits_processing.py:L96-L124](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L96-L124)：

```python
class SynthIDState:
  """SynthID watermarking state."""

  def __init__(self, batch_size, ngram_len, context_history_size, device):
    self.context = torch.zeros(
        (batch_size, ngram_len - 1),
        dtype=torch.int64,
        device=device,
    )
    self.context_history = torch.zeros(
        (batch_size, context_history_size),
        dtype=torch.int64,
        device=device,
    )
    self.num_calls = 0
```

- `context` 对应 [logits_processing.py:L114-L118](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L114-L118)：注意第二维是 `ngram_len - 1`，这就是论文里 \(H\) 个上下文 token 的由来。
- `context_history` 对应 [logits_processing.py:L119-L123](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L119-L123)：一个宽度为 `context_history_size` 的滑动窗口（默认配置里很大，用于容纳一段生成历史）。
- `num_calls = 0` 对应 [logits_processing.py:L124](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L124)。

惰性初始化入口 `_init_state` 见 [logits_processing.py:L204-L211](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L204-L211)：

```python
def _init_state(self, batch_size: int):
  """Initializes the state."""
  self.state = SynthIDState(
      batch_size=batch_size,
      ngram_len=self.ngram_len,
      context_history_size=self.context_history_size,
      device=self.device,
  )
```

它把处理器自己的 `ngram_len` / `context_history_size` / `device` 透传给 `SynthIDState`。在 `watermarked_call` 中，`if self.state is None: self._init_state(batch_size)` 触发它（见 [logits_processing.py:L267-L269](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L267-L269)）。

#### 4.2.4 代码实践

实践目标：手动创建一次状态，看清三块张量的形状。

操作步骤：

```python
# 示例代码
import torch
from synthid_text.logits_processing import SynthIDLogitsProcessor

p = SynthIDLogitsProcessor(
    ngram_len=5, keys=list(range(30)),
    context_history_size=2048, temperature=1.0, top_k=40,
    device=torch.device("cpu"),
)
print("构造后 state 是否为 None：", p.state is None)

p._init_state(batch_size=2)          # 手动触发惰性初始化
print("context 形状：", tuple(p.state.context.shape), "（应为 (2, 4)）")
print("context_history 形状：", tuple(p.state.context_history.shape), "（应为 (2, 2048)）")
print("num_calls：", p.state.num_calls, "（应为 0）")
print("context 是否全零：", bool((p.state.context == 0).all()))
```

需要观察的现象与预期结果：

1. 构造后 `p.state is None` 为 `True`，印证「状态惰性创建」。
2. `context.shape == (2, 4)`，因为 `ngram_len - 1 = 4`。
3. `context_history.shape == (2, 2048)`。
4. `num_calls == 0`，`context` 全零。

这些形状关系完全由源码决定，可断言；具体打印「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `context` 的第二维是 `ngram_len - 1` 而不是 `ngram_len`？

参考答案：一个 ngram 由「`ngram_len - 1` 个上下文 token」加「1 个候选 token」组成。`context` 只存上下文部分，候选 token 是当前词表里所有 top_k 候选，由 `_compute_keys` 在哈希时逐个并入，所以 `context` 只需 `ngram_len - 1` 个位置。

**练习 2**：`context_history` 初始化为全零。如果一段真实生成历史里恰好有某个上下文哈希等于 0，会误判为「重复」吗？为什么影响很小？

参考答案：理论上存在哈希碰撞成 0 的可能。但 `context_history` 是滑动窗口，且只在「上下文哈希等于窗口内某个已有值」时才判重复；全零初值意味着最初几次比较的参照点都是 0，由于真实上下文哈希几乎不会等于 0，窗口很快会被真实哈希填满覆盖，误判概率极低。

---

### 4.3 参数校验逻辑

#### 4.3.1 概念说明

构造函数末尾有两道「门」：校验 `temperature` 与 `top_k`。它们看似只是两行 `if ... raise ValueError`，但**直接关系到水印能否成立**。

水印的本质，是在采样分布上施加一个**统计偏置**：让被打了 g 值标记的候选 token 的概率被抬高或压低。这个机制依赖两件事：

1. **概率是非退化的**：`temperature > 0`，否则 logits 被「无限锐化」，softmax 变成 one-hot，没有概率可以重新分配。
2. **候选不止一个**：`top_k > 1`，否则候选集合里只有一个 token，根本没有「偏置谁」的余地。

因此 `temperature=0.0`（贪心解码）和 `top_k=1`（永远只取最高分）都会让水印彻底失效，处理器选择在构造时就直接拒绝，而不是生成出一段「看似带水印、实则没有」的文本。

#### 4.3.2 核心流程

两条校验的判定逻辑：

- `temperature` 必须是 `float` **且**严格大于 0。否则抛 `ValueError`。若恰为 `0.0`，额外在错误信息里提示「想要贪心解码请用 `do_sample=False`」。
- `top_k` 必须是 `int` **且**大于 1。否则抛 `ValueError`。

注意执行顺序：`temperature` 校验（L182）在 `top_k` 校验（L199）**之前**。所以如果同时传入 `temperature=0.0` 和 `top_k=1`，只会先看到 `temperature` 的报错，看不到 `top_k` 的报错。要分别观察两条信息，需要分开测试。

#### 4.3.3 源码精读

`temperature` 校验见 [logits_processing.py:L182-L192](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L182-L192)：

```python
# Check validity of temperature.
if not (isinstance(temperature, float) and temperature > 0):
  except_msg = (
      f"`temperature` (={temperature}) has to be a strictly positive float,"
      " otherwise your next token scores will be invalid."
  )
  if isinstance(temperature, float) and temperature == 0.0:
    except_msg += (
        " If you're looking for greedy decoding strategies, set"
        " `do_sample=False`."
    )
  raise ValueError(except_msg)

self.temperature = temperature
```

要点：

- 用 `not (A and B)` 的写法，只要类型不是 `float` 或值不大于 0 就拒绝。
- `temperature=0.0` 时追加「贪心解码」提示，说明作者预见到这是最常见的误用。
- 只有通过校验，才会执行 `self.temperature = temperature`（L194）赋值。

`top_k` 校验见 [logits_processing.py:L199-L202](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L199-L202)：

```python
# Check validity of top_k.
if not (isinstance(top_k, int) and top_k > 1):
  raise ValueError(f"`top_k` has to be > 1, but is {top_k}")

self.top_k = top_k
```

要求 `top_k` 是 `int` 且严格大于 1，即最少要 2 个候选。

#### 4.3.4 代码实践

实践目标：分别触发两条校验，看清报错信息，并解释设计意图。

操作步骤：

```python
# 示例代码
import torch
from synthid_text.logits_processing import SynthIDLogitsProcessor

def build(temperature, top_k):
    return SynthIDLogitsProcessor(
        ngram_len=5, keys=list(range(30)),
        context_history_size=2048,
        temperature=temperature, top_k=top_k,
        device=torch.device("cpu"),
    )

# (A) temperature=0.0：先单独测，避免被 top_k 干扰
try:
    build(temperature=0.0, top_k=40)
except ValueError as e:
    print("[A] temperature=0.0 报错：", e)

# (B) top_k=1：用合法 temperature 单独测
try:
    build(temperature=1.0, top_k=1)
except ValueError as e:
    print("[B] top_k=1 报错：", e)

# (C) 同时传两个非法值：观察只会先报 temperature
try:
    build(temperature=0.0, top_k=1)
except ValueError as e:
    print("[C] 两者皆非法时先报：", e)
```

需要观察的现象与预期结果（错误文案来自源码字面量，可断言；具体终端输出「待本地验证」）：

1. 情形 A 的报错包含 `has to be a strictly positive float` 以及 `do_sample=False` 提示。
2. 情形 B 的报错为 `` `top_k` has to be > 1, but is 1 ``。
3. 情形 C 与 A 完全一致——印证「temperature 校验在前」。

设计意图解释：`temperature=0` 与 `top_k=1` 都会把采样退化为贪心（永远取最高分），此时水印的统计偏置无处施加，强行生成只会得到「伪水印」文本。处理器在构造期就 fail-fast，避免静默出错。

#### 4.3.5 小练习与答案

**练习 1**：`top_k=1` 为什么会破坏水印？

参考答案：`top_k=1` 表示候选集合只剩 1 个 token，水印靠「在多个候选间重新分配概率」来埋信号。候选只有一个时，无论 g 值如何偏置，最终被采到的都只能是那一个 token，水印信号无法进入输出。

**练习 2**：为什么情形 C（同时非法）只报 `temperature`？

参考答案：因为构造函数里 `temperature` 的校验语句（L182）写在 `top_k` 校验（L199）之前。一旦 `temperature` 校验 `raise`，函数立即中断，根本走不到 `top_k` 那一行。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「处理器体检」。

任务：写一段脚本，构造一个**合法**的处理器，把它的「身份指纹」完整打印出来，再解释这些指纹背后的设计。

```python
# 示例代码
import torch
from synthid_text.logits_processing import SynthIDLogitsProcessor

p = SynthIDLogitsProcessor(
    ngram_len=5,                 # 对应论文 H=4
    keys=list(range(30)),        # depth=30
    context_history_size=2048,
    temperature=0.7,
    top_k=40,
    device=torch.device("cpu"),
    skip_first_ngram_calls=False,
    apply_top_k=True,
    num_leaves=2,
)

print("ngram_len         :", p.ngram_len)
print("keys 数量(depth)  :", p.keys.shape)
print("hash_iv           :", p.hash_iv)
print("hash_iv < 2^63-1 ?:", p.hash_iv < torch.iinfo(torch.int64).max)
print("state 是否惰性     :", p.state is None)
p._init_state(batch_size=1)
print("context 形状       :", tuple(p.state.context.shape))
print("context_history 形状:", tuple(p.state.context_history.shape))
print("num_calls          :", p.state.num_calls)
```

完成打印后，用一段话回答：

1. `hash_iv` 是怎么由 `keys` 得到的？（SHA-256 + 取模）
2. 为什么 `context` 第二维是 4？（`ngram_len - 1 = H`）
3. 为什么 `temperature` / `top_k` 必须满足那两条约束？（水印依赖非退化采样）

预期：`keys` 形状为 `(30,)`，`context` 形状为 `(1, 4)`，`context_history` 形状为 `(1, 2048)`，`num_calls` 为 0。`hash_iv` 为某个小于 \(2^{63}-1\) 的正整数，具体数值「待本地验证」。

---

## 6. 本讲小结

- 构造函数全部参数关键字化（`def __init__(self, *, ...)`），避免位置传参出错。
- `keys` 经 `SHA-256` → 大端整数 → 对 \(2^{63}-1\) 取模，得到不可预测的 `hash_iv`，它正是 `accumulate_hash` 的初始哈希状态；换 keys 即换水印。
- `SynthIDState` 维护三块运行时记忆：`context`（`[B, ngram_len-1]` 上下文）、`context_history`（`[B, context_history_size]` 已见上下文哈希窗口）、`num_calls`（调用计数）；状态惰性创建于首次 `watermarked_call`。
- `temperature` 必须 `float` 且 `> 0`，`top_k` 必须 `int` 且 `> 1`，二者都是为了保证采样非退化、水印偏置有处可施；`temperature=0.0` / `top_k=1` 会在构造期 fail-fast。
- 校验顺序是 `temperature` 在前、`top_k` 在后，同时非法只会先报前者。

---

## 7. 下一步学习建议

本讲只讲了「开机准备」。处理器真正干活的逐 token 主流程在 `watermarked_call`：温度缩放 → 取 top_k → 滑动上下文 → 算 ngram keys → 采 g 值 → 更新得分 → 上下文去重。

- 下一讲 **u3-l2 水印施加主流程 watermarked_call** 会逐步拆解这 5 步，并解释返回的三元组（更新后 scores、top_k indices、原始 scores）的含义。
- 如果你对 `hash_iv` 如何参与整批哈希还意犹未尽，可回头对照 `compute_ngram_keys`（[logits_processing.py:L358-L401](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L358-L401)）和 `_compute_keys`（[logits_processing.py:L403-L448](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L403-L448)）中 `torch.full((batch_size,), self.hash_iv, ...)` 的用法。
