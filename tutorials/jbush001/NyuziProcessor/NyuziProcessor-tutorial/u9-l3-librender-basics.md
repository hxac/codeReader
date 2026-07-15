# librender 渲染库基础

## 1. 本讲目标

本讲是「软件栈与裸机运行时」单元的第三讲。前面两讲我们分别搞懂了「程序如何从地址 0 启动并 `printf` 到终端」（u9-l1）和「libos 如何用 `parallel_execute` 唤醒多个硬件线程并行干活」（u9-l2）。本讲把这两条线索汇合到 Nyuzi 真正的「重负载应用」上——**3D 图形渲染库 librender**。

学完本讲你应该能够：

- 理解 **Surface** 这个「二维位图 + 向量偏移表」的抽象，以及它为什么天然贴合 16 通道 SIMD。
- 掌握 **Texture / mipmap** 的数据组织方式，以及纹理采样如何挑选 mip 层级并做双线性滤波。
- 看懂 **RenderContext** 的「先提交命令、再 `finish()` 触发渲染」编程模型，并能解释 `finish()` 如何复用 libos 的 `parallel_execute` 把几何阶段与像素阶段分派给所有工作线程。

本讲只讲三大基础构件的「是什么、怎么用、内部如何并行」，**不**深入光栅化算法、着色器插值与 tile 排序细节——它们留给专家层的图形渲染管线单元（u13）。

## 2. 前置知识

本讲默认你已经建立以下认知（来自前置讲义，这里只做一句话回顾）：

- **向量 SIMD 与 16 通道**（u2-l1 / u5-l1）：`vector_t` 由 16 个 32 位标量拼成，一条向量指令可并行处理 16 个数据；标量操作数会广播到所有通道。
- **块访存与 scatter/gather**（u2-l3）：`MEM_BLOCK` 一次搬动整行 64 字节；scatter/gather 则给 16 个通道各一个地址，靠 16 个 subcycle 串行完成。本讲里你会看到这两条指令被频繁用来搬像素。
- **`parallel_execute` 共享任务池**（u9-l2）：libos 提供一个 `parallel_execute(func, context, num_elements)`，把 `num_elements` 个任务放进全局计数器，所有硬件线程用 CAS（底层 LL/SC）自取 index、各跑各的，主线程自旋到所有任务完成后才返回——这就是 librender 并行渲染的引擎。
- **缓存行 = 64 字节 = 向量宽度**（u1-l1 / u3-l3）：这个「巧合」是 librender 把 4×4 像素块对齐到一条向量指令的物理基础。

一个贯穿全讲的关键直觉：**librender 是一个用软件（运行在 Nyuzi 通用核上）实现的 GPU 渲染器**。它没有专用光栅化硬件，而是靠「16 通道 SIMD + 多线程 + tile 分块」这三件套，把图形流水线映射到 Nyuzi 的并行能力上。理解了这一点，三大构件的设计动机就都顺理成章了。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `software/libs/librender/` 下：

| 文件 | 作用 |
|------|------|
| [Surface.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h) / [Surface.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp) | 二维位图抽象，封装像素读写、tile 清空/刷回、预计算的向量偏移表 |
| [Texture.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h) / [Texture.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp) | 纹理抽象，管理多级 mip surface 并做采样（选层 + 双线性/最近邻） |
| [RenderContext.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.h) / [RenderContext.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp) | 对外的命令接口：`bind*` 设置状态、`drawElements` 入队、`finish()` 触发两阶段并行渲染 |
| [README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/README.md) | 架构说明：tile-based / sort-middle、几何阶段与像素阶段的分工 |
| [schedule.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/schedule.h) / [schedule.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c) | libos 的并行原语 `parallel_execute`，被 `RenderContext::finish` 直接调用 |

另外会用到一个真实应用作为实践参照：[software/apps/shadow_map/main.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp)（演示「渲染到纹理」）。

## 4. 核心概念与源码讲解

### 4.1 Surface：贴合 SIMD 的二维位图

#### 4.1.1 概念说明

渲染离不开「一块二维像素内存」——它既是最终显示的帧缓冲（framebuffer），也可能是纹理、深度缓冲（Z-buffer）或离屏渲染目标。librender 用 **Surface** 统一抽象这一切。

Surface 的核心设计目标是：**让一个 4×4 的像素块恰好对齐到 16 个向量通道**。光栅化最终总是以 4×4 像素为单位处理（u13 会详述），如果每个像素的内存地址能预先算好排成一个 16 元向量，那么「读 16 个像素」就是一条 gather 指令、「写 16 个像素」就是一条 scatter 指令——不需要循环。Surface 在构造时就预计算好这套「偏移向量」，把地址算术从热路径上移走。

Surface 支持三种颜色空间（[Surface.h:45-50](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L45-L50)）：

- `RGBA8888`：每像素 4 字节，显示帧缓冲的常用格式；
- `FLOAT`：每像素 4 字节（一个 `float`），用于深度缓冲等需要精度的场合；
- `GRAY8`：每像素 1 字节，灰度图。

> 注意：`RGBA8888` 与 `FLOAT` 都是 4 字节/像素，这正是后文很多「按 4 字节步进」优化的前提。

#### 4.1.2 核心流程

一个 Surface 的生命周期：

