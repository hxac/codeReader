# NLLB 机器翻译

## 1. 本讲目标

本讲是「其他模态」单元的最后一讲，主角是机器翻译运行时 `nllb_600m`。它和前面讲的 LLM（u2）、VLM（u5）都属于「带自回归生成」的模型，但架构完全不同——它使用的是经典的 **encoder-decoder（编码器-解码器）** 架构，而不是 LLM 那种「单一 decoder + KV cache 续写」的结构。

学完本讲，你应当能够：

1. 说清 `nllb_600m` 的构造过程：它加载了哪几个 ncnn 子网、用什么分词器、为什么用 Pimpl（指针实现）惯用法。
2. 理解 **正弦位置编码（sinusoidal positional embedding）** 的数学公式与代码实现，并能解释它为什么在 C++ 端而不是 ncnn 图里完成。
3. 跟踪 `translate` 的完整 encoder→cross-attention decoder 自回归流程，说清 **源语言 token** 和 **目标语言 token** 各自被放在哪里、如何引导翻译方向。
4. 区分 `translate` 的四个重载，掌握「同步返回整句」与「流式 callback 增量返回」两种用法。

本讲承接 u2-l1（基类 `ncnn_llm_base` 的公共能力，包括 `KVCache`、`create_option`、`load_net`、`sample_logits`）与 u3-l2（BPE 分词器）。NLLB 是项目中少数「自跑解码循环、不调用共享文本运行时四函数」的运行时，理解它能帮你对照出共享运行时（u2-l2）解决的是什么问题。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么要 encoder-decoder？

之前讲的 LLM（如 Qwen）是 **decoder-only** 架构：只有一个带因果掩码的 Transformer 堆栈，靠「续写下一个 token」工作。它做翻译时，是把「原文 + 指令」全部拼成输入序列，再自回归地吐出译文。

而 NLLB（No Language Left Behind，Meta 的多语种翻译模型）走的是 **seq2seq（序列到序列）** 的老派但有效的路线：

- **编码器（encoder）**：读入源语言整句，因为是双向自注意力（没有因果掩码），它可以同时「看到」全句，输出一组「理解了原意的记忆向量」（memory）。
- **解码器（decoder）**：自回归地生成译文。每生成一个词，它既看自己已经生成的译文（带因果掩码的自注意力），又通过 **交叉注意力（cross-attention）** 去读 encoder 输出的 memory。

打个比方：encoder 是「读完原文做笔记的人」，decoder 是「看着笔记逐句口译的人」。这种分工让 decoder 不必把整段原文塞进自己的上下文，特别适合「输入和输出是两种不同语言」的翻译任务。

### 2.2 位置编码是什么、为什么用「正弦」？

Transformer 的注意力机制本身没有「顺序」概念——把句子打乱词序，自注意力的计算方式不变。所以要额外给每个位置注入一个**位置编码（positional embedding）**，加到词向量上，告诉模型「这是第几个词」。

位置编码有两大家族：

- **可学习位置编码（learned）**：一张可训练的查找表，第 i 行就是第 i 个位置的向量。NLLB 原始模型用的其实是这种。
- **正弦位置编码（sinusoidal）**：用固定的三角函数公式生成，不需要训练参数。这是 2017 年《Attention is All You Need》论文里的经典方案。

NLLB-600 在本项目里被简化为用**正弦位置编码**（详见 4.2 与导出脚本注释）。它的妙处在于：同一套公式既能算位置 1、2、3，也能算位置 1000，理论上对任意长度都能外推。

### 2.3 语言 token：用一个词控制翻译方向

NLLB 词表里有专门的「语言标记 token」，命名遵循 `语言_文字` 格式，例如：

- `eng_Latn`：英语（拉丁字母）
- `zho_Hans`：简体中文（汉字）
- `zho_Hant`：繁体中文

神奇之处在于：**同一个 encoder 记忆，只要 decoder 起步时喂不同的语言 token，就会翻译成不同语言**。这就是 NLLB 用一个模型支持 200 种语言互译的关键——语言 token 充当了「翻译方向开关」。

---

## 3. 本讲源码地图

本讲涉及四个文件：

| 文件 | 作用 |
|------|------|
| `src/nllb_600m.h` | 对外头文件：声明 `nllb_600m` 类、`NllbConfig` 配置结构、四个 `translate` 重载。用 Pimpl 惯法隐藏实现细节。 |
| `src/nllb_600m.cpp` | 全部实现：内部 `Impl` 类（继承 `ncnn_llm_base`）、子网加载、encoder/decoder 前向、自回归解码循环。本讲的绝对主角。 |
| `examples/nllb_main.cpp` | 命令行示例：解析参数、拼出模型文件路径、构造 `nllb_600m`、演示同步与流式两种翻译调用。 |
| `src/ncnn_llm_base.h` | 基类：提供 `sinusoidal_positional_embedding`（全序列版与单位置版）、`mat_from_int_vector`、`add_mats_inplace`、`sample_logits`、`KVCache` 等 NLLB 依赖的公共能力。 |

