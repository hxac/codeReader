# 综合实战：实现一种新的自定义注意力

## 1. 本讲目标

本讲是 AttentionEngine 学习路线的「集大成」一讲。前面你已经分别学过符号 IR（u2）、降级三件套与 `lower_tl`（u2/u3）、引擎分发与缓存（u3-l3）、线性注意力（u4-l1）以及基准与正确性校验（u5-l4）。本讲要把这些拼到一起，回答一个最实际的问题：

> 如果我现在想发明一种**全新的注意力**，从零到「能跑、能反传、和参考实现对齐」，到底要做哪几件事？

学完后你应当能够：

1. 拿到一种新注意力的数学定义后，**判断它走哪条降级路线**：是纯逐元素（只需 `score_mod`），还是需要行级在线递推（需要设计 `online_rowscales`/`final_rowscales` 与四段方法）。
2. **独立设计 `online_rowscales` 与 `final_rowscales`**，并把在线算法拆成 `online_fwd` / `online_fwd_epilogue` / `forward` / `backward` 四段。
3. 写出符合符号自动微分能力边界的 `score_mod`（知道为什么 sigmoid 要用 tanh 等价写法）。
4. **用 `do_bench_*` 系列入口 + 参考实现做前向与反向对齐**，并能定位「IR 层 / 降级层 / 模板层」的错误。

本讲围绕三个最小模块展开：**online 算法设计**、**四段方法实现**、**前向反向对齐验证**。

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立，这里只做一句话回顾）：

- **用户四件套**：`score_mod`（逐元素分数变换）、`mask_mod`（下标布尔遮蔽）、`online_func`（行级在线算法）、`CustomIO`（q/k/v 之外的可学习输入）。流水线固定为 `scores = q@k → block_mask → score_mod → online_func → o = p@v`（u1-l4）。
- **符号 IR 与降级**：用户函数不被解析源码，而是喂入符号「诱饵」靠运算符重载挂出 DAG，再由 `generate_tl_from_dag` 发射成 TileLang 代码（u2-l3）。`score_mod` 的反向由 `SymbolScalar._backward` 手写的反向模式自动微分完成（u2-l5）。
- **OnlineFunc 的语义**：online 算法把 KV 切块逐块递推行级状态，每块用 `o_scale` 把已累加的 `o` 重缩放；状态分循环内过程量 `online_rowscales` 与循环后落盘量 `final_rowscales` 两套（u2-l6）。
- **引擎**：`AttentionEngine(...)` 构造即编译，tl 后端按形状分发到 `lower`/`lower_gqa`/`lower_decode*`/`lower_decode_mla`，生成源码以 md5 为键缓存、importlib 动态加载（u3-l3）。
- **线性注意力**：另一条平行入口 `LinearAttentionEngine`，用 `q_mod`/`k_mod`/`v_mod`/`decay_mod` 描述，对照参考是 `fla`（u4-l1）。
- **基准与校验**：`do_bench` 用 CUDA Event 计时，`check_close`/`print_debug` 用「绝对误差 < atol **或** 相对误差 < rtol」判定，TFLOPS = FLOPs / t × 1e-9（u5-l4）。

一个反复用到的直觉：**注意力输出的每一行只依赖该 query 对应的那一行 scores**。这条性质决定了能否「分块在线」地计算，是本讲所有设计的出发点。

## 3. 本讲源码地图

本讲以三个用户脚本为「样板间」，对照 `OnlineFunc` 基类与基准工具来讲解。

