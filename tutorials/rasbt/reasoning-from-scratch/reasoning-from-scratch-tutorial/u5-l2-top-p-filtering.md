# Top-p（核采样）过滤

## 1. 本讲目标

学完本讲，你应该能够：

1. **手推** nucleus sampling（核采样）的累积概率保留集合：给定一组概率和 `top_p`，能算出哪些 token 被保留、保留质量是多少。
2. **解释**源码中 `prefix < top_p`（而不是 `cumprobas < top_p`）这一边界写法的确切含义，以及它为什么恰好实现了论文中「累积质量首次达到 top_p 的最小集合」这一定义。
3. **验证** `keep[:, 0] = True` 的最小保留兜底在极端 `top_p` 下如何避免「全零分布」这一非法输入。
4. 读懂 `generate_text_top_p_stream_cache`，说清温度缩放与 top-p 过滤在流水线中的**先后顺序**与**分工**，以及 `temperature=0` 时 top-p 会被绕过这一容易踩坑的细节。

## 2. 前置知识

本讲承接 u5-l1（温度采样），那里 established 的事实不再重复推导，直接使用：

- **logits 与 softmax**：模型每步输出整个词表上的 logits，`softmax` 把它变成概率分布。
- **温度采样**：`scale_logits_by_temperature` 在 softmax **之前**把 logits 除以 T，T<1 锐化、T>1 平滑；采样用 `torch.multinomial`（有放回）按概率抽 token。
- **温度的局限**：温度只是**整体重新加权**——它把长尾概率压小，但长尾上的 token 仍然有非零概率被抽中。T 调大追求多样性时，那些概率极低的「胡言乱语」token 也会混进来；T 调小压住胡言乱语时，多样性又没了。我们想要的是「**该多样时多样、该确定时确定**」。

本讲需要的新概念只有三个，都很轻：

| 概念 | 一句话解释 |
|---|---|
| **nucleus sampling（核采样 / top-p 采样）** | Holtzman 等人 2020 年提出：不固定保留「前 k 个」（top-k），而是保留**按概率从大到小排序后、累积质量首次达到 `top_p` 的最小 token 集合**（这个集合叫「核」），其余 token 概率直接置零，最后重归一化再采样。 |
| **累积和（cumulative sum）** | 排序后逐项往前累加。例如 `[0.5, 0.41, 0.09]` 的累积和是 `[0.5, 0.91, 1.0]`。`torch.cumsum` 一行完成。 |
| **熵（entropy）** | 分布「不确定性」的度量：\[ H(p) = -\sum_i p_i \ln p_i \]（单位 nat）。分布越均匀熵越大，越集中熵越小。top-p 过滤砍掉长尾后重归一化，熵必然下降——我们会在实践中亲手量出降了多少。 |

为什么 top-p 比 top-k 更聪明？top-k 固定保留 k 个 token：面对一个很「尖」的分布（下一个词几乎确定）时，k 个还是太多，可能采出怪词；面对一个很「平」的分布（下一步真的很开放）时，k 个又太少，掐死了多样性。top-p 保留的**个数随分布形状自适应**：尖分布自动少保留，平分布自动多保留。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| [reasoning_from_scratch/ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py) | 主角：`top_p_filter`（L194-L222）与 `generate_text_top_p_stream_cache`（L225-L267）；对照阅读 `generate_text_temp_stream_cache`（L150-L192）、`scale_logits_by_temperature`（L83-L86）、消费者示例 `self_consistency_vote`（L270-L319） |
| [tests/test_ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py) | 两个 top-p 单元测试：截断与重归一化（L82-L91）、批量行独立重归一化（L94-L107） |
| [ch04/01_main-chapter-code/ch04_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb) | 延伸阅读：书中 4.5 节 "Balancing diversity and coherence with top-p sampling"（4.5.1 讲子集选取，4.5.2 讲把过滤器接入生成函数），可视化低温/高温分布叠加 top-p 的效果 |
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) | 预告：第 6 章 GRPO 的 rollout 采样 `sample_response` 直接 `from .ch04 import top_p_filter` 复用本讲的过滤器（L9 导入、L78 调用） |

## 4. 核心概念与源码讲解

### 4.1 top_p_filter：五步流水线

#### 4.1.1 概念说明

`top_p_filter` 解决的问题：**在概率分布上动态划一条截止线，把长尾「核外」token 的概率清零，再把剩余质量重归一化为合法分布**，供后续 `torch.multinomial` 采样。

它和温度的分工可以用一句话概括：

> **温度负责「重塑」整个分布的形状，top-p 负责「砍掉」分布的尾巴。** 温度再怎么调，每个 token 的概率都还是正数；top-p 之后，核外 token 的概率严格为 0，永远不可能被采到。

