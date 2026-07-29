# CLI 集成与端到端实践

> 本讲是 typst-bundle 学习手册的最后一篇。前面几讲我们一直在看「typst-bundle 内部是怎么工作的」——从 `bundle_impl` 的实现化、`compile_document` 的格式分派，到 `export.rs` 的编码与 `introspect.rs` 的统一内省。本讲换一个视角：**站在 CLI 这一侧**，看用户敲下 `typst compile` 之后，命令行是怎么把 bundle 这套机制驱动起来、并把一堆文件写到磁盘上的。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `typst::compile::<Bundle>` 在 CLI 里是如何被选中和驱动的（`compile_and_export` 的分派）。
2. 描述 `export_bundle` 到落盘的完整函数链：构造 `BundleOptions` → 调用 `typst_bundle::export` → `write_virtual_fs` 写盘。
3. 区分两种「feature」：编译期的 Cargo feature `bundle` 与运行期的 CLI `--features bundle`，并解释 `warn_or_error_for_bundle` 的「开了警告、没开报错」语义。
4. 说明 stdout 为什么不能作为 bundle 的输出、以及 `typst watch` 下的 http-server 如何通过 `set_bundle` 在内存里托管整个 bundle。

## 2. 前置知识

本讲是专家层的「收尾」，默认你已经读过：

- **u4-l1（export 主流程与 VirtualFs）**：知道 `typst_bundle::export(bundle, options)` 用 rayon 并行把 `Bundle.files` 转成 `VirtualFs`（一个 `IndexMap<VirtualPath, Bytes>`），以及 `BundleOptions` 聚合了 html/pdf/png/svg 四类选项。
- **u5-l2（跨文档链接与锚点）**：知道 `create_link_anchors` 为跨文档链接生成了锚点，导出时 `LateLinkResolver` 据此把链接解析成相对 URI。

另外需要一点 CLI 背景知识：

- **输出目标（Output）与输出格式（OutputFormat）的区别**。`typst::Output` 是编译器侧的产物 trait（`PagedDocument` / `HtmlDocument` / `Bundle` 都是它的实现）；`typst-cli` 的 `OutputFormat`（Pdf/Png/Svg/Html/Bundle）是命令行侧「我想要哪种产物」的用户选项。两者通过 `compile::<T>` 的类型参数对应起来。
- **两个 feature 不要混淆**。这是本讲最容易踩坑的地方，第 4.3 节会专门讲清楚：一个是在 `cargo build` 阶段的 Cargo feature（决定代码里有没有 `typst-bundle` 这个 crate）；一个是在 `typst compile` 运行时的 `--features` 参数（决定库的 `Feature::Bundle` 是否打开）。**它们是两回事。**

## 3. 本讲源码地图

本讲主要横跨三个 crate，源码地图如下：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-cli/src/compile.rs` | CLI 编译主入口。`compile_and_export` 按输出格式分派；`export_bundle` 把 `Bundle` 转成 `VirtualFs`；`write_virtual_fs` 把虚拟文件系统落盘。 |
| `crates/typst-cli/src/args.rs` | 定义 `OutputFormat` 枚举（含 `Bundle`）、`--features` 参数、`Output`（stdout/路径）与 `write`。 |
| `crates/typst-cli/src/world.rs` | 把 CLI 的 `Feature` 映射成 `typst::Feature`，并用 `Library::builder().with_features(...)` 注入库。 |
| `crates/typst/src/lib.rs` | 泛型编译入口 `compile::<T>`、`compile_impl` 里的 feature 门控、以及 `warn_or_error_for_bundle`。 |
| `crates/typst-bundle/src/export.rs` | `export()`、`BundleOptions`、`VirtualFs` 类型定义。 |
| `crates/typst-kit/src/server.rs` | `HttpServer`：watch 模式下的本地 HTTP 服务器，`set_bundle` 在内存里托管 bundle。 |
| `crates/typst-syntax/src/path.rs` | `VirtualPath::realize`：把虚拟路径拼到磁盘根目录上。 |

## 4. 核心概念与源码讲解

### 4.1 CLI 导出驱动：从 `compile::<Bundle>` 到 `export_bundle`

#### 4.1.1 概念说明

前面所有讲义里，我们分析的入口函数都是 `typst::compile::<Bundle>(world)`——一个**类型驱动**的泛型函数：传什么类型参数 `T`，就产出什么产物（u1-l2 讲过这个分发）。但用户在命令行里并不会写「类型参数」，用户写的是 `--format bundle`。所以第一个要回答的问题是：

> 用户写的 `--format bundle`，是怎么变成代码里的 `typst::compile::<Bundle>(world)` 调用的？

答案在 `typst-cli` 的 `compile.rs`：CLI 把所有「编译并导出」的逻辑收口在一个函数 `compile_and_export` 里，它用 `match config.output_format` 做运行时分派，而 bundle 这条分支里就写着 `typst::compile::<Bundle>(world)`。换句话说，**CLI 的运行时枚举分派 ↔ 编译器的编译期类型分派**，在 bundle 分支里对接上了。

#### 4.1.2 核心流程

从用户敲命令到产物生成，bundle 这一条线的完整流程是：

```
typst compile main.typ --format bundle --features bundle -o build/
   │
   1. CLI 解析参数：output_format = OutputFormat::Bundle
   2. compile_once(world, config)
        └─ compile_and_export(world, config)           // compile.rs:317
             └─ match OutputFormat::Bundle =>
                  typst::compile::<Bundle>(world)        // compile.rs:336  ← 编译
                  export_bundle(bundle, config)          // compile.rs:337  ← 导出
   3. export_bundle(bundle, config)                       // compile.rs:391
        ├─ 构造 BundleOptions{html,pdf,png,svg}
        ├─ typst_bundle::export(&bundle, &options) → VirtualFs  // compile.rs:399
        ├─ write_virtual_fs(root, &fs)                   // compile.rs:407  ← 落盘
        └─ (watch 模式) server.set_bundle(bundle, fs)     // compile.rs:411
