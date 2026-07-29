# 编译配置与单次编译

## 1. 本讲目标

本讲聚焦 `crates/typst-cli/src/compile.rs` 中「把命令行参数变成一次真正编译」的那段逻辑。学完后你应该能够：

1. 说清楚 `CompileConfig` 这个结构体里每个字段的来源与作用，以及为什么 CLI 要先做一层「预处理」。
2. 跟着 `CompileConfig::new_impl` 画出：输出格式是如何被推断的、输出路径是如何被推导的、`--pages` 与 `--no-pdf-tags` 之间有哪些隐含规则与校验。
3. 描述 `compile_once` 一次「编译 → 导出 → 合并静态警告 → 打印诊断 → 写依赖」的完整主循环，以及它在 watch 模式下如何与 `Status` 状态显示配合。
4. 解释 `PdfStandards` 是如何从 CLI 的枚举构造出来的、`From<PdfStandard>` 这个转换在做什么，并理解 `#[typst_macros::time]` 与 `Timer::record` 的计时机制。

> 本讲只讲「单次编译」这一条主干。多格式导出的图片模板、并行渲染、`ExportCache`、HTML/Bundle 写盘等细节属于下一讲 **u2-l3 多格式导出**，本讲只在用到时简要提及，不展开。

## 2. 前置知识

阅读本讲前，请确认你已经掌握下面这些（它们都在前置讲义里建立过）：

- **`CompileArgs` / `CompileCommand`（u1-l3）**：`typst compile` 的全部命令行选项都被 clap 派生宏定义成强类型字段，例如 `format: Option<OutputFormat>`、`pages: Option<Vec<Pages>>`、`pdf_standard: Vec<PdfStandard>`、`no_pdf_tags: bool`、`ppi: f64`。本讲要做的就是把「这一堆松散字段」整理成「一次编译真正需要的样子」。
- **`SystemWorld`（u2-l1）**：编译器核心只认纯逻辑的 `World` trait，`SystemWorld` 是它的操作系统版实现。本讲里凡是「编译」二字，实际调用都是把一个 `&mut SystemWorld` 交给 `typst::compile`。
- **`HintedStrResult` / `SourceResult` / `Warned`（u1-l2）**：CLI 层的错误用 `HintedStrResult`（一条主消息 + 若干提示行），编译器层用 `SourceResult<T>`（带源码 `Span` 的诊断列表），而 `Warned<T>` 把「正常产出 + 旁路收集的警告」打包在一起。本讲会反复用到这三者。
- **软失败与 `set_failed()`（u1-l2）**：有些子命令返回 `Ok(())` 但要把退出码改成非 0，靠的是 `thread_local` 的 `EXIT`。`compile_once` 在编译失败分支里就会调用它。
- **clap 的 `#[value(name = ...)]`（u1-l3）**：`PdfStandard` 枚举把命令行名（如 `a-1a`）和 Rust 变体名（`A_1a`）解耦，本讲末尾的 `From<PdfStandard>` 转换就是在做这两套名字之间的映射。

如果你对 `typst::compile::<T>(world)` 这个泛型入口还陌生，只需记住一点：同一个 `SystemWorld`，按目标类型 `T`（`PagedDocument` / `HtmlDocument` / `Bundle`）的不同，会跑出不同形态的产物。本讲主要面向 `PagedDocument`。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `crates/typst-cli/src/compile.rs` | **本讲主角**。包含 `compile` 入口、`CompileConfig` 结构体及其预处理 `new_impl`、`compile_once` 主循环、`compile_and_export` 分发、`pdf_options` 选项构造、`From<PdfStandard>` 转换。 |
| `crates/typst-cli/src/args.rs` | 提供 `CompileCommand`、`CompileArgs`、`OutputFormat`、`PdfStandard`、`Pages`、`DepsFormat`、`DiagnosticFormat` 等原始参数类型。本讲把它们当作输入。 |
| `crates/typst-cli/src/main.rs` | `dispatch` 把 `Command::Compile` 分发给 `compile::compile`；`set_failed` 在编译失败时被复用。 |

> 下游文件 `crates/typst-cli/src/watch.rs`、`src/deps.rs` 会在 `compile_once` 末尾被调用，本讲只点到为止，细节留给 u2-l5 和 u3-l4。

## 4. 核心概念与源码讲解

### 4.1 CompileConfig：预处理后的「编译蓝图」

#### 4.1.1 概念说明

回忆 u1-l3：clap 给我们的是 `CompileArgs`——一堆「用户想表达什么」的原始字段，里面很多是 `Option`、很多带默认值、很多彼此存在隐含约束（比如「用 `--pages` 导出部分页就等于关闭 PDF 标签」）。

如果让编译主循环直接读 `CompileArgs`，会出现两个问题：

1. **重复劳动**：每次重编译（尤其 watch 模式）都要重新推断「输出格式到底是 PDF 还是 PNG」，而这只取决于命令行，编译期间不会变。
2. **关注点混乱**：像「输出路径推导」「stdout 冲突校验」「PDF 标准合法性」这些是一次性、与「编译」本身无关的逻辑，混在主循环里会很难读。

于是 CLI 设计了一层中间结构 `CompileConfig`：它在编译**之前**被构造一次，把 `CompileArgs` 翻译成「编译器与导出器直接能用的、已经校验过的、带缓存的蓝图」。编译主循环只需要读它，不需要再做推断。

