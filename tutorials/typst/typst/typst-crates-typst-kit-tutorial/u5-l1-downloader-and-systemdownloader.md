# u5-l1 Downloader trait 与 SystemDownloader

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `Downloader` trait 为什么是 typst-kit 里**所有网络下载的唯一入口**，以及它「两层接口 + 一个契约」的设计。
- 看懂 `SystemDownloader` 这个内置实现如何用 `ureq` + `native-tls` 拼出一个最小 HTTPS 客户端：设置 user-agent、读取系统代理、应用自定义 CA 证书。
- 解释为什么 trait 文档把「404 → `io::ErrorKind::NotFound`」写成一条**契约**，以及上层（如 `UniversePackages`）如何利用这条契约区分「版本不存在」与「网络失败」。
- 自己写一个返回固定字节的自定义 `Downloader`，验证 `download()` 的默认实现确实建立在 `stream()` 之上。

## 2. 前置知识

本讲是「网络下载子系统」的第一讲，承接 [u1-l3 模块地图与 World 契约](u1-l3-modules-and-world-contract.md)。在继续前，请确认你已经了解：

- **typst-kit 的定位**：它是面向 Typst 工具集成的积木库，按 feature（特性开关）按需启用。本讲的 `Downloader` trait 本身**不依赖任何特性**，随时可用；而内置实现 `SystemDownloader` 需要 `system-downloader` 特性。
- **特性门禁**：typst-kit 奉行 `default = []`（默认全关）。`system-downloader` 这个开关会拉入 `ureq`、`native-tls`、`env_proxy`、`openssl` 这一组重型网络依赖，所以它默认关闭。
- **World 与包加载的关系**：`Downloader` 不直接出现在 `World` trait 里，它是被包加载链（u4 单元）间接使用的「底层能力」——`UniversePackages` 持有一个 `Box<dyn Downloader>` 来真正发 HTTP 请求。

几个本讲会用到的 Rust 概念，先用一句话预热：

| 概念 | 一句话解释 |
| --- | --- |
| `io::Result<T>` | 就是 `Result<T, io::Error>`，标准库的 IO 结果类型。 |
| `io::ErrorKind::NotFound` | 一种「错误种类」，表示「找不到」。它不是字符串，而是枚举，上层可以用 `err.kind() == NotFound` 精确匹配。 |
| `Box<dyn Read>` | 一个堆上的「可读字节流」，把具体 reader 类型擦除成统一接口。 |
| `&dyn Any` | 一个「动态类型」引用，调用方可以传任意类型的值进来，被调用方再用 `downcast` 取回原类型。本讲里它充当**下载的 key**。 |
| `OnceCell` | 「最多初始化一次」的同步容器，是实现**懒加载**的常用工具。 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/downloader.rs` | 本讲核心。定义 `Downloader` trait、`SystemDownloader` 实现（以及下一讲才展开的 `ProgressDownloader`/`Progress`）。 |
| `src/packages.rs` | 上层消费者。`UniversePackages` 持有 `Box<dyn Downloader>`，并在 `package()` 里消费 404 契约，是理解「为什么要这条契约」的最佳例子。 |
| `Cargo.toml` | 定义 `system-downloader` 特性及其依赖集合（`ureq`/`native-tls`/`env_proxy`/`openssl`）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`Downloader` trait**：两层接口（`stream`/`download`）、404 契约，以及 `Box`/`Arc` 的透明转发。
2. **`SystemDownloader`**：内置实现如何构建一次 HTTPS 请求。
3. **证书懒加载**：`cert()` / `with_cert_path` 的 `OnceCell` 模式。

### 4.1 Downloader trait：两层接口与 404 契约

#### 4.1.1 概念说明

typst-kit 里**凡是要从网络拿东西的地方**（下载 Typst Universe 的包、拉取包索引），都只认一个抽象：`Downloader` trait。这样做的好处是：

- **可替换**：默认实现 `SystemDownloader` 走真实 HTTPS；但在测试里、或在内网/离线环境里，你可以塞一个返回固定字节、或走自建镜像的假实现进去——上层代码完全不用改。
- **关注点分离**：trait 只负责「把 URL 变成字节」，至于字节怎么解包（`.tar.gz`）、落不落盘，那是上层 `UniversePackages`（u4-l3）和 `SystemPackages`（u4-l1）的事。

trait 只有两个方法，且它们**不是平级的**：`stream` 是必须实现的原语，`download` 是建立在 `stream` 之上的「便利方法」，自带默认实现。

#### 4.1.2 核心流程

trait 的设计可以用下面这个伪代码概括：

```
trait Downloader {
    // 必须实现：流式原语
    fn stream(key, url) -> io::Result<(Option<大小提示>, Box<dyn Read>)>;

