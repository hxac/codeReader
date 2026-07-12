# 训推分离：Rollout 引擎抽象与实现

## 1. 本讲目标

本讲是强化学习（RL）后训练单元的「基础设施课」。读完本讲，你应当能够：

1. 理解**训推分离（training/inference decoupling）**为什么是 RLAIF、PPO、GRPO、Agentic RL 的共同底座——训练侧用 PyTorch 反向传播更新策略（policy），采样侧需要高吞吐地生成大量回答并回算每个 token 的对数概率。
2. 看懂 `trainer/rollout_engine.py` 里的抽象设计：`RolloutEngine` 抽象基类定义了什么契约、`RolloutResult` 这个结果容器装了什么。
3. 掌握 `TorchRolloutEngine` 如何用项目自研的 `generate` 采样，并用 `compute_per_token_logps` 回算 old logps。
4. 理解 `SGLangRolloutEngine` 如何通过 HTTP 协议把权重同步给独立的推理进程、再回收 logprob，从而实现真正的「训推分离」。
5. 能写一个小脚本调用 `create_rollout_engine('torch', ...)` 跑通一次 rollout。

本讲**只讲 rollout 引擎本身**，不涉及奖励（reward）计算、优势（advantage）归一化、PPO/GRPO 的 clip/loss 等内容——这些分别在 u7-l3、u7-l4、u7-l5。

## 2. 前置知识

### 2.1 什么是 rollout

在策略梯度类 RL 里，模型不能只靠静态数据集学习，而要**自己生成回答、再根据回答的好坏调整自己**。这一步「用当前策略 πθ 生成一批回答」就叫 **rollout（采样/ rollout 一轮）**。

一个最朴素的 rollout 流程：

```
for 每个 prompt:
    回答 = 模型.generate(prompt)        # 采样
    记录 每个回答 token 的 log πθ(token) # 回算对数概率
```

为什么除了「生成」还要「回算对数概率」？因为后续 PPO/GRPO 的损失里需要用到**重要性比率（importance ratio）**：

\[ r_t = \exp\big(\log\pi_\theta(a_t) - \log\pi_{\theta_{\text{old}}}(a_t)\big) \]

其中 \(\log\pi_{\theta_{\text{old}}}\) 就是采样那一刻、**还没更新参数时**算出来的对数概率，业内俗称 **old logps**。所以 rollout 引擎必须在 `no_grad` 下既生成回答、又把这套 old logps 一并算好返回，否则后面 ratio 无从计算。

### 2.2 为什么要「训推分离」

把训练和采样塞进同一个 PyTorch 进程，在 toy 规模没问题，但放大到 RL 时会遇到：

- **采样是吞吐瓶颈**：RL 要为每个 prompt 生成 N 条候选（`num_generations`，GRPO 默认 4~8），纯 `model.generate` 逐 token 自回归、利用率低。
- **训练侧的计算图很重**：DDP、autocast、GradScaler、`torch.compile` 都是为反向传播优化的，反而拖慢纯前向推理。
- **推理引擎（如 SGLang / vLLM）有专门的 paged-attention、连续批处理、前缀缓存**，吞吐远高于 PyTorch eager。

「训推分离」就是把这两个职责解耦成两个角色：

| 角色 | 职责 | 实现方式 |
|------|------|----------|
| Trainer（训练侧） | 用新的回答 + reward 算 loss、反向传播更新 θ | PyTorch + DDP |
| Rollout Engine（采样侧） | 用当前 θ 高吞吐采样、回算 old logps | PyTorch 自研 或 SGLang 独立进程 |

二者之间靠两条「消息」协同：训练侧把**更新后的权重**推给采样侧（`update_policy`），采样侧把**生成结果 + logps**返回给训练侧（`rollout`）。

### 2.3 你需要先掌握的

- **u3-l6 自定义 generate**：本讲的 `TorchRolloutEngine.rollout` 内部就是调 `model.generate`。
- **u3-l5 logits_to_keep 与位移交叉熵**：本讲的 `compute_per_token_logps` 用 `logits_to_keep` 做高效切片，原理与 u3-l5 一脉相承。
- **u4-l1 init_model / DDP / autocast**：rollout 引擎的构造依赖这些训练公共件。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [trainer/rollout_engine.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py) | rollout 引擎的全部实现：`compute_per_token_logps`、`RolloutResult`、`RolloutEngine` 抽象基类、`TorchRolloutEngine`、`SGLangRolloutEngine`、`create_rollout_engine` 工厂函数 |

下游消费方（说明这个抽象被谁用、怎么用）：

| 文件 | 作用 |
|------|------|
| [trainer/train_grpo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py) | GRPO/CISPO 训练，在 `grpo_train_epoch` 里调 `rollout_engine.rollout(...)` 并消费 `RolloutResult` |
| [trainer/train_ppo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py) | PPO 训练，同样以 `RolloutEngine` 为采样后端 |
| [trainer/train_agent.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py) | Agentic RL，在 `rollout_single` 里反复调 `rollout` 实现多轮工具调用 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看「结果容器 + 抽象契约」（`RolloutResult` / `RolloutEngine`），再拆「回算 logps 的核心函数」（`compute_per_token_logps`），然后逐个实现两种引擎（`TorchRolloutEngine`、`SGLangRolloutEngine`）。

