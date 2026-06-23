# generate.py 与 llama_repl.py 脚本解读

> 本讲对应手册单元 U2·L3，承接 [U2·L1]（三种执行模式 torch / pyvm / mk）。建议你已读完那一讲，知道 `Generator` 基类 + 三个子类的分工、以及 `generate` 与 `generate_with_eos` 的区别。本讲不再重复解释执行器内部，而是把镜头拉远到**脚本层**：两个入口脚本是如何把"加载模型 → 构建调度 → prefill → decode → 计时"这条流水线串起来的。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **prefill 与 decode 两个阶段各自在做什么、为什么必须分开**：prefill 一次性"吃下"整段 prompt 算出第一个新 token；decode 此后每步只喂一个 token、产出一个 token，循环直到生成长度或 EOS。
2. 看懂基准脚本 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) 与交互脚本 [llama_repl.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py) 里 prefill 的同一段代码（`model(prefill_inp)` 后取 `[:, -1:]`），并解释为什么只取最后一个位置的预测。
3. 解释 **为什么 decode 每步只生成一个 token**（自回归依赖 + KV 缓存），并指出两个脚本在"是否提前停止"上的差异：`generate.py` 调固定步数的 `generate`（基准要稳定步数），`llama_repl.py` 调 `generate_with_eos`（聊天遇到 EOS 就停）。
4. 看懂 **CUDA Event 计时**：`enable_timing=True`、`record()`、`torch.cuda.synchronize()`、`elapsed_time()` 四件套，以及为什么要把 prefill 排除在计时循环之外、为什么要做 `num_warmup` 次预热。
5. 亲手算出两种 **tokens/秒** 公式的来历：基准脚本的 `(ntok-1)/elapsed * batch_size` 与交互脚本的 `num_generated/elapsed`。

## 2. 前置知识

- **自回归生成（autoregressive generation）**：大模型一次只预测"下一个 token"。把它预测出的 token 拼回输入再预测下一个，如此循环。本讲的重点是：**预测第一个 token 和预测后续 token 是两种形状完全不同的计算**。
- **prefill / decode 两阶段**（本讲核心概念）：
  - **prefill（预填充）**：用户给出一段 prompt，模型对**整段 prompt** 做一次前向，算出"紧跟在 prompt 后面的那个 token"。这一步是"一次处理很多 token"的并行计算。
  - **decode（解码）**：拿到第一个 token 之后，每一步只**新进来一个 token**（上一步刚预测的那个），模型借助此前已经算好并缓存下来的中间状态（KV cache）推出"下一个 token"。这一步是"一次处理一个 token"的串行循环。
- **KV 缓存（KV cache）**：注意力计算里，每个历史 token 的 Key/Value 算过一次后可以缓存，下一步不必重算。正是有了 KV 缓存，decode 才能每步只喂一个新 token——历史的部分由缓存提供。
- **三种执行模式**（U2·L1）：`torch`/`pyvm`/`mk` 是三种"执行器"。本讲不关心执行器内部，只关心**脚本如何调用它们的 `generate` / `generate_with_eos`**——这两个方法对三种模式都是同一份接口。
- **CUDA Event 计时**：GPU 计算是异步的（CPU 发出指令后不等它算完）。要准确测一段 GPU 工作的耗时，需要用"事件（event）"在 GPU 时间线上打两个戳，再求差。

如果上面某几个词还陌生，记住一句话即可：**prefill 是"一次算很多"，decode 是"每步算一个"——脚本把这两段分别编排，并对后者计时**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) | 基准脚本：固定 prompt + 固定步数，反复计时并打印 tokens/sec |
| [megakernels/scripts/llama_repl.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py) | 交互聊天脚本：每轮做 chat 模板拼装 + prefill + 带 EOS 的 decode，循环读输入 |
| [megakernels/generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) | `Generator.generate`（固定步数 decode）与 `generate_with_eos`（带 EOS 的 decode）定义；decode 循环的真正实现 |
| [megakernels/llama.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py) | `LlamaForCausalLM.forward`、`LlamaLMHead.forward`：解释 prefill 为何能一次性给出每个位置的预测 |
| [megakernels/model_types.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py) | `BatchState` 数据类：两个脚本都用它打包 `input_ids`/`position_ids`/`seq_len` 喂给模型 |