1. **构造**：给定宽、高、颜色空间，可选地传入一块已有内存（`base`）。若不传，则用 `memalign` 按 `kCacheLineSize`(64) 对齐自己分配一块，并标记「自己拥有、析构时释放」。
2. **预计算偏移向量**：算出三套 16 元向量——`f4x4AtOrigin`（4×4 块在原点时每通道的字节地址）、`fXStep`/`fYStep`（4×4 块内每像素相对左上角的屏幕坐标偏移）。
3. **渲染期读写**：通过 `readBlock`/`writeBlockMasked` 整块读写 4×4 像素，或通过 `readPixels` 做带掩码的任意坐标 gather 采样。
4. **tile 级管理**：`clearTile` 用块存储快速清一个 64×64 tile；`flushTile` 用 `dflush` 指令把 tile 从 L2 刷回系统内存（显示用）。

关键常量定义在 [Surface.h:29-31](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L29-L31)：`kCacheLineSize=64`、`kTileSize=64`、`kVectorSize=64`——三者相等并非偶然，而是「缓存行＝向量宽度＝tile 边长」这条贯穿全项目的主线。

#### 4.1.3 源码精读

**构造函数**决定每像素字节数、步进（stride），并按是否传入 `base` 决定内存归属，最后调用 `initializeOffsetVectors`：

```cpp
// Surface.cpp:25-59（节选）
fStride = width * fBytesPerPixel;
if (base == nullptr) {
    fBaseAddress = reinterpret_cast<int>(memalign(kCacheLineSize,
         static_cast<size_t>(width * height * fBytesPerPixel)));
    fOwnedPointer = true;
} else {
    fBaseAddress = reinterpret_cast<int>(base);
    fOwnedPointer = false;
}
initializeOffsetVectors();
```

注意 `fBaseAddress` 被存成 `int` 而非指针——因为后面要把它塞进 16 元**整数向量**里做地址运算（Nyuzi 的向量元素是 32 位整数）。

**偏移向量预计算**是 Surface 的灵魂（[Surface.cpp:67-109](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L67-L109)）。看 `f4x4AtOrigin` 如何拼出 4×4 块的 16 个地址：

```cpp
// 4×4 块内：列号（0..3）乘 4 字节，每个列号重复 4 次（同一行的 4 个像素）
f4x4AtOrigin = { 0,4,8,12,  0,4,8,12,  0,4,8,12,  0,4,8,12 };
// 行号（0..3）乘 4，每个行号重复 4 次（同一行的 4 个像素）
veci16_t widthOffset = { 0,0,0,0,  4,4,4,4,  8,8,8,8,  12,12,12,12 };
// 组合：字节地址 = 基址 + (行号*宽 + 列号) * 4
f4x4AtOrigin += widthOffset * fWidth + fBaseAddress;
```

回忆 4×4 像素在 16 通道中的排布（来自 [Surface.h:62-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L62-L67) 的注释）：

```
 0  1  2  3
 4  5  6  7
 8  9 10 11
12 13 14 15
```

即 lane 0..3 是第一行的 4 个像素，lane 4..7 是第二行，依此类推。于是 `writeBlockMasked` 只需把 `f4x4AtOrigin` 加上块的左上角偏移，就得到 16 个像素地址，一条 scatter 存完（[Surface.h:68-80](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L68-L80)）：

```cpp
void writeBlockMasked(int left, int top, vmask_t mask, vecu16_t values) {
    veci16_t ptrs = f4x4AtOrigin + left * 4 + top * fStride;
    __builtin_nyuzi_scatter_storei_masked(ptrs, values, mask);
}
```

`left*4` 是块在行内的字节偏移（4 字节/像素），`top*fStride` 是块的行偏移。这条 scatter 正是 u2-l3 讲过的「scatter 带 mask 存储」——掩码控制哪些像素真正写入，未覆盖的三角形像素被跳过。

**`clearTile`** 展示了「快路径 + 慢路径」的经典取舍（[Surface.h:83-104](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L83-L104)）：当 tile 完整落在 surface 内且为 32bpp 时，用「每行 4 条向量存储」一把刷完 64×64；否则退回逐像素的 `slowClearTile`。

**`flushTile`** 把渲染好的 tile 从 L2 刷回内存供显示（[Surface.cpp:154-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L154-L170)），核心是内联汇编 `dflush`：

```cpp
asm("dflush %0" : : "s" (ptr));
```

这对应缓存控制指令——把脏缓存行写回系统内存。模拟器/FPGA 的显示窗口从这块内存读像素，所以渲染完一个 tile 必须刷回（详见 u6-l4 的 IO/内存出口与 u8-l2 的帧缓冲窗口）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「4×4 像素块 ↔ 16 通道」的映射关系。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [Surface.cpp:67-109](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L67-L109) 的 `initializeOffsetVectors`。
2. 假设有一个 `Surface(640, 480, RGBA8888)`（即 `fWidth=640`，`fBytesPerPixel=4`，`fStride=2560`）。
3. 手算 `f4x4AtOrigin` 的 lane 5（即第二行第二列像素）的值：按公式 `fBaseAddress + (row*640 + col)*4`，row=1、col=1，得 `fBaseAddress + (640 + 1)*4 = fBaseAddress + 2564`。
4. 再算 `writeBlockMasked(left=100, top=50, ...)` 时 lane 5 的最终地址：`f4x4AtOrigin[lane5] + 100*4 + 50*2560 = fBaseAddress + 2564 + 400 + 128000 = fBaseAddress + 130964`。
5. 对照像素坐标：`left + col = 100 + 1 = 101`，`top + row = 50 + 1 = 51`，手算字节地址 `fBaseAddress + (51*640 + 101)*4 = fBaseAddress + (32640 + 101)*4 = fBaseAddress + 130964`，两者一致。

