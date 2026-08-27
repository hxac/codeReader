# 测试体系：单元测试与冒烟矩阵

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 pdf-craft 的三层测试体系是如何分工的：单元测试（毫秒级、零网络）→ 边界测试（架构契约守卫）→ 冒烟测试（真实 OCR/LLM 后端、分钟级）。
2. 读懂并扩展 `tests/smoke/*.json` 冒烟矩阵：理解「资产 × 路由 × 后端 × 翻译配置」如何用一个 JSON 文件描述一批真实转换，并会用 `pdf_craft_tool smoke matrix` 跑通它。
3. 理解 `tests/test_module_boundaries.py` 与 `tests/test_composable_boundaries.py` 这两个「架构守卫」测试约束了什么：谁拥有什么职责、模块拼起来时哪些契约不许破。
4. 能为新功能挑选正确层级的测试：纯函数用断言直测、跨层流程用伪协作对象、外部服务用 mock 或冒烟矩阵。

## 2. 前置知识

### 2.1 unittest 基础

pdf-craft 的测试全部基于 Python 标准库 `unittest`（不用 pytest 跑，虽然开发依赖里有 pytest）。你需要熟悉这几个常用手段：

- `self.assertEqual(a, b)` / `self.assertTrue(x)`：普通断言。
- `self.assertRaisesRegex(Error, "片段")`：断言抛出的异常类型与消息片段，见 [tests/test_composable_boundaries.py:118-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L118-L120)。
- `self.subTest(name=name)`：在同一测试方法里循环多个用例且失败互不掩盖，见 [tests/test_smoke.py:225-231](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_smoke.py#L225-L231)。
- `unittest.mock.patch("模块.属性")`：临时替换某个对象为 mock，测试结束后自动还原。

### 2.2 测试替身的三种武器

pdf-craft 的测试里反复出现三种「替身」，初学者容易混淆，先建立直觉：

| 武器 | 形态 | 例子 | 适用场景 |
| --- | --- | --- | --- |
| 伪协作对象（fake） | 手写一个最小类，实现协议要求的方法 | `_FakeTransform`、`_FakeHandler` | 被测代码依赖的是 Protocol，只关心接口不关心实现 |
| 捕获器（capture） | 伪对象把收到的参数存起来，测试事后检查 | `_CapturePatcher`、`_CaptureTransform` | 想知道「被测代码到底传了什么给协作者」 |
| mock/patch | 用 `unittest.mock.patch` 替换真实类或函数 | `patch("pdf_craft_tool.smoke.runner.PDFCraft")` | 想隔离一个具体的外部依赖点 |

### 2.3 为什么这个项目特别需要分层测试

回顾前面单元的认知：pdf-craft 的两条外部依赖都「慢且花钱」——OCR 后端要下载模型或调用远程服务（u3-l4），LLM 翻译按 token 计费（u8-l1）。如果每个测试都真调 OCR，测试套件就无法在 CI 里快速跑。所以这个仓库的取舍是：

- **绝大多数测试离线跑**：用伪协作对象和 mock 把 OCR、LLM、PDF 渲染全部挡在门外。
- **少数「冒烟」用例真调后端**：由人手动触发、参数化成 JSON 矩阵，验证「这套真实配置今天还能不能工作」。
- **两层架构守卫**：用极便宜的断言防止模块边界随着提交逐渐腐烂。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `test.py` | 仓库级测试入口：用 `unittest` 发现并运行 `tests/test*.py`，CI 调用的就是它 |
| `tests/test_toc_text.py`、`tests/test_jointer.py` | 纯函数单元测试的代表：目录文本归一化、章节块合并 |
| `tests/test_smoke.py` | 冒烟体系自己的单元测试——「测试的测试」，用 mock 离线验证矩阵展开与运行报告 |
| `tests/smoke/minimal.json` | 最小冒烟矩阵：1 个资产、1 条路由、本地 DeepSeek OCR |
| `tests/smoke/all_ocr_backends.json`、`tests/smoke/vendor_real.json`、`tests/smoke/table_formula_real.json` | 其他三份矩阵：六后端矩阵、真实 vendor + LLM 翻译、表格公式资产 |
| `tests/assets/` | 冒烟资产池：`double_column.pdf`、`citation.pdf`、`epub/Cambridge.epub` 等 PDF/EPUB 文件 |
| `tests/test_module_boundaries.py` | 模块边界守卫：EPUB 编排归 pipeline、XMLTranslator 格式无关 |
| `tests/test_composable_boundaries.py` | 组合边界守卫：提取→包→渲染/回写全链路的契约测试 |
| `pdf_craft_tool/smoke/assets.py` | 扫描资产目录，发现全部 PDF/EPUB 资产 |
| `pdf_craft_tool/smoke/ocr.py` | 冒烟专用的 OCR 配置工厂：后端字符串 → 六种配置类之一 |
| `pdf_craft_tool/smoke/runner.py` | 冒烟核心：`SmokeRun` 数据类、`expand_matrix` 矩阵展开、`run_smoke` 执行与报告落盘 |
| `pdf_craft_tool/smoke/checks.py` | 产物校验器：`check_package`、`check_markdown`、`check_epub`、`check_pdf` |
| `pdf_craft_tool/cli.py`（smoke 子命令部分） | `smoke assets` / `smoke run` / `smoke matrix` 三个子命令 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**冒烟矩阵**、**单元测试**、**边界测试**。

### 4.1 冒烟矩阵

#### 4.1.1 概念说明

「冒烟测试」（smoke test）指最短路径的真实端到端验证——名字来自硬件工程师第一次通电时看有没有冒烟。在 pdf-craft 里，它回答的问题是：**某个真实 OCR 后端 + 某个真实资产 + 某条转换路由，现在还能不能完整跑通并产出合法产物？**

这个问题的答案会随外部世界变化：vendor 服务改接口、本地 CUDA 环境变动、上游 `doc-page-extractor` 升级。所以冒烟不能写死在代码里，而被参数化成 **JSON 矩阵**：一个文件描述「资产 × 路由 × 后端 × 翻译配置」的一批组合，CLI 逐条执行并给每条生成可追溯的运行报告。

它与 u11-l1 讲过的运行目录双轨制衔接：每条冒烟 run 都在自己的 `run` 目录里留下 `manifest.json`（运行参数与阶段时间线）、`checks.json`（校验结论）和 `logs/`（含异常 traceback），失败后可以只看目录就知道死在哪一步。

#### 4.1.2 核心流程

```text
tests/smoke/minimal.json（JSON 矩阵）
        │  python -m pdf_craft_tool smoke matrix --config ...
        ▼
expand_matrix(config, assets_root)
        │  defaults 合并进每个 run；校验 资产格式 × 路由 合法性
        ▼
[SmokeRun, SmokeRun, ...]          ← 冻结 dataclass，一行矩阵 = 一个 run
        │  逐条 run_smoke(...)
        ▼
create_run_directory(output_root, "资产名-路由名")   ← u11-l1 的「标签-日期-序号」目录
        ▼
五阶段执行：configure → extract → render → check → finish
        │  （EPUB 路由跳过 configure/extract；package 路由跳过 render）
        ▼
落盘 manifest.json / checks.json / logs/traceback.txt
        │
        ▼
_smoke_exit_code：passed/planned → 0；skipped/failed → 1
```

五种终态要分清：

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `passed` | 全部检查通过 | 0 |
| `planned` | `--dry-run`，只写计划不执行 | 0 |
| `skipped` | 前置条件不满足（缺 OCR 配置、无 CUDA、环境变量缺失） | 1 |
| `failed` | 执行或检查失败 | 1 |

#### 4.1.3 源码精读

**（1）路由体系：八个 SmokeRoute，按资产格式分组。**

[runner.py:35-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L35-L40) 定义了 `SmokeRoute` 字面量类型与两个集合：

```python
SmokeRoute = Literal[
    "package", "package-markdown", "package-epub", "markdown", "epub",
    "pdf-patch", "epub-check", "epub-translate",
]
PDF_ROUTES = {"package", "package-markdown", "package-epub", "markdown", "epub", "pdf-patch"}
EPUB_ROUTES = {"epub-check", "epub-translate"}
```

这八条路由覆盖了 u1-l1 讲过的五大工作流：`markdown`/`epub` 是一步式转换，`package*` 三条停在中间产物（正好复习 u6-l1 的 DocumentPackage 契约），`pdf-patch` 对应译文回写，`epub-check`/`epub-translate` 处理现成 EPUB。

**（2）矩阵展开：defaults 合并与格式校验。**

[runner.py:112-128](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L112-L128) 的 `expand_matrix` 是矩阵的语义核心：

```python
defaults = dict(config.get("defaults", {}))
for item in config.get("runs", []):
    item = defaults | item          # 字典合并：run 字段覆盖 defaults
    ...
    if known[asset].format == "pdf" and route not in PDF_ROUTES:
        raise ValueError(f"PDF asset {asset} cannot use route {route}")
    pages = item.get("page_indexes")
    runs.append(SmokeRun(**(item | {"page_indexes": tuple(pages) if pages else None})))
```

三个要点：`defaults | item` 是 Python 3.9+ 的字典合并运算，run 级字段总是胜出；资产格式与路由的合法性在这里硬校验（PDF 资产不能走 `epub-check`）；`page_indexes` 从 JSON 列表转成元组以适配冻结 dataclass。

**（3）minimal.json：最小矩阵长什么样。**

[tests/smoke/minimal.json:1-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/tests/smoke/minimal.json#L1-L18) 全文只有一条 run：

```json
{
  "defaults": {
    "page_indexes": [1],
    "ocr_size": "tiny",
    "translation": {
      "package_marker": "[smoke-translated]",
      "package_submit": "REPLACE"
    }
  },
  "runs": [
    {
      "asset": "double_column.pdf",
      "route": "markdown",
      "backend": "deepseek-ocr-local",
      "ocr": {}
    }
  ]
}
```

逐字段解读：`page_indexes: [1]` 只转第 1 页（省时省钱）；`ocr_size: "tiny"` 用最小识别尺寸；`backend: "deepseek-ocr-local"` 选本地后端，配合 `"ocr": {}`（空字典直接作为构造参数）意味着 `DeepSeekOCRLocalConfig()` 全默认值——**不需要任何环境变量与凭据**，这也是它被命名为 minimal 的原因；`translation.package_marker` 让渲染前跑一个确定性包转换（给每段文字追加 `[smoke-translated]` 标记），随后检查器验证标记确实出现在产物里——这样不用花一分钱 LLM 费用就测通了「提取 → 转换 → 渲染」三段全链路。

**（4）run_smoke：五阶段与失败报告。**

[runner.py:131-177](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft_tool/smoke/runner.py#L131-L177) 的 `run_smoke` 是执行骨架。三个值得注意的设计：

- `dry_run=True` 时写完 manifest 直接以 `planned` 收尾（[runner.py:155-157](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L155-L157)），完全不碰 OCR——这是零成本验证矩阵写法是否正确的手段。
- 任何异常都不会让进程当场崩溃，而是把 traceback 擦除凭据后写进 `logs/traceback.txt`，并在 manifest 里记录失败阶段与异常类型（[runner.py:162-169](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft_tool/smoke/runner.py#L162-L169)）——「保留完整失败报告供人工检查」。
- `_ExecutionReport.stage` 是上下文管理器（[runner.py:70-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L70-L88)）：进入时登记阶段为 running，正常退出改 passed、异常改 failed，并记录起止时间与耗时。时间线 `configure → extract → render → check → finish` 的五个条目就是这么来的。

**（5）「不可用」与「失败」的区别。**

[runner.py:316-323](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft_tool/smoke/runner.py#L316-L323) 的 `_unavailable_ocr_reason` 沿异常链（`__cause__`/`__context__`）向下找 "No CUDA devices available" 字样：

```python
def _unavailable_ocr_reason(error: Exception) -> str | None:
    """Recognise infrastructure gaps without hiding extraction failures."""
    current: BaseException | None = error
    while current is not None:
        if "No CUDA devices available" in str(current):
            return "OCR backend unavailable: local OCR requires CUDA, but no CUDA device is available"
        current = current.__cause__ or current.__context__
    return None
```

命中则整条 run 记 `skipped` 而非 `failed`：机器没有 GPU 是环境事实，不是代码缺陷，不该污染「今天代码是否健康」的信号。注意 docstring 强调「不掩盖提取失败」——只有这条特定根因才豁免。

**（6）凭据永不落盘。**

[runner.py:468-492](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L468-L492) 有两道防线：`_redact` 在写 manifest 前递归地把命中 `_is_secret_key`（`api_key`、`ak`、`sk`、`access_key`、后缀 `_token`/`_key` 等）的键值替换为 `[redacted]`；`_secret_values`（[runner.py:495-511](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L495-L511)）收集真实密钥字符串，供 `redact_text` 擦除异常消息与 traceback 里意外出现的密钥。所以矩阵里可以放心地内联真实凭据——报告里永远是 `[redacted]`。

**（7）产物校验器。**

[checks.py:16-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/checks.py#L16-L34) 的 `check_package` 验证 DocumentPackage 合法性（有章节 XML、XML 可解析、可选 toc.xml 可解析、按需要求页几何）；[checks.py:61-80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/checks.py#L61-L80) 的 `check_markdown` 用正则抽出全部本地图片链接并逐一验证文件存在（URL、锚点跳过）；[checks.py:83-106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/checks.py#L83-L106) 的 `check_epub` 手工验证 EPUB 容器契约：mimetype、container.xml、恰好一个 OPF、manifest 资源齐全、spine 引用有效、EPUB 2 的 NCX / EPUB 3 的 nav 目录链接可达。这些正是 u6 单元讲过的渲染契约的「机器可读版」。

**（8）CLI 入口。**

[cli.py:116-144](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L116-L144) 注册了三个子命令：`smoke assets`（列出资产池）、`smoke run`（命令行参数描述单条 run）、`smoke matrix`（读 JSON 矩阵）。[cli.py:377-383](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L377-L383) 的 `_smoke_exit_code` 定义退出码约定：只有 `passed` 与 `planned` 算成功，`skipped` 也算失败——因为矩阵作者既然写了这条 run，就默认它应当在本机可跑。

**（9）测试的测试。**

`tests/test_smoke.py` 本身是冒烟体系的单元测试：用 `patch("pdf_craft_tool.smoke.runner.PDFCraft")` 把门面换成 mock（[test_smoke.py:151-160](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_smoke.py#L151-L160)），零 OCR 成本验证事件记录与时间线；用伪造的 `UnavailableCraft` 验证 CUDA 豁免逻辑（[test_smoke.py:281-303](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_smoke.py#L281-L303)）。这保证了「冒烟框架自身的逻辑」也在 CI 保护之下。

#### 4.1.4 代码实践

**实践目标**：亲手复制并改造一份冒烟矩阵，先用 `--dry-run` 零成本验证写法，再（在有凭据的机器上）真实跑通。

**操作步骤**：

1. 复制最小矩阵：`cp tests/smoke/minimal.json tests/smoke/my-matrix.json`。
2. 编辑 `my-matrix.json`，改成 vendor 后端 + EPUB 路由 + 显式页码（示例代码，凭据换成你自己的）：

```json
{
  "defaults": {
    "page_indexes": [1],
    "ocr_size": "tiny",
    "translation": {
      "package_marker": "[my-smoke]",
      "package_submit": "APPEND_TEXT"
    }
  },
  "runs": [
    {
      "asset": "citation.pdf",
      "route": "package-epub",
      "backend": "deepseek-ocr-vendor",
      "ocr": {
        "base_url": "https://your-vendor.example/v1",
        "api_key": "sk-xxx",
        "model": "your-model-name"
      }
    },
    {
      "asset": "epub/Cambridge.epub",
      "route": "epub-check"
    }
  ]
}
```

   注意第二条 run 故意不带 `backend`/`ocr`——`epub-check` 是 EPUB 路由，不需要 OCR 配置。
3. 先做干跑：`python -m pdf_craft_tool smoke matrix --config tests/smoke/my-matrix.json --dry-run`。
4. 打开干跑产出的两个 run 目录（命令会打印路径），阅读 `manifest.json` 与 `checks.json`。
5. （有凭据的机器）去掉 `--dry-run` 真跑一次，再检查产物 `output/book.epub`。

**需要观察的现象**：

- 干跑时两条 run 的 `checks.json` 里 `status` 均为 `planned`，退出码为 0。
- `manifest.json` 的 `run.ocr.api_key` 值是 `[redacted]` 而不是真实密钥。
- run 目录名形如 `citation-package-epub-20260826-001`（资产名-路由名-日期-序号），与 u11-l1 的目录双轨制一致。
- 真跑时第一条 run 的 manifest 里 `timeline` 包含五个阶段，`ocr_events` 记录第 1 页的 COMPLETE 事件与 token 数；EPUB 产物通过 `check_epub` 全部检查；产物 EPUB 内文包含 `[my-smoke]` 标记（APPEND_TEXT 模式追加在原文之后）。

**预期结果**：两条 run 均 `passed`。若把 `backend` 改成本地后端而机器无 CUDA，会得到 `skipped` 与「OCR backend unavailable」提示而非崩溃。真跑行为待本地验证（本讲义撰写环境未执行真实转换）。

#### 4.1.5 小练习与答案

**练习 1**：`all_ocr_backends.json` 里六个 run 都没写 `asset` 和 `route`，为什么不会报错？

**答案**：`expand_matrix` 用 `defaults | item` 合并配置（[runner.py:117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L117)），该矩阵的 `defaults` 里已写明 `"asset": "citation.pdf"` 与 `"route": "package"`，run 级只覆盖 `backend` 等差异字段。这正是矩阵设计的意义：公共参数上提、每行只写增量。

**练习 2**：如果把 minimal.json 的 `route` 改成 `epub-check` 会发生什么？

**答案**：`expand_matrix` 抛 `ValueError: PDF asset double_column.pdf cannot use route epub-check`（[runner.py:122-123](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L122-L123)）。资产格式（由 `discover_assets` 按扩展名判定，[assets.py:18-21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/assets.py#L18-L21)）与路由分组必须在矩阵展开时就匹配，错误早于任何文件操作暴露。

**练习 3**：为什么 `package_marker` 检查能替代 LLM 翻译来验证「转换步骤真的执行了」？

**答案**：`_package_steps` 在没有 `translation.llm` 时用 `_DeterministicChapterTransformer`（给每个文本成员追加固定标记字符串，[runner.py:338-372](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L338-L372)）构造确定性转换步骤，渲染后检查器在产物里找标记（[runner.py:269-271](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/smoke/runner.py#L269-L271)）。它走的是与真翻译完全相同的公开路径（`craft.translate_package` → `TranslationStep`），但不依赖 LLM 的不确定输出——用确定性替身验证管线连通性，这正是「冒烟」与「单元测试替身」思想的合流。

### 4.2 单元测试

#### 4.2.1 概念说明

单元测试是 CI 里每次提交都跑的最内层。这个仓库的入口不是 pytest，而是自带的 [test.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/test.py#L15-L23)：用 `unittest.TestLoader.discover` 在 `tests/` 下发现所有 `test*.py`，也支持传一个文件名单跑一个模块。CI 工作流 `pr-check.yml` 的「Run tests」步骤调用的就是 `poetry run python test.py`（[.github/workflows/pr-check.yml:46-48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.github/workflows/pr-check.yml#L46-L48)），同一步流水线里还有 pyright 类型检查与 pylint。

`tests/` 下的 23 个测试文件大致按被测模块命名：`test_jointer.py`（块合并）、`test_punctuation.py`（标点归一化）、`test_toc_text.py`（目录文本归一化）、`test_llm_loop.py`（修复循环）、`test_pdf_patcher.py`（PDF 回写）、`test_parser.py`（HTML 安全过滤）……对应关系一目了然，找测试就是按模块名对号入座。

#### 4.2.2 核心流程

写一个 pdf-craft 风格的单元测试，决策树只有三问：

```text
被测对象是纯函数？
  是 → 直接导入函数，断言输入输出（test_toc_text.py 风格）
  否 ↓
被测对象的依赖是 Protocol（结构化类型）？
  是 → 手写伪协作对象/捕获器，构造被测对象注入（test_composable_boundaries.py 风格）
  否 ↓
只想隔离某个具体依赖点？
    → unittest.mock.patch 替换它（test_smoke.py 风格）
```

三种风格对应 u1-l3 讲过的架构事实：核心模块之间靠 Protocol 与 DocumentPackage 磁盘契约解耦，所以大多数测试不需要 mock 框架——直接喂一个假协作者即可。

#### 4.2.3 源码精读

**（1）纯函数测试：test_toc_text.py。**

[test_toc_text.py:6-19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_text.py#L6-L19) 测的是目录文本归一化 `normalize_text`（u4-l1 的多模式匹配前置步骤）：

```python
class TestNormalizeText(unittest.TestCase):
    """测试 normalize_text 函数"""

    def test_basic_whitespace_normalization(self):
        """测试基本的空白符规范化"""
        text = "hello    world"
        result = normalize_text(text)
        self.assertEqual(result, "hello world")
```

值得学习的风格：每个方法一个明确命名的行为；docstring 用中文写「测什么」；一个方法内可以放多个相关的断言（如连字符断词测试同时覆盖 `-` 与 `—` 两种连字符，[test_toc_text.py:21-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_text.py#L21-L31)）。这种测试没有任何 I/O，是回归保护的基石。

**（2）伪协作对象：test_composable_boundaries.py 的备件库。**

[test_composable_boundaries.py:25-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L25-L33) 的 `_FakeTransform` 是提取引擎的假后端：

```python
class _FakeTransform:
    def extract_package(self, *, analysing_path, **_kwargs):
        (analysing_path / "chapters").mkdir(parents=True)
        (analysing_path / "assets").mkdir()
        (analysing_path / "toc.xml").write_text("<toc/>")
        DocumentPackage.from_path(analysing_path).write_metadata(
            dpi=300, page_pixel_sizes={1: (100, 100)}
        )
        return None, None, None, None, "metering"
```

它按磁盘契约伪造一个最小合法包（这正是 u6-l1 讲过的「目录即数据格式」的红利——伪造产物只需 `mkdir` 加几个文件）。同理 `_AllPagesFailOCR`（[test_composable_boundaries.py:53-63](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L53-L63)）是一个只会吐 FAILED 事件的假 OCR 生成器，`_FakeDocument`/`_FakeHandler`（[test_composable_boundaries.py:66-94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L66-L94)）是假 PDF 处理器——`render_page` 返回一张 100×100 的纯色 PIL 图像并自己数渲染次数。

**（3）mock 隔离：test_smoke.py。**

当只想隔掉一个点时用 patch。[test_smoke.py:151-156](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_smoke.py#L151-L156) 把 `PDFCraft` 类整个换成 mock，再用 `side_effect` 让 `extract_pdf_with_metering` 回调 `on_ocr_event` 吐一个合成 COMPLETE 事件：

```python
with patch("pdf_craft_tool.smoke.runner.PDFCraft") as craft_class:
    craft = craft_class.return_value
    def extract(_source, _path, options):
        options.on_ocr_event(OCREvent(OCREventKind.COMPLETE, 1, 1, 12, 5, 7))
        return package, metering
    craft.extract_pdf_with_metering.side_effect = extract
```

于是「冒烟报告正确记录 OCR 事件与阶段时间线」这个断言（[test_smoke.py:161-166](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_smoke.py#L161-L166)）可以在没有任何 OCR 后端的机器上跑通。

**（4）私有函数也直测：test_jointer.py。**

[test_jointer.py:7-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L7-L13) 直接导入 `Jointer` 模块的下划线私有成员（`_normalize_equation`、`_AssetHolder` 等）做纯函数测试：

```python
from pdf_craft.extractor.chapter.jointer import (
    Jointer, _AssetHolder, _normalize_equation, _normalize_table, _parse_block_content,
)
```

这在很多工程规范里会被禁止，但在这个仓库是被接受的做法：把复杂逻辑拆成模块级小函数，测试就能绕过类实例化直接打到目标分支。`_AssetHolder`（[jointer.py:43-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L43-L51)）因此天然是个测试数据构造器。

#### 4.2.4 代码实践

**实践目标**：模仿 `test_toc_text.py` 的纯函数测试风格，为 `jointer.py` 中未被现有测试覆盖的**中文正则分支**补写测试。

现有测试的空白点：`test_jointer.py:414-483` 的表格相邻段测试全部用英文样本（`"Table 1:"`、`"Note:"`、`"1. "`），而 [jointer.py:19-30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L19-L30) 的三个正则都包含中文分支——`表|表格`（表格标题）、`资料来源|来源|注|备注`（表格注）、`[①-⑳]`（带圈数字脚注）——这些分支目前没有直接测试。而中文书恰恰是 pdf-craft 的主要输入。

**操作步骤**：

1. 阅读 [jointer.py:390-409](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L390-L409) 的两个判定函数 `_is_table_title_text` 与 `_is_table_caption_text`，确认它们是可独立调用的纯函数（输入字符串，输出布尔）。
2. 新建 `tests/test_jointer_cn_regex.py`（示例代码，不是仓库原有文件）：

```python
# 示例代码：为 jointer.py 中文正则分支补写的测试
import unittest

from pdf_craft.extractor.chapter.jointer import (
    _is_table_caption_text,
    _is_table_title_text,
)


class TestJointerChineseRegex(unittest.TestCase):
    """测试表格标题/表格注判定的中文分支"""

    def test_chinese_table_title_is_recognized(self):
        """「表3.2 数据汇总」应命中 _TABLE_TITLE_PATTERN 的中文分支"""
        self.assertTrue(_is_table_title_text("表3.2 各省降水量汇总"))

    def test_chinese_table_number_is_recognized(self):
        """中文数字编号「表三、」也应命中（[一二三四五六七八九十] 分支）"""
        self.assertTrue(_is_table_title_text("表三、主要试验结果"))

    def test_chinese_source_note_is_caption(self):
        """「资料来源：」应命中 _TABLE_CAPTION_PATTERN"""
        self.assertTrue(_is_table_caption_text("资料来源：国家统计局年鉴"))

    def test_circled_number_is_caption(self):
        """带圈数字「① 」应命中 _FOOTNOTE_PATTERN"""
        self.assertTrue(_is_table_caption_text("① 数据为四舍五入后的近似值。"))

    def test_plain_chinese_paragraph_is_neither(self):
        """普通中文段落既不是表格标题也不是表格注"""
        text = "如上一节所述，我们对全部样本重新估计了模型参数。"
        self.assertFalse(_is_table_title_text(text))
        self.assertFalse(_is_table_caption_text(text))


if __name__ == "__main__":
    unittest.main()
```

3. 运行：`python test.py test_jointer_cn_regex`（利用 test.py 的单文件参数，[test.py:8-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/test.py#L8-L11)）。

**需要观察的现象**：五个测试全部通过；可以再故意把 `表3.2` 改成 `表 3.2`（编号前多个空格）观察结果是否仍为 True（提示：正则里 `表` 后面是 `\s*`，应当仍命中）。

**预期结果**：全部通过。注意 `①` 分支要求编号后跟空白（`\s+`），「①数据为…」（无空格）预期不命中——可以自己加一条反向断言验证边界。具体断言结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_FakeDocument.metadata()` 的实现是 `raise AssertionError(...)` 而不是返回假数据（[test_composable_boundaries.py:68-70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L68-L70)）？

**答案**：这是一个「防误用哨兵」。该假文档只服务 OCR 几何缓存测试，断言注释明说 metadata 不应被这条路径调用。返回假数据会让意外的调用静默通过，而抛 AssertionError 能让「被测代码意外依赖了 metadata」立刻以测试失败形式暴露——替身不仅提供假数据，还固化了「谁不该被调用」的契约。

**练习 2**：`test.py` 的单文件参数有什么用？CI 里为什么不需要它？

**答案**：`python test.py test_jointer` 只跑一个测试文件（[test.py:8-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/test.py#L8-L13)），本地开发改一个模块时秒级反馈。CI 要验证整个提交，必须全量发现 `test*.py`，所以 `pr-check.yml` 不带参数调用。

**练习 3**：给 `_normalize_equation`（[test_jointer.py:16-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L16-L33) 的被测对象）再想一个尚未覆盖的输入。

**答案**：例如混合定界符嵌套（`text $$E=mc^2$$ tail \[a+b\]`，两个公式分别命中 `$$...$$` 与 `\[...\]` 分支，验证第一个匹配被提取而剩余文本完整保留在 caption 中），或空定界符 `$ $`（验证退化输入不会崩）。答案不唯一，关键是先读 [jointer.py:17-30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L17-L30) 附近的实现再挑边界。

### 4.3 边界测试

#### 4.3.1 概念说明

代码会腐烂的两个方向：**职责放错地方**（比如格式无关的 XMLTranslator 里混进了 EPUB 的 ZIP 操作），以及**模块拼起来时契约被破坏**（比如提取器产出的包少了渲染器需要的字段）。普通单元测试管不到这两件事——每个模块单看都是对的。

pdf-craft 用两个专门的测试文件做「架构守卫」：

- `test_module_boundaries.py`（19 行）：模块边界——**谁拥有什么职责**。手段是断言某个符号的归属模块，以及直接扫描子包的全部源码文本，禁止出现越界依赖的字符串。
- `test_composable_boundaries.py`（300+ 行）：组合边界——**跨层流程的契约**。手段是用 4.2 讲的伪协作对象驱动真实的跨模块代码路径，断言可观察的磁盘产物与异常行为。

这两个文件是 u1-l3 讲过的「架构守卫测试固化模块边界」的具体实现。它们最值得注意的地方是**与修复提交一一对应**：本仓库最近两个 fix 都同时改了 `tests/test_composable_boundaries.py`——`2b3670a`（跳过空章节）与 `bbb2d20`（拒绝全失败提取）都是「修 bug + 补回归测试」成对落地（用 `git show --stat` 可验证）。也就是说，这个文件同时是架构约束和新行为的回归锚点。

#### 4.3.2 核心流程

模块边界测试的两种断言模式：

```text
模式 A（符号归属）：
  assert translate_epub.__module__.startswith("pdf_craft.pipeline.epub")
  → EPUB 编排逻辑必须住在 pipeline.epub 子包，不许被搬走

模式 B（源码文本扫描）：
  把子包全部 .py 拼成一个大字符串
  → assertNotIn("pipeline.epub", sources)
  → assertNotIn("Zip(", sources)
  → assertNotIn("search_spine_paths", sources)
  → 格式无关引擎的源码里连「EPUB 专属概念的名字」都不许出现
```

组合边界测试的通用套路：

```text
手写伪协作对象（假 Transform / 假 OCR / 假 Handler）
  → 驱动真实模块（PDFExtractionEngine / ChapterPackageTransformer / OCR）
  → 断言三类结果之一：
     a) 磁盘产物（包目录内容、page_N.failed 标记、document.json）
     b) 异常类型与字段（NoUsableOCRPagesError.failed_page_indexes）
     c) 协作者交互（mock 断言 analyse_toc.assert_not_called()、渲染器收到的参数）
```

#### 4.3.3 源码精读

**（1）模块边界：全文只有两个测试。**

[test_module_boundaries.py:9-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L9-L18) 值得整段读完：

```python
class TestModuleBoundaries(unittest.TestCase):
    def test_epub_orchestration_is_owned_by_pipeline(self):
        self.assertTrue(translate_epub.__module__.startswith("pdf_craft.pipeline.epub"))

    def test_xml_translator_is_format_agnostic(self):
        self.assertTrue(XMLTranslator.__module__.startswith("pdf_craft.transformer.xml_translator"))
        root = Path(__file__).parents[1] / "pdf_craft" / "transformer" / "xml_translator"
        sources = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
        self.assertNotIn("pipeline.epub", sources)
        self.assertNotIn("Zip(", sources)
        self.assertNotIn("search_spine_paths", sources)
```

第一个测试锁定 u9-l1 讲过的事实：EPUB 的 ZIP 迁移、spine 盘点等编排属于 `pipeline.epub`。第二个测试是 u7-l2「XMLTranslator 是格式无关引擎」的可执行证明：如果有人在 `transformer/xml_translator/` 里为了方便直接 `import pipeline.epub` 或 `zipfile.ZipFile(`，CI 立刻红。字符串扫描虽然朴素（可能误伤注释），但零成本且覆盖每个文件——架构约束不需要复杂的依赖分析工具。

**（2）组合边界：全页失败必须拒绝产出空包。**

[test_composable_boundaries.py:112-141](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L112-L141) 是 `bbb2d20`（fix #412「拒绝无可用地页的提取」）的回归测试。注意它的三个技巧：

```python
engine = object.__new__(PDFExtractionEngine)          # 绕过 __init__，不加载 OCR 后端
setattr(engine, "_ocr", cast(OCR, _AllPagesFailOCR())) # 直接注入只吐 FAILED 的假 OCR

with patch("pdf_craft.transform.analyse_toc") as analyse_toc, self.assertRaisesRegex(
    NoUsableOCRPagesError, "no usable pages"
) as raised:
    engine.extract_package(..., ignore_ocr_errors=True, ...)

self.assertEqual(raised.exception.failed_page_indexes, (1, 2))
analyse_toc.assert_not_called()                        # 失败后绝不能继续目录分析
```

`object.__new__` 跳过构造函数（构造会触发 OCR 懒加载，u3-l1 讲过），使测试可以直击 `extract_package` 的错误处理分支；`assert_not_called` 断言下游步骤被彻底短路。这正是 u3-l5 讲过的语义：`ignore_ocr_errors=True` 放行失败页，但可用页为零时必须抛 `NoUsableOCRPagesError` 而不是产出空包。

**（3）组合边界：空章节跳过翻译。**

[test_composable_boundaries.py:143-173](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L143-L173)（对应 `2b3670a`，fix #411「翻译时跳过空章节」）手工在磁盘上摆出两个章节文件——一个 `layouts=[]` 的空章、一个有文本的实章——然后跑真实的 `ChapterPackageTransformer`：

```python
translator = _DeterministicXMLTranslator()
target = ChapterPackageTransformer(
    ChapterXMLTransformer(translator)
).transform(source, root / "target")

self.assertEqual(translator.calls, 1)              # 空章没有进转换器
self.assertEqual(                                 # 空章原样复制
    (target.chapters_path / "chapter_1.xml").read_text(),
    (source.chapters_path / "chapter_1.xml").read_text(),
)
self.assertIn("T:text", (target.chapters_path / "chapter_2.xml").read_text())
```

`_DeterministicXMLTranslator`（[test_composable_boundaries.py:97-108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L97-L108)）给每个文本节点加 `T:` 前缀并数调用次数——用调用计数与产物内容双重断言钉死「跳过」行为。

**（4）组合边界：DocumentPackage 是中立契约的可执行证明。**

[test_composable_boundaries.py:183-196](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L183-L196) 用 `_FakeTransform` 产出包，断言 `ocr/` 缓存目录不存在（u6-l1 讲过它不属于契约成员），然后把同一个包分别喂给两个渲染器：

```python
self.assertFalse((root / "package" / "ocr").exists())
with patch("pdf_craft.renderer.markdown.renderer.render_markdown_file") as markdown:
    MarkdownRenderer().render(package, root / "book.md")
self.assertEqual(markdown.call_args.args[0], package.chapters_path)
with patch("pdf_craft.renderer.epub.renderer.render_epub_file") as epub:
    EpubRenderer().render(package, root / "book.epub")
self.assertEqual(epub.call_args.args[0], package.chapters_path)
```

「提取器产物 → 两个渲染器都能消费且不含私有缓存」这条 u6-l1 的架构论述，在这里是一条跑在 CI 里的断言。

**（5）组合边界：断点续跑的两条时间线。**

[test_composable_boundaries.py:243-260](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L243-L260) 验证 u3-l3 的断点续跑：第一次 `recognize` 中途关闭生成器后 `render_count == 1`，复用同一 `ocr/` 目录重跑，`render_count` 仍是 1——页面位图绝不重复渲染，几何来自缓存。[test_composable_boundaries.py:262-286](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L262-L286) 则验证被 `ignore_ocr_errors` 放行的失败页留下 `page_1.failed` 标记、不写 `done` 哨兵，重跑时该页真正重试且成功后标记被清除。两条测试合起来把 u3-l3 讲的「三件套缓存语义」全部固化为回归保护。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「fix 与边界测试成对落地」，然后为 `test_module_boundaries.py` 追加一条新的架构断言。

**操作步骤**：

1. 在仓库根目录运行（只读 git 命令）：

```bash
git show --stat --oneline bbb2d20
git show --stat --oneline 2b3670a
```

   观察两个提交都修改了 `tests/test_composable_boundaries.py`，再分别用 `git show bbb2d20 -- tests/test_composable_boundaries.py` 看新增的测试函数与源码改动的对应关系。

2. 阅读 [test_module_boundaries.py:12-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L18)，模仿其源码扫描模式，给该文件追加一个测试（示例代码）：

```python
# 示例代码：追加到 tests/test_module_boundaries.py 的 TestModuleBoundaries 类中
def test_markdown_renderer_does_not_depend_on_pipeline(self):
    """渲染器只消费 DocumentPackage，不得反向依赖翻译管线。"""
    from pdf_craft.renderer.markdown import renderer as markdown_renderer
    self.assertTrue(markdown_renderer.__name__.startswith("pdf_craft.renderer.markdown"))
    root = Path(__file__).parents[1] / "pdf_craft" / "renderer"
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in root.rglob("*.py")
    )
    self.assertNotIn("pipeline.epub", sources)
    self.assertNotIn("pipeline.pdf", sources)
```

3. 运行 `python test.py test_module_boundaries`。

**需要观察的现象**：git 输出中两个 fix 的文件清单都包含 `tests/test_composable_boundaries.py`；新测试在本仓库当前代码上应当通过（u6 讲过渲染器只依赖 DocumentPackage 契约）。

**预期结果**：如果断言失败，说明渲染器源码出现了对管线的直接依赖——这恰是该测试要拦截的架构退化。通过与否待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`test_xml_translator_is_format_agnostic` 为什么扫描 `Zip(` 而不是扫描 `import zipfile`？

**答案**：`Zip(` 是调用 `zipfile.ZipFile(...)` 构造时必然出现的字符串，同时也能拦住 `from zipfile import ZipFile` 后的任何用法；而只扫 `import zipfile` 会被 `from zipfile import ...` 绕过。当然字符串扫描本质是启发式（注释里出现 `Zip(` 会误报），它的价值在于零成本、全文件覆盖——架构守卫追求的是「让越界变得很难悄悄发生」，不是完美的依赖分析。

**练习 2**：为什么全页失败测试要 `analyse_toc.assert_not_called()`，只断言抛异常不够吗？

**答案**：只断言异常只能证明「最终失败了」，不能证明「失败发生在正确的地方」。如果引擎先跑完目录分析再抛错，测试照样绿，但浪费 token 的 bug 就漏网了。`assert_not_called` 把「失败必须短路后续步骤」也写进契约——组合边界测试关心的是模块间的协作顺序，不只是最终返回值。

**练习 3**：`test_package_translation_skips_empty_chapters` 为什么比较两个 XML 文件的**完整文本**相等，而不是只检查转换器调用次数？

**答案**：调用次数证明「空章没进转换器」，但「跳过」还隐含另一半承诺——空章文件必须原样出现在目标包里（否则渲染时会缺章节）。逐字节比较源与目标的 `chapter_1.xml` 同时钉死这两点：既没有翻译，也没有被弄丢或被改写。一个断言覆盖一个完整契约，比两个松散断言更可靠。

## 5. 综合实践

把本讲三个模块串成一条完整的「新功能测试链」。场景：假设你为 pdf-craft 贡献了一个改进——支持识别中文书里常见的「图注前置」格式（图片上方的 `图3-1 …` 段落应归为图片标题），完成以下四步：

1. **单元层**：仿照 4.2.4 的做法，为你新增/修改的判定函数写纯函数测试，必须覆盖中文正则的命中与不命中两个方向（参考 [test_toc_text.py:33-45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_toc_text.py#L33-L45) 同时测中英两种文本的风格）。
2. **组合层**：仿照 [test_composable_boundaries.py:198-223](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_composable_boundaries.py#L198-L223)，用 `DocumentPackage.from_path` 手工摆一个含图片与前置图注的最小包，驱动真实管线，断言产出的 `PDFReplacement` 或渲染文本正确。
3. **冒烟层**：复制 `tests/smoke/minimal.json` 为 `figure-caption-matrix.json`，把 `asset` 换成 `figure-caption.pdf`（资产池里现成有这个文件，见 [tests/assets/](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/assets/) 目录），先用 `--dry-run` 验证 `planned`，再真跑验证 `passed`。
4. **复盘**：写一页笔记回答——你的改动分别被哪一层测试保护？如果只做其中一层，哪类回归会漏掉？（例如：只有单元测试会漏「包契约被破坏」，只有冒烟会漏「细粒度正则回归且每次跑都花钱」。）

整个过程不改任何源码即可完成第 3、4 步；第 1、2 步产物是新增测试文件，属于本仓库「fix + 回归测试成对落地」惯例的实践。

## 6. 本讲小结

- pdf-craft 的测试分三层：**单元测试**（`test.py` 驱动的 `tests/test*.py`，CI 每次提交必跑、零网络）、**边界测试**（模块边界 + 组合边界两类架构守卫）、**冒烟矩阵**（`pdf_craft_tool smoke matrix` 跑 JSON 描述的真实后端组合，按需手动触发）。
- 冒烟矩阵的核心是 `SmokeRun` 冻结数据类与 `expand_matrix` 的 `defaults | item` 合并；八条路由按 PDF/EPUB 资产格式分组校验；每条 run 落盘 `manifest.json`/`checks.json`/`logs/`，五阶段时间线让失败可追溯，密钥经 `_redact` 永不落盘，无 CUDA 记 `skipped` 而非 `failed`。
- 单元测试的三种武器是纯函数直测、伪协作对象（`_FakeTransform`/`_AllPagesFailOCR`）与 `unittest.mock.patch`；因为核心模块靠 Protocol 与磁盘契约解耦，大多数测试不需要 mock 框架。
- 模块边界测试用 `__module__` 断言与源码文本扫描（禁止 `Zip(`、`pipeline.epub` 出现在 xml_translator 子包）固化「格式无关引擎」的架构约束；组合边界测试用伪协作者驱动真实跨层流程，把「全失败拒绝空包」「空章跳过翻译」「断点续跑不重渲染」等契约变成 CI 断言。
- 仓库最近两个 fix（`2b3670a`、`bbb2d20`）都同步修改了 `tests/test_composable_boundaries.py`——补测试与新行为成对提交是这个仓库的明确惯例。

## 7. 下一步学习建议

本讲是 u11「工程化」单元的收尾，你已经具备为这个仓库贡献代码的完整测试素养。下一讲 **u12-l1 综合实战：编写自定义转换步骤** 是全手册的收官：把 u7 的转换器协议、u8 的 LLM 基础设施与本讲的测试方法结合起来，实现一个自定义 `PackageTransformer`/`TranslationStep` 并接入 `convert_pdf_to_epub`，同时按本讲的分层原则为它配上测试。建议继续精读的源码：`pdf_craft_tool/smoke/runner.py` 的 `_translate_package_steps`（看冒烟如何走公开门面方法而非内部 API），以及 `git log -- tests/` 浏览历史上每个 fix 对应的回归测试——那是理解「这个项目认为什么值得保护」的最快途径。
