# 页面提取后端：doc-page-extractor 适配（u3-l4）

## 1. 本讲目标

上一讲（u3-l3）我们读完了 OCR 驱动器 `OCR.recognize` 的事件流、缓存与断点续跑，但刻意把一件事留在了黑盒里：`RENDERED` 事件之后、`COMPLETE` 事件之前，那行 `self._extractor.image2page(...)`（[pdf_craft/pdf/ocr.py:166-178](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L166-L178)）到底做了什么。本讲就打开这个黑盒。

学完本讲，你应该能够：

1. 说清 `PageExtractorNode` 如何用一条 `isinstance` 链把六种 OCR 配置（u2-l1）分派到上游 `doc-page-extractor` 包的六个工厂函数；
2. 解释为什么所有对 `doc_page_extractor` 的 import 都写在函数体内部、本地运行时缺失时为什么会得到一条带安装指令的 `RuntimeError`；
3. 读懂 OCR 结果的布局映射：上游十种布局类型如何归一化为 `PageLayout.ref`、正文（body）与脚注（footnotes）两串布局如何划分、重复噪声如何被 n-gram 过滤器拦截；
4. 手工打开并统计提取产物 `ocr/page_N.xml`，把里面的 `ref` 值反推回上游布局类型。

## 2. 前置知识

本讲需要以下几个概念，用通俗语言先解释一遍：

- **上游包与适配层**：pdf-craft 自己不做 OCR 模型推理，真正的 OCR 能力来自一个独立发布的依赖包 `doc-page-extractor`（见 [pyproject.toml:33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L33) 的 `"doc-page-extractor>=1.2.0,<2.0.0"`）。pdf-craft 里没有这个包的源码，本讲的 `page_extractor.py` 是一层**适配器（adapter）**：把「pdf-craft 自己的六种配置类」翻译成「上游的工厂函数调用」，再把「上游的识别结果」翻译成「pdf-craft 自己的 `Page`/`PageLayout` 数据结构」。
- **工厂函数（factory）**：一种只负责"造对象"的函数。上游提供了 `create_deepseek_ocr_page_extractor`、`create_unlimited_ocr_vendor_page_extractor` 等工厂，pdf-craft 不关心造出来的对象内部长什么样，只约定它有 `extract_page_results`、`download_ocr_model`、`load_ocr_model` 等方法可调。
- **optional dependency（可选依赖 / extra）**：Python 包可以把重型依赖声明成"可选档位"。pdf-craft 的标准安装只装 `doc-page-extractor` 本体；`pip install 'pdf-craft[local]'` 才会额外装上本地推理需要的 PyTorch/CUDA 等重型运行时（见 [pyproject.toml:48-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51)）。这就是"本地配置"和"vendor 配置"运行位置差异的物理基础（u2-l1 已讲配置侧，本讲看消费侧）。
- **isinstance 链**：用一连串 `if isinstance(x, A)` / `elif isinstance(x, B)` … 判断对象的具体类型。它除了分派逻辑，还有一个副产品：类型检查器（pyright）能在每个分支里把 `self._ocr` 收窄成具体类型，从而安全访问该类型独有的字段（比如本地配置的 `models_cache_path`、vendor 配置的 `api_key`）。
- **n-gram**：文本里连续 n 个字符（或词）组成的片段。比如 `"abcabc"` 里 3-gram `"abc"` 出现了 2 次。OCR 大模型偶尔会"复读机式"地重复同一段话（术语叫 neural text degeneration，神经文本退化），表现为同一个 n-gram 连续重复几十次——用 n-gram 统计就能把这种噪声识别出来。
- **代理字符（surrogate）**：Unicode 区间 U+D800–U+DFFF 的字符。它们只在 UTF-16 编码内部有意义，单独出现就是非法字符，而且会让很多 XML 写入器崩溃，所以落盘前要剔除。
- **两阶段识别（stage）**：开启 `includes_footnotes`（来自 `ExtractionOptions`，u2-l2）后，上游会对一页跑两轮：第一轮（stage 1）分析整页，第二轮（stage 2）针对脚注区域再做一次更细的识别。每个阶段上游生成器都会产出一对 `(图像, 结果)`。

