# 生成主循环：非 MTP 的逐 token 解码

## 1. 本讲目标

本讲精读 TileRT 在**不开 MTP**时如何一段一段地把 prompt 喂进模型、再一个一个地把 token 吐出来。读完本讲你应该能够：

- 说清 `DSAv32Generator._generate_without_mtp` 的完整循环结构：tokenize、缓冲填充、逐 token forward、位置推进、EOS 终止、收尾复位。
- 解释为什么 TileRT 在非 MTP 模式下**不做 chunked prefill**，而是把 prompt 也拆成「一次一个 token」的解码步。
- 看懂 `decode_layer.forward` 如何把一个 token 交给 C++ 后端算子 `dsa_show_hands`，并从 8 卡结果里取出下一个 token。
- 理解 `prompt_mask` 如何用一行 `torch.where` 同时实现「prompt 区强制 teacher forcing、生成区写采样结果」两种行为。

本讲只讲非 MTP 路径。MTP（投机解码）会一次 forward 预测多个 token、还要统计接受长度，复杂度高得多，留到 [u3-l3](u3-l3-mtp-speculative-decoding.md) 专讲。

## 2. 前置知识

本讲建立在两讲之上，如果你还没读，建议先看：

- **[u1-l5 程序化 API 与 Generator 生命周期](u1-l5-generator-api-and-lifecycle.md)**：`generate` 返回四元组 `(text, time_list, accepted_counts, prompt_len)`，非 MTP 时 `accepted_counts` 恒为空列表；`enable_thinking` 是 chat template 的开关而非采样参数。
- **[u2-l3 ShowHandsDSALayer](u2-l3-show-hands-dsa-layer.md)** 与 **[u2-l5 三层张量执行契约](u2-l5-three-layer-tensor-contract.md)**：`decode_layer` 就是 `ShowHandsDSALayer`；它维护着交给 C++ 后端的「四元张量列表契约」——`intermediates`(temp_vars)、`caches`、`params`、`profile_logs`。其中 `caches` 里的 KI/KV/PE 缓存会**跨 token 累积**，这是理解「为什么 forward 只传一个 token 却能记住整段历史」的关键。

几个术语先对齐：

- **prefill / decode**：传统推理把处理 prompt 的阶段叫 prefill（并行吃一大段），把生成新 token 的阶段叫 decode（一次一个）。TileRT 在非 MTP 模式下**把这两个阶段统一成 decode**，每步只吃一个 token，靠 KV 缓存记住历史——这是为了把单 token 延迟（TPOT）压到毫秒级。
- **teacher forcing**：训练或预填充时，下一步的输入不取模型自己的预测，而取「标准答案」。本讲的 `prompt_mask` 正是干这件事。
- **EOS（End Of Sequence）**：tokenizer 的句子结束符 id，`self.eos_id`。生成到 EOS 就该停。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tilert/models/deepseek_v3_2/generator.py` | 生成器主类，含两个生成路径 | `_generate_without_mtp` 的整个循环，以及 `generate` 如何分发到它 |
| `tilert/models/deepseek_v3_2/modules/end2end.py` | `ShowHandsDSALayer` 执行器 | `forward`、`reset_sequence`、`set_sampling_seed` 三个被循环调用的方法，以及底层算子 `dsa_show_hands` |
| `tilert/models/deepseek_v3_2/temp_var_indices.py` | temp_vars 的命名下标 | `Idx.TOKEN_OUT`、`Idx.CUR_POS`、`Idx.TOKEN_ID` 三个被循环读写的槽位 |

一句话总览：**`generator.py` 负责「喂什么、取什么、何时停」，`end2end.py` 负责「把一个 token 送进 C++ 后端跑一遍」**。

## 4. 核心概念与源码讲解

### 4.1 tokenize 与缓冲填充

#### 4.1.1 概念说明

用户传进来的是一段自然语言 `prompt`，模型只认 token id。所以第一步是把字符串变成 id 列表。TileRT 用 HuggingFace 的 `apply_chat_template`，它会把对话渲染成模型训练时用的格式（加上 `<｜begin▁of▁sentence｜>`、`<｜User｜>`、`<｜Assistant｜>` 等特殊符），最后以 `add_generation_prompt=True` 收尾，留出「该模型回答了」的起手位置。

拿到 id 列表后，TileRT **不开多个长度不一的循环**，而是预分配一块定长缓冲 `tokens`，把 prompt 放在开头，其余位置填一个「非法哨兵值」`-1`。这块哨兵值后面有双重用途：构造 `prompt_mask`、在收尾时被裁掉。

为什么定长？因为后端的 KV 缓存、位置编码（RoPE）的 `freqs_cis` 都在 `from_pretrained` 时按 `max_seq_len` 预分配好了（见 u2-l3）。Python 侧也按同一个上界开缓冲，循环只需要一个 `for` 跑到底。

#### 4.1.2 核心流程

```text
prompt(str)
   │ apply_chat_template(add_generation_prompt=True, thinking=...)
   ▼