```

注意第 2 步里 `compile::<Bundle>` 和 `export_bundle` 是用 `and_then` 串起来的：编译成功（拿到 `Bundle`）才导出；编译失败（`Err`）则直接跳过导出，进入错误打印。

#### 4.1.3 源码精读

先看分派入口 `compile_and_export`，bundle 分支在三选一里：

[crates/typst-cli/src/compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341) —— `compile_and_export` 按 `config.output_format` 分派。`OutputFormat::Bundle` 分支（335–339 行）先 `typst::compile::<Bundle>(world)` 拿到 `Bundle`，再 `export_bundle(bundle, config)`。

它的兄弟分支可以作为对照：PDF/PNG/SVG 走 `compile::<PagedDocument>`（323 行），HTML 走 `compile::<HtmlDocument>`（328 行）。**bundle 是唯一一个产物类型不是「单文档」的分支**——这正是 u1-l1 强调的「bundle 打破一次编译 = 一个文件」的体现。

再看真正的导出函数 `export_bundle`：

[crates/typst-cli/src/compile.rs:391-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L391-L415) —— `export_bundle` 做了四件事：

1. 392–397 行：把 CLI 上那些零散的格式选项（`--pretty`、PDF 标准、PNG 的 `--ppi` 等）聚合成一个 `BundleOptions`。
2. 399 行：调用 `typst_bundle::export(&bundle, &options)`，得到 `VirtualFs`（一个路径→字节 的映射）。**这一步把「逻辑产物」变成「字节」**，是 u4-l1/u4-l2 讲过的编码黑盒。
3. 400–407 行：确定输出根目录，并调用 `write_virtual_fs(root, &fs)` 落盘（落盘细节见 4.2）。
4. 409–412 行：如果在 watch 模式且启用了 http-server，把 bundle 喂给服务器（见 4.4）。

注意它的返回类型是 `SourceResult<Vec<Output>>`——返回的是「写出去了哪些文件路径」的列表，而不是单个路径，因为 bundle 一次会写出多个文件。

最后回到最顶层，看 `compile::<Bundle>` 是如何被「触发」的。`compile` 是泛型函数，feature 门控发生在它的内部实现 `compile_impl` 里：

[crates/typst/src/lib.rs:73-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L73-L82) —— `compile<T: Output>`，`T` 由调用方（CLI 的 `compile::<Bundle>`）在编译期决定。

[crates/typst/src/lib.rs:105-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L105-L109) —— `compile_impl` 开头 `match T::target()`：`Target::Bundle` 走 `warn_or_error_for_bundle(...)`（108 行）。**这就是 feature 门控的入口**，详见 4.3。

#### 4.1.4 代码实践

> **目标**：在源码里确认「运行时枚举分派」与「编译期类型分派」在 bundle 分支的对接点。

操作步骤：

1. 打开 `crates/typst-cli/src/compile.rs`，定位 `compile_and_export`（317 行起）。
2. 找到 `OutputFormat::Bundle =>` 分支（335 行），确认它调用了 `typst::compile::<Bundle>(world)`。
3. 跟进 `Bundle` 这个类型——它来自 `crates/typst-bundle/src/lib.rs` 的 `pub struct Bundle`（[lib.rs:44-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/lib.rs#L44-L54)），并且 `impl Output for Bundle` 的 `target()` 返回 `Target::Bundle`（[lib.rs:61-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/lib.rs#L61-L63)）。
4. 沿着 `compile::<Bundle>` 进 `compile_impl`，确认 `match T::target()` 的 `Bundle` 分支调用了 `warn_or_error_for_bundle`。

需要观察的现象：CLI 侧用**值**（`OutputFormat::Bundle`）做分派，编译器侧用**类型**（`T = Bundle`，进而 `T::target() = Target::Bundle`）做分派，两者在「Bundle」这个名字上对齐。

预期结果：你能在源码里画出一条完整的「`--format bundle` → `compile::<Bundle>` → `Bundle::target()` → `Target::Bundle`」的链路。**待本地验证**：如果你想确认 `OutputFormat` 枚举确实含 `Bundle` 变体，见 `crates/typst-cli/src/args.rs` 第 591–597 行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 bundle 分支的返回类型是 `Vec<Output>`，而 PDF 分支只返回一个输出？

**参考答案**：因为 bundle 一次编译会产出多个文件（多个 document + 多个 asset），`write_virtual_fs` 会返回每个写出文件的路径列表；而 PDF 只产生一个文件。

**练习 2**：如果用户只写了 `typst compile main.typ --features bundle`（没有 `--format bundle`），会发生什么？

**参考答案**：`--features bundle` 只打开运行期 `Feature::Bundle`（允许 bundle 编译不报错），但**不改变** `output_format`。`output_format` 默认仍由输出路径扩展名推断、否则为 Pdf（[compile.rs:111-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L111-L127)）。所以它会按 PDF 编译，bundle 完全没被触发。要触发 bundle 必须显式 `--format bundle`。

---

### 4.2 BundleOptions 构造与 VirtualFs 落盘：`write_virtual_fs`

#### 4.2.1 概念说明

`export_bundle` 拿到 `Bundle` 之后，第一步是把它变成字节（`VirtualFs`），第二步是把这些字节写到磁盘。这两步的职责分工是：

- `typst_bundle::export` 负责**编码**——把每个 `BundleFile`（document 或 asset）变成字节，但**完全不知道磁盘**。它的输出 `VirtualFs` 只是一个「虚拟路径 → 字节」的内存映射。
- `write_virtual_fs` 负责**落盘**——它知道磁盘根目录 `root`，把虚拟路径拼接上去，创建子目录，写字节。它**完全不管编码**。

这种「编码 / 落盘」分离的设计，让 `VirtualFs` 成了 typst-bundle 与 typst-cli 之间干净的交接格式：bundle crate 只产出一个内存里的文件系统，怎么用（写盘、塞进 http-server、打包）是消费者的事。

#### 4.2.2 核心流程

`write_virtual_fs(root, fs)` 对 `VirtualFs` 里每一个 `(path, data)` 并行做三件事：

```
对 fs 里的每个 (VirtualPath, Bytes)：   // rayon par_iter，并行
   1. path.realize(root)                  // 把虚拟路径拼到磁盘 root 上 → realized: PathBuf
   2. 若 realized 有父目录 → create_dir_all(parent)
   3. std::fs::write(realized, data)       // 写字节
   4. 记录 Ok(Output::Path(realized))      // 收集返回路径