一句话直觉：**`CompileArgs` 是用户的草稿，`CompileConfig` 是车间主任审过、可以直接照着干的施工图。**

#### 4.1.2 核心流程

`CompileConfig` 有两个公开构造入口，共享同一份实现：

```
CompileCommand ──► CompileConfig::new(command)       ┐
                                                    ├──► new_impl(args, watch=None)
WatchCommand  ──► CompileConfig::watching(command)  ┘        或 (args, watch=Some(..))
```

- `new` 用于 `typst compile`，传入 `watch = None`，所以 `config.watching == false`。
- `watching` 用于 `typst watch`，传入 `watch = Some(..)`，`config.watching == true`，并且只有这条路径会构造内置 HTTP 服务器（受 `http-server` feature 控制）。

`new_impl` 大致分这几段（后续小节逐一展开）：

1. 收集「静态警告」容器 `warnings`，克隆 `input`。
2. 推断 `output_format`（显式 `--format` 优先，否则看输出文件扩展名，否则默认 PDF）。
3. 推导 `output`（显式 `--output` 优先，否则把输入文件换扩展名）。
4. 把 `--pages` 翻译成 `PageRanges`，并据此计算 `tagged`（是否写 PDF 标签）。
5. 校验 `tagged == false` 时是否与所选 PDF 标准冲突，冲突就 `bail!`。
6. 用 `PdfStandards::new` 把 CLI 枚举编译成核心库要的类型。
7. （仅 watch）按需构造 `HttpServer`。
8. 处理已弃用的 `--make-deps`、校验 stdout 冲突。
9. 把所有字段组装成 `Self` 返回。

#### 4.1.3 源码精读

**两个构造入口只是 `new_impl` 的薄包装**：`new` 和 `watching` 的唯一区别是给 `new_impl` 的第二个参数传 `None` 还是 `Some(command)`，从而决定 `watching` 字段和 HTTP 服务器的去留。

[crates/typst-cli/src/compile.rs:91-100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L91-L100)
这里把编译命令与 watch 命令统一收口到 `new_impl`。

**`CompileConfig` 结构体本身**：每一个字段都是「编译/导出阶段会直接读取」的东西。注意它混合了三类内容——路径与格式（`input`/`output`/`output_format`）、导出参数（`pages`/`tagged`/`pdf_standards`/`ppi`/`pretty`）、运行时状态（`warnings`/`watching`/`export_cache`/可选的 `server`）。

[crates/typst-cli/src/compile.rs:50-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L50-L89)
这段定义里值得留意的几个字段：
- `warnings: Vec<HintedString>`——这里装的是「与源码无关、来自命令行本身」的警告（如弃用提示、`--pages` 隐含 `--no-pdf-tags`），与编译器产生的源码级警告分开存放，稍后在 `compile_once` 里合并。
- `export_cache: ExportCache`——随配置一起创建，在 watch 模式下用来跳过没变的图片页（u2-l3 详讲）。
- `#[cfg(feature = "http-server")] pub server: Option<HttpServer>`——只有启用 feature、且是 watch、且输出格式是 HTML/Bundle 时才会是 `Some`。

**入口函数 `compile`** 串起三件事：建计时器 → 构造配置 → 构造 World → 用 `timer.record` 把 `compile_once` 包起来跑一次。

[crates/typst-cli/src/compile.rs:38-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L38-L48)
注意 `CompileConfig::new` 和 `SystemWorld::new` 都用 `?` 冒泡错误（`HintedStrResult`），说明「配置构造失败」和「环境构造失败」都属于 CLI 层的硬失败——直接报错退出，不进入编译。

#### 4.1.4 代码实践

**目标**：用真实运行确认「配置构造在编译之前、且只发生一次」。

1. 在 `crates/typst-cli` 下构建 CLI（参考 u1-l1 的构建方式）：`cargo build`。
2. 写一个最小文档 `hello.typ`，内容为一行 `#set page(width: 10cm); Hello`。
3. 运行 `./target/debug/typst compile hello.typ`，观察：即使不写 `--format` 也能得到 `hello.pdf`，说明 `output_format` 被推断为 PDF、`output` 被推导为 `hello.pdf`。
4. 再次运行同样的命令：正常情况下 `typst compile` 每次都会重新编译（不像 `watch` 有缓存）。你看到的「只构造一次配置」体现在单次进程内——可以用 `--timings timings.json` 跑一次，然后用 `cat timings.json` 或加载到 <https://ui.perfetto.dev> 观察是否只有一个 `compile once` 时间块（详见 4.4）。

> 现象：`--timings` 生成的 JSON 里应能看到名为 `compile once` 的计时条目，对应 `#[typst_macros::time(name = "compile once")]` 标注的函数。**待本地验证**：不同机器上计时绝对值不同，关注的是「条目存在」而非数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么把「输出格式推断」放进 `CompileConfig::new_impl` 而不是 `compile_once` 里每次都做？

> **答案**：输出格式只取决于命令行参数（`--format` 或输出文件扩展名），在一次进程（尤其 watch 反复重编译）内不会改变。提前算好并存进 `CompileConfig`，既避免重复劳动，也把「一次性的参数校验」与「每次都跑的编译循环」解耦，让 `compile_once` 保持纯粹。

