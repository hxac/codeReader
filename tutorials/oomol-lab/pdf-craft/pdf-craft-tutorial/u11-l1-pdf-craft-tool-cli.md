# 仓库本地 CLI：pdf_craft_tool

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `pdf_craft_tool` 的完整子命令树（`pdf extract/convert/translate`、`package translate/patch-pdf/render`、`epub translate`、`smoke assets/run/matrix`），并说出每个子命令对应 `PDFCraft` 门面的哪个方法。
2. 解释「环境变量装配」：`.env` 文件如何被读入、六种 OCR 后端配置如何按 `PDF_CRAFT_OCR_MODE` 构造、命名 LLM profile 如何解析（包括 OOMOL 特殊通道）。
3. 解释「运行目录」：`pdf-craft-output/manual/` 下 `label-日期-序号` 目录如何分配、`.pdf-craft-tool-run.json` 所有权文件如何防止 OCR 缓存被错误复用。

本讲是「工程化」单元的第一讲。前面所有单元都在读库代码，本讲换一个视角：**把库当作黑盒使用者的视角**——仓库维护者自己是如何调用 `pdf_craft` 公开 API 的。这个视角对二次开发特别有价值，因为 `pdf_craft_tool` 就是官方给出的「最完整的 API 使用示范」。

## 2. 前置知识

### 2.1 什么是仓库本地 CLI

`pdf_craft_tool` 是仓库内的开发、验收与手动转换工具，**不包含在发布的 `pdf-craft` Python 包中**。证据在打包配置里——pyproject.toml 只把 `pdf_craft` 一个包打进发行版：

- [pyproject.toml:54-L54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L54)：`packages = [{include = "pdf_craft"}]`，`pdf_craft_tool` 不在其中。

为什么要有这样一层？回顾 u2-l1 和 u2-l3 的结论：**库不读环境变量**，库调用者必须显式传入 `DeepSeekOCRVendorConfig(...)` 或 `LLM(...)` 这样的配置对象。这对库用户是优点（行为可预测、凭据不泄漏），但对仓库维护者自己来说是负担——每次手动验证都要写一遍样板代码。`pdf_craft_tool` 就是把这套样板代码集中到一个 argparse CLI 里：凭据从 `.env` 读，流程组合走公开 API。

### 2.2 argparse 的子命令机制

Python 标准库 `argparse` 支持用 `add_subparsers()` 把一个命令拆成多个子命令（类似 `git add`、`git commit` 的结构）。子命令还可以嵌套（`git remote add`），本 CLI 用到了两层嵌套：`pdf` → `convert`。每个子命令通过 `set_defaults(handler=函数)` 绑定一个处理函数，解析完成后直接调用 `args.handler(args)` 完成分发，不需要手写 if-else 链。

### 2.3 `.env` 文件与 `os.environ.setdefault`

`.env` 是「KEY=VALUE」格式的本地配置文件惯例，通常被 git 忽略（凭据不进版本库）。很多项目用 `python-dotenv` 加载它；本 CLI **手写了一个 18 行的解析器**（不引入额外依赖），并且用 `os.environ.setdefault` 而非 `os.environ[...] = ...` 加载——这个差异是本讲的一个重要细节：**进程已有的环境变量优先于 `.env` 文件**。

### 2.4 前置讲义回顾

本讲假设你已了解：

- u1-l4：`PDFCraft` 门面的积木式方法（`extract_pdf`、`render_markdown`、`translate_package`、`translate_pdf`、`patch_pdf_with_package`、`translate_epub`）。
- u2-l1：六种 OCR 配置类（三族模型 × local/vendor 两种运行位置）。
- u2-l3：`LLM` 配置类的字段与缓存/日志目录。
- u3-l3：`ocr/page_N.xml` 结果缓存与断点续跑——本讲的「运行目录所有权守卫」正是为保护这个缓存。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `pdf_craft_tool/cli.py` | argparse 子命令树定义 + 各子命令处理函数 | 模块一「子命令解析」的主体 |
| `pdf_craft_tool/runtime.py` | `.env` 加载、OCR 配置工厂、LLM profile 解析 | 模块二「环境变量装配」的主体 |
| `pdf_craft_tool/paths.py` | 输出目录分配：日期 + 当日序号 | 模块三「运行目录」的主体 |
| `.env.template` | 全部 `PDF_CRAFT_*` 环境变量的模板与注释 | 模块二的环境变量清单 |
| `pdf_craft_tool/README.md` | CLI 官方使用说明（中文） | 实践命令的依据 |
| `pdf_craft_tool/__main__.py` | `python -m pdf_craft_tool` 的入口 | 仅两行：导入 `main` 并 `SystemExit` |

