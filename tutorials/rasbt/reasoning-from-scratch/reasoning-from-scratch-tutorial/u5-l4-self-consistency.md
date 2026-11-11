# u5-l4 自一致性投票

## 1. 本讲目标

学完本讲，你应该能够：

1. 描述自一致性（self-consistency）的三阶段流水线：**多次温度采样 → 提取短答案 → 多数投票**，并说清它与单次采样、Best-of-N 打分的本质区别。
2. 精读 `ch04.py` 中的 `self_consistency_vote` 与 `generate_text_stream_concat_flex`，理解「可插拔生成引擎」的装配方式。
3. 理解 `Counter` 计票的实现细节，以及**平票时 `final_answer=None`** 这一「库不擅自决策、交给调用者兜底」的设计。
4. 对比 `self_consistency_math500.py`（逐条采样，带 early stop）与 `self_consistency_math500_batched.py`（把 `num_samples` 个采样塞进一个 batch）两条工程路线的差异与取舍。

## 2. 前置知识

本讲站在前面几讲的肩膀上，先用两段话把需要的直觉补齐。

**随机采样为什么需要「投票」来救。** 在 u5-l1、u5-l2 中我们看到：温度采样 + top-p 过滤能产生多样化的回答，但单次随机采样未必比贪心解码好——对 0.6B 的 base 模型，CoT 贪心解码能到 40.6%，而「CoT + 单次采样」反而掉到 33.4%。问题不在采样本身，而在**单次采样的方差**：一次抽签抽到哪条推理路径全凭运气。自一致性的想法非常朴素：既然正确答案往往只有一两个、而错误答案各不相同，那就**采样 n 次、对最终短答案计票、取众数**。正确的推理路径会彼此收敛到同一个答案，错误的路径各自发散，众数就把「被多条路径反复支持」的答案放大出来。

**投票的理论上限。** 假设每条采样链以概率 \( p \) 独立答对，抽 \( n \) 条链做多数投票，则投票答对的概率近似为

\[
P(\text{多数正确}) \;=\; \sum_{k > n/2} \binom{n}{k}\, p^{k}(1-p)^{n-k}
\]

当 \( p > 0.5 \) 时这个概率随 \( n \) 增大而趋近 1；但若 \( p \leq 0.5 \)（模型本身太弱或 prompt 太差），投票救不回来，甚至放大系统性错误。还要注意「独立」假设在实践中并不成立——n 条链来自同一个模型、同一个 prompt，错误是相关的。这些理想化假设恰好能解释实验数据中的非单调现象（见 4.1.3 的表格：无 CoT 时 n=3→29.6%、n=5→27.8%、n=10→31.6%，并非单调上升）。

此外，本讲直接复用三件旧资产（不再重复展开）：

- **温度缩放 + top-p 过滤 + multinomial 采样**（u5-l1 / u5-l2）：`generate_text_top_p_stream_cache` 这条「缩放→softmax→过滤→采样」流水线；
- **答案提取**（u4-l2）：`extract_final_candidate(text, fallback="number_then_full")` 从长回答里抠出最后的短答案；
- **MATH-500 评测链路**（u4-l1 / u4-l4）：`render_prompt` 渲染、`grade_answer` 判分、jsonl 逐行落盘。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `reasoning_from_scratch/ch04.py` | 库版本：`generate_text_stream_concat_flex`（可插拔生成引擎）与 `self_consistency_vote`（三阶段投票），本讲主战场 |
| `ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py` | 独立实验脚本（逐条采样版）：在库版本上**复制改造**出 `early_stop` 提前停止，并串起 MATH-500 评测循环 |
| `ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py` | 批处理实验脚本：新增 `generate_text_top_p_batched`，把 n 个采样并行塞进一个 batch |
| `ch04/02_math500-inference-scaling-scripts/README.md` | 实验结果表（第 4–12 行）与复现命令 |
| `reasoning_from_scratch/ch03.py` | 提供 `extract_final_candidate`（u4-l2 已精读，本讲只作为被调用方引用） |
| `tests/test_ch04.py` | 对 `generate_text_stream_concat_flex` 与 `self_consistency_vote` 的单元测试，本讲实践会借鉴它的 monkeypatch 手法 |

## 4. 核心概念与源码讲解

### 4.1 自一致性投票：思想与三阶段流水线

#### 4.1.1 概念说明

自一致性（self-consistency）是最经典的推理时扩展方法之一：**不改动任何权重**，只是把同一个问题「独立地」回答 n 次，再对每次的**最终短答案**（而不是整段推理过程）做多数投票。它成立的前提有两个：

1. 采样要有多样性——所以必须配合温度/top-p 随机采样，贪心解码每次结果相同，投票没有意义；
2. 每次都能提取出一个可比较的短答案——这靠 u4-l2 的 `\boxed{}` 提取契约与数字回退保证。

与第 5 章（u6）将讲的 Best-of-N 区别在于**择优准则**：自一致性用「出现次数最多」当准则，不需要打分模型；Best-of-N 用一个启发式分数选最优。前者相信「群众的共识」，后者相信「评分器的眼光」。

