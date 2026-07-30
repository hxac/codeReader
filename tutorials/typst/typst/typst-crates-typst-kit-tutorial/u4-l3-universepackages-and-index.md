# UniversePackages 与包索引

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `UniversePackages` 在 typst-kit 包加载链（data → cache → 网络）中扮演的「网络层」角色，以及它为什么只服务 `preview` 命名空间。
- 解释 `package()` 如何把一个 `PackageSpec` 拼成 URL、下载 `.tar.gz`、解压，并返回一个可在内存里读取的 `tar::Archive`。
- 理解 `index()` 的 **懒加载**（`OnceCell`）与 **惰性反序列化**（索引以原始 `serde_json::Value` 形式缓存、按需逐条解析）策略，并说明这种设计为什么带来**向前兼容**。
- 读懂 `lazy_deserialize_index` 测试，解释为何非法版本号 `0.2.0-dev` 的条目被「跳过」而非让整个 `latest_version()` 失败。

本讲承接 u4-1（`SystemPackages` 三级加载链）、u4-2（`FsPackages` 原子存储）与 u5-1（`Downloader` trait）。`UniversePackages` 正是 u4-1 中「网络这一级」的具体实现，而它把所有真正的网络收发都委托给了 u5-1 的 `Downloader`。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 什么是 Typst Universe

Typst Universe 是 Typst 官方的包注册表，地址是 `https://packages.typst.org`。你在 Typst 里写 `#import "@preview/cetz:0.3.1": canvas` 时，`@preview` 是命名空间（namespace），`cetz` 是包名，`0.3.1` 是版本。Typst Universe 只服务 `preview` 这一个命名空间，这一点会反复出现在源码里（常量 `NAMESPACE = "preview"`）。

### 2.2 包加载链回顾

回顾 u4-1：`SystemPackages::obtain(spec)` 按 data 目录 → cache 目录 → 网络的优先级解析一个包。当 data、cache 都未命中、且包属于 `preview` 命名空间时，就轮到本讲的主角 `UniversePackages` 出场，从网络下载并落盘到 cache。所以 `UniversePackages` 是整条链的「最后一公里」。

### 2.3 字节来源与解耦

`UniversePackages` **自己不会发 HTTP 请求**。它只负责「拼 URL + 解包 + 管索引」，真正的网络收发交给 u5-1 的 `Downloader` trait。换句话说：

- `UniversePackages` 知道**要下载什么、下载后怎么处理**。
- `Downloader` 知道**怎么把字节从网上拿回来**。

这种拆分让 `UniversePackages` 可以用任何 `Downloader` 实现（系统下载器、带进度的 `ProgressDownloader`、或测试用的假下载器）。

### 2.4 关键数据类型速查

来自 `typst-syntax` 的几个类型会在本讲反复出现：

- `PackageSpec`：完整包标识，含 `namespace / name / version`，显示为 `@preview/cetz:0.3.1`。
- `VersionlessPackageSpec`：去掉版本，显示为 `@preview/cetz`，用于「查最新版本」。
- `PackageVersion`：语义化版本 `(major, minor, patch)`，派生了 `Ord`，可直接比较大小、取 `max()`。

