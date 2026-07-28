# 视觉编码器加载与 vision_type 配置

## 1. 本讲目标

本讲是「视觉语言模型（VLM）」单元的第一讲，回答一个聚焦的问题：

> 当一个模型支持「看图」，构造函数 `ncnn_llm_gpt(...)` 在加载模型时，比纯文本模型**多做**了哪些事？

学完后你应当能够：

- 说清 `vision_type` 三种取值（`VISION_CLOSE` / `VISION_VIT` / `VISION_QWEN3_5_VL`）各自代表什么、构造时加载哪些 ncnn 子网。
- 把 `model.json` 里 `setting.vision` 的字段（网络文件名、`patch_size` / `patch_dim` / `max_num_patches` / `spatial_merge_size`、`rope.mrope_section`）与构造函数里的读取代码一一对应。
- 理解 `<|image_pad|>` 这个特殊令牌为什么必须单独解析成 `image_pad_id`，以及它在后续「图像嵌入注入」中扮演的占位角色。
- 看懂构造函数里「必填用 `get` 直接读、选填用 `contains` 包一层」的配置约定。

本讲**只讲「配置 → 成员变量 + 网络」这一步**：图像预处理、视觉特征提取、图像嵌入如何拼进文本序列，分别留给 u5-l2 / u5-l3 / u5-l4。

## 2. 前置知识

阅读本讲前，建议你已经掌握（对应前置讲义）：

- **u1-l5**：`model.json` 顶层 `params` / `tokenizer` / `setting` 三块结构，以及「必填用 `contains` 守卫判定」的约定。
- **u2-l1**：基类 `ncnn_llm_base` 的 `load_net()`、`create_option()`、`Net` 的加载与 Vulkan 选项。
- **u2-l3**：`prefill` 的整体流程（分词 → RoPE → embed → 因果 mask → decoder → proj_out），知道 prefill 返回一个携带 KV cache 的 `ctx`。
- **u3-l1**：特殊令牌（`eos` / `bos` / `additional_special_tokens`）如何从字符串解析成整数 id，以及「必需用 `.at()` 报错、可选用 `.find()` 静默得 -1」的设计。

几个本讲会反复用到的术语：

- **视觉编码器（vision encoder）**：把一张图变成一串「图像嵌入向量」的网络，通常是 ViT（Vision Transformer）或它的变体。这串向量之后会被拼接到文本 token 的嵌入序列里，让语言模型「看见」图像。
- **patch（图像块）**：把一张图切成大小相同的小方块，每块当作一个「图像 token」。`patch_size` 是块边长（像素）。
- **占位令牌（image_pad）**：`<|image_pad|>` 是一个特殊的文本 token，在 prompt 里占一串连续位置；运行时这段位置会被真实图像嵌入整段替换。它的 token id 就是 `image_pad_id`。
- **VLM（Vision-Language Model）**：能同时处理文本和图像的语言模型，如 Qwen2.5-VL、Qwen3.5-VL。

> 提示：本仓库 `assets/` 里**没有**自带模型权重与 `model.json`（只有 `mclip_unigram_tokenizer.txt`），权重需自行下载。因此本讲多处实践是「源码阅读型」，需要你对照源码推理，而不是真跑模型。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `ncnn_llm_gpt` 类声明：视觉网络成员指针、视觉配置成员、`Vision_Type` 枚举。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 构造函数的 `// Vision settings` 分支：解析 `setting.vision`、加载视觉子网、读取 patch 配置、解析 `image_pad_id`。 |

下游会用到这些已加载网络/配置的函数（本讲只点名，不展开，留给后续讲义）：

- `get_visiual_features()`（运行视觉编码器，产出 `image_embeds`）
- `bgr_to_pixel_values()` / `get_image_size_for_patches()`（图像预处理，受 `vision_type` / `patch_size` / `spatial_merge_size` 影响）
- `prefill(text, bgr, ctx)`（VLM 多轮图文 prefill，受 `vision_type` 影响 mRoPE 选择）

## 4. 核心概念与源码讲解

### 4.1 vision_type 枚举：三类模型的开关

#### 4.1.1 概念说明

`ncnn_llm_gpt` 是一个「既能跑纯文本 LLM、又能跑 VLM」的统一类。一个具体模型属于哪一类，由 `model.json` 里 `setting.vision.type` 这个字符串决定。构造函数把这个字符串翻译成一个 C++ 枚举 `vision_type`，后续所有视觉相关代码都用这个枚举做分支。

枚举定义在头文件里，共三个值：

| 枚举值 | 对应 `setting.vision.type` 字符串 | 含义 |
| --- | --- | --- |
| `VISION_CLOSE` | `"close"`（或 `setting` 里没有 `vision` 块） | 纯文本模型，**不加载任何视觉网络**。 |
| `VISION_VIT` | `"vit"` | 标准 ViT 风格 VLM（如 Qwen2.5-VL）。 |
| `VISION_QWEN3_5_VL` | `"qwen3.5_vl"` | Qwen3.5-VL 风格 VLM，在图像预处理与位置编码上有特殊处理。 |

