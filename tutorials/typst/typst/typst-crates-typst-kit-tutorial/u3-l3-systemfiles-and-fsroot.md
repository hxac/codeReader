# SystemFiles 与 FsRoot 路径解析

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Typst 的「双根模型」：一个文件要么属于**项目根（Project）**，要么属于某个**包根（Package）**，二者都用 `VirtualRoot` 枚举表示。
- 解释 `FsRoot` 这个「目录包装类型」如何把一条虚拟路径（`VirtualPath`）安全地映射到磁盘上的真实文件，并理解它**两层防线**如何阻止路径越界（path escape）。
- 读懂 `SystemFiles`：它把「项目文件从 `FsRoot` 加载、包文件从 `SystemPackages` 加载」统一成同一个 `FileLoader`，关键在于 `root(id)` 的分发——而**包一旦解析成功，也会变成一个 `FsRoot`**。
- 理解项目根与每个包各自形成独立根的**隔离机制**，并看懂 typst-cli 为何在 typst-kit 之外又保留了一份自己的 `SystemFiles`。

本讲承接 u3-l1（`FileStore` 与 `FileLoader` 抽象），打开 u3-l1 留作黑盒的「`SystemFiles` 与 `FsRoot` 如何把 `FileId` 解析到磁盘」。

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的结论，这里只做最简回顾：

- **`FileLoader` 是字节来源**：它只有一个方法 `load(id) -> FileResult<Bytes>`，是 `FileStore` 唯一可插拔的接口。本讲的 `SystemFiles` 正是 `FileLoader` 的一个内置实现。
- **`FileStore<L>` 是缓存层**：它按 `FileId` 懒加载、缓存，对外提供 `source(id)`/`file(id)`。typst-cli 的 `SystemWorld` 用 `FileStore<SystemFiles>` 来实现 `World::source`/`World::file`。
- **`FileId` 与磁盘路径解耦**：`FileId` 是一个被全局 intern 的句柄，它 `Deref` 到 `RootedPath`，而 `RootedPath = VirtualRoot + VirtualPath`。也就是说，一个文件由「它在哪个根里」+「根内的相对路径」唯一确定，**与机器上的绝对路径无关**——这正是 Typst 构建可复现的基础。

两个本讲才深入、之前被刻意搁置的概念：

- **`VirtualRoot`**：文件的「宿主根」只有两类——项目根 `Project`，或某个包根 `Package(PackageSpec)`。
- **`FsRoot`**：一个用真实目录路径 `PathBuf` 包装出来的类型，表示「这个虚拟根在磁盘上对应哪个文件夹」。项目根是一个 `FsRoot`，每个解析到的包也是一个 `FsRoot`。

> 提示：本讲的 `FsRoot` **不受任何 feature 开关限制**，始终可用；而 `SystemFiles` 需要 `system-files` 特性。这与 u1-l2 讲过的「条目级 `#[cfg(feature)]` 门禁」一致。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/files.rs` | 本讲核心。定义 `FsRoot`（目录包装 + 安全解析/加载）与 `SystemFiles`（按 `VirtualRoot` 分发的 `FileLoader`）。 |
| `../typst-syntax/src/path.rs` | `VirtualRoot`、`VirtualPath`、`realize` 的定义地。理解「路径越界被谁拒绝」必须回到这里。 |
| `../typst-cli/src/world.rs` | typst-cli **自己的** `SystemFiles`（私有），在 kit 版本基础上多了 `main` 字段与 stdin/empty 处理，用于对比。 |
| `src/packages.rs` | 仅引用一处：`SystemPackages::obtain` 的返回类型——它揭示了「包也会变成 `FsRoot`」这一关键事实。 |

永久链接基址：`https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/`

## 4. 核心概念与源码讲解

### 4.1 VirtualRoot：项目根与包根的双根模型

#### 4.1.1 概念说明

一份 Typst 文档在编译时，要读取的文件可能来自两个完全不同的地方：

1. **你自己的项目文件**：比如 `main.typ`、`chapter1.typ`、`logo.png`，它们都在你的项目目录里。
2. **依赖包里的文件**：比如 `#import "@preview/cetz:0.3.0": canvas` 引入的包，它的源码在另一个目录里。

Typst 用 `VirtualRoot` 枚举把这两种「宿主」明确区分开：