#### 4.1.2 核心流程

`self_consistency_vote` 的三阶段可以用下面的伪代码概括：

```text
输入: prompt, n=num_samples, 温度 T, top_p, 每题最多生成 max_new_tokens

阶段一 采样:  for i in 0..n-1:
              重置随机种子 seed+i+1 (可选)
              answer_i = 生成引擎(prompt, T, top_p)   # 每次一条完整推理链
阶段二 提取:  short_i = extract_final_candidate(answer_i)
阶段三 计票:  counts = Counter(short_0..short_{n-1})
              top_freq = 最大票数
              majority_winners = [所有票数 == top_freq 的答案]
              final_answer = 唯一赢家 ? 赢家 : None   # 平票 → None
输出: 字典 {full_answers, short_answers, counts, groups, majority_winners, final_answer}
```

注意三阶段的**分工边界**：采样引擎只负责「产出一条完整回答」，提取器只负责「把回答压成一个可比较的字符串」，投票只负责「在 n 个字符串里找众数」。三者通过纯函数式的数据传递（列表 → 列表 → Counter）衔接，任何一环都可以单独替换。

#### 4.1.3 源码精读

先看库函数的签名与默认值，位于 [reasoning_from_scratch/ch04.py:L270-L275](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L270-L275)：`num_samples=10, temperature=0.8, top_p=0.9, max_new_tokens=2048` 是作者推荐的采样组合（温度和 top-p 正是 u5-l2 讲过的 `top_p_filter` 的默认搭档）；`seed=None` 默认不可复现，传入整数则逐样本重置种子。

采样循环在 [reasoning_from_scratch/ch04.py:L277-L296](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L277-L296)：每个样本先用 `torch.manual_seed(seed + i + 1)` 重置全局随机流（第 i 个样本用种子 `seed+i+1`），再调用 `generate_text_stream_concat_flex` 注入 top-p 采样引擎生成一条完整回答，最后用 `extract_final_candidate(answer, fallback="number_then_full")` 压出短答案。逐样本重置种子的好处是**每个样本的随机流互相独立、且整次实验可复现**——如果对所有样本设同一个种子，同样的 prompt 会采样出完全相同的回答，投票退化成全票一票。

实验效果可以对照 README 的结果表（500 题全量、DGX Spark GPU），见 [ch04/02_math500-inference-scaling-scripts/README.md:L83-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L83-L98)。摘出与本讲直接相关的几行：

| 方法（base 模型） | 准确率 | 耗时 |
| --- | --- | --- |
| CoT 贪心（第 3 行） | 40.6% | 84.5 min |
| Top-p 采样 n=1 + CoT（第 8 行） | 33.4% | 129.2 min |
| 自一致性 n=3 + CoT（第 9 行） | 42.2% | 211.6 min |
| 自一致性 n=5 + CoT（第 10 行） | 48.0% | 452.9 min |
| 自一致性 n=10 + CoT（第 11 行） | 52.0% | 862.6 min |

两个值得咀嚼的观察：① 单次采样（33.4%）确实低于贪心（40.6%），而投票把它一路拉回到 52.0%——这就是「用计算量换准确率」的推理时扩展；② 耗时近似随 n 线性增长（每条采样链都要完整生成一遍），n=10 时已超过 14 小时，这也是 4.4 节 early stop 与批处理两个工程优化的动机。

#### 4.1.4 代码实践

**实践目标**：不跑模型，先读懂 README 的实验账本，量化「投票的性价比」。

1. 打开 [ch04/02_math500-inference-scaling-scripts/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md)，把表格第 8–11 行抄下来。
2. 计算相邻两档（n=1→3→5→10）的「准确率增量」与「耗时增量」：
   - n=1→3：+8.8 个百分点，+82.4 min；
   - n=3→5：+5.8 个百分点，+241.3 min；
   - n=5→10：+4.0 个百分点，+409.7 min。
3. 观察边际收益递减、边际成本递增，回答：如果你的预算只够跑 n=3 或 n=5 其中一档，你会选哪档、依据是什么？
4. 再对比无 CoT 的第 5–7 行（29.6% / 27.8% / 31.6%），解释为什么这一组对 n 不敏感甚至非单调（提示：回到 4.1.2 的二项分布公式，无 CoT 时单链正确率 \( p \) 太低）。

**预期结果**：你会得出「有 CoT 时投票收益显著、无 CoT 时投票收益微弱且抖动」的结论——短答案的众数只有在「多数链经常答对」时才有意义。本实践为纯数据观察，无需运行任何代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么自一致性必须配合温度采样，而不能用贪心解码？

**答案**：贪心解码每步取 `argmax`，对同一个 prompt 的输出是确定性的——n 次生成会得到 n 份完全相同的回答，`Counter` 里只有一个键且票数为 n，投票退化为「单次贪心结果」，白白付出 n 倍计算。多样性来自 u5-l1 的 `multinomial` 随机采样（`generate_text_top_p_stream_cache` 的 `temperature>0` 分支）。

