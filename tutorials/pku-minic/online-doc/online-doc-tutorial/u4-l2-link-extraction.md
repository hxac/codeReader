# u4-l2 链接提取子系统

## 1. 本讲目标

本讲聚焦 `scripts/check_links.py` 中「把文档里的链接一条条找出来」这一步——也就是**链接提取（link extraction）子系统**。

学完本讲，你应当能够：

- 说清楚检查器是如何用 `markdown-it-py` 把一份 Markdown 切成 token 流，并从中抽出链接和图片的；
- 看懂用 `selectolax` 解析 HTML、按「标签 + 属性」表抽取 `href` / `src` / `srcset` 等链接的逻辑；
- 理解为什么还要额外做一层「裸 URL 兜底」，以及它的正则与尾部修剪规则；
- 掌握每条链接的**行列号定位**与**去重**机制，明白错误信息里 `文件:行:列` 是怎么算出来的。

本讲承接 u4-l1（你已经知道检查器的整体流程、`Link`/`Issue` 等 dataclass、配置与入口），是后续 u4-l3（Docsify 路由解析与本地校验）、u4-l4（远程校验）的前置——**没有「提取」就没有「校验」**。

## 2. 前置知识

- **Markdown 与 HTML 的区别**：Markdown 是给人写的轻量标记（如 `[文字](地址)`），HTML 是浏览器解析的标签语言（如 `<a href="地址">文字</a>`）。本仓库的文档以 Markdown 为主，但允许在 Markdown 里直接嵌 HTML。
- **token（词法单元）**：把一段文本按语法规则切成一个个有类型的片段，每个片段就是一个 token。例如一段 Markdown 会被切成「标题开始 / 行内内容 / 段落开始 / …」这样的 token 序列。
- **CSS 选择器**：`a[href]` 表示「带 `href` 属性的 `<a>` 标签」。本讲会用到这类选择器来批量定位 HTML 节点。
- **正则表达式（regex）**：用模式描述一类字符串。例如 `[^\s]` 表示「任意一个非空白字符」。
- 回顾 u4-l1：`Link` 是一个 `frozen` dataclass，字段为 `source`（来源文件）、`line`、`column`、`url`，构造时会自动做 `html.unescape(url.strip())` 归一化。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 仓库唯一的程序代码；本讲的提取子系统（约 58–393 行）就位于其中 |

为方便对照，本讲涉及的关键代码点（均为上述文件）：

- 模块级常量：`BARE_URL_RE`（裸 URL 正则）、`MARKDOWN`（markdown-it 解析器）、`HTML_LINK_ATTRS`（标签→属性表）、`HTML_LINK_SELECTOR`（由表生成的选择器）。
- Markdown 提取：`extract_markdown_links`、`line_for`、`column_for`。
- HTML 提取：`html_links`、`parse_srcset`、`line_column_at`、`find_html_node`、`find_html_value`、`extract_html_links`。
- 裸 URL 兜底：`bare_html_links`、`trim_bare_url`。
- 去重与分发：`dedupe_links`、`extract_links`。

## 4. 核心概念与源码讲解

### 4.1 Markdown 链接抽取

#### 4.1.1 概念说明

Markdown 文档里的链接形态多样：标准链接 `[文字](地址)`、图片 `![替代文字](地址)`、以及 markdown-it 自动识别的「裸 URL」。如果只用正则去匹配 `[...](...)`，很容易漏掉图片、漏掉自动链接、还会被代码块里的假链接干扰。

更稳的做法是**借用一个成熟的 Markdown 解析器**，让它把文本按 CommonMark 规范切成 token 流，我们再从 token 里挑出「链接类」token。本仓库用的是 `markdown-it-py`（`requirements.txt` 里通过 `markdown-it-py[linkify]` 安装，`[linkify]` 额外带上了自动识别 URL 的能力）。

#### 4.1.2 核心流程

`extract_markdown_links` 的处理流程可以概括为：