**需要观察的现象**：手算的两条路径得出同一地址，说明 `f4x4AtOrigin` 把「二维像素坐标」线性化进了向量通道。

**预期结果**：地址完全相等。这验证了 Surface 用预计算向量把 4×4 块的地址算术「免费」化，热路径上没有乘法、只有向量加。

> 待本地验证：若你想在硬件/模拟器上跑，可在 `writeBlockMasked` 里临时加一行打印（仅调试用，勿提交）观察 `ptrs[lane]` 是否与手算一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Surface` 的 `operator new` 要用 `memalign(sizeof(vecu16_t), size)` 做向量宽度对齐？

**参考答案**：Surface 含 `veci16_t f4x4AtOrigin` 等 16 元向量成员（[Surface.h:187-192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L187-L192)）。Nyuzi 的向量访存要求地址对齐到向量宽度（64 字节），否则触发对齐异常（u7-l3）。对象本身的起始地址也必须对齐，才能保证这些向量字段落在合法地址上。

**练习 2**：`clearTile` 的快路径要求 `fColorSpace == RGBA8888 || fColorSpace == FLOAT`，但 `GRAY8` 也是合法颜色空间。为什么 `GRAY8` 走不了快路径？

**参考答案**：快路径用 `vecu16_t`（16×4 字节 = 64 字节）按整行刷写，假设每像素 4 字节（[Surface.h:88-100](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L88-L100)）。`GRAY8` 每像素只有 1 字节，字节布局不匹配，只能退到 `slowClearTile` 的 `memset` 路径（[Surface.cpp:135-146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L135-L146)）。

### 4.2 Texture / mipmap：多级纹理与采样

#### 4.2.1 概念说明

**纹理（texture）** 是贴在三维模型表面的二维图像。渲染时，像素着色器根据每个像素的纹理坐标 `(u, v)`（都在 0.0~1.0 范围）从纹理里取出颜色，这个过程叫**纹理采样**。

直接用原始大图采样有个问题：当三角形在屏幕上很小（远处）、却在纹理上覆盖一大片时，每个屏幕像素对应很多纹素（texel），逐个采样既慢又会产生走样（锯齿/闪烁）。**mipmap** 的解法是：预先把纹理逐级缩小一半，存成一组「金字塔」层级——

- level 0：原始分辨率（例如 256×256）
- level 1：128×128
- level 2：64×64
- ……直到 1×1

采样时根据「屏幕上像素有多密集」自动挑选合适层级：远处用低分辨率小图（省带宽、抗走样），近处用高分辨率大图（保细节）。这就是 **mip 层级选择**。

librender 的 **Texture** 就是「一组按 mip 层级组织的 Surface」加上一个采样器。它本身**不拥有**这些 surface（[Texture.h:36-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.h#L36-L39) 注释明言不释放），只是持有指针——这让「渲染到一张 surface，再把它当纹理采样」（render-to-texture）变得自然，shadow_map 应用正是这么做的。

#### 4.2.2 核心流程

纹理采样的流程（[Texture.cpp:78-144](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L78-L144) 的 `readPixels`）：

1. **估密度 → 选 mip 层级**：用相邻屏幕像素的纹理坐标差 `|u[1] - u[0]|` 估计「每像素跨多少纹素」，取倒数再取 log₂ 得到层级，并夹到 `[0, fMaxMipLevel]`。
2. **坐标换算**：把 `(u, v)` 从纹理空间 `[0,1]` 换算成该层级的纹素坐标，处理环绕（wrap）。
3. **取纹素**：
   - 最近邻：直接取一个纹素；
   - 双线性（bilinear）：取覆盖该采样点的 4 个相邻纹素，按小数部分加权混合。
4. **输出**：4 个颜色通道（RGBA），每个都是 16 元向量（一次为 16 个像素采样）。

mip 层级选择的「直觉公式」可以写成：

\[
\text{mipLevel} \approx \log_2\!\left(\frac{1}{|u_1 - u_0|}\right)
\]

即纹理坐标每像素的跳变越大（纹理被压缩得越厉害），层级越高（用越小的图）。代码用 `__builtin_clz`（前导零计数）充当「廉价的整数 log₂」，注释坦承这只看了一个方向、是个 hack（[Texture.cpp:81-87](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L81-L87)）。

> 回忆 u2-l2：`__builtin_clz` 是单周期查表指令。这里再次看到 Nyuzi 把「log₂」这种原本昂贵的运算用硬件查表摊平。

#### 4.2.3 源码精读

**mip surface 注册**（[Texture.cpp:54-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L54-L76)）有三条规则值得注意：

```cpp
void Texture::setMipSurface(int mipLevel, const Surface *surface) {
    assert(mipLevel < kMaxMipLevels);          // 最多 8 层（Texture.h:26）
    fMipSurfaces[mipLevel] = surface;
    if (mipLevel > fMaxMipLevel) fMaxMipLevel = mipLevel;
    if (mipLevel == 0) {
        fBaseMipBits = __builtin_clz(surface->getWidth()) + 1;  // 记下基准层级位数
        for (int i = 1; i < fMaxMipLevel; i++) fMipSurfaces[i] = 0; // 清掉旧的高层
        fMaxMipLevel = 0;
    } else {
        assert(surface->getWidth() == fMipSurfaces[0]->getWidth() >> mipLevel); // 必须逐级减半
    }
}
```

- **level 0 必须最先设置**：因为它决定了 `fBaseMipBits`（基准层级计算的偏移量），且会清空已注册的高层。
- **高层宽度必须 = 基层宽度 >> mipLevel**：mipmap 的几何约束，由断言守护。

**mip 层级选择**（[Texture.cpp:86-91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L86-L91)）：

```cpp
int mipLevel = __builtin_clz(static_cast<unsigned int>(1.0f /
                             __builtin_fabsf(u[1] - u[0]))) - fBaseMipBits;
