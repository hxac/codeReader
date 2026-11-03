# u9-l4 平台抽象与 TLS 多后端矩阵

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TLSContext` 与 `TLSSession` 两个抽象类各自的职责边界，以及它们为什么被拆成两个对象。
2. 解释 aria2「链接期单选、运行期多态」的双层抽象手法：四个 TLS 后端如何在编译期互斥地各自定义同一个 `make` 工厂函数。
3. 跟踪一次 HTTPS 握手的完整链路：`--check-certificate` 选项如何一路传到某个后端的 `tlsConnect` 验证分支。
4. 对比 OpenSSL、GnuTLS、Apple（SecureTransport）、Windows（Schannel）四个后端在证书加载、信任库、证书校验位置、握手模型上的差异。
5. 用 `aria2c --version` 的 `Enabled Features` 与 `Libraries` 两行输出，反推当前二进制链接的是哪个 TLS 后端。

## 2. 前置知识

本讲是「专家层」讲义，默认你已学过 u1-l2（构建系统）、u4-l2（HTTP 协议实现）与 u2-l2（EventPoll）。再用三段话补齐 TLS 领域的最低背景：

**TLS 握手在做什么。** TLS（Transport Layer Security）是插在 TCP 之上、HTTP 之下的加密层。连接建立后，双方先交换若干「握手消息」：协商协议版本与加密套件、互相验证证书、交换密钥材料，之后应用数据才被加密传输。对我们最重要的是两点：其一，握手要来回多趟，所以在一个非阻塞 socket 上握手必然「走一步、歇一步」；其二，「验证对方证书」是客户端的职责，这一步的松紧决定了你是否会被中间人攻击。

**证书与信任链。** 服务端证书由 CA（证书颁发机构）签发，浏览器/下载器内置一批「根 CA 证书」作为信任起点。验证要做三件事：证书链能追溯到受信任的根（`SSL_get_verify_result` 一类接口返回的就是这个结论）、证书在有效期内、证书上的名字（SAN/CN 字段）与实际访问的主机名匹配。**自签名证书**没有 CA 背书，链验证必然失败——这就是本讲实践环节要用它做实验的原因。

**非阻塞三值约定。** 回顾 u2-l2/u2-l3：aria2 全程非阻塞，任何网络操作都可能「没就绪」。TLS 抽象因此约定三个返回值：`TLS_ERR_OK`（成功）、`TLS_ERR_WOULDBLOCK`（没就绪，稍后再来）、`TLS_ERR_ERROR`（致命失败）。配合 `checkDirection()` 回答「下次该等读还是等写」，就能把 TLS 嫁接到 EventPoll 的事件循环上。

另有一个贯穿全讲的编译期概念：**同一个符号的四份定义**。C++ 里一个非内联函数只能有一份定义，但 aria2 让四个后端源文件各自定义 `TLSContext::make`，靠 Makefile 的条件编译保证最终只有一份被编进产物——这是理解「多后端矩阵」的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/TLSContext.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSContext.h#L53-L72) | TLS「配置工厂」抽象接口：证书加载、信任库、验证开关 |
| [src/TLSSession.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L54-L120) | TLS「单连接会话」抽象接口：握手、读写、方向查询 |
| [src/LibsslTLSContext.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc) / [src/LibsslTLSSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc) | OpenSSL 后端（Linux/BSD 等通用平台的主力后端） |
| [src/LibgnutlsTLSContext.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc) / [src/LibgnutlsTLSSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc) | GnuTLS 后端（configure 默认优先探测的 TLS 库） |
| [src/AppleTLSContext.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc) / [src/AppleTLSSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSSession.cc) | Apple SecureTransport 后端（macOS 专用） |
| [src/WinTLSContext.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc) / [src/WinTLSSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc) | Windows Schannel 后端 |
| [src/SocketCore.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L903-L1034) | 握手驱动器：`tlsHandshake` 状态机，承接 u2-l4 的 SocketCore |
| [src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L177-L274) | TLS 上下文的装配点：把选项翻译成 TLSContext 配置 |
| [src/FeatureConfig.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc) / [src/FeatureConfig.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.h#L47-L58) | 编译期特性上报：`--version` 的 Features/Libraries 输出来源 |
| [src/version_usage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L53-L82) | `--version` 的打印实现 |
| [src/a2netcompat.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2netcompat.h#L95-L99) | 平台兼容层：`sock_t` 等类型在 POSIX/Windows 间的统一 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**TLS 抽象接口**、**握手驱动链路**、**四个平台后端**、**FeatureConfig 能力上报**。

### 4.1 模块一：TLS 抽象接口——TLSContext 与 TLSSession

#### 4.1.1 概念说明

aria2 把 TLS 拆成两个生命周期完全不同的对象：

- **`TLSContext` 是进程级配置对象**——回答「我有哪些证书、信任哪些 CA、要不要验证对方」。一个进程最多两个实例：一个客户端身份（下载用）、一个服务端身份（`--rpc-secure` 开启 RPC over HTTPS 时用）。它像一张「身份证 + 信任清单」，内容在整个进程运行期间基本不变。
- **`TLSSession` 是连接级会话对象**——每条 TLS 连接一个，负责这条连接上的握手、加解密读写与关闭。连接关闭即销毁。

为什么要拆开？因为 TLS 库本身就这样分层（OpenSSL 的 `SSL_CTX`/`SSL`、GnuTLS 的 credentials/session 都是成对出现）：配置重（加载证书、建信任库），会话轻（引用配置即可）。上层代码只在「创建连接」时调 `TLSSession::make(ctx)`，完全不必关心用的是哪家库。

这个抽象还有个隐藏设计：头文件里 [TLSSession.h:52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L52-L52) 写着一句注释——"To create another SSL/TLS backend, implement TLSSession class below"（要创建新的 TLS 后端，实现下面的 TLSSession 类即可）。整份接口就是为「再加一个后端」预留的插槽。

#### 4.1.2 核心流程

`TLSContext` 的接口只有七个方法，按用途分三组：

```text
TLSContext（配置工厂）
├── 身份：addCredentialFile(certfile, keyfile)   加载本端证书+私钥（客户端证书/RPC 服务端证书）
├── 信任：addSystemTrustedCACerts()               加载操作系统信任的根 CA
│         addTrustedCACertFile(certfile)          加载 --ca-certificate 指定的 PEM 包
└── 状态：good() / getSide() / getVerifyPeer() / setVerifyPeer(bool)
```

`TLSSession` 的接口围绕「一次非阻塞握手 + 加解密读写」：

```text
TLSSession（单连接会话）
├── 建立：init(sockfd)                绑定底层 socket
│         setSNIHostname(hostname)    设置 SNI（服务器名称指示）扩展
├── 握手：tlsConnect(hostname, version, handshakeErr)   客户端方向
│         tlsAccept(version)                              服务端方向
│         checkDirection() → TLS_WANT_READ / TLS_WANT_WRITE
├── 数据：writeData / readData        密文世界与明文世界的桥梁
└── 收尾：closeConnection() / getLastErrorString() / getRecvBufferedLength()
```

握手期间一次调用的返回语义：

```text
tlsConnect() 返回 TLS_ERR_OK         → 握手完成，version 里是协商出的协议版本
tlsConnect() 返回 TLS_ERR_WOULDBLOCK → 没就绪；调 checkDirection() 问下次等读还是等写
tlsConnect() 返回 TLS_ERR_ERROR      → 失败；handshakeErr 里是人类可读的原因
                                     （专用于「证书验证失败」这类需要展示的场景）
