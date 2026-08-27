# 章节生成流程：从目录到 chapter_N.xml

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「一本书的章节边界」是如何被确定的：不是页码，而是**目录条目坐标与正文标题块坐标的匹配**。
- 理解 `generate_chapter_files` 的完整流程：清空旧文件 → 逐章生成 → 写盘前做标点归一化与章内层级分析。
- 理解第一个目录条目匹配之前的内容（封面、前言等）为何落入 `chapter_head.xml`，以及它的 `level` 为何被设成目录最大层级。
- 理解 `analyse_chapter_internal_levels` 如何只凭「标题块的检测框高度」就把一章内的小节标题分出 1～5 级。
- 亲手修改 `toc.xml` 并零成本重新生成章节文件，观察章节边界的变化。

## 2. 前置知识

本讲建立在 u4-l3（Toc 数据模型）、u5-l1（章节数据模型）、u5-l2（Jointer）之上，先把几个关键结论复述一遍。

### 2.1 坐标键 (page_index, order)：全书通用的「地址」

pdf-craft 里，定位一段文字不靠字符串，靠坐标：

- **目录条目** `Toc` 只存 `id`、`page_index`、`order`、`level`、`children`，不存标题文本（u4-l3 的核心设计）。
- **OCR 识别出的正文块** `BlockLayout` 同样携带 `page_index`（页码）与 `order`（页内块序号），外加检测框 `det`（u3-l4 的 PageLayout 五字段）。

于是「这个正文块是不是某个目录条目对应的标题」这个问题，被转化为一次字典查询：`(page_index, order) in ref2toc`。

### 2.2 TITLE_TAGS：哪些布局算「标题」

```python
TITLE_TAGS = ("title", "sub_title")
```

