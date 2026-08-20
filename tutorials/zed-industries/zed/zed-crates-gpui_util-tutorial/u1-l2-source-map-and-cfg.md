# 源码地图：lib.rs 单文件布局与条件编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 凭一张「分区地图」说出 `src/lib.rs` 全部 604 行代码的九大功能区段，以及每个区段大致在哪一行。
2. 给任意一段代码标注它属于哪一类：**平台无关** / **仅 Windows** / **每个平台编译其一** / **仅测试**。
3. 解释 `#[cfg(target_os = "windows")]`、`#[cfg(not(...))]`、`cfg!(debug_assertions)` 三种写法的区别。
4. 在 Linux 或 macOS 机器上，不借助 Windows 电脑，验证 Windows 分支代码能否通过编译、`which` 依赖是否被引入。

本讲是「地图课」：不深挖每个工具的内部机制（那是第二单元各讲的任务），而是让你拿到任何一个函数名，都能立刻知道它在文件里的位置、受什么条件编译控制。

## 2. 前置知识

上一讲（u1-l1）我们已经知道：gpui_util 是 zed 工作区最底层的基础工具箱，全平台依赖只有 `log` 和 `anyhow`，`which` 仅在 Windows 目标引入。本讲需要再补充几个 Rust 语言概念。

### 2.1 属性（attribute）是什么

Rust 中形如 `#[...]` 的语法叫**属性**，是写给编译器的「附加说明」。例如：

- `#[test]`：告诉编译器「这是一个测试函数」。
- `#[macro_export]`：把宏导出到 crate 根，供其他 crate 使用。
- `#[cfg(...)]`：**条件编译**，本讲的主角。

### 2.2 `#[cfg]` 与 `cfg!` 的区别

`cfg` 是 configuration（配置）的缩写，Rust 编译器在**编译期**就知道一堆目标平台信息（操作系统、CPU 架构、指针宽度等）。两种用法：

| 写法 | 语义 | 两个分支是否都要通过编译 |
|---|---|---|
| `#[cfg(条件)]` | 条件不成立时，**整段代码不参与编译**，如同不存在 | 否，只有保留的分支需要合法 |
| `cfg!(条件)` | 求值为一个 `bool` **常量**，代码两个分支都参与编译 | 是 |

本讲会看到 `#[cfg]` 的多种条件：`target_os = "windows"`、`not(target_os = "windows")`、`target_pointer_width = "64"`，以及 `cfg!(debug_assertions)`（debug 构建为 `true`，release 构建为 `false`）。

### 2.3 平台门控依赖

条件编译不只作用于代码，还能作用于**依赖本身**。Cargo.toml 里可以声明「某个依赖只在编译特定目标时引入」，后面 4.1.3 会看到 gpui_util 的 `which` 就是这么写的。这意味着：在 Linux 上构建时，`which` 甚至不会被下载编译——这是底层工具库控制编译时间和体积的重要手段。

### 2.4 `#[test]` 与 `#[cfg(test)]`

单独的 `#[test]` 属性就足以让函数只在测试编译（`cargo test`）时生效，普通 `cargo build` 时该函数不会进入产物。很多项目还会额外包一层 `#[cfg(test)] mod tests { ... }` 把测试集中起来，但 gpui_util 选择了更轻的形式：测试函数直接写在实现代码旁边。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| [crates/gpui_util/src/lib.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs) | 604 | crate 门面：除 arc_cow 外**全部**公开工具都在这一个文件里 |
| [crates/gpui_util/src/arc_cow.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs) | 142 | 唯一子模块：`ArcCow` 智能指针及其全套 trait 委托实现 |
| [crates/gpui_util/Cargo.toml](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml) | 16 | 依赖声明（上一讲已精读，本讲只引用其中的平台门控段） |

一个有意思的事实：这个 crate 的「源码地图」简单到近乎朴素——没有 `src/` 下的多层目录、没有 `mod.rs`（zed 工程规范也禁止创建 `mod.rs`），全部逻辑摊平在 `lib.rs`。**地图越小，越应该一次记全**，这正是本讲的目标。

## 4. 核心概念与源码讲解

### 4.1 lib.rs 的 cfg 模块分区：一张地图看懂 604 行

#### 4.1.1 概念说明

`lib.rs` 采用「单文件分区」组织：按功能把代码切成九个纵向区段，从上到下依次是：

