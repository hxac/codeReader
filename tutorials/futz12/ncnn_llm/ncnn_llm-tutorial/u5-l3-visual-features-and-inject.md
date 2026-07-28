# 视觉特征提取与图像嵌入注入

## 1. 本讲目标

本讲承接 u5-l2（图像预处理与 patch 切分），回答两个问题：

1. 一张已经切好的 **patch 像素块**，如何经过视觉编码器变成模型能理解的 **图像嵌入（image_embeds）**？
2. 这些图像嵌入，如何被**注入**到文本 token 嵌入序列里那个 `<|image_pad|>` 占位符的位置？

学完后你应当能够：

- 说清 `get_visiual_features` 从「像素 patch」到「image_embeds」的完整链路与两条分支。
- 读懂 `vision_encoder` 这个 ncnn 网络的前向调用（`in0..in3` → `out0`）。
- 用自己的话讲明白 `inject_image_embeds` 的「单占位符展开」机制。
- 解释 `image_pad_index` 是怎么得到的、它在下游 mRoPE 与 `position_id` 推进里起什么作用。

本讲**不**重复 u5-l1（视觉网络加载）、u5-l2（图像预处理/重排）和 u4-l2（mRoPE/xdrope 的轴分配细节）的内容，只在需要时点一句承接。

## 2. 前置知识

- **patch 与 patch 网格**：图像被切成若干个 `patch_size × patch_size` 的小块，排成 `num_patches_h × num_patches_w` 的网格，patch 总数记为

  \[ \text{seq\_len} = \text{num\_patches\_w} \times \text{num\_patches\_h} \]

  这些概念在 u5-l2 已建立。

- **ncnn 的 Extractor 调用模式**：`create_extractor()` → `input("inN", 张量)` → `extract("outN", 张量)`，靠字符串插槽名绑定输入输出。这是 u2-l2 的核心，本讲视觉网络的前向调用完全沿用这套模式。

- **embedding（嵌入）**：把一个离散对象（token id、一张 patch）映射成一个定长浮点向量。文本侧叫 `token_embed`，图像侧叫 `image_embeds`。

- **占位符 token**：文本提示词里放一个特殊 token（这里是 `<|image_pad|>`）当「图像的座位牌」，推理时再用真实图像嵌入替换它。`image_pad_id` 就是 `<|image_pad|>` 对应的整数 id（u3-l1、u5-l1 已讲过它如何在构造函数里被单独解析）。

- **KV cache / decoder / prefill**：u2-l2、u2-l3 已建立。本讲聚焦 prefill 里**图文分支**比纯文本分支多出来的两步——「提取图像特征」和「注入图像嵌入」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | `get_visiual_features`（图像→image_embeds）、`prefill(text, bgr, ctx)`（图文 prefill 调用点）、构造函数里 `image_pad_id` 的解析都在这里。 |
| [src/utils/rope_embed.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp) | `inject_image_embeds` 的实现（占位符展开）。注意：它住在 rope 模块里，因为下游紧接着要生成 mRoPE。 |
| [src/utils/rope_embed.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h) | `inject_image_embeds` 的声明。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 视觉网络成员（`vision_embed_patch` / `vision_embed_pos` / `vision_encoder`）、`image_pad_id` / `patch_size` 等配置成员的声明。 |
| [examples/llm_ncnn_run/cli_runner.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp) | 用户带图消息的模板拼接，说明文本里**只有一个** `<|image_pad|>` 占位符。 |

> 提示：`get_visiual_features` 这个函数名里把 visual 拼成了 visiual，是项目源码里的既存拼写，本讲引用时保持原样。

---

## 4. 核心概念与源码讲解

### 4.1 get_visiual_features：从像素 patch 到图像嵌入

#### 4.1.1 概念说明

`get_visiual_features` 是图文 prefill 的「视觉前端总入口」。它的职责用一句话概括：

> 给我一张 BGR 图像（`ncnn::Mat`），我还你一组图像嵌入 `image_embeds`（一个 `(hidden_dim, H)` 的 `ncnn::Mat`，H 是图像 token 数），外加网格尺寸 `num_patches_w / num_patches_h`。

它的前半段（图像缩放、patch 切分、`bgr_to_pixel_values`、`reorder_patches_for_merge`）是 u5-l2 的内容，本讲从「patch 像素 → patch 嵌入」这一步开始讲，重点是把每个 patch 喂给 `vision_embed_patch` 网络、再把整批 patch 嵌入堆叠成 `patch_embeds` 的循环。

它解决的问题是：文本 token 可以直接查 `embed_net` 的词表得到嵌入，但图像没有「词表」，必须用一个专门的视觉子网（patch embedding + 视觉 Transformer 编码器）把像素编码成语义向量。

#### 4.1.2 核心流程

`get_visiual_features` 的主干（去掉已在 u5-l2 讲过的预处理细节）：

