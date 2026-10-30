# 字体与包生态：typst-kit 与 CLI 资源管理

## 1. 本讲目标

编译器内核（`typst::compile`）本身不关心字体文件在磁盘的哪里、包从网上怎么下载——它只通过 `World` 抽象提出「给我字体」「给我包文件」的请求。真正回答这些请求的，是宿主侧的资源管理层。本讲聚焦其中最完整的一份实现：**typst-kit** 及其在 typst-cli 中的接线。学完后你应该能够：

1. 说清 `FontStore` 的惰性加载模型，以及「系统字体 → 嵌入字体 → `--font-path` 目录」三级发现顺序。
2. 描述 `Downloader` trait 的抽象契约（尤其是 404 → `NotFound` 的约定）与 `ProgressDownloader` 装饰器的进度上报机制。
3. 讲出包解析的三级优先级（本地 data 目录 → 本地 cache 目录 → Typst Universe 下载）、「命名空间/包名/版本」的目录布局，以及下载落盘的原子化策略。
4. 独立配置自定义字体目录与包缓存目录，并追踪到源码中对应的位置。

## 2. 前置知识

- **World 契约（回顾 u3-l1）**：`World` trait 中与资源相关的方法有 `book()`（字体元数据簿）、`font(index)`（按索引取字体）、`source(id)` / `file(id)`（按文件 ID 取源码/字节）。编译器只调用这些方法，不关心背后是文件系统、网络还是内存。
- **FileId 与 VirtualRoot（回顾 u2-l1）**：每个文件由 2 字节的 `FileId` 句柄标识，它的根要么是 `VirtualRoot::Project`（项目根），要么是 `VirtualRoot::Package(spec)`（某个包的根）。本讲会看到：当根是包时，文件加载会一路走到 `SystemPackages::obtain`。
- **惰性初始化**：`OnceLock` 是「首次访问时计算一次、之后永远复用」的单次写容器；`LazyLock` 是其「连计算函数都延迟到首次访问」的变体。字体发现和包索引都靠它们推迟昂贵的 IO。
- **trait 对象与 `dyn Any`**：`Box<dyn FontSource>`、`&dyn Any` 允许把不同类型擦除后传递，再用 `downcast_ref::<T>()` 在运行时还原具体类型。下载器的「进度 key」机制就建立在这上面。
- **fontdb**：一个第三方 Rust 字体数据库库，负责枚举目录与系统字体源、解析字体元数据（家族名、样式、字重等），Typst 用它做字体「发现」，但不用它做字形渲染。
- **tar.gz 与原子落盘**：Universe 的包是 gzip 压缩的 tar 归档。多进程并发写同一目录时，惯用策略是「先写临时目录、再 `rename`」——`rename` 在同一文件系统上是原子操作，失败可观测，不会留下半个包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-kit/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/lib.rs) | typst-kit 的门面：crate 定位说明与全部特性门控清单 |
| [crates/typst-kit/src/fonts.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs) | `FontStore`、`FontSource` 与 `embedded` / `system` / `scan` 三个字体来源 |
| [crates/typst-kit/src/downloader.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs) | `Downloader` trait、`SystemDownloader`（ureq + native-tls）与 `ProgressDownloader` 进度装饰器 |
| [crates/typst-kit/src/packages.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs) | `SystemPackages` / `FsPackages` / `UniversePackages`：三级包来源与缓存落盘 |
| [crates/typst-kit/src/files.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs) | `FileLoader` trait、`FileStore` 缓存与 `FsRoot`；包根 `FsRoot` 在这里与文件加载衔接 |
| [crates/typst-cli/src/fonts.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs) | CLI 侧 `discover_fonts`（发现顺序的最终裁定处）与 `typst fonts` 命令 |
| [crates/typst-cli/src/download.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs) | CLI 的下载器工厂：`SystemDownloader` + `ProgressDownloader` 组装与终端进度条 |
| [crates/typst-cli/src/packages.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/packages.rs) | CLI 的包存储工厂：把 `--package-path` / `--package-cache-path` 参数翻译成 `SystemPackages` |
| [crates/typst-cli/src/world.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs) | `SystemWorld`：字体与包如何接入 `World` 的 `book` / `font` / `source` / `file` |
| [crates/typst-cli/src/args.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs) | `FontArgs`、`PackageArgs` 与 `--cert` 的 clap 定义（含环境变量回退） |

## 4. 核心概念与源码讲解

### 4.1 typst-kit 的定位与特性门控

#### 4.1.1 概念说明

typst-kit 是给「Typst 集成者」（CLI、编辑器插件、Web 服务等宿主）准备的百宝箱。它的文档注释开宗明义：自己是字体搜索、包加载等事项的 **single source of truth（唯一事实来源）**——也就是说，官方希望所有宿主都复用这套实现，而不是各自发明字体发现逻辑。

这个 crate 最重要的设计是**重度特性门控**：默认一个特性都不开（`default = []`），每块功能及其带来的第三方依赖都可以单独启用。这让 WebAssembly 里的小型集成可以只拿字体惰性加载、而完全不链接 TLS 栈。

#### 4.1.2 核心流程

依赖被启用与否的传导关系（在 Cargo.toml 的 `[features]` 中声明）：

