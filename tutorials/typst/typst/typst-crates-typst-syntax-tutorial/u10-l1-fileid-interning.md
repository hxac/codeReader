# FileId 与路径驻留

## 1. 本讲目标

本讲解决一个贯穿整个 Typst 编译器的工程问题：**编译器内部到底用什么来「指代一个文件」？**

读完后你应当能够：

1. 理解 Typst 的**虚拟文件系统模型**——为什么不用操作系统的真实路径，而是用 `VirtualRoot` + `VirtualPath` 组成的 `RootedPath`。
2. 掌握 `FileId` 如何把一个较重的 `RootedPath` **全局驻留（intern）成一个 16 位整数**，以及它所采用的「故意 leak」策略与 \(2^{16}\) 上限。
3. 理解 `FileId` 为什么必须是 `NonZeroU16`，以及它如何被打包进 8 字节的 `Span` 里，让一个 span 既能定位「哪个文件」又能定位「文件里的哪个节点」。

本讲是 U10 单元（文件身份、包清单与高亮）的第一篇，承接 U6 的 Span 系统。建议先读过 u6-l1《Span 紧凑编码》。

## 2. 前置知识

在进入源码前，先用三个小问题建立直觉。

**问题一：为什么不直接用字符串路径？**
操作系统的真实路径有两个麻烦：一是平台相关（Windows 用 `\`，Unix 用 `/`）；二是「不规整」，同一个文件可以写成 `a/b.typ`、`a/./b.typ`、`a/c/../b.typ`，甚至符号链接会让 `..` 逃出项目根。编译器需要的是一个**确定、规整、跨平台**的文件标识。于是 Typst 在真实路径之上架了一层「虚拟文件系统」。

**问题二：为什么不直接用 `RootedPath` 当标识？**
`RootedPath` 里装着规整好的字符串路径，可能还附带一个 `PackageSpec`（命名空间、名字、版本号）。它是个较「重」的结构。而编译器里每个语法树节点都要带一个标识它属于哪个文件的标签（即 `Span`），这种标签会被**无数次地复制、比较、哈希**。如果标签是 `RootedPath`，每次都要拷字符串，太贵了。我们需要一个**轻量、可 Copy** 的身份证号。

**问题三：什么叫「驻留（intern）」？**
「驻留」是一种经典技巧：维护一张全局表，把「值相等」的对象映射成「同一个整数」。第一次见到某个值就分配新号码并存表；以后再见到相等的值，直接返回已有号码。这样「相等」就退化成「整数相等」，复制退化成「复制一个整数」。字符串驻留在很多语言解释器里都有（比如 Java 的字符串池）。Typst 把同样的技巧用在了文件路径上，产物就是 `FileId`。

> 一个贯穿全讲的关键认识：`FileId` 是**故意泄漏（leak）**出来的全局单例编号。它从不释放，但这正是它又快又简单的原因——我们会在 4.3 节讲清为什么这种 leak 是可接受的。

## 3. 本讲源码地图

本讲只涉及两个源文件：

| 文件 | 作用 |
| --- | --- |
| [src/path.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs) | 虚拟文件系统模型（`VirtualRoot`/`VirtualPath`/`RootedPath`）、全局驻留表 `INTERNER`、文件身份证 `FileId`。本讲主战场。 |
| [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | 8 字节 `Span` 的位布局。本讲只读其中「高 16 位存 FileId」那一小段，说明 `FileId` 与 `Span` 的耦合。 |

可见性提示：`path` 模块在 `lib.rs` 里是私有 `mod`，但其中的 `FileId`、`RootedPath`、`VirtualPath`、`VirtualRoot`、`PathError`、`VirtualizeError`、`RealizeError` 都通过 `pub use` 挂牌到了 crate 根（见 [lib.rs:L29-L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L29-L32)）。也就是说，**这些类型是公开 API**，你可以直接在外部代码里 `use typst_syntax::{FileId, RootedPath, VirtualPath, VirtualRoot};` 使用它们。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先建立虚拟文件系统模型（4.1），再把路径驻留成 `FileId`（4.2），再深入驻留表的 leak 策略与上限（4.3），最后看 `FileId` 如何嵌进 `Span`（4.4）。

### 4.1 虚拟文件系统模型：VirtualRoot、VirtualPath 与 RootedPath

#### 4.1.1 概念说明

Typst 的源文件来自两种地方：**用户自己的项目**，以及**依赖的包**。这两类文件的「根」不同——项目根由 `TYPST_ROOT` 定义，包根则是包的安装目录。为了在编译器内部用同一种方式指代这两类文件里的任意一个，Typst 设计了一个虚拟文件系统：

- 一个文件的身份 = **它在哪个根里** + **根内的哪条路径**。
- 「在哪个根里」用 `VirtualRoot` 表示，只有两种取值：`Project`（项目）或 `Package(PackageSpec)`（某个具体版本的包）。
- 「根内的哪条路径」用 `VirtualPath` 表示，它**永远是一条规范化过的、以 `/` 开头的绝对路径**，不含 `.`、`..`、反斜杠。

把这两者打包，就是 `RootedPath`。可以这样理解：

```
RootedPath = ( VirtualRoot, VirtualPath )
            = ( "在哪个根", "根内的规范化路径" )
