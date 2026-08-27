# u9-l2 EPUB 适配层：ZIP、spine 与文本修正

## 1. 本讲目标

上一讲（u9-l1）我们沿着 `translate()` 主函数走完了 `translate_epub` 的全流程：双句柄 Zip、mimetype 首条目、三类翻译任务、5%/5%/90% 进度权重。当时我们把很多调用当作「黑盒」——`Zip` 是什么？`search_spine_paths` 怎么知道一本书有哪些章节？翻译完的目录标题为什么要做一次「法语引号解包」？`XMLInterrupter` 这个被塞进 `translate_elements` 的拦截器到底拦了什么？

学完本讲，你应该能够：

1. 说清 EPUB 的容器格式：它是一个 ZIP 包，`META-INF/container.xml` 指向 OPF 元数据文件，OPF 里的 `manifest` + `spine` 定义阅读顺序，目录则有 EPUB 2 的 NCX 与 EPUB 3 的 nav 两种形态。
2. 读懂适配层五个模块的分工：`Zip`（双句柄读写）、`common`（找 OPF、剥命名空间）、`spines`（spine 检索）、`toc`（目录读写）、`metadata`（元数据读写）。
3. 解释 `unwrap_french_quotes` 的字符映射规则，以及为什么它只作用于目录和元数据、不碰章节正文。
4. 描述 `XMLInterrupter` 的三个钩子如何在「送译前」把 MathML 公式换成 LaTeX 占位符、在「写回时」换回原始 MathML。

## 2. 前置知识

### 2.1 EPUB 到底长什么样

把一个 `.epub` 文件改名为 `.zip` 解压，你会看到这样的目录树：

```text
mimetype                          ← 内容固定为 "application/epub+zip"，必须是 ZIP 第一个条目且不压缩
META-INF/container.xml            ← 指路牌：告诉阅读器 OPF 文件在哪
OEBPS/content.opf                 ← 图书的"户口本"：manifest（清单）+ spine（阅读顺序）+ metadata（元数据）
OEBPS/chapter1.xhtml              ← 正文章节（XHTML 文档）
OEBPS/toc.ncx                     ← EPUB 2 目录
OEBPS/nav.xhtml                   ← EPUB 3 目录（properties="nav"）
```

四个关键术语：

- **container.xml**：ZIP 内的固定路径 `META-INF/container.xml`，其中的 `<rootfile full-path="...">` 指向 OPF。它是进入一本书的唯一确定入口。
- **OPF**（Open Packaging Format）：图书的核心描述文件，`<manifest>` 列出包内所有资源（id、href、media-type），`<spine>` 用一串 `<itemref idref="...">` 声明「按什么顺序读这些文档」。
- **spine**：阅读顺序列表。翻译一本 EPUB，本质上是翻译 spine 指向的每一个 XHTML/HTML 文档的 `<body>`。
- **NCX / nav 目录**：EPUB 2 用 `.ncx` 文件（`navMap/navPoint` 结构），EPUB 3 用 XHTML 里的 `<nav type="toc">`（`ol/li/a` 结构）。两种形态差异很大，适配层必须都支持。

### 2.2 XML 命名空间与 ElementTree

OPF 和 container.xml 都是带命名空间的 XML，例如 `<xmlns:opf="http://www.idpf.org/2007/opf">`。Python 标准库 `xml.etree.ElementTree`（下称 ET）解析后，标签会变成 `{命名空间}tag` 的形式——直接 `find("manifest")` 找不到东西。所以适配层有一个 `strip_namespace` 工具先把命名空间剥掉，之后的查找就能用裸标签名了。

### 2.3 MathML：EPUB 里的数学公式

EPUB 3 允许用 MathML 写公式：

```xml
<p>速度 <math><mi>v</mi><mo>=</mo><mi>s</mi><mo>/</mo><mi>t</mi></math> 是位移与时间之比。</p>
```

对翻译管线来说这是麻烦事：把 MathML 摊平成文本片段后，得到的是 `v`、`=`、`s`、`/`、`t` 这样一堆碎渣——送进 LLM 会被当成外文正文翻译掉，公式就毁了。本讲的「片段拦截器」就是为解决这个问题而生的。

### 2.4 与前面讲义的关系

- u7-l3 讲过 `TextSegment`（文本、父元素栈、深度等字段）——拦截器直接操作它，本讲用到时再回顾。
- u9-l1 讲过 `translate()` 主流程与 `SubmitKind`，本讲只在其挂钩点处引用，不重复展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/pipeline/epub/adapter/zip.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py) | `Zip` 类：同时打开源（只读）与目标（只写）两个 ZIP，按文件粒度「改写谁、迁移谁」，退出时兜底复制未处理文件 |
| [pdf_craft/pipeline/epub/adapter/common.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py) | `find_opf_path`（经 container.xml 定位 OPF）与 `strip_namespace`（递归剥命名空间） |
| [pdf_craft/pipeline/epub/adapter/spines.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/spines.py) | `search_spine_paths`：解析 OPF 的 manifest + spine，产出（文档路径, media-type）流 |
| [pdf_craft/pipeline/epub/adapter/toc.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py) | `Toc` 数据类 + `read_toc`/`write_toc`，兼容 EPUB 2 NCX 与 EPUB 3 nav 两种目录 |
| [pdf_craft/pipeline/epub/adapter/metadata.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py) | `MetadataField` + `read_metadata`/`write_metadata`，按 tag 分桶回写 OPF 元数据 |
| [pdf_craft/pipeline/epub/adapter/math.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/math.py) | 手写的 MathML→LaTeX 转换器 `xml_to_latex`（注意：当前主链路未导入它，拦截器实际用的是 `mathml2latex` 三方库，此文件可视为备用实现） |
| [pdf_craft/pipeline/epub/translation/punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py) | `unwrap_french_quotes`：对元素的 text 与 tail 做引号映射（剥外层引号、内层单引号升级） |
| [pdf_craft/pipeline/epub/translation/xml_interrupter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py) | `XMLInterrupter`：三个钩子，送译前把 MathML 换成 LaTeX 占位、写回时换回原始 MathML |
| [pdf_craft/pipeline/epub/translation/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py) | u9-l1 的主角；本讲关注它**在哪里**调用适配层与两个修正机制 |
| [pdf_craft/pipeline/epub/translation/epub_transcode.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py) | 把 `Toc`/`MetadataField` 编码成可翻译的 XML 元素（`toc-list`/`metadata-list`），译后解码回来 |

