# 字体发现三连：embedded / scan / system

## 1. 本讲目标

上一讲（u2-l1）我们建立了字体加载的「收录 + 懒加载」机制：`FontStore` 按 `push` 顺序把 `(FontSource, FontInfo)` 收进槽位，首次访问才真正读盘。但那批 `(FontSource, FontInfo)` 元组从哪里来？谁去硬盘上、或操作系统里、或二进制里把字体找出来？

本讲回答这个问题。读完本讲，你应当能够：

- 说清 `embedded()`、`scan()`、`system()` 三个顶层发现函数各自做什么、对应哪个 feature 开关。
- 理解最关键的一条分野：**内置字体返回的是已经解析好的 `Font`（字节已在内存），而扫描/系统字体返回的是 `FontPath`（只有磁盘路径，字节尚未读取）**——这正是懒加载的前提。
- 看懂 `with_db()` 如何把第三方库 `fontdb` 的能力桥接成 Typst 需要的 `(FontPath, FontInfo)` 流。
- 了解 Windows/macOS 上对 Adobe 字体的特殊补丁 `load_adobe_fonts()`。

## 2. 前置知识

- **fontdb**：一个 Rust 生态里常用的「字体数据库」库。你告诉它「扫描这个目录」或「加载系统字体」，它就维护一份 face（字形面）列表，每条记录包含字体文件路径、在该文件中的索引、以及可供解析的字体数据。typst-kit 不自己造轮子，而是借助 fontdb 做发现，再用自己的 `FontInfo` 做解析。
- **face 与 index**：一个字体文件可能包含**多个**字形面（典型如 `.ttc` 字体集合，TrueType Collection）。`index` 表示这个 face 是文件里的第几个。所以「定位一个字体」需要 `(path, index)` 两元组——这正是 `FontPath` 的两个字段。
- **feature 开关回顾（承接 u1-l2）**：typst-kit 默认关闭所有特性。本讲涉及两个开关：
  - `embedded-fonts = ["dep:typst-assets", "typst-assets/fonts"]`：拉入 `typst-assets` 并打开它的 `fonts` 特性，把一批字体字节编译进二进制。
  - `scan-fonts = ["dep:fontdb", "fontdb/memmap", "fontdb/fontconfig", "dep:dirs"]`：拉入 fontdb（带内存映射 `memmap` 与 Linux 的 `fontconfig` 支持）以及 `dirs`（定位系统标准目录）。
