# SystemPackages 与三级加载链

## 1. 本讲目标

当你在 Typst 文档里写下 `#import "@preview/cetz:0.3.1": *` 时，编译器需要回答一个问题:**这个包的文件到底从哪里来?** 本讲就讲清楚 typst-kit 给出的默认答案——`SystemPackages`。

学完本讲,你应该能够:

1. 说清楚 `SystemPackages` 内部的三个组成部分(数据目录、缓存目录、Universe 网络)以及它们之间的**优先级顺序**。
2. 逐步讲出 `obtain()` 的解析流程:先查 data、再查 cache,都没命中且命名空间是 `preview` 时才触发网络下载并落盘。
3. 解释为什么**并发下载不会损坏包内容**——这涉及 `FsPackages::store` 的临时目录 + 原子 rename 策略,以及 `FileStore` 持锁避免重复下载的关系。
4. 区分 `new()` 与 `from_parts()` 两种构造方式,理解 typst-cli 为什么选择后者。

本讲承接 u1-l3(模块地图与 World 契约)和 u3-3(SystemFiles 与 FsRoot)。你会看到:`SystemPackages::obtain` 最终返回的就是 u3-3 讲过的 `FsRoot`,这正是「项目文件与包文件走同一段读取代码」的关键。

## 2. 前置知识

阅读本讲前,请确保你已经理解以下概念(它们在前置讲义中已建立):

- **World 契约**:Typst 编译器只通过 `World` trait 与外界打交道(见 u1-l3)。编译需要 source、file、font、today 等回调。
- **`FileId` 与虚拟根**:Typst 用 `FileId` 标识文件,它由一个 `VirtualRoot`(`Project` 或 `Package(PackageSpec)`)加一个 `VirtualPath` 组成,与磁盘绝对路径解耦(见 u3-3)。
- **`FsRoot`**:对 `PathBuf` 的 newtype,代表「某个虚拟根在磁盘上的真实目录」,提供 `resolve` / `load` 两个方法(见 u3-3)。
- **特性开关**:typst-kit 默认关闭所有特性,按需启用(见 u1-l2)。包加载相关的特性有 `system-packages`、`universe-packages`。

补充几个本讲会用到的类型定义,你不需要完全记住,知道它们的作用即可:

- **`PackageSpec`**:完整标识一个包,包含 `namespace`(命名空间,如 `preview`)、`name`(包名)、`version`(版本)。它的字符串形式形如 `@preview/cetz:0.3.1`,见 [typst-syntax/src/package.rs:224-233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L224-L233)。
- **`PackageError`**:包加载失败的错误枚举,本讲重点用到 `NotFound`、`VersionNotFound`、`MalformedArchive` 三种,见 [typst-library/src/diag.rs:701-712](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L701-L712)。
- **`PackageResult<T>`**:就是 `Result<T, PackageError>` 的别名。

> 一个直觉:**包加载本质上是「给一个 `PackageSpec`,换一个能读文件内容的 `FsRoot`」**。整个本讲都围绕这个映射展开。

## 3. 本讲源码地图

本讲涉及的关键源码文件:

| 文件 | 作用 |
|------|------|
| [src/packages.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs) | **本讲核心**。定义 `SystemPackages`、`obtain`、`new`/`from_parts`,也定义了被它组合的 `FsPackages` 与 `UniversePackages`。 |
| [../typst-cli/src/packages.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs) | typst-cli 的装配层。展示真实的 CLI 如何用 `from_parts` 组装一个 `SystemPackages`。 |
| [src/files.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs) | `SystemFiles::root` 调用 `packages.obtain(spec)`——这是包文件与项目文件汇合的地方(见 u3-3)。 |
| [src/downloader.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs) | `Downloader` trait 的定义,`obtain` 触发下载时最终会走到这里(详见 u5-1)。 |

> 提醒:`FsPackages` 和 `UniversePackages` 虽然定义在 `packages.rs` 里,但它们各有独立的讲义(u4-2、u4-3)。本讲**把它们当作黑盒**:只需知道 `FsPackages::obtain(spec) -> Option<FsRoot>`(本地查)和 `UniversePackages::package(spec) -> 压缩包`(网络下载)即可,内部细节不在本讲展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:

1. **`SystemPackages`** —— 它是什么、由哪三部分组成、为什么这样设计。
2. **`obtain()`** —— 三级优先级链的核心解析逻辑与并发安全性。
3. **`new()` / `from_parts()`** —— 两种构造方式与 CLI 的真实装配。

### 4.1 SystemPackages:包加载的统一入口

#### 4.1.1 概念说明

