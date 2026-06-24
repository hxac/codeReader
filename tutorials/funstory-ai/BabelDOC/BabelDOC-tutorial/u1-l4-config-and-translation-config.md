# 配置体系：CLI 参数、TOML 与 TranslationConfig

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 BabelDOC 的「三层配置」心智模型：命令行参数（CLI）、TOML 配置文件、中心配置对象 `TranslationConfig`，以及它们如何汇聚成一份翻译任务。
- 看懂 `babeldoc/main.py` 里 `create_parser()` 用 `configargparse` 定义的所有参数分组，并能写出一个合法的 TOML 配置文件。
- 理解 `TranslationConfig` 作为「装配盘」的角色：它接收零散参数、做派生计算（如 `pool_max_workers` 默认取 `qps`）、处理兼容/快捷开关（如 `enhance_compatibility`、`ocr_workaround`），最终被流水线消费。
- 认识 `WatermarkOutputMode` 枚举、页面范围（`pages`/`page_ranges`/`should_translate_page`）等常用配置项的真实实现。
- 认识 `babeldoc/const.py` 中的全局常量（`CACHE_FOLDER`、`TIKTOKEN_CACHE_FOLDER`、`WATERMARK_VERSION`）。

## 2. 前置知识

在进入本讲前，建议你已经读过：

- **u1-l2（安装与运行）**：知道 `babeldoc` 命令怎么跑、最小翻译命令需要哪些参数、入口分 `cli()` 同步壳与 `main()` 异步核心两层。
- **u1-l3（目录结构与入口）**：知道 `babeldoc/main.py` 是 CLI 入口、`babeldoc/format/pdf/high_level.py` 是翻译编排入口，并听过 `TranslationConfig`（中心配置对象）、`TRANSLATE_STAGES`（阶段全景）这些术语。

下面几个名词本讲会反复用到，先统一一下：

- **CLI（Command Line Interface）**：你在终端敲的 `babeldoc --lang-out zh ...` 这些东西，每一条 `--xxx` 就是一个「参数（argument）」。
- **TOML**：一种把配置写成文件的格式（类似 JSON / YAML，但更注重「人类可读」）。BabelDOC 允许你把一长串命令行参数写进一个 `.toml` 文件，再用 `--config file.toml` 加载，避免每次都敲一长串。
- **配置对象 / Config 对象**：把零散的参数「打包」成一个 Python 对象，程序内部传来传去就只传这一个对象。BabelDOC 的这个对象就是 `TranslationConfig`。
- **配置汇聚**：无论你用命令行还是 TOML 文件，最终都要「倒进」同一个 `TranslationConfig` 对象里，下游流水线只认这个对象。这就是本讲的核心线索。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `babeldoc/main.py` | CLI 入口。`create_parser()` 用 configargparse 定义全部命令行参数与分组；异步 `main()` 解析参数、组装 `TranslationConfig` 并交给 `async_translate`。 |
| `babeldoc/format/pdf/translation_config.py` | 定义中心配置对象 `TranslationConfig`、枚举 `WatermarkOutputMode`、跨分片共享上下文 `SharedContextCrossSplitPart`、结果对象 `TranslateResult`。 |
| `babeldoc/const.py` | 全局常量：`CACHE_FOLDER`、`TIKTOKEN_CACHE_FOLDER`、`WATERMARK_VERSION`，以及进程池、`batched` 等工具。 |
| `README.md` | 官方 TOML 配置示例（「Configuration File」一节），是本讲实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 配置三层汇聚：从命令行到流水线

#### 4.1.1 概念说明

BabelDOC 的配置体系可以抽象成三层，理解这三层的「汇聚方向」是本讲的总钥匙：

```text
   命令行参数 --lang-in en --lang-out zh ...           ┐
                                                       │  三种「入口形态」
   TOML 配置文件 config.toml （内容等价于上面的命令行）  │
                                                       ┘
                        │
                        │  configargparse 解析 + 合并（命令行优先级高于配置文件）
                        ▼
              argparse 的 args 命名空间（一堆属性 args.lang_in / args.qps ...）
                        │
                        │  main() 里手工把 args 一个个搬进构造函数
                        ▼
              TranslationConfig（中心配置对象 / 装配盘）
                        │
                        ▼
              high_level.async_translate(config)  —— 流水线只认 config
```