最后 create_dir_all(root) 兜底（保证根目录存在）
```

关键点是 `VirtualPath::realize`：它不是简单的字符串拼接，而是按「段（segment）」逐段 push 到 `root` 上，并且在每一段上做路径校验（拒绝 `..` 逃逸等）。这保证了 bundle 里写出的路径不会越出 `root` 目录。

#### 4.2.3 源码精读

先看 `BundleOptions` 怎么构造——它就是四个格式选项的聚合：

[crates/typst-cli/src/compile.rs:391-399](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L391-L399) —— `BundleOptions { html, pdf, png, svg }`，每个子选项都来自 compile.rs 里的辅助函数（`html_options`、`pdf_options`、`png_options`、`svg_options`）。这正好对应 [export.rs:43-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/export.rs#L43-L53) 的 `BundleOptions` 结构。**bundle 里可能同时存在四种格式的文档**，所以四类选项得一起备齐，导出时按文档实际格式各取所需。

再看落盘函数：

[crates/typst-cli/src/compile.rs:417-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L417-L438) —— `write_virtual_fs`：

- 419–420 行：先 `create_dir_all(root)`，保证输出根目录存在。
- 422 行：`fs.par_iter()` 用 rayon **并行**写多个文件（bundle 里文件可能很多，并行写盘有收益）。
- 424–426 行：`path.realize(root)` 把虚拟路径拼到 `root` 上。如果虚拟路径里有子目录（比如 `assets/note.txt`），这里会把 `root/assets/note.txt` 算出来。
- 428–431 行：`realized.parent()` 有值就 `create_dir_all(parent)`，按需创建子目录。
- 433–434 行：`std::fs::write(&realized, data)` 写字节。
- 435 行：把写出的路径收集进返回的 `Vec<Output>`。

注意这里的「并行」和 u5-l3 讲的 rayon 并行是同一套机制：每个文件互相独立，适合并行；错误用 `collect()` 汇总成 `StrResult<Vec<Output>>`。

最后看 `VirtualPath::realize` 的实现，理解它为什么安全：

[crates/typst-syntax/src/path.rs:243-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L243-L249) —— `realize` 从 `root` 开始，对每一段 `s` 调用 `s.realize()`（逐段校验并转成 `OsStr`）后 `out.push(s)`。虚拟路径在构造时（u3-l2 讲过的 `BundlePath`）就已拒绝空路径、反斜杠和 `..`，所以这里只是把合法段顺序拼上，不会逃逸。

#### 4.2.4 代码实践

> **目标**：跟踪一个 `BundleFile::Asset` 和一个 `BundleFile::Document` 各自从 `VirtualFs` 到磁盘的路径。

操作步骤：

1. 假设 bundle 里有 `index.html`（document）和 `meta.json`（asset）两个文件。
2. 在 [export.rs:24-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/export.rs#L24-L40) 确认：`export()` 对两者都只产出 `(VirtualPath, Bytes)`——asset 直接 `bytes.clone()`（35 行），document 走 `export_document` 编码。**编码之后，两者在 `VirtualFs` 里就长得一模一样了**。
3. 在 [compile.rs:417-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L417-L438) 确认：`write_virtual_fs` 对 `VirtualFs` 里的条目**一视同仁**，不区分 document / asset，统一 `realize` + `fs::write`。

需要观察的现象：document 与 asset 的差异在 `export()` 里就抹平了；`write_virtual_fs` 只看到一个 `(path, bytes)` 的扁平映射。

预期结果：你能用一句话说清——「编码层区分 document/asset，落盘层只认字节」。**待本地验证**：实际编译一个含子目录 asset（如 `#asset("data/note.txt", ...)`）的 bundle，观察 `write_virtual_fs` 是否自动创建了 `data/` 子目录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_virtual_fs` 要在循环外 `create_dir_all(root)`，又在循环内对每个 `parent` `create_dir_all`？

