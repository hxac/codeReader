# 采样与后处理

## 1. 本讲目标

在上一讲（u3-l2 prefill 与 decode 推理主流程）里，我们已经看到一次前向的终点是 `_sample_and_scatter_token`：模型算出每个请求在词表上的原始分数 `logits`，剩下的工作就是「从 logits 里挑出下一个 token」。本讲专门打开这个「挑 token」的黑盒。

学完本讲你应该能够：

- 说清楚 `sample()` 这个统一入口的完整执行顺序：**惩罚项 → 温度 → softmax → 采样**。
- 解释三类惩罚项（repetition / frequency / presence）各自的数学含义，以及它们是如何在 Triton kernel 里改写 logits 的。
- 理解 top-k、top-p（nucleus）两种过滤策略的实现，以及随机采样背后的 Gumbel-max 技巧。
- 认识 LightLLM 把「采样参数」放进 GPU 常驻 buffer、用 Triton kernel 批量处理的工程做法。

本讲覆盖三个最小模块：**采样**、**惩罚项**、**top-k/top-p**。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。

- **logits**：模型最后一层 `lm_head` 的输出，形状是 `[batch_size, vocab_size]`，每个值是该 token 的「未归一化分数」。它不是概率，可正可负。
- **softmax**：把一组实数变成概率分布（非负、求和为 1）。对 logits 做 softmax 得到每个 token 被选中的概率。

  \[ \mathrm{prob}_i = \frac{\exp(\mathrm{logit}_i)}{\sum_j \exp(\mathrm{logit}_j)} \]

- **采样（sampling）**：根据概率分布随机抽一个 token。概率越大的 token 越容易被抽中，但小概率 token 也有机会，从而产生多样性。
- **贪心解码（greedy）**：不随机，永远选概率最大的那个 token（`argmax`）。确定性最强、多样性为零。
- **温度（temperature）**：在 softmax 之前把 logits 除以一个数 \(T\)。\(T>1\) 让分布更平坦（更随机），\(T<1\) 让分布更尖锐（更确定）。
- **惩罚项（penalty）**：在采样前对「已经出现过的 token」的 logits 做调整，避免重复（repetition）、避免高频词刷屏（frequency / presence）。

一个关键直觉：**所有这些操作都是对 logits 的「就地改写」或「先改写再采样」，顺序非常重要**。本讲后面会反复回到这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [generic_post_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py) | 采样主入口 `sample()`、top-k/top-p 过滤、随机采样、批参数组装 |
| [apply_penalty.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty.py) | cpu_counter 模式下的惩罚 Triton kernel（稀疏处理「出现过的 token」） |
| [apply_penalty_gpu_cache.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty_gpu_cache.py) | gpu_counter 模式下的惩罚 kernel（稠密处理整个词表） |
| [gen_sampling_params.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gen_sampling_params.py) | 采样参数的批化 gather、token 计数 buffer 的初始化与更新 |
| [req_manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/req_manager.py) | `ReqSamplingParamsManager`：采样参数与 token 计数的 GPU 常驻管理器 |
| [sampling_params.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py) | `SamplingParams` 数据结构：temperature/top_p/top_k/各 penalty 的字段定义与默认值 |

## 4. 核心概念与源码讲解

### 4.1 采样主流程：sample 函数（采样模块）

#### 4.1.1 概念说明

`sample()` 是所有模型、所有后端（normal / chunked_prefill / dp / diverse）共用的唯一采样入口。它接收「一批请求的 logits」和「这批请求对象」，返回「每个请求下一个 token 的 id 以及它的对数概率」。它把惩罚、温度、softmax、过滤、随机抽取这几件事按固定顺序串起来，因此理解了它就理解了 LightLLM 采样的全貌。

它被 `_sample_and_scatter_token` 调用，后者在 [base_backend.py:L803](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L788-L825) 里这样使用：

```python
next_token_ids, next_token_logprobs = sample(logits, run_reqs, self.eos_id)
```

