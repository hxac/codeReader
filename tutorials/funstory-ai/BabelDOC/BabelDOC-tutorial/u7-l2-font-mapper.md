# 字体映射：FontMapper

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 BabelDOC 把「原文 PDF 字体」映射到「目标语言字体」的整体策略：先按 `lang_out` 选一族候选字体，再按字符能不能渲染逐个挑出最合适的那一个。
- 理解字体被分成 `normal / script / fallback / base` 四类桶，以及 `serif / sans-serif / script` 三种字体族与 `--primary-font-family` 的关系。
- 掌握 `FontMapper.has_char` 与 `FontMapper.map` 这两个最常用的对外方法，理解 `map` 内部的「script → normal → fallback → 兜底」多级回退。
- 认识字体的度量信息（`ascent / descent / encoding_length`）从哪里来、如何被加载并最终写回 IL。

本讲承接 u7-l1（Typesetting 排版重排）。在排版阶段我们说过：译文要逐字符贴着原版面重排，于是每个译文字符都需要一个「能渲染它、又能拿到字体度量」的运行时字体——这个字体就是 `FontMapper` 选出来的。本讲专门拆解这个「选字体」的环节。

## 2. 前置知识

### 2.1 为什么需要字体映射

PDF 里的原文是用「原 PDF 自带的字体」画的。比如一篇英文论文用的是 Times 字体，它只包含拉丁字母和少量符号，**根本画不出中文字**。当 BabelDOC 把英文翻成中文后，要「贴着原版面」把中文画回去，就必须为这些中文字符**另选一个能渲染中文的字体**。

这带来一个核心问题：**译文里的每一个字符，该用哪个字体来画？** 答案由三步组成：

1. **选一族候选字体**：根据目标语言（`lang_out`，如 `zh`）准备一批候选字体（例如思源宋体、思源黑体等）。
2. **逐字符判定可用性**：对译文中的每个字符，在这批候选里找到一个「能渲染它」的字体。
3. **尽量贴近原文风格**：在「能渲染」的前提下，尽量让新字体的粗细（bold）、衬线（serif）等属性与原文一致。

`FontMapper` 就是负责这三件事的组件。它的产物是一个个 `pymupdf.Font` 运行时字体对象，既能在排版时用 `char_lengths` 量出字符宽度（见 u7-l1），也能在最终渲染时被 `PDFCreater` 写进输出 PDF。

### 2.2 字体度量的三个关键量

字体里和排版强相关的三个数：

- **ascent（上伸量）**：基线（baseline）之上字符能升到的高度，决定字符顶部位置。
- **descent（下伸量）**：基线之下字符下沉的深度（通常为负数），决定字符底部位置。
- **encoding_length（编码长度）**：字体字符编码的字节数，影响 CMap / 字符串解码。

这三个量在 u4-4 里已经出现过（字体后端会改写 descent）。本讲关注的是：它们如何作为「预先算好的度量」随字体一起加载，最终又被 `add_font` 写回 IL 的 `PdfFont` 对象。

### 2.3 关键术语速查

| 术语 | 含义 |
| --- | --- |
| `lang_out` | 目标语言代码，如 `zh`、`ja`、`ko`、`en` |
| `font_family` | 一种语言对应的一组候选字体（含 4 类桶） |
| `normal / script / fallback / base` | font_family 内的 4 类字体桶 |
| `serif / sans-serif / script` | 三种字体族（衬线 / 无衬线 / 手写） |
| `has_char` | 判断某个字符是否可被候选字体渲染 |
| `map` | 给定原字体 + 一个字符，挑出一个最合适的运行时字体 |

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/document_il/utils/fontmap.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py) | **本讲主角**。`FontMapper` 类的全部逻辑：选字体族、分类、判定、映射、度量加载、写回 PDF。 |
| [babeldoc/assets/embedding_assets_metadata.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py) | 定义 `CN_FONT_FAMILY` 等各语言的字体族清单，`get_font_family(lang_code)` 在此实现。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 含一个**遗留**的 `resfont_map`（语言→字体简称），本讲会澄清它与现行实现的区别。 |
| [babeldoc/format/pdf/document_il/midend/typesetting.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py) | 排版阶段，`FontMapper` 的主要消费方：在这里实例化并调用 `map`。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `primary_font_family` 字段与断言。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | CLI 选项 `--primary-font-family`。 |

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：**目标语言字体选择 → 字体族分类 → 字符可用性判断 → 字体度量加载**。它们恰好对应 `FontMapper` 把一个字符最终落到一个字体上的全过程。

