# u7-l1 转换器协议：PackageTransformer 与 ChapterTransformer

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 pdf-craft 提供的两级内容转换扩展点——章级 `ChapterTransformer` 与包级 `PackageTransformer`——各自拿到什么输入、承担什么责任。
2. 精读 `ChapterPackageTransformer` 的「复制—改写」实现：它如何在不破坏原包的前提下派生一个全新的 `DocumentPackage`。
3. 掌握 `TranslationStep` 的用法：如何把一个章节转换器接入 `convert_pdf_to_markdown` / `convert_pdf_to_epub` 的一步式流程，以及多步骤链式执行时目录里发生了什么。
4. 独立编写一个自定义 `ChapterTransformer`，并验证「输出被改写、原包不被破坏」这两个关键性质。

## 2. 前置知识

本讲站在前面几讲的结论之上，先用两段话把要用到的背景补齐。

**扩展点与转换器。** 一个库如果只提供固定功能，用户的需求稍变就得改库源码。「扩展点」是库作者预留的插槽：库负责搭好流水线，把流水线中某一步的实现交给你写。pdf-craft 把「提取完成之后、渲染之前」的这一段开放为扩展点，你的代码叫「转换器」（transformer）——输入是提取产物，输出是改写后的提取产物。最典型的转换器就是翻译器：把中文章节换成英文章节，其余流程（渲染 Markdown/EPUB）完全复用。

**结构化类型与 Protocol。** Python 的 `typing.Protocol` 是「鸭子类型的静态检查版」：只要一个类拥有签名匹配的 `transform` 方法，它就自动满足协议，**不需要（也不能通过）继承**来声明关系。pdf-craft 的两个转换器协议都是这样定义的，所以你写转换器时只需保证方法名和参数对得上。与之配套的两个数据结构在前几讲已经建立：

- **Chapter（u5-l1）**：一章的内存对象，`layouts` 列表混排 `ParagraphLayout`（文字段落，内含 `BlockLayout` 块）与 `AssetLayout`（图片/表格/公式资源块）；块内容 `content` 是「字符串、行内公式、脚注引用、HTMLTag」混居的列表。
- **DocumentPackage（u6-l1）**：提取器与渲染器之间的磁盘契约，目录即数据格式（`chapters/`、`assets/` 必备，`toc.xml`、`cover.png`、`document.json` 可选；`ocr/` 是提取器私有缓存，**不属于**契约）。

