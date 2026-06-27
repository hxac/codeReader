# 显示列表：录制与回放

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「显示列表（display list）」是什么，以及它为什么能带来「录制一次、多次回放」的价值。
- 独立写出显示列表的标准三步流程：`fz_new_display_list` → `fz_new_list_device` + `fz_run_page` → `fz_run_display_list`。
- 理解 list device 如何把一次 `fz_run_page` 的绘图指令「录制」成一串紧凑的节点（node）。
- 知道 `fz_run_display_list` 如何在回放时套用一个额外的 `ctm`（缩放/旋转），并把指令分发到任意目标设备，从而支持缩放渲染与后台打印。
- 动手把同一页以 100%、200%、400% 三种缩放回放到 draw device，体会「一次录制多次渲染」的性能优势。

本讲承接上一讲 [u4-l1 fz_device：显示设备抽象](u4-l1-device-model.md)：上一讲我们把 device 定义为「绘图指令的消费者」，本讲介绍一个特殊的 device——list device，它不真正画图，而是把指令「录」下来，留待以后反复回放。

## 2. 前置知识

在进入源码前，先用三个生活类比建立直觉。

1. **录音机类比**。上一讲的 draw device 像一个「现场演奏者」：你给它一条 `fill_path` 指令，它立刻把像素画进 pixmap。list device 则像一个「录音机」：你给它指令，它不画图，而是把指令原样存进一卷「磁带」。之后你可以把同一卷磁带放进不同的「播放器」（draw device、文本抽取设备、另一个 list device……）反复播放。
2. **磁带就是显示列表**。这卷「磁带」就是 `fz_display_list`。它存的是一份**已经解释好的**绘图指令流——也就是说，昂贵的「解析页面（解压、解码、解释 PDF 内容流）」这一步只做一次，结果被缓存进磁带。
3. **为什么这很有用**。官方头文件用两句话点明了用途：

   > 作为减少页面重复解析的缓存机制；以及在多线程中作为一种数据结构——一个线程解析页面，另一个线程渲染页面。

   也就是说，显示列表既是**缓存**，也是**线程间传递页面内容的载体**。

本讲还需要你回忆 u4-l1 的两个结论：device 是一张「函数指针虚表」；`fz_run_page(ctx, page, dev, ctm, cookie)` 会把页面正文、标注、表单控件依次驱动到设备 `dev` 上。list device 正是 `dev` 的一种具体实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/mupdf/fitz/display-list.h` | 显示列表的公共 API 契约：`fz_new_display_list` / `fz_new_list_device` / `fz_run_display_list` / `fz_keep_display_list` / `fz_drop_display_list` / `fz_bound_display_list` / `fz_display_list_is_empty`。 |
| `source/fitz/list-device.c` | 上述 API 的全部实现，也是本讲的主战场：定义节点结构、list device 的虚表、录制函数 `fz_append_display_node`、回放函数 `fz_run_display_list`。 |
| `docs/examples/multi-threaded.c` | 官方「录制 + 多线程回放」范例，本讲把它当作标准三步流程的样板。 |
| `source/tools/mudraw.c` | 命令行渲染工具，展示了「同一段 list 被多次 `fz_run_display_list` 回放」的真实工程用法（含后台打印）。 |
| `include/mupdf/fitz/device.h` | 提供 `fz_new_draw_device`、`fz_close_device`、`fz_drop_device` 等设备生命周期函数，实践环节会用到。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**显示列表创建 → list device 录制 → run_display_list 回放**。三者恰好对应一条完整的「录—存—放」流水线。

### 4.1 显示列表创建

#### 4.1.1 概念说明

`fz_display_list` 是一个**已经解释好的绘图指令的线性序列**。它的核心价值是把「解释页面」这件昂贵的事和「把指令画出来」这件相对便宜的事**解耦**：

- 「解释页面」要打开文档、解压对象流、解释 PDF/XPS 内容流操作符、加载字体与图片——开销大，且依赖文档对象与格式专用层。
- 「回放显示列表」只是把一串现成的指令按顺序重放——开销小，且与文档格式完全无关。

正因为回放阶段不再碰文档，显示列表可以跨越线程边界被安全地传递（文档对象不是线程安全的，显示列表录制完成后则是「自包含」的）。这就是 multi-threaded 范例「主线程录制、工作线程渲染」能成立的前提。

头文件把这份意图写得非常清楚，建议先读这段注释再读代码：

[include/mupdf/fitz/display-list.h:35-47](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/display-list.h#L35-L47) —— 官方对显示列表「缓存 + 多线程数据结构」双重用途的权威说明。

#### 4.1.2 核心流程

显示列表本身的生命周期与 [u2-l2 引用计数](u2-l2-memory-refcount.md) 中讲过的 `fz_storable` 完全一致：

```
fz_new_display_list(ctx, mediabox)   -- 创建空列表，引用计数 refs=1
        │
        ├── fz_keep_display_list      -- 自增 refs（如跨线程传递前 keep 一次）
        │
        ├── （录制阶段：见 4.2；回放阶段：见 4.3）
        │
        └── fz_drop_display_list      -- 自减 refs；refs 归零时遍历所有节点，
                                        释放其持有的 path/text/image/shade…