`pdf_craft_tool/smoke/` 子包（冒烟矩阵）留给下一讲 u11-l2 专门讲，本讲只在子命令树里指出它的位置。

## 4. 核心概念与源码讲解

### 4.1 模块一：子命令解析

#### 4.1.1 概念说明

`pdf_craft_tool` 把 pdf-craft 的五大工作流（u1-l1 总结过）全部映射成命令行动词。核心设计有两个：

1. **handler 分发模式**：每个子命令用 `set_defaults(handler=函数)` 绑定处理函数，`main()` 解析完参数后统一调用 `args.handler(args)`——新增子命令不需要修改分发逻辑。
2. **参数附加器（argument adder）复用**：`_add_pdf_source`、`_add_extraction_options`、`_add_translation_options` 等小函数把一组相关参数打包，多个子命令共享同一套参数定义，保证「PDF 提取三兄弟」（extract/convert/translate）的参数行为完全一致。

#### 4.1.2 核心流程

子命令树全貌（缩进表示嵌套）：

```text
pdf_craft_tool
├── pdf                # 以 PDF 为输入的三条路径
│   ├── extract        # PDF -> DocumentPackage
│   ├── convert        # PDF -> Markdown 或 EPUB（--format 必填）
│   └── translate      # PDF -> 翻译后的 Markdown / EPUB / PDF（默认 pdf）
├── package            # 以已有 DocumentPackage 为输入
│   ├── translate      # 包 -> 翻译包
│   ├── patch-pdf      # 原 PDF + 包 -> 回写 PDF
│   └── render         # 包 -> Markdown 或 EPUB（不需要 OCR 配置）
├── epub
│   └── translate      # EPUB -> 翻译 EPUB
└── smoke              # 参数化冒烟测试（下一讲主角）
    ├── assets         # 列出 tests/assets 下发现的资产
    ├── run            # 命令行跑一条 route
    └── matrix         # 跑 JSON 矩阵配置
```

执行流程：

1. `python -m pdf_craft_tool ...` → `__main__.py` 调 `main()`。
2. `main()` 构建解析器、`parse_args()`。
3. 若没有 handler（用户没给子命令）→ 打印帮助返回 0。
4. 否则调用 `args.handler(args)`，处理函数内部完成「环境装配 → 调 PDFCraft 门面 → 打印产物路径与 token 计量」。

#### 4.1.3 源码精读

