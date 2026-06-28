# 全文搜索：mugrep 与 stext 搜索

## 1. 本讲目标

上一讲（u5-l2）我们已经能把一页文档「翻译」成一棵带坐标的结构化文本树 `fz_stext_page`。本讲回答它的下一个自然问题：**在这棵树上怎么找一个词，并知道它画在页面的哪里？**

读完本讲你应当能够：

- 说清楚为什么搜索命中区域用四边形 `fz_quad` 表示，而不是矩形 `fz_rect`。
- 掌握单页搜索接口 `fz_search_stext_page` 的输入输出，特别是「命中标记 `hit_mark`」与「返回值是四边形数而非命中数」这两个易错点。
- 看懂命令行工具 `mugrep` 如何用流式（streaming）搜索 API `fz_search` 在「按需喂页」的模式下跨页、跨文档组织搜索，并能维护一个跨页边界的三页滑动窗口。

本讲覆盖三个最小模块：**命中四边形 `fz_quad`**、**单页搜索接口 `fz_search_stext_page`**、**mugrep 与流式跨页搜索**。

## 2. 前置知识

本讲直接承接 u5-l2（结构化文本与 stext 设备），你需要记得：

- `fz_stext_page` 是一棵 `block → line → char` 的树，每个 `fz_stext_char` 除 unicode 码点 `c` 外，还带着它在该页的几何位置（`origin` 与 `quad`）。
- 文本抽取的「阅读顺序」取自字形绘制顺序，不保证与视觉顺序一致；同一行字共享一条基线 `fz_stext_line.dir`。
- stext 设备只是 `fz_device` 的一个派生实现，因此「搜索」本质上是先抽取 stext，再在 stext 上做字符串匹配。

此外复习 u3-l3 的几何：`fz_rect` 是**轴对齐**矩形（四条边都水平/竖直），适合框选水平排版的文字；但文字一旦被旋转、倾斜或斜体，它的真实包围框就不再是轴对齐的，这时候需要 `fz_quad` 这种「四点任意四边形」。本讲会大量用到它。

一个贯穿全讲的直觉：**搜索 = 把一页的 stext 拍扁成一段纯文本（haystack，草垛），在上面找关键词（needle，针），再把匹配到的字节区间反查回 stext 树里的字符，取出这些字符的 `quad` 作为高亮区域。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/mupdf/fitz/geometry.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h) | 定义四边形 `fz_quad` 及其变换/包含判定函数 |
| [include/mupdf/fitz/structured-text.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h) | 声明 `fz_search_stext_page` 等单页搜索接口，以及新一代流式搜索的全部类型与函数 |
| [source/fitz/stext-search.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c) | 搜索的真正实现：选区高亮、文本规范化（"spinning"）、查找器、流式搜索状态机 |
| [source/fitz/util.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c) | 便捷封装：`fz_search_page` / `fz_search_page_number`，把「取页 → 抽 stext → 搜索」串成一步 |
| [source/tools/mugrep.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c) | `mutool grep` 的实现，流式搜索 API 的标准用法范例 |
| [docs/examples/searchtest.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/searchtest.c) | 官方测试程序，演示各种 `fz_search_options` 与正反向搜索 |

## 4. 核心概念与源码讲解

### 4.1 命中四边形 fz_quad：为什么是四边形而不是矩形

#### 4.1.1 概念说明

搜索不仅要告诉你「找到了」，还要告诉你「在页面上哪里」，这样才能高亮、跳转、框选。最自然的表示就是给每个命中一段几何区域。

那为什么用四边形 `fz_quad` 而不是矩形 `fz_rect`？因为文字并不总是水平的：

- 旋转排版的文字（例如竖排、旋转 90° 的页眉）其字形包围框的四条边不与坐标轴对齐。
- 斜体（italic）字形本身就是平行四边形。
- 即便单个字形是正放的，跨行的一个长命中会被拆成「每行一段」，每段都是一个细长的四边形条带。

`fz_rect` 强制轴对齐，用它包斜体或旋转文字会产生多余的空白；`fz_quad` 用四个角点 `ul/ur/ll/lr`（上左、上右、下左、下右）自由描述任意四边形，因而成为搜索命中的标准载体。这也正是 u5-l2 里 `fz_stext_char.quad` 字段的用途——每个字符自带一个四边形，搜索时把命中字符的四边形合并即可。

#### 4.1.2 核心流程