注意一个**反直觉但重要**的事实：`VISION_VIT` 和 `VISION_QWEN3_5_VL` 在构造阶段加载的 ncnn 网络**完全一样**（同样的网络名、同样的 `model.json` 字段、同样的加载顺序）。两者的区别**不在「加载什么网络」，而在 `vision_type` 这个枚举值本身**——它会在下游 `bgr_to_pixel_values()`（双帧时序堆叠）和 `prefill()`（交错 mRoPE 选择）里触发不同的分支。

#### 4.1.2 核心流程

构造函数对 vision 的判定流程：

```text
默认 vision_type = VISION_CLOSE，vision_type_str = "close"
        │
        ▼
setting 里有没有 "vision" 块？  ── 没有 ──▶ 保持 VISION_CLOSE，跳过整个视觉加载
        │ 有
        ▼
读 vision.type 字符串
        │
        ├── "vit"        ──▶ vision_type = VISION_VIT
        ├── "qwen3.5_vl" ──▶ vision_type = VISION_QWEN3_5_VL
        └── 其它         ──▶ vision_type 保持 VISION_CLOSE（但会进入加载分支，见 4.2）
        │
        ▼
若 vision_type_str != "close"：加载视觉网络 + 读 patch 配置 + 解析 image_pad_id
```

最后一点值得留意：判定的闸门是字符串 `vision_type_str != "close"`，而不是 `vision_type != VISION_CLOSE`。也就是说，如果 `vision.type` 写了一个无法识别的字符串（既不是 `"close"` 也不是 `"vit"`/`"qwen3.5_vl"`），`vision_type` 枚举会**停留在 `VISION_CLOSE`**，但代码仍会**去加载视觉网络**——这会导致「网络加载了、但枚举说自己是 close」的不一致状态。实践中应避免写错 type 字符串。

#### 4.1.3 源码精读

枚举定义在头文件，紧跟视觉配置成员之后：

[src/ncnn_llm_gpt.h:134-138](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L134-L138) —— 定义 `Vision_Type` 三值枚举：

```cpp
enum Vision_Type {
    VISION_CLOSE = 0,
    VISION_VIT = 1,
    VISION_QWEN3_5_VL = 2
} vision_type;
```

构造函数在成员初始化列表里就把 `vision_type` 设为默认值 `VISION_CLOSE`（注意它写在 `try` 块之前的初始化列表中）：

[src/ncnn_llm_gpt.cpp:18-19](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L18-L19) —— 构造函数签名与默认 vision_type：

```cpp
ncnn_llm_gpt::ncnn_llm_gpt(...) 
    : vision_type(Vision_Type::VISION_CLOSE) {
```

判定与翻译逻辑在 `// Vision settings` 段：

[src/ncnn_llm_gpt.cpp:174-185](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L174-L185) —— 读取 `setting.vision` 块并把字符串翻译成枚举：

```cpp
std::string vision_type_str = "close";
if (config["setting"].contains("vision")) {
    auto vision_cfg = config["setting"]["vision"];
    vision_type_str = vision_cfg["type"].get<std::string>();
    
    if (vision_type_str != "close") {
        if (vision_type_str == "vit") {
            vision_type = Vision_Type::VISION_VIT;
        } else if (vision_type_str == "qwen3.5_vl") {
            vision_type = Vision_Type::VISION_QWEN3_5_VL;
        }
        // … 之后是加载网络（4.2）…
```

可以看到：`setting` 没有 `vision` 块时 `vision_type_str` 保持 `"close"`，整个 `if` 不进入，`vision_type` 维持成员初始化列表里的 `VISION_CLOSE`，纯文本模型走的就是这条路。

#### 4.1.4 代码实践

**实践目标**：理解「同一个类、靠一个枚举切换 LLM/VLM」的设计，并识别 `vision_type` 在下游哪里被用到。

**操作步骤（源码阅读型）**：

1. 在 `src/ncnn_llm_gpt.cpp` 里全局搜索 `vision_type ==`，列出所有用到 `vision_type` 做分支的位置。
2. 对每个位置，用一句话记录「这里 `VISION_QWEN3_5_VL` 比 `VISION_VIT` 多做了什么」。

**需要观察的现象**：你会发现 `vision_type` 的真正用途分散在构造函数**之外**——例如图像预处理 `bgr_to_pixel_values` 里给 Qwen3.5-VL 做双帧堆叠（见 [src/ncnn_llm_gpt.cpp:1024](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1024) 与 [src/ncnn_llm_gpt.cpp:1063](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1063)），以及 `prefill` 里给 Qwen3.5-VL 选交错 mRoPE（见 [src/ncnn_llm_gpt.cpp:466-473](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L466-L473)）。

