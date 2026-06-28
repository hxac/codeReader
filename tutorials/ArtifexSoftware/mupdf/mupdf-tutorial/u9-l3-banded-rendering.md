# 分带渲染与渐进输出

## 1. 本讲目标

本讲承接 u4-l4（mudraw 渲染管线）与 u9-l2（多线程渲染），回答一个工程问题：**当一页要被光栅成上亿像素的大位图时，如何避免一次性把整页 pixmap 全部分配进内存？**

学完本讲你应该能够：

- 说清楚「分带（banded）渲染」是什么、为什么能降低内存峰值；
- 理解 `fz_band_writer` 这套抽象如何把一张大图拆成若干条带，逐条流式写入输出文件；
- 在 `mudraw.c` 中定位「按 band 分配 pixmap、逐 band 渲染、逐 band 输出」的关键代码段，并掌握 `-B`（band 高度）与 `-T`（渲染线程）两个开关的配合规则。

---

## 2. 前置知识

本讲假设你已经掌握以下内容（对应前置讲义）：

- **pixmap 的内存布局**（u4-l3）：一张 `fz_pixmap` 的总字节量约为 `stride * h`，而 `stride ≈ w * n`。分辨率越高，`w` 与 `h` 越大，内存随面积线性增长。
- **draw device 与显示列表**（u4-l2 / u4-l3）：`fz_new_draw_device` 把矢量指令光栅化进 pixmap；`fz_display_list` 是「录制一次、多次回放」的自包含指令流。
- **多线程渲染模型**（u9-l2）：`fz_document` / `fz_page` 只能主线程访问，跨线程传递页面内容必须借助自包含的 `fz_display_list`，工作线程各自 `fz_clone_context`。

一条贯穿全讲的直觉：

> 整页渲染 = 先把整张 `w × H` 的大 pixmap 分配出来，再一次性填满。
> 分带渲染 = 只分配一张 `w × B`（`B ≪ H`）的小 pixmap，从上到下渲染若干条带，每渲染完一条就立刻输出、立刻复用同一块内存。

因此分带的本质是**用「时间上的串行复用」换「空间上的峰值降低」**。如果再把每条带交给不同工作线程并行渲染，就能在控制内存的同时拿回吞吐量——这正是 mudraw 的工业级做法。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/band-writer.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/band-writer.h) | band-writer 的公共接口与虚表结构定义：`fz_write_header` / `fz_write_band` / `fz_close_band_writer` / `fz_drop_band_writer`。 |
| [source/fitz/output.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c) | band-writer 的「骨架」实现：上面四个函数的通用调度逻辑，负责维护累计行号 `line` 与 trailer 自动收尾。 |
| [source/fitz/output-pnm.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pnm.c) | PNM/PPM/PGM band-writer，最简单的「逐行直写」范例。 |
| [source/fitz/output-png.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-png.c) | PNG band-writer，演示「跨 band 增量压缩」的典型——zlib 流在多条带间持续 `deflate`。 |
| [source/tools/mudraw.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c) | 命令行渲染工具，分带渲染的集大成者：`-B` 切分带、`drawband` 渲染单条带、`worker_thread` 多线程渲染条带、band-writer 逐带输出。 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块按「为什么分带 → 输出侧如何接住条带 → mudraw 如何把它们串起来」的顺序展开。

### 4.1 分带渲染策略

#### 4.1.1 概念说明

「分带（banding）」是把一张高 `H` 像素的页面位图，沿垂直方向切成 `N` 条高 `B` 像素的水平条带（band），每次只渲染一条带。

为什么需要它？因为整页 pixmap 的内存随面积增长：

\[
M_{\text{整页}} = w \cdot n \cdot H \cdot (\text{每像素字节数})
\]

其中 `n` 是每像素分量数（Gray=1、RGB=3、CMYK=4，另可加 alpha 与专色）。一张 A4、300dpi、RGB 的页面约有 `2480 × 3508` 像素，`n=3`，仅 pixmap 一项就要约 26 MB；若升到 600dpi 或 CMYK+专色，轻易突破上百 MB。对于打印机驱动、嵌入式设备、超大画幅输出，这种峰值不可接受。

分带后峰值降为：

\[
M_{\text{分带}} \approx w \cdot n \cdot B \quad (B \ll H)
\]

只要 `B` 取得足够小（例如 64 或 128 行），无论页面多高，渲染期 pixmap 峰值都被钉死在一个常数级别。代价是渲染次数从 1 次变成 `N = \lceil H / B \rceil` 次，并且每条带都要重新执行一遍内容流（或回放一遍显示列表）。