- `Project`：项目的规范根（即 `TYPST_ROOT` 指定的目录）。
- `Package(PackageSpec)`：某个具体包的根，带上包的全名 `命名空间/名字:版本`。

为什么叫「虚拟（Virtual）」？因为 Typst 用 `FileId` 标识文件，而 `FileId` 只关心「在哪个根 + 根内的相对路径」，**不关心磁盘绝对路径**。同一份 `main.typ`，无论放在 `/home/alice/doc/` 还是 `C:\Users\bob\doc\`，只要相对项目根的位置一样，就得到同一个 `FileId`。这让缓存、增量编译、跨机器可复现都成为可能。

#### 4.1.2 核心流程

一个 `FileId` 的解构路径如下：

```text
FileId  ──(Deref)──>  RootedPath { root: VirtualRoot, vpath: VirtualPath }
                          │                      │
                 id.root()│                      │id.vpath()
                          ▼                      ▼
                   VirtualRoot            VirtualPath
              (Project | Package(spec))   (根内的规范化相对路径)
```

后续所有「按根分发」的逻辑，第一步都是 `match id.root() { Project => ..., Package(spec) => ... }`。

#### 4.1.3 源码精读

`VirtualRoot` 的定义极简，只有两个变体（[../typst-syntax/src/path.rs:74-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L74-L81)）：

```rust
pub enum VirtualRoot {
    /// The canonical root of the Typst project.
    /// This is what `TYPST_ROOT` defines.
    Project,
    /// A root in a package.
    Package(PackageSpec),
}
```

`RootedPath` 把这两部分打包，并提供 `root()` 与 `vpath()` 两个访问器（[../typst-syntax/src/path.rs:37-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L37-L54)）：

```rust
pub fn root(&self) -> &VirtualRoot { &self.root }
pub fn vpath(&self) -> &VirtualPath { &self.vpath }
```

而 `FileId` 通过 `Deref` 透明地暴露 `RootedPath` 的方法（[../typst-syntax/src/path.rs:175-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L175-L181)），所以你才能直接写 `id.root()`、`id.vpath()`——它们其实是 `RootedPath` 上的方法。

#### 4.1.4 代码实践

**目标**：亲手确认 `FileId` 解构出的 `root()` 与 `vpath()`，体会「根 + 根内路径」的二元表示。

**操作步骤**（源码阅读型）：

1. 打开 `src/files.rs` 的测试模块，找到构造 `FileId` 的辅助函数 `id(path)`（文件末尾的 `#[cfg(test)] mod tests`）。
2. 阅读它如何用 `RootedPath::new(VirtualRoot::Project, VirtualPath::new(path).unwrap()).intern()` 造出一个项目根的 `FileId`。
3. 对照上面的解构图，写出 `id("a.typ")` 的 `root()` 值与 `vpath()` 字符串。

**需要观察的现象 / 预期结果**：`root()` 是 `VirtualRoot::Project`；`vpath().get_without_slash()` 是 `"a.typ"`（注意 `VirtualPath` 总会被规范化成以 `/` 开头的绝对形式，`get_without_slash()` 去掉那个前导斜杠）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Typst 要把文件表示成 `(VirtualRoot, VirtualPath)` 而不是直接用磁盘绝对路径？

> **参考答案**：绝对路径因机器而异，会让缓存失效、让构建不可复现；用解耦的 `FileId` 后，同一份内容在任何机器上都是同一个 id，利于增量编译与可复现构建。

**练习 2**：一个 `FileId` 的 `root()` 返回 `Package(@preview/cetz:0.3.0)`，它的 `vpath()` 是 `/canvas.typ`。这描述了什么？

> **参考答案**：它指向 cetz 包 0.3.0 版本根目录下的 `canvas.typ` 文件；至于这个根在磁盘上的具体位置，由包加载子系统（`SystemPackages`）决定，与 `FileId` 本身无关。

---

### 4.2 FsRoot：虚拟路径到磁盘的安全映射

#### 4.2.1 概念说明

`VirtualRoot` 只说了「在哪个根」，没说「这个根在磁盘哪里」。`FsRoot` 就是补上这一环的类型——它是一个对 `PathBuf` 的新类型（newtype），表示「一个真实目录，充当某个虚拟根的后端」：

```rust
pub struct FsRoot(PathBuf);
```

它只做两件事：

- `resolve(&VirtualPath) -> FileResult<PathBuf>`：把根内相对路径拼成磁盘绝对路径。
- `load(&VirtualPath) -> FileResult<Bytes>`：解析后把文件读进内存。