**练习 2**：投票对象为什么是 `extract_final_candidate` 提取的短答案，而不是完整回答文本？

**答案**：完整回答包含推理过程，措辞、格式、顺序几乎必然不同，直接对全文计数会得到「每个答案 1 票」的全面平票。短答案把「\( \boxed{42} \)」「The answer is 42」「…so x=42」这些不同表述都压缩成可比较的 `"42"`，正确的共识才能积累票数。

**练习 3**：理论上想让多数投票的胜率超过单链，对单链正确率 \( p \) 有什么要求？

**答案**：需要 \( p > 0.5 \)。由多数投票的二项分布公式，\( p>0.5 \) 时胜率随 \( n \) 上升趋于 1；\( p=0.5 \) 时持平；\( p<0.5 \) 时投票反而放大错误。这解释了为什么弱模型/差 prompt 上加采样预算收效甚微。

### 4.2 generate_text_stream_concat_flex：可插拔的生成引擎

#### 4.2.1 概念说明

`self_consistency_vote` 每个样本都需要「给一个 prompt、拿回一段完整文本」。但第 2~4 章已有的生成函数族（basic / cache / stream / stream_cache、temp、top_p）接收的是 `token_ids` 张量、产出的是逐 token 的生成器。`generate_text_stream_concat_flex` 就是中间的**适配器**：它把「prompt 字符串进 → 完整回答文本出」这个高层接口，桥接到任意一个底层流式生成函数上。名字里的三个词恰好是它的三个特征：**stream**（消费逐 token 的生成器）、**concat**（把 token 拼成整段文本）、**flex**（生成函数可插拔注入）。

这是策略模式（依赖注入）的教学典范：上层 `self_consistency_vote` 只依赖「一个能生成文本的引擎」这个抽象，不关心引擎内部是贪心还是 top-p；第 5 章的自我精炼循环（u6-l3）也复用了同一个适配器。

#### 4.2.2 核心流程

```text
generate_text_stream_concat_flex(model, tokenizer, prompt, ..., generate_func=None, **generate_kwargs)
  1. generate_func 为 None → 默认用贪心引擎 generate_text_basic_stream_cache
  2. tokenizer.encode(prompt) → 包装成 (1, seq_len) 的 input_ids
  3. 迭代 generate_func(model=..., token_ids=input_ids, max_new_tokens=..., eos_token_id=..., **generate_kwargs)
       每拿到一个 token：squeeze → 记入 generated_ids → (可选) verbose 实时打印
  4. 返回 tokenizer.decode(generated_ids)   # 只含生成部分，不含 prompt
```

关键点是 `**generate_kwargs` 的**透传**：`temperature=0.8, top_p=0.9` 这些参数在 flex 里完全不出现，它们原样穿过 `**generate_kwargs` 落到具体的 `generate_func` 里。flex 对参数语义保持无知，因此换引擎时不需要改适配器。

#### 4.2.3 源码精读

函数全文见 [reasoning_from_scratch/ch04.py:L13-L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L13-L44)。逐段说明：

- [reasoning_from_scratch/ch04.py:L20-L21](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L20-L21)：默认引擎兜底为 `generate_text_basic_stream_cache`（u3-l2 精读过的贪心 + KV 缓存版，签名见 [reasoning_from_scratch/ch02.py:L117-L122](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L117-L122)）——不传引擎时它就是第 2 章的普通生成器。
- [reasoning_from_scratch/ch04.py:L23-L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L23-L25)：prompt 编码为 token id 并 `unsqueeze(0)` 补上 batch 维，这是模型前向的约定形状。
- [reasoning_from_scratch/ch04.py:L28-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L28-L36)：`for token in generate_func(...)` 消费生成器；每个 token 记入 `generated_ids`，`verbose=True` 时逐 token 打印（u3-l2 讲过的惰性求值在这里被「消费」）。
- [reasoning_from_scratch/ch04.py:L32-L33](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L32-L33)：`eos_token_id=tokenizer.eos_token_id` 与 `**generate_kwargs` 一起传给引擎——EOS 提前停止的逻辑完全由引擎负责。
- [reasoning_from_scratch/ch04.py:L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L44)：只 decode `generated_ids`，prompt 部分天然被排除在返回文本之外。

而在 `self_consistency_vote` 一侧，注入点在 [reasoning_from_scratch/ch04.py:L282-L287](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L282-L287)：`generate_func=generate_text_top_p_stream_cache, temperature=temperature, top_p=top_p`——引擎换成 u5-l2 的 top-p 采样版（[reasoning_from_scratch/ch04.py:L225-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L225-L267)），`temperature`/`top_p` 作为透传参数落入其中。整个第 4 章的实验（CoT、自一致性）都通过这个适配器复用同一套生成基础设施。

#### 4.2.4 代码实践

**实践目标**：不加载模型，用项目自带的单元测试验证 flex 的「参数透传」与「token 拼接」行为。