    // 可选：自带默认实现，基于 stream
    fn download(key, url) -> io::Result<Vec<u8>> {
        let (hint, reader) = self.stream(key, url)?;   // 复用 stream
        let buf = 预分配;                                  // 有 hint 就按 hint 预分配
        reader.read_to_end(&mut buf)?;                  // 把流读成完整字节
        Ok(buf)
    }
}

// 契约：如果远端返回 HTTP 404，stream/download 应当
//       返回 err.kind() == io::ErrorKind::NotFound 的错误。
```

两个关键设计点：

1. **`download` 默认实现**：你只要实现了 `stream`，就「免费」得到一个 `download`——它先用 `stream` 拿到 reader，再 `read_to_end` 读成 `Vec<u8>`。这正是本讲实践任务要验证的事。
2. **`key: &dyn Any`**：每次下载都带一个「动态 key」。`SystemDownloader` 完全忽略它（参数名是 `_`）；它的真正读者是下一讲的 `ProgressDownloader`——进度条需要靠 key 判断「这次下载要不要显示」。例如 CLI 下载包时传 `PackageSpec` 作 key（要显示进度），但拉取包索引时传字符串 `"package index"`（不显示）。

#### 4.1.3 源码精读

trait 的文档注释里**白纸黑字写明**了 404 契约，这是整条设计链的基石：

```rust
/// Downloads resources from the network.
///
/// If the remote returns a `404` status code, the implementation should return
/// an error with [`io::ErrorKind::NotFound`].
pub trait Downloader: Send + Sync + 'static {
```

注意三件事：

- 「should return ... `NotFound`」是一条**约定**（should），而非编译器强制的规则。所以每个实现者都得自觉遵守，下游才能统一用 `err.kind() == NotFound` 来判断。
- trait bound `Send + Sync + 'static`：因为 `World` 是 `Send + Sync`，下载可能在多线程间传递；`'static` 表示实现里不能借用临时数据。
- 这是整份讲义里最重要的一句话契约——见 [src/downloader.rs:27-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L27-L34)。

`stream` 是必须实现的流式原语，返回「大小提示 + reader」二元组：

[系统下载器 trait 的 stream 方法 src/downloader.rs:37-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L37-L41) —— 它返回 `Option<usize>`（来自 `Content-Length` 头，可能没有）和一个擦除了类型的 reader。

`download` 的默认实现，**完全建立在 `stream` 之上**，并利用 hint 预分配缓冲：

[download 默认实现 src/downloader.rs:47-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L47-L55) —— 有 `Some(size)` 就 `Vec::with_capacity(size)`，没有就空 `Vec`，然后 `read_to_end`。预分配能避免反复扩容拷贝。

为了让「持有者类型」也满足 trait，代码给 `Box<T>` 和 `Arc<T>` 各写了一份**透明转发**实现：

[Box<T> 透明转发 src/downloader.rs:58-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L58-L70) 与 [Arc<T> 透明转发 src/downloader.rs:72-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L72-L84) —— 它们只是把调用转给内层 `(**self)`。这两个看似多余的实现非常关键：正因为有了它们，`Box<dyn Downloader>`、`Box<SystemDownloader>`、`Arc<SystemDownloader>` 全都满足 `Downloader`。`UniversePackages` 才能用一个 `Box<dyn Downloader>` 字段持有任意实现（见 [src/packages.rs:318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L318)）。

#### 4.1.4 代码实践

**实践目标**：亲手实现一个返回固定字节的 `Downloader`，并且**故意只实现 `stream`、不实现 `download`**，验证默认的 `download()` 确实基于 `stream()` 工作。

**操作步骤**（示例代码，需要在一个把 `typst-kit` 列为依赖的独立 Cargo 项目中运行；待本地验证）：

```rust
// 示例代码：返回固定字节的自定义 Downloader
use std::any::Any;
use std::io::{self, Cursor, Read};
use typst_kit::downloader::Downloader;