为什么要砍尾巴？实践中发现（这正是原论文标题 "The Curious Case of Neural Text Degeneration" 指出的现象）：语言模型分布的长尾里堆满了语料中「偶尔出现过」的低质拼接，人类文本从来不会从均匀的全词表里选词。砍掉长尾，是把采样范围收缩到「模型真正有信心的核」。

注意函数的操作对象是**概率**（softmax 之后），不是 logits——核采样的定义就建立在「累积概率质量」上，logits 空间里没有对应物。

#### 4.1.2 核心流程

输入：`probas`，形状 `(batch, vocab_size)` 的概率分布（生成循环里传入的是 `(1, vocab)`，因为取了 `[:, -1]` 后仍保留 batch 维）；标量 `top_p`。

```
top_p_filter(probas, top_p):
  0. 若 top_p 为 None 或 >= 1.0：原样返回（不过滤）
  1. 排序    sorted_probas, sorted_idx = 按概率降序排序（记下原位置）
  2. 累积和  cumprobas[j] = sorted_probas[0] + ... + sorted_probas[j]
  3. 选核    prefix[j] = cumprobas[j] - sorted_probas[j]   # 第 j 个 token 之前的质量
             keep[j]  = prefix[j] < top_p                   # 此前尚未攒够 top_p → 我属于核
             keep[0]  = True                                # 兜底：至少保留概率最大的 1 个
  4. 置零    核外 sorted_probas 置 0
  5. 回填    按 sorted_idx 把置零后的值散射回原始词表位置
  6. 重归一化 filtered / sum(filtered)，使每行重新和为 1
输出：与输入同形状、同「行列结构」的过滤后概率分布
```

用规格里给定的例子手推一遍（这是本讲的核心算例，后面实践会验证）：

排序后概率 \( p = (0.5,\ 0.41,\ 0.09) \)，取 \( \text{top\_p} = 0.9 \)。

\[
\begin{aligned}
\text{cumprobas} &= (0.5,\ 0.91,\ 1.0) \\
\text{prefix} &= \text{cumprobas} - p = (0,\ 0.5,\ 0.91) \\
\text{keep} &= \text{prefix} < 0.9 = (\text{True},\ \text{True},\ \text{False})
\end{aligned}
\]

保留前两个 token，保留质量 \( 0.5 + 0.41 = 0.91 \ge 0.9 \)——恰好是「累积质量**首次达到** 0.9 的最小集合」。重归一化后：

\[
\left( \frac{0.5}{0.91},\ \frac{0.41}{0.91},\ 0 \right) \approx (0.5495,\ 0.4505,\ 0)
\]

#### 4.1.3 源码精读

**入口与短路分支**（[reasoning_from_scratch/ch04.py:L194-L196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L196)）：`top_p=None` 或 `top_p>=1.0` 时直接返回原张量——`top_p=1.0` 意味着「核 = 整个词表」，过滤是恒等操作，这一行短路既省计算又是文档（默认无过滤）。函数签名从这里也可看出它期望二维输入（下面所有操作都在 `dim=1` 即词表维上进行）。

**Step 4.1 排序**（[reasoning_from_scratch/ch04.py:L198-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L198-L199)）：`torch.sort(probas, dim=1, descending=True)` 同时返回排好序的值 `sorted_probas` 和「排序前每个值住在哪个词表位置」的索引 `sorted_idx`。后者是第 5 步回填的钥匙——概率必须回到它原来的词表 id 上，否则采样出的 token id 会张冠李戴。

**Step 4.2 累积和**（[reasoning_from_scratch/ch04.py:L201-L202](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L201-L202)）：`torch.cumsum` 沿词表维做前缀和，得到每个排序位上的累积质量。

**Step 4.3.1 前缀质量判据与兜底**（[reasoning_from_scratch/ch04.py:L204-L209](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L204-L209)）：

```python
prefix = cumprobas - sorted_probas   # cumulative mass before each token
keep = prefix < top_p
keep[:, 0] = True
```

三行是整个函数的灵魂，4.3 节专门拆解。此处先记结论：`keep[j]` 为真的条件是「排在我前面的 token 总质量还没攒够 top_p」，即「把我也算进来才凑得出 top_p」；`keep[:, 0] = True` 保证概率最高的 token 永远在核里。源码注释里给出的算例正是我们手推过的 `[0.5, 0.41, 0.09]`、`top_p=0.9` 保留前两个。

**Step 4.3.2 核外置零**（[reasoning_from_scratch/ch04.py:L211-L215](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L211-L215)）：`torch.where(keep, sorted_probas, 0)` 在**排序坐标系**里把核外概率清零——被清零的是「排序后第 j 大」的位置，还不是词表位置。

**Step 4.3.3 回填原顺序**（[reasoning_from_scratch/ch04.py:L216-L217](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L216-L217)）：

