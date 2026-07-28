# OnlineFunc 与 online 算法的降级

## 1. 本讲目标

学完本讲，你应该能够：

- 用「分块递推」的语言描述 online（在线）注意力算法：为什么不能一次性算出整张 scores，而要逐 KV 块维护行级状态并不断重缩放输出 `o`。
- 说清 `OnlineFunc` 抽象里 `online_rowscales` / `final_rowscales` 两套状态的区别，以及 `online_fwd` / `online_fwd_epilogue` / `forward` / `backward` 四段方法各自的职责。
- 读懂 [`lower.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L318-L472) 中的 `lower_online_func`，把它拆成「符号诱饵 → 四段降级 → 注册张量」七个步骤，并指出每一段产出的字符串字段最终填进模板的哪个占位符。
- 理解 `o_scale`（跨块重缩放因子）和 `doosum`（反向逐行求和项）这两个关键量如何被检测、生成与复用。

本讲是 u2-l5（score_mod 降级）的姊妹篇：score_mod 负责「逐元素分数变换」，而 online_func 负责「行级在线算法」，二者共同构成 transformer 注意力前向/反向的核心。

## 2. 前置知识

### 2.1 为什么需要 online 算法

标准 softmax 注意力的公式是：

\[
\text{ attn}(Q,K,V)_i = \frac{\sum_j \exp(s_{ij})\, v_j}{\sum_j \exp(s_{ij})}, \quad s_{ij} = q_i \cdot k_j
\]

如果先把整张 \(s\)（shape `[seq_len, seq_len]`）物化到显存再 softmax，序列一长就会爆显存，且访存代价巨大。**Online（在线）算法**（FlashAttention 的核心思想）的做法是：把 KV 维度切成一个个 `block_N` 大小的块，**每读入一块就立刻更新结果、不再回头**。关键在于：softmax 依赖全局最大值 \(m\) 和分母 \(r\)，而它们要等所有块处理完才知道。于是我们维护一对「逐块递推」的行级状态：

- \(m\)：到目前为止见过的行最大值；
- \(r\)：到目前为止的（已对齐到当前 \(m\) 的）指数和。

每来一个新块，就用新最大值把**已经累加好的输出 `o` 和分母 `r` 一起重缩放**，保证数值始终正确。最终再除以 \(r\) 得到结果。

### 2.2 两套 rowscale

AttentionEngine 把上面的行级状态分成两类（定义在 [`OnlineFunc.__init__`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L30-L45)）：

| 名称 | 生命周期 | 典型例子 | 作用 |
|---|---|---|---|
| `online_rowscales` | 跨 KV 块递推、循环内每块更新 | `m`（行最大值）、`r`（指数和） | 驱动在线递推 |
| `final_rowscales` | 循环结束后一次性算出，**保存到全局显存供反向复用** | `lse`（log-sum-exp） | 前向输出 + 反向重算 |

一句话区分：`online_rowscales` 是「过程中的中间状态」，`final_rowscales` 是「过程结束后的最终状态，且要留给反向用」。

### 2.3 符号降级的通用套路

回顾 u2-l2/u2-l3/u2-l5：AttentionEngine 不解析用户源码，而是构造一批**符号诱饵**（`SymbolScalar` / `SymbolicArray`）喂进用户函数跑一遍，靠运算符重载自动「挂出」一张符号 DAG，再用 `generate_tl_from_dag` 把 DAG 翻译成 TileLang 代码。本讲的 `lower_online_func` 就是把这套套路套到 online 算法的四段方法上。

> 名词提醒：`SymbolicArray`（见 u2-l2）是带 `[block_M, block_N]` 形状的二维符号值，支持 `get_reduce("max"/"sum")` 做**行规约**（丢最后一维）；它和逐元素的 `SymbolScalar` 不同，本讲里 `scores` 就是 `SymbolicArray`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`attention_engine/attn_engine/attn_engine.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L16-L105) | 定义 `OnlineFunc` 基类：四段方法的抽象签名、`online_rowscales`/`final_rowscales` 字段。 |
| [`attention_engine/core/lower/lower.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | **本讲主角**。`lower_online_func` 把 `OnlineFunc` 降级成 kernel 代码字符串；`lowerOnlineFuncOutput` 收集所有产物字段。 |
| [`attention_engine/core/codegen/common.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py) | codegen helper：`fill_op`（初始化）、`func_block`/`call_op`（函数定义/调用）、`copy_op`（缓冲区拷贝）。 |
| [`attention_engine/core/transform/core.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | `SymbolicArray.get_reduce`、`set_allow_reuse`、`clear_codegen` 等符号簿记方法。 |
| [`attention_engine/core/template/tl_template/attn/attn_tl.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py) | TileLang kernel 模板，`{{...}}` 占位符接收降级产物。 |
| [`attn_script/mha.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | `OnlineSoftmax` 标准范例，本讲实践的对象。 |
| [`attn_script/sigmoidattn.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py) | `OnlineIdentity` 范例：状态为空时的退化情形。 |

