# 模型目录与 model.json 配置体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `model.json` 在 ncnn_llm 里扮演的角色——它是「模型目录的说明书」。
- 看懂 `model.json` 的三大顶层块：`params`、`tokenizer`、`setting`。
- 把每一个配置字段，精确对应到 `ncnn_llm_gpt` 构造函数里读取它的那几行代码。
- 区分哪些字段是「必填」（缺了构造函数就抛异常），哪些是「选填」（有默认值）。
- 为一个纯文本 LLM 手写出一份最小可用的 `model.json`。

本讲承接 [u1-l3（目录结构与源码地图）](u1-l3-directory-and-source-map.md)：你已经知道模型权重放在 `assets/<模型名>/` 目录下；这一讲回答「目录里那个 `model.json` 到底写了什么、代码怎么吃掉它」。它也是后续 [u2（LLM 推理主链路）](u2-l1-base-class-common.md) 的前置——构造函数把 `model.json` 翻译成一堆成员变量，prefill/generate 正是靠这些成员变量驱动的。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 一个模型目录里有什么

ncnn_llm 不读 `.safetensors` 也不读 `.onnx`，它读的是 ncnn 自己的两类文件：

- `*.ncnn.param`：网络结构（文本，可读）。
- `*.ncnn.bin`：网络权重（二进制）。

一个完整的模型通常被切成三张子网，每张子网各有一对 `.param`/`.bin`：

| 子网 | 作用 |
| --- | --- |
| embed（嵌入） | 把 token id 变成向量 |
| decoder（解码器） | Transformer 主体，吃向量、吐隐状态、产出 KV cache |
| proj_out / lm_head（输出头） | 把隐状态变成词表上的 logits |

加上分词器文件（`vocab.txt`、`merges.txt` 等），再配上一个 `model.json`，就是一个可运行目录。`model.json` 的职责就是告诉运行时：**这些文件分别叫什么名字、分词器是什么类型、模型有哪些超参数**。

### 2.2 nlohmann::json 的两个基本用法

