# SAR 多通道图像加载与归一化

## 1. 本讲目标

本讲对应端到端流水线「训练阶段」中的第一处框架修改：**如何把 SAR 芯片正确地读进内存，并归一化成 YOLOv8 能消化的 uint8 图像**。

学完后你应当能够：

- 说清楚为什么 SAR 的 TIFF 必须用 `cv2.IMREAD_UNCHANGED` 读取，默认的 `cv2.imread` 会丢失什么。
- 写出多通道线性归一化的公式，并理解「clip + 线性缩放 + 取整」三步把 dB / 水深值压到 `[0, 255]` uint8 的过程。
- 解释为什么本项目特意把图像归一化到 **uint8**，而不是常见的 mean/std 浮点归一化——答案是「为了白嫖 Ultralytics 默认数据增强」。
- 看懂 `normalize()` 里 `min_values = [-6000, -50, -50]` 的通道顺序为何与写盘波段顺序相反。

本讲只讲「加载 + 归一化」这一个最小闭环，PIoU2 损失、xView3 指标、验证流程分别留给 u3-l3、u3-l4、u3-l5。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**SAR 像素值不是 0~255 的「颜色」，而是 dB（分贝）。** 合成孔径雷达（SAR）测量的是地表对微波的回波强度，经过对数压缩后用分贝表示，典型落在 `[-50, 20]` 区间：`-50` 近乎无回波（平静海面），`20` 是极强回波（硬目标）。xView3-SAR 的第三个通道 `bathymetry`（水深）单位是米，典型落 `[-6000, 2000]`（马里亚纳海沟到珠峰顶）。这三类数值的范围、量纲完全不同，而 YOLOv8 的卷积层期望 `uint8 [0,255]` 输入，所以必须各通道分别做线性映射。

**TIFF 比 PNG/JPG「重」。** 训练芯片是多波段、16 位整型（`int16`）的 GeoTIFF（见 [dataset/generate_xview3.py:L79-L80](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L79-L80)，`out_dtype = vh.dtype`）。默认的 `cv2.imread` 会把它**降位**成 8-bit BGR，悄悄丢掉高位深度信息；我们必须用 `IMREAD_UNCHANGED` 阻止这种「好心办坏事」。

**归一化是「训推一致性」暗线的一环。** 正如 u1-l3 指出的，归一化方式必须贯穿训练与推理一致：训练侧把 SAR 线性压到 `uint8`，推理侧（C++）也必须做同样的线性映射（只是终点是 signed int8，见 u6-l1）。本讲聚焦训练侧的 Python 实现。

> 术语速查：**dB**（分贝，对数刻度的回波强度）、**band / 通道**（多波段图像的第几层）、**int16**（16 位有符号整数，范围 `[-32768, 32767]`）、**uint8**（8 位无符号，范围 `[0, 255]`）、**broadcasting**（numpy 广播，不同形状数组按规则对齐运算）。

## 3. 本讲源码地图

本讲涉及的源码很少，但每一段都关键：

| 文件 | 作用 | 本讲用到哪部分 |
| --- | --- | --- |
| [software/training/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md) | 训练侧相对上游 Ultralytics v8.2.91 的五类修改说明 | 修改 #1（TIFF 加载）与 #2（归一化）的原文 |
| [dataset/generate_xview3.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py) | 把 xView3 大场景裁成芯片 | 写盘时的波段顺序（解释通道顺序疑问的关键证据） |

> 说明：`software/training/` 只存放相对上游的**补丁说明**（README + 三个 `.py` 文件），完整的 `BaseDataset`、`load_image`、数据增强代码都在上游 Ultralytics 仓库里。本讲引用的 `get_image_and_label`、`load_image` 均指上游方法，修改点以 README 记录为准。

## 4. 核心概念与源码讲解

### 4.1 TIFF 图像加载

#### 4.1.1 概念说明

YOLOv8 上游用 OpenCV（`cv2.imread`）读取训练图像。OpenCV 的默认读取行为对「普通照片」很友好：自动转成 8-bit、3 通道 BGR。但对本项目的 SAR 芯片，这个默认行为是灾难性的：

1. **降位**：`int16` 的 dB 值会被截断/重映射进 `[0,255]`，`-50` dB 和 `20` dB 的相对关系被破坏。
2. **多波段**：芯片是 3 波段 TIFF，默认行为可能只保留部分通道或改写通道顺序。