```
输入: bgr (BGR 图像 Mat)
 ├─ 若 bgr 为空 → 释放 image_embeds, 返回 (无图场景)
 ├─ get_image_size_for_patches → 缩放到目标 target_w/target_h   [u5-l2]
 ├─ 算出 patch 网格 num_patches_w × num_patches_h, seq_len
 ├─ bgr_to_pixel_values + reorder_patches_for_merge + 双帧堆叠   [u5-l2 + 本讲补充]
 ├─ get_window_index (窗口重排索引)
 ├─ ★ patch embed 循环: 对每个 patch 调 vision_embed_patch → 拼成 patch_embeds(patch_dim, seq_len)
 └─ ★ vision_encoder 前向 (两条分支二选一, 见 4.2) → image_embeds
输出: image_embeds, num_patches_w, num_patches_h
```

关键数据形状：

- `pixel_values` 经 reshape 后是 `(patch_size*patch_size*2*3, seq_len)`：每个 patch 是一列，含 2 帧（双帧堆叠）× 3 通道 × `patch_size²` 像素。
- `patch_embeds` 是 `(patch_dim, seq_len)`：每个 patch 一列 `patch_dim` 维向量。
- `image_embeds` 是 `(hidden_dim, H)`：H 为视觉编码器内部 merger 合并后的图像 token 数。

#### 4.1.3 源码精读

先看函数签名与空图早返回：

[src/ncnn_llm_gpt.cpp:1158-1164](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1158-L1164) —— `get_visiual_features` 签名；若 `bgr` 为空就释放 `image_embeds` 并把网格置 0 返回。这个空图分支很重要：它让同一个 `prefill(text, bgr, ctx)` 既能跑纯文本（传空 Mat）又能跑图文，后续 `inject_image_embeds` 收到空 `image_embeds` 就直接跳过。

接着是 patch 网格计算与预处理（u5-l2 已讲）：

[src/ncnn_llm_gpt.cpp:1166-1192](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1166-L1192) —— `get_image_size_for_patches` + `ncnn_mat_resize` 得到 `bgr_resized`；算出 `num_patches_w/num_patches_h` 与 `seq_len`；`bgr_to_pixel_values` + `reorder_patches_for_merge` 后，把每个 patch 的像素**复制两份**堆叠（`tmp(... 2, 3, seq_len)`，行 0 和行 1 内容相同），最终 reshape 成 `(patch_size*patch_size*2*3, seq_len)`。这份「双帧堆叠」是 Qwen3.5-VL 风格视觉前处理的特点（对应 2× 的空间合并因子）。

**本讲的重点一：patch embed 循环。**

[src/ncnn_llm_gpt.cpp:1198-1206](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1198-L1206):

```cpp
ncnn::Mat patch_embeds(patch_dim, seq_len);
for (int i = 0; i < seq_len; i++) {
    ncnn::Mat patch = pixel_values.row_range(i, 1).reshape(patch_size, patch_size, 2, 3);
    ncnn::Mat patch_embed;
    ncnn::Extractor ex = vision_embed_patch->create_extractor();
    ex.input("in0", patch);
    ex.extract("out0", patch_embed);
    memcpy(patch_embeds.row(i), patch_embed.reshape(patch_dim), patch_dim * sizeof(float));
}
```

这段做的事：对 `seq_len` 个 patch 逐个调用 `vision_embed_patch` 子网（一次标准的 `in0`→`out0` 调用），把每个 `patch_size×patch_size×2×3` 的像素块变成 `patch_dim` 维向量，再逐行 `memcpy` 堆进 `patch_embeds`。注意是**逐 patch 循环调用**而非整批一次过——每个 patch 是一次独立的 ncnn 推理。

> 这里的 `vision_embed_patch`、`patch_size`、`patch_dim` 都是 u5-l1 讲过的、构造函数从 `model.json` 的 `setting.vision` 里加载的成员（声明见 [src/ncnn_llm_gpt.h:96-98](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L96-L98) 与 [src/ncnn_llm_gpt.h:128-132](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L128-L132)）。

视觉编码器前向的两条分支留到 4.2 精读。

#### 4.1.4 代码实践

**实践目标**：算出给定图像尺寸下，patch embed 循环会跑多少次 `vision_embed_patch`。

**操作步骤**：

1. 假设某 VLM 配置 `patch_size=14`、`spatial_merge_size=2`，一张图经 `get_image_size_for_patches` 缩放后 `target_w=392`、`target_h=280`。
2. 按源码 [src/ncnn_llm_gpt.cpp:1174-1176](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1174-L1176) 的公式手算：
   - `num_patches_w = (392 + 14 - 1) / 14 = 28`
   - `num_patches_h = (280 + 14 - 1) / 14 = 20`
   - `seq_len = 28 × 20 = 560`
3. 于是 patch embed 循环会对 `vision_embed_patch` 调用 560 次。

**需要观察的现象**：循环次数等于 patch 总数 `seq_len`，与图像分辨率线性相关。