错误类型 `PackageError` 来自 `typst-library`，本讲关注其中三个变体：`NotFound`（包不存在）、`VersionNotFound`（包存在但该版本不存在）、`NetworkFailed`（网络失败）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-kit/src/packages.rs` | **本讲核心**。定义 `UniversePackages` 及其 `package()`、`index()`、`latest_version()`，并含 `lazy_deserialize_index` 测试。 |
| `crates/typst-kit/src/downloader.rs` | `Downloader` trait 的定义。`UniversePackages` 通过它下载字节，是 u5-1 的内容，本讲只引用其接口。 |
| `crates/typst-syntax/src/package.rs` | `PackageSpec` / `VersionlessPackageSpec` / `PackageVersion` 的定义，含 `PackageVersion` 的解析与反序列化（解释 `0.2.0-dev` 为何非法的关键）。 |
| `crates/typst-library/src/diag.rs` | `PackageError` 枚举定义。 |

## 4. 核心概念与源码讲解

### 4.1 UniversePackages：Typst Universe 的注册表句柄

#### 4.1.1 概念说明

`UniversePackages` 是 typst-kit 对「Typst Universe 官方注册表」的封装句柄。它解决的问题是：

> 给定一个 `PackageSpec`，从注册表下载这个包；给定一个 `VersionlessPackageSpec`，查出它的最新版本。

它**不负责**把包写到磁盘（那是 `FsPackages::store` 的工作，见 u4-2），也**不负责**真正发网络请求（那是 `Downloader` 的工作，见 u5-1）。它只承担「注册表协议」这一层：知道 URL 怎么拼、`.tar.gz` 怎么解、索引 JSON 怎么读。源码文档直言这一点：

[src/packages.rs:L309-L312](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L309-L312) —— `UniversePackages` 的文档注释说明：并不存在标准化的注册表协议，本类型只是「为配合官方 Typst Universe 注册表而设计」。

注意它受 `#[cfg(feature = "universe-packages")]` 门禁。这个特性是 `system-packages` 的下游（特性依赖链 `system-packages → universe-packages`，见 u1-l2），所以只要你用了 `SystemPackages`，`UniversePackages` 就一定存在。

#### 4.1.2 核心流程

`UniversePackages` 只有三个字段，分别对应三种职责：

```text
┌─────────────────────────────────────────────┐
│            UniversePackages                 │
├─────────────────────────────────────────────┤
│  url: String                                │  ← 注册表基地址（默认 packages.typst.org）
│  downloader: Box<dyn Downloader>            │  ← 字节从哪来（见 u5-1）
│  index: OnceCell<Box<[serde_json::Value]>>  │  ← 包索引的懒加载缓存
└─────────────────────────────────────────────┘
```

- `url`：注册表基地址。`new()` 默认指向 `https://packages.typst.org`，`with_url()` 可换成镜像。
- `downloader`：以 trait object（`Box<dyn Downloader>`）形式持有，擦除了具体类型，这样运行时可以换成带进度的 `ProgressDownloader`。
- `index`：包索引，用 `OnceCell` 实现「第一次访问才下载、之后复用」。它存的是**原始 JSON 值**而非反序列化后的结构体——这是 4.3 节的重点。

它对外暴露两个主要能力：`package(spec)` 下载某个包、`latest_version(spec)` 查最新版本。后者依赖前者所用的索引。

#### 4.1.3 源码精读

先看结构体定义与三个字段：

[src/packages.rs:L314-L321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L314-L321) —— `UniversePackages` 结构体，含 `url`、`downloader`、`index` 三字段。

再看构造方法。`new` 是面向官方注册表的便捷构造，内部直接转调 `with_url`：

[src/packages.rs:L328-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L328-L341) —— `new()` 把 URL 写死为 `https://packages.typst.org`；`with_url()` 是真正的构造器，接受任意 `impl Downloader` 与任意基地址。

注意三个细节：

1. `new` / `with_url` 都接受 `impl Downloader`（泛型），存进去时 `Box::new(downloader)` 擦除类型——「进时泛型、存时 object」的常见模式。
2. `index` 初始化为 `OnceCell::new()`，即「空」，下载被推迟到首次访问（4.3 节）。
3. 命名空间被定义成常量：

[src/packages.rs:L324-L326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L324-L326) —— `NAMESPACE = "preview"`，Universe 只服务这个命名空间；`package()` 和 `latest_version()` 都会先校验它。

最后，`UniversePackages` 没有派生 `Debug`，而是手写了实现，故意只暴露 `url`，隐藏 `downloader` 和 `index`：

[src/packages.rs:L441-L448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L441-L448) —— 手写 `Debug`，用 `finish_non_exhaustive()` 表示还有字段但不想打印（注意结构体名显示成 `"Downloader"`，这是源码里一个小瑕疵，但不影响行为）。

#### 4.1.4 代码实践

**实践目标**：确认 `UniversePackages` 的 URL 与命名空间常量，并体会「构造时注入下载器」的解耦。

**操作步骤（源码阅读型）**：

