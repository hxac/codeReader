# 综合实战：编写自定义转换步骤

## 1. 本讲目标

这是整部手册的收官之讲。前面十一个单元里，我们把 pdf-craft 的提取链路、目录分析、章节生成、文档包、渲染器、XML 翻译引擎、LLM 基础设施与 PDF 回写管线逐一拆开看过；本讲要把它们重新组装回去——**由你来写一个插件，把它插进官方管线**。

学完本讲，你应该能够：

1. 独立实现一个 `PackageTransformer`，并让它通过 `steps=` 参数接入 `convert_pdf_to_markdown` / `convert_pdf_to_epub` / `translate_pdf` 三条官方工作流。
2. 复用 `XMLTranslator`（含其内置的修复循环与缓存体系）构建带领域术语表的自定义翻译步骤。
3. 说清 pdf-craft 分层架构的取舍逻辑：为什么扩展点设计在「包进包出」这一层，而不是更早或更晚。

本讲的立场是：**读源码的最终检验，是能安全地扩展它**。

## 2. 前置知识

本讲默认你已读过以下讲义（至少达到「能复述结论」的程度）：

- **u7-l1 转换器协议**：两级扩展点——章级 `ChapterTransformer`（改 `Chapter` 内存对象）与包级 `PackageTransformer`（包进包出）；默认包级实现 `ChapterPackageTransformer` 的「复制—改写」策略。
- **u7-l2 XMLTranslator 核心**：`TranslationTask` 三字段（element / action / payload），`translate_elements` 生成器的五步流程，翻译与回填拆为 `translation_llm` 与 `fill_llm` 两次调用。
- **u8-l1 / u8-l2 LLM 基础设施**：配置与执行分离（`LLM` 配置类 + `runtime_for` 运行时工厂），传输层重试与语义层修复循环的分工，`cache_seed_content` 的缓存隔离作用。
- **u6-l1 DocumentPackage**：包目录即数据格式（`chapters/`、`assets/`、`toc.xml`、`cover.png`、`document.json`），`ocr/` 是提取器私有缓存、不属于契约。
- **u11-l2 测试体系**：仓库「修复提交与回归测试成对落地」的惯例，组合边界测试如何用伪协作者固化契约。

还需要两块 Python 语言知识：

- **Protocol 结构化类型**：`typing.Protocol` 定义的是「形状契约」——只要你的类有同名同签名的方法，就算实现了协议，无需继承。pdf-craft 的两个转换器协议都是这么用的。
- **`xml.etree.ElementTree`**：标准库 XML 编解码，`Chapter` 对象与 `chapter_N.xml` 之间的编解码函数（`encode` / `decode`）就建立在其 `Element` 树上。

一个贯穿本讲的关键认知：**转换器协议对「做什么」零假设**。协议只约定输入输出形状，你的转换器内部可以调 LLM、查数据库、跑正则，甚至什么都不做——pdf-craft 完全不关心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | 门面类 `PDFCraft`；本讲焦点是 `TranslationStep` 信封、`_apply_steps` 步骤链、`_as_package_transformer` 分派与 `_accepts_package` 签名探测 |
| [pdf_craft/transformer/package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py) | `PackageTransformer` 协议定义与 `ChapterPackageTransformer` 参考实现（自定义包级转换器的抄写模板） |
| [pdf_craft/transformer/protocol.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/protocol.py) | 章级 `ChapterTransformer` 协议（4 行文件） |
| [pdf_craft/transformer/chapter_xml.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py) | `ChapterXMLTransformer`：把 `XMLTranslator` 适配成章级转换器的桥梁 |
| [pdf_craft/transformer/xml_translator/xml_translator/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py) | `XMLTranslator` 引擎本体：构造参数与 `translate_element` 入口 |
| [pdf_craft/transformer/__init__.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/__init__.py) | transformer 子包的公开边界 |
| [pdf_craft/pipeline/epub/translation/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py) | 官方构造 `XMLTranslator` 的标准范例（参数取值可抄） |
| [pdf_craft_tool/cli.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py) | 仓库 CLI 把 `XMLTranslator` 包成 `ChapterXMLTransformer` 的完整示范 |
| [tests/test_composable_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py) | 组合边界测试：确定性翻译器 `_DeterministicXMLTranslator` 与「空章跳过」契约 |
| [docs/en/API_REFERENCE.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md) | 官方公开 API 参考：扩展类型表与 `SubmitKind` 语义 |

## 4. 核心概念与源码讲解

### 4.1 自定义转换器：从协议到实现

#### 4.1.1 概念说明

「自定义转换器」要回答的问题是：**在提取完成之后、渲染开始之前，我想对文档内容做点官方没提供的事，应该把代码写在哪里？**

pdf-craft 给出的答案是两个嵌套的扩展点：

- **章级 `ChapterTransformer`**：一次拿到一个 `Chapter` 内存对象（`transform(chapter) -> chapter`），改完返回。不用碰 XML、不用管目录复制，适合「逐章独立」的文本处理（翻译、改写、标注）。
- **包级 `PackageTransformer`**：一次拿到整个 `DocumentPackage`（`transform(package, output_path) -> DocumentPackage`），**读原包、写新包**，对输出目录全权负责。适合跨章全局操作（按全书统计生成术语表、改目录树、往包里加新文件）。