```

注意 `VirtualPath` 是「虚拟」的——它只是一串用 `/` 分隔的片段，不对应任何具体操作系统路径。要把它变成真正能读盘的路径，得再提供一个真实的根目录（`realize`）；反过来，从真实路径推出虚拟路径则用 `virtualize`。这层抽象让编译器内核完全与操作系统无关。

#### 4.1.2 核心流程

`VirtualPath` 的核心流程是**规范化**：把用户给的任意字符串压成「以 `/` 开头、无 `.`/`..`/连续斜杠/反斜杠」的标准形式。

```
输入 "a/./file.txt"
  → 拆成组件 [a] [.] [file.txt]
  → 逐个 push：'a' 压入 → '.' 忽略 → 'file.txt' 压入
  → 得到 "/a/file.txt"

输入 "../x"
  → 遇到 '..' 时尝试 pop，但路径已空 → 越过根 → 返回 PathError::Escapes
```

`VirtualPath` 内部用一个私有结构 `Segments`（本质上是一个 `EcoString`，**保证永远以 `/` 开头**）存放规范化结果。`push_component` 是规范化的执行者：

- `Root`（开头 `/`）→ 清空重来；
- `Current`（`.` 或空段）→ 忽略；
- `Parent`（`..`）→ 弹出最后一段，若已空则报 `Escapes` 错误；
- `Normal` → 追加一段。

这套规则保证了虚拟路径**永远不可能越过自己的根**（仅从词法层面，符号链接另说）。`RootedPath` 本身不再做任何路径处理，它只是把 `VirtualRoot` 和 `VirtualPath` 装在一起。

#### 4.1.3 源码精读

`VirtualRoot` 是一个简单的二选一枚举（[src/path.rs:L72-L81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L72-L81)）：

```rust
pub enum VirtualRoot {
    /// 项目根（TYPST_ROOT）
    Project,
    /// 某个包的根
    Package(PackageSpec),
}
```

`PackageSpec`（定义在 `package.rs`）由 `namespace` / `name` / `version` 三部分组成（见 [src/package.rs:L226-L233](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L226-L233)），所以「包根」天然就精确到了某个版本。

`RootedPath` 只是把两者打包，并提供 `intern()` 转成 `FileId` 的入口（[src/path.rs:L19-L34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L19-L34)）：

```rust
pub struct RootedPath {
    root: VirtualRoot,
    vpath: VirtualPath,
}

