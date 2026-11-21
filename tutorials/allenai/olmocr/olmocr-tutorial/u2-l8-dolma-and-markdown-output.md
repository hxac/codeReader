# 输出侧：Dolma 文档与 Markdown 落盘

## 1. 本讲目标

前面几讲我们跟随一页 PDF 走完了「渲染 → 提示词 → 推理请求 → front matter 解析 → 重试与旋转纠错」的全过程。本讲来到流水线的最后一站——**输出侧**。学完本讲，你应该能够：

1. 说出一份 Dolma 文档 JSON 的完整结构：`id` / `text` / `source` / `added` / `created` / `metadata` / `attributes` 各自存什么。
2. 解释 `pdf_page_spans` 如何用字符偏移把整篇文档文本切回「第几页」，以及换行符归属哪一页。
3. 理解 `--max_page_error_rate`（默认 0.004，即 1/250）如何让一篇 fallback 页过多的文档被整篇淘汰。
4. 描述 worker 如何把一组 Dolma 文档写成 `results/output_{hash}.jsonl`，以及 `--markdown` 如何按源目录镜像落盘。
5. 手推 `get_markdown_path` 对 `s3://`、`tarball::`、本地绝对路径、含 `..` 路径的清洗结果。

## 2. 前置知识

- **Dolma 格式**：Dolma 是 AI2 的预训练语料框架，其最小单位是「文档 JSON」：`text` 是纯文本正文，`id` 是文档唯一标识，`metadata` 记录来源信息，`attributes` 存放与文本或页对齐的标注向量。olmOCR 的定位之一就是「预训练语料生产工具」，所以它的主输出是 Dolma 格式（第 1 单元讲过：Dolma 与 Markdown 是两种输出形态）。
- **JSONL**：JSON Lines，每行一个独立 JSON 对象，流式可追加、可逐行消费，是大批量数据处理的标准交换格式。
- **字符偏移（span）**：用 `[起始位置, 结束位置]` 两个整数在字符串上切出一个片段，遵循「左闭右开」约定：`text[start:end]`。Python 切片天然支持这种约定。
- **sha1 内容寻址**：`hashlib.sha1(text.encode()).hexdigest()` 对文本内容做哈希。两份内容相同的文档会得到相同 `id`——这是天然的去重键。
- **承接前讲**：u2-l5 讲过 `FrontMatterParser` 把模型输出的 YAML front matter 剥离，`response.natural_text` 只是纯正文；u2-l6 讲过重试耗尽后 `make_fallback_result` 会用 pdftotext 兜底并打 `is_fallback=True` 标记。本讲要处理的正是这些「每页一个 `PageResult`」如何汇成「每篇一个 Dolma 文档」。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `olmocr/pipeline.py` | L94-L103 | `PageResult`：单页结果的数据载体（本讲输入） |
| `olmocr/pipeline.py` | L233-L250 | `make_fallback_result`：构造 fallback 页的参考模板 |
| `olmocr/pipeline.py` | L518-L572 | `process_single_pdf`：页结果汇总 + `max_page_error_rate` 淘汰门槛 |
| `olmocr/pipeline.py` | L602-L651 | `build_dolma_document`：**本讲主角一**，拼装 Dolma JSON |
| `olmocr/pipeline.py` | L654-L695 | `get_markdown_path`：**本讲主角二**，源路径 → 安全 Markdown 路径 |
| `olmocr/pipeline.py` | L698-L804 | `worker`：**本讲主角三**，把结果真正写到磁盘/S3 |
| `olmocr/pipeline.py` | L1222, L1228 | `--max_page_error_rate` 与 `--markdown` 两个 CLI 参数定义 |
| `olmocr/datatypes.py` | L7-L33 | 旧版 `PdfOutput.mk_dolma_doc`（历史遗留，仅作对照） |
| `tests/test_pipeline.py` | L516-L570 | `TestMarkdownPathHandling`：路径清洗的回归测试 |

另外本讲会顺带引用 [olmocr/pipeline.py:210-212](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L210-L212)（front matter 剥离点）和 [olmocr/pipeline.py:1162](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1162)（`--stats` 回读输出文件的正则）。

## 4. 核心概念与源码讲解

### 4.1 build_dolma_document：把页结果拼成一份 Dolma 文档

#### 4.1.1 概念说明

