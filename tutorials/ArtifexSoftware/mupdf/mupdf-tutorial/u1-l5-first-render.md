# 第一个渲染程序：跑通 example.c

## 1. 本讲目标

本讲带你亲手跑通 MuPDF 官方的第一个渲染示例 `docs/examples/example.c`。学完后你应当能够：

- 写出 MuPDF 最小渲染程序的「标准三步」：创建 context → 注册 handler → 打开文档。
- 解释 zoom（缩放）和 rotate（旋转）如何通过 `fz_scale` 与 `fz_pre_rotate` 拼成一个变换矩阵 ctm。
- 用 `fz_new_pixmap_from_page_number` 把任意一页渲染成位图，并按正确的顺序释放资源。
- 看懂一段「位图 → PPM 图像」的手动像素输出代码。

本讲是第一单元的收尾：你已经知道 MuPDF 是什么（u1-l1）、怎么编译（u1-l2）、目录长什么样（u1-l3）、`mutool` 如何分发命令（u1-l4）。本讲把这些知识「跑起来」——用不到 140 行 C 代码完成一次真实的页面渲染。

## 2. 前置知识

阅读本讲前，建议你先了解几个名词。它们在后续讲义里会被深入展开，这里只需有个直觉：

- **context（上下文）**：MuPDF 的「全局对象」，几乎每个 fitz 函数的第一个参数都是它。它内部装着异常栈、资源缓存、锁等。单线程程序只要一个 context。
- **handler（文档处理器）**：每种文档格式（PDF / XPS / EPUB / 图片……）对应一个 handler，它知道如何识别和打开这种格式。要先用 `fz_register_document_handlers` 把它们登记进 context，之后 `fz_open_document` 才能按文件名/内容自动匹配。
- **pixmap（位图）**：一块连续的像素内存，渲染的最终产物。`struct fz_pixmap` 用 `samples`（像素首地址）、`w/h`（宽高）、`stride`（每行字节数）、`n`（每像素通道数）来描述这块内存。
- **ctm（current transformation matrix，当前变换矩阵）**：一个 2D 仿射矩阵，把「页面坐标（以点 point 为单位，72 dpi）」映射到「设备像素坐标」。缩放和旋转都是通过往这个矩阵里塞内容实现的。
- **PPM**：一种极简的明文图像格式（P3 为 ASCII 版本），用「宽 高 + 每个像素的 RGB 数值」描述图像，方便在没有图像库的情况下肉眼/工具验证渲染结果。

本讲用到的 C 知识仅有：命令行参数 `argc/argv`、`fz_try/fz_catch` 异常块（见 u2-l3，这里先当作 try/catch 用）、以及按地址偏移读取一段内存。异常机制的原理留到 u2-l3 再讲，本讲只学它的「用法外形」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) | 本讲主角：官方最小渲染示例，从打开文档到输出 PPM 全过程 |
| [include/mupdf/fitz/document.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h) | 文档抽象的公共 API：`fz_open_document` / `fz_count_pages` 等 |
| [include/mupdf/fitz/context.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h) | `fz_new_context` / `fz_drop_context` 与 `FZ_STORE_UNLIMITED` 的定义 |
| [include/mupdf/fitz/geometry.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h) | `fz_matrix` 与 `fz_scale` / `fz_pre_rotate` 等几何函数 |
| [include/mupdf/fitz/util.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/util.h) | 高层便捷函数 `fz_new_pixmap_from_page_number` 的声明 |
| [include/mupdf/fitz/pixmap.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h) | `struct fz_pixmap` 字段定义与 `fz_drop_pixmap` |
| [Makefile](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile) | `make examples` 目标，编译示例 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 context 创建与 handler 注册**——一切调用的前提。
- **4.2 打开文档与页数**——把一个文件变成可操作的 `fz_document`。
- **4.3 矩阵与 pixmap 渲染**——计算 ctm 并光栅化出一帧像素。

### 4.1 context 创建与 handler 注册

#### 4.1.1 概念说明

MuPDF 的设计哲学是：**所有状态都挂在 context 上，函数尽量「无状态」**。因此使用 MuPDF 的第一步永远是 `fz_new_context`，最后一步是 `fz_drop_context`。

`fz_new_context` 接收三个参数：

