# 术语表系统：Glossary 与 Hyperscan

## 1. 本讲目标

本讲聚焦 BabelDOC 翻译流水线中的「术语表系统」。学完本讲，你应当能够：

- 理解 BabelDOC 为什么需要术语表（解决专有名词、领域术语前后译文不一致的问题）。
- 掌握 `Glossary.from_csv` 如何从 CSV 加载术语，以及 `tgt_lng` 列如何做归一化过滤。
- 理解 `Glossary` 如何用 **Hyperscan**（高性能多模式正则引擎）对一段文本做一次性匹配，并理解「分块（每块 2 万 pattern）」设计。
- 看懂匹配到的术语如何被注入到给 LLM 的翻译提示中，以及术语表的「名字」从哪里来、多个术语表如何协作。
- 能够自己写一个三列术语表，加载后对一段文本调用匹配，验证哪些术语被命中。

本讲承接 u6-l4「自动术语抽取」：u6-l4 讲的是**机器自动**从文档里提炼术语并翻译（多数投票汇总成 `auto_extracted_glossary`），本讲讲的是**术语表这个数据结构本身**——无论术语来自用户手写 CSV 还是自动抽取，最终都汇入同一个 `Glossary` 类，由它负责匹配与提示注入。

## 2. 前置知识

- **什么是术语表（glossary）**：一份「原文术语 → 译文术语」的对照表。例如论文里反复出现 `Transformer`，我们希望全篇统一译成「Transformer」而不是一会儿「变换器」一会儿「转换器」。术语表就是强制这种一致性的工具。
- **为什么不直接字符串替换**：直接把文本里的术语替换掉会破坏原文结构（BabelDOC 用占位符 `{v1}` 保护公式、用 `<style>` 标签保护富文本，见 u6-l2）。正确做法是**把术语表作为提示喂给 LLM**，让 LLM 在翻译时自觉采用约定译法，原文结构不动。
- **为什么匹配要用 Hyperscan**：术语表可能有几千上万条。如果对每条术语用一个 Python 正则去 `re.search`，复杂度是「条数 × 段落数」，非常慢。Hyperscan 是 Intel 开源的高性能多模式正则匹配库（C 实现、SIMD 加速），可以**把成千上万个正则编译进一个数据库，对一段文本只扫描一遍就找出所有命中**，非常适合这个场景。
- **Hyperscan 与 Python 的桥接**：BabelDOC 使用 `hyperscan` 这个 Python 绑定。核心 API 是 `hyperscan.Database()`：先 `compile(expressions, ids, flags)` 编译模式集合，再 `scan(bytes, on_match_callback)` 扫描文本，命中时回调 `on_match`。

> 关键术语速查：`GlossaryEntry`（单条术语）、`Glossary`（术语表，含匹配引擎）、`tgt_lng`（术语的目标语言列）、`normalize_source`（源术语归一化）、Hyperscan（多模式正则引擎）、`_build_glossary_block`（把命中术语拼成提示片段）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/glossary.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py) | 术语表核心实现：`GlossaryEntry`、`Glossary`，含 `from_csv` 加载、Hyperscan 编译与 `get_active_entries_for_text` 匹配。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | 术语表的「装配与协作」：`SharedContextCrossSplitPart` 管理用户术语表与自动术语表，提供 `get_glossaries_for_translation` 决定翻译时用哪些表。 |
| [babeldoc/format/pdf/document_il/midend/il_translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py) | 术语注入翻译提示：`_build_glossary_block` 调匹配、拼 Markdown 表格，`generate_prompt_for_llm` 把它填入提示模板。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | CLI 入口：`--glossary-files` 参数解析与 `Glossary.from_csv` 调用。 |
| [docs/example/demo_glossary.csv](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/example/demo_glossary.csv) | 官方示例术语表，演示了引号、逗号的 CSV 转义。 |

数据流（贯穿本讲）：`CSV 文件 → from_csv 加载（tgt_lng 过滤）→ Glossary（编译 Hyperscan 库）→ 对每段文本 get_active_entries_for_text 命中 → _build_glossary_block 拼提示 → LLM 翻译`。

## 4. 核心概念与源码讲解

### 4.1 CSV 术语加载与 tgt_lng 过滤

#### 4.1.1 概念说明

