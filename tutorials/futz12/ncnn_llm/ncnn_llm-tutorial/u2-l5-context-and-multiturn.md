# 推理上下文 ctx 与多轮对话

## 1. 本讲目标

本讲要把 u2-l3、u2-l4 讲过的「单次 prefill / 单次 generate」升级成「多轮会话」。学完本讲你应该能做到：

- 说清楚 `ncnn_llm_gpt_ctx` 这一套类型层次保存了什么（KV cache、`cur_token`、`position_id`），以及它为什么是「跨轮记忆」的唯一载体。
- 掌握 `clone()` 的作用：理解 `prefill` / `generate` 为什么都是「克隆输入 ctx、改动副本、再返回」，并据此解释多轮对话里「记忆」是如何一层层累积的。
- 跟着 `position_id` 的推进算出任意一轮对话里「下一个 token 该用什么位置编码」。
- 知道 Qwen3.5 混合架构为什么需要一个派生类 `qwen3_5_ctx`，它额外携带的 `sconv_cache` / `gdr_cache` 是什么。
- 读懂 `run_cli` 的多轮循环，能自己写出一段「记住前文」的多轮对话。

本讲承接 u2-l4（generate 主循环），并为 u5-l4（VLM 多轮 prefill）与 u7-l2（工具调用回填上下文）打底。

## 2. 前置知识

在进入本讲前，请先回忆以下几个概念（前几讲已建立）：

- **KV cache**：Transformer 自回归推理里，把每一层的 Key / Value 缓存下来，避免每生成一个 token 就重算历史。本项目里 KV cache 的类型是 `vector<pair<ncnn::Mat, ncnn::Mat>>`，长度等于层数（`attn_cnt`），每层一对 K/V（见 u2-l1）。
- **prefill / generate 两阶段**：prefill 一次性「吃进」一整段 prompt 并填好 KV cache，产出第一个 token；generate 在已有 KV cache 上逐 token 续写（见 u2-l3、u2-l4）。
- **自回归**：下一个 token 的预测依赖前面所有 token，因此「记住前文」本质上就是「保留并继续追加 KV cache」。
- **位置编码（RoPE）**：每个 token 需要一个位置编号来生成 cos/sin cache。位置编号必须跨轮连续，否则模型会把「第 0 个 token」当成的位置语义搞乱。

一句话点题：**多轮对话 = 同一个 ctx 在多轮 prefill / generate 之间被反复「续写」**。ctx 就是「会话状态」的对象化。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| `src/ncnn_llm_gpt.h` | 定义 ctx 的类型层次：抽象基类 `ncnn_llm_gpt_ctx`、文本派生类 `ncnn_llm_gpt_base_ctx`、混合架构派生类 `qwen3_5_ctx`，以及 `clone()` 接口。 |
| `src/ncnn_llm_gpt.cpp` | 实现 `create_ctx` / `clone_ctx` 工厂与助手；在三个 `prefill` 重载和 `generate` 里读写 ctx，推进 `position_id`。 |
| `examples/llm_ncnn_run/cli_runner.cpp` | `run_cli` 的多轮循环：system prompt 预填充 → 循环读取用户输入 → prefill → generate，ctx 在循环里贯穿。 |
| `tests/test_llm.cpp` | `test_model_context_memory` 给出「验证模型能记住前文」的最小可参考用例。 |

贯穿本讲的核心结论先放在这里：**ctx 是一个「可克隆、可传递、随轮次增长」的状态对象；prefill 和 generate 都不直接修改调用方传入的 ctx，而是克隆一份再改、改完返回**。这套函数式风格是多轮记忆与「会话分叉/回退」能成立的关键。

## 4. 核心概念与源码讲解

### 4.1 ctx 体系与 clone：把对话状态封装成可传递的对象

#### 4.1.1 概念说明

「多轮对话」对推理引擎来说，难点不是模型本身，而是**状态在多次调用之间如何保存与传递**。一次 `generate` 调用结束、函数返回后，局部变量就消失了，但下一轮用户提问时模型必须「记得」前面说过什么——这些记忆全部住在 KV cache 里。

`ncnn_llm` 的做法是：把「一轮推理结束后的全部状态」打包成一个对象，就是 `ncnn_llm_gpt_ctx`。它保存三样东西：

1. `kv_cache`：到目前为止累积的全部 K/V（历史对话的「记忆实体」）。
2. `cur_token`：上一步刚确定的 token id（generate 下一步要 embed 并送进 decoder 的就是它）。
3. `position_id`：下一个待分配的位置编号（RoPE 要用它）。

有了这个对象，`prefill` 和 `generate` 的签名就可以写成「吃一个 ctx、吐一个 ctx」，把会话状态像接力棒一样传下去。

为了让「纯 Transformer」和「Qwen3.5 混合架构」共用同一套接口，ctx 被设计成一个**多态类层次**：