struct FixedDownloader {
    data: Vec<u8>,
}

impl Downloader for FixedDownloader {
    // 只实现 stream：返回固定字节，并用 Cursor 把 Vec 包装成 reader
    fn stream(
        &self,
        _key: &dyn Any,   // 我们的实现忽略 key
        _url: &str,
    ) -> io::Result<(Option<usize>, Box<dyn Read>)> {
        let len = self.data.len();
        Ok((Some(len), Box::new(Cursor::new(self.data.clone()))))
    }
    // 注意：这里不写 download()，刻意使用 trait 的默认实现
}

fn main() {
    let dl = FixedDownloader { data: b"hello typst".to_vec() };
    // 这里调用的是「默认实现」的 download()，它内部会先调 stream() 再 read_to_end
    let bytes = dl.download(&"fixed-key", "https://example.com/x").unwrap();
    assert_eq!(bytes, b"hello typst");
    println!("download() 默认实现工作正常，拿到 {} 字节", bytes.len());
}
```

**需要观察的现象**：

1. 由于我们没有写 `download`，能成功拿到字节，说明默认实现确实「借道」了 `stream`。
2. 断点验证：把 `stream` 的返回改成 `Err(io::ErrorKind::NotFound.into())`，再调 `download`，应得到一个 `kind()` 为 `NotFound` 的错误——这正好复现了 404 契约。

**预期结果**：打印出 `download() 默认实现工作正常，拿到 11 字节`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `download` 要设计成「可选、带默认实现」，而不是和 `stream` 一样必须实现？

> 参考答案：因为大多数实现者只需要「拿到完整字节」这个最常见的能力。让它复用 `stream` 的默认实现，既减少了样板代码，又把「流式读取」这件事统一到一个原语上。但默认实现要先把整个流读进内存，对于真正需要边下边处理（或边下边显示进度）的场景，上层可以改写 `download`——这正是下一讲 `ProgressDownloader` 干的事。

**练习 2**：如果不写 `impl Downloader for Box<T>`，`UniversePackages` 还能用 `Box<dyn Downloader>` 字段吗？

> 参考答案：能用 `Box<dyn Downloader>` 作为字段类型（这是 trait object，不依赖那个泛型 impl）。但那个泛型 impl 的真正价值在于：它让 `Box<SystemDownloader>`、`Arc<SystemDownloader>` 这类「持有具体实现」的类型本身也满足 `Downloader`，从而能被传给 `new(impl Downloader)` 这样的泛型入口。

### 4.2 SystemDownloader：最小 HTTPS 客户端如何构建一次请求

#### 4.2.1 概念说明

`SystemDownloader` 是 typst-kit 给出的**开箱即用**实现，受 `system-downloader` 特性门禁。它的定位是「一个够用的最小 HTTPS 客户端」：

- 用 `ureq`（一个轻量 HTTP 客户端）发请求；
- 用 `native-tls`（调用操作系统原生 TLS：Linux 上通常是 OpenSSL、macOS 上是 Secure Transport、Windows 上是 SChannel）建立加密通道；
- 尊重系统代理环境变量（`HTTP_PROXY`/`HTTPS_PROXY` 等），并允许注入自定义 CA 证书（企业内网常用）。

它不是一个功能完备的 HTTP 库——typst 只需要「GET 一个 URL，把 body 拿回来」这一件事，所以实现极简。

#### 4.2.2 核心流程

`SystemDownloader::stream` 每次调用都会现场拼一个 `ureq::Agent`，流程如下：

```
stream(key, url):
  1. 准备 AgentBuilder + TlsConnector::builder
  2. 设置 user-agent（标识自己是 Typst）
  3. 按当前 url 查环境变量 → 转成 ureq::Proxy → 挂到 builder（若有）
  4. 若配置了自定义证书，加进 TLS builder 的根证书
  5. 构建 TLS connector，挂到 builder
  6. builder.build().get(url).call()
       └─ 如果 ureq 返回 Status(404) → 转成 io::ErrorKind::NotFound   ← 契约落地点
       └─ 其它错误 → io::Error::other(err)
  7. 读 Content-Length 头作为大小提示
  8. 返回 (content_len, response.into_reader())
