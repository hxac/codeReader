# MTP 多 token 预测与投机解码

## 1. 本讲目标

上一讲（u3-l2）我们读了非投机解码路径 `_generate_without_mtp`：每一步只喂一个 token、只产一个 token，靠 KI/KV/PE 缓存记住历史。这是一种「一个 forward 换一个 token」的严格自回归。

本讲进入 TileRT 的另一条解码路径——**MTP（Multi-Token Prediction，多 token 预测）投机解码**。学完本讲，你应该能够：

1. 说清投机解码为什么能在一个 batch 下把单 token 延迟（TPOT）进一步压低，以及 MTP 与传统 draft-model 投机解码的区别。
2. 读懂 `_generate_with_mtp` 的 **prefill / decode 双阶段**循环：prompt 如何被切成 `mtp_seq_len=4` 的块喂入、padding 与控制算子如何配合、decode 阶段如何用「上一轮的 draft」拼出下一轮输入。
3. 说清 `num_accepted`、`predicted_tokens`、`cur_pos += num_accepted` 三者如何决定 token 写回位置，并能用 README 给出的 `mean=2.77` 算出「一次 forward 实际产出多少 token」。
4. 读懂 MTP 模块（`MTP` 容器）如何**复用主模型算子**（`MTPPreprocessLayer` + `MoeBlock` + `RMSNormHeadProj`）拼出一个「投机头」，以及它为何挂在 `layer_61_` 前缀下。

> 阅读前提：你已经学过 u3-l2（非 MTP 解码主循环）、u2-l4（`Dsa` 层组装与 `register_op`）、u2-l5（params/temp_vars/caches 四元组与 `Idx` 枚举）。本讲大量沿用这些前置概念。

## 2. 前置知识

### 2.1 自回归解码的瓶颈

普通自回归（非 MTP）生成里，每产一个 token 都要完整跑一遍 61 层 transformer。设单次 forward 耗时为 \( t \)，则生成 \( N \) 个 token 需要 \( N \cdot t \) 时间，每 token 平均延迟（TPOT）就等于 \( t \)。这就是 u3-l2 的模型。

能不能让一次 forward 产出不止一个 token？这是投机解码要解决的问题。

### 2.2 投机解码的两种形态

投机解码（speculative decoding）的核心思路是：**用一个便宜的方式先「猜」出几个候选 token，再用主模型一次性验证它们**。验证比从头生成便宜，因为可以把多个 token 并行处理。

主流有两种「猜」的方式：

| 形态 | 草稿来源 | 典型代表 |
|------|---------|---------|
| draft-model | 一个独立的小模型 | Medusa / n-gram / 小 LLM |
| **MTP（多 token 预测）** | **主模型自身的额外 MTP 头** | **DeepSeek-V3、TileRT** |

TileRT 用的是 MTP：它不引入额外的小模型，而是给主模型接一个「投机头」（MTP 层），让主模型一次预测多个后续 token，再由主模型自身验证。好处是不需要维护第二个模型，坏处是这个投机头要和主模型一起训练、一起转换权重。

### 2.3 接受长度（accepted length）

投机解码里最关键的指标是「每次 forward 接受了几个 token」，记为 `num_accepted`。如果草稿猜对了，主模型就接受它（省下一次 forward）；猜错了，从错误点之后丢弃、用主模型的正确 token 接上。

设每次 forward 平均接受 \( \mu \) 个 token，则生成 \( N \) 个 token 大约只需要 \( N/\mu \) 次 forward，TPOT 从 \( t \) 降到约 \( t/\mu \)。本讲后面会用 README 的 `mean=2.77` 实算这个收益。

### 2.4 复习：四元张量契约与 Idx

回顾 u2-l5：Python 把权重 `params`、KV 缓存 `caches`、激活临时变量 `temp_vars`、`profile_logs` 四组扁平张量列表交给 C++ 后端，并在 CUDA Graph 里固化。`temp_vars` 的每个槽位由 `Idx`（`DsaTempVarIdx`）枚举命名。本讲会反复用到这些 MTP 相关槽位：`DRAFT_TOKENS`、`PREDICTED_TOKENS`、`ACCEPTED_TOKENS`、`NEXT_DRAFT_TOKENS`、`CUR_POS`、`LAST_HIDDEN_STATES`、`TOKEN_OUT`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [tilert/models/deepseek_v3_2/generator.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py) | 生成器，含两条解码路径 | `_generate_with_mtp` 双阶段主循环（核心） |
| [tilert/models/deepseek_v3_2/modules/mtp.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp.py) | MTP 容器，组装投机头 | `register_op` 三个子算子、共享 embedding |
| [tilert/models/deepseek_v3_2/modules/mtp_preprocess.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py) | MTP 预处理层 | `MTPPreprocessLayer`：融合 embedding + 上一步隐状态 |
| [tilert/models/deepseek_v3_2/modules/end2end.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py) | `ShowHandsDSALayer` 执行器 | MTP 相关的 set/get 方法、`forward` |
| [tilert/models/deepseek_v3_2/temp_var_indices.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py) | `Idx` 枚举 | MTP 槽位编号 |
| [README.md](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md) | MTP 用法与示例 | `mean=2.77` 统计示例 |

## 4. 核心概念与源码讲解

### 4.1 MTP 投机解码：核心思想、双阶段总览与收益

#### 4.1.1 概念说明

MTP 在 TileRT 里是一个**可选的解码加速通道**。它在构造 `DSAv32Generator` 时由 `with_mtp=True` 开启，决定三件事：

1. 要不要加载 MTP 投机头的权重（`from_pretrained` 时额外装配 `MTP` 容器）。
2. 调用 `generate` 时走 `_generate_with_mtp` 还是 `_generate_without_mtp`。
3. `mtp_seq_len` 取 4 还是 1。

`mtp_seq_len=4` 是理解整个 MTP 的关键数字：它表示 **一次 forward 里 MTP 通道处理的 token 序列长度为 4**。后端据此把主模型输出 + MTP 头预测拼成最多 4 个候选 token，由主模型验证后决定接受几个。

#### 4.1.2 核心流程

`generate` 根据 `with_mtp` 分流，整个 MTP 路径分为两大阶段：