1. `alloc`：自定义内存分配器，传 `NULL` 用标准库的 `malloc/free`。
2. `locks`：多线程用的锁集合，单线程传 `NULL`（多线程见 u9-l2）。
3. `max_store`：资源缓存（store）的最大字节数。`FZ_STORE_UNLIMITED` 表示不设上限。

context 建好后，默认「不知道」任何文档格式。需要调用 `fz_register_document_handlers` 把「本构建支持的所有格式处理器」登记进去（哪些格式取决于编译期的 `FZ_ENABLE_*` 开关，详见 u1-l2、u1-l4）。这之后，`fz_open_document` 才能根据扩展名或内容自动选用正确的 handler。

#### 4.1.2 核心流程

```
fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED)   →  得到 ctx（可能为 NULL）
        │
        ▼
fz_try(ctx) fz_register_document_handlers(ctx);  →  登记格式处理器
fz_catch(ctx) { 报错 + fz_drop_context(ctx); 退出 }
```

注意三件事：

- `fz_new_context` **可能返回 NULL**（内存不足等），必须判空。
- `fz_register_document_handlers` 会抛异常（在 `fz_try` 内调用），失败时要先 `fz_drop_context` 再退出，避免泄漏 context。
- 之后所有会「分配资源」的调用（open、render）都应放在 `fz_try` 内。

#### 4.1.3 源码精读

example.c 中创建 context 的两行：

```c
/* Create a context to hold the exception stack and various caches. */
ctx = fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED);
```
见 [docs/examples/example.c:50-56](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L50-L56)：用默认分配器、单线程、缓存不限大小创建 context，并判空。

`fz_new_context` 实际是一个宏，展开为带版本校验的实现函数：

```c
#define fz_new_context(alloc, locks, max_store) fz_new_context_imp(alloc, locks, max_store, FZ_VERSION)
```
见 [include/mupdf/fitz/context.h:345](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L345)。`FZ_VERSION` 用来保证「编译时头文件版本」与「运行时库版本」一致，不匹配会抛异常。

`max_store` 的取值含义在枚举里：

```c
enum {
    FZ_STORE_UNLIMITED = 0,
    FZ_STORE_DEFAULT = 256 << 20,   /* 256 MB */
};
```
见 [include/mupdf/fitz/context.h:312-315](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L312-L315)。生产环境一般用 `FZ_STORE_DEFAULT`（256 MB 上限，满了会淘汰旧缓存）；示例为了「只渲染一页、绝不被淘汰」直接用了无限。

登记 handler 的异常块：

```c
fz_try(ctx)
    fz_register_document_handlers(ctx);
fz_catch(ctx)
{
    fz_report_error(ctx);
    fprintf(stderr, "cannot register document handlers\n");
    fz_drop_context(ctx);
    return EXIT_FAILURE;
}
```
见 [docs/examples/example.c:58-67](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L58-L67)。`fz_register_document_handlers` 的声明在 [include/mupdf/fitz/document.h:445](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L445)，文档注释说明它「注册本构建支持的所有标准文档类型」。

#### 4.1.4 代码实践

**实践目标**：验证「创建 context → 登记 handler → 销毁」这条最小生命周期。

**操作步骤**：

1. 新建一个 `mini.c`（示例代码，不是项目原有文件）：

   ```c
   #include <mupdf/fitz.h>
   #include <stdio.h>

   int main(void) {
       fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
       if (!ctx) { fprintf(stderr, "no ctx\n"); return 1; }
       fz_try(ctx)
           fz_register_document_handlers(ctx);
       fz_catch(ctx)
           fprintf(stderr, "register failed\n");
       fz_drop_context(ctx);
       printf("ctx lifecycle ok\n");
       return 0;
   }
   ```

2. 用示例相同的链接方式编译（参考 `make examples` 的链接规则，见 [Makefile:442-443](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L442-L443)）：链接 `libmupdf.a` 与 `libmupdf-third.a`。

**需要观察的现象**：程序正常打印 `ctx lifecycle ok` 并退出，无内存错误。

