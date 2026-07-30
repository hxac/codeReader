# 网络下载与进度报告

## 1. 本讲目标

typst-cli 在两类场景下需要联网：下载 `@preview` 等在线包（见 u3-l2），以及 `typst update` 自更新时从 GitHub 拉取预编译二进制（见 u4-l4）。本讲只聚焦这背后的**通用下载基础设施**，学完后你应当能够：

- 说清 `download.rs` 为什么只是一个「薄适配器」：它把命令行参数（`--cert` / `TYPST_CERT`）装配成一个带 TLS 证书和 user-agent 的 `SystemDownloader`，再套上一层进度装饰器。
- 理解 `Downloader` / `ProgressDownloader` / `ProgressReporter` 三个抽象（定义在 typst-kit）如何分工，以及 CLI 如何用一个 `key → 名称` 的闭包决定「哪些下载要打印进度条」。
- 掌握 `PrintProgress` 如何借助 `terminal::out()` 的 `clear_last_line` 在终端里实现「单行刷新」的进度条，以及在非 TTY（管道/重定向）下如何自动降级。
- 能跟踪从命令行 `--cert` 一直到 stderr 上那行 `12.3 KiB / 1.2 MiB (10 %), ...` 的完整数据流。

## 2. 前置知识

本讲承接 u1-l2（入口与命令分发），你应当已经知道：

- `ARGS` 是用 `LazyLock` 惰性解析的全局命令行参数，首次被访问时才用 clap 解析一次。
- `terminal::out()` 返回一个封装了「可选彩色 stderr 输出」的 `TermOut` 句柄（singleton），诊断信息（u2-l4）和 watch 状态（u2-l5）都走这条出口。

此外需要一点背景概念：