```python
filtered = torch.zeros_like(probas).scatter(1, sorted_idx, kept_sorted)
```

`scatter` 是 `gather` 的逆操作：对每个排序位 j，执行 `filtered[sorted_idx[j]] = kept_sorted[j]`，把值送回原始词表 id。排序→过滤→回填，词表位置自始至终没有丢失。

**Step 4.4 重归一化**（[reasoning_from_scratch/ch04.py:L219-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L219-L222)）：砍尾后每行和小于 1（上例为 0.91），除以行和恢复成合法分布——`torch.multinomial` 要求输入行和为 1。`keepdim=True` 让分母保持 `(batch, 1)` 形状从而正确广播（源码注释直言这对批量情形是必要的）；`clamp_min(1e-12)` 是防御性下限，防止全零分布除零——正常路径下 `keep[:, 0] = True` 已保证分母不小于最大概率，必然大于 0。

**测试如何锁定行为**（[tests/test_ch04.py:L82-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L82-L91)）：`[0.5, 0.4, 0.1]`、`top_p=0.6` 时断言输出为 `[0.5556, 0.4444, 0.0]`。手推验证：`prefix = (0, 0.5, 0.9)`，`keep = (T, T, F)`，保留质量 0.9，\( 0.5/0.9 \approx 0.5556 \)、\( 0.4/0.9 \approx 0.4444 \)，与断言一致；同测试还断言 `top_p=1.0` 时输出与输入逐位相等（走的是 L195 的短路）。第二个测试（[tests/test_ch04.py:L94-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L94-L107)）用一个两行、每行分布不同的批量输入验证：各行独立排序、独立截断、独立重归一化，两行和都回到 1。

#### 4.1.4 代码实践

**实践目标**：亲手验证 4.1.2 的手算结果，再探测两个极端值与熵的变化。以下为**示例代码**（非项目原有文件），保存为 `reasoning-from-scratch-tutorial/practice_top_p.py` 用 `python practice_top_p.py` 运行，只需 torch，CPU 即可：

1. **操作步骤**：

```python
# 示例代码：验证 top_p_filter 的截断、极端值与熵变化
import torch
from reasoning_from_scratch.ch04 import top_p_filter

probas = torch.tensor([[0.5, 0.41, 0.09]])

# (a) 手算验证：top_p=0.9 应保留前两个 token，重归一化为 [0.5495, 0.4505, 0]
print("top_p=0.9 :", top_p_filter(probas, top_p=0.9))

# (b) 极小 top_p：核收缩到只剩概率最大的 token，输出 [1.0, 0, 0]
print("top_p=0.001:", top_p_filter(probas, top_p=0.001))

# (c) top_p=1.0：走短路分支，原样返回
print("top_p=1.0 :", top_p_filter(probas, top_p=1.0))

# (d) 兜底验证：top_p=0 时 prefix[0]=0<0 不成立，全靠 keep[:,0]=True 保命
print("top_p=0.0 :", top_p_filter(probas, top_p=0.0))

# (e) 熵变化：过滤前后各算一次
def entropy(p):
    p = p.clamp_min(1e-12)
    return -(p * p.log()).sum(dim=1)

for tp in (0.5, 0.7, 0.9, 1.0):
    f = top_p_filter(probas, top_p=tp)
    print(f"top_p={tp}: 熵 {entropy(probas).item():.4f} -> {entropy(f).item():.4f}")
```

2. **需要观察的现象**：
   - (a) 的输出是否等于 `(0.5495, 0.4505, 0.0)`（浮点上 `torch.allclose` 级别一致）。
   - (b) 是否输出 `(1.0, 0.0, 0.0)`——分布坍缩到 argmax，等价于贪心。
   - (c) 是否与输入逐位相同。
   - (d) 是否**没有**报错且仍输出 `(1.0, 0.0, 0.0)`；可临时注释掉源码 L209 想象会发生什么（全零分布 → 重归一化除以 `1e-12` 仍全零 → `multinomial` 报 invalid distribution 错误）——不必真改源码，想清楚即可。
   - (e) 熵随 `top_p` 减小而单调下降。
3. **预期结果**：手算部分——原始分布熵 \( H = -(0.5\ln 0.5 + 0.41\ln 0.41 + 0.09\ln 0.09) \approx 0.929 \) nat；`top_p=0.9` 过滤后 \( H \approx -(0.5495\ln 0.5495 + 0.4505\ln 0.4505) \approx 0.689 \) nat。程序输出与这些值一致。
4. 运行结果与硬件相关时不显著（纯 CPU 张量运算），但具体打印值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：排序后概率 `(0.3, 0.3, 0.3, 0.1)`，`top_p=0.6`，哪些 token 被保留？输出分布是什么？

