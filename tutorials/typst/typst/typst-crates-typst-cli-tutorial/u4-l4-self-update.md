# 自更新机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst update` 的一条完整主线：**备份当前二进制 → 查询 GitHub release → 比较版本 → 下载预编译资产 → 解压 → 用 `self_replace` 原地替换**。
- 理解为什么自更新被关在一道 `self-update` feature 开关后面，以及未启用时的「占位实现」如何工作。
- 读懂 `determine_asset!` 宏如何用编译期常量 `env!("TARGET")` 把当前平台的三元组（target triple）映射成 release 里对应的资产名。
- 区分 Windows 的 ZIP 解压与其它平台的 `.tar.xz` 解压两条路径。
- 解释 `revert` 回滚、`backup_path` 的平台目录选择，以及 `--force` 对降级的保护逻辑。

本讲是 u3-l3（网络下载与进度报告）的直接续篇：自更新复用的正是那里的 `download::downloader()` 下载基础设施。

## 2. 前置知识

- **进程自我替换**：一个正在运行的程序想替换自己的可执行文件并不简单——在大多数操作系统上，正在运行的二进制文件会被锁定或保持已加载状态。`self-replace` crate 的做法是：把新二进制写成一个临时文件，再用平台特定的系统调用（如 Linux 的 `rename`、Windows 的 `MoveFileEx`）原子地把它「搬到」当前可执行文件的路径上。本讲不深入它的内部，只需知道 `self_replace::self_replace(path)` 的语义是「把 `path` 指向的文件安装为当前进程的可执行文件」。

- **GitHub Releases**：Typst 在 GitHub 上为每次版本发布（release）附带一批「预编译资产」（prebuilt assets），每个资产是一个压缩包，按操作系统和 CPU 架构命名，例如 `typst-x86_64-unknown-linux-musl`、`typst-aarch64-apple-darwin`、`typst-x86_64-pc-windows-msvc`。`typst update` 就是去这个 release 列表里挑出匹配当前平台的那个压缩包。

- **语义化版本（semver）**：形如 `主版本.次版本.修订号`（如 `0.13.1`）。`semver` crate 的 `Version` 类型支持直接用 `<`、`>` 比较，本讲用它来判断「远端版本是否比本地新」。

- **硬失败 vs 软失败**：这是 u1-l2 建立的概念。`typst compile` 等命令即便编译失败也返回 `Ok(())`、靠 `set_failed()` 改退出码（软失败）；而 `typst update` 在 `dispatch` 中带 `?`，错误会一路冒泡成应用级错误并打印、以非零码退出（硬失败）。本讲的错误全是硬失败。

- **target triple（目标三元组）**：Rust 用一串形如 `CPU-厂商-操作系统-ABI` 的字符串描述编译目标，如 `x86_64-unknown-linux-gnu`。`rustc -vV` 可查到当前工具链的 `host` 与 `target`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/update.rs` | 自更新的全部核心逻辑：主流程、`determine_asset!` 宏、`Release` 结构、版本比较、解压、备份路径。 |
| `src/download.rs` | 通用下载工厂 `downloader()`，被自更新复用来下载 release 信息与资产。 |
| `src/args.rs` | `UpdateCommand` 参数结构（`version` / `--force` / `--revert` / `--backup-path`）。 |
| `src/main.rs` | feature 门控（`#[cfg(feature = "self-update")]`）、`dispatch` 分发，以及未启用时的占位 `mod update`。 |
| `Cargo.toml` | `self-update` feature 定义，以及 `self-replace` / `xz2` / `zip` 三个可选依赖。 |

## 4. 核心概念与源码讲解

### 4.1 自更新机制的整体设计

#### 4.1.1 概念说明

`typst update` 让 CLI 在没有包管理器（如 `brew`、`cargo install`、系统包）介入的情况下自行升级——它直接从 GitHub release 拉取官方预编译二进制并替换自己。

这是一个「重特权」操作：它会覆盖磁盘上的可执行文件，因此 typst-cli 把它关在一道编译期 feature 开关 `self-update` 之后。这道开关有两层含义：