| # | 区段 | 行号范围 | 内容 | 编译类别 |
|---|---|---|---|---|
| 1 | Windows 平台工具 | L17–L146 | `CREATE_NO_WINDOW` 常量、`new_std_command`（两个互补实现）、`get_windows_system_shell` | 大部分**仅 Windows** |
| 2 | 通用小函数 | L148–L171 | `post_inc`（后置自增）、`measure`（计时） | 平台无关 |
| 3 | 宏与开发期辅助 | L173–L209 | `debug_panic!`、`some_or_debug_panic`、`maybe!` | 平台无关 |
| 4 | ResultExt | L210–L288 | 错误处理扩展 trait 及其实现 | 平台无关 |
| 5 | 日志内部实现 | L290–L336 | `log_error_with_caller`、自由函数 `log_err`、`DebugAsDisplay` | 平台无关（内部有极小的平台差异） |
| 6 | Future 适配器 | L338–L503 | `TryFutureExt` / `TryFutureExtBacktrace` 及三个包装 future | 平台无关 |
| 7 | defer | L505–L526 | `Deferred` 结构体与 `defer` 函数（RAII 延迟执行） | 平台无关 |
| 8 | TypeId 哈希器 | L528–L581 | `TypeIdHashBuilder`、`TypeIdHasher`、内嵌单元测试 | 平台无关 + 仅测试 |
| 9 | 排序工具 | L583–L603 | `truncate_to_bottom_n_sorted_by`（部分排序） | 平台无关 |

此外文件最顶部还有三行「零碎」：[crates/gpui_util/src/lib.rs:L1-L2](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L1-L2) 是历史注释（记录 `FutureExt`、`Timeout` 等已迁出的项，上一讲讲过），L4–L13 是只用 `std` 的导入，[crates/gpui_util/src/lib.rs:L15](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L15) 是唯一的模块声明 `pub mod arc_cow;`。

**为什么要这样摊平？** 这是一个只有约 600 行的基础库：区段之间几乎没有相互调用（`ResultExt` 调用 `log_error_with_caller`、Future 适配器也调用它，是少数例外），摊平反而让「找一个函数」变成一次滚动。当文件增长到需要分区检索时，才值得拆目录——arc_cow 就是因为自带 140 行 trait 实现而被单独拆出去的。

#### 4.1.2 核心流程

编译器处理这个文件时，对每个 `#[cfg]` 做如下决策（伪代码）：

```text
对 lib.rs 的每个顶层项（函数/常量/trait/宏/测试）:
    读取其 #[cfg(...)] 条件
    若条件在当前目标上为假:
        整个项从编译中剔除（连语法检查都可以跳过）
    否则:
        保留并照常编译
```

关键在于「剔除」是彻底的：在 Linux 上，`get_windows_system_shell` 里的 `use std::path::PathBuf` 甚至不会被解析。因此同一份源码可以放心使用 Windows 独有的 API，Unix 编译器根本看不见它们。

本 crate 用到的条件编译共有四种模式，值得逐一记住：

1. **仅 Windows**：`#[cfg(target_os = "windows")]`，如 `CREATE_NO_WINDOW`、`get_windows_system_shell`。
2. **互补对**：同一个函数名写两遍，一个 `#[cfg(target_os = "windows")]`、一个 `#[cfg(not(target_os = "windows"))]`，任何平台恰好编译其一——`new_std_command` 就是这种模式。
3. **嵌套细分**：Windows 函数内部再按 CPU 指针宽度分：`#[cfg(target_pointer_width = "64")]` / `#[cfg(target_pointer_width = "32")]`（决定查 `ProgramFiles(x86)` 还是 `ProgramW6432` 环境变量）。条件可以层层嵌套。
4. **编译期布尔**：`cfg!(debug_assertions)`（不是 `#[cfg]`），debug_panic! 宏用它让 panic 分支在 release 下编译为日志分支。

而依赖侧的流程（与 Cargo 配合）：

```text
cargo 解析 gpui_util 的依赖:
    若目标满足 cfg(target_os = "windows"):
        依赖集合 = { log, anyhow, which }
    否则:
        依赖集合 = { log, anyhow }
```

#### 4.1.3 源码精读

**互补对的范本：`new_std_command`。** Windows 版多了设置进程创建标志的步骤，其余完全一致：