- **`Font` 与 `FontInfo` 的拆分（承接 u2-l1）**：`Font` 是 `Arc` 包裹的字体本体（含字形数据，较重），`FontInfo` 是从字体里解析出的廉价元数据（family、weight 等）。发现阶段只需要 `FontInfo` 来登记，`Font` 留到真正渲染时再加载。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-kit/src/fonts.rs` | 本讲主角。定义 `embedded()`、`scan()`、`system()`、`with_db()`、`load_adobe_fonts()`，以及上一讲已讲的 `FontStore`/`FontSlot`/`FontPath`/`FontSource`。 |
| `crates/typst-kit/Cargo.toml` | `[features]` 表里 `embedded-fonts` 与 `scan-fonts` 的权威定义，决定了每个函数在什么条件下才被编译。 |
| `crates/typst-cli/src/fonts.rs` | typst-cli 的 `discover_fonts()`，演示如何把三个发现函数按固定顺序拼装进一个 `FontStore`（`typst fonts` 命令与编译都用它）。 |
| `crates/typst-library/src/text/font/info.rs` | `FontInfo` 结构体定义，确认 `family` 是公开字段，供我们打印字体名。 |

## 4. 核心概念与源码讲解

### 4.1 全景：三连函数与「字节 vs 路径」的分野

#### 4.1.1 概念说明

typst-kit 在 `fonts.rs` 顶层提供了**三个**字体发现函数，它们的签名长得几乎一样，都返回一个「产出 `(来源, 元数据)` 元组的迭代器」：

| 函数 | feature | 产出元组的「来源」类型 | 字节是否已就绪 |
| --- | --- | --- | --- |
| `embedded()` | `embedded-fonts` | `Font` | **是**（字节已编译进二进制） |
| `scan(path)` | `scan-fonts` | `FontPath` | 否（只有磁盘路径） |
| `system()` | `scan-fonts` | `FontPath` | 否（只有磁盘路径） |

这张表是本讲最重要的一行结论。`embedded()` 返回的 `Font` 已经是解析完毕的字体对象（数据就在进程内存里），所以把它收录进 `FontStore` 后，`FontSource::load()` 只是做一次廉价的 `Arc` clone。而 `scan()` 与 `system()` 返回的 `FontPath` 只是「这个字体在磁盘上的 `(path, index)`」——字节还没读，真正的 `fs::read` 要等到 `FontStore::font(index)` 首次访问时，由 [`FontSource for FontPath`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117) 的 `load()` 触发。

为什么这样设计？因为「发现」可能扫到成百上千个系统字体，如果发现时就全部读进内存，启动会很慢、内存会爆。把「发现」（拿到 path + 元数据）与「加载」（读字节）拆开，配合上一讲的 `OnceLock` 懒加载，才能做到「用到哪个才读哪个」。

#### 4.1.2 核心流程

typst-cli 把三连函数按固定优先级拼装（系统字体 → 内置字体 → 命令行额外路径）：

```text
discover_fonts()
  ├─ fonts::system()      // 操作系统标准目录里的字体（FontPath）
  ├─ fonts::embedded()    // 二进制内置字体（Font）
  └─ fonts::scan(path)    // 用户用 --font-path 指定的目录（FontPath）
        │
        ▼
   全部 extend 进同一个 FontStore
        │
        ▼
   book 下标顺序 = 收录顺序 = 优先级（先收录的优先匹配）
```

关键约定：**先收录的字体在下标里靠前，typst 在按 family 匹配字体时也会更早命中**。所以 `discover_fonts` 把系统字体放在最前，意味着用户安装的同名字体会覆盖内置字体。

#### 4.1.3 源码精读

typst-cli 的拼装逻辑只有十几行，是理解三连关系的最佳入口：

[crates/typst-cli/src/fonts.rs:L36-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L36-L55) —— `discover_fonts()` 按 system → embedded → scan 的顺序把三类字体 `extend` 进同一个 `FontStore`。

注意 `embedded()` 被 `#[cfg(feature = "embedded-fonts")]` 包裹（CLI 侧对应第 45 行的 `#[cfg(...)]`），而 `system()` 与 `scan()` 只有在开了 `scan-fonts` 时才存在。如果某个 feature 没开，对应那一行根本不会被编译。

#### 4.1.4 代码实践

阅读型实践：打开上面链接的 `discover_fonts()`，对照本节的优先级表，确认三行 `extend` 的顺序，并回答：如果用户在命令行传了 `--font-path ./my-fonts`，其中有一个名叫 `Libertinus Serif` 的字体，它会覆盖内置的 `Libertinus Serif` 吗？为什么？（提示：看收录顺序与下标位置。）

预期结果：**不会覆盖**。因为 `embedded()` 在 `scan(path)` 之前被 `extend`，内置字体下标更小、更靠前，typst 匹配时先命中内置的那个。

#### 4.1.5 小练习与答案

**练习**：为什么 `scan()` 和 `system()` 都被归到同一个 `scan-fonts` 特性，而 `embedded()` 单独一个 `embedded-fonts`？

**参考答案**：因为 `scan()` 与 `system()` 都依赖 `fontdb`（外加 `dirs`），底层基础设施相同；而 `embedded()` 依赖的是 `typst-assets`（把字体字节编进二进制），是完全不同的依赖集合与体积代价。按依赖边界划分 feature，让不需要扫描系统字体的嵌入式/CI 场景可以只开 `embedded-fonts`，避免引入 fontdb。

---

### 4.2 embedded()：取出二进制内置字体

#### 4.2.1 概念说明

