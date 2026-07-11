# 自定义 generate：采样、解码与流式输出

## 1. 本讲目标

上一讲（u3-l5）我们搞清楚了 `MiniMindForCausalLM.forward` 如何把一段 `input_ids` 变成训练用的交叉熵损失。本讲要回答另一个问题：**推理时，模型是怎么一个字一个字把回答「写」出来的？**

读完本讲，你应当能够：

- 说清楚「自回归生成（autoregressive generation）」这个循环到底在反复做什么。
- 理解 KV Cache 如何让每一步只算「新来的一个 token」，而不是每步都重算整段序列。
- 读懂 temperature / top_k / top_p / repetition_penalty 这四个采样旋钮各自的数学含义与代码实现。
- 解释 batch 生成时，先结束（已经吐出 EOS）的序列如何被处理，整体何时提前终止。
- 理解 `TextStreamer` 是如何实现「逐字蹦字」的流式输出，以及 `num_return_sequences`、`return_kv` 等扩展点的用途。

本讲对应的最小模块是 `MiniMindForCausalLM.generate` 与 `TextStreamer` 调用。

## 2. 前置知识

- **自回归（autoregressive）**：语言模型生成文本的方式是「看前面已经写出来的字，预测下一个字，把它接上去，再看前面所有字，再预测下一个字……」如此循环。每一步的输出都依赖前面所有步的输出。
- **softmax**：把一组任意实数（logits）归一化成一个概率分布（每个分量 ≥ 0，且求和为 1）。本讲里它把模型输出的 logits 变成「下一个 token 是词表里第 i 个词的概率」。
- **采样 vs 贪心**：
  - 贪心（greedy）：每步直接取概率最大的那个 token，结果确定、可复现，但容易单调重复。
  - 采样（sampling）：按概率分布「掷骰子」抽一个 token，结果有随机性、更丰富，但可能跑偏。
- **KV Cache**：在 u3-l2 我们看到 Attention 层会把每层的 Key、Value 缓存下来。生成时，前面 token 的 K/V 已经算过一次了，没必要每生成一个新字就把历史全重算一遍——只要把新 token 的 K/V 追加进缓存即可。这就是本讲反复利用的「增量推理」基础。
- **token 与解码**：模型输出的不是文字，而是 token id（一个整数）；要用分词器的 `decode` 把一串 id 还原成人类可读的文字。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | 定义 `MiniMindForCausalLM.generate`，本项目**自研**的自回归生成方法（L256–L288）。 |
| [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) | CLI 推理入口，构造 `TextStreamer` 并调用 `model.generate`，最后统计 tokens/s。 |

> 一个关键事实：`MiniMindForCausalLM` 虽然继承了 HuggingFace 的 `GenerationMixin`，但本讲的主角 `generate` 方法是**完全重写**的——它没有走 HF 那套 `LogitsProcessor` 机制，而是用几十行原生 PyTorch 手写了自回归循环与采样逻辑。这正是 MiniMind「从 0 原生实现、透明可学」理念的体现。

## 4. 核心概念与源码讲解

### 4.1 generate 全貌：自回归循环与 KV Cache 增量推理

#### 4.1.1 概念说明

`generate` 要解决的问题是：给定一段 prompt（输入 token 序列），让模型一段一段地往后续写，直到满足某个停止条件（达到最大长度，或所有序列都生成了结束符 EOS）。

最朴素的想法是：每生成一个新 token，就把「prompt + 已生成的所有 token」整段重新喂进 `forward`，重新算一遍。问题是注意力对序列长度是平方复杂度，序列越长越慢，而且前面那些 token 的 K/V 我们明明上一步已经算过了，纯属浪费。

所以工程上的标准做法是 **KV Cache 增量推理**：把历史 token 的 K/V 缓存起来，每一步只把「最新那一个 token」喂进 `forward`，让 Attention 把它的新 K/V 追加进缓存，再拿新的 Q 去和「全部历史 K/V（缓存里的 + 新追加的）」做注意力。这样每一步的计算量基本恒定，跟序列长度无关。

#### 4.1.2 核心流程

`generate` 的主干可以抽象成下面这段伪代码：