流水线内部的基本单位是「页」：每个页面独立请求模型、独立重试、独立解析成一个 `PageResult`。但消费端（预训练语料、人工阅读）需要的基本单位是「文档」。`build_dolma_document` 就是这中间的**聚合器**：把 N 个 `PageResult` 拼成一份带页级标注的 Dolma JSON。

它要解决三个问题：

1. **拼接**：把各页 `natural_text` 顺序连成一篇 `text`，同时记录每一页的字符区间，否则拼接后就无法回答「这段文字来自第几页」。
2. **内容寻址**：生成一个由文本内容决定的 `id`，让下游可以按内容去重。
3. **携带标注**：语言、旋转、表格/图表等页级信息（u2-l4 讲的 `PageResponse` 字段）不能丢，要作为 `attributes` 附着在文档上。

注意一个前置门槛：`build_dolma_document` 不是所有页结果的「无条件聚合器」。调用它的 `process_single_pdf` 会先检查 fallback 页占比，超标则整篇丢弃——这正是学习目标 3 的 `max_page_error_rate` 淘汰逻辑。

#### 4.1.2 核心流程

```text
process_single_pdf（调用方）
  ├── 收集 num_pages 个 PageResult（每页一个 Task）
  ├── assert 所有页 is_valid
  ├── 统计 fallback 页数 num_fallback_pages
  ├── 若 num_fallback_pages / num_pages > max_page_error_rate
  │       └── 整篇丢弃，返回 None（不再进入 build）
  └── build_dolma_document(pdf_orig_path, page_results)
          ├── 逐页拼接 text，同步记录 span
          ├── 全文为空 → 返回 None
          ├── 组装 metadata（来源、版本、token 统计……）
          ├── id = sha1(text)
          └── 组装 attributes（页 span + 各页标注向量）
```

拼接与 span 记录的数学关系：设第 \(i\) 页（0 起索引）的正文片段为 \(c_i\)（`natural_text` 为 `None` 时取空串；非末页额外追加一个换行符 `\n`），全文 \(T = c_0 c_1 \cdots c_{n-1}\)，则该页的 span 为

\[
\text{start}_i = \sum_{j<i} |c_j|, \qquad \text{end}_i = \text{start}_i + |c_i|, \qquad T[\text{start}_i:\text{end}_i] = c_i
\]

即每页 span 首尾相接、无缝覆盖全文，且**页与页之间的分隔换行符计入前一页的 span**（因为 `\n` 是追加在 \(c_i\) 末尾的）。`pdf_page_spans` 中每个元素是三元组 `[start, end, page_num]`，`page_num` 是 PDF 里的 1 起始页码（u2-l3 讲过页码从 1 开始）。

淘汰比例的判定则是简单的不等式：

