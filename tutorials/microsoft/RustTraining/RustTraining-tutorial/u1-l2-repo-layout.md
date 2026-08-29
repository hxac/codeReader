# 目录结构：一个文档工程仓库的布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出仓库中每个顶层目录与关键文件的职责（七个书籍目录、`xtask/`、`docker/`、`.github/`、`.cargo/` 以及根目录的配置与许可文件）。
2. 理解 `xtask/src/main.rs` 中的 `BOOKS` 常量是「磁盘目录」与「构建系统」之间的注册契约：目录只有出现在 `BOOKS` 里才会被构建。
3. 区分这个仓库的两层结构：**内容层**（七本 mdBook 书籍）与**基础设施层**（xtask 构建工具、Docker 运行时、GitHub Actions 流水线）。

本讲承接上一讲（u1-l1）建立的全景认知：我们已经知道 RustTraining 是一个「文档工程」，唯一的 Rust 代码是构建工具 xtask。本讲把镜头拉近，逐个看清仓库里每个东西是干什么的。

## 2. 前置知识

- **mdBook**：一个用 Markdown 写书的静态站点生成器。每本书是一个目录，里面放 `book.toml`（配置）和 `src/SUMMARY.md`（章节目录），构建后变成一个可浏览的 HTML 网站。上一讲已介绍，本讲只需要这个层面的理解。
- **Cargo workspace（工作空间）**：Cargo 允许一个仓库里装多个 crate（Rust 的编译单元），由根目录的 `Cargo.toml` 统一管理。本仓库的 workspace 只有一个成员：`xtask`。
- **约定优于配置 vs 显式注册**：有些系统会自动扫描目录发现内容；RustTraining 不是——它用一个手写的常量 `BOOKS` 显式列出所有书。这种「注册表」模式让构建行为完全可预测，代价是新增内容必须改代码。这是本讲最重要的思维模型。
- **slug（短标识符）**：`BOOKS` 里每本书的第一个字段，例如 `"async-book"`，它同时就是磁盘上的目录名。一个字符串身兼两职，这正是「契约」的体现。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 面向人的书籍清单（表格）、五级难度说明、本地构建命令 |
| [Cargo.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/Cargo.toml) | 根 workspace 定义，声明唯一的成员 `xtask` |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | 构建工具全部源码：`BOOKS` 注册表、批量构建、落地页生成、静态服务器 |
| [.cargo/config.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml) | 一行别名，让 `cargo xtask` 等价于 `cargo run --package xtask --` |
| [xtask/Cargo.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/Cargo.toml) | xtask crate 自身的依赖声明（仅 `ctrlc`） |
| [.gitignore](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.gitignore) | 声明 `site/`、`docs/`、`target/` 为生成物，不入库 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**顶层目录职责** → **BOOKS 注册表** → **workspace 组成**。

### 4.1 顶层目录职责

#### 4.1.1 概念说明

打开仓库第一眼会看到两类东西混在一起：七个「书目录」和一堆「基础设施」。先在脑中把它们分成两层：

- **内容层**：`async-book/`、`c-cpp-book/`、`csharp-book/`、`engineering-book/`、`python-book/`、`rust-patterns-book/`、`type-driven-correctness-book/`。每个目录就是一本书的全部原料（Markdown + 配置），没有任何可执行代码。
- **基础设施层**：`xtask/`（构建工具）、`docker/`（容器化运行时）、`.github/`（CI 流水线）、`.cargo/`（cargo 别名）。它们不为读者提供内容，而是让内容能被构建、预览、部署。

一个关键事实：**构建产物目录 `site/` 和 `docs/` 不在仓库里**，因为它们由 xtask 在构建时生成，且被 `.gitignore` 排除。

#### 4.1.2 核心流程

仓库的静态布局可以这样描述：

