# 字体发现与 fonts 命令

## 1. 本讲目标

Typst 是一个排版系统，排版离不开字体。当我们执行 `typst compile doc.typ` 时，编译器需要知道「系统里有哪些字体可用」「某个字体族（family）有哪些变体（粗体、斜体等）」。本讲要回答的问题是：**CLI 是从哪些地方把字体找出来的？又是如何展示给用户的？**

学完本讲，你应当能够：

1. 说清楚 `discover_fonts` 如何按「系统字体 + 内嵌字体 + 自定义路径字体」三个来源组合出最终的字体集合，以及三个 `--ignore-*` / `--font-path` 开关如何控制这一过程。
2. 看懂 `typst fonts` 命令的输出结构：字体族列表，以及 `--variants` 给出的变体树。
3. 理解静态字体与可变字体（variable font）在输出上的差异，以及 `write_variant` / `write_axis` 如何格式化变体轴（axes）。
4. 理解同一份 `discover_fonts` 如何被 `fonts` 命令**立即**调用、又被 `SystemWorld` **惰性**复用，从而避免重复扫描。

本讲聚焦两个文件：`src/fonts.rs`（字体发现与命令实现）与 `src/world.rs`（编译时的惰性复用）。

---

## 2. 前置知识

在进入源码前，先建立几个关于字体的直觉。

### 2.1 字体族（family）与变体（variant）

一个「字体族」是一组风格相近的字体的统称，例如 `New Computer Modern`。同一个族下往往有多个**变体**：常规（Regular）、粗体（Bold）、斜体（Italic）、粗斜体（Bold Italic）等。Typst 用 `FontVariant` 描述一个变体的三个维度：

- **Style**：风格，如 `Normal` / `Italic` / `Oblique`。
- **Weight**：字重，一个数值（如 400 = Regular，700 = Bold）。
- **Stretch**：字宽，如 `Normal` / `Condensed` / `Expanded`。

一个字体文件通常对应一个变体（一个 `.ttf`/`.otf` 文件 = 一个 `FontInfo`）。

### 2.2 可变字体（variable font）与变体轴（axes）

**可变字体**是 OpenType 的一种格式：同一个字体文件里，字体可以沿若干**轴（axis）**连续变化。常见标准轴有：

| 轴标签 | 含义 | 说明 |
|--------|------|------|
| `wght` | Weight | 字重，如 100–900 |
| `wdth` | Width | 字宽 |
| `ital` | Italic | 是否斜体（0/1） |
| `slnt` | Slant | 倾斜角度 |
| `opsz` | Optical Size | 光学尺寸，针对不同字号优化 |

每个轴都有一个取值范围 `[min, max]` 和一个默认值 `default`。例如一个可变字体的 `wght` 轴可能是 `100–900 (Default: 400)`。可变字体还可能有自定义轴（非标准 4 字母标签）。**静态字体**没有轴，每个文件固定一种风格。

理解了「族 → 变体 → 轴」这三级结构，后面 `write_variant` 的分支逻辑就一目了然了。

### 2.3 字体的三个来源

CLI 把字体分成三个来源：

1. **系统字体（system）**：操作系统已安装的字体，通过系统字体目录或平台 API 发现。
2. **内嵌字体（embedded）**：编译进 `typst` 二进制本身的字体，保证「即使系统没有任何字体也能排版」。这是一个 Cargo feature（`embedded-fonts`），默认开启。
3. **自定义路径字体（scan）**：用户通过 `--font-path` 指定的目录，CLI 会**递归**扫描其中的字体文件。

本讲的 `discover_fonts` 就是把这三类字体按顺序装进一个 `FontStore`。

### 2.4 FontStore 与 FontBook（背景，来自外部 crate typst-kit）

`fonts.rs` 顶部 `use typst_kit::fonts::{self, FontPath, FontStore}` 引入的类型来自 `typst-kit`（这是一个外部依赖，不在本仓库内）。你只需理解它们的角色：