**预期结果**：构造阶段 `vision_type` 只是被「设定」，真正的「使用」在预处理与 prefill。这印证了 4.1.1 的结论：VIT 与 QWEN3_5_VL 在加载阶段无差别。

#### 4.1.5 小练习与答案

**练习 1**：如果一个模型的 `model.json` 写了 `setting.vision.type = "VIT"`（大写），构造后 `vision_type` 会是什么？视觉网络会被加载吗？

> **答案**：字符串比较大小写敏感，`"VIT" != "vit"`，所以 `vision_type` 枚举保持 `VISION_CLOSE`；但因为闸门是 `vision_type_str != "close"`，`"VIT"` 仍会进入加载分支，**视觉网络照样被加载**。结果就是「网络加载了、但枚举是 close」的不一致——下游用到 `vision_type` 的分支都会按纯文本走，图像功能失效。配置务必用小写 `"vit"`。

**练习 2**：为什么把 `vision_type` 的默认值放在成员初始化列表（`: vision_type(VISION_CLOSE)`），而不是在函数体里 `vision_type = VISION_CLOSE;`？

> **答案**：放在初始化列表能保证进入 `try` 块之前 `vision_type` 就有确定值。若 `setting.vision` 缺失或解析中途抛异常（被 catch 转成 `load model failed`），对象也不会留下未初始化的枚举成员，符合 RAII 与「对象要么完整、要么不存在」的原则。

---

### 4.2 视觉网络成员与加载流程

#### 4.2.1 概念说明

一个 VLM 的视觉部分通常拆成两到三个 ncnn 子网，分别对应 ViT 的不同阶段：

| 成员指针 | 对应 ViT 阶段 | 作用 |
| --- | --- | --- |
| `vision_embed_patch` | Patch Embedding | 把一个图像块（`patch_size × patch_size` 像素）线性投影成一个 `patch_dim` 维向量。 |
| `vision_embed_pos`（可选） | 绝对位置编码 | 给每个 patch 注入可学习的二维绝对位置嵌入。**只有部分模型有**。 |
| `vision_encoder` | Transformer Encoder 主干 | 多层自注意力 + MLP，输入 patch 嵌入（可选加位置嵌入/RoPE/mask），输出最终的图像嵌入。 |

`vision_embed_pos` 是**可选**的：它的 `model.json` 字段 `vision_embed_pos_param` 用 `contains` 判定（见 4.2.3）。有没有它，会让 `get_visiual_features()` 走两条完全不同的前向路径（加绝对位置嵌入的「全图注意力」路径 vs. 带窗口重排与注意力掩码的「窗口注意力」路径），但那属于 u5-l3 的内容。

#### 4.2.2 核心流程

当 `vision_type_str != "close"` 时，构造函数按固定顺序加载视觉网络：

```text
1. 从 vision_cfg 读 4 个必填文件名（patch_param/bin、encoder_param/bin），
   拼上 model_path 前缀得到完整路径
2. new 出 vision_embed_patch、vision_encoder 两个 ncnn::Net
3. 若 use_vulkan：给这两个 Net 开 use_vulkan_compute
4. load_param + load_model 加载这两个 Net
5. （可选）若 vision_cfg 含 vision_embed_pos_param：
   同样读路径 → new Net → 可选开 vulkan → load_param + load_model
6. 从分词器查 <|image_pad|> 得到 image_pad_id（4.4）
7. 读 patch_size / patch_dim / max_num_patches / spatial_merge_size（4.3）
8. （可选）若 vision_cfg 含 rope：读 mrope_section（视觉 mRoPE 配置）
```

注意：视觉网络的 Vulkan 开关是**逐 Net 单独设**的，与 decoder_net 的精细策略不同——视觉网络只简单地开 `use_vulkan_compute`，**没有**像 decoder 那样额外开 `use_bf16_storage` 或关 fp16。这与 u2-l1 讲过的「只有 decoder_net 做了精细的精度取舍」一致。

#### 4.2.3 源码精读

视觉网络成员声明在头文件，是三个 `shared_ptr<ncnn::Net>`：

[src/ncnn_llm_gpt.h:96-98](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L96-L98) —— 三个视觉网络指针成员：

```cpp
std::shared_ptr<ncnn::Net> vision_embed_patch;
std::shared_ptr<ncnn::Net> vision_embed_pos;
std::shared_ptr<ncnn::Net> vision_encoder;
```

`shared_ptr` 默认为 `nullptr`，所以纯文本模型（`VISION_CLOSE`）下它们始终是空指针——下游 `get_visiual_features` 也就不会被调用。

加载代码（紧接 4.1.3 的判定之后）：

