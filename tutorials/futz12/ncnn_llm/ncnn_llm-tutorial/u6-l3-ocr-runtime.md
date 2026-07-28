# GLM-OCR 与 HunyuanOCR 推理

## 1. 本讲目标

OCR（光学字符识别）是「输入一张图片、输出图中的文字」的任务。ncnn_llm 用一个独立的运行时类 `ncnn_llm_ocr` 把两类 OCR 模型——GLM-OCR 与 HunyuanOCR——接到同一套**共享文本解码运行时**上。

学完本讲，你应当能够：

1. 说清 `ncnn_llm_ocr` 如何根据 `model_type` 把请求分流到 GLM-OCR 或 HunyuanOCR 两条实现路径。
2. 描述 GLM-OCR 的 `prefill`：图片如何被切成 strip、视觉网络如何提取特征、图像嵌入如何注入 token 序列、文本解码器如何用 mRoPE 跑出第一个 token。
3. 描述 HunyuanOCR 与 GLM-OCR 的差异：`smart_resize` + CHW 预处理、4 轴 xdrope、按 id 直接装配 token 序列。
4. 看懂两条路径**共享**的 `generate` 自回归解码循环，理解它如何复用 u2-l2 的 `llm_run_text_embed` / `llm_run_decoder_with_kv` / `llm_run_lm_head` / `llm_select_next_token`。

## 2. 前置知识

本讲假设你已经读过下列讲义，下面只做最小回顾，不重复细节：

- **u2-l2 ncnn 调用模式与共享文本运行时**：建立了 ncnn 的 `create_extractor → input("in0"…) → extract("out0")` 三步调用模式，以及四个共享自由函数 `llm_run_text_embed`、`llm_run_decoder_with_kv`、`llm_run_lm_head`、`llm_select_next_token`。本讲的 `generate` 就是把这四个函数串成一个循环。
- **u2-l3 prefill 文本预填充流程**：建立了 prefill 的骨架——分词 → embed → 因果掩码 → decoder（产出 KV cache）→ lm_head → argmax。本讲 GLM-OCR 的 `prefill` 完全沿用这套骨架，只是多了一段「图像特征注入」。
- **u2-l4 generate 自回归解码主循环**：建立了「embed 上一个 token → decoder（带 KV cache）→ lm_head → 采样」的逐步循环，以及 `is_prefill` 开关、decode 阶段全 0 掩码等概念。本讲的 `generate` 与它几乎同构。
- **u4-l2 视觉位置编码 mRoPE 与 Hunyuan xdrope**：建立了 mRoPE 的 t/h/w 三轴切分（`mrope_section`）与 Hunyuan xdrope 的 linear/w/h/t 四轴（`xdrope_section`）+ NTK-alpha 基。本讲只讲「这些 cache 怎么在 OCR 运行时里被生成和喂进 decoder」。

补充几个 OCR 专属概念：

- **patch（图块）**：把一张大图按固定大小（如 14×14）切成的小方块，是视觉 Transformer 的基本单元。
- **spatial merge（空间合并）**：把相邻 2×2 个 patch 合并成 1 个 token 的操作，用 `spatial_merge_size=2` 控制，目的是减少图像 token 数量。
- **strip（长条）**：GLM-OCR 把所有 patch 按「2×2 块内连续」的顺序重排成的一条又长又窄的图像张量，专门配合视觉网络内部的 reshape。
- **CHW**：通道(Channel)-高(H)-宽(W) 的平面张量布局，是 HunyuanOCR 视觉网络要求的输入格式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/ncnn_llm_ocr.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.h) | OCR 运行时类的声明：成员变量（两条路径共用 + 各自专属）、公开 API（`prefill`/`generate`）、私有视觉预处理与 RoPE 辅助函数。 |
| [src/ncnn_llm_ocr.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp) | 全部实现：构造与分流、GLM-OCR 的 `prefill`/`generate`、HunyuanOCR 的 `prefill_hunyuan`/`generate_hunyuan` 及其视觉预处理。 |
| [examples/ocr_main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/ocr_main.cpp) | 命令行入口：解析 `--model/--image/--prompt`、加载图片、调用 `prefill` + `generate` 打印识别结果。 |
| src/utils/rope_embed.h(.cpp) | 提供 `generate_rope_embed_cache_vision_mrope`（GLM-OCR 文本 mRoPE）与 `generate_hunyuan_xdrope_cos_sin`（Hunyuan 4 轴 xdrope）。 |
| src/utils/vision_rope.h | 提供 `generate_vision_rope_cache_2d`（GLM-OCR 视觉网络 ViT 内部的 2D mRoPE）。 |
| src/ncnn_text_runtime.h(.cpp) | 四个共享文本运行时函数，被两条 `generate` 路径复用。 |

类关系上，`ncnn_llm_ocr` 继承自 `ncnn_llm_base`（复用 KV cache 类型别名、`create_option`、Vulkan 管理等公共能力），但它**不**继承 `ncnn_llm_gpt`——OCR 有自己独立的四个 ncnn 子网与一套自己的 `prefill`/`generate`，只是解码阶段的四个调用步骤与 `ncnn_llm_gpt` 共享同一组自由函数。

## 4. 核心概念与源码讲解

### 4.1 ncnn_llm_ocr 构造与 model_type 分流

#### 4.1.1 概念说明

OCR 运行时要同时支持两种来源不同、结构差异很大的模型：

- **GLM-OCR**：文本解码器用三轴 mRoPE，视觉网络内部用 2D mRoPE，prompt 通过拼字符串再 bbpe 编码生成。
- **HunyuanOCR**：文本解码器用四轴 xdrope（NTK-alpha 缩放），视觉网络不带 RoPE，token 序列按 id 直接装配。