1. **默认不启用**：`Cargo.toml` 的 `default = ["embedded-fonts", "http-server"]` 不含 `self-update`，所以官方默认构建（以及很多分发渠道）其实并没有自更新能力。
2. **拉入额外依赖**：开启它会引入 `self-replace`、`xz2`、`zip` 三个 crate（见 `Cargo.toml`）。

即便如此，`update` 子命令本身在 `args.rs` 里始终存在（这样帮助文本、补全脚本结构稳定），只是当 feature 关闭时它在命令行里被「隐藏」，并且运行时会立刻报错。

#### 4.1.2 核心流程

自更新的整体骨架（`update()` 函数）可以用下面的伪代码概括：

```
fn update(command):
    # ① 降级保护：若指定了 version 且低于当前，且未 --force，直接 bail
    if let Some(version) = command.version:
        if version < 0.8.0: 提示旧版没有 update 命令
        if not force and version < current: bail("downgrading requires --force")

    # ② 确定备份路径并创建其父目录
    backup_path = command.backup_path 或 backup_path()

    # ③ 若是 --revert：用备份文件替换当前二进制后删除备份，返回
    if command.revert:
        if not backup_path.exists(): bail("no backup found")
        self_replace(backup_path); remove(backup_path); return

    # ④ 备份当前二进制到 backup_path
    copy(current_exe, backup_path)

    # ⑤ 查询 release（最新或指定 tag）
    release = Release::from_tag(command.version, downloader)

    # ⑥ 判断是否需要更新
    if not update_needed(release) and not force: 打印 "Already up-to-date."; return

    # ⑦ 下载并解压匹配平台的二进制
    binary = release.download_binary(determine_asset!(), downloader)

    # ⑧ 写入临时文件并自我替换
    temp_exe = NamedTempFile::new(); write(temp_exe, binary)
    self_replace(temp_exe)
```

关键设计点：

- **先备份、后替换**：在任何网络操作之前就把当前可执行文件复制到备份路径，这样无论后续下载/解压/替换哪一步失败，用户都能 `typst update --revert` 回滚。
- **降级保护前置**：降级检查（第①步）发生在所有副作用之前，确保「不该降级」时连备份都不会动。
- **复用下载基础设施**：第⑤⑦步用的 `downloader()` 正是 u3-l3 讲过的那个工厂，带 TLS 证书与进度报告。

#### 4.1.3 源码精读

首先看 feature 门控。`main.rs` 顶部的模块声明按条件编译：

[main.rs:14-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L14-L15) —— 仅当启用 `self-update` feature 时才编译真正的 `update` 模块；否则编译下面的占位模块。

未启用时的占位实现在文件末尾：

[main.rs:134-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L134-L147) —— 这是一个同名 `mod update`，其 `update()` 直接 `bail!`，提示用户改用包管理器。注意它和真正的 `update.rs` 互斥（`cfg` 取反），所以两个 `update` 函数永远不会同时存在。

`dispatch` 里 `update` 带 `?`，说明它是硬失败：

[main.rs:77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L77) —— `Command::Update(command) => crate::update::update(command)?,`。错误会冒泡到 `main`，作为应用级错误打印并以非零码退出（与 `compile` 的软失败相反）。

feature 与依赖的对应关系在 `Cargo.toml`：

[Cargo.toml:98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L98) —— `self-update = ["dep:self-replace", "dep:xz2", "dep:zip"]`。`dep:` 前缀表示「只拉依赖、不改其它 feature」。对应的三个可选依赖声明在 [Cargo.toml:45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L45)、[Cargo.toml:55-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L55-L56)（`self-replace` / `xz2` / `zip` 均 `optional = true`）。

参数模型在 `args.rs`：

[args.rs:242-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L242-L265) —— `UpdateCommand` 有四个字段：`version: Option<Version>`（可选的目标版本）、`force: bool`、`revert: bool`（与 `version`、`force` 互斥）、`backup_path: Option<PathBuf>`（命令行 `--backup-path` 与环境变量 `TYPST_UPDATE_BACKUP_PATH` 共同汇入）。子命令本身的隐藏标记在 [args.rs:103-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L103-L105)（`#[cfg_attr(not(feature = "self-update"), clap(hide = true))]`）。

#### 4.1.4 代码实践

