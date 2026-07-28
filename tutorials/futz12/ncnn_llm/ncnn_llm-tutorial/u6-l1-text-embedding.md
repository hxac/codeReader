# 文本嵌入 API

## 1. 本讲目标

本讲聚焦 `ncnn_embedding` 这条「文本嵌入」运行时。学完后你应当：

- 理解什么是文本嵌入（embedding），以及它和生成式 LLM 在运行时上的根本区别。
- 掌握 `ncnn_embedding` 如何从 `model.json` 完成构造与配置加载。
- 读懂 `encode_text` / `encode_text_batch` 的完整链路：分词 → RoPE → 编码器前向 → 后处理。
- 理解 `mean_pool` 与 `normalize` 两个后处理步骤为什么对「相似度比较」不可或缺。
- 能用余弦相似度（`cosine_similarity`）比较两段文本的语义，并跑通 `embedding_main` 打印相似度矩阵。

## 2. 前置知识

### 2.1 什么是文本嵌入

生成式模型（前面 U2 讲的 `ncnn_llm_gpt`）回答「下一个词是什么」，输出是一串文本。嵌入模型回答的是「这段话在语义上像什么」，输出是一个**固定长度的浮点向量**（例如 768 维）。

直觉上：模型把语义相近的句子映射到向量空间里相近的位置。于是「今天天气很好」和「今天天气不错」的向量夹角很小，而「今天天气很好」和「我喜欢吃苹果」的夹角较大。比较两个向量方向（夹角）的工具就是**余弦相似度**：

\[
\text{sim}(a,b) = \frac{a \cdot b}{\|a\|\,\|b\|}
\]

值域为 \([-1, 1]\)，越接近 1 表示方向越一致、语义越相近。

### 2.2 与生成式运行时的区别（承接 u2-l1）

u2-l1 讲过 `ncnn_llm_base` 提供的 KV cache、`sample_logits` 等公共能力。**嵌入模型完全不需要这些**——它只做一次「前向编码」，不逐 token 自回归、不维护 KV cache、不采样。所以 `ncnn_embedding` 是一个**独立类**，并不继承 `ncnn_llm_base`：

```cpp
class ncnn_embedding {        // 注意：没有 : public ncnn_llm_base
private:
    std::shared_ptr<ncnn::Net> text_encoder_net;
    std::shared_ptr<ncnn::Net> vision_encoder_net;
    ...
};
```

它和 `ncnn_llm_gpt` 是平行的两条运行时，都构建在 ncnn 之上，只是嵌入这条线更简单：编码器跑一遍、做后处理、返回向量。

### 2.3 分词器选择（承接 u3-l1）

u3-l1 讲过 `tokenizer.type` 如何在 bpe / bbpe / unigram 之间切换。本讲你会看到这套机制在嵌入运行时里的真实落地：构造函数根据 `tokenizer.type` 决定实例化 `BpeTokenizer` 还是 `UnigramTokenizer`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_embedding.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h) | `ncnn_embedding` 类声明：成员变量、公开 API（`encode_text` 等）、私有后处理方法（`mean_pool`/`normalize`）。 |
| [src/ncnn_embedding.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp) | 全部实现：构造与配置加载、文本/图像编码、池化与归一化、图像预处理。 |
| [examples/embedding_main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp) | 最小可运行示例：加载模型、编码若干句子、打印相似度矩阵。 |

构建上，`embedding_main` 是一个依赖核心库 `ncnn_llm` 的 binary target（见 [xmake.lua:113-118](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L113-L118)），因此它直接复用 `ncnn_embedding` 的全部实现。

> 本讲只讲**文本**嵌入。同一文件里的 `encode_image` / `encode_image_file` / `preprocess_image` 属于 CLIP 多模态嵌入，留给下一讲 u6-l2。

## 4. 核心概念与源码讲解

### 4.1 ncnn_embedding 构造与配置加载

#### 4.1.1 概念说明

