# 着色器与纹理采样

## 1. 本讲目标

上一讲（u13-l2）我们跟着 `Rasterizer` 递归细分三角形，最终对每个被覆盖的 4×4 像素块回调了 `TriangleFiller::fillMasked`，并在那里做了 Z-buffer 早期剔除。但「一个像素到底应该是什么颜色」这件事，我们一直当作黑盒——它正是本讲要打开的内容。

学完本讲，你应当能够：

- 说清 librender 的**着色器回调契约**：应用如何通过继承 `Shader`、实现 `shadeVertices` / `shadePixels` 两个回调，把「变换 + 光照 + 采样」的逻辑插进渲染管线；
- 解释**透视正确插值**为什么要先除以 Z、再乘回 Z，并能对照 `TriangleFiller` 的源码指出这五步分别落在哪几行；
- 读懂 `Texture::readPixels` 如何**挑选 mipmap 层级、做双线性滤波**，以及它如何把 16 个像素的采样压成几次 gather；
- 解释 `fillMasked` 末尾的**混合（blend）写回**：premultiplied alpha、clamp 到 8bpp、最终用掩码写回帧缓冲；
- 把这四件事串成「插值参数 → 采样纹理 → 计算光照 → 混合写回」一条完整的像素处理链路。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **16 通道 SIMD 数据模型**（u2-l1 / u9-l3）：`vecf16_t` 是 16 个 `float` 拼成的向量，恰好映射一个 4×4 像素块；`vmask_t` 是 16 位掩码，逐通道控制是否参与运算。
- **gather / scatter 访存**（u2-l3）：当 16 个通道各自需要一个不同地址的内存数据时，用一次 gather 读、一次 scatter 写；这正是纹理采样与帧缓冲写回的底层机制。
- **tile-based 渲染架构**（u9-l3 / u13-l1）：`RenderContext::finish()` 把渲染拆成几何阶段与像素阶段，像素阶段每个线程独占一个 64×64 的 tile，tile 互不重叠。
- **光栅化与覆盖测试**（u13-l2）：`Rasterizer` 用边函数递归细分，对被覆盖的 4×4 块产出 16 位掩码 `mask`，再交给 `TriangleFiller::fillMasked`。

几个本讲要用到、但不展开讲的术语：

- **顶点着色 / 像素着色（shader）**：可编程的处理阶段。顶点着色器对每个顶点算出一组「随顶点变化的参数（varying）」；像素着色器对每个像素算出最终颜色。
- **varying（可变参数）**：随三角形内部位置变化、需要被插值的量，例如纹理坐标、法线、颜色。
- **uniform（统一参数）**：整次绘制里所有顶点 / 像素共享、不随位置变化的量，例如 MVP 矩阵、光源方向。
- **mipmap**：同一张纹理逐级减半的多分辨率版本，远处物体用小图、近处用大图，既省带宽又防锯齿。
- **双线性滤波（bilinear filtering）**：在 4 个相邻纹素之间按权重插值，得到平滑的采样结果。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `software/libs/librender/Shader.h` | 着色器抽象基类，定义 `shadeVertices` / `shadePixels` 两个回调契约。 |
| `software/libs/librender/TriangleFiller.cpp` | 像素阶段的核心：透视正确插值、调用 `shadePixels`、混合写回。 |
| `software/libs/librender/LinearInterpolator.h` | 二维线性插值器，把参数梯度变成 16 通道 SIMD 求值。 |
| `software/libs/librender/Texture.cpp` / `Texture.h` | 纹理采样：mipmap 选择 + 双线性 / 最近邻滤波。 |
| `software/libs/librender/RenderContext.cpp` | 几何阶段：调用顶点着色器、剥离位置参数、把 varying 喂给 `TriangleFiller`。 |
| `software/apps/sceneview/TextureShader.h` | 示例着色器：带纹理 + Lambert 光照。 |
| `software/apps/sceneview/DepthShader.h` | 示例着色器：把深度画成灰度（更简单，用于对比）。 |
| `software/apps/sceneview/sceneview.cpp` | 应用主程序：装配着色器、纹理、uniform 并提交绘制。 |

## 4. 核心概念与源码讲解

### 4.1 着色器接口：Shader 回调契约

#### 4.1.1 概念说明

librender 没有像 OpenGL 那样用一门独立的着色器语言（GLSL），而是直接用 **C++ 继承** 来实现可编程管线。应用写一个继承 `librender::Shader` 的子类，实现两个虚函数：

- `shadeVertices`：输入一批顶点的属性（位置、纹理坐标、法线…），输出每个顶点的「参数（params）」。这一步在**几何阶段**被调用。
- `shadePixels`：输入一批像素被插值后的参数，输出每个像素的颜色。这一步在**像素阶段**被调用。

关键设计是「**批处理 + SIMD**」：两个回调一次都处理**最多 16 个**元素（16 个顶点或 4×4=16 个像素），用 `vecf16_t` 一次算完。这样应用的着色逻辑天然就是向量化的，Nyuzi 的 LLVM 工具链会把 `vecf16_t` 运算直接编成 16 通道 SIMD 指令。这种「库在合适的时机以 16 个一批地调用你」的模式就是**回调契约**：库规定签名、调用时机、数据布局，你只管填算法。