\[
\frac{\#\text{fallback 页}}{\#\text{总页数}} > r \quad\Rightarrow\quad \text{丢弃整篇}, \qquad r = 0.004 = \frac{1}{250}
\`

例如一篇 10 页的文档只要有 1 页 fallback（0.1 > 0.004）就会被丢弃；而一篇 1000 页的文档允许最多 4 页 fallback。

#### 4.1.3 源码精读

**输入数据结构 `PageResult`**——注意第一个字段名叫 `s3_path`，但实际存的是原始源路径（可以是 S3、本地或 `tarball::成员` 形式），是历史命名：

- [olmocr/pipeline.py:94-103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L94-L103) 定义了冻结数据类 `PageResult`：携带源路径、页码、解析好的 `PageResponse`、本次请求的输入/输出 token 数，以及 `is_fallback` / `is_valid` 两个状态位。本讲的实践就是手工构造它。

**淘汰门槛**——`process_single_pdf` 在调用 build 之前做整篇裁决：

- [olmocr/pipeline.py:553-569](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L553-L569) 先断言所有页 `is_valid`（u2-l6 讲过：`process_page` 永远会返回一个结果——最差也是 fallback——所以这个断言是安全网）；然后统计 `num_fallback_pages`，占比超过 `args.max_page_error_rate` 就记 error 日志并 `return None`（整篇不出现在输出里）；占比超标线以内但有 fallback 页则只给 warning，继续走 build。这样「质量淘汰」被推迟到整篇粒度裁决，而不是单页粒度——正是 u2-l6 结论「单页永不阻塞流水线」在输出侧的兑现。
- 参数默认值在 [olmocr/pipeline.py:1222](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1222)：`--max_page_error_rate` 默认 0.004，帮助文本写明「1/250 by default」。

**拼接与 span 记录**：

- [olmocr/pipeline.py:603-621](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L603-L621) 遍历 `page_results`：`natural_text` 非 `None` 时取正文，**非末页追加 `"\n"`**；`None` 时取空串（空白页/纯图页也会占一个 span，只是长度可能为 0）。每页记录 `[start_pos, current_char_pos, page_num]`。如果拼完全文是空串，说明整篇没有任何文字，直接返回 `None`——这份 PDF 不会出现在输出中。

**metadata 与内容寻址 id**：

- [olmocr/pipeline.py:624-633](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L624-L633) 组装 `metadata`：`Source-File`（原始路径）、`olmocr-version`（版本号，可追溯是哪个模型产出的）、`pdf-total-pages`、输入/输出 token 总量、fallback 页总数。然后 `id_ = hashlib.sha1(document_text.encode()).hexdigest()`——**id 是全文的 sha1**，内容相同则 id 相同。
- 一个细节：`added` / `created` 用 `datetime.datetime.now()` 当天日期，同一文档重跑会得到相同 `id` 但不同 `added`。

**最终 JSON 结构**：

- [olmocr/pipeline.py:635-651](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L635-L651) 组装出完整的 Dolma 文档字典并返回（注意返回的是 **dict**，不是 JSON 字符串——序列化推迟到 worker 写文件时）。

各字段一览：

| 字段 | 内容 | 说明 |
| --- | --- | --- |
| `id` | sha1(text) | 内容寻址，天然去重键 |
| `text` | 各页 natural_text 顺序拼接 | 正文本体（front matter 已在上游剥离） |
| `source` | `"olmocr"` | 语料来源标签 |
| `added` / `created` | 当天日期字符串 | 格式 `%Y-%m-%d` |
| `metadata` | 来源/版本/页数/token 统计 | 整篇粒度的元信息 |
| `attributes.pdf_page_numbers` | `[[start, end, page_num], ...]` | 字符偏移 → 页码映射（span） |
| `attributes.primary_language` 等 | 与页对齐的列表 | 其余 5 个 key 均为逐页列表 |

**历史对照**：仓库里还有一个旧版实现 [olmocr/datatypes.py:7-33](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/datatypes.py#L7-L33) 的 `PdfOutput.mk_dolma_doc`：`source` 是 `"s2pdf"`、没有 `attributes`、metadata 里也没有 token 统计。u1-l4 已经提过 `datatypes.py` 是无人引用的历史遗留——它反映了项目从「S2 论文 PDF 转换」演化到「通用 olmOCR 流水线」的痕迹。

#### 4.1.4 代码实践

1. **实践目标**：手工构造两个 `PageResult`（一个正常页、一个 fallback 页），调用 `build_dolma_document`，验证 id 与 span 的正确性。
2. **操作步骤**：在装有 `pip install olmocr`（轻量安装即可，本实践不需要 GPU/torch）的环境里，新建 `dolma_practice.py`（**示例代码**，非项目原有文件，建议放在仓库外的临时目录）：

   ```python
   import hashlib
   from olmocr.pipeline import PageResult, build_dolma_document
   from olmocr.prompts import PageResponse

   def fake_page(page_num, text, fallback=False, lang="en"):
       # 字段参考 make_fallback_result（pipeline.py:233-250）
       return PageResult(
           s3_path="s3://bucket/papers/report.pdf",  # 字段名叫 s3_path，实际存原始源路径
           page_num=page_num,
           response=PageResponse(
               natural_text=text,
               primary_language=lang,
               is_rotation_valid=True,
               rotation_correction=0,
               is_table=False,
               is_diagram=False,
           ),
           input_tokens=1200,
           output_tokens=800,
           is_fallback=fallback,
           is_valid=True,
       )

   page1_text = "# 第一章\n\n这是第一页的正文。"
   page2_text = "这是第二页的正文，来自 pdftotext 兜底。"

   results = [
       fake_page(1, page1_text),
       fake_page(2, page2_text, fallback=True, lang=None),
   ]

   doc = build_dolma_document("s3://bucket/papers/report.pdf", results)

   # 1) 验证 id 是全文 sha1
   assert doc["id"] == hashlib.sha1(doc["text"].encode()).hexdigest()

   # 2) 验证 span 能切回每页原文（注意：第 1 页 span 末尾含换行符）
   spans = doc["attributes"]["pdf_page_numbers"]
   print("全文 repr:", repr(doc["text"]))
   print("spans:", spans)
   for start, end, page_num in spans:
       print(f"page {page_num}: {doc['text'][start:end]!r}")

   # 3) 观察其余 attributes 与 metadata
   print("primary_language:", doc["attributes"]["primary_language"])
   print("metadata:", doc["metadata"])
   ```

   运行：`python dolma_practice.py`。
3. **需要观察的现象**：第 1 页的 span 是 `[0, len(page1_text) + 1, 1]`（多出的 1 就是追加的 `\n`），第 2 页的 span 从 `len(page1_text) + 1` 开始且恰好到全文末尾；两个 span 无缝相接、并集等于全文长度。`primary_language` 是 `["en", None]`，`metadata` 中 `total-fallback-pages` 为 1。
4. **预期结果**：断言全部通过；把第 1 页的 `natural_text` 改成 `None` 再跑，观察第 1 页 span 变成 `[0, 1, 1]`——长度 1，内容只剩换行符（空正文 + 分隔符）；把两页都改成 `None`，`build_dolma_document` 返回 `None`（空文本分支）。
5. 上述行为基于对源码的直接推演，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一篇 251 页的 PDF，最多允许几页 fallback？252 页呢？

**答案**：判定是严格大于 `num_fallback_pages / num_pages > 0.004`。251 页时 \(251 \times 0.004 = 1.004\)，允许 1 页（\(1/251 \approx 0.00398\)，不超标）；2 页就超标。252 页时 \(252 \times 0.004 = 1.008\)，同样最多 1 页。要允许 2 页至少需要 501 页（\(2/501 \approx 0.00399\)）。

**练习 2**：为什么换行符要计入前一页的 span，而不是后一页？

**答案**：实现上 `"\n"` 是追加在非末页 `content` 的末尾（L610），所以它落在前一页的 `[start, end)` 区间内。这是一种约定选择：页间分隔符视为「上一页的收尾」，使得所有 span 首尾相接、无缝覆盖全文，没有一个字符不属于任何页；若归入后一页，则前一页 span 的 end 与后一页 start 之间会留出「无主」字符。

**练习 3**：两份内容完全相同的 PDF（路径不同）跑完后，输出 JSONL 里会有几条记录？id 各是什么？

**答案**：两条记录（`Source-File` 不同，worker 不会按内容去重），但两条的 `id` 相同——因为 id 只由 `text` 的 sha1 决定，与路径无关。内容级去重是留给下游（如 Dolma 工具链）按 `id` 完成的。

### 4.2 get_markdown_path：从源路径到 workspace 内的安全 Markdown 路径

#### 4.2.1 概念说明

`--markdown` 开启后，olmOCR 会把每篇文档的正文额外写成 `.md` 文件，并且**镜像源文件的目录结构**——`s3://bucket/a/b.pdf` 的输出落在 `workspace/markdown/a/b.md`。这个函数解决两个问题：

1. **路径归一**：源文件可能是 S3 路径、本地绝对路径、或 `tarball::内部路径`（u2-l2 讲过的记法），必须把它们统一映射成 workspace 下的相对路径。
2. **安全**：源路径可能包含 `..`（目录穿越）或绝对路径前导 `/`，若不清洗，markdown 文件可能写到 workspace 之外，甚至覆盖系统文件。`tests/test_pipeline.py` 中的 `TestMarkdownPathHandling` 就是为一起真实回归 bug 写的（绝对路径导致输出写到 workspace 外）。

#### 4.2.2 核心流程

```text
get_markdown_path(workspace, source_file)
  ├── 含 "::" → tarball 分支：剥掉 tarball 后缀名得 basename，拼上内部路径
  ├── 以 "s3://" 开头 → urlparse 取 bucket 之后的 path，去前导 "/"
  ├── 其他（本地路径） → 仅去前导 "/"，变成相对路径
  ├── 清洗：按 "/" 切分，丢弃空段和 ".." 段
  ├── 文件名后缀换成 ".md"
  └── 返回 workspace/markdown/<目录结构>/<名>.md
```

注意 tarball 分支里有个「双层剥后缀」细节：`os.path.splitext` 先剥一层（`docs.tar.gz` → `docs.tar`），若结果还以 `.tar` 结尾再手工剥掉 4 个字符（→ `docs`）。

#### 4.2.3 源码精读

- [olmocr/pipeline.py:665-679](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L665-L679) 三分支归一：tarball 路径按 `::` 切一次（`split("::", 1)`），S3 路径用 `urlparse` 取 `.path` 后 `lstrip("/")`，本地路径直接 `lstrip("/")`。
- [olmocr/pipeline.py:681-684](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L681-L684) 安全清洗：`[p for p in parts if p and p != ".."]` 同时丢弃空段（连续 `//`）与 `..` 段，从构造上杜绝路径逃逸。
- [olmocr/pipeline.py:686-695](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L686-L695) `splitext` 换成 `.md` 后缀，拼出 `workspace/markdown/...` 完整路径。

几个输入 → 输出示例（可用代码验证）：

| 输入 `source_file` | 输出（workspace 为 `/tmp/ws`） |
| --- | --- |
| `s3://bucket/a/b.pdf` | `/tmp/ws/markdown/a/b.md` |
| `/home/ubuntu/docs/sub/f.pdf` | `/tmp/ws/markdown/home/ubuntu/docs/sub/f.md` |
| `docs/sub/f.pdf` | `/tmp/ws/markdown/docs/sub/f.md` |
| `tarballs/docs.tar.gz::inner/f.pdf` | `/tmp/ws/markdown/docs/inner/f.md`（见下方练习 2 的辨析） |
| `documents/../../../etc/passwd.pdf` | `/tmp/ws/markdown/documents/etc/passwd.md` |

- 回归测试：[tests/test_pipeline.py:519-570](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/tests/test_pipeline.py#L519-L570) 四个用例分别覆盖绝对路径、S3 路径、相对路径与 `..` 穿越，全部断言输出留在 workspace 内。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证路径清洗规则，尤其是规格里要求的两类路径。
2. **操作步骤**（**示例代码**）：

   ```python
   from olmocr.pipeline import get_markdown_path

   cases = [
       "s3://bucket/a/b.pdf",
       "/home/ubuntu/docs/subfolder/report.pdf",
       "tarballs/docs.tar.gz::inner/dir/paper.pdf",
       "documents/../../../etc/passwd.pdf",
   ]
   for src in cases:
       print(f"{src:55s} -> {get_markdown_path('/tmp/ws', src)}")
   ```

3. **需要观察的现象**：四个输出都以 `/tmp/ws/markdown/` 开头；`..` 段消失；后缀都变成 `.md`。
4. **预期结果**：与上表一致；可再运行项目自带测试双重确认：`python -m pytest tests/test_pipeline.py -k TestMarkdownPathHandling`（需要 dev 依赖中的 pytest）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么本地绝对路径 `/home/ubuntu/f.pdf` 的输出是 `markdown/home/ubuntu/f.md` 而不是 `markdown/f.md`？

**答案**：本地分支只做 `lstrip("/")`（L679），把绝对路径降级为相对路径，中间目录全部保留。这样不同子目录的同名文件不会互相覆盖；回归测试注释里写明这正是当年 bug 的修复方式——曾经绝对路径会导致输出写到 workspace 之外。

**练习 2**：`tarballs/docs.tar.gz::inner/f.pdf` 走到哪一步？最终文件名是什么？

**答案**：tarball 分支：`splitext(basename("docs.tar.gz"))` 得 `docs.tar`，再因 `.tar` 结尾剥成 `docs`；拼内部路径得 `docs/inner/f.pdf`。随后统一换后缀：文件名部分是 `f.pdf`，`splitext` 只剥 `.pdf` 得 `f`，加 `.md` → `f.md`，所以最终是 `workspace/markdown/docs/inner/f.md`。（若内部成员名本身含多个点，如 `paper.v2.pdf`，则得到 `paper.v2.md`。）

**练习 3**：如果去掉 `p != ".."` 这个过滤条件，攻击者可以用什么输入把文件写到 workspace 之外？

**答案**：例如 `s3://bucket/../../etc/cron.d/evil.pdf`——S3 分支 lstrip 后为 `../../etc/cron.d/evil.pdf`，不过滤 `..` 时最终路径为 `workspace/markdown/../../etc/cron.d/evil.md`，经文件系统解析逃逸到 workspace 外的 `/etc/cron.d/evil.md`。`TestMarkdownPathHandling::test_path_traversal_with_dotdot_stays_in_workspace` 正是防这个的。

### 4.3 worker 结果落盘：output_{hash}.jsonl 与 markdown 目录

#### 4.3.1 概念说明

`build_dolma_document` 只产出内存中的 dict，真正写盘的是 `worker`（u2-l1 讲过 worker 是队列的消费者）。落盘的单位不是「一篇文档」也不是「一个 PDF」，而是**一个 work item**（默认约 500 页一组，u2-l2 讲过 `items_per_group` 的估算）。每个 work item 产出恰好一个 `results/output_{work_item.hash}.jsonl` 文件，文件内每行一篇文档。

这个设计承接了 u1-l3 建立的两个认知：

- **断点续跑**：done flag 只有在整组落盘完成后才写（`mark_done` 在最后一步），重跑未完成组时以整文件覆盖保证最终一致。
- **多机协同**：workspace 在 S3 上时，多个 worker 各写各的 hash 文件，互不冲突；`--stats` 靠文件名里的 hash 反查该组包含哪些源文件。

`--markdown` 是一个**附加输出**：在 Dolma JSONL 之外，把 `doc["text"]`（注意：这是 front matter 已剥离的纯正文，剥离发生在 [olmocr/pipeline.py:210-212](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L210-L212) 的解析步骤）按 `get_markdown_path` 的路径逐篇写出。

#### 4.3.2 核心流程

```text
worker（每个 work item 循环一次）
  ├── TaskGroup 并发处理组内所有路径（process_pdf / process_tarball）
  ├── 收集 dolma_docs（tarball 贡献 list，单 PDF 贡献单个 doc；None 剔除）
  ├── 全部 docs 逐行 json.dumps 写入本地临时文件
  ├── 目标路径 = workspace/results/output_{work_item.hash}.jsonl
  │       ├── s3:// 前缀 → parse_s3_path + upload_file
  │       └── 否则 → makedirs + shutil.copyfile（整文件覆盖）
  ├── 若 --markdown：
  │       └── 对每个 doc：get_markdown_path(workspace, doc["metadata"]["Source-File"])
  │               ├── s3:// 路径 → 临时文件 + upload_file
  │               └── 否则 → makedirs + open(..., "w") 写 doc["text"]
  ├── metrics.add_metrics(finished_input_tokens / finished_output_tokens)
  └── work_queue.mark_done(work_item)   # 最后一步！
```

#### 4.3.3 源码精读

- [olmocr/pipeline.py:723-739](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L723-L739) 收集阶段：逐个取 task 结果，异常或 `None` 的文档直接跳过（某个 PDF 失败不影响同组其他 PDF 落盘）；`process_tarball` 返回 list、`process_pdf` 返回单个 doc，这里用 `isinstance(result, list)` 归并。
- [olmocr/pipeline.py:741-763](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L741-L763) **JSONL 落盘**：先写到 `tempfile.NamedTemporaryFile`，再按目标路径分叉——S3 workspace 用 `upload_file` 上传，本地 workspace 用 `shutil.copyfile` 覆盖拷贝（先建 `results/` 目录）。「先临时文件再整体搬运」意味着无论重跑多少次，`output_{hash}.jsonl` 要么是旧的完整版本、要么是新的完整版本，**不会出现半截文件**——这就是重跑覆盖的最终一致性。文件名里的 hash 正是 work group 的 sha1（u1-l3/u3-l1 讲过其跨机确定性）。
- [olmocr/pipeline.py:765-794](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L765-L794) **markdown 落盘**：从每篇 doc 取 `metadata["Source-File"]` 与 `text`；路径经 `get_markdown_path` 计算；S3 与本地再次分叉（S3 走临时文件 + 上传，本地 `os.makedirs(..., exist_ok=True)` 后直写）。
- [olmocr/pipeline.py:796-802](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L796-L802) 收尾：把成功文档的 token 数累加进 metrics（u4-l4 会细讲），最后才 `mark_done(work_item)`。顺序保证了「done flag 出现 ⇒ 该组的 results 与 markdown 都已写完」。
- [olmocr/pipeline.py:803-804](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L803-L804) 整个 work item 的处理包在 try/except 里：任何异常只记日志，worker **不会退出**，继续取下一个 work item——与 u2-l1 的「worker 异常不退出」结论呼应。
- 输出文件名还是 `--stats` 的统计契约：[olmocr/pipeline.py:1162](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1162) 用正则 `output_(\w+).jsonl` 从文件名提取 hash，反查 work_queue 得到该组的原始路径，从而统计「Total documents skipped」等指标。

#### 4.3.4 代码实践

本实践是**源码阅读型 + 本地模拟**（无需 GPU、无需模型）。

1. **实践目标**：在不跑流水线的情况下，亲手复现 worker 对一个 work item 的落盘动作，并确认「done 之前先落盘」的顺序。
2. **操作步骤**（**示例代码**）：

   ```python
   import json, os, re, shutil, tempfile
   from olmocr.pipeline import PageResult, build_dolma_document, get_markdown_path
   from olmocr.prompts import PageResponse

   # 1) 手工造一篇两页文档（复用 4.1.4 的 fake_page）
   def fake_page(page_num, text):
       return PageResult(
           s3_path="s3://bucket/a/b.pdf", page_num=page_num,
           response=PageResponse(natural_text=text, primary_language="en",
                                 is_rotation_valid=True, rotation_correction=0,
                                 is_table=False, is_diagram=False),
           input_tokens=100, output_tokens=50, is_fallback=False, is_valid=True)

   docs = [build_dolma_document("s3://bucket/a/b.pdf",
           [fake_page(1, "page one text"), fake_page(2, "page two text")])]

   # 2) 模拟 worker 的 JSONL 落盘（pipeline.py:741-759 的本地分支）
   work_hash = "abc123"
   ws = "/tmp/fake_ws"
   with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tf:
       for doc in docs:
           tf.write(json.dumps(doc)); tf.write("\n")
       temp_path = tf.name
   out_path = os.path.join(ws, "results", f"output_{work_hash}.jsonl")
   os.makedirs(os.path.dirname(out_path), exist_ok=True)
   shutil.copyfile(temp_path, out_path); os.unlink(temp_path)

   # 3) 模拟 --markdown 分支（pipeline.py:765-794）
   for doc in docs:
       md_path = get_markdown_path(ws, doc["metadata"]["Source-File"])
       os.makedirs(os.path.dirname(md_path), exist_ok=True)
       with open(md_path, "w") as f:
           f.write(doc["text"])

   # 4) 用 print_stats 同款正则回读（pipeline.py:1162）
   match = re.search(r"output_(\w+).jsonl", out_path)
   print("hash from filename:", match.group(1))
   with open(out_path) as f:
       for line in f:
           d = json.loads(line)
           print("id:", d["id"][:12], "| pages:", d["attributes"]["pdf_page_numbers"])
   print("markdown 文件内容:", open(f"{ws}/markdown/a/b.md").read())
   ```

3. **需要观察的现象**：`/tmp/fake_ws/results/output_abc123.jsonl` 只有一行；正则提取出 `abc123`；`/tmp/fake_ws/markdown/a/b.md` 内容是 `page one text\npage two text`（中间恰好一个换行、末尾无换行）。
4. **预期结果**：与上述一致；若把第 3 步挪到第 2 步之前，体会「mark_done 之前必须完成全部落盘」的顺序约束为什么重要（颠倒后 done flag 出现时 markdown 可能还没写）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一个 work item 含 3 个 PDF，其中 1 个被 `max_page_error_rate` 淘汰、1 个是 `NoSuchKey` 的 S3 路径（u2-l2 讲过完整性校验推迟到 worker），`results/output_{hash}.jsonl` 里会有几行？done flag 会写吗？

**答案**：1 行——淘汰与丢失的 PDF 都返回 `None`，收集时被剔除；剩下 1 篇正常落盘。done flag 照常写：组内个别 PDF 失败不算组失败，这正是「整组完成」的定义（u1-l3）。

**练习 2**：为什么 markdown 写文件用 `open(path, "w")` 直写，而 JSONL 要先写临时文件再 `copyfile`？

**答案**：JSONL 是断点续跑的**权威产物**——重跑覆盖必须做到原子性（旧全量或新全量，无半截），所以经临时文件整体搬运；markdown 是附加产物，单篇重写本身粒度小、损坏代价低，直写即可。另外 S3 分支两者其实都走「临时文件 + upload_file」，因为 S3 的 put 本身就是整对象覆盖。

**练习 3**：`--stats` 是怎么知道「Total documents skipped」的？

**答案**：它扫描 done_work_items 的文件名，用 `output_(\w+).jsonl` 提取 hash，反查 work_queue 中该 hash 对应的原始路径集合 `original_paths`；再统计实际出现在输出里的 `Source-File` 集合 `all_processed`，两者相减（`original_paths - all_processed`，L1163 与 L1180）即被过滤/丢弃/丢失的文档数。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**手工扮演一次「微型 worker」**。

1. 准备：轻量安装环境（`pip install olmocr`，无需 GPU）。
2. 用 4.1.4 的 `fake_page` 构造**三个** `PageResult`，要求：第 1 页正常且 `natural_text` 长度记为 L1；第 2 页 `natural_text=None`（模拟空白页）；第 3 页 `is_fallback=True`（模拟兜底页，参考 `make_fallback_result` 的字段取值）。
3. 调用 `build_dolma_document`，完成以下检查并记录：
   - `doc["id"] == hashlib.sha1(doc["text"].encode()).hexdigest()`；
   - 三个 span 首尾相接且并集长度等于 `len(doc["text"])`；第 2 页 span 长度为 1（空正文 + 分隔换行符）；
   - `attributes` 里 5 个逐页列表长度都是 3，且 `primary_language[1]` 等于第 2 页传入的语言值（空白页传 `None`）；
   - `metadata["total-fallback-pages"] == 1`。
4. 用 `get_markdown_path` 分别处理 `s3://bucket/a/b.pdf` 与 `tarballs/pack.tar.gz::inner.pdf`，核对结果为 `<ws>/markdown/a/b.md` 与 `<ws>/markdown/pack/inner.md`（tarball 双层剥后缀 `pack.tar.gz → pack`，内部成员再换 `.md` 后缀，见 4.2 练习 2 的辨析）。
5. 按 4.3.4 的方式把文档写进 `<ws>/results/output_handmade.jsonl`，再写 markdown 文件；最后用 `python -m olmocr.viewer.dolmaviewer --help` 看一眼（u7-l3 的 Dolma 查看器正是消费这份 JSONL 的工具，本讲先混个脸熟）。
6. 思考题：如果你把 `--max_page_error_rate` 调成 `1.0`，第 3 步的文档还会被淘汰吗？（答案：永远不会——fallback 占比不可能超过 1.0；等价于关闭整篇淘汰，只留下「空文本返回 None」这一条出路。）

## 6. 本讲小结

- `build_dolma_document` 把 N 个 `PageResult` 聚合成一份 Dolma JSON：`text` 为各页 `natural_text` 顺序拼接（非末页补 `\n`），`id` 是全文 sha1（内容寻址去重键），页级标注进 `attributes`。
- `pdf_page_spans` 用 `[start, end, page_num]` 三元组记录每页字符区间，span 首尾相接无缝覆盖全文，分隔换行符归属前一页；空白页也占一个（可能长度为 0 的）span。
- `--max_page_error_rate`（默认 0.004 = 1/250）在整篇粒度裁决质量：fallback 页占比超标则整篇丢弃，返回 `None`，不出现在输出中。
- worker 以 work item 为单位落盘：`results/output_{hash}.jsonl` 每行一篇文档，经临时文件整体搬运保证覆盖的原子性；`mark_done` 在全部落盘之后才写。
- `--markdown` 附加输出 `doc["text"]`（front matter 已在上游剥离），路径由 `get_markdown_path` 清洗：三分支归一 + 过滤 `..` 与空段 + 换 `.md` 后缀，确保永远落在 `workspace/markdown/` 之内。
- 输出文件名 `output_{hash}.jsonl` 同时是 `--stats` 反查源路径的契约， Dolma 查看器与下游语料工具都消费这份格式。

## 7. 下一步学习建议

- **单元 3（u3-l1、u3-l2）**：深入 `WorkQueue` 与两种 Backend，弄清 `work_item.hash` 如何生成、done flag 与 `output_{hash}.jsonl` 的对应关系如何在多机场景下协同。
- **u4-l4（metrics 与监控）**：本讲结尾的 `finished_input_tokens` / `finished_output_tokens` 只是冰山一角，下一讲系统讲解 `MetricsKeeper` 与 `WorkerTracker`。
- **u7-l3（Dolma 查看器）**：学完单元 1~4 后回头读 `olmocr/viewer/dolmaviewer.py`，看它如何用 `pdf_page_spans` 把 JSONL 渲染成可翻页比对的 HTML 报告，检验你对本讲 span 语义的理解。
- 顺手阅读：[olmocr/datatypes.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/datatypes.py#L7-L33) 的旧版 `mk_dolma_doc`，对比新旧两代 Dolma 产物的字段差异，体会输出契约的演化。