`embedded()` 把 typst 预先挑选、随二进制分发的字体交给你。它依赖 `typst-assets` crate 的 `fonts()` 函数——后者返回的是一组 `&'static [u8]`，即编译进二进制的字体字节。这意味着：**只要开了 `embedded-fonts`，这份 Typst 工具哪怕在没有联网、没有任何系统字体的机器上也能正常排版基础文档。**

内置字体清单（见函数文档注释）：正文用的 *Libertinus Serif* 与 *New Computer Modern*、数学用的 *New Computer Modern Math*、代码用的 *DejaVu Sans Mono*。

#### 4.2.2 核心流程

```text
typst_assets::fonts()        // 一组静态字节切片
   │  每个 data 可能是一个「字体集合」(.ttc)，含多个 face
   ▼
flat_map(|data| Font::iter(Bytes::new(data)))
   │  对每个字节块解析出 0 或多个 Font
   ▼
map(|font| (font, font.info().clone()))
   │  每个已加载 Font 配上它的元数据
   ▼
返回 (Font, FontInfo) 迭代器
```

注意 `flat_map` 而不是 `map`：单个字节块可能含多个 face（字体集合），`Font::iter` 会把它们逐个吐出。这正是「一个文件 → 多个 face」的体现。

#### 4.2.3 源码精读

[crates/typst-kit/src/fonts.rs:L129-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L129-L142) —— `embedded()` 把 `typst_assets::fonts()` 的静态字节，用 `flat_map(Font::iter)` 解析成 `(Font, FontInfo)` 流。

关键点：
- 整个函数被 `#[cfg(feature = "embedded-fonts")]` 门禁；不开该特性时它根本不存在。
- 返回类型是 `impl Iterator<Item = (Font, FontInfo)>`——元组的第一个元素是**已加载的 `Font`**，与 `scan/system` 的 `FontPath` 形成对比。
- 这些 `Font` 收录进 `FontStore` 后，匹配到 [`FontSource for Font`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L105-L109)（注意是 `Font` 那个 impl，不是 `FontPath`），其 `load()` 直接 `Some(self.clone())`，零读盘。

#### 4.2.4 代码实践

阅读型实践：在 [`FontSource for Font`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L105-L109) 与 [`FontSource for FontPath`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117) 两个 impl 之间对比 `load()` 的实现差异，用一句话总结。

预期结果：`Font` 的 `load` 只是 `clone`（数据已在内存），`FontPath` 的 `load` 会 `fs::read` 读盘再 `Font::new` 解析。这就是 `embedded()` 与 `scan()/system()` 在「加载代价」上的根本差别。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `embedded()` 里的 `flat_map` 改成 `map`，会发生什么？

**参考答案**：`Font::iter` 返回的是一个迭代器（一个字节块可能含多个 face），用 `map` 会把「迭代器本身」当作单个元素，导致类型不对、无法编译；即便强行收集，也会丢失同一字节块里第 2 个及以后的 face。`flat_map` 才能把每个 face 摊平成独立元素。

**练习 2**：为什么 `embedded()` 不需要 `fontdb`？

**参考答案**：内置字体是编译进二进制的静态字节，不需要去文件系统或操作系统「发现」它们，直接用 Typst 自己的 `Font::iter` 解析即可。fontdb 只在需要扫描磁盘/系统时才有用。

---

### 4.3 with_db()：fontdb 适配核心

#### 4.3.1 概念说明

`scan()` 和 `system()` 都非常薄——真正干活的共享骨架是私有的 `with_db()`。它接受一个闭包，闭包负责「往 fontdb 里塞字体」（可以是 `load_system_fonts`、`load_fonts_dir`，或 Adobe 补丁），然后 `with_db()` 统一把 fontdb 里的记录翻译成 Typst 的 `(FontPath, FontInfo)` 流。

为什么要包一层？因为 fontdb 的数据模型（`Source::File`、face id 等）和 Typst 的模型（`FontPath`、`FontInfo`）并不一致。`with_db()` 就是这两个世界之间的**适配器**，让 `scan`/`system` 只需要关心「塞什么进去」，不必各自重复「翻译出来」的逻辑。

#### 4.3.2 核心流程