模型权重相关：运行 NLLB 需要在 `assets/nllb_600m/` 下准备 `embed.ncnn.param/.bin`、`encoder_noembed.ncnn.param/.bin`、`decoder_noembed.ncnn.param/.bin`、`vocab.txt`、`merges.txt`（见 [examples/nllb_main.cpp:73-80](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L73-L80)）。权重需自行下载/导出，仓库不随附（与 u1-l2/u1-l3 一致）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 `nllb_600m` 构造：Pimpl 惯法与子网布局
- 4.2 正弦位置编码 `sinusoidal_positional_embedding`
- 4.3 `translate` 主流程：源/目标语言 token 与 encoder→cross-attention decoder 自回归生成
- 4.4 `translate` 重载与流式 callback

### 4.1 `nllb_600m` 构造：Pimpl 惯法与子网布局

#### 4.1.1 概念说明

NLLB 的对外类 `nllb_600m` 是一个非常「薄」的壳：它只持有一个 `std::unique_ptr<Impl>` 指针，所有真实逻辑都在内部类 `Impl` 里。这种手法叫 **Pimpl（Pointer to Implementation，指向实现的指针）惯法**。

为什么这么做？因为 `Impl` 继承自 `ncnn_llm_base`，而后者包含了 ncnn 的 `ncnn::Net`、`ncnn::Mat` 等重型头文件。把这些藏到 `.cpp` 里，可以让 `nllb_600m.h` 保持轻量，外部使用者只需 `#include "nllb_600m.h"` 而不必拉入 ncnn 的全部头文件，降低编译耦合与编译时间。

`NllbConfig` 是翻译时的采样/停止配置，字段与 LLM 的 `GenerateConfig` 概念对应但更精简：

```cpp
struct NllbConfig {
    float temperature = 1.0f;
    int top_k = 0;
    float top_p = 1.0f;
    bool do_sample = false;   // 默认贪心解码
    int max_steps = 512;      // 最多生成 512 步
};
```
见 [src/nllb_600m.h:7-13](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L7-L13)。注意 NLLB **没有 repetition_penalty 字段**——这正是它与共享文本运行时（u2-l4）的一个区别：NLLB 走的是基类自带的 `sample_logits`，而非带重复惩罚的 `llm_select_next_token`。

#### 4.1.2 核心流程

构造时，外壳把八个路径参数（三组子网的 param/bin + 分词器的 vocab/merges）原样转发给 `Impl`：

```
nllb_600m(...) 构造
   └─> std::make_unique<Impl>(...)   // 转发 8 个路径 + use_vulkan
          └─> ncnn_llm_base(use_vulkan, 4)   // 基类：4 线程
          └─> BpeTokenizer::LoadFromFiles(...)   // 加载词表+merges，注册特殊令牌
          └─> create_option() 赋给三个 Net
          └─> load_net(embed_net_/encoder_net_/decoder_net_)   // 任一失败置 ok_=false
          └─> 从词表查 </s> 的 id，存入 bos_eos_id_
```

注意两个构造重载的区别：一个不传 `use_vulkan`（默认 `false`），一个显式传入（见 [src/nllb_600m.h:17-34](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L17-L34)）。`nllb_main.cpp` 用的是后者，由命令行 `--vulkan` 决定。

#### 4.1.3 源码精读

`Impl` 的构造函数（关键部分）：

```cpp
class nllb_600m::Impl : public ncnn_llm_base {
public:
    Impl(...)
        : ncnn_llm_base(use_vulkan, 4)            // 继承基类，4 线程
        , ...
        , bpe_(BpeTokenizer::LoadFromFiles(
              vocab_file_, merges_file_,
              SpecialTokensConfig{
                  .bos_token = "</s>",            // NLLB 的 bos 和 eos 都是 </s>
                  .eos_token = "</s>",
                  .unk_token = "<unk>",
                  .mask_token = "<mask>",
              }))
    {
        ncnn::Option opt = create_option();
        embed_net_.opt = opt;
        encoder_net_.opt = opt;
        decoder_net_.opt = opt;

        if (!load_net(embed_net_, embed_param_, embed_bin_)) { ... }
        if (!load_net(encoder_net_, encoder_param_, encoder_bin_)) { ... }
        if (!load_net(decoder_net_, decoder_param_, decoder_bin_)) { ... }

        if (ok_) {
            const auto& t2i = bpe_.token_to_id();
            auto it = t2i.find("</s>");
            ... bos_eos_id_ = it->second;     // NLLB 中 </s> 的 id 通常为 2
        }
    }
```
见 [src/nllb_600m.cpp:24-82](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L24-L82)。

几个要点：

- **三个 `ncnn::Net` 成员**：`embed_net_`（token 查表）、`encoder_net_`（双向编码器）、`decoder_net_`（解码器 + lm_head 投影）。注意 encoder 和 decoder 的文件名都带 `_noembed` 后缀——意思是「不含 embedding 查表」，因为查表被单独拆成了 `embed` 子网。这呼应了导出脚本的设计：正弦位置编码不进 ncnn 图、embedding 单独导出。
- **`load_net` 失败即 `ok_=false`**：基类的健康检查机制（u2-l1 讲过），任一子网加载失败就标记不可用，后续 `translate` 会因 `if (!ok_) return false;` 直接短路。
- **`bos_eos_id_` 的解析**：NLLB 用同一个 `</s>` token 同时充当序列开始（bos）和结束（eos），默认值 `2`（见 [src/nllb_600m.cpp:273](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L273)），但构造时会用词表里的真实 id 覆盖。词表里找不到 `</s>` 会被视为致命错误（`ok_=false`）。