## 4. 核心概念与源码讲解

### 4.1 online 算法的分块递推模型

#### 4.1.1 概念说明

把 KV 维度切成 \(K\) 个 `block_N` 大小的块。对每个 query 行 \(i\)，设第 \(k\) 块的（经 score_mod 与 mask 处理后的）原始分数为 \(s^{(k)}_{ij}\)。online softmax 的递推关系如下（初始 \(m^{(0)}=-\infty,\ r^{(0)}=0,\ o^{(0)}=0\)）：

\[
m^{(k)}_i = \max\!\big(m^{(k-1)}_i,\ \max_j s^{(k)}_{ij}\big)
\]

\[
\alpha^{(k)}_i = \exp\!\big(m^{(k-1)}_i - m^{(k)}_i\big) \quad\text{（旧状态的缩放因子，}\le 1\text{）}
\]

\[
r^{(k)}_i = \alpha^{(k)}_i \cdot r^{(k-1)}_i + \sum_j \exp\!\big(s^{(k)}_{ij} - m^{(k)}_i\big)
\]

\[
o^{(k)}_i = \alpha^{(k)}_i \cdot o^{(k-1)}_i + \sum_j \exp\!\big(s^{(k)}_{ij} - m^{(k)}_i\big)\, v_j
\]

处理完所有块后做收尾（epilogue）：

\[
\text{out}_i = o^{(K)}_i \,/\, r^{(K)}_i, \qquad \text{lse}_i = \log r^{(K)}_i + m^{(K)}_i
\]

这里的 \(\alpha^{(k)}_i\) 就是 AttentionEngine 里反复出现的 **`o_scale`**——每来一个新块，先把已经累加的 `o` 乘上它，再累加本块贡献。而 `m`、`r` 是 `online_rowscales`，`lse` 是 `final_rowscales`。

#### 4.1.2 核心流程

把上面数学对应到 kernel 主循环（伪代码）：

```text
初始化：T.fill(m, -inf); T.fill(r, 0); T.fill(acc_o, 0); T.fill(o_scale, 1.0)
for k in KV 块:
    scores = Q @ K块                       # score_mod 之后
    (scores, [m_new, r_new], o_scale) = online_fwd(...)   # 在线递推
    acc_o *= o_scale                       # 用本块 o_scale 重缩放旧输出
    m, r = m_new, r_new                    # online_rowscales_update
    acc_o += scores @ V块                  # 累加本块
收尾：o, lse = online_fwd_epilogue(acc_o, [m, r])   # o=o/r, lse=log(r)+m
保存：lse 写回全局显存（供反向复用）
```

注意三点：① `o_scale` 的初值是 `1.0`（见模板 [`attn_tl.py:108`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L107-L108)）；② `acc_o *= o_scale` 发生在 `online_fwd` 之后、累加 `V` 之前（[`attn_tl.py:155-156`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L155-L159)）；③ `final_rowscales`（lse）在收尾段算出并作为 kernel 输出保存。

#### 4.1.3 源码精读：mha.py 的 OnlineSoftmax

