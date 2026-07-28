# CLIP 多模态嵌入

## 1. 本讲目标

本讲承接 [u6-l1 文本嵌入 API](u6-l1-text-embedding.md)。在 u6-l1 里，我们只跑通了 `ncnn_embedding::encode_text`——把一段文字变成一个固定维度的向量。本讲要回答下一个自然问题：**如果输入不是文字，而是一张图片呢？**

学完本讲，你应当能够：

- 知道如何用一个 `supports_image()` 调用判断当前嵌入模型是否具备图像能力；
- 理解 CLIP 风格的「双编码器 + 对比学习」与 VLM（u5 单元）「把图像注入语言模型 token 流」的本质区别；
- 读懂 `preprocess_image` 的双线性缩放与逐通道归一化；
- 用 `encode_image` / `encode_image_file` 得到图像向量，并用余弦相似度做「以文搜图」检索。

## 2. 前置知识

本讲默认你已经掌握以下概念（在前面讲义中建立）：

- **嵌入向量（embedding）**：把任意一段文本/一张图压缩成一个固定长度的浮点向量，向量越接近，语义越相似（u6-l1）。
- **归一化（normalize）**：把向量缩放到长度为 1（单位球面上），这样两个向量的点积就等于余弦相似度（u6-l1、u2-l1）。
- **ncnn 的 Extractor 调用模式**：`create_extractor()` → `input("inN", …)` → `extract("outN", …)`（u2-l2）。
- **ncnn::Mat 的通道布局**：`Mat(w, h, c)` 是「平面（planar）」排布，即 `mat.channel(c).row(y)[x]` 访问第 c 通道、第 y 行、第 x 列（u5-l2）。

本讲新引入一个核心概念：**对比学习（contrastive learning）**。CLIP 的训练目标不是「让模型看图说话」，而是「让匹配的图文对在向量空间里靠得最近」。形象地说：

- 文本编码器把句子映射成空间里的一个点；
- 图像编码器把图片映射成同一个空间里的另一个点；
- 训练时拉近「这只猫的图片」和「一只猫」两个点，推开它和「一辆汽车」的点。

推理时，我们只要比较文本点和图像点的距离（余弦相似度），就能知道哪句话最描述这张图。这一点和 u5 单元的 VLM **完全不同**：VLM 是把图像特征**塞进**语言模型的 token 序列里让模型生成文字；CLIP 则是图文**各自独立编码**、只在最后比一次相似度。记住这条分界线，本讲就不会和 VLM 混淆。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `src/ncnn_embedding.h` | `ncnn_embedding` 类声明。定义 `vision_encoder_net` 成员、`supports_image()`、`encode_image/encode_image_file`、`preprocess_image`，以及图像预处理相关配置成员（`image_size_`、`image_mean_`、`image_std_`）。 |
| `src/ncnn_embedding.cpp` | 上述函数的实现。本讲重点是构造函数里 `model_type=="clip"` 分支、`preprocess_image`、`encode_image`、`encode_image_file` 四段。 |
| `examples/clip_main.cpp` | 演示入口：编码一批文本 + 一张图，打印图文相似度矩阵。 |
| `src/utils/image_utils.h` / `image_utils.cpp` | 提供 `load_image_to_ncnn_mat`（磁盘图片→`ncnn::Mat`）和 `ncnn_mat_empty`，被 `encode_image_file` 复用。 |

对应的 xmake 构建目标在 [xmake.lua:121-125](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L121-L125)，名为 `clip_main`。

## 4. 核心概念与源码讲解

### 4.1 模型类型与 vision_encoder_net：supports_image 判断

#### 4.1.1 概念说明

`ncnn_embedding` 这个类既能跑纯文本嵌入模型（如 mclip、jina-embedding），也能跑图文双编码器模型（如 jina-clip-v2）。这两类模型在 `model.json` 里靠一个字段区分：`model_type`，取值 `"embedding"` 或 `"clip"`。

只有 `"clip"` 类型才需要第二个神经网络——**视觉编码器（vision encoder）**。于是类里用一个成员指针 `vision_encoder_net` 是否为空，来代表「这个模型到底支不支持图像」。对外暴露的判断函数就是 `supports_image()`。这是一种很常见的「能力探测」设计：不问类型字符串，而问「相关网络有没有被加载」。