`ncnn_embedding` 的构造函数只做一件事：把模型目录里的 `model.json` 翻译成一组**成员变量**（网络、分词器、维度、RoPE 参数等），让后续 `encode_text` 直接使用。这和 u1-l5 讲过的 `ncnn_llm_gpt` 构造函数思路一致——「配置 → 成员变量」，只是嵌入模型要管的东西更少（没有 decoder、没有 KV cache、没有 attention 层数 `attn_cnt`）。

一个关键分支是 `model_type`：

- `"embedding"`（默认）：纯文本嵌入模型，只加载一个 text encoder。
- `"clip"`：图文双塔模型，额外加载一个 vision encoder（本讲不展开，见 u6-l2）。

#### 4.1.2 核心流程

构造函数的执行顺序（伪代码）：

```text
读 model.json
├─ model_type = config["model_type"] 或 "embedding"
├─ 创建 text_encoder_net；若 model_type=="clip" 再创建 vision_encoder_net
├─ 设置 num_threads（若 > 0）
├─ 若 use_vulkan：设置 vulkan_device_index + bf16 storage 等精度开关
├─ 加载 encoder 的 .param / .bin（clip 用 text_encoder_*，其它用 encoder_*）
├─ 加载分词器：tokenizer.type ∈ {unigram, bpe, bbpe}
├─ 读 setting：embed_dim / image_size / max_seq_len / image_mean/std / rope
└─ 任何异常 → ok_ = false
```

注意：所有配置读取都被包在 `try { ... } catch (...)` 里，失败只置 `ok_ = false`，不抛异常。外部通过 `ok()` 查询是否加载成功（这是 u2-l1 讲过的「单次失败、永久不可用」健康检查模式的同款用法）。

#### 4.1.3 源码精读

先看类里有哪些成员变量，这决定了配置最终落在哪里：

[ncnn_embedding 类成员变量，src/ncnn_embedding.h:21-39](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L21-L39) —— 定义了两个网络指针、两种分词器指针，以及 `model_type_`、`text_embed_dim_`（默认 768）、`rope_head_dim_`（默认 64）、`rope_theta_`（默认 100000）、`max_seq_len_`（默认 512）和一组图像预处理默认值（ImageNet 的 mean/std）。

构造函数读取 `model.json` 并按 `model_type` 分流加载网络：

[src/ncnn_embedding.cpp:14-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L14-L22) —— 读 `model_type`（缺省 `"embedding"`），当且仅当 `model_type == "clip"` 时才创建 `vision_encoder_net`。这是后续 `supports_image()` 判断的依据。

网络权重路径同样按 `model_type` 取不同键名：

[src/ncnn_embedding.cpp:57-72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L57-L72) —— `clip` 读 `params.text_encoder_param/bin`，其它读 `params.encoder_param/bin`。也就是说，写 `model.json` 时纯文本嵌入模型用 `encoder_param` 键，CLIP 用 `text_encoder_param` 键。

分词器分支（承接 u3-l1）：

[src/ncnn_embedding.cpp:83-123](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L83-L123) —— 默认 `tokenizer_type = "bpe"`；若 `tokenizer.type == "unigram"`，则用 `UnigramTokenizer::LoadFromFile` 加载（带 bos/eos/pad/unk/cls/sep/mask 七个特殊令牌配置，u3-l1 讲过的 `SpecialTokensConfig`），否则用 `BpeTokenizer::LoadFromFiles`，并把 `tokenizer_type == "bbpe"` 作为最后一个参数开启字节级编码。

setting 读取：

[src/ncnn_embedding.cpp:125-173](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L125-L173) —— 这里有一个**双键兼容**的小细节值得注意：维度既可写成 `setting.embed_dim`，也可写成 `setting.text_embed_dim`（[第 125-130 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L125-L130)），两者都 `contains` 守卫、后者覆盖前者。RoPE 参数允许写在 `setting.rope.*` 子表里，也可直接写在 `setting.*` 下（[第 155-173 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L155-L173)），这是为了兼容不同来源的导出脚本。