一个必须内化的约束：**包级转换器必须产出「另一个合法的包」**。因为渲染器只认 `DocumentPackage` 契约（u6-l1 讲过的五成员目录约定），你的输出目录要通过 `DocumentPackage.from_path(...).validate()`，否则下游直接拒绝。

#### 4.1.2 核心流程

手写一个包级转换器的标准五步（对照 `ChapterPackageTransformer` 的实现逐句抄）：

```
输入: package (DocumentPackage), output_path (Path)

1. 安检   : package.validate()                    # 原包必须合法
2. 占位   : output_path 已存在则抛 FileExistsError # 输出目录必须全新
3. 复制   : chapters/、assets/ 整树复制；
           toc.xml、cover.png、document.json 存在才复制
4. 改写   : 对副本中每个 chapter_N.xml:
           decode(XML) -> 改 Chapter 对象 -> encode -> save_xml(原子写)
5. 交货   : return DocumentPackage.from_path(output_path).validate()
```

三处细节决定成败：

- **原包只读**。所有写操作都发生在 `output_path` 下，中途崩溃也不污染上游——这是整条 `steps` 链能安全组合的前提。
- **`ocr/` 缓存不复制**。它是提取器私有目录，不属于契约；复制它只会浪费磁盘。
- **原子写**。`save_xml` 先写 `.tmp` 再 `replace`（下文源码精读会看到），保证「文件存在即完整」。

#### 4.1.3 源码精读

**协议本身：两个「形状契约」**

[pdf_craft/transformer/package.py:L16-L19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L16-L19) 定义包级协议：`transform(self, package: DocumentPackage, output_path: Path) -> DocumentPackage`——包进包出，仅此而已。

[pdf_craft/transformer/protocol.py:L4-L6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/protocol.py#L4-L6) 定义章级协议：`transform(self, chapter: Chapter) -> Chapter`——整个文件只有这一个问题定义，是全仓库最小的公开协议。

**参考实现：`ChapterPackageTransformer.transform` 的复制—改写**

[pdf_craft/transformer/package.py:L38-L56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L38-L56) 就是 4.1.2 伪代码的原型：L39 校验原包，L40-L41 拒绝已存在的输出目录，L42-L47 复制契约成员（注意 L45 的三元组循环——`toc_path`、`cover_path`、`metadata_path` 都是可选成员，存在才复制），L49-L52 逐章「decode → 章级转换 → encode → save_xml」循环，L54-L55 可选的 `toc_transformer` 改写目录 XML，L56 从磁盘重建并校验后交货。

[pdf_craft/transformer/package.py:L25-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L25-L36) 的构造函数还藏着一个协议约定：L32-L33 探测章级转换器是否实现 `with_mode` 钩子，实现则把 `SubmitKind` 模式传播下去——你的自定义章级转换器如果支持双语追加等模式，也应提供这个钩子。

**原子写工具**

[pdf_craft/common/xml.py:L28-L40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L28-L40) 的 `save_xml` 先写同目录临时文件、成功后 `replace` 顶替，异常时清理残留——自定义转换器写章节 XML 时应直接复用它（`from pdf_craft.common.xml import read_xml, save_xml`；注意 `pdf_craft.common` 是内部模块，未在顶层 `pdf_craft` 重导出，属于「可用但非稳定公开面」）。

**适配器：把 `XMLTranslator` 接到章级协议**

[pdf_craft/transformer/chapter_xml.py:L28-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L28-L38) 的 `transform` 是三行核心：`encode(chapter)` 把章对象变 XML 元素 → 无可译文本则原样返回（L30-L34 注释解释了 OCR 会产出空章，硬送 `XMLTranslator` 会触发 "Translation failed unexpectedly"）→ 否则打包成 `TranslationTask` 交给引擎、拿回译好的元素再 `decode` 回章对象。这是「复用 XMLTranslator 构建自定义步骤」的标准接口。

**引擎构造参数**

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:L27-L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L27-L52) 是 `XMLTranslator` 的完整构造签名。自定义翻译步骤时你真正关心的参数是：`user_prompt`（注入 translate.jinja 的用户规则——术语表就放这里）、`max_retries`（回填修复次数）、`max_group_score`（每组 token 预算，默认 2600）、`cache_seed_content`（缓存隔离种子）。L41-L42 印证了 u7-l2 的结论：双运行时、双 `protocol_version`。

**官方构造范例（可抄的参数取值）**

- [pdf_craft/pipeline/epub/translation/translator.py:L62-L72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L62-L72)：EPUB 管线用 `max_fill_displaying_errors=10`、`cache_seed_content=f"{_get_version()}:{target_language}"`——缓存种子由「库版本 + 目标语言」拼成。
- [pdf_craft_tool/cli.py:L449-L460](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L449-L460)：仓库 CLI 的 `_xml_transformer` 用 `max_fill_displaying_errors=3`、`cache_seed_content=f"pdf-craft-tool:{args.target_language}"`，并演示了双 LLM 各自独立的缓存与日志目录，最后 `ChapterXMLTransformer(translator)` 完成包装——**这两行就是你自定义翻译步骤的骨架**。