[crates/gpui_util/src/lib.rs:L17-L32](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L17-L32) —— 定义 `CREATE_NO_WINDOW`（值 `0x0800_0000`，仅 Windows 编译），然后给出 `new_std_command` 的两个实现：Windows 版创建 `Command` 后调用 `creation_flags(CREATE_NO_WINDOW)` 避免子进程弹出控制台黑窗；非 Windows 版（`#[cfg(not(target_os = "windows"))]`）直接透传 `Command::new`。调用方完全感知不到平台差异——这正是互补对模式的价值。

**整块门控的最大区块：`get_windows_system_shell`。**

[crates/gpui_util/src/lib.rs:L34-L146](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L34-L146) —— 整个函数带 `#[cfg(target_os = "windows")]`，约 110 行在非 Windows 平台上完全不参与编译。内部包含三个嵌套函数（探测 ProgramFiles 目录、MSIX 安装目录、scoop 目录）和一个 `LazyLock` 静态量 `SYSTEM_SHELL`（L121–L143，按 9 个候选位置的优先级找到 pwsh 并缓存）。其中嵌套的条件编译在 [L39-L51](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L39-L51)：按 `target_pointer_width` 决定用哪个环境变量名。这个函数的探测逻辑本身是第三单元 u3-l1 的主题，本讲只需记住它的「位置 + 门控方式」。

**编译期布尔：`cfg!(debug_assertions)`。**

[crates/gpui_util/src/lib.rs:L173-L183](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L173-L183) —— `debug_panic!` 宏内部用 `if cfg!(debug_assertions)` 在「debug 构建 panic」与「release 构建记录带回溯的错误日志」之间切换。注意两个分支**都要通过编译检查**（这是 `cfg!` 与 `#[cfg]` 的本质区别），只是运行时走其一。

**平台内部微差异：`log_error_with_caller` 的路径分隔符处理。**

[crates/gpui_util/src/lib.rs:L294-L297](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L294-L297) —— 同一个 `let file` 声明了两次：非 Windows 直接用 `caller.file()`；Windows 先把 `\` 替换成 `/`。这是「平台无关函数内部的小 cfg」模式，与整块剔除不同，它只是让一行代码在不同平台有不同初值。

**依赖侧的门控。**

[crates/gpui_util/Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L11-L12) —— `[target.'cfg(target_os = "windows")'.dependencies]` 段声明 `which.workspace = true`。代码里唯一使用 `which` 的地方是 Windows 函数内的 [L130-L131](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L130-L131)（`which::which_global` 作为兜底探测）。依赖门控与代码门控必须配套：如果只在代码里 cfg 而依赖全平台引入，Unix 构建就白白背上 `which`；反之则 Windows 编译失败。

#### 4.1.4 代码实践

**实践：在 Linux/macOS 上验证 Windows 分支能否编译。**

1. **实践目标**：亲身体验「一份源码、多个目标」，确认 Windows 专属代码在非 Windows 主机上也能被编译器检查。

2. **操作步骤**：

   ```bash
   # 进入 zed 仓库根目录（本讲所有命令都在仓库根执行）

   # 第 1 步：编译当前平台的版本（非 Windows 主机上编译的是 not(windows) 分支）
   cargo check -p gpui_util

   # 第 2 步：查看是否已安装 Windows 目标的标准库
   rustup target list --installed

   # 第 3 步：若列表中没有 x86_64-pc-windows-msvc，先安装（只下载标准库，不需要 Windows 系统）
   rustup target add x86_64-pc-windows-msvc

   # 第 4 步：让编译器以 Windows 为目标检查本 crate
   cargo check -p gpui_util --target x86_64-pc-windows-msvc
   ```

3. **需要观察的现象**：
   - 两条 `cargo check` 都应以 `Finished` 结束、无 error。
   - 第 4 步的输出里应出现 `which` crate 的编译行（如 `Compiling which v8.x.x`），而第 1 步没有。

4. **预期结果**：Windows 分支（`new_std_command` 的 Windows 版、`get_windows_system_shell`、`CREATE_NO_WINDOW`）语法与类型检查全部通过，证明 `cargo check --target` 让你在任何主机上都能验证 cfg 门控代码。`check` 只做编译前端、不做链接，因此不需要 Windows 链接器。具体输出文本因本机工具链版本而异，**待本地验证**。

5. **如果无法安装目标**（例如离线环境）：退而求其次，用「阅读 + 永久链接」方式验证——对照 4.1.3 的四个链接逐段确认 `#[cfg]` 的配对关系是否完整（每个「仅 Windows」项是否都能在非 Windows 目标下被安全剔除）。