#### 4.1.2 核心流程

```text
读 model.json 的 model_type
        |
        +-- "clip"      → 创建 vision_encoder_net，加载 vision_encoder_param/bin
        +-- 其他        → vision_encoder_net 保持 nullptr

supports_image() = (vision_encoder_net != nullptr)

所有图像 API（encode_image / encode_image_file）入口先检查 !vision_encoder_net，
若为空则直接返回空向量，绝不崩溃。
```

#### 4.1.3 源码精读

类成员声明——`vision_encoder_net` 是一个可空的 `shared_ptr<ncnn::Net>`：

[src/ncnn_embedding.h:21-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L21-L22) 定义了文本/视觉两个编码器网络指针，视觉那一个初始为空。

构造函数里，**只有 `model_type=="clip"` 才会 new 出视觉网络**：

[src/ncnn_embedding.cpp:20-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L20-L22) 这三行是整条图像链路的总开关——若不是 clip，后面所有图像函数都拿不到网络。

随后视觉网络的权重在 clip 分支里单独加载（`vision_encoder_param` / `vision_encoder_bin` 两个键，注意它们和文本编码器的键名 `text_encoder_param` / `text_encoder_bin` 是分开的）：

[src/ncnn_embedding.cpp:74-81](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L74-L81) 读取并加载视觉编码器的 `.param` 与 `.bin`。

对外的能力探测函数极其简洁——指针非空即支持：

[src/ncnn_embedding.h:52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L52) `supports_image()` 的全部实现就是判一次空。

#### 4.1.4 代码实践

**实践目标**：验证「`supports_image()` 的真假完全由 `model_type` 决定」。

**操作步骤（源码阅读型）**：

1. 打开 `src/ncnn_embedding.cpp`，在构造函数第 20-22 行确认：`vision_encoder_net` 只在 `model_type_ == "clip"` 时被 `make_shared`。
2. 全局搜索 `vision_encoder_net` 的所有写入点，确认除此之外没有任何地方会给它赋值——也就是说一旦构造时不是 clip，它就永远是 `nullptr`。
3. 打开 `examples/clip_main.cpp` 第 77 行，确认主程序用 `embed.supports_image()` 决定要不要走图像分支：`if (!image_path.empty() && embed.supports_image())`。

**预期结果**：你会看到「是否支持图像」这条能力被收敛到一个布尔判断上，主程序无需关心 `model_type` 字符串，只看 `supports_image()`。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `model.json` 把 `model_type` 写成 `"embedding"`，却同时提供了 `vision_encoder_param` 字段，会发生什么？

**参考答案**：什么也不会发生。构造函数只在 `model_type_ == "clip"` 时才创建并加载 `vision_encoder_net`（[src/ncnn_embedding.cpp:20-22](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L20-L22)），`vision_encoder_param` 字段在非 clip 分支里根本不会被读取。`supports_image()` 返回 false，所有图像 API 直接返回空向量。多余的配置字段被静默忽略。

**练习 2**：为什么 `supports_image()` 不直接返回 `model_type_ == "clip"`，而要去判 `vision_encoder_net != nullptr`？

**参考答案**：判指针更稳健。`model_type_` 只说明「声明了要 clip」，但如果视觉网络加载失败（文件缺失等），构造函数会抛异常、`ok_` 被置 false，整个对象本就不可用。判 `vision_encoder_net` 这个「实际产物」比判「意图字符串」更贴近真实状态，也和「以加载结果为准」的整体设计（参考 u2-l1 的 `ok_` 健康标志）一致。

---

### 4.2 图像预处理 preprocess_image：双线性缩放与逐通道归一化

#### 4.2.1 概念说明

一张磁盘上的图片，尺寸和数值范围都不能直接喂进神经网络。`preprocess_image` 负责把任意分辨率的原始像素矩阵改造成视觉编码器期望的输入：

1. **缩放（resize）**：把宽高不等的目标图缩放到固定边长 `image_size_`（CLIP 默认 224）。
2. **归一化（normalize）**：把 0~255 的像素值按通道减均值、除标准差，变换到大致以 0 为中心的范围。