### 4.1 RolloutResult 结果容器与 RolloutEngine 抽象基类

#### 4.1.1 概念说明

无论采样后端是本地 PyTorch 还是远端 SGLang，训练侧拿到手的东西必须长一个样——否则 `grpo_train_epoch` 就得为每种后端写一套 if 分支。因此 rollout 引擎抽象出两样东西：

1. **统一的结果容器 `RolloutResult`**：一个 dataclass，规定一次 rollout 必须返回哪些字段。
2. **统一的接口契约 `RolloutEngine`**：一个抽象基类（ABC），规定任何引擎都必须实现 `rollout`（采样）和 `update_policy`（同步权重）两个方法。

这是典型的「**依赖倒置**」：上层训练循环只依赖抽象 `RolloutEngine`，不依赖具体是 torch 还是 sglang，从而做到后端可插拔。

#### 4.1.2 核心流程

```
训练循环每一步：
    1. rollout_engine.rollout(prompt_ids, ...) -> RolloutResult
           ↑ 引擎内部可能调 torch.generate，也可能 POST /sglang/generate
    2. 用 RolloutResult.per_token_logps 作为 old_logps，配合新前向算 ratio
    3. loss.backward(); optimizer.step()  -> 模型权重更新
    4. rollout_engine.update_policy(model)  -> 把新权重同步回采样侧
           ↑ torch 后端是改个引用，sglang 后端是写盘 + HTTP 通知重载
```

#### 4.1.3 源码精读

先看结果容器。`RolloutResult` 是一个 `@dataclass`，没有任何方法，纯粹是六个字段的数据包：

[trainer/rollout_engine.py:39-47](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L39-L47) —— 定义 `RolloutResult`，规定一次采样必须返回的六要素。

六个字段的含义：

| 字段 | 形状（典型） | 含义 |
|------|-------------|------|
| `output_ids` | `[B*num_gen, P+R]` | prompt + completion 拼接后的完整 id 序列（含 padding） |
| `completion_ids` | `[B*num_gen, R]` | 仅回答部分（prompt 之后）的 id |
| `per_token_logps` | `[B*num_gen, R]` | 回答部分每个 token 在采样时刻的 old logps |
| `completions` | `List[str]` | 回答的解码文本（用于算文本类 reward、打印日志） |
| `prompt_lens` | `[B*num_gen]` | 每条样本的 prompt 长度（回答的起始列） |
| `completion_mask` | `[B*num_gen, R]` | 0/1 掩码，标记 completion_ids 中哪些是真实 token、哪些是 pad |

其中 `B` 是数据 batch、`num_gen` 是每个 prompt 重复采样的次数（GRPO 的「分组」大小）。

再看抽象基类：

[trainer/rollout_engine.py:50-60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L50-L60) —— `RolloutEngine(ABC)` 用 `@abstractmethod` 钉死两个必须实现的方法：`rollout` 与 `update_policy`。

注意第 52 行的类属性 `tokenizer = None`：它把 tokenizer 当作「所有引擎都该有」的公共属性提到基类声明，子类在 `__init__` 里赋值后即可被上层当作 `rollout_engine.tokenizer` 访问。这种写法让上层不用关心子类构造细节。

`rollout` 的签名也值得注意——它接收的是**已经 tokenize 好的张量**（`prompt_ids`、`attention_mask`），而不是原始字符串。这意味着「文本→token」这一步由训练侧（如 `train_grpo.py` 第 74 行的 `tokenizer(...)`）完成，引擎只负责「token→生成→logps」。这样设计的好处是：tokenizer 在训练侧只加载一份，且训练侧可以自由做 `padding_side="left"`、`max_seq_len` 截断等预处理。

#### 4.1.4 代码实践

**实践目标**：理解「抽象契约 + 统一容器」如何让上层与后端解耦。

**操作步骤**（源码阅读型）：

1. 打开 `trainer/rollout_engine.py`，确认 `RolloutEngine` 只声明了 `rollout` 和 `update_policy` 两个抽象方法。
2. 打开 `trainer/train_grpo.py` 的 `grpo_train_epoch`（第 71 行起），观察它如何消费 `rollout_result`：分别取了 `output_ids`、`completion_ids`、`completions`、`per_token_logps`、`prompt_lens`、`completion_mask`，正好对应 `RolloutResult` 的六个字段。
3. 在 `train_grpo.py` 中搜索 `args.rollout_engine`，确认上层通过 `--rollout_engine torch|sglang` 一个开关就能切后端，而 `grpo_train_epoch` 内部代码完全不变。

**需要观察的现象**：上层训练循环里**没有任何** `if sglang: ... else: ...` 的分支去处理采样本身（只在保存/分布式等处有少量 `use_sglang` 标记）。这正是抽象的意义。

**预期结果**：你应当能确认「换后端只改一个 CLI 参数」这件事是成立的。

#### 4.1.5 小练习与答案

**练习 1**：如果让你新增一个 `VllmRolloutEngine`，你需要实现哪几个方法？为什么？

> **答案**：必须实现 `rollout` 和 `update_policy`（因为它们是 `@abstractmethod`）。可选实现 `flush_cache` / `health`。因为基类用 ABC 强制约束，不实现就实例化会直接 `TypeError`。