**参考答案**：循环外保证根目录一定存在（即使 `fs` 为空也建一个空目录）；循环内保证每个文件的父目录存在（虚拟路径可能含多级子目录）。`create_dir_all` 是幂等的，重复调用无害。

**练习 2**：`write_virtual_fs` 用了 `par_iter`，但「同一个目录下写多个文件」会有竞争吗？

**参考答案**：不会有功能性竞争。不同文件写到不同路径，`create_dir_all` 幂等，`fs::write` 覆盖各自的目标文件。唯一的代价是并行的 `create_dir_all` 可能重复建同一个父目录，但因为幂等所以结果正确（性能上可忽略）。

---

### 4.3 feature 开关：`warn_or_error_for_bundle` 与 stdout 限制

#### 4.3.1 概念说明

这是本讲最关键、也最容易被误读的一节。**typst 里有两个完全不同的「feature」概念，都和 bundle 有关，但作用在完全不同的阶段：**

| 名称 | 阶段 | 控制什么 | 谁来开 |
| --- | --- | --- | --- |
| **Cargo feature `bundle`** | 编译期（`cargo build`） | 是否把 `typst-bundle` crate 编译进二进制；门控 `server::set_bundle` 等 `#[cfg(feature = "bundle")]` 代码 | 构建者（CLI 默认就开） |
| **运行时 `--features bundle`** | 运行期（`typst compile`） | 是否把库的 `Feature::Bundle` 打开；`warn_or_error_for_bundle` 据此「警告 or 报错」 | 用户（每次编译时） |

为什么要有两层？因为 bundle 是**实验性特性**，Typst 既想在发行版里「物理上带上」这套代码（方便用户试用），又想在用户没明确同意时「逻辑上禁止」它（避免误用未稳定的 API）。于是：Cargo feature 让代码编进去，运行时 feature 让用户显式 opt-in。