- `ncnn_llm_gpt_ctx`：抽象基类，只声明 `virtual clone()` 和三个公共字段。
- `ncnn_llm_gpt_base_ctx`：纯 Transformer 用，`clone()` 复制 KV cache。
- `qwen3_5_ctx`：Qwen3.5 混合架构用，额外携带 `sconv_cache` / `gdr_cache`（见 4.4）。

#### 4.1.2 核心流程

ctx 的生命周期如下：

```text
prefill(system_prompt)          # 无 ctx 重载：从零创建一个 ctx（create_ctx）
        │
        ▼
   ctx = prefill(user_msg_1, ctx)   # 克隆 ctx → 改副本 → 返回新 ctx
        │
        ▼
   ctx = generate(ctx, cfg, cb)     # 克隆 ctx → 自回归续写 → 返回新 ctx
        │
        ▼
   ctx = prefill(user_msg_2, ctx)   # 再克隆、再续写 ……
```

每一步都返回一个**新的** ctx，调用方用 `ctx = ...` 把它接住。于是 KV cache 一轮轮累积，`position_id` 一路递增，模型就「记住」了整段会话。

#### 4.1.3 源码精读

抽象基类只声明接口与三个公共字段，`clone()` 是纯虚函数：

```cpp
class ncnn_llm_gpt_ctx {
public:
    virtual ~ncnn_llm_gpt_ctx() = default;
    virtual std::shared_ptr<ncnn_llm_gpt_ctx> clone() const = 0;
    KVCache kv_cache;
    int cur_token = 0;
    int position_id = 0;
};
```

> [src/ncnn_llm_gpt.h:L45-L54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L45-L54) — 抽象基类，三个字段就是「会话状态」的全部。`clone()` 返回 `shared_ptr<ncnn_llm_gpt_ctx>`，便于多态传递。

纯 Transformer 的派生类实现 `clone()`，逐层复制 KV cache：

```cpp
class ncnn_llm_gpt_base_ctx : public ncnn_llm_gpt_ctx {
public:
    std::shared_ptr<ncnn_llm_gpt_ctx> clone() const override {
        auto dst = std::make_shared<ncnn_llm_gpt_base_ctx>();
        dst->kv_cache.resize(kv_cache.size());
        for (size_t i = 0; i < kv_cache.size(); ++i) {
            dst->kv_cache[i].first  = kv_cache[i].first;
            dst->kv_cache[i].second = kv_cache[i].second;
        }
        dst->cur_token  = cur_token;
        dst->position_id = position_id;
        return dst;
    }
};
```

> [src/ncnn_llm_gpt.h:L56-L69](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L56-L69) — 基础派生类的克隆实现。

关于这段「逐元素赋值」有一个容易踩坑的细节需要特别说明：**`ncnn::Mat` 内部是引用计数的，赋值是浅拷贝（共享底层缓冲区），不是深拷贝**。也就是说 `dst->kv_cache[i].first = kv_cache[i].first` 之后，新旧 ctx 共享同一块 K 的内存。这之所以安全，是因为后续 decoder 前向遵循「读旧 cache、写新 cache」的模式：

- 旧 KV cache 作为输入插槽 `cache_k%d` / `cache_v%d` 被**读取**，底层缓冲区从不被原地改写；
- decoder 的输出通过 `out_cache_k%d` / `out_cache_v%d` 写到 ncnn **新分配**的 `ncnn::Mat`，再整体赋给 `new_ctx->kv_cache[i]`，替换掉那个共享引用。

所以即便克隆是浅拷贝，新旧 ctx 也不会互相污染——输入端只读，输出端是新内存。理解这一点，你才能放心地在多轮对话里保留旧 ctx 做「分叉」或「回退」。

工厂函数 `create_ctx` 负责在「从零开始」时按架构选对类型：

```cpp
static std::shared_ptr<ncnn_llm_gpt_ctx> create_ctx(int sconv_cnt, int gdr_cnt) {
    if (sconv_cnt > 0 || gdr_cnt > 0) {
        return std::make_shared<qwen3_5_ctx>();
    }
    return std::make_shared<ncnn_llm_gpt_base_ctx>();
}
```

> [src/ncnn_llm_gpt.cpp:L9-L14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14) — 工厂：`sconv_cnt` / `gdr_cnt` 大于 0 才用 `qwen3_5_ctx`，否则用基础 ctx。这两个计数来自 `model.json` 的 `setting.sconv_cnt` / `setting.gdr_cnt`（见 u1-l5）。

而「带 ctx 继续 prefill」的重载，第一行永远是克隆：

```cpp
std::shared_ptr<ncnn_llm_gpt_ctx> ncnn_llm_gpt::prefill(
    const std::string& input_text, const std::shared_ptr<ncnn_llm_gpt_ctx> ctx) const {
    std::shared_ptr<ncnn_llm_gpt_ctx> new_ctx = clone_ctx(ctx);
    ...
}
```

