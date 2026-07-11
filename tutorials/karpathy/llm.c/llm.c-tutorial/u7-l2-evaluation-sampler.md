# 评测：HellaSwag / MMLU 与采样器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 llm.c 为什么需要「评测（evaluation）」，以及它提供的两条评测路径分别解决什么问题。
- 理解 HellaSwag / MMLU 这类「四选一续写」任务的 **completion-style（续写式）** 评测协议：把每个候选续写拼到上下文后面，用模型对续写的平均下一个 token 损失来打分。
- 掌握 `dev/data/hellaswag.py`、`dev/data/mmlu.py` 如何准备数据，并通过 `write_evalfile` 把多选题打包成 C 端可读的 `.bin`。
- 掌握 `dev/eval` 的完整标准评测流程：`export_hf.py` 把 llm.c 权重导出成 HuggingFace 模型 → `run_eval.sh` 调用 EleutherAI `lm-evaluation-harness` 跑 6 个基准 → `summarize_eval.py` 汇总平均分。
- 理解 `llmc/sampler.h` 的 `sample_softmax` / `random_f32` 采样器，以及训练主循环里 `-h hellaswag_eval` 是如何周期性地把准确率打印出来的。

## 2. 前置知识

本讲默认你已经学完：

- **u1-l4（数据管线）**：知道 `.bin` 是「1024 字节头 + uint16 token 流」，下一个 token 预测靠 `targets[i]=inputs[i+1]` 错位一位实现。
- **u3-l3（采样与自回归生成）**：知道 `sample_mult` 用「逆累积分布函数」采样、自回归生成为何每个 token 都要重算前向。
- **u5-l1（CUDA 主线架构）**：知道 `train_gpt2.cu` 的训练主循环结构（验证 / 训练 / 采样 / 检查点）。
- **u6-l2（融合算子与 global norm）**：知道 `fused_classifier` 会产出 per-token 的 `losses(B*T)`。

几个本讲要用到的术语，先做通俗解释：

- **基准（benchmark）**：一个标准化的测试集，附带标准答案，用来给不同模型打「可比的分数」。训练 loss 只反映「拟合训练数据的程度」，不直接等价于「模型好不好用」，所以需要独立的基准。
- **下一个 token 损失（per-token loss）**：在位置 t，模型预测下一个 token 的负对数似然 \(-\log P(\text{token}_{t}\mid \text{前面所有 token})\)。损失越低，说明模型越「认可」这个 token。
- **多选题任务（multiple-choice）**：给一段上下文（context）和若干候选续写（ending），要求选出（模型认为）最合理的那一个。HellaSwag 和 MMLU 都是 4 选 1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [dev/data/hellaswag.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/hellaswag.py) | 下载 HellaSwag，做 Python 参考评测，并把数据写成 `hellaswag_val.bin` 供 C 端用 |
| [dev/data/mmlu.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/mmlu.py) | 下载 MMLU，做 Python 参考评测（同样的 completion-style 协议） |
| [dev/data/data_common.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py) | 提供 `write_evalfile`：把多选题打包成 C 端可读的 `.bin` |
| [dev/eval/export_hf.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/export_hf.py) | 把 llm.c 的 `.bin` 权重转换成 HuggingFace 模型（承接 u4-l2 的二进制协议） |
| [dev/eval/run_eval.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/run_eval.sh) | 调用 EleutherAI `lm-evaluation-harness` 跑 6 个基准任务 |
| [dev/eval/summarize_eval.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/summarize_eval.py) | 把 6 个任务的 json 结果汇总成一个平均分 |
| [dev/eval/README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/README.md) | 评测流程的官方说明文档 |
| [llmc/dataloader.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h) | 定义 `EvalLoader`：C 端读取评测 `.bin`、组 batch、判定正确数 |
| [llmc/sampler.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/sampler.h) | 推理采样器 `sample_softmax` / `random_f32` |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | `gpt2_validate` 产出 per-token loss；训练中周期性跑 HellaSwag；生成时用采样器 |

## 4. 核心概念与源码讲解

### 4.1 两条评测路径与 completion-style 协议

#### 4.1.1 概念说明

llm.c 提供两条评测路径，互为补充：