impl RootedPath {
    pub fn new(root: VirtualRoot, vpath: VirtualPath) -> Self { ... }
    /// 把自己驻留成 FileId
    pub fn intern(self) -> FileId { FileId::new(self) }
    ...
}
```

`VirtualPath::new` 是规范化的入口，它调用 `Segments::normalize`（[src/path.rs:L193-L198](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L193-L198)）：

```rust
pub fn new(path: impl AsRef<str>) -> Result<Self, PathError> {
    let segments = Segments::normalize(components(path.as_ref()))?;
    Ok(Self(segments))
}
```

`push_component` 处理 `..` 越界报错（[src/path.rs:L567-L584](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L567-L584)），这是「路径不可能逃出根」的保证所在：

```rust
Component::Parent => {
    if !self.pop() {
        return Err(PathError::Escapes);  // 已在根上再 '..' → 越界
    }
}
```

真实路径 ↔ 虚拟路径的双向转换由两个互为「counterpart」的函数承担：`virtualize`（真实→虚拟，[src/path.rs:L208-L227](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L208-L227)）先用 `strip_prefix` 去掉真实根再规范化；`realize`（虚拟→真实，[src/path.rs:L243-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L243-L249)）则把虚拟路径一段段 `push` 到真实根上。

#### 4.1.4 代码实践

**实践目标**：直观感受 `VirtualPath` 的规范化与「越界即报错」行为。

**操作步骤**：阅读 `path.rs` 内联测试 `test_new`（[src/path.rs:L713-L740](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L713-L740)），它列举了多种输入与预期输出。挑选其中几条，在脑中（或写到一个小测试里）走一遍 `normalize` 流程。

**需要观察的现象**：注意这些对照——`"a/./file.txt"` → `/a/file.txt`（`.` 被消去）、`"hello/.././/wor/ld.typ.extra"` → `/wor/ld.typ.extra`（`..` 回退一级、多余斜杠被合并）、`".."` → `Err(Escapes)`（根上不能 `..`）、`"a\\world.txt"` → `Err(Backslash)`（禁止反斜杠）。

**预期结果**：每条 `Ok(...)` 的输出都以 `/` 开头、规整无冗余；每条 `Err(...)` 都对应一种被拒绝的危险写法。

**运行方式（待本地验证）**：在本仓库执行 `cargo test -p typst-syntax test_new` 可实际跑这个测试。

#### 4.1.5 小练习与答案

**练习 1**：`VirtualPath::new("a/.../world")`（注意是三个点 `...`，不是两个点）的结果是什么？

**答案**：得到 `/hello`... 不对——按 `test_new` 中的 `"hello/.../world"` → `/hello/.../world`，三个点 `...` 是一个**合法的普通段名**（它不是 `..`），所以被原样保留。规范化的特殊处理只针对精确的 `..`、`.`、空串。所以 `"a/.../world"` → `/a/.../world`（`Ok`）。

**练习 2**：为什么 `VirtualPath` 不允许反斜杠？

**答案**：因为反斜杠在 Windows 上是路径分隔符，在 Unix 上是普通字符，同一条虚拟路径在不同平台会被解析成不同结构，带来跨平台兼容隐患。所以一律禁止，强制只用 `/`。

---

### 4.2 FileId：路径的驻留身份证

#### 4.2.1 概念说明

有了 `RootedPath`，文件身份似乎已经解决。但正如前置知识里所说，`RootedPath` 太重，不适合塞进每个 span。于是 Typst 引入 `FileId`：

> `FileId` 是一个 `RootedPath` 的**全局驻留编号**——一个 `NonZeroU16`。

它的定义只有一行（[src/path.rs:L94-L98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L94-L98)）：

```rust
#[derive(Copy, Clone, Eq, PartialEq, Hash)]
pub struct FileId(NonZeroU16);
```

`Copy + Clone + Eq + PartialEq + Hash` 全都派生自底层整数——这意味着**复制、判等、哈希都只是一个 16 位整数操作**，几乎零成本。这与 `RootedPath`（要拷字符串）形成鲜明对比，正是驻留的意义。

#### 4.2.2 核心流程

驻留的入口是 `RootedPath::intern(self)`，它转交给 `FileId::new(self)`（[src/path.rs:L104-L128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L104-L128)）。核心流程是一个典型的「查表 → 命中返回 / 未命中分配」：

```
FileId::new(path):
  1. 拿写锁访问全局表 INTERNER
  2. 若 to_id 里已有该 path → 返回旧 id   (去重)
  3. 否则:
       a. num = 当前已驻留数量 + 1         (从 1 开始编号)
       b. 若 num 超出 u16/NonZeroU16 → panic "out of file ids"
       c. Box::leak 把 path 永久存下，得到 &'static RootedPath
       d. to_id[leaked] = id；from_id.push(leaked)
       e. 返回 id
```

注意第 2 步是**去重**的关键：同一个 `RootedPath`（值相等）无论 `intern` 多少次，永远拿到同一个 `FileId`。反向查询用 `FileId::get(&self)`（[src/path.rs:L170-L172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L170-L172)），用编号做下标从 `from_id` 取回那条 `'static` 路径。此外 `FileId` 还 `Deref` 到 `RootedPath`（[src/path.rs:L175-L181](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L175-L181)），所以拿到 `FileId` 后可以直接调用 `vpath()`、`root()` 等方法，就像它是个 `RootedPath` 一样。