> [src/ncnn_llm_gpt.cpp:L639-L640](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L639-L640) — 文本多轮 prefill 重载；`clone_ctx` 只是一行转发：[src/ncnn_llm_gpt.cpp:L5-L7](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L5-L7)。

`generate` 同样在进入主循环前先克隆输入 ctx（[src/ncnn_llm_gpt.cpp:L867](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L867)），因此调用方传进来的 ctx 在整个生成过程中保持不变。是否把生成结果「写回」会话，完全由调用方决定（`ctx = model.generate(ctx, ...)`）。

> **小结**：clone 带来三个好处——① 函数式风格，每次调用都不污染调用方，便于分叉/回退；② 多轮对话里可以安全地保留任意一轮的 ctx 快照；③ 工具调用失败时可以回退到调用前的状态。

#### 4.1.4 代码实践

**实践目标**：亲手验证「prefill / generate 不修改调用方的 ctx」，从而理解克隆语义。

**操作步骤**（源码阅读型实践，无需运行模型）：

1. 在 [src/ncnn_llm_gpt.cpp:L639](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L639) 的 `prefill(text, ctx)` 里确认第一行是 `clone_ctx(ctx)`，函数体内所有改动都作用在 `new_ctx` 上。
2. 在 [src/ncnn_llm_gpt.cpp:L867](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L867) 的 `generate` 里确认 `auto ctx = clone_ctx(ctx_in);`，循环里改的是局部 `ctx`，返回的也是这个 `ctx`。
3. 想象下面这段伪代码的语义：

```cpp
// 示例代码（非项目原有）
auto ctx_after_prefill = model.prefill(user_msg, ctx_base);
auto ctx_after_gen     = model.generate(ctx_after_prefill, cfg, cb);
// ctx_after_prefill 仍是「只 prefill、未 generate」的状态
// ctx_base 仍是「用户消息之前」的状态 —— 三条会话分支同时存在
```

**需要观察的现象**：`ctx_base`、`ctx_after_prefill`、`ctx_after_gen` 指向三个不同的 KV cache 状态，互不影响。

**预期结果**：因为每次调用都克隆，所以三个 `shared_ptr` 各自持有独立的（逻辑上的）会话状态，可以实现「同一前文、不同后续」的会话分叉。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `prefill(text, ctx)` 第一行的 `clone_ctx(ctx)` 删掉、直接 `auto& new_ctx = ctx;` 修改入参，多轮对话还能正常工作吗？为什么？

> **参考答案**：功能上「单线会话」仍能跑通，但会失去两个能力：一是无法保留历史快照做分叉/回退；二是 `generate` 里 `clone_ctx` 之后再 `prefill` 回填工具结果（见 u2-l4 的 `handle_tool`）会污染调用方持有的 ctx，导致不可预期的状态。函数式克隆是这套 API 的设计前提。

**练习 2**：`ncnn_llm_gpt_ctx::clone()` 为什么声明为 `const`？

> **参考答案**：克隆是「复制自己」，不应改动原对象，因此 `const`。它返回一个新的 `shared_ptr<ncnn_llm_gpt_ctx>`，调用方拿到副本去改，原 ctx 保持不变——这正是多轮记忆能安全累积的语义基础。

---

### 4.2 position_id 的推进：跨多轮的位置连续性

#### 4.2.1 概念说明

RoPE 位置编码靠一个从 0 开始递增的位置编号来区分「这是第几个 token」。在多轮对话里，第二轮的用户消息绝不是「从位置 0 重新开始」，而必须**接在第一轮的末尾**继续编号。否则模型会误以为又在读一段全新序列，语义会错乱。

`ncnn_llm` 把这个「下一个可用位置」记录在 `ctx->position_id` 里。只要 ctx 在多轮之间被正确传递，`position_id` 就会一路递增，保证整段会话的位置编号连续。

#### 4.2.2 核心流程与公式

设一轮 prefill 处理了 \(n\) 个 token（含这一轮新加的全部 token），生成阶段每步产出 1 个 token。位置推进规则是：

\[ p_{\text{新}} = p_{\text{旧}} + n_{\text{prefill}} \]

generate 里每一步：

\[ p \leftarrow p + 1 \]

具体到代码，三个入口的推进方式略有不同：

| 入口 | 起始位置 | 结束时 position_id |
| --- | --- | --- |
| `prefill(text)`（无 ctx，首轮） | `0` | \(M\)（编码后总 token 数） |
| `prefill(text, ctx)`（多轮文本） | 继承 `ctx->position_id` | \(p_{\text{旧}} + M\) |
| `generate(ctx, ...)` | 继承 `ctx->position_id` | 每生成 1 个 token 加 1 |