**预期结果**：560 次 patch embedding 调用。具体数值取决于缩放结果，**待本地验证**（需真实模型目录）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_visiual_features` 开头要判断 `bgr` 是否为空？

**参考答案**：为了让 `prefill(text, bgr, ctx)` 这一个函数同时服务纯文本和图文两种场景。纯文本时传一个空 Mat，这里早返回、`image_embeds` 为空，下游 `inject_image_embeds` 收到空嵌入就跳过注入、退化为纯文本 prefill。

**练习 2**：patch embed 循环里，`vision_embed_patch` 的输入 `patch` 是单个 patch 还是整张图？

**参考答案**：单个 patch。`pixel_values.row_range(i, 1)` 取出第 `i` 个 patch（一列），reshape 成 `patch_size×patch_size×2×3`，作为一次独立 ncnn 推理的 `in0`。所以循环次数 = `seq_len`。

---

### 4.2 vision_encoder 前向：两条分支的 ncnn 调用

#### 4.2.1 概念说明

`vision_embed_patch` 只是把像素变成「无位置的 patch 向量」。真正让 patch 之间通过自注意力交换信息、并最终合并（merger）成图像 token 的，是 `vision_encoder` 这个视觉 Transformer 编码器网络。

项目里它有**两条前向分支**，由「是否加载了 `vision_embed_pos`（可学习绝对位置嵌入）网络」决定：

- **分支 A（有 `vision_embed_pos`）**：把可学习位置嵌入 `pos_embeds` 与 2D RoPE 一起喂进编码器。
- **分支 B（无 `vision_embed_pos`，窗口注意力风格）**：用窗口重排 + 分块注意力掩码，喂 2D RoPE 与 attention_mask。

两条分支最终都从一个 `out0` 插槽取出 `image_embeds`，差别只在喂进去的辅助张量和是否需要前后重排。

#### 4.2.2 核心流程

```
分支 A (vision_embed_pos != nullptr):
   patch_embeds ─┐
   pos_embeds    ├─ vision_encoder(in0..in3) → out0 = image_embeds   (单次前向)
   emb_cos/sin  ─┘   (来自 generate_vision_rope_cache_2d)

分支 B (vision_embed_pos == nullptr):
   patch_embeds ─┐
   emb_cos/sin  ─┤─ 按窗口索引重排 (patch_embeds_reordered / emb_*_reordered)
   attention_mask─┘
        ↓
   vision_encoder(in0..in3) → out0 = image_embeds
        ↓
   按窗口索引「还原」顺序 → image_embeds (最终)