**SubmitKind。** 三种「译文落地方式」的枚举（[pdf_craft/transformer/xml_translator/xml_translator/submitter.py:L11-L14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/submitter.py#L11-L14)）：`REPLACE`（译文替换原文）、`APPEND_TEXT`（译文追加进同一文本流）、`APPEND_BLOCK`（译文作为独立块追加，通常是更清晰的中英对照排版）。它的语义由具体转换器解释，本讲末尾会看到 PDF 回写明确拒绝 `APPEND_BLOCK`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `pdf_craft/transformer/protocol.py` | 章级协议定义 | `ChapterTransformer` 的最小契约 |
| `pdf_craft/transformer/package.py` | 包级协议 + 默认包级实现 | `PackageTransformer` 协议、`ChapterPackageTransformer` 的复制—改写 |
| `pdf_craft/transformer/chapter_xml.py` | XML 翻译适配器 | `XMLTaskTranslator` 协议、`ChapterXMLTransformer`（为下一讲铺垫） |
| `pdf_craft/craft.py` | 门面 | `TranslationStep`、`_apply_steps`、`_as_package_transformer` 分派逻辑 |
| `pdf_craft/document/package.py` | 包契约 | `from_path` / `validate`（派生包的收尾校验） |
| `pdf_craft/common/xml.py` | XML 工具 | `read_xml` / `save_xml`（原子写） |
| `tests/test_craft.py` | 单元测试 | 手工构造迷你包驱动转换器的测试写法 |

导入路径提示：`ChapterPackageTransformer`、`PackageTransformer`、`SubmitKind` 等从包顶层 `from pdf_craft import ...` 即可（见 [pdf_craft/__init__.py:L15-L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L15-L22)）；而 `ChapterTransformer` 是公共协议但**不在包顶层导出**，需从 `from pdf_craft.transformer import ChapterTransformer` 导入（见 [pdf_craft/transformer/__init__.py:L1-L6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/__init__.py#L1-L6)），`Chapter` 数据类则从 `pdf_craft.extractor.chapter.chapter` 导入——官方 API 参考明确了这一点。

## 4. 核心概念与源码讲解

### 4.1 转换器协议：两级扩展点

#### 4.1.1 概念说明

pdf-craft 用两个 Protocol 把转换器分成两级粒度：

| | `ChapterTransformer`（章级） | `PackageTransformer`（包级） |
| --- | --- | --- |
| 输入 | 一个解码好的 `Chapter` 内存对象 | 原 `DocumentPackage`（一组路径）+ 输出目录 |
| 输出 | 改写后的 `Chapter` | 落盘后的新 `DocumentPackage` |
| 你要操心 XML 读写吗 | 不要，库代劳 | 要，全权负责产物目录 |
| 适合 | 逐章文本处理（翻译、清洗、打标） | 跨章/全局操作（目录翻译、重排章节、增删资源） |

章级是「低层协议」：门槛低，拿到的是熟悉的 dataclass，改字段就行；包级是「高层协议」：自由度大，但连「复制 assets 目录」这样的杂务都得自己做。库提供了默认的包级实现 `ChapterPackageTransformer`（4.2 节）把章级升级成包级，所以大多数场景只需写章级转换器。

此外还有一个「消费侧」协议 `XMLTaskTranslator` 值得认识：`ChapterXMLTransformer` 需要 XML 翻译器，却不 import 具体的 `XMLTranslator` 类，而是声明「我只需要一个有 `translate_element` 方法的对象」。这是依赖倒置——协议不只用来让你实现，也用来让库自己面向接口编程。`ChapterXMLTransformer` 是官方内置的桥接件，把下一讲的 `XMLTranslator` 适配成 `ChapterTransformer`，本讲先认识它的骨架。

#### 4.1.2 核心流程

调用方视角下，两个协议的使用方式：

```text
# 章级：库循环，你只写单章函数
for chapter_xml in chapters/*.xml:
    chapter = decode(chapter_xml)          # 库：XML → 内存对象
    chapter = your_transformer.transform(chapter)   # 你：改内存对象
    save_xml(encode(chapter), chapter_xml) # 库：内存对象 → XML

# 包级：库只给坐标，你写整个目录的生成过程
new_package = your_transformer.transform(原包路径, 输出路径)
assert new_package 满足 DocumentPackage 契约
```

#### 4.1.3 源码精读

先看章级协议，全文只有几行（[pdf_craft/transformer/protocol.py:L1-L6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/protocol.py#L1-L6)）：

```python
class ChapterTransformer(Protocol):
    """Format-neutral transformation contract used by document pipelines."""
    def transform(self, chapter: Chapter) -> Chapter: ...
```

「format-neutral」（格式无关）是关键词：协议不知道 Markdown、EPUB 为何物，只操作 `Chapter`。这保证了同一个转换器可以被 Markdown 渲染和 EPUB 渲染复用。

再看包级协议（[pdf_craft/transformer/package.py:L16-L19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L16-L19)）：

```python
class PackageTransformer(Protocol):
    """A format-neutral transformation from one package to another."""

    def transform(self, package: DocumentPackage, output_path: Path) -> DocumentPackage: ...
```

注意输入输出都是 `DocumentPackage`——「包进包出」。`output_path` 由调用方指定，且约定**必须是尚不存在的目录**（4.2 节会看到强制检查），这使得每次转换都是「派生新包」而非「原地改写」。

第三个协议是 `XMLTaskTranslator`（[pdf_craft/transformer/chapter_xml.py:L9-L11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L9-L11)）：

```python
class XMLTaskTranslator(Protocol):
    """The public XMLTranslator subset required for a Chapter task."""
    def translate_element(self, task: TranslationTask[Chapter], **kwargs) -> tuple[Element, Chapter]: ...
```

它声明的方法签名恰好是 `XMLTranslator.translate_element` 的公开子集。任务对象 `TranslationTask` 只有三个字段：待翻的 XML 元素、`action`（即 `SubmitKind`）与任意负载 `payload`（见 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:L19-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L19-L23)）。

最后看内置适配器 `ChapterXMLTransformer`（[pdf_craft/transformer/chapter_xml.py:L28-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L28-L38)）：

```python
def transform(self, chapter: Chapter) -> Chapter:
    element = encode(chapter)
    if not any(segment.text.strip() for segment in search_text_segments(element)):
        return chapter          # 空章节直接放行，不进翻译
    translated, _ = self._translator.translate_element(
        TranslationTask(element=element, action=self._mode, payload=chapter)
    )
    return decode(translated)
```

它的套路是「编码 → 翻译 → 解码」：先把 `Chapter` 编码成 XML 元素交给翻译器，再把翻好的元素解码回 `Chapter`。第 33 行的守卫很务实——OCR 会对「没有可翻文本的页」也产出章节文件，直接喂给翻译器会触发 "Translation failed unexpectedly"，所以先检查文本段全为空就原样返回。它还实现了 `with_mode`（[pdf_craft/transformer/chapter_xml.py:L24-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L24-L26)），返回一个换了提交模式的新实例——这个方法名在 4.2 节会被 `ChapterPackageTransformer` 探测调用，是自定义转换器获得「模式感知」能力的钩子。

#### 4.1.4 代码实践

**实践目标**：不用任何 OCR/LLM 凭据，手工搭一个迷你 `DocumentPackage`，写一个 `Upper` 转换器把文本转大写，验证协议「实现即满足」、且转换只影响派生包。

**操作步骤**：保存以下脚本并运行（示例代码，写法对照 [tests/test_craft.py:L39-L58](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L39-L58) 的测试装配方式）：

```python
# practice_upper.py（示例代码）
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree.ElementTree import tostring

from pdf_craft.craft import PDFCraft
from pdf_craft.document import DocumentPackage
from pdf_craft.extractor.chapter.chapter import (
    BlockLayout, Chapter, ParagraphLayout, encode,
)


def build_mini_package(root: Path) -> DocumentPackage:
    package = DocumentPackage.from_path(root / "source")
    package.chapters_path.mkdir(parents=True)   # chapters/ 必备
    package.assets_path.mkdir()                 # assets/ 必备
    package.write_metadata(page_pixel_sizes={1: (10, 10)})
    chapter = Chapter(
        id=None, level=-1,
        layouts=[ParagraphLayout(
            "text", 0, [BlockLayout(1, 1, (1, 1, 5, 5), ["hello pdf-craft"])],
        )],
    )
    (package.chapters_path / "chapter_1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + tostring(encode(chapter), encoding="unicode"), encoding="utf-8")
    return package


class Upper:
    """实现 ChapterTransformer 协议：注意没有继承任何基类。"""
    def transform(self, chapter: Chapter) -> Chapter:
        for layout in chapter.layouts:
            if not isinstance(layout, ParagraphLayout):
                continue                    # 跳过图片/表格等资源块
            for block in layout.blocks:
                block.content = [str(part).upper() for part in block.content]
        return chapter


with TemporaryDirectory() as directory:
    root = Path(directory)
    source = build_mini_package(root)
    target = PDFCraft().translate_package(source, root / "target", Upper())
    print((target.chapters_path / "chapter_1.xml").read_text(encoding="utf-8"))
    print((source.chapters_path / "chapter_1.xml").read_text(encoding="utf-8"))
```

**需要观察的现象**：`Upper` 没有继承 `ChapterTransformer` 却能直接传给 `translate_package`；打印的两份 XML 内容不同。

**预期结果**（待本地验证）：`target/chapters/chapter_1.xml` 中的文本变为 `HELLO PDF-CRAFT`，`source/chapters/chapter_1.xml` 仍为 `hello pdf-craft`。若删掉 `assets` 目录的创建语句，则会在 `validate` 处报 `missing assets directory`。

#### 4.1.5 小练习与答案

**练习 1**：如果给你的转换器类起名为 `process` 而不是 `transform`，还能满足 `ChapterTransformer` 协议吗？

**答案**：不能。Protocol 是结构化类型，匹配依据是方法名加签名；没有 `transform(self, chapter) -> Chapter` 方法就不满足协议，传入后调用时会抛 `AttributeError`。

**练习 2**：`ChapterTransformer` 与 `PackageTransformer` 各适合以下哪个需求？(a) 把全书脚注编号整体偏移；(b) 把每章出现的旧术语替换为新术语。

**答案**：(a) 包级——脚注编号是全书统一的编号空间（u6-l3 讲过按 `(page_index, order)` 全书编号），跨章状态适合包级转换器；(b) 章级——逐章独立替换，用默认的 `ChapterPackageTransformer` 包装即可复用整套落盘逻辑。

**练习 3**：`XMLTaskTranslator` 协议为什么不让 `ChapterXMLTransformer` 直接 `from pdf_craft import XMLTranslator` 再调用？

**答案**：为了让 `ChapterXMLTransformer` 只依赖「最小必需接口」而不是具体实现。协议只声明了它用到的 `translate_element` 子集，任何满足该签名的对象（包括测试替身）都能注入，具体类 `XMLTranslator`（连带 LLM 运行时）成为可替换的实现细节。

### 4.2 包级实现：ChapterPackageTransformer

#### 4.2.1 概念说明

`ChapterPackageTransformer` 是库自带的「章级 → 包级」适配器：你给它一个章级转换器，它替你完成包级的全部杂务——校验、建目录、复制资源、循环读写每章 XML、收尾再校验。它的核心策略是**复制—改写**：

1. 先把原包完整复制到输出目录（chapters、assets、toc、cover、document.json）；
2. 再在**副本**上逐章 decode → transform → encode 落盘；
3. 原包从头到尾只读不写。

为什么复制全部成员而不是只写改过的章节？因为下游渲染器需要的是一个**完整契约**的包（u6-l1）：Markdown/EPUB 渲染要读 assets 里的图片、EPUB 渲染强制要求 `toc.xml`。派生包必须「开箱即可渲染」。反过来说，`ocr/` 目录（提取器私有缓存）**不复制**——派生包只服务渲染，不是新的提取缓存。

「不可变原包」带来一个重要推论（u1-l4 已提过）：转换链每一步都产出新包，任何一步出错都不会污染上游，你可以放心对同一个原包反复试验不同转换器。

#### 4.2.2 核心流程

`ChapterPackageTransformer.transform(package, output_path)` 的执行过程：

1. `package.validate()`——先安检原包（chapters/、assets/ 必须存在）。
2. 若 `output_path` 已存在 → 抛 `FileExistsError`，拒绝覆盖。
3. `mkdir` 输出目录，`copytree` 复制 `chapters/` 与 `assets/`。
4. `toc.xml`、`cover.png`、`document.json` 三个可选成员，存在才 `copy2`。
5. 按文件名排序遍历副本中所有 `chapter*.xml`（含 `chapter_head.xml`，见 u5-l3）：
   `decode(read_xml(path))` → `chapter_transformer.transform(chapter)` → `save_xml(encode(...), path)`。
6. 若提供了 `toc_transformer` 且原包有 `toc.xml`：对副本的 `toc.xml` 做元素级变换后落盘（典型用途：翻译目录条目标题）。
7. `DocumentPackage.from_path(output_path).validate()` 重建并校验派生包，作为返回值。

构造阶段另有一条模式传播逻辑：当 `mode != REPLACE` 且章级转换器实现了 `with_mode` 方法时，先用 `with_mode(mode)` 换取一个模式感知的新转换器。`ChapterXMLTransformer` 正是靠这个钩子把 `APPEND_TEXT/APPEND_BLOCK` 语义传给 `XMLTranslator` 的；普通自定义转换器不实现 `with_mode` 也不影响，只是拿不到模式信息。

#### 4.2.3 源码精读

构造函数与模式传播（[pdf_craft/transformer/package.py:L22-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L22-L36)）：

```python
def __init__(self, chapter_transformer, *, mode=SubmitKind.REPLACE,
             toc_transformer=None):
    if mode != SubmitKind.REPLACE and hasattr(chapter_transformer, "with_mode"):
        chapter_transformer = getattr(chapter_transformer, "with_mode")(mode)
    self.chapter_transformer = chapter_transformer
    self.mode = mode
    self.toc_transformer = toc_transformer
```

`chapter_transformer` 与 `mode` 存为公开属性——这不是随手为之，4.3 节会看到门面正是读取这两个属性来做模式协调的。

主体流程（[pdf_craft/transformer/package.py:L38-L56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L38-L56)）：

```python
def transform(self, package: DocumentPackage, output_path: Path) -> DocumentPackage:
    package.validate()
    if output_path.exists():
        raise FileExistsError(f"output package already exists: {output_path}")
    output_path.mkdir(parents=True)
    copytree(package.chapters_path, output_path / "chapters")
    copytree(package.assets_path, output_path / "assets")
    for source in (package.toc_path, package.cover_path, package.metadata_path):
        if source is not None and source.exists():
            copy2(source, output_path / source.name)

    for path in sorted((output_path / "chapters").glob("chapter*.xml")):
        chapter = decode(read_xml(path))
        transformed = self.chapter_transformer.transform(chapter)
        save_xml(encode(transformed), path)
    ...
    return DocumentPackage.from_path(output_path).validate()
```

几个细节值得咀嚼：

- 第 40—41 行的存在性检查把「派生」变成硬约束：同名输出目录已存在就报错，绝不静默覆盖（重跑实验前要清理目录，这是使用时的常见坑）。
- 第 49 行 `sorted(glob("chapter*.xml"))` 保证按确定顺序处理章节；通配符同时覆盖 `chapter_1.xml`、`chapter_2.xml` 和前置章节 `chapter_head.xml`。
- 你的转换器抛出的任何异常都会中断循环——此时副本目录已经建了一半，但由于原包未被触碰，删掉残缺的输出目录重跑即可，没有半写坏数据的风险。
- 落盘用的是 `save_xml`（[pdf_craft/common/xml.py:L28-L40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L28-L40)）：先写 `.xml.tmp` 临时文件再 `replace` 原子改名，进程中途被杀也不会留下半个 XML——和 u4-l3 讲过的 `toc.xml`「文件存在即完整」是同一条纪律。

#### 4.2.4 代码实践

**实践目标**：绕开门面，直接调用 `ChapterPackageTransformer`，验证三件事——原包不被改写、已存在的输出目录触发 `FileExistsError`、`toc_transformer` 能改到副本的 `toc.xml`。

**操作步骤**：在 4.1.4 的脚本里追加（示例代码）：

```python
# practice_pkg.py（示例代码）
from pdf_craft.transformer import ChapterPackageTransformer

with TemporaryDirectory() as directory:
    root = Path(directory)
    source = build_mini_package(root)
    source.toc_path.write_text('<toc page_indexes="1"><item /></toc>')

    def mark_toc(element):            # Element → Element
        element.set("translated", "yes")
        return element

    transformer = ChapterPackageTransformer(
        Upper(), toc_transformer=mark_toc)
    target = transformer.transform(source, root / "t1")

    print("原包不动:", "hello" in
          (source.chapters_path / "chapter_1.xml").read_text())
    print("目录被改:", 'translated="yes"' in target.toc_path.read_text())

    try:                              # 同一输出路径再来一次
        transformer.transform(source, root / "t1")
    except FileExistsError as error:
        print("重复输出被拒:", error)
```

**需要观察的现象**：`t1` 目录中 `chapters/`、`assets/`、`toc.xml`、`document.json` 一应俱全，但没有 `ocr/`；第二次调用立刻抛错。

**预期结果**（待本地验证）：依次打印 `原包不动: True`、`目录被改: True`、`重复输出被拒: output package already exists: .../t1`。此行为与单元测试 [tests/test_craft.py:L73-L104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L73-L104)（源包原文保留、目标包含译文且 `toc.xml` 内容一致）和 [tests/test_craft.py:L106-L128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L106-L128)（`toc_transformer` 生效）的断言一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 45 行复制 `toc.xml`/`cover.png`/`document.json` 时要判断 `source.exists()`，而 `chapters`、`assets` 不用？

**答案**：因为契约（u6-l1）规定 `chapters/`、`assets/` 必备（`validate` 会强制检查，缺了早在第 39 行就抛错），而 `toc.xml`、`cover.png`、`document.json` 是可选成员，提取产物可能没有（例如 `generate_plot=False` 时无封面图），存在才复制。

**练习 2**：你的章级转换器在第 3 章处理到一半抛了异常，此时原包和输出目录分别是什么状态？

**答案**：原包完好无损（全程只读）；输出目录是一个残缺的复制品——前几章可能已被改写、后面的章节还是复制来的原文。由于该目录未通过最后的 `from_path().validate()` 返回，它不会被当作合法派生包使用；删掉它重跑即可。

**练习 3**：想让自定义章级转换器在 `APPEND_TEXT` 模式下「追加译文」而在 `REPLACE` 模式下「替换原文」，需要实现什么？

**答案**：实现 `with_mode(self, mode)` 方法，返回一个按 `mode` 分支行为的新转换器实例（参考 [pdf_craft/transformer/chapter_xml.py:L24-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L24-L26)）。`ChapterPackageTransformer` 构造时探测到该方法就会用它取得模式感知版本。

### 4.3 步骤包装：TranslationStep 接入一步式流程

#### 4.3.1 概念说明

前面两个协议是「实现侧」的扩展点，而 `TranslationStep` 是「使用侧」的信封：一个冻结 dataclass，把「转换器 + 提交模式」打包成一项，塞进 `convert_pdf_to_markdown` / `convert_pdf_to_epub` / `translate_pdf` 的 `steps` 参数。`steps` 接受两类元素：

- `TranslationStep(transformer, mode=...)`：transformer 可以是章级转换器（自动包装成 `ChapterPackageTransformer`），也可以是包级转换器；
- 裸的 `PackageTransformer` 对象：直接使用，不经过包装。

为什么需要这层信封？因为一步式方法的签名只认包级形状，而用户手里的大多是章级转换器；同时 `mode` 是「请求」而非「转换器内部状态」，需要一处显式声明的位置。信封的存在让 `steps=[TranslationStep(我的转换器, mode=SubmitKind.APPEND_TEXT)]` 成为一行就能接入的声明式用法。

多步骤按列表顺序执行，每步产出一个新包，目录名依次为原包根下的 `transformed-0`、`transformed-1`、……——上一讲 u1-l4 说的「转换步骤链式产出 transformed-N 新包，原包不可变」正是在此落地。

#### 4.3.2 核心流程

一步式方法内部的步骤执行（`_apply_steps`）：

```text
current = 提取产出的原包
for index, step in enumerate(steps):
    transformer = _as_package_transformer(step)      # 统一升级成包级
    output = 原包根 / f"transformed-{index}"
    current = transformer.transform(current, output)  # 包进包出，链式传递
渲染 current
```

`_as_package_transformer` 的四路分派（按顺序判断）：

1. `step` 本身就是裸 `PackageTransformer` → 原样返回；
2. `TranslationStep` 里包的已是 `ChapterPackageTransformer` → 做**模式协调**：若 `step.mode` 非 `REPLACE` 且与转换器自身 `mode` 不一致，用其 `chapter_transformer` 按 `step.mode` 重建；`step.mode` 为默认 `REPLACE` 时不覆盖转换器自带的模式；
3. `step.transformer` 的 `transform` 方法**恰好有**名为 `package`、`output_path` 的两个位置参数（用 `inspect.signature` 嗅探） → 认定为包级，直接使用；
4. 其余一律视为章级 → 包进 `ChapterPackageTransformer(chapter_transformer, mode=step.mode)`。

第 3 条的签名嗅探值得注意：`transform(self, chapter, *, trace=False)` 只有一个位置参数，不会误判为包级（有专门测试 [tests/test_craft.py:L162-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L162-L170) 守住这一点）——判定依据是位置参数的个数**和名字**，不是猜。

另外 `translate_pdf` 在转换开始前有一步模式预检：用 `_step_mode` 求出每个步骤的有效模式（`TranslationStep` 显式给了非 `REPLACE` 模式用之，否则回退到转换器自身的 `mode` 属性），凡遇 `APPEND_BLOCK` 立刻抛 `ValueError`。原因在 u10 会展开：PDF 回写要把译文按 bbox 放回原版式，「新增独立块」没有几何坐标可依。

#### 4.3.3 源码精读

`TranslationStep` 定义在门面文件里（[pdf_craft/craft.py:L30-L35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L30-L35)）：

```python
@dataclass(frozen=True)
class TranslationStep:
    """A user-requested content transformation inserted before rendering."""

    transformer: ChapterTransformer | PackageTransformer
    mode: SubmitKind = SubmitKind.REPLACE
```

一步式方法中的挂接点：`convert_pdf_to_markdown` 在提取与渲染之间调用 `_apply_steps`（[pdf_craft/craft.py:L185-L189](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L185-L189)），`convert_pdf_to_epub` 同构（[pdf_craft/craft.py:L202-L204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L202-L204)）：

```python
with _package_workspace(package_path) as workspace:
    package, metering = self.extract_pdf_with_metering(source, workspace, extraction)
    package = self._apply_steps(package, steps)
    self.render_markdown(package, output, assets_path, ...)
```

链式执行本体（[pdf_craft/craft.py:L212-L221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L212-L221)）：

```python
def _apply_steps(self, package, steps) -> DocumentPackage:
    current = package
    for index, step in enumerate(steps):
        transformer = self._as_package_transformer(step)
        output = package.chapters_path.parent / f"transformed-{index}"
        current = transformer.transform(current, output)
    return current
```

注意 `transformed-{index}` 挂在**最初那个包**的根目录下（`chapters_path.parent` 就是包根），而不是上一个派生包里——所有派生包平铺做兄弟，方便你逐个检查中间结果。

四路分派本体（[pdf_craft/craft.py:L238-L251](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L238-L251)）：

```python
@staticmethod
def _as_package_transformer(step) -> PackageTransformer:
    if not isinstance(step, TranslationStep):
        return step
    transformer = step.transformer
    if isinstance(transformer, ChapterPackageTransformer):
        if step.mode != SubmitKind.REPLACE and transformer.mode != step.mode:
            return ChapterPackageTransformer(
                transformer.chapter_transformer, mode=step.mode
            )
        return transformer
    if _accepts_package(transformer):
        return cast(PackageTransformer, transformer)
    return ChapterPackageTransformer(cast(ChapterTransformer, transformer), mode=step.mode)
```

签名嗅探的实现（[pdf_craft/craft.py:L280-L292](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L280-L292)）：取出 `transform` 的全部位置参数，要求恰好两个且名字依次是 `package`、`output_path`——名字和数量都对才算包级。

PDF 预检与 `_step_mode`（[pdf_craft/craft.py:L150-L155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L150-L155)、[pdf_craft/craft.py:L295-L298](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L295-L298)）：

```python
for step in steps:
    mode = _step_mode(step, self._as_package_transformer(step))
    if mode == SubmitKind.APPEND_BLOCK:
        raise ValueError("PDF output does not support APPEND_BLOCK")
```

对应测试 [tests/test_craft.py:L130-L160](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L130-L160) 用三种携带方式（`TranslationStep`、`ChapterPackageTransformer(mode=...)`、自定义包级转换器）验证了拒绝行为。

最后看一个「协议适配」的现成范例——`translate_pdf` 兼容旧式 `Callable[[str], str]` 文本回调的适配器（[pdf_craft/craft.py:L301-L316](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L301-L316)）：

```python
class _TextChapterTransformer:
    """Adapt the legacy block-text callback to the package transformer shape."""

    def transform(self, chapter: Chapter) -> Chapter:
        for layout in chapter.layouts:
            if not isinstance(layout, ParagraphLayout):
                continue
            for block in layout.blocks:
                text = _to_patch_text(block.content)
                translated = self._callback(text)
                if translated != text:
                    block.content = [translated]
        return chapter
```

它就是我们本讲实践要写的转换器的「官方版本」：遍历段落与块、改写块内容、原样返回章节。写自己的转换器时照这个骨架来即可。

#### 4.3.4 代码实践

**实践目标**：实现本讲规格指定的任务——一个把每章所有文本块末尾追加标记字符串的 `ChapterTransformer`，包装成 `TranslationStep` 接入 `convert_pdf_to_markdown`，验证输出 Markdown 已被改写、原包未被破坏。

**版本 A：有 OCR 凭据（真实管线）**。保存以下脚本（示例代码；OCR 配置参考 u2-l1）：

```python
# practice_step.py（示例代码）
from pdf_craft import DeepSeekOCRVendorConfig, PDFCraft, PDFOptions, TranslationStep
from pdf_craft.extractor.chapter.chapter import Chapter, ParagraphLayout

class MarkerTransformer:
    def transform(self, chapter: Chapter) -> Chapter:
        for layout in chapter.layouts:
            if not isinstance(layout, ParagraphLayout):
                continue
            for block in layout.blocks:
                block.content = [*(block.content), " [MARK]"]
        return chapter

craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
    base_url="...", api_key="...", model="...")))
metering = craft.convert_pdf_to_markdown(
    "book.pdf", "book.mark.md",
    package_path="work/book-package",   # 保留中间包，便于观察
    steps=[TranslationStep(MarkerTransformer())],
)
```

**版本 B：无凭据的离线等价模拟**。`PDFCraft.from_engine` 支持注入假引擎跳过 OCR（测试专用通道，[tests/test_craft.py:L13-L24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py#L13-L24) 是范本）：

```python
# practice_step_offline.py（示例代码，离线模拟提取）
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree.ElementTree import tostring

from pdf_craft.craft import PDFCraft, TranslationStep
from pdf_craft.extractor.chapter.chapter import (
    BlockLayout, Chapter, ParagraphLayout, encode,
)

class FakeEngine:
    def extract_package(self, *, pdf_path, analysing_path, **kwargs):
        (analysing_path / "chapters").mkdir(parents=True)
        (analysing_path / "assets").mkdir()
        chapter = Chapter(None, -1, [ParagraphLayout(
            "text", 0, [BlockLayout(1, 1, (1, 1, 5, 5), ["hello pdf-craft"])])])
        (analysing_path / "chapters" / "chapter_1.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + tostring(encode(chapter), encoding="unicode"), encoding="utf-8")
        return None, None, None, None, "metering"

with TemporaryDirectory() as directory:
    root = Path(directory)
    craft = PDFCraft.from_engine(FakeEngine())
    craft.convert_pdf_to_markdown(
        "whatever.pdf", root / "book.md",
        package_path=root / "package",
        steps=[TranslationStep(MarkerTransformer())],   # 类定义同版本 A
    )
    print((root / "book.md").read_text(encoding="utf-8"))
    print((root / "package" / "transformed-0" / "chapters" / "chapter_1.xml")
          .read_text(encoding="utf-8"))
```

**需要观察的现象**（四个验证点）：

1. 输出的 `book.md` 每个段落末尾出现 `[MARK]`；
2. `work/book-package/chapters/chapter_1.xml`（原包）**不含** `[MARK]`——渲染消费的是派生包；
3. 包根下出现 `transformed-0/`，结构与原包对齐；
4. 用同一个 `package_path` 原样重跑会抛 `FileExistsError: output package already exists: .../transformed-0`（4.2 节的拒绝覆盖约束在链路上的体现），删除 `transformed-0` 后可重跑。

**预期结果**（待本地验证）：版本 B 打印的 Markdown 含 `hello pdf-craft [MARK]`，`transformed-0` 的章节 XML 含 `[MARK]` 而原包 `package/chapters/chapter_1.xml` 不含。

#### 4.3.5 小练习与答案

**练习 1**：`steps=[TranslationStep(A), TranslationStep(B)]` 执行后，`B.transform` 收到的章节内容来自哪里？目录里多了哪几个包？

**答案**：`B` 收到的是 `A` 处理过的派生包（链式：原包 → `transformed-0` → `transformed-1`）。原包根下多出 `transformed-0`、`transformed-1` 两个平级目录，最终渲染消费 `transformed-1`。

**练习 2**：`translate_pdf("s.pdf", package, "out.pdf", lambda t: t, steps=[TranslationStep(t1, SubmitKind.APPEND_BLOCK)])` 会发生什么？为什么 `convert_pdf_to_markdown` 不拦？

**答案**：`translate_pdf` 在转换开始前抛 `ValueError("PDF output does not support APPEND_BLOCK")`。因为 PDF 回写要把译文放回原文的检测框位置，`APPEND_BLOCK` 产生的「新增独立块」没有几何坐标可安置；而 Markdown/EPUB 是流式排版，追加块天然可行，故不拦。

**练习 3**：`ChapterPackageTransformer(MyTransformer(), mode=SubmitKind.APPEND_TEXT)` 包进 `TranslationStep(...)` 时**不指定** `mode`，最终生效的模式是什么？

**答案**：`APPEND_TEXT`。分派的模式协调分支只在 `step.mode != REPLACE` 时才重建转换器；默认 `REPLACE` 视为「未表态」，保留转换器自带的模式（[pdf_craft/craft.py:L243-L248](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L243-L248)）。同时 `_step_mode` 会回读转换器的 `mode` 属性，预检也不会漏掉它。

## 5. 综合实践

把本讲三个模块串成一个「带模式感知的版权标记转换器」：

1. **实现转换器**：`CopyrightTransformer` 实现 `transform(chapter)` 与 `with_mode(mode)` 两个方法——`REPLACE` 模式下给每章最后一个文本块**替换**为统一的版权声明行；`APPEND_TEXT` 模式下在每章每个文本块末尾**追加**简短标记（如 ` · 样例`）。`with_mode` 返回按模式分支的新实例。
2. **先单元验证**：用 4.1.4 的 `build_mini_package` 造迷你包（把单块文本改多块），分别以 `ChapterPackageTransformer(CopyrightTransformer(), mode=SubmitKind.REPLACE)` 和 `mode=SubmitKind.APPEND_TEXT` 转换，检查两种模式下派生包 XML 的差异是否符合设计——这一步验证 `with_mode` 钩子真的被构造函数调用了。
3. **再接入管线**：包装 `TranslationStep(CopyrightTransformer(), mode=SubmitKind.APPEND_TEXT)` 传入 `convert_pdf_to_markdown`（离线可用 4.3.4 版本 B 的假引擎），确认输出 Markdown 每段带标记、原包干净。
4. **写一页笔记**：回答——你的扩展点为什么选章级而不是包级？（提示：版权标记逐章独立，无需跨章状态，选章级可白得复制—改写的全部杂务。）若需求变成「在目录里也加标记」，需要动哪个参数？（提示：`ChapterPackageTransformer` 的 `toc_transformer`。）

## 6. 本讲小结

- pdf-craft 的转换扩展点分两级：章级协议 `ChapterTransformer`（`transform(chapter) -> chapter`，操作内存对象）与包级协议 `PackageTransformer`（`transform(package, output_path) -> DocumentPackage`，对目录全权负责）；两者都是 `Protocol` 结构化类型，实现即满足、无需继承。
- `ChapterPackageTransformer` 用「复制—改写」把章级升级为包级：校验 → 拒绝已存在输出 → 复制契约成员（`ocr/` 缓存不复制）→ 在副本上逐章 decode/transform/encode → 可选 `toc_transformer` → 重建校验返回；原包全程只读，转换器中途抛错也不会污染上游。
- 构造函数会探测 `with_mode` 钩子向章级转换器传播 `SubmitKind` 模式；`ChapterXMLTransformer` 是官方的模式感知范例（编码 → 翻译 → 解码，空章节直接放行），下一讲深入。
- `TranslationStep` 是使用侧信封（transformer + mode），`steps` 还接受裸 `PackageTransformer`；`_apply_steps` 按序链式执行，每步在原包根下平铺产出 `transformed-N` 新包，最终渲染最后一个。
- `_as_package_transformer` 四路分派统一形状，其中用 `inspect.signature` 按「两个名为 `package`/`output_path` 的位置参数」识别用户自定义包级转换器；`translate_pdf` 预检并拒绝 `APPEND_BLOCK`，因为 PDF 回写无法安放没有几何坐标的新增块。
- `_TextChapterTransformer`（旧式文本回调适配器）给出了自定义章级转换器的标准骨架：遍历 `layouts` → 过滤 `ParagraphLayout` → 改写 `blocks[i].content` → 返回章节。

## 7. 下一步学习建议

本讲把「转换器如何接入」讲完了，下一讲 **u7-l2「XMLTranslator 核心：翻译任务的编排」** 将打开 `ChapterXMLTransformer` 里那个 `_translator` 的黑盒：`XMLTranslator` 如何用翻译与回填两个 LLM 运行时完成真正的翻译，jinja 提示词模板长什么样。阅读源码时建议带着本讲的两个钩子去读：`TranslationTask` 的三个字段如何被消费、`SubmitKind` 在提交器里如何落地。若想先巩固本讲，可通读 [tests/test_craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_craft.py)——它几乎为本讲每个行为分支都配了断言；包级转换器与 PDF 回写的几何配合则留到 u10-l1 再展开。