#### 4.2.3 源码精读

`FileId::new` 的实现（[src/path.rs:L104-L128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L104-L128)）：

```rust
pub fn new(path: RootedPath) -> Self {
    let mut interner = INTERNER.write().unwrap();   // 写锁
    if let Some(&id) = interner.to_id.get(&path) {
        return id;                                   // 命中：去重
    }
    // 未命中：分配新号（从 1 开始）
    let num = u16::try_from(interner.from_id.len() + 1)
        .and_then(NonZeroU16::try_from)
        .expect("out of file ids");                  // 超 2^16 则 panic
    let id = FileId(num);
    let leaked = Box::leak(Box::new(path));           // 永久驻留
    interner.to_id.insert(leaked, id);
    interner.from_id.push(leaked);
    id
}
```

读这几行要抓住三个要点：① `INTERNER.write()` 取的是**写锁**（关于为什么不用「先读锁查、未命中再升级写锁」，源码注释 [src/path.rs:L107-L110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L107-L110) 解释了「双重检查不划算」）；② 编号从 1 开始（`len()+1`），所以 0 永远不会被分配；③ `Box::leak` 把 `path` 变成 `&'static RootedPath`，这正是 leak 策略的体现（4.3 节详谈）。

反向取回路径（[src/path.rs:L169-L172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L169-L172)）：

```rust
pub fn get(&self) -> &'static RootedPath {
    INTERNER.read().unwrap().from_id[usize::from(self.0.get() - 1)]
}
```

注意下标是 `self.0.get() - 1`：因为编号从 1 开始，而 `Vec` 下标从 0 开始，所以要减一。这一行也解释了为什么 `get()` 只要**读锁**——它不修改表。

#### 4.2.4 代码实践

**实践目标**：亲手验证「两条相等的 `RootedPath` 驻留后得到同一个 `FileId`」。

**操作步骤**：下面是一段示例代码（标注为「示例代码」，非项目原有）。可以把它写进一个依赖 `typst-syntax` 的小程序，或改写成本 crate 内的一个 `#[test]`：

```rust
// 示例代码
use typst_syntax::{RootedPath, VirtualPath, VirtualRoot};

let vpath = VirtualPath::new("src/main.typ").unwrap();
let a = RootedPath::new(VirtualRoot::Project, vpath.clone()).intern();
let b = RootedPath::new(VirtualRoot::Project, vpath).intern();

assert_eq!(a, b);                 // 同路径 → 同 FileId
println!("{a:?}");                // Debug 会打印出 /src/main.typ
```

**需要观察的现象**：`assert_eq!(a, b)` 通过；打印 `a` 得到形如 `/src/main.typ` 的路径（因为 `FileId` 的 `Debug` 委托给了 `RootedPath`，见 [src/path.rs:L183-L187](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L183-L187)）。

**预期结果**：两条等值路径驻留后 `FileId` 相等——这正是 `to_id` 去重的直接体现。注意 `VirtualPath` 派生了 `Clone`，所以示例里先 `clone` 一份再 `intern` 两次。

**运行方式（待本地验证）**：把示例放进 `path.rs` 的 `#[cfg(test)] mod tests`，或在外部 crate 里 `cargo add typst-syntax` 后运行。

#### 4.2.5 小练习与答案

**练习 1**：`FileId::get` 为什么用 `self.0.get() - 1` 做下标？

**答案**：因为 `FileId` 的编号从 1 开始（`new` 里 `from_id.len() + 1`），而存放路径的 `from_id: Vec` 下标从 0 开始。编号 `n` 对应 `from_id[n-1]`。这也顺便保证了编号 0 永远不对应任何真实文件。

**练习 2**：既然 `FileId` 已经 `Deref` 到 `RootedPath`，为什么还要单独留一个 `get()` 方法？

**答案**：`Deref::deref` 的返回类型是 `&Self::Target`，其生命周期绑定在 `&self` 上；而 `get()` 显式返回 `&'static RootedPath`，不受借用期限制。当需要把路径引用存到别处、脱离当前 `FileId` 借用时，`'static` 生命周期是必要的。`Deref` 只是为方便链式调用而存在的语法糖，底层同样调用 `get()`。

---

### 4.3 全局 INTERNER 与 leak 驻留策略

#### 4.3.1 概念说明

驻留表本身是一个全局静态变量 `INTERNER`，定义为（[src/path.rs:L84-L86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L84-L86)）：