if (mipLevel > fMaxMipLevel) mipLevel = fMaxMipLevel;
else if (mipLevel < 0) mipLevel = 0;
```

`u[1] - u[0]` 是向量里两个**相邻屏幕像素**的纹理坐标之差（像素着色按 4×4 块、即 16 通道批量处理，相邻通道即相邻像素）。`1.0 / |差|` 近似「每像素覆盖的纹素数」，`clz` 把它折成 log₂，减去 `fBaseMipBits` 归一化到该纹理的层级范围，最后夹到合法区间。

**坐标换算与环绕**（[Texture.cpp:97-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L97-L103)）：

```cpp
vecf16_t uRaster = wrapfv(fracfv(u)) * (mipWidth - 1);
vecf16_t vRaster = (1.0 - wrapfv(fracfv(v))) * (mipHeight - 1);
```

`fracfv` 取小数部分、`wrapfv` 处理负数环绕（[Texture.cpp:32-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L32-L44)），实现 `GL_REPEAT` 式的纹理平铺。注意 `v` 方向取了 `1.0 - ...`：纹理空间 v=1.0 对应图像**顶部**（与屏幕坐标的 y 向下相反）。

**双线性滤波**（[Texture.cpp:105-138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L105-L138)）：取采样点周围的 4 个纹素（左上、右上、左下、右下，坐标用 `wrapiv` 在边缘环绕），按小数部分算出 4 个权重，加权混合：

```cpp
vecf16_t tlWeight = (1.0 - wu) * (1.0 - wv);
vecf16_t trWeight = wu * (1.0 - wv);
vecf16_t blWeight = (1.0 - wu) * wv;
vecf16_t brWeight = wu * wv;
for (int channel = 0; channel < 4; channel++)
    outColor[channel] = (tlColor[channel]*tlWeight) + (blColor[channel]*blWeight)
                      + (trColor[channel]*trWeight) + (brColor[channel]*brWeight);
```

4 个权重之和恒为 1，保证能量守恒。注意每个 `surface->readPixels` 内部是带掩码的 gather（[Surface.h:109-140](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L109-L140)），16 个通道各自取自己的纹素——这正是 u2-l3 讲过的 gather 采样的真实用武之地。

#### 4.2.4 代码实践

**实践目标**：理解 render-to-texture——同一块 surface 既能当渲染目标、又能当纹理源。

**操作步骤**：

1. 打开 [shadow_map/main.cpp:73-81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L73-L81)，阅读这段「光影图」的构造：

   ```cpp
   Surface *lightMapSurface = new Surface(kLightmapSize, kLightmapSize, Surface::FLOAT);
   ...
   RenderTarget *lightMapTarget = new RenderTarget();
   lightMapTarget->setColorBuffer(lightMapSurface);
   ...
   Texture *lightMapTexture = new Texture();
   lightMapTexture->enableBilinearFiltering(true);
   lightMapTexture->setMipSurface(0, lightMapSurface);   // 同一个 surface 当纹理
   ```

2. 跟到 [main.cpp:120-145](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L120-L145)：第一遍 `context->finish()` 把场景从光源视角渲染进 `lightMapSurface`；随后 `context->bindTexture(0, lightMapTexture)` 把这块 surface 当纹理，从相机视角再渲染一遍，着色器里采样它来判断阴影。

**需要观察的现象**：同一个 `lightMapSurface` 指针先后被用作 `RenderTarget` 的 color buffer 和 `Texture` 的 mip level 0 surface。

**预期结果**：你能用自己的话说明——因为 Texture 只持有 Surface 指针、不复制也不释放，所以「先把像素写进 surface，再让另一个着色器采样同一块内存」天然成立，无需拷贝。这正是 shadow_map 单文件实现「动态阴影」的关键。

> 待本地验证：若在模拟器上运行 `run_emulator` 启动 shadow_map，会打开帧缓冲窗口显示一个旋转的带阴影圆环（画面输出走 u8-l2 讲过的 fbwindow）。本机若无 SDL2 图形环境则看不到窗口，但程序逻辑仍可阅读。

#### 4.2.5 小练习与答案

**练习 1**：mipmap 的层级 0 必须最先调用 `setMipSurface(0, ...)` 设置。如果先设置了 level 2、再设置 level 0 会发生什么？

**参考答案**：设置 level 0 时会执行 `for (int i = 1; i < fMaxMipLevel; i++) fMipSurfaces[i] = 0;` 并把 `fMaxMipLevel = 0`（[Texture.cpp:62-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L62-L71)），即先前进的 level 2 会被清空。所以必须按从低到高的顺序注册。

**练习 2**：`readPixels` 选 mip 层级时只看了 `u` 方向的差 `u[1]-u[0]`，没看 `v` 方向。这会带来什么潜在问题？

**参考答案**：当纹理在 `v` 方向被剧烈压缩、`u` 方向却没变（例如一条几乎平行于 u 轴的细长斜三角形）时，代码会低估所需的 mip 层级，采样会偏模糊不足、可能产生走样。注释 `XXX this is a hack`（[Texture.cpp:81-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Texture.cpp#L81-L85)）已点明，理想做法应取两方向中较大的密度。

### 4.3 RenderContext：命令提交与两阶段并行渲染

#### 4.3.1 概念说明

**RenderContext** 是 librender 对外的总入口，也是应用代码打交道最多的类。它的编程模型借鉴了经典图形 API（OpenGL 的「状态机 + 命令缓冲」）：

1. **设置状态**：用一串 `bind*` 调用告诉渲染器「接下来用什么着色器、顶点属性、纹理、uniform、渲染目标」。
2. **提交绘制命令**：`drawElements(indices)` 把一次绘制（用当前状态画一组三角形）追加进**命令队列**。
3. **触发渲染**：`finish()` 真正执行所有命令——在此之前**什么也不画**。

这种「先录制、后回放」的设计有两个好处：一是状态变更的语义清晰（`bind*` 只影响其后的 `drawElements`）；二是 `finish()` 拿到全部绘制命令后，能做全局优化——**librender 选择的是 tile-based（sort-middle）架构**（见 [README.md:8-16](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/README.md#L8-L16)）：把画面切成 64×64 的 tile，每个线程独占一个 tile 渲染到底，既免去像素级锁、又让活动 tile 全部驻留在 L2 缓存里，极大降低外部内存带宽。

`finish()` 是把命令队列「翻译」成实际渲染的枢纽，也是本讲的重点——它**直接复用** u9-l2 讲的 `parallel_execute` 来驱动所有硬件线程。

#### 4.3.2 核心流程

`finish()` 把渲染分成两大阶段（[README.md:18-62](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/README.md#L18-L62)）：

```
                       ┌──────────── finish() ────────────┐
   命令队列 fDrawQueue  │                                  │  帧缓冲
   (RenderState 列表) ──▶│  ① 几何阶段 Geometry Phase       │──▶ 显示
                       │     a. shadeVertices (顶点着色)    │
                       │     b. setUpTriangle (三角形装配)  │
                       │  ② 像素阶段 Pixel Phase            │
                       │     fillTile / wireframeTile      │
                       │        (逐 tile 光栅化+着色)       │
                       └──────────────────────────────────┘
                每一步都用 parallel_execute 分派给所有硬件线程