**预期结果**：在带 `sanitize` 或 `memento` 的构建下（u1-l2 介绍过 `build=sanitize`/`memento`）运行也不会报泄漏。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FZ_STORE_DEFAULT` 改成 `FZ_STORE_UNLIMITED`，`fz_new_context` 的返回值会因此失败吗？
**答案**：不会。`FZ_STORE_UNLIMITED` 的值是 `0`（见 [context.h:313](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L313)），它只是「不设上限」的合法参数，不是错误码。

**练习 2**：为什么 example.c 在 `fz_catch` 里要先调用 `fz_drop_context` 再 `return`？
**答案**：`fz_new_context` 已经成功分配了 context，注册失败时若不手动 `fz_drop_context` 就 `return`，会造成内存泄漏。`fz_drop_context` 会释放 context 及其全局状态（见 [context.h:374](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L374)）。

---

### 4.2 打开文档与页数

#### 4.2.1 概念说明

有了 context 和 handler 之后，下一步是把磁盘上的文件「打开」成内存里的 `fz_document` 对象。`fz_open_document(ctx, filename)` 会：

1. 根据文件名扩展名（magic）或文件内容，匹配一个已注册的 handler（识别逻辑详见 u3-l2）。
2. 调用该 handler 的 `open` 回调，读取文件基本结构，返回 `fz_document *`。

`fz_document` 是「格式无关」的抽象：无论 PDF、XPS 还是 EPUB，对外都是同一套 API。其中最常用的就是 `fz_count_pages`——查询总页数。

> 一个细节：用户习惯「页码从 1 开始」，而 MuPDF 内部「页码从 0 开始」。example.c 用 `atoi(argv[2]) - 1` 做了这个换算。

#### 4.2.2 核心流程

```
doc = fz_open_document(ctx, input)        ← 可能在 fz_try 内抛异常
        │
        ▼
page_count = fz_count_pages(ctx, doc)     ← 总页数
        │
        ▼
检查 0 <= page_number < page_count        ← 越界则报错退出
```

打开文档与计数页数都用 `fz_try/fz_catch` 保护。注意清理顺序：**后申请的资源先释放**——如果计数失败，要先 `fz_drop_document` 再 `fz_drop_context`。

#### 4.2.3 源码精读

打开文档：

```c
fz_try(ctx)
    doc = fz_open_document(ctx, input);