```rust
static INTERNER: LazyLock<RwLock<Interner>> = LazyLock::new(|| {
    RwLock::new(Interner { to_id: FxHashMap::default(), from_id: Vec::new() })
});
```

它用 `LazyLock` 做首次访问时初始化，用 `RwLock` 支持多线程并发访问。内部的 `Interner`（[src/path.rs:L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L89-L92)）维护**双向映射**：

```rust
struct Interner {
    to_id:   FxHashMap<&'static RootedPath, FileId>,  // 路径 → 编号（去重 + 查询）
    from_id: Vec<&'static RootedPath>,                // 编号 → 路径（get 反查）
}
```

本节要回答的核心问题是：**源码注释里那句「We can't leak more than 2^16 pair … so it's not a big deal」凭什么成立？故意 leak 为什么可接受？** 答案有三点（详见 4.3.2）。

#### 4.3.2 核心流程

为什么 leak 可接受？三条理由，对应三个数学/工程事实：

1. **有硬上限**。`FileId` 是 `NonZeroU16`，最多只能表示 \(2^{16}-1 = 65535\) 个文件。`new` 里用 `u16::try_from(...).and_then(NonZeroU16::try_from).expect("out of file ids")`（[src/path.rs:L119-L121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L119-L121)）在越界时直接 panic。所以 leak 的总量被钉死在 65535 条以内，**绝不会无限增长**。

2. **实际数量远低于上限**。一次 Typst 编译涉及的源文件通常只有几十到几百个（项目文件 + 依赖包的入口文件），离 65535 差着两个数量级。注释里也写「typically will leak a lot less」。

3. **生命周期本就和进程等长**。被编译的文件在整个编译期间一直要被引用（`World` 持有它们），根本不存在「编译到一半某个路径就不再需要」的场景。与其费劲设计释放逻辑，不如让它和进程一起活到结束。leak 换来的是：路径变成 `'static`，可以**无生命周期参数**地存进 `Interner`、`Span` 等结构里，大幅简化类型签名。

用公式概括上限关系：

\[
\text{可驻留文件数} = 2^{16}-1 = 65535
\]

\[

\text{每条驻留开销} \approx \text{sizeof}(\textit{RootedPath}) \text{（含字符串）}
\]

由于 1 给了硬天花板，2 说明实际占用很小，3 说明不释放也没坏处——三者合一，leak 策略站得住脚。

此外还有一个**不走去重**的入口 `FileId::unique`（[src/path.rs:L142-L153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L142-L153)）：它同样分配新号、同样 leak，但**只 push 进 `from_id`，不插 `to_id`**。结果是「同路径也会得到不同 id」，且无法通过路径反查到它。文档说明它专用于「无路径身份」的虚拟文件（如从 stdin 读入的内容）——这种文件每次都该是全新的，不该与任何已有路径共享 id。

#### 4.3.3 源码精读

leak 那段最关键的代码与注释（[src/path.rs:L116-L127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L116-L127)）：

```rust
// Create a new entry forever by leaking the pair. We can't leak more
// than 2^16 pair (and typically will leak a lot less), so its not a
// big deal.
let num = u16::try_from(interner.from_id.len() + 1)
    .and_then(NonZeroU16::try_from)
    .expect("out of file ids");
let id = FileId(num);
let leaked = Box::leak(Box::new(path));   // ← 故意 leak
interner.to_id.insert(leaked, id);
interner.from_id.push(leaked);
```

`Box::leak(Box::new(path))` 的语义是：把 `path` 装箱到堆上，然后「泄露」这个 `Box`，换回一个 `&'static mut RootedPath`（这里隐式重借成共享引用）。从此这块内存**永远不会被释放**，但其引用是 `'static`，可以安全地用作 `FxHashMap` 的键和 `Vec` 的元素。

对照看 `unique` 与 `new` 的差异——`unique` 缺了 `to_id.insert` 这一步（[src/path.rs:L142-L153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L142-L153)），这正是它「不去重、不可反查」的全部原因。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码推算 leak 上限，理解「为什么不会 OOM」。