### 4.1 目标语言字体选择：从 lang_out 到 font_family

#### 4.1.1 概念说明

`FontMapper` 一被构造，第一件事就是问：**「目标语言是哪种？给我准备对应的一批字体。」** 这个「一批字体」在代码里叫 `font_family`，它是一个有固定 4 个键的字典：

```python
{
    "script":   [...],   # 手写体（如霞鹜文楷）
    "normal":   [...],   # 正文字体（思源宋体/黑体）
    "fallback": [...],   # 兜底字体（GoNotoKurrent）
    "base":     [...],   # 基准字体（用于度量基准，如思源黑体 CN Regular）
}
```

每个键的值是该类下的 `.ttf` 字体文件名列表。选择哪一族，**只取决于目标语言 `lang_out`**。

#### 4.1.2 核心流程

```
lang_out(如 "zh")
   │
   ▼
assets.get_font_family(lang_out)
   │  └─ embedding_assets_metadata.get_font_family(lang_code)
   │        · 大写化 lang_code
   │        · "KR"→韩 / "JP"或"JA"→日 / "HK"→港 / "TW"→台 / "EN"→英 / "CN"→中
   │        · 其余→英
   ▼
某个 *_FONT_FAMILY 字典（4 个桶）
   │
   ▼
模块加载时 __add_fallback_to_font_family 已把其它语言的字体
追加进来当兜底 → 即使 lang_out=zh，韩/日/英字体也都在列表里
```

一个常被忽略但很重要的点：模块在被 import 时，`__add_fallback_to_font_family()` 就执行了，它把**所有语言的字体都互相追加**进各自的桶里。所以中文（`zh`）的 `normal` 列表里，除了思源 CN，还混入了思源 TW/HK/KR/JP 和 Noto。这意味着：**即便 `lang_out=zh`，一个韩文字符「가」也能在候选里找到能渲染它的字体**——这将在 4.3 的实践中得到验证。

#### 4.1.3 源码精读

`FontMapper.__init__` 开头先取 `font_family`：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:50-58](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L50-L58) —— 按 `lang_out` 取 `font_family`，并把 4 类桶里的字体文件名汇总到 `self.font_file_names`。

`get_font_family` 的真实实现在 `embedding_assets_metadata.py`：

> [babeldoc/assets/embedding_assets_metadata.py:1417-1434](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1417-L1434) —— 用语言子串匹配（`KR/JP/JA/HK/TW/EN/CN`）挑出对应的 `*_FONT_FAMILY`，匹配不到则退回英文族。注意它只看「是否包含某子串」，所以 `zh-CN` 和 `zh-Hans` 都命中 `CN` 分支。

以中文族为例，看一眼 4 个桶里到底装了什么：

> [babeldoc/assets/embedding_assets_metadata.py:1272-1290](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1272-L1290) —— `CN_FONT_FAMILY`：`script` 是霞鹜文楷，`normal` 是思源宋体/黑体 CN（Bold 在前、Regular 在后），`fallback` 是 GoNotoKurrent，`base` 是思源黑体 CN Regular。

互相追加兜底字体的逻辑：

> [babeldoc/assets/embedding_assets_metadata.py:1385-1397](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1385-L1397) —— 遍历所有语言族，把别族独有的字体追加进当前族的各桶，使任何语言族最终都「装着全套字体」。

