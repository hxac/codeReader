# u9-l2 EPUB 适配层：ZIP、spine 与文本修正

## 1. 本讲目标

上一讲（u9-l1）我们沿 `translate()` 主函数走通了 EPUB 翻译的**编排层**：mimetype 首迁移、三类任务生成、进度权重、流式写回。本讲向下沉一层，拆开编排层脚下的三块基石：

1. **EPUB 适配层**（`adapter/` 包）：`Zip` 双句柄如何做到「边读源包、边写目标包」；`search_spine_paths` 如何从 OPF 里检索出全部正文文档；目录（NCX/nav）与元数据（OPF metadata）如何读写。
2. **标点修正**（`punctuation.py`）：`unwrap_french_quotes` 如何清洗翻译产出中的法语引号与书名号，以及它为何只作用于目录和元数据。
3. **片段拦截器**（`xml_interrupter.py`）：`XMLInterrupter` 如何通过三个钩子把 MathML 公式从翻译流中「拦截—占位—还原」。

学完本讲，你应当能够：

- 说清 EPUB 容器格式中 mimetype、`META-INF/container.xml`、OPF、manifest、spine、NCX/nav 各自的角色；
- 独立用 `Zip` + `search_spine_paths` 列出任意 EPUB 的全部 spine 文档；
- 解释 `unwrap_french_quotes` 的字符级映射规则，并说明它在管线中的应用边界；
- 追踪 `interrupt_source_text_segments` / `interrupt_translated_text_segments` / `interrupt_block_element` 三个钩子在翻译管线两端的调用点与协作方式。

## 2. 前置知识

### 2.1 EPUB 是一个有纪律的 ZIP

EPUB 遵循 OCF（Open Container Format）规范，本质是一个 ZIP 压缩包，但有三条「纪律」：

- **`mimetype` 必须是 ZIP 的第一个条目，且不压缩（stored）**。这样阅读器不必解压整个包，只读文件头就能识别文档类型；
- **`META-INF/container.xml`** 是固定入口，其中的 `<rootfile full-path="...">` 指向 OPF 文件（包的「户口本」）；
- **OPF 文件**（通常叫 `content.opf`）包含三块：`<metadata>`（书名、作者等元数据）、`<manifest>`（清单：本书包含哪些文件，每项有 `id`、`href`、`media-type`）、`<spine>`（书脊：按阅读顺序列出 manifest 中的文档 `idref`）。

一个典型 EPUB 的内部结构：

```text
mimetype                          ← 第一项、不压缩，内容固定为 "application/epub+zip"
META-INF/container.xml            ← 指向 OPF
OEBPS/content.opf                 ← metadata + manifest + spine
OEBPS/toc.ncx                     ← EPUB 2 的目录（NCX 格式）
OEBPS/nav.xhtml                   ← EPUB 3 的目录（nav 格式，二选一）
OEBPS/text/chapter01.xhtml        ← spine 中的正文文档
OEBPS/images/cover.jpg
```

**manifest 与 spine 的分工**：manifest 回答「有哪些文件」，spine 回答「按什么顺序读」。一本书的封面、字体可能在 manifest 里但不在 spine 里；翻译时只翻 spine 中的 XHTML/HTML 文档。

### 2.2 EPUB 2 与 EPUB 3 的目录差异

- EPUB 2 用 **NCX** 文件（`media-type="application/x-dtbncx+xml"`），目录树是嵌套的 `<navPoint><navLabel><text>`；
- EPUB 3 用 **nav 文档**（manifest 中 `properties="nav"`），目录是 `<nav type="toc">` 下的 `<ol><li><a>` 列表。

### 2.3 XML 命名空间与 ElementTree 的 `{ns}tag` 写法

OPF 文件根元素通常带命名空间。Python 标准库 ElementTree 解析后，标签名会变成 `{http://www.idpf.org/2007/opf}metadata` 这种带花括号的形式。本讲的 `strip_namespace` 就负责把这些前缀剥掉，让后续查找可以统一用 `"metadata"` 这种短名。

### 2.4 XML 的 text 与 tail

ElementTree 中一个元素的文本有两处：

- `element.text`：开标签与第一个子元素之间的文本；
- `element.tail`：闭标签与下一个兄弟元素之间的文本。

任何「遍历所有文本」的逻辑都必须同时处理两者，否则会漏掉一半文本。这个细节在 `unwrap_french_quotes` 里会直接看到。

### 2.5 回顾：TextSegment 与 parent_stack（来自 u7-l3）

XMLTranslator 把 XML 树摊平成 `TextSegment` 流（[segment/text_segment.py:L21-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L21-L27)），每个片段携带 `parent_stack`——从根到该文本节点的祖先元素链。本讲的拦截器正是靠检查 `parent_stack` 里有没有 `<math>` 祖先来判定「这段文本住在公式里」。

### 2.6 MathML 与 LaTeX