**练习 2**：`CompileConfig::new` 与 `CompileConfig::watching` 各自把 `watch` 参数传成什么值？这会怎样影响 `watching` 字段？

> **答案**：`new` 传 `None`（`watching == false`），`watching` 传 `Some(command)`（`watching == true`）。后者还会触发 HTTP 服务器的构造逻辑。

---

### 4.2 输出格式推断与输出路径推导

#### 4.2.1 概念说明

用户写 `typst compile doc.typ` 时通常**既不指定 `--format` 也不指定 `--output`**，CLI 却能乖乖产出 `doc.pdf`。这背后的两段「猜测」就是 `new_impl` 最开始要做的事：

- **格式推断（format inference）**：优先级是 `--format/-f` > 输出文件扩展名 > 默认 PDF。
- **路径推导（output derivation）**：优先级是 `--output` > 把输入文件扩展名换成当前格式对应的扩展名。

这两步有先后：必须先确定 `output_format`，才能推导默认输出路径（因为换扩展名要按格式来）。

#### 4.2.2 核心流程

格式推断的决策树（`a ? b : c` 风格）：

```
output_format =
  显式 --format 存在?         => 用它
  : 否则 args.output 是路径?   => 按扩展名匹配 pdf/png/svg/html
                                （都不匹配则 bail「无法推断」）
  : 否则（output 是 stdout 或未给）=> OutputFormat::Pdf
```

路径推导：

```
output =
  显式 --output 存在?         => 用它
  : 否则 => 把 input 路径的扩展名换成
           {Pdf=>"pdf", Png=>"png", Svg=>"svg", Html=>"html", Bundle=>""}
```

注意两个边界：
- stdin 输入时，clap 已经用 `required_if_eq("input", "-")` 强制要求给出 `--output`（见 u1-l3），所以默认路径推导分支里的 `let Input::Path(path) = &input else { panic!(...) }` 实际上不可能触发——注释里写的「guarded by the CLI」就是指这道防线。
- `Bundle` 格式换扩展名时用的是空字符串 `""`，意味着「把输入路径去掉扩展名」作为输出目录名（u2-l3 详讲）。

#### 4.2.3 源码精读

**格式推断**：先看 `--format`，再看输出路径扩展名，最后兜底 PDF。

[crates/typst-cli/src/compile.rs:111-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L111-L127)
`ext.eq_ignore_ascii_case("pdf")` 用大小写不敏感比较，所以 `doc.PDF` 也能识别。若扩展名四个都不匹配（比如 `doc.txt`），用 `bail!` 报错并提示用 `--format/-f` 手动指定。

**路径推导**：用 `with_extension` 按格式换扩展名。

[crates/typst-cli/src/compile.rs:129-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L129-L142)

`OutputFormat` 的五个变体在 `args.rs` 里定义，`is_paged()` 把 PDF/PNG/SVG 归为一类（对应 `PagedDocument`），与 HTML/Bundle 区分。

[crates/typst-cli/src/args.rs:589-604](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L589-L604)

#### 4.2.4 代码实践

**目标**：亲手验证「扩展名 → 格式」的映射与「无法推断」的报错。

1. 准备 `hello.typ`。
2. 分别运行下面四条命令，观察产物文件名（`.pdf`/`.png`/`.svg`/`.html`）：
   - `./target/debug/typst compile hello.typ`（应得 `hello.pdf`）
   - `./target/debug/typst compile hello.typ hello.png`（应得 `hello.png`）
   - `./target/debug/typst compile hello.typ hello.SVG`（大小写不敏感，应得 `hello.SVG`，内容为 SVG）
   - `./target/debug/typst compile hello.typ -f svg out.xyz`（显式 `-f svg` 优先于 `.xyz`，应得 SVG 内容写入 `out.xyz`）
3. 再运行一条应当报错的命令：
   - `./target/debug/typst compile hello.typ hello.txt`
   - **预期**：报错 `could not infer output format for path hello.txt. consider providing the format manually with --format/-f`，退出码非 0（`bail!` 在 CLI 层会变成硬失败）。

> 步骤 3 的错误文案直接来自 [compile.rs:119-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L119-L123) 这段 `bail!`，逐字对照即可确认。**待本地验证**退出码确为非 0。

#### 4.2.5 小练习与答案

**练习 1**：运行 `typst compile - <out.pdf < hello.typ`（从 stdin 读、写到 `out.pdf`），输出路径推导分支会执行吗？

> **答案**：不会。因为 stdin 输入时 clap 强制要求给出 `--output`/位置输出参数，`args.output` 一定是 `Some`，所以走 `unwrap_or_else` 的「用显式输出」分支，不会进入 `with_extension` 那段。若万一进入，`let Input::Path(path) = &input else { panic!(...) }` 会触发，但注释说明这被 CLI 提前挡住了。

**练习 2**：为什么 `Bundle` 对应的扩展名是空字符串 `""`？

> **答案**：Bundle 是「一整个目录的文件集合」而非单个文件，输出位置是一个目录。用空扩展名意味着把输入路径去掉扩展名当目录名用（比如 `doc.typ` → 目录 `doc/`）。写盘逻辑在 u2-l3 的 `export_bundle`/`write_virtual_fs`。

