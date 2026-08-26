# EPUB 翻译工作流：translate_epub 全流程

## 1. 本讲目标

学完本讲，你应该能够：

- 独立说出 `PDFCraft().translate_epub` 背后的完整数据流：ZIP 迁移 → 目录/元数据/章节三类任务生成 → 逐元素翻译写回 → 收尾补齐未处理文件。
- 掌握 `translate()` 的全部参数语义：`submit`、`concurrency`、`max_retries`、`on_progress`、`on_fill_failed`，以及 `llm` / `translation_llm` / `fill_llm` 的取舍。
- 理解进度模型：目录 5%、元数据 5%、章节共享剩余 90%，并能手算任意一本书的进度步长。
- 理解一个容易被忽视的 ZIP 细节：为什么 `mimetype` 必须是目标 EPUB 的第一个条目、为什么它要被显式地最先迁移。
- 理解双 LLM 配置的兜底规则与 `cache_seed_content` 中版本号、目标语言的隔离作用。

本讲是 u9 单元的第一讲。u7-l2 已经精读了 XMLTranslator 内部的翻译编排（任务模型、切分分组、双 LLM 调用），本讲把镜头拉远，看这条引擎如何被嵌入「翻译一本现成 EPUB」的完整工程管线：读容器、生成任务、算进度、写回 ZIP。

## 2. 前置知识

### 2.1 EPUB 是什么

EPUB 本质上是一个改了扩展名的 ZIP 压缩包，里面装着一本电子书的全部资产：

```text
mimetype            ← 纯文本文件，内容固定为 "application/epub+zip"，必须是 ZIP 第一个条目且不压缩
META-INF/
  container.xml     ← 指向 OPF 文件的"入口指示牌"
OEBPS/（目录名不固定）
  content.opf       ← 包描述文件：metadata（书名、作者等）+ manifest（资产清单）+ spine（阅读顺序）
  nav.xhtml / toc.ncx ← 目录（EPUB 3 用 nav 文档，EPUB 2 用 NCX 文件）
  chapter1.xhtml    ← 正文，本质是 XHTML
```

三个关键词：

- **spine**：OPF 里 `<spine>` 下的 `<itemref>` 列表，声明「按什么顺序读这些文档」。翻译一本书，就是翻译 spine 上的每个 XHTML 文档。
- **OPF metadata**：书名、作者等元数据，其中部分字段（书名、副标题）值得翻译，部分技术字段（identifier、date、language）不能动。
- **mimetype 条目**：EPUB 规范（OCF）要求它必须是 ZIP 容器的第一个文件、且不得压缩，很多阅读器靠它快速识别格式。这个约束直接决定了本讲源码中一行看似多余的代码。

### 2.2 回顾 SubmitKind 三种提交模式

来自 u7-l1/u7-l2 的结论：`SubmitKind` 决定译文如何落地——

| 模式 | 效果 | 适用 |
| --- | --- | --- |
| `REPLACE` | 译文替换原文 | 单语目标语言版 |
| `APPEND_TEXT` | 译文紧跟原文之后，同一文本流 | 紧凑双语版 |
| `APPEND_BLOCK` | 译文作为独立块接在原文后 | 段落对仗式双语版（通常最清晰） |

官方文档 [docs/en/EPUB_TRANSLATION.md:L36-L58](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L36-L58) 给出了三者的对照表，并指出一个细节：目录和元数据结构无法安全地追加独立块，因此 `APPEND_BLOCK` 对这两类区域会被降级为 `APPEND_TEXT`——本讲会在源码里找到这行降级逻辑。

### 2.3 回顾双 LLM 与流式映射

来自 u7-l2/u7-l4 的结论：XMLTranslator 用 `translation_llm` 求译文、`fill_llm` 修复回填 XML 结构；`translate_elements` 是生成器，按元素流式产出 `(译文元素, 任务载荷)`，全书内容不驻留内存；并发翻译由双端队列线程池保序执行。本讲只需记住它的接口形状：**喂进去一组任务，按顺序吐出一组结果**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/pipeline/epub/translation/translator.py` | **本讲主角**。`translate()` 主函数：装配 XMLTranslator、迁移 ZIP、生成三类任务、消费翻译结果并汇报进度 |
| `pdf_craft/pipeline/epub/__init__.py` | 把 `translate` 以别名 `translate_epub` 导出，共 5 行 |
| `pdf_craft/craft.py` | 门面方法 `PDFCraft.translate_epub`，纯转发 |
| `pdf_craft/pipeline/epub/adapter/zip.py` | `Zip` 类：同时持有源/目标两个 ZIP 句柄，提供 `migrate`/`read`/`replace`，退出时补齐未处理文件 |
| `pdf_craft/pipeline/epub/adapter/spines.py` | `search_spine_paths`：解析 OPF，按 spine 顺序产出 `(文档路径, 媒体类型)` |
| `pdf_craft/pipeline/epub/adapter/toc.py` | `read_toc`/`write_toc`：识别 EPUB 2/3 目录格式并读写 |
| `pdf_craft/pipeline/epub/adapter/metadata.py` | `read_metadata`/`write_metadata`：筛出可翻译元数据字段并回写 |
| `pdf_craft/pipeline/epub/translation/epub_transcode.py` | 把 `Toc` 列表、元数据字段编码为翻译用 XML 元素，译完再解码回来 |
| `pdf_craft/transformer/xml_translator/xml_translator/translator.py` | XMLTranslator 引擎（u7-l2 已精读），本讲只看它的构造参数与 `translate_elements` 接口 |
| `docs/en/EPUB_TRANSLATION.md` | 官方 EPUB 翻译指南，参数语义的权威对照 |

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**翻译入口**、**进度权重**、**双 LLM 配置**。

### 4.1 翻译入口：translate 主函数与三类任务

#### 4.1.1 概念说明

`translate_epub` 是 pdf-craft 五大工作流中唯一「不碰 PDF」的一条：输入一本现成 EPUB，输出一本翻译后的新 EPUB，全程不需要 OCR 配置。它要同时解决四个工程问题：

1. **容器复制**：EPUB 是 ZIP，翻译只能改其中一部分文件（spine 文档、目录、元数据），其余图片、样式、字体必须原样搬运。
2. **任务生成**：把「一本书」拆成 XMLTranslator 认识的「一组 TranslationTask」。
3. **结果落地**：译文元素如何变回合法的目录条目、元数据字段、XHTML 章节。
4. **进度可观**：长流程必须能向外汇报「翻到哪了」。

#### 4.1.2 核心流程

整个 `translate()` 可以画成一条单向流水线：

```text
校验 LLM 配置（缺则抛 ValueError，先于任何文件操作）
        │