```text
with_db(f):
  1. 新建空 fontdb::Database
  2. 调用 f(&mut db)             // 调用方决定塞哪些字体进来
  3. 遍历 db.faces():
       a. 从 face.source 取出 path（File / SharedFile），跳过 Binary
       b. db.with_face_data(id, FontInfo::new)  // 借 fontdb 读字节 + 用 Typst 构造 FontInfo
            - .expect(...)  保证 face 数据可读（fontdb 既然收录了它，就一定能读）
            - ?              若 FontInfo::new 返回 None（Typst 用不了的字体），跳过
       c. 包成 FontPath { path, index: face.index }
  4. collect::<Vec<_>>().into_iter()  // 求值成拥有所有权的 Vec
```

第 4 步的 `.collect::<Vec<_>>()` 看似多余，实则必要：`db` 是函数内的局部变量，迭代器在遍历时需要借用 `db`（因为 `with_face_data` 借的是 `&self`）。先 `collect` 成 `Vec` 把数据「物化」、与 `db` 解绑，函数返回后 `db` 被释放也不会影响迭代器。代价是这一步会**立即**完成全部解析（非惰性）——但对于字体发现这通常可以接受。

#### 4.3.3 源码精读

[crates/typst-kit/src/fonts.rs:L168-L194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L168-L194) —— `with_db()` 是 `scan`/`system` 的共享骨架：闭包负责填充 fontdb，本函数负责把 face 翻译成 `(FontPath, FontInfo)`。

几个值得品味的细节：

- **fontdb 只发现，Typst 才解析**：`db.with_face_data(face.id, FontInfo::new)` 让 fontdb 读出某个 face 的字节，再把字节喂给 Typst 的 `FontInfo::new`。也就是说「这个 face 的 family/weight 是什么」由 Typst 自己说了算，fontdb 只是个「帮我找到文件、读出字节」的帮手。
- **`.expect("database must contain this font")`**：fontdb 既然把这条 face 收录进来了，按契约它一定能提供数据，所以这里用 `expect` 而非 `?`；这是一条「内部不变量」。
- **紧随其后的 `?`**：即使字节可读，`FontInfo::new` 也可能返回 `None`（例如某些 Typst 不支持的字体格式），此时整条 `filter_map` 返回 `None`，安静地跳过它——这正是「系统里有 1000 个字体，Typst 只用得了其中一部分」的容错点。
- **`FontPath { path, index: face.index }`**：把 fontdb 的 face 翻译成 Typst 的 `FontPath`，`index` 保留「文件内第几个 face」的信息（见 [`FontPath` 结构体](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L119-L127)）。

#### 4.3.4 代码实践

阅读型实践：在 `with_db` 的 `filter_map` 里，去掉 `?`（把 `FontInfo::new` 返回 `None` 的字体也保留下来）在概念上会带来什么后果？结合「Typst 用不了的字体」这一点说明为什么跳过它们是必要的。

预期结果：保留会导致 `(FontPath, None)` 这样的非法元组，无法塞进 `FontStore`（`push` 要求第二个元素是非空 `FontInfo`）。跳过是为了保证收录进 `FontBook` 的每个字体都是 Typst 真正能渲染的。

#### 4.3.5 小练习与答案

**练习**：`with_db` 用了 `.collect::<Vec<_>>().into_iter()`，而不是直接返回 `db.faces().filter_map(...)`。请从**生命周期**角度解释原因。

**参考答案**：`filter_map` 闭包里调用了 `db.with_face_data(...)`，它借用 `&db`；而 `db` 是 `with_db` 的局部变量。若直接返回借用了 `db` 的迭代器，函数返回时 `db` 被销毁，迭代器就成了悬垂引用，无法编译。先 `collect` 成拥有所有权的 `Vec<(FontPath, FontInfo)>`，就与 `db` 彻底解绑，可以安全返回。

---

### 4.4 scan()：递归扫描指定目录

#### 4.4.1 概念说明

`scan(path)` 在**用户指定的目录**里递归寻找字体。它对应 typst-cli 的 `--font-path` 参数：当你想把项目自带的字体、或某个字体目录纳入可用范围时，就用它。

