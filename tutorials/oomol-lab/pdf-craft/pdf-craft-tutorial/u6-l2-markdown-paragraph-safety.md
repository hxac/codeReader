# u6-l2 Markdown 段落模型与 HTML 安全过滤

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 pdf-craft 为什么必须对 OCR 文本做 HTML 白名单过滤，而不是原样透传。
2. 手工追踪 `parse_raw_markdown` 对一段含 HTML 的文本的完整解析过程：主循环如何找标签、四类危险构造如何被整体删除、标签如何按四类策略分别处理。
3. 掌握 GitHub 白名单的三层结构：标签白名单（63 个）、属性白名单（通用属性 + 元素专属属性）、URL 协议白名单。
4. 理解 `HTMLTag[P]` 泛型容器的设计意图——为什么同一个容器既能表示解析结果，又能承载章节 XML 里的公式与脚注引用。

## 2. 前置知识

### 2.1 OCR 文本为什么不可信

在 u3-l4 我们讲过：上游 doc-page-extractor 识别文本块时，`html` 内容优先于纯文本（HTML 片段能保留加粗、斜体、表格等排版信息）。这意味着**扫描页里印着的任何东西都会进入 OCR 文本**——包括恶意构造的 `<script>` 标签或 `<img onerror=...>` 属性。如果一本被投毒的 PDF 被转换成 Markdown/EPUB 后在浏览器或阅读器里渲染，这些片段就可能变成真实执行的 HTML，这就是 XSS（跨站脚本攻击）。

pdf-craft 的输出（Markdown、EPUB）最终会在 HTML 环境中渲染，所以 OCR 文本必须当成**不可信输入**对待。

### 2.2 白名单 vs 黑名单

两种安全过滤思路：

- **黑名单**：列出危险的东西，拦截它们。问题是你永远列不全——新的标签、新的属性、新的协议随时出现。
- **白名单**：列出允许的东西，其余全部拦截或转义。未知的东西默认不安全。

GitHub 渲染 Markdown 时采用白名单策略（基于 Ruby 的 html-pipeline 的 sanitization filter），本模块的白名单就是对它的移植。

### 2.3 CommonMark 与 GFM 的原始 HTML 规则

- **CommonMark** 规定了 Markdown 中「原始 HTML」的语法：开标签 `<tagname attr="value">`、闭标签 `</tagname>`、自闭合 `<tagname />`，以及注释 `<!-- -->`、处理指令 `<?...?>`、声明 `<!DOCTYPE ...>`、CDATA `<![CDATA[...]]>` 四类构造。标签名由 ASCII 字母开头，后跟字母/数字/连字符。
- **GFM**（GitHub Flavored Markdown）在 CommonMark 之上加了 **tagfilter 扩展**：对 `script`、`iframe`、`style` 等九个特殊标签，把开头的 `<` 替换成 `&lt;`——标签结构被破坏，浏览器只会把它当普通文字显示，内容本身保留。

### 2.4 泛型与 TypeVar

`HTMLTag[P]` 里的 `P` 是 Python 泛型类型变量（`TypeVar`）。可以把 `HTMLTag` 理解为一个「容器模板」：`P` 是槽位，表示「这个标签里除了字符串和嵌套标签之外，还可能装什么领域对象」。解析 Markdown 原文时 `P` 不出现；解码章节 XML 时 `P` 是公式和脚注引用。一个容器服务两条数据通路。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pdf_craft/markdown/paragraph/parser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py) | 手写的标签扫描器：`parse_raw_markdown` 主循环、HTML 构造识别、属性解析、闭合标签匹配、属性过滤 |
| [pdf_craft/markdown/paragraph/tags.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py) | GitHub 白名单数据源：tagfilter 集合、忽略集合、63 个标签定义、通用/专属属性、URL 协议，以及四个谓词函数 |
| [pdf_craft/markdown/paragraph/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py) | `HTMLTag[P]` 泛型容器与 `flatten`/`decode`/`encode` 三个工具函数 |
| [pdf_craft/markdown/paragraph/render.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py) | 把 `HTMLTag` 树渲染回 Markdown 文本（下游消费方，帮助理解容器为何这样设计） |
| [pdf_craft/extractor/chapter/jointer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py) | `parse_raw_markdown` 在真实提取链路中的唯一调用点（公式保护后解析块文本） |
| [pdf_craft/extractor/chapter/content.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py) | `Content` 类型别名——`HTMLTag[BlockMember]` 在章节模型中的实例化 |
| [tests/test_parser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py) | 41 个单元测试，覆盖本讲全部行为，是最佳的行为说明书 |

本讲在整条链路中的位置：u5 章节生成时，Jointer 把每个 OCR 文本块交给 `parse_raw_markdown`，得到 `Content` 列表写进 `chapter_N.xml`（u5-l1 讲过章节数据模型）；u6-l1 讲过 `DocumentPackage` 是渲染器与提取器之间的契约。本讲下钻到 `Content` 列表内部：字符串与 `HTMLTag` 混居的规则是什么、`HTMLTag` 从哪里来。

