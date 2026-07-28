# 分词器抽象与特殊令牌

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `tokenizer_types.h` 里 `SpecialTokensConfig` 与 `SpecialTokenIds` 两个数据结构的字段与用途；
- 读懂 `ncnn_llm_gpt` 构造函数里加载分词器的整段代码，知道 `tokenizer.type` 字段到底控制了什么；
- 解释 `eos` / `bos` 是如何从配置里的字符串被解析成整数 id 的，以及空串保护的作用；
- 说清楚 `<|image_pad|>` 这类 `additional_special_tokens` 为什么要单独加载、它在编码时和普通文本有什么不同。

本讲承接 u2-l3：你已经知道 `prefill` 第一步是 `bpe->encode(input_text)`，本讲就往上走一层，回答「这个 `bpe` 对象本身是怎么从 `model.json` 构造出来的」。

## 2. 前置知识

- **token 与 id**：模型不直接处理文字，而是处理整数 id。分词器（tokenizer）负责在「字符串」和「`vector<int>` id 序列」之间来回转换。正向叫 encode（编码），反向叫 decode（解码）。
- **词表（vocab）**：一张「字符串 ↔ id」的映射表。`id_to_token` 是「按下标取字符串」的数组，`token_to_id` 是「按字符串取 id」的哈希表。id 一般就是该字符串在词表文件里的行号。
- **特殊令牌（special token）**：一类有「控制含义」而非「文本含义」的 token，比如 `<eos>`（结束）、`<bos>`（开头）、`<|image_pad|>`（图像占位）。它们在编码时通常要被**当成不可拆分的整体**，不能用普通分词规则切碎。
- **BPE 与 bbpe**：BPE（Byte-Pair Encoding）按「子词合并规则」切分文本；bbpe（byte-level BPE）先把文本映射到「字节→可见字符」再合并，是 GPT 系列的常见做法。本讲的 `BpeTokenizer` 同时支持这两种模式，靠一个布尔开关切换。
- **`std::optional`**：C++17 引入的「可能没有值」的类型。`has_value()` 判断是否有值，`*` 取值。`tokenizer_types.h` 用它来表示「这个特殊令牌配置里没写」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/tokenizer/tokenizer_types.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h) | 定义特殊令牌的「配置（字符串）」与「id（整数）」两个数据结构，全项目分词器共用。 |
| [src/utils/tokenizer/bpe_tokenizer.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h) | `BpeTokenizer` 类声明：加载、编码、解码接口，以及特殊令牌相关方法。 |
| [src/utils/tokenizer/bpe_tokenizer.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp) | `BpeTokenizer` 实现，含本讲要精读的 `EnsureSpecialTokens` / `AddAdditionalSpecialToken` / `encode` 中的特殊令牌匹配。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | LLM 主运行时的构造函数，本讲聚焦其中「Load tokenizer」一段。 |
| [src/ncnn_embedding.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp) | 嵌入运行时构造函数，作为对比：它才是真正按 `type=="unigram"` 选择 `UnigramTokenizer` 的地方。 |

## 4. 核心概念与源码讲解

### 4.1 特殊令牌的数据结构：SpecialTokensConfig / SpecialTokenIds

#### 4.1.1 概念说明

一个模型会用到的特殊令牌，种类基本固定：开头（bos）、结尾（eos）、未知词（unk）、分隔（sep）、填充（pad）、分类（cls）、掩码（mask）。ncnn_llm 把它们抽象成「成对的两个结构」：

- **`SpecialTokensConfig`**：描述「配置里写的字符串」。每个字段是 `std::optional<std::string>`，写没写都能表达——没写就是 `nullopt`。它对应 `model.json` 里 `tokenizer` 块的那些键。
- **`SpecialTokenIds`**：描述「解析出来的整数 id」。每个字段是 `int`，默认 `-1` 表示「不存在」。

这样就把「人类可读的配置」和「模型要用的整数」分离开：加载流程读 config，再把它解析成 ids 塞进分词器。

#### 4.1.2 核心流程

```
model.json 里的字符串          C++ 内存里的整数
"bos": "<|im_start|>"   ──►   SpecialTokensConfig.bos_token  ──► SpecialTokenIds.bos_id = 151644
"eos": "<|im_end|>"     ──►   SpecialTokensConfig.eos_token  ──► SpecialTokenIds.eos_id = 151645
（没写 unk）             ──►   SpecialTokensConfig.unk_token = nullopt ──► unk_id = -1
```