#### 4.3.2 核心流程

运行时 `--features bundle` 的传播链：

```
typst compile ... --features bundle          // args.rs:442  →  args.features: Vec<Feature> 含 Feature::Bundle
   │
   SystemWorld::new()                         // world.rs
   └─ Feature::Bundle  --Into-->  typst::Feature::Bundle   // world.rs:349-356
   └─ Library::builder().with_features(features).build()   // world.rs:67  → 库的 features 集合
   │
   compile::<Bundle>(world)
   └─ compile_impl: match Target::Bundle => warn_or_error_for_bundle(&library.features, sink)
        ├─ features.is_enabled(Feature::Bundle) == true  → sink.warn(...)   // 仅警告，继续
        └─ features.is_enabled(Feature::Bundle) == false → bail!(...)        // 致命错误，中断
```

而 stdout 限制则是 bundle 特有的硬约束：bundle 是「多个文件」，标准输出是「一条字节流」，二者根本对不上，所以 `export_bundle` 直接拒绝把 bundle 写到 stdout。

#### 4.3.3 源码精读

先看运行时 `--features` 参数的定义：

[crates/typst-cli/src/args.rs:440-443](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L440-L443) —— `--features` 参数（也支持环境变量 `TYPST_FEATURES`，逗号分隔）。文档明确写着「Enables in-development features that may be changed or removed at any time」——这就是「实验性 opt-in」的入口。

再看它如何注入到库里：

[crates/typst-cli/src/world.rs:64-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L64-L67) —— `SystemWorld::new` 里把 `process_args.features` 逐个 `Into::into` 转成 `typst::Feature`，再 `Library::builder().with_features(features).build()`。映射规则见 [world.rs:349-356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L349-L356)：CLI 的 `Feature::Bundle` → `typst::Feature::Bundle`。

然后是门控本身——`warn_or_error_for_bundle`：

[crates/typst/src/lib.rs:270-286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L270-L286) —— 两个分支：

- **已开启**（271–277 行）：`sink.warn(...)`，提示「bundle export is experimental」，**只警告、不中断**，编译继续。
- **未开启**（278–284 行）：`bail!(...)`，提示「bundle export is only available when `--features bundle` is passed」，**致命错误、立即中断**。

注意它和 HTML 的姐妹函数 `warn_or_error_for_html`（[lib.rs:247-266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L247-L266)）是同一套模式。这说明 Typst 对所有实验性输出目标都用「开 = 警告、不开 = 报错」的一致策略。

> 对比 Cargo feature：`set_bundle` 上挂着 `#[cfg(feature = "bundle")]`（[server.rs:55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L55)），这是**编译期**门控。而 typst-cli 对 typst-kit 的依赖里**写死了** `"bundle"`（[typst-cli/Cargo.toml:58-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L58-L71)），所以官方 CLI 二进制永远带着 typst-bundle 代码——用户感知不到 Cargo feature 这一层，只感知运行时 `--features bundle`。

最后看 stdout 限制：

[crates/typst-cli/src/compile.rs:400-407](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L400-L407) —— `export_bundle` 在取输出根目录时，`Output::Stdout` 分支直接 `bail!("cannot write bundle to standard output")`（402–404 行）。这是**编译期常量错误**（用 `Span::detached()`），因为它和具体源码位置无关，纯粹是「输出方式与产物形态不匹配」。

#### 4.3.4 代码实践

> **目标**：亲手触发 `warn_or_error_for_bundle` 的两个分支，观察行为差异。

操作步骤（**待本地验证**，需要本地能构建并运行 typst CLI）：

1. **报错分支**：准备一个最小 bundle 源 `main.typ`（内容见下方「示例代码」），运行：
   ```
   typst compile main.typ --format bundle -o build/
   ```
   不带 `--features bundle`。预期：报致命错误「bundle export is only available when `--features bundle` is passed」，并给 hint「bundle export is experimental」。
2. **警告分支**：同样的源，运行：
   ```
   typst compile main.typ --format bundle --features bundle -o build/
   ```
   预期：编译成功，但 stderr 里有一条警告「bundle export is experimental」。
3. **stdout 限制**：尝试把输出指向 stdout：
   ```
   typst compile main.typ --format bundle --features bundle -o -
   ```
   预期：报错「cannot write bundle to standard output」。

示例代码（最小 bundle，仅作演示，非项目原有文件）：

```typst
#document("hello.html")[Hello, bundle!]
```

