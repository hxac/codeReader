# 二次开发：接入新模型家族

## 1. 本讲目标

本讲是整本学习手册的收官篇，也是「性能、测试、导出与二次开发」单元的最后一讲。它不再讲某个单一函数，而是把前面所有讲义的知识**串成一套可操作的工程方法**：当你拿到一个 ncnn_llm 还不支持的模型时，从零到能跑起来，到底要改哪些地方。

学完后你应该能够：

1. 用一张「决策树」判断一个新模型是**纯配置可接入**（只改 `model.json`、零代码），还是**需要代码分支**（要改构造函数 / prefill / generate，甚至新增 RoPE 变体或自定义算子）。
2. 为一个标准 decoder-only LLM 写出一份完整、可被构造函数正确解析的最小 `model.json`，并逐字段说清它被哪几行代码读取、是必填还是选填。
3. 根据 `setting.rope.type`、`tokenizer.type`、`sconv_cnt`/`gdr_cnt` 三组开关，判断新模型需要走哪条 RoPE 分支、哪种分词器、以及是否需要派生上下文 `qwen3_5_ctx`。
4. 说清「跨模型族共享 decoder + KV cache 运行时」这条设计主线在接入新模型时**在哪里成立、在哪里被打破**（混合架构是唯一例外）。

## 2. 前置知识

本讲是综合实战，强烈建议先读完以下四讲，本讲不再重复其细节，只承接结论：

- **u1-l5 模型目录与 model.json 配置体系**：`model.json` 分 `params` / `tokenizer` / `setting` 三块，构造函数把它们翻译成成员变量；判定必填与否的标准是「代码有没有用 `contains` 包一层」。
- **u2-l3 prefill 文本预填充流程**：纯文本 `prefill` 的「分词 → RoPE → embed → 因果 mask → 两段 decoder → proj_out → argmax」骨架。
- **u3-l1 分词器抽象与特殊令牌**：`SpecialTokensConfig` / `SpecialTokenIds`，以及 `tokenizer.type` 在 bpe / bbpe 间切换。
- **u4-l1 RoPE 基础与长上下文变体**：基础 / NTK / YaRN / LongRoPE 四种纯文本 RoPE 变体。

还需要补充两个贯穿性概念：

- **配置驱动（config-driven）**：ncnn_llm 的设计理想是「换一个模型只换一个 `model.json`，C++ 代码一行不改」。能做到这点的前提是新模型的算子组合、KV cache 命名约定、分词算法、位置编码方案**都已经被运行时覆盖**。本讲的第一任务就是判断这个前提成不成立。
- **共享运行时（shared runtime）**：指 `src/ncnn_text_runtime.cpp` 里那四个自由函数（`llm_run_text_embed` / `llm_run_decoder_with_kv` / `llm_run_lm_head` / `llm_select_next_token`），LLM / OCR / ASR 三种模态原样复用它们，是「跨模型族共享 decoder」的落地（详见 u2-l2）。接入新模型时，能走共享运行时就走，走不通才需要内联展开。

## 3. 本讲源码地图

本讲涉及的关键文件与职责如下：

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 主 LLM 运行时实现。构造函数（L18–L246）是本讲的「单一事实来源」：所有 `model.json` 字段都在这里被读取；prefill（L248–L841）与 generate（L843–L985）则是判断「需不需要分支」的现场。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 声明构造函数签名、`RoPE_Type`/`Vision_Type` 枚举、以及所有被 `model.json` 填充的成员变量，还有上下文类层次 `ncnn_llm_gpt_ctx` / `ncnn_llm_gpt_base_ctx` / `qwen3_5_ctx`。 |
| [src/utils/rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) | 纯文本四种 RoPE 变体的生成函数签名。判断「新模型的位置编码是否已被覆盖」就看这里有没有对应函数。 |
| [src/utils/tokenizer/tokenizer_types.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h) | `SpecialTokensConfig` / `SpecialTokenIds` 两个纯数据结构，是「人类可读配置」与「模型所需整数 id」之间的桥梁。 |
| [src/utils/gdr.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.h) | 自定义 ncnn 算子 `GatedDeltaRule` / `ShortConv` 与 `register_gdr_layers`，混合架构接入时绕不开。 |
| [readme.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md) | 支持模型表（L48–L60）与模型目录结构（L96–L103），用来对照「已接入家族」用了哪些配置。 |

> 一句话定位：本讲不教你写新算法，而是教你**读懂构造函数这台「配置解析器」，然后回答「新模型能不能塞进去」**。构造函数读什么、怎么读，就是 `model.json` 的契约。

## 4. 核心概念与源码讲解

