# 圆柱面投影的数学原理：映射变换与相机参数

## 1. 本讲目标

本讲是进阶算法主线的第二篇，承接 [u2-l1](u2-l1-opencv-stitching-pipeline.md) 的「OpenCV 八阶段流水线」，专门拆解流水线第 5 阶段——**圆柱面投影（Cylindrical Projection）**——背后的数学。

读完本讲，你应该能够：

1. 说清楚 `mapForward`（源像素 → 圆柱面）和 `mapBackward`（圆柱面 → 源像素）这两条映射公式的几何含义。
2. 理解相机内参矩阵 \(K\)、旋转矩阵 \(R\)，以及代码里真正参与运算的两个派生矩阵 \(R K^{-1}\) 与 \(K R^{-1}\) 各自的作用。
3. 理解全局常数 `scale = 2707.47f` 为什么其实就是「焦距（以像素为单位）」。
4. 读懂 `detectResultRoi` 如何用「正向映射 + 取 min/max」求出输出图像的边界框 `dst_tl / dst_br`。
5. 读懂 `buildMaps` 如何用「反向映射」为每个目标像素算出对应的源坐标，填出供 `remap` 使用的 `xmap / ymap`，并能回答「为什么图像变换普遍用反向映射而不是正向映射」。

本讲只读一个源文件 [圆柱面投影.cpp](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp)，但它是后续 u2-l4（硬件定点实现）和 u5-l1（定点位宽推导）的数学基础——硬件里的 `k_inv` 系数、CORDIC 算的 `sin/cos`，都直接对应本讲讲清的这套浮点公式。

## 2. 前置知识

在进入源码前，先用三段话补齐必要的几何与线性代数直觉。

### 2.1 为什么要做圆柱面投影

七路摄像头各自拍一张平面图，要把它们拼成一张 360° 全景。直接把平面图「平移拼接」是不行的，因为每张平面图都带着**透视形变**（近大远小、直线会聚）。圆柱面投影的做法是：想象把每张平面图「贴回」到一个以相机为中心的圆柱面上，再把圆柱面展开成矩形长条。展开后的矩形天然适合横向拼接，这就是全景图的标准中间表示。

### 2.2 像素坐标与相机内参 K

图像里的像素坐标 \((x, y)\)（列、行）本身没有物理意义，要把它换算成「相机看到的一条光线方向」，需要相机**内参矩阵** \(K\)：

\[
K = \begin{pmatrix} f_x & 0 & c_x \\ 0 & f_y & c_y \\ 0 & 0 & 1 \end{pmatrix}
\]

其中 \(f_x, f_y\) 是焦距（像素单位），\((c_x, c_y)\) 是主点（图像中心）。\(K^{-1}\) 的作用正是「把像素 \((x,y)\) 还原成相机坐标系下的一条归一化光线方向」。本代码里 \(f_x = f_y\)，所以焦距是一个标量，记作 `scale`。

### 2.3 旋转矩阵 R 与「每路相机的朝向」

七路摄像头朝向不同，每路有自己的旋转矩阵 \(R\)（3×3 正交矩阵）。把第 \(i\) 路相机拍到的像素「统一到同一个圆柱面」，就要先用 \(R\) 把该相机的光线方向旋转到公共参考系。因为 \(R\) 正交，\(R^{-1} = R^{T}\)（转置即逆），这是后面 `rinv = R.t()` 的数学依据。

> 术语速查：`scale`（焦距尺度）、内参 \(K\)、旋转 \(R\)、正向映射（源→目标）、反向映射（目标→源）、`atan2`（四象限反正切）、`remap`（OpenCV 的反向映射重采样函数）。

## 3. 本讲源码地图

| 文件 | 关键函数/段 | 行号 | 作用 |
|---|---|---|---|
| 圆柱面投影.cpp | 全局 `scale` 与矩阵数组 | L30-L35 | 投影用的焦距常数 + 5 个全局 3×3 矩阵 |
| 圆柱面投影.cpp | `setCameraParams` | L93-L123 | 把 \(K, R\) 预处理成 `r_kinv`/`k_rinv` 等派生矩阵 |
| 圆柱面投影.cpp | `mapForward` | L36-L49 | 源像素 → 圆柱面坐标 \((u,v)\) |
| 圆柱面投影.cpp | `mapBackward` | L50-L67 | 圆柱面坐标 \((u,v)\) → 源像素 |
| 圆柱面投影.cpp | `detectResultRoi` | L68-L91 | 正向扫描全体源像素，求输出边界框 |
| 圆柱面投影.cpp | `buildMaps` | L125-L147 | 反向扫描目标矩形，生成 `xmap/ymap` |