要点有三：

1. **入口形态有两种，但语义等价**：`--lang-in en`（命令行）和 TOML 里 `lang-in = "en"` 表达的是同一件事。命令行适合一次性使用，TOML 适合反复使用的固定配置。
2. **命令行优先级更高**：当同一个参数同时在命令行和 TOML 文件里出现时，命令行的值会覆盖文件里的值（这是 `configargparse` 的默认行为）。
3. **流水线只认 `TranslationConfig`**：`high_level.async_translate(config)` 接收的唯一主参数就是 `config`。下游所有阶段（解析、版面、段落、翻译、渲染）都从这个对象上读取自己需要的设置。所以 `TranslationConfig` 是一处「汇聚点 / 装配盘」。

#### 4.1.2 核心流程

配置从无到有的完整流程：

1. 用户在终端运行 `babeldoc ...` 或 `babeldoc -c config.toml ...`。
2. `create_parser()` 构造一个带「参数分组」和「TOML 解析能力」的解析器。
3. `main()` 调用 `parser.parse_args()`，得到合并后的 `args`。
4. `main()` 用 `args` 实例化翻译器、版面模型、术语表等「可换部件」。
5. `main()` 为每个待翻译 PDF 文件，把 `args`（以及上面装配好的部件）搬进 `TranslationConfig(...)`。
6. `async_translate(config)` 消费 `config`，开始翻译。

> 注意第 5 步：**每个 PDF 文件会各自得到一个独立的 `TranslationConfig` 实例**（见 4.3）。这点对理解「多文件批量翻译」很重要。

#### 4.1.3 源码精读

`main()` 里这段循环就是把 `args` 搬进 `config` 的核心：

[main.py:674-678](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L674-L678) —— 遍历每个待翻译文件，为其新建一个 `TranslationConfig` 实例（每个文件一份）。

随后立即把 `config` 喂给流水线：

[main.py:744-755](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L755) —— 在进度条上下文里 `async for event in async_translate(config)`，流水线只接收 `config` 这一个对象。

#### 4.1.4 代码实践

**实践目标**：用肉眼跟踪一次「配置汇聚」。

1. 打开 `babeldoc/main.py`，定位 `async def main()`（约 461 行起）。
2. 找到 `args = parser.parse_args()`，这是三种入口形态汇聚成 `args` 的瞬间。
3. 顺着往下读，找到 `TranslationConfig(...)` 的构造（约 678 行），数一数它接收了多少个 `args.xxx`。
4. 继续往下，确认 `async_translate(config)` 只传了 `config`。

**预期结果**：你会清楚地看到「散落的 args → 单个 config → 流水线」这条主线，这正是本讲要建立的心智模型。

#### 4.1.5 小练习与答案

**练习 1**：如果用户既在 TOML 文件里写了 `qps = 4`，又在命令行加了 `--qps 10`，最终生效的是哪个？

**答案**：命令行的 `--qps 10` 生效。`configargparse` 默认让命令行参数覆盖配置文件中的同名值。

**练习 2**：批量翻译 3 个 PDF 时，会创建几个 `TranslationConfig` 实例？

**答案**：3 个。`main()` 在 `for file in pending_files` 循环里为每个文件各构造一个 `config`（见 [main.py:674-678](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L674-L678)）。

---

### 4.2 configargparse 与 TOML 配置文件

#### 4.2.1 概念说明

BabelDOC 没有直接用 Python 标准库的 `argparse`，而是用了第三方库 **configargparse**。它的好处是：**同一套参数定义，既能从命令行读，也能从配置文件读**，且二者天然兼容。

configargparse 的两个关键能力在本讲中很重要：

- **`is_config_file=True`**：把某个参数标记成「配置文件路径」。解析时它会先去读这个文件，把文件里的键值当成参数填进来。
- **`TomlConfigParser`**：告诉 configargparse「配置文件是 TOML 格式，请按 TOML 解析」。传给它的 `["babeldoc"]` 表示「TOML 里 `[babeldoc]` 这一节（table）下的键，才当作有效参数」。

