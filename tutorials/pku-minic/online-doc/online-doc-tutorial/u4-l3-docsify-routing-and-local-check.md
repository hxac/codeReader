# Docsify 路由解析与本地链接校验

## 1. 本讲目标

本讲是链接检查器核心逻辑的「本地侧」精读。学完本讲，你应当能够：

1. 说清楚 `docsify_candidates` 如何把一条 Docsify 风格的站内链接翻译成「一个或多个候选磁盘文件」。
2. 解释为什么同一个路由要尝试多种候选（README 回退、index 回退、扩展名补全）。
3. 读懂锚点（anchor）的归一化流水线：NFC、小写、空格转连字符、标点压缩、URL 编解码，以及「超集匹配」的思想。
4. 掌握本地链接的两类错误——`local-missing-target` 与 `local-missing-anchor`——分别如何判定。

本讲承接 [u4-l2 链接提取子系统](u4-l2-link-extraction.md)：那里产出的是带行列号的 `Link` 列表；本讲回答的问题是——**拿到一条站内 `Link` 后，检查器怎么知道它指向的页面/锚点到底存不存在？** 答案就是：先用 Docsify 路由规则把链接还原成候选文件，再在磁盘上逐一核对，必要时再核对标题锚点。

## 2. 前置知识

### 2.1 Docsify 路由回顾

在 [u1-l4 站点入口与导航机制](u1-l4-entry-and-navigation.md) 我们已经知道：Docsify 是单页应用（SPA），它**不在磁盘上存 HTML，而是把 Markdown 文件按路由即时渲染**。路由到文件的约定是：

- `/lv1-main/structure` 渲染 `docs/lv1-main/structure.md`
- `/lv1-main/` 渲染该目录的 `docs/lv1-main/README.md`
- `/misc-app-ref/koopa?id=符号名称` 渲染 `docs/misc-app-ref/koopa.md`，并滚动到标题「符号名称」

注意第三个例子里的 `?id=`：因为 Docsify 用 `#`（hash）做整站路由，页内锚点**不能**再用 `#锚点`，而是改用 `?id=锚点` 来表示。所以本仓库文档里的站内链接一律用 `?id=` 写锚点（见 [u3-l2 Docsify 扩展 Markdown 写作规范](u3-l2-docsify-markdown-conventions.md)）。本讲的检查器必须同时理解这两种写法。

### 2.2 URL 的三段：path / query / fragment

Python 标准库 `urllib.parse.urlparse` 把一个 URL 拆成几段：

```python
>>> urlparse('/misc-app-ref/koopa?id=符号名称#frag')
ParseResult(scheme='', netloc='', path='/misc-app-ref/koopa',
            query='id=符号名称', fragment='frag')
```

本讲最关心 `path`（路由主体）、`query`（拿 `?id=` 锚点）、`fragment`（拿 `#锚点`，本仓库文档基本不用，但检查器为健壮性仍支持）。

### 2.3 Unicode NFC 归一化

同一个汉字可能有多种 Unicode 编码形式（组合/分解）。`unicodedata.normalize('NFC', s)` 把字符串统一到「规范合成形式（Normalization Form C）」，让两个视觉相同的标题生成相同的锚点，避免「看着一样其实字节不同」导致的误判。

### 2.4 Markdown 标题如何变成锚点

Markdown 里 `## 词法/语法分析` 这样的标题，渲染成网页后通常带一个 `id` 锚点。锚点一般由标题文字经「小写 + 空格转连字符」得到（GitHub、Docsify 大致如此），但各家实现细节略有出入。检查器没有去猜 Docsify 的精确算法，而是**把同一标题的多种可能写法都算出来再做集合匹配**，这是本讲要讲清楚的核心技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `scripts/check_links.py` | 全部本地校验逻辑都在这一个文件里 |
| `docs/index.html` | Docsify 站点配置，确认 `docs_root`、路由风格等前提 |

本讲涉及的函数集中在 [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py)，按职责分四组：

| 组 | 函数 | 职责 |
|----|------|------|
| 路由主体 | `docsify_candidates` | URL → 候选文件列表 + 锚点 |
| 候选生成 | `markdown_page_candidates` / `markdown_index_candidates` / `html_page_candidates` / `html_index_candidates` | 把一个「路径基」扩展成具体文件 |
| 锚点 | `collect_local_anchors` / `anchor_forms` / `expand_anchor_forms` / `parse_html_anchors` / `inline_text` | 归一化 + 收集目标文件的锚点集合 |
| 判定 | `check_local_link` | 综合上面两组，产出 `Issue` 或通过 |