**一个必须知道的坑：签名探测**

[pdf_craft/craft.py:L280-L292](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L280-L292) 的 `_accepts_package` 用 `inspect.signature` 检查转换器的 `transform` 方法：**恰好两个位置参数、且参数名必须是 `package` 与 `output_path`**，才被认作包级转换器。这不是防御式编程装饰——它有实际后果（见 `_as_package_transformer`，4.2.3 详述）：如果你把自己的包级转换器参数命名为 `source` / `target`，探测失败，它会被误当成章级转换器包装，运行时以 `TypeError`（参数个数不符）崩溃。**参数名是接口的一部分。**

#### 4.1.4 代码实践

**实践一：实现「章节摘要」包级转换器（确定性版，零凭据可跑）**

1. **实践目标**：手写一个 `PackageTransformer`，为每章开头插入一段「本章导读」文本，验证你掌握了复制—改写—校验的全流程。
2. **操作步骤**：

   示例代码（非项目原有代码）：

   ```python
   # summary_transformer.py —— 示例代码
   from pathlib import Path
   from shutil import copy2, copytree

   from pdf_craft import DocumentPackage
   from pdf_craft.extractor.chapter.chapter import (
       BlockLayout, Chapter, ParagraphLayout, decode, encode,
   )
   from pdf_craft.common.xml import read_xml, save_xml


   class SummaryPackageTransformer:
       """为每章开头插入一段导读文本的包级转换器。

       注意：transform 的两个参数名 package / output_path 是
       门面签名探测 _accepts_package 认定包级转换器的依据，不可改名。
       """

       def __init__(self, lead_text: str = "【本章导读】") -> None:
           self._lead_text = lead_text

       def transform(
           self, package: DocumentPackage, output_path: Path,
       ) -> DocumentPackage:
           package.validate()                                   # 1. 安检
           if output_path.exists():
               raise FileExistsError(f"output package already exists: {output_path}")
           output_path.mkdir(parents=True)                      # 2. 占位
           copytree(package.chapters_path, output_path / "chapters")  # 3. 复制
           copytree(package.assets_path, output_path / "assets")
           for source in (package.toc_path, package.cover_path, package.metadata_path):
               if source is not None and source.exists():
                   copy2(source, output_path / source.name)

           for path in sorted((output_path / "chapters").glob("chapter*.xml")):
               chapter = decode(read_xml(path))
               save_xml(encode(self._prepend_summary(chapter)), path)  # 4. 改写

           return DocumentPackage.from_path(output_path).validate()    # 5. 交货

       def _prepend_summary(self, chapter: Chapter) -> Chapter:
           # 找一个已有文本块的页码与检测框，保证新增块的几何信息有出处
           page_index, det = 1, (0, 0, 1, 1)
           for layout in chapter.layouts:
               if isinstance(layout, ParagraphLayout) and layout.blocks:
                   page_index = layout.blocks[0].page_index
                   det = layout.blocks[0].det
                   break
           words = sum(
               len(str(member))
               for layout in chapter.layouts
               if isinstance(layout, ParagraphLayout)
               for block in layout.blocks
               for member in block.content
           )
           lead = ParagraphLayout("text", 0, [BlockLayout(
               page_index, 0, det,
               [f"{self._lead_text}本章约 {words} 个字符的内容。"],
           )])
           chapter.layouts.insert(0, lead)
           return chapter
   ```

   字段依据：`ParagraphLayout(ref, level, blocks)` 与 `BlockLayout(page_index, order, det, content)` 的定义见 [pdf_craft/extractor/chapter/chapter.py:L20-L24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L20-L24) 与 [pdf_craft/extractor/chapter/chapter.py:L61-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L61-L65)；`Chapter(id, level, layouts)` 见 [pdf_craft/extractor/chapter/chapter.py:L13-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L13-L17)。

3. **需要观察的现象**：`decode(read_xml(path))` 能正确还原章对象；`DocumentPackage.from_path(output_path).validate()` 不抛异常。
4. **预期结果**：对任一已有提取包运行 `SummaryPackageTransformer().transform(package, Path("out-pkg"))`，`out-pkg/chapters/chapter_1.xml` 的 `<body>` 首个子元素是新插入的 `<paragraph>`。待本地验证（若你的包里存在 `chapter_head.xml`，注意它也会被插入导读，可按需在 `_prepend_summary` 里按 `chapter.id is None` 跳过）。

#### 4.1.5 小练习与答案

**练习 1**：把实践一的 `transform` 参数改名为 `(self, source, target)`，会发生什么？

**答案**：[pdf_craft/craft.py:L280-L292](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L280-L292) 的签名探测要求参数名恰为 `package` / `output_path`，改名后探测返回 `False`，该对象被当作章级 `ChapterTransformer` 包进 `ChapterPackageTransformer`；运行时它会以一个参数（`chapter`）调用你的双参方法，抛 `TypeError`。结论：**对结构化类型协议，参数名也是契约**。

**练习 2**：为什么 `ChapterPackageTransformer.transform` 复制 `toc.xml`、`cover.png`、`document.json` 时要用「存在才复制」的三元组循环，而 `chapters/`、`assets/` 直接整树复制？