## 4. 核心概念与源码讲解

### 4.1 EPUB 适配层：Zip 双句柄与容器解剖

#### 4.1.1 概念说明

「适配层」（adapter）解决的问题是：**翻译引擎 XMLTranslator 是格式无关的，它只认识 XML 元素流；而 EPUB 是一个带规范约束的 ZIP 容器。** 两者之间需要一个翻译官——把「一本书」拆解成「一批待翻译的 XML 元素」，再把译文装回一个合法的 EPUB 包。

适配层的核心设计是一个 `Zip` 双句柄结构：**源包只读、目标包只写，永不原地修改**。这带来两个好处：

1. **失败安全**：翻译中途崩溃，源文件完好无损。
2. **按需改写**：只有被翻译的文件走「读原文 → 写译文」，其余文件（图片、CSS、字体）原样迁移字节。

这一点与第 u7-l1 讲 `ChapterPackageTransformer` 的「复制—改写」策略是同一个哲学：不可变输入、派生输出。

#### 4.1.2 核心流程

适配层在 `translate()` 中的工作顺序（承接 u9-l1）：

```text
打开双句柄 Zip(source, target)
  │
  ├─ migrate(mimetype)              ← 必须第一个迁移，借源 ZipInfo 保留"不压缩"属性
  │
  ├─ search_spine_paths(zip)        ← 解析 container.xml → OPF → manifest+spine
  │     └─ yield (章节路径, media-type) 流
  │
  ├─ read_toc(zip)                  ← 探测 EPUB 版本 → 找 NCX 或 nav → 解析出 list[Toc]
  │
  ├─ read_metadata(zip)             └─ 解析 OPF <metadata>，过滤 SKIP_FIELDS
  │
  ├─ （翻译循环中）zip.replace(章节路径)   ← 把译文写进目标包
  │
  └─ __exit__：把所有未处理文件兜底迁移到目标包，关闭双句柄
```

找 OPF 的链路是理解整个适配层的钥匙：

```text
META-INF/container.xml
    └─ <rootfile full-path="OEBPS/content.opf">   ← 唯一确定入口
            └─ OEBPS/content.opf
                ├─ <manifest><item id href media-type/>…   ← 资源清单（字典：id → (href, media-type)）
                ├─ <spine><itemref idref/>…                 ← 阅读顺序（查 manifest 字典得路径）
                └─ <metadata><dc:title>…</metadata>          ← 书名、作者等
```

#### 4.1.3 源码精读

**① Zip 双句柄的建立与兜底迁移**

[zip.py:L8-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L8-L23) 定义 `Zip` 的构造：同时打开源（`"r"`）与目标（`"w"`，`ZIP_DEFLATED` 压缩），任一打开失败就关掉已打开的那个再抛异常（不留半开句柄）；`_processed_files` 集合记录「已被处理过的文件路径」，这是后面兜底迁移的去重账本。

[zip.py:L25-L41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L25-L41) 是上下文管理器协议：`__exit__` 里只有**没有发生异常**（`_exc_type is None`）时，才遍历源包全部文件，跳过目录条目（以 `/` 结尾），把不在 `_processed_files` 里的文件逐一 `migrate`——这就是「兜底迁移」：你只需要显式处理要翻译的文件，图片、CSS 等剩余资产在退出时自动复制。最后无论成败都关闭两个句柄，且 `return False` 表示不吞异常。

**② migrate 为什么能保住 mimetype 的「不压缩」属性**

[zip.py:L52-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L52-L62) 的 `migrate` 从源包取出该条目的 `ZipInfo` 元数据对象，读出字节后用 `zinfo_or_arcname=source_info` 写入目标包，并显式传 `compress_type=source_info.compress_type`。EPUB 规范要求 `mimetype` 必须是 ZIP 第 0 条目且**不得压缩**——虽然目标包默认用 `ZIP_DEFLATED`，但借用源 `ZipInfo` 后该条目保留自己的 `ZIP_STORED` 压缩方式。这正是 u9-l1 里 `translate()` 第一步就 `zip.migrate(Path("mimetype"))` 的原因。

**③ read 与 replace：两个方向的文件句柄**

[zip.py:L64-L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L64-L69) 一共六行：`read` 返回源包的只读句柄，`replace` 返回目标包的同路径只写句柄，**并把路径记入 `_processed_files**`——被「替换」的文件即视为已处理，退出时不会再被兜底迁移覆盖。另外 [zip.py:L43-L50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L43-L50) 的 `list_files(prefix_path)` 支持按目录前缀列出源包文件，前缀会补齐 `/`。