---

## 4. 核心概念与源码讲解

### 4.1 docsify_candidates 候选规则

#### 4.1.1 概念说明

站内链接写的是**浏览器路由**（如 `/lv1-main/structure`），而磁盘上存的是**真实文件**（如 `docs/lv1-main/structure.md`）。`docsify_candidates` 就是这两者之间的「翻译器」：给它一条站内链接，它返回**若干个候选文件路径**外加**锚点字符串**。

为什么是「若干个」候选而不是一个？因为单凭路由字符串，有时无法唯一确定文件。例如 `/lv1-main/structure` 既可能是「页面 `structure.md`」，也可能是「目录 `structure/` 下的 `README.md`」（Docsify 允许把目录当页面访问）。检查器无法预判，索性把两种都列出来，交给后续「哪个文件真实存在」来一锤定音。

#### 4.1.2 核心流程

`docsify_candidates` 先从 URL 解析出「锚点」和「路径基」，再按路径的形态特征分四种情况返回候选：

```text
输入：raw_url（一条站内链接）
├─ 解析 fragment(#x) 与 query(?id=y) → anchor
├─ 解析 path，URL 解码
├─ 若 path 为空（形如 #x 或 ?id=y）        → 候选 = [源文件本身]
├─ 计算 base（绝对路由→docs_root 下；相对路由→源文件所在目录）
├─ 路径以 '/' 结尾（目录，如 /lv1-main/）   → 候选 = README/index 系列
├─ 路径带后缀（如 /x/README.md）            → 候选 = [原样 base]
└─ 路径无后缀（如 /lv1-main/structure）     → 候选 = page + index 四件套
输出：(候选路径列表, anchor)
```

四种情况对应的候选，可以用一张表概括（设 `docs_root=docs`、Markdown 扩展名 `.md`、HTML 扩展名 `.html`）：

| 链接写法 | path 特征 | 返回的候选文件 |
|----------|-----------|----------------|
| `#x` / `?id=x` | path 为空 | `源文件本身` |
| `/lv1-main/` | 以 `/` 结尾 | `docs/lv1-main/README.md`、`docs/lv1-main/index.html` |
| `/lv1-main/README.md` | 有后缀 | `docs/lv1-main/README.md`（原样） |
| `/lv1-main/structure` | 无后缀 | `docs/lv1-main/structure.md`、`docs/lv1-main/structure/README.md`、`docs/lv1-main/structure.html`、`docs/lv1-main/structure/index.html` |

#### 4.1.3 源码精读

锚点取自 `fragment` 或 `query` 里的 `id`，用一个小工具 [first_value](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L445-L446) 取列表首元素（`parse_qs` 返回的是 `{'id': [值]}`）：

```python
def first_value(values: list[str] | None) -> str | None:
  return values[0] if values else None
```

主体逻辑在 [docsify_candidates](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L465-L489)：

```python
def docsify_candidates(root, docs_root, source, raw_url, file_types):
  parsed = urlparse(raw_url)
  anchor = parsed.fragment or first_value(parse_qs(parsed.query).get('id'))
  path = unquote(parsed.path)
  if not path:                                   # ① 无 path：回到源文件
    return [root / source], anchor
  base = docs_root / path.lstrip('/') if path.startswith('/') \
      else (root / source).parent / path         # ② 绝对→docs_root；相对→源文件目录
  base = Path(os.path.normpath(base))
  if path.endswith('/'):                         # ③ 目录路由
    return markdown_index_candidates(base, file_types) + html_index_candidates(base, file_types), anchor
  if Path(path).suffix:                          # ④ 已显式带扩展名
    return [base], anchor
  return (                                       # ⑤ 无扩展名：四件套全试
      markdown_page_candidates(base, file_types)
      + markdown_index_candidates(base, file_types)
      + html_page_candidates(base, file_types)
      + html_index_candidates(base, file_types)
  ), anchor
```

逐句说明：

- **锚点解析**：`fragment` 优先（标准 `#x`），没有就读 `?id=`。这覆盖了本仓库的 `?id=` 写法，也兼容传统的 `#x`。
- **② 路径基 `base` 的两种来源**：以 `/` 开头的「绝对路由」挂在 `docs_root` 下（这就是 [DEFAULT_CONFIG](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L44) 里 `docs_root='docs'` 的用途）；不以 `/` 开头的「相对链接」（如 `./next.md`）则挂在源文件所在目录下，与 Markdown 相对路径语义一致。
- **`os.path.normpath`**：把 `../`、`./`、多余斜杠归一，防止 `lv1-main/../lv1-main/x` 这类写法绕过判定。
- **③④⑤ 三分支**：分别对应目录、显式文件、歧义路由，正是上面表格的三行。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：不运行代码，纯靠读 `docsify_candidates`，手工「跑」一遍真实链接。