[`mha.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L27-L84) 的 `OnlineSoftmax` 几乎是上面数学的逐行翻译：

[`mha.py:47-63`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63) —— `online_fwd`，对应递推的核心几行：

```python
m_new = m.max(scores.get_reduce("max"))      # m^(k) = max(m, 行最大)
scale_tmp = (m - m_new).exp()                # α = exp(m_old - m_new) = o_scale
r = r * scale_tmp                            # r 旧值缩放
scores = (scores - m_new).exp()              # exp(s - m_new)
r = r + scores.get_reduce("sum")             # r 新值
...
o_scale = scale_tmp
return scores, new_online_rowscales, o_scale
```

注意 `scores.get_reduce("max")` 和 `.get_reduce("sum")` 是 `SymbolicArray` 的**行规约**（见 [`SymbolicArray.get_reduce`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L262-L278)，丢掉最后一维 `block_N`），它和逐元素 `m.max(...)`（底层 `Max` 节点）是两回事——这正是 u2-l1 强调的「逐元素 max 与行规约 ReduceMax 的区分」。

[`mha.py:65-72`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L65-L72) —— `online_fwd_epilogue`，对应收尾公式 `o/r` 与 `lse=log(r)+m`。

#### 4.1.4 代码实践：手算一个两块的递推

**实践目标**：用具体数字验证 online 递推的正确性，建立对 `o_scale` 的直觉。

**操作步骤**：

1. 设某 query 行，两块 KV 的原始分数（已 scale）为 \(s^{(1)}=[1,2]\)、\(s^{(2)}=[0,3]\)，对应 \(v^{(1)}=[1,1]\)、\(v^{(2)}=[1,1]\)。
2. 第 1 块：\(m^{(1)}=2\)，\(\alpha^{(1)}=\exp(-\infty-2)=0\)（初值不影响），\(r^{(1)}=e^{-1}+e^{0}\approx1.3688\)，\(o^{(1)}=e^{-1}+e^{0}\approx1.3688\)。
3. 第 2 块：\(m^{(2)}=\max(2,3)=3\)，\(\alpha^{(2)}=\exp(2-3)=e^{-1}\approx0.3679\)，于是 \(r^{(2)}=\alpha r^{(1)} + (e^{-3}+e^{0})\approx0.503+1.0498\approx1.553\)，\(o^{(2)}=\alpha o^{(1)} + (e^{-3}+e^{0})\approx1.553\)。
4. 收尾：\(\text{out}=o^{(2)}/r^{(2)}\approx1.0\)。

**预期结果**：把所有 4 个分数拼起来做标准 softmax \(p_j=e^{s_j}/\sum e^{s_j}\)，\(s=[1,2,0,3]\)，加权求和 \(v\)（全 1）应得到 1.0，与 online 结果一致。

**需要观察的现象**：\(\alpha^{(2)}=0.3679\) 就是 `o_scale`，它把第 1 块累积的 `o`/`r` 整体压低，再补上第 2 块——这正是模板里 `acc_o *= o_scale` 的意义。

> 说明：本实践是手算，不需 GPU；如要核对，可写 5 行 numpy 复现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `online_fwd` 里 `scale_tmp = (m - m_new).exp()` 误写成 `(m_new - m).exp()`，递推会怎样？
**答案**：`o_scale` 变成 \(\exp(m_{new}-m)\ge 1\)，旧 `o`/`r` 会被放大而非缩小，分母与分子错配，最终 softmax 结果错误（数值还可能溢出）。

**练习 2**：为什么 `final_rowscales` 存的是 `lse = log(r)+m`，而不是直接存 \(r\)？
**答案**：反向需要的是稳定的 log-sum-exp；存 `lse` 后反向里 `p = exp(s - lse)` 可直接复用，避免对 \(r\) 取 log 的额外开销与数值问题。

---

### 4.2 OnlineFunc 抽象与四段方法

#### 4.2.1 概念说明

[`OnlineFunc`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L16-L105) 是用户继承的基类（定义在引擎入口文件而非 core，因为它属于「用户 API」层）。它把 online 算法拆成四段**静态方法**，每段都是纯符号操作（输入输出都是 `SymbolScalar`/`SymbolicArray`，无真实数值）：

| 方法 | 调用时机 | 输入 | 输出 | 语义 |
|---|---|---|---|---|
| `online_fwd` | 主循环内每个 KV 块 | `scores, online_rowscales, b,h,q_idx` | `scores_new, new_rowscales, o_scale` | 在线递推一步 + 算本块 o_scale |
| `online_fwd_epilogue` | 主循环结束后一次 | `o, online_rowscales, b,h,q_idx` | `o_new, final_rowscales` | 收尾：归一化 o、算 final_rowscales |
| `forward` | **反向** kernel 内 | `scores, final_rowscales, b,h,q_idx,kv_idx` | `scores_new` | 从保存的 final_rowscales 重算「online 之后的分数」（如概率 p） |
| `backward` | **反向** kernel 内 | `dp, scores, final_rowscales, doosum_rowscales, b,h,q_idx,kv_idx` | `dscores` | 算 dscores |

注意 `forward`/`backward` 是为反向服务的：`forward` 重算前向的中间量（recompute-in-backward，省显存），`backward` 算分数的梯度。

#### 4.2.2 核心流程

四段方法在前向/反向 kernel 中的落点：

```text
前向 kernel:
  循环外初始化 online_rowscales (m, r)
  for k in KV 块:
      scores = Q@K → score_mod
      → online_fwd(...)            # 4.2-a: 递推 + o_scale
      acc_o *= o_scale; acc_o += scores@V
      更新 online_rowscales
  → online_fwd_epilogue(...)       # 4.2-b: 收尾，算 final_rowscales(lse) 并存全局

反向 kernel:
  加载 final_rowscales 分块
  scores = K@Q → score_mod
  → forward(...)                   # 4.2-c: 重算 p = exp(s - lse)
  ... 用 dO 算 dp ...
  → backward(...)                  # 4.2-d: dscores = g_bwd(dp, p, doosum)
```

`doosum_rowscales` 是反向专用的一项「逐行求和」（见 4.4）。

#### 4.2.3 源码精读：基类默认实现与退化情形

基类提供了「什么都不做」的默认实现，对应**逐元素、不需要行规约**的注意力（如 sigmoid/relu attention）。看 [`attn_engine.py:47-61`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L47-L61)：

```python
@staticmethod
def online_fwd(scores, online_rowscales, b, h, q_idx):
    o_scale = SymbolScalar("o_scale", Var("o_scale"))  # 默认占位
    return scores, online_rowscales, o_scale            # 不改 scores，状态不变
```

即：`online_rowscales` 为空、`o_scale` 是常量。`OnlineIdentity`（[`sigmoidattn.py:24-49`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L24-L49)）就是这个退化情形：它的 `online_fwd` 返回 `o_scale = Var("1")`，`forward`/`backward` 直接原样返回。对比 `OnlineSoftmax` 的 `online_fwd`（要更新 m、r、算 scale_tmp），就能体会到「行级在线算法」与「逐元素变换」的本质差异——这正是本讲与 u2-l5（score_mod，纯逐元素）的分界线。

#### 4.2.4 代码实践：读基类 docstring 并对照两个子类

**实践目标**：把四段方法的「签名约定」与两个真实子类对上号。

**操作步骤**：

1. 打开 [`attn_engine.py:16-28`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L16-L28)，读基类 docstring 对四段方法的描述。
2. 打开 [`mha.py:27-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L27-L84)（OnlineSoftmax）和 [`sigmoidattn.py:24-49`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L24-L49)（OnlineIdentity）。
3. 对照下表，确认每段方法「有没有真正干活」：