`SystemPackages` 是 typst-kit 提供的「默认包加载器」。它的职责只有一句话:**给定一个 `PackageSpec`,告诉调用者这个包的文件可以从哪个磁盘目录读出来。**

回顾 u3-3 我们讲过的「双根模型」:`FileId` 的 `VirtualRoot` 要么是 `Project`(项目根),要么是 `Package(PackageSpec)`。当编译器遇到一个属于 `Package` 根的 `FileId` 时,`SystemFiles::root` 就会把里面的 `PackageSpec` 交给 `SystemPackages::obtain`,换回一个 `FsRoot`:

```rust
// files.rs —— SystemFiles 把包文件解析委托给 SystemPackages
pub fn root(&self, id: FileId) -> FileResult<FsRoot> {
    Ok(match id.root() {
        VirtualRoot::Project => self.project.clone(),
        VirtualRoot::Package(spec) => self.packages.obtain(spec)?,  // 本讲的主角
    })
}
```

这段代码见 [src/files.rs:309-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L309-L314)。**注意 `obtain` 返回的 `FsRoot` 与项目根用的是同一个类型**——这就是 u3-3 所说「项目文件与包文件走同一段读取代码」的落点。

那么一个包「可能从哪里来」?Typst 的答案是三个来源,按优先级排列:

1. **数据目录(data)**:系统级、用户**主动安装**的包存放处(如 Linux 的 `~/.local/share/typst/packages`)。
2. **缓存目录(cache)**:用于缓存**自动下载**的包(如 Linux 的 `~/.cache/typst/packages`)。
3. **Typst Universe 网络**:官方包注册表 `https://packages.typst.org`,只有 `preview` 命名空间的包才会从这里下载。

`SystemPackages` 把这三个来源打包成一个结构体。注意一个关键设计:**`data` 和 `cache` 是 `Option`**(因为某些系统环境下取不到标准目录),但 **`universe` 是必有字段**。原因是特性开关的依赖链:`system-packages` 启用时会传递启用 `universe-packages`(见下文 4.1.3),所以只要你有 `SystemPackages`,就一定也有 `UniversePackages`。

#### 4.1.2 核心流程

`SystemPackages` 的工作可以想象成一个「先问本地、再问网络」的调度器:

```
            ┌──────────────────────────────────────────────┐
输入:       │  obtain("@preview/foo:0.1.0")                │
PackageSpec └────────────────────┬───────────────────────────┘
                                 ▼
                  ┌──────────────────────────────┐
            ①    │ data 目录里有这个包吗?        │ ── 有 ──▶ 返回 Ok(FsRoot)
                  └──────────────┬───────────────┘
                                 │ 没有
                                 ▼
                  ┌──────────────────────────────┐
            ②    │ cache 目录里有这个包吗?       │ ── 有 ──▶ 返回 Ok(FsRoot)
                  └──────────────┬───────────────┘
                                 │ 没有
                                 ▼
                  ┌──────────────────────────────┐
            ③    │ 命名空间 == "preview" 且有 cache?│
                  │   是 → 从 Universe 下载,      │ ── 成功 ──▶ 返回 Ok(FsRoot)
                  │        解压到 cache,再读一次  │
                  └──────────────┬───────────────┘
                                 │ 否 / 仍失败
                                 ▼
                         返回 Err(NotFound)
```

三个要点先记住,后面 4.2 会逐行对照源码:

- **优先级**:data > cache > 网络。本地命中就不会联网。
- **下载有前提**:只有 `namespace == "preview"` 才下载(Universe 只服务 preview),而且**必须有 cache 目录**才能下载(因为下载结果要落盘到 cache)。
- **下载是副作用**:`obtain` 名义上是「查询」,但可能会**写磁盘**(把下载的包存进 cache)。源码文档专门提醒了这一点。

#### 4.1.3 源码精读

先看结构体定义:

```rust
// 受 system-packages 特性门禁
#[cfg(feature = "system-packages")]
#[derive(Debug)]
pub struct SystemPackages {
    data: Option<FsPackages>,
    cache: Option<FsPackages>,
    universe: UniversePackages,
}
```

见 [src/packages.rs:33-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L33-L39)。这段代码做了三件事:**(1) 整个类型被 `system-packages` 特性门禁**——不启用该特性,这个类型根本不存在;**(2) `data`/`cache` 是 `Option<FsPackages>`**——表示这两个目录可选;**(3) `universe` 直接是 `UniversePackages`(非 Option)**——一定存在。

为什么 `universe` 一定存在?看 Cargo.toml 里的特性依赖链就明白了:

```toml
system-packages = ["dep:dirs", "universe-packages"]   # 启用 system-packages 自动带上 universe-packages
universe-packages = ["dep:flate2", "dep:tar", "dep:fastrand"]
```