```

注意 `key` 在这里被忽略（参数写作 `_: &dyn Any`），因为真正的 HTTPS 客户端不需要靠 key 区分行为——key 只服务于进度上报。

#### 4.2.3 源码精读

结构体本身只有三个字段，其中 `cert` 用 `OnceCell` 为懒加载埋下伏笔：

[SystemDownloader 结构体 src/downloader.rs:86-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L86-L94) —— `user_agent` 是标识字符串；`cert_path` 是可选的证书文件路径；`cert` 是懒加载的证书对象。

`stream` 的开头准备两个 builder，并设置 user-agent、读取代理：

```rust
let mut builder = ureq::AgentBuilder::new();
let mut tls = TlsConnector::builder();

// Set user agent.
builder = builder.user_agent(&self.user_agent);

// Get the network proxy config from the environment and apply it.
if let Some(proxy) = env_proxy::for_url_str(url)
    .to_url()
    .and_then(|url| ureq::Proxy::new(url).ok())
{
    builder = builder.proxy(proxy);
}
```

[user-agent 与代理配置 src/downloader.rs:155-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L155-L167) —— `env_proxy::for_url_str(url)` 会按 URL 的协议（http/https）去查对应的环境变量，体现「按 URL 选择代理」。

应用证书、构建 TLS、发出请求并做 **404 映射**，是本模块最关键的一小段：

[SystemDownloader::stream 的 404 映射 src/downloader.rs:170-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L170-L181) —— 这里把 `ureq::Error::Status(404, _)` 专门挑出来，包成 `io::Error::new(io::ErrorKind::NotFound, err)`；其它所有错误（超时、DNS 失败、500 等）一律走 `io::Error::other(err)`。正是这一句，让 4.1 节那条「404 契约」落到了实处。

最后把 `Content-Length` 头解析成大小提示，并返回 reader：

[读取 Content-Length 并返回 reader src/downloader.rs:183-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L183-L188) —— 头不存在或解析失败时 `.parse().ok()` 优雅地退化成 `None`，下游（默认 `download` 或进度条）会按「未知大小」处理。

**上层如何消费这条契约**：在 `UniversePackages::package` 里，下载失败时会先用 `err.kind() == NotFound` 分流——命中 404 时再额外查一次最新版本，从而把错误细分成 `VersionNotFound`（包存在但版本写错了，附带正确版本号）和 `NotFound`（包压根不存在）：

[UniversePackages::package 消费 404 契约 src/packages.rs:367-379](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L367-L379) —— `Err(err) if err.kind() == NotFound` 这一支就是依赖 SystemDownloader 把 404 翻译成了 `NotFound`；如果是网络故障（DNS/超时/500），则落到最后的 `NetworkFailed`。请把这段和上面 downloader 的 404 映射对照看，体会「契约」如何把两层代码连起来。

#### 4.2.4 代码实践

**实践目标**：精读 404 映射那一行，并把它和上层用法串起来。

**操作步骤**（源码阅读型实践，无需编译）：

1. 打开 [src/downloader.rs:178-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L178-L181)，找到 `match err { ureq::Error::Status(404, _) => ... }`。
2. 打开 [src/packages.rs:367-379](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L367-L379)，找到 `Err(err) if err.kind() == std::io::ErrorKind::NotFound`。
3. 在脑中（或纸面上）画出数据流：`ureq 返回 404` → `SystemDownloader 包成 NotFound` → `package() 命中 NotFound 分支` → `查 latest_version` → `返回 VersionNotFound(附版本号)`。

**需要观察的现象**：两层代码**没有任何直接调用关系**，仅靠「`NotFound`」这一个错误种类就完成了协作。这就是把规则写进 trait 文档的好处。

**预期结果**：你能用自己的话回答：「为什么上层能区分『版本不存在』和『网络失败』？」——因为前者会被翻译成 `NotFound`，后者不会。

#### 4.2.5 小练习与答案

**练习 1**：如果某天 `SystemDownloader::stream` 里有人手滑把 `Status(404, _)` 改成了 `Status(410, _)`（410 Gone），上层 `UniversePackages` 的行为会变成什么样？

> 参考答案：404 不再被翻译成 `NotFound`，于是 `package()` 里 `err.kind() == NotFound` 那一支永远命中不到，所有「包/版本不存在」的情况都会滑到最后一行，被报成 `NetworkFailed`。用户看到的错误信息会从「版本不存在，最新版是 x.y.z」退化成「网络失败」。这正说明这条契约是**两个模块之间的隐性约定**，破坏它不会编译报错，却会让用户体验变差。

**练习 2**：为什么代理是用 `env_proxy::for_url_str(url)` 按 URL 查，而不是全局查一次？

> 参考答案：因为同一个进程可能既访问 `https://` 也访问 `http://`，而常见代理环境变量区分协议（`HTTPS_PROXY` vs `HTTP_PROXY`），甚至可能对特定主机走 `NO_PROXY`。按 URL 查询才能正确处理这种差异。