#### 4.1.4 代码实践

**实践目标**：理解 Pimpl 惯法与三子网布局，动手追踪构造链。

**操作步骤**：

1. 打开 [src/nllb_600m.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h)，确认 `nllb_600m` 类只有一个成员 `std::unique_ptr<Impl> impl_;`（[第 59-60 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L59-L60)），且 `class Impl;` 只是前向声明（[第 59 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L59)）——头文件里完全看不到 ncnn 的痕迹。
2. 打开 [src/nllb_600m.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L24-L82)，找到 `Impl` 的成员变量区（[第 262-273 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L262-L273)），数一数：3 个 `ncnn::Net`、1 个 `BpeTokenizer`、6 个路径 string、1 个 `bos_eos_id_`。
3. 思考题（先自己想再看答案）：为什么 `embed` 要从 encoder/decoder 里拆出来单独成一个子网？

**需要观察的现象**：头文件极简，实现全在 cpp；三个子网各对应一对 `.param/.bin` 文件。

**预期结果**：能在源码里指出「Pimpl 把 ncnn 隔离在 cpp 内」「三个子网分别负责查表、编码、解码」这两点。

#### 4.1.5 小练习与答案

**练习 1**：`NllbConfig` 里为什么没有 `repetition_penalty`？

**参考答案**：因为 NLLB 走的是基类 `ncnn_llm_base::sample_logits`（[src/ncnn_llm_base.h:147-169](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L147-L169)），而该函数本身就只支持 temperature/top_k/top_p，不支持重复惩罚。重复惩罚是共享文本运行时 `llm_select_next_token`（u3-l4）才有的能力。NLLB 是项目中「自跑解码循环、不复用共享运行时」的代表。

**练习 2**：构造函数里 `ncnn_llm_base(use_vulkan, 4)` 的第二个参数 `4` 是什么？

**参考答案**：是 `num_threads`（线程数），即基类成员 `num_threads_`（[src/ncnn_llm_base.h:109](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L109)），会被 `create_option()` 写进 `ncnn::Option::num_threads`（[src/ncnn_llm_base.h:130-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L130-L136)）。NLLB 硬编码为 4 线程。

---

### 4.2 正弦位置编码 `sinusoidal_positional_embedding`

#### 4.2.1 概念说明

正弦位置编码用一个不依赖训练参数的固定公式，为序列中每个位置生成一个 d_model 维向量。它的核心思想是：用不同频率的正弦/余弦波来「编码」位置，低维用高频（变化快）、高维用低频（变化慢），让模型能从不同尺度感知相对位置。

本项目里，正弦位置编码在 **C++ 端** 计算，加到 embedding 上，而不是写进 ncnn 计算图。导出脚本 [export/nllb_export.py](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py) 的注释明确写了「Sinusoidal positional embedding is NOT exported, handled in Python」（在这里改成 C++ 处理）。这样做的好处是位置编码可由宿主代码灵活控制（例如区分 encoder 全序列编码与 decoder 单步编码），不绑死在导出的图里。

#### 4.2.2 核心流程

基类提供了**两个**函数，分别服务于两种调用场景：

1. **全序列版** `sinusoidal_positional_embedding(seq_len, d_model)`：一次性生成 `(d_model, seq_len)` 的整张表，每个位置 `i` 对应位置号 `pos = i + 1`（注意是**从 1 起算**）。用于 encoder 输入（整句）和 decoder 的 prefill。
2. **单位置版** `sinusoidal_positional_embedding_for_pos(position, d_model)`：只为某一个具体 `position` 生成 `d_model` 维的单行向量。用于 decoder 自回归解码的每一步（每步只处理一个新 token，不需要整张表）。

数学公式（全序列版，位置 `pos = i+1`，`half_dim = d_model/2`）：

\[ \text{inv\_freq}_j = \exp\!\left(j \cdot \left(-\frac{\ln 10000}{\text{half\_dim}}\right)\right) = 10000^{-j/\text{half\_dim}}, \quad j = 0,1,\dots,\text{half\_dim}-1 \]

\[ \text{PE}(\text{pos},\, j) = \sin(\text{pos}\cdot \text{inv\_freq}_j), \qquad \text{PE}(\text{pos},\, j+\text{half\_dim}) = \cos(\text{pos}\cdot \text{inv\_freq}_j) \]

注意本项目的**布局是「前半 sin、后半 cos」拼接**（`[sin..., cos...]`），不是原论文那种 `[sin, cos, sin, cos]` 交错排布。这与导出脚本里 `torch.cat([torch.sin(x), torch.cos(x)], dim=-1)` 完全一致。

#### 4.2.3 源码精读

全序列版（[src/ncnn_llm_base.h:49-72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L49-L72)）：

```cpp
inline ncnn::Mat sinusoidal_positional_embedding(int seq_len, int d_model) {
    int half_dim = d_model / 2;
    ncnn::Mat emb(d_model, seq_len);          // w=d_model, h=seq_len
    emb.fill(0.0f);

    std::vector<float> inv_freq(half_dim);
    double log_10000 = std::log(10000.0);
    double denom_base = static_cast<double>(std::max(1, half_dim));
    for (int i = 0; i < half_dim; ++i) {
        inv_freq[i] = static_cast<float>(std::exp(
            static_cast<double>(i) * -(log_10000 / denom_base)));   // 10000^(-i/half_dim)
    }

    for (int i = 0; i < seq_len; ++i) {
        float pos = static_cast<float>(i + 1);  // 关键：位置从 1 起算
        float* row_ptr = emb.row(i);
        for (int j = 0; j < half_dim; ++j) {
            float angle = pos * inv_freq[j];
            row_ptr[j] = std::sin(angle);                 // 前半段：sin
            row_ptr[j + half_dim] = std::cos(angle);      // 后半段：cos
        }
    }
    return emb;
}
```