prompt_tokens(list[int])          # chat 模板渲染后的 token id
   │
   │ total_len = min(max_seq_len, max_new_tokens + prompt_len)
   ▼
tokens: [prompt_id_0, ..., prompt_id_{p-1}, -1, -1, ..., -1]   # 长度 = total_len
   │
   ▼
prompt_mask = tokens != -1        # True 的位置属于 prompt，False 的位置待生成
```

总长度由两个上界取小：

\[
\text{total\_len} = \min(\text{max\_seq\_len},\ \text{max\_new\_tokens} + \text{prompt\_len})
\]

`max_seq_len` 来自 `ModelArgs`（模型能处理的最大序列长度），`max_new_tokens` 来自生成器构造参数。两者取小，保证既不超模型容量、也不超用户要的生成长度。

#### 4.1.3 源码精读

tokenize 在 `_generate_without_mtp` 开头，允许调用方传 `prompt_tokens` 跳过（基准测试为了精确控制长度会用到）：

[tilert/models/deepseek_v3_2/generator.py:195-200](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L195-L200) —— 当未提供现成 token 时，用 `apply_chat_template` 渲染对话；注意 `thinking=self.enable_thinking` 把「是否思考」透传给模板。

接下来算总长、开缓冲、造掩码，是本模块最关键的三行：

[tilert/models/deepseek_v3_2/generator.py:206-212](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L206-L212) —— `torch.full(..., -1)` 先把整块缓冲填成哨兵 `-1`；再把 prompt 拷进开头；最后 `tokens != -1` 得到布尔掩码。`prompt_mask` 形状与 `tokens` 相同，`True` 标出「这是已知的 prompt 位置」。

#### 4.1.4 代码实践

这是源码阅读型实践，不需要 GPU：

1. **实践目标**：亲手算出一段短 prompt 的 `total_len`、`tokens` 缓冲内容和 `prompt_mask`，验证你对哨兵值的理解。
2. **操作步骤**：
   - 在本地用一个 HuggingFace tokenizer（任意 chat 模型即可）对 `"你好"` 跑 `apply_chat_template(..., add_generation_prompt=True)`，记下 `prompt_len`。
   - 假设 `max_new_tokens=100`、`max_seq_len=4096`，按上面公式手算 `total_len`。
   - 在纸上画出长度为 `total_len` 的 `tokens` 数组：前 `prompt_len` 格填 prompt id，其余填 `-1`；再画出对应的 `prompt_mask`（前 `prompt_len` 格 `True`，其余 `False`）。
3. **需要观察的现象**：`total_len` 应等于 `prompt_len + 100`（因为远小于 `max_seq_len`）；`prompt_mask` 恰好在 `prompt_len` 处由 `True` 切到 `False`。
4. **预期结果**：缓冲里 `-1` 的个数 = `max_new_tokens`。如果 `prompt_len + max_new_tokens > max_seq_len`，则会被 `max_seq_len` 截断，此时 `-1` 个数变少——这正是取小公式防止越界的作用。
5. 真正在 B200 上跑这段需要 8 卡环境，本地无 GPU 时上述手算即为「待本地验证」的可验证结论。

#### 4.1.5 小练习与答案

**练习 1**：如果用户把 `max_new_tokens` 设得特别大（比如 100 万），`total_len` 会变成 100 万吗？
**答**：不会。`total_len = min(max_seq_len, max_new_tokens + prompt_len)`，会被 `max_seq_len` 封顶，因为后端 KV 缓存和 RoPE 表只预分配了这么大。

**练习 2**：为什么哨兵值选 `-1` 而不是 `0`？
**答**：因为 `0` 是合法的 token id（通常是 `<pad>` 或某个真实词），用它当哨兵会和真 token 混淆；`-1` 不是任何 tokenizer 的合法 id，`tokens != -1` 能干净地区分「真 token」与「待填位置」。

---

### 4.2 逐 token forward 与位置推进

#### 4.2.1 概念说明

这是整篇讲义的核心。传统 LLM 推理有两个截然不同的阶段：prefill 一次吃一整段 prompt，decode 一次吃一个 token。TileRT 在非 MTP 模式下**只用一条路**：循环里每步只喂一个 token 给 `decode_layer.forward`，后端凭 KV 缓存记住之前所有 token。

于是 prompt 也被「逐 token 嚼碎」——这看起来浪费，但对 TPOT 优化是值得的：每步的计算量恒定（单 token 的矩阵向量乘），便于 CUDA Graph 捕获成固定形状的图、做到极致低延迟（见 u2-l3 的 `prepare_money` 图捕获）。

每一步要做四件事：

1. 把**上一步位置** `prev_pos` 上的 token 喂给后端；
2. 从后端结果里取出**当前步位置** `cur_pos_val` 的预测 token；
3. 用 `prompt_mask` 决定「信模型还是信标准答案」；
4. 把 `prev_pos` 推进到 `cur_pos_val`，进入下一轮。

#### 4.2.2 核心流程

位置关系始终满足：

\[
\text{cur\_pos\_val} = \text{prev\_pos} + 1
\]

因为循环末尾 `prev_pos = cur_pos_val`，而 `cur_pos_val` 从 1 开始、每次加 1。`prev_pos` 初始为 0。所以「喂进 `tokens[0, prev_pos]`，预测 `cur_pos_val` 位置」在数学上就是「已知第 0..prev_pos 个 token，预测第 prev_pos+1 个」。

```text
prev_pos = 0
for cur_pos_val in 1 .. total_len-1:
    # 1) 喂上一步的 token，后端用 KV 缓存算出下一步预测
    results = decode_layer.forward(tokens[0, prev_pos])
    next_token = results[device0].intermediates[TOKEN_OUT]

    # 2) prompt 区保留原值(teacher forcing)，生成区采用预测
    next_token = where(prompt_mask[cur_pos_val], tokens[cur_pos_val], next_token)
    tokens[cur_pos_val] = next_token

    # 3) 推进位置
    prev_pos = cur_pos_val