最关键的设计意图是**安全**：一份 `.typ` 文件里可能写 `#include "../../etc/passwd"` 之类的恶意/越界路径，`FsRoot` 必须保证任何 `VirtualPath` 都不会「爬出」它所在的根目录。这份防御分**两层**，分别由 `typst-syntax` 与 `FsRoot` 承担。

#### 4.2.2 核心流程：两层防线

**第一层（构造期，在 `typst-syntax`）**：`VirtualPath::new` / `join` 在创建时就会**归一化**路径。归一化过程中，遇到 `..` 会弹出上一级；如果 `..` 已经在根（`/`）上无处可弹，就直接返回 `PathError::Escapes` 错误。也就是说——**你根本无法构造出一个能用 `..` 爬出根的 `VirtualPath`**。等到 `FsRoot::resolve` 拿到它时，它在「词法层面」已经是被收拢在根内的。

**第二层（实现期，在 `typst-syntax::realize`，由 `FsRoot::resolve` 调用）**：即便词法上没有 `..` 逃逸，某些**平台特有**的路径段仍可能在拼接到磁盘路径后产生意外。`VirtualPath::realize` 会对每个段做 `Segment::realize()` 校验，拒绝：

- Windows 上的盘符前缀（如 `C:`，会被 Windows 当成重置路径）；
- Windows 保留设备名（`CON`、`NUL`、`LPT1` 等）；
- 一个段解析成多个路径分量或非 `Normal` 分量的情况。

这一层返回 `RealizeError::Invalid`，被 `FsRoot::resolve` 用 `map_err(Into::into)` 转成 `FileError`。

> ⚠️ 源码注释明确指出：这**只是词法检查，不做文件系统操作**，因此**符号链接（symlink）仍可能逃逸**。这是已知边界，不是 bug。

`load` 在 `resolve` 成功后再加两步：用 `fs::metadata` 判断目标是不是目录（是则返回 `FileError::IsDirectory`），否则用 `fs::read` 读字节。

```text
FsRoot::load(vpath)
   │
   ├─ self.resolve(vpath) ──> VirtualPath::realize(root)
   │        （第一层已由 VirtualPath::new 提前保证；第二层在此校验平台特有段）
   │
   ├─ fs::metadata(path).is_dir()?  ── 是目录则 Err(FileError::IsDirectory)
   └─ fs::read(path)                ── 否则读字节 -> Ok(Bytes)
```

#### 4.2.3 源码精读

`FsRoot` 是个普通 newtype，派生了 `Clone/Eq/PartialEq/Hash` 等——这意味着它可以被当作「根的标识」比较与哈希（[src/files.rs:324-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L324-L329)）：

```rust
#[derive(Debug, Clone, Eq, PartialEq, Hash)]
pub struct FsRoot(PathBuf);
```

`resolve` 一行实现，把活全交给 `VirtualPath::realize`（[src/files.rs:342-346](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L342-L346)）：

```rust
pub fn resolve(&self, path: &VirtualPath) -> FileResult<PathBuf> {
    path.realize(&self.0).map_err(Into::into)
}
```

`load` 的注释点明了「越界会被拒，但符号链接除外」这一边界，随后是目录判断与读取（[src/files.rs:348-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L348-L359)）：

```rust
pub fn load(&self, path: &VirtualPath) -> FileResult<Bytes> {
    // Join the path to the root. If it tries to escape, deny access. Note:
    // It can still escape via symlinks.
    let path = self.resolve(path)?;
    let f = |e| FileError::from_io(e, &path);
    if fs::metadata(&path).map_err(f)?.is_dir() {
        Err(FileError::IsDirectory)
    } else {
        fs::read(&path).map(Bytes::new).map_err(f)
    }
}
```

回到第一层防线——`realize` 本身只是「把每个段依次 push 到根路径」（[../typst-syntax/src/path.rs:243-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L243-L249)），真正的平台校验在每个段的 `Segment::realize()` 里：

```rust
pub fn realize(&self, root: &Path) -> Result<PathBuf, RealizeError> {
    let mut out = root.to_path_buf();
    for s in self.0.iter() {
        out.push(s.realize()?);   // 每段单独校验，任一段非法即 Err
    }
    Ok(out)
}
```

