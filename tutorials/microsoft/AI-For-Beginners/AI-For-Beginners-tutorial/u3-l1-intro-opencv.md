# 计算机视觉入门与 OpenCV

## 1. 本讲目标

从本讲开始，我们进入课程第 IV 单元「计算机视觉（Computer Vision，CV）」。在上一单元里，你已经用 PyTorch/Keras 训练过神经网络；本讲先不碰神经网络，而是回答一个更基础的问题：**在把图像喂给神经网络之前，我们能用哪些「传统」手段去读取、清洗和增强图像？**

学完本讲你应该能够：

- 说清计算机视觉要解决哪些任务，以及它在整门 AI 课程里的位置。
- 用 OpenCV 完成图像的加载、颜色空间转换、缩放与二值化等基本处理。
- 理解均值滤波、中值滤波、高斯滤波、阈值化等经典算子的作用。
- 用「帧差法（frame difference）」检测视频里的运动区域，并理解它和光流（optical flow）的关系。

本讲是后续卷积神经网络（CNN）课的「数据预处理」前奏：好的预处理能让神经网络用更少的训练数据解决同样的问题。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**图像在 Python 里就是一个数组。** 一张 320×200 像素的灰度图，在内存里是一个形状为 `(200, 320)` 的 NumPy 数组（行数在前、列数在后）；同样尺寸的彩色图则是 `(200, 320, 3)`，最后一维的 3 对应红、绿、蓝三个通道。这一点至关重要：所谓「图像处理」，本质上就是对这个多维数组做数学运算。上一单元里你熟悉的 `numpy`，在这里就是主力工具。

**为什么要「传统 CV」？** 神经网络很强大，但它需要大量数据、算力，且像个黑盒。很多任务——比如把照片里的文档摆正、检测监控画面里有没有东西在动——用确定性的数组运算就能解决，又快又可解释。课程的一个核心理念是：**能用简单的计算机视觉技术替神经网络分担工作，就一定要用**，这样可以减少对训练数据的依赖。

**OpenCV 是事实标准。** OpenCV 用 C++ 实现、性能极高，并提供 `cv2` 这个 Python 接口。本单元几乎所有 Notebook 的预处理步骤都离不开它。上一单元的 `u1-l3` 已经在 `ai4beg` 环境里装好了 OpenCV，所以你可以直接 `import cv2`。

> 承接上一讲：`u2-l5` 讲了过拟合与正则化，其中「增加数据」是抗过拟合的手段之一；本讲要讲的图像增强（缩放、旋转、阈值化等）正是制造更多训练样本、让网络更好泛化的传统武器。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `lessons/4-ComputerVision/` 下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 计算机视觉单元的总目录，列出本单元 7 个小课的导航。 |
| `06-IntroCV/README.md` | 本课讲义正文，定义 CV、列举图像库、讲解 OpenCV 的加载与处理。 |
| `06-IntroCV/OpenCV.ipynb` | 本课可执行 Notebook，含盲文图像处理、帧差运动检测、光流三个完整示例。 |
| `06-IntroCV/data/braille.jpeg`、`data/motionvideo.mp4` | Notebook 用到的示例图片与视频。 |
| `06-IntroCV/lab/MovementDetection.ipynb`、`lab/palm-movement.mp4` | 综合实践 lab，用光流判断手掌上下左右的运动方向。 |

找东西口诀（承接 `u1-l2`）：看 CV 单元导览→`4-ComputerVision/README.md`；读讲义→`06-IntroCV/README.md`；跑代码→`OpenCV.ipynb`；做实验→`lab/`。

## 4. 核心概念与源码讲解

### 4.1 CV 任务分类

#### 4.1.1 概念说明

计算机视觉（Computer Vision）是一门让计算机「看懂」数字图像的学科。课程在 [06-IntroCV/README.md:3-3](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L3-L3) 给出的定义里强调，这里的「理解」含义很广，可能指：

- **图像分类（image classification）**：整张图属于哪一类，是 CV 里最简单的任务（见 [06-IntroCV/README.md:7-7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L7-L7)）。
- **目标检测（object detection）**：找出图中物体在哪里（位置 + 类别）。
- **事件检测（event detection）**：理解画面里发生了什么。
- **图像描述 / 3D 重建**：把图片转成文字，或恢复场景的三维结构。
- **与人相关的专项任务**：年龄与情绪估计、人脸检测与识别、3D 姿态估计等。

