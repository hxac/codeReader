# u9-l4 平台抽象与 TLS 多后端矩阵

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TLSContext` 与 `TLSSession` 两个抽象类各自的职责边界，以及「一个进程里到底会有几个 TLS 上下文、几个 TLS 会话」。
2. 解释 `TLS_ERR_OK / TLS_ERR_ERROR / TLS_ERR_WOULDBLOCK` 三值返回约定如何让 TLS 这种「阻塞式状态机」融入 aria2 的非阻塞事件循环。
3. 对比 OpenSSL、GnuTLS、AppleTLS（Secure Transport）、WinTLS（Schannel）四个后端在证书加载、信任库、证书校验位置上的差异。
4. 通过 `aria2c --version` 的 Enabled Features 与 Libraries 两行输出，反推当前二进制是用哪个 configure 配置、哪个 TLS 后端编译出来的。
5. 完成「自签名证书 + `--check-certificate=true/false`」对照实验，并在握手验证分支处加日志观察两条路径的分叉点。

## 2. 前置知识

本讲默认你已读过 u4-l1（HTTP 下载命令链）与 u4-l2（HttpConnection 报文解析），知道 TLS 握手发生在 `HttpRequestCommand` 建连之后、发请求之前。在此之上，补充几个 TLS 侧的概念：

- **X.509 证书与信任链**：HTTPS 站点出示一张证书（含域名 SAN/CN、公钥、有效期），浏览器/客户端需要沿「站点证书 → 中间 CA → 根 CA」逐级验签。根 CA 存放在**系统信任库**里（Linux 常见路径是 `/etc/ssl/certs`，Windows 是证书存储，macOS 是 KeyChain）。
- **PEM 与 PKCS12**：两种把「证书 + 私钥」打包成文件的格式。PEM 是 Base64 文本（`-----BEGIN CERTIFICATE-----`），证书和私钥通常分成两个文件；PKCS12（`.p12`/`.pfx`）是二进制格式，把证书、私钥、中间链打成一个文件，通常带口令。aria2 作为 RPC 服务端（`--rpc-certificate`）或客户端双向认证（`--certificate`）时都要加载它们。
- **SNI（Server Name Indication）**：客户端在握手第一个包里明文写出要访问的主机名，让同一 IP 上托管多个证书的服务器知道该出示哪张。这就是 `TLSSession::setSNIHostname` 存在的原因。
- **非阻塞 socket 上的 TLS**：TLS 握手和记录读写内部需要多次网络往返。socket 是非阻塞的，底层库每次「想读但没数据 / 想写但缓冲满」时都会返回一个「稍后再试」信号（OpenSSL 的 `SSL_ERROR_WANT_READ/WRITE`、GnuTLS 的 `GNUTLS_E_AGAIN`）。上层必须据此向 EventPoll 注册读或写事件，等事件到来再重试——这正是 u2-l4 讲过的 `wantRead_`/`wantWrite_` 机制在 TLS 层的翻版。
- **编译期单选（compile-time single choice）**：C++ 没有运行时接口注册的惯例设施，aria2 的做法是让四个后端各自定义同名的工厂函数 `TLSContext::make` / `TLSSession::make`，由构建系统保证**恰好只有一个后端的源文件被编译**。这是理解整个 TLS 模块的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/TLSContext.h` | TLS 上下文抽象接口：证书加载、信任库加载、verifyPeer 开关 |
| `src/TLSSession.h` | TLS 会话抽象接口：握手、读写、SNI、三值错误约定 |
| `src/LibsslTLSContext.cc` / `LibsslTLSSession.cc` | OpenSSL 后端实现 |
| `src/LibgnutlsTLSContext.cc` / `LibgnutlsTLSSession.cc` | GnuTLS 后端实现 |
| `src/AppleTLSContext.cc` / `AppleTLSSession.cc` | macOS Secure Transport 后端（KeyChain） |
| `src/WinTLSContext.cc` / `WinTLSSession.cc` | Windows Schannel 后端 |
| `src/SocketCore.cc` | TLS 会话的宿主：`tlsHandshake` 状态机把抽象接口接入命令链 |
| `src/MultiUrlRequestInfo.cc` | TLS 上下文的装配点：客户端与 RPC 服务端各一个 |
| `src/FeatureConfig.cc` / `.h` | 编译期特性上报：`--version` 输出的数据来源 |
| `src/version_usage.cc` | `--version` 的打印逻辑 |
| `src/a2netcompat.h` | 平台兼容层：`sock_t` 等类型差异抹平 |
| `configure.ac` | 四后端的探测顺序与互斥闸门 |
| `src/Makefile.am` | 按宏条件编译对应后端源文件 |

## 4. 核心概念与源码讲解

### 4.1 TLS 抽象接口：TLSContext 与 TLSSession

#### 4.1.1 概念说明

aria2 把 TLS 拆成两个生命周期完全不同的抽象：

- **`TLSContext`（上下文）——进程级、可复用的「配置袋」**。它持有证书与私钥（本端身份）、信任的 CA（如何验证对端）、以及 `verifyPeer` 开关。整个进程只有两个实例：一个客户端上下文（所有 HTTPS 下载共用）和一个服务端上下文（仅 `--enable-rpc --rpc-secure` 时创建）。
- **`TLSSession`（会话）——每连接一个的「握手与读写状态机」**。每条 TLS 连接各自创建、各自销毁，握手成功后的收发数据都走它。