**操作步骤**：
1. 打开 [src/path.rs:L119-L121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/path.rs#L119-L121)，确认编号上限逻辑：`from_id.len()` 一旦达到 65535，再 `+1` 得 65536，`u16::try_from(65536)` 会失败，`expect("out of file ids")` 触发 panic。
2. 估算最坏内存占用：假设每条 `RootedPath` 约 100 字节（含 `EcoString` 与可能的 `PackageSpec`），65535 条 ≈ 6.5 MB。

**需要观察的现象**：即便达到理论上限，驻留表也只占个位数 MB 级别，对编译器进程而言微不足道。

**预期结果**：leak 在「有上限 + 实际很少 + 生命周期等长」三重保护下是安全的、可接受的。这是一条**源码阅读型实践**——结论从源码与算术直接得出，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果要把 leak 改成「可释放」，至少需要改动哪些地方？会带来什么代价？

**答案**：需要给 `Interner` 引入引用计数或弱引用，让 `FileId` 不再是简单的 `Copy` 数字（要么变成「可能失效的句柄」，要么引入 epoch/代际机制）。代价是：`FileId` 不再零成本复制；`Span` 里嵌入的 16 位 id 可能指向已释放的路径，需要额外的有效性检查；`'static` 生命周期优势丧失，大量类型签名要加生命周期参数。收益（省下几 MB）远不抵这些复杂度，所以 Typst 选择 leak。

**练习 2**：`FileId::unique` 为什么不往 `to_id` 里插？

**答案**：`unique` 的语义是「产生一个全新的、不可通过路径访问的虚拟文件 id」（如 stdin 内容）。如果插进 `to_id`，那么后续 `FileId::new` 用相同路径就会命中它、复用同一个 id，违背了「每次都应独一无二」的设计意图。不插 `to_id` 既保证了唯一性，也保证它不会被路径反查到。

---

### 4.4 FileId 嵌入 Span：用 16 位定位文件

#### 4.4.1 概念说明

到这里，`FileId` 已经是一个轻量的 16 位身份证。但它的使命不止于「代表文件」——它还要**嵌进 `Span`**，和「节点编号」一起塞进 8 字节里。回顾 u6-l1：`Span` 是一个 `NonZeroU64`，位布局是「高 16 位 = FileId，低 48 位 = number」（见 [src/span.rs:L92-L101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L92-L101) 的注释）：

```
| 16 bits file id | 48 bits number |
```

这就解释了本讲最后一个学习目标里的两个「为什么」：

- **为什么 `FileId` 正好是 16 位？** 因为 `Span` 只给文件 id 留了 16 位。`path.rs` 的 `FileId(NonZeroU16)` 与 `span.rs` 的 `FILE_ID_SHIFT = 48`（[src/span.rs:L104](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L104)）是**互相锁死**的设计：一个决定「id 用多少位」，另一个就得给 id 留多少位。\(2^{16}\) 的文件上限正是从这里来的。
- **为什么是 `NonZeroU16`？** 因为 detached（游离）span 用「全 0 的 file id」来表示「不属于任何文件」。`Span::id()` 把高 16 位取出来转成 `NonZeroU16`（[src/span.rs:L160-L167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L160-L167)），若是 0 就返回 `None`。`FileId` 用 `NonZeroU16` 正好保证真实文件的 id 永远非零，**绝不会和 detached 的 0 撞车**。

#### 4.4.2 核心流程

`FileId` 与 `Span` 之间的打包/拆包是一对互逆位运算：

```
打包 pack(id, low):
  bits = (id 的 16 位整数 << 48) | low
  → Span(bits)

拆包 id():
  取高 16 位 = (bits >> 48) as u16
  若为 0 → None (detached)
  否则 → Some(FileId)
```

当解析器要给某节点盖 span 时，用 `Span::from_number(id, SpanNumber(n))`（[src/span.rs:L117-L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L117-L122)）：把 `FileId` 和节点编号 `n` 一左移一或，就压成一个 8 字节 `Span`。反过来拿到一个 `Span`，调 `span.id()` 就能取出它属于哪个文件——整个流程没有任何查表，纯位运算。

#### 4.4.3 源码精读

位布局常量（[src/span.rs:L102-L110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L102-L110)）：

```rust
const NUMBER_BITS: usize = 48;
const FILE_ID_SHIFT: usize = Self::NUMBER_BITS;   // = 48，id 放在高 16 位
const NUMBER_MASK: u64 = (1 << Self::NUMBER_BITS) - 1;
```

打包函数 `pack`（[src/span.rs:L145-L150](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L145-L150)），把 `FileId` 左移到高位：

```rust
const fn pack(id: FileId, low: u64) -> Self {
    let bits = ((id.into_raw().get() as u64) << Self::FILE_ID_SHIFT) | low;
    // The file ID is non-zero.
    Self(NonZeroU64::new(bits).unwrap())
}
```

注意末尾注释与 `unwrap`：因为 `FileId` 的 `into_raw()` 是 `NonZeroU16`（非零），左移到高位后整体 `bits` 也必非零，所以 `NonZeroU64::new(bits).unwrap()` 永不 panic。这正是 `FileId` 选 `NonZeroU16` 的连锁好处——它让整个 `Span` 自动满足 `NonZeroU64` 的约束，于是 `Option<Span>` 也能享受 null 优化（仍是 8 字节）。

拆包函数 `id()`（[src/span.rs:L157-L167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L157-L167)）：

```rust
pub const fn id(self) -> Option<FileId> {
    // Detached span has only zero high bits, so it will trigger the None case.
    match NonZeroU16::new((self.0.get() >> Self::FILE_ID_SHIFT) as u16) {
        Some(v) => Some(FileId::from_raw(v)),
        None => None,
    }
}
```

右移 48 位取出高 16 位，若是 0（detached）则 `NonZeroU16::new(0)` 返回 `None`。一行代码同时完成了「取文件 id」和「判断是否 detached」两件事。

最后看 `SpanKind`（[src/span.rs:L75-L83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L75-L83)）：`Number { id: FileId, num: SpanNumber }` 变体正是把 `FileId` 与节点编号并排暴露给使用者的「易用视图」，由 `Span::get()` 解包得到。

#### 4.4.4 代码实践

**实践目标**：亲手做一次 `FileId` ↔ `Span` 的打包-拆包往返，验证 16 位 id 能无损嵌进 `Span`。

**操作步骤**：参考 `span.rs` 内联测试 `test_span_number_encoding`（[src/span.rs:L564-L570](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L564-L570)）。下面是「示例代码」，模仿该测试：

```rust
// 示例代码
use std::num::NonZeroU16;
use typst_syntax::{FileId, Span, SpanNumber};

let id = FileId::from_raw(NonZeroU16::new(5).unwrap());
let span = Span::from_number(id, SpanNumber(10));   // 注意 from_number 是 pub(crate)
assert_eq!(span.id(), Some(id));                    // 高 16 位取回 id
```

> 注意：`Span::from_number` 在当前源码里是 `pub(crate)` 可见（[src/span.rs:L118](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L118)），外部 crate 无法直接调用。要实际运行，应把它写成**本 crate 内的 `#[test]`**（放进 `span.rs` 的 `#[cfg(test)] mod tests`），或直接运行已有测试 `cargo test -p typst-syntax test_span_number_encoding`。

**需要观察的现象**：`span.id()` 返回 `Some(id)`，且取回的 `FileId` 与传入的完全相等——说明 16 位 id 经左移/右移往返无损。

**预期结果**：往返一致。这同时印证：只要 id 是真实文件（非零），`id()` 必返回 `Some`；而 `Span::detached().id()` 返回 `None`（因为其高 16 位是 0）。

#### 4.4.5 小练习与答案

**练习 1**：假设把 `FileId` 从 `NonZeroU16` 改成普通 `u16`，`Span::pack` 末尾的 `NonZeroU64::new(bits).unwrap()` 还安全吗？

**答案**：不再安全。若 `id` 可以为 0 且 `low` 恰好也为 0，则 `bits == 0`，`NonZeroU64::new(0)` 返回 `None`，`unwrap()` 会 panic。`NonZeroU16` 从类型层面排除了 id 为 0 的可能，使得「`bits` 必非零」成为编译期可推理的不变量——这正是选 `NonZero` 的根本原因。

**练习 2**：`Span::detached()` 的 `id()` 为什么返回 `None`，而不是某个特殊 `FileId`？

**答案**：detached span 用整体值 1 表示，其高 16 位（file id）是 0、低 48 位（number）是 1。file id 为 0 没有对应任何真实文件（真实 id 从 1 开始），所以 `NonZeroU16::new(0)` 自然返回 `None`。这让「是否 detached」与「file id 是否为 0」成为同一个判定，无需额外的标志位。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这条「从路径到 span」的端到端追踪任务：

**任务**：在脑中（或写成本 crate 内的测试）走完下面这条链路，并回答问题。

1. 构造一条项目内路径：`VirtualPath::new("chapter/intro.typ").unwrap()`，配上 `VirtualRoot::Project`，组成 `RootedPath`。
2. 调用 `.intern()` 得到 `FileId`（记为 `id`）。**问题 A**：此时全局 `INTERNER` 的 `to_id` 和 `from_id` 各多了一条什么记录？
3. 假设解析器给某节点编号 `SpanNumber(42)`，用 `Span::from_number(id, SpanNumber(42))` 得到 `span`。**问题 B**：`span.id()` 返回什么？`span.number()` 返回什么？
4. 再用同样的 `RootedPath`（重新构造一份相等的）调用 `.intern()`。**问题 C**：第二次得到的 `FileId` 与 `id` 是什么关系？为什么不需要新增表项？
5. **问题 D**：这条链路里，哪一步是「故意 leak」？请用本讲的三个理由说明它为何可接受。

**参考答案**：

- A：`to_id` 多一条「该 `&'static RootedPath` → `id`」的映射；`from_id` 末尾 push 了同一条 `'static` 路径，下标为 `id 数值 - 1`。
- B：`span.id()` 返回 `Some(id)`（高 16 位取回）；`span.number()` 返回 `42`（低 48 位掩码）。
- C：两次 `FileId` 完全相等（`==` 成立）。因为 `new` 第一步就用 `to_id.get(&path)` 命中了已有记录，直接返回旧 id，不分配新号、不新增 `from_id` 项。
- D：第 2 步的 `Box::leak(Box::new(path))` 是故意 leak。可接受的理由：① 有 \(2^{16}-1\) 的硬上限（`expect("out of file ids")`）；② 实际文件数远低于上限；③ 文件路径的生命周期本就与编译进程等长，不释放也无害，且换来了 `'static` 生命周期、简化了 `Span` 等类型签名。

## 6. 本讲小结

- Typst 用一层**虚拟文件系统**屏蔽操作系统差异：`RootedPath = (VirtualRoot, VirtualPath)`，其中 `VirtualRoot` 区分项目/包，`VirtualPath` 是规范化的、以 `/` 开头的绝对路径，且词法上不可能越过根。
- `FileId(NonZeroU16)` 是 `RootedPath` 的**全局驻留编号**，`Copy`/`Eq`/`Hash` 全是整数操作，把「文件身份」从重结构压成 2 字节。
- 驻留表 `INTERNER` 维护 `to_id`（去重/查询）与 `from_id`（反查）双向映射；`intern` 命中则复用、未命中则分配并 `Box::leak`。
- **leak 策略可接受**的三重保障：\(2^{16}-1\) 硬上限、实际占用很小、生命周期与进程等长——换来 `'static` 路径引用与极简类型签名。
- `FileId` 之所以正好 16 位且非零，是与 `Span` **互相锁死**的设计：`Span` 高 16 位存 id、低 48 位存节点编号；`NonZeroU16` 保证真实文件 id 永不为 0，从而与 detached span（id 为 0）天然区分，并让整个 `Span` 满足 `NonZeroU64`。
- 额外入口 `FileId::unique` 不走去重、不可路径反查，专供 stdin 等「无路径身份」的虚拟文件。

## 7. 下一步学习建议

本讲把「文件身份」讲透了，接下来两条线可以并行选择：

- **顺读 u10-l2《包清单解析》**：`VirtualRoot::Package(PackageSpec)` 里的 `PackageSpec` 正是从 `typst.toml` 解析来的。下一篇会讲清 `PackageManifest`/`PackageSpec`/`PackageVersion` 的结构与 serde 解析，正好补上「包根是怎么来的」这一环。
- **回顾与延伸 Span 系统**：若想进一步看清 `FileId` 嵌入 `Span` 后如何参与诊断定位，建议重读 u6-l1《Span 紧凑编码》与 u6-l3《DiagSpan、SubRange 与外部范围》，对照本讲的 16 位 file id 理解 `DiagSpan` 为何还要额外的 8 字节 `extra`。
- **源码延伸**：想看 `FileId` 在真实编译流程里如何被使用，可以追踪 `Source::new(id, text)`（u8-l1）——`Source` 的第一个参数就是本讲的 `FileId`，它是 `Source` 把「文本 + 语法树 + 行索引」绑到一个具体文件上的纽带。