**答案**：`cumprobas = (0.3, 0.6, 0.9, 1.0)`，`prefix = (0, 0.3, 0.6, 0.9)`，`keep = prefix < 0.6 = (T, T, F, F)`。注意 `prefix[2] = 0.6` 不小于 0.6（严格小于），第三个 token 被排除——保留质量恰好等于 0.6。重归一化输出 `(0.5, 0.5, 0, 0)`。

**练习 2**：把 Step 4.3.1 的判据换成 `keep = cumprobas < top_p`，对 `(0.5, 0.41, 0.09)`、`top_p=0.9` 会保留谁？这有什么问题？

**答案**：`cumprobas = (0.5, 0.91, 1.0)`，判据给出 `(T, F, F)`，只保留概率最大的 token，保留质量 0.5 远未达到 0.9。问题在于它丢弃了「跨过阈值的那个 token」：判据要求「连我在内的累积质量都还没到 top_p」才保留我，实现的是「保留质量严格小于 top_p」，比核采样定义激进得多，会让 top-p 在尖分布上退化成近似贪心（详见 4.3 节）。

**练习 3**：`top_p_filter` 为什么必须重归一化？不重归一化直接喂给 `torch.multinomial` 会怎样？

**答案**：砍尾后行和小于 1（例中为 0.91）。`torch.multinomial` 要求每行概率和为 1（带容差）；更重要的是，不归一化会把 9% 的概率质量凭空蒸发，剩余 token 的相对比例虽不变，但采样在数学上不再是合法分布，行为未定义/报错。重归一化把 0.91 的质量按原比例放大回 1，核内两个 token 的相对比例 `0.5 : 0.41` 保持不变。

### 4.2 generate_text_top_p_stream_cache：温度 × top-p 组合

#### 4.2.1 概念说明

有了过滤器，下一步是把它接进生成循环。`generate_text_top_p_stream_cache` 就是 u5-l1 的 `generate_text_temp_stream_cache` 加了一行：在 softmax 之后、multinomial 之前插入 top-p 过滤。

组合的直觉：

- **温度在前（logits 层面）**：先把分布塑形——T 大则摊平（多样性），T 小则聚拢（一致性）。
- **top-p 在后（概率层面）**：无论温度把分布摊得多平，长尾一律清零，只从「核」里采样。

两者搭配正是实践中最常见的配置：较高的温度提供多样性，top-p 兜底防止低质长尾 token 漏网。仓库里 `self_consistency_vote` 的默认参数就是 `temperature=0.8, top_p=0.9`（见 [reasoning_from_scratch/ch04.py:L271-L272](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L271-L272)），第 5 讲的自一致性投票将直接消费这一组合。

#### 4.2.2 核心流程

```
generate_text_top_p_stream_cache(model, token_ids, max_new_tokens,
                                 eos_token_id, temperature=0., top_p=None):
  初始化：model.eval()、新建 KVCache、清零位置游标          # 与 u3-l2 相同
  prefill：整段 prompt 前向，取末位 logits
  循环 max_new_tokens 步：
    T=0/None → argmax（贪心；注意：top_p 完全不参与！）
    T>0：
      logits = out / temperature          # Step 3.2  温度缩放
      probas = softmax(logits)            # Step 3.3  概率化
      probas = top_p_filter(probas, top_p)# Step 4    （新增）砍尾 + 重归一化
      next   = multinomial(probas)        # Step 3.4  采样（CPU 上执行再搬回）
    命中 EOS → break（EOS 不产出）
    yield next_token
    非最后一步 → 对 next_token 做 decode 前向
```

顺序不可颠倒的原因：温度作用在 logits 上、top-p 定义在概率上，`softmax` 是两个世界之间唯一的桥，所以流水线只能是「缩放 logits → 概率化 → 过滤概率 → 采样」。

#### 4.2.3 源码精读

**函数签名与默认值**（[reasoning_from_scratch/ch04.py:L225-L233](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L225-L233)）：与 `generate_text_temp_stream_cache` 相比只多了一个 `top_p=None` 参数。**注意默认 `temperature=0.`**——直接调用而不传温度时走贪心分支，`top_p` 形同虚设（见下）。缓存初始化（`model.eval()`、新建 `KVCache`、`reset_kv_cache()`）与 u3-l2 讲过的骨架逐行相同。

**贪心分支绕过过滤器**（[reasoning_from_scratch/ch04.py:L244-L245](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L244-L245)）：`temperature` 为 `None` 或 `0.0` 时直接 `argmax`。这是本函数最值得划线的细节：**top-p 只在 `temperature > 0` 时生效**。逻辑上也自洽——argmax 选出的必然是概率最大的 token，它永远在核内（`keep[:, 0] = True`），过滤与否不改变结果，干脆跳过。但做实验时若忘了设温度，会误以为 top-p「不起作用」。