因此必须给**每一处** `cv2.imread` 加上 `cv2.IMREAD_UNCHANGED` 标志，要求 OpenCV「原样返回」——保留原始位深与全部波段。

#### 4.1.2 核心流程

读取流程很短：

```
磁盘芯片 (.tif, int16, 3 波段)
        │  cv2.imread(path, cv2.IMREAD_UNCHANGED)
        ▼
内存数组 (H, W, 3)，dtype=int16，通道顺序由 OpenCV 决定（见 4.1.3 的「反转」细节）
        │  交给 normalize()
        ▼
uint8 图像 (H, W, 3)
```

注意「通道顺序由 OpenCV 决定」这一步——它是后面 `min_values` 顺序疑问的根源。

#### 4.1.3 源码精读

README 用一句话点出这处修改，覆盖**所有** `cv2.imread` 调用点：

> **Image Loading:** To read tiff files the `cv2.IMREAD_UNCHANGED` flag must be added to every `cv2.imread` calls.
>
> —— [software/training/README.md:L26-L26](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L26-L26)

「every」一词很关键：Ultralytics 在 `load_image`、推理、可视化等多处都会 `imread`，任何一处漏掉标志，读出来的数组就不一致，训练会悄悄掉精度。

**为什么通道顺序会反转？** 关键证据在数据生成脚本写盘的波段顺序。`generate_xview3.py` 把 VV 写到第 1 波段、VH 第 2、bathymetry 第 3：

```python
dst.write(vv_crop, 1)          # 波段 1 = VV
dst.write(vh_crop, 2)          # 波段 2 = VH
dst.write(bathymetry_crop, 3)  # 波段 3 = bathymetry
```

—— [dataset/generate_xview3.py:L148-L150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L148-L150)

也就是说，**磁盘上的波段顺序是 `(VV, VH, bathymetry)`**。但下一节的 `normalize()` 却把第 0 通道当成 bathymetry（`min=-6000`）。这两者要自洽，唯一的解释是：`cv2.imread(..., IMREAD_UNCHANGED)` 读取 3 波段 TIFF 时**反转了通道顺序**（OpenCV 的 BGR 约定），于是磁盘 `(VV, VH, bathymetry)` 进内存后变成 `(bathymetry, VH, VV)`。这就是 `min_values` 第一位是 `-6000`（bathymetry）的真正原因——它对齐的是**内存中的通道顺序**，而非磁盘波段顺序。4.2 节会给出完整公式印证。

> ⚠️ **待本地验证**：`cv2.imread` 对 3 波段 `int16` GeoTIFF 的确切通道顺序行为会因 OpenCV/libtiff 版本而异。上面的「反转」解释是与 `generate_xview3.py` 写盘顺序 + `normalize()` min/max 顺序交叉验证后的最自洽推断；建议你在本地用一张真实芯片打印 `img[..., 0].min()` 验证（见 4.1.4）。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `IMREAD_UNCHANGED` 对 SAR TIFF 的位深与通道数的影响。

**操作步骤**（需要一张由 `generate_xview3.py` 产出的真实芯片；若无数据则转为「源码阅读型实践」）：

1. 用默认方式与加标志方式分别读同一张芯片：

   ```python
   import cv2
   img_default = cv2.imread("00000000.tif")               # 默认
   img_unchanged = cv2.imread("00000000.tif", cv2.IMREAD_UNCHANGED)
   print(img_default.dtype, img_default.shape)
   print(img_unchanged.dtype, img_unchanged.shape)
   ```

2. 观察通道顺序：打印每通道的最小值，判断哪一通道是 bathymetry（应出现接近 `-6000` 的值）：

   ```python
   for c in range(img_unchanged.shape[2]):
       print(c, img_unchanged[..., c].min(), img_unchanged[..., c].max())
   ```

**需要观察的现象**：

- `img_default` 的 `dtype` 很可能是 `uint8`（被降位），`img_unchanged` 应为 `int16`（保留位深）。
- 通道 0 的 `min` 接近 `-6000` → 印证「通道 0 = bathymetry」，即读取后通道被反转。

