# FsPackages 目录约定与原子存储

## 1. 本讲目标

在 u4-1 里,我们讲清了 `SystemPackages` 的三级优先级链(data → cache → 网络),但有一个关键零件被刻意当成了黑盒:每次走「下载」分支时,那行 `cache.store(spec, |tempdir| archive.unpack(tempdir))?` 是怎么把内容**安全地**落到磁盘上的?u4-1 只点了一句——「临时目录 + 原子 rename,所以并发下载不会损坏包」。本讲就专门打开这个黑盒。

学完本讲,你应该能够:

1. 说清 `FsPackages` 的**目录层级约定**:`namespace/name/version` 三层目录是怎么映射一个 `PackageSpec` 的,以及 `obtain()` 如何凭这个约定判断「包在不在」。
2. 逐步讲出 `store()` 的**原子写入流程**:为什么要先写进一个带随机后缀的临时目录,再 `rename` 到目标位置;以及为什么遇到 `DirectoryNotEmpty` 错误可以安全忽略。
3. 解释 `Tempdir` + `Drop` 如何保证「失败时自动清理临时目录」,从而不残留垃圾文件。
4. 理解 `latest_version()` 如何扫描本地目录、用 `PackageVersion` 的 `Ord` 取最大版本,并说明为什么「0.10.0 大于 0.2.0」。

本讲承接 u4-1(SystemPackages 与三级加载链),并用到 u3-3 的 `FsRoot`、u1-2 的特性开关概念。后续 u4-3 会接着讲 `UniversePackages`(本讲里 `store` 的调用者之一)。

## 2. 前置知识

阅读本讲前,请确保你已经理解以下概念(它们在前置讲义中已建立):

- **`FsRoot`**:对 `PathBuf` 的 newtype,代表「某个虚拟根在磁盘上的真实目录」,提供 `resolve` / `load`(见 u3-3)。本讲中 `obtain()` 返回的就是它。
- **`PackageSpec`**:完整标识一个包,含 `namespace`、`name`、`version`,字符串形如 `@preview/cetz:0.3.1`(见 u4-1)。
- **特性开关**:typst-kit 默认全关(见 u1-2)。本讲涉及的特性有 `system-packages`、`universe-packages`。注意:`store` 与 `Tempdir` 只在 `universe-packages` 下存在,因为只有「下载」路径才需要落盘。
- **`PackageResult<T>`**:即 `Result<T, PackageError>`(见 u4-1)。

补充两个本讲会用到的类型定义,知道作用即可:

- **`PackageVersion`**:Typst 的语义化版本号,只有三个 `u32` 字段 `major`/`minor`/`patch`(没有预发布段)。它派生了 `Ord`/`PartialOrd`,见 [typst-syntax/src/package.rs:353-362](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L353-L362)——这是 `latest_version` 能用 `.max()` 的前提。
- **`VersionlessPackageSpec`**:去掉版本号的包标识,只有 `namespace` + `name`,用来问「这个包有哪些版本」,见 [typst-syntax/src/package.rs:270-275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L270-L275)。

> 一个贯穿全讲的直觉:**`FsPackages` 把「包」理解为磁盘上一棵 `namespace/name/version` 的目录树**,所有「查包」「存包」「找最新版」的操作,都只是在这棵树上做 `join` + `exists` / `read_dir` / `rename`。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开:

| 文件 | 作用 |
|------|------|
| [src/packages.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs) | **本讲核心**。定义 `FsPackages`(含 `new`/`system_data`/`system_cache`/`path`/`obtain`/`latest_version`/`store`)以及私有的 `Tempdir`。 |
| [typst-syntax/src/package.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs) | `PackageSpec` / `PackageVersion` / `VersionlessPackageSpec` 的定义,`PackageVersion` 的 `Ord` 派生。 |
| [src/files.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs) | `FsRoot::new` 的定义,`obtain` 返回它(见 u3-3)。 |

> 提醒:`SystemPackages` 与 `UniversePackages` 也在 `packages.rs` 里,但它们各有独立讲义(u4-1、u4-3)。本讲**只聚焦 `FsPackages` 这一个类型**——它是 `SystemPackages` 的 `data`/`cache` 两个字段的类型,也是 `store` 真正执行落盘的地方。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:

1. **`FsPackages`:目录约定与查询** —— 它是什么、目录怎么组织、`obtain()` 如何判断包是否存在。
2. **`store()`:临时目录与原子 rename** —— 并发安全的写入机制,以及 `Tempdir` + `Drop` 的自动清理。
3. **`latest_version()`:扫描本地版本号** —— 在目录树里取最大版本。

### 4.1 FsPackages:目录约定与查询

#### 4.1.1 概念说明

`FsPackages` 是 typst-kit 里最朴素的一种包来源:**它就是一个磁盘目录,只要这个目录按约定组织,它就能提供包。** 它的定义极其简单——是对 `PathBuf` 的 newtype:

```rust
/// Serves packages from a well-structured directory on the file system.
///
/// This directory should be structured as follows:
/// - Top-level directories denote namespaces
/// - Second-level directories denote packages
/// - Third-level directories denote package versions
#[derive(Debug, Clone)]
pub struct FsPackages(PathBuf);
```

见 [src/packages.rs:145-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L145-L152)。文档把它的全部「规则」讲完了:**顶层目录 = 命名空间,二层 = 包名,三层 = 版本**。换句话说,一个 `@preview/cetz:0.3.1` 的包,在这个根目录下就长这样:

