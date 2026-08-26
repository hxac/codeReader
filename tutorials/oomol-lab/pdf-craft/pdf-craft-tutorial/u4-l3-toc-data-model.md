# u4-l3 Toc 数据模型与 toc.xml 缓存

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TocInfo` 与 `Toc` 两个数据类各存什么、为什么不存标题文本。
2. 手写一个脚本，用 `ElementTree` 解析提取产物中的 `toc.xml`，按缩进打印整棵目录树。
3. 在纸上模拟 `_structure_toc_by_levels` 的栈算法，把一张扁平的「(页码, 序号) → 层级」表还原成树。
4. 解释 `toc.xml` 的缓存语义：它存在时目录分析被完全短路，但它控制的章节切分每次都会重算——因此手改 `toc.xml` 可以零成本地改变章节划分。

本讲是目录分析单元（u4）的第三讲，也是承上启下的一讲：上游两讲解决了「哪些页是目录页」和「每个条目是第几级」，本讲把这些结论固化成数据结构、落盘成 `toc.xml`，并交给下一单元（u5 章节生成）消费。

## 2. 前置知识

- **扁平层级表 `Ref2Level`**：上一讲（u4-l2）的核心产物。它是一个字典，键是 `(page_index, order)` 二元组（某页正文中第 `order` 个布局块），值是该条目的层级 `level`（0 最顶级）。它是「扁平」的——只有层级数字，没有父子关系。定义见 [pdf_craft/extractor/toc/toc_levels.py:L13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L13)：`Ref2Level = dict[tuple[int, int], int]`。
- **树与森林**：目录天然是一棵树（章包含节，节包含小节）。`TocInfo.content` 是一个列表，列表里每个顶级条目是一棵树的根，合起来构成「森林」。
- **栈（stack）与括号匹配的类比**：读入 `level 0 → level 1 → level 2` 的条目像依次打开三层括号，遇到更小或相等的 level 就像关闭括号。栈算法正是用这个直觉建树。
- **`ElementTree`**：Python 标准库的 XML 处理模块，`Element("toc")` 建元素、`elem.set(k, v)` 设属性、`elem.append(child)` 挂子元素、`fromstring`/`tostring` 做文本与对象间的转换。本讲的编解码与动手实践都基于它。
- **磁盘即契约**：回顾 u3-l1——引擎各步骤之间以磁盘文件为契约。`ocr/page_N.xml` 是 OCR 结果缓存，`toc.xml` 是目录分析结果缓存。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [pdf_craft/extractor/toc/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L1-L93) | 目录数据模型 | `TocInfo`/`Toc` 数据类、`iter_toc` 生成器、`encode`/`decode` |
| [pdf_craft/extractor/toc/analysing.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L1-L147) | 目录分析编排 | `analyse_toc` 的缓存短路、`_structure_toc_by_levels` 栈算法 |
| [pdf_craft/common/xml.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L1-L40) | XML 公共工具 | `indent` 缩进美化、`read_xml`/`save_xml`（临时文件原子写） |
| [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L66-L121) | 提取引擎主流程 | `toc_path` 的位置、`analyse_toc` 的调用点、章节生成紧随其后 |
| [pdf_craft/extractor/chapter/generation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L23-L87) | 章节生成（下游消费者，预告 u5） | `toc.xml` 缓存如何影响章节切分 |

## 4. 核心概念与源码讲解

### 4.1 目录数据结构：TocInfo 与 Toc

#### 4.1.1 概念说明

目录分析的全部产出浓缩成两个数据类：

- **`TocInfo`**：整份文档的目录信息，含两个字段——`content`（顶级条目列表，即森林）和 `page_indexes`（被判为目录页的页码列表，已排序）。
- **`Toc`**：单个目录条目，五个字段——`id`（全书唯一编号，从 1 递增）、`page_index`（标题所在页码，1 起始）、`order`（该标题在页面布局块中的序号）、`level`（层级，0 最顶级）、`children`（子条目列表）。

一个关键设计决策：**`Toc` 不存标题文本**。标题文本已经存在于 `ocr/page_N.xml` 里；`Toc` 只记录「位置坐标 `(page_index, order)` + 层级结论」。下游的章节生成用这个坐标反查正文中对应的布局块，把标题文本「就地」取回（见 4.4.3）。这样 `toc.xml` 保持极小，且永远不会与 OCR 文本不同步。

#### 4.1.2 核心流程

`iter_toc` 是访问树的统一入口，它做深度优先先序遍历（先输出自己，再递归输出每个孩子）：

```
iter_toc([A, B])          # A、B 是顶级条目
  yield A                 # 先自己
  yield from iter_toc(A.children)   # 再整棵子树
  yield B
  yield from iter_toc(B.children)
