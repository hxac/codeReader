# 包存储与解析

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 Typst 的包（package）在磁盘上有哪三个来源，以及 `SystemPackages::obtain` 按什么优先级去查找一个包。
- 读懂 typst-cli 里只有十几行的 `packages.rs`，理解它如何把命令行参数 `PackageArgs` 装配成一个 `SystemPackages`。
- 掌握 `--package-path` / `--package-cache-path` 两个选项与 `TYPST_PACKAGE_PATH` / `TYPST_PACKAGE_CACHE_PATH` 两个环境变量之间的覆盖关系。
- 理解 `FsPackages` 约定的「namespace / name / version」三层目录结构，并能手工构造一个本地 `@local` 包让它被编译器找到。
- 说明同一个 `packages::system()` 是如何被 `SystemWorld`（编译）和 `init`（脚手架）复用的。

## 2. 前置知识

本讲会用到以下概念（前几讲已建立，这里只做最简提醒）：

- **crate 与依赖方向**：typst-cli 是「薄壳」，真正的包加载逻辑住在它依赖的 `typst-kit` crate 里。本讲会出现两个同名文件——CLI 的 `src/packages.rs`（薄壳）和 `typst-kit` 的 `src/packages.rs`（实现），注意区分。
- **`World` trait**：编译器核心只认抽象接口，typst-cli 用 `SystemWorld` 作为它的操作系统版实现（见 u2-l1）。
- **clap 派生宏与 `#[clap(flatten)]`**：参数组像积木一样被拼进不同子命令（见 u1-l3）。
- **`FileId` / `VirtualRoot`**：编译器内部用全局唯一的 `FileId` 标识一个文件，包内文件挂在 `VirtualRoot::Package(spec)` 这个虚拟根下（见 u2-l1）。
- **Typst 包的命名**：形如 `@namespace/name:version`，例如 `@preview/cetz:0.3.1` 或 `@local/demo:0.1.0`。`@preview` 是官方在线仓库 Typst Universe 服务的命名空间。

> 一个关键直觉：**Typst 没有专门的「包管理器进程」**。所谓「装包」只是在编译时，按固定目录约定去文件系统里找一个文件夹；找不到才联网下载并缓存。本讲讲的就是「去哪找、按什么顺序找、找到后怎么用」。

## 3. 本讲源码地图

| 文件 | 作用 | 归属 |
| --- | --- | --- |
| `src/packages.rs` | **薄壳**：唯一函数 `system(args)`，把 `PackageArgs` 装配成 `SystemPackages` | typst-cli |
| `src/args.rs` | 定义 `PackageArgs`（`--package-path` / `--package-cache-path`），并经 `WorldArgs` / `InitCommand` 拼入各子命令 | typst-cli |
| `src/world.rs` | `SystemFiles` 持有一个 `SystemPackages`，在 `root()` 里对包内 `FileId` 调用 `obtain` | typst-cli |
| `src/init.rs` | `init` 命令复用同一个 `packages::system()`，用 `obtain` 定位模板包根目录 | typst-cli |
| `crates/typst-kit/src/packages.rs` | **实现**：`SystemPackages`、`FsPackages`、`UniversePackages` 三个类型与 `obtain` / `store` / `latest_version` | typst-kit |
| `tests/smoke.rs` | `test_package_resolved` 等端到端测试，演示如何用 `--package-path` 喂一个本地包 | typst-cli |

> 提醒：本讲引用的永久链接中，`crates/typst-kit/...` 路径指向依赖 crate，其余都在 `crates/typst-cli/` 下。

## 4. 核心概念与源码讲解

### 4.1 SystemPackages：三级存储模型与 obtain 查找优先级

#### 4.1.1 概念说明

当 Typst 源码里出现 `#import "@preview/foo:1.0.0": *` 时，编译器需要把 `@preview/foo:1.0.0` 这个**逻辑包名**解析成磁盘上某个**真实目录**，目录里必须有 `typst.toml` 清单和入口文件。

`SystemPackages` 就是负责「把包名解析成目录」的角色。它内部维护**三个来源**，按固定优先级依次尝试：

1. **data 目录**（本地包目录）：存放用户自己放进去或系统级安装的包。
2. **cache 目录**（缓存目录）：存放从网上自动下载下来的包。
3. **Universe（在线仓库）**：当前两个都找不到、且包属于 `@preview` 命名空间时，才联网下载，下载结果写进 cache 目录。

注意 data 和 cache 是「用户主动放」与「系统自动缓存」的分工——这个分工后续会影响 `latest_version` 只查 data 不查 cache。