```
<根目录>/
└── preview/              ← namespace
    └── cetz/             ← name
        └── 0.3.1/        ← version
            ├── lib.typ
            ├── typst.toml
            └── ...
```

`FsPackages` 没有数据库、没有索引、没有任何运行时状态——**「包在不在」完全等价于「这个三层目录在不在」**。这种「按约定优于配置」的设计,意味着你可以直接用 `cp` 或解压工具往里放包,`FsPackages` 不需要任何「注册」动作就能发现它。

#### 4.1.2 核心流程

`FsPackages::obtain` 把一个 `PackageSpec` 变成 `FsRoot` 的过程,就是「拼路径 + 查存在」:

```
obtain("@preview/cetz:0.3.1")
    │
    │  ① 拼 subdir = "{namespace}/{name}/{version}" = "preview/cetz/0.3.1"
    │  ② dir = self.path().join(subdir) = <根目录>/preview/cetz/0.3.1
    │
    ▼
   dir.exists() ?
    ├─ true  → 返回 Some(FsRoot::new(dir))   ← 命中:可以读文件了
    └─ false → 返回 None                       ← 未命中:这层来源没有该包
```

注意它返回的是 `Option<FsRoot>`,**不是 `Result`**。原因和 u4-1 讲过的一样:「目录不存在」对缓存层来说只是**正常的未命中**,不是错误——`SystemPackages::obtain` 会继续问下一层来源。

#### 4.1.3 源码精读

先看构造与路径访问:

```rust
impl FsPackages {
    /// 指向任意目录构造。
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self(path.into())
    }

    /// 返回它所服务的目录。
    pub fn path(&self) -> &Path {
        &self.0
    }
    // ...
}
```

见 [src/packages.rs:155-158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L155-L158) 与 [src/packages.rs:184-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L184-L187)。`new` 接受任意路径,意味着 `FsPackages` 可以指向**任何**符合约定的目录——这正是 u4-1 里 typst-cli 用 `--package-path` 覆盖标准目录的基础。

再看 `obtain` 本体,真的只有一行有效逻辑:

```rust
pub fn obtain(&self, spec: &PackageSpec) -> Option<FsRoot> {
    let subdir = eco_format!("{}/{}/{}", spec.namespace, spec.name, spec.version);
    let dir = self.path().join(subdir.as_str());
    dir.exists().then_some(FsRoot::new(dir))
}
```

见 [src/packages.rs:189-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L189-L195)。三个细节:

- `eco_format!` 拼出 `"preview/cetz/0.3.1"` 这样的相对路径。注意三层都来自 `spec`,顺序就是「namespace/name/version」,与 4.1.1 的目录约定严格一致。
- `self.path().join(...)` 把相对路径接到根目录上,得到绝对路径。
- `dir.exists().then_some(FsRoot::new(dir))`:`exists()` 为真才包成 `Some(FsRoot)`。回顾 u3-3,`FsRoot` 只是 `PathBuf` 的 newtype,这里 `FsRoot::new(dir)` 接收 `PathBuf`。

**`obtain` 没有任何特性门禁**——它是 `FsPackages` 的基础能力,只要 `FsPackages` 这个类型存在就能用。下面看两个需要特性的「便捷构造器」:`system_data` 与 `system_cache`。它们负责把「操作系统的标准目录」找出来:

```rust
#[cfg(feature = "system-packages")]
pub fn system_data() -> Option<FsPackages> {
    dirs::data_dir().map(|dir| FsPackages::new(dir.join("typst/packages")))
}

#[cfg(feature = "system-packages")]
pub fn system_cache() -> Option<FsPackages> {
    dirs::cache_dir().map(|dir| FsPackages::new(dir.join("typst/packages")))
}
```

见 [src/packages.rs:160-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L160-L170) 与 [src/packages.rs:172-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L172-L182)。两者的区别只在用 `dirs::data_dir` 还是 `dirs::cache_dir`,然后在末尾都拼上 `typst/packages`。两个要点:

- 它们都被 **`system-packages` 特性门禁**(因为依赖 `dirs` crate 找系统目录)。回顾 u4-1,`SystemPackages::new` 正是用这两个函数填充 `data`/`cache` 字段。
- 都返回 `Option`:某些精简环境(如无 HOME 的容器)取不到标准目录,`dirs::xxx_dir()` 返回 `None`,这里顺势返回 `None`——这就是 `SystemPackages` 的 `data`/`cache` 字段做成 `Option` 的根源。

它们各自对应的标准目录(来自函数文档)如下:

| 函数 | Linux | macOS | Windows |
|------|-------|-------|---------|
| `system_data` | `$XDG_DATA_HOME/typst/packages` 或 `~/.local/share/typst/packages` | `~/Library/Application Support/typst/packages` | `%APPDATA%/typst/packages` |
| `system_cache` | `$XDG_CACHE_HOME/typst/packages` 或 `~/.cache/typst/packages` | `~/Library/Caches/typst/packages` | `%LOCALAPPDATA%/typst/packages` |

见 [src/packages.rs:163-166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L163-L166) 与 [src/packages.rs:174-177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L174-L177)。`data` 与 `cache` 在同一台机器上路径不同,这正是 Typst 区分「用户主动安装的包」(data)与「自动下载的包」(cache)的物理隔离。

#### 4.1.4 代码实践

**实践目标**:亲手验证「目录约定」与 `obtain` 的对应关系——手动按三层约定建一个目录,看 `obtain` 能否命中。