**预期结果**：`img_unchanged` 的 `dtype=int16`、`shape=(H, W, 3)`；通道 0 为 bathymetry。若无真实数据，则**待本地验证**，改用步骤 2 的逻辑在任意 3 波段 TIFF 上定性确认通道反转行为即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `IMREAD_UNCHANGED` 漏加到 `load_image`，但验证阶段的 `imread` 加了，会发生什么？

> **参考答案**：训练时模型看到的是被降位/错通道的 uint8 图像，验证时却是正确的 int16→归一化图像，两者分布不一致。模型在验证集上的精度会异常偏低，且很难排查，因为「能跑」但不准。这正是 README 强调「every」的原因。

**练习 2**：除了位深，`IMREAD_UNCHANGED` 还保留了什么？

> **参考答案**：保留了全部波段（含可能的第 4 通道/Alpha），不做任何 BGR/灰度转换与缩放——即「as-is」。

---

### 4.2 多通道线性归一化

#### 4.2.1 概念说明

加载进来的 `int16` 图像有三个量纲不同的通道：VV/VH 是 dB（`[-50, 20]`），bathymetry 是米（`[-6000, 2000]`）。YOLOv8 期望 `uint8 [0,255]`。我们需要一个**逐通道、线性、带截断**的映射，把每个通道各自压到 `[0,255]`。

为什么是「线性 + clip」而不是 mean/std 标准化？因为目标产物是 **uint8**（见 4.3 节，为了复用增强），而 mean/std 会产生负数和浮点，无法直接存成 uint8 喂给默认增强流水线。

#### 4.2.2 核心流程

对每个通道 $c$，给定该通道的物理范围 $[lo_c, hi_c]$，归一化分三步：

1. **线性平移与缩放**，把 $[lo_c, hi_c]$ 映射到 $[0,1]$：

\[
x'_c = \frac{x_c - lo_c}{hi_c - lo_c}
\]

2. **截断（clip）到 $[0,1]$**，吸收越界值（比 `lo_c` 更小或比 `hi_c` 更大的极端像素）：