### 4.1 接入新模型的总决策：配置驱动 vs 代码分支

#### 4.1.1 概念说明

拿到一个新模型（比如某个 HuggingFace 上的 LLM），第一步不是写代码，而是**对照构造函数给它做体检**，判定它落在下面四类里的哪一类。这决定了工作量是「写一份 JSON」还是「改十个源码点」。

#### 4.1.2 核心流程：四类接入路径

```
                       新模型架构特征
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
  标准 decoder-only       长上下文变体          非 Transformer / 混合架构
  + 基础 RoPE            (NTK/YaRN/LongRoPE)   (线性注意力 / short conv)
        │                    │                    │
        ▼                    ▼                    ▼
   【A 类·零代码】      【B 类·零代码】        【D 类·需自定义算子】
   只写 model.json      写 model.json          + gdr.cpp 新算子
   setting.rope.type    + rope_scaling 字段    + qwen3_5_ctx + 内联 decoder
   = "RoPE"             setting.rope.type
                        = "NTKRoPE" 等
                             │
                             ▼
              【C 类·需新代码】若位置编码方案不在四种之内
              → 新增 rope_embed.cpp 生成函数 + 新 RoPE_Type 枚举值
              + 构造函数/prefill/generate 三处分支
```

把决策翻译成判断清单：

| 判断项 | 看哪里 | 落在 A/B（零代码） | 落在 C/D（需改码） |
| --- | --- | --- | --- |
| 位置编码 | [rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) 四种函数 | 基础/NTK/YaRN/LongRoPE 之一 | 新方案 → C 类 |
| 分词算法 | 构造函数 L83–L92 的 `type` 分支 | bpe / bbpe | unigram 在 gpt 不支持；其它新算法 → 类似 C 类 |
| 注意力状态 | `sconv_cnt`/`gdr_cnt` 是否为 0 | 0（纯 KV cache） | >0 → D 类混合架构 |
| 架构形态 | 是否 decoder-only | 是 | encoder-decoder → 走 `nllb_600m` 运行时，不是 gpt |

> 重要：ncnn_llm 已支持的全部家族都落在 A/B/D 三类（见 README 表 L48–L60）。**唯一需要代码分支的是 D 类混合架构（Qwen3.5）**，这是本讲 4.5 的重点。

#### 4.1.3 源码精读

四类划分的依据全部写在构造函数开头——它读什么、不读什么，就是「运行时覆盖了什么」。先看构造函数的骨架：

[ncnn_llm_gpt.cpp:18-25](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L18-L25) —— 打开 `model.json` 并解析为 `json config`，整个构造函数的「体检」从这里开始。

[ncnn_llm_gpt.cpp:243-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L243-L245) —— 体检的兜底：任何字段读取异常都被 `catch` 统一转成 `load model failed`。这意味着 `model.json` 写错不会静默，而是「尽早失败」，这是接入新模型时定位字段拼写错误的第一线索。

#### 4.1.4 代码实践

**实践目标**：用决策树给一个真实模型归类。

**操作步骤**：

1. 打开 README 的支持模型表（[readme.md:48-60](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L48-L60)），挑两个家族：`MiniCPM4`（纯 LLM）与 `Qwen3.5`（VLM/混合）。
2. 对照上面的四类决策树，分别判定它们属于 A/B/C/D 哪一类。
3. 写下你判断的依据（提示：Qwen3.5 用到了 `sconv_cnt`/`gdr_cnt`，见构造函数 L109–L114）。

**需要观察的现象 / 预期结果**：MiniCPM4 应判为 A 或 B 类（零代码，仅 `model.json`）；Qwen3.5 应判为 D 类（需自定义算子与混合上下文）。如果你判反了，回到 4.1.2 的判断清单核对「注意力状态」一行。

#### 4.1.5 小练习与答案

**练习 1**：一个新模型只有标准多头注意力 + 基础 RoPE + BPE 分词，它属于哪一类？需要改 C++ 代码吗？
**答案**：A 类，零代码。只需写一份 `model.json`，`setting.rope.type` 填 `"RoPE"`、`tokenizer.type` 填 `"bpe"` 或 `"bbpe"` 即可。

**练习 2**：如果新模型用的是一种全新的旋转位置编码（四种变体都不匹配），构造函数会怎样？该怎么做？
**答案**：构造函数里 `rope_cfg["type"]` 不匹配任何分支（L121–L133），`rope_type` 保持成员默认值 `RoPE`（基础），结果**静默回退**到基础 RoPE，不会报错但推理结果会错。正确做法是 C 类：在 `rope_embed.cpp` 新增生成函数、在 `RoPE_Type` 枚举加值、并在构造函数与三个 prefill/generate 的 RoPE 分支里各加一个 `else if`。