单位置版（[src/ncnn_llm_base.h:74-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L74-L97)）公式相同，但只算一行、且直接接收 `position`（不再 `+1`）：

```cpp
inline ncnn::Mat sinusoidal_positional_embedding_for_pos(int position, int d_model) {
    ...
    for (int j = 0; j < half_dim; ++j) {
        float angle = static_cast<float>(position) * inv_freq[j];   // 直接用 position，不 +1
        emb_ptr[j] = std::sin(angle);
        emb_ptr[j + half_dim] = std::cos(angle);
    }
    ...
}
```

两个函数被 `nllb_600m.cpp` 的 `embedding_forward` 调用（[src/nllb_600m.cpp:163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183)）：`pos == -1` 时走全序列版（用于 encoder 与 decoder prefill），否则走单位置版（用于 decoder 每步）。位置编码与 token embedding 相加后才送入网络：

```cpp
ncnn::Mat embedding_forward(const std::vector<int>& input_ids, int pos) {
    ncnn::Extractor ex = embed_net_.create_extractor();
    ex.input("in0", mat_from_int_vector(input_ids));   // token id -> 查表
    ncnn::Mat out0;
    ex.extract("out0", out0);                          // out0.h=seq_len, out0.w=d_model

    ncnn::Mat pos_emb = (pos == -1)
        ? sinusoidal_positional_embedding(out0.h, out0.w)      // 全序列
        : sinusoidal_positional_embedding_for_pos(pos, out0.w);// 单位置
    ncnn::Mat result = out0.clone();
    add_mats_inplace(result, pos_emb);                 // 逐元素相加：token_embed + PE
    return result;
}
```

注意 `add_mats_inplace` 会校验形状一致才相加（[src/ncnn_llm_base.h:22-34](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L22-L34)），形状不匹配时静默跳过——这是个潜在陷阱：若维度算错，位置编码会「没加上」而不报错。

#### 4.2.4 代码实践

**实践目标**：手算并验证正弦位置编码，理解「位置从 1 起算」与「前 sin 后 cos」布局。

**操作步骤**：

1. 假设 `d_model = 4`（故 `half_dim = 2`），手算 `position = 1` 时的 PE 向量：
   - `inv_freq[0] = 10000^(0/2) = 1`，`inv_freq[1] = 10000^(-1/2) = 0.01`
   - 前半（sin）：`[sin(1·1), sin(1·0.01)] = [sin(1), sin(0.01)] ≈ [0.8415, 0.0100]`
   - 后半（cos）：`[cos(1), cos(0.01)] ≈ [0.5403, 0.9999]`
   - 拼接：`[0.8415, 0.0100, 0.5403, 0.9999]`
2. 对照 `sinusoidal_positional_embedding_for_pos(1, 4)` 的代码，确认你的手算与代码逻辑一致。
3. 阅读导出脚本 [export/nllb_export.py:72-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L72-L94) 的 `SinusoidalPositionalEmbeddingTS`，确认 C++ 与 Python 用的是同一套公式（`pos = arange(1, seq_len+1)`，同样从 1 起算；同样 `cat([sin, cos])`）。

**需要观察的现象**：位置 1 与位置 2 的 PE 向量在前几个高频维差别明显、在后几个低频维几乎相同——这正是「不同频率编码不同尺度」的直观体现。

**预期结果**：能解释为什么 `pos = i + 1`（而非 `i`），以及为什么 C++ 与导出脚本必须用完全一致的位置起算点与拼接顺序。

**待本地验证**：若要打印真实数值，可写一个小程序调用 `sinusoidal_positional_embedding_for_pos` 并输出，需先 `#include "ncnn_llm_base.h"`。

#### 4.2.5 小练习与答案

**练习 1**：全序列版用 `pos = i + 1`，单位置版为什么不也 `+1`？

**参考答案**：因为单位置版接收的 `position` 参数已经是「真实位置号」（decoder 解码循环里传入 `pos=2,3,4,...`），调用方已经算好了，函数内不必再偏移。全序列版则是根据数组下标 `i` 生成位置，需要 `+1` 把 0 基下标映射成「从 1 起算」的位置号。两者最终都遵循「位置从 1 开始」的同一约定。

**练习 2**：为什么本项目选择在 C++ 端加位置编码，而不是放进 ncnn 图？

**参考答案**：因为 decoder 的自回归解码需要「每步只为单个新位置算一次 PE」，而 encoder 是「一次算整句」。把它们都塞进固定的 ncnn 图会很不灵活。在 C++ 端用 `embedding_forward` 的 `pos` 参数分流（`-1` 走全序列、否则走单位置），可以复用同一个 `embed_net_` 查表子网、只换位置编码策略，更简洁。

---

### 4.3 `translate` 主流程：源/目标语言 token 与自回归生成