1. 读取文件原文，并按行切分（`splitlines()`），留作「列号定位」时查找用。
2. 用 `MARKDOWN.parse(text)` 得到 token 流。
3. 遍历 token 流，分三类处理：
   - **整块原始 HTML**（`html_block`，例如单独成行的 `<div>...</div>`）：交给 HTML 提取器 `html_links` 处理（见 4.2）。
   - **行内内容**（`inline`，即段落、列表项里的一行文字）：进一步遍历它的子 token `children`。
   - 其它块级 token（标题、段落、代码块的开闭标记等）：忽略。
4. 对 `inline` 的每个子 token 再分三类：
   - `html_inline`（行内嵌的原始 HTML，如句子里的 `<a href>`）：交给 `html_links`。
   - `link_open`（`[文字](地址)` 的「开头」，也含 linkify 自动生成的链接）：取它的 `href` 属性。
   - `image`（`![替代文字](地址)`，是一个自包含 token）：取它的 `src` 属性。
5. 最后对结果做去重。

> 直觉：markdown-it 把「链接」拆成了「开标签 + 文字 + 闭标签」。我们只关心开标签 `link_open` 上的 `href`；图片 `image` 是自闭合的，属性直接挂在它自己身上。

#### 4.1.3 源码精读

解析器对象在模块顶层只创建一次（[scripts/check_links.py:59-60](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L59-L60)）：

```python
MARKDOWN = MarkdownIt(
    'commonmark', {'html': True, 'linkify': True}).enable('linkify')
```

- `'commonmark'`：按 CommonMark 规范解析。
- `'html': True`：允许 Markdown 里直接写原始 HTML（这样 `html_block` / `html_inline` token 才会出现，本仓库文档大量使用了这一点）。
- `'linkify': True` + `.enable('linkify')`：开启「自动链接」，把正文里形如 `https://example.com/x` 的裸 URL 自动变成 `link_open` token——这正是 Markdown 文档里裸 URL **不需要**单独兜底（见 4.3）的原因。

提取主函数（[scripts/check_links.py:353-378](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L353-L378)）：

```python
def extract_markdown_links(root: Path, source: Path) -> list[Link]:
  text = (root / source).read_text(encoding='utf-8')
  lines = text.splitlines()
  links: list[Link] = []
  for token in MARKDOWN.parse(text):
    line = line_for(token)
    if token.type == 'html_block':
      links.extend(html_links(source, token.content, line))
      continue
    if token.type != 'inline':
      continue
    for child in token.children or []:
      child_line = line_for(child, line)
      if child.type == 'html_inline':
        links.extend(html_links(source, child.content, child_line))
      elif child.type == 'link_open':
        url = child.attrGet('href')
        if url:
          links.append(Link(source, child_line,
                            column_for(lines, child_line, url), url))
      elif child.type == 'image':
        url = child.attrGet('src')
        if url:
          links.append(Link(source, child_line,
                            column_for(lines, child_line, url), url))
  return dedupe_links(links)
```

要点解读：

- `token.content` 对于 `html_block` / `html_inline` 就是那段原始 HTML 文本，直接喂给 `html_links`；第三个参数 `line` / `child_line` 是这段子串在文件里的**起始行**（见 4.4 的 `base_line`）。
- `child.attrGet('href')` / `attrGet('src')` 取属性值；`if url:` 过滤掉空地址。
- `link_open` 只取开头这一个 token，闭标签 `link_close` 和中间的 `text` 都不需要——地址只在开标签上。
- 行号用 `line_for(token)` / `line_for(child, line)`，列号用 `column_for(lines, child_line, url)`（见 4.4）。
- `token.children or []`：块级 `inline` token 才有 children，且可能为 `None`，故用 `or []` 兜底。

#### 4.1.4 代码实践

**目标**：亲手验证 markdown-it 把不同写法的链接切成不同的 token。

**步骤**：

1. 在仓库根目录起一个 Python 交互环境（需先 `pip install -r requirements.txt`），执行：

```python
from markdown_it import MarkdownIt
md = MarkdownIt('commonmark', {'html': True, 'linkify': True}).enable('linkify')
sample = "[文档](/lv1-main/) 和图片 ![](/x.png)，还有裸 https://e.com/a。\n"
for t in md.parse(sample):
    if t.type == 'inline':
        for c in (t.children or []):
            print(c.type, dict(c.attrs) if c.attrs else '')
```

2. 观察输出里的 token 类型与属性。

