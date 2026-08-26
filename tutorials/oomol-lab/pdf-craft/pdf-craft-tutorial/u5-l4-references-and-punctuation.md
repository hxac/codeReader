# 脚注引用与标点归一化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Mark` 数据结构如何用「类别 + 数字」抽象十几种脚注编号风格，以及 `①` 与 `➀` 为何被视为同一个标记。
2. 追踪 `References` 生成器把脚注区文本流切分为一条条脚注的完整过程，理解「标记开头开新条目」的状态机。
3. 解释正文中的脚注标记如何被替换为 `Reference` 对象，以及 `chapter_N.xml` 中为什么正文只留 `<ref id="..."/>` 指针、脚注内容统一挂在 `<references>` 区。
4. 理解 `normalize_punctuation_in_chapter` 的全角化规则（左邻汉字 / 两侧汉字），并能参照现有测试写出自己的断言测试。

## 2. 前置知识

- **脚注（footnote）与引用标记（mark）**：书籍排版中，正文中会出现一个小编号（如 `①`、`[3]`、`*`），页面底部对应编号处给出注释全文。正文里的编号叫「标记」，底部的注释条目叫「脚注」。pdf-craft 要做的就是把这两者重新关联起来——OCR 只能分别认出正文文本和页脚文本，关联关系得靠程序重建。
- **Unicode 编号字符**：除了 ASCII 数字，Unicode 里存在大量「装饰过的数字」：带圈数字 `①②③`、黑圈数字 `❶❷`、罗马数字 `Ⅰ Ⅱ`、全角数字 `１２３`、甚至数学粗体 `𝟭𝟮`。不同出版社、不同语言的书用不同风格标记脚注。
- **全角与半角标点**：中文排版使用全角标点（`，；：？！`，每个占一个汉字宽度），英文使用半角（`,;:?!`）。OCR 引擎在中文书里经常把全角标点误识别为半角，导致排版难看。修复这一问题是 `punctuation.py` 的使命（对应 [issue #310](https://github.com/oomol-lab/pdf-craft/issues/310)）。
- **前置讲义回顾**：u5-l1 讲过 `Reference` 数据类以 `(page_index, order)` 为 id、多处引用共享同一实例、XML 编码为「两遍式解码 + 收集式编码」；u5-l3 讲过 `generate_chapter_files` 的章节切分流程，本讲展开其中「脚注关联」与「标点归一化」两个环节在写盘前的确切位置与实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/extractor/chapter/mark.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py) | 定义 `Mark` 数据类与 Unicode 编号字符总表，提供单字符识别 `transform2mark` 与文本切分 `search_marks` |
| [pdf_craft/extractor/chapter/reference.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py) | `References` 生成器：把一页脚注区布局流按「标记开头」切分为一条条 `Reference` |
| [pdf_craft/extractor/chapter/punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py) | `normalize_punctuation_in_chapter`：把中文语境中的半角标点归一化为全角 |
| [pdf_craft/extractor/chapter/generation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py) | 调用方：组装正文/脚注两个 Jointer、按页匹配 References、替换标记、写盘前归一化 |
| [pdf_craft/extractor/chapter/chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py) | `Reference` 数据类定义、`search_references_in_chapter` 收集器、`encode/decode` 中 `<references>` 区的读写 |
| [pdf_craft/extractor/chapter/content.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py) | `expand_text_in_content` 文本展开工具：标记替换与标点归一化都靠它遍历 Content |
| [tests/test_punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_punctuation.py) | 标点归一化的 8 个单元测试，是本讲实践的模板 |
| tests/assets/citation.pdf | 仓库自带的含脚注测试 PDF，用于端到端实践 |

## 4. 核心概念与源码讲解

### 4.1 标记匹配：Mark 与 Unicode 编号总表

#### 4.1.1 概念说明

要建立「正文标记 ↔ 脚注条目」的对应，第一步是回答：**什么算一个标记？**

pdf-craft 的答案是一张硬编码的 Unicode 编号字符总表。`mark.py` 用两个枚举描述一个编号字符的两面：

- `NumberClass`（类别）：决定**相等性**——两个标记是否指同一个东西；
- `NumberStyle`（风格）：描述**字形外观**——带圈、黑圈、全角、数学粗体等。