这两步几乎对所有视觉模型都通用，区别只在具体数值（`image_size_`、`image_mean_`、`image_std_`）。CLIP 用的是 ImageNet 的统计量：均值 `(0.485, 0.456, 0.406)`、标准差 `(0.229, 0.224, 0.225)`，顺序按 **R, G, B**。

#### 4.2.2 核心流程

缩放采用**双线性插值（bilinear interpolation）**：对输出图里的每个像素 `(x, y)`，先反推它在原图的浮点坐标 `(src_x, src_y)`，取周围 4 个真实像素，按距离做加权平均。

设 4 个邻接像素为 \(v_{00}, v_{01}, v_{10}, v_{11}\)（下标第一位是 y、第二位是 x），横向距离 \(dx\)、纵向距离 \(dy\)，则插值公式为：

\[
v = v_{00}(1-dx)(1-dy) + v_{01}\,dx(1-dy) + v_{10}(1-dx)\,dy + v_{11}\,dx\,dy
\]

随后对每个通道 c 做归一化：

\[
v'_c = \frac{v_c / 255 - \mu_c}{\sigma_c}
\]

最终输出是 ncnn 的**平面（planar）布局** `Mat(image_size_, image_size_, 3)`，即通道在前、空间在后，用 `output.channel(c).row(y)[x]` 写入。

#### 4.2.3 源码精读

输出张量形状与缩放系数（注意 `scale = 原图 / 目标`，所以反推时 `src = x * scale`）：

[src/ncnn_embedding.cpp:239-243](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L239-L243) 创建 `image_size_×image_size_×3` 的平面 Mat，并算出 x/y 方向缩放比。

4 邻接像素的索引与双线性加权（与上面公式一一对应）：

[src/ncnn_embedding.cpp:250-273](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L250-L273) 取 `x0,y0,x1,y1` 四个整数邻接点，用 `(1-dx)(1-dy)` 等四个权重做加权平均，边界处用 `std::min(..., width-1)` 钳制防止越界。

逐通道归一化并写入平面布局：

[src/ncnn_embedding.cpp:275-278](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L275-L278) `(v/255 - image_mean_[c]) / image_std_[c]`，写入 `output.channel(c).row(y)[x]`。

三个归一化参数都是可配置成员，默认值就是 ImageNet RGB 统计量：

[src/ncnn_embedding.h:33-35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L33-L35) `image_size_=224`、`image_mean_`、`image_std_` 的默认声明；构造函数里若 `setting` 含 `image_mean`/`image_std`/`image_size` 则覆盖默认值（见 [src/ncnn_embedding.cpp:134-153](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L134-L153)）。

> **通道顺序提醒**：`preprocess_image` 假设输入 `image_data` 是 **RGB 交错（interleaved）** 排布（`image_data[i*3+0]` 是 R）。这与构造函数里 RGB 顺序的 `image_mean_`/`image_std_` 一致。它和 VLM 单元（u5）里 `bgr_to_pixel_values` 用的 BGR 约定不同——这正是 4.3 节 `encode_image_file` 里要做一次通道翻转的原因。

#### 4.2.4 代码实践

**实践目标**：直观感受「归一化数值」对最终向量的影响。

**操作步骤（源码阅读 + 参数观察型）**：

1. 在 [src/ncnn_embedding.cpp:275](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L275) 临时加一行日志，打印第一个输出像素归一化后的三通道值：`fprintf(stderr, "pixel(0,0) R=%.3f G=%.3f B=%.3f\n", output.channel(0).row(0)[0], output.channel(1).row(0)[0], output.channel(2).row(0)[0]);`（仅用于观察，验证后删掉，不要提交）。
2. 用一张纯白图（像素全 255）和一张纯黑图（像素全 0）分别走 `encode_image`，观察打印值。
3. **待本地验证**：纯白像素应当得到 `v' = (1.0 - μ_c)/σ_c`（R 通道约 `(1-0.485)/0.229 ≈ 2.249`）；纯黑像素得到 `-μ_c/σ_c`（R 通道约 `-2.117`）。