---

### 4.2 model.json 的 setting 字段与构造函数配置读取

#### 4.2.1 概念说明

`model.json` 的 `setting` 块是接入新模型的**主控制面板**。它几乎全是选填字段（用 `contains` 守卫），缺省时走成员默认值。理解它的唯一方法，是把构造函数里 `config["setting"]...` 的每一行，反向标注成「这个字段控制了什么运行时行为」。

#### 4.2.2 核心流程：四组 setting 字段

`setting` 块对纯文本 LLM 有四组关键字段：

1. **层数计数 `attn_cnt` / `sconv_cnt` / `gdr_cnt`**：决定循环多少层、以及上下文用哪个派生类。
2. **`rope` 子块**：选 RoPE 变体 + 填维度/基/缩放参数。
3. **`functions` 子块**：工具调用相关 token（选填，缺省 -1 则工具功能静默关闭）。
4. **`vision` 子块**：纯文本模型填 `"close"` 即可（VLM 才展开，见 u5-l1）。

> 本讲聚焦纯文本接入；`vision` 块在 u5-l1 已详述，这里只在总表里标注「纯文本填 close」。

#### 4.2.3 源码精读

**层数计数 —— 决定 KV cache 层数与上下文类型**

[ncnn_llm_gpt.cpp:106-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L106-L114) —— 读取 `attn_cnt`（默认 32，成员声明见 [ncnn_llm_gpt.h:109](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L109)）、`sconv_cnt`、`gdr_cnt`（默认均 0）。注意三者都用 `contains` 守卫，**全选填**。`attn_cnt` 直接决定 prefill/generate 里 `for (int i = 0; i < attn_cnt; i++)` 循环多少层 KV cache（见 [ncnn_llm_gpt.cpp:296](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L296)），填错层数会导致 KV cache 槽位数与 `.param` 里导出的 `cache_k%d` 数量对不上、加载或推理时崩。

**RoPE 子块 —— 选变体并填参数**

[ncnn_llm_gpt.cpp:116-146](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L116-L146) —— `rope` 块的核心逻辑：

- L118–L120：`rope_head_dim` 选填，默认 64（[ncnn_llm_gpt.h:112](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L112)）。
- L121–L133：按 `type` 字符串（大小写敏感！）切换 `RoPE_Type` 枚举（[ncnn_llm_gpt.h:114-119](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L114-L119)）：`"LongRoPE"` / `"RoPE"` / `"NTKRoPE"` / `"YaRNRoPE"`。
- L135–L143：`rope_scaling` 子块填 `ntk_scaling_params`（六个 float），NTK/YaRN 必填、基础/LongRoPE 不用。
- L145：`rope_theta` 在 `rope` 块内**无 `contains` 守卫、直接 `get`**，所以只要写了 `rope` 块，`rope_theta` 就是**必填**。

**functions 子块 —— 工具调用 token（选填）**

[ncnn_llm_gpt.cpp:148-160](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L148-L160) —— 仅当 `functions.type == "tool_call"` 时解析 `tool_call_id` / `tool_call_end_id` 两个特殊 token 字符串为 id。缺省时两个 id 为 -1，`define_tools` 会直接 no-op（[ncnn_llm_gpt.cpp:988](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L988)），模型完全感知不到工具。

#### 4.2.4 代码实践

**实践目标**：为某个标准 decoder-only LLM 写出一份**完整可解析**的最小 `model.json`，并逐字段标注「被哪行代码读取、必填/选填」。

**操作步骤**：

1. 假设要接入一个 28 层、`rope_head_dim=128`、`rope_theta=1000000`、基础 RoPE、bbpe 分词的模型。
2. 依据构造函数 L58–L103 与 L106–L146，补全下面这份模板（示例代码，仓库未追踪任何 `model.json`，此为依源码推导）：