构造 XMLTranslator（双 LLM、目标语言、缓存种子）
        │
打开 Zip（源读 / 目标写 双句柄）
        │
① migrate("mimetype")  ← 必须第一个迁移，保证目标 ZIP 条目顺序
        │
② 数章节数 search_spine_paths / 读目录 read_toc / 读元数据 read_metadata
        │
③ 计算进度权重（目录 5% / 元数据 5% / 章节 90%，见 4.2）
        │
④ for 译文元素, 上下文 in translator.translate_elements(三类任务流):
        │     ├─ TOC 分支：     解引号 → 解码 → write_toc
        │     ├─ METADATA 分支：解引号 → 解码 → write_metadata
        │     └─ CHAPTER 分支： 去重 id → zip.replace 写回 XHTML
        │           （每个分支完成后累加进度并回调 on_progress）
        ▼
with 块正常退出 → __exit__ 把所有未处理文件原样迁移 → 关闭两个句柄
```

三类任务的生成规则（`_generate_tasks_from_book`）：

- **目录**：整棵目录树编码成一个 `<toc-list>` 元素，一个任务。目录条目标题是读者最先看到的内容，所以单独成块优先翻译。
- **元数据**：全部可翻译字段编码成一个 `<metadata-list>` 元素，一个任务。技术字段（language、identifier、date、meta、contributor）在读取阶段就被排除。
- **章节**：spine 上每个含 `<body>` 的 XHTML 文档一个任务，只翻译 body 内部，文档声明、head 里的样式脚本一概不动。

#### 4.1.3 源码精读

**入口别名与门面转发。** 管线的公开名字 `translate_epub` 其实是模块级函数 `translate` 的别名：

[pdf_craft/pipeline/epub/__init__.py:L1-L5](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/__init__.py#L1-L5) 把 `translate` 重新绑定 为 `translate_epub` 并加入 `__all__`，这是「内部叫 translate、外部叫 translate_epub」的命名桥。

[pdf_craft/craft.py:L174-L177](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L174-L177) 是门面上的同名方法：必填 `target_language` 与 `submit` 走关键字参数，其余一切经 `**options` 透传（`user_prompt`、`max_retries`、`max_group_tokens`、`concurrency`、`llm`、三个回调等）。门面不加任何逻辑，只提供统一调用姿势。

**主函数签名——全部控制项一览。**

[pdf_craft/pipeline/epub/translation/translator.py:L40-L54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L40-L54) 定义 `translate()` 的 13 个参数：

| 参数 | 默认 | 语义 |
| --- | --- | --- |
| `source_path` / `target_path` | 必填 | 输入输出路径，内部 `resolve()` 为绝对路径 |
| `target_language` | 必填 | 语言码或语言名（`"zh"`、`"Japanese"` 均可） |
| `submit` | 必填 | SubmitKind 三选一 |
| `user_prompt` | None | 附加翻译要求（术语、语气），补充而非替换内置结构指令 |
| `max_retries` | 5 | XML 结构修复尝试次数（语义层），区别于 `LLM.retry_times`（传输层） |
| `max_group_tokens` | 2600 | 翻译分组 token 预算，越大请求越少但失败重试越贵 |
| `concurrency` | 1 | 并发翻译数，输出顺序稳定 |
| `llm` | None | 单 LLM 快捷配置 |
| `translation_llm` / `fill_llm` | None | 翻译 / 回填修复双配置 |
| `on_progress` | None | 进度回调，取值 0.0~1.0 |
| `on_fill_failed` | None | 结构修复失败回调，收 `FillFailedEvent` |

**mimetype 必须第一个迁移。**

[pdf_craft/pipeline/epub/translation/translator.py:L73-L78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L73-L78) 打开 `Zip` 上下文后第一件事就是 `zip.migrate(Path("mimetype"))`，源码注释直言 "mimetype should be the first file in the EPUB ZIP"。原因在 [pdf_craft/pipeline/epub/adapter/zip.py:L52-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L52-L62)：`migrate` 用源文件的 `ZipInfo` 整体搬运，**连压缩方式都原样保留**——mimetype 在合法 EPUB 里是「存储（不压缩）」的，复制压缩标记就保住了这一规范要求；而目标 ZIP 是刚以 `"w"` 模式打开的空文件，此刻迁移必然落在第 0 个条目。若等到收尾的兜底循环再迁移，条目顺序就取决于源文件 `namelist()` 的顺序，不再是自己能保证的。

**Zip 双句柄与收尾兜底。**

[pdf_craft/pipeline/epub/adapter/zip.py:L8-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L8-L23) 构造函数同时打开源（只读）与目标（DEFLATED 写）两个 `zipfile.ZipFile`，并用 `_processed_files` 集合登记已处理路径；构造失败时先关闭已打开的句柄再抛异常，不留文件泄漏。

[pdf_craft/pipeline/epub/adapter/zip.py:L25-L41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L25-L41) 的 `__exit__` 是整个迁移设计的兜底：**只要 with 块正常退出**（无异常），就把源包里所有尚未处理过的常规文件（跳过目录条目）逐个 `migrate` 过去，最后关闭两个句柄。这意味着翻译逻辑只需显式处理「被翻译的文件」，图片、样式、字体等一律由收尾循环自动搬运；而一旦中途抛异常，`__exit__` 跳过兜底迁移直接关闭——目标文件不完整，正好对应文档「不可恢复错误意味着目标文件不应视为完整翻译」的约定（[docs/en/EPUB_TRANSLATION.md:L162-L168](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L162-L168)）。

[pdf_craft/pipeline/epub/adapter/zip.py:L64-L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L64-L69) 提供一对读写出口：`read` 从源包读，`replace` 在目标包以写模式打开同名条目并登记为已处理——「替换」即「目标包里新写一份，源包不动」。

**盘点与任务生成。**

[pdf_craft/pipeline/epub/translation/translator.py:L80-L82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L80-L82) 一次盘点三个数：spine 文档总数、目录条目（`read_toc` 返回整棵 `Toc` 树，EPUB 2 解析 NCX、EPUB 3 解析 nav 文档，见 [pdf_craft/pipeline/epub/adapter/toc.py:L51-L67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py#L51-L67)；找不到目录文件会直接抛 `ValueError`）、可翻译元数据字段（[pdf_craft/pipeline/epub/adapter/metadata.py:L21-L29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L21-L29) 的 `SKIP_FIELDS` 冻结集合排除五类技术字段）。

[pdf_craft/pipeline/epub/translation/translator.py:L146-L156](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L146-L156) 是任务生成器的头部，藏着前文预告的降级逻辑：目录与元数据的 `head_submit` 若为 `APPEND_BLOCK` 则改写为 `APPEND_TEXT`。原因：`encode_toc_list` / `encode_metadata` 产出的是编码器自定义的扁平容器（`<toc-list>`、`<metadata-list>`，见 [pdf_craft/pipeline/epub/translation/epub_transcode.py:L58-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L58-L65) 与 [L80-L89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/epub_transcode.py#L80-L89)），这类结构没有可以并排的「块」，块级追加会产生非法 XML。

[pdf_craft/pipeline/epub/translation/translator.py:L158-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L158-L170) 依次产出目录任务与元数据任务；二者的 `payload` 都携带各自的上下文对象（`TocContext` / `MetadataContext`），记录「从哪个文件读的、原始 XML 节点是谁」，供写回阶段使用。

[pdf_craft/pipeline/epub/translation/translator.py:L172-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L172-L187) 按 spine 顺序逐文档产出章节任务：`search_spine_paths`（[pdf_craft/pipeline/epub/adapter/spines.py:L10-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/spines.py#L10-L43)，解析 OPF 的 manifest 建立 id→href 映射，再沿 spine 的 itemref 只挑 XHTML/HTML 媒体类型）产出路径与媒体类型；`XMLLikeNode` 按 `text/html` 与否切换解析姿态；`find_first(xml.element, "body")` 取出 body 元素作为翻译对象——**找不到 body 的文档静默跳过，不生成任务**。

**消费循环：逐元素写回。**

[pdf_craft/pipeline/epub/translation/translator.py:L99-L113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L99-L113) 把任务流、并发度、三个公式拦截钩子（`XMLInterrupter`，把 MathML 公式摘出翻译流，细节属 u9-l2）和失败回调一并交给 `translate_elements`，随后按 `payload` 里的元素类型分流写回：

- **TOC 分支** [L114-L122](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L114-L122)：先 `unwrap_french_quotes` 去掉译文中残留的法语引号/书名号（[pdf_craft/pipeline/epub/translation/punctuation.py:L28-L34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/punctuation.py#L28-L34)，把 `«»《》` 剥掉、`‹›〈〉` 降一级），再 `decode_toc_list` 还原为 `Toc` 树，`write_toc` 按原文档格式（NCX 或 nav）原地更新标题后 `zip.replace` 写回。
- **METADATA 分支** [L124-L132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L124-L132)：同样解引号、解码、`write_metadata` 按「同标签按出现顺序对位」回填文本（[pdf_craft/pipeline/epub/adapter/metadata.py:L58-L85](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/metadata.py#L58-L85)）。
- **CHAPTER 分支** [L134-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L134-L143)：先 `deduplicate_ids_in_element` 给 XML 元素去重 id——翻译过程（尤其 APPEND 模式复制元素、拦截器标记元素）可能引入重复 id，不去重会破坏 XHTML 有效性；然后 `zip.replace(chapter_path)` 把整棵 XML 写回目标包。

注意三个分支共用一个模式：**每处理完一个元素，累加该类权重并调用一次 `on_progress`**——这正是下一节的主题。

#### 4.1.4 代码实践：解剖一个 EPUB 的 ZIP 与 spine

**实践目标**：亲手验证「EPUB 是 ZIP」「mimetype 是第一个条目且不压缩」「spine 决定翻译范围」三件事，并体验 `Zip` 的兜底迁移。

**操作步骤**（示例代码，`source.epub` 换成你手头的任意 EPUB）：

```python
# step1_anatomy.py —— 只用标准库，观察 EPUB 容器
import zipfile