> 关键直觉：分带降低的是**渲染期 pixmap 的峰值**，不是文档解析或显示列表录制开销。后者由 u4-l2 的显示列表一次性承担，且正是它让「反复回放给每条带」变得廉价。

#### 4.1.2 核心流程

mudraw 里一页的分带渲染主流程（伪代码）：

```text
totalheight = 页面变换后的整数像素高 H
if (band_height != 0):
    drawheight  = band_height               # pixmap 只分配 B 行
    bands       = ceil(totalheight / B)     # 条带数
    tbounds.y1  = tbounds.y0 + B + 2        # 渲染裁剪框（含 2 像素重叠余量）

fz_write_header(bander, w, totalheight, ...) # 告诉输出端：整页是 w×H

for band in 0 .. bands:
    pix->y = band * band_height              # 把本条带定位到整页中的正确纵坐标
    渲染 (display_list, ctm, tbounds) → pix  # 只渲染本条带那一段
    fz_write_band(bander, pix.stride, drawheight, pix.samples)  # 立刻输出
    tbounds.y0 += band_height                # 裁剪框下移到下一条带
    tbounds.y1 += band_height

fz_close_band_writer(bander)                 # 写 trailer 收尾
```

三个要点：

1. **pixmap 复用**：整页只分配一张 `w × B` 的小 pixmap，循环里通过修改 `pix->y`（条带在整页中的纵向偏移）和 `tbounds`（渲染裁剪框）来复用它。
2. **裁剪框 + 重叠余量**：`tbounds` 把回放范围限定在当前条带的纵向区间；`+2` 是一条小小的重叠余量，避免条带接缝处因裁剪过早而丢掉边缘的反走样碎片。
3. **band-writer 的契约**：输出端事先知道整页是 `w × H`，随后按顺序收到每一条带，自己负责拼装/编码。

#### 4.1.3 源码精读

`band_height` 是一个全局开关，`0` 表示不分带（[source/tools/mudraw.c:351](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L351)）：

```c
static int band_height = 0;
```

分带的几何切分在 `dodrawpage` 里完成（[source/tools/mudraw.c:1112](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1112)）：

```c
fz_irect band_ibounds = ibounds;
int band, bands = 1;
int totalheight = ibounds.y1 - ibounds.y0;
int drawheight = totalheight;

if (band_height != 0)
{
    /* Banded rendering; we'll only render to a given height at a time. */
    drawheight = band_height;
    if (totalheight > band_height)
        band_ibounds.y1 = band_ibounds.y0 + band_height;   // pixmap 只取 B 行高
    bands = (totalheight + band_height-1)/band_height;      // 条带数 = ceil(H/B)
    tbounds.y1 = tbounds.y0 + band_height + 2;              // 渲染裁剪框，+2 重叠余量
}
```

- `band_ibounds` 是 pixmap 的整数包围盒，被裁到 `band_height` 高，所以 `fz_new_pixmap_with_bbox` 分配出的 pixmap 只有 `B` 行。
- `drawheight` 记录「每次写入 band-writer 时声明的高度」——分带时是 `B`，不分带时是整页高 `H`。
- `tbounds.y1 = ... + band_height + 2` 中的 `+2` 即前述接缝重叠余量。

真正渲染「单条带」的工作由 `drawband` 完成（[source/tools/mudraw.c:652](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L652)）：

```c
static void drawband(fz_context *ctx, fz_page *page, fz_display_list *list,
        fz_matrix ctm, fz_rect tbounds, fz_cookie *cookie,
        int band_start, fz_pixmap *pix, fz_bitmap **bit)
{
    ...
    dev = fz_new_draw_device_with_proof(ctx, fz_identity, pix, proof_cs);
    ...
    if (list)
        fz_run_display_list(ctx, list, dev, ctm, tbounds, cookie);  // 有 list：回放（多线程路径）
    else
        fz_run_page(ctx, page, dev, ctm, cookie);                   // 无 list：直接跑页面
    fz_close_device(ctx, dev);
    ...
    if (mono 输出格式)
        *bit = fz_new_bitmap_from_pixmap_band(ctx, pix, NULL, band_start);  // 单色：转位图条带
}
```

读这段可以得出三件事：