## 4. 核心概念与源码讲解

### 4.1 prefill 阶段：一次前向吃下整段 prompt

#### 4.1.1 概念说明

用户给的 prompt 长度不固定（可能十几个 token，也可能几千）。模型要生成回答，第一步必须先"消化"整段 prompt，算出**第一个要输出的 token**。这一步就叫 **prefill**。

prefill 的特点：

- **一次性处理所有 prompt token**：把整段 `input_ids`（形状 `(1, prompt_len)`）整体送进模型，做一次完整前向。
- **每个位置都会给出预测**：因果语言模型对序列中每个位置都预测"它后面那个 token"。位置 0 预测 token 1，位置 1 预测 token 2……位置 `prompt_len-1` 预测**紧跟在 prompt 之后的新 token**。
- **只取最后一个**：前面那些位置的预测都在预测 prompt 内部"已知"的 token，没有用；只有**最后一个位置**的预测才是我们真正想要的"第一个生成 token"。

所以两个脚本里都出现同一句关键代码：`new_input_token = prefill_output.output_ids[:, -1:]`——取最后一列，就是这个首 token。这个首 token 随后被放进 `output_tokens[:, 0]`，成为 decode 循环的起点。

> 为什么不直接把 prompt 也丢进 decode 循环逐 token 算？因为那样要串行算 `prompt_len` 步、每步一个 token，极慢。prefill 把这 `prompt_len` 步**并行成一次前向**，是性能的关键。这也正是 prefill 必须和 decode 分开的原因。

#### 4.1.2 核心流程

两个脚本的 prefill 段几乎一字不差，伪代码如下：

```
input_ids = tokenizer(prompt)            # 形状 (1, prompt_len)，已在 GPU 上
prompt_len = input_ids.shape[-1]
position_ids = arange(prompt_len)        # 0, 1, ..., prompt_len-1

prefill_inp = BatchState(input_ids, position_ids)
prefill_output = model(prefill_inp)      # ← 一次前向，吃下整段 prompt
new_token = prefill_output.output_ids[:, -1:]   # ← 只取最后一个位置的预测

output_tokens[:, 0] = new_token          # ← 这个首 token 成为 decode 的起点
```

注意三个要点：

1. **`position_ids = arange(prompt_len)`**：prefill 时位置编号是连续的 0..prompt_len-1，因为整段 prompt 是同时（并行）被处理的。
2. **`[:, -1:]`**：保留最后一列（且保留 batch 维），形状从 `(1, prompt_len)` 变成 `(1, 1)`，正好可作为 decode 的单 token 输入。
3. **prefill 在计时循环之外**：两个脚本都把 `model(prefill_inp)` 放在计时事件 `record()` 之前。基准要测的是"解码吞吐"，prefill 只算一次，不计入。

#### 4.1.3 源码精读

**generate.py 的 prefill 段**——从分词到取出首 token：

[megakernels/scripts/generate.py:114-137](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L114-L137) —— 第 114–115 行把 `input_ids` 搬到 GPU 并记下 `prompt_len`；第 119–123 行构造 `position_ids = arange(prompt_len)`；第 125–128 行打包成 `BatchState`；**第 130 行 `prefill_output = model(prefill_inp)` 就是 prefill 的那一一次前向**；第 132 行 `new_input_token = prefill_output.output_ids[:, -1:]` 取首 token；第 134–137 行分配 `output_tokens` 并把首 token 写到第 0 列。

**llama_repl.py 的 prefill 段**——完全相同的套路，只是套在闭包 `generate(messages)` 里：

[megakernels/scripts/llama_repl.py:98-107](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L98-L107) —— 第 103 行 `prefill_output = model(prefill_inp)`，第 105 行 `new_input_token = prefill_output.output_ids[:, -1:]`，第 107 行 `output_tokens[:, 0] = new_input_token`。与 generate.py 一一对应。

**为什么取 `[:, -1:]` 就够？** 看 lm head 的实现：