1. 打开 `src/packages.rs`，定位 `impl UniversePackages`，读 `new`、`with_url`、`url()`、`NAMESPACE`。
2. 在 `crates/typst-cli/src/packages.rs`（或 grep `UniversePackages::` 全仓）中查找 CLI 是如何构造 `UniversePackages` 的，确认它是否通过 `with_url` 传入用户自定义的注册表地址（典型场景是企业内网镜像）。
3. 思考：如果你要写一个单元测试，让 `UniversePackages` 在**完全不联网**的情况下工作，你需要注入一个什么样的 `Downloader`？（答案：一个返回固定字节、或直接返回 `NotFound` 的假下载器——4.3 节的测试正是这么做的。）

**预期结果**：你会清楚看到「注册表地址 + 下载器」是两个可独立替换的维度。

> 待本地验证：第 2 步中 CLI 是否真的把命令行参数 `--registry` 之类的值透传给 `with_url`，请在本地 grep 确认（不同版本实现可能有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `downloader` 字段用 `Box<dyn Downloader>` 而不是泛型参数 `UniversePackages<D: Downloader>`？

**参考答案**：用 trait object 可以在运行时替换实现（比如先包一层 `ProgressDownloader` 再注入），并且让 `UniversePackages` 能作为 `SystemPackages` 的具体字段存在而不把泛型参数「传染」到整个加载链。代价是少了一次静态分发、多一次虚函数调用，但下载本身是 I/O，这点开销可忽略。

**练习 2**：`UniversePackages::new` 默认指向哪个 URL？想用镜像怎么办？

**参考答案**：默认 `https://packages.typst.org`。镜像用 `with_url(downloader, "https://你的镜像地址")`。

---

### 4.2 package()：下载并解包 .tar.gz

#### 4.2.1 概念说明

`package(spec)` 是 `UniversePackages` 的核心下载方法。它的职责很纯粹：

> 把一个 `PackageSpec` 变成一个**可在内存里读取的 `tar::Archive`**——即解压好、但还没落到磁盘上的包内容。

注意它返回的是 `PackageResult<tar::Archive<...>>`，**不是**文件路径、**也不是**已经写好的目录。真正把内容写到 cache 目录的是调用方——`SystemPackages::obtain` 里的 `cache.store(spec, |tempdir| archive.unpack(tempdir)...)`（见 u4-1、u4-2）。这种「下载/解码」与「落盘」的分离，让 `package()` 可以被独立测试，也让落盘策略（原子 rename）集中在 `FsPackages::store`。

#### 4.2.2 核心流程

`package()` 的执行过程可以画成一条带分支的流水线：

```text
package(spec)
  │
  ├─ spec.namespace != "preview" ?  ──是──▶ Err(NotFound)      ← 只服务 preview
  │
  ├─ 拼接 URL: {url}/preview/{name}-{version}.tar.gz
  │
  ├─ downloader.download(spec, url)
  │     │
  │     ├─ Ok(bytes)
  │     │     └─ GzDecoder(Cursor(bytes)) ──▶ tar::Archive ──▶ Ok(archive)
  │     │
  │     ├─ Err(NotFound)   ← 远端返回 404
  │     │     └─ 再查 latest_version 来区分两种情况：
  │     │          ├─ 查到最新版 ─▶ Err(VersionNotFound(spec, latest))
  │     │          └─ 查不到     ─▶ Err(NotFound(spec))
  │     │
  │     └─ Err(其他)         ──▶ Err(NetworkFailed)
```

最值得品味的是 **404 的二义性处理**：一个 URL 返回 404，既可能是「包名写错了」（包根本不存在），也可能是「版本写错了」（包存在但没这个版本）。`package()` 不满足于一个笼统的 404，而是再花一次 `latest_version()` 调用来**消歧**，从而给用户更精准的报错（「包存在，但版本 X 不存在，最新版是 Y」）。

数据流上还要注意：`download()` 返回的是 `Vec<u8>`（u5-1 的默认实现会把流读成一个完整向量），所以包字节此刻**全在内存里**；随后 `GzDecoder` 包裹 `Cursor`（把 `Vec` 当成可读的「虚拟文件」），`tar::Archive` 再在上面逐条读 tar 条目。也就是说：gzip 是**按需流式解压**的，但原始压缩字节已经全在内存了。