## 4. 核心概念与源码讲解

### 4.1 标签解析器

#### 4.1.1 概念说明

pdf-craft 没有引入任何现成的 Markdown 解析库（如 markdown-it、mistune），而是在 `parser.py` 里手写了一个**字符级扫描器**。原因有二：

1. **只需要处理原始 HTML**：输入是单个 OCR 文本块，不包含 Markdown 语法（标题、列表等已在更上层被结构化），需要识别的只有 CommonMark 定义的原始 HTML 构造。
2. **边扫描边过滤**：解析、安全过滤、构建容器对象必须一次完成——先解析成通用 DOM 再过滤，中间态就可能携带危险内容。

解析器的输出是「字符串与 `HTMLTag` 混合列表」。例如输入 `Text before <b>bold</b> text after`，输出三元素列表：`["Text before ", HTMLTag(b), " text after"]`（[tests/test_parser.py:43-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L43-L52) 断言了这一结果）。

#### 4.1.2 核心流程

主循环是一个经典的扫描器模式：

```text
pos = 0
循环：
  在 input[pos:] 中找下一个 "<"
  找不到 → 把剩余文本追加进结果，结束
  把 "<" 之前的文本追加进结果
  尝试从当前位置解析一个 HTML 构造：
    解析成功 → 把结果（字符串 / HTMLTag / 列表）并入结果列表，pos 前移
    解析失败 → 这个 "<" 不是标签（比如 "a < b"），当作普通字符追加，pos 前移 1
```

单个 HTML 构造的判定顺序：

```text
"<" 之后：
├─ "<!--"   → HTML 注释，找到 "-->"，整段删除
├─ "<?"     → 处理指令，找到 "?>"，整段删除
├─ "<![CDATA[" → CDATA 段，找到 "]]>"，整段删除
├─ "<!" + 字母 → 声明（如 <!DOCTYPE），找到 ">"，整段删除
└─ 其余     → 按「标签」解析（开标签 / 闭标签 / 自闭合）
```

标签解析时，闭标签走简化分支（只认标签名和 `>`），开标签则要完整解析属性列表，然后按 4.2 节的四类策略分流。对非自闭合的开标签，还要用深度计数法找到匹配的闭标签，把中间内容**递归**交给 `parse_raw_markdown`。

#### 4.1.3 源码精读

**入口签名**——输入字符串，输出混合列表，`P` 是给下游 decode 用的载荷槽位（本函数内从不构造 `P`）：

- [parser.py:8-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L8-L22)：`parse_raw_markdown` 的签名与 docstring，四条职责（删除危险构造、按白名单过滤、转义不允许的标签但暴露子内容、应用 GFM tagfilter）写在注释里。

**主循环**：

- [parser.py:27-54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L27-L54)：每次 `find("<")` 定位下一个可能的标签；[L46-49](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L46-L49) 处理三种返回形态——列表则 `extend`（对应「转义标签 + 子内容 + 转义闭标签」多段结果）、非空字符串则 `append`、空字符串直接跳过（注释等被删干净的构造）；[L51-54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L51-L54) 是「不是标签的 `<`」兜底——追加字面量 `"<"` 并前进一格。

**四类危险构造的整体删除**：

- [parser.py:73-105](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L73-L105)：注释、处理指令、CDATA、声明四段结构相同的代码——各自找结束定界符，找到就返回空字符串（等价于删除）。注意 [L100](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L100) 对声明额外要求 `<!` 后必须跟 ASCII 字母，否则（如 `<!>` 或 `<! text`）不算声明、回退到标签解析再失败、最终按字面量处理。为什么删而不是转义？因为这些构造在 Markdown 输出里没有任何语义价值，而注释内容可能藏 payload。

**标签识别与四类策略分流**（核心分支，4.2 节详细展开策略本身）：

- [parser.py:126-136](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L126-L136)：先看是不是 `</` 闭标签；标签名用正则 `([a-zA-Z][a-zA-Z0-9-]*)` 匹配并统一转小写（大小写不敏感，`<DIV>` 等价 `<div>`）。
- [parser.py:139-160](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L139-L160)：**孤儿闭标签**分支（正常配对的闭标签会在开标签处理时被一并消费，走不到这里）。依次判：tagfilter 集合 → 替换 `<` 为 `&lt;`；白名单内 → 原样作为文本返回；否则 → `escape()` 转义。
- [parser.py:163-173](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L163-L173)：开标签先解析属性，再判 tagfilter——注意只把开头的 `<` 换成 `&lt;`，标签其余部分（含属性和 `>`）原样保留，结果是一个纯字符串，如 `<script src="x">` 变成 `&lt;script src="x">`。
- [parser.py:175-192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L175-L192)：**忽略集合**分支——标签本身消失，但子内容被递归解析后原样上浮（剥壳保肉）。
- [parser.py:194-224](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L194-L224)：**白名单**分支——构造 `HTMLTag` 对象：先 `_filter_attributes` 过滤属性，自闭合则 children 为空；否则找闭标签、把中间内容递归解析成 children。找不到闭标签时按自闭合处理（L221-224），保证解析永不卡死。
- [parser.py:225-250](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L225-L250)：**其余一切标签**分支——开、闭标签都 `escape()` 转义成文本，但**中间内容仍递归解析**。这是「转义但暴露子内容」策略：`<custom><b>Bold</b></custom>` 的输出是三段——`"&lt;custom&gt;"`、`HTMLTag(b)`、`"&lt;/custom&gt;"`（见 [tests/test_parser.py:196-205](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L196-L205)）。安全的子标签存活，不安全的壳只是显示出来。