[megakernels/llama.py:384-403](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L384-L403) —— 第 394 行 `logits = self.lm_head(hidden_states)` 对**每个位置**都算出词表维度的 logits；第 396 行 `next_token_ids = logits.argmax(dim=-1)` 沿词表维取 argmax，于是 `output_ids` 的形状是 `(batch, seq_len)`，**每个位置一个预测**。prefill 时 `seq_len = prompt_len`，所以 `output_ids` 有 `prompt_len` 个预测，最后一个正是 prompt 之后的首 token。其余位置预测的是 prompt 内部已知 token，故丢弃。

而整段 prompt 之所以能"一次并行"算完，是因为模型主干对完整序列做一次前向：

[megakernels/llama.py:477-487](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L477-L487) —— `LlamaModel.forward` 先 embed 整段 `input_ids`，再 `for layer in self.layers: out = layer(out)` 把所有 transformer 层跑完，全程在完整 `seq_len` 上并行——这就是 prefill "一次算很多"的来源。

#### 4.1.4 代码实践

**目标**：亲手改 prompt 和长度，确认 prefill 取出的是"prompt 之后的首 token"。

1. 改 prompt 并缩小生成长度，跑 `torch` 模式（无需编译 megakernel，最易跑通）：

   ```bash
   # 仓库根目录
   python megakernels/scripts/generate.py mode=torch \
       prompt="The capital of France is" ntok=8
   ```

   pydra 的命令行约定是 `key=value` 形式，所以改 `prompt=` 和 `ntok=` 即可，无需改源码。

2. 观察打印的 `Input ids shape:` 这一行：它的第二个维度就是 `prompt_len`。再观察 `Output ids:`，第 0 个就是 prefill 取出的首 token（例如很可能是指向 "Paris" 的 token）。
3. 把同一个 `prompt` 换成更长的句子再跑一次，对比 `Input ids shape` 的变化——确认 prompt 越长，prefill 一次前向处理的 token 越多。

**预期结果**：`Input ids shape` 随 prompt 长度变化；输出序列的第 0 个 token 是对"prompt 之后下一个词"的预测，语义上合理。

> 若本地无 GPU/权重：改为**源码阅读型实践**——对照 4.1.3，在 `llama.py:394-396` 处确认 lm head 对每个位置都产生一个 argmax 预测，从而理解 `[:, -1:]` 为何恰好取出首 token。

#### 4.1.5 小练习与答案

**练习 1**：如果脚本误写成 `new_input_token = prefill_output.output_ids[:, :1]`（取第一列而非最后一列），decode 会从哪个 token 开始？会发生什么？
**参考答案**：会取**位置 0** 的预测，即"预测 prompt 的第 2 个 token"——这通常不是我们想要的"prompt 之后的首 token"，后续 decode 会从错误起点发散，生成与 prompt 无关的内容。这正是为什么必须用 `[:, -1:]` 取最后一个位置。

**练习 2**：为什么 `position_ids = arange(prompt_len)`（连续的 0,1,...）在 prefill 是对的，而 decode 阶段每步的位置编号是单独传入的 `pos_id`（见 4.2）？
**参考答案**：prefill 把整段 prompt 一次性并行处理，每个 token 的位置就是它在序列里的自然序号 0..prompt_len-1。decode 阶段每步只有一个新 token，它的位置是"当前序列长度"，随步数增长，所以要单独传一个标量 `pos_id`，告诉模型这个新 token 处在第几号位置（用于 RoPE 旋转位置编码）。

---

### 4.2 decode 循环与 eos 检测：每步一个 token

#### 4.2.1 概念说明

prefill 产出首 token 后，剩下的 token 全部由 **decode** 循环逐个生成。decode 的根本特点是：**每一步只喂一个 token、只产出一个 token**。

为什么必须"每步一个"？因为自回归的串行依赖：要预测第 k+1 个 token，必须先知道第 k 个 token 是什么；而第 k 个 token 是上一步刚用 `argmax` 采样出来的，**在它被采样出来之前，无法预测第 k+1 个**。所以 decode 不可能像 prefill 那样并行多步——它本质上是一个"采样一个、喂回一个"的串行循环。

那为什么不用每步重算整段历史？因为 **KV 缓存**：每层注意力把历史 token 的 Key/Value 存在缓存里，decode 每步只需把**一个新 token** 的 Q/K/V 算出来，再让它 attend 到缓存里的全部历史 K/V 即可。所以输入张量的"序列维"每步都长度为 1，但注意力看到的有效上下文长度 `seq_len` 每步 +1。