> ⚠️ **关于 `resfont_map`（重要澄清，请务必读懂）**
>
> `high_level.py` 里定义了一个看似在做「语言→字体」映射的字典 `resfont_map`：
>
> [babeldoc/format/pdf/high_level.py:77-85](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L77-L85) —— 把语言代码映射到字体简称：`zh-cn/zh-hans/zh → "china-ss"`、`zh-tw/zh-hant → "china-ts"`、`ja → "japan-s"`、`ko → "korea-s"`。
>
> 但**在全代码库范围内搜索，`resfont_map` 只在这一个地方出现，没有任何地方读取或导入它**——它是**遗留（legacy）死代码**。当前真正生效的语言→字体映射，是上面讲的 `get_font_family` + `*_FONT_FAMILY` 那一套。那些简称（`china-ss` / `china-ts` / `japan-s` / `korea-s`）**不是**现行的 `font_id`（现行的是 `SourceHanSansCN-Regular.ttf` 这类文件名）。本讲会带你用现行代码做实践，而不是基于这段死代码。这是一个「读源码要分辨死代码」的真实例子。

#### 4.1.4 代码实践

**实践目标**：验证 `get_font_family("zh")` 返回的 4 个桶，并亲眼看到「跨语言兜底」。

**操作步骤**（无需 API key，纯本地）：

```python
# 示例代码
from babeldoc.assets.embedding_assets_metadata import get_font_family

family = get_font_family("zh")
for bucket in ("script", "normal", "fallback", "base"):
    print(bucket, "->", family[bucket])
```

**需要观察的现象**：`normal` 列表里不仅有 `SourceHanSerifCN-*.ttf` / `SourceHanSansCN-*.ttf`，还混入了 `SourceHanSerifKR-*.ttf`、`NotoSerif-*.ttf` 等其它语言的字体——这就是 `__add_fallback_to_font_family` 的效果。

**预期结果**：每个桶都比「该语言独有字体」更长。**待本地验证**：若你在干净环境运行，import 时上述兜底逻辑会自动执行，无需联网。

#### 4.1.5 小练习与答案

**练习 1**：`get_font_family("zh-Hans")` 与 `get_font_family("zh-CN")` 返回的字体族相同吗？为什么？

> **答案**：**不同**。`get_font_family` 把代码大写后只判断是否含子串 `KR/JP/JA/HK/TW/EN/CN`。`"zh-CN"` 大写为 `ZH-CN`，含 `CN`，命中中文族 `CN_FONT_FAMILY`；而 `"zh-Hans"` 大写为 `ZH-HANS`，不含上述任何子串，落入 `else` 退回 `EN_FONT_FAMILY`。这说明 `get_font_family` 的语言判定较「粗」，实际使用中 `lang_out` 多为 `zh` / `zh-CN` 这类能命中 `CN` 的写法。这是一个值得在本地实测确认的点（见 4.1.4 的运行方式）。

**练习 2**：`resfont_map` 里 `"china-ss"` 对应的现行字体大致是哪几个？

> **答案**：`china-ss` 对应简体中文，现行实现里即 `CN_FONT_FAMILY` 的字体（思源宋体/黑体 CN）。注意这是「等价理解」，不是「同名映射」——`resfont_map` 已不再被使用。

### 4.2 字体族分类：normal / script / fallback / base 与 serif/sans-serif

#### 4.2.1 概念说明

拿到 `font_family` 后，`FontMapper` 把 4 个桶分别装进 4 组「字体对象列表」，并建立一个总的 `type2font` 字典。这个分类决定了后续 `map()` 的**查找顺序**：

- **normal**：正文字体，最常用。中文是思源宋体（serif）/思源黑体（sans-serif）的 Bold 与 Regular。
- **script**：手写/楷书体。中文是霞鹜文楷。注意它对 `italic`（斜体）敏感。
- **fallback**：兜底字体（GoNotoKurrent），覆盖面广，用于前面都找不到时。
- **base**：基准字体，单一一个，用于需要统一度量基准的地方（如排版时量空格宽度）。

另外还有一组**字体族（font family）属性**概念，与上面 4 个桶正交：

- **serif（衬线）**：如宋体、Times，笔画末端有装饰。
- **sans-serif（无衬线）**：如黑体、Arial，笔画末端平直。
- **script（手写/斜体）**：手写风格。

CLI 的 `--primary-font-family` 让用户**强制**选定其中一种，覆盖原文自动检测出来的属性。

