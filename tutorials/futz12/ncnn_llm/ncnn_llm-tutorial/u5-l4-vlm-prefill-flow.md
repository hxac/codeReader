# VLM prefill 完整流程

## 1. 本讲目标

本讲把前面几讲攒下的零件拼成一条完整的「图文 prefill」流水线。读完本讲，你应当能够：

- 说清楚 `ncnn_llm_gpt::prefill(input_text, bgr, ctx)` 这个带图像、带历史上下文的重载，与纯文本 `prefill(input_text)` 在**每一步**上的差异。
- 解释图像分支为什么会改变四件事：**视觉特征提取与嵌入注入**、**mRoPE 多轴位置编码**、**position_id 的推进量**、**带历史 KV 的因果掩码维度**。
- 理解 `clone_ctx` 在多轮对话里保护调用方上下文的作用。
- 能对照 `cli_runner` 的首轮带图分支，手动跟踪一次真实的 VLM 推理。

一句话定位：本讲不是讲「怎么识别一张图」，而是讲「图像进入主推理链路之后，prefill 这一步在工程上做了哪些额外处理」。

## 2. 前置知识

本讲默认你已经读过：

- **u2-l3 纯文本 prefill**：知道 prefill 的两段式设计（主体 N-1 个 token 跑 decoder 只取 KV cache，末位 token 单独走一遍取 hidden state），以及因果掩码用 `-1e38f` 屏蔽未来。
- **u2-l5 ctx 与多轮对话**：知道 `ncnn_llm_gpt_ctx` 把 `kv_cache`、`cur_token`、`position_id` 打包成「会话状态」，`clone()` 做浅拷贝、读旧写新。
- **u4-l2 视觉 RoPE**：知道 mRoPE 把 head_dim 的 d/2 个旋转对按 `mrope_section` 切成 t/h/w 三段，分别承载时间/高度/宽度三个位置轴。
- **u5-l1 ~ u5-l3**：知道 `vision_type` 三值枚举、`get_visiual_features` 如何把 patch 像素变成 `image_embeds`、`inject_image_embeds` 如何用 H 个真实图像嵌入替换单个 `<|image_pad|>` 占位符。

下面用到的两个术语再强调一次：