构造函数用 [nlohmann_json](https://github.com/nlohmann/json) 解析 `model.json`。你只需记住两点：

- `config["a"]["b"]`：用 `operator[]` 逐层下钻取值。**若某一层键不存在，会抛异常。**
- `config["a"].contains("b")`：先判断键存不存在，再决定要不要取。这是「选填字段」的标准写法。

记住这两个操作，整段构造函数的「必填 vs 选填」逻辑就看懂了一半。

### 2.3 构造函数 = 配置翻译器

可以把 `ncnn_llm_gpt` 的构造函数理解成一台「配置翻译机」：输入是磁盘上的 `model.json`，输出是对象内部的一堆成员变量（网络指针、分词器、`attn_cnt`、`rope_theta`……）。本讲关心的是「翻译」这一步，翻译完之后这些成员变量怎么用，留给 u2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ncnn_llm_gpt.cpp` | **本讲主角**。构造函数（L18–L246）逐字段读取 `model.json` |
| `src/ncnn_llm_gpt.h` | 声明构造函数写入的成员变量及其默认值 |
| `src/utils/tokenizer/bpe_tokenizer.h` | `LoadFromFiles` 的签名，决定 `tokenizer` 块怎么用 |
| `src/utils/tokenizer/tokenizer_types.h` | `SpecialTokensConfig` 等分词器配置结构 |
| `examples/llm_ncnn_run/main.cpp` | CLI 里构造 `ncnn_llm_gpt` 的那一行，串起 u1-l4 |
| `readme.md` | 给出一份 `model.json` 示例（**注意：与真实代码有出入，本讲会指出**） |

## 4. 核心概念与源码讲解

### 4.1 model.json 的整体结构与加载入口

#### 4.1.1 概念说明

`model.json` 是每个模型目录的「说明书」。它的顶层通常有三个块：

```text
model.json
├── params      ：三张子网（embed / decoder / proj_out）的文件名
├── tokenizer   ：分词器类型 + 分词器文件 + 特殊令牌
└── setting     ：模型超参（attn_cnt / rope / vision / functions …）
```

有时候还会看到一个顶层 `model_type` 字段——但要特别注意：**`ncnn_llm_gpt`（LLM/VLM 运行时）的构造函数并不读取 `model_type`，也不读取 `setting.hidden_size`**。这两个字段是给 OCR / ASR / embedding 等其它运行时用的（见 `src/ncnn_llm_ocr.cpp`、`src/ncnn_llm_asr.cpp`）。README 里的示例 JSON 把它们一并列出，容易让人误以为 gpt 构造函数也会读——本讲一律以 gpt 构造函数的真实代码为准。

#### 4.1.2 核心流程

构造函数开头的「加载入口」非常简短：

1. 打开 `model_path + "/model.json"`。
2. 用 `ifs >> config` 把整个文件解析进一个 `nlohmann::json` 对象 `config`。
3. 此后所有字段读取都基于这个 `config`。
4. 任何一步抛异常，都会被结尾的 `catch` 统一包成 `"ncnn_llm_gpt load model failed: ..."` 重新抛出。

`model_path` 从哪来？从 CLI。在 `main.cpp` 里：

```cpp
ncnn_llm_gpt model(opt.model_path, opt.use_vulkan, opt.num_threads, opt.vulkan_device);
```

`opt.model_path` 就是 u1-l4 里 `normalize_model_path` 归一化后的路径（裸名会被补成 `./assets/<裸名>`）。

#### 4.1.3 源码精读

打开并解析 `model.json`：

[读取 model.json 到 config 对象 — src/ncnn_llm_gpt.cpp:21-25](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L21-L25)

```cpp
json config;
{
    std::ifstream ifs(model_path + "/model.json");
    ifs >> config;
}
```

CLI 侧的构造调用，把命令行解析出的路径喂给构造函数：

[main.cpp 构造 ncnn_llm_gpt — examples/llm_ncnn_run/main.cpp:41-43](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L41-L43)

最外层的异常兜底，所有字段读取错误最终都汇到这里：

[catch 统一兜底 — src/ncnn_llm_gpt.cpp:243-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L243-L245)

```cpp
} catch (std::exception &e) {
    throw std::runtime_error(std::string("ncnn_llm_gpt load model failed: ") + e.what());
}
```

> 小贴士：以后你在终端看到 `ncnn_llm_gpt load model failed: ...`，多半就是 `model.json` 里某个**必填**字段写错或漏写了——这正是本讲要教你的「避坑」点。

#### 4.1.4 代码实践

**实践目标**：确认构造函数确实在找 `<model_path>/model.json`，并体会缺文件时的报错。

**操作步骤**：

1. 准备一个空的临时目录 `assets/_fake_model/`，里面**不要**放 `model.json`。
2. 运行 `xmake run llm_ncnn_run --model ./assets/_fake_model`。

**需要观察的现象**：程序立刻报错退出，错误信息形如 `ncnn_llm_gpt load model failed: ...`（`ifstream` 打开失败或 `ifs >> config` 解析失败抛异常）。

**预期结果**：报错而非崩溃，因为 `catch` 把异常转成了可读信息。如果你放一个**语法错误**的 `model.json`（比如缺一个花括号），也会被同样捕获——说明解析发生在构造最早期。

**待本地验证**：具体错误文本以本地运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把网络结构、权重、分词器等信息直接写进 `model.json`，而要拆成一堆外部文件？

**参考答案**：`model.json` 只放「轻量元信息」（文件名、类型、超参），真正的权重是几十 MB 到几 GB 的二进制，必须单独成文件由 ncnn 的 `load_param/load_model` 流式加载；分词器词表也较大且格式各异，独立成文件更利于复用与替换。

**练习 2**：`config["params"]["decoder_param"]` 和 `config["setting"].contains("attn_cnt")` 这两种写法，哪种在键缺失时会抛异常？

**参考答案**：前者会。`operator[]` 在键不存在时抛异常；`contains` 只是判断，返回 `false` 而不抛。所以前者对应「必填」，后者对应「选填」。

---

### 4.2 params 块：读取 ncnn 网络文件路径

#### 4.2.1 概念说明

`params` 块的职责只有一件事：告诉构造函数三张子网各自的 `.param` / `.bin` 文件叫什么名字。ncnn_llm 把完整模型拆成 embed / decoder / proj_out 三张网，分别由三个 `ncnn::Net` 对象持有（见头文件里的 `embed_net` / `decoder_net` / `proj_out_net`）。

#### 4.2.2 核心流程

1. 从 `config["params"]` 取出六个文件名字符串。
2. 每个都拼上 `model_path + "/"` 前缀，得到完整路径。
3. 用 `load_param(...)` 和 `load_model(...)` 把三张网分别加载进内存。

#### 4.2.3 源码精读

读取六个路径并拼接前缀：

[读取 params 中的网络文件名 — src/ncnn_llm_gpt.cpp:58-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L58-L63)

```cpp
std::string decoder_param  = model_path + "/" + config["params"]["decoder_param"].get<std::string>();
std::string decoder_bin    = model_path + "/" + config["params"]["decoder_bin"].get<std::string>();
std::string embed_param    = model_path + "/" + config["params"]["embed_token_param"].get<std::string>();
std::string embed_bin      = model_path + "/" + config["params"]["embed_token_bin"].get<std::string>();
std::string proj_out_param = model_path + "/" + config["params"]["proj_out_param"].get<std::string>();
std::string proj_out_bin   = model_path + "/" + config["params"]["proj_out_bin"].get<std::string>();
```

随后真正加载（中间还插了一句 `register_gdr_layers`，那是 u7 的自定义算子，这里先忽略）：

[load_param / load_model 加载三张网 — src/ncnn_llm_gpt.cpp:75-80](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L75-L80)

> ⚠️ **避坑：README 示例与真实代码不一致**。README 给的示例用的是 `embed_param` / `embed_bin` / `lm_head_param` / `lm_head_bin`（见 [readme.md:233-259](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L233-L259)），但构造函数实际读取的键是 **`embed_token_param` / `embed_token_bin` / `proj_out_param` / `proj_out_bin`**。只有 `decoder_param` / `decoder_bin` 两项与 README 一致。**当你手写 `model.json` 时，请以本节代码里的键名为准**，否则会触发 `load model failed`。这正应了 u1-l3 的结论：README 会滞后，建立认知要以源码为准。

这六个字段都通过 `operator[]` 直接取值、**没有** `contains` 守卫，所以它们都是**必填**。

#### 4.2.4 代码实践

**实践目标**：亲手验证「键名写错就加载失败」。

**操作步骤**：

1. 找一个可用的文本模型目录（从 [模型镜像](https://mirrors.sdu.edu.cn/ncnn_modelzoo/) 下载，放到 `assets/`）。
2. 复制它的 `model.json`，把 `embed_token_param` 故意改成 README 写的 `embed_param`。
3. 运行 `xmake run llm_nn_run --model <该目录>`。

**需要观察的现象**：构造函数在 L60 处 `config["params"]["embed_token_param"]` 取不到值，抛出 nlohmann json 异常，被 L243 的 `catch` 捕获。

**预期结果**：终端打印 `ncnn_llm_gpt load model failed: ...`，程序退出。

**待本地验证**：若无可用模型，可只做「源码阅读」——在 L58-L63 旁标注每个键对应哪张子网。

#### 4.2.5 小练习与答案

**练习 1**：`embed_token_param` 和 `proj_out_param` 分别对应模型的哪两个功能环节？

**参考答案**：`embed_token_param` 对应「输入端嵌入」——把 token id 向量化；`proj_out_param` 对应「输出端投影（lm_head）」——把 decoder 的隐状态映射回词表得到 logits。

**练习 2**：为什么路径拼接要用 `model_path + "/" + 文件名`，而不是让用户在 JSON 里直接写完整路径？

**参考答案**：这样 `model.json` 只记「相对文件名」，整个模型目录可以整体搬动（换盘符、换机器）而不必改 JSON；运行时由 `model_path`（来自 `--model`）提供目录根，灵活且可移植。

---

### 4.3 tokenizer 块：读取分词器与特殊令牌

#### 4.3.1 概念说明

`tokenizer` 块告诉构造函数：用什么分词器、词表和合并表在哪、有哪些特殊令牌（eos / bos / 额外特殊令牌）。ncnn_llm 的分词器统一用 `BpeTokenizer` 承载，但通过 `type` 字段在「普通 BPE」和「字节级 BPE（bbpe）」之间切换。

几个术语：

- **BPE**：Byte-Pair Encoding，按合并规则把文本切成子词。
- **bbpe**：byte-level BPE，先把文本按字节编码再做 BPE，能处理任意字符（Qwen 系常用）。
- **eos / bos**：结束 / 起始特殊令牌，生成时遇到 eos 就停。
- **additional_special_tokens**：额外的特殊令牌，比如图像占位 `<|image_pad|>`、工具标记等。

#### 4.3.2 核心流程

1. 读 `tokenizer.type`（缺省默认 `"bpe"`）；若是 `"bbpe"` 则开启字节级编码。
2. 读 `vocab_file` / `merges_file`，调用 `BpeTokenizer::LoadFromFiles` 构造分词器。
3. 读 `additional_special_tokens`（字符串数组），逐个 `AddAdditionalSpecialToken` 注册。
4. 读 `eos` / `bos` 字符串，用分词器把它们翻译成数字 id（空字符串 → -1）。

#### 4.3.3 源码精读

读取 `type` 并据此构造分词器（注意最后一个参数 `type == "bbpe"` 控制 byte encoder 开关）：

[tokenizer.type 与 LoadFromFiles — src/ncnn_llm_gpt.cpp:83-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L83-L92)

```cpp
std::string type = "bpe";
if (config["tokenizer"].contains("type")) {
    type = config["tokenizer"]["type"].get<std::string>();
}
std::string vocab_file = model_path + "/" + config["tokenizer"]["vocab_file"].get<std::string>();
std::string merges_file = model_path + "/" + config["tokenizer"]["merges_file"].get<std::string>();