| 方法 | OnlineSoftmax | OnlineIdentity |
|---|---|---|
| `online_fwd` | 更新 m、r，算 o_scale | 原样返回，o_scale=1 |
| `online_fwd_epilogue` | `o/r`、算 lse | 原样返回 |
| `forward` | `exp(s - lse)` | 原样返回 |
| `backward` | `(dp-doosum)*scores` | 原样返回 |

**预期结果**：能说出「OnlineIdentity 的四段几乎是 no-op，所以 sigmoid attention 不需要行规约、也不需要 doosum」。

#### 4.2.5 小练习与答案

**练习 1**：`forward` 和 `backward` 为什么不在前向 kernel 里调用？
**答案**：它们只在反向 kernel 里用——`forward` 负责 recompute 前向中间量（如概率 p），`backward` 据此算 dscores；前向不需要它们。

**练习 2**：`online_fwd_epilogue` 的输入 `o` 是什么？
**答案**：是主循环累加完所有 KV 块后的 `acc_o`（未归一化的加权和），epilogue 把它除以 `r` 得到最终输出。

---

### 4.3 lower_online_func：四段降级主流程

#### 4.3.1 概念说明

[`lower_online_func`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L318-L472) 是降级层（lower）的指挥者。它输入一个 `OnlineFunc` 实例，输出一个 [`lowerOnlineFuncOutput`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L220-L243)——一捆字符串字段，每个字段对应模板里一个 `{{...}}` 占位符。它的工作和 u2-l5 的 `lower_score_mod` 同构：**构造符号诱饵 → 调用用户方法挂出 DAG → `generate_tl_from_dag` 发射代码 → 用 codegen helper 拼成片段**。区别只在于 online_func 要处理四段方法、两套 rowscale，以及反向专用的 doosum。

#### 4.3.2 核心流程：七步降级

`lower_online_func` 的执行顺序（行号对应 [`lower.py:318-472`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L318-L472)）：

```text
① 构造符号诱饵 (scores, b, h, q_idx, acco)            L321-337
② 生成 online_rowscales 的初始值代码                    L343-346   → online_rowscales_initvalue
③ 调 online_fwd 挂 DAG → 发射函数定义与调用             L348-360   → online_func_def / call_online_func
④ 记录 o_scale 变量名 + rowscales 回写                  L362-372   → o_scale_varname / online_rowscales_update
⑤ 调 online_fwd_epilogue 挂 DAG → 发射收尾代码          L374-382   → online_func_epilogue
⑥ 把 final_rowscales 注册为 kernel 输出张量             L384-399
⑦ 反向：调 forward / backward 挂 DAG 并发射             L402-453   → online_func_fwd / custom_bwd_body / doosum 系列
```

其中 ②③④⑤ 对应前向，⑦ 对应反向。注意 ⑤ 之前会先 [`clear_codegen()`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L209-L211) 复位 online_rowscales 的簿记（`count`/`visit_count`/`lowered`），因为 epilogue 要把这些状态变量**再读一次**重新发射代码，不复位会因 `lowered` 去重而漏掉。

#### 4.3.3 源码精读

**① 符号诱饵**（[`lower.py:321-337`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L321-L337)）：构造 `scores = SymbolicArray(..., [block_M, block_N])`、`acco = SymbolicArray(..., [block_M, dimv])`，以及标量 `b/h/q_idx`。`scores` 用 `SymbolicArray`（不是 `SymbolScalar`），因为 `online_fwd` 里要调 `scores.get_reduce("max")` 做行规约。