with zipfile.ZipFile("source.epub") as zf:
    names = zf.namelist()
    print("条目总数:", len(names))
    print("第一个条目:", names[0])            # 预期: mimetype
    info = zf.getinfo("mimetype")
    print("mimetype 压缩方式:", info.compress_type)  # 预期: 0 (ZIP_STORED)
    print("mimetype 内容:", zf.read("mimetype"))
```

```python
# step2_spine.py —— 复用 pdf_craft 的适配层，列出将被翻译的文档
from pathlib import Path
from pdf_craft.pipeline.epub.adapter import Zip, search_spine_paths

with Zip(
    source_path=Path("source.epub").resolve(),
    target_path=Path("copy.epub").resolve(),
) as zip:
    for path, media_type in search_spine_paths(zip):
        print(path, "->", media_type)
# with 正常退出后，检查 copy.epub：我们没迁移任何文件，
# 但它应是源书的完整副本 —— 这就是 __exit__ 兜底循环的功劳
```

**需要观察的现象**：

1. `namelist()[0]` 是否为 `mimetype`，其 `compress_type` 是否为 0。
2. `search_spine_paths` 输出的文档数量与顺序——这就是即将被逐个翻译的章节清单（顺序即 spine 顺序）。
3. `copy.epub` 是否与源书大小相近、能否用解压工具打开。

**预期结果**：三步全部吻合；`copy.epub` 是完整副本。若手头的书 `mimetype` 不是第一个条目，说明源书本身不合规范，翻译产物的阅读器兼容性可能受影响。若不便准备 EPUB 文件，本实践标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `migrate` 要用源文件的 `ZipInfo` 整体搬运，而不是读出内容后 `writestr(path, content)` 重写？

**答案**：`writestr` 以字符串路径为参数时会用默认压缩方式（目标包构造时指定的 DEFLATED）写入新条目，丢失 mimetype「不得压缩」的规范属性；传入源 `ZipInfo` 则连同 `compress_type` 在内的元数据一起保留（[zip.py:L52-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/zip.py#L52-L62)）。同时，先迁移 mimetype 还锁定了它在目标 ZIP 中的条目位置为第 0 个。

**练习 2**：一个 spine 文档若没有 `<body>` 元素，会发生什么？它会被翻译吗？会影响进度终点吗？

**答案**：`_generate_tasks_from_book` 中 `find_first(xml.element, "body")` 返回 `None` 时不产出任务（[translator.py:L178-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L178-L187)），该文档不翻译、由收尾循环原样迁移。但 `total_chapters` 在盘点阶段按 spine 文档总数统计（[translator.py:L80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L80)），已把它计入分母，因此进度终点会略低于 100%。这是正常 XHTML 不会触发的边界情况（XHTML 必有 body），可视为已知的不精确。

**练习 3**：为什么目录和元数据各只生成一个翻译任务，而章节每章一个任务？

**答案**：目录与元数据的数据量小（几十到几百 token），编码成单个 XML 元素交给引擎内部按 token 预算自然分组即可，合并成单任务还能减少任务编排开销、保证整棵目录树在同一次翻译中获得一致的术语与语气；章节数据量大且相互独立，每章一个任务可以让流式映射逐章产出、逐章写回、逐章推进进度，全书内容不驻留内存。

### 4.2 进度权重：TOC / 元数据 / 章节的 5% / 5% / 90%

#### 4.2.1 概念说明

翻译一本书可能要发几百个 LLM 请求、跑几十分钟，调用方需要知道「大概翻到哪了」。`on_progress` 回调以 0.0~1.0 的浮点数汇报进度，但注意它的语义是**完成的工作量占比**，而非「读到书中第几页」。

权重设计的直觉：

- 目录和元数据虽然重要（读者第一眼看到的就是书名和目录），但翻译量极小——各分 5%。
- 章节正文是绝对大头——共享剩余 90%，按章均摊。
- 缺哪类就把它归零、权重让给章节：无目录条目的书，章节直接占 95% + 元数据 5%；两者都缺则章节占满 100%。

用数学表达，设 \( T \in \{0,1\} \) 表示是否有目录条目，\( M \in \{0,1\} \) 表示是否有可翻译元数据，\( N \) 为 spine 章节数，则：

\[
w_{toc} = 0.05T, \quad w_{meta} = 0.05M, \quad w_{chap} = 1 - w_{toc} - w_{meta}, \quad \Delta_{chapter} = \frac{w_{chap}}{N}
\]

进度序列单调不减，且（得益于流式映射的保序输出）推进顺序恒为：目录 → 元数据 → 章节按 spine 顺序。

#### 4.2.2 核心流程

```text
盘点: toc_has_items = len(toc_list) > 0
      metadata_has_items = len(metadata_fields) > 0
      total_items = (目录?1:0) + (元数据?1:0) + 章节数