```text
RustTraining/                     ← 仓库根
│
├── 内容层（七本书，每个目录结构相同）
│   ├── async-book/                # 🔵 Deep Dive：Async Rust
│   ├── c-cpp-book/                # 🟢 Bridge：面向 C/C++ 背景
│   ├── csharp-book/               # 🟢 Bridge：面向 C#/Swift/Java 背景
│   ├── python-book/               # 🟢 Bridge：面向 Python 背景
│   ├── rust-patterns-book/        # 🟡 Advanced：高级模式
│   ├── type-driven-correctness-book/  # 🟣 Expert：类型驱动正确性
│   └── engineering-book/          # 🟤 Practices：工程实践
│       每本书内部：
│       ├── book.toml              # mdBook 配置
│       ├── mermaid-init.js        # 图表初始化脚本
│       ├── mermaid.min.js         # mermaid 渲染库
│       └── src/
│           ├── SUMMARY.md         # 章节目录（决定侧边栏导航）
│           └── ch00-*.md ~ ch17-*.md   # 章节正文
│
├── 基础设施层
│   ├── xtask/                     # Rust 构建工具（本仓库唯一 Rust 代码）
│   │   ├── Cargo.toml
│   │   └── src/main.rs
│   ├── docker/                    # Dockerfile、nginx.conf、compose.yaml、README
│   ├── .github/workflows/         # pages.yml（Pages 部署）、docker.yml（镜像 CI）
│   └── .cargo/config.toml         # cargo xtask 别名
│
├── 根配置与治理文件
│   ├── Cargo.toml                 # workspace：members = ["xtask"]
│   ├── Cargo.lock                 # 依赖锁定（自动生成）
│   ├── README.md                  # 面向人的总入口
│   ├── CONTRIBUTING.md / SECURITY.md / CODE_OF_CONDUCT.md
│   ├── LICENSE (MIT) / LICENSE-DOCS (CC-BY-4.0)
│   ├── .nojekyll                  # 告诉 GitHub Pages 跳过 Jekyll 处理
│   └── .gitignore                 # 排除 site/、docs/、target/ 等生成物
│
└── （构建时才出现，不入库）
    ├── site/                      # cargo xtask build 的输出，本地预览用
    └── docs/                      # cargo xtask deploy 的输出，Pages 用
```

数据流向：**书目录（原料）→ xtask（构建）→ site/ 或 docs/（产物）→ 本地服务器或 GitHub Pages（读者）**。

#### 4.1.3 源码精读

**证据一：README 的书籍表格与磁盘目录一一对应。** [README.md:47-55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55) 列出七本书的链接、级别和受众，链接里的路径段（如 `async-book/`）就是磁盘目录名。这是「给人读」的清单。