#### 4.1.5 小练习与答案

**练习 1**：如果不写 `#[cfg(not(target_os = "windows"))]` 而只保留 Windows 版 `new_std_command`，在 Linux 上编译 gpui_util 的下游 crate（如 fuzzy）会发生什么？

**答案**：Linux 上该函数根本不存在，下游所有 `gpui_util::new_std_command(...)` 调用都会报「找不到函数」的编译错误（E0425）。互补对模式的要点是：**任何目标上函数都存在**，只是实现不同，从而保证 API 表面跨平台恒定。

**练习 2**：`#[cfg(target_os = "windows")]` 和 `cfg!(target_os = "windows")` 都能表达「是否是 Windows」，`debug_panic!` 宏（L176）为什么选了后者？

**答案**：因为 `debug_panic!` 希望 panic 分支和日志分支**都保留在编译产物里**，由构建类型在运行前决定走哪条；如果用 `#[cfg(debug_assertions)]` 剔除，也能工作，但 `cfg!` 写成普通 `if` 的形式更直观，且两个分支都会接受完整的类型检查。反过来，`get_windows_system_shell` 若用 `cfg!` 写，其内部的 Windows API 调用就得在所有平台上通过编译，Unix 机器直接报错——所以整块 Windows 代码必须用 `#[cfg]` 剔除。

**练习 3**：数一数：lib.rs 中带 `#[cfg(target_os = "windows")]` 的顶层项有几个？

**答案**：4 个——`CREATE_NO_WINDOW`（L17–L18）、Windows 版 `new_std_command`（L20–L27）、`get_windows_system_shell`（L34–L146），再加上互补的 `#[cfg(not(target_os = "windows"))]` 版 `new_std_command`（L29–L32，条件相反）。若把函数内部的嵌套 cfg（L294–L297 的 windows/not(windows)、L39–L51 的指针宽度）也算上，条件编译注解总共出现 9 处。

### 4.2 arc_cow 子模块：唯一的子目录级模块

#### 4.2.1 概念说明

`arc_cow.rs` 是整个 crate 唯一被拆出的模块，通过 [crates/gpui_util/src/lib.rs:L15](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L15) 的 `pub mod arc_cow;` 引入，外部以 `gpui_util::arc_cow::ArcCow` 的路径使用。它只定义一个类型：

```rust
pub enum ArcCow<'a, T: ?Sized> {
    Borrowed(&'a T),
    Owned(Arc<T>),
}
```

即「要么借用一个引用，要么持有一份引用计数的所有权」。你可以把它理解为 `Cow<'a, T>` 的近亲，但 clone 一个 `Owned` 变体只递增 `Arc` 计数，不复制数据。它的完整用法是第二单元 u2-l6 的主题；本讲只关注**它的源码组织形态**——这是阅读 Rust 泛型库极佳的入门样本，因为它把「一个类型 + 一族 trait 委托实现」的标准布局完整示范了一遍。

#### 4.2.2 核心流程

整个文件 142 行，结构是机械而清晰的「三段式」：

```text
arc_cow.rs 的组织流程:
1. 定义枚举（L9-L12）           —— 唯一的数据定义，只有两个变体
2. 值语义 trait（L14-L52）       —— PartialEq/PartialOrd/Ord/Eq/Hash/Clone
                                    比较、哈希、克隆都作用于【指向的数据】
3. 构造与视图（L54-L141）        —— From 转换族 + Borrow/Deref/AsRef/Debug
                                    各种"变成 &T 或从别的类型造出来"的通道
```

几乎每个 impl 都遵循同一个委托模式：

```text
match self {
    Self::Borrowed(borrowed) => 对 borrowed 做事,
    Self::Owned(owned)       => 对 owned 做事,
}
```

这个模式值得记住：**二选一枚举的标准实现就是「分别处理两个变体，语义保持一致」**。读一遍 arc_cow.rs，你就掌握了快速读任何 Rust 枚举 wrapper 的方法——先找 match，再看两个分支是否对称。

#### 4.2.3 源码精读

**数据定义。**