#### 4.2.3 源码精读

完整方法如下：

[src/packages.rs:L348-L380](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L348-L380) —— `package()`：校验命名空间 → 拼 URL → 下载 → 解压成 `tar::Archive`，并把 404 映射为 `VersionNotFound`/`NotFound`。

逐段拆解关键代码点：

**① 命名空间校验**（L355-L357）：非 `preview` 直接 `NotFound`，因为 Universe 不服务别的命名空间。

**② URL 拼接**（L359-L365）：格式是 `{url}/preview/{name}-{version}.tar.gz`。注意它用 `Self::NAMESPACE` 而非硬编码字符串，保证一致性。

**③ 下载与解压**（L367-L371）：

```rust
match self.downloader.download(spec, &url) {
    Ok(data) => {
        let decompressed = flate2::read::GzDecoder::new(Cursor::new(data));
        Ok(tar::Archive::new(decompressed))
    }
```

这里 `download` 的第一个参数 `spec` 是 **key**——它是 `&dyn Any` 类型（见 u5-1 的 `Downloader` trait）。这个 key 不是给下载用的，而是给 `ProgressDownloader`（u5-l2）用的：上层据此判断「这次下载要不要显示进度条」。包下载传 `PackageSpec`、索引下载传字符串 `"package index"`，正是为了让进度层能区分二者。

**④ 404 消歧**（L372-L377）：

```rust
Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
    Err(match self.latest_version(&spec.versionless()) {
        Ok(version) => PackageError::VersionNotFound(spec.clone(), version),
        Err(_) => PackageError::NotFound(spec.clone()),
    })
}
```

`spec.versionless()` 把 `@preview/cetz:0.3.1` 变成 `@preview/cetz`，再交给 `latest_version()`（4.3 节）查最新版。这里把 u5-1 约定的「404 → `io::ErrorKind::NotFound`」契约用得淋漓尽致：正是因为下载层把 404 统一翻译成 `NotFound`，`package()` 才能用一个 `if` 精准捕获它。

**⑤ 其他网络错误**（L378）：一律 `NetworkFailed`。

要看 `package()` 如何被上层消费，回到 `SystemPackages::obtain`：

[src/packages.rs:L107-L120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L107-L120) —— 仅当 cache 存在且命名空间为 `preview` 时，调用 `universe.package(spec)` 拿到 archive，再用 `cache.store` 把它原子落盘，最后 `cache.obtain` 读回 `FsRoot`。

这条链印证了 4.2.1 的分工：`package()` 负责取字节+解压，`store()` 负责原子写盘。

#### 4.2.4 代码实践

**实践目标**：跟踪 `package()` 的返回类型，确认「字节在内存、解压在流上、落盘在调用方」。

**操作步骤（源码阅读型）**：

1. 在 `src/packages.rs` 中定位 `package()` 的签名，看清返回类型 `PackageResult<tar::Archive<impl Read + use<>>>`。
2. 跟着 `flate2::read::GzDecoder::new(Cursor::new(data))` 与 `tar::Archive::new(decompressed)`，回答：`Archive` 持有的 reader 链是 `tar → GzDecoder → Cursor<Vec<u8>>`，对吗？（对。）
3. 跳到 `SystemPackages::obtain`（L108-L115），看 `archive.unpack(tempdir)`——确认「解包到磁盘」发生在这里，而不在 `package()` 内。
4. 查阅 `tar` crate 文档（或本地 `cargo doc --open -p tar`）确认 `Archive::unpack` 会把 tar 里所有条目写到给定目录。

**需要观察的现象 / 预期结果**：你会清楚地看到三个阶段——`download`（全量进内存）→ `GzDecoder`（按需解压）→ `unpack`（落盘）——分别由 `Downloader`、`package()`、`store()` 三个角色承担。

> 待本地验证：若想亲眼看到解压过程，可在测试里构造一个返回真实 `.tar.gz` 字节的假 `Downloader`，调用 `package()` 后 `archive.entries()` 遍历条目名（参考 4.3 节测试里的 `DummyDownloader` 写法，把它改成返回固定字节）。