#### 4.2.2 核心流程

configargparse 解析 TOML 的流程：

1. 在命令行遇到 `-c config.toml`（`is_config_file=True`）。
2. 用 `TomlConfigParser(["babeldoc"])` 读 `config.toml`。
3. 取出 `[babeldoc]` 节下的所有键值，例如 `lang-in = "en-US"`、`qps = 10`。
4. 把这些键值「翻译」成对应的命令行参数（`lang-in` → `--lang-in`，`qps` → `--qps`）。
5. 与真实命令行参数合并（命令行优先），得到最终的 `args`。

**命名小坑**：TOML 里的键用的是**连字符**（`lang-in`、`openai-api-key`），和命令行长选项去掉 `--` 后完全一致。注意是连字符 `-`，不是下划线 `_`；也不需要再加 `--`。

#### 4.2.3 源码精读

解析器的构造和「配置文件开关」：

[main.py:32-41](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L32-L41) —— 创建 `configargparse.ArgParser`，指定 `TomlConfigParser(["babeldoc"])`；并注册 `-c/--config` 为配置文件入口（`is_config_file=True`）。

参数被分成若干「分组（group）」，便于 `--help` 阅读。翻译相关参数都挂在 `Translation` 组下：

[main.py:115-120](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L115-L120) —— 创建 `Translation` 参数组。

几个最常用的参数定义：

[main.py:131-142](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L131-L142) —— `--lang-in/-li`（默认 `en`）与 `--lang-out/-lo`（默认 `zh`），分别指定源语言与目标语言代码。

[main.py:148-154](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L148-L154) —— `--qps/-q`（默认 `4`）：翻译服务的每秒请求数上限（QPS 限流），后面会驱动线程池大小。

OpenAI 服务相关参数单独成组：

[main.py:394-411](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L394-L411) —— 「Translation - OpenAI Options」组：`--openai-model`（默认 `gpt-4o-mini`）、`--openai-base-url`、`--openai-api-key/-k`。

> 对照 README 的 TOML 示例（[README.md:264-307](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L264-L307)），你会发现 TOML 里的键名和这里的命令行长选项一一对应。

#### 4.2.4 代码实践

**实践目标**：把一条命令行翻译成等价的 TOML 配置。

1. 假设你的命令行是：

   ```bash
   babeldoc --openai --openai-api-key sk-xxx \
            --lang-in en --lang-out zh \
            --qps 6 --output ./out \
            --files examples/ci/test.pdf
   ```

2. 仿照 README 示例，写一个 `my.toml`（注意 `[babeldoc]` 表头、连字符键名）：

   ```toml
   [babeldoc]
   openai = true
   openai-api-key = "sk-xxx"
   lang-in = "en"
   lang-out = "zh"
   qps = 6
   output = "./out"
   ```

3. 用配置文件运行（注意 `--files` 仍可放命令行，因为它每次都不同）：

   ```bash
   babeldoc -c my.toml --files examples/ci/test.pdf
   ```

**需要观察的现象**：程序正常启动并开始下载资源/请求翻译，说明 TOML 被正确解析合并。

**预期结果**：与直接用命令行参数运行的行为一致。**待本地验证**（需要有效的 OpenAI 兼容 API key 与网络）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TOML 文件里要写 `[babeldoc]` 这个表头，而不是直接写 `lang-in = "en"`？

**答案**：因为解析器用的是 `TomlConfigParser(["babeldoc"])`，它只认 `[babeldoc]` 这一节（table）下的键。没有表头或写错表头，键会被忽略。

**练习 2**：把 `--openai-api-key` 写进 TOML 时，键名应该写成什么？

**答案**：`openai-api-key = "..."`。TOML 键名 = 命令行长选项去掉 `--`，且保持连字符。

---

### 4.3 TranslationConfig：中心配置对象（装配盘）

#### 4.3.1 概念说明

`TranslationConfig` 是 BabelDOC 的「中心配置对象」。它做三件事：