**采样三步 + 新增一步**（[reasoning_from_scratch/ch04.py:L248-L259](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L248-L259)）：与 u5-l1 精读过的 `generate_text_temp_stream_cache`（[reasoning_from_scratch/ch04.py:L174-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L174-L181)）逐行对照，唯一的新增是 L255 的 `probas = top_p_filter(probas, top_p)`，插在 softmax（L252）与 `multinomial`（L258）之间。采样在 CPU 上执行（`probas.cpu()`）再把结果搬回原设备——与 u3-l2/u5-l1 观察到的约定一致。`top_p=None` 时过滤器走短路分支原样返回，函数行为退化为纯温度采样——一个函数覆盖三种模式（贪心 / 纯温度 / 温度+top-p）。

**EOS 提前停止与收尾**（[reasoning_from_scratch/ch04.py:L261-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L261-L267)）：EOS 检查在 yield 之前（EOS 本身不产出），`step + 1 < max_new_tokens` 跳过最后一次前向——均为 u3-l2 已建立的模式，不再展开。

**测试中的间接验证**（[tests/test_ch04.py:L110-L126](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L110-L126)）：虽然没有专门针对 `generate_text_top_p_stream_cache` 的测试，但温度版与 top-p 版共享同一骨架；该测试用 `DummyModelCache`（logits 恒定、argmax 必中 EOS）验证「首个 token 即 EOS → 产出为空、`eval`/`reset_kv_cache` 均被调用」，这一行为对 top-p 版同样成立。

#### 4.2.4 代码实践

**实践目标**：体感对比「纯高温采样」与「高温 + top-p」的输出质量差异，并验证 `temperature=0` 时 top-p 被绕过。需要先按 u1-l2/u1-l3 下载 0.6B base 权重（CPU 可跑，慢；GPU 更佳）。

1. **操作步骤**（示例代码，基于 u1-l2 的模型加载套路）：

```python
# 示例代码：温度与 top-p 的组合对比
import time
from reasoning_from_scratch.ch04 import generate_text_top_p_stream_cache
from reasoning_from_scratch.qwen3 import Qwen3Tokenizer, download_qwen3_small

model_path = download_qwen3_small(kind="base")   # 已下载则秒回
tokenizer = Qwen3Tokenizer(model_path, model_path)
# ……按 ch02/05_use_model/generate_simple.py 的方式加载 model 并 load_weights、to(device)

prompt = "The capital of France is"
for tp in (None, 0.9):
    torch.manual_seed(123)
    out = model  # ← 换成你的模型对象
    text = ""
    t0 = time.time()
    ids = torch.tensor(tokenizer.encode(prompt), device=device).unsqueeze(0)
    for tok in generate_text_top_p_stream_cache(
            model, ids, max_new_tokens=30,
            eos_token_id=tokenizer.eos_token_id,
            temperature=1.5, top_p=tp):
        text += tokenizer.decode(tok.tolist())
    print(f"top_p={tp}: {text!r}  ({time.time()-t0:.1f}s)")
```

2. **需要观察的现象**：同一 `seed` 下，`top_p=None`（纯 T=1.5）的输出更可能出现不连贯片段；`top_p=0.9` 在保留多样性的同时明显更通顺。再跑一组 `temperature=0.0, top_p=0.9` 与 `temperature=0.0, top_p=None`，两者输出应**逐字相同**——证明贪心分支确实无视 top-p。
3. **预期结果**：定性结论如上；具体文本因随机种子与硬件浮点而异，**待本地验证**。
4. 无 GPU 时建议把 `max_new_tokens` 降到 15–20 控制时长。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `top_p_filter` 被放在 softmax 之后而不是之前？

**答案**：`top_p_filter` 的输入是概率——它要做累积和、与 `top_p` 比较质量、重归一化，这些操作都定义在概率单纯形上；而温度缩放作用于 logits。流水线「除以 T → softmax → 过滤 → 采样」是两种操作的天然分工：温度重塑整个分布的形状，top-p 在已成形的概率上砍尾。

**练习 2**：调用 `generate_text_top_p_stream_cache(model, ids, 20, temperature=0.0, top_p=0.1)`，top-p 起作用了吗？输出和 `generate_text_basic_stream_cache`（u3-l2）有何关系？

**答案**：没起作用。`temperature=0.0` 走 argmax 贪心分支（L244-L245），`top_p_filter` 根本不被调用。输出与贪心流式生成完全一致（逐 token 相同）——argmax 选中的 token 永远是排序后第 0 位，任何合法的 top-p 核都包含它。

**练习 3**：`self_consistency_vote` 里对同一条 prompt 采样 10 次（`temperature=0.8, top_p=0.9`），为什么每次结果可以不同？随机性来自哪一行？