### 4.3 证书懒加载：cert / with_cert_path 的 OnceCell 模式

#### 4.3.1 概念说明

很多企业/内网环境用自签证书，TLS 握手会失败。`SystemDownloader` 允许注入一张额外的 CA 证书来信任这些环境。它提供了**三种构造方式**，对应证书来源的三种情况：

| 构造方法 | 证书来源 | 何时真正读取 |
| --- | --- | --- |
| `new` | 不配置证书 | —— |
| `with_cert` | 调用方直接给一个已构造好的 `Certificate` 对象 | 构造时即就绪 |
| `with_cert_path` | 给一个 PEM 文件路径 | **首次下载时才读盘**（懒加载） |

第三种是本模块的重点：**读证书文件可能失败（文件不存在、格式错），把它推迟到真正下载时再读**，可以让「构造 downloader」这一步永远成功，错误只在「真要发请求」时才暴露。

#### 4.3.2 核心流程

`cert()` 方法是懒加载的核心，用一个 `OnceCell<Certificate>` 做缓存：

```
cert() -> Option<io::Result<&Certificate>>:
  if OnceCell 已经有值（self.cert.get() 是 Some）:
      直接返回 Some(Ok(缓存值))              ← 命中快路径，不再读盘
  else if 配置了 cert_path:
      get_or_try_init：读 PEM 文件 → Certificate::from_pem
      返回 Some(Ok(...)) 或 Some(Err(...))
  else:
      返回 None（根本没配证书）
```

`OnceCell::get_or_try_init` 的语义是「如果还没初始化，就用闭包初始化一次」；闭包返回 `Err` 时，cell **保持未初始化**，下次调用会重试。这保证了：证书文件读取失败不会永久缓存为失败，也不需要每次都重复读盘成功。

#### 4.3.3 源码精读

三个构造方法，差别只在 `cert` 字段的初始状态：

[SystemDownloader 的三个构造方法 src/downloader.rs:98-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L98-L125) —— `new` 用空的 `OnceCell::new()`；`with_cert` 用 `OnceCell::with_value(cert)` 让证书立刻就绪；`with_cert_path` 只记下路径，`OnceCell` 留空。

`cert()` 的懒加载逻辑：

```rust
fn cert(&self) -> Option<io::Result<&Certificate>> {
    if let Some(cert) = self.cert.get() {
        return Some(Ok(cert));           // 命中缓存：不读盘
    }

    self.cert_path.as_ref().map(|path| {
        self.cert.get_or_try_init(|| {
            let pem = std::fs::read(path)?;
            Certificate::from_pem(&pem).map_err(io::Error::other)
        })
    })
}
```