最后是统一的异常兜底：

[src/ncnn_embedding.cpp:184-187](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L184-L187) —— 任何字段拼错、文件缺失、JSON 解析失败，都会被 catch 成 `ok_ = false`，并打印 `Error loading embedding model: ...`。

#### 4.1.4 代码实践

**实践目标**：把 `model.json` 字段和构造函数的读取代码一一对应起来。

**操作步骤**：

1. 找一个嵌入模型目录（例如默认的 `assets/jina-embeddings-v5-text-nano`，若不存在则任选一个嵌入模型目录），打开它的 `model.json`。
2. 对照 [构造函数源码 src/ncnn_embedding.cpp:6-188](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L6-L188)，为每个 JSON 字段注明它被哪一行代码读取、赋给了哪个成员变量。
3. 重点关注：`model_type` 决定了走哪条网络加载分支；`tokenizer.type` 决定了实例化哪个分词器类。

**需要观察的现象**：你会看到 `embed_dim` 与 `text_embed_dim` 两个键都能工作，且 `rope` 既可以是一级子表也可以散落在 `setting` 下。

**预期结果**：得到一张「JSON 字段 → 成员变量 → 代码行号」的对照表。若手头没有模型目录，可只做源码侧的对照，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若 `model.json` 里同时写了 `setting.embed_dim` 和 `setting.text_embed_dim`，最终 `text_embed_dim_` 取哪个值？

**答案**：取 `text_embed_dim` 的值。因为 [第 125-130 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L125-L130) 先读 `embed_dim`、后读 `text_embed_dim`，后者覆盖前者。

**练习 2**：为什么 `ncnn_embedding` 不需要 `attn_cnt`（KV cache 层数）这个配置？

**答案**：嵌入模型只跑一次前向编码、不维护 KV cache、不自回归解码，所以没有「每层一对 K/V」的概念，自然不需要 `attn_cnt`。这与 u2-l1 讲的 `ncnn_llm_base` 的 KV cache 形成对比。

---

### 4.2 encode_text / encode_text_batch：文本编码主链路

#### 4.2.1 概念说明

`encode_text` 是嵌入运行时最核心的公开 API：输入一段字符串，输出一个 `text_embed_dim_` 维的 `std::vector<float>`。它内部完成「分词 → 生成 RoPE → 编码器前向 → 后处理」四步。`encode_text_batch` 只是批量的薄包装。

#### 4.2.2 核心流程

```text
encode_text(text)
├─ 选分词器编码：unigram→encode(text, true, true)；bpe→encode(text, false, false)
├─ 空串保护：seq_len==0 返回全 0 向量
├─ 超长截断：seq_len > max_seq_len_ 时 resize
├─ 把 token_ids 填进 ncnn::Mat input_ids
├─ 生成 RoPE cos/sin cache（type=="RoPE_full" 用 full 版，否则普通版）
├─ Extractor: input in0=input_ids, in1=cos_cache, in2=sin_cache; extract out0
├─ 后处理：
│   ├─ model_type=="clip" → 直接 normalize(embeddings)
│   └─ 其它              → normalize(mean_pool(embeddings))
└─ 拷贝成 std::vector<float> 返回
```

这里复用了 u2-l2 讲过的 ncnn 调用三步模式（`create_extractor` → `input` → `extract`），只不过嵌入模型的输入插槽是 `in0/in1/in2`（token id + cos cache + sin cache），输出是 `out0`。

#### 4.2.3 源码精读

分词器选择与编码方式：

[src/ncnn_embedding.cpp:290-295](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L290-L295) —— 注意两种分词器的 `encode` 参数不同：unigram 传 `(text, true, true)`（加 bos、加 eos），bpe 传 `(text, false, false)`（不加）。这是因为这两类模型训练时的约定不同；若搞反会导致 token 序列与模型预期不一致，向量质量下降。

长度保护与截断：

