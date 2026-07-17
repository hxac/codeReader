# 光栅化与三角形填充

## 1. 本讲目标

本讲是「图形渲染管线」单元的第二篇，承接 [u13-l1 tile-based 渲染架构](u13-l1-render-architecture.md)。在 u13-l1 里我们已经知道：像素阶段 `fillTile` 会逐个取出落在本 tile 里的三角形，对每个三角形调用 `fillTriangle`，并在 `TriangleFiller::fillMasked` 里完成真正的上色。但当时我们把这两步当黑盒略过了。本讲要打开这两个黑盒，回答三个问题：

1. **覆盖判定**：`Rasterizer` 凭什么说「这 16 个像素被三角形覆盖、那 16 个没有」？它用了什么数学工具？
2. **SIMD 并行**：一个 4×4 像素块为什么恰好映射到 Nyuzi 的 **16 个向量通道**？覆盖测试与深度测试是如何「一次算 16 个像素」的？
3. **Z-buffer 早期剔除**：`TriangleFiller` 在着色之前如何用深度缓冲把被遮挡的像素提前剔除，从而省掉最贵的着色计算？

学完后，你应当能画出「递归细分 64→16→4」的三层光栅化树，能解释「边函数 + 平凡接受/拒绝 + 中间递归」三类子块各走哪条路，并能读懂 `fillTriangle` → `rasterizeRecursive` → `subdivideTile` → `fillMasked` 这条从三角形到像素的主调用链。

## 2. 前置知识

阅读本讲前，建议先掌握以下概念（前序讲义已建立）：

- **16 通道 SIMD 与 vector_t**：Nyuzi 一条向量指令同时处理 16 个数据通道，vector_t 由 16 个 32 位标量拼成 512 位（[u2-l1](u2-l1-isa-overview.md)）。
- **gather / scatter 访存**：向量指针逐通道给地址，把 16 个分散的内存单元一次性读进（gather）或写出（scatter）一个向量寄存器（[u2-l3](u2-l3-memory-instructions.md)）。本讲里 4×4 像素块的整块读写正是靠它实现的。
- **Surface 与 4×4 块布局**：Surface 预计算了 `f4x4AtOrigin` 指针向量与 `fXStep/fYStep` 屏幕坐标偏移向量，让一个 4×4 僗素块恰好对齐 16 个通道（[u9-3](u9-l3-librender-basics.md)、`Surface.h`）。
- **tile 架构**：像素阶段每个线程独占一个 64×64 的 tile，把落进来的三角形逐个渲染（[u13-l1](u13-l1-render-architecture.md)）。本讲的 `fillTriangle` 入口就是在「某个 tile 内、对某个三角形」被调用的。

下面用一张表把本讲反复用到的常量列清楚（均来自 `Surface.h`）：

| 常量 | 值 | 含义 |
|------|-----|------|
| `kTileSize` | 64 | 一个 tile 的边长（像素），64×64 |
| `kVectorSize` | 64 | 向量宽度（字节）= 16 通道 × 4 字节 |
| `kMaxParams` | 16 | 每个三角形最多 16 个待插值参数（`TriangleFiller.h`）|

由此可知关键结构：一个 tile 是 64×64 像素，可以切成十六个 16×16 子块，每个 16×16 子块又能切成十六个 4×4 子块——而 4×4=16 正好是 Nyuzi 一条向量指令能并行处理的通道数。这条「4 的幂」几何结构与「16 通道」硬件能力的对齐，是本讲一切 SIMD 优化的根。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `software/libs/librender/Rasterizer.h` / `Rasterizer.cpp` | 本讲主角之一。只负责「覆盖判定」——求出三角形覆盖了哪些像素，对每个 4×4 像素块回调 `TriangleFiller::fillMasked`。核心是递归细分算法。 |
| `software/libs/librender/TriangleFiller.h` / `TriangleFiller.cpp` | 本讲主角之二。一次只持有一个三角形的状态，负责把一个 4×4 块的 16 个像素「并行计算深度、做早期 Z 剔除、插值参数、着色、混合、写回」。 |
| `software/libs/librender/LinearInterpolator.h` | 二维线性插值器：用预计算的梯度 \( (x\cdot g_x + y\cdot g_y + c_{00}) \) 一次算出 16 个像素的参数值。 |
| `software/libs/librender/Surface.h` / `Surface.cpp` | `readBlock`/`writeBlockMasked` 用 gather/scatter 读写 4×4 块；`f4x4AtOrigin`、`fXStep`/`fYStep` 定义 16 通道到 16 像素的映射。 |
| `software/libs/librender/SIMDMath.h` | 向量化的 `min`/`max`/`clamp`/`saturate` 等工具函数。 |

> 本讲不展开「着色器内部到底算什么颜色」（那是 [u13-l3](u13-l3-shaders-textures.md) 的事），只关心 `shadePixels` 被调用**之前**与**之后**光栅化器做的覆盖判定、深度剔除与块写回。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**递归细分** → **SIMD 覆盖测试** → **Z-buffer 早期剔除**。三者恰好对应一条三角形从「宏观覆盖」到「逐像素可见」的判定链。

### 4.1 递归细分光栅化

#### 4.1.1 概念说明：用边函数把「在三角形内」变成一次比较

判断点 \(P\) 是否在三角形内，最稳的办法是**半平面测试（half-plane test）**。对三角形的每条有向边，可定义一个**边函数（edge function）**：

\[
E(x,y) = (x - x_1)(y_2 - y_1) - (y - y_1)(x_2 - x_1)
\]