它本身只做一件事——把 `path` 喂给 fontdb 的 `load_fonts_dir`，剩下的翻译工作全部交给 `with_db()`。

#### 4.4.2 核心流程

```text
scan(path):
  with_db(|db| db.load_fonts_dir(path))
    │  fontdb 递归遍历 path 下所有文件，识别其中的字体
    ▼
  返回 (FontPath, FontInfo) 迭代器
```

`load_fonts_dir` 是**递归**的（见 `scan` 的文档注释 "searched recursively"），会深入子目录。fontdb 自己判断哪些文件是字体（按扩展名与文件头），typst-kit 不重复造这个逻辑。

#### 4.4.3 源码精读

[crates/typst-kit/src/fonts.rs:L159-L166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L159-L166) —— `scan()` 是 `with_db` 的一行包装：把 `path` 交给 `db.load_fonts_dir`。

注意两个细节：
- 函数开头有一个 `let _scope = typst_timing::TimingScope::new("scan system fonts");`——它把这次扫描的耗时记进 typst 的性能追踪（承接 u8-l1 的 Timer）。注意这里的字符串写的是 `"scan system fonts"`（与 `system()` 共用，是个小的命名复用）。
- `#[cfg(feature = "scan-fonts")]` 门禁：没有 `scan-fonts`，这个函数不存在。

#### 4.4.4 代码实践

这是本讲的**主实践**（可运行）。请完成下面这步，并在「综合实践」里看到它的完整串联版本。

操作步骤：

1. 新建一个 Cargo 项目，依赖 typst-kit 并只启用 `scan-fonts`：

```toml
# Cargo.toml —— 示例配置
[dependencies]
typst-kit = { path = "../../path/to/typst/crates/typst-kit", features = ["scan-fonts"] }
# 若使用已发布版本：typst-kit = { version = "...", features = ["scan-fonts"] }
```

2. 准备一个目录（例如 `./my-fonts`），放入一两个 `.ttf` 或 `.otf` 字体文件。

3. 编写 `main.rs`：

```rust
// 示例代码：发现 -> 打印 -> 收录
use typst_kit::fonts::{self, FontStore};

fn main() {
    let dir = std::path::Path::new("./my-fonts");

    // 1) 扫描目录，先把结果物化（scan 返回的是惰性迭代器）
    let discovered: Vec<_> = fonts::scan(dir).collect();

    // 2) 发现阶段：只拿到了路径与元数据，字体字节尚未读盘
    for (fp, info) in &discovered {
        println!("- {:30} {}", info.family, fp.path.display());
    }

    // 3) 收录进 FontStore
    let mut store = FontStore::new();
    store.extend(discovered);

    // 4) 通过 book().families() 列出所有 family（与 `typst fonts` 命令同款做法）
    for (family, indices) in store.book().families() {
        let first = indices.into_iter().next().unwrap();
        let _font = store.font(first); // <-- 这一行才会真正 fs::read 读盘
        println!("loaded family: {family}");
    }
}
```

需要观察的现象：

- 第 2 步打印时，字体**还没有**被读进内存（只是路径与元数据）。
- 第 4 步调用 `store.font(first)` 时，才真正触发磁盘读取（可在 `FontPath::load` 处加日志验证）。

预期结果：能看到 `my-fonts` 里每个字体的 family 名被打印两遍——一遍来自发现阶段的 `info.family`，一遍来自收录后的 `book().families()`。若你的目录里没有 Typst 能识别的字体文件，`discovered` 可能为空——此时请确认放入的是合法 `.ttf`/`.otf`/`.ttc`。**待本地验证**（实际输出取决于你放入的字体）。

#### 4.4.5 小练习与答案

**练习**：如果在 `./my-fonts` 里同时放了某个字体的 `.ttc` 集合（含 3 个 face），`scan()` 会产出几条 `(FontPath, FontInfo)`？它们的 `FontPath.index` 分别是什么？