这是一个轻量的**可运行示例**:

```rust
// 示例代码 —— 仅依赖 typst-kit 基础能力(obtain 无特性门禁)
//   Cargo.toml:
//   typst-kit    = { path = "../typst-kit" }
//   typst-syntax = { path = "../typst-syntax" }
use std::fs;
use typst_kit::packages::FsPackages;
use typst_syntax::package::PackageSpec;

fn main() {
    let root = std::env::temp_dir().join("typst-kit-u4l2-obtain");
    let _ = fs::remove_dir_all(&root);
    // 严格按 namespace/name/version 三层创建
    let pkg = root.join("preview").join("my-pkg").join("0.4.2");
    fs::create_dir_all(&pkg).unwrap();
    fs::write(pkg.join("lib.typ"), "hi").unwrap();

    let packages = FsPackages::new(&root);
    let spec: PackageSpec = "@preview/my-pkg:0.4.2".parse().unwrap();

    // 命中:obtain 返回 Some,且路径正是那三层目录
    let root_found = packages.obtain(&spec).expect("应当命中");
    println!("命中: {}", root_found.path().display());

    // 未命中:版本不对,目录不存在,返回 None
    let wrong: PackageSpec = "@preview/my-pkg:9.9.9".parse().unwrap();
    assert!(packages.obtain(&wrong).is_none());
    println!("未命中(版本不存在): 符合预期");

    let _ = fs::remove_dir_all(&root);
}
```

**需要观察的现象与预期结果**:

```
命中: /tmp/typst-kit-u4l2-obtain/preview/my-pkg/0.4.2
未命中(版本不存在): 符合预期
```

第一行印证 `obtain` 拼出的路径与我们手动建的目录完全一致;第二行印证「目录不存在 ⇒ `None`」。> 若你无法在本地运行,可标注为「待本地验证」——但依据 `obtain` 的源码逻辑,该结果是确定的。

#### 4.1.5 小练习与答案

**练习 1**:`obtain` 为什么返回 `Option<FsRoot>` 而不是 `Result<FsRoot, ...>`?

> **参考答案**:因为「该来源没有这个包」是**正常的、预期内的未命中**,不是错误。`SystemPackages::obtain` 会拿着 `None` 继续问下一层来源(见 u4-1)。用 `Option` 表达「有/无」,语义比 `Result` 更贴切;真正的错误(下载失败、解压失败)发生在 `store` / `UniversePackages` 那一侧,那里才用 `Result`。

**练习 2**:如果某个用户把同一个包同时放进了 data 目录和 cache 目录(同版本),`SystemPackages::obtain` 会读哪一个?为什么?

> **参考答案**:读 **data 目录**的。因为 `SystemPackages::obtain` 的优先级是 data > cache > 网络(见 u4-1),data 命中后直接 `return`,根本不会去查 cache。`FsPackages` 自身没有「优先级」概念——它只认自己那一个目录;优先级是 `SystemPackages` 通过调用顺序施加的。

---

### 4.2 store():临时目录与原子 rename

#### 4.2.1 概念说明

`obtain` 解决了「读」,`store` 解决了「写」。当 `SystemPackages::obtain` 决定从网络下载一个包时,它需要把下载内容**写进 cache 目录**——但这里有个棘手的问题:**多个 Typst 进程可能同时下载同一个包**(比如你同时编译两个都引用 `@preview/cetz:0.3.1` 的文档)。如果两个进程都直接往同一个目标目录里写文件,就可能互相覆盖、留下**写了一半的损坏包**。

`store` 的职责就是消除这个风险。它的核心思想可以用一句话概括:**先在一个「带随机后缀的临时目录」里把内容完整写好,再用一次 `rename` 原子地整体搬到目标位置**。`rename` 在同一文件系统上是原子的——要么整个完成、要么完全没发生,旁观者永远不会看到一个「写一半」的中间状态。

回顾 u4-1,`SystemPackages::obtain` 调用它的方式是:

```rust
cache.store(spec, |tempdir| {
    archive.unpack(tempdir).map_err(|err| {
        PackageError::MalformedArchive(Some(eco_format!("{err}")))
    })
})?;
```

注意 `store` 接受一个**闭包**:它把临时目录 `tempdir` 交给调用者,让调用者负责「往里写什么」(这里是解压 tar 包)。`store` 自己只负责「目录约定 + 临时目录管理 + 原子落盘」。这种分工让 `store` 既能服务于「解压下载的包」,也能服务于任何「需要原子写入一棵目录树」的场景。

#### 4.2.2 核心流程

`store` 的执行过程(对照下方源码看):

```
store(spec="@preview/cetz:0.3.1", write=闭包)

  base_dir    = <根>/preview/cetz              ← 该包所有版本的共同父目录
  package_dir = base_dir/0.3.1                 ← 这个版本的最终位置
  tempdir     = base_dir/.tmp-0.3.1-<随机数>    ← 临时位置(与 package_dir 同目录)

  ① Tempdir::create(tempdir)   create_dir_all 建出临时目录(顺带建出 base_dir)
  ② write(tempdir)              调用闭包:把内容写进临时目录(非原子)
  ③ rename(tempdir → package_dir)   原子整体搬迁
        ├─ Ok                 → 完成,返回 Ok(())
        ├─ DirectoryNotEmpty  → 别的进程已经装好了,安全忽略,返回 Ok(())
        └─ 其它错误           → 返回 Err

  ✗ 若 ①② 任一步失败:
        提前 return Err;此时 tempdir(一个 Tempdir 值)出作用域被销毁,
        Drop 自动 remove_dir_all,清掉临时目录,不留垃圾。
```

