# 色彩与像素值算子族：cvtcolor、brightness_contrast、normalize 等

## 1. 本讲目标

学完本讲，你应该能够：

1. 拿到 CV-CUDA 的算子总清单（61 个），并按功能族（几何、色彩、滤波、像素值调整、归一化、统计分析、批操作、融合）给它们分类，遇到需求能立刻想到候选算子。
2. 正确调用 `cvcuda.cvtcolor`、`cvcuda.brightness_contrast`、`cvcuda.normalize`，理解它们各自的参数语义与数学公式。
3. 打开任意算子的 C 头文件（如 `OpCvtColor.h`），读懂其中的 **Limitations 契约表**——它是每个算子"支持哪些布局/通道数/数据类型"的权威来源，从而在写代码前就知道自己的 dtype/通道组合是否合法。

本讲承接 u3-l1 建立的「四连函数」套路（Tensor/变长批 × allocating/`_into`），把视野从单个算子扩展到整个算子族。

## 2. 前置知识

- **逐像素（pointwise）算子 vs 邻域算子**：`brightness_contrast`、`normalize` 这类算子输出某像素只依赖输入同一位置的像素，GPU 上天然并行、极快；`gaussian`、`medianblur` 这类滤波算子则要看邻域。本讲的主角（cvtcolor、brightness_contrast、normalize）基本都是逐像素算子。
- **色彩空间**：同一张图可以用不同坐标系描述像素。RGB 用红绿蓝三通道；BGR 只是通道顺序不同；灰度（GRAY/Y）只有一个亮度通道；HSV 用色相/饱和度/明光描述，调色时更直观；YUV 把亮度（Y）与色度（UV）分离，是视频编码的常客。"色彩空间转换"就是把像素从一个坐标系搬到另一个坐标系。
- **归一化（Normalization）**：深度学习模型通常希望输入是"减去均值、除以标准差"后的分布，即把像素值映射到约 \( [-2, 2] \) 的范围。归一化算子就是把这件事做成一次 GPU 操作。
- **uint8 与浮点**：解码器输出的图几乎都是 uint8（0~255）；模型输入几乎都要 float。`normalize` 这类算子的输出 dtype 与输入相同（见 4.5 的契约表），所以"转浮点"通常要先单独做（`cvcuda.convertto`）。
- 回顾 u2-l2：**DataType 只管位级信息，ImageFormat 才带颜色语义**；本讲的 cvtcolor 正是"输入 dtype + 转换码 → 输出 ImageFormat"的推导机器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/operators/Operators.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp) | 声明全部算子的 Python 导出函数，是"算子总清单"的权威位置 |
| [samples/operators/cvtcolor.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/cvtcolor.py) | cvtcolor 的官方示例：读图 → stack 成批 → 转换 → 写回 |
| [python/mod_cvcuda/operators/OpCvtColor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCvtColor.cpp) | cvtcolor 的 pybind11 绑定：四个重载入口 |
| [python/mod_cvcuda/CvtColorUtil.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CvtColorUtil.cpp) | 转换码 → 输出格式/输出形状的推导表与函数 |
| [python/mod_cvcuda/ColorConversionCode.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/ColorConversionCode.cpp) | 把 C 枚举 `NVCVColorConversionCode` 导出为 `cvcuda.ColorConversion` |
| [python/mod_cvcuda/operators/OpBrightnessContrast.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp) | brightness_contrast 的绑定：张量参数版 + 标量版共 8 个入口 |
| [python/mod_cvcuda/operators/OpNormalize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp) | normalize 的绑定：base/scale 张量版与标量列表版 |
| [src/cvcuda/include/cvcuda/OpCvtColor.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCvtColor.h) | cvtcolor 的 C API 与 Limitations 契约表 |
| [src/cvcuda/include/cvcuda/OpBrightnessContrast.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpBrightnessContrast.h) | brightness_contrast 的公式与 dtype 支持矩阵 |
| [src/cvcuda/include/cvcuda/OpNormalize.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h) | normalize 的公式、广播规则与 Limitations |
| [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py) | 综合实践的输入图来源（tabby_tiger_cat.jpg） |
| [samples/common.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py) | `read_image`/`write_image` 辅助函数（返回 RGB8 HWC 张量） |

## 4. 核心概念与源码讲解

### 4.1 算子分类地图：61 个算子如何按族记忆

#### 4.1.1 概念说明

CV-CUDA 的算子数量多（Python 侧共 61 个），逐个背不现实，也没必要。好在它们命名高度规范（`OpXxx` → `cvcuda.xxx`），且按功能自然分族。一旦建立"族"的索引，遇到需求就能直接跳到 2~3 个候选算子，再去查它们的 Limitations 表做精选。

算子总清单的权威位置是 `Operators.hpp` 里的一串前向声明——每个 `void ExportOpXxx(py::module &)` 对应一个 Python 可见算子。

#### 4.1.2 核心流程