```
见 [docs/examples/example.c:69-78](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L69-L78)。`fz_open_document` 的 API 契约在 [include/mupdf/fitz/document.h:502](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L502)，注释说明它会「读取基本结构以便定位页和对象，并尝试修复损坏文档（不改写文件）」。

计数页数与越界检查：

```c
page_count = fz_count_pages(ctx, doc);
...
if (page_number < 0 || page_number >= page_count) { ... 报错 + 清理 ... }
```
见 [docs/examples/example.c:80-98](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L80-L98)。`fz_count_pages` 声明在 [include/mupdf/fitz/document.h:707](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L707)，注释说明「无页文档可能返回 0」。

注意越界分支里也是先 `fz_drop_document` 再 `fz_drop_context`（[example.c:95-96](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L95-L96)）——这是 MuPDF 资源释放的通用范式：**逆序释放，且 context 总是最后一个 drop**。

#### 4.2.4 代码实践

**实践目标**：体会「同一套 `fz_open_document` + `fz_count_pages`，作用于不同格式」。

**操作步骤**：

1. 准备两个文件：一个 PDF、一个 EPUB（或 XPS）。
2. 基于 example.c 改写一个只打印页数的小程序（示例代码）：把渲染部分删掉，只保留 open + count，输出 `"%s: %d pages"`。

**需要观察的现象**：同一段代码，对 PDF 和 EPUB 都能正确打印页数。

**预期结果**：因为 `fz_register_document_handlers` 登记了所有格式，`fz_open_document` 会自动选用 PDF handler 或 EPUB handler（EPUB 经 HTML 引擎处理，见 u1-l1）。这正是「双层架构」的价值：上层代码不关心格式。

#### 4.2.5 小练习与答案

**练习 1**：example.c 为什么用 `atoi(argv[2]) - 1` 而不是直接 `atoi(argv[2])`？
**答案**：因为用户输入是 1 起始的页码（usage 文案明确写「Page numbering starts from one」，见 [example.c:39](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L39)），而 MuPDF 内部页号从 0 开始，所以减 1。

**练习 2**：如果传给 example.c 的文件名扩展名不认识（比如 `.xyz`），会发生什么？
**答案**：`fz_open_document` 找不到匹配的 handler，会抛异常，被 [example.c:72-78](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L72-L78) 的 `fz_catch` 捕获，打印 `cannot open document` 并清理退出。（MuPDF 也会尝试按文件内容识别，但都不匹配时同样抛异常。）

---

### 4.3 矩阵与 pixmap 渲染

#### 4.3.1 概念说明

这是本讲的核心。要把一页「画」成像素，需要两样东西：

1. **ctm（变换矩阵）**：告诉 MuPDF「这页要放大几倍、旋转多少度」。
2. **一个目标 pixmap**：用来盛放渲染出的像素。

example.c 用一个「一行搞定」的便捷函数 `fz_new_pixmap_from_page_number` 把「取页 → 建矩阵 → 建设备 → 渲染 → 返回 pixmap」全部打包。它内部其实走的是「device 模型 + draw device」的完整链路（见 u4 单元），本讲先把它当黑盒用。

#### 4.3.2 核心流程

**第一步：理解 `fz_matrix`。**

MuPDF 用 6 个浮点数 `[a b c d e f]` 表示一个 2D 仿射矩阵，对应矩阵：

\[
\begin{bmatrix} a & b & 0 \\ c & d & 0 \\ e & f & 1 \end{bmatrix}
\]

它把一个点 \((x, y)\) 变换为：

\[
(x', y') = (a x + c y + e,\; b x + d y + f)
\]

定义见 [include/mupdf/fitz/geometry.h:387-390](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L387-L390)。

**第二步：构造缩放 + 旋转矩阵。**

example.c 的写法是：

```c
ctm = fz_scale(zoom / 100, zoom / 100);
ctm = fz_pre_rotate(ctm, rotate);
```

- `fz_scale(sx, sy)` 生成纯缩放矩阵 `[sx 0 0 sy 0 0]`（[geometry.h:431](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L431)）。`zoom` 是百分比，100 表示 1 倍。MuPDF 默认分辨率是 **72 dpi**（1 点 = 1/72 英寸），所以 zoom=100 时，1 个点正好映射到 1 个像素。
- `fz_pre_rotate(m, deg)` 是「前乘」一个旋转矩阵，即结果 = `rotate(deg) · m`（[geometry.h:515](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L515)）。

矩阵乘法对点的「作用顺序」是从右往左：`rotate · scale · p` 表示**先缩放、再旋转**。这与直觉一致：「放大后再转」。

> **关于旋转方向的一个提醒**：example.c 的 usage 文案写「Rotation is in degrees clockwise」（顺时针，见 [example.c:41](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L41)），而底层 `fz_pre_rotate`/`fz_rotate` 的文档注释写的是「counter clockwise（逆时针）」（[geometry.h:500](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L500)、[geometry.h:510-511](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L510-L511)）。这种差异源于页面坐标系与屏幕光栅坐标系 Y 轴方向的不同（页面内容空间 Y 轴朝上，位图 Y 轴朝下）。最终在输出的 PPM 上「看起来是顺时针还是逆时针」**待本地验证**——本讲的实践环节会让你亲眼确认。

**第三步：渲染进 pixmap。**

```c
pix = fz_new_pixmap_from_page_number(ctx, doc, page_number, ctm, fz_device_rgb(ctx), 0);
```

- 第 5 个参数 `fz_device_rgb(ctx)` 指定 RGB 颜色空间（声明见 [include/mupdf/fitz/color.h:297](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/color.h#L297)）。
- 第 6 个参数 `0` 表示 `alpha=0`（不要透明通道）。

因此每个像素占 `n = 3` 个字节（R、G、B）。

**第四步：理解 pixmap 的内存布局。**

`struct fz_pixmap` 关键字段（[include/mupdf/fitz/pixmap.h:431-445](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L431-L445)）：

| 字段 | 含义 |
| --- | --- |
| `w, h` | 图像宽、高（像素） |
| `n` | 每像素通道数（RGB 无 alpha 时为 3） |
| `stride` | 每行字节数（通常 `= w * n`，也可能带 padding） |
| `samples` | 像素数据首地址 |

example.c 的像素遍历正是按这个布局来的：

```c
for (y = 0; y < pix->h; ++y) {
    unsigned char *p = &pix->samples[y * pix->stride];   /* 第 y 行起点 */
    for (x = 0; x < pix->w; ++x) {
        printf("%3d %3d %3d", p[0], p[1], p[2]);          /* RGB 三通道 */
        p += pix->n;                                       /* 跳到下一像素 */
    }
}
```
见 [docs/examples/example.c:117-132](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L117-L132)。注意它用 `y * stride` 定位行、用 `p += n` 在行内步进——这正是「stride 与 n 协同」的标准写法。

#### 4.3.3 源码精读

构造 ctm 的两行（含注释「默认 72 dpi」）：

```c
/* Compute a transformation matrix for the zoom and rotation desired. */
/* The default resolution without scaling is 72 dpi. */
ctm = fz_scale(zoom / 100, zoom / 100);
ctm = fz_pre_rotate(ctm, rotate);
```
见 [docs/examples/example.c:100-103](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L100-L103)。

渲染进 pixmap：

```c
fz_try(ctx)
    pix = fz_new_pixmap_from_page_number(ctx, doc, page_number, ctm, fz_device_rgb(ctx), 0);