```
初始化 input_ids = prompt
初始化 past_key_values = None
初始化 finished = [False] * batch        # 每条序列是否已结束

重复最多 max_new_tokens 次：
    past_len = 已缓存 K/V 的长度（首步为 0）
    只取 input_ids[:, past_len:]（即「还没算过」的新 token）送进 forward
    得到 logits（只关心最后一个位置）和新的 past_key_values

    用 temperature / repetition_penalty / top_k / top_p 改写 logits
    采样或贪心选出 next_token

    让已 finished 的序列强行吐 EOS（保持 batch 对齐）
    把 next_token 拼到 input_ids 末尾
    更新 past_key_values
    经 streamer 把 next_token 推出去（流式打印）

    更新 finished；若全部 finished 则提前 break

返回完整的 input_ids（= prompt + 生成内容）
```

注意一个细节：首步 `past_len=0`，所以送进去的是完整 prompt，这一步叫 **prefill**（预填），它一次性把 prompt 所有 token 的 K/V 都缓存好；从第二步起 `past_len>0`，送进去的只有 1 个 token，这叫 **decode**（解码）步。

#### 4.1.3 源码精读

整个 `generate` 方法定义在这里（含 `@torch.inference_mode()` 装饰器与函数签名）：

[MiniMindForCausalLM.generate 签名 — model/model_minimind.py:L255-L257](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L255-L257)
> `@torch.inference_mode()` 关闭整个生成过程的梯度追踪，省掉反向传播要用的显存，是推理时必备。签名里列出了所有采样旋钮：`temperature`、`top_p`、`top_k`、`repetition_penalty`、`do_sample` 等。

初始化部分：把输入在 batch 维复制 `num_return_sequences` 份，并准备好 `finished` 标记。

[generate 初始化 — model/model_minimind.py:L258-L262](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L258-L262)
> `input_ids = kwargs.pop("input_ids", inputs).repeat(num_return_sequences, 1)` 同时兼容 `inputs=` 和 `input_ids=` 两种传参方式，并用 `.repeat` 在第 0 维复制 N 份，实现「同一 prompt 并行生成 N 条不同回答」。`finished` 是一个布尔张量，每条序列一个槽位，记录它是否已经结束。

循环主干——KV Cache 增量推理的关键两行：

[增量切片 + forward — model/model_minimind.py:L263-L266](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L263-L266)
> `past_len = past_key_values[0][0].shape[1]` 从第 0 层缓存的 K 张量读出「已经缓存了多少个 token」；首步缓存为空则 `past_len=0`。`self.forward(input_ids[:, past_len:], ...)` 只把「切片之后」的新 token 喂进去。同时每步把 `attention_mask` 在末尾补一列 1，让它和不断增长的 `input_ids` 保持等长对齐。

缓存更新：

[更新 past_key_values — model/model_minimind.py:L281](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L281)
> `past_key_values = outputs.past_key_values if use_cache else None`：开 `use_cache` 时把更新后的缓存接住；关掉则每步都置空（退化为朴素全量重算，仅用于对比或调试）。

> 这些缓存怎么用上的？回顾 u3-l2 的 [Attention.forward:L120-L122](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L120-L122)：`xk = torch.cat([past_key_value[0], xk], dim=1)` 会把本步新算的 K 和缓存里的历史 K 在序列维拼接——这就是「增量」落地的位置。

#### 4.1.4 代码实践：观察 KV Cache 的加速效果

**实践目标**：直观感受 `use_cache` 开与关对生成速度的影响。

**操作步骤**：

1. 先按 u1-l3 的方式让 `eval_llm.py` 能正常跑通一次对话（确认有权重、CUDA 可用）。
2. 复制一份临时脚本 `bench_cache.py`（**示例代码，非项目原有文件**），核心是分别用 `use_cache=True` 和 `use_cache=False` 调用 `generate`，并计时：

   ```python
   # 示例代码：放在仓库根目录运行，需能 import 到 model/eval_llm 的 init_model
   import time, argparse
   from eval_llm import init_model

   args = argparse.Namespace(
       load_from='model', save_dir='out', weight='full_sft', lora_weight='None',
       hidden_size=768, num_hidden_layers=8, use_moe=0,
       inference_rope_scaling=False, device='cuda')
   model, tokenizer = init_model(args)
   prompt = tokenizer.apply_chat_template(
       [{"role": "user", "content": "用三句话介绍太阳系"}],
       tokenize=False, add_generation_prompt=True)
   inp = tokenizer(prompt, return_tensors="pt").to('cuda')

   for uc in (True, False):
       st = time.time()
       out = model.generate(inputs=inp["input_ids"], attention_mask=inp["attention_mask"],
                            max_new_tokens=128, use_cache=uc, do_sample=False,  # 贪心，保证两次内容一致
                            eos_token_id=tokenizer.eos_token_id)
       n = out.shape[1] - inp["input_ids"].shape[1]
       print(f"use_cache={uc}: {n} tokens, {n/(time.time()-st):.2f} tokens/s")
   ```