从 [python/mod_cvcuda/operators/Operators.hpp:L54-L114](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L54-L114) 可以数出恰好 **61 个** `ExportOp*` 声明。本讲按功能把它们划分成下表（划分是为了便于记忆，权威清单以 Operators.hpp 为准）：

| 功能族 | 代表算子（Python 函数名） | 典型用途 |
|--------|---------------------------|----------|
| 几何/重采样 | `resize`, `pillowresize`, `hq_resize`, `rotate`, `warp_affine`, `warp_perspective`, `remap`, `flip`, `custom_crop`, `center_crop`, `random_resized_crop` | 缩放、旋转、翻转、裁剪（u3-l1 已入门） |
| 色彩空间/通道 | `cvtcolor`, `advcvtcolor`, `channel_reorder`, `composite` | RGB↔BGR、RGB↔灰度、YUV↔RGB、通道重排 |
| 像素值调整 | `brightness_contrast`, `gamma_contrast`, `color_twist`, `histogram_eq`, `clahe`, `invert`, `solarize`, `posterize`, `auto_contrast`, `adjust_hue/saturation/sharpness/contrast` | 曝光、对比度、色调、风格化增强 |
| 滤波/卷积 | `gaussian`, `medianblur`, `laplacian`, `average_blur`, `box_blur`, `conv2d`, `bilateral_filter`, `joint_bilateral_filter`, `morphology`, `threshold`, `adaptive_threshold`, `inpaint` | 去噪、边缘、二值化、修复 |
| 归一化/类型 | `normalize`, `convertto` | 推理前的标准预处理 |
| 统计/分析 | `histogram`, `minmaxloc`, `min_area_rect`, `non_max_suppression`, `sift`, `pairwise_matcher`, `find_homography`, `label`, `bndbox` | 输出不是图像，而是 Array/Tensor 结果 |
| 数据增强 | `gaussian_noise`, `erase`, `jpeg_compression_distortion` | 训练数据增广 |
| 批/内存操作 | `stack`, `padandstack`, `copy_make_border`, `osd`, `reformat` | 批组织、填充、可视化叠加 |
| 融合算子 | `resize_crop_convert_reformat`, `crop_flip_normalize_reformat` | 多步预处理合并进单 kernel（u3-l4 专讲） |

规律：**前五族是"图像进、图像出"的视觉算子**（本讲与 u3-l1 的领地），统计/分析族输出的是数值结果，批操作族甚至不改变像素值。

#### 4.1.3 源码精读

[python/mod_cvcuda/operators/Operators.hpp:L54-L114](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L54-L114) 逐行声明了 61 个算子的导出函数（这里列出开头几行）：

```cpp
void ExportOpReformat(py::module &m);
void ExportOpResize(py::module &m);
void ExportOpCustomCrop(py::module &m);
void ExportOpNormalize(py::module &m);
void ExportOpConvertTo(py::module &m);
...
```

这段是纯前向声明：真正的 `ExportOpXxx` 定义在 `operators/OpXxx.cpp` 中，由 `Main.cpp` 依次调用（回顾 u1-l4 的"定义、声明、注册三站"）。另注意 `ExportOpGaussian`（模糊）与 `ExportOpGaussianNoise`（噪声，L92）是两个不同算子，这是 u1-l4 提醒过的易混点。

#### 4.1.4 代码实践

1. **实践目标**：建立自己的算子速查表。
2. **操作步骤**：在仓库根目录执行 `rg -c '^void ExportOp' python/mod_cvcuda/operators/Operators.hpp` 确认数量为 61；再把 4.1.2 的表格复制出来，对照 Operators.hpp 逐行把 61 个算子填进你自己的分类列。
3. **需要观察的现象**：是否有算子你不知道该归哪一类（比如 `osd`、`label`）——把这些"孤儿"标出来，它们往往就是后续讲义的主角。
4. **预期结果**：得到一张 61 行的分类表，此后检索算子先查表再进源码。

#### 4.1.5 小练习与答案