**实践目标**：直观感受 feature 开关对 `update` 子命令可见性与行为的影响（不实际执行更新）。

**操作步骤**：

1. 先用默认 feature 构建：在 `crates/typst-cli` 下执行 `cargo build`。
2. 运行 `./target/debug/typst --help`，观察子命令列表里**是否出现** `update`（默认应被隐藏，但仍可显式调用）。
3. 运行 `./target/debug/typst update`，观察报错信息——应出现占位模块的 `bail!` 文案：「self-updating is not enabled for this executable ...」。
4. 重新构建并启用 feature：`cargo build --features self-update`。
5. 再次运行 `./target/debug/typst --help` 与 `./target/debug/typst update --help`，对比 `update` 是否可见、`--force` / `--revert` / `--backup-path` 是否出现。

**需要观察的现象**：feature 关闭时 `update` 在帮助里隐藏、运行即报错；开启后参数齐全。

**预期结果**：占位报错文本与 [main.rs:140-146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L140-L146) 完全一致。

> 若环境无网络，第 4 步之后的实际下载会失败，但 `--help` 不触发网络，可安全观察参数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 typst-cli 要把自更新做成可选 feature，而不是默认启用？

**参考答案**：自更新会直接覆盖可执行文件，属于高特权操作；许多安装渠道（`cargo install`、系统包管理器、Linux 发行版）本身已经提供了升级机制，若 typst 再自作主张地替换二进制，会和包管理器的版本记录冲突。因此默认关闭，只让需要它的构建（如官方独立分发包）启用。

**练习 2**：占位 `mod update`（`main.rs:134-147`）和真正的 `src/update.rs` 为什么不会冲突？

**参考答案**：二者用互斥的 `#[cfg(feature = "self-update")]` 与 `#[cfg(not(feature = "self-update"))]` 守卫，任何一次编译只会有其一被编入。它们对外暴露同名同签名的 `update` 函数，对 `dispatch` 考调用方完全透明。

---

### 4.2 `determine_asset!` 宏：把编译目标映射成资产名

#### 4.2.1 概念说明

GitHub release 里的每个资产按「目标平台」命名，命名约定就是 `typst-<目标三元组>`。`typst update` 需要在运行时知道自己该下载哪一个。

这里有一个微妙之处：**编译时的目标三元组并不总是等于 release 里的资产名**。Typst 官方为 Linux 预编译的是 musl 静态链接版本（便于在任意发行版运行），而开发者本地常用的工具链默认是 gnu 版本。于是 `determine_asset!` 宏做了一张「重定向表」：把若干 gnu/musl 三元组映射到实际存在资产的那个名字。

这个判断必须在**编译期**完成——因为 `env!("TARGET")` 是编译期常量（由 Cargo 注入），它描述的是「这份二进制是为哪个平台编译的」，而不是运行机器的真实情况。也就是说，在 x86_64 Linux 上交叉编译出的 aarch64 二进制，运行时 `env!("TARGET")` 仍是 aarch64。

#### 4.2.2 核心流程

宏分两层：

```
determine_asset!()                           # 外层：提供重定向表
  → determine_asset!(__impl: { 表 })         # 内层：根据 env!("TARGET") 查表
      match env!("TARGET") {
          "x86_64-unknown-linux-gnu" => "typst-x86_64-unknown-linux-musl",
          "aarch64-unknown-linux-gnu" => "typst-aarch64-unknown-linux-musl",
          ... 其余重定向项 ...
          _ => "typst-<TARGET>",              # 兜底：直接用编译目标名
      }
```

关键点：

- 重定向表只覆盖「官方用 musl 发布、但常见工具链默认 gnu」的几种 Linux 目标（以及一种反向的 riscv64 情况：官方按 gnu 发布，但工具链默认名是 musl）。
- 表里**没有**的目标走兜底分支，资产名就是 `typst-` 加上原始三元组（如 macOS、Windows）。
- `env!("TARGET")` 在编译期被替换成字符串字面量，`match` 实际上退化成编译期常量匹配，最终 `determine_asset!()` 展开成一个单一的 `&'static str`。

#### 4.2.3 源码精读

宏定义在 `update.rs` 开头：