[crates/gpui_util/src/arc_cow.rs:L9-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L9-L12) —— 定义 `ArcCow` 枚举：`Borrowed(&'a T)` 持有生命周期 `'a` 的引用，`Owned(Arc<T>)` 持有引用计数指针；`T: ?Sized` 允许 `str`、`[u8]` 这类非固定尺寸类型作为参数。

**值语义委托（比较与哈希按内容算）。**

[crates/gpui_util/src/arc_cow.rs:L14-L43](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L14-L43) —— 一口气实现 `PartialEq`、`PartialOrd`、`Ord`、`Eq`、`Hash` 五个 trait。以 `PartialEq` 为例（L14–L20）：先把两个操作数都通过 `as_ref()` 拿到 `&T`，再比较内容。也就是说，一个 `Borrowed("ab")` 和一个 `Owned(Arc::from("ab"))` 判等结果为 `true`——来源不同不影响值相等。

**Clone 是引用计数递增，不是拷贝。**

[crates/gpui_util/src/arc_cow.rs:L45-L52](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L45-L52) —— `clone` 对 `Borrowed` 复制引用（Copy 语义），对 `Owned` 调 `Arc::clone`（仅递增计数）。这是它比 `Cow` 便宜的关键。

**From 转换族：八条构造通道。**

[crates/gpui_util/src/arc_cow.rs:L54-L103](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L54-L103) —— 从 `&T`、`Arc<T>`、`&Arc<T>`、`String`、`&String`、`Cow<str>`、`Vec<T>`、`&str`（转 `[u8]`）共八种来源构造 `ArcCow`。注意规律：**拿引用进来就 Borrowed，拿所有权进来就 Owned**。

**视图委托：Deref / AsRef / Borrow。**