为了让一个类同时承载两者，`ncnn_llm_ocr` 在构造时读 `model.json` 顶层的 `model_type` 字段（取值 `glm_ocr` 或 `hunyuan_ocr`），把它存进成员 `model_type_`，后续 `prefill`/`generate` 在入口处据此分流到不同实现。这就是本讲的核心设计——**「一个外壳、两条路径」**。

#### 4.1.2 核心流程

构造函数做四件事：

1. 读 `model.json`，解析 `model_type`（缺省 `glm_ocr`）。
2. 创建并加载四个 `shared_ptr<ncnn::Net>`：`vision_net_`（视觉）、`text_embed_net_`（token→embedding）、`text_decoder_net_`（Transformer 解码器）、`lm_head_net_`（投影到词表）。注意这四张子网名与 `ncnn_llm_gpt` 的 `embed_net/decoder_net/proj_out_net` 不同，OCR 用的是 `vision/text_embed/text_decoder/lm_head`。
3. 构造 `BpeTokenizer`、解析特殊 token（`eos_`、`eop_`、Hunyuan 的 `bos_id_` 等）。
4. 按 `model_type_` 进入不同分支：HunyuanOCR 分支读 xdrope 与一堆特殊 token id；GLM-OCR 分支读文本 mRoPE + 视觉 mRoPE 配置。

分流的整体形状（伪代码）：

```
ncnn_llm_ocr(model_path, use_vulkan, num_threads):
    读 model.json -> model_type_
    创建并加载 4 个 Net
    构造 BpeTokenizer、解析 eos/eop
    if model_type_ == "hunyuan_ocr":
        读 xdrope(rope.type 必须为 "xdrope") + 视觉 patch 配置 + 特殊 token id
    else:  # glm_ocr
        读文本 rope(type 必须为 "mRoPE", mrope_section 3 项) + 视觉 rope(2D mRoPE)
```

#### 4.1.3 源码精读

构造函数签名与基类初始化，OCR 默认 4 线程：

[ncnn_llm_ocr.cpp:4-5](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L4-L5) —— 构造函数委托给 `ncnn_llm_base(use_vulkan, num_threads)`，线程数缺省 4。

读取 `model_type`：

[ncnn_llm_ocr.cpp:12-14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L12-L14) —— 若 `model.json` 含 `model_type` 就覆盖默认值 `glm_ocr`。这是整条分流链的源头。

创建四个子网并把 Vulkan/线程设置下发：

[ncnn_llm_ocr.cpp:16-37](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L16-L37) —— 四个 `make_shared<ncnn::Net>()`；Vulkan 开启时只对 `vision_net_` 与 `text_decoder_net_` 打开 `use_vulkan_compute`，且 `text_decoder_net_` 额外开 `use_bf16_storage`、关闭 fp16 算术——这与 u8-l1 讲的「decoder 单独 bf16 存储」取舍一致。

从 `config["params"]` 读八条网络文件路径并加载：

[ncnn_llm_ocr.cpp:39-65](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L39-L65) —— 注意键名是 `vision_param/bin`、`text_embed_param/bin`、`text_decoder_param/bin`、`lm_head_param/bin`，与 gpt 运行时的 `embed_token_*`/`proj_out_*` 完全不同，区分了两个运行时。

构造分词器并解析 eos/eop：

[ncnn_llm_ocr.cpp:67-95](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L67-L95) —— `type` 缺省 `bpe`，`"bbpe"` 开启字节级编码；额外特殊 token（如 `<|image|>`）逐个 `AddAdditionalSpecialToken` 并把 id 收进 `additional_special_id_set_` 供 `generate` 跳过输出；`eos_ids`（复数）专为 HunyuanOCR 的多 eos 准备。

**HunyuanOCR 分支**——强校验 xdrope 配置：

[ncnn_llm_ocr.cpp:110-159](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L110-L159) —— 必须满足 `rope.type == "xdrope"`，否则 `throw`；`xdrope_section` 各段之和必须等于 `rope_head_dim/2`（见 125-127 行校验），否则报错。这一段还读了 `bos_token_id`、`system_end_token_id`、`user_end_token_id`、`image_start/end_token_id` 等一堆 Hunyuan 专属 id（默认值取自 Hunyuan 词表），以及 `kv_cache` 开关、视觉 patch 配置。

**GLM-OCR 分支**——强校验 mRoPE 配置：

[ncnn_llm_ocr.cpp:160-260](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L160-L260) —— 文本 rope 必须是 `mRoPE` 且 `mrope_section` 恰好 3 项（t/h/w）；视觉 `setting.vision.rope` 必须是 2D mRoPE，`mrope_section` 恰好 2 项（h/w）且和等于 `rope_head_dim`。任何不符合都 `throw`。

构造函数最外层的 `catch`：

[ncnn_llm_ocr.cpp:262-265](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L262-L265) —— 所有解析异常都被转成 `ok_=false` 并抛 `ncnn_llm_ocr load model failed`，与 gpt 运行时的「尽早失败」哲学一致（参见 u1-l5）。

类声明中两条路径的成员变量分堆存放，便于对照：