#### 4.1.2 核心流程

回调的调用链（几何阶段 → 像素阶段）：

```text
几何阶段 RenderContext::shadeVertices(index)
  ├─ gather 16 个顶点的属性 → packedAttribs[]
  ├─ state.fShader->shadeVertices(packedParams, packedAttribs, uniforms, mask)
  └─ scatter 把 packedParams 写回顶点参数缓冲

几何阶段 RenderContext::enqueueTriangle
  ├─ 用 params[0..3]（XYZW）做透视除法、背面剔除、binning
  └─ 把 params[4..]（varying）拷进 tri.params  ← 位置被剥离

像素阶段 TriangleFiller::fillMasked
  ├─ 透视插值得到 interpolatedParams[]
  ├─ state.fShader->shadePixels(color, interpolatedParams, uniforms, textures, mask)
  └─ 混合并写回帧缓冲
```

两个**重要约定**：

1. **顶点着色器输出的前 4 个参数永远是裁剪空间位置 X、Y、Z、W**（对应枚举 `kParamX/Y/Z/W`）。它们被几何阶段消费（做透视除法、背面剔除、装进 tile 队列），**不会**传给像素着色器。
2. 真正传给 `shadePixels` 的 `inParams[0..]` 是从第 5 个参数开始的 varying，并且**从 0 重新编号**。这就是为什么 `TextureShader` 的 `shadePixels` 里 `inParams[0]` 是纹理坐标 u 而不是 X。

#### 4.1.3 源码精读

**基类契约**：[Shader.h:48-89](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L48-L89) 定义了两个纯虚函数、颜色通道枚举与构造参数。

构造函数接受两个计数（[Shader.h:81-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L81-L84)）：

- `attribsPerVertex`：每个顶点输入多少个属性；
- `paramsPerVertex`：每个顶点输出多少个参数（含前 4 个位置 XYZW）。

两个回调签名（[Shader.h:57-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L57-L66)）：

```cpp
virtual void shadeVertices(vecf16_t *outParams, const vecf16_t *inAttribs,
                           const void *uniforms, vmask_t mask) const = 0;
virtual void shadePixels(vecf16_t *outColor, const vecf16_t *inParams,
                         const void *uniforms, const Texture * const * sampler,
                         vmask_t mask) const = 0;
```