```
generate(prompt, with_mtp=True)
  │
  ├─ set_sampling_seed(seed, with_mtp=True)      # 请求级采样种子
  │
  └─ _generate_with_mtp(prompt)
       │
       ├─ ① prefill 阶段：把 prompt 切成长度 4 的块逐块喂入，
       │     填充 KV 缓存，不做投机接受（prompt 是已知答案）
       │
       ├─ set_cur_pos(prompt_len - 1)             # 对齐 RoPE 位置
       │
       └─ ② decode 阶段：每步用上一轮 draft 拼输入，
              forward 后读 num_accepted / predicted_tokens，
              按接受数量回写并推进 cur_pos
```

prefill 阶段的目标是**把 prompt 的 KV 状态灌进缓存**（与非 MTP 路径用每步一个 token 灌缓存是同一目的，只是这里用块为单位更快）；decode 阶段才是真正「投机 + 接受」产生新 token 的地方。这也是为什么统计接受长度时只统计 decode 阶段的 `decode_accepted_counts`，prefill 阶段不参与。

#### 4.1.3 源码精读

分流发生在 `generate` 里，注意第 178 行的守卫：只有加载了 MTP 权重（`self.with_mtp=True`）才允许本次调用用 MTP：

```python
# tilert/models/deepseek_v3_2/generator.py:176-185
active_mtp = with_mtp if with_mtp is not None else self.with_mtp
if active_mtp and not self.with_mtp:
    raise ValueError("Cannot use MTP mode: MTP weights were not loaded")
self.decode_layer.set_sampling_seed(self.sampling_seed, with_mtp=active_mtp)
if active_mtp:
    return self._generate_with_mtp(prompt, print_log, prompt_tokens=prompt_tokens)
```

[generator.py:176-185](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L176-L185)：MTP 分流与「未加载权重则报错」守卫。

`mtp_seq_len` 在构造时确定，是 MTP 路径与普通路径最显著的常量差异：

```python
# tilert/models/deepseek_v3_2/generator.py:89
self.mtp_seq_len = 4 if with_mtp else 1
```

[generator.py:89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L89)：`mtp_seq_len` 开关。

#### 4.1.4 代码实践

**实践目标**：从 API 层面确认 MTP 是「同一段代码、两条路径」的开关，而非另一套接口。

**操作步骤**：

1. 对照 README 的 MTP 示例（[README.md:249-268](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L249-L268)），把构造参数里的 `with_mtp=True` 改成 `with_mtp=False`。
2. 不改动 `generate(prompt)` 的调用方式。
3. 观察两次返回值结构。

**需要观察的现象**：`generate` 返回 `(text, time_list, accepted_counts, prompt_len)`。MTP 模式下 `accepted_counts` 是一个非空列表（decode 阶段每步的接受数）；非 MTP 模式下 `accepted_counts` 恒为 `[]`（见 generator.py:185）。

**预期结果**：同一份调用代码，仅靠构造时的 `with_mtp` 开关，就切换了底层解码路径与返回的统计内容。这验证了 README 所说的「preserving the same Python API interface」。

**待本地验证**：真实硬件（8× B200）下两条路径的 TPOT 差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 说 MTP「preserving the same Python API interface」？

> **答**：因为 `generate` 的签名和返回结构对调用方不变。MTP 与否由 Generator 构造时的 `with_mtp`（或 `generate` 的 `with_mtp` 覆盖参数）决定，调用方无需为 MTP 改写代码；只是返回的 `accepted_counts` 在 MTP 下才有内容。

**练习 2**：如果不传 `with_mtp=True` 就直接在 `generate` 里传 `with_mtp=True`，会发生什么？

> **答**：会触发 generator.py:177-178 的守卫，抛 `ValueError("Cannot use MTP mode: MTP weights were not loaded")`。因为 MTP 权重在 `from_pretrained` 时只有在 `self.with_mtp=True` 才会装配（见 4.5.3）。

---

### 4.2 prefill 阶段：prompt 分块、padding 与控制算子

#### 4.2.1 概念说明

prefill 阶段要解决的问题是：**把整段 prompt 的 KV 状态填进缓存**，好让 decode 阶段能从 prompt 末尾继续生成。与非 MTP 路径「每步喂一个 prompt token」不同，MTP 路径把 prompt 切成长度 4 的块，**一块一块地喂**——这本身就是一种加速（4 个 token 一批处理）。

但 MTP 后端期望的输入是定长 4 的「draft 序列」。当 prompt 长度不是 4 的倍数时，最后一块会不足 4 个，需要 padding。此外，MTP 头的预处理需要「下一个 token」作为移位输入（`mtp_extra_token`），这也需要专门通过控制算子告诉后端。

#### 4.2.2 核心流程

```
cur_pos = 0
while cur_pos < prompt_len - 1:
    draft_end = min(cur_pos + 4, prompt_len)
    draft_tokens = tokens[cur_pos : draft_end]          # 取一块，可能 < 4
    actual_token_count = draft_tokens 的真实长度
    if 不足 4:
        用块内最后一个 token 填充到 4
    set_prefill_mtp_extra_token(tokens[cur_pos + 4])    # MTP[0] 移位输入
    set_prefill_valid_tokens(actual_token_count)        # 告诉后端有几个是真
    forward(draft_tokens, with_mtp=True)
    cur_pos += actual_token_count

# prefill 结束，对齐位置
cur_pos = prompt_len - 1
set_cur_pos(prompt_len - 1)
set_prefill_valid_tokens(0)                              # 关闭 prefill 特殊处理
```

两个控制算子的含义：

- `set_prefill_valid_tokens(n)`：告诉后端这个长度 4 的 draft 块里只有前 `n` 个是真实 token，其余是 padding（要被复制而非生成）。
- `set_prefill_mtp_extra_token(t)`：MTP 预处理需要把「当前块之后紧跟的那个 token」当作 MTP 头的监督/移位输入，这里把它单独传进去。

#### 4.2.3 源码精读

prefill 主循环（注意 `while cur_pos < prompt_len - 1`，留最后一个 prompt token 给 decode 阶段的第一步）：

