# prefill 文本预填充流程

## 1. 本讲目标

本讲精读纯文本入口 `ncnn_llm_gpt::prefill(const std::string& input_text)`，把一段字符串变成模型生成的「第一个 token」。读完本讲你应该能够：

- 说清 prefill 的完整步骤与执行顺序：分词 → RoPE cache → embed → 因果 mask → decoder → proj_out → 首个 token。
- 解释为什么代码要 `token_ids.pop_back()` 把最后一个 token 单独拎出来跑。
- 看懂因果掩码（causal mask）是如何用 `-1e38f` 构造的，以及它在两段 decoder 调用里维度为什么不同。
- 把本讲和上一讲（u2-l2 共享文本运行时）的 ncnn 插槽约定（`in0/in1/in2/in3`、`cache_k%d`、`out_cache_k%d`）对应起来。

> 前置依赖：本讲默认你已学完 [u2-l2 ncnn 调用模式与共享文本运行时](u2-l2-ncnn-pattern-and-text-runtime.md)，熟悉 ncnn 的 `create_extractor`→`input`→`extract` 三步模式与 KV cache 的插槽命名约定。

## 2. 前置知识

### 2.1 什么是 prefill（预填充）

大语言模型生成文本是**自回归**的：每一步预测下一个 token。在开始预测之前，必须先把用户给的整段 prompt「读一遍」，让每一层 Transformer 都把这批 token 的 Key/Value 存进 KV cache。这个「读 prompt、填 cache」的阶段就叫 **prefill**（预填充）。

prefill 和后续的 generate（解码）有一个关键区别：

- **prefill**：一次性处理一整串 token（并行），主要产物是 KV cache。
- **generate**：每步只处理 1 个 token，复用并追加 KV cache。

### 2.2 因果性（causality）与掩码

Transformer 的自注意力会让每个位置看到所有位置。但语言模型只能「看到过去，不能看到未来」，否则就作弊了。解决办法是给注意力分数加一个**因果掩码**：把「未来位置」对应的分数压成一个极小的负数（本项目用 `-1e38f`，近似负无穷），经过 softmax 后权重就趋近于 0。

### 2.3 本讲会用到的几个量

| 符号 | 含义 |
|---|---|
| \(N\) | 分词后（含可能的 bos）的 token 总数 |
| `token_ids` | prefill 内部的 token id 序列；pop_back 之后长度为 \(N-1\) |
| `attn_cnt` | Transformer 层数（默认 32），决定 KV cache 的层数 |
| `rope_head_dim` | RoPE 作用在每个头上的维度（默认 64） |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 本讲主角，`prefill(input_text)` 的全部实现都在这里（第 248–436 行） |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 声明 `prefill` 的三个重载、`ncnn_llm_gpt_ctx` 上下文类、各成员变量（`attn_cnt`/`rope_head_dim`/`rope_type` 等） |
| [src/utils/rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) | RoPE cache 生成函数的声明（`generate_rope_embed_cache` 及长上下文变体） |
| [src/utils/tokenizer/bpe_tokenizer.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h) | `BpeTokenizer::encode` 的声明，负责把字符串变成 token id |

> 提醒：`ncnn_llm_gpt` 有三个 `prefill` 重载。本讲只讲**纯文本、无历史上下文**的那个，即 `prefill(const std::string& input_text)`，它是「第一轮对话」的入口。带图像的重载留给 u5，带历史 ctx 的多轮重载留给 u2-l5。

## 4. 核心概念与源码讲解

先看 prefill 的总流程，再逐模块拆。

```
input_text (字符串)
   │  ① bpe->encode + 可选 bos
   ▼
token_ids (长度 N)
   │  ② pop_back：抽出最后一个 token，留给第二段 decoder
   ▼
token_ids (长度 N-1) + last_token_id
   │  ③ 生成 RoPE cos/sin cache（按 rope_type 选变体）
   │  ④ embed_net：token_ids → token_embed
   │  ⑤ 构造 (N-1)×(N-1) 因果 mask
   │  ⑥ decoder 第一段：批量跑 N-1 个 token，【只】产出 KV cache
   ▼
kv_cache（N-1 行）
   │  ⑦ embed_net：last_token_id → last_token_embed
   │  ⑧ 生成 last token 的 RoPE（position_id = N-1）
   │  ⑨ decoder 第二段：单 token，喂入 kv_cache，产出 out0 + 更新后的 kv_cache（N 行）
   ▼
decode_out (1 行 hidden state)
   │  ⑩ proj_out_net：decode_out → logits
   │  ⑪ argmax(logits) → next_token_id
   ▼
ctx（含 kv_cache / cur_token / position_id）—— 交给 generate 续写
```