| 文件 | 作用 |
| --- | --- |
| [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | 标准因果 softmax 注意力，含完整的 `OnlineSoftmax`（行级在线算法的范本）。 |
| [attn_script/reluattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py) | relu 注意力：`score_mod` 用 `max(0)`，搭配空状态 `OnlineIdentity`。是「逐元素路线」的最小样本。 |
| [attn_script/sigmoidattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py) | sigmoid 注意力：用 tanh 等价写 sigmoid，演示符号 autodiff 的能力边界，并带一个 `CustomIO` 偏置。 |
| [attn_script/retention.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py) | transformer 路径下的 retention：`OnlineRetention` 有非平凡 rowscales，是「在线算法设计」的第二范本。 |
| [attn_script/retention_linear.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py) | 线性注意力侧的 retention：演示 `decay_mod`/`q_mod` 的写法。 |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | `OnlineFunc` 基类（四段方法 + combine 的签名）与 `AttentionEngine` 构造签名。 |
| [attention_engine/benchmark/bench_utils.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py) | `check_close`/`print_debug` 校验、`do_bench_reluattn`/`do_bench_retention_linear` 等对齐入口。 |
| [attention_engine/core/transform/core.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | `SymbolScalar._backward`：决定 `score_mod` 反向能支持哪些算子的真凶。 |

阅读建议：先扫 `OnlineFunc` 基类签名，再对照 `mha.py` 把四段方法「填实」，然后看 `reluattn.py`/`sigmoidattn.py` 理解逐元素捷径，最后用 `bench_utils.py` 做对齐。

## 4. 核心概念与源码讲解

### 4.1 第一步永远是分类：逐元素变换 vs 行级在线算法

#### 4.1.1 概念说明

拿到一种新注意力，第一件事不是写代码，而是问一个问题：

> **归一化（或聚合）这一步，对某一行 scores 的输出，是否依赖整行（全部 KV），还是只依赖该行里的每一个分数自身？**

- **只依赖每个分数自身 → 逐元素路线**：`score_mod` 把 `q@k` 后的每个分数单独变换即可（缩放、加偏置、过激活），不需要维护跨块的行级状态。此时 `online_func` 用退化的 `OnlineIdentity`，`online_rowscales`/`final_rowscales` 都是空字典。
  - 例子：**relu attention**（`score = max(score, 0)`）、**sigmoid attention**（`score = sigmoid(score)`）。
- **依赖整行 → 行级在线路线**：归一化分母（softmax 的行和、retention 的行绝对值之和）是整行的函数，必须逐块递推维护「到目前为止」的部分统计量，这就是 `online_rowscales`。归一化的最终结果在循环结束后才确定，需要落盘给反向用，这就是 `final_rowscales`。
  - 例子：**softmax attention**（行最大值 + 行指数和）、**retention**（行绝对值之和，截断到 ≥1）。

这条分类直接决定你要不要设计在线状态、要不要手写四段方法，是「工作量」的分水岭。

#### 4.1.2 核心流程

判断流程可以画成一张决策图：

```
新注意力的数学定义
        │
        ▼
归一化/聚合是否按「整行」进行？
        │
   ┌────┴────┐
  否         是
   │         │
   ▼         ▼
逐元素路线   行级在线路线
score_mod    score_mod（可选逐元素预处理，如缩放）
+            + online_func：
OnlineIdentity  online_rowscales（循环内递推）
（空 rowscales） final_rowscales（循环后落盘）
   │              + 四段方法
   └────┬─────────┘
        ▼
  mask_mod（是否因果/带遮蔽）
        ▼
  AttentionEngine(...) 构造即编译
```

无论哪条路线，最终都汇入同一个 `AttentionEngine` 构造调用，因为引擎只认四件套，不关心你内部是逐元素还是在线。

#### 4.1.3 源码精读

**逐元素路线样板——relu attention 的 `score_mod` 与 `OnlineIdentity`**：

[attn_script/reluattn.py:13-17](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L13-L17) 把 `q@k` 的分数先乘 `1/√D`，再用 `.max(0)` 截断到非负——这就是逐元素 relu：

```python
D = 64
scores_scale = 1/D**0.5
def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
    score = score * scores_scale
    score = score.max(0)   # 等价于 relu(score)
    return score
```

> 注意：`score.max(0)` 里的 `0` 是 Python 标量。`SymbolScalar.op` 会自动把 `int`/`float` 包装成符号常量，所以你可以直接写字面量，参见 [core.py:85-90](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L85-L90)。

[attn_script/reluattn.py:20-45](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L20-L45) 是退化的 `OnlineIdentity`：两个 rowscales 都是空字典，四段方法全部「原样透传」、`o_scale` 恒为 1：

```python
class OnlineIdentity(OnlineFunc):
    def __init__(self):
        online_rowscales = {}      # 无跨块状态
        final_rowscales = {}       # 无需落盘
        super().__init__(online_rowscales, final_rowscales, CustomIO())
    @staticmethod
    def online_fwd(scores, online_rowscales, b, h, q_idx):
        o_scale = SymbolScalar("o_scale", Var("1"))   # 恒不缩放
        return scores, online_rowscales, o_scale
    ...  # epilogue/forward/backward 均原样返回
```

对比 **行级在线路线样板——softmax 的 `OnlineSoftmax`**：[attn_script/mha.py:27-44](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L27-L44) 声明了非空的 rowscales：

```python
online_rowscales = {
    "m": SymbolScalar("m", Var("-inf")),   # 行最大值，循环内递推
    "r": SymbolScalar("r", Var("0.0")),    # 行指数和，循环内递推
}
final_rowscales = {
    "lse": SymbolScalar("lse", Var("0.0")),  # 循环后落盘，供反向复用
}
```

`m`/`r` 是过程量、`lse` 是落盘量——这就和 relu 的空字典形成了鲜明对比，正好印证上面的分类。

#### 4.1.4 代码实践

**目标**：用一个简单的数学判断，预测每种注意力该走哪条路线，再到源码里验证。

**操作步骤**：

1. 对下列三种注意力，先写下你的预测（逐元素 / 行级在线）：
   - `o = softmax(qk / √D) @ v`
   - `o = relu(qk / √D) @ v`
   - retention：`r = clamp(Σ|qk_masked|, 1); o = (qk_masked / r) @ v`
2. 打开三个脚本，只看 `__init__` 里 `online_rowscales`/`final_rowscales` 是否为空，验证你的预测。

**需要观察的现象**：`reluattn.py` 与 `sigmoidattn.py` 的 rowscales 为空；`mha.py` 与 `retention.py` 的 rowscales 非空。

**预期结果**：relu、sigmoid → 逐元素（`OnlineIdentity`）；softmax、retention → 行级在线。这与「归一化是否按整行进行」完全一致。

（本实践是纯源码阅读型，不依赖 GPU，「待本地验证」仅指你需自行打开文件核对。）

#### 4.1.5 小练习与答案

**练习 1**：有人想实现 `o = (tanh(qk) + 1)/2 @ v`（即 sigmoid 注意力的分子部分，但**不除以行和**）。它走哪条路线？

**答案**：逐元素路线。分子里每个分数只被自己的 tanh 变换，没有整行归一化，所以 `score_mod` 写 `((score*0.5).tanh()+1)*0.5`，配 `OnlineIdentity`。这正是 `sigmoidattn.py` 的做法（见 [attn_script/sigmoidattn.py:14-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L14-L18)）。

**练习 2**：为什么 `mha.py` 里 `lse` 放在 `final_rowscales` 而不是 `online_rowscales`？

**答案**：`lse`（log-sum-exp）只在循环结束后、收尾时计算一次，且反向需要复用它来重算归一化分数，因此它是「落盘量」`final_rowscales`；而 `m`/`r` 每处理一个 KV 块都要更新，是「过程量」`online_rowscales`。

---

### 4.2 online 算法设计：online_rowscales 与 final_rowscales

#### 4.2.1 概念说明

当确定要走行级在线路线后，核心设计工作就是**把一个「整行归一化」的数学公式，改写成「逐块递推」的形式**，并据此决定两套状态：

- **`online_rowscales`（过程量）**：每处理一个 KV 块都要读旧值、算新值的状态。设计要点是「新值能否由旧值 + 当前块的局部统计量递推出来」。一旦可以递推，就能避免物化整张 scores 矩阵（这正是 FlashAttention 类算法省显存的根源）。
- **`final_rowscales`（落盘量）**：循环结束后才定下来的、反向需要复用的量。它会被存到全局显存，在反向里被 `forward`/`backward` 段读取。

设计约束：**两套状态的初值要正确**（softmax 的 `m` 初值 `-inf`、`r` 初值 `0`；retention 的累加器初值 `0`），并且**状态之间、状态与 `o` 之间的重缩放因子 `o_scale` 要一致**——每块更新状态时，已累加的 `o` 必须用同一个 `o_scale` 缩放，否则数值会错。

#### 4.2.2 核心流程

以 **online softmax** 为例，把「整行归一化」改写成逐块递推。设当前块分数为 `s`（已是 `score_mod` 后的值），行级状态为 `m`（最大值）、`r`（指数和），已累加输出为 `o`：

\[
m_{\text{new}} = \max(m,\ \mathrm{reduce\_max}(s))
\]

\[
\text{scale} = \exp(m - m_{\text{new}}),\qquad r_{\text{new}} = r\cdot\text{scale} + \mathrm{reduce\_sum}(\exp(s - m_{\text{new}}))
\]

\[
o \leftarrow o\cdot\text{scale} + \exp(s - m_{\text{new}})\cdot v,\qquad o\_scale = \text{scale}
\]

收尾（epilogue）：

\[
o \leftarrow o / r_{\text{new}},\qquad \mathrm{lse} = \log(r_{\text{new}}) + m_{\text{new}}
\]

伪代码：

```
m = -inf; r = 0; o = 0
for each KV block s, v:
    m_new = max(m, rowmax(s))
    scale = exp(m - m_new)
    r = r*scale + rowsum(exp(s - m_new))
    o = o*scale + exp(s - m_new) @ v          # o_scale = scale
    m = m_new
o = o / r
lse = log(r) + m                                # 落盘给反向
```

retention 的递推更简单（无指数，只用绝对值之和与截断）：

\[
r_{\text{wo\_clamp}} \leftarrow r_{\text{wo\_clamp}} + \mathrm{reduce\_abssum}(s),\quad r_{\text{new}} = \max(r_{\text{wo\_clamp}},\ 1.0),\quad o\_scale = r / r_{\text{new}}
\]

\[
s \leftarrow s / r_{\text{new}},\qquad o \leftarrow o\cdot o\_scale + s @ v
\]

关键差异：retention 用 `get_reduce("abssum")`（绝对值之和）而非 `sum`/`max`，且分母用 `max(..., 1.0)` 截断防小数值。

#### 4.2.3 源码精读

**OnlineSoftmax 的 `online_fwd`**：[attn_script/mha.py:47-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63) 完全是上面公式的直译。注意 `scores.get_reduce("max")` 与 `get_reduce("sum")` 是行规约（丢最后一维），与逐元素 `m.max(...)` 不同：

```python
m_new = m.max(scores.get_reduce("max"))          # 行最大值规约
scale_tmp = (m - m_new).exp()
r = r * scale_tmp
scores = (scores - m_new).exp()
r = r + scores.get_reduce("sum")                 # 行求和规约
o_scale = scale_tmp                              # o 用同一 scale 缩放
return scores, {"m": m_new, "r": r}, o_scale
```

**OnlineRetention 的 `online_fwd`**：[attn_script/retention.py:32-48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py#L32-L48) 用 `abssum` 规约与 `max(1.0)` 截断，注意它维护了两个 online 状态 `r_wo_clamp`/`r`：

```python
r_wo_clamp = r_wo_clamp + scores.get_reduce("abssum")
r_new = r_wo_clamp.max(1.0)
o_scale = r / r_new
scores = scores / r_new
return scores, {"r_wo_clamp": r_wo_clamp, "r": r_new}, o_scale
```

**落盘 final_rowscales**：[attn_script/retention.py:50-56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py#L50-L56) 在 epilogue 里把循环内的 `r` 复制为 `final_rowscales["r"]`，供反向读取；softmax 的 epilogue 则落盘 `lse`（[mha.py:65-72](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L65-L72)）。

> 设计提示：状态字典的 key 名是「跨四段方法的契约」——`online_fwd` 返回的新 online 状态、`online_fwd_epilogue` 读取的 online 状态、`forward`/`backward` 读取的 final 状态，都要用同一套 key。

#### 4.2.4 代码实践

**目标**：动手设计一个新在线算法的 rowscales——「online ReLU-mean 归一化」。

**算法定义**（示例，仅供设计练习）：

\[
p = \mathrm{relu}(s),\quad d = \mathrm{reduce\_sum}(p),\quad o = (p / d) @ v
\]

即对 relu 后的分数做**按行求和归一化**。请设计它的两套 rowscales。

**操作步骤**：

1. 判断哪些量需要逐块递推：行和 `d` 需要递推（每块累加 `reduce_sum(p)`）。
2. 写出递推关系：因为 relu 后分数非负且无指数放大，`d_new = d + reduce_sum(relu(s))`，`o_scale = 1`（无需重缩放已有 o）。
3. 决定初值：`d` 初值应为 `0.0`（累加器）。
4. 决定落盘量：反向重算需要整行和，故 `d` 同时是 `final_rowscales`。

**需要观察的现象**：相比 softmax，这里**没有行最大值 m、没有 exp、o_scale 恒为 1**，状态更少，但仍需一个 `online_rowscales["d"]`。

**预期结果**：`online_rowscales = {"d": SymbolScalar("d", Var("0.0"))}`，`final_rowscales = {"d": SymbolScalar("d", Var("0.0"))}`。（这是一个设计练习，是否真能跑通取决于 relu 在 score_mod 与在线算法里的分工；本步只练「设计」，落地见 4.3。）

#### 4.2.5 小练习与答案

**练习 1**：softmax 的 `o_scale` 和 retention 的 `o_scale` 含义相同吗？

**答案**：作用相同、来源不同。两者都是「对已累加的 `o` 做重缩放」的因子，保证累加基准一致。softmax 的 `o_scale = exp(m - m_new)` 来自行最大值变化；retention 的 `o_scale = r / r_new` 来自分母截断值变化。只要 `o` 与行状态用同一个 `o_scale` 同步缩放，数值就正确。

**练习 2**：为什么 `OnlineIdentity` 的 `o_scale` 恒为 1？

**答案**：逐元素路线没有跨块行状态，已累加的 `o` 不需要随新块重缩放，故 `o_scale` 设为常量 `Var("1")`（[reluattn.py:31-33](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L31-L33)），退化为「每块直接累加」。

---

### 4.3 四段方法实现：online_fwd / epilogue / forward / backward

#### 4.3.1 概念说明

设计好 rowscales 后，要把在线算法**落到 `OnlineFunc` 的四个静态方法**里。这四段不是任意切分，而是对应「生成 kernel 的四个代码段」与「反向计算的四个角色」：

| 方法 | 何时执行 | 作用 | 对应降级产物 |
| --- | --- | --- | --- |
| `online_fwd` | 循环内，每个 KV 块 | 递推 online 状态、产出本块缩放后的 scores、给出 `o_scale` | 注入前向 KV 循环体 |
| `online_fwd_epilogue` | 循环后（收尾） | 用最终状态归一化 `o`、计算并落盘 `final_rowscales` | 注入前向 epilogue |
| `forward` | 反向时（重算） | 用落盘的 `final_rowscales` 把「原始 scores」重算成「归一化后的 scores」，供反向用 | 注入反向重算段 |
| `backward` | 反向时 | 给定上游梯度 `dp` 与重算的 scores，求 `dscores` | 注入反向主体 |

第五个方法 `combine` 只在 split-kv 解码时用到（合并多个 split 的部分输出），训练场景用不到（u4-l3/u4-l4）。

四个方法的签名由基类 [attn_engine.py:47-92](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L47-L92) 固定，你只能填实现、不能改参数。

#### 4.3.2 核心流程

四段方法之间的关系（以 softmax 反向为例）：

```
【前向】
scores_raw ──score_mod──> scores
            │
            └─(每块)─> online_fwd(scores, {m,r}) -> scores', {m',r'}, o_scale
                                            │ o 累加并用 o_scale 重缩放
            └─(收尾)─> online_fwd_epilogue(o, {m,r}) -> o/lse, final={lse}

【反向】（recompute-in-backward：重算前向归一化，省掉存中间 scores）
final={lse} ──> forward(scores, {lse}) -> p = exp(scores - lse)   # 重算归一化分数
dp ──> backward(dp, p, {lse}, doosum) -> dscores = (dp - sum(dp·p)) · p
```

softmax 反向的数学：设 `p = softmax(scores)`，`o = p @ v`，则

\[
\frac{\partial o}{\partial \mathrm{scores}} \text{ 对应 } \mathrm{dscores} = (\mathrm{dp} - \mathrm{dppsum})\cdot p,\quad \mathrm{dppsum} = \mathrm{reduce\_sum}(\mathrm{dp}\cdot p)
\]

这正是 [mha.py:80-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L80-L84) 的写法（`dppsum` 由降级器从 `doosum_rowscales` 自动探测，见 u2-l6）。

逐元素路线（relu/sigmoid）的四段全部退化为恒等：`forward` 原样返回 scores，`backward` 原样返回 `dp`，因为逐元素归一化（恒等）的反向就是逐元素反传，由 `score_mod` 的符号 autodiff 接管。

#### 4.3.3 源码精读

**softmax 的四段（行级在线范本）**：

- `online_fwd`：[mha.py:47-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63)（见 4.2.3）。
- `online_fwd_epilogue`：[mha.py:65-72](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L65-L72)，`o / r` 收尾、`lse = log(r) + m` 落盘。
- `forward`：[mha.py:74-78](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L74-L78)，反向重算 `p = exp(scores - lse)`。
- `backward`：[mha.py:80-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L80-L84)，`dscores = (dp - dppsum) * scores`。

**retention 的四段（第二个范本）**：`online_fwd` 见 4.2.3；epilogue [retention.py:50-56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py#L50-L56) 落盘 `r`；`forward` [retention.py:59-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py#L59-L63) 重算 `scores / r`；`backward` [retention.py:65-70](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention.py#L65-L70) 给出 `dscores = dp / r`。

**逐元素路线的退化四段**：[reluattn.py:30-45](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L30-L45) 全部恒等返回；sigmoidattn 的 `OnlineIdentity` 同构（[sigmoidattn.py:24-49](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L24-L49)）。

> 命名陷阱：`forward` 这个方法名容易误解。它**不是前向主路径**，而是「反向时重算归一化 scores」的辅助函数（docstring 也写明 `g(scores, scale) for backward recompute`，见 [attn_engine.py:74-82](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L74-L82)）。前向主路径其实是 `online_fwd` + `online_fwd_epilogue`。

#### 4.3.4 代码实践

**目标**：把 4.2 设计的「ReLU-mean 归一化」的四段方法写出来。

**操作步骤**：在一份新脚本里，仿照 `OnlineRetention` 写一个 `OnlineReluMean`（**示例代码**，未随仓库提供）：

```python
# 示例代码：仅供理解四段方法的结构，非仓库自带
class OnlineReluMean(OnlineFunc):
    def __init__(self):
        online_rowscales  = {"d": SymbolScalar("d", Var("0.0"))}
        final_rowscales   = {"d": SymbolScalar("d", Var("0.0"))}
        super().__init__(online_rowscales, final_rowscales, CustomIO())

    @staticmethod
    def online_fwd(scores, online_rowscales, b, h, q_idx):
        d = online_rowscales["d"]
        d_new = d + scores.get_reduce("sum")          # 累加本块行和
        o_scale = SymbolScalar("o_scale", Var("1"))   # 无需重缩放
        return scores, {"d": d_new}, o_scale          # relu 留给 score_mod

    @staticmethod
    def online_fwd_epilogue(o, online_rowscales, b, h, q_idx):
        d = online_rowscales["d"]
        return o / d, {"d": d}                         # 用整行和归一化 o，落盘 d

    @staticmethod
    def forward(scores, final_rowscales, b, h, q_idx, kv_idx):
        return scores / final_rowscales["d"]           # 反向重算归一化分数

    @staticmethod
    def backward(dp, scores, final_rowscales, doosum_rowscales, b, h, q_idx, kv_idx):
        return dp / final_rowscales["d"]               # dscores（简化，未含 mean 反向全项）
```

**需要观察的现象**：构造 `AttentionEngine(...)` 时若能成功编译，说明四段签名与 rowscales key 自洽；若报错，多半是 key 不一致或某段返回值数量不对。

**预期结果**：构造阶段不抛异常即说明四段接线正确。能否与参考对齐取决于反向公式是否完整（本例 `backward` 简化了 mean 的反向，仅供结构演示）——**待本地验证**，建议先用更稳妥的 `OnlineIdentity`+relu 路线（4.4、综合实践）打通闭环。

#### 4.3.5 小练习与答案

**练习 1**：`forward` 与 `online_fwd` 都接收 scores，它们处理的 scores 含义一样吗？

**答案**：不一样。`online_fwd` 的 scores 是 `score_mod` 后、**逐块**的原始 scores（循环内每块一组）；`forward` 的 scores 是反向时**重算**用的、已经过 `score_mod` 的 scores，要用落盘的 `final_rowscales` 把它还原成「归一化后的分数 p」。前者是前向累加素材，后者是反向求导素材。

**练习 2**：逐元素路线为什么 `backward` 直接 `return dp` 就够？

**答案**：逐元素路线（`OnlineIdentity`）的「在线归一化」是恒等，`p = scores`，于是 `o = scores @ v`，反向 `dscores = dp @ v.T` 完全由 score_mod 之后的标准矩阵链决定，而 score_mod 自身的逐元素反向交给符号 autodiff（4.4）。所以在线算法层面 `backward` 只需把 `dp` 透传给 score_mod 的反向，无需额外减去行和项。

---

### 4.4 score_mod 的写法约束：与符号 autodiff 能力对齐

#### 4.4.1 概念说明

逐元素路线的 `score_mod` 之所以能反传，是因为降级器用 `SymbolScalar._backward` 对它做了**手写的反向模式自动微分**（u2-l5）。但这个手写 autodiff **并非支持全部算子**——它只实现了少数几个算子的求导规则。因此，写 `score_mod` 时必须**主动绕开未实现的算子**，用数学等价但「可微」的写法替代。这是初学者最容易踩的坑：前向能编译、一跑反向就 `raise NotImplementedError`。

当前 `SymbolScalar._backward` 支持的算子类型有：`Add`、`Mul`、`Div`、`Tanh`、`Max`、`Log`；其余（包括 `Exp`、`Sub`、`Abs`、各种 reduce）会抛 `NotImplementedError`。

#### 4.4.2 核心流程

判定一种 score 变换能否反传的流程：

```
score_mod 用到的算子
        │
   全在 {Add, Mul, Div, Tanh, Max, Log} 里？
        │
      是 → 直接写，可前向+反向
      否 → 能否用数学等价改写？
              是 → 改写（如 sigmoid 用 tanh）
              否 → 暂不支持，需扩展 core.py 的 _backward
```

几个常用等价改写：

- **sigmoid**：`σ(x) = 1/(1+e^{-x})` 含 `Exp`（不可微）→ 改写成 `σ(x) = (tanh(x/2)+1)/2`，只用 `Tanh`（可微）。
- **relu**：`max(x, 0)` 用 `Max`（可微，反向走 `maxbwd`）→ 直接写 `score.max(0)`。
- **减法**：`a - b` 用 `Sub`（不可微）→ 改写成 `a + (-b)`，用 `Add` + `Neg`（`Neg` 反向由 `Add`/`Mul` 链路覆盖）。

#### 4.4.3 源码精读

**sigmoid 用 tanh 等价写法的铁证**：[attn_script/sigmoidattn.py:14-21](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L14-L21)。仓库特意把「直接 sigmoid」的写法注释掉，留下 tanh 写法：

```python
def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
    softmax_bias = custom_fwd_inputs.input_tensors["softmax_bias"]
    score = score + softmax_bias
    score = ((score*0.5).tanh() + 1) * 0.5          # ← tanh 等价 sigmoid
    return score

# def score_mod(...):
#     return 1 / (1 + (-score).exp())                # ← 注释掉的 Exp 写法，反向会失败
```

**为什么必须这么做——`_backward` 的能力边界**：[core.py:126-195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L126-L195) 是 `SymbolScalar._backward` 的全部实现。它用 `if/elif` 逐一处理 `Add`/`Mul`/`Div`/`Tanh`/`Max`/`Log`，最后 [core.py:193-195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L193-L195) 的兜底分支对其它类型直接抛错：

```python
else:
    raise NotImplementedError(f"backward for {self.code.type} is not implemented")
```

`Exp` 没有被任何 `elif` 命中，因此走兜底抛错——这正是注释掉的 sigmoid 写法跑不通反向的原因。相对地，`Tanh` 的反向规则在 [core.py:165-173](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L165-L173) 实现（`grad0 = grad - grad*self*self`，因为 tanh 的导数是 `1-tanh²`），所以 tanh 写法能反传。

**relu 用 Max 直接写**：`Max` 的反向在 [core.py:174-185](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L174-L185) 实现（用 `maxbwd` 做逐元素选择），故 `reluattn.py` 的 `score.max(0)` 前向反向都通。

#### 4.4.4 代码实践

**目标**：亲手触发一次「不可微」报错，再用等价写法修复。

**操作步骤**：

1. 复制 `sigmoidattn.py`，把 `score_mod` 改回注释里的 `return 1 / (1 + (-score).exp())`。
2. 构造 `mod` 并调用 `do_bench_sigmoidattn(..., requires_grad=True)`（见 [bench_utils.py:701-702](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L701-L702)），让它在 [bench_utils.py:730-734](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L730-L734) 触发 `o.backward(do)`。
3. 观察报错栈，定位到 `core.py` 的 `NotImplementedError`。
4. 改回 tanh 写法，重新运行，反向应能跑通。

**需要观察的现象**：改回前，反向阶段抛 `NotImplementedError: backward for Exp is not implemented`；改回后，`print_debug(query.grad, dQ)` 等三处校验通过（[bench_utils.py:746-750](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L746-L750)）。

**预期结果**：tanh 写法下，与 `flash_sigmoid` 参考的 `dQ/dK/dV` 相对误差在 rtol/atol 范围内（具体数值**待本地验证**，取决于 GPU 与 dtype）。

#### 4.4.5 小练习与答案

**练习 1**：relu attention 的 `score.max(0)`，`0` 这个常量有没有梯度？反向会不会出问题？

**答案**：`0` 被包装成 `SymbolicConst`，`require_grad` 为假，所以 `_backward` 里 `if self.prev[1].require_grad` 分支不会触发（[core.py:181-185](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L181-L185)）。梯度只回传给 `score` 这一支（通过 `maxbwd` 做门控），反向正常。

**练习 2**：如果我想在 score_mod 里写 `score - bias`，正确做法是？

**答案**：`Sub` 未实现反向，应改写成 `score + (-bias)`（`__neg__` → `Neg`，再 `Add`）。或直接 `score + (-1) * bias`，全程落在 `Add`/`Mul` 上。

---

### 4.5 前向+反向对齐验证：do_bench_* 与参考实现

#### 4.5.1 概念说明

实现完一种新注意力，「能编译」不等于「算得对」。必须做两件事：**正确性对齐**（和参考实现的输出、梯度逐元素比对）与**性能测量**（TFLOPS）。AttentionEngine 在 `bench_utils.py` 为每种内置注意力都配了一个 `do_bench_*` 入口，它把「造数据 → 前向 → 反向 → 参考前向 → 参考反向 → `print_debug` 逐元素比对 → 计时」打包成一条龙。

设计自定义注意力时，关键是**选对参考实现**：

- 逐元素/标准 softmax → flash-attn（fa2/fa3）或纯 torch einsum；
- sigmoid → `flash_sigmoid`；
- relu / retention → 手写 torch einsum；
- 线性注意力 → `fla`（如 `chunk_retention`）。

#### 4.5.2 核心流程

对齐一条新注意力的标准动作：

```
1. 写参考程序 ref_program(q,k,v,...)：用纯 torch 把同样的数学算一遍
2. o      = attn(q,k,v)        # 生成 kernel 前向
3. o_ref  = ref_program(q,k,v) # 参考前向
4. print_debug(o, o_ref)       # 逐元素比对（输出）
5. o.backward(do); o_ref.backward(do)
6. print_debug(q.grad, q_ref.grad)  # 逐元素比对（dQ/dK/dV）
7. do_bench 计时，TFLOPS = FLOPs/t*1e-9
```

`print_debug`（[bench_utils.py:158-192](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L158-L192)）报告「不接近元素数、占比、最大绝对/相对误差及其位置」，是排错主工具；`check_close`（[bench_utils.py:133-155](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L133-L155)）则是断言式硬判定。两者都用「绝对误差 < atol **或** 相对误差 < rtol」的 **or** 语义。

#### 4.5.3 源码精读

**relu 对齐入口**：`do_bench_reluattn`（[bench_utils.py:949-1022](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L949-L1022)）。它的参考实现是纯 torch einsum + `F.relu`：

```python
def ref_program(query, key, value):
    qk = torch.einsum('bqhd,bkhd->bhqk', query, key)
    qk = qk / (D ** 0.5)
    qk = F.relu(qk)
    o  = torch.einsum('bhqk,bkhd->bqhd', qk, value)
    return o
```

随后对输出与三路梯度分别 `print_debug`（[bench_utils.py:987-992](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L987-L992)），并用 `tilelang.profiler.do_bench` 测前向/反向延迟、换算 TFLOPS（[bench_utils.py:1008-1022](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1008-L1022)）。注意它的 `causal` 形参**只影响 TFLOPS 估算（×0.5），不影响 ref_program**——这条参考永远是全量（非因果）的。这是一个关键陷阱（综合实践会用到）。

**线性注意力对齐入口**：`do_bench_retention_linear`（[bench_utils.py:617-698](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L617-L698)）。参考实现是 `fla.ops.retention.chunk_retention`（[bench_utils.py:642-646](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L642-L646)），并对输出与 `q.grad/k.grad/v.grad` 三路做 `print_debug`（[bench_utils.py:662-668](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L662-L668)，rtol/atol=1e-2）。

> 工程注意：`do_bench_attention`（softmax 用）走本模块自带的 `do_bench`；而 `do_bench_reluattn`/`do_bench_retention`/`do_bench_retention_linear` 用的是 `tilelang.profiler.do_bench`（[bench_utils.py:994](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L994)、[670](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L670)）。跨变体比延迟时要心里有数（u5-l4）。

#### 4.5.4 代码实践

**目标**：用现成入口验证 relu 注意力的前向+反向对齐。

**操作步骤**：

1. 按 u1-l2 配好环境（`PYTHONPATH` 挂 `attention_engine/` 与 `3rd_parties/tilelang`，`LD_PRELOAD` 预加载 `libcuda.so`）。
2. 直接运行 `attn_script/reluattn.py` 的 `__main__`（[reluattn.py:79-96](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L79-L96)），它构造 `mod` 后调用 `do_bench_reluattn(mod, ..., requires_grad=True)`。
3. 观察终端：先打印输出对齐（`print_debug(o, o_ref)`），再打印 `dQ/dK/dV` 三路对齐，最后打印 `tl` 与 `torch` 的延迟/TFLOPS。

**需要观察的现象**：输出与三路梯度的「not close 占比」应接近 0%，`torch.allclose` 为 True；TFLOPS 一栏 `tl` 应与 `flash`（这里其实是 torch 参考程序）处于同一量级。

**预期结果**：fp16 下输出与梯度应在默认 rtol=atol=1e-3 内对齐；具体 TFLOPS 数值**待本地验证**（依赖显卡型号、是否命中 autotune 缓存 `reluattn_tune.json`/`reluattn_tune_bwd.json`）。

#### 4.5.5 小练习与答案

**练习 1**：`do_bench_reluattn` 的 `causal=True` 会让参考输出变因果吗？

**答案**：不会。`causal` 只在 [bench_utils.py:952](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L952) 用于把 TFLOPS 乘 0.5，`ref_program` 本身不读 `causal`，永远算全量注意力。所以若你的 kernel 是因果的，直接套这个入口会**对不上**，必须改写参考（见综合实践）。

**练习 2**：为什么线性注意力的 `print_debug` 用 1e-2 而不是 1e-3？

**答案**：线性注意力跨块递推状态 H，误差会沿序列累积，且常用 bfloat16，数值容差需放宽；retention/retention_linear 都用 1e-2（[bench_utils.py:662](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L662)、[1071](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1071)）。逐元素 relu/softmax 一般可用 1e-3。

---

## 5. 综合实践

把本讲三件事（路线判定 → 在线设计 → 对齐验证）串成一个端到端任务：**实现因果 relu 注意力（causal relu attention），跑通 tl 后端前向+反向，并与参考对齐**。

> 「OnlineRelu」其实就是 `relu` 的 `score_mod` + `OnlineIdentity`——relu 是逐元素变换，不需要在线状态。真正的练习重点在于：把四件套组装起来、处理 `causal_mask`、并**亲手写出与因果 kernel 匹配的参考程序**。

### 步骤 1：写 score_mod 与 OnlineFunc

新建 `attn_script/my_reluattn.py`（**示例代码**，基于 [reluattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py) 改写）：

```python
# 示例代码
from attn_engine import AttentionEngine, OnlineFunc
from core import CustomIO, SymbolScalar, Var
from core.utils import meta_tensor
import torch

def causal_mask(b, h, q_idx, kv_idx):           # 因果遮蔽
    return q_idx >= kv_idx

D = 64
scores_scale = 1 / D**0.5
def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
    score = score * scores_scale
    score = score.max(0)                          # relu，Max 可反传
    return score

class OnlineRelu(OnlineFunc):                     # 等价于 OnlineIdentity
    def __init__(self):
        super().__init__({}, {}, CustomIO())      # 逐元素：rowscales 为空
    @staticmethod
    def online_fwd(scores, online_rowscales, b, h, q_idx):
        return scores, online_rowscales, SymbolScalar("o_scale", Var("1"))
    @staticmethod
    def online_fwd_epilogue(o, online_rowscales, b, h, q_idx):
        return o, {}
    @staticmethod
    def forward(scores, final_rowscales, b, h, q_idx, kv_idx):
        return scores
    @staticmethod
    def backward(dp, scores, final_rowscales, doosum_rowscales, b, h, q_idx, kv_idx):
        return dp
```

### 步骤 2：构造引擎

```python
# 示例代码
B, H, S, DV = 64, 6, 2048, 64
qkv_meta = (
    meta_tensor(B, H, S, D, dtype=torch.float16),
    meta_tensor(B, H, S, D, dtype=torch.float16),
    meta_tensor(B, H, S, DV, dtype=torch.float16),
)
mod = AttentionEngine(
    qkv_meta, CustomIO({}),
    score_mod=score_mod, mask_mod=causal_mask,    # ← 关键：带因果
    online_func=OnlineRelu(),
)
```

构造即编译；形状 `q_seqlen==kv_len` 且 `head==head_kv` 会分发到 `lower`（[attn_engine.py:293-313](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L293-L313)）。想看生成代码，可 `with open("my_relu_tl.py","w") as f: f.write(mod.tl_code)`（[attn_engine.py:365](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L365)）。

### 步骤 3：写**因果**参考并对齐（关键坑）

`do_bench_reluattn` 自带的 `ref_program` 是**非因果**的（4.5.3），直接套会对不上。两种做法：

- **做法 A（推荐先做）**：先去掉因果，`mask_mod=None`，直接调用 `do_bench_reluattn(mod, B,H,S,D,DV, requires_grad=True)`，验证非因果前向+反向对齐（这一步与 [reluattn.py:79-96](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L79-L96) 完全一致，最容易打通闭环）。
- **做法 B（本任务目标）**：保留 `mask_mod=causal_mask`，自己写因果参考。relu 下被遮蔽位置（默认 `mask_value="-inf"`）经 `relu` 后自然变成 0，等价于「把未来位置置零」。参考程序（**示例代码**）：

```python
# 示例代码：因果 relu 注意力的 torch 参考
def ref_program(query, key, value):
    qk = torch.einsum('bqhd,bkhd->bhqk', query, key) / (D ** 0.5)
    qk = torch.nn.functional.relu(qk)
    causal = torch.tril(torch.ones(S, S, device=qk.device, dtype=torch.bool))
    qk = qk * causal                              # 未来位置置零
    return torch.einsum('bhqk,bkhd->bqhd', qk, value)
```

然后用 `print_debug` 比对 `o` 与 `o_ref`，再各自 `backward(do)` 比对 `q.grad/k.grad/v.grad`。

### 步骤 4：观察与判定

- 构造阶段：不抛异常 = 四件套签名与 rowscales 自洽。
- 对齐阶段：`print_debug` 的「not close 占比」应≈0%，`torch.allclose` 为 True。
- 性能：用 `tilelang.profiler.do_bench` 测延迟，TFLOPS = `2*B*H*S*S*D + 2*B*H*S*S*DV`（因果 ×0.5）/ t × 1e-9。

**待本地验证**：以上数值结果依赖具体显卡与 dtype；本任务在没有 GPU 的环境里无法实跑，请在本机按 u1-l2 配置后验证。若反向对不上，优先排查 4.4 的算子可微性；若前向系统性偏大/偏小，排查 rowscales 初值与 `o_scale` 是否同步。

### 选做：线性注意力侧的自定义 decay_mod

若你想练线性注意力（u4-l1），可仿照 [retention_linear.py:25-30](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py#L25-L30)：

```python
# 示例代码
def decay_mod(decay, custom_io):       # decay: [B,H,T]
    return decay.log()                 # 取对数后做块内 cumsum 衰减
def q_mod(q, custom_io):
    return q * (1 / D**0.5)
```

构造 `LinearAttentionEngine(...)` 后调用 `do_bench_retention_linear(mod, B,H,T,D,DV, requires_grad=True)`，与 `fla` 的 `chunk_retention` 对齐（[bench_utils.py:642-668](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L642-L668)）。

## 6. 本讲小结

- **先分类再动手**：归一化是否按「整行」进行，决定走逐元素路线（`OnlineIdentity`，rowscales 为空）还是行级在线路线（要设计 rowscales + 四段方法）。
- **两套 rowscales 的分工**：`online_rowscales` 是循环内递推的过程量，`final_rowscales` 是循环后落盘、供反向复用的量；初值要正确，`o_scale` 要让 `o` 与行状态同步缩放。
- **四段方法对应四段代码**：`online_fwd`（循环体递推）、`online_fwd_epilogue`（收尾归一化+落盘）、`forward`（反向重算归一化 scores）、`backward`（求 dscores）；签名由基类固定，逐元素路线全部退化为恒等。
- **score_mod 受符号 autodiff 约束**：`SymbolScalar._backward` 仅支持 `Add/Mul/Div/Tanh/Max/Log`，sigmoid 必须用 tanh 等价写法，这是「前向通、反向报错」最常见的坑。
- **对齐验证要选对参考**：逐元素/softmax 用 flash-attn 或 torch einsum，sigmoid 用 flash_sigmoid，线性注意力用 fla；`do_bench_reluattn` 的 `causal` 形参只影响 TFLOPS 不影响参考，因果 kernel 需自写参考。
- **排错分层**：构造期报错→rowscales/签名层；反向 `NotImplementedError`→score_mod 算子层；前向系统偏差→在线算法递推/初值层。

## 7. 下一步学习建议

- **横向扩展算子**：若你的新注意力必须用 `Exp`/`Sub` 等不可微算子，可按 [graph.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py) 与 [core.py:126-195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L126-L195) 的模式，给 `SymbolScalar._backward` 增加一个 `elif` 分支（这正是 u5-l7「二次开发切入点」的第一类练习）。
- **补 decode/combine**：本讲只覆盖训练场景的四段方法；解码场景还需实现第五段 `combine`（[attn_engine.py:94-105](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L94-L105)），可结合 u4-l3/u4-l4 的 split-kv 与 MLA combine 学习。
- **看生成代码**：用 `mod.tl_code` 导出 kernel 源码，对照 u5-l6 的调试技巧，标注每段代码来自哪个降级函数，加深对「描述→代码」整条编译链的理解。
- **继续阅读**：`attn_script/` 下的 `simple_gla.py`、`mamba2.py` 是更复杂的在线/线性注意力样板，可作为下一个实现目标；`retention.py` 与 `retention_linear.py` 对照阅读能帮你彻底看清 transformer 路径与线性路径的差异。