而 `..` 逃逸其实早在 `VirtualPath` 构造时就被拦截了——`Segments::push_component` 处理 `Parent` 时，若已无段可弹就报 `PathError::Escapes`（[../typst-syntax/src/path.rs:567-584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L567-L584)）：

```rust
Component::Parent => {
    if !self.pop() {
        return Err(PathError::Escapes);   // 在根上再弹 -> 越界
    }
}
```

#### 4.2.4 代码实践

**目标**：用一个临时目录构造 `FsRoot`，验证合法路径能解析/读取，并亲眼看到越界路径「在构造阶段就被拒」、读取目录会得到 `IsDirectory`。

> 说明：本实践**不需要** `system-files` 特性，因为 `FsRoot` 不受 feature 门禁。只需依赖 `typst-kit`（提供 `FsRoot`）与 `typst-syntax`（提供 `VirtualPath`）。

**操作步骤**：新建一个临时 Cargo 项目，加入上述依赖，把下面这段**示例代码**写进 `src/main.rs` 并运行。

```rust
// 示例代码：FsRoot 解析与加载的最小验证
use std::fs;
use typst_kit::files::FsRoot;
use typst_syntax::VirtualPath;

fn main() {
    // 1) 准备一个临时目录当作项目根，写入一个 .typ 文件
    let dir = std::env::temp_dir().join("typst-kit-fsroot-demo");
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("hello.typ"), "Hello from FsRoot").unwrap();

    let root = FsRoot::new(dir.clone());

    // 2) 合法路径：能解析、能读取
    let vp = VirtualPath::new("hello.typ").unwrap();      // 归一化为 /hello.typ
    let real = root.resolve(&vp).unwrap();                // -> dir/hello.typ
    let bytes = root.load(&vp).unwrap();                  // -> 文件字节
    println!("real  = {}", real.display());
    println!("bytes = {}", std::str::from_utf8(&bytes).unwrap());

    // 3) 读取「根目录本身」-> IsDirectory（VirtualPath::new("") 归一化为 /）
    let root_vp = VirtualPath::new("").unwrap();
    match root.load(&root_vp) {
        Err(e) => println!("load(/) 被拒，符合预期：{e:?}"),
        Ok(_)  => println!("（意外）竟然读到了根目录"),
    }

    // 4) 越界路径：在「构造」阶段就被拒绝，根本到不了 FsRoot::resolve
    println!("new(\"..\")                      => {:?}", VirtualPath::new(".."));                   // Err(Escapes)
    println!("new(\"a/../../etc/passwd\")       => {:?}", VirtualPath::new("a/../../etc/passwd"));  // Err(Escapes)
}
```

**需要观察的现象 / 预期结果**：

1. `real` 打印为 `<临时目录>/hello.typ`；`bytes` 打印为 `Hello from FsRoot`。
2. `load(/)` 报错，错误对应 `FileError::IsDirectory`（因为它指向目录）。
3. 两处 `VirtualPath::new(..)` 都打印 `Err(...)`，错误是 `PathError::Escapes`——**注意：越界是被 `VirtualPath::new` 拒绝的，不是被 `FsRoot::resolve` 拒绝的**。这是本讲最需要建立的准确认知。
4. `VirtualPath::new("a/..")` 会**成功**（归一化为 `/`），因为它没有越界、只是回到根——这也回到第 3 点的 `IsDirectory`。