> 一句话概括：**decode 每步只生成一个 token，是因为下一个 token 依赖上一步刚采样出的 token，存在串行依赖；而历史部分由 KV 缓存代劳，所以每步的实际输入只有一个 token。**

两个脚本在"decode 如何结束"上不同：

| 脚本 | 调用方法 | 结束条件 | 为什么这么选 |
| --- | --- | --- | --- |
| `generate.py` | `generate(...)` | 固定跑满 `ntok-1` 步 | 基准测试要**固定步数**，计时才稳定可比 |
| `llama_repl.py` | `generate_with_eos(...)` | 遇到 EOS 提前停 | 聊天应在自然结束处停止，不能硬跑满 |

`generate_with_eos` 的内部实现（分块循环 + EOS 比对）已在 [U2·L1 的 4.1 节](u2-l1-three-execution-modes.md)详细讲过，本讲只关注脚本如何**使用**它。

#### 4.2.2 核心流程

decode 循环本身的骨架（无论哪种模式，都在 `Generator.generate` 里）：

```
# 已生成 ntok_already_generated 个 token（首 token 算第 1 个）
for i in range(ntok):                     # 本批要生成 ntok 个新 token
    读入上一个 token：output_tokens[:, 当前位置]
    用 pos_id = prompt_len + 已生成数 + i - 1 标记它的位置
    output_ids = run(这个 token, pos_id)   # ← 一次前向，只处理这一个 token
    output_tokens[:, 下一个位置] = output_ids
```

关键点：循环每轮只挑出**一个** token 喂进 `run`/`model`，产出一个新 token 写到下一格。位置编号 `pos_id` 随步数递增，反映"当前 decode 到了第几号位置"。

以 `generate.py` 为例，它调用 `gen.generate(output_tokens, prompt_len, config.ntok - 1)`——固定生成 `ntok-1` 步（首 token 已由 prefill 给出，所以是 `ntok-1` 而非 `ntok`），**全程不检查 EOS**。

`llama_repl.py` 则调用 `gen.generate_with_eos(..., eos_token_check_interval=16)`：每生成 16 个 token 就把这一批搬到 CPU 比对一次 EOS，命中就提前返回。

#### 4.2.3 源码精读

**decode 循环的真正实现（mk 模式）**——注意每步只取一个 token：

[megakernels/generators.py:145-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L145-L163) —— 第 159 行 `input_ids = output_tokens[:, input_token_pos : input_token_pos + 1]` 只切出**长度为 1**的一段（单个 token）；第 161 行 `pos_id = prompt_len + ntok_already_generated + i - 1` 计算这个 token 的位置编号；第 162 行 `output_ids = self.run(input_ids, pos_id=pos_id)` 做一次单 token 前向；第 163 行把结果写到 `output_token_pos = input_token_pos + 1`。**整段循环每轮喂 1 个、写 1 个**，这正是 decode 的串行本质。

**decode 循环（torch 模式）**——同样的"每步一个"，但能看到 `seq_len` 在增长：

[megakernels/generators.py:68-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L68-L92) —— 第 85 行输入仍是 `output_tokens[:, input_token_pos : input_token_pos + 1]`（单个 token）；注意第 87 行 `seq_len=starting_seq_len + i + 1`——虽然每步只喂一个新 token，但模型看到的有效序列长度每步 +1（历史由 KV 缓存提供）。这就是"输入长度恒为 1、上下文长度逐步增长"的解码语义。

**两个脚本对 decode 的不同调用**：

[megakernels/scripts/generate.py:174](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L174) —— `gen.generate(output_tokens, prompt_len, config.ntok - 1)`，固定 `ntok-1` 步、无 EOS。这是为稳定计时而设计的。

[megakernels/scripts/llama_repl.py:110-116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L110-L116) —— `gen.generate_with_eos(output_tokens=..., prompt_len=prompt_len, ntok=config.max_tokens_per_turn, eos_token_ids=eos_token_ids, eos_token_check_interval=16)`，命中 EOS 即停。这里的 `eos_token_ids` 来自第 39–42 行 `GenerationConfig.from_pretrained(...).eos_token_id`（单个 int 会被包成 list）。

