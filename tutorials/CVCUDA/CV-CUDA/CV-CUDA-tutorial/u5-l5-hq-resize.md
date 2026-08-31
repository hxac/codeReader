# 复杂算子案例二：HQResize 与 PillowResize 高质量缩放

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「高质量缩放」的质量差异**来自哪里**：为什么 `cvcuda.resize(LINEAR)` 缩小图像会产生摩尔纹（moiré），而 PillowResize / HQResize 不会。
2. 描述 PillowResize 的实现思路：按 Pillow 语义做**可分离两趟重采样**（先水平后垂直），滤镜支持域随缩小比例自动展宽（抗锯齿），并按 dtype × 通道 × 布局分派多种向量化内核。
3. 描述 HQResize 的实现思路：**逐轴自适应**地选择缩小/放大滤镜、用代价模型搜索轴处理顺序、系数表一次预计算后设备端查表插值，并为 2 倍缩放等特例提供直通单内核路径。
4. 按精度与性能需求，在 `resize` / `pillowresize` / `hq_resize` 三档缩放算子中做出正确选择，并会查它们的 Limitations 契约表。

本讲承接 [u5-l3](./u5-l3-legacy-vs-native.md) 的结论：legacy 内核（`priv/legacy/*.cu`）与原生内核（`priv/Op*.cu`）是仓库中两种并存的内核形态。本讲正好各取一个最重的代表——PillowResize（legacy 形态）与 HQResize（原生形态，内核头文件 4600 余行，是仓库中最大的单个内核文件），对照着看两种形态在组织复杂算法时的不同手法。

## 2. 前置知识

### 2.1 重采样 = 插值 + 滤波

把一幅 \( W_{in} \times H_{in} \) 的图像变成 \( W_{out} \times H_{out} \)，本质是**重采样**：对每个输出像素，在输入图像的连续坐标 \((x_c, y_c)\) 处估计一个值。最简单的估计是最近邻（取最近像素）和双线性（取 2×2 邻域加权）。`cvcuda.resize` 的 `NEAREST/LINEAR/CUBIC/AREA` 就是这类「点采样」插值。

### 2.2 混叠（aliasing）：普通插值缩小图像为什么会出条纹

图像里有高频细节（细密纹理、栅栏、文字笔画）。缩小图像相当于**降低采样率**。根据奈奎斯特定理，采样率低于信号最高频率两倍时，高频会「折叠」成低频伪影——摩尔纹、锯齿边缘、闪烁的条纹。双线性插值每个输出像素只看 2×2 邻域，**没有先滤掉即将越界的高频**，所以缩小越狠、伪影越重。

解决办法是**先低通滤波、再降采样**：缩小 \( k \) 倍时，让插值核的宽度随 \( k \) 展宽，等效于在采样前做了平均/平滑。这正是 Pillow 的 `LANCZOS/BICUBIC` 缩小、以及 torchvision `antialias=True` 的语义。两种算子的抗锯齿实现殊途同归：

- PillowResize：`filterscale = max(in/out, 1)`，滤镜支持域 `support = filter.support * filterscale`；
- HQResize：抗锯齿时半径按 `radius = k * inSize / outSize`（\( k \) 为滤镜固有半径）放大。

### 2.3 常见滤镜核

| 滤镜 | 支持域（半径） | 特点 |
|------|------|------|
| Box（盒式） | 0.5 | 平均，最简单的低通 |
| Bilinear/Triangular（三角） | 1 | 线性插值核 |
| Hamming | 1 | 加窗 sinc |
| Bicubic | 2 | 三次多项式，锐利 |
| Lanczos3 | 3 | \( \text{sinc}(x)\cdot\text{sinc}(x/3) \) 加窗，高质量缩小的金标准 |

Lanczos 核定义为（\( a=3 \)）：

\[
L(x) = \operatorname{sinc}(x)\cdot \operatorname{sinc}\!\left(\frac{x}{a}\right), \quad |x| < a;\qquad L(x)=0,\ |x|\ge a
\]

### 2.4 可分离滤波：两趟一维代替一趟二维

二维重采样核若是 \( x \)、\( y \) 两个方向核的乘积（可分离），就可以拆成两趟：先沿水平方向把每行重采样到 \( W_{out} \) 列（行数不变），把结果存入**中间缓冲**；再沿垂直方向重采样到 \( H_{out} \) 行。计算量从 \( O(k^2) \) 降为 \( O(2k) \)。代价是中间图像要在显存里走一趟「写 + 读」。两个算子的核心结构都是这个两趟流水，也都在想办法（融合内核）消掉这次中间往返。

### 2.5 衡量质量的指标

- **PSNR**（峰值信噪比）：\( \text{PSNR} = 10\log_{10}\!\left(\dfrac{255^2}{\mathrm{MSE}}\right) \)，单位 dB，越高越接近参考图。
- **SSIM**（结构相似性）：在局部窗口内比较亮度、对比度与结构的相似性，取值 \([-1,1]\)，越接近 1 越好。

本讲综合实践用「缩小再放大回原尺寸，与原图比 PSNR/SSIM」来量化三档算子的质量差。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [samples/operators/pillowresize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/pillowresize.py) | PillowResize 的 Python 用法样例 |
| [samples/operators/hq_resize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/hq_resize.py) | HQResize 的 Python 用法样例 |
| [src/cvcuda/include/cvcuda/OpPillowResize.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPillowResize.h) | PillowResize C API 与 Limitations 契约 |
| [src/cvcuda/include/cvcuda/OpHQResize.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpHQResize.h) | HQResize C API、形状/ROI 结构体与 Limitations 契约 |
| [src/cvcuda/priv/legacy/pillow_resize.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h) | 5 种滤镜类、系数预计算设备函数、融合开关（legacy 公共头） |
| [src/cvcuda/priv/legacy/pillow_resize.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu) | PillowResize 全部内核与两趟流水编排（legacy 形态） |
| [src/cvcuda/priv/OpPillowResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPillowResize.cpp) | PillowResize priv 包装：exportData、布局校验、转调 legacy |
| [src/cvcuda/priv/OpHQResize.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu) | HQResize priv 顶层：2D/3D 分派、planar 批展开 |
| [src/cvcuda/priv/OpHQResize2D.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize2D.cu) | 2D 实现类，持有滤镜工厂并委托 `HQResizeRun<2>` |
| [src/cvcuda/priv/OpHQResizeFilter.cuh](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh) | 滤镜类型体系、系数表工厂（按设备缓存） |
| [src/cvcuda/priv/OpHQResizeKernel.cuh](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh) | 4600 行内核主文件：SampleDesc、轴序搜索、逐趟重采样、直通路径 |
| [src/cvcuda/priv/OpHQResizePolicy.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizePolicy.hpp) | 2 倍缩放直通路径的按 SM 架构门禁 |