```

**① 几何阶段**（对每条 draw 命令依次执行两步）：

- **顶点着色 `shadeVertices`**：把顶点属性喂给应用提供的顶点着色器，算出顶点参数（含裁剪空间坐标 x/y/z/w）。顶点按 16 个一批分给线程（每批对应一个向量 lane），最多 4 线程×16 = 64 个顶点同时在途。
- **三角形装配 `setUpTriangle`**：按 index buffer 取出三角形，做近裁面裁剪（可能一拆为多）、背面剔除、屏幕空间→光栅坐标换算，最后用**包围盒测试**把三角形塞进它可能覆盖的各个 tile 的队列里。

**② 像素阶段**（几何阶段全部完成后才开始）：

- 每个 tile 由一个任务表示，`fillTile` 把 tile 队列里的三角形**按提交序排序**（因为几何阶段并行执行、三角形入队顺序被打乱了），逐个做精确覆盖测试、参数插值、光栅化、深度测试、像素着色、混合写回，最后 `flushTile` 刷回内存。

关键设计：**几何阶段按「顶点/三角形」分片并行，像素阶段按「tile」分片并行**。两类分片粒度不同，但都用同一个 `parallel_execute` 引擎——线程从全局计数器抢 index，抢到哪个就处理哪个，天然负载均衡。

#### 4.3.3 源码精读

**构造与命令队列**（[RenderContext.cpp:28-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L28-L33)）：构造时建一个 `RegionAllocator`（临时内存竞技场）并让 `fDrawQueue` 复用它。

```cpp
RenderContext::RenderContext(size_t workingMemSize)
    : fClearColorBuffer(false), fAllocator(workingMemSize) {
    fDrawQueue.setAllocator(&fAllocator);
}
```

`workingMemSize` 默认 `0x400000`（4 MiB，见 [RenderContext.h:37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.h#L37)）。README 的「Limits」一节警告：复杂场景可能撑爆这块竞技场触发断言（[README.md:63-70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/README.md#L63-L70)），构造时可加大。

**状态绑定与入队**：`bindTarget` 算出 tile 的行列数（[RenderContext.cpp:57-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L57-L64)）；`drawElements` 把当前 `fCurrentState`（一份快照）追加进 `fDrawQueue`（[RenderContext.cpp:72-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L72-L76)）：

```cpp
void RenderContext::drawElements(const RenderBuffer *indices) {
    fCurrentState.fIndexBuffer = indices;
    fDrawQueue.append(fCurrentState);   // 快照入队，之后改状态不影响这条命令
}
```

**`finish()` 的两阶段**是本讲核心（[RenderContext.cpp:98-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L98-L143)）。先建 tile 队列数组，再遍历命令队列：

```cpp
void RenderContext::finish() {
    unsigned int kMaxTiles = fTileColumns * fTileRows;
    fTiles = new (fAllocator) TriangleArray[kMaxTiles];      // 每个 tile 一个三角形队列
    ...
    fBaseSequenceNumber = 0;
    for (fRenderCommandIterator = fDrawQueue.begin();
         fRenderCommandIterator != fDrawQueue.end(); ++fRenderCommandIterator) {
        RenderState &state = *fRenderCommandIterator;
        int numVertices = state.fVertexAttrBuffer->getNumElements();
        int numTriangles = state.fIndexBuffer->getNumElements() / 3;
        ...
        parallel_execute(_shadeVertices, this, (numVertices + 15) / 16);  // ①a 顶点着色
        parallel_execute(_setUpTriangle, this, numTriangles);             // ①b 三角形装配
        fBaseSequenceNumber += numTriangles;
    }
    // ② 像素阶段
    if (fWireframeMode)
        parallel_execute(_wireframeTile, this, fTileColumns * fTileRows);
    else
        parallel_execute(_fillTile, this, fTileColumns * fTileRows);
    ...
}
```

读这段代码要注意四点：

1. **三条 `parallel_execute` 调用**分别对应「顶点着色」「三角形装配」「逐 tile 填充」。每条的第三个参数是**任务总数**——顶点按 `(numVertices+15)/16` 向上取整（每 16 个顶点一批，对应 16 个 lane），三角形按 `numTriangles`，tile 按 `fTileColumns*fTileRows`。
2. **`_xxx` 是 C 风格跳板函数**（[RenderContext.cpp:78-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L78-L96)），把 `parallel_execute` 要求的 `void(*)(void*, int)` 签名桥接到 C++ 成员函数：

   ```cpp
   void RenderContext::_fillTile(void *_castToContext, int index) {
       static_cast<RenderContext*>(_castToContext)->fillTile(index);
   }
   ```

   回忆 [schedule.h:20-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/schedule.h#L20-L28)：`parallel_func_t` 是 `void(*)(void*, int)`，libos 是 C 接口、不懂 C++ 成员函数，故需跳板。
3. **几何阶段的两步是串行的**（顶点着色全部完成后才开始三角形装配），因为装配要读着色器产出的顶点参数；**像素阶段在所有 draw 命令的几何阶段都完成后才开始**，因为 tile 队列要等所有三角形都装配入队完毕。串行靠的是 `parallel_execute` 的「阻塞到所有任务完成才返回」语义（u9-l2）。
4. **`fBaseSequenceNumber`** 给每条 draw 命令的三角形一个全局递增的序号，像素阶段用它把乱序入队的三角形**排回提交序**（`tile.sort()`，[RenderContext.cpp:440](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L440)），保证半透明等依赖顺序的场景正确。

**三角形装配与 binning**（[RenderContext.cpp:318-396](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L318-L396)）：`enqueueTriangle` 做透视除法、坐标换算、背面剔除、包围盒计算，最后用包围盒把三角形投到所有相交 tile 的队列：

```cpp
int minTileX = max(bbLeft / kTileSize, 0);
int maxTileX = min(bbRight / kTileSize, fTileColumns - 1);
...
for (int tiley = minTileY; tiley <= maxTileY; tiley++)
    for (int tilex = minTileX; tilex <= maxTileX; tilex++)
        fTiles[tiley * fTileColumns + tilex].append(tri);   // 同一三角形入多个 tile 队列