**预期结果（基于源码逻辑，待本地验证）**：

- `[文档](/lv1-main/)` → 出现 `link_open`，其 `attrs` 含 `{'href': '/lv1-main/'}`。
- `![](/x.png)` → 出现 `image`，含 `{'src': '/x.png'}`。
- 裸 `https://e.com/a` → 同样出现 `link_open`，`href` 为 `https://e.com/a`（这是 `linkify` 的功劳）。

这说明：**在本检查器眼里，裸 URL 和手写链接在 Markdown 里是「同一种东西」**，都走 `link_open` 分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `extract_markdown_links` 里只处理 `link_open`，却忽略 `link_close` 和中间的 `text` token？

**参考答案**：链接的目标地址只存放在开标签 `link_open` 的 `href` 属性上；`text` 是显示文字、`link_close` 是闭标记，它们都不携带 URL，提取链接时无需关心。

**练习 2**：如果一份 Markdown 里完全用原始 HTML 写链接（`<a href="/p">go</a>` 而非 `[go](/p)`），它会被哪条分支捕获？

**参考答案**：取决于它是否独占一行。独占一行会被切成 `html_block`，行内则被切成 `inline` 下的 `html_inline` 子 token；二者都交给 `html_links` 处理（见 4.2），而不是 `link_open` 分支。

---

### 4.2 HTML 链接抽取

#### 4.2.1 概念说明

无论是真正的 `.html` 文件，还是 Markdown 里嵌入的原始 HTML 片段，都需要一种「按 HTML 标签抽取链接」的能力。HTML 里能携带链接的属性不只 `<a href>` 一种：`<img src>`、`<img srcset>`、`<script src>`、`<link href>`、`<video poster>` 等等都是。本仓库用 `selectolax`（一个快速的 HTML 解析库）来干这件事。

selectolax 能把 HTML 解析成 DOM、用 CSS 选择器批量取节点、读节点的属性值，但它**不保留每个节点在原文里的字符偏移**。而我们的错误信息需要精确到「第几行第几列」，于是代码采用了一个常见技巧：**拿到节点后，再回到原文里把它的文本「重新定位」一次**，据此反推行列号。

#### 4.2.2 核心流程

`html_links(source, text, base_line)` 的流程：

1. 用 `HTMLParser(text)` 解析，用预生成的选择器 `HTML_LINK_SELECTOR` 取出所有「带链接属性」的节点。
2. 维护一个单调递增的游标 `cursor`，对每个节点：
   - 查「标签→属性」表 `HTML_LINK_ATTRS` 得到该标签需要检查的属性列表；
   - 用 `find_html_node` 在原文中（从 `cursor` 往后）定位该节点序列化文本 `node.html` 的起始偏移，并推进 `cursor`；
   - 对每个属性取值；若是 `srcset`，用 `parse_srcset` 拆出多个 URL，否则就是单个值；
   - 用 `find_html_value` 在节点范围内定位属性值的位置，再用 `line_column_at` 把偏移换算成「文件行、列」（叠加 `base_line`）。
3. 把每个 URL 包成 `Link` 收集起来。

`srcset` 是个小坑：它的值形如 `a.png 1x, b.png 2x`，一条属性里塞了多个「URL + 描述符」，需要按逗号拆分、再各取第一个空白前的 URL。

#### 4.2.3 源码精读

「标签→属性」表与由它生成的选择器（[scripts/check_links.py:61-75](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L61-L75)）：

```python
HTML_LINK_ATTRS = {
    'a': ('href',),
    'area': ('href',),
    'iframe': ('src',),
    'img': ('src', 'srcset'),
    'link': ('href',),
    'script': ('src',),
    'source': ('src', 'srcset'),
    'video': ('poster', 'src'),
}
HTML_LINK_SELECTOR = ', '.join(
    f'{tag}[{attr}]'
    for tag, attrs in HTML_LINK_ATTRS.items()
    for attr in attrs
)
```

`HTML_LINK_SELECTOR` 最终展开成 `a[href], area[href], iframe[src], img[src], img[srcset], link[href], script[src], source[src], source[srcset], video[poster], video[src]`，一次 CSS 查询就能捞出所有可能含链接的节点。新增一种「带链接的标签」只需往表里加一行，选择器会自动跟着变。