---

### 4.3 tagged 标签、--pages 与 PDF 标准的校验逻辑

#### 4.3.1 概念说明

这一小节是 `new_impl` 里**约束最密集**的一段，涉及三件事的三角关系：

- **`--pages`**：只导出部分页面（如 `--pages 2,4-6`）。它在 CLI 里是 `Option<Vec<Pages>>`，一旦非空就表示「用户想分页导出」。
- **PDF 标签（tagged）**：为了无障碍（屏幕阅读器等），Typst 默认会在 PDF 里写「标签树」。标签是为「完整文档」设计的，**和「只导出部分页」天然冲突**。
- **PDF 标准**：像 `PDF/A-1a`、`PDF/A-2a`、`PDF/A-3a`、`PDF/UA-1` 这类带 `a`（accessible）或 `UA` 的标准**强制要求**带标签。

于是产生了这条隐含规则：**只要用了 `--pages`，就等同于 `--no-pdf-tags`（因为部分页无法生成正确的标签树）**。这条规则会让那些「必须带标签」的标准无法满足，所以需要校验并给出清晰报错。

#### 4.3.2 核心流程

先算 `tagged`，再按 `tagged` 决定警告/报错：

```
pages  = args.pages 翻译成 Option<PageRanges>
tagged = (!args.no_pdf_tags) && pages.is_none()
        // 即：用户没显式关标签、且没分页导出 → 才写标签

if 输出是 PDF 且 pages 存在 且 用户没显式 --no-pdf-tags:
    push 静态警告 "using --pages implies --no-pdf-tags"
    （提示：PDF 将不可无障碍访问；加 --no-pdf-tags 可消音）

if !tagged:                     // 即标签被关掉了（无论原因）
    对每个「必须无障碍」的标准 (A_1a/A_2a/A_3a/UA_1)：
        若用户选了它：
            - 显式 --no_pdf_tags  => bail "cannot disable PDF tags when exporting a {name} document"
            - 否则（因 --pages 隐含关闭）=> bail 同样信息 + hint "using --pages implies --no-pdf-tags"
```

这里有一个微妙之处：`tagged` 把「两种关闭标签的原因」合并成了一个布尔值，但 `bail!` 时又要区分这两种原因，于是用 `args.no_pdf_tags` 再判一次，从而给出更精准的提示。

#### 4.3.3 源码精读

**计算 `pages` 与 `tagged`，并按需 push 静态警告**：

[crates/typst-cli/src/compile.rs:144-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L144-L156)
`args.pages` 是 `Option<Vec<Pages>>`（来自 u1-l3 的页面范围解析），这里把内部 range 抽出组装成核心库要的 `PageRanges`。`HintedString::from(...).with_hints([...])` 构造的就是一条「主消息 + 提示行」的静态警告，它会先存进 `warnings`，编译完再和源码级警告一起打印。

**无障碍标准的强校验**：`ACCESSIBLE` 是一个 `(PdfStandard, 显示名)` 的数组，逐项检查。

[crates/typst-cli/src/compile.rs:158-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L158-L178)
注意这里的两个 `bail!`：第一个（用户显式 `--no-pdf-tags`）只给主消息；第二个（因 `--pages` 隐含关闭）额外带 `hint:`。`bail!` 宏的 `; hint: ...` 语法是 typst 的 `HintedStrResult` 专属，用来追加提示行——这正是 u1-l2 提到的 `HintedStrResult`（主消息 + 提示）的来源。

**stdout 与 watch 的冲突校验**：除了标签，`new_impl` 还要保证「不能在 watch 模式往 stdout 写产物/依赖」「不能同时把产物和依赖都写到 stdout」。

[crates/typst-cli/src/compile.rs:211-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L211-L222)
这三个分支用 `match (&output, &deps, watch)` 一次性覆盖三种非法组合，干净利落。

> 补充：已弃用的 `--make-deps`（隐藏选项）也在这一段附近被处理——如果用户用了它而没有指定 `--deps`，就把它转成 `--deps <path> --deps-format make` 并 push 一条弃用警告。
>
> [crates/typst-cli/src/compile.rs:201-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L201-L209)

#### 4.3.4 代码实践

**目标**：观察 `--pages` 隐含 `--no-pdf-tags` 的警告，以及它与「必须无障碍」标准的冲突报错。

1. 准备一个多页文档 `multi.typ`：
   ```typst
   #set page(width: 10cm, height: 5cm)
   #lorem(200)
   ```
2. 运行 `./target/debug/typst compile multi.typ --pages 1`，观察终端是否打印警告 `using --pages implies --no-pdf-tags`（并带两条提示行）。这说明没显式 `--no-pdf-tags` 时，`--pages` 会自动关标签并提醒你。
3. 再运行 `./target/debug/typst compile multi.typ --pages 1 --pdf-standard a-1a`：
   - **预期**：报错 `cannot disable PDF tags when exporting a PDF/A-1a document`，且因这里是「`--pages` 隐含关闭」而非「显式关闭」，应带 hint 行 `using --pages implies --no-pdf-tags`。退出码非 0。
4. 对照源码确认：步骤 2 的警告来自 [compile.rs:149-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L149-L156)，步骤 3 的报错（带 hint 的那个分支）来自 [compile.rs:170-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L170-L175)。