记忆口诀：**`setCameraParams` 备料，`mapForward` 用来量尺寸（求 ROI），`mapBackward` 用来填像素（建 map），`buildMaps` 把它们串起来。**

## 4. 核心概念与源码讲解

### 4.1 setCameraParams：把 K、R 预处理成「真正参与运算」的派生矩阵

#### 4.1.1 概念说明

`mapForward` / `mapBackward` 每个像素都要做一次矩阵乘法，而每次都现算 \(R K^{-1}\)、\(K R^{-1}\) 太浪费。所以工程上的标准做法是：**在投影开始前，把所有需要用到的矩阵一次性算好，存进全局数组**，之后每个像素只做一次「向量×矩阵」即可。

这就是 `setCameraParams` 的职责——它是 `buildMaps` 的第一步，输入是 OpenCV 标定阶段（u2-l1 的 HomographyBasedEstimator + BundleAdjusterRay）产出的 \(K\) 和 \(R\)，输出是四个填好的全局数组：`k`、`rinv`、`r_kinv`、`k_rinv`。

#### 4.1.2 核心流程

```
输入: K (3x3 内参), R (3x3 旋转), T (3x1 平移, 默认0)
  │
  ├─ k[i]      ← 直接拷贝 K 的 9 个元素（行优先）
  ├─ rinv      ← R 的转置 R.t()  （因为 R 正交，R⁻¹ = Rᵀ）
  ├─ r_kinv    ← R * K.inv()     即 R·K⁻¹   ← 给 mapForward 用
  └─ k_rinv    ← K * rinv        即 K·R⁻¹   ← 给 mapBackward 用
```

四个矩阵的对应关系（**本讲最重要的一张表**）：

| 全局数组 | 数学形式 | 含义 | 谁会用 |
|---|---|---|---|
| `k` | \(K\) | 内参（像素↔光线） | 备用 |
| `rinv` | \(R^{-1}=R^{T}\) | 旋转的逆 | 备用、构造 `k_rinv` |
| `r_kinv` | \(R K^{-1}\) | 「像素 → 公共坐标系光线」 | `mapForward` |
| `k_rinv` | \(K R^{-1}\) | 「公共坐标系光线 → 像素」 | `mapBackward` |

注意方向上的对称：正向 `r_kinv` 是 \(R K^{-1}\)，反向 `k_rinv` 是 \(K R^{-1}\)，两者互为「逆运算链」上的伙伴。

#### 4.1.3 源码精读

[圆柱面投影.cpp:L30-L35](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L30-L35) 定义了全局焦距常数和 5 个长度为 9 的一维数组（3×3 矩阵按行优先展开）：

```cpp
float scale = 2707.47f;
float k[9];
float rinv[9];
float r_kinv[9];
float k_rinv[9];
float t[3];
```

`scale = 2707.47f` 就是本路相机的**焦距（像素单位）**。它和 `main` 里 OpenCV 标准圆柱投影器用的 `cameras[0].focal`（见 [L348-L349](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L348-L349)）是同一个物理量，作者把它写死成常数，是为了让下面这套从 OpenCV 内部「抄出来」的独立函数不依赖 OpenCV 对象也能跑。

[圆柱面投影.cpp:L93-L123](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L93-L123) 是 `setCameraParams` 的核心。先看它如何构造两个关键派生矩阵：

```cpp
Mat_<float> R_Kinv = R * K.inv();          // L111  r_kinv = R·K⁻¹
r_kinv[0] = R_Kinv(0, 0); ... r_kinv[8] = R_Kinv(2, 2);   // 行优先存入数组

Mat_<float> K_Rinv = K * Rinv;             // L116  k_rinv = K·R⁻¹
k_rinv[0] = K_Rinv(0, 0); ... k_rinv[8] = K_Rinv(2, 2);
```

其中 `Rinv` 来自 [L106](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L106) 的 `Mat_<float> Rinv = R.t();`——利用旋转矩阵「转置即逆」的性质，避免显式求逆。`CV_Assert`（[L97-L99](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L97-L99)）只是断言输入矩阵的尺寸和类型正确。

> **承接硬件**：u2-l4 里硬件模块的定点系数 `k_inv0~k_inv8`，本质就是把这里的 `r_kinv`（或 `k_rinv`）从浮点量化成定点二进制。本讲讲清浮点含义，硬件讲义只解决「定点化」问题。

#### 4.1.4 代码实践

**实践目标**：确认 `r_kinv` 和 `k_rinv` 确实是互为反向运算的「链路」。

**操作步骤**：