需要观察的现象：第 1 步与第 2 步的唯一差别就是有没有 `--features bundle`，结果一个是致命错误、一个只是警告——这正是 `warn_or_error_for_bundle` 两个分支的体现。

预期结果：你能在终端分别复现「报错」「警告成功」「stdout 报错」三种现象。若本地未启用 bundle 编译能力，则**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然 typst-cli 已经在 Cargo.toml 里写死了 `typst-kit/bundle`，为什么用户还要在运行时再传一次 `--features bundle`？

**参考答案**：Cargo feature 解决的是「代码在不在二进制里」（物理存在），运行时 `--features bundle` 解决的是「这次编译允不允许用」（逻辑许可）。bundle 实验性未稳定，Typst 要求用户每次显式 opt-in，避免误用，所以即使代码编进去了，默认仍 `bail!`。

**练习 2**：如果把 `warn_or_error_for_bundle` 里的 `bail!` 改成 `sink.warn`，会有什么后果？

**参考答案**：那 bundle 就不再是「opt-in」——任何用户 `--format bundle` 都会静默（仅警告）地编译成功。这违背了「实验性特性必须显式同意」的策略，可能让用户在不知情的情况下依赖未稳定的行为。所以报错分支是必要的护栏。

---

### 4.4 http-server 模式：`set_bundle` 的内存托管

#### 4.4.1 概念说明

`typst watch` 会在本地起一个 HTTP 服务器，把编译产物托管在内存里，浏览器访问就能看到结果，源文件一改就自动刷新。对 HTML 而言，服务器只需要托管「一个页面」（`set_html`）。但 bundle 是**多个文件**，所以服务器要变成一个「迷你的虚拟文件系统路由器」：浏览器请求 `/index.html` 就返回首页，请求 `/meta.json` 就返回那个 asset。

这个能力由 `typst-kit` 的 `HttpServer` 提供，bundle 模式下通过 `set_bundle` 注入。

#### 4.4.2 核心流程

```
typst watch main.typ --format bundle --features bundle   (默认会起 http-server，因 default = [..., "http-server"])
   │
   1. CompileConfig::new_impl 见 output_format==Bundle 且 watch 且 server 启用
        → HttpServer::new(...) 创建服务器，返回 placeholder 页
   2. 每轮编译：compile_and_export → export_bundle
        └─ server.set_bundle(bundle, fs)        // compile.rs:411
             └─ set_router(闭包)：按请求路由在 fs 里查文件
                   ├─ 先查原始路径 path
                   └─ 再查 path/index.html（目录默认页）
             └─ 触发所有连接的浏览器 reload
```

`set_bundle` 同时接收 `bundle` 和 `fs` 两个参数——它需要 `fs` 来取字节，需要 `bundle` 来判断某个文件是不是 HTML 文档（只有 HTML 才能注入 live-reload 脚本）。

#### 4.4.3 源码精读

先看 CLI 在哪里创建服务器、又在哪里调用 `set_bundle`：

[crates/typst-cli/src/compile.rs:184-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L184-L196) —— `CompileConfig::new_impl` 里，当处于 watch 模式、未禁用 serve、且 `output_format` 是 `Html` 或 `Bundle` 时，创建 `HttpServer`。

[crates/typst-cli/src/compile.rs:409-412](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L409-L412) —— `export_bundle` 末尾，`#[cfg(feature = "http-server")]` 门控下调用 `server.set_bundle(bundle, fs)`。注意它传的是 **`bundle`（移动）和 `fs`（引用的克隆）**——`bundle` 被服务器持有，因为路由闭包需要查 `bundle.files` 判断文件类型。

再看 `set_bundle` 的实现，它是 bundle 路由的核心：

[crates/typst-kit/src/server.rs:54-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L54-L78) —— `set_bundle` 调 `set_router` 注册一个闭包，闭包逻辑：

1. 58 行：把请求路径包成 `VirtualPath`。
2. 59 行：算出 `path.join("index.html")`——支持「目录默认页」语义（请求 `/` 时回落到 `/index.html`）。
3. 60–74 行：依次尝试 `path` 和 `with_index` 两个候选，在 `fs` 里查；查到后判断：如果这个文件在 `bundle.files` 里是 `BundleDocument::Html`，就当 HTML 返回（可注入 live-reload），否则当 `Raw` 字节返回。
4. 76 行：都没查到返回 `None`（外层 `handle` 会回 404）。

