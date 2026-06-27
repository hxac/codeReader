# 坐标、矩阵与页面几何

## 1. 本讲目标

本讲是「文档抽象」单元的几何补完。学完本讲，你应该能够：

- 说清楚 `fz_matrix` 这 6 个浮点数（`a b c d e f`）如何表示一个二维仿射变换，并能手算「一个点被矩阵变换后的坐标」。
- 区分 `fz_concat` / `fz_pre_*` / `fz_post_*` 三类组合函数的语义，理解**矩阵乘法不可交换**对渲染结果的影响。
- 区分 `fz_rect`（浮点矩形）与 `fz_irect`（整数像素包围盒），并理解渲染管线中「页面用户坐标 → 设备像素」是如何由这两者搭桥的。
- 独立构造一个「先缩放再旋转」与「先旋转再缩放」的 CTM，并解释为何两幅图会不同。

> 名词提示：本讲反复出现的 **CTM**（Current Transformation Matrix，当前变换矩阵）就是 `example.c` 里那个名为 `ctm` 的 `fz_matrix` 变量，它把页面用户空间（默认 72 dpi）映射到设备像素。

## 2. 前置知识

本讲承接 [u3-l1 fz_document 与 fz_page 抽象](u3-l1-document-abstraction.md)：你已经知道 `fz_bound_page` 返回页面边界、`fz_run_page` 把页面内容驱动到 device。本讲要回答的是——**边界矩形是「浮点的页面坐标」，而 device/pixmap 是「整数的像素网格」，这两套坐标系之间是怎么换算的？** 答案就是 `fz_matrix` 与 `fz_rect`/`fz_irect` 这一组几何原语。

如果你跑过 [u1-l5 第一个渲染程序](u1-l5-first-render.md)，应该见过这两行：

```c
ctm = fz_scale(zoom / 100, zoom / 100);
ctm = fz_pre_rotate(ctm, rotate);
```

当时我们只说「缩放和旋转拼成 ctm」，本讲就把这背后的数学和 API 约定彻底讲透。

需要的基础数学：矩阵乘法、三角函数（旋转用到 cos/sin）。不会用到线性代数更深的内容。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/geometry.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h) | 几何原语的**契约**：`fz_point`/`fz_rect`/`fz_irect`/`fz_matrix`/`fz_quad` 的结构体定义与全部函数声明，每个函数前的注释是权威文档。 |
| [source/fitz/geometry.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c) | 几何原语的**实现**：矩阵乘法、缩放/旋转/平移/剪切、求逆、矩形交并集、浮点→整数转换的真实代码。 |
| [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) | 官方最小渲染示例，演示 CTM 的标准构造方式（缩放 + 旋转）。 |
| [source/fitz/util.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c) | `fz_new_pixmap_from_page_number` 的实现，是把「矩阵 + rect + irect」串成渲染管线的活教材。 |