[cert() 懒加载实现 src/downloader.rs:134-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L134-L145) —— 注意它返回的是**嵌套**类型 `Option<io::Result<...>>`：外层 `Option` 表示「有没有配证书」，内层 `Result` 表示「配了，但读取得成不成功」。

证书在 `stream` 里被消费：

[stream 中应用证书 src/downloader.rs:170-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L170-L172) —— `cert?` 把内层 `Result` 的错误用 `?` 上抛。也就是说，当 `with_cert_path` 配的证书读取失败时，错误会通过 `stream` 传回给调用方。

> ⚠️ **源码阅读提示（待你自行核对）**：`with_cert_path` 的文档注释写的是「If the certificate cannot be read, it is ignored」（无法读取则忽略）；但实际实现中 `cert()` 返回 `Some(Err(...))`，而 `stream` 里用 `cert?` 把这个错误**向上传递**了，并非忽略。这是文档注释与实现之间的细微出入——阅读源码时请以 [stream 中的 `cert?`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L170-L172) 与 [cert() 的返回类型](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L134-L145) 为准。这也是本讲 4.3.5 的练习题。

#### 4.3.4 代码实践

**实践目标**：理解 `Option<io::Result<...>>` 这个嵌套返回类型的语义。

**操作步骤**（源码阅读型实践）：

1. 阅读 [cert() src/downloader.rs:134-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L134-L145)。
2. 用一张小表列出 `cert()` 的三种可能返回，并说出每个的含义：
   - `None`：？
   - `Some(Ok(&Certificate))`：？
   - `Some(Err(...))`：？
3. 再去 [stream 里的消费处 src/downloader.rs:170-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L170-L172)，确认 `if let Some(cert) = self.cert()` 匹配的是「外层有值」，`cert?` 处理的是「内层 Result」。

**需要观察的现象**：返回类型为什么是嵌套的 `Option<Result>`，而不是扁平的 `Result<Option>`？

**预期结果**：你能解释——外层 `Option` 用来区分「没配证书（正常情况，不应报错）」与「配了证书」；只有配了证书，才需要用内层 `Result` 表达「读取成功与否」。如果用扁平 `Result<Option>`，「没配证书」和「读取出错」都得挤进 `Err`，反而丢失了「这是不是一种错误」的信息。

**说明**：若想真正验证懒加载的「首次访问才读盘」行为，需要准备一张有效的 PEM 证书并启用 `system-downloader` 特性发起真实请求，属于较重的本地环境配置，故此处标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`cert()` 为什么用 `OnceCell::get_or_try_init`，而不是构造时直接 `std::fs::read` 读好证书？

> 参考答案：为了 (1) 让构造 downloader 这一步零失败——读证书可能出错，推迟到真要下载时再读；(2) 懒加载——如果这个 downloader 实例最终没发任何请求（比如包已在缓存里），就完全不会去读那块磁盘；(3) 失败可重试——`get_or_try_init` 失败不缓存结果，下次能再试。

**练习 2**：本模块 4.3.3 末尾提到的「文档说忽略、代码却用 `?` 上抛」不一致，如果你是维护者，会倾向于「改文档」还是「改代码」？说说理由。

> 参考答案（开放题）：两种都合理。改文档（让注释符合 `?` 上抛的现实）成本最低、最不易引入回归；改代码（把 `cert?` 改成 `if let Ok(c) = cert { tls.add_root_certificate(c.clone()); }`，使其真的「忽略」）则符合「证书问题不该阻断主流程」的原意，但要确认忽略坏证书不会带来安全风险。这类取舍正是读源码时值得停下来想一想的地方。

## 5. 综合实践

把本讲三个模块串起来，完成一次「自顶向下的调用链追踪」。

**任务**：假设用户在 Typst 文档里写 `#import "@preview/charged-ieee:0.99.0"`，而这个版本不存在。请用文字（可配流程图）讲清从「发起 HTTP 请求」到「用户看到错误信息」之间，`Downloader` 契约是如何在三个层级之间传递的。

**建议步骤**：