[src/ncnn_llm_gpt.cpp:187-207](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L187-L207) —— 构造视觉网络文件路径、new Net、按需开 Vulkan、加载：

```cpp
std::string vision_embed_patch_param = model_path + "/" + vision_cfg["vision_embed_patch_param"].get<std::string>();
std::string vision_embed_patch_bin  = model_path + "/" + vision_cfg["vision_embed_patch_bin"].get<std::string>();
std::string vision_encoder_param    = model_path + "/" + vision_cfg["vision_encoder_param"].get<std::string>();
std::string vision_encoder_bin      = model_path + "/" + vision_cfg["vision_encoder_bin"].get<std::string>();
// … fprintf 打印路径 …
vision_embed_patch = std::make_shared<ncnn::Net>();
vision_encoder = std::make_shared<ncnn::Net>();

if (use_vulkan) {
    vision_embed_patch->opt.use_vulkan_compute = true;
    vision_encoder->opt.use_vulkan_compute = true;
}
vision_embed_patch->load_param(vision_embed_patch_param.c_str());
vision_embed_patch->load_model(vision_embed_patch_bin.c_str());
vision_encoder->load_param(vision_encoder_param.c_str());
vision_encoder->load_model(vision_encoder_bin.c_str());
```

可选的 `vision_embed_pos` 用 `contains` 守卫，是「选填」的关键判据：

[src/ncnn_llm_gpt.cpp:209-221](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L209-L221) —— 仅当配置里写了 `vision_embed_pos_param` 才加载位置嵌入网络：

```cpp
if (vision_cfg.contains("vision_embed_pos_param")) {
    std::string vision_embed_pos_param = model_path + "/" + vision_cfg["vision_embed_pos_param"].get<std::string>();
    std::string vision_embed_pos_bin   = model_path + "/" + vision_cfg["vision_embed_pos_bin"].get<std::string>();
    // … fprintf …
    vision_embed_pos = std::make_shared<ncnn::Net>();
    if (use_vulkan) {
        vision_embed_pos->opt.use_vulkan_compute = true;
    }
    vision_embed_pos->load_param(vision_embed_pos_param.c_str());
    vision_embed_pos->load_model(vision_embed_pos_bin.c_str());
}
```

下游 `get_visiual_features` 正是用 `if (vision_embed_pos)` 这个空指针判真假来分流的（见 [src/ncnn_llm_gpt.cpp:1208](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1208)）——这是 `shared_ptr` 默认 `nullptr` 与可选网络天然契合的体现。

#### 4.2.4 代码实践

**实践目标**：确认「VIT 与 QWEN3_5_VL 加载的网络完全相同」，并理解可选网络的判定依据。

**操作步骤（源码阅读型）**：

1. 在 4.2.3 的加载段（[src/ncnn_llm_gpt.cpp:187-221](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L187-L221)）里查找：是否存在任何 `if (vision_type == ...)` 来区分加载哪些网络？
2. 翻到 `get_visiual_features`（[src/ncnn_llm_gpt.cpp:1208](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1208) 与 [src/ncnn_llm_gpt.cpp:1278](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1278)），确认前向路径是用「有没有 `vision_embed_pos`」分流的。

