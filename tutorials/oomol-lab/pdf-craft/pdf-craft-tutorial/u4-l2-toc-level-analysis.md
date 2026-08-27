# 层级分析：统计法与 LLM 法

## 1. 本讲目标

上一讲（u4-l1）我们搞清楚了「哪些页是目录页」：`find_toc_pages` 用 Aho-Corasick 把正文标题与候选页文本匹配打分，产出 `PageRef` 列表。本讲沿着目录分析的第二步继续：**知道目录条目之后，如何推断每个条目处于第几层级（章、节、小节…）**。

学完本讲，你应该能够：

1. 说清统计法的核心直觉——「字号（检测框高度）是层级的最强排版信号」，以及 `split_by_cv` 如何用变异系数把高度值聚成层级组。
2. 区分「有目录页」与「无目录页」两条分析路径的差异（`analyse_toc_levels` vs `analyse_title_levels`，以及它们对应的 LLM 版本）。
3. 理解 LLM 法的输入输出契约：提示词怎么构造、响应怎么校验、失败怎么包装成 `LLMAnalysisError`。
4. 解释 `_do_analyse_toc` 中 `except LLMAnalysisError` 分支为什么打印警告降级到统计法，而不是让整个提取任务失败。

## 2. 前置知识

### 2.1 变异系数（CV）

统计法反复用到**变异系数**（Coefficient of Variation）：

\[ CV = \frac{\sigma}{\mu} \]

其中 \(\mu\) 是组内高度的平均值，\(\sigma\) 是标准差。CV 衡量「相对离散程度」：一组章节标题的检测框高度都在 28px 附近（CV 很小），说明它们大概率是同一层级；若组里混着 28px 和 15px 的值（CV 变大），说明这个组还能再拆。用 CV 而不是方差的好处是与量纲无关——300 DPI 和 150 DPI 渲染出的绝对高度不同，但 CV 可比。

### 2.2 检测框高度 = 字号代理

OCR 阶段（u3-l4）为每个文本块产出了 `PageLayout`，其中 `det` 是检测框坐标 `(left, top, right, bottom)`。本讲中大量出现 `height = bottom - top`：**用检测框高度近似字号**。它不完美（中文与拉丁字符同字号下框高不同、上下标会拉高框），但在扫描书籍场景下是最稳定的层级信号之一。

### 2.3 Ref2Level：本讲的中心数据结构

```python
Ref2Level = dict[tuple[int, int], int]  # key: (page_index, order) value: level
```

键是 `(页码, 块序号)`——一个能唯一定位正文里某个标题块的引用；值是层级（0 最顶层）。两条分析路径、两种方法最终都产出这个字典，再由 `_structure_toc_by_levels` 建成 `Toc` 树写入 `toc.xml`。

### 2.4 与前讲的衔接

- `PageRef.matched_titles` 里已经保存了「目录行文本 → 它引用的正文标题 `(page_index, order)` 列表」，这是上一讲 Aho-Corasick 匹配的副产品，本讲 LLM 法与统计法都直接消费它。
- `toc.xml` 是目录分析的结果缓存：文件已存在则 `analyse_toc` 直接解码返回（见 [analysing.py:L31-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L31-L32)）。想重跑分析必须先删掉它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/extractor/toc/analysing.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py) | 编排层：`analyse_toc` 入口、`_do_analyse_toc` 的统计/LLM 双路径选择与回退、`_structure_toc_by_levels` 建树 |
| [pdf_craft/extractor/toc/toc_levels.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py) | 统计法全部实现：标题高度聚类、目录页挂钩分析、多目录页层级偏移换算 |
| [pdf_craft/extractor/toc/llm_analyser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py) | LLM 法全部实现：提示词构造、响应校验 schema、`_LLMAnalyser` 执行器与 `LLMAnalysisError` |
| [pdf_craft/common/cv_splitter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py) | 统计法的数学引擎：`split_by_cv` 按 CV 控制的最大间隔分裂聚类 |
| [pdf_craft/extractor/toc/config.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/config.py) | 阈值常量：`MAX_TITLE_CV = 0.025`、`MAX_LEVELS = 4` |
| [pdf_craft/extractor/toc/types.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py) | `Toc`/`TocInfo` 数据结构与 `toc.xml` 编解码 |
| [tests/test_toc_llm_analyser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_llm_analyser.py) | 用假 LLM 对象测 `_LLMAnalyser` 的错误包装与重试耗尽 |
| [tests/test_cv_splitter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/tests/test_cv_splitter.py) | `split_by_cv` 的单元测试 |