[update.rs:20-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L20-L40) —— 外层 `determine_asset!()` 把重定向表传给内层 `__impl` 变体；内层用 `match env!("TARGET")` 在表里查找，命中则 `concat!("typst-", $target)`，未命中则 `concat!("typst-", env!("TARGET"))`。

宏的调用点在主流程里：

[update.rs:102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L102) —— `release.download_binary(determine_asset!(), &downloader)`。注意 `determine_asset!()` 在这里被当作普通函数参数传入，因为宏在编译期就已展开成字符串字面量。

资产名的完整对照（源自宏注释指向的 `.github/workflows/release.yml`）：

| 编译目标（`env!("TARGET")`） | 实际下载资产名 |
| --- | --- |
| `x86_64-unknown-linux-gnu` | `typst-x86_64-unknown-linux-musl` |
| `aarch64-unknown-linux-gnu` | `typst-aarch64-unknown-linux-musl` |
| `armv7-unknown-linux-gnueabi` | `typst-armv7-unknown-linux-musleabi` |
| `riscv64gc-unknown-linux-musl` | `typst-riscv64gc-unknown-linux-gnu` |
| 其它（如 `aarch64-apple-darwin`） | `typst-<原样三元组>` |

#### 4.2.4 代码实践

**实践目标**：确定「如果在本机运行 `typst update`，会去下载哪个资产」。

**操作步骤**：

1. 在终端运行 `rustc -vV`，找到 `target:` 那一行，记录三元组（例如 `x86_64-unknown-linux-gnu`）。
2. 打开 [update.rs:26-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L26-L30) 的重定向表，看你的三元组是否命中。
3. 据此推断资产名。

**需要观察的现象 / 预期结果**：

- 若你的 `target` 是 `x86_64-unknown-linux-gnu`（最常见的 Linux 开发机），命中第一行，资产名是 `typst-x86_64-unknown-linux-musl`。
- 若是 macOS（如 `aarch64-apple-darwin`）或 Windows（如 `x86_64-pc-windows-msvc`），不在表里，资产名就是 `typst-aarch64-apple-darwin` / `typst-x86_64-pc-windows-msvc`。

> 注意：本实践只做推断，不实际下载。如想验证，可对照 GitHub 上某次 Typst release 的 Assets 列表确认该名字确实存在。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `env!("TARGET")` 而不是 `std::env::consts::OS` + `ARCH` 在运行时拼？

**参考答案**：`env!("TARGET")` 是**编译期**注入的完整三元组，描述「这份二进制是为哪个目标编译的」，能精确区分 `gnu` 与 `musl`、`gnueabi` 与 `musleabi` 等 ABI 差异；而 `OS`/`ARCH` 只到操作系统和 CPU 粒度，无法表达 ABI，不足以选出正确的 musl/gnu 资产。此外编译期匹配让整个宏坍缩成一个字面量，无运行时开销。

**练习 2**：表里 `riscv64gc-unknown-linux-musl => ...-gnu` 的方向与其它行相反，说明了什么？

**参考答案**：说明官方为 riscv64gc 发布的是 **gnu** 版本资产，而该平台常用的工具链默认三元组带 `musl`。重定向表的方向完全取决于「官方发布了哪个」，宏的作用就是把「工具链默认名」校正到「官方资产名」。

---

### 4.3 查询 release、比较版本、下载与解压

#### 4.3.1 概念说明

确定了资产名之后，还需要三步才能拿到可执行字节：

1. **查询 release 元信息**：调用 GitHub API（`api.github.com/.../releases/tags/v<版本>` 或 `releases/latest`），拿到一个 JSON，里面列出本次 release 的全部资产（名字 + 下载 URL）。Typst 用 `Release` / `Asset` 两个结构反序列化它。
2. **比较版本**：把 release 的 `tag_name`（形如 `v0.13.1`）解析成 semver，和本地版本比；若不比本地新（且未 `--force`），直接打印「Already up-to-date.」并退出，不下载任何东西。
3. **下载并解压**：在资产列表里找到名字以我们选中的资产名开头的那一项，下载其 `browser_download_url`，再按平台解压成裸二进制字节。

解压分两种格式：

- **Windows 资产是 ZIP**：解压后取出 `typst.exe`。
- **其它平台资产是 `.tar.xz`**：先 xz 解压、再 tar 解包，取出 `typst`。