`html_links` 主体（[scripts/check_links.py:317-332](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L317-L332)）：

```python
def html_links(source: Path, text: str, base_line: int = 1) -> list[Link]:
  links: list[Link] = []
  cursor = 0
  for node in HTMLParser(text).css(HTML_LINK_SELECTOR):
    attrs = HTML_LINK_ATTRS.get(node.tag, ())
    node_start = find_html_node(text, node.html, cursor)
    cursor = max(cursor, node_start + 1)
    for attr in attrs:
      value = node.attributes.get(attr)
      if value is None:
        continue
      for url in parse_srcset(value) if attr == 'srcset' else [value]:
        line, column = line_column_at(
            text, find_html_value(text, node_start, url), base_line)
        links.append(Link(source, line, column, url))
  return links
```

辅助函数（[scripts/check_links.py:291-314](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L291-L314)）：

```python
def parse_srcset(value: str) -> list[str]:
  return [item.strip().split()[0] for item in value.split(',') if item.strip().split()]

def line_column_at(text: str, offset: int, base_line: int = 1) -> tuple[int, int]:
  prefix = text[:max(offset, 0)]
  line = base_line + prefix.count('\n')
  column = len(prefix.rsplit('\n', 1)[-1]) + 1
  return line, column

def find_html_node(text: str, node_html: str, cursor: int) -> int:
  if node_html:
    offset = text.find(node_html, cursor)
    if offset != -1:
      return offset
  return cursor

def find_html_value(text: str, node_start: int, value: str) -> int:
  offset = text.find(value, node_start)
  if offset == -1:
    offset = text.find(html.unescape(value), node_start)
  return offset if offset != -1 else node_start
```

要点解读：

- **游标 `cursor`**：因为节点是按文档顺序返回的，从上一个节点之后开始查找，可以避免在出现重复节点时「总匹配到第一个」。`cursor = max(cursor, node_start + 1)` 保证它单调前进。
- **`line_column_at` 的 `base_line`**：当 `html_links` 处理的是 Markdown 里某段 `html_block`/`html_inline` 子串时，这段子串并非从文件第 1 行开始，而是从 `base_line` 开始；所以「子串内偏移换算的行号」要加上 `base_line`。直接处理整份 `.html` 时 `base_line=1`。
- **`find_html_value` 的双查找**：先按原值找，找不到再按 `html.unescape` 后的值找——因为源码里属性值可能含 HTML 实体（如 `&amp;`），而 selectolax 给出的值可能是已解码形式。
- `srcset` 特判：`parse_srcset` 把 `"a.png 1x, b.png 2x"` 拆成 `['a.png', 'b.png']`，丢弃描述符。

#### 4.2.4 代码实践

**目标**：体会 selectolax「不保留源偏移」，以及代码如何用 `node.html` 回原文定位。

**步骤**：

1. 在 Python 里运行（需 `selectolax`）：

```python
from selectolax.parser import HTMLParser
html = '<p>x</p>\n<a href="/lv1-main/">go</a>\n<img src="a.png" srcset="a.png 1x, b.png 2x">'
for n in HTMLParser(html).css('a[href], img[src], img[srcset]'):
    print(n.tag, n.html, '|', n.attributes)
```

2. 观察：`n.html` 是节点序列化文本，`n.attributes` 是属性字典，但**没有任何字段直接告诉你它在原文第几行**——这就是本检查器要额外做 `find_html_node` + `line_column_at` 的原因。

**预期结果（待本地验证）**：能拿到每个节点的 `href`/`src`/`srcset` 值，但拿不到源行号，印证「重新定位」的必要性。

#### 4.2.5 小练习与答案

**练习 1**：`HTML_LINK_SELECTOR` 是怎么从 `HTML_LINK_ATTRS` 自动生成的？如果想新增支持 `<embed src>`，需要改几处？

**参考答案**：它是对表里每个 `(tag, attr)` 组合拼出 `tag[attr]` 再用 `, ` 连接的。新增 `<embed src>` 只需在 `HTML_LINK_ATTRS` 里加 `'embed': ('src',)` 一行，选择器会自动多出 `embed[src]`，`html_links` 也会自动检查它（因为属性来自同一张表）。