```

这就是 sort-middle 架构的「sort」——按 tile 重新分桶。

**逐 tile 填充**（[RenderContext.cpp:422-494](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L422-L494)）：`fillTile` 由某个线程领走一个 tile index，清色/清深度、排序三角形、逐三角形精确覆盖测试与光栅化（交给 `TriangleFiller`，u13 详述），最后 `flushTile`：

```cpp
void RenderContext::fillTile(int index) {
    const int x = index % fTileColumns;
    const int y = index / fTileColumns;
    ...
    if (fClearColorBuffer) colorBuffer->clearTile(tileX, tileY, fClearColor);
    if (fRenderTarget->getDepthBuffer())
        fRenderTarget->getDepthBuffer()->clearTile(tileX, tileY, 0xff800000);  // -inf
    tile.sort();                       // 恢复提交序
    TriangleFiller filler(fRenderTarget);
    for (const Triangle &tri : tile) { ... fillTriangle(...); }
    colorBuffer->flushTile(tileX, tileY);   // 刷回内存供显示
}
```

注意 `0xff800000` 是浮点 `-inf` 的位模式——深度缓冲初始化为「无穷远」，从而首个覆盖某像素的片段总能通过深度测试并被写入（具体的深度比较方向在 `TriangleFiller` 中实现，u13 详述）。

#### 4.3.4 代码实践

**实践目标**：精读 `RenderContext::finish`，说清它如何借 `parallel_execute` 启动所有工作线程、并区分几何阶段与像素阶段。（本任务对应规格中的实践要求。）

**操作步骤**（源码阅读型）：

1. 先复习 `parallel_execute` 的实现（[schedule.c:46-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L46-L58)）：它把任务总数写进 `max_index`，主线程在 `dispatch_job` 里一边抢 index 自己干、一边等所有工作线程把 `active_jobs` 归零（u9-l2 详述）。工作线程则由应用启动时调用的 `start_all_threads()`（写控制寄存器 `CR_RESUME_THREAD`，u9-l2）唤醒进入 `worker_thread` 死循环。

2. 打开 [RenderContext.cpp:98-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L98-L143)，在三条 `parallel_execute` 调用处各画一条线，分别标注「①a 顶点着色」「①b 三角形装配」「② 像素阶段」。

3. 追踪一次调用的线程视角：假设 4 线程、`numTriangles=100`。`parallel_execute(_setUpTriangle, this, 100)` 后，4 个线程各自在 [schedule.c:29-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L29-L44) 的 `dispatch_job` 里 CAS 抢 `current_index`（0..99），抢到哪个就调 `_setUpTriangle(this, index)` → `setUpTriangle(index)` 装配第 `index` 个三角形。谁先抢完谁就继续抢下一个，直到 100 个全被领走、且 `active_jobs` 归零，`parallel_execute` 才返回，几何阶段这才结束。

**需要观察的现象**：

- 几何阶段的两步 `parallel_execute` 是**先后两次**调用——顶点着色整批完成（函数返回）后，三角形装配才开始；
- 像素阶段的 `parallel_execute` 在外层 `for`（遍历所有 draw 命令）**全部结束后**才被调用一次，任务数是 `tileColumns*tileRows`；
- 三类任务的「分片粒度」不同：顶点按 16 个一批、三角形按个、tile 按块，但**调度引擎完全相同**。

**预期结果**：你能写出类似下面的一句话总结——

> `finish()` 自己**不直接 for 循环处理**顶点/三角形/tile，而是把每类工作切成 N 个等价任务交给 `parallel_execute`；`parallel_execute` 把任务放进共享计数器，所有被 `start_all_threads` 唤醒的硬件线程（含主线程）并行抢任务执行，函数阻塞到全部完成后才返回，从而天然实现了「几何阶段（顶点着色+三角形装配）串行两步、像素阶段（逐 tile）随后并行」的两阶段流水，且线程间无需像素级锁（每个 tile 同一时刻只被一个线程持有）。

> 待本地验证：若想观察真实并行行为，可在 `dispatch_job`（[schedule.c:41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L41)）处临时加一行 `printf`（仅调试，勿提交），用模拟器运行任一渲染应用，会看到不同线程 ID 交替领取 index——但 printf 会严重拖慢渲染并可能改变时序，仅供参考。

#### 4.3.5 小练习与答案

**练习 1**：`finish()` 里几何阶段的两步为什么必须**先后串行**（顶点着色整批完成后再开始装配），而不能合并成一次 `parallel_execute`？

**参考答案**：三角形装配（`setUpTriangle`）要读顶点着色（`shadeVertices`）写出的顶点参数 `state.fVertexParams`（[RenderContext.cpp:116-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L116-L121) 与 [RenderContext.cpp:266-268](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L266-L268)）。存在生产者-消费者依赖，必须等生产者（顶点着色）全部完成、消费者才能开始。`parallel_execute` 的「阻塞到完成」语义正好提供了这个屏障。

**练习 2**：为什么像素阶段要在**所有 draw 命令**的几何阶段都结束后才开始，而不是每条 draw 命令各自「装配完就立刻渲染它的 tile」？

**参考答案**：因为 tile 队列是**跨所有 draw 命令共享**的——同一个 tile 可能被第 1 条命令的三角形和第 5 条命令的三角形同时覆盖，它们都被 `append` 进同一个 `fTiles[idx]`（[RenderContext.cpp:391-394](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L391-L394)）。只有等所有三角形都入队后，`fillTile` 里 `tile.sort()`（按全局 `sequenceNumber`）才能把跨命令的三角形排成正确的提交序，保证遮挡/混合正确。

**练习 3**：`drawElements` 把 `fCurrentState` 的**快照**追加进队列，而不是存指针。结合 `bindUniforms` 的实现（[RenderContext.cpp:50-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L50-L55)），说明为什么必须存快照。

**参考答案**：应用会在多次 `drawElements` 之间反复改状态（换 shader、换 uniforms）。若只存指针，后一次 `bind*` 会覆盖前一次命令看到的状态。`drawElements` 拷贝 `fCurrentState` 值，使每条命令冻结了它被提交那一刻的状态。uniforms 尤其特殊：`bindUniforms` 把数据 `memcpy` 进 `fAllocator`（[RenderContext.cpp:52-53](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L52-L53)），快照里存的是这份副本的指针——这也解释了 README 注释里「uniforms 在 `finish()` 后失效，下一帧要重新 bind」（[RenderContext.h:67-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.h#L67-L69)），因为 `finish()` 末尾会 `fAllocator.reset()`（[RenderContext.cpp:140](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L140)）释放这块副本。

## 5. 综合实践

**任务**：以 shadow_map 应用为线索，把 Surface → Texture → RenderContext 三大构件串成一条完整的「渲染一帧」数据流，并标注每一步落在哪个源文件、用了什么并行机制。

**操作步骤**：

1. 打开 [shadow_map/main.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp)。先看入口（[main.cpp:55-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L55-L65)）：非 0 线程进 `worker_thread()`（等待被 `parallel_execute` 唤醒），只有线程 0 走 `main` 体——这正是 u9-l2 描述的「所有线程经 `_start` 按线程号分流」。

2. 画出三类 Surface 的角色：
   - `lightMapSurface`（FLOAT，256×256）：光源视角的「影子图」，既是渲染目标又是纹理源；
   - `colorBuffer`（RGBA8888，640×480，绑定到 `frameBuffer`）：最终显示帧缓冲；
   - `depthBuffer`（FLOAT）：深度缓冲。

3. 跟踪**第一遍渲染**（光源视角，[main.cpp:126-141](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L126-L141)）：`bindTarget(lightMapTarget)` → `bindShader` → `clearColorBuffer` → `bindUniforms` → `bindVertexAttrs` → `drawElements`（地面）→ 再 bind/uniform/draw（圆环）→ `finish()`。在 `finish()` 内部，圆环和地面的三角形被装配并塞进 lightMap 的 tile 队列，最后像素阶段把深度写进 `lightMapSurface`。

4. 跟踪**第二遍渲染**（相机视角，[main.cpp:144-166](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L144-L166)）：`bindTexture(0, lightMapTexture)` 把刚才写好的 `lightMapSurface` 当纹理绑上，`bindTarget(outputTarget)` 切到显示帧缓冲，`OutputShader` 在像素着色时采样这张光影图判断该像素是否在阴影里。

5. 画出数据流图（手绘即可）：

   ```
   顶点属性 ──▶ shadeVertices ──▶ 顶点参数
                                   │
                      drawElements │ setUpTriangle（裁剪/剔除/binning）
                                   ▼
                            tile 队列（fTiles）
                                   │  fillTile（排序/光栅/深度/着色/混合）
       Texture(lightMapSurface) ◀──┼──▶ colorBuffer（RGBA8888）
              ▲                     │
              └── bindTexture ── pixel shader 采样
                                   ▼
                          flushTile → dflush → 显示
   ```

**需要观察的现象 / 预期结果**：

- 你能指出 `Surface` 出现在「目标」与「源」两种角色（[main.cpp:73-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L73-L88)），`Texture` 只是把前者包了一层采样接口（[main.cpp:79-81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L79-L81)），`RenderContext::finish` 把全部命令转化成「几何 + 像素」两阶段并行（[RenderContext.cpp:98-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L98-L143)）。
- 你能说清三处并行的引擎都是 `parallel_execute`，分片粒度依次是「顶点(16个/批)」「三角形(个)」「tile(块)」。
- 你能解释为什么渲染完每个 tile 要 `flushTile`：显示设备/VGA 窗口从系统内存读像素，而刚写的像素可能还在 L2（u6-l4 / u8-l2）。

> 待本地验证：在装有 SDL2 的环境用 `run_emulator` 跑 shadow_map，应看到一个旋转的带阴影圆环；改 `#define SHOW_SHADOW_MAP 1`（[main.cpp:35](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/shadow_map/main.cpp#L35)）可直观光影图本身。无图形环境时本任务退化为纯源码阅读，结论同样成立。