本单元后续课程正好对应这些任务：07-ConvNets（分类）、11-ObjectDetection（检测）、12-Segmentation（像素级分割）。本单元的 7 个小课在 [4-ComputerVision/README.md:5-13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/README.md#L5-L13) 列出。

现代 CV 大多用神经网络解决，但课程反复强调：**在把图像交给神经网络之前，往往值得先用算法手段增强图像**。

#### 4.1.2 核心流程

一个典型的「传统 CV 预处理 → 神经网络」流水线如下：

```text
原始图像/视频
   │  ① 读取（cv2.imread / VideoCapture）
   ▼
NumPy 数组 (H, W) 或 (H, W, 3)
   │  ② 颜色空间处理（BGR→RGB / 转灰度 / 转 HSV）
   ▼
   │  ③ 增强（缩放、模糊去噪、阈值化、几何变换）
   ▼
干净 / 对齐的图像
   │  ④ 喂给神经网络（后续 CNN 课）
   ▼
预测结果
```

第一步是「图像在内存里长什么样」。课程在 [06-IntroCV/README.md:28-28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L28-L28) 说明：灰度图就是 `H×W` 的二维数组，彩色图多一维通道，形状为 `H×W×3`。

#### 4.1.3 源码精读

OpenCV 并非唯一选择，[06-IntroCV/README.md:13-18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L13-L18) 给出四个常用 Python 图像库，定位各不同：

| 库 | 定位 |
| --- | --- |
| `imageio` | 读写多种图像格式，还支持用 ffmpeg 把视频帧转图片。 |
| `Pillow`（PIL） | 比图像读写更强，支持形变、调色板等图像操作。 |
| `OpenCV` | C++ 实现，图像处理事实标准，有便捷的 Python 接口。 |
| `dlib` | 含人脸、面部关键点等较难的 CV 算法。 |

其中 OpenCV 被称为「事实标准（de facto standard）」，见 [06-IntroCV/README.md:22-22](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L22-L22)。本课 Notebook 开头就 `import cv2` 并配上 `matplotlib`、`numpy`，再定义一个并排显示多图的辅助函数 `display_images`（见 [OpenCV.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/OpenCV.ipynb) 第 2 个 cell）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「图像就是数组」这件事，并体会 OpenCV 默认的 BGR 颜色顺序带来的「坑」。

操作步骤：

1. 打开 `OpenCV.ipynb`，选 `ai4beg` 内核。
2. 运行前两个 cell，确认 `import cv2` 不报错。
3. 运行加载盲文图片的 cell：
   ```python
   im = cv2.imread('data/braille.jpeg')
   print(im.shape)
   plt.imshow(im)
   ```
4. 观察 `im.shape`（应为形如 `(高, 宽, 3)`）和图片颜色。

需要观察的现象：图片能显示，但**颜色看起来怪怪的**（蓝色和红色像是对调了）。这就是下一节要讲的 BGR/RGB 问题。

预期结果：`print(im.shape)` 输出一个三维形状；图像显示偏色。如运行报 `module 'cv2' not found`，请回到 `u1-l3` 确认内核选的是 `ai4beg`。其余现象待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一张 320×200 像素的彩色图，对应的 NumPy 数组形状是什么？为什么是这个顺序？

> **答案**：形状是 `(200, 320, 3)`，即「行（高）× 列（宽）× 通道」。因为数组按行存储，行数等于图像高度，列数等于宽度，最后再用一维存 3 个颜色通道。

**练习 2**：在 [06-IntroCV/README.md:13-18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L13-L18) 列出的四个库里，哪一个最适合「把一段视频逐帧抽成图片」？

> **答案**：`imageio`，因为它支持 ffmpeg，可以方便地把视频帧转成图像；而 `dlib` 偏机器学习算法、`Pillow`/`OpenCV` 偏单图处理。

---

### 4.2 OpenCV 图像处理

#### 4.2.1 概念说明

这一模块解决三件事：**读图、管颜色、做几何与亮度调整**。

读图用 `cv2.imread`，得到的直接是 NumPy 数组。但 OpenCV 有一个历史包袱：**它默认按 BGR（蓝-绿-红）顺序存颜色**，而 Python 生态里大多数工具（包括 `matplotlib`）用 RGB（红-绿-蓝）。若不转换，图像就会偏色——这是 CV 新手最常踩的坑。

「颜色空间（color space）」不止 RGB/BGR 两种。本课常用的还有：

- **灰度（GRAY）**：丢掉颜色只留亮度，一个像素一个值，常用于「颜色不重要」的场景（如运动检测、盲文识别）。
- **HSV（色调 Hue-饱和度 Saturation-明度 Value）**：把「颜色是什么」和「多亮」分开，后续做光流可视化、肤色检测时很好用。

#### 4.2.2 核心流程

OpenCV 处理图像的标准套路见 [06-IntroCV/README.md:30-59](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L30-L59)：

1. **加载**：`im = cv2.imread('image.jpeg')`。
2. **转换颜色空间**：用同一个 `cv2.cvtColor` 函数切换，例如 `cv2.cvtColor(im, cv2.COLOR_BGR2RGB)` 转成 RGB，或转灰度、转 HSV（见 [06-IntroCV/README.md:40-44](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L40-L44)）。
3. **几何/亮度预处理**（[06-IntroCV/README.md:50-59](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L50-L59)），常用操作包括：
   - 缩放：`cv2.resize(im, (320,200), interpolation=cv2.INTER_LANCZOS)`。
   - 模糊去噪：`cv2.medianBlur(im, 3)` 或 `cv2.GaussianBlur(im, (3,3), 0)`。
   - 阈值化：`cv2.threshold` / `cv2.adaptiveThreshold`，往往比调亮度对比度更好用。
   - 仿射变换（保持平行线仍平行）与透视变换（已知 4 个点对应关系，可把斜拍的文档「摆正」）。

颜色空间转换的「数学」很简单——它只是对每个像素的三个通道值做一次线性重排或查表，例如 BGR→RGB 就是把第 0、2 通道对调。

#### 4.2.3 源码精读

Notebook 用盲文（Braille）图片演示了一整套「读图 → 灰度 → 二值化 → 切字符」的预处理链路，这正是「用纯 CV 给神经网络减负」的范例。

转灰度只需一行（[OpenCV.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/OpenCV.ipynb) 第 5 个 cell）：

```python
bw_im = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
```

盲文是黑白点阵，颜色无关紧要，所以先转灰度。随后 Notebook 把这张图一步步清洗成干净的黑白二值图（第 7 个 cell）：

```python
im = cv2.blur(bw_im, (3, 3))                       # 先模糊去噪
im = cv2.adaptiveThreshold(im, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                           cv2.THRESH_BINARY_INV, 5, 4)  # 自适应阈值二值化
im = cv2.medianBlur(im, 3)                          # 中值滤波去椒盐噪声
_, im = cv2.threshold(im, 0, 255, cv2.THRESH_OTSU)  # Otsu 自动选阈值
im = cv2.GaussianBlur(im, (3, 3), 0)                # 再高斯模糊
_, im = cv2.threshold(im, 0, 255, cv2.THRESH_OTSU)  # 再 Otsu
```

这段代码的关键在于：**不同滤波器各管一类噪声**——均值/高斯模糊对付细密噪声，中值滤波对付「椒盐」状坏点，Otsu 阈值则自动找到「前景/背景」的最佳分界。

为了让斜拍的盲文文本摆正，Notebook 还用了**透视变换**（第 15 个 cell），由 `cv2.findHomography` 算出变换矩阵，再由 `cv2.warpPerspective` 应用到整张图。摆正后，第 17 个 cell 用固定步长 `char_h=36, char_w=24` 把它切成一个个字符小图，交给后续网络分类（就像 MNIST 那样）。

#### 4.2.4 代码实践

**实践目标**：体会 BGR/RGB 转换的差异，并亲手做一次阈值化。

操作步骤：

1. 在 `OpenCV.ipynb` 运行到运动检测部分后，找到第 30 个 cell：
   ```python
   plt.imshow(cv2.cvtColor(frames[(sub[0]+sub[-1])//2], cv2.COLOR_BGR2RGB))
   ```
   先注释掉 `cvtColor`，直接 `plt.imshow(frames[...])`，对比转换前后的颜色。
2. 新建一个 cell（示例代码），对盲文图做一次最简单的固定阈值化：
   ```python
   # 示例代码
   import cv2
   g = cv2.cvtColor(cv2.imread('data/braille.jpeg'), cv2.COLOR_BGR2GRAY)
   _, bin_im = cv2.threshold(g, 127, 255, cv2.THRESH_BINARY)
   plt.imshow(bin_im, cmap='gray')
   ```

需要观察的现象：第 1 步中，未经 `cvtColor` 的图偏色（红蓝颠倒），转换后颜色正常；第 2 步中，调节阈值 `127` 的高低会改变前景/背景的分割范围。

预期结果：颜色转换前后差异明显；阈值化得到一张黑白图。具体阈值效果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么运动检测、盲文识别这类任务要先把彩色图转成灰度？

> **答案**：因为这些任务只关心「形状/位置/明暗变化」，颜色信息没用。转灰度把数据量降到原来的 1/3（从 3 通道变 1 通道），既省算力又减少干扰。

**练习 2**：`cv2.threshold`（固定阈值）和 `cv2.adaptiveThreshold`（自适应阈值）有什么区别？什么场景该用后者？

> **答案**：固定阈值对整张图用同一个分界值；自适应阈值在每个像素的小邻域内单独算阈值。当图像光照不均（如左亮右暗）时，固定阈值会失效，应改用自适应阈值。

**练习 3**：课程在 [06-IntroCV/README.md:56-58](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L56-L58) 提到「仿射变换」和「透视变换」。用一句话区分二者。

> **答案**：仿射变换需要 3 个点对应关系、保持平行线仍平行；透视变换需要 4 个点、可以还原斜拍产生的透视形变（如把斜拍的文档拍正）。

---

### 4.3 经典滤波算子

#### 4.3.1 概念说明

「滤波（filtering）」是 CV 里最古老也最实用的一类操作，本质上是用一个小数组（叫**卷积核/滤波核 kernel**）在图像上滑动，把核覆盖区域内的像素按某种规则聚合成一个新值。常见的有两类：

- **平滑类（去噪）**：均值滤波、高斯滤波（加权平均，越近权重越大）、中值滤波（取中位数，对孤立坏点特别有效）。
- **锐化/边缘类**：阈值化（把灰度压成黑白）、Sobel/Canny 等边缘检测算子。

为什么需要它们？神经网络对噪声敏感，原始图像里常有相机噪点、光照不均，先滤波去噪能让后续分类更稳。

本模块还顺带讲一个把「滤波」思想用到极致的应用：**运动检测**。思路见 [06-IntroCV/README.md:72-72](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L72-L72)——固定摄像机拍到的相邻两帧本应几乎一样，直接把两帧数组相减，差值小代表静止、大代表有东西在动。

#### 4.3.2 核心流程

帧差法运动检测的完整链路（对应 Notebook 第 19–30 个 cell）：

1. **读视频为帧序列**：用 `cv2.VideoCapture` 逐帧 `read`，存进列表。
2. **转灰度**：颜色无关，统一转灰度降维。
3. **相邻帧相减**：
   \[
   d_t = \text{frame}_{t+1} - \text{frame}_t
   \]
4. **量化「运动量」**：对差值图求范数（所有像素差值平方和再开方），得到一个标量。
   \[
   a_t = \| d_t \| = \sqrt{\sum_{i} d_{t,i}^{2}}
   \]
5. **滑动平均平滑**：用 `np.convolve` 做窗口为 `w` 的移动平均，去掉抖动。
6. **设阈值找事件**：`a_t > threshold` 的连续帧段，即一段「运动事件」。

数学上，第 4 步的范数衡量两帧的整体差异；第 5 步的移动平均公式为：
\[
\mathrm{MA}_t = \frac{1}{w}\sum_{k=0}^{w-1} a_{t+k}
\]

#### 4.3.3 源码精读

读视频帧的核心循环在 [OpenCV.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/OpenCV.ipynb) 第 20 个 cell：

```python
vid = cv2.VideoCapture('data/motionvideo.mp4')
c = 0
frames = []
while vid.isOpened():
    ret, frame = vid.read()   # ret 为 False 表示视频结束
    if not ret:
        break
    frames.append(frame)
    c += 1
vid.release()                 # 用完一定要释放
```

逐帧相减并求范数在第 22 个 cell，用 `zip` 把前后帧配对（示例代码标注其出自 Notebook）：

```python
bwframes = [cv2.cvtColor(x, cv2.COLOR_BGR2GRAY) for x in frames]
diffs = [(p2 - p1) for p1, p2 in zip(bwframes[:-1], bwframes[1:])]
diff_amps = np.array([np.linalg.norm(x) for x in diffs])
```

接着用阈值 `threshold = 13000` 和移动平均找出真正发生运动的那段帧（第 24、26 个 cell）：

```python
def moving_average(x, w):
    return np.convolve(x, np.ones(w), 'valid') / w

active_frames = np.where(diff_amps > threshold)[0]
```

最后展示这段运动事件的「中间帧」时，Notebook 特意提醒（第 29 个 cell 文字）：**这里颜色不对！** 因为 OpenCV 读出来的帧是 BGR，要再用 `cv2.cvtColor(..., cv2.COLOR_BGR2RGB)` 转回 RGB 才能正常显示（第 30 个 cell）。这正是 4.2 节讲的颜色坑在视频场景的再次出现。

课程在 [06-IntroCV/README.md:78-81](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L78-L81) 还提到：帧差法只告诉你「动了多少」，不告诉你「往哪动」。要回答方向，需要**光流（optical flow）**——它分稠密（每个像素都算一个运动向量）和稀疏（只跟踪角点/边缘等特征）两种。Notebook 第 32 个 cell 用 `cv2.calcOpticalFlowFarneback` 算稠密光流，输出的每个像素是一个 `(dx, dy)` 向量。

#### 4.3.4 代码实践

**实践目标**：用帧差法在 `motionvideo.mp4` 上检测出运动区间，并验证阈值的影响。这正是本讲规格要求的「用帧差法检测视频中的运动区域」。

操作步骤：

1. 打开 `OpenCV.ipynb`，运行第 19–28 个 cell（运动检测部分）。
2. 重点看第 24 个 cell 画出的 `moving_average(diff_amps, 10)` 曲线和红线阈值 `threshold = 13000`。
3. 把阈值改成 `8000` 和 `20000`，分别重跑第 26 个 cell 的 `sub = subsequence(active_frames)`，观察输出的运动帧范围变化。

需要观察的现象：

- 曲线大部分时间贴近 0（静止），中间有一段明显凸起（手掌在动）。
- 阈值越低，`active_frames` 越多、可能把噪声也算进来；阈值越高，越保守、可能漏掉部分运动帧。

预期结果：阈值 `13000` 时 `subsequence` 返回大约 `[195, 196, ..., 322]` 的连续帧段（Notebook 第 26 个 cell 的实际输出正是这一段），中间帧是手掌运动最明显的那一帧。修改阈值后的范围会相应变宽或变窄，具体数值待本地验证。

> 如果你本地跑不动视频，可退化为「源码阅读型实践」：对照第 22 个 cell 的 `zip(bwframes[:-1], bwframes[1:])`，解释为什么差分数组长度比帧数少 1。

#### 4.3.5 小练习与答案

**练习 1**：均值滤波、高斯滤波、中值滤波，哪一个最适合去除「椒盐噪声」（零星的白点/黑点）？为什么？

> **答案**：中值滤波。椒盐噪声是极端值，取邻域中位数能直接把它们「投」掉；而均值/高斯滤波会把坏点的影响平均扩散开，反而留下污渍。

**练习 2**：帧差法里为什么用 `np.linalg.norm(diff)` 而不是直接用 `diff.mean()` 来衡量运动量？

> **答案**：两者都能把差值图压缩成一个标量。范数对「局部剧烈变化」更敏感（平方再开方放大了大差值），能更突出真正的运动；均值则会被大片微小变化稀释。课程选范数是为了让运动事件在曲线上更「尖」、更容易被阈值切出来。

**练习 3**：帧差法能判断运动方向吗？如果不能，本课给出了哪种方法来补救？

> **答案**：不能，它只能给出「动了多少」。要判断方向需要光流（optical flow），它为每个像素算出运动向量 `(dx, dy)`，从而得到运动方向和大小。

---

## 5. 综合实践

把本讲三块知识（读视频帧、颜色/灰度处理、运动量量化）串起来，完成本课的官方 lab：**MovementDetection**。

**任务**：在 `lab/palm-movement.mp4`（一个人在稳定背景前把手掌向左/右/上/下移动）中，用光流判断每段视频在朝哪个方向动。这是 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/lab/README.md) 和 [lab/MovementDetection.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/lab/MovementDetection.ipynb) 指定的目标。