#### 4.3.2 核心流程

```
Release::from_tag(tag, downloader):
    url = tag ? .../releases/tags/v<tag> : .../releases/latest
    data = downloader.download("release information", url)   # 静默，不显示进度
    若 404 → bail("release not found")
    反序列化为 Release { tag_name, assets }

update_needed(release):
    current = 解析 typst::utils::version().raw()
    new     = 解析 release.tag_name（去掉前缀 'v'）
    return new > current

Release::download_binary(asset_name, downloader):
    asset = assets.find(|a| a.name.starts_with(asset_name))   # 找不到则 bail
    data  = downloader.download("release", asset.url)         # key="release"，显示进度
    若 404 → bail("asset not found")
    if asset_name 含 "windows": extract_binary_from_zip(data, asset_name)
    else:                       extract_binary_from_tar_xz(data)
```

这里承接 u3-l3 的一个关键细节：`downloader.download` 的第一个参数是 `&dyn Any` 形式的 key，CLI 用它决定是否显示进度。在 `download.rs` 的闭包里，只有恰好等于 `"release"` 的 key 才显示进度条；`"release information"`（查 release 元信息，体积极小）被归入 `None` 静默处理——所以「查 release」不会打扰用户，而「下载二进制」（动辄几 MB）才会显示进度条。

#### 4.3.3 源码精读

`Release` / `Asset` 是对 GitHub API JSON 的直接映射：

[update.rs:118-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L118-L129) —— 两个字段 `name` 与 `browser_download_url`，用 `serde::Deserialize` 反序列化。

`from_tag` 拼 URL 并处理 404：

[update.rs:134-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L134-L156) —— 注意它把 `io::ErrorKind::NotFound` 翻译成更友好的 `bail!("release not found (searched at {url})")`。这正是 u3-l3 提到的「404 → `NotFound`」约定的消费点。key 用的是字符串字面量 `"release information"`。

`update_needed` 的版本比较：

[update.rs:226-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L226-L236) —— 先 `strip_prefix('v')` 去掉 tag 前缀，再 `parse()` 成 `Version`，比较 `new_tag > current_tag`。本地版本来自 `typst::utils::version().raw()`（编译期注入的版本字符串）。

`download_binary` 选资产并按平台分流：

[update.rs:161-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L161-L186) —— 用 `assets.iter().find(|a| a.name.starts_with(asset_name))` 选资产（`starts_with` 而非精确等于，是为了兼容资产名可能带的后缀）；下载的 key 是 `"release"`（显示进度）；最后用 `asset_name.contains("windows")` 在 zip 与 tar.xz 之间分流。

ZIP 解压：

[update.rs:189-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L189-L204) —— 用 `ZipArchive` 打开，按 `{asset_name}/typst.exe` 取出二进制条目读到底。

`.tar.xz` 解压：

[update.rs:207-223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L207-L223) —— `tar::Archive::new(XzDecoder::new(...))` 先 xz 后 tar；遍历条目找路径以 `typst` 结尾的那个，读到底。注意这里用「路径以 `typst` 结尾」而非精确路径，因此能容忍压缩包内的目录层级差异。

下载器复用：

[download.rs:15-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L15-L32) —— `downloader()` 装配 `SystemDownloader`（带 `--cert` / `TYPST_CERT` 证书与 `typst/<版本>` user-agent），再用 `ProgressDownloader` 包装，闭包里把 `"release"` key 映射成可显示名、其余静默。`update.rs` 第 94 行 `let downloader = download::downloader();` 就是这里的入口。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `update_needed` 与主流程，理清「什么情况下会真正触发下载」的判断逻辑（不实际执行更新）。

**操作步骤**：

1. 读 [update.rs:226-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L226-L236)，确认返回 `new_tag > current_tag`。
2. 读主流程的判断分支 [update.rs:97-100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L97-L100)：`if !update_needed(&release)? && !command.force { ... Already up-to-date ... }`。
3. 用 `typst --version` 查本地版本，假设是 `0.13.1`。
4. 推断以下四种调用的行为：
   - 最新 release 恰好也是 `v0.13.1`：`typst update`
   - 最新 release 是 `v0.14.0`：`typst update`
   - `typst update 0.13.0`（不带动 `--force`）
   - `typst update 0.13.0 --force`