**练习 2**：`RolloutResult` 里为什么要把 `per_token_logps` 单独作为一个字段，而不是让上层自己前向算？

> **答案**：因为 `per_token_logps` 必须是 **old** logps——即「采样那一刻、参数还没更新」时的对数概率。它要在 `no_grad` 下、用**生成时**的权重算。如果交给上层在反向传播图里算，就不再是 old 版本了，ratio 就错了。所以只能由 rollout 引擎在采样阶段一并算好并固化返回。

---

### 4.2 compute_per_token_logps：高效回算每个 token 的对数概率

#### 4.2.1 概念说明

给定一段已经生成好的序列 `input_ids = [prompt..., completion...]`，我们要算出**每个 completion token 的对数概率** \(\log\pi_\theta(a_t)\)。

朴素做法是把整段 `[B, L]` 送进模型算出全部 `[B, L, vocab]` 的 logits，再 `log_softmax + gather`。但这有两个浪费：

1. 我们只关心最后 R 个 token 的 logprob，前面的 prompt 部分根本不需要走 `lm_head`。
2. `lm_head` 是 `[hidden, vocab]` 的大矩阵乘，对 6400 词表来说每多算一个位置就多算 6400 个输出。

`compute_per_token_logps` 用 `logits_to_keep`（u3-l5 已建立的概念）一次性把这两个浪费都消除。

#### 4.2.2 核心流程

设序列长 `L = P + R`，要回算最后 `n_keep = R` 个 token 的 logprob。算法是：

```
1. 前向时传 logits_to_keep = R + 1
   -> 模型只对最后 R+1 个位置算 lm_head，logits 形状 [B, R+1, vocab]
2. logits = logits[:, :-1, :]   # 丢掉最后一个位置 -> [B, R, vocab]
   因为最后一个位置预测的是「序列外」的下一个 token，我们没有它的真值
3. 对每个样本 i：
   ids_row = input_ids[i, -R:]           # 这 R 个 token 的真值
   logp[i] = gather(log_softmax(logits[i]), ids_row)  # [R]
4. stack 成 [B, R]
```

为什么对齐是对的？因为「位置 t 的 logits 预测的是位置 t+1 的 token」。`logits[:, :-1, :]` 保留的是位置 `P-1 .. P+R-2` 的 logits，它们分别预测位置 `P .. P+R-1`，正好就是 `input_ids[:, -R:]` 这 R 个真值 token。这就是 u3-l5 讲过的**位移交叉熵**的 logits_to_keep 高效版。

#### 4.2.3 源码精读

[trainer/rollout_engine.py:23-36](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L23-L36) —— `compute_per_token_logps`，用 `logits_to_keep` 切片 + gather 回算每 token 的 logprob。

几个关键细节：

- **第 25-26 行的早退**：`if n_keep <= 0: return ... new_empty((B, 0))`。当传入的 `n_keep` 为 0（比如 completion 为空）时直接返回形状 `[B, 0]` 的空张量，避免后面 `gather` 报错。这种「形状对、值为空」的返回对上层很重要，保证 batch 维度对齐。
- **第 27 行剥 DDP 外壳**：`unwrapped = model.module if isinstance(model, DistributedDataParallel) else model`。因为 DDP 包装后 `model(input_ids)` 会被它拦截，而这里我们只想做一次纯前向、不需要梯度同步，所以剥到内部的真模型再调用。注意这里**没有**剥 `torch.compile` 的 `_orig_mod`——因为 compile 包装的模型直接调用也能正常前向。
- **第 28 行 `is_inference()` 判断**：`input_ids.detach().clone() if input_ids.is_inference() else input_ids`。某些 PyTorch 版本里，推理模式下产生的张量带有 inference 标记，直接喂给需要梯度的算子会报错，这里按需 clone。`compute_per_token_logps` 本身被 `no_grad` 包裹，这个判断是双保险。
- **第 29 行的核心切片**：`logits = unwrapped(..., logits_to_keep=n_keep + 1).logits[:, :-1, :]`。一句话同时完成「只算尾部 R+1 个位置的 lm_head」和「位移丢一」两件事。
- **第 31-35 行的逐行 gather**：`torch.gather(logits_row.log_softmax(dim=-1), 1, ids_row.unsqueeze(1))`。先对词表维 `log_softmax` 得到对数概率，再用真值 id 做 gather 抽出对应位置。之所以用 `for` 逐行而不是 batch 一把梭，是因为每个样本要配对「自己的 logits 行」和「自己的 ids 行」，逐行写最直白且可读。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `logits_to_keep` 与位移对齐的正确性。

**操作步骤**（源码阅读 + 思想实验）：

1. 假设 `input_ids` 形状 `[2, 10]`（P=6, R=4），调用 `compute_per_token_logps(model, input_ids, n_keep=4)`。
2. 在脑中/纸上标注：第 29 行 `logits_to_keep=5`，所以模型只算最后 5 个位置（位置 5..9）的 logits，`[:, :-1, :]` 后剩位置 5..8 共 4 行 logits。
3. `input_ids[:, -4:]` 取位置 6..9 共 4 个真值 token。
4. 逐行配对：位置 5 的 logits ↔ 位置 6 的真值、……、位置 8 的 logits ↔ 位置 9 的真值。

