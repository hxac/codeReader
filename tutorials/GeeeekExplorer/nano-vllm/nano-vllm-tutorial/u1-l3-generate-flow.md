# 从 generate 到推理主循环

## 1. 本讲目标

上一篇（u1-l2）我们建立了全局代码地图，知道了 `LLMEngine.__init__` 装配了「引擎三件套」——`model_runner`（执行）、`tokenizer`（编解码）、`scheduler`（调度）。本讲要回答的核心问题是：

> 当你调用 `llm.generate(prompts, sampling_params)` 之后，引擎内部到底发生了什么？一条 prompt 是怎么一步步变成输出文本的？

学完本讲，你应当能够：

1. 说出 `generate` 的整体编排流程：编码、入队、循环 `step`、收集结果、解码。
2. 解释一次 `step` 里的三段式职责：`schedule`（调度）→ `run`（前向计算）→ `postprocess`（后处理）。
3. 读懂进度条里 `Prefill xxx tok/s` 与 `Decode xxx tok/s` 这两个吞吐数字是怎么算出来的，以及为什么用同一个 `num_tokens` 变量同时表达 prefill 与 decode 的产出。
4. 理解 `Scheduler` 在每一步是如何决定「跑哪些序列、跑 prefill 还是 decode」的。

本讲只聚焦在**主循环的骨架**上。调度器内部的分块 prefill、抢占等细节留给 u2 单元展开；`model_runner` 内部如何准备张量、如何跑模型，留给 u4 单元展开。本讲里它们都只作为「被调用的黑盒接口」出现。

---

## 2. 前置知识

在进入源码前，先用大白话对齐几个本讲反复出现的概念。

### 2.1 prefill 与 decode：推理的两个阶段

大语言模型生成文本时分两个阶段：

- **prefill（预填）**：处理你给的整段 prompt。模型要一次性「读完」prompt 里的所有 token，并为它们算出并保存 KV Cache。这一步是**计算密集**的，一次处理很多 token，吞吐很高。
- **decode（解码）**：prefill 之后，模型一次只生成**一个**新 token，然后把这个新 token 的 KV 也存下来，再基于已积累的全部 KV 去预测下一个 token。如此反复，直到遇到结束符或达到长度上限。这一步每次只算 1 个 token，是**显存带宽密集**的。

打个比方：prefill 是「通读一遍题目并做好笔记」，decode 是「看着笔记一个字一个字写答案」。

### 2.2 序列（Sequence）与状态

引擎把每一条请求抽象成一个 `Sequence` 对象（下一篇 u2-l1 会专门讲它）。现在只需知道它有三种状态：

- `WAITING`：刚提交，在等待区排队，还没开始 prefill。
- `RUNNING`：prefill 已完成（或正在进行中），正在 decode 产出 token。
- `FINISHED`：生成结束（遇到 eos 或达到 max_tokens）。

每一步循环里，调度器就在 `waiting` 和 `running` 两个队列之间搬动这些序列。

### 2.3 吞吐（throughput）

吞吐 = 单位时间处理的 token 数，单位 `tok/s`。本讲会看到 prefill 和 decode 的吞吐分别统计，因为它们性能特征完全不同，混在一起算没有意义。

数学上就是：