这里有个精妙之处：服务器**不是简单地把 `fs` 整个 dump 出去**，而是「按请求路由、按需取字节」。对于 HTML 文档它会返回 `HttpBody::Html`（带 live-reload 脚本注入能力，见 [server.rs:168-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L168-L185) 的 `handle_body`），对于 PDF/asset 则返回 `HttpBody::Raw` 并按扩展名猜 MIME 类型（[server.rs:228-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L228-L236) 的 `select_mime_type`）。这就是它需要 `bundle` 参数的原因——只有 `bundle` 能告诉它「这个文件是不是 HTML」。

最后看 `set_router` 如何触发浏览器刷新：

[crates/typst-kit/src/server.rs:80-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L80-L86) —— `set_router` 调 `bucket.put(router)`，而 `Bucket::put`（[server.rs:331-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L331-L334)）会 `condvar.notify_all()`，唤醒所有阻塞在 `/__events` 上的浏览器连接（[server.rs:213-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L213-L219)），发送 `reload` 事件，浏览器自动刷新。

#### 4.4.4 代码实践

> **目标**：理解 `set_bundle` 的「目录默认页」与「HTML/Raw 分流」两个设计。

操作步骤（源码阅读型）：

1. 读 [server.rs:54-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L54-L78)，找到「尝试 `path` 和 `path/index.html` 两个候选」的循环（60 行）。
2. 找到「判断是否 HTML 文档」的 `matches!` 表达式（62–67 行），确认它查的是 `bundle.files` 而非 `fs`。
3. 读 [server.rs:168-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L168-L185)，确认 `HttpBody::Html` 会注入 live-reload 脚本，`HttpBody::Raw` 不会。

需要观察的现象：服务器把 bundle 当成一个「按 URL 路由的网站」来服务，HTML 文档享受热重载，非 HTML 文件按 MIME 直出。

预期结果：你能解释为什么 `set_bundle` 必须同时收 `bundle` 和 `fs`——`fs` 提供字节，`bundle` 提供类型判断。**待本地验证**：用 `typst watch ... --format bundle --features bundle` 起服务，浏览器访问 `http://localhost:3000/`，确认能看到首页且改动后自动刷新。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `set_bundle` 不能只传 `fs`、省掉 `bundle`？

**参考答案**：`fs` 只是 `VirtualPath → Bytes`，丢失了「这个文件原本是 HTML 文档还是 asset」的信息。而只有 HTML 文档才需要注入 live-reload 脚本（返回 `HttpBody::Html`）。所以必须保留 `bundle`，靠 `bundle.files` 判断文件类型。

**练习 2**：浏览器请求 `/`（根路径）时，服务器怎么知道该返回哪个文件？

**参考答案**：路由闭包会把 `/` 包成 `VirtualPath`，并额外尝试 `path.join("index.html")`（[server.rs:59-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L59-L60)）。只要 bundle 里有 `index.html`，请求 `/` 就会回落到它——这就是「目录默认页」语义。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个端到端的 bundle 项目。

### 5.1 实践目标

编写一个真实的 bundle 项目，它同时包含：1 个 HTML 文档、1 个 PDF 文档、1 个由 `#context` + `json.encode` 生成的 `meta.json` asset，且两个文档之间存在 `#link` 跨链。然后编译、逐项检查产物，并对照源码说明从 `export_bundle` 到落盘经过了哪几个函数。

### 5.2 示例源文件（示例代码，非项目原有文件）

新建 `main.typ`：

```typst
// 示例代码：一个最小的多文件 bundle
// 用法：typst compile main.typ --format bundle --features bundle,html -o build/

// ① 一个 HTML 文档（首页），里面有一条指向 report 的跨文档链接
#document("index.html")[
  #set document(title: "首页")
  = 首页
  这是 bundle 的入口页。
  查看年度 #link(<report>)[报告（PDF）]。
]

// ② 一个 PDF 文档（报告），标题在子内容里用 set 设置，并回链首页
#document("report.pdf")[
  #set document(title: "年度报告 2026")
  = 年度报告 <report>
  本报告由 typst-bundle 生成。
  返回 #link(<home>)[首页]。
]

// ③ 一个 asset：用 #context + json.encode 动态生成 meta.json
#context {
  let meta = (
    project: "my-bundle",
    documents: 2,
    generated-by: "typst",
  )
  asset("meta.json", json.encode(meta))
}
```