[src/ncnn_embedding.cpp:297-305](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L297-L305) —— 空输入返回维度为 `text_embed_dim_` 的全 0 向量（保证返回形状稳定）；超过 `max_seq_len_`（默认 512）则直接 `resize` 截断，避免编码器超长崩坏。

生成 RoPE 并前向：

[src/ncnn_embedding.cpp:312-326](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L312-L326) —— `rope_type_ == "RoPE_full"` 时调 `generate_rope_embed_cache_full`（对全部维度旋转），否则调 `generate_rope_embed_cache`（只旋转前 `rope_head_dim_` 维，与 u4-l1 讲的基础 RoPE 一致）。两个函数的声明见 [rope_embed.h:35-37](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L35-L37)。注意 `position_id` 这里恒为 0（嵌入模型每次都从头编码，不像生成模型要续写）。

后处理分支：

[src/ncnn_embedding.cpp:333-339](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L333-L339) —— 这是非常关键的一行分叉：CLIP 模型直接对编码器输出 `normalize`（因为 CLIP 用特殊 token 位置代表整句，不做平均），其它模型先 `mean_pool` 再 `normalize`。下一节会解释为什么。

批量接口：

[src/ncnn_embedding.cpp:347-354](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L347-L354) —— `encode_text_batch` 只是对每个文本循环调用 `encode_text`，**没有做 batch 化前向**（即没有把多条文本拼成一个 batch 一次过网络）。这意味着 batch 的加速收益主要来自分词阶段的并行，而不是编码器前向。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `encode_text` 调用，看清 token id 如何变成向量。

**操作步骤**：

1. 阅读 [encode_text 源码 src/ncnn_embedding.cpp:285-345](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L285-L345)。
2. 在脑中（或纸上）用一句话标注每一步：分词 → 截断 → RoPE → `in0/in1/in2` → `out0` → 后处理。
3. 思考：`encode_text_batch` 既然没做真 batch 前向，编码 100 条文本时主要瓶颈在哪？

**需要观察的现象 / 预期结果**：你能复述出 `in0` 是 token id、`in1/in2` 是 RoPE 的 cos/sin、`out0` 是序列级的 hidden states；并指出 batch 接口只是串行循环。运行验证「待本地验证」（需准备嵌入模型目录）。

#### 4.2.5 小练习与答案

**练习 1**：`encode_text` 里 `position_id` 为什么固定传 0，而 u2-l3 里 prefill 的 position_id 要从 0 递增到 N-1？

**答案**：嵌入模型每次都把整段文本一次性编码、不复用历史状态，RoPE 表只针对当前这段从 0 开始的位置生成（表内已包含 0..seq_len-1 所有位置）。生成模型则要为「续写」维护跨步的 position_id。两者本质都是「序列内位置从 0 起」，只是嵌入模型没有跨次调用的状态。

**练习 2**：若输入文本被截断到 `max_seq_len_`，相似度比较会有什么影响？

**答案**：超长文本的尾部信息丢失，向量只反映前 `max_seq_len_` 个 token 的语义，相似度可能偏低或失真。实际使用中应让待比较的文本都控制在 `max_seq_len_` 以内。

---

### 4.3 mean_pool 与 normalize：把序列压成单位向量

#### 4.3.1 概念说明

编码器 `out0` 不是一个向量，而是一**串**向量——序列里每个 token 位置都对应一个 `text_embed_dim_` 维向量（形状可理解为 `(dim, seq_len)`）。要比两段话的相似度，必须先把这串向量压成**一个**代表整句话的向量，再做归一化。这两步就是 `mean_pool` 和 `normalize`。

- **mean_pool（平均池化）**：沿序列维度求平均，把 L 个向量合成 1 个。
- **normalize（L2 归一化）**：把向量除以它的模长，缩放到单位球面上。

数学上，对序列 \(x_{i,j}\)（\(i\) 为位置，\(j\) 为维度，序列长 \(L\)）：