```

注意一个关键设计：列表里的每个节点会**额外持有**它引用到的资源（路径、文本、图像、渐变、颜色空间等）的引用。所以 `fz_drop_display_list` 归零时，要做一次完整遍历来 drop 这些下级资源（详见 4.3.3 的析构函数）。这意味着：**页面一旦被完整录制进显示列表，原始 `fz_page` 对象就可以立即 drop**，指令已经「自带干粮」存在列表里了。

#### 4.1.3 源码精读

先看列表的结构体定义：

[source/fitz/list-device.c:147-154](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L147-L154) —— `struct fz_display_list` 把首成员设为 `fz_storable`（C 多态手法），从而复用通用的引用计数基础设施；`list` 是节点数组指针，`mediabox` 记录页面边界，`max`/`len` 以「节点个数」为单位管理容量。

创建函数非常简洁：

[source/fitz/list-device.c:1667-1677](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1667-L1677) —— `fz_new_display_list` 用 `fz_malloc_struct` 分配结构体，`FZ_INIT_STORABLE` 把引用计数初始化为 1、析构回调挂成 `fz_drop_display_list_imp`，并把 `list/max/len` 置零（节点数组**延迟到首次录制时才分配**）。

`mediabox` 这个参数很关键：它必须是**被录制页面在 72 dpi 用户空间下的边界**（通常就是 `fz_bound_page` 的返回值）。回放阶段不会再去问页面要边界，而是用录制时记下的这个 `mediabox`（通过 `fz_bound_display_list` 取回）。

其余访问函数都是薄封装：

- [source/fitz/list-device.c:1679-1685](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1679-L1685) —— `fz_keep_display_list` 转调通用 `fz_keep_storable`。
- [source/fitz/list-device.c:1687-1696](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1687-L1696) —— `fz_drop_display_list` 转调 `fz_drop_storable`，并夹了一对 `fz_defer_reap_start/end`（推迟缓存回收，避免析构期间触发 store 扫除）。
- [source/fitz/list-device.c:1698-1702](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1698-L1702) —— `fz_bound_display_list` 直接返回 `list->mediabox`。
- [source/fitz/list-device.c:1704-1707](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1704-L1707) —— `fz_display_list_is_empty` 用 `len == 0` 判空。

#### 4.1.4 代码实践

**实践目标**：亲手创建一个空的显示列表，验证它的边界与「空」状态，再正确释放，建立对生命周期的体感。

**操作步骤**（编写一个最小程序，标注为「示例代码」）：

```c
/* 示例代码：list-create.c —— 仅演示空列表的创建与查询 */
#include <mupdf/fitz.h>
#include <stdio.h>