一个命中区域的形成：

1. 在 stext 树上确定一段连续字符 `[begin_char .. end_char]`。
2. 逐字符取出 `ch->quad`。
3. 若相邻字符在同一行且位置紧邻（间距小于模糊阈值），把它们合并成一个更宽的四边形；否则另起一个新四边形。

于是得到一个关键结论：**一次命中（hit）可能对应多个四边形（quad）**——典型场景是关键词横跨两行，第一行末尾和第二行开头各贡献一个四边形。所以「命中数 ≤ 四边形数」。

#### 4.1.3 源码精读

`fz_quad` 的定义只有四个点：

[geometry.h:775-784](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L775-L784) —— 注释明确点出「quads 与 rects 的关键差别在于 quads 的边不一定轴对齐」，随后是 `{ ul, ur, ll, lr }` 四个 `fz_point`。

每个 stext 字符都自带一个 quad，见 `fz_stext_char` 结构：

[structured-text.h:476-487](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L476-L487) —— `origin` 是基线起点，`quad`（第 483 行）才是该字形的视觉包围四边形，`size` 是字号。搜索高亮用的是 `quad` 而非 `origin`。

四边形的「合并」逻辑在 `add_quad`。它先尝试把新字符并入上一个四边形：

[stext-search.c:1647-1681](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1647-L1681) —— 关键在第 1655–1668 行：只有当上一个四边形与新字符**同页（`end->seq == seq`）**、且两者的右上角/右下角与新字符的左上角/左下角在「沿基线方向」与「垂直基线方向」上的距离都落在模糊阈值内时，才把新字符的右上/右下角并入（即把四边形向右拉伸）；否则在数组里追加一个新四边形。

阈值与字符大小成正比（[stext-search.c:1651-1652](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1651-L1652)）：

\[
\text{vfuzz} = \text{size}\times 0.1,\qquad \text{hfuzz} = \text{size}\times 0.5
\]

两个距离函数把任意两点的位移投影到「沿基线」与「垂直基线」方向上：

[stext-search.c:87-99](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L87-L99) —— `hdist` 量沿基线方向的间隔（行内是否连续），`vdist` 量垂直基线方向的间隔（是否换行）。以水平基线 `dir=(1,0)` 为例，退化成 `hdist=|Δx|`、`vdist=|Δy|`；对旋转基线，`dir` 把投影转到了正确朝向。于是同一行相邻字 `vdist≈0` 被合并，跨行字 `vdist` 大于 `vfuzz` 而另起四边形——这正是「一次命中多个四边形」的几何来源。

#### 4.1.4 代码实践

**目标**：直观感受「一个命中 → 多个四边形」。

**步骤**：

1. 找（或造）一份 PDF，让某个关键词在一页内恰好从行末折到下一行行首（例如两端对齐的长段落里的常见词）。
2. 用 `mutool grep -n 关键词 文件.pdf` 搜索。
3. 阅读下文 4.3 的源码后，自行写一小段程序调用 `fz_search_page_number`（见 4.2.3），打印返回值与每个 `hit_bbox[i]` 的四点坐标。

**需要观察的现象**：对该折行命中，返回的四边形数量大于 1，且这些四边形分属两条不同的 y 坐标带（即两个 `vdist` 较大的行）。

**预期结果**：四边形数 = 该命中触及的行数。**待本地验证**（取决于具体文档的折行情况）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用 `fz_rect` 直接当作命中区域？
**答案**：`fz_rect` 强制轴对齐，无法贴合旋转、斜体或竖排文字的真实边界，高亮会出现明显错位与多余空白；`fz_quad` 用四角点描述任意四边形，才能精确包裹这类字形。

**练习 2**：一次搜索命中最多会产生几个四边形？
**答案**：没有上限，等于该命中所覆盖的「字符段」跨越的行数（每行一段，相邻段在 `add_quad` 中因 `vdist` 超过 `vfuzz` 而不合并）。所以四边形数只与命中跨多少行有关，与命中字符数无直接关系。

---

### 4.2 单页搜索接口 fz_search_stext_page

#### 4.2.1 概念说明

最直接、最常用的搜索入口是 `fz_search_stext_page`：给它一棵已经抽好的 stext 树和一个关键词 `needle`，它返回**命中的四边形**。这是 PDF 阅读器里「在当前页查找」按钮背后的核心调用。