**参考答案**：产出 3 条。每条对应一个 face，`FontPath.index` 分别是 `0`、`1`、`2`（即 `face.index`）。这正解释了为什么 `FontPath` 要带 `index` 字段——同一个文件能定位多个 face。

---

### 4.5 system()：发现操作系统字体

#### 4.5.1 概念说明

`system()` 不需要你指定目录，它去**操作系统自带的标准字体位置**找字体：Windows 的 `C:\Windows\Fonts`、macOS 的 `/Library/Fonts` 与 `~/Library/Fonts`、Linux 上则借助 `fontconfig` 查询系统已安装的字体。它对应 typst-cli 默认（不加 `--ignore-system-fonts`）的行为。

`system()` 与 `scan()` 的区别只有一句：**`scan()` 扫描你给的目录，`system()` 扫描操作系统约定好的目录**。二者底层都走 `with_db()`。

#### 4.5.2 核心流程

```text
system():
  with_db(|db| {
      db.load_system_fonts();              // fontdb 按当前 OS 找标准目录
      #[cfg(windows/macos)]
      load_adobe_fonts(db);                // 在 Win/macOS 上额外补 Adobe 字体
  })
```

这里第一次出现了 `load_adobe_fonts` 的调用——它只在 Windows 与 macOS 上编译（用 `#[cfg(any(target_os = "windows", target_os = "macos"))]` 门禁），下一节详解。

#### 4.5.3 源码精读

[crates/typst-kit/src/fonts.rs:L144-L157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L144-L157) —— `system()` 调 `db.load_system_fonts()`，并在 Windows/macOS 上追加 Adobe 字体。

要点：
- `db.load_system_fonts()` 是 fontdb 提供的跨平台方法：在 Linux 上走 fontconfig（这也是 `scan-fonts` 特性要带 `fontdb/fontconfig` 的原因），在 Windows/macOS 上走各自的系统 API/约定路径。
- `system()` 与 `scan()` 共用同一个 timing 名字 `"scan system fonts"`，所以性能追踪里它们会合并到同一计时项。

#### 4.5.4 代码实践

阅读型实践：在 Linux 上，`scan-fonts` 特性里为什么要显式带上 `fontdb/fontconfig`？请到 `Cargo.toml` 的 `[features]` 表里找到 `scan-fonts` 这一行确认。

预期结果：`scan-fonts = ["dep:fontdb", "fontdb/memmap", "fontdb/fontconfig", "dep:dirs"]`。`fontconfig` 是 Linux/BSD 上事实标准的字体发现服务；不带它，`load_system_fonts()` 在 Linux 上就无法查询系统字体。`fontdb/memmap` 则让 fontdb 用内存映射高效读取大字体文件。

#### 4.5.5 小练习与答案

**练习**：`scan(path)` 与 `system()` 都返回 `(FontPath, FontInfo)`，且都走 `with_db`。请说出它们传给 `with_db` 的闭包有什么不同。

**参考答案**：`scan` 的闭包是 `|db| db.load_fonts_dir(path)`——扫描**指定**目录；`system` 的闭包是 `|db| { db.load_system_fonts(); (Win/macOS 上) load_adobe_fonts(db); }`——扫描**操作系统标准**目录，并在特定平台补 Adobe 字体。两者共享 `with_db` 的翻译逻辑。

---

### 4.6 load_adobe_fonts()：Windows/macOS 的 Adobe 字体补丁

#### 4.6.1 概念说明

Adobe Fonts（原 Typekit）在 Windows 与 macOS 上通过 Creative Cloud 的 CoreSync 把字体放在一个**非标准**目录里（`<data_dir>/Adobe/...`）。fontdb 的 `load_system_fonts()` 不会扫到这些位置，于是 typst-kit 专门用 `load_adobe_fonts()` 把它们补进来。

这个函数只在 `cfg(all(feature = "scan-fonts", any(target_os = "windows", target_os = "macos")))` 下编译，Linux 上完全不存在。源码注释里还附了 Adobe Fonts 服务条款第 3.1(A) 条的链接，说明这种本地加载在许可上是允许的。

#### 4.6.2 核心流程