1. **轻量内联评测（训练中，只测 HellaSwag）**：用 `dev/data/hellaswag.py` 把数据离线打包成 `.bin`，训练主循环里周期性地读出来算准确率。优点是快、可与训练同步进行、能看到准确率随训练步数的上升曲线；缺点是只覆盖一个任务。
2. **完整标准评测（训练后，6 个基准）**：先把 llm.c 权重导出成 HuggingFace 模型，再交给社区标准工具 EleutherAI `lm-evaluation-harness`（与 HuggingFace Open LLM Leaderboard 完全一致的跑法）。优点是权威、可与其他模型横评；缺点是慢（README 说 1～3 小时）。

两条路径在 HellaSwag 上的「打分口径」略有不同，这是初学者最容易混淆的点，我们先把协议讲清楚。

HellaSwag / MMLU 本质都是 **四选一续写**：给上下文 `ctx` 和 4 个候选结尾 `ending[0..3]`，要选出最合理的一个。打分有两种 style：

- **multiple-choice style（多头选择式）**：只比较模型对 4 个候选「第一个 token」的概率。`lm-evaluation-harness` 默认走这条。
- **completion-style（续写式）**：把 `ctx + ending_i` 拼成一整段，计算模型对**整个 ending_i** 的平均下一个 token 损失，损失最低者胜出。llm.c 的 `dev/data` 脚本走这条。

`dev/data/hellaswag.py` 顶部注释明确记录了两种 style 的数字差异（GPT-2 124M）：

> eleuther harness reports acc 28.92%, acc_norm 31.14% (multiple choice style)
> this script: 10042 acc: 0.2859 acc_norm: 0.2955 (completion style)

可以看到两者接近但不相等——这是方法论差异，不是 bug。

#### 4.1.2 核心流程

completion-style 的核心是「用模型对续写的似然来打分」。给定上下文 \(c\) 和候选续写 \(e_i\)（一段 token 序列），模型对该续写的似然是：

\[
P(e_i \mid c) = \prod_{t} P\!\left(e_i[t] \mid c, e_i[:t]\right)
\]

取负对数得到损失（数值上更稳定）：

\[
\text{loss}(e_i) = -\sum_{t} \log P\!\left(e_i[t] \mid c, e_i[:t]\right)
\]

这正好就是 GPT-2 在每个位置上的 **下一个 token 交叉熵损失**之和。再除以续写长度得到「平均每个 token 的损失」：

\[
\text{avg\_loss}(e_i) = \frac{\text{loss}(e_i)}{\text{len}(e_i)}
\]

最终预测：

- `pred = argmin_i loss(e_i)`（未归一化，倾向短续写）→ 对应 `acc`
- `pred_norm = argmin_i avg_loss(e_i)`（按长度归一化）→ 对应 `acc_norm`

直觉：**模型认为「最顺理成章」的那个续写，就是它损失最低、最不意外的那个。**

`render_example` 的关键步骤：

1. `ctx_tokens = enc.encode(ctx)`：把上下文编码成 token。
2. 对每个候选 `end`，`end_tokens = enc.encode(" " + end)`——注意前置一个空格，这是 GPT-2 BPE 分词器的约定（词首与词中编码不同）。
3. 第 i 行 `tokens = ctx_tokens + end_tokens`，`mask` 在续写区域置 1、上下文区域置 0。
4. 4 行长度不一，collation 时统一 pad 到 `max_len`。

#### 4.1.3 源码精读

`render_example` 把一条样本渲染成 4 行 token + 4 行 mask，并额外存一份给 C 端用的 `data`：