int main(void)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_rect mediabox = fz_make_rect(0, 0, 595, 842); /* A4 纵向，单位 point */

    fz_display_list *list = fz_new_display_list(ctx, mediabox);

    printf("empty? %d\n", fz_display_list_is_empty(ctx, list));      /* 预期 1（真） */
    fz_rect mb = fz_bound_display_list(ctx, list);
    printf("mediabox = (%g, %g, %g, %g)\n", mb.x0, mb.y0, mb.x1, mb.y1); /* 预期 0,0,595,842 */

    fz_drop_display_list(ctx, list);
    fz_drop_context(ctx);
    return 0;
}
```

**需要观察的现象**：`empty?` 打印 `1`；`mediabox` 打印出 A4 尺寸。

**预期结果**：列表刚创建时 `len == 0`，所以判空为真；边界等于传入的 `mediabox`。真实渲染行为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fz_new_display_list` 传入的 `mediabox` 故意填成 `fz_empty_rect`，回放时会发生什么？
**参考答案**：`mediabox` 只是被记录并在 `fz_bound_display_list` 时原样返回；它本身不参与节点回放逻辑。真正决定回放可见区域的是 `fz_run_display_list` 的 `scissor` 参数和各节点的 `rect`。所以列表仍可正常回放，只是你失去了「页面原本多大」的信息——调用方据此算 pixmap 尺寸时会出错。因此实践中应传真实页面边界。

**练习 2**：为什么 `fz_drop_display_list` 不直接 `fz_free(list)`，而要走引用计数？
**参考答案**：列表可能被多处共享（例如主线程录制后 keep 一份交给工作线程）。引用计数保证只有**最后一个**持有者 drop 时才真正遍历节点、释放下级资源，避免悬挂指针或重复释放。

---

### 4.2 list device 录制

#### 4.2.1 概念说明

光有一个空列表没用，我们还需要一个「录音机」把页面指令灌进去——这就是 **list device**。它和 draw device、stext device 一样，都是 `fz_device` 的派生实现（回顾 u4-l1 的「虚表 + 派生结构体」多态）。区别在于：

- draw device 的回调会把指令**光栅化成像素**；
- list device 的回调则把指令**序列化成一个节点**，追加到列表的节点数组末尾。

所以 list device 是一个**纯生产者**：它消费 `fz_run_page` 发出的统一绘图指令，把它们转译成自包含的节点流。录制完成后，list device 就可以丢弃了——指令已经全部落在列表里。

#### 4.2.2 核心流程

录制的标准四步（这是本讲最重要的代码模板，背下来）：

```
① list = fz_new_display_list(ctx, fz_bound_page(ctx, page))   -- 建空列表
② dev  = fz_new_list_device(ctx, list)                        -- 建录音机，并 keep 列表
③ fz_run_page(ctx, page, dev, fz_identity, NULL)             -- 跑页面，指令逐条录进列表
④ fz_close_device(ctx, dev); fz_drop_device(ctx, dev)        -- 冲刷并丢弃录音机
   fz_drop_page(ctx, page)                                    -- 页面已无用，立即释放
```

注意第 ③ 步传给 `fz_run_page` 的 ctm 通常是 `fz_identity`：录制阶段**不**做缩放，把「原始用户空间坐标」原样存盘；真正的缩放留到回放阶段再套。这正是「一次录制、多种缩放回放」能成立的关键——存的是格式无关的「原始指令」，缩放只是回放时改变的一个参数。

`fz_new_list_device` 会对列表做一次 `fz_keep_display_list`（见下面源码），所以**录音机存在期间列表不会被意外释放**；相应地，录音机的 `drop_device` 回调里会配对地 `fz_drop_display_list`。

#### 4.2.3 源码精读

先看 list device 的派生结构体：

[source/fitz/list-device.c:156-177](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L156-L177) —— 首成员 `fz_device super` 让它能被当作通用 `fz_device *` 传递；其余字段是「当前图形状态」的缓存（`path/alpha/ctm/stroke/colorspace/color/rect`）加上一个裁剪栈 `stack[]`（深度 `STACK_SIZE = 96`，见第 28 行）。这些缓存字段不是装饰——它们是实现「增量编码」的关键，下面会讲。

再看构造函数如何填充虚表：