**需要观察的现象**：logits 的第 i 行预测的正是 ids 的第 i 行，二者严格错位对齐。

**预期结果**：返回张量形状 `[2, 4]`，每行 4 个值分别是这 4 个 completion token 的 logprob。**待本地验证**：可在脚本里 `print(out.shape)`、并把某一行 `torch.exp(out).sum()`（理论上接近该 token 在 softmax 后的概率，可对照 `F.softmax(logits).gather` 验证一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么是 `logits_to_keep = n_keep + 1` 而不是 `n_keep`？

> **答案**：要预测最后 R 个 token，需要用到「倒数第 R 个 token 之前那个位置」的 logits。比如要预测位置 P..P+R-1 共 R 个 token，需要位置 P-1..P+R-2 共 R 个位置的 logits，而位置 P-1 是序列倒数第 R+1 个位置，所以前向时 `lm_head` 必须覆盖最后 R+1 个位置，故 `logits_to_keep = R+1`。随后用 `[:, :-1, :]` 再丢掉那个会预测「序列外 token」的多余位置。

**练习 2**：函数里 `for` 循环逐行 gather，能不能改成纯 batch 向量化？有什么权衡？

> **答案**：可以，例如 `F.cross_entropy` 或把 `ids` 升维后一次 gather。当前逐行写法更可读、更不容易在 batch 维和词表维上出错，且 rollout 通常 batch 不大、这点循环开销相比 `lm_head` 矩阵乘可忽略。换向量化版本性能略好但牺牲可读性，是合理的工程取舍。

---

### 4.3 TorchRolloutEngine：本地 PyTorch 推理引擎

#### 4.3.1 概念说明

`TorchRolloutEngine` 是默认后端（CLI 参数 `--rollout_engine torch`）。它不引入任何外部进程，直接用**同一个 PyTorch 模型对象**既做采样又做 logps 回算。它的「训推分离」是最轻量的：训练侧和采样侧共享同一个 `policy_model` 对象的内存引用，`update_policy` 只是换一个引用变量。

这种模式的优点是简单、零部署成本、单卡即可跑通；缺点是吞吐受限于 PyTorch eager 的 `generate`。它适合学习、调试、小规模实验。

#### 4.3.2 核心流程

```
rollout(prompt_ids[B,P], attention_mask[B,P], num_gen, max_new_tokens, temperature):
    1. 把 prompt 沿 batch 维 repeat_interleave(num_gen) -> [B*num_gen, P]
       （关键：连续重复，保证后面 rewards.view(-1, num_gen) 能按组聚合）
    2. 在 no_grad + autocast 下调 model.generate(...)
       -> output_ids [B*num_gen, P+R]
    3. completion_ids = output_ids[:, P:]  -> [B*num_gen, R]
    4. full_mask = (output_ids != pad_token_id)
    5. per_token_logps = compute_per_token_logps(model, output_ids, R, full_mask)
    6. batch_decode 得 completions 文本
    7. 打包成 RolloutResult 返回

update_policy(new_model):
    self.policy_model = new_model   # 仅换引用，零成本同步
```

#### 4.3.3 源码精读

先看构造：

[trainer/rollout_engine.py:64-69](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L64-L69) —— `TorchRolloutEngine.__init__`，保存 policy_model、tokenizer、device 和可选的 autocast 上下文。

注意 `autocast_ctx` 是可选的（默认 `None`），它让 rollout 也能复用训练侧设定的混合精度（fp16/bf16），保证采样和后续前向的数值精度一致。

再看核心 `rollout`：

[trainer/rollout_engine.py:71-92](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L71-L92) —— `TorchRolloutEngine.rollout`，repeat_interleave + generate + 回算 logps。

逐段拆：

- **第 72 行剥 DDP**：`model = self.policy_model.module if isinstance(..., DistributedDataParallel) else self.policy_model`。`generate` 是项目自研方法（u3-l6），DDP 包装会拦截 `forward` 但不一定转发 `generate`，所以这里剥到内部模型。
- **第 73-74 行上下文管理**：`ctx = self.autocast_ctx if self.autocast_ctx else nullcontext()`，配合 `torch.no_grad()`。`nullcontext()` 是个空上下文管理器，当没传 autocast 时什么都不做，避免写 if/else。
- **第 75-84 行采样**：调 `model.generate(...)`，关键参数：
  - `input_ids=prompt_ids.repeat_interleave(num_generations, dim=0)`：把 `[B,P]` 变成 `[B*num_gen, P]`。`repeat_interleave` 是「**连续**重复」——`[A,B]` 在 `num_gen=2` 下变成 `[A,A,B,B]`，而不是 `[A,B,A,B]`。这一点至关重要：GRPO 后续要用 `rewards.view(-1, num_generations)` 按行 reshape 成 `[B, num_gen]` 做组内归一化（见 `train_grpo.py` 第 121 行），只有连续重复才能保证每组的 num_gen 行是同一个 prompt。
  - `do_sample=True, temperature=temperature`：采样而非贪心，保证多样性。
  - `num_return_sequences=1`：每次 generate 调用每条只出 1 条（多样性靠 `repeat_interleave` 扩 batch，而不是靠 `num_return_sequences`）。
  - `pad_token_id` / `eos_token_id`：交给 generate 内部做结束与对齐（u3-l6 已讲）。
  - 末尾 `.clone()`：从 inference/no_grad 上下文里把张量「拿出来」一份独立拷贝，避免后续被 autograd 或原地操作影响。
