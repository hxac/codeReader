# draw device 与 pixmap 位图渲染

## 1. 本讲目标

前两讲我们建立了两件事：`fz_device` 是一张「绘图指令」的虚表（u4-l1），显示列表能把指令「录下来、再放出来」（u4-l2）。但无论是 trace device 打印日志，还是 list device 录制节点，都还没有真正「画出像素」。

本讲就补上这最后一块拼图——**位图渲染后端 draw device**：它接收 `fill_path` / `fill_image` 等指令，把它们光栅化进一块连续内存 `fz_pixmap`，最终得到你在屏幕或 PNG 里看到的像素。

学完本讲你应当能够：

- 说清 `fz_new_draw_device` 是如何把一条矢量指令变成像素写进 pixmap 的；
- 画出 `fz_pixmap` 的内存布局，并解释 `samples` / `w` / `h` / `stride` / `n` 各字段的含义与相互关系；
- 理解颜色空间（Gray / RGB / CMYK）如何决定每个像素的通道数 `n`，从而决定 `stride` 与总内存大小。

## 2. 前置知识

- **光栅化（rasterization）**：把「用数学描述的图形」（一条路径、一段文字、一张图）转换成「离散像素网格」的过程。PDF 里一个矩形是 4 条直线方程，屏幕上却要变成一片 100×60 的彩色方块，中间的转换就是光栅化。
- **扫描线算法**：draw device 内部用一个 *rasterizer*（光栅化器）把多边形边界打散成「边」，再按行（扫描线）填充，判断每个像素中心是否落在多边形内，是则上色。
- **仿射变换与 ctm**：来自 u3-l3。页面默认是 72 dpi 的用户空间，要把坐标映射到设备像素就要乘一个 `fz_matrix`（即 ctm）。
- **引用计数 keep / drop**：来自 u2-l2。pixmap 是 `fz_storable` 派生对象，遵循「每个 new 配一个 drop」。
- **device 虚表与生命周期**：来自 u4-l1。device 的固定生命周期是 `new → run → close → drop`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/pixmap.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h) | `fz_pixmap` 结构体定义、构造函数、像素遍历/清除等 API 契约。 |
| [include/mupdf/fitz/device.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h) | `fz_new_draw_device*` 系列入口、`fz_draw_options` 选项结构。 |
| [source/fitz/draw-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c) | draw device 的全部实现：设备结构、虚表接线、`fill_path` / `fill_image` 回调、选项解析。 |
| [source/fitz/pixmap.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c) | pixmap 的构造、`n` 与 `stride` 的计算、`fz_clear_pixmap*` 清屏实现。 |
| [source/tools/mudraw.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c) | 命令行渲染工具，展示了「清屏 → 建 device → run_page → close → drop」的标准用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 draw device：把矢量指令光栅化进 pixmap

#### 4.1.1 概念说明

draw device 是 `fz_device` 的一个具体派生实现。回顾 u4-l1 的比喻：device 是「访客」，格式解释器（PDF/XPS/…）是「生产者」。trace device 把指令打印成日志，list device 把指令存进列表，而 **draw device 把指令「画」出来**——画到一块由调用方提供的 `fz_pixmap` 上。

所以 draw device 的本质是：**一台连接到「画布 pixmap」的光栅化机器**。它接到 `fill_path`（填充路径）就把多边形扫描线填充进 pixmap；接到 `fill_image`（贴图）就把图像像素按变换矩阵缩放、合成进 pixmap。

关键点：draw device **不拥有** pixmap，pixmap 是调用方传进来的（[device.h:619](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L619) 的文档明确写了 "Target pixmap ... The pixmap is not cleared by the draw device"）。这意味着两件事：① 你要自己负责清屏；② 你要自己负责 pixmap 的 keep/drop。

#### 4.1.2 核心流程

