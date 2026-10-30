# typst-bundle：实验性格式的内部结构（待确认）

> 标题中的「待确认」来自规划阶段：typst-bundle 是 workspace 中较新的 crate。本讲全部内容已在当前 HEAD（`84cca406a`）上逐行核对源码，所述链路均真实存在；不确定之处文中会单独标注。需要提醒的是：bundle 导出仍处于实验阶段（源码中明确写着 "bundle export is experimental"），未来版本可能随时调整。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Feature::Bundle` 这个特性开关（feature flag）从 CLI 参数一路传递到编译器内部的两条独立作用线：一条控制**库的可见性**（`asset`、`document` 等绑定是否对脚本可见），一条控制**编译目标的准入**（`compile::<Bundle>` 是否被放行）。
2. 跟踪 bundle 格式的完整装配路径：`typst_bundle::FORMAT` 常量 → `Library::builder(...)` → `LibraryBuilder::build` → `std.format` 模块 → prelude 全局 `asset`，以及格式专属 show 规则如何注册进 `NativeRuleMap`。
3. 讲清 `Output::create` 之后的布局主线：`bundle()` → 根级 realize（`RealizationKind::Bundle`）→ `collect` 收集文档与资产 → 并行 `compile_document` → 按扩展名 `determine_format` → `layout_document_for_bundle` / `html_document_for_bundle`。
4. 掌握一种面对新模块时的通用阅读方法：**从注册点反向阅读**——先找到「它把自己注册到哪里」，再顺着注册时填入的函数指针一路读下去。

## 2. 前置知识

本讲默认你已读完 u5-l3（Format 注册机制）与 u4-l1（编译主流程），这里先复述三个关键概念：

- **Format（格式）与 Target（目标）的区别**：`Format` 是一个普通结构体（不是 trait），它把「一种导出格式的注册信息」打包成数据（格式元素、feature 门、规则注册函数）；`Target` 是编译目标枚举（`Paged` / `Html` / `Bundle`），决定整次编译走哪条 `Output` 生产线。bundle 既是**一种格式**（有 `FORMAT` 常量、有格式元素 `format.bundle`），又是**一种目标**（`Target::Bundle` 对应输出类型 `typst_bundle::Bundle`）。
- **feature flag（特性开关）**：实验功能的总闸。`Feature` 是一个只有三个变体的枚举（`Html`、`Bundle`、`A11yExtras`），宿主通过 `Library::builder(...).with_features(...)` 打开。feature 与 target 相互独立：feature 控制标准库里哪些绑定对脚本可见，target 决定编译产出什么。
- **虚拟文件系统（VirtualFs）**：bundle 导出不再产出「一个文件」，而是「一堆文件」。`typst-bundle` 用 `IndexMap<VirtualPath, Bytes>` 表示这堆文件——路径是 `typst-syntax` 里的虚拟路径（见 u2-l1），值是字节。CLI 再把它写到磁盘目录。

还有一个背景：为什么需要 bundle？此前 Typst 一次编译只能产出一个 PDF/PNG/SVG/HTML，而「多页网站」这类需求需要一次产出 `index.html`、`blog.html`、`styles.css` 等一整套文件。bundle 目标允许在一份 Typst 源码里用 `document("blog.html")[...]` 和 `asset("favicon.ico", ...)` 声明任意多个输出文件。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-bundle/src/lib.rs` | crate 入口：`Bundle` 输出类型、`Output` 实现、`bundle()` 主流程（根级 realize + 收集 + 并行编译各文档） |
| `crates/typst-bundle/src/format.rs` | `FORMAT` 常量与格式元素 `BundleFormat`、`AssetElem`（asset 元素）及其规则、`AssetData` 转换 |
| `crates/typst-bundle/src/introspect.rs` | `BundleIntrospector`：把所有子文档的内省合并成一个全局内省器 |
| `crates/typst-bundle/src/link.rs` | 跨文档链接锚点：给被链接的元素生成命名锚 |
| `crates/typst-bundle/src/export.rs` | `export()`：把 `Bundle` 翻译成 `VirtualFs`（逐文件调 typst-pdf/render/svg/html） |
| `crates/typst-library/src/lib.rs` | `Feature`/`Features`、`LibraryBuilder::build`、`global()`、prelude 注入 |
| `crates/typst-library/src/format.rs` | `Format` 结构体与 `format::module()`（上一讲主战场，本讲复用） |
| `crates/typst-library/src/model/document.rs` | `DocumentElem`（`document` 元素）、`determine_format`、`DocumentFormat`/`PagedFormat` |
| `crates/typst-library/src/routines.rs` | `RealizationKind::Bundle` 定义 |
| `crates/typst-realize/src/lib.rs` | realize 对 `RealizationKind::Bundle` 的特殊处理（`BUNDLE_RULES`） |
| `crates/typst-layout/src/pages/mod.rs` | `layout_document_for_bundle`：bundle 内单个分页文档的布局入口 |
| `crates/typst/src/lib.rs` | `compile::<T>` 的 target 分发、`warn_or_error_for_bundle` 门禁 |
| `crates/typst-cli/src/args.rs`、`world.rs`、`compile.rs` | `--features`/`--format` 参数、Library 装配、`export_bundle` 写盘 |
| `tests/src/world.rs`、`tests/src/run.rs`、`tests/src/output.rs` | 测试体系如何启用 bundle（`Features::all()` + bundle stage） |
| `tests/suite/model/asset.typ`、`tests/ref/bundle/*.txt` | bundle 测试用例与参考产物 |

## 4. 核心概念与源码讲解

本讲的三个核心最小模块是：**Bundle 特性开关**、**资源注入链路**、**bundle 布局入口**，最后附带一个收尾模块（导出与跨文档内省）把链路走完整。

### 4.1 模块一：Bundle 特性开关——feature 与 target 的双重门控

#### 4.1.1 概念说明

「启用 bundle」在代码里其实是**两个互相独立的开关**，初学者最容易把它们混为一谈：