```

`decode_layer.forward` 内部把 token 搬到 CPU 后调底层算子：

[tilert/models/deepseek_v3_2/modules/end2end.py:551-558](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L551-L558) —— `dsa_show_hands(token_id.cpu(), ...)` 是真正驱动 C++ 后端跑一遍 transformer 的入口；它不返回结果，而是把输出写进各卡的 `intermediates` 张量（in-place），`forward` 再把 8 张卡的「四元组」原样返回。

底层算子本身只是按 `with_mtp`/`is_glm5` 拼出名字后 `getattr`：

[tilert/models/deepseek_v3_2/modules/end2end.py:99-104](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L99-L104) —— 非 MTP、非 glm5 时实际调用的是 `torch.ops.tilert.dsa_show_hands(token_id)`。

#### 4.2.3 源码精读

循环主体在这里，是本讲最该逐行读懂的片段：

[tilert/models/deepseek_v3_2/generator.py:220-236](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L220-L236) —— 逐行拆解：

- `start_time`/`end_time` 夹住 `forward`，把每步耗时记进 `time_list`（后面算平均单 token 延迟用）。
- `decode_layer.forward(tokens[0, prev_pos], with_mtp=with_mtp)`：只传**一个标量 token**（`tokens[0, prev_pos]`），不是整段。
- `intermediates, *_ = multi_devices_results[0]`：只取 device 0 的结果，忽略其余 7 卡（它们的 `TOKEN_OUT` 也会被同步写，但解码取 token 只需 0 卡）。
- `next_token = intermediates[Idx.TOKEN_OUT][0][0]`：`Idx.TOKEN_OUT` 是 temp_vars 的第 25 号槽（见 [temp_var_indices.py:43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L43)），`[0][0]` 取出 batch=0、位置=0 的那个 token id。
- `torch.where(prompt_mask[0, cur_pos_val], tokens[0, cur_pos_val], next_token)`：**本讲最精妙的一行**。掩码为 `True`（prompt 区）时保留原 prompt token（teacher forcing），为 `False`（生成区）时采用模型预测。这行同时处理了 prompt 填充和生成两种语义。
- `tokens[0, cur_pos_val] = next_token`：把决定好的 token 写回缓冲，供下一轮 `forward` 当输入。
- `prev_pos = cur_pos_val`：推进位置，保证下轮 `forward` 喂的是本轮刚写入的 token。

#### 4.2.4 代码实践

1. **实践目标**：手工模拟一次 3 步循环，验证「prompt 区被强制覆盖、生成区采预测」的行为。
2. **操作步骤**：假设 `prompt_tokens = [10, 20, 30]`（`prompt_len=3`），`max_new_tokens` 足够大，`max_seq_len` 足够大。
   - 第 1 轮：`cur_pos_val=1, prev_pos=0`。喂 `tokens[0]=10`，模型预测 `next_token=999`（假设）。`prompt_mask[0,1]=True`，于是 `next_token = where(True, tokens[1], 999) = 20`，写回 `tokens[1]=20`。
   - 第 2 轮：`cur_pos_val=2, prev_pos=1`。喂 `tokens[1]=20`，预测 `888`。掩码 `True` → 写回 `tokens[2]=30`。
   - 第 3 轮：`cur_pos_val=3, prev_pos=2`。喂 `tokens[2]=30`，预测 `777`。`prompt_mask[0,3]=False`（这是生成区）→ 写回 `tokens[3]=777`。
3. **需要观察的现象**：前两轮模型预测（999、888）被**丢弃**，prompt token（20、30）原样保留；第 3 轮才开始真正采用模型输出（777）。
4. **预期结果**：循环结束后 `tokens = [10, 20, 30, 777, ...]`，前 `prompt_len` 格始终是原 prompt。这也解释了为什么 prompt 阶段的 forward「白跑」却不影响正确性——它的预测被 teacher forcing 覆盖，但它的**副作用（更新 KV 缓存）是必需的**。
5. 真 GPU 运行结果：待本地验证（需 8× B200）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prompt 阶段明明丢弃了预测，却还必须跑 `forward`？
**答**：因为 `forward` 有两个产物——一是 `TOKEN_OUT` 里的预测（被丢弃），二是**更新各层的 KI/KV/PE 缓存**（被保留）。后续生成步要靠这些缓存「记住」prompt 内容，所以每步的 KV 缓存写入是必须执行的副作用。

**练习 2**：循环里只用了 `multi_devices_results[0]`（device 0）取 token，那 device 1..7 的 forward 是不是白跑了？
**答**：没有白跑。8 卡做的是**张量并行**——每卡只算模型的一部分（注意力头、专家等），device 0 的 `TOKEN_OUT` 是 8 卡 allreduce 汇聚后的最终结果。其余卡的 `intermediates` 也被更新了，只是取最终 token 时读 0 卡即可。此外 0 卡还承担 NSA 稀疏选择并广播给其余卡（见 [u2-l6](u2-l6-mla-and-sparse-select.md)）。

---

### 4.3 EOS 终止与 reset_sequence

#### 4.3.1 概念说明

循环不能无脑跑到 `total_len`。两种提前结束的情况：

- **生成到 EOS**：模型主动说「我说完了」，此时应立刻停。
- **跑满 `total_len`**：达到长度上限。

注意一个细节：prompt 区**不会**因为恰好含 EOS 而停——`finished` 只在「生成区出现 EOS」时置位。这避免了一个边界 bug：如果用户 prompt 里恰好含 EOS 字符，不应误判结束。

循环结束后还有两件收尾工作：

1. `reset_sequence()`：清空 KV 缓存，让生成器可以**复用**处理下一个 prompt，而不必重新加载权重（这是 u1-l5 讲过的「`generate` 可多次调用」的前提）。
2. 从 `tokens` 里切出真正的补全 token、解码成文本返回。

#### 4.3.2 核心流程

```text
每轮循环末尾:
    finished |= (~prompt_mask[cur_pos_val]) AND (next_token == eos_id)
    if finished.all(): break        # batch_size=1, all() 即单元素