见 [pdf_craft/pdf/ref.py:L1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ref.py#L1)。OCR 后端的布局类型在 u3-l4 已归一化为若干种 `ref`，其中只有 `title` 与 `sub_title` 被视为标题。目录分析（u4）与章节切分（本讲）都依赖这个判定。

### 2.3 Jointer 的输出流

u5-l2 讲过，`Jointer` 把按页按块的 OCR 输出重组为按自然段与资源组织的布局流：

- `ParagraphLayout`：文字段落，内含若干 `BlockLayout`；
- `AssetLayout`：图片、表格、公式等资源块。

本讲的切分循环正是消费这股流，决定每个布局归属哪一章。

### 2.4 变异系数（CV）与中位数

- **中位数** `median`：把一组数值排序后取中间值，比平均值更抗极端值干扰（[pdf_craft/common/statistics.py:L17-L30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/statistics.py#L17-L30)）。
- **变异系数**（Coefficient of Variation）：

\[ CV = \frac{\sigma}{\mu} \]

即标准差除以平均值，衡量一组数的**相对离散程度**。一组「同一层级的标题高度」应当大小接近（CV 小）；如果把两个不同层级的标题混在一组，大小差距拉开（CV 变大）。u4-l2 的目录层级分析已经用过一次这个思想，本讲的章内层级分析再用一次。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/extractor/chapter/generation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py) | 本讲主文件：章节生成入口、切分状态机、输入流水线 |
| [pdf_craft/extractor/chapter/analyse_level.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py) | 章内标题层级分析 |
| [pdf_craft/common/cv_splitter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py) | `split_by_cv`：CV 控制的递归二分聚类（u4-l2 已见过） |
| [pdf_craft/pdf/ref.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ref.py) | `TITLE_TAGS` 常量 |
| [pdf_craft/extractor/toc/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py) | `Toc`/`TocInfo`/`iter_toc`（u4-l3 已精读） |
| [pdf_craft/extractor/toc/config.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/config.py) | `MAX_TITLE_CV = 0.025` 阈值 |
| [pdf_craft/extractor/chapter/chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py) | `Chapter`/`ParagraphLayout` 定义与 XML 编解码（u5-l1 已精读） |
| [pdf_craft/extractor/chapter/punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py) | 标点归一化（本讲只讲位置，机制留到 u5-l4） |
| [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py) | 引擎四步主流程中第三步的调用点 |

## 4. 核心概念与源码讲解

### 4.1 章节生成入口：generate_chapter_files

#### 4.1.1 概念说明

`generate_chapter_files` 是引擎四步主流程（OCR 循环 → 目录分析 → **章节生成** → 元数据落盘）的第三步，调用点在 [pdf_craft/transform.py:L106-L112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L106-L112)：`analyse_toc` 产出 `TocInfo` 后紧跟着调它。

它解决的问题是：把「OCR 缓存（`ocr/page_N.xml`）+ 目录结论（`toc.xml`）」这两层缓存重算成**章节文件** `chapters/chapter_N.xml`。理解它的关键是 u3-l1 说过的一句话：

> `chapters/` 每次重算，`ocr/` 与 `toc.xml` 才是缓存层。

所以这个函数的第一件事就是删光旧的 `chapter_*.xml`——章节文件是纯粹的派生物，永远可以由缓存重建。

#### 4.1.2 核心流程

```text
generate_chapter_files(pages_path, chapters_path, toc)
  1. mkdir chapters/，删除全部旧的 chapter_*.xml
  2. for chapter in _generate_chapters(...):     # 切分生成器（4.2）
       a. 决定文件名后缀 tail：id 为 None → "head"，否则 → str(id)
       b. chapter = normalize_punctuation_in_chapter(chapter)   # 写盘前处理一
       c. chapter = analyse_chapter_internal_levels(chapter)    # 写盘前处理二
       d. encode(chapter) → save_xml(chapters/chapter_{tail}.xml)
```

注意 `chapter_*.xml` 这个 glob 会同时匹配 `chapter_head.xml`（`*` 匹配 `"head"`），所以前置章节文件也会被清掉重建。

#### 4.1.3 源码精读

入口函数全文只有 20 行：

- [pdf_craft/extractor/chapter/generation.py:L23-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L23-L26) —— 建目录、`glob("chapter_*.xml")` 逐个 `unlink()`：每次全量重算，不留增量。
- [pdf_craft/extractor/chapter/generation.py:L28-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L28-L36) —— 消费切分生成器；`chapter.id is None` 时后缀取 `"head"`，写 `chapter_head.xml`；否则 `chapter_{id}.xml`。
- [pdf_craft/extractor/chapter/generation.py:L38-L42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L38-L42) —— 写盘前的固定两步：先标点归一化，后层级分析，最后 `encode` + `save_xml` 落盘。

两步写盘前处理各司其职：

- **标点归一化** `normalize_punctuation_in_chapter`（[punctuation.py:L16-L21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L16-L21)）：修正 OCR 常见的「汉字后跟半角标点」问题（如 `你好,` → `你好，`），同时处理正文布局与脚注引用布局。这是 issue #310 的修复，具体判定规则留到 u5-l4 精读。
- **章内层级分析** `analyse_chapter_internal_levels`：为章内小节标题分配 1～5 级，详见 4.4。

顺序上标点在前、层级在后，但两者互不依赖（前者改文本、后者只读检测框高度），固定顺序只是管线的确定性约定。

`encode` 产出的 XML 结构（u5-l1 讲过编解码，这里只看根元素）见 [pdf_craft/extractor/chapter/chapter.py:L118-L125](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L118-L125)：根元素 `<chapter>`，`id` 属性只在非 None 时写入——所以 **`chapter_head.xml` 的根元素没有 `id` 属性**，这是识别它的可靠特征。

#### 4.1.4 代码实践

**实践目标**：统计一个已提取包中所有章节文件的 id、level 与布局数量，并确认 `chapter_head.xml` 是否存在。

**操作步骤**（示例代码）：

```python
# stats_chapters.py —— 示例代码
from pathlib import Path
from xml.etree import ElementTree as ET

chapters_path = Path("package/chapters")  # 换成你的 DocumentPackage 路径

files = sorted(chapters_path.glob("chapter_*.xml"))
print(f"共 {len(files)} 个章节文件")
for f in files:
    root = ET.parse(f).getroot()
    body = root.find("body")
    print(
        f"{f.name}: id={root.get('id')} "
        f"level={root.get('level')} layouts={len(body)}"
    )
```

**需要观察的现象**：

- 文件总数是否约等于目录条目数（外加可能的 `chapter_head.xml`）；
- `chapter_head.xml` 的 `id` 是否为 `None`；
- 每个文件的 `layouts` 数量差异（短章与长章）。

**预期结果**：若书的前言等内容出现在第一个目录条目之前，列表的第一个文件是 `chapter_head.xml` 且 `id=None`；其余文件 `id` 从 1 递增。实际数值取决于你使用的 PDF，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate_chapter_files` 要先删除所有旧文件，而不是像 OCR 那样逐页缓存？

**答案**：章节切分依赖整本书的目录匹配结果：`toc.xml` 的任何变化都可能移动所有章节边界。逐文件缓存在目录变化时几乎必然全部失效，且无法检测「哪些章节受影响」；而重算的输入（`ocr/`、`toc.xml`）已被缓存，切分本身是纯本地 CPU 逻辑，成本远低于 OCR，全量重算反而最简单、最不易出错。

**练习 2**：如果两次提取之间 `toc.xml` 没变、`ocr/` 也没变，重新生成的 `chapter_*.xml` 会和上次不同吗？

**答案**：不会。切分输入（OCR 页文件 + 目录）与写盘前处理（标点归一化、层级分析）都是确定性的纯函数逻辑，相同输入得到相同输出——这正是「派生物」可随时重建的前提。

### 4.2 章节切分循环：_generate_chapters 状态机

#### 4.2.1 概念说明

这是本讲的心脏。`_generate_chapters` 是一个**生成器**（generator）：它逐个吐出 `Chapter` 对象，边消费布局流边维护「当前章」，把一本书切成若干章。

切分的触发条件只有一个：**当前布局是标题段落，且它的某个块的坐标 `(page_index, order)` 命中目录条目**。换句话说：

- 目录决定「章」从哪里开始；
- 目录里没有的标题（小节标题）不会切章，只是留在章内（其层级由 4.4 分析）。

#### 4.2.2 核心流程

先用伪代码描述状态机（`S` 为当前章，初始为 None）：

```text
ref2toc = { (page_index, order) -> Toc }     # 由目录树先序遍历摊平而来

for layout in 布局流:                          # 顺序：文档阅读顺序
    if layout 是标题段落 and 其任一块坐标 in ref2toc:
        item = 命中的目录条目
        if S 存在: yield S                     # 上一章定稿
        S = Chapter(id=item.id, level=item.level, layouts=[layout])
    else:
        if S 不存在: S = head 章（见 4.3）
        S.layouts.append(layout)
if S 存在: yield S                             # 最后一章定稿
```

「持有—切换—冲刷」与 u5-l2 的 Jointer 状态机如出一辙：当前章一直持有布局，直到下一个目录标题出现才让出。

布局流本身由 `_extract_body_layouts` 提供，它是一条三段流水线：

```text
ocr/page_N.xml（XMLReader 流式读取）
  → 过滤掉目录页（toc.page_indexes）
  → body / footnotes 两个 Jointer 并行重组成段落流
  → footnotes 流按页聚合成 References，按页游标匹配
  → 正文块中的脚注标记（Mark）替换为 Reference 对象
  → join_texts_in_content 合并块内相邻文本
  → yield layout
```

#### 4.2.3 源码精读

**切分主循环**：

- [pdf_craft/extractor/chapter/generation.py:L48-L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L48-L52) —— 初始化 `chapter = None`，并用 `iter_toc`（先序遍历，见 [toc/types.py:L23-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L23-L26)）把目录树摊平成坐标字典 `ref2toc`。目录的层级结构在这里被「压扁」——切分只关心每个条目的坐标，不关心它在树的哪一层。
- [pdf_craft/extractor/chapter/generation.py:L56-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L56-L60) —— 候选判定三条件：是 `ParagraphLayout`、有块、`layout.ref in TITLE_TAGS`。三个条件缺一不可——普通正文段落即使坐标碰巧命中也不会切章。
- [pdf_craft/extractor/chapter/generation.py:L61-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L61-L65) —— 遍历该段落的块，查 `ref2toc`。标题可能被 OCR 拆成多个块，只要**任一块**坐标命中即可认定。
- [pdf_craft/extractor/chapter/generation.py:L66-L73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L66-L73) —— 命中即切章：先 `yield` 上一章，再以目录条目的 `id`、`level` 开新章。注意章的 `level` 直接继承目录层级——章有多「深」由 u4 的目录层级分析决定，与本讲 4.4 的**章内**层级是两回事。
- [pdf_craft/extractor/chapter/generation.py:L76-L84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L76-L84) —— 未命中则追加进当前章；当前章还不存在时创建 head 章（4.3 详述）。
- [pdf_craft/extractor/chapter/generation.py:L86-L87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L86-L87) —— 流结束后冲刷最后一章。

**输入流水线** `_extract_body_layouts`：

- [pdf_craft/extractor/chapter/generation.py:L91-L95](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L91-L95) —— `XMLReader` 以 `page` 为前缀流式读取 `ocr/` 目录下的页文件并 `decode` 成 `Page` 对象（这就是断点续跑缓存被消费的地方）。
- [pdf_craft/extractor/chapter/generation.py:L96-L110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L96-L110) —— **目录页被整体排除**：`p.index not in toc_page_indexes` 的页才进入 Jointer（body 与 footnotes 各一个实例）。目录页上的「第一章 …… 3」这类文本因此不会混进任何章节。
- [pdf_craft/extractor/chapter/generation.py:L111-L126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L111-L126) —— 脚注流经 `_extract_page_references`（[L139-L159](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L139-L159)，按页分组聚合）变成逐页的 `References`；`get_references` 用一个只前进的游标按页匹配——脚注与正文都按页码有序，游标无需回退。
- [pdf_craft/extractor/chapter/generation.py:L128-L136](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L128-L136) —— 段落落盘前：把块内文本中的脚注标记替换为 `Reference` 对象（[L173-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L173-L187) 的 `expand` 回调，找不到对应脚注则保留原文），并 `join_texts_in_content` 合并块内相邻文本片段。这两步是 u5-l4 引用机制的入口，本讲只需知道位置。

#### 4.2.4 代码实践

**实践目标**：验证「目录决定章」——删掉 `toc.xml` 里的一个条目后，对应标题不再切章。

**操作步骤**（示例代码，直接调用切分入口，零 OCR 成本）：

```python
# resplit.py —— 示例代码
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET
from pdf_craft.extractor.chapter import generate_chapter_files
from pdf_craft.extractor.toc import decode as decode_toc

package = Path("package")
chapters_path = package / "chapters"

# 1. 备份当前章节文件，便于对比
backup = Path("chapters_backup")
if not backup.exists():
    shutil.copytree(chapters_path, backup)

# 2. 从 toc.xml 中删除一个二级条目（第一个有子条目的顶级条目的第一个子条目）
tree = ET.parse(package / "toc.xml")
for item in tree.getroot().findall("item"):
    children = item.findall("item")
    if children:
        removed = children[0]
        item.remove(removed)
        print("已删除条目: id=", removed.get("id"), "page=", removed.get("page_index"))
        break
tree.write(package / "toc.xml", encoding="utf-8", xml_declaration=True)

# 3. 仅重跑章节生成（ocr/ 缓存不动）
toc = decode_toc(ET.parse(package / "toc.xml").getroot())
generate_chapter_files(
    pages_path=package / "ocr",
    chapters_path=chapters_path,
    toc=toc,
)
```

**需要观察的现象**：

- 被删条目对应标题下的内容去哪了？（应并入上一章）
- 章节文件数量是否减一？
- 文件名 `chapter_{id}.xml` 的 `id` 序列是否出现「空洞」？

**预期结果**：章节文件少一个；被删标题所在的内容并入前一章（该章 `layouts` 数变大）；由于其余条目的 `id` 保存在 `toc.xml` 属性中不会重排，文件名序列会出现空洞（如 `1,2,4,5...`）。被删标题本身仍以 `title` 块留在正文里，由 4.4 的层级分析赋级，渲染为上一章的小节标题。**待本地验证**（具体条目选取取决于你的 `toc.xml` 结构）。

#### 4.2.5 小练习与答案

**练习 1**：为什么切分匹配要求 `layout.ref in TITLE_TAGS`？如果去掉这个条件会发生什么？

**答案**：坐标 `(page_index, order)` 标识的是「页内第几个块」，目录条目在 u4 生成时也是从标题块收集的，两者天然对应。但如果去掉 `TITLE_TAGS` 判定，任何恰好落在该坐标上的布局（例如重新 OCR 后该位置变成了正文段）都可能被当作章标题，切出错误边界；类型判定是一道廉价的语义保险。

**练习 2**：目录页（`toc.page_indexes` 中的页）为什么必须从布局流里排除？不排除会有什么后果？

**答案**：目录页上密布「章节标题 + 页码」文本，且目录条目的 `page_index` 本就指向目录页或其附近。不排除的话，目录页的标题块可能命中 `ref2toc`，在文档最开头就把章节切得支离破碎，目录文本本身也会作为正文混进章节。

**练习 3**：`get_references` 的游标为什么只前进、不回退？

**答案**：`body_jointer.execute()` 产出的布局按阅读顺序（页码单调不减）排列，脚注聚合流也按页码排序。两股流各自有序，滑动游标每个布局只需推进到 `current.page_index >= block.page_index` 即可，均摊 O(1)；回退匹配会让复杂度退化为线性扫描。

### 4.3 前置章节处理：chapter_head.xml

#### 4.3.1 概念说明

真实书籍的第一个目录条目之前往往有封面、版权页、序言、译者的话等「不属于任何一章」的内容。`_generate_chapters` 的处理方式是：**在第一个目录命中之前，所有布局都归入一个 `id=None` 的章**，写盘时命名 `chapter_head.xml`。

它的 `level` 不取自目录（它没有对应条目），而是取**全书目录的最大层级**，源码注释写明用意：「防止章节标题盖过其他」。

#### 4.3.2 核心流程

```text
第一个未命中的布局到来时：
  max_level = max(所有目录条目的 level)     # 无条目时 default=0
  chapter = Chapter(id=None, level=max_level, layouts=[])
  chapter.layouts.append(layout)
```

下游消费：Markdown 渲染时每章的标题级数基数就是 `Chapter.level`（[pdf_craft/markdown/render/render.py:L53-L59](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L53-L59) 传 `toc_level=chapter.level`），标题实际级数为 `min(toc_level + layout.level, 6)`（[pdf_craft/markdown/render/layouts.py:L57-L61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L57-L61)，Markdown 最多 6 级）。

#### 4.3.3 源码精读

- [pdf_craft/extractor/chapter/generation.py:L76-L84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L76-L84) —— head 章的创建：`chapter is None` 说明还没有任何目录命中。`max((t.level for t in iter_toc(toc.content)), default=0)` 求最大层级，目录为空时兜底为 0。
- [pdf_craft/extractor/chapter/generation.py:L81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L81) —— 行内注释「防止章节标题盖过其他」。设为 `max_level` 后，head 章内的标题布局渲染时从 `max_level + level` 级起——若设 0，head 里的版权页大字标题会渲染成一级标题 `#`，在文档结构里盖过真正的章标题。
- [pdf_craft/extractor/chapter/generation.py:L32-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L32-L36) —— `id is None` → `tail = "head"` → `chapter_head.xml`。
- [pdf_craft/extractor/chapter/chapter.py:L121-L122](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L121-L122) —— `id` 属性仅在非 None 时写入：`chapter_head.xml` 的根元素形如 `<chapter level="1">`，没有 `id`。

两个边界情形值得记住：

- 如果第一个布局恰好命中目录（书一开篇就是第一章），`chapter` 从头就是正常章，**不会产生** `chapter_head.xml`；
- 如果全书没有任何目录命中（如目录分析失败、`toc.xml` 为空），所有内容都在一个 head 章里，`chapter_head.xml` 是唯一的章节文件。

#### 4.3.4 代码实践

**实践目标**：确认 `chapter_head.xml` 的存在条件与内容构成。

**操作步骤**：在 4.1.4 的统计脚本基础上增加（示例代码）：

```python
head = chapters_path / "chapter_head.xml"
if head.exists():
    root = ET.parse(head).getroot()
    body = root.find("body")
    print(f"head 章存在: id={root.get('id')} level={root.get('level')} "
          f"layouts={len(body)}")
    for el in list(body)[:5]:
        print("  首个布局:", el.tag, el.get("ref"), el.get("level"))
else:
    print("无 head 章：第一个目录条目之前没有内容")
```

**需要观察的现象**：head 章里前几个布局的 `ref` 类型（多为 `text`/`title`）与 `level` 分布。

**预期结果**：若存在，`id` 一栏为 `None`；其内容是封面文字、版权页、前言等；`level` 等于目录最大层级（如两级目录的书为 2）。具体内容取决于 PDF，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`max_level` 为什么用 `default=0`？

**答案**：`max()` 对空序列抛异常，`default=0` 处理「目录为空」的退化情形——此时全书就是一个 head 章，level 取 0 即可，不需要与其他章比较。

**练习 2**：head 章会被 `chapters_path.glob("chapter_*.xml")` 删除吗？

**答案**：会。glob 模式中的 `*` 匹配任意字符串，`chapter_head.xml` 同样命中，每次重算时一并删除重建。

**练习 3**：一本书目录分析完全失败（`toc.xml` 只有空的 `<toc/>` 根元素），`chapters/` 里会有什么？

**答案**：只有一个 `chapter_head.xml`，包含全部正文布局。因为 `ref2toc` 为空、永远没有命中，所有内容都进入 head 章（`level=0`）；最后的 `if chapter: yield chapter` 把它冲刷出去。

### 4.4 章内层级分析：analyse_chapter_internal_levels

#### 4.4.1 概念说明

目录切出了章，但章内还有小节：`1.1`、`1.2.3`、无编号的小标题……这些标题**不在目录里**（或者即使目录里有，也不参与切章）。渲染成 Markdown 时它们需要正确的 `##`/`###` 深度——这就是 `analyse_chapter_internal_levels` 的工作：给章内每个标题布局分配层级。

它的依据只有一个物理信号：**标题文字的检测框高度**（即视觉字号）。排版惯例是小节标题字号小于大节标题，且同一层级标题字号一致。这与 u4-l2 目录层级分析的思路同源，但作用域从「全书」缩小到「一章」，且不涉及目录页换算。

层级语义约定：

- 章标题本身（`layouts[0]`）固定 `level = 0`；
- 章内标题分 1～5 级（Markdown 最多 6 级标题，章已占去一级）。

#### 4.4.2 核心流程

```text
输入: 一章的 layouts
1. 收集候选: 跳过非标题布局; 第一个布局(章标题)固定 level=0 不参与;
   其余标题布局取块高度中位数 median(det[3] - det[1])
2. 聚类: split_by_cv(高度列表, max_cv=0.025, max_groups=5)
   —— 反复把 CV 超阈值的组在最大间隔处二分
3. 赋级: 组按平均高度降序 → level = 1, 2, ..., N
   (最高的标题 → 最小 level → 最浅的 Markdown 标题)
```

`split_by_cv` 的分裂循环（[cv_splitter.py:L59-L70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L59-L70)）：

1. 从单组开始；
2. 找出 CV 最大且超过 `max_cv` 的组（[L78-L93](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L78-L93)）；
3. 将该组按高度排序，在**相邻最大间隔**处切成两半（[L96-L113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L96-L113)）；
4. 重复直到没有组超阈值或达到 `max_groups`；
5. 返回按组内均值**升序**排列的组列表（[L72-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L72-L75)）。

阈值 `MAX_TITLE_CV = 0.025` 非常紧（[toc/config.py:L3](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/config.py#L3)）：同一层级标题的高度差异应控制在 2.5% 以内，稍有混杂就继续分裂。

#### 4.4.3 源码精读

- [pdf_craft/extractor/chapter/analyse_level.py:L6-L7](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L6-L7) —— `_MAX_TITLE_GROUP = 5`，注释说明原因：Markdown 最多 6 级标题，章标题占去一级。
- [pdf_craft/extractor/chapter/analyse_level.py:L10-L24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L10-L24) —— 主函数：`split_by_cv` 返回升序组，`reversed` 后变**从大到小**，`enumerate(start=1)` 依次赋 level 1、2、…。最大的标题拿到最小 level（渲染时 `#` 最少），层级语义与目录层级一致。
- [pdf_craft/extractor/chapter/analyse_level.py:L27-L37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L27-L37) —— 候选收集：
  - [L30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L30) 只看 `ParagraphLayout` 且 `ref in TITLE_TAGS` 的布局——普通正文即使字号大也不参与；
  - [L32-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L32-L33) `i == 0`（章的第一个布局）固定 `level = 0`——它就是章标题，正是 4.2 中开新章时放进去的那个标题布局；
  - [L34-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py#L34-L36) 高度信号取块检测框高度 `det[3] - det[1]` 的**中位数**：标题跨多个块时，中位数抵御了个别异常块的干扰。
- [pdf_craft/common/cv_splitter.py:L47-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L47-L75) —— `split_by_cv` 本体；CV 计算见 [L36-L44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L36-L44)，即标准差除以均值。

两级 level 体系至此完整，用一张表总结：

| level | 作用域 | 来源 | 消费点 |
| --- | --- | --- | --- |
| `Chapter.level` | 整章 | 目录条目 `Toc.level`（head 章为 max_level） | 渲染标题级数基数 `toc_level` |
| `ParagraphLayout.level = 0` | 章标题布局 | `analyse_level.py` 固定赋值 | 章标题 `#` 深度 = `toc_level + 0` |
| `ParagraphLayout.level = 1..5` | 章内标题布局 | `split_by_cv` 按高度聚类 | 小节标题深度 = `toc_level + level` |

#### 4.4.4 代码实践

**实践目标**：观察一章内标题布局的 level 分布，验证「字号大 → level 小」。

**操作步骤**（示例代码）：

```python
# levels.py —— 示例代码
from pathlib import Path
from xml.etree import ElementTree as ET

f = Path("package/chapters/chapter_1.xml")  # 任选一章
body = ET.parse(f).getroot().find("body")

for el in body:
    if el.tag == "paragraph" and el.get("ref") in ("title", "sub_title"):
        # 块高度中位数（与源码算法一致）
        heights = [
            int(b.get("det").split(",")[3]) - int(b.get("det").split(",")[1])
            for b in el.findall(".//block")
        ]
        heights.sort()
        med = heights[len(heights) // 2] if heights else -1
        print(f"level={el.get('level')} ref={el.get('ref')} 高度中位数={med}")
```

**需要观察的现象**：输出的 `level` 是否与高度中位数单调对应（高度越大 level 越小）；`chapter_N.xml` 的第一个标题布局是否 `level=0`。

**预期结果**：第一个标题布局 `level=0`；其余标题按高度聚类分到 1～N 级，同级高度接近。若一章内只有章标题（无小节），则只有一个 level=0。注意：`block` 元素的实际属性布局以 u5-l1 的编解码为准，若 `det` 取法不符，请对照该章 `paragraph` 元素的实际 XML 微调脚本，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_collect_heights` 跳过 `i == 0` 的布局而不是把它也放进聚类？

**答案**：第一个布局是章标题，地位特殊且唯一——它的 level 语义固定为 0（本章在文档结构中的锚点）。若参与聚类，它的（通常最大的）高度会拉高聚类分割点，可能把原本同属一级的小节标题错分成两组。

**练习 2**：`split_by_cv` 为什么返回前要按组均值排序？`analyse_chapter_internal_levels` 又为什么 `reversed`？

**答案**：排序保证输出顺序确定（从最小字号组到最大字号组），`reversed` 后变成从大到小。`enumerate(start=1)` 依此赋 1、2、…，于是字号最大（层级最高）的标题拿到最小 level，与「level 越小标题越浅」的全书约定一致。两步配合把「聚类结果」翻译成「层级语义」。

**练习 3**：一章内出现了 7 种明显不同的标题字号，`_MAX_TITLE_GROUP = 5` 会怎样？

**答案**：分裂循环最多进行到 5 组即止（`while len(groups) < max_groups`），最接近的字号段会被合并进同一组、共享同一 level。这是对 Markdown 6 级标题上限的主动妥协：超过 5 级的章内层级在 Markdown 里本就无处安放。

## 5. 综合实践

把本讲三个模块串成一个完整实验：**用手工修改 `toc.xml` 的方式，重新划分一本书的章节**。

前置：一个已完成提取的 `DocumentPackage`（`package_path` 含 `ocr/`、`toc.xml`、`chapters/`，可用 u1-l4 的 `extract_pdf` + `package_path` 生成）。

**步骤**：

1. **基线统计**：运行 4.1.4 的脚本，记录文件数、每个文件的 `id`/`level`/`layouts` 数，确认是否存在 `chapter_head.xml`；用 4.4.4 的脚本记录 `chapter_1.xml` 的标题 level 分布。
2. **备份**：`cp -r package/chapters package/chapters_backup`。
3. **改造目录**：用 4.2.4 的脚本从 `toc.xml` 删除一个二级条目；或者反过来，把某个一级条目的 `level` 属性改成与其父级相同（观察 `iter_toc` 摊平后切分是否受影响——思考：切分用 `level` 吗？）。
4. **重算章节**：直接调用 `generate_chapter_files(pages_path=package/"ocr", chapters_path=package/"chapters", toc=decode(...))`，不触碰 OCR 缓存。
5. **对比**：再次运行统计脚本，与基线对照：
   - 被删条目的内容并入了哪一章（`layouts` 数的变化）；
   - 文件名的 id 空洞；
   - head 章是否不变；
   - 被删标题现在的 `level`（应被 4.4 分析为上一章的章内标题）。
6. **思考题**：如果把 `ocr/` 里某页的 `page_N.xml` 删掉再重算（不重新 OCR），会发生什么？（提示：`XMLReader` 读不到该页，该页内容从布局流中消失，但不会报错——`decode` 是逐文件流式的。）

**预期结果**：整个过程零 OCR、零 token 消耗、秒级完成——这正是「`chapters/` 是派生物、`ocr/` + `toc.xml` 才是缓存」架构的直接红利。具体数值**待本地验证**。

## 6. 本讲小结

- 章节边界由**坐标匹配**决定：标题段落（`ref in TITLE_TAGS`）的任一块坐标 `(page_index, order)` 命中目录条目，即开启新章；目录页本身被排除在布局流之外。
- `_generate_chapters` 是「持有—切换—冲刷」生成器状态机；目录树经 `iter_toc` 摊平成 `ref2toc` 字典后，层级结构对切分不再重要。
- 第一个目录命中之前的内容归入 `id=None` 的 head 章，写为 `chapter_head.xml`（根元素无 `id` 属性）；其 `level` 取目录最大层级，防止前置内容在渲染时盖过真正章节标题。
- `chapters/` 每次全删重算，`ocr/` 与 `toc.xml` 是缓存层——手改 `toc.xml` 后直接重跑 `generate_chapter_files` 即可零成本重新划分章节。
- 章内小节层级由 `analyse_chapter_internal_levels` 用标题块**检测框高度的 CV 聚类**（`split_by_cv`，阈值 0.025，最多 5 组）确定：章标题 level=0，小节 1～5 级，字号越大 level 越小。
- 写盘前固定两步处理：标点归一化（改文本）→ 章内层级分析（改 level），随后 `encode` + `save_xml` 落盘。

## 7. 下一步学习建议

本讲产出的 `chapter_N.xml` 里，正文块的脚注标记已被替换为 `Reference` 对象，标点也已归一化——但这两个机制我们都只点了位置。下一讲 **u5-l4 脚注引用与标点归一化** 将精读：

- `pdf_craft/extractor/chapter/reference.py` 与 `mark.py`：`search_marks` 如何识别正文中的引用标记，`References.get` 如何按 `(page_index, order)` 对应脚注；
- `pdf_craft/extractor/chapter/punctuation.py`：`_normalize_segments` 的近邻汉字判定与半角→全角映射规则。

读完 u5 单元即完成了提取链路的全部精读，之后可以带着对 `DocumentPackage` 的完整理解进入 u6（文档包与渲染），看 `chapter_N.xml` 如何变成 Markdown 与 EPUB。