> **待本地验证**：不同版本可能的措辞略有差异；以你本地实际输出为准与源码对照。

#### 4.3.5 小练习与答案

**练习 1**：用户运行 `typst compile doc.typ --no-pdf-tags --pdf-standard ua-1`，会得到什么？为什么？

> **答案**：报错 `cannot disable PDF tags when exporting a PDF/UA-1 document`。因为 `PDF/UA-1` 在 `ACCESSIBLE` 列表里，它强制要求标签；用户又显式 `--no-pdf-tags`，所以命中 [compile.rs:168-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L168-L169) 那个不带 hint 的 `bail!` 分支。

**练习 2**：如果既不传 `--pages` 也不传 `--no-pdf-tags`，`tagged` 是什么？这有什么实际效果？

> **答案**：`tagged = true`（默认带标签）。导出 PDF 时会写入无障碍标签树，给屏幕阅读器提供结构信息。这也是为什么 Typst 的默认 PDF 体积略大——标签树占空间，可用 `--no-pdf-tags` 关闭。

**练习 3**：为什么校验循环里要分 `if args.no_pdf_tags { ... } else { ... }` 两个 `bail!`，而不是合并成一句？

> **答案**：为了让提示更精准。用户显式关标签时，错误就是单纯的「标准不允许」；而因 `--pages` 隐含关标签时，用户可能并不知道分页会关标签，所以多给一条 hint `using --pages implies --no-pdf-tags` 帮他定位原因。

---

### 4.4 compile_once：单次编译-导出-诊断主循环与 Timer 计时

#### 4.4.1 概念说明

`CompileConfig` 准备好后，真正「跑一次编译」的就是 `compile_once`。它是 `typst compile` 的心脏，也是 `typst watch` 每次重编译时反复调用的同一段逻辑。它要协调五件事：

1. **状态显示**（仅 watch）：开始时打印 `Compiling…`，结束按结果打印 `Success`/`PartialSuccess`/`Error`。
2. **编译 + 导出**：调用 `compile_and_export`，拿到产物或错误，外加一堆警告。
3. **合并静态警告**：把 `CompileConfig::warnings`（命令行层面的警告）追加到编译器警告里。
4. **打印诊断**：成功时只打印警告；失败时打印错误 + 警告，并调用 `set_failed()` 让退出码变非 0。
5. **写依赖文件**：如果配置了 `--deps`，把本次编译依赖的文件列表写出（供 Make/Ninja 等用）。

此外，`compile_once` 本身被 `#[typst_macros::time]` 标注，并整体包在 `Timer::record` 里，用来产出性能计时数据。

#### 4.4.2 核心流程

```
compile_once(world, config):
    start = Instant::now()
    if config.watching: Status::Compiling.print()           // watch 专属

    Warned { output, warnings } = compile_and_export(world, config)
                        // output: Result<Vec<Output>, Vec<SourceDiagnostic>>

    // 把命令行静态警告转成 SourceDiagnostic 追加进 warnings
    for w in config.warnings: warnings.push(warning(Span::detached(), ...))

    match output:
        Ok(_) =>        // 编译成功
            duration = start.elapsed()
            if watching:
                warnings 空? => Status::Success(duration)
                否则        => Status::PartialSuccess(duration)
            print_diagnostics(world, errors=[], warnings)    // 只打警告
            open_output(config)?                             // --open 打开产物
        Err(errors) => // 编译失败
            set_failed()                                     // 退出码改非 0（软失败！）
            if watching: Status::Error.print()
            print_diagnostics(world, errors, warnings)       // 打错误 + 警告

    if let Some(dest) = config.deps:                         // 写依赖文件
        write_deps(world, dest, deps_format, output.ok())
    Ok(())
```

关键设计点：

- **`compile_once` 永远返回 `Ok(())`**。即使编译失败，它也只是 `set_failed()` 后正常返回——这是 u1-l2 讲过的「软失败」：进程退出码非 0，但 `?` 不会提前中断后续逻辑（比如诊断照样打印）。真正的「硬失败」是配置/环境构造阶段的 `bail!`。
- **静态警告用 `Span::detached()`**。命令行警告不指向源码任何位置，所以用「游离 span」包成 `SourceDiagnostic`，这样就能和源码级警告走同一条 `print_diagnostics` 打印管道。
- **依赖写出用 `output.as_deref().ok()`**：只有编译成功才有产物路径可写进依赖文件的 outputs；失败时传 `None`。

#### 4.4.3 源码精读

**`compile_once` 全貌**：注意 `#[typst_macros::time(name = "compile once")]` 这个属性宏——它把整个函数体包进计时，名字 `compile once` 会出现在 `--timings` 的 JSON 里。

[crates/typst-cli/src/compile.rs:257-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L257-L314)

**解构 `Warned` 并合并静态警告**：`compile_and_export` 返回 `Warned<SourceResult<Vec<Output>>>`，把「产物结果」与「旁路警告」拆开。

[crates/typst-cli/src/compile.rs:267-275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L267-L275)

**成功 / 失败两个分支**：成功时按「有无警告」区分 `Success` 与 `PartialSuccess`，失败时 `set_failed()` 后打印错误。

[crates/typst-cli/src/compile.rs:277-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L277-L306)