```

注意：`generate_vision_rope_cache_2d` 是 **ViT 内部**的位置编码（给视觉编码器自己用的 2D RoPE），与解码器侧的 mRoPE/xdrope（u4-l2）是两条**独立**的线路，不要混淆。

#### 4.2.3 源码精读

**分支 A**：有 `vision_embed_pos` 时的简洁前向。

[src/ncnn_llm_gpt.cpp:1208-1232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1208-L1232):

```cpp
if (vision_embed_pos) {
    ncnn::Mat pos_embeds;
    {
        ncnn::Mat grid(num_patches_w, num_patches_h);
        ncnn::Extractor ex = vision_embed_pos->create_extractor();
        ex.input("in0", grid);
        ex.extract("out0", pos_embeds);
    }
    pos_embeds = reorder_patches_for_merge(pos_embeds, num_patches_h, num_patches_w, spatial_merge_size);
    // ... 生成 emb_cos / emb_sin (generate_vision_rope_cache_2d) ...
    ncnn::Extractor ex = vision_encoder->create_extractor();
    ex.input("in0", patch_embeds);
    ex.input("in1", pos_embeds);
    ex.input("in2", emb_cos);
    ex.input("in3", emb_sin);
    ex.extract("out0", image_embeds);
    return 0;
}
```

要点：

- `vision_embed_pos` 先用一个网格 `grid(num_patches_w, num_patches_h)` 查出可学习位置嵌入 `pos_embeds`，再按 merge 规则重排。
- `vision_encoder` 吃 4 个输入（`in0`=patch 嵌入、`in1`=位置嵌入、`in2/in3`=2D RoPE 的 cos/sin），吐 `out0`=image_embeds。一次前向搞定，无需额外还原。

**分支 B**：窗口注意力风格。

[src/ncnn_llm_gpt.cpp:1234-1296](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1234-L1296) —— 用 `window_index`（来自 `get_window_index`）把 `patch_embeds` 和 `emb_cos/emb_sin` 重排成「窗口连续」顺序，构造分块 `attention_mask`（窗口内为 0、窗口间为 `-1e9f` 屏蔽），再前向：

```cpp
ncnn::Extractor ex = vision_encoder->create_extractor();
ex.input("in0", patch_embeds_reordered);
ex.input("in1", emb_cos_reordered);
ex.input("in2", emb_sin_reordered);
ex.input("in3", attention_mask);
ex.extract("out0", image_embeds);
```

[src/ncnn_llm_gpt.cpp:1287-1296](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1287-L1296) —— 前向后还要按 `window_index` **反向还原**顺序（把按窗口重排的结果映射回原始 patch 位置），得到最终的 `image_embeds`。

无论走哪条分支，产物 `image_embeds` 的形状都是 `(hidden_dim, H)`：行数 H 即「图像 token 数」，由编码器内部的 spatial merger 决定（合并后约为 `seq_len / spatial_merge_size²`，确切值**待本地验证**）。这个 H 在 prefill 里被记作 `image_embeds_size = image_embeds.h`（[src/ncnn_llm_gpt.cpp:446](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L446)），是后续注入与 mRoPE 的关键参数。

#### 4.2.4 代码实践

**实践目标**：对照源码列出两条分支下 `vision_encoder` 的输入插槽差异。

**操作步骤**：

1. 打开 [src/ncnn_llm_gpt.cpp:1223-1230](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1223-L1230)（分支 A）与 [src/ncnn_llm_gpt.cpp:1278-1285](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1278-L1285)（分支 B）。
2. 填下面这张表：

| 插槽 | 分支 A（有 vision_embed_pos） | 分支 B（窗口注意力） |
| --- | --- | --- |
| in0 | patch_embeds | patch_embeds_reordered |
| in1 | ? | ? |
| in2 | ? | ? |
| in3 | ? | ? |
| out0 | image_embeds | image_embeds |

**需要观察的现象**：两条分支都走 `in0..in3`→`out0`，但 in1/in3 含义不同；分支 B 多了前后两次按 `window_index` 的重排。

**预期结果**：分支 A 的 in1=pos_embeds、in3=emb_sin；分支 B 的 in1=emb_cos_reordered、in3=attention_mask。两边 out0 都是 image_embeds。

#### 4.2.5 小练习与答案

**练习 1**：分支 B 为什么在 `vision_encoder` 前后各做一次按 `window_index` 的重排？

**参考答案**：窗口注意力要求「同一个窗口内的 patch 在序列里连续」，所以前向**前**要按 `window_index` 把 patch 重排成窗口连续顺序；但最终我们要的 `image_embeds` 必须对应回**原始 patch 位置**（才能和文本序列里的占位区间对齐），所以前向**后**要按同样的索引反向还原顺序。

**练习 2**：`generate_vision_rope_cache_2d` 生成的 cos/sin 喂给谁？它和 u4-l2 讲的 mRoPE 是同一条线路吗？

**参考答案**：它喂给 **视觉编码器 `vision_encoder` 自己**（作为 ViT 内部的 2D 位置编码）。它和解码器侧给 LLM decoder 用的 mRoPE/xdrope 是**两条独立**的线路，只是都叫「RoPE」、都用 cos/sin 表，不要混淆。

---

### 4.3 inject_image_embeds：单占位符展开机制

#### 4.3.1 概念说明

`inject_image_embeds` 是把「图像」真正接进文本主干的那一刀。它的核心机制可以用一句话说清：

> 文本提示词里只放了**一个** `<|image_pad|>` 占位符；注入时找到这个占位符，把它**删掉**，并在原位插入 H 个真实的图像嵌入（H = `image_embeds.h`）。

这是 ncnn_llm 的一个重要设计选择。作为对比，HuggingFace 版 Qwen2-VL 会在文本里预先写好「每张图 N 个 `<|image_pad|>`」（N 等于图像 token 数）；而本项目始终只放一个占位符，运行时再展开成 H 个。这样做的好处是：文本模板固定、不依赖具体图片分辨率，展开推迟到拿到真实 `image_embeds` 之后。

#### 4.3.2 核心流程

先看「文本里只有一个占位符」是怎么来的。在 cli_runner 里，带图的用户消息模板是：

[examples/llm_ncnn_run/cli_runner.cpp:59](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/cli_runner.cpp#L59):

```cpp
{"user", "<|vision_start|><|image_pad|><|vision_end|>" + input}
```

可见 `<|image_pad|>` 只出现**一次**。分词后，`token_ids` 里就只有一个等于 `image_pad_id` 的元素。

`inject_image_embeds` 的逻辑（伪代码）：

```
若 image_embeds 为空 → 直接返回（纯文本，啥也不做）
image_pad_index = -1
新建 injected 序列，长度 = token_ids.size() - 1 + image_embeds.h
扫描 token_ids，找到第一个 token_ids[i] == image_pad_id:
    image_pad_index = i
    injected = [ token_ids[0..i) ]                      # 占位符之前
             + [ image_pad_id ] × image_embeds.h         # 展开 H 个占位 id
             + [ token_ids[i+1..end) ]                   # 占位符之后
    token_embed_injected 同理：前段照抄、中段换成 image_embeds 的 H 行、后段照抄
    break (只处理第一个占位符)