（带图像的 `prefill(text, bgr, ctx)` 因图像嵌入会替换 `image_pad` 占位，位置推进另有公式，详见 u5-l4。）

> 一个贯穿全讲的**不变量**：`position_id` 恒等于「到目前为止已经处理过的 token 总数」，也就是 KV cache 里已有的行数（对纯 Transformer 而言）。下一轮无论 prefill 还是 generate，都从这个值继续。

#### 4.2.3 源码精读

先看首轮 `prefill(text)` 的收尾，它从 0 起算、把首位置写进新建的 ctx：

```cpp
auto ctx = create_ctx(sconv_cnt, gdr_cnt);
ctx->kv_cache = std::move(kv_cache);
ctx->cur_token = next_token_id;
ctx->position_id = (int)token_ids.size() + 1;   // token_ids 是 pop_back 之后的，+1 = 完整长度 M
```

> [src/ncnn_llm_gpt.cpp:L422-L425](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L422-L425) — 首轮：`position_id` = 主体 token 数 + 1（末位 token）。注意 `token_ids` 此处已 `pop_back` 掉末位 token（见 u2-l3），主体长度为 \(M-1\)，加 1 正好得 \(M\)。

再看多轮文本重载 `prefill(text, ctx)`，它先取出继承来的位置，主体段用它、再整体加 token 数：

```cpp
int current_pos = new_ctx->position_id;          // 继承上一轮
// ... 用 current_pos 生成本轮主体的 RoPE cache ...
new_ctx->position_id += token_ids.size();        // 主体段加 (M-1)
// ... 末位 token 用 new_ctx->position_id 生成 RoPE ...
new_ctx->position_id += 1;                       // 末位再加 1
```

> [src/ncnn_llm_gpt.cpp:L647-L659](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L647-L659) — 取出 `current_pos`、主体段推进；末位 token 的推进见 [src/ncnn_llm_gpt.cpp:L754-L756](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L754-L756)。两步合计 \(+(M-1)+1 = +M\)，与首轮一致。

generate 主循环里，每生成一个 token 推进一次：

```cpp
for (int step = 0; step < cfg.max_new_tokens; ++step) {
    // ... 用 ctx->position_id 生成本步 RoPE cache ...
    ctx->position_id++;
    // ... decoder 前向、采样出 next_id ...
}
```

> [src/ncnn_llm_gpt.cpp:L894-L906](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L894-L906) — generate 每步 `position_id++`。

正是这三处「继承 → 推进」的接力，让多轮对话里 RoPE 的位置编号永不重置、永远连续。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或打印日志）跟踪一段两轮对话的 `position_id`，验证连续性。

**操作步骤**（源码阅读型实践）：

1. 假设 system prompt 编码后 \(M_0 = 10\) 个 token；第一轮用户消息 \(M_1 = 8\)；模型回复 \(G_1 = 20\) 个 token；第二轮用户消息 \(M_2 = 6\)。
2. 按上面的公式逐步算：
   - 首轮 `prefill(system)` 后：`position_id = 10`。
   - `prefill(msg1, ctx)` 后：`10 + 8 = 18`。
   - `generate` 生成 20 个 token 后：`18 + 20 = 38`。
   - `prefill(msg2, ctx)` 后：`38 + 6 = 44`。
3. 在源码里标注这四步分别对应哪一行赋值（[L425](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L422-L425)、[L659/L756](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L647-L659)、[L906](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L894-L906)）。

**需要观察的现象 / 预期结果**：第二轮的 `prefill` 应从位置 38 开始为 6 个 token 分配 RoPE，而不是从 0 开始。

> 待本地验证：若想实测，可在上述三处 `position_id` 赋值后加一行 `fprintf(stderr, "position_id=%d\n", new_ctx->position_id);`，跑一段真实对话核对数值（需自备 `qwen3_0.6b` 等模型，见 u1-l2）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prefill(text, ctx)` 不能像首轮那样把 `position_id` 设成 `token_ids.size() + 1`？

> **参考答案**：那样会丢弃前几轮累积的位置偏移，让本轮 token 从接近 0 的位置重新编码，破坏整段会话的位置连续性。多轮重载必须「继承 `ctx->position_id` 再叠加」，而不是重置。

**练习 2**：如果 `generate` 里漏掉 `ctx->position_id++`，会出现什么现象？

> **参考答案**：每一步生成的 token 都会用同一个位置编号生成 RoPE，模型会把整段回复当成「都挤在同一个位置」，注意力分布错乱，输出会迅速退化成重复或乱码。

---

### 4.3 qwen3_5_ctx：为混合架构扩展的 sconv/gdr cache

#### 4.3.1 概念说明

到目前为止我们默认 decoder 全是 Transformer 的注意力层，KV cache 只需 K/V 两样。但 Qwen3.5 这类**混合架构**（hybrid）会混入「线性注意力 / 门控增量规则（Gated Delta Rule）」等非标准层，这些层有自己的递归状态——`ShortConv` 层的状态记作 `sconv_cache`，`GatedDeltaRule` 层的状态记作 `gdr_cache`。

这些状态和 KV cache 一样，需要跨轮保留，否则模型同样会「失忆」。因此项目派生出一个 `qwen3_5_ctx`，在 KV cache 之外再挂两组 cache。custom ncnn 算子 `GatedDeltaRule` / `ShortConv` 本身的实现属于 u7-l4 的内容，本讲只关心**它们的运行时状态如何被 ctx 携带**。

#### 4.3.2 核心流程

```text
create_ctx(sconv_cnt, gdr_cnt)
        │  sconv_cnt>0 或 gdr_cnt>0 ?
        ├── 是 → new qwen3_5_ctx      （带 sconv_cache / gdr_cache）
        └── 否 → new ncnn_llm_gpt_base_ctx（只带 KV cache）