术语表的「原料」是一个 CSV 文件，必须有三列（第三列可选）：

| 列名 | 含义 | 是否必需 |
| --- | --- | --- |
| `source` | 原文术语（源语言） | 必需 |
| `target` | 译文术语（目标语言） | 必需 |
| `tgt_lng` | 该条术语适用的目标语言，如 `zh-CN`、`en-US` | 可选 |

`tgt_lng` 的设计意图：**同一个术语表 CSV 可以同时服务多种目标语言**。比如一份 `terms.csv` 里既有中英对照、也有中日对照，通过 `tgt_lng` 列标注。翻译到中文时只加载 `tgt_lng=zh-CN`（或留空表示「通用」）的条目，翻译到日语时只加载 `tgt_lng=ja-JP` 的条目。

判断「目标语言是否匹配」需要**归一化**：`zh-CN`、`zh_CN`、`ZH-CN` 都应视为同一种语言。BabelDOC 的归一化规则是「转小写 + 把连字符 `-` 换成下划线 `_`」，所以 `zh-CN` → `zh_cn`。

#### 4.1.2 核心流程

`from_csv` 的执行步骤（伪代码）：

```
glossary_name = 文件名（去掉 .csv 后缀）          # 术语表的名字来源
normalized_out = lang_out.lower().replace("-", "_")  # 目标语言归一化（只做一次）
读取文件原始字节 → chardet 猜测编码 → 解码成字符串
用 csv.DictReader(doublequote=True) 解析
校验：表头必须含 source 和 target 两列，否则抛错
for 每一行:
    取 source / target / tgt_lng
    if 该行填了 tgt_lng 且非空白:
        归一化该行的 tgt_lng
        if 归一化后 != normalized_out:   # 语言不匹配
            跳过该行（continue）
    加入 loaded_entries
return Glossary(name=glossary_name, entries=loaded_entries)
```

两个关键点：①`tgt_lng` 留空的行被视为「对任何目标语言都适用」，不会被过滤；②归一化是对**行内 `tgt_lng`** 和**整体 `--lang-out`** 各做一次，再比较。

#### 4.1.3 源码精读

先看单条术语的数据结构。`GlossaryEntry` 只是一个简单的三字段容器（[babeldoc/glossary.py:16-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L16-L23)）：

```python
class GlossaryEntry:
    def __init__(self, source: str, target: str, target_language: str | None = None):
        self.source = source
        self.target = target
        self.target_language = target_language
```

`from_csv` 是核心加载逻辑（[babeldoc/glossary.py:123-170](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L123-L170)）。要点：

- **名字来自文件名**：`glossary_name = file_path.stem`（[第 131 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L131)），即去掉扩展名后的文件名。这个名字后面会出现在 LLM 提示里。
- **编码自适应**：用 `chardet.detect` 猜测 CSV 文件编码再解码（[第 138-141 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L138-L141)），因此术语表可以是 UTF-8、GBK 等。
- **列校验**：必须含 `source`、`target` 两列，否则 `raise ValueError`（[第 143-146 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L143-L146)）。
- **tgt_lng 过滤**：核心三行——只有当该行填了非空 `tgt_lng` **且**归一化后不等于目标语言时才 `continue` 跳过（[第 153-158 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L153-L158)）：

```python
if tgt_lng and tgt_lng.strip():
    normalized_entry_tgt_lng = tgt_lng.strip().lower().replace("-", "_")
    if normalized_entry_tgt_lng != normalized_target_lang_out:
        continue  # Skip if language doesn't match
```

注意 `normalized_target_lang_out` 在循环外只算了一次（[第 135 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L135)），这是个小优化。

CLI 侧的加载入口在 `main.py`：`--glossary-files` 接收逗号分隔的多个路径（[babeldoc/main.py:278-283](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L278-L283)），随后对每个路径调用 `Glossary.from_csv(file_path, args.lang_out)`（[babeldoc/main.py:584-608](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L584-L608)）。注意它把 `args.lang_out` 作为 `target_lang_out` 传入，这就是 `tgt_lng` 过滤的「整体目标语言」来源。加载后只把**含条目**的术语表（`glossary_obj.entries` 非空）收进 `loaded_glossaries`，空表会被丢弃并记日志。