`Mark` 数据类持有 `number`（数值）、`char`（原始字符）、`clazz`、`style` 四个字段。关键设计在相等性：`__eq__` 与 `__hash__` 只比较 `(clazz, number)`，完全忽略 `char` 和 `style`。

#### 4.1.2 核心流程

单字符识别与文本切分的流程：

```text
transform2mark("①")
  → 查 _number_marks.marks 字典（字符 → Mark）
  → 命中：返回 Mark(number=1, char="①", clazz=CIRCLED_NUMBER, style=CIRCLED_NUMBER)
  → 未命中（普通汉字/字母）：返回 None

search_marks("实验见①与❷")
  → re.split(字符类正则, text)   # 分隔符（编号字符）因捕获组保留在结果中
  → 逐段判断：能转 Mark 的产出 Mark 对象，其余产出原字符串
  → 产出流：["实验见", Mark(①), "与", Mark(❷), ""]
```

相等性设计带来的容错：正文的 `①`（带圈数字）与脚注开头的 `➀`（带圈无衬线数字）属于同一 `NumberClass` 且数值相同，因此 `Mark(①) == Mark(➀)` 成立——OCR 对上标小字号的字形风格识别不稳定时，关联依然能建立。反之 `①` 与 `❶` 类别不同（黑圈），不相等。

#### 4.1.3 源码精读

`Mark` 数据类与它的相等性定义：

- [pdf_craft/extractor/chapter/mark.py:35-55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L35-L55) — `Mark` 持有 `number/char/clazz/style`；`__str__` 返回原始字符（序列化用）；`__hash__` 与 `__eq__` 只看 `(clazz, number)`，使不同字形的同号标记可互相匹配。

单字符识别函数：

- [pdf_craft/extractor/chapter/mark.py:74-80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L74-L80) — `transform2mark` 用字典完成 O(1) 查表，未命中返回 `None`，调用方据此区分「标记」与「普通文本」。

文本切分生成器：

- [pdf_craft/extractor/chapter/mark.py:83-89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L83-L89) — `search_marks` 用 `re.split` 把文本切成「普通片段 / 单个编号字符」交替的流，普通片段原样 `yield`，编号字符转成 `Mark` 再 `yield`。

总表的构建：

- [pdf_craft/extractor/chapter/mark.py:92-108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L92-L108) — `_NumberMarks` 在模块导入时把所有编号字符编入三张索引：`marks`（字符 → Mark 查表）、`styles`（风格 → 字符列表，供提示词展示样例）、`pattern`（把全部字符拼成一个字符类正则，供 `re.split` 使用）。
- [pdf_craft/extractor/chapter/mark.py:112-395](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L112-L395) — 总表数据本体：罗马数字（大小写两套）、带圈数字（0–50）、双圈、无衬线、黑圈、括号汉字 `㈠`、圈汉字 `㊀`、全角数字及四种花式数字，共十余组，每组是 `(数字, 字符)` 序列。

#### 4.1.4 代码实践

这是一个纯内存操作，无需 OCR 凭据即可运行（以下为**示例代码**，输出为依据源码推导的预期结果，**待本地验证**）：

1. **实践目标**：直观感受 `search_marks` 的切分行为与 `Mark` 的相等性语义。
2. **操作步骤**：在仓库根目录执行：

```bash
python -c "
from pdf_craft.extractor.chapter.mark import search_marks, transform2mark

for item in search_marks('实验见①，另有❷'):
    print(repr(item))

m = transform2mark('①')
print(m.number, m.clazz.name, m.style.name)
print(transform2mark('①') == transform2mark('➀'))  # 同类同号 → True
print(transform2mark('①') == transform2mark('❶'))  # 类别不同 → False
print(transform2mark('甲'))                          # 非编号字符 → None
"
```

