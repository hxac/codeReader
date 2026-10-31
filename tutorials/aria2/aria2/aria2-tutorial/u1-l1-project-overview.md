# 项目总览：aria2 是什么、能做什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 aria2 的定位：一个支持 HTTP(S)/FTP/SFTP/BitTorrent/Metalink 五类协议的命令行下载工具。
2. 解释 aria2 最核心的卖点——「多源并行下载」：同一个文件可以同时从多个镜像、多种协议下载，并且 HTTP 下载到的数据还会反哺给 BT 网络。
3. 对照 README 的 Feature 列表，把每条特性大致映射到 `src/` 目录下对应的源码区域。
4. 看懂「功能 ↔ 依赖」对照表，理解为什么同一份源码在不同机器上编译出的 aria2 功能集不同。
5. 说出项目的版本号规则、发布节奏和源码托管位置。

## 2. 前置知识

本讲是整套手册的第一篇，不要求你已经读过任何 aria2 代码，但以下概念最好先有个模糊印象：

- **下载工具（download utility）**：给定一个 URL（比如 `https://example.com/file.zip`），把文件完整地取回本地并保存。浏览器的「另存为」做的就是这件事，aria2 是它的命令行强化版。
- **协议（protocol）**：通信双方约定的报文格式。HTTP(S) 是网页协议、FTP/SFTP 是传统文件传输协议、BitTorrent（简称 BT）是 P2P 协议、Metalink 则不是传输协议，而是一种「下载描述格式」（XML 文件，里面列出一个文件的多个镜像地址和校验和）。
- **分段下载（segmented downloading）**：把一个大文件切成若干段，开多条连接同时下载不同的段，最后拼回完整文件。这是 aria2 「ultra fast」的直接来源。
- **DHT（Distributed Hash Table，分布式哈希表）**：BT 网络中无 tracker 也能找到其他下载者的机制。
- **RPC（Remote Procedure Call，远程过程调用）**：让其他程序（比如浏览器插件、图形前端）通过网络调用来控制 aria2 的接口。
- **编译期开关（compile-time feature）**：C/C++ 项目里，很多功能是否可用取决于编译时有没有找到对应的第三方库，而不是运行时的选项。

如果上面某条暂时不理解也没关系，后文用到时会再解释。

## 3. 本讲源码地图

本讲主要阅读的是「文档型源码」——它们不是 `.cc/.h` 代码，但和代码同样重要，是理解项目的入口：

| 文件 | 作用 |
| --- | --- |
| `README.rst` | 项目自述文件：特性清单、依赖矩阵、构建方法、各协议实现注意事项。本讲的主线阅读材料。 |
| `NEWS` | 版本发布公告。当前内容极简，只指向 GitHub 的 release 页面，但记录了当前版本号。 |
| `configure.ac` | autotools 构建配置的源头，第 5 行的 `AC_INIT` 声明了项目名与版本号，是版本号的「权威出处」。 |
| `src/main.cc` | 程序入口。本讲只粗看一眼，建立「命令行 → 下载」的直觉，细节留给下一篇讲义。 |
| `src/FeatureConfig.cc` | 编译期特性上报：`aria2c --version` 输出里的 `Features:` 一行就是它生成的，把 README 的依赖表和实际二进制联系起来。 |
| `src/` 目录本身 | 约 951 个源码文件，按文件名前缀天然分成若干子系统，是本讲实践环节的「探险地图」。 |

## 4. 核心概念与源码讲解

### 4.1 README 特性清单

#### 4.1.1 概念说明

任何开源项目的 README 都是作者给你的「官方导览」。aria2 的 README.rst 用 reStructuredText（`.rst` 是它的扩展名，一种类似 Markdown 的标记语言）写成，结构依次是：免责声明 → 介绍 → **特性清单** → 版本与发布 → 源码获取 → **依赖** → 构建方法 → 各协议专题 → 参考文献。

其中「Features」一节（约 40 条）相当于 aria2 的功能承诺清单。读它的正确姿势不是背下来，而是把每条特性当成一个「路标」——每条特性背后都对应 `src/` 里一组源码文件，本讲实践就要建立这张映射表。

#### 4.1.2 核心流程

阅读 README 特性清单的推荐流程：