**② 初始值**（[`lower.py:343-346`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L343-L346)）：遍历 `online_rowscales`，用 [`init_value_map_func`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L23-L29) 把 `"-inf"` 映射成 `"-1e38"`（避免运行时 NaN），再用 [`fill_op`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L24-L27) 生成 `T.fill(m, -1e38)` / `T.fill(r, 0.0)`。

**③ online_fwd 降级**（[`lower.py:348-360`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L348-L360)）：这一步是核心。

```python
scores_new, new_online_rowscales, o_scalevar = online_fwd(scores, online_rowscales, b, h, q_idx)
for k, v in new_online_rowscales.items():
    new_online_rowscales[k].set_allow_reuse(False)   # 保护跨块状态不被 inplace 复用
o_scalevar.set_allow_reuse(False)
tl_code, input_vars_online = generate_tl_from_dag(
    list(new_online_rowscales.values()) + [scores_new, o_scalevar])
online_func_def = func_block("online_func", input_vars_online.values(), tl_code)
call_online_func = call_op("online_func", input_vars_online.values())
```

- 第一行**真的执行**了用户的 `online_fwd`，靠运算符重载挂出 DAG（返回值 `scores_new`/`new_online_rowscales`/`o_scalevar` 都是符号子树的根）。
- [`set_allow_reuse(False)`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L60-L64) 是关键保护：`m`、`r`、`o_scale` 是跨 KV 块长期存活的状态，绝不能被 u2-l3 的 inplace 复用优化改写到别的缓冲区名，否则下一块就读不到正确状态。
- `generate_tl_from_dag` 把这些根的 DAG 翻译成 TileLang 代码（`tl_code`），同时收集 `input_vars_online`（叶子+中间+输出变量名，作为函数形参）。
- [`func_block`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L110-L118) 把它包成 `def online_func(...): ...` 函数定义；[`call_op`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L29-L30) 生成同名调用 `online_func(scores, m, r, ...)`。二者共用同一份形参/实参名字表。

**④ o_scale 变量名 + rowscales 回写**（[`lower.py:362-372`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L362-L372)）：

```python
o_scale_varname = o_scalevar.varname              # 供模板 acc_o *= o_scale[i]
for k, v in new_online_rowscales.items():
    if v.varname == k: continue                   # 名字没变就跳过
    online_rowscales_update.add_line(copy_op(v, online_rowscales[k]))  # T.copy(m_new, m)
```

为什么需要「回写」？因为 `online_fwd` 里 `m_new` 是一个**新变量名**（由 op 派生），而下一轮循环读的是原始名字 `m`。所以要把新值 `T.copy` 回原名缓冲。`if v.varname == k: continue` 处理「值没变」（如 OnlineIdentity 的空状态）的退化情形。

**⑤ epilogue 降级**（[`lower.py:374-382`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L374-L382)）：先 `clear_codegen()` 复位，再调 `online_fwd_epilogue(acco, online_rowscales, ...)`，发射 `[acco_new] + final_rowscales` 的 DAG。注意它**不包函数**（`online_func_epilogue = str(tl_code)`），直接内联进主 kernel 的循环外。

**⑥ 注册张量**（[`lower.py:384-399`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L384-L399)）：把每个 `final_rowscales`（如 lse）通过 `add_output_tensor` 注册成全局输出 `g_lse`（shape `[batch, heads, seq_len]`）并登记一条 copy_map（reg→global），供前向存盘；中间变量通过 `add_intermediate_tensor` 注册成 fragment 缓冲。

**⑦ 反向降级**（[`lower.py:402-453`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L402-L453)）：仅当传入 `bwd_kernel_options`（即用户 `require_grad`）才执行。核心两步：

```python
# forward → online_func_fwd（重算 p）
scores_2 = online_func.forward(qkT, final_rowscales_bwd, b, h, q_idx, kv_idx)
tl_code, input_vars_fwd = generate_tl_from_dag([scores_2])
online_func_fwd = str(tl_code)

# backward → custom_bwd_body（算 dscores）
qkT.clear_codegen()
dscores = online_func.backward(dsT, qkT, final_rowscales_bwd, doosum_shared, b, h, q_idx, kv_idx)
tl_code, input_vars_bwd = generate_tl_from_dag([dscores])
custom_bwd_body = str(tl_code)
```

其中 `final_rowscales_bwd[k]` 是把全局 `lse` 切成 `[1, block_N]` 的分块符号（反向逐块加载），见 4.4。`backward` 的 `scores` 形参在语义上代表 `forward` 重算得到的「online 之后的值」（OnlineSoftmax 里即概率 p）；具体的缓冲区复用由模板与 inplace 优化衔接（bwd kernel 完整数据流详见 u3-l2）。

#### 4.3.4 代码实践：四段方法 → 降级字段 → 模板占位符（核心任务）