**末尾写依赖**：`output.as_deref().ok()` 把 `Result` 转成 `Option`——成功才传产物路径。

[crates/typst-cli/src/compile.rs:308-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L308-L311)

**Timer 是怎么把 `compile_once` 包起来的**：回到入口 `compile`，`timer.record(&mut world, |world| compile_once(world, config))` 用闭包把 `compile_once` 交给计时器执行；`Timer::new_or_placeholder` 在用户没给 `--timings` 时返回一个「占位」计时器（不真正记录），所以计时是无开销可选功能。

[crates/typst-cli/src/compile.rs:38-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L38-L48)

**`compile_and_export` 按 `output_format` 分发**：这是「编译」与「导出」的衔接点。本讲只需理解它对 Paged/Png/Svg 走 `typst::compile::<PagedDocument>`，对 Html 走 `HtmlDocument`，对 Bundle 走 `Bundle`；导出细节留给 u2-l3。

[crates/typst-cli/src/compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341)

**`print_diagnostics` 把 errors 和 warnings 串成一个迭代器**，经 `typst_kit::diagnostics::emit` 打到 `terminal::out()`。具体格式（human/short）与终端抽象在 u2-l4 详讲，这里只注意它把「错误在前、警告在后」拼到一起。

[crates/typst-cli/src/compile.rs:717-733](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L717-L733)

#### 4.4.4 代码实践

**目标**：用 `--timings` 看到「compile once」计时块，并制造一次编译失败以观察软失败（错误打印但流程不中断、退出码非 0）。

1. 准备两个文件。正确版 `ok.typ`：`#set page(width: 10cm); Hi`。错误版 `bad.typ`：故意写一个语法错误，如 `#let x = ;`。
2. 计时实践：
   ```
   ./target/debug/typst compile ok.typ --timings t.json
   ```
   用 `cat t.json`（或加载到 <https://ui.perfetto.dev>）查找名为 `compile once` 的事件。
   - **预期**：JSON（Chrome Trace Event 格式）里存在 `name` 为 `compile once` 的条目。这正是 [compile.rs:257](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L257) 的宏注入的。
3. 软失败实践：
   ```
   ./target/debug/typst compile bad.typ; echo "exit=$?"
   ```
   - **预期**：终端打印一段带源码定位的错误诊断（来自 `print_diagnostics`），随后 `exit=` 显示一个非 0 值。注意「诊断被打印」说明 `compile_once` 并没有因错误而提前 `return`，而是走完失败分支后正常返回——这就是软失败。`set_failed()` 的效果体现在那个非 0 退出码上（参考 u1-l2 的 `thread_local EXIT`）。

> **待本地验证**：`--timings` 的 JSON 字段结构在不同版本可能微调；退出码的具体数值以本地为准（通常失败为 1）。

#### 4.4.5 小练习与答案

**练习 1**：编译失败时 `compile_once` 返回 `Ok(())`，那进程为什么还是以非 0 退出？

> **答案**：因为失败分支调用了 `set_failed()`，它把 `thread_local` 的 `EXIT` 从 `SUCCESS` 改成 `FAILURE`。`main()` 最终用这个值作为进程退出码（u1-l2）。这是「返回值正常、退出码异常」的软失败模式，目的是让诊断打印等收尾逻辑不被 `?` 提前打断。

**练习 2**：为什么命令行层面的静态警告要先存进 `CompileConfig::warnings`，而不是在 `compile_once` 里临时构造？

> **答案**：因为静态警告的内容（如「`--pages` 隐含 `--no-pdf-tags`」「`--make-deps` 已弃用」）只取决于命令行参数，在 `new_impl` 阶段就能确定；提前算好存起来，`compile_once` 只负责「把它们转成 `SourceDiagnostic` 追加到编译器警告里统一打印」，职责更清晰，也避免在每次重编译时重复判定。

**练习 3**：`compile_and_export` 返回的 `Warned` 里，`output` 和 `warnings` 分别是什么类型？为什么要把它们打包在一起？

> **答案**：`output: SourceResult<Vec<Output>>`（要么是产物列表，要么是错误诊断列表），`warnings: Vec<SourceDiagnostic>`。打包是因为「即使最终失败，编译过程中也可能产生了有价值的警告」，需要独立于主结果传递出来，否则失败时警告会被丢掉。

---

### 4.5 PdfStandards 构造与 From<PdfStandard> 转换

#### 4.5.1 概念说明

PDF 标准在代码里出现在**两个层面**：

- **CLI 层**：`crates/typst-cli/src/args.rs` 的 `PdfStandard` 枚举，每个变体用 `#[value(name = "a-1a")]` 暴露成命令行字符串。它是给 clap 用的，只关心「用户在 `--pdf-standard` 后面写了什么」。
- **核心库层**：`typst_pdf::PdfStandard` 与 `typst_pdf::PdfStandards`。前者是单个标准的枚举，后者是「一组标准的集合表示」，真正参与 PDF 导出的内部结构。

`compile.rs` 要做两件事把这两层接起来：

1. **逐个转换**：用 `From<PdfStandard> for typst_pdf::PdfStandard` 把 CLI 枚举的每个变体映射到核心库枚举。
2. **整体构造**：用 `PdfStandards::new(&[...])` 把这组标准编译成最终的 `PdfStandards`，顺便做「多个标准是否兼容」的校验（不兼容就 `bail!`）。