加载完的 `loaded_glossaries` 最终会传入 `TranslationConfig`（[babeldoc/main.py:713](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L713)），由 `SharedContextCrossSplitPart.initialize_glossaries` 收纳（见 4.4）。

#### 4.1.4 代码实践：手写一个三列术语表并加载

**实践目标**：验证 `from_csv` 的 `tgt_lng` 过滤行为与归一化规则。

**操作步骤**：

1. 参照 `docs/example/demo_glossary.csv`，新建一个 `my_terms.csv`，内容如下（示例代码）：

   ```csv
   source,target,tgt_lng
   Transformer,Transformer,zh-CN
   BabelDOC,BabelDOC 文档翻译器,zh-CN
   GPU,グラフィックボックス,ja-JP
   CPU,通用术语 CPU,
   ```

2. 写一个最小脚本（示例代码）：

   ```python
   from pathlib import Path
   from babeldoc.glossary import Glossary

   # 翻译到中文：应保留 zh-CN 与通用条目，过滤掉 ja-JP
   g_zh = Glossary.from_csv(Path("my_terms.csv"), target_lang_out="zh-CN")
   print("zh-CN 加载条目数:", len(g_zh.entries))
   for e in g_zh.entries:
       print(" ", e)

   # 翻译到日语：应保留 ja-JP 与通用条目，过滤掉 zh-CN
   g_ja = Glossary.from_csv(Path("my_terms.csv"), target_lang_out="ja_JP")
   print("ja-JP 加载条目数:", len(g_ja.entries))
   ```

**需要观察的现象**：
- `g_zh` 应有 3 条（`Transformer`、`BabelDOC`、`CPU`），`GPU`（`ja-JP`）被过滤。
- `g_ja` 也应有 3 条（`GPU`、`CPU`，以及……注意 `Transformer`/`BabelDOC` 是 `zh-CN` 会被过滤），所以是 `GPU` + `CPU` 两条。

**预期结果**：`target_lang_out="ja_JP"` 用下划线写法，仍然能匹配 CSV 里用连字符写的 `ja-JP`，证明归一化生效。CPU 那条 `tgt_lng` 为空，在两种语言下都被保留。

> 待本地验证：不同 `hyperscan` 版本编译耗时略有差异，但条目数结论不受影响。

#### 4.1.5 小练习与答案

**练习 1**：如果一条术语的 `tgt_lng` 写成 `ZH_cn`，而 `--lang-out` 是 `zh-CN`，它会被加载吗？
**答案**：会。两者归一化后都是 `zh_cn`（小写 + 连字符转下划线），相等，通过过滤。

**练习 2**：为什么 `from_csv` 把 `normalized_target_lang_out` 放在循环**外**计算？
**答案**：因为整体目标语言对每一行都相同，预先算一次避免在每行重复 `.lower().replace()`，是个微优化。

### 4.2 Hyperscan 高性能正则匹配

#### 4.2.1 概念说明

术语表加载后，下一步是**对每一段待翻译文本，找出其中出现了哪些术语**。这是术语注入的前提。

朴素做法是对每条术语调一次 `re.search`，复杂度为 \(O(\text{条数} \times \text{段落数})\)。当术语表有上万条、文档有上千段时，这个开销很大。

BabelDOC 的做法是：在 `Glossary` 构造时，把**所有源术语编译成一个 Hyperscan 数据库**（`hs_dbs`）。之后每段文本只需对这个数据库 `scan` 一次，Hyperscan 内部用 Aho-Corasick / 自动机 + SIMD 一次性找出所有命中，复杂度接近 \(O(\text{文本长度})\)，与术语条数几乎无关。

为避免单个数据库过大，BabelDOC 把术语按**每 2 万条**分块，每块编译成一个独立的 `hyperscan.Database`，扫描时依次跑每个块。

#### 4.2.2 核心流程

构建阶段 `_build_regex_and_lookup`（构造时调用一次）：

```
对每条 entry:
    normalized_key = normalize_source(entry.source)     # 归一化 key
    normalized_lookup[normalized_key] = (source, target)
    id_lookup.append((source, target))                  # 按 idx 取值
    hs_pattern.append((re.escape(entry.source), idx))   # 模式用「原始 source」转义
按 chunk_size = 20000 分块:
    for 每一块:
        expressions, ids = 解压该块
        hs_db = hyperscan.Database()
        hs_db.compile(expressions, ids, flags=CASELESS | SINGLEMATCH)
        hs_dbs.append(hs_db)
```