1. **库侧开关**：`Feature::Bundle` 写进 `Library.features` 后，`std.format.bundle` 这个绑定才对脚本可见（带 feature 门的绑定在 feature 未开启时对脚本不可用，u3-l2 讲过作用域与绑定）。这意味着没开 feature 时，`#asset(...)` 连名字都解析不到。
2. **目标侧开关**：`typst::compile::<Bundle>()` 一进来就检查 `library.features.is_enabled(Feature::Bundle)`——开了只发一条「实验性」警告，没开直接报错。这是对「宿主忘了开 feature 却请求 bundle 目标」的兜底。

为什么一个功能要设两道门？因为 feature 门控的是**标准库内容**（作用于求值阶段），target 检查的是**编译流水线**（作用于 `Output::create`）。两者由不同的代码层持有，必须在两处各自把关。

#### 4.1.2 核心流程

```text
CLI: --features bundle (或环境变量 TYPST_FEATURES=bundle)
  │
  ├─► args.rs 解析为 Vec<Feature>          （参数层）
  ├─► world.rs: Library::builder([...typst_bundle::FORMAT])
  │              .with_features(features)   （库侧：feature 集合 + 格式注册）
  │       └─► format.bundle 绑定带上 feature 门 → #asset 可见
  │
  └─► compile.rs: --format bundle ⇒ compile_and_export 走
         typst::compile::<Bundle>(&world)   （目标侧）
              └─► compile_impl 匹配 Target::Bundle
                    └─► warn_or_error_for_bundle：feature 开 → 警告；没开 → 报错
```

#### 4.1.3 源码精读

先看 `Feature` 枚举——它住在 typst-library，全仓库只有这三个实验特性：

[crates/typst-library/src/lib.rs:L285-L299](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L285-L299) 定义了 `Feature` 枚举（`Html`、`Bundle`、`A11yExtras`）与 `Feature::all()`；`Features` 则是一个位集合，`is_enabled` 就是查位。CLI 传入的 `--features` 列表最终被收集进它：

[crates/typst-library/src/lib.rs:L252-L283](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L252-L283) `Features(SmallBitSet)` 的构造与查询：`FromIterator<Feature>` 逐位置位，`is_enabled` 按位判断。

CLI 侧的参数定义（`value_delimiter = ','` 支持逗号分隔，且可从 `TYPST_FEATURES` 环境变量读取）：

[crates/typst-cli/src/args.rs:L519-L522](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L519-L522) `--features` 参数：多个 `Feature` 以逗号分隔，环境变量 `TYPST_FEATURES` 作为默认值。

接着看库装配现场——`SystemWorld::new` 同时做了两件事：注册五种格式（含 `typst_bundle::FORMAT`），并打开 feature：

[crates/typst-cli/src/world.rs:L56-L77](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L56-L77) 把 `--features` 转成 feature 集合，连同五种 `FORMAT` 一起交给 `Library::builder(...).with_features(...).build()`。注意：**格式注册与 feature 开启是两次独立调用**——五个格式永远全部注册，feature 只决定门开不开。

最后是目标侧门禁。`compile_impl` 按 `T::target()` 分发（这正是 u4-l1 讲过的「泛型 Output 决定生产线」）：

[crates/typst/src/lib.rs:L100-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L100-L114) `compile_impl` 开头对三种 target 做检查：`Paged` 直接放行，`Html` 与 `Bundle` 各有一个 warn-or-error 函数。

[crates/typst/src/lib.rs:L269-L287](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L269-L287) `warn_or_error_for_bundle`：feature 已开 → 往 sink 写一条「bundle export is experimental」警告；未开 → `bail!` 直接失败，提示加上 `--features bundle`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到两道门的作用顺序——先撞上目标侧报错，再开 feature 看到实验性警告。

1. 准备一个最小 bundle 源文件 `multi.typ`（放在仓库任意临时目录，例如 `/tmp/bundle-lab/`）：

   ```typ
   #document("hello.pdf")[
     = Hello Bundle
     这是第一个文档。
   ]
   #asset("note.txt", "hello from asset")
   ```

2. **不开 feature**，先直接请求 bundle 目标：

   ```bash
   cargo run -p typst-cli -- compile --format bundle /tmp/bundle-lab/multi.typ -o /tmp/bundle-lab/out
   ```

3. **开 feature** 再跑一次：

   ```bash
   cargo run -p typst-cli -- compile --features bundle --format bundle \
     /tmp/bundle-lab/multi.typ -o /tmp/bundle-lab/out
   ```

**需要观察的现象**：

- 第 2 步应报错 `bundle export is only available when --features bundle is passed`——这条消息正来自 `warn_or_error_for_bundle` 的 `bail!` 分支。
- 第 3 步应出现警告 `bundle export is experimental`，随后在 `/tmp/bundle-lab/out/` 下生成 `hello.pdf` 与 `note.txt` 两个文件。
- 对照实验：把 `--features bundle` 去掉、也不传 `--format bundle`（输出扩展名推断不出 bundle，见 `compile.rs:114-127` 只认 pdf/png/svg/html），观察命令直接因「无法推断格式」失败——说明 bundle 无法靠扩展名触发，必须显式 `--format bundle`。

**预期结果**：错误/警告的措辞与源码字符串逐字一致；输出目录里出现两个文件。若本地未编译 CLI，以上均为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Library::builder` 永远注册 `typst_bundle::FORMAT`，而不是开了 feature 才注册？

**参考答案**：`Format` 注册的是「这种格式的元素与规则要不要进标准库」，而 feature 决定「绑定是否可见」。注册信息里带着 `feature: Some(Feature::Bundle)`（见 4.2.3 的 `with_feature`），`format::module()` 在定义绑定时统一挂门。若开了才注册，宿主程序就得写两套装配逻辑，且 `Library::formats` 列表（`DocumentFormatOptions::new` 要用它生成默认选项）会随 feature 抖动，破坏记忆化缓存的稳定性。

**练习 2**：如果宿主只开了 feature 却没传 `--format bundle`，会发生什么？

**参考答案**：什么特别的事都不会发生——编译照常走默认 `Target::Paged` 产 PDF。但此时 `#asset(...)` 在脚本里可见了：在 paged 目标下使用它会被 `ASSET_UNSUPPORTED_RULE` 拦下（见 4.2.3），报「assets are only supported in the bundle target」。这正是 `tests/suite/model/asset.typ` 里 `asset-outside-of-bundle paged` 测试用例覆盖的行为。