1. **收纳**：把几十个零散参数收成一个对象，下游只需 `config.qps` 这样访问。
2. **派生计算**：很多字段不是简单存储，而是在构造时算出来的。例如 `pool_max_workers` 没传时默认等于 `qps`。
3. **副作用 / 快捷开关**：某些开关会连带改写其它字段。例如打开 `ocr_workaround` 会自动关掉扫描检测；`enhance_compatibility` 会一次性打开多个兼容选项。

它是一个**普通类**（不是 `@dataclass`），构造函数 `__init__` 接收约 50 个关键字参数。源码里有一条重要约定：**新参数必须加在 `__init__` 参数列表末尾**，以保持向后兼容（位置参数调用不会错位）。

#### 4.3.2 核心流程

`TranslationConfig.__init__` 的内部流程（简化伪代码）：

```text
构造函数(config):
    1. 直接赋值：translator / lang_in / lang_out / qps / pages ...
    2. 派生计算：
       pool_max_workers        = pool_max_workers ?? qps
       term_pool_max_workers   = term_pool_max_workers ?? pool_max_workers
    3. 快捷开关联动：
       if enhance_compatibility:
           skip_clean = True; dual_translate_first = True;
           disable_rich_text_translate = True
       if ocr_workaround:
           skip_scanned_detection = True; disable_rich_text_translate = True
       if auto_enable_ocr_workaround:
           ocr_workaround = False; skip_scanned_detection = False   # 留给运行期再决定
    4. 解析页面范围：page_ranges = parse_pages(pages)
    5. 决定工作目录 working_dir（None 时按 debug 取缓存目录或临时目录）
    6. 初始化跨分片共享上下文 shared_context_cross_split_part
    7. 兼容性清理：table_model 已废弃 → 置 None 并告警
```

其中第 3 步是初学者最容易踩坑的地方：**你以为只设了一个开关，实际上它改了好几个字段**。

#### 4.3.3 源码精读

类定义与「新参数加在末尾」的兼容性约定：

[translation_config.py:153-160](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L153-L160) —— `TranslationConfig` 类开头，注释明确要求新参数追加在 `__init__` 末尾以保持向后兼容。

`pool_max_workers` 与 `term_pool_max_workers` 的派生默认值（都用 `qps` 兜底）：

[translation_config.py:244-254](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L244-L254) —— `pool_max_workers` 默认取 `qps`；`term_pool_max_workers` 默认再取 `pool_max_workers`。这就是为什么你只调 `--qps` 就能同时影响翻译并发和术语抽取并发。

`enhance_compatibility` 的连锁效果（一行开关顶三行）：

[translation_config.py:262-268](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L262-L268) —— `skip_clean`、`dual_translate_first`、`disable_rich_text_translate` 三个字段都 `or enhance_compatibility`，与 CLI 里 `--enhance-compatibility` 的 help 描述完全对应。

`ocr_workaround` 的副作用：

[translation_config.py:276-278](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L276-L278) —— 打开 OCR workaround 时，自动把 `skip_scanned_detection` 和 `disable_rich_text_translate` 都设为 `True`。

`auto_enable_ocr_workaround` 的「初始化期清零、运行期再决定」设计：

[translation_config.py:343-345](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L343-L345) —— 初始化时把 `ocr_workaround` 和 `skip_scanned_detection` 强制清零，留给扫描检测阶段（`DetectScannedFile`）在运行期按真实扫描比例再决定是否打开。这与 README 里那段「Important Interaction Note」是对应的。

工作目录 `working_dir` 的三态决策（与 `debug`、`CACHE_FOLDER` 联动）：

[translation_config.py:287-300](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L287-L300) —— 未指定时：debug 模式用 `CACHE_FOLDER/working/<文件名>`（方便排查），非 debug 用系统临时目录（用完即删）；指定了则用 `working_dir/<文件名>`。

已废弃字段的清理（`table_model`）：

[translation_config.py:326-331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L326-L331) —— 即使传了 `table_model`，也会被警告并强制置 `None`（RapidOCR 表格文本检测已退役）。

#### 4.3.4 代码实践

**实践目标**：用源码验证「快捷开关的连锁副作用」，避免被 help 文案的「一个开关」误导。