1. 打开 [tests/test_ch04.py:L48-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L48-L70)。该测试用一个 fake 生成器（依次 `yield` token 7、8）和一个把 7/8 映射为 `"X"`/`"Y"` 的 `DummyTokenizer`（定义在 [tests/test_ch04.py:L11-L23](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L11-L23)）来隔离模型。
2. 运行该测试：

   ```bash
   pytest tests/test_ch04.py::test_generate_text_stream_concat_flex_uses_custom_generator -v
   ```

3. 阅读断言：`out == "XY"` 验证「逐 token 消费 + 拼接 decode」；`calls[0]["temperature"] == 0.5` 验证 kwargs 原样透传；`calls[0]["eos_token_id"] == tok.eos_token_id` 验证 EOS 由 flex 主动补给引擎。
4. 仿照它写一个 5 行小脚本（示例代码，不是项目原有代码）：自己定义 `fake_generate_func`，`yield` 三个 token，断言 flex 返回的字符串与 `DummyTokenizer` 的映射一致。

**需要观察的现象**：测试通过；fake 引擎收到的 kwargs 与你传入的完全一致。
**预期结果**：确认 flex 本身不含任何采样逻辑，它只是「编码 → 驱动引擎 → 拼接解码」的管道。若本机未配好 pytest 环境则为「待本地验证」（该测试不依赖网络与模型权重，属轻量测试）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `generate_func` 换成 `generate_text_basic_stream_cache`（默认值）再跑 `self_consistency_vote`，会发生什么？

**答案**：贪心引擎输出确定，10 个样本的完整回答逐字相同，`short_answers` 是 10 个相同字符串，`counts` 只有一项、票数 10。功能上不报错，但白付 10 倍计算，自一致性名存实亡——所以采样多样性是投票的前提。

**练习 2**：为什么 `temperature`、`top_p` 参数不直接写进 `generate_text_stream_concat_flex` 的函数签名？

**答案**：因为不同引擎的参数集不同（贪心引擎不需要温度，第 5 章的用法可能还有别的参数）。用 `**generate_kwargs` 透传让适配器对引擎的具体参数保持无关，新增引擎类型时 flex 一行都不用改——这是依赖注入「面向抽象、隔离变化」的直接收益。

### 4.3 计票与平票：Counter、majority_winners 与 final_answer=None

#### 4.3.1 概念说明

投票阶段要回答一个问题：**当没有答案拿到严格多数票时，库应该怎么办？** 本项目的设计是把决定权交还给调用者：`self_consistency_vote` 在平票时返回 `final_answer=None`，但同时返回完整的票数分布（`counts`）、分组索引（`groups`）与并列冠军名单（`majority_winners`），让上层自己决定兜底策略（评测脚本选择「取首次出现的并列者」）。这比在库内部悄悄挑一个更显式、更可审计——一个「统计函数」不应该暗含「平票裁决策略」。

这一设计也和测试友好的纯函数风格一致：投票块只依赖 `short_answers` 列表，可以脱离模型单独测试。

#### 4.3.2 核心流程

```text
counts = Counter(short_answers)            # {"42": 3, "7": 1, "": 1}
groups = {答案: [产出该答案的样本下标列表]}     # {"42": [0,2,4], "7": [1], "": [3]}
mc = counts.most_common()                  # 按票数降序; 同票按首次出现顺序
top_freq = mc[0][1]                        # 最大票数
majority_winners = [s for s, f in mc if f == top_freq]
final_answer = mc[0][0] if len(majority_winners)==1 else None
```

平票判定可以形式化为：设 \( c(a) \) 为答案 \( a \) 的票数，\( n \) 为样本总数，则

\[
\text{唯一赢家存在} \iff \left|\arg\max_a c(a)\right| = 1, \qquad
\text{平票} \iff \left|\arg\max_a c(a)\right| > 1
\]

注意「严格多数」（某答案票数 \( > n/2 \)，此时不可能有并列）只是平票的充分不必要反面——例如 n=4 时票型 2:1:1 没有严格多数，但赢家唯一（2 票者胜）；票型 2:2 才触发 `None`。4.4 节脚本里的 early stop 用的正是「票数 \( > n/2 \) 的强条件」，比「赢家已唯一」更保守。

#### 4.3.3 源码精读

投票块全文位于 [reasoning_from_scratch/ch04.py:L298-L310](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L298-L310)：

- [reasoning_from_scratch/ch04.py:L299-L302](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L299-L302)：`Counter(short_answers)` 一步计票；`groups` 用字典推导初始化后回填样本下标，方便回答「赢家的某条完整推理链是第几个样本」——评测脚本正是靠它把获胜答案对应的一条 `full_answers` 写进日志。
- [reasoning_from_scratch/ch04.py:L304-L306](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L304-L306)：`if not mc` 处理空输入（`num_samples=0`）的极端情况，直接给 `majority_winners=[], final_answer=None`。
- [reasoning_from_scratch/ch04.py:L307-L310](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L307-L310)：先取最大票数 `top_freq`，再收集所有并列者；`len(majority_winners) == 1` 时才给出 `final_answer`，否则置 `None`。