其中 \((x_1,y_1)\to(x_2,y_2)\) 是这条边的两个端点。几何上，\(E\) 在被这条边切开的两个半平面里符号相反。只要约定三角形的顶点按**逆时针（CCW）**顺序给出，那么「在三角形内」就等价于「对三条边都有 \(E \le 0\)」。

逐像素算三个 \(E\) 再判号当然可行，但对一个 64×64 的 tile 就要算 4096 次。`Rasterizer.cpp` 顶部注释点明了它用的是更快的方法：

[Rasterizer.cpp:17-23](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L17-L23) 中文说明：算法参考 Larrabee 光栅化文章与 Ned Greene 的 SIGGRAPH'93 论文《Hierarchical polygon tiling with coverage masks》——即**层次化（hierarchical）覆盖掩码**。

层次化的核心思想是：边函数是**线性**的，所以在一个矩形块上，\(E\) 的最大值和最小值都出现在块的**四个角**之一。于是对每个块可以做两种「一刀切」判定：

- **平凡接受（trivial accept）**：取块上 \(E\) **最大**的那个角（最可能在外侧的角）。若连这个最坏的角都满足 \(E \le 0\)，则整块都在边内侧。
- **平凡拒绝（trivial reject）**：取块上 \(E\) **最小**的那个角（最可能在内侧的角）。若连这个最好的角都满足 \(E > 0\)，则整块都在边外侧。

对三条边同时做这两种判定，就能把任意一个矩形块归入三类：

```
            三条边都"平凡接受"      ──►  整块完全在三角形内   → 直接整块填充（快路径）
任意一条边"平凡拒绝"                ──►  整块完全在三角形外   → 直接丢弃
            两者都不是              ──►  部分覆盖            → 切成更小的子块，递归再判
```

这就是「递归细分」的全部直觉：先把 tile 当一个 64×64 的大块判一次，对部分覆盖的子块切成 16×16 再判，再部分覆盖就切成 4×4。切到 4×4 时，16 个采样点恰好就是 16 个像素，平凡接受掩码就变成了**精确的逐像素覆盖掩码**。

#### 4.1.2 核心流程：64 → 16 → 4 的三层递归

光栅化入口是 `fillTriangle`，它先算三角形的包围盒，再在「扫描法」与「递归法」之间二选一：

```
fillTriangle(filler, tileLeft, tileTop, 三个顶点, clipRight, clipBottom)
   │
   ├─ 算三角形在 tile 内的包围盒 bbLeft/bbTop/bbRight/bbBottom（按 4 对齐）
   │
   ├─ 若包围盒很小 (< kMaxSweep) ──► rasterizeSweep   // 注：kMaxSweep=0，永远不取
   └─ 否则 ─────────────────────► rasterizeRecursive
                                       │
                                       ├─ setupRecurseEdge × 3   // 为三条边各算接受/拒绝角值与步进矩阵
                                       └─ subdivideTile(tileSizeBits = log2(64) = 6, ...)
                                              │
                                              ├─ tileSizeBits == 2 ?   // 已经是 4×4 叶子
                                              │     └─ 是：trivialAcceptMask 即精确覆盖掩码 → filler.fillMasked(...)
                                              │
                                              ├─ 整块接受的子块：用掩码 0xffff 逐 4×4 块填充（快路径）
                                              ├─ 整块拒绝的子块：跳过
                                              └─ 部分覆盖的子块：步进矩阵除以 4，递归 subdivideTile(tileSizeBits - 2)
```

递归深度由 `tileSizeBits` 控制：初始值 `__builtin_ctz(kTileSize)` 即 \(\log_2 64 = 6\)，每深入一层减 2，因此只有三个层级的内部块需要递归：

| 层级 | tileSizeBits | 子块边长 | 一个 tile 含多少个这样的子块 |
|------|--------------|----------|------------------------------|
| 顶层 | 6 | 64 | 1（整个 tile）|
| 中层 | 4 | 16 | 16 个 16×16 |
| 叶层 | 2 | 4 | 每个 16×16 再切成 16 个 4×4，共 256 个 |

注意叶层的 4×4 块**不再做拒绝判断**：它的 16 个采样点就是 16 个像素，「平凡接受」的角点退化为像素自身，掩码位为 1 表示该像素被覆盖，为 0 则不画。

#### 4.1.3 源码精读

**(1) 入口与包围盒** —— `fillTriangle` 把三角形坐标在 tile 内按 4 对齐后求包围盒，并选择光栅化策略：

[Rasterizer.cpp:353-370](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L353-L370) 中文说明：先求包围盒（`min3`/`max3` 分别取三顶点坐标的最小/最大值，`& ~3` 把坐标向 4 的倍数对齐，再用 `clipRight/clipBottom/tileLeft+kTileSize` 三者夹取到本 tile 与屏幕范围内）。随后判断包围盒是否小于 `kMaxSweep`：是则走扫描法 `rasterizeSweep`，否则走递归法 `rasterizeRecursive`。

需要留意一处「陷阱」：文件顶部的 `kMaxSweep` 被定义为 0：

[Rasterizer.cpp:33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L33) 中文说明：`const int kMaxSweep = 0;`。由于包围盒宽高不可能小于 0，`bbRight - bbLeft < 0` 恒为假，所以**扫描法分支永远不会执行**——`rasterizeSweep` 目前是禁用的实验代码（注释也写了 "Currently disabled"）。实际生产路径永远走 `rasterizeRecursive`。

**(2) 边的初始化** —— `setupRecurseEdge` 为一条边算出两个角点的边函数值，以及用于「一次算 16 个子块」的步进矩阵：