1. 打开 `translation_config.py`，找到 `__init__`。
2. 搜索 `enhance_compatibility`，列出它影响了哪几个字段（提示：`skip_clean`、`dual_translate_first`、`disable_rich_text_translate`）。
3. 再看 CLI 里 `--enhance-compatibility` 的 help（[main.py:204-208](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L204-L208)），确认 help 文案与源码行为一致。
4. 同样地，追踪 `ocr_workaround` 改写了哪些字段。

**预期结果**：你得到一张「开关 → 被联动字段」的对照表，以后配置时心里有数。

#### 4.3.5 小练习与答案

**练习 1**：用户既没有传 `pool_max_workers` 也没有传 `term_pool_max_workers`，只设了 `--qps 8`。那么翻译并发池和术语抽取并发池分别是多大？

**答案**：都是 8。`pool_max_workers` 默认取 `qps=8`；`term_pool_max_workers` 默认再取 `pool_max_workers=8`（见 [translation_config.py:244-254](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L244-L254)）。

**练习 2**：为什么 `TranslationConfig` 要求新参数加在 `__init__` 末尾？

**答案**：为了向后兼容。下游（如 PDFMathTranslate-next）可能用位置参数构造它；新参数若插在中间会错位。末尾追加且带默认值，旧的位置参数调用仍然正确。

---

### 4.4 常用配置项与 WatermarkOutputMode 枚举

#### 4.4.1 概念说明

这一节把几个「最常用、最需要看清细节」的配置项集中讲透：语言、QPS、水印（watermark）、分片（split）、页面范围（pages）。

- **语言**：`lang_in` / `lang_out` 是源/目标语言代码，会同时传给翻译器和术语表过滤。
- **QPS**：翻译服务的每秒请求上限，既用于限流，又作为并发池默认大小。
- **水印**：BabelDOC 默认会在译文 PDF 上加水印（标识由 BabelDOC 生成）。用 `WatermarkOutputMode` 枚举控制「加水印 / 不加水印 / 两种都要」。
- **分片**：超大 PDF 可用 `--max-pages-per-part` 切成多份分别翻译再合并。
- **页面范围**：`--pages` 用类似 `1,2,1-,-3,3-5` 的语法指定只翻译哪些页。

`WatermarkOutputMode` 是一个**枚举（enum）**：相比裸字符串，枚举能让「取值只能是这几种」在类型层面就被约束住，避免拼错字符串。

#### 4.4.2 核心流程

水印模式的解析（CLI 字符串 → 枚举）发生在 `main()` 里，而不是 `TranslationConfig` 内：

```text
args.watermark_output_mode（字符串 "watermarked"/"no_watermark"/"both"，默认 "watermarked"）
   │
   │  main() 里 if/elif 映射
   ▼
watermark_output_mode（WatermarkOutputMode 枚举）
   │
   ▼
config.watermark_output_mode
```

页面范围的解析则相反，发生在 `TranslationConfig` 内部：

```text
pages 字符串 "1-,-3,3-5"
   │
   │  parse_pages()
   ▼
page_ranges = [(1,-1), (1,3), (3,5)]   # (start,end)，-1 表示无上限
   │
   │  should_translate_page(n) 逐页判断
   ▼
某页是否需要翻译
```

#### 4.4.3 源码精读

`WatermarkOutputMode` 枚举定义：

[translation_config.py:21-24](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L21-L24) —— 三个取值 `Watermarked` / `NoWatermark` / `Both`，对应字符串 `"watermarked"` / `"no_watermark"` / `"both"`。

CLI 端 `--watermark-output-mode` 的 `choices` 约束（保证只能填这三个值）：

[main.py:214-220](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L214-L220) —— 用 `choices=["watermarked", "no_watermark", "both"]` 限制取值，默认 `watermarked`。

`main()` 把字符串映射成枚举（并兼容已废弃的 `--no-watermark`）：

[main.py:653-661](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L653-L661) —— 根据 `args.no_watermark`（已废弃）和 `args.watermark_output_mode` 字符串，决定最终的 `WatermarkOutputMode` 枚举值。

分片策略的构造：