```text
load_adobe_fonts(db):
  data_dir = dirs::data_dir()?                  // 如 ~/.local/share 或 %APPDATA%
  base = data_dir/Adobe
  prefix = "." (macOS) / "" (Windows)
  subdirs = [
      "CoreSync/plugins/livetype/{prefix}r",
      "{prefix}User Owned Fonts",
  ]
  对每个子目录里的「文件」（跳过子目录）:
      db.load_font_file(path)
```

macOS 上的 `.` 前缀是因为这些是 Unix 风格的隐藏目录（以点开头），Windows 则没有这个约定所以前缀为空。

#### 4.6.3 源码精读

[crates/typst-kit/src/fonts.rs:L196-L224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L196-L224) —— `load_adobe_fonts()` 在 Windows/macOS 上扫描 Adobe CoreSync 目录并喂给 fontdb。

要点：
- `dirs::data_dir()` 提供 OS 相关的用户数据目录（这正是 `scan-fonts` 要带 `dep:dirs` 的另一个原因）。
- 用 `entry.metadata().is_file()` 过滤，只加载文件、跳过子目录（Adobe 把每个字体存成单独的文件）。
- `db.load_font_file(entry.path()).ok()` 忽略单个文件加载失败——某文件损坏不应让整个 Adobe 字体加载中断。
- 函数被 `system()` 调用（见 4.5），不会单独对外暴露。

#### 4.6.4 代码实践

阅读型实践：由于该函数只在 Windows/macOS 上编译，请在 Linux 环境下**阅读源码**即可，重点理解 `prefix` 在两个平台上的取值差异及其原因。

预期结果：macOS 用 `.` 前缀（隐藏目录约定），Windows 用空前缀。这正是同一份代码用 `if cfg!(target_os = "macos") { "." } else { "" }` 适配两端的体现。

#### 4.6.5 小练习与答案

**练习**：`load_adobe_fonts` 里多处用了 `let Ok(...) = ... else { return };` 或 `.ok()` 来「出错就跳过/返回」。为什么对待错误如此「宽容」？

**参考答案**：字体发现是「尽力而为」的过程——用户机器上可能没有 Adobe 目录、目录可能为空、个别字体文件可能损坏。任何一个失败都不应让整个字体发现流程崩溃；跳过坏文件、在无目录时直接返回，才能保证 typst 仍能正常使用其他字体。

## 5. 综合实践

把本讲三个发现函数串起来，模拟 typst-cli 的 `discover_fonts()` 做一次「多来源字体汇总」，并验证「发现 vs 加载」的边界。

任务：

1. 新建 Cargo 项目，依赖 typst-kit，**同时**启用 `embedded-fonts` 与 `scan-fonts`：

```toml
# 示例配置
[dependencies]
typst-kit = { path = "<typst-kit 路径>", features = ["embedded-fonts", "scan-fonts"] }
```

2. 编写 `main.rs`，按 system → embedded → scan 的顺序收录字体，并统计每个来源贡献了多少个 family：

```rust
// 示例代码：多来源字体汇总
use typst_kit::fonts::{self, FontStore};

fn main() {
    let mut store = FontStore::new();
    let mut counts = [(0usize, "system"), (0, "embedded"), (0, "scan(dir)")];

    // 注意：system()/scan() 返回 FontPath，embedded() 返回 Font；
    // 三者都能被 extend 收录，因为 FontPath 与 Font 都实现了 FontSource。
    counts[0].0 = fonts::system().inspect(|_| store_push::<_>(/*占位*/)).count();
    // 上面这行只为示意；实际应如下分步统计：
    let sys: Vec<_> = fonts::system().collect();
    counts[0].0 = sys.len();
    store.extend(sys);

    let emb: Vec<_> = fonts::embedded().collect();
    counts[1].0 = emb.len();
    store.extend(emb);

    let scn: Vec<_> = fonts::scan(std::path::Path::new("./my-fonts")).collect();
    counts[2].0 = scn.len();
    store.extend(scn);

    for (n, label) in counts {
        println!("{label:<12} 发现 {n} 个字体");
    }
    println!("book 中 family 总数（去重后）：{}",
        store.book().families().count());

    // 触发一次真正加载，验证懒加载：只有这一步才读盘
    if let Some((_, idxs)) = store.book().families().next() {
        if let Some(i) = idxs.into_iter().next() {
            let _ = store.font(i);
            println!("首次 store.font({i}) 触发了实际加载");
        }
    }
}

// 仅占位，演示用；实际代码请用 store.extend(...) 收录
#[allow(dead_code)]
fn store_push<T: typst_kit::fonts::FontSource>(_x: T) {}
```