最终构造出的 `config.pdf_standards` 会传给 `pdf_options`，再交给 `typst_pdf::pdf` 做真正的合规化处理（这部分在核心库，不在 CLI）。

#### 4.5.2 核心流程

```
// new_impl 里：
pdf_standards =
    PdfStandards::new(
        args.pdf_standard          // Vec<PdfStandard>  (CLI 枚举)
            .iter().copied()
            .map(Into::into)       // ← 这里触发 From<PdfStandard>，转成 typst_pdf::PdfStandard
            .collect::<Vec<_>>()
            .as_slice()
    )?;                            // 不兼容的标准组合会在这里 bail

// pdf_options 里，最终塞进 PdfOptions：
PdfOptions {
    ...
    standards: config.pdf_standards.clone(),
    tagged: config.tagged,
    ...
}
```

`From` 转换是纯一一映射（同名变体互转，除了 `UA_1` → `Ua_1` 这种大小写差异），没有任何业务逻辑——它的存在意义是**把两个 crate 的类型隔离开**，让 CLI 不直接依赖 `typst_pdf` 的内部枚举布局。

#### 4.5.3 源码精读

**`PdfStandards::new` 的调用点**：注意 `.map(Into::into)` 隐式触发下面的 `From` 实现。

[crates/typst-cli/src/compile.rs:180-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L180-L182)

**`From<PdfStandard>` 转换**：每个 CLI 变体映射到一个核心库变体。注意 `PdfStandard::UA_1 => typst_pdf::PdfStandard::Ua_1` 这种大小写差异——CLI 用 `UA_1`（受 `#[expect(non_camel_case_types)]` 允许），核心库用 `Ua_1`。

[crates/typst-cli/src/compile.rs:735-757](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L735-L757)

**`pdf_options` 把标准与标签组装进 `PdfOptions`**：这里还能看到 `creation_timestamp` 的处理——CLI 传入时用 UTC，否则用本地时间。`standards` 与 `tagged` 正是前面几节算出来的结果。

[crates/typst-cli/src/compile.rs:600-625](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L600-L625)

> 对照看 CLI 的 `PdfStandard` 枚举定义，`#[value(name = ...)]` 决定了命令行接受的字符串（`1.4`/`a-1a`/`ua-1`…），与 Rust 变体名解耦。
>
> [crates/typst-cli/src/args.rs:657-699](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L657-L699)

#### 4.5.4 代码实践

**目标**：通过编译不同 PDF 标准的产物，确认 `From` 转换与 `PdfStandards::new` 生效（这一步主要是源码阅读 + 轻量验证）。

1. 运行 `./target/debug/typst compile ok.typ --pdf-standard 1.7 a-1b`，观察是否成功生成 `ok.pdf`。多个标准用空格或逗号分隔（`value_delimiter = ','`，同时 clap 也接受多次出现）。
2. 阅读 [compile.rs:180-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L180-L182) 与 [compile.rs:735-757](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L735-L757)，确认命令行 `ua-1` 是如何变成 CLI 变体 `PdfStandard::UA_1`、再变成核心库 `typst_pdf::PdfStandard::Ua_1` 的三段链路：`字符串 → CLI 枚举（clap）→ 核心库枚举（From）`。
3. **（可选，源码阅读型）** 尝试组合两个不兼容的标准（若你不确定哪些不兼容，可阅读 `typst_pdf::PdfStandards::new` 的源码——它定义在核心 crate，不在本讲义范围内），观察 `PdfStandards::new` 返回 `Err` 时 CLI 的报错表现。若不便构造，标注「待本地验证」。

> 重点不是纠结哪些标准互斥（那是 `typst_pdf` 的事），而是理解 CLI 这里只做「忠实搬运 + 类型映射」，校验职责委托给核心库。

#### 4.5.5 小练习与答案

**练习 1**：为什么需要手写 `From<PdfStandard> for typst_pdf::PdfStandard`，而不是让两个枚举直接是同一个类型？

> **答案**：为了解耦。CLI 的 `PdfStandard` 服务于命令行解析（`#[value(name=...)]`、`#[expect(non_camel_case_types)]`），核心库的 `typst_pdf::PdfStandard` 服务于 PDF 合规化逻辑。让 CLI 直接复用核心库枚举，会把命令行命名约束（如 `UA_1`）泄漏进核心库，反之亦然。`From` 实现是一道干净的「翻译层」。

**练习 2**：`.iter().copied().map(Into::into).collect::<Vec<_>>()` 这串链式调用里，`Into::into` 实际调用的是哪个函数？

> **答案**：调用的是下面手写的 `From<PdfStandard> for typst_pdf::PdfStandard` 的 `from`（`Into::into` 由 `From` 自动获得）。它把每个 CLI `PdfStandard` 转成 `typst_pdf::PdfStandard`。

**练习 3**：`config.pdf_standards` 最终在哪个函数里被消费？

> **答案**：在 `pdf_options` 里被塞进 `PdfOptions { standards: config.pdf_standards.clone(), ... }`（[compile.rs:616-624](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L616-L624)），随后 `export_pdf` 把 `PdfOptions` 交给 `typst_pdf::pdf`（[compile.rs:379-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L379-L388)）做真正的标准合规化导出。