- **第 85-88 行切出回答 + 算 mask + 回算 logps**：
  - `prompt_len = prompt_ids.size(1)`：原始 prompt 长度 P（注意 left-padding 在上层 `train_grpo.py` 已做，这里 prompt_ids 的有效列数就是 P）。
  - `completion_ids = output_ids[:, prompt_len:]`：切掉 prompt 部分，只剩回答。
  - `full_mask = (output_ids != pad_token_id).long()`：整个序列里非 pad 的位置，作为 `attention_mask` 传给 `compute_per_token_logps`，让回算时正确忽略 padding。
  - `compute_per_token_logps(self.policy_model, output_ids, completion_ids.size(1), full_mask)`：回算 R 个 token 的 old logps。注意传的是**完整** `output_ids`（含 prompt），因为预测 completion token 需要 prompt 提供上下文。
- **第 89 行解码**：`batch_decode(completion_ids, skip_special_tokens=True)` 得到文本，供 reward 模型打分、日志打印。
- **第 90-92 行打包**：构造 `RolloutResult`。两个 mask/长度值得注意：
  - `prompt_lens` 用 `prompt_ids.new_full((B*num_gen,), prompt_len)`——所有样本 prompt 长度都是 P（因为同一批 padding 到等长）。
  - `completion_mask` 用 `attention_mask.new_ones(...)`——这里返回**全 1** 的 R 列掩码。注意这是一个「宽松」掩码：它把所有生成出的位置都标 1，**包括 padding 部分**。真正的「截到 eos」的精确掩码由上层 `train_grpo.py` 第 126-130 行根据 `eos_token_id` 重新构造。这种分工避免了引擎过度耦合训练逻辑。

最后是 `update_policy`：

[trainer/rollout_engine.py:94-95](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L94-L95) —— `update_policy` 仅替换 `self.policy_model` 引用，这是「同一进程」模式下零成本的权重同步。

这一行看似 trivial，但它**统一了接口**：上层在 `train_grpo.py` 第 313、316、193 行无条件调 `rollout_engine.update_policy(model)`，不用关心是 torch 还是 sglang 后端。在 torch 后端，它确保引擎持有的是「最新被 DDP/compile 包装后的模型对象」——这就是为什么 `train_grpo.py` 在 `torch.compile` 之后（第 313 行）和 DDP 包装之后（第 316 行）都要各调一次 `update_policy`，把包装后的新对象同步进引擎。

#### 4.3.4 代码实践

**实践目标**：跑通一次 `TorchRolloutEngine.rollout`，打印关键张量形状。

**操作步骤**（最小可运行脚本，需本地有可加载的 SFT/pretrain 权重）：

在仓库根目录新建 `test_rollout.py`（这是**示例代码**，非项目原有文件）：

```python
# 示例代码：验证 TorchRolloutEngine.rollout 的输出形状
import sys, os
sys.path.append(os.path.abspath('.'))
import torch
from transformers import AutoTokenizer
from model.model_minimind import MiniMindConfig
from trainer.trainer_utils import init_model
from trainer.rollout_engine import create_rollout_engine

device = "cuda" if torch.cuda.is_available() else "cpu"
lm_config = MiniMindConfig(hidden_size=512, num_hidden_layers=8)
model, tokenizer = init_model(lm_config, base_weight="full_sft", device=device)

engine = create_rollout_engine(
    engine_type="torch", policy_model=model, tokenizer=tokenizer, device=device,
)

prompts = ["你好，请介绍一下你自己。", "1+1等于几？"]
inputs = tokenizer(prompts, return_tensors="pt", padding=True,
                   padding_side="left", add_special_tokens=False).to(device)

res = engine.rollout(
    prompt_ids=inputs["input_ids"],
    attention_mask=inputs["attention_mask"],
    num_generations=2, max_new_tokens=32, temperature=0.8,
)

print("output_ids   :", res.output_ids.shape)        # 期望 [4, P+R]
print("completion_ids:", res.completion_ids.shape)   # 期望 [4, 32]
print("per_token_logps:", res.per_token_logps.shape) # 期望 [4, 32]
print("prompt_lens   :", res.prompt_lens)            # 期望每条都是 P
print("completion_mask:", res.completion_mask.shape) # 期望 [4, 32]
for i, c in enumerate(res.completions):
    print(f"[{i}] {c}")
```

运行：`python test_rollout.py`（确保 `./out/full_sft_512.pth` 存在；若用 MoE 权重要加 `_moe`）。

**需要观察的现象**：
- `num_generations=2`、2 个 prompt → 所有 batch 维都是 `4`（= 2×2）。
- `per_token_logps` 与 `completion_ids` 列数相同。
- 同一个 prompt 的两条回答（索引 0,1 是 prompt0；2,3 是 prompt1）因 `temperature=0.8` 采样而内容不同。

**预期结果**：形状符合上表，`per_token_logps` 每个值都是有限的负数（logprob ≤ 0）。**待本地验证**：具体数值取决于权重与设备。