- **`FontStore`**：字体仓库。它持有一个 **`FontBook`**（字体「目录册」，记录所有已发现字体的元数据：族名、变体、轴）和**惰性加载**能力——元数据在发现阶段就收集好，而真正的字体数据（像素、字形）只有在编译器真正用到某个字体时才按索引加载。
- `FontBook` 提供 `families()`（枚举族名与属于该族的字体索引）、`info(index)`（取某个索引的 `FontInfo` 元数据）等方法。
- `FontStore::source(index)` 返回某字体的来源；来自磁盘文件的字体其 source 可向下转型为 `FontPath`（持有真实路径），内嵌字体则没有路径。
- `fonts::system()` / `fonts::embedded()` / `fonts::scan(path)` 分别返回对应来源的字体迭代器，可被 `FontStore::extend` 批量装入。

这些细节**不在本仓库内**，所以本讲不给出 typst-kit 的行号链接，只据 `fonts.rs` 的用法讲解。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/fonts.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs) | 字体发现的核心函数 `discover_fonts`、`fonts` 命令的实现 `fonts()`，以及变体/轴的格式化 `write_variant` / `write_axis`。本讲的主战场。 |
| [src/world.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | `SystemWorld` 如何用 `LazyLock` 惰性复用 `discover_fonts`，并通过 `book()` / `font()` 把字体交给编译器。 |
| [src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | `FontArgs`（字体相关命令行参数）与 `FontsCommand`（`fonts` 子命令结构）的定义。 |
| [src/main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | `dispatch` 把 `Fonts` 子命令分发给 `crate::fonts::fonts`（仅一行，承接 u1-l2）。 |

---

## 4. 核心概念与源码讲解

### 4.1 字体从哪里来：discover_fonts 的三源组合

#### 4.1.1 概念说明

`discover_fonts` 是字体发现的唯一入口。它读入一组 `FontArgs`，决定「要不要包含系统字体」「要不要包含内嵌字体」「还要扫描哪些额外目录」，然后把这些字体装进一个 `FontStore` 返回。

设计上有两点值得注意：

- **来源之间是「累加」关系**：系统、内嵌、自定义路径三者的字体都会进入同一个 `FontStore`，族名相同的字体会被归到一起。
- **顺序固定**：先系统，再内嵌，最后自定义路径。当多个来源存在同族字体时，靠前的来源通常在 `FontBook` 中排在前面（从而在 `fonts` 输出中也靠前）。

#### 4.1.2 核心流程

`discover_fonts(args)` 的执行过程可以用下面的伪代码描述：

```
store = FontStore::new()              # 空仓库
if not args.ignore_system_fonts:
    store.extend(system())            # 装入系统字体
if feature("embedded-fonts") and not args.ignore_embedded_fonts:
    store.extend(embedded())          # 装入内嵌字体
for path in args.font_paths:          # 逐个扫描自定义目录
    store.extend(scan(path))
return store
```

三个开关的作用一目了然：

| 命令行开关 | 环境变量 | 作用 |
|------------|----------|------|
| `--ignore-system-fonts` | `TYPST_IGNORE_SYSTEM_FONTS` | 跳过系统字体 |
| `--ignore-embedded-fonts` | `TYPST_IGNORE_EMBEDDED_FONTS` | 跳过内嵌字体（仅 `embedded-fonts` feature 下存在） |
| `--font-path <DIR>` | `TYPST_FONT_PATHS` | 追加一个递归扫描的目录（可多次指定，或用路径分隔符给出多个） |

#### 4.1.3 源码精读

`FontArgs` 定义了上面三个开关（注意 `ignore_embedded_fonts` 字段被 `#[cfg(feature = "embedded-fonts")]` 条件编译保护）：

- [src/args.rs:466-490](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L466-L490) — `FontArgs` 结构：`font_paths`（`--font-path`）、`ignore_system_fonts`（`--ignore-system-fonts`）、`ignore_embedded_fonts`（`--ignore-embedded-fonts`，仅在开启 `embedded-fonts` feature 时编译）。

`discover_fonts` 本体非常短，严格按「系统 → 内嵌 → 自定义」顺序累加：

- [src/fonts.rs:36-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L36-L55) — `discover_fonts`：先 `FontStore::new()` 建空仓库；若未忽略系统字体则 `extend(fonts::system())`；在 `embedded-fonts` feature 下若未忽略内嵌字体则 `extend(fonts::embedded())`；最后遍历 `font_paths` 逐个 `extend(fonts::scan(path))`。

注意第 45 行的 `#[cfg(feature = "embedded-fonts")]`：如果这个 feature 没开，整段内嵌字体逻辑连同对应的命令行参数都会被编译器移除。该 feature 默认开启，定义见：

- [Cargo.toml:88-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L88-L92) — `default = ["embedded-fonts", "http-server"]`，且 `embedded-fonts = ["typst-kit/embedded-fonts"]`，即 CLI 的 feature 透传给 `typst-kit`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `discover_fonts` 的三源组合与三个开关。

**操作步骤**：

1. 在仓库根目录构建 CLI（承接 u1-l1）：
   ```bash
   cargo build
   ```
2. 默认配置下，只看内嵌字体（排除系统字体的干扰）：
   ```bash
   ./target/debug/typst fonts --ignore-system-fonts
   ```
3. 再排除内嵌字体，理论上应当一个字体都看不到：
   ```bash
   ./target/debug/typst fonts --ignore-system-fonts --ignore-embedded-fonts
   ```
4. 准备一个自定义字体目录，放一两个 `.ttf`/`.otf` 文件，然后只扫描它：
   ```bash
   mkdir -p /tmp/myfonts && cp /path/to/某字体.ttf /tmp/myfonts/
   ./target/debug/typst fonts \
       --ignore-system-fonts --ignore-embedded-fonts \
       --font-path /tmp/myfonts
   ```

**需要观察的现象**：

- 第 2 步应列出内嵌字体族（如 `DejaVu Sans Mono`、`Libertinus Serif`、`New Computer Modern`、`New Computer Modern Math`）。
- 第 3 步应输出为空（无任何字体族）。
- 第 4 步应只列出你在 `/tmp/myfonts` 放入的字体族。

**预期结果**：输出与「系统 / 内嵌 / 自定义」三个来源的开关逐一对应，验证 `discover_fonts` 的累加逻辑。

> 这些行为已被项目自身的 smoke 测试固定下来，可作为参照：[tests/smoke.rs:50-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L50-L59) 的 `test_fonts_embedded` 断言内嵌字体族清单；[tests/smoke.rs:61-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L61-L83) 的 `test_fonts_path` 把开发用字体写入临时目录，用 `--font-path` 扫描后比对发现到的族名集合是否与预期完全一致。如果本地无法构建，可先阅读这两个测试理解预期行为（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：若想「完全只用某个目录里的字体，忽略系统和内嵌」，应该怎么组合开关？
**答案**：同时使用 `--ignore-system-fonts --ignore-embedded-fonts --font-path <DIR>`。这正是 `test_fonts_path` 采用的组合。

**练习 2**：为什么 `ignore_embedded_fonts` 字段（args.rs:487-489）要加 `#[cfg(feature = "embedded-fonts")]`，而 `ignore_system_fonts` 不用？
**答案**：内嵌字体只在 `embedded-fonts` feature 下才存在（`fonts::embedded()` 本身也是条件编译的）。关掉该 feature 时，根本没有内嵌字体可言，对应的「忽略」开关也就没有意义，故连同字段一起被移除；系统字体始终存在，所以无需条件编译。

---

### 4.2 fonts 命令：字体族列表与 --variants 变体树

#### 4.2.1 概念说明

`fonts` 命令把 `discover_fonts` 的结果打印出来。它有两种粒度：

- 默认：**只列字体族名**，每行一个。适合快速确认「某个字体在不在」。
- 加 `--variants`：在每个族名下额外打印该族所有变体的详细信息（文件路径、Style/Weight/Stretch、可变字体轴），用树形字符（`├`/`└`/`│`）排版，便于排查「为什么我的粗体没生效」之类的问题。

#### 4.2.2 核心流程

```
store = discover_fonts(command.font)
for (family, indices) in store.book().families():   # 族名 → 属于该族的字体索引
    打印 family
    if command.variants:
        for index in indices:                       # 遍历该族每个变体
            info = store.book().info(index)
            path  = store.source(index) 向下转型为 FontPath 的路径（内嵌则无）
            write_variant(格式化器, info, path, 是否最后一个)   # 渲染变体树
        打印空行                                     # 族与族之间留白
```

关键点：`book().families()` 返回的不是扁平的字体列表，而是「族名 → 一组索引」的映射。同一族的多个变体（如常规、粗体、斜体）共享一个族名，被归到同一组下一起打印。

#### 4.2.3 源码精读

命令入口函数 `fonts()`：

- [src/fonts.rs:13-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L13-L34) — `fonts(command)`：调用 `discover_fonts`；遍历 `book().families()`，先 `println!("{family}")` 打印族名；若 `command.variants` 为真，则把该族的索引列表变成 `peekable` 迭代器，逐个取出 `info` 与 `path`，并调用 `write_variant` 渲染。

注意第 22-25 行对 `path` 的处理：`fonts.source(index)` 返回一个 `dyn Any` 的来源对象，代码用 `downcast_ref::<FontPath>()` 尝试把它转成「磁盘路径」来源。转换成功就拿到真实路径；失败（内嵌字体无路径）则得到 `None`，后续 `write_variant` 会把它显示成 `(Embedded)`。这就是区分「磁盘字体」与「内嵌字体」的方式。

`FontsCommand` 本身很薄，只是把 `FontArgs`（flatten 进来）和一个 `--variants` 布尔开关打包：

- [src/args.rs:229-239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L229-L239) — `FontsCommand`：`#[clap(flatten)] font: FontArgs` 复用字体参数；`#[arg(long)] variants: bool` 控制 `--variants`。

命令分发只一行（承接 u1-l2 的 `dispatch`）：

- [src/main.rs:76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L76) — `Command::Fonts(command) => crate::fonts::fonts(command)`。

#### 4.2.4 代码实践

**实践目标**：对比「只列族名」与「列出变体树」两种输出，理解 `--variants` 多打印了什么。

**操作步骤**：

```bash
# 1) 只看族名
./target/debug/typst fonts --ignore-system-fonts

# 2) 展开变体树
./target/debug/typst fonts --ignore-system-fonts --variants
```

**需要观察的现象**：

- 第 1 步每个族只占一行。
- 第 2 步每个族名下方多出若干以 `├` / `└` 开头的行，描述该族的各个变体；静态字体显示 `(Embedded)` 或路径加上一行 `Style/Weight/Stretch`，可变字体显示 `(Variable)` 并列出各轴范围；族与族之间有一行空行（来自 `fonts.rs:31` 的 `println!()`）。

**预期结果**：族名清单与变体树结构清晰对应。若本地无可变字体，可临时在 `--font-path` 指向一个含可变字体的目录来观察 `(Variable)` 分支（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fonts()` 要把族内的索引迭代器改成 `peekable()`？
**答案**：为了判断「当前变体是不是该族的最后一个」，从而在 `write_variant` 里选用 `└`（最后一个）或 `├`（非最后）作为树形标记，并据此决定缩进填充串（`"     "` 还是 `"  │  "`）。`last` 参数见 `fonts.rs:26`。

**练习 2**：内嵌字体在 `--variants` 输出里会显示文件路径吗？为什么？
**答案**：不会。内嵌字体的 `source` 无法 `downcast` 成 `FontPath`（它们没有磁盘路径），所以 `path` 为 `None`，`write_variant` 会把它显示为 `(Embedded)`（见 `fonts.rs:64-67`）。

---

### 4.3 变体与可变字体轴的格式化：write_variant / write_axis

#### 4.3.1 概念说明

`write_variant` 负责「一个变体占多少行、怎么排版」；`write_axis` 负责「一条轴怎么显示成 `名称: min-max (Default: default)`」。二者配合，把静态字体与可变字体统一成一种树形输出。

核心设计思想：**对于一个可变字体，如果某条标准轴（如 `wght`）真的存在于它的轴列表里，就用「范围」形式（`Weight: 100-900 (Default: 400)`）展示；只有当该轴不存在时，才退回显示单个静态值（`Weight: 400`）。** 这样输出既不重复、又信息完整。

#### 4.3.2 核心流程

`write_variant(f, info, path, last)` 的判断逻辑：

```
path_text = path 有值 ? 真实路径 : "(Embedded)"
marker = last ? '└' : '├'
pad    = last ? "     " : "  │  "
axes = info.axes 按标准轴顺序排序
if axes 为空（静态字体）:
    写 "  {marker} {path_text}"
    写 "{pad} Style: .., Weight: .., Stretch: .."
else（可变字体）:
    写 "  {marker} {path_text} (Variable)"
    standard = StandardAxes::parse(&axes)        # 解析出 ital/slnt/wght/wdth 是否存在
    if standard.ital 与 slnt 都不存在: 写 "Style: {style}"
    if standard.wght 不存在:           写 "Weight: {weight}"
    if standard.wdth 不存在:           写 "Stretch: {stretch}"
    for axis in axes: write_axis(axis)            # 存在的轴用范围形式逐条列出
```

`write_axis(axis)` 按轴标签选一个可读名字和一个数值显示函数：

```
match axis.tag:
    ITAL → "Italic"     , 原值
    SLNT → "Slant"      , 原值
    WGHT → "Weight"     , FontWeight::from_wght
    WDTH → "Stretch"    , FontStretch::from_wdth
    OPSZ → "Optical Size", "{v}pt"
    _     → 标签字符串（自定义轴）
```

最终每条轴形如 `Weight: 100-900 (Default: 400)`，由 `write_axis_with` 统一拼出。

#### 4.3.3 源码精读

`write_variant` 是分支最复杂的函数，分静态/可变两条路径：

- [src/fonts.rs:57-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L57-L97) — `write_variant`：第 64-67 行决定路径文本；第 70-71 行按 `last` 选树形标记与缩进串；第 73-74 行把轴按 `StandardAxes::order(tag)` 排序（保证输出顺序稳定）；第 76-78 行是静态字体分支；第 79-94 行是可变字体分支，用 `StandardAxes::parse(&axes)` 判断哪些标准轴存在，**只对不存在的轴**打印静态值，存在的轴交给下面的 `write_axis` 以范围形式打印。

> 小提示：`typst_utils::display(|f| ...)` 是一个把闭包式格式化包装成 `Display` 对象的工具，这样就能把它传给 `print!`/`writeln!`，避免先拼成 `String`。`fonts.rs` 里多处用它（如第 28、64、92 行）。

`write_axis` 与 `write_axis_with` 负责单条轴的格式化：

- [src/fonts.rs:99-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L99-L112) — `write_axis`：按 `axis.tag` 匹配，为标准轴选人类可读的名字和合适的值显示方式（如 `wght` 用 `FontWeight::from_wght` 转成字重数值，`opsz` 附加 `pt` 单位），非标准轴回退到标签字符串。
- [src/fonts.rs:114-128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L114-L128) — `write_axis_with`：统一拼出 `{name}: {min}-{max} (Default: {default})`，`min`/`max`/`default` 都经同一个显示函数转换，保证单位一致。

用一个具体例子把流程串起来。假设某可变字体有 `wght` 轴（100–900，默认 400）且 `variant.style = Normal, weight = 400, stretch = Normal`，则输出大致为：

```
  └ /path/to/font.ttf (Variable)
     Style: Normal
     Stretch: Normal
     Weight: 100-900 (Default: 400)
```

`Weight` 没有走「静态值」那行（因为 `standard.wght` 存在），而是作为轴以范围形式单独列出；`Style` 与 `Stretch` 因对应轴不存在，走静态值行。

#### 4.3.4 代码实践

**实践目标**：通过修改格式化逻辑，直观理解 `write_variant` 的两条分支。

**操作步骤**（源码阅读 + 本地观察型实践）：

1. 运行 `--variants`，在输出里找一个静态字体（无 `(Variable)` 标记）和一个可变字体（有 `(Variable)` 标记）。
2. 阅读 `write_variant`（[fonts.rs:57-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L57-L97)），对照输出确认：静态字体走了第 76-78 行，可变字体走了第 79-94 行。
3. （可选，本地修改型实践，仅在你的本地副本上做、勿提交）把第 78 行的 `Style: {style:?}, Weight: {weight}, Stretch: {stretch}` 暂时改成只打印 `Weight: {weight}`，重新 `cargo build`，再跑 `--variants`，观察静态字体那一行变短了——这能帮你确认这一行确实由该分支生成。

**需要观察的现象**：静态字体的「Style/Weight/Stretch」一行随你的修改而变化，可变字体部分不受影响。

**预期结果**：输出变化只发生在静态字体分支，印证两条路径的独立性。若不便修改源码，则纯阅读源码并对照默认输出即可（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：对于一个可变字体，如果它同时有 `wght` 轴，`write_variant` 会同时打印「`Weight: {weight}`」静态行和「`Weight: 100-900 (Default: 400)`」轴行吗？
**答案**：不会。第 85 行 `if standard.wght.is_none()` 守卫确保：只有当 `wght` 轴**不存在**时才打印静态 `Weight` 行；存在时则跳过静态行，改由第 91-93 行的轴循环以范围形式打印。这样避免了「字重信息重复」。

**练习 2**：`write_axis` 对 `opsz`（Optical Size）轴的值显示有何特殊处理？
**答案**：它在显示函数里给数值附加了 `pt` 单位（`write!(f, "{v}pt")`，见 [fonts.rs:107-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs#L107-L109)），所以光学尺寸轴会显示成类似 `Optical Size: 8-144pt (Default: 12pt)`，而其他轴不带单位。

---

### 4.4 惰性发现：SystemWorld 如何复用 discover_fonts

#### 4.4.1 概念说明

`discover_fonts` 不仅服务于 `fonts` 命令，真正的编译过程也用它——`SystemWorld`（u2-l1 讲过的「编译器与操作系统的桥梁」）在内部持有字体仓库。但二者有一个关键差别：

- `fonts` 命令：**立即**调用 `discover_fonts`，因为要马上打印结果。
- `SystemWorld`：**惰性**调用 `discover_fonts`，只有在编译器第一次真正查询字体时才扫描。

扫描字体（尤其是遍历系统字体目录）是相对昂贵的 I/O 操作。如果某次编译根本不碰字体（理论上极少见），或者只是想尽快开始编译，惰性求值就能把这笔开销推迟到「确有需要」的时刻。这套机制由 Rust 标准库的 `LazyLock` 实现。

#### 4.4.2 核心流程

```
SystemWorld::new(...):
    ...
    fonts: LazyLock::new( 闭包: { discover_fonts(&world_args.font) } )   # 注册但暂不执行
    ...

# 之后编译器调用 World::book() / World::font(index) 时:
book(index)  -> self.fonts.book()      # 首次访问触发闭包，执行 discover_fonts
font(index)  -> self.fonts.font(index)

# watch 模式下，编译开始前可强制扫描:
scan_fonts() -> LazyLock::force(&self.fonts)   # 立即执行闭包（若尚未执行）
```

`LazyLock` 保证：闭包**最多执行一次**，且在多线程下也是安全的；首次访问之后的所有访问都直接复用其结果。

#### 4.4.3 源码精读

`SystemWorld` 的 `fonts` 字段类型本身就把「惰性」写进了签名：

- [src/world.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L31) — `fonts: LazyLock<FontStore, Box<dyn Fn() -> FontStore + Send + Sync>>`。第二个类型参数是一个 boxed 闭包，正是 `LazyLock::new` 接受的初始化函数。

在 `SystemWorld::new` 里，这个闭包被注册为「调用 `crate::fonts::discover_fonts`」，但此刻并不执行：

- [src/world.rs:76-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L76-L85) — 构造 `SystemWorld` 时，`fonts` 字段用 `LazyLock::new(Box::new(|| crate::fonts::discover_fonts(&world_args.font)))` 初始化。注意它复用的正是 4.1 节那个 `discover_fonts`，参数同样来自 `WorldArgs.font`（与 `FontArgs` 同构）。

`World` trait 的两个字体相关方法直接委托给这个惰性仓库：

- [src/world.rs:122-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L122-L124) — `fn book(&self) -> &LazyHash<FontBook> { self.fonts.book() }`：返回字体目录册。首次调用会触发 `LazyLock` 执行 `discover_fonts`。
- [src/world.rs:138-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L138-L140) — `fn font(&self, index: usize) -> Option<Font> { self.fonts.font(index) }`：按索引惰性加载真实字体数据。

`scan_fonts` 提供了「强行立即扫描」的开关，供 watch 模式使用：

- [src/world.rs:109-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L109-L114) — `scan_fonts(&mut self)`：用 `LazyLock::force(&self.fonts)` 强制执行初始化闭包。注释说明「若字体已经扫描过则什么都不做」。在 watch 模式下，CLI 会在首轮编译前调用它，好让被发现的字体文件也被纳入文件监听集合（这样新增/改动字体会触发重编译，详见 u2-l5）。

> 一句话总结「复用」：`discover_fonts` 是字体发现的**唯一真相源**，`fonts` 命令和 `SystemWorld` 都调用它，区别仅在「立即求值」还是「惰性求值」。这也意味着 `--font-path`、`--ignore-system-fonts` 等开关对 `typst compile` / `typst watch` 与 `typst fonts` 的行为完全一致。

#### 4.4.4 代码实践

**实践目标**：确认编译时与 `fonts` 命令用的是同一套字体发现逻辑。

**操作步骤**：

1. 用 `fonts` 命令查看某个自定义目录下的字体族：
   ```bash
   ./target/debug/typst fonts --ignore-system-fonts --ignore-embedded-fonts \
       --font-path /tmp/myfonts
   ```
2. 写一个极简文档 `/tmp/hello.typ`，内容为 `#set text(font: "<你在上一步看到的族名>"); Hello`。
3. 用**相同的**字体开关编译它：
   ```bash
   ./target/debug/typst compile /tmp/hello.typ \
       --ignore-system-fonts --ignore-embedded-fonts \
       --font-path /tmp/myfonts
   ```

**需要观察的现象**：第 1 步能发现的字体族，在第 3 步编译时同样可用；若漏掉 `--font-path`，编译会因找不到字体而回退到默认字体（或报警告）。

**预期结果**：`fonts` 命令的可见字体集合 = 编译时的可见字体集合，验证二者共用 `discover_fonts`。若族名拼写不确定，可先去掉 `#set text(...)` 让文档用默认字体，确认流程跑通后再指定自定义族名（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SystemWorld` 用 `LazyLock` 而不是在 `new` 里直接调用 `discover_fonts`？
**答案**：为了把「扫描所有字体目录」这一相对昂贵的 I/O 推迟到真正需要字体时。`LazyLock` 还保证初始化闭包在多线程下最多执行一次，安全且无重复扫描。

**练习 2**：`scan_fonts()`（world.rs:112-114）调用 `LazyLock::force`。如果在此之前 `book()` 已经被调用过，`force` 会重新扫描一次吗？
**答案**：不会。`LazyLock` 的初始化是「至多一次」的——一旦闭包执行过，无论是因为 `book()` 触发还是 `force` 触发，后续的 `force` 直接返回已缓存的结果。`scan_fonts` 的注释也明确写了「Does nothing if the fonts were already scanned」。

---

## 5. 综合实践

把本讲的「三源组合 + 变体展示 + 编译复用」串起来，完成下面这个小任务。

**任务**：排查「我安装的字体为什么在 Typst 里没生效」。

1. 用 `typst fonts`（不加任何忽略开关）完整列出当前可见字体族，确认你的目标字体是否在列：
   ```bash
   ./target/debug/typst fonts | grep -i <你的字体名>
   ```
2. 若不在，用 `--font-path` 显式指向字体所在目录再列一次：
   ```bash
   ./target/debug/typst fonts --font-path /目录 | grep -i <你的字体名>
   ```
3. 找到后，用 `--variants` 查看它的变体与可变字体轴，记下族名的**精确拼写**与是否有 `(Variable)` 标记：
   ```bash
   ./target/debug/typst fonts --variants --font-path /目录
   ```
4. 用**完全相同的** `--font-path` 编译一份引用该字体的文档，验证它能被正确使用：
   ```bash
   ./target/debug/typst compile doc.typ --font-path /目录
   ```

**完成后，你应当能够解释**：

- 你的字体来自系统 / 内嵌 / 自定义路径中的哪一类（对照 `discover_fonts`）。
- 它是静态字体还是可变字体，有哪些轴（对照 `write_variant` / `write_axis`）。
- 为什么 `typst fonts` 看得到的字体，`typst compile` 也一定能用（因为二者共用 `discover_fonts`，且 `SystemWorld` 以 `LazyLock` 惰性复用它）。

---

## 6. 本讲小结

- `discover_fonts` 是字体发现的**唯一真相源**：按「系统 → 内嵌 → 自定义路径」顺序把字体累加进 `FontStore`，由 `--ignore-system-fonts` / `--ignore-embedded-fonts` / `--font-path` 三个开关控制；内嵌字体分支受 `embedded-fonts` feature（默认开启）条件编译保护。
- `fonts` 命令默认只列字体族名；`--variants` 会用 `├`/`└` 树形字符展开每个族下所有变体的路径、Style/Weight/Stretch 与可变字体轴。
- `write_variant` 区分静态字体（一行 Style/Weight/Stretch）与可变字体（`(Variable)` + 逐条轴）；对可变字体，**只有当某标准轴不存在时**才打印其静态值，存在的轴由 `write_axis` 以 `名称: min-max (Default: default)` 范围形式列出。
- 磁盘字体的路径通过把 `source` 向下转型为 `FontPath` 取得；内嵌字体无路径，显示为 `(Embedded)`。
- `SystemWorld` 用 `LazyLock` **惰性**复用同一个 `discover_fonts`，经 `book()` / `font()` 喂给编译器；`scan_fonts()` 可在 watch 模式下强制提前扫描，以便把字体文件纳入监听集合。
- 因此 `typst fonts` 与 `typst compile` / `typst watch` 的字体可见性完全一致——命令行开关对二者同等生效。

---

## 7. 下一步学习建议

- **横向承接**：本讲的 `FontArgs` 实际上是 `WorldArgs` 的一部分（被 `compile`/`watch`/`eval`/`query` 共享，见 u1-l3）。后续 u3-l2「包存储与解析」会讲 `WorldArgs` 里的另一半——`PackageArgs`，它和 `FontArgs` 一样被 `SystemWorld::new` 消费，可以对照阅读 [src/packages.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs) 与本讲的 [src/fonts.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/fonts.rs)。
- **纵向深入**：想看 `scan_fonts()` 在 watch 循环中究竟何时被调用、字体文件如何被纳入监听，请继续学习 u2-l5「Watch 模式与增量重编译」。
- **延伸阅读（仓库外）**：`FontStore` / `FontBook` / `system()` / `embedded()` / `scan()` 的实现都在外部 crate `typst-kit` 中，本仓库不含其源码；若想深入字体加载细节，可到 typst 仓库的 `crates/typst-kit/src/fonts` 目录下阅读（注意其代码不在本 HEAD 的永久链接范围内）。