**练习 2**：为什么 `html_links` 里要用游标 `cursor` 不断推进，而不是每次都从原文开头 `text.find(node.html)`？

**参考答案**：若文档里有两个完全相同的节点（如两个 `<a href="/x">`），每次都从头查找会永远匹配到第一个，导致第二个节点的行号算错。游标单调前进，保证按出现顺序依次定位。

---

### 4.3 裸 URL 兜底

#### 4.3.1 概念说明

「裸 URL」指直接出现在正文里、没有被任何标签或 Markdown 语法包裹的网址，如 `详见 https://example.com/page。`。前面看到，在 **Markdown 正文**里，markdown-it 的 `linkify` 会自动把它们变成 `link_open`，所以已被 4.1 覆盖。

但在 **`.html` 文件的纯文本**里（以及 Markdown 内嵌的原始 HTML 文本里），没有任何「自动链接」机制，一个写在 `<p>` 文字里的 `https://...` 不会被 `html_links`（它只看属性）捕获。为了让 `.html` 文件里散落的网址也不被漏检，检查器额外加了一道**裸 URL 兜底**——注意：它只在 `extract_html_links`（针对 `.html` 文件）里被调用，Markdown 路径不调用它。

#### 4.3.2 核心流程

`bare_html_links(source, text)` 的流程：

1. 逐行扫描文本。
2. 对每一行用正则 `BARE_URL_RE` 找出所有匹配。
3. 对每个匹配，用 `trim_bare_url` 修剪掉被「误吞」的尾部标点与不成对的括号/方括号。
4. 以「行号 = 当前行号、列号 = 匹配起点 + 1」构造 `Link`。

正则只识别以 `//`、`http://`、`https://` 开头的串，遇到空白或尖括号/引号就停。修剪函数处理两类常见误伤：句末标点（`. , ; :`）和包裹用的括号（如 `(见 https://x.com)` 里多吞的右括号）。

#### 4.3.3 源码精读

裸 URL 正则（[scripts/check_links.py:58](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L58)）：

```python
BARE_URL_RE = re.compile(r'(?:(?:https?:)?//)[^\s<>\'"]+')
```

- `(?:(?:https?:)?//)`：匹配 `http://`、`https://`，或省略协议的 `//`（协议相对 URL）。
- `[^\s<>\'"]+`：贪婪地吃掉「非空白、非 `<` `>`、非单/双引号」的字符——碰到空格、标签边界或引号就停，避免吞掉 HTML 标签。

尾部修剪（[scripts/check_links.py:335-341](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L335-L341)）：

```python
def trim_bare_url(raw: str) -> str:
  url = raw.rstrip('.,;:')
  while url.endswith(')') and url.count('(') < url.count(')'):
    url = url[:-1]
  while url.endswith(']') and url.count('[') < url.count(']'):
    url = url[:-1]
  return url
```

- 先砍掉结尾的 `.` `,` `;` `:`（句末标点常被贪婪匹配吞进来）。
- 再用「括号配平」修剪：若结尾是 `)` 但全文 `(` 比 `)` 少，说明这个右括号是包裹用的、不属于 URL，去掉它；方括号同理。用 `while` 是因为可能连续多吞（如 `]))`）。