[source/fitz/list-device.c:1490-1548](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1490-L1548) —— `fz_new_list_device` 把 `super.fill_path`、`super.fill_text`、`super.clip_path`……逐个挂上 `fz_list_*` 回调（与 u4-l1 讲过的 device 虚表一一对应），最后 `dev->list = fz_keep_display_list(ctx, list)` 持有列表引用，并把图形状态初始化为单位矩阵、alpha=1、默认灰度颜色空间。

每一个 `fz_list_*` 回调都长一个样：计算指令的影响矩形，然后调用统一的 `fz_append_display_node` 追加节点。以填充路径为例：

[source/fitz/list-device.c:766-785](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L766-L785) —— `fz_list_fill_path` 先用 `fz_bound_path` 算出矩形，再把 `cmd=FZ_CMD_FILL_PATH`、路径、颜色、alpha、ctm 等交给 `fz_append_display_node`。文本/图像类回调（如 `fz_list_fill_text` 第 850 行）则额外用 `fz_keep_text` 克隆一份文本对象，作为节点的私有数据存进去——这就是 4.1.2 说的「节点持有下级资源引用」。

录制的心脏是 `fz_append_display_node`，它做两件事：

1. **增量编码**：对照 `writer->` 里缓存的「上一条指令后的图形状态」，**只写变化的部分**。例如颜色没变就不写颜色、ctm 只有 `ad` 分量变了就只写 `a/d` 两个 float。节点头是一个 32 位位域，用若干 bit 标记「本节点带不带 rect/path/cs/color/alpha/ctm/stroke」：

   [source/fitz/list-device.c:110-122](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L110-L122) —— `fz_display_node` 的位域布局；`size` 占 9 位，单节点最大 511 个 `fz_display_node` 字节，超出则用 511 作为「间接」标记、后随一个真实 `size_t`（见第 559-563、588-601 行）。

2. **容量自增长**：当 `list->len + size > list->max` 时，`fz_realloc_array` 把节点数组翻倍扩容（第 565-585 行），并修正裁剪栈里指向旧内存的 `update` 指针——这是 C 里手写可变数组的标准操作。

节点类型由一个枚举枚举齐全：

[source/fitz/list-device.c:30-60](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L30-L60) —— `fz_display_command` 列出了全部指令类型，与 u4-l1 的 device 虚表回调一一对应（`FZ_CMD_FILL_PATH`、`FZ_CMD_FILL_TEXT`、`FZ_CMD_CLIP_PATH`、`FZ_CMD_POP_CLIP`、`FZ_CMD_BEGIN_GROUP`……）。

这套「位域头 + 增量数据 + 自增长数组」的设计，让显示列表在内存里极其紧凑——这是它能在多线程间低开销传递、且缓存命中率高的根本原因。

#### 4.2.4 代码实践

**实践目标**：在真实源码里走通「录制四步」，并亲手运行官方范例观察录制效果。

**操作步骤**：

1. 打开官方范例 [docs/examples/multi-threaded.c:216-248](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L216-L248)，逐行对标注释与上面的「四步」模板，确认每一行分别属于哪一步。重点看第 229-238 行：`fz_new_display_list` → `fz_new_list_device` → `fz_run_page` → `fz_close_device`，以及 `fz_always` 块里第 243-247 行如何丢弃 device 和 page。
2. 编译并运行该范例（需要准备一个页数较少的 PDF）：

   ```bash
   make examples
   ./build/debug/multi-threaded some-few-pages.pdf
   ```

**需要观察的现象**：终端按页打印 `thread at page N loading!` / `rendering!` / `done!`，并在当前目录生成 `out0000.png`、`out0001.png`……每张图对应一页。

**预期结果**：主线程为每一页录制显示列表，再把列表交给一个工作线程回放渲染。注意主线程在录制完（drop device 与 page 之后）才把 `list` 交给工作线程——这正好印证了「录制完成后列表自包含、可跨线程」的设计。若未配置子模块或缺第三方库，编译可能失败，此时「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么录制时给 `fz_run_page` 传 `fz_identity`，而不是直接传一个带缩放的 ctm？
**参考答案**：录制阶段存的是「原始用户空间指令」，缩放留到回放。这样同一份列表可以用任意 ctm 回放出任意分辨率；若录制时就 baked-in 缩放，列表就只能在那个固定分辨率下使用，丧失了「一次录制多次渲染」的意义。