见 [Cargo.toml:66-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L66-L70)。开启 `system-packages` 会**单向传递**地开启 `universe-packages`(这是 u1-l2 讲过的特性依赖链,方向不可反推)。所以编译期就能保证:`SystemPackages` 存在 ⇒ `UniversePackages` 也存在,无需用 `Option` 表达「可能没有」。

> 小结这个模块的设计哲学:**「三个来源、两个本地可选、一个网络必有、严格按优先级」**。把 data 和 cache 都做成 `Option<FsPackages>`,让 `SystemPackages` 能优雅地应对「某系统没有标准目录」的情况——查不到就跳过那一层,不会 panic。

#### 4.1.4 代码实践

**实践目标**:用最简单的方式确认「`obtain` 返回的 `FsRoot` 与项目根是同一个类型」,从而验证 u3-3 所说的「包文件与项目文件走同一段读取代码」。

这是一个**源码阅读型实践**,无需运行:

1. 打开 [src/files.rs:299-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L299-L314)。
2. 阅读 `SystemFiles::new(project: FsRoot, packages: SystemPackages)`:它的两个字段分别是项目根 `FsRoot` 和 `SystemPackages`。
3. 阅读 `SystemFiles::root`:对 `Project` 分支返回 `self.project.clone()`(直接是 `FsRoot`),对 `Package` 分支返回 `self.packages.obtain(spec)?`(`obtain` 也返回 `FsRoot`)。
4. **需要观察的现象**:两个分支返回的是**同一个类型 `FsRoot`**。
5. **预期结论**:`SystemFiles::load`(u3-3 讲过的 `FileLoader` 实现)在拿到 `root` 后,后续 `resolve` / `load` 对项目文件和包文件**完全没有区别**。包加载的唯一特殊性,就在于「如何由 `PackageSpec` 得到那个 `FsRoot`」——这正是 `SystemPackages` 解决的全部问题。

#### 4.1.5 小练习与答案

**练习 1**:`SystemPackages` 的 `data` 和 `cache` 为什么是 `Option`,而 `universe` 不是?

> **参考答案**:`data`/`cache` 依赖操作系统提供的标准目录(通过 `dirs` crate),某些环境下可能取不到(例如无 HOME 的容器),所以做成 `Option` 以优雅跳过;而 `universe` 是一个「网络客户端」对象,不依赖具体磁盘目录,且特性依赖链 `system-packages → universe-packages` 在编译期就保证它一定可用,所以用非 `Option`。

**练习 2**:如果某个系统既没有标准 data 目录、也没有标准 cache 目录,`SystemPackages::new` 构造出的对象还有可能成功加载包吗?

> **参考答案**:几乎不可能。因为下载分支**必须有 cache 才能落盘**(见 4.2),而 data 和 cache 都为 `None` 时,`obtain` 三个来源全部缺位,只能返回 `NotFound`。除非包恰好通过自定义的 `from_parts` 注入了非标准目录。

---

### 4.2 SystemPackages::obtain:三级优先级链

#### 4.2.1 概念说明

`obtain(&self, spec: &PackageSpec) -> PackageResult<FsRoot>` 是 `SystemPackages` 的核心方法,也是外部世界查询包的唯一入口。它的文档对自己的一句话总结是:「**返回给定包内容可被加载的文件系统根;若本地没有,可能从网络下载(有磁盘副作用)。**」

`PackageResult<FsRoot>` 表示:成功就拿到一个 `FsRoot`(可读文件),失败就拿到一个 `PackageError`(说明为什么没找到)。

设计上,`obtain` 把 4.1.2 的三级流程浓缩成一个函数,并且对**错误做了精细化映射**——同样是「没找到」,它会区分「这个版本不存在(但包存在,最新版是 X)」和「这个包压根不存在」,从而让上层能给出更友好的提示(「你想用的版本不存在,要不要试试 0.2.0?」)。

#### 4.2.2 核心流程

`obtain` 的伪代码(对照真实源码看):

```
fn obtain(spec):
    # 第一级:data 目录
    if data 存在 且 data.obtain(spec) 命中:
        return Ok(那个 FsRoot)

    # 第二、三级都包在「cache 必须存在」的前提下
    if cache 存在:
        if cache.obtain(spec) 命中:           # 第二级:cache 目录
            return Ok(那个 FsRoot)

        # 第三级:网络下载(仅 preview 命名空间)
        if spec.namespace == "preview":
            archive = universe.package(spec)?    # 下载并解压,失败直接 ? 抛错
            cache.store(spec, |tmp| archive.unpack(tmp))?  # 原子落盘到 cache
            if cache.obtain(spec) 命中:          # 落盘后再查一次
                return Ok(那个 FsRoot)

    # 三级都没拿到
    return Err(NotFound(spec))
```