**练习 3**：`Feature` 枚举标了 `#[non_exhaustive]`，这表达了什么意图？

**参考答案**：编译器团队保留未来增删实验特性的权利。对下游使用者意味着：match 该枚举时必须带通配分支；对学习者的启示是——围绕 feature 写的代码（包括本讲内容）都可能随版本变化。

### 4.2 模块二：资源注入链路——FORMAT 常量到全局 asset

#### 4.2.1 概念说明

这个模块回答：「`#asset(...)` 这个函数和 `#document(...)` 元素是怎么出现在我的脚本里的？」

答案沿 u5-l3 的 Format 注册机制展开，bundle 是该机制的一个标准消费者。核心是一条**注册数据流**：

- 导出 crate（typst-bundle）在自己的 `format.rs` 里定义格式元素 `BundleFormat` 和常量 `FORMAT = Format::new::<BundleFormat>().with_feature(Feature::Bundle).with_rules(register)`——把「我需要哪些门、哪些规则」声明成数据。
- 宿主把 `FORMAT` 交给 `Library::builder`，`build()` 时统一装配：格式元素定义进 `std.format` 模块、规则注册函数灌进 `NativeRuleMap`。
- 最后 prelude 机制把 `std.format.bundle.asset` 提升为全局名 `asset`，并把沿途的 feature 门一并带给全局绑定。

`document` 元素则不同：它定义在 typst-library 的 `model/document.rs`（因为「文档元数据」是所有目标共用的概念，`set document(title: ...)` 在 PDF 导出里也要用），bundle 只是给它**解锁了两个 bundle 专属字段**（`path` 与 `format`，以及作为函数调用时的 `body`）。

#### 4.2.2 核心流程

```text
typst_bundle::FORMAT (常量)
  │  Format { elem: BundleFormat::ELEM, feature: Bundle, rules: register }
  ▼
LibraryBuilder::build()
  ├─ global(formats, ...) ──► format::module(formats)
  │                             ├─ define "bundle" → BundleFormat 绑定（挂 feature 门）
  │                             └─ （AssetElem 定义在 BundleFormat 的 scope 上）
  ├─ prelude(global)
  │     └─ prelude_path(["format","bundle","asset"]) ──► 全局名 asset（继承 feature 门）
  └─ for format in formats: format.register_rules(&mut rules)
        └─ register: 对 Target::Paged 与 Target::Html
             ├─ replace  DocumentElem 规则 → DOCUMENT_UNSUPPORTED_RULE
             └─ register AssetElem    规则 → ASSET_UNSUPPORTED_RULE
```

#### 4.2.3 源码精读

先看注册声明的源头——`FORMAT` 常量与 `register` 函数：

[crates/typst-bundle/src/format.rs:L11-L22](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L11-L22) `FORMAT` 用 builder 风格声明：格式元素是 `BundleFormat`，挂 `Feature::Bundle` 门，规则注册函数是 `register`。`register` 对 `Target::Paged` 和 `Target::Html` 两个目标分别 `replace`（覆盖 `document` 的默认规则）与 `register`（新增 `asset` 规则），写入的是两个「不支持」规则。

`Format::with_feature` / `with_rules` / `register_rules` 的实现（u5-l3 已精读，这里只定位）：

[crates/typst-library/src/format.rs:L127-L150](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L127-L150) 三个 builder 方法只是存数据，`register_rules` 在装配期调用存好的函数指针。

装配现场 `LibraryBuilder::build`：

[crates/typst-library/src/lib.rs:L231-L249](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L231-L249) `build` 做三件事：`global(&self.formats, ...)` 把格式元素定义进全局作用域；`(self.routines.rules)()` 先建基础规则表（由总装 crate 的 `ROUTINES` 接线，见 [crates/typst/src/lib.rs:L329-L342](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L329-L342)），再逐格式调 `format.register_rules(&mut rules)` 追加格式专属规则；最后 `formats` 整表存进 `Library`。

`std.format` 模块如何挂门：

[crates/typst-library/src/format.rs:L89-L103](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L89-L103) `format::module` 遍历所有注册格式，把格式元素按名 define 进作用域；若格式带 feature（bundle 带），则对绑定调用 `with_feature(feature)`——feature 未开启时这个绑定对脚本不可见。

prelude 提升——`asset` 全局化的那一行：