#### 4.2.5 小练习与答案

**练习 1**：当下载返回 404 时，`package()` 为什么不直接报 `NotFound`，而是再查一次 `latest_version`？

**参考答案**：404 有二义性——可能是包名错（包不存在），也可能是版本错（包存在但版本不存在）。查一次最新版即可消歧：查得到就报 `VersionNotFound`（附带「最新版是 Y」，对用户极有帮助），查不到才报 `NotFound`。

**练习 2**：`package()` 返回的 `tar::Archive` 里，包的字节此刻在内存还是磁盘？

**参考答案**：在内存。`download()` 返回 `Vec<u8>`，被 `Cursor` 包成可读源；`GzDecoder` 在其上做按需解压。磁盘写入要等到上层 `archive.unpack(tempdir)`。

---

### 4.3 index() 与 latest_version()：懒加载索引与惰性反序列化

#### 4.3.1 概念说明

`latest_version(spec)` 回答「这个包的最新版本是多少」。要回答它，必须有一份**包索引**——注册表上所有包及其版本的清单。Typst Universe 把这份清单放在 `{url}/preview/index.json`。

这里有两个关键设计决策：

1. **懒加载（lazy）**：索引不在构造 `UniversePackages` 时下载，而是第一次真正需要时才下载，且只下载一次（`OnceCell`）。绝大多数使用场景根本不会查最新版本（用户已经写明了版本号），所以懒加载避免了无谓的网络请求。

2. **惰性反序列化（lazy deserialization）**：索引下载后，**不**立即反序列化成某个结构体数组，而是以原始 `serde_json::Value` 的形式整个缓存起来；只有当 `latest_version()` 真正遍历时，才**逐条**尝试解析，解析失败的条目被**跳过**而不是让整体失败。

第二个决策是本讲最精妙之处，也是本讲指定的实践任务要分析的核心。它的意义是**向前兼容**：注册表的索引格式没有标准化、可能随时变化（源码注释明确写了这一点）。如果未来注册表里出现本编译器版本看不懂的条目（比如用了新的版本号格式、或带上了未知字段），「逐条跳过」策略能保证其余正常条目仍可用；而「一次性整表反序列化」则会让任何一条坏数据拖垮整个索引。

#### 4.3.2 核心流程

`latest_version()` 的执行流程：

```text
latest_version(spec)
  │
  ├─ spec.namespace != "preview" ?  ──是──▶ bail（只有 preview 有索引）
  │
  ├─ self.index()?        ← 拿到 &[serde_json::Value]（首次会触发下载+缓存）
  │
  └─ 对每个 value：
        MinimalPackageInfo::deserialize(value)
          ├─ Ok(info)  ──▶ 保留
          └─ Err       ──▶ .ok() 变 None ──▶ filter_map 跳过
        再过滤 name == spec.name
        取 version
        最后 .max()      ← PackageVersion 的 Ord 比较
```

`index()` 自身的流程：

```text
index()
  └─ OnceCell::get_or_try_init:
        拼 URL: {url}/preview/index.json
        downloader.download(&"package index", url)
          ├─ Ok(data) ─▶ serde_json::from_slice ─▶ Box<[Value]>  ─▶ 缓存
          ├─ Err(NotFound) ─▶ bail("not found")
          └─ Err(其他)    ─▶ bail(...)
```

关于版本取最大值：`PackageVersion` 派生了 `Ord`，按 `(major, minor, patch)` 的字典序比较。设两个版本 \(v_1=(M_1,m_1,p_1)\)、\(v_2=(M_2,m_2,p_2)\)，则

\[
v_1 > v_2 \iff (M_1,m_1,p_1) \succ (M_2,m_2,p_2)
\]

以字典序比较，所以 \(0.10.0 > 0.2.0\)（注意不是字符串比较，是数值比较，避免了 `"0.10.0" < "0.2.0"` 的经典坑）。

#### 4.3.3 源码精读

**① `latest_version()` 与内嵌的 `MinimalPackageInfo`**：