用 injected 覆盖 token_ids / token_embed
```

注意三个细节：

1. 长度公式 `token_ids.size() - 1 + image_embeds.h`：删掉 1 个占位符，加进 H 个图像嵌入。
2. `token_ids_injected` 里图像区段被填成 **H 个 `image_pad_id`**（`memset`），而 `token_embed_injected` 对应区段填的是**真实 image_embeds 的 H 行**。decoder 实际读的是 `token_embed`（`in0`），不会再重新查词表，所以 `token_ids` 在这里的值只是为了让 `.size()` 正确反映展开后的序列长度。
3. `break` 表示只展开**第一个**占位符（与「单占位符」设计一致）。

#### 4.3.3 源码精读

声明与实现：

[src/utils/rope_embed.h:49](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.h#L49) —— 函数声明：`void inject_image_embeds(std::vector<int>& token_ids, ncnn::Mat& token_embed, int& image_pad_index, int image_pad_id, const ncnn::Mat& image_embeds);`。注意 `token_ids` / `token_embed` / `image_pad_index` 都是**输出**参数（会被改写）。

[src/utils/rope_embed.cpp:302-335](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L302-L335):

```cpp
void inject_image_embeds(std::vector<int>& token_ids, ncnn::Mat& token_embed,
                         int& image_pad_index, int image_pad_id, const ncnn::Mat& image_embeds)
{
    image_pad_index = -1;
    if (image_embeds.empty()) { return; }                  // 纯文本: 跳过

    std::vector<int> token_ids_injected(token_ids.size() - 1 + image_embeds.h);
    ncnn::Mat token_embed_injected(token_embed.w, token_embed.h - 1 + image_embeds.h);

    for (int i = 0; i < (int)token_ids.size(); i++) {
        if (token_ids[i] == image_pad_id) {
            image_pad_index = i;
            // token ids: 前 / H×占位id / 后
            memcpy(token_ids_injected.data(), token_ids.data(), i * sizeof(int));
            memset(token_ids_injected.data() + i, image_pad_id, image_embeds.h * sizeof(int));
            memcpy(token_ids_injected.data() + i + image_embeds.h,
                   token_ids.data() + i + 1, (token_ids.size() - 1 - i) * sizeof(int));
            // token embed: 前 / image_embeds 的 H 行 / 后
            memcpy(token_embed_injected.row(0), token_embed.row(0), i * token_embed.w * sizeof(float));
            memcpy(token_embed_injected.row(i), image_embeds.row(0),
                   image_embeds.h * token_embed.w * sizeof(float));
            memcpy(token_embed_injected.row(i + image_embeds.h), token_embed.row(i + 1),
                   (token_ids.size() - 1 - i) * token_embed.w * sizeof(float));
            break;
        }
    }
    token_ids = token_ids_injected;
    token_embed = token_embed_injected;
}
```

逐段对应「核心流程」里的伪代码。这里 `token_embed.w` 就是 hidden_dim（与 `image_embeds.w` 相同），所以图像嵌入的 H 行能严丝合缝地嵌进文本嵌入矩阵。

再看它在 prefill 里的调用点（上下文很关键）：

[src/ncnn_llm_gpt.cpp:446-461](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L446-L461):

```cpp
get_visiual_features(bgr, image_embeds, num_patches_w, num_patches_h);
const int image_embeds_size = image_embeds.h;
auto token_ids = bpe->encode(input_text, false, false);
token_ids.pop_back();                                       // 弹末位 token (见 u2-l3)
ncnn::Mat input_ids_mat = ncnn::Mat(...).clone();
ncnn::Mat token_embed;
{ /* embed_net: token_ids -> token_embed */ }
int image_pad_index = -1;
inject_image_embeds(token_ids, token_embed, image_pad_index, image_pad_id, image_embeds);
```

调用顺序很清楚：先 `get_visiual_features` 拿到 `image_embeds` → 文本 `encode` + `embed_net` 查表得到 `token_embed` → 调 `inject_image_embeds` 把图像嵌进 `token_embed`。之后 decoder 读的就是这份「图文混合」的 `token_embed`。

#### 4.3.4 代码实践

`inject_image_embeds` 对 `token_ids`（`std::vector<int>`）那段是**纯整数搬运、不依赖任何模型**，我们可以把它单独抽出来跑通，直观看到「拼接前后序列长度与 image_pad_index 的变化」。

**实践目标**：用一段最小 C++ 复现 inject 对 token_ids 的展开逻辑，打印前后长度与 `image_pad_index`。

**操作步骤**：

1. 新建 `inject_demo.cpp`（**示例代码**，仅复现 token_ids 部分，不含 ncnn::Mat）：

```cpp
#include <cstdio>
#include <cstring>
#include <vector>