#### 4.2.2 核心流程

```
font_family (4 桶)
   │
   ▼
为每个字体文件:
   · pymupdf.Font(fontfile=...)          建运行时字体
   · has_glyph / char_lengths 套 lru_cache
   · 挂载 ascent_fontmap/descent_fontmap/encoding_length
   │
   ▼
fontid2font: {font_id(=文件名): pymupdf.Font}
normal_fonts / script_fonts / fallback_fonts / base_font : 各桶的 Font 列表
   │
   ▼
type2font = {"normal":..., "script":..., "fallback":..., "base":[base_font]}
   └─ 给 map_in_type 按 type 名查桶
```

`--primary-font-family` 的作用发生在 `map()` 入口：它会**改写**从原文字体读到的 `serif`/`italic` 属性，从而影响后续在桶内的筛选。

#### 4.2.3 源码精读

构造桶与 `type2font`：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:83-112](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L83-L112) —— 用 `font_family` 的 4 个键分别填 `normal_fonts/script_fonts/fallback_fonts` 与单一 `base_font`，并合成 `type2font`。注意 `fontid2font["base"]` 与 `fontid2fontpath["base"]` 都被「别名为 base 桶第一个字体」，这样后续无论用 `"base"` 还是用真实文件名都能查到同一个对象。

字体族枚举与 `--primary-font-family` 的合法取值：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:17-32](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L17-L32) —— `PrimaryFontFamily` 枚举与 `from_str`：`serif/sans-serif/script` 分别映射，其余（含 `None`）归为 `NONE`（即不强制、自动选择）。

`--primary-font-family` 在 `map()` 里如何改写原文属性：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:174-180](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L174-L180) —— `SERIF` 强制 `serif=True`；`SANS_SERIF` 强制 `serif=False`；`SCRIPT` 同时强制 `serif=False` 且 `italic=True`（让筛选倾向手写/斜体桶）。

CLI 选项本身的定义：

> [babeldoc/main.py:307-313](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L307-L313) —— `--primary-font-family`，`choices=["serif","sans-serif","script"]`，默认 `None`（自动选择）。

#### 4.2.4 代码实践

**实践目标**：观察 4 个桶的内容与 `type2font` 的结构，并验证 `--primary-font-family` 改写原文属性的效果。

**操作步骤**：

```python
# 示例代码（需先 babeldoc --warmup 下载字体到 ~/.cache/babeldoc）
from types import SimpleNamespace
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper

cfg = SimpleNamespace(primary_font_family=None, lang_out="zh")
fm = FontMapper(cfg)

print("type2font 桶名:", list(fm.type2font.keys()))
print("base_font id:", fm.base_font.font_id)
print("normal_fonts 数量:", len(fm.normal_fonts))
print("script_fonts:", [f.font_id for f in fm.script_fonts])
```

**需要观察的现象**：`type2font` 恰好 4 个键；`base_font.font_id` 是 `SourceHanSansCN-Regular.ttf`；`normal_fonts` 因兜底机制数量较多。

**预期结果**：以上字段均能正常打印。**待本地验证**：若字体未下载，`FontMapper(cfg)` 会在构造时联网下载，建议先 `babeldoc --warmup`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `base` 桶通常只放一个字体，而 `normal` 放多个？

> **答案**：`base` 只用于「需要统一度量基准」的场景（如 u7-l1 里用 `base_font.char_lengths("你", ...)` 估算空格宽度），单一字体足以；`normal` 是正文主力，需要覆盖 serif/sans-serif × Bold/Regular 多种组合，故放多个。

**练习 2**：若用户指定 `--primary-font-family sans-serif`，一篇宋体（serif）正文会被映射成什么？

> **答案**：`map()` 入口会把 `serif` 改写为 `False`，于是在 `normal` 桶里筛选时优先挑「无衬线」字体（思源黑体），从而让译文整体呈无衬线风格。

### 4.3 字符可用性判断：has_char 与 map 的多级回退

#### 4.3.1 概念说明

这是 `FontMapper` 最常用的两个对外方法，也是 u7-l1 排版与前端解析里高频调用的入口：