**④ find_opf_path：两步定位 OPF**

[common.py:L8-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py#L8-L27) 读取固定路径 `META-INF/container.xml`，先带命名空间查 `rootfile`，查不到再退化为裸标签查找（兼容命名空间写法各异的文件），两次都落空或缺少 `full-path` 属性则抛 `ValueError`。旁边的 [common.py:L30-L35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py#L30-L35) `strip_namespace` 递归把 `{ns}tag` 截成 `tag`，供后续解析 OPF/NCX 使用。

**⑤ search_spine_paths：manifest 字典 + spine 顺序表**

[spines.py:L10-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/spines.py#L10-L43) 是一个生成器：定位 OPF 后，先把 `<manifest>` 的所有 `<item>` 收进字典 `manifest_items[id] = (href, media_type)`；再遍历 `<spine>` 的每个 `<itemref idref>`，用 idref 查字典拿到 href，**只放行 `application/xhtml+xml` 与 `text/html` 两种 media-type**，并拼接 `opf_dir / href` 把相对路径补全为 ZIP 内路径。生成器形态意味着调用方（如 `translate()` 里统计章节数的 `sum(1 for _, _ in search_spine_paths(zip))`）可以零成本先数个数、再迭代取任务。

**⑥ read_toc：同一棵 Toc 树，两种物理形态**

[toc.py:L11-L41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L11-L41) 的 `Toc` dataclass 文档字符串本身就是一张对照表——EPUB 2 的 `navPoint` 与 EPUB 3 的 `nav li/a` 各字段如何映射到统一的 `title/href/fragment/id/children`。[toc.py:L51-L67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L51-L67) 的 `read_toc` 先用 [toc.py:L80-L90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L80-L90) `_detect_epub_version`（读 OPF 根元素的 `version` 属性，`3` 开头即 EPUB 3）分派到两条解析路径：EPUB 2 走 [toc.py:L126-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L126-L137) 的 `_read_ncx_toc`（递归解析 `navMap/navPoint`），EPUB 3 走 [toc.py:L254-L275](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L254-L275) 的 `_read_nav_toc`（找 `type="toc"` 的 `nav` 再遍历 `ol/li`）。目录文件本身的定位在 [toc.py:L93-L123](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L93-L123)：EPUB 2 找 `media-type="application/x-dtbncx+xml"` 的 manifest 项，EPUB 3 找 `properties` 含 `nav` 的项。

回写时的难点是「译文条目与原 XML 节点配对」，[toc.py:L430-L473](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L430-L473) 的 `_match_toc_with_elements` 用三级策略：先按 id 匹配、再按 href 匹配、剩下的按位置 zip 配对——尽力保住原节点的属性与结构。

**⑦ read_metadata：跳过技术性字段**

[metadata.py:L21-L29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L21-L29) 定义了 `SKIP_FIELDS`：`language`、`identifier`、`date`、`meta`、`contributor` 不参与翻译——语言代码、UUID、日期翻了反而坏。[metadata.py:L32-L55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L32-L55) 的 `read_metadata` 在 OPF 根的直接子元素里找 tag 以 `metadata` 结尾的元素，收集「有非空文本且不在跳过名单」的字段为 `MetadataField(tag_name, text)`。[metadata.py:L58-L85](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L58-L85) 的 `write_metadata` 按 tag 名分桶，用计数器把译文依次填回同 tag 的原节点——书名译者、多作者都能各归其位。

**⑧ 谁在使用适配层**

[translator.py:L73-L82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L73-L82) 是 u9-l1 已见过的开场：`with Zip(...)` 打开双句柄、首个迁移 `mimetype`，随后一行内完成「盘点三章」——`search_spine_paths` 数章节数、`read_toc` 读目录、`read_metadata` 读元数据。而 [translator.py:L172-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L172-L187) 的 `_generate_tasks_from_book` 再次迭代 spine，把每个文档的 `<body>` 包成 `TranslationTask`。章节任务与目录/元数据任务的**编码方式不同**：目录与元数据先经 [epub_transcode.py:L58-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L58-L65) 与 [epub_transcode.py:L80-L89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L80-L89) 编码成单个 `toc-list`/`metadata-list` 元素，章节则直接用文档本身的 `body`。

#### 4.1.4 代码实践

**实践目标**：不用 LLM，直接用适配层把一个真实 EPUB 的骨架「解剖」出来——列出全部 spine 文档路径、目录条目和可翻译元数据字段。

**操作步骤**：

1. 在仓库根目录新建 `inspect_epub.py`（示例代码，自行创建，勿放进 `pdf_craft/` 包内）：

   ```python
   # 示例代码
   from pathlib import Path
   from tempfile import TemporaryDirectory

   from pdf_craft.pipeline.epub.adapter import (
       Zip, read_metadata, read_toc, search_spine_paths,
   )

   epub_path = Path("tests/assets/epub/Cambridge.epub")

   def dump_tree(toc_list, indent=0):
       for toc in toc_list:
           print("  " * indent + f"- {toc.title!r} -> {toc.full_href}")
           dump_tree(toc.children, indent + 1)

   with TemporaryDirectory() as tmp:
       # Zip 需要同时提供源路径与目标路径（双句柄，构造即打开目标包）
       with Zip(epub_path, Path(tmp) / "copy.epub") as zip:
           print("== spine 文档（按阅读顺序）==")
           for path, media_type in search_spine_paths(zip):
               print(f"  {path}  [{media_type}]")

           print("== 目录树 ==")
           toc_list, toc_context = read_toc(zip)
           print(f"  (EPUB {toc_context.version}, 文件: {toc_context.toc_path})")
           dump_tree(toc_list)

           print("== 可翻译元数据字段 ==")
           fields, _ = read_metadata(zip)
           for field in fields:
               print(f"  <{field.tag_name}> {field.text!r}")
   ```

2. 确认已按 u1-l2 安装 pdf-craft（本实践不需要 OCR 与 LLM 凭据）。
3. 运行 `python inspect_epub.py`。
4. 把 `epub_path` 换成 `tests/assets/epub/` 下的其他书（如 `The little prince.epub`、`治疗精神病.epub`）再跑一次，对比目录形态。

**需要观察的现象**：

- spine 列表的顺序就是这本电子书的阅读顺序；条目路径是 ZIP 内路径（如 `OEBPS/Text/chapter1.xhtml`）。
- `toc_context.version` 告诉你这本书是 EPUB 2（NCX）还是 EPUB 3（nav）。
- 元数据里看不到 `language`、`identifier`、`date` 等字段——它们在 `SKIP_FIELDS` 名单里。
- 退出 `with` 块后，临时目录里的 `copy.epub` 是一份完整副本（兜底迁移生效）。

**预期结果**：三段清单打印出来，且脚本不修改源 EPUB。（各本书的具体条目数待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果直接用 `zipfile.ZipFile(epub_path, "r")` 只读打开来做这个实验，代码会简单很多。为什么适配层仍要设计成双句柄？

**答案**：翻译的本质是「产出新包」而不是「原地改包」。`zipfile` 不支持原地修改条目，标准做法只能是重写整个包；`Zip` 把这件事显式化——源只读、目标只写，任何时刻中断都不会损坏源文件，且翻译过的文件用 `replace` 写入、未翻译的由 `__exit__` 兜底迁移，职责清晰。

**练习 2**：`search_spine_paths` 里为什么要检查 `media_type in ("application/xhtml+xml", "text/html")`？

**答案**：spine 理论上只应指向文档类资源，但真实世界的书可能有脏数据。图片、CSS 即使混进 spine 也不是翻译对象（它们没有可翻译文本），过滤掉可以避免把二进制资源当 XHTML 解析而崩溃。

**练习 3**：`Zip.list_files()` 与 `search_spine_paths()` 都能「列出文件」，区别是什么？

**答案**：`list_files` 是物理视角——ZIP 里有什么条目就列什么（还可按前缀过滤）；`search_spine_paths` 是语义视角——按 OPF 声明的**阅读顺序**只列出可作为章节翻译的文档，并附带 media-type。前者是容器操作，后者是规范解析。

### 4.2 标点修正：unwrap_french_quotes 引号解包

#### 4.2.1 概念说明

翻译目录标题和元数据时会遇到一个排版尴尬：外文书喜欢用引号或书名号把标题包起来（法语的 `« … »`、中文的`《 … 》`），翻译成目标语言后，这层「包裹」往往变成噪音——LLM 输出的译标题里会残留、误用或半翻译这些符号，写回目录后观感很糟。

`unwrap_french_quotes`（解包法语引号）做一次字符级清理，规则只有三条：

1. **双层包裹符直接删除**：`«`、`»`、`《`、`》` 映射为空字符串；
2. **单层符升级**：`‹`→`«`、`›`→`»`、`〈`→`《`、`〉`→`》`——原本嵌套在内的单层引号「顶替」被删掉的外层，符合中文出版规范（外层双书名号、内层单书名号；外层拿掉后内层升为双）；
3. **其余字符原样保留**。

例如 `《红楼梦〈上〉》` 解包后是 `红楼梦《上》`——外层书名号没了（标题本身就是标题，不需要再包裹），内层单书名号升级为双书名号。

#### 4.2.2 核心流程

```text
输入：一个已翻译的 XML 元素（toc-list 或 metadata-list）
  │
  ├─ iter_with_stack(element)          ← 先序遍历，逐个访问元素
  │
  ├─ 对每个元素：
  │     ├─ element.text 含字符？ → 按映射表逐字符重写
  │     └─ element.tail 含字符？ → 按映射表逐字符重写
  │
  └─ 返回同一元素（原地修改）
```

注意它**同时处理 text 与 tail**。ET 里一段 `«a» <b/> «c»` 中，`«c»` 是 `<b/>` 的 tail 而不是任何元素的 text——只处理 text 会漏掉一半。

#### 4.2.3 源码精读

**① 映射表：九个字符的排版规则**

[punctuation.py:L5-L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L5-L16) 用字典 `_QUOTE_MAPPING` 完整声明了全部行为：法语双引号 `«»` 与中文双书名号 `《》` 映射为 `""`（删除），法语单引号 `‹›` 与中文单书名号 `〈〉` 分别升级为对应的双符号。规则是数据不是代码，想扩展（比如处理日文 `「」`）只需加一行映射。

**② 生成器式的字符流处理**

[punctuation.py:L19-L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L19-L25) 的 `_strip_quotes` 是一个生成器：逐字符查表，查不到（`None`）就原样 `yield`，查到空串就丢弃，查到新字符就 `yield` 映射值。三个分支恰好对应「保留 / 删除 / 替换」三种命运。

**③ 遍历与改写**

[punctuation.py:L28-L34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L28-L34) 的 `unwrap_french_quotes` 用 [xml.py:L22-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/xml.py#L22-L36) 的 `iter_with_stack` 做先序遍历（这个通用遍历器来自格式无关的 xml_translator 工具箱），对每个元素分别重写 `text` 与 `tail`，最后原样返回元素。

**④ 挂钩点：只出现在两个分支里**

在 `translate()` 的写回循环中，[translator.py:L114-L118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L114-L118)（TOC 分支）与 [translator.py:L124-L128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L124-L128)（METADATA 分支）都在「拿到译文元素之后、解码写回之前」调用 `unwrap_french_quotes(translated_elem)`；而 [translator.py:L134-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L143)（CHAPTER 分支）没有这一步。

**为什么只对目录和元数据生效？** 三个原因：

1. **内容性质不同**。目录标题、书名、作者名是「短而独立」的字段串，外层引号只是包裹字段的原排版残留，删掉无损语义；章节正文里的引号是**内容本身**——对话、引文、作品名，一刀切删除会破坏文义。
2. **时机不同**。它作用于 LLM 输出之后，是对「标题类译文」的兜底清洗——LLM 翻译 `《...》` 包裹的短标题时极易残留或错配符号；正文中的引号由 LLM 在上下文中自行正确处理，不需要也不应该机械清洗。
3. **结构不同**。目录/元数据被编码成扁平的 `toc-list`/`metadata-list` 单元素（见 [epub_transcode.py:L58-L89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L58-L89)），在这种浅结构上做全局字符映射是安全的；章节是深结构 HTML，且其中还混着公式占位符（下一节的拦截器），粗暴的全树字符改写风险大于收益。

#### 4.2.4 代码实践

**实践目标**：为 `unwrap_french_quotes` 写三个断言测试，覆盖「`《》` 包裹标题」「正文对话」「嵌套书名号」三种情况，并回答它为何只对目录和元数据生效。

**操作步骤**：

1. 在仓库根目录新建 `test_unwrap.py`（示例代码，可用 `python -m pytest test_unwrap.py` 或 `python test_unwrap.py` 运行）：

   ```python
   # 示例代码
   import unittest
   from xml.etree.ElementTree import Element, SubElement

   from pdf_craft.pipeline.epub.translation.punctuation import unwrap_french_quotes


   def toc_list_with(*titles: str) -> Element:
       root = Element("toc-list")          # 模拟 epub_transcode.encode_toc_list 的产物
       for title in titles:
          item = SubElement(root, "toc-item")
          t = SubElement(item, "title")
          t.text = title
       return root


   class TestUnwrapFrenchQuotes(unittest.TestCase):
       def test_wrapped_title(self):
           # 情况一：《》包裹的标题 —— 外层整体剥掉
           root = toc_list_with("《The Little Prince》")
           unwrap_french_quotes(root)
           self.assertEqual(root[0][0].text, "The Little Prince")

       def test_french_quotes_in_body(self):
           # 情况二：正文式文本（法语引号包裹的短语）—— 同样被剥掉
           root = toc_list_with("Il dit «bonjour» puis part")
           unwrap_french_quotes(root)
           self.assertEqual(root[0][0].text, "Il dit bonjour puis part")

       def test_nested_marks(self):
           # 情况三：嵌套 —— 外层删除，内层单书名号升级为双书名号
           root = toc_list_with("《红楼梦〈上〉》")
           unwrap_french_quotes(root)
           self.assertEqual(root[0][0].text, "红楼梦《上》")


   if __name__ == "__main__":
       unittest.main()
   ```

2. 运行测试，三个断言应全部通过。
3. 追加第四个用例验证 tail 也会被处理：构造 `<toc-item><title>a</title><title>b</title></toc-item>`，把 `»` 放在第一个 `title` 的 `tail` 里（`first_title.tail = "» tail 文本"`），断言解包后 tail 以 ` tail 文本` 开头。
4. 写 100 字左右的笔记回答：为什么 [translator.py:L114-L118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L114-L118) 与 [translator.py:L124-L128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L124-L128) 调用了 `unwrap_french_quotes`，而 CHAPTER 分支（[translator.py:L134-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L143)）没有？

**需要观察的现象**：三个断言全绿；tail 用例也通过；练习 4 的答案落在 4.2.3 末段给出的三个理由上（字段串 vs 内容、译后清洗的定位、浅结构 vs 深结构）。

**预期结果**：测试通过。断言的期望值可先人工推演映射表再对照运行结果（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`_QUOTE_MAPPING` 里 `‹` 映射到 `«` 而不是被删除，为什么？

**答案**：删除的是「最外层包裹」；单层引号通常出现在双层之内（`«a ‹b› c»`）。外层剥掉后，内层引号仍是有效的引用语义，按出版规范升级为双层（`a «b» c`）而不是丢弃，信息不丢失。

**练习 2**：如果想让日文引号 `「」` 也被解包，改哪里？

**答案**：在 [punctuation.py:L5-L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L5-L16) 的字典里加映射即可（例如 `"「": ""`、`"」": ""`，若存在内层 `『』` 则映射到 `「」`）——规则是纯数据，`_strip_quotes` 与 `unwrap_french_quotes` 无需改动。

**练习 3**：这个函数为什么返回元素本身，而不是返回新元素？

**答案**：它是**原地修改**（in-place）：调用点紧接着就把同一个元素交给 `decode_toc_list`/`decode_metadata`，没有必要复制；这也与 `Zip` 的「改写谁就写谁」的流式风格一致。

### 4.3 片段拦截器：XMLInterrupter 与公式保护

#### 4.3.1 概念说明

u7-l3 讲过，翻译引擎把 XML 树摊平成 `TextSegment` 流（每个片段 = 一段文本 + 它的父元素栈）。EPUB 章节里的 MathML 公式被摊平后会成为一堆无意义碎片（`v`、`=`、`s`、`/`、`t`），直接送译有两个灾难性后果：

1. LLM 把公式碎片当外文正文「翻译」，公式被毁；
2. 公式碎片混入正常文本，污染分词与分组。

`XMLInterrupter`（片段拦截器）的职责是在**片段流的三个关口**做调包：

| 关口 | 钩子 | 做什么 |
| --- | --- | --- |
| 送译前（源片段流） | `interrupt_source_text_segments` | 把同一 `<math>` 元素下的所有片段**截走**，换成一个 `<expression id="N">` 占位片段，文本是渲染好的 LaTeX（如 ` $v=s/t$ `） |
| 译后（译文片段流） | `interrupt_translated_text_segments` | 译文流里遇到占位片段时，把**原始 MathML 片段**嫁接回译文树的对应位置 |
| 译后（块级元素） | `interrupt_block_element` | 整块是公式时，把占位元素整体换回原始 `<math>` 元素 |

效果：LLM 看到的是「正文 + 简洁的 LaTeX 占位」，写回 EPUB 的是「译文 + 原封不动的 MathML」。公式既没被翻译坏，也不占 token。

这三个钩子不是 EPUB 管线的私货，而是 `XMLTranslator.translate_elements` 的公开参数——[xml_translator/translator.py:L79-L81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L79-L81) 声明了三个可选回调。格式无关的引擎通过依赖注入获得「拦截片段」的能力，EPUB 侧注入 MathML 专用实现。第 u11-l2 讲会看到的边界守卫测试 [test_module_boundaries.py:L12-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L17) 甚至断言 xml_translator 源码里不出现 `pipeline.epub`、`Zip(`、`search_spine_paths` 字样——架构上强制了这层隔离。

#### 4.3.2 核心流程

**送译前（源侧）的状态机**：

```text
对每个源 TextSegment：
  1. _interrupted_index：沿 parent_stack 找有没有 <math> 祖先
  2. 有 → 给该 math 元素分配 id（__XML_INTERRUPTER_ID），
         把片段存进 _raw_text_segments[id] 缓冲区（不立即 yield）
  3. 无 → 正常 yield
  4. 每当「当前拦截 id」发生变化（公式结束，回到正文）：
         把缓冲区里同 id 的片段合并成一个占位片段并 yield：
           - 新建 <expression id=N> 元素
           - 文本 = _render_latex(...) 渲染的 LaTeX
           - parent_stack = 原 math 之外的栈 + [占位元素]
         并记录 _placeholder2intercepted[id(占位)] = 原 math 元素
流结束时：最后一个公式的缓冲区也要冲刷
```

**LaTeX 渲染**（`_render_latex`）优先走三方库 `mathml2latex`，失败则退回「拼接原始文本 + 空白归一化」；行内公式包 `$...$`、块级公式包 `$$...$$`，两侧留空格与正文隔开。

**译后（译文侧）的还原**：

```text
对每个译文 TextSegment：
  - 父元素没有 id 属性 → 原样 yield（普通译文）
  - 父元素是占位 <expression id=N> 且是行内 → 弹出 _raw_text_segments[N]，
        把原始 MathML 片段的栈接到译文占位元素的栈上，逐个 yield
  - 父元素是占位且是块级（占位即块父）→ 丢弃该译文片段（隐藏 LaTeX 文本）
        块级公式的还原走另一个钩子 interrupt_block_element：
        按 id() 在 _placeholder2intercepted 里查到原 math 元素，整体替换
```

#### 4.3.3 源码精读

**① 私有记号与三个缓冲区**

[xml_interrupter.py:L12-L14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L12-L14) 定义三个常量：`__XML_INTERRUPTER_ID`（打在 math 元素上的私有属性名）、`math`（要拦截的标签）、`expression`（占位标签）。[xml_interrupter.py:L17-L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L17-L22) 的构造函数里是三个状态：自增 id 计数器、当前正在拦截的公式 id（`_last_interrupted_id`）、以及两张表——`_placeholder2interrupted`（占位元素 `id()` → 原 math 元素）与 `_raw_text_segments`（公式 id → 被截走的原始片段列表）。

**② 源侧入口：截流与冲刷**

[xml_interrupter.py:L24-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L24-L33) 的 `interrupt_source_text_segments` 是一个生成器包装：逐段展开，流结束时若还有未冲刷的公式（`_last_interrupted_id` 非 None），补一次合并输出——否则最后一个公式的占位符永远出不去。

**③ 核心状态机**

[xml_interrupter.py:L50-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L50-L75) 的 `_expand_source_text_segment`：先用 [xml_interrupter.py:L134-L140](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L134-L140) 的 `_interrupted_index` 沿父栈找 `math` 祖先（找最外层的一个即停）；命中就登记 id 并缓冲片段；每当新片段的拦截 id 与上一个不同（进入新公式或回到正文），就把上一个公式的缓冲冲刷成一个占位片段。`interrupted_index is None` 的纯正文片段直接放行。

**④ 合并冲刷：构造占位片段**

[xml_interrupter.py:L77-L132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L77-L132) 的 `_pop_and_merge_from_buffered`：用缓冲片段构造 `<expression id=N>` 占位元素（若原 math 带 `display` 属性则透传，见 [const.py:L2](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/const.py#L2)），把占位元素接在「原父栈去掉 math 及其内部」的末尾，生成一个新的 `TextSegment`（文本为 LaTeX），并登记 `_placeholder2interrupted`。随后一段循环把缓冲片段的父栈**相对化**到 math 元素（栈截断、深度重算）——为译后还原时「嫁接回译文树」做准备。中间有一大段被注释掉的代码（[xml_interrupter.py:L106-L124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L106-L124)，TODO 标注），是关于「行内公式是否在译文中重复出现」的未完成实验，读代码时跳过即可。

**⑤ LaTeX 渲染与兜底**

[xml_interrupter.py:L142-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L142-L170) 的 `_render_latex`：用 `combine_text_segments` 把缓冲片段重组回 `<math>` 元素，克隆后清掉私有属性与 tail，序列化为 MathML 字符串，经 BeautifulSoup 喂给 `mathml2latex` 的 `process_mathml`；任何异常都被吞掉（`except Exception: pass`），失败时退回「拼接片段原始文本」。成功时按 [inline.py:L109-L116](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/inline.py#L109-L116) 的 `is_inline_element`（查 HTML 行内标签表与 `display` 属性）决定包 `$...$` 还是 `$$...$$`。公式渲染是「尽力而为」：渲染失败也只影响占位符观感，原始 MathML 始终安全地留在缓冲区里。

**⑥ 译后还原：两个关口**

[xml_interrupter.py:L172-L192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L172-L192) 的 `_expand_translated_text_segment`：弹出译文片段父元素上的 id 属性；没有 id 的是普通译文直接放行；有 id 且占位元素是**块父**（块级公式）则丢弃译文（LaTeX 文本不该出现在成品里）；否则把 `_raw_text_segments[id]` 里的原始片段逐个「接栈」后 yield——译文树里占位符的位置被原始 MathML 填充。而 [xml_interrupter.py:L41-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L41-L48) 的 `interrupt_block_element` 处理块级：按元素 `id()` 在 `_placeholder2interrupted` 查到原 math，整体换回。两个钩子都会顺手清掉 `__XML_INTERRUPTER_ID` 私有属性，避免泄漏进成品。

**⑦ 接线处**

[translator.py:L92-L103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L92-L103)：`translate()` 创建一个 `XMLInterrupter` 实例，把它的三个方法作为 `interrupt_*` 回调传入 `translate_elements`——引擎在摊平源片段、展开译文片段、处理块元素时分别回调，EPUB 专用的公式保护就这样挂进了格式无关的流水线。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——跟踪一个含行内公式的小节，写出「LLM 看到什么、成品里是什么」，并核对三个钩子的触发时机。

**操作步骤**：

1. 阅读下面的输入示例（示例代码，模拟一个 EPUB 章节 body）：

   ```xml
   <body>
     <p>设速度为 <math><mi>v</mi><mo>=</mo><mfrac><mi>s</mi><mi>t</mi></mfrac></math>，其中 s 是位移。</p>
   </body>
   ```

2. 对照 [xml_interrupter.py:L50-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L50-L75) 手工推演 `interrupt_source_text_segments` 的片段流：哪些片段被缓冲、何时冲刷。
3. 写出你推演的答案：送译的片段流形如
   - `设速度为 `（父栈 `[body, p]`）
   - ` $$或 $v=\frac{s}{t}$ `（父栈 `[body, p, expression[id=1]]`，占位片段）
   - `，其中 s 是位移。`（父栈 `[body, p]`）
4. 核对 [xml_interrupter.py:L172-L192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L172-L192)：译文流里父元素带 id 的片段发生什么？`_raw_text_segments` 里缓冲的 `v`、`=` 片段去哪了？
5. （可选，需本地可运行环境）写一个三行脚本验证「不含 math 的元素零损耗」：构造任意 `Element`，用 `iter_with_stack` 数出 `(text, tail)` 非空的节点数，再手动调用 `unwrap_french_quotes` 前后各数一次，确认节点数不变、只有字符被映射。

**需要观察的现象**：步骤 3 的推演结果里，公式碎片（`v`、`=`、`mfrac` 内部）**不出现在**送译片段流中；占位片段两侧带空格、行内公式带 `$` 定界。步骤 4 能说清「占位片段的译文被丢弃、原始片段接栈后放回」。

**预期结果**：推演答案与源码逻辑一致（LaTeX 具体渲染形式取决于 mathml2latex，占位符结构待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么占位片段的文本两侧要留空格（`f" {latex} "`）？

**答案**：占位符会与前后正文拼接成连续文本送给 LLM。不留空格时 `为$v=s/t$，` 这样的黏连既不利分词，也可能让 LLM 把 `$` 当普通标点处理；空格是廉价的边界信号。

**练习 2**：`_placeholder2interrupted` 的键为什么用 Python 内建 `id(元素)` 而不是自增的 `__XML_INTERRUPTER_ID` 字符串？

**答案**：两个键的用途不同。字符串 id 打在元素属性上，跟随片段流跨阶段传递（源侧标记、译后侧识别）；而 `interrupt_block_element` 拿到的是**译文树里的元素对象本身**，此时最可靠的对应关系是对象身份——占位元素对象在创建时（`_pop_and_merge_from_buffered` 里）就以 `id()` 登记了指向原 math 的映射，块级还原时直接查对象身份即可，不必依赖属性在后续处理中是否被保留。

**练习 3**：块级公式的译文片段在 `_expand_translated_text_segment` 里被 `return` 丢弃了（[xml_interrupter.py:L179-L182](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L179-L182)），那成品里的块级公式从哪来？

**答案**：从第三个钩子 `interrupt_block_element` 来。块级公式独占一个块，引擎在提交阶段逐块处理元素，该钩子按对象身份把 `<expression>` 占位元素整体替换回原始 `<math>` 元素——LaTeX 译文文本被丢弃，原始 MathML 完整回归。行内与块级走两条还原路径，分别覆盖「片段级嫁接」与「元素级替换」。

## 5. 综合实践

**任务：给你的「EPUB 体检报告」补上修正环节——做一次只读解剖 + 一次可运行的清洗验证。**

1. **解剖（实践 4.1.4 的延伸）**：对 `tests/assets/epub/` 下的每本书运行 `inspect_epub.py`，把结果整理成一张表：书名 | EPUB 版本 | 目录形态（NCX/nav）| spine 文档数 | 可翻译元数据字段数。观察哪本书是 EPUB 2、哪本是 EPUB 3。
2. **定位引号噪音**：在你解剖出的目录树与元数据字段里 grep 书名号与法语引号（`《》〈〉«»‹›`），记录哪些标题会被 `unwrap_french_quotes` 改写、改成什么样。
3. **验证清洗**：把第 2 步找到的真实标题填进 4.2.4 的测试用例（替换三个预制字符串），重跑断言，确认真实数据的清洗结果符合预期。
4. **追踪公式（若书中有 MathML）**：在某本含公式的书上，用 `zip.read(章节路径)` 打开一个 spine 文档，找到 `<math` 出现的章节，对照 4.3.4 的推演写出该章送译片段流的形状。
5. **收尾问题**：假设你要给 `translate()` 加一个「正文也做标点归一化」的选项，依据本讲源码说明：(a) 应该挂在哪个位置（对照 [translator.py:L134-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L143) 的 CHAPTER 分支）；(b) 为什么不能复用 `unwrap_french_quotes` 的删除规则；(c) 需要提防与 `XMLInterrupter` 占位符的什么冲突（提示：占位片段的 `$...$` 文本也是片段流的一员）。

本综合实践不修改任何源码，全部产物是脚本、表格与笔记。

## 6. 本讲小结

- **适配层是格式翻译官**：`Zip` 以「源只读、目标只写」的双句柄保证失败安全，`migrate` 借源 `ZipInfo` 保住 mimetype 第 0 条目且不压缩的规范约束，`__exit__` 兜底迁移所有未处理文件。
- **EPUB 解剖三步走**：`container.xml` → `find_opf_path` 定位 OPF；`manifest` 字典 + `spine` 顺序表 → `search_spine_paths` 产出章节流；OPF 根的 `version` 属性分派 NCX（EPUB 2）或 nav（EPUB 3）两条目录解析路径，统一为 `Toc` 树。
- **元数据按 tag 分桶回写**：`SKIP_FIELDS` 挡住语言/标识符/日期等技术字段，译文按 tag 名与计数器各归其位。
- **`unwrap_french_quotes` 是译后标题清洗**：双层包裹符（`«»`《》`）删除、单层（`‹›`〈〉`）升级，text 与 tail 都要处理；只作用于目录与元数据——它们是短字段串、浅结构、且引号只是排版残留，而正文中的引号是内容本身。
- **`XMLInterrupter` 用三个钩子保护公式**：源侧把同一 `<math>` 的片段截走并冲刷成 `<expression id=N>` 的 LaTeX 占位；译后行内走片段嫁接、块级走 `interrupt_block_element` 元素替换，原始 MathML 完整回归成品。
- **依赖注入维持架构边界**：三个 `interrupt_*` 回调是格式无关引擎 `XMLTranslator.translate_elements` 的公开参数，EPUB 侧注入实现——边界守卫测试甚至用字符串断言强制 xml_translator 不出现 EPUB 概念。

## 7. 下一步学习建议

本讲补完了 `translate_epub` 的全部地基，接下来两条路：

1. **进入 PDF 翻译与回写单元（u10）**：对照 EPUB 管线看另一条翻译工作流——`PDFTranslationPipeline` 如何从 DocumentPackage 生成替换列表、`PDFPatcher` 如何用 pypdf + reportlab 把译文按原版式叠回 PDF。特别建议对比「双句柄 Zip 的新包派生」与「patcher 的预检后落盘」两种失败安全策略。
2. **横向巩固工程化视角（u11）**：阅读 `tests/test_module_boundaries.py` 全文与 `tests/smoke/minimal.json`，理解本讲提到的架构守卫测试如何与冒烟矩阵配合，把「适配层/引擎分层」从约定变成可执行的断言。

若想继续深挖 EPUB 本身，可通读 [toc.py:L430-L473](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L430-L473) 的三级匹配策略，并思考：如果一本书的目录在翻译前后条目数不一致（LLM 增删了条目），`_match_toc_with_elements` 的位置兜底会怎样表现？