**预期结果**：你能用计算器手算出上述值，并和日志对上，从而确认归一化公式确实生效。

#### 4.2.5 小练习与答案

**练习 1**：双线性插值里，为什么 `x1 = std::min(x0 + 1, width - 1)` 而不是直接 `x0 + 1`？

**参考答案**：当 `src_x` 落在原图最右一列时，`x0` 已经是 `width-1`，`x0+1` 会越界。用 `std::min` 钳到 `width-1`，相当于在最右列做「边界复制（edge replication）」——把越界的邻居当成边缘像素本身，避免数组越界访问。

**练习 2**：如果把 `image_mean_` 和 `image_std_` 都改成 0 和 1（即不做均值标准差归一化，只除 255），图像向量会变成什么样？

**参考答案**：归一化后的输入会从「零均值、单位方差」变成「0~1 范围」。视觉编码器是用带 ImageNet 归一化的数据训练的，输入分布偏移会导致网络各层激活值偏离训练分布，最终图像向量与文本向量的对齐关系被破坏，余弦相似度变得不可靠（检索准确率明显下降）。这正说明归一化数值必须与训练时严格一致。

---

### 4.3 图像编码 API：encode_image 与 encode_image_file

#### 4.3.1 概念说明

图像编码 API 有两层，分工清晰：

- **`encode_image(image_data, w, h, channels)`**：核心层。输入已经是「内存里的原始像素字节」，负责预处理 + 视觉网络前向 + 归一化，返回图像向量。任何拿到像素数据的调用方（比如从摄像头、解码库）都能直接用。
- **`encode_image_file(image_path)`**：便捷层。输入是一个图片文件路径，内部用 `load_image_to_ncnn_mat` 解码磁盘文件，再转交给 `encode_image`。

两者都带「`!ok_ || !vision_encoder_net`」的前置守卫——对象不可用或不支持图像时，安全地返回空向量，而不是崩溃。

#### 4.3.2 核心流程

`encode_image` 的链路：

```text
原始像素字节 (RGB 交错)
      |
      v
preprocess_image  ──► pixel_values (224×224×3 平面, 已归一化)
      |
      v
vision_encoder_net:  input("in0", pixel_values) → extract("out0", embedding)
      |
      v
normalize(embedding)  ──► 单位长度图像向量 (vision_embed_dim_ 维)
```

`encode_image_file` 在最前面多两步：磁盘解码 + 通道顺序纠正：

```text
图片路径
   |
   v
load_image_to_ncnn_mat  ──► BGR 交错 Mat (来自 stb_image, 经 RGB→BGR 翻转)
   |
   v
手工翻转 BGR→RGB  ──► RGB 交错字节流
   |
   v
encode_image(...)   (衔接上面的核心链路)
```

注意视觉网络的输出 `out0` **已经是单个 pooled 向量**（在导出脚本里就把注意力汇聚/CLS 投影 baked 进了网络），所以 C++ 侧**不做 mean_pool**，只 `normalize`。这与 CLIP 文本侧的处理对称（见 4.4.2）。

#### 4.3.3 源码精读

`encode_image` 的守卫与核心三步：

[src/ncnn_embedding.cpp:356-368](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L356-L368) 先判 `!vision_encoder_net`，再 `preprocess_image`，再用标准 `in0→out0` 模式跑视觉编码器。

只归一化、不 mean_pool，拷出 `vision_embed_dim_` 维向量：

[src/ncnn_embedding.cpp:375-380](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L375-L380) `normalize(embedding)` 后 `memcpy` 到 `std::vector<float>`。