**练习 1**：`cvcuda.convertto` 的导出声明叫 `ExportOpConvertTo`，但 Python 函数名是 `convertto` 而不是 `convert_to`。去哪里确认？
**答案**：看 [python/mod_cvcuda/operators/OpConvertTo.cpp:L65-L66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpConvertTo.cpp#L65-L66)，`m.def("convertto", ...)` 的第一个参数才是 Python 侧名字。C++ 导出函数名与 Python 函数名并非机械对应，以 `m.def` 为准。

**练习 2**：想把一批 uint8 图像的像素值取反（255−x），候选算子有哪些？
**答案**：像素值调整族的 `invert`（Operators.hpp L106，`ExportOpInvert`）；也可以用 `brightness_contrast` 的公式凑出来（brightness=−1、brightness_shift=255），但直接用语义化的 `invert` 更清晰。

### 4.2 cvtcolor：色彩空间转换与输出格式的自动推导

#### 4.2.1 概念说明

`cvcuda.cvtcolor` 把图像从一个色彩空间转到另一个，转换码沿用 OpenCV 的命名习惯（`BGR2RGB`、`RGB2GRAY`、`YUV2RGB_NV12`……），并额外支持 Bayer、mRGBA 等。它回答一个每个管线都会遇到的问题："解码器给我 RGB，模型要 BGR / 灰度 / 浮点 YUV，怎么换？"

对用户最友好的一点是：**你只需要给转换码，输出张量的形状、通道数、dtype 全部自动推导**——3 通道进 1 通道出（RGB2GRAY）、uint16 进 uint16 出（位深继承）这些规则都写在绑定层的工具函数里。

#### 4.2.2 核心流程

allocating 变体 `cvcuda.cvtcolor(src, code)` 的执行路径：

1. pybind 按参数类型决议到 Tensor 版 `CvtColor`（u3-l1 的重载套路）；
2. `GetOutputFormat(input.dtype(), code)`：查 `kOutputFormat` 表得到 8bit 基准输出格式，再把输入位深"继承"到输出的 packing 上；
3. `GetOutputTensorShape(input.shape(), outputFormat, code)`：校验输入 rank 为 3~4（HWC/CHW/NHWC/NCHW），定位 H/C 轴，把 C 轴长度替换为输出格式的通道数；对 NV12/NV21 相关的码，高度还要乘 \( \frac{2}{3} \) 或 \( \frac{3}{2} \)；
4. 输出 dtype 取输出格式第 0 平面第 0 通道的类型；
5. `Tensor::Create(outputShape, outputDType)` 分配输出（进对象缓存，u4-l2 展开）；
6. `CvtColorInto`：`ResourceGuard` 对输入加读锁、输出加写锁，然后 `submit` 到流上。

变长批入口额外要求批内格式统一，且平面格式只支持 `RGB8p`/`RGBA8p` 等少数组合（见绑定层的 `PreservePlanarVarShapeOutput` 检查）。

#### 4.2.3 源码精读

**示例三步走**：[samples/operators/cvtcolor.py:L40-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/cvtcolor.py#L40-L53) 先用 `cvcuda.stack` 把 HWC 单图包成 NHWC 批，再转换，最后 `reshape` 去掉批维写盘：

```python
nhwc_image: cvcuda.Tensor = cvcuda.stack([input_image])
converted: cvcuda.Tensor = cvcuda.cvtcolor(
    nhwc_image, code=cvcuda.ColorConversion.RGB2BGR
)
output_image: cvcuda.Tensor = converted.reshape(converted.shape[1:], "HWC")
```

示例注释建议给 cvtcolor 喂批化（NHWC）输入；而 C 头契约（4.5 节）显示 HWC 布局同样在支持列表里，形状推导函数也接受 rank 3~4。

**输出格式查表**：[python/mod_cvcuda/CvtColorUtil.cpp:L39-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CvtColorUtil.cpp#L39-L55) 是 `kOutputFormat` 映射表的开头，每个转换码对应一个 8bit 基准输出格式：

```cpp
const std::unordered_map<NVCVColorConversionCode, NVCVImageFormat> kOutputFormat = {
    {     NVCV_COLOR_BGR2BGRA, NVCV_IMAGE_FORMAT_BGRA8},
    ...
    {      NVCV_COLOR_BGR2RGB,  NVCV_IMAGE_FORMAT_RGB8},
    {      NVCV_COLOR_RGB2BGR,  NVCV_IMAGE_FORMAT_BGR8},
    {     NVCV_COLOR_BGR2GRAY, NVCV_IMAGE_FORMAT_Y8_ER},
```

注意 `RGB2BGR` 与 `BGR2RGB` 在内存层面是同一个操作（交换 R/B 通道），区别只在语义命名。

**位深继承**：[python/mod_cvcuda/CvtColorUtil.cpp:L147-L179](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CvtColorUtil.cpp#L147-L179) 的 `GetOutputFormat` 要求输入各通道位深一致，然后把输入位深写入输出 packing——所以 uint16 进、uint16 出，无需用户指定。

**NV12 的高度变换**：[python/mod_cvcuda/CvtColorUtil.cpp:L181-L200](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CvtColorUtil.cpp#L181-L200) 中，NV12 输入（Y 面 + 半高 UV 面）转 RGB 时输出高度是输入的 \( \frac{2}{3} \)，反向则是 \( \frac{3}{2} \)。这是"输出形状会变"的最典型例子。

**Python 枚举从哪来**：[python/mod_cvcuda/ColorConversionCode.cpp:L26-L37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/ColorConversionCode.cpp#L26-L37) 用 `py::enum_` 把 C 枚举导出为 `cvcuda.ColorConversion`，共 200 余个值，docstring 明说"mirrors OpenCV convention"。

#### 4.2.4 代码实践

1. **实践目标**：跑通官方 cvtcolor 示例并观察转换码对输出的影响。
2. **操作步骤**：
   ```bash
   cd samples
   python3 operators/cvtcolor.py                       # 默认输入 assets/images/tabby_tiger_cat.jpg
   python3 operators/cvtcolor.py -i assets/images/tabby_tiger_cat.jpg -o /tmp/gray.jpg  # 改下面这行后再跑
   ```
   把脚本中的 `code=cvcuda.ColorConversion.RGB2BGR` 改为 `RGB2GRAY`，再运行一次。
3. **需要观察的现象**：第一次输出与原图相比红蓝互换（暖色调变冷）；改 `RGB2GRAY` 后输出变为单通道灰度图，且 `converted.shape` 的最后一维从 3 变成 1。
4. **预期结果**：`write_image` 成功写出两张可对比的图；可用 `print(converted.shape, converted.dtype)` 打印确认通道数变化。
5. 本环境无 GPU，以上为**待本地验证**内容。

#### 4.2.5 小练习与答案

**练习 1**：对一张实际是 RGB 的图执行 `BGR2RGB`，输出看起来会怎样？
**答案**：转换码只声明"输入按 BGR 解释、输出按 RGB 排列"，本质是交换第 0 与第 2 通道。数据实际是 RGB 时，输出的数据排列就是 BGR——用 RGB 查看器打开会红蓝互换。语义正确性取决于你对输入的真实了解。

**练习 2**：为什么 `cvcuda.cvtcolor` 不需要像 `resize` 那样由调用者指定输出形状？
**答案**：resize 的目标尺寸是自由参数（缩到多大都行），而 cvtcolor 的输出形状由转换码唯一确定（通道数来自 `kOutputFormat` 表、高度按 NV12 规则缩放），因此绑定层在 [OpCvtColor.cpp:L68-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCvtColor.cpp#L68-L70) 自动推导即可。

**练习 3**：变长批 `cvcuda.cvtcolor(ImageBatchVarShape, ...)` 对输入批有什么额外要求？
**答案**：批内所有图像格式必须统一，否则 [OpCvtColor.cpp:L169-L173](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCvtColor.cpp#L169-L173) 抛出 "All images in input must have the same format"；平面输入还只支持 RGB8p/RGBA8p 等少数格式（`PreservePlanarVarShapeOutput`）。

### 4.3 brightness_contrast：一条公式 + 三种参数形态

#### 4.3.1 概念说明

亮度对比度调整只有一个公式（同时出现在 C 头与 Python docstring 中）：

\[
\text{out} = \text{brightness\_shift} + \text{brightness} \times \left(\text{contrast\_center} + \text{contrast} \times (\text{in} - \text{contrast\_center})\right)
\]

直觉读法：以 `contrast_center` 为支点拉伸（contrast），再整体缩放（brightness）与平移（brightness_shift）。四个参数都有**中性默认值**——brightness=1、contrast=1、brightness_shift=0，contrast_center 按输入类型取值域中点（float 为 0.5、无符号整数为 \( 2^{b-1} \)、有符号整数为 \( 2^{b-2} \)），所以"什么都不传"等价于恒等变换。

它的参数有三种形态，这是本算子最值得学的 API 设计：

1. **标量版**：`brightness_contrast(src, b, c, bs, cc, clamp=...)`，四个 Python float，全批共用一套；
2. **张量版（固定批）**：参数是仅 1 个元素的 `cvcuda.Tensor`；
3. **张量版（变长批）**：参数张量含 1 或 N 个元素，N 为批内图像数——即**每张图一套参数**，这是数据增强管线的刚需。

#### 4.3.2 核心流程

标量版调用 `UnaryElementwiseTensor<cvcuda::BrightnessContrast>(...)` 模板直接把四个 double 传给 C++ 算子。张量版流程：

1. allocating 变体先用 `tensorLike(src)` 克隆一个同 shape/dtype/layout 的输出张量；
2. `runGuard` 把当前流（未传则 `Stream::Current()`）包进 `ResourceGuard`，对输入和四个参数张量加**读锁**、输出加**写锁**；
3. 未提供的参数张量以空张量（`nvcv::Tensor{nullptr}`）传入，C++ 侧回退到中性默认值。

输出与输入的布局、通道数、宽高、样本数必须一致，但 **dtype 可以不同**（如 uint8 进、float16 出，需 _into 变体自备输出）——这是它与 normalize 的重要区别。

#### 4.3.3 源码精读

**输出克隆**：[python/mod_cvcuda/operators/OpBrightnessContrast.cpp:L32-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp#L32-L38) 用 `tensorLike` 造一个与输入同形的输出：

```cpp
inline Tensor tensorLike(Tensor &src)
{
    const auto &srcShape = src.shape();
    Shape       dstShape = nvcvpy::CreateShape(srcShape);
    return Tensor::Create(dstShape, src.dtype(), src.layout());
}
```

**参数张量统一加锁**：[python/mod_cvcuda/operators/OpBrightnessContrast.cpp:L50-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp#L50-L79) 的 `runGuard` 用循环把四个可选参数张量逐个以 `LOCK_MODE_READ` 注册进 `ResourceGuard`，再把空缺参数翻译成空张量：

```cpp
for (const auto &arg : {brightness, contrast, brightnessShift, contrastCenter})
{
    if (arg) { guard.add(LockMode::LOCK_MODE_READ, {*arg}); }
}
```

这是"参数也是张量、也参与流同步"这一设计在绑定层的直接体现。

**标量版走模板**：[python/mod_cvcuda/operators/OpBrightnessContrast.cpp:L126-L139](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp#L126-L139) 中标量入口只是 `UnaryElementwiseInto/UnaryElementwiseTensor<cvcuda::BrightnessContrast>` 的一行包装，多带一个 `clamp` 参数（整数输出时裁剪到 \([0, 2^b-1]\)，浮点输出裁剪到 \([0,1]\)）。

**公式与默认值写进 docstring**：[python/mod_cvcuda/operators/OpBrightnessContrast.cpp:L164-L192](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp#L164-L192) 的 `m.def("brightness_contrast", ...)` 把 4.3.1 的公式与所有默认值原样写进 Python 帮助文档——在 Python 侧 `help(cvcuda.brightness_contrast)` 即可看到。

#### 4.3.4 代码实践

1. **实践目标**：用一条命令体感公式中各参数的作用。
2. **操作步骤**（示例代码）：
   ```python
   import cvcuda, numpy as np
   from common import read_image, write_image   # samples 目录下

   img  = read_image("assets/images/tabby_tiger_cat.jpg")   # RGB8 HWC
   nhwc = cvcuda.stack([img])
   out  = cvcuda.brightness_contrast(nhwc, 1.0, 2.0, 0.0, 128.0, clamp=True)
   write_image(out.reshape(out.shape[1:], "HWC"), "/tmp/high_contrast.jpg")
   ```
   分别改成 `(2.0, 1.0, 0.0, 128.0)`（提亮）与 `clamp=False` 再跑。
3. **需要观察的现象**：contrast=2 时明暗分化加剧；clamp=False 时高亮区出现"反卷"（uint8 溢出回绕）或过曝截断差异；clamp=True 则被压在上限。
4. **预期结果**：三张对比图，直观建立四个参数的语感。
5. 本环境无 GPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：变长批版 brightness_contrast 想给批内 8 张图各配一套参数，参数张量形状应是什么？
**答案**：含 8 个元素的一维张量（如 `cvcuda.Tensor((8,), np.float32)`）。docstring 明确允许 1 或 N 个元素：1 个则全批广播，N 个则逐图取值（[OpBrightnessContrast.cpp:L232-L243](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpBrightnessContrast.cpp#L232-L243)）。

**练习 2**：输入是 uint8 图，不传 `contrast_center` 时支点取多少？
**答案**：\( 2^{8-1} = 128 \)。规则见 docstring：无符号整数取 \( 2^{b-1} \)，有符号取 \( 2^{b-2} \)，浮点取 0.5。

**练习 3**：标量版比张量版多了哪个参数？它解决什么问题？
**答案**：`clamp`。逐像素乘加后整数输出可能越过值域（如 uint8 超过 255），clamp 把输出裁剪回名义范围，避免回绕伪影。

### 4.4 normalize：把像素值映射到模型想要的分布

#### 4.4.1 概念说明

`cvcuda.normalize` 实现深度学习预处理里最常见的标准归一化。默认模式下公式为：

\[
\text{out}[d] = (\text{in}[d] - \text{base}[p]) \times \text{scale}[p] \times \text{global\_scale} + \text{shift}
\]

其中 \( d \) 是数据张量的下标，\( p \) 是参数张量的下标。若设置标志 `cvcuda.NormalizeFlags.SCALE_IS_STDDEV`，则 scale 被解释为标准差，公式变为：

\[
m = \frac{1}{\sqrt{\text{stddev}[p]^2 + \epsilon}}, \quad \text{out}[d] = (\text{in}[d] - \text{mean}[p]) \times m \times \text{global\_scale} + \text{shift}
\]

`base` 通常是均值或最小值，`scale` 通常是标准差的倒数或 \( \frac{1}{\max-\min} \)；`global_scale`/`global_shift` 用来把结果再适配到输出类型的动态范围。与 brightness_contrast 的张量参数版类似，base/scale 也有**张量版与标量列表版**两种形态，后者免去上传参数张量。

#### 4.4.2 核心流程

- **输出即输入克隆**：`Tensor::Create(input.shape(), input.dtype())`——形状与 dtype 都不变。想得到 float 输出，请先用 `cvcuda.convertto` 转浮点（见 4.4.3）。
- **广播规则**：参数张量在 N/H/W/C 任一轴长度为 1 即在该轴广播（如 `(1,1,1,3)` 的 base 对 NHWC 输入按通道取值）。
- **标量列表版**：Python 传 `[0.485, 0.456, 0.406]` 这样的 1~4 元列表，绑定层打包成 `float4` 传入，不产生任何参数张量上传。

#### 4.4.3 源码精读

**张量版主体**：[python/mod_cvcuda/operators/OpNormalize.cpp:L45-L72](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L45-L72) 的 `NormalizeInto` 中，`ResourceGuard` 把输入与 base、scale 一起加读锁，再 submit：

```cpp
guard.add(LockMode::LOCK_MODE_READ, {input, base, scale});
guard.add(LockMode::LOCK_MODE_WRITE, {output});
```

**输出克隆**：[python/mod_cvcuda/operators/OpNormalize.cpp:L74-L80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L74-L80) 一行说明输出契约：

```cpp
Tensor output = Tensor::Create(input.shape(), input.dtype());
```

**标量列表打包**：[python/mod_cvcuda/operators/OpNormalize.cpp:L84-L97](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L84-L97) 的 `ToFloat4AndCount` 把 1~4 个 float 装进一个 `float4`（未用的 lane 置零）并记录实际个数，允许"1 个值全通道广播"或"逐通道各一个"。

**Python 侧公式与标志**：[python/mod_cvcuda/operators/OpNormalize.cpp:L292-L315](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L292-L315) 是标量列表版 `m.def("normalize", ...)` 的 docstring，明言"interleaved (NHWC/HWC) and planar (NCHW/CHW) input are both supported"；标志枚举 `NormalizeFlags` 在 [OpNormalize.cpp:L194](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L194) 导出，值来自 C 宏 `CVCUDA_NORMALIZE_SCALE_IS_STDDEV`（[OpNormalize.h:L42-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L42-L43)）。

**权威公式与广播语义**：[src/cvcuda/include/cvcuda/OpNormalize.h:L60-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L60-L79) 给出两条公式及 `param_idx` 的逐轴广播规则（`param_shape[axis] == 1 ? 0 : data_idx[axis]`）。

#### 4.4.4 代码实践

1. **实践目标**：手工验证 normalize 公式。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np, cvcuda
   # 构造已知输入：单像素 3 通道，值 [100, 200, 50]
   src = cvcuda.as_tensor(np.array([[[100, 200, 50]]], np.uint8).copy(), "HWC")
   f   = cvcuda.convertto(src, np.float32)             # 先转 float，否则 uint8 输出会截断
   out = cvcuda.normalize(f, [0.5, 0.5, 0.5], [1/128., 1/128., 1/128.])
   print(np.asarray(out.cuda()))                       # 借助 __cuda_array_interface__ 查看
   ```
3. **需要观察的现象**：按公式手算 \( (100-0.5)\times\frac{1}{128} \approx 0.777 \)，与打印的第一个通道值对照。
4. **预期结果**：三个通道的输出均落在约 \([-0.004, 1.58]\) 区间，与手算一致。
5. 本环境无 GPU 与运行依赖，**待本地验证**（查看输出需 numpy 能消费 CAI/DLPack，`np.asarray(out.cuda())` 依赖较新的 numpy；也可按 u2-l4 用 cupy/torch 承接）。

#### 4.4.5 小练习与答案

**练习 1**：`normalize` 直接作用在 uint8 张量上，输出 dtype 是什么？为什么通常不这么做？
**答案**：输出与输入同 dtype（uint8）。归一化结果是约 \([-2,2]\) 的小数，uint8 会四舍五入成 0/1/2，信息几乎全毁。正确顺序是先 `convertto` float 再 normalize（依据 [OpNormalize.cpp:L77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpNormalize.cpp#L77) 与 4.5 的契约表）。

**练习 2**：ImageNet 风格的 `mean=[0.485,0.456,0.406]`、`std=[0.229,0.224,0.225]`（作用于 0~1 浮点图），用本算子如何表达？
**答案**：`cvcuda.normalize(f, mean, std, cvcuda.NormalizeFlags.SCALE_IS_STDDEV)`——设置标志后 scale 直接传标准差，算子内部取倒数；也可不设标志、自己传 `1/std` 作为 scale。

**练习 3**：base/scale 张量形状 `(1,1,1,3)` 与 `(3,)` 对 NHWC 输入效果有何异同？
**答案**：语义相同——都在 C 轴逐通道、在 N/H/W 轴广播。依据是 [OpNormalize.h:L77-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L77-L79) 的逐轴广播规则：长度为 1 的轴取 0，长度匹配的轴逐元素取。

### 4.5 Limitations 契约表：每个算子的支持矩阵怎么读

#### 4.5.1 概念说明

CV-CUDA 每个算子的 C 头文件（`src/cvcuda/include/cvcuda/OpXxx.h`）在 Submit 函数的文档注释里都维护着一张 **Limitations 契约表**，固定回答五个问题：

1. **Data Layout**：支持哪些布局（kNHWC/kHWC/kNCHW/kCHW）；
2. **Channels**：支持 1/2/3/4 通道中的哪些；
3. **Data Type**：一张 8bit/16bit/32bit、有符号/无符号、F16/F32/F64 的逐项 Yes/No 表；
4. **Input/Output dependency**：输出与输入在布局/dtype/通道/宽高/样本数上是否必须一致；
5. **Supported backends**：CUDA/CPU 后端支持情况（目前算子均仅 CUDA）。

这张表是**仓库不变量**的一部分（见 AGENTS.md 对算子契约的要求），比任何二手资料都权威。Python 侧抛出的 "not supported" 异常，最终依据都是这里声明的范围。

#### 4.5.2 核心流程

读表三步法：

1. 先看 **Data Layout + Channels**：自己的张量布局（u2-l1 的 layout 知识）和通道数是否在列表里；
2. 再查 **Data Type 表**：按"位宽 × 符号 × 浮点"定位自己 dtype 那一行，必须是 Yes；
3. 最后看 **dependency 表**：决定输出张量要怎么准备（尤其用 `_into` 变体时）。

三个本讲算子的契约对比（摘自各自头文件）：

| 维度 | cvtcolor | brightness_contrast | normalize |
|------|----------|---------------------|-----------|
| 布局 | NHWC/HWC/NCHW/CHW | NHWC/HWC/NCHW/CHW | NHWC/HWC/NCHW/CHW |
| 通道 | 1/3/4（平面张量） | 1/2/3/4 | 1/3/4 |
| dtype | U8,S8,U16,S16,S32,F16,F32,F64 **取决于转换码** | U8,U16,S16,S32,F32（**不支持 S8/U32/F16/F64**） | U8,S8,U16,S16,S32,F32（不支持 U32/F16/F64） |
| 输出 dtype | 与输入同基底类型 | **可与输入不同** | **必须与输入相同** |
| 备注 | 平面 NCHW/CHW 不含子采样 YUV420 与 packed YUV422 码 | 参数为 float32（int32 数据时须 float64） | base/scale 张量另有限制 |

#### 4.5.3 源码精读

**cvtcolor 的契约**：[src/cvcuda/include/cvcuda/OpCvtColor.h:L54-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCvtColor.h#L54-L79) 声明布局/通道/dtype 范围、平面模式的排除项（YUV420 子采样与 packed YUV422 转换码）以及后端表（CUDA Yes / CPU No）：

```
 *  Input:
 *       Data Layout:    [kNHWC, kHWC, kNCHW, kCHW]
 *       Channels:       [1, 3, 4] for kNCHW/kCHW planar tensors; ...
 *       Data Type:      [U8, S8, U16, S16, S32, F16, F32, F64] depending on conversion code.
```

**brightness_contrast 的契约**：[src/cvcuda/include/cvcuda/OpBrightnessContrast.h:L64-L106](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpBrightnessContrast.h#L64-L106) 的 dtype 逐项表里 `8bit Signed | No`、`32bit Unsigned | No`、`16bit Float | No`；其后的 dependency 表标明 dtype 一栏为 `No`（允许输入输出不同）。

**normalize 的契约**：[src/cvcuda/include/cvcuda/OpNormalize.h:L82-L126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L82-L126) 的 dtype 表与 brightness_contrast 相近，但 dependency 表中 dtype 为 `Yes`（必须相同），且另有一节 "Scale/Base Tensor" 约束参数张量自身。

#### 4.5.4 代码实践

1. **实践目标**：为综合实践（第 5 节）预检合法性。
2. **操作步骤**：综合实践会用 `read_image` 得到 RGB8（即 U8×3）NHWC 张量，走 `BGR2RGB`。打开 [OpCvtColor.h:L54-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCvtColor.h#L54-L79) 核对三行：布局含 kNHWC ✓；通道含 3 ✓；dtype 含 U8 ✓。再核对 brightness_contrast 的表：U8 行 Yes ✓、通道 3 ✓。
3. **需要观察的现象**：三个检查点都落在表的 Yes/包含 范围内。
4. **预期结果**：确认整条管线合法；随后在第 5 节真正运行验证。
5. 反向实验（可选）：故意构造 S8 张量调 brightness_contrast，预期被 priv 层拒绝——**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`normalize` 的契约表为什么单独有一节 "Scale/Base Tensor"？
**答案**：因为 base/scale 是额外传入的参数张量，有自己的形状/广播/dtype 约束（见 [OpNormalize.h:L127](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L127) 起），与主输入输出的约束相互独立，需分别声明。

**练习 2**：某算子头文件写 `Planar image layouts: Not applicable` 加 `Reason`（AGENTS.md 的仓库不变量），这表示什么？
**答案**：表示该算子处理的数据没有"图像平面布局"概念（如处理坐标数组、非图像数据），因此在公开 C 头的 Limitations 契约里显式声明不适用并给出理由，而不是留空让读者猜。

**练习 3**：Python 里抛异常说 dtype 不支持，去哪找权威解释？
**答案**：先查对应 `src/cvcuda/include/cvcuda/OpXxx.h` 的 Limitations dtype 表；若在表内却仍报错，再用 `rg` 到 `src/cvcuda/priv/OpXxx.cpp` 找抛错点（u3-l1 讲过 priv 层"先校验后执行"）。

## 5. 综合实践

**任务**：写一个脚本，把 `samples/assets/images/tabby_tiger_cat.jpg`（hello_world 的默认输入图）依次过 `BGR2RGB` → `brightness_contrast` → `normalize` 三站小管线，全程不离开 GPU；然后用 4.5 节的方法核对所用 dtype/通道组合的合法性。

```python
# 示例代码：保存为 samples/my_color_pipeline.py 运行（需已安装 cvcuda wheel）
import numpy as np
import cvcuda
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parent.parent))
from common import read_image, write_image

# 0. 读图：read_image 返回 RGB8 的 HWC 张量（samples/common.py 契约）
img = read_image("assets/images/tabby_tiger_cat.jpg")
batched = cvcuda.stack([img])                    # HWC -> NHWC，整条管线保持批形式

# 1. 色彩空间：BGR2RGB 交换 R/B 通道（3 通道 U8 -> 3 通道 U8，查 OpCvtColor.h: 合法）
swapped = cvcuda.cvtcolor(batched, code=cvcuda.ColorConversion.BGR2RGB)

# 2. 亮度对比度：提亮 + 拉对比，clamp 防止 uint8 溢出（查 OpBrightnessContrast.h: U8 合法）
adjusted = cvcuda.brightness_contrast(swapped, 1.1, 1.4, 10.0, 128.0, clamp=True)
write_image(adjusted.reshape(adjusted.shape[1:], "HWC"), "/tmp/stage2_bright.jpg")

# 3. 归一化：normalize 输出 dtype 与输入相同，先转 float 再归一（ImageNet 风格参数）
f32 = cvcuda.convertto(adjusted, np.float32, scale=1/255.0)   # 0~255 -> 0~1
normalized = cvcuda.normalize(
    f32, [0.485, 0.456, 0.406], [0.229, 0.224, 0.225],
    cvcuda.NormalizeFlags.SCALE_IS_STDDEV,
)

# 4. 验证：归一化结果约呈 N(0,1) 分布（打印需 GPU 数组能被 numpy/cupy/torch 承接，见 u2-l4）
print("shape:", normalized.shape, "dtype:", normalized.dtype)
```

**操作步骤**：

1. 把脚本放进 `samples/` 目录（复用 `common.py`），`python3 my_color_pipeline.py` 运行；
2. 打开 `/tmp/stage2_bright.jpg` 与原图对比，确认第 1、2 站生效（红蓝互换 + 提亮拉对比）；
3. 按脚本注释里的三处"查表"提示，打开 [OpCvtColor.h:L54-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCvtColor.h#L54-L79)、[OpBrightnessContrast.h:L64-L106](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpBrightnessContrast.h#L64-L106)、[OpNormalize.h:L82-L126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L82-L126) 逐项核对；
4. 附加实验：把第 3 站的 `convertto` 删掉、直接对 uint8 做 normalize，保存输出观察"全黑/全白"的截断现象，印证练习结论。

**需要观察的现象与预期结果**：stage2 图有明显的色彩与对比变化；`normalized` 的 shape 为 `(1, H, W, 3)`、dtype 为 `float32`；跳过 convertto 的附加实验输出像素几乎全为 0。本环境无 GPU，**待本地验证**。

## 6. 本讲小结

- CV-CUDA 的 61 个算子（[Operators.hpp:L54-L114](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L54-L114)）可按功能分族记忆：几何、色彩、像素值调整、滤波、归一化、统计、增强、批操作、融合。
- `cvtcolor` 的输出格式/形状/位深全部由转换码自动推导（`kOutputFormat` 查表 + 位深继承 + NV12 高度 \( \times\frac{2}{3} \)），转换码沿用 OpenCV 命名。
- `brightness_contrast` 只有一条仿射公式，但有标量、1 元素张量、N 元素张量三种参数形态，参数张量同样要进 `ResourceGuard` 加读锁；输出 dtype 可与输入不同。
- `normalize` 输出与输入同 shape 同 dtype（想 float 先 `convertto`），base/scale 支持张量广播与标量列表两种形态，`SCALE_IS_STDDEV` 标志把 scale 语义切到标准差。
- 每个算子 C 头文件里的 **Limitations 契约表**（布局/通道/dtype/依赖/后端）是支持矩阵的唯一权威，写管线前先查表可以避免绝大多数"运行时才炸"。

## 7. 下一步学习建议

- 下一讲 u3-l3 将深入本讲反复出现的 allocating 与 `_into` 两类变体，解释对象缓存如何让 `cvcuda.cvtcolor(...)` 隐式分配的输出张量被复用，以及两种变体在循环管线中的性能差异。
- 想继续读源码的读者，推荐顺着 `cvcuda.convertto`（[python/mod_cvcuda/operators/OpConvertTo.cpp:L51-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpConvertTo.cpp#L51-L57)）看一个最简单的"输出类型由参数决定"的算子绑定，再到 `src/cvcuda/priv/OpCvtColor.cpp` 对照 priv 层如何执行 Limitations 校验（承接 u5-l1 的四层解剖）。