> 待本地验证：第二层防线（盘符、`CON` 等保留名）是平台相关的，上述 `RealizeError` 行为需在 Windows 上验证；在 Unix 上这类非法段较难自然构造。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VirtualPath::new("..")` 直接返回 `Err`，而不是「等到 `FsRoot::resolve` 才报错」？

> **参考答案**：`VirtualPath` 在构造时就做归一化，`..` 在根上无处可弹即判为 `PathError::Escapes`。这样所有持有 `VirtualPath` 的代码都可以信任「它已被收拢在根内」，`FsRoot` 无需再重复处理 `..`，职责更清晰。

**练习 2**：源码注释说「It can still escape via symlinks」。请解释这句话，并说明 `FsRoot` 为什么不在这一层堵住它。

> **参考答案**：词法检查只看路径字符串，不读文件系统；如果根内某个子目录是指向根外的符号链接，`..`/合法路径顺它就能读到根外文件。要堵住它需要 `fs::canonicalize` 并比对真实路径前缀，代价较高且会引入真实 I/O，与「虚拟、可复现、无 I/O」的设计取舍冲突，因此留作已知边界。

---

### 4.3 SystemFiles：按根分发的 FileLoader

#### 4.3.1 概念说明

`FileLoader` trait 的文档其实已经把 `SystemFiles` 的职责说透了（[src/files.rs:256-266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L256-L266)）：实现 `load(id)` 时，先看 `id.root()` 判断是项目还是包，再到对应的根里读取 `id.vpath()`。`SystemFiles` 就是这段描述的标准实现。

它的两个字段正好对应两类来源：

```rust
pub struct SystemFiles {
    project: FsRoot,          // 项目文件从这里加载
    packages: SystemPackages, // 包文件从这里加载
}
```

这里有一个贯穿本讲的**关键事实**：`packages.obtain(spec)` 的返回类型也是 `FsRoot`！也就是说，**项目根是一个 `FsRoot`，每个解析成功的包也是一个 `FsRoot`**。于是「按根分发」之后，两条分支都汇聚到同一套 `FsRoot::resolve/load`，逻辑完全统一。

#### 4.3.2 核心流程

```text
                 FileStore<SystemFiles>
                         │ loader.load(id)
                         ▼
                 SystemFiles::load(id)
                         │
                         │ root(id): match id.root()
                         │   ┌───────────────────────────────┐
            Project ─────┤   │ self.project.clone()          │ ── FsRoot ─┐
                         │   │                               │            │
          Package(spec)──┤   │ self.packages.obtain(spec)?   │ ── FsRoot ─┤
                         │   └───────────────────────────────┘            │
                         │                                                ▼
                         └────────────────────────────────────►  FsRoot::load(id.vpath())
                                                                          │
                                                                          ▼
                                                                       Bytes
```

要点：

- `root(id)` 是分发的核心：`Project` 直接克隆项目 `FsRoot`；`Package(spec)` 调用 `packages.obtain(spec)?`，未命中会沿 data→cache→Universe 网络链查找（详见单元 4），最终拿到包目录的 `FsRoot`，或返回 `NotFound`。
- 拿到 `FsRoot` 后，`resolve(id)`/`load(id)` 都只是「`root(id)?.方法(id.vpath())`」，项目与包走的是同一段代码。

#### 4.3.3 源码精读

`SystemFiles` 整体受 `#[cfg(feature = "system-files")]` 门禁（[src/files.rs:280-293](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L280-L293)）。构造器就是简单赋值（[src/files.rs:296-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L296-L301)）。

分发的核心 `root(id)`——注意两条分支都产出 `FsRoot`（[src/files.rs:308-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L308-L314)）：

```rust
pub fn root(&self, id: FileId) -> FileResult<FsRoot> {
    Ok(match id.root() {
        VirtualRoot::Project => self.project.clone(),
        VirtualRoot::Package(spec) => self.packages.obtain(spec)?,
    })
}
```

`resolve` 与 `FileLoader::load` 都建立在 `root(id)` 之上，一短再短（[src/files.rs:303-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L303-L306)、[src/files.rs:317-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L317-L322)）：

```rust
pub fn resolve(&self, id: FileId) -> FileResult<PathBuf> {
    self.root(id)?.resolve(id.vpath())
}

impl FileLoader for SystemFiles {
    fn load(&self, id: FileId) -> FileResult<Bytes> {
        self.root(id)?.load(id.vpath())
    }
}
```

而「包也变成 `FsRoot`」这一事实，藏在 `SystemPackages::obtain` 的签名里（[src/packages.rs:95-100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L100)）——它先查 data 目录、再查 cache 目录，命中即返回那个目录的 `FsRoot`：

```rust
pub fn obtain(&self, spec: &PackageSpec) -> PackageResult<FsRoot> {
    if let Some(packages) = &self.data
        && let Some(root) = packages.obtain(spec)
    {
        return Ok(root);   // ← 注意：返回的就是 FsRoot
    }
    // …cache / 网络下载链路同理，最终也是返回 FsRoot 或 NotFound
}
```

> 这正是 `SystemFiles` 能用「统一 `FsRoot` 接口」同时服务项目与包的根本原因：包加载子系统负责把一个 `PackageSpec` 变成磁盘上的一个目录（`FsRoot`），之后文件读取的代码就与项目文件**毫无区别**。

#### 4.3.4 代码实践