**练习 2**：录制阶段 `fz_close_device` 漏掉会怎样？
**参考答案**：`fz_close_device` 会冲刷设备内部缓冲（对 list device 主要是完成配对的裁剪/分组收尾）。漏掉它再直接 drop device，会触发 u4-l1 提到的 `dropping unclosed device` 警告，且可能丢失最后未冲刷的指令，导致回放结果不完整。

---

### 4.3 run_display_list 回放

#### 4.3.1 概念说明

回放是把「磁带」塞进「播放器」的过程。`fz_run_display_list(ctx, list, dev, ctm, scissor, cookie)` 逐个读出节点，恢复图形状态，然后把每条指令重新发给目标设备 `dev`。`dev` 可以是任何设备——draw device（出位图）、stext device（抽文本）、test device（探测彩色/灰度）、甚至另一个 list device（再录一遍）。

回放阶段最强大的地方是**两个运行时参数**：

- `ctm`（函数签名里叫 `top_ctm`）：回放时叠加在每条指令之上的「顶层变换」。录制时存的是单位坐标，回放时你给它一个 `fz_scale(2,2)`，整页就放大两倍——**无需重新解析页面**。这正是缩放渲染/缩略图生成性能极高的原因。
- `scissor`：可见区域裁剪框。回放时会用它对每个节点做**快速可见性剔除（cull）**，完全在框外的指令直接跳过，连目标设备都不调用。这对「只渲染页面的某个局部」或「分带渲染」至关重要。

#### 4.3.2 核心流程

回放主循环可概括为：

```
把 top_ctm 叠加到每个节点的 ctm 上：trans_ctm = concat(node.ctm, top_ctm)
for 每个节点 node:
    1. 还原增量编码的图形状态（rect/cs/color/alpha/ctm/stroke/path）
    2. 用 trans_rect = transform(node.rect, top_ctm) 算变换后的影响区
    3. 用 intersect(trans_rect, scissor) 做可见性剔除：
         - 完全不可见且在裁剪/分组外 → 跳过（甚至进入 clipped 计数）
         - 可见 → goto visible
    4. visible: 把指令通过 fz_fill_path/fz_fill_text/... 重新发给目标 dev
    5. 若 cookie->abort → 提前 break
```

其中 `top_ctm` 与节点自带 `ctm` 的组合用 `fz_concat` 完成。沿用 [u3-l3 坐标、矩阵与页面几何](u3-l3-geometry-matrix.md) 的约定，`fz_concat(A, B) = A × B`（A 先作用于点），所以回放对一点 \(p\) 的最终变换是「先节点 ctm，再顶层 top_ctm」：

\[
p' = \mathrm{top\_ctm}\,\cdot\,(\mathrm{ctm}\,\cdot\,p)
\]

这保证了录制时存的「页面内部相对坐标」与回放时给的「页面到设备坐标」能正确复合。

#### 4.3.3 源码精读

回放主函数很长（约 420 行），但结构清晰：

[source/fitz/list-device.c:1709-1744](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1709-L1744) —— `fz_run_display_list` 开头：本地变量保存「当前图形状态」（`path/alpha/ctm/stroke/color/colorspace`），初始化 cookie 的 `progress_max = list->len`，进入主循环 `for (; node != node_end; node = next_node)`。

每轮循环先**解码节点头与增量数据**，把图形状态还原：

[source/fitz/list-device.c:1744-1884](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1744-L1884) —— 依次读取间接 size、rect、colorspace、color、alpha、ctm（按 `CTM_CHANGE_AD/BC/EF` 位标志只读变化分量）、stroke、path。这一段是 4.2.3 增量编码的「逆操作」。

然后做**可见性剔除**——这是回放性能的一大来源：

[source/fitz/list-device.c:1896-1965](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1896-L1965) —— 先算 `trans_rect = fz_transform_rect(rect, top_ctm)`；再用 `fz_intersect_rect(trans_rect, scissor)` 判空。对路径/文本用「无效矩形」判据（零面积的也放行），其余用「空矩形」判据。若当前已处于被裁掉的嵌套层（`clipped` 计数非零），则只调整嵌套深度、不真正下发指令。