## 6. 本讲小结

- **Surface** 是「二维位图 + 预计算向量偏移表」的统一抽象，把 4×4 像素块映射到 16 个向量通道，使整块读写退化为一条 gather/scatter 指令；它同时充当帧缓冲、深度缓冲、纹理数据载体。
- **Texture / mipmap** 把一组按 2 倍逐级缩小的 Surface 组织成纹理金字塔，采样时用相邻像素的纹理坐标差（经 `__builtin_clz` 折成 log₂）挑层级，再按双线性或最近邻取色；Texture 只持指针、不拥有 surface，天然支持 render-to-texture。
- **RenderContext** 采用「先 `bind*`/`drawElements` 录制命令、后 `finish()` 回放」的模型，是 sort-middle / tile-based 架构的入口。
- **`finish()`** 把渲染拆成几何阶段（顶点着色 + 三角形装配）与像素阶段（逐 tile 填充），三类工作用不同分片粒度但**同一套 `parallel_execute` 引擎**驱动所有硬件线程并行，线程间靠「每线程独占 tile」免去像素级锁。
- 几何阶段两步因生产者-消费者依赖而串行；像素阶段等所有三角形入队后才统一排序渲染，以保证跨 draw 命令的正确遮挡与混合顺序。
- 三大构件把 Nyuzi 的「16 通道 SIMD + 多线程」抽象直接落地为软件 GPU：Surface 对应 SIMD 数据布局、Texture 对应 SIMD 采样、RenderContext 对应多线程任务分派。