[src/packages.rs:L382-L412](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L382-L412) —— `latest_version()` 与仅在函数内部定义的 `MinimalPackageInfo`。

`MinimalPackageInfo` 是个**只含两个字段**的最小结构体：

```rust
#[derive(Deserialize)]
struct MinimalPackageInfo {
    name: String,
    version: PackageVersion,
}
```

它故意只取 `name` 和 `version`——查最新版本只需要这两个字段。由于 serde 默认**忽略未知字段**（没有 `#[serde(deny_unknown_fields)]`），索引条目里诸如 `entrypoint`、`description`、`authors` 等多余字段都会被安静忽略，不影响解析。

关键的一行是 L405-L411：

```rust
self.index()?
    .iter()
    .filter_map(|value| MinimalPackageInfo::deserialize(value).ok())
    .filter(|package| package.name == spec.name)
    .map(|package| package.version)
    .max()
    .ok_or_else(|| eco_format!("failed to find package {spec}"))
```

`.filter_map(|value| MinimalPackageInfo::deserialize(value).ok())` 就是「惰性反序列化 + 跳过坏条目」的核心：`deserialize` 返回 `Result`，`.ok()` 把 `Err` 变成 `None`，`filter_map` 随即丢弃它。随后 `filter` 按名字筛选、`map` 取版本、`max()` 取最大；若一个都没匹配到，`max()` 返回 `None`，被 `ok_or_else` 转成 `"failed to find package"` 错误。

**为什么 `0.2.0-dev` 会被跳过？** 因为 `MinimalPackageInfo.version` 的类型是 `PackageVersion`，它的反序列化会调用字符串解析。看 `PackageVersion` 的自定义 `Deserialize`：

[crates/typst-syntax/src/package.rs:L475-L480](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L475-L480) —— `PackageVersion` 的 `Deserialize`：先把输入当字符串读，再 `.parse()`。

而 `parse` 走的是 `FromStr`：

[crates/typst-syntax/src/package.rs:L432-L455](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L432-L455) —— `PackageVersion::from_str`：按 `.` 切三段，每段必须能 `parse::<u32>()`，且不能有第四段。

对 `"0.2.0-dev"`，按 `.` 切得到 `["0", "2", "0-dev"]`，patch 段 `"0-dev"` 无法解析成 `u32`，于是 `from_str` 报错 `` `0-dev` is not a valid patch version ``，进而 `Deserialize` 失败，进而被 `.ok()` 跳过。这就是为什么含 `0.2.0-dev` 的条目不会让整个 `latest_version()` 崩掉。

**② `index()` 的懒加载**：

[src/packages.rs:L414-L438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L414-L438) —— `index()`：用 `OnceCell::get_or_try_init` 实现下载一次、永久缓存，并以 `serde_json::Value` 而非结构体形式存储。

重点看注释（L417-L419）对设计意图的直接陈述：「为了兼容性，单个条目保持未反序列化状态。这样，无法被当前编译器版本反序列化的包会被跳过，而不是整体失败。」这正是 4.3.1 所说的向前兼容。

还要注意索引 URL 用的是 key `&"package index"`（L427），与包下载用的 key（`PackageSpec`）不同——这同样是给 `ProgressDownloader` 区分进度展示用的。

#### 4.3.4 代码实践（本讲指定任务）

**实践目标**：分析 `lazy_deserialize_index` 测试，彻底理解「非法版本号 `0.2.0-dev` 被跳过」的机制及其向前兼容意义。

**测试源码**：

[src/packages.rs:L450-L501](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L450-L501) —— `lazy_deserialize_index`：用一个永远返回 `NotFound` 的 `DummyDownloader` 构造 `UniversePackages`，再**直接注入**两条约定的索引数据，验证惰性反序列化行为。

**操作步骤（分析型）**：