> 说明：HTML 文档需要 `Feature::Html`，所以 `--features` 要写成 `bundle,html`（逗号分隔，对应 [args.rs:442](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L442) 的 `value_delimiter = ','`）；这一点在 u2-l2 的 `compile_document` HTML 门控（[typst-bundle/src/lib.rs:321-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/lib.rs#L321-L329)）里讲过。标签的精确附着语法（`<report>`、`<home>`）请以本地实际行为为准——**待本地验证**。

### 5.3 操作步骤（待本地验证）

1. 编译：
   ```
   typst compile main.typ --format bundle --features bundle,html -o build/
   ```
2. 检查 `build/` 目录，预期包含：`index.html`、`report.pdf`、`meta.json` 三个文件。
3. 打开 `build/index.html`，点击「报告（PDF）」链接，确认它指向 `report.pdf`（跨文档链接生效）。
4. 查看 `build/meta.json`，确认内容是合法 JSON 且字段正确。
5. 对照源码，把「编译到落盘」的函数链填进下表：

| 步骤 | 函数 | 文件:行 |
| --- | --- | --- |
| 选中 bundle 分支 | `compile_and_export` 的 `OutputFormat::Bundle` 分支 | compile.rs:335-339 |
| 导出入口 | `export_bundle` | compile.rs:391-415 |
| 编码为字节 | `typst_bundle::export` | compile.rs:399 |
| 落盘 | `write_virtual_fs` | compile.rs:417-438 |
| （watch）托管 | `server.set_bundle` | compile.rs:409-412 |

### 5.4 预期结果与检查清单

- [ ] `build/` 下三个文件齐全（HTML + PDF + JSON）。
- [ ] `index.html` 里的链接能跳到 `report.pdf`（验证 u5-l2 的跨文档锚点 + u4-l2 的 `LateLinkResolver`）。
- [ ] `meta.json` 是合法 JSON，内容与源码里的 `meta` 字典一致（验证 asset 直通、`json.encode` 在 `#context` 内可用）。
- [ ] 若漏掉 `--features bundle`，编译报错（验证 4.3 的 `warn_or_error_for_bundle` 报错分支）。
- [ ] 若漏掉 `html`（只写 `--features bundle`），编译在 `compile_document` 处报 HTML 未启用（验证 u2-l2 的 HTML feature 门控）。

如果以上各项全部通过，你就把本手册从 u1 到 u5 的全部知识点串成了一条真实的编译链路。

## 6. 本讲小结

- CLI 用**运行时枚举** `OutputFormat::Bundle` 分派，编译器用**类型** `compile::<Bundle>` 接收，两者在 bundle 分支对接（`compile.rs:335-339`）。
- `export_bundle` 是导出总入口：构造 `BundleOptions` → 调 `typst_bundle::export` 得 `VirtualFs` → `write_virtual_fs` 落盘；`VirtualFs` 是 bundle 与 CLI 之间干净的「路径→字节」交接格式。
- `write_virtual_fs` 用 rayon 并行落盘，靠 `VirtualPath::realize` 把虚拟路径安全拼到磁盘根上，并按需创建子目录。
- **两种 feature 不要混淆**：Cargo feature `bundle`（编译期、CLI 默认开、门控代码存在）vs 运行时 `--features bundle`（门控 `warn_or_error_for_bundle`：开=警告、不开=报错）。
- bundle 不能写到 stdout（多文件 ≠ 单字节流），`export_bundle` 直接 `bail!`。
- watch 模式下 `set_bundle` 把 bundle 托管进 http-server 内存，按 URL 路由取字节，HTML 文档享 live-reload、非 HTML 按 MIME 直出——它需要 `bundle` 来判断文件类型。

## 7. 下一步学习建议

到这里，typst-bundle 学习手册的 11 篇讲义已经全部完成，你已经从「bundle 是什么」一路读到了「CLI 如何落盘」。如果还想继续深入，建议：

1. **对比 HTML 目标的 CLI 路径**：把本讲的 `export_bundle` 与 `export_html`（[compile.rs:344-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L357)）对比阅读，体会「单文件目标」与「多文件 bundle 目标」在 CLI 层的差异（尤其 stdout、http-server 的不同）。
2. **阅读 `typst-kit/src/server.rs` 全文**：理解 live-reload 的 SSE（Server-Sent Events）机制与 `Bucket` 的条件变量通知，这是 watch 体验的底层。
3. **动手扩展**：尝试给本讲的 bundle 加一个 SVG 文档和一个 PNG asset，验证四格式混排；再尝试用 `typst watch` 起服务，体验多文件热重载。
4. **回到内省与链接**：如果综合实践里跨文档链接没生效，回头重读 u5-l1（统一内省器）和 u5-l2（`create_link_anchors`），用 `--features bundle` 时的诊断信息定位问题。