**带 EOS 的提前停止逻辑**（回顾，详见 U2·L1）：

[megakernels/generators.py:21-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L21-L58) —— `generate_with_eos` 用 `range(1, ntok, eos_token_check_interval)` 分块，每块调一次 `self.generate(...)`（第 42 行），再把这批 token `.cpu()` 出来逐个比对 `eos_token_ids`（第 52–56 行），命中就 `return (命中位置, 已生成数)`。第 32 行断言 `batch size must be 1`：带 EOS 的路径只服务单样本交互。

#### 4.2.4 代码实践

**目标**：在源码里**标出 prefill 与 decode 的分界线**，并验证 decode 每步只处理一个 token。

1. 打开 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py)，按下面这张表在注释里画出分界（**这是阅读标注，不要提交**）：

   | 阶段 | generate.py 行号 | llama_repl.py 行号 | 代码 |
   | --- | --- | --- | --- |
   | prefill | 第 130 行 | 第 103 行 | `model(prefill_inp)` |
   | 取首 token | 第 132 行 | 第 105 行 | `output_ids[:, -1:]` |
   | ↓ 分界线 ↓ | —— | —— | （此后进入 decode） |
   | decode（固定步数） | 第 174 行 `gen.generate(...)` | —— | 计时事件包住 |
   | decode（带 EOS） | —— | 第 110 行 `gen.generate_with_eos(...)` | 计时事件包住 |

2. 跳进 decode 实现 [generators.py:159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L159)，确认切片 `input_token_pos : input_token_pos + 1` 只取 1 个 token；再对照 [generators.py:87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L87) 看 `seq_len` 如何随步数增长。
3. **回答关键问题**：为什么 decode 每步只生成一个 token？（用你自己的话，再对照 4.2.1 的概括核对。）

**预期结果**：你能清楚说出分界线——prefill 是计时循环之前的一次 `model(...)`，decode 是计时循环里反复调用的 `gen.generate(...)`/`gen.generate_with_eos(...)`；并解释清楚"下一个 token 依赖上一个采样结果 + 历史由 KV 缓存代劳"两点。

#### 4.2.5 小练习与答案

**练习 1**：`generate.py` 为什么不使用 `generate_with_eos`，而要固定跑满 `ntok-1` 步？
**参考答案**：因为它是**基准脚本**，目标是稳定、可比地测"解码 N 步"的耗时与吞吐。如果用 EOS 提前停止，不同 prompt/温度下实际步数不同，每次计时的工作量就不一样，tokens/sec 无法横向比较。固定步数保证每次计时做的工作量完全一致。

**练习 2**：`llama_repl.py` 里 `eos_token_check_interval=16`，为什么不是 1（每生成一个 token 就查一次 EOS）？
**参考答案**：每次把 token 从 GPU 搬到 CPU 做比对是有开销的（`output_tokens[...].cpu()` 涉及显存→内存传输与同步）。每 16 步查一次，把这种搬运/同步的频率降到 1/16，减少对 decode 主循环的干扰，同时最多"多生成 15 个 token 才停"，对聊天体验影响很小。这是延迟与"及时停止"之间的折中。

---

### 4.3 计时与 tokens/秒：CUDA Event 与预热

#### 4.3.1 概念说明

GPU 计算是**异步**的：CPU 提交一条 GPU 指令后立刻返回，不会等 GPU 算完。所以用 `time.time()` 在 CPU 侧前后取差，测到的往往只是"CPU 提交指令的时间"，而不是 GPU 真正算的时间。要准确测 GPU 工作耗时，标准做法是用 **CUDA Event**：

1. 建两个 event（`enable_timing=True`）。
2. 在工作**之前** `start_event.record()`，在**之后** `end_event.record()`——这两个戳打在 GPU 时间线上。
3. `torch.cuda.synchronize()` 等 GPU 把所有工作（包括 end_event）都做完。
4. `start_event.elapsed_time(end_event)` 得到两个戳之间的毫秒数。

两个脚本都把这套机制**只包在 decode 上**：prefill 在事件之外，计时只反映"解码 N 个 token"的代价。此外都有**预热（warmup）**：前若干次运行不计入统计，用来让 cuBLAS/内核 autotuning、GPU 时钟频率稳定下来，避免冷启动拖高平均值。