返回字典见 [reasoning_from_scratch/ch04.py:L312-L319](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L312-L319)，六个键的作用：

| 键 | 含义 | 典型消费者 |
| --- | --- | --- |
| `full_answers` | n 条完整回答文本 | 评测脚本取获胜答案的一条原始推理链写日志 |
| `short_answers` | n 个提取后的短答案 | 调试提取质量 |
| `counts` | `{短答案: 票数}` | 观察分布集中度 |
| `groups` | `{短答案: [样本下标]}` | 定位某答案出自哪些样本 |
| `majority_winners` | 并列冠军列表（可能多个） | 脚本平票时取 `[0]` 兜底 |
| `final_answer` | 唯一赢家或 `None` | 下游判分 |

配套的单元测试在 [tests/test_ch04.py:L129-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L129-L163)：它用 monkeypatch 把 `generate_text_stream_concat_flex` 替换成按序返回 `["2", "2", "3"]` 的假引擎，断言 `final_answer == "2"`、`counts["2"] == 2`、`majority_winners == ["2"]`——注意它连模型和 tokenizer 都用 `None`/占位对象，验证了投票逻辑可完全脱离 GPU 测试。

顺带一提 `Counter.most_common()` 的一个细节：同票项按**首次出现顺序**排列（Python 的 `sorted` 稳定，且 `Counter` 内部字典保持插入序）。这个「隐藏语义」正是评测脚本「平票取首次出现」兜底的依据，下一节会看到。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：① 真实模型上跑一次 5 样本投票，读懂返回字典；② 不用模型，复现 2:2 平票返回 `None` 的行为。

**第一部分：真实投票（需要已下载 base 权重；GPU 建议，CPU 可行但慢）**

1. 在仓库根目录新建脚本（示例代码，非项目原有代码）：

   ```python
   import torch
   from reasoning_from_scratch.ch02 import get_device
   from reasoning_from_scratch.ch03 import load_model_and_tokenizer, render_prompt, load_math500_test
   from reasoning_from_scratch.ch04 import self_consistency_vote

   device = get_device()
   model, tokenizer = load_model_and_tokenizer(which_model="base", device=device)

   data = load_math500_test()
   problem = data[0]["problem"]                      # 任选一道题
   prompt = render_prompt(problem)

   result = self_consistency_vote(
       model=model, tokenizer=tokenizer, prompt=prompt, device=device,
       num_samples=5, max_new_tokens=256,            # 教学规模; 正式实验用 2048
       temperature=0.8, top_p=0.9, seed=42,
   )
   print("counts:           ", result["counts"])
   print("groups:           ", result["groups"])
   print("majority_winners: ", result["majority_winners"])
   print("final_answer:     ", result["final_answer"])
   ```

2. 运行并观察 `[Sample i/5] → '...'` 的逐样本进度行，然后核对四个打印。

**需要观察的现象**：5 个短答案中应有若干重复；`groups` 的值列表长度之和等于 5；若某答案独占 3 票以上，`majority_winners` 只有一个元素且 `final_answer` 非空。
**预期结果**：CPU 上约需几十分钟量级（256 token × 5 样本），GPU 上数分钟；具体票型「待本地验证」（取决于权重与种子，`seed=42` 保证可复现）。

**第二部分：平票复现（零计算资源）**

1. 构造 4 个样本 2:2 的 `short_answers`，把 [reasoning_from_scratch/ch04.py:L298-L310](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L298-L310) 的计票块原样抄进一个脚本（示例代码）：

   ```python
   from collections import Counter

   short_answers = ["42", "42", "7", "7"]            # 人为构造 2:2 平票

   counts = Counter(short_answers)
   mc = counts.most_common()
   top_freq = mc[0][1]
   majority_winners = [s for s, f in mc if f == top_freq]
   final_answer = mc[0][0] if len(majority_winners) == 1 else None

   print(mc, majority_winners, final_answer)
   ```

2. 运行并核对输出；再改成 `["42", "42", "7", "7", "7"]`（3:2）对比。

**预期结果**：2:2 时输出 `[('42', 2), ('7', 2)] ['42', '7'] None`——`majority_winners` 有两个并列者、`final_answer` 为 `None`，且 `mc` 中 `'42'` 排在 `'7'` 前（首次出现顺序）；改成 3:2 后 `final_answer` 变为 `'7'`。

#### 4.3.5 小练习与答案

**练习 1**：`num_samples=4`、票型 2:1:1 时，`final_answer` 是什么？这算平票吗？

**答案**：`final_answer='42'`（假设 2 票者是 `'42'`）。不算平票：`majority_winners` 只有 `'42'` 一个（2 票严格多于 1 票），赢家唯一即产出答案。可见「平票」指的是**最大票数并列**，不是「没有过半数」。

**练习 2**：为什么库函数不在平票时自己 `random.choice(majority_winners)` 挑一个？