1. `fz_new_draw_device_with_proof` 用 `fz_identity` 建设备（[source/tools/mudraw.c:669](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L669)），真正的页面→设备变换 `ctm` 与裁剪框 `tbounds` 交给 `fz_run_display_list`（这与 u4-l3「缩放矩阵放 device 还是 run_page 都等价」一致）。
2. `band_start` 参数 = 本条带在整页中的起始行号（`band * band_height`），它用于把单色 pixmap 转成对应位置的位图条带。
3. 分带与不分带走的是**同一个** `drawband`：不分带时 `bands=1`、`band_height` 实为整页高，等于「整页当一条带」渲染一次。这条统一代码路径是 mudraw 设计上的简洁之处。

#### 4.1.4 代码实践

**实践目标**：用大分辨率渲染同一页，对比「整页渲染」与「分带渲染」的内存峰值。

**操作步骤**（先编译，见 u1-l2，例如 `make build=debug`）：

1. 整页渲染（不分带）。用一个多页 PDF 的某一页，强制 300dpi：
   ```bash
   ./build/debug/mutool draw -o full.ppm -r 300 input.pdf 1
   ```
2. 分带渲染，把 band 高度限制为 64 行：
   ```bash
   ./build/debug/mutool draw -o banded.ppm -r 300 -B 64 -T 1 input.pdf 1
   ```
   注意 `-B` 在 mudraw 里必须搭配 `-T`（见 4.3.3 的校验逻辑），`-T 1` 表示单线程分带。

**需要观察的现象**：

- 两次输出的 `full.ppm` 与 `banded.ppm` 文件大小应**相等**（同样的 `w×H×n` 字节，PPM 是未压缩裸数据），用 `ls -l` 或 `wc -c` 对比即可验证分带没有改变最终图像。
- 若想看内存峰值差异，可改用调试分配器：mudraw 自带 `-m`（lowmemory 模式，会关掉设备缓存）并不能直接量内存。更直接的办法是用系统工具（Linux 下 `/usr/bin/time -v` 看 `Maximum resident set size`，或 `valgrind --tool=massif`）分别跑两条命令，比较 RSS 峰值。

**预期结果**：分带版本的 pixmap 峰值约为整页版本的 `B / H`（例如 `64 / 3508 ≈ 1.8%`），但显示列表等共享开销不变，故总 RSS 下降幅度小于理论值，方向一定向下。

> 待本地验证：具体 RSS 数值依赖机器与系统 malloc 实现，本讲不臆测具体数字。

#### 4.1.5 小练习与答案

**练习 1**：把 `-B` 设成大于整页高度（如 `-B 100000`）会怎样？

**参考答案**：`bands = ceil(totalheight / band_height) = 1`，`band_ibounds.y1` 不被缩小（因为 `totalheight > band_height` 不成立），等价于整页渲染——分带退化成「一条带 = 整页」，逻辑仍然正确，只是没有省内存。

**练习 2**：为什么 `tbounds.y1` 要写成 `+ band_height + 2` 而不是恰好 `+ band_height`？

**参考答案**：那 2 像素是重叠余量。条带边界上的图形（尤其是反走样后的半透明边缘碎片）若被裁剪框过早切断，会在拼接处出现细缝；多渲染 2 行可以让边界碎片落进当前条带，消除可见接缝。pixmap 仍只有 `B` 行高，多出来的渲染会落在 pixmap 包围盒之外被自然丢弃，不会越界。

---

### 4.2 band-writer 流式输出

#### 4.2.1 概念说明

分带渲染产出的不是一张完整 pixmap，而是一连串「条带像素」。如何把它们写进一个合法的输出文件？这正是 `fz_band_writer` 要解决的问题。

`fz_band_writer` 是一个**面向条带的输出抽象**：调用方先告诉它整页尺寸，再一条一条喂数据，它负责把条带拼装/编码成目标格式（PNG / PNM / PS / PSD / PCL / PWG …）。

它与 u6-l1 的 `fz_document_writer` 是不同层次的东西：`fz_document_writer` 管的是「多页 + 文档框架」（begin/end_page 返回一个 device），而 `fz_band_writer` 管的是「一张位图如何分条带落盘」。事实上 mudraw 走的是更底层的 band-writer 路径，而 muconvert 走的是 document-writer 路径（见 u6-l2 / u6-l3）。