另外承接已建立的知识：`page_N.xml` 断点续跑缓存、`OCREvent` 六种事件、token 双轨预算都已在 u3-l3 讲过，本讲直接使用这些结论。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/pdf/page_extractor.py` | 本讲主角。`PageExtractorNode` 适配器：配置分派、懒加载、`image2page` 主流程、布局映射与过滤 |
| `pdf_craft/pdf/types.py` | `Page`/`PageLayout` 数据结构与 `page_N.xml` 的 XML 编解码（`encode`/`decode`） |
| `pdf_craft/pdf/ngrams.py` | `has_repetitive_ngrams`：字符级重复 n-gram 检测，过滤 OCR"复读机"噪声 |
| `pdf_craft/pdf/ocr.py` | 调用方。OCR 驱动器把渲染好的图像交给 `image2page`，并把结果编码落盘 |
| `pdf_craft/ocr_config.py` | 六种 OCR 配置类（u2-l1 已精读，本讲只看它们如何被消费） |
| `pdf_craft/common/asset.py` | `ASSET_TAGS`（三种资源标签）与 `AssetHub.clip`（裁剪图片、按内容哈希存入 `assets/`） |
| `pdf_craft/common/surrogates.py` | `remove_surrogates`：剔除代理字符 |
| `tests/test_page_extractor_structured.py` | 用"假结构体"直接驱动 `_iter_page_layouts` 的单元测试，不需要任何 OCR 服务 |
| `pyproject.toml` | 声明 `doc-page-extractor` 硬依赖与 `[local]` 可选档 |

提醒：`doc_page_extractor` 是外部上游包，仓库里没有它的源码。本讲把它当作一个只有接口契约的黑盒——这正是适配层存在的意义。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**后端工厂**、**懒加载与错误提示**、**布局类型映射**。

### 4.1 后端工厂：六种配置到上游工厂的映射

#### 4.1.1 概念说明

u2-l1 讲过，`PDFOptions.ocr` 接受六个 frozen dataclass 之一（三族模型 × 本地/vendor 两种运行位置）。但那六个类只是**声明**，真正"按声明造出能干活的识别器"的工作，由 `PageExtractorNode._create_page_extractor` 完成。

这个模块解决的问题是：**配置对象与执行器彻底解耦**。用户在脚本里构造配置时不需要网络、不需要 GPU（构造是纯数据操作）；而造执行器这一步被推迟到真正开始提取时才发生。映射规则一句话概括：

- 三种**本地**配置 → 上游三个本地工厂，传 `model_path` / `local_only` / `enable_devices_numbers`（DeepSeek 两族共用一个工厂，用 `ocr_model` 字符串区分 `"deepseek-ocr"` 与 `"deepseek-ocr2"`）；
- 三种 **vendor** 配置 → 先把 pdf-craft 的配置**逐字段复制**成上游的同名配置类，再交给上游 vendor 工厂。

#### 4.1.2 核心流程

```text
PageExtractorNode(ocr=config)          # 只存配置，_page_extractor = None
  │
  ├─ 首次需要时 _get_page_extractor()
  │    └─ _create_page_extractor()     # isinstance 链分派：
  │         ├─ DeepSeekOCRLocalConfig   → create_deepseek_ocr_page_extractor(ocr_model="deepseek-ocr",  model_path, local_only, enable_devices_numbers)
  │         ├─ DeepSeekOCR2LocalConfig  → create_deepseek_ocr_page_extractor(ocr_model="deepseek-ocr2", 同上)
  │         ├─ UnlimitedOCRLocalConfig  → create_unlimited_ocr_page_extractor(model_path, local_only, enable_devices_numbers)
  │         ├─ DeepSeekOCRVendorConfig  → 逐字段复制 → create_deepseek_ocr_vendor_page_extractor
  │         ├─ DeepSeekOCR2VendorConfig → 逐字段复制 → create_deepseek_ocr2_vendor_page_extractor
  │         ├─ UnlimitedOCRVendorConfig → 复制 ak/sk/base_url/轮询间隔/超时 → create_unlimited_ocr_vendor_page_extractor
  │         └─ 其余类型 → TypeError
  │
  └─ 造好的执行器被缓存在 self._page_extractor，后续复用
```

#### 4.1.3 源码精读

先看类骨架与懒加载入口。构造函数只保存配置、把 `_page_extractor` 置空；`_get_page_extractor` 是典型的"首次调用才创建、之后复用"的惰性初始化（承接 u1-l4 讲过的惰性初始化模式）：

[pdf_craft/pdf/page_extractor.py:53-61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L53-L61) —— `PageExtractorNode` 构造时只存配置；`_get_page_extractor` 在首次被调用时才触发 `_create_page_extractor()` 并缓存结果。

分派链的前两个分支处理 DeepSeek 本地两族。注意两点：`from doc_page_extractor.extractor import ...` 写在分支**内部**（懒加载，4.2 详述）；DeepSeek 与 DeepSeek OCR 2 共用同一个上游工厂，仅靠 `ocr_model` 字符串区分：

[pdf_craft/pdf/page_extractor.py:65-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L65-L88) —— 本地 DeepSeek / DeepSeek OCR 2 配置分别以 `ocr_model="deepseek-ocr"` / `"deepseek-ocr2"` 调用上游工厂，并透传模型缓存路径、离线模式与可用设备号。

Unlimited 本地配置走另一个工厂，且没有 `ocr_model` 参数（它本来就是独立模型族）：

[pdf_craft/pdf/page_extractor.py:89-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L89-L99) —— `UnlimitedOCRLocalConfig` 分派到 `create_unlimited_ocr_page_extractor`，只传模型路径、离线模式与设备号。

vendor 分支则是"逐字段复制"：pdf-craft 在 [ocr_config.py:84-112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L84-L112) 定义的三个 vendor 配置（DeepSeek 双族走 OpenAI 兼容三件套 `base_url`/`api_key`/`model` 加采样与超时参数，Unlimited 用百度 `ak`/`sk` 并多出轮询间隔），在这里被复制成上游 `doc_page_extractor.adapters` 里的**同名配置类**再交给工厂：

[pdf_craft/pdf/page_extractor.py:100-137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L100-L137) —— DeepSeek 两族 vendor 配置的 7 个字段（`base_url`、`api_key`、`model`、`temperature`、`top_p`、`max_tokens`、`timeout_seconds`）被逐字段复制进上游配置类，再调用对应 vendor 工厂。

[pdf_craft/pdf/page_extractor.py:138-155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23f551c50/pdf_craft/pdf/page_extractor.py#L138-L155) —— Unlimited vendor 配置复制 `ak`/`sk`/`base_url`/`poll_interval_seconds`/`timeout_seconds` 五个字段；链的末尾是兜底的 `TypeError`，防御"未来新增了配置类型但忘了适配"的情况。

为什么不干脆把上游配置类直接暴露给用户？因为 pdf-craft 想把上游版本演进隔离在自己的适配层里——上游 1.x 内部怎么改名，都不影响 `pdf_craft.ocr_config` 这层公开 API（u1-l3 讲过 `__init__.py` 划定公开边界的思想）。

另外还有一个配置相关的守卫值得注意：DeepSeek OCR 2 的本地模型在 `ocr_size="tiny"` 档位下识别不可靠，适配层直接拒绝这种组合：

[pdf_craft/pdf/page_extractor.py:336-341](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L336-L341) —— `_validate_ocr_size` 在每次 `image2page` 开头被调用（[L181](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L181)），`DeepSeekOCR2LocalConfig` 搭配 `"tiny"` 会抛 `ValueError`，提示改用 `"base"` 档。

#### 4.1.4 代码实践

**实践目标**：不联网、不装 GPU，验证三种本地配置确实分派到正确的上游工厂与参数。

**操作步骤**：把下面的脚本存为 `audit_factory.py` 并运行（示例代码，思路来自 `tests/test_page_extractor_structured.py` 用假对象驱动内部方法的先例）。它在导入 `PageExtractorNode` **之前**，往 `sys.modules` 里塞一个假的 `doc_page_extractor.extractor` 模块，把工厂函数替换成"只记录调用参数"的桩：

```python
# 示例代码：audit_factory.py —— 用桩模块观察本地配置的分派结果（离线可跑）
import sys
import types