> 注意：若没有训练好的权重，可把 `base_weight=None`（走随机初始化），形状验证依然成立，只是回答是乱码。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `repeat_interleave` 改成 `repeat`（即 `[A,B] → [A,B,A,B]`），GRPO 还能正常按组归一化吗？

> **答案**：不能。GRPO 用 `rewards.view(-1, num_generations)` 把 `rewards` reshape 成 `[B, num_gen]`，要求连续 num_gen 行是同一 prompt 的候选。`repeat` 产生交错排列会使分组错乱，优势归一化把不同 prompt 的回答混到一组，训练信号被污染。

**练习 2**：`rollout` 里为什么先 `model.generate` 再单独 `compute_per_token_logps`，而不是让 generate 直接吐 logps？

> **答案**：项目自研 `generate`（u3-l6）只负责采样出 token id，不返回每个 token 的对数概率。所以需要事后把整段 `output_ids` 再前向一次、用 `logits_to_keep` 高效回算。代价是 completion 部分被前向两次（一次在 generate 里生成、一次在这里回算），但换来了 generate 与 logps 解耦的清晰边界。

---

### 4.4 SGLangRolloutEngine：HTTP 训推分离引擎

#### 4.4.1 概念说明

`SGLangRolloutEngine` 是高性能后端（`--rollout_engine sglang`）。它把采样工作完全交给一个**独立运行的 SGLang 推理服务进程**，训练进程只通过 HTTP 与之通信：

- **采样**：`POST /generate`，把 `input_ids` 发过去，SGLang 用 paged-attention、连续批处理等技巧高吞吐生成，并在响应里直接返回每个 token 的 logprob（`return_logprob=True`），省掉训练侧的事后回算。
- **同步权重**：`POST /update_weights_from_disk`，训练侧把新权重写盘成 transformers 格式，通知 SGLang 热重载，从而让采样进程跟上参数更新。

这是「真·训推分离」：训练和推理是两个进程，甚至可以分布在不同 GPU 上。文件顶部注释给出了启动命令：

[trainer/rollout_engine.py:1-4](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L1-L4) —— 说明使用 SGLang 加速前需先用 `sglang.launch_server` 启动一个 transformers 格式的模型服务。

#### 4.4.2 核心流程

```
rollout:
    1. 去掉每条 prompt 左侧 padding，得到有效 token 列表
    2. 按 num_gen 展开成 B*num_gen 条 input_ids
    3. POST /generate { input_ids, sampling_params, return_logprob=True }
    4. 解析响应：output_ids、output_token_logprobs
    5. 对齐长度（logprobs 可能比 completion 短/长，做前补 0 / 截断）
    6. 拼回 prompt+completion、pad 成张量，打包 RolloutResult

update_policy(model):
    1. 仅 rank 0 执行：
       a. 剥 DDP + _orig_mod 外壳
       b. save_pretrained 写盘到 shared_ckpt_path（fp16）
       c. POST /update_weights_from_disk { model_path } 通知热重载
    2. dist.broadcast 把 ok 标志广播到所有 rank
    3. 失败则 raise RuntimeError
```

#### 4.4.3 源码精读

构造函数：

[trainer/rollout_engine.py:99-105](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L99-L105) —— `SGLangRolloutEngine.__init__`，记录服务地址、共享检查点路径、超时，并加载一份本地 tokenizer（用于 pad/eos id 与编解码）。

注意它**不持有任何模型权重**——这是与 `TorchRolloutEngine` 的根本区别。它的 `self.http = requests` 只是把 `requests` 库存成属性，方便测试时注入 mock。

`rollout` 的采样与解析：

[trainer/rollout_engine.py:107-173](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L107-L173) —— `rollout`，组装 `/generate` payload、解析 logprobs、pad 成张量。

几个关键点：

- **第 109-112 行去左 padding**：`valid_ids = ids[mask.bool()].tolist()`。因为上层用 `padding_side="left"`，prompt 前面有 pad，而 SGLang 直接吃 token id、不需要 padding，所以必须先用 attention_mask 把有效部分筛出来。
- **第 113 行展开**：`all_input_ids = [ids for ids in input_ids_list for _ in range(num_generations)]`——等价于 `repeat_interleave`，保证连续重复。
- **第 115-123 行 payload**：`input_ids` 是一个「批的批」（list of list），SGLang 一次处理多条；`sampling_params` 里放 temperature、max_new_tokens、stop_token_ids（eos）；`return_logprob=True` 是关键，要求服务端返回 logprob。
- **第 125-128 行请求与容错**：`resp.raise_for_status()` 在非 200 时抛异常；第 129-130 行处理「单条返回不是 list」的情况，兼容服务端返回单对象或数组的两种形态。
- **第 135-155 行解析每条结果**：
  - `completion_ids` 优先取 `meta_info.output_ids`，回退到顶层 `output_ids`。
  - `raw_logprobs` 取 `meta_info.output_token_logprobs`。SGLang 的 logprob 条目可能是 `[logprob, token_id, text]` 这种 tuple，也可能是裸数字，第 141-145 行做了两种形态的兼容——只取第一个元素（即 logprob 值）。
  - **第 147-150 行长度对齐**：logprobs 与 completion_ids 长度可能不一致（服务端有时不返回首个 token 的 logprob）。短了就**前面补 0**（首 token 缺失），长了就**截最后 N 个**。这是一个务实的容错——保证返回的 logps 张量形状与 completion_ids 严格一致，避免后续 gather 越界。