\[
\text{throughput} = \frac{\text{处理的 token 数}}{\text{耗时（秒）}}
\]

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | 推理引擎主体。`generate`、`add_request`、`step` 全部在这里，是本讲的主角。 |
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | 调度器。决定每一步跑哪些序列、跑 prefill 还是 decode，并在前向后做收尾（`postprocess`）。 |
| [nanovllm/engine/sequence.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | `Sequence` 状态对象。本讲只用到它的几个计数字段和 `append_token`，细节留给 u2-l1。 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 真正跑模型的人。本讲只关心它的对外接口 `call("run", ...)` 和 `run` 方法，内部实现是黑盒。 |

可以用一句话串起这条主链：

```
generate()  →  add_request()  →  循环 step()  →  step 内部: schedule() → run() → postprocess()
```

---

## 4. 核心概念与源码讲解

本讲的三个最小模块，正好对应主链上的三个层次：**编排层（generate）**、**迭代层（step）**、**决策层（Scheduler）**。

---

### 4.1 generate：推理的编排层

#### 4.1.1 概念说明

`generate` 是离线推理的总入口。回顾 u1-l1 提到的定位：nano-vllm 只做**离线推理**——你一次性丢进来一批 prompt，它算完之后把结果全部返回给你。`generate` 的职责就是把这个「批量提交 → 反复迭代 → 收集结果」的过程串起来。

它本身**不做计算**，也不碰显存，它只负责「调度时间线」：什么时候入队、什么时候调 `step`、什么时候算吞吐、什么时候把结果解码成文字。

#### 4.1.2 核心流程

`generate` 的执行过程可以用下面这段伪代码描述：

```
function generate(prompts, sampling_params):
    建立进度条（总数 = len(prompts)）
    把 sampling_params 归一化成一个列表，每个 prompt 对应一份
    for (prompt, sp) in zip(prompts, sampling_params):
        add_request(prompt, sp)        # 编码 + 入队
    while not 全部完成:
        记录开始时间
        output, num_tokens = step()    # 跑一步
        根据 num_tokens 的正负更新对应阶段的吞吐
        把这一步完成的序列存进 outputs，进度条 +完成数
    按 seq_id 排序 → 解码成文字 → 返回
```

几个关键设计：

1. **先全部入队，再统一迭代。** 注意 `add_request` 的循环跑完之后，所有 prompt 才都进了 `waiting` 队列；之后才进入 `while` 循环。这是离线推理「一次性给一批」的体现。
2. **吞吐按阶段分别统计。** prefill 步和 decode 步用同一个循环体处理，但只更新各自阶段的吞吐数字。
3. **结果按 `seq_id` 排序。** 保证返回顺序和提交顺序一致。

#### 4.1.3 源码精读

先看 `generate` 的方法签名和进度条初始化：

[llm_engine.py:L60-L68](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L60-L68) —— 这一段创建进度条，并把 `sampling_params` 归一化。如果你只传了一个 `SamplingParams`（而不是列表），第 67–68 行会用 `[sampling_params] * len(prompts)` 复制成与 prompt 数量等长的列表，让每个 prompt 都拿到同一份采样参数。

接着是把所有请求入队的循环：

[llm_engine.py:L69-L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L69-L70) —— 对每对 `(prompt, sp)` 调 `add_request`。`add_request` 的实现在 [llm_engine.py:L43-L47](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L43-L47)：如果传入的是字符串，就先用 `tokenizer.encode` 编码成 token id 列表，然后包成一个 `Sequence` 对象，交给 `scheduler.add(seq)` 塞进 `waiting` 队列。

> 提示：`add_request` 也接受 `list[int]`（已经是 token id 的 prompt），这是为「不经过 chat template、直接控制 token」的高级用法预留的。

然后是核心的迭代循环：

[llm_engine.py:L71-L86](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L71-L86) —— 这是本讲最值得细看的一段。逐行拆解：

- **第 72 行**：`prefill_throughput = decode_throughput = 0.` 初始化两个吞吐变量。注意它俩在循环外只初始化一次——每一步只会刷新其中一个，另一个保留上一次的值，所以进度条上能同时显示「最近一次 prefill 吞吐」和「最近一次 decode 吞吐」。
- **第 73 行**：`while not self.is_finished()`。`is_finished` 直接问调度器（[llm_engine.py:L57-L58](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L57-L58)），其判断标准是 `waiting` 和 `running` 两个队列**都空了**（见 [scheduler.py:L19-L20](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L19-L20)）。只要还有没跑完的序列，就继续。
- **第 74–75 行**：用 `perf_counter()` 记录单步耗时，然后调 `step()`。
- **第 76–79 行**：根据 `num_tokens` 的**正负**决定刷新哪个吞吐。这正是 4.2 节要讲的关键设计。
- **第 80–83 行**：把两个吞吐写进进度条的 `postfix`，也就是你在终端看到的 `Prefill=…tok/s, Decode=…tok/s`。
- **第 84–86 行**：遍历这一步**完成**的序列，把它的 `completion_token_ids` 存进 `outputs` 字典（key 是 `seq_id`），并对进度条 `update(1)`。注意一次 decode 步可能同时结束好几条序列，所以这里用的是循环而不是单个 `+1`。

最后是收尾：

[llm_engine.py:L88-L89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L88-L89) —— 先按 `seq_id` 排序（`seq_id` 来自一个单调递增计数器 `Sequence.counter`，所以排序后顺序 == 提交顺序），再用 `tokenizer.decode` 把每条 token 序列翻译成文字，包成 `{"text": ..., "token_ids": ...}` 的列表返回。这正是 u1-l1 里看到的返回格式。

#### 4.1.4 代码实践

**实践目标**：亲手确认「全部入队先于迭代开始」这件事，并观察进度条的两个吞吐数字。

**操作步骤**：

1. 打开 [example.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py)，确认它调用的是 `llm.generate(prompts, sampling_params)`（第 24 行）。
2. 在你**本地**的 `nanovllm/engine/llm_engine.py` 第 70 行（`add_request` 循环结束处）和第 73 行（`while` 开始处）各加一条 `print`（这是示例代码，仅用于学习，不属于项目原代码）：

```python
        for prompt, sp in zip(prompts, sampling_params):
            self.add_request(prompt, sp)
        print(f"[debug] 已入队 {len(prompts)} 条请求，开始迭代")  # 示例代码
        outputs = {}
        ...
        while not self.is_finished():
            print(f"[debug] 当前 waiting+running 未空，执行一步")  # 示例代码
```

3. 运行 `python example.py`（需要 u1-l1 中准备好的 GPU 环境与 Qwen3-0.6B 权重）。

**需要观察的现象**：

- 「已入队 N 条请求」**只打印一次**，且发生在所有 step 之前，证明是「先全部入队」。
- 「执行一步」会打印很多次，次数远大于请求数（因为 decode 阶段每个 token 一步）。
- 进度条右侧的 `Prefill` 和 `Decode` 数字会分别跳动。

**预期结果**：你能清楚看到入队阶段和迭代阶段是严格分离的两个阶段。若本地无 GPU，此步「待本地验证」，可改为下面的源码阅读型实践。

> 说明：本讲所有「加 print」的实践都要求你修改**自己本地副本**，仅为学习用途。worker 不会、也不应改动仓库源码。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `generate` 第 69–70 行的入队循环删掉（不调用 `add_request`），`while` 循环还会执行吗？

**答案**：不会。因为没有任何序列入队，`waiting` 和 `running` 都是空的，`is_finished()` 一开始就返回 `True`，`while` 一次都不进，直接返回空结果。

**练习 2**：为什么结果要用 `sorted(outputs.keys())` 排序，而不是直接按字典插入顺序？

**答案**：`outputs` 的 key 是 `seq_id`，而 `seq_id` 是全局单调递增的计数器，所以按 `seq_id` 排序等价于按提交顺序排序，保证返回结果与输入 `prompts` 一一对应。虽然 Python 3.7+ 字典保持插入顺序，但显式按 `seq_id` 排序能让语义更清晰、不依赖实现细节。

---

### 4.2 step：一次迭代的「三段式」

#### 4.2.1 概念说明

如果说 `generate` 是「指挥官」，那 `step` 就是「一次冲锋」——它代表引擎的一次迭代。nano-vllm 把一次迭代严格拆成三段，职责清晰分离：

1. **schedule（调度）**：问调度器「这一步跑哪些序列、是 prefill 还是 decode」。
2. **run（前向）**：把调度结果交给 `model_runner`，跑一次模型前向，采样出 token。
3. **postprocess（后处理）**：把采样到的 token 写回各序列，判定哪些序列该结束。

这种「调度 / 计算 / 收尾」三段式是 vLLM 系推理引擎的经典骨架，理解了它，就抓住了引擎的脉搏。

#### 4.2.2 核心流程

```
function step():
    (seqs, is_prefill) = scheduler.schedule()            # ① 决策：跑什么
    num_tokens = Σ seq.num_scheduled_tokens  (prefill)
                 或  -len(seqs)              (decode)    # ② 算这一步产出
    token_ids   = model_runner.call("run", seqs, is_prefill)  # ③ 前向+采样
    scheduler.postprocess(seqs, token_ids, is_prefill)   # ④ 写回+判定结束
    outputs = [已完成序列的 (seq_id, completion_token_ids)]
    return outputs, num_tokens
```

#### 4.2.3 源码精读

`step` 的完整实现只有 7 行，但信息密度极高：

[llm_engine.py:L49-L55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55) —— 逐行拆解：

- **第 50 行**：`seqs, is_prefill = self.scheduler.schedule()`。调度器返回两样东西：本步要跑的序列列表 `seqs`，以及一个布尔值 `is_prefill` 标明本步是 prefill 阶段还是 decode 阶段。`schedule` 的细节见 4.3 节。
- **第 51 行**（本讲最巧妙的一行）：

```python
num_tokens = sum(seq.num_scheduled_tokens for seq in seqs) if is_prefill else -len(seqs)
```

这里用一个变量同时编码了两件事：

- prefill 步：`num_tokens` = 本步所有序列**计划处理的 token 数之和**（正数）。
- decode 步：`num_tokens` = `-len(seqs)`（负数，绝对值是序列数）。

为什么 decode 用「序列数的负数」来表达「token 数」？因为 **decode 阶段每条序列每步只产出 1 个 token**，所以「序列数」就等于「产出 token 数」。于是第 51 行让两种阶段都得到了「本步产出的 token 数」，只是 decode 用负号区分。

- **第 52 行**：`token_ids = self.model_runner.call("run", seqs, is_prefill)`。把调度结果送给执行器，跑一次前向并采样。`call` 是一个通用分发方法（[model_runner.py:L85-L89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L85-L89)）：它通过名字字符串找到对应方法并调用，多卡时还会先通过共享内存把调用广播给其它 worker（u5-l3 会讲）。这里调用的就是 `run`（[model_runner.py:L214-L220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)），它返回每条序列采样到的一个 token id 列表。本讲把它当黑盒即可。
- **第 53 行**：`self.scheduler.postprocess(...)`。把 token 写回序列、判定结束，见 4.3.3。
- **第 54 行**：从本步序列里挑出**已完成**的，组成 `(seq_id, completion_token_ids)` 元组列表。`seq.is_finished` 是 `Sequence` 上的一个属性（[sequence.py:L39-L41](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L39-L41)），当状态变为 `FINISHED` 时为真。

现在把第 51 行和 `generate` 的吞吐统计接起来看，整条逻辑就闭环了：

[llm_engine.py:L76-L79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L76-L79) —— `if num_tokens > 0`（prefill，正数）就算 `prefill_throughput = num_tokens / Δt`；`else`（decode，负数）就算 `decode_throughput = -num_tokens / Δt`。注意 decode 分支里那个**负号**，正好抵消了第 51 行加上的负号，于是：

\[
\text{decode\_throughput} = \frac{-\,num\_tokens}{\Delta t} = \frac{\text{len}(seqs)}{\Delta t}
\]

也就是「每秒产出的 decode token 数」。一个正负号的复用，省下了一个额外的 `is_prefill` 判断和两个变量，非常精炼。

#### 4.2.4 代码实践

**实践目标**：观察一次推理中 prefill 步与 decode 步的节奏，理解「prefill 一步处理很多 token、decode 一步每序列只出 1 个 token」。

**操作步骤**：

1. 在你**本地副本**的 `step` 方法里，在第 50 行之后加一行日志（示例代码）：

```python
    def step(self):
        seqs, is_prefill = self.scheduler.schedule()
        num_tokens = sum(seq.num_scheduled_tokens for seq in seqs) if is_prefill else -len(seqs)
        # 示例代码：打印每一步的类型与规模
        print(f"[step] {'PREFILL' if is_prefill else 'DECODE'} | 序列数={len(seqs)} | num_tokens={num_tokens}")
        token_ids = self.model_runner.call("run", seqs, is_prefill)
        ...
```

2. 运行 `python example.py`（example.py 里 `max_tokens=256`，2 条 prompt）。

**需要观察的现象与预期结果**（待本地验证）：

- 一开始会出现一条 `PREFILL` 步（把 prompt 一次性处理掉），`num_tokens` 是个较大的正数。
- 之后是一长串 `DECODE` 步，每一步 `序列数=2`（两条 prompt 同时 decode），`num_tokens=-2`。
- 直到两条序列都达到 256 token 或遇到 eos，循环结束。
- 你会直观看到：**prefill 只有寥寥几步，decode 占了绝大多数步数**——这正是为什么 decode 效率对总延迟影响最大。

**源码阅读型替代实践**（无需 GPU）：不运行，直接对照 [llm_engine.py:L51](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L51) 和 [llm_engine.py:L76-L79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L76-L79)，在纸上推演：假设一次 decode 步有 3 条序列、耗时 0.02 秒，手算 `num_tokens` 与 `decode_throughput`，验证得到 `150 tok/s`。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 51 行的 `-len(seqs)` 改成 `len(seqs)`（去掉负号），进度条上的 Decode 吞吐会变成什么？程序会报错吗？

**答案**：不会报错，但语义会错。`num_tokens` 变成正数后，`generate` 的 `if num_tokens > 0` 分支会误判 decode 步为 prefill，去刷新 `prefill_throughput`，而 `decode_throughput` 永远停在 0。这说明那个负号是「区分两种阶段」的关键约定，不是可有可无的。

**练习 2**：一次 decode 步里，`model_runner.call("run", ...)` 返回的 `token_ids` 列表长度，和 `seqs` 的长度有什么关系？

**答案**：长度相等。因为 decode 阶段每条序列本步只产出 1 个 token，所以返回列表里第 i 个 token 对应 `seqs[i]`。`postprocess` 正是用 `zip(seqs, token_ids)` 把它们一一配对的（见 4.3.3）。

---

### 4.3 Scheduler：每一步「跑什么」的决策者

#### 4.3.1 概念说明

`Scheduler` 是 `step` 三段式里第一段（schedule）和第三段（postprocess）的真正实现者。它的核心职责只有一个：

> 在每一步开始时，从 `waiting` / `running` 两个队列里挑出一批序列，并决定本步是 prefill 还是 decode。

挑序列时要受两个硬约束（来自 [config.py:L9-L10](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L9-L10)）：

- `max_num_seqs`：同时跑的序列数上限（默认 512）。
- `max_num_batched_tokens`：一步里 prefill 处理的 token 总数上限（默认 16384）。

调度策略的灵魂是六个字：**prefill 优先于 decode**。只要 `waiting` 队列里还有能调度的序列，这一步就做 prefill；只有 `waiting` 空了（或 prefill 这一轮没调度到任何序列），才转去做 decode。这样能让新请求尽快进入「能产出 token」的 decode 状态。

#### 4.3.2 核心流程

`schedule` 的决策流程：

```
function schedule():
    scheduled = []
    # —— 阶段一：尝试 prefill ——
    while waiting 非空 且 len(scheduled) < max_num_seqs:
        取 waiting 队首 seq
        算还能塞多少 token（remaining = max_num_batched_tokens - 已占）
        若 remaining == 0：break
        算这条 seq 这一步要处理多少 token
        若塞不下 且 scheduled 非空：break（只允许首条切片）
        分配块、记 num_scheduled_tokens
        若整条 prompt 处理完：状态置 RUNNING，从 waiting 移到 running
        scheduled.append(seq)
    if scheduled 非空:
        return scheduled, True          # ← 本步是 prefill

    # —— 阶段二：decode ——
    while running 非空 且 len(scheduled) < max_num_seqs:
        取 running 队首 seq
        检查显存能否再 append（不行就 preempt 别的序列腾地方）
        seq.num_scheduled_tokens = 1；is_prefill = False
        scheduled.append(seq)
    把 scheduled 重新放回 running 队首
    return scheduled, False             # ← 本步是 decode
```

返回值元组 `(seqs, is_prefill)` 的第二个元素，正是 `step` 第 50 行拿到的那个布尔值。

#### 4.3.3 源码精读

先看 `schedule` 的 prefill 阶段：

[scheduler.py:L29-L55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L29-L55) —— 逐段理解：

- **第 30 行**：`while self.waiting and len(scheduled_seqs) < self.max_num_seqs`，两个条件任一不满足就停。
- **第 32 行**：`remaining = self.max_num_batched_tokens - num_batched_tokens`，追踪本步还能塞多少 prefill token。
- **第 35–41 行**：算这条序列这一步要处理的 token 数。涉及前缀缓存命中（`num_cached_tokens`）和块分配，这部分逻辑牵涉 `BlockManager`，属于 u3 单元内容，本讲先认为「算出本步要新算 `num_tokens` 个 token」即可。
- **第 42–43 行**：`if remaining < num_tokens and scheduled_seqs`，这是**分块 prefill（chunked prefill）**的开关——只有当 `scheduled_seqs` 还为空（即处理的是第一条序列）时，才允许把一条超长 prompt 切片处理；如果已经有别的序列在篮子里了，就不再硬塞。分块与抢占细节见 u2-l3。
- **第 46–47 行**：`seq.num_scheduled_tokens = min(num_tokens, remaining)` 决定本步真正处理多少个 token，并累加进 `num_batched_tokens`。这个 `num_scheduled_tokens` 正是 `step` 第 51 行求和的那个字段。
- **第 48–51 行**：如果这条 prompt 的 token 已全部被调度（缓存命中数 + 本次调度数 == 总数），就把状态从 `WAITING` 置为 `RUNNING`，并把它从 `waiting` 搬到 `running`——这意味着它下一步就有资格参与 decode 了。
- **第 54–55 行**：只要这一轮 prefill 调度到了序列，就立刻 `return scheduled_seqs, True`，**根本不会**走到 decode 阶段。这就是「prefill 优先」。

再看 decode 阶段：

[scheduler.py:L57-L73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L57-L73) —— 只有 prefill 阶段什么都没调度到时才会执行：

- **第 58 行**：遍历 `running` 队列，同样受 `max_num_seqs` 约束。
- **第 60–65 行**：`while not self.block_manager.can_append(seq)` 检查显存够不够再为这条序列追加一个 token 的 KV。不够就调 `preempt` 抢占别的 running 序列（或抢占自己）腾出显存。`preempt`（[scheduler.py:L75-L79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79)）会把序列打回 `WAITING`、释放它的块、重新塞回 `waiting` 队首。抢占机制详见 u2-l3。
- **第 67–69 行**：每条 decode 序列本步只处理 1 个 token，所以 `num_scheduled_tokens = 1`，并把 `is_prefill` 置为 `False`。
- **第 72 行**：`self.running.extendleft(reversed(scheduled_seqs))` 把刚处理的一批序列按原顺序塞回 `running` 队首，准备下一轮 decode。
- **第 73 行**：`return scheduled_seqs, False`，本步是 decode。

最后是 `postprocess`——`step` 三段式的收尾：

[scheduler.py:L81-L92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L81-L92) —— 对每对 `(seq, token_id)`：

- **第 83 行**：`self.block_manager.hash_blocks(seq)` 把这条序列新写入的块登记进哈希表，为**前缀缓存**服务（u3-l2 详解）。
- **第 84–85 行**：把刚处理的 token 数累加进 `num_cached_tokens`（表示「这些 token 的 KV 已经存好了」），并把 `num_scheduled_tokens` 清零，为下一步做准备。
- **第 86–87 行**：`if is_prefill and seq.num_cached_tokens < seq.num_tokens: continue`——这是分块 prefill 的延续：如果这条 prompt 还没全部处理完，就先**不**采样新 token，跳过本次追加，等下一轮 prefill 继续切片。
- **第 88 行**：`seq.append_token(token_id)` 把采样到的新 token 追加进序列（见 [sequence.py:L67-L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L67-L70)，会更新 `token_ids`、`last_token`、`num_tokens`）。
- **第 89–92 行**：判定结束——遇到 eos（且 `ignore_eos` 为假）或生成数达到 `max_tokens`，就把状态置为 `FINISHED`，释放它的块，并从 `running` 移除。被置为 `FINISHED` 的序列，正是 `step` 第 54 行通过 `is_finished` 属性挑出来上报给 `generate` 的那些。

把 4.1、4.2、4.3 串起来，一条请求的完整生命是：

```
add_request → WAITING
  → schedule(prefill) 处理 prompt → RUNNING
    → schedule(decode) 每步 +1 token
      → postprocess 检测到 eos/max_tokens → FINISHED → 被 step 上报 → generate 收集
```

#### 4.3.4 代码实践

**实践目标**：验证「prefill 优先」与「decode 每步每序列 1 个 token」这两条调度规律。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [scheduler.py:L25-L73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L25-L73)。
2. 假想初始状态：`waiting = [A, B]`（两条 prompt），`running = []`，`max_num_seqs=512`，`max_num_batched_tokens=16384`，两条 prompt 都远短于 16384 token。
3. 在纸上推演连续两次调用 `schedule()`：
   - 第 1 次：进入 prefill 阶段，A 和 B 都能塞下（token 总和 < 16384）。两条都被调度，A、B 状态变 `RUNNING`，进入 `running`。返回 `([A, B], True)`。
   - 第 2 次：`waiting` 已空，prefill 阶段 `while` 不进，`scheduled_seqs` 为空，跳到 decode 阶段。从 `running` 取出 A、B，各 `num_scheduled_tokens=1`。返回 `([A, B], False)`。

**需要观察的结论**：

- 第 1 次返回 `is_prefill=True`，一次性把两条 prompt 都处理了。
- 第 2 次起，只要没有新请求入队，每次都返回 `is_prefill=False`，且每条序列只前进 1 个 token——这与 4.2.4 在运行时观察到的 `DECODE | 序列数=2 | num_tokens=-2` 完全吻合。

**预期结果**：你能用调度规则准确预测每一步的 `(seqs, is_prefill)`，而不需要真的跑模型。

#### 4.3.5 小练习与答案

**练习 1**：假设 `waiting` 里有 3 条请求，但它们的 prompt token 总和超过了 `max_num_batched_tokens`。第一次 `schedule()` 会把 3 条都返回吗？

**答案**：不一定。prefill 阶段会逐条尝试塞入，累计 token 数一旦达到 `max_num_batched_tokens`（`remaining == 0`，第 33–34 行 break），就停止追加。所以可能只返回前 1～2 条，剩下的留在 `waiting` 里等下一步 prefill。而且根据第 42–43 行，只有第一条序列允许被切片（分块 prefill）。

**练习 2**：`postprocess` 里第 86–87 行的 `continue` 跳过了 `append_token`。这意味着什么？被跳过的序列此时处于什么状态？

**答案**：这意味着这是一次还没把整条 prompt 处理完的 prefill 步（分块 prefill 的中间步），本步还**不该**采样新 token，所以不调用 `append_token`。被跳过的序列状态仍是 `WAITING`（因为只有 `num_cached_tokens + num_scheduled_tokens == num_tokens` 时才会被置为 `RUNNING`，第 48–49 行），它会继续留在 `waiting` 队列里，等下一次 prefill 把剩余 token 处理完。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「为引擎装一个简易仪表盘」的小任务。

**任务**：在你本地的 `llm_engine.py` 中，给 `step` 方法增加一段统计代码，让它在每次推理结束后，打印一份摘要：总步数、prefill 步数、decode 步数、总产出 token 数。

**参考实现（示例代码，仅供学习）**：

```python
    # 在 __init__ 末尾加（示例代码）
    self._step_log = {"prefill": 0, "decode": 0, "tokens": 0}

    def step(self):
        seqs, is_prefill = self.scheduler.schedule()
        num_tokens = sum(seq.num_scheduled_tokens for seq in seqs) if is_prefill else -len(seqs)
        # 示例代码：仪表盘统计
        if is_prefill:
            self._step_log["prefill"] += 1
            self._step_log["tokens"] += num_tokens
        else:
            self._step_log["decode"] += 1
            self._step_log["tokens"] += -num_tokens
        token_ids = self.model_runner.call("run", seqs, is_prefill)
        self.scheduler.postprocess(seqs, token_ids, is_prefill)
        outputs = [(seq.seq_id, seq.completion_token_ids) for seq in seqs if seq.is_finished]
        return outputs, num_tokens
```

然后在 `generate` 的 `pbar.close()` 之后（[llm_engine.py:L87](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L87) 附近）加一行：

```python
        print(f"[仪表盘] prefill步={self._step_log['prefill']} decode步={self._step_log['decode']} 总token={self._step_log['tokens']}")
```

**运行与观察**（待本地验证，需 GPU + 模型）：

1. 跑 `python example.py`。
2. 你应当看到 decode 步数远大于 prefill 步数。
3. 把 example.py 里的 `max_tokens` 从 256 调小到 16，再跑一次，观察 decode 步数随之下降——直观验证「decode 步数 ≈ max_tokens × 请求数 / 每步并发序列数」。

**思考题**：仪表盘里的「总 token」包括了 prefill 处理的 prompt token 和 decode 产出的 token 两部分。如果你只想统计 decode 产出的 token，应该怎么改？（提示：只累加 `else` 分支里的 `-num_tokens`。）

---

## 6. 本讲小结

- `generate` 是离线推理的**编排层**：先把所有 prompt 经 `add_request` 编码入队，再 `while not is_finished()` 反复调 `step`，最后按 `seq_id` 排序、解码返回。
- `step` 是一次迭代的**三段式**：`schedule`（决策跑什么）→ `run`（前向+采样）→ `postprocess`（写回 token + 判定结束）。
- 第 51 行 `num_tokens = ... if is_prefill else -len(seqs)` 用一个变量的**正负号**同时编码了「阶段类型」和「产出 token 数」，配合 `generate` 的 `if num_tokens > 0` 实现了 prefill/decode 吞吐的分别统计。
- `Scheduler.schedule` 遵循 **prefill 优先于 decode**：只要 `waiting` 有可调度序列就做 prefill 并立即返回 `True`，否则才处理 `running` 做 decode 返回 `False`。
- decode 阶段每条序列每步只产出 1 个 token（`num_scheduled_tokens = 1`），所以「序列数」就等于「decode token 数」，这正是 `-len(seqs)` 语义成立的根基。
- `postprocess` 通过检测 eos / 达到 `max_tokens` 把序列置为 `FINISHED`，这些完成的序列被 `step` 上报给 `generate`，最终进入返回结果。

---

## 7. 下一步学习建议

本讲把推理主循环的骨架讲清楚了，但有意留下了几个「黑盒」。接下来建议：

- **进入 u2 单元（调度与请求管理）**：
  - u2-l1 会深入 `Sequence` 的内部结构（`num_tokens` / `num_cached_tokens` / `num_scheduled_tokens` 三个计数字段的精确含义、`__getstate__`/`__setstate__` 为多进程通信做的精简），把本讲里「先用着」的序列字段彻底搞懂。
  - u2-l2 会展开 `schedule` 里 `max_num_seqs` 与 `max_num_batched_tokens` 的约束博弈细节。
  - u2-l3 会讲本讲反复提到但没展开的**分块 prefill**与**抢占（preempt）**机制。
- **如果对执行侧好奇**：可以提前跳读 `model_runner.py` 的 `run` 方法（[model_runner.py:L214-L220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)），看看 `step` 第 52 行那个黑盒里到底做了什么，正式讲解在 u4-l1。
- **建议动手**：在进入下一篇前，先把本讲第 5 节的「仪表盘」综合实践做一遍，亲手看到 prefill/decode 的步数对比，会对后续理解调度优化有直觉上的帮助。
