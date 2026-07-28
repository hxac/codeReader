# generate 自回归解码主循环

## 1. 本讲目标

上一讲（u2-l3）我们跟着 `prefill` 把一段提示词「一次喂进去」，得到了模型生成的**第一个 token**。但真正的对话是一句一句往外蹦的——每生成一个词，就把它接回输入再生成下一个，如此循环。这个「自己回归到自己」的循环就是 **自回归（autoregressive）解码**，由 `ncnn_llm_gpt::generate` 负责。

学完本讲你应当能够：

1. 画出 `generate` 自回归主循环里**每一步的数据流**（embed → decoder → lm_head → 采样）。
2. 说清楚 **KV cache 在 decode 阶段如何被「读旧值、写新值」原地更新**，以及它和 prefill 阶段（u2-l3）的 `emplace_back` 追加有何不同。
3. 区分 **贪心采样** 与 **temperature / top_k / top_p** 三种采样策略，并解释 `repetition_penalty`（重复惩罚）的「负乘正除」逻辑。
4. 知道循环如何因 `eos`（结束符）停止，以及 `tool_call` 如何把生成过程**分流**去执行工具调用。

本讲是 u2-l2（共享文本运行时）和 u2-l3（prefill）的直接延续，把「共享 decoder + KV cache」这条设计主线推到生成阶段。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么叫「自回归」

「回归（regressive）」在这里不是统计学的回归，而是「回头依赖自己」。语言模型本质上是一个**条件概率**预测器：给定已经出现的一串 token \(x_0, x_1, \dots, x_{t-1}\)，预测下一个 token \(x_t\) 的分布

\[
P(x_t \mid x_0, x_1, \dots, x_{t-1})
\]

生成时，我们把预测出的 \(x_t\) 接到序列末尾，再预测 \(x_{t+1}\)，循环往复。每一步都「回头」用到之前所有 token，所以叫自回归。

### 2.2 为什么要 KV cache

Transformer 的注意力层在计算当前 token 时，要去看「之前所有 token」的 Key 和 Value。如果每生成一个新 token 都把整段历史重新算一遍，复杂度是 \(O(N^2)\)。**KV cache** 把每一层、每个历史 token 算出的 K、V 存起来，新生成一个 token 时只需算它**自己**的 K、V，再和缓存里的拼一起做注意力——每步降到 \(O(N)\)。这就是 prefill 之后，generate 能一个一个 token 高效吐出来的关键。

在 ncnn_llm 里，`KVCache`（定义于基类头，u2-l1 讲过）就是 `vector<pair<ncnn::Mat, ncnn::Mat>>`，长度等于 Transformer 层数，每层一对 K/V 张量。

### 2.3 prefill vs generate 的分工

| | prefill（u2-l3） | generate（本讲） |
|---|---|---|
| 一次处理 | 一整串 prompt（N 个 token） | 每次只 1 个 token |
| KV cache 更新 | `emplace_back` 逐层**追加** | 原地**覆盖**（写回更长的新 cache） |
| 位置编码 | 一次生成 N 个位置的 cos/sin | 每步只生成 1 个位置 |
| 掩码 mask | 因果方阵（用 `-1e38f` 屏蔽未来） | 全 0 向量（最后一个 token 天然只看过去） |
| 取输出 | 只取 KV cache，末位再取 hidden state | 取 hidden state → logits → 采样 |