from pdf_craft.ocr_config import (
    DeepSeekOCRLocalConfig,
    DeepSeekOCR2LocalConfig,
    UnlimitedOCRLocalConfig,
)

calls = []

def _factory(name):
    def _create(**kwargs):
        calls.append((name, kwargs))
        return object()  # 只记录，不真正创建
    return _create

fake_pkg = types.ModuleType("doc_page_extractor")
fake_ext = types.ModuleType("doc_page_extractor.extractor")
fake_ext.create_deepseek_ocr_page_extractor = _factory("create_deepseek_ocr_page_extractor")
fake_ext.create_unlimited_ocr_page_extractor = _factory("create_unlimited_ocr_page_extractor")
fake_pkg.extractor = fake_ext
sys.modules["doc_page_extractor"] = fake_pkg
sys.modules["doc_page_extractor.extractor"] = fake_ext

from pdf_craft.pdf.page_extractor import PageExtractorNode  # 桩就位后再导入

for config in (
    DeepSeekOCRLocalConfig(),
    DeepSeekOCR2LocalConfig(),
    UnlimitedOCRLocalConfig(),
):
    calls.clear()
    node = PageExtractorNode(config)
    node._get_page_extractor()  # pylint: disable=protected-access
    print(type(config).__name__, "->", calls)
```

**需要观察的现象**：三种配置各自触发了哪个工厂、传了哪些关键字参数。

**预期结果**（依据源码 [L65-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L65-L99) 推导）：

```text
DeepSeekOCRLocalConfig  -> [('create_deepseek_ocr_page_extractor', {'ocr_model': 'deepseek-ocr',  'model_path': None, 'local_only': False, 'enable_devices_numbers': None})]
DeepSeekOCR2LocalConfig -> [('create_deepseek_ocr_page_extractor', {'ocr_model': 'deepseek-ocr2', 'model_path': None, 'local_only': False, 'enable_devices_numbers': None})]
UnlimitedOCRLocalConfig -> [('create_unlimited_ocr_page_extractor', {'model_path': None, 'local_only': False, 'enable_devices_numbers': None})]
```

三个本地配置都走了懒加载路径且只创建一次（可在循环里二次调用 `_get_page_extractor()`，观察 `calls` 不再增长）。若你的环境中真实 `doc-page-extractor` 已安装，本脚本会用桩覆盖它——仅对当前进程生效，无副作用。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 `isinstance` 链而不是 `{type: handler}` 字典做分派？

**参考答案**：一是每个分支内部要做延迟 import（把 `doc_page_extractor` 的加载推迟到真正选中该分支时），函数体内写 import 比往字典里塞 lambda 更自然；二是 isinstance 分支让 pyright 在每个分支里把 `self._ocr` 收窄为具体配置类型，访问 `models_cache_path`、`api_key` 等字段时能获得静态类型检查；三是链尾可以自然地落一个兜底 `TypeError`（[L155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L155)），字典遇到未知类型只会 `KeyError`。

**练习 2**：`DeepSeekOCRVendorConfig` 的哪些字段会被复制进上游配置？漏掉一个会怎样？

**参考答案**：7 个字段：`base_url`、`api_key`、`model`、`temperature`、`top_p`、`max_tokens`、`timeout_seconds`（[L108-118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L108-L118)）。漏掉的话该参数不会报错，而是静默落回上游配置类自己的默认值——用户的调优（比如把 `timeout_seconds` 调大）会无声失效，所以这种"逐字段复制"的适配代码改动时要逐字段核对。

**练习 3**：`UnlimitedOCRVendorConfig` 与 DeepSeek 双族 vendor 配置在映射时有何不同？

**参考答案**：Unlimited 用的是百度式双密钥 `ak`/`sk` 而非 OpenAI 式 `api_key`，没有 `temperature`/`top_p`/`max_tokens` 这类采样参数，多出 `poll_interval_seconds`（异步任务的轮询间隔，u2-l1 讲过），映射时复制 5 个字段（[L146-154](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L146-L154)）。

### 4.2 懒加载与错误提示

#### 4.2.1 概念说明

这个模块回答两个"为什么"：

1. **为什么所有 `doc_page_extractor` 的 import 都写在函数内部？** 因为 `doc-page-extractor` 及其背后的重型依赖很重（本地档还涉及 PyTorch/CUDA）。pdf-craft 的用户里有相当一部分只翻译现成 EPUB、根本不碰 PDF（u1-l4 讲过 EPUB-only 用户）。如果模块顶部就 import，这些用户装包时就要被迫背上整条依赖链。延迟到函数体内，配合 `transform` 模块的懒加载引擎设计（u3-l1），做到了"不用 OCR 就不加载 OCR 代码"。
2. **本地运行时缺失时为什么抛 `RuntimeError` 而不是放任 `ModuleNotFoundError`？** 因为 `ModuleNotFoundError` 在用户眼里像"代码有 bug"，而它实际是"少装了一个可选档位"的**安装问题**。适配层把它翻译成一句带修复动作的提示，用户照抄命令即可解决。

#### 4.2.2 核心流程

```text
用户调用 convert_pdf_to_*（首次真正提取）
  → OCR 引擎加载（u3-l1 的懒加载引擎）
    → PageExtractorNode._get_page_extractor()
      → _create_page_extractor() 选中本地分支
        → 函数体内 import doc_page_extractor.extractor   # 本体是硬依赖，必装，此处不会失败
        → factory() 内部尝试加载本地推理运行时
             ├─ 运行时齐全 → 正常创建执行器
             └─ ModuleNotFoundError → 转译为 RuntimeError：
                 "Local OCR requires the optional local runtime.
                  Install it with: pip install 'pdf-craft[local]'"