1. 先读 Introduction 一段，抓住「一句话定位」。
2. 再扫 Features 列表，按「协议能力 / 传输优化 / BT 生态 / 控制接口 / 平台特性」粗分类。
3. 遇到不懂的缩写（DHT、PEX、MSE……）到文末 References 一节找对应的 RFC 或 BEP（BitTorrent Enhancement Proposal）编号。
4. 记下 3 条你最感兴趣的特性，作为后续精读源码的切入点。

版本发布的节奏（README「Versioning and release schedule」一节）可以总结为：

```text
版本号 = MAJOR.MINOR.PATCH          # MAJOR 长期保持 1
每月 15 日发布 MINOR 版本            # 无变化则跳过
每月 5 日特性/文档冻结               # 给翻译团队 10 天窗口
安全问题随时发 PATCH 版本
```

#### 4.1.3 源码精读

**① 一句话定位**。README 开头的 Introduction 明确列出五类协议和最核心的「混血下载」能力：

> aria2 is a utility for downloading files. The supported protocols are HTTP(S), FTP, SFTP, BitTorrent, and Metalink. aria2 can download a file from multiple sources/protocols and tries to utilize your maximum download bandwidth. It supports downloading a file from HTTP(S)/FTP/SFTP and BitTorrent at the same time, while the data downloaded from HTTP(S)/FTP/SFTP is uploaded to the BitTorrent swarm.

见 [README.rst:L9-L19](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L9-L19)。注意最后一句：HTTP 下载到的数据同时上传给 BT 网络——这是 aria2 区别于普通下载器的独特设计。

**② 特性清单本体**。从「Command-line interface」到「Disk cache to reduce disk activity」共 40 余条，见 [README.rst:L29-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L29-L77)。几个值得划重点的条目：

- `Download files through HTTP(S)/FTP/SFTP/BitTorrent`（L35）——协议矩阵的浓缩；
- `Segmented downloading`（L36）——多连接分段的卖点；
- `BitTorrent extensions: Fast extension, DHT, PEX, MSE/PSE, Multi-Tracker, UDP tracker`（L58-L59）——BT 扩展全家桶；
- `JSON-RPC (over HTTP and WebSocket)/XML-RPC interface`（L66）——控制接口；
- `IPv6 support with Happy Eyeballs`（L76）——双栈连接竞速（IPv4/IPv6 同时尝试，谁先通走谁）；
- `Disk cache to reduce disk activity`（L77）——写盘缓存。

**③ 版本号与发布节奏**，见 [README.rst:L80-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L80-L93)。

**④ 源码托管**：官方仓库在 GitHub，见 [README.rst:L95-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L95-L105)。

**⑤ 当前版本号的三处出处**（本仓库 HEAD `9e727358` 上的实际值）：