通过剔除后，复合变换并**把指令重新发给目标设备**：

[source/fitz/list-device.c:1967-2009](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1967-L2009) —— `trans_ctm = fz_concat(ctm, top_ctm)`（第 1968 行），随后 `fz_fill_path/fz_stroke_path/fz_fill_text/fz_clip_path/...` 把指令发给 `dev`。注意这些 `fz_xxx` 都是 u4-l1 讲过的「设备包装函数」（做判空与异常隔离），而非直接戳虚表。

最后是一段**容错**：

[source/fitz/list-device.c:2105-2124](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L2105-L2124) —— 单条指令出错时，除非是 `FZ_ERROR_SYSTEM`（致命、必须上抛），否则**吞掉错误**：`cookie->errors++` 计数、`fz_report_error` 打印、继续下一条。这让一个坏指令不会毁掉整页渲染。

至于「列表被析构时如何释放下级资源」，看析构回调：

[source/fitz/list-device.c:1550-1665](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1550-L1665) —— `fz_drop_display_list_imp` 遍历每一个节点，按相同的解码步进规则定位私有数据，对其中 `fz_text/fz_shade/fz_image/fz_function/fz_default_colorspaces` 等逐个 drop，最后 `fz_free(list->list)` 与 `fz_free(list)`。这就是「列表自包含、可独立释放」的实现保证。

工程级用法看 mudraw：它在录制完列表后，**对同一个 list 多次调用 `fz_run_display_list`**，分别送给不同设备（先送 test device 探测彩色、再送 draw device 出图）：

[source/tools/mudraw.c:1483-1509](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1483-L1509) —— 第 1485-1490 行完成录制；随后第 1499-1512 行用同一个 `list` 回放到 `fz_new_test_device` 探测页面特征。一次录制、多次消费的真实范例。

后台打印（bgprint）更进一步：录制线程把 `list` 交给后台线程去回放写盘，主线程立刻去录制下一页，从而让「录制」与「回放输出」并行——这正是头文件说的「多线程数据结构」用途。参见 [source/tools/mudraw.c:1843-1850](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1843-L1850) 里后台线程结束时对 `bgprint.list` 的 drop。

#### 4.3.4 代码实践

**实践目标**：把同一页录制一次，再以 100%、200%、400% 三种缩放回放到 draw device，输出三张 PNG，亲手验证「一次录制、多次渲染」。

**操作步骤**：新建 `record-once-play-many.c`（示例代码），完整程序如下。它严格遵循 mudraw 的约定——**给 draw device 传 `fz_identity`，把带缩放的 ctm 传给 `fz_run_display_list`**：