## 5. 综合实践

把本讲四个最小模块串成一个端到端的小任务：**追踪一次「带分页、带 PDF 标准、带依赖输出」的编译，从命令行一路跟到 `compile_once` 末尾**。

**输入文档** `report.typ`（3 页以上）：

```typst
#set page(width: 21cm, height: 29.7cm)
#lorem(600)
```

**任务步骤**：

1. **配置构造阶段（4.1 / 4.2 / 4.3）**：运行
   ```
   ./target/debug/typst compile report.typ report-out.pdf \
     --pages 1,3 \
     --deps report.d.json --deps-format json \
     --pdf-standard a-1b
   ```
   运行前先预测，运行后对照确认：
   - `output_format` 如何被推断？（看输出路径扩展名 `.pdf`）→ 4.2
   - 因为用了 `--pages`，`tagged` 应为 `false`，且应出现「using --pages implies --no-pdf-tags」警告 → 4.3
   - `a-1b` **不在** `ACCESSIBLE` 列表里（只有 `a-1a/a-2a/a-3a/ua-1` 在），所以不会触发标签冲突报错，编译应成功 → 4.3
2. **编译-导出-诊断阶段（4.4）**：观察终端先打印分页导出的警告，然后（成功）只打印警告不打印错误，最后写出 `report.d.json`。
3. **依赖产物（衔接 u3-l4）**：打开 `report.d.json`，对照说明它的 `inputs` 应包含 `report.typ`（以及任何被它 `#import`/`#include`/`#image` 的文件）。
4. **标准转换（4.5）**：在源码里标注这次 `a-1b` 走过的三段链路——`字符串 "a-1b"` →（clap）→ `PdfStandard::A_1b` →（`From`）→ `typst_pdf::PdfStandard::A_1b` →（`PdfStandards::new`）→ `config.pdf_standards` →（`pdf_options`）→ `PdfOptions.standards`。
5. **计时验证（4.4）**：再跑一次加 `--timings tr.json`，确认 `compile once` 条目存在。

**预期结果**：得到只含第 1、3 页的 `report-out.pdf`，终端有一条 `--pages` 隐含关闭标签的警告，`report.d.json` 正确记录依赖，`tr.json` 含 `compile once` 计时。如果某一步与预期不符，回到对应小节的源码精读核对。

> 涉及依赖文件 JSON 结构的细节（`inputs`/`outputs` 字段、UTF-8 校验）属于 u3-l4「依赖追踪与构建集成」，本实践只做现象级观察。

## 6. 本讲小结

- **`CompileConfig` 是「施工图」**：它在编译前一次性构造，把松散的 `CompileArgs` 翻译成已校验、可直接消费的蓝图；`new`（compile）与 `watching`（watch）共享 `new_impl`，靠第二参数区分是否 watch。
- **格式与路径都有三级回退**：`--format` > 输出扩展名 > 默认 PDF；`--output` > 输入换扩展名。stdin 输入由 clap 强制要求显式输出。
- **`tagged` 把两种「关标签」原因合并**：`tagged = !no_pdf_tags && pages.is_none()`。`--pages` 隐含 `--no-pdf-tags` 并产生警告；带 `a`/`UA` 的标准强制要求标签，冲突时按原因给出带或不带 hint 的 `bail!`。
- **`compile_once` 是软失败心脏**：编译失败也返回 `Ok(())`，靠 `set_failed()` 改退出码；它串联「状态显示 → 编译导出 → 合并静态警告 → 打印诊断 → 写依赖」，并用 `Span::detached()` 把命令行警告塞进同一打印管道。
- **Timer 是可选无开销计时**：`#[typst_macros::time]` + `Timer::record` + `new_or_placeholder`，只有 `--timings` 时才真正记录，产出 Chrome Trace JSON。
- **PDF 标准走三段链路**：`字符串 → CLI PdfStandard（clap）→ typst_pdf::PdfStandard（From 转换）→ PdfStandards（new 构造 + 校验）→ PdfOptions.standards`，CLI 只做忠实搬运与类型映射。

## 7. 下一步学习建议

- **u2-l3 多格式导出**：本讲反复提到的 `compile_and_export` 分发、图片页面模板 `{p}/{0p}/{t}`、`ExportCache`、HTML/Bundle 写盘都在那里展开，建议紧接着学。
- **u2-l4 诊断与终端输出**：本讲里 `print_diagnostics` 只是调用点，`codespan-reporting` 的彩色输出、`terminal.rs` 的 `TermOut` 抽象、human/short 两种格式的差异在那一讲详讲。
- **u2-l5 Watch 模式与增量重编译**：本讲的 `compile_once` 是 watch 每次重编译调用的同一段逻辑；watch 的主循环、依赖监控、`comemo::evict` 缓存淘汰在那一讲。
- **u3-l4 依赖追踪与构建集成**：本讲末尾的 `write_deps` 调用点对应 `deps.rs` 的 JSON/Zero/Make 三种格式，那里讲清依赖文件的结构。
- **源码延伸**：想了解 `PdfStandards::new` 的兼容性校验与真正的 PDF 合规化，需要跨到 `crates/typst-pdf`；想了解 `#[typst_macros::time]` 宏的展开，可读 `crates/typst-macros`。