两个类都是纯虚接口，`TLSSession.h` 里有一句直白的注释点明了设计意图：

> To create another SSL/TLS backend, implement TLSSession class below.

（要创建另一个 SSL/TLS 后端，实现下面的 TLSSession 类即可。）

#### 4.1.2 核心流程

一次 HTTPS 下载中 TLS 相关对象的完整协作流程：

```text
进程启动（MultiUrlRequestInfo::prepare）
  ├─ TLSContext::make(TLS_CLIENT, minTLSVer)        # 进程唯一客户端上下文
  ├─ 按选项装证书/信任库：addCredentialFile /
  │  addTrustedCACertFile / addSystemTrustedCACerts
  ├─ setVerifyPeer(--check-certificate 的值)
  └─ SocketCore::setClientTLSContext(clTlsContext)   # 存进静态成员

每条 HTTPS 连接（命令链视角）
  HttpRequestCommand（u4-l1）
    └─ SocketCore::tlsConnect(hostname)              # SocketCore.cc:910
        └─ tlsHandshake(clTlsContext_, hostname)     # 三态状态机
            ├─ A2_TLS_NONE：TLSSession::make + init(sockfd)
            │            + setSNIHostname(hostname)
            ├─ A2_TLS_HANDSHAKING：tlsSession_->tlsConnect(...)
            │   返回 WOULDBLOCK → 命令读事件后重来，直到 OK
            └─ A2_TLS_CONNECTED：此后读写全部改走 tlsSession_
```

关键约定是**三值返回**：`TLS_ERR_OK`（完成）、`TLS_ERR_ERROR`（失败）、`TLS_ERR_WOULDBLOCK`（底层阻塞，需要等事件再来）。配合 `checkDirection()` 返回 `TLS_WANT_READ` / `TLS_WANT_WRITE`，上层就知道该向 EventPoll 注册哪个方向的兴趣——这与 u2-l4 的 `wantRead_`/`wantWrite_` 完全同构。

#### 4.1.3 源码精读