**需要观察的现象 / 预期结果（推断）**：

| 调用 | `update_needed` | 行为 |
| --- | --- | --- |
| 最新 = 0.13.1 时 `typst update` | `false`（0.13.1 > 0.13.1 不成立） | 打印「Already up-to-date.」并退出，**不下载** |
| 最新 = 0.14.0 时 `typst update` | `true` | 继续下载替换 |
| `typst update 0.13.0` | `false` | 注意：降级保护在第①步（4.1）已 `bail!`，根本到不了这里 |
| `typst update 0.13.0 --force` | `false`，但 `force=true` 短路了 `!force` | 继续下载替换（强制降级） |

关键结论：`update_needed` 只判断「远端是否更新」；「允许不允许降级」是另一回事，由第①步的前置校验和这里的 `--force` 共同决定。

#### 4.3.5 小练习与答案

**练习 1**：为什么查询 release 元信息时 key 用 `"release information"`，而下载二进制时 key 用 `"release"`？

**参考答案**：key 决定进度条是否显示（见 `download.rs` 的闭包）。release 元信息 JSON 很小、下载极快，无需进度条打扰用户，故用不在白名单里的 `"release information"` 归入静默；二进制资产体积大，用恰好等于 `"release"` 的 key 触发进度显示。

**练习 2**：`download_binary` 里用 `a.name.starts_with(asset_name)` 而非 `==`，为什么？

**参考答案**：资产名可能带有 `.tar.xz`、`.zip` 等压缩后缀或平台补充后缀（例如实际资产全名可能是 `typst-x86_64-unknown-linux-musl.tar.xz`），用 `starts_with` 能稳健地匹配到「以我们选中的三元组名开头」的资产，避免对后缀做硬编码假设。

---

### 4.4 自我替换、备份路径与回滚

#### 4.4.1 概念说明

下载解压得到的只是「裸二进制字节」，还躺在内存里。要让它成为「下次 `typst` 命令实际运行的程序」，必须把它写到磁盘上当前可执行文件所在的路径。这一步由 `self_replace::self_replace(path)` 完成——它把 `path` 指向的文件原子地安装为当前进程的可执行文件。

替换是不可逆的高风险操作，因此 typst 设计了**备份 + 回滚**机制：

- **更新前备份**：把**当前的**（即将被替换的）二进制复制到一个备份文件 `typst_backup.part`。
- **`--revert` 回滚**：若更新后发现问题，运行 `typst update --revert`，它会用备份文件再次 `self_replace` 回去，然后删除备份。

备份文件放在哪由 `backup_path()` 按平台决定，也可用 `--backup-path`（或环境变量 `TYPST_UPDATE_BACKUP_PATH`）自定义。

另外，`--force` 既管「强制降级」（第①步），也在主流程里作为「即使不比本地新也强制替换」的旁路（第⑥步）——同一个 flag 在两处都起作用。

#### 4.4.2 核心流程

更新路径的收尾：

```
# 第④步：备份
copy(current_exe, backup_path)

# 第⑦⑧步：下载解压后
temp_exe = NamedTempFile::new()          # 临时文件，同目录便于原子 rename
write_all(temp_exe, binary_data)
self_replace::self_replace(&temp_exe)    # 把临时文件安装为当前 exe
  # 失败：删除临时文件并 bail
```

回滚路径（`--revert`，在第③步提前返回）：

```
if not backup_path.exists(): bail("no backup found")
self_replace::self_replace(&backup_path) # 用备份替换当前 exe
remove_file(&backup_path)                # 删除备份
```

备份路径选择（`backup_path()`）：

```
Linux:   dirs::state_dir() 或 dirs::data_dir()   → <它>/typst/typst_backup.part
其它:    dirs::data_dir()                         → <它>/typst/typst_backup.part
```

#### 4.4.3 源码精读

主流程的备份与替换：

[update.rs:87-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L87-L92) —— 先 `env::current_exe()` 定位当前可执行文件，再 `fs::copy` 复制到备份路径。这一步在下载之前，确保失败可回滚。