两者的吞吐口径也略有不同，反映了各自目的：

| 脚本 | 公式 | 含义 |
| --- | --- | --- |
| `generate.py` | `(ntok - 1) / elapsed * batch_size` | 每秒 decode 前向数 × batch；标准化基准 |
| `llama_repl.py` | `num_generated / elapsed` | 这一轮实际生成的 token 数 / 这一轮耗时 |

#### 4.3.2 核心流程

`generate.py` 的计时循环：

```
times, cpu_times = [], []
for _ in range(num_warmup + num_iters):
    start_event.record()                   # GPU 时间线：起点
    cpu_start = time.time()
    gen.generate(output_tokens, prompt_len, ntok-1)   # ← 只计 decode
    cpu_end = time.time()
    end_event.record()                     # GPU 时间线：终点
    synchronize()                          # 等 GPU 做完
    times.append(start_event.elapsed_time(end_event) / 1000)  # ms→s
    cpu_times.append(cpu_end - cpu_start)

elapsed = mean(times[num_warmup:])         # 丢掉预热，取平均
fwd_per_second = (ntok - 1) / elapsed
tokens_per_second = batch_size * fwd_per_second
```

注意 `(ntok - 1)`：decode 只生成 `ntok-1` 个 token（首 token 来自 prefill），所以每秒前向数 = `(ntok-1)/elapsed`；每个前向产出 `batch_size` 个 token（batch 里每条各一个），故 `tokens/sec = batch_size * fwd_per_second`。

`llama_repl.py` 的计时更紧凑——直接包住 `generate_with_eos`：

```
start_event.record()
until_eos, num_generated = gen.generate_with_eos(...)
end_event.record()
synchronize()
elapsed = start_event.elapsed_time(end_event) / 1000
tokens_per_second = num_generated / elapsed
```

这里 `num_generated` 是 `generate_with_eos` 返回的"实际生成 token 数"（命中 EOS 提前停时小于上限），所以 `tokens/sec` 反映这一轮的真实产出。

#### 4.3.3 源码精读

**generate.py 的计时循环**——CUDA event + CPU 计时 + 预热剔除：

[megakernels/scripts/generate.py:167-184](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L167-L184) —— 第 169 行循环 `range(config.num_warmup + config.num_iters)`（默认预热 5 次、计时 10 次）；第 170–171 行建两个 `enable_timing=True` 的 event；第 174 行 `gen.generate(...)` 是被计时的 decode（注意 prefill 的 `model(prefill_inp)` 在第 130 行、**事件之外**）；第 177 行 `synchronize()` 后第 178 行读 `elapsed_time(...)` 并除以 1000 转秒；第 181 行 `times[num_warmup:]` 丢掉预热。

**generate.py 的吞吐计算**：

[megakernels/scripts/generate.py:204-207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L204-L207) —— `fwd_per_second = (config.ntok - 1) / elapsed`（decode 前向数除以平均 GPU 秒数），`tokens_per_second = config.batch_size * fwd_per_second`。第 185 行还会打印 `Average time` 的 GPU 与 CPU 两套毫秒数，便于对比"GPU 实际算的时间"与"CPU 侧墙钟时间"。

**llama_repl.py 的计时与吞吐**：

[megakernels/scripts/llama_repl.py:81-82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L81-L82) —— 在闭包外预先建好两个 event（每轮复用）。

[megakernels/scripts/llama_repl.py:109-122](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L109-L122) —— 第 109 行 `start_event.record()` 紧贴在 decode 之前（prefill 的 `model(...)` 在第 103 行、事件之外）；第 110–116 行 `generate_with_eos(...)`；第 117 行 `end_event.record()`；第 119–120 行同步并读耗时；第 122 行 `tokens_per_second = num_generated / elapsed`。

**llama_repl.py 的预热**——一次丢弃的生成：

[megakernels/scripts/llama_repl.py:131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L131) —— `generate([{"role": "user", "content": "hi"}])` 在进入交互循环前跑一轮，触发首次分配/autotuning；返回值被丢弃。这样用户输入的第一句话就不会承担冷启动开销。

#### 4.3.4 代码实践

**目标**：体会"预热"对计时的影响，并验证 GPU 与 CPU 计时的差异。