**目标**：用源码追踪一次完整的「`FileStore` → `SystemFiles` → `FsRoot`」调用链，看清项目分支与包分支如何汇流。

**操作步骤**（源码阅读型）：

1. 从 `src/files.rs` 的 `FileStore::file`（u3-l1 讲过）出发，它调用 `slot(id, |slot| slot.file(&self.loader, id))`，最终调 `loader.load(id)`。
2. 跟到 `impl FileLoader for SystemFiles` 的 `load`，看它先 `self.root(id)?` 再 `.load(id.vpath())`。
3. 进 `root(id)`，分别设想 `id.root()` 为 `Project` 与 `Package(spec)` 时的两条路径。
4. 两条路径最终都落到 `FsRoot::load`（4.2 已读）。

**需要观察的现象 / 预期结果**：在笔记上画出 4.3.2 的流程图，并用一句话标注「项目与包的差异只在 `root(id)` 这一步，之后完全相同」。

> 待本地验证：要真正跑通「包分支」，需要构造一个 `SystemPackages`（带 data/cache 目录与 downloader），属于单元 4 的内容；本讲只要求读懂调用链。

#### 4.3.5 小练习与答案

**练习 1**：`SystemFiles::resolve` 与 `FileLoader::load` 的实现几乎一模一样（都是 `self.root(id)?.<方法>(id.vpath())`）。这种重复是坏味道吗？

> **参考答案**：不算坏味道，反而体现了设计的统一性——一旦 `root(id)` 把任意 `FileId` 归约成一个 `FsRoot`，「解析路径」和「读字节」就只是对该 `FsRoot` 的两种操作，自然同构。真要消除重复可以抽一个内部 helper，但当前两行实现已足够清晰。

**练习 2**：为什么 `root(id)` 返回的是 `FileResult<FsRoot>`（带 `?`），而不是直接返回 `FsRoot`？

> **参考答案**：因为 `Package(spec)` 分支可能失败——包既不在 data 也不在 cache、且网络下载也失败时，`obtain` 会返回 `NotFound`。`?` 把这个错误向上冒泡成 `FileError`，最终体现在 `World::file/source` 的 `FileResult` 里。

---

### 4.4 项目根与包根的隔离机制（含 CLI 对照）

#### 4.4.1 概念说明

「隔离」是双根模型最重要的安全性质：**项目文件只能看见项目根内的东西，某个包的文件只能看见自己那个包目录内的东西，彼此互不可达。** 这之所以成立，是因为：

1. 每个 `FileId` 的 `vpath` 都被 `VirtualPath` 归一化、收拢在**它自己那个根**内（4.2 的第一层防线）。
2. 项目根与每个包都是**各自独立的 `FsRoot`**，`root(id)` 根据 `id.root()` 严格分发——一个项目根的 `FileId` 永远只会拿到项目 `FsRoot`，绝不会拿到某个包的 `FsRoot`。
3. 包的 `FsRoot` 指向 `命名空间/名字/版本` 这一独立子目录，包与包之间也是各自独立的根。

换句话说，`..` 越界被拦在「单根之内」，跨根则根本无从发生——因为跨根需要换 `VirtualRoot`，而那是 `FileId` 创建时就被固定下来的。

**与 typst-cli 的对照**：typst-cli 并没有直接复用 typst-kit 的 `SystemFiles`，而是在 `world.rs` 里**保留了一份自己的私有 `SystemFiles`**。二者结构几乎相同（同样的 `project: FsRoot` + `packages: SystemPackages`、同样的 `resolve`/`root`），但 CLI 版本多两样东西：

- 一个 `main: FileId` 字段，用来回答 `World::main`（「主入口文件是谁」是集成方的职责，不属于「文件加载」这个关注点）；
- `FileLoader::load` 里对 `<stdin>`、`<empty>` 两个特殊 `FileId` 的特判。

这恰好示范了 typst-kit 的定位（u1-l1 讲过）：**kit 提供干净、可复用的核心积木（项目根 + 包加载），集成方（CLI）再叠加自己特有的部分**。kit 版本的 `load` 因此只有纯粹的一行 `self.root(id)?.load(id.vpath())`，没有 stdin 这类 CLI 专属逻辑。

#### 4.4.2 核心流程

typst-cli 把 `SystemFiles` 装进 `FileStore`，再由 `SystemWorld` 转发：