**预期结果**：加载段没有按 `vision_type` 区分网络，三个 `shared_ptr<ncnn::Net>` 对 VIT 和 QWEN3_5_VL 都是同样的赋值；前向则按 `vision_embed_pos` 是否为空分两条路。所以「模型用哪种 ViT 主干」其实由「有没有 `vision_embed_pos_param`」决定，而不是由 `vision.type` 字符串决定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vision_embed_patch` / `vision_encoder` 用直接 `get<string>()` 读取（缺字段会抛异常），而 `vision_embed_pos` 用 `contains` 守卫？

> **答案**：前者是 VLM 的**必填**网络——没有 patch embedding 和 encoder 就无法做视觉，缺字段应当尽早失败（被 catch 转成 `load model failed`）。后者是**可选**网络（取决于模型是否用绝对位置嵌入），用 `contains` 守卫使其缺失时不报错、`vision_embed_pos` 保持 `nullptr`，下游再据此分流。这正是 u1-l5 讲过的「必填用 `get`、选填用 `contains`」约定。

**练习 2**：纯文本模型（`VISION_CLOSE`）下，`vision_encoder` 这个 `shared_ptr` 的值是什么？调用 `vision_encoder->create_extractor()` 会发生什么？

> **答案**：保持默认的 `nullptr`。对其调用任何方法都是未定义行为（解引用空指针）。但代码不会出错，因为纯文本模型根本不会走 `get_visiual_features` 这条路（`prefill(text)` 单参数重载不碰图像），`vision_type == VISION_CLOSE` 时图像相关函数均不被触发。

---

### 4.3 patch 配置字段：patch_size / patch_dim / max_num_patches / spatial_merge_size

#### 4.3.1 概念说明

视觉网络的权重加载完之后，构造函数还要读四个整数配置，它们描述「图像如何被切成 patch、以及 patch 如何合并」：

| 字段 | 默认值（头文件） | 含义 |
| --- | --- | --- |
| `patch_size` | 14 | 每个 patch 的边长（像素）。常见取 14 或 16。 |
| `patch_dim` | 1280 | patch embedding 的输出维度（即一个 patch 经 `vision_embed_patch` 后得到的向量长度）。 |
| `max_num_patches` | 49152 | 单张图最多切成多少个 patch，用于限制图像分辨率上限。 |
| `spatial_merge_size` | 2 | 空间合并尺寸：在 ViT 输出后，把相邻 `2×2` 个 patch 的嵌入合并成 1 个，喂给语言模型。这是 Qwen-VL 系列降低图像 token 数的常用手法。 |

这四个字段是**必填**的（构造函数里直接 `get<int>()`，没有 `contains` 守卫），因为缺任何一个都无法正确切图与投影。

`patch_dim` 有一个容易被忽略的连带关系：它必须等于 `vision_embed_patch` 这个 ncnn 网络的实际输出宽度，也必须等于语言模型 token embedding 的维度（因为图像嵌入最终要和文本嵌入拼接进同一个序列）。`get_visiual_features` 里就是按 `patch_dim` 分配输出矩阵的（[src/ncnn_llm_gpt.cpp:1198](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1198)）。

#### 4.3.2 核心流程

```text
读 patch_size  ──▶ 决定切图块大小、effective_patch_size = patch_size * 2
读 patch_dim   ──▶ 决定 patch 嵌入向量长度（= 喂给 LLM 的图像 token 维度）
读 max_num_patches ──▶ 决定单图分辨率上限（get_image_size_for_patches 里据此缩放）
读 spatial_merge_size ──▶ 决定 ViT 输出后 2×2 合并、reorder_patches_for_merge 的重排粒度
```

下游用法举例（本讲只点名）：

- `get_image_size_for_patches(img_h, img_w, patch_size, max_num_patches, …)`（[src/ncnn_llm_gpt.cpp:1170](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1170)）用 `patch_size` 与 `max_num_patches` 计算目标分辨率。
- `reorder_patches_for_merge(..., spatial_merge_size)`（[src/ncnn_llm_gpt.cpp:1179](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1179)）按合并尺寸重排 patch 顺序。

> 数学背景：若图像被切成 `H_p × W_p` 个 patch（`H_p = target_h / patch_size`），经过 `spatial_merge_size × spatial_merge_size` 合并后，喂给语言模型的图像 token 数为 `(H_p / spatial_merge_size) × (W_p / spatial_merge_size)`。例如 28×28 个 patch、`spatial_merge_size=2`，合并后为 14×14 = 196 个图像 token。

#### 4.3.3 源码精读

四个成员在头文件里带默认值声明：

[src/ncnn_llm_gpt.h:128-132](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L128-L132) —— 视觉配置成员（含 `image_pad_id`，4.4 详述）：

```cpp
int image_pad_id = -1;
int patch_size = 14;
int patch_dim = 1280;
int max_num_patches = 49152;
int spatial_merge_size = 2;
```

构造函数里这四个字段是**无条件 `get<int>()` 读取**（位于「`vision_type_str != "close"`」分支内，所以只有 VLM 模型才会执行到这里）：

[src/ncnn_llm_gpt.cpp:228-232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L228-L232) —— 读取四个 patch 配置（必填，缺一即抛异常）：

```cpp
// Load vision config
patch_size = vision_cfg["patch_size"].get<int>();
patch_dim = vision_cfg["patch_dim"].get<int>();
max_num_patches = vision_cfg["max_num_patches"].get<int>();
spatial_merge_size = vision_cfg["spatial_merge_size"].get<int>();
```

紧随其后是可选的视觉 RoPE 配置（仅当 `vision_cfg` 含 `rope` 块），读取 `mrope_section`（视觉多轴 RoPE 的轴分配，详见 u4-l2）：

[src/ncnn_llm_gpt.cpp:234-240](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L234-L240) —— 可选读取视觉 mRoPE 配置：

```cpp
if (vision_cfg.contains("rope")) {
    auto rope_cfg = vision_cfg["rope"];
    if (rope_cfg["type"] == "mRoPE") {
        vision_rope_type = VisionRoPE_Type::mRoPE;
        mrope_section = rope_cfg["mrope_section"].get<std::vector<int>>();
    }
}
```

#### 4.3.4 代码实践

**实践目标**：算一张图最终会变成多少个语言模型 token，把四个配置串起来。

**操作步骤（纸笔计算 + 源码对照）**：

1. 假设某 VLM 的 `patch_size=14`、`spatial_merge_size=2`、`max_num_patches` 足够大。一张 `336×336` 的图，先算 patch 数：`H_p = 336/14 = 24`，`W_p = 24`，共 `24×24 = 576` 个 patch。
2. 经 `spatial_merge_size=2` 合并：语言模型侧图像 token 数 = `(24/2) × (24/2) = 12×12 = 144`。
3. 对照 `get_visiual_features` 里 `num_patches_w/num_patches_h` 与 `reorder_patches_for_merge` 的用法，验证你的计算。

**预期结果**：`336×336` 的图在该配置下产出 144 个图像 token。这说明 VLM 中一张图会显著拉长序列（144 ≈ 上百个文本 token 的量级），这也是 `max_num_patches` 存在的意义——限制图像分辨率以控制序列长度。

> 待本地验证：以上为按公式推算的结果，若你手头有真实 VLM 权重，可在 `get_visiual_features` 里打印 `num_patches_w`、`num_patches_h`、`image_embeds.h` 实测确认。

#### 4.3.5 小练习与答案

**练习 1**：把 `patch_dim` 在 `model.json` 里写错（比如写成真实输出维度的一半），加载阶段会报错吗？什么时候才会暴露？

> **答案**：加载阶段**不会**报错——`patch_dim` 只是赋给一个 `int` 成员，与网络权重的实际维度无关，构造函数不会校验。错误会在 `get_visiual_features` 运行时暴露：`ncnn::Mat patch_embeds(patch_dim, seq_len)`（[src/ncnn_llm_gpt.cpp:1198](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1198)）按错误的 `patch_dim` 分配缓冲，随后 `memcpy` 拷贝 `patch_embed` 时长度不匹配，可能出现越界或后续拼接维度错位。

**练习 2**：`spatial_merge_size` 从 2 改成 1（不合并），对序列长度有何影响？

> **答案**：合并尺寸为 1 等于不合并，图像 token 数 = patch 总数（上例从 144 变回 576），序列长度显著增加、推理变慢，但每个 token 携带的空间信息更细。`spatial_merge_size` 是「token 数 vs. 空间分辨率」的权衡旋钮。

---

### 4.4 image_pad_id 与 <|image_pad|> 占位令牌

#### 4.4.1 概念说明

VLM 的核心难题是：**图像嵌入是一个个 `patch_dim` 维的连续向量，而语言模型的输入是一串离散 token id。两者怎么在同一个序列里共存？**

ncnn_llm 用的手法是「占位 + 替换」：

1. 在 prompt 文本里，图像的位置用一串连续的 `<|image_pad|>` 特殊令牌占位（比如连续放 144 个）。
2. 分词时，这些 `<|image_pad|>` 被识别成同一个 token id，即 `image_pad_id`，token 序列里出现一长串重复的 `image_pad_id`。
3. token 经 `embed_net` 查表成 `token_embed` 后，在那段 `image_pad_id` 对应的行，**整段替换**成视觉编码器输出的真实 `image_embeds`（这一步叫 `inject_image_embeds`，属 u5-l3）。

所以 `image_pad_id` 的作用是「在 token 序列里给图像预留一段已知 id 的连续槽位」。它必须是分词器里真实存在的一个 token，构造函数会在加载完分词器后到 `bpe->token_to_id()` 里查它。

#### 4.4.2 核心流程

```text
分词器加载完毕（additional_special_tokens 已注册，含 <|image_pad|>）
        │
        ▼