band-writer 的虚表骨架（[include/mupdf/fitz/band-writer.h:93](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/band-writer.h#L93)）：

```c
struct fz_band_writer
{
    fz_drop_band_writer_fn *drop;
    fz_close_band_writer_fn *close;
    fz_write_header_fn     *header;    // 写文件头（整页尺寸、色彩空间等）
    fz_write_band_fn       *band;      // 写一条带
    fz_write_trailer_fn    *trailer;   // 收尾（如 PNG 的 IEND）
    fz_output *out;                    // 底层字节输出
    int w, h, n, s, alpha, xres, yres, pagenum;
    int line;                          // 已经写入的累计行号
    fz_separations *seps;
};
```

注意 `line` 字段：它记录「整页里我已经写了多少行」，是分带输出的状态机核心。

#### 4.2.2 核心流程

band-writer 的标准三段式调用契约：

```text
fz_write_header(w, w, H, n, alpha, xres, yres, pagenum, cs, seps)   # 1. 写头，登记整页尺寸
  └─ 内部调 w->header()，把 w/h/n 等缓存进结构体
for 每条带:
    fz_write_band(w, stride, band_height, samples)                   # 2. 喂一条带
      └─ 内部调 w->band(stride, band_start=累计line, band_height, samples)
      └─ w->line += band_height
      └─ 若 line == H：自动调 w->trailer() 收尾
fz_close_band_writer(w)                                              # 3. 关闭
fz_drop_band_writer(w)                                               # 4. 释放
```

两个由骨架统一保证的不变量（在 [source/fitz/output.c:789](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c#L789) 的 `fz_write_band` 里）：

1. **末条带自动截断**：`if (writer->line + band_height > writer->h) band_height = writer->h - writer->line;`——最后一条带可能不足 `B` 行，骨架按整页高 `H` 自动钳制，调用方无需自己算末条带高度。
2. **满页自动收尾**：当 `line == h` 时自动调用 `trailer`，并把 `line++` 锁死，防止后续空 band 重复触发。

#### 4.2.3 源码精读

骨架函数 `fz_write_header` 把整页尺寸缓存进结构体再调具体格式的 `header` 回调（[source/fitz/output.c:768](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c#L768)）：

```c
void fz_write_header(fz_context *ctx, fz_band_writer *writer, int w, int h, int n,
        int alpha, int xres, int yres, int pagenum, fz_colorspace *cs, fz_separations *seps)
{
    ...
    writer->w = w; writer->h = h; writer->n = n;   // 注意：h 是整页高 H，不是条带高
    writer->line = 0;
    writer->seps = fz_keep_separations(ctx, seps);
    writer->header(ctx, writer, cs);
}
```

骨架函数 `fz_write_band` 维护累计行号并自动收尾（[source/fitz/output.c:789](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c#L789)）：

```c
void fz_write_band(fz_context *ctx, fz_band_writer *writer, int stride, int band_height, const unsigned char *samples)
{
    ...
    if (writer->line + band_height > writer->h)
        band_height = writer->h - writer->line;          // 末条带钳制
    if (band_height > 0) {
        writer->band(ctx, writer, stride, writer->line, band_height, samples);  // band_start = writer->line
        writer->line += band_height;
    }
    if (writer->line == writer->h && writer->trailer) {
        writer->trailer(ctx, writer);                    // 满页自动收尾
        writer->line++;                                  // 锁死，防重复
    }
}
```

具体格式只需填 `header` / `band` / `trailer` / `drop` 四个回调。最简单的是 PNM——它逐行直写，条带之间无状态（[source/fitz/output-pnm.c:59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pnm.c#L59)）：

```c
static void pnm_write_band(fz_context *ctx, fz_band_writer *writer,
        int stride, int band_start, int band_height, const unsigned char *p)
{
    ...
    int end = band_start + band_height;
    ...
    while (end--) {                 // 逐行
        len = w;
        while (len) { fz_write_data(ctx, out, p, ...); p += ...; }
        p += stride - w*n;          // 跳到下一行（处理 stride 中的 padding）
    }
}
```

PNM 不关心 `band_start`，因为 PNM 文件体就是连续像素流，第几条带都一样直写。它的 `header` 只写文件头 `P5/P6\n w h\n 255\n`（[source/fitz/output-pnm.c:29](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pnm.c#L29)），没有 `trailer`。

真正体现「跨条带状态」的是 PNG。PNG 的像素体要经 zlib 压缩，而压缩流天然是**跨整页连续**的，不能按条带切片独立压缩。PNG band-writer 的做法是：第一条带来时才惰性 `deflateInit`，此后每条带都把行数据喂进**同一个** `z_stream`，只有最后一条带才用 `Z_FINISH` 收口（[source/fitz/output-png.c:207](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-png.c#L207)）：

```c
finalband = (band_start+band_height >= h);     // 是否最后一条带
...
if (writer->udata == NULL) {                   // 第一条带：惰性初始化 zlib
    ...
    err = deflateInit(&writer->stream, Z_DEFAULT_COMPRESSION);
    ...
}
... // 把本条带每行前加一个 0（PNG 的 none 预测过滤器字节），拷进 udata 缓冲
err = deflate(&writer->stream,
        (finalband && remain == writer->stream.avail_in) ? Z_FINISH : Z_NO_FLUSH);
```

关键一行在 [source/fitz/output-png.c:304](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-png.c#L304)：中间条带用 `Z_NO_FLUSH`（压缩器可以跨条带保留上下文，压缩率不受分带影响），只有最后一条带用 `Z_FINISH`。收尾时 `trailer` 调 `deflateEnd` 并写 `IEND` chunk（[source/fitz/output-png.c:327](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-png.c#L327)）。

> 这正是 band-writer 抽象的价值：调用方（mudraw）永远只调 `fz_write_header` + N 次 `fz_write_band`，完全不用关心 PNM 是「无状态直写」还是 PNG 是「跨带增量压缩」——差异被吸收进各格式的回调实现里。

#### 4.2.4 代码实践

**实践目标**：用源码阅读理解 band-writer 的「统一契约 + 各格式差异」。

**操作步骤**：

1. 打开 [source/fitz/output-pnm.c:118](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pnm.c#L118) 的 `fz_write_pixmap_as_pnm`，它示范了「不分带」时如何用 band-writer：把整张 pixmap 当成**一条** band 喂进去：
   ```c
   fz_write_header(ctx, writer, pixmap->w, pixmap->h, pixmap->n, pixmap->alpha, 0, 0, 0, pixmap->colorspace, pixmap->seps);
   fz_write_band(ctx, writer, pixmap->stride, pixmap->h, pixmap->samples);   // 一条带 = 整页
   fz_close_band_writer(ctx, writer);
   ```
2. 对比 [source/fitz/output-png.c:207](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-png.c#L207) 的 `png_write_band`，确认它如何用 `finalband` 标志区分中间带与末带。

**需要观察的现象**：

- `fz_write_pixmap_as_pnm` 与 mudraw 的分带循环调的是**完全相同**的三个函数（`fz_write_header` / `fz_write_band` / `fz_close_band_writer`），区别只在「喂一次」还是「喂 N 次」。这说明不分带是分带的特例（`N=1`）。

**预期结果**：你能向别人讲清「为什么 PNG 分带输出和整页输出得到的 PNG 字节完全一致」——因为 zlib 流是连续的，`Z_NO_FLUSH` 不产生额外边界，最终 `deflate` 的输入字节流与不分带时逐字节相同。

#### 4.2.5 小练习与答案

**练习 1**：如果某个格式的 `trailer` 回调为 NULL（如 PNM），满页时会发生什么？

**参考答案**：[source/fitz/output.c:802](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c#L802) 的判断是 `if (writer->line == writer->h && writer->trailer)`，`trailer` 为 NULL 时条件不成立，什么都不做——PNM 本就没有文件尾，这是正确的。

**练习 2**：为什么 PNG 不能像 PNM 那样「每条带独立处理」？

**参考答案**：PNG 的像素体是**一个**跨整页的 zlib 压缩流（可能拆成多个 IDAT chunk，但逻辑上连续）。若每条带独立 `deflateInit/deflateEnd`，相当于把图像切成 N 段独立压缩，不仅压缩率下降，产出的也不是合法 PNG。所以 PNG band-writer 必须在多条带间维持同一个 `z_stream`，仅在末带 `Z_FINISH`。

---

### 4.3 mudraw 分带接入

#### 4.3.1 概念说明

前两模块讲了「分带策略」和「输出抽象」，本模块把它们在 mudraw 里拼成一条完整链路，并补上多线程这一维。

mudraw 对分带的接入有三个特点：

1. **`-B` 与 `-T` 强制配对**：在 mudraw 的命令行语义里，分带（`-B`）和多线程（`-T`）必须同时出现，单独用任一个都会被判定为「无意义」而报错退出。这是因为 mudraw 把分带定位成「为多线程并行服务的切分手段」。
2. **统一代码路径**：不分带时 `bands=1`，分带时 `bands=N`，渲染与输出共用同一段循环，无需维护两套逻辑。
3. **多线程 = 一条带一线程**：工作线程池里的每个线程负责渲染一条带，主线程负责按序回收并喂给 band-writer，保证输出顺序。

#### 4.3.2 核心流程

mudraw 单页分带渲染的完整时序（带多线程）：

```text
主线程:  数页 → 建 display_list（录制整页指令）
         建 band-writer，fz_write_header(整页 w×H)
         预触发前 min(num_workers, bands) 条带，各自 worker_thread 开始渲染
         for band in 0..bands:
             等 workers[band%N].stop   → 取回本条带 pixmap
             fz_write_band(band-writer, pix)      # 顺序写入，保证图像纵向正确
             若还有后续带：把它派给刚空闲的 worker 预渲染（流水线）
         fz_close_band_writer

工作线程: 等 start 信号 → drawband(自己的 ctx, list, ctm, tbounds, band*band_height, pix)
         → 触发 stop 信号
```

注意「顺序写入、并行渲染」的关键：多条带可以并行渲染，但 `fz_write_band` 必须**按 band 升序**调用，否则图像会纵向错位。主线程用一个轮转下标 `band % num_workers` 把带分配给工作线程，并严格按 `band` 递增顺序回收与写入。

#### 4.3.3 源码精读

`-B` / `-T` 的解析（[source/tools/mudraw.c:2167](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2167)）：

```c
case 'B': band_height = atoi(fz_optarg); break;          // L2167
...
case 'T': max_num_workers = atoi(fz_optarg); break;      // L2221（DISABLE_MUTHREADS 时被编译排除）
```

二者必须配对的校验在 [source/tools/mudraw.c:2302](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2302)：

```c
if (band_height < 0)  { fprintf(stderr, "band height must be > 0\n"); exit(1); }
if (band_height == 0) { fprintf(stderr, "multiple threads without banding is pointless\n"); exit(1); }
...
else if (band_height != 0) { fprintf(stderr, "banding without multiple threads is pointless\n"); exit(1); }
```

并且多线程还要求必须用显示列表（与 u9-l2 一致，[source/tools/mudraw.c:2296](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2296)）：`fz_page` 不可跨线程访问，只有自包含的 `fz_display_list` 能安全传给工作线程。

输出格式也受限：分带只支持 PxM / PCL / PCLM / PDFOCR / PS / PSD / PNG 等「可流式」格式（[source/tools/mudraw.c:2456](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2456)），且明确禁止 J2K 与分带同用（`Can't band with J2k output!`，[source/tools/mudraw.c:1219](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1219)）——因为 JPEG2000 的编码依赖整页小波变换，无法按条带增量。

按输出格式构造对应 band-writer（[source/tools/mudraw.c:1180](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1180)，节选）：

```c
if (output_format == OUT_PGM || OUT_PPM || OUT_PNM)  bander = fz_new_pnm_band_writer(ctx, out_);
else if (output_format == OUT_PNG)                   bander = fz_new_png_band_writer(ctx, out_);
else if (output_format == OUT_PS)                    bander = fz_new_ps_band_writer(ctx, out_);
else if (output_format == OUT_PSD)                   bander = fz_new_psd_band_writer(ctx, out_);
else if (output_format == OUT_PWG)                   bander = fz_new_pwg_band_writer(ctx, out_, &opts);   // 或 mono 版
else if (output_format == OUT_PCL)                   bander = fz_new_color_pcl_band_writer(ctx, out_, &opts);
...
if (bander)
    fz_write_header(ctx, bander, pix->w, totalheight, pix->n, pix->alpha,
                    pix->xres, pix->yres, output_pagenum++, pix->colorspace, pix->seps);  // 注意传 totalheight
```

关键：`fz_write_header` 的第二个高度参数是 `totalheight`（整页高 H），不是条带高——band-writer 必须知道整页尺寸才能正确组织输出。

逐带渲染与输出的主循环（[source/tools/mudraw.c:1224](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1224)，节选）：

```c
for (band = 0; band < bands; band++)
{
    if (num_workers > 0) {
        worker_t *work = &workers[band % num_workers];
        mu_wait_semaphore(&work->stop);          // 等该 worker 渲完本带
        pix = work->pix; bit = work->bit;
        if (work->error) fz_throw(...);
    } else {
        drawband(ctx, page, list, ctm, tbounds, cookie, band * band_height, pix, &bit);  // 单线程：主线程自己画
    }

    if (bander && (pix || bit))
        fz_write_band(ctx, bander, bit ? bit->stride : pix->stride,
                      drawheight, bit ? bit->samples : pix->samples);   // 顺序写入本带

    if (num_workers > 0 && band + num_workers < bands) {
        work->band = band + num_workers;          // 预派发后续带（流水线）
        work->pix->y = band_ibounds.y0 + work->band * band_height;      // 重定位 pixmap
        mu_trigger_semaphore(&work->start);
    }
    if (num_workers <= 0) pix->y += band_height;  // 单线程：pixmap 下移
    tbounds.y0 += band_height;                    // 裁剪框下移
    tbounds.y1 += band_height;
}
```

工作线程侧非常简洁（[source/tools/mudraw.c:1782](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1782)）：

```c
static void worker_thread(void *arg)
{
    worker_t *me = (worker_t *)arg;
    int band;
    do {
        mu_wait_semaphore(&me->start);            // 等主线程派活
        band = me->band;
        if (band >= 0) {
            fz_try(me->ctx)
                drawband(me->ctx, NULL, me->list, me->ctm, me->tbounds,
                         &me->cookie, band * band_height, me->pix, &me->bit);
            fz_catch(me->ctx)
                me->error = 1;                    // 异常不跨线程 rethrow，用标志回传（见 u9-l2）
        }
        mu_trigger_semaphore(&me->stop);          // 通知主线程完工
    } while (band >= 0);                          // band<0 是关停信号
}
```

`worker_t` 结构持有每线程自己的 `ctx`、`list`、`ctm`、`tbounds`、`pix`、`cookie`（[source/tools/mudraw.c:284](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L284)），这正是 u9-l2「每个工作线程克隆 context、共享 store、独有异常栈」的具体落地：`band` 字段为 `-1` 时是关停信号。

> 工作线程池是**惰性扩张**的：`dodrawpage` 在发现 `num_workers < bands` 时才创建新线程（[source/tools/mudraw.c:1125](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1125)），且线程数上限是「每页条带数」，跨页复用——线程数随渲染需要缓慢爬升，不会一次拉满。

#### 4.3.4 代码实践

**实践目标**：在 mudraw.c 中走通「按 band 分配 pixmap → 多线程渲染 → 顺序写入」的接入点，并验证多线程分带的正确性与加速。

**操作步骤**：

1. 在源码中定位以下三处（建议用编辑器跳转到对应行号）：
   - 按 band 创建 pixmap：[source/tools/mudraw.c:1159](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1159) `fz_new_pixmap_with_bbox(...)` 与随后的 `pix->y += band * band_height`。
   - 分配/派发工作线程：[source/tools/mudraw.c:1124](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1124) 起的 worker 启动块。
   - 顺序写入：[source/tools/mudraw.c:1248](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1248) 的 `fz_write_band`。
2. 用大分辨率渲染一个多页 PDF，分别测 1 线程与多线程：
   ```bash
   ./build/debug/mutool draw -o a.ppm -r 300 -B 64 -T 1  input.pdf 1-n    # 单线程分带
   ./build/debug/mutool draw -o b.ppm -r 300 -B 64 -T 4  input.pdf 1-n    # 4 线程分带
   ```
3. 用 `cmp a.ppm b.ppm` 验证两次输出逐字节相同（多线程不应改变结果）；用 `time` 比较两者耗时。

**需要观察的现象**：

- `cmp` 无输出（相同），证明多线程分带渲染是确定性的、顺序写入保证了图像正确。
- 多线程版本墙钟时间应明显更短（尤其在多核机器、页数多、`-B` 取适中值如 32~128 时）。若 `-B` 取得极大（接近整页高），`bands` 很小，并行度受限，加速不明显。

**预期结果**：分带 + 多线程在结果上与单线程整页渲染完全一致，且在合适 `band_height` 下获得接近线性的吞吐加速。

> 待本地验证：实际加速比取决于 CPU 核数、页面内容复杂度与 `band_height` 取值，本讲不臆测具体倍数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mudraw 强制 `-B` 与 `-T` 必须同时出现？

**参考答案**：mudraw 把分带定位为「多线程并行的切分单位」——单独 `-B`（单线程分带）只省内存不省时间，mudraw 认为这种用法用 display-list 整页渲染已足够，故报 `banding without multiple threads is pointless`；单独 `-T`（多线程不分带）则没有可并行的条带单位，报 `multiple threads without banding is pointless`。这是 mudraw 的产品取舍，不是 MuPDF 库本身的限制——库层面单线程分带完全合法（`fz_write_pixmap_as_pnm` 就是 N=1 的特例，库 API 不阻止 N>1 的单线程分带）。

**练习 2**：工作线程渲染失败时，主线程如何得知？

**参考答案**：异常不能跨线程 `fz_rethrow`（u9-l2），所以 worker 在 `fz_catch` 里把 `me->error = 1`（[source/tools/mudraw.c:1803](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1803)）。主线程回收该 band 时检查 `if (work->error) fz_throw(...)`（[source/tools/mudraw.c:1239](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1239)），在主线程的 `fz_try` 上下文里重新抛出，从而被 mudraw 的页级错误处理捕获。

---

## 5. 综合实践

把三个模块串起来，完成一个「用自定义分配器量化分带内存收益」的小任务。

**任务**：基于 u2-l2 的自定义分配器，写一个最小程序（或改造 example.c），分别以「整页」和「分带」两种方式渲染同一页到 PNM，统计两种模式下的内存峰值分配量。

**步骤**：

1. 实现一个 `fz_alloc_context`，在 `malloc` 回调里维护「当前在用字节 current」与「历史峰值 peak」（参考 mudraw 自带的内存统计实现思路，见 [source/tools/mudraw.c:1774](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1774) 处 `if (info->current > info->peak) info->peak = info->current;` 的维护方式）。
2. 用该分配器创建 context，注册 handler，打开一个 PDF。
3. **整页路径**：`fz_new_display_list` + `fz_new_list_device` 录制首页 → `fz_new_pixmap_from_page_number(ctx, doc, 0, ctm, colorspace, 0)` 一次渲染整页 → 用 `fz_new_pnm_band_writer` 把整页当一条带写出。记录渲染前后的 `peak`。
4. **分带路径**：录制同样的 display list → 按 4.1 的循环，分配 `w × B` 的小 pixmap，逐带 `fz_run_display_list`（带 `tbounds` 裁剪）→ 逐带 `fz_write_band`。记录 `peak`。
5. 对比两个 `peak`，验证 \[ M_{\text{分带}} / M_{\text{整页}} \approx B/H \] 的方向。

**预期结论**：分带路径的 pixmap 相关峰值显著低于整页路径；两者的 display-list 录制开销相近。这正是分带渲染「以串行复用换峰值降低」的量化体现。

> 待本地验证：自定义分配器的回调签名、`fz_alloc_context` 的填法以 [include/mupdf/fitz/system.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h) 与 u2-l2 的实际定义为准；本实践为源码阅读型 + 编程型，具体数值依赖你的实现。

---

## 6. 本讲小结

- **分带的本质**是用「时间上串行复用一张小 pixmap」换「空间上渲染期峰值的降低」，峰值从 \(w \cdot n \cdot H\) 降到 \(w \cdot n \cdot B\)，代价是渲染次数变成 \(\lceil H/B \rceil\)。
- **`fz_band_writer`** 是面向条带的输出抽象，契约是 `fz_write_header`（登记整页 w×H）→ N 次 `fz_write_band` → `fz_close_band_writer`；骨架在 [source/fitz/output.c:768](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output.c#L768) 统一维护累计行号 `line`、末带自动钳制与满页自动 `trailer`。
- **各格式差异被吸收进回调**：PNM 无状态逐行直写，PNG 跨条带维持同一个 zlib 流（中间带 `Z_NO_FLUSH`、末带 `Z_FINISH`），调用方无需感知。
- **mudraw 的接入**有三个特点：`-B`/`-T` 强制配对、分带与不分带共用同一段循环（不分带即 `bands=1`）、多线程「一条带一线程 + 顺序写入」。
- **多线程分带**以自包含的 `fz_display_list` 为跨线程桥梁，工作线程克隆 context、共享 store、用 `error` 标志而非跨线程 rethrow 回传失败，与 u9-l2 的模型完全一致。
- **输出格式受限**：分带只支持可流式的 PxM/PNG/PS/PSD/PCL/PWG/PCLM/PDFOCR，禁止 J2K（其小波编码无法按条带增量）。

---

## 7. 下一步学习建议

- **续读具体 band-writer 实现**：[source/fitz/output-ps.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-ps.c)、[source/fitz/output-pwg.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pwg.c)、[source/fitz/output-pcl.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/output-pcl.c) ——它们都是真实打印机语言的分带编码器，能加深对「条带如何编码成页面描述语言」的理解。
- **回到 document-writer 视角**：对照 u6-l1 / u6-l3，体会 mudraw（band-writer 路径）与 muconvert（document-writer 路径）在输出抽象上的层次差异，理解为何 PS 既是 document-writer 后端又内含 band-writer。
- **结合多线程深入**：重读 u9-l2，把本讲的 `worker_thread` + 信号量流水线放回「context 家族 / 锁偏序 / 异常每线程一份」的框架里，完整理解 MuPDF 的并发渲染设计。
- **性能与内存调优**：尝试在 mudraw 中调整 `-B`（条带高度）与 `-T`（线程数）的组合，观察吞吐与内存的权衡曲线，建立对「条带高度过小→调度开销大、过大→并行度低」的直觉。