```python
# tilert/models/deepseek_v3_2/generator.py:297-328
while cur_pos < prompt_len - 1:
    draft_end = min(cur_pos + self.mtp_seq_len, prompt_len)
    draft_tokens = tokens[0, cur_pos:draft_end].clone()
    actual_token_count = draft_tokens.shape[0]

    if actual_token_count < self.mtp_seq_len:
        pad_token = draft_tokens[-1].item()
        padding = torch.full(
            (self.mtp_seq_len - actual_token_count,),
            pad_token, dtype=torch.long, device=self.default_device,
        )
        draft_tokens = torch.cat([draft_tokens, padding])

    draft_tokens = draft_tokens.reshape(1, self.mtp_seq_len).to(torch.int32)

    mtp_extra_pos = cur_pos + self.mtp_seq_len
    if mtp_extra_pos < prompt_len:
        mtp_extra_token = int(tokens[0, mtp_extra_pos].item())
    else:
        mtp_extra_token = int(tokens[0, draft_end - 1].item())
    self.decode_layer.set_prefill_mtp_extra_token(mtp_extra_token)

    self.decode_layer.set_prefill_valid_tokens(actual_token_count)

    start_time = time.time()
    self.decode_layer.forward(draft_tokens, with_mtp=True)
    end_time = time.time()
    prefill_time_list.append(end_time - start_time)

    cur_pos += actual_token_count
```

[generator.py:297-328](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L297-L328)：prefill 分块、padding、控制算子与推进。

padding 的细节值得注意：**用块内最后一个 token 去填充**（`pad_token = draft_tokens[-1].item()`），而不是用 0 或 eos。这样 padding 位和真实末位 token 相同，配合 `set_prefill_valid_tokens` 告知后端只取前 `actual_token_count` 个，padding 位的计算结果会被丢弃，不影响正确性。

prefill 结束后的位置对齐：

```python
# tilert/models/deepseek_v3_2/generator.py:330-333
cur_pos = prompt_len - 1
self.set_cur_pos(prompt_len - 1)

self.decode_layer.set_prefill_valid_tokens(0)
```

[generator.py:330-333](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L330-L333)：把 `cur_pos` 重置到 `prompt_len - 1`，并通过 `set_cur_pos` 同步后端 RoPE 位置，最后把 `valid_tokens` 置 0 关闭 prefill 特殊处理。

这两个控制算子在执行器里只是薄封装，转调后端 `torch.ops.tilert.*` 算子：

```python
# tilert/models/deepseek_v3_2/modules/end2end.py:598-615
def set_prefill_valid_tokens(self, num_valid_tokens: int) -> None:
    dsa_mtp_e2e_show_hands_set_prefill_valid_tokens(num_valid_tokens, self.is_glm5)

def set_prefill_mtp_extra_token(self, token: int) -> None:
    dsa_mtp_e2e_show_hands_set_prefill_mtp_extra_token(token, self.is_glm5)
```

[end2end.py:598-615](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L598-L615)：两个 prefill 控制算子的封装。

#### 4.2.4 代码实践

**实践目标**：手动模拟 prefill 的分块与 padding 逻辑，验证不同 prompt 长度下块的划分。

**操作步骤**：写一段**示例代码**（非项目原有），在 CPU 上复现分块逻辑：

```python
# 示例代码：模拟 prefill 分块（不依赖 GPU）
mtp_seq_len = 4
prompt_len = 10          # 试着改成 8、9、12 观察变化
tokens = list(range(prompt_len))   # 假装是 prompt token id

cur_pos = 0
chunks = []
while cur_pos < prompt_len - 1:
    draft_end = min(cur_pos + mtp_seq_len, prompt_len)
    block = tokens[cur_pos:draft_end]
    actual = len(block)
    if actual < mtp_seq_len:
        block = block + [block[-1]] * (mtp_seq_len - actual)   # 用末位填充
    extra_pos = cur_pos + mtp_seq_len
    extra = tokens[extra_pos] if extra_pos < prompt_len else tokens[draft_end - 1]
    chunks.append((cur_pos, actual, block, extra))
    cur_pos += actual

for c in chunks:
    print(f"start={c[0]:2d} actual={c[1]} block={c[2]} extra={c[3]}")
```

**需要观察的现象**：

- `prompt_len=10`：应得到 3 块（start=0/4/7），最后一块 actual=3、被填充到 4。
- `prompt_len=8`：2 块，各 4 个，无 padding。
- 注意循环条件 `cur_pos < prompt_len - 1`，最后会停在 `prompt_len-1`，把最后一个 prompt token 留给 decode 第一步。

**预期结果**：你能口头预测任意 `prompt_len` 下的分块数量与每块的 `actual` 值。这等价于你理解了源码循环。

**待本地验证**：真实运行时 `prefill_time_list` 的长度应等于这里的分块数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 padding 用「块内最后一个真实 token」而不是固定值 0？

> **答**：因为 padding 位本身不携带信息，且会被 `set_prefill_valid_tokens(actual)` 告知后端忽略。用末位 token 填充可以避免后端在无效位置上产生异常 embedding/采样，是一种保守且无害的选择；后端只复制、不生成这些位。

**练习 2**：为什么循环条件是 `cur_pos < prompt_len - 1` 而不是 `cur_pos < prompt_len`？

> **答**：最后一个 prompt token 要留给 decode 阶段的第一步（见 4.3，decode 第一步用 `tokens[prompt_len - 1]` 作为 draft 起点）。这样 prefill 填好前 `prompt_len - 1` 个位置的 KV，decode 第一步正好从第 `prompt_len - 1` 个 prompt token 出发预测第一个新 token。

---

### 4.3 decode 阶段：draft token 组装与多 token 接受回写

#### 4.3.1 概念说明

decode 阶段是 MTP 真正「投机 + 接受」产生新 token 的地方。每一步：

1. 组装一个长度 4 的 draft 输入：第一步用最后一个 prompt token 重复 4 次；其后各步用上一轮 forward 留下的 `next_draft_tokens`。
2. 跑一次 `forward`，后端在 MTP 通道里验证草稿，产出「接受几个」(`num_accepted`) 和「接受/纠正后的 token 列表」(`predicted_tokens`)。
3. 把 `num_accepted` 个 token 写回 `tokens` 缓冲，并把 `cur_pos` 推进 `num_accepted`。