// 示例代码：复现 inject_image_embeds 对 token_ids 的展开逻辑
int main() {
    const int image_pad_id = 32000;          // 假设 <|image_pad|> 的 id
    const int H = 256;                        // 假设图像产生 256 个图像 token

    // 模拟一段 prompt 分词后的 token_ids（只有一个 image_pad_id 占位符）
    std::vector<int> token_ids = {10, 11, 12, image_pad_id, 13, 14};  // N = 6
    int N = (int)token_ids.size();
    printf("注入前 size = %d\n", N);

    // --- 复刻 inject_image_embeds 的 token_ids 分支 ---
    std::vector<int> injected(N - 1 + H);
    int image_pad_index = -1;
    for (int i = 0; i < N; i++) {
        if (token_ids[i] == image_pad_id) {
            image_pad_index = i;
            memcpy(injected.data(), token_ids.data(), i * sizeof(int));
            // memset 按字节填，这里用循环填 int 更直观
            for (int k = 0; k < H; k++) injected[i + k] = image_pad_id;
            memcpy(injected.data() + i + H, token_ids.data() + i + 1,
                   (N - 1 - i) * sizeof(int));
            break;
        }
    }
    // ----------------------------------------------------

    printf("image_pad_index = %d\n", image_pad_index);
    printf("注入后 size = %d  (期望 %d = %d - 1 + %d)\n",
           (int)injected.size(), N - 1 + H, N, H);
    printf("占位符前 %d 个文本 token, 图像区 %d 个, 占位符后 %d 个文本 token\n",
           image_pad_index, H, N - 1 - image_pad_index);
    return 0;
}
```

2. 编译运行：`g++ -std=c++17 inject_demo.cpp -o inject_demo && ./inject_demo`

**需要观察的现象**：注入后序列变长（多了 H−1 个位置）；`image_pad_index` 指向图像区起点；图像区被填满 `image_pad_id`。

**预期结果**：

```
注入前 size = 6
image_pad_index = 3
注入后 size = 261  (期望 261 = 6 - 1 + 256)
占位符前 3 个文本 token, 图像区 256 个, 占位符后 2 个文本 token
```

> 说明：真正的 `inject_image_embeds` 还会同步展开 `token_embed`（`ncnn::Mat`），把图像区的 H 行换成真实 `image_embeds`；这里只演示 token_ids 一侧的长度/索引变化，因为这部分不依赖模型权重、可直接验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么注入后的序列长度是 `token_ids.size() - 1 + image_embeds.h`，而不是 `token_ids.size() + image_embeds.h`？

**参考答案**：因为要先**删掉那一个** `<|image_pad|>` 占位符（−1），再把 H 个真实图像嵌入插进原位（+H），净变化是 `−1 + H`。

**练习 2**：注入后 `token_ids` 的图像区被填成 H 个 `image_pad_id`，但 decoder 真正用的是 `token_embed` 而非 `token_ids`。那这 H 个 `image_pad_id` 还有什么用？

**参考答案**：decoder 读 `in0 = token_embed`（已被替换为真实图像嵌入），不会重新查词表，所以这 H 个 id 的「值」对 decoder 无意义；但 `token_ids.size()` 被用来确定展开后的序列长度，进而决定下游 mask 的列数、RoPE cache 的 `seqlen` 等，所以必须把数量填对。换句话说，这里填 `image_pad_id` 只是「占个位、把长度撑对」。

**练习 3**：如果文本里**没有** `<|image_pad|>` 但 `image_embeds` 非空，会发生什么？

**参考答案**：扫描循环找不到 `token_ids[i] == image_pad_id`，`image_pad_index` 保持 −1、`injected` 全是未初始化内容，但末尾仍执行 `token_ids = token_ids_injected`，会把 `token_ids` 替换成垃圾数据。实际使用中，带图消息一定会经 cli_runner 的模板带上 `<|image_pad|>`，所以正常路径不会触发；但这也提醒：调用 `inject_image_embeds` 前要保证「有图就有占位符」。

---

### 4.4 image_pad_index：占位起点与下游 mRoPE / position_id

#### 4.4.1 概念说明

`image_pad_index` 是 inject 阶段顺手产出的「**图像区在展开序列里的起始下标**」。它本身只是一个 int，但它是把「图像」和「位置编码」两件事串起来的钥匙：

- 对 **mRoPE 生成器**（u4-l2）：它需要知道「序列里 `[image_pad_index, image_pad_index + image_embeds_size)` 这一段是图像 token」，才能给这一段的 token 分配「时间轴恒定、高度/宽度轴随网格行列变化」的多轴位置。
- 对 **position_id 推进**：因为图像 token 在时间轴上不逐个递增，整块图像只贡献少量时间步（约 `num_patches_w / spatial_merge_size` 步），所以 prefill 末尾推进 `position_id` 时要单独算图像段的贡献。

#### 4.4.2 核心流程

`image_pad_index` 的生命周期：

```
构造函数:  image_pad_id = bpe->token_to_id().find("<|image_pad|>")  (默认 -1)
prefill:   image_pad_index = -1                                          (初值)
           ↓
inject:    扫到 token_ids[i] == image_pad_id → image_pad_index = i      (赋值)
           ↓