**属性解析器**：

- [parser.py:280-369](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L280-L369)：`_parse_attributes` 按 CommonMark 属性语法逐个解析：属性名正则 `[a-zA-Z_:][a-zA-Z0-9_.:-]*`（L309），支持双引号、单引号、无引号三种取值（L337-361），无值属性（布尔属性如 `open`）记为空字符串（L323-326），[L364](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L364) 对属性值做 HTML 实体反转义（`title="&lt;Test&gt;"` 解析出 `<Test>`，见 [tests/test_parser.py:299-307](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L299-L307)）。解析失败（未闭合引号、非法字符）返回 `None` 位置，上层随即把整个构造当作字面文本。

**闭标签深度匹配**：

- [parser.py:396-463](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L396-L463)：`_find_closing_tag` 处理同名嵌套——`<div><div>Inner</div></div>` 里外层 `div` 必须匹配第二个 `</div>`。算法是大小写不敏感地交替找最近的同名开/闭标签，遇开标签深度加一、遇闭标签深度减一，归零即命中（[tests/test_parser.py:411-425](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L411-L425) 验证嵌套同名标签正确配对）。注意 [L423](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L423) 的完整性校验：`<div` 后必须跟空白或 `>/`，防止 `<divider>` 被误认成 `<div>`。

#### 4.1.4 代码实践

**实践目标**：亲手验证扫描器对「非标签 `<`」和「不完整构造」的容错。

**操作步骤**（示例代码，可在仓库根目录用 `python -i` 交互运行）：

```python
from pdf_craft.markdown.paragraph import parse_raw_markdown

# 1. 数学不等式："<" 后是空格，不构成标签
print(parse_raw_markdown("Price < 100 and > 50"))

# 2. 未闭合的注释：找不到 "-->"，整个构造按字面处理
print(parse_raw_markdown("Hello <!-- never ends"))

# 3. 孤儿闭标签：白名单内 vs 白名单外
print(parse_raw_markdown("Text</div>more"))
print(parse_raw_markdown("Text</custom>more"))
```

**需要观察的现象**：

1. 第一条输出 `["Price ", "<", " 100 and > 50"]`——`<` 被拆成单独的字面量元素（与 [tests/test_parser.py:352-355](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L352-L355) 的断言一致）。
2. 第二条里 `<!-- never ends` 作为普通文本保留在输出中（删除逻辑要求找到结束定界符才生效）。
3. 第三条：`</div>` 原样保留为文本（白名单内），`</custom>` 变成 `&lt;/custom&gt;`（白名单外被转义）。