[dev/data/hellaswag.py:63-100](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/hellaswag.py#L63-L100) —— 注意 L87 的 `enc.encode(" " + end)` 与 L89 的 `mask_rows = [0]*len(ctx_tokens) + [1]*len(end_tokens)`。

Python 参考评测的核心打分逻辑（前向 → 错位 → per-token 损失 → mask 区域求平均 → argmin）：

[dev/data/hellaswag.py:129-147](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/hellaswag.py#L129-L147) —— 关键三行：L142 `sum_loss = masked_shift_losses.sum(dim=1)`、L143 `avg_loss = sum_loss / shift_mask.sum(dim=1)`、L146-147 `pred = sum_loss.argmin()` / `pred_norm = avg_loss.argmin()`。

MMLU 的 `render_example` 用了完全相同的协议，只是上下文模板换成问答体 `"Question: {question}\n\nAnswer:"`，并把 ABCD 字母答案映射成 0/1/2/3：

[dev/data/mmlu.py:61-87](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/mmlu.py#L61-L87) —— 注意 L86 `"ABCD".index(example["label"])` 把字母标签转成索引。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 completion-style 评测的输出，并验证它依赖的是「整个续写的平均损失」而非单 token 概率。
2. **操作步骤**：在仓库根目录执行 `python dev/data/hellaswag.py -m gpt2 -d cuda`（无 GPU 可用 `-d cpu`，会很慢；只看前几十条即可用 `Ctrl-C` 中断）。脚本会逐条打印 `acc` 与 `acc_norm`。
3. **需要观察的现象**：前 10 条会打印上下文、4 个候选各自的 `avg_loss`、以及 `predicted / actual`；可以看到被选中的候选 `avg_loss` 通常是 4 个里最小的。
4. **预期结果**：随着样本数增加，`acc_norm` 收敛到注释里写的约 `0.2955`（GPT-2 124M）。如果你只跑到几百条就中断，数字会在这个值附近波动，属正常。
5. **若无法确定运行结果**：若没有网络或 GPU，明确标注「待本地验证」，转而阅读 [dev/data/hellaswag.py:155-162](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/hellaswag.py#L155-L162) 的 debug 打印来理解 `avg_loss` 的含义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `enc.encode(" " + end)` 要在候选续写前加一个空格？
**答案**：GPT-2 的 BPE 分词器对「词首」和「词中/词尾」使用不同的 token（例如 `" roof"` 和 `"roof"` 可能不是同一个 id）。续写在原文里紧跟上下文、前面本就有空格，加空格才能还原真实分词，否则模型看到的 token 序列与预训练分布不一致，打分会偏。

**练习 2**：`pred`（基于 `sum_loss`）与 `pred_norm`（基于 `avg_loss`）在什么情况下会给出不同答案？
**答案**：当某个候选续写特别短时，它的 `sum_loss`（损失之和）天然偏小，会被 `pred` 偏爱；而 `avg_loss` 除以了长度，抵消了这种「短即占便宜」的偏差。所以当正确答案恰好是较长续写、而某个错误短续写被 `pred` 误选时，两者就会不一致。`acc_norm` 通常比 `acc` 略高也是这个原因。

### 4.2 评测 .bin 文件格式与 C 端 EvalLoader

#### 4.2.1 概念说明

第 4.1 讲的是「Python 参考评测」。为了让 **C 端在训练中也能并行评测 HellaSwag**，需要把多选题数据离线打包成一个 C 能高效读取的 `.bin`。这个工作由 `write_evalfile` 完成，读取端是 `EvalLoader`。

这个 `.bin` 的格式和 u1-l4 讲的训练数据 `.bin` 思路一致（都是「头 + 流」），但专为「四选一」结构设计：

- **头**：256 个 int32。第 0 个是魔数 `20240522`（区别于训练数据的 `20240520`），第 1 个是版本号 `1`，第 2 个是样本总数 `num_examples`，第 3 个是「最长一条样本的字节数」`longest_example_bytes`（用来预分配读取缓冲）。
- **样本流**：一条条样本首尾相接，每条样本是一串 `uint16`，结构如下：

  ```
  <START_EXAMPLE=65535> <EXAMPLE_BYTES> <EXAMPLE_INDEX> <LABEL>
  <NUM_COMPLETIONS=4>
  <NUM_context> <context_tokens...>          （上下文 4 个候选共享，只存一份）
  <NUM_end0> <end0_tokens...>                （4 段候选续写）
  <NUM_end1> <end1_tokens...>
  <NUM_end2> <end2_tokens...>
  <NUM_end3> <end3_tokens...>
  ```

两个关键设计：

1. **`<EXAMPLE_BYTES>` 记录本条样本总字节数**：让读取端可以 `fseek` 一步跳过整条样本，从而多卡分片时能快速定位到自己负责的那段（不必逐 token 解析前面的样本）。
2. **`<START_EXAMPLE>=65535` 作分隔符**：`uint16` 最大值是 65535，用它当哨兵意味着 **任何真实 token id 都必须严格小于 65535**（`write_evalfile` 里有断言 `0 <= t < 2**16-1` 检查这点）。

#### 4.2.2 核心流程

`EvalLoader` 的核心任务是把每条「四选一」样本展开成 batch 里的 **4 行**：4 行共享同一段上下文 token，各自接上不同的候选续写。这样一次前向就能同时算出 4 个候选的损失。

`evalloader_next_example_` 在填 `inputs` / `targets` / `mask` 时有一个精巧设计：

- **inputs**：4 行都先填入相同的上下文，第 `c` 行再接上第 `c` 个候选续写。
- **targets**：在续写区域用「错位一位」——`targets[...][context_length + i - 1] = end_token[i]`，即「让上一个位置预测下一个 token」。这正是 u1-l4 讲过的下一个 token 预测编码。
- **mask**：在同样这些位置置 1，标记「这些位置的损失才计入该候选的打分」。

于是模型一次前向产出的 per-token 损失，在 mask=1 的位置恰好就是各候选续写的负对数似然。

`evalloader_stat_losses` 负责判定正确数：对每条样本的 4 行，分别在 mask=1 位置求损失平均，取 `argmin` 作为预测，与 `label` 比较即可（对应 4.1 讲的 `acc_norm` 口径）。

多卡分片：`examples_per_process = ceil(num_examples / num_processes)`，第 `r` 个进程负责 `[r * examples_per_process, (r+1) * examples_per_process)` 这一段，各卡评测不同样本、最后 all-reduce 汇总。

#### 4.2.3 源码精读

`EvalLoader` 结构体——注意它和训练 `DataLoader` 一样带 `process_rank` / `num_processes`，并额外有 `mask` 与 `label` 字段：

[llmc/dataloader.h:274-296](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L274-L296)

`evalloader_next_example_` 填 `inputs` / `targets` / `mask` 的核心——注意 L437 的 `targets[...][context_length + i - 1]` 错位、L441 的 `mask = 1`：

[llmc/dataloader.h:423-444](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L423-L444)

`evalloader_stat_losses` 判定正确数——对每个候选在 mask 区域求平均损失（L498 `average_loss /= count`），取 `argmin` 与 `label` 比较：

[llmc/dataloader.h:468-509](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L468-L509)

多卡分片与 `fseek` 快速跳过——L313-314 算出本进程的起止样本索引，L323-334 利用 `<EXAMPLE_BYTES>` 跳到起点：

[llmc/dataloader.h:298-338](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L298-L338)

写端的 `write_evalfile`——L78 魔数 `20240522`、L89 起逐字段写入样本、L98/L103 对 token id 做 `< 2**16-1` 断言：

[dev/data/data_common.py:62-121](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py#L62-L121)

#### 4.2.4 代码实践

1. **实践目标**：把「一条样本在 `.bin` 里的字节布局」与「读进 batch 后的 `inputs/targets/mask` 布局」对应起来。
2. **操作步骤**：先读 [dev/data/data_common.py:86-111](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/data_common.py#L86-L111)（写一条样本），再读 [llmc/dataloader.h:380-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L380-L447)（读一条样本）。
3. **需要观察的现象**：写端的 `stream.append(...)` 顺序与读端的 `loader->buffer[...]` 索引一一对应（`buffer[0]` 是 label、`buffer[1]` 是 NUM_COMPLETIONS、`buffer[2]` 是 context_length、`buffer[3:]` 是 context tokens）。
4. **预期结果**：你能在纸上画出一张表——给定 `ctx = [a,b]`、`end0 = [c,d]`，写出 batch 第 0 行的 `inputs = [a,b,c,d,0,...]`、`targets = [?, a-c, d-?, ...]`（其中 `a-c` 表示位置 1 的 target 是 c）、`mask = [0,1,1,0,...]`。
5. **若无法确定运行结果**：标注「待本地验证」，重点是确认读写两端字段顺序对齐。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.bin` 里要把上下文（context）只存一份，而不是每个候选都重复存一份？
**答案**：4 个候选共享同一段上下文，存一份既省磁盘（HellaSwag val 有 10042 条样本，上下文往往比候选长），也方便读取端把上下文「广播」填进 4 行，逻辑更简洁。

**练习 2**：`evalloader_stat_losses` 里如果某个候选的 `count == 0`（mask 区域没有 1），会发生什么？
**答案**：代码在 L498 用 `if (count > 0) { average_loss /= count; }` 保护，避免除零；此时 `average_loss` 保持 0.0f。同时 L482 的 `active` 标志只有在 mask 命中时才置 1，L504 用 `if (active && ...)` 确保完全空白的样本（例如 batch 末尾没填满）不计入正确数统计。

### 4.3 标准 6 基准评测：export_hf + lm-evaluation-harness

#### 4.3.1 概念说明

内联评测（4.2 节）只测 HellaSwag 一个任务、且是 completion-style。要得到能和 HuggingFace Open LLM Leaderboard 直接对比的完整成绩，必须用社区标准工具 **EleutherAI `lm-evaluation-harness`**。但这个工具只认 HuggingFace 格式的模型，所以流程是：

```
llm.c 训练产出的 .bin 权重
        │  export_hf.py
        ▼
HuggingFace 模型目录（config.json + safetensors + tokenizer）
        │  run_eval.sh → lm-evaluation-harness
        ▼
6 个基准任务的 json 结果
        │  summarize_eval.py
        ▼
6 项平均分
```

`export_hf.py` 承接 u4-l2 讲过的 `.bin` 协议：读 1024 字节头（魔数 `20240326`、版本 3=fp32 或 5=bf16），按预定义的 `shapes` 字典依次读出 16 类参数，把填充词表 `Vp` 截回真实词表 `V`，做权重绑定（`lm_head.weight = wte.weight`），最后 `save_pretrained` 写成 HF 模型。

#### 4.3.2 核心流程

6 个基准任务及其评测设置（`run_eval.sh` 里每行一个任务）：

| 任务 | 主指标 | few-shot |
| --- | --- | --- |
| truthfulqa_mc | mc2 | 0-shot |
| winogrande | acc | 5-shot |
| arc_challenge | acc_norm | 25-shot |
| hellaswag | acc_norm | 10-shot |
| gsm8k | acc | 5-shot |
| mmlu（57 个 hendrycksTest 子科目） | acc | 5-shot |

注意：这里 `lm-evaluation-harness` 跑的 HellaSwag 是 **10-shot multiple-choice style**，而 4.1/4.2 节的 Python 参考 / C 内联是 **0-shot completion-style**，所以两者的 HellaSwag 数字不能直接相等——这正好回应了练习里的「口径差异」。

`summarize_eval.py` 用一个 `key` 字典为每个任务指定主指标（如 hellaswag 取 `acc_norm`、mmlu 取 `acc`），把每项分数 ×100 后对 6 项求平均。

#### 4.3.3 源码精读

`export_hf.py` 的 `convert` 函数读头并按形状读权重——L54-59 解析 `maxT/V/L/H/C/Vp`，L64-81 定义 16 类参数的 `shapes`：

[dev/eval/export_hf.py:54-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/export_hf.py#L54-L92) —— 注意 L91-92 把 `wte` 从 `Vp` 行截回 `V` 行（丢弃填充），以及 L84 `dtype = np.float32 if version == 3 else np.int16`（bf16 借 int16 中转，见 u4-l2）。

权重绑定——`lm_head` 直接复用 `wte`，所以导出时只有 16 类参数：

[dev/eval/export_hf.py:103-105](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/export_hf.py#L103-L105)

`run_eval.sh` 的 6 个任务调用——每行 `python main.py --model hf-causal-experimental --tasks <任务> --num_fewshot <N>`：

[dev/eval/run_eval.sh:44-49](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/run_eval.sh#L44-L49)

`run_eval.sh` 末尾自动调用汇总脚本：

[dev/eval/run_eval.sh:51-52](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/run_eval.sh#L51-L52)

`summarize_eval.py` 的 `key` 字典为每个任务指定主指标：

[dev/eval/summarize_eval.py:11-32](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/summarize_eval.py#L11-L32)

#### 4.3.4 代码实践（本讲主实践任务）

1. **实践目标**：把 `export_hf.py` 与 `run_eval.sh` 的关系讲清楚，并解释 HellaSwag 任务如何从 4 个候选续写中选出概率最高者。
2. **操作步骤**：
   - 阅读 [dev/eval/README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/README.md) 全文，画出「`.bin` → export_hf → HF 模型 → run_eval（lm-evaluation-harness）→ json → summarize」的流程。
   - 对照 [dev/eval/run_eval.sh:37-52](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/run_eval.sh#L37-L52)：`$1` 是模型（HF 名或本地目录），`$2` 是结果目录名；脚本先 `cd lm-evaluation-harness`，再依次跑 6 个任务，最后回到根目录跑 `summarize_eval.py`。
3. **需要观察的现象**：README 里给出了一个 774M 模型的样例输出（hellaswag 10-shot 约 57.8 分），对比 4.1 节 Python 参考（GPT-2 124M completion-style 约 0.2955）——模型更大、且是 10-shot，所以分数高得多。
4. **预期结果（文字说明，回答主实践任务的两个问题）**：
   - **`export_hf.py` 与 `run_eval.sh` 的关系**：`export_hf.py` 是「格式转换器」，把 llm.c 自有的 `.bin` 权重翻译成 `lm-evaluation-harness` 能加载的 HuggingFace 模型；`run_eval.sh` 是「评测驱动器」，接收导出后的 HF 模型路径，调用 harness 跑 6 个基准并汇总。前者是后者的前置依赖——没有 `export_hf`，harness 不认识 llm.c 的权重。
   - **HellaSwag 如何选出概率最高者**：对 4 个候选，分别把「上下文 + 候选」拼成一段喂给模型，计算模型对候选续写部分每个 token 的负对数似然，在续写区域求平均得到 `avg_loss`；4 个候选中 `avg_loss` 最小（即似然最高、模型最「认可」）的那个就是预测答案，与标注 `label` 比对即判正误。（C 端 `evalloader_stat_losses` 走完全相同的口径。）
5. **若无法本地跑通**：完整流程需要 clone `lm-evaluation-harness`、下载 6 个数据集、单次跑 1～3 小时。若资源不足，标注「待本地验证」，重点完成上面的文字说明与流程图。

#### 4.3.5 小练习与答案

**练习 1**：README 里建议在导出模型的 `config.json` 中手动加上 `"_attn_implementation": "flash_attention_2"`，为什么？
**答案**：README 明确说「在 bfloat16 下不用 FlashAttention 2 时分数会明显偏低，且这个问题没有完全解决」。FlashAttention 2 在数值上更稳定（尤其长序列、bf16 下），所以作为一个临时 workaround 来保证评测分数不被注意力实现的数值误差拖低。

**练习 2**：`summarize_eval.py` 为什么需要 `key` 字典为每个任务指定不同的主指标？
**答案**：不同基准社区约定的「主指标」不同——分类任务用 `acc`、需要长度归一化的用 `acc_norm`（如 hellaswag、arc_challenge）、TruthfulQA 用 `mc2`。如果不区分、一律取 `acc`，就会取错字段或取不到，汇总结果就错了。

### 4.4 采样器与训练中的评测调用

#### 4.4.1 概念说明

`llmc/sampler.h` 是推理阶段从 logits 选 token 的工具，提供两个函数：

- `random_f32(state)`：基于 xorshift\* 的伪随机数生成器，返回 `[0,1)` 的 float（与 u3-l3 讲的 CPU 版 `random_f32` 同源，同一个 xorshift\* 算法）。
- `sample_softmax(logits, n, coin)`：直接吃 **logits**（未归一化），内部做 softmax 后按概率区间采样出一个 token id。

注意它和 u3-l3 的 CPU 版 `sample_mult` 的区别：`sample_mult` 吃的是已经 softmax 过的 `probs`；`sample_softmax` 吃的是原始 `logits`，自己先 `exp` 求和。两者统计等价（都是逆累积分布函数采样），只是输入阶段不同。

#### 4.4.2 核心流程

`sample_softmax` 的一个小优化：它**不求归一化概率**，而是把随机硬币 `coin` 缩放到未归一化的总和空间：

1. `norm = ∑ exp(logits[i])`（softmax 的分母）。
2. `coin *= norm`——把原本 `[0,1)` 的硬币等比放大到 `[0, norm)`。
3. 累加 `cdf += exp(logits[i])`，第一次 `coin < cdf` 时返回 `i`。

因为每段区间长度正比于 `exp(logits[i])`，所以 P(返回 i) 正比于 `exp(logits[i])`，即标准 softmax 分布。这样省去了「先归一化整个概率向量」这一步。

训练主循环里 HellaSwag 评测的触发条件是 `run_hellaswag = hellaswag_eval && hellaswag_available`——既要在命令行传 `-h 1`，又要求 `dev/data/hellaswag/hellaswag_val.bin` 存在。满足后，每隔 `val_loss_every` 步：

```
evalloader_reset()                           # 多卡分片、定位到本卡起点
for i in range(num_batches):
    evalloader_next_batch()                  # 把若干样本展开成 B×T 的 inputs/targets/mask
    gpt2_validate(...)                        # 前向 + fused_classifier，产 per-token cpu_losses
    correct = evalloader_stat_losses(...)     # 用 mask 区域平均损失判正确数
    eval_acc_norm += correct
eval_acc_norm = multi_gpu_cpu_float_sum(...)  # 跨卡 all-reduce 汇总
print("HellaSwag: %d/%d = %f", ...)
logger_log_eval(...)
```

注意 `gpt2_validate` 是关键桥梁：它不仅返回平均 loss，还会把 **每个位置的 per-token 损失**填进 `model->cpu_losses`（`B*T` 个 float），这正是 `evalloader_stat_losses` 判定候选优劣所需要的。

采样器在「生成文本」分支也用到：每个时间步前向后，取 `logits[0, t-1, :]` 拷回 CPU，抛一枚硬币调 `sample_softmax` 采出下一个 token（承接 u3-l3 的自回归生成）。

#### 4.4.3 源码精读

`sample_softmax` 与 `random_f32`：

[llmc/sampler.h:10-39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/sampler.h#L10-L39) —— 注意 L29-30 的 `coin *= norm` 优化、L34 的 `if (coin < cdf) return i;`、L38 兜底 `return n - 1`。

`gpt2_validate` 产出 per-token 损失——注释 L760 明确说「部分评测（如 HellaSwag）需要 per-token 损失」：

[train_gpt2.cu:758-786](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L758-L786) —— 关键是 L778 `fused_classifier(...)` 填 `acts.losses`、L779 拷回 `model->cpu_losses`。

训练中 HellaSwag 评测循环：

[train_gpt2.cu:1732-1749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1732-L1749) —— L1741 `gpt2_validate`、L1742 `evalloader_stat_losses`、L1746 跨卡汇总、L1747 打印准确率。

HellaSwag 的开关与文件检查（`-h` 解析、`.bin` 是否存在）：

[train_gpt2.cu:1626-1647](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1626-L1647) —— L1630 `run_hellaswag = hellaswag_eval && hellaswag_available`、L1644-1646 在文件缺失时打印提示「`python dev/data/hellaswag.py` 导出后用 `-h 1`」。

生成分支调用采样器：

[train_gpt2.cu:1782-1784](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1782-L1784) —— `coin = random_f32(...)`、`next_token = sample_softmax(cpu_logits, vocab_size, coin)`。

`-h` 命令行参数解析与帮助文本：

[train_gpt2.cu:1489](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1489) （解析） / [train_gpt2.cu:1397](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1397) （帮助）。

#### 4.4.4 代码实践

1. **实践目标**：在训练中看到 HellaSwag 准确率随步数上升。
2. **操作步骤**：
   - 先生成评测 `.bin`：`python dev/data/hellaswag.py`（需要联网下载 HellaSwag、有 `tiktoken` / `transformers` / `torch`）。它会跑完 Python 参考评测并在末尾写出 `dev/data/hellaswag/hellaswag_val.bin`（见 [dev/data/hellaswag.py:165-166](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/data/hellaswag.py#L165-L166)）。
   - 再以 `-h 1` 启动 CUDA 训练：`make train_gpt2cu && ./train_gpt2cu log124M/ ... -h 1`（完整参数参考 `scripts/run_gpt2_124M.sh`，并保证 `-b` 给的 batch size ≥ 4，否则 `evalloader_reset` 会因 `can_fit_examples == 0` 报错退出，见 [llmc/dataloader.h:304-310](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L304-L310)）。
3. **需要观察的现象**：配置表会打印 `| run hellaswag | yes |`；每隔 `val_loss_every` 步会打印一行 `HellaSwag: X/10042 = 0.xx`，准确率随训练逐步上升（从随机猜测的 0.25 起步）。
4. **预期结果**：GPT-2 124M 从随机初始化训练时，早期 HellaSwag 准确率约 0.25（4 选 1 的随机基线），随 loss 下降而缓慢上升。注意：要从头训练到接近注释里的 0.29 需要大量算力，「待本地验证」主要看准确率「会动、会升」即可。
5. **若无法本地跑通**：若无 GPU 或无时间训练，标注「待本地验证」，改为阅读 [train_gpt2.cu:1732-1749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1732-L1749) 复述评测循环的 5 个步骤。

#### 4.4.5 小练习与答案

**练习 1**：`sample_softmax` 为什么用 `coin *= norm` 而不是先把 logits 归一化成概率再采样？
**答案**：归一化需要遍历一次 logits 算 `norm`、再遍历一次把每个 `exp(logits[i])` 除以 `norm`——两次遍历。而 `coin *= norm` 把硬币放大到 `[0, norm)` 后，直接用未归一化的 `exp(logits[i])` 累加 CDF 即可，省掉了第二次「除以 norm」的遍历。两者选出的分布完全相同（都是 softmax 分布），后者更省计算。

**练习 2**：为什么训练中的 HellaSwag 评测要在 `if (step > 0 && step % val_loss_every == 0)` 条件下才跑，而不是每步都跑？
**答案**：跑一次完整 HellaSwag val（10042 条样本）需要很多次前向，开销远大于一个训练步。每步都跑会严重拖慢训练、且准确率在相邻步间几乎不变（没有观测价值）。所以只在周期性检查点（与验证 loss 同频率）跑一次，用少量额外开销换取「准确率随训练变化」的曲线。

## 5. 综合实践

把本讲三条线索串起来，完成一次「双口径对比」：

1. **生成评测数据**：运行 `python dev/data/hellaswag.py`，记录它打印的 Python 参考 `acc_norm`（completion-style，0-shot），并确认生成了 `dev/data/hellaswag/hellaswag_val.bin`。
2. **内联评测**：用一个已训练好的 llm.c GPT-2 权重（如 `log124M/` 下的 checkpoint，或 starter pack 的 `gpt2_124M.bin`）启动训练并加 `-h 1`，记录若干步后打印的 `HellaSwag: X/10042 = ...`。验证它和第 1 步的 Python 参考 `acc_norm` 接近（都是 0-shot completion-style，口径一致）。
3. **标准评测**：用 `export_hf.py` 把同一个权重导出成 HF 模型，再按 `dev/eval/README.md` 跑 `run_eval.sh`，记录其中 `hellaswag_10shot.json` 的分数（10-shot multiple-choice style）。
4. **对比与分析**：在一份表格里列出三种口径的 HellaSwag 分数，用一句话解释为何第 3 步的数字与第 1、2 步不同（shot 数不同 + style 不同）。再解释为何第 1、2 步应当接近（同一口径，只是执行器从 Python 换成 C）。

> 说明：完整 `run_eval.sh` 需要 clone `lm-evaluation-harness` 并跑 1～3 小时。若资源受限，第 3 步可只读 [dev/eval/run_eval.sh:47](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/eval/run_eval.sh#L47) 与 README 样例输出，标注「待本地验证」即可；第 1、2 步是本综合实践的核心。

## 6. 本讲小结

- llm.c 提供两条评测路径：**训练中内联 HellaSwag**（快、周期性、completion-style）与 **训练后 6 基准 lm-evaluation-harness**（权威、可横评、multiple-choice style）。
- HellaSwag / MMLU 都是四选一续写任务；completion-style 用「模型对整个候选续写的平均下一个 token 损失」打分，`avg_loss` 最小者胜出（对应 `acc_norm`）。
- `write_evalfile` 把多选题打包成专用 `.bin`（魔数 `20240522`、上下文只存一份、`<EXAMPLE_BYTES>` 支持 fseek 跳过、`<START_EXAMPLE>=65535` 作哨兵），由 C 端 `EvalLoader` 读取并展开成 batch 的 4 行。
- `EvalLoader` 用「targets 错位一位 + mask 标记续写区」把下一个 token 预测编码进 batch，`evalloader_stat_losses` 在 mask 区求平均损失判正确，并支持多卡分片 + all-reduce 汇总。
- 标准 6 基准流程是 `export_hf.py`（`.bin`→HF 模型，承接 u4-l2 协议）→ `run_eval.sh`（lm-evaluation-harness）→ `summarize_eval.py`（按各任务主指标汇总平均）。
- `sample_softmax` 用 `coin *= norm` 优化省掉一次归一化遍历；训练中 HellaSwag 评测依赖 `gpt2_validate` 产出的 per-token `cpu_losses` 作为判分基础。

## 7. 下一步学习建议

- **下一讲 u7-l3（性能剖析）**：本讲多次提到「评测慢、训练慢」，下一讲会讲 `profile_gpt2.cu` 如何配合 NVIDIA Nsight Compute 定位性能瓶颈 kernel，帮你理解为什么评测和训练会花那么多时间。
- **继续阅读源码**：想深入了解 completion-style 与 multiple-choice style 的数值差异，可对比本讲的 `dev/data/hellaswag.py` 与 `lm-evaluation-harness` 里 hellaswag 任务的实现。想了解 `fused_classifier` 如何一次算出 per-token 损失，可回看 u6-l2 与 [llmc/fused_classifier.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh)。
- **动手扩展**：尝试仿照 `hellaswag.py` + `write_evalfile` 的模式，为另一个四选一数据集（如 ARC-easy）生成 `.bin`，并验证 `EvalLoader` 能否直接复用读取（提示：只要满足「4 个候选、共享上下文」的结构即可）。