```

任何需要「平铺所有条目」的消费方（比如建立坐标索引、统计最大层级）都复用这个生成器，而不必各自写递归。

#### 4.1.3 源码精读

数据类定义——注意 `content: list["Toc"]` 与 `children: list["Toc"]` 用字符串前向引用表达递归结构：

- [pdf_craft/extractor/toc/types.py:L8-L11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L8-L11)：`TocInfo` 只有 `content` 与 `page_indexes` 两个字段。
- [pdf_craft/extractor/toc/types.py:L14-L20](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L14-L20)：`Toc` 的五个字段，全部是不可变语义的标量或列表容器。
- [pdf_craft/extractor/toc/types.py:L23-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L23-L26)：`iter_toc` 用 `yield` + `yield from` 三行实现先序遍历。

#### 4.1.4 代码实践

用 Python 交互式环境手工构造一棵小树并遍历（示例代码，不依赖任何 PDF 产物）：

```python
# 示例代码：手工构造 Toc 树并验证 iter_toc 的遍历顺序
from pdf_craft.extractor.toc.types import Toc, TocInfo, iter_toc

sec11 = Toc(id=2, page_index=3, order=1, level=1, children=[])
sec12 = Toc(id=3, page_index=5, order=3, level=1, children=[])
ch1 = Toc(id=1, page_index=3, order=0, level=0, children=[sec11, sec12])
info = TocInfo(content=[ch1], page_indexes=[7, 8])

print([t.id for t in iter_toc(info.content)])  # 预期输出 [1, 2, 3]
print(info.page_indexes)                        # 预期输出 [7, 8]
```

1. 实践目标：确认 `iter_toc` 的输出顺序是「父先于子、兄弟按列表顺序」。
2. 操作步骤：安装好 pdf-craft 的环境中运行上述片段。
3. 需要观察的现象：id 序列为 `[1, 2, 3]`，即先序遍历。
4. 预期结果：与注释一致；若改为后序遍历则应是 `[2, 3, 1]`，可用于对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Toc` 不保存标题文本，而要绕道 `(page_index, order)` 坐标？

**答案**：标题文本已在 `ocr/page_N.xml` 中，且 OCR 结果是断点续跑的缓存（u3-l3）。若 `toc.xml` 也复制一份文本，两处数据可能不一致；只存坐标则 `toc.xml` 永远是「指针 + 层级结论」，体积小且与 OCR 文本天然同步。

**练习 2**：`TocInfo.page_indexes` 存的是什么页？没有目录页时它是什么？

**答案**：存的是被 `find_toc_pages` 判定为目录页、并参与了层级分析的页码（升序）。当全书没有目录页、走「无目录页」分析路径时，`toc_page_indexes` 保持空列表（见 [pdf_craft/extractor/toc/analysing.py:L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L69)），因此 `toc.xml` 根节点上会是空的 `page_indexes` 属性。

### 4.2 XML 编解码：encode 与 decode

#### 4.2.1 概念说明

`TocInfo` 是内存对象，要落盘就必须序列化。项目没有用 JSON 或 pickle，而是选择手写 XML 编解码，原因有二：

1. **与整条链路的文件格式一致**：`page_N.xml`、`chapter_N.xml` 全是 XML，`toc.xml` 保持同族，公共工具（`read_xml`/`save_xml`/`indent`）可以复用。
2. **树形结构天然映射**：`Toc` 的嵌套孩子直接变成嵌套的 `<item>` 元素，不需要额外的「扁平化 + 还原」步骤。

XML 的形状是：根元素 `<toc page_indexes="7,8">`，每个条目一个 `<item id="1" page_index="3" order="0" level="0">` 元素，子条目作为子元素嵌套。

#### 4.2.2 核心流程