1. 用 `mk` 模式（需先按 [U1·L3](u1-l3-build-and-run-llama-demo.md) 编译 `mk_llama`）跑基准，注意观察末尾三行：

   ```bash
   python megakernels/scripts/generate.py mode=mk \
       prompt="tell me a funny joke about cookies" ntok=100
   ```

2. 观察 `Average time: ...ms (CPU: ...ms)`：通常 CPU 时间会**略大于** GPU 时间（因为 CPU 时间还含 Python 循环、事件记录、同步等开销）。若两者差很大，说明 Python 侧开销不可忽略。
3. 把 `num_warmup` 设为 0 对比（pydra 传 `num_warmup=0 num_iters=10`），看 `Average time` 是否变高——印证预热的作用。
4. 试着增大 `ntok`（如 `ntok=200`）再跑，观察 `Tokens per second` 是否基本稳定（decode 每步代价近似恒定，故吞吐应随步数稳定，而不是随步数下降）。

**预期结果**：
- 关掉预热后首轮计时偏高；
- `Tokens per second` 在步数变化时大致稳定（这正是 decode 吞吐的标志）。

> 若本地无 GPU/权重：改为**源码阅读型实践**——对照 4.3.3，解释为什么必须先 `synchronize()` 再读 `elapsed_time()`（因为 event 打在 GPU 时间线上，CPU 必须等 GPU 执行到 end_event 才能读到有效的间隔）。

#### 4.3.5 小练习与答案

**练习 1**：`generate.py` 的 `fwd_per_second` 为什么是 `(ntok - 1) / elapsed` 而不是 `ntok / elapsed`？
**参考答案**：因为首 token 由 prefill 给出（`output_tokens[:, 0]`），decode 循环只生成剩下的 `ntok-1` 个 token（`gen.generate` 传入的是 `config.ntok - 1`）。所以一次完整基准里"decode 前向"的次数是 `ntok-1`，除以平均耗时才是每秒前向数。

**练习 2**：如果忘记写 `torch.cuda.synchronize()`，`start_event.elapsed_time(end_event)` 会怎样？
**参考答案**：可能读到不完整甚至无意义的间隔。因为 `end_event.record()` 只是把"打戳"这个动作排进 GPU 队列，CPU 立刻继续执行；若不同步，CPU 可能在 GPU 真正执行到 end_event 之前就去问"两个戳隔了多久"，此时 end_event 还没发生，`elapsed_time` 的结果不可靠。`synchronize()` 确保 GPU 把（包括 end_event 在内的）所有工作做完后再读取。

---

## 5. 综合实践：改 prompt 与 ntok，标出 prefill/decode 分界线

**任务**：按规格要求，修改 `generate.py` 的 `prompt` 与 `ntok` 运行；在源码里标注 prefill 与 decode 的分界线；并解释为何 decode 每步只生成一个 token。把三件事串起来完成。

**操作步骤**：

1. **改参数运行**（无需改源码，pydra 用 `key=value` 传参）。先用最容易跑通的 `torch` 模式：

   ```bash
   # 仓库根目录；ntok 用小值便于人工核对
   python megakernels/scripts/generate.py mode=torch \
       prompt="Write a one-sentence greeting." ntok=16
   ```

   抄下打印的 `Input ids shape`（其中第二维即 `prompt_len`）、`Output ids` 与 `Output text`。

2. **在源码标注分界线**。打开 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py)，按下面在脑中（或临时注释里）画线：

   - **prefill**：第 130 行 `prefill_output = model(prefill_inp)`（吃下整段 prompt）→ 第 132 行取 `[:, -1:]` → 第 137 行写入 `output_tokens[:, 0]`。**这一段在计时循环之外。**
   - **中间**：第 139–165 行构建调度 + `match config.mode` 选生成器（与 prefill/decode 的"计算"无关，是准备）。
   - **decode**：第 174 行 `gen.generate(output_tokens, prompt_len, config.ntok - 1)`。**这一行在计时循环内（第 169 行起）。**

   同理在 [llama_repl.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py) 标注：prefill = 第 103 行，decode = 第 110 行 `generate_with_eos(...)`，两者被第 109/117 行的 `record()` 夹住。