[main.py:663-667](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L663-L667) —— 当指定 `--max-pages-per-part` 时，用静态方法 `TranslationConfig.create_max_pages_per_part_split_strategy(...)` 生成一个 `PageCountStrategy` 切分策略（见 [translation_config.py:154-156](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L154-L156)）。不指定则 `split_strategy = None`，不分片。

页面范围解析 `parse_pages`：

[translation_config.py:383-406](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L383-L406) —— 把 `"1,2,1-,-3,3-5"` 解析成 `[(1,1),(2,2),(1,-1),(1,3),(3,5)]`。规则：`a-b` 表示区间；缺左端点默认 1；缺右端点用 `-1` 表示「到末页」。

逐页判断 `should_translate_page`：

[translation_config.py:408-423](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L408-L423) —— 给定页码 `n`，遍历 `page_ranges` 判断是否落在某个区间内（`end == -1` 视为无上限）。空列表表示「一页都不翻」。

CLI 端 `--pages` 与 `--max-pages-per-part` 的定义：

[main.py:120-125](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L120-L125) —— `--pages/-p` 的 help 给出了语法示例 `1,2,1-,-3,3-5`。

[main.py:221-225](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L221-L225) —— `--max-pages-per-part`：每部分最大页数，不设则不分片。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `parse_pages` 与 `should_translate_page` 的行为（纯逻辑，无需 API key）。

写一段最小脚本（**示例代码**，可直接放到 Python REPL 运行）：

```python
# 示例代码：复现 TranslationConfig 的页面解析逻辑
from babeldoc.format.pdf.translation_config import TranslationConfig

# 直接调用实例方法需要先有实例；这里用 parse_pages 的同等逻辑手算：
def parse_pages(pages_str):
    ranges = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            ranges.append((int(start) if start else 1, int(end) if end else -1))
        else:
            ranges.append((int(part), int(part)))
    return ranges

print(parse_pages("1,2,1-,-3,3-5"))
# 预期：[(1, 1), (2, 2), (1, -1), (1, 3), (3, 5)]
```

**需要观察的现象**：

- `"1-"` → `(1, -1)`：从第 1 页到末页。
- `"-3"` → `(1, 3)`：从第 1 页到第 3 页（缺左端点默认 1）。
- `"3-5"` → `(3, 5)`：第 3 到第 5 页。

**预期结果**：输出与注释里写的一致。进一步可对照 `should_translate_page`：对一份 10 页文档，`"1-,-3,3-5"` 会让第 1～10 页（`1-`）、第 1～3 页、第 3～5 页都命中，等价于「全部翻译」。

#### 4.4.5 小练习与答案

**练习 1**：`--watermark-output-mode both` 最终会让 `config.watermark_output_mode` 等于哪个枚举值？会产出几份 PDF？

**答案**：等于 `WatermarkOutputMode.Both`。会同时产出「带水印」和「不带水印」两套 mono/dual PDF（具体路径见后续 `TranslateResult`）。

**练习 2**：`pages = "5"` 时，`parse_pages` 返回什么？`should_translate_page(5)` 和 `should_translate_page(6)` 分别返回什么？

**答案**：`parse_pages("5")` 返回 `[(5, 5)]`（单页写成区间）。`should_translate_page(5)` 返回 `True`，`should_translate_page(6)` 返回 `False`。

**练习 3**：为什么 BabelDOC 用枚举 `WatermarkOutputMode` 而不是直接用字符串 `"both"`？

**答案**：枚举把合法取值固定下来，拼错（如 `"Booth"`）会在编写期/映射期立刻暴露，而不是等到渲染时才出莫名错误；同时也让代码自文档化。

---

### 4.5 const.py 全局常量

#### 4.5.1 概念说明

`babeldoc/const.py` 放的是「全局级、跨模块共享」的常量与少量工具函数。本节关注和配置/运行环境最相关的几个：