关键直觉：`cur_pos += num_accepted` 而不是 `+= 1`。这就是投机解码省 forward 的来源——一步接受多个 token，游标就跳多个位置。

#### 4.3.2 核心流程

```
finished = False
while cur_pos < total_len - 1 and not finished:
    if 第一步 (cur_pos == prompt_len - 1):
        draft = [last_prompt_token] * 4              # 起点
    else:
        draft = get_next_draft_tokens(0)             # 复用上一轮的 draft

    forward(draft, with_mtp=True)

    num_accepted  = get_num_accepted(0)              # 本轮接受几个
    predicted     = get_predicted_tokens(0)          # 本轮产出 token 列表
    decode_accepted_counts.append(num_accepted)

    # 把 accepted 个 token 写回 tokens，逐个检查 EOS
    for i in range(num_accepted):
        if cur_pos + 1 + i >= total_len: break
        new_token = predicted[i]
        tokens[cur_pos + 1 + i] = new_token
        if new_token == eos: finished = True; break

    cur_pos += num_accepted                          # 关键：多 token 推进
```

`get_next_draft_tokens` 返回的「下一轮 draft」是后端在验证过程中顺带为下一轮准备的草稿——这是投机解码「连续投机」的体现：验证本轮的同时，已经为下一轮生成了候选。

#### 4.3.3 源码精读

decode 主循环：

```python
# tilert/models/deepseek_v3_2/generator.py:335-375
finished = False
while cur_pos < total_len - 1 and not finished:
    if cur_pos == prompt_len - 1:
        last_token = tokens[0, prompt_len - 1].item()
        draft_tokens = torch.full(
            (self.mtp_seq_len,), last_token,
            dtype=torch.long, device=self.default_device,
        )
        draft_tokens = draft_tokens.reshape(1, self.mtp_seq_len).to(torch.int32)
    else:
        draft_tokens = self.decode_layer.get_next_draft_tokens(0).reshape(
            1, self.mtp_seq_len
        )

    start_time = time.time()
    self.decode_layer.forward(draft_tokens, with_mtp=True)
    end_time = time.time()
    decode_time_list.append(end_time - start_time)

    num_accepted = self.decode_layer.get_num_accepted(0)
    predicted_tokens = self.decode_layer.get_predicted_tokens(0).flatten()
    decode_accepted_counts.append(num_accepted)

    num_output_tokens = num_accepted
    for i in range(num_output_tokens):
        if cur_pos + 1 + i >= total_len:
            break
        new_token = int(predicted_tokens[i].item())
        tokens[0, cur_pos + 1 + i] = new_token

        if cur_pos + 1 + i >= prompt_len and print_log:
            decoded_text = self.tokenizer.decode([new_token], skip_special_tokens=True)
            print(decoded_text, end="", flush=True)

        if new_token == self.eos_id:
            finished = True
            break

    cur_pos += num_accepted
```

[generator.py:335-375](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L335-L375)：decode 主循环——draft 组装、forward、读接受数、回写、推进。

三个读取方法都是「从对应设备的 temp_vars 槽位读结果」的薄封装，这正是 u2-l5「Idx 命名扁平下标」的应用：

```python
# tilert/models/deepseek_v3_2/modules/end2end.py:617-651
def get_next_draft_tokens(self, device_id: int = 0) -> torch.Tensor:
    intermediates, _, _, _ = self._get_device_result(device_id)
    return intermediates[Idx.NEXT_DRAFT_TOKENS]

def get_num_accepted(self, device_id: int = 0) -> int:
    intermediates, _, _, _ = self._get_device_result(device_id)
    return int(intermediates[Idx.ACCEPTED_TOKENS][0].item())

def get_predicted_tokens(self, device_id: int = 0) -> torch.Tensor:
    intermediates, _, _, _ = self._get_device_result(device_id)
    return intermediates[Idx.PREDICTED_TOKENS]
```

[end2end.py:617-651](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L617-L651)：`get_next_draft_tokens` / `get_num_accepted` / `get_predicted_tokens` 直接按 `Idx` 下标读 temp_vars。

对应的槽位定义在枚举里，`NEXT_DRAFT_TOKENS`、`ACCEPTED_TOKENS`、`PREDICTED_TOKENS` 是 MTP 专属的激活槽：

```python
# tilert/models/deepseek_v3_2/temp_var_indices.py:52-56
DRAFT_TOKENS = 34
PREDICTED_TOKENS = 35
PREDICTED_HIDDEN = 36
ACCEPTED_TOKENS = 37
NEXT_DRAFT_TOKENS = 38
```

[temp_var_indices.py:52-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L52-L56)：MTP 相关的 temp_vars 槽位编号。

#### 4.3.4 代码实践

**实践目标**：追踪 `num_accepted` 与 `predicted_tokens` 如何决定 token 写回位置，验证 `cur_pos += num_accepted` 的推进逻辑（这是本讲的核心实践任务）。

**操作步骤**：写一段**示例代码**模拟 decode 循环的游标推进（不依赖 GPU）：

```python
# 示例代码：模拟 decode 阶段的游标推进与回写
prompt_len = 5
total_len = 20
tokens = [-1] * total_len
for i in range(prompt_len):
    tokens[i] = 1000 + i          # 假装的 prompt

# 假装后端每步返回的 (num_accepted, predicted_tokens)
fake_steps = [(2, [2001, 2002]), (3, [2003, 2004, 2005]),
              (1, [2006]), (4, [2007, 2008, 2009, 2010])]

cur_pos = prompt_len - 1          # = 4，与源码 set_cur_pos 后一致
written = []
for num_accepted, predicted in fake_steps:
    for i in range(num_accepted):
        if cur_pos + 1 + i >= total_len:
            break
        new_token = predicted[i]
        tokens[cur_pos + 1 + i] = new_token
        written.append((cur_pos + 1 + i, new_token))
    cur_pos += num_accepted
    print(f"after step: cur_pos={cur_pos}, tokens={tokens}")

print("写入位置序列:", written)
print("生成区:", tokens[prompt_len:])
```

**需要观察的现象**：

- 第一步 `num_accepted=2`：把 2001、2002 写到位置 5、6，`cur_pos` 从 4 跳到 6。
- 第二步 `num_accepted=3`：写到位置 7、8、9，`cur_pos` 跳到 9。
- 写回位置永远是 `cur_pos + 1 + i`，即「当前游标之后连续 accepted 个位置」。