**答案**：因为包契约中后两者必备、前三三者可选（`DocumentPackage` 的 `toc_path` 等字段类型是 `Path | None`，见 [pdf_craft/document/package.py:L11-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L11-L26)）。源包没有目录文件时硬复制会 `FileNotFoundError`；而缺了 `chapters/` 或 `assets/` 的包本来就通不过 `validate`（L28-L32 的目录检查），无需特判。

**练习 3**：如果想让实践一的转换器在 `ExtractionOptions.includes_footnotes=False` 的包上也不破坏脚注引用，需要注意什么？

**答案**：`decode` 是两遍式解码——先建引用表再解正文，悬空引用直接报错（u5-l1 讲过）。只要我们只**插入新段落**、不删除或改写任何既有 `Reference`，引用表就保持完整；反之，若你的转换器要删除段落，必须同时检查它是否是某个引用的 `layouts` 宿主，否则 `encode` 时会产出悬空 `ref id`，下游解码即崩。

### 4.2 步骤组合：TranslationStep 与 _apply_steps

#### 4.2.1 概念说明

写好转换器只是造出了零件；「步骤组合」解决的是**怎么把零件装进官方流水线、多个零件按什么顺序生效**。

pdf-craft 的装配语言极简，只有两条规则：

1. **一切步骤都是 `steps: Sequence[TranslationStep | PackageTransformer]`**。`TranslationStep` 是个信封（转换器 + 提交模式）；裸 `PackageTransformer` 也可以直接进列表，等价于模式缺省。
2. **步骤按列表顺序串行执行，前一步的输出包是后一步的输入包，最终渲染最后一个包**。没有并行、没有条件分支——需要复杂编排时，自己写一个聚合的 `PackageTransformer`。

#### 4.2.2 核心流程

以 `convert_pdf_to_epub` 为例，`steps` 在整条链路中的位置：

```
convert_pdf_to_epub(source, output, steps=[s0, s1])
│
├─ 1. extract_pdf_with_metering(...)          # 提取 → 原始包 P
├─ 2. _apply_steps(P, [s0, s1])               # 本讲主角
│     ├─ s0.transform(P,  P_root/transformed-0)  → P0
│     └─ s1.transform(P0, P_root/transformed-1)  → P1
├─ 3. book_meta 缺省时提取 PDF 元数据            # 注意：在步骤之后
└─ 4. render_epub(P1, output, ...)            # 渲染最后一个包
```

两个关键数据流事实：

- **派生包平铺在原包根下**：第 `i` 步的输出目录固定是 `package.chapters_path.parent / f"transformed-{i}"`（注意基准永远是**原始**包根，不是 `current` 的根），所以 `transformed-0`、`transformed-1` 是兄弟目录，链路一目了然、可逐步检查。
- **`translate_pdf` 的步骤链多一段前缀**：先经 `translate_package` 产出 `translated` 包（在临时目录里），再在其上跑 `_apply_steps`，最后回写 PDF。

#### 4.2.3 源码精读

**信封：`TranslationStep`**