3. **需要观察的现象**：`search_marks` 输出中，普通中文片段是 `str`，编号字符变成了 `Mark(number=1, char='①', clazz=<NumberClass.CIRCLED_NUMBER: ...>, style=...)` 形式的对象。
4. **预期结果**：三次比较分别打印 `True`、`False`、`None`；切分流中 `①` 与 `❷` 都是 `Mark` 对象且 `number` 分别为 1、2。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Mark.__eq__` 不比较 `char` 和 `style`？如果比较了会发生什么？

**答案**：正文标记与脚注标记由 OCR 在不同字号（上标小字 vs 页脚正常字）下识别，同一本书里两处很可能被认成不同字形（如 `①` 与 `➀`）。相等性只锚定「类别 + 数值」就能容忍这种风格漂移；若把 `char` 纳入相等性，风格稍异就匹配失败，脚注会整体丢失关联。

**练习 2**：`search_marks` 依赖 `re.split(_number_marks.pattern, text)` 且模式带捕获组。如果去掉捕获组会发生什么？

**答案**：`re.split` 在模式含捕获组时会把「分隔符本身」（即编号字符）也放进结果列表；去掉捕获组后结果只剩普通文本片段，所有编号字符凭空消失——既无法产出 `Mark`，文本也被破坏。这里捕获组是切分器能工作的前提。

---

### 4.2 引用收集：References 生成器

#### 4.2.1 概念说明

`reference.py` 的 `References` 类解决的问题是：**把一页脚注区的布局流，按「标记开头」切成一条条独立的脚注**。

u3-l4 讲过，OCR 驱动器把每页识别结果分成 `body_layouts`（正文区）与 `footnotes_layouts`（脚注区）；u5-l2 讲过 Jointer 把它们各自合并成连续的布局流。脚注流进入本模块后长这样（示意）：

```text
[块: "① 这是第一条脚注的文本..."]
[块: "② 这是第二条脚注..."]      ← 同一段落里混了多条脚注
[AssetLayout: 某脚注引用的图片]
```

`References` 的工作就是扫描每个块的**开头**：碰到编号标记（`①`、`Ⅰ` 等）或星号前缀（`**`），就认为上一条脚注结束、新脚注开始；其余布局统统归入当前脚注的 `layouts`。

#### 4.2.2 核心流程

```text
输入：一页脚注区的布局流
  │
  ├─ _iter_and_inject_marks：逐布局扫描
  │    ├─ AssetLayout → 原样透传（挂到当前脚注）
  │    └─ ParagraphLayout → _split_paragraph_by_marks
  │         对每个 block 调 _extract_head_mark：
  │           · 开头是 "*{1,6} " 星号前缀 → mark = 前缀字符串
  │           · 开头是编号字符（transform2mark 命中）→ mark = Mark
  │           · 其余 → mark = None，块归入前导/当前段
  │         有 mark 的块把段落一分为二，产出 (mark, 新段落)
  │
  └─ _extract_references 状态机：
       遇到 Mark|str（标记）→ 先产出累积中的 reference，再开新条目（order 从 1 递增）
       遇到 layout        → 追加到当前条目的 layouts
       开头无标记的 layout → 丢弃（TODO：跨页脚注，暂无解法）
       结束时产出最后一条