```text
SystemWorld { files: FileStore<SystemFiles> }
      │
      ├─ World::source(id) -> files.source(id)  ─┐
      ├─ World::file(id)   -> files.file(id)    ─┤  都经 FileStore 缓存
      │                                           │   再调 SystemFiles::load
      ├─ World::main()     -> files.loader().main │
      │                                           │
      └─ dependencies()    -> files.dependencies()│
                             再用 loader.resolve(id) 把 FileId 还原成磁盘 PathBuf
```

#### 4.4.3 源码精读

typst-cli 的 `SystemFiles` 是私有结构体，比 kit 版多了 `main` 字段（[../typst-cli/src/world.rs:185-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L185-L191)）：

```rust
struct SystemFiles {
    main: FileId,
    project: FsRoot,
    packages: SystemPackages,
}
```

它的 `resolve` 与 `root` 与 kit 版逐字相同（[../typst-cli/src/world.rs:247-257](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L247-L257)）；区别只在 `FileLoader::load` 多了对 stdin/empty 的特判（[../typst-cli/src/world.rs:260-270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L260-L270)）：

```rust
impl FileLoader for SystemFiles {
    fn load(&self, id: FileId) -> FileResult<Bytes> {
        if id == *EMPTY_ID {
            Ok(Bytes::new([]))
        } else if id == *STDIN_ID {
            read_from_stdin().map(Bytes::new)
        } else {
            self.root(id)?.load(id.vpath())   // ← 与 kit 版完全一致
        }
    }
}
```

`SystemWorld` 的字段与转发印证了这套装配（[../typst-cli/src/world.rs:33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L33)）：`files: FileStore<SystemFiles>`；`World::source/file` 直接转发给 `FileStore`（[../typst-cli/src/world.rs:130-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L130-L136)）；`root()` 通过 `self.files.loader().project.path()` 暴露项目根（[../typst-cli/src/world.rs:87-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L87-L90)）；`dependencies()` 则用 `loader.resolve(id)` 把每个依赖 `FileId` 还原成磁盘路径（[../typst-cli/src/world.rs:98-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L98-L101)）：

```rust
pub fn dependencies(&mut self) -> impl Iterator<Item = PathBuf> + '_ {
    let (loader, deps) = self.files.dependencies();
    deps.filter_map(|id| loader.resolve(id).ok())
}
```

#### 4.4.4 代码实践

**目标**：通过对照 kit 版与 CLI 版的 `SystemFiles`，深刻体会「积木核心 vs 集成特化」的边界。

**操作步骤**（源码阅读 + 填表）：

1. 并排打开 `src/files.rs`（kit 版 `SystemFiles`）与 `../typst-cli/src/world.rs`（CLI 版 `SystemFiles`）。
2. 完成下表（在笔记里填写）：

| 关注点 | kit 版 `SystemFiles` | CLI 版 `SystemFiles` |
|--------|----------------------|----------------------|
| 字段 | `project`, `packages` | ？ |
| `main` 入口文件 | 无 | ？ |
| `load` 对 stdin/empty 的处理 | 无 | ？ |
| `resolve` / `root` 实现 | ？ | ？ |
| 可见性 | `pub`（受 `system-files` 门禁） | 私有 `struct` |

**需要观察的现象 / 预期结果**：你会发现除「`main` 字段 + stdin/empty 特判 + 可见性」外，两者**逐字相同**。这正好说明：kit 提供的是「纯文件加载」积木，CLI 在其上叠加「主入口 + 标准输入」这些 CLI 专属关注点。

#### 4.4.5 小练习与答案

**练习 1**：既然两者几乎一样，typst-cli 为什么不直接 `use typst_kit::files::SystemFiles`，而要自己再写一份？

> **参考答案**：CLI 需要 (a) 一个 `main: FileId` 来回答 `World::main`，(b) 在 `load` 里特判 stdin/empty。这两件事都不属于「通用文件加载」的关注点。若硬塞进 kit 的 `SystemFiles`，会让通用积木带上 CLI 专属逻辑，违背 u1-l1 讲过的「single source of truth 只管公共逻辑」的定位，所以 CLI 选择保留自己的薄封装。

**练习 2**：假设包 A 里的某文件写了 `#include "../B/lib.typ"` 想偷读包 B。这条路径会被如何处理？

> **参考答案**：`../B/lib.typ` 会先被 `VirtualPath::new/join` 归一化：从包 A 的根目录出发，`..` 试图弹出根，无段可弹 → 直接 `PathError::Escapes`，连构造都失败，更不会触达磁盘。跨包访问在 `FileId` 层就被阻断了。