**答案**：随机性来自 `torch.multinomial(probas.cpu(), num_samples=1)`（L258）：过滤后的分布仍有多个非零概率的 token（核内），multinomial 按概率抽取，每次调用独立。top-p 并不消除随机性，它只是把随机性**限制在高质量核内**——这正是自一致性投票（u5-l4）能采出「和而不同」的多条推理路径、再投票的前提。

### 4.3 边界语义：`prefix < top_p` 与 `keep[:, 0] = True`

#### 4.3.1 概念说明

核采样的原始定义是：**保留最小的 token 集合，使其累积质量 ≥ top_p**。「最小集合」意味着：一个 token 属于核，当且仅当**在它之前的质量还没攒够 top_p**——一旦攒够了，后面的 token 就都多余了。

源码把这句话直接翻译成了张量判据：

\[
\text{keep}[j] \iff \underbrace{\sum_{i<j} p_{(i)}}_{\text{prefix}[j]} < \text{top\_p}
\]

其中 \( p_{(i)} \) 是降序排列后的第 i 大概率。而 `cumprobas < top_p` 翻译的是另一句话：「连我算在内都没攒够」——它实现的是「保留质量**严格小于** top_p」，会在阈值被跨过的那一刻过早收手，丢掉跨阈值的那个 token。

至于 `keep[:, 0] = True`：`prefix[0] = 0`，只要 `top_p > 0`，`0 < top_p` 自然成立，这一行看似多余。它的真正职责是兜住**非正的 top_p**（例如误传 `top_p=0`）：此时所有 `prefix < 0` 全为 False，若没有这行强制保留，分布会全零，重归一化除以 `clamp_min(1e-12)` 后仍全零，`multinomial` 将报「invalid distribution」错。一行兜底把「非法参数」降解为「退化为贪心」的安全行为。

#### 4.3.2 核心流程

三种判据在同一算例上的对比（`p = (0.5, 0.41, 0.09)`）：

| 判据 | keep | 保留质量 | 问题 |
|---|---|---|---|
| `prefix < top_p`（源码，top_p=0.9） | (T, T, F) | **0.91 ≥ 0.9** ✓ | 无——恰好首次达到 0.9 |
| `cumprobas < top_p`（错误版） | (T, F, F) | 0.5 < 0.9 ✗ | 丢掉跨阈值 token，过度截断 |
| `cumprobas <= top_p`（另一错误版） | (T, F, F) | 0.5 < 0.9 ✗ | 本算例与 `<` 版同样过度截断；但换一组分布（如 `(0.5, 0.4, 0.1)`、`top_p=0.9`，cumprobas=(0.5, 0.9, 1.0)）它又碰巧给出 (T,T,F) 与源码一致——行为随「累积是否恰好等于 top_p」漂移，不可靠，见 4.3.5 练习 2 |

极端 `top_p` 的行为边界：

| top_p | 行为 | 对应源码 |
|---|---|---|
| `None` / `≥ 1.0` | 短路原样返回（恒等过滤） | L195-L196 |
| `0.001`（极小正数） | 核收缩到头名 token，输出退化为 one-hot `(1, 0, …, 0)`，采样等价贪心 | L207-L209 + L222 |
| `0` / 负数（非法） | `keep[:, 0]=True` 兜底，同样退化为 one-hot，不报错 | L209 |

#### 4.3.3 源码精读

**判据与兜底三行**（[reasoning_from_scratch/ch04.py:L204-L209](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L204-L209)）：`prefix = cumprobas - sorted_probas` 用「累积和减去自身」优雅地算出「排在我之前的质量和」，避免再写一次累积；注释 "Example: [0.5, 0.41, 0.09] with top_p=0.9 should keep the first two tokens" 明确记录了边界意图——0.91 越过 0.9 的那个 token（0.41）被保留。`keep[:, 0] = True` 的注释 "(fallback for very small/non-positive top_p)" 直言其防御使命。

**严格小于的必要性**：若写成 `prefix <= top_p`，算例 `prefix = (0, 0.5, 0.91)`、`top_p=0.9` 下结果不变（0.91 > 0.9），但在「恰好等于」的边界上（如 4.1.5 练习 1 的 `prefix[2] = 0.6`、`top_p=0.6`）会多保留一个 token。严格 `<` 让「保留质量恰好等于 top_p」时果断收手，集合真正「最小」。

**下游对边界的依赖**：第 6 章 GRPO 的 `sample_response` 原样复用本函数（[reasoning_from_scratch/ch06.py:L78](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L78)，导入见 L9）——rollout 采样同样依赖「核非空」这一兜底保证，否则一次误传参数就会让整个训练循环崩溃在 `multinomial` 上。