\[
p_j = \frac{1}{L}\sum_{i=1}^{L} x_{i,j}
\]

\[
\hat{p}_j = \frac{p_j}{\sqrt{\sum_k p_k^2}}
\]

归一化后，两向量的点积就等于余弦相似度（因为 \(\|\hat{p}\|=1\)）：

\[
\hat{a} \cdot \hat{b} = \frac{a \cdot b}{\|a\|\,\|b\|} = \text{cos}(\theta)
\]

#### 4.3.2 核心流程

```text
mean_pool(embeddings):          # embeddings 形状 (dim=w, seq_len=h)
  pooled = 全 0 向量 (长度 dim)
  for 每一行 i in [0, seq_len):
      pooled += embeddings.row(i)
  pooled *= 1/seq_len
  return pooled

normalize(vec):
  norm = sqrt(sum(vec[i]^2))
  if norm > 1e-10:  return vec / norm
  else:             return vec      # 零向量不除，原样返回
```

#### 4.3.3 源码精读

`mean_pool` 的维度约定：

[src/ncnn_embedding.cpp:190-212](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L190-L212) —— 这里 `seq_len = embeddings.h`、`dim = embeddings.w`（ncnn::Mat 的 w 是最内层维度）。它逐行 `embeddings.row(i)` 累加到 `pooled`，最后乘 `1/seq_len`。注意它**不带 attention mask 加权**——即所有 token（含 padding）等权参与平均，因此输入端的 `max_seq_len_` 截断（而非 padding）对它更友好。

`normalize` 的零向量保护：

[src/ncnn_embedding.cpp:214-237](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L214-L237) —— 计算 L2 模长 `norm = sqrt(sum(x^2))`；只有当 `norm > 1e-10f` 时才除以 `norm`，否则原样返回。这个保护防止零向量（例如空输入或退化输出）触发除零得到 NaN。

后处理在 `encode_text` 里的组合：

[src/ncnn_embedding.cpp:333-339](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L333-L339) —— 非 CLIP 模型走 `normalize(mean_pool(embeddings))` 两步；CLIP 模型跳过 `mean_pool`，因为它用特殊 token（如 `[CLS]`/eos）位置的向量代表整句，直接归一化即可。

#### 4.3.4 代码实践

**实践目标**：直观感受 mean_pool 与 normalize 对相似度的影响。

**操作步骤**：

1. 阅读 [mean_pool src/ncnn_embedding.cpp:190-212](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L190-L212) 与 [normalize src/ncnn_embedding.cpp:214-237](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L214-L237)。
2. 思考两个对照实验（可在本地改一份临时副本验证，**不要改源码**）：
   - 若去掉 `normalize`，只保留 `mean_pool`，相似度矩阵的数值范围会如何变化？两段相近文本的相似度还会接近 1 吗？
   - 若去掉 `mean_pool`，只取序列里某一个 token 位置的向量，整句语义还能被正确表示吗？

**需要观察的现象 / 预期结果**：
- 去掉 normalize：相似度数值会被向量模长放大/缩小，不再限制在 \([-1,1]\)，长短句的比较会失真。
- 去掉 mean_pool：单个 token 向量只反映该词的局部语义，无法代表整句，相似度会变得不稳定。

运行验证「待本地验证」（需嵌入模型）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `normalize` 要对 `norm <= 1e-10` 单独处理，而不是无脑除？

**答案**：零向量（或接近零）除以接近零的模长会得到 NaN/Inf，污染后续相似度计算。源码在 `norm <= 1e-10` 时原样返回，是一种数值安全兜底。

**练习 2**：CLIP 分支为什么不做 `mean_pool`？

**答案**：CLIP 文本塔在训练时就约定用某个特殊 token 位置（如首部 `[CLS]` 或尾部 eos）的 hidden state 作为整句的表示，所以直接取该位置向量归一化即可；做平均反而会稀释这个被训练好的聚合位置的信息。

---

### 4.4 cosine_similarity 与相似度矩阵

