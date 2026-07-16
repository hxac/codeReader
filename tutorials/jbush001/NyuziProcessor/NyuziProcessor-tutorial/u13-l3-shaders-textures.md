# 着色器与纹理采样

## 1. 本讲目标

本讲是「图形渲染管线」单元的最后一篇。上一篇（u13-l2）讲完了三角形如何被光栅化成 4×4 像素块、并用 16 通道 SIMD 做覆盖测试与早期 Z 剔除。本讲接着回答两个问题：**这些像素最终该是什么颜色？颜色又如何落进帧缓冲？**

学完后你应当掌握：

- 顶点着色器 / 像素着色器的回调接口契约，以及「参数（parameter）」在两个阶段之间如何流动、为何会被重新编号。
- 透视正确的参数插值原理，以及它在 `TriangleFiller` 中的 SIMD 实现。
- 纹理采样：mipmap 层级选择、坐标换算、双线性滤波，以及底层 `Surface::readPixels` 的 gather 实现。
- 像素颜色如何经定点数 alpha 混合后，以 scatter 写回帧缓冲。

## 2. 前置知识

本讲承接 u13-l1（tile 渲染架构）与 u13-l2（光栅化），并用到更早的几条线索：

- **16 通道 SIMD**：Nyuzi 的 `vecf16_t` 是 16 个 float 拼成的向量寄存器，一条指令同时处理 16 个数据。渲染中一个 4×4 像素块恰好对应 16 个通道。
- **gather / scatter**（u2-l3）：当 16 个通道各自需要一个不同地址的内存数据时，用一次 gather 读、一次 scatter 写；这正是纹理采样与帧缓冲写回的底层机制。
- **两阶段渲染**（u13-l1）：`RenderContext::finish()` 先跑几何阶段（顶点着色 + 三角形装配 + 分 tile），再跑像素阶段（逐 tile 光栅化 + 着色 + 写回）。
- **透视除法**：把裁剪空间坐标 \((x,y,z,w)\) 除以 \(w\) 得到屏幕空间坐标，是投影的核心步骤。

本篇只讲像素颜色怎么来、怎么写，不重复光栅化覆盖算法。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `software/libs/librender/Shader.h` | 着色器抽象基类，定义 `shadeVertices` / `shadePixels` 回调契约与颜色通道枚举。 |
| `software/libs/librender/TriangleFiller.cpp` | 像素阶段核心：透视插值、调用 `shadePixels`、颜色转换与混合写回。 |
| `software/libs/librender/LinearInterpolator.h` | 二维线性插值器，参数梯度的 SIMD 求值。 |
| `software/libs/librender/Texture.h` / `Texture.cpp` | 纹理抽象：mipmap 管理、mip 层级选择、双线性 / 最近邻采样。 |
| `software/libs/librender/Surface.h` | `readPixels`（gather 读纹理）、`writeBlockMasked`（scatter 写帧缓冲）。 |
| `software/apps/sceneview/TextureShader.h` | 示例着色器：带纹理 + 朗伯光照。 |
| `software/apps/sceneview/DepthShader.h` | 示例着色器：把深度画成灰度的调试着色器。 |
| `software/apps/sceneview/sceneview.cpp` | 把着色器、纹理、uniforms 装配进 `RenderContext` 的应用主程序。 |

## 4. 核心概念与源码讲解

### 4.1 着色器接口

#### 4.1.1 概念说明

GPU 的「着色器」是应用层注入的两段回调代码：**顶点着色器**决定每个顶点的位置与随顶点变化的参数（varying），**像素着色器**决定每个像素的最终颜色。Nyuzi 没有固定的固定功能管线，而是用一个 C++ 抽象基类 `Shader` 把这两段回调的签名固定下来，应用继承它、填入自己的计算。librender 负责在合适的时机、以 16 个一批的方式调用它们。

这种「库调你」的方向叫**回调契约（callback contract）**：库规定了函数签名、调用时机、数据布局，你只管实现算法。

#### 4.1.2 核心流程