3. 运行 `python bench_cache.py`。

**需要观察的现象**：两次生成的文字内容应基本一致（因为都用了贪心 `do_sample=False`），但 `use_cache=True` 的 tokens/s 明显更高（通常快数倍，序列越长差距越大）。

**预期结果**：`use_cache=True` 比 `use_cache=False` 快很多；生成内容相同。**待本地验证**具体倍数（取决于显卡与序列长度）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `max_new_tokens` 设得极大，而模型迟迟不输出 EOS，循环会怎样终止？
> **答**：循环 `for _ in range(max_new_tokens)` 跑满次数后自然退出，返回当前已拼好的 `input_ids`。EOS 只是「提前 break」的条件，不是唯一终止方式。

**练习 2**：首步（prefill）`past_len` 为什么是 0？从第二步起 `past_len` 又是怎么得到的？
> **答**：首步 `past_key_values` 为 `None`，三元表达式取 0。从第二步起，`past_key_values` 已被赋值为上一步 `outputs.past_key_values`，读取第 0 层 K 张量的列数（`shape[1]`）即为已缓存 token 数。

---

### 4.2 采样四件套：temperature / top_k / top_p / repetition_penalty

#### 4.2.1 概念说明

「贪心」永远挑概率最大的 token，输出确定但呆板；「纯采样」按原始概率分布抽签，又容易抽到离谱的低概率 token。本项目在两者之间提供四个旋钮，从 logits 出发一步步改造概率分布：

- **temperature（温度）**：先把 logits 整体除以 T。T<1 让分布更「尖」（更自信，更接近贪心），T>1 让分布更「平」（更随机）。注意它作用于 logits，在采样前。
- **top_k**：只保留概率最高的 k 个候选，其余一律置 −∞（等价于概率 0），避免抽到长尾垃圾词。
- **top_p（nucleus / 核采样）**：把候选按概率从大到小排序，累加概率，只保留累加值首次达到 p 的那个最小集合。比 top_k 更「自适应」——分布集中时留几个，分布发散时留很多。
- **repetition_penalty（重复惩罚）**：对「已经出现过的 token」压低它们的 logit，抑制模型反复说同一句话。

#### 4.2.2 核心流程

四件套在代码里按固定顺序串行作用于「最后一个位置的 logits」：

```
logits = outputs.logits[:, -1, :] / temperature          # 1. 温度缩放
if repetition_penalty != 1.0: 对已出现 token 改写 logits  # 2. 重复惩罚
if top_k > 0: 保留 top_k 个，其余置 -inf                  # 3. top_k 截断
if top_p < 1.0: 核采样截断                                 # 4. top_p 截断
next_token = softmax 后 multinomial 采样（或 argmax 贪心）
```

带温度的 softmax 概率为：

\[ p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)} \]

重复惩罚对已出现 token 的 logit 做如下改写（α 即 `repetition_penalty`）：