匹配阶段 `get_active_entries_for_text(text)`（每段文本调用）：

```
text = 归一化文本空白
active_entries = []
def on_match(idx, _from, _to, _flags, _ctx):
    active_entries.append(id_lookup[idx])   # 用命中模式的 idx 还原 (source, target)
    return False                            # 继续扫描，不中止
for hs_db in hs_dbs:
    scratch = hyperscan.Scratch(hs_db)
    hs_db.scan(text.encode("utf-8"), on_match, scratch=scratch)
return active_entries
```

两个标志位的含义：
- `HS_FLAG_CASELESS`：大小写不敏感。所以源术语 `AutoML` 能匹配文本里的 `automl`。
- `HS_FLAG_SINGLEMATCH`：每个模式在一次扫描中**至多报告一次**。术语 `Transformer` 在段落里出现 5 次，也只回调一次，天然去重。

分块数量的计算：若术语条数为 \(n\)，块大小为 \(c = 20000\)，则块数约为

\[
\text{chunks} = \left\lceil \frac{n}{c} \right\rceil
\]

（源码日志里用 `n // c + 1` 作为上界近似，见 [第 98 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L98)）。

> 一个容易踩坑的细节：模式用的是 **`re.escape(entry.source)`（原始 source，保留原始空白）**，而 `normalized_lookup` 的 key 用的是 **归一化后的 source**。文本侧只做了空白归一化（多个空白合一个）。因此**若源术语本身含不规则空白（如制表符、多空格），它不会被空白归一化、仍按原样匹配**。实践中建议源术语写规范单空格。

#### 4.2.3 源码精读

`normalize_source` 是源术语归一化方法（[babeldoc/glossary.py:59-66](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L59-L66)）：转小写 + 连续空白合成单空格 + `strip`。它在两处被用：构造时去重（[第 48 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L48)）和构建 lookup key。

> 注意一个有趣的实现细节：[第 37 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L37) 用的是 `re.compile(r"\s+", regex.UNICODE)`——`re.compile` 来自标准库 `re`，而 `regex.UNICODE` 来自第三方 `regex` 库。两者都定义了 `UNICODE` 常量且数值兼容，所以能跑通；但这是一个跨库混用的写法，读源码时不必困惑。

`_build_regex_and_lookup` 构建 Hyperscan 数据库（[babeldoc/glossary.py:68-121](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L68-L121)）。核心片段（[第 86-111 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L86-L111)）：

```python
for idx, entry in enumerate(self.entries):
    normalized_key = self.normalize_source(entry.source)
    self.normalized_lookup[normalized_key] = (entry.source, entry.target)
    self.id_lookup.append((entry.source, entry.target))
    hs_pattern.append((re.escape(entry.source).encode("utf-8"), idx))

chunk_size = 20000
for i, pattern_chunk in enumerate(batched(hs_pattern, chunk_size, strict=False)):
    expressions, ids = zip(*pattern_chunk, strict=False)
    hs_db = hyperscan.Database()
    hs_db.compile(
        expressions=expressions, ids=ids, elements=len(pattern_chunk),
        flags=hyperscan.HS_FLAG_CASELESS | hyperscan.HS_FLAG_SINGLEMATCH,
    )
    self.hs_dbs.append(hs_db)
```

分块用的 `batched` 辅助函数（[babeldoc/glossary.py:26-34](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L26-L34)）是个标准生成器，把可迭代对象切成定长元组。

匹配方法 `get_active_entries_for_text`（[babeldoc/glossary.py:193-214](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L193-L214)）。核心是 `on_match` 回调——Hyperscan 每命中一个模式就回调它，传入该模式的 `idx`，回调用 `idx` 去 `id_lookup` 取回 `(original_source, target)`（[第 204-208 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L204-L208)）：

```python
def on_match(idx, _from, _to, _flags, _context=None):
    active_entries.append(self.id_lookup[idx])
    return False

for hs_db in self.hs_dbs:
    scratch = hyperscan.Scratch(hs_db)
    hs_db.scan(text.encode("utf-8"), on_match, scratch=scratch)
return active_entries
```

注意返回的是 `(original_source, target)`，**source 是原始大小写的原文术语**（不是归一化小写），这样填进 LLM 提示表格时更自然。