- **`CACHE_FOLDER`**：BabelDOC 的统一缓存根目录，默认 `~/.cache/babeldoc`。字体、模型、翻译缓存、tiktoken 数据、debug 工作目录都落在它下面。
- **`TIKTOKEN_CACHE_FOLDER`**：tiktoken（OpenAI 的分词器）的缓存目录，会被写进环境变量 `TIKTOKEN_CACHE_DIR`，让 tiktoken 离线也能加载。
- **`WATERMARK_VERSION`**：水印里显示的版本标识，优先取 `git describe`，取不到则退回 `v<__version__>`。
- **`get_cache_file_path`**：在 `CACHE_FOLDER` 下拼出某个缓存文件路径的辅助函数。

#### 4.5.2 核心流程

`const.py` 在被 import 时就完成了「副作用初始化」：

```text
模块加载时：
  CACHE_FOLDER = ~/.cache/babeldoc
  TIKTOKEN_CACHE_FOLDER = CACHE_FOLDER / "tiktoken"
  TIKTOKEN_CACHE_FOLDER.mkdir(...)          # 确保目录存在
  os.environ["TIKTOKEN_CACHE_DIR"] = ...    # 写环境变量，影响 tiktoken
  WATERMARK_VERSION = git describe 或 v<version>
```

这意味着：**只要 `import babeldoc.const`，tiktoken 的缓存目录就已经被设置好**，无需调用方操心。

#### 4.5.3 源码精读

`CACHE_FOLDER` 与 `get_cache_file_path`：

[const.py:11-20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L11-L20) —— `CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"`；`get_cache_file_path` 支持可选子目录（会自动创建）。

tiktoken 缓存目录与环境变量：

[const.py:42-44](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L42-L44) —— 创建 `TIKTOKEN_CACHE_FOLDER` 并把路径写入 `TIKTOKEN_CACHE_DIR`，使 tiktoken 走本地缓存。

`WATERMARK_VERSION` 的取值优先级：

[const.py:23-40](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L23-L40) —— 优先用 `git describe --always`（开发/源码安装时），失败（如 pip 安装、无 git）则退回 `v{__version__}`（即 `v0.6.3`）。水印里展示的就是这个值。

`enable_process_pool` 与进程池（开发/测试用）：

[const.py:49-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L49-L55) —— `enable_process_pool()` 默认关闭，仅供开发测试；CLI 的 `--enable-process-pool`（help 标注 `DEBUG ONLY`）会调用它。

> 联系 4.3：`TranslationConfig` 在 debug 模式下会把工作目录放在 `CACHE_FOLDER/working/<文件名>`（[translation_config.py:287-289](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L287-L289)），这就是 `const.py` 与配置对象的一次直接联动。

#### 4.5.4 代码实践

**实践目标**：观察 `CACHE_FOLDER` 在本机的真实位置与内容。

1. 在终端运行：

   ```bash
   python -c "from babeldoc.const import CACHE_FOLDER, TIKTOKEN_CACHE_FOLDER, WATERMARK_VERSION; print(CACHE_FOLDER); print(TIKTOKEN_CACHE_FOLDER); print(WATERMARK_VERSION)"
   ```

2. 如果之前跑过 `--warmup` 或翻译，用 `ls ~/.cache/babeldoc` 查看里面已下载的字体、模型、tiktoken 等子目录。

**需要观察的现象**：打印出的路径就是你机器上的缓存根；`WATERMARK_VERSION` 在源码仓库里通常是一个 git 短哈希，pip 安装则是 `v0.6.3`。

**预期结果**：路径形如 `/home/<user>/.cache/babeldoc`，子目录含 `tiktoken/` 等。**待本地验证**（取决于安装方式与是否运行过翻译）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `const.py` 要在 import 时就 `mkdir` 并设置 `TIKTOKEN_CACHE_DIR` 环境变量？

**答案**：tiktoken 默认会联网下载分词表；提前把缓存目录建好并指向它，能让 tiktoken 优先用本地缓存，支持离线/内网场景，也避免重复下载。

**练习 2**：`WATERMARK_VERSION` 在「从 GitHub clone 后直接运行」和「pip 安装」两种情况下分别会是什么？

**答案**：前者（仓库里有 `.git`）取 `git describe --always` 的输出（如一个 commit 短哈希或标签）；后者（site-packages、无 `.git`）触发异常分支，退回 `v0.6.3`（见 [const.py:39-40](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L39-L40)）。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串起来——为一个固定场景编写一份 TOML 配置，并预测 `TranslationConfig` 构造后若干字段的最终值。