```
encode(TocInfo)                      decode(Element)
  root = <toc page_indexes="7,8">      校验根标签是 'toc'
  对 content 中每个 Toc：              解析 page_indexes（逗号分隔，空串→[]）
    递归 encode_item：                 对每个子元素递归 decode_item：
      <item id/page_index/order/level>   校验标签是 'item'
      先递归编码 children                校验四个属性齐全（缺失抛 ValueError）
      再 append 到父元素                  递归解码孩子 → 构造 Toc
  indent(root) 美化缩进              组装 TocInfo(content, page_indexes)
```

`encode_item` 的顺序值得注意：**先递归编码孩子、再把自身 append 到父元素**（[pdf_craft/extractor/toc/types.py:L41-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L41-L43)）。由于 `encode_item` 的最后一个动作才是 `parent.append(item)`，孩子元素会先于自身挂到 `item` 上，最终 XML 中孩子的缩进层级正确。

落盘侧还有两个公共工具的细节：

- `indent` 给没有文本的元素补换行和两空格缩进，让 XML 人眼可读、diff 友好。
- `save_xml` 采用**临时文件原子写**：先写 `toc.xml.tmp`，再 `replace` 覆盖目标。中途崩溃不会留下半截文件——这对「存在即缓存命中」的语义至关重要（见 4.4）。

#### 4.2.3 源码精读

- [pdf_craft/extractor/toc/types.py:L29-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L29-L38)：`encode` 把 `page_indexes` 用逗号拼成根属性，然后对每个顶级条目调用内部递归函数 `encode_item`。
- [pdf_craft/extractor/toc/types.py:L34-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L34-L43)：`encode_item` 写四个属性、先递归孩子后 `parent.append(item)`。
- [pdf_craft/extractor/toc/types.py:L50-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L50-L60)：`decode` 先校验根标签与 `page_indexes` 属性；空字符串显式处理为空列表（`if page_indexes_str:`），避免 `"".split(",")` 产出 `['']` 再被 `int()` 炸掉的经典坑。
- [pdf_craft/extractor/toc/types.py:L62-L88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L62-L88)：`decode_item` 对四个属性逐一做「缺失即抛 `ValueError`」的防御式校验，然后递归解码孩子并构造 `Toc`。
- [pdf_craft/extractor/toc/types.py:L90-L93](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L90-L93)：最终组装 `TocInfo`，`content` 来自根元素直接子节点的列表推导。
- [pdf_craft/common/xml.py:L28-L40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L28-L40)：`save_xml` 写 `.xml.tmp` 再 `replace` 的原子替换；失败时清理临时文件。文件头固定写入 XML 声明。
- [pdf_craft/common/xml.py:L21-L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L21-L25)：`read_xml` 把任何解析异常统一包装成带文件路径的 `ValueError`。

#### 4.2.4 代码实践

做一次「往返等价」实验（示例代码）：

```python
# 示例代码：encode → XML 文本 → decode → 再 encode，验证往返无损
from xml.etree.ElementTree import tostring
from pdf_craft.extractor.toc.types import Toc, TocInfo, encode, decode

info = TocInfo(
    content=[Toc(id=1, page_index=3, order=0, level=0, children=[
        Toc(id=2, page_index=4, order=2, level=1, children=[]),
    ])],
    page_indexes=[],
)
xml1 = tostring(encode(info), encoding="unicode")
xml2 = tostring(encode(decode(encode(info))), encoding="unicode")
assert xml1 == xml2, "round-trip mismatch"
print(xml1)
```

1. 实践目标：验证编解码是信息无损的往返。
2. 操作步骤：运行上述脚本；再故意删掉输出 XML 里某个 `item` 的 `level` 属性，把它 `fromstring` 后传入 `decode`。
3. 需要观察的现象：第一次断言通过；删除属性后抛出 `ValueError: Missing 'level' attribute in item`。
4. 预期结果：往返一致；缺属性被防御式校验拦下。待本地验证（属性缺失报错的具体文案以运行结果为准）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `page_indexes` 为空列表，`encode` 写出的根属性是什么？`decode` 读回来会得到什么？

**答案**：`",".join([])` 是空字符串，所以写出 `page_indexes=""`；`decode` 中 `if page_indexes_str:` 对空串为假，`page_indexes` 保持 `[]`。两端一致。

**练习 2**：`save_xml` 为什么要先写临时文件再 `replace`，而不是直接写 `toc_path`？