total_items == 0 → 直接 return（with 正常退出，目标包仍是源的完整副本）

toc_weight     = 0.05 if 有目录 else 0
metadata_weight= 0.05 if 有元数据 else 0
chapters_weight= 1.0 - toc_weight - metadata_weight
每章步长        = chapters_weight / total_chapters

消费循环中每完成一个元素:
  TOC 完成     → current_progress += toc_weight      → 回调
  METADATA 完成→ current_progress += metadata_weight  → 回调
  CHAPTER 完成 → current_progress += 每章步长          → 回调
```

目录与元数据是「一次性加权」（整棵树译完才加 5%），章节是「按个加权」（每章译完加一份）。

#### 4.2.3 源码精读

[pdf_craft/pipeline/epub/translation/translator.py:L84-L97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L84-L97) 是权重计算的完整实现，注释直接写明 "Calculate weights: TOC (5%), Metadata (5%), Chapters (90%)"。注意两个防御点：

1. `total_items == 0` 时提前 `return`——避免后续 `chapters_weight / total_chapters` 在「无目录、无元数据、无章节」的空书上除零；此时 with 块正常退出，目标文件仍是源书完整副本。
2. `progress_per_chapter` 用条件表达式 `if total_chapters > 0 else 0` 兜底——无章节但有目录/元数据的书（理论上极罕见）不会除零。

进度累加散布在消费循环的三个分支末尾：[L120-L122](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L120-L122)（TOC）、[L130-L132](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L130-L132)（元数据）、[L141-L143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L141-L143)（章节），形态完全一致：`current_progress += 权重` 后 `if on_progress: on_progress(current_progress)`。

官方文档对用户的承诺见 [docs/en/EPUB_TRANSLATION.md:L139-L160](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L139-L160)：`on_progress` 收 0.0~1.0，章节占大头，目录与元数据各 5%；`on_fill_failed` 收 `FillFailedEvent`，其中 `over_maximum_retries=True` 的最终事件表示修复机会耗尽、输出需要人工检查。

#### 4.2.4 代码实践：手算并复现进度模型

**实践目标**：把权重公式变成可断言的代码，验证几本「虚拟书」的进度序列。

**操作步骤**（示例代码，纯本地无依赖）：

```python
# progress_model.py —— 复现 translator.py L84-L97 的权重计算
def compute_weights(toc_items: int, metadata_fields: int, total_chapters: int):
    toc_has_items = toc_items > 0
    metadata_has_items = metadata_fields > 0
    total_items = (1 if toc_has_items else 0) + (1 if metadata_has_items else 0) + total_chapters
    if total_items == 0:
        return None
    toc_weight = 0.05 if toc_has_items else 0
    metadata_weight = 0.05 if metadata_has_items else 0
    chapters_weight = 1.0 - toc_weight - metadata_weight
    per_chapter = chapters_weight / total_chapters if total_chapters > 0 else 0
    return toc_weight, metadata_weight, per_chapter