[crates/typst-library/src/lib.rs:L427-L431](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L427-L431) prelude 末尾按路径提升三个子绑定，其中 `["format", "bundle", "asset"]` 让 `asset` 直接以全局名可用。机制见 [crates/typst-library/src/foundations/scope.rs:L226-L242](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/scope.rs#L226-L242) 的 `prelude_path`：它沿路径逐级下钻并**把路径上的 feature 门聚合到全局绑定**（文档注释明确要求路径上不能有互相冲突的 feature 门）。所以 `#asset(...)` 在未开 feature 时同样不可见。

接着看两个「主角元素」。先是 `asset`：

[crates/typst-bundle/src/format.rs:L211-L225](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L211-L225) `AssetElem`：两个必选字段——`path: BundlePath`（bundle 内的输出路径，可含斜杠建目录）与 `data: AssetData`（原始字节）。注意 `#[elem(..., Locatable)]`：asset 是可定位元素（u4-l7 的概念），因此能被 `query` 查到、能挂标签。

[crates/typst-bundle/src/format.rs:L236-L245](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L236-L245) `AssetData` 的 `cast!`：脚本传 `str` 自动按 UTF-8 编码成 `Bytes`，传 `bytes`（如 `read(..., encoding: none)` 的结果）原样使用。

非 bundle 目标下的拦截规则：

[crates/typst-bundle/src/format.rs:L153-L161](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L153-L161) `DOCUMENT_UNSUPPORTED_RULE`：在 paged/html 目标下 `document` 元素被 show 时报「只有 bundle 目标才能构造文档」，并提示可用 `set document(..)` 配置元数据。

[crates/typst-bundle/src/format.rs:L227-L234](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L227-L234) `ASSET_UNSUPPORTED_RULE`：同理，非 bundle 目标下 `asset` 报「assets are only supported in the bundle target」。

最后是住在 typst-library 的 `DocumentElem`：

[crates/typst-library/src/model/document.rs:L146-L207](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/document.rs#L146-L207) `DocumentElem` 的字段分两组：`path`/`format`/`body` 标注了「only supported in the bundle target」（`path` 与 `body` 是必选，所以**只有在 bundle 目标下才能把 document 当函数调用**）；`title`/`author`/`description`/`keywords`/`date` 是所有目标共享的元数据，由 `set document(...)` 配置。

[crates/typst-library/src/model/document.rs:L209-L238](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/document.rs#L209-L238) `determine_format`：显式 `format` 参数优先，否则按 `path` 的扩展名推断（`.pdf`/`.svg`/`.png`/`.html`），都推不出就报错让用户显式指定。这个函数是 4.3 布局入口的关键分岔点。

[crates/typst-library/src/model/document.rs:L272-L317](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/document.rs#L272-L317) `DocumentFormat`（`Paged(PagedFormat)` 或 `Html`）与 `PagedFormat`（`Pdf`/`Png`/`Svg`）：一个 bundle 文档可以混合输出三种分页格式加 HTML——bundle 内部其实是个「格式聚合器」。

#### 4.2.4 代码实践

**实践目标**：通过测试套件验证「资源注入链路」两端的真实行为——`asset` 在 bundle 目标下产出文件、在 paged 目标下被拦。

1. 阅读现成测试 [tests/suite/model/asset.typ](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/model/asset.typ)：注意每个小节 `--- 名字 bundle ---` / `--- asset-outside-of-bundle paged ---` 的属性写法——`bundle` 属性声明该测试要跑 bundle stage。
2. 运行其中两个（沿用 u1-l5 学过的过滤语法）：

   ```bash
   cargo testit --exact asset
   cargo testit --exact asset-outside-of-bundle
   ```

3. 打开参考产物 [tests/ref/bundle/asset.txt](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/ref/bundle/asset.txt)：内容只有一行 `ecc3d25e910e0fc7677cc5418b7bcdad data.json`——bundle stage 的参考产物是「哈希 + 文件名」清单，对整个 VirtualFs 做逐文件哈希对照。

**需要观察的现象**：第一个测试通过（bundle 目标下 `#asset("data.json", "[1, 2]")` 产出 JSON 文件）；第二个测试通过且其「期望错误」注解（`// Error: 2-20 assets are only supported in the bundle target`）与 `ASSET_UNSUPPORTED_RULE` 的消息一致。

**预期结果**：两个测试均绿；若想看真实文件内容，可在 `tests/store/bundle/` 下找到本次运行的活输出（u1-l5 讲过 store 目录）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`#asset("a.txt", "hi")` 里的 `"hi"` 是字符串，`AssetElem.data` 却要求 `AssetData(Bytes)`——这中间发生了什么？

**参考答案**：`AssetData` 上有 `cast!`（format.rs:240-245）：`v: Str => Self(Bytes::from_string(v))` 把字符串按 UTF-8 编码为字节。这是 u3-l5 讲过的 FromValue 派发——`Bytes` 与 `Str` 两种脚本值都能转换进来，后者自动编码。

**练习 2**：为什么 `document` 元素定义在 typst-library 而 `asset` 定义在 typst-bundle？

**参考答案**：`document` 的元数据字段（title/author/…）是所有导出格式共用的（`set document(title: ...)` 在 PDF 导出同样生效），属于「类型归属 library」的架构约定（u1-l3）；而 `asset` 只对 bundle 目标有意义，没有跨格式复用价值，就近放在导出 crate 里，通过 `BundleFormat` 的 `#[scope]`（format.rs:147-151）挂到 `format.bundle` 作用域下。

**练习 3**：在 bundle 目标下，`set document(format: pdf)` 里的 `format` 参数用什么类型接收？合法值有哪些？

**参考答案**：`Smart<DocumentFormat>`（document.rs:163）。`DocumentFormat` 的 cast 支持 `PagedFormat`（`pdf`/`png`/`svg`，来自 `#[derive(Cast)]` 的字符串枚举）与字符串 `"html"`；`auto`（默认）则回落到按 `path` 扩展名推断。

### 4.3 模块三：bundle 布局入口——Output::create 到 layout_document_for_bundle

#### 4.3.1 概念说明

这是本讲的主战场：feature 开了、`asset`/`document` 都可见了，编译器要怎么把「一棵 Content 树」变成「一堆排版好的文档 + 一堆原始字节」？

关键认识有三点：

1. **`Bundle` 是一个 `Output`**。u4-l1 讲过 `compile::<T>` 的多轮迭代循环对任意 `T: Output` 通用；bundle 实现了 `Output` trait，所以它天然获得了「排版迭代至内省收敛」的能力——而且迭代的是**整个 bundle**（所有子文档一起参与一个大内省环，query 到的 heading 是所有文档里的 heading）。
2. **根级 realize 换了一副规则**。bundle 的根级 realization 用 `RealizationKind::Bundle`，其分组规则表 `BUNDLE_RULES` 是**空的**——顶层不允许普通排版内容，只放行 `document`、`asset` 和 Tag，其他元素直接报错「请包进 document」。
3. **每个 `document` 才是一次真正的排版**。`compile_document` 先 `determine_format` 决定子目标（paged 或 html），用 `TargetElem::target` 把子目标写进样式链（同一份样式，按文档切换 target——这是 u5-l3 讲过的三层分叉中的样式层），再调 `layout_document_for_bundle`（paged）或 `html_document_for_bundle`（html）。后者与普通的 `layout_document` 只差一件事：**不重置 Locator**，而是接收外层传入的 locator，让子文档的 Location 哈希挂在整个 bundle 的大树之下，跨轮次保持稳定。

#### 4.3.2 核心流程

```text
compile::<Bundle>
  └─ (迭代循环, ≤ MAX_ITERS=5) T::create ──► bundle(engine, content, styles)
       └─ bundle_impl  #[comemo::memoize]
            ├─ (routines.realize)(RealizationKind::Bundle, ...)
            │     └─ BUNDLE_RULES = []：顶层只留 Tag / DocumentElem / AssetElem
            ├─ collect(children)：分类为 Child::Tag/Asset/Document
            │     └─ 顶层杂散元素 → 报错；路径重复 → delayed_error
            ├─ engine.parallelize：并行处理每个 Child
            │     └─ compile_document(document, styles, locator)
            │          ├─ determine_format(styles)：显式 format ‖ 按扩展名推断
            │          ├─ TargetElem::target.set(format.target()) → 链进 styles
            │          ├─ Paged(fmt) ──► typst_layout::layout_document_for_bundle
            │          │      （PNG/SVG 时校验「必须单页」）
            │          └─ Html ──► 检查 Feature::Html ──► typst_html::html_document_for_bundle
            ├─ BundleIntrospector::new(&items)：合并全部子文档的内省
            ├─ link_targets + create_link_anchors：跨文档链接锚
            └─ 汇成 IndexMap<VirtualPath, BundleFile> ⇒ Bundle
```

#### 4.3.3 源码精读

先看 `Bundle` 类型与它的 `Output` 实现——这是它接入编译主循环的插头：

[crates/typst-bundle/src/lib.rs:L40-L75](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L40-L75) `Bundle` 持有 `files: Arc<IndexMap<VirtualPath, BundleFile>>` 与一个**全 bundle 共享的** `BundleIntrospector`（文档注释明确说明：整个 bundle 处于同一个大内省环，各文档互相可内省）。`Output` 的三个方法：`introspector()` 供收敛判定、`target()` 返回 `Target::Bundle`、`create()` 委托给 `bundle()`。

[crates/typst-bundle/src/lib.rs:L77-L95](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L77-L95) `BundleFile` 只有两种：`Document(BundleDocument)` 或 `Asset(Bytes)`；`BundleDocument` 又分 `Paged(Box<PagedDocument>, PagedExtras)` 与 `Html(Box<HtmlDocument>)`——注意 bundle 并不立即导出字节，而是保留**结构化的子文档**，导出（翻译成字节）推迟到 `export()` 阶段。

主流程入口与它的 memoize 壳（u5-l8 将展开 comemo，这里只需认识这个模式：外层薄函数拆 Engine、内层 `#[comemo::memoize]` 接 tracked 参数）：

[crates/typst-bundle/src/lib.rs:L130-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L130-L176) `bundle()` 拆出 world/library/introspector/sink/route 交给 `bundle_impl`；后者重建 Engine、把外部样式标记为 outside（与页面级样式边界有关，u4-l2 讲过 `visit_styled` 的执法），然后第一步就是调 `(engine.library.routines.realize)(RealizationKind::Bundle, ...)`——**经 Routines 函数指针表进入 typst-realize**（u1-l3 的「动态链接」架构，绕开循环依赖）。

`RealizationKind::Bundle` 与空规则表：

[crates/typst-library/src/routines.rs:L151-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/routines.rs#L151-L163) `RealizationKind` 四变体中 `Bundle` 是根级特例：「内容被 realize 成 documents 与 assets」。

[crates/typst-realize/src/lib.rs:L50-L62](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L50-L62) 与 [crates/typst-realize/src/lib.rs:L1022-L1026](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L1022-L1026) realize 按 kind 选择分组规则表：`Bundle` 用 `BUNDLE_RULES`（**空表**），普通文档用 `FLOW_RULES`（TEXTUAL/PAR/CITES/LIST/ENUM/TERMS）。空表意味着顶层的普通内容不会被打包成段落/列表——它们根本不该出现在这里。另有两条 bundle 特判见 [crates/typst-realize/src/lib.rs:L610-L652](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L610-L652)：bundle 下允许 `set document(format: ..)`、忽略 page 样式规则，而普通目标会报错。

收集阶段——分类与查重：

[crates/typst-bundle/src/lib.rs:L245-L295](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L245-L295) `collect` 遍历 realize 产物 `Vec<Pair>`：`TagElem` → `Child::Tag`（保留可定位元素，供内省）；`AssetElem` → `Child::Asset`；`DocumentElem` → `Child::Document`（同时 `locator.next(&elem.span())` 为该文档分配子 locator）；**其他任何元素** → 报错「{} is not allowed at the top-level in bundle export」，hint 是「try wrapping the content in a `document` instead」。路径查重用 `seen` 表，重复路径走 `delayed_error`（带「already in use here」的指向性 hint，u3-l6 讲过延迟错误在编译末尾统一晋升）。

并行编译每个文档：

[crates/typst-bundle/src/lib.rs:L189-L205](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L189-L205) `engine.parallelize(children, ...)` 用 rayon 并行处理各 Child（`collect_combined_result` 聚合错误）——各子文档互相独立，天然可并行。

[crates/typst-bundle/src/lib.rs:L297-L350](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L297-L350) `compile_document` 四步：`determine_format`（4.2.3 已读）；`TargetElem::target.set(format.target()).wrap()` 后 `styles.chain(&target)`——**用样式链切换子目标**，这是「同一套 Content 面向多目标」的关键一跳；paged 分支调 `typst_layout::layout_document_for_bundle`，且 PNG/SVG 格式时校验页数必须为 1（多页报 delayed_error「documents exported to an image format only support a single page」）；html 分支先检查 `Feature::Html`（未开则 bail，hint 教用户传 `--features bundle,html`），再调 `typst_html::html_document_for_bundle`。

本模块的标题角色——`layout_document_for_bundle` 与普通版本的唯一差异：

[crates/typst-layout/src/pages/mod.rs:L28-L76](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L28-L76) 普通 `layout_document`：memoize 壳内层调 `layout_document_common(..., Locator::root(), ...)`——**从根 locator 开始**，每次编译的 Location 都从同一棵哈希树的根重新分配。

[crates/typst-layout/src/pages/mod.rs:L78-L125](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L78-L125) `layout_document_for_bundle`：多接收一个外部 `locator: Locator`，用 `LocatorLink::new(locator)` 包装后交给同一个 `layout_document_common`。这样子文档的 Location 挂在外层 locator 之下——bundle 里第 N 个文档的位置不因前 N-1 个文档变化而漂移（u4-l7 讲过 Location 是 u128 分层哈希的「元素身份证」，这里的「分层」就体现在链式 locator 上）。

[crates/typst-layout/src/pages/mod.rs:L127-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L127-L176) `layout_document_common`：两个入口共享的全部实质——文档级 realize（`RealizationKind::Document`，收集 info 与格式选项）、`layout_pages` 分页布局、产出 `PagedDocument`。**读到这里可以下结论：bundle 内的一个 PDF 文档，其排版过程与普通 PDF 编译完全同构，只是 locator 的根不同。**

#### 4.3.4 代码实践

**实践目标**：不改一行源码，用 `--timings`（u4-l1 学过的计时输出）「看见」从 feature 检查到 `layout_document_for_bundle` 的真实调用链——计时点就是函数调用的脚印。

1. 用 4.1.4 的 `multi.typ`，加一个计时输出：

   ```bash
   cargo run -p typst-cli -- compile --features bundle --format bundle \
     /tmp/bundle-lab/multi.typ -o /tmp/bundle-lab/out --timings /tmp/bundle-lab/timings.json
   ```

2. 把 JSON 里的计时事件按时间排序，提取每个事件的 `name` 字段（Chrome Tracing 格式，事件带 `ts`/`dur`/`name`；可用 `jq -r '.traceEvents[].name'` 粗略列出，注意去重）。

3. 对照源码标注链路：`compile`（[crates/typst/src/lib.rs:L74-L83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L74-L83) 的 `#[typst_macros::time]`）→ `bundle`（[crates/typst-bundle/src/lib.rs:L130-L131](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/lib.rs#L130-L131)）→ `layout document`（[crates/typst-layout/src/pages/mod.rs:L79-L80](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L79-L80)，`layout_document_for_bundle` 的 time 标注名）→ `export bundle`（[crates/typst-bundle/src/export.rs:L26-L27](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/export.rs#L26-L27)）→ `export pdf`（[crates/typst-bundle/src/export.rs:L86-L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/export.rs#L86-L87)）。

4. 把 `multi.typ` 里的文档改成 `#document("hello.png")[...]` 再跑一次，观察 PNG 路径（`export png` 计时点出现）与「必须单页」校验。

**需要观察的现象**：计时事件里出现上述 scope 名；若文档触发多轮内省迭代，还能看到 `iter(n)` 层级的 `layout document` 反复出现（u4-l1 讲过迭代命名）。注意 `bundle_impl` 本身是 memoize 函数、没有 time 标注，它内部调用的子函数才可见。

**预期结果**：一份能逐事件对应到源码函数的调用链清单。具体事件集合随文档内容而变，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：顶层写一行裸文本 `hello`（不在任何 `document` 里），bundle 编译会发生什么？错误在哪个环节产生？

**参考答案**：realize 之后、`collect` 阶段报错。裸文本不会被 `BUNDLE_RULES` 分组（空表），会以普通元素形式流到 `collect` 的 `else` 分支，得到「… is not allowed at the top-level in bundle export」，hint「try wrapping the content in a `document` instead」（lib.rs:265-271）。

**练习 2**：`layout_document` 与 `layout_document_for_bundle` 为什么必须是两个函数、而不是给 `layout_document` 加一个可选 locator 参数？

**参考答案**：两者的 memoize 缓存键不同：普通版以「整份内容」为身份、locator 恒为根；bundle 版的 locator 来自外层 SplitLocator 的 `next()`，每个子文档各有一个。合并成一个带参数的函数会让普通编译也背上 locator 参数的缓存键开销，且普通版的语义就是「从根开始」。两入口共享 `layout_document_common`，重复代码为零——这是「薄入口 + 厚公共实现」的常见 crate 分层手法（`bundle`/`bundle_impl` 同理）。

**练习 3**：bundle 里一个 `#document("a.pdf")[...]` 的页码计数器，与另一个 `#document("b.pdf")[...]` 的页码计数器互相影响吗？

**参考答案**：不影响页码——页码计数器天然 per document（`BundleFormat` 文档注释明确写了这个例外：页计数器是按文档算的，因为分页本就按文档发生）。但 heading 计数器、state、query 等**其他内省是全 bundle 全局的**：两个文档的 heading 会连在一起编号，`query(heading)` 一次拿到所有文档的 heading。若要按文档隔离需手动重置（见 format.rs 文档的 Introspection 一节）。

### 4.4 模块四（收尾）：导出与跨文档内省——BundleIntrospector、链接锚与 VirtualFs

#### 4.4.1 概念说明

布局产出的 `Bundle` 是**结构化**的（子文档还是 `PagedDocument`/`HtmlDocument`），最后一公里是把它们翻译成字节。这一步有两个值得学习的机制：

1. **合并内省**：`BundleIntrospector` 把所有子文档的内省条目拼进一张按文档序排列的元素表，并用「off-by-one 下标」记录每个元素属于哪个子文档——外部（query/counter）看到的是一个统一的内省器，内部能路由到正确的子文档。
2. **跨文档链接**：`link` 到另一个文档里的标签，需要为目标元素生成**命名锚**（HTML 的 `id`、PDF 的 named destination）。这在布局完成后统一回填：`link_targets` 找出所有被链接的位置 → `create_link_anchors` 给它们分配锚名并**原地修改**子文档（HTML 改 DOM、paged 改导出选项）。链接的相对路径解析则被推迟到导出期，由 `LateLinkResolver` 携带「当前文件路径 + bundle 内省器」完成。

#### 4.4.2 核心流程

```text
bundle_impl 产出 items 后：
  BundleIntrospector::new(&items)         ── 合并元素表（文档序）
  introspector.link_targets()             ── 收集所有被链接的 Location，按文档分组
  create_link_anchors(&mut items, ..)     ── HTML: 原地改 DOM 插 id
                                            paged: 写入 extras.anchors（named destinations）
                                            资产/文档本身: 分配空锚（使自身可被链接）
  files = IndexMap<path, BundleFile>      ── 结构化 Bundle 完成

导出期（独立于编译循环）：
  typst_bundle::export(&bundle, &BundleOptions)
    └─ rayon 并行逐文件：
         Document(Paged, Pdf)  ──► typst_pdf::pdf_in_bundle(.., anchors, link_resolver)
         Document(Paged, Png)  ──► typst_render::render(第 1 页).encode_png()
         Document(Paged, Svg)  ──► typst_svg::svg_in_bundle(.., anchors, ..)
         Document(Html)        ──► typst_html::html_in_bundle(root, .., ..)
         Asset(bytes)          ──► 原样返回
  CLI: write_virtual_fs(root, &fs)        ── 写入磁盘目录
```

#### 4.4.3 源码精读

[crates/typst-bundle/src/introspect.rs:L22-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/introspect.rs#L22-L78) `BundleIntrospector` 的存储：`children` 保存每个子文档的路径与子内省器；`elements: ElementIntrospector<Option<NonZeroUsize>>` 是目标无关的内省引擎（u4-l7 讲过 Introspect trait），position 里存的「off-by-one 下标」指回 `children`——`child(location)` 用「下标减一」路由到目标文档。query 系方法全部委托 `elements`，所以对脚本而言全 bundle 就是一个可查询整体。

[crates/typst-bundle/src/link.rs:L20-L63](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/link.rs#L20-L63) `create_link_anchors`：HTML 文档调 `typst_html::create_link_anchors` 原地改 DOM 插 id；paged 文档把锚写进 `extras.anchors`（供 PDF named destination / SVG anchor 使用）；最后给每个 asset 与 document 自身插入**空锚**，让「链接到某个文件」也能成立。

[crates/typst-bundle/src/export.rs:L22-L46](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/export.rs#L22-L46) `VirtualFs` 类型别名与 `export`：rayon 并行逐文件导出，每个文档先构造 `LateLinkResolver::new(Some(path), introspector)`——链接解析此刻才知道「自己是谁、邻居在哪」。Asset 字节直接透传。

[crates/typst-bundle/src/export.rs:L61-L82](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/export.rs#L61-L82) `export_document` 按 `extras.format`/变体派发到四种导出器；注意各导出函数（如 [crates/typst-bundle/src/export.rs:L84-L94](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/export.rs#L84-L94) 的 `export_pdf`）单独 `#[comemo::memoize]`——字体子集化等重活跨编译缓存，这正是 u5-l1 讲过的记忆化策略在 bundle 场景的复用。`typst-bundle` 的 Cargo.toml 因此依赖 typst-pdf/render/svg/html 四个导出 crate（见 [crates/typst-bundle/Cargo.toml](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/Cargo.toml)）——它是一个「格式聚合器」crate。

CLI 侧的写盘与预览服务器：

[crates/typst-cli/src/compile.rs:L417-L446](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L417-L446) `export_bundle`：组装 `BundleOptions`（聚合 html/pdf/png/svg 四组选项，均来自对应 CLI 参数）→ `typst_bundle::export` → 拿到 `VirtualFs` 后要求输出必须是目录（写 stdout 直接拒绝）→ `write_virtual_fs` 落盘；若在 `typst watch` 下还把 bundle 交给 HTTP 服务器做 live reload（`--port`/`--no-reload`/`--no-serve` 参数，见 [crates/typst-bundle/src/format.rs:L48-L56](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-bundle/src/format.rs#L48-L56) 的文档说明）。

测试体系里的 bundle（测试侧同样要走完整链路，但 feature 一律全开）：

[tests/src/world.rs:L210-L222](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/world.rs#L210-L222) 测试库注册五种格式并 `with_features(Features::all())`——所以套件里 bundle 测试不需要任何额外开关。

[tests/src/run.rs:L203-L207](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/run.rs#L203-L207) 只有测试声明了 bundle stage 才 `compile::<Bundle>`（复用同一次求值结果 `evaluated`）。

[tests/src/output.rs:L495-L524](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/output.rs#L495-L524) 测试侧 `make_live` 用**固定选项**（pretty HTML、72ppi PNG、pretty SVG）导出，保证产物确定可比对；参考文件存「哈希清单」或确定性 tar（`save_live`）。

#### 4.4.4 代码实践

**实践目标**：体验「跨文档内省是全局的」这一语义，并顺着 ref 产物理解测试如何固定 VirtualFs。

1. 写一个跨文档计数测试（保存为 `/tmp/bundle-lab/cross.typ`，内容为示例代码）：

   ```typ
   #document("a.pdf")[
     = First
   ]
   #document("b.pdf")[
     = Second
     // context 里 query 全 bundle 的 heading
     #context [#query(heading).len() headings total]
   ]
   ```

2. 编译并观察 `b.pdf` 里的数字：应为 2（两个文档的 heading 全局可见，且 heading 编号跨文档连续）。

   ```bash
   cargo run -p typst-cli -- compile --features bundle --format bundle \
     /tmp/bundle-lab/cross.typ -o /tmp/bundle-lab/out2
   ```

3. 对比 `#context [page numbering: #counter(page).display()]` 在两个文档里的值——页码计数器按文档独立（预期 a 第 1 页、b 第 1 页）。

**需要观察的现象**：第 2 步是 2 而不是 1；第 3 步两份 PDF 的页码互不累加。两者对照恰好体现「全局内省 + 按文档分页」的规则。

**预期结果**：PDF 可打开且内容符合预期；无法直接查看 PDF 时，可改用 `#document("b.html")` 输出 HTML 便于文本核对。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`BundleIntrospector::new` 为什么要接收 `&[Item]`（含 Asset 和 Tag）而不是只接收子文档的 introspector 列表？

**参考答案**：因为 asset 和 document 元素自身也是可定位元素（`AssetElem` 标了 `Locatable`），可以被 `query(asset)` 查到、可以被 `link` 指向。它们的 Tag/Location 信息只存在于 items 里，不在任何子文档的内省器中，所以合并器必须看到完整 items 序列才能建出覆盖「文档 + 资产 + 顶层标签」的统一元素表。

**练习 2**：为什么链接锚生成放在布局之后、导出之前，而不是布局期间顺手做掉？

**参考答案**：锚要覆盖「任何文档链接到任何位置」的全局需求——只有所有子文档都排完（位置、页码、DOM 都定型），`link_targets` 才能枚举出完整的被链接集合。布局期间任何一个文档都看不到其它文档的最终形态。这也是「导出 crate 只读 PagedDocument」边界的有意突破点：bundle 用 `LateLinkResolver` 把链接的最终解析推迟到导出期，代价是导出函数多了一个 resolver 参数。

**练习 3**：`export` 阶段失败（比如某文档 PDF 导出报错）会触发重新布局吗？

**参考答案**：不会。`compile::<Bundle>` 的迭代循环只围绕「内省是否收敛」转（`constraint.validate`），到 `export` 被调用时循环早已结束；导出错误经 `collect_combined_warned_result` 聚合并上抛给 CLI。这与 u4-l1「布局迭代、导出一次性」的阶段划分一致。

## 5. 综合实践

**任务**：手工整理一份《bundle 装配链路图》，把本讲三个模块串成一条从 CLI 参数到最终字节的完整链路，并用 `--timings` 与测试套件各验证一次。

**步骤**：

1. **链路整理**（纯阅读）：按下面清单逐项在源码中找到对应位置，抄录文件与行号，画成一张有向图（节点为函数/结构体，边为调用）：

   - `--features bundle`（args.rs:519-522）→ `with_features`（world.rs:64-75）
   - `typst_bundle::FORMAT`（typst-bundle/format.rs:11-14）→ `LibraryBuilder::build`（typst-library/lib.rs:231-249）
   - `format::module` 挂门（typst-library/format.rs:94-99）→ `prelude_path(["format","bundle","asset"])`（typst-library/lib.rs:430）
   - `--format bundle` → `compile_and_export`（cli/compile.rs:361-362）→ `compile::<Bundle>` → `warn_or_error_for_bundle`（typst/lib.rs:269-287）
   - `Output::create` → `bundle` → `bundle_impl`（typst-bundle/lib.rs:59-229）→ `RealizationKind::Bundle`（routines.rs:151-155）
   - `compile_document` → `determine_format`（model/document.rs:209-227）→ `layout_document_for_bundle`（pages/mod.rs:78-97）→ `layout_document_common`（127-176）
   - `BundleIntrospector::new` + `create_link_anchors`（introspect.rs:37-46、link.rs:20-63）→ `export`（export.rs:26-46）→ `write_virtual_fs`（cli/compile.rs:448-459）

2. **动态验证**：用 4.3.4 的 `--timings` 方法跑 `multi.typ`，把得到的计时事件名逐个标注到上图的节点上；没有计时点的事件（memoize 内层、`collect` 等）标注「不可见」。

3. **交叉验证**：跑 `cargo testit --exact asset-query`（该测试同时用到 `document`、`query` 全局内省与 `asset`，见 [tests/suite/model/asset.typ](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/model/asset.typ) 的 `asset-query` 小节），对照 [tests/ref/bundle/asset-query.txt](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/ref/bundle/asset-query.txt)（两行哈希清单：`main.pdf` 与 `data.json`），确认「一个 bundle 同时产出文档 + 由 context/query 生成的资产」。

**预期产出**：一张标注了源码行号与计时事件名的链路图，外加三组运行记录（错误门禁、警告放行、测试通过）。所有运行动态结果待本地验证。

## 6. 本讲小结

- **双重门控**：`Feature::Bundle` 同时管「库里 `format.bundle`/`asset` 绑定是否可见」与「`compile::<Bundle>` 目标是否放行」；前者在 `format::module` 挂门、由 prelude 带到全局，后者在 `compile_impl` 的 `warn_or_error_for_bundle` 检查——开 feature 只得警告，不开直接报错。
- **注册点反向阅读法**：新格式的全部装配信息浓缩在一个 `FORMAT` 常量里（格式元素 + feature 门 + 规则注册函数），顺着 `LibraryBuilder::build` 的三步（global → rules → formats 字段）即可还原它在标准库和规则表里的落点。
- **bundle 布局是「根级分拣 + 逐文档排版」**：根级 realize 用空的 `BUNDLE_RULES`，顶层只容 `document`/`asset`/Tag；`collect` 查重路径后，`compile_document` 按 `determine_format` 切子目标并用样式链切换 target，再交给 `layout_document_for_bundle`——它与普通 `layout_document` 只差「不重置 locator」，共享 `layout_document_common`。
- **内省是全 bundle 一个环**：所有子文档共用一个 `BundleIntrospector`（query/counter 全局，页码例外按文档），`compile::<Bundle>` 的五轮迭代循环迭代的是整个 bundle；Location 经链式 locator 挂在整棵树下，跨轮稳定。
- **导出是格式聚合**：`typst-bundle` 依赖 pdf/render/svg/html 四个导出 crate，`export()` 并行把结构化子文档翻译成 `VirtualFs`，跨文档链接靠布局后回填锚点 + 导出期 `LateLinkResolver` 延迟解析。
- **一切以源码为准**：该 crate 标注 since unreleased/experimental，`tests/suite/model/asset.typ` 与 `tests/ref/bundle/` 是行为的最权威快照。

## 7. 下一步学习建议

- **下一讲 u5-l5（宏的魔法）**：本讲反复出现的 `#[elem]`、`#[func]`、`cast!`、`#[scope]` 都由 typst-macros 生成，下一讲用 `cargo expand` 看 `AssetElem` 这类元素的展开产物，理解字段与构造代码从何而来。
- **回看 u5-l8（增量编译）**：本讲出现了三处 memoize（`bundle_impl`、`layout_document_for_bundle_impl`、`export_pdf` 等），结合 comemo 一讲思考「bundle 场景下哪层缓存最划算」。
- **延伸阅读**：`tests/suite/model/document.typ`（document 元素行为全集）、`tests/ref/bundle/link-bundle-*.txt`（跨文档链接用例族），以及 `crates/typst-bundle/src/format.rs` 里 `BundleFormat` 的文档注释——它本身就是 bundle 功能的官方使用说明（含 `typst watch` 的 HTTP 服务器参数）。