下面按这个顺序拆成 5 个最小模块。

### 4.1 分词：从文本到 token id 序列

#### 4.1.1 概念说明

模型不认识字符，只认识整数 id。第一步必须用分词器（这里固定用 `BpeTokenizer`）把字符串切成一串 token id。大多数因果语言模型还要求在最前面加一个**句首标记 bos**（beginning of sequence），告诉模型「这是一段话的开头」。

#### 4.1.2 核心流程

1. 调用 `bpe->encode(input_text, false, false)`，得到 `token_ids`（两个 `false` 表示不自动加 bos/eos，bos 由 prefill 自己控制）。
2. 若成员 `bos >= 0`，在序列头部插入 bos。
3. 取出并弹出最后一个 token（`pop_back`），保存为 `last_token_id`，留待第二段 decoder 单独处理。

#### 4.1.3 源码精读

[src/ncnn_llm_gpt.cpp:248-253](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L248-L253) —— 分词、加 bos、弹出最后一个 token：

```cpp
auto token_ids = bpe->encode(input_text, false, false);
if (bos >= 0) token_ids.insert(token_ids.begin(), bos);

int last_token_id = token_ids.back();
token_ids.pop_back();
```

- 第 249 行：`encode` 返回 `std::vector<int>`，两个 `false` 对应 `add_bos=false`、`add_eos=false`（见 [bpe_tokenizer.h:23-27](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h#L23-L27)）。
- 第 250 行：`bos` 是构造函数从 `model.json` 的 `tokenizer.bos` 读出的句首 id；为 -1 时不加。
- 第 252–253 行：`pop_back` 的原因详见 4.5.1，这里只需记住它把序列拆成「主体 N-1 个 + 最后 1 个」。

#### 4.1.4 代码实践

阅读上面的四行代码，回答：如果 `input_text` 分词后得到 7 个 token、且模型需要 bos，那么 `pop_back` 之后 `token_ids.size()` 是多少？`last_token_id` 又保存的是哪一个 token？

预期：`token_ids.size()` = 7（先加 bos 变 8，再 pop_back 变 7）；`last_token_id` 是原序列的最后一个（即第 8 个）token 的 id。

#### 4.1.5 小练习与答案

**练习 1**：为什么 prefill 自己用 `insert(begin, bos)` 加 bos，而不是直接给 `encode` 传 `add_bos=true`？

**参考答案**：因为是否加 bos 取决于构造函数读到的 `bos` 成员是否 `>= 0`（有些模型的 bos 配置为空、解析成 -1）。prefill 希望对「没有 bos」的模型也能正确工作，所以用 `if (bos >= 0)` 显式控制，而不是无条件让分词器加。

**练习 2**：`pop_back` 之后，`token_ids` 里还剩下「主体」部分。这部分后续会进入哪一段 decoder？`last_token_id` 又会进入哪一段？

**参考答案**：主体（N-1 个）进入第一段批量 decoder（4.5.2 的 Pass 1），只产出 KV cache；`last_token_id` 进入第二段单 token decoder（Pass 2），产出 hidden state 与更新后的 KV cache。

### 4.2 位置编码：生成 RoPE cos/sin cache

#### 4.2.1 概念说明

光有 token id 还不够——模型还需要知道每个 token 处在序列的**第几个位置**。ncnn_llm 用的是 **RoPE**（旋转位置编码，Rotary Position Embedding）：预先算好每个位置、每个维度的 `cos` 与 `sin` 两张表（cache），在 decoder 里用它们对 query/key 做旋转变换。

本讲的关注点是「**怎么生成这两张表并喂给 decoder**」；RoPE 的数学原理与四种变体（基础 / NTK / YaRN / LongRoPE）的深入对比留给 [u4-l1](u4-l1-rope-basics-and-variants.md)。

#### 4.2.2 核心流程

根据成员 `rope_type` 在四个生成函数里四选一：

| `rope_type` | 调用 | 用途 |
|---|---|---|
| `RoPE`（默认） | `generate_rope_embed_cache` | 基础 RoPE |
| `NTK_RoPE` | `generate_ntk_rope_embed_cache` | NTK-aware 缩放，扩上下文 |
| `YARN_RoPE` | `generate_yarn_rope_embed_cache` | YaRN 缩放 |
| `LongRoPE` | `generate_rope_embed_cache_LongRoPE` | LongRoPE（带 short/long factor） |

四个函数的公共签名是 `(seqlen, embed_dim, position_id, cos_cache, sin_cache, rope_theta, ...)`：生成 `seqlen` 个位置、从 `position_id` 起算的 cos/sin 表。

#### 4.2.3 源码精读

[src/ncnn_llm_gpt.cpp:255-266](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L255-L266) —— 为主体 N-1 个 token 生成 RoPE cache（`position_id` 起点为 0）：

```cpp
ncnn::Mat cos_cache, sin_cache;
if (rope_type == RoPE_Type::LongRoPE) {
    generate_rope_embed_cache_LongRoPE(token_ids.size(), rope_head_dim, 0, cos_cache, sin_cache, rope_theta, short_factor.data(), long_factor.data(), original_max_position_embeddings);
} else if (rope_type == RoPE_Type::NTK_RoPE) {
    generate_ntk_rope_embed_cache(token_ids.size(), rope_head_dim, 0, cos_cache, sin_cache, rope_theta, ntk_scaling_params);
} else if (rope_type == RoPE_Type::YARN_RoPE) {
    generate_yarn_rope_embed_cache(token_ids.size(), rope_head_dim, 0, cos_cache, sin_cache, rope_theta, ntk_scaling_params);
}
else
{
    generate_rope_embed_cache(token_ids.size(), rope_head_dim, 0, cos_cache, sin_cache, rope_theta);
}
```

- 第一参数 `token_ids.size()` = \(N-1\)（主体长度），即要生成多少个位置。
- 第三参数 `0`：主体从位置 0 开始排，所以 RoPE 的起始 position 是 0。
- 基础版 `generate_rope_embed_cache` 的签名见 [rope_embed.h:35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L35)。

> 注意：第 4.5 节会看到，最后一个 token 还会**单独再生成一次** RoPE，起点是 `position_id = N-1`。

#### 4.2.4 代码实践

在 [src/ncnn_llm_gpt.cpp:255-266](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L255-L266) 这段代码里，把四个分支的「第一参数」「第三参数」分别抄下来，列成一张表。预期你会看到主体段是 `(N-1, position=0)`，再对比 4.5 节里 last token 段是 `(1, position=N-1)`，体会「两段拼接出完整 0..N-1 位置」。

#### 4.2.5 小练习与答案

**练习 1**：如果某模型的 `rope_type` 是默认的 `RoPE`，但 `rope_theta` 被设成了 `1000000.0f`，上面这段代码会走到哪个分支？`rope_theta` 是怎么传进去的？

**参考答案**：走到最后的 `else` 分支，调用 `generate_rope_embed_cache(...)`；`rope_theta` 作为最后一个参数传入，控制旋转的基频（theta 越大，相同位置的角度变化越平缓，等价于「看得更远」）。

**练习 2**：为什么主体段的 `position_id` 起点是 0，而不是 1？

**参考答案**：因为这是首轮 prefill、没有历史上下文，序列的第一个 token 就在绝对位置 0。带历史 ctx 的多轮 prefill 会从 `ctx->position_id` 起算（见 u2-l5）。

### 4.3 嵌入：token id → token_embed

#### 4.3.1 概念说明

token id 只是个整数，模型需要把它变成一个浮点向量（embedding）才能送进 Transformer。这一步由 **embed 网络**（`embed_net`，对应 `model.json` 里的 `embed_token_*` 子网）完成：输入一串 id，输出一个形状为 `(hidden_size, seq_len)` 的矩阵，每一列是一个 token 的向量。

> 这一步本质上就是查表（embedding lookup），但 ncnn 把它包成了一个独立的 `.param/.bin` 子网，所以调用方式和普通网络一样。

#### 4.3.2 核心流程

1. 把 `vector<int>` 包装成 `ncnn::Mat`（一维，长度 = seq_len），并 `.clone()` 一份，保证内存独立。
2. 对 `embed_net` 走 ncnn 三步：`create_extractor` → `input("in0", ...)` → `extract("out0", token_embed)`。

这正是 [u2-l2](u2-l2-ncnn-pattern-and-text-runtime.md) 讲过的 ncnn 调用模式与 `in0/out0` 插槽约定。

#### 4.3.3 源码精读

[src/ncnn_llm_gpt.cpp:268-274](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L268-L274) —— 主体段的嵌入提取：

```cpp
ncnn::Mat input_ids_mat = ncnn::Mat((int)token_ids.size(), 1, (void*)token_ids.data()).clone();
ncnn::Mat token_embed;
{
    ncnn::Extractor ex = embed_net->create_extractor();
    ex.input("in0", input_ids_mat);
    ex.extract("out0", token_embed);
}
```

- 第 268 行：`ncnn::Mat(w, h, data)` 把 vector 的裸指针包成 Mat；`.clone()` 复制一份，避免后续 decoder 误改原始 vector 内存（这是 ncnn 外部指针用法的常见安全姿势，u2-l2 也强调过）。
- 第 271–273 行：标准三步调用，输入插槽 `"in0"`，输出插槽 `"out0"`。

#### 4.3.4 代码实践

阅读这段代码后，对照 [src/ncnn_llm_gpt.cpp:324-330](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L324-L330) 里对 `last_token_id` 做的几乎一模一样的嵌入提取（只是序列长度变成 1）。说清楚两段调用的「输入 Mat 长度」分别是多少。

预期：主体段长度 = \(N-1\)；last token 段长度 = 1。

#### 4.3.5 小练习与答案

**练习 1**：第 268 行为什么要 `.clone()`？如果不 clone 会怎样？

**参考答案**：`ncnn::Mat` 用外部指针构造时不拥有数据。`.clone()` 复制出一份独立内存，确保 embed 网络内部即使有重排/改写也不会污染原始 `token_ids`，也保证 Mat 生命周期独立于 vector。

**练习 2**：`embed_net` 对应 `model.json` 里哪几个字段？（提示：回顾 u1-l5）

**参考答案**：对应 `params.embed_token_param` 与 `params.embed_token_bin`（真实键名带 `_token`，README 里的 `embed_*` 已过时，以源码为准）。

### 4.4 因果掩码：构造 attention mask

#### 4.4.1 概念说明

主体段是一次性把 \(N-1\) 个 token 一起送进 decoder 的，必须用因果掩码防止「后面的 token 偷看前面的答案」。本项目用一个 \((N-1)\times(N-1)\) 的矩阵表示掩码：第 \(i\) 行代表「第 \(i\) 个 token 作为 query」，第 \(j\) 列代表「第 \(j\) 个 token 作为 key」。

- 允许注意：`row[j] = 0`（分数不变）。
- 禁止注意（未来）：`row[j] = -1e38f`（分数被压成近似负无穷，softmax 后权重 ≈ 0）。

#### 4.4.2 核心流程

构造一个上三角为 `-1e38f`、其余为 `0` 的方阵：

\[
\text{mask}_{i,j} =
\begin{cases}
0, & j \le i \\
-10^{38}, & j > i
\end{cases}
\]

直观长这样（✓ = 0 可注意，✗ = -1e38 屏蔽）：

```
      j=0  j=1  j=2  j=3
i=0    ✓    ✗    ✗    ✗
i=1    ✓    ✓    ✗    ✗
i=2    ✓    ✓    ✓    ✗
i=3    ✓    ✓    ✓    ✓
```

#### 4.4.3 源码精读

[src/ncnn_llm_gpt.cpp:276-283](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L276-L283) —— 主体段的因果掩码：

```cpp
ncnn::Mat mask((int)token_ids.size(), (int)token_ids.size());
mask.fill(0.0f);
for (int i = 0; i < (int)token_ids.size(); i++) {
    float* row = mask.row(i);
    for (int j = i + 1; j < (int)token_ids.size(); j++) {
        row[j] = -1e38f;
    }
}
```

- 第 276 行：`ncnn::Mat(w, h)` 两维都等于 \(N-1\)，即 \((N-1)\times(N-1)\) 方阵。
- 第 277 行：先全填 0（全部允许）。
- 第 278–282 行：对每一行 \(i\)，把列 `j > i`（严格未来）改成 `-1e38f`。`mask.row(i)` 返回第 \(i\) 行的首地址。

> **对比 last token 的 mask**：[src/ncnn_llm_gpt.cpp:344-345](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L344-L345) 里 last token 的掩码是 `((N-1)+1) x 1`、全 0。因为最后一个 token 是序列里**最靠后**的，它没有「未来」可屏蔽，所以可以大大方方地注意所有历史位置（含自己）。维度从方阵退化成一列向量，正是因为它只有一个 query。

#### 4.4.4 代码实践

手算一个最小例子：设 \(N-1 = 4\)，按上面双重循环写出最终的 \(4\times4\) mask 矩阵，并标注哪几个元素是 `-1e38f`。预期：上三角（不含对角线）共 6 个元素是 `-1e38f`，其余 10 个是 0。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `-1e38f` 而不是负无穷或一个更「圆」的数（如 `-1e9`）？

**参考答案**：`-1e38f` 接近 float 能表示的大负数，softmax（指数化）后权重会变成严格的 0，足以屏蔽未来位置；同时又不会溢出成 NaN。比 `-1e9` 更「狠」，能避免在 fp16/低精度下屏蔽不干净。

**练习 2**：last token 的 mask 维度是 `((N-1)+1) x 1`。这里的 `+1` 代表什么？

**参考答案**：last token 要注意的对象 = N-1 个历史 key（来自主体段的 KV cache）+ 它自己这 1 个新 key，共 N 个位置，所以长度是 `(N-1)+1 = N`。全 0 表示这 N 个位置全允许注意。

### 4.5 解码与投影：decoder_net 两段式 + proj_out_net

这是 prefill 最核心、也最长的一段。它分两个子模块讲：**4.5.A 主体批量解码（Pass 1）** 与 **4.5.B 末位 token 解码 + 投影 + 取首 token（Pass 2）**。

#### 4.5.1 概念说明：为什么要两段式（pop_back 的真正原因）

先把全讲最关键的问题讲透：**为什么要把最后一个 token 弹出来单独跑？**

答案在两段 decoder **取的东西不同**：

- **Pass 1（主体 N-1 个 token）**：批量、并行地把这批 token 推过 decoder，但**只取 KV cache，根本不取 `out0`**（不取 hidden state）。这批 token 的唯一职责是「贡献自己的 Key/Value 给缓存」，它们预测出的 hidden state 没人需要，直接丢弃。
- **Pass 2（最后一个 token）**：把末位 token 单独喂进 decoder，同时把 Pass 1 产出的 KV cache 作为输入（`cache_k%d/cache_v%d`）喂回去。因为只有 1 个 query token，decoder 的 `out0` 天然就是**一行** hidden state，直接喂给 `proj_out_net` 就能得到 logits，再 argmax 就是模型生成的第一个 token。同时它会把末位 token 自己的 K/V 追加进 cache，让 cache 从 \(N-1\) 行变成完整的 \(N\) 行。

换句话说，**pop_back 是为了让「预测下一个 token」这件事复用和 generate 完全相同的单 token 解码形状**——不需要从一坨多行 hidden state 里去切最后一行。代价是最后这个 token 要多跑一次 decoder，但它只有一个 token，开销很小。

> 这种「主体批量填 cache + 末位单步出 logits」是本项目的刻意选择。上一讲（u2-l2）提到的共享函数 `llm_run_lm_head` 在 prefill 模式下是用 `row_range` 切最后一行来达到同样目的——思路不同，效果一致。

#### 4.5.2 核心流程

**Pass 1（主体批量解码，产出 KV cache）**：

1. 新建 decoder Extractor，依次 `input`：`in0=token_embed`、`in1=mask`、`in2=cos_cache`、`in3=sin_cache`。
2. 对每一层 `i`（共 `attn_cnt` 层）：`extract("out_cache_k%d"/"out_cache_v%d")`，把 K/V 追加进 `kv_cache`。
3. （可选）若模型有 `sconv_cnt`/`gdr_cnt`（Qwen3.5 混合架构），额外抽取 `out_cache_conv%d`/`out_cache_gdr%d`。
4. **不**取 `out0`。

**Pass 2（末位 token 解码，产出 hidden state 与更新后的 KV cache）**：

1. 对末位 token 做嵌入（4.3 已讲）。
2. 生成它的 RoPE cache，`position_id = N-1`（[src/ncnn_llm_gpt.cpp:332-342](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L332-L342)）。
3. 构造 last mask（4.4 已讲）。
4. 新建 decoder Extractor：`in0/in1/in2/in3` 同上；**额外**把 Pass 1 的 `kv_cache` 按层 `input("cache_k%d"/"cache_v%d")` 喂回去。
5. 再次 `extract("out_cache_k%d"/"out_cache_v%d")`，**覆盖** `kv_cache[i]`（此时 cache 含末位 token，变成 \(N\) 行）。
6. 这次**取 `out0`** → `decode_out`（单行 hidden state）。

**投影与取首 token**：

7. `proj_out_net`：`in0=decode_out` → `out0=logits`（词表大小的一维分数）。
8. 手写 argmax：遍历 logits 取最大值下标 → `next_token_id`。
9. 用 `create_ctx` 建上下文，把 `kv_cache`、`cur_token=next_token_id`、`position_id=N` 装进去；若有 sconv/gdr，再装进 `qwen3_5_ctx`。返回 ctx。

#### 4.5.3 源码精读

**Pass 1：批量 decoder，只取 KV cache** —— [src/ncnn_llm_gpt.cpp:285-321](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L285-L321)：

```cpp
std::vector<std::pair<ncnn::Mat, ncnn::Mat>> kv_cache;
...
ncnn::Extractor ex = decoder_net->create_extractor();
ex.input("in0", token_embed);
ex.input("in1", mask);
ex.input("in2", cos_cache);
ex.input("in3", sin_cache);

for (int i = 0; i < attn_cnt; i++) {
    char name_k_out[32], name_v_out[32];
    std::snprintf(name_k_out, sizeof(name_k_out), "out_cache_k%d", i);
    std::snprintf(name_v_out, sizeof(name_v_out), "out_cache_v%d", i);
    ncnn::Mat k_cache, v_cache;
    ex.extract(name_k_out, k_cache);
    ex.extract(name_v_out, v_cache);
    kv_cache.emplace_back(std::move(k_cache), std::move(v_cache));
}
// ... 同样抽取 sconv_cache / gdr_cache（若有）
```

注意这段里**没有** `ex.extract("out0", ...)`——主体段的 hidden state 被刻意丢弃。插槽名 `out_cache_k%d`/`out_cache_v%d` 正是 u2-l2 讲过的「与导出脚本的隐式契约」。

**Pass 2：单 token decoder，喂入并更新 KV cache，取出 out0** —— [src/ncnn_llm_gpt.cpp:347-401](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L347-L401)：

```cpp
ncnn::Extractor ex = decoder_net->create_extractor();
ex.input("in0", last_token_embed);
ex.input("in1", last_mask);
ex.input("in2", last_cos_cache);
ex.input("in3", last_sin_cache);

for (int i = 0; i < attn_cnt; i++) {            // 喂入 Pass 1 的 KV cache
    ...
    ex.input(name_k_in, kv_cache[i].first);      // "cache_k%d"
    ex.input(name_v_in, kv_cache[i].second);     // "cache_v%d"
}
...
for (int i = 0; i < attn_cnt; i++) {            // 取回更新后的 KV cache（已含末位 token）
    ...
    ex.extract(name_k_out, k_cache);             // "out_cache_k%d"
    ex.extract(name_v_out, v_cache);
    kv_cache[i] = std::make_pair(std::move(k_cache), std::move(v_cache));
}
...
ex.extract("out0", decode_out);                  // 这次取 hidden state
```

对比 Pass 1：Pass 2 多了「`input(cache_k%d/cache_v%d)`」这一组喂入，也多了 `extract("out0")`。这正是「读旧 cache → 写新 cache → 出 hidden」的 decode 形状，和 [generate 循环](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L892-L970)里每一步的形状完全一致。

> **与共享运行时的关系**：注意 prefill 这里是**直接内联** ncnn 调用，没有走 u2-l2 的 `llm_run_text_embed`/`llm_run_decoder_with_kv`/`llm_run_lm_head` 包装；但用的是**完全相同的插槽约定**。而 `generate`（[src/ncnn_llm_gpt.cpp:892-979](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L892-L979)）在非 Qwen3.5 路径上则直接调用了这些共享函数。两者风格略异，但底层契约统一。

**投影 + argmax + 建 ctx** —— [src/ncnn_llm_gpt.cpp:403-435](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L403-L435)：

```cpp
ncnn::Mat logits;
{
    ncnn::Extractor ex = proj_out_net->create_extractor();
    ex.input("in0", decode_out);
    ex.extract("out0", logits);
}

int next_token_id = 0;                           // 手写 argmax（贪心）
{
    const float* p = logits;
    float max_val = p[0];
    for (int i = 1; i < logits.w; ++i) {
        if (p[i] > max_val) { max_val = p[i]; next_token_id = i; }
    }
}

auto ctx = create_ctx(sconv_cnt, gdr_cnt);
ctx->kv_cache = std::move(kv_cache);
ctx->cur_token = next_token_id;
ctx->position_id = (int)token_ids.size() + 1;    // = (N-1)+1 = N
```

- `proj_out_net` 对应 `model.json` 的 `proj_out_*` 子网（u1-l5）。
- prefill 末尾的 argmax 是**纯贪心**（不采样），只挑分数最高的那个 token 作为起点；真正的采样（temperature/top_k/top_p）发生在 `generate` 里（u2-l4/u3-l4）。
- `create_ctx`（[src/ncnn_llm_gpt.cpp:9-14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14)）按是否有 sconv/gdr 决定返回 `qwen3_5_ctx` 还是 `ncnn_llm_gpt_base_ctx`。
- `position_id = token_ids.size() + 1`：主体 pop 后是 \(N-1\)，所以下一个待生成 token 的位置是 \(N\)，留给 generate 续写。

#### 4.5.4 代码实践

这是本讲的**主线实践**：跟踪一次 `prefill(input_text)` 调用，在源码里给六个阶段标注行号，并亲手验证 pop_back 的理由。

1. **实践目标**：把「tokenize → rope → embed → mask → decoder → proj_out」六步精确对应到代码行，并能向别人解释 pop_back。
2. **操作步骤**：
   - 打开 [src/ncnn_llm_gpt.cpp:248-436](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L248-L436)。
   - 在笔记里画一张表，左列写六步，右列填对应行号（参考答案见下）。
   - 重点圈出两处 `ex.extract(...)` 的差异：Pass 1（约 296–320 行）**没有** `extract("out0")`，Pass 2（约 400 行）**有** `extract("out0", decode_out)`。
   - 可选加强：在 Pass 1 之后临时加一行打印（仅本地调试，勿提交），例如 `fprintf(stderr, "[pass1] kv layers=%d cache_rows=%d\n", attn_cnt, kv_cache[0].first.h);`，再在 Pass 2 之后同样打印 `kv_cache[0].first.h`。
3. **需要观察的现象**：若你加了打印，应看到 Pass 1 后 cache 行数 = \(N-1\)，Pass 2 后变成 \(N\)（末位 token 被追加）。
4. **预期结果（六步行号参考）**：
   - tokenize：249–253
   - rope：255–266
   - embed（主体）：268–274
   - mask：276–283
   - decoder（Pass 1 取 cache）：285–321；decoder（Pass 2 出 out0）：347–401
   - proj_out + argmax + ctx：403–435
5. **pop_back 的解释（用你自己的话写一遍）**：要点是「主体批量段只产 KV cache 不取 hidden；末位 token 单独走一遍 decode，天然得到单行 hidden 喂给 lm_head，同时把 cache 补齐到 N 行」。
6. 若无法本地运行（缺模型权重），明确标注「待本地验证」，仅完成源码阅读与行号标注即可。

#### 4.5.5 小练习与答案

**练习 1**：Pass 1 的 decoder 调用为什么**不**取 `out0`？取了会怎样？

**参考答案**：主体段的 hidden state 没有任何下游需要——prefill 只关心它贡献的 KV cache 和「末位 token 的预测」。即使取了 `out0`，也会得到 \(N-1\) 行 hidden state，还得切片取最后一行才能喂 lm_head，多余且浪费。本项目干脆不取，把出 logits 的任务交给 Pass 2 的单 token 调用。

**练习 2**：经过两段 decoder 之后，`kv_cache` 一共有多少「行」？`ctx->position_id` 是多少？

**参考答案**：Pass 1 产生 \(N-1\) 行，Pass 2 追加 1 行，共 \(N\) 行。`ctx->position_id = token_ids.size() + 1 = (N-1)+1 = N`，即下一个生成 token 将位于位置 \(N\)。

**练习 3**：prefill 末尾用 argmax（贪心）选 `next_token_id`，但 `generate` 里却用 `llm_select_next_token`（带温度/采样）。为什么 prefill 要用贪心？

**参考答案**：prefill 只产出「第一个」token 作为生成起点，是否采样对整体影响很小；用贪心最简单、确定性强。真正的多样性采样交给 generate 的每一步。这是一种实现上的简化，并非硬性规定。

## 5. 综合实践

把本讲全部知识串起来，做一个「**手动预测 prefill 结果**」的纸面实验：

**任务**：假设某模型 `attn_cnt=4`、`rope_head_dim=64`、`rope_type=RoPE`（默认）、需要 bos，你输入一句分词后得到 5 个 token 的 prompt。

1. 写出 `bpe->encode` 之后、`pop_back` 之后 `token_ids` 的长度。
2. 算出主体段 RoPE cache 的 `seqlen` 与 `position_id`，以及 last token 段 RoPE 的 `seqlen` 与 `position_id`。
3. 写出主体段 mask 的形状，以及 last token mask 的形状。
4. 推算 Pass 1 之后 `kv_cache` 的层数与每层 cache 的行数；Pass 2 之后又是多少。
5. 写出返回 ctx 的 `cur_token` 由哪一步决定、`position_id` 等于多少。

**参考答案**：
1. `encode` 后加 bos 共 6 个；`pop_back` 后 `token_ids.size() = 5`，`last_token_id` = 原第 6 个。
2. 主体段：`seqlen=5, position_id=0`；last token 段：`seqlen=1, position_id=5`。
3. 主体 mask：\(5\times5\) 方阵（上三角 `-1e38f`）；last mask：\((5+1)\times1 = 6\times1\) 全 0。
4. Pass 1 后：4 层（= `attn_cnt`），每层 cache 5 行；Pass 2 后：仍 4 层，每层 6 行。
5. `cur_token` = `argmax(proj_out(decode_out))`（Pass 2 的 `out0` 经投影后取最大值下标）；`position_id = 5 + 1 = 6`。

完成后再去源码里逐行对一遍 [src/ncnn_llm_gpt.cpp:248-435](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L248-L435)，确认你的推算和代码一致。

## 6. 本讲小结

- prefill = 「读 prompt、填 KV cache、给出第一个 token」，与 generate 的「逐 token 续写」相对。
- 六步主线：`bpe->encode` → 生成 RoPE cache → `embed_net` 取 `token_embed` → 构造因果 mask → 两段 `decoder_net` → `proj_out_net` + argmax。
- **pop_back 的本质**：主体批量段只取 KV cache（不取 `out0`），末位 token 单独走一遍单 token decode，天然得到单行 hidden state 喂给 lm_head，同时把 cache 补齐到完整 \(N\) 行——这和 generate 每一步的形状完全一致。
- 因果 mask 用 `-1e38f` 屏蔽未来；主体是 \((N-1)^2\) 方阵，末位 token 退化为全 0 的列向量。
- prefill 直接内联 ncnn 调用，但严格遵守 u2-l2 的插槽约定（`in0..in3`、`cache_k%d`、`out_cache_k%d`）；`generate` 则改用共享函数，两者底层契约统一。
- 返回的 `ctx` 携带 `kv_cache`、`cur_token`、`position_id`（= \(N\)），交给 generate 续写；带 sconv/gdr 的 Qwen3.5 模型还会额外携带两类 cache。

## 7. 下一步学习建议

- 紧接本讲应学 [u2-l4 generate 自回归解码主循环](u2-l4-generate-loop.md)：看 `cur_token` 如何在每一步被 embed → decoder → lm_head → 采样推进，并理解 KV cache 在 decode 阶段的更新方式与本讲 Pass 2 的呼应。
- 想深入 RoPE 的数学与四种长上下文变体，读 [u4-l1 RoPE 基础与长上下文变体](u4-l1-rope-basics-and-variants.md) 与 [src/utils/rope_embed.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp)。
- 想理解多轮对话里 prefill 如何从 `ctx->position_id` 续接历史 KV cache，预习 [u2-l5 推理上下文 ctx 与多轮对话](u2-l5-context-and-multiturn.md)，对照本讲首轮 `position_id=0` 的设定。
- 建议同时打开 [src/ncnn_text_runtime.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp) 的 `llm_run_decoder_with_kv`，对比「共享函数版」与「本讲内联版」在 `is_prefill` 分支上的写法差异。