三个容易忽略的细节:

1. **下载分支嵌套在 `if cache 存在` 内部**。这意味着:**没有 cache 目录就无法下载**——因为你需要一个地方存放下载结果。所以 cache 同时承担「第二级缓存查询」和「第三级下载落盘地」两个角色。
2. **下载后又调了一次 `cache.obtain`**。为什么不直接用下载/落盘的结果?因为 `store` 只负责「把内容写进正确的目录」,返回的是 `()`;真正把它变成 `FsRoot` 仍走统一的 `FsPackages::obtain`,保证「无论包是预装的还是刚下载的,取 root 的代码路径完全一致」。
3. **下载只对 `preview` 触发**。`UniversePackages::NAMESPACE` 是常量 `"preview"`(见 [src/packages.rs:326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L326))。其它命名空间的包**永远不会联网下载**,只能在 data/cache 里找——这对应「本地私有包」的场景。

关于错误映射,`universe.package(spec)` 内部会区分三种下载失败(详见 u4-3):

- 网络错误(非 404)→ `PackageError::NetworkFailed`
- 404 但能查到该包的其它版本 → `PackageError::VersionNotFound(spec, 最新版本)`(提示用户升级)
- 404 且查不到任何版本 → `PackageError::NotFound(spec)`

而 `obtain` 末尾的兜底 `Err(PackageError::NotFound(spec))`,只在前两级本地查询都落空、且下载分支根本没进入(或下载错误已经被 `?` 抛出)时才会执行。

#### 4.2.3 源码精读

完整方法如下:

```rust
pub fn obtain(&self, spec: &PackageSpec) -> PackageResult<FsRoot> {
    if let Some(packages) = &self.data
        && let Some(root) = packages.obtain(spec)
    {
        return Ok(root);
    }

    if let Some(cache) = &self.cache {
        if let Some(root) = cache.obtain(spec) {
            return Ok(root);
        }

        // Download from network if it doesn't exist yet.
        if spec.namespace == UniversePackages::NAMESPACE {
            let mut archive = self.universe.package(spec)?;

            cache.store(spec, |tempdir| {
                archive.unpack(tempdir).map_err(|err| {
                    PackageError::MalformedArchive(Some(eco_format!("{err}")))
                })
            })?;

            if let Some(root) = cache.obtain(spec) {
                return Ok(root);
            }
        }
    }

    Err(PackageError::NotFound(spec.clone()))
}
```

见 [src/packages.rs:95-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L124)。逐段说明:

- **L96-L100**:第一级。注意这里用了 Rust 2024 的 `let ... && let ...` 链式 `let`(等价于「`data` 存在**且** `data.obtain` 命中」)。`FsPackages::obtain` 返回 `Option<FsRoot>`——命中返回 `Some(root)`,目录不存在就返回 `None`(见 [src/packages.rs:191-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L191-L195))。`Option` 而非 `Result`,是因为「目录不存在」对缓存层来说是**正常的未命中**,不是错误。
- **L102-L105**:第二级,逻辑同上,只是查的是 cache。
- **L108-L120**:第三级下载分支,关键见 [src/packages.rs:107-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L107-L120):
  - `self.universe.package(spec)?`:下载 `.tar.gz` 并包成 `tar::Archive`(详见 u4-3)。`?` 把任何 `PackageError`(网络失败、版本不存在等)直接向上抛。
  - `cache.store(spec, |tempdir| archive.unpack(tempdir)...)`:**把压缩包解压到 cache**。`store` 接受一个闭包,把临时目录 `tempdir` 传进来,让调用者负责往里写内容;解压失败映射为 `MalformedArchive`。
  - `if let Some(root) = cache.obtain(spec)`:落盘后**再查一次**,成功才返回。这一步几乎一定会命中(刚写进去),但代码仍保持防御性。
- **L123**:兜底,返回 `NotFound`。

**并发安全性**:这是本讲最值得品味的设计,`obtain` 的文档用专门一段说明:

> Concurrent downloads do not cause corruption, but for the purpose of efficiency, it may be desirable to avoid them. If you use the `FileStore`, this is already the case since it acquires a lock during file loading.

见 [src/packages.rs:84-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L84-L94)。这段话包含两层、互补的保证:

1. **正确性保证(不会损坏)**:由 `FsPackages::store` 的「临时目录 + 原子 rename」提供。多个进程同时下载同一个包时,各自先写进自己的 `.tmp-<版本>-<随机数>` 临时目录,最后用 `std::fs::rename` 原子地移动到目标位置。即使两个进程竞争同一下载,**后到的那个**遇到目标目录非空会安全地忽略 `DirectoryNotEmpty` 错误——所以「谁赢」未定义,但**内容绝不会损坏或写一半**。关键 rename 逻辑见 [src/packages.rs:273-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L273-L277)。临时目录被 `Tempdir` 结构体持有,失败时靠 `Drop` 自动清理(见 [src/packages.rs:295-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L295-L300))。**这部分详见 u4-2。**

2. **效率保证(避免重复下载)**:由 `FileStore` 的锁提供。回顾 u3-1:`FileStore` 内部是 `Mutex<FxHashMap<FileId, FileSlot>>`,在 `slot()` 里**持锁期间**完成整个加载(包括调用 `FileLoader::load`,进而调用 `SystemFiles::load` → `SystemFiles::root` → `packages.obtain`)。所以同一个 `FileId`(即同一个包的同一个文件)在同一个 `FileStore` 里只会被加载一次,后续线程命中缓存——**自然就避免了重复下载**。

一句话区分两者:**`store` 的临时目录 + rename 保证「就算并发也安全」;`FileStore` 的锁保证「根本不会并发」**。前者是兜底的正确性,后者是日常的效率优化。`obtain` 方法签名是 `&self`(不可变借用),它本身**不持任何锁**——所以如果你绕过 `FileStore` 直接并发调 `obtain`,安全性完全靠第一层兜底。

#### 4.2.4 代码实践

**实践目标**:亲手验证 `obtain` 的「本地命中走 data、未命中且无法下载返回 NotFound」两种结果,不产生任何真实网络请求。

这是本讲的主实践,采用**可运行的示例程序**(用一个永远返回 `NotFound` 的 `DummyDownloader` 替代真实网络)。完整代码见下方 5. 综合实践。这里先点明要观察什么:

1. 对一个**预先在 data 目录里建好**的包(目录形如 `data/preview/my-pkg/0.1.0/lib.typ`)调 `obtain`,应**命中第一级 data**,返回 `Ok(FsRoot)`,且 `root.path()` 指向那个目录。
2. 对一个**不存在的包**调 `obtain`:data 未命中、cache 未命中、进入下载分支后 `DummyDownloader` 让 `universe.package` 返回 `NotFound` → `obtain` 返回 `Err(PackageError::NotFound(...))`。
3. **需要观察的现象**:两次调用,一次 `Ok`、一次 `Err`,且全程没有联网。
4. **预期结果**:见综合实践的输出。如果你无法运行,这部分输出可标注为「待本地验证」,但依据源码逻辑它是确定的。

#### 4.2.5 小练习与答案

**练习 1**:为什么下载分支要放在 `if let Some(cache) = &self.cache` **内部**,而不是和 data 那一级并列?

> **参考答案**:因为下载下来的包需要落盘,而落盘的目标就是 cache 目录。没有 cache,下载了也没地方存,所以下载逻辑必须依赖 cache 存在。这也解释了为什么 cache 字段同时充当「缓存查询」和「下载落盘地」。

**练习 2**:假设 `cache` 存在但为空、`data` 为 `None`,对一个 `@preview/foo:0.1.0` 的包调 `obtain`,而 `UniversePackages` 返回了 `VersionNotFound(foo:0.1.0, 0.2.0)`。`obtain` 最终返回什么?

> **参考答案**:返回 `Err(PackageError::VersionNotFound(foo:0.1.0, 0.2.0))`。因为下载分支里 `self.universe.package(spec)?` 的 `?` 会把这个错误直接向上抛出,`obtain` 不会把它吞掉变成兜底的 `NotFound`——这正是错误精细化映射的体现。

**练习 3**:`obtain` 自己加锁了吗?如果有两个线程对**同一个** `SystemPackages`、**同一个**包同时调 `obtain`,会发生什么?

> **参考答案**:`obtain` 没有加锁(签名是 `&self`,内部不持锁)。两个线程可能各自走完「下载 → store → rename」流程,但 `FsPackages::store` 的临时目录带随机后缀、且 rename 对「目标非空」安全忽略,所以**不会损坏**,只是浪费了一次下载(「谁赢未定义」)。要避免这种浪费,应像 typst-cli 那样通过 `FileStore` 调用——它的锁会串行化对同一 `FileId` 的加载。

---

### 4.3 new() 与 from_parts():两种构造方式

#### 4.3.1 概念说明

`SystemPackages` 提供两个构造函数,分别面向「开箱即用」和「完全自定义」两种场景:

- **`new(downloader)`**:用**操作系统标准目录**作为 data/cache,用**官方 Universe 注册表**作为网络源。等价于「和 CLI 默认行为完全一致」。你只需传入一个 `downloader`(因为下载能力是可插拔的,见 u5-1)。
- **`from_parts(data, cache, universe)`**:三部分都由调用者**自己指定**。用于测试(注入假目录、假下载器)或自定义部署(比如把包放到非标准位置、用镜像站)。

为什么 CLI 用 `from_parts` 而不是 `new`?因为 CLI 支持 `--package-path` / `--package-cache-path` 命令行参数,允许用户**覆盖**标准目录。CLI 的逻辑是「优先用用户指定的路径,没指定才回退到系统标准目录」——这正是 `from_parts` + `FsPackages::system_data`/`system_cache` 的组合。

#### 4.3.2 核心流程

两个构造函数的关系:

```
new(downloader)
   │
   ├── data   = FsPackages::system_data()     // 系统标准 data 目录
   ├── cache  = FsPackages::system_cache()    // 系统标准 cache 目录
   └── universe = UniversePackages::new(downloader)  // 官方注册表
       │
       ▼
   from_parts(data, cache, universe)          // 统一出口


   (typst-cli 的 system(args))
   ├── data   = args.package_path.map(FsPackages::new).or_else(system_data)
   ├── cache  = args.package_cache_path.map(FsPackages::new).or_else(system_cache)
   └── universe = UniversePackages::new(downloader())
       │
       ▼
   from_parts(data, cache, universe)
```

`new` 其实就是 `from_parts` 的一个「预设了系统标准目录」的便捷封装。CLI 则用自己的「用户参数优先、否则回退标准目录」逻辑构造 data/cache,但仍走 `from_parts` 这个统一出口。

#### 4.3.3 源码精读

先看 typst-kit 的两个构造函数:

```rust
/// 用标准系统目录 + 官方注册表构造(与 CLI 默认一致)。
pub fn new(downloader: impl Downloader) -> Self {
    Self::from_parts(
        FsPackages::system_data(),
        FsPackages::system_cache(),
        UniversePackages::new(downloader),
    )
}

/// 用自定义的三部分构造。
pub fn from_parts(
    data: Option<FsPackages>,
    cache: Option<FsPackages>,
    universe: UniversePackages,
) -> Self {
    Self { data, cache, universe }
}
```

见 [src/packages.rs:52-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L52-L67)。两点注意:

- `new` 接受 `impl Downloader`(任意实现了下载器 trait 的类型),这呼应了 u5-1 讲的「下载能力可插拔」。
- `FsPackages::system_data()` / `system_cache()` 都返回 `Option<FsPackages>`——找不到标准目录时返回 `None`,正好喂给 `from_parts` 的 `Option` 参数。

再看 typst-cli 的真实装配:

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