```

构造完成后建立 `mark → Reference` 索引（先到先得），供正文查询用 `get(mark)`。

#### 4.2.3 源码精读

星号前缀正则与类入口：

- [pdf_craft/extractor/chapter/reference.py:8](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L8) — `_START_PREFIX_PATTERN` 匹配块开头的 1–6 个星号加空白，覆盖 `* 注`、`** 注` 这类非编号式脚注标记。
- [pdf_craft/extractor/chapter/reference.py:12-23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L12-L23) — 构造函数提取本页全部脚注并建立 `_mark2reference` 字典；注意循环里的 `if mark not in ...`——同一标记重复出现时保留第一条（页内重复编号通常意味着识别噪声）。

核心状态机：

- [pdf_craft/extractor/chapter/reference.py:32-55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L32-L55) — `_extract_references`：`Mark | str` 是「新条目」信号，`layout` 是「内容」信号；开头就遇到 layout（还没有任何标记）时进入 TODO 分支直接丢弃——源码注释明确承认这可能是上一页跨页脚注，暂无判断能力。
- [pdf_craft/extractor/chapter/reference.py:57-65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L57-L65) — `_iter_and_inject_marks` 把「布局流」改写成「标记与布局交替的流」：段落先经下一函数切分，切出的标记以独立元素插入流中。

段落切分与开头标记提取：

- [pdf_craft/extractor/chapter/reference.py:67-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L67-L99) — `_split_paragraph_by_marks` 逐块检查开头标记：无标记的块累积到当前子段落；有标记的块触发切段，并新建一个以「去掉标记后的剩余文本」为首个 block 的子段落。注意剩余部分会重建为新的 `BlockLayout`，保留原块的 `page_index/order/det` 溯源信息。
- [pdf_craft/extractor/chapter/reference.py:101-126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L101-L126) — `_extract_head_mark` 只看 `content` 的第一个元素且要求是字符串：先试星号前缀，再试首字符 `transform2mark`；剩余文本 `rest` 非空则替换首元素、为空则整段首文本被完全消耗（`new_content = content[1:]`）。

**正文侧的匹配与替换**（调用方在 generation.py）：

- [pdf_craft/extractor/chapter/generation.py:104-126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L104-L126) — 脚注 Jointer 的产出经 `_extract_page_references` 按页分组，每组构造一个 `References`；`get_references` 是一个游标函数，随正文块页码推进同步前进，取到「当前页」的脚注集合。
- [pdf_craft/extractor/chapter/generation.py:173-187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L173-L187) — `_replace_mark_with_reference`：对正文每个块的文本跑 `search_marks`，每个 `Mark` 查 `references.get`，命中就把文本中的编号字符**原地替换为 `Reference` 对象**；未命中则保留原文（`yield str(item)`）。替换发生在 [generation.py:128-134](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L128-L134) 的正文循环里，随后 `join_texts_in_content` 把碎片文本重新粘合。
- [pdf_craft/extractor/chapter/content.py:42-56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L42-L56) — `expand_text_in_content` 是替换得以发生的底层工具：深度遍历 Content（含 `HTMLTag` 内部），把每个字符串元素删掉、用 `expand` 回调的产出序列原位插回。标记替换与 4.3 的标点归一化共用这一机制。

**引用为何不写入正文而挂到 references 区**：

- [pdf_craft/extractor/chapter/chapter.py:68-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L68-L75) — `search_references_in_chapter` 深搜整章正文（`flatten` 展开 HTMLTag），按 `(page_index, order)` 去重地收集所有 `Reference` 实例——同一脚注被正文引用 N 次也只收集一次。
- [pdf_craft/extractor/chapter/chapter.py:135-142](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L135-L142) — `encode` 在正文写完后，把收集到的引用按 id 排序、集中写入根元素下的 `<references>` 区；而正文块里只留 `<ref id="页码-序号"/>` 空元素（见 [chapter.py:419-422](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L419-L422)）。本质是**指针 + 表**的存储结构：正文是轻量指针，脚注全文只在 `<references>` 区出现一次，同一引用多处共享、不重复存储；解码时（[chapter.py:85-91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L85-L91)）先建 id → Reference 映射再解正文，指针还原为共享实例。
- [pdf_craft/extractor/chapter/chapter.py:428-442](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L428-L442) — `_encode_reference`：`<ref>` 元素带 id 属性，内含 `<mark>` 子元素（标记原文）与若干 `<paragraph>`/`<asset>`（脚注正文布局）。

#### 4.2.4 代码实践

1. **实践目标**：端到端观察「正文 `<ref>` 指针 + `<references>` 表」结构，验证标记匹配真的发生了。
2. **操作步骤**：
   - 准备好 OCR 凭据（u1-l2 的方式，`PDFOptions(ocr=DeepSeekOCRVendorConfig(...))`）。
   - 写脚本（**示例代码**）：

   ```python
   from pdf_craft import PDFCraft, PDFOptions
   from pdf_craft.ocr_config import DeepSeekOCRVendorConfig
   from pathlib import Path

   craft = PDFCraft()
   craft.extract_pdf(
       pdf_file_path=Path("tests/assets/citation.pdf"),
       analysing_path=Path("./citation_package"),
       options=PDFOptions(
           ocr=DeepSeekOCRVendorConfig(
               base_url="...", api_key="...", model="..."
           ),
       ),
   )
   ```

   - 打开产物 `citation_package/chapters/` 下的 `chapter_N.xml`，在正文块中搜索 `<ref id=`，记下其 id；再到文件末尾 `<references>` 区找同 id 的 `<ref>`，查看其 `<mark>` 与脚注段落文本。
   - 也可以不写脚本，直接用仓库 CLI：配置 `.env` 后执行 `python -m pdf_craft_tool pdf extract`（具体参数见 `pdf_craft_tool/cli.py`，u11-l1 会详讲）。
3. **需要观察的现象**：正文中的编号字符（如 `①`）已不在文本里，原位置变成 `<ref id="3-1"/>` 这样的空元素；`<references>` 区的对应条目里 `<mark>` 保存着编号原文，后面跟着脚注的完整段落。
4. **预期结果**：`citation.pdf` 是仓库专门用于测试脚注关联的资产（同族还有 `citation_large.pdf`），正常情况下至少能找到一个「正文指针 ↔ references 条目」配对。实际产物内容取决于 OCR 服务，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一条脚注的正文太长跨到了下一页，下一页脚注区的第一个块以普通文本开头（没有标记）。按当前实现会发生什么？

**答案**：该块落在 `_extract_references` 的 `else` 分支被**丢弃**——[reference.py:50-53](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reference.py#L50-L53) 的 TODO 注释明确说明：无法判断它是上一页脚注的延续还是应忽略的多余内容，目前直接舍弃。这是当前实现的已知局限。

**练习 2**：为什么 `References` 是**按页**构造的（`generation.py` 里每组布局属于同一 `page_index`），而不是全书一个？

**答案**：脚注编号通常每页重新从 ① 开始（或每章重新计数），只有同一页内的「标记 → 条目」对应才是无歧义的。按页构造让 `mark2reference` 的键空间天然隔离；同时正文匹配用的是「当前块所在页」的 References（`get_references(block.page_index)`），页码错位时宁可不替换也不误挂。

**练习 3**：正文里出现 `①` 但该页脚注区没有对应条目时，读者会在 Markdown 里看到什么？

**答案**：`references.get(item)` 返回 `None`，走 [generation.py:182](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L182) 的 `yield str(item)`——编号字符原样留在正文中，不产生引用。这是「宁缺毋错」的降级策略。

---

### 4.3 标点归一化：normalize_punctuation_in_chapter

#### 4.3.1 概念说明

OCR 引擎（尤其是面向多语言的模型）常把中文书里的全角标点识别成半角 ASCII：`他说,这很重要` 应为 `他说，这很重要`。`punctuation.py` 在章节写盘前做一遍定向修复。

修复不是「无差别替换」——英文内容里的 `,` `;` `:` 必须保持半角，否则 `hello, world` 会变成 `hello，world`。规则以**邻接字符是否为汉字**为判据：

| ASCII | 全角 | 规则 |
| --- | --- | --- |
| `,` `;` `?` `!` | `，` `；` `？` `！` | 左侧最近非空白字符是汉字 → 替换 |
| `:` | `：` | 左右**两侧**最近非空白字符都是汉字 → 替换 |

冒号要求更严，因为 `中文: English` 这类「中文标签 + 英文值」的写法（定义列表、配置示例）在技术书里很常见，单侧判据会误伤。

#### 4.3.2 核心流程

```text
normalize_punctuation_in_chapter(chapter)
  ├─ 归一化 chapter.layouts            （正文）
  └─ 对 search_references_in_chapter 的每个 Reference
       归一化 ref.layouts              （脚注内容也不能漏）