#### 4.2.4 代码实践：验证术语命中

**实践目标**：亲手调用 `get_active_entries_for_text`，观察 Hyperscan 的大小写不敏感与单次匹配。

**操作步骤**（示例代码）：

```python
from babeldoc.glossary import Glossary, GlossaryEntry

# 用代码直接构造一个术语表（绕过 CSV）
g = Glossary(
    name="demo",
    entries=[
        GlossaryEntry("AutoML", "自动机器学习"),
        GlossaryEntry("GPU", "图形处理器"),
        GlossaryEntry("nonexistent_term_xyz", "不会出现"),
    ],
)

text = "We use AutoML and automl on a GPU, GPU, GPU."
hits = g.get_active_entries_for_text(text)
print("命中:", hits)
```

**需要观察的现象**：
- `AutoML` 与 `automl` 都命中，但因 `SINGLEMATCH`，`AutoML` 只出现一次。
- `GPU` 在文本里出现 3 次，也只命中一次。
- `nonexistent_term_xyz` 不出现。

**预期结果**：`hits` 大致为 `[('AutoML', '自动机器学习'), ('GPU', '图形处理器')]`（顺序由 Hyperscan 报告顺序决定）。注意 `automl` 命中后回填的是 `('AutoML', ...)` 原始大小写——这正是 `id_lookup[idx]` 还原原始 source 的效果。

#### 4.2.5 小练习与答案

**练习 1**：术语表有 45000 条，会被编译成几个 Hyperscan 数据库？
**答案**：3 个。\( \lceil 45000/20000\rceil = 3 \)。

**练习 2**：如果去掉 `HS_FLAG_SINGLEMATCH`，同一段文本里一个术语出现多次会发生什么？
**答案**：`on_match` 会被回调多次，`active_entries` 里出现重复的 `(source, target)`。当前靠 `SINGLEMATCH` 在引擎层去重，下游 `_build_glossary_block` 还会再 `sorted()`，所以即使多次命中最终表格也不会重复。

### 4.3 术语注入翻译提示

#### 4.3.1 概念说明

匹配到术语不是终点——BabelDOC **不会**直接替换文本里的术语（那会破坏占位符和富文本结构）。它的做法是把命中的术语拼成一段 **Markdown 表格提示**，插进给 LLM 的翻译提示里，要求 LLM「凡是遇到 Source Term，一律用 Target Term」。

这样做的好处：原文结构（公式占位符 `{v1}`、样式标签 `<style>`）完全不动，一致性交给 LLM 按 prompt 执行。这也是术语表能和 u6-l2 的占位符系统和平共处的原因。

#### 4.3.2 核心流程

在 `ILTranslator` 初始化时，一次性把翻译要用的术语表缓存下来（避免每段重复查询配置）：

```
_cached_glossaries = shared_context.get_glossaries_for_translation(auto_extract_glossary)
```

每翻译一段文本 `text`，构建提示时：

```
glossary_block = "" 
for glossary in _cached_glossaries:
    active = glossary.get_active_entries_for_text(text)   # Hyperscan 匹配
    if active:
        按术语表 name 分组 → 命中条目排序
拼成 Markdown:
    ## Glossary
    Always use the glossary's Target Term for any Source Term ...
    ### Glossary: <名字>
    | Source Term | Target Term |
    | original_source | target |
把 glossary_block 填入 PROMPT_TEMPLATE 的 $glossary_block 占位
```

关键点：**只有命中术语的术语表才会出现在提示里**（命中为空的表不注入），既省 token 又减少噪声。

#### 4.3.3 源码精读

术语表缓存在 `ILTranslator.__init__` 中建立（[babeldoc/format/pdf/document_il/midend/il_translator.py:349-354](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L349-L354)）：

```python
# Cache glossaries at initialization
self._cached_glossaries = (
    self.shared_context_cross_split_part.get_glossaries_for_translation(
        self.translation_config.auto_extract_glossary
    )
)
```

提示模板 `PROMPT_TEMPLATE` 中预留了 `$glossary_block` 占位（[babeldoc/format/pdf/document_il/midend/il_translator.py:63](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L63)），位于「规则」与「上下文」之间。