#### 4.1.2 核心流程

`sample()` 的执行顺序（务必记住这个顺序，它是本讲的主线）：

1. **组装批参数**：从每个请求对象上读出 temperature / top_p / top_k / 各 penalty / eos 屏蔽等，拼成 GPU 张量；同时统计几个「全局快路径」布尔量（是否全部贪心、是否可跳过 top-k、是否可跳过 top-p）。
2. **应用惩罚项**：根据 `penalty_counter_mode` 选择 cpu 或 gpu 的 Triton kernel，**就地改写 logits**（详见 4.2）。
3. **屏蔽非法 token**：若有请求声明了 `invalid_token_ids`，把它们的 logit 置为 `-inf`。
4. **温度 + softmax**：`logits /= temperature`，再 `softmax` 得到概率 `probs`。
5. **挑 token**：分三条快路径——
   - 全部贪心 → `argmax`；
   - 跳过 top-k 且跳过 top-p → 直接对 `probs` 随机采样；
   - 否则 → 走完整的 top-k/top-p 过滤再采样（详见 4.3）。

用伪代码表示：

```
sample(logits, reqs, eos_id):
    tensors, flags = _get_post_sample_tensors(reqs)   # 组装批参数 + 快路径标志
    apply_penalty*(logits, ...)                        # 就地改写 logits（惩罚）
    if has_invalid_token_ids:
        apply_invalid_token_ids(logits, ...)           # 置 -inf
    logits.div_(temperature)                           # 温度
    probs = softmax(logits)                            # 概率
    if is_all_greedy:        return argmax(logits)     # 快路径 1
    elif skip_top_k & skip_top_p: return random_sample # 快路径 2
    else:                    return top_p_top_k_sample # 完整路径
```

#### 4.1.3 源码精读

入口签名与第一步组装批参数：[generic_post_process.py:L11-L27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L11-L27)。`_get_post_sample_tensors` 一次性返回 13 个值——6 个数值张量、2 个非法 token 相关张量、5 个布尔标志。

惩罚项分 cpu/gpu 两条路：[generic_post_process.py:L43-L69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L43-L69)。注意源码里有一段很长的中文注释，解释了为什么要分两种统计模式——这正好是 4.2 要展开的内容。

温度、softmax 与三条采样快路径：[generic_post_process.py:L78-L96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L78-L96)。注意三个判断条件的优先级：**贪心优先级最高**，其次是「无需过滤」的纯随机，最后才是完整的 top-k/top-p。

批参数组装里最值得记住的是这几个「快路径」标志，它们决定了走哪条采样分支：[generic_post_process.py:L164-L194](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L164-L194)。

```python
is_all_greedy = True
skip_top_k = True
skip_top_p = True
...
for i, req_obj in enumerate(reqs):
    ...
    if top_k_val > 1:        # 只要有一个请求 top_k>1，整批就不是纯贪心
        is_all_greedy = False
    if top_k_val != req_obj.vocab_size:   # 只要有一个 top_k 没占满词表，就不能跳过 top-k
        skip_top_k = False
    if shm_param.top_p != 1.0:            # 只要有一个 top_p≠1，就不能跳过 top-p
        skip_top_p = False
```