## 7. 下一步学习建议

本讲只打开了 librender 的「基础三件套」，刻意没碰光栅化与着色的算法细节。建议下一步：

1. **图形渲染管线单元（u13）**：直接深入 [Rasterizer.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp) 与 [TriangleFiller.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp)，搞懂「递归细分到 4×4」「16 通道覆盖测试」「Z-buffer 早期剔除」「透视正确插值」——这些正是本讲里 `fillTile` 调用的 `fillTriangle` 的内部。
2. **着色器接口**：读 [Shader.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Shader.h) 的两个纯虚函数 `shadeVertices` / `shadePixels`，再看 sceneview 的 [TextureShader.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/sceneview/TextureShader.h) 学着写一个自己的着色器。
3. **回看硬件基础**：本讲反复用到的 gather/scatter、`__builtin_clz`、`dflush`、控制寄存器 `CR_RESUME_THREAD`，分别对应 u2-l3（向量访存）、u2-l2（查表指令）、u6-l4（缓存控制）、u9-l2（线程唤醒）。若哪一处感到模糊，回查对应讲义。
4. **动手实验**：拷一份 shadow_map，把 `OutputShader` 换成只输出纯色的简单着色器，重新 `cmake . && make` 并运行，观察画面变化——这是验证你对「命令录制 → finish 回放 → 着色器回调」全链路理解的最快方式。