\[
\tilde{x}_c = \mathrm{clip}(x'_c,\ 0,\ 1)
\]

3. **放大并取整**，映射到 uint8：

\[
o_c = \mathrm{round}(\tilde{x}_c \times 255),\quad o_c \in \{0,1,\dots,255\}
\]

本项目取：

| 通道（内存顺序） | 物理量 | $lo_c$ | $hi_c$ | 区间宽度 |
| --- | --- | --- | --- | --- |
| 0 | bathymetry（水深，米） | $-6000$ | $2000$ | $8000$ |
| 1 | VH（dB） | $-50$ | $20$ | $70$ |
| 2 | VV（dB） | $-50$ | $20$ | $70$ |

举两个数值例子（VV 通道）：

- $x=-50 \Rightarrow x'=0 \Rightarrow o=0$（最弱回波 → 纯黑）
- $x=20 \Rightarrow x'=1 \Rightarrow o=255$（最强回波 → 纯白）
- $x=-15 \Rightarrow x'=\frac{35}{70}=0.5 \Rightarrow o=\mathrm{round}(127.5)=128$

注意第 1、2 通道的 $lo/hi$ 相同（都是 `[-50,20]`），所以 VV/VH 谁在通道 1、谁在通道 2 对归一化结果**没有影响**——但 bathymetry 必须是通道 0，否则水深值会被当成 dB，整张图会爆白。这正是 4.1.3「通道顺序」为什么重要的落点。

#### 4.2.3 源码精读

README 给出的归一化实现只有一行核心计算：

```python
def normalize(self, img):
    min_values = np.array([-6000, -50, -50])
    max_values = np.array([2000, 20, 20])
    return (np.clip((img - min_values) / (max_values - min_values), 0, 1) * 255).round().astype(np.uint8)
```

—— [software/training/README.md:L31-L35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L31-L35)

逐段拆解：

- `min_values / max_values`：形状 `(3,)`，对应内存通道顺序 `(bathymetry, SAR, SAR)`，即 4.2.2 表中的 $lo_c / hi_c$。
- `(img - min_values) / (max_values - min_values)`：`img` 形状 `(H, W, 3)`，numpy 把 `(3,)` 沿**最后一根轴（通道轴）广播**，于是每个通道用各自的 $lo_c/hi_c$ 计算——这正是要求 `img` 最后一维是通道、且顺序与 min/max 一致的原因。
- `np.clip(..., 0, 1)`：对应步骤 2，吸收越界。
- `* 255).round().astype(np.uint8)`：对应步骤 3，放大、四舍五入、转 uint8。`round()` 用「四舍六入五成双」（banker's rounding），所以 `127.5 → 128`、`126.5 → 126`。

README 同时指明了**插入点**——必须在 `BaseDataset.get_image_and_label` 中、`load_image` 之后调用：

> Image normalisation must be called in the `get_image_and_label` method of the `BaseDataset` class, after the `load_image` call.
>
> —— [software/training/README.md:L28-L28](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L28-L28)

`get_image_and_label` 是 Ultralytics 上游 `BaseDataset` 中每张图的「取图 + 取标签」入口；`load_image` 在它内部完成 `cv2.imread`。把 `normalize()` 放在 `load_image` **之后**，意味着先把 `int16` 读进来、立刻压成 `uint8`，再交给后续增强流水线。

#### 4.2.4 代码实践

**实践目标**：实现 `normalize()` 并用越界输入验证 clip 行为；解释 min/max 顺序。

**操作步骤**（仅需 numpy，**可本地运行**，无需项目数据）：

```python
import numpy as np

def normalize(img):
    min_values = np.array([-6000, -50, -50])
    max_values = np.array([2000, 20, 20])
    return (np.clip((img - min_values) / (max_values - min_values), 0, 1) * 255).round().astype(np.uint8)

# 构造 (bathymetry, VV, VH) 三通道测试图，形状 (H, W, C)，这里 H=4, W=1
img = np.array([
    [[-6000, -50, -50]],   # 三通道各自最小值 → 应为 0
    [[ 2000,  20,  20]],   # 三通道各自最大值 → 应为 255
    [[-2000, -15, -15]],   # 中点 → 约 128
    [[-9999,  99,  99]],   # 越界：bathymetry 过小、SAR 过大 → 被 clip
], dtype=np.float32)

print(normalize(img).reshape(-1, 3))
```

**需要观察的现象 / 预期结果**：

```
[[  0   0   0]      # 最小值 → 0
 [255 255 255]      # 最大值 → 255
 [128 128 128]      # 0.5 → round(127.5)=128
 [  0 255 255]]     # bathymetry -9999 < -6000 → clip 到 0；SAR 99 > 20 → clip 到 255
```

最后一行验证了 clip 的两个方向：`-9999` 被钉在 `0`、`99` 被钉在 `255`。如果去掉 `np.clip`，越界值会产生负数或大于 255 的数，`astype(np.uint8)` 会发生回绕（wrap-around），得到荒谬的结果——这正是 clip 不可省的原因。

**思考题（关于 min/max 顺序）**：磁盘写盘顺序是 `(VV, VH, bathymetry)`（见 [dataset/generate_xview3.py:L148-L150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L148-L150)），「按波段顺序」min 应当是 `[-50, -50, -6000]`。但代码里却是 `[-6000, -50, -50]`。为什么？

> **参考解释**：`min_values` 对齐的不是磁盘波段顺序，而是 `cv2.imread(..., IMREAD_UNCHANGED)` 读入内存后的**通道顺序**。OpenCV 读取 3 波段 TIFF 时会反转通道（BGR 约定），磁盘 `(VV, VH, bathymetry)` 进内存后变成 `(bathymetry, VH, VV)`，所以 min/max 必须写成 `(bathymetry, SAR, SAR)` = `[-6000, -50, -50]` 才能与广播轴匹配。若误写成「波段顺序」`[-50,-50,-6000]`，bathymetry 会被按 dB 范围缩放，整张图在该通道几乎全白，训练崩溃。这正是 4.1 强调通道顺序的原因。

#### 4.2.5 小练习与答案

**练习 1**：若把 `* 255` 改成 `* 127`，模型还能训练吗？会有什么副作用？

> **参考答案**：能训练，但 uint8 的动态范围被压缩到 `[0,127]`，丢失一半量化精度；模型可用的信号分辨率下降，尤其对小目标（SAR 船舶是点目标）不利。选 255 是为了充分利用 uint8 全量程。

**练习 2**：为什么 bathymetry 区间宽 8000，而 SAR 只有 70，却都要映射到同一个 `[0,255]`？

> **参考答案**：归一化目的是「让每个通道都落在网络习惯的 `[0,255]`」，而非保留跨通道的相对量纲。各通道各自满量程映射，等于让网络自己通过卷积权重去学习通道间的相对重要性。这是一种「逐通道归一化」的标准做法。

---

### 4.3 数据增强复用

#### 4.3.1 概念说明

Ultralytics 自带一套很强的数据增强：Mosaic、MixUp、随机翻转、HSV 抖动、仿射变换等。这些增强**默认假设输入是 `uint8 [0,255]`**——例如 HSV 抖动直接在 `[0,255]` 上做色度运算，Mosaic 把四张图拼成大图。

如果本项目把 SAR 归一化成「浮点 mean/std」（像 ImageNet 那样），这些默认增强就会全部失灵，需要逐一改写，工程量巨大且易引入 bug。本项目的巧妙之处在于：**特意把输出做成 `uint8`，从而原封不动地复用全部默认增强。**

#### 4.3.2 核心流程

```
load_image (cv2.imread + IMREAD_UNCHANGED)  → int16 (H,W,3)
        │
        ▼  ← normalize() 插入点（get_image_and_label 内）
normalize: int16 → uint8 [0,255] (H,W,3)
        │
        ▼  ← Ultralytics 默认增强流水线（无需改动）
Mosaic / HSV / Flip / Affine ...  仍在 uint8 域操作
        │
        ▼
送入 backbone（网络前向时再按需做 /255 缩放）
```

关键判断：把「SAR→uint8」这一步放在增强**之前**，就同时满足了两边——网络拿到的是规整 uint8 图，增强拿到的是它期望的 uint8 图。

#### 4.3.3 源码精读

README 用一句话点明了这层用意：

> This allows the augmentation techniques to be used as default with no additional modifications.
>
> —— [software/training/README.md:L37-L37](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L37-L37)

「no additional modifications」（无需额外修改）是这一节的核心：归一化到 uint8 的设计代价（损失一点浮点精度）换来了「整个增强流水线零改动」的巨大工程收益。这也是为什么 `normalize()` 必须落在 `load_image` 之后、增强之前——位置错了，复用就不成立。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在上游 Ultralytics 中定位增强流水线，确认它确实假设 uint8 输入。

**操作步骤**：

1. 在本地装好上游 Ultralytics（`pip install ultralytics==8.2.91`）。
2. 用 `Grep`/IDE 搜索上游源码中的 `def get_image_and_label`（位于 `ultralytics/data/dataset.py` 的 `BaseDataset`），阅读其调用 `self.load_image(...)` 之后、返回 `label` 之前的部分——确认 `normalize()` 应插入此处。
3. 搜索默认增强配置（`ultralytics/cfg/default.yaml` 中的 `hsv_h/hsv_s/hsv_v/degrees/translate/scale/shear/mosaic` 等键），观察它们的取值，理解这些增强在 `[0,255]` uint8 域上的语义。

**需要观察的现象**：

- `get_image_and_label` 在 `load_image` 之后直接构造 `label` 字典，没有对图像做归一化——这正是本项目插入 `normalize()` 的空位。
- 默认 `hsv_v`（亮度抖动）等参数会直接对像素值加减，显然假设 uint8 范围。

**预期结果**：你能在 `BaseDataset.get_image_and_label` 中精确指出 README 要求的插入位置（`load_image` 调用之后）。**待本地验证**：具体行号随上游版本变化，本讲不固化行号。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `normalize()` 的输出改成 `float32`（仍 `[0,1]`），默认 HSV 增强会发生什么？

> **参考答案**：HSV 抖动默认按 `[0,255]` 量级增减色度/亮度。输入变成 `[0,1]` float 后，这些增强会「用力过猛」，把像素瞬间推到远超 1 或低于 0 的值，随后又可能被错误地 clip/回绕，导致训练图像失真、精度下降。这就是必须输出 uint8 的根本原因。

**练习 2**：归一化放在 `load_image` **之前**可行吗？

> **参考答案**：不可行。`load_image` 内部才完成 `cv2.imread`，归一化的输入（`int16` 数组）在 `load_image` 之前还不存在。插入点必须是 `load_image` 之后。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「端到端」的归一化自检。

**任务**：写一个函数 `load_and_normalize(path)`，模拟训练侧的取图流程，并加上自检断言。

```python
import cv2
import numpy as np

def normalize(img):
    min_values = np.array([-6000, -50, -50])
    max_values = np.array([2000, 20, 20])
    return (np.clip((img - min_values) / (max_values - min_values), 0, 1) * 255).round().astype(np.uint8)

def load_and_normalize(path):
    # 步骤 1：用 IMREAD_UNCHANGED 读 TIFF，保留 int16 与全部波段
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    assert img is not None, f"读取失败，检查路径或是否漏加 IMREAD_UNCHANGED: {path}"
    assert img.dtype == np.int16, f"期望 int16，实际 {img.dtype}（是否漏加 IMREAD_UNCHANGED？）"
    assert img.ndim == 3 and img.shape[2] == 3, f"期望 (H,W,3)，实际 {img.shape}"

    # 步骤 2：归一化到 uint8 [0,255]
    out = normalize(img.astype(np.float32))

    # 步骤 3：自检——输出必须是 uint8、值域在 [0,255]
    assert out.dtype == np.uint8
    assert out.min() >= 0 and out.max() <= 255
    return out
```

**自检要点**：

1. 步骤 1 的两个断言直接守护了 4.1 的核心结论：漏加 `IMREAD_UNCHANGED` 时 dtype 会变成 `uint8`，断言会立刻报错而不是悄悄掉精度。
2. 步骤 2 复用了 4.2 的 `normalize()`；注意先 `astype(np.float32)`，避免 int16 减法溢出。
3. 在拿到真实芯片后，额外打印 `out[..., 0].mean()` 与 `out[..., 1].mean()`：通道 0（bathymetry）的均值通常明显不同于通道 1/2（SAR），可借此二次确认通道顺序与 4.1.3 的「反转」推断。

**若无真实数据**：用 4.2.4 的合成数组替代步骤 1 的 `cv2.imread`，直接喂给 `normalize()` 跑步骤 2、3，同样能验证归一化与自检逻辑——这部分**可本地运行**，下载真实 xView3 数据后再补全步骤 1（**待本地验证**）。

## 6. 本讲小结

- SAR 芯片是 `int16`、3 波段的 GeoTIFF，必须用 `cv2.IMREAD_UNCHANGED` 读取，否则会被默认 `cv2.imread` 降位、破坏通道，悄悄掉精度。
- `normalize()` 用「逐通道线性映射 + clip 到 `[0,1]` + 放大取整」三步，把 dB（`[-50,20]`）与水深（`[-6000,2000]`）各自压成 `uint8 [0,255]`；clip 不可省，否则 `astype(uint8)` 会回绕。
- `min_values = [-6000, -50, -50]` 对齐的是 `cv2.imread` 读取后**内存中的通道顺序**（OpenCV 反转通道，磁盘 `(VV,VH,bathymetry)` 进内存变 `(bathymetry,VH,VV)`），而非磁盘波段顺序。
- 归一化必须插在 `BaseDataset.get_image_and_label` 中、`load_image` **之后**：先读 int16，立刻压 uint8，再交给增强。
- 特意输出 `uint8` 是为了**零改动复用 Ultralytics 默认数据增强**（Mosaic/HSV/翻转等都假设 `[0,255]` uint8）。
- 归一化是 u1-l3 提出的「训推一致性」暗线的一环：训练侧 uint8 归一化必须与推理侧 C++ 的同公式映射（signed int8）严格一致，否则掉精度（详见 u6-l1）。

## 7. 下一步学习建议

本讲解决了「图像怎么进网络」。接下来：

- **u3-l3（PIoU2 边界框回归损失）**：图像进了网络，预测框与真值框的「相似度」怎么算？本项目用 PIoU2 替代默认 CIoU，是训练侧另一处关键修改，且与推理 NMS 共享同一公式（训推一致 IoU 暗线）。
- **u6-l1（框架补丁总览与 C++ 归一化）**：想看清「训推归一化一致」的另一头，可提前扫一眼推理侧 C++ 如何把 SAR 归一化成 **signed int8**，对照本讲的 uint8 实现，体会「同公式、不同终点 dtype」的设计。
- **延伸阅读**：上游 Ultralytics 的 `ultralytics/data/dataset.py` 中 `BaseDataset` 与 `ultralytics/data/augment.py` 的增强流水线，理解 `normalize()` 插入点的前后文。