> 说明：上面 `main` 里第一段 `inspect` 写法是**故意错误**的示意（提醒你不要在迭代时去 push 一个 `&` 引用），正确做法是下方「先 collect 再 extend」的三段。请以那三段为准。

3. 运行并观察：

   - 三个来源各自发现多少个字体？`embedded` 在开了 `embedded-fonts` 后应当稳定输出若干个（Libertinus Serif、New Computer Modern 等）。
   - `system` 的数量取决于你机器上装了多少字体（Linux 上若未装 fontconfig 开发库，可能较少）。
   - `scan("./my-fonts")` 的数量取决于你放入的字体。
   - 去重后的 family 总数通常**小于**三者之和（不同来源可能含同名 family，但它们下标不同、都会被保留，只是 `families()` 按 family 名聚合显示）。

4. 进阶：在 [`FontPath` 的 `load()` 实现](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117) 里临时加一行 `eprintln!("read {:?}", self.path);`，重新运行，观察「只有访问到的字体才会被读盘」——验证发现与加载确实被拆开了。（注意：本讲规则要求不修改源码，这一步请在你的本地副本/分支上做，验证后还原。）

预期结果与待确认：family 计数因机器而异，属正常现象；关键验证点是第 4 步——你应只看到**被 `store.font(i)` 访问过的那一个**字体路径被打印，其余几百个系统字体都不会触发读盘。**待本地验证**。

## 6. 本讲小结

- typst-kit 提供三个顶层字体发现函数：`embedded()`（内置，需 `embedded-fonts`）、`scan(path)` 与 `system()`（均需 `scan-fonts`）。
- 最关键的分野：`embedded()` 产出**已加载的 `Font`**（字节在内存），`scan()`/`system()` 产出**`FontPath`**（只有磁盘路径，字节未读）——后者配合上一讲的 `OnceLock` 实现懒加载。
- `with_db()` 是 `scan`/`system` 的共享骨架：闭包负责「把字体塞进 fontdb」，它负责把 fontdb 的 face 翻译成 Typst 的 `(FontPath, FontInfo)`，并用 `FontInfo::new` 决定哪些字体 Typst 能用（用不了的安静跳过）。
- 收录顺序 = 下标顺序 = 匹配优先级；typst-cli 按 system → embedded → scan 拼装，所以同名系统字体排在内置字体之前。
- `load_adobe_fonts()` 只在 Windows/macOS 编译，把 Adobe CoreSync 非标准目录里的字体补进 fontdb，体现「发现过程尽力而为、容错跳过」的设计。
- 发现阶段的「先 `collect` 成 `Vec`」是为了摆脱对局部 `fontdb` 的借用，是生命周期上的必要处理。

## 7. 下一步学习建议

本讲把「字体从哪来」讲完了。接下来建议：

- 若你想看字体如何被**真正加载与缓存**，回到 u2-l1 复习 `FontSlot` 的 `OnceLock` 机制；可进一步阅读 `FontPath::load` 与 `typst_library::text::Font::new` 的交互。
- 若你想进入 typst-kit 的下一个子系统，进入 **u3（文件与源码加载）**：`FileStore` 与 `FontStore` 是平行的设计，`FileLoader` 的可替换字节来源思路与本讲的 `FontSource` 一脉相承。
- 顺带推荐阅读 typst-cli 的 `discover_fonts()`（本讲已引用）与 `typst fonts` 子命令，看这三连函数如何被真实产品代码消费。