```
几何阶段                          像素阶段（逐 4x4 块）
───────                          ────────────────
inAttribs (每顶点 N 个属性)
     │
     ▼
shadeVertices ──► outParams (每顶点 P 个参数)
     │                 │ 前 4 个 = x,y,z,w（位置）
     │                 │ 其余 P-4 个 = varying
     ▼                 ▼
（透视除法、装配、   （插值后去掉位置，重新编号）
  分 tile）              │
                        ▼
                   shadePixels ◄── inParams (P-4 个, 从 0 重编号)
                        │   ▲
                        │   ├── uniforms（每帧常量）
                        │   └── sampler（纹理数组）
                        ▼
                   outColor (RGBA 共 4 个向量)
```

两个关键约定：

1. **位置参数必须排在前 4 个**。`shadeVertices` 输出的 `outParams[0..3]` 约定为裁剪空间 \((x,y,z,w)\)，几何阶段会用它们做透视除法与光栅化（见 4.2）。
2. **位置参数不会传给 `shadePixels`**。几何阶段把这 4 个位置参数剥离，只把剩下的 \(P-4\) 个 varying 拷进三角形结构，并在传给像素着色器时**从 0 重新编号**。

#### 4.1.3 源码精读

`Shader` 基类把两个回调声明为纯虚函数，并保存「每顶点属性数」与「每顶点参数数」：

[Shader.h:57-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L57-L66) — 定义 `shadeVertices`（最多 16 顶点一批，属性进、参数出）与 `shadePixels`（最多 16 像素一批，插值参数进、颜色出）两个回调签名。注意 `shadePixels` 还接收 `uniforms`（常量）与 `sampler`（纹理指针数组）。

[Shader.h:36-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L36-L42) — `VertexParam` 枚举固定了前 4 个参数的含义：`kParamX, kParamY, kParamZ, kParamW`。这正是「位置必须排在前 4 个」约定的依据。

[Shader.h:81-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L81-L84) — 构造函数接收 `(attribsPerVertex, paramsPerVertex)`，子类在初始化列表里声明。例如 `TextureShader` 写 `Shader(8, 9)`：每顶点 8 个属性进、9 个参数出。

「剥离位置、重新编号」发生在几何阶段装配三角形时：

[RenderContext.cpp:376-383](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L376-L383) — 注释明说「skipping position which is already in x0/y0/z0」。代码从 `params0 + 4` 开始拷贝，每顶点只留 `fParamsPerVertex - 4` 个 varying。于是后续 `shadePixels` 收到的 `inParams[0]` 对应的是原来的第 5 个参数（索引 4）。

以 `TextureShader` 为例看契约如何落地。它输出 9 个参数：

[TextureShader.h:45-68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L45-L68) — `shadeVertices` 把顶点位置乘 MVP 矩阵写入 `outParams[0..3]`（即 x,y,z,w），纹理坐标写到 `outParams[4..5]`，法线乘法线矩阵写到 `outParams[6..8]`。参数布局表如下：

| 输出索引 | 含义 | 去向 |
|----------|------|------|
| 0–3 | 裁剪空间 x,y,z,w | 几何阶段消费（透视除法、光栅化），不传给像素着色器 |
| 4 | 纹理 u | → `inParams[0]` |
| 5 | 纹理 v | → `inParams[1]` |
| 6–8 | 变换后法线 x,y,z | → `inParams[2..4]` |

[TextureShader.h:70-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L70-L97) — `shadePixels` 正是按上表的重新编号读取：`inParams[0]、inParams[1]` 当纹理坐标，`inParams[2..4]` 当法线。若 `fHasTexture` 为真，调用 `sampler[0]->readPixels(...)` 取颜色再乘以光照；否则直接用光照值当灰度。

`DepthShader` 是个极简的调试着色器，用来验证几何是否正确：