# 用例 1：常见书 —— 有目录、有元数据、18 章
w = compute_weights(12, 3, 18)
assert w == (0.05, 0.05, 0.90 / 18)
# 进度序列: 0.05, 0.10, 0.10+k, ..., 终点 1.0
assert 0.05 + 0.05 + 18 * (0.90 / 18) == 1.0

# 用例 2：无目录条目、无元数据、10 章（目录文件存在但为空）
w = compute_weights(0, 0, 10)
assert w == (0, 0, 0.1)

# 用例 3：有目录、无元数据、8 章 → 章节占 95%
w = compute_weights(5, 0, 8)
assert w == (0.05, 0, 0.95 / 8)

# 用例 4：空书 → None（源码中直接 return）
assert compute_weights(0, 0, 0) is None
print("全部通过")
```

**需要观察的现象**：四个用例的断言是否全部通过；用例 1 的终点是否等于 1.0。注意 `0.05`、`0.90` 都无法被二进制浮点精确表示，累加结果理论上可能落在 `0.9999999999...` 或 `1.0000000000...1` 上——若你的环境断言失败，把最后一个断言改为 `abs(... - 1.0) < 1e-9`，这也是源码本身的选择：`translate()` 只把进度用于回调展示，从不依赖它精确等于 1.0。

**预期结果**：全部通过。之后翻回源码逐行对照，确认你的复现与 [L84-L97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L84-L97) 一字不差。

#### 4.2.5 小练习与答案

**练习 1**：一本 30 章、有目录、有元数据的书，`on_progress` 的第 3 次回调值是多少？

**答案**：前两次是目录与元数据：0.05、0.10；每章步长 0.90/30 = 0.03，第 3 次回调是第一章完成：0.10 + 0.03 = 0.13。

**练习 2**：`concurrency=4` 时，四个章节在并行翻译，进度会不会「先跳到后面章节再跳回来」？

**答案**：不会。u7-l4 已知流式映射用双端队列线程池只等最老的 future，输出严格保序；`translate_elements` 按任务顺序 yield，消费循环按 yield 顺序累加进度，因此进度序列恒为「目录 → 元数据 → 章节按 spine 顺序」单调递增。并发改变的是墙钟时间，不是回调顺序。

**练习 3**：为什么目录和元数据不按「条目数」加权，而是各占固定的 5%？

**答案**：两者各自只构成一个翻译元素，翻译量通常不足全书的 1%；固定小权重既承认「读者第一眼看到的内容值得单独汇报」，又保证进度近似等于正文完成度。若按条目数加权，一本 500 条目录的书会让目录占掉过大比重，进度反而失真；固定权重还天然避免「无章节时按章均摊除零」的一类问题（配合 `total_items == 0` 的提前返回双保险）。

### 4.3 双 LLM 配置：translation_llm 与 fill_llm

#### 4.3.1 概念说明

u7-l2 建立的核心结论在本讲落地：**翻译与回填是两次独立的 LLM 调用**。翻译调用求「译文质量」，回填调用求「结构遵从」——把译文塞回 XML 骨架时模型常犯漏块、多块、标签错位等结构错误，需要另一个（可以用更低温、更守规矩的）模型反复修复。

因此 `translate()` 提供三个 LLM 参数、两种配置姿势：

| 姿势 | 参数 | 场景 |
| --- | --- | --- |
| 单模型 | 只传 `llm` | 翻译与修复用同一个模型，最省事 |
| 双模型 | 同时传 `translation_llm` 与 `fill_llm` | 翻译用高创造力的模型、修复用低温守规矩的模型，或两者分属不同服务 |

规则是「`llm` 作为两者的兜底，但至少要能凑齐两个」：`translation_llm = translation_llm or llm`、`fill_llm = fill_llm or llm`，兜底后仍为 `None` 则抛 `ValueError`。**只配翻译不配修复是不允许的**——文档明言「翻译专用配置不够，因为管线还必须能修复畸形的译文 XML」（[docs/en/EPUB_TRANSLATION.md:L116-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L116-L137)）。

#### 4.3.2 核心流程

```text
translation_llm = translation_llm or llm
fill_llm        = fill_llm or llm
二者任一为 None → ValueError（先于打开 ZIP，不产生目标文件）