**操作步骤**：

1. 打开 `docs/lv1-main/ir-gen.md`，找到第 91 行的链接 `[Koopa IR 规范](/misc-app-ref/koopa?id=符号名称)`。
2. 以 `raw_url = '/misc-app-ref/koopa?id=符号名称'` 代入 `docsify_candidates`（设源文件是 `docs/lv1-main/ir-gen.md`）：
   - `anchor` = ？（提示：`fragment` 为空，读 `?id=`）
   - `path` = ？（URL 解码后）
   - 命中哪个分支？（提示：有无后缀？）
   - 返回的候选文件列表是？

**需要观察/预期结果**：

- `anchor = '符号名称'`
- `path = '/misc-app-ref/koopa'`，无后缀 → 命中 ⑤ 分支
- 候选为 `docs/misc-app-ref/koopa.md`、`docs/misc-app-ref/koopa/README.md`、`docs/misc-app-ref/koopa.html`、`docs/misc-app-ref/koopa/index.html`

**验证**：`docs/misc-app-ref/koopa.md` 确实存在（`git ls-files docs/misc-app-ref/koopa.md` 可见），所以这条链接会通过目标检查；锚点「符号名称」也确实存在于 [docs/misc-app-ref/koopa.md:3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md#L3) 的 `## 符号名称` 标题——这是本仓库里一条真实且合法的「带锚点站内链接」。

#### 4.1.5 小练习与答案

**练习 1**：链接 `/preface/`（`toc.md` 中真实存在）会命中哪个分支？返回哪些候选？

> **答案**：`path='/preface/'` 以 `/` 结尾，命中 ③ 分支，返回 `markdown_index_candidates` + `html_index_candidates` = `docs/preface/README.md`、`docs/preface/index.html`。前者真实存在，故通过。

**练习 2**：若有人写了一条相对链接 `next.md`（不以 `/` 开头），`base` 会指向哪里？

> **答案**：走 ② 的 `else` 分支，`base = (root / source).parent / 'next.md'`，即相对**源文件所在目录**解析，和 Markdown 的相对路径直觉一致；随后因有 `.md` 后缀命中 ④ 分支返回 `[base]`。

---

### 4.2 README/index 回退

#### 4.2.1 概念说明

4.1 调用的四个 `*_candidates` 辅助函数，职责单一：给定一个「路径基 `base`」，吐出**符合某类约定的具体文件**。它们之所以要成套出现，是因为 Docsify 对「目录当页面」和「页面当目录」都很宽容——访问 `/lv1-main/structure` 时，它可能是文件也可能是目录，于是检查器**两种都试**，这就是「回退（fallback）」的含义：先试最可能的，试不到再退而求其次。

四个函数对应「2 种文件类型 × 2 种约定」的笛卡尔积：

| 函数 | 约定 | 示例（base=`docs/x/structure`） |
|------|------|----------------------------------|
| `markdown_page_candidates` | 把 base 当**页面**，补 Markdown 扩展名 | `docs/x/structure.md` |
| `markdown_index_candidates` | 把 base 当**目录**，找 `README.md` | `docs/x/structure/README.md` |
| `html_page_candidates` | 把 base 当页面，补 HTML 扩展名 | `docs/x/structure.html` |
| `html_index_candidates` | 把 base 当目录，找 `index.html` | `docs/x/structure/index.html` |

#### 4.2.2 核心流程

四个函数都是「遍历配置里的扩展名集合，逐个拼接」。关键在于 `Path.with_suffix` 与 `base / f'XXX{ext}'` 的区别：

```text
page 约定：base.with_suffix(ext)   → 把 base 自身最后一个后缀换成 ext
index 约定：base / f'README{ext}'  → 在 base 下新建一层 README/index
```

类型集合（`.md` / `.html`）来自 [DEFAULT_CONFIG](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L44) 的 `md_extensions` / `html_extensions`，可被 `check-links.toml` 覆盖。本仓库两者分别为 `['.md']`、`['.html']`，故每个函数通常各产出 1 个候选。

#### 4.2.3 源码精读

四个函数极短，结构成对（[scripts/check_links.py:449-462](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L449-L462)）：

```python
def markdown_page_candidates(base, file_types):
  return [base.with_suffix(extension) for extension in file_types.markdown]

def markdown_index_candidates(base, file_types):
  return [base / f'README{extension}' for extension in file_types.markdown]

def html_page_candidates(base, file_types):
  return [base.with_suffix(extension) for extension in file_types.html]

def html_index_candidates(base, file_types):
  return [base / f'index{extension}' for extension in file_types.html]
```

要点：

- **page 用 `with_suffix`**：`Path('docs/x/structure').with_suffix('.md')` 得 `docs/x/structure.md`；若 base 本身有后缀（如 `a.txt`），会被替换而非追加。
- **index 用 `/` 拼接**：`Path('docs/x/structure') / 'README.md'` 得 `docs/x/structure/README.md`，即把 base 当目录。
- **Markdown 与 HTML 分离**：本仓库 `docs/` 下几乎没有裸 `.html`（站点是 SPA，只有 `index.html` 一个外壳），但 HTML 候选仍保留，是为了校验 `index.html` 本身及将来可能出现的 HTML 片段。

把这些函数代回 4.1 的 ⑤ 分支，无后缀路由 `structure` 的完整候选顺序就是：`structure.md` → `structure/README.md` → `structure.html` → `structure/index.html`。检查器随后取**第一个真实存在**的为准。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解目录路由与无后缀路由在候选集合上的差异。

**操作步骤**：

1. 对照本节表格，写出 `/lv1-main/`（目录）与 `/lv1-main/structure`（无后缀）各自的候选列表。
2. 回答：为什么目录路由（③）只试 index 系列，而无后缀路由（⑤）要试 page + index 全套？

**需要观察/预期结果**：

- `/lv1-main/` → `docs/lv1-main/README.md`、`docs/lv1-main/index.html`（只有 index 系列）。
- `/lv1-main/structure` → `docs/lv1-main/structure.md`、`docs/lv1-main/structure/README.md`、`docs/lv1-main/structure.html`、`docs/lv1-main/structure/index.html`（page + index 四件套）。
- **理由**：结尾的 `/` 已经明确告诉检查器「这是一个目录」，所以只找该目录的入口文件（`README.md` / `index.html`）；而没有后缀、又没有结尾 `/` 的写法有歧义（可能是页面也可能是目录），于是两种约定都试一遍，靠磁盘存在性消歧。

#### 4.2.5 小练习与答案

**练习 1**：若把 `check-links.toml` 的 `md_extensions` 改成 `[".md", ".markdown"]`，`markdown_page_candidates(base)` 会返回几个候选？

> **答案**：返回 2 个：`base.with_suffix('.md')` 和 `base.with_suffix('.markdown')`。这正是「按配置扩展名集合展开」的设计收益——支持多扩展名无需改逻辑。

**练习 2**：`base / f'README{extension}'` 里 `extension` 已经自带前导点（`.md`），所以拼出来是 `README.md` 而非 `READMEmd`。如果有人误把 `md_extensions` 配成 `["md"]`（漏掉点）会怎样？

> **答案**：不会出错。[normalize_extensions](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L209-L213) 会在加载时给没有前导点的扩展名补上 `.`，所以 `"md"` 会被规整成 `".md"`，最终仍拼出 `README.md`。

---

### 4.3 锚点归一化与收集

#### 4.3.1 概念说明

当一条站内链接带锚点（`/misc-app-ref/koopa?id=符号名称`），光确认目标文件存在还不够，还得确认文件里**真的有这个标题锚点**，否则用户点击后会跳到页面顶部而非指定章节。

难点在于：同一个标题「符号名称」，在链接里可能写成原样、可能被 URL 编码、可能小写、可能带连字符……而目标文件里的标题也可能以不同形式出现。检查器的解法是**「超集匹配」**：把链接的锚点和目标文件的所有标题，**各自展开成一簇等价写法**，只要两簇有任何一个公共元素，就算匹配成功。用集合语言说，就是判断

\[
A_{\text{链接锚点}} \cap B_{\text{目标标题集}} \neq \emptyset
\]

为真即通过。

#### 4.3.2 核心流程

锚点处理分两半：**收集目标文件的锚点集**，和**把任意锚点字符串归一化成一簇写法**。

```text
收集（collect_local_anchors）：读目标文件
├─ HTML 文件：直接抓所有 [id]/[name] 属性
└─ Markdown 文件：
   ├─ 遍历 token，每遇 heading_open + inline，取标题文字 → anchor_forms
   └─ 同时抓文件内 HTML 的 [id]/[name]（Markdown 里可内嵌 HTML）
  最后整体再过一遍 expand_anchor_forms

归一化（anchor_forms）：标题文字 → 4 个基础变体
├─ normalized：NFC + 解 HTML 实体 + 去首尾空白（保留大小写与空格）
├─ lower：小写（保留空格）
├─ hyphenated：空格→连字符
└─ compact-hyphen：去标点 + 空格→连字符
  每个 base 变体再交给 expand_anchor_forms → 再各扩成 4 种（原值/解码/小写/编码）
```

`expand_anchor_forms` 把每个字符串扩成 `{原值, URL 解码, 小写, URL 编码}` 四种，是为了同时容忍「已编码」「未编码」「大小写不同」的写法。

#### 4.3.3 源码精读

先看 [anchor_forms](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L413-L418)，它把一个标题文字变成 4 个基础变体：

```python
def anchor_forms(text: str) -> set[str]:
  normalized = unicodedata.normalize('NFC', html.unescape(text).strip())
  lower = normalized.lower()
  hyphenated = re.sub(r'\s+', '-', lower)
  compact = re.sub(r'[^\wU+0080-U+FFFF\s-]', '', lower)  # 源码以 Unicode 转义书写 U+0080..U+FFFF（非 ASCII）区间
  return expand_anchor_forms({normalized, lower, hyphenated, re.sub(r'\s+', '-', compact)})
```

- `normalized`：先 NFC，再 `html.unescape`（标题里可能有 `&amp;` 之类实体），再 `strip`。
- `lower`：小写化（对中文无影响，但处理英文标题如 `IR Generation`）。
- `hyphenated`：把连续空白替换成单个 `-`，模拟「空格转连字符」的常见锚点规则。
- `compact`：用正则 `[^\wU+0080-U+FFFF\s-]` 删掉既非「单词字符」、也非「中日韩等高字节字符」、也非「空白/连字符」的标点（例如 `词法/语法分析` 里的 `/` 会被删掉，变成 `词法语法分析`）。

[expand_anchor_forms](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L402-L410) 给每个字符串再做「URL 维度」的扩展：

```python
def expand_anchor_forms(values: set[str]) -> set[str]:
  result: set[str] = set()
  for value in values:
    if not value:
      continue
    decoded = unquote(unicodedata.normalize('NFC', value))
    result.update({value, decoded, decoded.lower(),
                  quote(decoded, safe='-_.~')})
  return result
```

于是每个基础变体又裂变成 4 个：原值、`unquote` 解码值、解码后小写、`quote(safe='-_.~')` 编码值。最终一个标题会得到一簇（最多 16 个）等价写法。

标题文字怎么取？[inline_text](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L396-L399) 把 inline token 的子节点（`text` / `code_inline` / `image`）内容拼起来，保留了行内代码（如标题 `## `int` 类型` 里的 `int`）：

```python
def inline_text(token: Token) -> str:
  if not token.children:
    return token.content
  return ''.join(child.content for child in token.children
                if child.type in {'code_inline', 'text', 'image'})
```

[collect_local_anchors](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L431-L442) 是收集入口，Markdown 路径下扫描「`heading_open` 紧跟 `inline`」的标题对，再叠加 HTML 锚点：

```python
def collect_local_anchors(path, file_types):
  text = path.read_text(encoding='utf-8')
  anchors: set[str] = set()
  if file_types.is_html(path):
    anchors.update(parse_html_anchors(text))
  elif file_types.is_markdown(path):
    tokens = MARKDOWN.parse(text)
    for index, token in enumerate(tokens[:-1]):
      if token.type == 'heading_open' and tokens[index + 1].type == 'inline':
        anchors.update(anchor_forms(inline_text(tokens[index + 1])))
    anchors.update(parse_html_anchors(text))   # Markdown 内嵌 HTML 的 id/name
  return expand_anchor_forms(anchors)
```

[parse_html_anchors](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L421-L428) 用 `selectolax` 抓所有带 `id` 或 `name` 属性的节点：

```python
def parse_html_anchors(text: str) -> set[str]:
  anchors: set[str] = set()
  for node in HTMLParser(text).css('[id], [name]'):
    for attr in ('id', 'name'):
      value = node.attributes.get(attr)
      if value:
        anchors.add(value)
  return anchors
```

注意最后 `collect_local_anchors` 返回前又整体过了一次 `expand_anchor_forms`，所以无论标题在收集时是哪种变体，最终输出集合都已涵盖「编码/小写」维度——这就是超集匹配的底气。

#### 4.3.4 代码实践（源码阅读型 + 选做动手）

**实践目标**：理解「多变体 + 超集匹配」为何能容忍写法差异。

**操作步骤**：

1. 取真实标题 `词法/语法分析`（[docs/lv1-main/structure.md:25](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/structure.md#L25) 的 `## 词法/语法分析`），手工推导 `anchor_forms` 的 4 个基础变体：
   - `normalized` = `词法/语法分析`
   - `lower` = `词法/语法分析`
   - `hyphenated` = `词法/语法分析`（无空格，不变）
   - `compact` = ?（提示：`/` 会被 `compact` 正则删掉）
2. **选做（需已 `pip install -r requirements.txt`）**：在仓库根目录运行
   `python3 -c "import sys; sys.path.insert(0,'scripts'); from check_links import anchor_forms; print(sorted(anchor_forms('词法/语法分析')))"`，
   核对你推导的变体是否都在输出集合里。

**需要观察/预期结果**：

- `compact` 变体应为 `词法语法分析`（`/` 被删除）。
- 这意味着：链接里写 `?id=词法/语法分析` 或 `?id=词法语法分析` 都能命中同一标题——前者命中 `normalized/hyphenated` 那一簇，后者命中 `compact` 那一簇。
- `expand_anchor_forms` 还会额外补上 URL 编码（`quote`，`safe='-_.~'`）和解码（`unquote`）形式，所以中文锚点无论是否被百分号编码都能匹配。
- 第 2 步的确切输出字符串（含百分号编码的具体字节）**待本地验证**——可重点关注集合中是否同时包含「带 `/`」与「不带 `/`」两类写法。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `anchor_forms` 里 `compact` 变体要用正则 `[^\wU+0080-U+FFFF\s-]`，而不是简单地 `[^\w\s-]`？

> **答案**：`\w` 默认虽含 Unicode 字母，但保险起见显式保留 `U+0080-U+FFFF`（涵盖中日韩等非 ASCII 字符），确保像「词法」这样的汉字**不会**被当成标点删掉，只删真正无意义的标点（如 `/`、`：`、`，`）。否则中文标题会被错误地清空。

**练习 2**：`collect_local_anchors` 已经对每个标题调用了 `anchor_forms`（内部含 `expand_anchor_forms`），为何函数末尾还要对整个集合再 `expand_anchor_forms` 一次？

> **答案**：`parse_html_anchors` 抓到的 `id`/`name` 是**原始属性值**，没有经过任何归一化；对整个集合统一再扩一次，保证 HTML 锚点也享有「解码/小写/编码」的等价簇，匹配口径与 Markdown 标题完全一致。

---

### 4.4 本地错误判定

#### 4.4.1 概念说明

前三节是「零件」，本节的 `check_local_link` 是「总装」：把候选规则（4.1/4.2）和锚点匹配（4.3）拼起来，对一条 `Link` 给出最终结论——**通过**，或 **`local-missing-target`**（目标文件不存在），或 **`local-missing-anchor`**（文件在、但锚点不在）。

这两类错误分别对应两种最常见的「坏链」：一是链接指向了一个根本不存在的页面（笔误、删页未同步），二是页面在、但 `?id=` 指向的标题被改名或删除了。分开报错，作者能立刻知道是该补文件还是该改锚点。

#### 4.4.2 核心流程

```text
check_local_link(link):
  candidates, anchor = docsify_candidates(...)        # 4.1+4.2
  target = 候选里第一个真实存在的文件
  ├─ 没有 target          → Issue(local-missing-target, "tried <所有候选>")
  ├─ 有 anchor 且
  │   target 的锚点集 与 anchor 簇 不相交  → Issue(local-missing-anchor, "anchor ... not found in <target>")
  └─ 否则                 → None（通过）
```

判断「锚点是否匹配」用集合的 `isdisjoint`：两个集合**没有**任何公共元素时返回 `True`。所以「不相交（disjoint）= 没匹配上 = 报缺失锚点」。即

\[
\text{collect\_local\_anchors}(target) \cap \text{expand\_anchor\_forms}(\{anchor\}) = \emptyset \;\Rightarrow\; \text{报 local-missing-anchor}
\]

#### 4.4.3 源码精读

[check_local_link](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L492-L504) 只有十几行，却是本地校验的最终裁判：

```python
def check_local_link(root, docs_root, link, file_types):
  candidates, anchor = docsify_candidates(
      root, docs_root, link.source, link.url, file_types)
  target = next(
      (candidate for candidate in candidates if candidate.exists()), None)
  if target is None:
    tried = ', '.join(candidate.relative_to(root).as_posix()
                      for candidate in candidates)
    return Issue(link, 'local-missing-target', f'target does not exist; tried {tried}')
  if anchor and collect_local_anchors(target, file_types).isdisjoint(expand_anchor_forms({anchor})):
    rel_target = target.relative_to(root).as_posix()
    return Issue(link, 'local-missing-anchor', f'anchor "{unquote(anchor)}" not found in {rel_target}')
  return None
```

关键点：

- **`next(..., None)` 取首个存在文件**：候选列表是有优先级的（page 优先于 index，见 4.1.3 的 ⑤ 分支顺序），`candidate.exists()` 在磁盘上真实探测，取第一个命中者作为 `target`。
- **`local-missing-target` 的报错列出 `tried`**：把**所有**候选的相对路径拼进消息，作者据此能看出「检查器到底找了哪些文件」，便于定位是拼写错了还是文件漏建。这正是本讲综合实践要观察的重点。
- **`local-missing-anchor` 用 `isdisjoint`**：左侧是目标文件的全量归一化锚点集（4.3），右侧是链接锚点的等价簇（`expand_anchor_forms({anchor})`）；二者不相交才报错。`unquote(anchor)` 把锚点解码后显示，便于阅读中文锚点。
- **`if anchor and ...`**：没有锚点的链接只校验文件存在性，跳过锚点判定。

它在主流程里的位置见 [check_links 主循环](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L670-L683)：遍历所有链接，远程链接攒批异步校验，本地链接逐条调用 `check_local_link`；产出的 `Issue` 最终经 [print_issues](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L700-L708) 以 `文件:行:列` + `category: message` 格式打印。

#### 4.4.4 代码实践（动手型，本讲主实践）

**实践目标**：制造一个不存在的页面链接，亲眼看到 `local-missing-target` 报错，并读懂 `tried` 列出的候选路径。

**操作步骤**：

1. 确保已安装依赖：`pip install -r requirements.txt`。
2. 临时在某个会被扫描的 Markdown 末尾加一行坏链，例如追加到 `docs/toc.md`：
   ```markdown
   * [测试坏链](/lv99-none/)
   ```
3. 运行检查器（用 `--no-http` 跳过远程校验、`--verbose` 看进度）：
   ```bash
   python3 scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
4. 在输出里找到含 `lv99-none` 的报错行，读出 `tried` 后列出的候选路径。
5. 实验结束后**删掉**刚才加的那一行，恢复 `docs/toc.md`。

**需要观察/预期结果**（按源码逻辑推导，确切格式**待本地验证**）：

- `/lv99-none/` 以 `/` 结尾 → 命中 4.1.3 的 ③ 分支，候选为 `markdown_index_candidates` + `html_index_candidates`，即 `docs/lv99-none/README.md` 与 `docs/lv99-none/index.html`。
- 两者都不存在 → `target is None` → 报 `local-missing-target`。
- 报错信息形如：
  ```text
  - docs/toc.md:<行号>:<列号>: /lv99-none/
    local-missing-target: target does not exist; tried docs/lv99-none/README.md, docs/lv99-none/index.html
  ```
- **解释**：`tried` 列出的正是 4.2 表格中「目录路由」那一行的候选。检查器把 Docsify 的「目录 → README/index」约定翻译成了具体文件名，逐一探测，都找不到才下「目标缺失」的结论。

**进阶观察**：把坏链换成无后缀形式 `/lv99-none`（去掉结尾斜杠），重跑一次，预期 `tried` 会变成**四个**候选（`lv99-none.md`、`lv99-none/README.md`、`lv99-none.html`、`lv99-none/index.html`）——这就印证了 4.1.3 的 ⑤ 分支「无后缀路由试四件套」。

#### 4.4.5 小练习与答案

**练习 1**：一条链接 `/lv1-main/structure?id=不存在的锚点`，目标文件 `docs/lv1-main/structure.md` 确实存在，但里面没有「不存在的锚点」这个标题。会报哪类错？为什么不是 `local-missing-target`？

> **答案**：报 `local-missing-anchor`。因为 `target` 找得到（`structure.md` 存在），`target is None` 不成立；随后 `anchor` 非空，且 `collect_local_anchors(target)` 与 `expand_anchor_forms({'不存在的锚点'})` 不相交，故落到锚点缺失分支。

**练习 2**：为什么 `check_local_link` 对候选用「取第一个存在者」而非「全部存在才算通过」？

> **答案**：Docsify 路由本身允许多种写法命中同一资源，候选列表只是「可能的落点」。只要**任意一个**候选真实存在，链接就是有效的；要求全部存在反而会把合法链接误判为坏链。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「从坏链到定位」的完整排查：

**任务**：仓库里有一条已知的合法带锚点链接 `/misc-app-ref/koopa?id=符号名称`（出现在 `docs/lv1-main/ir-gen.md:91`）。请你做三件事，逐步破坏它并观察检查器的反应，从而验证 4.1～4.4 的判定逻辑。

**步骤**：

1. **基线**：运行 `python3 scripts/check_links.py --config check-links.toml --no-http --verbose`，确认当前 `misc-app-ref/koopa` 相关链接全部 PASS（无误报）。
2. **破坏锚点**：把 `ir-gen.md:91` 的 `?id=符号名称` 临时改成 `?id=不存在的锚点`，重跑检查器。预期出现 `local-missing-anchor`，且消息里 `not found in docs/misc-app-ref/koopa.md`。解释：为什么此时 `tried` 不会出现在消息里？（提示：目标文件存在，没走 `local-missing-target` 分支。）
3. **破坏目标**：把链接改成指向不存在的页面 `/misc-app-ref/koopa99?id=符号名称`，重跑。预期出现 `local-missing-target`，`tried` 列出四个候选（`misc-app-ref/koopa99.md`、`misc-app-ref/koopa99/README.md`、`misc-app-ref/koopa99.html`、`misc-app-ref/koopa99/index.html`）。
4. **收尾**：用 `git checkout docs/lv1-main/ir-gen.md` 还原改动，重跑确认零误报。

**预期结果与反思**：

- 第 2 步证明「文件在、锚点不在」→ `local-missing-anchor`；第 3 步证明「文件不在」→ `local-missing-target`，二者由 `check_local_link` 里 `target is None` 的先后判断清晰区分。
- 第 3 步的四个候选正好对应 4.1.3 的 ⑤ 分支 + 4.2 的四件套，说明检查器的报错信息本身就是一份「路由解析过程的审计日志」。
- 真实运行的确切行号/列号与措辞**待本地验证**，但错误类别与 `tried` 候选集合是源码逻辑的确定性结论。

## 6. 本讲小结

- `docsify_candidates` 是「路由 → 文件」的翻译器，按路径的四种形态（空 / 目录 `/` / 带后缀 / 无后缀）返回不同候选集，并把 `#fragment` 或 `?id=` 解析成锚点。
- 绝对路由挂 `docs_root`、相对路由挂源文件目录，最后用 `os.path.normpath` 归一，杜绝 `../` 绕过。
- 四个 `*_candidates` 辅助函数用 `with_suffix`（页面）与 `/ README`（目录）两套约定，对 `.md`/`.html` 扩展名集合展开；目录路由只试 index，无后缀路由试 page+index 四件套。
- 锚点校验采用「超集匹配」：`anchor_forms` 造 4 个基础变体，`expand_anchor_forms` 再各扩成编码/小写 4 种，标题与链接两端都展开后取交集判定。
- `check_local_link` 总装：取首个存在的候选为 `target`，无 target 报 `local-missing-target`（带 `tried` 审计），有 target 但锚点不相交报 `local-missing-anchor`。
- 本地两类错误对应两种坏链成因，分开报错让作者一眼知道「补文件」还是「改锚点」。

## 7. 下一步学习建议

本讲完成了「本地侧」校验。下一步进入 [u4-l4 远程链接的异步校验与片段验证](u4-l4-async-remote-check.md)，看检查器如何处理 `http(s)` 开头的远程链接：`asyncio` 并发与信号量限流、先 HEAD 后 GET 的回退、远程 HTML 锚点验证，以及 `evaluate_remote_response` 为何把 403 判为通过。读完 u4-l4 后，[u4-l5 忽略规则、主流程编排与 CI 集成](u4-l5-ignore-rules-and-ci.md) 会把本地与远程两类 `Issue` 用忽略规则做二次过滤，并接入 GitHub Actions。建议结合本讲的 `local-missing-target`/`local-missing-anchor` 两类 category，去 [u4-l5](u4-l5-ignore-rules-and-ci.md) 的 `[[ignore]]` 规则里体会 `category` 字段如何精确「静音」某一类错误。