1. **看测试如何避免联网**：`DummyDownloader::stream` 直接返回 `Err(NotFound)`（L460-L468）。由于测试不会触发真正的下载（它绕过 `index()` 直接塞数据），这个假下载器只是为了满足构造 `UniversePackages` 的参数要求。
2. **看测试如何注入索引**：L471-L484 通过 `packages.index = OnceCell::from(...)` 直接把两条 `serde_json::Value` 塞进 `OnceCell`，跳过下载阶段。这正是 `index` 字段为 `pub(crate)` 可见性的妙用（同模块测试可写）。两条数据：
   - `charged-ieee` 版本 `0.1.0`（合法）；
   - `unequivocal-ams` 版本 `0.2.0-dev`（**非法**，注释明说 "currently not valid, so this package can't be parsed"）。
3. **看第一条断言**（L486-L490）：查 `charged-ieee` 的最新版，得到 `Ok(0.1.0)`。说明合法条目正常解析。
4. **看第二条断言**（L492-L499）：查 `unequivocal-ams` 的最新版，得到 `Err("failed to find package @preview/unequivocal-ams")`。

**需要解释的核心问题**：为什么第二条不是让整个 `latest_version()` 报「解析错误」，而是报「找不到包」？

**参考答案**：因为 `latest_version()` 在 L407 用了 `.filter_map(|value| MinimalPackageInfo::deserialize(value).ok())`。对 `unequivocal-ams` 这条，`deserialize` 会尝试把 `"0.2.0-dev"` 解析成 `PackageVersion`——按 4.3.3 的分析，`"0-dev"` 不是合法 `u32`，解析失败——`.ok()` 把这个 `Err` 变成 `None`，`filter_map` 丢弃整条。于是遍历完后没有任何条目匹配 `unequivocal-ams`，`max()` 返回 `None`，最终落到 `ok_or_else` 报「找不到包」。坏条目被**孤立跳过**，没有污染对 `charged-ieee` 的查询。

**对向前兼容的意义**：设想未来的 Typst Universe 索引里出现了当前老版本编译器看不懂的条目（比如新的版本号格式、或字段结构变化）。「逐条跳过」保证：只要你想查的那个包本身是老编译器能理解的格式，查询就照样成功；只有当你主动去查那条「看不懂」的包时才会得到「找不到」。反之，若用「一次性整表反序列化成 `Vec<FullPackageInfo>`」，任何一条坏数据都会让整个索引加载失败，于是**所有**包的最新版本查询集体失效——这对老编译器面对新注册表是灾难性的。这就是惰性反序列化的价值：把「我不认识的条目」从致命错误降级为可忽略的噪音。

> 待本地验证：若想亲手运行，执行 `cargo test --features universe-packages lazy_deserialize_index -p typst-kit` 观察两条断言通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么索引以 `serde_json::Value` 数组的形式缓存，而不是直接反序列化成一个 `Vec<SomeStruct>`？

**参考答案**：为了向前兼容。索引格式未标准化、可能变化。以原始 `Value` 缓存 + 用时逐条 `MinimalPackageInfo::deserialize`，可以让看不懂的条目被 `.ok()` 跳过，而不拖垮整个索引。一次性整表反序列化则会让单条坏数据导致全盘失败。

**练习 2**：在 `latest_version` 的迭代里，`.filter_map(|value| MinimalPackageInfo::deserialize(value).ok())` 中的 `.ok()` 如果换成 `unwrap()`，会发生什么？

**参考答案**：遇到 `0.2.0-dev` 这类无法解析的条目时直接 panic，整个程序崩溃。`.ok()` 把解析失败降级为「跳过该条」，是惰性反序列化能工作的关键。

**练习 3**：为什么 `MinimalPackageInfo` 只声明 `name` 和 `version` 两个字段，却不担心索引条目里的其他字段（如 `entrypoint`）导致反序列化失败？

**参考答案**：serde 默认忽略未知字段（除非加了 `#[serde(deny_unknown_fields)]`）。所以多出的字段被安静丢弃，这正是「最小信息结构」能稳稳匹配「最大可能输入」的原因。

## 5. 综合实践

把本讲三个模块串起来，完成下面这条**端到端追踪任务**。

**场景**：用户在 Typst 里写 `#import "@preview/charged-ieee:99.0.0": *`（包存在，但版本 `99.0.0` 不存在）。请用文字画出从「触发下载」到「得到精准报错」的完整链路，并指出每一步分别由本讲的哪个方法/字段负责。