```jsonc
// 示例 model.json —— 依 ncnn_llm_gpt 构造函数推导，A 类零代码接入
{
  "params": {                                  // L58-L63，六个字段全必填，无 contains
    "decoder_param":      "decoder.ncnn.param",
    "decoder_bin":        "decoder.ncnn.bin",
    "embed_token_param":  "embed.ncnn.param",  // 注意真实键名是 embed_token_*，非 README 的 embed_*
    "embed_token_bin":    "embed.ncnn.bin",
    "proj_out_param":     "lm_head.ncnn.param",
    "proj_out_bin":       "lm_head.ncnn.bin"
  },
  "tokenizer": {                               // L84-L103
    "type": "bbpe",                            // L84-L85 选填，bbpe 开字节级编码（L91 末尾 type=="bbpe"）
    "vocab_file": "vocab.txt",                 // L87 必填
    "merges_file": "merges.txt",               // L88 必填
    "additional_special_tokens": ["<|im_start|>", "<|im_end|>"], // L94 必填（值可空数组）
    "eos": "<|im_end|>",                       // L99 必填，空串得 -1
    "bos": ""                                  // L102 必填，空串得 -1
  },
  "setting": {                                 // L106-L146
    "attn_cnt": 28,                            // L106-L108 选填，默认 32
    "rope": {                                  // L116 contains 守卫
      "rope_head_dim": 128,                    // L118 选填，默认 64
      "type": "RoPE",                          // L126 基础 RoPE，无需 rope_scaling
      "rope_theta": 1000000.0                  // L145 必填（rope 块内无 contains）
    }
    // 纯文本模型不写 vision 块即可，vision_type 默认 VISION_CLOSE（构造函数 L19、L175）
  }
}
```

3. 逐字段在源码里反查行号，确认你标的「必填/选填」与代码一致。

**需要观察的现象 / 预期结果**：把上述 JSON 的某个必填键（如 `rope_theta`）删掉再喂给构造函数，应在 L145 的 `.get<float>()` 抛异常、被 L243 catch 成 `load model failed`。把 `type` 写成 `"rope"`（小写）则不报错、但静默回退基础 RoPE（推理结果错）。

**待本地验证**：上述「删字段必报错」「大小写静默回退」行为需有真实模型目录才能跑通确认；无模型时可只做源码侧的行号核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rope_theta` 是必填，而 `rope_head_dim` 是选填？
**答案**：`rope_head_dim` 外面套了 `if (rope_cfg.contains("rope_head_dim"))`（L118），缺失时用成员默认 64；而 `rope_theta` 是 `rope_cfg["rope_theta"].get<float>()`（L145），无 `contains` 守卫，缺失即抛异常。判定标准就是「有没有 `contains` 包一层」。

**练习 2**：接入一个用 NTK-aware 缩放的长上下文模型，`setting.rope` 块要加哪些字段？
**答案**：`"type": "NTKRoPE"`（L128）触发 NTK 分支，并新增 `rope_scaling` 子块，填齐 `alpha` / `beta_fast` / `beta_slow` / `factor` / `mscale` / `mscale_all_dim` 六个 float（L137–L142，全是 `get` 无守卫，必填）。

---

### 4.3 分词器类型选择

#### 4.3.1 概念说明

ncnn_llm 的分词器是「构造时定型」的——构造函数根据 `tokenizer.type` 决定实例化哪种分词器，之后整个推理过程都用同一个对象。主运行时 `ncnn_llm_gpt` 实际只支持 **BPE 家族**（bpe / bbpe 两种），这点常被误解，需要先澄清。

#### 4.3.2 核心流程

```
tokenizer.type（选填，默认 "bpe"）
        │
        ├── "bpe"  → BpeTokenizer，use_byte_encoder=false（SentencePiece 预分词，▁ 表示空格）
        ├── "bbpe" → BpeTokenizer，use_byte_encoder=true （字节级，整段 BPE）
        └── （gpt 运行时不识别其它值；unigram 只在嵌入运行时 ncnn_embedding 用）