[Rasterizer.cpp:37-98](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L37-L98) 中文说明：函数接收一条边的两端 \((x_1,y_1)\to(x_2,y_2)\) 与当前块的左上角 `(tileLeft, tileTop)`，输出接受角值 `outAcceptEdgeValue`、拒绝角值 `outRejectEdgeValue` 及两个步进矩阵。

关键几行：

- `int xStep = y2 - y1; int yStep = x2 - x1;`（第 73–74 行）：把边函数 \(E=(x-x_1)\Delta_x-(y-y_1)\Delta_y\) 的两个系数提出来，其中 \(\Delta_x=y_2-y_1\)、\(\Delta_y=x_2-x_1\)。
- `outAcceptEdgeValue = (trivialAcceptX - x1) * xStep - (trivialAcceptY - y1) * yStep;`（第 76 行）：在「接受角」处计算 \(E\)。`trivialAcceptX/Y` 是依据边方向选出的「最坏情况角」（见前 `if (y2 > y1)` / `if (x2 > x1)` 分支，分别把角点偏移到块的右侧或下侧）。
- `if (y1 > y2 || (y1 == y2 && x2 > x1)) { outAcceptEdgeValue++; outRejectEdgeValue++; }`（第 79–85 行）：实现 **top-left 填充约定**。相邻三角形共享边时，用这个约定保证每个像素只被其中一个三角形拥有，避免接缝处出现「漏点」或「重叠」。
- `outAcceptStepMatrix = xAcceptStepValues - yAcceptStepValues;`（第 96 行）：把 `kXStep`（列偏移 0,1,2,3）与 `kYStep`（行偏移 0,1,2,3）乘上子块尺寸与边系数，得到「从角值出发，加上这个矩阵就得到 16 个子块各自的接受角值」的步进矩阵。

**(3) 递归主体** —— `subdivideTile` 是真正的「干活的马」，把当前块判成接受/拒绝/递归三类：

[Rasterizer.cpp:101-137](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L101-L137) 中文说明：先用 `acceptStep + acceptCornerValue` 算出 16 个子块的接受角值，再用三次 `__builtin_nyuzi_mask_cmpi_sle(..., 0)`（有符号小于等于 0）按位与，得到 `trivialAcceptMask`——它是一个 16 位掩码，第 `i` 位为 1 表示第 `i` 个子块对三条边都平凡接受。若已到叶子（`tileSizeBits == 2`），且掩码非零，就直接 `filler.fillMasked(tileLeft, tileTop, trivialAcceptMask)` 把覆盖的像素交给填充器，然后返回。

[Rasterizer.cpp:141-161](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L141-L161) 中文说明：对**平凡接受**的子块走快路径——用 `__builtin_ctz` 逐个找出掩码里置位的位，换算出该子块的左上角坐标 `subTileLeft/subTileTop`，再用 0xffff（整块覆盖）掩码，按 4×4 步长双重循环调用 `fillMasked(..., 0xffff)`。一个大三角形内部的成片像素由此被极快地刷掉，不必逐像素判定。

[Rasterizer.cpp:163-214](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L163-L214) 中文说明：再算拒绝掩码 `trivialRejectMask`（任意一条边的拒绝角值 \(>0\) 即拒绝）。把「既不接受也不拒绝」的位取出来作为 `recurseMask`，对其中每一位置位，把步进矩阵整体右移 2（即除以 4，因为子块边长缩为一半），以该子块的接受/拒绝角值为新的角值，递归调用 `subdivideTile` 自身。注意第 191 行 `if (x >= clipRight || y >= clipBottom) continue;` 把超出屏幕的子块剪掉。

**(4) 递归启动** —— `rasterizeRecursive` 为三条边各调用一次 `setupRecurseEdge`，然后从顶层 `tileSizeBits = __builtin_ctz(kTileSize)` 开始递归：

[Rasterizer.cpp:217-262](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L217-L262) 中文说明：第 234–235 行注释强调「假设朝向相机的三角形是逆时针（CCW）」。三条边分别取 \((v_1\to v_3)\)、\((v_3\to v_2)\)、\((v_2\to v_1)\)（注意顺序与 u13-l1 里 `woundCCW` 的分支选择是对应的）。第 257 行 `__builtin_ctz(kTileSize)` 即 6，是递归的起始 `tileSizeBits`。

> 「接受用 \(\le 0\)、拒绝用 \(>0\)」的不对称，正是因为采用了 CCW 内侧为负的边函数约定；top-left 约定的 +1 偏移也建立在同一套符号约定之上。三者必须配套使用，不能单独改。

#### 4.1.4 代码实践：手动走一遍 64→16→4 的递归

**实践目标**：用一个极小的例子，亲手追踪一次递归细分的判定路径，理解「接受/拒绝/递归」三类分支如何交替出现。

**操作步骤**（源码阅读型实践）：

1. 想象一个屏幕为 64×64（恰好一个 tile）、三角形为「右上半三角形」，顶点 \((0,0)、(64,0)、(0,64)\)（CCW）。调用 `fillTriangle(filler, 0, 0, 0,0, 64,0, 0,64, 64,64)`。
2. 在 `subdivideTile` 里，顶层的 16 个子块（每个 16×16）会得到什么样的 `trivialAcceptMask` 与 `trivialRejectMask`？
   - 左上角那个 16×16 子块（索引 0）完全在三角形内 → 接受位为 1。
   - 右下角那个 16×16 子块（索引 15，即列 3 行 3）完全在三角形外 → 拒绝位为 1。
   - 对角线附近（如索引 5、6、9、10）的子块部分覆盖 → 进入递归掩码。