XMLTranslator(
    translation_llm, fill_llm, target_language, user_prompt,
    ignore_translated_error = False,      # 译文出错不静默放过
    max_retries             = 5,          # 结构修复尝试次数（语义层）
    max_fill_displaying_errors = 10,
    max_group_score          = max_group_tokens,
    cache_seed_content = f"{版本号}:{target_language}",  # 缓存隔离种子
)

XMLTranslator 内部:
    translation_runtime = runtime_for(translation_llm, protocol_version="xml-translation-v1")
    fill_runtime        = runtime_for(fill_llm,        protocol_version="xml-fill-v1")
    # 双 protocol_version → 翻译与回填各自的缓存键空间互不污染
```

要分清两层重试，它们常被混淆：

- `translate(max_retries=5)`：**语义层**结构修复次数——回填结果校验失败后带着错误反馈再修的轮数（u7-l5 的爬山修复在此计数）。
- `LLM(retry_times=5)`：**传输层**重试——网络错误、限流、空响应的原样重发（u8-l1 的机制）。

#### 4.3.3 源码精读

[pdf_craft/pipeline/epub/translation/translator.py:L55-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L55-L60) 是兜底与校验：两行 `or` 兜底后，两个 `ValueError` 分别点名缺哪一项。注意它们发生在 `with Zip(...)` **之前**——配置错误不会创建半成品目标文件。

[pdf_craft/pipeline/epub/translation/translator.py:L62-L72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L62-L72) 构造 XMLTranslator 并把管线级参数翻译成引擎级参数：`max_group_tokens` 改名为 `max_group_score`（分组预算按「分数」而非纯 token 计，见 u7-l3）；`cache_seed_content` 拼入版本号与目标语言。`ignore_translated_error=False` 表示译文层错误不忽略——译文质量问题的容错由引擎内部的降级策略（失败片段保留原文并发 `FillFailedEvent`）负责，而非在入口静默吞掉。

[pdf_craft/pipeline/epub/translation/translator.py:L190-L194](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L190-L194) 的 `_get_version` 用 `importlib.metadata` 查询 `epub-translator` 包的版本号，查不到（例如开发环境）则回退字符串 `"development"`。这个历史包名透露了管线的出身——它从独立的 epub-translator 项目演化而来；对使用者的意义是：**缓存种子随库版本与目标语言联动**，升级库或换目标语言后整体换键，不会复用旧译文（u8-l1 的缓存八维指纹机制在此生效）。

双运行时的建立见 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:L39-L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L39-L52)：两个 `runtime_for` 分别绑定 `protocol_version="xml-translation-v1"` 与 `"xml-fill-v1"`，协议号参与缓存键，翻译与回填两套请求即使发给同一模型也各自缓存、互不串扰；`XMLStreamMapper` 同时拿到 `translation_llm.encoding` 用于本地 token 计数。

引擎入口 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:L75-L113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L75-L113) 回顾了 u7-l2 的结论：`translate_elements` 收任务流与回调，逐元素 `yield (译文元素, 任务载荷)`——载荷 `T` 对引擎是完全不透明的泛型，EPUB 管线把 `_ElementContext` 塞进去取出来，两条管线因此复用同一引擎。任务模型三字段见 [L19-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L19-L23)。

#### 4.3.4 代码实践：验证配置校验与双模型组装

**实践目标**：亲眼看到缺 LLM 时的快速失败，并组装一份双模型配置（不发真实请求）。

**操作步骤**（示例代码）：

```python
# llm_config_check.py
from pdf_craft import LLM, PDFCraft, SubmitKind