**答案**：因为「文件存在即缓存命中」（4.4）。如果直接写目标文件，进程在写到一半时崩溃会留下残缺的 `toc.xml`，下次运行会把它当作有效缓存解码并抛解析错误。临时文件 + `replace`（同目录下的原子重命名）保证目标路径上要么是旧文件、要么是完整新文件。

**练习 3**：`decode_item` 里 `[decode_item(child) for child in item]` 会遍历所有子元素。如果 XML 中混入了一个非 `item` 的子元素会发生什么？

**答案**：递归进入 `decode_item` 后第一个检查 `item.tag != "item"` 就会抛出 `ValueError: Expected tag 'item', got '...'`，即格式校验是逐层强制的。

### 4.3 栈式建树：_structure_toc_by_levels

#### 4.3.1 概念说明

上一讲的输出 `Ref2Level` 是一张扁平表：它知道每个条目是第几级，却不知道谁是谁的孩子。`_structure_toc_by_levels` 负责把扁平表「卷」成森林。

算法依赖一个朴素而可靠的排版事实：**目录条目按文档顺序出现，且层级只会呈括号结构**——一个 `level 1` 的条目，其父必然是它前面最近的、层级比它小的那个条目。于是用栈维护「当前从根到正在处理条目的路径」，逐条目处理：

- 新条目层级 `level` 比栈顶**大**：栈顶就是它的父，直接挂上去、入栈（路径加深）。
- 新条目层级**小于等于**栈顶：不断弹栈，直到栈顶层级严格小于它——弹掉的正是「已完结」的子树——然后挂到新栈顶下、入栈（路径回撤再加深）。

#### 4.3.2 核心流程

伪代码（对应真实实现）：

```
虚拟根 root(level=-1, id=-1)      # 永不弹出的哨兵
next_id = 1
stack = [root]

对 ref2level 按 (page_index, order) 升序遍历：
    node = Toc(id=next_id, level=level, children=[])
    next_id += 1
    while stack 非空 且 stack[-1].level >= level:
        pop 栈顶                     # 关闭所有已完结的子树
    if 栈空: break                   # 防御：level 不可能小于根的 -1
    stack[-1].children.append(node)  # 新栈顶是父
    stack.append(node)               # node 成为当前路径末端

返回 root.children                   # 虚拟根的孩子就是森林的顶级条目
```

手工模拟一遍（输入按文档顺序为 `0, 1, 2, 1, 0` 五个层级）：

| 输入 level | 弹栈后栈（按 level） | 挂到谁下面 | 树的状态 |
| --- | --- | --- | --- |
| 0 | [-1] | 根 | A(0) |
| 1 | [-1, 0] | A | A → B(1) |
| 2 | [-1, 0, 1] | B | A → B → C(2) |
| 1 | [-1, 0] | A | A → B → C；A → D(1) |
| 0 | [-1] | 根 | A…；E(0) |

两个实现细节：

- **遍历顺序**：`sorted(ref2level.items(), key=lambda x: x[0])` 按 `(page_index, order)` 字典序排序，保证条目按真实阅读顺序进入算法；`id` 也因此是「文档顺序编号」。
- **虚拟根**：`level=-1` 比一切合法层级（≥0）都小，永不弹出；`if not stack: break` 是对非法负层级的防御性兜底，正常输入下不会触发。

复杂度：每个条目至多入栈一次、出栈一次，总时间 \( O(n \log n) \)（排序主导；建树本身是 \( O(n) \)）。

#### 4.3.3 源码精读

- [pdf_craft/extractor/toc/analysing.py:L117-L127](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L117-L127)：构造 `level=-1` 的虚拟根与初始栈 `stack = [root]`，`next_id` 从 1 起编号。
- [pdf_craft/extractor/toc/analysing.py:L129-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L129-L137)：按 `(page_index, order)` 排序遍历扁平表，为每个条目新建 `Toc` 节点并递增 `next_id`。
- [pdf_craft/extractor/toc/analysing.py:L138-L145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L138-L145)：算法核心三步——`while stack and stack[-1].level >= level: stack.pop()`（注意是 `>=`：同级也弹，因为同级是兄弟不是孩子）、`if not stack: break` 防御、挂到 `stack[-1]` 下并入栈。
- [pdf_craft/extractor/toc/analysing.py:L147](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L147)：返回 `root.children`——虚拟根被丢弃，只留森林。