- **`has_char(char_unicode)`**：「这个字符，候选字体里有没有人能画？」返回布尔。前端 `il_creater`、`formular_helper` 用它判断「这个字符是不是公式里的特殊符号」（如果候选字体都画不出，多半是公式专用符号）。
- **`map(original_font, char_unicode)`**：「给定原 PDF 字体和一个字符，给我**最合适**的那个运行时字体。」返回一个 `pymupdf.Font`，或 `None`（实在找不到）。排版阶段对译文每个字符都调用它。

#### 4.3.2 核心流程

`map()` 的多级回退（关键！）：

```
读原文字体的 bold/italic/monospaced/serif
   │  (若 --primary-font-family 指定，则覆盖 serif/italic)
   ▼
1) map_in_type(..., "script")         ← 斜体优先找手写体
   │  命中 → 返回
   ▼
2) 若 italic：在 script_fonts 里逐个 has_glyph，命中 → 返回
   ▼
3) map_in_type(..., "normal")         ← 正文桶按 bold/serif 筛
   │  命中 → 返回
   ▼
4) map_in_type(..., "fallback")       ← 兜底桶再筛
   │  命中 → 返回
   ▼
5) 在 fallback_fonts 里逐个 has_glyph  ← 实在没有就只看「能不能画」
   │  命中 → 返回
   ▼
6) 全部失败 → 打 warning，返回 None
```

`map_in_type` 在「指定桶」内筛选，逐字体检查：

1. 该字体**能否渲染**此字符（`has_glyph`）。
2. **粗细**是否一致（`bold == font.is_bold`）。
3. **衬线**是否一致（特殊 workaround：思源黑体的 `serif` 属性为真，所以额外用「font_id 是否含 `serif`」来校验）。

任一不满足就跳到桶里下一个字体。整个桶都没有则返回 `None`，交由外层 `map` 进入下一级。

#### 4.3.3 源码精读

`has_char`：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:119-126](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L119-L126) —— 只接受单字符；遍历**所有**字体（不分桶），任一能渲染即返回 `True`。注意它扫的是全部候选，范围最广。

`map_in_type`：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:128-152](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L128-L152) —— 在指定桶内按 `has_glyph → bold 匹配 → serif 匹配（含思源黑体 workaround）` 三关筛选，返回第一个满足的字体，否则 `None`。

`map` 主体（多级回退）：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:154-213](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L154-L213) —— 先读原文属性、应用 `primary_font_family` 覆盖，再按 `script → normal → fallback → 兜底 has_glyph` 逐级回退，全失败则 warning + 返回 `None`。

`map` 在排版阶段被逐字符调用（u7-l1 的核心消费点）：

> [babeldoc/format/pdf/document_il/midend/typesetting.py:1517-1536](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1517-L1536) —— 对 `pdf_same_style_unicode_characters` 中的每个译文字符，用 `self.font_mapper.map(font, char_unicode)` 选出运行时字体，包进 `TypesettingUnit`。注意末尾会过滤掉「`unicode is not None` 但 `font is None`」的单元（即 `map` 返回 None 的字符被丢弃，不会画错字）。

`has_char` 在前端/公式辅助里的用法：

> [babeldoc/format/pdf/document_il/utils/formular_helper.py:25-26](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L25-L26) —— `if not font_mapper.has_char(char)` 即认定该字符「正常字体画不出」，倾向于判为公式字符（呼应 u5-4）。

#### 4.3.4 代码实践

**实践目标**：亲手验证「在 `lang_out=zh` 下，中/繁/日/韩字符是否都可渲染」，并观察 `map` 会把同一个字符映射到哪个字体。

**操作步骤**（**注意：现行 `font_id` 是 `.ttf` 文件名，不是 `china-ss` 那类简称**）：