**答案**：统计与裁决职责分离。库若内置随机裁决，同一输入两次调用结果不同、不可复现，也剥夺了调用方的策略空间（评测脚本想按首次出现兜底，安全场景可能想宁可弃答）。返回 `None` + 并列名单是最小信息量的诚实接口。

**练习 3**：`extract_final_candidate` 偶尔会返回空字符串 `""`（如生成被截断且无数字）。空字符串会怎样参与投票？

**答案**：`""` 是完全合法的 Counter 键，与其他短答案一样计票。若它在 n 个样本中意外拿到并列最高票，会进入 `majority_winners`，甚至成为 `final_answer`——下游 `grade_answer` 对空串判不正确。这是把「提取失败」静默并入投票的一种妥协，也是 4.4 节评测脚本要靠 `max_new_tokens=2048` 降低截断概率的原因之一。

### 4.4 MATH-500 实验脚本：单条版 early_stop 与 batched 版批处理

#### 4.4.1 概念说明

`ch04/02_math500-inference-scaling-scripts/` 下有两个可独立运行的评测脚本，把 `self_consistency_vote` 装进 u4-l4 的评测循环（渲染→生成→提取→判分→jsonl 落盘）：

- **单条版** `self_consistency_math500.py`：逐样本串行采样（一次 forward 只生成一条回答）。它在库版本之上增加了一个省算力的巧招——**early stop 提前停止**：一旦某个答案的票数超过 \( n/2 \)，剩余样本不可能翻盘，直接停止采样。
- **batched 版** `self_consistency_math500_batched.py`：把同一 prompt 的 n 个采样拼成一个 **batch**，一次前向同时推进 n 条生成。README 明确提示这条路线「更快但更吃显存」（[ch04/02_math500-inference-scaling-scripts/README.md:L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L73)）。

值得注意的是脚本对 `self_consistency_vote` 采取**复制改造**而非导入：两个脚本各自内嵌了一份改过的投票函数（用 `# NEW` 注释标出差异点），与 README 声明「脚本 import 包以避免重复」并行不悖——作者保留了每个实验脚本「可独立阅读的完整实验记录」属性，改动点一眼可见。

#### 4.4.2 核心流程

单条版 `self_consistency_vote`（含 early stop）的流程：

```text
for i in 0..n-1:
    采样并提取 short; counts[short] += 1
    if early_stop and counts[short] > n/2:   # 严格大于一半 → 数学上已锁定胜局
        majority_winners=[short]; final_answer=short; break
若未被 early stop 命中 → 走与库版本相同的平票判定
```

batched 版的核心 `generate_text_top_p_batched` 流程：

```text
prompt_ids.repeat(n, 1)                     # 同一 prompt 复制 n 行 → 天然等长, 无需 padding
一次 prefill 前向 (batch=n)
for step in 0..max_new_tokens-1:
    温度缩放 → softmax → top_p_filter (整批一起过滤, 每行独立重归一化)
    逐行 multinomial 采样 (已结束的行直接填 eos_token_id, 不再采样)
    记录本步 token; 更新 finished 行掩码
    全部行结束 → break; 否则整批做一次 decode 前向
```

它解决的是**吞吐**问题：n 条链共享同一次前向的算力发射开销，prefill 阶段尤其划算（n 次 prefill 变 1 次）。代价是显存随 n 线性增长，且无法提前停止（整批必须陪跑到最长的那条链结束）。

#### 4.4.3 源码精读

**单条版的 early stop**，位于 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py:L72-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L72-L79)：刚计完票的 `short` 若满足 `counts[short] > num_samples / 2`，立即锁定 `majority_winners`/`final_answer` 并 `break`。判据用**严格大于**：n=5 时第 3 票（3 > 2.5）即触发，省下 2 次生成；n=4 时 2 票不触发（2 > 2.0 为假），因为 2:2 平票仍可能出现、未到数学锁定。early stop 后 `counts`/`groups` 只反映已采样部分，且跳过了平票判定（胜局已定，不可能平票）。该功能默认关闭，需命令行 `--early_stop` 打开（参数定义见 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py:L270-L274](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L270-L274)）。

**评测循环的平票兜底**，位于 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py:L144-L149](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L144-L149)：库返回 `final_answer=None` 时，脚本取 `results["majority_winners"][0]`——如 4.3.3 所述，`most_common()` 对同票项保持首次出现顺序，所以「并列者中的第一个」就是**最早被采样出来**的答案。随后的 [L155-L161](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L155-L161) 用 `short_answers` 与获胜答案比对，从 `full_answers` 里捞一条对应完整推理链写入 jsonl 的 `generated_text` 字段，供事后人工审查。命令行默认值 `--num_samples 3`、`--temperature 1.0`（见 [L252-L269](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L252-L269)），而 README 的正式实验统一用 `--temperature 0.9 --top_p 0.9`。