调用点在 `_do_analyse_toc` 的收尾处：

- [pdf_craft/extractor/toc/analysing.py:L111-L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L111-L114)：无论走「有目录页」还是「无目录页」、LLM 还是统计法，最终都汇聚到 `TocInfo(content=_structure_toc_by_levels(ref2level), page_indexes=toc_page_indexes)`。

#### 4.3.4 代码实践

用纯 Python 复刻该算法并跑断言（示例代码，不需要 PDF 产物）：

```python
# 示例代码：复刻 _structure_toc_by_levels 的栈算法
def structure(ref2level: dict[tuple[int, int], int]):
    root = {"level": -1, "children": []}
    stack = [root]
    for (page_index, order), level in sorted(ref2level.items()):
        node = {"level": level, "page_index": page_index, "children": []}
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        if not stack:
            break
        stack[-1]["children"].append(node)
        stack.append(node)
    return root["children"]

tree = structure({(1, 0): 0, (1, 1): 1, (2, 0): 2, (2, 1): 1, (3, 0): 0})
assert [n["level"] for n in tree] == [0, 0]          # 两个顶级章
assert [c["level"] for c in tree[0]["children"]] == [1, 1]  # 第一章程下两节
assert tree[0]["children"][0]["children"][0]["level"] == 2  # 小节
print("ok")
```

1. 实践目标：脱离 pdf-craft 也能独立复现栈式建树。
2. 操作步骤：运行脚本；然后把输入改成 `(1,0):1`（第一个条目就是 level 1），观察树形。
3. 需要观察的现象：断言全部通过；首条目为 level 1 时它仍会成为顶级条目（挂在虚拟根下），因为根的 -1 永远小于 1。
4. 预期结果：理解「栈顶严格小于自身才算父亲」这一不变式。

#### 4.3.5 小练习与答案

**练习 1**：弹栈条件为什么是 `stack[-1].level >= level` 而不是 `>`？

**答案**：同级条目互为兄弟。若用 `>`，遇到同级的下一个条目时会把前一个同级条目误当成父亲，树会向右歪成链。`>=` 确保只有严格更小的层级才能当父。

**练习 2**：虚拟根的 `level=-1` 起什么作用？去掉它、直接用空栈会遇到什么问题？

**答案**：它是一个永不弹出的哨兵，保证任何 `level >= 0` 的条目都能找到挂载点，同时让「顶级条目」自然成为 `root.children`。若用空栈，第一个条目入栈前需要特判；且当出现异常层级导致栈被弹空时，`if not stack: break` 需要一个哨兵才不至于在正常流程中误触发。

**练习 3**：如果 `ref2level` 中某条目的 level 序列出现「跳级」（如 `0, 2, 2`），算法结果是什么？

**答案**：不会报错也不会修复——第一个 `2` 直接挂到 `0` 下面成为其孩子（跳过的 1 级不存在），第二个 `2` 与之互为兄弟。层级跳跃的来源是上游分析（u4-l2）的分组质量，建树算法对输入完全信任。

### 4.4 结果缓存：toc.xml 的短路语义与下游影响

#### 4.4.1 概念说明

`analyse_toc` 的前四行实现了整条目录分析链路的缓存：

```python
if toc_path.exists():
    return decode_toc(read_xml(toc_path))
```

一旦 `analysing_path/toc.xml` 存在，`find_toc_pages`、统计法、LLM 法、栈式建树**全部跳过**，直接解码文件返回。这带来三层含义：

1. **省钱省时**：重跑提取时 OCR 有 `page_N.xml` 缓存（u3-l3），目录分析有 `toc.xml` 缓存，两次都不产生任何 token 消耗。
2. **缓存即干预入口**：文件是普通 XML，手改它等价于「以官方格式注入人工目录结论」——这正是综合实践的玩法。
3. **缓存没有校验**：短路只看「存在」，不校验内容与 `ocr/` 是否一致。想让目录重新分析，唯一办法是删掉 `toc.xml`（u4-l2 已提到）。

但要注意缓存的作用范围：它只短路**目录分析**这一步。**章节生成每次运行都会全量重算**——`generate_chapter_files` 开头先清空旧的 `chapter_*.xml` 再重建。于是「toc.xml 缓存 + chapters 重算」组合出一个非常实用的行为：改 `toc.xml` 后重跑，OCR 零成本、目录分析零成本、章节划分按新目录重新切分。