#### 4.1.2 核心流程

`obtain(spec)` 的查找流程可以用下面的伪代码描述：

```
fn obtain(spec):
    if data 目录存在 且 data.obtain(spec) 找到了:
        return data 里的根目录            # 优先级 1：本地
    if cache 目录存在:
        if cache.obtain(spec) 找到了:
            return cache 里的根目录       # 优先级 2：已下载缓存
        if spec.namespace == "preview":  # 只有 @preview 才联网
            archive = universe.package(spec)   # 下载 .tar.gz
            cache.store(spec, 解压 archive)    # 原子写进 cache
            return cache.obtain(spec)          # 再从 cache 取
    return NotFound(spec)                # 全都没找到 → 报错
```

两个要点：

- **短路返回**：只要在某一级找到就立刻返回，不会继续往下查，也不会联网。这意味着本地 data 目录里的包可以「遮蔽」同名在线包。
- **下载是副作用**：联网下载会把包解压写进 cache 目录，所以 `obtain` 可能有文件系统副作用；但通过先解压到临时目录再原子移动，保证不会写出半包（详见 4.4）。

#### 4.1.3 源码精读

`SystemPackages` 结构体本身就三个字段，对应三个来源：[`crates/typst-kit/src/packages.rs:33-39`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L33-L39)（`data` / `cache` 是 `Option<FsPackages>`，`universe` 是 `UniversePackages`）。

查找逻辑的核心是 `obtain` 方法：[`crates/typst-kit/src/packages.rs:95-124`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L124)。其中关键片段：

```rust
// 优先级 1：本地 data 目录
if let Some(packages) = &self.data
    && let Some(root) = packages.obtain(spec)
{
    return Ok(root);
}

// 优先级 2 & 3：cache 目录，必要时联网下载
if let Some(cache) = &self.cache {
    if let Some(root) = cache.obtain(spec) {
        return Ok(root);
    }
    // 只有 @preview 才下载
    if spec.namespace == UniversePackages::NAMESPACE {
        let mut archive = self.universe.package(spec)?;
        cache.store(spec, |tempdir| { archive.unpack(tempdir) ... })?;
        if let Some(root) = cache.obtain(spec) {
            return Ok(root);
        }
    }
}

Err(PackageError::NotFound(spec.clone()))
```

> 这段用了 Rust 的 `let ... && let ...` 链式 `let`（较新的稳定语法），把「data 存在 且 在 data 里找到」两个条件写成一行。`UniversePackages::NAMESPACE` 就是字符串 `"preview"`：[`crates/typst-kit/src/packages.rs:326`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L326)。

而「找到之后怎么用」发生在 `SystemFiles::root`：当一个 `FileId` 的根是 `VirtualRoot::Package(spec)` 时，就调用 `self.packages.obtain(spec)` 把包名解析成磁盘目录（`FsRoot`）：[`src/world.rs:252-257`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L252-L257)。

```rust
fn root(&self, id: FileId) -> FileResult<FsRoot> {
    Ok(match id.root() {
        VirtualRoot::Project => self.project.clone(),
        VirtualRoot::Package(spec) => self.packages.obtain(spec)?,
    })
}
```

这正是「编译器只要包名，CLI 负责把包名翻译成真实路径」的接缝。

#### 4.1.4 代码实践

**目标**：体会「data 优先于 cache」的短路逻辑。

1. 阅读 [`crates/typst-kit/src/packages.rs:95-124`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L124) 的 `obtain`。
2. 假设 `@preview/foo:1.0.0` 已经被下载并存在于 cache 目录，同时你在 data 目录里也手动放了一个 `@preview/foo:1.0.0`（内容不同）。
3. 推测：编译时 Typst 会用哪一个？
4. **预期结果**：用 data 目录里的版本（优先级 1 短路返回，根本不会看 cache）。这是一个「待本地验证」的推断：你可以用本讲综合实践里的方法，分别向 `--package-path`（data）和 `--package-cache-path`（cache）放同名包，再编译观察实际生效的是哪一个。

#### 4.1.5 小练习与答案

**练习 1**：如果系统上既没有 data 目录、也没有 cache 目录（`system_data()` / `system_cache()` 都返回 `None`），编译一个 `@local/demo:0.1.0` 会发生什么？

**答案**：`obtain` 里 data、cache 两个分支都因为 `Option` 是 `None` 而被跳过；`@local` 不等于 `"preview"`，所以也不会联网；最终落到 `Err(PackageError::NotFound(spec))`，编译器报「package not found」。