```

关键区分：`doc-page-extractor` **本体**在 [pyproject.toml:33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L33) 是硬依赖，标准安装一定有；缺的是 `[local]` 档（[pyproject.toml:48-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51)）里由上游声明的 PyTorch 等推理运行时——`ModuleNotFoundError` 发生在 `factory()` 调用**内部**，而不是那行 import 上。

#### 4.2.3 源码精读

转译逻辑只有一个短函数，docstring 直接点明意图——"把缺失的可选本地运行时变成一个可操作的（可 actionable 的）包错误"：

[pdf_craft/pdf/page_extractor.py:42-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L42-L50) —— `_create_local_page_extractor` 包装本地工厂调用：捕获 `ModuleNotFoundError`，用 `raise ... from error` 保留原始异常链，抛出带 `pip install 'pdf-craft[local]'` 指令的 `RuntimeError`。三个本地分支（[L69-76](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L69-L76)、[L81-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L81-L88)、[L93-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L93-L99)）都经由它创建执行器；vendor 分支不需要包装（远程服务不依赖本地运行时）。

与之配套的是模型管理方法的类型守卫。`download_models`/`load_models` 是给本地模型预下载、预加载用的（OCR 驱动器在 [ocr.py:53-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L53-L57) 转发调用），对 vendor 配置没有意义，于是先用类型元组把关：

[pdf_craft/pdf/page_extractor.py:35-39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L35-L39) —— `_LOCAL_OCR_CONFIG_TYPES` 元组收集三种本地配置类型，供下面的守卫做 `isinstance` 判断。

[pdf_craft/pdf/page_extractor.py:157-165](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L157-L165) —— `download_models` 与 `load_models` 对非本地配置直接抛 `RuntimeError("download_models is only available for local OCR.")`；对本地配置则触发懒加载后转发给上游执行器的 `download_ocr_model` / `load_ocr_model`。

`image2page` 内部还有三处函数体内的延迟 import，进一步印证这个模块的懒加载纪律——`AbortError`/`TokenLimitError`/`plot`/`ExtractionContext` 都等到真正识别一页时才加载：

[pdf_craft/pdf/page_extractor.py:181-184](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L181-L184) —— 每次识别一页时才从上游导入中止/预算异常类、绘图函数与 `ExtractionContext`。

一个把全部线索串起来的推论（承接 u2-l1 的 `ensure_ocr_config` 兜底逻辑）：用户什么 OCR 配置都不传时，兜底默认是 `DeepSeekOCRLocalConfig`（本地档）。于是在"标准安装 + 不传配置"的环境里，第一次提取就会命中本节的 `RuntimeError`，并直接收到安装指令——错误提示与默认值设计是配套的。

#### 4.2.4 代码实践

**实践目标**：亲眼看到两类守卫错误的信息。

**操作步骤**：在标准安装（未装 `[local]` 档）的环境运行下面的脚本（示例代码）：

```python
# 示例代码：probe_local_error.py
from pathlib import Path

from pdf_craft.ocr_config import (
    DeepSeekOCR2LocalConfig,
    DeepSeekOCRLocalConfig,
    DeepSeekOCRVendorConfig,
)
from pdf_craft.pdf.page_extractor import PageExtractorNode

# ① 本地运行时缺失时的提示
node = PageExtractorNode(DeepSeekOCRLocalConfig(models_cache_path=Path("./models")))
try:
    node._get_page_extractor()  # pylint: disable=protected-access
except RuntimeError as error:
    print("RuntimeError:", error)

# ② vendor 配置调用模型管理方法的守卫
vendor_node = PageExtractorNode(DeepSeekOCRVendorConfig(
    base_url="https://api.example.com/v1", api_key="dummy", model="dummy",
))
try:
    vendor_node.download_models(None)
except RuntimeError as error:
    print("RuntimeError:", error)

# ③ OCR2 本地 + tiny 尺寸的守卫（4.1 提过）
node3 = PageExtractorNode(DeepSeekOCR2LocalConfig())
try:
    node3._validate_ocr_size("tiny")  # pylint: disable=protected-access
except ValueError as error:
    print("ValueError:", error)