提取函数（[scripts/check_links.py:344-350](https://github.com/pku-minic/online-doc/blob/d172f8994f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L344-L350)）：

```python
def bare_html_links(source: Path, text: str) -> list[Link]:
  links: list[Link] = []
  for line_no, line in enumerate(text.splitlines(), start=1):
    for match in BARE_URL_RE.finditer(line):
      links.append(Link(source, line_no, match.start() + 1,
                        trim_bare_url(match.group(0))))
  return links
```

- 按行扫描让「列号」天然落在该行内：`match.start() + 1` 即列（1 起始）。
- `extract_html_links` 把它和 `html_links` 拼接后统一去重（[scripts/check_links.py:381-383](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L381-L383)）：

```python
def extract_html_links(root: Path, source: Path) -> list[Link]:
  text = (root / source).read_text(encoding='utf-8')
  return dedupe_links(html_links(source, text) + bare_html_links(source, text))
```

> 关键区分：`bare_html_links` 只服务于 `.html` 文件。Markdown 文件里的裸 URL 由 markdown-it 的 `linkify` 在 4.1 里捕获，无需这道兜底。

#### 4.3.4 代码实践

**目标**：验证 `trim_bare_url` 如何处理「被包裹/带标点」的裸 URL。

**步骤**：在 Python 里执行：

```python
import sys
sys.path.insert(0, 'scripts')
from check_links import trim_bare_url, BARE_URL_RE
for s in ['https://x.com/a)。', '(see https://x.com/b)', 'https://x.com/c,']:
    m = BARE_URL_RE.search(s)
    print(repr(m.group(0)), '->', repr(trim_bare_url(m.group(0))))
```

**预期结果（待本地验证）**：

- `https://x.com/a)。` 的匹配 `https://x.com/a)。` 中，末尾中文句号不在 `.,;:` 与括号集里，按当前实现不会被 `trim_bare_url` 移除（它只处理 ASCII 标点与 `()` `[]`）；这提示我们：**兜底规则对中文标点覆盖有限**，是个已知的边界。
- `(see https://x.com/b)` 的匹配是 `https://x.com/b)`，修剪后变 `https://x.com/b`（去掉不成对的右括号）。
- `https://x.com/c,` 修剪后变 `https://x.com/c`（去掉结尾逗号）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Markdown 文件不调用 `bare_html_links`，`.html` 文件却要？

**参考答案**：Markdown 正文里的裸 URL 会被 markdown-it 的 `linkify` 自动转成 `link_open`，已在 4.1 捕获；而 `.html` 的纯文本没有自动链接机制，`html_links` 又只看标签属性，所以必须额外用 `bare_html_links` 兜底，否则会漏检。

**练习 2**：`BARE_URL_RE` 用 `[^\s<>\'"]+` 而不是 `.+` 来匹配 URL 主体，好处是什么？

**参考答案**：限制字符集可以让匹配在遇到空白、尖括号或引号时及时停止，避免把后面的 HTML 标签或文字也吞进来（例如 `<a> https://x.com </a>` 不会把 `</a>` 吃掉），从而减少误匹配。

---

### 4.4 行列号定位与去重

#### 4.4.1 概念说明

错误信息要能精确指到「哪个文件第几行第几列」，读者才能快速定位修复。因此每条 `Link` 除了 `url`，还携带 `line` 和 `column`。不同来源的链接，行列号算法不同：

- **Markdown 链接**：markdown-it 的 token 自带 `map`（所在行范围），行号直接取；列号则在「该行原文」里查找 URL 字符串得到。
- **HTML 链接**：selectolax 不给源偏移，需先在原文定位节点与属性值（4.2 的 `find_html_*`），再换算成行列。
- **裸 URL**：按行扫描，行号是循环计数、列号是匹配起点。

此外，同一条 URL 可能在同一文件同一行出现多次（或被多种提取路径重复捕获）。为了不重复报告、不重复校验，需要一个**去重**步骤。去重的粒度是「来源文件 + 行 + URL」三元组：同文件同行同 URL 只保留一条，但不同行或不同文件的同名 URL 仍各自保留（因为每一处出现都可能是独立的坏链）。

#### 4.4.2 核心流程

辅助函数各有分工：

- `line_for(token, fallback)`：从 token 的 `map` 取起始行（1 起始），无 `map` 则用 `fallback`。
- `column_for(lines, line, url)`：在第 `line` 行原文里查找 `url`（先原值、再 `html.unescape` 后的值），返回列号（1 起始）。
- `dedupe_links(links)`：以 `(source, line, url)` 为键去重，保留首次出现。

分发函数 `extract_links` 按扩展名把每个文件交给 Markdown 或 HTML 提取器（[scripts/check_links.py:386-393](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L386-L393)）。

#### 4.4.3 源码精读

行号与列号（[scripts/check_links.py:265-276](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L265-L276)）：

```python
def line_for(token: Token, fallback: int = 1) -> int:
  return token.map[0] + 1 if token.map else fallback

def column_for(lines: list[str], line: int, url: str) -> int:
  if line < 1 or line > len(lines):
    return 1
  decoded = html.unescape(url)
  index = lines[line - 1].find(url)
  if index == -1:
    index = lines[line - 1].find(decoded)
  return index + 1 if index >= 0 else 1
```

- `token.map` 是 markdown-it 给的 `[起始行, 结束行]`（0 起始），`+1` 转 1 起始。
- `column_for` 的「双查找」与 `find_html_value` 同理：原文里属性值可能写成 HTML 实体，先按传进来的 `url` 找、找不到再按解码值找；都找不到就退回列 `1`（保证总有一个合法列号，不让定位崩溃）。

去重（[scripts/check_links.py:279-288](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L279-L288)）：

```python
def dedupe_links(links: list[Link]) -> list[Link]:
  seen: set[tuple[Path, int, str]] = set()
  result: list[Link] = []
  for link in links:
    key = (link.source, link.line, link.url)
    if key in seen:
      continue
    seen.add(key)
    result.append(link)
  return result
```

- 键为 `(来源文件, 行号, URL)`，**不含列号**——所以同一行里出现的相同 URL 会被合并成一条。
- 注意 `link.url` 已在 `Link.__post_init__` 里做过 `html.unescape(strip())` 归一化（[scripts/check_links.py:96-97](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L96-L97)），因此 `&amp;` 与 `&` 形式的同一 URL 能正确判等去重。

按扩展名分发（[scripts/check_links.py:386-393](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L386-L393)）：

```python
def extract_links(root: Path, files: list[Path], file_types: FileTypes) -> list[Link]:
  links: list[Link] = []
  for source in files:
    if file_types.is_markdown(source):
      links.extend(extract_markdown_links(root, source))
    elif file_types.is_html(source):
      links.extend(extract_html_links(root, source))
  return links
```

`file_types`（u4-l1 讲过）依据配置里的 `md_extensions`/`html_extensions` 判定文件类型，决定走 Markdown 还是 HTML 提取器。

> 去重为什么在「每个文件内部」做（`extract_markdown_links`/`extract_html_links` 末尾各调一次），而不是全局做一次？因为同一文件里同一行可能既被 `html_links` 又被别的路径捕获，先在文件内合并能减少后续处理量；而跨文件的相同 URL 本就要分别报告，无需全局合并。

#### 4.4.4 代码实践

**目标**：理解 `dedupe_links` 的去重粒度——同文件同行同 URL 合并，但跨行保留。

**步骤**：在 Python 里执行：

```python
import sys
sys.path.insert(0, 'scripts')
from check_links import Link, dedupe_links
from pathlib import Path
s = Path('demo.md')
links = [
    Link(s, 3, 5,  '/lv1-main/'),   # 第 3 行
    Link(s, 3, 30, '/lv1-main/'),   # 同行同 URL → 合并
    Link(s, 9, 5,  '/lv1-main/'),   # 不同行 → 保留
]
print(len(dedupe_links(links)))
```

**预期结果（待本地验证）**：输出 `2`——第 3 行的两条相同 URL 合并为一条，第 9 行的那条保留。

#### 4.4.5 小练习与答案

**练习 1**：`column_for` 在两处查找都失败时返回 `1`，而不是抛异常。这种「兜底返回」有什么好处？

**参考答案**：保证 `Link` 始终有一个合法的列号，让后续的格式化输出（`文件:行:列`）和排序不会因为列号缺失而崩溃；定位精度退化为「仅精确到行」，仍可接受。

**练习 2**：去重键为什么是 `(source, line, url)` 而不是只看 `url`？

**参考答案**：同一个 URL 出现在不同文件或不同行时，每一处都是独立的链接引用，可能其中一处指向的目标后来失效了；只按 `url` 去重会丢掉「另一处也引用了它」的信息，无法把每一处坏链都报告给作者。按「文件+行+URL」去重则只合并真正的重复，保留所有有意义的出现位置。

---

## 5. 综合实践

把本讲四个模块串起来：写一份**同时包含 Markdown 链接、图片、裸 URL** 的小文档，放进 `docs/`，用检查器验证三种链接都被识别。

**步骤**：

1. 先记录基线。在仓库根目录运行（不联网）：

```bash
python3 scripts/check_links.py --config check-links.toml --no-http --verbose
```

记下末尾汇总行里的链接总数，例如 `Checked N file(s), M link(s), ...` 中的 `M`。

2. 新建文件 `docs/u4-l2-extract-test.md`，内容如下（图片用仓库里真实存在的图，确保能通过本地校验）：

```markdown
# u4-l2 提取测试

- Markdown 内部链接：见 [lv1 主线](/lv1-main/)。
- 图片：![栈帧](lv4-const-n-var/example-stack-frame.png)
- 裸 URL：详见 https://example.com/u4-l2-test
```

3. 再次运行同一条命令。

**需要观察的现象**：

- 末尾汇总行的链接总数应当比基线**多 3**（说明三种写法各被识别出一条）。
- 在逐条日志里应出现两条 `Checking local link: ...`：
  - `/lv1-main/ ... PASS`（Markdown 链接，按 Docsify 路由解析到 `docs/lv1-main/README.md`，u4-l3 会详讲）；
  - `lv4-const-n-var/example-stack-frame.png ... PASS`（图片，相对路径解析到 `docs/lv4-const-n-var/example-stack-frame.png`，存在故通过）。
- **裸 URL 不会出现在逐条日志里**：它是远程链接（`https://...`），在 `--no-http` 下被静默跳过（不发起请求，也不加入待检远程集合），但仍计入链接总数。

**预期结果**：链接总数 `+3`、两条本地链接 `PASS`、裸 URL「只计数不检查」。若总数只 `+2`，说明裸 URL 没被 `linkify` 识别——检查它前后是否有空格分隔。

**实践后清理**：验证完毕后删除 `docs/u4-l2-extract-test.md`，避免把它误提交进文档站。

> 这个综合实践把四件事连了起来：Markdown 链接走 `link_open`（4.1）、图片走 `image`（4.1）、裸 URL 由 `linkify` 识别（4.1，并呼应 4.3 的「Markdown 不需要兜底」）、而本地/远程的分流与行列号则会进入 u4-l3/u4-l4。

## 6. 本讲小结

- 链接提取是检查器的「输入侧」，分 Markdown 与 HTML 两条路径，由 `extract_links` 按扩展名分发。
- **Markdown 路径**用 `markdown-it-py`（`commonmark` + `html` + `linkify`）把文档切成 token 流，从 `link_open` 取 `href`、从 `image` 取 `src`，原始 HTML（`html_block`/`html_inline`）转交 HTML 提取器。
- **HTML 路径**用 `selectolax` 按「标签→属性」表（`HTML_LINK_ATTRS` → `HTML_LINK_SELECTOR`）批量取节点属性，并对 `srcset` 做拆分；因 selectolax 不保留源偏移，用「游标 + 回原文重定位」反推行列号。
- **裸 URL 兜底**（`BARE_URL_RE` + `trim_bare_url`）只在 `.html` 文件上运行，处理正文里未被标签包裹的网址并修剪尾部标点与不成对括号；Markdown 里的裸 URL 已由 `linkify` 覆盖。
- **行列号**对 Markdown 取自 token `map` + 原文查找、对 HTML 取自偏移换算、对裸 URL 取自行号与匹配起点；**去重**以 `(来源文件, 行, URL)` 为键，且 `Link` 构造时已对 URL 做 `html.unescape(strip())` 归一化。

## 7. 下一步学习建议

- 本讲产出的是一筐「带行列号的 `Link`」。这些 `Link` 接下来怎么判对错？请进入 **u4-l3 Docsify 路由解析与本地链接校验**：看 `docsify_candidates` 如何把 `/lv1-main/` 这类路由还原成候选 `.md` 文件、`collect_local_anchors` 如何校验 `?id=` 锚点。
- 远程 `https://` 链接的校验在 **u4-l4 远程链接的异步校验与片段验证**，那里会用到本讲提取出的 `Link.normalized_url`。
- 想巩固「token 流」的直觉，可再读 `collect_local_anchors`（[scripts/check_links.py:431-442](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L431-L442)），它同样遍历 markdown-it token，但这次是为了收集标题锚点——与本讲形成对照。