```
见 [docs/examples/example.c:105-115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L105-L115)。该便捷函数声明在 [include/mupdf/fitz/util.h:59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/util.h#L59)，同系列还有 `fz_new_pixmap_from_page`（按 `fz_page` 对象）等（[util.h:58](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/util.h#L58)）。

最后的资源释放，严格逆序：

```c
fz_drop_pixmap(ctx, pix);
fz_drop_document(ctx, doc);
fz_drop_context(ctx);
```
见 [docs/examples/example.c:134-138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L134-L138)。`fz_drop_pixmap` 声明在 [include/mupdf/fitz/pixmap.h:199](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/pixmap.h#L199)。注意 pixmap 先于 document 释放，document 先于 context 释放——因为 pixmap 依赖 document、document 依赖 context。

#### 4.3.4 代码实践

这是本讲的主实践。

**实践目标**：编译运行 example.c，把 PDF 第一页渲染成 PPM；再让它渲染第 3 页、150% 缩放、旋转 90°。

**操作步骤**：

1. 先确认已按 u1-l2 编译出库（`libmupdf.a` / `libmupdf-third.a`）。
2. 编译示例：

   ```bash
   make examples
   ```

   该目标见 [Makefile:440](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L440)，编译规则见 [Makefile:442-443](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L442-L443)。产物路径由 `OUT` 决定，默认是 `build/release/example`（若用 `make build=debug examples` 则是 `build/debug/example`，与 example.c 头部注释一致，见 [example.c:1-17](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L1-L17)）。

3. 渲染第一页（页码从 1 起，缩放 100%，不旋转）：

   ```bash
   ./build/release/example mydoc.pdf 1 100 0 > page1.ppm
   ```

4. **达成「第 3 页、150%、旋转 90°」有两种方式**：

   - **方式 A（无需改码）**：example.c 已把这些做成命令行参数，直接传参即可：

     ```bash
     ./build/release/example mydoc.pdf 3 150 90 > page3.ppm
     ```

     （内部 `atoi(argv[2]) - 1` 把 3 转成内部页号 2；zoom=150 → scale=1.5；rotate=90。）

   - **方式 B（按题目要求「修改代码」）**：编辑 example.c，把缩放与旋转「写死」或改默认值，例如把第 102-103 行改成：

     ```c
     ctm = fz_scale(1.5, 1.5);     /* 150% */
     ctm = fz_pre_rotate(ctm, 90); /* 旋转 90 度 */
     ```

     并把目标页改为内部页号 2（即用户页码 3），重新 `make examples` 后运行。

**需要观察的现象**：

- `page1.ppm` 是按 72 dpi 渲染的第 1 页图像。
- `page3.ppm` 的尺寸约为第 3 页的 1.5 倍，且画面旋转了 90°（宽高对调）。
- 用任意图片查看器（如 `display`、`gimp`，或在线 PPM 查看器）打开两个 PPM 对比。

**预期结果**：能清晰看到缩放使图像变大、旋转使方向改变，且宽高互换。

> 若环境缺少图片查看器：PPM 的 P3 格式是明文，可直接 `head page3.ppm` 看到前几行 `P3`、宽 高、`255` 及像素数值，据此也能判断宽高是否对调。旋转方向（顺/逆时针）**待本地验证**——用一张方向特征明显的页面（如带箭头或文字朝向）最容易确认。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `fz_pre_rotate` 换成「先 rotate 再 scale」的写法 `fz_concat(fz_rotate(90), fz_scale(1.5,1.5))`，结果和 example.c 的写法一样吗？
**答案**：一样。`fz_pre_rotate(m, d)` 的语义就是 `fz_concat(fz_rotate(d), m)`（前乘），见 [geometry.h:416](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L416) 的 `fz_concat`。两种写法得到同一个复合矩阵。

**练习 2**：把 `fz_new_pixmap_from_page_number` 的最后一个参数从 `0` 改成 `1`（开启 alpha 通道），PPM 输出代码会出什么问题？
**答案**：此时每像素 `n = 4`（RGB+A），但 PPM 输出仍按 `p[0..2]` 读三个通道并 `p += n` 步进 4 字节。结果是每个被读取的像素都「跳过了一个 alpha 字节」、且读到的不再是连续像素的 RGB——画面会错位/偏色。这正好说明 `n` 与输出逻辑必须匹配。

**练习 3**：为什么资源释放必须「pixmap → document → context」逆序？
**答案**：pixmap 依赖 document（渲染时用到页面对象），document 依赖 context（几乎所有 fitz 调用都要 ctx）。先释放被依赖者会导致后续释放访问已释放内存。context 是最底层、最后释放（见 [example.c:134-138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L134-L138)）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「带校验的渲染器」小任务。

**任务**：基于 example.c 扩展一个 `render_check.c`（示例代码），实现以下功能：

1. 从命令行读入 `文件名 页码 缩放 旋转 输出.ppm`。
2. 按 4.1～4.3 的标准流程：建 context、登记 handler、打开文档、计数页数并做越界检查、构造 ctm、渲染进 RGB pixmap。
3. 渲染前先用 `fz_count_pages` 打印总页数；渲染后打印 `pix->w`、`pix->h`、`pix->n`、`pix->stride`，并**自检**：`stride` 是否等于 `w * n`（无 padding 时应相等）。
4. 按 P3 格式写出 PPM。
5. 严格逆序释放 pixmap / document / context，且每一步的 `fz_try/fz_catch` 都要正确清理已申请的资源。

**验证方法**：

- 对同一页用「zoom=100 不旋转」和「zoom=200 不旋转」分别渲染，检查后者宽高是否约为前者 2 倍、`stride` 是否相应翻倍。
- 对「zoom=100 rotate=90」检查宽高是否互换。
- 用 `build=sanitize` 重新编译运行，确认无内存错误与泄漏。

**这个任务能帮你检验什么**：你是否真正理解了 context/handler 生命周期、文档抽象的统一 API、ctm 的构造顺序、pixmap 的内存布局，以及 MuPDF 「逆序释放 + try/catch 清理」的资源管理范式。

## 6. 本讲小结

- MuPDF 最小程序骨架是「三步」：`fz_new_context` → `fz_register_document_handlers` → `fz_open_document`，每步都要判空/包 `fz_try` 并正确清理。
- `FZ_STORE_UNLIMITED`（=0）表示缓存不限大小，`FZ_STORE_DEFAULT`（256 MB）是生产推荐值（[context.h:312-315](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L312-L315)）。
- `fz_open_document` + `fz_count_pages` 是格式无关的统一接口；用户页码从 1 起，内部从 0 起。
- 缩放与旋转通过 `fz_scale` + `fz_pre_rotate` 拼成一个 `fz_matrix`（ctm）；`pre_rotate` 是前乘，等价于「先缩放、再旋转」。
- `fz_new_pixmap_from_page_number` 一步把页面光栅化为 pixmap；其内存由 `samples/w/h/stride/n` 描述。
- 资源释放严格逆序：pixmap → document → context，且 context 总是最后 drop。

## 7. 下一步学习建议

你现在已经能渲染单页，但很多内部机制还是黑盒。建议接下来：

- **u2（context/内存/异常）**：本讲你只用了 `fz_try/fz_catch` 的外形，u2-l3 会揭开它基于 `setjmp/longjmp` 的原理；u2-l2 讲 keep/drop 引用计数与自定义分配器，让你理解「drop」到底释放了什么。
- **u3（文档抽象）**：u3-l1 把 `fz_document`/`fz_page` 的虚表讲透，u3-l2 解释 `fz_register_document_handlers` 背后的 handler 注册表与格式识别，u3-l3 系统讲 `fz_matrix` 与坐标几何。
- **u4（设备模型与渲染管线）**：本讲的 `fz_new_pixmap_from_page_number` 其实内部走了「device → draw device → pixmap」的完整链路，u4 会拆开这条链，让你看到 `fz_run_page` 如何驱动设备。
- 想立刻「换个玩法」：用 `mutool draw`（u1-l4 介绍的分发表里的 `draw` 子命令）做同样的事，对照 example.c 理解「库 API」与「命令行工具」的对应关系。