```python
# 示例代码（需先 babeldoc --warmup）
from types import SimpleNamespace
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper
from babeldoc.format.pdf.document_il import il_version_1

cfg = SimpleNamespace(primary_font_family=None, lang_out="zh")
fm = FontMapper(cfg)

# 1) has_char：跨语言兜底验证
for c in ["你", "漢", "あ", "가"]:
    print(f"has_char({c!r}) = {fm.has_char(c)}")

# 2) 逐字体 has_glyph：看「哪个字体能画」
def who_can_render(fm, c):
    code = ord(c)
    return [fid for fid, f in fm.fontid2font.items() if f.has_glyph(code)]

for c in ["你", "가"]:
    hits = who_can_render(fm, c)
    print(f"{c!r} 可被 {len(hits)} 个字体渲染，例如:", hits[:3])

# 3) map：需要一个「原文 PdfFont」做入参，这里造一个最小 PdfFont
orig = il_version_1.PdfFont(
    name="Times", font_id="Times", serif=True, bold=False,
    italic=False, monospace=False, ascent=800, descent=-200,
    encoding_length=1,
)
for c in ["你", "A"]:
    mapped = fm.map(orig, c)
    print(f"map({c!r}) ->", mapped.font_id if mapped else None)
```

**需要观察的现象**：
- `has_char("가")` 在 `lang_out=zh` 下**仍为 `True`**——证明韩文字体被兜底机制纳入了候选。
- `map("你", ...)` 会落到一个思源 CN 字体；`map("A", ...)` 可能落到含拉丁字母的字体。

**预期结果**：CJK 字符大多可渲染并被映射到思源系列字体。**待本地验证**：具体落到 Bold 还是 Regular 取决于 `orig` 的 `bold`/`serif` 与桶内顺序。

#### 4.3.5 小练习与答案

**练习 1**：`map` 为什么要把 `script`（手写体）桶放在最前面查？

> **答案**：因为 `map_in_type("script")` 在「非斜体」时直接返回 `None`（见 [fontmap.py:137-138](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L137-L138)），只有斜体场景才会真正用手写体。把它放前面是为了「斜体优先用手写字体」，符合排版直觉，且对非斜体几乎零成本（直接 `None` 跳过）。

**练习 2**：若 `map` 对某字符返回 `None`，排版阶段会怎样？

> **答案**：见 typesetting.py 末尾的过滤（4.3.3 引用），「`unicode is not None` 但 `font is None`」的单元会被丢弃，该字符不会出现在输出里，避免用错字体画错字。

### 4.4 字体度量加载：ascent / descent / encoding 与缓存

#### 4.4.1 概念说明

每个候选字体除了「能画字」，还要带上「度量信息」才能被排版和渲染正确使用。`FontMapper` 在构造时为每个字体做了三件事：

1. **建 `pymupdf.Font` 运行时对象**：用 `pymupdf.Font(fontfile=...)` 打开 `.ttf`。
2. **加载预存的度量**：从资源元数据里读 `ascent / descent / encoding_length`，挂到字体对象上（`ascent_fontmap / descent_fontmap / encoding_length`）。注意，这些是**随包预计算好**的，不是运行时现算的，来自 `assets.get_font_and_metadata`。
3. **套缓存**：给昂贵的 `has_glyph`、`char_lengths` 方法包 `lru_cache`，避免对同一字符反复量宽。这呼应 u7-l1 讲过的「字体度量三级缓存」中的组件级缓存。

度量信息的最终归宿有二：排版阶段通过 `char_lengths` 实时量宽；渲染阶段由 `add_font` 把 `ascent/descent/encoding_length` 写回 IL 的 `PdfFont` 对象，供 `PDFCreater` 子集化与生成时使用。

#### 4.4.2 核心流程

```
对 font_file_names 中每个字体:
   · assets.get_font_and_metadata(name) → (font_path, font_metadata)
   · pymupdf.Font(fontfile=font_path)
   · has_glyph  ← lru_cache(maxsize=10240, typed=True)
   · char_lengths ← lru_cache(maxsize=10240, typed=True)
   · .ascent_fontmap  = metadata["ascent"]
   · .descent_fontmap = metadata["descent"]
   · .encoding_length = metadata["encoding_length"]
   存入 fonts / fontid2fontpath / fontid2font

has_char / map_in_type 自身也套 lru_cache（4.2.3 引用的 L114-117）
```