**预期结果**：你能预测每步后 `cur_pos` 的值与写入位置。把 `fake_steps` 的 `num_accepted` 全改成 1，就退化成 u3-l2 的非 MTP 路径（每步写一个、游标 +1），从而直观看到 MTP 的省 forward 效果。

**待本地验证**：真实运行时 `decode_accepted_counts` 列表就是每步的真实 `num_accepted`。

#### 4.3.5 小练习与答案

**练习 1**：decode 第一步为什么用「最后一个 prompt token 重复 4 次」作为 draft？

> **答**：因为 decode 第一步发生在 `cur_pos == prompt_len - 1`，此时还没有「上一轮的 next_draft_tokens」可用（prefill 阶段不产生给 decode 用的 draft）。用一个确定的真实 token（prompt 末位）填充 4 位，相当于给后端一个合法的起点 draft，后端会在这一步产出第一批新 token 与给下一步用的 `NEXT_DRAFT_TOKENS`。

**练习 2**：如果 `num_accepted` 恒为 1，MTP 路径相比非 MTP 路径是更快还是更慢？

> **答**：更慢（或至少不更快）。因为 `num_accepted=1` 意味着每次 forward 仍只接受 1 个 token，但 MTP 通道还要额外跑投机头的计算，徒增开销却没省 forward。MTP 的收益完全来自 `num_accepted > 1`。

---

### 4.4 接受长度统计与「一次 forward 实际产出多少 token」

#### 4.4.1 概念说明

MTP 的性能收益完全由「平均接受长度」决定。TileRT 在 decode 阶段把每步的 `num_accepted` 收集进 `decode_accepted_counts`，结束时打印均值、最小、最大。README 给出的典型统计是：

```text
Accepted length: mean=2.77, min=1, max=4
```

这一节我们要把 `mean=2.77` 翻译成两个具体的工程结论：(1) 一次 forward 实际产出几个 token；(2) 相比非 MTP，生成同样多 token 省了多少 forward。

#### 4.4.2 核心流程

设生成了 \( N \) 个 token，decode 阶段共调用 \( K \) 次 forward，每次接受数为 \( a_i \)，则：

\[
N = \sum_{i=1}^{K} a_i, \qquad \mu = \frac{N}{K} = \frac{1}{K}\sum_{i=1}^{K} a_i
\]

- **一次 forward 平均产出的 token 数**就是 \( \mu \)。`mean=2.77` 表示平均每次 forward 产出 2.77 个 token。
- **forward 调用数** \( K = N / \mu \)。生成 1000 个 token 约需 \( 1000 / 2.77 \approx 361 \) 次 forward，而非 MTP 需要 1000 次。
- **理论加速比**（就 forward 次数而言）为 \( \mu \approx 2.77\times \)。

注意 `max=4` 与 `mtp_seq_len=4` 对应：单次最多接受 4 个（MTP 头最多预测这么多）；`min=1` 表示最坏情况下只接受主模型自己产出的那一个。

> **说明**：上述「产出 token 数」指主模型验证后被接受的 token 数。后端 MTP 头会预测更多候选，但只有被验证正确的才计入 `num_accepted`。

#### 4.4.3 源码精读

统计发生在 decode 循环结束后：

```python
# tilert/models/deepseek_v3_2/generator.py:377-396
if print_log:
    print("\n")
    total_tokens = sum(decode_accepted_counts)
    logger.info(f"--Number of forward calls (decode): {len(decode_accepted_counts)}")
    logger.info(f"--Total tokens generated: {total_tokens}")
    if len(decode_accepted_counts) > 0:
        avg_accepted = sum(decode_accepted_counts) / len(decode_accepted_counts)
        min_accepted = min(decode_accepted_counts)
        max_accepted = max(decode_accepted_counts)
        logger.info(
            f"--Accepted tokens per call: mean={avg_accepted:.2f}, "
            f"min={min_accepted}, max={max_accepted}"
        )

    if decode_time_list:
        total_decode_time = sum(decode_time_list)
        effective_tps = total_tokens / total_decode_time if total_decode_time > 0 else 0
        avg_time_ms = total_decode_time / len(decode_time_list) * 1000
        logger.info(f"--Avg forward time: {avg_time_ms:.2f}ms")
        logger.info(f"--Effective TPS (with MTP): {effective_tps:.2f} tokens/s")
```

[generator.py:377-396](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L377-L396)：decode 阶段统计——forward 次数、总 token 数、接受长度均值/极值、有效 TPS。

注意几个量的口径：

- `total_tokens = sum(decode_accepted_counts)`：decode 阶段实际产出的 token 总数。
- `len(decode_accepted_counts)`：decode 阶段 forward 调用次数。
- `effective_tps = total_tokens / total_decode_time`：把「一步多 token」算进去后的有效吞吐，这正是 MTP 相比非 MTP 的 TPS 提升来源（非 MTP 的 TPS 上限约等于 `1/单步时间`）。

README 的统计示例：

```text
# README.md:270-276
Accepted length: mean=2.77, min=1, max=4
```

[README.md:270-276](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L270-L276)：README 给出的 MTP 接受长度统计示例。

README 的发布新闻也印证了 MTP 的吞吐收益（注意这里 `mtp=3` 指的是 `num_mtp=3` 个草稿头，对应 `mtp_seq_len=4`）：

```text
# README.md:38
Multi-Token Prediction (MTP) is now available in TileRT! With mtp=3, we achieve
decoding rates of up to 590 tokens/s under synthetic workloads.
```

[README.md:38](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L38)：MTP 发布时的吞吐数据。

#### 4.4.4 代码实践

**实践目标**：用 `mean=2.77` 实算「一次 forward 实际产出多少 token」与「省了多少 forward」（本讲核心实践任务的第二部分）。

**操作步骤**：手算并填表（纯算术，无需 GPU）：