[crates/gpui_util/src/arc_cow.rs:L114-L123](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L114-L123) —— `Deref` 实现让 `ArcCow<str>` 可以直接当 `&str` 用（自动解引用）；[L105-L112](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L105-L112) 的 `Borrow` 与 [L125-L132](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/arc_cow.rs#L125-L132) 的 `AsRef` 返回同样的 `&T`。三者代码几乎相同，却必须分别实现，因为它们服务于不同的标准库场景（`Borrow` 用于 `HashMap` 键查找、`Deref` 用于自动解引用、`AsRef` 用于泛型收参）。

#### 4.2.4 代码实践

**实践：给 arc_cow.rs 的 impl 块做分类清单。**

1. **实践目标**：通过亲手分类，掌握「值语义委托 vs 视图委托 vs 构造转换」三分法，练出快速读枚举 wrapper 的肌肉记忆。

2. **操作步骤**：
   - 打开 arc_cow.rs，从 L14 到 L141 逐个 impl 阅读。
   - 画一张三列表格，把 10 个 impl（PartialEq、PartialOrd、Ord、Eq、Hash、Clone、8 个 From、Borrow、Deref、AsRef、Debug）填进对应列：
     - 值语义：PartialEq / PartialOrd / Ord / Eq / Hash / Clone
     - 构造转换：8 个 `From`
     - 视图委托：Borrow / Deref / AsRef / Debug
   - 验证规律：除 `Eq`（L34，空实现）和几个 `From` 外，是否每个 impl 体内都有 `match self { Self::Borrowed(..) => .., Self::Owned(..) => .. }`？

3. **需要观察的现象**：几乎每个实现的两个 match 分支都严格对称，唯一的差异是 `Borrowed` 分支直接用引用、`Owned` 分支多一步 `Arc` 解引用（`&**owned` 或 `owned.as_ref()`）。

4. **预期结果**：得到一张与 4.2.3 行号呼应的分类表；`Eq` 是唯一的「派生式空实现」（`impl<T: ?Sized + Eq> Eq for ArcCow<'_, T> {}`），因为它只是向编译器承诺「这个类型的 PartialEq 是自反的」，没有方法体。

5. 补充一个小实验（**示例代码**，非项目原有代码，可放进临时 bin 或 `cargo playground`）：

   ```rust
   use std::sync::Arc;
   use gpui_util::arc_cow::ArcCow;

   fn describe(s: &ArcCow<'_, str>) -> String {
       // Deref 生效：可以直接用 &str 的方法
       format!("len={} up={}", s.len(), s.to_uppercase())
   }

   let borrowed: ArcCow<str> = ArcCow::from("hello");
   let owned: ArcCow<str> = ArcCow::from(String::from("hello"));
   assert_eq!(describe(&borrowed), describe(&owned)); // PartialEq 按内容比较
   ```

   运行结果待本地验证（需要在 zed 工作区内以依赖方式引用 gpui_util，或复制枚举定义做等价实验）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 arc_cow 要拆成单独文件，而 `ResultExt`、`defer`、`TypeIdHasher` 都留在 lib.rs？

**答案**：因为它的代码量（142 行）几乎全部是同一个类型的一族 trait 实现，且相对自洽、不依赖 lib.rs 里的其他工具；单独成文件后 lib.rs 少掉四分之一体积。而 `ResultExt` 与 `log_error_with_caller` 紧密耦合、`defer` 只有 20 行，拆出去反而增加跳转成本。这也呼应 zed 工程规范「优先在既有文件里实现，除非是新逻辑组件」。

**练习 2**：`ArcCow` 的 `Hash` 实现（L36–L43）为什么对 `Owned` 分支写 `Hash::hash(&**owned, state)` 而不是 `owned.hash(state)`？

**答案**：`Arc<T>` 自身的 `Hash` 实现是委托给内部 `T` 的（标准库保证），所以两种写法结果相同；但显式写 `&**owned` 让「哈希的是 T 的内容」这一意图不依赖读者对 `Arc: Hash` 语义的了解，可读性更好。同时它与 `Borrowed` 分支的 `Hash::hash(borrowed, state)` 形成对称——又是 4.2.2 说的「分支对称」原则。

**练习 3**：在本讲的「平台无关 / 仅 Windows / 仅测试」三分法里，arc_cow.rs 的 142 行属于哪一类？

**答案**：全部「平台无关」——整个文件没有任何 `#[cfg]`（导入清单 L1–L7 只有 `std`），任何目标平台都完整编译。

### 4.3 lib.rs 内嵌测试 type_id_hasher：crate 自带的样例

#### 4.3.1 概念说明

lib.rs 里唯一的测试是 L566–L581 的 `#[test] fn type_id_hasher`，紧贴在 `TypeIdHasher` 实现的下方。它同时扮演三个角色：

1. **回归测试**：保证哈希器对 `TypeId` 正常工作（结果非零）。
2. **使用文档**：它就是「TypeIdHasher 该怎么用」的活样例，比 doc 注释更可信。
3. **本讲的分类样本**：它演示了第三种编译类别——**仅测试**。注意它没有包在 `#[cfg(test)] mod tests` 里，`#[test]` 属性本身就保证了该函数只在 `cargo test` 的测试编译中存在，普通构建会把它剔除。

#### 4.3.2 核心流程

测试的执行流程：

```text
cargo test -p gpui_util
    → 以测试模式重新编译 crate（#[test] 函数这次参与编译，并生成 main）
    → 运行测试函数 type_id_hasher:
        对 5 种类型分别调用 verify_hashing_with:
            TypeId::of::<T>()     取得类型的 TypeId
            type_id.hash(&mut hasher)   喂给 TypeIdHasher
            assert_ne!(hasher.finish(), 0)  断言哈希值非零
    → 报告 1 passed
```

被测代码只有两小段：[crates/gpui_util/src/lib.rs:L544-L558](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L544-L558) 的 `write`（只取前 8 字节直接当 u64 用）和 [L560-L563](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L560-L563) 的 `finish`（原样返回）。测试内部还嵌套定义了辅助函数 `verify_hashing_with`（L570–L574），这是 Rust 测试里常见的「参数化断言」写法。

#### 4.3.3 源码精读

**测试本体。**

[crates/gpui_util/src/lib.rs:L566-L581](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L566-L581) —— 定义 `type_id_hasher` 测试：内部函数 `verify_hashing_with` 接收一个 `TypeId`，用 `TypeIdHasher::default()` 新建哈希器、执行哈希并断言结果非零；然后挑选五种类型验证——`usize`（普通）、`()`（零尺寸）、`str`（unsized）、`&str`（引用）、`Vec<u8>`（泛型集合）。L575 的注释解释了选型意图：「Pick a variety of types, just to demonstrate it's all sane. Normal, zero-sized, unsized, &c.」（挑多种类型，只为证明一切正常：普通的、零尺寸的、非固定尺寸的等）。

**被测的哈希器（供对照，细节留待 u2-l7）。**

[crates/gpui_util/src/lib.rs:L539-L564](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L539-L564) —— `TypeIdHasher` 只有一个 `value: u64` 字段；`write` 尝试取入参前 8 字节用 `u64::from_ne_bytes` 直接当作哈希值，取不到 8 字节时调用 `debug_panic!`（正好复用 4.1.3 讲过的宏）。这就是「恒等哈希」：不散列、零计算。

#### 4.3.4 代码实践

**实践：运行 crate 内唯一的测试。**

1. **实践目标**：亲手跑通 `cargo test -p gpui_util`，并验证 `#[test]`（无 `#[cfg(test)]` 包装）在普通构建中不产生任何产物。

2. **操作步骤**：

   ```bash
   # 在 zed 仓库根目录：
   cargo test -p gpui_util

   # 对照实验：只构建不测试，然后观察测试函数是否存在
   cargo build -p gpui_util
   ```

3. **需要观察的现象**：
   - `cargo test` 输出应包含 `running 1 test` 与 `test type_id_hasher ... ok`（具体措辞以本地 cargo 版本为准，待本地验证）。
   - gpui_util 是纯库 crate，`cargo build` 正常完成且没有任何测试相关的输出——`#[test]` 函数没有进入库产物。

4. **预期结果**：1 个测试通过、0 个失败；若故意把 L553 的断言改坏（例如把 `assert_ne!` 换成 `assert_eq!(hasher.finish(), 0)`），再跑 `cargo test -p gpui_util` 应看到该测试失败——这能确认你修改的是真正被执行的代码（实验后请还原）。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试要专门挑 `()`（零尺寸类型）和 `str`（unsized 类型）？

**答案**：这是边界值测试思想。`TypeId` 对任意类型（包括零尺寸、非固定尺寸类型）都应产出稳定且互不相同的值；`write` 只认前 8 字节，如果某些特殊类型的 `TypeId::hash` 调用传入的字节数异常，这里最可能暴露。挑选多样性输入是为了证明「恒等哈希」假设在全景类型空间里成立。

**练习 2**：这个测试文件级没有 `#[cfg(test)]`，那 `cargo build` 时 `verify_hashing_with` 和 `assert_ne!` 会不会被编译进库？

**答案**：不会。`#[test]` 标注的函数（以及只被它调用的嵌套项）在非测试构建中被编译器剔除，效果与 `#[cfg(test)]` 等价；区别只是组织风格——集中式 `mod tests` 便于统一导入，就地测试（ gpui_util 的选择）让测试紧贴实现、读代码时立刻可见。

**练习 3**：`write` 里调用的 `debug_panic!`（L554）在 `cargo test` 的默认 profile 下是 panic 还是日志？

**答案**：是 panic。`cargo test` 默认使用 dev/test profile，`debug_assertions` 开启，`cfg!(debug_assertions)` 为 `true`，于是 `debug_panic!` 展开为 `panic!`。这也意味着「用 TypeIdHasher 哈希非 TypeId 数据」这类误用在测试中会立即炸出来，而不是静默产出错误哈希。

## 5. 综合实践

把本讲三个模块串起来，完成一份**《lib.rs 条件编译地图》**。这是本讲规格中指定的核心实践任务。

**任务 A：制作模块清单表。**

为 `src/lib.rs` 的每个顶层项（加上 arc_cow.rs 整体）制作一张表，包含四列：**名称 / 行号 / 功能一句话 / 编译类别**。编译类别使用三值标注：

- `平台无关` —— 所有目标都编译（如 `post_inc` L148、`ResultExt` L210、`defer` L505、`TypeIdHasher` L539、`truncate_to_bottom_n_sorted_by` L583、arc_cow.rs 全部）。
- `仅 Windows` —— 只在 Windows 目标编译（`CREATE_NO_WINDOW` L17、Windows 版 `new_std_command` L20、`get_windows_system_shell` L34）；此外为精确起见，可加两个细化标注：`仅非 Windows`（L29 的互补实现）与 `仅测试`（L566 的 `type_id_hasher` 测试）。

提示：可以先不看本讲 4.1.1 的表格，独立从源码归纳，再对答案。全表应有约 25 个条目（17 个函数、2 个宏、2 个 trait、3 个结构体、1 个常量、1 个测试，外加 arc_cow 模块）。

**任务 B：用 cargo tree 对比两个目标的依赖差异。**

```bash
# 在 zed 仓库根目录执行：

# 当前平台（Linux/macOS 主机）
cargo tree -p gpui_util

# 以 Windows 为目标解析（cargo tree 只做依赖解析、不编译，
# 因此通常无需先 rustup target add）
cargo tree -p gpui_util --target x86_64-pc-windows-msvc
```

需要观察的现象与预期结果：

- 第一条命令的树里只有 `log` 和 `anyhow` 两个直接依赖（各自还会带出少量传递依赖）。
- 第二条命令的树里多出 `which`——这正是 [Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L11-L12) 平台门控依赖的效果，与 4.1.3 的代码侧门控互为印证。
- 两次输出都应显示 gpui_util 位于树的根。具体版本号（如 `which v8.x.x`）以本地 `Cargo.lock` 为准，**待本地验证**。

**任务 C：交叉验证。**

把任务 A 表格中标注为「仅 Windows」的条目，与任务 B 中「只在 Windows 目标出现的依赖」对照，回答：`which` 这个依赖支撑了哪段代码？如果 zed 团队决定把 `get_windows_system_shell` 迁到别的 crate，`Cargo.toml` 需要怎么改？

参考答案：`which` 支撑 [L130-L131](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L130-L131) 的 `which::which_global("pwsh.exe")` 兜底探测；若函数迁走，应把 `[target.'cfg(target_os = "windows")'.dependencies]` 段连同 `which.workspace = true` 一起（或改写到）迁入目标 crate，否则 gpui_util 在 Windows 上会背上未使用的依赖（或目标 crate 编译失败）。

## 6. 本讲小结

- `src/lib.rs` 共 604 行、九大功能区段：Windows 平台工具（L17–L146）、通用小函数（L148–L171）、宏与开发期辅助（L173–L209）、`ResultExt`（L210–L288）、日志内部实现（L290–L336）、Future 适配器（L338–L503）、`defer`（L505–L526）、TypeId 哈希器（L528–L581）、排序工具（L583–L603）。
- 条件编译在本 crate 有四种形态：整块 `#[cfg(target_os = "windows")]` 剔除、`new_std_command` 式互补对（每平台恰好编译其一）、函数内部嵌套的 `#[cfg(target_pointer_width)]` 微调、以及 `cfg!(debug_assertions)` 编译期布尔。
- 代码门控与依赖门控必须配套：`get_windows_system_shell` 用到 `which`，所以 `Cargo.toml` 里 `which` 只对 Windows 目标声明——在 Linux 上连编译都不会发生。
- `arc_cow.rs` 是唯一子模块，142 行展示「一个枚举 + 一族对称 match 委托」的标准布局；`ArcCow` 的比较、哈希、克隆都作用于指向的数据，clone 只递增 `Arc` 计数。
- 唯一测试 `type_id_hasher` 直接内嵌在实现旁，靠 `#[test]` 属性（无需 `#[cfg(test)]`）实现「仅测试」编译类别。
- 在非 Windows 主机上验证 Windows 分支的工具箱：`cargo check -p gpui_util --target x86_64-pc-windows-msvc`（需 `rustup target add`）、`cargo tree --target`（只解析、通常无需安装目标）、以及编辑器里 rust-analyzer 对未激活 cfg 分支的灰显。

## 7. 下一步学习建议

本讲你已经拿到完整的「源码地图」，第二单元将按使用频率从高到低逐区深入，建议顺序：

1. **下一讲 u2-l1（ResultExt）**：精读地图第 4 区段（L210–L288），这是整个 zed 代码库调用最频繁的错误处理工具，也是理解项目「禁止 `let _ =` 丢弃错误」规范的钥匙。
2. **u2-l2（log_error_with_caller）**：顺着 `ResultExt` 的调用进入第 5 区段（L290–L336），理解 `#[track_caller]` 与日志 target 的推导。
3. 之后再依次进入宏区段（u2-l3）、Future 适配器（u2-l4）、`defer`（u2-l5）、`ArcCow` 深用（u2-l6）、`TypeIdHasher`（u2-l7）。
4. 阅读间隙可以做个热身：在 zed 仓库里随便挑一个 crate（如 `crates/fuzzy`），用 `rg "gpui_util::" -n` 看看下游都在用地图上的哪些区段——这会告诉你哪些工具是「高频出口」。