完成步骤（基于本讲学过的内容，按 lab Notebook 的 4 个 `# Code here` 填空）：

1. **取帧**：复用 4.3.3 读视频的循环，把 `palm-movement.mp4` 读成帧列表，再全部转灰度。
2. **算光流并转极坐标**：对相邻灰度帧调用 `cv2.calcOpticalFlowFarneback`，再用 `cv2.cartToPolar` 把每个像素的 `(dx, dy)` 转成「幅值 + 角度」。
3. **画方向直方图**：对每一帧，把所有像素的角度做直方图分箱（lab 提示：可先把幅值低于某阈值的向量置零，去掉眨眼、头部微动等噪声），观察不同运动方向落在哪个角度区间。
4. **判定方向**：选定上/下/左/右对应的角度区间，若该区间计数超过阈值，就判定这一帧在朝该方向运动。

**进阶目标（stretch goal）**：参考 lab README 给出的肤色跟踪博文，用 HSV 颜色空间分割出手掌/手指区域并跟踪其轨迹。

需要观察的现象：四类运动的帧，其方向直方图会在四个不同的角度区间（约 0°、90°、180°、270°）轮流出现峰值；置零小幅向量后，峰值更干净。

预期结果：能给出类似「第 50–120 帧向右、第 200–270 帧向上」的判定。具体帧号与阈值待本地验证。