| 量 | 公式 | 代入 mean=2.77 | 结果 |
|----|------|----------------|------|
| 一次 forward 平均产出 token 数 | \( \mu \) | 2.77 | **2.77 个** |
| 生成 1000 token 所需 forward 数 | \( N/\mu \) | 1000/2.77 | **≈ 361 次** |
| 非 MTP 所需 forward 数 | \( N \) | 1000 | 1000 次 |
| 省下的 forward 数 | \( N - N/\mu \) | 1000 − 361 | **≈ 639 次** |
| 理论 forward 次数加速比 | \( \mu \) | 2.77 | **≈ 2.77×** |

**需要观察的现象**：

- 「一次 forward 实际产出多少 token」的答案就是均值本身：**平均 2.77 个**（介于 min=1 与 max=4 之间）。
- 若把均值改成 GLM-5.1 在 README 中提到的「average acceptance length 3.2」（[README.md:61](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L61)），生成 1000 token 只需约 312 次 forward，加速比约 3.2×。

**预期结果**：你能向别人解释「为什么 MTP 能把 TPOT 降下来」——不是因为单次 forward 变快，而是因为单次 forward 产出的 token 变多了，从而摊薄了每 token 的平均延迟。

**待本地验证**：真实运行 `python -m tilert.generate --model deepseek_v3_2 --with-mtp --max-new-tokens 1000`，把打印的 `mean` 代入上表复算。

#### 4.4.5 小练习与答案

**练习 1**：README 同时给出了 GLM-5.1 的「average acceptance length 3.2」和「peak under best-case MTP acceptance (4.0)」。为什么峰值是 4.0？

> **答**：因为 `mtp_seq_len=4`（`num_mtp=3` 个草稿 + 主模型 1 个），单次 forward 最多接受 4 个 token。`max=4` 就是这个上限。mean=3.2/2.77 是平均，受模型预测准确率制约，永远 ≤ 4。

**练习 2**：`effective_tps = total_tokens / total_decode_time`，而 `avg_time_ms` 是单次 forward 平均耗时。如果 `avg_time_ms` 不变，`effective_tps` 与 `mean` 是什么关系？

> **答**：`effective_tps = total_tokens / (K · avg_time_ms) = (N) / ((N/\mu) · t) = \mu / t`。即有效 TPS 正比于接受长度均值 \( \mu \)，反比于单次 forward 时间 \( t \)。在单次 forward 耗时不变的前提下，接受长度越高，有效 TPS 越高——这就是 MTP 通过提高 \( \mu \) 来提速的数学本质。

---

### 4.5 MTP 模块子结构：复用主模型算子的投机头

#### 4.5.1 概念说明

前四节都在讲 Python 编排层（generator 如何调度）。这一节下沉到模型组装层，看 MTP 这个「投机头」到底由哪些算子拼成。

关键结论：**MTP 头不是从零写的，它复用了主模型的算子**。`MTP` 容器由三部分组成：

1. `MTPPreprocessLayer`：MTP 专属的预处理层（融合「当前 token embedding」与「上一步主模型隐状态」）。
2. `MoeBlock`：**直接复用主模型的 MoE 层**（注意力 + MoE 前馈），这是 u2-l4 / u2-l7 讲过的同一个类。
3. `RMSNormHeadProj`：**复用主模型的 head 投影**（最终 RMSNorm + lm_head），并设置 `retain_weights=True`（因为 head 权重与主模型共享，不能被 `remove_selected` 释放）。

而且 MTP 头挂在 `layer_{n_layers}_` 前缀下（DeepSeek-V3.2 的 `n_layers=61`，所以是 `layer_61_`），相当于「第 62 个 transformer 层」，权重键名与主模型 61 层遵循同一套 `layer_{i}_{alias}_dev_{d}` 契约（见 u1-l6、u2-l4）。

#### 4.5.2 核心流程

```
MTP 容器 (layer_61_)
  ├─ MTPPreprocessLayer   prefix=layer_61_  suffix=_dev_{d}
  │     RMSNorm(embedding) ⊕ RMSNorm(last_hidden) → eh_proj → 隐状态
  ├─ MoeBlock              prefix=layer_61_  suffix=_dev_{d}
  │     MLA 注意力 + MoE 前馈  (复用主模型算子)
  └─ RMSNormHeadProj       prefix=layer_61_  suffix=_dev_{d}  retain_weights=True
        RMSNorm + lm_head   (复用主模型 head，权重共享)

MTP 还额外持有两个全局共享张量：
  ├─ model.embed_tokens.weight   (词表嵌入，与主模型共享)
  └─ freqs_cis                   (RoPE 频率表，与主模型共享)
```

`MTPPreprocessLayer` 的预处理逻辑（来自它的 `golden_forward` 参考实现）：对当前 token 的 embedding 做 RMSNorm，对上一步主模型的隐状态（`last_hidden_states`）做 RMSNorm，两者拼接后过一个线性投影 `eh_proj`，得到 MTP 层的输入隐状态。这就是 MTP「把主模型末层隐状态当作额外输入」的关键——它解释了为什么 PD 分离时需要 `inject_last_hidden_state`（见 u4-l6）。

#### 4.5.3 源码精读

`MTP` 容器用三次 `register_op` 装配三个子算子，前缀统一为 `layer_{n_layers}_`：

```python
# tilert/models/deepseek_v3_2/modules/mtp.py:27-50
mtp_layer_id = self.model_args.n_layers
self.register_op(
    MTPPreprocessLayer(self.model_args, self.num_devices, device_id),
    prefix=f"layer_{mtp_layer_id}_",
    suffix=f"_dev_{device_id}",
)
self.register_op(
    MoeBlock(
        model_args=model_args,
        device_id=device_id,
        num_devices=num_devices,
        mla_cls=mla_cls,
        mla_num_devices=mla_num_devices,
        mla_kwargs=mla_kwargs,
    ),
    prefix=f"layer_{mtp_layer_id}_",
    suffix=f"_dev_{device_id}",
)
self.register_op(
    RMSNormHeadProj(model_args=model_args, device_id=device_id, num_devices=num_devices),
    prefix=f"layer_{mtp_layer_id}_",
    suffix=f"_dev_{device_id}",
    retain_weights=True,
)
```