#### 4.4.2 核心流程

引擎四步主流程（u3-l1）中本讲涉及的部分：

```
_extract_from_pdf(analysing_path=工作目录)
  ├─ 1. OCR 循环 → ocr/page_N.xml（缓存，SKIP）
  ├─ 2. analyse_toc(ocr/, toc.xml)
  │     ├─ toc.xml 存在 → decode 直接返回   ← 本讲的缓存短路
  │     └─ 不存在 → find_toc_pages? → LLM/统计 → _structure_toc_by_levels
  │                → encode → save_xml（原子写） → 返回
  ├─ 3. generate_chapter_files(ocr/, chapters/, toc)  ← 每次全量重算
  └─ 4. write_metadata → document.json
```

#### 4.4.3 源码精读

- [pdf_craft/extractor/toc/analysing.py:L25-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L25-L38)：`analyse_toc` 全貌。第 31-32 行是缓存短路；第 34 行 `mkdir(parents=True, exist_ok=True)` 确保工作目录存在；第 35-36 行「分析 → 编码 → 原子落盘」。
- [pdf_craft/transform.py:L66-L69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L66-L69)：`toc_path = analysing_path / "toc.xml"`——缓存文件固定位于提取工作目录根部，与 `ocr/`、`chapters/`、`assets/` 平级。
- [pdf_craft/transform.py:L106-L112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L106-L112)：引擎调用点——`analyse_toc` 之后**无条件**调用 `generate_chapter_files`，印证「分析可缓存、切分必重算」。
- [pdf_craft/extractor/chapter/generation.py:L23-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L23-L26)：`generate_chapter_files` 每次先 `unlink` 掉所有旧的 `chapter_*.xml`。
- [pdf_craft/extractor/chapter/generation.py:L49-L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L49-L52)：下游消费方式——用 `iter_toc` 把树摊平成 `ref2toc[(page_index, order)]` 坐标索引，正文布局块按坐标反查命中的目录条目，命中即切出新章节。**`Toc` 不存文本的设计在这里闭环**：章节标题的文本来自正文中被命中的那个布局块，`Toc` 只提供 `id` 与 `level`。
- [pdf_craft/extractor/chapter/generation.py:L76-L84](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L76-L84)：第一个目录条目之前的正文（封面、前言等）归入 `id=None` 的 `chapter_head.xml`，其 `level` 取全书最大层级以防被标题覆盖——这是 u5 的内容，此处只看缓存如何影响它。

#### 4.4.4 代码实践

验证缓存的三段式实验（需要一份真实 PDF 与可用的 OCR 配置）：

1. 实践目标：亲眼确认「toc.xml 存在 → 分析短路」且「chapters 每次重算」。
2. 操作步骤：
   1. 用 `package_path=Path("pkg")` 跑一次 `convert_pdf_to_markdown`，确认生成 `pkg/toc.xml` 与 `pkg/chapters/`。
   2. 记录 `pkg/toc.xml` 的修改时间（`stat` 或 `ls -l --time-style=full-iso`）。
   3. 不删除任何文件，再次运行同一条转换命令。
   4. 对比第二次运行的输出：`toc.xml` 修改时间应不变；`chapters/` 下的文件应全部被重写（时间戳更新）。
3. 需要观察的现象：第二次运行速度快得多、OCR 全部 SKIP（可注册 `on_ocr_event` 打印事件类型确认）、token 计量为 0。
4. 预期结果：缓存短路生效。待本地验证（具体耗时取决于 PDF 规模与 OCR 后端）。

#### 4.4.5 小练习与答案

**练习 1**：用户改了 `ExtractionOptions.toc_llm` 想换一种层级分析，重跑后结果没变，为什么？

**答案**：`toc_path.exists()` 短路发生在一切分析之前，`toc_llm` 根本没被使用。必须先删除工作目录下的 `toc.xml`，新的 LLM 分析才会执行。

**练习 2**：`page_N.xml` 缓存与 `toc.xml` 缓存的粒度有何不同？

**答案**：`page_N.xml` 是**页级**缓存——删掉某一页的文件只重跑那一页（u3-l3 的断点续跑）；`toc.xml` 是**全书级**缓存——存在即整体复用、删除即全量重算，没有中间粒度。