四个设计要点先记住,4.2.3 会逐行对照源码:

1. **临时目录紧挨目标目录**:`tempdir` 放在 `base_dir` 内部,而不是系统的 `/tmp`。这是为了让「临时目录」和「最终目录」尽量在**同一个挂载点(mount point)**上,从而 `rename` 能成功。
2. **随机后缀**:`.tmp-<version>-<fastrand::u32>`,让并发的两个 `store` 各写各的临时目录,互不干扰。
3. **`DirectoryNotEmpty` 被当作成功**:如果 rename 时目标已存在且非空(说明另一个进程抢先装好了),当前进程安全地认为「已经搞定」。
4. **Drop 兜底清理**:临时目录被一个 `Tempdir` 结构体持有;一旦 `store` 因任何原因提前返回,`Tempdir` 被销毁,`Drop` 会删除临时目录。

#### 4.2.3 源码精读

`store` 整体被 **`universe-packages` 特性门禁**,因为只有「下载落盘」才需要它(它引入了 `fastrand` 来生成随机后缀):

```rust
#[cfg(feature = "universe-packages")]
pub fn store(
    &self,
    spec: &PackageSpec,
    write: impl FnOnce(&Path) -> PackageResult<()>,
) -> PackageResult<()> {
    // ...
}
```

见 [src/packages.rs:224-229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L224-L229)。签名透露了它的「分工」:`write` 是一个**只调一次**(`FnOnce`)的闭包,接收临时目录路径,返回 `PackageResult<()>`——成功与否由调用者决定,`store` 只为它提供安全的落盘环境。

接着是三个路径变量的计算:

```rust
// 该版本所在的「包目录」(所有版本的共同父目录)
let base_dir = self.path().join(format!("{}/{}", spec.namespace, spec.name));
// 这个版本最终的归宿
let package_dir = base_dir.join(format!("{}", spec.version));
```

见 [src/packages.rs:234-238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L234-L238)。注意 `base_dir` 只拼了 `namespace/name`(没有 version)——它是「同一个包的所有版本」的共同父目录;`package_dir` 才加上 version。这与 4.1 的三层约定一致:多个版本会作为兄弟目录共存于 `base_dir` 下。

接下来是关键的「临时目录」创建。源码里有一段很长的注释,讲清了「为什么临时目录要放在 base_dir 旁边」:

```rust
// To prevent multiple Typst instances from interfering, we download
// into a temporary directory first and then move this directory to
// its final destination.
//
// In the `rename` function's documentation it is stated:
// > This will not work if the new name is on a different mount point.
//
// By locating the temporary directory directly next to where the
// package directory will live, we are (trying our best) making sure
// that `tempdir` and `package_dir` are on the same mount point.
let tempdir = Tempdir::create(base_dir.join(format!(
    ".tmp-{}-{}",
    spec.version,
    fastrand::u32(..),
)))
.map_err(|err| error("failed to create temporary package directory", err))?;
```

见 [src/packages.rs:240-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L240-L255)。三个要点:

- 临时目录名是 `.tmp-<version>-<随机 u32>`。`.tmp-` 前缀让它与正常版本目录(纯数字)明显区分;`fastrand::u32(..)` 的随机数保证并发进程互不踩踏。
- 临时目录的**父目录就是 `base_dir`**——即它和 `package_dir` 是「邻居」。这样 `rename(tempdir → package_dir)` 的源和目的极可能在同一挂载点,`rename` 才能成功(跨挂载点 rename 在很多系统上会失败,`std::fs::rename` 文档明确说「This will not work if the new name is on a different mount point」)。这是把临时目录放在项目目录内、而非系统 `/tmp` 的根本原因。
- `Tempdir::create` 内部是 `create_dir_all`(见 [src/packages.rs:289-292](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L289-L292)),所以它会**顺带把 `base_dir` 也建出来**——`store` 不需要单独建 `base_dir`。

然后调用闭包写入内容:

```rust
// Non-atomically write the package contents into the temporary directory.
write(tempdir.as_ref())?;
```

见 [src/packages.rs:258-259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L258-L259)。这一步是**非原子**的(解压一堆文件需要时间),但因为写在临时目录里,即便中途崩溃,也只会留下一个无人引用的 `.tmp-*` 目录,不会污染真正的 `package_dir`。注意 `?`:若闭包返回 `Err`(例如 `MalformedArchive`),`store` 立刻向上抛错——此时 `tempdir` 变量被销毁,触发 `Drop` 清理(见 4.2.3 末尾)。

最后是原子的搬迁与冲突处理:

```rust
match std::fs::rename(&tempdir, &package_dir) {
    Ok(()) => Ok(()),
    Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => Ok(()),
    Err(err) => Err(error("failed to move downloaded package directory", err)),
}
```

见 [src/packages.rs:273-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L273-L277)。这段 match 是并发安全的精髓:

- **`Ok(())`**:rename 成功,我们的临时目录已经整体变成 `package_dir`,大功告成。注意此时 `tempdir` 这个 `Tempdir` 值**持有的旧路径已经不存在了**(它被改名了),所以紧接着函数返回、`Drop` 运行时 `remove_dir_all` 会作用在一个已不存在的路径上——`_ = ...` 忽略它的错误,无害。
- **`DirectoryNotEmpty`**:目标目录 `package_dir` 已存在且非空,说明**另一个进程抢先把它装好了**。源码注释(见 [src/packages.rs:261-272](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L261-L272))明确:此时安全地当成功处理——「我们不去校验已存在包的完整性,就像包目录一开始就存在时我们也不校验一样」。也就是说,**「谁赢未定义,但内容绝不会损坏」**。代价是:若已存在的包本身是坏的,`store` 不会发现它(注释里也说,若这种坏包情况真的出现,未来可能引入文件锁或校验和)。
- **其它错误**:才真正报错。

最后看 `Tempdir` 与它的 `Drop`,这是「失败自动清理」的实现:

```rust
#[cfg(feature = "universe-packages")]
#[derive(Debug)]
struct Tempdir(PathBuf);

#[cfg(feature = "universe-packages")]
impl Tempdir {
    fn create(path: PathBuf) -> std::io::Result<Self> {
        std::fs::create_dir_all(&path)?;
        Ok(Self(path))
    }
}

#[cfg(feature = "universe-packages")]
impl Drop for Tempdir {
    fn drop(&mut self) {
        _ = std::fs::remove_dir_all(&self.0);
    }
}
```

见 [src/packages.rs:281-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L281-L300)。`Tempdir` 是个 RAII 包装:构造时建目录,销毁时(`Drop`)无条件 `remove_dir_all`。这保证了**无论 `store` 从哪一条路径返回(成功 rename、闭包失败、rename 失败),临时目录都会被清理**:

- 成功 rename 后:临时目录已被改名走,`Drop` 删一个不存在的路径,`_ =` 忽略错误。
- 任一步失败后:临时目录还在,`Drop` 把它连同里面的半成品一起删掉。

这就是为什么 4.2.2 流程图里「失败」分支不需要显式清理代码——Rust 的析构顺序自动替我们做了。`store` 因此能在错误路径上简洁地用 `?` 提前返回,而不必手写 `finally` 式的清理。

#### 4.2.4 代码实践

**实践目标**:亲手调用 `store` 写入一个伪造包,验证两件事——(1)内容落到了正确的 `namespace/name/version` 三层目录;(2)当闭包失败时,临时目录被 `Drop` 清理,没有 `.tmp-*` 残留。

这是本讲的主实践。完整可运行代码见下方 **5. 综合实践**。这里先点明要观察什么:

1. 对 `@preview/demo:1.2.0` 调 `store`,闭包里往临时目录写一个 `lib.typ`。**预期**:成功后 `base/preview/demo/1.2.0/lib.typ` 存在,且再次 `obtain` 该 spec 能命中(印证 store 与 obtain 共享同一目录约定)。
2. 对 `@preview/bad:0.0.1` 调 `store`,闭包直接返回 `Err`。**预期**:`store` 返回 `Err`;扫描 `base` 目录,**没有** `.tmp-*` 开头的残留目录(印证 `Tempdir::Drop` 已清理)。
3. **需要观察的现象**:成功路径产出正确目录树;失败路径不残留临时目录。
4. **预期结果**:见综合实践的输出。

#### 4.2.5 小练习与答案

**练习 1**:`store` 为什么把临时目录放在 `base_dir` **内部**,而不是用 `std::env::temp_dir()`(系统的 `/tmp`)?

> **参考答案**:为了让 `rename(tempdir → package_dir)` 成功。`std::fs::rename` 在跨挂载点时会失败(官方文档明确说明),而 `/tmp` 经常是一个独立挂载点(甚至是 tmpfs)。把临时目录紧挨最终目录(都在 `base_dir` 内),能最大程度保证两者在同一挂载点上,`rename` 才能原子完成。这是「正确性优先于整洁」的取舍:宁可把临时垃圾放在项目目录内(但有 Drop 兜底清理),也要换 rename 的原子性。

**练习 2**:两个进程同时对 `@preview/foo:1.0.0` 调 `store`,最终目录里的内容会不会是「两个进程写的文件的混乱拼接」?

> **参考答案**:不会。每个进程写进**各自**的随机临时目录(`.tmp-1.0.0-<不同随机数>`),内容完整后才 `rename`。rename 到目标时,若对方已装好,本进程拿到 `DirectoryNotEmpty` 并安全放弃。所以目标目录要么是 A 进程的完整内容、要么是 B 进程的完整内容,**绝不会是拼接**。这正是「并发不会损坏」的机制——u4-1 把它当作黑盒的那句话,落到源码就是这里。

**练习 3**:`store` 的闭包参数为什么是 `FnOnce` 而不是 `Fn`?

> **参考答案**:因为内容只需要写**一次**:把(下载好的)包解压进临时目录,一次就够。`FnOnce` 表达「最多调用一次」的语义,也允许闭包消耗(`move`)外部数据(比如移动 `archive` 的所有权)。这比 `Fn`(可多次调用)更贴切,也让编译器在编译期保证闭包不会被意外重复执行。

---

### 4.3 latest_version():扫描本地版本号

#### 4.3.1 概念说明

除了「精确版本在不在」(`obtain`),有时还需要回答「这个包在本地有哪些版本,最大是哪个」——比如用户写 `#import "@preview/foo": *` 但省略了版本,或者错误信息想提示「你要的版本不存在,最新的其实是 X」(见 u4-1 的 `VersionNotFound`)。`latest_version` 就是干这个的。

它返回 `Option<PackageVersion>`:`Some(最大版本)` 表示找到了;`None` 表示该包在本地一个版本都没有。注意它接收的是 `VersionlessPackageSpec`(只有 namespace + name,不含 version)——因为查询的本来就是「版本集合」。