缓存命中函数：对同一 `(字符, 字号)` 反复调用 `char_lengths` 不再重复计算，这是排版逐字符量宽的性能命脉。

#### 4.4.3 源码精读

加载字体与挂载度量：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:60-81](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L60-L81) —— 对每个字体文件：建 `pymupdf.Font`、给 `has_glyph`/`char_lengths` 套 `lru_cache`，并把 `ascent/descent/encoding_length` 从元数据挂到字体对象上。

`has_char`/`map_in_type` 的方法级缓存：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:114-117](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L114-L117) —— 把 `has_char` 与 `map_in_type` 自身也包成 `lru_cache(maxsize=10240, typed=True)`，使「同一原字体 + 同一字符」的映射结果被记住，排版时大量重复字符不再重算。

度量在排版时的实时量宽用法（u7-l1 已讲，这里看入口）：

> [babeldoc/format/pdf/document_il/midend/typesetting.py:1329-1331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1329-L1331) —— 用 `base_font.char_lengths("你", font_size*scale)[0] * 0.5` 估算空格宽度，正是依赖 `char_lengths` 的缓存与度量。

度量写回 IL（`add_font` 内构造 `PdfFont`）：

> [babeldoc/format/pdf/document_il/utils/fontmap.py:285-307](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L285-L307) —— 把每个被使用的字体的 `ascent_fontmap/descent_fontmap/encoding_length` 与 `is_bold/is_italic/is_monospaced/is_serif` 一并写入新建的 `il_version_1.PdfFont`，供 `PDFCreater` 写入输出 PDF。

`add_font` 的调用方（u7-3 会详讲 PDFCreater）：