```c
/* 示例代码：record-once-play-many.c
 * 用法：./record-once-play-many some.pdf [页码(从1起)] */
#include <mupdf/fitz.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    const char *filename = argc >= 2 ? argv[1] : "input.pdf";
    int pageno = argc >= 3 ? atoi(argv[2]) : 1;
    float scales[] = { 1.0f, 2.0f, 4.0f }; /* 100% / 200% / 400% */
    int i;

    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);

    fz_document *doc = NULL;
    fz_page *page = NULL;
    fz_display_list *list = NULL;

    fz_var(doc); fz_var(page); fz_var(list);

    fz_try(ctx)
    {
        doc = fz_open_document(ctx, filename);
        page = fz_load_page(ctx, doc, pageno - 1);   /* 内部页码从 0 起 */
        fz_rect mediabox = fz_bound_page(ctx, page);

        /* ① 录制：建空列表 + list device + 跑页面（用 identity，不预缩放） */
        list = fz_new_display_list(ctx, mediabox);
        fz_device *rec = fz_new_list_device(ctx, list);
        fz_run_page(ctx, page, rec, fz_identity, NULL);
        fz_close_device(ctx, rec);
        fz_drop_device(ctx, rec);

        /* 页面已全部进列表，页面对象可以提前释放 */
        fz_drop_page(ctx, page); page = NULL;

        /* ② 回放：同一个 list，三种缩放，各出一张 PNG */
        for (i = 0; i < 3; i++)
        {
            fz_matrix ctm = fz_scale(scales[i], scales[i]);
            fz_irect ibounds = fz_round_rect(fz_transform_rect(mediabox, ctm));

            fz_pixmap *pix = fz_new_pixmap_with_bbox(ctx, fz_device_rgb(ctx), ibounds, NULL, 0);
            fz_clear_pixmap_with_value(ctx, pix, 0xff); /* 清成白底 */

            fz_device *dev = fz_new_draw_device(ctx, fz_identity, pix); /* 给设备 identity */
            fz_run_display_list(ctx, list, dev, ctm, fz_infinite_rect, NULL); /* 缩放放这里 */
            fz_close_device(ctx, dev);
            fz_drop_device(ctx, dev);

            char out[64];
            snprintf(out, sizeof out, "scale_%d.png", (int)(scales[i] * 100));
            fz_save_pixmap_as_png(ctx, pix, out);
            printf("wrote %s  (%dx%d)\n", out,
                   fz_pixmap_width(ctx, pix), fz_pixmap_height(ctx, pix));
            fz_drop_pixmap(ctx, pix);
        }
    }
    fz_always(ctx)
    {
        fz_drop_page(ctx, page);
        fz_drop_display_list(ctx, list);
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
    {
        fz_report_error(ctx);
        fz_drop_context(ctx);
        return 1;
    }

    fz_drop_context(ctx);
    return 0;
}
```

编译（在仓库根目录；根据你 `make` 时的模式选用 `build/release` 或 `build/debug`）：

```bash
gcc -o record-once-play-many record-once-play-many.c \
    -Iinclude \
    build/release/libmupdf.a build/release/libmupdf-third.a \
    -lm -lpthread
./record-once-play-many some.pdf 1
```

**需要观察的现象**：当前目录生成 `scale_100.png`、`scale_200.png`、`scale_400.png` 三张图；后两张的像素尺寸约为前一张的 2 倍、4 倍，内容相同但更清晰。

**预期结果**：三张图内容一致、尺寸按缩放比例增长，证明同一份列表被正确地以三种 ctm 回放。注意 `fz_new_pixmap_from_display_list` 这个名字看似方便的函数**在当前版本并不存在**（pixmap.h 中没有），因此上面用「手算缩放后边界 + `fz_new_pixmap_with_bbox` + `fz_new_draw_device(identity)` + `fz_run_display_list(ctm)`」的标准做法。真实运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把上面程序里 `fz_new_draw_device(ctx, fz_identity, pix)` 的 `fz_identity` 改成 `ctm`，会出现什么问题？
**参考答案**：缩放会被应用**两次**——一次在 draw device 内部（把设备坐标再缩放），一次在 `fz_run_display_list` 的 `top_ctm`。结果图像会被放大到预期的平方倍（4 倍变 16 倍），且 pixmap 尺寸没跟着长，内容会被裁切错位。正确约定是：draw device 收 `fz_identity`，缩放只放进 `fz_run_display_list`。

**练习 2**：把 `fz_run_display_list` 的 `scissor` 从 `fz_infinite_rect` 改成页面左上角四分之一区域，渲染结果会怎样？性能呢？
**参考答案**：只有落在该区域内的指令会被下发到 draw device，区域外的指令在 4.3.3 的剔除阶段就被跳过；输出图像只画出了左上角四分之一的内容（其余保持白底）。当页面指令很多、而可视区域很小时，这种剔除能显著减少回放耗时——这正是分带渲染与局部刷新的基础。

---

## 5. 综合实践

**任务**：量化「一次录制、多次回放」相对于「每次都重新解析页面」的性能优势。

**思路**：用同一页、同一组缩放（100/200/400%），对比两种方案的总耗时：

- **方案 A（用显示列表）**：录制 1 次 → 回放 3 次。
- **方案 B（不用显示列表）**：对每个缩放都调用一次 `fz_new_pixmap_from_page_number(ctx, doc, pageno-1, ctm, colorspace, alpha, seps)`（见 [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) 与 [include/mupdf/fitz/document.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h)），每次都重新解释页面。