**证据二：`.gitignore` 证明 `site/` 和 `docs/` 是生成物。** [.gitignore:4-7](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.gitignore#L4-L7) 明确把 `site/`、`docs/`、`target/` 排除在版本库之外，注释写着 `# xtask output`。所以你在仓库里永远看不到这两个目录，它们只存在于执行过构建的本地机器或 CI runner 上。

**证据三：书目录的内部结构是统一的。** 以 async-book 为例，目录里只有 `book.toml`、`mermaid-init.js`、`mermaid.min.js` 和 `src/`（`SUMMARY.md` 加 18 个 `ch*.md` 章节文件）。这种高度一致的结构正是 xtask 能用同一个循环构建所有书的前提（下一模块会看到）。

**证据四：README 的维护者命令把目录职责串成了流程。** [README.md:94-98](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L94-L98) 列出四个命令，注释里直接点明了输出目录的分工：

```text
cargo xtask build               # Build all books into site/ (local preview)
cargo xtask serve               # Build and serve at http://localhost:3000
cargo xtask deploy              # Build all books into docs/ (for GitHub Pages)
cargo xtask clean               # Remove site/ and docs/
```

**证据五：根目录的 `.nojekyll` 是一个空文件。** 它与 xtask 在每次构建输出里写入的 `.nojekyll`（见 [xtask/src/main.rs:165-166](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L165-L166)）作用相同：GitHub Pages 默认会用 Jekyll 处理静态目录，下划线开头的文件等会被丢弃，这个空文件让 Pages 跳过 Jekyll、原样发布。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立一张属于自己的仓库地图，之后读任何文件都能立刻定位它在哪一层。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -la`，对照上面 4.1.2 的目录树，亲手把目录树画一遍（纸笔或任意画图工具均可）。
   - 为每个顶层目录和根文件写**一行**职责注释，例如 `docker/ —— 把构建产物装进 nginx 容器运行`。
   - 执行 `ls async-book/` 和 `ls async-book/src/ | head`，再随机挑另一本书（如 `python-book/`）执行同样命令，确认七本书的内部结构彼此一致。
3. **需要观察的现象**：七个 `*-book` 目录的内部布局完全相同（`book.toml` + `src/SUMMARY.md` + 章节文件）；仓库里确实没有 `site/` 和 `docs/`。
4. **预期结果**：你得到一张两层（内容层 / 基础设施层）注释齐全的目录树。（待本地验证：具体文件数量因书而异，`async-book` 有 18 个章节文件，其他书可能略有不同。）

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录已经有一个 `.nojekyll`，为什么 xtask 每次构建还要在输出目录里再写一个？

**答案**：根目录的 `.nojekyll` 只保护仓库本身被 Pages 从分支发布时的场景；而 `cargo xtask deploy` 生成的 `docs/`（以及 CI 上传的 `site/` 产物）是一个**独立的**被发布的目录树，GitHub Pages 只看发布目录里有没有 `.nojekyll`。所以 xtask 在 [xtask/src/main.rs:166](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L166) 每次都往输出目录写一个，确保无论以哪种方式发布都生效。

**练习 2**：判断对错：「往仓库里新建一个 `my-book/` 目录并放入合法的 `book.toml` 和 `src/SUMMARY.md`，运行 `cargo xtask build` 后它会自动出现在 `site/` 里。」

**答案**：错。xtask 不扫描磁盘，只遍历 `BOOKS` 常量（见 4.2）。未注册的目录对构建系统完全不可见——不会报错，也不会被构建。要让新书被收录，必须把它加进 `BOOKS`（这正是单元四 u4-l4 综合实战的主题）。

### 4.2 BOOKS 注册表

#### 4.2.1 概念说明

上一讲已经提出「书籍元数据双源维护」：README 的表格给人读，xtask 的 `BOOKS` 常量给机器用。本讲深入这第二源，看它如何充当**注册契约**。

`BOOKS` 是一个编译期常量数组，每个元素是四元组 `(slug, title, description, category)`：

- **slug**：如 `"async-book"`，双重身份——既是磁盘目录名，也是输出目录名和落地页链接。
- **title / description**：落地页卡片上显示的标题与描述。
- **category**：五级分类的机器值（`bridge` / `deep-dive` / `advanced` / `expert` / `practices`），决定落地页卡片的配色标签。

「契约」的含义是：**磁盘目录和 `BOOKS` 条目必须互相对应**。缺了任何一边，链路就断——目录存在但未注册 → 不构建；注册了但目录不存在 → 构建时跳过并告警。

#### 4.2.2 核心流程

`build_to` 函数消费 `BOOKS` 的流程（伪代码）：

```text
输出目录 = 项目根/site（或 docs）
若输出目录已存在 → 整个删掉重建
for (slug, _, _, _) in BOOKS:            # 只遍历注册表，不扫描磁盘！
    书目录 = 项目根/slug
    若书目录不存在:
        打印 "✗ {slug}/ not found, skipping"
        continue                          # 跳过这一本，不影响其他书
    在书目录下执行: mdbook build --dest-dir 输出目录/slug
    成功 → 打印 "✓ {slug}"，计数 +1
打印 "{ok}/{BOOKS.len()} books built"
用 BOOKS 的元数据生落地页 index.html
写 .nojekyll
```

注意两个不对称的失败方向：

| 状态 | 行为 |
|---|---|
| 目录存在，未注册到 `BOOKS` | 完全静默——构建系统根本不知道它存在 |
| 已注册到 `BOOKS`，目录不存在 | 打印 `✗ xxx-book/ not found, skipping`，继续构建其他书 |

#### 4.2.3 源码精读

**`BOOKS` 常量的定义。** [xtask/src/main.rs:8-52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) 用一条文档注释 `/// (slug, title, description, category)` 说明四元组结构，随后列出全部七本书。节选前两条：

```rust
/// (slug, title, description, category)
const BOOKS: &[(&str, &str, &str, &str)] = &[
    (
        "c-cpp-book",
        "Rust for C/C++ Programmers",
        "Move semantics, RAII, FFI, embedded, no_std",
        "bridge",
    ),
    // ... 共七条
];
```

这是一个 `&[( &str, &str, &str, &str )]`——字符串四元组的切片引用。没有结构体、没有 enum，Rust 元组数组在数据量小、形状固定时是最轻量的选择。

**定位项目根的方式。** [xtask/src/main.rs:54-59](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L54-L59) 用编译期宏 `env!("CARGO_MANIFEST_DIR")`（xtask crate 自身的位置）取父目录得到仓库根。因为 slug 是相对于仓库根解析的，这一步是「slug → 磁盘目录」翻译的基础。

**slug 如何变成路径，以及「缺目录则跳过」分支。** [xtask/src/main.rs:140-145](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L140-L145) 是契约的执行现场：

```rust
for &(slug, _, _, _) in BOOKS {
    let book_dir = root.join(slug);
    if !book_dir.is_dir() {
        eprintln!("  ✗ {slug}/ not found, skipping");
        continue;
    }
```

`root.join(slug)` 就是「契约」的字面实现：slug 被直接当作仓库根下的目录名拼接。`is_dir()` 检查失败时打印到 stderr 并 `continue`，让一本书的缺失不会拖垮整个批量构建。

**逐书调用 mdbook。** [xtask/src/main.rs:146-152](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L146-L152) 在书目录里以子进程方式运行 `mdbook build --dest-dir <输出目录>/<slug>`，把每本书渲染到统一的输出树下各自的子目录。

**计数与收尾。** [xtask/src/main.rs:161-167](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L161-L167) 打印 `{ok}/{BOOKS.len()} books built`——分母是注册表长度而非磁盘目录数；随后 `write_landing_page(&out)` 再次以 `BOOKS` 为数据源生落地页，最后写 `.nojekyll`。**同一份注册表被消费两次**（构建循环 + 落地页），这就是「单一数据源」的价值：改一处，两处行为同时更新。

#### 4.2.4 代码实践（源码阅读型 + 本地核对）

1. **实践目标**：亲眼验证「`BOOKS` 的七个 slug 与磁盘目录一一对应」这条契约当前是成立的。
2. **操作步骤**：
   - 在仓库根执行 `ls -d -- *-book | sort`，得到磁盘上的书目录列表。
   - 执行 `grep -oE '"[a-z-]+-book"' xtask/src/main.rs | sort -u`，从源码里抽出 `BOOKS` 的 slug（slug 只含小写字母和连字符，而 title 含空格和大写，所以这个正则只会命中 slug）。
   - 肉眼对比两份列表，或用 `diff <(ls -d -- *-book | sort) <(grep -oE '"[a-z-]+-book"' xtask/src/main.rs | sort -u)` 一步到位。
3. **需要观察的现象**：两份列表各有 7 项，内容完全一致（`async-book`、`c-cpp-book`、`csharp-book`、`engineering-book`、`python-book`、`rust-patterns-book`、`type-driven-correctness-book`）。
4. **预期结果**：`diff` 无输出（退出码 0）。待本地验证。
5. **思考题（规格要求）**：只新建目录而不注册到 `BOOKS` 会发生什么？——答案见 4.2.2 的表格和练习 3：**什么都不会发生**，这正是需要警惕的地方，因为没有报错，也没有任何提示。

#### 4.2.5 小练习与答案

**练习 1**：`BOOKS` 中 `engineering-book` 的 category 是什么？落地页上会显示成什么文字？

**答案**：category 是 `"practices"`（[xtask/src/main.rs:47-51](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L47-L51)）。`category_label` 函数（[xtask/src/main.rs:170-179](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L170-L179)）把它映射为展示文字 `Practices`，未匹配的值会原样透传（`_ => cat` 兜底分支）。

**练习 2**：如果把 `BOOKS` 里 `"python-book"` 误写成 `"pythonbooks"`（磁盘目录仍是 `python-book`），运行 `cargo xtask build` 会看到什么？构建会整体失败吗？

**答案**：会在 stderr 看到 `✗ pythonbooks/ not found, skipping`，其余六本正常构建，最后打印 `6/7 books built`。构建**不会**整体失败，`build_to` 的 `continue` 逻辑保证了单本缺失只降级不中断；但落地页也会少这张卡片，因为落地页同样以 `BOOKS` 为数据源。

**练习 3**：为什么说 `BOOKS` 是「契约」而不是「缓存」或「配置文件」？

**答案**：它是写在 Rust 源码里的编译期常量，改它就要改代码、重新编译；它不是从磁盘扫描结果的缓存（磁盘才是被校验的一方），也不是外置配置（没有 TOML/JSON 文件）。它声明的是一种双方必须满足的约定：磁盘上得有同名目录，输出目录、落地页链接也由它派生。这种设计牺牲了「自动发现」的便利，换来构建行为的完全显式与可预测。

### 4.3 workspace 组成

#### 4.3.1 概念说明

一个纯文档项目为什么有 `Cargo.toml` 和 `Cargo.lock`？因为构建工具 xtask 本身是 Rust 写的。根 `Cargo.toml` 定义了一个 **workspace**，把 `xtask` 纳入管理；`.cargo/config.toml` 再配一个别名，让维护者能用 `cargo xtask build` 这样自然的命令驱动它。

三个文件各司其职：

| 文件 | 职责 |
|---|---|
| `Cargo.toml`（根） | 声明 workspace 及其成员 |
| `xtask/Cargo.toml` | xtask 这个 crate 自己的名字、版本、依赖 |
| `.cargo/config.toml` | 定义 `xtask` 别名，桥接「cargo 命令」与「运行 xtask 二进制」 |

#### 4.3.2 核心流程

```text
用户输入: cargo xtask build
        │
        ├─ cargo 向上查找 .cargo/config.toml → 发现别名
        │   xtask = "run --package xtask --"
        │
└─ 等价展开: cargo run --package xtask -- build
        │
        ├─ workspace（根 Cargo.toml, members=["xtask"]）里找到 xtask crate
        ├─ 编译 xtask/src/main.rs → 二进制
        └─ 以 "build" 为参数运行它 → main() 的 match 分发到 cmd_build()
```

注意 `--` 的作用：它把之后的参数原样传给 xtask 二进制，cargo 自己不解析它们。另外，cargo 会从当前目录向上查找 `.cargo/config.toml`，所以在仓库任意子目录里执行 `cargo xtask` 都能命中这个别名。

#### 4.3.3 源码精读

**根 workspace 定义，只有三行。** [Cargo.toml:1-3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/Cargo.toml#L1-L3)：

```toml
[workspace]
resolver = "2"
members = ["xtask"]
```

`members = ["xtask"]` 说明整个 workspace 只有一个成员 crate；`resolver = "2"` 启用新版依赖解析器（特性统一按目标平台解析），是 2021 版之后的推荐默认。没有 `[package]` 节——根不是 crate，只是管理者。

**xtask crate 自身的清单。** [xtask/Cargo.toml:1-8](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/Cargo.toml#L1-L8) 声明 `name = "xtask"`、`edition = "2021"`，`publish = false` 表示永不发布到 crates.io（它只服务于本仓库），依赖只有一个 `ctrlc = "3.4"`（供 `serve` 命令优雅退出用，细节在 u2-l6）。

**别名定义。** [.cargo/config.toml:1-2](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml#L1-L2)：

```toml
[alias]
xtask = "run --package xtask --"
```

**xtask 的入口把参数分发到子命令。** [xtask/src/main.rs:61-77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77) 用 `match` 匹配第一个参数：`build`/`serve`/`deploy`/`clean` 各自对应一个函数，`help` 或无参打印用法并以 0 退出，未知命令打印到 stderr 并以 1 退出。本讲只需知道分发存在，逐行精读留给 u2-l2。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：确认「workspace 只有一个成员」并且别名确实指向它。
2. **操作步骤**：
   - 依次阅读上面三个文件（都很短，总共不超过 15 行有效内容）。
   - 在仓库根执行 `cargo metadata --no-deps --format-version 1 | head -c 400`，观察输出的 `"packages"` 字段里是否只有 `xtask` 一个包。（待本地验证：该命令需要 cargo 可用。）
   - 执行 `cargo xtask help`，预期打印用法后以退出码 0 结束。
3. **需要观察的现象**：`packages` 数组里只有 `"name":"xtask"` 一项；`cargo xtask help` 输出四个子命令的用法说明（与 [xtask/src/main.rs:87-95](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L87-L95) 的文本一致）。
4. **预期结果**：三个文件构成的最小 workspace 被完整走通：别名 → 成员查找 → 编译运行。若 `cargo metadata` 不可用，仅完成阅读部分也可。

#### 4.3.5 小练习与答案

**练习 1**：根 `Cargo.toml` 里为什么没有 `[package]` 节？如果给它加上 `name` 和 `version` 会怎样？

**答案**：因为根目录只需要扮演 workspace 管理者，本身不是可编译的 crate——唯一的代码在 `xtask/` 子目录里。若给根也加 `[package]`，根会变成一个虚拟性消失的「根 crate」，通常还需 `src/main.rs` 之类的目标文件才能编译，反而搅乱了「文档仓库 + 一个工具 crate」的简单结构。（这是 Cargo 的通用行为，不是本仓库特有。）

**练习 2**：`xtask/Cargo.toml` 里 `publish = false` 有什么实际意义？

**答案**：禁止把这个 crate 发布到 crates.io。xtask 深度绑定本仓库的目录结构（`project_root()`、`BOOKS` 里的 slug），对任何其他项目都没有复用价值，发布只会造成误导；同时这也让 `cargo publish` 之类的误操作直接失败。

**练习 3**：删掉 `.cargo/config.toml` 后，`cargo xtask build` 会发生什么？怎么补救？

**答案**：cargo 不再认识 `xtask` 这个子命令，会把它当作普通命令别名缺失而报错（提示未知命令或列出版本帮助，具体文案随 cargo 版本）。补救方式是不用别名，直接执行等价命令 `cargo run --package xtask -- build`，或恢复该文件。这也解释了为什么这个文件虽小却是基础设施层不可缺少的一环。

## 5. 综合实践

**任务：制作并验证你的「仓库地图 + 注册契约核对」笔记。**

1. 完整做完 4.1.4 的目录树手绘（含每个目录一行职责注释）。
2. 完成 4.2.4 的 slug 核对，把你使用的命令和对比结果（两份 7 项列表）记录在笔记里。
3. 用一个「反证实验」加深对契约的理解：在仓库根新建一个空目录 `my-book/`（或复制 `async-book` 的 `book.toml`/`src/` 进去），运行 `cargo xtask build`，观察构建输出里完全没有 `my-book` 的踪影，落地页（`site/index.html` 或 `cargo xtask serve` 后访问 `http://localhost:3000`）也没有它的卡片；构建完成后删除 `my-book/` 与 `site/`（`cargo xtask clean`）恢复原状。
4. 在笔记末尾用三到五句话回答：**这个仓库为什么选择「显式注册（BOOKS）」而不是「自动扫描目录」？** 提示：从构建可预测性、落地页排序与元数据（title/description/category 从哪来）、失败时的行为三个角度想。

验收标准：笔记能让你在不打开仓库的情况下复述每个顶层目录的职责、说出 BOOKS 四元组的含义，并解释两个不对称的失败方向。

## 6. 本讲小结

- 仓库分两层：**内容层**是七个结构完全相同的书籍目录（`book.toml` + `src/SUMMARY.md` + 章节文件），**基础设施层**是 `xtask/`、`docker/`、`.github/`、`.cargo/`；`site/` 与 `docs/` 是不入库的构建产物。
- [xtask/src/main.rs:8-52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) 的 `BOOKS` 常量是 (slug, title, description, category) 四元组注册表，slug 同时是磁盘目录名、输出目录名和落地页链接。
- 构建循环只遍历 `BOOKS` 不扫描磁盘：目录未注册 → 静默忽略；注册而目录缺失 → 打印 `✗ skipping` 并继续，最终计数为 `{ok}/7`。
- 同一份 `BOOKS` 被消费两次（批量构建 + 落地页生成），构成单一数据源。
- 根 `Cargo.toml` 是只有 `xtask` 一个成员的 workspace；`.cargo/config.toml` 的别名让 `cargo xtask <命令>` 等价于 `cargo run --package xtask -- <命令>`。
- 根目录与输出目录里的 `.nojekyll` 都是为了让 GitHub Pages 跳过 Jekyll 处理。

## 7. 下一步学习建议

- 下一讲 **u1-l3（本地构建与预览）**：亲手安装 `mdbook` 与 `mdbook-mermaid`，跑通 `cargo xtask build` / `serve` / `clean` 的完整循环，把本讲地图上的每个目录「激活」一遍。
- 想提前深入 xtask 源码的读者，可以直接通读 [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs)（全文件只有一个文件、无第三方重依赖），重点看 `build_to` 与 `write_landing_page` 如何消费 `BOOKS`——单元二会逐段精读。
- 对「一本书内部长什么样」感兴趣的读者，可提前浏览 `async-book/book.toml` 和 `async-book/src/SUMMARY.md`，那是 u1-l4 的主题。