```

**需要观察的现象**：三段各自抛出的异常类型与消息内容。

**预期结果**：

- ①（仅当环境未装 local 档）`RuntimeError: Local OCR requires the optional local runtime. Install it with: pip install 'pdf-craft[local]'`；
- ② `RuntimeError: download_models is only available for local OCR.`；
- ③ `ValueError: deepseek-ocr2-local is not reliable with ocr_size='tiny'; use ocr_size='base' for the validated local OCR2 path.`。

注意：① 依赖环境状态——若本机已安装 local 档（或有 GPU 运行时），工厂可能正常创建而不抛错，属正常现象；②③ 与环境无关，必定可复现。①的完整触发链路（经由 `convert_pdf_to_markdown` 首次提取时才抛出）**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `ModuleNotFoundError` 转成 `RuntimeError` 的价值在哪里？`from error` 子句又起什么作用？

**参考答案**：价值在"可操作性"——`ModuleNotFoundError: No module named 'torch'` 让用户以为是代码缺陷，而转译后的消息直接给出修复命令 `pip install 'pdf-craft[local]'`，把安装问题还原成安装问题。`raise ... from error` 保留了原始异常链，用户排查时仍能看到底层缺的是哪个模块（[L47-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L47-L50)）。

**练习 2**：如果删掉 `_create_local_page_extractor` 这层包装，把工厂调用写裸，用户会看到什么？

**参考答案**：标准安装用户第一次提取时会在 `factory()` 内部直接看到原始 `ModuleNotFoundError`（比如缺 `torch`），既不知道该装什么，也可能误判为 pdf-craft 的 bug；而且该异常发生在懒加载引擎深处，调用栈对初学者非常不友好。

**练习 3**：`from doc_page_extractor.extractor import create_deepseek_ocr_page_extractor` 这行写在分支内部，除了懒加载还有什么好处？

**参考答案**：还避免了**部分初始化**问题——如果写在模块顶部，任何一个上游符号改名都会让整个 `pdf_craft.pdf.page_extractor` 模块导入失败，连不需要 OCR 的功能（比如 EPUB 翻译）都会被连坐；写在函数内部则把故障半径缩小到"真正走到该分支的调用"。（这正是 u3-l1 讲过的懒加载引擎设计的延续。）

### 4.3 布局类型映射

#### 4.3.1 概念说明

后端工厂解决"谁来识别"，本模块解决"识别结果怎么进 pdf-craft 的数据模型"。上游识别一页后返回一个 `structured` 对象，里面是带类型的块（block）列表：每块有布局类型 `kind`、边界框 `det`（left, top, right, bottom 四个像素坐标）、可选的 `text`/`html` 与子块 `children`。适配层要做四件事：

1. **归一化类型**：上游的十种布局类型被映射表折叠成八种 `ref` 值——`title` 改叫 `sub_title`，`footnote` 与 `aside` 都归并为 `text`，不在表里的类型直接丢弃；
2. **划分正文/脚注**：按"是不是脚注块"和"处于第几个识别阶段"把块分进 `body_layouts` 或 `footnotes_layouts` 两串；
3. **清洗**：剔除代理字符、压缩空白、把越界的边界框裁回图像范围、用 n-gram 过滤器拦截"复读机"文本；
4. **提取资源**：图片/表格/公式三种块（`ASSET_TAGS`）从页面图像上裁剪下来，按内容哈希存入 `assets/`，`hash` 写进布局。

产出的 `Page` 最终被 `encode` 成 `ocr/page_N.xml`（u3-l3 讲过的断点续跑缓存），格式由 `pdf_craft/pdf/types.py` 定义。

#### 4.3.2 核心流程

`image2page` 的主循环（两阶段识别）：

```text
image2page(image, page_index, ..., includes_footnotes, ...)
  1. 校验 ocr_size 组合（见 4.1）
  2. 构造 ExtractionContext（中止检查、token 双轨预算、临时输出目录）
  3. generator = 上游执行器.extract_page_results(
        image, size=ocr_size,
        stages = 2 if includes_footnotes else 1,   # 是否加跑脚注阶段
        context, device_number)
  4. 循环 next(generator)，每轮拿到 (本阶段图像, 本阶段结果)：
       - AbortError / TokenLimitError 原样放行（中断与预算有专门语义）
       - 其他异常包装成 OCRError（带页码与阶段号，可被 ignore_ocr_errors 策略处理）
       - 对结果的每个块走 _iter_page_layouts（见下）
       - 可选：plot 保存 page_N_stage_M.png 调试图
  5. 返回 Page(index, 封面原图, body_layouts, footnotes_layouts,
              input_tokens, output_tokens)
```

`_iter_page_layouts` 的逐块过滤管线：

```text
对 structured.blocks 中的每个块：
  1. kind.value 查 _LAYOUT_KIND_TO_REF → ref；查不到 → 丢弃
  2. kind == "footnote" 且未开启 includes_footnotes → 丢弃
  3. 文本归一化：block.html 优先于 block.text；子块文本以 "\n" 拼接；
     去代理字符；空白压成单空格
  4. det 裁剪到图像边界内；若 left≥right 或 top≥bottom（退化框）→ 丢弃
  5. 重复 n-gram 两档过滤（短模式 / 长模式）→ 命中 → 丢弃
  6. 第二阶段且 ref ∈ ASSET_TAGS → 丢弃（资源已在第一阶段裁过）
  7. ref ∈ ASSET_TAGS → asset_hub.clip 裁图落盘，得到内容哈希
  8. yield (PageLayout(ref, det, text, hash, order), is_footnote)

调用方按 (is_footnote, 阶段号) 分拣：
  is_footnote=True                 → footnotes_layouts
  第一阶段                         → body_layouts
  第二阶段且非资源                 → footnotes_layouts（第二道保险）