#### 4.3.1 概念说明

这是本讲的核心模块。`translate` 把一段源语言文本翻译成目标语言文本，全程由 `Impl::translate_stream` 驱动（[src/nllb_600m.cpp:94-160](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L94-L160)）。

关键设计有两个：

1. **源语言 token 进 encoder**：把 `source_lang` 对应的 id **插到输入 token 序列最前面**，告诉 encoder「这句是什么语言」。
2. **目标语言 token 进 decoder**：decoder 不以普通词起步，而是先用 `</s>` 做 prefill、再以 `target_lang` 作为第一个解码输入。模型在「看到目标语言标记」后，自然地朝该语言生成。

这两步共同实现「一个模型、双向互译」——只要你交换 `source_lang` 和 `target_lang`，翻译方向就反过来。

#### 4.3.2 核心流程

完整的 encoder-decoder 翻译管线如下：

```
translate_stream(text, src_lang, tgt_lang, config, callback)
│
├─ 1. 解析语言 token id：src_lang_id, tgt_lang_id（词表查不到则返回 false）
│
├─ 2. 编码源句（encoder 侧）
│     bpe_.encode(text) → input_ids
│     input_ids 头部插入 src_lang_id          # 源语言 token 引导 encoder
│     embed + 正弦PE → encoder_forward → encoder_output(memory)
│
├─ 3. decoder 预填充（用 </s> 起步）
│     bos = {bos_eos_id_}                      # </s>
│     embed(</s>) + 正弦PE(位置1) → decoder_prefill(记忆=encoder_output)
│     → 得到初始 KV cache（此时不取 logits）
│
├─ 4. 自回归解码循环（for pos = 2 .. max_steps）
│     last_index 初始 = tgt_lang_id            # 目标语言 token 作为首个解码输入
│     每步：
│       embed(last_index) + 单位置PE(pos) → decoder_decode(记忆=encoder_output, 旧KV)
│       → logits + 新 KV cache
│       next = sample_logits(logits, sample_cfg)
│       output.push_back(next)
│       if next == </s> : break                # 遇到 eos 停止
│       delta 解码 → callback 增量返回
│
└─ 返回 true
```

三个要点：

- **encoder 是双向的**：`encoder_forward` 不传任何 mask（[src/nllb_600m.cpp:185-191](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L185-L191)），整句 token 互相全可见。这与 LLM/VLM 的因果 decoder 形成对比。
- **decoder 有两段注意力**：自注意力（带因果 mask，看已生成的译文）+ 交叉注意力（看 encoder 的 memory）。memory 在每一步都被重新喂入（`ex.input("in1", encoder_out)`）。
- **目标语言 token 不进输出文本**：`output` 数组只收集真正的译文 token，`tgt_lang_id` 仅作为首步输入消费掉、不 push 进 `output`，所以 `decode` 出来的文本不含语言标记。

#### 4.3.3 源码精读

**第一步：解析语言 token + 准备 encoder 输入**（[src/nllb_600m.cpp:101-118](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L101-L118)）：

```cpp
const auto& t2i = bpe_.token_to_id();
auto it_src = t2i.find(source_lang);
auto it_tgt = t2i.find(target_lang);
if (it_src == t2i.end() || it_tgt == t2i.end()) {
    std::cerr << "Unknown language tokens: ...";
    return false;                          // 语言标记不存在 → 直接失败
}
int src_lang_id = it_src->second;
int tgt_lang_id = it_tgt->second;

std::vector<int> input_ids = bpe_.encode(input_text, false, true);  // add_bos=false, add_eos=true
input_ids.insert(input_ids.begin(), src_lang_id);  // 源语言 token 插到最前

ncnn::Mat embed_input = embedding_forward(input_ids, -1);     // 查表 + 全序列正弦PE
ncnn::Mat encoder_output = encoder_forward(embed_input);      // 双向编码 → memory
```