**要求覆盖的环节**：

1. `SystemPackages::obtain` 发现 data、cache 都未命中，命名空间是 `preview`，进入网络分支，调用 `UniversePackages::package(spec)`（参考 [src/packages.rs:L107-L120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L107-L120)）。
2. `package()` 拼出 URL `…/preview/charged-ieee-99.0.0.tar.gz` 并交给 `downloader.download`。
3. 远端返回 404，下载层按 u5-1 的契约把它翻译成 `io::ErrorKind::NotFound`。
4. `package()` 捕获 `NotFound`，转而调用 `latest_version(&spec.versionless())` 消歧（[src/packages.rs:L372-L377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L372-L377)）。
5. `latest_version()` 触发 `index()` 首次下载 `index.json` 并缓存（`OnceCell`），再惰性遍历、过滤 `charged-ieee`、取 `max()` 得到真实最新版（比如 `0.1.0`）。
6. `package()` 据此返回 `Err(VersionNotFound(spec, 0.1.0))`，最终用户看到「包存在，但版本 99.0.0 不存在，最新是 0.1.0」。

**额外思考**：如果第 5 步的索引里 `charged-ieee` 那条恰好带了一个本编译器看不懂的版本号（类似 `0.2.0-dev`），最终报错会退化成什么？（答：该坏条目被跳过；若 `charged-ieee` 还有别的合法版本条目，仍能查到并返回 `VersionNotFound`；若它**只有**这一条坏版本，则 `latest_version` 报「找不到」，进而 `package()` 退化为 `NotFound`。）

## 6. 本讲小结

- `UniversePackages` 是包加载链的「网络层」，只服务 `preview` 命名空间（`NAMESPACE`），把真正的网络收发委托给 `Downloader`（u5-1），自己只管 URL 拼接、解包与索引。
- `package(spec)` 把 `PackageSpec` 拼成 `…/preview/{name}-{version}.tar.gz`，下载后用 `GzDecoder`+`tar::Archive` 在内存里解压；它**不落盘**，落盘由上层 `SystemPackages::obtain` 通过 `cache.store` 完成。
- 下载返回 404 时，`package()` 会再查一次 `latest_version` 来消歧：包存在但版本错→`VersionNotFound`（附带最新版），包不存在→`NotFound`，从而给出对用户更友好的报错。
- 索引 `index()` 用 `OnceCell` 懒加载、只下载一次，且以原始 `serde_json::Value` 形式缓存——这是「惰性反序列化」的前提。
- `latest_version()` 用仅含 `name`/`version` 的 `MinimalPackageInfo` 逐条解析，靠 `.filter_map(... .ok())` **跳过**无法解析的条目（如 `0.2.0-dev`），再按 `name` 过滤、用 `PackageVersion` 的 `Ord` 取 `max()`。
- 这种「逐条跳过」的惰性反序列化带来**向前兼容**：面对格式演进的注册表，老编译器仍能查到它能理解的包，而不会被单条新格式数据拖垮整个索引。

## 7. 下一步学习建议

- **若想看下载层细节**：进入 u5-1（`Downloader` trait 与 `SystemDownloader`），理解 `download` 默认实现如何基于 `stream`、以及 `SystemDownloader` 如何把 HTTP 404 翻译成 `io::ErrorKind::NotFound`——正是本讲 `package()` 依赖的契约。
- **若想看进度条如何接入**：进入 u5-2（`ProgressDownloader`），理解本讲反复提到的「key」（`PackageSpec` vs `"package index"`）如何被用来决定是否展示下载进度。
- **若想回到加载链全局**：重读 u4-1（`SystemPackages` 三级优先级），把本讲的 `UniversePackages` 作为「第三级」嵌回去，体会 data→cache→网络的整体设计。
- **延伸阅读源码**：直接对照 `lazy_deserialize_index` 测试（[src/packages.rs:L450-L501](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L450-L501)）与 `PackageVersion` 的解析（[crates/typst-syntax/src/package.rs:L432-L455](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L432-L455)），亲手验证 `0.2.0-dev` 被跳过的全过程。