**接口契约一：TLSContext**。先看两个前置枚举与类的七个纯虚方法（[src/TLSContext.h:L44-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSContext.h#L44-L72)）。`TLSSessionSide` 区分客户端/服务端角色；`TLSVersion` 表达最低协议版本（TLS1.1/1.2/1.3）；静态工厂 `make` 是唯一创建入口。注意 `addCredentialFile` 的注释：私钥文件必须是**未加密**的。

**接口契约二：TLSSession 与三值错误码**（[src/TLSSession.h:L44-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L44-L56)）定义了 `TLSErrorCode` 三值与「实现 TLSSession 即可新增后端」的注释；完整方法表在 [src/TLSSession.h:L60-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L60-L112)：`init` 绑定 socket、`tlsConnect/tlsAccept` 做两个方向的握手、`writeData/readData` 语义与非阻塞 `send/recv` 对齐（成功返回字节数，阻塞返回 `TLS_ERR_WOULDBLOCK`）、`getRecvBufferedLength` 汇报库内部已缓冲的可读字节数。

**装配点**。客户端上下文在 `MultiUrlRequestInfo::prepare` 中创建（[src/MultiUrlRequestInfo.cc:L252-L274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L252-L274)）：`--min-tls-version` 经 `util::toTLSVersion` 转成枚举（不认识的值回落 TLS1.2，见 [src/util.cc:L2471-L2483](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2471-L2483)）；`--ca-certificate` 与系统信任库是二选一关系；最后 `setVerifyPeer(PREF_CHECK_CERTIFICATE)` 把 `--check-certificate` 的值写进上下文并存入 `SocketCore` 的静态成员。服务端上下文仅在 `--rpc-secure` 时创建（[src/MultiUrlRequestInfo.cc:L183-L203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L183-L203)），必须提供 `--rpc-certificate`，且要在 `DownloadEngine` 创建**之前**塞进 `SocketCore`——因为 RPC 监听命令一出生就要用它做 `tlsAccept`。

**宿主状态机**。`SocketCore` 内部用一个三态枚举管理每连接的 TLS 进度（[src/SocketCore.cc:L119-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L119-L128)，注意值跳过了 1）。核心是 `tlsHandshake`（[src/SocketCore.cc:L915-L961](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L915-L961)）：`A2_TLS_NONE` 时创建会话、`init` 绑定 fd，并且只在「客户端 + 主机名非纯数字/IP + 含点」时设置 SNI（对 `localhost` 这类名字发 SNI 反而会招致 GnuTLS 告警，注释里写明了原因）；`A2_TLS_HANDSHAKING` 时按角色调 `tlsConnect` 或 `tlsAccept`，阻塞则整体返回 false，由命令层等事件后重入。命令链的触发点在 [src/HttpRequestCommand.cc:L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L129)：`getSocket()->tlsConnect(getRequest()->getHost())` 返回 false 就重排自己等读写事件，这正好接上 u4-l1 的握手棒次。

#### 4.1.4 代码实践

**实践目标**：不运行任何程序，仅靠源码画出「一次 HTTPS 下载中 TLS 对象的创建时序图」，验证你对抽象层与宿主层的理解。

**操作步骤**：

1. 从 [src/HttpRequestCommand.cc:L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L129) 出发，向下追 `tlsConnect` → `tlsHandshake`，标出 `TLSSession::make`、`init`、`setSNIHostname`、`tlsConnect` 各自被调用的条件分支。
2. 向上追 `SocketCore::setClientTLSContext` 的调用点（[src/MultiUrlRequestInfo.cc:L273](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L273)），确认客户端上下文在引擎创建前就已装配。
3. 用 `Grep` 搜索 `tlsSession_->readData` 与 `tlsSession_->writeData` 在 `SocketCore.cc` 中的位置，确认握手完成后普通读写如何被透明替换为 TLS 读写。

**需要观察的现象**：你画出的图里应当出现「1 个 `TLSContext`（客户端）+ N 个 `TLSSession`（每连接一个）」的数量关系，以及 WOULDBLOCK 导致同一条命令被反复重排的回环。

**预期结果**：时序图与 4.1.2 的文字流程一致；若不一致，回到源码检查你漏掉的分支。（本实践为源码阅读型，无需运行，结论可直接由源码验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TLSContext` 只有两个实例，而 `TLSSession` 每条连接一个？

**参考答案**：`TLSContext` 承载的是可跨连接复用的配置（证书、信任库、verifyPeer），创建成本高（要解析证书文件、加载整个信任库），且对所有连接语义相同；`TLSSession` 封装的是单条连接的握手状态与记录层缓冲，连接之间不可共享。这是 TLS 库的通用模式：OpenSSL 的 `SSL_CTX`/`SSL`、GnuTLS 的 credentials/session 都是同样的两级结构。

**练习 2**：`tlsConnect` 返回 `TLS_ERR_WOULDBLOCK` 后，aria2 怎么知道接下来该等读事件还是等写事件？

**参考答案**：调用 `TLSSession::checkDirection()`。以 OpenSSL 后端为例（[src/LibsslTLSSession.cc:L107-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L107-L117)），它用 `SSL_get_error` 判断上一次返回是否为 `SSL_ERROR_WANT_WRITE`，是则 `TLS_WANT_WRITE`，否则一律 `TLS_WANT_READ`。`SocketCore` 据此设置 `wantRead_`/`wantWrite_`，命令层再翻译成 EventPoll 的事件注册。

**练习 3**：`SocketCore` 的 TLS 状态枚举为什么取值 0、2、3，缺了 1？

**参考答案**：源码只给出了三个状态而没有解释缺口（[src/SocketCore.cc:L119-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L119-L128)），最可能是历史演进中删掉过一个中间状态（例如独立的「写中」态）后编号未重排。对学习者的启示是：不要从魔法数字反推语义，以枚举名为准。（此题属于「待确认」的历史细节。）

### 4.2 四个平台后端：OpenSSL / GnuTLS / AppleTLS / WinTLS

#### 4.2.1 概念说明

四个后端解决同一个问题，但哲学不同：

- **OpenSSL（Libssl\*）**：功能最全的通用后端。证书校验**不信任库的自动验证**，握手成功后由 aria2 亲手取对端证书、查验证结果、提取 SAN/CN 做主机名匹配——校验逻辑在 aria2 手里，行为跨版本最稳定。
- **GnuTLS（Libgnutls\*）**：同样是通用后端，但把证书链验证交给 `gnutls_certificate_verify_peers`，并用**目的（purpose/EKU）参数**限定「这张证书必须允许用作 TLS 服务器」（2025 年的提交 `44aa33d4` 补上了这一校验），之后再做主机名匹配。
- **AppleTLS**：macOS 原生 Secure Transport。证书**存在 KeyChain 里**，支持「按证书指纹引用 KeyChain 中的身份」这种苹果特有玩法；验证由系统 API 完成。
- **WinTLS**：Windows 原生 Schannel。证书校验用一组 `SCH_CRED_*` 标志表达（自动验证/手动验证/吊销检查），凭证只认 PFX/PKCS12。

#### 4.2.2 核心流程

每个后端都要回答同样五个问题：

```text
① 工厂怎么写？       TLSContext::make / TLSSession::make → new 各自实现类
② 本端证书怎么加载？  PEM 双文件？PKCS12 单文件？KeyChain 指纹？
③ 信任库怎么来？      系统信任库？CA bundle 文件？还是压根不支持？
④ 协议版本怎么限？    OpenSSL 用 SSL_OP_NO_*；GnuTLS 用优先级字符串；
                      Win 用 SP_PROT_* 位掩码；Apple 用 SSLSetProtocolVersion*
⑤ 对端证书怎么验？    自己验（OpenSSL）/ 库验+purpose（GnuTLS）/
                      系统验（Apple/Win）
```

问题③的答案差异最大，直接体现为 `addSystemTrustedCACerts` 与 `addTrustedCACertFile` 两个虚方法在各后端的实现取舍（见 4.2.3 的对照表）。

#### 4.2.3 源码精读

**① 工厂与「编译期单选」**。四个后端各自定义同名工厂，例如 OpenSSL 后端（[src/LibsslTLSContext.cc:L95-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L95-L98)）、GnuTLS 后端（[src/LibgnutlsTLSContext.cc:L52-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L52-L55)）、Apple 后端（[src/AppleTLSContext.cc:L179-L182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L179-L182)）、Windows 后端（[src/WinTLSContext.cc:L110-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L110-L113)）。由于符号同名，构建系统必须保证只编译一个后端——[src/Makefile.am:L325-L400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L325-L400) 用 `if HAVE_APPLETLS` / `if HAVE_WINTLS` / `if HAVE_LIBGNUTLS` / `if HAVE_OPENSSL` 四个条件块拼装：抽象头文件 `TLSContext.h TLSSession.h` 恒在编译清单里，具体实现按宏二选一（Libssl 块还叠加了 `if !HAVE_APPLETLS` 的防御性条件）。选择权在 configure：[configure.ac:L325-L430](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L325-L430) 按 **Apple → Win → GnuTLS → OpenSSL** 的固定顺序探测（四个都是 `ARIA2_ARG_WITHOUT`，默认探测，见 [configure.ac:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L55-L61)），一旦某个后端成功就置 `have_ssl=yes`，后续探测块全部被 `have_ssl != yes` 闸门挡住——首中即赢、至多一个。

**② OpenSSL 后端**。构造函数（[src/LibsslTLSContext.cc:L100-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L100-L169)）以 `SSLv23_method()` 起步（这个名字是历史遗留，实际表示「协商最高可用版本」），再用 `switch` 的**贯穿（fall-through）**把最低版本以下的所有协议逐一加 `SSL_OP_NO_*` 关掉——`TLS_PROTO_TLS13` 会连带关掉 1.2、1.1、1.0、SSLv3，以此类推；随后设 `SSL_OP_ALL`（兼容性 workaround 大礼包）、禁压缩、限密码套件为 `HIGH:!aNULL:!eNULL`，并对 1.0.x 系列手工启用 P-256 ECDH（3.x 起不再需要）。证书加载是「双路」（[src/LibsslTLSContext.cc:L175-L197](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L175-L197)）：给了 keyfile 就走 PEM（私钥文件 + 证书链文件分别加载），keyfile 为空则把 certfile 当 PKCS12 整体解析（[src/LibsslTLSContext.cc:L198-L274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L198-L274)，先读进内存再 `d2i_PKCS12_bio` + `PKCS12_parse`，口令固定为空串）。信任库两个入口（[src/LibsslTLSContext.cc:L276-L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L276-L300)）分别映射 `SSL_CTX_set_default_verify_paths`（系统路径）与 `SSL_CTX_load_verify_locations`（指定 bundle 文件）。

**③ OpenSSL 的验证分支（本讲综合实践的主战场）**。会话侧 `tlsConnect`（[src/LibsslTLSSession.cc:L224-L332](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L224-L332)）先驱动 `SSL_connect`（在 [src/LibsslTLSSession.cc:L171-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L171-L222) 的 `handshake` 中，`WANT_READ/WRITE` 翻译为 `TLS_ERR_WOULDBLOCK`，并顺带用 `SSL_version` 回填协商到的版本号）。握手成功后进入本讲最关键的一个 `if`：`side == TLS_CLIENT && getVerifyPeer()`（[src/LibsslTLSSession.cc:L234-L251](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234-L251)）——取对端证书（OpenSSL 3.0 起函数改名 `SSL_get1_peer_certificate`，代码用版本宏二选一，这是跨版本兼容的典型写法）、`SSL_get_verify_result` 非 `X509_V_OK` 即失败并把 `X509_verify_cert_error_string` 的文案写进 `handshakeErr`；随后（[src/LibsslTLSSession.cc:L252-L328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L252-L328)）解析 SAN 里的 DNS 名与 IP 地址、兜底取 CN（结尾的点会被剥掉），最后交给 `net::verifyHostname` 做主机名匹配，不匹配则报 `hostname does not match`。**换句话说：`--check-certificate=false` 时 aria2 根本不调 `SSL_get_verify_result`，自签名证书的下载就能通过。**

**④ GnuTLS 后端**。上下文构造（[src/LibgnutlsTLSContext.cc:L57-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L57-L72)）分配 certificate credentials 并放宽为允许 X509 v1 的 CA 证书；PEM 与 PKCS12 双路加载（[src/LibgnutlsTLSContext.cc:L83-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L83-L127)，PKCS12 走内存接口 `gnutls_certificate_set_x509_simple_pkcs12_mem`）。最有教学价值的是 `addSystemTrustedCACerts`（[src/LibgnutlsTLSContext.cc:L129-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L129-L146)）：`gnutls_certificate_set_x509_system_trust` 是 GnuTLS 3.3 才有的函数，configure 用 `AC_CHECK_FUNCS` 探测成 `HAVE_GNUTLS_CERTIFICATE_SET_X509_SYSTEM_TRUST`（[configure.ac:L395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L395)），老版本编译出来的 aria2 会日志提示 "System certificates not supported" 并返回 false——同一个虚方法，功能随库版本浮动。会话侧 `init`（[src/LibgnutlsTLSSession.cc:L99-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L99-L168)）展示了协议版本限定的另一种表达：优先级字符串 `"SECURE128:+SIGN-RSA-SHA1"` 再按最低版本追加 `:-VERS-TLS1.1` 之类的排除项（若 configure 启用了 `USE_GNUTLS_SYSTEM_CRYPTO_POLICY` 则直接用 `@SYSTEM`），并针对 GnuTLS 3.1.3–3.2.8 的 OCSP 非阻塞 bug 做了编译期 workaround（文件顶部 L85–L97 的注释值得一看）。验证分支（[src/LibgnutlsTLSSession.cc:L237-L301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L237-L301)）：`gnutls_handshake` 循环里 `GNUTLS_E_AGAIN` 翻译为 WOULDBLOCK；验证用 `gnutls_certificate_verify_peers` 并传入 `GNUTLS_DT_KEY_PURPOSE_OID = GNUTLS_KP_TLS_WWW_SERVER`——证书除了链有效还必须**声明可用于 TLS 服务器**（EKU/ExtendedKeyUsage 校验）；状态位逐个翻译成人话（`issuer is not known`、`expired`……）拼进 `handshakeErr`。

**⑤ AppleTLS 后端**。`addCredentialFile`（[src/AppleTLSContext.cc:L192-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L192-L209)）先当指纹试、再当 PKCS12 试，都失败则警告只支持这两种。指纹玩法（[src/AppleTLSContext.cc:L220-L315](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L220-L315)）：把参数当十六进制指纹规范化，先校验长度是否是任一支持哈希的合法长度，然后构造 KeyChain 查询（`SecPolicyCreateSSL` + `SecItemCopyMatching`）枚举全部身份，对每个身份的证书**依次用所有支持的哈希算法算摘要**与指纹比对——所以 SHA-1/SHA-256 指纹都能用。CA bundle 直接不支持（[src/AppleTLSContext.cc:L211-L216](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L211-L216)），让你去 KeyChain 里管理。会话侧把 `verifyPeer` 翻译成系统选项：10.8+ 上若不验证则设 `kSSLSessionOptionBreakOnServerAuth`（[src/AppleTLSSession.cc:L435-L443](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSSession.cc#L435-L443)），否则用 `SSLSetEnableCertVerify` 交给系统验证。

**⑥ WinTLS 后端**。构造函数（[src/WinTLSContext.cc:L70-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L70-L108)）把最低版本翻译成 `SP_PROT_TLS1_1_CLIENT/SERVER` 等位掩码，并设最小加密强度 128 位。`setVerifyPeer`（[src/WinTLSContext.cc:L128-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L128-L155)）是四后端里「验证开关」表达最特别的一个：`SCH_CRED_AUTO_CRED_VALIDATION | SCH_CRED_REVOCATION_CHECK_CHAIN` 表示让 Schannel 自动验证并查吊销；服务端或用户关掉验证时改设 `SCH_CRED_MANUAL_CRED_VALIDATION` 等一组「跳过」标志。真正的凭据句柄惰性创建（[src/WinTLSContext.cc:L157-L191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L157-L191)，`AcquireCredentialsHandleW`，且在 `setVerifyPeer` 改标志后 `cred_.reset()` 作废重建）。凭证只认 PFX（[src/WinTLSContext.cc:L193-L229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L193-L229)，`PFXIsPFXBlob` + `PFXImportCertStore`，先试空口令再试无口令），CA bundle 同样不支持（[src/WinTLSContext.cc:L231-L236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L231-L236)，改用系统信任库）。会话侧握手是 Schannel 的 `InitializeSecurityContext/AcceptSecurityContext` 状态机（[src/WinTLSSession.cc:L199-L205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L199-L205)），读写还要自己处理记录的加密解密缓冲（`checkDirection` 按内部状态 `st_handshake_read/write` 判断方向，[src/WinTLSSession.cc:L232-L241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L232-L241)），是四个后端中实现最重的一个。

**四后端能力对照表**（依据上文各处源码归纳）：

| 维度 | OpenSSL | GnuTLS | AppleTLS | WinTLS |
| --- | --- | --- | --- | --- |
| 本端证书格式 | PEM 双文件 + PKCS12 | PEM 双文件 + PKCS12 | KeyChain 指纹 + PKCS12 | 仅 PFX/PKCS12 |
| CA bundle 文件 | 支持 | 支持 | 不支持（用 KeyChain） | 不支持（用系统库） |
| 系统信任库 | 支持 | 视 GnuTLS 版本（≥3.3） | 隐含（KeyChain） | 隐含（证书存储） |
| 对端验证位置 | aria2 亲手验（verify_result + SAN/CN） | 库验 + EKU purpose，aria2 补主机名匹配 | 系统 API 验 | Schannel 标志驱动 |
| 最低版本表达 | `SSL_OP_NO_*` 位 | 优先级字符串排除项 | `SSLSetProtocolVersion*` | `SP_PROT_*` 位掩码 |
| 哈希后端联动 | OpenSSL MD | nettle/gcrypt 内部 | Apple MD（CommonCrypto） | Windows MD |

最后一行呼应 u1-l2 讲过的「依赖优先级」：`configure.ac:L520-L542` 里消息摘要后端的选择与 TLS 后端绑定——选了 AppleTLS 就用 `USE_APPLE_MD`，选了 OpenSSL 才有 `USE_OPENSSL_MD`，native TLS（Apple/Win）会排斥 nettle/gcrypt（`have_nativetls` 闸门，[configure.ac:L432-L485](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L432-L485)）。

#### 4.2.4 代码实践

**实践目标**：在一份源码里同时看清「同一虚方法的四种实现」，训练按接口读实现的能力。

**操作步骤**：

1. 打开 `TLSContext.h` 的四个虚方法声明，做一张空表（方法 × 后端）。
2. 依次打开四个 `*TLSContext.cc`，逐格填写每个方法的行为，重点标注「返回 false 并打 WARN」的格子（Apple 与 Win 的 `addTrustedCACertFile`、老版本 GnuTLS 的 `addSystemTrustedCACerts`）。
3. 追一个问题：`MultiUrlRequestInfo.cc:L261-L271` 在 `--ca-certificate` 加载失败时只打 `MSG_WARN_NO_CA_CERT` 继续跑，那么在 AppleTLS/WinTLS 上配了 `--ca-certificate` 会发生什么？（提示：看返回 false 的分支与日志级别。）

**需要观察的现象**：你会发现在 Apple/Windows 上 `--ca-certificate` 实际上是「静默降级」的选项——文件被读、函数返回 false、日志记一条 INFO/WARN，下载继续用系统信任库。

**预期结果**：对照表填完后与 4.2.3 末尾的表格一致；第三个问题的答案：`addTrustedCACertFile` 返回 false 触发 [src/MultiUrlRequestInfo.cc:L261-L266](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L261-L266) 的 `A2_LOG_INFO(MSG_WARN_NO_CA_CERT)`，进程不会失败。（本实践为源码阅读型；若手头有 macOS/Windows 机器可编译验证，标注「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LibsslTLSContext.cc` 里 `TLS_PROTO_TLS13` 分支的 `case` 没有独立的 `break`，而是特意标注 fall through？

**参考答案**：最低版本语义是「禁用低于它的所有版本」。选 TLS1.3 就必须同时禁 1.2、1.1、1.0、SSLv3，让 case 贯穿即可让 `ver_opts` 逐层累加 `SSL_OP_NO_*`（[src/LibsslTLSContext.cc:L114-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L114-L131)）。GnuTLS 的优先级字符串追加 `:-VERS-*`（[src/LibgnutlsTLSSession.cc:L136-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L136-L152)）是同一思想的另一种语法。

**练习 2**：四个后端都定义了 `TLSContext::make`，为什么链接期不会重定义冲突？

**参考答案**：因为 `src/Makefile.am` 的条件块保证至多编译一个后端的源文件（[src/Makefile.am:L332-L400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L332-L400)）；而 configure 的 `have_ssl` 闸门（[configure.ac:L384-L430](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L384-L430)）保证 Apple/Win/GnuSSL/OpenSSL 四个 `have_*` 变量至多一个为 yes。这是「编译期单选」策略：换后端必须重新 configure + 编译，不能运行时切换。

**练习 3**：同样叫「验证对端证书」，OpenSSL 后端与 GnuTLS 后端的分工有何不同？

**参考答案**：OpenSSL 后端里 aria2 亲自完成全流程——`SSL_connect` 期间库只做链构建，握手成功后 aria2 调 `SSL_get_verify_result` 拿链验证结论、再自己解析 SAN/CN 调 `net::verifyHostname`（[src/LibsslTLSSession.cc:L234-L328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234-L328)）。GnuTLS 后端把链验证整体交给 `gnutls_certificate_verify_peers`（含 EKU purpose 参数），aria2 只负责把状态位翻译成错误文案并补做主机名匹配（[src/LibgnutlsTLSSession.cc:L253-L301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L253-L301)）。抽象接口的价值正在于此：实现策略可以完全不同，`SocketCore` 与命令链毫不感知。

### 4.3 FeatureConfig 能力上报与平台兼容层

#### 4.3.1 概念说明

u1-l1 建立过一条链路：configure 探测 → `AC_DEFINE` 宏 → 条件编译 → `--version` 上报。本讲把这条链路的「上报端」拆开看：

- **`strSupportedFeature`**：一个 switch，每个 `FEATURE_*` 枚举位在 `#ifdef` 保护下要么返回特性名字符串、要么返回 `nullptr`（表示「本构建不支持」）。它是**编译期事实在运行期的唯一投影点**。
- **`featureSummary`**：把非 nullptr 的名字用逗号拼成一行。
- **`usedLibs`**：类似地按宏拼接**第三方库及版本号**，TLS 后端也在此上报——这是判断「到底编了哪个 TLS 后端」最直接的办法。

「Enabled Features 里有 HTTPS」只说明 `ENABLE_SSL` 开着（某个 TLS 后端中选了）；「Libraries 里出现 `GnuTLS/3.8.x` 还是 `OpenSSL/3.x.y`」才回答了是哪个后端。两个输出互相配合，缺一不可。

#### 4.3.2 核心流程

```text
configure 探测（首中即赢）
  have_appletls / have_wintls / have_libgnutls / have_openssl
        │ AC_DEFINE
        ▼
config.h 宏（HAVE_APPLETLS / HAVE_WINTLS / HAVE_LIBGNUTLS / HAVE_OPENSSL / ENABLE_SSL）
        │ #ifdef
        ▼
FeatureConfig.cc 编进不同的字符串分支
        │
        ▼
aria2c --version ──► "Enabled Features: ... HTTPS ..."
                     "Libraries: ... GnuTLS/3.8.6 ..."   ← 后端判定依据
```

`--version` 还有一个容易忽略的兄弟：`Context` 构造时会 `A2_LOG_INFO(usedLibs())` 把库清单写进日志（[src/Context.cc:L185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L185)），所以**看日志头部也能确认后端**，不必非得跑 `--version`。

#### 4.3.3 源码精读

**特性枚举与开关**（[src/FeatureConfig.h:L47-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.h#L47-L65)）：九个特性位加 `MAX_FEATURE` 哨兵。HTTPS 特性直接绑定 `ENABLE_SSL` 宏（[src/FeatureConfig.cc:L150-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L150-L156)）——注意这里看不出是哪个后端，四后端在 `ENABLE_SSL` 这一层已经被抹平。`featureSummary` 是简单的跳过空位拼接循环（[src/FeatureConfig.cc:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L96-L113)）。

**库清单**（[src/FeatureConfig.cc:L191-L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L191-L248)）：TLS 相关的四段值得对照读——AppleTLS/WinTLS 只报名字（系统组件无版本宏可取），GnuTLS 报 `GNUTLS_VERSION`，OpenSSL 则要从一个十六进制拼装宏 `OPENSSL_VERSION_NUMBER` 里手工移位出主次修订号、并按 patch 字节换算出老式字母后缀（如 `3.5.1a`）。nettle 那段注释「头文件里没有库版本」道尽了版本上报的现实困难。

**打印端**（[src/version_usage.cc:L55-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L55-L81)）：`showVersion` 依次输出 Enabled Features、Hash Algorithms（来自 u5-l5 讲过的 MessageDigest）、Libraries、Compiler、System 五行。

**平台兼容层**。TLS 后端能「一套抽象吃四个平台」，离不开更底层的一组 shim 头文件。`TLSSession.h` 自己就 `#include "a2netcompat.h"`（[src/TLSSession.h:L38-L40](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L38-L40)），因为 `init(sock_t sockfd)` 的参数类型必须跨平台一致：Windows 上 `sock_t` 是 `SOCKET`（句柄），POSIX 上是 `int`（[src/a2netcompat.h:L96-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2netcompat.h#L96-L100)）；同一文件还抹平了 `setsockopt` 的第 4 参类型（`a2_sockopt_t`，char\* vs void\*，[src/a2netcompat.h:L46-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2netcompat.h#L46-L55)）以及缺 `getaddrinfo` 的古老平台（内置替代实现）。`a2netcompat.h` 又叠在 `a2io.h` 之上（`a2io.h` 负责 `a2_write` 之类的文件 I/O 差异）。WinTLS 后端正是靠这套约定才能直接拿 `sock_t` 去喂 Schannel。

**configure 汇总表**。configure 结束时打印的四行 TLS 探测结果（[configure.ac:L1095-L1098](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L1095-L1098)）与 `--version` 输出一一对应，构建期与运行期可以互证——这是 u1-l2 结论在 TLS 上的落地。

#### 4.3.4 代码实践

**实践目标**：从 `aria2c --version` 的输出反推出该二进制的 TLS 后端与 configure 配置。

**操作步骤**：

1. 在已编译的仓库里运行 `./src/aria2c --version`（未编译则阅读 [src/version_usage.cc:L55-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L55-L81) 想象输出，或安装发行版的 aria2c 对照）。
2. 找到 "Enabled Features" 行，检查有无 `HTTPS`；再找 "Libraries" 行，检查出现的是 `GnuTLS/x.y.z`、`OpenSSL/x.y.z`、`AppleTLS` 还是 `WinTLS`。
3. 回到 `FeatureConfig.cc` 的 `usedLibs`，确认你看到的每个库名对应哪个 `#ifdef`；再回到 configure.ac 确认该后端中选时的探测顺序与闸门。

**需要观察的现象**：例如在 Ubuntu 上典型输出是 `Enabled Features: ... HTTPS ...` 且 `Libraries` 含 `GnuTLS/3.x`——据此可推断 configure 时 gnutls 探测先于 openssl 命中（apple/wintls 因平台不符直接出局）。

**预期结果**：能不看 `config.log` 说出「本二进制的 TLS 后端是 X，因为 usedLibs 的第 X 个 ifdef 分支被编入」。若手头二进制与本仓库源码不是同一次构建产物，结论需标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么判断 TLS 后端要看 Libraries 行而不是 Enabled Features 行？

**参考答案**：`FEATURE_HTTPS` 只绑定 `ENABLE_SSL`（[src/FeatureConfig.cc:L150-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L150-L156)），四个后端在抽象层之下已不可区分；而 `usedLibs` 按 `HAVE_APPLETLS/HAVE_WINTLS/HAVE_LIBGNUTLS/HAVE_OPENSSL` 四个互斥宏分别上报（[src/FeatureConfig.cc:L207-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L207-L224)），能唯一指认后端。

**练习 2**：`--disable-ssl` 之后 `--version` 的两行输出各自会发生什么变化？

**参考答案**：configure 中 `enable_ssl != yes` 会把四个 `with_*` 全部置 no（[configure.ac:L316-L323](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L316-L323)），于是 `ENABLE_SSL` 未定义：Enabled Features 行失去 `HTTPS`，Libraries 行失去对应 TLS 条目；同时源码里所有 `#ifdef ENABLE_SSL` 的路径（TLS 上下文装配、SocketCore 的 TLS 方法）整体消失，HTTPS/BT 的 TLS tracker 能力不可用。

**练习 3**：`nettle` 为什么在 Libraries 里没有版本号？

**参考答案**：`usedLibs` 中该分支的注释写明「头文件里没有库版本」（[src/FeatureConfig.cc:L225-L228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L225-L228)）。版本上报依赖库在公共头文件里暴露版本宏（如 `GNUTLS_VERSION`、`OPENSSL_VERSION_NUMBER`），nettle 没有提供，只能报名字。

## 5. 综合实践

综合实践把本讲三个模块串成一个完整实验：**用自签名证书制造一次「必然失败的握手」，再亲手关掉验证让它成功，最后在源码里看清分叉点**。

1. **准备一个自签名 HTTPS 站点**（示例命令，细节可按发行版调整）：

   ```bash
   # 生成自签名证书（CN 必须与访问主机名一致，否则会命中 hostname 不匹配分支）
   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
     -days 2 -nodes -subj "/CN=localhost"
   # 起一个最简 HTTPS 服务并放一个测试文件
   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 2 \
     -nodes -subj "/CN=localhost" && echo hello > index.html
   python3 -m http.server --ssl-certfile cert.pem --ssl-keyfile key.pem 8443
   ```

   （若无 python3 现代版本，可换用 `openssl s_server -WWW -accept 8443 -key key.pem -cert cert.pem`，效果等同。）

2. **验证特性与后端**：运行 `./src/aria2c --version`，按 4.3.4 的方法记录 Enabled Features 与 Libraries 中 TLS 相关条目。

3. **两次对照下载**（待本地验证）：

   ```bash
   ./src/aria2c https://localhost:8443/index.html -d /tmp/a2t --allow-overwrite=true
   ./src/aria2c --check-certificate=false https://localhost:8443/index.html \
     -d /tmp/a2t2
   ```

   预期：第一次失败，报证书错误（OpenSSL 后端典型文案含 `certificate verification failed` 或 `hostname does not match`，取决于证书细节）；第二次成功下载 `hello`。

4. **定位分叉点**：给失败的那次加上 `-l /tmp/a2t/log.txt --log-level=debug`，从日志行头的 CUID 找到抛错的命令（u9-l3 的诊断方法）。然后按你的后端在源码分叉处加一行日志：
   - OpenSSL 后端：在 [src/LibsslTLSSession.cc:L234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234) 的 `if (tlsContext_->getSide() == TLS_CLIENT && tlsContext_->getVerifyPeer()) {` 之前插入 `A2_LOG_INFO("verifyPeer branch entered");`，重新编译 `make -j$(nproc)` 后重复第 3 步两次下载。
   - GnuTLS 后端：在 [src/LibgnutlsTLSSession.cc:L253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L253) 的 `if (tlsContext_->getVerifyPeer()) {` 前插入同样的日志。

5. **观察与解释**：`--check-certificate=false` 那次日志中**不应出现**你加的这行——因为 `getVerifyPeer()` 为 false，整个验证块被跳过，这就是「关掉验证」在源码里的全部含义。把两次运行的日志差异、`--version` 的后端结论、以及你画的 4.1.4 时序图整理成一页实验报告。

注意：改源码加日志仅用于本地学习，请勿把调试改动提交；实验后用 `git checkout -- src/` 恢复。

## 6. 本讲小结

- aria2 以 `TLSContext`（进程级配置袋，全进程至多两个实例）与 `TLSSession`（每连接一个的握手/读写状态机）两个纯虚接口抹平 TLS 后端差异；`SocketCore::tlsHandshake` 的三态状态机（NONE→HANDSHAKING→CONNECTED）是抽象接口接入命令链的宿主。
- `TLS_ERR_OK/ERROR/WOULDBLOCK` 三值约定加上 `checkDirection()`，把各库五花八门的「稍后再试」信号统一成 EventPoll 可理解的读/写事件需求，与非阻塞 socket 的处理方式完全同构。
- 四后端「编译期单选」：configure 按 Apple→Win→GnuTLS→OpenSSL 顺序探测、`have_ssl` 闸门首中即赢，四组源文件由 Makefile.am 条件块互斥编译，每个后端各自定义同名工厂 `TLSContext::make`/`TLSSession::make`。
- 后端能力差异集中在证书与信任库：OpenSSL/GnuTLS 支持 PEM+PKCS12 与 CA bundle，Apple 走 KeyChain（含指纹引用身份的特有玩法），Win 只认 PFX 且 CA bundle 一律回退系统库；验证策略从「aria2 亲手验」（OpenSSL）到「库验+EKU purpose」（GnuTLS）再到「系统标志驱动」（Apple/Win）各不相同。
- `--version` 的 Enabled Features 只报 `ENABLE_SSL` 有无，判具体后端要看 Libraries 行（或日志头部的 `usedLibs()` 输出）；FeatureConfig 是编译期探测结果在运行期的唯一投影点。
- 平台兼容层（`a2netcompat.h`/`a2io.h`）用 `sock_t`、`a2_sockopt_t` 这类小别名承接差异，是四个后端能共享同一抽象的底座。

## 7. 下一步学习建议

- **u9-l5（测试体系）**：TLS 相关测试集中在 `test/LibsslTLSSessionTest.cc` 与 `test/LibgnutlsTLSSessionTest.cc`，去看测试如何伪造握手环境，是理解验证分支的捷径。
- **横向阅读 `SocketCore.cc` 的 TLS 收发路径**：`tlsSession_->readData/writeData` 如何替换明文读写、`getRecvBufferedLength` 如何参与 u2-l4 讲过的 `SocketRecvBuffer`，能把本讲与网络底座讲连成一体。
- **对照 u8-l3（内嵌 HTTP/WebSocket 服务器）**：RPC over TLS（`--rpc-secure`）用的是服务端上下文与 `tlsAccept`，把本讲的 `TLS_SERVER` 半边补全；WebSocket 的 wss 也复用同一会话抽象。
- 若你对「新增第五个后端」感兴趣，按 `TLSSession.h` 的注释提示，参照 WinTLS（最重）或 GnuTLS（中等）实现全部纯虚方法，再在 configure.ac/Makefile.am 加一对探测与条件块，即可完整走一遍 u9-l6 的二次开发流程。