**练习 2**：为什么下载分支要用 `if spec.namespace == UniversePackages::NAMESPACE` 守卫，而不是对所有命名空间都尝试下载？

**答案**：因为 Typst Universe 只服务 `@preview` 命名空间。`@local`、`@myorg` 等私有命名空间没有对应的在线仓库，强试下载只会得到 404。所以只有 `@preview` 走联网路径，其余命名空间只能从本地 data 目录找。

---

### 4.2 CLI 薄壳：system() 如何装配与环境变量覆盖

#### 4.2.1 概念说明

`typst-kit` 提供的 `SystemPackages` 是通用能力，它不知道命令行参数长什么样。typst-cli 需要一个「翻译层」：读用户的 `--package-path` / `--package-cache-path`，决定 data 和 cache 两个槽位分别指向哪里，再交给 `SystemPackages::from_parts` 组装。这个翻译层就是 typst-cli 的 `src/packages.rs`，只有一个函数 `system()`。

这里的关键设计是**覆盖而非追加**：用户传了 `--package-path`，就用它**替换**掉系统默认的 data 目录；没传，才回落到 `system_data()` 给出的平台默认目录。

#### 4.2.2 核心流程

两个槽位的解析逻辑对称：

```
data   = --package-path    存在? → 用它    否则 → system_data()   (可能为 None)
cache  = --package-cache-path 存在? → 用它 否则 → system_cache()  (可能为 None)
universe = 永远构造一个 UniversePackages(下载器)        # 与参数无关
```

注意三点：

- 两个环境变量 `TYPST_PACKAGE_PATH` / `TYPST_PACKAGE_CACHE_PATH` 是由 **clap** 自动注入到对应字段的（`#[clap(env = ...)]`），所以对 `system()` 来说，「环境变量赋值」和「命令行赋值」最终都汇入同一个 `Option<PathBuf>` 字段，**优先级由 clap 决定**：命令行 > 环境变量 > 默认。
- `universe` 槽位**总是**被构造，不论用户有没有传包路径——它只是一个「懒下载」的句柄，真正联网发生在 `obtain` 里。
- 如果某槽位最终是 `None`（既没传参数，平台也没有对应目录），`SystemPackages` 在该级就「跳过」，不会报错。

#### 4.2.3 源码精读