[update.rs:103-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L103-L113) —— 用 `tempfile::NamedTempFile` 写临时文件，`self_replace::self_replace(&temp_exe)` 完成替换。`NamedTempFile` 默认建在系统临时目录、且 Drop 时自动清理，`self_replace` 内部会把它搬到当前 exe 路径。失败时主动 `remove_file` 清理临时文件再报错。

回滚分支（在主流程最前面，先于备份与下载）：

[update.rs:74-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L74-L85) —— `--revert` 与 `version`/`force` 互斥（见 `args.rs` 的 `conflicts_with`）。它检查备份是否存在，不存在就 `bail!`；存在则 `self_replace` 后删除备份。注意回滚**不会**创建新的备份（它把备份「用掉了」），所以连续两次 `--revert` 第二次会因「no backup found」失败。

备份路径的平台选择：

[update.rs:251-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L251-L262) —— Linux 优先用 `dirs::state_dir()`（`$XDG_STATE_HOME` 或 `~/.local/state`），回退到 `dirs::data_dir()`；非 Linux 直接用 `dirs::data_dir()`（macOS 的 `~/Library/Application Support`、Windows 的 `%APPDATA%`）。最终路径都是 `<根>/typst/typst_backup.part`。`.part` 后缀暗示这是个「过渡文件」。

`--backup-path` 的接入：主流程 [update.rs:67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L67) 用 `command.backup_path.clone().map(Ok).unwrap_or_else(backup_path)?`——有自定义就用自定义，否则回落到平台默认函数。结合 `args.rs` 的 `env = "TYPST_UPDATE_BACKUP_PATH"`，优先级是：`--backup-path` 命令行 > `TYPST_UPDATE_BACKUP_PATH` 环境变量 > 平台默认（clap 自动把命令行/环境变量合并到同一字段）。

父目录创建：

[update.rs:69-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L69-L72) —— `fs::create_dir_all(backup_dir)` 确保自定义路径的父目录存在，避免 `fs::copy` 因目录缺失而失败。

#### 4.4.4 代码实践

**实践目标**：确定「如果在本机运行 `typst update`，备份文件会写到哪」，并理解 `--backup-path` 的覆盖优先级（不实际执行更新）。

**操作步骤**：

1. 先确定你的操作系统类型。
2. 读 [update.rs:251-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L251-L262)，按平台推断 `dirs::state_dir()` / `dirs::data_dir()` 的取值。
3. 推断最终备份路径。

**需要观察的现象 / 预期结果（推断）**：

- **Linux**：`$XDG_STATE_HOME`（未设则 `~/.local/state`）下的 `typst/typst_backup.part`，即通常为 `~/.local/state/typst/typst_backup.part`。若该目录不可用，回退到 `~/.local/share/typst/typst_backup.part`。
- **macOS**：`~/Library/Application Support/typst/typst_backup.part`。
- **Windows**：`%APPDATA%\typst\typst_backup.part`。

4. 进一步推断：若设置环境变量 `TYPST_UPDATE_BACKUP_PATH=/tmp/mybackup.bin` 再运行 `typst update`，备份会写到哪？

**预期结果**：因为 [args.rs:263](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L263) 的 `env = "TYPST_UPDATE_BACKUP_PATH"`，clap 会把该环境变量填进 `command.backup_path`，主流程第 67 行的 `map(Ok).unwrap_or_else` 会优先采用它，故备份写到 `/tmp/mybackup.bin`（且第 69-72 行会 `create_dir_all` 确保其父目录 `/tmp` 存在）。

#### 4.4.5 小练习与答案

**练习 1**：为什么「备份」发生在「下载」之前，而不是之后？

**参考答案**：备份的目的是「在替换失败或新版本有问题时能回到替换前的状态」。如果先下载再备份，一旦下载过程本身触发替换并失败，就没有可回滚的备份了。把备份放在所有网络操作之前，保证无论后续哪一步失败，磁盘上始终有一份完整的旧版本可供 `--revert`。

**练习 2**：连续执行两次 `typst update --revert` 会怎样？