**batched 版的批量组装**，位于 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py:L131-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L131-L150)：`prompt_ids.repeat(current_batch, 1)` 把同一 prompt 复制 n 行。因为所有行**长度天然相同**，这里不需要 u4-l4 批处理脚本里的 left-padding——这是「批同一 prompt 的多个采样」与「批不同 prompt」的关键差异。外层 `while len(full_answers) < num_samples` 配合 `current_batch = min(batch_size, remaining)` 为分块采样预留了结构（当前 `batch_size = max(1, num_samples)`，实际一轮完成）。

**逐行随机流**，位于 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py:L51-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L51-L57)：为每一行创建独立的 `torch.Generator(device="cpu")`，种子为 `seed + seed_offset + idx + 1`（`seed_offset=len(full_answers)`，保证多轮调用时每行种子不冲突）。逐行独立生成器是批采样的必然要求——若共用全局 RNG，一次 `multinomial` 的调用次序会改变每行的随机流。一个值得注意的细节：该分支的条件是 `seed is not None and temperature not in (None, 1.0)`，即恰好 `temperature=1.0` 时虽有采样但不创建逐行生成器（走全局随机流），复现实验时需留意。

**结束行的处理**，位于 [ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py:L67-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L67-L105)：`finished` 布尔掩码标记已产出 EOS 的行；采样阶段这些行直接填充 `eos_token_id` 跳过 `multinomial`（贪心分支见 [L62-L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L62-L66)，采样分支见 [L73-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L73-L86)），token 追加时也跳过它们（[L92-L99](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L92-L99)）。填 EOS 而不是随机 token 是为了不让已结束的行污染后续前向（其 KV 缓存仍参与 batch 计算）。全部行结束后 `break`，同样保留了「跳过最后一次前向」优化（[L107-L108](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L107-L108)）。

两版脚本的其余差异可以列成一张表（行号均指对应文件）：

| 维度 | 单条版 | batched 版 |
| --- | --- | --- |
| 采样方式 | n 次独立的单行 prefill+decode（[L47-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L47-L70)） | 1 次 n 行的批量 prefill+decode（[L136-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L136-L150)） |
| early stop | 支持，`counts[short] > n/2` 提前 break（[L72-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L72-L79)） | 不支持（整批必须陪跑到最长链结束） |
| 可复现性 | 每样本 `torch.manual_seed(seed+i+1)` 重置全局流（[L48-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L48-L49)） | 逐行独立 CPU Generator，种子 `seed+offset+idx+1`（[L51-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L51-L57)） |
| `long_answer` 初始化 | 未初始化（`extracted` 为 None 的极端路径可能未定义，[L155-L161](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L155-L161)） | 显式 `long_answer = None`（[L238](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L238)） |
| 显存 | 恒定（单行） | 随 n 线性增长 |