3. 进入递归的子块再切成 16 个 4×4。其中左上的 4×4 子块继续「整块接受」（走第 4.1.3 的快路径，用 0xffff 掩码），对角线上的 4×4 子块到达叶子 `tileSizeBits==2`，其 `trivialAcceptMask` 就是该 4×4 块里 16 个像素的精确覆盖掩码（可能是类似 `0b0111_0011_0001_0000` 这样的形状）。

**需要观察的现象**：

- 大三角形内部用极少的递归层 + 0xffff 快速填充就能覆盖大片像素；
- 只有沿三角形斜边的薄薄一层才需要递归到 4×4 叶子做精确判定；
- 完全在外的整块（如右下角）在拒绝掩码判定后立即被丢弃，**根本不会调用 `fillMasked`**。

**预期结果**：你能画出一棵「64 → 16 → 4」的三层树，并标注每个内部节点属于「整块接受 / 整块拒绝 / 部分覆盖（递归）」哪一类。三角形越大、越接近轴对齐，递归到叶子的子块比例越低，光栅化越快。

> 待本地验证：若你已在 [u1-l2](u1-l2-build-and-run.md) 构建出 `nyuzi_emulator`，可运行 `tests/render/triangle`（见第 5 节综合实践）观察一个真实三角形的渲染输出，对照上面的判定路径。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `tileSizeBits == 2` 改成 `tileSizeBits == 0`（即递归到 1×1 才停），会发生什么？

**参考答案**：叶子的 16 个采样点会挤进一个 1×1 区域里互相重叠，覆盖掩码失去空间分辨率意义；同时 `subAcceptStep` 不断右移会变成 0，导致所有 16 个采样点的边函数值相同。结果是覆盖判定退化、且叶子数量爆炸（256 个 4×4 → 4096 个 1×1）。代码刻意停在 4×4，正是因为 16 通道向量一次正好处理 4×4，停在叶子即「一次向量比较得到 16 个像素的精确覆盖」，是几何与硬件的对齐点。

**练习 2**：为什么「平凡接受」取的是块上 \(E\) **最大**的角，而不是最小的角？

**参考答案**：因为接受判据是「整块都满足 \(E \le 0\)」。若 \(E\) 最大的那个角（最难满足条件的角）都已经 \(\le 0\)，那么块的其余部分必然更小、更满足条件，整块接受成立。反之若取最小角，即使它 \(\le 0\) 也不能保证其它角也 \(\le 0\)，会得到错误的「整块接受」。代码里 `trivialAcceptX/Y` 依据边方向选择的正是最大角。

---

### 4.2 SIMD 覆盖测试：4×4 块如何映射到 16 个通道

#### 4.2.1 概念说明：16 通道与 4×4 像素块的天作之合

第 4.1 节我们看到，`subdivideTile` 反复用「一个向量加上一个角值，得到 16 个数」的方式同时处理 16 个子块。这之所以能成立，靠的是两件事的对齐：

1. **几何上**：每个块被切成 \(4\times 4=16\) 个子块，天然对应 16 个采样点。
2. **硬件上**：Nyuzi 的向量指令一次处理 16 个通道，且能用 `__builtin_nyuzi_mask_cmpi_*` 这类「向量比较」直接产出 16 位的掩码。

这两者结合，就把「对一个块做覆盖测试」变成了「一两条向量指令」。本模块专门讲清楚这 16 个通道与 16 个像素/子块的**排列对应关系**——它既是覆盖掩码的解释方式，也是后面 `fillMasked` 用 gather/scatter 读写整块的依据。

#### 4.2.2 核心流程：从 16 位掩码到 (列, 行) 坐标

16 个通道在一个 4×4 块里的排列，由两个常量向量定义：

[Rasterizer.cpp:34-35](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L34-L35) 中文说明：`kXStep = {0,1,2,3, 0,1,2,3, 0,1,2,3, 0,1,2,3}` 给出每个通道的列号，`kYStep = {0,0,0,0, 1,1,1,1, 2,2,2,2, 3,3,3,3}` 给出每个通道的行号。通道号 `index`（0–15）与 (列, 行) 的换算是：

\[
\text{列} = \text{index}\ \&\ 3, \qquad \text{行} = \text{index} \gg 2
\]

即通道按**行优先**排布：

```
通道号 index        列(index&3)  行(index>>2)
 0  1  2  3          0 1 2 3      0 0 0 0
 4  5  6  7    →     0 1 2 3      1 1 1 1
 8  9 10 11          0 1 2 3      2 2 2 2
12 13 14 15          0 1 2 3      3 3 3 3
```

这与 `Surface` 的 `writeBlockMasked`/`readBlock` 注释里画的 4×4 布局**完全一致**：

[Surface.h:62-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L62-L67) 中文说明：注释明确给出 4×4 块的通道顺序就是 `0 1 2 3 / 4 5 6 7 / 8 9 10 11 / 12 13 14 15`，与光栅化器的掩码位排列一一对应。

因此一个 16 位掩码 `mask` 的第 `i` 位，描述的就是 4×4 块里 (列=`i&3`, 行=`i>>2`) 那个像素/子块的状态。掩码位 → 坐标的换算在代码里出现两次，用法一致：

[Rasterizer.cpp:148-151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L148-L151) 中文说明：用 `__builtin_ctz(currentMask)` 找到最低位的置位（即当前要处理的通道号），换算成子块左上角坐标 `subTileLeft = tileLeft + ((index & 3) << subTileSizeBits)`、`subTileTop = tileTop + ((index >> 2) << subTileSizeBits)`，再把该通道从掩码中清掉继续循环。