转换发生在分词器加载函数里（见 4.4.3），核心是「拿字符串去 `token_to_id` 表里查 id」。

#### 4.1.3 源码精读

两个结构都极简，字段一一对应：

[tokenizer_types.h:5-13](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h#L5-L13) 定义了 `SpecialTokensConfig`，7 个 `std::optional<std::string>`，每个对应一种特殊令牌的「字符串配置」。

[tokenizer_types.h:15-23](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h#L15-L23) 定义了 `SpecialTokenIds`，7 个 `int` 全部默认 `-1`，表示「尚未解析 / 不存在」。

注意它只有「数据」，没有任何方法——这是一个纯数据载体（POD），具体的「字符串→id」解析逻辑放在分词器类里（4.4.3），这样两种分词器（BPE / Unigram）可以共用同一份配置定义。

#### 4.1.4 代码实践

**实践目标**：建立「配置字符串 → 整数 id」的直觉。

**操作步骤**：

1. 打开 [tokenizer_types.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h)，数清楚 `SpecialTokensConfig` 与 `SpecialTokenIds` 各有几个字段。
2. 自行画一张对照表，左边写 `SpecialTokensConfig` 的字段名，右边写 `SpecialTokenIds` 的对应字段名，确认它们一一对应。

**预期结果**：两边都是 7 个字段（bos/eos/unk/sep/pad/cls/mask），只是类型从 `optional<string>` 变成了 `int`（默认 -1）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SpecialTokensConfig` 用 `std::optional<string>`，而 `SpecialTokenIds` 用普通 `int`（默认 -1）？

**答案**：配置层要区分「用户写了空值」和「用户根本没写这个键」——`optional` 的 `nullopt` 表达「没写」。而到了 id 层，只需要一个统一的「不存在」标记，用 `-1` 即可，不必再保留「没写」这层语义。

**练习 2**：`SpecialTokenIds` 里 `eos_id` 的默认值是什么？它代表什么含义？

**答案**：默认 `-1`，代表「该模型没有 eos 令牌」或「尚未解析」。后续代码（如 generate 的停止判断）会用 `>= 0` 来判断 eos 是否有效。

---

### 4.2 tokenizer.type 分支：bpe 与 bbpe

#### 4.2.1 概念说明

`model.json` 的 `tokenizer` 块里有个选填字段 `type`。你需要先纠正一个常见误解：**在 LLM 主运行时 `ncnn_llm_gpt` 里，`type` 并不在「BPE」和「Unigram」两种分词器之间选择，它只在「普通 BPE」和「字节级 BPE（bbpe）」之间切换**——两者都由同一个 `BpeTokenizer` 类实现，差别只是一个布尔开关 `use_byte_encoder`。

真正的「按 `type=="unigram"` 选择 `UnigramTokenizer`」的逻辑，在**嵌入运行时** `ncnn_embedding` 里（见 4.2.3 末尾的对比）。这是本项目一个容易踩坑的点：不同运行时对同一个 `type` 字段的解释范围不同。

#### 4.2.2 核心流程

`ncnn_llm_gpt` 构造函数加载分词器的步骤：

```
1. 读 type（默认 "bpe"，config 里有就用 config 的）
2. 拼 vocab_file / merges_file 路径
3. LoadFromFiles(...) 构造 BpeTokenizer
        └─ use_byte_encoder = (type == "bbpe")   ← type 唯一作用点
4. 逐个 AddAdditionalSpecialToken(...)
5. 解析 eos / bos（见 4.3）
```

`type` 的全部影响，就是第 3 步里传给 `LoadFromFiles` 的最后一个参数 `type == "bbpe"`。

#### 4.2.3 源码精读

`type` 默认值与读取，[ncnn_llm_gpt.cpp:83-86](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L83-L86)：先用 `"bpe"` 兜底，只有 `config["tokenizer"]` 里 `contains("type")` 时才覆盖。这是项目里典型的「选填字段」读法（u1-l5 已建立这个模式）。

真正的构造调用，[ncnn_llm_gpt.cpp:90-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L90-L92)：

```cpp
bpe = std::make_shared<BpeTokenizer>(BpeTokenizer::LoadFromFiles(
    vocab_file, merges_file, SpecialTokensConfig{}, false, true, type == "bbpe"
));
```

注意三个细节：

1. 第三个参数传了 `SpecialTokensConfig{}`——一个**全空的配置**。也就是说，gpt 运行时**不走** `EnsureSpecialTokens` 这条路来设置 bos/eos（因为第 4 个参数 `add_special_if_missing=false` 配合空配置，等于啥也不干）。它的 bos/eos 是随后单独手工解析的（4.3）。
2. 倒数第二个参数 `type == "bbpe"`，是 `type` 字段唯一发挥作用的地方：为 `true` 时 `BpeTokenizer` 启用字节级编码（`InitByteMaps`），编码风格变成 GPT 那种。
3. `LoadFromFiles` 是个静态工厂方法，返回的是值对象，再被 `make_shared` 包成共享指针存到成员 `bpe` 里（[ncnn_llm_gpt.h:99](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L99)）。

**对比：嵌入运行时才有的 unigram 分支**。[ncnn_embedding.cpp:83-123](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L83-L123) 里，`tokenizer_type == "unigram"` 时会构造 `UnigramTokenizer`（[ncnn_embedding.cpp:89-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L89-L114)），`else` 分支才走 `BpeTokenizer`（且同样用 `tokenizer_type == "bbpe"` 切字节级，[ncnn_embedding.cpp:115-123](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L115-L123)）。所以「type 选 Unigram」只发生在嵌入运行时，gpt 运行时只认 bpe/bbpe。

> 提示：规格里「按 type 选择 BPE/Unigram」的说法，对整个项目而言是对的，但具体到 `ncnn_llm_gpt.cpp` 这一个文件，type 只切 bpe/bbpe。读源码要落到具体运行时。

#### 4.2.4 代码实践

**实践目标**：确认 `type` 在 gpt 构造函数里的唯一作用点。

**操作步骤**：

1. 在 [ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) 里全文搜索 `type`，观察它在「Load tokenizer」段落里只出现两次：读出来（L83-86）和传给 `LoadFromFiles`（L91）。
2. 打开 [ncnn_embedding.cpp:89](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L89)，对比 `if (tokenizer_type == "unigram")` 分支，确认 Unigram 只在这里被选中。

**预期结果**：gpt 里 `type` 只有 `bpe` / `bbpe` 两个有效取值，控制字节级编码开关；embedding 里多一个 `unigram` 取值。

#### 4.2.5 小练习与答案

**练习 1**：某份 `model.json` 把 `tokenizer.type` 写成 `"unigram"`，但模型是用 `ncnn_llm_gpt`（即 `llm_ncnn_run`）加载的，会发生什么？

**答案**：在 gpt 构造函数里，`type == "bbpe"` 为 `false`，于是按普通 BPE 加载（`use_byte_encoder=false`）。`"unigram"` 这个值不会被识别，等于退化成默认的 `"bpe"`。结果是用 BPE 规则去切本该用 Unigram 切的模型，分词结果会错乱。要让 Unigram 生效，得用嵌入运行时（如 `embedding_main`）。

**练习 2**：`type == "bbpe"` 时，`BpeTokenizer` 内部会额外做哪一步初始化？

**答案**：调用 `InitByteMaps()` 建立「字节 ↔ 映射字符」的双向表（见 `LoadFromFiles` 里 `if (tok.use_byte_encoder_) tok.InitByteMaps();`，[bpe_tokenizer.cpp:355-357](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L355-L357)）。

---

### 4.3 eos / bos 的解析：字符串如何变成 id

#### 4.3.1 概念说明

`model.json` 里 eos/bos 写的是字符串（如 `"<|im_end|>"`），但 generate 循环里判断停止用的是整数（`ctx->cur_token == eos`）。所以构造函数必须把字符串解析成 id，存到成员 `eos` / `bos` 里。gpt 运行时采用的是「手工查询」的方式：直接拿字符串去 `token_to_id` 表里查。

#### 4.3.2 核心流程

```
读 config["tokenizer"]["eos"] → 得到字符串 eos_token
if (eos_token != "")          → 空串保护
    eos = bpe->token_to_id().at(eos_token)   ← 用 .at() 查，查不到就抛异常
else
    eos = -1
```

bos 同理。注意两个关键设计：

- **空串保护**：配置里 eos 可以写成 `""`（表示「这个模型没有 eos」），代码遇到空串就把 id 设为 `-1`，不报错。
- **用 `.at()` 而非 `.find()`**：`.at()` 在 key 不存在时会抛 `out_of_range` 异常。这是「尽早失败」——如果你声明了 eos 却拼错字符串、词表里查不到，构造阶段就直接报错，而不是带着错误的 `eos=-1` 跑到生成阶段才出诡异 bug。抛出的异常会被构造函数末尾的 `catch` 统一转成 `load model failed`（[ncnn_llm_gpt.cpp:243-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L243-L245)）。

#### 4.3.3 源码精读

[ncnn_llm_gpt.cpp:99-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L99-L103) 是 eos/bos 解析的全部代码：

```cpp
auto eos_token = config["tokenizer"]["eos"].get<std::string>();
eos = (eos_token != "") ? bpe->token_to_id().at(eos_token) : -1;

auto bos_token = config["tokenizer"]["bos"].get<std::string>();
bos = (bos_token != "") ? bpe->token_to_id().at(bos_token) : -1;
```

`eos` / `bos` 是 `ncnn_llm_gpt` 的成员（[ncnn_llm_gpt.h:103-104](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L103-L104)，默认值 `0`）。解析后，`bos` 会在 `prefill` 开头被插到 token 序列最前面（[ncnn_llm_gpt.cpp:249-250](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L249-L250)），`eos` 会在 generate 循环里作为停止条件（[ncnn_llm_gpt.cpp:875](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L875)）。

**对比 `.at()` 与 `.find()`**：同样是查 `token_to_id`，eos/bos 用 `.at()`（必须存在，否则报错），而下面要讲的 `<|image_pad|>`、`<think>` 用 `.find()`（不存在就静默得 -1，不报错）。区别在于「这个令牌是不是必需」。

#### 4.3.4 代码实践

**实践目标**：体会 `.at()` 的「尽早失败」与空串保护。

**操作步骤**：

1. 阅读上面的 [ncnn_llm_gpt.cpp:99-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L99-L103) 两段。
2. 假设你故意把 `model.json` 里 `eos` 改成一个词表里没有的字符串（如 `"__not_a_token__"`），在脑中走一遍流程：`.at()` 抛异常 → 被第 243 行 `catch` 捕获 → 抛出 `ncnn_llm_gpt load model failed: ...`。
3. 再假设把 `eos` 改成 `""`，走一遍：条件不成立 → `eos = -1`，构造成功，只是这个模型「没有 eos」。

**预期结果**：理解「拼错字符串 → 构造期就炸」是有意为之的安全设计，避免错误带入推理阶段。**待本地验证**：可在本地用一份小模型目录复现这两种配置，观察报错信息。

#### 4.3.5 小练习与答案

**练习 1**：为什么 eos 用 `.at()`、而 `<|image_pad|>` 用 `.find()`？

**答案**：eos 是文本模型的**必需**令牌（没有它 generate 无法判断何时停止），所以拼错要立即报错；`<|image_pad|>` 只在视觉模型里才需要，纯文本模型词表里没有它是正常的，所以用 `.find()` 找不到就给 `-1` 静默跳过。

**练习 2**：`eos_token == ""` 时 `eos` 被设为 `-1`，那 generate 循环里 `if (ctx->cur_token == eos) break;` 还能正常停止吗？

**答案**：`eos == -1` 时，除非模型偶然输出 id 为 -1 的 token（实际不会，合法 id 都 ≥ 0），否则这个停止条件永远不触发，模型会一直生成到 `max_new_tokens` 上限。这就是「没有 eos 的模型靠步数兜底」。

---

### 4.4 additional_special_tokens 的解析与「单独加载」的原因

#### 4.4.1 概念说明

除了固定的 7 个特殊令牌，现代模型还会自定义一批「额外特殊令牌」，典型的就是 `<|image_pad|>`（图像占位）、`<|endoftext|>`、各种 `<|tool_call|>` 等。它们的共同点是：**在编码时必须被当成不可拆分的整体**。

为什么不能让普通 BPE 去切？以 `<|image_pad|>` 为例：

- 普通 BPE 看到 `<|image_pad|>`，可能把它切成 `<`、`|`、`image`、`_pad`、`|`、`>` 一串碎片；
- 但 VLM 在 prefill 时要把这块占位**整段替换**成图像嵌入（见 u5-l3 的 `inject_image_embeds`），它必须知道「这 N 个连续的 `<|image_pad|>` token 各自的 id 是多少」，才能定位占位区间。
- 所以 `<|image_pad|>` 必须作为一个**原子的、有固定 id 的整体**存在于词表和编码结果里。

这就是「单独加载」的核心原因：把额外特殊令牌注册进 `additional_special_tokens_` 列表，`encode` 时会**优先**用最长子串匹配把它们整体识别出来，绕过 BPE。

#### 4.4.2 核心流程

```
配置 additional_special_tokens: ["<|image_pad|>", "<|endoftext|>", ...]
        │
        │  逐个调用 AddAdditionalSpecialToken(token)
        ▼
内部：在 token_to_id 里查 token
        ├─ 找到      → 用已有 id
        └─ 没找到    → 追加到词表末尾，分配新 id（add_if_missing）
登记到 additional_special_tokens_ / _ids_ / _to_id_ / _id_set_

编码 encode(text) 时：
        逐位置扫描 → 用最长子串匹配 additional_special_tokens_
        ├─ 命中 → flush 之前的普通文本（走 BPE），再直接输出该 special id
        └─ 未命中 → 累积到 buffer，留给 BPE 处理
```

#### 4.4.3 源码精读

构造函数读取并注册额外特殊令牌，[ncnn_llm_gpt.cpp:94-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L94-L97)：

```cpp
std::vector<std::string> additional_special_tokens =
    config["tokenizer"]["additional_special_tokens"].get<std::vector<std::string>>();
for (const auto& token : additional_special_tokens) {
    bpe->AddAdditionalSpecialToken(token);
}
```

`AddAdditionalSpecialToken` 的实现，[bpe_tokenizer.cpp:390-417](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L390-L417)：

```cpp
int id = -1;
auto it = token_to_id_.find(token);
if (it != token_to_id_.end()) {
    id = it->second;                 // 词表里已有，复用 id
} else if (add_if_missing) {
    id = static_cast<int>(id_to_token_.size());
    id_to_token_.push_back(token);   // 没有就追加到词表末尾
    token_to_id_.emplace(token, id);
} else {
    return;                          // 不允许新增就忽略
}
additional_special_tokens_.push_back(token);
additional_special_token_ids_.push_back(id);
additional_special_token_to_id_.emplace(token, id);
additional_special_id_set_.insert(id);
```

注意构造函数调用时没传第二个参数，默认 `add_if_missing=true`（[bpe_tokenizer.h:37-38](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h#L37-L38)），所以 `<|image_pad|>` 即使不在词表里也会被追加进去。

**为什么单独加载？看 `encode` 怎么用它们**。[bpe_tokenizer.cpp:471-493](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L471-L493)：

```cpp
while (i < n) {
    int matched_index = -1;
    size_t matched_len = 0;
    if (!additional_special_tokens_.empty()) {
        for (size_t k = 0; k < additional_special_tokens_.size(); ++k) {
            // longest match：多个 special token 共享前缀时选最长的
            ...
            if (i + len <= n && text.compare(i, len, sp) == 0) {
                matched_index = static_cast<int>(k);
                matched_len = len;
            }
        }
    }
    if (matched_index >= 0) {
        flush_buffer();                              // 先把累积的普通文本走 BPE
        ids.push_back(additional_special_token_ids_[matched_index]); // 再整体输出 special id
        i += matched_len;
        continue;
    }
    buffer.push_back(text[i]);                       // 否则累积普通文本
    ++i;
}
```

这段是「单独加载」的全部意义所在：额外特殊令牌在编码时被**优先整体匹配**，不会被 BPE 切碎。`flush_buffer` 把前面累积的普通文本交给 BPE，保证「普通文本走 BPE、特殊令牌走原子替换」两条路径互不干扰。

**解码时也靠这份登记**。[bpe_tokenizer.cpp:514-519](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L514-L519) 在 `decode` 里判断一个 id 是否是特殊令牌时，除了查那 7 个固定 id，还会查 `additional_special_id_set_`，从而在 `skip_special_tokens=true` 时把它们一起跳过。

**顺带一提**：视觉模型里 `image_pad_id` 的单独查找，用的是 `.find()`（[ncnn_llm_gpt.cpp:223-226](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L223-L226)），找到就用、找不到保持 `-1`。它和 `AddAdditionalSpecialToken` 是配合关系：后者保证 `<|image_pad|>` 有 id 且编码时整体输出，前者把这个 id 取出来供后续 `inject_image_embeds` 定位占位。

#### 4.4.4 代码实践

**实践目标**：用 `SpecialTokensConfig` 描述一组特殊令牌并亲手把字符串解析成 id，体会「额外特殊令牌为何要单独加载」。

**操作步骤（最小调用示例，示例代码）**：

下面是一段「示例代码」（非项目原有文件），演示如何参照构造函数的写法加载分词器、注册额外特殊令牌、解析 bos/eos/unk 并打印：

```cpp
// 示例代码：演示 SpecialTokensConfig → SpecialTokenIds 的解析过程
#include "utils/tokenizer/bpe_tokenizer.h"
#include "utils/tokenizer/tokenizer_types.h"
#include <iostream>

int main() {
    // 1) 描述一组特殊令牌（字符串配置）
    SpecialTokensConfig spec;
    spec.bos_token = "<|im_start|>";
    spec.eos_token = "<|im_end|>";
    spec.unk_token = "<|unk|>";

    // 2) 加载分词器（需要真实的 vocab/merges 文件，待本地准备）
    BpeTokenizer tok = BpeTokenizer::LoadFromFiles(
        "vocab.txt", "merges.txt", spec, /*add_special_if_missing=*/true);

    const SpecialTokenIds& ids = tok.special_ids();
    std::cout << "bos_id=" << ids.bos_id << "\n";
    std::cout << "eos_id=" << ids.eos_id << "\n";
    std::cout << "unk_id=" << ids.unk_id << "\n";

    // 3) 单独注册额外特殊令牌（参照 ncnn_llm_gpt.cpp:94-97）
    tok.AddAdditionalSpecialToken("<|image_pad|>");
    for (int id : tok.additional_special_token_ids()) {
        std::cout << "additional special id: " << id << "\n";
    }

    // 4) 验证它被整体编码（不会被 BPE 切碎）
    auto encoded = tok.encode("hello <|image_pad|> world");
    for (int id : encoded) std::cout << id << " ";
    std::cout << "\n";
    return 0;
}
```

**需要观察的现象**：

1. `bos_id` / `eos_id` / `unk_id` 是否被正确解析成词表里的行号；
2. `<|image_pad|>` 出现在 `additional_special_token_ids()` 里，且它的 id 与编码结果中对应位置的 id 一致；
3. 编码 `"hello <|image_pad|> world"` 时，`<|image_pad|>` 只占 **1 个** id（整体匹配），而不是被切成多个碎片。

**预期结果**：额外特殊令牌作为一个原子 id 出现在编码序列里；这正是它能被 `inject_image_embeds` 整段替换的前提。

> 说明：本示例需要一份真实的 `vocab.txt` / `merges.txt`（可从任意已导出的模型目录取，如 `assets/<某模型>/`）。若本地暂无模型资产，**待本地验证**；可先做下面的「源码阅读型实践」作为替代。

**源码阅读型实践（无需模型资产）**：

1. 在 [bpe_tokenizer.cpp:471-493](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L471-L493) 的循环里，找到「命中 special token 时先 `flush_buffer()` 再 push special id」这两行，用自己的话解释：如果改成「不 flush 直接 push」会出什么问题。
2. 回答本讲标题里的问题——**`<|image_pad|>` 为什么要单独加载？** 把答案写成一句话：因为它在编码时必须被整体匹配、保持原子 id，才能在 VLM 的 prefill 里被整段替换为图像嵌入。

#### 4.4.5 小练习与答案

**练习 1**：`AddAdditionalSpecialToken` 调用时 `add_if_missing` 默认为 `true`。如果一个额外特殊令牌字符串不在词表里，会发生什么？

**答案**：它会被追加到 `id_to_token_` 末尾，分配一个等于当前词表大小的新 id，并登记到 `token_to_id_` 和几个 `additional_special_*` 容器里（[bpe_tokenizer.cpp:404-407](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L404-L407)）。这样即使词表文件没收录，编码时也能正确识别它。

**练习 2**：编码时为什么要做「longest match（最长匹配）」？

**答案**：因为额外特殊令牌可能共享前缀，比如同时存在 `<|image_pad|>` 和 `<|image|>`。从同一位置开始匹配时，若短的被选中，长的就会被截断后误走 BPE。选择最长命中能保证语义最具体的那个令牌被整体识别（[bpe_tokenizer.cpp:475-485](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L475-L485)）。

**练习 3**：`decode` 时如何判断一个 id 是不是「额外特殊令牌」从而决定是否跳过？

**答案**：查 `additional_special_id_set_` 这个集合（[bpe_tokenizer.cpp:519](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L519)）。它在 `AddAdditionalSpecialToken` 时被填充，用集合是为了 O(1) 查询。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从 `model.json` 到分词器对象」的纸上推演：

1. **准备一份 `tokenizer` 配置块**（可参考 u1-l5 的字段说明）。至少包含：`type`（写 `"bbpe"`）、`vocab_file`、`merges_file`、`eos`（如 `"<|im_end|>"`）、`bos`（如 `"<|im_start|>"`）、`additional_special_tokens`（至少含 `"<|image_pad|>"`）。把 `eos` 故意留成空串 `""` 做对照。

2. **对照 `ncnn_llm_gpt.cpp:83-103`**，逐行写下这段配置会被怎样处理：
   - `type=="bbpe"` 让 `LoadFromFiles` 启用了什么？（字节级编码）
   - `eos==""` 时 `eos` 成员的最终值？（-1）
   - `bos=="<|im_start|>"` 是通过哪个函数调用变成 id 的？（`bpe->token_to_id().at(...)`）
   - `"<|image_pad|>"` 走的是哪条注册路径？（`AddAdditionalSpecialToken`）

3. **画出 `encode("<|im_start|>描述<|image_pad|>图<|im_end|>")` 的 id 序列结构**，标注：
   - 哪几个 id 来自 `additional_special_tokens` 的整体匹配；
   - 哪几段走的是普通 BPE。

4. **回答贯穿性问题**：为什么 `<|im_start|>` / `<|im_end|>` 在 gpt 里走「`token_to_id().at()` 手工查询」、而 `<|image_pad|>` 走「`AddAdditionalSpecialToken` 注册」？提示：前者只在序列首尾出现、由构造函数直接插值/判断停止；后者要在文本中段被**任意次数**地整体识别，必须进入 `encode` 的最长匹配逻辑。

> 本综合实践为源码阅读型，无需运行；如本地有已导出模型，可把第 2 步的推演用 u3-l2 的 `BpeTokenizer::LoadFromFiles` 实际跑一遍验证。

## 6. 本讲小结

- `tokenizer_types.h` 用一对纯数据结构 `SpecialTokensConfig`（7 个 `optional<string>`）和 `SpecialTokenIds`（7 个 `int` 默认 -1）分离「配置字符串」与「解析后的 id」，被 BPE / Unigram 两种分词器共用。
- 在 LLM 主运行时 `ncnn_llm_gpt` 里，`tokenizer.type` **只在 `bpe` 与 `bbpe` 之间切换**（都由 `BpeTokenizer` 实现，靠 `use_byte_encoder` 开关）；真正的 `unigram` 分支只在嵌入运行时 `ncnn_embedding` 里。
- `eos` / `bos` 用 `bpe->token_to_id().at(...)` 把字符串解析成 id，空串保护得到 -1，拼错字符串则由 `.at()` 抛异常、被构造函数 `catch` 转成 `load model failed`——这是有意为之的尽早失败。
- `additional_special_tokens`（如 `<|image_pad|>`）必须「单独加载」：注册后 `encode` 会用最长子串匹配把它们**整体识别**为单个原子 id，绕过 BPE 切分，这是 VLM 后续 `inject_image_embeds` 整段替换占位的前提。
- 同样是查 `token_to_id`，必需令牌（eos/bos）用 `.at()` 报错、可选令牌（`<|image_pad|>`/`<think>`）用 `.find()` 静默得 -1，这是「必需 vs 可选」的区分设计。

## 7. 下一步学习建议

- **u3-l2 BPE / BBPE 分词器实现**：本讲只到「分词器对象怎么构造」，下一讲深入 `BpeTokenizer` 内部——`vocab`/`merges` 加载、`BpeForPiece` 合并算法、字节级 `byte encoder` 与 `encode`/`decode` 往返。
- **u3-l3 Unigram 分词器实现**：如果你想搞懂「嵌入运行时那条 `unigram` 分支」的内部，看 `UnigramTokenizer` 的 Trie + Viterbi 分段。
- **u3-l4 采样与解码策略**：分词器解决「文本 ↔ id」，采样解决「logits → 下一个 id」，两者共同构成 generate 循环的两端。
- **u5-l3 视觉特征提取与图像嵌入注入**：去看 `<|image_pad|>` 的 id 最终如何被用来定位并替换成图像嵌入，闭环本讲留下的「为什么要单独加载」的伏笔。