它属于 MuPDF 的「第一代」搜索接口，签名简单、固定大小数组输出，适合一次性查一页。需要留意三个设计点：

1. **大小写不敏感**：该接口内部硬编码了 `FZ_SEARCH_IGNORE_CASE`（详见 4.2.3），无法切换为大小写敏感或正则。若需要这些能力，改用同族接口 `fz_match_stext_page`（多一个 `fz_search_options` 参数）。
2. **返回值是四边形数，不是命中数**：呼应 4.1，一次命中可能贡献多个四边形，所以「找到 3 个四边形」不等于「找到 3 处」。
3. **`hit_mark` 用来分组**：因为返回值只给四边形，调用方无法知道哪几个四边形属于同一处命中；`hit_mark[i]` 记录「第 i 个四边形属于第几处命中」，相同 `hit_mark` 值的四边形是同一处。

#### 4.2.2 核心流程

单页搜索的完整链路（以最外层便捷封装 `fz_search_page_number` 为例）：

```
fz_search_page_number(ctx, doc, pageNo, needle, hit_mark, quads, max)
  └─ fz_load_page                       // 取出 fz_page
  └─ fz_search_page
       └─ fz_new_stext_page_from_page   // 抽 stext（默认带 DEHYPHENATE）
       └─ fz_search_stext_page          // 在 stext 上搜索
            └─ 内部其实走新一代流式 API：
               fz_new_search + fz_feed_search(本页) + 循环 fz_search_forwards
               每个命中通过回调 oldsearch_cb 装进输出数组
```

注意最后一步：**「旧」接口是用「新」流式引擎实现的**。`fz_search_stext_page` 只是把流式引擎约束在单页、并把它产生的 `fz_search_match` 拆解成扁平的四边形数组。理解了这一点，就明白为什么两套接口的行为一致、为什么旧接口无法表达「跨页命中」——它根本只喂了一页。

#### 4.2.3 源码精读

接口声明与文档（注意「实验性、可能变更」的告示）：

[structured-text.h:640-650](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L640-L650) —— 明确说明「大小写不敏感」「返回四边形数并存入传入数组」。

它的实现确实是流式引擎的薄包装：

[stext-search.c:2091-2105](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L2091-L2105) —— 第 2102 行硬编码 `FZ_SEARCH_IGNORE_CASE` 调用 `fz_match_stext_page_cb`，再由回调 `oldsearch_cb` 把每个命中的多个四边形依次填入输出数组。

`oldsearch_cb` 解释了 `hit_mark` 的来源：

[stext-search.c:2068-2089](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L2068-L2089) —— 每进入一次回调就是「新的一处命中」，`data->hit++`（第 2073 行）；该命中内的每个四边形都被打上同一个 `hit` 标记写入 `hit_mark`（第 2079–2081 行）。第 2086–2088 行注释强调：即便输出数组满了也「不」返回 1 中止搜索，目的是让旧接口仍能返回**正确的四边形总数**（即便调用方数组装不下）。所以判断「实际命中几处」要对 `hit_mark` 去重计数，而不是看返回值。

最外层的便捷封装让调用方连 stext 都不用自己抽：