## 4. 核心概念与源码讲解

### 4.1 三档缩放算子：质量差异来自哪里

#### 4.1.1 概念说明

CV-CUDA 提供三档缩放，语义分别对齐三个业界参照物：

| 算子 | 对齐语义 | 抗锯齿 | 典型用途 |
|------|---------|--------|---------|
| `cvcuda.resize` | OpenCV `resize` | 仅 `AREA` 有类似效果 | 推理预处理，速度优先 |
| `cvcuda.pillowresize` | Pillow `Image.resize` | 所有滤镜在缩小时核自动展宽 | 与 CPU 端 Pillow 管线像素级对齐 |
| `cvcuda.hq_resize` | torchvision `interpolate(antialias=True)` 风格 | `antialias=True` 时对**缩小方向**生效 | 训练数据增强、高保真管线 |

质量差异的根源只有一条：**缩小（降采样）时是否先做低通**。`resize(LINEAR)` 的插值核固定 2×2，不管缩小多少倍；后两者的核宽随缩小比例展宽，把将要折叠的高频先平均掉。此外 Lanczos/Cubic 这类负瓣核还能保住边缘锐度，比纯平均更清晰。

#### 4.1.2 核心流程

三个算子的 Python 入口（样例代码，均来自仓库 samples）：

```python
# 普通 resize：输出形状需带通道维 (H, W, C)
out = cvcuda.resize(img, (h, w, 3))                       # 默认 LINEAR

# PillowResize：输出形状 (H, W, C) + 显式 Format + 滤镜
out = cvcuda.pillowresize(img, (h, w, img.shape[2]),
                          cvcuda.Format.RGB8, cvcuda.Interp.LANCZOS)

# HQResize：输出形状只要 (H, W)（空间维），双滤镜 + 抗锯齿开关
out = cvcuda.hq_resize(img, (h, w),
                       min_interpolation=cvcuda.Interp.LANCZOS,
                       mag_interpolation=cvcuda.Interp.LINEAR,
                       antialias=True)
```

#### 4.1.3 源码精读

PillowResize 样例（与 Pillow 输出逐像素对齐的注释直接写在代码里）：