- **TLS / CA 证书**：HTTPS 请求要验证服务器身份，靠的是一组「受信任的根证书（CA）」。企业内网或离线镜像常会用自签证书，这时需要用 `--cert` 把额外的 CA 证书喂给 HTTP 客户端，否则验证会失败。
- **user-agent**：HTTP 请求头，标识发起方。Typst 把它设成 `typst/<版本号>`，方便服务端（如包仓库）统计与识别。
- **装饰器模式（decorator）**：用一个「包装类型」在不改动原实现的前提下给它加新能力。这里的 `ProgressDownloader` 包住 `SystemDownloader`，给它加上「边下边报告进度」的能力。
- **TTY**：交互式终端。判断「是否 TTY」决定了能否用 ANSI 转义序列做光标移动、清屏等「花活」；输出被管道或重定向时通常不是 TTY，这些花活要关掉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/download.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs) | CLI 侧的下载适配器：`downloader()` 组装下载器，`PrintProgress` 实现终端进度报告。本讲主角。 |
| [src/terminal.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs) | `TermOut`：可选彩色 stderr 输出与 `clear_last_line` / `clear_screen`，被进度报告复用。 |
| [src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | 定义 `--cert` / `TYPST_CERT` 参数（`cert: Option<PathBuf>`）。 |
| [src/packages.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/packages.rs) | `system()` 把 `downloader()` 喂给 `UniversePackages`（包下载场景复用）。 |
| [src/update.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs) | 自更新场景复用 `downloader()`，用 `"release"` / `"release information"` 两种 key。 |
| `crates/typst-kit/src/downloader.rs` | 下载抽象的**真正实现**：`Downloader` trait、`SystemDownloader`（ureq + native_tls）、`ProgressDownloader`、`ProgressReader`、`Progress` 的格式化。本讲会跨 crate 引用它。 |

> 说明：前三层抽象（trait 与默认实现）住在 typst-kit，以便 Web 编辑器、语言服务器等其他前端也能复用；typst-cli 只负责「把命令行配置接进去 + 把进度画到自己的终端上」。这种「核心在 kit、薄壳在 cli」的分层与 u1-l1 讲的「编译器核心 + 薄壳 CLI」完全一致。

## 4. 核心概念与源码讲解

### 4.1 下载器的组装：`downloader()` 与 `SystemDownloader`

#### 4.1.1 概念说明

Typst 需要联网下载两类东西：在线包（`@preview/foo:1.0.0` 这类）和自更新用的 GitHub release 二进制。真正的 HTTP + TLS 代码不应该塞在 CLI 里——别的前端也要用。于是 typst-kit 提供了一个默认实现 `SystemDownloader`（基于 `ureq` + `native_tls`），CLI 只需做两件事：

1. 把用户通过 `--cert` / `TYPST_CERT` 指定的自定义 CA 证书传进去；
2. 设置一个 `typst/<版本>` 的 user-agent。

这两件事都发生在一个十几行的函数 `downloader()` 里。可以把 `downloader()` 理解成「按当前命令行配置，订制一台下载器」的工厂函数。

#### 4.1.2 核心流程

```text
命令行 / 环境变量
  ├── --cert / TYPST_CERT  ──► ARGS.cert: Option<PathBuf>
  └── typst 版本            ──► user_agent = "typst/<version>"

downloader()
  ├── 构造 SystemDownloader：
  │     • ARGS.cert 为 None  → SystemDownloader::new(user_agent)
  │     • ARGS.cert 为 Some  → SystemDownloader::with_cert_path(user_agent, cert)
  └── 用 ProgressDownloader 包一层（见 4.2），返回 impl Downloader
```

注意 `downloader()` 的返回类型是 `impl Downloader`——调用方只拿到 trait 接口，不关心内部到底是 `SystemDownloader` 还是包了进度装饰器的组合体。这正是后面「包下载」和「自更新」两个场景能复用同一份代码的关键。

#### 4.1.3 源码精读

CLI 侧的工厂函数，证书分支一目了然：

[src/download.rs:L15-L32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L15-L32) —— `downloader()`：读 `ARGS.cert` 决定走 `new` 还是 `with_cert_path`，再用 `ProgressDownloader::new` 包一层（4.2 详述）。

```rust
pub fn downloader() -> impl Downloader {
    let user_agent = format!("typst/{}", typst::utils::version().raw());
    let system = match ARGS.cert.clone() {
        None => SystemDownloader::new(user_agent),
        Some(cert) => SystemDownloader::with_cert_path(user_agent, cert),
    };
    ProgressDownloader::new(system, |key| { /* 见 4.2 */ })
}
```

`cert` 字段的定义，注意它同时绑定了命令行选项和环境变量：

[src/args.rs:L73-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L73-L75) —— `cert: Option<PathBuf>`，`#[clap(long, env = "TYPST_CERT")]`，故 `--cert <path>` 与 `TYPST_CERT=<path>` 等价。

证书真正被消费是在 typst-kit 里。`SystemDownloader` 把证书路径存起来，**惰性加载**——只有第一次真正发请求时才读 PEM 文件、解析证书：

[crates/typst-kit/src/downloader.rs:L134-L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L134-L145) —— `cert()`：用 `OnceCell` 保证证书至多加载一次；无配置返回 `None`，有配置则在首次访问时 `fs::read` + `Certificate::from_pem`。

请求发出时，证书被加进 TLS 连接器，同时还会从环境变量读取代理配置：

[crates/typst-kit/src/downloader.rs:L148-L189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L148-L189) —— `impl Downloader for SystemDownloader` 的 `stream`：设 user-agent、套用 `env_proxy` 代理、加自定义 CA、构建 native TLS、发 GET；特别地，`404` 会被翻译成 `io::ErrorKind::NotFound`（这是个约定，下游靠它区分「包不存在」和「网络故障」），并从 `Content-Length` 头取出体积提示。

> 小细节：`cert()` 在加载失败时返回 `Some(Err(_))`，而 `stream` 里是 `tls.add_root_certificate(cert?.clone())`——这个 `?` 会把加载错误**传播**成请求失败。所以「证书路径配错」并不会被悄悄忽略，而是会在第一次下载时报错。这一点和 `with_cert_path` 文档注释里的「If the certificate cannot be read, it is ignored」略有出入，留给你在 4.1.4 里亲自核对。

#### 4.1.4 代码实践

**目标**：理清「`--cert` 是怎么传到底层 HTTP 客户端的」，并核对证书加载失败时的真实行为。

**操作步骤**：

1. 从 [src/args.rs:L73-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L73-L75) 出发，跟到 [src/download.rs:L17-L20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L17-L20)，再到 typst-kit 的 `cert()` 与 `stream`，画出证书的传递路径。
2. 在终端里构造一个会触发联网的编译（需要一个未缓存的 `@preview` 包，且能访问网络）：

   ```bash
   # 清空包缓存以强制重新下载（路径视平台而定，可用 typst info 查看）
   # 然后：
   echo '#import "@preview/fletcher:0.5.3" as fletcher: #fletcher' > /tmp/dl.typ
   ./target/debug/typst compile /tmp/dl.typ /tmp/dl.pdf
   ```

3. 对照观察无网络/错误证书时的失败提示：断网后重跑，或在 `TYPST_CERT=/tmp/不存在的路径 ./target/debug/typst compile ...` 下观察报错。

**需要观察的现象**：

- 正常联网时，stderr 上先出现 `downloading @preview/fletcher:0.5.3`，随后是一行不断刷新的进度（见 4.3）。
- 断网时，应看到类似 `failed to download package (...)` 的错误。

**预期结果**：证书路径 `args.cert → downloader() → SystemDownloader::with_cert_path → cert()（惰性）→ stream() 里 add_root_certificate`。至于「证书路径配错是被忽略还是报错」，源码层面的 `?` 传播倾向于**报错**，但运行时行为受 native_tls/平台影响——明确标注「待本地验证」。

> 说明：本实践依赖网络与包仓库可达；若环境离线，重点完成第 1 步的源码跟踪即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `downloader()` 返回 `impl Downloader` 而不是具体的 `ProgressDownloader<SystemDownloader, ..., PrintProgress>`？

**参考答案**：返回 trait 接口可以隐藏内部组合类型，调用方（`packages::system`、`update::update`）只需面对 `Downloader`，不必关心是否套了进度装饰器；同时也让 typst-kit 那边接收 `&dyn Downloader` 的代码保持通用。

**练习 2**：`cert` 用了 `#[clap(long, env = "TYPST_CERT")]`，这意味着什么？

**参考答案**：用户既可以用命令行 `--cert <path>`，也可以用环境变量 `TYPST_CERT=<path>` 传入同一个值，二者汇入同一个 `cert` 字段（命令行优先级更高）。

---

### 4.2 进度装饰器：`ProgressDownloader` 与 key → 名称映射

#### 4.2.1 概念说明

`SystemDownloader` 本身只会闷头下载，不知道「终端进度条」为何物。要加进度，typst-kit 提供了 `ProgressDownloader`——一个**装饰器**：它包住任意 `Downloader`，在下载过程中周期性地调用一个 `ProgressReporter`（「进度汇报员」）。

但并非每次下载都该刷屏：下载一个几 MiB 的包要显示进度，而下载几十 KiB 的「包索引」或「release 元数据 JSON」就没必要。typst-kit 用一个巧妙设计解决这个问题——**每次下载都带一个「key」**，由调用方传入；装饰器把这个 key 交给 CLI 提供的工厂闭包，闭包返回 `Some(名字)` 就显示进度，返回 `None` 就静默。于是「要不要显示进度」这件事，被优雅地收敛到一个 `key → 名称` 的映射里。

#### 4.2.2 核心流程

```text
调用方发起 download(key, url)
        │
        ▼
ProgressDownloader::download
        │  ① self.inner.stream(key, url)  → (体积提示, reader)   ← 真正联网在底层
        │  ② progress = (self.progress)(key)  ← 调工厂闭包，由 key 决定名字/是否显示
        │  ③ ProgressReader::new(...).download()
        ▼
ProgressReader 分块读取（8 KiB/块）：
   每经过一个 period（100ms）：
     • 把这段时间下载量压入滚动窗口 samples（容量 25）
     • reporter.update(&state)   ← 回调 CLI 的 PrintProgress
   开始时 reporter.start(...)；结束时 reporter.finish(...)
```

工厂闭包里的 key 映射规则（这是 CLI 唯一的「业务判断」）：

| 调用场景 | 传入的 key | 类型 | 映射结果 | 是否显示进度 |
| --- | --- | --- | --- | --- |
| 在线包本体下载 | `&PackageSpec`（如 `@preview/foo:1.0.0`） | `PackageSpec` | `Some("@preview/foo:1.0.0")` | ✅ 显示 |
| 自更新下载二进制 | `&"release"` | `&str` | `Some("release")` | ✅ 显示 |
| 下载包索引 | `&"package index"` | `&str` | `None` | ❌ 静默 |
| 自更新下载 release 元数据 | `&"release information"` | `&str` | `None` | ❌ 静默 |

规律很清楚：**大体积的「本体」下载才打扰用户，小体积的「索引/元数据」下载保持安静**。

#### 4.2.3 源码精读

CLI 的工厂闭包——整段下载逻辑里唯一属于 CLI 的「判断」：

[src/download.rs:L22-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L22-L31) —— 对 key 做 `downcast_ref`：是 `PackageSpec` 就格式化成包名；是恰好等于 `"release"` 的字符串就显示 `"release"`；其余（含 `"package index"`、`"release information"`）落入 `None` 分支静默。

```rust
ProgressDownloader::new(system, |key| {
    let name = if let Some(spec) = key.downcast_ref::<PackageSpec>() {
        Some(eco_format!("{spec}"))
    } else if let Some(&s @ "release") = key.downcast_ref::<&str>() {
        Some(s.into())
    } else {
        None
    };
    PrintProgress(name)
})
```

`key` 的类型是 `&dyn Any`，所以这里要用 `downcast_ref::<具体类型>()` 在运行时把它「认」出来。这正是 typst-kit 模块文档里说的「All downloads are identified by a dynamic key」。

装饰器与分块读取的主体在 typst-kit：

[crates/typst-kit/src/downloader.rs:L243-L265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L243-L265) —— `impl Downloader for ProgressDownloader`：`download` 先调底层 `stream` 拿到体积提示和 reader，再用闭包造一个 reporter，交给 `ProgressReader` 边读边报。

[crates/typst-kit/src/downloader.rs:L426-L477](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L426-L477) —— `ProgressReader::download`：8 KiB 缓冲循环，每达到 `period`（默认 100ms）就把本周期下载量压入 `samples` 滚动窗口并回调 `update`；循环前后分别调用 `start` / `finish`。注释说明它参考自 rustup 的 `DownloadTracker`。

四处 key 传入点，印证上面的映射表：

- [crates/typst-kit/src/packages.rs:L367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L367) —— 在线包本体：`self.downloader.download(spec, &url)`，key 是 `PackageSpec`。其上方 L350 注释「Will invoke the downloader with the `spec` as the key」。
- [crates/typst-kit/src/packages.rs:L427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L427) —— 包索引：`self.downloader.download(&"package index", &url)`，key 是 `"package index"`。
- [src/update.rs:L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L147) —— release 元数据（小 JSON）：`downloader.download(&"release information", &url)`，静默。
- [src/update.rs:L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/update.rs#L172) —— release 二进制（大文件）：`downloader.download(&"release", ...)`，显示进度。

#### 4.2.4 代码实践

**目标**：纯源码阅读，验证「四种 key 各自是否打印进度」。

**操作步骤**：

1. 打开 [src/download.rs:L22-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L22-L31)，对照本节映射表，逐一确认四种 key 走哪个分支。
2. 分别打开 packages.rs 的 L367 / L427 与 update.rs 的 L147 / L172，确认调用方确实传了对应 key。

**需要观察的现象**：`"release information"` 虽然也满足 `&str`，但因为字符串字面量模式要求**恰好等于** `"release"`，它落入 `None` 分支。

**预期结果**：四条映射与上表完全一致；由此可推断——`typst update` 时，先静默下载 release 元数据 JSON，匹配到资产后才对二进制本体显示进度条。

#### 4.2.5 小练习与答案

**练习 1**：为什么把「是否显示进度」交给一个 `&dyn Any` 的 key，而不是给 `Downloader` 加一个 `silent: bool` 参数？

**参考答案**：key 同时携带了「这是什么下载」的语义（是哪个包？是 release 吗？），CLI 既能据此决定显示与否，还能把名字（`@preview/foo:1.0.0`）打印出来；而 `bool` 只能表达「显/不显」，丢了语义。key 用 `dyn Any` 则让 trait 定义保持中立——typst-kit 不必认识 `PackageSpec` 之外的类型。

**练习 2**：`ProgressReader` 的 `samples` 是个容量 25 的滚动窗口，作用是什么？

**参考答案**：保存最近 25 个周期（每周期 100ms，即最近约 2.5 秒）各自的下载量，用来算**平滑后的平均速度**，避免单次瞬时速度抖动让进度条上的 `xx KiB/s` 跳来跳去。

---

### 4.3 终端进度渲染：`PrintProgress` 与 `TermOut`

#### 4.3.1 概念说明

`ProgressReporter` 是 typst-kit 定义的接口（`start` / `update` / `finish` 三个回调），它本身不知道往哪儿写。CLI 的实现就是 `PrintProgress`——它把进度画到终端上。核心技巧是「单行刷新」：

- `start` 先打印一行 `downloading <名字>` 和一个空行；
- 之后每次 `update` 都先用 ANSI 转义**擦掉上一行**，再重新打印当前的 `已下载 / 总量 (xx %), 速度, ETA`。

只要 `update` 调用得足够频繁（每 100ms 一次），肉眼看起来就是一行在不断更新的进度条。

这个「擦上一行」的能力来自 `terminal::out()` 的 `clear_last_line`，它和诊断输出（u2-l4）、watch 状态（u2-l5）用的是**同一个** `TermOut` singleton——所以进度、诊断、watch 状态都写到同一条 stderr 上，颜色策略也一致。

#### 4.3.2 核心流程

`PrintProgress` 的三个回调：

```text
start(progress):
  若有名字：
    彩色打印 "downloading"，再打印 " <名字>\n\n"
  update(progress)            ← 复用下面的更新逻辑

update(progress):
  若有名字：
    clear_last_line()          ← 擦掉上一行（仅 TTY 生效）
    writeln!("{progress}")     ← 打印格式化后的进度行

finish(progress):
  update(progress)             ← 最后再刷新一次，定格最终状态
```

`clear_last_line` 的实现是两条 ANSI 转义（写入底层 stream）：

- `\x1B[1F`：光标上移一行；
- `\x1B[0J`：从光标清到屏幕末尾。

合起来就是「回到上一行开头，把那一行抹掉」。关键守卫：**只有 `supports_color()` 为真（即真 TTY）时才写转义**；被管道/重定向时这俩方法是空操作（no-op）。

`{progress}` 这个字符串长什么样，由 typst-kit 的 `Progress` 的 `Display` 决定，核心是几段下载统计：

- 平均速度（基于 4.2 的滚动窗口）：`bytes_per_sec = bytes_per_period × frequency`，其中 frequency = 1s / period，period=100ms 时 frequency=10；
- 当服务器给了 `Content-Length` 时还显示百分比与 ETA：

\[
\text{ETA} = \text{period} \times \left\lfloor \frac{\text{content\_len} - \text{downloaded}}{\text{bytes\_per\_period}} \right\rfloor
\]

- 没给 `Content-Length` 时只显示 `已下载, 速度`，不显示百分比和 ETA。

最终形如 `1.2 MiB / 12.3 MiB ( 10 %), 1.1 MiB/s, ETA: 10 s`。

#### 4.3.3 源码精读

`PrintProgress` 的全貌——非常薄，真正的活都在 `update`：

[src/download.rs:L36-L63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L36-L63) —— `struct PrintProgress(Option<EcoString>)` 持有可能的名字；`start` 用 `term::Styles::default()` 的 `header_help` 配色打印 `downloading`（复用 codespan 的配色体系，和帮助信息同色），随后 `update`/`finish` 走 `clear_last_line` + `writeln`。

```rust
fn update(&mut self, progress: &Progress) {
    if self.0.is_some() {
        let mut out = terminal::out();
        _ = out.clear_last_line();
        _ = writeln!(out, "{progress}");
    }
}
```

注意 `if self.0.is_some()`：名字为 `None`（即 4.2 里静默的下载）时，`update` 什么都不打印——这是「静默」的最终落点。

`clear_last_line` 与 `terminal::out` 的实现：

[src/terminal.rs:L38-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L38-L48) —— `clear_last_line`：守卫 `supports_color()`，写 `\x1B[1F\x1B[0J`。非 TTY 下是 no-op。

[src/terminal.rs:L10-L22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L10-L22) —— `out()` 返回指向 singleton 的 `TermOut` 句柄；它实现了 `Write` 和 `WriteColor`，所以 `writeln!` 和 `set_color` 都能直接用在它上面。

[src/terminal.rs:L80-L93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93) —— `TermOutInner::new`：颜色策略来自 `ARGS.color` 与 `stderr().is_terminal()`；底层 stream 是 `termcolor::StandardStream::stderr(...)`，即**所有进度都走 stderr**（与诊断一致，不会污染被重定向到文件的正常输出）。

进度字符串的格式化在 typst-kit：

[crates/typst-kit/src/downloader.rs:L307-L351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/downloader.rs#L307-L351) —— `Display for Progress`：按是否有 `content_len` 分两种格式，分别带或不带百分比/ETA，并用 `format_byte_unit` 把字节数换算成 `B / KiB / MiB / GiB`。

#### 4.3.4 代码实践

**目标**：理解 `PrintProgress::update` 如何用 `clear_last_line` 实现「单行刷新」，并观察 TTY 与非 TTY 下的差异。

**操作步骤**：

1. 精读 [src/download.rs:L52-L58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/download.rs#L52-L58) 的 `update`，再对照 [src/terminal.rs:L38-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L38-L48) 的 `clear_last_line`，回答：为什么这个组合能实现「一行不断更新」？
2. 触发一次联网包下载，对比「直接看终端」与「重定向到文件」两种情况：

   ```bash
   # 情况 A：直接在终端跑，应看到一行刷新的进度
   ./target/debug/typst compile /tmp/dl.typ /tmp/dl.pdf
   # 情况 B：把 stderr 重定向到文件（非 TTY）
   ./target/debug/typst compile /tmp/dl.typ /tmp/dl.pdf 2> /tmp/progress.log
   # 然后查看文件内容
   ```

**需要观察的现象**：

- 情况 A（TTY）：`downloading <包名>` 之后是一行原地刷新的进度，最后定格在 100%。
- 情况 B（非 TTY）：`clear_last_line` 退化为 no-op，于是每次 `update` 都新写一行——`/tmp/progress.log` 里会看到**多行**进度快照（每 100ms 一行），而不是一行。

**预期结果**：进度条「单行刷新」完全依赖 TTY 下的 ANSI 清行；一旦不是 TTY，逻辑不变但视觉效果变成「逐行追加」。这就是把 `clear_last_line` 用 `supports_color()` 守起来的意义——避免往文件里写一堆乱码转义。运行时现象需联网，标注「待本地验证（需要网络）」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `update` 里的 `clear_last_line()` 删掉，TTY 下会看到什么？

**参考答案**：每次 `update` 都会 `writeln` 一行新进度而不擦旧行，于是进度会**逐行向下堆叠**，每 100ms 多一行，和重定向到文件时的效果一样——失去「单行刷新」的视觉效果。

**练习 2**：为什么进度要走 stderr（`termcolor::StandardStream::stderr`）而不是 stdout？

**参考答案**：typst 的正常产物（如编译出的 PDF）可能走 stdout（`-` 输出），诊断与进度属于「带外信息」，走 stderr 才不会污染被管道传递的正常输出，也方便用户单独丢弃或记录。

**练习 3**：`PrintProgress::finish` 只是再调一次 `update`，没有做额外的「收尾打印」。这样设计够用吗？

**参考答案**：够用。`update` 本身就会把最终（100%）进度定格在屏幕上；下载完成即结束，无需额外文案。若未来想加「下载完成 ✓」之类提示，再扩展 `finish` 即可，体现了接口的可扩展性。

---

## 5. 综合实践

**任务**：触发一次真实的在线包下载，把本讲三条主线（下载器组装、进度装饰器、终端渲染）串成一张完整的数据流图。

**操作步骤**：

1. 构建并准备一个会触发 `@preview` 包下载的文档（沿用 4.1.4 的 `/tmp/dl.typ`）。
2. 运行编译，盯着 stderr 观察：`downloading @preview/...` → 进度行刷新 → 编译完成。
3. 画一张从命令行到屏幕的数据流图，标注每一步发生在哪个文件：

   ```text
   --cert / TYPST_CERT  ──(args.rs:73)──► ARGS.cert
        │
        ▼  download.rs:15  downloader()
   SystemDownloader (证书惰性加载 + user-agent + 代理)   ── typst-kit/downloader.rs:90
        │  ProgressDownloader 包装 + key→名称闭包          ── download.rs:22
        ▼
   UniversePackages.obtain → download(spec, url)          ── typst-kit/packages.rs:367
        │  ProgressReader 分块(8KiB)/100ms 采样            ── typst-kit/downloader.rs:426
        ▼  回调 reporter
   PrintProgress.start/update/finish                      ── download.rs:38
        │  clear_last_line + writeln
        ▼
   terminal::out() → TermOut → stderr                     ── terminal.rs:10
   ```

4. 变体验证（选做）：分别用 `--cert <一个PEM文件>`、断网、`2> /tmp/log`（非 TTY）三种条件重跑，对照本讲结论解释你看到的现象。

**预期结果**：能口述「`--cert` 如何变成 TLS 连接里的根证书」「为什么包本体显示进度而包索引不显示」「进度条靠什么实现单行刷新」「为什么重定向后就变成多行」。涉及运行时现象的部分若环境离线，则以完成源码流图与 4.1～4.3 的源码阅读为准，运行结果标注「待本地验证」。

## 6. 本讲小结

- `download.rs` 是一层**薄适配器**：`downloader()` 把 `ARGS.cert` 和版本号装配成一个带证书与 user-agent 的 `SystemDownloader`，再用 `ProgressDownloader` 包一层。
- 真正的 HTTP/TLS/进度采样逻辑住在 typst-kit（`SystemDownloader` 用 ureq + native_tls，`ProgressDownloader` 用 `ProgressReader` 分块采样）；CLI 只负责「配置接入」和「终端渲染」。
- 「是否显示进度」被收敛进一个 `key → 名称` 的工厂闭包：`PackageSpec` 与恰好为 `"release"` 的 key 显示进度，`"package index"` / `"release information"` 静默——大文件才打扰用户。
- `PrintProgress` 实现 `ProgressReporter`，靠 `terminal::out()` 的 `clear_last_line`（ANSI `\x1B[1F\x1B[0J`）实现单行刷新，并与诊断、watch 共用同一个 stderr singleton。
- 非 TTY 下 `clear_last_line` 退化为 no-op，进度自动变成逐行追加，不会往文件里写乱码转义；颜色与清屏统一由 `supports_color()` / `is_terminal()` 把关。
- 同一个 `downloader()` 被**包下载**（`packages::system` → `UniversePackages`）与**自更新**（`update::update` → `Release`）两个场景复用，体现了 trait 抽象的解耦价值。

## 7. 下一步学习建议

- 顺着「包」这条线，进入 **u3-l2 包存储与解析**，看 `downloader()` 喂给的 `UniversePackages` 如何与本地/缓存 `FsPackages` 组合成三级查找。
- 顺着「自更新」这条线，进入 **u4-l4 自更新机制**，看 `update.rs` 如何用 `"release"` / `"release information"` 两种 key 完成元数据获取与二进制下载、解压、self-replace。
- 想理解「进度报告用的同一个终端出口还被谁用了」，回顾 **u2-l4 诊断与终端输出** 与 **u2-l5 Watch 模式与增量重编译**，对照它们对 `terminal::out()` / `clear_screen` 的使用。