- **第 151-155 行拼回完整序列**：`full_output = prompt + completion_ids`，对应 `RolloutResult.output_ids`。
- **第 158-172 行 pad 成张量**：
  - `max_comp_len` / `max_out_len` 取本批最长，`pad_to_tensor` 把不等长的 list 补齐成 `[B*num_gen, max_len]` 张量。
  - `per_token_logps` 的 pad 值是 `0.0`（不是 pad_id！因为 logps 是浮点，且后续会被 `completion_mask` 屏蔽，填 0 不影响 loss）。
  - `prompt_lens` 是每条**有效 prompt 的真实长度**（注意这里和 torch 后端不同：sglang 去掉了 padding，所以每条 prompt_len 可能不同）。
  - `completion_mask` 是 `[1]*真实长度 + [0]*pad`。

`update_policy` 的权重热同步是最有工程含量的部分：

[trainer/rollout_engine.py:175-194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L175-L194) —— `update_policy`，rank0 写盘 + 通知 SGLang 热重载，再广播结果。

逐段拆：

- **第 177 行只 rank0 做**：`if not dist.is_initialized() or dist.get_rank() == 0`。多卡训练时，DDP 的每个 rank 都有完整权重副本，但只需要一个 rank 写盘，避免多进程写同一文件冲突。
- **第 179-180 行剥双层外壳**：先剥 DDP 的 `.module`，再剥 `torch.compile` 的 `._orig_mod`，拿到最内层的 `MiniMindForCausalLM`。
- **第 182-184 行写盘**：
  - `state_dict = {k: v.detach().half().cpu() ...}`：转 fp16、搬 CPU，省显存和带宽。
  - `unwrapped.save_pretrained(abs_path, state_dict=state_dict, safe_serialization=False)`：写成 transformers 格式（SGLang 只认这个格式）。`safe_serialization=False` 表示用 pickle 而非 safetensors，与项目其他地方的加载习惯一致。
  - `self.tokenizer.save_pretrained(abs_path)`：把 tokenizer 也存进去，SGLang 重载时一并加载。
- **第 185-187 行 HTTP 通知**：`POST /update_weights_from_disk { model_path: abs_path }`，SGLang 收到后从磁盘热加载新权重，**无需重启服务**。非 200 只打印警告、不直接抛异常（留给后面的统一判断）。
- **第 190-192 行广播**：`dist.broadcast(ok_t, src=0)`。把 rank0 的成功标志广播给所有 rank，再做 `dist.barrier()` 同步——保证所有训练进程在「权重已同步给 SGLang」这一点上达成共识后才继续。
- **第 193 行失败抛异常**：`if not ok: raise RuntimeError(...)`。任一环节失败就中断训练，避免后续在「SGLang 用着旧权重、训练用着新权重」的不一致状态下继续采样。

另外两个辅助方法：

[trainer/rollout_engine.py:196-205](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L196-L205) —— `flush_cache` 清空 SGLang 的 KV/prefix 缓存（权重变了旧缓存失效），`health` 探活。

`flush_cache` 在权重热重载后很有用：SGLang 会缓存 prefix 的 KV，而新权重下旧 KV 已无效，清掉避免脏读。

#### 4.4.4 代码实践

**实践目标**：理解 HTTP 训推分离的权重同步时序，而不必真的部署 SGLang。

**操作步骤**（源码阅读 + 思想实验）：

1. 阅读 `train_grpo.py` 第 240-243 行的 CLI 参数：`--sglang_base_url`（默认 `http://localhost:8998`）、`--sglang_model_path`（默认 `../model`，用于加载 tokenizer）、`--sglang_shared_path`（默认 `./sglang_ckpt_grpo`，写盘路径）。
2. 对照 `update_policy` 源码，画出一次「训练更新→SGLang 热重载」的时序：
   ```
   rank0: save_pretrained(写盘) -> POST /update_weights_from_disk
                                            |
   SGLang:  从磁盘 reload 权重 <- (HTTP 200) -+
   all ranks: broadcast(ok) -> barrier -> 继续下一轮 rollout
   ```
3. 思考：为什么 rank0 写盘后必须 `dist.barrier`？如果某个 rank 没等 barrier 就发起 `rollout`（POST /generate），会怎样？

**需要观察的现象**（实际部署时）：SGLang 服务的日志里会出现一次 `update_weights` 事件；`/generate` 返回的 `meta_info.output_token_logprobs` 是一个 list，每项含 logprob 值。

**预期结果**：能口述「写盘 → HTTP 通知 → 广播 → barrier」四步；能解释不做 barrier 会导致部分 rank 用旧权重采样、部分用新权重，破坏数据一致性。**待本地验证**：需真正部署 SGLang 才能观察 HTTP 交互。

> 如果想真正跑通，需先按文件顶部注释启动服务（`python -m sglang.launch_server --model-path ./minimind-3 --port 8998 ...`），再用 `--rollout_engine sglang` 启动 `train_grpo.py`。这要求已用 `convert_model.py`（u8-l1）把权重转成 transformers 格式。

#### 4.4.5 小练习与答案

**练习 1**：`update_policy` 里为什么要 `dist.broadcast(ok_t, src=0)` 再 `barrier`，而不是让每个 rank 各自 POST？