场景：把一份英文论文 `paper.pdf` 翻译成中文，QPS 限制为 5，不要水印，只翻译前 10 页，开启兼容增强模式，输出到 `./out`。

1. **编写 `paper.toml`**（参照 [README.md:264-307](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L264-L307)）：

   ```toml
   [babeldoc]
   openai = true
   openai-api-key = "sk-xxx"
   openai-base-url = "https://api.openai.com/v1"
   openai-model = "gpt-4o-mini"

   lang-in = "en"
   lang-out = "zh"
   qps = 5
   output = "./out"

   pages = "1-10"
   watermark-output-mode = "no_watermark"
   enhance-compatibility = true
   ```

2. **运行**：

   ```bash
   babeldoc -c paper.toml --files paper.pdf
   ```

3. **预测字段值**（在运行前先写出你的答案，再对照源码核对）：

   | 字段 | 你的预测 | 源码依据 |
   | --- | --- | --- |
   | `config.lang_out` | `zh` | 直接赋值 |
   | `config.qps` | `5` | 直接赋值 |
   | `config.pool_max_workers` | `5` | 默认取 `qps` |
   | `config.skip_clean` | `True` | `enhance_compatibility` 联动 |
   | `config.disable_rich_text_translate` | `True` | `enhance_compatibility` 联动 |
   | `config.watermark_output_mode` | `NoWatermark` | 字符串→枚举映射 |
   | `config.page_ranges` | `[(1, 10)]` | `parse_pages("1-10")` |

4. **核对**：逐项对照 4.3、4.4 的源码链接，看你的预测是否正确。

**预期结果**：你不仅写出了一份可用的 TOML，还能在脑中预先算出 `TranslationConfig` 的关键字段——这表示你已经真正掌握了「三层配置汇聚」。运行部分**待本地验证**（需 API key 与网络）；字段预测部分可完全靠读源码完成。

## 6. 本讲小结

- BabelDOC 的配置是「三层汇聚」：命令行参数 / TOML 文件 → `args` → `TranslationConfig`，流水线只认 `config`。
- `configargparse` + `TomlConfigParser(["babeldoc"])` 让同一套参数既能命令行又能 TOML 表达；TOML 键名用连字符、放在 `[babeldoc]` 节下，命令行优先级高于文件。
- `TranslationConfig` 是中心装配盘：除了收纳参数，还做派生计算（`pool_max_workers` 默认取 `qps`）和副作用联动（`enhance_compatibility`、`ocr_workaround`、`auto_enable_ocr_workaround`）。
- `WatermarkOutputMode` 枚举（`Watermarked`/`NoWatermark`/`Both`）约束水印输出；页面范围由 `parse_pages` 解析、`should_translate_page` 判定；`--max-pages-per-part` 触发分片策略。
- `const.py` 提供全局常量 `CACHE_FOLDER`、`TIKTOKEN_CACHE_FOLDER`、`WATERMARK_VERSION`，import 时即完成 tiktoken 缓存目录的副作用初始化。
- 重要约定：`TranslationConfig.__init__` 的新参数必须追加在末尾以保持向后兼容；某些字段（如 `table_model`）已废弃会被强制清理。

## 7. 下一步学习建议

- 想看 `config` 被流水线如何消费？进入 **u2-l2（翻译主流程编排：do_translate 与 _do_translate_single）**，那里会逐阶段讲 `config.xxx` 是怎么被取用的。
- 想理解 `--max-pages-per-part` 分片背后的 `split_strategy` 如何执行？看 **u8-l2（分片翻译与结果合并）**。
- 想了解水印版本 `WATERMARK_VERSION` 怎样写进 PDF 元数据、以及 `check_metadata` 如何防护重复翻译？看 **u8-l5（异常体系与健壮性处理）**。
- 对 QPS 限流与并发池（`pool_max_workers`）的实际运行机制感兴趣？进入 **u6-l1（翻译器服务与缓存）** 与 **u6-l2（IL 翻译编排）**。