> 一个补充常量：`geometry.c` 里旋转用到 `FZ_PI`，它定义在 [include/mupdf/fitz/system.h:83](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h#L83)，值为 `3.14159265f`。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**fz_matrix 仿射变换**、**缩放/旋转组合（矩阵顺序）**、**rect 与 irect**。

### 4.1 fz_matrix：仿射变换

#### 4.1.1 概念说明

页面上的内容是用「用户空间坐标」描述的——PDF 默认一个单位等于 1/72 英寸（72 dpi）。但屏幕和图片是「像素」的，而且我们还想缩放、旋转、平移。于是需要一个统一的数学对象来表达「怎么把一个用户坐标变成设备坐标」，这就是**仿射变换（affine transformation）**。

「仿射」指的是：变换保持**直线还是直线、平行线还是平行线**，但角度和长度可以变。任意二维仿射变换都能写成 6 个数，MuPDF 把它装进 `fz_matrix`：

\[
M=\begin{bmatrix} a & b & 0\\ c & d & 0\\ e & f & 1 \end{bmatrix}\quad\text{紧凑表示为}\quad [a\ b\ c\ d\ e\ f]
\]

MuPDF 采用**行向量约定**：把点 \((x,y)\) 写成行向量 \(\begin{bmatrix}x&y&1\end{bmatrix}\)，变换通过**右乘**矩阵完成：

\[
\begin{bmatrix}x'&y'&1\end{bmatrix}=\begin{bmatrix}x&y&1\end{bmatrix}M
\]

展开就是：

\[
x' = ax+cy+e,\qquad y' = bx+dy+f
\]

其中 \(\begin{bmatrix}a&b\\c&d\end{bmatrix}\) 这 2×2 部分负责**线性变换**（缩放、旋转、剪切），\((e,f)\) 负责**平移**。这就是为什么缩放/旋转矩阵的 `e=f=0`，而平移矩阵的 `a=d=1, b=c=0`。

「什么都不做」的单位矩阵是 `a=d=1, b=c=e=f=0`，即 `fz_identity`。

#### 4.1.2 核心流程

把一个点送进变换，只需一次乘加。流程伪代码：

```
输入: 点 (x, y), 矩阵 [a b c d e f]
输出: 变换后点 (x', y')
  x' = a*x + c*y + e
  y' = b*x + d*y + f
```

几个常用矩阵的形状（来自头文件注释，可直接对照）：

| 操作 | 矩阵 `[a b c d e f]` | 含义 |
| --- | --- | --- |
| 缩放 `fz_scale(sx,sy)` | `[sx 0 0 sy 0 0]` | x 方向乘 sx、y 方向乘 sy |
| 旋转 `fz_rotate(θ)` | `[cosθ sinθ -sinθ cosθ 0 0]` | 绕原点旋转 θ 度 |
| 平移 `fz_translate(tx,ty)` | `[1 0 0 1 tx ty]` | 整体平移 (tx, ty) |
| 剪切 `fz_shear(sx,sy)` | `[1 sy sx 1 0 0]` | 错切 |

#### 4.1.3 源码精读

`fz_matrix` 的结构体定义和「6 元素 ↔ 3×3」的映射关系，权威说明在头文件注释里：

- [include/mupdf/fitz/geometry.h:374-390](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L374-L390)：定义 `fz_matrix { float a,b,c,d,e,f; }`，注释画出了 3×3 矩阵的排布与「恒为常数的单位向量」说明。
- [include/mupdf/fitz/geometry.h:392-395](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L392-L395)：声明单位矩阵常量 `fz_identity`。

「点右乘矩阵」的真实代码，验证了我们手算公式：

```c
// source/fitz/geometry.c:334-341
fz_point fz_transform_point(fz_point p, fz_matrix m)
{
    float x = p.x;
    p.x = x * m.a + p.y * m.c + m.e;   // = a*x + c*y + e
    p.y = x * m.b + p.y * m.d + m.f;   // = b*x + d*y + f
    return p;
}
```

参见 [source/fitz/geometry.c:334-341](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L334-L341)。注意它先把 `p.x` 存进临时变量 `x`，因为第二行还要用到**原始的** x，否则会被第一行覆盖。

三个基本矩阵的构造都很「直白」，只填 6 个字段：

- `fz_scale`：[source/fitz/geometry.c:67-75](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L67-L75)，把 `a=sx, d=sy`，其余清零。
- `fz_translate`：[source/fitz/geometry.c:215-223](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L215-L223)，把 `e=tx, f=ty`，对角线为 1。
- `fz_rotate`：[source/fitz/geometry.c:121-162](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L121-L162)，先 `fmod(theta,360)` 规整角度，并对 0/90/180/270 这四个特例走**精确的整数 sin/cos**（避免浮点误差），其余角度才调用 `sinf/cosf`，最终填成 `[cos sin -sin cos 0 0]`。

> 方向小贴士：`fz_rotate` 的头文件注释（[geometry.h:487-500](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L487-L500)）把它描述为「逆时针（counter clockwise）」。但 MuPDF 的位图/pixmap 的 y 轴是**朝下**的，所以在最终图像上，正角度肉眼看上去是**顺时针**——这也是 `example.c` 帮助文字里写「Rotation is in degrees clockwise」的原因。两句话都对，只是参照的坐标系不同。

#### 4.1.4 代码实践

**实践目标**：用最小程序验证「点右乘矩阵」的公式，建立对手算的信心。

**操作步骤**（示例代码，需自行编译）：

```c
/* 示例代码：打印若干点经过缩放矩阵后的坐标 */
#include <mupdf/fitz.h>
#include <stdio.h>

int main(void)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_matrix s = fz_scale(2.0f, 3.0f);        /* x 放大2倍, y 放大3倍 */
    fz_point pts[] = { {1,0}, {0,1}, {1,1}, {2,5} };
    int i;
    for (i = 0; i < 4; i++) {
        fz_point q = fz_transform_point(pts[i], s);
        printf("(%.0f,%.0f) -> (%.1f,%.1f)\n", pts[i].x, pts[i].y, q.x, q.y);
    }
    fz_drop_context(ctx);
    return 0;
}
```

**需要观察的现象 / 预期结果**：输出应为 `(1,0)->(2.0,0.0)`、`(0,1)->(0.0,3.0)`、`(1,1)->(2.0,3.0)`、`(2,5)->(4.0,15.0)`，与公式 \(x'=2x,\ y'=3y\) 完全吻合。

> 若尚未编译出 `libmupdf.a`，编译命令可参考 `example.c` 文件头注释（[example.c:1-17](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L1-L17)）。运行结果受本地环境影响时，标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：矩阵 `[0 1 -1 0 0 0]` 把点 `(1,0)` 变到哪？这是什么几何操作？

**答案**：\(x'=0\cdot1+(-1)\cdot0+0=0\)，\(y'=1\cdot1+0\cdot0+0=1\)，即 `(0,1)`。它是绕原点旋转 90°（对照 `fz_rotate(90)` 得 `[cos90 sin90 -sin90 cos90 0 0]=[0 1 -1 0 0 0]`）。

**练习 2**：为什么 `fz_transform_point` 必须用临时变量 `x` 暂存 `p.x`？如果直接写 `p.x = p.x*m.a + p.y*m.c + m.e; p.y = p.x*m.b + ...` 会出什么错？

**答案**：第二行计算 `y'` 时需要**原始的** x。若第一行已覆盖 `p.x`，第二行就会用新值 `x'` 去算 `y'`，结果错误。

---

### 4.2 缩放/旋转组合：矩阵顺序为什么重要

#### 4.2.1 概念说明

真实渲染很少只用一种变换。`example.c` 同时有缩放和旋转，CTM 是「缩放矩阵」和「旋转矩阵」的**组合**。问题来了：矩阵乘法**不可交换**——先缩放后旋转，与先旋转后缩放，通常得到不同的矩阵。

设线性部分缩放 \(S=\begin{bmatrix}s_x&0\\0&s_y\end{bmatrix}\)、旋转 \(R=\begin{bmatrix}\cos\theta&\sin\theta\\-\sin\theta&\cos\theta\end{bmatrix}\)。

- 若**均匀缩放** \(s_x=s_y=s\)，则 \(S=sI\) 与任何矩阵可交换，\(SR=RS\)，顺序无所谓。
- 若**非均匀缩放**（如 x 放大 2 倍、y 不变），则 \(SR\neq RS\)，顺序会显著改变结果。

因为 MuPDF 用行向量约定，点的变换读作 \(pM=pM_1M_2\)——**左边的矩阵先作用于点**。所以「矩阵在乘积中靠左」＝「该变换先发生」。这条规则是本模块的核心。

#### 4.2.2 核心流程

MuPDF 提供三类组合函数，理解它们的关键是分清「矩阵层面的左/右乘」与「时间层面的先/后」：

| 函数 | 数学含义 | 对点的效果 |
| --- | --- | --- |
| `fz_concat(A, B)` | \(A\times B\) | 点先过 A，再过 B（A 先发生） |
| `fz_pre_X(m)` | \(X\times m\)（左乘 X） | X 先发生，m 后发生 |
| `fz_post_X(m)` | \(m\times X\)（右乘 X） | m 先发生，X 后发生 |

> 记忆口诀：**「pre = 左乘 = 先发生」**，**「post = 右乘 = 后发生」**。「pre/post」描述的是矩阵在乘积的左边还是右边；而行向量约定下，左边先作用于点，所以两者一致。

`example.c` 的两行就用了 `pre`：

```c
ctm = fz_scale(zoom/100, zoom/100);   /* 基矩阵 = 缩放 S */
ctm = fz_pre_rotate(ctm, rotate);      /* ctm = R × S  =>  点先过 R 再过 S */
```

最终 \(ctm = R\times S\)。由于 `example.c` 用的是**均匀**缩放（x、y 同一个 `zoom`），\(R\) 与 \(S\) 可交换，所以无论先转后缩还是先缩后转，图像都一样——这就是为什么示例代码敢用最简写法。

#### 4.2.3 源码精读

`fz_concat` 是矩阵乘法的本体，6 个元素逐项相乘相加：

```c
// source/fitz/geometry.c:54-65
fz_matrix fz_concat(fz_matrix one, fz_matrix two)
{
    fz_matrix dst;
    dst.a = one.a * two.a + one.b * two.c;
    dst.b = one.a * two.b + one.b * two.d;
    dst.c = one.c * two.a + one.d * two.c;
    dst.d = one.c * two.b + one.d * two.d;
    dst.e = one.e * two.a + one.f * two.c + two.e;
    dst.f = one.e * two.b + one.f * two.d + two.f;
    return dst;
}
```

参见 [source/fitz/geometry.c:54-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L54-L65)。注意 `e/f`（平移）项里同时出现 `one.e/two.e`，这正是平移在复合时被线性部分「扭曲」的体现。

`fz_pre_scale` 用更少的乘法实现「左乘缩放」——它知道缩放矩阵的非零元素位置，所以不必走通用 `fz_concat`：

```c
// source/fitz/geometry.c:77-85
fz_matrix fz_pre_scale(fz_matrix m, float sx, float sy)
{
    m.a *= sx;  m.b *= sx;   /* 第 1 行乘 sx */
    m.c *= sy;  m.d *= sy;   /* 第 2 行乘 sy */
    return m;                 /* e,f 不变：左乘缩放不改变平移 */
}
```

参见 [source/fitz/geometry.c:77-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L77-L85)。你可以把它和 `fz_post_scale`（[geometry.c:87-97](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L87-L97)）对比：后者还会缩放 `e,f`，因为右乘缩放会放大平移量。

`fz_pre_rotate`（[source/fitz/geometry.c:164-213](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L164-L213)）同理，对 90° 的整数倍走「交换并取反」的精确快速路径，一般角度才套用 `cos/sin` 公式。

两个有用的辅助判定：

- `fz_is_rectilinear(m)`（[source/fitz/geometry.c:305-310](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L305-L310)）：判断矩阵是否「仅含 90° 整数倍旋转 + 缩放，无剪切」。渲染时若 rectilinear，水平/垂直线变换后仍水平/垂直，可走更快的代码路径。
- `fz_matrix_expansion(m)`（[source/fitz/geometry.c:312-316](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L312-L316)）：返回 \(\sqrt{|ad-bc|}\)，即矩阵的**面积缩放因子**（行列式的绝对值开方）。

#### 4.2.4 代码实践

**实践目标**（本讲的主实践任务）：用**非均匀缩放** + 旋转，亲手验证「矩阵顺序改变渲染结果」。本实践也是规格中要求的代码实践任务。

> 为什么强调非均匀？因为均匀缩放（x、y 同倍）与旋转可交换，两种顺序渲染出的图**完全相同**，看不出差别。必须用 `fz_scale(2,1)` 这种 x、y 不等的缩放，才能让顺序的差别显现出来。

**操作步骤**：复制 `docs/examples/example.c` 为 `order_test.c`，把构造 CTM 的两行（[example.c:100-107](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L100-L107)）替换为下面两个版本，分别编译运行：

版本 A ——「先放大(x2) 再旋转 45°」，即点先过缩放、再过旋转：

```c
/* 版本 A: ctm = S × R  =>  点先过 S 再过 R */
ctm = fz_concat(fz_scale(2.0f, 1.0f), fz_rotate(45));
pix = fz_new_pixmap_from_page_number(ctx, doc, page_number, ctm, fz_device_rgb(ctx), 0);
```

版本 B ——「先旋转 45° 再放大(x2)」，点先过旋转、再过缩放：

```c
/* 版本 B: ctm = R × S  =>  点先过 R 再过 S */
ctm = fz_concat(fz_rotate(45), fz_scale(2.0f, 1.0f));
pix = fz_new_pixmap_from_page_number(ctx, doc, page_number, ctm, fz_device_rgb(ctx), 0);
```

> 等价写法：版本 B 也可写成 `ctm = fz_pre_rotate(fz_scale(2,1), 45);`，因为 `fz_pre_rotate(m)=R×m`，令 `m=S` 即得 \(R\times S\)。这正好重现 `example.c` 原来的写法，只是把均匀缩放换成了非均匀。

**需要观察的现象**：取一个明显有方向的页面（例如含一行斜置文字或一个大箭头），把两个版本的 PPM 都转成 PNG 查看：

- 版本 A：内容先被横向拉伸成扁的，再整体转 45°，拉伸方向也跟着转了。
- 版本 B：内容先转 45°，再被**水平**方向拉伸，拉伸方向固定为水平。

两幅图明显不同。手算验证：对点 \((1,0)\)，

- 版本 A（先 S 后 R）：\((1,0)\xrightarrow{S}(2,0)\xrightarrow{R}(2\cos45,2\sin45)\approx(1.414,1.414)\)
- 版本 B（先 R 后 S）：\((1,0)\xrightarrow{R}(\cos45,\sin45)\approx(0.707,0.707)\xrightarrow{S}(1.414,0.707)\)

结果不同，与图像差异一致。

**预期结果**：两份 PPM 像素尺寸可能不同（旋转后包围盒变了），内容朝向也不同。**待本地验证**具体像素。

#### 4.2.5 小练习与答案

**练习 1**：`fz_concat(fz_scale(2,2), fz_rotate(90))` 与 `fz_concat(fz_rotate(90), fz_scale(2,2))` 结果相同吗？为什么？

**答案**：相同。因为 `fz_scale(2,2)` 是均匀缩放 \(2I\)，与任何矩阵可交换，故 \(S R = R S\)。这也是 `example.c` 用最简写法成立的根本原因。

**练习 2**：用 `fz_pre_*` 把「平移 (10,20) → 再缩放 2 倍」写成一行 CTM（点先平移再缩放）。

**答案**：点先平移 T、再缩放 S，即 \(ctm = T\times S\)？注意顺序——「点先过 T 再过 S」对应乘积 \(T\times S\) 中 T 在左。用 `fz_pre_*` 表达：「先平移」=左乘 T，得 `fz_pre_translate(fz_scale(2,2), 10, 20)`。验证：`fz_pre_translate(m,tx,ty)=T×m`，令 `m=S` 即 \(T\times S\)。✓

---

### 4.3 fz_rect 与 fz_irect：浮点矩形与整数像素包围盒

#### 4.3.1 概念说明

页面几何有两套「矩形」表达，必须分开：

- **`fz_rect`**：四个**浮点**数 `{x0,y0,x1,y1}`，表示**与坐标轴对齐**的矩形（左上角 `(x0,y0)`、右下角 `(x1,y1)`）。它活在「用户空间 / 设备浮点空间」，用来描述页面边界、裁剪框、命中区域。
- **`fz_irect`**：四个**整数** `{x0,y0,x1,y1}`，即「整数像素包围盒（bounding box）」。它活在「像素网格」，被 draw device 和 pixmap 用来分配内存、定位像素。

为什么需要两套？因为**像素是整数**：pixmap 的宽高、每个像素的行列号都必须是整数，而页面坐标和变换结果都是浮点。渲染链路必须有一个「浮点 rect → 整数 irect」的取整步骤。

`fz_rect` 还设计了一个精巧的**三态**模型（详见头文件注释 [geometry.h:197-234](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L197-L234)）：

| 类别 | 含义 | 判定 |
| --- | --- | --- |
| **finite（有限）** | 普通矩形，\(x0\le x1,\ y0\le y1\) | `fz_is_valid_rect` 为真 |
| **infinite（无限）** | 表示「不裁剪 / 覆盖一切」 | 四角取哨兵值 `FZ_MIN_INF_RECT`/`FZ_MAX_INF_RECT` |
| **invalid（无效）** | \(x0>x1\) 或 \(y0>y1\)，代表「空/无交集」 | `fz_is_valid_rect` 为假 |

这套三态让「两个矩形求交集」能区分两种结果：**真没交集**（返回 invalid）vs **有交集但面积为零**（返回一个 finite 的零面积矩形）。这是纯 `x0<=x1` 判定做不到的。

哨兵值（[geometry.h:227-228](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L227-L228)）选的是「能安全往返于 float 之间」的最大/最小 32 位整数：`FZ_MIN_INF_RECT=0x80000000`、`FZ_MAX_INF_RECT=0x7fffff80`。

#### 4.3.2 核心流程

渲染管线里，几何原语的协作链是：

```
fz_bound_page         -> 得到页面边界的 fz_rect（用户空间浮点）
fz_transform_rect     -> 用 ctm 把它变换到设备浮点空间（仍为 fz_rect）
fz_round_rect         -> 浮点 rect 取整为 fz_irect（像素包围盒）
fz_new_pixmap_with_bbox -> 用 irect 的宽高分配 pixmap 像素内存
fz_new_draw_device    -> 用同一个 ctm 把矢量指令光栅化进 pixmap
```

两个浮点→整数函数的差异（均来自头文件注释）：

- `fz_irect_from_rect`：严格按 `floor(x0)/ceil(x1)` 取整，**任何微小误差都会多占整像素**。
- `fz_round_rect`：先在 `x0` 加 `+0.001`、`x1` 减 `0.001` 再取整，**容忍小的浮点误差**，避免因精度噪声凭空多出一行/列像素。渲染管线（见下文 `util.c`）用的是 `fz_round_rect`。

矩形运算（交集/并集/扩展）都遵循三态规则：与 infinite 求交等于另一方、与 invalid 求并等于另一方等。

此外还有一个 `fz_quad`（[geometry.h:775-784](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L775-L784)）：由 4 个点 `{ul,ur,ll,lr}` 组成，**边不必与坐标轴对齐**。它用来表达「被旋转/剪切后的矩形区域」，例如文本搜索命中、选区高亮。`fz_rect` 变换后会变斜，就先转成 quad 再处理。

#### 4.3.3 源码精读

两个结构体的定义与注释：

- `fz_rect`：[include/mupdf/fitz/geometry.h:197-234](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L197-L234)，注释详细解释了三态模型与哨兵值选取理由。
- `fz_irect`：[include/mupdf/fitz/geometry.h:242-251](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L242-L251)，注明「used in the draw device and for pixmap dimensions」。

几个常量的初始化，能直观看到三态的编码方式：

```c
// source/fitz/geometry.c:380-388
const fz_rect fz_infinite_rect = { FZ_MIN_INF_RECT, FZ_MIN_INF_RECT, FZ_MAX_INF_RECT, FZ_MAX_INF_RECT };
const fz_rect fz_empty_rect    = { FZ_MAX_INF_RECT, FZ_MAX_INF_RECT, FZ_MIN_INF_RECT, FZ_MIN_INF_RECT };
const fz_rect fz_invalid_rect  = { 0, 0, -1, -1 };
```

参见 [source/fitz/geometry.c:380-388](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L380-L388)。注意 `fz_empty_rect` 故意让 `x0>x1`，所以它本身是 invalid 的——它常被用作「逐点扩展求包围盒」的累加初值。

浮点→整数转换，注意取整方向（左上角向外扩、右下角向外扩，保证不漏像素）与安全整数上限：

```c
// source/fitz/geometry.c:393-408 （fz_irect_from_rect 关键部分）
b.x0 = fz_clamp(floorf(r.x0), MIN_SAFE_INT, MAX_SAFE_INT);
b.y0 = fz_clamp(floorf(r.y0), MIN_SAFE_INT, MAX_SAFE_INT);
b.x1 = fz_clamp(ceilf (r.x1), MIN_SAFE_INT, MAX_SAFE_INT);
b.y1 = fz_clamp(ceilf (r.y1), MIN_SAFE_INT, MAX_SAFE_INT);
```

参见 [source/fitz/geometry.c:393-408](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L393-L408)。`MIN_SAFE_INT/MAX_SAFE_INT=±16777216`（即 \(2^{24}\)，见 [geometry.c:376-378](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L376-L378)）是 float 能精确表示的最大整数，超出就有精度损失，故做 clamp。`fz_round_rect`（[geometry.c:425-441](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L425-L441)）结构相同，但多了 `±0.001f` 的容差。

`fz_transform_rect` 是连接「矩阵」与「矩形」的关键：把矩形的**四个角**分别用矩阵变换，再取它们的**轴对齐包围盒**（因为旋转/剪切后矩形会变斜，必须用 AABB 重新框住）：

```c
// source/fitz/geometry.c:571-583 （非 rectilinear 一般情形）
s.x = r.x0; s.y = r.y0;   /* 四个角 */
t.x = r.x0; t.y = r.y1;
u.x = r.x1; u.y = r.y1;
v.x = r.x1; v.y = r.y0;
s = fz_transform_point(s, m);  t = fz_transform_point(t, m);
u = fz_transform_point(u, m);  v = fz_transform_point(v, m);
r.x0 = MIN4(s.x, t.x, u.x, v.x);  r.y0 = MIN4(s.y, t.y, u.y, v.y);
r.x1 = MAX4(s.x, t.x, u.x, v.x);  r.y1 = MAX4(s.y, t.y, u.y, v.y);
```

参见 [source/fitz/geometry.c:519-594](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L519-L594)。它对 rectilinear 矩阵有快速路径（[geometry.c:528-548](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/geometry.c#L528-L548)），只需变换两个对角点。

**整条链路的真实调用**就在便利函数 `fz_new_pixmap_from_page_with_separations` 里，是本讲最好的「活教材」：

```c
// source/fitz/util.c:205-219
fz_pixmap *fz_new_pixmap_from_page_with_separations(fz_context *ctx, fz_page *page,
        fz_matrix ctm, fz_colorspace *cs, fz_separations *seps, int alpha)
{
    fz_rect rect;  fz_irect bbox;  fz_pixmap *pix;  fz_device *dev = NULL;
    fz_var(dev);
    rect = fz_bound_page(ctx, page);     /* 1. 页面边界 fz_rect（用户空间）*/
    rect = fz_transform_rect(rect, ctm); /* 2. 用 ctm 变换到设备浮点空间 */
    bbox = fz_round_rect(rect);          /* 3. 浮点 rect -> 整数 fz_irect */
    pix  = fz_new_pixmap_with_bbox(ctx, cs, bbox, seps, alpha); /* 4. 按包围盒分配像素 */
    /* ... 后续用 ctm 创建 draw device 并 fz_run_page ... */
}
```

参见 [source/fitz/util.c:205-219](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c#L205-L219)。这一段把本讲的「矩阵 + rect + irect」三件事按顺序串了起来，正好印证 4.3.2 的流程图。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「浮点 rect → 整数 irect」的取整，以及「无交集 → invalid」的三态行为。

**操作步骤**（示例代码）：

```c
/* 示例代码：观察 rect/irect 转换与交集三态 */
#include <mupdf/fitz.h>
#include <stdio.h>

int main(void)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_rect r = fz_make_rect(0.3f, 0.7f, 99.6f, 50.2f);
    fz_irect b = fz_irect_from_rect(r);
    printf("rect  = (%.1f,%.1f,%.1f,%.1f)\n", r.x0, r.y0, r.x1, r.y1);
    printf("irect = (%d,%d,%d,%d)  w=%d h=%d\n",
           b.x0, b.y0, b.x1, b.y1, b.x1 - b.x0, b.y1 - b.y0);

    /* 两个不重叠的有限矩形求交，应得到 invalid */
    fz_rect a = fz_make_rect(0, 0, 10, 10);
    fz_rect c = fz_make_rect(20, 20, 30, 30);
    fz_rect inter = fz_intersect_rect(a, c);
    printf("intersect valid? %d  (%.0f,%.0f,%.0f,%.0f)\n",
           fz_is_valid_rect(inter), inter.x0, inter.y0, inter.x1, inter.y1);

    fz_drop_context(ctx);
    return 0;
}
```

**预期结果**：

- `irect = (0,0,100,51)`，宽 100、高 51——注意 `x0=floor(0.3)=0`、`x1=ceil(99.6)=100`，浮点边界被**向外**取整，确保不丢像素。
- `intersect valid? 0`，且坐标呈 `x0>x1`（invalid），说明两矩形无交集。

**待本地验证**：受编译环境影响，以实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fz_irect_from_rect` 对 `x0` 用 `floor`、对 `x1` 用 `ceil`，而不是统一用 `round`？

**答案**：渲染时「宁可多覆盖一像素，也不能漏掉边缘像素」。`floor(x0)/ceil(x1)` 让整数包围盒**完全包含**原浮点矩形；统一 `round` 可能把刚好在边界上的内容切掉。

**练习 2**：`fz_empty_rect` 的坐标是 `{MAX, MAX, MIN, MIN}`，`x0>x1`。既然它 invalid，为什么还要单独定义而不直接用 `fz_invalid_rect`？

**答案**：语义不同。`fz_empty_rect` 专门用作「逐点 `fz_include_point_in_rect` 扩展求包围盒」的**累加初值**（从一个点开始重建矩形）；它的命名表达了「我还没包含任何东西」的意图。两者数值上都 invalid，但用途和可读性不同。

---

## 5. 综合实践

把本讲三件事（矩阵构造、矩阵顺序、rect/irect 转换）串成一个端到端的小任务：**手动复算渲染管线给出的 pixmap 尺寸，验证你对几何链路的理解。**

任务：写一个程序，对某文档的第 1 页，用 `ctm = fz_concat(fz_scale(2, 1), fz_rotate(30))`（非均匀缩放 + 旋转）做下面的事：

1. `fz_bound_page` 取页面边界 `fz_rect`，打印。
2. `fz_transform_rect(bounds, ctm)` 得到设备浮点矩形，打印（注意旋转后宽高会变）。
3. `fz_round_rect(...)` 得到整数 `fz_irect`，打印其宽高。
4. `fz_new_pixmap_from_page_number(ctx, doc, 0, ctm, fz_device_rgb(ctx), 0)` 渲染，打印 `pix->w` 和 `pix->h`。
5. **核对**：第 3 步的 irect 宽高应当等于第 4 步的 `pix->w / pix->h`。

参考骨架（示例代码，需补全错误处理与资源释放）：

```c
/* 示例代码：综合实践骨架 */
fz_page *page = fz_load_page(ctx, doc, 0);
fz_rect bounds = fz_bound_page(ctx, page);
fz_matrix ctm  = fz_concat(fz_scale(2.0f, 1.0f), fz_rotate(30));
fz_rect devr   = fz_transform_rect(bounds, ctm);
fz_irect bbox  = fz_round_rect(devr);
printf("bounds=(%.1f,%.1f,%.1f,%.1f)  dev=(%.1f,%.1f,%.1f,%.1f)  bbox=%dx%d\n",
       bounds.x0, bounds.y0, bounds.x1, bounds.y1,
       devr.x0, devr.y0, devr.x1, devr.y1,
       bbox.x1 - bbox.x0, bbox.y1 - bbox.y0);

fz_pixmap *pix = fz_new_pixmap_from_page_number(ctx, doc, 0, ctm, fz_device_rgb(ctx), 0);
printf("pixmap = %dx%d\n", pix->w, pix->h);
/* 释放：pix -> page（page 由 _from_page_number 内部已 drop，此处若手动 load 则需 drop） */
```

**预期结果**：`bbox` 的宽高与 `pix->w/pix->h` 一致；由于 30° 旋转 + x 方向 2 倍拉伸，`dev` 矩形的宽高会明显大于原 `bounds`。**待本地验证**具体数值。

> 进阶思考：把 `ctm` 改成均匀缩放 `fz_scale(2,2)` 再算一次 `dev`，对比旋转角相同、但缩放均匀时包围盒的差异，体会非均匀缩放与旋转组合后包围盒的膨胀。

## 6. 本讲小结

- `fz_matrix` 是 6 元素 `[a b c d e f]` 的二维仿射变换，MuPDF 用**行向量右乘**约定：\(x'=ax+cy+e,\ y'=bx+dy+f\)。2×2 部分管线性变换，`(e,f)` 管平移。
- 矩阵乘法**不可交换**。`fz_concat(A,B)=A×B`（A 先作用于点）；`fz_pre_X`＝左乘 X（X 先发生），`fz_post_X`＝右乘 X（X 后发生）。口诀：**pre＝左乘＝先发生**。
- `example.c` 用均匀缩放 + `fz_pre_rotate`，因均匀缩放与旋转可交换，顺序无所谓；一旦换成非均匀缩放，顺序就会改变渲染结果。
- `fz_rect`（浮点、轴对齐、三态 finite/infinite/invalid）活在用户/设备浮点空间；`fz_irect`（整数像素包围盒）活在像素网格。渲染靠 `fz_transform_rect` + `fz_round_rect` 在两者间搭桥。
- `fz_transform_rect` 变换四个角再取 AABB；`fz_irect_from_rect` 严格 floor/ceil，`fz_round_rect` 带 ±0.001 容差；浮点→整数都 clamp 到 ±\(2^{24}\) 的安全整数范围。
- 整条链路在 `util.c:205-219` 一目了然：`bound_page → transform_rect → round_rect → pixmap`，这就是 CTM 把页面坐标变成像素的完整过程。

## 7. 下一步学习建议

本讲建立了「坐标变换」的数学与 API 基础。接下来的自然去向：

- **进入渲染管线内部**：[u4-l1 fz_device 显示设备抽象](u4-l1-device-model.md) 讲 device 虚表如何接收绘图指令；[u4-l3 draw device 与 pixmap 位图渲染](u4-l3-draw-device-pixmap.md) 讲 `fz_new_draw_device(ctx, ctm, pix)` 怎样用本讲的 ctm 把矢量指令光栅化进 pixmap。届时你会看到 ctm 被 device 内部反复使用。
- **坐标系再延伸**：当 device 把内容画进 pixmap 时，本讲的 `fz_transform_rect`/`fz_round_rect` 会决定 pixmap 的尺寸与裁剪。带着本讲的理解去看 `draw-device.c`，会非常顺。
- **搜索与选区**：本讲提到的 `fz_quad`（非轴对齐四边形）将在 [u5-l3 全文搜索](u5-l3-text-search.md) 中作为「搜索命中的带坐标区域」再次出现，届时你会明白为什么要用 quad 而非 rect。