> **答案**：两点。其一，只有 rank0 写盘（避免多进程写冲突），所以只有 rank0 知道是否成功，必须广播给其他 rank；其二，barrier 保证所有 rank 在「SGLang 已切到新权重」后才开始下一轮采样，否则快 rank 会用旧权重采样，导致 batch 内样本来自不同策略版本，污染重要性比率。

**练习 2**：对比 `TorchRolloutEngine` 与 `SGLangRolloutEngine` 在「回算 logps」这件事上的差异。

> **答案**：Torch 后端自己用 `compute_per_token_logps` 事后前向回算 old logps；SGLang 后端则由推理服务在 `return_logprob=True` 时**直接在响应里返回** logprob，训练侧零额外前向。这也是 SGLang 后端吞吐更高的原因之一：省掉了训练侧的一次完整前向。

---

## 5. 综合实践

把四个最小模块串起来，完成一个「**对比两种后端、验证它们返回同构的 RolloutResult**」的小任务。

**任务**：在读懂源码的基础上，画一张 rollout 引擎的「契约-实现」对照表，并手写一段调用代码。

**步骤**：

1. **画契约表**：列出 `RolloutEngine` 的两个抽象方法（`rollout`、`update_policy`），以及 `TorchRolloutEngine` 和 `SGLangRolloutEngine` 各自的实现策略。参考答案：

   | 抽象方法 | TorchRolloutEngine | SGLangRolloutEngine |
   |----------|--------------------|---------------------|
   | `rollout` | `model.generate` + `compute_per_token_logps` 事后回算 | `POST /generate`（`return_logprob=True`），服务端返回 |
   | `update_policy` | 换 `self.policy_model` 引用（零成本） | rank0 `save_pretrained` 写盘 + `POST /update_weights_from_disk` + 广播 barrier |

2. **验证同构性**：对照 4.3.4 的示例脚本，确认 `RolloutResult` 的六个字段（`output_ids` / `completion_ids` / `per_token_logps` / `completions` / `prompt_lens` / `completion_mask`）在两种后端下形状与含义一致——这就是上层 `grpo_train_epoch` 能无差别消费两种后端的根本原因。

3. **（选做，需 SGLang）** 部署 SGLang 后，用 `--rollout_engine sglang` 重跑相同 prompt，对比 torch 与 sglang 两种后端返回的 `completions`（在相同 temperature、固定随机种子下应高度相似，但不完全相同，因为采样实现与 RNG 来源不同）。

**预期结果**：能独立解释「为什么换后端只改一个 CLI 参数、上层训练代码完全不动」，并能指出 `update_policy` 在两种后端下代价相差几个数量级（torch：O(1) 引用赋值；sglang：写盘 + HTTP + 广播）。

---

## 6. 本讲小结

- **训推分离**是 RLAIF/PPO/GRPO/Agent 的共同底座：训练侧反向传播更新 θ，采样侧高吞吐生成 + 回算 old logps，二者靠 `update_policy`（同步权重）和 `rollout`（返回结果）协同。
- `RolloutResult` 是统一结果容器（六字段），`RolloutEngine` 是抽象基类（`rollout` + `update_policy` 两个 `@abstractmethod`），二者共同让上层训练循环与后端实现彻底解耦——换后端只改 `--rollout_engine` 一个参数。
- `compute_per_token_logps` 用 `logits_to_keep = R+1` + `[:, :-1]` 位移切片高效回算每个 completion token 的 old logprob，是 u3-l5 位移交叉熵的高效推理版。
- `TorchRolloutEngine` 是默认轻量后端：同进程、共享模型对象、`generate` 采样 + 事后回算 logps、`update_policy` 仅换引用。
- `SGLangRolloutEngine` 是高性能后端：独立进程、HTTP 通信、服务端直接返回 logprob、`update_policy` 走「rank0 写盘 + `/update_weights_from_disk` 热重载 + 广播 barrier」，是真正的训推分离。
- 两种后端共享同一个 `RolloutResult` 契约，是「依赖倒置」设计原则的良好范例。

## 7. 下一步学习建议

掌握了 rollout 引擎之后，建议按以下顺序继续：

1. **u7-l3 奖励信号：Reward Model 与奖励塑造**——rollout 返回的 `completions` 文本会喂给奖励模型和 `calculate_rewards`，理解奖励如何从文本变成标量。
2. **u7-l4 GRPO 与 CISPO**——看 `RolloutResult.per_token_logps` 如何作为 old_logps 进入 `ratio = exp(new_logps - old_logps)`，进而构造 PPO clip / CISPO clamp 损失。
3. **u7-l5 PPO：Actor-Critic 与 GAE**——看 PPO 如何在同一个 rollout 引擎之上额外引入 Critic 价值网络。
4. **u7-l6 Agentic RL**——看 `rollout_single` 如何**多次**调用 `rollout` 实现「生成 tool_call → 执行 → 拼 observation → 续写」的多轮循环，体会 rollout 引擎作为「采样原语」的可组合性。

建议继续精读的源码：`trainer/rollout_engine.py` 全文（仅 225 行，是理解训推分离最完整的参考实现），以及 `trainer/train_grpo.py` 的 `grpo_train_epoch`（第 71-203 行）作为引擎的标准消费方。