整个薄壳只有十几行：[`src/packages.rs:6-18`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs#L6-L18)。

```rust
pub fn system(args: &PackageArgs) -> SystemPackages {
    SystemPackages::from_parts(
        args.package_path
            .clone()
            .map(FsPackages::new)
            .or_else(FsPackages::system_data),
        args.package_cache_path
            .clone()
            .map(FsPackages::new)
            .or_else(FsPackages::system_cache),
        UniversePackages::new(crate::download::downloader()),
    )
}
```

逐行解读：

- `args.package_path.clone().map(FsPackages::new)`：用户给了路径就包成 `FsPackages`（`Some`）。
- `.or_else(FsPackages::system_data)`：`Option::or_else` 只在左侧是 `None` 时才求值右侧。所以「用户路径优先，否则用平台默认 data 目录」。注意 `or_else` 接的是**惰性**闭包——当左侧已经是 `Some` 时，`system_data()` 根本不会被调用。
- 第二个槽位 cache 同理。
- 第三个槽位 `UniversePackages::new(crate::download::downloader())` 把 u3-l3 讲的下载器（带 user-agent、`--cert` 证书、进度报告）接进来，所以包下载与自更新共用同一套网络栈。

`PackageArgs` 本身的定义在 args.rs，两个选项各自绑定一个环境变量：[`src/args.rs:450-464`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L450-L464)。

```rust
pub struct PackageArgs {
    #[clap(long = "package-path", env = "TYPST_PACKAGE_PATH", value_name = "DIR")]
    pub package_path: Option<PathBuf>,

    #[clap(long = "package-cache-path", env = "TYPST_PACKAGE_CACHE_PATH", value_name = "DIR")]
    pub package_cache_path: Option<PathBuf>,
}
```

#### 4.2.4 代码实践

**目标**：验证「命令行覆盖环境变量、环境变量覆盖默认」的优先级。

1. 运行 `typst info`，记下 `Package path` 和 `Package cache path` 两行的默认值（这是平台默认目录）。
2. 设置环境变量后再运行：`TYPST_PACKAGE_PATH=/tmp/mydata typst info`，观察 `Package path` 是否变成 `/tmp/mydata`。
3. 再叠加命令行覆盖：`TYPST_PACKAGE_PATH=/tmp/mydata typst info --package-path /tmp/cli`（若 `info` 子命令支持该选项），观察是否命令行胜出。
4. **预期结果**：环境变量生效；若命令行也给出，命令行优先。

> 注意：`typst info` 解析的是 `TYPST_PACKAGE_PATH` 环境变量并回落到 `FsPackages::system_data()`（见 [`src/info.rs:343-354`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L343-L354)），它**不会**读取某次 `compile` 的 `--package-path` 覆盖，所以它是观察「环境变量 + 默认」的好工具，但不反映某次编译里命令行临时指定的路径。如果你本地 `typst info --help` 没有列出 `--package-path`，就以步骤 2 的环境变量方式验证即可（待本地确认 info 是否暴露该选项）。

#### 4.2.5 小练习与答案

**练习 1**：`args.package_path.map(FsPackages::new).or_else(FsPackages::system_data)` 为什么用 `or_else` 而不是 `unwrap_or`？

**答案**：`or_else` 接闭包、惰性求值；`unwrap_or` 接值、立即求值。若用 `unwrap_or(FsPackages::system_data())`，即使 `package_path` 已经是 `Some`，`system_data()` 也会被无条件调用一次（多一次 `dirs::data_dir()` 查询，且生成一个被丢弃的 `FsPackages`）。`or_else` 避免了这种无用功。

**练习 2**：用户同时设置了 `TYPST_PACKAGE_PATH=/a` 并在命令行写了 `--package-path /b`，`args.package_path` 最终是什么？

**答案**：是 `/b`。clap 对带 `env` 的字段，命令行值的优先级高于环境变量，所以字段持有命令行值；环境变量只在命令行未提供时才作为回退。然后 `system()` 用 `/b` 构造 data 槽位，`system_data()` 因惰性不会被调用。

---

### 4.3 FsPackages：本地包目录约定与路径解析

#### 4.3.1 概念说明

`FsPackages` 表示「从一个固定结构的目录里提供包」。它对目录结构有严格约定——**三层目录对应包名的三个部分**：

```
<根目录>/
└── <namespace>/        # 顶层 = 命名空间，如 local / preview
    └── <name>/         # 第二层 = 包名，如 demo
        └── <version>/  # 第三层 = 版本，如 0.1.0
            ├── typst.toml   # 清单
            └── lib.typ      # 入口（由 manifest 指定）
```

`FsPackages` 只是一个对 `PathBuf` 的新类型包装（`struct FsPackages(PathBuf)`），本身不存任何状态。它的两个「平台默认」构造器 `system_data()` 和 `system_cache()` 分别给出 data 目录和 cache 目录的标准位置。

#### 4.3.2 核心流程

给定包规格 `spec = @local/demo:0.1.0`，`FsPackages::obtain` 这样定位目录：

```
subdir = "{namespace}/{name}/{version}"   # "local/demo/0.1.0"
dir = self.path().join(subdir)
if dir.exists(): return Some(FsRoot::new(dir))
else: return None
```

平台默认目录（`system_data` / `system_cache`）由 `dirs` crate 探测：

| 来源 | Linux | macOS | Windows |
| --- | --- | --- | --- |
| data | `$XDG_DATA_HOME/typst/packages` 或 `~/.local/share/typst/packages` | `~/Library/Application Support/typst/packages` | `%APPDATA%/typst/packages` |
| cache | `$XDG_CACHE_HOME/typst/packages` 或 `~/.cache/typst/packages` | `~/Library/Caches/typst/packages` | `%LOCALAPPDATA%/typst/packages` |

> 「data 是给人放包的，cache 是给程序缓存下载的」——这个语义区分在 `latest_version` 里会被用到：只有 data 目录会参与「查找本地最新版本」。

#### 4.3.3 源码精读

`FsPackages` 是个极简的新类型：[`crates/typst-kit/src/packages.rs:151-158`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L151-L158)。

路径定位的实现在 `obtain`：[`crates/typst-kit/src/packages.rs:191-195`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L191-L195)。

```rust
pub fn obtain(&self, spec: &PackageSpec) -> Option<FsRoot> {
    let subdir = eco_format!("{}/{}/{}", spec.namespace, spec.name, spec.version);
    let dir = self.path().join(subdir.as_str());
    dir.exists().then_some(FsRoot::new(dir))
}
```

- 用 `eco_format!` 拼出 `namespace/name/version`，拼到根目录后面。
- `dir.exists().then_some(...)`：目录存在才返回 `Some`，否则 `None`。**它不校验目录里有没有 `typst.toml`**——清单校验是后续 `init` 或编译器加载清单时才做的。

平台默认目录探测：[`crates/typst-kit/src/packages.rs:167-182`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L167-L182)，`system_data` 用 `dirs::data_dir()`，`system_cache` 用 `dirs::cache_dir()`，各自再 `join("typst/packages")`。

smoke 测试正是按这个约定摆放文件的：[`tests/smoke.rs:141-149`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L141-L149) 把 `typst.toml` 写到 `local/demo/0.1.0/typst.toml`，对应 `@local/demo:0.1.0`。

#### 4.3.4 代码实践

**目标**：手工构造一个本地包，验证三层目录约定。

1. 建一个临时目录作为包根，例如 `/tmp/pkgroot`。
2. 按约定建出 `local/demo/0.1.0/`，并在其中放 `typst.toml` 和 `lib.typ`：

   ```toml
   # /tmp/pkgroot/local/demo/0.1.0/typst.toml
   [package]
   name = "demo"
   version = "0.1.0"
   entrypoint = "lib.typ"
   ```

   ```typ
   // /tmp/pkgroot/local/demo/0.1.0/lib.typ
   #let hello() = [Hello from local package!]
   ```

3. 写一个 `main.typ`：`#import "@local/demo:0.1.0": hello; #hello()`。
4. 编译：`typst compile main.typ --package-path /tmp/pkgroot`。
5. **预期结果**：编译成功，输出 PDF 里出现 `Hello from local package!`。这验证了 `FsPackages::obtain` 用 `local/demo/0.1.0` 子目录找到了包根。

#### 4.3.5 小练习与答案

**练习 1**：如果你把包放在 `/tmp/pkgroot/demo/0.1.0/`（漏掉了 `local` 这一层），编译会怎样？

**答案**：`FsPackages::obtain` 拼出的子目录是 `local/demo/0.1.0`，于是去 `/tmp/pkgroot/local/demo/0.1.0` 找，找不到（因为你放成了 `/tmp/pkgroot/demo/0.1.0`），`dir.exists()` 为假，返回 `None`。该级被跳过，最终报 package not found。顶层目录必须正好是命名空间名。

**练习 2**：`obtain` 找到目录后返回 `FsRoot`，但目录里可能根本没有 `typst.toml`。这一步校验在哪里发生？

**答案**：`FsPackages::obtain` 只判断 `dir.exists()`，不读清单。清单的读取与校验发生在更上层——编译器加载包时会读 `typst.toml`，`init` 命令则会显式调用 `parse_manifest` + `manifest.validate`（见 4.5 与 u3-l5）。

---

### 4.4 UniversePackages：在线包下载与缓存原子写回

#### 4.4.1 概念说明

`UniversePackages` 负责对接 Typst Universe 在线仓库（`https://packages.typst.org`）。它提供两种能力：下载某个具体包（`package`），以及查询某个包的最新版本（`latest_version`，需要先下载包索引 `index.json`）。

它的下载器不是 typst-cli 自己造的，而是由 `crate::download::downloader()` 注入——这个下载器带 user-agent、可选的 `--cert` 自定义证书、以及终端进度报告（u3-l3 详讲）。所以「包下载」和「自更新」复用同一套网络与进度基础设施。

下载到的包是 `.tar.gz`，会被解压写进 cache 目录。写回过程用了「先写临时目录、再原子改名」的策略，避免多个 Typst 实例并发下载同一个包时互相破坏。

#### 4.4.2 核心流程

在线下载并缓存的整体流程：

```
UniversePackages.package(spec):
    url = "{registry}/preview/{name}-{version}.tar.gz"
    data = downloader.download(spec, url)     # spec 作为进度 key
    return tar::Archive(GzDecoder(data))

SystemPackages.obtain 里的下载分支:
    archive = universe.package(spec)
    cache.store(spec, |tempdir| archive.unpack(tempdir))
    return cache.obtain(spec)
```

`cache.store` 的原子写回策略：

1. 在目标目录**同级**建一个临时目录 `.tmp-{version}-{随机数}`（保证和目标在同一挂载点，`rename` 才能原子生效）。
2. 把包内容写进临时目录。
3. 用 `std::fs::rename` 把临时目录改名为最终目录。
4. 如果目标目录已被另一个并发实例先填好（报 `DirectoryNotEmpty`），则忽略错误——说明已经有人成功放好了。
5. `Tempdir` 的 `Drop` 实现保证出错时清理临时目录。

> 这是一种经典的「原子替换」文件系统模式：写到一个隐藏的临时位置，最后用一次原子 `rename` 切换到正式位置，读者永远不会看到「写了一半」的状态。

#### 4.4.3 源码精读

`UniversePackages` 持有 registry URL、下载器和懒加载的包索引：[`crates/typst-kit/src/packages.rs:313-321`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L313-L321)。默认 URL 是 `https://packages.typst.org`：[`crates/typst-kit/src/packages.rs:330-332`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L330-L332)。

下载单个包的实现：[`crates/typst-kit/src/packages.rs:351-380`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L351-L380)，其中 URL 拼装：

```rust
let url = format!("{}/{}/{}-{}.tar.gz", self.url, Self::NAMESPACE, spec.name, spec.version);
```

下载结果先 `GzDecoder` 解压再交给 `tar::Archive`。若下载返回 `NotFound`，还会尝试查最新版本给出更精确的 `VersionNotFound` 错误提示。

原子写回的核心是 `FsPackages::store`：[`crates/typst-kit/src/packages.rs:225-278`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L225-L278)。关键的 rename 容错：

```rust
match std::fs::rename(&tempdir, &package_dir) {
    Ok(()) => Ok(()),
    Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => Ok(()),
    Err(err) => Err(error("failed to move downloaded package directory", err)),
}
```

注释（源码 L260-272）解释了为什么容忍 `DirectoryNotEmpty`：这意味着另一个实例已经抢先成功放好了包，当前这次可以安全放弃。代价是不校验已存在包的完整性——这是有意的权衡。

自动版本探测 `latest_version`：[`crates/typst-kit/src/packages.rs:385-412`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L385-L412) 会先懒加载 registry 的 `index.json`（[`crates/typst-kit/src/packages.rs:423-438`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L423-L438)），过滤出同名包取最大版本号。索引的每条记录刻意不完整反序列化，这样遇到本版本编译器无法解析的包可以跳过而不整体失败。

#### 4.4.4 代码实践

**目标**：观察下载进度与缓存写回（不依赖外网也能理解逻辑）。

1. 阅读 [`src/download.rs:14-32`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L14-L32) 的 `downloader()`，注意它用 `PackageSpec` 作为进度 `key`，在 `ProgressDownloader` 里转成人类可读的包名。
2. 阅读 [`crates/typst-kit/src/packages.rs:225-278`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L225-L278) 的 `store`，画出「临时目录 → rename → 容错」的流程。
3. 若本地有外网：编译一个 `#import "@preview/cetz:0.3.1": *` 的小文档，观察终端打印 `downloading @preview/cetz:0.3.1` 与进度行刷新；编译完成后查看 cache 目录下是否出现 `preview/cetz/0.3.1/`。
4. 若无外网：在断网下编译同一个文档，观察报错信息（应为网络失败相关），并对照 `package` 方法里 `Err(err) => Err(PackageError::NetworkFailed(...))` 的分支。
5. **预期结果**：有外网时 cache 目录被原子写入；无外网时得到 `NetworkFailed` 而非半包。

#### 4.4.5 小练习与答案

**练习 1**：为什么临时目录要建在「目标目录的同级」（`base_dir.join(".tmp-...")`）而不是系统全局的 `/tmp`？

**答案**：因为最后一步用的是 `std::fs::rename`，而 `rename` 在跨挂载点时会失败（Rust 标准库文档明说）。把临时目录放在目标旁边，能最大程度保证二者在同一文件系统/挂载点上，`rename` 才是原子的。

**练习 2**：`latest_version` 对 `@local` 命名空间会走哪条路？

**答案**：`SystemPackages::latest_version`（[`crates/typst-kit/src/packages.rs:127-142`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L127-L142)）里，`spec.namespace != "preview"` 时只查 **data** 目录的 `latest_version`（扫描 `namespace/name/` 下所有版本子目录取最大值），不查 cache，也不联网。这体现了「cache 不用于本地版本探测」的语义分工。

---

### 4.5 复用：SystemWorld 与 init 如何共享 system()

#### 4.5.1 概念说明

`packages::system()` 是一个纯粹的「参数 → SystemPackages」工厂，不持有任何子命令特有状态。因此它能被两个截然不同的入口复用：

- **编译链路**：`compile` / `watch` / `eval` / `query` 都会构造 `SystemWorld`，而 `SystemWorld` 内部的 `SystemFiles` 持有一个 `SystemPackages`，用来解析编译过程中遇到的所有 `@namespace/...` 导入。
- **脚手架链路**：`init` 命令构造 `SystemPackages` 不是为了编译，而是为了 `obtain` 一个模板包的根目录，进而读它的 `typst.toml`、复制模板文件。

这种复用正是「薄壳 + 通用实现」分层带来的好处：命令行装配逻辑只写一遍，编译与脚手架各自取用。

#### 4.5.2 核心流程

两条链路对 `SystemPackages` 的使用方式对比：

```
编译链路:
  WorldArgs.package (PackageArgs) ──▶ system() ──▶ SystemFiles.packages
  编译中遇到包 FileId ──▶ SystemFiles::root ──▶ packages.obtain(spec) ──▶ FsRoot ──▶ 读源码

init 链路:
  InitCommand.package (PackageArgs) ──▶ system() ──▶ packages
  packages.obtain(spec) ──▶ FsRoot ──▶ 读 typst.toml ──▶ 复制 template 目录
```

差别在于：编译链路把 `SystemPackages` 藏在 `SystemFiles` 里、由 `FileLoader` 按需触发；init 链路则直接拿来用一次，拿到包根后就不再需要它。

#### 4.5.3 源码精读

**编译链路**：`SystemFiles` 结构体持有 `packages: SystemPackages`：[`src/world.rs:187-191`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L187-L191)。它在 `SystemFiles::new` 末尾由 `packages::system(&world_args.package)` 构造：[`src/world.rs:239-243`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L239-L243)。

```rust
Ok(Self {
    main,
    project: FsRoot::new(root),
    packages: crate::packages::system(&world_args.package),
})
```

而 `world_args.package` 来自 `WorldArgs` 里 `#[clap(flatten)] pub package: PackageArgs`：[`src/args.rs:417-419`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L417-L419)。`WorldArgs` 又被 compile/watch/eval/query 共享，所以这些命令天然都带上了包路径选项。

**init 链路**：`init` 函数第一行就构造同样的 `SystemPackages`：[`src/init.rs:16-17`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L16-L17)。随后用 `packages.obtain(&spec)?` 拿到模板包根目录：[`src/init.rs:30-32`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L30-L32)。

```rust
let packages = packages::system(&command.package);
...
let root = packages.obtain(&spec)?;
let package_path = root.path();
```

`InitCommand` 同样有 `#[clap(flatten)] pub package: PackageArgs`：[`src/args.rs:149-151`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L149-L151)，所以 `typst init` 也支持 `--package-path` / `--package-cache-path`。

对比 `test_package_resolved`：它用 `compile --package-path <dir>` 让一个本地 `@local/demo:0.1.0` 被找到，正是走的「编译链路 → `system()` → `SystemFiles.packages.obtain`」：[`tests/smoke.rs:137-157`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L137-L157)。

#### 4.5.4 代码实践

**目标**：用 `init` 触发同一条 `obtain` 路径，体会复用。

1. 准备一个本地模板包（在 4.3.4 的目录基础上），在 `typst.toml` 里加 `[template]` 段：`path = "template"`、`entrypoint = "main.typ"`，并在包内建 `template/main.typ`。
2. 运行 `typst init @local/demo:0.1.0 --package-path /tmp/pkgroot --dir myproj`。
3. **预期结果**：`init` 调用 `packages.obtain(&spec)` 找到 `/tmp/pkgroot/local/demo/0.1.0`，读其 `typst.toml`，把 `template/` 目录复制成新项目 `myproj/`，并打印成功提示与下一步 `typst watch main.typ` 命令。
4. 对照 [`src/init.rs:30-50`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L30-L50) 确认：`obtain` 之后是 `parse_manifest` → `validate` → `scaffold_project` → `print_summary`。

> 若 `@local` 模板的 `[template]` 段书写有困难，可直接观察一个 `@preview` 模板：`typst init @preview/<某模板>`，它走的是 `obtain` 的「下载到 cache」分支（4.4），但 `init` 之后的处理逻辑完全相同。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `compile` 和 `init` 能用同一个 `packages::system()`，尽管二者对包的用途完全不同？

**答案**：因为 `system()` 只做「把 `PackageArgs` 装配成 `SystemPackages`」这一件与用途无关的事。`SystemPackages` 提供的是通用的 `obtain(spec) -> FsRoot` 能力——编译用它来读包内源码，`init` 用它来定位模板目录。装配与使用解耦，所以能复用。

**练习 2**：`SystemFiles` 把 `SystemPackages` 存为字段（`packages: SystemPackages`），而 `init` 只是局部变量用完即弃。这种差异反映了什么？

**答案**：编译是长生命周期的——`SystemWorld` 在一次编译（乃至 watch 的多轮编译）中要反复解析不断出现的包导入，所以 `SystemPackages` 必须作为状态常驻。`init` 只需解析一次模板包规格就完成了使命，之后不再访问包存储，因此用局部变量即可，无需持久化。

## 5. 综合实践

把本讲的知识串起来：手工构造一个本地包，分别用「编译」和「init」两条链路验证 `obtain` 的解析。

1. **建包目录**（遵循 4.3 的三层约定）：

   ```
   /tmp/pkgroot/local/demo/0.1.0/
   ├── typst.toml
   ├── lib.typ
   └── template/
       └── main.typ
   ```

   `typst.toml`：

   ```toml
   [package]
   name = "demo"
   version = "0.1.0"
   entrypoint = "lib.typ"

   [template]
   path = "template"
   entrypoint = "main.typ"
   ```

   `lib.typ`：`#let greet() = [Hi from @local/demo]`
   `template/main.typ`：`#import "@local/demo:0.1.0": greet; #greet()`

2. **编译链路验证**：写一个 `main.typ` 内容为 `#import "@local/demo:0.1.0": greet; #greet()`，运行：

   ```bash
   typst compile main.typ --package-path /tmp/pkgroot
   ```

   预期成功，PDF 含 `Hi from @local/demo`。这对应 `SystemFiles::root` → `packages.obtain`（4.1、4.5）。

3. **故意失败验证**：把 `--package-path` 指向一个空目录再编译，预期看到 `error: package not found (searched for @local/demo:0.1.0)`（对照 `test_package_unresolved`：[`tests/smoke.rs:160-173`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L160-L173)）。

4. **环境变量验证**：改用 `TYPST_PACKAGE_PATH=/tmp/pkgroot typst compile main.typ`（不写 `--package-path`），预期同样成功，证明环境变量注入（4.2）。

5. **init 链路验证**：运行：

   ```bash
   typst init @local/demo:0.1.0 --package-path /tmp/pkgroot --dir myproj
   ```

   预期生成 `myproj/`，内含从 `template/` 复制的 `main.typ`，并打印成功提示。这对应 `init` 复用 `system()` → `obtain`（4.5）。

6. **思考题**：步骤 2 与步骤 5 用的是不是**同一个** `packages.obtain` 调用路径？答案是肯定的——这正是本讲的核心结论「薄壳 `system()` 被编译与脚手架共享」。

## 6. 本讲小结

- Typst 的包有三级来源：**data 目录（本地）→ cache 目录（已下载缓存）→ Universe 在线下载**，`SystemPackages::obtain` 按此优先级短路查找，data 可遮蔽同名在线包。
- typst-cli 的 `src/packages.rs` 只是一个十几行的薄壳 `system()`，把 `PackageArgs` 装配成 `SystemPackages::from_parts(data, cache, universe)`。
- `--package-path` / `--package-cache-path` 选项与 `TYPST_PACKAGE_PATH` / `TYPST_PACKAGE_CACHE_PATH` 环境变量，经 clap 汇入同一字段，优先级为「命令行 > 环境变量 > 平台默认」，且是**覆盖**而非追加。
- `FsPackages` 约定「namespace/name/version」三层目录；`obtain` 只判断目录是否存在，不校验清单内容。
- 在线包仅对 `@preview` 命名空间下载，下载为 `.tar.gz` 解压后用「临时目录 + 原子 rename」写回 cache，保证并发安全与无半包。
- 同一个 `system()` 被 `SystemWorld`（编译链路，常驻为 `SystemFiles.packages`）与 `init`（脚手架链路，用完即弃）复用。

## 7. 下一步学习建议

- **u3-l3 网络下载与进度报告**：本讲的 `UniversePackages` 与自更新都依赖 `crate::download::downloader()`，下一讲深入这个下载器的 TLS 证书与进度刷新机制。
- **u3-l5 init 项目脚手架**：本讲只讲到 `init` 复用 `obtain` 定位包根，u3-l5 会完整讲解 `parse_manifest` / `validate` / `scaffold_project` 的模板复制流程。
- **u2-l1 SystemWorld**：若想更深入理解包内 `FileId` 如何经 `VirtualRoot::Package` 与 `SystemFiles::root` 衔接到本讲的 `obtain`，可回看该讲。
- **延伸阅读**：直接对照 [`crates/typst-kit/src/packages.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs) 通读一遍实现，重点看 `obtain`、`store`、`UniversePackages::package` 三个方法，把本讲的三级优先级与原子写回在源码里逐一印证。
