# 图像预处理与 patch 切分

## 1. 本讲目标

上一讲（u5-l1）我们看清了：构造函数在 `vision_type != close` 时会加载 `vision_embed_patch`、`vision_encoder` 等视觉网络，并读取 `patch_size`、`patch_dim`、`max_num_patches`、`spatial_merge_size` 四个配置。但「一张磁盘上的 jpg/png 是如何变成可以喂进这些网络的张量」这一步被留成了黑盒。

本讲就打开这个黑盒。读完本讲你应当能够：

- 说清楚 `load_image_to_ncnn_mat` 如何把图片文件读成 `ncnn::Mat`（BGR 交错、u8）。
- 手算 `get_image_size_for_patches`：给定原图尺寸和 `patch_size`/`max_num_patches`，推出目标缩放尺寸与 patch 网格（行×列）。
- 解释 `bgr_to_pixel_values` 如何把整图切成一个个 patch、做归一化、排成「每行一个 patch」的矩阵。
- 说明 `reorder_patches_for_merge` 在 `spatial_merge_size=2` 时为什么要重排、以及重排的具体规则。

本讲只讲**预处理与切分**，不涉及视觉网络的前向（patch embed / vision encoder），那部分留给 u5-l3。

## 2. 前置知识

- **patch（图像块）**：视觉 Transformer（ViT）不直接吃整张图，而是把图切成一个个固定大小的小方块（例如 14×14 像素），每个方块叫一个 patch，后续每个 patch 当成一个「图像 token」。
- **patch 网格**：把图切成 patch 后，会形成一个「行×列」的网格，例如 16 行 × 24 列，共 384 个 patch。
- **spatial merge（空间合并）**：Qwen-VL 系列在 ViT 之后有一个「合并器」，会把相邻的 2×2 个 patch token 融合成 1 个 token，把序列长度压缩到 1/4。本讲讲的「重排」就是为了让这次合并更高效。
- **ncnn::Mat**：ncnn 的张量类型。本讲会用到它的几个维度：`mat.w`（宽）、`mat.h`（高）、`mat.c`（通道）、`mat.row(i)`（取第 i 行指针）。详细用法可回顾 u2-l2。
- **BGR vs RGB**：OpenCV/ncnn 传统上用 BGR 通道顺序；CLIP 等模型训练时用 RGB。本讲会看到一次「读入是 BGR、喂网络前转成 RGB」的转换。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/image_utils.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.h) | 声明跨模态共用的图像工具：加载、缩放、判空。 |
| [src/utils/image_utils.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.cpp) | 上述工具的实现，底层用 `stb_image` 与 ncnn 自带的 resize。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 本讲四个核心函数（`get_scaled_image_size`/`get_image_size_for_patches`/`bgr_to_pixel_values`/`reorder_patches_for_merge`）都是 `ncnn_llm_gpt` 的私有成员，集中在「Vision Helper Implementations」一节。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 声明这四个私有方法与 `patch_size` 等成员。 |
| [examples/llm_ncnn_run/main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp) | 主入口里 `load_image_to_ncnn_mat` 的实际调用点，展示「图片如何进入程序」。 |

> 提醒：本讲的三个核心函数（`get_image_size_for_patches` 等）在 [src/utils/image_utils.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.h) 里**没有**声明——它们是 `ncnn_llm_gpt` 类的私有成员，定义在 [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) 里。`image_utils.h` 只放与类无关的通用工具（加载/缩放/判空）。不要找错文件。

---

## 4. 核心概念与源码讲解

### 4.1 load_image_to_ncnn_mat：把图片文件读成 BGR 张量

#### 4.1.1 概念说明

模型不能直接读 jpg/png 文件，需要先把文件解码成内存里的像素数组。这一步是整个视觉链路的「入口」，必须：

- 解码任意常见格式（jpg/png/bmp/…）。
- 输出一个统一的、后续代码好处理的 `ncnn::Mat`。
- 失败时给一个可被上层识别的「空 Mat」。