[pdf_craft/craft.py:L30-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L30-L36) 定义了这个冻结 dataclass：一个 `transformer`（章级或包级）加一个 `mode: SubmitKind = REPLACE`。官方用法见 [docs/en/PDF_TRANSLATION.md:L80-L91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/PDF_TRANSLATION.md#L80-L91)——`steps=[TranslationStep(translator, mode=SubmitKind.REPLACE)]`；同页 L93 明确写着「多个步骤按列表顺序执行，高级用法可直接传公开的 `PackageTransformer` 作为步骤」。

**执行：`_apply_steps`**

[pdf_craft/craft.py:L212-L221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/craft.py#L212-L221) 是步骤链的全部实现，只有六行：`current` 从原包出发，循环内先经 `_as_package_transformer` 归一化（见下），再把 `current` 变换到 `transformed-{index}`。注意 L219 的输出目录基于原包根计算——这就是 4.2.2 说的「平铺命名」。

**归一化：`_as_package_transformer`**

[pdf_craft/craft.py:L238-L251](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L238-L251) 把四种合法步骤统一成 `PackageTransformer`：裸包级转换器原样通过（L240-L241）；信封里装的是 `ChapterPackageTransformer` 且信封模式与其内 mode 不一致时，重建一个带信封模式的实例（L243-L248）；信封里装的是其他包级形状（`_accepts_package` 认定）直接解包使用（L249-L250，**此时信封的 mode 被忽略**）；剩下的按章级转换器包进 `ChapterPackageTransformer`（L251）。

**入口接线：两个一步式方法与 `translate_pdf`**

[pdf_craft/craft.py:L192-L210](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L192-L210) 的 `convert_pdf_to_epub` 显示步骤链插在提取与渲染之间（L204）、`book_meta` 自动提取在步骤之后（L205-L206）。[pdf_craft/craft.py:L179-L190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) 的 `convert_pdf_to_markdown` 结构相同。[pdf_craft/craft.py:L147-L158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L147-L158) 的 `translate_pdf` 则先做模式预检——配合 [pdf_craft/craft.py:L295-L298](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L295-L298) 的 `_step_mode` 解析信封与转换器两层的模式，任一步骤解析出 `APPEND_BLOCK` 即抛 `ValueError`（PDF 回写不支持块追加），然后经 [pdf_craft/craft.py:L223-L236](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L223-L236) 的 `_translate_for_pdf` 在临时目录里串「翻译包 → 步骤链」。

**提交模式的语义**

[docs/en/API_REFERENCE.md:L88-L96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L88-L96) 官方总结了三种 `SubmitKind`：`REPLACE`（替换原文，产出纯目标语文档）、`APPEND_TEXT`（译文追加进同一文本流）、`APPEND_BLOCK`（译文作独立块追加，通常是更清晰的双语排版，但不支持 PDF 回写）。

#### 4.2.4 代码实践

**实践二：接入 `convert_pdf_to_epub` 并观察多步骤数据流**

1. **实践目标**：把实践一的转换器挂进官方 EPUB 工作流，并用 `package_path` 保留中间产物，亲眼看到 `transformed-N` 链。
2. **操作步骤**（示例代码）：

   ```python
   # run_capstone.py —— 示例代码
   from pdf_craft import DeepSeekOCRVendorConfig, PDFCraft, PDFOptions
   from summary_transformer import SummaryPackageTransformer

   craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
       base_url="...", api_key="...", model="...",
   )))

   craft.convert_pdf_to_epub(
       "book.pdf", "book.epub",
       package_path="work/package",        # 保留中间包，便于检查 steps 链
       steps=[SummaryPackageTransformer()],  # 裸包级转换器直接作步骤
   )
   ```

   再做一次双步骤实验：`steps=[SummaryPackageTransformer(), SummaryPackageTransformer(lead_text="【二次标注】")]`，然后检查 `work/package/` 目录。
3. **需要观察的现象**：`work/package/` 下出现 `transformed-0/` 与 `transformed-1/` 两个兄弟目录；`transformed-0/chapters/chapter_1.xml` 只有「本章导读」前缀，`transformed-1/chapters/chapter_1.xml` 同时有「二次标注」与「本章导读」两个前缀（后插入者在前）；最终 EPUB 的第一章开头与 `transformed-1` 的内容一致。
4. **预期结果**：如上。**注意一个复跑坑**：`chapters/` 每次提取会全删重建（章节文件是纯派生物），但 `transformed-0/` 不会被清理——用同一 `package_path` 复跑且 steps 非空时，会因输出目录已存在抛 `FileExistsError`（来自 [pdf_craft/transformer/package.py:L40-L41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L40-L41) 的检查，你的自定义转换器也应有同样检查）。复跑前先删 `transformed-*`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`TranslationStep(MyPackageTransformer(), mode=SubmitKind.APPEND_TEXT)` 中的 `mode` 会生效吗？

**答案**：不会。`_as_package_transformer` 对信封里的非 `ChapterPackageTransformer` 包级转换器走 [pdf_craft/craft.py:L249-L250](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L249-L250) 分支：直接解包使用，`mode` 被丢弃。`SubmitKind` 是「章级文本落地方式」的概念，包级转换器对输出内容全权负责，不存在统一的模式语义——想要 APPEND 行为就在你自己的 `transform` 里实现。想让模式可注入，可在你的类上提供 `with_mode` 钩子（`ChapterPackageTransformer` 与 `ChapterXMLTransformer` 都这么做）。

**练习 2**：`convert_pdf_to_epub` 里 `_apply_steps`（L204）在 `_extract_book_meta`（L205-L206）之前执行，这个顺序对自定义步骤有影响吗？

**答案**：有边界上的含义。步骤改写的是**章节内容包**，`book_meta` 提取读的是**源 PDF 的元数据**（书名作者），两者数据源不同，因此顺序通常无感。但如果你希望「书名也按步骤逻辑改写」，不能指望 steps——它们碰不到 `book_meta`，应当显式传 `book_meta=BookMeta(title=...)` 参数（`render_epub` 的签名见 [pdf_craft/craft.py:L135-L145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L135-L145)）。EPUB 的**导航目录**倒是能改：用 `ChapterPackageTransformer(toc_transformer=...)`（[pdf_craft/transformer/package.py:L30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L30)）改写 `toc.xml`。

**练习 3**：为什么 `_apply_steps` 的输出目录用「原包根 + transformed-{index}」而不是让每步自选目录或嵌套（`transformed-0/transformed-1/...`）？

**答案**：平铺命名让任意一步的产物都能从原包根直接寻址，便于调试时逐目录 diff；同时 `_apply_steps` 只需向转换器传一个 `output_path`，把「放哪儿」留给自己、「写什么」留给转换器，职责切分干净。嵌套则会造成深路径与目录权限放大；自选目录则要求步骤对象携带路径状态，破坏「转换器是无状态纯函数」的心智模型。

### 4.3 架构复盘：分层、取舍与可扩展点

#### 4.3.1 概念说明

收官需要回答一个元问题：**pdf-craft 为什么长成这样？扩展点为什么开在这里？**

把整条链路压缩成一句话：**不可信的 PDF 输入，经确定性的提取管线，变成磁盘上中立、可校验的 `DocumentPackage` 契约；一切「内容级」变化（翻译、改写、增强）都作为「包进包出」的纯函数串在契约之上；最后由格式专属的渲染器或回写器消费。**

这个形态的三个 architectural bets（架构押注）：

1. **中间产物押注磁盘而非内存**。包不是内存对象而是目录树，天然获得：可检查（`ls` 就能看）、可缓存（`ocr/`、`toc.xml` 断点续跑）、可手工干预（改 `toc.xml` 重切章节）、可逆转（转换器复制而非原地改）。
2. **扩展点押注在契约之上、渲染之下**。太早（OCR 输出层）会被版式细节缠住，太晚（渲染输出层）只能做正则替换、丢掉全部结构。夹在中间，转换器既拿到结构化 `Chapter` 树，又不绑定任何输出格式——同一个步骤对象可用于 Markdown、EPUB、PDF 回写三条出口。
3. **LLM 押注为可替换基础设施**。`LLM` 配置类 + `runtime_for` 运行时 + 修复循环协议，把「模型不可靠」隔离在传输层（重试）与语义层（反馈重写）两道防线内；转换器协议对此零感知——`_DeterministicXMLTranslator` 这种不调模型的确定性实现照样跑通全链路（测试就是这么写的）。

#### 4.3.2 核心流程

全书数据流与扩展点位置（回顾 u1-l3 的模块地图，标出本讲焦点）：

```
PDF ──OCR──> ocr/page_N.xml ──目录分析──> toc.xml ──章节生成──> chapters/*.xml
                                                                        │
                                        ┌───────────────────────────────┘
                                        ▼
                              DocumentPackage（契约）
                                        │
        ╔═══════════════════════════════╪═══════════════════════════╗
        ║   扩展层：steps = [PackageTransformer | TranslationStep]   ║  ← 本讲焦点
        ║   每步: transform(包, 输出目录) -> 新包（原包只读）          ║
        ╚═══════════════════════════════╤═══════════════════════════╝
                                        ▼
            ┌───────────────┬───────────┴──────────┐
       MarkdownRenderer  EpubRenderer      PDFPatcher（回写）
```

#### 4.3.3 源码精读

**公开边界是刻意收窄的**

[pdf_craft/transformer/__init__.py:L1-L6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/__init__.py#L1-L6) 只导出八个名字：两个协议、两个适配器、引擎与三个翻译类型。顶层 [pdf_craft/\_\_init\_\_.py:L15-L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L15-L22) 原样转发。对照 [docs/en/API_REFERENCE.md:L98-L106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L98-L106) 的扩展类型表——文档承诺的「为需要自定义结构变换的应用暴露的类」与导出清单一一对应，不多不少。

**门面的惰性组装**

[pdf_craft/craft.py:L253-L262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262) 的 `_pdf_engine` 把 `PDFExtractionEngine` 的 import 推迟到首次提取，L258 注释点明动机：「让 EPUB-only 调用者永远不导入历史适配器」。这与转换器协议的设计同源——**每一层都假设下游可能不存在**。

**契约测试固化扩展点行为**

[tests/test_composable_boundaries.py:L97-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L97-L108) 的 `_DeterministicXMLTranslator` 证明转换器协议不要求 LLM：它给每个文本节点加 `T:` 前缀就算「翻译」完成。[tests/test_composable_boundaries.py:L143-L173](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L143-L173) 用它驱动真实 `ChapterPackageTransformer`，断言空章被跳过（`translator.calls == 1`）且空章文件逐字节不变——这是「你的自定义转换器应该 mimic 的契约测试写法」：小包手工搭建 + 确定性协作者 + 逐文件断言。

**取舍清单（总结表）**

| 决策 | 取 | 舍 | 代价与补偿 |
| --- | --- | --- | --- |
| 扩展层级 | 包级（复制—改写） | 原地改写内存对象 | 每步全量复制包，磁盘与 IO 放大；换来原包不可变、链路可中断可检查 |
| 转换器粒度 | 章级（`ChapterTransformer`） | 块级 / 页级 | 粒度太细则无法做跨段落决策；块级需求由转换器自己遍历 `layouts` 满足 |
| 结构表示 | `Chapter` 对象树 + XML 落盘 | 自定义 DSL / AST | XML 可手工检查编辑（`toc.xml` 玩法的基础）；编解码幂等性由 `encode`/`decode` 双向测试保证 |
| LLM 接入 | 双 LLM + 修复循环 + 缓存 | 单次调用祈祷 | 配置面变大；换来结构遵从率与可复算性 |
| 门面 | 一步式方法固定三段（提取→步骤→渲染） | 自由编排 DAG | 编排自由度受限；复杂编排自行组合积木式方法（`extract_pdf` + `translate_package` + `render_*`）即可 |

#### 4.3.4 代码实践

**实践三：扩展点选型自检 + 架构笔记**

1. **实践目标**：为你的场景书面论证「包级 vs 章级」的选型，形成一页架构笔记。
2. **操作步骤**：对照下表逐行打勾，写出你的场景每行的结论，最后合成一段 200 字左右的选型理由：

   | 判据 | 选包级 `PackageTransformer` | 选章级 `ChapterTransformer` |
   | --- | --- | --- |
   | 需要读全书才能决定输出？（术语表、索引、交叉引用） | ✓ | ✗ |
   | 需要改 `toc.xml` / 往包里加新文件？ | ✓（`toc_transformer` 或自写） | ✗ |
   | 只做逐章独立的文本替换？ | 可（经 `ChapterPackageTransformer` 包装） | ✓（更少代码） |
   | 需要非标准的输出包结构？ | ✓（自控目录布局） | ✗ |
   | 想复用 `SubmitKind` 模式语义？ | 需自实现 `with_mode` | ✓（信封模式自动传播） |

3. **需要观察的现象**：无运行现象，产出是笔记本身；重点检验你的理由是否引用了真实源码行为（如信封模式传播见 [pdf_craft/craft.py:L243-L251](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L243-L251)，`toc_transformer` 见 [pdf_craft/transformer/package.py:L54-L55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L54-L55)）。
4. **预期结果**：一页笔记，含选型结论、三条以上源码依据、至少一个「我放弃的方案及原因」。

#### 4.3.5 小练习与答案

**练习 1**：有人提议「把转换器改成接收内存中的 `Chapter` 列表、返回新列表，省去 XML 编解码与目录复制」。用本仓库的一个已验证行为反驳或支持他。

**答案**：反驳。磁盘契约带来的能力在仓库里都有实证：`toc.xml` 存在即短路目录分析、手改后重切章节零成本（u4-l3）；`ocr/page_N.xml` 支撑断点续跑（u3-l3）；组合边界测试 [tests/test_composable_boundaries.py:L143-L173](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L143-L173) 直接以目录为断言对象。纯内存方案失去可检查性与断点能力，且进程崩溃即丢全部中间产物；复制成本可用「只在步骤间落盘、步骤内全内存」缓解——这正是 `ChapterPackageTransformer` 的循环结构（decode→transform→encode 在内存中完成，仅在步骤边界落盘）。

**练习 2**：`PDFCraft.translate_package` 的 docstring（[pdf_craft/craft.py:L126-L131](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L126-L131)）说「公开操作刻意保持单一且翻译聚焦；任意包转换链仍是内部组合细节」。这与你「任意 `PackageTransformer` 可作步骤」的用法矛盾吗？

**答案**：不矛盾，是两个不同层面的承诺。`translate_package` 是**公开方法层**的克制——门面不为每种组合各开一个便捷方法，避免 API 膨胀；而 `steps` 参数与 `PackageTransformer` 协议是**扩展点层**的开放——高级应用自组合。文档 [docs/en/API_REFERENCE.md:L98-L106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L98-L106) 明确把 `PackageTransformer` 列为公开协议。这正是「便捷层收窄、能力层开放」的分层取舍。

**练习 3**：如果你的自定义步骤需要知道「这本书一共有几章」来均匀分配 LLM 预算，应该在包级还是章级实现？为什么？

**答案**：包级。章级转换器每次只见到一个 `Chapter`，天然无法回答全书问题（除非借助外部状态，那会破坏无状态纯函数假设并引入执行顺序依赖）。包级转换器在 `transform` 里先 `create_chapters_reader(package.chapters_path)()` 盘点全书（该读取器工厂见 [pdf_craft/extractor/chapter/reader.py:L8-L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reader.py#L8-L16)，门面自己在 [pdf_craft/craft.py:L329-L335](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L329-L335) 也这么用），再逐章改写——「先全局盘点、再局部改写」正是包级存在的意义。

## 5. 综合实践

**收官任务：带领域术语表的翻译 + 章节导读，一步产出成品 EPUB。**

任务定义：你拿到一本英文技术书 PDF，希望产出一个中文 EPUB，要求 (a) 术语按你提供的词表统一翻译；(b) 每章开头有一行导读。两个需求分别落在两个步骤上，串进 `convert_pdf_to_epub`。

第一步：**构建术语表翻译步骤**（复用 `XMLTranslator`）。示例代码：

```python
# glossary_step.py —— 示例代码
from pdf_craft import ChapterXMLTransformer, FillFailedEvent, LLM, SubmitKind, TranslationStep, XMLTranslator

GLOSSARY = """术语表（翻译时必须遵守）:
- transformer -> 转换器
- DocumentPackage -> 文档包
- SubmitKind -> 提交模式
"""

def build_glossary_step(llm: LLM, fill_llm: LLM) -> TranslationStep:
    translator = XMLTranslator(
        translation_llm=llm,
        fill_llm=fill_llm,
        target_language="简体中文",
        user_prompt=GLOSSARY,              # 注入 translate.jinja 的用户规则
        ignore_translated_error=False,
        max_retries=5,
        max_fill_displaying_errors=3,      # 取值参考仓库 CLI 的示范
        max_group_score=2600,
        cache_seed_content=f"my-glossary-v1:简体中文",  # 词表改版时同步改它，隔离缓存
    )
    return TranslationStep(
        ChapterXMLTransformer(translator),  # 引擎 -> 章级转换器
        mode=SubmitKind.APPEND_TEXT,        # 双语：译文追加进同一文本流
    )

def on_fill_failed(event: FillFailedEvent) -> None:
    print(f"回填失败: {event.error}（第 {event.attempt} 次，最终={event.final}）")
```

构造参数与取值依据：`XMLTranslator` 签名见 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:L27-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L27-L38)；`max_fill_displaying_errors=3` 与 `cache_seed_content` 的拼法抄自 [pdf_craft_tool/cli.py:L454-L459](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L454-L459)；`ChapterXMLTransformer` 的适配见 [pdf_craft/transformer/chapter_xml.py:L16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L16)。注意 `user_prompt` 最终由 `translate.jinja` 模板渲染进 system 消息，机制见 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:L154-L167](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L154-L167)。

第二步：**组合两个步骤跑通全链路**。示例代码：

```python
# run_final.py —— 示例代码
from pdf_craft import DeepSeekOCRVendorConfig, LLM, PDFCraft, PDFOptions
from glossary_step import build_glossary_step
from summary_transformer import SummaryPackageTransformer

llm = LLM(key="...", url="https://api.example.com/v1", model="some-model",
          token_encoding="o200k_base",
          cache_path="work/llm-cache", log_dir_path="work/llm-logs")

craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
    base_url="...", api_key="...", model="...",
)))

craft.convert_pdf_to_epub(
    "book.pdf", "book.zh.epub",
    package_path="work/package",
    steps=[
        build_glossary_step(llm, llm),          # 步骤 0：术语表翻译 -> transformed-0
        SummaryPackageTransformer("【导读】"),    # 步骤 1：加导读 -> transformed-1
    ],
)
```

第三步：**验收与复盘**。

1. 检查 `work/package/transformed-0/chapters/chapter_1.xml`：原术语处应出现「转换器」等词表译名；`transformed-1/` 的对应章开头应有导读段落。
2. `unzip -l book.zh.epub` 确认成品结构；打开阅读器验证双语段落与导读。
3. 对照 [pdf_craft/craft.py:L212-L221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L212-L221) 的 `_apply_steps` 写一页数据流笔记：画出「原始包 → transformed-0（翻译）→ transformed-1（导读）→ render_epub」的链，标注每步读哪个目录、写哪个目录、失败时哪一步可以单独重跑（提示：翻译步骤有 LLM 缓存，复跑时翻译命中缓存，导读步骤是本地操作——这正是把「贵的一步」放前面、「便宜的一步」放后面的编排理由）。
4. 撰写 4.3.4 的选型笔记：为什么导读选包级自写、翻译选「`XMLTranslator` + 章级适配」而不是全自写包级翻译器（参考答案方向：翻译需要切组、回填修复、缓存三大机制，自写等于重造 u7 单元的轮子；导读只需要插段落，复用 `ChapterPackageTransformer` 或手写模板都够）。

如无法获得真实凭据，可将第一步替换为实践一那种确定性转换器（例如硬编码词典替换），链路结构与复盘部分完全不变。全部运行结果待本地验证。

## 6. 本讲小结

- **两级扩展点形状固定**：章级 `transform(chapter) -> chapter`、包级 `transform(package, output_path) -> package`；包级实现必须遵循「安检 → 占位 → 复制 → 改写 → 校验交货」五步，原包全程只读。
- **参数名是接口**：`_accepts_package` 按 `transform(self, package, output_path)` 的签名探测包级转换器，参数改名会被误判为章级并在运行时以 `TypeError` 崩溃。
- **步骤链极简且确定**：`steps` 里的元素被 `_as_package_transformer` 归一化后按列表顺序串行执行，输出平铺为原包根下的 `transformed-0/1/...` 兄弟目录，最终只渲染最后一个包；复跑前需清理这些目录。
- **`TranslationStep` 的 `mode` 只对章级语义生效**：包级转换器被解包直用时模式被忽略；要支持模式注入需自实现 `with_mode` 钩子。
- **复用而非重造**：自定义翻译步骤的标准骨架是 `TranslationStep(ChapterXMLTransformer(XMLTranslator(...)))`，构造参数与缓存种子取值可直接参考 EPUB 管线与仓库 CLI 两处官方示范；`user_prompt` 是术语表的注入点，`cache_seed_content` 是词表改版的缓存隔离开关。
- **架构复盘三押注**：中间产物押注磁盘（可检查、可缓存、可手工干预）、扩展点押注契约之上渲染之下（结构可见且格式无关）、LLM 押注为可替换基础设施（协议对确定性实现同样成立）。

## 7. 下一步学习建议

本讲之后，手册的十二个单元已全部完成。三个继续深化的方向：

1. **通读守护架构的测试**：把 [tests/test_module_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py) 与 [tests/test_composable_boundaries.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py) 当作「架构的执行规约」重读一遍——每个断言都是一条你写扩展时不能踩的暗契约。
2. **跟踪上游依赖**：`XMLTranslator` 之下的 `doc-page-extractor`（u3-l4）与渲染器之下的 `epub-generator`（u6-l4）是两个独立演进的包，读它们的变更日志能预判 pdf-craft 下一步的接口调整。
3. **给仓库提一个真实扩展**：把你综合实践里的转换器按仓库惯例补上单元测试（模仿 `_DeterministicXMLTranslator` 的确定性协作者写法），如果它解决的是普遍问题，这正是一次合格贡献的起点。