```

n-gram 过滤的数学：设文本长度为 \( L \)，检测的 n-gram 长度为 \( n \)，重复阈值（触发次数）为 \( T \)。要让同一个 n-gram 连续出现 \( T \) 次，至少需要 \( n \times T \) 个字符，所以代码先做早退判断 \( L < n_{\min} \times T \)，再把搜索空间收缩为：

\[ n \in [\,n_{\min},\ \min(n_{\max},\ \lfloor L/T \rfloor)\,] \]

两档参数为：短模式 \( n_{\min}=2, n_{\max}=5, T=16 \)（抓 `"1.1.1.1.1."` 这类短复读）；长模式 \( n_{\min}=6, n_{\max}=20, T=8 \)（保守地抓长句复读）。采用**字符级**切分而不是词级，是为了对没有空格分隔的中文同样有效。

#### 4.3.3 源码精读

先看本讲的主角常量——布局类型映射表。十种上游类型映射为八种 `ref`：

[pdf_craft/pdf/page_extractor.py:22-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L22-L33) —— `_LAYOUT_KIND_TO_REF`：`title` 归一化为 `sub_title`（与后续章节层级的命名对齐）；`footnote` 与 `aside`（旁注）都归并为普通 `text`；六种带语义的类型保持原名；不在表里的类型在下游按 `"unknown"` 丢弃。

资源标签只有三种，由公共模块统一定义（第四章的 `jointer.py`、渲染器都会复用同一份常量）：

[pdf_craft/common/asset.py:8-9](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/asset.py#L8-L9) —— `ASSET_TAGS = ("image", "table", "equation")`：会被裁剪成图片文件存入 `assets/` 的三种布局。

再看主循环。`stages=2 if includes_footnotes else 1` 决定是否加跑脚注阶段；注意 `image, page_result = next(generator)` 里 `image` 被不断更新——上游每个阶段随结果一起返回该阶段所用的图像，后续的边界框裁剪与资源裁剪都以它为坐标系（从 pdf-craft 侧能确认这一约定；上游第二阶段具体如何裁出脚注区域，属于 `doc-page-extractor` 内部实现）：

[pdf_craft/pdf/page_extractor.py:202-223](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L202-L223) —— 用 `while True` 手动迭代上游生成器：`StopIteration` 结束循环；`AbortError`/`TokenLimitError` 原样放行（它们在 OCR 驱动层有专门的处理语义，见 u3-l3）；其余异常统一包装为带页码与阶段号的 `OCRError`，供 `ignore_ocr_errors` 策略消费。

拿到每阶段结果后，按"是否脚注块 / 第几阶段"分拣布局。注意第二阶段的资源块已在 `_iter_page_layouts` 里被拦截，这里的 `not in ASSET_TAGS` 判断是第二道保险：

[pdf_craft/pdf/page_extractor.py:225-240](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L225-L240) —— `is_footnote` 为真进 `footnotes_layouts`；第一阶段的其余块进 `body_layouts`；第二阶段的非资源块也进 `footnotes_layouts`。`order` 字段按入列顺序赋值（编码时会断言它与下标一致）。

然后是逐块过滤管线本体，八步判定一气呵成：

[pdf_craft/pdf/page_extractor.py:273-285](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L273-L285) —— 类型查表（未知即丢）、脚注开关判定、文本与边界框归一化（`det` 为 `None` 的退化框直接丢弃）。

[pdf_craft/pdf/page_extractor.py:287-300](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L287-L300) —— 两档 n-gram 噪声过滤（短模式 2–5/阈值 16，长模式 6–20/阈值 8），以及"第二阶段不再收资源块"的拦截。

[pdf_craft/pdf/page_extractor.py:302-312](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L302-L312) —— 资源块调用 `asset_hub.clip` 裁剪落盘取得内容哈希，最后 `yield` 出 `(PageLayout, is_footnote)`。

文本归一化的细节：`html` 优先于纯文本（保留上游识别出的内联 HTML 结构，u6-l2 的 Markdown 解析器会消费它们）；子块文本以换行拼接——这正是"图片块的题注子块"能变成图片布局文本的机制：

[pdf_craft/pdf/page_extractor.py:314-334](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L314-L334) —— `_normalize_block_text` 先取本块的 `html`（无则 `text`），再依次拼接各子块；`_normalize_text` 剔除代理字符（[common/surrogates.py:1-3](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/surrogates.py#L1-L3)）并把连续空白压成单空格。

[pdf_craft/pdf/page_extractor.py:343-357](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L343-L357) —— `_normalize_layout_det` 把边界框四边夹取（clamp）到图像范围 \([0,\text{width}] \times [0,\text{height}]\) 内，退化框返回 `None`。

资源裁剪的落点。`AssetHub.clip` 按 `det` 裁出子图，以 PNG 内容的 sha256 命名写入 `assets/`，内容相同自然去重——这就是提取产物里 `assets/` 目录满是一长串哈希文件名的原因：

[pdf_craft/common/asset.py:16-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/asset.py#L16-L34) —— 先写临时文件再算哈希：目标已存在则删临时文件直接复用（去重），否则原子改名落盘。

最后看数据结构与落盘格式。`Page` 携带两串布局与 token 计量；`PageLayout` 的五字段就是 `page_N.xml` 里每个 `<layout>` 节点的全部信息：

[pdf_craft/pdf/types.py:13-28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L13-L28) —— `Page`（index、封面原图、`body_layouts`、`footnotes_layouts`、输入/输出 token）与 `PageLayout`（`ref`、`det`、`text`、`order`、`hash`）。`DeepSeekOCRSize` 的五档取值也定义在 [types.py:10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L10)。

[pdf_craft/pdf/types.py:70-91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L70-L91) —— `encode` 把 `Page` 序列化为 XML：根节点 `<page>` 带 `index`/`input_tokens`/`output_tokens` 属性，正文与脚注分别装进 `<body>` 与 `<footnotes>` 两个子节点；编码前断言 `order` 与列表下标一致。OCR 驱动器在 [ocr.py:204](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ocr.py#L204) 调用 `save_xml(encode(page), file_path)` 落成 `page_N.xml`；逆向的 `decode` 在 [types.py:44-67](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L44-L67)，`order` 按出现顺序重建。

依据 `encode` 的规则（[types.py:112-119](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L112-L119)），一份 `page_N.xml` 长这样（示例，按序列化规则手工构造，非仓库内真实文件）：

```xml
<page index="1" input_tokens="1820" output_tokens="946">
  <body>
    <layout ref="sub_title" det="10,20,300,80">Chapter 1 Introduction</layout>
    <layout ref="text" det="10,90,300,200">This chapter covers ...</layout>
    <layout ref="image" det="10,220,300,400" hash="ab3f...">Figure 1. Caption</layout>
  </body>
  <footnotes>
    <layout ref="text" det="10,500,300,560">1. See appendix for details.</layout>
  </footnotes>