- **KV cache**：每层 Transformer 的 K、V 矩阵，类型是 `vector<pair<ncnn::Mat, ncnn::Mat>>`，长度等于层数 `attn_cnt`。prefill 阶段它被「追加」，decode 阶段被「读旧值、原地覆盖」（详见 u2-l3/u2-l4）。
- **position_id**：RoPE 用的位置下标。纯文本里每个 token 推进 1；图像分支里图像 token 在「时间轴」上被压缩，推进量不同（这是本讲的重点之一）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 三个 `prefill` 重载都在这里：纯文本首发、带 ctx 的纯文本多轮、本讲的带图像多轮。还有 `get_visiual_features` 视觉前端。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `ncnn_llm_gpt_ctx` / `ncnn_llm_gpt_base_ctx` / `qwen3_5_ctx` 三个上下文类与 `clone()` 实现。 |
| [src/utils/rope_embed.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp) | `inject_image_embeds`、`generate_rope_embed_cache_vision_mrope`、`generate_rope_embed_cache_vision_mrope_interleaved` 等本讲用到的位置编码函数。 |
| [examples/llm_ncnn_run/cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | `run_cli` 多轮对话循环，首轮带图走的就是本讲的 `prefill(text, bgr, ctx)`。 |

## 4. 核心概念与源码讲解

本讲的三个 `prefill` 重载签名（[src/ncnn_llm_gpt.h:150-152](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L150-L152)）：

```cpp
std::shared_ptr<ncnn_llm_gpt_ctx> prefill(const std::string& input_text) const;                                   // 纯文本首发
std::shared_ptr<ncnn_llm_gpt_ctx> prefill(const std::string& input_text, const ncnn::Mat& bgr, const std::shared_ptr<ncnn_llm_gpt_ctx> ctx) const; // 本讲：图文多轮
std::shared_ptr<ncnn_llm_gpt_ctx> prefill(const std::string& input_text, const std::shared_ptr<ncnn_llm_gpt_ctx> ctx) const;                       // 纯文本多轮
```

本讲聚焦第二个。它的全部实现见 [src/ncnn_llm_gpt.cpp:438-637](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L438-L637)。

### 4.1 全景：图文 prefill 与纯文本 prefill 的差异

#### 4.1.1 概念说明

纯文本首发 `prefill(input_text)` 处理的是一段「全新的、没有历史的」文本；本讲的 `prefill(input_text, bgr, ctx)` 处理的是「一段文本 + 一张图 + 之前若干轮留下的上下文」。三者叠加，决定了这个重载比纯文本版多了四件事，而其余骨架完全相同。

#### 4.1.2 核心流程

图文 prefill 的完整步骤（行号对应 [src/ncnn_llm_gpt.cpp:438-637](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L438-L637)）：

```
1. clone_ctx(ctx)              → new_ctx（克隆历史上下文，不污染调用方）     L439
2. get_visiual_features(bgr)   → image_embeds（H 个图像嵌入）+ patch 网格宽高   L441-446   ★图像特有
3. bpe->encode + pop_back      → token_ids（去掉最后一个 token，留作末位单独处理） L448-450
4. embed_net                   → token_embed                                  L452-458
5. inject_image_embeds         → 把单个 <|image_pad|> 展开成 H 个真实图像嵌入   L460-461   ★图像特有
6. 选 RoPE cache：
     - 无图 → 基础 RoPE，pos += token_ids.size()
     - Qwen3.5-VL → 交错 mRoPE                              L463-474   ★图像特有
     - 其他 VLM → 标准 mRoPE
     pos += token_ids.size() - H + (num_patches_w/merge)    ★推进量不同
7. 构造因果掩码（宽度含历史 KV 行数）                          L476-483   ★多轮特有
8. 第一段 decoder（主体 token）：
     读 cache_k%d/cache_v%d（历史）→ 写 out_cache_k%d/out_cache_v%d   L485-541
9. 末位 token：embed + RoPE + 全 0 掩码 + 第二段 decoder → out0       L543-615
10. proj_out_net → logits；argmax → next_token_id                  L617-634
11. new_ctx->cur_token = next_token_id；return new_ctx             L635-636
```

与纯文本首发 [prefill(input_text)](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L248-L436) 的对比：

| 环节 | 纯文本首发 | 图文多轮（本讲） |
| --- | --- | --- |
| 起点 | 从零创建 ctx | `clone_ctx(ctx)` 克隆历史 |
| 视觉前端 | 无 | `get_visiual_features` + `inject_image_embeds` |
| RoPE 变体 | 基础/NTK/YaRN/LongRoPE（单轴） | 无图时退回单轴；有图时 mRoPE 或交错 mRoPE（多轴） |
| position_id 起点 | 0 | `new_ctx->position_id`（继承历史） |
| position_id 推进 | 每个 token +1 | 图像段时间轴被压缩（见 4.4） |
| 掩码形状 | (N, N) 方阵 | (N, N + 历史KV行数)，前若干列是历史 |
| 第一段 decoder 读 cache | 不读（kv_cache 从空开始） | 读历史 `cache_k%d`/`cache_v%d` |

打 ★ 的就是本讲要展开的四个「图像/多轮特有」环节，对应 4.2–4.5 四个最小模块。

#### 4.1.3 源码精读

整个重载的开头与收尾：

[src/ncnn_llm_gpt.cpp:438-450](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L438-L450) —— 克隆上下文、提取视觉特征、分词并弹出末位 token：

```cpp
std::shared_ptr<ncnn_llm_gpt_ctx> ncnn_llm_gpt::prefill(...) const {
    std::shared_ptr<ncnn_llm_gpt_ctx> new_ctx = clone_ctx(ctx);   // ① 克隆
    ncnn::Mat image_embeds;
    int num_patches_w = 0, num_patches_h = 0;
    get_visiual_features(bgr, image_embeds, num_patches_w, num_patches_h);  // ② 视觉前端
    const int image_embeds_size = image_embeds.h;

    auto token_ids = bpe->encode(input_text, false, false);
    int last_token_id = token_ids.back();
    token_ids.pop_back();                                          // ③ 留末位
```

收尾的两段 decoder 之后是投影与贪心解码（这部分与纯文本完全相同），见 [src/ncnn_llm_gpt.cpp:617-636](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L617-L636)：`proj_out_net` 把末位 hidden state 投影成 logits，再 argmax 取最大值下标写入 `new_ctx->cur_token`。注意 prefill 末尾**只做贪心 argmax，不采样**（采样是 `generate` 的事，见 u2-l4）。

#### 4.1.4 代码实践

**目标**：建立「骨架相同、四处不同」的整体印象。

**步骤**：
1. 打开 [src/ncnn_llm_gpt.cpp:438](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L438) 的图文 prefill，再并排打开 [src/ncnn_llm_gpt.cpp:248](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L248) 的纯文本 prefill。
2. 在笔记里把两者逐行对齐，标出 4.1.2 表格中打 ★ 的四行。
3. 确认收尾的 `proj_out` + argmax 两段在两个版本里逐字相同。

**观察现象 / 预期结果**：你会发现两个函数约 70% 的代码（embed、两段 decoder、proj_out、argmax）结构一致，差异集中在开头（克隆 + 视觉）和中间（RoPE + 掩码）。这就是「跨模态共享 decoder 运行时」的体现。

#### 4.1.5 小练习与答案

**练习**：图文 prefill 末尾为什么只用 argmax，而不像 `generate` 那样做 temperature/top_p 采样？

**参考答案**：prefill 的职责是「吃进上下文、吐出第一个 token」，把会话状态推进到可以交给 `generate` 续写的位置。第一个 token 用贪心 argmax 保证可复现、且不引入采样随机性；真正的多样化解码（temperature、top_k、top_p、repetition penalty）由后续的 `generate` 循环负责（见 u2-l4、u3-l4）。

---

### 4.2 clone_ctx：克隆上下文，保护多轮对话

#### 4.2.1 概念说明

`clone_ctx` 是本讲重载的第一行。它的作用是：**先克隆调用方传入的 ctx，再在副本上修改，原 ctx 保持不变**。这样调用方手里那份「历史上下文」不会被这次 prefill 污染，从而支持「同一历史分叉出多条对话」「失败回退」等场景。这是 u2-l5 讲过的多轮安全机制，在图文 prefill 里被原样复用。

#### 4.2.2 核心流程

```
prefill(text, bgr, ctx)
  └─ new_ctx = clone_ctx(ctx)        // 副本
       └─ ctx->clone()                // 虚函数分发到具体子类
            ├─ base_ctx::clone()      // 拷贝 kv_cache、cur_token、position_id
            └─ qwen3_5_ctx::clone()   // 额外拷贝 sconv_cache、gdr_cache
  └─ 后续所有写操作都作用于 new_ctx
  └─ return new_ctx                   // 调用方拿到新 ctx，旧 ctx 不变
```

克隆的安全性来自两层：`ncnn::Mat` 是引用计数的浅拷贝（共享数据块、不复制像素），配合 decoder「读旧 cache、写新分配」的模式，使得副本与原件在 KV cache 上共享只读的旧数据、各自拥有新写入的数据，互不干扰。

#### 4.2.3 源码精读

[src/ncnn_llm_gpt.cpp:5-7](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L5-L7) —— `clone_ctx` 只是一层转调：

```cpp
static std::shared_ptr<ncnn_llm_gpt_ctx> clone_ctx(const std::shared_ptr<ncnn_llm_gpt_ctx>& src) {
    return src->clone();
}
```

真正的克隆逻辑在两个子类的 `clone()`。纯 Transformer 用 [ncnn_llm_gpt_base_ctx::clone()](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L56-L69)，逐层浅拷贝 KV cache 并复制两个标量：

```cpp
dst->kv_cache[i].first  = kv_cache[i].first;   // ncnn::Mat 浅拷贝
dst->kv_cache[i].second = kv_cache[i].second;
dst->cur_token   = cur_token;
dst->position_id = position_id;
```

Qwen3.5 混合架构用 [qwen3_5_ctx::clone()](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L71-L89)，额外多拷两份自定义算子状态：

```cpp
dst->sconv_cache = sconv_cache;
dst->gdr_cache   = gdr_cache;
```

调用点在 [src/ncnn_llm_gpt.cpp:439](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L439)。

#### 4.2.4 代码实践

**目标**：验证「prefill 不污染输入 ctx」。

**步骤**（源码阅读型）：
1. 在 [src/ncnn_llm_gpt.cpp:439](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L439) 确认 `new_ctx` 是克隆产物。
2. 跟踪后续所有 `new_ctx->kv_cache[i] = ...`（如 L522、L594）和 `new_ctx->position_id += ...`（L466/L473/L553），确认它们都写在 `new_ctx` 上，从不碰 `ctx`。
3. 对照 `cli_runner` 的多轮循环 [examples/llm_ncnn_run/cli_runner.cpp:61](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L61) 与 [:67](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L67)：每轮都用返回值覆盖 `ctx`。

**预期结果**：每轮 `ctx = model.prefill(...)` 拿到的是新副本，上一轮的 ctx 被替换但从未被原地修改。若想在某一轮「回到上一轮」，只要保留旧 `ctx` 指针即可——这是克隆带来的会话分叉能力。

#### 4.2.5 小练习与答案

**练习 1**：如果把第一行 `clone_ctx(ctx)` 改成直接用 `ctx`，多轮对话会出什么问题？

**参考答案**：每次 prefill 会原地改写 `ctx->kv_cache` 和 `ctx->position_id`。一旦某一轮推理失败或想回退，历史就已被覆盖、无法恢复；也无法从同一历史分叉出两条不同的对话。

**练习 2**：为什么 `qwen3_5_ctx::clone()` 要比 base 版多拷 `sconv_cache`/`gdr_cache`？

**参考答案**：Qwen3.5 混合架构除了标准注意力 KV cache，还有 ShortConv 与 GatedDeltaRule 两个自定义算子的内部状态（见 u7-l4），它们同样是「会话状态」的一部分，跨轮必须随 ctx 一起保存，否则下一轮前向会读到错误的状态。

---

### 4.3 视觉特征提取与图像嵌入注入：get_visiual_features + inject_image_embeds

#### 4.3.1 概念说明

这是图像分支最核心的两步。`get_visiual_features` 把一张 BGR 图像（u5-l2 切好的 patch → u5-l3 的 patch embed + ViT）变成 `image_embeds`——一个形状为 `(hidden_dim, H)` 的矩阵，H 是图像 token 数。`inject_image_embeds` 再把这 H 个图像嵌入，「塞进」文本 token 序列里那个唯一的 `<|image_pad|>` 占位符的位置，把一条「文本序列」变成「图文混合序列」。

u5-l3 已经分别讲过这两个函数内部；本讲只关注它们在 prefill 主链路里的**接线方式与产物**。

#### 4.3.2 核心流程

```
bgr (ncnn::Mat)
  └─ get_visiual_features
       ├─ 空 bgr → image_embeds 清空，patch 宽高归 0，提前返回（兼顾纯文本）
       └─ 否则 → resize/切 patch/reorder/ViT → image_embeds (hidden_dim, H)
            同时输出 num_patches_w、num_patches_h（patch 网格列/行数）
  └─ token_ids (含 1 个 <|image_pad|>) + token_embed (N-1, hidden_dim)
  └─ inject_image_embeds
       ├─ 找到 <|image_pad|> 下标 i = image_pad_index
       ├─ 删掉这 1 个占位，原位插入 H 个图像嵌入
       └─ token_ids 变长 (N-1) → (N-1-1+H)，token_embed 同步变长
  └─ 产物：图文混合的 token_embed，供 decoder 使用；image_pad_index 供 4.4 的 mRoPE 分轴
```

一个关键设计：文本里**只放一个** `<|image_pad|>`，inject 时再展开成 H 个。这样 prompt 模板写得简短（见 `cli_runner` 里 `"<|vision_start|><|image_pad|><|vision_end|>" + input`），实际序列长度由真实图像尺寸决定。

#### 4.3.3 源码精读

prefill 里的调用与产物取用，[src/ncnn_llm_gpt.cpp:441-461](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L441-L461)：

```cpp
ncnn::Mat image_embeds;
int num_patches_w = 0, num_patches_h = 0;
get_visiual_features(bgr, image_embeds, num_patches_w, num_patches_h);  // → image_embeds (hidden_dim, H)
const int image_embeds_size = image_embeds.h;                           // H

auto token_ids = bpe->encode(input_text, false, false);
int last_token_id = token_ids.back(); token_ids.pop_back();
ncnn::Mat input_ids_mat = ...; 
ncnn::Mat token_embed;
{ /* embed_net: token_ids → token_embed */ }

int image_pad_index = -1;
inject_image_embeds(token_ids, token_embed, image_pad_index, image_pad_id, image_embeds);
```

注意三个产物会被后续环节消费：
- `image_embeds_size`（= H）→ 4.4 的 position_id 推进公式要用。
- `image_pad_index`（占位符在序列里的下标）→ 4.4 的 mRoPE 要用它切分「图像前 / 图像内 / 图像后」三段。
- `num_patches_w` → 4.4 的图像行数换算 `num_patches_w / spatial_merge_size`。

`get_visiual_features` 本体在 [src/ncnn_llm_gpt.cpp:1158-1232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1158-L1232)。开头有一段对空图的早返回（[:1159-1164](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1159-L1164)）——这是同一个 prefill 重载能同时服务「带图」和「不带图」的关键：bgr 为空时 `image_embeds` 保持空，下游 4.4 的 RoPE 分支检测到 `image_embeds.empty()` 就退回纯文本路径。

`inject_image_embeds` 的实现在 [src/utils/rope_embed.cpp:302-335](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L302-L335)，核心是「定位占位符 → 三段 memcpy 拼接」：

```cpp
if (token_ids[i] == image_pad_id) {
    image_pad_index = i;
    // token_ids：[0,i) 原样 | [i, i+H) 全填 image_pad_id | [i+1, end) 后移
    memcpy(... i * sizeof(int));
    memset(... + i, image_pad_id, image_embeds.h * sizeof(int));
    memcpy(... + i + image_embeds.h, ... (token_ids.size()-1-i) * sizeof(int));
    // token_embed 同样三段拼接，中间换成 image_embeds
    break;   // 只替换第一个占位符
}
```

#### 4.3.4 代码实践

**目标**：看清「单占位符展开成 H 个」前后序列长度的变化。

**步骤**（源码阅读型）：
1. 在 [src/utils/rope_embed.cpp:310-311](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L310-L311) 读两行容量公式：注入后 token 数 = `token_ids.size() - 1 + image_embeds.h`，embedding 行数 = `token_embed.h - 1 + image_embeds.h`。两者都体现「删 1 个占位、插 H 个真实嵌入」。
2. 假设一段 prompt 编码后（pop_back 之前）有 20 个 token，其中含 1 个 `<|image_pad|>`，图像产生 H=256 个嵌入。手算 inject 之后 `token_ids` 的长度。
3. 在 `cli_runner` 首轮带图分支 [examples/llm_ncnn_run/cli_runner.cpp:57-61](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L57-L61) 确认 prompt 模板里确实只有一个 `<|image_pad|>`。

**预期结果**：第 2 步，pop_back 后 19 个 token（含 1 占位），inject 后 = 19 - 1 + 256 = 274。序列因图像而显著变长，这正是 VLM 推理比纯文本慢的主要原因之一。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inject_image_embeds` 里 `token_ids` 展开后，中间 H 个位置填的还是 `image_pad_id`，而不是真实的图像 token id？

**参考答案**：图像信息已经以**向量形式**（image_embeds）写进了 `token_embed`；decoder 真正消费的是 `token_embed`（`in0`）。`token_ids` 在 inject 之后只用于「撑对长度」，方便后续按 `token_ids.size()` 分配 RoPE cache 和掩码维度，其具体取值不再参与前向计算，所以填占位符 id 即可。

**练习 2**：`get_visiual_features` 对空 bgr 的早返回，对整个 prefill 有什么意义？

**参考答案**：它让 `prefill(text, bgr, ctx)` 这个重载成为「图文两用」入口——传空图就退化为纯文本多轮 prefill，下游 4.4 的 `image_embeds.empty()` 分支与 4.1 对比表里的「无图」列对应。代码不用为「无图」单独再写一个函数。

---

### 4.4 mRoPE cache 与 position_id 推进（图像分支特有）

#### 4.4.1 概念说明

图像 patch 是二维网格，单轴递增的 position_id 无法区分「水平相邻」与「垂直相邻」，所以 VLM 用多轴 mRoPE（u4-l2）。本讲关注的是 prefill 主链路里**如何选择 mRoPE 函数**，以及一个更微妙的问题：**图像分支的 position_id 推进量与纯文本不同**。

核心直觉：图像 H 个 token 在「时间轴 t」上不各占一步，而是被压缩成「图像行数」步——因为同一行的 token 共享同一个时间位置，靠高度轴 h、宽度轴 w 区分。所以 position_id 的推进量不是 H，而是 `num_patches_w / spatial_merge_size`（合并后的行数）。

#### 4.4.2 核心流程

[src/ncnn_llm_gpt.cpp:463-474](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L463-L474) 的 RoPE 选择逻辑：

```
if (image_embeds 为空):                       // 纯文本退化
    基础 RoPE（单轴），position_id += token_ids.size()
else:
    if vision_type == QWEN3_5_VL:
        交错 mRoPE（hardcoded {11,11,10}, partial_rotary 0.5）
    else:
        标准 mRoPE（按 mrope_section 切 t/h/w）
    position_id += token_ids.size() - image_embeds_size + (num_patches_w / spatial_merge_size)
```

图像分支的推进量公式：

\[
\Delta\text{pos} \;=\; \underbrace{(|T| - H)}_{\text{本批非图像 token 数}} \;+\; \underbrace{\bigl\lfloor n_w / m \bigr\rfloor}_{\text{合并后图像行数}}
\]

其中 \(|T|\) 是 inject 之后的 `token_ids.size()`（含 H 个图像 token），\(H\) 是 `image_embeds_size`，\(n_w\) 是 `num_patches_w`，\(m\) 是 `spatial_merge_size`。前一项让每个文本 token 各占 1 步；后一项让整块图像在时间轴上只占「行数」步。

这与 mRoPE 函数内部对每个 token 的位置赋值一致（见下方源码）：图像 token 在 t 轴恒为 `image_pad_index`（同一行同一时间），h 轴按行号、w 轴按列号区分。

#### 4.4.3 源码精读

[src/ncnn_llm_gpt.cpp:463-474](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L463-L474) —— RoPE 三分支与推进量：

```cpp
ncnn::Mat cos_cache, sin_cache;
if (image_embeds.empty()) {
    generate_rope_embed_cache(token_ids.size(), rope_head_dim, new_ctx->position_id, cos_cache, sin_cache, rope_theta);
    new_ctx->position_id += token_ids.size();
} else {
    if (vision_type == Vision_Type::VISION_QWEN3_5_VL) {
        generate_rope_embed_cache_vision_mrope_interleaved(token_ids.size(), rope_head_dim, new_ctx->position_id,
            image_pad_index, image_embeds_size, num_patches_w, cos_cache, sin_cache, rope_theta);
    } else {
        generate_rope_embed_cache_vision_mrope(token_ids.size(), rope_head_dim, new_ctx->position_id,
            image_pad_index, image_embeds_size, num_patches_w, spatial_merge_size, mrope_section, cos_cache, sin_cache, rope_theta);
    }
    new_ctx->position_id += token_ids.size() - image_embeds_size + (num_patches_w / spatial_merge_size);
}
```

标准 mRoPE 内部对位置轴的赋值，[src/utils/rope_embed.cpp:367-396](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L367-L396)：

```cpp
int pos = position_id;
if (i < image_pad_index) {                       // 图像前：文本，逐 token +1
    pos += i;
} else if (i >= image_pad_index + image_embeds_size) {  // 图像后：跳过 H、补回行数
    pos += i - image_embeds_size + (num_patches_w / spatial_merge_size);
} else {                                         // 图像内：按 mrope_section 分轴
    if (j < mrope_section[0])            pos += image_pad_index;              // t 轴：恒定
    else if (j < mrope_section[0]+mrope_section[1]) pos += image_pad_index + hid;  // h 轴：行号
    else                                  pos += image_pad_index + wid;        // w 轴：列号
}
```

可以看到，「图像后」的文本 token 位置跳变量 `-H + 行数` 与外层 position_id 推进公式完全对应——二者是同一个思想在「逐 token 赋值」和「批量推进」两个层面的体现。

`vision_type` 本身来自构造函数对 `model.json` 的解析，见 [src/ncnn_llm_gpt.cpp:175-185](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L175-L185)：`setting.vision.type` 为 `"qwen3.5_vl"` 时置 `VISION_QWEN3_5_VL`，`"vit"` 时置 `VISION_VIT`，其余（含默认 `"close"`）保持 `VISION_CLOSE`。

> 补充：`generate_rope_embed_cache_vision_mrope_interleaved`（[src/utils/rope_embed.cpp:161-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L161-L245)）用于 Qwen3.5-VL，它把维度按 (t,h,w) 三元组**交错**排列、硬编码 `{11,11,10}`、`merge_size=2` 且只旋转一半维度（`partial_rotary_factor=0.5`，未旋转的维度 cos=1/sin=0）。这与标准 mRoPE 的「连续分段」不同，但对 position_id 推进公式的影响一致（仍是行数压缩）。

#### 4.4.4 代码实践

**目标**：手算一次图像分支的 position_id 推进，验证「压缩」直觉。

**步骤**（源码阅读 + 手算）：
1. 设想一张图经 `get_image_size_for_patches` 后 patch 网格为 8 列 × 8 行（`num_patches_w=8, num_patches_h=8`），`spatial_merge_size=2`，ViT 输出 `image_embeds_size = 8*8/4 = 16`（2×2 合并后）。
2. 设 inject 后 `token_ids.size() = 50`（含这 16 个图像 token）。
3. 套公式：推进量 = 50 - 16 + (8/2) = 34 + 4 = 38。
4. 对照纯文本：同样 50 个 token 会推进 50。图像分支少推进了 12（= 16 - 4），正好是「H 个图像 token 压缩成 4 行」省下的步数。

**观察现象 / 预期结果**：图像分支的 position_id 推进量永远满足 `推进量 = 文本token数 + 图像行数`，而图像行数 `num_patches_w/spatial_merge_size` 远小于 H，所以图像在时间轴上被显著压缩。

> 第 1 步里 `image_embeds_size` 与 patch 网格、merge 的精确换算依赖 ViT/merger 的具体实现，若你手头的模型 merger 不同，请以本地实际打印值为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么图像 token 在时间轴 t 上要「压缩」，而不是每个 token 各占 1 步？

**参考答案**：图像的二维结构主要由 h/w 轴表达；如果 t 轴也让 H 个 token 各占一步，等于把「空间相邻」强行变成「时间先后」，会破坏 RoPE「相对位置编码注意力」的语义（同一行不同列的 patch 会获得很大的相对距离）。压缩成行数步、同一行共享 t 值，才能让注意力正确反映二维拓扑。

**练习 2**：`mrope_section` 这个配置在哪里读进来？为什么标准 mRoPE 需要它、交错 mRoPE 不需要？

**参考答案**：`mrope_section` 在构造函数里从 `setting.rope.mrope_section` 读入（[src/ncnn_llm_gpt.cpp:238](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L238)）。标准 mRoPE 用它决定 t/h/w 三段各占多少个连续维度；交错 mRoPE（Qwen3.5-VL）按 (t,h,w) 三元组循环排列，段宽固定为 `{11,11,10}` 并在代码里硬编码（[:172](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L172)），所以不需要外部配置。

---

### 4.5 带历史 KV 的因果掩码构造

#### 4.5.1 概念说明

掩码（mask）告诉注意力机制「每个 token 可以看到哪些位置」。纯文本首发用一个 N×N 的因果方阵（下三角可见、未来屏蔽）；本讲因为带历史 ctx，掩码要**把历史 KV 也算进可见范围**——宽度变成 `N + 历史KV行数`，前若干列是历史（全部可见），后 N 列才是本批新 token（彼此因果）。

这个「带历史 KV 的掩码」并不是图像特有的，它和纯文本多轮 `prefill(text, ctx)` 完全同构——只要进了多轮，掩码就得这么构造。本讲把它和图像分支放一起讲，是因为 VLM 几乎总是以多轮方式使用（先 system prompt，再图文轮）。

#### 4.5.2 核心流程

设本批新 token 数为 N（即 inject 后的 `token_ids.size()`），历史 KV 行数为 K（`new_ctx->kv_cache[0].first.h`）。

```
掩码形状：(N + K) 列 × N 行
初始化全 0（所有位置默认可见）
对第 i 行（第 i 个新 token）：
    屏蔽列 j ∈ [K + i + 1, N + K)   →  这些是「同批里排在 i 之后」的新 token（未来）
    列 j ∈ [0, K]                  →  全部历史，可见（保持 0）
    列 j ∈ [K, K + i]              →  同批里排在前面的新 token，可见（保持 0）
屏蔽值用 -1e38f（softmax 后趋近于 0）
```

几何上，这是一条「从左上角历史块开始、向右下延伸的因果下三角」，历史块是一整块全 0（任何新 token 都能看完所有历史），新 token 块内部才是因果的。

末位 token 的第二次 decoder 用的是一个更简单的全 0 掩码 `[K+N+1, 1]`（见 [:555-556](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L555-L556)）：因为末位 token 恒为序列最末，因果性天然满足，没有任何位置需要屏蔽。

#### 4.5.3 源码精读

[src/ncnn_llm_gpt.cpp:476-483](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L476-L483) —— 第一段 decoder 的掩码：

```cpp
ncnn::Mat mask((int)token_ids.size() + new_ctx->kv_cache[0].first.h, (int)token_ids.size());
mask.fill(0.0f);
for (int i = 0; i < (int)token_ids.size(); i++) {
    float* row = mask.row(i);
    for (int j = new_ctx->kv_cache[0].first.h + i + 1; j < (int)token_ids.size() + new_ctx->kv_cache[0].first.h; j++) {
        row[j] = -1e38f;
    }
}
```

- `mask` 构造：`ncnn::Mat(列数, 行数)`，列数 = N + K，行数 = N。
- 屏蔽起点 `K + i + 1`：跳过历史 K 列，再跳过自己和前面的 i 个本批 token。
- 屏蔽终点 `N + K`：本批最末。

把这段和纯文本多轮 `prefill(text, ctx)` 的掩码 [src/ncnn_llm_gpt.cpp:669-676](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L669-L676) 并排看，你会发现**逐字相同**——这印证了「带历史 KV 的掩码」是多轮机制，与是否有图无关。差异只在 N 的来源（图像分支的 N 是 inject 之后、可能很大的长度）。

掩码构造好之后，第一段 decoder 把它作为 `in1` 输入，同时把历史 KV 作为 `cache_k%d`/`cache_v%d` 输入、把更新后的 KV 作为 `out_cache_k%d`/`out_cache_v%d` 输出，见 [src/ncnn_llm_gpt.cpp:485-523](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L485-L523)。Qwen3.5 还会额外收发 `cache_conv%d`/`cache_gdr%d`（[:501-540](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L501-L540)）。

#### 4.5.4 代码实践

**目标**：亲手画出一个「带历史 KV」的因果掩码，理解它的形状。

**步骤**（纸笔 + 源码）：
1. 设 K=10（历史 KV 行数）、N=5（本批新 token 数）。
2. 画一个 5 行 × 15 列的格子，全部填 0。
3. 对第 i 行（i=0..4），把列 j ∈ [10+i+1, 15) 涂黑（屏蔽）。
4. 数一数：第 0 行屏蔽 4 格（列 11-14），第 4 行屏蔽 0 格。
5. 对照 [src/ncnn_llm_gpt.cpp:478-482](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L478-L482) 确认循环边界与你的涂色一致。
6. 再把 K=0 的特例画一遍，确认它正好退化成纯文本首发的 N×N 因果方阵（[src/ncnn_llm_gpt.cpp:276-283](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L276-L283)）。

**预期结果**：历史块（前 10 列）永远是全 0 的「可见带」；新 token 块（后 5 列）是因果下三角。K=0 时整个掩码就是标准因果方阵，说明首发 prefill 只是其 K=0 的特例。

#### 4.5.5 小练习与答案

**练习 1**：为什么历史块（前 K 列）必须是全 0，而不能也加因果屏蔽？

**参考答案**：历史 KV 是**之前轮次已经生成、且已经互相注意过**的 token。本轮的任何一个新 token 都应当能看到全部历史（不然模型就「失忆」了），所以历史列对所有新 token 都可见（0）。因果性只约束「本批新 token 之间」的相对顺序。

**练习 2**：末位 token 第二次 decoder 用的掩码是全 0 向量（[:555-556](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L555-L556)），为什么不需要屏蔽？

**参考答案**：末位 token 在「历史 + 本批 + 自己」里排在最末，它能看到所有位置都不违反因果性（没有任何位置是「它的未来」），所以掩码全 0、无需屏蔽。

---

## 5. 综合实践

把本讲四个模块串起来，做一次端到端的「图文 prefill vs 纯文本 prefill」对照实践。

**实践目标**：用 `prefill(input_text, bgr, ctx)` 跑一次 VLM 推理，并逐环节列出图像分支比纯文本多做了什么。

**操作步骤**：

1. **准备模型与图**：按 u1-l2/u1-l5 在 `assets/` 下放好一个 VLM 模型目录（`setting.vision.type` 为 `vit` 或 `qwen3.5_vl`），并准备一张测试图。
2. **构建并运行**：`xmake build llm_ncnn_run`，然后 `xmake run llm_ncnn_run <model_path> --image <img_path>`（具体选项以 `options.cpp` 为准）。
3. **跟踪首轮带图分支**：对照 [examples/llm_ncnn_run/cli_runner.cpp:57-61](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L57-L61)，确认首轮走的是 `prefill(user_message, image, ctx)`，其中 `user_message` 含 `<|vision_start|><|image_pad|><|vision_end|>`。
4. **源码侧手算**：在你这张图的实际 patch 网格下，按 4.4.4 的方法算出 `image_embeds_size`、inject 后的序列长度、position_id 推进量、掩码宽度（N+K）。
5. **对比纯文本**：用同一模型、同一句输入、不传 `--image` 再跑一次（此时走 `prefill(text, ctx)`，[:67](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L67)）。

**需要观察的现象 / 预期结果**：填一张对照表——

| 环节 | 纯文本 `prefill(text,ctx)` | 图文 `prefill(text,bgr,ctx)` |
| --- | --- | --- |
| 是否提取视觉特征 | 否 | 是，`get_visiual_features` |
| 是否注入图像嵌入 | 否 | 是，`<|image_pad|>` 展开为 H 个 |
| RoPE cache 选择 | 单轴（基础/NTK/YaRN/LongRoPE） | 多轴 mRoPE（或交错 mRoPE） |
| position_id 推进量 | token_ids.size() | token_ids.size() − H + (num_patches_w/merge) |
| 掩码宽度 | N + K | N + K（同构，但 N 因注入 H 而更大） |

> 若本地暂无可用 VLM 权重，步骤 2 的实际运行可标注「待本地验证」，但步骤 3–5 的源码跟踪与手算完全可以独立完成，是本实践的核心。

## 6. 本讲小结

- 图文 `prefill(text, bgr, ctx)` 与纯文本首发**共享约七成代码**（embed、两段 decoder、proj_out、argmax），差异集中在四处：视觉前端、RoPE 选择、position_id 推进、掩码。
- 第一行 `clone_ctx(ctx)` 保证多轮对话中调用方的历史上下文不被污染，是会话分叉与回退的基础；`ncnn::Mat` 浅拷贝 + 「读旧写新」使其安全且廉价。
- `get_visiual_features` 产出 `image_embeds (hidden_dim, H)` 与 patch 网格；`inject_image_embeds` 把文本里唯一的 `<|image_pad|>` 展开成 H 个真实图像嵌入，序列因此变长。
- 图像分支用多轴 mRoPE（标准或交错），图像 token 在时间轴上被压缩成「行数」步，故 position_id 推进量是 `token_ids.size() − H + num_patches_w/spatial_merge_size`，而非简单的逐 token +1。
- 「带历史 KV 的因果掩码」是多轮机制（非图像特有）：宽度 = N + 历史 KV 行数，历史块全 0 可见、本批块因果下三角；它与纯文本多轮 `prefill(text, ctx)` 的掩码逐字相同。
- prefill 末尾只做贪心 argmax 产出首个 token，多样化采样交给 `generate`；返回的 `new_ctx` 携带更新后的 KV cache、`cur_token`、`position_id` 交给 `generate` 续写。

## 7. 下一步学习建议

- **进入 OCR / ASR 模态**：u6-l3（GLM-OCR 与 HunyuanOCR）和 u6-l4（Qwen3-ASR）会复用本讲的「注入占位 + 共享 decoder」模式，只是把图像嵌入换成 strip 图像嵌入或音频嵌入，值得对照阅读 `src/ncnn_llm_ocr.cpp`、`src/ncnn_llm_asr.cpp`。
- **深入多轴 RoPE**：若 4.4 的位置压缩还没完全想透，建议回到 u4-l2，把 `mrope_section`、`xdrope_section`、交错 mRoPE 的赋值代码再手推一遍。
- **工具调用回填上下文**：`generate` 里的工具调用会内部再调 `prefill` 把工具结果回填进 ctx（[src/ncnn_llm_gpt.cpp:864](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L864)），这条「prefill → generate → 再 prefill」的链路将在 u7-l2 展开。
- **跑 benchmark**：u8-l2 的 `benchllm` 可以量化「带图」比「不带图」慢多少，把本讲 4.3.4 关于「序列变长导致变慢」的直觉变成数字。