**预期结果**：所有输入都返回列表，不抛异常，输出中不存在任何「活着」的非白名单标签结构。上述结论依据源码分支与既有测试推断，具体输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_parse_html_construct` 对声明（`<!`）额外要求后跟 ASCII 字母？

**答案**：因为四类构造的判定按前缀从长到短排列（`<!--`、`<?`、`<![CDATA[`、`<!`）。`<!` 是最短前缀，若不约束后续字符，`<>` 或 `<! 5` 这类碎片也会被当声明吞掉。要求后跟字母使其只匹配 `<!DOCTYPE` 这类真声明；其余 `<!` 碎片落到标签解析，再因标签名正则不匹配而失败，最终按字面量 `<` 处理。

**练习 2**：主循环 [L46-49](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L46-L49) 里 `elif parsed:` 的含义是什么？为什么空字符串要跳过？

**答案**：`parsed` 可能是字符串、`HTMLTag` 或列表。`elif parsed:` 为假有两种情况：空字符串（注释等被整体删除的构造）和空列表（忽略标签且无子内容）。空字符串若被 append 会在结果里留下 `""` 元素，污染下游的文本拼接（`join_texts_in_content` 之类），所以直接跳过、只前移位置。

**练习 3**：`_find_closing_tag` 为什么要做「`<div` 后必须跟空白或 `>/`」的校验？

**答案**：字符串查找 `<div` 会命中 `<divider>`、`<divx>` 等更长标签名的开头。不校验的话，`<div><divider></divider></div>` 会把 `<divider>` 的开头误认为嵌套的 `<div`，深度计数出错，导致外层 `div` 匹配到错误的闭标签、内容切分错位。校验下一个字符保证只统计真正的 `div` 标签。

### 4.2 白名单过滤

#### 4.2.1 概念说明

`tags.py` 是整个过滤体系的数据源，它把 GitHub 的净化规则整理成三张集合表 + 一张映射表。所有标签在解析器里被分成**四类处理策略**，这是本讲最核心的一张表：

| 类别 | 数据源 | 开标签处理 | 闭标签处理 | 子内容 |
|---|---|---|---|---|
| ① GFM tagfilter（9 个） | `_FILTERED_TAGS` | `<` 替换为 `&lt;`，其余保留 | 同左 | 保留为纯文本 |
| ② 忽略（3 个） | `_IGNORE_TAGS`（left/center/right） | 标签删除 | 标签删除 | 递归解析后上浮 |
| ③ 白名单（63 个） | `_TAG_DEFINITIONS` | 保留为 `HTMLTag` + 属性过滤 | 随配对开标签消费 | 递归解析为 children |
| ④ 其余一切 | 兜底 | `escape()` 转义 | `escape()` 转义 | 递归解析后上浮 |

四类策略的风险考量各不相同：

- **① tagfilter**：这九个标签（`script`、`iframe`、`style`、`title`、`textarea`、`xmp`、`noembed`、`noframes`、`plaintext`）的共性是**改变浏览器对后续内容的解释方式**（如 `plaintext` 让之后整页变纯文本、`title` 内容进入标题栏）。只破坏 `<` 即可废掉它们，内容无需删除——这正是 GFM 6.11 节的规定动作。
- **② 忽略**：`<left>` `<center>` `<right>` 是 OCR 常见的旧式对齐标签，本身无害但不在 GitHub 白名单里，直接删除会丢掉对齐语义、转义又会在输出里留下可见噪音，所以选择「剥壳」。
- **③ 白名单**：安全且有用的标签，保留结构、过滤属性。
- **④ 兜底**：未知标签默认不可信，但硬删可能丢内容、硬留可能带风险，于是折中为「转义显示 + 子内容照常解析」。

属性层面同样是白名单，且分两层。一个标签最终允许的属性集合为：

\[ A_{\text{allowed}}(t) = U \cup S(t) \]

其中 \( U \) 是通用属性集合（`UNIVERSAL_ATTRIBUTES`，对几乎所有白名单标签生效），\( S(t) \) 是元素专属属性集合（如 `a` 的 `href`、`img` 的 `src`）。注意 `class`、`style`、所有 `on*` 事件属性、所有 `data-*` 属性都**不在**任何一层里——它们全部被静默丢弃。

URL 属性（`href`、`src`、`cite`）还有第三层协议白名单：只允许 `http:`、`https:`、`mailto:` 和相对路径。`javascript:`、`data:`、`vbscript:` 等危险协议会被整条属性丢弃。

#### 4.2.2 核心流程

一个开标签被判定为白名单后的处理流程：

```text
解析属性列表 [(name, value), ...]
    ↓ 逐个属性：
    name ∈ A_allowed(tag) ？
    ├─ 否 → 丢弃
    └─ 是 → name ∈ {href, src, cite} ？
            ├─ 是 → is_protocol_allowed(value) ？保留 : 丢弃
            └─ 否 → 保留
    ↓
HTMLTag(definition=标签定义, attributes=过滤后属性, children=递归解析的子内容)
```

协议判定的细节（`is_protocol_allowed`）：

```text
value 为空 → 允许
value 以 "/" "./" "../" 开头 → 允许（相对路径）
value 小写后以 "http:" / "https:" / "mailto:" 开头 → 允许
其余（javascript:、data:、file:、JAVASCRIPT: 混淆大小写…） → 拒绝
```

#### 4.2.3 源码精读

**tagfilter 集合**：

- [tags.py:69-81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L69-L81)：`_FILTERED_TAGS` 九个标签，每个都带注释说明它为何危险（如 `script` 可执行 JavaScript、`plaintext` 让后续整页按纯文本解释）。这份清单直接对应 GFM 规范 6.11 节。

**忽略集合**：

- [tags.py:92-98](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L92-L98)：`_IGNORE_TAGS` 只有 `left`、`center`、`right` 三个旧式对齐标签。

**标签定义与白名单主表**：

- [tags.py:52-58](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L52-L58)：`HTMLTagDefinition` 数据类——`name`、`attributes`（frozenset，即该标签允许的完整属性集）、`is_block`（块级/行内标记，供下游排版用）。
- [tags.py:459-523](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L459-L523)：`_TAG_DEFINITIONS` 是唯一事实来源（single source of truth），收录 63 个标签定义——结构元素（div/p/blockquote）、标题（h1-h6）、行内格式（b/i/strong/em…）、列表（ol/ul/li/dl）、表格（table/thead/tbody/tr/td/th…）、媒体（img/picture/video）、东亚注音（ruby/rt/rp）等。
- [tags.py:527](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L527)：`ALLOWED_TAGS` 直接从主表派生，避免两份清单不同步。

**属性白名单两层结构**：

- [tags.py:108-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L108-L190)：`UNIVERSAL_ATTRIBUTES` 约 90 个通用属性（id/title/lang/align/width 等 ARIA 与微数据属性）。通读一遍会发现刻意缺席的名字：`class`、`style`、所有 `on*`、所有 `data-*`。
- [tags.py:199-211](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L199-L211)：`ELEMENT_SPECIFIC_ATTRIBUTES` 元素专属属性——`a` 多了 `href`，`img` 多了 `src`，`blockquote`/`q` 多了 `cite` 等。这些是 URL 类属性的来源。
- 各标签定义用并集组装，例如 [tags.py:428-432](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L428-L432) 的 `HTML_A = HTMLTagDefinition(name="a", attributes=UNIVERSAL_ATTRIBUTES | ELEMENT_SPECIFIC_ATTRIBUTES["a"], ...)`。

**协议白名单与判定函数**：

- [tags.py:219-226](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L219-L226)：`ALLOWED_PROTOCOLS = {"http", "https", "mailto"}`，注释说明相对路径同样允许。
- [tags.py:547-561](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L547-L561)：`is_protocol_allowed`——先放行空值与 `//./../` 开头的相对路径，再小写后逐一比对协议前缀。小写比对挡住了 `JaVaScRiPt:` 这类大小写混淆。
- [tags.py:535-544](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L535-L544)：三个谓词 `tag_definition` / `is_tag_filtered` / `is_tag_ignored`，全部先 `lower()` 再查表，与解析器的标签名归一化配合。

**属性过滤的执行点**：

- [parser.py:372-393](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/parser.py#L372-L393)：`_filter_attributes` 先查属性白名单（L384），再对 `href`/`src`/`cite` 三个 URL 属性追加协议检查（L386-389）——协议不合法时**整条属性被丢弃**，而不是清空值。`javascript:` 链接失去 `href` 后退化成普通文本，正是 [tests/test_parser.py:166-174](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L166-L174) 断言的行为。

#### 4.2.4 代码实践

**实践目标**：用白名单的「眼睛」看一遍常见攻击向量，确认每一条都被正确拦截。

**操作步骤**（示例代码）：

```python
from pdf_craft.markdown.paragraph import parse_raw_markdown, HTMLTag

cases = [
    '<img src="x" onerror="alert(1)">',          # 事件属性注入
    '<a href="javascript:alert(1)">点我</a>',     # 危险协议
    '<a href="https://example.com" target="_blank">正常链接</a>',  # 应完整保留
    '<table summary="s"><tr><td>Cell</td></tr></table>',          # 表格应保留
]
for c in cases:
    result = parse_raw_markdown(c)
    for item in result:
        if isinstance(item, HTMLTag):
            print(item.definition.name, dict(item.attributes))
        else:
            print(repr(item))
```

**需要观察的现象**：

1. `img` 标签保留为 `HTMLTag`，但 `attributes` 里只剩 `("src", "x")`——`onerror` 不在 `UNIVERSAL_ATTRIBUTES ∪ {src, longdesc, loading, alt}` 中，被静默丢弃。
2. `a` 标签保留，但 `attributes` 中**没有** `href`——`javascript:` 协议非法，整条属性被丢弃；链接文本「点我」保留。
3. 正常链接的 `href` 与 `target` 都在（`target` 属于通用属性）。
4. `table`/`tr`/`td` 全部保留为嵌套的 `HTMLTag` 对象，`Cell` 是最内层的字符串子节点。

**预期结果**：四条输入中不存在任何存活的事件属性或危险协议；表格结构完整。其中 `onerror` 行为依据与 `onclick` 相同的白名单机制（[tests/test_parser.py:119-128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py#L119-L128) 断言 `onclick` 被过滤），待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `class` 和 `style` 不在通用属性白名单里？保留它们会有什么后果？

**答案**：`style` 可以注入任意 CSS——除了破坏页面布局，还可能配合 `position:absolute` 做点击劫持、用 `background-image:url(...)` 做数据外带。`class` 本身不执行代码，但可能被目标页面的既有 CSS/JS 选择器命中，触发非预期行为。对「内容转换」这一场景，两者都没有保留价值，GitHub 同样拒绝它们。

**练习 2**：`_filter_attributes` 对 URL 属性检查失败时为什么「丢弃整条属性」而不是「保留标签但清空值」？

**答案**：`href=""` 或 `src=""` 仍是合法属性，部分渲染器会对空 URL 发起当前页面请求，行为不可控。丢弃整条属性后，`<a>` 退化为无链接的行内元素，语义最安全。这也与 GitHub html-pipeline 的行为一致。

**练习 3**：`is_protocol_allowed("HTTPS://EXAMPLE.COM")` 返回什么？`is_protocol_allowed("//cdn.example.com/x.js")` 呢？

**答案**：第一个返回 `True`——函数先把 URL 转小写再比对 `https:` 前缀（[tags.py:556-559](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L556-L559)），大写协议是合法输入。第二个也返回 `True`，但走的不是协议分支——`//` 开头被当作相对路径放行（协议相对 URL）。如果想拒绝外部资源加载，需要在上层另做域名限制，本函数不承担该职责。

### 4.3 HTMLTag 容器

#### 4.3.1 概念说明

过滤之后的产物需要一种内存表示。最直观的选择是字符串拼接——但一旦拼回字符串，结构信息（哪些是标签、哪些是文本、标签里嵌套了什么）就丢了，下游（EPUB 渲染、翻译引擎）无法区分「需要翻译的文本」和「不能动的标签」。所以 pdf-craft 设计了 `HTMLTag[P]` 泛型容器：

```python
@dataclass
class HTMLTag(Generic[P]):
    definition: HTMLTagDefinition        # 标签定义（含属性白名单、块级标记）
    attributes: list[tuple[str, str]]    # 已过滤的属性
    children: list["str | P | HTMLTag[P]"]  # 混合子内容
```

`P` 是关键设计。容器里除了字符串和嵌套的 `HTMLTag`，还有第三类东西——**领域对象**：

- **解析路径**（`parse_raw_markdown`）：`P` 从不出现，因为原文里只有文本和标签。公式在解析前就被 Jointer 换成了 PUA 占位符（见 4.3.3），解析后再展开回 `InlineExpression`。
- **XML 解码路径**（`types.decode`）：从 `chapter_N.xml` 读回章节时，`<inline_expr>`、`<ref>` 这类**非白名单元素**正是公式与脚注引用的载体，它们经 `decode_payload` 回调变成 `P`。

于是同一个容器类型覆盖了两条数据通路，类型上体现为章节模型中的实例化（[content.py:6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L6)）：

```python
Content = list[str | BlockMember | HTMLTag[BlockMember]]
```

即 u5-l1 讲过的：块内容是普通文本、`InlineExpression`/`Reference`（合称 `BlockMember`）与白名单 `HTMLTag` 的混居列表。

一个微妙但重要的对照：在「文本解析」世界里，非白名单标签被转义成文本；在「XML 解码」世界里，非白名单**元素**反而是载荷本身（`<ref>`、`<inline_expr>`）。两者的分界由 `tag_definition(child.tag)` 给出——查得到定义就是 HTML 容器，查不到就交给载荷回调。这能成立是因为 chapter XML 是**我们自己写出的、已净化过的**文件，不存在投毒问题；白名单过滤只发生在不可信的原文进入的那一刻。

#### 4.3.2 核心流程

三个工具函数各自的职责：

```text
flatten(children)   把树拍平：跳过 HTMLTag 壳，只产出所有 str 与 P
                    （翻译引擎要的正是"纯内容序列"）

decode(root, decode_payload)   XML Element 树 → 混合列表
                    白名单元素 → HTMLTag（递归）
                    其余元素  → decode_payload(element) 即 P
                    text/tail → 字符串

encode(root, children, encode_payload)   混合列表 → XML Element 树
                    字符串   → 拼进 root.text 或上个元素的 tail
                    HTMLTag  → Element(标签名, 属性) 递归
                    P        → encode_payload(p) 即领域元素
```

encode 对字符串的处理值得注意：第一个子元素之前的文本进 `root.text`，之后的文本进**前一个元素的 `tail`**——这是 XML ElementTree 的标准文本模型（元素间文本只能挂在 tail 上），decode 沿同一模型还原，保证往返一致。

下游消费链全景：

```text
OCR 块文本
  → (Jointer 保护 LaTeX 为占位符)
  → parse_raw_markdown          ← 本讲主角
  → Content 列表
  → encode → chapter_N.xml（DocumentPackage，u6-l1）
  → decode → Content 列表
  ├→ render_markdown_paragraph → Markdown 输出（u6-l3）
  ├→ EPUB 渲染（u6-l4）
  └→ flatten → 翻译引擎 / PDF 回写取纯内容序列
```

#### 4.3.3 源码精读

**容器定义**：

- [types.py:7-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L7-L14)：`P = TypeVar("P")` 与 `HTMLTag` 数据类三字段。`children` 的类型注解是递归的——列表里可以再嵌 `HTMLTag[P]`，对应标签的任意深度嵌套。

**拍平器**：

- [types.py:17-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L17-L22)：`flatten` 用生成器递归展开，遇 `HTMLTag` 就下钻它的 children，否则原样产出。翻译引擎与脚注搜索（u5-l4 的 `search_references_in_chapter`，位于 [chapter.py:68-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L68-L75)）都靠它拿到与排版无关的内容序列。

**XML 解码**：

- [types.py:25-49](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L25-L49)：`decode` 先收 `root.text`（L29-30），再逐子元素分流（L32-44）——`tag_definition(child.tag)` 查得到就构造 `HTMLTag` 并递归，查不到就调 `decode_payload(child)` 得到 `P`；每个元素处理后收 `child.tail`（L46-47）。白名单在 decode 时的角色从「安全过滤」变成了「结构判别」：区分 HTML 容器与领域载荷。

**XML 编码**：

- [types.py:52-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L52-L75)：`encode` 是 decode 的镜像。L62-66 处理字符串挂载（`root.text` vs `last_element.tail`）；L67-71 把 `HTMLTag` 转成 `Element(child.definition.name, dict(child.attributes))` 并递归；L72-75 把其余对象交给 `encode_payload`。注意 u5-l1 讲过的 encode 收集式脚注处理（先 `flatten` 深搜引用、排序去重集中放 `<references>` 区）正是以这里的 `encode_payload` 为挂载点。

**解析路径的真实调用点**：

- [jointer.py:494-499](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L494-L499)：`_parse_block_content` 先调 `_protect_latex_expressions` 把 LaTeX 公式替换成 PUA 占位符（防止 `$x<y$` 里的 `<` 被当标签破坏），再调 `parse_raw_markdown`；随后 [L501-518](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L501-L518) 用 `expand_text_in_content` 把占位符展开回 `InlineExpression` 对象——这就是「P 靠后处理注入」的完整演示。

**下游渲染**：

- [render.py:7-16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py#L7-L16)：`render_markdown_paragraph` 把混合列表渲染回 Markdown 文本，`render_payload` 回调负责字符串与 `P` 的具体输出。
- [render.py:30-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py#L30-L50)：`_render_html_tag` 重写标签：无 children 输出自闭合 `<tag ... />`，有 children 则递归输出 `<tag ...>...</tag>`；属性值经 `_escape_attribute` 转义（L63-74）。既然 `HTMLTag` 只能由白名单构造，重写出来的标签天然安全——这就是「容器即安全凭证」：**过了过滤这一关的对象，下游可以放心地想怎么渲染就怎么渲染**。

#### 4.3.4 代码实践

**实践目标**：验证 `parse → encode → decode` 的往返一致性，直观感受 `P` 槽位的存在。

**操作步骤**（示例代码）：

```python
from xml.etree.ElementTree import Element, tostring
from pdf_craft.markdown.paragraph import parse_raw_markdown, encode, decode, HTMLTag

# 1. 解析一段含白名单标签的文本
content = parse_raw_markdown('前文 <b>加粗</b> 中文 <table><tr><td>单元格</td></tr></table> 尾文')

# 2. 用 encode 写进 XML，载荷回调简单处理字符串之外的东西（本例没有 P）
root = Element("block")
encode(root, content, encode_payload=lambda p: Element("payload"))

# 3. 用 decode 读回来，载荷回调模拟领域对象（用字符串占位）
restored = decode(root, decode_payload=lambda el: f"P({el.tag})")

# 4. 观察 XML 的形状
print(tostring(root, encoding="unicode"))
print(restored)
```

**需要观察的现象**：

1. 打印的 XML 中，`<b>`、`<table>`、`<tr>`、`<td>` 都是原样标签；「前文 」挂在 `root.text`，「 中文 」挂在 `<b>` 的 `tail` 上。
2. `restored` 与 `content` 结构相同：字符串、`HTMLTag(b)`、`HTMLTag(table)` 混居。
3. 如果把某个子元素改成非白名单标签（例如手工 `root.append(Element("inline_expr"))`），decode 时它会走 `decode_payload` 分支变成 `P("inline_expr")` 字符串——这正是章节 XML 里公式与脚注引用的回来路径。

**预期结果**：往返后结构无损，白名单标签保持为 `HTMLTag`，非白名单元素变成载荷。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`parse_raw_markdown` 的返回类型里有 `P`，但函数体内从不构造 `P`。既然如此，为什么签名不直接写 `list[str | HTMLTag]`？

**答案**：为了与 `decode`/`encode`/`flatten` 共享同一套类型词汇。`flatten(children: Iterable[str | P | HTMLTag[P]])` 的输入可能来自解析路径（P 为空）也可能来自 XML 解码路径（P 是 BlockMember）。如果解析函数返回非泛型的 `HTMLTag`，两条路径的产物就是不同类型，下游每个函数都要写两份签名。让 `HTMLTag` 始终带 `P` 槽位，一条类型链就贯通了；`parse_raw_markdown` 的返回值视为 `P` 恒为空的特例即可。

**练习 2**：为什么 Jointer 要在调用 `parse_raw_markdown` **之前**把 LaTeX 公式换成占位符，而不是解析之后再找公式？

**答案**：公式里几乎必然出现 `<`、`>`、`&`（如 `a<b`、`x \& y`）。先解析的话，`<` 会被扫描器按标签规则处理——轻则公式被拆碎转义，重则公式片段与后续文本被误配成标签结构。占位符（PUA 私有区字符加编号）对扫描器是不可见的普通字符，公式内容完全绕开解析；解析完成后 `expand_text_in_content` 再把占位符原地展开为 `InlineExpression`，公式以结构化对象（P）进入内容树。

**练习 3**：在 decode 里，白名单查询 `tag_definition(child.tag)` 起到「结构判别」作用；如果有人在 chapter_N.xml 里手工塞入一个 `<script>` 元素，decode 会怎么处理它？

**答案**：`script` 不在 `_TAG_DEFINITIONS` 里，所以它不会成为 `HTMLTag`，而是被送进 `decode_payload` 回调，由章节层的解码逻辑决定命运（不认识就报错或忽略）。这再次说明 decode 阶段不做安全过滤是合理的：chapter XML 是受信任的内部产物，真正的安全门在 `parse_raw_markdown`——不可信文本只能从那里进来，且进来时 `script` 已被 tagfilter 破坏成文本，不可能再以元素形式出现在 encode 的产物里。

## 5. 综合实践

综合实践把本讲三个模块串起来：写一个迷你的「安全预览器」，模拟「OCR 文本 → 净化 → 渲染」的完整链路。

**任务**：参考 [tests/test_parser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_parser.py) 的断言风格，编写一个脚本 `safe_preview.py`（示例代码，放在仓库外或临时目录运行均可，不要写入仓库），对下面这段模拟 OCR 输入做净化并输出结果：

```python
INPUT = (
    '<p>前言 <!-- hidden comment -->段落</p>'
    '<script>alert("XSS")</script>'
    '<center><b>居中加粗</b></center>'
    '<img src="x" onerror="alert(1)" alt="图">'
    '<a href="javascript:evil()">链接</a>'
    '<table summary="数据"><tr><td>Cell</td></tr></table>'
    '<custom>自定义</custom>'
)
```

**步骤**：

1. 调用 `parse_raw_markdown(INPUT)` 得到混合列表。
2. 遍历列表，按类型分桶统计：纯字符串（含被转义的）、`HTMLTag` 对象。
3. 对每个 `HTMLTag` 打印 `definition.name`、过滤后的 `attributes`。
4. 用 `render_markdown_paragraph`（`render_payload=lambda x: [x]`）把结果渲染回文本并打印。
5. 对照预期逐条核验：

| 输入构造 | 预期归宿 | 依据 |
|---|---|---|
| `<!-- hidden comment -->` | 消失 | 注释整体删除 |
| `<script>` / `</script>` | 变成 `&lt;script>` / `&lt;/script>` 文本，`alert("XSS")` 保留为纯文本 | tagfilter |
| `<center>` / `</center>` | 消失，`<b>` 保留为 `HTMLTag` | 忽略集合剥壳 |
| `onerror="alert(1)"` | 属性消失 | 不在 img 属性白名单 |
| `href="javascript:evil()"` | 整条 `href` 消失 | 协议白名单 |
| `<table>` 及内部 | 嵌套 `HTMLTag` 完整保留 | 白名单 |
| `<custom>` / `</custom>` | 变成 `&lt;custom&gt;` / `&lt;/custom&gt;` 文本，`自定义` 保留 | 兜底转义 |

6. 再把 `INPUT` 换成你在 u3/u5 实践中真实提取出的某个 `chapter_N.xml` 块文本（或任一含 HTML 的 OCR 文本），重复上述步骤，观察真实数据的过滤情况。

**验收标准**：渲染输出的文本中，用肉眼搜索不到任何存活的 `<script`、`onerror`、`javascript:`；`<table>`/`<b>`/`<img>` 结构完好；能对每条结果说出它命中了 4.2 节四类策略中的哪一类。

## 6. 本讲小结

- OCR 文本是不可信输入：扫描页里印着的 `<script>` 会被上游识别成 HTML 片段，必须在进入内容树之前净化，否则会随 Markdown/EPUB 输出在渲染环境中复活成真实 HTML。
- `parse_raw_markdown` 是手写的字符级扫描器：主循环定位 `<`，先删四类危险构造（注释/处理指令/CDATA/声明），再对标签按四类策略分流——tagfilter 破坏 `<`、忽略集合剥壳、白名单构造 `HTMLTag`、其余转义但保留子内容；解析失败的一律退回字面文本，永不抛错。
- 白名单三层结构：63 个标签（`_TAG_DEFINITIONS` 唯一事实来源）→ 属性（通用 `UNIVERSAL_ATTRIBUTES` ∪ 元素专属，`class`/`style`/`on*`/`data-*` 全部缺席）→ URL 协议（`http`/`https`/`mailto` 与相对路径，违者整条属性丢弃）。
- `HTMLTag[P]` 是泛型容器：`definition` + 已过滤 `attributes` + 递归 `children`；`P` 槽位让同一容器同时服务文本解析路径（P 为空）与章节 XML 解码路径（P 是公式/脚注引用），`flatten`/`decode`/`encode` 沿此贯通。
- 「容器即安全凭证」：能被构造成 `HTMLTag` 的内容必然已过白名单，下游（Markdown 渲染、EPUB 渲染、翻译引擎）可以放心消费，无需重复安检。

## 7. 下一步学习建议

- 下一讲 u6-l3 将沿本讲的下游走：`MarkdownRenderer` 如何把整个 `DocumentPackage`（含本讲的 `Content` 列表）渲染成 Markdown 文件，重点看表格转 Markdown 表格与 `assets_path` 对图片链接的影响。
- 想巩固 XML 通路，可回读 [chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py) 中 `_decode_paragraph` 对 `decode_content`（即本讲 `types.decode`）的调用，以及 `<ref>`/`<inline_expr>` 载荷元素的编解码。
- 对安全过滤有兴趣的读者，建议对照阅读 GFM 规范 6.11 节（Disallowed Raw HTML）与 GitHub html-pipeline 的 sanitization filter 源码，体会本模块白名单的移植来源与取舍。