[mtp.py:27-50](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp.py#L27-L50)：MTP 容器的三次 `register_op`——预处理层、复用的 `MoeBlock`、复用的 `RMSNormHeadProj`（`retain_weights=True`）。

MTP 还要额外接管两个全局共享张量（embedding 与 RoPE 频率表），并在 `init_tilert_weights` 里先取出它们再委托给父类：

```python
# tilert/models/deepseek_v3_2/modules/mtp.py:52-62
def init_tilert_weights(self, state_dicts: dict[str, torch.Tensor]) -> None:
    self.embed_tokens_weight = state_dicts["model.embed_tokens.weight"]
    self.freqs_cis = state_dicts["freqs_cis"]
    super().init_tilert_weights(state_dicts)

def get_weights_list(self) -> list[torch.Tensor]:
    return [
        self.embed_tokens_weight,
        self.freqs_cis,
        *super().get_weights_list(),
    ]
```

[mtp.py:52-62](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp.py#L52-L62)：MTP 接管共享的 embedding 与 freqs_cis，再把它们连同子算子权重一起塞进 `get_weights_list`（最终 extend 进 params 列表）。

`MTPPreprocessLayer` 的参考前向实现清楚展示了「融合 embedding + 上一步隐状态」的逻辑：

```python
# tilert/models/deepseek_v3_2/modules/mtp_preprocess.py:197-229
def golden_forward(
    self,
    x: torch.Tensor,
    last_hidden_states: torch.Tensor,
) -> torch.Tensor:
    assert self.ref_embedding_rmsnorm_gamma is not None
    assert self.ref_hidden_rmsnorm_gamma is not None
    assert self.ref_eh_proj_weight is not None

    future_norm = torch.nn.functional.rms_norm(
        x.float(), [x.size(-1)], self.ref_embedding_rmsnorm_gamma, 1e-6,
    )
    prev_norm = torch.nn.functional.rms_norm(
        last_hidden_states.float(),
        [last_hidden_states.size(-1)],
        self.ref_hidden_rmsnorm_gamma,
        1e-6,
    )
    combined = torch.cat([future_norm, prev_norm], dim=-1)
    return linear(combined, self.ref_eh_proj_weight)
```

[mtp_preprocess.py:197-229](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py#L197-L229)：`MTPPreprocessLayer` 参考前向——分别 RMSNorm(embedding) 与 RMSNorm(last_hidden)，拼接后过 `eh_proj` 投影。这就是 MTP 头「吃主模型末层隐状态」的来源。

在执行器里，MTP 容器的创建发生在 8 卡权重加载线程内（见 u2-l3），并且**复用了主模型 `Dsa` 的 V2 P2P 缓冲**（`peer_bufs` / `ll_buf`），所以 MTP 头的 MLA 通信也走 device 0 广播稀疏选择的同一套机制：

```python
# tilert/models/deepseek_v3_2/modules/end2end.py:435-454
if self.with_mtp:
    from tilert.models.deepseek_v3_2.modules.mla_v2 import (
        PureMlaV2,
        SparseSelectMlaV2,
    )

    mtp_kwargs: dict = {}
    mtp_kwargs["mla_cls"] = SparseSelectMlaV2 if device_id == 0 else PureMlaV2
    mtp_kwargs["mla_num_devices"] = 1 if device_id == 0 else self.num_devices - 1
    if device_id == 0:
        mtp_kwargs["mla_kwargs"] = {
            "peer_bufs": dsa.v2_peer_bufs,
        }
    else:
        mtp_kwargs["mla_kwargs"] = {"ll_buf": dsa.v2_ll_buf}
    mtp = MTP(self.model_args, device_id, self.num_devices, **mtp_kwargs)
    mtp.init_tilert_weights(state_dicts)
    params.extend(mtp.get_weights_list())
    caches.extend(mtp.get_cache_vars())
    logger.info(f"Loaded real MTP weights for device {device_id}")
```

[end2end.py:435-454](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L435-L454)：MTP 容器在加载线程内创建，MLA 选型与主模型一致（device 0 用 `SparseSelectMlaV2`），并复用 `dsa.v2_peer_bufs` / `dsa.v2_ll_buf`，MTP 权重与缓存分别 `extend` 进 params / caches。

#### 4.5.4 代码实践

**实践目标**：验证 MTP 头的权重键名契约，确认它与主模型遵循同一套 `layer_{i}_{alias}_dev_{d}` 命名（见 u1-l6 权重转换）。

**操作步骤（源码阅读型实践）**：

1. 打开 [mtp.py:27-50](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp.py#L27-L50)，记下三次 `register_op` 的 `prefix="layer_61_"`、`suffix="_dev_{d}"`。
2. 打开 [mtp_preprocess.py:50-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py#L50-L66)，记下 `MTPPreprocessTilertWeightsAlias` 的三个短别名：`embedding_rmsnorm_gamma`、`hidden_rmsnorm_gamma`、`eh_proj_weights`。
3. 按 u2-l1 / u2-l4 的键名拼接规则（`prefix + 短别名 + suffix`）写出 device 0 上 MTP 预处理层的三个权重键名。

**需要观察的现象 / 预期结果**：拼出的键名应为：

```
layer_61_embedding_rmsnorm_gamma_dev_0
layer_61_hidden_rmsnorm_gamma_dev_0
layer_61_eh_proj_weights_dev_0
```

这与 u1-l6 权重转换器写出的 MTP 层（`layer_61_...`）键名逐字符一致——正是这套统一契约让离线转换与运行时加载能对上号。

**进阶观察**：对比 `RMSNormHeadProj` 注册时带了 `retain_weights=True`（mtp.py:49），而前两个 `register_op` 没带。结合 u2-l1 解释：head 权重与主模型末层 head 共享，加载后不能被 `remove_selected` 释放，所以显式 retain。

**待本地验证**：在转换后的权重目录用 `safetensors` 工具检索 `layer_61_` 前缀的键，确认它们存在且分卡后缀正确。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `RMSNormHeadProj` 要设 `retain_weights=True`，而 `MTPPreprocessLayer` 不用？

> **答**：`RMSNormHeadProj` 的 head（lm_head）权重与主模型共享（MTP 头预测的 logits 用同一个 head 投影）。`remove_selected` 会释放已用完的权重以降峰值显存（见 u2-l1），但共享权重在后续还会被主模型用到，不能释放，故显式 `retain_weights=True`。`MTPPreprocessLayer` 的权重（`eh_proj` 等）是 MTP 专属、加载完即可转交后端，无需 retain。

**练习 2**：MTP 头的 MLA 为什么能直接复用主模型 `Dsa` 的 `peer_bufs` / `ll_buf`？

> **答**：因为 MTP 头用的就是主模型同一套 MLA 算子（`SparseSelectMlaV2` / `PureMlaV2`），通信模式完全相同（device 0 算稀疏选择并广播给其余卡）。`peer_bufs`（device 0 的通讯录）和 `ll_buf`（其余卡的接收缓冲）是通信用的 GPU 地址，MTP 头和主模型在同一个 8 卡进程内、同一组卡上运行，共享这些缓冲既安全又省显存（见 u2-l3、u2-l6）。

---

## 5. 综合实践

**综合任务**：把本讲四个视角（双阶段编排、draft 组装、接受回写、MTP 头结构）串起来，画出一张完整的 MTP 请求时序图并标注数据来源。

**操作步骤**：

1. **画 prefill 阶段时序**（基于 generator.py:297-333）：从 `cur_pos=0` 开始，对每个块标注「draft_tokens 来源（tokens 切片）→ padding → set_prefill_mtp_extra_token → set_prefill_valid_tokens → forward」。画到 `cur_pos` 到达 `prompt_len - 1`、`set_cur_pos` 调用为止。

2. **画 decode 阶段时序**（基于 generator.py:335-375）：标注每步的「draft 来源（第一步=末位 prompt token×4，其后=get_next_draft_tokens）→ forward → get_num_accepted/get_predicted_tokens → 回写 `cur_pos+1+i` → `cur_pos += num_accepted`」。

3. **标注后端读写**：在每个 `forward` 节点上，标出它写入的 temp_vars 槽位（`PREDICTED_TOKENS`、`ACCEPTED_TOKENS`、`NEXT_DRAFT_TOKENS`，见 end2end.py:617-651 与 temp_var_indices.py:52-56），以及 Python 侧如何用 `Idx` 读回。

4. **算一次真实请求**：假设 `prompt_len=20`、`max_new_tokens=100`、实测 `mean=2.77`。
   - prefill 块数：`ceil((20-1)/4)` = 5 块（最后一块 actual=3）。
   - decode forward 数：约 `100/2.77` ≈ 36 次。
   - 非 MTP 对照：decode 需 100 次 forward。
   - 在图上标出「省下的 forward 数 ≈ 64 次」。

5. **回溯到模型结构**：在图的 decode 节点旁注明，每次 forward 内部除了 61 层主模型，还多跑了 `MTP` 容器（`MTPPreprocessLayer` + `MoeBlock` + `RMSNormHeadProj`，mtp.py:27-50），并指出它读 `LAST_HIDDEN_STATES`（主模型末层隐状态）作为预处理输入。

**预期产出**：一张能向同事讲清「MTP 一次请求从头到尾发生了什么」的时序图，每个箭头都能对应到本讲引用的具体源码行号。若没有 GPU，步骤 4 的算术部分可独立完成并自检。

**待本地验证**：在真实硬件上运行一次 MTP 生成，把实测的 `decode_accepted_counts`、`mean`、`effective_tps` 标到你的图上，与估算对比。

## 6. 本讲小结

- **MTP 是一条可选的投机解码通道**，由 `with_mtp=True` 开启，把 `mtp_seq_len` 从 1 变 4，对调用方 API 完全透明（`generate` 签名不变）。
- **双阶段结构**：prefill 阶段把 prompt 切成长度 4 的块（不足则用末位 token padding，并用 `set_prefill_valid_tokens` / `set_prefill_mtp_extra_token` 告知后端）灌 KV 缓存；decode 阶段才做投机接受。
- **decode 的核心是 `cur_pos += num_accepted`**：每步用上一轮 `next_draft_tokens` 拼输入，forward 后读 `num_accepted` / `predicted_tokens`，把接受的 token 写到 `cur_pos+1+i` 并按接受数推进游标——这是 MTP 省 forward 的根本机制。
- **接受长度决定收益**：`mean=2.77` 意味着一次 forward 平均产出 2.77 个 token，生成 1000 token 约省 639 次 forward；收益正比于均值 \( \mu \)，上限是 `mtp_seq_len=4`。
- **MTP 头复用主模型算子**：`MTP` 容器 = `MTPPreprocessLayer`（融合 embedding + 主模型末层隐状态）+ 复用的 `MoeBlock` + 复用的 `RMSNormHeadProj`，挂在 `layer_61_` 前缀下，遵循与 61 层主模型相同的 `layer_{i}_{alias}_dev_{d}` 键名契约，并复用主模型的 V2 P2P 通信缓冲。
- **结果读取走 Idx 槽位**：`get_num_accepted` / `get_predicted_tokens` / `get_next_draft_tokens` 都是从 device 0 的 temp_vars 槽位（`ACCEPTED_TOKENS` / `PREDICTED_TOKENS` / `NEXT_DRAFT_TOKENS`）读结果，是 u2-l5 四元张量契约的具体应用。

## 7. 下一步学习建议

- **u3-l4（采样与 CUDA Graph 重捕获）**：本讲的采样种子由 `set_sampling_seed(with_mtp=True)` 设置，且 MTP 模式下 `reset_sequence` / `cleanup` 会调两遍（mtp=True 和 mtp=False 两个图）。下一讲讲采样参数变化如何触发 CUDA Graph 重捕获，正好解释 MTP 为何要维护两套图。
- **u4-l6（引擎接口与缓存注入）**：MTP 头需要主模型末层隐状态（`LAST_HIDDEN_STATES`），这正是 PD 分离时 `inject_last_hidden_state` 的用途。学完本讲再读 u4-l6，会理解为什么 prefill-decode 分离时 MTP 需要额外注入隐状态。
- **延伸阅读**：对照 [mtp_preprocess.py:197-229](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py#L197-L229) 的 `golden_forward` 与 DeepSeek-V3 原论文的 MTP 章节，理解「eh_proj 拼接 embedding 与隐状态」的设计动机；再阅读 [README.md:38](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L38) 与 [README.md:61](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L61) 的吞吐数据，把本讲的 \( \mu \)-based 收益模型与官方实测对照。