`_build_glossary_block` 是注入逻辑（[babeldoc/format/pdf/document_il/midend/il_translator.py:1090-1132](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1090-L1132)）。核心片段（[第 1102-1132 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1102-L1132)）：

```python
glossary_entries_per_glossary: dict[str, list[tuple[str, str]]] = {}
for glossary in self._cached_glossaries:
    active_entries = glossary.get_active_entries_for_text(text)
    if active_entries:
        glossary_entries_per_glossary[glossary.name] = sorted(active_entries)
...
for glossary_name, entries in glossary_entries_per_glossary.items():
    glossary_block_lines.append(f"### Glossary: {glossary_name}")
    glossary_block_lines.append("| Source Term | Target Term |\n|-------------|-------------|")
    for original_source, target_text in entries:
        glossary_block_lines.append(f"| {original_source} | {target_text} |")
```

注意三件事：①按 `glossary.name` 分组，每张表一个 `### Glossary: <名字>` 小节；②条目 `sorted(active_entries)` 再列出，保证提示稳定可复现；③指令明确要求「即使术语被拆行、嵌在标签里、有变体也要用 Target Term」（[第 1115-1116 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1115-L1116)）。

最后 `generate_prompt_for_llm` 把三段拼装进模板（[babeldoc/format/pdf/document_il/midend/il_translator.py:1134-1164](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1134-L1164)）：

```python
glossary_block = self._build_glossary_block(text)
return PROMPT_TEMPLATE.substitute(
    role_block=role_block,
    glossary_block=glossary_block,
    context_block=context_block,
    lang_out=self.translation_config.lang_out,
    text_to_translate=text,
)
```

> 说明：`ILTranslatorLLMOnly`（u6-l3）也复用了同样的术语表机制——它的提示模板里同样有 `$glossary_block` / `$glossary_tables_block` 占位。术语注入是两个翻译器共用的横切能力。

#### 4.3.4 代码实践：阅读测试断言理解注入

**实践目标**：理解「命中才有提示、未命中为空」的行为。

**操作步骤**：阅读 `_build_glossary_block` 的两个早退分支——[第 1099-1100 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1099-L1100)（无缓存术语表时返回 `""`）与 [第 1109-1110 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1109-L1110)（所有表都未命中时返回 `""`）。

**需要观察的现象**：当一段文本不含任何术语时，`glossary_block` 为空字符串，`PROMPT_TEMPLATE.substitute` 把 `$glossary_block` 替换成空，提示里不会出现 `## Glossary` 段。

**预期结果**：这保证了「无关段落不付术语 token 开销」。

> 待本地验证：可在 `_build_glossary_block` 入口加一行 `logger.debug(text[:40])`，开 `--debug` 翻译，观察日志里哪些段落真正带上了术语表。

#### 4.3.5 小练习与答案

**练习**：为什么不直接把整张术语表塞进提示，而要先按文本匹配？
**答案**：①省 token —— 大术语表全塞进去会爆炸式增加每段请求的输入长度与费用；②降噪 —— 只给 LLM 看与当前段落相关的术语，减少被无关术语干扰译错的风险；③匹配本身由 Hyperscan 高效完成，开销可忽略。

### 4.4 术语命名来源与多术语表协作

#### 4.4.1 概念说明

实际使用中可能同时存在**多张术语表**：用户用 `--glossary-files a.csv,b.csv` 传了两张表，再加上 u6-l4 自动抽取的 `auto_extracted_glossary`。BabelDOC 需要解决两个问题：

1. **命名**：每张表要有个名字，这个名字会出现在 LLM 提示的 `### Glossary: <名字>` 里。用户表的名字来自文件名；自动表的名字是固定前缀加防冲突后缀。
2. **取舍**：自动术语表和用户术语表**同时存在时用谁**？BabelDOC 的策略是——当开启自动抽取且有自动表时，**翻译阶段只用自动表**（它已吸收全文统计，质量更统一）；否则用「用户表 + 自动表」合并。

所有术语表都挂在 `SharedContextCrossSplitPart` 上，这个对象**跨分片共享**（见 u8-l2），保证分片翻译时术语记忆全局一致。

#### 4.4.2 核心流程