本项目用业界常见的单头库 **stb_image** 来解码，避免引入 OpenCV 等重依赖。

#### 4.1.2 核心流程

1. 调用 `stbi_load(path, &w, &h, &c, 3)`，第 5 个参数 `3` 表示「无论原图几通道，强制输出 3 通道」。返回值 `data` 是 RGB 交错的 u8 数组。
2. 若 `data` 为空（文件不存在/格式不支持），返回默认构造的空 `ncnn::Mat()`，上层用 `ncnn_mat_empty` 判空。
3. 否则新建一个 `ncnn::Mat(w, h, 3, 1u)`（宽 w、高 h、3 通道、每元素 1 字节即 u8），逐像素把 **RGB 换成 BGR** 拷进去。
4. `stbi_image_free(data)` 释放 stb 的内存，返回 BGR 张量。

> 注意：这一步**只做解码 + RGB→BGR**，不做缩放、不做归一化。缩放和归一化留给后续步骤，因为目标尺寸要先由 `get_image_size_for_patches` 算出来。

#### 4.1.3 源码精读

声明在工具头里，是个与类无关的自由函数：

[src/utils/image_utils.h:6-6](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.h#L6-L6) —— 声明 `load_image_to_ncnn_mat`，入参是图片路径，返回 `ncnn::Mat`。

[src/utils/image_utils.cpp:9-27](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.cpp#L9-L27) —— 核心实现。关键两句：

```cpp
// 强制转成 3 通道 RGB
unsigned char* data = stbi_load(image_path.c_str(), &w, &h, &c, 3);
if (!data) return ncnn::Mat();          // 失败 → 空 Mat
ncnn::Mat bgr(w, h, 3, (size_t)1u);     // 1u = 每元素 1 字节
// RGB → BGR：dst[0]=data[2], dst[1]=data[1], dst[2]=data[0]
```

逐像素的 `dst[i*3+0] = data[i*3+2]` 就是把 R 放到第 3 位、B 放到第 1 位，完成 RGB→BGR。

实际调用点在主入口，展示了「图片如何进入程序」并由 `ncnn_mat_empty` 做失败兜底：

[examples/llm_ncnn_run/main.cpp:47-56](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L47-L56) —— 当命令行带 `--image` 时加载图片；加载失败（`ncnn_mat_empty`）则报错退出，否则把 `image` 透传给 `run_cli`。

#### 4.1.4 代码实践

**实践目标**：验证 `load_image_to_ncnn_mat` 的「失败返回空 Mat」与「成功返回 BGR」两条路径。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 准备一张小图（例如 2×2 像素的 png）放在 `assets/` 下，命名为 `tiny.png`。
2. 阅读上面的源码片段，先**手算**：对一个 2×2 图，`w*h=4`，循环 4 次，每次写 3 字节，共 12 字节。
3. （可选，待本地验证）在 `examples/` 下仿照 `ocr_main` 写一个最小 main，调用 `load_image_to_ncnn_mat("tiny.png")`，打印 `mat.w`/`mat.h`/`mat.c`，再故意传一个不存在的路径，确认返回的 `ncnn_mat_empty(mat)` 为 `true`。

**预期结果**：

- 成功时 `mat.w==2, mat.h==2`，通道数为 3，像素总数 4。
- 失败时 `ncnn_mat_empty` 返回 `true`（见 [src/utils/image_utils.cpp:82-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/image_utils.cpp#L82-L84) 的判空逻辑：`mat.empty() || w<=0 || h<=0`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `stbi_load` 第 5 个参数传 `3`，而不是用原图通道数 `c`？

> **答**：传 `3` 是要求 stb_image **强制输出 3 通道**，这样无论原图是灰度（1 通道）、PNG 带 alpha（4 通道）还是普通 jpg（3 通道），返回的都是统一的 3 通道 RGB，后续逐像素拷贝的 `i*3` 索引才永远成立。`c` 只是 stb 回填的「原图通道数」，供参考。

**练习 2**：函数里为什么把 RGB 换成 BGR？

> **答**：ncnn/OpenCV 生态习惯用 BGR 通道顺序作为内存表示。虽然这里换成 BGR，但后续 `bgr_to_pixel_values`（见 4.3）会再把它转回 RGB 并归一化，以匹配 CLIP/ViT 训练时的输入约定。本步只是把「内存表示」统一成项目惯例。

---

### 4.2 get_image_size_for_patches：算出目标缩放尺寸与 patch 网格

#### 4.2.1 概念说明

图片大小千变万化，但 ViT 的 patch 切分要求「整图能被 `patch_size` 整除」，且 patch 总数不能超过 `max_num_patches`（否则显存/计算量爆炸，也超出模型训练时见过的长度）。

所以需要一个算法：在「尽量保持原图比例」的前提下，把图缩放到一个合适的尺寸，使得

- 宽高都是 `patch_size` 的整数倍（实际更强：是 `patch_size*2` 的整数倍，原因见下）。
- patch 总数 `≤ max_num_patches`。

这就是 `get_image_size_for_patches` 的职责。

#### 4.2.2 核心流程

设 `ps = patch_size`，`eps = patch_size * 2`（effective patch size）。对原图某一边长 `size`、缩放系数 `scale`，目标边长为：

\[
\text{target} = \max\!\left(\text{eps},\;\ \text{eps} \cdot \left\lceil \frac{\text{size}\cdot\text{scale}}{\text{eps}} \right\rceil\right)
\]

即「先把原图边长按 `scale` 缩放，再向上取整到 `eps` 的整数倍，且不小于一个 patch 块」。

然后用 patch 总数约束迭代缩小 `scale`：

\[
N_p = \frac{\text{target\_h}}{\text{ps}} \cdot \frac{\text{target\_w}}{\text{ps}}, \quad
\text{若}\ N_p > \text{max\_num\_patches}\ \text{则}\ \text{scale} \mathrel{-}= 0.02\ \text{再试}
\]

伪代码：

```text
scale = 1.0
eps = patch_size * 2
loop:
    target_h = ceil(size_h * scale / eps) * eps
    target_w = ceil(size_w * scale / eps) * eps
    N = (target_h/ps) * (target_w/ps)
    if N > max_num_patches:
        scale -= 0.02          # 还太大，整体缩小一点
    else:
        break                  # 找到了
return target_h, target_w
```

**两个关键设计**：

1. **为什么对齐到 `patch_size*2` 而不是 `patch_size`？** 因为 `spatial_merge_size=2`，ViT 之后的合并器要把相邻 2×2 patch 合并。若宽、高的 patch 数不全是偶数，就无法整齐地切成 2×2 块。对齐到 `patch_size*2` 等价于「保证 patch 行数、列数都是偶数」。
2. **为什么步长 0.02？** 一个经验性的小步长，让 `scale` 缓慢下降，避免一次跳太多导致 patch 数骤减、信息损失过大。

#### 4.2.3 源码精读

成员与默认值（被构造函数从 model.json 覆盖）：

[src/ncnn_llm_gpt.h:129-132](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L129-L132) —— `patch_size=14`、`patch_dim=1280`、`max_num_patches=49152`、`spatial_merge_size=2`。这些默认值在 [src/ncnn_llm_gpt.cpp:228-232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L228-L232) 被 `vision_cfg["..."].get<int>()` 覆盖（回顾 u5-l1）。

私有方法声明：

[src/ncnn_llm_gpt.h:191-192](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L191-L192) —— 声明 `get_scaled_image_size` 与 `get_image_size_for_patches`，都是 `const` 私有方法。

单边缩放函数：

[src/ncnn_llm_gpt.cpp:999-1003](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L999-L1003) —— 即上面的取整公式：

```cpp
float scaled_size_f = (float)size * scale;
int scaled_size = (int)(std::ceil(scaled_size_f / (float)effective_patch_size) * effective_patch_size);
return std::max(effective_patch_size, scaled_size);
```

主迭代：

[src/ncnn_llm_gpt.cpp:1005-1018](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1005-L1018) —— `scale` 从 1.0 起，每次不满足就 `-= 0.02`，注意 `num_patches` 用 `long long` 防止大图相乘溢出：

```cpp
int effective_patch_size = patch_size * 2;
while (true) {
    target_height = get_scaled_image_size(scale, image_height, effective_patch_size);
    target_width  = get_scaled_image_size(scale, image_width,  effective_patch_size);
    long long num_patches = ((long long)target_height / patch_size)
                          * ((long long)target_width  / patch_size);
    if (num_patches > max_num_patches) scale -= 0.02f;
    else break;
}
```

#### 4.2.4 代码实践

**实践目标**：给定一张图与一组配置，手算出最终 patch 网格。

**操作步骤**：取 `patch_size=14`、`max_num_patches=2560`（Qwen2.5-VL 常见值），原图 `960×700`。按上面伪代码手算（或用下面的示例脚本）。

**示例代码**（非项目代码，仅用于复现网格计算）：

```python
import math
patch_size = 14
max_num_patches = 2560
ih, iw = 700, 960   # 注意：高在前

def scaled(scale, size, eps):
    s = math.ceil(size * scale / eps) * eps
    return max(eps, s)

eps = patch_size * 2   # 28
scale = 1.0
while True:
    th = scaled(scale, ih, eps)
    tw = scaled(scale, iw, eps)
    n = (th // patch_size) * (tw // patch_size)
    if n > max_num_patches:
        scale -= 0.02
    else:
        break
print(f"target_h={th} target_w={tw} "
      f"grid={th//patch_size}x{tw//patch_size} patches={n} scale={scale:.2f}")
```

**需要观察的现象**：

- `target_h`、`target_w` 是否都是 28 的倍数。
- patch 行数、列数是否都是偶数（这是 `eps=patch_size*2` 的直接结果）。
- patch 总数是否 `≤ max_num_patches`。

**预期结果**（待本地验证，取决于具体尺寸）：对 `700×960`、`max_num_patches=2560`，缩放后应得到一个 `28` 的倍数尺寸，例如 `target_h≈700` 附近、`target_w≈980` 附近，patch 网格行数列数均为偶数。

#### 4.2.5 小练习与答案

**练习 1**：若 `max_num_patches` 设得特别大，循环第一次就满足条件，`scale` 会是多少？图片会被放大吗？

> **答**：`scale` 保持 `1.0`。但「不放大」——`get_scaled_image_size` 只是向上取整到 `eps` 的倍数，可能让边长略增（例如 700→700 取整不变；701→728），并不会按 `scale>1` 放大。所以 `scale` 只用于「缩小以控制 patch 数」，不用于放大。

**练习 2**：为什么 `num_patches` 要用 `long long`？

> **答**：两张大图的 `target_h * target_w` 可能超过 32 位 `int` 上限（约 21 亿）。例如 `target_h=target_w=65536` 时乘积约 4.3×10⁹，已溢出。用 `long long`（在 [src/ncnn_llm_gpt.cpp:1011](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1011-L1011)）避免溢出导致比较失真、死循环。

---

### 4.3 bgr_to_pixel_values：切 patch、转 RGB、归一化

#### 4.3.1 概念说明

`get_image_size_for_patches` 算出目标尺寸后，整图会被 `ncnn_mat_resize` 缩放到 `target_w × target_h`。但 ViT 的 patch embed 网络要的不是整图，而是「每个 patch 摊平成一条向量」的矩阵，并且数值要归一化到模型训练时用的分布。

`bgr_to_pixel_values` 一次性完成三件事：

1. 把整图按 `patch_size` 切成 `num_patches_h × num_patches_w` 个 patch。
2. 每个 patch 摊平成 `patch_size*patch_size*3` 维向量，内部按 **R 平面 / G 平面 / B 平面** 排列（即 planar RGB）。
3. 对每个像素做 `(x/255 - mean)/std` 归一化。

#### 4.3.2 核心流程

- patch 网格：`num_patches_h = ceil(img_h/ps)`、`num_patches_w = ceil(img_w/ps)`。因为前面已对齐到 `ps*2`，这里其实是整除，`ceil` 只是防御。
- 输出张量形状：`ncnn::Mat(embed_dim, num_patches)`，其中 `embed_dim = ps*ps*3`。即「每一行 = 一个 patch 的摊平向量」，共 `num_patches` 行。
- 遍历每个 patch `p`：
  - 算出它在网格里的行列 `(ph, pw)` 与左上角像素 `(start_y, start_x)`。
  - 输出指针拆三段：`ptr_r`（R 段）、`ptr_g`（G 段）、`ptr_b`（B 段），每段 `ps*ps`。
  - 遍历 patch 内 `(y, x)`：读 BGR 像素，取 `pixel[2]` 当 R、`pixel[1]` 当 G、`pixel[0]` 当 B，归一化后写入对应段。越界位置补 0。
- 归一化常数：默认 CLIP 的 `mean=(0.48145466, 0.4578275, 0.40821073)`、`std=(0.26862954, 0.26130258, 0.27577711)`；`VISION_QWEN3_5_VL` 用 `0.5/0.5`，且除以 `255.5` 而非 `255`。

> 注意两次「换通道」：`load_image_to_ncnn_mat` 把 RGB 转成 BGR 存内存；`bgr_to_pixel_values` 又把 BGR 转回 RGB 喂网络。两次相反的操作保证了「网络看到的 = 训练时的 RGB」。

#### 4.3.3 源码精读

[src/ncnn_llm_gpt.h:193-193](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L193-L193) —— 声明 `bgr_to_pixel_values`。

[src/ncnn_llm_gpt.cpp:1020-1082](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1020-L1082) —— 完整实现。要点：

```cpp
float image_mean[3] = {0.48145466f, 0.4578275f, 0.40821073f};  // CLIP 默认
float image_std[3]  = {0.26862954f, 0.26130258f, 0.27577711f};
if (vision_type == VISION_QWEN3_5_VL) { /* 改成 0.5/0.5 */ }

int num_patches_h = (img_h + patch_size - 1) / patch_size;     // ceil
int num_patches_w = (img_w + patch_size - 1) / patch_size;
int embed_dim = patch_size * patch_size * 3;
ncnn::Mat pixel_values(embed_dim, num_patches);                // 每行一个 patch
```

每个 patch 内，输出按 R/G/B 三段排列：

```cpp
float* ptr_r = out_ptr;                                  // [0, ps*ps)
float* ptr_g = out_ptr + patch_size * patch_size;        // [ps*ps, 2*ps*ps)
float* ptr_b = out_ptr + patch_size * patch_size * 2;    // [2*ps*ps, 3*ps*ps)
```

像素归一化（注意 `pixel[2]` 是 R，因为内存是 BGR）：

```cpp
*ptr_r++ = (pixel[2] / 255.f - image_mean[0]) / image_std[0];
*ptr_g++ = (pixel[1] / 255.f - image_mean[1]) / image_std[1];
*ptr_b++ = (pixel[0] / 255.f - image_mean[2]) / image_std[2];
```

越界补 0 的分支（`cur_img_y >= img_h` 或 `cur_img_x >= img_w`）保证即使尺寸不是 `ps` 整数倍（理论上不会发生）也不会越界读。

#### 4.3.4 代码实践

**实践目标**：确认「输出每行一个 patch、维度 `ps*ps*3`、planar RGB」这一布局。

**操作步骤**（源码阅读型）：

1. 在 [src/ncnn_llm_gpt.cpp:1037](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1037-L1037) 处，确认输出 Mat 形状是 `(embed_dim, num_patches)`，即 `w=embed_dim`、`h=num_patches`。
2. 设 `patch_size=14`，算出 `embed_dim = 14*14*3 = 588`。
3. 跟踪一个 patch 的写入：对一个 14×14 的 patch，R 段写 196 个 float、G 段 196 个、B 段 196 个，共 588，与 `embed_dim` 一致。

**预期结果**：`pixel_values.h == num_patches`、`pixel_values.w == 588`（ps=14 时），每行正好是一个 patch 的 planar RGB 归一化向量。

**待本地验证**：可选地，写一段调试代码打印 `pixel_values.row(0)` 的前几个值，确认是归一化后的浮点（范围大致在 `[-2, 2]`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 patch 内部要按 R/G/B 分段（planar），而不是按像素交错（`R,G,B,R,G,B,...`）？

> **答**：因为后续 patch embed 网络（卷积）按通道平面处理更自然、也与模型导出时的权重布局一致。planar 布局让同一通道的像素在内存里连续，便于卷积核一次性读取一个通道的整块。

**练习 2**：`VISION_QWEN3_5_VL` 分支里除以 `255.5` 而不是 `255`，会有什么细微差别？

> **答**：`x/255.5` 比起 `x/255` 把范围从 `[0,1]` 变成约 `[0, 0.998]`，再配合 `mean=0.5,std=0.5`，使归一化后范围约 `[-0.999, 0.998]`，略微「收紧」。这是 Qwen3.5-VL 训练时的约定，推理时必须与训练一致，否则数值分布不匹配会显著掉点。

---

### 4.4 reorder_patches_for_merge：为 2×2 空间合并重排 patch

#### 4.4.1 概念说明

经过 4.3，我们得到 `num_patches` 个 patch 向量，按**光栅顺序**（从左到右、从上到下）排列。但 Qwen-VL 的视觉编码器之后有一个 **merger（合并器）**，会把相邻的 `spatial_merge_size × spatial_merge_size`（通常 2×2）个 patch 融合成 1 个 token，序列长度缩到 1/4。

为了让 merger 高效，需要让「属于同一个 2×2 块的 4 个 patch」在序列里**连续**。但光栅顺序下，同一列上下相邻的两个 patch 之间隔着 `w_patches` 个位置，并不连续。`reorder_patches_for_merge` 就是来「按块聚拢」的。

#### 4.4.2 核心流程

设网格 `h_patches × w_patches`，`merge_size=2`。

1. 把网格切成 `(h_patches/2) × (w_patches/2)` 个 2×2 块，记 `grid_h = h_patches/2`、`grid_w = w_patches/2`。
2. 按「先块后块内」的顺序重排：
   - 外层遍历每个块 `(gh, gw)`。
   - 内层遍历块内 4 个 patch `(mh, mw)`。
   - 把原 patch `(gh*2+mh, gw*2+mw)` 拷到输出序列的下一个位置。

用一个 4×4 网格（merge_size=2）举例，原光栅序号：

```text
原网格（光栅顺序）:
 0  1  2  3
 4  5  6  7
 8  9 10 11
12 13 14 15
```

重排后（每个 2×2 块连续）：

```text
块(0,0): 0,1,4,5     块(0,1): 2,3,6,7
块(1,0): 8,9,12,13    块(1,1): 10,11,14,15

输出序列 = [0,1,4,5, 2,3,6,7, 8,9,12,13, 10,11,14,15]
```

可见原本相隔 4 个位置的「0 和 4」现在紧挨在一起，正是 merger 所需。

#### 4.4.3 源码精读

[src/ncnn_llm_gpt.h:194-194](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L194-L194) —— 声明 `reorder_patches_for_merge`，`merge_size` 默认 2。

[src/ncnn_llm_gpt.cpp:1084-1113](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1084-L1113) —— 实现，核心是四层循环：

```cpp
int grid_h = h_patches / merge_size;
int grid_w = w_patches / merge_size;
ncnn::Mat reordered(feature_dim, num_patches, (size_t)4u);  // 4u = float(4字节)
int new_row_idx = 0;
for (int gh = 0; gh < grid_h; gh++)
  for (int gw = 0; gw < grid_w; gw++)
    for (int mh = 0; mh < merge_size; mh++)
      for (int mw = 0; mw < merge_size; mw++) {
        int original_row_idx = (gh*merge_size + mh) * w_patches
                             + (gw*merge_size + mw);
        memcpy(reordered.row(new_row_idx),
               pixel_values.row(original_row_idx),
               feature_dim * sizeof(float));
        new_row_idx++;
      }
```

- 输入与输出的 `num_patches`、`feature_dim` 都不变，只是**行的顺序**变了，所以用 `memcpy` 整行拷贝即可。
- 开头的 `if (num_patches != h_patches*w_patches) return ncnn::Mat();`（[src/ncnn_llm_gpt.cpp:1088](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1088-L1088)）是形状一致性校验，网格与 patch 数对不上时返回空。

它在主链路里被调用两次：一次重排像素 patch，一次重排位置编码（保证两者顺序一致）：

[src/ncnn_llm_gpt.cpp:1178-1179](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1178-L1179) —— 对 `pixel_values` 重排；[src/ncnn_llm_gpt.cpp:1217](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1217-L1217) —— 对位置编码 `pos_embeds` 重排。两处必须用同一套重排规则，否则 patch 与位置编码会对不上。

#### 4.4.4 代码实践

**实践目标**：手算一个 4×4 网格在 `merge_size=2` 下的重排结果，理解「块内连续」。

**操作步骤**：

1. 取 `h_patches=4, w_patches=4, merge_size=2`，按上面四层循环展开。
2. 列出每个 `new_row_idx` 对应的 `original_row_idx`。
3. 画出重排后序列，确认每个 2×2 块的 4 个 patch 连续。

**预期结果**：

| new_row_idx | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| original_row_idx | 0 | 1 | 4 | 5 | 2 | 3 | 6 | 7 | 8 | 9 | 12 | 13 | 10 | 11 | 14 | 15 |

即输出序列为 `[0,1,4,5, 2,3,6,7, 8,9,12,13, 10,11,14,15]`，每个 2×2 块的 4 个 patch 都连续。

**现象解释**：注意「0 和 4」原本在光栅序里相隔 4，重排后相邻——这正是 merger 需要的布局。如果不做这一步，merger 就得跨大跨度取数，既低效也与导出网络的预期不符。

#### 4.4.5 小练习与答案

**练习 1**：如果 `h_patches` 或 `w_patches` 是奇数会怎样？

> **答**：`grid_h = h_patches/merge_size` 会整除截断，剩余的边行/边列不会被覆盖到，`new_row_idx` 也到不了 `num_patches`，输出尾部会是未初始化内存。正因为如此，4.2 才把目标尺寸对齐到 `patch_size*2`，**保证** patch 行、列数都是偶数，避免这种半块情况。

**练习 2**：为什么像素 patch 和位置编码 `pos_embeds` 都要重排？

> **答**：merger 之后每个新 token 由「同一 2×2 块的 4 个 patch」融合而成，它对应的「位置」也必须是这 4 个 patch 位置的融合。如果只重排 patch 不重排位置编码，patch 与位置就会错位，模型的空间感知会乱。所以 [src/ncnn_llm_gpt.cpp:1179](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1179-L1179) 和 [src/ncnn_llm_gpt.cpp:1217](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1217-L1217) 用完全相同的规则各重排一次。

---

## 5. 综合实践

把四个模块串起来，跟踪一张图从文件到「可喂网络」的全过程。

**任务**：假设有一个 Qwen2.5-VL 风格模型，`patch_size=14`、`max_num_patches=2560`、`spatial_merge_size=2`，输入图 `700×960`。

1. **加载**：说明 `load_image_to_ncnn_mat("a.jpg")` 返回的 Mat 的 `w/h/c` 与通道顺序。
2. **算尺寸**：用 4.2 的示例代码算出 `target_h, target_w`，以及 `num_patches_h, num_patches_w`（注意它们必须是偶数）。画出 patch 网格（行×列）。
3. **切 patch**：写出 `bgr_to_pixel_values` 输出的 Mat 形状（`w=? h=?`），并指出每个 patch 向量的维度。
4. **重排**：用 4.4 的方法，写出重排后「前 4 个输出位置」分别对应原网格里的哪些 patch 行号（即第一个 2×2 块）。
5. **串联**：在源码 [src/ncnn_llm_gpt.cpp:1169-1179](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1169-L1179) 里，标注出 `get_image_size_for_patches → ncnn_mat_resize → bgr_to_pixel_values → reorder_patches_for_merge` 这条调用链的行号，确认它们的顺序与上面你画的流程一致。

> 这条链路位于 `get_visiual_features` 函数内（[src/ncnn_llm_gpt.cpp:1158](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L1158-L1158) 起），它正是下一讲 u5-l3 的主角——视觉特征提取。本讲你已把它的「预处理前半段」完全打通。

## 6. 本讲小结

- `load_image_to_ncnn_mat` 用 stb_image 解码、强制 3 通道、RGB→BGR，失败返回空 Mat，只做解码不做缩放。
- `get_image_size_for_patches` 以 `scale` 从 1.0 起按 0.02 步长缩小，把目标宽高对齐到 `patch_size*2` 的倍数，保证 patch 行列数均为偶数且 patch 总数 `≤ max_num_patches`。
- `bgr_to_pixel_values` 把整图切成 `num_patches` 个 patch，每行一个 `patch_size*patch_size*3` 维向量，按 planar RGB 排列，并做 `(x/255-mean)/std` 归一化（Qwen3.5-VL 用 0.5/0.5、除 255.5）。
- `reorder_patches_for_merge` 按 2×2 块把光栅顺序重排为「块内连续」，让后续 merger 能连续取数；像素 patch 与位置编码必须用同一规则各重排一次。
- 四步被 `get_visiual_features` 顺序串起：`get_image_size_for_patches → resize → bgr_to_pixel_values → reorder_patches_for_merge`，产物再喂给 patch embed / vision encoder（u5-l3）。
- 本讲所有函数都依赖构造函数从 `model.json` 读取的 `patch_size`/`patch_dim`/`max_num_patches`/`spatial_merge_size` 四个配置（回顾 u5-l1）。

## 7. 下一步学习建议

下一讲 **u5-l3 视觉特征提取与图像嵌入注入** 将从 `get_visiual_features` 切入，讲清本讲产出的 `pixel_values` 如何经 `vision_embed_patch`（patch embed）、`vision_encoder`（ViT 主体，含 merger）变成 `image_embeds`，以及 `inject_image_embeds` 如何把这些图像嵌入塞进文本 token 序列的 `<|image_pad|>` 占位处。

建议继续阅读：

- [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) 中 `get_visiual_features`（约 L1158 起）与 `inject_image_embeds` 的完整实现。
- 想理解 merger 之后 token 数如何变成 `num_patches/4`，可留意 u5-l3 里 `image_embeds` 的行数与本讲 `num_patches` 的关系。
- 若对位置编码好奇，可结合 u4-l2（视觉 mRoPE）预习：本讲的 patch 网格 `(num_patches_h, num_patches_w)` 正是 mRoPE 高度轴/宽度轴取值的来源。