## 6. 本讲小结

- 计算机视觉让计算机「看懂」图像，任务从最简单的图像分类，到目标检测、分割、3D 重建、人脸/姿态等；本单元后续课程逐一对应。
- **图像在 Python 里就是 NumPy 数组**，灰度图是 `(H, W)`、彩色图是 `(H, W, 3)`，几乎所有「图像处理」都是数组运算。
- OpenCV 是图像处理事实标准，但默认用 **BGR** 颜色顺序，喂给 `matplotlib` 等工具前要 `cv2.cvtColor(im, cv2.COLOR_BGR2RGB)`，否则颜色颠倒。
- 预处理三件套：颜色空间转换（RGB/灰度/HSV）、几何变换（缩放/仿射/透视）、滤波去噪（均值/高斯/中值/阈值化）。
- **帧差法**用相邻两帧相减 + 求范数 + 阈值，就能廉价地检测视频里的运动事件；要进一步知道运动方向，需要光流。
- 核心方法论：能用传统 CV 替神经网络分担的工作就一定用，这样可以减少对训练数据的依赖——这是下一讲 CNN 的数据预处理基础。

## 7. 下一步学习建议

本讲只讲了「喂给网络之前」的预处理，还没真正用神经网络处理图像。下一讲 `u3-l2 卷积神经网络 CNN 与经典架构` 将进入 `lessons/4-ComputerVision/07-ConvNets/`，讲解为什么处理图像要用专门的卷积层（而不是上一单元的全连接层），并梳理 LeNet/AlexNet/VGG/ResNet 的演进。

继续阅读建议：

- 想吃透本课的滤波与二值化，精读 [OpenCV.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/OpenCV.ipynb) 的盲文处理 7 个 cell（第 6–17 个 cell）。
- 想深入光流，读 [06-IntroCV/README.md:101-103](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/06-IntroCV/README.md#L101-L103) 推荐的 LearnOpenCV 光流教程。
- 想理解「图像作为数组」如何被卷积层消费，带着本讲的 `(H, W, 3)` 直觉去读 `07-ConvNets/README.md`。