draw device 内部维护一个**状态栈**（stack），栈的每一层是 `fz_draw_state`，记录「当前要往哪块 pixmap 上画」（`dest`）、当前裁剪框（`scissor`）、当前混合模式（`blendmode`）等。裁剪/蒙版/透明组每嵌套一层，栈就 push 一次（可能换一块临时 pixmap），结束后 pop 回来。这与 u4-l1 讲的「容器栈」是对应的。

一条 `fill_path` 指令的典型光栅化流程：

```text
fill_path(path, in_ctm, color, alpha) 到达 draw device
   │
   1. ctm = concat(in_ctm, dev->transform)   ← 设备变换与本次变换相乘
   2. 算出在当前像素分辨率下的「容差」flatness
   3. bbox = 当前 dest 的矩形 ∩ 当前 scissor    ← 只光栅化可见区域
   4. flatten_fill_path: 把路径曲线展平成边，喂给 rasterizer
   5. resolve_color: 把浮点颜色 + alpha 换算成 0~255 的字节颜色向量 colorbv
   6. fz_convert_rasterizer: 扫描线填充，把 colorbv 写进 dest 的像素
```

注意第 1 步：draw device 会把「建设备时传入的变换 `dev->transform`」和「每次调用传入的 `in_ctm`」**相乘**。这就解释了一个常见困惑——为什么有人把缩放矩阵传给 `fz_new_draw_device`，有人传给 `fz_run_page`？因为两者最终都会被 concat，**放哪儿都等价**（矩阵乘法满足结合律）。mudraw 选择把 `fz_identity` 传给 device、把真正的 ctm 传给 `run_page`，而 `fz_new_draw_device_with_options` 反过来把缩放放进 device、回放时用 identity。

#### 4.1.3 源码精读

**设备结构**——`fz_draw_device` 派生自 `fz_device`，第一成员 `super` 就是基类（C 的多态手法，见 u3-l1）：

[draw-device.c:70-87](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L70-L87) 定义了 draw device 自身：`transform`（设备变换矩阵）、`rast`（光栅化器）、`default_cs`/`proof_cs`（默认/校样颜色空间）、`top`（状态栈深度）、`stack`（状态数组）以及 `cache_x`/`cache_y`（图像缩放缓存）。

**状态结构**——栈的每一层：

[draw-device.c:52-68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L52-L68) 中 `fz_draw_state` 的 `dest` 就是「当前画布」，`scissor` 是当前裁剪框，`mask`/`shape`/`group_alpha` 在透明度/蒙版处理时使用。

**建设备**——5 个公开的 `fz_new_draw_device*` 函数都委托给内部 `new_draw_device`：

[draw-device.c:3273-3295](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3273-L3295) 是 5 个薄包装，区别只在是否带 `clip`（裁剪框）和 `proof_cs`（校样色空间）。

[draw-device.c:3165-3271](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3165-L3271) 是真正的构造函数。其中 [3170-3200 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3170-L3200) 把全部绘图回调接到 `fz_draw_*` 实现上（这就是「填虚表」）；[3209-3217 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3209-L3217) 把传入的 pixmap 挂到栈底 `stack[0].dest`，并把初始 scissor 设成 pixmap 的外接矩形——**整个设备从一开始就知道「画布是哪块内存、有多大」**。

**填充路径**——光栅化的代表：