一个重要的使用约定(在 u4-1 的 `SystemPackages::latest_version` 里体现):**本地版本扫描只查 data 目录,不查 cache 目录**。源码注释解释了原因:cache 目录是「自动下载的缓存」,不是「本地包的存储地」,所以不该用它来判断「用户本地有哪些版本」。见 [src/packages.rs:203-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L203-L205)。

#### 4.3.2 核心流程

`latest_version` 的思路是「枚举目录 + 解析版本号 + 取最大」:

```
latest_version(spec={namespace="preview", name="demo"})
    │
    │  ① subdir = "preview/demo"
    │  ② 枚举 <根>/preview/demo/ 下的所有条目(子目录名即版本号字符串)
    │  ③ 把每个名字 parse() 成 PackageVersion,解析失败的(如 "0.2.0-dev")安静跳过
    │  ④ 在所有成功解析的版本中取 .max()
    │
    ▼
   Some(最大版本)  或  None(目录不存在 / 一个合法版本都没有)
```

这里有一个值得品味的细节:`PackageVersion` 只接受纯数字三段(如 `0.3.1`),任何带预发布段的名字(如 `0.2.0-dev`)都会 `parse()` 失败。`latest_version` 对此的处理是**安静跳过**(`parse().ok()`),而不是整体报错——这与 u4-3 将要讲的「包索引惰性反序列化」是同一种容错哲学:**个别坏数据不该让整个查询失败**。

#### 4.3.3 源码精读

```rust
pub fn latest_version(
    &self,
    spec: &VersionlessPackageSpec,
) -> Option<PackageVersion> {
    let subdir = format!("{}/{}", spec.namespace, spec.name);
    std::fs::read_dir(self.path().join(&subdir))
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter_map(|path| path.file_name()?.to_string_lossy().parse().ok())
        .max()
}
```

见 [src/packages.rs:199-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L199-L214)。这是一段写得很紧凑的迭代器链,逐环拆解:

- **`std::fs::read_dir(...)`**:返回 `io::Result<ReadDir>`——目录可能不存在(包压根没装过),所以是 `Result`。
- **`.into_iter()`**:`Result` 实现了 `IntoIterator`,把 `Ok(ReadDir)` 变成「产出一次 `ReadDir`」的迭代器,把 `Err` 变成「什么都不产出」的空迭代器。**这一步巧妙地把「目录不存在」降级成了「空迭代」**,从而无需写 `match` 或 `?`。
- **`.flatten()`**:`ReadDir` 本身又是迭代器(产出 `io::Result<DirEntry>`),`flatten` 把它展平,于是我们直接拿到一个个 `io::Result<DirEntry>`。
- **`.filter_map(|entry| entry.ok())`:**丢掉读取单个条目时的偶发错误,只留 `DirEntry`。
- **`.map(|entry| entry.path())`**:拿到条目的路径(`PathBuf`)。
- **`.filter_map(|path| path.file_name()?.to_string_lossy().parse().ok())`**:这是核心。`file_name()` 取末尾文件名(即版本号字符串,如 `"0.3.1"`);`parse()` 把它解析成 `PackageVersion`;**解析失败(如 `0.2.0-dev`、`not-a-version`)返回 `None`,被 `filter_map` 跳过**。注意这里的 `?` 是在闭包里(闭包返回 `Option`),所以失败只是「跳过这一条」。
- **`.max()`**:`PackageVersion` 派生了 `Ord`(见 [typst-syntax/src/package.rs:354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L354)),`.max()` 取最大值。若迭代器为空(目录不存在或全被跳过),返回 `None`。

**一个容易被忽略的点:版本号是「数值比较」而非「字符串比较」**。`PackageVersion` 的 `Ord` 是按 `major`、`minor`、`patch` 三个 `u32` **数值**逐个比较的(见 [typst-syntax/src/package.rs:353-362](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L353-L362))。所以:

\[ 0.10.0 > 0.2.0 \quad (\text{因为 } 10 > 2) \]

这和「字符串排序」(会把 `"0.10.0"` 排在 `"0.2.0"` 前面,因为字符 `'1' < '2'`)完全不同。用 `u32` 比较避免了「版本 10 反而比版本 2 旧」的经典坑。

#### 4.3.4 代码实践

**实践目标**:亲手验证「数值比较」与「跳过非法版本」两个行为。

这是一个**可运行示例**(`latest_version` 无特性门禁):

```rust
// 示例代码
//   Cargo.toml:
//   typst-kit    = { path = "../typst-kit" }
//   typst-syntax = { path = "../typst-syntax" }
use std::fs;
use typst_kit::packages::FsPackages;
use typst_syntax::package::{PackageVersion, VersionlessPackageSpec};

fn main() {
    let root = std::env::temp_dir().join("typst-kit-u4l2-version");
    let _ = fs::remove_dir_all(&root);
    let name_dir = root.join("preview").join("demo");
    // 故意混入「数值更大但字符串更小」的版本,以及一个非法版本
    for v in ["0.1.0", "0.2.0", "0.10.0", "0.2.0-dev"] {
        fs::create_dir_all(name_dir.join(v)).unwrap();
    }

    let packages = FsPackages::new(&root);
    let spec = VersionlessPackageSpec {
        namespace: "preview".into(),
        name: "demo".into(),
    };
    let latest = packages.latest_version(&spec);

    println!("最新版本: {latest:?}");
    // 0.10.0 才是数值最大;0.2.0-dev 因非法被跳过
    assert_eq!(latest, Some(PackageVersion { major: 0, minor: 10, patch: 0 }));
    println!("[成功] 最大版本是 0.10.0(数值比较),且 0.2.0-dev 被跳过");

    let _ = fs::remove_dir_all(&root);
}
```