```

注意 `writeData`/`readData` 返回 `ssize_t`（字节数），而错误码是负数（`TLS_ERR_ERROR = -1`、`TLS_ERR_WOULDBLOCK = -2`），字节数与错误共用一个返回通道，这是 C 风格接口的惯用法。

#### 4.1.3 源码精读

**TLSContext 的完整契约**在 [src/TLSContext.h:44-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSContext.h#L44-L72)：先定义两个公共枚举——会话方向 `TLS_CLIENT`/`TLS_SERVER` 与最低协议版本 `TLS_PROTO_TLS11/12/13`，然后是七个纯虚方法。其中 `addCredentialFile` 的注释很关键：「private key `keyfile' must be decrypted」（私钥文件必须是未加密的）、「certfile can contain multiple certificates」（证书文件可含整条证书链）——这两句约束来自后端实现的真实限制。

**TLSSession 的完整契约**在 [src/TLSSession.h:44-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L44-L56)：两个枚举给出三值错误码与方向码；静态工厂 `make(TLSContext* ctx)` 是唯一的创建入口。`tlsConnect` 的签名注释（[src/TLSSession.h:93-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TLSSession.h#L93-L100)）明确要求：返回 `TLS_ERR_ERROR` 时必须把证书验证错误填进 `handshakeErr`——这就是你在终端里看到的 `SSL/TLS handshake failure: ...` 消息的来源。

接下来是本讲最重要的一处设计。四个后端文件里**各自定义了同一个静态方法**：

- [src/LibsslTLSContext.cc:95-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L95-L98) — `TLSContext::make` 返回 `new OpenSSLTLSContext(...)`
- [src/LibgnutlsTLSContext.cc:52-55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L52-L55) — 返回 `new GnuTLSContext(...)`
- [src/AppleTLSContext.cc:179-182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L179-L182) — 返回 `new AppleTLSContext(...)`
- [src/WinTLSContext.cc:110-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L110-L113) — 返回 `new WinTLSContext(...)`

`TLSContext::make` 在四个互斥编译单元中各有一份定义——这就是「链接期单选、运行期多态」的双层抽象，分别对应两张编排表：

- 编译期（互斥单选）：[src/Makefile.am:324-342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L324-L342) 的 `ENABLE_SSL` 块先收编两个抽象头文件，随后 `HAVE_APPLETLS`、`HAVE_WINTLS`、`HAVE_LIBGNUTLS` 三个条件块各收编一对后端源码；[src/Makefile.am:388-400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L388-L400) 的 `HAVE_OPENSSL` 块里，Libssl 那对文件还额外套了 `if !HAVE_APPLETLS`——macOS 上即使链接了 OpenSSL（仍用于消息摘要/DH），TLS 也强制走 Apple 后端。
- 运行期（虚接口多态）：上层只持有 `TLSContext*`/`TLSSession*`，所有调用经虚表分发，与具体库解耦。

对比一下你熟悉的两条路线：这既不是 u2-l2 EventPoll 那种「运行期 new 哪个后端类」的选择，也不是简单的 `#ifdef` 散落各处——而是把「选择」压缩进链接期，让运行期代码完全干净。

#### 4.1.4 代码实践：找出四份 make 定义并验证互斥

1. **实践目标**：亲眼确认「同一符号四份定义、靠构建系统保证唯一」这一机制。
2. **操作步骤**：
   ```bash
   cd <aria2 源码根目录>
   grep -n "TLSContext::make" src/*.cc
   grep -n "TLSSession::make" src/*.cc
   # 再看 Makefile.am 的四个条件块
   sed -n '324,342p;360,364p;388,400p' src/Makefile.am
   ```
3. **需要观察的现象**：`TLSContext::make` 恰好命中四行，分别位于 Libssl/Libgnutls/Apple/Win 四个 Context 文件；`TLSSession::make` 同样四处，位于对应的四个 Session 文件。
4. **预期结果**：结合 Makefile.am 的 `if HAVE_APPLETLS` / `if HAVE_WINTLS` / `if HAVE_LIBGNUTLS` / `if HAVE_OPENSSL`（且 `!HAVE_APPLETLS`）四个互斥块，说明任意一份 configure 产物只会编入其中一对。若你本地编译过 aria2，还可用 `nm -C src/aria2c | grep 'TLSContext::make'` 看最终二进制里只保留了一份定义（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `addSystemTrustedCACerts` 与 `addTrustedCACertFile` 是两个方法，而不是一个带参数的？
**答案**：两者语义完全不同——前者是「导入操作系统自带的所有根 CA」（来源由后端决定：OpenSSL 用默认路径、GnuTLS 用系统信任库、Apple 是钥匙串、Windows 是系统证书存储），后者是「导入用户用 `--ca-certificate` 指定的一个 PEM 文件」。调用方 [MultiUrlRequestInfo.cc:261-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L261-L271) 逻辑是「给了 ca 文件就只导它，否则若开验证才导系统库」，拆开的方法正好一一对应两条互斥分支，且允许后端声明「我不支持 CA bundle 文件」（Apple/Windows 就这么干）。

**练习 2**：`checkDirection()` 什么时候会被调用？它的返回值被翻译成了什么？
**答案**：在握手或读写返回 `TLS_ERR_WOULDBLOCK` 之后。以 OpenSSL 后端为例，[src/LibsslTLSSession.cc:107-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L107-L117) 用 `SSL_get_error` 判断是 `SSL_ERROR_WANT_WRITE` 还是其他（默认按 WANT_READ 处理）。上层 SocketCore 拿到方向后置 `wantRead_`/`wantWrite_` 标志（见 4.2.3），最终翻译成 EventPoll 的读/写事件注册——这正是 u2-l2 讲过的「回调只置位、不执行」机制在 TLS 上的应用。

**练习 3**：`getRecvBufferedLength()` 是做什么用的？为什么 TLS 连接特别需要它？
**答案**：它返回「TLS 库内部已经解密、但上层还没取走」的数据长度（OpenSSL 后端继承基类默认返回 0，WinTLS 后端返回 `decBuf_.size()`，见 [src/WinTLSSession.cc:834](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L834-L834)）。因为 TLS 解密是按记录（record）进行的，一次可能解出比上层请求更多的数据，这些数据留在库缓冲里、不占用 socket 接收缓冲。调用方（如 [src/AbstractCommand.cc:152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L152-L152)）据此判断「还有没消费完的数据」，避免误判连接空闲。

### 4.2 模块二：握手驱动链路——从 --check-certificate 到 tlsConnect

#### 4.2.1 概念说明

抽象接口只是插槽，还得有人插电。这条链路回答三个问题：

1. **谁创建 TLSContext？** —— `MultiUrlRequestInfo::prepare()`（u1-l3 讲过的 `execute()` 三段骨架的第一段），在下载引擎创建之前把选项翻译成 TLS 配置。
2. **谁创建 TLSSession 并驱动握手？** —— `SocketCore::tlsHandshake`，一个三状态小状态机，被 HTTP 命令链在需要时反复调用。
3. **`--check-certificate` 走到哪？** —— 它最终落在 `TLSContext::setVerifyPeer(bool)`，再被各后端的 `tlsConnect` 读取，决定要不要走证书验证分支。

#### 4.2.2 核心流程

装配期（进程启动，一次）：

```text
MultiUrlRequestInfo::prepare()
├── [--rpc-secure 且 --rpc-certificate 非空]
│     TLSContext::make(TLS_SERVER, minTLSVer)
│     addCredentialFile(rpc-certificate, rpc-private-key)
│     → SocketCore::setServerTLSContext()          # RPC 服务器身份
└── [ENABLE_SSL]
      TLSContext::make(TLS_CLIENT, minTLSVer)
      ├── [--certificate 非空] addCredentialFile(certificate, private-key)
      ├── [--ca-certificate 非空] addTrustedCACertFile(...)   # 用户 CA 包优先
      ├── [否则且 --check-certificate=true] addSystemTrustedCACerts()
      ├── setVerifyPeer(--check-certificate)
      └── → SocketCore::setClientTLSContext()      # 下载客户端身份
```

连接期（每条 HTTPS 连接）：

```text
HttpRequestCommand::executeInternal()   (u4-l1 命令链的第三棒)
  └── SocketCore::tlsConnect(hostname)
        └── tlsHandshake(ctx, hostname) 状态机：
              A2_TLS_NONE ──首次──► 创建 TLSSession、init(sockfd)、设 SNI
              A2_TLS_HANDSHAKING ──► tlsConnect()/tlsAccept()
                  ├── OK         → A2_TLS_CONNECTED，返回 true
                  ├── WOULDBLOCK → 置 wantRead_/wantWrite_，返回 false（命令 requeue）
                  └── ERROR      → 抛 DL_ABORT_EX("SSL/TLS handshake failure: ...")
```

#### 4.2.3 源码精读

**装配点的客户端分支**在 [src/MultiUrlRequestInfo.cc:252-274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L252-L274)：注意三个选项的优先级——`--ca-certificate` 给了就只导它；否则 `--check-certificate=true`（默认值就是 true，见 [src/OptionHandlerFactory.cc:1048-1050](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1048-L1050)）才导入系统信任库；最后 `setVerifyPeer` 一锤定音。最低协议版本由 `--min-tls-version`（默认 `TLSv1.2`，[src/OptionHandlerFactory.cc:514](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L514-L514)）经 [src/util.cc:2471-2484](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2471-L2484) 的 `toTLSVersion` 翻译成枚举。服务器分支在 [src/MultiUrlRequestInfo.cc:183-203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L183-L203)，只在 `--rpc-secure` 时创建，服务于 u8-l3 讲过的 RPC over HTTPS。

**状态机本体**在 [src/SocketCore.cc:915-1034](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L915-L1034)，三段值得细读：

- 初始装配段 [src/SocketCore.cc:925-948](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L925-L948)：`TLSSession::make` → `init(sockfd_)`；SNI 有个讲究——只对「非纯数字且带点」的主机名设置（[src/SocketCore.cc:937-944](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L937-L944)），因为对 IP 地址或 `localhost` 发 SNI 会触发 GnuTLS 告警。
- 就绪段 [src/SocketCore.cc:1002-1015](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1002-L1015)：`WOULDBLOCK` 时按 `checkDirection()` 置 `wantRead_`/`wantWrite_` 后**返回 false 而非 true**——注释写得很清楚：这样该函数会在缓冲处理完毕后被再次调用。
- 失败段 [src/SocketCore.cc:1017-1023](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1017-L1023)：抛 `DL_ABORT_EX`，优先展示后端填好的 `handshakeErr`（证书验证失败的具体原因），没有才退到 `getLastErrorString()`。

**命令链触发点**回到 [src/HttpRequestCommand.cc:127-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L127-L135)：协议是 `https` 就先 `tlsConnect`，没握完则按 want 标志注册读写事件并 `addCommandSelf()` 自我重排——把 u2-l3 的命令接力与 u4-l1 的 HTTP 命令链缝在了一起。

#### 4.2.4 代码实践：用日志观察一次真实握手

1. **实践目标**：不开调试器，仅凭日志看清「SNI 设置 → 握手成功 → 协商版本」三个时刻。
2. **操作步骤**：
   ```bash
   ./src/aria2c --version          # 先确认 Features 里有 HTTPS（见 4.4）
   ./src/aria2c -l tls.log --log-level=debug -d /tmp https://example.com/
   grep -n "TLS\|Securely connected" tls.log
   ```
3. **需要观察的现象**：日志里应出现 `Creating TLS session`、`TLS Handshaking`（[src/SocketCore.cc:927](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L927-L947) 的两条 debug 日志）以及最终的 `Securely connected to example.com (93.184.216.34:443) with TLSv1.x`（[src/SocketCore.cc:994](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L994-L995)）。
4. **预期结果**：`with` 后面的版本串由 [src/SocketCore.cc:976-990](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L976-L990) 的 switch 从 `tlsConnect` 带回的 `TLSVersion` 枚举翻译而来——现代环境下通常是 `TLSv1.3`。若你的网络环境无法访问该站点，可换任意 HTTPS URL；具体日志行内容待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`--check-certificate=false` 时，aria2 还会加载系统 CA 证书吗？
**答案**：不会。看 [src/MultiUrlRequestInfo.cc:267-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L267-L271)：`addSystemTrustedCACerts` 只在「没给 `--ca-certificate` 且 `check-certificate` 为真」时调用。关掉验证开关后既不导入系统库、也不导入用户 CA 包，`setVerifyPeer(false)` 让各后端握手时直接跳过证书验证分支——省内存也更「坦诚」。

**练习 2**：SocketCore 的 `tlsHandshake` 为什么要保存 `secure_` 状态而不是一口气握完？
**答案**：因为 socket 是非阻塞的：TLS 握手要多个来回，任何一步都可能 `WOULDBLOCK`，此时必须返回 false、把控制权交还事件循环，等 EventPoll 报告 socket 可读/可写后再进来继续。`secure_` 的三个取值（`A2_TLS_NONE`/`A2_TLS_HANDSHAKING`/`A2_TLS_CONNECTED`，[src/SocketCore.cc:920-950](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L920-L950)）分别对应「还没开始」「握到一半」「已完成」，`A2_TLS_CONNECTED` 还兼做幂等短路：连接池复用连接时再调 `tlsConnect` 直接返回 true。

**练习 3**：为什么「对 localhost 不发 SNI」？
**答案**：见 [src/SocketCore.cc:935-938](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L935-L938) 的注释：SNI 的语义是把「要访问哪个虚拟主机」告诉服务器，只对域名有意义；对 IP 地址或 `localhost` 这类无点主机名发 SNI，GnuTLS 会产生 TLS alert。所以条件是 `!isNumericHost(hostname) && hostname 含 '.'`。

### 4.3 模块三：四个平台后端对比

#### 4.3.1 概念说明

四个后端面对同一份契约，交出了四份截然不同的答卷。差异的根源在于**底层 TLS 库的 API 形态**：

- **OpenSSL / GnuTLS**：通用 C 库，原生支持非阻塞（`SSL_connect`/`gnutls_handshake` 在非阻塞 fd 上返回 WANT_READ/WRITE），aria2 只做薄封装；证书验证由 aria2 **自己做**（提 SAN/CN、比对主机名），保证行为跨后端一致。
- **Apple SecureTransport / Windows Schannel**：操作系统组件，API 偏同步、与系统证书存储（钥匙串/证书存储）深度绑定；验证交给系统库做，aria2 通过库的开关项控制；Apple 后端甚至直接从钥匙串按指纹找证书。
- **Windows Schannel** 最特殊：它的握手 API 一次调用只推进一小步且不管网络收发，于是 WinTLS 后端不得不**自己维护读写缓冲、手写一台显式握手状态机**。

#### 4.3.2 核心流程

四种握手模型对照（伪代码）：

```text
OpenSSL：   repeat { rv = SSL_connect(ssl) }      # 库记住中间状态，重入即可
GnuTLS：    for(;;) { rv = gnutls_handshake(sess)  # 非致命警告继续循环
              if fatal → ERROR; if again → WOULDBLOCK }
Apple：     rv = SSLHandshake(ctx)                 # 同步推进
            if wouldBlock → WOULDBLOCK
            if serverAuthCompleted → 递归再调一次（验证完成的钩子点）
Windows：   st_initialized → 组初始消息 → st_handshake_write
            → st_handshake_read → 解析 →（循环 write/read）→ st_handshake_done
            # 每步自己 send()/recv()，缓冲全自管
```

#### 4.3.3 源码精读

**OpenSSL 后端。** Context 构造 [src/LibsslTLSContext.cc:100-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L100-L152) 是一份「安全加固清单」：`SSLv23_method()` 建立方法对象（名字古老，实为「协商可用版本」）；最低版本经 fall-through 累积 `SSL_OP_NO_*` 位（选 TLS1.3 就依次关掉 1.2/1.1/1.0/SSLv3，[src/LibsslTLSContext.cc:114-131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L114-L131)——注意每个 case 都不 break、往下渗透，这是位掩码叠加的惯用写法）；再叠加 `SSL_OP_ALL`（兼容性 workaround）、禁 SSLv2、禁压缩、`SSL_MODE_AUTO_RETRY|PARTIAL_WRITE`、`RELEASE_BUFFERS` 省内存，最后 cipher list 限定 `"HIGH:!aNULL:!eNULL"`（[src/LibsslTLSContext.cc:148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L148-L152)）。信任库两个方法分别映射 `SSL_CTX_set_default_verify_paths`（[src/LibsslTLSContext.cc:276-287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L276-L287)）与 `SSL_CTX_load_verify_locations`（[src/LibsslTLSContext.cc:289-300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L289-L300)）。

OpenSSL 的**手工证书验证**是本讲的实践锚点，位于 [src/LibsslTLSSession.cc:234-328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234-L328)，结构是「三连问」：

1. 对端给了证书吗？——`SSL_get1_peer_certificate`（OpenSSL 3.0+；旧版叫 `SSL_get_peer_certificate`，见 [src/LibsslTLSSession.cc:236-244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L236-L244) 的版本条件编译）。没有 → `handshakeErr = "certificate not found"`。
2. 证书链验证通过吗？——`SSL_get_verify_result` ≠ `X509_V_OK` 就把错误码翻译成人话（[src/LibsslTLSSession.cc:247-251](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L247-L251)）。自签名证书在这里死掉，典型错误串如 `self-signed certificate` / `self signed certificate`。
3. 名字对得上吗？——从证书提 SAN 的 DNS 名与 IP 地址（[src/LibsslTLSSession.cc:255-291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L255-L291)）与 CommonName（[src/LibsslTLSSession.cc:292-324](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L292-L324)），交给公共函数 `net::verifyHostname` 比对（[src/LibsslTLSSession.cc:325-328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L325-L328)），失败 → `handshakeErr = "hostname does not match"`。

注意这整段被 `if (... getSide() == TLS_CLIENT && getVerifyPeer())` 包着——`--check-certificate=false` 就是把这道闸门打开、整段跳过。

**GnuTLS 后端。** 握手是一个 for(;;) 循环（[src/LibgnutlsTLSSession.cc:241-252](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L241-L252)）：`GNUTLS_E_AGAIN/E_INTERRUPTED` 翻成 WOULDBLOCK，`gnutls_error_is_fatal` 为真才报 ERROR——**警告类返回值会继续循环**，这是 GnuTLS 特有的语义。验证同样是手工三连问，但第一问用的是 `gnutls_certificate_verify_peers` 且带上了扩展密钥用途（EKU）参数 `GNUTLS_KP_TLS_WWW_SERVER`（[src/LibgnutlsTLSSession.cc:253-264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L253-L264)——这正是最近一次提交 `gnutls: Perform ExtendedKeyUsage validation` 引入的加固：证书即使链合法，用途不是「TLS 服务器」也要拒）。状态位翻译成可读错误串的段落见 [src/LibgnutlsTLSSession.cc:268-296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L268-L296)。Context 侧，系统信任库依赖较新的 GnuTLS 版本，旧版干脆打日志 `"System certificates not supported"` 返回 false（[src/LibgnutlsTLSContext.cc:129-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSContext.cc#L129-L146)）——同一个抽象方法在不同后端可能「合法地失败」，调用方须容忍。

**Apple 后端。** 最有个性的两个方法：`addCredentialFile` 不直接读文件，而是「先当指纹试、再当 PKCS12 试」（[src/AppleTLSContext.cc:192-209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L192-L209)）——指纹路线会用该串去**钥匙串里查询**身份证书（[src/AppleTLSContext.cc:220-278](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L220-L278)）；`addTrustedCACertFile` 直接宣布不支持（[src/AppleTLSContext.cc:211-216](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSContext.cc#L211-L216)），让你用钥匙串管理证书。握手是同步式 `SSLHandshake`，`errSSLWouldBlock` 翻 WOULDBLOCK，收到 `errSSLServerAuthCompleted`（证书验证完成的钩子事件）就递归再推进一次（[src/AppleTLSSession.cc:733-748](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleTLSSession.cc#L733-L748)）。证书校验本身交给 SecureTransport（配置阶段已设好验证开关），aria2 不重复实现。

**Windows 后端。** Context 的本体是一个 `SCHANNEL_CRED` 结构：版本开关用 `SP_PROT_TLS1_x_CLIENT/SERVER` 位累积（[src/WinTLSContext.cc:76-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L76-L101)），**switch 里只有 TLS1.1/1.2 两个 case**——传 TLS1.3 会落进 `default: abort()`，即 WinTLS 后端不支持 TLS 1.3。`verifyPeer` 的实现是纯标志位：开验证就上 `SCH_CRED_AUTO_CRED_VALIDATION`（系统库自动验），关验证就换 `SCH_CRED_MANUAL_CRED_VALIDATION` 一组手动标志（[src/WinTLSContext.cc:128-155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L128-L155)）。凭据句柄惰性创建于 `getCredHandle`（[src/WinTLSContext.cc:157-191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L157-L191)）。客户端证书只认 PFX/PKCS12（`PFXIsPFXBlob` + `PFXImportCertStore`，[src/WinTLSContext.cc:193-229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L193-L229)）。Session 侧则是一台货真价实的显式状态机：`st_initialized → st_handshake_write → st_handshake_read → ... → st_handshake_done`，读写全部自己 `send()`/`recv()`（[src/WinTLSSession.cc:575-657](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L575-L657)）——因为 Schannel 的 `InitializeSecurityContext` 每次只吐出「该发什么」和「还差什么」，网络搬运得调用者自己干。

把四家答案排成一张矩阵（建议你动手核对每一格）：

| 维度 | OpenSSL (Libssl) | GnuTLS | Apple (SecureTransport) | Windows (Schannel) |
|---|---|---|---|---|
| Context 本体 | `SSL_CTX` | `gnutls_certificate_credentials_t` | `SecIdentityRef`（钥匙串身份） | `SCHANNEL_CRED` + 惰性 `CredHandle` |
| 客户端证书 | PEM 双文件或 PKCS12 | PEM 双文件或 PKCS12 | 仅 PKCS12（空密码）或钥匙串指纹 | 仅 PFX/PKCS12 |
| 系统信任库 | `set_default_verify_paths` | `set_x509_system_trust`（版本门槛） | 钥匙串即系统库 | 系统证书存储（隐式） |
| `--ca-certificate` | 支持 | 支持 | **不支持**（WARN） | **不支持**（WARN，用系统库） |
| 证书校验在哪做 | aria2 手工三连问 | aria2 手工三连问 + EKU | 系统库内 | 系统库内 |
| 主机名匹配 | `net::verifyHostname` | `net::verifyHostname` | 系统库内 | 系统库内 |
| 握手模型 | 库内重入（`SSL_connect`） | 库内循环 + 警告容忍 | 同步 `SSLHandshake` | **自管缓冲的显式状态机** |
| TLS 1.3 | 支持（编译期探测宏） | 依库版本 | 支持 | **不支持**（switch 无此分支） |

最后看**公共的主机名匹配器** `net::verifyHostname`（[src/SocketCore.cc:1536-1569](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1536-L1569)）：主机名是数字地址（IP）时走 IP SAN 的二进制比对；否则**若证书带 DNS SAN 就只看 SAN、完全忽略 CN**（这是 RFC 6125 的推荐做法），只有证书没有 SAN 时才退回 CN 匹配。OpenSSL 与 GnuTLS 两个手工验证后端共用这一个函数，保证行为一致。

#### 4.3.4 代码实践：源码对比阅读

1. **实践目标**：亲手填满上面那张矩阵，体会「同一契约、四种实现」。
2. **操作步骤**：对四个后端各做三件事——
   ```bash
   # ① 各后端 addTrustedCACertFile 的态度
   grep -n -A6 "addTrustedCACertFile" src/LibsslTLSContext.cc src/LibgnutlsTLSContext.cc \
                                            src/AppleTLSContext.cc src/WinTLSContext.cc
   # ② 各后端验证分支的入口判断
   grep -n "getVerifyPeer" src/LibsslTLSSession.cc src/LibgnutlsTLSSession.cc \
                                src/AppleTLSSession.cc src/WinTLS*.cc
   # ③ TLS 1.3 的处理
   grep -n "TLS13\|TLS_PROTO_TLS13" src/LibsslTLSContext.cc src/LibsslTLSSession.cc \
                                     src/WinTLSContext.cc src/AppleTLSSession.cc
   ```
3. **需要观察的现象**：Libssl/Libgnutls 在 Session 的 `tlsConnect` 里手工验证；Apple/Win 在 Context 阶段就把验证策略配成库开关；`TLS_PROTO_TLS13` 在 WinTLSContext 的 switch 中没有任何 case。
4. **预期结果**：与你填写的矩阵一致。特别注意 ②：OpenSSL 与 GnuTLS 命中于握手函数内部（真验证分支），Apple/Windows 命中于 Context 的配置阶段（`setVerifyPeer` 等）——「校验位置」一行的源码证据就在这里。

#### 4.3.5 小练习与答案

**练习 1**：为什么 OpenSSL/GnuTLS 后端要自己提 SAN/CN 做主机名匹配，而 Apple/Windows 不用？
**答案**：四个后端都能让库验证证书链，但「主机名匹配」的内置程度不同。SecureTransport 与 Schannel 把主机名验证整合在凭据验证流程里（配置好标志位即可，Schannel 还会在 `InitializeSecurityContext` 时传入主机名参数）；而 OpenSSL 传统上需要应用层自己做（历史上也可用 `SSL_set1_host`，aria2 选择手工实现），GnuTLS 亦然。aria2 把匹配逻辑统一写在 `net::verifyHostname`（[src/SocketCore.cc:1536](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1536-L1569)），两个手工后端共用，保证「带 SAN 时忽略 CN」等细节跨后端一致。

**练习 2**：`--min-tls-version=TLSv1.3` 在四个后端上分别会发生什么？
**答案**：`toTLSVersion` 返回 `TLS_PROTO_TLS13`（[src/util.cc:2479-2480](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2479-L2480)）。OpenSSL 后端在构造时叠加 `SSL_OP_NO_TLSv1_2`（且仅在编译环境有 `TLS1_3_VERSION` 宏时才有这个 case，[src/LibsslTLSContext.cc:116-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSContext.cc#L116-L120)）；GnuTLS 把 minVer 存下来交给会话层；Apple 在会话层映射协议枚举；**Windows 后端没有 TLS1.3 的 case，直接 `default: abort()`**（[src/WinTLSContext.cc:84-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSContext.cc#L84-L86)）——这是矩阵里「WinTLS 不支持 TLS 1.3」的源码证据。

**练习 3**：WinTLS 为什么需要 `writeBuf_`/`readBuf_` 这样的自管缓冲，而 OpenSSL 后端不需要？
**答案**：OpenSSL 的 `SSL_connect` 直接绑定 socket fd（`SSL_set_fd`，[src/LibsslTLSSession.cc:73-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L73-L85)），库内部替你收发并记住中间状态，遇到非阻塞就返回 WANT_READ/WRITE。Schannel 的 `InitializeSecurityContext` 却是「纯计算」：吐出一段该发送的数据、或要求你补充一段收到的数据，网络 I/O 一概不管。于是 [src/WinTLSSession.cc:628-648](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L628-L648) 的握手状态机必须自己 `send()` 写缓冲、[src/WinTLSSession.cc:660-693](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WinTLSSession.cc#L660-L693) 自己 `recv()` 填读缓冲，用 `st_handshake_write`/`st_handshake_read` 状态显式记录「下一步该收还是该发」。

### 4.4 模块四：FeatureConfig 能力上报与平台兼容层

#### 4.4.1 概念说明

TLS 后端是编译期决定的，用户（和排查问题的你）需要一个运行期窗口看清「这个二进制到底链了谁」。这就是 `FeatureConfig` 的职责：它把 `configure` 探测结果产生的编译宏，翻译成 `aria2c --version` 里两行人类可读的输出——`Enabled Features` 与 `Libraries`。这套机制在 u1-l1/u1-l2 已建立（「功能与第三方库一一绑定」），本讲落到 TLS 的具体呈现。

顺带认识 `a2io.h`/`a2netcompat.h` 这层「平台方言翻译」：TLS 抽象接口里出现的 `sock_t`、`ssize_t` 等类型，都是这层兼容层统一出来的。

#### 4.4.2 核心流程

```text
configure 探测 → AC_DEFINE 编译宏（config.h）→ 条件编译进 FeatureConfig.cc
    ENABLE_SSL          → Features 里的 "HTTPS"
    HAVE_APPLETLS       → Libraries 里的 "AppleTLS"
    HAVE_WINTLS         → Libraries 里的 "WinTLS"
    HAVE_LIBGNUTLS      → Libraries 里的 "GnuTLS/<版本>"
    HAVE_OPENSSL        → Libraries 里的 "OpenSSL/<版本>"

aria2c --version（version_usage.cc::showVersion）
    "Enabled Features": featureSummary()   ← 枚举遍历 + 宏过滤
    "Libraries":       usedLibs()          ← 宏过滤 + 版本号拼接
启动日志（Context 构造）也会 A2_LOG_INFO(usedLibs())
```

#### 4.4.3 源码精读

特性枚举只有九项（[src/FeatureConfig.h:47-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.h#L47-L58)）。`strSupportedFeature` 是「一特性一函数分支」的查表：HTTPS 特性分支在 [src/FeatureConfig.cc:150-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L150-L156)——编译宏 `ENABLE_SSL` 存在就返回 `"HTTPS"`，否则返回 `nullptr` 表示「此构建无此特性」。`featureSummary`（[src/FeatureConfig.cc:96-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L96-L113)）就是遍历这张表把非空项用逗号拼起来。

TLS 后端的归属上报在 `usedLibs`：四个后端宏各自拼一段（[src/FeatureConfig.cc:207-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L207-L224)）——AppleTLS/WinTLS 没有版本号概念只报名字；GnuTLS 用 `GNUTLS_VERSION` 字符串；OpenSSL 的版本号是一个长整型位域，代码把它逐段位移拆出主/次/修版本与字母后缀。打印出口是 [src/version_usage.cc:53-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L53-L82) 的 `showVersion`，另外 [src/Context.cc:185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L185-L185) 在进程启动时也把 `usedLibs()` 写进日志——所以**翻旧日志也能知道当时的二进制链的是哪个后端**。

平台兼容层的样例：`sock_t` 在 Windows 是 `SOCKET`（句柄型）、否则是 `int`（[src/a2netcompat.h:95-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2netcompat.h#L95-L99)）；mingw 下 `a2_sockopt_t` 要换成 `char*`（[src/a2netcompat.h:46-55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2netcompat.h#L46-L55)）。`TLSSession::init(sock_t sockfd)` 的签名因此才能在两个世界通用。这与 TLS 后端矩阵是同一主题的两半：**后端差异走条件编译 + 链接期单选，平台差异走宏翻译 + 类型别名**。

#### 4.4.4 代码实践：configure 输出与 --version 互证

1. **实践目标**：验证「configure 汇总表、`--version` 输出、源码宏」三方同源。
2. **操作步骤**：
   ```bash
   ./configure > configure.log 2>&1   # 或翻看你构建时留下的日志
   grep -iE "openssl|gnutls|tls" configure.log | head -20
   ./src/aria2c --version | grep -A1 "Enabled Features"
   ```
3. **需要观察的现象**：configure 末尾的汇总表里 TLS 相关行（如 `OpenSSL: yes` 或 `GnuTLS: yes`）与你构建出的 `aria2c --version` 的 `Libraries` 行一致；Features 行含 `HTTPS` 当且仅当 TLS 后端启用。
4. **预期结果**：三处信息互相印证。若你按 u1-l2 的方式用 `--without-*` 裁剪过依赖，这里的差异会直接显现；不同机器编译的二进制输出可能不同。具体输出内容待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：一个二进制能同时上报 GnuTLS 和 OpenSSL 吗？能同时用两者做 TLS 吗？
**答案**：`usedLibs` 是条件宏拼接，`HAVE_LIBGNUTLS` 与 `HAVE_OPENSSL` 同时定义时两段都会出现，所以**上报层面可以共存**（例如 OpenSSL 用于消息摘要、GnuTLS 用于 TLS 的构建）。但 TLS 抽象的 `make` 工厂在链接期只能解析到一份定义，**做 TLS 的后端永远只有一个**；macOS 上甚至会出现「Libraries 报了 OpenSSL、TLS 实际走 AppleTLS」的组合（Makefile.am 的 `if !HAVE_APPLETLS` 保证了这一点）。

**练习 2**：用户报障说「aria2 下载 HTTPS 报 handshake failure」，但没说是哪个平台的二进制，你的第一句话问什么？
**答案**：让他跑 `aria2c --version`。`Libraries` 行直接给出 TLS 后端与版本（OpenSSL/GnuTLS/AppleTLS/WinTLS），`Enabled Features` 确认 HTTPS 是否启用；再让他用 `-l xxx.log --log-level=debug` 复现，启动日志第一段就有 `usedLibs()` 的输出（[src/Context.cc:185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L185-L185)），握手失败的 `handshakeErr` 内容也因后端而异（例如 GnuTLS 会给出逐项状态位拼接的串）。后端不同，排查路径完全不同。

**练习 3**：为什么 `FEATURE_HTTPS` 映射的宏叫 `ENABLE_SSL` 而不是 `HAVE_OPENSSL`？
**答案**：`ENABLE_SSL` 是「具备 HTTPS 能力」这一**功能**的语义宏，与具体后端解耦；`HAVE_OPENSSL` 只是「探测到 OpenSSL 库」这一**依赖**事实。四个后端无论哪个被启用都会定义 `ENABLE_SSL`（这正是 u1-l2 讲过的「特性推导」）。把功能宏与依赖宏分层，是让「同一功能、多后端」在构建系统里保持可组合的关键——如果特性判断写死 `HAVE_OPENSSL`，GnuTLS 构建的 aria2 就会错误地报告不支持 HTTPS。

## 5. 综合实践：自签名证书攻防实验

把本讲四个模块串成一个完整实验：亲手搭一个自签名 HTTPS 服务，观察 `--check-certificate` 开关在源码验证分支上的真实效果，并在验证分支加日志。

1. **实践目标**：
   - 确认本机 aria2 的 TLS 后端（模块四）。
   - 观察验证开关如何改变同一条握手链路的结局（模块二）。
   - 亲手在 OpenSSL 后端验证分支加日志（模块一/三）。
2. **准备材料**：装有 openssl 命令行工具的环境、可运行的 aria2c（按 u1-l2 构建）。
3. **操作步骤**：
   ```bash
   # ① 确认后端
   ./src/aria2c --version | sed -n '/Enabled Features/,+2p'

   # ② 生成自签名证书并起一个本地 HTTPS 服务（-www 会回一个状态页）
   mkdir -p /tmp/tls-lab && cd /tmp/tls-lab
   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 1 -nodes \
          -subj "/CN=localhost"
   openssl s_server -accept 18443 -cert cert.pem -key key.pem -www &

   # ③ 默认（--check-certificate=true）下载
   ./src/aria2c -d /tmp/tls-lab -o default.out https://localhost:18443/ ; echo "exit=$?"

   # ④ 关闭验证再试
   ./src/aria2c --check-certificate=false -d /tmp/tls-lab -o nocheck.out \
          https://localhost:18443/ ; echo "exit=$?"
   ```
   然后**改源码加日志**：在 [src/LibsslTLSSession.cc:234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234-L235) 的 `if` 之前插入一行（示例代码，非项目原有代码）：
   ```cpp
   A2_LOG_INFO(fmt("tlsConnect: verifyPeer=%s hostname=%s",
                   tlsContext_->getVerifyPeer() ? "true" : "false",
                   hostname.c_str()));
   ```
   重新 `make` 后重复 ③④。
4. **需要观察的现象**：
   - ③ 应当失败，退出码非 0，错误信息形如 `SSL/TLS handshake failure: ... self ... certificate`（由 [src/SocketCore.cc:1017-1022](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1017-L1022) 抛出，`handshakeErr` 来自 OpenSSL 后端「三连问」的第二问）。
   - ④ 应当成功落盘，且新增日志行恒为 `verifyPeer=false`——证明关掉开关后整个验证分支被跳过。
   - 加日志后，③ 的日志行是 `verifyPeer=true`，随后紧接 `SSL_get_verify_result` 的失败路径。
5. **预期结果**：一次开关切换，恰好对应 [src/LibsslTLSSession.cc:234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslTLSSession.cc#L234-L235) 那道 `if` 闸门的开与关；若你的后端是 GnuTLS，现象等价，但失败串换成 GnuTLS 状态位拼接的长句（见 [src/LibgnutlsTLSSession.cc:268-296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L268-L296)）。具体输出文本待本地验证。
6. **思考题**：为什么 ④ 成功后文件内容是 HTML 状态页？`localhost` 没有点号，SNI 被跳过了（[src/SocketCore.cc:937](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L937-L938)），这对单证书的 `s_server` 有影响吗？

## 6. 本讲小结

- aria2 把 TLS 拆成**进程级配置**（`TLSContext`：证书、信任库、验证开关）与**连接级会话**（`TLSSession`：握手、加解密、方向查询）两个抽象，头文件注释明言这就是新后端的插槽。
- 四个后端各自定义同一个 `TLSContext::make`/`TLSSession::make`，靠 Makefile.am 的互斥条件块实现**链接期单选**，再经虚接口获得运行期多态——「选择」被压缩进构建系统，运行期代码零分支。
- 握手由 `SocketCore::tlsHandshake` 三状态机驱动：`WOULDBLOCK` → `checkDirection()` → `wantRead_/wantWrite_` → EventPoll 注册 → 命令 requeue，把 TLS 无缝嫁接到 u2-l2 的事件循环上。
- `--check-certificate` 落到 `setVerifyPeer`：OpenSSL/GnuTLS 后端在 `tlsConnect` 里做**手工三连问**（有证书？链合法？名字匹配？），Apple/Windows 后端把验证交给系统库只配开关；四家对 CA bundle 文件的支持、TLS 1.3 支持各不相同，WinTLS 甚至要自管缓冲手写握手状态机。
- `FeatureConfig` 把编译宏翻译成 `--version` 的 `Enabled Features`/`Libraries` 两行输出，`ENABLE_SSL` 是功能宏、`HAVE_OPENSSL` 等是依赖宏，两层分离让多后端可组合；`a2io.h`/`a2netcompat.h` 则用类型别名弥合平台方言。

## 7. 下一步学习建议

本讲补全了 u9 工程实践单元的平台维度。接下来：

1. **u9-l5（测试体系）**：`test/` 目录下有各 TLS 后端的会话级测试（如 `LibsslTLSSessionTest` 等，可 `ls test | grep -i tls` 确认），学习如何在没有真实网络的情况下用内存 socket 驱动一次握手。
2. **u9-l6（二次开发实战）**：本讲的「加一行日志改行为」是最小的二次开发；u9-l6 会走完「注册新选项 → 引擎读取 → 行为生效」的完整改动链路。
3. **回看 u8-l3（内嵌服务器）**：`TLS_SERVER` 侧的 context 服务的正是 `--rpc-secure` 的 RPC over HTTPS，现在你可以顺着 `SocketCore::setServerTLSContext` → `tlsAccept` 把服务器侧握手链路也走一遍，与 u8-l3 的 HttpListenCommand 命令链对接。
4. 若想继续深挖安全面：对照 [src/LibgnutlsTLSSession.cc:253-264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgnutlsTLSSession.cc#L253-L264) 的 EKU 验证提交（`git show 64365cf3`）阅读一次真实的安全加固是如何进入四个后端之一的。