> **重要结论（初学者常踩坑）**：`SamplingParams` 的默认 `top_k = -1`（表示「全部 token」），见 [sampling_params.py:L316](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py#L310-L316)。而 `-1 > 1` 为假，所以 `is_all_greedy` 保持 `True`。也就是说——**用默认参数启动时，LightLLM 走的是贪心 `argmax` 解码，不是随机采样**。要让模型真正「随机采样」，请求里必须显式设置 `top_k > 1`（或携带随机 `seed`）。

#### 4.1.4 代码实践

**实践目标**：在不运行模型的前提下，用源码阅读追踪一次采样的完整流程。

**操作步骤**：

1. 打开 [base_backend.py:L788-L825](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L788-L825)，确认 `sample(logits, run_reqs, self.eos_id)` 的调用点。
2. 打开 [generic_post_process.py:L11-L96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L11-L96)，从上到下标出 5 个阶段的行号。
3. 在 `_get_post_sample_tensors` 里找到决定 `is_all_greedy` / `skip_top_k` / `skip_top_p` 的三行。

**需要观察的现象**：

- 惩罚项发生在 `softmax` **之前**（对 logits 操作），过滤/采样发生在 `softmax` **之后**（对 probs 操作）。
- 三条采样分支互斥，按 `is_all_greedy` → `skip_top_k & skip_top_p` → `top_p_top_k` 优先级判定。

**预期结果**：你能画出一张「logits → penalty → invalid mask → /temperature → softmax → 分支采样」的流水线图，并标注每一步对应的行号。

> 待本地验证：若你有 GPU 环境，可用 `/generate` 分别发送 `{"temperature":0}` 与 `{"temperature":0.8,"top_k":50,"top_p":0.9}` 两次请求，对比前者（贪心，每次输出相同）与后者（随机，每次输出不同）的现象，从而印证快路径分支。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `apply_penalty` 那段代码整个删掉，`sample()` 还能正常返回 token 吗？结果会变成什么样？

**参考答案**：能正常返回，但「重复/频率/存在」三类惩罚全部失效，模型生成更易陷入重复循环。因为惩罚只是改写 logits，删掉它等于所有 penalty 取默认值（presence=0、frequency=0、repetition=1，即无操作），后面的温度、softmax、采样流程不受影响。

**练习 2**：为什么 `is_all_greedy` 的判定条件是 `top_k_val > 1`，而不是 `top_k_val == 1`？

**参考答案**：`top_k = 1` 表示只考虑概率最高的 1 个 token，等价于 `argmax`（贪心）；`top_k = -1`（默认，表示全部 token）虽然语义上是「不过滤」，但默认参数下用户其实想要确定性输出，故也被归入贪心快路径。只有 `top_k > 1`（真正要在一个受限候选集里随机抽）才算「非贪心」。

---

### 4.2 惩罚项如何改写 logits（惩罚项模块）

#### 4.2.1 概念说明

为了控制生成文本的重复度，主流推理框架都提供三类「token 出现次数」相关的惩罚。LightLLM 把它们在采样前一次性作用到 logits 上：

- **repetition_penalty（重复惩罚，默认 1.0）**：对「已经出现过的 token」，按比例缩小或放大其 logit。`>1` 表示抑制已出现 token。
- **frequency_penalty（频率惩罚，默认 0.0）**：按 token 出现的**次数**线性下调 logit，出现越多下调越狠。
- **presence_penalty（存在惩罚，默认 0.0）**：只要 token **出现过**（不论几次），就下调一个固定值。

还有一个专门作用在 eos（结束符）上的 **length penalty（长度惩罚）**：通过指数衰减鼓励/推迟模型输出 eos，从而控制生成长度。这些字段定义在 [sampling_params.py:L263-L308](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py#L263-L308)。

#### 4.2.2 核心流程

对一个「已经出现过」、出现次数为 \(c\) 的 token，其原始 logit 记为 \(x\)。LightLLM 的 cpu 模式 kernel 按下面顺序改写（三步串行）：

1. **repetition**（比例惩罚）：

   \[ x' = \begin{cases} x / r & x > 0 \\ x \cdot r & x \le 0 \end{cases} \]

   其中 \(r\) 是 repetition_penalty。注意正负 logit 处理方式不同，是为了保证「缩小绝对值」的语义在正负两侧都成立。

2. **frequency**（按次数线性惩罚）：

   \[ x'' = x' - c \cdot f \]

   \(f\) 是 frequency_penalty，\(c\) 是该 token 的累计出现次数。

3. **presence**（固定惩罚）：

   \[ x''' = x'' - p \]

   \(p\) 是 presence_penalty（与次数无关，出现过就扣固定值）。

对 eos 的长度惩罚（鼓励/抑制提前结束）：

\[ \text{scale} = 2^{\log_2(d) \cdot L} - 1 \]

\[ x_{\text{eos}} \leftarrow x_{\text{eos}} + |x_{\text{eos}}| \cdot \text{scale} \]

其中 \(d\) 是衰减因子（`exponential_decay_length_penalty` 的第二项，默认 1.0 → scale=0 → 无操作），\(L\) 是「超过起始长度的已生成 token 数」。当某个请求还未达到 `min_new_tokens`，则其 eos 被**强制置为 -10000000**（禁止提前结束）。

**两种计数模式**。惩罚需要知道「每个 token 出现过几次」。LightLLM 提供两种统计方式（由 `--penalty_counter_mode` 控制，默认 `gpu_counter`）：

- **cpu_counter**：每个请求在 CPU 上维护一个 `dict[token_id] -> count`（`collections.Counter`），采样时把整批的 `(token_ids, counts)` 拼成稀疏数组传给 kernel。优点省显存，缺点是长输出/高并发时 CPU 组 batch 成为瓶颈。kernel 只需遍历「出现过的 token」，是**稀疏**的。
- **gpu_counter**：每个请求预分配一个 `vocab_size` 大小的 GPU 计数 buffer，每生成一个 token 就用 atomic add 更新。kernel 直接在整个词表上稠密扫描。优点快，缺点是显存开销大（注释里举例：词表 50 万 × 1000 请求 × int32 ≈ 600MB）。

#### 4.2.3 源码精读

cpu 模式的核心 kernel，repetition/frequency/presence 三步对应这三行：[apply_penalty.py:L48-L52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty.py#L43-L52)。

```python
rep_logits  = tl.where(cur_logits > 0, cur_logits / cur_repetition, cur_logits * cur_repetition)
freq_logits = rep_logits - token_ids_count * cur_freqency
pre_logits  = freq_logits - cur_presence
```

注意 `token_ids` / `token_ids_count` 来自 `p_token_ids` / `p_token_counts`，它们只包含「本请求出现过的 token」，所以这个 kernel 是**稀疏**的——只改写必要的几行 logit。

紧随其后的 eos 长度惩罚：[apply_penalty.py:L54-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty.py#L54-L65)。其中 `mask_eos != 0`（即 `mask_eos_reqs` 为真，表示还没到 `min_new_tokens`）时把 eos logit 写成 `-10000000.0`，禁止提前收尾。

gpu 模式把同样的数学做成稠密 kernel（在整个词表上扫描），并对 repetition 加了「只对出现过的 token 生效」的保护：[apply_penalty_gpu_cache.py:L41-L46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty_gpu_cache.py#L41-L46)。

```python
p_logits = tl.where(origin_logits > 0, origin_logits / cur_repetition, origin_logits * cur_repetition)
p_logits = tl.where(token_ids_count > 0, p_logits, origin_logits)   # 没出现过的 token 不受 repetition 影响
p_logits = p_logits - token_ids_count * cur_freqency
p_logits = p_logits - tl.where(token_ids_count > 0, cur_presence, 0.0)
```

这里的差别在于：gpu 模式扫描整个词表，所以必须用 `tl.where(token_ids_count > 0, ...)` 显式跳过「没出现过的 token」；而 cpu 模式天然只遍历出现过的 token，不需要这层保护。

gpu 模式把 eos 长度惩罚单独拆成一个 kernel `_eos_penalty`：[apply_penalty_gpu_cache.py:L50-L81](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty_gpu_cache.py#L50-L81)。数学与 cpu 模式完全一致。

**这些 buffer 在哪里？** 采样参数与计数 buffer 由 `ReqSamplingParamsManager` 统一管理，它在构造时就按 `penalty_counter_mode` 预分配了 GPU/pinned 内存：[req_manager.py:L104-L135](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/req_manager.py#L104-L135)。

还有一个省力优化：如果一个请求的三类惩罚全是默认值（presence=0 & frequency=0 & repetition=1），就根本不需要统计 token 计数，`need_out_token_id_statistics` 会被置为 `False`：[req_manager.py:L147-L151](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/req_manager.py#L145-L151)。

```python
req.need_out_token_id_statistics = not (
    shm_param.presence_penalty == 0.0
    and shm_param.frequency_penalty == 0.0
    and shm_param.repetition_penalty == 1.0
)
```

token 计数的「写入」由两个 Triton kernel 完成：初始化时统计 prompt 的 token（仅当 `input_penalty=True`，默认 `False`，即默认只惩罚输出 token、不惩罚输入）见 [gen_sampling_params.py:L82-L114](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gen_sampling_params.py#L82-L114)；每生成一个 token 后原子累加见 [gen_sampling_params.py:L156-L179](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gen_sampling_params.py#L156-L179)。

#### 4.2.4 代码实践

**实践目标**：用一个独立的 numpy 小例子，亲手验证 repetition + frequency + presence 三步公式，确认与 kernel 行为一致。

**操作步骤**（示例代码，可脱离 LightLLM 单独运行）：

```python
# 示例代码：手工复现 apply_penalty 对单个 token 的三步改写
import numpy as np

def apply_penalty_one(logit, count, repetition, frequency, presence):
    x = logit / repetition if logit > 0 else logit * repetition
    x = x - count * frequency
    x = x - presence
    return x

print(apply_penalty_one(logit=2.0, count=3, repetition=1.2, frequency=0.5, presence=0.2))
# 等价于 kernel: 2.0/1.2 - 3*0.5 - 0.2
print(2.0/1.2 - 3*0.5 - 0.2)
```

**需要观察的现象**：

- `logit` 为正时，repetition 用除法（缩小正值）；`logit` 为负时用乘法（让负值更负）。两种情况都会让该 token 在 softmax 后概率更低，达到「抑制已出现 token」的效果。
- frequency 与 count 成正比，count=0 时这一项无贡献。
- presence 与 count 无关，是固定值。

**预期结果**：手工公式与 kernel 的 `rep_logits - count*freq - presence` 完全一致。

> 待本地验证：若启动了服务，可对比 `--penalty_counter_mode cpu_counter` 与 `gpu_counter` 两种模式在相同 `frequency_penalty=0.5` 下的输出，理论上数值应当一致（只是性能不同）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 repetition penalty 在 logit 为正时用除法、为负时用乘法？

**参考答案**：repetition penalty 的目标是「抑制已出现过的 token」。对正 logit，除以 \(r>1\) 会变小，降低其相对优势；对负 logit，乘以 \(r>1\) 会让它更负。两种情况在 softmax 后都表现为「该 token 的概率变得更低」，从而达到了抑制效果。之所以对正负用不同运算（而非统一用除法或乘法），是因为统一运算会破坏这个「双向削弱」的性质：例如对负 logit 也用除法（\(-2 / 1.2\)）反而会让它变大方、概率升高，违背抑制初衷。

**练习 2**：`need_out_token_id_statistics` 这个标志为 `False` 时，会省掉什么工作？

**参考答案**：省掉每生成一个 token 后的计数更新（`update_req_to_token_id_counter`），也省掉采样时为该请求拼装 `(token_ids, counts)`。因为三类惩罚都是默认值（无操作），统计 token 出现次数纯属浪费，提前标记可跳过。

---

### 4.3 top-k / top-p 过滤与随机采样（top-k/top-p 模块）

#### 4.3.1 概念说明

经过惩罚、温度、softmax 之后，我们拿到了完整的概率分布 `probs`。接下来要从中挑一个 token。直接按 `probs` 随机抽样会让大量低概率的「噪音 token」也有机会被选中，导致输出胡言乱语。两种最常见的过滤策略：

- **top-k**：只保留概率最高的 k 个 token，把其余 token 的概率清零，再在剩下的里归一化采样。k 越小越保守。
- **top-p（nucleus sampling，核采样）**：把 token 按概率从大到小排序，累加，取**累计概率首次达到 p** 的那个最小集合（「核」），其余清零。p 越小越保守。它的好处是候选集大小随分布自适应——分布集中时候选少，分布平坦时候选多。

至于「怎么从一个概率分布里随机抽一个 token」，LightLLM 用了 **Gumbel-max 技巧**：与其直接做 categorical 采样，不如给每个 logit 加上一个 Gumbel 噪声再取 argmax，数学上等价但便于向量化。

#### 4.3.2 核心流程

**top-k + top-p 联合过滤**（`_top_p_top_k`）：

1. 对每行 probs 降序排序，得到 `probs_sort` 与原始下标 `probs_idx`。
2. 计算 `probs_sum = cumsum(probs_sort)`（前缀和）。
3. **top-p**：把满足「自身之前的前缀和已经超过 top_p」的位置清零：

   \[ \text{mask}_p(i) = \big(S_{i-1} > \text{top\_p}\big),\quad S_{i-1} = S_i - \text{prob}_{(i)} \]

4. **top-k**：把位置序号 \(\ge k\) 的清零：

   \[ \text{mask}_k(i) = \big(i \ge k\big) \]

5. 在未被清零的候选上做 multinomial 采样，再用 `probs_idx` 把「排序后位置」映射回真实 token id。

**注意顺序**：源码里先做 top-p、再做 top-k，但二者都是「清零」操作，最终候选集是两者的交集（取更严格者）。

**随机采样**（`_random_sample`，Gumbel-max）：

\[ q_i \sim \text{Exponential}(1), \quad \text{token} = \arg\max_i \frac{\text{prob}_i}{q_i} \]

利用了「\( -\ln U \sim \text{Exp}(1) \)（\(U\) 为均匀分布）」与 Gumbel 分布的关系，把「按概率抽样」转成「加噪后取 argmax」，天然适合 GPU 向量化。若请求带了随机 `seed`，则用该请求自己的 `torch.Generator` 生成噪声，保证可复现。

#### 4.3.3 源码精读

完整的 top-k/top-p 过滤函数：[generic_post_process.py:L99-L107](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L99-L107)。

```python
probs_sort, probs_idx = probs.sort(dim=-1, descending=True)
probs_sum = torch.cumsum(probs_sort, dim=-1)
probs_sort[(probs_sum - probs_sort) > top_ps.view(-1, 1)] = 0.0   # top-p：自身之前的前缀和超阈值则清零
probs_sort[torch.arange(0, probs.shape[-1]).view(1,-1) >= top_ks.view(-1, 1)] = 0.0  # top-k：序号>=k 清零
```

关键细节：`(probs_sum - probs_sort)` 正是「排在它前面所有 token 的累计概率」\(S_{i-1}\)。当它已大于 `top_p`，说明这个 token 不属于「核」，清零。

采样入口与后端选择：[generic_post_process.py:L110-L144](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L110-L144)。LightLLM 支持两种采样后端（由 `--sampling_backend` 控制，默认 `triton`）：

- `triton`：先用上面的 `_top_p_top_k` 过滤，再用 `torch.multinomial` 抽样；若任一请求带 seed，则改用 `_random_sample` 以尊重各自的 generator。
- `flashinfer`：直接调用 flashinfer 的 `top_k_top_p_sampling_from_probs`，采用 `filter_apply_order="joint"`（联合过滤，一步到位），通常更快但依赖 flashinfer 库。

随机采样的 Gumbel-max 实现：[generic_post_process.py:L147-L154](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L147-L154)。

```python
q = torch.empty_like(probs)
q.exponential_()                       # q ~ Exponential(1)，等价于 -ln(Uniform)
if exist_req_use_random_seed:
    for i, req in enumerate(reqs):
        if req.generator is not None:
            q[i].exponential_(generator=req.generator)   # 带 seed 的请求用自己的噪声源
return probs.div(q).argmax(dim=-1).view(-1)
```

> 当 `skip_top_k` 与 `skip_top_p` 同时为真（即所有请求 `top_k == vocab_size` 且 `top_p == 1.0`）时，`sample()` 走纯随机快路径直接调用 `_random_sample`，跳过昂贵的排序，见 [generic_post_process.py:L86-L90](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L86-L90)。这是「让常见情况变快」的典型优化。

#### 4.3.4 代码实践

**实践目标**：用 numpy 复现 `_top_p_top_k` 的过滤逻辑，亲手看清「核」是如何被选出来的。

**操作步骤**（示例代码，可独立运行）：

```python
# 示例代码：复现 top-p + top-k 过滤
import numpy as np

np.random.seed(0)
probs = np.random.dirichlet(np.ones(8))   # 造一个 8-token 的概率分布
top_p, top_k = 0.6, 5

order = np.argsort(-probs)                # 降序下标
probs_sort = probs[order]
cumsum = np.cumsum(probs_sort)

mask_p = (cumsum - probs_sort) > top_p    # 自身之前的前缀和 > top_p → 清零
mask_k = np.arange(len(probs)) >= top_k   # 序号 >= top_k → 清零
probs_sort[mask_p | mask_k] = 0.0

kept = order[probs_sort > 0]
print("保留的 token 下标：", kept, "对应概率：", probs[kept])
print("核内累计概率：", probs[kept].sum())
```

**需要观察的现象**：

- 调小 `top_p`（如 0.4），保留的 token 变少；调大（如 0.9），保留变多。
- `top_k` 给候选数设了一个硬上限：即使 top_p 还没到，也最多保留 k 个。
- 最终保留集合是 top-p 与 top-k 的**交集**（取更严格者）。

**预期结果**：保留集合的累计概率略大于等于 `top_p`（因为含「首次跨过阈值」的那个 token），且数量不超过 `top_k`。这与 LightLLM 的 `_top_p_top_k` 行为一致。

> 待本地验证：在服务里用 `{"top_p":0.9,"top_k":50,"temperature":0.8}` 连续请求多次，观察输出多样性的变化；再尝试 `--sampling_backend flashinfer` 重启，对比 triton 与 flashinfer 的输出分布是否一致（理论上接近，因采样带随机性不会完全相同）。

#### 4.3.5 小练习与答案

**练习 1**：`_top_p_top_k` 里 top-p 和 top-k 都是把某些位置「清零」，而不是「删除」。为什么不删除？清零后采样怎么保证概率归一？

**参考答案**：因为 batch 内每个请求的候选数不同，删除会造成不规则形状、无法用张量统一处理；清零则保持 `[batch, vocab]` 的规则形状。采样时 `torch.multinomial(probs_sort, num_samples=1)` 会**隐式按非零项重新归一化**（multinomial 把输入当成未归一化权重），所以清零的项概率为 0、其余项按相对大小分配，无需显式 renormalize。

**练习 2**：Gumbel-max 技巧里为什么用 `probs.div(q).argmax()`（除法）而不是加法？

**参考答案**：因为 \(q \sim \text{Exponential}(1)\) 即 \(q = -\ln U\)（\(U\) 为均匀分布）。对概率取 \( \text{prob}/q \) 再取 argmax，等价于对 \( \ln\text{prob} - \ln q = \ln\text{prob} + \ln U\) 取 argmax，而 \(\ln\text{prob} + G\)（\(G\) 为 Gumbel 噪声）的 argmax 正好服从按 `prob` 抽样的 categorical 分布。用除法是为了在数值上稳定地把「乘法概率」转成「加法 Gumbel」，便于 argmax 一次完成。

---

## 5. 综合实践

**任务**：以本讲规格要求为主线——「在 `generic_post_process.py` 中追踪一次采样的完整流程，说明 penalty 如何作用到 logits，以及 top_p/top_k 采样的执行顺序」——完成下面这张「采样流水线追踪表」。建议把它写进自己的学习笔记。

**操作步骤**：

1. 从 [base_backend.py:L803](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L788-L825) 的 `sample(logits, run_reqs, self.eos_id)` 出发。
2. 在 [generic_post_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py) 里逐行定位下表的每一格，填入「行号 + 一句话作用」。

| 阶段 | 操作对象 | 关键行号 | 作用（一句话） |
| --- | --- | --- | --- |
| ① 组装批参数 | 各请求的 sampling_param | L12-L26 | 读出 temperature/top_p/top_k/penalty 等，判快路径标志 |
| ② 惩罚项 | logits（就地） | L43-L69 | 按 cpu/gpu 模式调 Triton kernel 改写 logits |
| ③ 非法 token | logits（就地） | L71-L76 | 把 invalid_token_ids 的 logit 置 -inf |
| ④ 温度+softmax | logits → probs | L78-L79 | logits/=temperature 后 softmax |
| ⑤ 分支采样 | probs | L81-L96 | 贪心 / 纯随机 / top-k-top-p 三选一 |

3. 在第②阶段，分别打开 [apply_penalty.py:L48-L52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty.py#L43-L52)（cpu 稀疏）与 [apply_penalty_gpu_cache.py:L41-L46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/post_process/apply_penalty_gpu_cache.py#L41-L46)（gpu 稠密），确认两者数学等价。
4. 在第⑤阶段，打开 [generic_post_process.py:L99-L107](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_post_process.py#L99-L107)，确认 top-p 先于 top-k 执行、二者都采用「清零」语义。

**预期结果**：你能合上书本，用自己的话讲清楚——「logits 先被惩罚项就地改写（repetition→frequency→presence 三步，外加 eos 长度惩罚），再除温度、过 softmax 得到 probs；如果整批都是贪心就 argmax，否则对 probs 做 top-p 再 top-k 的清零过滤，最后 multinomial 抽样」。并能指出每一步对应的源码行号与 kernel 文件。

## 6. 本讲小结

- `sample()` 是所有后端共用的唯一采样入口，执行顺序固定为：**组装批参数 → 惩罚项 → 非法 token 屏蔽 → 温度 → softmax → 分支采样**。
- 惩罚项发生在 softmax **之前**（改 logits），过滤/采样发生在 softmax **之后**（用 probs），顺序不可乱。
- 三类惩罚为 repetition（比例）、frequency（按次数线性）、presence（固定值），默认值使其整体为无操作；另有作用于 eos 的指数长度惩罚。
- token 计数有 cpu（稀疏、省显存、长输出有瓶颈）与 gpu（稠密、快、吃显存）两种模式，由 `--penalty_counter_mode` 切换，默认 `gpu_counter`。
- top-k 与 top-p 都用「清零」语义，候选集是两者交集；multinomial 会隐式归一化。
- 默认 `top_k=-1` 会被归入贪心快路径——**默认参数下 LightLLM 走 argmax 贪心解码**，要随机采样必须显式设 `top_k>1` 或带 seed。

## 7. 下一步学习建议

- 本讲聚焦「普通文本采样」。若想看采样结果如何被解析为「思考链 + 工具调用」，可继续学习 **u7-l6 约束解码、推理解析与函数调用**，那里会讲到 `reasoning_parser` / `function_call_parser`。
- 若想理解采样所依赖的「采样参数是如何从 HTTP 请求一路传到这里的」，可回顾 **u2-l2 HTTP API 服务与请求分发** 中 `SamplingParams` 的构建，以及 **u2-l3 请求对象与共享内存通信** 中它在共享内存里的布局。
- 若对「采样之前的 logits 从哪来」仍有疑问，回到 **u3-l2 prefill 与 decode 推理主流程** 的 post 层 `lm_head` 与 `_sample_and_scatter_token` 衔接处。
- 进阶读者可阅读 **u7-l5 MTP 推测解码**，看 draft 模型一次预测多 token 后，主模型如何用这里的 logits/采样逻辑做「验证 + 接受」。