#### 4.2.3 源码精读：向量化的边函数与掩码生成

**(1) 一次算 16 个采样点的边函数值**。在 `subdivideTile` 开头：

[Rasterizer.cpp:122-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Rasterizer.cpp#L122-L128) 中文说明：`acceptEdgeValue1 = acceptStep1 + acceptCornerValue1`。这里 `acceptStep1` 是一个 16 元素的向量（步进矩阵），`acceptCornerValue1` 是一个标量（角值），相加时标量被**广播**到 16 个通道（这正是 [u5-l1](u5-l1-operand-fetch.md) 里讲的「标量即退化的向量」）。一条加法指令就得到了 16 个子块各自的接受角值。三条边各算一次，得到三个 16 元素向量。

**(2) 向量比较直接出掩码**：

```c
const vmask_t trivialAcceptMask =
        __builtin_nyuzi_mask_cmpi_sle(acceptEdgeValue1, veci16_t(0))   // 边1：E<=0 ?
        & __builtin_nyuzi_mask_cmpi_sle(acceptEdgeValue2, veci16_t(0))  // 边2
        & __builtin_nyuzi_mask_cmpi_sle(acceptEdgeValue3, veci16_t(0)); // 边3
```

`__builtin_nyuzi_mask_cmpi_sle(a, b)` 对 16 个通道逐个做有符号「小于等于」比较，把结果压成一个 16 位掩码（对应 [u2-l2](u2-l2-arithmetic-instructions.md) 里讲的向量比较指令）。三个掩码按位与，就是「三条边同时满足」的子块集合。这一步把「16 个子块 × 3 条边 = 48 次比较」压缩成 3 条向量比较指令 + 2 个按位与。

**(3) 整块读写：gather/scatter**。覆盖判定之后，`fillMasked` 要把结果写回颜色缓冲（和读深度缓冲）。`Surface` 预计算了一个指针向量 `f4x4AtOrigin`，让 16 个通道各自指向 4×4 块里对应像素的地址：

[Surface.cpp:92-108](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L92-L108) 中文说明：`f4x4AtOrigin` 初始为 `{0,4,8,12, 0,4,8,12, 0,4,8,12, 0,4,8,12}`（列内像素地址偏移，RGBA8888 每像素 4 字节），再叠加 `widthOffset * fWidth`（行偏移）与 `fBaseAddress`（基地址），得到 16 个通道各自的绝对像素地址。

随后 `writeBlockMasked`/`readBlock` 用一条 scatter/gather 指令处理整块：

[Surface.h:68-80](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L68-L80) 中文说明：`writeBlockMasked` 用 `__builtin_nyuzi_scatter_storei_masked(ptrs, values, mask)` 把向量 `values` 按 `mask` 逐通道写到 `ptrs` 指向的 16 个像素；`readBlock` 用 `__builtin_nyuzi_gather_loadi(ptrs)` 把 16 个像素读进一个向量。`left`/`top` 偏移在调用处叠加（`f4x4AtOrigin + left*4 + top*fStride`），定位到任意 4×4 块。

> 一条 scatter/gather 指令读写整块 4×4——这正是 [u13-l1](u13-l1-render-architecture.md) 所说的「tile 工作集常驻 L2、块访问对齐缓存行」在代码层面的体现：64 字节的向量恰好等于一个缓存行（[u3-3](u3-l3-config-synthesis.md)）。

#### 4.2.4 代码实践：把 16 个通道贴到 4×4 像素上

**实践目标**：亲手验证「16 位掩码 ↔ 4×4 像素」的对应关系，理解一条向量指令如何同时处理 16 个像素。

**操作步骤**（源码阅读型实践）：

1. 打开 `Surface.h` 第 62–67 行的 4×4 布局注释，与 `Rasterizer.cpp` 第 34–35 行的 `kXStep`/`kYStep` 对照，确认两者通道排列一致。
2. 假设某个 4×4 叶子块的覆盖掩码是 `mask = 0xF0F0`（二进制 `1111_0000_1111_0000`）。画出哪 8 个通道（像素）被覆盖。
   - 解：置位的是 index = 4,5,6,7,12,13,14,15，即第 1 行和第 3 行的全部像素。
3. 在 `Surface.h` 的 `writeBlockMasked` 里，这 8 个置位通道对应的 `ptrs` 偏移分别是 `f4x4AtOrigin + left*4 + top*fStride` 后的第 4..7、12..15 个元素，即第 1、3 行各 4 个像素的地址——scatter 指令只写这 8 个，其余 8 个保持不变。

**需要观察的现象**：掩码的位排列不是任意的，它严格按行优先映射到 4×4 块；只要掩码与指针向量的排列一致，一条 scatter 指令就能「按掩码精确写回被覆盖的像素」。

**预期结果**：你能用一句话说清——「掩码第 `i` 位 ↔ 4×4 块的 (列 `i&3`, 行 `i>>2`) 像素 ↔ 向量第 `i` 通道」，并能解释为何这种统一排列让覆盖判定、深度测试、块写回都能复用同一套 16 通道逻辑。

#### 4.2.5 小练习与答案

**练习 1**：`kXStep` 为什么是 `{0,1,2,3, 0,1,2,3, ...}` 而不是 `{0,1,2,...,15}`？

**参考答案**：因为 4×4 块只有 4 列，列号在 0–3 之间循环重复；同一行的 4 个像素列号是 0,1,2,3，换行后列号又从 0 开始。`kYStep` 同理在行方向上每 4 个通道递增一次。这两个向量合起来精确描述了 16 个通道在 4×4 网格里的 (列, 行) 位置，是「把几何坐标编进向量」的标准手法。

**练习 2**：`__builtin_ctz(mask)` 在覆盖测试里用来做什么？为什么用它而不是从 0 到 15 顺序遍历？

**参考答案**：`__builtin_ctz` 返回一个整数最低位的置位编号，用来在掩码里「挑出下一个被覆盖的子块」。它比顺序遍历 16 位快得多——掩码里有多少个 1 就只迭代多少次，对全 0 的掩码直接跳过（不进循环）。在稀疏覆盖（如细长三角形）的场景下，这能显著减少无效迭代。

---

### 4.3 Z-buffer 早期剔除与像素着色

#### 4.3.1 概念说明：在着色之前先丢掉被挡住的像素

光栅化知道「哪些像素被三角形覆盖」之后，还不能直接上色——因为后画的三角形可能被前面的物体挡住，这些像素的颜色最终根本看不见。**深度缓冲（Z-buffer / depth buffer）**记录了每个像素当前最近的深度，新像素只有在「比我已记录的更近」时才应被写入。

最朴素的深度测试流程是：**先着色、再测深度、按结果决定是否写回**。但着色（`shadePixels`）往往是最贵的环节——要采样纹理、算光照。如果一个 4×4 块的 16 个像素里大半都被遮挡，先全算一遍颜色再丢掉，纯属浪费。

`TriangleFiller::fillMasked` 因此采用 **早期 Z（early-Z）**：在调用着色器**之前**就做完深度测试，把不通过的像素从掩码里剔掉，着色器只对着真正可见的像素工作。本模块讲清这套「深度计算 → 早期剔除 → 着色 → 混合 → 写回」的流水。

#### 4.3.2 核心流程：fillMasked 的六步

```
fillMasked(left, top, mask)            // mask: 覆盖掩码（来自光栅化器）
  1. 坐标转换：把 4×4 块的光栅坐标 (left, top) 换算成屏幕空间向量 x, y
  2. 算深度：用插值器算 16 个像素的 z（透视正确 1/z，或常数）
  3. 早期 Z（若开启深度缓冲）：
       a. readBlock 读出 16 个像素的旧深度
       b. passDepthTest = (新z > 旧深度) 的 16 位掩码
       c. mask &= passDepthTest        // 把未通过的像素剔出
       d. 若 mask == 0 直接 return       // 整块被遮挡，省掉着色
       e. writeBlockMasked 写入新深度
  4. 插值参数：对每个参数（最多 16 个）算 16 个像素的值（透视正确需乘回 z）
  5. 着色：shadePixels(color, params, ...) 算出 16 个像素的颜色
  6. 颜色转换与混合：RGBA8888 量化、可选 alpha 混合，writeBlockMasked 写回颜色缓冲
```

几个关键点提前点出：

- 深度测试用 **「大于」**（`z > 旧深度`）：Nyuzi 采用「**z 越大越靠近相机**」的约定。深度缓冲在 tile 开始时被初始化为 `0xff800000`，即浮点数 **\(-\infty\)**（见 [RenderContext.cpp:434-436](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L434-L436)），所以任何有限 z 都大于它，第一个写入的像素必然通过测试。
- 早期 Z 的「早」体现在步骤 3 在步骤 4–5（参数插值与着色）之前；步骤 3d 的早退更是把「整块被遮挡」的情况变成零成本。

#### 4.3.3 源码精读

**(1) 坐标转换与深度计算**：

[TriangleFiller.cpp:135-146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L135-L146) 中文说明：先用 `fTwoOverWidth`/`fTwoOverHeight`（构造时算好的 \(2/W\)、\(2/H\)）与 `getXStep()`/`getYStep()`（4×4 块内 16 个像素的屏幕坐标偏移）把光栅坐标换算到 \([-1,1]\) 屏幕空间，得到向量 `x`、`y`。再算 16 个像素的深度 `zValues`：若需要透视修正（三个顶点 z 不全相等），则 `z = 1.0 / fOneOverZInterpolator.getValuesAt(x,y)`（先线性插值 \(1/z\)，再取倒数得到透视正确的 z，对应文件第 32–42 行的注释原理）；否则直接用常数 `fZ0`。

**(2) 早期 Z 剔除（本模块核心）**：

[TriangleFiller.cpp:148-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L148-L160) 中文说明：当状态开启了深度缓冲（`fState->fEnableDepthBuffer`）：用 `readBlock(left, top)` 一次读出 16 个像素的旧深度；`__builtin_nyuzi_mask_cmpf_gt(zValues, depthBufferValues)` 做 16 通道浮点「大于」比较，得到通过掩码 `passDepthTest`；`mask &= passDepthTest` 把未通过深度测试的像素从覆盖掩码中剔除——这就是**早期 Z 优化**。若剔除后 `mask == 0`（整块全被遮挡），立即 `return`，连参数插值和着色都跳过；否则用 `writeBlockMasked` 写入新的更近深度。

**(3) 参数插值（透视正确）**：

[TriangleFiller.cpp:162-178](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L162-L178) 中文说明：对每个参数，常数参数直接广播；需要透视修正的参数用 `linearInterpolator.getValuesAt(x,y) * zValues`——先在屏幕空间线性插值「参数/z」，再乘回 z 还原成透视正确的参数值（与第 32–42 行注释、第 107–133 行 `setUpParam` 的设置对应）。这一步一次算出 16 个像素的全部参数。

[TriangleFiller.cpp:90-103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L90-L103) 中文说明：`setUpInterpolator` 在 `setUpTriangle` 阶段预先把「三顶点的参数值」反解成平面梯度 \((g_x, g_y)\) 与原点值 \(c_{00}\)，于是任意点的参数就是线性的 \(x\cdot g_x + y\cdot g_y + c_{00}\)（见 `LinearInterpolator::getValuesAt`）。把「插值」变成「一次平面方程求值」，是 16 通道并行插值的前提。

**(4) 着色、颜色转换与写回**：

[TriangleFiller.cpp:180-233](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/TriangleFiller.cpp#L180-L233) 中文说明：调用 `fState->fShader->shadePixels(...)` 让着色器对 16 个像素算出颜色（着色器细节留待 [u13-l3](u13-l3-shaders-textures.md)）；随后按颜色空间转换：RGBA8888 下用 `clamp` 把 \([0,1]\) 颜色量化到 0–255 并打包成 `0xff000000 | r | (g<<8) | (b<<16)`；若开启混合且 alpha 小于 1，则读取目标旧颜色做**预乘 alpha 混合**（`newR = ((s<<8) + d*(255-a))>>8`）。最后 `destSurface->writeBlockMasked(left, top, mask, pixelValues)` 用最初的掩码 `mask` 把结果 scatter 写回颜色缓冲——被早期 Z 剔除或未被覆盖的像素（掩码位为 0）不会被写。

> 注意深度写回用的是**测试后的掩码**（第 159 行 `writeBlockMasked(..., mask, ...)` 的 `mask` 已经 `&= passDepthTest`），而颜色写回用的是同一个掩码。这意味着「未通过深度测试的像素既不更新深度、也不更新颜色」，深度缓冲始终持有每个像素最近的可见深度。

#### 4.3.4 代码实践：观察早期 Z 的早退

**实践目标**：通过阅读与（可选的）运行，理解早期 Z 如何在「整块被遮挡」时省掉着色。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 `TriangleFiller.cpp` 第 148–160 行的早期 Z 块里，追踪 `mask` 的变化：进入时它是光栅化器给的覆盖掩码（第 4.2 节），经过 `mask &= passDepthTest` 后只剩「既被覆盖又通过深度测试」的像素。
2. 构造一个心智实验：两个完全重叠的 4×4 三角形，A 在前（z 大）、B 在后（z 小），按 B 先画、A 后画的顺序。当画 A 时：
   - `readBlock` 读出的旧深度是 B 写入的小 z；
   - A 的新 z（大）`> B 的旧 z`，`passDepthTest` 全 1，`mask` 不变，A 正常覆盖 B；
   - 反过来，若画 B（后 z）时 A（前 z）已在深度缓冲里，则 `passDepthTest` 全 0，`mask` 变 0，第 156 行 `if (mask == 0) return;` 直接退出——**B 的着色被完全跳过**。
3. （可选运行）若已构建环境，运行 `tests/render/depthbuffer`（`tests/render/depthbuffer/runtest.py`），观察有/无深度缓冲时三角形相互遮挡的结果差异（注意该测试默认目标为 emulator，见 [u15-l1](u15-l1-test-harness.md) 的测试框架）。

**需要观察的现象**：

- 早期 Z 让「被完全遮挡的 4×4 块」在着色前就退出，避免无用的 `shadePixels` 调用；
- 深度缓冲的初值 \(-\infty\) 保证了第一个写入任何像素的三角形必然通过测试；
- 「z 越大越近」的约定与初值方向一致，无需特判第一个像素。

**预期结果**：你能解释 `mask &= passDepthTest; if (mask == 0) return;` 这两行为何是性能关键——它们把最贵的着色计算限制在「真正可见」的像素上。待本地验证：在真实场景（如 `tests/render/triangle` 叠两个三角形）里，开启/关闭 `fEnableDepthBuffer` 对渲染结果与耗时的差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么深度缓冲初值是 `0xff800000` 而不是 `0`？

**参考答案**：`0xff800000` 作为 IEEE754 单精度浮点数是 \(-\infty\)。Nyuzi 用「z 越大越近、测试为 `z > 旧深度`」的约定，所以初值取 \(-\infty\) 能保证任何有限的 z 都满足 `z > -∞`，第一个写入的像素无条件通过。若初值是 0，那么所有 z ≤ 0 的像素都会被错误剔除。

**练习 2**：早期 Z 把不通过的像素从 `mask` 里剔除后，着色器 `shadePixels` 收到的 `mask` 是什么？它如何利用这个掩码？

**参考答案**：着色器收到的是**剔除后**的掩码。它按这个掩码只对置位的通道计算颜色（例如只对可见像素采样纹理）。最后颜色写回 `Surface::writeBlockMasked` 时也用同一掩码做 scatter，未置位的通道不写。这样从插值、着色到写回，全程只对真正可见的像素付出代价——这是「像素级并行 + 掩码驱动」的统一收益。

**练习 3**：早期 Z 写深度（第 159 行）用的掩码，与最后写颜色（第 232 行）用的掩码是同一个吗？为什么这样设计是正确的？

**参考答案**：是同一个（都是经过 `mask &= passDepthTest` 之后的掩码）。因为「未通过深度测试的像素」既不应更新颜色（它不可见），也不应更新深度（它的 z 比当前记录的远，覆盖掉会更糟）。用同一掩码保证「深度缓冲始终记录每个像素最近的可见深度」，与颜色缓冲的内容一一对应。

## 5. 综合实践

**任务**：把本讲三个模块串起来，跑通一个真实三角形并解释它经过的完整光栅化链路。

**步骤**：

1. **构建与运行**（依据 [u1-l2](u1-l2-build-and-run.md)、[u1-l4](u1-l4-first-program.md)）。运行渲染测试三角形：

   ```bash
   cd tests/render/triangle
   ./runtest.py            # 调用 test_harness，在 emulator 目标上编译 main.cpp 并运行
   ```

   该测试在 [tests/render/triangle/runtest.py:23-25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/runtest.py#L23-L25) 注册，目标为 `emulator`，并与一张参考图 `reference.png` 的哈希比对来判定通过。若环境不允许运行，改为源码阅读型实践（见步骤 3）。

2. **对照源码还原链路**。这个三角形在像素阶段被某个线程的 `fillTile` 取出（[RenderContext.cpp:422-494](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L422-L494)），依次经过：
   - `filler.setUpTriangle(...)` 设置三角形与插值矩阵；
   - `filler.setUpParam(...)` 设置参数；
   - `fillTriangle(filler, tileX, tileY, ...)` 进入光栅化器。
   
   请在本讲的源码里逐一标出下列每一步发生在哪一行：
   - 包围盒计算与递归启动（4.1.3）；
   - 16 通道覆盖掩码的生成（4.2.3）；
   - 每个被覆盖的 4×4 块回调 `fillMasked`（4.1.3 叶子与快路径）；
   - 早期 Z 剔除、参数插值、着色、写回（4.3.3）。

3. **画一张「三角形 → 像素」全链路图**。要求至少包含：递归细分的三层（64/16/4）、平凡接受快路径与叶子精确掩码两条分支、`fillMasked` 内的早期 Z 早退分支、以及 16 位掩码如何同时驱动深度写回与颜色写回。

**预期结果**：你能用一段话向别人讲清「一个三角形从三条边函数开始，如何经过层次化覆盖判定、16 通道 SIMD 并行、早期 Z 剔除，最终变成帧缓冲里一组带颜色的像素」，并指出每一步对应的源码行号。待本地验证：若能运行，截图渲染结果并与 `reference.png` 对照；若不能，至少完成步骤 2 的行号标注与步骤 3 的链路图。

## 6. 本讲小结

- **光栅化 = 层次化覆盖判定**。`Rasterizer` 用边函数 \(E=(x-x_1)(y_2-y_1)-(y-y_1)(x_2-x_1)\) 把「点是否在三角形内」变成「\(E\le 0\) 与否」，再利用边函数的线性性，对每个矩形块取最坏角做「平凡接受 / 平凡拒绝」，部分覆盖的块递归细分。
- **递归深度与几何对齐**。从 64×64 顶层（`tileSizeBits=6`）每层边长除 4，到 4×4 叶子（`tileSizeBits=2`）停止。叶子处 16 个采样点即 16 个像素，平凡接受掩码变成精确覆盖掩码；大三角形内部走 0xffff 整块快路径，只有斜边附近才递归到叶子。
- **16 通道 ↔ 4×4 像素一一对应**。`kXStep`/`kYStep` 把通道号编成 (列, 行)，掩码第 `i` 位 ↔ 像素 (列 `i&3`, 行 `i>>2`)；向量比较一次出 16 位掩码，gather/scatter 一次读写整块 64 字节（= 一个缓存行）。
- **早期 Z 在着色前剔除**。`TriangleFiller::fillMasked` 先算深度、再做 `mask &= (z>旧深度)`，整块被遮挡时 `mask==0` 直接 return，跳过最贵的参数插值与着色；深度与颜色用同一掩码写回，保证深度缓冲始终记录最近可见像素。
- **约定必须配套**。CCW 顶点序 + 「内侧 \(E\le 0\)」+ 「接受用 \(\le\)、拒绝用 \(>\)」+ top-left 偏移 +1 + 「z 越大越近、初值 \(-\infty\)」共同构成一套自洽的渲染约定，改动其一需连带检查其余。
- **扫描法已禁用**。`kMaxSweep=0` 使 `rasterizeSweep` 永不执行，生产路径恒为递归法；读代码时不要被这条分支误导。

## 7. 下一步学习建议

本讲把「三角形 → 被覆盖的像素」这条链讲完了，但刻意没碰两件事：着色器内部到底怎么算颜色、纹理怎么采样。建议下一步：

1. **学习 [u13-l3 着色器与纹理采样](u13-l3-shaders-textures.md)**。本讲里 `shadePixels` 只是个回调，参数是「透视正确插值后的逐像素值」。下一讲会以 sceneview 的 `TextureShader`/`DepthShader` 为例，讲清着色器回调接口、透视正确插值（与 `TriangleFiller.cpp` 第 32–42 行的注释呼应）和 mipmap 纹理采样的实现。
2. **回看几何阶段**。如果你还想理解「三角形的三个顶点和参数从何而来」，重读 [u13-l1](u13-l1-render-architecture.md) 的 `shadeVertices`/`setUpTriangle`/`enqueueTriangle` 部分——本讲的 `setUpTriangle`/`setUpParam` 正是几何阶段为每个三角形准备好的状态。
3. **延伸阅读**。`Rasterizer.cpp` 顶部给出的两篇参考文献（Larrabee 光栅化、Greene 的层次化覆盖掩码）是本算法的原始出处，值得对照阅读以理解「层次化 tiling」这一通用思想如何被 Nyuzi 落实到 16 通道 SIMD 上。