```text
scan-fonts        → fontdb (memmap, fontconfig) + dirs
embedded-fonts    → typst-assets/fonts
universe-packages → flate2 + tar + fastrand
system-packages   → dirs + universe-packages
system-downloader → env_proxy + native-tls + ureq + openssl
system-files      → system-packages
```

typst-cli 对 typst-kit 的依赖直接启用了一揽子特性（`bundle`、`embedded-fonts`、`scan-fonts`、`system-packages`、`universe-packages` 等），因此我们平时用的 `typst` 命令具备完整的系统字体扫描与联网下载能力。

#### 4.1.3 源码精读

- [crates/typst-kit/src/lib.rs:1-4](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/lib.rs#L1-L4)：crate 文档，声明自己是宿主集成的唯一事实来源，包含 `typst-cli` 所用的各类实现。
- [crates/typst-kit/src/lib.rs:6-31](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/lib.rs#L6-L31)：全部特性门控清单：`embedded-fonts`（嵌入字体）、`scan-fonts`（路径/系统字体发现）、`system-files`、`system-packages`、`universe-packages`（Universe 下载）、`system-downloader`（网络请求）、`watcher`、`timer`、`http-server` 等。
- [crates/typst-kit/Cargo.toml:48-92](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/Cargo.toml#L48-L92)：`[features]` 段，`default = []` 加上每条特性拉入的可选依赖，与上面的传导图一一对应。
- [crates/typst-cli/Cargo.toml:59-66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/Cargo.toml#L59-L66)：CLI 对 typst-kit 的依赖声明，`features = [...]` 启用全套能力。
- [crates/typst-cli/Cargo.toml:91-98](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/Cargo.toml#L91-L98)：CLI 自身的 `default = ["embedded-fonts", "http-server"]`——发行版二进制默认带嵌入字体。

#### 4.1.4 代码实践

1. **实践目标**：直观感受特性门控对 API 可见性的影响。
2. **操作步骤**：在本地仓库执行 `cargo doc -p typst-kit --no-deps --open`，浏览 `fonts`、`packages`、`downloader` 三个模块；再对比 `cargo tree -p typst-kit -e features --depth 1` 的输出，确认默认构建下几乎没有第三方依赖。
3. **需要观察的现象**：文档中 `embedded`、`system`、`scan` 等函数是否显示（docs.rs 构建下带特性，本地 `cargo doc` 默认无特性时这些条目可能缺失）。
4. **预期结果**：理解「特性 = 可选依赖 + 可选 API」的一一对应关系。
5. 具体显示效果**待本地验证**（取决于本地 cargo doc 的特性配置）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 typst-kit 默认不启用任何特性？
**答案**：它的用户是各类宿主（包括 WASM 环境），不同宿主需要的依赖面差异巨大；默认全关可以让集成者精确裁剪二进制体积，例如纯内存 World 不需要 fontdb，沙箱环境不需要 TLS。

**练习 2**：`system-packages` 特性为什么连带启用 `universe-packages`？
**答案**：见 [crates/typst-kit/Cargo.toml:64-66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/Cargo.toml#L64-L66)：`SystemPackages` 的 `obtain` 在本地目录未命中时要尝试从 Universe 下载，该逻辑在 `UniversePackages` 里，依赖 flate2/tar/fastrand，因此必须连带启用。

### 4.2 字体发现：FontStore 与三个字体来源

#### 4.2.1 概念说明

排版需要字体，但「找到磁盘上所有字体」与「加载字体到内存」是两件成本悬殊的事：前者要遍历目录、解析每个文件的元数据；后者要把整个字体文件读进内存并解析字形表。Typst 的解法是把两者拆开：

- **发现（discover）**：只收集「路径 + 元数据」（家族名、变体、可变轴），结果登记进 `FontBook`；
- **加载（load）**：排版真正用到某个字体时才读文件，且每个字体至多加载一次。

`FontStore` 就是这个模型的容器：一本元数据簿加一排「惰性槽位」。它的 `book()` 与 `font(index)` 两个方法的文档注释明确写着：可以直接用来实现 `World::book` 与 `World::font`。

#### 4.2.2 核心流程

字体来源有三个（对应三个顶层函数）：

1. `fonts::system()`：操作系统标准字体位置（Linux 走 fontconfig 等，由 fontdb 决定）；
2. `fonts::embedded()`：编译进二进制的官方字体（Libertinus Serif、New Computer Modern、NCM Math、Deja Vu Sans Mono）；
3. `fonts::scan(path)`：用户指定的目录，递归扫描。

CLI 的装配顺序（`discover_fonts`）为：

```text
FontStore::new()
  ├─ 若未 --ignore-system-fonts   → extend(fonts::system())
  ├─ 若未 --ignore-embedded-fonts → extend(fonts::embedded())   # 编译期特性门控
  └─ 依序遍历 --font-path 各目录   → extend(fonts::scan(path))
```

发现顺序决定字体在 `FontBook` 中的索引顺序；排版时的字体选择（`FontBook::select`）按家族名查表、再在候选索引中挑最匹配变体的那一个。

单次加载的生命周期：

```text
World::font(index)
  → FontStore::font(index)
    → FontSlot::get()
      → OnceLock::get_or_init(source.load())   # 首次才执行
        → FontPath::load() = fs::read + Font::new(data, index)
      → 之后所有访问直接克隆缓存的 Font
```

#### 4.2.3 源码精读

- [crates/typst-kit/src/fonts.rs:24-27](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L24-L27)：`FontStore` 的两个字段——`LazyHash<FontBook>`（可哈希的元数据簿，供 comemo 缓存）与 `Vec<FontSlot>`（惰性槽位）。
- [crates/typst-kit/src/fonts.rs:39-53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L39-L53)：`push` / `extend`——登记一条（来源, 元数据）记录；文档注明字体按加入顺序编号，索引与 `book` 一致。
- [crates/typst-kit/src/fonts.rs:59-77](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L59-L77)：`book()` 与 `font(index)`，注释明说可直接对接 `World::book` / `World::font`；`source(index)` 供 CLI 反查字体来源。
- [crates/typst-kit/src/fonts.rs:86-97](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L86-L97)：`FontSlot` 与其 `get`——`OnceLock<Option<Font>>` 保证「至多加载一次」，连加载失败（`None`）也会被记住。
- [crates/typst-kit/src/fonts.rs:100-117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L100-L117)：`FontSource` trait 与 `FontPath` 的实现——`load` 才执行 `fs::read` 并按集合索引构造 `Font`，附带 `TimingScope` 计时打点（与 u4-l1 讲的计时体系同源）。
- [crates/typst-kit/src/fonts.rs:120-127](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L120-L127)：`FontPath { path, index }`——一个 `.ttc` 集合文件里可能有多支字体，`index` 指明取第几支。
- [crates/typst-kit/src/fonts.rs:134-142](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L134-L142)：`embedded()`——从 `typst_assets::fonts()` 取嵌入数据，逐个产出 `(Font, FontInfo)`；注意它产出的是已加载的 `Font` 本身（嵌入字体无需惰性 IO）。
- [crates/typst-kit/src/fonts.rs:147-166](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L147-L166)：`system()` 与 `scan(path)`——都委托 `with_db`，前者让 fontdb 加载系统字体，后者加载指定目录（递归）。
- [crates/typst-kit/src/fonts.rs:169-194](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L169-L194)：`with_db`——建临时 `fontdb::Database`，遍历每个 face，过滤出文件来源，用 `FontInfo::new` 解析元数据，产出 `(FontPath, FontInfo)` 序列。
- [crates/typst-kit/src/fonts.rs:203-227](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L203-L227)：`load_adobe_fonts`——仅 Windows/macOS，额外扫描 Adobe 的 Creative Cloud 同步字体目录（注释里给出了 Adobe 条款依据）。
- [crates/typst-cli/src/fonts.rs:37-55](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L37-L55)：**发现顺序的最终裁定处** `discover_fonts`——系统字体、嵌入字体、`--font-path` 目录依序 extend；函数整体被 `#[typst_macros::time(name = "discover fonts")]` 包裹，可用 `--timings` 观测。
- [crates/typst-cli/src/world.rs:31-38](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L31-L38) 与 [crates/typst-cli/src/world.rs:88-90](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L88-L90)：`SystemWorld` 的 `fonts` 字段是 `LazyLock<FontStore, ...>`——连「发现」本身都推迟到第一次真正需要字体时；[crates/typst-cli/src/world.rs:118-123](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L118-L123) 的 `scan_fonts()` 提供强制立即扫描的入口（`typst fonts` 命令用它）。
- [crates/typst-cli/src/world.rs:131-149](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L131-L149)：`World::book` / `World::font` 的实现就是一行委托——`FontStore` 文档承诺的「直接对接」在这里兑现。
- [crates/typst-cli/src/fonts.rs:13-34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L13-L34)：`typst fonts` 命令——遍历 `book().families()` 打印家族名；`--variants` 时逐变体打印路径（把 `source` 向下转换回 `FontPath` 拿到路径；嵌入字体转换失败则显示 `(Embedded)`）。
- [crates/typst-library/src/text/font/book.rs:75-78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L75-L78)：`FontBook::select`——按家族名取索引列表，再挑最接近请求变体的字体；这就是发现顺序最终影响排版的地方。

#### 4.2.4 代码实践

**实践：自定义字体目录的发现与使用验证**

1. **实践目标**：验证 `fonts::scan` 能发现自定义目录中的字体，且 `typst fonts` 与编译输出都能证明它被发现、被使用。
2. **操作步骤**：
   1. 在仓库外建一个实验目录（例如 `~/typst-font-lab/`），在其中新建 `myfonts/`，放入任意一个你有权使用的 `.ttf` / `.otf` 字体文件（例如从系统字体目录复制一份）；
   2. 记下该字体的家族名（可先用 `fc-scan 文件名` 或字体查看工具确认；若无法确认，第 3 步的输出会告诉你）；
   3. 运行（二选一，前者要求已 `cargo build -p typst-cli`）：
      ```bash
      cargo run -p typst-cli -- fonts --font-path ~/typst-font-lab/myfonts --variants
      # 或已安装的 CLI：typst fonts --font-path ~/typst-font-lab/myfonts --variants
      ```
   4. 编写 `hello-font.typ`（**示例代码**）：
      ```typst
      #set text(font: "你的字体家族名")
      Hello, custom font!
      ```
      用同样的 `--font-path` 参数编译：
      ```bash
      cargo run -p typst-cli -- compile --font-path ~/typst-font-lab/myfonts hello-font.typ
      ```
   5. 对照源码确认路径枚举位置：`--font-path` 的值经 [crates/typst-cli/src/args.rs:546-559](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L546-L559) 的 `FontArgs.font_paths`（环境变量 `TYPST_FONT_PATHS`，多路径按系统分隔符切分），在 [crates/typst-cli/src/fonts.rs:50-52](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L50-L52) 被逐个交给 `fonts::scan`，最终进入 [crates/typst-kit/src/fonts.rs:163-166](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L163-L166)。
3. **需要观察的现象**：`fonts` 子命令输出里出现你的字体家族名，`--variants` 列出的路径正是你放入的文件；编译后的 PDF 用了该字体。
4. **预期结果**：家族列表含自定义字体；若家族名写错，编译时 Typst 会报「unknown font family」类警告并回退默认字体——这本身也是一次验证。
5. 编译输出的具体内容**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FontStore` 要同时持有 `LazyHash<FontBook>` 而不是普通 `FontBook`？
**答案**：`World::book` 的返回类型就是 `&LazyHash<FontBook>`（u3-l1 讲过 World 契约）。`LazyHash` 把「内容哈希」也做了惰性缓存，comemo 才能以哈希判断两次编译之间字体环境是否变化，从而复用排版缓存。

**练习 2**：`fonts::embedded()` 返回 `(Font, FontInfo)`，而 `system()` / `scan()` 返回 `(FontPath, FontInfo)`，为什么类型不同？
**答案**：见 [crates/typst-kit/src/fonts.rs:100-117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L100-L117)。嵌入字体的数据已经在二进制里，直接给出加载好的 `Font`（其 `load` 实现只是克隆）；文件系统字体则只给 `FontPath`，把 `fs::read` 推迟到 `FontSlot` 的 `OnceLock` 首次初始化——用不到的字体永远不读盘。

**练习 3**：`typst fonts --variants` 如何区分某行字体是嵌入的还是磁盘上的？
**答案**：[crates/typst-cli/src/fonts.rs:22-25](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L22-L25) 把 `FontStore::source(index)` 向下转换为 `FontPath`：成功则打印文件路径，失败（嵌入字体的来源是 `Font`）则打印 `(Embedded)`（[crates/typst-cli/src/fonts.rs:64-67](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L64-L67)）。

### 4.3 下载器抽象：Downloader trait 与进度装饰器

#### 4.3.1 概念说明

包下载是 typst-kit 中唯一需要联网的能力。为了不把网络栈焊死在包逻辑里，kit 定义了一个极小的 `Downloader` trait：给定 URL，返回「大小提示 + 字节流读取器」。所有可能触发下载的功能（目前是 Universe 包与包索引）都经由它，于是：

- 沙箱宿主可以提供自己的实现（比如走代理、走内存缓存、直接拒绝联网）；
- kit 自带一个 `SystemDownloader`（ureq + 系统 TLS）作为默认实现；
- 任何实现都能被 `ProgressDownloader` 包装，获得进度上报——这是典型的**装饰器模式**。

trait 还有一条关键**错误约定**：远端返回 404 时必须映射为 `io::ErrorKind::NotFound` 的错误。包加载方靠它区分「版本不存在」与「网络故障」，进而给出「建议使用最新版 X.Y.Z」的精准错误。

另一个精巧设计是下载的 `key: &dyn Any` 参数：它不参与请求，只作为「这次下载是什么」的身份标签传给进度报告器，由报告者自行决定显示什么。

#### 4.3.2 核心流程

一次带进度的包下载：

```text
UniversePackages::package(spec)
  └─ downloader.download(key = spec, url)         # key 是 PackageSpec
       └─ ProgressDownloader::download             # 装饰器拦截
            ├─ inner.stream(key, url)              # SystemDownloader 发起 HTTPS GET
            ├─ progress 工厂函数(key)               # CLI 在这里 downcast 决定是否显示
            └─ ProgressReader 分块读（8 KiB/块）
                 ├─ 每 100ms 采样一次速率（滑动窗口 25 个样本）
                 └─ start / update / finish 回调 ProgressReporter
```

错误映射约定：

```text
HTTP 404                    → io::ErrorKind::NotFound
其他 ureq/tls/IO 错误        → 普通错误 → PackageError::NetworkFailed
NotFound 且能查到最新版本    → PackageError::VersionNotFound(spec, latest)
```

#### 4.3.3 源码精读

- [crates/typst-kit/src/downloader.rs:27-56](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L27-L56)：`Downloader` trait——必选的 `stream` 与基于 `stream` 提供默认实现的 `download`；文档写明 404 → `NotFound` 的约定与 `key` 参数的用途。
- [crates/typst-kit/src/downloader.rs:86-126](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L86-L126)：`SystemDownloader`——持有 user agent、可选自定义证书（`with_cert_path` 的 PEM 在首次访问时经 `OnceCell` 惰性加载）。
- [crates/typst-kit/src/downloader.rs:160-201](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L160-L201)：`Downloader` for `SystemDownloader` 的实现——用 ureq 构建 GET 请求；从环境变量读代理配置（`env_proxy`）；注入自定义 CA 证书；[crates/typst-kit/src/downloader.rs:190-193](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L190-L193) 把 `ureq::Error::Status(404, _)` 翻译成 `ErrorKind::NotFound`，兑现 trait 契约；`Content-Length` 头作为大小提示返回。
- [crates/typst-kit/src/downloader.rs:212-232](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L212-L232)：`ProgressDownloader<T, F, P>`——包装内层下载器 `T`、进度工厂 `F` 与报告器 `P`。
- [crates/typst-kit/src/downloader.rs:255-277](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L255-L277)：装饰器对两个方法的实现——`download` 走 `stream` + `ProgressReader`（边下边报），`stream` 反而走 `download` + `Cursor`（整体下完再交给读取器）。二者语义等价，只是进度呈现时机不同。
- [crates/typst-kit/src/downloader.rs:292-301](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L292-L301)：`ProgressReporter` trait 的 `start` / `update` / `finish` 三回调；[crates/typst-kit/src/downloader.rs:438-489](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L438-L489) 的 `ProgressReader::download` 以 8 KiB 块循环读取、按周期采样速率并驱动回调（源码注明实现参考了 rustup 的 `DownloadTracker`）。
- [crates/typst-cli/src/download.rs:15-32](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs#L15-L32)：CLI 的下载器工厂——user agent 是 `typst/<版本>`；`--cert`（环境变量 `TYPST_CERT`，见 [crates/typst-cli/src/args.rs:73-75](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L73-L75)）存在时改用 `with_cert_path`；最后包上 `ProgressDownloader`，工厂闭包对 key 做向下转换：`PackageSpec` 显示包名、`"release"` 显示（自更新场景）、其余（如 `"package index"`）静默。
- [crates/typst-cli/src/download.rs:36-63](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs#L36-L63)：`PrintProgress`——`start` 打印带颜色的 `downloading {name}`，`update` 清掉最后一行重写进度（`Progress` 的 `Display` 输出 `已下 / 总量 (百分比), 速率/s, ETA`）。

#### 4.3.4 代码实践

**实践：观察一次真实下载的进度输出与 key 过滤**

1. **实践目标**：把 4.3.2 的流程图与真实终端行为对上号。
2. **操作步骤**：
   1. 把包缓存指到空目录以强制下载（环境变量 `TYPST_PACKAGE_CACHE_PATH`，见 4.4.3）：
      ```bash
      mkdir -p ~/typst-pkg-lab/cache
      export TYPST_PACKAGE_CACHE_PATH=~/typst-pkg-lab/cache
      ```
   2. 写 `use-pkg.typ`（**示例代码**，版本号请以 Universe 实际可用的为准）：
      ```typst
      #import "@preview/cetz:0.3.4"
      Hello #cetz.canvas({ draw.circle((0,0), radius: (1,1)); })
      ```
   3. 编译并注视终端：`cargo run -p typst-cli -- compile use-pkg.typ`。
   4. 立即再编译一次。
3. **需要观察的现象**：第一次应出现 `downloading @preview/cetz:0.3.4` 与不断刷新的进度行；下载包索引（key 为 `"package index"`）时**没有**对应进度行——这正是 [crates/typst-cli/src/download.rs:22-30](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs#L22-L30) 的 key 过滤在起作用；第二次编译不再有任何下载输出。
4. **预期结果**：包 tar.gz 下载进度可见、索引下载静默、缓存命中后零网络请求。
5. 具体输出格式与包体积**待本地验证**（取决于网络环境与所选包）。

#### 4.3.5 小练习与答案

**练习 1**：如果宿主环境必须经公司代理且使用自签 CA 证书，CLI 用户怎么办？
**答案**：代理无需配置——`SystemDownloader` 自动读环境变量（[crates/typst-kit/src/downloader.rs:173-179](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L173-L179)）；自签证书用 `--cert /path/to/ca.pem` 或环境变量 `TYPST_CERT`，在 [crates/typst-cli/src/download.rs:17-20](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs#L17-L20) 被接入 `with_cert_path`，PEM 首次使用时加载。

**练习 2**：为什么 `Downloader` 一定要约定「404 → `ErrorKind::NotFound`」？
**答案**：[crates/typst-kit/src/packages.rs:375-380](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L375-L380) 依赖这个错误类别分流：`NotFound` 时去查包索引拿最新版本、产出 `VersionNotFound(spec, latest)`（提示用户应使用的版本），其他错误一律归为 `NetworkFailed`。没有这条约定，「版本写错」与「断网」就无法给出不同的诊断。

**练习 3**：`ProgressDownloader` 的 `stream` 实现为什么「先整包下载再包成 `Cursor`」，而不是直接透传内层的流？
**答案**：进度统计需要知道已下载字节与总量的完整轨迹，而 `stream` 的调用方拿到读取器后会自行控制读取节奏。装饰器在 [crates/typst-kit/src/downloader.rs:269-277](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/downloader.rs#L269-L277) 选择在内层 `download` 完整走完进度流程后，把结果字节包成内存 `Cursor` 返回，保证进度语义不丢。

### 4.4 包解析与缓存：SystemPackages 三级优先

#### 4.4.1 概念说明

`#import "@preview/cetz:0.3.4"` 里的 `@preview/cetz:0.3.4` 是一个 `PackageSpec`（命名空间/包名/版本，回顾 u2-l1 的 package.rs）。编译器遇到它时只是构造一个根为 `VirtualRoot::Package(spec)` 的 `FileId`，然后照常调 `world.source(id)`——**解析这个根指向哪片磁盘，完全是宿主的事**。typst-kit 的 `SystemPackages` 给出的答案是三级优先：

1. **data 目录**：用户手动放置包的系统级位置（如 `~/.local/share/typst/packages`）；
2. **cache 目录**：自动下载包的缓存位置（如 `~/.cache/typst/packages`）；
3. **Typst Universe**：官方注册表 `https://packages.typst.org`——仅当命名空间是 `preview` 时才尝试，下载后解包进 cache 目录再复用。

目录布局有严格约定：`data`/`cache` 根下第一层是命名空间、第二层是包名、第三层是版本号。这既是查找规则，也是你手动安装本地包时要遵循的格式。

#### 4.4.2 核心流程

`SystemPackages::obtain(spec)` 的完整决策树：

```text
obtain(spec)
  ├─ data 目录有 spec？        → 返回该目录作为包根 FsRoot
  ├─ cache 目录有 spec？       → 返回
  ├─ spec.namespace == "preview"
  │    ├─ universe.package(spec)   # GET {url}/preview/{name}-{version}.tar.gz
  │    ├─ cache.store(spec, 解包)  # 临时目录 → rename 原子落盘
  │    └─ cache.obtain(spec)       # 再查一次，成功返回
  └─ 全部落空 → PackageError::NotFound(spec)
```

下载一个不存在的版本时：

```text
HTTP 404 → 查 index.json 得最新版本 latest
        → PackageError::VersionNotFound(spec, latest)   # 错误信息会建议 latest
```

原子落盘的并发安全策略（`FsPackages::store`）：

```text
base_dir/…/{version}/           ← 最终位置
base_dir/…/.tmp-{version}-{随机}/  ← 先解包到这里（与最终位置同目录 ⇒ 同挂载点）
write(tempdir)                   ← 缓慢、可中断的阶段只发生在临时目录
rename(tempdir, package_dir)     ← 原子一步
  └─ 若报 DirectoryNotEmpty     ← 另一个进程已装好 ⇒ 视为成功
Drop(tempdir)                    ← 任何失败路径都自动清理临时目录
```

`#import` 到字节的完整调用链（CLI 接线后）：

```text
#import "@preview/x:1.0"
  → typst-eval 构造 FileId（根 = VirtualRoot::Package(spec)）
  → World::source(id) → SystemWorld::source → FileStore（未命中）
  → CLI SystemFiles::load（world.rs）→ root(id)
  → match id.root():
       Project        → 项目 FsRoot
       Package(spec)  → SystemPackages::obtain(spec)   ← 本讲主角（可能触发下载）
  → FsRoot::load(vpath) 读出字节 → 封装 Source → 回到求值器
```

#### 4.4.3 源码精读

- [crates/typst-kit/src/packages.rs:20-39](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L20-L39)：模块文档与 `SystemPackages` 结构——`data` / `cache` / `universe` 三部分；文档即三级优先级清单。
- [crates/typst-kit/src/packages.rs:52-67](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L52-L67)：`new(downloader)` 用系统默认目录组装；`from_parts` 支持完全自定义（CLI 正是用它接命令行参数）。
- [crates/typst-kit/src/packages.rs:95-124](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L95-L124)：`obtain` 的完整实现，与上面的决策树逐行对应；注释说明并发下载不会损坏（但有锁更高效），`FileStore` 已在加载期间持锁。
- [crates/typst-kit/src/packages.rs:145-158](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L145-L158)：`FsPackages` 的目录约定文档——三层目录分别对应命名空间、包名、版本。
- [crates/typst-kit/src/packages.rs:163-182](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L163-L182)：`system_data()` 与 `system_cache()` 的跨平台路径表（Linux / macOS / Windows 三套，Linux 遵循 XDG）。
- [crates/typst-kit/src/packages.rs:191-198](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L191-L198)：`FsPackages::obtain`——拼出 `命名空间/包名/版本` 路径，存在即返回 `FsRoot`。
- [crates/typst-kit/src/packages.rs:202-217](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L202-L217)：`latest_version`——读包名目录下所有能解析成版本的子目录取最大值；只对 data 目录使用（cache 不算用户本地包）。
- [crates/typst-kit/src/packages.rs:227-281](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L227-L281)：`store`——临时目录 + `rename` 的原子落盘全流程，注释详细解释了「临时目录放在最终位置旁边以保证同挂载点」与「`DirectoryNotEmpty` 视为他人已装好」两个关键决定。
- [crates/typst-kit/src/packages.rs:284-310](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L284-L310)：`Tempdir` 及其 `Drop` 实现——失败路径自动 `remove_dir_all`，不留垃圾。
- [crates/typst-kit/src/packages.rs:316-344](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L316-L344)：`UniversePackages`——注册表 URL、下载器与 `OnceCell` 包索引三个成员；默认 URL `https://packages.typst.org`，可用 `with_url` 指向镜像。
- [crates/typst-kit/src/packages.rs:354-383](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L354-L383)：`package()`——拼出 `{url}/preview/{name}-{version}.tar.gz` 下载，gzip 解压成 `tar::Archive` 返回；404 时借 `latest_version` 升级错误为 `VersionNotFound`。
- [crates/typst-kit/src/packages.rs:388-441](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L388-L441)：`latest_version` 与 `index()`——包索引 `{url}/preview/index.json` 只下载一次（`OnceCell` 缓存）；索引用 `serde_json::Value` 存原始条目、查询时逐条惰性反序列化，无法解析的条目（如版本是 `0.2.0-dev` 这种非法值）被跳过而不是炸掉整个索引——[crates/typst-kit/src/packages.rs:453-503](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L453-L503) 的单元测试 `lazy_deserialize_index` 专门验证这一行为。
- [crates/typst-kit/src/packages.rs:127-142](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L127-L142)：`SystemPackages::latest_version` 的分流——`preview` 命名空间问注册表，其他命名空间只搜本地 data 目录，找不到提示「请指明版本」。
- [crates/typst-cli/src/packages.rs:6-18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/packages.rs#L6-L18)：CLI 工厂 `system()`——`--package-path` 有值就用它建 `FsPackages`，否则回退 `system_data()`；`--package-cache-path` 同理回退 `system_cache()`；universe 部分用 [crates/typst-cli/src/download.rs:15-32](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/download.rs#L15-L32) 的下载器。参数定义在 [crates/typst-cli/src/args.rs:529-543](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L529-L543)，对应环境变量 `TYPST_PACKAGE_PATH` / `TYPST_PACKAGE_CACHE_PATH`。
- [crates/typst-cli/src/world.rs:196-200](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L196-L200) 与 [crates/typst-cli/src/world.rs:248-266](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L248-L266)：CLI 自己的 `SystemFiles`（比 kit 版多一个 `main` 文件追踪），`root(id)` 对 `VirtualRoot::Package(spec)` 调 `packages.obtain(spec)`——包解析挂进文件加载链路的确切位置。
- [crates/typst-kit/src/files.rs:256-266](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L256-L266)：`FileLoader` trait——文档建议按 `id.root()` 分流项目/包；kit 还提供了可复用的 `SystemFiles`（[crates/typst-kit/src/files.rs:288-322](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L288-L322)），CLI 的实现即以它为范本。
- [crates/typst-library/src/diag.rs:870-873](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/diag.rs#L870-L873)：`PackageError` 枚举起点——`NotFound` / `VersionNotFound` / `NetworkFailed` / `MalformedArchive` 等变体与本讲各错误路径一一对应，最终都由 CLI 的诊断系统渲染给用户。

#### 4.4.4 代码实践

**实践：手动布一个本地包，验证三级查找与缓存**

1. **实践目标**：亲手按目录约定放置一个本地包，验证 data 目录命中优先于网络；再观察一次真实下载在 cache 目录留下的布局。
2. **操作步骤**：
   1. 建一个假包（**示例代码**）：
     ```bash
     export TYPST_PACKAGE_PATH=~/typst-pkg-lab/data
     mkdir -p ~/typst-pkg-lab/data/local/hello/0.1.0
     cat > ~/typst-pkg-lab/data/local/hello/0.1.0/lib.typ <<'EOF'
     #let greet(name) = [Hello, #name! (from local package)]
     EOF
     ```
   2. 写 `use-local.typ`（**示例代码**）：
     ```typst
     #import "@local/hello:0.1.0": greet
     #greet("Typst")
     ```
   3. 编译：`cargo run -p typst-cli -- compile use-local.typ`。注意命名空间是 `@local` 不是 `@preview`——它**永远不会**触发下载（`obtain` 只对 `preview` 尝试网络）。
   4. 再做网络侧验证（复用 4.3.4 的步骤）：下载一个 `@preview` 包后，执行
      ```bash
      find $TYPST_PACKAGE_CACHE_PATH -maxdepth 3 -type d
      ```
      对照 [crates/typst-kit/src/packages.rs:145-151](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L145-L151) 的三层布局检查目录结构。
   5. 源码侧定位：CLI 参数如何变成目录——[crates/typst-cli/src/packages.rs:8-15](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/packages.rs#L8-L15) 的两级 `map(FsPackages::new).or_else(系统默认)`。
3. **需要观察的现象**：假包编译成功且输出含 `(from local package)`；`find` 显示 `preview/包名/版本` 的三层结构；整个过程中 `TYPST_PACKAGE_CACHE_PATH` 之外没有任何新目录。
4. **预期结果**：本地包走 data 目录零网络；下载包落入 cache 目录且布局与源码约定一致。
5. 编译输出与目录清单**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么下载要「先解包到 `.tmp-` 开头的临时目录再 rename」，而不是直接解包到最终目录？
**答案**：见 [crates/typst-kit/src/packages.rs:243-280](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L243-L280)。解包是缓慢、可中断的过程，直接写最终目录会让并发运行的另一个 Typst 实例读到「半个包」。临时目录与最终位置同处一个父目录（同挂载点保证 `rename` 可用），全部写完再原子 `rename`；若目标已被别人装好（`DirectoryNotEmpty`），本次写入作废、视为成功。

**练习 2**：用户写 `#import "@preview/froz:99.0.0"`（不存在的版本），错误信息里的「建议版本」从哪来？
**答案**：下载返回 404 → [crates/typst-kit/src/packages.rs:375-380](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L375-L380) 捕获 `NotFound`，调用 `latest_version` 从包索引（`index.json`，`OnceCell` 只下载一次）里取该包的最大版本，包装成 `PackageError::VersionNotFound`。

**练习 3**：`@local` 命名空间的包能否通过 `SystemPackages` 下载？如何确定它的最新版本？
**答案**：不能——`UniversePackages` 只服务 `preview` 命名空间（`obtain` 中 [crates/typst-kit/src/packages.rs:108](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L108) 有显式判断）；`latest_version` 对其他命名空间只扫描本地 **data** 目录（cache 不算），完全没有则报「please specify the desired version」（[crates/typst-kit/src/packages.rs:127-142](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L127-L142)）。

## 5. 综合实践

**任务：为一份文档搭建完全可控的资源环境，并逐点在源码中找到依据。**

场景：假设你要在一台无系统字体权限、且需离线复现的构建机上编译一份文档。请完成：

1. **字体侧**：
   - 准备目录 `lab/fonts/`，放入文档所需字体；
   - 用 `--ignore-system-fonts --font-path lab/fonts` 让发现结果只含你的字体与嵌入字体，用 `typst fonts --variants` 截图/记录输出；
   - 在源码中标注三个依据点：发现顺序（[crates/typst-cli/src/fonts.rs:41-52](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/fonts.rs#L41-L52)）、目录递归扫描（[crates/typst-kit/src/fonts.rs:162-166](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L162-L166)）、惰性加载（[crates/typst-kit/src/fonts.rs:86-97](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/fonts.rs#L86-L97)）。
2. **包侧**：
   - 在联网环境用 `TYPST_PACKAGE_CACHE_PATH=lab/cache` 编译一次带 `@preview` 依赖的文档，让依赖全部落入 `lab/cache`；
   - 把 `lab/` 整体拷到离线机器，再次编译，验证零网络请求（终端无 `downloading` 输出）；
   - 在源码中标注三个依据点：cache 命中先于下载（[crates/typst-kit/src/packages.rs:102-121](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L102-L121)）、目录布局（[crates/typst-kit/src/packages.rs:145-151](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/packages.rs#L145-L151)）、包根接入文件加载链（[crates/typst-cli/src/world.rs:261-266](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L261-L266)）。
3. **产出**：一份「资源环境说明」——列出字体清单、包清单、用到的全部 CLI 参数与环境变量，以及每个行为对应的源码位置。

预期：离线第二次编译与联网首次编译产物一致；说明文档里的每个论断都能点开一个永久链接自证。（具体产物**待本地验证**。）

## 6. 本讲小结

- typst-kit 是宿主集成的「唯一事实来源」库，全部能力经特性门控按需启用，默认零依赖；CLI 启用了全套。
- 字体分「发现」与「加载」两阶段：`FontStore` 用 `LazyHash<FontBook>` 登记元数据、用 `OnceLock` 槽位保证每个字体至多读盘一次；CLI 发现顺序为系统字体 → 嵌入字体 → `--font-path` 目录，且整个发现被 `LazyLock` 推迟到首次访问。
- `Downloader` 是极小的下载抽象，靠「404 → `ErrorKind::NotFound`」约定支撑精准错误分流；`ProgressDownloader` 用装饰器模式为任何实现加进度，`key: &dyn Any` 让 CLI 只对包下载显示进度、对索引下载保持静默。
- 包解析三级优先：data 目录（手动安装）→ cache 目录（自动下载缓存）→ Universe 下载（仅 `preview` 命名空间）；目录布局固定为「命名空间/包名/版本」三层。
- 下载落盘用「兄弟临时目录 + 原子 rename」防并发损坏，`DirectoryNotEmpty` 视为他人已装好；包索引 `index.json` 经 `OnceCell` 只拉取一次，条目惰性反序列化、坏条目跳过。
- 包与字体的终点都是 `World` 契约：`book()`/`font()` 委托 `FontStore`，`VirtualRoot::Package` 的文件加载经 `SystemPackages::obtain` 拿到包根 `FsRoot`——内核对此一无所知。

## 7. 下一步学习建议

- **增量编译与 watch（u5-l8）**：本讲反复出现的 `LazyHash`、`LazyLock`、`OnceLock` 都在为 comemo 记忆化服务；下一讲看 `Source::edit` 增量重解析与 watch 长驻编译如何与这些缓存协作。
- **延伸阅读源码**：`crates/typst-kit/src/files.rs` 的 `FileStore`（文件级缓存与依赖追踪，`SystemWorld::dependencies` 的底层）；`crates/typst-kit/src/watcher.rs` 与 `server.rs`（watch 与 HTTP 服务的资源侧支撑）；`crates/typst-assets`（嵌入字体的实际来源 crate）。
- **动手方向**：参照本讲的接线方式，尝试为 `typst-kit` 的 `Downloader` 写一个「把所有请求记录到日志、拒绝外网只允许镜像域」的实现，体会 trait 抽象给宿主留出的控制面。