两者的评测主循环 `evaluate_math500_stream`（单条版 [L98-L200](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py#L98-L200)、batched 版 [L184-L283](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/self_consistency_math500_batched.py#L184-L283)）结构与 u4-l4 完全同构，此处不再重复。

#### 4.4.4 代码实践

**实践目标**：小规模冒烟运行两个脚本，对比行为与耗时。

1. 先做零成本的「手算 early stop」：设 `num_samples=4`，样本短答案依次为 `A, A, B, B`。逐票演算 `counts[A] > 4/2` 是否曾为真（第 2 票时 2 > 2.0 为假），确认本例**不会**提前停、最终走平票返回 `None`；再对 `num_samples=5` 演算两个序列：`A, A, A`（第 3 票使 3 > 2.5 成立，立即停止、省下 2 次生成）与 `A, A, B, B, A`（直到最后 1 票才锁定胜局，一次也没省下）——提前停止的收益取决于高票答案**多早**出现。
2. 单条版冒烟（CPU 亦可，控制在几分钟内）：

   ```bash
   python ch04/02_math500-inference-scaling-scripts/self_consistency_math500.py \
       --which_model base --dataset_size 2 --num_samples 2 \
       --max_new_tokens 128 --temperature 0.9 --top_p 0.9 --early_stop
   ```

3. batched 版冒烟（把脚本名换成 `self_consistency_math500_batched.py`，去掉 `--early_stop`，其余参数相同）。
4. 各自打开输出的 `math500_base-<device>-evaluate-script.jsonl`，对比两版同题的 `extracted` 字段是否一致，记录两版总耗时。

**需要观察的现象**：两个 jsonl 均含 `index/problem/gtruth_answer/generated_text/extracted/correct` 六字段；同参数下两版准确率可能相同也可能不同（随机流不同，且 batched 前向的浮点归约顺序与单条不同，采样分布有微小数值差异）。
**预期结果**：GPU 上 batched 版明显快于单条版；CPU 上差距缩小甚至反转（批并行主要赢在 GPU 的并行前向，与 u4-l4 批处理评测的结论一致）。具体耗时「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：early stop 的判据为什么用严格大于 `num_samples / 2`，而不是大于等于？

**答案**：\( c(a) > n/2 \) 是「剩余票数无法翻盘」的充要条件。若用 `>=`，偶数 n 时拿 n/2 票就停——例如 n=4 拿 2 票即停，但另外 2 票仍可能投给同一对手形成 2:2 平票，提前宣布赢家是错的。严格大于保证了停下的时刻胜局在数学上已锁定（n=5 时 3 票 > 2.5 ✓，n=4 时 3 票 > 2.0 ✓、2 票不触发）。

**练习 2**：batched 版为什么不需要 left-padding，而 u4-l4 的 `evaluate_math500_batched.py` 需要？

**答案**：batched 自一致性批的是**同一个 prompt 的 n 个副本**，`prompt_ids.repeat(n, 1)` 产生的各行长度天然相同，无需对齐；u4-l4 批的是**不同题目的不同 prompt**，长度参差，必须 left-padding 补齐并配套掩码与位置 id。批内元素是否同构决定了要不要 padding，这是批处理设计的第一问。

**练习 3**：batched 版为什么没法做 early stop？

**答案**：early stop 依赖「逐条采样、逐票检查、随时可以不再开始下一条」的串行节奏。batched 版 n 条链在同一批次里齐头并进，即使某答案已过半，其余行也已付出（或正在付出）生成成本，中途裁剪行并不能省下已发射的前向；要支持就得拆成多轮小批（代码里的 `while`/`current_batch` 结构正是为此预留），但那会牺牲批大小、回到吞吐与提前停止的权衡。

## 5. 综合实践

把本讲三件资产（采样引擎、投票、评测脚本）串成一个小实验，二选一：

**路线 A（无 GPU，纯逻辑验证）**：仿照 [tests/test_ch04.py:L129-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L129-L163) 的 monkeypatch 手法，用 `unittest.mock.patch`（或直接赋值）把 `reasoning_from_scratch.ch04.generate_text_stream_concat_flex` 替换为按序返回预设回答的假引擎，构造三组 `short_answers` 序列：① 3:2 有赢家；② 2:2 平票；③ 含空字符串的 2:2 平票。对每组调用库版 `self_consistency_vote`，断言 `final_answer`/`majority_winners`/`counts` 符合预期，并写出每组对应的 `grade_answer` 判分结果（对照答案自拟）。这验证你能在零计算成本下测试整条「采样→提取→投票」逻辑。

**路线 B（有 GPU，小规模真实实验）**：取 MATH-500 前 3 题，用 4.4.4 的命令分别跑 `--num_samples 1` 与 `--num_samples 3`（其余参数相同），对比两个 jsonl：哪几题从错变对？`counts` 分布是否集中？再给某道平票题找到它的 `majority_winners`，验证脚本落盘的 `extracted` 取的是首次出现的那个。耗时与准确率结论「待本地验证」。

## 6. 本讲小结

- 自一致性 = **多次温度采样 + 短答案提取 + 多数投票**，是纯推理时扩展方法：不训练、只花算力；理论上有单链正确率 \( p > 0.5 \) 的前提，实验上 base 模型配合 CoT 从 33.4%（单采样）拉到 52.0%（n=10），代价是耗时近似线性翻倍。
- `generate_text_stream_concat_flex` 是**可插拔适配器**：prompt 进、完整文本出，通过 `generate_func` 注入 + `**generate_kwargs` 透传，把贪心/top-p 等任意流式生成器统一到同一接口下，被第 4、5 章实验复用。
- 库版 `self_consistency_vote` 用 `Counter` 计票，**平票时返回 `final_answer=None`** 并附上 `counts/groups/majority_winners` 全量信息——统计与裁决职责分离，兜底策略交给调用者（评测脚本按首次出现取 `majority_winners[0]`）。
- 单条版脚本的 **early stop**（`counts[short] > n/2` 即停）在数学锁定胜局的时刻省掉剩余采样；batched 版用 `prompt_ids.repeat(n, 1)` 把 n 个同 prompt 采样并成一个 batch，吞吐换显存，且不做提前停止。
- 两版脚本对投票函数**复制改造**（`# NEW` 标注差异），是「实验脚本自成一体、改动点可读」的教学工程风格；批同 prompt 无需 padding 与批不同 prompt 需要 left-padding 的对比，是批处理设计的一般性教训。

## 7. 下一步学习建议

本讲结束了单元五（第 4 章推理时扩展）。接下来进入单元六（第 5 章）：

- **u6-l1（Log-prob 与序列似然打分）**：自一致性只看「答案出现的次数」，下一讲学习给每条回答算一个连续分数（`calc_next_token_logprobas` / `avg_logprob_answer`），为择优提供更细的信号。
- **u6-l2（Best-of-N 与启发式打分）**：把「投票择优」换成「打分择优」，观察 `heuristic_score` 与 `final_answer` 众数的不同偏好，并比较两者在 MATH-500 上的实验数据。
- 建议同时回读 [reasoning_from_scratch/ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py) 的 import 区（[L5-L10](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L5-L10)）：`ch02` 的生成函数、`ch03` 的提取器、`qwen3` 的 KVCache 在这里汇成一条依赖链——这正是整本手册学习顺序的镜像。