前 4 个参数的位置语义由枚举固定（[Shader.h:36-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h#L36-L42)）：`kParamX, kParamY, kParamZ, kParamW`。`mask` 标记这批 16 个元素里哪些有效（最后一批顶点可能不足 16 个），无效通道的写回会被流水线掩码掉。

**几何阶段如何调用顶点着色器**：[RenderContext.cpp:149-181](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L149-L181)。它先用 `gatherElements` 把 16 个顶点的同一属性聚成一个 `vecf16_t`，调用着色器，再用 `__builtin_nyuzi_scatter_storef_masked` 把结果散开写回每个顶点的参数槽。

**位置剥离**：[RenderContext.cpp:376-383](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L376-L383) 注释写得很清楚——「skipping position which is already in x0/y0/z0」。它把三个顶点的 `params + 4`（跳过 4 个位置浮点）拷进 `tri.params`，按 `(paramsPerVertex - 4)` 的步长紧凑排列。

**像素阶段调用像素着色器**：[TriangleFiller.cpp:180-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L180-L183)：

```cpp
vecf16_t color[4];
fState->fShader->shadePixels(color, interpolatedParams, fState->fUniforms,
                             fState->fTextures, mask);
```

第 4 个参数 `sampler` 是一个纹理指针数组，`shadePixels` 用 `sampler[0]` 访问第 0 号纹理单元。这正是 4.3 节纹理采样的入口。

**示例：TextureShader** 的参数计数与回调见 [TextureShader.h:40-68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L40-L68)。构造为 `Shader(8, 9)`：每顶点 8 个属性、9 个输出参数。其中：

- 属性：`inAttribs[0..2]`=位置、`inAttribs[3..4]`=纹理坐标 uv、`inAttribs[5..7]`=法线；
- 输出参数：`outParams[0..3]`=MVP×位置（XYZW）、`outParams[4..5]`=纹理坐标（原样拷贝）、`outParams[6..8]`=法线矩阵×法线。

| 输出索引 | 含义 | 去向 |
|----------|------|------|
| 0–3 | 裁剪空间 x,y,z,w | 几何阶段消费（透视除法、binning），不传给像素着色器 |
| 4 | 纹理 u | → `inParams[0]` |
| 5 | 纹理 v | → `inParams[1]` |
| 6–8 | 变换后法线 x,y,z | → `inParams[2..4]` |

去掉前 4 个位置后，剩下 varying 是 `uv(2) + normal(3) = 5` 个，于是 `shadePixels` 收到的 `inParams[0]`=u、`inParams[1]`=v、`inParams[2..4]`=法线——与 [TextureShader.h:70-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L70-L97) 的用法完全吻合。

**对照：DepthShader** 是个极简调试着色器（[DepthShader.h:31-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/DepthShader.h#L31-L37)），构造 `Shader(8, 5)`：8 属性进、5 参数出（4 位置 + 1 个把 z 复制到 `outParams[4]` 的深度，[DepthShader.h:52-54](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/DepthShader.h#L52-L54)）。剥离位置后只剩 1 个 varying，故 `shadePixels` 的 `inParams[0]` 就是插值后的深度，再过一个硬编码线性斜坡变灰度（[DepthShader.h:56-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/DepthShader.h#L56-L69)）。

#### 4.1.4 代码实践

**实践目标**：确认「前 4 个参数是位置、被剥离；`shadePixels` 的 `inParams` 从 varying 开始」这一约定。

**操作步骤**：

1. 打开 `TextureShader.h`，看构造函数 `Shader(8, 9)`，确认 9 个输出参数里前 4 个是 `outParams[0..3]`（MVP×位置）。
2. 打开 `RenderContext.cpp:376-383`，确认 `enqueueTriangle` 只拷贝 `params + 4` 之后的内容到 `tri.params`。
3. 打开 `TriangleFiller.cpp:162-183`，确认 `fillMasked` 把插值结果原样传给 `shadePixels` 的 `inParams`。
4. 对照 `TextureShader.h:77-85`，确认 `inParams[0]/[1]` 被当成 u/v、`inParams[2..4]` 被当成法线。

**需要观察的现象**：参数下标在「着色器输出」「三角结构体」「像素着色器输入」三处之间的偏移关系——前 4 个位置参数在像素阶段被「吃掉」了。

**预期结果**：你能画一张表，把 `TextureShader` 的 9 个输出参数逐一对应到「是否传给像素着色器」和「像素着色器里的下标」。

#### 4.1.5 小练习与答案

**练习 1**：若一个新着色器想让像素阶段拿到 3 个 varying（比如两组纹理坐标 + 一个颜色），`shadeVertices` 应输出几个参数？构造函数怎么写（假设属性仍为 8）？

**答案**：4（位置）+ 3（varying）= 7 个参数，构造函数写 `Shader(8, 7)`。前 4 个必须是 XYZW。

**练习 2**：为什么 `shadePixels` 的签名里既有 `uniforms` 又有 `sampler`，而不把纹理塞进 `uniforms`？

**答案**：`uniforms` 是一块任意的、由应用定义结构的内存（`const void*`，应用自行 `static_cast`），用来传矩阵、光照等标量参数；而 `sampler` 是 `const Texture * const *`，专门用来传纹理对象，使 `Texture::readPixels` 这种「带掩码、向量化的纹素读取」能被着色器直接调用。两者职责分离，签名更清晰。

---

### 4.2 透视正确参数插值

#### 4.2.1 概念说明

三角形经过透视投影后，屏幕空间里「等距」并不等于 3D 空间里「等距」。如果直接在屏幕空间线性插值纹理坐标，贴图会发生**扭曲**（典型的「仿射纹理映射」缺陷，PS1 时代常见）——远处被透视压缩的部分会被错误地均匀拉平。

解决办法叫**透视正确插值（perspective-correct interpolation）**：线性插值会失真，是因为它没考虑「远处的东西在屏幕上被压缩」。只要把参数先除以 Z，对 `参数/Z` 和 `1/Z` 分别做屏幕空间线性插值，最后再乘回 Z，就能还原正确的值。

#### 4.2.2 核心流程

`TriangleFiller.cpp` 顶部的一段注释（[TriangleFiller.cpp:32-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L32-L42)）把算法概括成五步：

1. 在每个顶点处，把参数值除以 Z；
2. 在每个顶点处，取 Z 的倒数（`1/Z`）；
3. 在屏幕空间对第 1、2 步的量做线性插值；
4. 在每个像素处，对插值后的 `1/Z` 取倒数，还原出该像素的 Z；
5. 在每个像素处，把插值后的「参数/Z」乘以第 4 步的 Z，得到透视正确的参数。

数学上，设屏幕空间重心权重为 \(\alpha_i\)（\(\sum \alpha_i = 1\)），正确的透视插值为：

\[
c(p)=\frac{\sum_i \alpha_i(p)\,(c_i/Z_i)}{\sum_i \alpha_i(p)\,(1/Z_i)}
\]

即分子是「参数/Z」的线性插值，分母是「1/Z」的线性插值。代码里把分母的倒数记为 `zValues`，于是 \(c(p)=(\text{参数}/Z\text{ 的插值})\times zValues\)，恰好对应第 5 步。

> **链接知识**：Nyuzi 没有硬件除法（见 u5-l3），所以这里**只对 Z 做一次除法**（`zValues = 1.0f / fOneOverZInterpolator.getValuesAt(...)`，一个 16 通道向量除法），所有参数复用这同一个 `zValues`，只需再做「乘加 + 乘」即可。这把昂贵的除法降到每像素块一次。

#### 4.2.3 源码精读

**setUpTriangle**（[TriangleFiller.cpp:44-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L44-L88)）先从三个顶点的屏幕坐标算出一张「逆梯度矩阵」`fInvGradientMatrix**`（[TriangleFiller.cpp:63-75](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L63-L75)）。它的作用是：给定任意一个参数在三个顶点的值 \(c_0,c_1,c_2\)，能立刻解出该参数在屏幕空间的水平 / 垂直梯度（gx, gy），从而对每个像素用 `c = x*gx + y*gy + c00` 求值。

接着是一个关键优化分支（[TriangleFiller.cpp:77-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L77-L85)）：

```cpp
if (fZ0 == fZ1 && fZ0 == fZ2)
    fNeedPerspective = false;     // 三个 Z 相等：纯线性即可，省去除法
else {
    fNeedPerspective = true;
    setUpInterpolator(fOneOverZInterpolator, 1.0f / z0, 1.0f / z1, 1.0f / z2);
}
```

`fOneOverZInterpolator` 就是上面公式里的「分母」——对 `1/Z` 做线性插值。

**setUpParam**（[TriangleFiller.cpp:107-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L107-L133)）为每个参数配一个插值器，分三种情况：

```cpp
if (c0 == c1 && c0 == c2)            // 整个三角形上恒定：跳过插值
    fParameters[fNumParams].isConstant = true;
else if (fNeedPerspective)           // 透视：插值 c/Z
    setUpInterpolator(..., c0/fZ0, c1/fZ1, c2/fZ2);
else                                 // 退化：纯线性
    setUpInterpolator(..., c0, c1, c2);
```

**fillMasked 里的逐像素求值**（[TriangleFiller.cpp:142-178](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L142-L178)）：

```cpp
vecf16_t zValues;
if (fNeedPerspective)
    zValues = 1.0f / fOneOverZInterpolator.getValuesAt(x, y);   // 第 4 步
else
    zValues = fZ0;
...
else if (fNeedPerspective)
    interpolatedParams[paramIndex] =
        fParameters[paramIndex].linearInterpolator.getValuesAt(x, y) * zValues;  // 第 5 步
```

`LinearInterpolator::getValuesAt` 就是 `x*gx + y*gy + c00`（[LinearInterpolator.h:41-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/LinearInterpolator.h#L41-L44)），一次向量乘加搞定 16 个像素。注意 `zValues` 还兼任 4.4 节深度测试的输入——一举两得。

> 说明：源码用裁剪空间 z 做透视插值，`enqueueTriangle` 里有 `XXX ... a bit of a hack` 标注（z 未严格按 w 归一化）。因此这是工程上够用的近似实现，与本项目「实验性」定位一致。

#### 4.2.4 代码实践

**实践目标**：理解「三个 Z 相等时跳过透视」这条快速路径的意义。

**操作步骤**：

1. 在 [TriangleFiller.cpp:77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L77) 处看 `fNeedPerspective` 的判定。
2. 设想一个所有顶点 Z 都相同（正对着摄像机的平面）的三角形：它会走 `fNeedPerspective=false` 分支，`setUpParam` 直接用 `c0,c1,c2` 线性插值（[TriangleFiller.cpp:127-130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L127-L130)），`fillMasked` 里也不再算 `zValues`（用常量 `fZ0`）。
3. 对比一个斜着摆放的三角形：必须走透视分支，每像素多一次向量乘法。

**需要观察的现象**：快速路径省掉的是「每像素块的 `1/Z` 除法」和「每个参数乘 `zValues`」。

**预期结果**：你能说清为什么这个优化对性能重要——除法是 Nyuzi 上最贵的运算之一（具体周期数「待本地验证」，可结合 u11-l2 性能计数测量）。

#### 4.2.5 小练习与答案

**练习 1**：为什么是「参数/Z」和「1/Z」都做线性插值，而不是直接对「参数」和「Z」做线性插值？

**答案**：因为透视投影后，屏幕空间重心权重 \(\alpha_i\) 实际正比于 \(1/Z_i\)。只有把 \(1/Z_i\) 吸收进权重（即插值「参数/Z」和「1/Z」），线性组合的结果才在 3D 空间里正确。直接线性插值「参数」会得到仿射映射，产生贴图扭曲。

**练习 2**：`fParameters[paramIndex].isConstant = true`（[TriangleFiller.cpp:112-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L112-L113)）这条优化在什么场景下会命中？

**答案**：当某个参数在三角形三个顶点处取值相同（如整张面共用一个常量颜色或常量光照系数）时命中。此时插值无意义，`fillMasked` 直接用 `constantValue` 填充 16 个通道（[TriangleFiller.cpp:166-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L166-L167)），省掉一次向量乘加。

---

### 4.3 纹理采样与 mipmap 选择

#### 4.3.1 概念说明

像素着色器拿到插值后的纹理坐标 `(u, v)`（都在 0~1 范围），需要把它转换成「从纹理图里读哪个像素（纹素 texel）」。这件事由 `Texture::readPixels` 完成。它一次处理 16 个像素的 `(u,v)`（两个 `vecf16_t`），输出 4 个 `vecf16_t`（RGBA 四通道），与 SIMD 模型天然契合。

采样要解决两个问题：

1. **选哪一层 mipmap**：远处三角形在屏幕上很小，一个像素覆盖很多纹素，用原图会闪烁 / 锯齿；应选较小的 mipmap 层。
2. **读哪个 / 哪些纹素**：最近邻只读一个纹素（快但粗糙，放大时呈块状）；双线性读 4 个相邻纹素按权重融合（平滑）。

#### 4.3.2 核心流程

`Texture::readPixels` 的整体流程：

```text
输入: u, v (各 16 通道), mask
1. 由相邻像素的 u 差值估算「屏幕像素覆盖了多少纹素」→ log2 → mipLevel
2. 选 fMipSurfaces[mipLevel]，取其宽高
3. (u,v) ∈ [0,1] → 纹理栅格坐标 (tx, ty)，v 翻转 + wrap
4. 若开启双线性:
   a. 读 4 个角纹素 tl/tr/bl/br（坐标 +1 处做 wrap）
   b. 用小数部分算 4 个权重
   c. 4 通道分别加权融合
   否则: 最近邻，直接读 (tx, ty)
输出: outColor[0..3] (RGBA)
```

#### 4.3.3 源码精读

**mipmap 层级选择**（[Texture.cpp:86-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L86-L91)）：

```cpp
int mipLevel = __builtin_clz(static_cast<unsigned int>(1.0f /
                             __builtin_fabsf(u[1] - u[0]))) - fBaseMipBits;
```

这里的直觉是：`u[1] - u[0]` 是这 16 个像素里相邻两个像素的 u 差值（[Texture.cpp:84-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L84-L85) 注释指出「只看一个方向」是个 hack）。它的倒数 `1/Δu` 近似「一张完整纹理在 u 方向被分成了多少段」，也就是该 mip 层的纹素宽度。对它取 `log2` 就得到层级。

代码用了一个**位运算技巧**实现 `log2`：`__builtin_clz(x)` 是「前导零计数」，对 32 位整数 \(x\) 有 \(\text{clz}(x) = 31 - \lfloor\log_2 x\rfloor\)。所以 `clz(1/Δu)` 折算出纹理跨度，再减去基准 `fBaseMipBits`（`setMipSurface` 里由基准层宽度算出，[Texture.cpp:62-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L62-L64)）就得到相对层级，最后 clamp 到 `[0, fMaxMipLevel]`。

> 链接知识：`__builtin_clz` 对应 Nyuzi ISA 的 `clz` 指令在 C 层的内建（见 u2-l2），单周期完成，比 `log2f` 库函数快得多。

**坐标转换**（[Texture.cpp:97-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L97-L103)）：把 `[0,1]` 的 `(u,v)` 转成纹素栅格坐标。注意 `v` 被翻转（`1.0 - ...`），因为纹理 v=1 对应图片顶部；`wrapfv` / `fracfv` 处理坐标超出 `[0,1]` 时的环绕（repeat）。

**双线性滤波**（[Texture.cpp:105-138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L105-L138)）：

```cpp
veci16_t xPlusOne = wrapiv(tx + 1, mipWidth);   // 右邻纹素，边界环绕
veci16_t yPlusOne = wrapiv(ty + 1, mipHeight);  // 下邻纹素

surface->readPixels(tx, ty, mask, tlColor);
surface->readPixels(tx, yPlusOne, mask, blColor);
surface->readPixels(xPlusOne, ty, mask, trColor);
surface->readPixels(xPlusOne, yPlusOne, mask, brColor);

vecf16_t wu = fracfv(uRaster);   // 水平小数权重
vecf16_t wv = fracfv(vRaster);   // 垂直小数权重
...
for (int channel = 0; channel < 4; channel++)
    outColor[channel] = (tlColor[channel]*tlWeight) + (blColor[channel]*blWeight)
                      + (trColor[channel]*trWeight) + (brColor[channel]*brWeight);
```

设纹素栅格坐标的小数部分为 \((f_u, f_v)\)，则四角权重为：

\[
w_{tl}=(1-f_u)(1-f_v),\quad w_{tr}=f_u(1-f_v),\quad w_{bl}=(1-f_u)f_v,\quad w_{br}=f_uf_v
\]

四次 `surface->readPixels` 都是带掩码的 gather（见 u9-l3 的 Surface 抽象），把 16 个像素各自需要的纹素一次读齐，再向量加权——这就是「4×4 块对齐 16 通道」带来的红利。

**最近邻**（[Texture.cpp:140-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L140-L143)）：关闭滤波时直接读 `(tx, ty)`，一次 gather 出 16 个纹素。

**mipmap 数据结构**：纹理持有一个 `fMipSurfaces[kMaxMipLevels]`（`kMaxMipLevels = 8`）指针数组（[Texture.h:26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L26)、[Texture.h:59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L59)）。`setMipSurface` 只持指针、不拥有 Surface（[Texture.h:35-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L35-L39)），天然支持 render-to-texture。

**着色器侧的调用**：[TextureShader.h:83-89](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L83-L89)：

```cpp
if (uniforms->fHasTexture) {
    sampler[0]->readPixels(inParams[0], inParams[1], mask, outColor);  // u, v → RGBA
    outColor[kColorR] *= illumination;   // 再乘光照
    ...
}
```

`sampler[0]` 就是 `Texture*`，`readPixels` 把采样结果直接写进 `outColor`，着色器再叠上 Lambert 光照。

#### 4.3.4 代码实践

**实践目标**：观察 mipmap 层级如何随「像素覆盖的纹素数」变化。

**操作步骤**：

1. 阅读 [Texture.cpp:86-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L86-L91) 的 mipLevel 计算。
2. 阅读 [Texture.cpp:62-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L62-L64) 看 `fBaseMipBits` 如何由基准层宽度算出（`__builtin_clz(width)+1`）。
3. 做一个手算：基准纹理宽 256，`fBaseMipBits = clz(256)+1 = 23+1 = 24`。若某像素块 `1/Δu` 折算的纹素跨度约为 64，`mipLevel = clz(64) - 24 = 25 - 24 = 1`，即选第 1 层 mip（宽 128）。
4. 在 `sceneview.cpp` 里看纹理如何被装配多级 mip（[sceneview.cpp:170-181](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/sceneview.cpp#L170-L181)），每一级 `width >> mipLevel`。

**需要观察的现象**：物体离摄像机越远，屏幕上相邻像素的 u 差越小，`1/Δu` 越大，`clz` 越小，`mipLevel` 越大（选更小的 mip）。

**预期结果**：你能用一句话解释「为什么远处自动用小图」——因为 mip 层级是从「屏幕像素覆盖的纹素数」反推出来的。具体运行数值「待本地验证」（可临时在 `readPixels` 加一行 `printf` 打印 mipLevel）。

#### 4.3.5 小练习与答案

**练习 1**：[Texture.cpp:84-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L84-L85) 的注释承认 mip 选择「只看 u 一个方向」是 hack。这在什么情况下会选错层级？

**答案**：当纹理在 v 方向被极端压缩而 u 方向没有时（例如三角形被纵向压扁），只看 u 会低估覆盖，选偏大的 mip 层，导致采样过于模糊。理想做法应取 u、v 两个方向跨度的较大值。

**练习 2**：双线性滤波要读 4 个纹素，但代码用 `surface->readPixels` 只调了 4 次。为什么对 16 个像素来说「4 次 gather」就够了，而不是 64 次单独读取？

**答案**：因为 `Surface` 的 `readPixels` 接受 `veci16_t` 坐标、用 gather 一次读 16 个像素各自对应的纹素（u9-l3）。4 次 gather 分别取 16 个像素的左上、左下、右上、右下纹素，共 64 个纹素值，恰好覆盖双线性所需的全部输入。这正是 4×4 块对齐 16 通道向量带来的效率。

---

### 4.4 混合写回帧缓冲

#### 4.4.1 概念说明

`shadePixels` 算出的 `color[4]`（RGBA，每个通道是 `vecf16_t`，浮点，一般 0~1）还不能直接写进帧缓冲。帧缓冲通常是 `RGBA8888`（每通道 8 位整数），而且如果开了混合（blend），还要把新颜色和帧缓冲里已有的旧颜色按 alpha 融合。这一步在 `fillMasked` 末尾完成，是像素处理的最后一站。混合用的是**预乘 alpha（premultiplied alpha）**模型，并用定点数（移位）避开浮点 / 整数除法。

#### 4.4.2 核心流程

```text
shadePixels 产出 color[0..3] (浮点 RGBA, 16通道)
  ↓
每通道 clamp 到 [0,1] 再 ×255 → 8bpp 整数 (vecu16_t)
  ↓
若开启混合 且 有像素 alpha < 1:
  读回帧缓冲旧颜色 destColors (gather)
  premultiplied alpha 融合: new = src + dest*(1-α)   (定点 <<8 / >>8)
  ↓
打包成 0xff000000 | R | (G<<8) | (B<<16)
  ↓
writeBlockMasked(left, top, mask, pixelValues)  ← 用掩码只写命中的像素
```

设源 alpha 为 \(\alpha\)，旧颜色为 \(D\)，新（源）颜色为 \(S\)，预乘 alpha 的 over 运算（src over dst）为：

\[
C_{\text{out}} = S + (1-\alpha)\,D
\]

#### 4.4.3 源码精读

**浮点 → 8bpp 转换**（[TriangleFiller.cpp:191-196](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L191-L196)）：

```cpp
vecu16_t rS = __builtin_convertvector(clamp(color[kColorR], 0.0, 1.0) * 255.0f, vecu16_t);
vecu16_t gS = __builtin_convertvector(clamp(color[kColorG], 0.0, 1.0) * 255.0f, vecu16_t);
vecu16_t bS = __builtin_convertvector(clamp(color[kColorB], 0.0, 1.0) * 255.0f, vecu16_t);
```

`clamp` 保证不溢出，`__builtin_convertvector` 是「向量浮点 → 整数」转换（Nyuzi 的 `ftoi`，见 u2-l2），一次转 16 个像素。

**alpha 混合分支判定**（[TriangleFiller.cpp:199-200](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L199-L200)）有一个重要快速路径：只有当「开启混合 **且** 至少一个命中像素的 alpha 小于 1」时才走混合；全不透明时直接打包（[TriangleFiller.cpp:217-218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L217-L218)），省掉一次帧缓冲读回。

**premultiplied alpha 融合**（[TriangleFiller.cpp:202-216](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L202-L216)）。代码用 `(src<<8 + dest*(255-α)) >> 8` 做定点近似（`<<8 / >>8` 等于除以 256，比真除法便宜）：

```cpp
vecu16_t destColors = vecu16_t(fTarget->getColorBuffer()->readBlock(left, top));
vecu16_t rD = destColors & 0xff;
...
vecu16_t newR = saturate(((rS << 8) + (rD * oneMinusAS)) >> 8, 255);
...
pixelValues = 0xff000000 | newR | (newG << 8) | (newB << 16);
```

注意打包格式是 `0xAABBGGRR`（低位是 R），与 `RGBA8888` 的字节序一致。`saturate(..., 255)` 防止定点运算溢出 255。

**FLOAT 颜色空间分支**（[TriangleFiller.cpp:223-226](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L223-L226)）：直接把通道 0 当 float 存。这是**深度缓冲**用的格式——深度缓冲是一个 `Surface::FLOAT` 的 Surface（`sceneview.cpp:201` 的 `depthBuffer`），Z 值经此路径写入。

**掩码写回**（[TriangleFiller.cpp:232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L232)）：

```cpp
destSurface->writeBlockMasked(left, top, mask, vecu16_t(pixelValues));
```

`mask` 是 4.1 节一路传下来的、经过「覆盖测试 + 早期 Z 测试」双重筛选后的 16 位掩码（[TriangleFiller.cpp:155](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L155) 的 `mask &= passDepthTest`）。底层用 `__builtin_nyuzi_scatter_storei_masked` 一次性写 16 个像素，被掩码的通道不写。只有 `mask` 置位的通道才真正写进帧缓冲——这就是 u13-l2 光栅化产生的掩码最终的归宿。

> **链接知识**：因为 tile 之间互不重叠、任一像素只被一个线程写（u13-l1），所以这里写帧缓冲**不需要像素级的锁**。掩码写回 + tile 独占共同保证了无数据竞争。

#### 4.4.4 代码实践

**实践目标**：跟踪一个像素从「着色器输出颜色」到「落进帧缓冲」的全过程，并手算一次定点混合。

**操作步骤**：

1. 在 [TriangleFiller.cpp:180-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L180-L183) 处确认 `shadePixels` 把颜色写进 `color[4]`。
2. 跟到 [TriangleFiller.cpp:191-196](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L191-L196) 看浮点转 8bpp。
3. 手算定点混合：设 `aS = 128`（即 α≈0.5）、`rS = 200`、`rD = 100`，算 `newR = ((200<<8) + 100*(255-128)) >> 8 = (51200 + 12700) >> 8 = 63900 >> 8 = 249`。对照理论值 `200 + 100*(1-0.5) = 250`，定点版因 `/256` 略偏小。
4. 跟到 [TriangleFiller.cpp:232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L232) 看 `writeBlockMasked`。

**需要观察的现象**：整个写回链路里，只有 `writeBlockMasked` 这一步真正动了帧缓冲内存，且只写 `mask` 命中的通道；定点混合用移位代替除法。

**预期结果**：你能画出 `color[4] (float) → rS/gS/bS (8bpp) → pixelValues (RGBA8888) → 帧缓冲` 的数据流，并指出 alpha 混合在哪条分支才会发生、定点误差有多大。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `sceneview` 在大多数情况下走不到 alpha 混合分支？

**答案**：`TextureShader` 在「无纹理」分支显式置 `outColor[kColorA] = 1.0`（[TextureShader.h:95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L95)）；有纹理分支虽未显式写 alpha，但纹理 alpha 通常也为 1。于是 [TriangleFiller.cpp:200](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L200) 的 `mask_cmpf_lt(α, 1.0)` 为空，直接走全不透明快速路径，省掉一次帧缓冲读回。

**练习 2**：定点融合用 `(x<<8 + y) >> 8` 而不是 `x + y/256`，好处是什么？

**答案**：Nyuzi 没有硬件整数除法，`>>8` 是单周期移位，`(x<<8) + y` 也是单周期。用移位代替除法既快又能全程向量并行；代价是舍入偏差（截断而非四舍五入），对 8bpp 颜色可忽略。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**阅读型 + 跟踪型**」综合任务。

**任务**：以 `TextureShader`（带纹理）为对象，画一张完整的「像素处理时序图」，把一个 4×4 像素块从进入 `fillMasked` 到写回帧缓冲的全部步骤标注清楚，并在每一步旁边写出它用到的源码行号。然后做一个可预测结果的小修改。

**建议步骤**：

1. **入口与早期 Z**：从 [TriangleFiller.cpp:135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L135) 起，标出栅格 → 屏幕坐标转换、`zValues` 计算、深度测试与掩码收窄（[TriangleFiller.cpp:142-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L142-L160)）。
2. **透视插值**：标出 5 个 varying（u、v、法线 xyz）如何被插值（[TriangleFiller.cpp:162-178](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L162-L178)），并指出 `u/v` 来自 `TextureShader` 的 `outParams[4..5]`、法线来自 `outParams[6..8]`（[TextureShader.h:58-68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L58-L68)）。
3. **像素着色**：标出 `shadePixels` 入口（[TriangleFiller.cpp:182](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L182)），在其中用 `inParams[2..4]` 算 Lambert 光照、用 `inParams[0..1]` 调 `Texture::readPixels` 采样纹理、把纹理色乘光照（[TextureShader.h:76-89](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L76-L89)）。
4. **纹理采样**：在 `readPixels` 内标出 mip 选择（[Texture.cpp:86-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L86-L91)）、坐标转换（[Texture.cpp:100-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L100-L103)）、双线性融合（[Texture.cpp:105-138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L105-L138)）。
5. **混合写回**：标出浮点 → 8bpp、打包、`writeBlockMasked`（[TriangleFiller.cpp:191-232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L191-L232)）。
6. **小修改与预测**：把 [sceneview.cpp:218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/sceneview.cpp#L218) 的环境光系数 `uniforms.fAmbient` 从 `0.4f` 临时改成 `0.0f`，预测整体画面会明显变暗（背光面失去环境光兜底，[TextureShader.h:81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h#L81) 的 `illumination = clamp(dot,...) + ambient` 会塌到 0）。

**验收标准**：你的时序图应当让人一眼看出——

- 哪一步用到了「位置参数剥离」的约定（4.1）；
- 哪一步用到了「除一次 Z、所有参数复用」的优化（4.2）；
- 纹理采样在哪一步发生、为何只需几次 gather（4.3）；
- 最终写回为何不需要加锁（4.4 + u13-l1 的 tile 独占）。

如果想在真实程序里验证视觉效果，可在模拟器中重新构建并运行 `sceneview`（需 `resource.bin`，参考 u1-l4 的运行方式），具体画面「待本地验证」。

## 6. 本讲小结

- librender 用 **C++ 继承 `Shader`** 实现可编程管线，两个回调 `shadeVertices` / `shadePixels` 都按 **16 通道批处理**，与 SIMD 模型天然对齐。
- 顶点着色器输出的**前 4 个参数固定是位置 XYZW**，被几何阶段消费、不传给像素着色器；像素着色器的 `inParams[0..]` 是从第 5 个参数开始的 varying，从 0 重新编号。
- **透视正确插值**靠「参数/Z 与 1/Z 分别线性插值、再乘回 Z」实现；当三角形三顶点 Z 相等时退化为纯线性，省掉一次除法；梯度法把逐像素重心坐标变成整块 SIMD 求值。
- **纹理采样**在 `Texture::readPixels` 里：用相邻像素 u 差值的倒数 + `clz` 选 mip 层，再对 4 个角纹素做双线性融合，全程向量化的 gather。
- **混合写回**把浮点颜色 clamp×255 转 8bpp，必要时做预乘 alpha 的定点融合（`<<8 / >>8` 避免除法），最后用覆盖掩码 `writeBlockMasked` 写进帧缓冲；深度缓冲走 `FLOAT` 路径；tile 独占使其无需像素锁。
- 整条像素处理链路把 u13-l2 光栅化产生的 16 位掩码，最终消费成帧缓冲里一组写回的像素值。

## 7. 下一步学习建议

本讲是「图形渲染管线」单元（u13）的最后一篇，到此你已经从 tile 架构（u13-l1）、光栅化（u13-l2）一路读到着色与纹理（本讲），完整看过 librender 的像素阶段。建议：

- **横向对比着色器实现**：把 `TextureShader`（带光照 + 纹理）和 `DepthShader`（仅深度灰度）并排读，体会同一个 `Shader` 契约如何承载完全不同的渲染需求；也可读 `tests/render/` 下的更简单着色器例子。
- **回到数据流上游**：如果想看纹理数据本身是怎么进来的，回顾 `Surface`（u9-l3）与 `sceneview.cpp` 的资源文件加载（[sceneview.cpp:59-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/sceneview.cpp#L59-L95)）。
- **进入下一个单元 u14（FPGA SoC 与外设）**：本讲写进帧缓冲的颜色最终会经 VGA 控制器变成视频信号——u14-l2 会讲 `vga_controller` 如何从帧缓冲产生时序，与本讲的 `writeBlockMasked` 形成闭环。
- **性能视角**：用 u11-l2 的性能计数器统计一个 `sceneview` 帧的缓存缺失与 gather/scatter 代价，验证「tile 工作集常驻 L2、纹理采样是主要带宽热点」这些设计假设。