</page>
```

测试怎么验证这条管线？`tests/test_page_extractor_structured.py` 的手法非常值得学：它不依赖任何 OCR 服务，用三个小类 `_Kind`/`_Block`/`_Structured` 伪造出"上游返回值"的鸭子类型，直接驱动私有的 `_iter_page_layouts`：

[tests/test_page_extractor_structured.py:12-35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_page_extractor_structured.py#L12-L35) —— 伪造上游块结构：`kind.value`、`det`、`text`/`html`、`children`，与适配层实际访问的字段一一对应。

[tests/test_page_extractor_structured.py:39-79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_page_extractor_structured.py#L39-L79) —— 验证"图片块的题注子块文本被并进图片布局"：一个 `image` 块带 `image_caption` 子块，最终只产出一个 `ref="image"` 的布局，`text` 为题注，且 `assets/` 里恰有一个以该布局 `hash` 为名的 PNG。

[tests/test_page_extractor_structured.py:81-107](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_page_extractor_structured.py#L81-L107) —— 验证第二阶段的资源块被拦截：`stage_index=2` 传入 `image` 块，产出空列表且 `assets/` 目录为空。

[tests/test_page_extractor_structured.py:139-193](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_page_extractor_structured.py#L139-L193) —— 验证脚注开关：`includes_footnotes=False` 时 `footnote` 块被丢弃；`True` 时产出 `ref="text"` 且 `is_footnote=True` 的布局——注意映射后脚注的 `ref` 就是普通 `text`，区分靠它落在 `<footnotes>` 段。

#### 4.3.4 代码实践

本模块的实践分两部分：先离线驱动映射管线，再审计真实提取产物。

**实践 A：离线驱动 `_iter_page_layouts`（不需要任何 OCR 服务）**

实践目标：亲眼验证映射表、未知类型丢弃与 n-gram 过滤。

操作步骤：把下面脚本存为 `fake_blocks.py` 运行（示例代码，手法照搬自 `tests/test_page_extractor_structured.py`）：

```python
# 示例代码：fake_blocks.py
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from pdf_craft.common import AssetHub
from pdf_craft.ocr_config import DeepSeekOCRLocalConfig
from pdf_craft.pdf.page_extractor import PageExtractorNode

class _Kind:
    def __init__(self, value):
        self.value = value

class _Block:
    def __init__(self, kind, det, text=None, html=None, children=None):
        self.kind = _Kind(kind)
        self.det = det
        self.text = text
        self.html = html
        self.children = children or []

class _Structured:
    def __init__(self, blocks):
        self.blocks = blocks

node = PageExtractorNode(DeepSeekOCRLocalConfig())
image = Image.new("RGB", (100, 100), "white")
structured = _Structured(blocks=[
    _Block(kind="title",  det=(10, 10, 90, 20), text="Chapter 1"),
    _Block(kind="aside",  det=(10, 25, 90, 35), text="margin note"),
    _Block(kind="isolated_formula", det=(10, 40, 90, 50), text="E=mc^2"),  # 不在映射表
    _Block(kind="text",   det=(10, 55, 90, 65), text="ab" * 30),           # 复读噪声
    _Block(kind="footnote", det=(10, 70, 90, 80), text="1. note"),
])

with TemporaryDirectory() as tmp:
    for layout, is_footnote in node._iter_page_layouts(  # pylint: disable=protected-access
        image=image, structured=structured,
        asset_hub=AssetHub(Path(tmp)), stage_index=1, includes_footnotes=True,
    ):
        print(layout.ref, layout.det, repr(layout.text[:20]), is_footnote)
```

需要观察的现象：五个块里哪些存活、`ref` 变成了什么、哪些进了脚注。

预期结果（依据 [L273-297](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L273-L297) 推导）：

```text
sub_title (10, 10, 90, 20) 'Chapter 1' False
text (10, 25, 90, 35) 'margin note' False
text (10, 70, 90, 80) '1. note' True
```

`isolated_formula` 不在映射表被丢弃；`"ab"*30`（60 个字符，2-gram `"ab"` 连续出现 30 次 ≥ 阈值 16）被噪声过滤器丢弃；`aside` 归并为 `text`；`footnote` 也归并为 `text` 但 `is_footnote=True`。把 `includes_footnotes` 改成 `False` 再跑，脚注行会消失。

**实践 B：审计真实提取产物 `ocr/page_N.xml`**

实践目标：把本讲规格里的主任务做掉——统计产物中的布局类型并按映射表反推上游 kind。

操作步骤：

1. 先按 u1-l2 的方式对任一 PDF（例如 `tests/assets/space.pdf`）跑一次提取并保留中间包（传 `package_path`，最好开启 `includes_footnotes=True`）；
2. 运行下面的审计脚本（示例代码）：`python audit_ocr_xml.py <中间包路径>/ocr`。

```python
# 示例代码：audit_ocr_xml.py —— 统计每页布局 ref 并反推上游 kind
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

REF_TO_KINDS = {  # _LAYOUT_KIND_TO_REF 的逆向（多对一）
    "sub_title": {"title"},
    "text": {"text", "footnote", "aside"},
    "image": {"image"}, "image_caption": {"image_caption"},
    "table": {"table"}, "table_caption": {"table_caption"},
    "equation": {"equation"}, "equation_caption": {"equation_caption"},
}

ocr_dir = Path(sys.argv[1])
total = Counter()
for xml_path in sorted(ocr_dir.glob("page_*.xml")):
    root = ElementTree.parse(xml_path).getroot()
    counter = Counter()
    for section in ("body", "footnotes"):
        sec = root.find(section)
        if sec is not None:
            counter.update(layout.get("ref") for layout in sec.findall("layout"))
    print(xml_path.name, dict(counter),
          "tokens:", root.get("input_tokens"), "/", root.get("output_tokens"))
    total.update(counter)

print("ALL:", dict(total))
for ref, count in sorted(total.items()):
    print(f"{ref:16s} x{count:<4d} <- 可能的上游 kind: {REF_TO_KINDS.get(ref)}")