_normalize_layouts:
  ParagraphLayout → 逐 block 归一化 content
  AssetLayout     → 只归一化 title 与 caption（content 是表格 HTML，不动）

_normalize_content（两遍法）:
  第一遍：expand_text_in_content 收集 Content 内全部字符串段 segments
          （HTMLTag 内部文本也在收集范围，展开顺序 = 后续替换顺序）
  计算：_normalize_segments(segments)
          · 把所有段拼成跨段全局字符数组 full_chars，并记录每个字符的 (段号, 段内下标)
          · 对每个目标标点查左邻（跳过空白）：
              左邻非汉字 → 跳过
              属于冒号表 → 还要查右邻，右邻不存在或非汉字 → 跳过
          · 命中则写回该字符所属段的字符列表，标记 changed
          · 未发生任何改动 → 返回 None（短路，省掉第二遍）
  第二遍：expand_text_in_content 按收集顺序逐段替换回归一化后的文本
```

「跨段全局视角」是关键设计：文本被 `Reference`、`InlineExpression`、`HTMLTag` 切成多个字符串段后，`好</b>,世界` 中 `,` 的左邻 `好` 在**另一个段**里。把全部段拼成一个字符数组再查邻居，边界两侧的标点也能正确处理。

#### 4.3.3 源码精读

- [pdf_craft/extractor/chapter/punctuation.py:4-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L4-L13) — 两张映射表：`_LEFT_ONLY_ASCII_TO_FULLWIDTH`（逗号/分号/问号/叹号，只查左侧）与 `_BOTH_SIDES_ASCII_TO_FULLWIDTH`（冒号，两侧都查）。
- [pdf_craft/extractor/chapter/punctuation.py:17-21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L17-L21) — 入口函数：先处理正文布局，再借 4.2 讲过的 `search_references_in_chapter` 复用同一套收集逻辑处理每条脚注的布局；函数就地修改并返回原 chapter。模块注释标明这是为修复 issue #310。
- [pdf_craft/extractor/chapter/punctuation.py:24-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L24-L31) — `_normalize_layouts`：段落走 `block.content`；资源走 `title` 与 `caption` 但**跳过 `content`**——表格 HTML 里的标点属于原始内容，保持原样（对应测试 `test_skip_asset_content_but_normalize_caption`）。
- [pdf_craft/extractor/chapter/punctuation.py:34-61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L34-L61) — `_normalize_content` 的两遍法：第一遍的 `expand` 回调只收集不修改；若 `_normalize_segments` 返回 `None`（无改动）直接返回，避免第二遍白跑；第二遍用 `nonlocal segment_index` 游标按收集顺序逐段替换——两遍的遍历顺序由同一个 `expand_text_in_content` 保证一致。
- [pdf_craft/extractor/chapter/punctuation.py:64-102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L64-L102) — `_normalize_segments`：`full_chars` + `owners` 把多段文本摊平成全局字符数组并保留回写坐标；逐字符判定时先用 `_search_near_char` 找左邻，再按两张表分流（左邻非汉字跳过；冒号额外要求右邻也是汉字）；命中写回 `segment_chars`。
- [pdf_craft/extractor/chapter/punctuation.py:105-116](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L105-L116) — `_search_near_char`：向左/向右找最近的**非空白**字符，找不到（到了整章开头/结尾）返回 `None`。跳过空白意味着 `中文, 中文`、`中文，\n中文` 这类间隔不影响判定。
- [pdf_craft/extractor/chapter/punctuation.py:119-130](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/punctuation.py#L119-L130) — `_is_han_char`：按码点区间判断汉字，覆盖 CJK 统一表意文字及扩展 A/B/C/D/E/F/G。注意**不含**中文标点、假名、谚文——全角标点本身不会被当成「汉字」邻居。

调用位置（衔接 u5-l3 的章节生成流程）：

- [pdf_craft/extractor/chapter/generation.py:38-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L38-L42) — 每章写盘前依次执行 `normalize_punctuation_in_chapter`（本讲）与 `analyse_chapter_internal_levels`（u5-l3），然后 `encode` 落盘。归一化在**引用替换之后**进行，所以脚注布局虽然已从正文流摘出、挂在 `Reference` 对象上，仍会被入口函数的第二步覆盖到。

#### 4.3.4 代码实践

1. **实践目标**：参照现有测试风格，为归一化规则补充自己的断言，验证对中英混排边界的理解。
2. **操作步骤**：
   - 阅读 [tests/test_punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_punctuation.py)，注意 `_create_paragraph` 辅助函数如何快速搭出一个含单个 block 的段落，以及测试直接从内部模块 `pdf_craft.extractor.chapter.punctuation` 导入（这些类不在公开 API 里）。
   - 新建 `tests/test_punctuation_extra.py`（**示例代码**，属于你自己的练习文件，不要提交到仓库）：

   ```python
   import unittest
   from pdf_craft.extractor.chapter.chapter import BlockLayout, Chapter, ParagraphLayout
   from pdf_craft.extractor.chapter.punctuation import normalize_punctuation_in_chapter


   def make_paragraph(text: str) -> ParagraphLayout:
       return ParagraphLayout(
           ref="text", level=-1,
           blocks=[BlockLayout(page_index=1, order=0, det=(0, 0, 1, 1), content=[text])],
       )


   class TestExtra(unittest.TestCase):
       def test_question_mark_after_han(self):
           p = make_paragraph("对吗?")
           normalize_punctuation_in_chapter(Chapter(id=1, level=0, layouts=[p]))
           self.assertEqual(p.blocks[0].content[0], "对吗？")

       def test_fullwidth_punct_neighbor_not_han(self):
           # 左邻是右书名号(非汉字)，不应替换 —— 全角标点不算汉字
           p = make_paragraph("《书名》,正文")
           normalize_punctuation_in_chapter(Chapter(id=1, level=0, layouts=[p]))
           self.assertEqual(p.blocks[0].content[0], "《书名》,正文")

       def test_exclamation_at_chapter_start(self):
           # 左邻不存在(整章开头) —— 保持半角
           p = make_paragraph("! 开头")
           normalize_punctuation_in_chapter(Chapter(id=1, level=0, layouts=[p]))
           self.assertEqual(p.blocks[0].content[0], "! 开头")


   if __name__ == "__main__":
       unittest.main()
   ```

   - 运行：

   ```bash
   python -m pytest tests/test_punctuation_extra.py -v
   # 或
   python -m unittest tests.test_punctuation_extra -v
   ```

3. **需要观察的现象**：三个用例分别覆盖「汉字后问号替换」「左邻是全角标点（非汉字）不替换」「无左邻不替换」三条边界。
4. **预期结果**：第二条用例的断言依赖 `》` 的码点（0x300B）不在 `_is_han_char` 的任何区间内，因此 `《书名》,正文` 中的 `,` 左邻是 `》`（非汉字）→ 不替换；第三条中 `!` 位于全局字符数组首位、`_search_near_char` 返回 `None` → 跳过。三条断言依据源码推导均应通过，**待本地验证**——若某条失败，正好用失败信息反推你对区间的理解偏差。

#### 4.3.5 小练习与答案

**练习 1**：`中文, English; English,中文` 经归一化后是什么？为什么分号没有变化？

**答案**：结果是 `中文， English; English,中文`。第一个 `,` 左邻是 `文`（汉字）→ 替换为 `，`；`;` 的左邻是 `h`（英文字母）→ 跳过；末尾 `中文` 前面的 `,`（`English,中文`）左邻是 `h` → 跳过。规则只看左邻，所以「英文在左」的标点一律保留半角。这正是现有测试 `test_skip_mixed_context` 的用例。

**练习 2**：为什么 `_normalize_content` 要「先收集全部文本段、全局判定、再按顺序替换」，而不是在每个字符串段内部独立处理？

**答案**：Content 会被 `Reference`、`InlineExpression`、`HTMLTag` 切碎，`大家<b>好</b>,世界` 中 `,` 与它的汉字左邻 `好` 分属两个不同的字符串段。段内独立处理时 `,` 的左邻是段首（视为无邻居）→ 错误保留半角。全局摊平后字符数组里 `,` 的左邻恰是上一段末尾的 `好`，判定正确（现有测试 `test_convert_punctuation_across_html_tag_boundary` 验证了这一行为）。

**练习 3**：`InlineExpression`（行内公式）里的 `x,y` 会被归一化吗？

**答案**：不会。`expand_text_in_content` 只对 `isinstance(part, str)` 的元素调用 `expand`，`InlineExpression` 与 `Reference` 是非字符串元素，直接跳过。现有测试 `test_keep_inline_expression_unchanged` 验证公式内容原样保留——数学表达式中的半角逗号有语法含义，绝不能动。

## 5. 综合实践

把本讲三个模块串起来，完成一次「提取 → 解剖 → 归一化验证」：

1. **提取**：按 4.2.4 的方式对 `tests/assets/citation.pdf` 做一次提取，保留 `package_path`（如 `./citation_package`）。
2. **解剖引用链**：写一个独立脚本（**示例代码**），解析 `chapters/chapter_1.xml`（文件名以实际产物为准）：

   ```python
   from pathlib import Path
   from xml.etree.ElementTree import ElementTree

   tree = ElementTree()
   root = tree.parse(Path("citation_package/chapters/chapter_1.xml"))

   # 正文 block 中的指针
   body_refs = [
       ref.get("id")
       for block in root.find("body").iter("block")
       for ref in block
       if ref.tag == "ref"
   ]
   print("正文指针:", body_refs)

   # references 表：每条的 id 与 mark
   references_el = root.find("references")
   if references_el is not None:
       for ref_el in references_el.findall("ref"):
           mark = ref_el.find("mark")
           print(ref_el.get("id"), repr(mark.text if mark is not None else None))
   ```

   交叉核对：`body_refs` 中的每个 id 都应能在 `<references>` 区找到条目（编码时两者来自同一个收集器 `search_references_in_chapter`，正常一一对应）；观察同一 id 是否在正文中出现多次——对应同一脚注被多处引用的场景。
3. **归一化验证**：任选一条脚注的段落文本，检查其中文标点是否全为全角；再对照 4.3.4 的测试确认规则边界。
4. **闭环思考**（写进你的学习笔记）：如果注释掉 [generation.py:38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L38) 的归一化调用重新生成，`chapter_N.xml` 会有什么变化？如果注释掉 [generation.py:133](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L133) 的替换调用，`<references>` 区还会存在吗？（后者推论：不会——没有正文引用就没有收集来源，脚注区布局将被整体丢弃。此为源码推论，**待本地验证**。）

## 6. 本讲小结

- `Mark` 用 `(NumberClass 类别, number 数值)` 定义相等性、忽略字形风格，使正文的 `①` 能匹配脚注的 `➀`；`search_marks` 借带捕获组的 `re.split` 把文本切成「片段 / 标记」交替流。
- `References` 生成器以「标记开头开新条目」的状态机把一页脚注流切分为 `Reference` 列表（按页构造，order 从 1 递增），星号前缀 `*{1,6}` 是编号之外的补充标记形态；开头无标记的布局被丢弃（跨页脚注是已知局限）。
- 正文标记经 `_replace_mark_with_reference` **原地替换**为 `Reference` 对象；XML 编码采用「指针 + 表」：正文只留 `<ref id="页-序号"/>`，脚注全文集中挂在 `<references>` 区且多处引用共享一份。
- `normalize_punctuation_in_chapter` 以「邻接汉字」为判据做半角→全角修复：`,;?!` 查左邻、`:` 查两侧；通过「全局摊平字符数组」跨越 Content 被切碎产生的段边界；资源布局只处理 title/caption，公式与表格 HTML 不动。
- 归一化与标记替换共享 `expand_text_in_content` 这一底层遍历工具；归一化发生在每章写盘前的最后一步（`generation.py:38`），且额外覆盖脚注 `Reference` 自身的布局。

## 7. 下一步学习建议

本讲结束后，u5「章节生成」单元完结：你已经掌握章节数据模型、跨页合并、章节切分、脚注关联与标点归一化的全链路。接下来建议：

1. 进入 **u6-l1（DocumentPackage：中间产物契约）**：看 `chapter_N.xml` 连同 `assets/`、`toc.xml`、`document.json` 如何被打包成提取器与渲染器之间的稳定契约。
2. 顺带阅读 [pdf_craft/extractor/chapter/analyse_level.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/analyse_level.py)，补齐 u5-l3 提到的「章内层级分析」细节——它与本讲的归一化在 `generate_chapter_files` 中是相邻两步。
3. 想继续打磨脚注能力的话，可以把 4.2.5 练习 1 的跨页脚注问题当作二次开发练手场：在 `_extract_references` 的丢弃分支处尝试「若上一页最后一条脚注未闭合则并入」的启发式，并用 `citation_large.pdf` 验证效果。