**练习 3**：为什么「chapters 每次重算」对缓存语义是必要的补充？

**答案**：因为 `toc.xml` 是人工可编辑的干预入口。若 chapters 也被缓存，手改 `toc.xml` 后重跑将毫无效果。chapters 无条件重算（且重算只依赖本地文件、不花 token）使得「改目录 → 重切章节」的迭代闭环成立，代价几乎为零。

## 5. 综合实践

**任务：打造一个「目录树浏览器 + 缓存干预」小工具，完成一次目录手术。**

分三步：

**第一步：写独立脚本 `toc_tree.py`（示例代码）**，读取提取产物中的 `toc.xml`，按缩进打印整棵树：

```python
# 示例代码：解析 toc.xml 并按缩进打印目录树
import sys
from pathlib import Path
from xml.etree.ElementTree import fromstring

def walk(item, depth):
    print(
        "  " * depth
        + f"id={item.get('id')} level={item.get('level')} "
        + f"page_index={item.get('page_index')} order={item.get('order')}"
    )
    for child in item:
        walk(child, depth + 1)

root = fromstring(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"toc pages: {root.get('page_indexes') or '(none)'}")
for item in root:
    walk(item, 0)
```

**第二步：做一次目录手术。** 在打印结果中找一个 `level="0"` 的一级章节条目，把它的 `level` 改成 `1`（注意保持属性格式与文件其余部分不动），保存。改完后先重跑 `toc_tree.py` 肉眼检查树形变化（该章应「缩进」进前一章）。

**第三步：验证缓存传导。** 对同一 `package_path` 重新运行 `convert_pdf_to_markdown`，然后：

1. 检查 `chapters/` 目录——被降级的章节标题现在不再切出新 `chapter_N.xml`（它的内容并入了前一章），`chapter_*.xml` 的数量与内容随之变化；
2. 检查渲染出的 Markdown——对应标题的层级（`#` 的个数）应随 level 变化；
3. 若渲染的是 EPUB，目录（NCX/nav）中的嵌套关系也应同步变化。

观察要点：整个过程中 OCR 与目录分析零 token 消耗——你只用一个文本编辑器就改变了整本书的章节结构。具体的章节文件数量与 Markdown 标题形式**待本地验证**（取决于所选 PDF 的实际目录形态）。

## 6. 本讲小结

- `TocInfo`（森林 + 目录页页码）与 `Toc`（id / page_index / order / level / children）是目录分析的最终数据模型；`Toc` 只存「坐标 + 层级结论」，标题文本始终留在 `ocr/page_N.xml`。
- `encode`/`decode` 把树与 XML 一一映射：嵌套 `<item>` 对应嵌套孩子，四个整数属性逐一防御式校验，`page_indexes` 以逗号串编码、空串解码回空列表。
- 落盘走 `save_xml` 的临时文件原子写，保证「文件存在即完整」——这是缓存语义成立的前提。
- `_structure_toc_by_levels` 用「虚拟根 + 弹到严格更小」的栈算法把扁平 `Ref2Level` 表卷成森林，`>=` 弹栈条件保证同级互为兄弟；`id` 即文档顺序编号。
- `analyse_toc` 开头的 `toc_path.exists()` 短路使目录分析全书级缓存：存在即复用、删除即重算，不校验内容。
- 缓存只覆盖分析，不覆盖切分：`generate_chapter_files` 每次清空重建，因此手改 `toc.xml` 后重跑可以零成本地重新划分章节。

## 7. 下一步学习建议

下一单元（u5 章节生成）将正式打开本讲末尾只预告了一眼的 [pdf_craft/extractor/chapter/generation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L23-L87)：从 `Chapter` 数据模型与 `chapter_N.xml` 的 XML 编解码（u5-l1）开始，再到跨页文本块合并的 `Jointer`（u5-l2）。建议先读的衔接代码：

1. [generation.py:L49-L87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L49-L87)——`ref2toc` 坐标索引如何驱动章节切分；
2. [analysing.py:L111-L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L111-L114)——本讲建树结果的唯一出口，串起 u4 的全部三条链路。

动手有余力的读者，可以尝试给综合实践的 `toc_tree.py` 加一个校验子命令：检查子节点 level 是否严格大于父节点、id 是否从 1 连续递增——这正是 `decode` 没做、而你可以补上的质量闸门。