prefill / generate 里：
        dynamic_pointer_cast<qwen3_5_ctx>(ctx)
        ├── 命中 → 额外读写 cache_conv%d / cache_gdr%d 插槽
        └── 未命中 → 只读写 KV cache
```

`sconv_cnt` / `gdr_cnt` 决定 decoder 里有多少层是 ShortConv / GDR 层，也决定这些 cache 向量有多长。它们在构造函数里从 `model.json` 的 `setting` 读入（见 u1-l5），默认都是 0（纯 Transformer）。

#### 4.3.3 源码精读

派生类在 KV cache 之外多了两个向量，`clone()` 一并复制：

```cpp
class qwen3_5_ctx : virtual public ncnn_llm_gpt_ctx {
public:
    std::vector<ncnn::Mat> sconv_cache;
    std::vector<ncnn::Mat> gdr_cache;

    std::shared_ptr<ncnn_llm_gpt_ctx> clone() const override {
        auto dst = std::make_shared<qwen3_5_ctx>();
        // ... 复制 kv_cache（同 base_ctx）...
        dst->sconv_cache = sconv_cache;
        dst->gdr_cache   = gdr_cache;
        dst->cur_token   = cur_token;
        dst->position_id = position_id;
        return dst;
    }
};
```

> [src/ncnn_llm_gpt.h:L71-L89](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L71-L89) — `qwen3_5_ctx` 多带两组 cache，`clone()` 全部复制。

在 prefill/generate 里，用 `dynamic_pointer_cast` 判断当前 ctx 是不是 `qwen3_5_ctx`，是则额外读写对应的 cache 插槽：

```cpp
auto qwen_ctx = std::dynamic_pointer_cast<qwen3_5_ctx>(ctx);
if (qwen_ctx) {
    for (int i = 0; i < sconv_cnt; ++i) {
        // ex.input("cache_conv%d", qwen_ctx->sconv_cache[i]);   读
        // ex.extract("out_cache_conv%d", ...); → qwen_ctx->sconv_cache[i]  写
    }
    for (int i = 0; i < gdr_cnt; ++i) {
        // 同理处理 cache_gdr%d / out_cache_gdr%d
    }
}
```

> [src/ncnn_llm_gpt.cpp:L694-L733](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L694-L733) — 文本 prefill 重载里对 `qwen3_5_ctx` 的额外 cache 读写（输入 `cache_conv%d` / `cache_gdr%d`，输出 `out_cache_conv%d` / `out_cache_gdr%d`）。generate 里的对应分支见 [src/ncnn_llm_gpt.cpp:L912-L968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L968)。

注意 generate 里有个关键分支：纯 Transformer 走的是共享运行时函数 `llm_run_decoder_with_kv`（u2-l2），而 `qwen3_5_ctx` 因为多了 sconv/gdr 插槽、无法被那个通用函数覆盖，只能在 generate 里**内联展开** decoder 调用：

```cpp
auto qwen_ctx = std::dynamic_pointer_cast<qwen3_5_ctx>(ctx);
if (!qwen_ctx) {
    decode_out = llm_run_decoder_with_kv(*decoder_net, cur_embed, mask, cos_cache, sin_cache,
                                         ctx->kv_cache, attn_cnt, false);
} else {
    // 内联：手动 input cache_k/v、cache_conv、cache_gdr，再 extract out_cache_*
}
```

> [src/ncnn_llm_gpt.cpp:L912-L968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L968) — 这是 u2-l2 提到的「唯一例外」：带 sconv/gdr 的混合架构不走共享 `llm_run_decoder_with_kv`，而是在 generate 里内联展开。

#### 4.3.4 代码实践

**实践目标**：理解 `create_ctx` 的分流与 `dynamic_pointer_cast` 的判定逻辑。

**操作步骤**（源码阅读型实践）：

1. 在 [src/ncnn_llm_gpt.cpp:L9-L14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14) 确认 `create_ctx` 的判定条件是 `sconv_cnt > 0 || gdr_cnt > 0`。
2. 在 [src/ncnn_llm_gpt.cpp:L912](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912) 确认 generate 用同一个 `dynamic_pointer_cast<qwen3_5_ctx>` 决定走共享函数还是内联展开。
3. 对照 `model.json`：若 `setting` 里没有 `sconv_cnt` / `gdr_cnt`，构造函数里这两个计数默认为 0（[src/ncnn_llm_gpt.cpp:L109-L114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L109-L114)），于是 `create_ctx` 永远返回基础 ctx，模型按纯 Transformer 运行。

**需要观察的现象 / 预期结果**：纯 Transformer 模型（如 `qwen3_0.6b`）运行时 `dynamic_pointer_cast<qwen3_5_ctx>` 返回 `nullptr`，走 `llm_run_decoder_with_kv` 分支；只有 Qwen3.5 混合架构才会命中内联分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `qwen3_5_ctx` 用的是 `virtual public` 继承？

> **参考答案**：这是为多重继承留接口（项目里 `qwen3_5_ctx` 当前只单继承基类，`virtual` 保持灵活性，便于将来若有更复杂的混入组合时不出现菱形继承的歧义）。对学习者来说，关键点在于它 `is-a ncnn_llm_gpt_ctx`，所以可以装进 `shared_ptr<ncnn_llm_gpt_ctx>` 多态传递。

**练习 2**：如果不扩展 ctx、硬把 sconv/gdr 状态塞进 `kv_cache`，会有什么问题？

> **参考答案**：KV cache 的语义是「每层一对 K/V」，sconv/gdr 是不同形状、不同语义的递归状态，强行塞进去会破坏 `kv_cache` 的类型约定和 `attn_cnt` 计数，也让共享运行时 `llm_run_decoder_with_kv` 无法通用。派生一个 `qwen3_5_ctx` 是干净的做法。

---

### 4.4 run_cli 多轮对话循环：把 ctx 串成一条会话

#### 4.4.1 概念说明

前面三个模块讲清了 ctx「是什么、怎么克隆、怎么推进位置、怎么扩展」。本模块看 `cli_runner` 如何把它们组装成一个**可交互的多轮对话**：先预填 system prompt 建立基线 ctx，再进入循环，每轮读用户输入 → prefill 进上下文 → generate 生成回复，ctx 在循环变量里贯穿，于是每一轮都自然地「记得」前面所有轮次。

#### 4.4.2 核心流程

```text
run_cli:
  1. detect_template_type(model_path)            # 选 ChatML / YouTu 模板
  2. ctx = model.prefill(system_prompt)          # 建立初始 ctx（含 system）
  3. 若有内置工具：ctx = model.define_tools(ctx, builtin_tools, system_prompt)
  4. while 读用户输入:
       a. first_turn 且有图 → prefill(user_msg 含图像占位, image, ctx)   # VLM 首轮
          否则              → prefill(user_msg, ctx)                      # 文本多轮
       b. ctx = model.generate(ctx, cfg, 流式 callback)                   # 续写并更新 ctx
  5. ctx 越累积越长 → 模型记住全部历史