`encode_image_file` 的解码与通道翻转——`load_image_to_ncnn_mat` 返回 **BGR 交错**（它在 stb 解码后又做了一次 RGB→BGR，见 [src/utils/image_utils.cpp:19-23](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.cpp#L19-L23)），所以这里要把 BGR 再翻回 RGB 以匹配 `preprocess_image` 的 RGB 约定：

[src/ncnn_embedding.cpp:388-404](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L388-L404) 加载、判空、按 `i*3+0 ← src[i*3+2]`（BGR→RGB）重组字节，最后调用 `encode_image`。

两个 API 的声明与守卫语义一致：

[src/ncnn_embedding.h:46-47](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L46-L47) `encode_image` / `encode_image_file` 的签名；`vision_embed_dim_` 成员见 [src/ncnn_embedding.h:29](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.h#L29)，默认 0，由 `setting.vision_embed_dim` 配置（[src/ncnn_embedding.cpp:131-133](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L131-L133)）。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `encode_image_file` 调用，亲手验证通道翻转的正确性。

**操作步骤（源码阅读 + 推理型）**：

1. 读 [src/utils/image_utils.cpp:9-27](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.cpp#L9-L27)：`stbi_load(..., 3)` 得到 RGB，循环里 `dst[0]=data[2]; dst[2]=data[0]` 把它变成 BGR 返回。
2. 读 [src/ncnn_embedding.cpp:399-401](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L399-L401)：`image_data[0]=src[2]; image_data[2]=src[0]`。因为 `src` 此时是 BGR（`src[0]=B`），所以 `image_data[0]=R`、`image_data[2]=B`，即 RGB。
3. **结论**：两次翻转互相抵消，`encode_image_file` 最终送给 `preprocess_image` 的是「与磁盘文件一致的 RGB」，这正是 `image_mean_`（RGB 顺序）所期待的。

**预期结果**：你能用一句话讲清「为什么 `encode_image_file` 里要做一次看似多余的通道翻转」——因为底层的 `load_image_to_ncnn_mat` 为了迎合 VLM 管线统一用了 BGR，而 CLIP 的预处理是 RGB 约定，必须在便捷层把顺序纠正回来。

#### 4.3.5 小练习与答案

**练习 1**：如果调用方手里已经有一块 RGB 交错的像素 buffer（比如来自某个解码库），应该用 `encode_image` 还是 `encode_image_file`？需要注意什么？

**参考答案**：应该用 `encode_image`，直接传 buffer、宽、高、通道数。需要注意：buffer 必须是 **RGB 交错**（`pixel[i*3+0]=R`），否则 `preprocess_image` 会按错误的通道套用均值/标准差，向量失真。如果手头是 BGR，调用方要自己先翻成 RGB。

**练习 2**：`encode_image` 末尾为什么是 `normalize(embedding)` 而不是 `normalize(mean_pool(embedding))`（像非 CLIP 文本侧那样）？

**参考答案**：视觉编码器网络的 `out0` 在导出时就已经把「空间汇聚（如注意力 pooling 或 CLS token）+ 投影到共享空间」做进网络结构了，输出是单个向量而非序列。所以 C++ 侧无需再 `mean_pool`，直接归一化即可。文本侧的 CLIP 分支也是同理（[src/ncnn_embedding.cpp:334-335](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L334-L335)），两侧对称。

---

### 4.4 图文对齐与余弦相似度检索

#### 4.4.1 概念说明

CLIP 的核心价值在于：**文本向量和图像向量被映射到了同一个共享空间**，因此它们可以直接比相似度。这是 CLIP 训练（对比学习）的直接产物——训练时就在拉近匹配图文对、推开不匹配的对。

余弦相似度衡量两个向量的夹角：

\[
\text{cos}(a, b) = \frac{a \cdot b}{\|a\| \|b\|}
\]

取值范围 \([-1, 1]\)，越接近 1 越相似。因为 `encode_text` 和 `encode_image` 返回前都做了 `normalize`，两个向量都已在单位球面上（\(\|a\|=\|b\|=1\)），所以余弦相似度退化为简单的点积：

\[
\text{cos}(a, b) = a \cdot b = \sum_i a_i b_i
\]

这就是为什么本项目的相似度计算如此简洁。**前提条件**：文本向量维度 `text_embed_dim_` 必须等于图像向量维度 `vision_embed_dim_`（即共享空间维度），否则点积无法逐元素相加。对合法的 CLIP 模型，这两个维度天然相等。

#### 4.4.2 核心流程

「以文搜图」检索的标准做法：

```text
1. 预编码 N 条候选文本 → N 个文本向量
2. 编码 1 张（或多张）图 → 图像向量
3. 对每条文本 i 与图像 j，算 cos(text_i, image_j)
4. 对图像 j，取相似度最大的文本 i 作为「最匹配描述」
```

注意 CLIP 文本侧的后处理（u6-l1 已提及，此处再确认）：CLIP **跳过 mean_pool**，直接 `normalize(embeddings)`，因为 CLIP 用一个特殊 token（如 EOS/CLS）的位置向量代表整句：

[src/ncnn_embedding.cpp:334-339](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L334-L339) `model_type_=="clip"` 走 `normalize(embeddings)`，其余模型走 `normalize(mean_pool(...))`。

#### 4.4.3 源码精读

`clip_main.cpp` 里的余弦相似度实现（与 u6-l1 的 `embedding_main` 几乎一致，多了一个 `1e-10f` 防除零）：

[examples/clip_main.cpp:7-15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/clip_main.cpp#L7-L15) 计算 \(a \cdot b / (\|a\|\|b\| + \epsilon)\)。

主流程：先批量编码文本，再用 `supports_image()` 守卫编码图像，最后打印图文相似度矩阵：

[examples/clip_main.cpp:66-87](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/clip_main.cpp#L66-L87) 文本循环编码；图像分支被 `!image_path.empty() && embed.supports_image()` 双重守卫；矩阵打印交给 `print_similarity_matrix`。

矩阵打印函数——每行一条文本，每列一张图，单元格是相似度：

[examples/clip_main.cpp:17-36](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/clip_main.cpp#L17-L36) 双重循环调用 `cosine_similarity(text_embeds[i], image_embeds[j])`，按 `%-20.4f` 对齐输出。

> 主程序里 `supports_image()` 的真正价值在这里体现：第 77 行用它决定要不要尝试编码图像。即便用户传了图片路径，若模型是纯文本嵌入模型，也会被静默跳过图像分支、只打印文本向量维度，不会因为调用 `encode_image_file` 而得到一堆空向量。

#### 4.4.4 代码实践

**实践目标**：用 `clip_main` 跑一次完整图文检索，找出与一张图最匹配的文本描述。

**操作步骤**：

1. 准备一个 CLIP 模型目录（如 `assets/jina_clip_v2`，含 `model.json`、文本/视觉编码器 `.param/.bin`、词表）。模型权重需自行下载，仓库不带权重。
2. 准备一张测试图（比如一张猫的图片 `cat.jpg`）。
3. 构建并运行：
   ```bash
   xmake build clip_main
   xmake run clip_main assets/jina_clip_v2 cat.jpg
   ```
4. 观察输出的 `Text-Image Similarity Matrix`：每行是一条候选文本（`"a cat"`、`"a dog"`、`"a car"`、`"一只猫"` 等），唯一的列是你的图片，单元格是相似度。

**需要观察的现象**：

- 若图片确实是猫，`"a cat"` / `"一只猫"` 两行的相似度应明显高于 `"a dog"` / `"a car"`。
- 中英文描述（`"a cat"` 与 `"一只猫"`）的相似度应当接近——这正是 CLIP 跨语言对齐的体现。

**预期结果**：相似度最高（最接近 1）的那条文本，就是模型对这张图的「最佳描述」。**待本地验证**（受具体模型权重与图片内容影响）。

**进阶变体**：把 `clip_main.cpp` 里第 56-64 行的 `texts` 列表改成你自己的描述（比如加一条 `"a fluffy kitten on a sofa"`），重新构建运行，观察最高相似度文本是否随候选集变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CLIP 文本侧和图像侧都**必须**做 `normalize`，才能用点积代替余弦相似度？

**参考答案**：余弦相似度是 \(\frac{a \cdot b}{\|a\|\|b\|}\)。只有当 \(\|a\|=\|b\|=1\)（即归一化后）时，它才等于点积 \(a \cdot b\)。CLIP 的训练目标本身就是在归一化后的单位球面上优化角度，所以推理时两侧都要归一化，既保证语义一致，又把相似度计算简化成一次点积。

**练习 2**：如果一个 CLIP 模型的 `text_embed_dim_` 是 768、`vision_embed_dim_` 是 1024（不一致），`cosine_similarity` 会发生什么？

**参考答案**：`cosine_similarity` 内部循环用 `a.size()` 作为长度（[examples/clip_main.cpp:9](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/clip_main.cpp#L9)）。若两个向量长度不同，要么越界读、要么只比了前 768 维，结果完全错误。正常的 CLIP 模型在共享空间里两个维度必然相等——维度不一致说明模型配置或导出有问题，应当排查 `setting.embed_dim` / `text_embed_dim` / `vision_embed_dim` 的取值。

**练习 3**：CLIP 的「以文搜图」和 u5 单元 VLM 的「看图回答」相比，各适合什么场景？

**参考答案**：CLIP 适合**检索/分类/打标签**——候选描述是有限集合，比相似度即可，速度快、无需自回归生成。VLM 适合**开放式问答/描述生成**——需要模型自由产出自然语言。CLIP 不能「说出」图里有什么，只能告诉你「这张图更像哪句话」；VLM 则相反。两者互补，常组合使用（CLIP 先召回，VLM 再细看）。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个**迷你图文检索器**（源码阅读 + 改造型）：

1. **能力探测（4.1）**：读 `clip_main.cpp`，确认它用 `embed.supports_image()` 守卫图像分支。在你的笔记本里写下：如果换成 `embedding_main`（u6-l1）的纯文本模型，这一守卫会怎样阻止图像编码。
2. **预处理理解（4.2）**：手算一张纯红图（R=255, G=0, B=0）经 `preprocess_image` 后第一个像素的三通道值（用默认 ImageNet mean/std），写出计算过程。
3. **API 串联（4.3）**：仿照 `encode_image_file`，在 `clip_main.cpp` 里新增一个分支——用 `encode_image`（而非 `encode_image_file`）直接编码一段你手造的 RGB 像素 buffer（例如 224×224 全红），打印返回向量的维度与是否已归一化（模长应接近 1）。
4. **检索（4.4）**：把候选文本扩到 5 条以上（含中英文），跑 `clip_main` 对一张你选的图，记录相似度矩阵，并指出最高相似度文本。

完成后，你应当能独立解释「从磁盘图片到最匹配文本描述」的完整数据流，并清楚每一步在哪行源码里发生。

## 6. 本讲小结

- `supports_image()` 的实现就是 `vision_encoder_net != nullptr`，而该网络只在 `model_type_ == "clip"` 时创建与加载——这是整条图像链路的总开关。
- `preprocess_image` 做两件事：双线性缩放到 `image_size_`（默认 224）、逐通道 `(v/255 - mean)/std` 归一化（默认 ImageNet RGB 统计量），输出是 ncnn 平面布局。
- `encode_image` 是核心层（像素字节→向量），`encode_image_file` 是便捷层（路径→解码→翻转通道→`encode_image`）；翻转是因为 `load_image_to_ncnn_mat` 给的是 BGR，而 CLIP 预处理要 RGB。
- 视觉网络 `out0` 已是 pooled 向量，故 C++ 侧只 `normalize`、不 `mean_pool`，与 CLIP 文本侧对称。
- 因两侧都归一化，图文余弦相似度退化为点积；前提是 `text_embed_dim_ == vision_embed_dim_`（共享空间维度一致）。
- CLIP（对比学习、双编码器、比相似度）与 u5 的 VLM（图像注入 token 流、自回归生成）是两条本质不同的图文路线，不可混淆。

## 7. 下一步学习建议

- **继续模态之旅**：下一篇 [u6-l3 GLM-OCR 与 HunyuanOCR 推理](u6-l3-ocr-runtime.md) 讲 OCR——它和 CLIP 一样复用视觉前处理，但走的是「图像 prefill + 共享文本解码」的 VLM 路线，正好用来对比本讲的对比学习路线。
- **回顾对齐思想**：若想深入理解「为什么归一化后点积等于余弦相似度」，可重读 [u6-l1](u6-l1-text-embedding.md) 的 mean_pool/normalize 一节与 [u2-l1](u2-l1-base-class-common.md) 的基类工具函数。
- **扩展实践**：尝试把 `clip_main` 改造成「以图搜文」——给定一张图，从一个大文本库里召回 top-K 最相似描述；这是 CLIP 在实际工程里最常见的用法。