---

## 5. 综合实践

**任务**：把本讲三条主线——「双根模型 / `FsRoot` 安全映射 / `SystemFiles` 按根分发」——串成一张完整的「`FileId` → 字节」调用链图，并用代码验证项目分支。

**步骤**：

1. **画图**：在笔记里画出下面这条链，并标注每一跳发生在哪个文件、哪一行：
   - `World::file(id)`（CLI 转发）
   - → `FileStore::file(id)` → `FileSlot::file(loader, id)`（u3-l1/u3-l2）
   - → `SystemFiles::load(id)`（本讲 4.3）
   - → `SystemFiles::root(id)` 分发：`Project` 给项目 `FsRoot`、`Package(spec)` 给 `packages.obtain(spec)` 的 `FsRoot`
   - → `FsRoot::load(id.vpath())`（本讲 4.2：`resolve` → `realize` 两层防线 → 目录判断 → `fs::read`）
   - → `Bytes` 原路返回并被 `FileSlot` 缓存。
2. **代码验证（项目分支）**：在 4.2.4 的示例基础上，把 `FsRoot` 包进一个手写的 `FileLoader`（参考 `src/files.rs` 测试里的 `TestLoader`，但让它内部持有一个 `FsRoot`，`load` 时 `self.root.load(id.vpath())`），再用 `FileStore::new(...)` 包装，调用 `store.file(id)` 验证能读到 `hello.typ` 的内容。
3. **思考题**：在这条链里，「路径越界」最早在哪一层被拦截？为什么后续的 `FsRoot::resolve` 几乎不会再因为 `..` 报错？

**预期结果**：你得到一张清晰的调用链图，确认「项目与包的差异只在 `root(id)` 一步」，并用代码证明了项目侧端到端可读。包侧（`SystemPackages`）的端到端验证留待单元 4。

> 待本地验证：第 2 步若要让 `FileStore` 用你手写的 loader，需要同时引入 `typst-syntax`（构造 `FileId`）与 `typst-library`（`FileError`/`Bytes` 类型），编译期依赖请按需开启。

## 6. 本讲小结

- Typst 用 `VirtualRoot`（`Project` / `Package(spec)`）+ `VirtualPath` 的二元组定位文件，与磁盘绝对路径解耦，是可复现构建的基础。
- `FsRoot(PathBuf)` 把一个虚拟根落到真实目录，只提供 `resolve` 与 `load`；它依赖 `typst-syntax` 的**两层防线**防路径越界——`VirtualPath::new` 在构造期就用 `PathError::Escapes` 拦掉 `..` 逃逸，`realize` 再校验盘符/保留名等平台特有段（符号链接逃逸是已知边界）。
- `SystemFiles` 是「按根分发」的 `FileLoader`：`root(id)` 按 `id.root()` 把任意 `FileId` 归约成一个 `FsRoot`，之后 `resolve/load` 对项目与包完全统一。
- **关键洞察**：`SystemPackages::obtain` 也返回 `FsRoot`——包加载子系统负责把 `PackageSpec` 变成一个磁盘目录，于是项目文件和包文件走的是同一段读取代码。
- 隔离性来自「`FileId` 创建时根已固定 + 每个 `VirtualPath` 被收拢在自己根内」：项目/包、包与包之间互不可达，`..` 跨根在构造期即被拒。
- typst-cli 保留了一份自己的私有 `SystemFiles`，在 kit 版基础上加 `main` 字段与 stdin/empty 特判——这是「kit 提供公共积木、集成方叠加专属逻辑」设计定位的活样本。

## 7. 下一步学习建议

- 想打通「包分支」的端到端链路，进入**单元 4**：从 u4-l1 `SystemPackages` 与三级加载链读起，看清 `obtain(spec)` 如何把一个 `PackageSpec` 变成 `FsRoot`。
- 想理解 `FileStore` 拿到字节后如何变成 `Source` 并支持增量编译，回顾 u3-l2（`FileSlot` 三态状态机与缓冲区复用）。
- 想看「`resolve` 的另一用途」——把依赖 `FileId` 还原成磁盘路径用于 `typst watch` 的文件监视——结合 u7-l2 `Watcher` 阅读 `SystemWorld::dependencies` 的消费方。