1. 阅读源码 [L111](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L111) 与 [L116](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L116)。
2. 假设 \(K\) 为单位阵 \(I\)、\(R\) 也为单位阵 \(I\)（最简单情形），在纸上算：\(R K^{-1} = I\cdot I = I\)，\(K R^{-1} = I\cdot I = I\)。此时两个矩阵都等于 \(I\)。
3. 推广：当 \(K=I\) 时，`r_kinv` = `k_rinv` = \(R^{-1}\)；说明内参 \(K\) 是「像素↔光线」的换算层，去掉它就退化为纯旋转。

**需要观察的现象 / 预期结果**：当 \(K\) 非单位时，`r_kinv` ≠ `k_rinv`，但两者方向相反（一个正向、一个反向）。（待本地用真实 \(K,R\) 数值验证，可借助 [L316-L319](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L316-L319) 打印出的 `cameras[i].K()` / `.R` 构造输入。）

#### 4.1.5 小练习与答案

- **Q**：为什么 `rinv` 用 `R.t()` 而不是 `R.inv()`？
  **A**：因为 \(R\) 是正交旋转矩阵，满足 \(R^{T}R = I\)，转置就等于逆；用 `R.t()` 省去一次通用矩阵求逆，更快且数值更稳。
- **Q**：`k[9]` 这个全局数组在本讲后续会被 `mapForward/mapBackward` 直接读取吗？
  **A**：不会。`mapForward` 读 `r_kinv`，`mapBackward` 读 `k_rinv`；`k` 只是 `setCameraParams` 中间留存的「原始内参」，本讲的映射函数都不直接用它。

---

### 4.2 mapForward：源像素 → 圆柱面坐标

#### 4.2.1 概念说明

正向映射回答：「源图像里的像素 \((x,y)\)，投影到圆柱面后，落在圆柱展开图的哪个位置 \((u,v)\)？」几何上分三步：