**操作步骤**（示例代码骨架）：

```c
/* 示例代码片段：用 clock() 分别计时两种方案（仅示意，需补全错误处理与释放） */
#include <time.h>

static double ms_since(clock_t t0) { return (double)(clock() - t0) * 1000.0 / CLOCKS_PER_SEC; }

/* 方案 A：录制一次，回放三次 */
clock_t t0 = clock();
list = fz_new_display_list(ctx, fz_bound_page(ctx, page));
dev = fz_new_list_device(ctx, list);
fz_run_page(ctx, page, dev, fz_identity, NULL);
fz_close_device(ctx, dev); fz_drop_device(ctx, dev);
for (i = 0; i < 3; i++) {
    fz_matrix ctm = fz_scale(scales[i], scales[i]);
    /* ...用 4.3.4 的方式把 list 回放到 pixmap 并 drop pixmap... */
}
printf("方案A (录制1次+回放3次): %.2f ms\n", ms_since(t0));

/* 方案 B：三次都重新解析页面 */
t0 = clock();
for (i = 0; i < 3; i++) {
    fz_matrix ctm = fz_scale(scales[i], scales[i]);
    fz_pixmap *pix = fz_new_pixmap_from_page_number(ctx, doc, pageno - 1, ctm,
                                                     fz_device_rgb(ctx), 0, NULL);
    fz_drop_pixmap(ctx, pix);
}
printf("方案B (每次重新解析): %.2f ms\n", ms_since(t0));
```

**需要观察的现象**：方案 A 的总耗时通常明显低于方案 B；差距随页面复杂度（指令数、字体、图片数量）增大而拉大，因为方案 B 把「解释页面」的固定成本重复付出了 3 次。

**预期结果**：选一个内容丰富的页面（多字体、含图）能看出更显著的差异；纯文本简单页差距较小。具体数值「待本地验证」。把两种方案的耗时与页面指令规模（可粗略用渲染时 store 命中情况估计）整理成一张表，你就亲手验证了显示列表作为「解析缓存」的价值。

## 6. 本讲小结

- 显示列表 `fz_display_list` 是一份**已解释好的、自包含的**绘图指令流，既是「解析缓存」，也是「跨线程传递页面内容的载体」。
- 标准三步：`fz_new_display_list(ctx, mediabox)` 建空列表 → `fz_new_list_device` + `fz_run_page(page, dev, fz_identity, NULL)` 录制 → `fz_run_display_list` 回放。
- list device 是 `fz_device` 的派生实现，每个回调通过 `fz_append_display_node` 把指令**增量编码**成紧凑节点（位域头 + 仅变化的数据），存进自增长的节点数组。
- 录制完成后即可 drop 原始 `fz_page`；列表节点会自行 keep/drop 下级的 path/text/image/shade 等资源。
- 回放时 `fz_run_display_list` 把顶层 `top_ctm` 叠加到每条指令上（支持任意缩放/旋转），并用 `scissor` 做**可见性剔除**，单条指令出错会被吞掉而非中断整页。
- 工程约定：给 draw device 传 `fz_identity`，把带缩放的 ctm 传给 `fz_run_display_list`；mudraw 与 multi-threaded 范例都遵循这一约定。

## 7. 下一步学习建议

- 想看「位图后端」如何接收回放指令并光栅化，进入下一讲 [u4-l3 draw device 与 pixmap 位图渲染](u4-l3-draw-device-pixmap.md)。
- 想理解显示列表如何被串进完整的命令行渲染管线（含 cookie 进度、多分辨率、后台打印），进入 [u4-l4 mudraw：渲染管线的集大成者](u4-l4-mudraw-pipeline.md)。
- 对「列表跨线程传递」感兴趣，可直接读 [docs/examples/multi-threaded.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c)，并预习 [u9-l2 多线程渲染](u9-l2-multithreading.md)。
- 建议带着本讲的「录制—回放」模型去重读 `source/fitz/list-device.c` 的 `fz_append_display_node`（增量编码）与 `fz_run_display_list`（剔除+分发）这两个函数，它们是理解 MuPDF 渲染性能的关键。