#### 4.3.4 代码实践

**实践目标**：用实验对比「正确判据」与「两种错误判据」的保留集合差异，直观理解边界含义。示例代码：

1. **操作步骤**：

```python
# 示例代码：三种判据的边界对比
import torch
from reasoning_from_scratch.ch04 import top_p_filter

def naive_filter(probas, top_p, mode):
    """复刻 top_p_filter，但分别用两种可疑判据。示例代码。"""
    sp, si = torch.sort(probas, dim=1, descending=True)
    cum = torch.cumsum(sp, dim=1)
    if mode == "prefix":       # 源码版
        keep = (cum - sp) < top_p
    elif mode == "cum_lt":     # 错误版：< 
        keep = cum < top_p
    keep[:, 0] = True
    kept = torch.where(keep, sp, torch.zeros_like(sp))
    out = torch.zeros_like(probas).scatter(1, si, kept)
    return out / out.sum(dim=1, keepdim=True).clamp_min(1e-12)

cases = [
    ([[0.5, 0.41, 0.09]], 0.9),
    ([[0.5, 0.4, 0.1]],   0.6),   # 测试用例：保留质量 0.9，留前两个
    ([[0.3, 0.3, 0.3, 0.1]], 0.6),  # 恰好等于边界：prefix[2]=0.6
]
for p, tp in cases:
    p = torch.tensor(p)
    ref = top_p_filter(p, top_p=tp)
    bad = naive_filter(p, tp, "cum_lt")
    kept_ref = (ref > 0).sum().item()
    kept_bad = (bad > 0).sum().item()
    print(f"top_p={tp}: 源码保留 {kept_ref} 个, 错判据保留 {kept_bad} 个")
```

2. **需要观察的现象**：三个算例中错判据都比源码**少保留一个** token——丢掉的正是「跨过阈值」的那个：算例一、二里源码保留到累积质量越过 0.9/0.6 的 token，错判据在它前一位就停手；算例三里边界恰好取到精确值（`prefix[2] = 0.6` 恰等于 top_p），源码因严格 `<` 在此收手保留 2 个，错判据在 `cum[1] = 0.6` 处同样为 False，只保留 1 个。
3. **预期结果**：算例一：源码保留 2 个（0.5495/0.4505）、错判据保留 1 个（1.0）；算例二：源码保留 2 个（0.5556/0.4444，与 tests/test_ch04.py:L82-L91 的断言一致）、错判据保留 1 个；算例三：源码保留 2 个（0.5/0.5，保留质量恰为 0.6）、错判据保留 1 个。具体打印**待本地验证**。
4. 若想再探 `keep[:, 0]` 兜底，可在 `naive_filter` 里去掉那一行、传 `top_p=0.0`，观察输出全零——这就是兜底存在的理由。

#### 4.3.5 小练习与答案

**练习 1**：用一句话向同学解释 `prefix < top_p` 与 `cumprobas < top_p` 的区别。

**答案**：前者问「**轮到我之前**质量攒够 top_p 了吗？」——没攒够我就属于核，因此恰好保留「首次达到 top_p」的最小集合；后者问「**连我算上**攒够了吗？」——会在跨过阈值前一步提前收手，保留质量严格小于 top_p，过度截断。

**练习 2**：判据用 `cumprobas <= top_p`（把 `<` 换成 `<=`）在算例 `(0.5, 0.41, 0.09)`、`top_p=0.9` 下保留谁？在什么分布下它与源码版结果不同？

**答案**：`cumprobas = (0.5, 0.91, 1.0)`，`<= 0.9` 给出 `(T, F, F)`，只保留头名——比源码激进。关键在于它**不稳定**：源码判据 `prefix[j] < top_p` 等价于「前一位的累积和 `cum[j-1] < top_p`」，语义在任何输入下都一致；而 `cumprobas <= top_p` 只在「某位置累积**恰好等于** top_p」时才与源码碰巧重合（如 `(0.5, 0.4, 0.1)`、`top_p=0.9`：cumprobas=(0.5, 0.9, 1.0)，`<=` 给出 (T,T,F)，恰与源码相同），其余情形照旧丢掉跨阈值 token。边界行为随输入漂移的判据无法依赖，这正是源码选择把「严格小于」放在 prefix 上的理由。数值实验见 4.3.4 算例三（`(0.3, 0.3, 0.3, 0.1)`、`top_p=0.6`，两版一留二、一留一）。

**练习 3**：`denom.clamp_min(1e-12)`（L220）在什么情况下才真正派上用场？既然有 `keep[:, 0] = True`，它是不是死代码？