EPUB 3 的公式通常以 MathML（`<math><mrow><mi>x</mi>...`）嵌入。翻译管线的「通用语言」则是 LaTeX（`$x+1$` 行内、`$$...$$` 块级）。拦截器负责在两者之间搭桥。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/pipeline/epub/adapter/zip.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py) | `Zip` 双句柄：同时打开源/目标 ZIP，提供 migrate（原样迁移）、replace（改写）、read、兜底迁移 |
| [pdf_craft/pipeline/epub/adapter/common.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py) | `find_opf_path`（从 container.xml 定位 OPF）、`strip_namespace` 等工具 |
| [pdf_craft/pipeline/epub/adapter/spines.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/spines.py) | `search_spine_paths` 生成器：按 spine 顺序产出全部正文文档路径与媒体类型 |
| [pdf_craft/pipeline/epub/adapter/toc.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py) | `Toc` 数据结构与 NCX/nav 目录的读取、写回 |
| [pdf_craft/pipeline/epub/adapter/metadata.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py) | `read_metadata` / `write_metadata`：OPF 元数据的筛选与按位回填 |
| [pdf_craft/pipeline/epub/translation/epub_transcode.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py) | 转码层：`Toc` / `MetadataField` 与合成 XML 元素（`toc-list`、`metadata-list`）互转，桥接适配层与 XMLTranslator |
| [pdf_craft/pipeline/epub/translation/punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py) | `unwrap_french_quotes`：翻译产出后的引号清洗 |
| [pdf_craft/pipeline/epub/translation/xml_interrupter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py) | `XMLInterrupter`：公式的拦截、占位与还原 |
| [pdf_craft/pipeline/epub/translation/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py) | 上述模块的消费方（u9-l1 已精读，本讲只引用其接线点） |
| [pdf_craft/pipeline/epub/adapter/math.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/math.py) | 纯 Python 的 MathML→LaTeX 转换器；经全仓检索，当前主管线未引用它（拦截器实际走 mathml2latex），可视为备用实现 |
| [pdf_craft/transformer/xml_translator/xml/xml.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/xml.py) | `iter_with_stack` 先序遍历，`unwrap_french_quotes` 的遍历引擎 |
| [tests/test_module_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py) | 架构守卫：断言翻译引擎不认识 EPUB |
| tests/assets/epub/*.epub | 实践素材：`The little prince.epub`、`Cambridge.epub` 等真实电子书 |

**架构定位**：适配层存在的意义由架构守卫测试一锤定音——[test_module_boundaries.py:L12-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L17) 断言 XMLTranslator 的全部源码中不得出现 `pipeline.epub`、`Zip(`、`search_spine_paths`。也就是说：**翻译引擎是格式无关的，所有 EPUB 知识必须关在 `pipeline/epub` 里**。本讲的适配层与拦截器正是这道边界的 EPUB 侧实现。

## 4. 核心概念与源码讲解

### 4.1 EPUB 适配层（上）：Zip 双句柄与文件迁移

#### 4.1.1 概念说明

翻译一本 EPUB 的本质是：**逐个文件读源包、改写需要翻译的文件、其余文件原样搬运，最终产出一个完整的新包**。

ZIP 格式不支持原地改写（追加会留下垃圾、重写要复制全部条目），所以最清晰的模型是「双句柄」：左手打开源包只读，右手新建目标包只写。`Zip` 类把这对手柄包成上下文管理器，并用一个 `_processed_files` 集合记账——凡是已经写入目标包的路径都记一笔，退出时把没处理过的文件**兜底搬运**过去。这样调用方只需关心「我要改写哪些文件」，绝不会漏掉任何文件。

#### 4.1.2 核心流程

```text
with Zip(源, 目标) as zip:
    zip.migrate(mimetype)        # 显式最先搬运，保证第一条目位置
    ... 调用 zip.read / zip.replace 改写若干文件 ...
    ── 退出 with ──
    若无异常：遍历源包所有未处理条目 → 逐个 migrate（兜底迁移）
    最后：关闭两个句柄
```

三类基本操作：

| 操作 | 方向 | 语义 |
| --- | --- | --- |
| `read(path)` | 源包 | 打开源条目的读句柄（IO[bytes]） |
| `migrate(path)` | 源→目标 | **原样复制**：字节、ZipInfo（含 compress_type）一并保留 |
| `replace(path)` | 目标 | 标记该路径已处理，打开目标写句柄由调用方写新内容 |

#### 4.1.3 源码精读

**构造：同时打开两个句柄，失败时正确清理**（[adapter/zip.py:L8-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L8-L23)）：

```python
source_zip = zipfile.ZipFile(source_path, "r")
target_zip = zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED)
...
self._processed_files: set[Path] = set()
```

这里做了「要么都打开、要么都不留」的清理：第二个打开失败时先关掉第一个再抛异常，不会泄漏句柄。目标包默认压缩格式是 DEFLATED。

**`__exit__`：兜底迁移是本类的灵魂**（[adapter/zip.py:L28-L41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L28-L41)）：

```python
if _exc_type is None:
    all_files = self._source_zip.namelist()
    for file_path in all_files:
        if file_path.endswith("/"):
            continue                      # 目录条目跳过
        if Path(file_path) not in self._processed_files:
            self.migrate(Path(file_path))
finally:
    self._target_zip.close()
    self._source_zip.close()
return False
```

两个要点：

- 兜底迁移**只在无异常时**执行（`_exc_type is None`）。中途抛异常时目标包只保留已写入的部分（是个不完整的坏包），但不会再搬运剩余文件；
- `return False` 表示不吞异常，异常继续向上传播；`finally` 保证两个句柄无论如何都被关闭。

**`migrate`：保真复制**（[adapter/zip.py:L52-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L52-L62)）：

```python
source_info = self._source_zip.getinfo(path_str)
with self.read(path) as source_file:
    content = source_file.read()
self._target_zip.writestr(
    zinfo_or_arcname=source_info,      # 复用源条目的全部元数据
    data=content,
    compress_type=source_info.compress_type,
)
self._processed_files.add(path)
```

关键在 `zinfo_or_arcname=source_info`：直接把源条目的 `ZipInfo` 对象交给目标包。`ZipInfo` 携带文件名、时间戳、**压缩方式**等元数据，再显式传 `compress_type=source_info.compress_type`，确保 `mimetype` 从源包的「stored（不压缩）」迁到目标包仍是「stored」。这正是 OCF 规范对 mimetype 的硬性要求。

**消费方接线**：u9-l1 见过的 mimetype 首迁移（[translation/translator.py:L73-L78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L73-L78)）：

```python
with Zip(source_path=..., target_path=...) as zip:
    # mimetype should be the first file in the EPUB ZIP
    zip.migrate(Path("mimetype"))
```

显式调用保证 mimetype 是目标包的**第一个**条目——不依赖源包内部的排列顺序，也不依赖兜底迁移的遍历顺序。章节写回则走 `replace`（[translation/translator.py:L138-L139](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L138-L139)）：`with zip.replace(chapter_path) as target_file: xml.save(target_file)`。

#### 4.1.4 代码实践

1. **实践目标**：直观验证「双句柄 + 兜底迁移」的行为——不改写任何文件时，`with` 块结束后应得到一份与源包条目一致的副本。
2. **操作步骤**：在仓库根目录外新建 `zip_probe.py`（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft.pipeline.epub.adapter import Zip

   source = Path("tests/assets/epub/The little prince.epub")
   target = Path("/tmp/prince_copy.epub")

   with Zip(source_path=source, target_path=target) as zip:
       # with 块里什么都不做，观察退出后的兜底迁移
       print("源包条目数:", len(zip.list_files()))
   ```

   然后在 shell 里用系统自带工具核对副本：

   ```bash
   python zip_probe.py
   unzip -l "/tmp/prince_copy.epub" | head -15
   unzip -l "/tmp/prince_copy.epub" | tail -3
   ```

3. **需要观察的现象**：目标包条目数与源包一致；第一个条目是 `mimetype`（本例没有显式 migrate，兜底迁移按源包 namelist 顺序复制，而规范源包的 mimetype 本就在首位）。
4. **预期结果**：`/tmp/prince_copy.epub` 是一份可正常打开的 EPUB 副本；`unzip -l` 显示 mimetype 在首行、`Stored`（未压缩）状态。具体条目数与文件名**待本地验证**（取决于这本书的实际结构）。
5. 注意：`target_path` 不要指向源文件本身——目标包是以 `"w"` 模式新建的，会直接截断同名文件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `translate()` 里的 `zip.migrate(Path("mimetype"))` 这行删掉，产物 EPUB 还合法吗？

**答案**：多数情况下仍是合法的——`__exit__` 的兜底迁移会把 mimetype 复制过去，而绝大多数源包里 mimetype 本来就是第一个条目（兜底遍历按 namelist 顺序），且 `migrate` 保留 stored 压缩方式。但删掉后这条性质就**寄托于源包恰好规范**；显式调用让「mimetype 必须第一」成为不依赖输入质量的构造性保证，这正是它被写成独立一行并加注释的原因。

**练习 2**：`migrate` 与 `replace` 都会把路径加进 `_processed_files`，二者有何本质区别？

**答案**：`migrate`（[zip.py:L52-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L52-L62)）从源包读字节并连同 ZipInfo 原样写入目标包，内容不变；`replace`（[zip.py:L67-L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L67-L69)）只打开目标包写句柄，内容由调用方全新生成。共同点是都记账，避免兜底迁移时同一文件被写两次（重复条目会破坏 ZIP）。

**练习 3**：翻译中途 LLM 调用抛异常，目标包处于什么状态？

**答案**：`__exit__` 里 `_exc_type is not None`，兜底迁移被跳过，但 `finally` 仍会关闭句柄。目标包只包含此前已 migrate/replace 的条目（如 mimetype 和已翻译的前几章），是**不完整的坏包**，调用方应视为失败产物丢弃。异常本身继续向上抛（`return False`）。

### 4.2 EPUB 适配层（下）：OPF、spine、目录与元数据

#### 4.2.1 概念说明

有了 Zip 句柄，接下来的问题是「翻译哪些文件、怎么读写目录和元数据」。适配层的回答是一套纯函数 + 数据类：

- `find_opf_path`：从 `META-INF/container.xml` 找到 OPF 全路径——一切分析的起点；
- `search_spine_paths`：产出待翻译的正文文档清单；
- `read_toc` / `write_toc`：把 EPUB 2（NCX）或 EPUB 3（nav）目录读成统一的 `Toc` 树、翻译后原位写回；
- `read_metadata` / `write_metadata`：筛选 OPF 元数据中值得翻译的字段、译后按位回填；
- `epub_transcode`：把 `Toc` / `MetadataField` 编码成合成 XML 元素（`<toc-list>`、`<metadata-list>`），让「不是元素的书目信息」也能进入 XMLTranslator 的元素流水线。

#### 4.2.2 核心流程

```text
META-INF/container.xml
        │ find_opf_path
        ▼
     OPF 路径 ──────────────┬──────────────────────────────┐
                            │                              │
                    search_spine_paths              read_toc / read_metadata
                            │                              │
              manifest 字典(id→href,media)      版本探测(OPF @version)
              + spine 顺序(itemref@idref)       v2→NCX / v3→nav 定位与解析
                            │                              │
                            ▼                              ▼
              yield (opf_dir/href, media)        Toc 树 / MetadataField 列表
                                                            │
                                                encode_toc_list / encode_metadata
                                                            ▼
                                        合成 XML 元素 → XMLTranslator 翻译
                                                            │
                                                decode + write_toc / write_metadata
```

#### 4.2.3 源码精读

**`find_opf_path`：container.xml 的两级查找**（[adapter/common.py:L8-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py#L8-L27)）：

```python
container_path = Path("META-INF", "container.xml")
...
ns = {"ns": "urn:oasis:names:tc:opendocument:xmlns:container"}
rootfile = root.find(".//ns:rootfile", ns)
if rootfile is None:
    rootfile = root.find(".//rootfile")     # 容错：无命名空间的写法
...
return Path(rootfile.get("full-path"))
```

先用带命名空间的 XPath 查 `<rootfile>`，查不到再退化为裸标签名查找——兼容两类真实世界的 EPUB。`strip_namespace`（[common.py:L30-L35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/common.py#L30-L35)）则递归把所有 `{ns}tag` 截成 `tag`，供后续模块用短名操作。

**`search_spine_paths`：manifest 建字典、spine 定顺序**（[adapter/spines.py:L10-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/spines.py#L10-L43)）：

```python
manifest_items = {}
for item in manifest.findall("item"):
    item_id = item.get("id")
    item_href = item.get("href")
    media_type = item.get("media-type", "")
    if item_id and item_href:
        manifest_items[item_id] = (item_href, media_type)

for itemref in spine.findall("itemref"):
    idref = itemref.get("idref")
    ...
    if idref in manifest_items:
        href, media_type = manifest_items[idref]
        if media_type in ("application/xhtml+xml", "text/html"):
            yield opf_dir / href, media_type
```

三个细节值得注意：

1. **两遍结构**：先把 manifest 收进字典，再按 spine 的 `itemref` 顺序查字典拼接——产出的顺序就是阅读顺序；
2. **媒体类型白名单**：只翻 `application/xhtml+xml` 与 `text/html`，spine 里混入的图片或其他类型自然被跳过；
3. **`opf_dir / href`**：manifest 的 href 是相对于 OPF 所在目录的路径，拼前缀后才是包内全路径，可直接交给 `zip.read`。

消费方在 `translate()` 中调用了它两次（[translator.py:L80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L80) 先数总数算进度权重，[translator.py:L172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L172) 再逐个生成章节任务），每次调用独立重读 OPF，互不干扰。

**`Toc` 数据类：一份结构同时映射两种格式**（[adapter/toc.py:L11-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L11-L33)）。它的 docstring 本身就是对照表：EPUB 2 中 `title ↔ <navLabel><text>`、`href ↔ <content src>`；EPUB 3 中 `title ↔ <a>` 文本、`href ↔ <a href>`。`read_toc`（[toc.py:L51-L67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L51-L67)）先 `_detect_epub_version`（[toc.py:L80-L90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L80-L90)：读 OPF 根元素 `version` 属性，`startswith("3")` 判为 3），再 `_find_toc_path`（[toc.py:L93-L123](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L93-L123)）：v2 在 manifest 里找 `media-type="application/x-dtbncx+xml"` 的 NCX，v3 找 `properties` 含 `nav` 的文档，最后按版本分派到 NCX 或 nav 解析器。

**写回策略：三级匹配 + 原位更新**。`write_toc`（[toc.py:L70-L77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L70-L77)）不是重建整棵树，而是把译后的 `Toc` 列表与既有 XML 元素配对后改写文本。配对逻辑 `_match_toc_with_elements`（[toc.py:L430-L473](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L430-L473)）按 **id → href → 位置** 的优先级匹配，最大限度保留原有元素的属性、命名空间等细节，只替换标题文本。

**元数据：黑名单筛选 + 按位回填**（[adapter/metadata.py:L21-L29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L21-L29)）：

```python
SKIP_FIELDS = frozenset((
    "language", "identifier", "date", "meta", "contributor",
))
```

`language`/`identifier`/`date` 翻译了反而破坏机器语义，`meta`/`contributor` 多为技术信息——这五类直接排除，剩下 `title`、`creator` 等才值得送翻。`read_metadata`（[metadata.py:L32-L55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L32-L55)）遍历 `<metadata>` 子元素收集 `MetadataField(tag_name, text)`；`write_metadata`（[metadata.py:L58-L85](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L58-L85)）按 tag 分组、用 `tag_counters` 逐个消费译文，保证同名字段（比如多个 author）一一对应不错位，最后 `zip.replace` 写回 OPF。

**转码层：让书目信息变成「元素」**（[translation/epub_transcode.py:L58-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L58-L65) 与 [L80-L89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L80-L89)）：`encode_toc_list` 把 `Toc` 树编码成 `<toc-list><toc-item href=...><title>原文</title></toc-item></toc-list>`，`encode_metadata` 把字段列表编码成 `<metadata-list><field tag="dc:title">原文</field></metadata-list>`。这两个合成元素就是 u9-l1 里「目录与元数据各编码为单个 XML 元素」的落点——翻译引擎只认元素，不认识什么叫书名。

#### 4.2.4 代码实践

1. **实践目标**：写脚本用 `Zip` 与 `search_spine_paths` 列出一本 EPUB 的全部 spine 文档路径。
2. **操作步骤**：新建 `spine_list.py`（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft.pipeline.epub.adapter import Zip, search_spine_paths

   source = Path("tests/assets/epub/The little prince.epub")
   target = Path("/tmp/prince_probe.epub")

   with Zip(source_path=source, target_path=target) as zip:
       for i, (path, media_type) in enumerate(search_spine_paths(zip), 1):
           print(f"{i:3d}. {path}    [{media_type}]")
   ```

   运行：`python spine_list.py`。可换用 `tests/assets/epub/` 下的其他三本书重复实验（其中「治疗精神病.epub」是中文书，可对照观察中文 EPUB 的结构）。
3. **需要观察的现象**：输出的路径都是 `.xhtml`/`.html` 结尾；顺序即书的阅读顺序（通常前言、正文章节、尾注依次排列）；`media_type` 只会出现 `application/xhtml+xml` 或 `text/html` 两种值；图片、CSS、字体不会出现。
4. **预期结果**：打印的条目数等于该书 spine 中的文档数；每行格式如 `1. OEBPS/text/titlepage.xhtml    [application/xhtml+xml]`（具体路径与数量**待本地验证**，不同书结构不同）。退出 `with` 后 `/tmp/prince_probe.epub` 是完整副本。
5. 思考题（下一小节有答案）：为什么循环放在 `with` 内部？（因为 `search_spine_paths` 要通过 `zip.read` 读 OPF，句柄有效期内才能完成遍历。）

#### 4.2.5 小练习与答案

**练习 1**：manifest 和 spine 各回答什么问题？`search_spine_paths` 为什么需要两者配合？

**答案**：manifest 回答「包里有哪些文件」（id → href、media-type），spine 回答「按什么顺序阅读」（idref 序列）。只看 manifest 会把封面、字体等非正文也列为待翻译，且丢失顺序；只看 spine 则拿不到 href 与媒体类型。所以先建 manifest 字典、再按 spine 顺序查表并按媒体类型过滤。

**练习 2**：一个 EPUB 3 书包里同时存在 `toc.ncx` 和 `nav.xhtml`，`read_toc` 会读哪个？

**答案**：读 nav。`_detect_epub_version` 由 OPF 根元素的 `version` 属性判定（`startswith("3")` 即 v3，[toc.py:L80-L90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L80-L90)），v3 走「找 `properties` 含 `nav` 的 manifest 项」这条分支（[toc.py:L114-L121](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L114-L121)）；NCX 分支只在 v2 时使用。EPUB 3 里的 NCX 通常只是兼容性冗余。

**练习 3**：`read_metadata` 为什么跳过 `language` 字段？如果翻译它会发生什么？

**答案**：`language` 是给阅读器的机器信号（如 `zh`、`en`），语义是指示文档语言而非给人读的文字；LLM 把它「翻译」成中文词只会产出非法语言码，阅读器可能因此选择错误的语音朗读或断词。同理 `identifier`（唯一书号）、`date`（日期）都是机器字段，翻译纯属破坏。

**练习 4**：`write_metadata` 的按位回填如何保证「三个 author 译后还是三行、顺序不乱」？

**答案**：见 [metadata.py:L68-L83](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L68-L83)：译文字段先按 `tag_name` 分组保序，再遍历原 `<metadata>` 子元素，命中同名字段时用 `tag_counters[tag]` 计数器按出现顺序逐个消费译文——第 N 个 `creator` 拿第 N 条译文，天然一一对应。

### 4.3 标点修正：法语引号解包 unwrap_french_quotes

#### 4.3.1 概念说明

翻译目录和元数据时会遇到一个排版问题：**整值字段被引号包裹**。法语原文常用 « » 包书名，LLM 翻成中文时又常常「贴心地」给译文套上《 》，结果目录里每一行都变成《某章标题》——而在中文排版惯例里，书名号用于行文中引用书名，目录条目本身就已是标题，再包一层是冗余噪音。`unwrap_french_quotes` 就是这个「译后清理工」：把双引号（法语的 « »、中文的《 》）整体删除，同时把单层变体（‹ ›、〈 〉）**归一化为对应的双引号**。

为什么叫「法语引号」？因为 « »（guillemets）是法语的标准引号；中文技术书籍翻译自法语文献时，这层引号最容易残留。

#### 4.3.2 核心流程

映射表驱动、逐字符过滤（注意：**不是**按配对解析，而是无状态的字符级替换）：

```text
对元素树做先序遍历（iter_with_stack）：
  对每个元素：
    element.text ← filter(element.text)   # 开标签内文本
    element.tail ← filter(element.tail)   # 闭标签后文本
  属性（attrib）不动

filter 的字符规则（查 _QUOTE_MAPPING）：
  « » 《 》   → 删除（映射为空串）
  ‹ ›        → « »       （单层升双层，保留）
  〈 〉      → 《 》      （单层升双层，保留）
  其余字符    → 原样保留
```

因为是逐字符处理，「嵌套」情形 `«A ‹B› C»` 的结果是 `A «B» C`——外层删掉、内层保留并升级。

#### 4.3.3 源码精读

**映射表**（[translation/punctuation.py:L5-L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L5-L16)）：

```python
_QUOTE_MAPPING = {
    # 法语引号
    "«": "", "»": "",
    "‹": "«", "›": "»",
    # 中文书书名号
    "《": "", "》": "",
    "〈": "《", "〉": "》",
}
```

**生成器式过滤**（[punctuation.py:L19-L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L19-L25)）：

```python
def _strip_quotes(text: str):
    for char in text:
        mapped = _QUOTE_MAPPING.get(char, None)
        if mapped is None:
            yield char          # 普通字符
        elif mapped:
            yield mapped        # 单层→双层
        # 映射为空串的（«»《》）什么都不 yield，即删除
```

三分支分别对应「保留、替换、删除」。

**遍历并改写 text 与 tail**（[punctuation.py:L28-L34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L28-L34)）：

```python
def unwrap_french_quotes(element: Element) -> Element:
    for _, child_element in iter_with_stack(element):
        if child_element.text:
            child_element.text = "".join(_strip_quotes(child_element.text))
        if child_element.tail:
            child_element.tail = "".join(_strip_quotes(child_element.tail))
    return element
```

`iter_with_stack`（[xml/xml.py:L22-L37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/xml.py#L22-L37)）是先序遍历生成器，首个 yield 就是根元素自身，所以根的 text 也会被处理；同时处理 `tail` 则保证夹在两个标签之间的文本不漏网。

**应用边界：只挂在 TOC 与 METADATA 两个分支**。看消费方（[translator.py:L114-L115](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L114-L115)）：

```python
if context.element_type == _ElementType.TOC:
    translated_elem = unwrap_french_quotes(translated_elem)   # 译后清理
    decoded_toc = decode_toc_list(translated_elem)
```

METADATA 分支同样（[translator.py:L124-L125](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L124-L125)）；而 CHAPTER 分支（[translator.py:L134-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L143)）只做去重 id 和写回，**没有** unwrap。原因有两层：

1. **时机**：它作用在 `translated_elem` 上——是**翻译之后**的清理，不是对原文的预处理。目录条目、书名/作者这类「整值字段」里，包裹全文的引号是 LLM 输出的装饰，删掉只有好处；
2. **边界**：正文（CHAPTER）里的《 》是行文的合法成分——「他读了《夜航》之后」中的书名号删掉就破坏语义。函数本身是上下文无关的字符过滤（对正文同样会删），「只清理标题类字段」这条规则完全由**调用方**的选择实现，这正是本讲实践要验证的点。

（源码未注释此设计动机，以上两点是基于调用位置的代码事实与中文排版惯例的解读。）

#### 4.3.4 代码实践

1. **实践目标**：为 `unwrap_french_quotes` 写三个断言测试——《》包裹标题、正文混排、嵌套引号——并说明它为何只对目录和元数据生效。
2. **操作步骤**：新建 `test_unwrap.py`（示例代码，可放在仓库外运行，避免改动源码树）：

   ```python
   from xml.etree.ElementTree import Element, SubElement
   from pdf_craft.pipeline.epub.translation.punctuation import unwrap_french_quotes

   # 用例 1：《》包裹标题（目录条目场景）
   root = Element("toc-list")
   title = SubElement(root, "toc-item")
   title.text = "《小王子》"
   unwrap_french_quotes(root)
   assert title.text == "小王子", title.text

   # 用例 2：正文混排（含 text 与 tail 两处文本）
   p = Element("p")
   p.text = "他读了《夜航》之后"
   em = SubElement(p, "em")
   em.text = "非常感动"
   em.tail = "，又写了书评。"          # tail 也应被处理
   unwrap_french_quotes(p)
   assert p.text == "他读了夜航之后", p.text
   assert em.tail == "，又写了书评。", em.tail

   # 用例 3：嵌套（外层双引号删除、内层单层升级）
   t = Element("title")
   t.text = "«论‹几何›原本»"
   unwrap_french_quotes(t)
   assert t.text == "论《几何》原本", t.text

   print("全部断言通过")
   ```

   运行：`python test_unwrap.py`。
3. **需要观察的现象**：三个断言全部通过；特别注意用例 2 证明该函数对正文文本**同样会**删除书名号——它并不自知身处目录还是正文（`em.tail` 里没有引号，断言它保持不变是为了确认 tail 通道被正确处理而非被跳过）。
4. **预期结果**：输出「全部断言通过」。断言依据是映射表的确定性字符规则（`«»《》` 删除、`‹›→«»`、`〈〉→《》`），纯函数无外部依赖，结果可静态推得；如与实际输出不符，请核对脚本文件是否以 UTF-8 编码保存。
5. **回答「为何只对目录和元数据生效」**：因为函数是上下文无关的，生效范围完全由调用方 `translator.py` 决定——它只在 TOC（L114-L115）与 METADATA（L124-L125）两个分支、且在**译文**上调用；CHAPTER 分支不调用。设计逻辑：目录条目与元数据字段是「整值标题」，包裹全文的引号是 LLM 输出的冗余装饰；正文行文中书名号是合法语义成分，字符级删除会破坏句子。

#### 4.3.5 小练习与答案

**练习 1**：`«A «B» C»`（双层双引号嵌套）处理后是什么？

**答案**：`A B C`。映射是字符级的：所有 `«` 与 `»` 都映射为空串被删除，不区分层级、也不做配对平衡检查。「unwrap」的名字容易让人以为是配对解包，实际是全局过滤。

**练习 2**：为什么必须同时处理 `.text` 与 `.tail`？举一个只处理 text 会出错的例子。

**答案**：XML 文本分两处存放。例如 `<p>«他说<i>很感人</i>，然后哭了。»</p>` 中开头的 `«` 在 `p.text`，结尾的 `»` 在 `i.tail`（`</i>` 之后、`</p>` 之前）。若只处理 text，`»` 会残留，产出不配对的残缺文本。

**练习 3**：如果产品需求改为「正文也要去掉所有法语引号 « »，但保留中文书名号《 》」，如何最小改动实现？

**答案**：不要动 `unwrap_french_quotes`（它同时管两种字符）。更合适的最小实现是写一个新的映射过滤函数（只含 `"«": ""`、`"»": ""`、`"‹": "«"`、`"›": "»"` 四项），并在 `translator.py` 的 CHAPTER 分支（[translator.py:L134-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L137)）对 `translated_elem` 调用它。思路是「新规则新函数，旧函数语义不动」。

### 4.4 片段拦截器：XMLInterrupter 与公式保护

#### 4.4.1 概念说明

最后一座桥解决最棘手的问题：**章节 XHTML 里的 MathML 公式怎么办？**

- 公式不是翻译对象，但 XMLTranslator 摊平 XML 树时，`<math>` 内部的文本节点（变量名、数字）会被当成普通文本捞出来送翻；
- 把整棵 MathML 原样发给 LLM，token 开销巨大，还可能被模型改得面目全非；
- 翻完后还得把公式**原封不动**放回译文的正确位置。

`XMLInterrupter` 的方案是「**拦截—占位—还原**」三步走：把 `<math>` 子树从文本流中摘出，替换成一小段 LaTeX 文本（`$x+1$`）挂在合成占位元素下；LLM 把它当普通文本保留在译文中；回填阶段再把原始 MathML 换回去。它与翻译引擎之间只通过**三个回调钩子**协作——引擎在 [xml_translator/translator.py:L75-L83](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L75-L83) 暴露钩子签名，`translate()` 在 [translation/translator.py:L99-L103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L99-L103) 把 `XMLInterrupter` 的三个方法挂上去。引擎不知道 EPUB、不知道 MathML——这正呼应本讲开头的架构守卫。

#### 4.4.2 核心流程

```text
源文侧（发给 LLM 前）                      译文侧（回填之后）
─────────────────────────                ─────────────────────────
TextSegment 流                             TextSegment 流（译文）
   │ 查 parent_stack 有 <math> 祖先?          │ 查直接父元素的拦截 id
   ├─ 无 → 原样放行                           ├─ 无 → 原样放行
   └─ 有 → 按 math 元素登记 id、               ├─ 行内公式 → 取出缓存的原始
            缓存该片段；math 切换/               片段，挂回译文父栈（还原）
            流结束时：把缓存片段渲染              └─ 块级公式 → 丢弃译文占位片段
            成 "$latex$" 占位片段放行                  │
                                                 块元素流
                                                  │ 占位元素命中映射？
                                                  ├─ 是 → 换回原始 <math> 元素
                                                  └─ 否 → 仅清理残留 id 属性
```

#### 4.4.3 源码精读

**常量与状态**（[translation/xml_interrupter.py:L12-L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L12-L22)）：`_ID_KEY = "__XML_INTERRUPTER_ID"`、`_MATH_TAG = "math"`、`_EXPRESSION_TAG = "expression"`；实例状态是自增 id、当前正在拦截的 id、以及两张表——「拦截 id → 缓存的原始片段」（`_raw_text_segments`）与「占位元素 id(...) → 原始 math 元素」（`_placeholder2interrupted`）。

**源文侧判定：栈里有 math 祖先就算拦截区**（[xml_interrupter.py:L134-L140](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L134-L140)）：

```python
def _interrupted_index(self, text_segment: TextSegment) -> int | None:
    for i, parent_element in enumerate(text_segment.parent_stack):
        if parent_element.tag == _MATH_TAG:
            return i
    return None
```

**源文侧主逻辑**（[xml_interrupter.py:L50-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L50-L75)）：片段在 math 内时不放行，而是登记到其 math 元素名下（首次遇到时给该元素打 `_ID_KEY` 属性发号）；当流推进到**另一个** math（或非 math 片段）时，把上一个 math 的缓存结算成一个合并片段输出；流结束时（[L24-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L24-L33)）结算最后一个。非 math 片段立即放行。效果：**公式内文本从源文流中消失，取而代之的是一小段 LaTeX**。

**结算：构造占位片段**（[xml_interrupter.py:L77-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L77-L104)）：

```python
placeholder_element = Element(_EXPRESSION_TAG, {_ID_KEY: ...})
if interrupted_display is not None:
    placeholder_element.set(DISPLAY_ATTRIBUTE, interrupted_display)
...
merged_text_segment = TextSegment(
    text=self._render_latex(text_segments),   # "$ x+1 $"
    parent_stack=raw_parent_stack + [placeholder_element],
    ...
)
self._placeholder2interrupted[id(placeholder_element)] = interrupted_element
```

要点：占位元素是合成的 `<expression>`，携带 math 的 `display` 属性（`DISPLAY_ATTRIBUTE`，定义于 [xml/const.py:L2](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/const.py#L2)）以保留行内/块级语义；同时把「占位元素 → 原始 math」登记进映射表，供还原阶段查用。随后（[L125-L130](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L125-L130)）把各缓存片段的 `parent_stack` 统一截断为「相对 math 元素」的栈，为译文侧挂回做准备（该函数内 L106-L124 还有一段被注释掉的行内/块级差异化实验代码，注释写着「比较难搞，先关了再说」，是理解作者权衡的好材料）。

**LaTeX 渲染与兜底**（[xml_interrupter.py:L142-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L142-L170)）：先 `combine_text_segments` 把缓存片段重组回元素、下钻到 `math` 标签并克隆；然后尝试 `mathml2latex` 转换：

```python
try:
    mathml_str = tostring(math_element, encoding="unicode")
    soup = BeautifulSoup(mathml_str, "html.parser")
    latex = process_mathml(soup)
except Exception:
    pass

if latex is None:                       # 兜底：原始文本拼接，不加定界符
    latex = "".join(t.text for t in text_segments)
    ...
else:                                   # 成功：按行内/块级加 $ / $$
    if is_inline_element(math_element):
        latex = f"${latex}$"
    else:
        latex = f"$${latex}$$"
return f" {latex} "                     # 两侧垫空格，防止与相邻词粘连
```

**译文侧还原**（[xml_interrupter.py:L172-L192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L172-L192)）：译文片段的直接父元素若带拦截 id：

```python
if parent_element is text_segment.block_parent:
    # Block-level math, need to be hidden
    return                              # 块级：译文占位片段直接丢弃
raw_text_segments = self._raw_text_segments.pop(interrupted_id, None)
...
for raw_text_segment in raw_text_segments:
    raw_text_segment.parent_stack = text_basic_parent_stack + raw_text_segment.parent_stack
    yield raw_text_segment              # 行内：原始片段挂回译文父栈
```

**块元素还原**（[xml_interrupter.py:L41-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L41-L48)）：`interrupt_block_element` 查 `_placeholder2interrupted`——命中的块元素是占位符，换回原始 `<math>`；未命中但残留 `_ID_KEY` 属性的，仅清掉属性原样返回（防御：占位符没走片段路径时不会留下脏属性污染输出 XML）。

**钩子在引擎侧的接线**：XMLTranslator 只定义签名（[xml_translator/translator.py:L79-L81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L79-L81)）；`warp_callbacks`（[xml_translator/callbacks.py:L23-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/callbacks.py#L23-L32)）给未传入的钩子套恒等函数；真正的调用点在流式映射器——源文侧在 [stream_mapper.py:L131](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L131)，译文侧在 [stream_mapper.py:L63-L64](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L63-L64)。也就是说拦截发生在 u7-l4 讲过的 `map_stream` 流水线内部，对上层完全透明。

#### 4.4.4 代码实践

1. **实践目标**：以源码阅读方式追踪三个钩子的完整数据通路，并找到验证「公式文本不会进入翻译请求」的观察点。
2. **操作步骤**：
   1. 打开 [xml_translator/callbacks.py:L17-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/callbacks.py#L17-L32)，确认三个钩子的类型签名与恒等兜底；
   2. 在 [stream_mapper.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py) 中找到 L131（源文侧调用）与 L63-L64（译文侧调用），写出「钩子调用点 ↔ XMLInterrupter 方法」的对应关系；
   3. （可选实验）在 `_render_latex` 的兜底分支（[xml_interrupter.py:L160-L162](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L160-L162)）临时加一行 `print("fallback:", latex)`，对含公式的书跑一次小规模 `translate_epub`（按 u9-l1 的最小用法配置 LLM），观察哪些公式走了 mathml2latex、哪些走了兜底。**注意：实验后还原改动，不要把日志提交进源码。**
3. **需要观察的现象**：若按 u2-l3 配置了 `log_dir_path`，可在请求日志的 JSON 行里核对：含公式的段落，请求文本中出现 `$...$` 定界符，而不出现 `<mi>`、`<mrow>` 等 MathML 标签。
4. **预期结果**：三个钩子与调用点的对应关系为——`interrupt_source_text_segments` ↔ stream_mapper 源文展开处（L131）；`interrupt_translated_text_segments` 与 `interrupt_block_element` ↔ 回填完成处（L63-L64）。日志观察部分依赖所测书籍是否含 MathML 及 LLM 凭据，**待本地验证**。
5. 若本地无法运行完整翻译（需要 LLM 凭据），仅完成步骤 1、2 的静态追踪即达到本实践目标——这本身就是一次标准的「源码阅读型实践」。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 MathML 渲染成 `$latex$` 文本发给 LLM，而不是直接发送 MathML？

**答案**：三个理由。**省 token**：MathML 结构冗长（`<mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow>` 对 `$x+1$`），全书公式累积的节省可观；**防破坏**：LLM 对长结构化标记的复现并不可靠，而 `$x+1$` 作为普通文本几乎不会被改坏；**正确性**：公式本来就不该翻译，占位符让模型把它当上下文保留在译文中即可。还原时用缓存换回原始 MathML，保真无损。

**练习 2**：行内公式与块级公式在译文侧的还原路径有何不同？

**答案**：行内公式走片段路径——`_expand_translated_text_segment` 把缓存的原始片段重新挂到译文的父栈上（[xml_interrupter.py:L183-L192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L183-L192)），公式回到译文句子中间；块级公式走元素路径——译文占位片段被直接丢弃（L179-L181），由 `interrupt_block_element` 在块元素流过时把占位元素整体换回原始 `<math>`（[L41-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L41-L48)）。行内需要与文字交错，块级是独立区块，因此两条路径。

**练习 3**：`_render_latex` 两侧各垫一个空格（`return f" {latex} "`），为什么？

**答案**：占位文本会与前后文字直接拼接。若 `$x+1$` 紧贴中文（如「得到$x+1$的值」），定界符 `$` 与汉字之间没有边界，下游若再做正则提取 `$...$`，或 LLM 复述时，都可能把定界符与邻字粘连误判；垫空格保证公式定界片段是独立的词元，降低被截断或吞并的概率。

**练习 4**：`interrupt_block_element` 里「未命中映射但残留 `_ID_KEY`」的分支（[xml_interrupter.py:L43-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py#L43-L45)）防的是什么？

**答案**：防的是内部属性泄漏到最终 XML。源文侧会给 math 元素打上 `__XML_INTERRUPTER_ID` 属性，正常路径下各还原分支都会清理它；但任何片段被跳过、失败降级（如 u7-l5 的「保持原文」路径）时，携带该属性的元素可能未经清理就流到这里。这个分支兜底把属性摘掉再放行，保证产出的 XHTML 不出现实现细节污染——`__XML_INTERRUPTER_ID` 不是有意义的 HTML 属性。

## 5. 综合实践

**任务：写一个「EPUB 体检脚本」，把本讲三个模块串起来。**

脚本 `epub_checkup.py`（示例代码）对一本 EPUB 完成三件事：

```python
from pathlib import Path
from pdf_craft.pipeline.epub.adapter import Zip, read_metadata, read_toc, search_spine_paths
from pdf_craft.pipeline.epub.translation.punctuation import unwrap_french_quotes

def checkup(source: Path):
    target = Path("/tmp/checkup_copy.epub")
    with Zip(source_path=source, target_path=target) as zip:
        # ① spine 清单：文档数 + 前 5 个路径
        spines = list(search_spine_paths(zip))
        print(f"spine 文档数: {len(spines)}")
        for path, media_type in spines[:5]:
            print("   ", path, media_type)

        # ② 目录：版本、条目数、首条标题
        toc_list, ctx = read_toc(zip)
        print(f"EPUB 版本: {ctx.version}, 目录条目数: {len(toc_list)}")
        if toc_list:
            print("首条目录原文:", toc_list[0].title)

        # ③ 元数据：可翻译字段清单
        fields, _ = read_metadata(zip)
        for f in fields:
            print(f"   <{f.tag_name}> {f.text[:50]}")

checkup(Path("tests/assets/epub/The little prince.epub"))
```

在此基础上完成两件事：

1. **接入引号清理**：从 ② 的 `toc_list` 出发，手工走一遍「编码 → unwrap → 解码」：`from pdf_craft.pipeline.epub.translation.epub_transcode import encode_toc_list, decode_toc_list`，对编码后的元素调用 `unwrap_french_quotes` 再解码，对比前后 `toc_list[0].title` 是否变化（若原文没有引号则不变——这正好验证它「译后清理」的定位：对干净输入是无害幂等的）。
2. **回答架构题**（100 字以内）：为什么 `search_spine_paths`、`Zip` 必须留在 `pipeline/epub` 而不能进 `transformer/xml_translator`？用 [test_module_boundaries.py:L12-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L17) 的断言内容回答。

**预期结果**：脚本打印该书的 spine 清单、EPUB 版本、目录条目数与元数据字段（具体数值**待本地验证**）；引号清理对无引号标题不产生变化；架构题参考答案——翻译引擎保持格式无关，任何 EPUB 概念（ZIP、spine、OPF）进入引擎都会破坏其对其他输入（如 Markdown 章节）的通用性，守卫测试用字符串断言把这条边界固化下来。

## 6. 本讲小结

- **`Zip` 双句柄**（[adapter/zip.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py)）：源包只读、目标包只写，`_processed_files` 记账 + `__exit__` 兜底迁移保证「只声明要改的文件，其余自动搬运」；`migrate` 复用源 `ZipInfo` 保真（mimetype 的 stored + 首条目位置），`translate()` 显式先迁 mimetype 使该性质不依赖源包质量。
- **OPF/spine/目录/元数据检索**：`container.xml → find_opf_path → OPF` 是唯一入口；`search_spine_paths` 用「manifest 建字典 + spine 定顺序 + 媒体类型白名单」产出待翻文档；目录按 OPF 版本分派 NCX（v2）/nav（v3），写回用 id→href→位置三级匹配原位更新；元数据用 `SKIP_FIELDS` 排除机器字段、按 tag 分组计数回填。
- **`unwrap_french_quotes`**：字符级映射过滤（`«»《》`删除、`‹›〈〉`升双层），text 与 tail 双通道改写；它作用在**译后**元素上，且只挂在 TOC/METADATA 分支——目录条目与元数据字段的整值引号是冗余装饰，正文引号是合法行文。
- **`XMLInterrupter`**：三钩子协作的「拦截—占位—还原」——源文侧把 `<math>` 子树缓存并替换为 `$latex$` 占位片段（mathml2latex 转换、纯文本兜底），译文侧行内公式按片段挂回、块级公式按元素换回，块钩子顺带清理残留 id 属性。
- **架构边界**：`tests/test_module_boundaries.py` 断言翻译引擎源码不含 `Zip(`、`search_spine_paths` 等 EPUB 概念——格式知识全部关在 `pipeline/epub` 适配层，这是整条翻译管线保持可复用的根基。

## 7. 下一步学习建议

本讲补完了 u9-l1 编排层之下的全部地基，EPUB 翻译管线（单元 9）到此闭环。接下来两个方向任选：

1. **横向对照 PDF 翻译管线**（u10-l1「PDF 翻译管线：从包到替换列表」）：观察 `pipeline/pdf/` 如何用完全不同的适配方式（`PDFReplacement` 替换列表而非 ZIP 双句柄）复用同一个 XMLTranslator，体会「一个格式无关引擎 + 多个格式适配层」的架构收益；
2. **动手扩展**：回到 4.3 的思路，尝试为 `pipeline/epub/translation/` 增加一个新的译后文本修正（如全角/半角数字标点归一），沿 `punctuation.py → translator.py 接线` 的路径实现，并在 u11-l2 讲过的测试体系下补一个单元测试——这是通往 u12-l1 综合实战的热身。