\[ z_i' = \begin{cases} z_i / \alpha & \text{若 } z_i > 0 \\ z_i \cdot \alpha & \text{若 } z_i \le 0 \end{cases} \]

无论原 logit 正负，α>1 时新值都比原来「更小」（正的变小、负的变更负），从而降低该 token 被采到的概率。

#### 4.2.3 源码精读

取最后一位 logits 并除以温度：

[温度缩放 — model/model_minimind.py:L267](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L267)
> `logits = outputs.logits[:, -1, :] / temperature`：只取序列最后一个位置的词表 logits，再整体除以温度。这里 `forward` 默认 `logits_to_keep=0`（保留全部位置），所以可以正常取到最后一位。

重复惩罚（逐条序列处理）：

[repetition_penalty — model/model_minimind.py:L268-L270](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L268-L270)
> `seen = torch.unique(input_ids[i])` 取该序列里出现过的所有 token；`torch.where(score > 0, score / repetition_penalty, score * repetition_penalty)` 就是上面那个分段公式。注意它是「正除负乘」，两路都起到压制作用。

top_k 截断（一行向量化的精妙写法）：

[top_k — model/model_minimind.py:L271-L272](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L271-L272)
> `torch.topk(logits, top_k)[0][..., -1, None]` 取出「第 top_k 大的那个值」作为阈值，凡严格小于它的 logit 全部置 −∞。最终恰好保留 top_k 个候选。

top_p 核采样（最绕的一段，重点看掩码右移）：

[top_p — model/model_minimind.py:L273-L277](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L273-L277)
> 先把 logits 降序排序，对排序后的分布做累积求和 `cumsum`，超过 `top_p` 的位置标 True 得到 `mask`。这里有个关键技巧：`mask[..., 1:], mask[..., 0] = mask[..., :-1].clone(), 0` 把掩码整体右移一位——目的是**保留恰好让累积概率越过阈值的那一个 token**（它本身要留下，它后面的才丢弃）。最后用 `mask.scatter(1, sorted_indices, mask)` 把排序顺序下的掩码还原回原始词表顺序，再置 −∞。

采样或贪心二选一：

[next_token 选择 — model/model_minimind.py:L278](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L278)
> `do_sample=True` 时 `torch.multinomial(softmax(logits), num_samples=1)` 按分布抽签；`do_sample=False` 时 `torch.argmax(logits, dim=-1, keepdim=True)` 直接取最大值，即贪心解码。这就是本讲实践任务要切换的开关。

> 小知识：`eval_llm.py` 里 `do_sample=True` 是写死的（见 [eval_llm.py:L82-L87](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L82-L87)），并没有对应的命令行参数，所以想体验贪心解码需要临时改这一行（见下方实践）。

#### 4.2.4 代码实践：贪心 vs 采样对比 + tokens/s 统计

**实践目标**：亲手切换 `do_sample`，对比同一 prompt 在贪心与采样下的输出差异，并读懂 eval_llm 的 tokens/s 统计。

**操作步骤**：

1. 用默认（采样）模式跑一次，记下输出：
   ```bash
   python eval_llm.py --weight full_sft --temperature 0.85 --top_p 0.95
   # 选 [0] 自动测试，观察若干 prompt 的回答与末尾 [Speed]: xx tokens/s
   ```
2. **临时**把 [eval_llm.py:L84](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L84) 里的 `do_sample=True` 改成 `do_sample=False`，再跑一次（注意：这是临时改动用于观察，验证完请改回，不要提交）。
3. 对比两次同一 prompt 的输出。

**需要观察的现象**：
- 贪心模式下，同一 prompt 反复运行输出**几乎完全一致**（因为 `eval_llm.py` 每轮还会 `setup_seed(random.randint(...))`，采样会变，但贪心结果只由 logits 决定，不受种子影响）。
- 采样模式下，每次输出**都不一样**，文字通常更自然多样，但偶尔会出现小重复或偏题。
- 贪心更容易出现「卡住重复同一句话」的现象（这正是 repetition_penalty 想缓解的，但 eval_llm 默认传 `repetition_penalty=1`，等于关闭了惩罚）。

**tokens/s 统计原理**见 [eval_llm.py:L90-L91](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L90-L91)：
> `gen_tokens = len(generated_ids[0]) - len(inputs["input_ids"][0])` 用「总长度 − prompt 长度」算出净生成 token 数；`gen_tokens / (time.time() - st)` 即每秒生成 token 数。注意它计的是端到端吞吐，包含 prefill + 全部 decode 步。

**预期结果**：贪心输出稳定可复现、可能偏单调；采样输出多样。tokens/s 数值取决于硬件。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `temperature` 设成一个很小的值（比如 0.01），采样模式下的输出会变成什么样？为什么？
> **答**：logits 除以 0.01 相当于乘以 100，softmax 后分布极度尖锐，最大概率的那个 token 几乎垄断全部概率，效果近似贪心解码。

**练习 2**：top_p 代码里为什么要 `mask[..., 1:], mask[..., 0] = mask[..., :-1].clone(), 0` 这一步右移？
> **答**：原始 `mask` 在「累积概率首次超过 top_p 的那个位置」就为 True，但这个 token 应当被保留（正是它让累积概率达标）。右移一位后，被置 True（即被丢弃）的是它之后的位置，从而正确地保留住最小达标集合。

**练习 3**：为什么 `repetition_penalty` 要写成「正 logit 除以 α、负 logit 乘以 α」而不是统一除以 α？
> **答**：logit 可正可负。若统一除以 α，负 logit 反而会被「变大」（更接近 0），该 token 概率不降反升，与「惩罚」目的相悖。分段处理保证正负两路都被压低。

---

### 4.3 batch finished 处理、提前终止与扩展点

#### 4.3.1 概念说明

实际生成往往是「一个 batch 里同时生成多条序列」（比如 `num_return_sequences=4` 就是同一条 prompt 并行出 4 条回答）。问题是：这 4 条长度几乎不可能一样，先写完的那条（先吐 EOS）不能就这么晾着——batch 是个规整的张量，所有序列必须等长拼接。

`generate` 的做法是：**一旦某条序列吐了 EOS，就标记它 finished；之后每步都强行把它的新 token 覆盖成 EOS**（相当于用 EOS 做 padding），直到所有序列都 finished 才整体 break。这样 batch 维度始终对齐，又能让没结束的序列继续生成。

本模块顺带讲两个扩展点：`num_return_sequences`（一次出几条）和 `return_kv`（要不要把缓存也返回出来）。

#### 4.3.2 核心流程

```
finished = [False]*batch
每步采样得到 next_token 后：
    已 finished 的行 → next_token 强行改成 eos_token_id
    把 next_token 拼到 input_ids
    finished |= (next_token == eos_token_id)
    若 finished 全为 True → break
全部结束后：按需返回 input_ids，或 {generated_ids, past_kv}
```

#### 4.3.3 源码精读

`finished` 初始化为全 False：

[finished 初始化 — model/model_minimind.py:L261](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L261)
> 形状是 `[batch]`，batch 维等于 `num_return_sequences`（或输入本身的多条序列）。

已结束序列强制吐 EOS（关键的对齐技巧）：

[强制 EOS — model/model_minimind.py:L279](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L279)
> `torch.where(finished.unsqueeze(-1), eos_token_id 填充, next_token)`：凡 `finished` 为 True 的行，无论本轮采样到什么，都替换成 EOS。这样即便某条序列早结束了，它后面每步都乖乖追加 EOS，保证 batch 张量规整；又因为流式输出会 `skip_special_tokens`，这些 EOS padding 不会被打印出来，用户无感。

拼接新 token 与更新 finished、提前终止：

[拼接 / 终止判定 — model/model_minimind.py:L280-L285](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L280-L285)
> `input_ids = torch.cat([input_ids, next_token], dim=-1)` 把新 token 接到末尾；`finished |= next_token.squeeze(-1).eq(eos_token_id)` 把「本步真的吐了 EOS」的行标 True；`if finished.all(): break` 全部结束就提前跳出循环，避免白跑到 `max_new_tokens`。

返回与扩展点：

[返回逻辑 — model/model_minimind.py:L286-L288](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L286-L288)
> 正常返回 `input_ids`；若调用方传了 `return_kv=True`，则返回字典 `{'generated_ids': ..., 'past_kv': past_key_values}`。把缓存返回出来，便于后续在同一对话里「接着往下聊」（多轮）而无需重算 prompt——这点会在 u7 的 rollout engine 里被用到。

#### 4.3.4 代码实践：用 num_return_sequences 一次出多条

**实践目标**：体会 batch 生成与多候选输出。

**操作步骤**（**示例代码**，需在仓库根目录、能 import 到模型的前提下运行）：

```python
# 示例代码
import torch
from eval_llm import init_model
import argparse
args = argparse.Namespace(load_from='model', save_dir='out', weight='full_sft',
    lora_weight='None', hidden_size=768, num_hidden_layers=8, use_moe=0,
    inference_rope_scaling=False, device='cuda')
model, tokenizer = init_model(args)

prompt = tokenizer.apply_chat_template([{"role":"user","content":"写一句励志的话"}],
    tokenize=False, add_generation_prompt=True)
inp = tokenizer(prompt, return_tensors="pt").to('cuda')

out = model.generate(inputs=inp["input_ids"], attention_mask=inp["attention_mask"],
    max_new_tokens=64, do_sample=True, temperature=0.9, top_p=0.9,
    num_return_sequences=3, eos_token_id=tokenizer.eos_token_id)
print("batch 维度（=num_return_sequences）:", out.shape[0])  # 预期 3
for i in range(out.shape[0]):
    print(i, tokenizer.decode(out[i][inp["input_ids"].shape[1]:], skip_special_tokens=True))
```

**需要观察的现象**：`out.shape[0]` 等于 3（同一 prompt 复制了 3 份并行生成）；三条输出内容不同（因为采样）；长度可能不一，但张量第二维等长——短的那条后面被补了 EOS。

**预期结果**：打印出 3 句各不相同的励志短句。**待本地验证**具体内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能在「某条序列一吐 EOS 就立刻把它从 batch 里删掉」？
> **答**：batch 是一个规整张量，删掉某行会让不同层、不同张量的维度对不上。强行补 EOS 既保持了形状对齐，又能在 `skip_special_tokens` 时对用户隐形。

**练习 2**：`return_kv=True` 场景下，第二次调用 generate 续聊时，要怎么把上次的结果接进去？
> **答**：把上次返回的 `past_kv` 通过 `past_key_values=` 参数（被 `**kwargs` 收下，L260 `kwargs.pop("past_key_values", None)`）传给下一次 `generate`，并把新的 user 输入作为 `inputs`。这样上次 prompt 的 K/V 不必重算。这是 rollout engine 复用前向的基础。

---

### 4.4 TextStreamer 流式输出

#### 4.4.1 概念说明

「流式输出」就是像 ChatGPT 那样**一边生成一边把字打到屏幕上**，而不是等全部生成完再一次性吐出。它的好处是首字延迟低、用户体验好。

`generate` 本身不直接 print，它只在每生成一个 token 时调用 `streamer.put(token)`「推」一个 token 出去；具体怎么显示（打印到终端？写进队列？）由 streamer 对象决定。这是一种「生产者–回调」解耦：`generate` 只负责生产 token，显示策略交给外部注入的 streamer。

本项目用的是 `transformers.TextStreamer`，它内部维护一个「已解码文本」的游标，每次 `put` 进来新 token 就增量解码、把新增的文字 `print` 到 stdout。

#### 4.4.2 核心流程

```
生成开始前：streamer.put(prompt)              # 让 streamer 知道 prompt（用于游标初始化）
每步：        streamer.put(next_token)          # 推一个新 token，触发增量打印
全部结束：    streamer.end()                    # 收尾（flush 剩余字符）
```

#### 4.4.3 源码精读

`generate` 里三个 streamer 钩子：

[streamer 钩子 — model/model_minimind.py:L262](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L262)、[L282](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L282)、[L286](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L286)
> 一开始 `streamer.put(input_ids.cpu())` 推入整段 prompt；循环里每步 `streamer.put(next_token.cpu())` 推入新 token；结束 `streamer.end()`。注意都转成 `.cpu()`，因为 streamer 在 CPU 侧解码文字。

eval_llm 里如何构造与使用 streamer：

[构造 TextStreamer — eval_llm.py:L65](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L65)
> `TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)`：`skip_prompt=True` 表示不把开头的 prompt 再打印一遍（毕竟用户自己刚输进去）；`skip_special_tokens=True` 表示不打印 `<|im_end|>`、`<|endoftext|>` 等特殊标记，只显示人类语言。

[调用 generate 并传入 streamer — eval_llm.py:L82-L87](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L82-L87)
> `streamer=streamer` 把上面构造好的流式器注入 `generate`。注意 `generate` 本身返回完整 `generated_ids`，streamer 只负责「边算边打」，两者并不冲突——打印归打印，最终 `response` 仍然从返回值里 decode 出来（见 [eval_llm.py:L88](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L88)）。

> 补充：`TextStreamer` 是「直接 print」的简单实现；如果要把流式输出接到 Web 服务（比如 SSE 推送给前端），可以换成 `TextIteratorStreamer` 配合线程队列，这正是 u8-l2 `serve_openai_api.py` 里 `CustomStreamer` 的思路。本讲先建立「streamer 是可注入的回调」这一认知即可。

#### 4.4.4 代码实践：对比流式与非流式

**实践目标**：直观感受流式输出的「逐字打印」效果。

**操作步骤**：

1. 正常运行 `python eval_llm.py --weight full_sft`，选 [1] 手动输入，观察回答是**逐字蹦出来**的（这就是 streamer 在工作）。
2. **临时**把 [eval_llm.py:L82-L87](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L82-L87) 里的 `streamer=streamer` 改成 `streamer=None`（即不传 streamer），再跑一次。

**需要观察的现象**：
- 传 streamer 时：模型边生成边打印，首字很快出现。
- 不传 streamer 时：终端会卡住一段时间（等全部生成完），然后 `print('🧠: ', end='')` 后面才有内容一次性出现（注意 `🧠:` 是 generate 之前打的，所以会先看到一个空 header 然后等待）。

**预期结果**：流式体验明显更顺畅。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `streamer.put` 传的是 token id 而不是文字？
> **答**：因为分词存在「一个字可能由多个 token 组成」「一个 token 可能是不完整碎片」的情况。TextStreamer 内部维护已解码文本游标，每次 `put` 进新 token 后增量解码，只把「比上次多出来的文字」打印，从而正确处理 token 边界。

**练习 2**：`skip_prompt=True` 是怎么实现的？提示：`generate` 一开始就 `streamer.put(input_ids)` 推了整段 prompt。
> **答**：TextStreamer 收到第一次 `put`（prompt）时，会把它「解码但不打印」，仅用于初始化内部游标（记录「已输出到哪里」），从而实现 `skip_prompt`——prompt 进了游标但没出屏幕，后续 token 才真正打印。

---

## 5. 综合实践

**任务**：写一个最小评测脚本，把本讲的三个核心知识点——KV Cache、采样多样性、tokens/s 统计——串起来。

**要求**：

1. 加载 `full_sft` 权重，对同一条 prompt：
   - 用 `do_sample=True` 生成 3 次（设 `num_return_sequences=3` 一次搞定），观察 3 条输出互不相同；
   - 用 `do_sample=False` 生成 1 次，再跑第二遍，观察两次输出完全一致。
2. 分别在 `use_cache=True` / `use_cache=False` 下用贪心生成等长内容，统计 tokens/s，算出 KV Cache 带来的加速比。
3. 把 3 条采样输出、2 条贪心输出、以及两个 tokens/s 数值打印出来，写一两句结论。

**提示**：

- 复用 `from eval_llm import init_model` 加载模型，参考 4.1.4 的示例代码。
- 采样多样性用 `num_return_sequences`；贪心复现性靠 `do_sample=False`（不依赖随机种子）。
- tokens/s 计时仿照 [eval_llm.py:L81-L91](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L81-L91)：`gen_tokens / elapsed`。
- **结论示例（待本地验证）**：「贪心可复现、采样多样；KV Cache 开启后 tokens/s 约为关闭时的 N 倍」。

这个任务如果你能独立完成并解释清楚每一步现象，说明你已经真正吃透了 `generate` 的自回归循环、KV Cache 增量推理与采样机制。

## 6. 本讲小结

- `MiniMindForCausalLM.generate` 是**自研**的自回归生成方法，几十行 PyTorch 手写循环，绕开了 HF 的 `LogitsProcessor` 体系，透明可学。
- 主循环靠 **KV Cache 增量推理**提速：每步只把「切片后未算过的新 token」喂进 `forward`（首步全量 prefill，之后单 token decode），缓存由 `past_key_values` 在层间传递。
- 采样四件套按 **温度 → 重复惩罚 → top_k → top_p** 顺序改写最后一位 logits，最后 `multinomial` 采样或 `argmax` 贪心；`do_sample` 是切换开关。
- batch 生成时，先吐 EOS 的序列被标记 `finished` 并在后续每步**强制补 EOS**以保持张量对齐，全部 finished 则提前 break。
- `streamer` 是可注入的回调：`generate` 只管每步 `put(token)`，显示策略（终端打印 / Web SSE）交给外部；`eval_llm.py` 用 `TextStreamer(skip_prompt=True)` 实现逐字蹦字。
- 扩展点：`num_return_sequences` 复制 batch 一次出多条；`return_kv=True` 额外返回缓存，供多轮续聊或 rollout 复用。

## 7. 下一步学习建议

- **进入训练侧**：到 u4 单元看训练基础设施。`generate` 是推理，训练用的还是 `forward`（u3-l5）；u4 会讲 DDP、混合精度、检查点续训等支撑训练的公共设施。
- **关注 rollout engine**：u7-l2 的 `rollout_engine.py` 会复用本讲的 `generate` 与 `return_kv` 思路，在强化学习里做「训推分离」的在线采样，届时你会看到 `generate` 在 RL 场景的真正用武之地。
- **延伸阅读源码**：如果想对比「自研 generate」与「HF 标准生成」的差异，可阅读 `transformers` 的 `GenerationMixin.generate` 与 `LogitsProcessor`；本项目刻意只取 `GenerationMixin` 的「协议外壳」（兼容 streamer、config），内核完全自己写。