mRoPE 生成: 把 image_pad_index 传进 generate_rope_embed_cache_vision_mrope(_interleaved)
position_id 推进: 用 token_ids.size() - image_embeds_size + num_patches_w/spatial_merge_size
```

#### 4.4.3 源码精读

**来源一**：构造函数里解析 `image_pad_id`（`image_pad_index` 的「原料」）。

[src/ncnn_llm_gpt.cpp:223-226](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L223-L226):

```cpp
auto it = bpe->token_to_id().find("<|image_pad|>");
if (it != bpe->token_to_id().end()) {
    image_pad_id = it->second;
}
```

用 `.find()` 而非 `.at()`：纯文本模型没有 `<|image_pad|>`，这里就静默保持默认 −1（u3-l1 讲过「必需用 at、可选用 find」的区分设计）。成员声明 [src/ncnn_llm_gpt.h:128](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L128)：`int image_pad_id = -1;`。

**来源二**：`image_pad_index` 在 inject 里被赋值（见 4.3.3，`image_pad_index = i`）。

**下游一**：传给 mRoPE 生成器。

[src/ncnn_llm_gpt.cpp:467-474](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L467-L474):

```cpp
if (vision_type == Vision_Type::VISION_QWEN3_5_VL) {
    generate_rope_embed_cache_vision_mrope_interleaved(
        token_ids.size(), rope_head_dim, new_ctx->position_id,
        image_pad_index, image_embeds_size, num_patches_w, cos_cache, sin_cache, rope_theta);
} else {
    generate_rope_embed_cache_vision_mrope(
        token_ids.size(), rope_head_dim, new_ctx->position_id,
        image_pad_index, image_embeds_size, num_patches_w, spatial_merge_size,
        mrope_section, cos_cache, sin_cache, rope_theta);
}
new_ctx->position_id += token_ids.size() - image_embeds_size + (num_patches_w / spatial_merge_size);
```

可以看到 `image_pad_index` 和 `image_embeds_size` 一起决定了「图像 token 在序列里的区间 `[image_pad_index, image_pad_index + image_embeds_size)`」，这正是 mRoPE 内部判断「当前 token 是文本还是图像、落在第几行/列」的依据（具体轴分配算法见 u4-l2，本讲不重复）。

**下游二**：`position_id` 推进。最后一行把图像段的时间步贡献单独算出：整段图像在时间轴上只占 `num_patches_w / spatial_merge_size` 步（合并后网格的行数），而不是 `image_embeds_size` 步。文本 token 则每个占 1 步，共 `token_ids.size() - image_embeds_size` 个。

#### 4.4.4 代码实践

**实践目标**：跟踪 `image_pad_index` 从「−1」到「被 inject 赋值」再到「传给 mRoPE」的完整路径。

**操作步骤**：

1. 在 [src/ncnn_llm_gpt.cpp:460](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L460)（`int image_pad_index = -1;`）与 [src/ncnn_llm_gpt.cpp:469](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L469)（mRoPE 调用）各加一行临时 `fprintf(stderr, ...)` 打印 `image_pad_index`、`image_embeds_size`、`token_ids.size()`。
2. 用一个带图模型跑一次 `llm_ncnn_run`（参考 u1-l2 的运行方式），观察打印值。
3. 对照 [src/utils/rope_embed.cpp:337-389](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/rope_embed.cpp#L337-L389) 里 `generate_rope_embed_cache_vision_mrope` 对 `i < image_pad_index` / `i >= image_pad_index + image_embeds_size` / else（图像段内）的三分支判断，确认 `image_pad_index` 正好划出了图像区间。

**需要观察的现象**：inject 前为 −1；inject 后变成图像区起点（一个具体的非负下标）；该值与 `image_embeds_size` 共同圈出图像 token 范围。

**预期结果**：例如 `image_pad_index=3`、`image_embeds_size=256`、`token_ids.size()=261`（与 4.3.4 的演示一致）。具体数值**待本地验证**（依赖真实图像与模型）。

> 注意：实践只是加临时日志观察，**不要提交**这些改动（本讲义禁止修改源码；练习后请还原）。

#### 4.4.5 小练习与答案

**练习 1**：`image_pad_id` 用 `.find()` 解析、找不到保持 −1；而 `eos` 在构造函数里用 `.at()` 解析。为什么这里用 find？

**参考答案**：`<|image_pad|>` 是**视觉模型专有**的占位符，纯文本模型没有它，属「可选」配置。用 `.find()` 找不到就静默 −1，`inject_image_embeds` 收到 `image_pad_id=-1` 时（配合空 `image_embeds`）自然走纯文本路径。而 `eos` 是所有模型都必需的，找不到就是配置错误，必须用 `.at()` 抛异常尽早失败。

**练习 2**：`position_id` 推进用的是 `token_ids.size() - image_embeds_size + (num_patches_w / spatial_merge_size)`，为什么图像段不是逐 token 加 `image_embeds_size`？

**参考答案**：因为 mRoPE 下图像 token 在**时间轴**上不逐个递增——同一行的 token 共享同一个时间位置，整块图像只占「合并后行数 = `num_patches_w / spatial_merge_size`」个时间步（高度/宽度信息走的是 h/w 轴，不走时间轴）。所以图像段时间步贡献是 `num_patches_w / spatial_merge_size` 而非 `image_embeds_size`，文本段才逐 token 计 `token_ids.size() - image_embeds_size` 步。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「一张图如何进入文本主干」的端到端跟踪。建议产出一张表 + 一段可运行演示。

**任务**：假设一个 VLM 配置 `patch_size=14`、`spatial_merge_size=2`，某次 prefill 输入图像经缩放后 `target_w=392`、`target_h=280`，文本 prompt 为 `"<|vision_start|><|image_pad|><|vision_end|>描述这张图"`，分词后得到 `token_ids = [v0, v1, image_pad_id, v2, v3, v4]`（6 个 token，末位 v4 会先被 pop_back，见 u2-l3，于是主体 5 个）。

1. **patch embed**（4.1）：算出 `num_patches_w=28`、`num_patches_h=20`、`seq_len=560`；`vision_embed_patch` 被调用 560 次。
2. **vision_encoder**（4.2）：经编码器内部 merger 后，`image_embeds` 行数 H ≈ `560 / (2²) = 140`（**待本地验证**确切值）。
3. **inject**（4.3）：展开后 `token_ids.size() = 5 - 1 + 140 = 144`，`image_pad_index = 2`（占位符在主体序列里的下标）。把 4.3.4 的演示程序改成 `H=140`、`token_ids={v0,v1,image_pad_id,v2,v3}` 重跑，验证长度与 `image_pad_index`。
4. **image_pad_index 下游**（4.4）：图像区间为 `[2, 2+140)=[2,142)`；`position_id` 推进里图像段贡献 `num_patches_w / spatial_merge_size = 28/2 = 14` 步。

**交付物**：

- 一张表，列出每个阶段的关键尺寸（`seq_len`、H、注入前后 `token_ids.size()`、`image_pad_index`、图像区间、position_id 图像段贡献）。
- 4.3.4 演示程序的输出截图（改 `H` 后），证明你对 inject 长度/索引的理解。

**预期结果**：尺寸表与演示程序输出一致；能用自己的话解释「图像从 bgr 到成为 decoder `in0` 的一部分」经过了哪几步、每步改变了什么。

> 数值标注「待本地验证」处，需在有真实 VLM 模型目录的环境中（参考 u1-l2/u1-l5 准备 `assets/`）用 `llm_ncnn_run` 实跑确认。

## 6. 本讲小结

- `get_visiual_features` 是图文 prefill 的视觉前端：把 BGR 图缩放→切 patch→逐 patch 调 `vision_embed_patch` 得 `patch_embeds`→经 `vision_encoder` 前向得 `image_embeds`；空图早返回，让同一函数兼顾纯文本。
- `vision_encoder` 有两条前向分支：有 `vision_embed_pos` 时喂「patch+位置嵌入+2D RoPE」一次过；否则走「窗口重排+分块 attention_mask」并在前后各做一次按 `window_index` 的重排/还原。两分支都遵循 `in0..in3 → out0` 的 ncnn 调用模式。
- `inject_image_embeds` 的核心是「**单占位符展开**」：文本里只有一个 `<|image_pad|>`，找到后删掉、在原位插入 H 个真实图像嵌入；展开后 `token_embed`（decoder 的 `in0`）图文混合，`token_ids` 只用于撑对长度。
- `image_pad_index` 是 inject 顺带产出的「图像区起点」，配合 `image_embeds_size` 圈出图像 token 区间，供 mRoPE 分配 t/h/w 轴、供 `position_id` 推进单独计算图像段时间步。
- `image_pad_id` 在构造函数用 `.find()` 解析（纯文本模型无此 token 则 −1），体现「可选配置静默降级、必需配置 `.at()` 尽早失败」的设计。

## 7. 下一步学习建议

- **u5-l4（VLM prefill 完整流程）**：本讲只讲了 prefill 里「提取视觉特征 + 注入嵌入」两步；下一讲会把 `clone_ctx`、带历史 KV 的 mask 构造、mRoPE cache 选择与 position_id 推进串成完整图文 prefill，建议接着读。
- **回顾 u4-l2**：如果对 `image_pad_index` 如何驱动 mRoPE 的 t/h/w 轴分配还不够清晰，回看 u4-l2 的 `generate_rope_embed_cache_vision_mrope` 三分支判断。
- **动手验证**：准备一个 VLM 模型目录（按 u1-l5 的 `model.json` 配 `setting.vision`），用 `llm_ncnn_run` 跑一次带图推理，结合本讲「综合实践」的尺寸表对照真实日志，把「待本地验证」的数值填实。
- **延伸阅读**：对照 `examples/llm_ncnn_run/cli_runner.cpp` 的带图分支与 `src/ncnn_llm_ocr.cpp`（u6-l3），看 OCR 运行时如何复用同一套「视觉特征 + 共享文本解码」思路。