**需要观察的现象与预期结果**:

```
最新版本: Some(PackageVersion { major: 0, minor: 10, patch: 0 })
[成功] 最大版本是 0.10.0(数值比较),且 0.2.0-dev 被跳过
```

- `0.10.0` 胜出,印证「数值比较」(若是字符串排序,`0.2.0` 会赢)。
- `0.2.0-dev` 没有让整个查询失败,也没被当成合法版本,印证「跳过非法」。

> 若你无法在本地运行,可标注为「待本地验证」——依据源码逻辑结果确定。

#### 4.3.5 小练习与答案

**练习 1**:如果 `latest_version` 遇到一个目录根本不存在(该包从未装过),会发生什么?会 panic 吗?

> **参考答案**:不会 panic,返回 `None`。`read_dir` 返回 `Err`,`.into_iter()` 把它变成空迭代器,后续链式调用都没有元素,`.max()` 最终返回 `None`。这正是 `.into_iter().flatten()` 这个惯用法的价值——把「目录读失败」优雅地降级成「空结果」,无需错误处理代码。

**练习 2**:`SystemPackages::latest_version` 在本地查找时为什么只查 data、不查 cache(见 u4-1)?

> **参考答案**:因为 cache 是「自动下载的缓存」,里面有什么版本完全取决于「之前恰好下载过哪些」,把它当作「本地可用版本」会给用户误导(比如以为某旧版本是「最新」)。而 data 是用户**主动**放置本地包的地方,语义上才适合回答「本地有哪些版本」。源码注释 [src/packages.rs:203-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L203-L205) 明确表达了这一点。

---

## 5. 综合实践

把本讲三个模块串起来:**用一个临时目录作为 `FsPackages`,调用 `store()` 写入一个伪造包,验证内容落到正确的 `namespace/name/version` 三层目录;再让闭包失败,验证 `Tempdir` 的 `Drop` 清理了临时目录。**

下面是完整的**示例代码**(非项目原有代码)。它不产生任何网络请求(`store` 的内容由我们直接写入,不涉及 `UniversePackages`/`Downloader`),可以安全运行。

```rust
// 示例代码 —— 放在你自己的练习项目里(不在 typst-kit 源码树内)
//
// Cargo.toml 依赖(typst 工作区内可用 path;工作区外改用 crates.io 版本号):
//   [dependencies]
//   typst-kit     = { path = "../typst-kit", features = ["universe-packages"] }
//   typst-library = { path = "../typst-library" }
//   typst-syntax  = { path = "../typst-syntax" }
//
// 说明:store 受 universe-packages 特性门禁;启用 system-packages 亦可(它会传递启用 universe-packages)。

use std::fs;

use typst_kit::packages::FsPackages;
use typst_library::diag::PackageError;
use typst_syntax::package::PackageSpec;

fn main() {
    // —— 用一个干净的临时目录作为包目录 ——
    let base = std::env::temp_dir().join("typst-kit-u4l2-store");
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();

    let packages = FsPackages::new(&base);

    // —— ① 成功路径:写入伪造包 @preview/demo:1.2.0 ——
    let spec: PackageSpec = "@preview/demo:1.2.0".parse().unwrap();
    packages
        .store(&spec, |tmp| {
            // 闭包只负责「往 tempdir 写内容」;目录约定与原子落盘由 store 负责
            fs::write(tmp.join("lib.typ"), "Hello from demo").unwrap();
            fs::create_dir_all(tmp.join("sub")).unwrap();
            fs::write(tmp.join("sub/x.typ"), "").unwrap();
            Ok(()) // 返回 PackageResult<()> = Result<(), PackageError>
        })
        .expect("store 应当成功");

    // 验证 1:内容落到了 namespace/name/version 三层目录下
    let target = base.join("preview").join("demo").join("1.2.0").join("lib.typ");
    println!("[成功] 包已写入 {}", target.display());
    assert!(target.exists(), "lib.typ 应存在");
    assert!(base.join("preview").join("demo").join("1.2.0").join("sub").exists(), "子目录应一并搬迁");

    // 验证 2:obtain 现在能命中 —— 印证 store 与 obtain 共享同一套目录约定
    assert!(packages.obtain(&spec).is_some(), "obtain 应命中刚写入的包");
    println!("[成功] obtain 命中新写入的包,路径 {}", packages.obtain(&spec).unwrap().path().display());

    // 验证 3:成功后没有残留的 .tmp-* 临时目录(它已被 rename 掉)
    assert!(no_tmp_leftover(&base), "成功后不应残留 .tmp-* 目录");
    println!("[成功] 成功路径无 .tmp-* 残留");

    // —— ② 失败路径:闭包返回 Err,验证 Tempdir 的 Drop 清理临时目录 ——
    let bad: PackageSpec = "@preview/bad:0.0.1".parse().unwrap();
    let res = packages.store(&bad, |_tmp| {
        Err(PackageError::Other(Some("simulated failure".into())))
    });
    assert!(res.is_err(), "store 应传播闭包的错误");
    println!("[成功] 失败路径 store 返回 Err: {res:?}");

    // 失败时 Tempdir 被销毁,Drop 会 remove_dir_all,因此不应残留 .tmp-* 目录
    assert!(no_tmp_leftover(&base), "失败时应清理临时目录,不应残留 .tmp-*");
    println!("[成功] 失败路径临时目录已被 Drop 清理,无 .tmp-* 残留");

    let _ = fs::remove_dir_all(&base);
}

/// 扫描 base,返回是否「没有 .tmp- 开头的残留目录」。
fn no_tmp_leftover(base: &std::path::Path) -> bool {
    fs::read_dir(base)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.file_name()
                .to_string_lossy()
                .starts_with(".tmp-")
        })
        .next()
        .is_none()
}
```