[DepthShader.h:34-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/DepthShader.h#L34-L37) — 声明 `Shader(8, 5)`：8 属性进、5 参数出（4 个位置 + 1 个拷贝的 z）。

[DepthShader.h:56-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/DepthShader.h#L56-L69) — `shadePixels` 只收到 1 个 varying（即重编号后的 `inParams[0]`，对应原来的拷贝 z），把它过一个硬编码的线性斜坡变成灰度。注释也坦白这是为特定模型硬编码的（hardcoded），是个快速目检工具。

#### 4.1.4 代码实践

**实践目标**：验证你对「参数重新编号」契约的理解。

**操作步骤**：

1. 打开 `TextureShader.h`，数清 `shadeVertices` 一共写了几个 `outParams[...]`（应为 9，对应 `Shader(8,9)`）。
2. 打开 `TextureShader.h` 的 `shadePixels`，列出它读取的 `inParams` 索引（应为 0、1、2、3、4）。
3. 打开 `RenderContext.cpp:376-383`，确认位置参数（索引 0–3）被跳过、从索引 4 开始拷贝。

**需要观察的现象**：`shadePixels` 的 `inParams[0]` 对应 `shadeVertices` 的 `outParams[4]`，两者索引差正好是 4（被剥离的位置参数个数）。

**预期结果**：你能用一句话说清「为什么 `shadePixels` 里纹理坐标是 `inParams[0]` 而 `shadeVertices` 里纹理坐标写到 `outParams[4]`」——因为前 4 个位置参数被几何阶段剥离了。

#### 4.1.5 小练习与答案

**练习 1**：若一个新着色器想让像素阶段拿到 3 个 varying（比如两种纹理坐标 + 一个颜色），`shadeVertices` 应输出几个参数？构造函数怎么写（假设属性仍为 8）？

**答案**：4（位置）+ 3（varying）= 7 个参数，构造函数写 `Shader(8, 7)`。

**练习 2**：为什么 `shadePixels` 的签名里 `sampler` 是「纹理指针的数组」而不是单个纹理？

**答案**：因为一次绘制可能绑定多个纹理（`RenderState::fTextures` 是 `kMaxActiveTextures = 4` 的数组，见 `RenderState.h:26-38`），像素着色器按槽位号 `sampler[i]` 取用。

---

### 4.2 透视正确的参数插值

#### 4.2.1 概念说明

光栅化把三角形填成像素后，每个像素需要它自己的 varying 值（纹理坐标、法线等）。最朴素的做法是按重心坐标做**线性插值**。但线性插值在屏幕空间里对 3D 透视投影是错的：投影会让远处被压缩，等距的屏幕像素对应的世界距离并不相等，直接线性插值会产生「橡胶皮」般的扭曲纹理——也就是常说的**透视失真**。

解决办法是**透视正确插值（perspective-correct interpolation）**：先在顶点处把每个属性除以深度，连同深度的倒数一起线性插值，最后在像素处再除回去。

#### 4.2.2 核心流程

记顶点属性为 \(a\)，顶点深度为 \(z\)。在屏幕空间用重心权重线性插值（\(\sum b_i = 1\)）。透视正确插值的标准五步法（与 `TriangleFiller.cpp:32-42` 的注释完全对应）：

\[
a'_i = \frac{a_i}{z_i}, \qquad w_i = \frac{1}{z_i}
\]

先线性插值这两个量：

\[
\overline{a'} = \sum_i b_i\, a'_i, \qquad \overline{w} = \sum_i b_i\, w_i
\]

再在每个像素处还原：

\[
z = \frac{1}{\overline{w}}, \qquad a = \overline{a'} \cdot z = \frac{\overline{a'}}{\overline{w}}
\]

直观理解：远处 \(z\) 大、\(1/z\) 小，线性插值时它的权重被自动按 \(1/z\) 缩小，从而抵消透视压缩。`TriangleFiller` 用两个优化：

- **梯度法代替逐像素重心坐标**：把线性插值 \(\overline{a'}(x,y) = c_{00} + g_x x + g_y y\) 写成两个方向梯度，对整个 4×4 块一次 SIMD 算出 16 个值。
- **常数捷径**：若某属性在三个顶点处相等，直接当常数，跳过插值。
- **正交捷径**：若三个顶点 \(z\) 全相等（\(fNeedPerspective = false\)），无需透视修正，普通线性插值即可，省掉除法。

伪代码（`fillMasked` 中的参数循环）：

```
若 fNeedPerspective:
    z = 1.0 / OneOverZInterpolator(x, y)     # 还原 z
    for 每个 param:
        param = LinearInterpolator(x, y) * z  # 插值 a'/...，再乘 z 还原
否则:
    z = z0
    for 每个 param:
        param = LinearInterpolator(x, y)      # 纯线性
```

#### 4.2.3 源码精读

[TriangleFiller.cpp:32-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L32-L42) — 作者把上述五步法写在注释里，并指出参考 Kok-Lim Low 的论文。这是理解整个模块的钥匙。

[TriangleFiller.cpp:63-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L63-L85) — 在 `setUpTriangle` 里，用三角形两条边向量构成 2×2 矩阵并求逆（`oneOverDeterminant`），以便后续对任意参数一次性算出屏幕空间梯度；同时判定 `fNeedPerspective`，并为 \(1/z\) 单独建一个插值器 `fOneOverZInterpolator`。

[TriangleFiller.cpp:107-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L107-L133) — `setUpParam` 三分支：常数、透视（参数先除以 \(z\)）、纯线性。透视分支 `c0/fZ0, c1/fZ1, c2/fZ2` 正是上面公式的 \(a_i/z_i\)。

[TriangleFiller.cpp:142-178](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L142-L178) — `fillMasked` 里先还原 \(z\)（`1.0f / fOneOverZInterpolator.getValuesAt(x, y)`），再对每个参数 `插值 * z` 还原。这就是 SIMD 版的透视正确插值——16 个像素（一个 4×4 块）一并算出。

[LinearInterpolator.h:41-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/LinearInterpolator.h#L41-L44) — 梯度求值仅一行：`x * gx + y * gy + c00`，全程向量运算。

> 说明：源码用 \(z\)（裁剪空间 z）做透视插值，并在 `enqueueTriangle` 里多处标注 `XXX ... a bit of a hack`（z 未严格按 w 归一化）。因此该渲染器的透视插值是工程上够用的近似实现，而非数学上严格按 \(w\) 的形式，这与本项目的「实验性」定位一致。

#### 4.2.4 代码实践

**实践目标**：理解梯度法如何把「逐像素重心坐标」变成「整块 SIMD 求值」。

**操作步骤**：

1. 读 `TriangleFiller.cpp:90-103` 的 `setUpInterpolator`，确认它只做三件事：用逆矩阵把三个顶点的值变成 \(g_x, g_y\)，再算原点值 \(c_{00}\)。
2. 读 `LinearInterpolator.h:41-44`，确认 `getValuesAt` 对 16 个 \((x,y)\) 一次返回 16 个结果。

**需要观察的现象**：无论三角形多大，每个参数的插值器只保存 3 个标量（\(g_x, g_y, c_{00}\)）；16 个像素的求值是同一条向量乘加指令。

**预期结果**：你能解释「为什么不必为每个像素单独算重心坐标」——因为线性函数 \(c_{00} + g_x x + g_y y\) 在屏幕空间是平面，梯度一旦算出，任意位置直接代入即可，且天然适合 SIMD。

#### 4.2.5 小练习与答案

**练习 1**：若三个顶点的 \(z\) 完全相同，`setUpParam` 会走哪条分支？为什么这样是安全的？

**答案**：当 `fNeedPerspective == false` 时走「纯线性」分支（`TriangleFiller.cpp:123-130`）。因为 \(z\) 相同时 \(a/z\) 与 \(a\) 只差一个公共常数因子，线性插值结果再乘回这个因子即可，省去除法。

**练习 2**：`fillMasked` 里还原 \(z\) 用的是 `1.0 / 插值(1/z)`，而不是直接插值 \(z\)。为什么不能直接线性插值 \(z\)？

**答案**：因为 \(z\) 本身在屏幕空间不是线性的（投影压缩了远处），而 \(1/z\) 在屏幕空间才是线性的。直接线性插值 \(z\) 会得到错误的深度，进而让透视还原 \(a = \overline{a'} \cdot z\) 也出错。

---

### 4.3 纹理采样与 mipmap 选择

#### 4.3.1 概念说明

像素着色器拿到插值后的纹理坐标 \((u,v)\)（通常归一化到 0–1），需要把它换算成纹理图上的实际像素并取出颜色——这就是**纹理采样**。两个问题随之而来：

- **走样与闪烁**：当三角形离相机很远时，一个屏幕像素覆盖了一大块纹理，只取单个纹素（texel）会产生摩尔纹与闪烁。**mipmap** 预先生成一组逐级缩小的纹理，采样时按像素覆盖的纹理面积挑一个合适层级，缓解走样。
- **块状感**：最近邻采样直接取最近纹素，放大时呈块状。**双线性滤波**取相邻 4 个纹素按距离加权平均，更平滑。

Nyuzi 的 `Texture` 把这两种手段都实现了，且全程对 16 个通道（16 个像素的纹理坐标）并行处理。

#### 4.3.2 核心流程

```
readPixels(u[16], v[16], mask, outColor)
  │
  ├─ 1. 选 mip 层级
  │     用相邻像素纹理坐标差估计“像素覆盖的纹理面积”
  │     1/差 = 缩放后的纹理尺寸 → clz 折成 log2 → mip 层级
  │
  ├─ 2. (u,v) 归一化 → 纹理光栅坐标 (tx, ty)
  │     frac 取小数部分 + 负数回绕 + v 轴翻转
  │
  ├─ 3a. 双线性：取 4 个相邻纹素 tl/tr/bl/br
  │      按小数权重加权平均 → outColor
  │
  └─ 3b. 最近邻：直接取 (tx,ty) → outColor
```

底层每个纹素的读取由 `Surface::readPixels` 完成：16 个通道各自算出自己的纹素地址，用一次 **gather** 把 16 个 packed 颜色一次性读入，再拆成 4 个 float 通道向量。

#### 4.3.3 源码精读

mip 层级选择用了一个巧妙的位运算技巧——`__builtin_clz`（前导零计数）来算 \(\log_2\)：

[Texture.cpp:86-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L86-L91) — 取相邻像素 \(u\) 坐标之差的倒数得到「缩放后纹理尺寸」，对其取 `clz` 折成 \(\log_2\)，再减去 `fBaseMipBits` 得到 mip 层级，最后夹到 \([0, fMaxMipLevel]\)。注释里的 `XXX` 坦白只看了一个方向（u），是个近似。

`fBaseMipBits` 在注册第 0 层时算好：

[Texture.cpp:62-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L62-L71) — `fBaseMipBits = __builtin_clz(width) + 1`，本质是 \(\lfloor\log_2(\text{width})\rfloor + 1\)，作为层级计算的基准。

坐标换算与回绕：

[Texture.cpp:100-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L100-L103) — 把 \((u,v)\) 经 `fracfv`（取小数部分，`SIMDMath.h:88-91`）与 `wrapfv`（负数加 1 回绕，`Texture.cpp:32-36`）规范化，再乘以 `(width-1)/(height-1)` 得到光栅坐标。注意 v 轴用 `1.0 - ...` 翻转，因为纹理 v=1.0 对应顶部。

双线性滤波：

[Texture.cpp:105-137](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L105-L137) — 取 \((tx,ty)\) 四个角的纹素（`wrapiv` 让坐标越过纹理边缘时回绕到 0，`Texture.cpp:40-44`），按小数部分算出四个权重 `tlWeight/trWeight/blWeight/brWeight`，对 RGBA 四通道各做一次加权平均。四个 `surface->readPixels` 调用就是四次 gather。

最近邻的对照很简单：

[Texture.cpp:139-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L139-L143) — 不开双线性时直接取 \((tx,ty)\) 单点，一次 gather 搞定。

底层纹素读取（gather）：

[Surface.h:109-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L109-L126) — 每个通道算出 `pointers = ty*stride + tx*bpp + base`，用 `__builtin_nyuzi_gather_loadi_masked` 一次读 16 个 packed 颜色（被掩码的通道不读），再拆成 RGBA 四个 float 向量（乘 `1/255`）。这正是 u2-l3 讲过的 gather，16 个地址不同需 16 个 subcycle 串行完成。

mipmap 数据结构：

[Texture.h:26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L26) 与 [Texture.h:59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L59) — `kMaxMipLevels = 8`，纹理持有一个 `fMipSurfaces[8]` 指针数组。注意 `setMipSurface` 不拥有这些 Surface（`Texture.h:35-39` 注释），方便 render-to-texture。

#### 4.3.4 代码实践

**实践目标**：看清 16 个像素的纹理坐标如何变成 16 个纹素颜色。

**操作步骤**：

1. 在 `Texture.cpp:117-120` 数一下双线性分支里调了几次 `surface->readPixels`（应为 4 次：tl/tr/bl/br）。
2. 进到 `Surface.h:113` 确认每次 `readPixels` 用的是 `__builtin_nyuzi_gather_loadi_masked`（gather，不是 block load）。
3. 对比 `Surface.h:76-80` 的 `readBlock`（4×4 块的 gather，用于深度缓冲整块读），理解它与按任意坐标 gather 的区别。

**需要观察的现象**：纹理采样对 16 个通道用 gather（地址各异）；而帧缓冲整块读/写用 `readBlock`/`writeBlockMasked`（地址连续、4×4 对齐）。

**预期结果**：你能解释「为什么纹理采样必须用 gather 而不能用 block load」——因为 16 个像素的纹理坐标各不相同，落点离散，不构成连续 4×4 块。

> 待本地验证：若在模拟器中运行 `sceneview` 并切换 `enableBilinearFiltering`（`sceneview.cpp:171`），可目测块状感与平滑感的差异。

#### 4.3.5 小练习与答案

**练习 1**：mip 层级选择为什么用 `__builtin_clz` 而不是调用 `log2`？

**答案**：`__builtin_clz`（前导零计数）在 Nyuzi 上是单周期硬件指令（见 u2-l2 的 `clz`），而 `log2f` 要走浮点库函数慢得多。用整数位运算近似 \(\log_2\) 是图形里的常见优化。

**练习 2**：双线性滤波读 4 个纹素，但若 `tx+1` 超出纹理宽度会怎样？

**答案**：`wrapiv(tx+1, mipWidth)`（`Texture.cpp:114`）会让越界坐标回绕到 0，即纹理在边缘是**重复（repeat）**包裹模式，而不是夹断（clamp）。

---

### 4.4 混合写回

#### 4.4.1 概念说明

像素着色器只给出 `outColor`（4 个 RGBA float 向量），把它写进帧缓冲还有三件事要做：

1. **格式转换**：float 颜色（0–1）要变成帧缓冲的 `RGBA8888`（每通道 8 位）。
2. **半透明混合（alpha blend）**：若该像素不是全不透明，要和帧缓冲里已有的目标颜色按 alpha 混合，而不是直接覆盖。
3. **掩码写回**：只有通过覆盖测试与深度测试的像素（mask 为 1 的通道）才真正写入，用 scatter 一次性写回 4×4 块。

这三步都在 `TriangleFiller::fillMasked` 的着色之后完成。混合用的是**预乘 alpha（premultiplied alpha）**模型，并且为了避开浮点除法，巧妙地用定点数（左移 8 位再右移 8 位）实现。

#### 4.4.2 核心流程

```
shadePixels → color[4] (RGBA float 向量, 每通道 0..1)
   │
   ├─ 转 8 位: rS = clamp(R)*255,  gS, bS 同理
   │
   ├─ 若 fEnableBlend 且该块存在 alpha<1 的像素:
   │     读目标块 destColors (gather)
   │     aS = A*255;  oneMinusAS = 255 - aS
   │     newR = saturate( ((rS<<8) + rD*oneMinusAS) >> 8, 255 )   # 定点 over 运算
   │
   ├─ 打包: pixelValues = 0xff000000 | R | (G<<8) | (B<<16)
   │
   └─ writeBlockMasked(left, top, mask, pixelValues)   # scatter, 只写 mask=1 的通道
```

预乘 alpha 的 over 运算（src over dst）标准形式为：

\[
C_{\text{out}} = C_{\text{src}} + (1 - \alpha_{\text{src}})\, C_{\text{dst}}
\]

源码用定点数近似：把 `rS` 左移 8 位（相当于乘 256），加上 `rD * (255 - aS)`，再右移 8 位（除以 256）。由于 `aS = 255·α`，`oneMinusAS/256 ≈ 1 - α`，于是结果约等于上式，且全程整数运算、无除法。

#### 4.4.3 源码精读

[TriangleFiller.cpp:181-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L181-L183) — 调用 `shadePixels` 拿到 `color[4]`，把 uniforms、sampler、当前 mask 一并传入。

[TriangleFiller.cpp:191-221](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L191-L221) — `RGBA8888` 分支：先把三通道 `clamp` 到 \([0,1]\) 再乘 255 转 8 位（`TriangleFiller.cpp:194-196`）。混合判定 `fEnableBlend && (mask 内存在 alpha<1)` 才走混合路径（`TriangleFiller.cpp:199-200`），否则视为全不透明直接打包（`TriangleFiller.cpp:217-218`）。混合分支里 `readBlock` 读目标颜色、按位拆出 `rD/gD/bD`，再做定点 over 运算 `saturate(((rS<<8) + rD*oneMinusAS)>>8, 255)`（`TriangleFiller.cpp:212-214`），最后打包成 `0xff000000 | newR | newG<<8 | newB<<16`。

[TriangleFiller.cpp:223-226](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L223-L226) — `FLOAT` 颜色空间分支：直接把通道 0 当 float 存。这是**深度缓冲**用的格式——深度缓冲是一个 `Surface::FLOAT` 的 Surface（见 `sceneview.cpp:201`），所以 Z 值经此路径写入。

[TriangleFiller.cpp:232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L232) — `writeBlockMasked` 把 4×4 块 scatter 写回，mask 控制只写覆盖到的像素。

scatter 写回的实现：

[Surface.h:68-72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L68-L72) — 用 `f4x4AtOrigin`（构造时预计算的 4×4 像素地址偏移表，见 u9-l3）加上 `(left,top)` 算出 16 个指针，调 `__builtin_nyuzi_scatter_storei_masked` 一次性写 16 个像素，被掩码的通道不写。

混合开关的来源：

[RenderState.h:30-31](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderState.h#L30-L31) — `fEnableDepthBuffer` 与 `fEnableBlend` 是每条绘制命令的状态（默认 false）。`sceneview` 没有显式开 blend，故实际走的是全不透明快速路径。

#### 4.4.4 代码实践

**实践目标**：理解定点数 over 运算如何替代浮点除法。

**操作步骤**：

1. 读 `TriangleFiller.cpp:212-214`，把 `((rS<<8) + rD*oneMinusAS)>>8` 展开。
2. 代入一个具体值：设 `aS = 128`（即 α≈0.5）、`rS = 200`、`rD = 100`，手算 `newR`。

**需要观察的现象**：手算结果应接近 `200 + 100*(1-0.5) = 250`，定点版会因 `/256` 而略微偏小（约 249）。

**预期结果**：你能说清「为什么用 `<<8 / >>8` 而不是直接除以 255」——定点乘除用移位实现，避开慢得多的整数除法指令，精度损失可接受。

#### 4.4.5 小练习与答案

**练习 1**：`sceneview` 没有调用 `enableBlend`，混合分支会被执行吗？

**答案**：不会。`TriangleFiller.cpp:199` 的判定要求 `fState->fEnableBlend` 为真，`sceneview` 用默认值 false，所以走 `TriangleFiller.cpp:217-218` 的全不透明快速打包路径。

**练习 2**：为什么混合前要先用 `readBlock` 把目标颜色读回来？

**答案**：over 运算 \(C_{\text{src}} + (1-\alpha)C_{\text{dst}}\) 需要目标色 \(C_{\text{dst}}\)，必须先从帧缓冲读出当前已绘制的内容，与源色混合后再写回。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，追踪 sceneview 中**一个带纹理像素**从顶点属性到帧缓冲的完整旅程，并做一个可预测结果的小修改。

**步骤**：

1. **装配**（`sceneview.cpp`）：读 `sceneview.cpp:209` 的 `bindShader(new TextureShader())`、`sceneview.cpp:171` 的 `enableBilinearFiltering(true)`、`sceneview.cpp:215-218` 的 uniforms（光照方向、环境光、平行光强度）、`sceneview.cpp:241` 的 `bindTexture`。说清这些数据各通过哪个参数进入着色器。
2. **顶点着色**（`TextureShader.h:45-68`）：确认每顶点 8 个属性 → 9 个参数（4 位置 + uv + 法线），位置参数被几何阶段消费。
3. **插值**（`TriangleFiller.cpp:142-178`）：确认像素阶段的 `inParams` 是去掉位置后重新编号的 varying，且是透视正确的。
4. **像素着色 + 采样**（`TextureShader.h:70-97` → `Texture.cpp:78-144` → `Surface.h:109-126`）：纹理坐标 `inParams[0,1]` 经 mip 选择、坐标换算、双线性滤波、gather 取纹素，再乘朗伯光照。
5. **混合写回**（`TriangleFiller.cpp:181-232`）：颜色转 `RGBA8888`，因未开 blend 走快速打包，scatter 写回帧缓冲。
6. **小修改与预测**：把 `TextureShader.h:81` 的环境光系数加到 `clamp` 之外（例如把 `uniforms->fAmbient` 从 0.4 临时改成 0.0），预测整体画面会明显变暗（背光面失去环境光兜底）。
7. **验证**：在模拟器中重新构建并运行 `sceneview`（需 `resource.bin`），目测画面明暗变化。

**预期结果**：你能画出一张包含「顶点属性 → 9 参数 → 剥离位置 → 5 varying 插值 → gather 采样 → 光照 → 打包 → scatter 写回」的完整数据流图，并正确预测改 `fAmbient` 后的视觉效果。

> 待本地验证：步骤 7 的实际运行画面需在本地或容器中构建工具链与 `sceneview` 目标后确认（参考 u1-l2 的构建流程）。

## 6. 本讲小结

- **着色器契约**：应用继承 `Shader`，实现 `shadeVertices`（属性进、参数出）与 `shadePixels`（插值参数进、颜色出）两个回调；构造函数声明 `(属性数, 参数数)`，位置参数 x/y/z/w 必须排在前 4 个。
- **参数重新编号**：几何阶段剥离前 4 个位置参数，只把剩下的 varying 拷进三角形并从 0 重编号传给 `shadePixels`，因此 `shadePixels` 的 `inParams[i]` 对应 `shadeVertices` 的 `outParams[i+4]`。
- **透视正确插值**：先除以 \(z\) 线性插值、再乘 \(z\) 还原；用梯度法把逐像素重心坐标变成整块 SIMD 求值；\(z\) 相同时走纯线性捷径。
- **纹理采样**：mip 层级用 `clz` 近似 \(\log_2\) 选择，坐标经 `frac`+回绕规范化，双线性滤波取 4 角纹素加权，底层用 gather 一次读 16 个离散地址的纹素。
- **混合写回**：颜色经 `clamp×255` 转 8 位，半透明用预乘 alpha 的定点 over 运算（`<<8 / >>8` 避免除法），最后按 mask 用 scatter 写回 4×4 块；深度缓冲走 `FLOAT` 路径。

## 7. 下一步学习建议

本讲完成了 librender 像素阶段的最后一块拼图，至此图形渲染管线单元（u13）已闭环。建议：

- **横向对照**：回到 u9-l3 把 `Surface` 的 `readBlock`/`writeBlockMasked`（块 gather/scatter）与本讲的按坐标 gather 对比，体会「对齐块 vs 离散地址」两种访存模式。
- **性能视角**：结合 u11-l2 的性能计数器，统计一个渲染帧里 `dcache` 缺失与 gather/scatter 的代价，理解纹理采样为何是带宽热点。
- **延伸阅读**：若想加深透视插值与光栅化的理论，可读 `TriangleFiller.cpp:32-42` 与 `Rasterizer.cpp:17-22` 注释里提到的两篇经典文献（Kok-Lim Low 的透视插值论文、Ned Greene 的层次化覆盖掩码）。
- **动手扩展**：尝试写一个新着色器（例如纯色 + 雾效），正确设置 `(属性数, 参数数)` 与位置参数布局，验证你对本讲契约的掌握。