**实践目标**：把 `OnlineSoftmax` 的四个方法，逐一对应到 `lower_online_func` 的输出字段和模板占位符。这是本讲的核心实践。

**操作步骤**：

1. 打开 [`mha.py:47-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L84)，找到 `OnlineSoftmax` 的四段方法。
2. 打开 [`lower.py:318-472`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L318-L472)，找到每段方法被调用的位置和它写入的 `lowerOnlineFuncOutput` 字段。
3. 打开 [`attn_tl.py`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py)，找到对应占位符。
4. 填写下表（答案见下）。

**对照表（参考答案）**：

| `OnlineSoftmax` 方法 | 降级字段（`lowerOnlineFuncOutput`） | 模板占位符 | 在 kernel 中的位置 |
|---|---|---|---|
| `__init__`（声明 m/r/lse） | `online_rowscales_initvalue`（m,r 初值）；lse 经 ⑥ 成为输出 | [`{{online_rowscales_initvalue}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L110) | 前向循环外初始化 |
| `online_fwd` | `online_func_def` + `call_online_func` + `o_scale_varname` + `online_rowscales_update` | [`{{online_func_def}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L73) / [`{{call_online_func}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L148-L159) | 前向循环内 |
| `online_fwd_epilogue` | `online_func_epilogue` | [`{{online_func_epilogue}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L167) | 前向循环后 |
| `forward` | `online_func_fwd` | [`{{online_func_fwd}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L357) | 反向循环内 |
| `backward` | `custom_bwd_body`（+ 触发 `isused_doosum`） | [`{{custom_bwd_body}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L396) | 反向循环内 |

**需要观察的现象**：`online_fwd` 一段方法对应**四个**字段（定义、调用、o_scale、回写），是四段里最「重」的；`online_fwd_epilogue` 只内联一段代码、不包函数；`forward`/`backward` 仅在反向 kernel 出现。

**预期结果**：能不看讲义，说出「online_fwd → online_func_def/call_online_func，epilogue → online_func_epilogue，forward → online_func_fwd，backward → custom_bwd_body」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ⑤ epilogue 之前要先 `clear_codegen()`？
**答案**：因为 `online_rowscales`（m, r）在 ③ 已经被 `generate_tl_from_dag` 遍历过一次（`lowered=True`）；epilogue 要再读 m、r 算 `lse=log(r)+m`，不复位会因为去重标志而漏发它们，导致 epilogue 代码缺失。

**练习 2**：`online_func_def` 是函数定义，而 `online_func_epilogue` 不是——为什么设计不同？
**答案**：`online_fwd` 在循环内被反复调用，包成函数可复用；epilogue 只在循环后执行一次，直接内联更简单、也避免函数调用开销。

---

### 4.4 o_scale 与 doosum：跨块重缩放与反向复用

#### 4.4.1 概念说明

本模块收束两个「横跨前向与反向」的关键量。

**o_scale**：即 4.1 里的 \(\alpha=\exp(m_{old}-m_{new})\)。它由 `online_fwd` 返回，降级后变量名存进 [`o_scale_varname`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L362-L363)。模板里两处用到它：循环前 `T.fill(o_scale, 1.0)`（初值 1），循环内 `acc_o[i,j] *= o_scale[i]`（[`attn_tl.py:155-156`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L155-L156)）。它被 `set_allow_reuse(False)` 保护，保证每块都读到本块新算出的值。

**doosum**（d-out output sum）：softmax 反向梯度里有一个**逐行求和项** \(D_i=\sum_j p_{ij}\,dp_{ij}\)，最终梯度为：

\[
ds_{ij} = p_{ij}\,(dp_{ij} - D_i)
\]

这个 \(D_i\) 就是 doosum（[`mha.py:80-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L80-L84) 里 `dppsum = doosum_rowscales`，`dscores=(dp-dppsum)*scores`）。它在反向 kernel 之前由一个独立的预处理 kernel 算好（即模板里的 `flashattn_bwd_preprocess`，`delta=sum(o*do)`），再作为 `g_doosum` 传入。

#### 4.4.2 核心流程

`lower_online_func` 对 doosum 的处理是**按需检测**：

```text
调 backward(...) 挂 DAG → generate_tl_from_dag([dscores]) → input_vars_bwd
if "doosum_shared" in input_vars_bwd:   # 用户的 backward 用到了 doosum
    isused_doosum = True
据此生成:
    custom_bwd_inputs      = "g_doosum: T.Buffer([batch,heads,seq_len], ...)"   # 形参声明
    custom_bwd_inputs_init = "doosum_shared = T.alloc_shared([1,block_N], ...)" # 分配
    custom_bwd_inputs_load = "T.copy(g_doosum[bz, bx, k*block_N:(k+1)*block_N], doosum_shared)"  # 逐块加载
```

即：**只有当用户的 `backward` 真的引用了 `doosum_rowscales` 参数，降级器才生成 doosum 相关代码**。`OnlineIdentity` 的 `backward` 直接 `return dp`，不碰 doosum，于是 `isused_doosum=False`，模板里那些 `{{isused_doosum}}` 分支全部走 false（见 [`attn_tl.py:593-596`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L593-L596) 的 `if isused_doosum:` 守卫）。

同时 `final_rowscales`（lse）也要在反向里逐块加载，逻辑与 doosum 同构：用 [`final_rowscales_load`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L450-L452) 生成 `T.copy(g_lse[...], lse_shared)`。

#### 4.4.3 源码精读

doosum 检测与生成在 [`lower.py:436-453`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L436-L453)：

```python
dscores = online_func.backward(dsT, qkT, final_rowscales_bwd, doosum_shared, ...)
tl_code, input_vars_bwd = generate_tl_from_dag([dscores])
custom_bwd_body = str(tl_code)

if lower_output.doosum_shared in input_vars_bwd:   # "doosum_shared" 是否出现在 DAG 叶子里
    isused_doosum = True
custom_bwd_inputs = f"g_doosum: T.Buffer([batch, heads, seq_len], accum_dtype), \n" if isused_doosum else ""
...
final_rowscales_load += f"T.copy(g_{k}[bz, bx, k * block_N : (k + 1) * block_N], {v.varname})\n"
custom_bwd_inputs_load = "T.copy(g_doosum[bz, bx, k * block_N : (k + 1) * block_N], doosum_shared)" if isused_doosum else ""
```

这里 [`final_rowscales_bwd[k]`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L420-L423) 被定义成 `SymbolScalar(f"{k}_shared", ..., shape_idx=["1", "block_N"])`——形状 `[1, block_N]` 表示「一行、block_N 个 KV 列」的切片，对应反向 kernel 每次处理一个 KV 块时加载的那一段 lse。`doosum_shared` 同样是 `["1", block_N]`（[`lower.py:417`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L416-L417)）。

> 设计要点：`isused_doosum` 还会反过来影响 [`lower_tl`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L696-L703) 里 `bwd_output_idx_list` 的长度（多用 doosum 时下标整体偏移 1），保证反向 kernel 输出张量（dq/dk/dv）的索引正确。这条链路的完整编排见 u3-l2。

#### 4.4.4 代码实践：对比 OnlineSoftmax 与 OnlineIdentity 的 doosum 分支

**实践目标**：亲眼看到「用不用 doosum」如何改变降级输出。

**操作步骤**：

1. 读 [`mha.py:80-84`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L80-L84)：确认 `OnlineSoftmax.backward` 用了 `doosum_rowscales`（`dppsum = doosum_rowscales`）。
2. 读 [`sigmoidattn.py:47-49`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L47-L49)：确认 `OnlineIdentity.backward` 直接 `return dp`，不碰 doosum。
3. 跟踪 [`lower.py:442-453`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L442-L453)：对两者分别推断 `isused_doosum`、`custom_bwd_inputs`、`custom_bwd_inputs_load` 的值。

**预期结果**：

| | OnlineSoftmax | OnlineIdentity |
|---|---|---|
| `isused_doosum` | `True` | `False` |
| `custom_bwd_inputs` | `g_doosum: T.Buffer(...)` | `""`（空） |
| `custom_bwd_inputs_load` | `T.copy(g_doosum[...], doosum_shared)` | `""`（空） |
| 反向是否调 `mod_prep` 算 delta | 是 | 否 |

**需要观察的现象**：模板 [`attn_tl.py:593-596`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L593-L596) 的 `if isused_doosum:` 守卫，使得 sigmoid attention 的反向不会去算无意义的 delta。

> 待本地验证：若本机有 GPU 与 TileLang，可分别跑 `mha.py`（`require_grad=True`）和 `sigmoidattn.py`，从 `attn_engine/cache/` 导出生成的 `.py`，对比 `flashattn_bwd` 里是否出现 `g_doosum` 与 `delta = mod_prep(o, do)`。

#### 4.4.5 小练习与答案

**练习 1**：降级器是「静态分析」还是「动态执行」判断 doosum 是否被使用？
**答案**：动态执行。它真的调用 `backward(...)` 跑一遍符号 DAG，然后检查 `doosum_shared` 是否出现在 `generate_tl_from_dag` 收集的 `input_vars_bwd` 里——和 score_mod 的符号化套路一致，不靠解析源码。

**练习 2**：为什么 `o_scale` 和 `m`/`r` 都要 `set_allow_reuse(False)`？
**答案**：它们都是跨 KV 块长期存活的状态变量，inplace 复用优化会把输出名改写成输入名以省缓冲区，但那会破坏「下一块读到本块新值」的语义，所以必须显式禁止。

---

## 5. 综合实践：跟踪 o_scale 与 lse 的一次完整旅程

**任务**：以 `mha.py` 的 `OnlineSoftmax` 为对象，把 `o_scale`（前向）和 `lse`（前向存盘 → 反向加载）这两条数据流，从「用户方法」一直跟到「模板占位符」，画出它们的完整生命周期。

**操作步骤**：

1. **前向 o_scale 链**：
   - 起点：[`mha.py:62`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63) `online_fwd` 里 `o_scale = scale_tmp`。
   - 降级：[`lower.py:353,363`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L348-L363) 把它 `set_allow_reuse(False)` 并记录 `o_scale_varname`。
   - 模板：[`attn_tl.py:108`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L107-L108) 初始化为 1.0，[`attn_tl.py:155-156`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L155-L156) `acc_o *= o_scale[i]`。

2. **lse 存盘-加载链**：
   - 算出：[`mha.py:68`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L65-L72) `online_fwd_epilogue` 里 `lse = r.log() + m`。
   - 注册为输出：[`lower.py:385-392`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L384-L399) `add_output_tensor` 登记为 `g_lse`。
   - 前向存盘：经 `lower_kernel` 生成 `output_args_copy_epilogue`，填进模板 [`{{final_rowscales_save}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L172)。
   - 反向加载：[`lower.py:420-423,450-451`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L420-L453) 定义 `final_rowscales_bwd`（`[1,block_N]` 切片）并生成 `final_rowscales_load`，填进模板 [`{{final_rowscales_load}}`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L354)。
   - 反向重算：[`mha.py:75-78`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L74-L78) `forward` 用 `lse` 重算 `p = exp(scores - lse)`，降级为 `online_func_fwd`。

3. **产出**：画两张时序图（前向、反向），标出每个量的「用户代码行 → 降级字段 → 模板占位符」三列对应关系。

**预期结果**：能清晰表述「o_scale 只活在前向循环内、不落盘；lse 前向落盘到全局、反向逐块加载重算」这一对比，理解 `final_rowscales` 的名字里「final」+「供反向复用」的双重含义。

> 待本地验证：若可运行，从 cache 目录导出生成代码，在 `main`（前向）里找到 `acc_o *= ...`、在 `flash_bwd`（反向）里找到 `T.copy(g_lse[...], lse_shared)` 与 `online_func_fwd` 段落，逐一标注来源。

## 6. 本讲小结

- **online 算法**靠逐 KV 块递推行级状态（`m` 最大值、`r` 指数和）避免物化整张 scores；每块用 `o_scale=exp(m_old-m_new)` 把已累加的 `o`/`r` 一起重缩放，保证数值正确。
- **两套 rowscale**：`online_rowscales`（过程状态、循环内更新）与 `final_rowscales`（最终状态、落盘供反向复用，如 `lse`）。
- **`OnlineFunc` 四段方法**：`online_fwd`（递推+o_scale）、`online_fwd_epilogue`（收尾）、`forward`（反向重算）、`backward`（算 dscores）；前两段属于前向，后两段属于反向。
- **`lower_online_func` 七步降级**：符号诱饵 → 初始值 → online_fwd 函数定义/调用 → o_scale/回写 → epilogue 内联 → 注册输出张量 → 反向 forward/backward；产出的字符串字段一一填进模板占位符。
- **保护机制**：`m`/`r`/`o_scale` 用 `set_allow_reuse(False)` 防止 inplace 复用破坏跨块状态；epilogue 前必须 `clear_codegen()` 复位簿记。
- **doosum 按需检测**：只有 `backward` 真的引用了 `doosum_rowscales`，才生成 `g_doosum` 形参与加载代码（`isused_doosum=True`），并影响反向输出索引。

## 7. 下一步学习建议

- **u3-l1（Jinja2 模板渲染）**：本讲多次提到「字段填进 `{{...}}` 占位符」，下一讲正式打开 `attn_template.py`，讲清 `lowerOnlineFuncOutput` 如何被渲染进 `attn_tl.py`。
- **u3-l2（完整 lower_tl 链路）**：本讲的 `lower_online_func` 只是 `lower_tl` 编排的一环。下一讲会把 custom_inputs + score_mod + online_func + mask + lower_kernel 串成一条完整的训练前向+反向流水线，并讲清 `output_idx_list` / `bwd_output_idx_list` 如何受 `isused_doosum` 影响。
- **u4-l4（MLA 解码与 combine kernel）**：`OnlineFunc` 还有一个本讲未展开的 `combine` 方法（[`attn_engine.py:94-105`](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L94-L105)），它服务于 split-kv 解码场景的多 split 合并，留到 MLA 解码讲义深入。
- **动手准备**：尝试仿照 `OnlineSoftmax` 写一个 `OnlineRelu`（其实退化为 `OnlineIdentity` + relu 的 score_mod），预测它的四段降级产物哪些为空、哪些不为空，再到 u5-l5 综合实战里验证。