[ncnn_llm_ocr.h:54-87](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.h#L54-L87) —— 第 54-64 行是 HunyuanOCR 专属（`use_kv_cache_`、`rope_alpha_`、`xdrope_section_`、各种特殊 id、`eos_ids_`），第 66-87 行是两者共用（`attn_cnt_`、`hidden_size_`、`head_dim_`、`patch_size_`、`image_token_id_` 等）。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认分流逻辑，看清两种 `model_type` 各自要求的配置。

**操作步骤**（源码阅读型实践）：

1. 打开 `src/ncnn_llm_ocr.cpp` 构造函数，找到第 12-14 行的 `model_type` 读取，确认默认值是 `"glm_ocr"`。
2. 找到第 110 行 `if (model_type_ == "hunyuan_ocr")` 分支与第 160 行 `else` 分支，列出两个分支各自**强制要求**的 `model.json` 字段。
3. 在笔记里画一张对照表：左列「HunyuanOCR 必填」、右列「GLM-OCR 必填」。

**需要观察的现象 / 预期结果**：

- HunyuanOCR：`setting.rope.type` 必须是 `"xdrope"`，`xdrope_section` 之和 = `rope_head_dim/2`。
- GLM-OCR：`setting.rope.type` 必须是 `"mRoPE"`，`mrope_section` 恰好 3 项；视觉 `setting.vision.rope.type` 必须是 `"mRoPE"`，其 `mrope_section` 恰好 2 项。
- 若故意把字段写错（例如给 HunyuanOCR 配 `mRoPE`），构造函数会抛异常并被 `catch` 转成 `load model failed`。

**待本地验证**：实际用错误配置构造时打印出的完整异常信息。

#### 4.1.5 小练习与答案

**练习 1**：如果 `model.json` 里完全没有 `model_type` 字段，`model_type_` 会是什么值？后续 `prefill` 会走哪条路径？

**答案**：`model_type_` 保持头文件里的默认值 `"glm_ocr"`（见 [ncnn_llm_ocr.h:52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.h#L52)），`prefill` 走 GLM-OCR 路径。

**练习 2**：为什么 OCR 运行时的四张子网叫 `vision/text_embed/text_decoder/lm_head`，而 `ncnn_llm_gpt` 的三张叫 `embed_token/decoder/proj_out`？

**答案**：这是两个独立运行时各自的命名约定，互不继承。OCR 多一张独立的 `vision_net_`，且文本侧拆成了 embed/decoder/lm_head 三段，命名直接反映「视觉 + 文本三段」的物理结构。

---

### 4.2 GLM-OCR 路径：strip 预处理、run_vision 与图文 prefill

#### 4.2.1 概念说明

GLM-OCR 的 `prefill` 把「一张图 + 一句 prompt」变成「第一个输出 token + KV cache」。它的核心思想是：先用视觉网络把图变成一组图像特征向量，再把这组向量**原位替换**掉 prompt 里若干个 `<|image|>` 占位 token 的 embedding，之后整条序列就当作普通文本送进 decoder。

这里有一个关键细节：图像 patch 在送进视觉网络之前，要按 2×2 空间合并块重排成一条「长条」（strip），因为视觉网络内部会做 reshape 把 2×2 的 patch 合并成 1 个 token。文本解码器侧则用三轴 mRoPE，给图像 token 分配二维位置（行/列），这样模型能区分「水平相邻」与「垂直相邻」。

#### 4.2.2 核心流程

GLM-OCR `prefill(prompt_text, bgr_image)` 的步骤：

```
1. bgr_to_image_strip(bgr) -> image_strip + (num_patches_h, num_patches_w)
   - get_image_size_for_patches: 把图缩放到 patch_size*spatial_merge_size 的整数倍
   - 按 2x2 块重排 patch，逐像素 (x/255-mean)/std 归一化
2. generate_vision_rope_cache_2d -> 视觉网络用的 cos/sin（ViT 内部 2D mRoPE）
3. run_vision(image_strip, cos, sin) -> vision_features (num_vision_tokens 个 hidden_size 维向量)
4. 拼 full_prompt 字符串: [gMASK]<sop><|user|>\n<|begin_of_image|><|image|>×N<|end_of_image|>prompt/nothink<|assistant|>\n<think></think>\n
5. bpe->encode(full_prompt) -> token_ids; llm_run_text_embed -> token_embed
6. 找到所有 image_token_id_ 的位置，把 vision_features 原位 memcpy 进 token_embed
7. 构造 (seq_len × seq_len) 因果掩码（上三角填 -1e38f）
8. generate_rope_embed_cache_vision_mrope -> 文本 decoder 用的 cos/sin（图像段用 2D 位置）
9. llm_run_decoder_with_kv(is_prefill=true) -> decode_out + kv_cache
10. llm_run_lm_head(decode_out 最后一行) -> logits -> argmax -> 第一个 token
11. 打包 ctx(kv_cache, cur_token, position_id) 返回
```

#### 4.2.3 源码精读

**图像尺寸计算**——把宽高对齐到 `patch_size * spatial_merge_size`（即 effective_patch_size）的整数倍，且总像素落在 `[min_pixels_, max_pixels_]`：

[ncnn_llm_ocr.cpp:268-289](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L268-L289) —— `round_by_factor` 把缩放后的尺寸取整到 effective_patch_size 的倍数；面积超上限就按 `sqrt(max_pixels/area)` 缩小，低于下限就放大。注意这与 u5-l2 的 `get_image_size_for_patches` 是同名同思路但**本类自己实现了一份**，因为 OCR 不继承 gpt。

**strip 重排**——核心是把光栅顺序的 patch 按 2×2 块重排成「块内连续」，逐像素归一化：

[ncnn_llm_ocr.cpp:291-348](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L291-L348) —— 先 `ncnn_mat_resize` 到目标尺寸；四层循环 `grid_h → grid_w → mh → mw` 按 2×2 块遍历，块内 patch 被依次写入 `image_strip` 的相邻列段（`strip_base_x = patch_idx * patch_size_`）；每个像素 `pixel[2]/[1]/[0]` 分别对应 R/G/B（输入是 BGR，所以下标 2 是 R），做 `(v/255 - mean)/std` 归一化。注释（306-309 行）指出这等价于 TorchScript wrapper 里的 `view(1,N,3,2,14,14)…reshape(1,3,14,14*N)`。最终 `image_strip` 形状是 `(patch_size*num_patches, patch_size, 3)` 的 planar 张量。

**视觉网络前向**——三输入一输出：

[ncnn_llm_ocr.cpp:350-358](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L350-L358) —— `in0=image_strip`、`in1=cos_cache`、`in2=sin_cache`、`out0=vision_features`。这里的 cos/sin 来自 `generate_vision_rope_cache_2d`（ViT 内部 2D mRoPE），与下面文本 decoder 的 mRoPE 是两套独立的 cache。

**prefill 主体**——视觉前端 + 注入 + decoder：

[ncnn_llm_ocr.cpp:365-463](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L365-L463) 完整函数。几个关键点：

- 366-368 行：`model_type_ == "hunyuan_ocr"` 时转交 `prefill_hunyuan`，否则继续 GLM 逻辑——这是分流入口。
- 384-392 行：拼 `full_prompt` 字符串。开头是 `[gMASK]<sop><|user|>\n<|begin_of_image|>`，中间填 N 个 `<|image|>`（N = `num_vision_tokens`），接 `<|end_of_image|>` + 用户 prompt，末尾若 prompt 不以 `/nothink` 结尾则补一个 `/nothink`，最后加 `<|assistant|>\n<think></think>\n`。这与 GLM 的 `apply_chat_template(add_generation_prompt=true, enable_thinking=false)` 对齐。
- 401-418 行：扫描 `token_ids` 找出所有等于 `image_token_id_` 的位置，若数量与 `num_vision_tokens` 相等就把 `vision_features` 逐行 `memcpy` 进 `token_embed` 对应行——这就是「占位符原位替换」。注意它和 u5-l3 的 `inject_image_embeds`（单占位符展开）不同：GLM-OCR 是 prompt 里**预放了 N 个占位符**再逐个替换。
- 431-443 行：文本 decoder 的 mRoPE cache。`generate_rope_embed_cache_vision_mrope` 接收 `image_token_positions[0]`（图像段起点）、`num_vision_tokens`（图像段长度）、`num_patches_w`、`spatial_merge_size_`，给图像 token 分配二维（行/列）位置，文本 token 仍走线性位置。`next_position_id` 因此不是简单的 `seq_len`，而是 `seq_len - num_vision_tokens + (num_patches_w / spatial_merge_size_)`——图像段在时间轴上被压缩成「列数/合并大小」步。
- 446-448 行：`llm_run_decoder_with_kv(..., true)`——`is_prefill=true`，空 KV cache 进、填满 N 行的 KV cache 出（参见 u2-l2）。
- 451-455 行：`row_range(seq_len-1, 1)` 取最后一行 hidden state，过 lm_head 再 argmax 得第一个 token。
- 458-462 行：把 `kv_cache`、`cur_token`、`next_position_id` 打包进 `ncnn_llm_gpt_base_ctx` 返回。

`generate_text_rope_cache` 是 GLM-OCR 文本侧的单轴 fallback（用于无图像 token 时），内部直接调 u4-l1 的基础 `generate_rope_embed_cache`：

[ncnn_llm_ocr.cpp:360-363](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L360-L363)

#### 4.2.4 代码实践

**实践目标**：跑通一次 GLM-OCR 识别，再对照源码看清图像 token 注入的位置。

**操作步骤**：

1. 按 u1-l2 构建 OCR 示例：`xmake build ocr_main`（产物在 `build/<平台>/<架构>/<模式>/` 下）。
2. 把 GLM-OCR 模型目录放到 `assets/glm_ocr/`（权重需自行下载，参见 u1-l3 说明）。
3. 运行（`ocr_main` 默认 `--model assets/glm_ocr`）：

   ```
   xmake run ocr_main --image 你的图片.png
   ```

   不带 `--prompt` 时默认 prompt 是 `"Read the text in the image."`（见 [ocr_main.cpp:48-50](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/ocr_main.cpp#L48-L50)）。

**需要观察的现象 / 预期结果**：

- 控制台先打印 `Loading OCR model (glm_ocr) from ...` 与各子网路径（来自 [ncnn_llm_ocr.cpp:48-56](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L48-L56)），再打印 mRoPE/视觉 rope 配置摘要。
- 之后逐 token 流式打印识别出的文字，最后输出 `Done.`。
- 若图片 token 数与 `num_vision_tokens` 不匹配，会打印 `WARNING: image token count mismatch!`（[ncnn_llm_ocr.cpp:416-418](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L416-L418)）。

**待本地验证**：不同尺寸图片实际产生的 `num_vision_tokens` 与识别准确度。

#### 4.2.5 小练习与答案

**练习 1**：GLM-OCR 的 prompt 里放了 N 个 `<|image|>`，而 u5-l3 的 VLM 只放一个 `<|image_pad|>` 再展开。这两种「占位注入」策略各有什么代价？

**答案**：GLM-OCR 的「预放 N 个占位符」需要事先知道 N（即 `num_vision_tokens`），并把 N 写进 prompt 字符串再整体 encode，token 序列长度天然等于「文本 + N 个图像位」；u5-l3 的「单占位符展开」在 inject 时才确定 N，更灵活但需要专门的 `inject_image_embeds` 做删除+插入。两者本质都是让 decoder 看到图文混合的 embedding 序列。

**练习 2**：为什么 `next_position_id` 不是 `seq_len` 而是 `seq_len - num_vision_tokens + num_patches_w/spatial_merge_size_`？

**答案**：图像 token 在 mRoPE 的时间轴（t 轴）上不是逐 token +1，而是按「图像宽度方向的合并块数」压缩，所以图像段贡献的时间步远少于 `num_vision_tokens`。续写时 `position_id` 必须从正确的压缩后时间步继续，否则 RoPE 的相对位置语义会错乱。

---

### 4.3 HunyuanOCR 路径：smart_resize、CHW 与 4 轴 xdrope

#### 4.3.1 概念说明

HunyuanOCR 与 GLM-OCR 在「视觉前处理、位置编码、token 装配」三处都不同：

- **视觉前处理**：不做 patch 级 strip 重排，而是把整张图 bicubic 缩放后转成 CHW 平面浮点张量，patch 切分交给视觉网络内部完成。视觉网络**不带 RoPE**（`run_vision_hunyuan` 只喂 `in0`）。
- **位置编码**：文本 decoder 用四轴 xdrope（linear/w/h/t），基频率用 NTK-alpha 放大以扩展上下文。
- **token 装配**：不拼 prompt 字符串，而是按特殊 token 的 **id** 直接 `push_back` 拼出 token 序列（`bos_id_`、`system_end_id_`、`image_start_id_`、一堆 `image_token_id_`、`image_end_id_`、文本 bbpe id、`user_end_id_`）。

#### 4.3.2 核心流程

```
prefill_hunyuan(prompt_text, bgr):
1. bgr_to_chw_image_hunyuan(bgr):
   - smart_resize_hunyuan: 把宽高对齐到 patch_size*spatial_merge_size(=factor) 倍数，
     并保证总面积落在 [min_pixels_, max_pixels_]
   - ncnn_mat_resize_bicubic -> 整图 bicubic 缩放
   - 转 planar CHW float, (v/255-mean)/std 归一化
2. run_vision_hunyuan(image_chw) -> vision_features（只 in0/out0，无 RoPE）
3. 直接按 id 装配 token_ids:
   [bos, system_end, image_start, image_token×num_vision_tokens, image_end, prompt_text 的 bbpe id..., user_end]
4. llm_run_text_embed(token_ids) -> token_embed; 把 vision_features memcpy 进图像 token 槽位
5. 构造 4 轴位置: pos_lin/pos_w/pos_h/pos_t
   - 默认全 = i（线性）
   - 图像段覆盖为 (w=c, h=r, t=0)
6. generate_hunyuan_xdrope_cos_sin(pos4, ...) -> cos/sin（NTK-alpha 基）
7. (seq_len × seq_len) 因果掩码
8. llm_run_decoder_with_kv(is_prefill=true) + lm_head + argmax -> 第一个 token
9. ctx.position_id = seq_len（Hunyuan 不压缩时间轴）
```

Hunyuan 的 vision_tokens 数量有个独特公式：`num_vision_tokens = patch_h * (patch_w + 1) + 2`（多出来的 `+1` 列与 `+2` 是 Hunyuan 视觉网络内部 merger 产生的 begin/row/token 结构）。

#### 4.3.3 源码精读

**smart_resize**——Python `round` 的「四舍六入五成双」复现：

[ncnn_llm_ocr.cpp:545-573](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L545-L573) —— `factor = patch_size * spatial_merge_size`（默认 32）；`round_half_even` 精确复现 Python 内置 `round` 的银行家舍入（0.5 向偶数取整）；先四舍五入到 factor 倍数得 `h_bar/w_bar`，再据面积超上限/下限用 `beta=sqrt(area/max_pixels)` 或 `sqrt(min_pixels/area)` 二次调整。这与 GLM-OCR 的 `get_image_size_for_patches` 思路相近但舍入规则更严格（为了逐位对齐 Python 参考）。

**CHW 转换**——整图 bicubic 后转 planar：

[ncnn_llm_ocr.cpp:575-596](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L575-L596) —— `ncnn_mat_resize_bicubic`（不是 GLM 的普通 resize）；输出 `ncnn::Mat(target_w, target_h, 3)` 三通道平面布局，通道 0/1/2 = R/G/B（从 BGR 的 `px[2]/[1]/[0]` 取），逐像素归一化。注意这里**不切 patch**，整张缩放图直接喂给视觉网络。

**视觉网络前向**——极简，无 RoPE：

[ncnn_llm_ocr.cpp:598-604](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L598-L604) —— 只有 `in0=image_chw`、`out0=feats`，对比 GLM 的 `run_vision` 少了 cos/sin 两个输入，印证「Hunyuan 视觉不带 RoPE」。

**prefill_hunyuan 主体**：

[ncnn_llm_ocr.cpp:606-689](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L606-L689)。关键点：

- 610-614 行：从缩放尺寸算 patch 网格 `gh×gw`、合并后 `patch_h×patch_w`，`num_vision_tokens = patch_h*(patch_w+1)+2`。
- 627-636 行：**按 id 直接装配** token 序列。`bos_id_` → `system_end_id_` → `image_start_id_` → N 个 `image_token_id_` → `image_end_id_` → `prompt_text` 的 bbpe id → `user_end_id_`。记录 `first_image_index` 供后续注入。
- 642-646 行：把 `vision_features` 逐行 `memcpy` 进 token_embed 的图像槽位（`first_image_index + i`）。
- 649-662 行：构造 4 轴位置。先全填线性 `i`，再对图像段覆盖 `pos_w[idx]=c`、`pos_h[idx]=r`、`pos_t[idx]=0`（图像 token 时间轴恒为 0，位置信息靠 w/h 轴携带）。
- 665-668 行：`generate_hunyuan_xdrope_cos_sin(pos4, ...)`，`pos4 = {pos_lin, pos_w, pos_h, pos_t}`。
- 677-682 行：空 KV cache 进 decoder（`is_prefill=true`），最后一行过 lm_head + argmax。
- 684-688 行：`position_id = seq_len`——Hunyuan 不像 GLM 那样压缩时间轴，直接用序列长度。

**4 轴 xdrope 的实现**——NTK-alpha 基 + 按段选轴：

[rope_embed.cpp:406-450](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L406-L450)。核心三步：

- 418-421 行：NTK-alpha 基，\(\theta_{\text{ntk}}=\theta_{\text{base}}\cdot\alpha^{d/(d-2)}\)。
- 423-426 行：每个频率维度的 `inv_freq[j] = 1/θ_ntk^(2j/d)`。
- 429-437 行：`axis_of_dim[j]` 由 `xdrope_section` 的前缀和决定（例如 `[16,16,16,16]` 时 `j/16` 即所在轴），第 442-448 行据此为每个维度取对应轴的 position 值 `pos4[axis_of_dim[j]][i]`，再算 `cos/sin(pos * inv_freq[j])`。

这与 u4-l2 讲的 xdrope 完全一致，本讲只关注「OCR 运行时如何调用它」。

#### 4.3.4 代码实践

**实践目标**：跑一次 HunyuanOCR，并对照源码列出它与 GLM-OCR 的预处理差异。

**操作步骤**：

1. 同样 `xmake build ocr_main`。
2. 把 HunyuanOCR 模型目录放到 `assets/hunyuan_ocr/`（其 `model.json` 里 `model_type` 必须是 `"hunyuan_ocr"`）。
3. 运行：

   ```
   xmake run ocr_main --model assets/hunyuan_ocr --image 你的图片.png
   ```

   不带 `--prompt` 时，`ocr_main` 检测到 `model_type()=="hunyuan_ocr"` 会用中文默认 prompt「检测并识别图片中的文字，将文本坐标格式化输出。」（[ocr_main.cpp:43-47](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/ocr_main.cpp#L43-L47)，源码里写成 UTF-8 字节转义）。

**需要观察的现象 / 预期结果**：

- 启动时打印 `[ncnn_llm_ocr] Vulkan disabled` 或 `enabled`，以及 `[hunyuan_ocr] attn_cnt: ... patch_size: ... alpha: ... xdrope_section size: ...`（[ncnn_llm_ocr.cpp:152-159](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L152-L159)）。
- prefill 阶段打印 `[ncnn_llm_ocr] hunyuan vision: resized WxH, grid GxG, vision_tokens=N`（[ncnn_llm_ocr.cpp:622-623](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L622-L623)）。
- 若 vision 网络输出的 token 数与公式预期不符，会打印 `WARNING: vision token count ... != expected ...`（[ncnn_llm_ocr.cpp:617-621](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L617-L621)）并回退用实际值。

**待本地验证**：实际 `vision_tokens` 数值与坐标格式化输出的样式。

#### 4.3.5 小练习与答案

**练习 1**：HunyuanOCR 装配 token 序列时为什么用「按 id `push_back`」而不是像 GLM 那样拼字符串再 encode？

**答案**：Hunyuan 的特殊 token（`bos`、`system_end`、`image_start/end`、`user_end`）是一批**数值 id**（默认从 120000 起，见 [ncnn_llm_ocr.h:63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.h#L63) 与构造函数 129-134 行），它们未必都有对应的可 encode 字符串形式；直接按 id 装配更可控，也避免 encode 时被 BPE 切分。GLM 的特殊 token 则是 `<|image|>` 这类字符串，能被 bbpe 整体识别，所以走拼字符串路线。

**练习 2**：Hunyuan 的 `position_id` 为什么直接等于 `seq_len`，而 GLM 要做时间轴压缩？

**答案**：xdrope 的图像段位置信息已经由 w/h 两个轴携带（`pos_w=c, pos_h=r`），时间轴 `pos_t=0` 恒定；linear 轴仍逐 token 递增用来维持全局因果顺序。因此续写时直接从 `seq_len` 继续即可，不需要像 GLM mRoPE 那样把图像段在 t 轴上压缩。

---

### 4.4 共享文本解码：generate / generate_hunyuan 自回归循环

#### 4.4.1 概念说明

prefill 产出第一个 token 后，剩余的文字由 `generate` 逐 token 续写。这是 OCR 运行时**复用共享文本运行时**最彻底的部分：两条路径的 `generate` 都把每一步拆成 `llm_run_text_embed → llm_run_decoder_with_kv(is_prefill=false) → llm_run_lm_head → llm_select_next_token` 这标准四步（参见 u2-l2、u2-l4）。

两条路径的 `generate` 差异很小，主要集中在三处：停止条件、特殊 token 跳过策略、以及每步的 RoPE 生成方式。

#### 4.4.2 核心流程

GLM-OCR `generate` 每步：

```
for step in [0, max_new_tokens):
    if cur_token in {eos_, eop_}: break
    if cur_token 不是特殊 token:
        decode 成文本；遇 "```" 或连续 >2 换行则 break；否则 callback 输出
    cur_embed = llm_run_text_embed(cur_token)
    cos/sin = generate_text_rope_cache(1, position_id)   # 单轴
    position_id++
    mask = [1, kv_len+1] 全 0
    decode_out = llm_run_decoder_with_kv(cur_embed, mask, cos, sin, kv_cache, is_prefill=false)
    logits = llm_run_lm_head(decode_out)
    next_id = llm_select_next_token(logits, history, sample_cfg)
    cur_token = next_id; history.insert(next_id)
```

HunyuanOCR `generate_hunyuan` 结构几乎相同，差异：停止条件用 `eos_ids_`（多 eos 集合）；特殊 token 用 `tok >= special_id_begin_` 判定；每步 RoPE 用 `generate_hunyuan_xdrope_cos_sin` 且四个轴都等于当前线性 `position_id`。

#### 4.4.3 源码精读

**GLM `generate` 完整循环**：

[ncnn_llm_ocr.cpp:465-541](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L465-L541)。要点：

- 470-472 行：Hunyuan 分流到 `generate_hunyuan`。
- 474 行：`ctx = ctx_in->clone()`——克隆上下文，不污染调用方（与 u2-l4 / u2-l5 一致）。
- 480 行：停止条件 `cur_token == eos_ || cur_token == eop_`。
- 484-509 行：**特殊 token 跳过 + 启发式截断**。`is_special` 判定含 `eos_/eop_` 与 `additional_special_id_set_`；非特殊 token 才 decode，若文本里出现 ```` ``` ````（489-491 行）或末尾连续换行 >2（494-506 行）就提前 break——这是为了在 OCR 输出「代码块/多段落」时干净收尾。
- 511-516 行：单 token embed + 单轴 RoPE + `position_id++`。
- 518-523 行：mask 是 `[1, kv_cache[0].first.h + 1]` 全 0（新 token 可看所有历史，因果性天然满足，见 u2-l4），`is_prefill=false` 表示 decode 阶段「读旧 cache、原地覆盖 +1」。
- 525-534 行：`llm_run_lm_head` + `llm_select_next_token`（带 repetition penalty 的共享采样）。`sample_cfg` 从 `GenerateConfig` 拷贝 temperature/top_p/top_k/repetition_penalty/do_sample。

**Hunyuan `generate_hunyuan`**：

[ncnn_llm_ocr.cpp:691-745](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L691-L745)。差异点：

- 702 行：停止条件 `eos_ids_.count(tok)`——多 eos 集合。
- 705-710 行：特殊 token 判定 `tok >= special_id_begin_`（Hunyuan 把所有 ≥120000 的 id 视作特殊），非特殊的才 `callback`。
- 716-720 行：每步 RoPE 用 `generate_hunyuan_xdrope_cos_sin`，`pos4` 四个轴都设成同一个 `p1 = {position_id}`（续写时纯文本，无图像二维结构，四轴合一退化为带 NTK-alpha 缩放的单轴）。
- 724-728 行：同样的全 0 mask + `is_prefill=false` decoder。
- 731-738 行：同样的 lm_head + `llm_select_next_token`。

**ocr_main 的采样配置**——刻意压成近贪心：

[ocr_main.cpp:65-71](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/ocr_main.cpp#L65-L71) —— `temperature=0.0`、`top_p=0.00001`、`top_k=1`、`do_sample=0`、`repetition_penalty` 按模型类型取 1.1（GLM）或 1.03（Hunyuan）。OCR 任务追求稳定可复现，所以基本退化为贪心解码；repetition penalty 用来抑制重复（Hunyuan 用更小的 1.03，避免过度惩罚坐标数字）。

#### 4.4.4 代码实践

**实践目标**：通过调整采样参数观察输出变化，并验证两条 `generate` 的停止行为。

**操作步骤**（源码阅读 + 本地实验型实践）：

1. 阅读 [ncnn_llm_ocr.cpp:527-534](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L527-L534) 与 [ncnn_llm_ocr.cpp:731-738](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L731-L738)，确认两条路径都把 `GenerateConfig` 字段拷进 `LlmTokenSampleConfig` 后调用同一个 `llm_select_next_token`。
2. 复制 `examples/ocr_main.cpp` 到一个临时修改版（**不要改原文件**），把 `cfg.temperature` 改成 `0.7`、`cfg.do_sample = 1`、`cfg.top_k = 5`，重新构建运行，对比输出与贪心版的差异。
3. 用同一张图连续运行两次贪心版，确认输出一致（验证 `do_sample=0` 的可复现性）。

**需要观察的现象 / 预期结果**：

- 贪心版（默认）两次运行结果应当完全相同，因为 `llm_select_next_token` 在 `do_sample=0` 或 `temperature<=0` 时走 argmax（见 u3-l4）。
- 提高温度 + 开采样后，输出可能出现细微差异或重复增多（repetition penalty 仍在生效）。

**待本地验证**：两种配置下识别准确率与重复情况的实际对比。

#### 4.4.5 小练习与答案

**练习 1**：两条 `generate` 都先 `ctx = ctx_in->clone()`。如果删掉这行直接改 `ctx_in`，会对调用方产生什么影响？

**答案**：调用方传入的 ctx 会被本次 `generate` 的 KV cache 增长和 `cur_token/position_id` 更新污染。clone 保证了「一次 generate 产生一条独立续写分支」，调用方仍可基于原始 ctx 走另一条分支——这与 u2-l5 讲的「ctx 不可变、clone 出新分支」的多轮机制一致。

**练习 2**：Hunyuan 的 `generate_hunyuan` 里 `pos4` 四个轴都设成 `position_id`，这和 prefill 里四轴分别是 linear/w/h/t 矛盾吗？

**答案**：不矛盾。prefill 时图像 token 有二维网格结构，需要 w/h 轴区分行列；续写阶段产出的都是纯文本 token，没有图像坐标，四轴合一是合理的退化。xdrope 的 NTK-alpha 基缩放仍然生效，所以即便四轴合一，它仍区别于 GLM 的普通 mRoPE。

---

## 5. 综合实践

本实践把本讲四条主线串起来，产出一份「GLM-OCR 与 HunyuanOCR 差异对照表」——这也是本讲规格里要求的代码实践任务。

**任务**：

1. 构建并运行一次 GLM-OCR 识别（4.2.4），记录启动日志、`num_vision_tokens`、识别结果。
2. 构建并运行一次 HunyuanOCR 识别（4.3.4），记录同样的信息。
3. 对照源码 [ncnn_llm_ocr.cpp:365-463](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L365-L463)（GLM prefill）与 [ncnn_llm_ocr.cpp:606-689](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L606-L689)（Hunyuan prefill），填写下表：

| 维度 | GLM-OCR | HunyuanOCR |
|------|---------|------------|
| 视觉前处理函数 | `bgr_to_image_strip`（patch 级 strip + 2×2 块重排） | `smart_resize_hunyuan` + `bgr_to_chw_image_hunyuan`（整图 bicubic + CHW） |
| 视觉网络输入 | `in0=image_strip, in1=cos, in2=sin`（带 2D mRoPE） | 仅 `in0=image_chw`（无 RoPE） |
| 文本 decoder 位置编码 | 三轴 mRoPE（`generate_rope_embed_cache_vision_mrope`） | 四轴 xdrope（`generate_hunyuan_xdrope_cos_sin`，NTK-alpha 基） |
| token 序列装配 | 拼 prompt 字符串 → bbpe encode | 按特殊 token id 直接 `push_back` |
| 续写 position_id | `seq_len - num_vision_tokens + num_patches_w/merge`（t 轴压缩） | `seq_len`（不压缩） |
| 默认 prompt | `"Read the text in the image."` | `"检测并识别图片中的文字，将文本坐标格式化输出。"` |
| `generate` 停止条件 | `eos_/eop_` + ```` ``` ```` + 连续换行 | `eos_ids_` 集合 |
| 默认 repetition_penalty | 1.1 | 1.03 |

4. 在最后用一段话总结：尽管两条路径在视觉前处理与位置编码上差异很大，它们的 `generate` 都退化成对同一组共享函数（`llm_run_text_embed`/`llm_run_decoder_with_kv`/`llm_run_lm_head`/`llm_select_next_token`）的循环调用——这正是 ncnn_llm「跨模型族共享 decoder + KV cache 运行时」设计主线在 OCR 模态的体现。

**待本地验证**：表中每一行都应能在源码里找到对应代码行；若运行时观察到的行为与表格不符，以源码为准并修正自己的理解。

## 6. 本讲小结

- `ncnn_llm_ocr` 用 `model_type`（`glm_ocr` / `hunyuan_ocr`）把请求分流到两条实现路径，共用四个 ncnn 子网（vision/text_embed/text_decoder/lm_head）和一套成员变量。
- GLM-OCR 的 `prefill` 用 `bgr_to_image_strip` 做 patch 级 strip 重排，视觉网络带 2D mRoPE，prompt 里预放 N 个 `<|image|>` 占位符再逐个原位替换为图像特征，文本 decoder 用三轴 mRoPE（图像段时间轴被压缩）。
- HunyuanOCR 的 `prefill_hunyuan` 用 `smart_resize` + CHW 整图预处理，视觉网络不带 RoPE，token 序列按特殊 id 直接装配，文本 decoder 用 NTK-alpha 缩放的四轴 xdrope（图像段时间轴恒为 0、不压缩）。
- 两条路径的 `generate` 结构几乎相同：克隆 ctx → 循环 `embed → decoder(is_prefill=false) → lm_head → llm_select_next_token`，差异仅在停止条件、特殊 token 跳过与每步 RoPE 生成。
- `ocr_main` 默认把采样压成近贪心（temperature=0、top_k=1、do_sample=0），repetition_penalty 按模型取 1.1 或 1.03，追求 OCR 输出的稳定可复现。
- OCR 运行时是「共享文本解码运行时」设计主线的又一次复用——视觉前端各异，但解码主循环与 LLM/VLM/ASR 共用同一组自由函数。

## 7. 下一步学习建议

- **u6-l4 Qwen3-ASR 语音识别**：同为「共享文本解码运行时」的模态扩展，但前端换成音频（wav→mel→audio_conv→audio_encoder），可对比 OCR 的视觉前端与 ASR 的音频前端如何把不同模态塞进同一套 decoder。
- **重读 u2-l2**：带着本讲对 `llm_run_decoder_with_kv` 的 `is_prefill` 开关、`cache_k%d/cache_v%d` 命名的实际使用体会，回看共享运行时的四个函数会更有感觉。
- **u8-l4 模型导出流程**：本讲的视觉网络、xdrope 配置都来自导出脚本产出的 `model.json` 与 ncnn 子网，阅读 `export/hunyuan_ocr_*` 系列脚本能看清这些字段是如何被「烤」进模型文件的。
- **源码延伸**：如果想理解 Hunyuan 视觉网络为何 `num_vision_tokens = patch_h*(patch_w+1)+2`，可结合 `export/hunyuan_ocr_add_kvcache.py` 看 merger 结构；本讲只把它当作视觉网络的既定契约。