#### 4.4.1 概念说明

有了归一化后的向量，比较语义就只剩一件事：算两个向量的余弦相似度。`examples/embedding_main.cpp` 里定义了一个独立的 `cosine_similarity` 自由函数，并把多条文本两两比较，打印成一个 N×N 的相似度矩阵。

值得一提的是：因为 `encode_text` 返回的向量**已经归一化**，`cosine_similarity` 里再算一次模长其实是冗余的（此时 \(\|a\|\approx\|b\|\approx 1\)，结果近似等于点积）。但示例代码这样做更稳健——即使上游归一化被改动，相似度定义仍然正确。

#### 4.4.2 核心流程

```text
cosine_similarity(a, b):
  dot   = sum(a[i] * b[i])
  norm_a = sqrt(sum(a[i]^2))
  norm_b = sqrt(sum(b[i]^2))
  return dot / (norm_a * norm_b + 1e-10)   # +1e-10 防除零

print_similarity_matrix(embeds, labels):
  打印表头（每个 label 截断到 12 字符）
  for i, j: 打印 cosine_similarity(embeds[i], embeds[j])
```

#### 4.4.3 源码精读

`cosine_similarity` 自由函数：

[examples/embedding_main.cpp:7-15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L7-L15) —— 一次性累加点积和两个模长平方，最后 `dot / (sqrt(norm_a) * sqrt(norm_b) + 1e-10f)`。`+1e-10f` 与 `normalize` 里的保护同理，防零向量除零。

相似度矩阵打印：

[examples/embedding_main.cpp:17-35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L17-L35) —— 把每个标签截断到 12 字符宽，用 `%-15.4f` 打印每对相似度，形成对角线为 1（自身与自身）、其余在 \([-1,1]\) 的对称矩阵。

`main` 的使用方式：

[examples/embedding_main.cpp:37-71](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L37-L71) —— 默认模型路径 `assets/jina-embeddings-v5-text-nano`（[第 38 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L38)），可用 `argv[1]` 覆盖；以 `ncnn_embedding embed(model_path, false, 4)` 构造（CPU、4 线程，[第 44 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L44)）；用 `embed.ok()` 检查加载是否成功；然后编码 5 条句子（4 中 1 英）并打印矩阵。注意第 62 行对每条文本调 `embed.encode_text(text)`，逐条收集到 `text_embeds`。

#### 4.4.4 代码实践

**实践目标**：跑通 `embedding_main`，观察语义相近的句子相似度更高。

**操作步骤**：

1. 准备一个嵌入模型目录（默认 `assets/jina-embeddings-v5-text-nano`，权重需自行下载放到 `assets/` 下）。
2. 构建 target（构建方式见 u1-l2）：
   ```bash
   xmake build embedding_main
   ```
3. 运行（在项目根目录，因为 `set_rundir` 设为 `$(projectdir)/`）：
   ```bash
   xmake run embedding_main
   # 或指定其它模型目录：
   xmake run embedding_main assets/<你的嵌入模型目录>
   ```

**需要观察的现象**：
- 「今天天气很好」与「今天天气不错」的相似度应明显高于它与「我喜欢吃苹果」的相似度。
- 中英文对照（「今天天气很好」与「The weather is nice today」）的相似度取决于模型是否跨语言训练——跨语言模型应给出较高值。
- 矩阵对角线恒为 1.0（自身与自身）。

**预期结果**：得到一个 5×5 的对称相似度矩阵，语义相近的句子对数值更大。若手头没有模型权重，本步骤为「待本地验证」，可改为源码阅读：跟踪 [main 第 60-65 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L60-L65) 的编码循环，说明每条文本如何变成一行向量。

#### 4.4.5 小练习与答案

**练习 1**：既然 `encode_text` 已经归一化了向量，`cosine_similarity` 里再算 `norm_a`、`norm_b` 是否多余？为什么仍要保留？