```

需要观察的现象：每页的 `ref` 分布；`<footnotes>` 段里出现了哪些 `ref`；带 `hash` 属性的布局与 `assets/` 目录下文件名的对应关系。

预期结果：

- `<body>` 段的 `ref` 只会出现映射表的八个值之一；
- `<footnotes>` 段里**不会**出现 `image`/`table`/`equation`（两道拦截，见 [L238](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L238) 与 [L299-300](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L299-L300)）；
- 未开启 `includes_footnotes` 的产物没有 `<footnotes>` 段（`encode` 只在列表非空时写该节点，见 [types.py:83-90](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L83-L90)）；
- 资源布局的 `hash` 值能在 `../assets/` 下找到同名 `.png`。

具体数值取决于所用 PDF 与 OCR 后端，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一个 `kind="title"` 的块最终在 `page_N.xml` 里的 `ref` 是什么？为什么叫这个名字？

**参考答案**：`sub_title`（映射表 [L24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L24)）。因为对整本书而言，页内的大标题只是"章以下层级"的标题，真正的顶级结构由目录分析（u4 单元）决定，所以 pdf-craft 的内部命名把它定位成子标题。

**练习 2**：`<footnotes>` 段里为什么不可能出现 `ref="image"` 的布局？

**参考答案**：两道保险：`_iter_page_layouts` 在第二阶段直接跳过 `ref in ASSET_TAGS` 的块（[L299-300](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L299-L300)）；即使漏网，主循环分拣时第二阶段的资源块也进不了 `footnotes_layouts`（[L238-240](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L238-L240)）。而第一阶段的 `image` 块只会进 `body_layouts`。

**练习 3**：一个块的 `det=(10, 10, 5, 80)`（left 大于 right）会怎样？为什么需要这个检查？

**参考答案**：`_normalize_layout_det` 夹取边界后发现 `left >= right`，返回 `None`，该块被丢弃（[L284-285](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L284-L285)、[L355-356](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L355-L356)）。退化框无法裁出有效图像、也无法在 PDF 回写时定位（u10 会用到这些坐标），留着只会污染下游；这类异常坐标在 OCR 输出中确实偶有出现。

**练习 4**（选做）：n-gram 过滤为什么短模式阈值是 16、长模式只要 8？

**参考答案**：误杀代价不同。短 n-gram（2–5 字符）在正常文本里天然容易连续重复（如列表编号 `"1. 2. 3."`、叠词），阈值必须放宽到 16 次才敢判定异常；长 n-gram（6–20 字符）在正常文本里几乎不可能原样连续出现 8 次，一旦出现几乎必是复读噪声，所以阈值可以收紧、更积极地拦截。注释里也标明长模式是"保守策略"（[L287-297](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L287-L297)）。

## 5. 综合实践

**任务：给你的提取产物写一份「布局审计报告」。**

把本讲三个模块串起来做一遍：

1. **准备产物**（承接 u1-l2 的脚本）：对 `tests/assets/` 下任一 PDF（推荐 `table&formula.pdf` 或 `figure-caption.pdf`）调用 `PDFCraft().extract_pdf`，传 `package_path` 保留中间包，`ExtractionOptions` 里开启 `includes_footnotes=True`、`page_indexes=range(1, 6)` 只取前几页，控制成本；
2. **审计布局**：运行 4.3.4 的 `audit_ocr_xml.py`，记录每页 `ref` 分布与全书汇总，按逆向映射表标注每个 `ref` 可能来自哪些上游 kind；
3. **核对三条不变量**，写进报告：
   - `<footnotes>` 段不含 `image`/`table`/`equation`；
   - 每个带 `hash` 的布局都能在 `assets/` 找到同名 PNG（用 `Path.exists()` 批量核对）；
   - `<body>` 内布局按文档顺序排列（`decode` 后 `order` 为 0,1,2,… 连续）；
4. **核对计量**：把每页 `input_tokens`/`output_tokens` 属性求和，与 `extract_pdf` 返回的 `OCRTokensMetering`（u1-l2）对账；
5. **写结论**：用一段话说明"一张页面图像"经过 `image2page` 后变成了什么——以你这份真实产物为例，指出正文、脚注、资源三类布局各自的数量与去向。

若手头没有可用的 OCR 凭据/模型，第 2–3 步可以退化为纯源码阅读：通读 `_iter_page_layouts`（[L262-312](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/page_extractor.py#L262-L312)）后，用自己的语言写出八步过滤管线的文字版流程图。

## 6. 本讲小结

- `PageExtractorNode` 是 pdf-craft 与上游 `doc-page-extractor` 之间的适配器：一条 `isinstance` 链把六种 OCR 配置分派到上游工厂（DeepSeek 本地两族共用工厂、靠 `ocr_model` 区分；vendor 三族先逐字段复制成上游同名配置类），链尾兜底 `TypeError`。
- 所有上游 import 都在函数体内，"不用 OCR 就不加载 OCR 代码"；本地运行时缺失时 `ModuleNotFoundError` 被转译成带 `pip install 'pdf-craft[local]'` 指令的 `RuntimeError`，`download_models`/`load_models` 仅对本地配置开放。
- 布局映射把上游十种类型折叠为八种 `ref`（`title→sub_title`，`footnote`/`aside→text`），未知类型丢弃；按"是否脚注块 + 识别阶段"分进 `body_layouts`/`footnotes_layouts`，资源块三道规则（第二阶段拦截、非空才写段、阶段分拣保险）保证脚注段永不混入资源。
- 逐块过滤管线还做了四类清洗：html 优先的文本归一化（子块以换行拼接、去代理字符、压空白）、边界框夹取与退化框丢弃、两档字符级 n-gram 复读噪声过滤、资源裁剪按内容哈希去重落盘。
- 产物 `Page` 经 `types.encode` 落成 `ocr/page_N.xml`（`<page index input_tokens output_tokens>` → `<body>`/`<footnotes>` → `<layout ref det hash>`），这既是断点续跑缓存（u3-l3），也是目录分析（u4）的输入。
- 测试 `tests/test_page_extractor_structured.py` 示范了"伪造上游结构、离线驱动私有管线"的测法，不需要任何 OCR 服务即可覆盖映射与过滤规则。

## 7. 下一步学习建议

本讲补完了引擎四步主流程中"OCR 循环"的最后一块拼图。下一讲 **u3-l5（计量、错误与中断控制）** 会把本讲反复照面的三个配角讲透：`OCRTokensMetering` 如何统计、`OCRError` 被 `ignore_ocr_errors` 策略消费的完整路径、`AbortedCheck` 如何穿过 `ExtractionContext` 进入上游生成器。之后进入 **u4 单元（目录分析）**：`toc_pages.py` 消费的第一手输入正是本讲的 `ocr/page_N.xml`——建议在进入 u4 前，先完成本讲综合实践，手头留一份真实的提取产物当实验材料。