# 1) 什么都不给 → 应在打开任何文件之前抛 ValueError
try:
    PDFCraft().translate_epub(
        "source.epub", "out.epub",
        target_language="zh", submit=SubmitKind.REPLACE,
    )
except ValueError as e:
    print("捕获 ValueError:", e)
# 观察执行后当前目录是否出现 out.epub —— 预期: 不出现

# 2) 只给 translation_llm 不给 fill_llm → 同样 ValueError
try:
    PDFCraft().translate_epub(
        "source.epub", "out.epub",
        target_language="zh", submit=SubmitKind.REPLACE,
        translation_llm=LLM(key="k", url="https://x/v1", model="m", token_encoding="o200k_base"),
    )
except ValueError as e:
    print("捕获 ValueError:", e)

# 3) 双模型姿势：翻译高温、修复低温（仅构造，构造期不发请求）
translation_llm = LLM(
    key="key-a", url="https://provider.example/v1",
    model="creative-model", token_encoding="o200k_base", temperature=0.7,
)
fill_llm = LLM(
    key="key-b", url="https://provider.example/v1",
    model="strict-model", token_encoding="o200k_base", temperature=0.2,
)
print("双模型配置构造完成:", translation_llm.model, "/", fill_llm.model)
```

**需要观察的现象**：前两个调用抛出的 `ValueError` 消息内容（应点名 `translation_llm` 或 `fill_llm`）；步骤 1 之后工作目录里有没有残留 `out.epub`。

**预期结果**：两次 `ValueError` 消息分别包含 "translation_llm" 与 "fill_llm" 字样；无 `out.epub` 残留（校验先于 `Zip` 打开）；步骤 3 正常打印两个模型名。LLM 构造期不发网络请求（u2-l3 结论），假 key 亦可。若在某些包装下异常类型有差异，以「待本地验证」记录实际输出。

#### 4.3.5 小练习与答案

**练习 1**：用户只想省事，传了 `llm=LLM(...)`。此时翻译和回填分别用哪个运行时？缓存会不会互相污染？

**答案**：`translation_llm` 与 `fill_llm` 都兜底为同一个 `LLM` 对象，但 XMLTranslator 内部仍建立两个运行时，`protocol_version` 分别为 `"xml-translation-v1"` 与 `"xml-fill-v1"`（[xml_translator/translator.py:L41-L42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L41-L42)）。协议号参与缓存键，所以即使同一模型，两类请求的缓存条目也分属不同键空间，不会污染。

**练习 2**：把 `target_language` 从 `"zh"` 改为 `"fr"` 后重跑，旧的翻译缓存会被复用吗？

**答案**：不会。`cache_seed_content=f"{_get_version()}:{target_language}"`（[translator.py:L71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L71)）把目标语言编入缓存种子，语言变化导致种子变化，由 u8-l1 的机制可知种子变化会使全部缓存键改变，等于整体换了一套缓存。

**练习 3**：`translate(max_retries=5)` 与 `LLM(retry_times=5)` 都叫「重试」，分别管什么？

**答案**：前者是语义层——回填 XML 结构校验失败后，带着错误反馈再修复的尝试次数，对应 u7-l5 的爬山修复循环；后者是传输层——请求超时、网络错误、空响应后的原样重发，对应 u8-l1 的运行时重试。文档在 [docs/en/EPUB_TRANSLATION.md:L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L114) 明确区分了两者。

## 5. 综合实践

把本讲三个模块串起来：跑一次真实的 `translate_epub`，观察进度序列与两种提交模式的产物差异。

**实践目标**：

1. 验证 `on_progress` 的实际取值序列与 4.2 的手算模型一致。
2. 对比 `SubmitKind.REPLACE` 与 `SubmitKind.APPEND_TEXT` 产物中同一章 HTML 的结构差异。
3. 顺带确认 `mimetype` 在产物 ZIP 中的位置。

**准备**：一份小 EPUB（章节数少、单章短，控制 token 成本）与一个可用的 OpenAI 兼容端点。

**第一步：翻译脚本**（示例代码）。

```python
# translate_epub_demo.py
from pdf_craft import LLM, PDFCraft, SubmitKind
from pdf_craft import FillFailedEvent

llm = LLM(
    key="your-api-key",
    url="https://api.example.com/v1",
    model="your-model",
    token_encoding="o200k_base",
    cache_path="translation-cache",   # 重跑复用已完成的翻译
    log_dir_path="translation-logs",  # 七类 JSON 行日志，便于排查
)