**参考答案**：第一次 `--revert` 用备份替换当前二进制后**删除了备份**（[update.rs:82-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L82-L84) 的 `fs::remove_file`）。第二次执行时 [update.rs:75-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L75-L80) 检查 `backup_path.exists()` 为假，于是 `bail!`「unable to revert, no backup found」。即回滚是「一次性」的。

**练习 3**：`--force` 在主流程里出现在哪两处、各起什么作用？

**参考答案**：第一处在 [update.rs:58-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L58-L63)，作用是「允许降级」——指定了比当前更低的版本时，没有 `--force` 就直接 `bail!`。第二处在 [update.rs:97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L97)，作用是「即使 `update_needed` 为假（远端不比本地新）也强制走下载替换」。两处合起来让 `--force` 既能跨降级保护、又能跨「已是最新」的短路。

## 5. 综合实践

把本讲知识串起来，做一个**纯源码阅读型**的端到端追踪（全程不实际执行更新，避免覆盖本机二进制）：

1. **选定平台**：运行 `rustc -vV` 记下 `target`，用 4.2 的方法推出 `typst update` 会请求的资产名。
2. **选定备份位置**：用 4.4 的方法推出默认备份路径。
3. **画出完整调用链**：从 `main.rs:77` 的 `dispatch` → `update.rs:update()` 出发，按顺序列出每一步调用的函数与对应的行号区间，标注哪些步骤有副作用（创建目录、复制文件、写临时文件、替换 exe、删除备份），哪些是纯查询（`from_tag`、`update_needed`）。
4. **标注失败点与回滚能力**：对每个有副作用的步骤，回答「若它失败，磁盘上留下了什么？用户能否 `--revert`？」例如：
   - `fs::copy` 失败：尚未替换，无需回滚。
   - `self_replace` 失败（[update.rs:109-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L109-L112)）：当前 exe 未变，但备份已存在，用户可 `--revert`（虽然其实没必要）。
5. **写一张参数行为表**：对 `UpdateCommand` 的四个字段（`version` / `force` / `revert` / `backup_path`）两两组合（合理范围内），列出每种组合下程序的走向，验证你对 `conflicts_with`、降级保护、强制旁路的理解。

**预期产物**：一张「调用链 + 副作用 + 失败回滚」三栏对照表，以及一张「参数组合 → 行为」表。若某格无法确定，标注「待本地验证」。

## 6. 本讲小结

- `typst update` 是一条「备份 → 查询 release → 比较版本 → 下载 → 解压 → `self_replace`」的主线，所有错误以 `?` 冒泡为硬失败。
- 自更新被 `self-update` feature 门控：默认关闭，关闭时 `main.rs` 提供一个直接 `bail!` 的同名占位 `mod update`，`update` 子命令在帮助里被隐藏。
- `determine_asset!` 宏用编译期常量 `env!("TARGET")` 把当前目标三元组映射成 release 资产名，并对若干 gnu/musl 目标做重定向校正。
- 版本比较在 `update_needed` 里用 semver `new_tag > current_tag`；`--force` 既绕过降级保护，也绕过「已是最新」的短路。
- 解压按平台分流：Windows 取 ZIP 内的 `typst.exe`，其它平台对 `.tar.xz` 做 xz+tar 解包取 `typst`。
- 备份与回滚：更新前把当前 exe 复制到 `<data 或 state 目录>/typst/typst_backup.part`；`--revert` 用它 `self_replace` 回去并删除备份，因而是「一次性」的；`--backup-path` / `TYPST_UPDATE_BACKUP_PATH` 可覆盖默认位置。

## 7. 下一步学习建议

- **构建期产物**：`typst update` 依赖的 release 资产由 `.github/workflows/release.yml` 产出；下一讲可结合 u4-l5（构建期产物与 completions）理解 CI 如何生成这些预编译包与 `cargo-binstall` 元数据（见 `Cargo.toml` 末尾的 `[package.metadata.binstall]`）。
- **诊断与环境内省**：若想验证本地 typst 的编译特性（如是否启用了 `self-update`），可阅读 u4-l6（info 命令），`typst info` 会列出构建特性。
- **延伸阅读源码**：`self-replace`、`xz2`、`zip` 三个 crate 的文档，理解 `self_replace` 的跨平台原子替换实现与 xz/tar 的流式解压细节。