记住这张对比表，本讲的许多细节都是在和 prefill 做对照。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `GenerateConfig` 配置结构体、`generate` 声明、特殊 token 成员（eos/tool_call/think 的 id） |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | `generate` 函数实现：自回归主循环、tool_call 分流、调用共享运行时 |
| [src/ncnn_text_runtime.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp) | 共享文本运行时：`llm_run_text_embed` / `llm_run_decoder_with_kv` / `llm_run_lm_head` / `llm_select_next_token` |
| [src/sampling.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp) | 采样原语：`softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs` |
| [examples/llm_ncnn_run/cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | 命令行示例如何构造 `GenerateConfig` 并调用 `generate` |

## 4. 核心概念与源码讲解

### 4.1 自回归循环主结构

#### 4.1.1 概念说明

`generate` 接收三个输入：prefill 产出的上下文 `ctx_in`（里面带着 KV cache、当前 token、位置 id）、采样配置 `cfg`、以及一个流式回调 `callback`（每生成一段文本就调用它，把文字「打印」出去）。

它的核心是一个 `for` 循环，每轮做一件事：**把上一步算出的 token 拿来用，再算出下一步的 token**。注意这里有一个「错位」——循环里首先处理的是 `ctx->cur_token`（**上一步**已经决定的 token），处理完（解码成文字或累积成工具调用）之后，才去计算**下一个** token。

#### 4.1.2 核心流程

下面是主循环的伪代码，省略采样细节与 tool_call 分流，只保留主干：

```text
generate(ctx_in, cfg, callback):
    ctx = clone_ctx(ctx_in)            # 克隆上下文，不污染调用方
    history = { ctx.cur_token }        # 记录已出现 token，供重复惩罚用
    for step in 0 .. cfg.max_new_tokens:
        if ctx.cur_token == eos: break              # ① 遇结束符则停
        callback(decode(ctx.cur_token))             # ② 把当前 token 解码成文字输出
        embed  = embed_net(ctx.cur_token)           # ③ 查表得到嵌入
        cos,sin = rope_cache(position_id)           # ④ 当前位置的 RoPE
        position_id += 1
        mask = zeros(cache_len + 1)                 # ⑤ decode 掩码：全 0
        hidden = decoder(embed, mask, cos, sin,     # ⑥ decoder，KV cache 原地更新
                          kv_cache, is_prefill=false)
        logits = proj_out(hidden)                   # ⑦ 投影成词表 logits
        next_id = select_next_token(logits, history, cfg)  # ⑧ 采样
        ctx.cur_token = next_id                      # ⑨ 作为下一轮的「当前 token」
        history.insert(next_id)
    return ctx
```

#### 4.1.3 源码精读

先看函数签名与循环骨架，位于 [src/ncnn_llm_gpt.cpp:843-875](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L843-L875)。这段定义了入参与 `for (int step = 0; step < cfg.max_new_tokens; ++step)` 主循环，并在循环开头用 `if (ctx->cur_token == eos) break;` 做结束判定。

`GenerateConfig` 是控制生成行为的「旋钮集合」，定义在 [src/ncnn_llm_gpt.h:32-43](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L32-L43)：

```cpp
struct GenerateConfig {
    int   max_new_tokens = 4096;   // 最多生成多少个 token
    float temperature = 0.3f;       // 温度：越大越随机
    float top_p = 0.8f;             // 核采样保留概率质量
    int   top_k = 50;               // 只在概率最高的 K 个里挑
    float repetition_penalty = 1.1f;// 重复惩罚，>1 抑制重复
    int   do_sample = 1;            // 1=采样，0/其它=贪心 argmax
    std::function<json(const json&)> tool_callback = nullptr; // 工具回调
    bool debug = false;
};
```

注意三个要点：

- `max_new_tokens` 默认 4096，这是**真正限制生成长度**的字段（u1-l4 提到命令行的 `max_tokens` 与它是两回事）。
- `do_sample` 是 `int` 类型，下游判断用的是 `cfg.do_sample != 1`（见 4.3），所以传 `0` 或 `false` 都会走贪心。
- `tool_callback` 默认为空，只有调用方（如 cli_runner）注入了回调，工具调用才会被真正执行。

克隆上下文用 `clone_ctx`，它只是转发给 `ctx->clone()`，见 [src/ncnn_llm_gpt.cpp:5-7](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L5-L7)。`clone()` 在 [src/ncnn_llm_gpt.h:56-89](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L56-L89) 里深拷贝 KV cache、`cur_token`、`position_id`（Qwen3.5 还多拷 `sconv_cache`/`gdr_cache`）。克隆保证 `generate` 改的是副本，**不会破坏调用方手里的原始上下文**——这对多轮对话（u2-l5）至关重要。

#### 4.1.4 代码实践

**实践目标**：通过修改循环上限，直观感受 `max_new_tokens` 的作用。

**操作步骤**：

1. 打开 [examples/llm_ncnn_run/cli_runner.cpp:71-110](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L71-L110)，找到构造 `GenerateConfig cfg;` 的地方。
2. 在 `cfg.do_sample = false;` 之后加一行 `cfg.max_new_tokens = 20;`（强行只生成 20 个 token）。
3. 重新构建并运行：`xmake build llm_ncnn_run && xmake run llm_ncnn_run assets/<你的模型目录>`。

**需要观察的现象**：模型每次回答都很快「戛然而止」，输出明显变短。

**预期结果**：因为循环最多跑 20 步，且每步通常对应一个（或几个）字/子词，回答会在约 20 个 token 处被截断。这验证了 `max_new_tokens` 是「步数上限」，而非「字数上限」。

> 若本地未配置模型权重，可改为阅读型实践：在 `generate` 的 `for` 头（[src/ncnn_llm_gpt.cpp:874](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L874)）确认 `step < cfg.max_new_tokens` 是唯一的上界，循环内部没有其它提前 `return`（除非遇到 `eos` 或工具调用后 `continue`）。**待本地验证**具体输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate` 第一步处理的是 `ctx->cur_token`，而不是先算一个新 token？

> **答**：因为 prefill 已经算出了第一个 token 并存进 `ctx->cur_token`（见 u2-l3 末尾）。`generate` 接手后要先把这个「现成」的 token 输出给用户，再用它去预测下一个。这是一种「错位一步」的写法：循环体上半段消费上一步的结果，下半段生产下一步的结果。

**练习 2**：把 `GenerateConfig::do_sample` 设为 `0`，生成结果会有什么特点？

> **答**：下游 `llm_select_next_token` 里 `cfg.do_sample != 1` 成立，会直接返回 `argmax`（贪心）。此时 `temperature/top_k/top_p` 全部失效，每次给相同 prompt 都会得到**完全相同**的输出。

---

### 4.2 KV cache 在 decode 阶段的更新

#### 4.2.1 概念说明

本模块回答两个问题：(1) 每一步 decoder 如何「读」旧 cache、「写」新 cache；(2) 它和 prefill 阶段的更新方式为什么不同。

回忆 u2-l2 讲过的 ncnn 插槽契约：decoder 网络的输入插槽叫 `cache_k%d` / `cache_v%d`（按层号 `i` 拼名），输出插槽叫 `out_cache_k%d` / `out_cache_v%d`。`%d` 是 Transformer 层号，从 0 到 `attn_cnt - 1`。

#### 4.2.2 核心流程

decode 阶段（`is_prefill=false`）的 KV cache 流转：

```text
输入: kv_cache[i]  (缓存到当前为止的所有历史 K/V)
     └─ 作为 cache_k{i}/cache_v{i} 喂给 decoder
decoder 把新 token 的 K/V 追加到末尾，得到 out_cache_k{i}/out_cache_v{i}
输出: kv_cache[i] = out_cache_k{i}/out_cache_v{i}   # 原地覆盖，长度 +1
```

关键区别：prefill 用 `kv_cache.emplace_back(...)`（**追加**一对新层），decode 用 `kv_cache[i] = ...`（**原地覆盖**第 i 层、但内容多了一行）。这是因为 prefill 一开始 cache 是空的、要一层一层填进去；decode 时 cache 已经有 `attn_cnt` 层了，每步只是把每层的内容「延长一行」。

#### 4.2.3 源码精读

掩码构造见 [src/ncnn_llm_gpt.cpp:908-909](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L908-L909)：

```cpp
ncnn::Mat mask(ctx->kv_cache[0].first.h + 1, 1);
mask.fill(0.f);
```

这里 `kv_cache[0].first.h` 是第 0 层 K 张量的「行数」，即**已缓存的序列长度**。`+1` 是当前要处理的新 token。整个 mask 全填 0，表示「当前 token 可以看到所有历史 token 加自己」——decode 时新 token 永远是序列最后一个，因果性天然满足，不需要像 prefill 那样构造 `-1e38f` 的因果方阵。

实际的 cache 读写封装在共享运行时函数 `llm_run_decoder_with_kv` 里，见 [src/ncnn_text_runtime.cpp:34-75](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L34-L75)。decode 分支（`is_prefill=false`）的关键两段：

```cpp
// 读：把现有 cache 喂回去（仅 decode 需要读，prefill 时 cache 为空）
if (!is_prefill) {
    for (int i = 0; i < attn_cnt; i++) {
        ex.input("cache_k%d"/"cache_v%d", kv_cache[i].first/second);
    }
}
// 写：取出新 cache
for (int i = 0; i < attn_cnt; i++) {
    ex.extract("out_cache_k%d"/"out_cache_v%d", k_cache/v_cache);
    if (is_prefill) kv_cache.emplace_back(...);   // prefill：追加
    else            kv_cache[i] = ...;            // decode：原地覆盖
}
```

generate 里调用它时第 8 个参数（`is_prefill`）恒为 `false`，见 [src/ncnn_llm_gpt.cpp:914-915](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L914-L915)：

```cpp
decode_out = llm_run_decoder_with_kv(*decoder_net, cur_embed, mask, cos_cache, sin_cache,
                                     ctx->kv_cache, attn_cnt, false);
```

> **特例：Qwen3.5 混合架构**。当上下文是 `qwen3_5_ctx`（带 `sconv_cnt`/`gdr_cnt` 的混合模型，u7-l4 详讲），generate 不走共享函数，而是**内联展开** decoder 调用，额外读写 `cache_conv%d`/`cache_gdr%d` 及其 `out_cache_*` 版本，见 [src/ncnn_llm_gpt.cpp:912-968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L968)。这是共享运行时唯一的「例外分支」，但 KV cache 的读-写-覆盖逻辑与普通分支一致。

#### 4.2.4 代码实践

**实践目标**：用源码阅读理解 mask 维度如何随步数增长。

**操作步骤**：

1. 在 [src/ncnn_llm_gpt.cpp:908](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L908) 的 `ncnn::Mat mask(...)` 之前临时加一行日志：`fprintf(stderr, "step=%d cache_h=%d mask_w=%d\n", step, ctx->kv_cache[0].first.h, ctx->kv_cache[0].first.h + 1);`。
2. 构建运行一段对话。

**需要观察的现象**：`step` 每加 1，`cache_h` 也加 1，`mask_w = cache_h + 1` 始终比缓存长度多 1。

**预期结果**：例如 prefill 后 cache 长度为 N，则日志依次显示 `step=0 cache_h=N mask_w=N+1`、`step=1 cache_h=N+1 mask_w=N+2`…… 这说明每步 decoder 把新 token 的 K/V 追加进 cache，序列长度线性增长。**待本地验证**（需要模型权重）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 decode 的 mask 可以全填 0，而 prefill 必须用因果掩码？

> **答**：decode 一次只处理序列末尾的 1 个新 token，它是最后一个，天然不可能「看到未来」，所以对所有历史位置都可以注意（全 0）。prefill 一次处理一整串，中间的 token 不能看到后面的 token，必须用 `-1e38f` 屏蔽未来位置。

**练习 2**：如果把 decode 分支误写成 `kv_cache.emplace_back(...)`（用了 prefill 的写法），会发生什么？

> **答**：每步都会新增一层而非延长现有层，`kv_cache.size()` 会从 `attn_cnt` 不断膨胀，层数对不上网络结构，下一步读 `cache_k%d` 时插槽错位，注意力计算完全错乱甚至崩溃。这正是 `is_prefill` 开关存在的意义。

---

### 4.3 采样：贪心 / temperature / top_k / top_p / repetition penalty

#### 4.3.1 概念说明

decoder 给出的是 hidden state，经 `proj_out`（lm_head）投影成**词表大小**维的 logits（原始分数）。采样就是把 logits 变成「下一个 token 的 id」。本项目有两套采样实现（u2-l1 提过）：基类私有的 `sample_logits` 服务 NLLB 这类自跑解码的运行时；**共享文本运行时的 `llm_select_next_token`** 才是 LLM/OCR/ASR 用的，本模块只讲后者。

`llm_select_next_token` 串了四种策略：重复惩罚 →（贪心 或 temperature+top_k+top_p+随机抽样）。

#### 4.3.2 核心流程

```text
llm_select_next_token(logits, history, cfg):
    scores = copy(logits)
    # ① 重复惩罚：对 history 里的 token「负乘正除」
    for t in history:
        if scores[t] < 0: scores[t] *= penalty
        else:             scores[t] /= penalty
    # ② 不采样 → 直接 argmax（贪心）
    if cfg.do_sample != 1 or temperature <= 0:
        return argmax(scores)
    # ③ 采样：softmax 带温度 → top_k → top_p → 离散抽样
    softmax_vec(scores, temperature)
    if top_k > 0:  apply_top_k(scores, top_k)
    if top_p < 1:  apply_top_p(scores, top_p)
    if sum 不有限或 <= 0: return argmax(scores)   # 兜底
    return sample_from_probs(scores)
```

#### 4.3.3 源码精读

`llm_select_next_token` 全文见 [src/ncnn_text_runtime.cpp:85-115](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L85-L115)。重复惩罚是其中最微妙的一段（[第 92-99 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L92-L99)）：

```cpp
for (int t : history) {
    if (t < 0 || t >= vocab_size) continue;
    if (scores[t] < 0) {
        scores[t] *= cfg.repetition_penalty;   // 负分 → 乘
    } else {
        scores[t] /= cfg.repetition_penalty;   // 非负分（含 0）→ 除
    }
}
```

为什么「负乘正除」能惩罚重复？默认 `repetition_penalty = 1.1 > 1`：

- 对**正分** token（模型本来偏向选的）：除以 1.1 会**变小**（如 `2.0 → 1.82`），softmax 后概率下降。
- 对**负分** token：乘以 1.1 会**更负**（如 `-2.0 → -2.2`），softmax 后概率同样下降。

两种情况都让「已经出现过的 token」分数变得更不利，从而抑制模型重复自己。这正是任务里要解释的「`<0` 时乘、`>=0` 时除」逻辑。注意边界 0 落在 `else`（除）分支——除以 1.1 仍是 0，无影响。

> 若想让模型**更**重复（罕见），可把 `repetition_penalty` 设为 `< 1`：此时正分变大、负分变没那么负，反而鼓励重复。等于 1 则无惩罚。

四种采样原语实现在 [src/sampling.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp)，都是对 `vector<float>` 的原地操作：

- `softmax_vec`（[第 7-15 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L7-L15)）：先减最大值稳定数值，再 `exp((x - max) / temperature)` 归一化。温度在**分母**，所以温度越大、指数越接近 0、分布越平坦（更随机）；温度越小、分布越尖锐（更确定）。
- `apply_top_k`（[第 17-23 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L17-L23)）：用 `nth_element` 找到第 K 大的阈值，把低于阈值的概率**清零**，只在最高的 K 个里选。
- `apply_top_p`（[第 25-50 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L25-L50)）：按概率降序排序，累加直到累计概率 ≥ p，只保留这些（核采样 nucleus），其余清零。
- `sample_from_probs`（[第 52-55 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L52-L55)）：用 `std::discrete_distribution` 按权重（未归一化也行）随机抽一个下标。

注意一个细节：`apply_top_k` / `apply_top_p` 只清零、**不重新归一化**，但因为 `discrete_distribution` 接受未归一化权重，并且 `llm_select_next_token` 在抽样前有一个 `sum` 兜底检查（[第 109-112 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L109-L112)），所以是安全的。

generate 把 `GenerateConfig` 翻译成 `LlmTokenSampleConfig` 后调用采样，见 [src/ncnn_llm_gpt.cpp:972-979](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L972-L979)：

```cpp
LlmTokenSampleConfig sample_cfg;
sample_cfg.vocab_size = vocab_size;
sample_cfg.temperature = cfg.temperature;
sample_cfg.top_p = cfg.top_p;
sample_cfg.top_k = cfg.top_k;
sample_cfg.repetition_penalty = cfg.repetition_penalty;
sample_cfg.do_sample = cfg.do_sample;
int next_id = llm_select_next_token(logits_mat, history, sample_cfg);
```

#### 4.3.4 代码实践

**实践目标**：亲手调节三个采样旋钮，对比输出差异；并复现重复惩罚的效果。

**操作步骤**：

1. 在 [examples/llm_ncnn_run/cli_runner.cpp:71-75](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L71-L75)，把 cli_runner 默认的贪心配置改成采样：

   ```cpp
   cfg.top_k = 50;
   cfg.top_p = 0.9f;
   cfg.temperature = 0.7f;
   cfg.do_sample = 1;          // 开启采样（原为 false）
   cfg.repetition_penalty = 1.1f;
   ```

2. 分别做三轮实验（每轮只改一个量，其余固定），用**同一个 prompt**（如「请写一首关于秋天的诗」）各跑一次：
   - **温度对照**：`temperature = 0.1` vs `temperature = 1.5`。
   - **top_k 对照**：`top_k = 1`（等价贪心）vs `top_k = 100`。
   - **top_p 对照**：`top_p = 0.5` vs `top_p = 0.95`。
3. 再做一个重复惩罚对照：把 prompt 设成容易重复的（如「请重复说一百遍你好」），分别用 `repetition_penalty = 1.0`（无惩罚）和 `1.5`（强惩罚）各跑一次。

**需要观察的现象**：

- 温度低 → 输出稳定、保守、几乎每次一样；温度高 → 用词更「跳」、每次不同、偶尔不通顺。
- `top_k = 1` 时输出与贪心完全一致。
- `repetition_penalty` 高时，「你好你好你好」式的连篇重复明显减少。

**预期结果**：以上趋势应当成立。但具体文本**待本地验证**（依赖实际模型）。关键不是记结论，而是理解：温度控制分布**陡峭程度**、top_k/top_p 控制**候选集合大小**、repetition_penalty 控制**对历史的抑制强度**。

> **纯源码型实践**（无需模型）：构造一个小 logits 向量，比如 `{1.0, 2.0, -1.0, 0.5}`，`history = {1}`（下标 1 已出现过，值为 2.0），手算 `repetition_penalty = 1.1` 后下标 1 变成 `2.0/1.1 ≈ 1.818`，argmax 仍是下标 1；若再加 `history = {0}`（值 1.0 → 0.909）、`history = {3}`（0.5 → 0.455），逐步观察最大值是否会被「拉下来」让别的下标反超。这能精确复现「负乘正除」的惩罚方向。

#### 4.3.5 小练习与答案

**练习 1**：默认 `GenerateConfig` 里 `do_sample = 1`，但 cli_runner 里却写了 `cfg.do_sample = false`，这两者矛盾吗？以哪个为准？

> **答**：不矛盾。结构体里的 `= 1` 只是**默认值**，cli_runner 显式赋值 `false`（即 0）会覆盖它。最终生效的是 cli_runner 的 `false`，所以命令行示例默认走贪心（argmax）。下游判断 `cfg.do_sample != 1`，`false` 成立，走贪心分支。

**练习 2**：`temperature = 0` 和 `do_sample = 0` 效果一样吗？

> **答**：都走贪心。`llm_select_next_token` 里条件是 `cfg.do_sample != 1 || cfg.temperature <= 0.0f`，两者满足其一就 argmax。区别只在语义：`do_sample` 是「是否采样」的总开关，`temperature <= 0` 是「温度退化为确定」。

---

### 4.4 eos / think / tool_call 的停止与分流

#### 4.4.1 概念说明

生成不是无限循环，它有三种「特殊时刻」：

1. **eos（end of sentence）**：模型吐出结束符，对话自然结束，`break` 跳出循环。
2. **tool_call**：模型吐出 `<tool_call>` 之类的标记，表示「我想调用某个工具」。generate 要把中间累积的调用文本交给回调执行，再把结果拼回上下文，继续生成。
3. **think**：`<think>...</think>` 是推理类模型的「思考过程」标签。需要说明的是，**当前 `generate` 主循环并没有对 think 标签做特殊拦截**，它们会像普通子词一样被解码、经 `callback` 流式输出给上层（由应用决定是显示还是隐藏）。`think_id`/`think_end_id` 在构造函数里被解析（见下），主要供上层使用。

#### 4.4.2 核心流程

循环顶部对 `ctx->cur_token` 的分流（伪代码）：

```text
if cur_token == eos:            break              # ① 结束
if cur_token == tool_call_id:   flag_in_tool_call = true        # 进入工具调用片段
elif cur_token == tool_call_end_id:                            # 工具调用片段结束
    flag_in_tool_call = false
    handle_tool(累积的 tool_call_content)    # 解析 JSON、执行回调、把结果 prefill 回上下文
    清空累积、清空 history、continue          # 不走本步的 embed/decoder，直接进下一轮
elif flag_in_tool_call:         累积到 tool_call_content          # 静默收集，不输出给用户
else:                           callback(decode(cur_token))       # 普通文本，流式输出
```

#### 4.4.3 源码精读

分流逻辑见 [src/ncnn_llm_gpt.cpp:874-890](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L874-L890)：

```cpp
for (int step = 0; step < cfg.max_new_tokens; ++step) {
    if (ctx->cur_token == eos) break;

    if (ctx->cur_token == tool_call_id) {
        flag_in_tool_call = true;
    } else if (ctx->cur_token == tool_call_end_id) {
        flag_in_tool_call = false;
        handle_tool(tool_call_content, ctx);
        tool_call_content.clear();
        history.clear();
        history.insert(ctx->cur_token);
        continue;                 // 跳过本步的 embed/decoder，直接下一轮
    } else if (flag_in_tool_call) {
        tool_call_content += bpe->decode({ctx->cur_token}, false);  // 静默累积
    } else {
        callback(bpe->decode({ctx->cur_token}, false));            // 普通输出
    }
    // ... 后续 embed/decoder/采样（普通路径才走到这里）
}
```

注意 `continue` 的作用：遇到 `tool_call_end_id` 时，本步**不**再走 embed→decoder→采样，因为 `handle_tool` 已经通过一次内部 `prefill`（把工具结果塞回上下文）改变了 `ctx`，下一轮直接消费 prefill 后的新 `cur_token`。

`handle_tool` 是定义在函数顶部的 lambda，见 [src/ncnn_llm_gpt.cpp:846-865](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L846-L865)。它做四件事：把累积文本 `json::parse` 成 JSON；调用 `cfg.tool_callback`（若未提供则包成 `{"tool_call": ...}`）；用固定模板把结果拼成一段对话（`<|im_end|>\n<|im_start|>user\n<tool_response>...`）；最后 `prefill(...)` 进上下文。这样模型在下一步就能「看到」工具返回值，继续生成。

这些特殊 token 的 id 在**构造函数**里从 model.json 或词表解析：

- `eos` 来自 tokenizer 配置（u1-l5/u3-l1）。
- `tool_call_id` / `tool_call_end_id` 来自 `setting.functions`，见 [src/ncnn_llm_gpt.cpp:148-159](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L148-L159)。
- `think_id` / `think_end_id` 直接在词表里查 `<think>` / `</think>`，见 [src/ncnn_llm_gpt.cpp:162-172](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L162-L172)。

这些 id 在头文件里声明为成员，默认 `-1`（表示「该模型没有这个特殊 token」），见 [src/ncnn_llm_gpt.h:103-108](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L103-L108)。当 `tool_call_id < 0` 时，`define_tools` 会直接返回原 ctx（[第 988 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L988)），即该模型不支持工具调用。

#### 4.4.4 代码实践

**实践目标**：理解「工具调用分流」如何让生成过程暂停、执行外部函数、再恢复。

**操作步骤**（源码阅读型）：

1. 读 `handle_tool` lambda（[src/ncnn_llm_gpt.cpp:846-865](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L846-L865)），注意它用 `try/catch` 包住 `json::parse`——若模型吐出的不是合法 JSON，`tool_call_json` 退化为空对象，但流程继续。
2. 读 cli_runner 里如何注入 `cfg.tool_callback`（[examples/llm_ncnn_run/cli_runner.cpp:77-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L77-L106)）：回调里从 `call.at("name")` 取函数名，在 `builtin_router` 里查表执行，把结果包成 `{"result":..., "call":...}` 返回。
3. 在脑中走一遍完整链路：模型生成 `<tool_call>` → `flag_in_tool_call=true` → 累积 JSON 文本 → 生成 `</tool_call>` → `handle_tool` 解析+回调+prefill 结果 → 模型继续生成最终回答。

**需要观察的现象**：工具调用的 JSON 文本**不会**出现在 `callback` 输出里（因为 `flag_in_tool_call` 分支只累积、不回调），用户只会看到最终的自然语言回答。

**预期结果**：在 cli_runner 实际运行时（需支持工具的模型），会看到 `[Tool Call]: ...` 和 `[Tool Result]: ...` 打印——那是 `tool_callback` 内部打印的，而非 generate 的 `callback`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tool_call_end_id` 分支里要 `history.clear()` 再 `history.insert(ctx->cur_token)`？

> **答**：因为 `handle_tool` 内部做了一次 `prefill`，把工具响应塞进了上下文，相当于开启了一段「新的对话片段」。之前累积的重复惩罚历史不再适用（那些 token 已经被工具响应「隔开」），所以清空 history、只保留当前 token，让后续采样的重复惩罚从新片段重新计算。

**练习 2**：如果一个模型的词表里没有 `<think>`，`think_id` 会是多少？会对生成造成什么影响？

> **答**：`think_id` 保持默认 `-1`（[src/ncnn_llm_gpt.h:107](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L107)）。由于 generate 主循环本就不拦截 think token，所以没有任何负面影响——`<think>` 文本（如果模型能生成的话）会作为普通文本输出。这个 id 当前主要是「预留」给上层使用的。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「带注释的 generate 全流程追踪」：

1. **画出数据流图**：在纸上画出一次 generate 步骤的完整数据流——`cur_token` → `embed_net` → `cur_embed` → （`mask` + `cos/sin` + `kv_cache in`）→ `decoder_net` → `decode_out` + `kv_cache out(覆盖)` → `proj_out_net` → `logits` → `llm_select_next_token`（重复惩罚 + 采样）→ `next_id`。在每个箭头旁标注它对应的源码行号（参照 4.1.3、4.2.3、4.3.3）。

2. **解释三个对照**：用自己的话写三段话，分别解释 (a) decode 的 mask 为何全 0 而 prefill 要因果掩码；(b) decode 的 KV cache 为何「原地覆盖」而 prefill「追加」；(c) 贪心与采样的本质区别（参考 4.3）。

3. **动手实验**（可选，需模型）：在 cli_runner 里依次尝试 `do_sample=false`（贪心）、`do_sample=1, temperature=0.1`（低温采样）、`do_sample=1, temperature=1.0, top_p=0.9`（核采样）三种配置，记录同一 prompt 的输出，体会「确定性 → 低随机 → 高随机」的渐变。

完成这项综合实践后，你应当能向别人完整讲清楚「ncnn_llm 是如何一个 token 一个 token 地生成回答的」。

## 6. 本讲小结

- `generate` 是一个 `for (step < max_new_tokens)` 自回归循环：每步先用上一步的 `cur_token`（解码输出或累积工具调用），再算出下一步的 token，循环往复，直到遇到 `eos` 或步数耗尽。
- 每步数据流是：`embed_net`(单 token) → `decoder_net`(带 KV cache, `is_prefill=false`) → `proj_out_net`(logits) → `llm_select_next_token`(采样)。
- KV cache 在 decode 阶段**原地覆盖**（`kv_cache[i] = ...`），每层长度 +1；这与 prefill 的 `emplace_back` 追加不同，由 `is_prefill` 开关区分。decode 的 mask 是全 0 向量（长度 = 缓存长度 + 1）。
- 采样由共享函数 `llm_select_next_token` 负责：先做**重复惩罚**（历史 token 负分乘、非负分除以 `repetition_penalty`），再根据 `do_sample` 选贪心 argmax 或 temperature+top_k+top_p+离散抽样。
- 停止与分流：`eos` 触发 `break`；`tool_call_id`/`tool_call_end_id` 之间累积的文本交给 `tool_callback` 执行并通过内部 `prefill` 回填上下文；`<think>` 标签在当前主循环里**不做拦截**，作为普通文本流式输出。
- `GenerateConfig`（`max_new_tokens`/`temperature`/`top_p`/`top_k`/`repetition_penalty`/`do_sample`/`tool_callback`）是控制生成行为的全部旋钮，cli_runner 演示了如何构造它。

## 7. 下一步学习建议

- **u2-l5（推理上下文 ctx 与多轮对话）**：本讲每次 `generate` 都 `clone_ctx` 出副本、循环里推进 `position_id` 与 KV cache。下一讲会讲清楚 `ctx` 体系（`ncnn_llm_gpt_base_ctx` / `qwen3_5_ctx`、`clone()`）以及 `run_cli` 如何把多轮 prefill+generate 串成连续对话。
- **u3-l4（采样与解码策略）**：若想更深入对比「两套采样实现」（基类 `sample_logits` vs 共享 `llm_select_next_token`），以及 `softmax_vec`/`apply_top_p` 的边界行为，可读这一篇。
- **u7-l2（工具调用机制）**：本讲的 `tool_call` 分流只展示了 generate 侧；完整的 `make_function_tool` / `define_tools` / `tool_callback` 协议在 u7-l2 详讲。
- **延伸阅读**：直接对照 [src/ncnn_llm_gpt.cpp:843-985](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L843-L985) 把整个 `generate` 函数通读一遍，是巩固本讲最有效的方式。