[pillowresize.py:39-50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/pillowresize.py#L39-L50) —— 样例读入 RGB8 的 HWC 张量后，指定输出形状 `(H, W, C)`、格式 `RGB8` 与 `LANCZOS` 滤镜调用 `cvcuda.pillowresize`。注释明确说明：Pillow 风格 resize 使用高质量降采样滤镜（如 LANCZOS），比普通双线性更贴近 PIL/Pillow 的输出。

[hq_resize.py:39-53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/hq_resize.py#L39-L53) —— HQResize 样例展示它的三个特色参数：`min_interpolation`（缩小方向用 LANCZOS，避免摩尔纹）、`mag_interpolation`（放大方向用 LINEAR，快且平滑）、`antialias=True`（缩小时额外低通）。

两个算子的支持矩阵差异很大，写管线前必须查 C 头文件的 Limitations 契约表：

- [OpPillowResize.h:100-148](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPillowResize.h#L100-L148) —— PillowResize 支持 NHWC/HWC/NCHW/CHW 四种布局；交错布局通道 1–4，平面布局只支持 1/3/4（双通道平面不支持）；dtype 支持 8U/8S/16U/16S/32S/32F；**输入输出必须同布局、同 dtype、同通道数**，只有宽高可以不同。
- [OpHQResize.h:188-217](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpHQResize.h#L188-L217) —— HQResize 张量路径支持 `(N)(D)HW(C)` 交错布局与 NCHW 平面布局（平面仅限 2D），**通道数任意正整数**，dtype 支持 8U/16U/16S/32F；注意它**不支持 8S 与 32S**，与 PillowResize 互补；输出 dtype 可以与输入不同（相同或 float32）。

另外 HQResize 还有两个 PillowResize 没有的能力，定义在 [OpHQResize.h:62-73](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpHQResize.h#L62-L73)：浮点 ROI（`HQResizeRoiF`，按 (D)HW 序给出每轴的 `[lo, hi]`），以及 ROI 的 `lo > hi` 表示**沿该轴翻转**（[OpHQResize.h:245-248](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpHQResize.h#L245-L248)）——裁剪、翻转、缩放三个操作在这里合为一谈。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到三档算子的质量差异。
2. **操作步骤**：
   - 按 [u1-l2](./u1-l2-install-and-first-run.md) 安装 cvcuda wheel，进入仓库 `samples/operators/` 目录；
   - 准备一张高频细节丰富的图（栅栏、编织物、小字号文字）；
   - 依次运行：
     ```bash
     python3 resize.py --input detail.jpg --output out_linear.jpg --width 256 --height 256
     python3 pillowresize.py --input detail.jpg --out cat_pillowresize.jpg --width 256 --height 256
     python3 hq_resize.py --input detail.jpg --out cat_hq.jpg --width 256 --height 256
     ```
     （参数名以 `parse_image_args` 的定义为准，可用 `--help` 查看。）
3. **需要观察的现象**：把三张缩小图放大到 100% 对比。`out_linear.jpg` 上细密纹理应出现摩尔纹或彩色条纹；另外两张应明显干净、边缘更锐。
4. **预期结果**：LANCZOS/抗锯齿版本没有摩尔纹。具体的 PSNR/SSIM 排序见本讲第 5 节综合实践——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hq_resize` 要区分 `min_interpolation` 与 `mag_interpolation` 两个参数，而 `resize` 只有一个 `interp`？

**答案**：缩小时需要宽核低通防混叠（如 LANCZOS），放大时宽核只增加计算量却没有抗锯齿收益，反而 LINEAR 已经足够平滑；分开指定可以让一条管线在「缩小为主」和「放大为主」两种场景都拿到最优质量-性能折中。`resize` 面向推理预处理的速度敏感场景，不提供这种精细控制。

**练习 2**：你的输入是 `int32` 张量，想用高质量缩小，该选哪个算子？

**答案**：只能选 `pillowresize`（支持 32S）；`hq_resize` 不支持 32S。反之若输入是任意通道数（比如 7 通道特征图）则只能选 `hq_resize`（通道数任意正整数），`pillowresize` 最多 4 通道。两者 dtype/通道支持是互补而非包含关系。

### 4.2 PillowResize：Pillow 语义的可分离重采样（legacy 形态）

#### 4.2.1 概念说明

PillowResize 的目标是**逐像素复现 Pillow `Image.resize` 的输出**，这样 GPU 管线可以无缝替换 CPU 上的 Pillow 而不产生训练/推理数据分布差异。它的算法是教科书式的可分离重采样：

1. 对输出图的每一行/列，先在 GPU 上**预计算一组重采样系数**（每个输出位置一个权重窗口 `[xmin, xmin+xmax)` 及归一化权重）；
2. **水平趟**：输入 → 中间缓冲（宽变为 \( W_{out} \)，高仍是 \( H_{in} \)）；
3. **垂直趟**：中间缓冲 → 输出。

Pillow 的关键语义细节都藏在系数计算里：半像素中心对齐（`center = in0 + (xx + 0.5) * scale`）、权重按和归一化、以及缩小核展宽。

#### 4.2.2 核心流程

```
pillow_resize_v2<Filter, T>(in, out, workspace, ...)
├─ 1. 计算比例与核宽
│     h_scale = W_in / W_out          （>1 为缩小）
│     h_filterscale = max(h_scale, 1) （缩小时 >1，放大时钳到 1）★抗锯齿关键
│     h_support = filter.support * h_filterscale
│     h_k_size  = ceil(h_support)*2 + 1     （最大 tap 数）
├─ 2. 在 workspace 里手划区域
│     [h_kk | v_kk | h_bounds | v_bounds | 中间缓冲 d_h_data(16字节对齐)]
├─ 3. 两次 _precomputeCoeffs kernel：算出每个输出 x / y 的 [xmin, xmax) 与权重
├─ 4. 水平趟 kernel：in → intermediate (Ptr2dNHWC 或 Ptr2dNCHW)
└─ 5. 垂直趟 kernel：intermediate → out
      （float + 小核 + 特定架构时可融合为单个 fused_pass，跳过中间缓冲）
```

权重计算的数学（对输出位置 \( x_o \)）：

\[
c = x_{in,0} + (x_o + 0.5)\cdot s,\qquad s = \frac{W_{in}}{W_{out}}
\]

\[
x_{min} = \max\!\big(0,\ \lfloor c - \sigma + 0.5 \rfloor\big),\qquad
x_{max} = \min\!\big(W_{in},\ \lfloor c + \sigma + 0.5 \rfloor\big) - x_{min}
\]

其中 \( \sigma = \text{support}\cdot\max(s,1) \) 是展宽后的半宽。每个 tap 的原始权重 \( w_i = f\!\left((i + x_{min} - c + 0.5)\cdot \frac{1}{\text{filterscale}}\right) \)，再除以 \( \sum_i w_i \) 归一化——归一化保证亮度不因边缘截断而漂移。

#### 4.2.3 源码精读

**滤镜定义**（legacy 公共头，host/device 双侧可用）：

[pillow_resize.h:58-63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L58-L63) —— 5 种滤镜的支持域常量：box 0.5、bilinear 1、hamming 1、bicubic 2、lanczos 3。`support()` 越大，每个输出像素要读的输入 tap 越多、越贵也越抗锯齿。

[pillow_resize.h:184-217](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L184-L217) —— `LanczosFilter`：`filter(x) = sinc(x) * sinc(x/3)`（\( a=3 \)），与 Pillow 的 LANCZOS 完全一致。其余滤镜（Bilinear/Box/Hamming/Bicubic，[L65-182](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L65-L182)）同构，都实现 `filter(x)` 与 `support()` 两个方法，作为模板参数注入内核。

**系数预计算**（每个输出位置独立，天然并行）：

[pillow_resize.h:225-310](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L225-L310) —— `PillowPrecomputeCoeffs` 设备函数实现了 4.2.2 的全部数学：先按半像素中心算 `center`，再夹取 `[xmin, xmax)`，逐 tap 求 `filterp.filter(...)` 权重并累加 `ww`，随后把权重除以 `ww` 归一化，最后把窗口边界写进 `bounds_out`。张量路径与变长批路径**共用这一段数学**（两者只差参数来自内核参数还是逐图数组）。它的启动器是 [pillow_resize.cu:44-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L44-L55) 的 `_precomputeCoeffs` 内核。

**主流程编排**：

[pillow_resize.cu:797-841](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L797-L841) —— `pillow_resize_v2` 的开头：第 809-810 行算 `h/v_scale`；第 815-822 行把 `filterscale` 钳到不小于 1——**这一行就是抗锯齿的全部秘密**，缩小时 `filterscale = in/out > 1`，于是第 825-826 行的 `support` 按比例展宽、第 829-830 行的 `k_size`（tap 数）随之变大；第 832-841 行在 workspace 中依次划出水平/垂直系数表、边界表与 16 字节对齐的中间缓冲（对齐是为了后面的向量读写）。

[pillow_resize.cu:872-886](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L872-L886) —— 两次 `_precomputeCoeffs` 启动：一次给所有输出列（水平系数），一次给所有输出行（垂直系数），随后两趟重采样 kernel 直接查表，不再重复算三角函数。

**两趟内核**：

[pillow_resize.cu:70-139](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L70-L139) —— `horizontal_pass` 内核。每个线程负责一个输出像素：查 `h_bounds` 得窗口，把窗口内输入像素加权和写入中间缓冲。文件头部 [L57-69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L57-L69) 的注释解释了关键优化：模板参数 `NC` 是编译期通道数，交错布局下整个像素（NC 个通道连续存放）作为**一个 `MakeType<T,NC>` 向量**一次读入、在 `MakeType<work_type,NC>` 寄存器向量里累加，把每通道一次的标量寻址折叠成一次宽访问；`NC == 0` 则退回逐通道标量循环（planar 布局用）。

[pillow_resize.cu:314-381](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L314-L381) —— `vertical_pass` 内核，结构同水平趟，沿行方向逐 tap 累加。

**性能变体（legacy 文件里也能长出精细分支）**：

[pillow_resize.cu:898-919](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L898-L919) —— `launch` lambda 里的融合决策：`can_fuse`（float + 向量化路径）、`is_upscale`、`small_kernel`（`k_size <= 5`，见 [pillow_resize.h:38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L38) 的 `kPillowResizeFuseMaxKSize`）共同决定是否走 `fused_pass` 单内核。注释交代了原因：融合省掉中间往返，但要为每个垂直 tap 重算水平求和；放大普遍划算，缩小只在部分架构划算，且**只对 float 融合**——整数中间结果要在两趟之间量化（Pillow 语义），byte 行是发射受限而非带宽受限，融合实测慢 25-41%。

[pillow_resize.h:40-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.h#L40-L56) —— `PillowResizeSupportsFusedDownscale`：缩小融合只在计算能力 8/9（Ampere/Hopper）开启，并按设备缓存结果。u8-l4 会讲到这种「按实测数据设架构门禁」是仓库优化纪律的常态。

[pillow_resize.cu:924-977](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L924-L977) —— 更多分支：三通道 byte 像素缩小时走 `horizontal_pass_paired`（[L147-238](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L147-L238)，相邻两输出的 tap 窗口重叠，共享段只读一次、两份累加，且保持与逐像素版本**位级一致**的三段循环划分）；垂直趟在中间/输出缓冲向量对齐时走 `vertical_pass_vec`（[L389-400](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L389-L400) 起，利用「垂直权重只依赖 dst_y」把整行展平后按 VEC=4 向量化）。

**入口校验与分派**：

[pillow_resize.cu:1200-1264](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1200-L1264) —— `PillowResize::infer`：校验输入输出格式一致、布局属于四种合法值、通道 ≤ 4、dtype 合法；[L1248-1264](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1248-L1264) 还有一处值得学习的防御：内核用 32 位乘积寻址，任何张量的字节范围超过 `INT32_MAX` 会溢出，于是显式拒绝并提示「拆小批量重提」。

[pillow_resize.cu:1266-1287](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1266-L1287) —— 插值枚举到滤镜类的分派：LINEAR→Bilinear、CUBIC→Bicubic、LANCZOS→Lanczos、BOX→Box、HAMMING→Hamming；再由 [pillow_resize_filter](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1149-L1178)（L1149-1178）按 dtype 实例化 `pillow_resize_v2<Filter, T>`。

[pillow_resize.cu:1180-1198](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1180-L1198) —— `getWorkspaceRequirements`：workspace 大小 = 水平/垂直系数与边界表 + 中间缓冲 \( N \cdot C \cdot H_{in} \cdot W_{out} \cdot \text{DataSize} \) + 16 字节对齐余量。注意中间缓冲形状是「源高 × 输出宽」，这就是水平趟刚结束时的图像形状。

priv 包装层很薄：[OpPillowResize.cpp:73-89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPillowResize.cpp#L73-L89) 做的正是 [u5-l2](./u5-l2-tensor-data-access.md) 讲过的 `exportData<TensorDataStridedCuda>` + 判空拦截；[L91-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPillowResize.cpp#L91-L123) 对平面布局额外校验通道与布局匹配后转调 legacy（planar 由 legacy 内核原生处理：每样本一个 z 切片，通道平面在内核里循环，使每输出的滤镜初始化在通道间摊销）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手算出两组典型参数下的核宽，建立「缩小多少倍、代价涨多少」的量化直觉。
2. **操作步骤**：
   - 打开 [pillow_resize.cu:809-830](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L809-L830)，对照公式手算两组：
     - 4096→1024（4 倍缩小）+ LANCZOS：\( s=4 \)，\( \sigma = 3\times4=12 \)，\( k = \lceil 12\rceil \times 2 + 1 = 25 \) tap；
     - 512→1024（2 倍放大）+ LANCZOS：\( s=0.5 \)，filterscale 钳到 1，\( \sigma = 3 \)，\( k = 7 \) tap；
   - 再用 BOX（support 0.5）重算 4 倍缩小的 \( k \) 值；
   - 最后在源码里找到 `k_size` 如何决定循环长度（[horizontal_pass 内核](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L86-L137) 中 `xmax` 即实际 tap 数，`k_size` 是每线程系数槽位的上界）。
3. **需要观察的现象**：缩小时 tap 数与缩小倍数成正比（LANCZOS 4 倍缩小 25 tap vs 放大 7 tap）；BOX 因 support 只有 0.5，4 倍缩小时 \( k=5 \)。
4. **预期结果**：理解「抗锯齿的代价」——同样的 LANCZOS，缩小 4 倍比放大 2 倍贵约 3.5 倍访存。若你的手算与源码行为不符（例如想用调试器验证），可给 `_precomputeCoeffs` 加打印后本地运行验证——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么中间缓冲形状是 \( N \cdot C \cdot H_{in} \cdot W_{out} \)，而不是 \( N \cdot C \cdot H_{out} \cdot W_{out} \)？

**答案**：因为水平趟先执行：它只改变宽度（\( W_{in} \to W_{out} \），高度仍是 \( H_{in} \)，所以中间图像是「源高 × 输出宽」。若交换两趟顺序，中间形状就是「输出高 × 源宽」。两种顺序的中间缓冲大小可能差很多——这正是 HQResize 用代价模型选轴序的动机（见 4.3）。

**练习 2**：为什么融合单内核路径「只对 float 开放」，整数类型被排除？

**答案**：Pillow 语义下，整数图像的两趟重采样在中间结果处要**量化回整数**（水平趟结束时 SaturateCast 到元素类型）。融合内核没有中间缓冲，无法复现这次量化，会改变输出；而 float 中间结果本就无损，融合可以做到与可分离路径位级一致。

**练习 3**：`vertical_pass_vec` 为什么能按整行展平做 4 元素向量化，而水平趟不能？

**答案**：垂直趟的权重只依赖输出行号 `dst_y`——同一输出行内所有（列 × 通道）标量共享同一组 `v_k` 权重，内存上又连续，因此可以按 VEC=4 连续元素向量化访存。水平趟的权重随输出列变化，相邻标量属于不同权重窗口，只能按「整像素向量」（`MakeType<T,NC>`）优化。

### 4.3 HQResize：逐轴自适应的原生重采样（原生 .cu 形态）

#### 4.3.1 概念说明

HQResize 是一个**通用 N 维（2D/3D）重采样器**，语义风格对齐 torchvision 的 `interpolate(antialias=True)`。它与 PillowResize 解决同一个问题（高质量缩放），但设计出发点不同：

- **逐轴独立决策**：每个空间轴根据自己的缩小/放大状态，独立选用 `min` 还是 `mag` 滤镜；`antialias` 只对缩小的轴生效。
- **轴序优化**：先处理哪个轴不是写死的，而是用一个代价模型在所有轴排列中搜索，最小化「中间缓冲 × 滤波计算量」的总量。
- **系数表查表**：滤镜不是在内核里现算解析式，而是初始化时**离散化成系数表**存进设备内存，内核里用线性插值查表。
- **直通特例**：对精确 2 倍放大/缩小等常见场景提供不经过中间缓冲的单内核快路径，并用按 SM 架构的实测门禁决定是否启用。

它也是仓库中少有的、入口带 **workspace 协商**三件套（精确需求 / 变长批需求 / 最大需求）的算子，因为中间缓冲必须由调用方按形状预先备好。

#### 4.3.2 核心流程

```
cvcuda.hq_resize(src, (H,W), min, mag, antialias)
 └─ priv HQResize (OpHQResize.cu)
     ├─ 按 layout 判断 2D/3D（layout 含 'D' 则 3D）
     ├─ HQResizeImpl2D → kernel::HQResizeRun<2>
         ├─ SetupSampleDesc
         │   ├─ ParseROI（lo>hi ⇒ 该轴翻转）
         │   ├─ SetupFilters：逐轴 out<in ? minFilter : magFilter ★逐轴自适应
         │   │     抗锯齿时 radius = k·in/out（核随缩小比例展宽）
         │   ├─ AdjustRoiForFilter（滤镜"光晕"外扩 + 夹取）
         │   └─ ProcessingOrderCalculator：DFS 搜索代价最小的轴序
         ├─ workspace：ndim-1 个 float 中间缓冲（2D 一个、3D 两个）
         └─ RunPasses
             ├─ 先试 direct 直通内核（2x2 线性 / 2x cubic 收缩/放大…）
             └─ RunPass<0>(in → inter) ；RunPass<1>(inter → out)
```

代价模型的每趟成本（[OpHQResizeKernel.cuh:2987-2994](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L2987-L2994)）：

\[
\text{cost}_{pass} = \alpha_{axis}\cdot \sigma_{axis}\cdot V_{curr} + 3\,V_{curr}
\]

其中 \( V_{curr} \) 是该趟开始时的中间图像体积（已处理轴取输出尺寸、未处理轴取输入 ROI 尺寸），\( \sigma_{axis} \) 是该轴滤镜支持域，\( \alpha \) 是轴系数（y 轴 1.0 最便宜、x 轴 1.4、深度轴 1.2——y 轴沿行方向访存更友好）。DFS 带剪枝地枚举全部轴排列取总代价最小者。直觉上它总会**先处理输出更小的那个轴**，让中间图像尽早变小。

#### 4.3.3 源码精读

**顶层分派**：

[OpHQResize.cu:181-184](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L181-L184) —— 构造函数创建 `HQResizeImpl`，后者在 [L91-95](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L91-L95) 同时持有 2D 与 3D 两个实现（`makeImpl2D()/makeImpl3D()`）。

[OpHQResize.cu:125-132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L125-L132) —— 张量入口按 `src.layout().find('D') >= 0` 判断是否 3D（DHW 布局），再由 [implForNDim](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L163-L171)（L163-171）路由，维度不是 2/3 则抛异常。

[OpHQResize.cu:40-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L40-L84) —— `ExpandPlanarTensorBatch`：平面（NCHW/CHW）TensorBatch 被展开成一批单通道 `(H,W,1)` HWC 视图（每「样本 × 通道」一个），视图用 `TensorWrapData` **零拷贝地别名原显存**。因为 HQResize 各通道独立，逐平面跑交错路径与原生平面路径位级一致，不用新写内核。[L142-160](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize.cu#L142-L160) 的 TensorBatch 入口据此分流；张量入口在内核内部做同样的平面视图（[OpHQResizeKernel.cuh:3227-3237](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3227-L3237)）。

**滤镜体系与系数表工厂**：

[OpHQResizeFilter.cuh:46-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L46-L67) —— 内部滤镜类型：`Nearest/Linear/Triangular/Gaussian/Cubic/Lanczos3`。注意 `Triangular` 专门表示「Linear + 抗锯齿」；`FilterTypeKind` 再把内核分成三族：Nearest、Linear、以及所有需要系数进共享内存的 `ShmFilter`——这是内核分派的第一层开关。

[OpHQResizeFilter.cuh:93-133](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L93-L133) —— `GetFilterMode` 把公开的插值枚举翻译成内部滤镜，`antialias && Linear` 升级为 `Triangular`；`GetFilterModes` 生成 (min, mag) 一对，**放大方向强制 `antialias=false`**——放大不会混叠，抗锯齿只是白费计算。

[OpHQResizeFilter.cuh:135-167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L135-L167) —— `ResamplingFilter`：一个 POD「系数表视图」`{coeffs, numCoeffs, anchor, scale}`，设备端 `operator()(x)` 用 `__ldg` 取相邻两个表项做**线性插值**。也就是说连续滤镜函数被离线采样成表，内核里查表即可，无需逐 tap 求 sinc。

[OpHQResizeFilter.cuh:237-296](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L237-L296) —— `ResamplingFiltersFactory`：四张系数表（三角 3、高斯 65、三次 129、Lanczos3 = 2·3·32+1 = 193 项）在 host 上按解析式初始化一次；`rescale(support)` 只改 `scale/anchor` 两个标量就完成了「核展宽」——同一张表适配任意支持域。`CreateCubic/Gaussian/Lanczos3/Triangular` 是带半径的构造入口。

[OpHQResizeFilter.cuh:298-355](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L298-L355) —— `DeviceFilterState`：系数表**按设备**缓存（`PerDeviceResource`），每设备分配 pinned + device 两份内存，host 初始化后 `cudaMemcpy` 到设备，再把 `coeffs` 指针统一偏移成设备地址。多 GPU 场景各卡各一份，这正是 [u4-l3](./u4-l3-multi-stream-thread-gpu.md) 讲过的「持久缓冲按卡重分配」原则的实例。

[OpHQResizeFilter.cuh:357-396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L357-L396) —— `GetResamplingFilter`：抗锯齿半径的统一公式，如 Lanczos3 缩小时 `radius = 3·inSize/outSize`、放大时钳回 3；与 PillowResize 的 `filterscale` 机制殊途同归。

**逐轴描述符与轴序搜索**：

[OpHQResizeKernel.cuh:104-145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L104-L145) —— `SampleDesc`：一次重采样的完整计划——`shapes[0..ndim]`（输入、各中间、输出的形状）、通道数、`processingOrder`（每趟处理哪个轴）、按**趟序**记录的 `origin/scale/filterKind/filter`、ROI 偏移与逐趟 block 形状。整个计划是平凡可拷贝的 POD，变长批路径会把它整批上传到设备端供内核按样本索引读取。

[OpHQResizeKernel.cuh:4407-4430](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L4407-L4430) —— `SetupFilters`：逐轴判断 `outSize < inSize ? minFilter : magFilter`——这就是「双滤镜」参数的落地处；支持域超出共享内存上限时还会 `rescale` 收缩。

[OpHQResizeKernel.cuh:4320-4376](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L4320-L4376) —— `SetupSampleDescFilterShapeScale`：解析 ROI（支持 lo>hi 翻转）、按轴配滤镜、为滤镜「光晕」外扩并夹取 ROI、调用 `SetupProcessingOrder` 得轴序，最后**按趟序**填出每趟的输出形状与 `origin/scale`。第 4343-4374 行的循环值得细读：中间形状从 ROI 尺寸出发，每处理一个轴就把该轴替换成输出尺寸——`shapes[pass+1]` 就是第 pass 趟结束时图像的样子。

[OpHQResizeKernel.cuh:2932-2999](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L2932-L2999) —— `ProcessingOrderCalculator`：DFS 枚举全部轴排列（带 `totalCost >= m_minCost` 剪枝），代价函数见 4.3.2 的公式。`PassCost` 的注释点明 y 轴（axis 1）系数最低，因为沿行访存更便宜。

**workspace 与两趟执行**：

[OpHQResizeKernel.cuh:3078-3098](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3078-L3098) —— `HQResizeRun` 类头部：`kNumTmpBuffers = kSpatialNDim - 1`（2D 一个中间缓冲、3D 两个）；中间元素类型按最大 4 通道的向量对齐。

[OpHQResizeKernel.cuh:3100-3132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3100-L3132) —— 张量路径的 workspace 需求计算：先用与执行时**完全相同**的 `SetupSampleDesc` 推出每趟输出体积，再加总中间缓冲——保证「协商」与「执行」两处的形状推导绝不漂移。变长批版本（[L3134-3185](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3134-L3185)）逐样本累加，还要为设备端 `SampleDesc` 数组与动态批包装元数据留空间；上界版本（[L3190-3218](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3190-L3218)）按最大形状估算，供复用。

[OpHQResizeKernel.cuh:3546-3588](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3546-L3588) —— 2D `RunPasses`：先用 `TensorNDWrap` 把张量数据包成 POD 包装器（[u5-l2](./u5-l2-tensor-data-access.md) 讲过的轻量寻址层）；接着**依次尝试一串直通内核**（2×2 线性、通用线性、2×2 滤镜、2 倍收缩滤镜、通用滤镜直通），任何一个命中就直接返回；否则走标准两趟：`RunPass<0>(in → intermediate)`、`RunPass<1>(intermediate → out)`。3D 版本（[L3592-3622](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3592-L3622)）三趟两个中间缓冲，同构。

[OpHQResizePolicy.hpp:23-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizePolicy.hpp#L23-L67) —— 直通路径的分类器：只认「精确 2 倍、零原点」的线性放大 / 三次收缩 / 三次放大三类几何；[L69-93](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizePolicy.hpp#L69-L93) 的 `UseDirectTensorPathForSM` 再按 SM 架构（如 Blackwell sm100/103、Turing sm75）逐场景门禁——注释和文件末尾的 `static_assert` 网格（[L100-113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizePolicy.hpp#L100-L113)）把这些实测结论固化成编译期断言，防止后人无意改动。

内核文件中还有一类「寄存器融合」直通内核：[OpHQResizeKernel.cuh:1863-1865](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L1863-L1865) 的注释说明两趟重采样在寄存器里按派发顺序求值、float 中间结果不落显存、结果与可分离路径一致——同一个「消中间缓冲」的目标，比 PillowResize 的 `fused_pass` 覆盖面更广。

**dtype × 通道的编译期分派**：

[OpHQResizeKernel.cuh:3001-3024](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3001-L3024) —— `RunTypedSwitch` 开头：通道数 1/2/3/4 走**静态通道**实例化（源/中间/目的都用 `Vec<T, N>` 向量类型，中间恒为 float），其他通道数退回动态通道（`-1`）版本。这与 PillowResize 用 `MakeType<T,NC>` 的思路相同，但由统一的宏开关管理，是原生形态在「类型分派」上更体系化的体现。

**2D 实现装配**：

[OpHQResize2D.cu:27-66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize2D.cu#L27-L66) —— `HQResizeImpl2D` 持有一个 `filter::ResamplingFiltersFactory` 成员（[L87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResize2D.cu#L87)），三种 `getWorkspaceRequirements` 重载与三种 `operator()` 全部现场构造 `HQResizeRun<2>` 并转发——运行器是无状态的计划执行器，状态（系数表）只在工厂里。

#### 4.3.4 代码实践（源码阅读型：跟踪一次调用的计划推导）

1. **实践目标**：对一组具体参数，人工复现 `SetupSampleDesc` 的推导，验证你对轴序与中间缓冲的理解。
2. **操作步骤**：
   - 场景：输入 `480×640`（H×W），输出 `240×1280`（**高度缩小、宽度放大**），`min=LANCZOS+antialias`、`mag=LINEAR`；
   - 打开 [SetupFilters](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L4407-L4430)：H 轴 240<480 用 min 滤镜（Lanczos3，抗锯齿半径 3·2=6）；W 轴 1280>640 用 mag 滤镜（Linear，无抗锯齿）；
   - 用代价公式估算两种轴序：先 H 后 W，中间体积 \( 240\times640 \)；先 W 后 H，中间体积 \( 480\times1280 \)——后者大 4 倍，两趟的 \( V_{curr} \) 与都更大，预期 `processingOrder` 先处理 H 轴；
   - 写出两趟计划：趟 0 输入 480×640 → 中间 240×640；趟 1 中间 → 输出 240×1280。
3. **需要观察的现象**：轴序选择让中间缓冲取两个方向中更小者；逐轴滤镜选择使缩小轴宽核、放大轴窄核。
4. **预期结果**：你的推导与 `SetupSampleDescFilterShapeScale` 填出的 `shapes[]`、`filter[]` 一致。如需实证，可在 [RunPasses](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeKernel.cuh#L3546-L3588) 里对 `sampleDesc.shapes` 加打印后构建运行——**待本地验证**（需要按 [u1-l3](./u1-l3-build-from-source.md) 从源码构建）。

#### 4.3.5 小练习与答案

**练习 1**：HQResize 为什么把滤镜系数预计算成表并在内核里线性插值查表，而不是像 PillowResize 那样在设备上现算？

**答案**：PillowResize 的系数预计算也是一个独立内核（`_precomputeCoeffs`），其实两家都避免在主内核里逐 tap 算解析式。HQResize 更进一步：它的滤镜要**按轴动态 rescale**（抗锯齿半径随缩小比例变化），如果为每种半径都现算就要在每个输出 tap 上求 sinc；系数表 + `rescale()` 只改两个标量，任何支持域共享同一张表，内核里一次线性插值即可取值，把三角函数彻底移出热循环。

**练习 2**：`antialias=True` 时放大方向会怎样？

**答案**：什么也不发生。[GetFilterModes](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L125-L133) 对 mag 滤镜强制 `antialias=false`，且 [GetResamplingFilter](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpHQResizeFilter.cuh#L357-L396) 里半径展宽只在 `outSize < inSize` 时生效——放大不产生混叠，展宽核纯属浪费。

**练习 3**：为什么 HQResize 需要 `GetMaxWorkspaceRequirements` 这样的「上界」接口，而 `cvcuda.flip` 完全没有 workspace 概念？

**答案**：flip 是逐像素原地可算的算子，不需要中间缓冲；HQResize 的可分离实现必须有中间图像（2D 一个、3D 两个），大小依赖输入输出形状。Python 绑定（[OpHQResize.cpp:388](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpHQResize.cpp#L388) 附近）会按形状协商并缓存需求；上界接口让调用方「一次分配、多种形状复用」，形状多变的管线不必反复分配。这与 [u8-l3](./u8-l3-workspace-and-stream-cache.md) 将讲的 Workspace 生命周期机制直接衔接。

### 4.4 两种实现的工程组织对比与选型

#### 4.4.1 概念说明

把 4.2 与 4.3 并排看，能提炼出 legacy 形态与原生形态在组织复杂算法时的系统性差异——这是对 [u5-l3](./u5-l3-legacy-vs-native.md) 「两种内核形态」结论的具体印证。

#### 4.4.2 核心流程（对比表）

| 维度 | PillowResize（legacy） | HQResize（原生 .cu） |
|------|------------------------|----------------------|
| 语义参照 | Pillow `Image.resize` | torchvision `interpolate(antialias=True)` 风格 |
| 维度 | 2D 图像 | 2D + 3D（DHW 体数据） |
| 滤镜选择 | 单滤镜（LINEAR/CUBIC/LANCZOS/BOX/HAMMING） | 双滤镜（min/mag）+ `antialias` + NEAREST/LINEAR/CUBIC/LANCZOS/GAUSSIAN |
| 轴处理顺序 | 固定：先水平后垂直 | 代价模型搜索（`ProcessingOrderCalculator`） |
| 系数来源 | 每次调用跑 `_precomputeCoeffs` 内核现算 | 工厂一次性初始化系数表，内核线性插值查表，按设备缓存 |
| 中间缓冲 | workspace 里 1 个（源高 × 输出宽） | `ndim-1` 个 float 缓冲，大小由计划推导，支持上界复用 |
| 平面布局 | legacy 内核原生处理（z 切片按样本） | 展开成单通道视图走交错路径（零拷贝别名） |
| dtype（见 Limitations） | 8U/8S/16U/16S/32S/32F | 8U/16U/16S/32F（输出可 float32） |
| 通道 | 交错 1–4、平面 1/3/4 | 张量任意正整数、图像批 1–4 |
| 特化路径 | float 融合内核、配对水平趟、行向量化 | 2 倍缩放直通内核族 + 按 SM 门禁 + 寄存器融合 |
| 入口 | Tensor / ImageBatchVarShape | Tensor / ImageBatchVarShape / TensorBatch，含 ROI 与翻转 |
| 额外能力 | — | 浮点 ROI、lo>hi 翻转、输出可升 float32 |

选型速查：

- **要和 CPU 端 Pillow 逐像素一致**（训练/推理分布对齐）→ `pillowresize`，别无选择。
- **训练数据增强、需要 antialias 语义或 3D 体数据** → `hq_resize`。
- **推理预处理、速度优先、可接受轻微混叠** → `resize`；对纹理敏感的输入至少升到 `resize(AREA)` 缩小或 `hq_resize(antialias=True)`。
- **dtype/通道不在支持矩阵内** → 查上表与对应 C 头 Limitations，两家支持集是互补的。

#### 4.4.3 源码精读

补充一处前面未展开的证据——两家的测试基线不同，这往往最能说明语义目标：

- [tests/cvcuda/system/TestOpPillowResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpPillowResize.cpp) 以 CPU 黄金参考校验 Pillow 语义（u7-l1 将展开 system 测试体系）；
- [tests/cvcuda/system/TestOpHQResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpHQResize.cpp) 覆盖双滤镜、ROI/翻转与 3D 路径；
- Python 侧有 [tests/cvcuda/python/test_oppillowresize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_oppillowresize.py) 与基准 [bench/python/ops/bench_hqresize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_hqresize.py)、[bench/python/ops/bench_pillowresize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_pillowresize.py)。

#### 4.4.4 代码实践

1. **实践目标**：把选型表沉淀为可复现的实测数据。
2. **操作步骤**：运行两个基准（见 [bench/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md) 的运行方式），对同一形状分别记录 `resize`、`pillowresize(LANCZOS)`、`hq_resize(LANCZOS+antialias)` 的吞吐。
3. **需要观察的现象**：LANCZOS 类缩小比 LINEAR resize 慢数倍（tap 数从 4 涨到 25 左右）；放大时差距缩小。
4. **预期结果**：得到自己硬件上的「质量-性能」价目表。具体倍数**待本地验证**（依赖 GPU 型号与形状）。

#### 4.4.5 小练习与答案

**练习**：你有一条「4 倍缩小 → 分类推理」的管线，输入是 uint8 RGB、batch=32、吞吐不达标。给出优化路径。

**答案**：① 若混叠不影响精度，先试 `resize(AREA)` 或 `hq_resize(min=LINEAR+antialias)`，把每输出 tap 从 ~25 降到 ~8；② 保持 LANCZOS 时确认走的是向量化交错路径（输入布局 NHWC、行距对齐，见 [pillow_resize.cu:1108-1123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/pillow_resize.cu#L1108-L1123) 的对齐门禁）；③ 用 `hq_resize` 时考虑输出直接升 float32 省一次 `convertto`；④ 形状固定则用 `_into` 变体 + 预分配，避免每帧分配（u3-l3）；⑤ 基准前后对比，遵守 [u8-l4](./u8-l4-op-toolchain.md) 的优化纪律。

## 5. 综合实践

**任务**：把同一张图分别用三档算子「缩小再放大回原尺寸」，计算与原图的 PSNR/SSIM 并排序，再测各自的耗时，验证质量-性能权衡。

下面是完整的示例脚本（**示例代码**，非仓库自带；PSNR/SSIM 用纯 numpy 实现，SSIM 为标准均匀窗口 8×8 版本，如装了 scikit-image 可换 `skimage.metrics` 得更严格的结果）：

```python
# hq_vs_pillow_quality.py —— 三档缩放算子的质量/性能对比（示例代码）
import sys, time
from pathlib import Path
import numpy as np
import cvcuda

sys.path.append(str(Path(__file__).parent.parent / "samples"))
from common import read_image  # noqa: E402  仓库自带：读图 → RGB8 HWC Tensor（GPU）

IN, SMALL = "detail.jpg", (256, 256)  # 缩小目标 (H, W)

def to_np(t):        # GPU Tensor → CPU numpy（同步拷贝）
    return t.cpu().numpy() if hasattr(t, "cpu") else np.asarray(t)

def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / mse) if mse > 0 else float("inf")

def ssim(a, b):      # 8×8 均匀窗口简化版；严格版建议用 scikit-image
    from numpy.lib.stride_tricks import sliding_window_view
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    a, b = a.astype(np.float64), b.astype(np.float64)
    wa = sliding_window_view(a, (8, 8, 1))[::8, ::8]
    wb = sliding_window_view(b, (8, 8, 1))[::8, ::8]
    mu_a, mu_b, s = wa.mean(axis=(-1, -2)), wb.mean(axis=(-1, -2)), 8 * 8
    va, vb = wa.var(axis=(-1, -2)), wb.var(axis=(-1, -2))
    cov = ((wa - mu_a[..., None, None]) * (wb - mu_b[..., None, None])).mean(axis=(-1, -2))
    m = ((2 * mu_a * mu_b + C1) * (2 * cov + C2)) / \
        ((mu_a ** 2 + mu_b ** 2 + C1) * (va + vb + C2))
    return m.mean()

src = read_image(IN)                      # uint8 HWC RGB，在 GPU 上
H, W, C = src.shape
h2, w2 = SMALL
stream = cvcuda.Stream()

def down_up(name, down, up):
    with stream:
        small = down(src)
        back = up(small)
    stream.sync()
    a, b = to_np(src).astype(np.uint8), to_np(back).astype(np.uint8)
    # 计时：同流重复 50 次取平均（含 allocating 分配；紧循环可用 _into 变体）
    t0 = time.perf_counter()
    for _ in range(50):
        with stream:
            up(down(src))
        stream.sync()
    dt = (time.perf_counter() - t0) / 50
    print(f"{name:28s} PSNR={psnr(a, b):6.2f}dB  SSIM={ssim(a, b):.4f}  {dt*1e3:7.3f} ms")
    return psnr(a, b), ssim(a, b), dt

jobs = {
    "resize(LINEAR)": (
        lambda t: cvcuda.resize(t, (h2, w2, C)),
        lambda t: cvcuda.resize(t, (H, W, C))),
    "resize(CUBIC)": (
        lambda t: cvcuda.resize(t, (h2, w2, C), cvcuda.Interp.CUBIC),
        lambda t: cvcuda.resize(t, (H, W, C), cvcuda.Interp.CUBIC)),
    "pillowresize(LANCZOS)": (
        lambda t: cvcuda.pillowresize(t, (h2, w2, C), cvcuda.Format.RGB8, cvcuda.Interp.LANCZOS),
        lambda t: cvcuda.pillowresize(t, (H, W, C), cvcuda.Format.RGB8, cvcuda.Interp.LANCZOS)),
    "hq_resize(LANCZOS+aa)": (
        lambda t: cvcuda.hq_resize(t, (h2, w2), min_interpolation=cvcuda.Interp.LANCZOS,
                                   mag_interpolation=cvcuda.Interp.LANCZOS, antialias=True),
        lambda t: cvcuda.hq_resize(t, (H, W), min_interpolation=cvcuda.Interp.LANCZOS,
                                   mag_interpolation=cvcuda.Interp.LANCZOS, antialias=True)),
}
for name, (d, u) in jobs.items():
    down_up(name, d, u)
```

操作步骤与观察点：

1. 选一张 1920×1080 以上、含细密纹理的图（要能暴露混叠）。
2. 运行脚本，记录四个算子的 PSNR、SSIM、耗时。
3. **预期排序（待本地验证）**：SSIM 大致 `pillowresize(LANCZOS) ≈ hq_resize(LANCZOS+aa) > resize(CUBIC) > resize(LINEAR)`；耗时反之。注意「缩小再放大」是**有损往返**，PSNR 排序同时受缩小核与放大核影响——这正是要双滤镜的 `hq_resize` 在两个方向都用 LANCZOS 的原因。
4. 进阶：把放大核统一改成 LINEAR（只比缩小质量），观察 LANCZOS 缩小带来的 SSIM 提升主要出现在哪个方向；再把输入换成平滑渐变图（无高频），看三档差距是否缩小——验证「质量差异来自抗锯齿」这一论断。
5. 若 `resize` 的 `CUBIC` 传参方式与本脚本不符，以 [samples/operators/resize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize.py#L44-L46) 与 Python docstring 为准调整。

## 6. 本讲小结

- 高质量缩放与普通缩放的唯一本质差异：**降采样前是否先低通**。PillowResize 用 `filterscale = max(in/out, 1)` 展宽核，HQResize 用 `radius = k·in/out` 展宽核，机制殊途同归。
- **PillowResize**（legacy 形态）＝ Pillow 逐像素语义：两趟可分离重采样（先水平后垂直）＋ 每次调用跑系数预计算内核；交错路径按 `MakeType<T,NC>` 整像素向量化，另有配对水平趟、行向量化垂直趟、float 融合内核等按实测门禁的性能变体。
- **HQResize**（原生形态）＝ 逐轴自适应：每轴独立选 min/mag 滤镜、抗锯齿只作用于缩小轴；`ProcessingOrderCalculator` 用代价模型搜索轴序以最小化中间缓冲与计算量；滤镜离散成系数表按设备缓存，内核线性插值查表；2 倍缩放等特例有直通单内核路径并按 SM 架构门禁。
- 两家的**支持矩阵互补**（dtype：pillow 有 8S/32S 而 hq 没有；通道：hq 任意而 pillow ≤4），写管线前查 C 头 Limitations 契约表。
- HQResize 是带 **workspace 协商**的算子（精确/变长批/上界三种需求接口），中间缓冲必须由调用方按形状准备——通往 u8-l3 的 Workspace 缓存机制。
- 性能特化分支（融合、配对、直通、按 SM 门禁）全都用注释记录实测数据与理由——这是仓库「先基准后改码」纪律在源码里的化石。

## 7. 下一步学习建议

- 下一讲 [u5-l6](./u5-l6-computer-vision-analysis-ops.md) 转向另一类复杂算子：SIFT、PairwiseMatcher、FindHomography 组成的特征匹配管线，看多阶段 kernel 的流水组织。
- 想深挖 workspace 生命周期（谁分配、谁回收、跨流怎么安全）：提前阅读 [src/cvcuda/include/cvcuda/Workspace.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/Workspace.hpp)，并对照 [python/mod_cvcuda/WorkspaceCache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/WorkspaceCache.cpp)（u8-l3 的主题）。
- 想验证自己的理解：读 [tests/cvcuda/system/TestOpHQResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpHQResize.cpp) 的参数化循环，找出哪个用例覆盖了「lo>hi 翻转」与 3D 路径。
- 想动手优化：按 [u7-l3](./u7-l3-benchmarks.md) 跑 `bench_hqresize.py`/`bench_pillowresize.py`，再用 [u7-l4](./u7-l4-nvtx-and-profiling.md) 的 NVTX 手段确认两趟内核与直通路径的命中情况。