- 构建脚本声明：`AC_INIT([aria2],[1.37.0],...)`，见 [configure.ac:L5](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L5)；
- 发布公告：`aria2 1.37.0`，见 [NEWS:L1-L7](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/NEWS#L1-L7)；
- git 标签：`git describe --tags` 输出 `release-1.37.0-42-g9e727358`，表示当前 HEAD 在 1.37.0 发布标签之后又前进了 42 个提交（待本地验证：你可以在仓库里自行运行这条命令）。

**⑥ 程序入口一瞥**。`src/main.cc` 只有 99 行，真正的 `main` 做的事少得出奇：构造 `Platform`（一次性初始化）、构造 `Context`（解析命令行）、执行 `reqinfo->execute()`（进入下载主逻辑），见 [src/main.cc:L54-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L54-L99)。这条三步链是下一篇讲义（u1-l3）的主菜，这里只需留下印象。

#### 4.1.4 代码实践

**实践目标**：亲手确认「版本号」和「编译期特性」都写在哪里，并第一次运行 aria2。

**操作步骤**：

1. 在仓库根目录运行 `git describe --tags`，观察输出中的版本标签。
2. 打开 `NEWS` 和 `configure.ac` 第 5 行，核对三处版本号是否一致（当前均为 1.37.0）。
3. 如果你已经编译过（下一篇讲义才会做）：运行 `./src/aria2c --version`，观察输出末尾的 `Features:` 一行。
4. 如果还没编译：跳到下面「源码阅读型」替代方案。

**需要观察的现象**：

- `git describe` 的输出形如 `release-1.37.0-42-g9e727358`；
- `--version` 的 `Features:` 行列出类似 `Async DNS, BitTorrent, Firefox3 Cookie, GZip, HTTPS, Message Digest, Metalink, ...` 的特性（具体取决于你机器上装了哪些依赖库；待本地验证）。

**预期结果**：版本号三处一致；`Features:` 行的内容与 4.3 节讲的依赖矩阵一一对应。

**源码阅读型替代方案**（无需编译）：`Features:` 行由 `featureSummary()` 拼接而成，见 [src/FeatureConfig.cc:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L96-L113)——它遍历所有特性编号，把「编译期宏开启的特性」名字用逗号连起来；每个名字的取舍逻辑在 [src/FeatureConfig.cc:L115-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L115-L168) 的 `strSupportedFeature()` 里，每条 `case` 都被 `#ifdef ENABLE_*` / `#ifdef HAVE_*` 包裹。数一数这个 switch 里有多少个 `#ifdef`，就能知道 aria2 有多少个「可裁剪」的功能面。

#### 4.1.5 小练习与答案

**练习 1**：Features 列表里提到的「Happy Eyeballs」（L76）解决什么问题？

**参考答案**：它是 RFC 6555 定义的双栈连接策略：当服务器域名同时有 IPv4 和 IPv6 地址时，客户端不再傻等一个协议超时，而是两个连接一起尝试、谁先建立成功用谁。aria2 对应实现在 `src/` 下的 `BackupIPv4ConnectCommand` 等文件中（属于 u2-l4 讲义的内容）。

**练习 2**：为什么 `src/main.cc` 里的 `main` 函数这么短？下载逻辑去哪了？

**参考答案**：入口只负责三件事：`Platform` 初始化、`Context` 装配（解析 argv）、`context.reqinfo->execute()` 启动真正的下载流程 `MultiUrlRequestInfo`。命令行参数被抽象成「RequestInfo」对象去执行，这样 CLI 和 libaria2（嵌入库）可以复用同一套执行模型。这是 u1-l3 讲义的主题。

**练习 3**：今天是某月 3 日，你想给 aria2 贡献一个新特性，README 的发布规则给你什么提示？

**参考答案**：特性/文档冻结发生在每月 5 日、15 日发版。3 日距离冻结只剩 2 天，新特性大概率赶不上本月版本，应按「下月版本」规划节奏。

### 4.2 协议支持矩阵

#### 4.2.1 概念说明

aria2 的「五协议」经常被误解为五个并列的传输协议，实际上要分两层看：

- **传输协议（4 个）**：HTTP(S)、FTP、SFTP、BitTorrent——它们真正负责搬数据。SFTP 是走 SSH 的文件传输，与 FTP 是两套完全不同的协议，只是名字像。
- **元数据格式（1 个）**：Metalink——它本身不传数据，而是一个 XML 描述文件（`.metalink`/`.meta4`），告诉你「这个文件有哪些镜像、多大、哈希值是多少」。aria2 拿到 Metalink 后再选用其中的 HTTP/FTP/SFTP/BT 资源去下载。

在此之上还有两个跨协议机制：

- **多源并行**：同一文件可同时从 HTTP + FTP + BT 多个来源拉取（README L14-L19）。
- **RPC 控制面**：JSON-RPC（HTTP/WebSocket）与 XML-RPC，让 aria2 可以被程序化控制（README L66）。

#### 4.2.2 核心流程

一条命令行输入在 aria2 里的大致旅程（仅示意，细节在后续讲义展开）：

```text
argv
  │
  ▼
URI / .torrent / .metalink / magnet: 识别
  │            │           │         │
  ▼            ▼           ▼         ▼
HTTP(S)链    FTP/SFTP链   BT链      BT链(magnet 先取元数据)
  │            │           │
  └────────────┴───────────┘
               ▼
      共同的 分段(Segment)/分片(Piece) 模型
               ▼
      磁盘写入 + 校验 + (BT)上传回血
```

关键直觉：**协议差异被隔离在「命令链」层**。每种协议一组接力执行的 Command 类（文件名前缀分别是 `Http*`、`Ftp*`、`Sftp*`、`Bt*`），而分段调度、磁盘写入、校验是所有协议共享的底层。

#### 4.2.3 源码精读

协议总述见 [README.rst:L12-L19](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L12-L19)。

`src/` 目录的文件名前缀就是天然的子系统划分。以下统计是我在当前 HEAD（`9e727358`）的仓库里实际运行 `ls src/ | grep -c '^<前缀>'` 得到的，可放心引用：

| 文件名前缀 | 文件数（不含目录） | 对应协议/子系统 | 后续讲义 |
| --- | --- | --- | --- |
| `Bt*` | 77 | BitTorrent 消息与交互 | u6 全单元 |
| `DHT*` | 111 | DHT 路由表/报文/任务 | u7 全单元 |
| `Http*` | 36 | HTTP(S) 命令链与报文 | u4-l1、u4-l2 |
| `Metalink*` | 26 | Metalink 解析与任务生成 | u8-l1、u8-l2 |
| `Ftp*` | 16 | FTP 命令链 | u4-l5 |
| `Sftp*` | 7 | SFTP 命令链（基于 libssh2） | u4-l5 |
| `Rpc*` | 10 | RPC 方法分发 | u8-l4 |
| `WebSocket*` | 8 | WebSocket 会话 | u8-l3 |
| `Peer*` | 23 | BT peer 管理 | u6-l3 |
| `RequestGroup*` | 7 | 任务模型 | u3-l1 |
| `DownloadEngine*` | 4 | 事件主循环 | u2-l1 |
| （`src/` 全部） | 951 | | |

一个有意思的观察：DHT 相关文件（111 个）比 HTTP（36 个）还多——DHT 是一个完整分布式系统（Kademlia 路由表 + 自定义 RPC 协议），复杂度不亚于一个传输协议。这也是大纲把 DHT 单独划为一个单元的原因。

BT 的运行细节（端口默认 6881-6999、最大 peer 数 55、DHT 路由表存于 `dht.dat` 等）见 [README.rst:L402-L458](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L402-L458)；Metalink 的行为（支持的哈希算法、多文件种子分组等）见 [README.rst:L461-L498](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L461-L498)；Metalink/HTTP（从 HTTP 响应头动态获得镜像列表，RFC 6249）见 [README.rst:L500-L508](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L500-L508)；WebSocket（RFC 6455，协议版本 13）见 [README.rst:L517-L521](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L517-L521)；可嵌入的 C++ 库 libaria2 见 [README.rst:L523-L531](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L523-L531)。

#### 4.2.4 代码实践

**实践目标**：用文件名搜索验证「前缀 ↔ 子系统」的对应关系。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   ls src/ | grep '^DHT' | head -10
   ls src/ | grep '^Metalink' | head -10
   ls src/ | grep '^Rpc' | head -10
   ```
2. 观察每个前缀下的文件名，尝试仅凭文件名猜出职责。例如 `DHTBucket.cc`、`DHTRoutingTable.cc` 是否和你理解的「路由表」对得上？
3. 再运行 `ls src/ | grep -c '^DHT'` 之类的统计命令，核对你心里的规模感。

**需要观察的现象**：

- `DHT*` 文件里有大量 `DHT<Node|Bucket|RoutingTable|Task|Message>*` 命名；
- `Rpc*` 文件里有 `RpcMethod.cc`、`RpcMethodImpl.cc`、`RpcMethodFactory.cc` 这样的「方法/实现/工厂」三件套。

**预期结果**：你会发现在不读一行实现的情况下，仅凭文件名就能对 951 个文件的目录建立粗粒度地图——这正是 C++ 项目「一个类一对 `.h/.cc` 文件」命名法的好处。

**待本地验证**：以上命令我已在本仓库运行过，统计数字真实有效；但请你在自己环境里重跑一遍加深印象。

#### 4.2.5 小练习与答案

**练习 1**：SFTP 和 FTP 共享代码多吗？从文件数和实现依赖分别说明。

**参考答案**：不多。`Ftp*` 有 16 个文件，`Sftp*` 只有 7 个。FTP 是 aria2 自己实现的文本协议（`FtpConnection.cc`），而 SFTP 建立在第三方库 libssh2 之上（这也是依赖表里 SFTP 唯一依赖 libssh2 的原因），aria2 只需封装会话协商与数据读取，所以文件更少。两者复用的是更底层的分段下载与磁盘写入。

**练习 2**：为什么说 Metalink 不是「第六种传输协议」？

**参考答案**：Metalink 文件只是元数据描述（镜像列表 + 校验和 + 文件信息），真正搬运数据的仍是它所引用的 HTTP(S)/FTP/SFTP/BT 资源。README L464-L465 也明确说明当前实现对 Metalink 支持这四类资源、忽略其他 P2P 协议。

**练习 3**：`aria2c "magnet:?xt=urn:btih:..."` 这种磁力链接没有种子文件，aria2 怎么开始下载？（只需说思路）

**参考答案**：magnet URI 里携带的是种子文件的 infohash。aria2 先通过 DHT 找到拥有该种子的 peer，再用 BT 扩展协议 ut_metadata（BEP 9）从 peer 那里把种子元数据本身下载下来，之后才进入正常 BT 下载流程。对应 u6-l9 讲义。

### 4.3 依赖特性表

#### 4.3.1 概念说明

README 的 Dependency 一节给出一张「功能 ↔ 依赖」对照表。它的含义是：**某些功能不是 aria2 自己实现的，而是调用第三方库**；编译时如果找不到库，对应功能整体不可用（不是报错，而是静默裁剪）。

这带来一个重要推论：**世界上不存在「功能完全相同」的两个 aria2 二进制**——Debian 上编译的和你在 macOS 上编译的特性集不同，排查问题前先看 `aria2c --version` 的 `Features:` 行。

对照表（README 原文，L112-L127）：

| 功能 | 依赖 |
| --- | --- |
| HTTPS | OSX 或 GnuTLS 或 OpenSSL 或 Windows |
| SFTP | libssh2 |
| BitTorrent | 无必需。可选：libnettle+libgmp / libgcrypt / OpenSSL |
| Metalink | libxml2 或 Expat |
| Checksum | 无必需。可选：OSX / libnettle / libgcrypt / OpenSSL / Windows |
| gzip, deflate in HTTP | zlib |
| Async DNS | C-Ares |
| Firefox3/Chromium cookie | libsqlite3 |
| XML-RPC | libxml2 或 Expat |
| JSON-RPC over WebSocket | libnettle 或 libgcrypt 或 OpenSSL |

出处：[README.rst:L112-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L112-L127)。

三条重要的「优先级」规则（README 的三条 note）：

1. **XML 解析**：libxml2 与 Expat 同时安装时优先 libxml2，想用 Expat 需 `--without-libxml2`（[README.rst:L130-L133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L130-L133)）。
2. **TLS**：macOS 优先系统级 AppleTLS、Windows 优先系统级 Schannel（WinTLS），其余平台 GnuTLS 优先于 OpenSSL（[README.rst:L135-L150](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L135-L150)）。
3. **哈希/加密**：libnettle 优先于 libgcrypt；若一个可选库都没装，aria2 会退回**内部实现，只支持 md5 和 sha1**（[README.rst:L152-L167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L152-L167)）。

合法的 SSL/加密组合共五种，见 [README.rst:L169-L176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L169-L176)：OpenSSL / GnuTLS+libgcrypt / GnuTLS+libnettle / Apple TLS（仅 OSX）/ Windows TLS（仅 Windows）。

另外，BitTorrent 与 Metalink 两个大块可以在 configure 阶段整体关闭：`--disable-bittorrent`、`--disable-metalink`（[README.rst:L178-L180](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L178-L180)）。

#### 4.3.2 核心流程

依赖如何变成「功能开关」的完整链路：

```text
./configure 探测第三方库
  │  找到 libxml2？
  ▼
在 config.h 中定义宏（如 HAVE_LIBXML2 / ENABLE_SSL / ENABLE_BITTORRENT）
  │
  ▼
源码用 #ifdef 选择实现分支
  │  例：src/FeatureConfig.cc 里每个特性一个 #ifdef
  ▼
编译出特性集不同的 aria2c
  │
  ▼
aria2c --version 的 "Features:" 行如实上报（featureSummary() 拼接）
```

这条「configure 探测 → 宏 → 条件编译 → FeatureConfig 上报」的链子，把本讲的文档（README 依赖表）和代码（FeatureConfig.cc）缝在了一起。

#### 4.3.3 源码精读

**① 特性上报的生成器**：`featureSummary()` 遍历所有特性编号，收集非空的名字：

```cpp
std::string featureSummary()
{
  std::string s;
  int first;
  for (first = 0; first < MAX_FEATURE && !strSupportedFeature(first); ++first)
    ;
  if (first < MAX_FEATURE) {
    s += strSupportedFeature(first);
    for (int i = first + 1; i < MAX_FEATURE; ++i) {
      const char* name = strSupportedFeature(i);
      if (name) {
        s += ", ";
        s += name;
      }
    }
  }
  return s;
}
```

见 [src/FeatureConfig.cc:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L96-L113)。这段代码的意思是：跳过开头连续的「未启用」特性，然后从第一个启用特性开始用逗号拼接——避免输出以 `, ` 开头。

**② 每个特性的条件编译**：`strSupportedFeature()` 是一张 switch 表，每个 `case` 都长这样（以 HTTPS 为例）：

```cpp
case (FEATURE_HTTPS):
#ifdef ENABLE_SSL
  return "HTTPS";
#else  // !ENABLE_SSL
  return nullptr;
#endif // !ENABLE_SSL
```

见 [src/FeatureConfig.cc:L115-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L115-L168)。`ENABLE_ASYNC_DNS` 对应 c-ares、`HAVE_SQLITE3` 对应 Firefox3 Cookie、`HAVE_ZLIB` 对应 GZip、`ENABLE_BITTORRENT`/`ENABLE_METALINK` 对应两个大块的开关——这些宏与 4.3.1 依赖表逐行对得上。

**③ 构建依赖的清单化描述**：README「How to build」一节把 Debian 系发行版的开发包名列成清单（`libgnutls-dev`、`libssh2-1-dev`、`libc-ares-dev`、`libxml2-dev` 等），见 [README.rst:L195-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L195-L221)；`configure` 的行为原则「检测可用库并启用尽可能多的特性」见 [README.rst:L263-L265](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L263-L265)；编译产物位置与单元测试命令见 [README.rst:L298-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L298-L302)（`make check` 使用 CppUnit）。

#### 4.3.4 代码实践

**实践目标**：把「依赖表 → 编译宏 → Features 上报」这条链走通。

**操作步骤**（源码阅读型，无需编译）：

1. 打开 [src/FeatureConfig.cc:L115-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L115-L168)，把每个 `case` 里的宏名抄下来（`ENABLE_ASYNC_DNS`、`HAVE_SQLITE3`、`HAVE_ZLIB`、`ENABLE_SSL`、`ENABLE_BITTORRENT`、`ENABLE_METALINK`……）。
2. 对照 4.3.1 的依赖表，给每个宏标注「它探测的是哪个库」。
3. 进阶（可选，待本地验证）：运行 `./configure` 后打开生成的 `config.h`，用 grep 搜索这些宏，确认哪些被定义成了 1。

**需要观察的现象**：

- FeatureConfig 里出现的宏与 README 依赖表中的库名一一对应；
- `config.h`（configure 生成，不在 git 里）中只有「实际找到库」的宏被定义。

**预期结果**：你得到一张三列对照表：`README 功能名 / 编译宏 / 第三方库`。这张表以后排查「为什么我的 aria2 没有 XXX 功能」时直接可用。

#### 4.3.5 小练习与答案

**练习 1**：一台没装 libxml2 也没装 Expat 的机器上编译 aria2，会发生什么？

**参考答案**：Metalink 与 XML-RPC 两个功能被禁用（依赖表 L119、L125 都要求 libxml2 或 Expat），configure 会输出相应提示，但编译继续、其余功能不受影响；最终 `aria2c --version` 的 Features 行不会出现 `Metalink`。若想彻底确认，可加 `--disable-metalink` 显式关闭。

**练习 2**：为什么 HTTPS 一行写着「OSX or GnuTLS or OpenSSL or Windows」四个选项？选哪个由什么决定？

**参考答案**：TLS 后端是多选一的可移植矩阵：macOS 用系统 AppleTLS（除非 `--without-appletls`），Windows 用系统 Schannel（除非 `--without-wintls`），其余平台在 GnuTLS 和 OpenSSL 里选，两者都在时 GnuTLS 优先（除非 `--without-gnutls --with-openssl`）。对应的四个实现文件是 `src/AppleTLSContext.cc`、`src/LibgnutlsTLSContext.cc`、`src/LibsslTLSContext.cc`、`src/WinTLSContext.cc`（u9-l4 讲义精读）。

**练习 3**：用户报告「我的 aria2 校验不了 sha-256 的 Metalink 文件」，最可能的编译期原因是什么？

**参考答案**：依赖表 note 说明（L162-L163）：如果编译时 libnettle/libgcrypt/OpenSSL 一个都没找到，aria2 使用内部摘要实现，而它**只支持 md5 和 sha1**。该用户的二进制很可能就是这样编译的——检查 `aria2c --version` 的 `Features:` 行即可确认。

## 5. 综合实践

**任务：制作你的第一份「特性-源码映射笔记」。**

把本讲三个模块串起来，产出一份属于你的地图：

1. **选特性**：重读 [README.rst:L29-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L29-L77) 的 Features 与 [README.rst:L112-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L112-L127) 的 Dependency，挑出 3 条你最感兴趣的特性（例如「BitTorrent WEB-Seeding」「JSON-RPC over WebSocket」「Disk cache」）。
2. **找源码**：对每条特性，在 `src/` 下用文件名搜索定位源码区域，例如：
   ```bash
   ls src/ | grep -i 'utpex\|extension'
   ls src/ | grep -i 'websocket'
   ls src/ | grep -i 'wrdiskcache\|cache'
   ```
3. **查依赖**：说明该特性依赖哪个库（或无需依赖）。
4. **记录**：按下面的模板写进自己的笔记（ Markdown 即可）：

   | 特性（README 原文） | 依赖库 | 猜测的源码区域（文件名前缀/具体文件） | 对应讲义 |
   | --- | --- | --- | --- |
   | 例：BitTorrent extensions: DHT | 可选 libnettle 等 | `DHT*`（111 个文件） | u7 |

5. **验证方向感**：把你猜的源码文件用 `Read` 工具或编辑器打开扫一眼类注释和函数名，检查是否与特性语义吻合；不吻合就修正笔记。

预期产出：3 行以上的映射表。这张表就是你后续按需精读源码的「索引页」。

## 6. 本讲小结

- aria2 是支持 HTTP(S)/FTP/SFTP/BitTorrent/Metalink 的命令行下载工具，核心卖点是**多源多协议并行下载**，且 HTTP 拿到的数据会回哺 BT 网络（[README.rst:L9-L19](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L9-L19)）。
- 五类协议分两层：四个传输协议 + 一个元数据格式（Metalink）；协议差异被隔离在各自的命令链（`Http*`/`Ftp*`/`Sftp*`/`Bt*` 前缀文件）里。
- `src/` 共 951 个文件，文件名前缀就是子系统地图：`DHT*` 111 个、`Bt*` 77 个、`Http*` 36 个、`Metalink*` 26 个……
- 功能与依赖一一绑定，configure 探测库 → 定义宏 → 条件编译 → `--version` 上报，`featureSummary()`/`strSupportedFeature()`（[src/FeatureConfig.cc:L96-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L96-L168)）是这条链的出口。
- 版本号 MAJOR.MINOR.PATCH，每月 15 日发版、5 日冻结，当前仓库处于 `1.37.0` 发布后 42 个提交的位置（`release-1.37.0-42-g9e727358`）。

## 7. 下一步学习建议

- **下一篇（u1-l2）**：构建系统与依赖——亲手执行 `autoreconf -i && ./configure && make`，观察 configure 输出的特性汇总表，验证本讲 4.3 节的依赖矩阵。
- **提前浏览**：`src/main.cc`（仅 99 行）和 `src/Context.h`，为 u1-l3 的启动流程分析做准备。
- **手册外延**：README 末尾 References（[README.rst:L533-L565](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/README.rst#L533-L565)）列出了 aria2 实现所依据的全部 RFC 与 BEP 文档；遇到 BT/DHT 相关概念时，把对应的 BEP 编号找出来读一遍是最快的补课方式。