1. 用 \(R K^{-1}\) 把像素 \((x,y)\) 还原成公共参考系下的一条光线方向 \((x', y', z')\)。
2. 把这条光线「打」到单位圆柱面上：水平角 \(\theta = \mathrm{atan2}(x', z')\)，高度就是 \(y'\) 归一化到单位半径 \(\dfrac{y'}{\sqrt{x'^2+z'^2}}\)。
3. 乘上焦距 `scale`，把「角度/半径」换算成「像素」。

#### 4.2.2 核心流程

数学公式（注意行内用 `\(...\)`、独立公式用 `\[...\]`）：

正向光线方向：
\[
\begin{pmatrix}x'\\y'\\z'\end{pmatrix} = R\,K^{-1}\begin{pmatrix}x\\y\\1\end{pmatrix}
\]

圆柱面坐标：
\[
u = s\cdot\mathrm{atan2}(x',\,z'), \qquad
v = s\cdot\frac{y'}{\sqrt{x'^2+z'^2}}
\]

其中 \(s\) 即 `scale`。直觉：`atan2` 给出「左右」角度，除以水平半径后的 `y'` 给出「上下」高度。

```
像素 (x,y,1)
   │  乘 r_kinv (R·K⁻¹)
   ▼
光线方向 (x',y',z')
   │  atan2 + 归一化 + ×scale
   ▼
圆柱坐标 (u,v)
```

#### 4.2.3 源码精读

[圆柱面投影.cpp:L36-L49](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L36-L49) 是 `mapForward` 全部内容，只有 4 行有效运算：

```cpp
inline
void mapForward(float x, float y, float &u, float &v)
{
    float x_ = r_kinv[0]*x + r_kinv[1]*y + r_kinv[2];   // 第0行·(x,y,1)
    float y_ = r_kinv[3]*x + r_kinv[4]*y + r_kinv[5];   // 第1行
    float z_ = r_kinv[6]*x + r_kinv[7]*y + r_kinv[8];   // 第2行
    u = scale * atan2f(x_, z_);
    v = scale * y_ / sqrtf(x_*x_ + z_*z_);
}
```

要点：

- `r_kinv[0..8]` 按**行优先**存储一个 3×3 矩阵，第三列 `r_kinv[2/5/8]` 乘以隐含的齐次 `1`，对应公式里的 \((x,y,1)\)。
- `atan2f(x_, z_)` 是四象限反正切，比 `atanf(x_/z_)` 更安全（不会除零，且能区分四个象限）。它在硬件里由 CORDIC IP 实现（见 u2-l4 的 `cordic_0`）。
- `sqrtf(x_*x_ + z_*z_)` 是光线在水平面（\(xz\) 平面）投影的长度，用于把高度 \(y'\) 归一化。

> **承接硬件**：`atan2f` ↔ CORDIC IP，`sqrtf` ↔ （通常查表或迭代），`scale` ↔ 定点常数 `coe`。本讲先认准浮点公式，硬件讲义再讲怎么定点化。

#### 4.2.4 代码实践

**实践目标**：手算一个像素的 `mapForward`，对照代码逻辑验证。

**给定**（自造的简单参数，便于手算）：
\[
K = \begin{pmatrix}1000&0&500\\0&1000&300\\0&0&1\end{pmatrix},\quad R=I,\quad s=2707.47
\]
因为 \(R=I\)，所以 \(R K^{-1} = K^{-1}\)，可手算得：
\[
K^{-1} = \begin{pmatrix}0.001&0&-0.5\\0&0.001&-0.3\\0&0&1\end{pmatrix}
\]

**操作步骤**：分别对三个像素手算 \((x',y',z')\) 再算 \((u,v)\)。

1. **主点 \((x,y)=(500,300)\)**：
   - \(x' = 0.001\cdot500 -0.5 = 0\)
   - \(y' = 0.001\cdot300 -0.3 = 0\)
   - \(z' = 1\)
   - \(u = 2707.47\cdot\mathrm{atan2}(0,1)=0\)，\(v = 2707.47\cdot0/1 = 0\)
   - **结果 \((0,0)\)**：图像中心映射到圆柱图原点。✅
2. **主点右侧 \((1000,300)\)**：
   - \(x'=0.001\cdot1000-0.5=0.5\)，\(y'=0\)，\(z'=1\)
   - \(u = 2707.47\cdot\mathrm{atan2}(0.5,1)=2707.47\cdot0.46365\approx1255.4\)
   - \(v=0\)
   - **结果 \((1255.4,\,0)\)**：向右的像素映射到圆柱图右侧，高度不变。✅
3. **主点上方 \((500,200)\)**（图像坐标 \(y\) 变小＝向上）：
   - \(x'=0\)，\(y'=0.001\cdot200-0.3=-0.1\)，\(z'=1\)
   - \(u=0\)，\(v=2707.47\cdot(-0.1)/1=-270.747\)
   - **结果 \((0,\,-270.747)\)**：向上的像素映射到圆柱图负 \(v\)（上方）。✅

**需要观察的现象 / 预期结果**：主点 \(\to(0,0)\)，水平偏移主要改变 \(u\)，垂直偏移主要改变 \(v\)——这正是「圆柱展开」该有的行为。若想用真值核对，可去掉 [L39-L41](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L39-L41) 那几行 `cout` 注释，让程序自己打印 `r_kinv` 并复算。（上述手算结果为「示例」，未在本地编译运行。）

#### 4.2.5 小练习与答案

- **Q**：把 `atan2f(x_, z_)` 改成 `atanf(x_/z_)` 会出什么问题？
  **A**：当 \(z'=0\) 时除零；而且 `atanf` 值域只有 \((-\pi/2,\pi/2)\)，无法区分光线朝前还是朝后，会得到错误的圆柱角度。`atan2f` 用两个分量定象限，安全且正确。
- **Q**：\(v\) 公式里为什么除以 \(\sqrt{x'^2+z'^2}\) 而不是除以 \(z'\)？
  **A**：因为要把光线投影到「单位半径」圆柱面上再取高度；\(\sqrt{x'^2+z'^2}\) 正是该光线在水平面的投影长度，用它归一化后 \(y'\) 才是圆柱面上的真实高度比例。

---

### 4.3 mapBackward：圆柱面坐标 → 源像素

#### 4.3.1 概念说明

反向映射回答反过来的问题：「圆柱展开图上的目标像素 \((u,v)\)，应该去源图像的哪个坐标 \((x,y)\) 取色？」它是 `mapForward` 的逆运算，分三步：

1. 把 \((u,v)\) 除以 `scale`，还原成「角度 \(u_n\)」和「归一化高度 \(v_n\)」。
2. 还原出单位圆柱面上的点：\((\sin u_n,\; v_n,\; \cos u_n)\)（注意 \(x'=\sin,\;z'=\cos\)，正好对应 `mapForward` 里 `atan2(x',z')` 的逆）。
3. 用 \(K R^{-1}\) 把这个圆柱面点「投影回」源图像的像素平面，再透视除以 \(z\) 得到 \((x,y)\)。

#### 4.3.2 核心流程

归一化与还原圆柱面点：
\[
u_n = u/s,\quad v_n = v/s,\qquad (x',y',z') = (\sin u_n,\;v_n,\;\cos u_n)
\]

投影回像素（注意这里用 `k_rinv` = \(K R^{-1}\)）：
\[
\begin{pmatrix}\tilde x\\\tilde y\\\tilde z\end{pmatrix} = K\,R^{-1}\begin{pmatrix}x'\\y'\\z'\end{pmatrix},\qquad
(x,y)=\begin{cases}(\tilde x/\tilde z,\;\tilde y/\tilde z) & \tilde z>0\\(-1,-1) & \text{否则}\end{cases}
\]

```
圆柱坐标 (u,v)
   │  ÷scale，sin/cos
   ▼
单位圆柱面点 (x',y',z')
   │  乘 k_rinv (K·R⁻¹)
   ▼
齐次像素 (x̃,ỹ,z̃)
   │  ÷z̃（透视除法）
   ▼
源像素 (x,y)
```

#### 4.3.3 源码精读

[圆柱面投影.cpp:L50-L67](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L50-L67)：

```cpp
inline
void mapBackward(float u, float v, float &x, float &y)
{
    u /= scale;  v /= scale;
    float x_ = sinf(u);
    float y_ = v;
    float z_ = cosf(u);

    float z;
    x = k_rinv[0]*x_ + k_rinv[1]*y_ + k_rinv[2]*z_;   // 第0行
    y = k_rinv[3]*x_ + k_rinv[4]*y_ + k_rinv[5]*z_;   // 第1行
    z = k_rinv[6]*x_ + k_rinv[7]*y_ + k_rinv[8]*z_;   // 第2行

    if (z > 0) { x /= z; y /= z; }
    else x = y = -1;          // 退化光线：标记为无效(-1,-1)
}
```

要点：

- `sinf/cosf` 由 `u/scale` 给出，与 `mapForward` 的 `atan2f` 互为逆运算。硬件里同样由 CORDIC 提供。
- 透视除法 `x/=z; y/=z`（[L65](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L65)）是把「齐次坐标」转回「像素坐标」的关键一步——因为成像时丢失了深度，必须假定 \(z=1\) 平面。
- `if (z > 0)` 是把关：某些圆柱面点对应的源光线会落在相机背后（\(\tilde z\le0\)），无法成像，于是把 \((x,y)\) 置为 \((-1,-1)\) 作为「无效像素」标记——下游会用这个负值判断边界（见 [warp 里的越界检查](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L216-L231)）。

#### 4.3.4 代码实践

**实践目标**：验证 `mapForward` 与 `mapBackward` 是一对互逆函数（在 \(z>0\) 的有效区域内）。

**操作步骤**：

1. 用 4.2.4 算出的 \((u,v)=(1255.4,\,0)\)（来自源像素 \((1000,300)\)）。
2. 代入 `mapBackward`：\(u_n = 1255.4/2707.47 = 0.46365\)，\(v_n=0\)。
3. \(x'=\sin(0.46365)=0.4472\)，\(y'=0\)，\(z'=\cos(0.46365)=0.8944\)。
4. 用 4.2.4 的 \(K^{-1}=R K^{-1}\)（\(R=I\)），需要反算 \(K R^{-1}=K\)。乘 \(K\)：
   \(\tilde x = 1000\cdot0.4472 + 0 + 500\cdot0.8944 = 447.2+447.2 = 894.4\)
   \(\tilde y = 0 + 0 + 300\cdot0.8944 = 268.3\)
   \(\tilde z = 0+0+1\cdot0.8944 = 0.8944\)
5. 透视除法：\(x=894.4/0.8944\approx1000\)，\(y=268.3/0.8944\approx300\)。
6. **还原回 \((1000,300)\)**，与出发点一致。✅

**需要观察的现象 / 预期结果**：`mapForward` 再 `mapBackward` 能回到原像素，说明两条公式互逆（数值上因浮点有微小误差）。这个「互逆性」是 `buildMaps` 能用反向映射建表的理论保证。

#### 4.3.5 小练习与答案

- **Q**：`mapBackward` 里 `x_ = sinf(u); z_ = cosf(u);` 与 `mapForward` 里 `u = atan2(x_, z_)` 的对应关系是什么？
  **A**：`atan2(sin θ, cos θ) = θ`，所以反向用 `sin/cos` 还原角度、正向用 `atan2` 求角度，互为逆运算。
- **Q**：为什么 `z<=0` 时要把 `(x,y)` 设成 `(-1,-1)` 而不是 `(-1,-1)` 之外的其他值？
  **A**：约定俗成的「无效哨兵值」。负坐标在图像里越界，下游 `warp` 用 `addr<0` 判断（见 [L216-L218](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L216-L218)），把这种点涂成黑色，从而自然处理「投影不到的区域」。

---

### 4.4 detectResultRoi：用正向扫描求输出边界框

#### 4.4.1 概念说明

要做反向映射，必须先知道「输出图像有多大」。但圆柱投影后的输出尺寸不是直接给的——它取决于「源图像四个角投影到圆柱面后落在哪里」。`detectResultRoi` 的策略简单而稳妥：**把源图像每一个像素都正向投影一遍，记录 \((u,v)\) 的最小/最大值**，就得到输出边界框 `dst_tl`（左上）和 `dst_br`（右下）。

这是正向映射最合适的用武之地：算 ROI 只关心「整体范围」（min/max），不在乎每个目标像素是否被填满，所以正向扫描的「空洞/重叠」缺点在这里不存在。

#### 4.4.2 核心流程

```
初始化: tl_u=tl_v=+∞, br_u=br_v=-∞
for 每个源像素 (x,y):
    mapForward(x,y) → (u,v)
    tl_u=min(tl_u,u); tl_v=min(tl_v,v)
    br_u=max(br_u,u); br_v=max(br_v,v)
dst_tl = (⌊tl_u⌋, ⌊tl_v⌋)
dst_br = (⌊br_u⌋, ⌊br_v⌋)
```

输出尺寸为 `(dst_br.y - dst_tl.y + 1) × (dst_br.x - dst_tl.x + 1)`（见 `buildMaps` 的 `create`）。

#### 4.4.3 源码精读

[圆柱面投影.cpp:L68-L91](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L68-L91)：

```cpp
void detectResultRoi(Size src_size, Point &dst_tl, Point &dst_br)
{
    float tl_uf = (std::numeric_limits<float>::max)();   // +∞
    float tl_vf = (std::numeric_limits<float>::max)();
    float br_uf = -(std::numeric_limits<float>::max)();  // -∞
    float br_vf = -(std::numeric_limits<float>::max)();

    float u, v;
    for (int y = 0; y < src_size.height; ++y)
      for (int x = 0; x < src_size.width; ++x) {
        mapForward(static_cast<float>(x), static_cast<float>(y), u, v);
        tl_uf = (std::min)(tl_uf, u); tl_vf = (std::min)(tl_vf, v);
        br_uf = (std::max)(br_uf, u); br_vf = (std::max)(br_vf, v);
      }

    dst_tl.x = static_cast<int>(tl_uf);   // 向零取整
    dst_tl.y = static_cast<int>(tl_vf);
    dst_br.x = static_cast<int>(br_uf);
    dst_br.y = static_cast<int>(br_vf);
}
```

要点：

- `(std::numeric_limits<float>::max)()` 外层加括号是防止与 Windows 宏 `max` 冲突的常见写法；`(std::min)`/`(std::max)` 同理。
- 注意 `static_cast<int>` 是**向零截断**而非四整，对正数相当于 `floor`；若 `tl_uf` 为负（投影后左上角可能在负坐标），结果需要小心（`buildMaps` 里用 `v - dst_tl.y` 做了偏移修正）。
- 复杂度是 \(O(W\cdot H)\)，对每张源图只跑一次，开销可接受。

> **承接硬件**：u2-l4 硬件模块里扫描的「目标区域」正是这里的 `dst_tl/dst_br`，在硬件里被固化成寄存器初值。

#### 4.4.4 代码实践

**实践目标**：理解「为什么求 ROI 用正向、填像素用反向」。

**操作步骤**：

1. 阅读 [L68-L91](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L68-L91)，确认它调用的是 `mapForward`。
2. 想象一个 2×2 的极小源图，四个角像素经 `mapForward` 得到 \((u,v)\)，取它们的 min/max，就是 `dst_tl/dst_br`。
3. 用 4.2.4 的参数估算：主点附近 \(u\in[0,1255]\)，所以 `dst_tl.x` 大概率是负值或 0——说明投影后图像左上角可以「伸到负坐标」，这正是后面 `buildMaps` 要做坐标平移的原因。

**需要观察的现象 / 预期结果**：`dst_tl` 的坐标可能是负数，`dst_br - dst_tl` 才是真正的输出宽高。（待本地用真实图像验证实际数值。）

#### 4.4.5 小练习与答案

- **Q**：能否用反向映射来求 ROI？为什么作者选正向？
  **A**：理论上可以反向，但反向映射需要先知道目标范围，形成「先有鸡还是先有蛋」的死循环。正向映射不需要预知目标范围，只需扫一遍源图像取极值，所以求 ROI 必然用正向。
- **Q**：为什么边界值取 `int` 而不是保留 `float`？
  **A**：因为输出图像的像素坐标是整数栅格，宽高也是整数；取整后得到的就是「能框住所有投影点的最小整数矩形」。

---

### 4.5 buildMaps：反向扫描目标矩形，生成 xmap/ymap

#### 4.5.1 概念说明

知道了输出边界框后，`buildMaps` 的工作是：**为输出图像的每一个目标像素 \((u,v)\)，算出它应该去源图像采样的浮点坐标 \((x,y)\)**，分别存进 `xmap` 和 `ymap` 两张浮点图。这两张图就是 OpenCV `remap` 函数需要的输入——`remap` 会用它们做双线性插值，把源图重采样成投影后的图。

这就是「**反向映射（inverse mapping）**」的标准做法：遍历目标、回查源。它解决了正向映射的两大痼疾——**空洞**（有些目标像素没有源像素映射过来）和**重叠**（多个源像素映射到同一目标）。

#### 4.5.2 核心流程

```
buildMaps(src_size, K, R):
  1. setCameraParams(K, R)          # 备料派生矩阵
  2. detectResultRoi(src_size) → dst_tl, dst_br
  3. 创建 xmap、ymap，尺寸 = (dst_br.y-dst_tl.y+1) × (dst_br.x-dst_tl.x+1)
  4. for v in [dst_tl.y, dst_br.y]:
        for u in [dst_tl.x, dst_br.x]:
            mapBackward(u, v) → (x, y)
            xmap[v-dst_tl.y, u-dst_tl.x] = x
            ymap[v-dst_tl.y, u-dst_tl.x] = y
  5. return Rect(dst_tl, dst_br)
```

注意循环里写表用的是 `v - dst_tl.y`、`u - dst_tl.x`——把可能为负的绝对坐标平移成从 0 开始的数组下标。

#### 4.5.3 源码精读

[圆柱面投影.cpp:L125-L147](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L125-L147)：

```cpp
Rect buildMaps(Size src_size, InputArray K, InputArray R,
               OutputArray _xmap, OutputArray _ymap)
{
    setCameraParams(K, R);                       // L127 备料
    Point dst_tl, dst_br;
    detectResultRoi(src_size, dst_tl, dst_br);   // L129 求边界

    _xmap.create(dst_br.y - dst_tl.y + 1,
                 dst_br.x - dst_tl.x + 1, CV_32F);   // 输出宽高
    _ymap.create(dst_br.y - dst_tl.y + 1,
                 dst_br.x - dst_tl.x + 1, CV_32F);

    Mat xmap = _xmap.getMat(), ymap = _ymap.getMat();
    float x, y;
    for (int v = dst_tl.y; v <= dst_br.y; ++v)
      for (int u = dst_tl.x; u <= dst_br.x; ++u) {
        mapBackward(static_cast<float>(u), static_cast<float>(v), x, y);
        xmap.at<float>(v - dst_tl.y, u - dst_tl.x) = x;   // 坐标平移到0基
        ymap.at<float>(v - dst_tl.y, u - dst_tl.x) = y;
      }
    return Rect(dst_tl, dst_br);
}
```

要点：

- `CV_32F` 表示每通道 32 位浮点——采样坐标必须是浮点，因为 `mapBackward` 算出的 \((x,y)\) 几乎不会落在整数像素上，`remap` 据此做插值。
- `buildMaps` 的调用方是 `warp`（[L155](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L155)），`warp` 在拿到 `xmap/ymap` 后做了两件事：① 自己手写了一套双线性插值（生成 `addr/weight` 表，供 FPGA 查表用，u2-l3 详讲）；② 调 `remap(src, dst, xmap, ymap, ...)`（[L262](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L262)）作为「标准答案」参考输出。
- 整个 `warp` 流程的坐标系：`detectResultRoi` 给出绝对坐标 `dst_tl/dst_br`，`buildMaps` 用平移后的下标存表，`warp` 最后返回 `dst_roi.tl()`（[L265](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L265)）供上层 `blender` 摆放各路投影图。

#### 4.5.4 代码实践

**实践目标**：回答本讲的核心思考题——**为什么图像变换普遍用反向映射（remap），而不是正向映射？**

**操作步骤**：

1. 阅读 `buildMaps` 的双重循环（[L136-L144](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%9C%86%E6%9F%B1%E9%9D%A2%E6%8A%95%E5%BD%B1.cpp#L136-L144)），确认它是「按目标像素遍历、回查源坐标」。
2. 对比 `detectResultRoi`（正向、只取极值）。问自己：如果用正向映射来「填像素」，会发生什么？

**需要观察的现象 / 预期结果 / 参考答案**：

- **正向映射填像素的两个致命问题**：
  1. **空洞**：投影是非线性变换，放大率随位置变化。把源像素逐一「投」到目标，相邻源像素可能投到不相邻的目标位置，留下没被任何源像素覆盖的「黑洞」。
  2. **重叠**：多个源像素可能投到同一个目标像素，需要额外的累加/平均逻辑。
- **反向映射（remap）的好处**：遍历每一个目标像素，**保证每个目标像素恰好被填一次**，没有空洞；对落在非整数位置的源坐标，用插值（最近邻/双线性）一次性解决，逻辑简单、可并行。
- 因此本代码「求尺寸用正向（`detectResultRoi`）、填像素用反向（`buildMaps`+`remap`）」是最合理的分工。这也是 FPGA 实现里「扫描目标区域 `dst_tl/dst_br`、逐像素算源地址」的根本理由（u2-l4）。

（本实践为源码阅读型，无需运行命令。）

#### 4.5.5 小练习与答案

- **Q**：`xmap`/`ymap` 为什么必须是 `CV_32F` 浮点类型，而不能用 `CV_8U`？
  **A**：因为 `mapBackward` 算出的源坐标是浮点（几乎不落在整数栅格上），需要保留小数位供 `remap` 插值；用 8 位整数会丢失小数，无法做双线性插值。
- **Q**：`buildMaps` 里 `xmap.at<float>(v - dst_tl.y, u - dst_tl.x)` 的减法有什么作用？
  **A**：把绝对坐标 `(u,v)`（可能从负值开始）平移到从 `(0,0)` 开始的数组下标，使输出图的左上角对应 `dst_tl`。

---

## 5. 综合实践

把本讲五个模块串成一个完整的「手工投影」小任务。

**任务**：在一张纸上，用最简参数完整跑一遍「`setCameraParams` → `detectResultRoi`（简化）→ `mapForward`/`mapBackward` 互逆验证」。

**给定**：
\[
K=\begin{pmatrix}1000&0&500\\0&1000&300\\0&0&1\end{pmatrix},\quad R=I,\quad s=2707.47
\]

**要求**：

1. 手算 \(K^{-1}\)，写出 `r_kinv`（=\(K^{-1}\)）和 `k_rinv`（=\(K\)）的 9 个元素。
2. 取源像素 \((1000,300)\)，用 `mapForward` 算出 \((u,v)\)（应得 \(\approx(1255.4,0)\)）。
3. 把上一步的 \((u,v)\) 代入 `mapBackward`，验证是否还原回 \((1000,300)\)。
4. 取源像素 \((500,200)\)，用 `mapForward` 算出 \((u,v)\)（应得 \(\approx(0,-270.747)\)），并解释为什么 \(v\) 是负值。
5. 最后用一段话总结：本代码在 `detectResultRoi` 用 `mapForward`、在 `buildMaps` 用 `mapBackward`，分别解决了什么问题。

**预期结果**：步骤 2、3 互逆闭合；步骤 4 的负 \(v\) 对应「图像坐标向上」；步骤 5 的总结应点出「正向求范围、反向填像素」的分工。整个练习不用电脑也能完成，目的就是把「浮点算法」这一层彻底吃透，为 u2-l3（查表）和 u2-l4（硬件定点）打好基础。

## 6. 本讲小结

- 圆柱面投影的核心是一对互逆映射：`mapForward`（源像素→圆柱）用 \(R K^{-1}\)，`mapBackward`（圆柱→源像素）用 \(K R^{-1}\)。
- `setCameraParams` 把标定产出的 \(K,R\) 预处理成 `r_kinv`/`k_rinv`，让每个像素只需一次「向量×矩阵」。
- 全局 `scale = 2707.47f` 就是焦距（像素单位），对应 `cameras[0].focal`，在硬件里成为定点常数 `coe`。
- `detectResultRoi` 用**正向扫描 + min/max** 求输出边界框 `dst_tl/dst_br`——正向映射最适合「算范围」。
- `buildMaps` 用**反向扫描**为每个目标像素回查源坐标，生成 `xmap/ymap` 供 `remap` 插值——反向映射最适合「填像素」，因为它没有空洞和重叠。
- `atan2f/sinf/cosf` 在硬件里由 CORDIC IP 实现，这套浮点公式是 u2-l4 硬件定点实现的直接源头。

## 7. 下一步学习建议

- **下一讲 [u2-l3](u2-l3-bilinear-interpolation-tables.md)**：进入 `warp` 函数的后半段，看它如何把 `mapBackward` 产出的浮点坐标 \((x,y)\)，转成 FPGA 查表用的 `addr` 地址表（4 个相邻像素）和 `weight` 权重表（4 个双线性权重），以及如何导出 `.coe` 系数文件。
- **之后 [u2-l4](u2-l4-cylindrical-projection-hardware.md)**：把本讲的浮点公式与 u2-l3 的查表一起映射到 `圆柱面投影.v` 的定点硬件，看 `k_inv`、CORDIC、`weight_x/y` 如何一一对应。
- **延伸阅读**：可对照 OpenCV 源码 `modules/stitching/src/warpers.cpp` 中 `CylindricalWarper` 的同名 `mapForward/mapBackward`，验证作者「抄出来独立化」的版本与官方实现一致；理解这一点后再读硬件实现，会有「浮点参考 → 定点 RTL」的清晰脉络。