**答案**：在当前实现下确实近似多余（结果≈点积）。但保留它让 `cosine_similarity` **不依赖上游是否归一化**——即使将来 `encode_text` 改成不归一化、或传入未归一化的向量，相似度定义仍然正确。这是一种解耦的稳健写法。

**练习 2**：相似度矩阵为什么一定是对称的、且对角线为 1？

**答案**：余弦相似度满足 \(\text{sim}(a,b)=\text{sim}(b,a)\)（点积和模长都可交换），所以矩阵对称；\(\text{sim}(a,a)=\frac{a\cdot a}{\|a\|^2}=1\)（归一化后更是显然），所以对角线为 1（数值上因 `+1e-10` 可能极轻微偏离 1）。

---

## 5. 综合实践

把本讲四个模块串起来，扩展 `embedding_main` 做一个**最小语义检索 demo**（在 `examples/` 下新建一份临时文件练习，**不要改原示例源码**）：

1. 定义一个「查询 + 若干候选文档」的小语料，例如：
   - query: 「怎么给手机充电」
   - docs: 「手机电池保养方法」「今日股市收盘上涨」「充电器使用注意事项」「明天下雨吗」
2. 用 `embed.encode_text(query)` 和 `embed.encode_text_batch(docs)` 得到向量。
3. 用 `cosine_similarity`（参考 [embedding_main.cpp:7-15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/embedding_main.cpp#L7-L15)）计算 query 与每个 doc 的相似度，按降序排序，打印 top-k。
4. 在报告里回答：
   - 排名第一的文档是否就是语义上最相关的？
   - 如果把 `encode_text` 内部的 `mean_pool` 或 `normalize` 去掉（在临时副本里改），排序结果会怎样变化？这印证了 4.3 里哪个结论？

> 这个任务综合了「构造与配置（4.1）」「encode_text（4.2）」「mean_pool/normalize（4.3）」「cosine_similarity（4.4）」四个模块。运行验证需嵌入模型权重，标注「待本地验证」。

## 6. 本讲小结

- `ncnn_embedding` 是与 `ncnn_llm_gpt` **平行的独立运行时**，不继承 `ncnn_llm_base`，不做 KV cache、不自回归、不采样——只跑一次编码器前向。
- 构造函数把 `model.json` 翻译成成员变量，按 `model_type`（`embedding`/`clip`）分流加载网络，按 `tokenizer.type`（`unigram`/`bpe`/`bbpe`）选择分词器，全部异常被 catch 成 `ok_ = false`。
- `encode_text` 主链路是：分词 → 截断到 `max_seq_len_` → 生成 RoPE（`RoPE_full` 或普通版）→ `in0/in1/in2 → out0` → 后处理；`encode_text_batch` 是串行循环、未做真 batch 前向。
- 后处理是相似度比较的关键：非 CLIP 模型走 `normalize(mean_pool(...))`——mean_pool 把序列压成单向量，normalize 拉到单位球面使点积等于余弦相似度；CLIP 模型跳过 mean_pool。
- `embedding_main` 用 `cosine_similarity` 把多条文本两两比较并打印相似度矩阵，是嵌入模型最经典的使用范式。

## 7. 下一步学习建议

- **u6-l2 CLIP 多模态嵌入**：本讲刻意跳过了 `encode_image` / `encode_image_file` / `preprocess_image`。下一讲会讲同一套 `ncnn_embedding` 如何把图像也编码到与文本相同的向量空间，实现图文检索。
- **回顾 u3-l1 / u3-l3**：本讲的 unigram 分词器分支（`LoadFromFile` + `SpecialTokensConfig`）正是 u3 系列讲过的内容，可对照加深理解。
- **回顾 u2-l2**：嵌入运行时同样遵循 ncnn 的 `create_extractor → input → extract` 三步调用模式，可与 LLM 运行时的调用方式对比阅读。
- 若你想深入相似度检索的工程化，可以思考如何为大量文档向量建立索引（如近似最近邻 ANN），本仓库未提供这部分，属于二次开发方向。