**操作步骤**:

1. 在 typst 工作区**外**新建一个 Cargo 项目(例如 `cargo new store-demo`),把上面代码粘进 `src/main.rs`。
2. 按注释配置 `Cargo.toml`,务必带上 `features = ["universe-packages"]`(或 `system-packages`)。
3. `cargo run`。

**需要观察的现象与预期结果**(示例输出):

```
[成功] 包已写入 /tmp/typst-kit-u4l2-store/preview/demo/1.2.0/lib.typ
[成功] obtain 命中新写入的包,路径 /tmp/typst-kit-u4l2-store/preview/demo/1.2.0
[成功] 成功路径无 .tmp-* 残留
[成功] 失败路径 store 返回 Err: Err(Other(Some("simulated failure")))
[成功] 失败路径临时目录已被 Drop 清理,无 .tmp-* 残留
```

- ① 印证 4.1 的目录约定:`store` 写出的路径正是 `preview/demo/1.2.0`,且**子目录** `sub/` 也被整体搬迁(因为 rename 是目录级的)。
- ② 印证 `store` 与 `obtain` 共享目录约定:刚 `store` 完,`obtain` 立刻命中。
- ③ 印证 4.2 的 RAII 清理:成功时临时目录被 rename 掉、失败时被 `Drop` 删掉,**两种情况都不留 `.tmp-*` 垃圾**。

> 若你无法在本地搭建该项目,可把上述输出标注为「待本地验证」——但依据本讲逐行讲解的源码逻辑,该结果是确定的。

**进阶(可选)**:把成功路径的闭包改成「先写一半文件,再返回 `Err`」(模拟解压中途失败)。重新运行,验证即便写了部分文件,`Drop` 仍会把整个 `.tmp-*` 目录连同半成品一起删干净——这正是「失败不留半成品」的体现。

## 6. 本讲小结

- `FsPackages` 是「按 `namespace/name/version` 三层目录约定提供包」的最小单元,本质是对 `PathBuf` 的 newtype,无运行时状态:「包在不在」=「三层目录在不在」。
- `obtain(spec) -> Option<FsRoot>` 把 `PackageSpec` 拼成路径并查 `exists()`,命中返回 `FsRoot`;返回 `Option` 是因为「未命中」是正常的、要交给下一层来源处理的。
- `system_data`/`system_cache` 分别定位操作系统的 data/cache 标准目录(都拼 `typst/packages`),受 `system-packages` 特性门禁,返回 `Option`(标准目录可能缺失)。
- `store(spec, write)` 用「临时目录 + 原子 rename」保证并发安全:每个进程写进 `.tmp-<version>-<随机>` 临时目录,再 `rename` 到目标;遇到 `DirectoryNotEmpty`(别人已装好)安全忽略;**「谁赢未定义,但内容绝不损坏」**。临时目录紧挨目标是为了保证同挂载点、rename 可用。
- `Tempdir` + `Drop` 提供 RAII 清理:无论 `store` 从成功还是失败路径返回,临时目录都会被 `remove_dir_all`;这让 `store` 能用 `?` 简洁地提前返回,无需手写清理。
- `latest_version` 用 `read_dir(...).into_iter().flatten()` 把「目录不存在」降级为空迭代,跳过无法解析的版本名,再用 `PackageVersion` 的**数值** `Ord` 取最大——所以 `0.10.0 > 0.2.0`。本地版本扫描只查 data、不查 cache。

## 7. 下一步学习建议

本讲把 `FsPackages`(以及 `store` 的并发安全机制)讲透了。建议接着阅读:

1. **u4-3 UniversePackages 与包索引**:本讲里 `store` 的闭包「写什么内容」来自 `SystemPackages::obtain` 中的 `archive.unpack(tempdir)`——那个 `archive` 是 `UniversePackages::package` 下载并解包 `.tar.gz` 得到的。u4-3 会讲清 URL 拼接、解包、404 → `VersionNotFound` 的映射,以及包索引的惰性反序列化(与本讲「跳过非法版本」是同一种容错哲学)。
2. **回到 u4-1 复盘并发安全**:现在你已经看懂了 `store` 的临时目录 + rename,可以重读 u4-1 里关于「`store` 保证不损坏 / `FileStore` 的锁保证不重复下载」的两层保证,体会「正确性兜底」与「效率优化」如何分工。
3. **u3-1 FileStore 与 FileLoader 抽象**:如果想进一步理解「为什么 `FileStore` 持锁就能避免重复下载」,可以回顾 `FileStore::slot` 在持锁期间完成整个加载的机制。

阅读时,可以始终带着本讲的主线:**`FsPackages` 用最朴素的目录约定 + 一次原子 rename,就把「并发安全地缓存包」这件看似复杂的事,化解成了文件系统本来就擅长的事**。