注意 `bpe_.encode(input_text, false, true)`：第二个参数 `add_bos=false`、第三个 `add_eos=true`（见 [bpe_tokenizer.h:23-26](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h#L23-L26)），即源句末尾自动加 `</s>`。最终 encoder 输入形如 `[src_lang_id, t1, t2, ..., tn, </s>]`。

**第二步：decoder prefill**（[src/nllb_600m.cpp:120-122](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L120-L122) + [193-223](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L193-L223)）：

```cpp
std::vector<int> bos = {bos_eos_id_};                       // </s>
ncnn::Mat bos_embed = embedding_forward(bos, -1);           // 位置1的正弦PE
KVCache kv_cache = decoder_prefill(bos_embed, encoder_output);
```

`decoder_prefill` 把 `</s>` 作为 decoder 的第一个输入（位置 1），同时喂入 encoder memory（`in1`）和因果 mask（`in2`），但**只提取 KV cache、不提取 `out0` logits**：

```cpp
KVCache decoder_prefill(const ncnn::Mat& hidden, const ncnn::Mat& encoder_out) {
    KVCache kv_cache;
    kv_cache.reserve(kNumDecoderLayers);          // kNumDecoderLayers = 24
    ncnn::Extractor ex = decoder_net_.create_extractor();
    ex.input("in0", hidden);
    ex.input("in1", encoder_out);                 // 交叉注意力的 memory

    // 因果自注意力 mask：上三角置 -inf
    const int seq_len = hidden.h;
    ncnn::Mat attention_mask(seq_len, seq_len);
    attention_mask.fill(0.0f);
    for (int i = 0; i < seq_len; ++i)
        for (int j = i + 1; j < seq_len; ++j)
            attention_mask.row(i)[j] = -std::numeric_limits<float>::infinity();
    ex.input("in2", attention_mask);

    // 逐层提取输出 KV cache（out_cache_k0/k1/...）
    for (int i = 0; i < kNumDecoderLayers; ++i) {
        ... ex.extract("out_cache_k%d"/"out_cache_v%d", ...);
        kv_cache.emplace_back(std::move(k_cache), std::move(v_cache));
    }
    return kv_cache;
}
```

> 说明：本项目把 decoder 的 KV cache 槽位数固定为 `kNumDecoderLayers = 24`（[src/nllb_600m.cpp:20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L20)），按 `out_cache_k%d`/`cache_k%d` 命名逐层读写。这与导出脚本注入的 KV cache 槽位是对应的契约。

**第三步：自回归解码循环**（[src/nllb_600m.cpp:124-157](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L124-L157)）：

```cpp
int last_index = tgt_lang_id;                  // 目标语言 token 作为首个解码输入
std::vector<int> output;
...
for (int pos = 2; pos < config.max_steps; ++pos) {
    std::vector<int> step_ids = {last_index};
    ncnn::Mat step_embed = embedding_forward(step_ids, pos);   // 单位置正弦PE

    auto [logits, new_cache] = decoder_decode(step_embed, encoder_output, kv_cache);
    kv_cache = std::move(new_cache);           // KV cache 更新

    last_index = sample_logits(logits, sample_cfg);   // 基类采样（默认贪心 argmax）
    output.push_back(last_index);

    if (last_index == bos_eos_id_) break;      // 遇到 </s> 停止

    // 增量解码（见 4.4）
    ...
}
```

`decoder_decode`（[src/nllb_600m.cpp:225-260](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L225-L260)）每步做三件事：喂新 token 嵌入（`in0`）、喂 encoder memory（`in1`）、喂旧 KV cache（`cache_k%d`/`cache_v%d`），再取出新 KV cache（`out_cache_k%d`）和 logits（`out0`）。mask 退化为 `(1,1)` 的 0（单 token 自注意力无需屏蔽）。

把整个 token 位置序列列出来就清楚了：

| 步骤 | 输入 token | 位置(PE) | 由谁产生 | 是否进 output |
|------|-----------|----------|----------|--------------|
| decoder prefill | `</s>` | 1 | 调用方固定 | 否 |
| 解码第 1 步 (pos=2) | `tgt_lang_id` | 2 | 调用方固定 | 否（仅消费） |
| 解码第 2 步 (pos=3) | 译文词 w1 | 3 | 上一步 argmax | 是（w1） |
| 解码第 3 步 (pos=4) | 译文词 w2 | 4 | 上一步 argmax | 是（w2） |
| ... | ... | ... | ... | ... |
| 某步 | `</s>` | — | argmax | 触发 break |

也就是说：喂 `tgt_lang_id` 得到的 logits，argmax 出的是译文的**第一个真正的词** w1。目标语言 token 就这样「身先士卒」地引导了生成方向，自己却不出现在译文里。

#### 4.3.4 代码实践

**实践目标**：跟踪一次完整的 encoder→decoder 翻译，亲手对应「语言 token 引导方向」的代码位置。

**操作步骤**：

1. 打开 [src/nllb_600m.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L94-L160) 的 `translate_stream`，在源码旁标注：
   - 「源语言 token 进 encoder」→ [第 115 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L115) `input_ids.insert(input_ids.begin(), src_lang_id)`
   - 「目标语言 token 进 decoder」→ [第 124 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L124) `int last_index = tgt_lang_id`
2. 对照上面的「token 位置序列」表格，确认 `output` 数组里不含 `tgt_lang_id`，也不含 prefill 的 `</s>`。
3. 准备好 `assets/nllb_600m/` 模型目录后，构建并运行示例（默认英→中）：
   ```
   xmake build nllb_main
   xmake run nllb_main --text "ncnn is the best edge-side neural network inference framework"
   ```
4. **做一次中→英翻译**（交换源/目标语言）：
   ```
   xmake run nllb_main --src zho_Hans --tgt eng_Latn --text "今天天气很好"
   ```

**需要观察的现象**：同样的模型、同样的代码路径，仅因 `--src`/`--tgt` 不同，输出语言就相反。

**预期结果**：英→中输出中文；中→英输出英文。这证明翻译方向完全由语言 token 决定。

**待本地验证**：实际译文质量与 tokens 数取决于本地模型权重；若 `assets/nllb_600m/` 不存在，构造阶段 `load_net` 会失败、`translate` 直接返回空串/false。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `decoder_prefill` 用 `</s>` 而不是 `tgt_lang_id` 起步？

**参考答案**：这遵循 NLLB/M2M 的解码约定——decoder 序列以 `</s>`（bos）开头，紧接目标语言 token，再接译文。prefill 先把 `</s>` 在位置 1 灌进 KV cache（只建 cache、不取 logits），随后第一步解码把 `tgt_lang_id` 放在位置 2、产出第一个译文词的预测。这与导出脚本 `scripted_greedy_decode` 里 seed `[2, lang_id]`（`</s>=2` 在前、语言 id 在后）的位置安排一致（[export/nllb_export.py:315-316](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L315-L316)）。

**练习 2**：encoder 的自注意力为什么不需要因果 mask？

**参考答案**：因为 encoder 要「理解整句原意」，源句的每个词都应能互相看到（双向）。因果 mask 是 decoder-only 模型（如 LLM）为了防止「看到未来」才加的；翻译任务里源句是完整给定的，不存在「未来泄露」问题。

---

### 4.4 `translate` 重载与流式 callback

#### 4.4.1 概念说明

`nllb_600m` 对外暴露 **四个** `translate` 重载（[src/nllb_600m.h:38-56](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L38-L56)），按「是否传 `NllbConfig`」和「是否传 callback」两个维度组合：

| 重载 | NllbConfig | callback | 返回类型 | 语义 |
|------|-----------|----------|---------|------|
| 1 | 否（用默认） | 否 | `std::string` | 同步，返回整句译文 |
| 2 | 是 | 否 | `std::string` | 同步，自定义采样配置 |
| 3 | 否（用默认） | 是 | `bool` | 流式，增量回调 |
| 4 | 是 | 是 | `bool` | 流式，自定义配置 + 增量回调 |

两个「同步」重载内部都调用 `translate_sync`，它其实只是把一个「把 delta 追加到字符串」的 lambda 传给 `translate_stream`（[src/nllb_600m.cpp:84-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L84-L92)）。也就是说，**流式是底层实现，同步是在流式之上包了一层**。

#### 4.4.2 核心流程

流式增量返回的关键在于「delta 解码」：每生成一个新 token，不是单独 decode 这个 token（因为多字节 UTF-8 或 BPE 合并可能跨 token），而是**对整个 `output` 数组重新 decode，再取与上次结果的差值**。

```
每步生成 next token 后：
  current = bpe_.decode(output, skip_special=true)   # 重新解码整段
  if current.size() >= last_decoded.size():
      delta = current.substr(last_decoded.size())    # 取新增尾部
      callback(delta)                                # 增量返回
      last_decoded = current
  else:
      callback(current)                              # 异常回缩时整体返回
      last_decoded = current
```

这样即便译文是中文（一个字可能由多个 BPE 片段拼成），也能保证每次 callback 收到的是完整的、不会截断 UTF-8 的字符串片段。

#### 4.4.3 源码精读

四个重载的转发逻辑（[src/nllb_600m.cpp:315-345](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L315-L345)），以「默认配置 + 流式」为例：

```cpp
bool nllb_600m::translate(const std::string& input_text,
                          const std::string& source_lang,
                          const std::string& target_lang,
                          std::function<void(const std::string&)> callback) {
    if (!impl_ || !impl_->ok()) return false;
    return impl_->translate_stream(input_text, source_lang, target_lang,
                                   NllbConfig{}, std::move(callback));
}
```

同步版则把结果攒进字符串（[src/nllb_600m.cpp:84-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L84-L92)）：

```cpp
std::string translate_sync(...) {
    std::string out;
    translate_stream(..., [&](const std::string& delta) { out += delta; });
    return out;
}
```

delta 解码的完整片段（[src/nllb_600m.cpp:144-156](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L144-L156)）：

```cpp
if (last_index == bos_eos_id_) {
    break;                            // eos 先判断，不进 callback
}

std::string current = bpe_.decode(output, true);   // skip_special_tokens=true
if (current.size() >= last_decoded.size()) {
    std::string delta = current.substr(last_decoded.size());
    if (!delta.empty() && callback) callback(delta);
    last_decoded.swap(current);
} else {
    if (callback) callback(current);
    last_decoded = std::move(current);
}
```

`nllb_main.cpp` 同时演示了同步与流式两种用法（[examples/nllb_main.cpp:109-121](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L109-L121)）：

```cpp
// 1) 同步翻译
std::string out = translator.translate(args.text, args.source_lang, args.target_lang);
std::cout << "[Sync] Output: " << out << "\n\n";

// 2) 流式翻译：每收到一段 delta 就立即打印
std::cout << "[Stream] Output: ";
bool ok = translator.translate(args.text, args.source_lang, args.target_lang,
    [](const std::string& chunk) {
        std::cout << chunk << std::flush;     // 边生成边输出
    });
std::cout << "\n[Stream] Status: " << (ok ? "success" : "failed") << "\n";
```

#### 4.4.4 代码实践

**实践目标**：体验同步与流式两种 API，理解 delta 解码。

**操作步骤**：

1. 阅读 [examples/nllb_main.cpp:109-121](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L109-L121)，对比两种调用形式：同步版用返回值、流式版用 lambda。
2. （源码阅读型实践）在 [src/nllb_600m.cpp:148-156](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L148-L156) 处思考：为什么用「整段重解码再取差值」，而不是直接 `bpe_.decode({next_token})`？提示：考虑中文 BPE 分片与多字节 UTF-8。
3. 准备好模型后，运行 `nllb_main` 观察两种输出的差异：同步版一次性打印整句；流式版逐段「边译边吐」。
4. 进阶（可选）：仿照 `nllb_main`，写一个自己的小 main，用带 `NllbConfig` 的重载把 `do_sample=true, temperature=0.8` 打开，观察贪心与采样的译文差异（需自行编译新 target）。

**需要观察的现象**：流式输出时，译文是分若干次打印出来的，每次一小段；同步输出则等全部生成完才打印。

**预期结果**：两种方式的最终译文文本应当一致（在贪心解码下）。

**待本地验证**：流式分段粒度取决于 BPE 合并边界，需本地实跑观察。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接对单个新 token 调 `decode` 取增量，而要整段重解码？

**参考答案**：因为 BPE 解码不是「逐 token 独立」的。一个中文字符可能由多个 BPE 片段拼成，单个片段单独 decode 可能是无意义的半个字或空串。整段 `decode(output)` 后取 `substr(last_decoded.size())` 的差值，能保证每个增量都是完整的、合法的字符串片段，不会把多字节 UTF-8 字符从中间截断。

**练习 2**：四个重载里，哪个是「最底层」的实现？

**参考答案**：是 `Impl::translate_stream`（带 `NllbConfig` 与 callback）。所有四个对外重载最终都落到它：同步重载只是把「追加到字符串」的 lambda 当 callback 传入，无 `NllbConfig` 的重载只是传一个默认 `NllbConfig{}`。所以「流式 + 配置」是最通用形态，其余都是它的便利封装。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面的综合任务：

**任务：画出 NLLB 一次翻译的完整数据流图，并标注每个模块的代码位置。**

1. 选一句短英文，例如 `"Hello world"`，设定 `--src eng_Latn --tgt zho_Hans`。
2. 在一张纸上（或笔记软件里）画出从字符串到译文的数据流，至少包含以下节点，并在每个节点旁标出对应的源码行号：
   - `bpe_.encode` → token id 序列（[nllb_600m.cpp:114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L114)）
   - 头部插入 `src_lang_id`（[第 115 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L115)）
   - `embedding_forward`（查表 + 正弦 PE）（[第 117 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L117) / [163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183)）
   - `encoder_forward` → memory（[第 118 行](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L118) / [185-191](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L185-L191)）
   - decoder prefill（`</s>` → KV cache）（[120-122](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L120-L122) / [193-223](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L193-L223)）
   - 自回归循环：`tgt_lang_id` → decoder_decode → sample → ...（[134-157](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L134-L157) / [225-260](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L225-L260)）
   - delta 解码与 callback（[148-156](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L148-L156)）
3. 在图上用两种颜色分别标出 **源语言 token** 和 **目标语言 token** 的注入点，并各写一句话说明它们如何引导翻译方向。
4. 在图旁写一句：本讲的正弦位置编码为什么必须用「位置从 1 起算、前 sin 后 cos」这套与导出脚本一致的约定。

**验收标准**：图能自洽地解释「输入英文 → 输出中文」的全过程，且每个箭头都能在源码里指到具体行。

---

## 6. 本讲小结

- `nllb_600m` 用 **Pimpl 惯法** 把 ncnn 隔离在 cpp 内，对外只暴露薄壳；内部 `Impl` 继承 `ncnn_llm_base`，加载 `embed`/`encoder`/`decoder` 三个子网加一个 BPE 分词器。
- 它是经典的 **encoder-decoder（seq2seq）** 架构：encoder 双向编码源句成 memory，decoder 自回归生成译文，二者通过**交叉注意力**连接。
- **正弦位置编码**在 C++ 端计算（不进 ncnn 图），有全序列版与单位置版两个函数；位置从 1 起算、采用「前 sin 后 cos」拼接，必须与导出脚本保持一致。
- **源语言 token** 插到 encoder 输入最前、**目标语言 token** 作为 decoder 首个解码输入；交换二者即可反转翻译方向——这是 NLLB 一个模型支持 200 种语言互译的关键。
- `translate` 有四个重载，本质是「流式 + 配置」最底层、其余是便利封装；流式靠**整段重解码取差值**实现增量返回，避免截断多字节字符。
- NLLB 走的是基类 `sample_logits`（无重复惩罚），是项目中「自跑解码循环、不复用共享文本运行时」的代表，可与 u2 的 LLM 主链路对照阅读。

---

## 7. 下一步学习建议

- **回看 u2-l1/u2-l2 对照差异**：本讲的 `decoder_prefill`/`decoder_decode` 与共享文本运行时的 `llm_run_decoder_with_kv`（u2-l2）解决的是同一类问题（带 KV cache 的 decoder 前向），但 NLLB 自管 KV cache、且多了交叉注意力（`in1` 喂 memory）。对比二者能加深对「为什么项目要抽象出共享运行时」的理解。
- **看导出脚本 `export/nllb_export.py`**：理解 HF 的 NLLB 模型如何被拆成三个 TorchScript 模块、KV cache 槽位如何注入、正弦位置编码为何留在 Python/C++ 端。这是 u8-l4「模型导出流程」的具体案例。
- **延伸到 u7（对话模板与工具调用）**：本讲的语言 token 是「用特殊 token 引导生成」的一个朴素例子；u7 的 ChatML 模板与工具调用机制是同一思想的工程化升级。
- **尝试接入新语种**：在 `assets/nllb_600m/vocab.txt` 里找到目标语言 token（如 `jpn_Jpan` 日语、`kor_Hang` 韩语），用 `--src/--tgt` 跑一遍，验证「语言 token 即开关」的结论。