```

无论哪种，构造函数都会：加载 vocab/merges → 注册额外特殊令牌 → 把 eos/bos 字符串解析成 id。

#### 4.3.3 源码精读

[ncnn_llm_gpt.cpp:83-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L83-L92) —— `type` 默认 `"bpe"`（L83），只用来决定传给 `LoadFromFiles` 的最后一个布尔参数 `type == "bbpe"`（L91），即**是否启用字节级编码**。bpe 与 bbpe 是同一个 `BpeTokenizer` 类，靠 `use_byte_encoder` 开关区分（详见 u3-l2）。注意这里没有 unigram 分支——`"unigram"` 传进来会落到 `type == "bbpe"` 为 false、按普通 bpe 处理，与词表训练方式不符会大量退化为 unk。

[ncnn_llm_gpt.cpp:94-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L94-L97) —— 注册 `additional_special_tokens`（如 `<|im_start|>`、`<|image_pad|>`）。这一步对工具调用与 VLM 都关键：encode 时这些字符串会被当成**原子整体**识别为一个 id、绕过 BPE 切分（详见 u3-l1）。

[ncnn_llm_gpt.cpp:99-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L99-L103) —— eos/bos 用 `.at()`（必需键，拼错字符串会抛异常）解析，空串保护得 -1。这两个 id 之后会驱动 generate 的停止条件（[ncnn_llm_gpt.cpp:875](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L875) `if (ctx->cur_token == eos) break;`）。

#### 4.3.4 代码实践

**实践目标**：判断一个新模型该填 `bpe` 还是 `bbpe`，并理解填错的后果。

**操作步骤**：

1. 查新模型 HF 仓库的 `tokenizer_config.json`，看它是否有 `byte_level` 预分词器（GPT/Llama/Qwen 系通常是 bbpe，SentencePiece 原生模型通常是 bpe）。
2. 在源码侧确认：构造函数 L91 的 `type == "bbpe"` 只控制 `LoadFromFiles` 第 6 个参数，即 `BpeTokenizer` 的 `use_byte_encoder`。
3. 回顾 u3-l2 的结论：`use_byte_encoder` 必须与词表训练方式一致，否则退化为 unk。

**需要观察的现象 / 预期结果**：填错类型不会让构造失败，但 `encode` 出的 token 序列会大量出现 `unk_id`，推理输出变成乱码。这是接入新模型时「能加载但结果全错」的最常见原因之一。

#### 4.3.5 小练习与答案

**练习 1**：把 `tokenizer.type` 整个字段删掉会怎样？
**答案**：走默认 `"bpe"`（L83）。若模型实际是 bbpe 训练的，则 `use_byte_encoder=false`，与词表不匹配，退化为 unk。所以「不填」不等于「安全」，必须显式填对。

**练习 2**：为什么 `<|image_pad|>` 必须出现在 `additional_special_tokens` 里，而不能依赖 BPE 自然切分？
**答案**：BPE 会把 `<|image_pad|>` 当普通文本切成 `<`、`|`、`image` 等碎片，无法得到单一占位 id；而下游 `inject_image_embeds`（u5-l3）需要靠**一个** `image_pad_id` 定位占位符再整段替换成图像嵌入。注册为 special token 后，encode 用最长匹配把它识别为原子 id（构造函数 L223–L226 再用 `find` 取出该 id）。

---

### 4.4 RoPE 变体选择

#### 4.4.1 概念说明

位置编码是接入新模型时**最容易踩坑**的一环：构造函数里的 `type` 字符串大小写敏感、不匹配会静默回退（见 4.1.5 练习 2）。本节给一张「变体 → 函数 → 配置」的对照表，作为接入时的查表依据。原理（inv_freq、mscale、short/long_factor）已在 u4-l1 详述，这里只讲「接入时怎么选」。

#### 4.4.2 核心流程：四种变体的配置要求

| `setting.rope.type` | 枚举值 ([h:114-119](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L114-L119)) | 调用的生成函数 ([rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h)) | 额外必填配置 |
| --- | --- | --- | --- |
| `"RoPE"` | `RoPE` | `generate_rope_embed_cache`（[h:35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L35)） | 仅 `rope_head_dim`、`rope_theta` |
| `"NTKRoPE"` | `NTK_RoPE` | `generate_ntk_rope_embed_cache`（[h:15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L15)） | + `rope_scaling`（六参数） |
| `"YaRNRoPE"` | `YARN_RoPE` | `generate_yarn_rope_embed_cache`（[h:25](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L25)） | + `rope_scaling`（六参数） |
| `"LongRoPE"` | `LongRoPE` | `generate_rope_embed_cache_LongRoPE`（[h:39](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L39)） | + `short_factor`/`long_factor`/`original_max_position_embeddings` |

> 关键提醒：`type` 字符串**精确匹配**，`"ntkrope"`、`"yarn"` 都不会命中，会静默回退基础 RoPE。

#### 4.4.3 源码精读

变体的「选择」发生在构造函数，「使用」散落在 prefill/generate 共四处（首发主体、首发末位、多轮 prefill、generate 每步）。先看选择点：

[ncnn_llm_gpt.cpp:121-133](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L121-L133) —— `type` 字符串到枚举的映射。注意 LongRoPE 分支（L122–L125）额外读取三个向量/标量并**直接 `get` 无 `contains`**，所以选了 LongRoPE 就必须配齐这三项。

再看使用点之一（其余三处结构完全相同）：

[ncnn_llm_gpt.cpp:256-266](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L256-L266) —— 首发 prefill 主体批次的 RoPE 分支：按 `rope_type` 四选一调用对应生成函数。**接入新变体（C 类）时，构造函数、以及全部四处使用点都要同步加 `else if`**，漏一处会导致某条路径用错 RoPE。

#### 4.4.4 代码实践

**实践目标**：在源码里定位 RoPE 的全部四处分支点，理解「加一个新变体」要改几处。

**操作步骤**：

1. 用 Grep 在 `src/ncnn_llm_gpt.cpp` 里搜 `rope_type == RoPE_Type::LongRoPE`，数它出现在几个函数里。
2. 预期命中五处：首发 prefill 主体（L256）与末位（L333）、多轮 prefill 主体（L649）与末位（L746）、generate 每步（L895）。

**需要观察的现象 / 预期结果**：共五处分支（首发主体/末位 + 多轮主体/末位 + generate）。这告诉你「新增一种 RoPE 变体」的最小改动面：构造函数选型 1 处 + 五处使用点各加一个 `else if` + `rope_embed.cpp` 新增一个生成函数 + 头文件新增枚举值与签名。

#### 4.4.5 小练习与答案

**练习 1**：LongRoPE 的 `short_factor` 和 `long_factor` 在运行时如何被选用？
**答案**：在 `generate_rope_embed_cache_LongRoPE` 内部按 `seqlen` 是否超过 `original_max_position_embeddings` 切换 SHORT / LONG 两套因子（详见 u4-l1）。构造函数只负责把两套都读进来（L123–L124），选用逻辑在生成函数里。

**练习 2**：为什么说 RoPE 的 `type` 大小写错误是「最难排查的接入 bug」？
**答案**：因为它**不报错**。`type` 不匹配任何 `else if`（L121–L133），`rope_type` 保持默认 `RoPE`，模型能加载、能推理、不崩，但位置编码全错、输出语义错乱。排查只能靠逐字核对字符串与构造函数分支。

---

### 4.5 共享运行时接线与混合架构例外

#### 4.5.1 概念说明

前面四节都在讲「配置怎么填」。本节回答最后一个关键问题：**填好配置之后，新模型在 prefill/generate 里到底走哪条代码路径？** 这关系到「跨模型族共享 decoder + KV cache 运行时」这条设计主线。

核心结论先抛出：**标准模型走共享运行时（零代码），混合架构（`sconv_cnt` 或 `gdr_cnt` > 0）必须内联展开 decoder（唯一例外）**。

#### 4.5.2 核心流程：上下文类型决定 decoder 路径

```
create_ctx(sconv_cnt, gdr_cnt)            ← ncnn_llm_gpt.cpp:9-14
        │
        ├── sconv_cnt==0 且 gdr_cnt==0 → ncnn_llm_gpt_base_ctx（仅 KV cache）
        │                                    │
        │                                    ▼
        │           generate 里走共享函数 llm_run_decoder_with_kv（L914）
        │           （跨模型族共享，OCR/ASR 也用它）
        │
        └── sconv_cnt>0 或 gdr_cnt>0  → qwen3_5_ctx（KV cache + sconv_cache + gdr_cache）
                                             │
                                             ▼
                  generate 里内联展开 decoder（L916-L968），
                  手动喂入/回收 cache_conv%d、cache_gdr%d 槽位
                  （共享函数不认识这些槽位，故无法复用）