循环结束后:
    reset_sequence()                 # 清 KV 缓存, 准备下一个请求
    completion = tokens[prompt_len : prompt_len + max_new_tokens]
    if eos_id in completion:
        completion = completion[:eos_index]   # 截到 EOS 为止
    text = batch_decode(completion)
    return text, time_list, prompt_len
```

`finished` 是形状 `[batch_size]` 的布尔张量（这里 `batch_size=1`），用 `|=` 累积，一旦某步在生成区采样到 EOS 就永久置位，下一轮 `finished.all()` 为真即 `break`。

#### 4.3.3 源码精读

EOS 判定与提前退出：

[tilert/models/deepseek_v3_2/generator.py:235-245](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L235-L245) —— `torch.logical_and(~prompt_mask[0, cur_pos_val], next_token == self.eos_id)` 同时要求「在生成区」且「token 是 EOS」两个条件，二者皆真才把 `finished` 置位。`finished.all()` 在 `batch_size=1` 时等价于单个布尔。

注意第 237-242 行还有一段：仅当 `cur_pos_val >= prompt_len`（生成区）才把 token decode 并 `print` 出来——所以你看到的流式输出是从第一个生成 token 开始的，prompt 不打印。

循环结束后收尾：

[tilert/models/deepseek_v3_2/generator.py:254-265](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L254-L265) —— `reset_sequence()` 清缓存；随后从 `tokens` 切 `[prompt_len : prompt_len + max_new_tokens]`，若含 `eos_id` 则截到它为止；最后 `batch_decode` 成文本。返回三元组 `(text, time_list, prompt_len)`，再由上层 `generate` 包成四元组（补上空的 `accepted_counts`）。

`reset_sequence` 的实现按是否 MTP 分两路，非 MTP 只调一次 reset 算子：

[tilert/models/deepseek_v3_2/modules/end2end.py:572-577](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L572-L577) —— 它最终调到 `torch.ops.tilert.dsa_show_hands_reset()`，由 C++ 后端把各层 KV 缓存指针归零/重置，使下一次 `forward` 从空缓存开始。

最后别忘了循环开始前还有一步「设采样种子」，它决定 top-p 采样的随机性：

[tilert/models/deepseek_v3_2/modules/end2end.py:560-570](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L560-L570) —— `set_sampling_seed` 在 `generate` 入口被调用（[generator.py:179](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L179)），种子固定整个请求，每步的位置变化提供随机性。

#### 4.3.4 代码实践

1. **实践目标**：理解 EOS 提前终止和 `reset_sequence` 的复用语义。
2. **操作步骤**：
   - 阅读 [generator.py:235](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L235)，回答：如果 prompt 第 5 个 token 恰好等于 `eos_id`，`finished` 会在第 5 步置位吗？
   - 假设生成在第 8 步采到 EOS 并 break，`time_list` 里有几个元素？`tokens` 里 `[prompt_len : prompt_len+max_new_tokens]` 这段中，EOS 之后的位置存的是什么？
   - 对照 [end2end.py:572-577](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L572-L577)，解释：为什么连续两次调用 `generate` 不需要重新 `from_pretrained`？
3. **需要观察的现象**：`time_list` 长度 = 实际跑的 forward 步数（含提前 break）；EOS 之后的 token 槽位仍是哨兵 `-1` 或被截掉。
4. **预期结果**：
   - prompt 里的 EOS **不会**触发终止（因为 `~prompt_mask` 为 False）。
   - 第 8 步 break 时 `time_list` 有 8 个元素。
   - `reset_sequence` 只清 KV 缓存，不动 `params`（权重），所以权重一次加载、多次 `generate` 复用。
5. 真 GPU 运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：循环 break 后，`tokens[0, prompt_len+8:]`（EOS 之后的位置）里是什么？
**答**：还是初始的哨兵 `-1`，因为这些位置从未被循环写过。收尾的切片 `[prompt_len : prompt_len + max_new_tokens]` 会把它们包含进来，但随后 `if self.eos_id in toks: toks = toks[: toks.index(eos_id)]` 把 EOS 及之后的全部截掉，所以最终解码不含它们。

**练习 2**：为什么 `finished` 用 `torch.tensor` 而不是普通 Python `bool`？
**答**：为了支持 `batch_size > 1` 的扩展。当前 `batch_size` 硬编码为 1，但用形状 `[batch_size]` 的张量 + `finished.all()` / `|=` 写法，将来扩到多 batch 时这段逻辑不用改。这是「为未来留口子」的工程习惯。

---

## 5. 综合实践

把三个模块串起来，完成一次完整的「源码追踪 + 伪代码重写」：

**任务**：阅读 [generator.py:187-265](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L187-L265) 的 `_generate_without_mtp` 全文，然后用**不超过 20 行伪代码**重写它的最少必要逻辑，要求：

1. 体现 tokenize → 开缓冲 → 循环 → EOS → reset → 返回 的完整骨架。
2. 在循环里明确标出 `token_id`（喂给 forward 的）和 `next_token`（从 forward 取出的）分别从哪来、写到哪。
3. 标注 `prompt_mask` 在哪一行决定了 teacher forcing 与采样的分流。

参考答案（伪代码）：

```python
def generate_without_mtp(prompt, max_new_tokens):
    # 1. tokenize 与缓冲填充
    prompt_tokens = tokenizer.apply_chat_template(prompt, add_generation_prompt=True)
    prompt_len = len(prompt_tokens)
    total_len = min(max_seq_len, max_new_tokens + prompt_len)
    tokens = [-1] * total_len
    tokens[:prompt_len] = prompt_tokens
    prompt_mask = [t != -1 for t in tokens]

    # 2. 逐 token forward 与位置推进
    prev_pos = 0
    finished = False
    for cur_pos in range(1, total_len):
        token_id = tokens[prev_pos]                       # 喂上一步的 token
        result = decode_layer.forward(token_id)           # 后端用 KV 缓存算
        next_token = result[0].temp_vars[TOKEN_OUT]       # 取当前步预测

        # 3. prompt 区 teacher forcing, 生成区采样
        if prompt_mask[cur_pos]:
            next_token = tokens[cur_pos]
        tokens[cur_pos] = next_token

        # EOS 终止 (仅生成区)
        if not prompt_mask[cur_pos] and next_token == eos_id:
            break
        prev_pos = cur_pos

    # 4. 收尾
    decode_layer.reset_sequence()                          # 清 KV 缓存, 可复用
    completion = tokens[prompt_len:prompt_len + max_new_tokens]
    if eos_id in completion:
        completion = completion[:completion.index(eos_id)]
    return tokenizer.decode(completion), prompt_len