[util.c:399-414](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c#L399-L414) —— `fz_search_page` 自动用默认选项 `{ FZ_STEXT_DEHYPHENATE }` 抽 stext（注意它开启了「连字符换行处理」，所以行末 `intro-duction` 之类会被拼回再匹配）。

[util.c:433-447](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c#L433-L447) —— `fz_search_page_number` 再多包一层 `fz_load_page`/`fz_drop_page`，是「给定文档与页号直接搜」的最简入口。

抽 stext 的辅助函数（被 mugrep 也用到）：

[util.c:335-349](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/util.c#L335-L349) —— `fz_new_stext_page_from_page_number` 内部 `fz_load_page` 后转交 `fz_new_stext_page_from_page`，并在 `fz_always` 里 drop 掉临时 `fz_page`，保证不泄漏。

#### 4.2.4 代码实践

**目标**：用 `fz_search_stext_page`（经便捷封装 `fz_search_page_number`）对一份多页 PDF 搜索关键词，打印每个命中所在页码与四边形坐标。

**操作步骤**（以下为示例代码，非仓库原有文件，请保存为 `mysearch.c` 放在仓库外或实验目录，编译方式参考 `docs/examples/`）：

```c
/* 示例代码：搜索并打印命中页码 + 四边形坐标 */
#include "mupdf/fitz.h"
#include <stdio.h>

int main(int argc, char **argv)
{
    if (argc < 3) { fprintf(stderr, "usage: %s file.pdf needle\n", argv[0]); return 1; }
    const char *path = argv[1], *needle = argv[2];

    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    if (!ctx) { fprintf(stderr, "ctx fail\n"); return 1; }
    fz_register_document_handlers(ctx);

    fz_try(ctx)
    {
        fz_document *doc = fz_open_document(ctx, path);
        int n = fz_count_pages(ctx, doc);
        for (int p = 0; p < n; p++)
        {
            int hit_mark[64];
            fz_quad quads[64];
            /* 返回值 = 写入的四边形数（不是命中数！）*/
            int nq = fz_search_page_number(ctx, doc, p, needle,
                                           hit_mark, quads, 64);
            for (int i = 0; i < nq; i++)
            {
                fz_quad q = quads[i];
                printf("page %d  hit#%d  ul(%.1f,%.1f) ur(%.1f,%.1f) "
                       "ll(%.1f,%.1f) lr(%.1f,%.1f)\n",
                       p + 1,            /* 用户页码从 1 起 */
                       hit_mark[i],      /* 同一 hit_mark = 同一处命中 */
                       q.ul.x, q.ul.y, q.ur.x, q.ur.y,
                       q.ll.x, q.ll.y, q.lr.x, q.lr.y);
            }
        }
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
        fz_report_error(ctx);

    fz_drop_context(ctx);
    return 0;
}
```

**需要观察的现象**：

- 多个四边形可能共享同一个 `hit_mark`——它们是同一处折行命中。
- 同一页里 `hit_mark` 从 0 递增；统计「不同 `hit_mark` 值的个数」才是该页的真实命中处数，它可能小于 `nq`。

**预期结果**：打印出形如 `page 3 hit#0 ul(...) ... lr(...)` 的若干行，坐标落在该页的页面坐标系（72 dpi 用户空间）内。

**编译提示**：链接 `libmupdf` 与 `libmupdf-third`（参考 u1-l2 的构建方式），例如：
```
cc mysearch.c -Iinclude build/release/libmupdf.a build/release/libmupdf-third.a -lm -o mysearch
```
**待本地验证**（取决于你机器上的编译环境与测试文档）。

#### 4.2.5 小练习与答案

**练习 1**：`fz_search_stext_page` 的返回值是 5，能说明该页有 5 处命中吗？
**答案**：不能。返回值是写入输出数组的**四边形数**。一处命中若跨多行会产生多个四边形。要得到真实命中处数，需对 `hit_mark[]` 去重计数。

**练习 2**：若输出数组 `max_quads` 太小装不下所有四边形，函数会怎样？
**答案**：搜索**不会**提前中止（`oldsearch_cb` 即便数组满也返回 0），返回值仍反映真实的四边形总数，只是超出 `max_quads` 的四边形没有被写入数组。调用方可据此判断是否需要扩大数组重试。

**练习 3**：想对单页做正则或大小写敏感搜索，该用哪个接口？
**答案**：`fz_match_stext_page`（[structured-text.h:1124](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L1124)），它比 `fz_search_stext_page` 多一个 `fz_search_options` 参数，可指定 `FZ_SEARCH_REGEXP` / `FZ_SEARCH_EXACT` 等。

---

### 4.3 mugrep 与流式搜索 API：跨页窗口

#### 4.3.1 概念说明

单页接口有个根本限制：**关键词跨页时找不到**。比如「docu-」在一页末尾、「ment」在下一页开头，或者一个长词恰好从第 5 页结尾流到第 6 页开头。单页搜索对每一页各自拍扁成文本，页与页之间被切断了。

MuPDF 的解决思路是「流式（streaming）搜索 API」，由一组以 `fz_search` 为中心的函数构成，它们的特点是**搜索器自己持有状态、按需向你索要页面**：

- 你创建一个 `fz_search`（带关键词与选项）。
- 你喂给它一页的 stext（`fz_feed_search`）。
- 你反复问「下一个匹配在哪」（`fz_search_forwards` / `fz_search_backwards`）。
- 它要么给你一个匹配，要么告诉你「请再喂我第 N 页」（`FZ_SEARCH_MORE_INPUT`），要么告诉你「搜完了」（`FZ_SEARCH_COMPLETE`）。

关键设计：搜索器内部维护一个**三页滑动窗口**（上一页 / 当前页 / 下一页），把这三页的文本拼成一段连续的「草垛」再查找，因此能命中跨页边界的关键词。`mugrep`（即 `mutool grep`）正是这套 API 的标准用法范例，它还支持跨多个文档依次搜索、正反向搜索、固定串/正则、忽略大小写与变音符号等。

#### 4.3.2 核心流程

流式搜索是一个**协作式状态机**，调用方与搜索器的交互循环如下：

```
search = fz_new_search(ctx, needle, options)        // 编译关键词（正则则预编译）
fz_feed_search(ctx, search, 第一页stext, seq=0)      // 主动喂首页
loop:
    res = fz_search_forwards(ctx, search)            // 或 fz_search_backwards
    switch res.reason:
      FZ_SEARCH_MATCH:      // 命中！res.u.match 里有 quads[] 与 begin/end 位置
                            //   读出后回到 loop 继续找下一个
      FZ_SEARCH_MORE_INPUT: // 它要更多页：res.u.seq_needed 是想要的页号
                            //   若该页号越界（文档末尾）→ fz_feed_search(ctx, search, NULL, seq)
                            //   否则抽出该页 stext → fz_feed_search(ctx, search, page, seq)
                            //   回到 loop
      FZ_SEARCH_COMPLETE:   // 搜完，跳出
fz_drop_search(ctx, search)
```

`seq` 是你喂页时附带的一个整数（通常就是 0 起的页号），搜索器会**原样回填**到命中结果里（`fz_search_quad.seq`），所以你能在命中时知道它属于哪一页。喂 `NULL` 表示「这个方向上没有更多页了」（文档边界）。

#### 4.3.3 源码精读

先看类型定义，它们决定了「命中」长什么样。三种结果原因：

[structured-text.h:1007-1017](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L1007-L1017) —— `MORE_INPUT`（还要更多页）、`MATCH`（命中）、`COMPLETE`（结束）。

每个命中四边形都带着它来自哪一页的 `seq`：

[structured-text.h:1019-1023](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L1019-L1023) —— `fz_search_quad` 是 `{ int seq; fz_quad quad; }`，`seq` 即喂页时传的页号。

一次完整命中的描述：

[structured-text.h:1034-1053](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L1034-L1053) —— `fz_search_match` 含 `num_quads/quads`（几何高亮）与 `begin/end`（`fz_stext_position`，精确定位到 stext 树里的字符，供摘录上下文用）；`fz_search_result` 用联合体把 `seq_needed`（要页时）与 `match`（命中时）共用同一字段。

再看引擎内部的三页窗口：

[stext-search.c:1260-1299](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1260-L1299) —— `struct fz_search` 持有 `search_page page[3]`（第 1294 行），三个常量 `PREVIOUS_PAGE=0 / CURRENT_PAGE=1 / NEXT_PAGE=2`（第 1297–1299 行）就是窗口的三格。`combined_spun_haystack` 把三页规范化后的文本拼成一段，`combined_spun_split_1/2` 记录前两段的分界，便于把字节偏移反查回某一页。

把三页拼成连续草垛的函数：

[stext-search.c:1564-1585](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1564-L1585) —— 直接 `memcpy` 拼接 `page[0]+page[1]+page[2]` 的 `spun_haystack`。正因为拼成了连续串，跨页的关键词才能被一次匹配命中。

真正调用「查找器」并在命中时收集四边形的核心段落：

[stext-search.c:1809-1884](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1809-L1884) —— 第 1809–1815 行根据方向选 `find`/`find_rev` 在拼接好的草垛里查找；命中后第 1866–1880 行遍历命中区间内每个字符，按它落在哪一段（`split_1`/`split_2`）决定 `seq`（第 1872–1877 行），再调 `add_quad` 收集四边形，最后返回 `FZ_SEARCH_MATCH`。这就是「跨页命中也能正确标注页号」的实现。

喂页函数会校验你给的就是它要的那一页：

[stext-search.c:1919-1964](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1919-L1964) —— 第 1933–1935 行：若搜索器之前请求过特定页，喂来的 `seq` 必须匹配 `req_seq`，否则抛错；`page==NULL` 时置 `end_of_doc`（第 1944 行）标记文档边界。

> 顺带一提「文本规范化」：搜索前，关键词与每页文本都被同一套 Unicode 变换「spinning」处理过——把各种空白/换行归一为空格、连字符归一、全角 ASCII 折半、按需做大小写折叠与变音符号剥离、Unicode 组合/分解（见 [stext-search.c:865-946](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L865-L946) 的 `fz_text_transform` 枚举与 [stext-search.c:1301-1346](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1301-L1346) 的 `init_transform_and_finder`）。所以 `café` 与 `CAFÉ`、全角与半角才能在忽略选项下匹配。规范化后的字节再通过一个 `index` 数组反查回原始 stext 字符。

现在看 mugrep 怎么把这套 API 用起来。主搜索循环在 `mugrep_run`：

[mugrep.c:143-214](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L143-L214) —— 第 155 行 `fz_new_search`；第 157–166 行先喂首页（正向喂第 0 页，反向喂最后一页）；第 168–206 行就是 4.3.2 描述的状态机循环。注意第 185 行用 `details->quads[0].seq + 1` 作为「显示页码」——内部 `seq` 从 0 起，显示给人看时 +1。

对三种结果原因的处理：

[mugrep.c:174-206](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L174-L206) —— `MATCH`（174–186）打印带上下文的摘录片段；`MORE_INPUT`（187–203）判断 `seq_needed` 是否越界，越界则喂 `NULL` 表示到文档边界，否则抽出该页 stext 喂进去；`COMPLETE`（204–205）跳出。

摘录片段的还原能力来自 `begin/end` 这两个 stext 位置：

[mugrep.c:120-141](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L120-L141) —— `show_match_snippet` 用命中的 `begin`/`end` 位置在 stext 树里把命中字符连同所在行的上下文重新打印出来（甚至能跨页，第 130–138 行处理了 `begin.page != end.page` 的情况），命中部分用 `mark_open/mark_close` 包裹（终端下是 ANSI 加粗）。

最后看 mugrep 的默认选项，这是一个容易踩的坑：

[mugrep.c:227](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L227) 与 [mugrep.c:299-300](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L299-L300) —— 选项初值是 `FZ_SEARCH_EXACT`，但只要没用 `-F`（固定串），就**追加 `FZ_SEARCH_REGEXP | FZ_SEARCH_KEEP_PARAGRAPHS`**。也就是说 `mutool grep foo file.pdf` 默认把 `foo` 当**正则**处理！`.` `*` 等字符会被特殊解释，要按字面搜必须加 `-F`。选项的完整清单见 [structured-text.h:981-990](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/structured-text.h#L981-L990)。

#### 4.3.4 代码实践

**目标**：跑通 `mutool grep`，再阅读官方示例 `searchtest.c`，理解流式搜索的喂页循环。

**步骤**：

1. 按 u1-l2 编译出 `mutool`。
2. 准备一份多页 PDF（仓库根目录若有示例文档可用，否则用任意多页 PDF）。
3. 运行：
   ```
   ./build/release/mutool grep -n -F "the" your.pdf
   ```
   `-n` 打印页号、`-F` 按固定串搜。再试 `mutool grep -i -F "THE" your.pdf`（忽略大小写）与 `mutool grep -b -F "the" your.pdf`（反向，从末页往前）。
4. 阅读 [docs/examples/searchtest.c:35-115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/searchtest.c#L35-L115)，对照本节的状态机循环理解它如何处理 `MATCH` / `MORE_INPUT` / `COMPLETE`，特别注意第 89–98 行对 `seq_needed` 越界（文档边界）的处理——与 mugrep 的逻辑一致。

**需要观察的现象**：

- 命中输出带页号与上下文片段，命中词被加亮（若输出到终端）。
- 反向搜索（`-b`）从最后一页开始，但仍按文档顺序喂页给引擎（喂最后一页，引擎再向前索取更早的页）。

**预期结果**：每处命中打印一行，含页号与该词所在片段。**待本地验证**（取决于测试文档与终端是否 tty）。

#### 4.3.5 小练习与答案

**练习 1**：为什么流式搜索要维护「三页」窗口，而不是只看当前页？
**答案**：为了命中跨页边界的关键词。当前页与下一页（正向）或上一页（反向）拼成连续文本后，一个从本页末尾延伸到下页开头的词才能被一次匹配找到；多留一页是为了在窗口滑动时仍能覆盖新的边界。窗口滑动逻辑见 `advance_page`/`retreat_page`（[stext-search.c:1454-1467](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L1454-L1467)）。

**练习 2**：`fz_feed_search(ctx, search, NULL, seq)` 中的 `NULL` 表示什么？
**答案**：表示「在当前搜索方向上没有更多页了」（文档边界）。搜索器据此把对应窗口格标记为 `end_of_doc`，从而能在到达文档端点时返回 `FZ_SEARCH_COMPLETE`。

**练习 3**：`mutool grep foo file.pdf`（不带 `-F`）与 `mutool grep -F foo file.pdf` 的行为有何不同？
**答案**：前者默认把 `foo` 当正则（`FZ_SEARCH_REGEXP | FZ_SEARCH_KEEP_PARAGRAPHS`），`foo` 里的 `.` `*` `+` 等会被特殊解释，且能跨段落匹配；后者按字面固定串搜。若关键词含正则元字符，必须用 `-F` 才能得到预期的字面匹配。

## 5. 综合实践

把三个最小模块串起来，完成一个小型「带页码定位的文档搜索器」：

1. **复用 4.2.4 的程序骨架**（基于 `fz_search_page_number`），但把它改造成接受多个文件参数，对每个文件逐页搜索。
2. **输出格式化**：每个命中打印「文件名 \t 页码 \t 命中处序号 \t 四边形四点坐标」，模仿 mugrep 的 `-H -n` 输出风格（参考 [mugrep.c:120-141](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mugrep.c#L120-L141)）。
3. **统计真实命中处数**：用 `hit_mark` 去重，分别报告「四边形总数」与「命中处数」，验证你对「返回值是四边形数」的理解。
4. **进阶（选做）**：改用流式 API（`fz_new_search` + `fz_feed_search` + `fz_search_forwards`）重写，体会它能找到**跨页**命中——可构造一个关键词恰好跨页的文档来对比两种实现的差异。

通过这个实践，你会同时用到：`fz_quad` 的几何含义、`hit_mark` 的分组语义、单页接口与流式接口的取舍。

## 6. 本讲小结

- 搜索命中区域用四边形 `fz_quad`（`ul/ur/ll/lr` 四点）表示，而非轴对齐的 `fz_rect`，以精确贴合旋转、斜体、竖排文字；命中字符的 `quad` 在 `add_quad` 中按基线方向合并，**一次命中可产生多个四边形**。
- `fz_search_stext_page` 是最常用的单页搜索入口，**大小写不敏感**（硬编码 `IGNORE_CASE`），返回值是**四边形数而非命中数**，需用 `hit_mark[]` 去重才能得到真实命中处数；它内部其实由新一代流式引擎实现。
- 流式 API（`fz_new_search` / `fz_feed_search` / `fz_search_forwards|backwards` / `fz_drop_search`）是协作式状态机，搜索器按需索取页面，内部维护**三页滑动窗口**拼成连续草垛，因而能命中**跨页**关键词；`seq` 随喂页传入并原样回填到命中的 `fz_search_quad.seq`。
- 搜索前会对关键词与文本做统一的 Unicode 规范化（空白/连字符/全角/大小写/变音符号/组合分解），保证「忽略」选项下的等价匹配。
- `mugrep`（`mutool grep`）是流式 API 的标准范例：状态机循环处理 `MATCH/MORE_INPUT/COMPLETE`，支持跨文档、正反向、固定串/正则、忽略大小写与变音符号；**默认把关键词当正则**，字面搜索须加 `-F`。

## 7. 下一步学习建议

- **选区与复制**：本讲的几何工具（`fz_highlight_selection`、`fz_copy_selection`、`fz_snap_selection`，见 [stext-search.c:321-471](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L321-L471)）与搜索共享同一套 stext 几何，阅读它们能加深对 `fz_quad` 与基线投影的理解，也为实现「鼠标选区高亮」打基础。
- **正则引擎**：流式搜索的正则能力由第三方库 mujs 的 `js_regcomp`/`js_regexec` 提供（[stext-search.c:710-757](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-search.c#L710-L757)），可在 `thirdparty/mujs/` 里进一步阅读。
- **OCR 搜索**：扫描件没有文字层时，可经 `fz_new_ocr_device`（u5-l2 提及）先生成 stext 再搜索，把本讲与 OCR 串起来。
- **下一单元（u6）**：从「读」转向「写」，进入 document writer 与格式转换，其中文本写入器 `fz_text_writer` 正是 stext 的导出后端，与本讲呼应。