**答案**：仅在「所有概率被清零」时才有意义，而 `keep[:, 0] = True` 加上 softmax 输出恒正已经排除了这种情况（分母 ≥ 最大概率 > 0），所以对本函数的合法调用而言它是防御性冗余，不是死代码——它是纵深防御的一层：将来若有人改判据、传 NaN/全零张量或复用此函数到非 softmax 输入上，clamp 把「除零崩溃」降解为「输出极端值」，配合测试更容易暴露问题而非静默炸掉。

## 5. 综合实践

**任务：写一个「top-p 扫描器」，量化核大小与熵随 top_p 的变化。**

不带模型、纯 CPU 张量运算即可完成，把本讲三个模块串起来：

1. 构造三个「性格不同」的分布（示例代码）：
   - 尖峰型：`[0.9, 0.05, 0.03, 0.02]`（下一词几乎确定）
   - 平坦型：`[0.3, 0.28, 0.25, 0.17]`（下一步很开放）
   - 现实型：`[0.5, 0.41, 0.09]`（本讲算例）
2. 对每个分布，让 `top_p` 从 0.1 扫到 1.0（步长 0.1），逐档调用 `top_p_filter`，记录三列：**保留 token 数**、**过滤前保留质量**（重归一化之前的行和）、**过滤后分布的熵**。
3. 把结果整理成表，回答三个问题：
   - 同一 `top_p` 下，尖峰型与平坦型保留的 token 数差多少？（top-p 的自适应性）
   - 熵随 `top_p` 下降的速度，尖峰型和平坦型哪个更陡？为什么？
   - `top_p=1.0` 这一档走的是哪条分支（对照 [reasoning_from_scratch/ch04.py:L195-L196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L195-L196)），熵等于多少？
4. **预期结果**（手算可先行推出，程序验证待本地进行）：尖峰型在 `top_p=0.9` 时只保留 1 个 token（第 2 个的 prefix=0.9 不小于 0.9，熵为 0）；平坦型在 `top_p=0.9` 时保留**全部 4 个**（第 4 个 token 的 prefix=0.83 < 0.9，仍属核，熵不变）；现实型如正文，保留 2 个、熵约 0.689 nat——三种「性格」在同一 `top_p` 下的核大小截然不同，这正是 top-p 优于固定个数 top-k 的地方。

进阶（可选）：仿照 `count_samples`（[reasoning_from_scratch/ch04.py:L132-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L132-L147)）对过滤后的分布各采样 1000 次，用频率分布复算熵，验证「采样频率 ≈ 概率」在核变小后收敛更快还是更慢。

## 6. 本讲小结

- `top_p_filter` 是一条五步流水线：**排序 → 累积和 → 前缀质量判据选核 → 置零并回填原词表位置 → 重归一化**，作用于 softmax 之后的概率，输出仍是合法分布。
- 边界判据 `prefix < top_p` 精确翻译了核采样定义「累积质量首次达到 top_p 的最小集合」；写成 `cumprobas < top_p` 会丢掉跨阈值的 token，导致过度截断（算例：0.91 vs 0.5）。
- `keep[:, 0] = True` 是极端/非法 `top_p`（如 0 或负数）下的兜底，保证核非空，把崩溃降解为「退化为贪心」。
- `top_p=1.0` 或 `None` 走短路分支原样返回；极小 `top_p` 使分布坍缩为 one-hot，等价 argmax。
- `generate_text_top_p_stream_cache` 在温度采样三步（缩放→softmax→multinomial）中间插入过滤这一步，形成「logits 层调温、概率层砍尾」的分工；**`temperature=0` 时 top-p 完全被绕过**，这是实验中最常见的坑。
- 温度 + top-p 的组合（如 0.8 + 0.9）是仓库的标准采样配置，被 `self_consistency_vote` 消费，并被第 6 章 `sample_response` 原样复用——本讲的过滤器是全书推理时扩展与 RL 训练的公共零件。

## 7. 下一步学习建议

- **u5-l3（思维链提示实验）**：换一个不采样的推理时扩展手段——只改提示模板、权重与采样参数都不动，对比两者「花钱买准确率」的方式有何不同。
- **u5-l4（自一致性投票）**：看 `generate_text_top_p_stream_cache`（`temperature=0.8, top_p=0.9`）采出的多条推理路径如何经 `extract_final_candidate`（u4-l2）提取短答案、再 `Counter` 投票定案——本讲的过滤器正是那里多样性与质量兼得的来源。
- **源码延伸**：提前翻一眼 [reasoning_from_scratch/ch06.py:L78](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L78)，确认 GRPO 的 rollout 采样循环与本讲 L252-L258 几乎逐行同构，为单元七热身。
- **理论延伸**：读 Holtzman 等人 2020 年的 *The Curious Case of Neural Text Degeneration*（本书第 4 章的参考之一），看人类文本的「核质量」统计如何支撑「只在核内采样」这一设计。