```

#### 4.5.3 源码精读

**上下文类型选择 —— 配置驱动**

[ncnn_llm_gpt.cpp:9-14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14) —— `create_ctx` 仅凭 `sconv_cnt`/`gdr_cnt` 决定上下文类型。这两个值来自构造函数读取的 `setting`（L109–L114），**完全配置驱动**：填了非零值就自动切到 `qwen3_5_ctx`，无需改 create_ctx。

[ncnn_llm_gpt.h:71-89](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L71-L89) —— `qwen3_5_ctx` 在基类上下文基础上多出 `sconv_cache` / `gdr_cache` 两个向量，并正确实现了 `clone()`（深拷贝这三类状态，支撑多轮分叉）。

**共享路径 —— 标准模型（A/B 类）**

[ncnn_llm_gpt.cpp:912-915](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L915) —— generate 里 `if (!qwen_ctx)` 分支调用共享函数 `llm_run_decoder_with_kv`。这就是「共享运行时」的落地：**任何标准模型，只要不是 qwen3_5_ctx，decoder 调用都汇聚到这一个函数**，与 OCR/ASR 共用。

**例外路径 —— 混合架构（D 类）**

[ncnn_llm_gpt.cpp:916-968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L916-L968) —— `else` 分支为 qwen3.5 混合架构**内联展开** decoder：手动 `input` 了 `cache_k%d`/`cache_v%d`（L923–L929）、`cache_conv%d`（L931–L935）、`cache_gdr%d`（L936–L940）三类状态，再 `extract` 回收。之所以不能复用共享函数，是因为 `llm_run_decoder_with_kv` 只认 KV 槽位、不认识 conv/gdr 槽位（详见 u7-l4）。

**自定义算子注册 —— D 类的前置条件**

[ncnn_llm_gpt.cpp:73](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L73) —— `register_gdr_layers(*decoder_net)` 必须在 `load_param`/`load_model`（L75–L76）**之前**调用，否则 ncnn 解析 `.param` 时遇到 `GatedDeltaRule`/`ShortConv` 算子名会找不到构造函数而加载失败。注册实现见 [gdr.cpp:358-362](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L358-L362)，且只作用于 `decoder_net`。

#### 4.5.4 代码实践

**实践目标**：对比「标准模型」与「混合架构」在 generate 里的 decoder 调用差异，体会共享运行时的边界。

**操作步骤**：

1. 读 [ncnn_llm_gpt.cpp:911-915](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L911-L915)：标准模型一行 `llm_run_decoder_with_kv(...)` 搞定。
2. 读 [ncnn_llm_gpt.cpp:916-968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L916-L968)：混合架构用了约 50 行内联代码完成同样的事，外加 conv/gdr 两类状态。
3. 数一数：内联路径比共享路径多了几类「状态槽位」的喂入与回收循环。

**需要观察的现象 / 预期结果**：内联路径多出 `cache_conv%d`/`out_cache_conv%d` 与 `cache_gdr%d`/`out_cache_gdr%d` 各一对循环（共 4 段），对应 `sconv_cnt`、`gdr_cnt`。这就是「混合架构是共享运行时唯一例外」的代码体现。

**待本地验证**：若要实际触发两条路径，需分别准备一个标准模型目录与一个 Qwen3.5 混合架构目录，在 generate 设断点观察 `qwen_ctx` 的真假。

#### 4.5.5 小练习与答案

**练习 1**：接入一个标准模型时，需不需要关心 `qwen3_5_ctx` 与内联 decoder？
**答案**：不需要。`create_ctx` 在 `sconv_cnt==0 && gdr_cnt==0` 时返回 `ncnn_llm_gpt_base_ctx`，generate 里 `dynamic_pointer_cast<qwen3_5_ctx>` 返回空指针，自动走 `llm_run_decoder_with_kv` 共享路径。标准模型接入者只需保证 `attn_cnt` 正确。

**练习 2**：为什么 `register_gdr_layers` 只注册到 `decoder_net`，而不注册到 `embed_net`/`proj_out_net`？
**答案**：`GatedDeltaRule`/`ShortConv` 是 Transformer 解码块内部的算子，只会出现在 decoder 的 `.param` 里；embed（查表）和 proj_out（线性投影）不含这些算子。且注册必须在各 net 的 `load_param` 之前，但只需对真正会引用这些算子的 net 注册（[ncnn_llm_gpt.cpp:73](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L73) 紧跟其后只 load decoder）。

## 5. 综合实践

把本讲全部内容串成一份**新模型接入文档**。这是本手册的毕业设计——你不需要真的有模型权重，但要能产出一份让另一位工程师照着就能接的清单。

**任务**：假设要接入一个虚构的新模型 `FooLM-1B`，其特征如下：

- 32 层标准多头注意力，无混合架构。
- 使用 NTK-aware RoPE（长上下文），`rope_head_dim=96`，`rope_theta=500000`。
- GPT 风格字节级 BPE 分词，eos 为 `<|endoftext|>`，无 bos。
- 支持工具调用，工具调用标记为 `<|tool_call|>` / `<|/tool_call|>`。

请产出三份交付物：

1. **决策归类**：用 4.1 的四类决策树判定 FooLM-1B 属于哪一类（A/B/C/D），并写明依据。
2. **完整 model.json**：参考 4.2.4 的模板，写出 FooLM-1B 的 `model.json`，要求每一行都对应构造函数里真实存在的读取代码（标注行号），且 `rope_scaling` 六个参数都列出（值可写成占位 `<from HF config>`）。
3. **改动清单**：按下面的表格格式，列出接入 FooLM-1M 需要触碰的每一个点，并标注「是否需要改 C++ 代码」。

参考改动清单表格格式：

| 接入点 | 文件 / 行号 | FooLM 是否需要改 | 说明 |
| --- | --- | --- | --- |
| 网络路径 | ncnn_llm_gpt.cpp:58-63 | 否（仅 JSON） | 填 params 六字段 |
| 分词器 | ncnn_llm_gpt.cpp:83-92 | 否（仅 JSON） | type 填 `bbpe` |
| eos/bos | ncnn_llm_gpt.cpp:99-103 | 否（仅 JSON） | eos 填字符串，bos 填空串 |
| 层数 | ncnn_llm_gpt.cpp:106-114 | 否（仅 JSON） | attn_cnt=32，sconv/gdr 不填 |
| RoPE 选型 | ncnn_llm_gpt.cpp:121-133 | 否（仅 JSON） | type 填 `NTKRoPE` |
| RoPE 使用点 | ncnn_llm_gpt.cpp:256 等 5 处 | 否 | NTKRoPE 已被分支覆盖 |
| 工具调用 token | ncnn_llm_gpt.cpp:148-160 | 否（仅 JSON） | functions.type=tool_call + 两个 id |
| 上下文类型 | ncnn_llm_gpt.cpp:9-14 | 否 | 非混合架构，自动 base_ctx |
| decoder 路径 | ncnn_llm_gpt.cpp:912-915 | 否 | 自动走共享 llm_run_decoder_with_kv |

**预期结论**：FooLM-1B 属于 **B 类（零代码）**——尽管它用了长上下文 NTK 变体与工具调用，但这些都被构造函数与运行时覆盖，**整份接入只需要一个 `model.json`、不改一行 C++**。这正是 ncnn_llm「配置驱动」设计的目标。如果你在改动清单里标出了任何「需要改 C++」，回到对应章节核对该能力是否真未被覆盖。

> 如果你的 FooLM 改成「带线性注意力 + 短卷积」，它就升格为 D 类，改动清单里「上下文类型」「decoder 路径」「自定义算子」三行会全部变成「是」，且要新增 `qwen3_5_ctx` 风格的派生类与内联 decoder——这就是 4.5 讲的例外。

## 6. 本讲小结

- 接入新模型的第一步是**用决策树归类**（A/B 零代码、C 需新 RoPE 函数、D 需自定义算子），依据全部写在构造函数这台「配置解析器」里。
- `model.json` 的 `setting` 块四组字段——层数计数（`attn_cnt`/`sconv_cnt`/`gdr_cnt`）、`rope`、`functions`、`vision`——几乎全选填（`contains` 守卫），唯一例外是 `rope` 块内的 `rope_theta` 与各变体的专属参数（直接 `get`、必填）。
- 分词器在主运行时只支持 **bpe/bbpe**（同一 `BpeTokenizer`、靠 `use_byte_encoder` 区分），填错不报错但推理变乱码；unigram 只在嵌入运行时用。
- RoPE 四变体（`RoPE`/`NTKRoPE`/`YaRNRoPE`/`LongRoPE`）由 `type` 字符串精确匹配切换，**大小写敏感、不匹配静默回退基础 RoPE**；新增变体需同步改构造函数选型 + 五处使用点。
- 标准模型在 generate 里走共享函数 `llm_run_decoder_with_kv`（跨 LLM/OCR/ASR 共用）；**混合架构（`sconv_cnt`/`gdr_cnt`>0）是唯一例外**，用 `qwen3_5_ctx` 并内联展开 decoder。
- 一句话总纲：**能配置驱动的绝不改代码**——只要新模型的位置编码、分词、注意力状态被运行时覆盖，接入就只是一份 `model.json`。

## 7. 下一步学习建议

本讲是学习手册的终点，但没有终点是真正的终点。建议沿三个方向继续：

1. **亲手接入一个真实模型**：从 [README 的 Model Zoo 镜像](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L88-L103) 选一个尚未在支持表里的轻量模型，按本讲的综合实践流程产出 `model.json` 并用 `llm_ncnn_run` 跑通。这是把全书知识内化的最快路径。
2. **深入导出侧**：本讲假设模型已转成 ncnn 格式。真正的「从零接入」还包括从 HuggingFace 导出，这部分见 u8-l4（`export/` 脚本），重点理解两个隐式契约——「词表行号 == token id」与 KV cache 的 `cache_k%d`/`out_cache_k%d` 槽位命名，它们是 C++ 运行时与导出脚本之间的对账基准。
3. **向 ncnn 下游延伸**：当模型架构需要 ncnn 原生不支持的算子时（D 类），参考 u7-l4 的 `GatedDeltaRule`/`ShortConv` 写法，学习如何继承 `ncnn::Layer`、设标志位、写 creator/destroyer 并 `register_custom_layer`；进一步可阅读 ncnn 官方文档理解 `pnnx` 如何把 PyTorch 算子映射成 ncnn 层。

至此，《ncnn_llm 学习手册》八个单元全部完成：从项目全貌（U1）到 LLM 主链路（U2）、分词与采样（U3）、位置编码（U4）、视觉语言（U5）、其他模态（U6）、工程化能力（U7），最终回到二次开发（U8）。你已经具备从「读源码」到「改源码」的完整能力。