```
用户 CSV → from_csv → name = 文件名
                                 ↓
TranslationConfig 构造 → initialize_glossaries(user_glossaries)
                                 ↓
SharedContextCrossSplitPart:
    user_glossaries = [...]                       # 用户表
    auto_extracted_glossary = None                # 翻译前由 u6-l4 填充
    norm_terms = 所有用户表归一化 source 的并集   # 供自动抽取「去重/参考」用

u6-l4 自动抽取跑完后:
    finalize_auto_extracted_glossary()  # 多数投票汇总成 auto_extracted_glossary

ILTranslator 翻译时:
    _cached_glossaries = get_glossaries_for_translation(auto_extract_enabled)
    # 若 auto_extract_enabled 且有 auto 表 → 只返回 [auto 表]
    # 否则 → 返回 用户表 + auto 表
```

#### 4.4.3 源码精读

用户术语表的**名字来自文件名**（[babeldoc/glossary.py:131](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L131)：`glossary_name = file_path.stem`），随后透传给 `Glossary.__init__`（[第 41 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L41)）。这个名字最终出现在提示里（[il_translator.py:1123](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1123)：`### Glossary: {glossary_name}`）。

`SharedContextCrossSplitPart` 同时持有两类术语表（[babeldoc/format/pdf/translation_config.py:39-41](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L39-L41)）：

```python
self.user_glossaries: list[Glossary] = []
self.auto_extracted_glossary: Glossary | None = None
self.raw_extracted_terms: list[tuple[str, str]] = []
```

`initialize_glossaries` 在配置构造时被调用（[babeldoc/format/pdf/translation_config.py:56-70](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L56-L70)），收纳用户表并构建 `norm_terms` 集合——后者把所有用户表的归一化源术语合并，供 u6-l4 自动抽取时作为「参考术语表」避免重复抽取（见 u6-l4）。

自动抽取后的**多数投票汇总**在 `finalize_auto_extracted_glossary`（[babeldoc/format/pdf/translation_config.py:99-121](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L99-L121)）：对每个源术语，用 `Counter(tgts).most_common(1)[0][0]` 选出出现最多的译文，组装成 `Glossary(name=self.unique_name, ...)`。

自动表的名字由 `_generate_unique_auto_glossary_name` 生成（[babeldoc/format/pdf/translation_config.py:76-90](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L76-L90)）：基准名 `auto_extracted_glossary`，若与某张用户表重名则追加 `#1`、`#2` 后缀——因为用户完全可能把自己的 CSV 命名为 `auto_extracted_glossary.csv`。

**翻译时用哪些表**由 `get_glossaries_for_translation` 决定（[babeldoc/format/pdf/translation_config.py:130-140](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L130-L140)）：

```python
def get_glossaries_for_translation(self, auto_extract_enabled: bool) -> list[Glossary]:
    with self._lock:
        if auto_extract_enabled and self.auto_extracted_glossary:
            return [self.auto_extracted_glossary]      # 只用自动表
        else:
            all_glossaries = list(self.user_glossaries)
            if self.auto_extracted_glossary:
                all_glossaries.append(self.auto_extracted_glossary)
            return all_glossaries                       # 用户表 + 自动表
```

这就是 4.3 里 `_cached_glossaries` 的来源。注意它加了 `self._lock`，因为分片并发翻译时多线程会并发读这些表。

#### 4.4.4 代码实践：对比两种术语表取舍

**实践目标**：理解「自动表开启时替代用户表」的策略。

**操作步骤**（源码阅读型实践）：

1. 阅读 `get_glossaries_for_translation`（[第 130-140 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L130-L140)），记录两个分支的返回值差异。
2. 结合 u6-l4 的门控逻辑：`auto_extract_glossary` 默认为 `True`，可被 `--no-auto-extract-glossary`（[babeldoc/main.py:294-299](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L294-L299)）关闭。

**需要观察的现象**：分四种组合思考 `_cached_glossaries` 的内容——

| `auto_extract_enabled` | 有自动表？ | `_cached_glossaries` |
| --- | --- | --- |
| True | 是 | `[auto_extracted_glossary]`（用户表被替代） |
| True | 否 | `[用户表...]` |
| False | 是 | `[用户表..., auto_extracted_glossary]` |
| False | 否 | `[用户表...]` |

**预期结果**：能口头复述「自动抽取开启且产出了自动表时，翻译阶段优先信任自动表」这一设计意图——因为自动表是基于全文统计的、覆盖最全、译文最一致。