> [babeldoc/format/pdf/document_il/backend/pdf_creater.py:1134](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/backend/pdf_creater.py#L1134) —— `self.font_mapper.add_font(pdf, self.docs)`：在生成阶段把候选字体真正插入输出 PDF 的 xref，并写入 `PdfFont` 度量。

#### 4.4.4 代码实践

**实践目标**：验证度量信息确实被加载并挂在字体对象上。

**操作步骤**：

```python
# 示例代码（需先 babeldoc --warmup）
from types import SimpleNamespace
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper

cfg = SimpleNamespace(primary_font_family=None, lang_out="zh")
fm = FontMapper(cfg)

base = fm.base_font
print("base font_id      :", base.font_id)
print("ascent_fontmap    :", base.ascent_fontmap)
print("descent_fontmap   :", base.descent_fontmap)
print("encoding_length   :", base.encoding_length)

# 缓存验证：连续两次 char_lengths，第二次应命中 lru_cache
import pymupdf  # 仅用于观察，非必需
w1 = base.char_lengths("你", 12.0)
w2 = base.char_lengths("你", 12.0)
print("char_lengths 一致:", w1 == w2, w1)
print("has_glyph 缓存信息:", base.has_glyph.cache_info())
```

**需要观察的现象**：`ascent` 为正、`descent` 为负；`has_glyph.cache_info()` 能看到命中次数（连续两次同字符第二次应命中）。

**预期结果**：度量数值非空且符合「上正下负」约定。**待本地验证**：具体数值取决于思源字体的实际度量。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `has_glyph` / `char_lengths` 要用 `lru_cache` 包，而且 `maxsize=10240`？

> **答案**：排版逐字符量宽，同一字符（尤其标点、常用汉字）会被成千上万次反复查询；不缓存会导致 `char_lengths` 成为性能瓶颈。`10240` 容量足以覆盖一页的高频字符集，`typed=True` 还按参数类型区分，避免 `1`（int）与 `"1"`（str）撞键。

**练习 2**：`ascent/descent` 是运行时算出来的吗？

> **答案**：不是。它们是随包预计算、存在资源元数据里的（`assets.get_font_and_metadata` 返回的 `font_metadata`），`FontMapper` 只是「读出来挂上去」，保证与字体文件一致且加载快。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「字体可用性体检」小工具。

**任务**：写一段脚本，针对 `lang_out="zh"`，对一组测试字符 `["你", "漢", "あ", "가", "α", "∑", "😀"]` 输出一张表，包含三列：

1. `has_char` 是否可渲染；
2. 能渲染它的字体数量（遍历 `fontid2font` 调 `has_glyph`）；
3. 用一个 `serif=True, bold=False` 的原文 `PdfFont` 调 `map` 后，落到哪个 `font_id`。

**参考实现骨架**：

```python
# 示例代码（需先 babeldoc --warmup）
from types import SimpleNamespace
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper
from babeldoc.format.pdf.document_il import il_version_1

cfg = SimpleNamespace(primary_font_family=None, lang_out="zh")
fm = FontMapper(cfg)
orig = il_version_1.PdfFont(
    name="orig", font_id="orig", serif=True, bold=False, italic=False,
    monospace=False, ascent=800, descent=-200, encoding_length=1,
)

chars = ["你", "漢", "あ", "가", "α", "∑", "😀"]
print(f"{'char':<4}{'has_char':<10}{'#fonts':<8}{'mapped font_id'}")
for c in chars:
    can = fm.has_char(c)
    n = sum(1 for f in fm.fontid2font.values() if f.has_glyph(ord(c)))
    m = fm.map(orig, c)
    print(f"{c:<4}{str(can):<10}{n:<8}{m.font_id if m else 'None'}")
```

**观察与思考**：

- CJK 字符应大多 `has_char=True` 且 `#fonts` 较大（跨语言兜底）。
- emoji「😀」很可能 `has_char=False`、`map` 返回 `None`——思源/Noto 这批字体不含彩色 emoji。
- 比较练习：把 `lang_out` 改成 `"ja"` 再跑一次，观察 `#fonts` 列是否变化（应几乎不变，因为兜底机制让各语言族最终装着同样的全套字体）。

**待本地验证**：以上现象需在有字体缓存的环境运行确认。

## 6. 本讲小结

- `FontMapper` 的核心职责是：**根据 `lang_out` 选一族候选字体，再为每个译文字符挑出一个能渲染、风格尽量贴近原文的运行时字体**。
- 字体选择的第一步是 `assets.get_font_family(lang_out)`，返回含 `script/normal/fallback/base` 四桶的字体族；模块加载时的兜底机制让任何语言族都「装着全套字体」，因此跨语言字符也常可渲染。
- 字体被组织进 `type2font` 四桶，与 `serif/sans-serif/script` 三种字体族属性正交；`--primary-font-family` 可在 `map()` 入口强制覆盖原文的 serif/italic。
- `has_char` 判「任意字体能否渲染」（前端/公式判定用），`map` 做「script→normal→fallback→兜底」多级回退（排版逐字符用）；两者都被 `lru_cache` 加速。
- 字体的 `ascent/descent/encoding_length` 是**预存元数据**而非运行时计算，加载后既供 `char_lengths` 实时量宽，也由 `add_font` 写回 IL 的 `PdfFont` 供 `PDFCreater` 使用。
- `high_level.py` 里的 `resfont_map` 是**遗留死代码**，现行映射以 `get_font_family` 为准——读源码时要能分辨这类「仍在文件里但不再生效」的旧逻辑。

## 7. 下一步学习建议

- **u7-l3 PDF 生成后端：PDFCreater**：本讲的 `add_font` 是在 `PDFCreater.write` 里被调用的，下一讲会讲清字体如何被真正嵌入输出 PDF、如何做字体子集化（subset），以及 mono/dual PDF 的生成差异。
- **回看 u7-l1 排版重排**：结合本讲理解「为什么排版需要 `FontMapper`」——`TypesettingUnit` 的 `font` 字段正是 `font_mapper.map` 的返回值，`char_lengths` 正是被缓存加速的度量方法。
- **延伸阅读 u4-4 字体后端**：对比「解析时」从 PDF 字体字典构造运行时字体（`ActiveFontFactory`）与「渲染时」从候选字体族挑选字体（本讲 `FontMapper`）——两者都是「选/造一个 pymupdf.Font」，但方向相反：前者为了读懂原文，后者为了画出译文。
- **建议动手**：跑一遍综合实践的脚本，把不同 `lang_out` 的可用性表对比一下，体感会比纯读源码更扎实。