bpe = std::make_shared<BpeTokenizer>(BpeTokenizer::LoadFromFiles(
    vocab_file, merges_file, SpecialTokensConfig{}, false, true, type == "bbpe"
));
```

`LoadFromFiles` 的完整签名（对照理解上面六个实参）：

[LoadFromFiles 签名 — src/utils/tokenizer/bpe_tokenizer.h:16-21](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h#L16-L21)

注册额外特殊令牌、解析 eos / bos：

[additional_special_tokens / eos / bos — src/ncnn_llm_gpt.cpp:94-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L94-L103)

```cpp
std::vector<std::string> additional_special_tokens =
    config["tokenizer"]["additional_special_tokens"].get<std::vector<std::string>>();
for (const auto& token : additional_special_tokens) {
    bpe->AddAdditionalSpecialToken(token);
}

auto eos_token = config["tokenizer"]["eos"].get<std::string>();
eos = (eos_token != "") ? bpe->token_to_id().at(eos_token) : -1;

auto bos_token = config["tokenizer"]["bos"].get<std::string>();
bos = (bos_token != "") ? bpe->token_to_id().at(bos_token) : -1;
```

> 🔑 **关键区分**：`vocab_file`、`merges_file`、`additional_special_tokens`、`eos`、`bos` 这五个都是**必填键**（没有 `contains` 守卫），但其中 `additional_special_tokens` 允许是空数组 `[]`、`eos`/`bos` 允许是空字符串 `""`（空字符串会得到 id = -1，表示「不使用」）。唯独 `type` 是选填，缺省时按 `"bpe"` 处理。

#### 4.3.4 代码实践

**实践目标**：理解 `type` 字段如何改变分词器行为。

**操作步骤**（源码阅读型）：

1. 在 L91 处看到 `type == "bbpe"` 被作为 `use_byte_encoder` 实参传给 `LoadFromFiles`。
2. 打开 `src/utils/tokenizer/bpe_tokenizer.h`，找到 `use_byte_encoder` 相关逻辑（byte encoder 会把文本先映射到 256 个字节）。
3. 设想同一句 `"你好"`：用 `bpe`（不开 byte encoder）和 `bbpe`（开 byte encoder）切出的 token 序列会不同。

**需要观察的现象**：bbpe 下中文等多字节字符会被拆成字节级 token。

**预期结果**：能口头解释「为什么 Qwen 这类模型在 `model.json` 里要写 `"type": "bbpe"`」——因为它需要 byte-level 编码来覆盖全 Unicode。

> 字节级编码的具体算法留到 [u3-l2（BPE/BBPE 分词器实现）](u3-l2-bpe-tokenizer.md) 精读，本讲只需记住 `type` 字段的这个开关作用。

#### 4.3.5 小练习与答案

**练习 1**：如果你的模型既不需要 bos 也不需要额外特殊令牌，`tokenizer` 块该怎么写才不会让构造函数抛异常？

**参考答案**：键不能省，但值可以「空」：`"bos": ""`、`"additional_special_tokens": []`。`eos` 同理可填 `""` 得到 -1，但通常 eos 是生成停止的关键，真实模型一般都会给一个有效值。

**练习 2**：`bpe->token_to_id().at(eos_token)` 里的 `.at(...)` 和 `operator[]` 有何区别？为什么这里用 `.at`？

**参考答案**：对 `std::unordered_map`，`at` 在键不存在时抛 `std::out_of_range`，`operator[]` 则会插入一个默认值。这里用 `.at` 是为了在 eos 字符串不在词表里时**显式报错**而不是悄悄得到一个 0，便于及早发现 `model.json` 里的 eos 写错了。

---

### 4.4 setting 块：读取 attn_cnt / rope / vision

#### 4.4.1 概念说明

`setting` 块放模型超参，是三个块里「最丰富」也最和后续推理流程挂钩的。本模块聚焦三个子项：

- **`attn_cnt`**：注意力（decoder）层数。它决定了 KV cache 的层数、prefill/generate 里循环多少轮去取/填 `cache_k%d`/`cache_v%d`。
- **`rope`**：旋转位置编码（RoPE）配置，包括类型、作用维度、基准频率。
- **`vision`**：视觉子块，决定这是纯文本模型还是 VLM，以及加载哪些视觉网络。

此外还有 `sconv_cnt` / `gdr_cnt`（Qwen3.5 混合架构用，u7 详述）、`functions`（工具调用，u7 详述）等可选项。

> 关于 RoPE 直觉：RoPE 用旋转矩阵给每个位置编码，基准频率 `rope_theta` 控制旋转的「快慢」。直觉上，第 \(i\) 个频率分量为
>
> \[ \theta_i = \text{rope\_theta}^{-2i/d} \]
>
> `rope_theta` 越大，相邻位置的差异越平滑，模型越能「看远」——这也是长上下文模型常把它调到 \(10^6\) 的原因。具体生成 cos/sin cache 的代码留到 [u4（位置编码 RoPE）](u4-l1-rope-basics-and-variants.md)，本讲只关心配置怎么被读进来。

#### 4.4.2 核心流程

整个 `setting` 块的特点是：**几乎每个子项都包在 `contains` 守卫里，所以全是选填、都有默认值**。

1. `attn_cnt` / `sconv_cnt` / `gdr_cnt`：有则覆盖，无则用头文件里的默认（`attn_cnt` 默认 32）。
2. `rope`：解析 `type` 选择四种变体之一（RoPE / LongRoPE / NTKRoPE / YaRNRoPE），读 `rope_head_dim` / `rope_theta`，LongRoPE 还要读两组缩放因子。
3. `vision`：根据 `type`（`close` / `vit` / `qwen3.5_vl`）决定是否加载视觉网络、读 patch 参数。

#### 4.4.3 源码精读

`attn_cnt` / `sconv_cnt` / `gdr_cnt` 三个计数（注意都有 `contains` 守卫）：

[attn_cnt / sconv_cnt / gdr_cnt — src/ncnn_llm_gpt.cpp:106-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L106-L114)

```cpp
if (config["setting"].contains("attn_cnt")) {
    attn_cnt = config["setting"]["attn_cnt"].get<int>();
}
if (config["setting"].contains("sconv_cnt")) {
    sconv_cnt = config["setting"]["sconv_cnt"].get<int>();
}
if (config["setting"].contains("gdr_cnt")) {
    gdr_cnt = config["setting"]["gdr_cnt"].get<int>();
}
```

`rope` 子块——按 `type` 分支选择变体，并读取缩放参数与 `rope_theta`：

[rope 子块（含四种变体分支） — src/ncnn_llm_gpt.cpp:116-146](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L116-L146)

对应到成员变量的默认值（`rope_head_dim` 默认 64、`rope_theta` 默认 100000、`rope_type` 默认 `RoPE`）：

[rope 相关成员及默认值 — src/ncnn_llm_gpt.h:112-120](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L112-L120)

`vision` 子块——这是区分「纯文本」与「VLM」的开关。默认 `vision_type_str = "close"`（不加载任何视觉网络）：

[vision 子块入口 — src/ncnn_llm_gpt.cpp:174-186](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L174-L186)

```cpp
std::string vision_type_str = "close";
if (config["setting"].contains("vision")) {
    auto vision_cfg = config["setting"]["vision"];
    vision_type_str = vision_cfg["type"].get<std::string>();

    if (vision_type_str != "close") {
        if (vision_type_str == "vit")               vision_type = Vision_Type::VISION_VIT;
        else if (vision_type_str == "qwen3.5_vl")   vision_type = Vision_Type::VISION_QWEN3_5_VL;
        // …加载 vision_embed_patch / vision_encoder / 可选 vision_embed_pos 网络
        // …读 patch_size / patch_dim / max_num_patches / spatial_merge_size
        // …从词表找 <|image_pad|> 的 id
    }
}
```

vision 类型枚举（默认 `VISION_CLOSE`，即纯文本）：

[vision_type 枚举 — src/ncnn_llm_gpt.h:134-138](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L134-L138)

完整的 vision 分支较长（加载额外网络、读 patch 参数、找 `image_pad_id`、可选的视觉 mRoPE），见：

[vision 子块完整实现 — src/ncnn_llm_gpt.cpp:174-242](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L174-L242)

> 三种 vision 类型的差别：`close` = 纯文本，不碰图像；`vit` = 经典 ViT 视觉塔；`qwen3.5_vl` = Qwen3.5 风格的视觉塔（还会用 `vision_embed_pos` 与 GDR/ShortConv 等机制，详见 u5、u7）。它们各自加载哪些网络、读哪些字段，会在 [u5-l1（视觉编码器加载与 vision_type 配置）](u5-l1-vision-encoder-load.md) 系统讲解。

#### 4.4.4 代码实践

**实践目标**：用「改默认值 + 源码阅读」体会 `attn_cnt` 与 `rope` 如何影响后续推理。

**操作步骤**（源码阅读型）：

1. 在 `setting` 里故意**不写** `attn_cnt`，追踪构造函数：因为 L106 的 `contains` 不成立，`attn_cnt` 保持头文件默认值 32。
2. 打开 prefill（[src/ncnn_llm_gpt.cpp:296-304](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L296-L304)），那里有 `for (int i = 0; i < attn_cnt; i++)` 循环提取 `out_cache_k%d`/`out_cache_v%d`。
3. 思考：若真实模型有 28 层、而你的 `model.json` 漏写了 `attn_cnt`，会发生什么？

**需要观察的现象**：`attn_cnt` 错误会导致 KV cache 层数与真实网络不匹配。

**预期结果**：能解释「为什么真实模型的 `model.json` 一定要写对 `attn_cnt`」——默认值 32 只是兜底，并不保证匹配你的网络。若层数对不上，prefill 里 `ex.extract("out_cache_k%d")` 会取不到对应输出。

**待本地验证**：行为细节以本地带模型运行结果为准。

#### 4.4.5 小练习与答案

**练习 1**：一个纯文本 Qwen3-0.6B 模型，`setting` 块最少必须写哪些字段才能让构造函数正常通过？

**参考答案**：**一个都不必写**。`attn_cnt`、`rope`、`vision` 全部有 `contains` 守卫和默认值，理论上 `setting` 写成 `{}` 都不会抛异常。但要让模型**推理正确**，你仍需写对 `attn_cnt` 和 `rope`（尤其 `rope_theta`），否则会用错误的默认值跑出乱码——「能构造」和「能正确推理」是两回事。

**练习 2**：`vision.type` 省略时，模型会被当成什么？为什么这样设计是安全的？

**参考答案**：省略时 `vision_type_str` 默认 `"close"`，即纯文本模型，不加载任何视觉网络。这样设计安全是因为「不带视觉」是成本最低、最常见的形态，把它作为默认值可以让纯文本模型的 `model.json` 完全不必提 `vision`。

**练习 3**：`rope.type` 取 `LongRoPE` 时，构造函数还会额外读哪几个字段？

**参考答案**：会额外读 `short_factor`、`long_factor`、`original_max_position_embeddings`（见 L123-125）。这三者是 LongRoPE 长上下文缩放所需的两组频率因子与原始最大位置长度，深入原理见 u4。

---

## 5. 综合实践

**任务**：对照 `ncnn_llm_gpt` 构造函数，为某个**纯文本 LLM** 手写一份最小可用的 `model.json`，并逐字段注明它被构造函数的哪几行代码读取。

**步骤**：

1. 选定一个文本模型（例如 Qwen3-0.6B），假设它的目录下有：`embed.ncnn.param/bin`、`decoder.ncnn.param/bin`、`lm_head.ncnn.param/bin`、`vocab.txt`、`merges.txt`。
2. 按本讲学到的「必填 vs 选填」规则，写出 `model.json`。
3. 在每个字段后面用注释（或单独列表）标注对应的源码行号。

**参考答案**（示例 `model.json`，键名严格对齐构造函数）：

```jsonc
{
  "params": {
    // —— 以下六项均为【必填】，读取于 ncnn_llm_gpt.cpp:58-63（无 contains 守卫）——
    "decoder_param":     "decoder.ncnn.param",   // L58
    "decoder_bin":       "decoder.ncnn.bin",     // L59
    "embed_token_param": "embed.ncnn.param",     // L60  ← 注意不是 README 的 embed_param
    "embed_token_bin":   "embed.ncnn.bin",       // L61
    "proj_out_param":    "lm_head.ncnn.param",   // L62  ← 注意不是 README 的 lm_head_param
    "proj_out_bin":      "lm_head.ncnn.bin"      // L63
  },
  "tokenizer": {
    // —— type 选填（L84-85，缺省 "bpe"）；其余五项【必填】（L87-103，无 contains 守卫）——
    "type": "bbpe",                              // L84-85, L91（开启 byte encoder）
    "vocab_file": "vocab.txt",                   // L87
    "merges_file": "merges.txt",                 // L88
    "additional_special_tokens": [],             // L94（可空数组）
    "eos": "<|endoftext|>",                      // L99-100（空串会得到 -1）
    "bos": ""                                    // L102-103（空串得到 -1，表示不用 bos）
  },
  "setting": {
    // —— 以下均为【选填】，读取于 L106-146（均有 contains 守卫 + 默认值）——
    "attn_cnt": 28,                              // L106-108（默认 32，见 ncnn_llm_gpt.h:109）
    "rope": {                                    // L116
      "type": "RoPE",                            // L126-127（可选 LongRoPE/NTKRoPE/YaRNRoPE）
      "rope_head_dim": 64,                       // L118-120（默认 64）
      "rope_theta": 1000000.0                    // L145（默认 100000，见 ncnn_llm_gpt.h:120）
    }
    // 纯文本模型无需写 "vision"：默认 "close"（L175），不加载任何视觉网络。
    // 也无需写 model_type / hidden_size：gpt 构造函数不读它们。
  }
}
```

**自检清单**：

- [ ] `params` 六个键名是否和 L58-63 **一字不差**？（最容易踩的坑是写成 README 的 `embed_param`/`lm_head_param`）
- [ ] `tokenizer` 里 `additional_special_tokens` / `eos` / `bos` 三个键是否都在？（必填，缺一个就抛异常）
- [ ] `setting.rope.rope_theta` 是否和模型真实值一致？（影响位置编码正确性）
- [ ] 是否误把 `model_type` / `hidden_size` 当成 gpt 必填项？（它们是 OCR/ASR/embedding 运行时用的）

> 说明：本实践是「源码阅读 + 配置编写」型任务，不依赖实际运行模型；若你有真实模型权重，可把写好的 `model.json` 放进目录，用 `xmake run llm_ncnn_run --model <目录>` 验证能否成功加载（能进入对话提示符即说明构造函数跑通）。

## 6. 本讲小结

- `model.json` 是模型目录的「说明书」，顶层分 `params` / `tokenizer` / `setting` 三块，构造函数把它解析进成员变量供后续推理使用。
- `params` 块给出三张子网（embed / decoder / proj_out）的文件名，六个字段全部**必填**；真实键名是 `embed_token_*` / `proj_out_*`，**README 示例里的 `embed_*` / `lm_head_*` 已过时**，以源码为准。
- `tokenizer` 块里 `vocab_file` / `merges_file` / `additional_special_tokens` / `eos` / `bos` 是**必填键**（值可空），`type` 选填，`"bbpe"` 会开启字节级编码。
- `setting` 块几乎全是**选填**：`attn_cnt` / `sconv_cnt` / `gdr_cnt` / `rope` / `vision` 都有 `contains` 守卫和默认值。
- 判定「必填 vs 选填」的方法：看代码里是否用 `config[...].contains(...)` 包了一层——有就是选填，没有就是必填。
- `model_type` 和 `hidden_size` 不被 gpt 构造函数读取（它们属于 OCR/ASR/embedding 运行时），别被 README 示例误导。

## 7. 下一步学习建议

本讲把「配置 → 成员变量」这条链路讲透了，接下来应该看「成员变量 → 推理流程」：

- 先读 [u2-l1（基类 ncnn_llm_base 与公共能力）](u2-l1-base-class-common.md)，理解 `KVCache`、`create_option`、`sample_logits` 这些被构造函数间接依赖的公共设施。
- 再读 [u2-l3（prefill 文本预填充流程）](u2-l3-prefill-flow.md)，看 `attn_cnt` 如何驱动 KV cache 循环、`rope` 配置如何变成 cos/sin cache——你会真切感受到本讲这些字段「为什么重要」。
- 对 RoPE 四种变体好奇的，直接跳 [u4-l1（RoPE 基础与长上下文变体）](u4-l1-rope-basics-and-variants.md)。
- 想了解 `vision` 子块三种类型细节的，去 [u5-l1（视觉编码器加载与 vision_type 配置）](u5-l1-vision-encoder-load.md)。

一句话：本讲之后，你已经能「读得懂、写得出」一个文本模型的 `model.json`，并知道它每一行被源码的哪里吃掉。