```

**进阶思考**（不必写代码）：如果让你把这段循环改成「真 prefill（一次吃整段 prompt）+ decode」两阶段，需要改动哪几处？提示——`forward` 现在每次只接受一个标量 token，要支持整段需要后端 `dsa_show_hands` 也支持批量输入，这正是 MTP 模式（`mtp_seq_len=4`）部分实现的思路，详见下一讲。

## 6. 本讲小结

- TileRT 非 MTP 模式**不做 chunked prefill**，而是把 prompt 和生成都统一成「每步喂一个 token」的解码循环，靠 KV 缓存记住历史——这是为极致 TPOT 服务的。
- `tokens` 缓冲用 `-1` 当哨兵填充 prompt 之后的区域，`prompt_mask = tokens != -1` 一行区分 prompt 区与生成区。
- 循环里 `forward(tokens[0, prev_pos])` 只传**一个** token，预测写入 `tokens[0, cur_pos_val]`，位置始终满足 `cur_pos_val = prev_pos + 1`。
- 最关键的一行 `torch.where(prompt_mask[...], 原值, 预测)` 同时实现了 prompt 区 teacher forcing 与生成区采样两种语义。
- EOS 终止只在**生成区**生效（`~prompt_mask AND == eos_id`），避免 prompt 内含 EOS 误触发；提前 break 后 `time_list` 长度等于实际 forward 步数。
- 收尾的 `reset_sequence()` 只清 KV 缓存不动权重，这是「权重加载一次、`generate` 多次复用」的实现基础。

## 7. 下一步学习建议

- 下一篇 **[u3-l3 MTP 多 token 预测与投机解码](u3-l3-mtp-speculative-decoding.md)**：看 TileRT 如何把每步「预测 1 个 token」升级为「预测 `mtp_seq_len=4` 个 draft token、再统计接受长度」，理解 `num_accepted`、`predicted_tokens`、`cur_pos += num_accepted` 这些非 MTP 路径里没有的概念。
- 想深入了解采样参数如何固化进 CUDA Graph、改参为何要 `go_home` 重新 `prepare_money`，看 **[u3-l4 采样、CUDA Graph 重捕获与 logprobs 导出](u3-l4-sampling-and-cuda-graph.md)**。
- 想看这套生成循环在基准测试里被怎么驱动、`time_list` 如何汇总成 tok/s，看 **[u3-l5 性能基准测试套件](u3-l5-benchmark-suite.md)**。
- 建议同时对照阅读 [generator.py 的 `generate` 入口（L154-L185）](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L154-L185)，看清 MTP 与非 MTP 两条路是如何在入口处分发的。