#### 4.4.5 小练习与答案

**练习**：用户把术语表命名为 `auto_extracted_glossary.csv`，会和自动抽取表冲突吗？
**答案**：不会。`_generate_unique_auto_glossary_name` 检测到 `auto_extracted_glossary` 已被用户表占用，会给自动表改名成 `auto_extracted_glossary#1`，避免提示里出现两个同名小节。

## 5. 综合实践

把本讲四个模块串起来，完成一个端到端的「术语表验证」小任务：

1. **准备术语表**：写一个 `physics.csv`（示例代码）：

   ```csv
   source,target,tgt_lng
   Large Language Model,大语言模型,zh-CN
   hallucination,幻觉,
   tokenizer,分词器,zh-CN
   ```

2. **加载并匹配**：用 `Glossary.from_csv(Path("physics.csv"), "zh-CN")` 加载，确认 `hallucination`（无 `tgt_lng`）也被加载。

3. **手动模拟注入**：对文本 `"Large Language Models often suffer from hallucination."` 调用 `get_active_entries_for_text`，打印命中。注意 `Large Language Model`（单数术语）能否命中复数 `Large Language Models`？思考为什么（提示：模式是 `re.escape` 后的**子串**字面匹配，Hyperscan 找子串，所以 `Large Language Model` 是 `Large Language Models` 的子串，能命中）。

4. **拼提示**：参考 `_build_glossary_block` 的逻辑，手工把命中结果拼成一段 Markdown 表格，对照 [第 1112-1130 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1112-L1130) 检查格式是否一致。

5. **反思**：把同一段文本放进 `_build_glossary_block`（需要构造 `ILTranslator`，或直接复刻其逻辑），验证「未命中术语 → 返回空串」。

> 待本地验证：第 3 步的「单数命中复数」结论取决于 Hyperscan 的子串语义；若想严格词边界匹配，需在 `re.escape` 外再加 `\b` 边界，但当前实现并未这样做——这正是值得在二次开发时考虑的改进点。

## 6. 本讲小结

- `Glossary` 是术语表的统一数据结构：无论术语来自用户 CSV 还是 u6-l4 自动抽取，都汇入它。
- `from_csv` 用 `chardet` 自适应编码、`csv.DictReader` 解析，强制要求 `source`/`target` 列；`tgt_lng` 列经「小写 + 连字符转下划线」归一化后与 `--lang-out` 比较，不匹配则跳过，留空则通用。
- 匹配引擎是 **Hyperscan**：构造时把每条源术语 `re.escape` 后编译进数据库（`CASELESS` + `SINGLEMATCH`），按 2 万条分块；匹配时 `on_match` 回调用模式 `idx` 去 `id_lookup` 还原 `(原始 source, target)`，一次扫描找出全部命中。
- 命中术语不会替换原文，而是由 `_build_glossary_block` 拼成 Markdown 表格注入 LLM 提示（`$glossary_block` 占位），未命中的表/段落不注入，省 token。
- 术语表名字来自 CSV 文件名（自动表用 `auto_extracted_glossary` 加防冲突后缀），出现在提示的 `### Glossary: <名字>` 小节。
- `get_glossaries_for_translation` 决定翻译用哪些表：自动抽取开启且有自动表时**只用自动表**，否则用「用户表 + 自动表」合并；所有表挂在跨分片共享的 `SharedContextCrossSplitPart` 上。

## 7. 下一步学习建议

- **u8-l2 分片翻译与结果合并**：术语表挂在 `SharedContextCrossSplitPart` 上正是为了跨分片共享，去读 `split_manager.py` / `result_merger.py` 看这个共享上下文如何在多分片间传递。
- **u6-l2 IL 翻译编排**：回顾占位符系统 `{v1}` / `<style>`，理解为什么术语注入必须走「提示」而非「字符串替换」——两者共用同一段提示模板。
- **二次开发方向**：若需要词边界匹配、模糊匹配或多语言混合术语表，可在 `_build_regex_and_lookup` 的 `re.escape` 处改造模式，或在 `from_csv` 的过滤逻辑上扩展，是练习扩展点的好位置。
- **继续阅读**：`hyperscan` 库的官方文档了解 `Scratch`、编译 flags 的更多选项；`chardet` / `csv` 标准库文档巩固加载侧知识。