3. **解释"decode 每步只生成一个 token"**。结合 [generators.py:159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L159)（每步切片长度为 1）与 [generators.py:87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L87)（`seq_len` 递增），写下你的解释，要点应包含：
   - 串行依赖：下一个 token 取决于上一个刚采样的 token，无法并行多步；
   - KV 缓存：历史 token 的 K/V 已缓存，每步只需处理 1 个新 token，故输入长度恒为 1。

4. **（可选，进阶）** 改成 `mode=mk` 重跑同一 prompt/ntok（前提是已编译 `mk_llama`），对比 `Tokens per second`：`mk` 应显著高于 `torch`，因为前者把每步的整层计算融合成一个 megakernel。

**需要观察的现象 / 预期结果**：

| 观察 | 预期 |
| --- | --- |
| `Input ids shape` 第二维 | 等于 prompt 的 token 数（与 prompt 长度相关） |
| `Output ids` 第 0 个 | prefill 给出的首 token（语义合理） |
| 分界线标注 | prefill 在计时循环**外**，decode 在**内** |
| `Tokens per second`（torch vs mk） | mk 明显更高（若已编译） |
| `Average time` 的 GPU vs CPU | CPU ≥ GPU，差额为 Python 侧开销 |

> 若本地无 GPU/权重：改为**源码阅读型综合实践**——画出 `output_tokens` 从 prefill（generate.py:130-137）到 decode（generate.py:174 → generators.py:145-163）的填充链路，在每个位置标出"这时输出张量的哪几格已被填好"，从而直观看到 prefill 填第 0 格、decode 逐格填第 1..ntok-1 格。

## 6. 本讲小结

- 两个脚本都遵循 **prefill → decode** 两阶段：prefill 用 `model(prefill_inp)` 一次吃下整段 prompt、取 `output_ids[:, -1:]` 得到首 token（[generate.py:130-132](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L130-L132)、[llama_repl.py:103-105](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L103-L105)）。
- lm head 对**每个位置**都产出一个预测（[llama.py:394-396](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L394-L396)），所以 prefill 才能一次性给出"prompt 之后的首 token"，这是把多步并行为一次前向的根源。
- **decode 每步只生成一个 token**：串行依赖（下一 token 依赖上一采样结果）+ KV 缓存（历史不必重算），每步输入长度恒为 1、`seq_len` 递增（[generators.py:159](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L159)、[generators.py:87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L87)）。
- 两个脚本在"decode 如何结束"上分流：基准脚本 `generate.py` 用固定步数的 `generate`（计时要稳定），交互脚本 `llama_repl.py` 用带 EOS 的 `generate_with_eos`（聊天自然停，`eos_token_check_interval=16`）。
- 计时用 **CUDA Event 四件套**（`enable_timing`/`record`/`synchronize`/`elapsed_time`）只包住 decode，prefill 在事件之外；`generate.py` 有 `num_warmup` 次预热，`llama_repl.py` 用一次丢弃的生成预热。
- 两套吞吐公式：基准 `tokens/sec = batch_size * (ntok-1)/elapsed`，交互 `tokens/sec = num_generated/elapsed`。

## 7. 下一步学习建议

- **下一讲建议**：进入 **KV cache 与 `setup_caches`**——读 [llama.py:552 起](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L552) 的 `setup_caches`，弄清 decode 每步"输入只有 1 个 token、却能 attend 到全部历史"背后的 K/V cache 张量形状与写入逻辑。这正好补上本讲 4.2 反复提到的"历史由缓存代劳"的细节。
- **回到 U2·L1 深化执行器**：如果还没细看，回头读 [U2·L1 的 4.3/4.4 节](u2-l1-three-execution-modes.md)，把 `pyvm`/`mk` 的 `run` 与本讲的 `generate` 循环对上——你会看到"decode 循环每步调一次 `run`"全貌。
- **chat 模板与多轮上下文**：`llama_repl.py` 把整个 `messages` 列表每轮重新 `apply_chat_template` 再 prefill（见 [llama_repl.py:84-91](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L84-L91)），即每轮都重做 prefill。可以思考：为什么交互脚本没有做"增量 prefill / 复用上一轮 KV"的优化？这是理解真实部署中 prefix caching 的起点。