**入口与分发**。[pdf_craft_tool/cli.py:L41-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L41-L48) 中 `main()` 先解析参数，再用 `hasattr(args, "handler")` 判断是否给了子命令，最后统一调用 `args.handler(args)`；返回值是 int 就作为进程退出码（`smoke` 子命令用这个机制返回非零码）。`__main__.py` 只有两行，见 [pdf_craft_tool/__main__.py:L1-L2](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/__main__.py#L1-L2)：`from .cli import main` 加 `raise SystemExit(main())`。

**`pdf` 命令组**。[pdf_craft_tool/cli.py:L55-L76](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L55-L76) 依次注册三个子命令：`extract` 只挂 PDF 来源与提取选项；`convert` 额外要求 `--format markdown|epub`（必填）与可选 `--output`；`translate` 多一个位置参数 `target_language`，`--format` 默认 `pdf`。三者都调用 `_add_pdf_source`，它给每个命令加上 `source` 位置参数、`--work-dir` 和 `--ocr-mode`（见 [pdf_craft_tool/cli.py:L148-L152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L148-L152)）。

**提取参数附加器**。[pdf_craft_tool/cli.py:L158-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L158-L170) 把 u2-l2 讲过的 `ExtractionOptions` 字段逐一暴露成命令行开关：`--pages`（逗号分隔、1 起始页码）、`--ocr-size`（五档，缺省 `gundam`）、`--dpi`、token 双限额、`--cover`、`--footnotes`、`--toc-assumed`、`--toc-llm PROFILE`。注意 `--pages` 的字符串要经 `_page_indexes`（[pdf_craft_tool/cli.py:L540-L549](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L540-L549)）解析成 tuple，负数或非整数直接 `SystemExit`。

**`pdf convert` 的处理函数**。[pdf_craft_tool/cli.py:L214-L222](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L214-L222) 是本讲实践的主角：先分配工作目录、在其中做提取（包固定落在 `work_dir/package`），输出文件缺省为 `work_dir/book.md` 或 `book.epub`，渲染完打印包路径、输出路径与 OCR token 计量。渲染细节在 [pdf_craft_tool/cli.py:L463-L467](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L463-L467)：Markdown 走 `craft.render_markdown(package, output, Path("assets"))`——图片复制到输出文件旁的 `assets/` 子目录（相对路径语义，对应 u6-l3 讲过的资源路径分离计算）；EPUB 走 `craft.render_epub(package, output)`。

**`package` 与 `epub` 命令组**。[pdf_craft_tool/cli.py:L78-L114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L78-L114) 注册对已有包/EPUB 的操作。两个值得注意的防御点：`_translate_pdf`（[pdf_craft_tool/cli.py:L226-L227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L226-L227)）在 `--format pdf` 且 `--submit` 不是 `replace` 时立即报错（承接 u10-l1：PDF 回写不支持 APPEND_BLOCK）；`_translate_epub`（[pdf_craft_tool/cli.py:L283-L284](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L283-L284)）在输出文件已存在时拒绝覆盖。

**翻译转换器的装配**。[pdf_craft_tool/cli.py:L449-L460](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L449-L460) 把 `--translation-llm` / `--fill-llm` 两个 profile 解析成 `LLM` 对象（缓存与日志目录落在工作目录下），构造 `XMLTranslator`，再包成 `ChapterXMLTransformer`——这正是 u7-l1 讲过的「XMLTaskTranslator 协议桥接」在真实代码中的使用现场。两个 profile 名相同（默认都是 `translation`）时复用同一个 `LLM` 实例。

#### 4.1.4 代码实践

**实践：用 `--help` 遍历整棵子命令树（不需要任何凭据）。**

1. 实践目标：不看源码也能说出每个子命令的参数，验证 4.1.2 的树状图。
2. 操作步骤：在仓库根目录依次执行：

   ```bash
   python -m pdf_craft_tool --help
   python -m pdf_craft_tool pdf --help
   python -m pdf_craft_tool pdf convert --help
   python -m pdf_craft_tool package --help
   python -m pdf_craft_tool package render --help
   python -m pdf_craft_tool smoke --help
   ```

   （仓库用 poetry 管理依赖，README 示例一律用 `poetry run python -m pdf_craft_tool ...`；若你直接用系统 Python 且已 `pip install -e .` 加 dev 依赖，`python -m` 形式同样可用。）
3. 需要观察的现象：`--help` 输出中的 `options` 列表与源码 `_add_*` 附加器一一对应；`pdf convert` 的 `--format` 标注 `(required)`；顶层无子命令时 `main()` 走 `parser.print_help()` 分支。
4. 预期结果：六条命令都能打印帮助并退出码为 0。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 里要写 `if not hasattr(args, "handler")` 而不是直接调用 `args.handler(args)`？

**答案**：顶层子解析器 `dest="command"` 没有设 `required=True`，用户可能只输入 `python -m pdf_craft_tool` 不带任何子命令，此时 `args` 上没有 `handler` 属性，直接调用会抛 `AttributeError`。这个分支把「无命令」优雅地降级为打印帮助、退出码 0。

**练习 2**：`pdf extract`、`pdf convert`、`pdf translate` 三个子命令的提取参数为什么长得几乎一样？

**答案**：三者都调用了同一个参数附加器 `_add_extraction_options`（并且 `_add_pdf_source` 提供来源与工作目录参数）。附加器模式保证「凡是做 PDF 提取的命令」共享同一套 `ExtractionOptions` 开关，新增提取选项时只改一处。

**练习 3**：`--submit append-block` 配 `--format pdf` 会发生什么？这条检查放在哪一层？

**答案**：`_translate_pdf` 开头抛 `SystemExit("PDF output supports only --submit replace")`。检查放在 CLI 层（[pdf_craft_tool/cli.py:L226-L227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L226-L227)），在任何文件操作之前快败；库层门面同样会预检拒绝（u10-l1 讲过），CLI 层检查是为了给出更贴合命令行语境的错误信息。

### 4.2 模块二：环境变量装配

#### 4.2.1 概念说明

库不读环境变量（u2-l1 的结论），但 CLI 需要一个便捷的凭据入口。`runtime.py` 的职责就是把 `PDF_CRAFT_*` 环境变量**翻译成库的显式配置对象**，让库保持纯净。模块开头的 docstring 明说了这层隔离：库调用者显式传配置对象、从不加载仓库 `.env`（[pdf_craft_tool/runtime.py:L1-L5](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L1-L5)）。

三个装配函数构成主干：

| 函数 | 输入 | 输出 |
| --- | --- | --- |
| `create_ocr_config_from_env(mode)` | OCR 模式（缺省读 `PDF_CRAFT_OCR_MODE`） | 六种 `OCRConfig` 之一 |
| `create_llm_from_env(profile, ...)` | profile 名 | `LLM` 配置对象 |
| `load_project_env(project_root)` | 仓库根 | 加载 `.env` 进环境（缺失即退出） |

#### 4.2.2 核心流程

一次 PDF 提取命令的装配流程（对应 `_extract` 处理函数）：

```text
调用 _extract(args, package_path)
  ├─ load_project_env(仓库根)        # 读 <仓库根>/.env，setdefault 进环境
  ├─ ocr_mode = args.ocr_mode or ocr_mode_from_env()
  │    # --ocr-mode 命令行参数 > .env 的 PDF_CRAFT_OCR_MODE
  ├─ _resolve_ocr_size / _validate_ocr_size
  ├─ _record_pdf_cache_owner(...)     # 模块三讲
  ├─ craft = PDFCraft(pdf=PDFOptions(
  │      ocr=create_ocr_config_from_env(ocr_mode)))   # 环境变量 → 配置对象
  └─ craft.extract_pdf_with_metering(source, package_path,
         ExtractionOptions(..., on_ocr_event=_print_ocr_event))
```

LLM profile 解析规则（`llm_values_from_env` 的递归设计）：

```text
profile "premium"
  ├─ 读 PDF_CRAFT_LLM_PREMIUM_PROFILE → 若指向别的名字则递归解析那个 profile
  ├─ PDF_CRAFT_LLM_PREMIUM_PROVIDER == "oomol"?
  │    ├─ 是 → subprocess 调 `oo llm config --json` 取临时凭据（不落盘）
  │    └─ 否 → 读 PDF_CRAFT_LLM_PREMIUM_{API_KEY,BASE_URL,MODEL}
  └─ 补充 token_encoding / timeout / temperature / top_p / retry_times
     以及 CLI 注入的 cache_path 与 log_dir_path
```

这套机制支撑了 `.env.template` 里的「用途默认」设计：`PDF_CRAFT_LLM_TOC_PROFILE`、`PDF_CRAFT_LLM_TRANSLATION_PROFILE`、`PDF_CRAFT_LLM_FILL_PROFILE` 默认都指向 `default` profile，需要时可以各自改指不同 profile。

#### 4.2.3 源码精读

**`.env` 加载**。[pdf_craft_tool/runtime.py:L27-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L27-L45)：`load_project_env` 只认仓库根下的 `.env`，文件不存在直接 `SystemExit` 并提示「复制 .env.template」；`load_env` 逐行手写解析——跳过空行与 `#` 注释、按第一个 `=` 切分、去掉引号，最后 `os.environ.setdefault`。**注意 `setdefault` 的语义：进程环境里已存在的变量不会被 `.env` 覆盖**，所以 `PDF_CRAFT_OCR_MODE=deepseek-ocr-vendor python -m pdf_craft_tool ...` 可以临时覆盖 `.env` 的选择。

**OCR 配置工厂**。[pdf_craft_tool/runtime.py:L48-L127](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L48-L127) 的 `create_ocr_config_from_env` 是一条 if 链，把六个模式字符串逐一映射到 u2-l1 讲过的六个 frozen dataclass。以 vendor 分支为例（[pdf_craft_tool/runtime.py:L99-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L99-L108)）：`base_url` / `api_key` / `model` 用 `_required` 读取，缺一个就 `SystemExit("Missing required environment variable: ...")`（[pdf_craft_tool/runtime.py:L202-L206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L202-L206)）；温度等可选项缺失则为 `None`，交给 dataclass 默认值。链尾兜底 `SystemExit(f"Unsupported PDF_CRAFT_OCR_MODE: {mode}")`。local 分支还保留了旧变量名别名（`_backend_str` 先读新名、再读 legacy 名，见 [pdf_craft_tool/runtime.py:L197-L199](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L197-L199)），兼容六后端重构前的 `.env`。

**模式读取与无副作用探测**。[pdf_craft_tool/runtime.py:L130-L137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L130-L137)：`ocr_mode_from_env` 只读字符串不初始化后端；`ocr_values_from_env` 调 `asdict` 把配置摊平成 dict——冒烟测试用它在报告中记录「用了哪些值」同时避免打印密钥对象。

**LLM profile 与 OOMOL 通道**。[pdf_craft_tool/runtime.py:L147-L170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L147-L170) 是 profile 解析主体：先把 profile 名大写化拼出前缀 `PDF_CRAFT_LLM_<NAME>`，若 `..._PROFILE` 指向别名则递归；provider 为 `oomol` 时走 `_oomol_llm_values`（[pdf_craft_tool/runtime.py:L173-L189](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L173-L189)）——`subprocess.run(["oo", "llm", "config", "--json"])` 取短期凭据，`apiKey`/`baseUrl`/`model` 任一缺失就提示 `oo auth login`。这解释了 `.env.template` 注释「"oomol" 的 API key 永远不要写在这里」（[.env.template:L49-L51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.env.template#L49-L51)）。缓存与日志目录由调用方传入（CLI 把它们指到工作目录下），构造出的值经 `create_llm_from_env`（[pdf_craft_tool/runtime.py:L140-L144](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/runtime.py#L140-L144)）解包成 `LLM(**values)`。

**`.env.template` 的组织方式**。[.env.template:L1-L6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.env.template#L1-L6) 开头就是 `PDF_CRAFT_OCR_MODE`（六选一的默认开关），随后按六个 backend 分组列出全部变量（L8-L47），最后是 LLM profile 区（L49-L69）。它是一份「一次填全、随时切换」的模板：`PDF_CRAFT_OCR_MODE` 只是缺省选择，`--ocr-mode` 命令行参数或环境变量都能覆盖它，且**切换不会改写 `.env`**。

#### 4.2.4 代码实践

**实践：用假 `.env` 验证装配行为（不需要真实凭据，不修改源码）。**

1. 实践目标：亲眼确认 `load_env` 的 `setdefault` 语义与 `_required` 的快败行为。
2. 操作步骤：在仓库根目录外（例如 `/tmp`）写一个独立脚本 `probe_env.py`（示例代码，不是项目原有文件）：

   ```python
   # 示例代码：验证 pdf_craft_tool.runtime 的环境装配行为
   import os
   from pathlib import Path
   from pdf_craft_tool.runtime import load_env, create_ocr_config_from_env

   # 1) setdefault 语义：进程环境优先
   os.environ["PDF_CRAFT_OCR_MODE"] = "deepseek-ocr2-vendor"
   load_env(Path("fake.env"))  # fake.env 里写 PDF_CRAFT_OCR_MODE=unlimited-ocr-vendor
   assert os.environ["PDF_CRAFT_OCR_MODE"] == "deepseek-ocr2-vendor"

   # 2) 必填变量缺失时快败
   try:
       create_ocr_config_from_env("deepseek-ocr-vendor")  # 没配 BASE_URL/API_KEY
   except SystemExit as e:
       print("SystemExit:", e)  # 预期打印 Missing required environment variable
   ```

   同目录放一个 `fake.env`，内容一行：`PDF_CRAFT_OCR_MODE=unlimited-ocr-vendor`。运行 `python probe_env.py`。
3. 需要观察的现象：第一个断言通过（`.env` 没有覆盖进程环境）；第二步抛出 `SystemExit` 且消息包含缺失变量名。
4. 预期结果：两行为都与 4.2.3 的源码分析一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_env` 用 `os.environ.setdefault` 而不是直接赋值？

**答案**：`setdefault` 让「进程已有环境变量」优先于 `.env` 文件。这样 `PDF_CRAFT_OCR_MODE=deepseek-ocr-vendor python -m pdf_craft_tool ...` 可以做单次覆盖，也让 CI 之类已在环境里注入凭据的场景不被 `.env` 抹掉。

**练习 2**：`.env` 里 `PDF_CRAFT_LLM_PREMIUM_PROFILE=default` 是什么意思？

**答案**：这是 profile 别名机制：解析 `premium` 时先读 `PDF_CRAFT_LLM_PREMIUM_PROFILE`，发现它指向 `default`，于是递归按 `default` profile 解析（读 `PDF_CRAFT_LLM_DEFAULT_*` 系列变量）。效果是「premium 这个名字暂时复用 default 的连接参数」。

**练习 3**：`_oomol_llm_values` 为什么用 subprocess 调外部命令而不是读环境变量？

**答案**：OOMOL 的连接是短期凭据，由本机 `oo` CLI 的登录态管理；直接从 `oo llm config --json` 现取，避免把会过期的密钥写进 `.env` 落盘。取不到或字段不全时统一 `SystemExit` 提示 `oo auth login`，凭据既不持久化也不打印。

### 4.3 模块三：运行目录

#### 4.3.1 概念说明

CLI 的每次运行都会产生一堆中间产物：`package/`（DocumentPackage）、翻译缓存、翻译日志、渲染输出。如果全都堆在一个目录里互相覆盖，多次实验就没法对比。`paths.py` 解决「产物放哪」：默认根 `pdf-craft-output/`（git 忽略）下，按「标签-日期-当日序号」创建永不冲突的运行目录。

但这里有一个和 u3-l3 直接相关的矛盾：**运行目录如果每次都新建，`ocr/page_N.xml` 断点续跑缓存就永远无法复用**。CLI 的解法是双轨制：

- 不传 `--work-dir`：每次新目录，适合一次性转换；
- 传 `--work-dir`：目录已存在则复用，缓存生效；但配一个「所有权文件」防止把 A 书的缓存误用到 B 书上。

#### 4.3.2 核心流程

目录分配算法（`create_run_directory`）：

```text
输入: root（如 pdf-craft-output/manual）, label（如 citation-convert）
day   = 当天日期 YYYYMMDD
seq   = 扫描 root 下已有 "*-day-数字" 目录的最大序号 + 1
loop: 尝试 mkdir(root/label-day-seq 三位序号)
       已存在（并发竞争）→ seq += 1 重试
返回新建的目录路径
```

工作目录决策（`_work_dir`，被所有处理函数调用）：

```text
传了 --work-dir ──→ 校验不是文件 → mkdir(parents, exist_ok) → 返回（可复用）
没传           ──→ create_run_directory(pdf-craft-output/manual,
                                  f"{源文件词干}-{操作名}")
                   # 如 citation-convert-20260826-001
```

所有权守卫（`_record_pdf_cache_owner`，仅 PDF 提取类命令触发）：

```text
构造 current = {schema, source 绝对路径, 文件大小, mptime_ns,
                ocr_mode, ocr_size, dpi, 各 token/尺寸限额, footnotes}
.json 存在 ──→ 逐字段比对（schema 除外）
              │ 全一致 → 通过（OCR 缓存可复用）
              │ 有差异 → SystemExit，列出 mismatched 字段
legacy：目录里有 package/ocr 但没有 .json ──→ SystemExit（拒绝来路不明的缓存）
否则写入 .pdf-craft-tool-run.json
```

#### 4.3.3 源码精读

**默认输出根**。[pdf_craft_tool/paths.py:L8-L8](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/paths.py#L8)：`DEFAULT_OUTPUT_ROOT = Path("pdf-craft-output")`——相对路径，意味着输出根落在**当前工作目录**下；冒烟输出则固定在其 `smoke/` 子目录（[pdf_craft_tool/cli.py:L133-L133](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L133)）。

**日期序号目录**。[pdf_craft_tool/paths.py:L11-L23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/paths.py#L11-L23)：`create_run_directory` 用 `%Y%m%d` 取当天日期，`_next_sequence`（[pdf_craft_tool/paths.py:L26-L33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/paths.py#L26-L33)）用正则 `.*-day-(\d+)$` 扫描已有目录取最大序号；`while True` 循环里 `mkdir()` 失败（`FileExistsError`）就序号加一重试——用文件系统的原子性天然解决了并发竞争，不需要锁。

**`_work_dir` 的双轨决策**。[pdf_craft_tool/cli.py:L470-L477](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L470-L477)：显式 `--work-dir` 先拒绝「路径存在但不是目录」，然后 `exist_ok=True` 复用；缺省时把 `f"{source.stem}-{operation}"` 作为标签交给 `create_run_directory`——`pdf convert tests/assets/citation.pdf` 会得到类似 `citation-convert-20260826-001` 的目录。

**所有权守卫**。[pdf_craft_tool/cli.py:L480-L521](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L480-L521) 是本模块最值得精读的函数。docstring 直说目的：「Guard manual PDF work-dir reuse so OCR caches stay tied to one source/backend」。它把「影响 OCR 结果的输入」序列化成 JSON 指纹（源文件绝对路径 + 大小 + mtime_ns + OCR 模式/尺寸 + dpi + 各限额 + 脚注开关），写入工作目录的 `.pdf-craft-tool-run.json`；再次复用该目录时逐字段比对，任何不匹配都 `SystemExit` 并列出差异字段。为什么 mtime 也要记？因为同一个路径的文件可能被替换过，大小相同不代表内容相同。这个守卫和 u3-l3 的 `done` 哨兵互补：库层缓存假设「目录属于这一本书」，CLI 层负责验证这个假设成立。

**提取结果落位**。[pdf_craft_tool/cli.py:L207-L211](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L207-L211)：`pdf extract` 把包固定放在 `work_dir/package`，然后打印包路径与 token 计量（`_print_metering`，[pdf_craft_tool/cli.py:L567-L568](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L567-L568)，输出 input/output token 数）——注意 CLI 用的是 `extract_pdf_with_metering` 而非 `extract_pdf`，因为 u3-l5 讲过：只有前者返回计量。

**OCR 事件实时打印**。[pdf_craft_tool/cli.py:L563-L564](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L563-L564)：`_print_ocr_event` 把 u3-l3 的六种 `OCREvent` 打成一行进度（如 `OCR start: page 1/20`），经由 `ExtractionOptions(on_ocr_event=_print_ocr_event)` 注入（[pdf_craft_tool/cli.py:L443-L444](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L443-L444)）——这是「只读观测钩子」的标准用法示范。

#### 4.3.4 代码实践

**实践：观察运行目录的分配与序号递增（不需要凭据，纯目录操作）。**

1. 实践目标：验证日期序号算法与「同日递增、绝不覆盖」的行为。
2. 操作步骤：写独立脚本（示例代码）：

   ```python
   # 示例代码：观察 create_run_directory 的分配行为
   from pathlib import Path
   from pdf_craft_tool.paths import create_run_directory

   root = Path("/tmp/observe-runs")
   a = create_run_directory(root, "demo-convert")
   b = create_run_directory(root, "demo-convert")
   print(a.name)  # 预期 demo-convert-<今天日期>-001
   print(b.name)  # 预期 demo-convert-<今天日期>-002
   ```

   运行两次脚本，观察四次输出的序号；再手动 `rmdir` 一个中间目录后重跑，看序号是否按「现存最大 + 1」跳号。
3. 需要观察的现象：同名标签在同一天内序号严格递增；删掉 `...-002` 后再跑会得到 `...-004`（因为 003 仍是现存最大），即序号不回填。
4. 预期结果：与 `_next_sequence` 的 `max(sequences, default=0) + 1` 逻辑一致。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `create_run_directory` 用「先 `mkdir`、失败则序号加一」而不是「先算好序号再创建」？

**答案**：两个进程可能同时算出同一个序号（检查-创建之间存在竞争窗口）。`mkdir()` 在文件系统层面是原子的：只有一个进程能成功，另一个收到 `FileExistsError` 后加一重试即可。这比加锁简单且跨进程安全。

**练习 2**：用户用同一个 `--work-dir` 先转换了 `citation.pdf`，再拿来转换 `newton.pdf`，会发生什么？

**答案**：`_record_pdf_cache_owner` 发现 `.pdf-craft-tool-run.json` 已存在，逐字段比对时 `source`（还有 size/mtime_ns）不匹配，抛 `SystemExit` 并列出 mismatched 字段。这防止了 B 书复用 A 书的 `ocr/page_N.xml` 缓存——那种错误不会报错，只会产出「用错内容的包」。

**练习 3**：`package render` 为什么不需要 `.env`，而 `pdf convert` 需要？

**答案**：`package render` 的处理函数（[pdf_craft_tool/cli.py:L270-L276](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L270-L276)）只用 `DocumentPackage.from_path(...).validate()` 加 `PDFCraft()` 裸门面（u1-l4 讲过裸构造合法、无 OCR 负担）；而 `pdf convert` 走 `_extract`，第一步就是 `load_project_env`，缺 `.env` 直接退出。渲染是纯派生操作，提取才需要 OCR 凭据。

## 5. 综合实践

**综合实践：走完「转换 → 检查运行目录 → 复用包再渲染」的完整闭环。**（本综合实践即本讲规格指定的实践任务。）

1. 实践目标：把三个模块串起来——用环境变量装配跑通转换，用运行目录管理产物，再用 `package render` 体验「包是中立契约」的复用价值。

2. 操作步骤：

   a. 准备凭据。复制模板并配置一个 vendor 后端（标准安装即可，无需 GPU）：

   ```bash
   cp .env.template .env
   # 编辑 .env：
   #   PDF_CRAFT_OCR_MODE=deepseek-ocr-vendor
   #   填 PDF_CRAFT_DEEPSEEK_OCR_BASE_URL / _API_KEY / _MODEL
   ```

   b. 转成 Markdown 与 EPUB 各一次（`tests/assets` 下任选一个小 PDF，`citation.pdf` 最小；先用 `--pages 1` 控制成本）：

   ```bash
   python -m pdf_craft_tool pdf convert tests/assets/citation.pdf \
     --format markdown --pages 1
   python -m pdf_craft_tool pdf convert tests/assets/citation.pdf \
     --format epub --pages 1
   ```

   c. 检查默认输出根目录：

   ```bash
   ls pdf-craft-output/manual/
   # 预期两个目录：citation-convert-<日期>-001 与 ...-002
   ls <第一次运行的目录>/
   ```

   d. 复用已提取的包重复渲染（不需要 `.env`，也不产生新的 OCR 调用）：

   ```bash
   python -m pdf_craft_tool package render \
     pdf-craft-output/manual/citation-convert-<日期>-001/package \
     --format markdown
   ```

3. 需要观察的现象：

   - 步骤 b 终端逐页打印 `OCR start/complete: page 1/1` 事件，结尾打印 `Package:`、`Output:` 与 `OCR tokens: input=..., output=...` 计量行。
   - 步骤 c 中每个运行目录应包含：`.pdf-craft-tool-run.json`（所有权指纹）、`package/`（内含 `chapters/`、`assets/`、`ocr/page_1.xml`、`document.json`，若目录分析有产出则还有 `toc.xml`）、`book.md`（或 `book.epub`）。Markdown 运行目录还会多一个与 `book.md` 同级的 `assets/` 目录——`_render` 传入相对路径 `Path("assets")`，渲染器将其解析为输出文件旁的子目录（见 `render_markdown_file` 对 `output_assets_path` 的处理：相对路径拼接 `output_path.parent`）；EPUB 运行目录没有这一层，图片直接嵌入包内。
   - 步骤 d 生成的 Markdown 与步骤 b 的第一次输出内容一致（同一个包派生），且全程没有任何 OCR 事件打印。

4. 预期结果：两次 convert 的运行目录序号递增、互不覆盖；`package render` 零 OCR 成本产出等价 Markdown。全程待本地验证（转换步骤依赖真实 OCR 服务凭据）。

5. 延伸思考：把步骤 b 的第二条命令加上 `--work-dir` 指向第一次的运行目录再跑一次——先预测会发生什么（所有权守卫的 `ocr_size`/`source` 等字段一致时应通过并命中 `ocr/` 缓存，第二次应当几乎零 token），然后验证。若把 `--ocr-mode` 换成另一个后端再试，应当被 `SystemExit` 拦下并列出 mismatched 字段。

## 6. 本讲小结

- `pdf_craft_tool` 是不随包发布的仓库本地 CLI（pyproject 只打包 `pdf_craft`），本质是官方维护的「公开 API 最完整使用示范」：凭据走 `.env`，流程全部经 `PDFCraft` 门面组合。
- 子命令树按输入类型分组：`pdf`（extract/convert/translate）、`package`（translate/patch-pdf/render）、`epub`（translate）、`smoke`（assets/run/matrix）；分发靠 `set_defaults(handler=...)`，参数靠 `_add_*` 附加器在兄弟命令间复用。
- 环境变量装配的边界画得很清：`runtime.py` 手写 `.env` 解析（`setdefault`，进程环境优先），`create_ocr_config_from_env` 用 if 链把六个模式映射到六个配置类，必填凭据缺失即 `SystemExit` 快败；LLM 走命名 profile + 别名递归，OOMOL 凭据经 `oo llm config --json` 现取不落盘。
- 运行目录双轨制：缺省在 `pdf-craft-output/manual/` 下按「标签-日期-序号」原子分配新目录；`--work-dir` 显式复用时靠 `.pdf-craft-tool-run.json` 所有权指纹（源文件路径/大小/mtime + OCR 设置）防止 OCR 缓存被错误复用。
- CLI 层还做了几件贴心的防御：`--format pdf` 只允许 `--submit replace`、EPUB 翻译拒绝覆盖已有输出、`deepseek-ocr2-local` 拒绝 `tiny` 尺寸、`--pages` 校验 1 起始正整数。
- 提取走 `extract_pdf_with_metering` 保留 token 计量，并注入 `on_ocr_event` 实时打印页级进度——这两个钩子正是 u3-l3/u3-l5 理论的实际消费者。

## 7. 下一步学习建议

本讲只打开了 `smoke` 子命令的大门。下一讲 **u11-l2 测试体系：单元测试与冒烟矩阵** 将深入 `pdf_craft_tool/smoke/` 子包（`runner.py`、`checks.py`、`assets.py`）与 `tests/smoke/*.json` 矩阵，讲清「一条 route 如何参数化地跑通并生成 `manifest.json` / `checks.json` 报告」以及退出码如何与报告状态联动，还会覆盖 `tests/test_module_boundaries.py` 这类架构守卫测试。如果你现在就想扩展视野，推荐先读 [pdf_craft_tool/README.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md) 的冒烟矩阵一节（本文引用的命令示例均出自那里），再回看 [pdf_craft_tool/cli.py:L306-L350](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L306-L350) 的 `_run_smoke`，体会本讲的「环境装配 + 运行目录」如何被冒烟流程复用。