progress_log = []

def show_progress(value: float) -> None:
    progress_log.append(value)
    print(f"进度: {value:.1%}")

def report_fill_failure(event: FillFailedEvent) -> None:
    if event.over_maximum_retries:
        print(f"[最终失败] {event.error_message}")

SUBMIT = SubmitKind.REPLACE   # 第二轮改为 SubmitKind.APPEND_TEXT
OUT = "translated.replace.epub"  # 第二轮改为 "translated.append.epub"

PDFCraft().translate_epub(
    "source.epub", OUT,
    target_language="zh",
    submit=SUBMIT,
    llm=llm,
    concurrency=2,
    on_progress=show_progress,
    on_fill_failed=report_fill_failure,
)

print("回调总数:", len(progress_log))
print("是否单调递增:", all(a <= b for a, b in zip(progress_log, progress_log[1:])))
print("终点:", progress_log[-1] if progress_log else None)
```

**第二步：观察进度**。对照 4.2 的公式：若书有目录、有元数据、N 章，则回调序列应为 `0.05, 0.10, 0.10+0.9/N, ..., 1.0`，共 N+2 次。

**第三步：对比两种产物**（shell 命令，路径以第一步实践 `search_spine_paths` 的输出为准）。

```bash
# REPLACE：正文段落应只剩中文
unzip -p translated.replace.epub OEBPS/Text/chapter1.xhtml | head -n 40

# APPEND_TEXT：同一段落应「原文 + 译文」紧邻出现
unzip -p translated.append.epub OEBPS/Text/chapter1.xhtml | head -n 40

# 确认 mimetype 仍是第一个条目且未压缩（compress 列为 Stored）
unzip -lv translated.replace.epub | head -n 8
```

**预期结果**：

- REPLACE 产物的章节里原文段落被译文替换；APPEND_TEXT 产物里同一段落后紧跟译文；两种产物的目录标题均被翻译（且按源码，目录区域的 APPEND 语义恒为文本追加）。
- `unzip -lv` 显示 `mimetype` 位于第一条、压缩方式 Stored。
- 进度序列单调递增、终点约为 1.0（浮点累加，允许 1e-9 级误差）、次数等于 N+2。

**无真实端点时的替代方案**：用本地简单 HTTP 服务伪造 OpenAI 兼容的 `/chat/completions` 响应（返回固定 `choices[0].message.content`）。回显型 mock 的译文大概率过不了结构校验，会触发 `on_fill_failed` 并让失败片段保留原文——这恰好是观察容错降级的机会；但进度回调、ZIP 迁移、产物可解压这些管线行为仍可完整验证。mock 下译文质量相关的现象均「待本地验证」。

## 6. 本讲小结

- `PDFCraft().translate_epub` 经别名 `translate_epub = translate` 抵达 [translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py) 的 `translate()` 主函数：校验 LLM → 构造 XMLTranslator → 迁移 mimetype → 盘点三类内容 → 生成任务流 → 逐元素翻译写回 → 收尾兜底迁移。
- `Zip` 是双句柄迁移器：被翻译的文件经 `replace` 新写，其余文件由 `__exit__` 的兜底循环原样搬运；中途异常则跳过兜底，目标文件不完整即「不可当作成品」。
- `mimetype` 必须显式第一个迁移：借源 `ZipInfo` 保留「不压缩」属性并锁定条目位置为第 0 个，这是 EPUB OCF 规范的硬性要求。
- 任务分三类：目录与元数据各编码成单个 XML 元素（`APPEND_BLOCK` 被降级为 `APPEND_TEXT`），章节按 spine 逐文档取 `<body>` 生成任务；写回前章节要去重 id、目录与元数据要做引号解包。
- 进度模型是固定权重：目录 5%、元数据 5%、章节均摊剩余 90%；回调按任务完成顺序单调递增，语义是「工作量占比」而非「阅读位置」。
- LLM 配置「单模型兜底、双模型可选、缺一不可」：`llm` 同时兜底两个角色，校验先于文件操作；`cache_seed_content` 由库版本与目标语言拼成，配合双 `protocol_version` 实现缓存三层隔离。

## 7. 下一步学习建议

下一讲 **u9-l2 EPUB 适配层：ZIP、spine 与文本修正** 将向下钻入本讲只当黑盒用的 adapter 层与文本修正机制：`Zip` 与 `search_spine_paths` 的更多细节、目录（NCX/nav）与元数据读写、`unwrap_french_quotes` 法语引号解包为何只对目录和元数据生效、`XMLInterrupter` 如何把 MathML 公式摘出翻译流再放回译文。

建议提前阅读的源码：

- [pdf_craft/pipeline/epub/adapter/toc.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/adapter/toc.py) —— `_match_toc_with_elements` 的 id/href/位置三级匹配策略。
- [pdf_craft/pipeline/epub/translation/xml_interrupter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/xml_interrupter.py) —— 三个 interrupt 钩子的具体实现。

若想换方向回顾依赖：重读 u7-l2 的 `translate_elements` 五步流程有助于把本讲的任务生成分支对应到引擎内部；u8-l1 的缓存键构成则能解释本讲 `cache_seed_content` 的隔离效果。