1. **底层（`SystemDownloader::stream`）**：回顾 [404 映射 src/downloader.rs:178-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L178-L181)。Universe 服务器对不存在的版本返回 404，这里被翻译成 `io::ErrorKind::NotFound`。
2. **中层（默认 `download`）**：回顾 [download 默认实现 src/downloader.rs:47-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L47-L55)。`download` 调用 `stream`，`?` 把 `NotFound` 错误原样上抛（默认实现不改变错误种类）。
3. **上层（`UniversePackages::package`）**：回顾 [消费 404 契约 src/packages.rs:367-379](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L367-L379)。命中 `NotFound` 分支，额外查 `latest_version`，发现包其实存在、最新版是 `0.1.0`，于是返回 `VersionNotFound(spec, 0.1.0)`——最终用户会看到「这个包存在，但你要的版本不对，最新版是 0.1.0」。

**进阶（可选）**：写一个 `FakeRegistryDownloader`（仿照 4.1.4 的 `FixedDownloader`，但 `stream` 对特定 URL 返回 `Err(io::ErrorKind::NotFound.into())`），把它喂给 `UniversePackages::new`，构造一个**完全不碰真实网络**的测试场景。`packages.rs` 末尾的 `DummyDownloader` 测试就是这种思路的真实例子，见 [src/packages.rs:458-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L458-L468)——它让 `stream` 永远返回 `NotFound`，从而把测试隔离在「网络层」之外。这正是 `Downloader` trait 作为「可替换抽象」的价值。

## 6. 本讲小结

- `Downloader` 是 typst-kit 中**所有网络下载的唯一入口**，设计成两层：必须实现的 `stream`（返回大小提示 + reader）和自带默认实现的 `download`（基于 `stream` 读成 `Vec<u8>`）。
- trait 文档规定了一条**隐性契约**：远端 404 必须翻译成 `io::ErrorKind::NotFound`。这条契约不靠编译器保证，靠的是「文档 + 每个实现者自觉」。
- `Box<T>` / `Arc<T>` 的透明转发实现，让 `Box<dyn Downloader>`、`Box<SystemDownloader>` 等持有者类型都满足 trait，是 `UniversePackages` 用 `Box<dyn Downloader>` 字段的前提。
- `SystemDownloader` 是受 `system-downloader` 特性门禁的内置实现，用 `ureq` + `native-tls` 拼出最小 HTTPS 客户端，每次请求现场设置 user-agent、按 URL 查系统代理、应用自定义证书，并把 `ureq` 的 404 翻译成 `NotFound`（[src/downloader.rs:178-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L178-L181)）。
- 证书通过 `OnceCell` **懒加载**：`with_cert_path` 只记路径，首次下载时才读盘，`cert()` 用嵌套的 `Option<io::Result<...>>` 区分「没配证书」与「配了但读取失败」。
- 上层 `UniversePackages::package` 靠 `err.kind() == NotFound` 区分「版本不存在」（→`VersionNotFound`，附最新版）与「网络失败」（→`NetworkFailed`），是这条契约最典型的消费者。

## 7. 下一步学习建议

- **下一讲 [u5-l2 进度下载：ProgressDownloader 与 ProgressReader](u5-l2-progress-downloader.md)**：本讲里那个一直被忽略的 `key: &dyn Any` 参数终于登场——`ProgressDownloader` 包裹内层 downloader，靠 `key` 决定要不要显示进度条，并用 `ProgressReader` 分块读取、周期回调。建议顺读。
- **回顾 u4 单元**：本讲多次引用 `UniversePackages`。若想完整理解「下载的字节如何被解包成 `.tar.gz`、如何落盘」，建议重读 [u4-l1 SystemPackages 与三级加载链](u4-l1-systempackages-priority-chain.md) 与 [u4-l3 UniversePackages 与包索引](u4-l3-universepackages-and-index.md)，把「下载 → 解包 → 存储」整条链补齐。
- **延伸阅读**：若对底层网络栈好奇，可对照 `Cargo.toml` 里 `system-downloader` 拉入的依赖（`ureq`、`native-tls`、`env_proxy`）阅读各自文档，理解「最小 HTTPS 客户端」每个零件的职责。