另需知道 [pdf_craft/pdf/ref.py:L1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ref.py#L1) 定义了 `TITLE_TAGS = ("title", "sub_title")`——统计法只认这两类布局的块是「标题」。

## 4. 核心概念与源码讲解

### 4.1 统计层级推断

#### 4.1.1 概念说明

统计法不依赖任何模型，它的假设只有一条：**同类层级的标题，字号（检测框高度）相近；不同层级，字号有台阶差**。出版社排版时章标题用大字号、节标题用中字号，这个惯例让「按高度聚类」成为可行的层级推断手段。

它需要解决两个问题：

1. 怎么把一堆高度值切成若干「层」？——`split_by_cv` 负责。
2. 「有目录页」时，目录行的高度层级是**每页内部的相对层级**（一个目录页里最大字是 level 0，但全书可能有多个目录页、且目录行字号与正文标题字号不是一回事），怎么换算成全书统一的全局层级？——`analyse_toc_levels` 的四步流程负责。

#### 4.1.2 核心流程

**`split_by_cv`（最大间隔分裂聚类）**：

```text
输入: [(高度值, 载荷), ...]，阈值 max_cv，上限 max_groups
1. 全部元素视为一个组
2. 循环（直到组数达 max_groups）:
   a. 找出 CV 仍超过 max_cv 的组（跳过元素 ≤ 2 的组）
   b. 若没有这样的组 → 结束
   c. 把该组按高度排序，在相邻值间隔（gap）最大处一分为二
   d. 若找不到间隔 → 结束
3. 各组按组内高度均值升序返回
```

调用侧拿到结果后做 `reversed(...)`，让**字号最大的组成为 level 0**。

**无目录页路径 `analyse_title_levels`**（一行转发）：

```text
遍历所有页 → 收集 ref ∈ ("title", "sub_title") 的块 → (height, (page_index, order))
→ split_by_cv(max_cv=0.025, max_groups=4) → reversed → 组序号即层级
```

**有目录页路径 `analyse_toc_levels`** 四步：

```text
1. _extract_ref2meta:
   对每个目录页，找出「引用了正文标题」的目录行（hook），
   按行高 split_by_cv(max_cv=0.75) 分成相对层级 relative_level，
   并记录该行引用的 (page_index, order) → _TitleMeta
2. _extract_content_title_levels(排除目录页、只看 ref2meta 中的标题):
   按正文标题高度做全局层级聚类 → ref2global_level
3. _extract_toc_level_offset:
   对每个目录页取其 relative_level 最小的那批条目，
   用它们在步骤 2 中的全局层级均值 avg 代表「该目录页的层级基准」，
   再 split_by_cv 得到每页的层级偏移 offset（多数情况一组，offset=0）
4. 合成: global_level = relative_level + toc_page_index 对应的 offset
```

用公式表达多目录页换算：设某目录页 \(p\) 上条目 \(i\) 的相对层级为 \(r_i\)，该页偏移为 \(o_p\)，则全局层级

\[ L_i = r_i + o_p \]

#### 4.1.3 源码精读

先看统计引擎本身。CV 计算在 `_Group._calculate_cv`（均值、方差、标准差逐个算出）：

- [pdf_craft/common/cv_splitter.py:L36-L44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L36-L44) —— 计算 \( CV = \sigma / \mu \)，元素不足两个时 CV 记 0（不再分裂），均值为 0 时记无穷大（必然分裂）。

主循环：

- [pdf_craft/common/cv_splitter.py:L47-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L47-L75) —— `split_by_cv` 主体：反复挑 CV 超标的最小组分裂，最多 `max_groups` 组；返回前按组内均值升序排序（`sorted(groups, key=lambda g: g.size)`），所以调用侧 `reversed` 后最大字号组是 level 0。
- [pdf_craft/common/cv_splitter.py:L96-L113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/cv_splitter.py#L96-L113) —— `_split_group_by_max_gap`：排序后找相邻元素的最大间隔，在那里切成两半。这是整个聚类的「切点选择」策略。

阈值常量：

- [pdf_craft/extractor/toc/config.py:L3-L4](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/config.py#L3-L4) —— `MAX_TITLE_CV = 0.025`（正文标题高度分组极严格：同组高度差异需在 2.5% 以内）、`MAX_LEVELS = 4`（最多识别 4 个层级）。

无目录页路径：

- [pdf_craft/extractor/toc/toc_levels.py:L16-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L16-L17) —— `analyse_title_levels` 只是把 `pages` 透传给 `_extract_content_title_levels`。
- [pdf_craft/extractor/toc/toc_levels.py:L127-L159](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L127-L159) —— `_extract_content_title_levels`：遍历页 → 过滤 `TITLE_TAGS` →（可选）排除目录页、只保留 `ref2meta` 里出现过的引用 → 收集 `(height, ref)` → `split_by_cv(max_cv=MAX_TITLE_CV, max_groups=MAX_LEVELS)` → `reversed` 后用 `enumerate` 给每组编号，写入 `Ref2Level`。两个可选参数正是「无目录页直接用 / 有目录页复用」的分岔点。

有目录页路径的主函数：

- [pdf_craft/extractor/toc/toc_levels.py:L20-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L20-L45) —— `analyse_toc_levels` 按上文四步依次调用 `_extract_ref2meta`、`_extract_content_title_levels`、`_extract_toc_level_offset`，最后在 L37-L43 的循环里合成 `global_level = meta.relative_level + level_offset`。L39-L41 的条件注释点明：`toc_level_offset` 的覆盖范围比 `ref2meta` 小，不在偏移表中的目录页条目（其挂钩未参与正文标题聚类）会被排除。

目录页挂钩分析：

- [pdf_craft/extractor/toc/toc_levels.py:L88-L124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L88-L124) —— `_analyse_toc_page_hooks`：逐目录页解码 `page_N.xml`，对每个正文块检查「该块的归一化文本里出现了哪些 `matched_titles` 的标题文本」，命中则把标题的引用集合并入这个 hook；然后按 hook 行高 `split_by_cv(max_cv=_MAX_TOC_CV)` 分组。注意 [L11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L11) 的 `_MAX_TOC_CV = 0.75` 远比正文标题的 0.025 宽松——目录行的高度受行内页码、点线影响，离散得多。

多目录页偏移换算：

- [pdf_craft/extractor/toc/toc_levels.py:L162-L200](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/toc_levels.py#L162-L200) —— `_extract_toc_level_offset`：先把每个引用在正文聚类中得到的全局层级收集进 `collected_global_levels`；再按目录页分组，取每页 `relative_level` 最小的条目，用其全局层级的均值 `avg` 作为该页的层级基准；最后对这些基准值再做一次 `split_by_cv` 得到各页偏移。L196 的注释说明大多数书只有一组目录页，偏移恒为 0。

#### 4.1.4 代码实践

**实践目标**：不依赖任何 PDF 与 OCR 服务，单独验证 `split_by_cv` 的分组行为，建立对「高度 → 层级」映射的手感。

**操作步骤**（示例代码，可直接保存为 `cv_demo.py` 在仓库根目录运行）：

```python
# 示例代码：模拟一本书的标题高度分布
from pdf_craft.common import split_by_cv

# 三档高度：章标题 ~46px、节标题 ~30px、小节标题 ~22px，再加一点噪声
heights = [46.1, 45.8, 46.3, 30.2, 29.8, 30.5, 22.1, 22.4, 21.9, 46.0, 30.0]
items = [(h, f"title_{i}") for i, h in enumerate(heights)]

groups = split_by_cv(payload_items=items, max_cv=0.025, max_groups=4)
for level, group in enumerate(reversed(groups)):
    print(f"level {level}: {sorted(group)}")
```

**需要观察的现象**：

1. 三档高度是否恰好被切成三组，组内成员都是同档的 `title_*`。
2. `reversed` 之后 level 0 是否对应 ~46px 那组（字号最大 = 最顶层）。
3. 把某个 `22.1` 改成 `26.0`（制造一个「骑墙」高度），观察它落入哪一组、分组数是否变化。

**预期结果**：干净的三档数据应产出三组；边界噪声可能引起组数或归属变化。具体分组输出**待本地验证**——`split_by_cv` 的切点取决于最大间隔位置，建议实际运行确认。

**进阶**：阅读 [tests/test_cv_splitter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_cv_splitter.py)，挑一个用例改成断言「四档高度」的输入，跑 `python -m pytest tests/test_cv_splitter.py`。

#### 4.1.5 小练习与答案

**练习 1**：为什么正文标题分组用 `MAX_TITLE_CV = 0.025`，而目录行分组用 `_MAX_TOC_CV = 0.75`？

**参考答案**：正文标题块是独立的单行文本，检测框高度几乎只由字号决定，同类标题高度差异极小，所以阈值必须收紧（2.5%）才能把「章/节」的细微字号差切开；目录行的高度受同行页码、引导点线、多行折行影响，本身离散度大，阈值太严会被切成大量无意义小组（源码注释「不宜过小导致过多分组」即此意），因此放宽到 0.75。

**练习 2**：一本 PDF 有两个目录页（第 5 页是「卷一」目录、第 90 页是「卷二」目录），第二页的目录行整体比第一页小一号。统计法如何保证两页条目的全局层级可比较？

**参考答案**：`_analyse_toc_page_hooks` 给出的是**页内相对层级**；`_extract_toc_level_offset` 再对「每页 relative_level 最小的条目」取其在正文标题聚类中的全局层级均值作为该页基准，按基准差异算出每页偏移 `offset`，最终 `global_level = relative_level + offset`。即使第二页整体字号小一档，只要其顶层条目在正文中对应的标题层级与第一页一致，偏移就会把两页对齐到同一全局刻度。

**练习 3**：`_extract_content_title_levels` 的参数 `disable_page_indexes` 在无目录页路径中为什么不传？

**参考答案**：无目录页路径没有发现任何目录页（`toc_pages` 为空），自然没有需要排除的页；该参数只在有目录页路径中传入目录页页码集合，避免目录页上大量「形似标题」的条目混入正文标题的高度统计、污染层级聚类。

### 4.2 LLM 层级推断

#### 4.2.1 概念说明

统计法有两个盲区：

1. **噪声分不清**：图注、习题编号、页眉都可能被 OCR 标成 `title`/`sub_title`，统计法把它们一律当标题分层级。
2. **语义看不懂**：「第 1 章」与「1.1 节」的层级关系靠编号语义一望即知，但两行高度几乎相同时，纯统计无法区分。

LLM 法把人类做这类判断时依赖的线索（编号模式、语义、缩进、位置、密度）连同统计法的字号预分组一起交给模型，让它输出每个条目的层级。与统计法「总是返回结果」不同，LLM 法是**尽力而为的增强路径**：所有失败模式都被收敛为 `LLMAnalysisError`，由调用方决定回退（见 4.3）。

#### 4.2.2 核心流程

与统计法对称，LLM 法也有两条路径：

**有目录页：`analyse_toc_levels_by_llm`**

```text
1. 从目录页内容提取全部目录条目（文本、缩进 indent=左边界、字号=行高）
2. 从 toc_page_refs.matched_titles 提取「目标标题 → 引用列表」
   （任一为空则直接返回 {}，走统计法也不奇怪——没有任何可用挂钩）
3. 构造提示词：COMPLETE TOC（全部条目，带缩进/字号标注） + TARGET TITLES（字母编号 A、B、C…）
4. _LLMAnalyser.request 发起带校验重试的请求，得到每个目标标题的层级
5. 展开引用：对每个 (page_index, order) 写入 ref2level
```

**无目录页：`analyse_title_levels_by_llm`**

```text
1. 收集全书 TITLE_TAGS 块：文本、ref、高度（无块则返回 {}）
2. 先用 split_by_cv(0.025, 4) 做字号预分组，作为提示词中的参考线索
3. 提示词要求 LLM：区分结构标题与噪声（噪声给 -1），输出 ID → 层级
4. 校验通过后，仅 level >= 0 的标题进入 ref2level（噪声被过滤）
```

**响应校验三规则**（`_TitleLevelsSchema` / `_TocLevelsSchema`）：

1. 值必须是整数，且在合法区间（标题版允许 -1 表噪声，上限 5）。
2. 层级跳变不能超过 2（从 0 直接跳到 3 很可能是模型胡说）。
3. 第一个有效层级通常应为 0（书从顶层章开始）。

校验失败不抛异常，而是返回错误文案——这文案会被拼进下一轮请求作为反馈，让模型自我纠正（修复循环的详细机制在 u8-l2 展开）。

#### 4.2.3 源码精读

**异常类型与常量**：

- [pdf_craft/extractor/toc/llm_analyser.py:L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L17) —— `_MAX_RETRIES = 3`：总尝试次数上限。
- [pdf_craft/extractor/toc/llm_analyser.py:L22-L24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L22-L24) —— `LLMAnalysisError`：LLM 分析失败的统一出口类型，是 4.3 回退逻辑的捕获目标。

**有目录页路径**：

- [pdf_craft/extractor/toc/llm_analyser.py:L85-L131](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L85-L131) —— `analyse_toc_levels_by_llm` 主体。L90-L102 的两个提前返回值得注意：目录条目为空或没有任何带引用的匹配标题时返回空字典（此时 `ref2level` 非 `None`，但为空——注意这与会触发回退的「异常」不同，是合法的空结果）。L126-L129 把每个目标标题的层级展开写到它引用的所有 `(page_index, order)` 上。
- [pdf_craft/extractor/toc/llm_analyser.py:L134-L152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L134-L152) —— `_extract_toc_entries`：目录页每个非空正文块变成一条 `_TocEntry`，`indent` 取检测框左边界、`font_size` 取行高。`references` 与 `is_matched` 字段在新方案中已不使用（注释注明），保留是历史痕迹。

**无目录页路径**：

- [pdf_craft/extractor/toc/llm_analyser.py:L27-L82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L27-L82) —— `analyse_title_levels_by_llm`：收集标题（L29-L40，文本做空白归一）→ CV 预分组（L45-L54，注释明确这是给 LLM 的「preliminary grouping」参考线索）→ 请求（L55-L74）→ L77-L81 只保留 `level >= 0` 的条目，-1 噪声标题被丢弃，这正是 LLM 法相对统计法的去噪能力。

**提示词**：

- [pdf_craft/extractor/toc/llm_analyser.py:L340-L389](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L340-L389) —— 目录版 system prompt：两步任务（先自由分析完整目录结构，再为 TARGET TITLES 输出结果），明确列出缩进、字号、编号模式等判断依据，并要求「RESULT:」后跟 JSON。让模型先在 ANALYSIS 区自由思考、最后才给结构化结果，是提高结构化输出质量常用的话术结构。
- [pdf_craft/extractor/toc/llm_analyser.py:L155-L201](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L155-L201) —— 标题版 system prompt：额外定义了 -1 噪声语义（图注、习题号、页眉、索引项）。
- [pdf_craft/extractor/toc/llm_analyser.py:L392-L413](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L392-L413) 与 [L204-L234](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L204-L234) —— 两个 user prompt 构造器：目录版列出全部条目（带 `Indent:`/`Size:` 标注）与目标标题（字母 ID）；标题版列出按字号分组的统计信息与全部标题（带 `Group:`/`Page:`/`Size:` 标注）。
- [pdf_craft/extractor/toc/llm_analyser.py:L510-L536](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L510-L536) —— `_index_to_letter_id`：目标标题用 A、B、…Z、AA 的 Excel 式字母编号，避免与目录条目的数字编号混淆。

**响应校验**：

- [pdf_craft/extractor/toc/llm_analyser.py:L237-L337](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L237-L337) —— `_validate_title_response`：先用 `rindex("RESULT:")` 取**最后一次**出现的标记（避免匹配到 ANALYSIS 区里引用的样例），再 `repair_json` 容错解析；随后做 ID 完备性检查（缺失/多余都返回错误文案）；最后过 schema、做「最小层级归一到 0」与 `MAX_LEVELS - 1` 封顶（L301-L322）。所有失败分支返回 `(None, 给模型的错误反馈)` 而非抛异常。
- [pdf_craft/extractor/toc/llm_analyser.py:L416-L507](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L416-L507) —— `_validate_toc_response`：目录版同构逻辑，区别是不允许 -1（目录条目都是真实条目）、ID 换成字母。
- [pdf_craft/extractor/toc/llm_analyser.py:L633-L680](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L633-L680) 与 [L683-L724](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L683-L724) —— 两个 pydantic 校验器，固化上文三规则；抛出的 `ValidationError` 消息会被上层转成反馈文案。

**执行器**：

- [pdf_craft/extractor/toc/llm_analyser.py:L560-L606](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L560-L606) —— `_LLMAnalyser.request`：把校验函数包进 `request_guaranteed_json`（u8-l2 详述的保证式 JSON 请求层），`max_retries=_MAX_RETRIES - 1` 意味着总共最多 3 次尝试（测试 `test_non_object_response_exhausts_as_typed_schema_failure` 用 3 个坏响应验证了耗尽行为）。两个细节：L568 的 `isinstance(llm, LLM)` 判断——传入真 `LLM` 配置则走 `runtime_for(llm, protocol_version="toc-json-v1")` 的运行时（带重试/缓存目录等），否则鸭子类型地直接调 `llm.request(...)`，这正是单测能用假 LLM 对象的原因；L594 的 `use_cache=False`——目录分析请求**不走** LLM 缓存（与翻译请求不同）。L603-L606 把任何非 `LLMAnalysisError` 的异常统一包成 `LLMAnalysisError` 并保留 `from error` 因果链。

#### 4.2.4 代码实践

**实践目标**：不花一分钱 API 费用，借助「假 LLM」机制体验 LLM 路径的校验与重试。

**操作步骤**：

1. 阅读 [tests/test_toc_llm_analyser.py:L14-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_llm_analyser.py#L14-L36)：`_BrokenLLM`（总是抛 `RuntimeError`）、`_JsonLLM`（返回固定 JSON）、`_SequenceLLM`（按脚本顺序返回多个响应）三个测试替身。
2. 运行整个测试文件：

   ```bash
   python -m pytest tests/test_toc_llm_analyser.py -v
   ```

3. 模仿 `test_wraps_llm_request_errors_as_analysis_error`（[L40-L56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_llm_analyser.py#L40-L56)），自己写一个用例：`_SequenceLLM(['{"0": 9, "1": 9}', '{"0": 0, "1": 1}'])`，校验函数直接复用 `_validate_title_response`（`payload=2`），断言第一次响应被「最小层级归一」接受为 `[0, 0]`，请求只发生 1 次。

**需要观察的现象**：

- `_BrokenLLM` 的 `RuntimeError` 如何被包成 `LLMAnalysisError`，且 `__cause__` 仍是原异常。
- 坏响应（如 `[1, 2]` 非对象）如何触发第二次请求（重试），三次全坏才耗尽。

**预期结果**：全部测试通过；你新写的用例中 `levels == [0, 0]`（9 归一化到 0）。归一化行为可从 [_validate_title_response 的 L304-L315](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L304-L315) 推出，具体断言结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_validate_title_response` 用 `rindex`（最后一次出现）而不是 `index`（第一次出现）来定位 `RESULT:`？

**参考答案**：模型可能在 ANALYSIS 区引用提示词里的格式说明，导致文本中出现多个 `RESULT:` 字样；真正承载结果的是最后那一段。测试 `_ResultLLM`（[tests/test_toc_llm_analyser.py:L34-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_llm_analyser.py#L34-L36)）专门构造了「ANALYSIS 里带 JSON、两个 RESULT」的响应来验证这一行为。

**练习 2**：标题版校验允许 -1，目录版不允许。为什么？

**参考答案**：无目录页路径把全书所有 `title`/`sub_title` 块都送进提示词，其中混有图注、页眉、习题号等噪声，-1 是「请模型帮忙标出噪声」的语义，之后 L77-L81 只保留 `level >= 0` 的条目完成过滤；有目录页路径的输入本身就是目录条目（且已被 `matched_titles` 过滤为真正引用了正文的行），不存在噪声标注需求，出现负值只可能是模型错误，应触发重试而非静默过滤。

**练习 3**：`_LLMAnalyser` 为什么要用 `isinstance(llm, LLM)` 分流，而不是统一走 `runtime_for`？

**参考答案**：`runtime_for` 需要 `LLM` 配置对象才能构造带重试、缓存、日志的完整运行时；测试与嵌入场景常用只有 `request(input)` 方法的鸭子类型替身。分流让「真配置走全功能运行时、替身走裸调用」共存，同时把 `protocol_version="toc-json-v1"` 的缓存隔离限定在真运行时上。这也是库内「配置与执行分离」设计的一个柔性边界。

### 4.3 回退策略与结果固化

#### 4.3.1 概念说明

`_do_analyse_toc` 是本讲三条线索的汇合点：它决定走哪条路径、在 LLM 失败时如何降级、以及最终如何把 `Ref2Level` 变成 `Toc` 树。回退策略的设计哲学是：**LLM 是锦上添花，统计法是保底**——一次 PDF 提取的前面是昂贵的 OCR（大量页面渲染与 token），不能因为最后的可选增强步骤失败而全部作废。

#### 4.3.2 核心流程

```text
analyse_toc(pages_path, toc_path, toc_assumed, toc_llm)
  ├─ toc.xml 已存在？ → 直接 decode 返回（缓存短路）
  ├─ _do_analyse_toc:
  │    ├─ toc_assumed 为真 → find_toc_pages 找目录页
  │    ├─ 有目录页:
  │    │    ├─ toc_llm 非空 → analyse_toc_levels_by_llm
  │    │    │     └─ 抛 LLMAnalysisError → print 警告，ref2level 保持 None
  │    │    └─ ref2level 仍是 None → analyse_toc_levels（统计法）
  │    ├─ 无目录页:
  │    │    ├─ toc_llm 非空 → analyse_title_levels_by_llm（同样 try/except）
  │    │    └─ ref2level 仍是 None → analyse_title_levels（统计法）
  │    └─ _structure_toc_by_levels(ref2level) → Toc 树
  └─ encode 后写入 toc.xml
```

建树算法（栈式）：

```text
维护栈 stack = [虚拟 root(level=-1)]
对排序后的 (page_index, order) → level 逐条处理:
  1. 弹出栈顶所有 level >= 当前 level 的节点
  2. 当前节点挂到新栈顶的 children
  3. 当前节点入栈
```

由于条目按 `(page_index, order)` 升序处理，栈中保存的正是「从根到当前节点」的祖先链。

#### 4.3.3 源码精读

- [pdf_craft/extractor/toc/analysing.py:L25-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L25-L38) —— `analyse_toc`：L31-L32 的缓存短路意味着**重跑 LLM 分析前必须删除 toc.xml**，否则旧结果（可能是统计法降级产物）会被原样复用；L36 把结果落盘，这一行也解释了为什么失败降级后 toc.xml 仍然会被写入。
- [pdf_craft/extractor/toc/analysing.py:L52-L66](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L52-L66) —— 仅当 `toc_assumed` 为真才调用 `find_toc_pages`；两个迭代器回调分别供给「正文标题」与「整页文本」，标题文本先被 [L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L22) 的 `_TITLE_HEAD_REGX` 剥掉 Markdown 式 `#` 前缀（OCR 有时把大字号标题识别成 `# 标题`）。
- [pdf_craft/extractor/toc/analysing.py:L71-L97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L71-L97) —— 有目录页分支。L72-L88 的 `try/except LLMAnalysisError`：捕获后只 `print` 一句警告（含原始错误信息），**不置 `ref2level`、不 re-raise**；于是 L90 的 `if ref2level is None` 成立，统计法接管。L96-L97 把目录页页码排序收集进 `toc_page_indexes`。
- [pdf_craft/extractor/toc/analysing.py:L99-L109](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L99-L109) —— 无目录页分支，与前者完全同构，只是换成 `analyse_title_levels_by_llm` / `analyse_title_levels`，警告文案也相应为 `LLM analysis title failed`。
- [pdf_craft/extractor/toc/analysing.py:L111-L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L111-L114) —— 用建好的树与目录页页码构造 `TocInfo`。`page_indexes` 会写入 toc.xml 的 `page_indexes` 属性，下游章节生成用它把目录页从正文中剔除（见 [generation.py:L96-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L96-L108)，u5 详述）。
- [pdf_craft/extractor/toc/analysing.py:L117-L147](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L117-L147) —— `_structure_toc_by_levels`：L118-L125 造 `level=-1` 的虚拟根（比任何真实层级都浅，保证第一批条目总能挂上）；L129 按 `(page_index, order)` 排序保证阅读顺序；L138-L139 的 `while stack and stack[-1].level >= level: stack.pop()` 是栈算法核心——弹出所有不比当前节点深的节点后，栈顶就是最近的浅层祖先；L141-L142 的 `if not stack: break` 是防御性代码（level 恒非负且 root 为 -1 时实际不可达）。
- [pdf_craft/extractor/toc/types.py:L29-L47](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/types.py#L29-L47) —— `encode`：树形 `toc.xml` 就在这里生成——根元素带 `page_indexes` 属性，每个 `item` 带 `id/page_index/order/level` 四个属性并嵌套 `children`。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次「LLM 失败 → 统计法回退」，并观察 toc.xml 缓存对重跑的影响。

**操作步骤**：

1. 复用 u1-l2 的转换脚本，指定 `package_path` 保留中间包，先用 `toc_llm=None` 跑一次。
2. 打开 `<package_path>/toc.xml`，用缩进观察层级深度（`item` 的嵌套层数）。
3. 删除 `toc.xml`（**不要**动 `ocr/` 目录），构造一个必然失败的 LLM 配置再跑一次。最省事的办法是传一个「坏 URL」的真配置（示例代码）：

   ```python
   # 示例代码：url 指向不存在的本地端口，请求必然连接失败
   from pathlib import Path
   from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions
   from pdf_craft.llm import LLM

   # OCR 配置同你平时能跑通的一份，例如 DeepSeekOCRVendorConfig(...)
   craft = PDFCraft(pdf=PDFOptions(ocr=...))
   bad_llm = LLM(
       key="sk-dummy", url="http://127.0.0.1:9",
       model="any", token_encoding="o200k_base",
       retry_times=1, retry_interval_seconds=0,  # 默认 5 次×6 秒，坏配置会等很久
   )
   craft.extract_pdf(
       source=Path("book.pdf"),
       package_path=Path("pkg"),
       options=ExtractionOptions(toc_llm=bad_llm),
   )
   ```

4. 再次删除 `toc.xml`，换成可用的 LLM 配置跑第三次。

**需要观察的现象**：

- 第 3 步控制台应打印 `LLM analysis toc failed, falling back to statistical method: ...`（或无目录页时的 `LLM analysis title failed...`），随后提取正常完成。
- 由于 `ocr/page_N.xml` 缓存在（u3-l3），第 3、4 步不会重新 OCR，目录分析在秒级完成。
- 比较第 2、3、4 步三份 toc.xml 的层级差异。

**预期结果**：第 3 步与第 2 步的 toc.xml 应当完全一致（同为统计法产物）；第 4 步（LLM 成功）可能呈现不同层级划分，且通常更贴近书的真实结构、噪声标题更少。差异大小取决于具体书籍，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：阅读 [analysing.py:L85-L88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L85-L88)，用约 100 字说明为什么这里打印警告而不是抛异常。

**参考答案**：LLM 分析是增强路径而非必要条件，统计法始终能给出可用结果；一次提取的成本大头是前面的 OCR（渲染、token），若因可选增强失败而中止，用户既拿不到产物也已付出成本。而且 `analyse_toc` 随后就会把（统计法）结果写入 toc.xml——若在这里抛异常，用户连降级产物都得不到，重试还要再等一遍流程。打印警告让用户知情，降级让任务继续，是典型的「best-effort 增强 + 确定性保底」取舍。

**练习 2**：`_do_analyse_toc` 中 LLM 路径返回空字典 `{}`（例如目录条目为空时）与会抛 `LLMAnalysisError` 的失败，后续走向有何不同？

**参考答案**：空字典是合法返回值，`ref2level` 被赋值为 `{}` 而非保持 `None`，于是 `if ref2level is None` 不成立，**统计法不会执行**，最终 `TocInfo.content` 为空列表（空目录）；而异常路径 `ref2level` 保持 `None`，统计法接管。也就是说「LLM 明确说没有条目」与「LLM 没能完成分析」被区分对待——前者是结论，后者才需要回退。

**练习 3**：`_structure_toc_by_levels` 中若把 `while stack and stack[-1].level >= level` 的 `>=` 改成 `>`，会发生什么？

**参考答案**：同级节点不再被弹出，而是互相嵌套：第二个 level-0 的章会变成第一个章的 `children`（因为栈顶的 level 等于而非大于当前 level 时不再弹出，当前节点被挂到上一章下面）。树的形状从「兄弟并列」变成「依次嵌套」，下游章节切分与 EPUB 目录都会随之错乱。`>=` 保证了「同层即兄弟」。

## 5. 综合实践

**任务**：对同一 PDF 完成「统计法 vs LLM 法」对照实验，并撰写回退设计的分析短文。

1. **准备**：选一本有目录页、层级不少于两层的 PDF（`tests/assets/` 下的资产或你自己的书）。写一个脚本 `compare_toc.py`，参数化 `toc_llm` 的有无，始终传 `package_path`。
2. **跑 A（统计法）**：`toc_llm=None` 跑提取，保存 `toc.xml` 为 `toc_stat.xml`。
3. **跑 B（LLM 法）**：删除 `toc.xml`（保留 `ocr/`），换 `toc_llm=LLM(...)`（真实可用配置）再跑，保存为 `toc_llm_result.xml`。
4. **对比**：写 10 行左右的对比脚本（`xml.etree.ElementTree` 递归打印两棵树的 `(level, page_index, order, id)` 序列），统计：条目总数、最大深度、level=0 的节点数、两棵树中相同 `(page_index, order)` 但 level 不同的条目数。
5. **分析短文**：结合第 4.3.5 练习 1 的答案与 [analysing.py:L85-L88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L85-L88)、[L103-L106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L103-L106) 两处 `except` 分支，写 100 字左右的说明：为什么这里是 `print` 警告加降级，而不是让异常向上传播。
6. **加分项**：把短文论点落到代码上——如果改成 `raise`，`analyse_toc` 的 [L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/analysing.py#L36)（`save_xml`）还能执行吗？用户重跑时哪些缓存会让第二次尝试变便宜？

**预期结果**：B 版 toc.xml 通常层级更干净（噪声标题少）；若你的 LLM 配置不可用，控制台出现降级警告且产物与 A 完全一致。对照数值**待本地验证**。

## 6. 本讲小结

- 层级分析的中心数据结构是 `Ref2Level`（`(page_index, order) → level`），统计法与 LLM 法最终都产出它，再由 `_structure_toc_by_levels` 用栈算法建成 `Toc` 树。
- 统计法的核心假设是「字号即层级信号」：`split_by_cv` 反复对 CV 超标的组在最大间隔处一分为二；正文标题阈值 0.025 极严格，目录行阈值 0.75 很宽松。
- 「有目录页」路径多出一步多页换算：目录行高度只给出页内相对层级，需经 `_extract_toc_level_offset` 用正文标题的全局层级校准出每页偏移。
- LLM 法有两条对称路径（`analyse_toc_levels_by_llm` / `analyse_title_levels_by_llm`），提示词带两步结构（先自由分析后 `RESULT:` JSON），响应经 ID 完备性、三规则 schema、归一化与封顶层层校验，最多尝试 3 次。
- 回退策略：`except LLMAnalysisError` 只打印警告、让 `ref2level` 保持 `None`，统计法接管；LLM 是 best-effort 增强，统计法是确定性保底，昂贵的 OCR 成本不允许因可选步骤失败而作废。
- `toc.xml` 是缓存边界：存在即短路返回，因此切换 `toc_llm` 重跑前必须删除它，而 `ocr/page_N.xml` 缓存让重跑几乎零成本。

## 7. 下一步学习建议

下一讲（u4-l3）将把镜头拉到 `Toc`/`TocInfo` 数据模型本身与 `toc.xml` 的编解码细节，并完整拆解 `_structure_toc_by_levels` 的栈式建树——本讲已初见其貌，下讲从缓存语义角度补全。之后进入 u5 章节生成单元：`generate_chapter_files` 会消费本讲产出的目录树切分章节，并用 `toc.page_indexes` 把目录页从正文中剔除（[generation.py:L96-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L96-L108)）。若你对 `_LLMAnalyser` 底层的 `request_guaranteed_json` 与修复循环感兴趣，可提前跳读 u8-l2。建议顺带阅读 [tests/test_cv_splitter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_cv_splitter.py) 与 [tests/test_toc_llm_analyser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_llm_analyser.py)，两份测试分别固化了本讲的数学引擎与 LLM 契约。