在 bpe->token_to_id() 里 find "<|image_pad|>"
        │
        ├── 找到 ──▶ image_pad_id = 该 token 的 id
        └── 没找到 ─▶ image_pad_id 保持 -1（头文件默认值）
        │
        ▼
（下游）prefill 时，token 序列里连续的 image_pad_id 行
        被 inject_image_embeds 整段替换为真实图像嵌入
```

这里用 `find`（返回迭代器）而非 `at`（找不到抛异常），意味着 `<|image_pad|>` 在配置层面是**可选**的——纯文本模型的分词器里不会有它，`image_pad_id` 就保持 -1，永远不会被用到。

#### 4.4.3 源码精读

`image_pad_id` 成员声明（紧跟 patch 配置之前）：

[src/ncnn_llm_gpt.h:128](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L128) —— `image_pad_id` 默认 -1：

```cpp
int image_pad_id = -1;
```

构造函数里，在视觉网络加载之后、patch 配置读取之前，用 `find` 查询 `<|image_pad|>`：

[src/ncnn_llm_gpt.cpp:223-226](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L223-L226) —— 解析 `<|image_pad|>` 为整数 id：

```cpp
auto it = bpe->token_to_id().find("<|image_pad|>");
if (it != bpe->token_to_id().end()) {
    image_pad_id = it->second;
}
```

为什么 `<|image_pad|>` 能被分词器认识？回顾 u3-l1：构造函数在解析分词器时，会把 `model.json` 的 `tokenizer.additional_special_tokens` 逐个注册（[src/ncnn_llm_gpt.cpp:94-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L94-L97)）。VLM 的 `additional_special_tokens` 里会包含 `"<|image_pad|>"`，所以它成为一个「原子整体」token，encode 时以最长子串匹配整体识别为 `image_pad_id`、绕过 BPE 切分。这正是 u3-l1 强调的「额外特殊令牌须单独注册，是后续整段替换占位的前提」。

下游替换发生地在 `prefill(text, bgr, ctx)`，调用 `inject_image_embeds`：

[src/ncnn_llm_gpt.cpp:460-461](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L460-L461) —— 用 `image_pad_id` 在 token 嵌入序列里定位占位槽位并注入图像嵌入（细节见 u5-l3）：

```cpp
int image_pad_index = -1;
inject_image_embeds(token_ids, token_embed, image_pad_index, image_pad_id, image_embeds);
```

#### 4.4.4 代码实践

**实践目标**：理解「占位令牌必须先注册成特殊令牌」的依赖链。

**操作步骤（源码阅读型）**：

1. 在构造函数里画出 `<|image_pad|>` 的「生命周期」：
   - 注册：[src/ncnn_llm_gpt.cpp:94-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L94-L97)（`additional_special_tokens` 循环里 `AddAdditionalSpecialToken`）。
   - 解析：[src/ncnn_llm_gpt.cpp:223-226](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L223-L226)（`find` 得到 `image_pad_id`）。
   - 使用：[src/ncnn_llm_gpt.cpp:460-461](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L460-L461)（`inject_image_embeds` 替换占位）。
2. 思考：如果 `additional_special_tokens` 里漏写了 `"<|image_pad|>"`，第 2 步 `find` 会怎样？

**预期结果**：漏写则 `find` 返回 `end()`，`image_pad_id` 保持 -1；下游 `inject_image_embeds` 找不到占位、图像嵌入无处注入，VLM 实际「看不见」图。这印证了三步依赖必须完整。

#### 4.4.5 小练习与答案

**练习 1**：为什么解析 `<|image_pad|>` 用 `find`，而解析 `eos` 用 `at`（见 [src/ncnn_llm_gpt.cpp:99-100](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L99-L100)）？

> **答案**：`eos` 是**每个**模型都必需的（没有 eos 就无法停止生成），缺失应当尽早报错，所以用 `at`（找不到抛异常，被 catch 转 `load model failed`）。`<|image_pad|>` 只在 VLM 里需要，纯文本模型的分词器没有它，用 `find` 让其缺失时静默得 -1、不影响纯文本模型加载。这是「必需 vs. 可选」用不同查找方式的又一例证（参见 u3-l1）。

**练习 2**：`image_pad_id` 在 token 序列里重复出现很多次（比如 144 次），会不会让 decoder 把它们当成「同一个词重复」从而产生奇怪注意力？

> **答案**：不会产生「词重复」问题，因为这些位置在进入 decoder **之前**就已经被 `inject_image_embeds` 整段替换成了各不相同的真实图像嵌入向量（每个 patch 的嵌入都不同）。decoder 看到的是 144 个不同的连续向量，而非 144 个相同的查表结果。`image_pad_id` 只在「embed 查表」这一中间步骤短暂存在，随即被覆盖。

---

## 5. 综合实践

**任务**：对照构造函数的 vision 分支（[src/ncnn_llm_gpt.cpp:174-242](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L174-L242)），手工填写下面这张「三种 vision_type 对照表」，并回答附加问题。

### 对照表（请填写后与下文答案核对）

| 维度 | VISION_CLOSE | VISION_VIT | VISION_QWEN3_5_VL |
| --- | --- | --- | --- |
| `setting.vision.type` 字符串 | ① | ② | ③ |
| 是否进入网络加载分支 | ④ | ⑤ | ⑥ |
| 加载 `vision_embed_patch` | ⑦ | ⑧ | ⑨ |
| 加载 `vision_embed_pos`（可选） | ⑩ | ⑪ | ⑫ |
| 加载 `vision_encoder` | ⑬ | ⑭ | ⑮ |
| 读 `patch_size` 等 4 个配置 | ⑯ | ⑰ | ⑱ |
| 需要 `<|image_pad|>`（解析 `image_pad_id`） | ⑲ | ⑳ | ㉑ |
| 下游区别（RoPE/预处理） | 无 | ⑮·a | ㉒ |

### 参考答案

| 维度 | VISION_CLOSE | VISION_VIT | VISION_QWEN3_5_VL |
| --- | --- | --- | --- |
| `setting.vision.type` 字符串 | `"close"` 或缺 `vision` 块 | `"vit"` | `"qwen3.5_vl"` |
| 是否进入网络加载分支 | 否（闸门 `!= "close"` 拦掉） | 是 | 是 |
| 加载 `vision_embed_patch` | 否 | **是** | **是** |
| 加载 `vision_embed_pos`（可选） | 否 | 视 `contains` 而定 | 视 `contains` 而定 |
| 加载 `vision_encoder` | 否 | **是** | **是** |
| 读 `patch_size` 等 4 个配置 | 否 | 是（必填） | 是（必填） |
| 需要 `<|image_pad|>` | 否（保持 -1） | 是（`find` 解析） | 是（`find` 解析） |
| 下游区别（RoPE/预处理） | 无 | 标准 mRoPE、单帧预处理 | **交错 mRoPE**、**双帧时序堆叠**预处理 |

**核心结论（请用自己的话复述一遍）**：

1. `VISION_VIT` 与 `VISION_QWEN3_5_VL` 在**构造阶段加载的网络与配置完全相同**——同样的 3 个 `shared_ptr<ncnn::Net>`、同样的 `model.json` 字段、同样的必填/选填约定。
2. 二者的**唯一差别**是 `vision_type` 枚举值，它在**下游**（`bgr_to_pixel_values` 的双帧堆叠、`prefill` 的交错 mRoPE 选择）才产生分歧。
3. `VISION_CLOSE` 是「纯文本」开关：既不加载视觉网络，也不解析 `image_pad_id`，三个网络指针保持 `nullptr`。
4. `vision_embed_pos` 是否存在，决定的是 ViT 主干走「绝对位置嵌入全图注意力」还是「窗口注意力」路径——这个分叉由「有没有 `vision_embed_pos_param`」控制，**与 `vision.type` 无关**。

> 待本地验证：若你下载了 Qwen2.5-VL 与 Qwen3.5-VL 的 ncnn 权重，可分别对比二者 `model.json` 的 `setting.vision` 块，验证它们的网络字段是否确实一致、差别只在 `type` 字符串。

## 6. 本讲小结

- `vision_type` 是三值枚举（`VISION_CLOSE` / `VISION_VIT` / `VISION_QWEN3_5_VL`），由 `model.json` 的 `setting.vision.type` 字符串翻译而来，大小写敏感、拼写必须精确（`"vit"` / `"qwen3.5_vl"`）。
- 构造函数对 vision 的闸门是 `vision_type_str != "close"`；纯文本模型（`VISION_CLOSE`）不加载任何视觉网络，三个 `shared_ptr<ncnn::Net>` 保持 `nullptr`。
- `VISION_VIT` 与 `VISION_QWEN3_5_VL` 在构造阶段加载**完全相同**的网络（`vision_embed_patch` + `vision_encoder`，可选 `vision_embed_pos`），二者区别仅在枚举值，分歧体现在下游预处理与 mRoPE。
- `vision_embed_patch` / `vision_encoder` 与四个 patch 配置（`patch_size` / `patch_dim` / `max_num_patches` / `spatial_merge_size`）是**必填**（直接 `get`），`vision_embed_pos` 与视觉 `rope` 是**选填**（`contains` 守卫）。
- `<|image_pad|>` 必须先在 `additional_special_tokens` 注册、再用 `find` 解析成 `image_pad_id`；它是图像嵌入在 token 序列里的占位槽位，下游由 `inject_image_embeds` 整段替换为真实图像嵌入。
- 配置异常统一被 `catch` 转成 `ncnn_llm_gpt load model failed` 报错（[src/ncnn_llm_gpt.cpp:243-245](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L243-L245)），遵循「尽早失败」原则。

## 7. 下一步学习建议

本讲只完成了「视觉网络与配置的加载」，这些网络和配置在推理时如何被**使用**，是后续三讲的主题：

- **u5-l2 图像预处理与 patch 切分**：讲 `load_image_to_ncnn_mat`、`get_image_size_for_patches`（用到本讲的 `patch_size` / `max_num_patches`）、`bgr_to_pixel_values`（用到 `vision_type` 与 `patch_size`）、`reorder_patches_for_merge`（用到 `spatial_merge_size`）。
- **u5-l3 视觉特征提取与图像嵌入注入**：讲 `get_visiual_features` 如何调用本讲加载的三个网络，以及 `inject_image_embeds` 如何用 `image_pad_id` 把图像嵌入拼进 token 序列。
- **u5-l4 VLM prefill 完整流程**：把本讲的配置加载、u5-l2 的预处理、u5-l3 的特征提取串成一次完整的图文 prefill，并对比纯文本 prefill 的差异。

建议你在进入 u5-l2 前，先回到本讲的「综合实践」对照表，确认自己能不查源码说出三种 `vision_type` 的加载差别——这是理解后续所有 VLM 代码的前提。