```

关键点：**`ctx` 是循环里被反复赋值的同一个变量**。每次 `prefill` / `generate` 返回的新 ctx 都覆盖它，记忆就这样层层叠加。

#### 4.4.3 源码精读

建立初始 ctx（system prompt 预填充，可选工具注入）：

```cpp
std::string system_prompt = "You are a helpful assistant.";
std::string prompt = apply_chat_template(template_type, {{"system", system_prompt}}, {}, false, false);
auto ctx = model.prefill(prompt);

if (!builtin_tools.empty()) {
    ctx = model.define_tools(ctx, builtin_tools, system_prompt);
}
```

> [examples/llm_ncnn_run/cli_runner.cpp:L40-L46](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L40-L46) — 只 prefill 一次 system prompt，就建好了贯穿整个会话的基线 ctx。`define_tools` 在此 ctx 上再追加工具描述（其实现见 [src/ncnn_llm_gpt.cpp:L987-L995](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L987-L995)，本质是一次带 ctx 的 prefill）。

循环主体——每轮把用户消息 prefill 进 ctx、再 generate 出回复：

```cpp
while (true) {
    std::string input;
    std::cout << "User: ";
    if (!std::getline(std::cin, input)) break;
    if (input == "exit" || input == "quit") break;

    if (first_turn && has_image) {
        std::string user_message = apply_chat_template(template_type, {
            {"user", "<|vision_start|><|image_pad|><|vision_end|>" + input}
        }, {}, true, false);
        ctx = model.prefill(user_message, image, ctx);   // VLM 首轮带图
        first_turn = false;
    } else {
        std::string user_message = apply_chat_template(template_type, {
            {"user", input}
        }, {}, true, false);
        ctx = model.prefill(user_message, ctx);           // 文本多轮
    }

    std::cout << "Assistant: ";
    GenerateConfig cfg;
    cfg.top_k = 40; cfg.top_p = 0.9f; cfg.temperature = 0.7f; cfg.do_sample = false;
    cfg.tool_callback = [&](const json& call) { /* 路由到 builtin_router */ };

    ctx = model.generate(ctx, cfg, [](const std::string& token) {
        std::cout << token << std::flush;                 // 流式打印
    });
}
```

> [examples/llm_ncnn_run/cli_runner.cpp:L51-L113](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L51-L113) — 整个多轮循环。注意 `ctx = model.prefill(...)` 与 `ctx = model.generate(...)` 这两次赋值就是「记忆累积」的全部魔法。

模板选择 `detect_template_type` 只读 `model.json` 的 `type` 字段，默认 ChatML：

> [examples/llm_ncnn_run/cli_runner.cpp:L9-L29](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L9-L29) — 读不到或读失败都安全降级为 ChatML。

一个常被忽略但很重要的细节：`cli_runner` 里**每轮只 prefill「本轮流式拼接出来的用户消息」，并不把上一轮模型的完整回复重新 prefill 进去**。模型之所以仍「记得」自己上一轮说了什么，是因为 `generate` 已经把回复的每个 token 都追加进了 ctx 的 KV cache。换句话说，回复的内容不是靠「重新读一遍文本」进入上下文，而是靠 generate 期间实打实地扩展了 KV cache。这正是自回归 + KV cache 多轮机制的本质。

#### 4.4.4 代码实践

**实践目标**：复现 `test_model_context_memory`，亲眼看到模型跨轮记住前文。

**参考用例**（项目原有测试）：

```cpp
// tests/test_llm.cpp: test_model_context_memory（节选）
ncnn_llm_gpt model(get_model_path("qwen3_0.6b"));