[draw-device.c:702-761](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L702-L761) `fz_draw_fill_path_aux`。第 [706 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L706) `ctm = fz_concat(in_ctm, dev->transform)` 就是上面说的「两个变换相乘」；第 [729-730 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L729-L730) 用「dest 矩形 ∩ scissor」收窄到可见区，再 `fz_flatten_fill_path` 把路径展平成边；第 [739 行](https://github.com/ArtifexSoftware-mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L739) `resolve_color` 算出字节颜色；第 [741 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L741) `fz_convert_rasterizer` 才真正执行扫描线填充，把像素写进 `state->dest`。

**贴图**——`fill_image` 走的是「解码→色彩转换→缩放→合成」：

[draw-device.c:1862-1980](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1862-L1980) `fz_draw_fill_image`，最后由 [1971 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L1971) 的 `fz_paint_image` 把处理好的图像像素合成进 `state->dest`。

#### 4.1.4 代码实践

**实践目标**：把 mudraw.c 的渲染主循环当成「活教材」，对照源码确认 draw device 的标准用法四步：清屏 → 建设备 → 驱动 → 关闭。

**操作步骤**：

1. 打开 [mudraw.c:660-682](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L660-L682)。
2. 逐行对应：第 [662-665 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L662-L665) 按「是否有 alpha」选择清屏方式（有 alpha 清成全 0 透明，无 alpha 清成 255 白底）；第 [669 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L669) 用 `fz_identity` 建设备（缩放交给 run_page 的 ctm）；第 [679 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L679) `fz_run_page` 驱动设备；第 [680-681 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L680-L681) close 后 drop。
3. 运行 `./build/debug/mutool draw -o out.png -r 100 some.pdf`，确认能生成 PNG（即 draw device 成功写进了 pixmap）。

**需要观察的现象**：渲染出的 PNG 内容正确；如果删掉第 662-665 行的清屏（脑内推演即可，不要改源码），无 alpha 时 pixmap 是**未初始化内存**，背景会出现随机脏像素——这正说明「draw device 不会替你清屏」。

**预期结果**：理解「device 不拥有、不清空 pixmap，调用方全权负责」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mudraw 在建设备时传 `fz_identity`，却把缩放矩阵传给 `fz_run_page`？

**答案**：因为 draw device 会在每次回调里做 `fz_concat(in_ctm, dev->transform)`，设备变换与本次变换相乘。把缩放放在哪一侧都行，mudraw 只是选择把缩放放进 run_page 的 ctm、设备侧用单位矩阵；矩阵乘法满足结合律，两种放法等价。

**练习 2**：如果渲染后忘记 `fz_close_device` 直接 `fz_drop_device`，会发生什么？（提示：回顾 u4-l1 的生命周期四步。）

**答案**：close 负责冲刷缓冲输出，drop 只减引用计数。跳过 close 会导致 device 内部尚未落盘/尚未完成的光栅化结果不完整；u4-l1 提到这会触发 `dropping unclosed device` 警告。正确顺序是 `close` 之后再 `drop`。

---

### 4.2 fz_pixmap 的内存布局：samples / stride / w / h / n

#### 4.2.1 概念说明

`fz_pixmap` 是一块**连续的、按行排列的字节内存**，外加描述它的元数据。你可以把它想象成一个二维像素数组，但实际存储是一维的字节数组。理解它的内存布局，是你能「手动读像素、写像素、算亮度、做后处理」的前提。

pixmap 的核心字段（[pixmap.h:431-445](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L431-L445)）：

| 字段 | 含义 |
| --- | --- |
| `x, y` | 这块像素区域在「大画布」里的左上角像素坐标（可为负，用于分块渲染） |
| `w, h` | 宽、高（像素数） |
| `n` | 每像素**字节数/通道数**（见 4.3） |
| `s` | 专色（spot）通道数，印刷用，通常为 0 |
| `alpha` | 是否有 alpha 通道（0 或 1） |
| `stride` | **一行像素占多少字节**，从本行某像素到下一行同一像素的字节偏移 |
| `samples` | 指向第一个像素首字节的指针 |
| `colorspace` | 颜色空间对象；为 NULL 表示这是纯 alpha 蒙版 |
| `xres, yres` | 分辨率（dpi），默认 96 |

#### 4.2.2 核心流程

像素在内存中的排列是**行优先（row-major）**，每个像素连续占 `n` 个字节。设第 `y` 行第 `x` 列像素的首字节地址为 `p`，则：

\[
p = \text{samples} + y \times \text{stride} + x \times n
\]

该像素的 `n` 个分量依次存放在 `p[0], p[1], …, p[n-1]`。分量的**顺序固定为**：过程色（如 R,G,B）、专色（spots）、alpha（[pixmap.h:34-39](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L34-L39) 与 [386-399](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L386-L399) 的注释）。例如无 alpha 的 RGB：`n=3`，`p[0]=R, p[1]=G, p[2]=B`。

通道数 `n` 由三部分相加（[pixmap.c:72](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L72)）：

\[
n = \text{colorspace\_n} + s + \text{alpha}
\]

而 `stride` 在默认构造时就是 `n * w`（[pixmap.c:139](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L139)），所以：

\[
\text{总内存} \approx \text{stride} \times h = n \times w \times h \quad \text{（字节）}
\]

> 小贴士：`stride` 允许大于 `n*w`（行尾可留 padding），也允许为负（实现「自底向上」的位图）。所以遍历像素**务必用 `stride` 而不是 `n*w`** 来换行，否则遇到带 padding 或反向行序的 pixmap 就会错位。

#### 4.2.3 源码精读

**结构体**：[pixmap.h:431-445](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L431-L445) 定义了 `struct fz_pixmap`。注意首成员是 `fz_storable storable`（来自 u2-l2 的引用计数基类），所以 pixmap 能被 keep/drop，也能进 `fz_store` 缓存。

**构造与 `n`/`stride` 的计算**：所有构造函数最终都进入 [pixmap.c:62-121](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L62-L121) 的 `fz_new_pixmap_with_data`。其中第 [72 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L72) 算 `n = alpha + s + fz_colorspace_n(...)`，第 [73-74 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L73-L74) 校验 `stride` 合法性，第 [114 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L114) 申请 `h * stride` 字节。而无数据版的 `fz_new_pixmap` 在 [131-140 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L131-L140) 用 `stride = n * w` 调用它——这就是默认「紧致无 padding」布局的来源。

**清屏**：draw device 不清屏，要靠 `fz_clear_pixmap*`。[pixmap.h:293](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L293) 的 `fz_clear_pixmap` 把所有分量（含 alpha）清成 0（全透明黑）；[pixmap.h:267](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L267) 的 `fz_clear_pixmap_with_value(pix, 255)` 把颜色分量清成 255、但 alpha **固定**清成 255（即不透明白底）。这正是 mudraw 第 662-665 行二选一的依据。

#### 4.2.4 代码实践

**实践目标**：手动遍历 `pix->samples`，按 `stride`/`n` 读取每个像素，计算并打印整张图的平均亮度，从而亲手验证内存布局。

下面是**示例代码**（非项目原有代码，仅演示用法；为简洁省略了错误处理与 fz_try/fz_catch，实际工程中应补上）：

```c
/* 示例代码：计算一个 RGB（无 alpha）pixmap 的平均亮度 */
#include "mupdf/fitz.h"

void print_average_luminance(fz_context *ctx, fz_pixmap *pix)
{
    /* 仅演示：假设是 RGB、n==3、无 padding（stride == n*w） */
    int w = pix->w, h = pix->h, n = pix->n;
    int stride = pix->stride;            /* 用 stride 换行，不要用 n*w */
    unsigned char *s = pix->samples;
    double sum = 0;
    int x, y;

    for (y = 0; y < h; y++)
    {
        unsigned char *row = s + y * stride;   /* 第 y 行起点 */
        for (x = 0; x < w; x++)
        {
            unsigned char *px = row + x * n;   /* (x,y) 像素首字节 */
            double r = px[0], g = px[1], b = px[2];
            /* ITU-R BT.601 亮度公式 */
            sum += 0.299 * r + 0.587 * g + 0.114 * b;
        }
    }
    printf("平均亮度 = %.2f (0~255)\n", sum / (w * h));
}
```

**操作步骤**：

1. 按 u1-l5 的 example.c 模板建好 context、打开文档、取第一页。
2. 用 `fz_new_pixmap_with_bbox(ctx, fz_device_rgb(ctx), bbox, NULL, 0)` 建一个无 alpha 的 RGB pixmap（`n=3`）。
3. `fz_clear_pixmap_with_value(ctx, pix, 255)` 清成白底。
4. 建 draw device：`fz_new_draw_device(ctx, ctm, pix)`，`fz_run_page` 驱动，`fz_close_device` 后 `fz_drop_device`。
5. 调用上面的 `print_average_luminance(ctx, pix)`。
6. 最后 `fz_drop_pixmap(ctx, pix)`（pixmap 是你建的，必须由你 drop）。

**需要观察的现象**：渲染一页「白底黑字」的文档，平均亮度应接近 255（大部分是白）；渲染一页深色背景的文档，平均亮度应明显偏低。

**预期结果**：你能正确用 `stride` 和 `n` 解析出每个像素，说明你掌握了 pixmap 的内存模型。

> 待本地验证：具体的平均亮度数值取决于你选的 PDF，本讲不预设具体数字。

#### 4.2.5 小练习与答案

**练习 1**：一个 `w=800, h=600`、RGB、带 alpha 的 pixmap，`samples` 缓冲区至少多大？

**答案**：`n = 3（RGB）+ 0（spot）+ 1（alpha）= 4`；`stride = n*w = 4*800 = 3200`；总内存 `= stride*h = 3200*600 = 1,920,000` 字节（约 1.83 MiB）。

**练习 2**：为什么遍历像素时用 `y * stride` 而不是 `y * n * w`？

**答案**：`stride` 才是一行的真实字节数，它可能因 padding 大于 `n*w`，也可能为负（反向行序）。用 `n*w` 假设了「紧致无 padding」，遇到非默认布局就会错位。`stride` 是布局的唯一真相。

---

### 4.3 颜色空间与通道

#### 4.3.1 概念说明

同一个页面，可以渲染成灰度图（省内存、省带宽），也可以渲染成 RGB（屏幕显示）或 CMYK（印刷）。**颜色空间决定了 `n`**，而 `n` 又决定了 `stride` 和总内存。所以「选颜色空间」本质是在选「每个像素几个字节」。

MuPDF 内置三种设备颜色空间，分别对应不同的通道数：

| 颜色空间 | `colorspace_n` | 含义 |
| --- | --- | --- |
| `fz_device_gray` | 1 | 灰度，每像素 1 个亮度字节 |
| `fz_device_rgb` | 3 | 红/绿/蓝，每像素 3 字节 |
| `fz_device_cmyk` | 4 | 青/品红/黄/黑，每像素 4 字节（印刷四色） |

draw device 在渲染时会按需把内容里的颜色**转换**到目标 pixmap 的颜色空间：比如页面里有张 CMYK 的图，但你渲染成 RGB pixmap，draw device 会自动把 CMYK 解码并转换成 RGB 再合成（见 4.1.3 `fz_draw_fill_image` 里的 `convert_pixmap_for_painting`）。

#### 4.3.2 核心流程

`n` 的推导链：

```text
选定 colorspace（gray=1 / rgb=3 / cmyk=4）
   │
   n = colorspace_n + s(通常0) + alpha(0或1)
   │
   stride = n * w  （默认构造）
   │
   总内存 = stride * h
```

于是颜色空间 + 是否 alpha 共同决定 `n`：

| 配置 | n | 说明 |
| --- | --- | --- |
| Gray，无 alpha | 1 | 最省内存，纯灰度 |
| Gray，有 alpha | 2 | 灰度 + 透明 |
| RGB，无 alpha | 3 | 屏幕常用 |
| RGB，有 alpha | 4 | 带透明背景 |
| CMYK，无 alpha | 5* | 见注 |

> 注：CMYK 文档常带专色（spot）通道用于印刷分色，`s>0` 时 `n` 还要再加。本讲聚焦常见屏幕渲染场景，专色留待印刷相关讲义。

#### 4.3.3 源码精读

**选项结构**：[device.h:681-692](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L681-L692) 的 `fz_draw_options` 把分辨率、旋转、`colorspace`、`alpha`、抗锯齿等打包在一起，`fz_new_draw_device_with_options`（[device.h:725](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L725)）一次性算好矩阵、建好 pixmap、接好 device。

**默认值**：[draw-device.c:3343-3357](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3343-L3357) `fz_init_draw_options` 把默认颜色空间设成 `fz_device_rgb`、默认 96 dpi、无 alpha。

**选项字符串映射**：[draw-device.c:3378-3386](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3378-L3386) 把字符串 `"gray"→1`、`"rgb"→3`、`"cmyk"→4` 映射成通道数；[3416-3432 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3416-L3432) 据此把 `opts->colorspace` 指向对应的设备颜色空间对象。这正是命令行 `-O colorspace=gray` 之类选项的落点。

**一键建 pixmap+device**：[draw-device.c:3454-3512](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3454-L3512) `fz_new_draw_device_with_options`。第 [3458-3459 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3458-L3459) 由分辨率算缩放（`resolution/72`，因为页面默认 72 dpi）；第 [3491 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3491) `fz_pre_rotate(fz_scale(...), rotate)` 组成变换矩阵（矩阵组合的细节见 u3-l3）；第 [3494 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3494) 用 `fz_new_pixmap_with_bbox` 建 pixmap——**这里传入的 `opts->colorspace` 就决定了 `n`**；第 [3498-3501 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c#L3498-L3501) 按「是否有 alpha」选择清屏方式（与 mudraw 一致）。

#### 4.3.4 代码实践

**实践目标**：用同一份选项、同一页文档，分别渲染成 Gray 和 RGB，对比 `n`、`stride` 与内存占用，直观体会颜色空间对通道数的影响。

**操作步骤**：

1. 用 `fz_init_draw_options` 初始化选项，得到默认（RGB）配置。
2. 调 `fz_new_draw_device_with_options(ctx, &opts, mediabox, &pix)` 渲染第一页。
3. 打印 `pix->n`（应为 3）、`pix->stride`（应为 `3*w`）、以及 `fz_pixmap_size(ctx, pix)`。
4. 把 `opts.colorspace = fz_device_gray(ctx)` 后重复 2~3 步，观察 `n` 变为 1、内存约为原来的 1/3。
5.（可选）设 `opts.alpha = 1`，观察 `n` 再 +1。

**需要观察的现象**：

| 配置 | n | 相对内存 |
| --- | --- | --- |
| Gray 无 alpha | 1 | 1× |
| RGB 无 alpha | 3 | 3× |
| RGB 有 alpha | 4 | 4× |

**预期结果**：确认 `n` 完全由 `colorspace_n + alpha` 决定，进而 `stride = n*w`、内存随 `n` 线性增长。

> 待本地验证：绝对内存取决于页面尺寸与分辨率，上表只反映相对关系。

#### 4.3.5 小练习与答案

**练习 1**：为什么屏幕渲染通常选 RGB，而印刷流程要选 CMYK？

**答案**：屏幕发光器件是 RGB 加色模型，直接对应像素的 R/G/B 三个子像素；而印刷是油墨减色模型，用 CMYK 四色油墨叠加，必须在 CMYK 空间分色才能正确出片。选错空间会导致颜色转换损失或不匹配。

**练习 2**：把 `alpha` 从 0 改成 1，`n` 怎么变？为什么文档背景会从「不透明白底」变成「透明」？

**答案**：`n` 加 1（多一个 alpha 字节）。无 alpha 时用 `fz_clear_pixmap_with_value(pix, 255)` 把背景清成不透明白（alpha 强制 255）；有 alpha 时用 `fz_clear_pixmap(pix)` 把背景清成全 0，即 alpha=0 的完全透明，于是页面没画到的区域就是透明的——便于把渲染结果叠加到别的背景上。

---

## 5. 综合实践

把三个模块串起来，写一个「亮度统计器」小程序：

1. 用 `fz_draw_options` 配置：`colorspace = fz_device_gray(ctx)`、`x_resolution = y_resolution = 150`、`alpha = 0`。
2. 调 `fz_new_draw_device_with_options` 一步拿到 device 与 pixmap，省去手动算矩阵和建 pixmap。
3. `fz_run_page` 驱动设备（注意此时缩放已在 device 的 `transform` 里，所以 run_page 传 `fz_identity`），close 后 drop device。
4. 因为是 Gray、`n=1`，直接遍历 `samples`（按 `stride` 换行）累加每个字节值，除以 `w*h` 得到平均亮度；再统计有多少比例的像素亮度低于 128（即「偏暗像素占比」）。
5. 对比：用同样的页改成 `colorspace = fz_device_rgb(ctx)` 再渲染一次，验证 Gray 版的亮度与你用 BT.601 公式从 RGB 版手算出的亮度是否吻合（理论上应非常接近）。

这个练习同时覆盖了 draw device 光栅化（模块一）、按 stride/n 遍历内存（模块二）、颜色空间对 n 的影响（模块三）。

> 提示：Gray 版 `n=1`，亮度就是像素值本身；RGB 版需用 `0.299R+0.587G+0.114B`。两者都依赖你对 `stride`/`n` 的正确理解。

## 6. 本讲小结

- draw device 是 `fz_device` 的位图后端，它接到 `fill_path`/`fill_image` 等指令后，用 rasterizer 扫描线填充，把像素写进调用方提供的 `fz_pixmap`；它**不拥有也不清空** pixmap。
- draw device 内部维护一个状态栈，每层 `fz_draw_state` 记录当前画布 `dest`、裁剪框 `scissor`、混合模式等；裁剪/蒙版/透明组会 push/pop 栈。
- 设备变换 `dev->transform` 与每次调用的 `in_ctm` 会被 `fz_concat` 相乘，所以缩放矩阵放在 device 还是 run_page 都等价。
- `fz_pixmap` 是连续行优先字节内存：`n = colorspace_n + s + alpha`，`stride` 是一行字节数（默认 `n*w`），像素 `(x,y)` 首字节位于 `samples + y*stride + x*n`，分量顺序固定为「过程色→专色→alpha」。
- 颜色空间决定 `n`：Gray=1、RGB=3、CMYK=4，再加 alpha；`n` 进而决定 `stride` 与总内存，渲染时 draw device 会自动把内容颜色转换到目标空间。
- 标准用法四步：清屏 → `fz_new_draw_device` → `fz_run_page` → `fz_close_device` + `fz_drop_device`，pixmap 由调用方 keep/drop。

## 7. 下一步学习建议

- **下一讲 u4-l4（mudraw 渲染管线）**：本讲只画了「单页单 device」，mudraw 会把 context、document、display-list、draw device、cookie、分带渲染串成工程级管线，是 draw device 的集大成应用，建议紧接着学。
- **继续阅读源码**：想深入光栅化内部，可读 `source/fitz/draw-device.c` 的 `fz_draw_begin_group`/`fz_draw_end_group`（透明组与混合模式）以及 rasterizer 相关实现；想看 pixmap 的更多变换可读 `source/fitz/pixmap.c` 的 `fz_scale_pixmap`、`fz_invert_pixmap`。
- **承上启下**：draw device 是「写像素」的代表，后续 u5（字体/文本/搜索）会讲到 stext device 这个「抽文字」的代表，对比两者能加深对 u4-l1「同一套指令、不同 device」抽象的理解。