见 [../typst-cli/src/packages.rs:6-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs#L6-L18)。这是 `from_parts` 最典型的用法,逐字段拆解:

- **data**:`args.package_path.map(FsPackages::new).or_else(FsPackages::system_data)`。即「如果用户用 `--package-path` 指定了路径,就用它;否则回退到系统标准 data 目录」。`.or_else` 的短路语义正好实现「用户参数优先」。
- **cache**:同理,用 `--package-cache-path` 覆盖,否则回退到 `system_cache`。
- **universe**:固定用官方注册表 `UniversePackages::new(...)`,下载器由 `crate::download::downloader()` 提供(typst-cli 的下载器配置,可能带自定义证书,见 u5-1)。

> 这个模式很好地体现了 typst-kit 的定位(回顾 u1-1):**kit 提供「积木」(`SystemPackages::from_parts`、`FsPackages::system_data` 等),集成方(typst-cli)负责「用命令行参数挑选积木」**。kit 不关心参数从哪来,CLI 不关心 obtain 内部细节,二者通过 `from_parts` 这个清晰的接口对接。

#### 4.3.4 代码实践

**实践目标**:对比 `new` 与 `from_parts`,理解「自定义」的威力。

这是一个**配置观察型实践**:

1. 打开 [../typst-cli/src/packages.rs:6-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs#L6-L18)。
2. 假设你在终端运行 `typst compile doc.typ --package-path /opt/my-packages`,追踪 `args.package_path` 会是什么值。
3. **需要观察的现象**:`args.package_path` 是 `Some("/opt/my-packages")`,于是 `.map(FsPackages::new)` 生成一个指向 `/opt/my-packages` 的 `FsPackages`,`.or_else(...)` 短路、**不再调用** `system_data()`。
4. **预期结论**:此后所有包优先从 `/opt/my-packages` 查找,而不是 `~/.local/share/typst/packages`。这正是 `from_parts` 相对 `new` 的价值——`new` 永远用系统目录,无法覆盖。

如果想进一步验证,可以阅读 typst-cli 的 `PackageArgs` 定义(用 `Grep` 搜 `struct PackageArgs`),确认 `package_path` / `package_cache_path` 字段确实由命令行参数填充。

#### 4.3.5 小练习与答案

**练习 1**:`new` 和 `from_parts` 哪个更「通用」?为什么 typst-kit 同时提供两者?

> **参考答案**:`from_parts` 更通用(三部分全自定义),`new` 只是 `from_parts` 的便捷封装(预设系统目录 + 官方注册表)。同时提供两者是为了「常用场景一行代码搞定,特殊场景完全可控」——这是 API 设计中常见的「便利构造 + 通用构造」双门模式。

**练习 2**:在 typst-cli 的 `system(args)` 里,如果用户**既没**指定 `--package-path`,系统也**没有**标准 data 目录(比如某些精简容器),`data` 字段最终是什么?

> **参考答案**:`args.package_path` 是 `None`,于是 `.map(...)` 仍是 `None`,接着 `.or_else(FsPackages::system_data)`——而 `system_data()` 在没有标准目录时也返回 `None`。所以 `data` 最终是 `None`,`SystemPackages` 在第一级(data)直接缺位,包只能靠 cache 或网络找。

---

## 5. 综合实践

把本讲三个模块串起来:**用 `from_parts` 组装一个只带 data 与 cache、universe 使用 `DummyDownloader` 的 `SystemPackages`,分别对「已存在」与「不存在」的 `PackageSpec` 调 `obtain`,观察返回的 `FsRoot` 或 `NotFound` 错误。**

下面是完整的**示例代码**(非项目原有代码)。它不产生任何真实网络请求(`DummyDownloader` 永远返回 `NotFound`),可以安全运行。

```rust
// 示例代码 —— 放在你自己的练习项目里(不在 typst-kit 源码树内)
//
// Cargo.toml 依赖(typst 工作区内可用 path;工作区外改用 crates.io 版本号):
//   [dependencies]
//   typst-kit    = { path = "../typst-kit", features = ["system-packages"] }
//   typst-syntax = { path = "../typst-syntax" }

use std::any::Any;
use std::fs;
use std::io::{self, Read};

use typst_kit::downloader::Downloader;
use typst_kit::packages::{FsPackages, SystemPackages, UniversePackages};
use typst_syntax::package::PackageSpec;

/// 永远返回 NotFound 的下载器:模拟「网络不可用 / 包不存在」,
/// 避免 obtain 触发任何真实网络请求。
struct DummyDownloader;

impl Downloader for DummyDownloader {
    fn stream(
        &self,
        _key: &dyn Any,
        _url: &str,
    ) -> io::Result<(Option<usize>, Box<dyn Read>)> {
        Err(io::ErrorKind::NotFound.into())
    }
}

fn main() {
    // —— 1. 准备一个「已存在」的包:data/preview/my-pkg/0.1.0/lib.typ ——
    // 目录层级必须严格是 namespace/name/version(FsPackages 的约定)。
    let data_dir = std::env::temp_dir().join("typst-kit-u4l1-data");
    let pkg_dir = data_dir.join("preview").join("my-pkg").join("0.1.0");
    fs::create_dir_all(&pkg_dir).unwrap();
    fs::write(pkg_dir.join("lib.typ"), "Hello from package!").unwrap();

    // —— 2. 准备一个空的 cache 目录(用独立目录,避免污染)——
    let cache_dir = std::env::temp_dir().join("typst-kit-u4l1-cache");
    fs::create_dir_all(&cache_dir).unwrap();

    // —— 3. 用 from_parts 组装:SystemPackages(data=有, cache=有, universe=Dummy) ——
    let packages = SystemPackages::from_parts(
        Some(FsPackages::new(data_dir.clone())),
        Some(FsPackages::new(cache_dir)),
        UniversePackages::new(DummyDownloader),
    );

    // —— 4a. 对「已存在」的包调用 obtain:期望命中第一级 data ——
    let exists: PackageSpec = "@preview/my-pkg:0.1.0".parse().unwrap();
    match packages.obtain(&exists) {
        Ok(root) => println!("[已存在] 命中 data, FsRoot -> {}", root.path().display()),
        Err(e) => println!("[已存在] 出错: {e}"),
    }

    // —— 4b. 对「不存在」的包调用 obtain:期望 data/cache 未命中,
    //         DummyDownloader 让 universe.package 返回 NotFound,最终 obtain 返回 NotFound ——
    let missing: PackageSpec = "@preview/no-such-pkg:9.9.9".parse().unwrap();
    match packages.obtain(&missing) {
        Ok(root) => println!("[不存在] 命中, FsRoot -> {}", root.path().display()),
        Err(e) => println!("[不存在] 出错: {e}"),
    }

    // —— 清理(可选)——
    let _ = fs::remove_dir_all(&data_dir);
    let _ = fs::remove_dir_all(&cache_dir);
}
```

**操作步骤**:

1. 在 typst 工作区**外**新建一个 Cargo 项目(例如 `cargo new pkg-demo`),把上面代码粘进 `src/main.rs`。
2. 按注释配置 `Cargo.toml` 的依赖,务必带上 `features = ["system-packages"]`。
3. `cargo run`。

**需要观察的现象与预期结果**:

```
[已存在] 命中 data, FsRoot -> /tmp/typst-kit-u4l1-data/preview/my-pkg/0.1.0
[不存在] 出错: package not found (searched for @preview/no-such-pkg:9.9.9)
```

- 第一行印证 4.1/4.2 的第一级:data 目录命中,`obtain` 直接返回 `Ok(FsRoot)`,**根本没走到下载分支**。
- 第二行印证 4.2 的错误映射:data/cache 都没命中 → 进入下载分支 → `DummyDownloader` 让 `universe.package` 返回 `NotFound`(且 `latest_version` 也失败)→ `obtain` 返回 `PackageError::NotFound`,其 `Display` 形如 `package not found (searched for @preview/no-such-pkg:9.9.9)`。
- 全程没有联网请求,印证了 `DummyDownloader` 的隔离作用。

> 若你无法在本地搭建该项目,可把上述输出标注为「待本地验证」——但依据本讲逐行讲解的源码逻辑,该结果是确定的。

**进阶(可选)**:把第 3 步的 `Some(FsPackages::new(cache_dir))` 改成 `None`,再对 `@preview/my-pkg:0.1.0` 运行一次。预期**仍命中 data**(因为 data 优先级最高,与 cache 有无无关),这验证了「优先级链」与「cache 缺位」的独立性。

## 6. 本讲小结

- `SystemPackages` 是 typst-kit 的默认包加载器,把**三个来源**(data 目录、cache 目录、Universe 网络)按优先级组合,对外只暴露一个查询入口 `obtain(&PackageSpec) -> PackageResult<FsRoot>`。
- 结构上 `data`/`cache` 是 `Option<FsPackages>`(标准目录可能缺失),`universe` 是非 `Option`(特性依赖链 `system-packages → universe-packages` 在编译期保证它可用)。
- `obtain` 的三级链:**data → cache → 网络**;网络下载**仅对 `preview` 命名空间**触发,且**必须有 cache**才能落盘;下载失败会被精细化映射为 `NotFound` / `VersionNotFound` / `NetworkFailed`。
- 并发安全分两层:`FsPackages::store` 的「临时目录 + 原子 rename」保证**内容不损坏**;`FileStore` 持锁串行化加载保证**不重复下载**。`obtain` 本身不加锁(`&self`)。
- `new(downloader)` 是「系统标准目录 + 官方注册表」的便捷封装;`from_parts(data, cache, universe)` 是完全自定义的通用出口。typst-cli 用 `from_parts` 实现「`--package-path` 用户参数优先,否则回退标准目录」。
- `obtain` 返回的 `FsRoot` 与项目根是**同一个类型**,被 `SystemFiles::root` 复用——这就是「包文件与项目文件走同一段读取代码」的落点。

## 7. 下一步学习建议

本讲把 `SystemPackages` 讲透了,但故意把它的两个零件(`FsPackages`、`UniversePackages`)当作黑盒。建议接着阅读:

1. **u4-2 FsPackages 目录约定与原子存储**:深入本讲多次提到的「临时目录 + rename」并发安全机制,看清 `store`、`Tempdir` 的 `Drop` 清理、以及 `latest_version` 如何扫描本地目录取最大版本号。
2. **u4-3 UniversePackages 与包索引**:看清本讲「下载并解压」那一步(`universe.package`)的完整实现——URL 拼接、`.tar.gz` 解包、404 → `VersionNotFound` 的映射,以及包索引的惰性反序列化策略。
3. **u5-1 Downloader trait 与 SystemDownloader**:`SystemPackages::new` / `from_parts` 接受的 `impl Downloader` 到底是什么,真实下载器如何处理代理与证书。

阅读时,可以始终带着本讲的主线:**这一切复杂设计,都是为了让 `obtain(spec)` 这个看似简单的查询,既能「本地优先、按需联网」,又能「并发安全、错误友好」**。