std::string prompt = apply_chat_template({{"system", "You are a helpful assistant."}}, {}, false, false);
auto ctx = model.prefill(prompt);                                 // 1. 基线 ctx

std::string msg1 = apply_chat_template({{"user", "My favorite color is blue."}}, {}, true, false);
ctx = model.prefill(msg1, ctx);                                   // 2. 告知一个事实
ctx = model.generate(ctx, GenerateConfig{}, [](const std::string&) {});  // 3. 让模型消化

std::string msg2 = apply_chat_template({{"user", "What is my favorite color?"}}, {}, true, false);
ctx = model.prefill(msg2, ctx);                                   // 4. 第二轮提问

std::string response;
ctx = model.generate(ctx, GenerateConfig{}, [&response](const std::string& token) {
    response += token;                                            // 5. 收集回复
});
TEST_ASSERT(!response.empty(), "Response should not be empty");
```

> [tests/test_llm.cpp:L256-L283](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L256-L283) — 完整可参考用例（模型不存在时由 `has_model` 跳过，见 [tests/test_llm.cpp:L257-L260](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L257-L260)）。

**操作步骤**：

1. 准备模型：在 `assets/qwen3_0.6b/` 放好模型目录（含 `model.json` 与三套 `.param`/`.bin`，见 u1-l5）。
2. 编译并运行测试：`xmake build test_llm && xmake run test_llm`（构建方式见 u1-l2）。观察输出里 `model_context_memory` 这一项。
3. 想自定义多轮对话，可仿照上面五步写一个最小 `main`，或直接用 `llm_ncnn_run` 交互式输入两轮，第二轮问「我刚才说的颜色是什么」。

**需要观察的现象**：第二轮提问「What is my favorite color?」时，模型应能回答出 `blue`（或其同义表述），证明第一轮的事实通过 ctx 被带进了第二轮。

**预期结果**：`response` 非空，且语义上指向第一轮告知的颜色。

> 待本地验证：实际回复内容依赖模型权重与采样参数，请以本地运行结果为准；若未准备模型，本测试会被跳过（输出 `(skipped - model not found)`），这是正常现象。

#### 4.4.5 小练习与答案

**练习 1**：在 `run_cli` 里，如果把 `ctx = model.generate(ctx, cfg, cb)` 改成 `model.generate(ctx, cfg, cb);`（不接住返回值），多轮对话会出现什么问题？

> **参考答案**：generate 是克隆后再改、返回新 ctx；不接住返回值意味着「模型这一轮生成的回复」对应的 KV cache 扩展被丢弃。下一轮 prefill 仍基于「上一轮 generate 之前」的 ctx，模型不会记得自己刚才说了什么，但会记得用户更早说过的话——表现为「知道自己被问什么，却忘了自己怎么回答的」，多轮一致性被破坏。

**练习 2**：为什么首轮带图要用单独的 `prefill(text, image, ctx)` 重载，而后续轮次即使用户再发图也走文本重载？

> **参考答案**：当前 `run_cli` 只在 `first_turn && has_image` 时走图像 prefill（[L57-L62](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L51-L68)），把第一张图嵌入占位。这是一种简化策略：CLI 演示侧重「首轮识图、后续追问」的典型流程。多图、跨轮换图需要更复杂的占位与 ctx 管理，留作二次开发练习。

---

## 5. 综合实践

**任务**：自己写一个三段式的最小多轮对话程序，把本讲四个模块串起来。

要求：

1. 用 `apply_chat_template` 构造 system prompt，`prefill` 建立基线 `ctx`（对应 4.1、4.4）。
2. 第一轮：`prefill("My name is <你的名字>.", ctx)` → `generate`，接住返回的 `ctx`（验证 4.4 的循环写法）。
3. 第二轮：基于上一步的 `ctx`，`prefill("What is my name?", ctx)` → `generate`，看模型能否答出名字（验证记忆=KV cache 累积）。
4. 在每一步前后打印 `ctx->position_id`，对照 4.2 的公式核对数值连续性。
5. （选做）保留第一轮 prefill 之后、generate 之前的 ctx 快照，构造一个「分叉」会话：一支正常 generate，另一支用不同 `GenerateConfig`（如更高 `temperature`）generate，对比两条分支的输出——体会 clone 带来的可分叉性。

**验收标准**：

- 第二轮能正确回忆第一轮的信息（待本地验证，依赖模型）。
- 打印的 `position_id` 单调递增、数值与手算一致。
- 分叉分支互不影响（两个 ctx 独立演化）。

> 提示：可参考 `tests/test_llm.cpp` 里 `test_model_context_memory`（[L256-L283](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L256-L283)）和 `test_model_tool_calling`（[L225-L254](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/tests/test_llm.cpp#L225-L254)）的写法，把模型路径换成你本地的 `assets/<模型名>`。

## 6. 本讲小结

- **ctx 是会话状态的对象化**：`ncnn_llm_gpt_ctx` 把 KV cache、`cur_token`、`position_id` 三样打包，是多轮记忆的唯一载体。它有多态层次——基础 `ncnn_llm_gpt_base_ctx` 与混合架构 `qwen3_5_ctx`。
- **clone 是函数式风格的核心**：`prefill` / `generate` 都先 `clone_ctx` 再改副本、返回新 ctx，调用方原始 ctx 不被污染，由此支持多轮累积、会话分叉与回退。
- **克隆的安全性来自 ncnn 的读写模式**：`ncnn::Mat` 赋值是引用计数的浅拷贝，但 decoder 读旧 cache（只读）、写新 cache（新分配），输入缓冲区从不被原地改写，故浅克隆不会互相污染。
- **position_id 跨轮连续递增**：首轮从 0 起算，多轮 prefill「继承 + 叠加」，generate 每步 +1；不变量是「`position_id` == 已处理 token 总数」。
- **qwen3_5_ctx 扩展了混合架构状态**：额外携带 `sconv_cache` / `gdr_cache`，用 `dynamic_pointer_cast` 判定；命中则在 generate 内联展开 decoder，否则走共享 `llm_run_decoder_with_kv`。
- **run_cli 把 ctx 串成会话**：system prompt 预填一次建立基线，循环里 `ctx = prefill(...); ctx = generate(...);` 反复赋值，记忆层层叠加；模型回复靠 generate 扩展 KV cache 进入上下文，无需重新 prefill 文本。

## 7. 下一步学习建议

- **u5-l4（VLM prefill 完整流程）**：本讲的 `prefill(text, bgr, ctx)` 重载里图像分支的位置推进（`token_ids.size() - image_embeds_size + num_patches_w/spatial_merge_size`）只是点了一句，图像如何替换 `image_pad` 占位、mRoPE 如何为图像 token 分配 t/h/w 轴，留待 u5 单元深入。
- **u7-l2（工具调用机制）**：本讲 generate 里 `handle_tool` 在工具调用结束后会 `prefill(tool_response, ctx)` 把工具结果回填进同一 ctx，这是 ctx 在「单轮内」被反复推进的另一个范例，工具调用的完整协议在 u7-l2。
- **u7-l4（自定义 ncnn 算子 GDR / ShortConv）**：想彻底搞懂 `sconv_cache` / `gdr_cache` 背后的 `GatedDeltaRule` / `ShortConv` 算子如何注册进 ncnn、其 `forward` 如何递归更新状态，继续读 u7-l4 与 `src/utils/gdr.*`。
- **u8-l3（测试框架）**：本讲反复引用的 `test_model_context_memory` 用的就是 `tests/test_framework.h` 的 `TEST_ASSERT` + `has_model` 跳过机制，想给 ctx 行为补单元测试可参考 u8-l3。
