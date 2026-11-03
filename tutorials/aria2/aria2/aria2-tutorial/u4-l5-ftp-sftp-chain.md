# FTP 与 SFTP：两套文件传输命令链

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序复述一次 FTP 下载在控制连接上发生的完整协商流程：`USER → PASS → TYPE → PWD → CWD → (MDTM) → SIZE → (PASV/EPSV 或 PORT/EPRT) → REST → RETR → 226`，并说出每条命令期望的应答码。
2. 解释主动模式（PORT/EPRT）与被动模式（PASV/EPSV）对「数据连接由谁发起」的影响，以及 aria2 在两种模式下命令链的差异。
3. 对比 `SftpNegotiationCommand` 与 FTP 协商链的异同：SFTP 没有第二条连接，握手/认证/打开文件/取大小/定位这五步全部在一条 SSH 连接内完成，且每一步都遵循 libssh2 的非阻塞 EAGAIN 约定。
4. 理解 `FtpConnection` 作为「协议编解码器」与 `FtpNegotiationCommand` 作为「协议状态机」的职责分离。

本讲承接 u4-l1（HTTP 命令链与命令接力模型）与 u4-l4（代理与 CONNECT 隧道）。阅读本讲前，请先确认你已理解：Command 的 `execute()` 返回 `true` 即销毁、返回 `false` 必须自我 requeue；`AbstractCommand` 通过 `setReadCheckSocket` / `setWriteCheckSocket` 向 EventPoll 登记关注的 I/O 事件；资源（Socket、Request、Segment）以构造函数参数在命令间接力传递。

## 2. 前置知识

### 2.1 FTP：双通道文本协议

FTP（RFC 959）与 HTTP 最大的不同是它用**两条 TCP 连接**：

- **控制连接**：贯穿整个会话，承载命令与应答。命令是一行文本（如 `USER anonymous`），应答以三位数字开头（如 `331 Password required`）。我们看到的 `USER/PASS/TYPE/PASV/RETR` 全部走这条连接。
- **数据连接**：只在传输文件内容（或目录列表）时临时建立，传完即关。文件字节流走这条连接。

应答码的分类规则：第一位表示类别（1xx 预备、2xx 成功、3xx 需要后续信息、4xx 临时错误、5xx 永久错误）。本讲会反复遇到：`220`（服务就绪）、`230`（登录成功）、`331`（需要密码）、`200`（命令成功）、`257`（PWD 结果）、`213`（SIZE/MDTM 结果）、`227`（进入被动模式）、`229`（EPSV 结果）、`350`（REST 接受）、`150/125`（数据连接已打开、开始传输）、`226`（传输完成）、`550`（文件不存在）。

FTP 应答还可能是**多行**的：如果应答第 4 个字符是 `-`（如 `220-Welcome\r\n220 Ready\r\n`），客户端必须继续读到出现以同一编号开头的结束行为止。

### 2.2 主动模式与被动模式

「第二条数据连接由谁发起」有两种答案：

- **主动模式（PORT/EPRT）**：客户端先在本地开一个监听 socket，然后用 `PORT h1,h2,h3,h4,p1,p2`（IPv4）或 `EPRT |1|ip|port|`（含 IPv6）把这个地址告诉服务器，**服务器反过来连接客户端**。这在客户端位于 NAT/防火墙之后时通常失败。
- **被动模式（PASV/EPSV）**：客户端发 `PASV`，服务器应答 `227 Entering Passive Mode (h1,h2,h3,h4,p1,p2)`，把数据连接的监听地址告诉客户端，**客户端主动连过去**。EPSV 是它的 IPv6 友好版本，应答形如 `229 Entering Extended Passive Mode (|||port|)`，只给端口不给地址（地址复用控制连接的对端）。

aria2 默认使用被动模式（`--ftp-pasv` 默认 `true`，见 [src/OptionHandlerFactory.cc:1240](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1240)），且对 IPv6 控制连接自动优先 EPSV/EPRT，失败再回退 PASV/PORT。

### 2.3 SFTP：不是「FTP over SSH」

初学者最常见的误解是以为 SFTP 是「给 FTP 套一层加密」。实际上 SFTP（SSH File Transfer Protocol）是一个**独立的二进制协议**，运行在 SSH 连接内部：

- 整个会话只有**一条 TCP 连接**，握手、认证、打开文件、读数据、关闭文件全部复用它；
- 没有控制/数据双通道，因此**不存在 PASV/PORT 的问题**；
- 「请求-应答」不再是一行文本，而是长度前缀 + 包体的二进制包（libssh2 帮我们处理了这些细节）；
- 「断点续传」不再用 `REST` 命令，而是对已打开的远端文件句柄执行 seek。

对源码阅读者来说，这意味着 SFTP 的协商状态比 FTP 少得多——aria2 里 FTP 协商链有 30 多个状态，SFTP 只有 6 个。

### 2.4 非阻塞 libssh2 的 EAGAIN 约定

libssh2 被初始化为非阻塞模式后，任何操作都可能返回 `LIBSSH2_ERROR_EAGAIN`，含义是「数据还没到/还没发出去，请稍后再试」。更麻烦的是，「稍后再试」之前你需要知道该等读事件还是写事件——libssh2 用 `libssh2_session_block_directions()` 告诉你方向。aria2 把这套约定封装成了三值返回：`SSH_ERR_OK`（完成）/ `SSH_ERR_WOULDBLOCK`（未完成，等事件）/ `SSH_ERR_ERROR`（出错抛异常），见 [src/SSHSession.cc:135-142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L135-L142)。

### 2.5 SFTP 依赖 libssh2，FTP 是内置的

FTP 支持编译在 aria2 内核里；SFTP 则依赖可选的 libssh2，由 `HAVE_LIBSSH2` 宏守门——协议工厂只在定义了该宏时才接受 `sftp://` URI，见 [src/InitiateConnectionCommandFactory.cc:73-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc#L73-L77)。这正是 u1-l2 讲过的「configure 探测 → 编译宏 → 条件编译」链条在协议层的又一次体现。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/InitiateConnectionCommandFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc) | 按 URI 协议选择第一个 Initiate 命令：http/https 走 HTTP 链，ftp/sftp 统一走 FTP 链 |
| [src/FtpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc) | DNS 解析完成后决定下一棒：查连接池、建新连接、按协议装配 ControlChain |
| [src/FtpConnection.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc) | FTP 控制协议编解码器：发送各条命令、解析应答码与应答体（本讲模块一） |
| [src/FtpNegotiationCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.h) | FTP 协商状态机：Seq 枚举驱动整个 RFC 959 会话（本讲模块二核心） |
| [src/FtpNegotiationConnectChain.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationConnectChain.h) | TCP 连接建立成功后创建 FtpNegotiationCommand 的「交接钩子」 |
| [src/FtpDownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpDownloadCommand.cc) | 数据连接上的数据泵，复用 u4-l1 讲过的 DownloadCommand |
| [src/FtpFinishDownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpFinishDownloadCommand.cc) | 数据收完后回到控制连接等 `226`，并把连接放回池 |
| [src/SftpNegotiationCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.h) | SFTP 协商状态机：握手→认证→打开→取属性→定位（本讲模块三核心） |
| [src/SftpDownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpDownloadCommand.cc) | SFTP 单连接数据泵（含关键的写检查补偿逻辑） |
| [src/SftpFinishDownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpFinishDownloadCommand.cc) | 关闭远端文件句柄并把 SSH 连接放回池 |
| [src/SocketCore.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc)（`ssh*` 系列方法） | 把 SSHSession 的三值返回翻译成 SocketCore 的 `wantRead_/wantWrite_` 约定 |
| [src/SSHSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc) | libssh2 的薄封装：非阻塞初始化、握手、认证、SFTP open/stat/seek/read |

## 4. 核心概念与源码讲解

### 4.1 FtpConnection：FTP 控制协议的编解码器

#### 4.1.1 概念说明

`FtpConnection` 不是一个 Command，不参与引擎调度。它是纯粹的**协议编解码器**：把「发一条 USER 命令」「解析一条 PASV 应答」这类协议动作封装成函数，供状态机（FtpNegotiationCommand）逐个调用。

这样分离的好处：

- 状态机只关心「下一步该发什么、应答码是什么才继续」，不必关心文本拼装与字节流解析；
- `FtpConnection` 持有 `SocketBuffer socketBuffer_`（发送缓冲，u2-l4 讲过它的部分写语义）和 `strbuf_`（接收缓冲），可以独立处理「一条应答分多个 TCP 段到达」「多条应答粘在一个 TCP 段里」的粘包/半包问题。

#### 4.1.2 核心流程

**发送侧**（所有 `sendXxx` 共用同一个模板）：

```text
若发送缓冲为空：
    拼装命令文本（如 "USER anonymous\r\n"）
    记一条 INFO 日志
    push 进 SocketBuffer
调用 socketBuffer_.send() 尝试发出（非阻塞，可能只发了一半）
返回「缓冲是否已清空」——true 表示这条命令发送完毕
```

**接收侧**（`bulkReceiveResponse`）：

```text
循环 readData 读 1KB，直到 EWOULDBLOCK（socket 置 wantRead_/wantWrite_）
把读到的字节追加进 strbuf_
用 getStatus() 从 strbuf_ 头部解析三位应答码
用 findEndOfResponse() 判断完整应答（单行/多行）是否已到齐
已到齐：从 strbuf_ 头部切出这条应答，剩余字节留在 strbuf_ 等下次解析
未到齐：返回 false，等下一次读事件
```

命令与期望应答码对照表（全部实现于 [src/FtpConnection.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc)）：

| 命令 | 发送函数（行号） | 期望应答 | 应答解析函数 |
| --- | --- | --- | --- |
| `USER` | `sendUser` [L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L81-L92) | 230（免密登录）/ 331（要密码） | `receiveResponse` |
| `PASS` | `sendPass` [L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L94-L105) | 230 | `receiveResponse` |
| `TYPE` | `sendType` [L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L107-L118) | 200 | `receiveResponse` |
| `PWD` | `sendPwd` [L120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L120-L129) | 257 | `receivePwdResponse` [L522](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L522-L544) |
| `CWD` | `sendCwd` [L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L131-L142) | 250 | `receiveResponse` |
| `MDTM` | `sendMdtm` [L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L144-L156) | 213（失败可容忍） | `receiveMdtmResponse` [L424](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L424-L460) |
| `SIZE` | `sendSize` [L158](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L158-L170) | 213（失败则长度未知） | `receiveSizeResponse` [L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L384-L402) |
| `EPSV` | `sendEpsv` [L172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L172-L181) | 229 | `receiveEpsvResponse` [L462](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L462-L492) |
| `PASV` | `sendPasv` [L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L183-L192) | 227 | `receivePasvResponse` [L494](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L494-L520) |
| `EPRT` | `sendEprt` [L203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L203-L215) | 200（失败回退 PORT） | `receiveResponse` |
| `PORT` | `sendPort` [L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L217-L233) | 200 | `receiveResponse` |
| `REST` | `sendRest` [L235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L235-L246) | 350 | `receiveResponse` |
| `RETR` | `sendRetr` [L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L248-L260) | 150 / 125 | `receiveResponse` |

#### 4.1.3 源码精读

**（1）发送模板：以 sendUser 为例**

```cpp
bool FtpConnection::sendUser()
{
  if (socketBuffer_.sendBufferIsEmpty()) {
    std::string request = "USER ";
    request += authConfig_->getUser();
    request += "\r\n";
    A2_LOG_INFO(fmt(MSG_SENDING_REQUEST, cuid_, "USER ********"));
    socketBuffer_.pushStr(std::move(request));
  }
  socketBuffer_.send();
  return socketBuffer_.sendBufferIsEmpty();
}
```

这段代码（[src/FtpConnection.cc:81-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L81-L92)）做了三件事：拼命令文本、打日志（注意用户名在日志里被脱敏为 `USER ********`）、非阻塞发送并汇报「是否发完」。函数返回 `false` 时，状态机会给自己注册写事件，下轮再来调一次 `sendUser`——此时缓冲非空，跳过拼装直接续发。所有 `sendXxx` 都是这个形状。

`TYPE` 命令的取值由 `--ftp-type` 选项决定：`V_ASCII` 发 `'A'`，否则发 `'I'`（二进制，默认值，见 [src/OptionHandlerFactory.cc:1257-1259](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1257-L1259)），对应 [src/FtpConnection.cc:107-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L107-L118)。文件下载必须用二进制模式，否则换行符转换会破坏内容和长度。

**（2）应答码解析：getStatus 与 findEndOfResponse**

```cpp
int FtpConnection::getStatus(const std::string& response) const
{
  int status;
  // When the response is not like "%d %*s", we return 0.
  if (response.find_first_not_of("0123456789") != 3 ||
      !(response.find(" ") == 3 || response.find("-") == 3)) {
    return 0;
  }
  if (sscanf(response.c_str(), "%d %*s", &status) == 1) { ... }
}
```

[src/FtpConnection.cc:262-277](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L262-L277) 的校验逻辑是：前三个字符必须都是数字（`find_first_not_of` 返回 3），第 4 个字符必须是空格（单行应答）或 `-`（多行应答）。不满足就返回 0，表示「还没法解析」或「非法应答」。

`findEndOfResponse`（[src/FtpConnection.cc:279-314](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L279-L314)）处理多行应答：当第 4 字符是 `-` 时，在缓冲中查找 `\r\n<status> ` 开头的结束行，找到才返回应答总长度；单行应答则只需找到第一个 `\r\n`。

**（3）粘包处理：bulkReceiveResponse**

[src/FtpConnection.cc:316-358](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L316-L358) 是接收侧的核心。两个细节值得注意：

- 读循环在 `readData` 返回 0 且 socket 置了 `wantRead()/wantWrite()` 时 break（等下轮事件），只有真正 EOF 时才抛 `EX_GOT_EOF`；
- 应答切走后 `strbuf_.erase(0, length)` 保留残余字节——如果服务器把 `230` 和下一条应答的头部一起发来，残余部分会在下一次调用时被继续解析。这是流式协议解析的标准做法，与 u4-l2 讲过的 HttpHeaderProcessor「记住已消费字节数」异曲同工，只是这里直接用 erase 完成。

**（4）PASV 应答解析：receivePasvResponse**

```cpp
if (response.first == 227) {
  int h1, h2, h3, h4, p1, p2;
  std::string::size_type p = response.second.find("(");
  if (p >= 4) {
    sscanf(response.second.c_str() + p, "(%d,%d,%d,%d,%d,%d).", ...);
    dest.first = fmt("%d.%d.%d.%d", h1, h2, h3, h4);
    dest.second = 256 * p1 + p2;
  }
```

[src/FtpConnection.cc:494-520](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L494-L520) 解析 `227 Entering Passive Mode (h1,h2,h3,h4,p1,p2).`，端口号按协议规定是 `p1 * 256 + p2`。EPSV 的对应解析（[L462-492](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpConnection.cc#L462-L492)）则从 `(|||port|)` 里按 `|` 切出第 4 段作为端口，并且**丢弃应答里的地址**（若有），因为 RFC 2428 规定 EPSV 客户端应复用控制连接的对端地址。

#### 4.1.4 代码实践

**实践目标**：在不运行 aria2 的前提下，手工扮演一次 FTP 客户端，验证你对命令与应答格式的理解；再用 `telnet`/`nc` 观察真实服务器的应答文本。

**操作步骤**（源码阅读 + 可选联网观察）：

1. 对照上面的命令表，把 `sendUser` 到 `sendRetr` 的每个函数读一遍，注意哪些命令的参数做了 `util::percentDecode`（URL 解码）——为什么 `CWD`/`RETR`/`SIZE`/`MDTM` 需要，而 `USER`/`PASS` 不需要？
2. （可选，待本地验证）找一个可达的 FTP 服务器，执行 `telnet <host> 21` 或 `nc <host> 21`，观察服务器主动发来的 `220` 问候语多行文本，对照 `findEndOfResponse` 的多行判断规则。
3. 构造一段「粘包」输入：把 `230 Login successful\r\n257 "/" is cwd\r\n` 写下来，手工模拟 `bulkReceiveResponse` 两次调用的返回值。

**需要观察的现象 / 预期结果**：

- 步骤 1 中你会发现 `CWD`/`RETR` 等携带路径的命令做了 percentDecode，因为路径来自 URL（可能含 `%20` 等编码），而用户名密码来自 AuthConfig（u4-l3 讲过其来源），已经是明文。
- 步骤 3 中第一次调用应切出 `230` 那行并返回 `true`，第二次切出 `257` 那行；`strbuf_` 最终为空。联网观察部分的具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`sendType` 发送 `'A'` 还是 `'I'` 由什么决定？为什么下载二进制文件必须用 `'I'`？

答案：由选项 `--ftp-type`（`PREF_FTP_TYPE`）决定，`V_ASCII` 时发 `'A'`，默认 `V_BINARY` 发 `'I'`。ASCII 模式下服务器会对换行符做 CRLF/LF 转换，字节内容与长度都会改变，校验和与断点续传的偏移都会失效，因此文件下载必须用二进制模式。

**练习 2**：如果服务器应答 `220-Welcome\r\nmore text\r\n220 Ready\r\n`，`getStatus` 返回什么？`findEndOfResponse` 如何知道应答已完整？

答案：`getStatus` 返回 220（前 3 字符是数字，第 4 字符是 `-`，仍可解析出状态码）。`findEndOfResponse` 看到第 4 字符为 `-` 判定为多行应答，于是查找 `\r\n220 `（即 `\r\n` + 状态码 + 空格）开头的结束行，再找到该行结尾的 `\r\n`，返回包含它的总长度。

**练习 3**：为什么 `FtpConnection` 的所有发送函数都返回「缓冲是否为空」而不是 void？

答案：因为发送是非阻塞的：一次 `send()` 可能只发出半条命令。返回 false 告诉调用方（状态机）「还没发完」，状态机保持当前状态、给自己注册写事件，下轮事件循环再次调用同一发送函数续发（此时 `sendBufferIsEmpty()` 为 false，不会重复拼装命令）。

### 4.2 FTP 命令链（上）：从连接建立到 RETR 的协商接力

#### 4.2.1 概念说明

FTP 下载的命令链在结构上与 u4-l1 的 HTTP 链完全同构，只是「中间那一棒」更复杂：

```text
CreateRequestCommand
  → FtpInitiateConnectionCommand   （DNS 解析、查连接池）
    → ConnectCommand               （等待 TCP 连接建立，经 ControlChain 交接）
      → FtpNegotiationCommand      （FTP 协议协商状态机，本讲主角）
        → FtpDownloadCommand       （数据连接上的数据泵）
          → FtpFinishDownloadCommand （等 226、回收连接）
```

关键角色分工：

- `FtpInitiateConnectionCommand` 决定「这条连接从哪来」：连接池复用还是新建；
- `FtpNegotiationCommand` 决定「这条连接说到哪一步了」：用 `Seq` 枚举记录协商进度，每次被引擎调度就向前推进零到多步；
- 协商完成的标志是创建 `FtpDownloadCommand` 并自我销毁。

#### 4.2.2 核心流程

`FtpNegotiationCommand` 的 `Seq` 枚举（[src/FtpNegotiationCommand.h:50-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.h#L50-L94)）完整刻画了 RFC 959 会话。一次被动模式、已启用 `--remote-time` 的全新连接，状态推进如下：

```text
SEQ_RECV_GREETING      等服务器 220 问候
SEQ_SEND_USER / RECV_USER    发 USER，230 → 跳过 PASS；331 → 发 PASS
SEQ_SEND_PASS / RECV_PASS    等 230
SEQ_SEND_TYPE / RECV_TYPE    等 200
SEQ_SEND_PWD  / RECV_PWD     等 257，记下服务器根目录（baseWorkingDir）
SEQ_SEND_CWD_PREP            把 URL 路径按 '/' 拆成目录队列（baseWorkingDir 打头）
SEQ_SEND_CWD / RECV_CWD      逐目录 CWD，全部 250 后：
                               --remote-time=true → SEQ_SEND_MDTM
                               否则               → SEQ_SEND_SIZE
SEQ_SEND_MDTM / RECV_MDTM    等 213，取 mtime（失败仅记日志，可继续）
SEQ_SEND_SIZE / RECV_SIZE    等 213 拿文件长度 → 进入文件准备（见 4.2.3）
SEQ_PREPARE_PASV             按控制连接地址族选择 EPSV（IPv6）或 PASV（IPv4）
SEQ_SEND_EPSV/PASV → RECV    解析出数据端口
                               （若配置了代理：SEQ_RESOLVE_PROXY →
                                SEQ_SEND_TUNNEL_REQUEST → SEQ_RECV_TUNNEL_RESPONSE，
                                数据连接也穿 CONNECT 隧道，见 u4-l4）
SEQ_SEND_REST_PASV           确认数据连接建立无误后走 REST
SEQ_SEND_REST / RECV_REST    发 REST <offset>，等 350
SEQ_SEND_RETR / RECV_RETR    等 125/150（数据即将到来）
SEQ_NEGOTIATION_COMPLETED    创建 FtpDownloadCommand，本命令销毁
```

主动模式的差异只在数据连接的建立段：

```text
SEQ_PREPARE_PORT             IPv6 → EPRT 路线，IPv4 → PORT 路线
SEQ_PREPARE_SERVER_SOCKET    本地 bind+listen 建监听 socket
SEQ_SEND_EPRT/PORT → RECV    把本地地址告诉服务器（EPRT 失败可回退 PORT）
SEQ_SEND_REST → RECV_REST    同上
SEQ_SEND_RETR → RECV_RETR    等 125/150
SEQ_WAIT_CONNECTION          调 serverSocket_->acceptConnection() 接受服务器
                              反向发起的连接，得到 dataSocket_
SEQ_NEGOTIATION_COMPLETED
```

还有一个「快进」入口：当连接从池里取回（已经登录过）时，`FtpNegotiationCommand` 直接以 `SEQ_SEND_CWD_PREP` 构造，跳过 USER/PASS/TYPE/PWD 四步。

#### 4.2.3 源码精读

**（1）协议分派：ftp 与 sftp 共用一条入口链**

[src/InitiateConnectionCommandFactory.cc:73-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc#L73-L84)：`ftp`（及 `HAVE_LIBSSH2` 下的 `sftp`）URI 都交给 `FtpInitiateConnectionCommand`，且要求 URI 必须含文件路径，否则抛出 `FTP/SFTP URI ... doesn't contain file path.`。这是「SFTP 借用 FTP 的链路骨架」的第一个证据。

**（2）FtpInitiateConnectionCommand：连接池与 ControlChain**

[src/FtpInitiateConnectionCommand.cc:170-222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L170-L222) 的 `createNextCommandPlain`（不走代理的路径）分两支：

```cpp
std::shared_ptr<SocketCore> pooledSocket =
    getDownloadEngine()->popPooledSocket(
        options, resolvedAddresses, getRequest()->getPort(), ...getUser());

if (!pooledSocket) {
  // 新建连接：ConnectCommand + ControlChain
  auto c = make_unique<ConnectCommand>(...);
  if (getRequest()->getProtocol() == "sftp") {
    c->setControlChain(std::make_shared<SftpNegotiationConnectChain>());
  } else {
    c->setControlChain(std::make_shared<FtpNegotiationConnectChain>());
  }
  ...
}
// 池命中：sftp → SftpNegotiationCommand(SEQ_SFTP_OPEN)
//         ftp  → FtpNegotiationCommand(SEQ_SEND_CWD_PREP, options)
```

两个要点：

- 连接池的 key 包含**用户名**（`createAuthConfig(...)->getUser()`），因为同一个服务器上不同账号的登录状态不同；`options` 出参带回池中连接记录的 `baseWorkingDir`，使复用连接能跳过 PWD、直接从正确的目录开始 CWD——回池的对称代码见 [src/FtpNegotiationCommand.cc:997-1005](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L997-L1005)（`--ftp-reuse-connection` 控制）。
- 新连接统一先造 `ConnectCommand`（u2-l3 讲过它等待非阻塞 connect 完成），再用 `ControlChain` 这个小钩子决定「连上之后交给谁」。[src/FtpNegotiationConnectChain.h:45-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationConnectChain.h#L45-L58) 的实现只有一件事：创建 `FtpNegotiationCommand`（默认 `SEQ_RECV_GREETING`），设 `STATUS_ONESHOT_REALTIME` 并 `setNoWait(true)`，让引擎立即调度它而不必等下一轮事件循环。`SftpNegotiationConnectChain`（[src/SftpNegotiationConnectChain.h:45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationConnectChain.h#L45-L56)）与之对称，创建 `SftpNegotiationCommand`。

走代理时（`createNextCommandProxied`，[src/FtpInitiateConnectionCommand.cc:80-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L80-L168)）：`ftp` 默认按 u4-l4 讲的 GET 模式转成 `HttpRequestCommand`（FTP over HTTP 代理，命令链就此并入 HTTP 链，本讲的 FTP 状态机不再登场）；`sftp` 恒为隧道模式（源码注释 `sftp always use tunnel mode`），隧道打通后从池里回来时直接造 `SftpNegotiationCommand`。

**（3）executeInternal：状态机的泵**

```cpp
bool FtpNegotiationCommand::executeInternal()
{
  auto segment = getSegments().empty() ? ... ;
  while (processSequence(segment))
    ;
  if (sequence_ == SEQ_RETRY) { return prepareForRetry(0); }
  else if (sequence_ == SEQ_NEGOTIATION_COMPLETED) {
    auto command = make_unique<FtpDownloadCommand>(
        getCuid(), getRequest(), getFileEntry(), getRequestGroup(), ftp_,
        getDownloadEngine(), dataSocket_, getSocket());
    ...
    getDownloadEngine()->addCommand(std::move(command));
    return true;   // 自我销毁
  }
  ...
  else { addCommandSelf(); return false; }   // 未完成，重排等待
}
```

[src/FtpNegotiationCommand.cc:106-151](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L106-L151)：`processSequence`（[L912-995](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L912-L995)）是一个巨大的 switch，把当前 `sequence_` 映射到对应处理函数；处理函数返回 true 表示「这一步完成，可以立刻做下一步」，于是 while 循环连着推进；返回 false 表示「在等网络」，跳出循环后 `addCommandSelf()` 重排。注意 `SEQ_NEGOTIATION_COMPLETED` 分支里创建数据泵命令时同时传了 `dataSocket_`（数据连接，作为数据泵的 socket）和 `getSocket()`（控制连接，转交给 Finish 命令去等 226）——双通道在接力棒里各占一席。

**（4）登录分叉：recvUser**

[src/FtpNegotiationCommand.cc:184-201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L184-L201)：`230` 直接跳到 `SEQ_SEND_TYPE`（匿名服务器已放行），`331` 才进入 `SEQ_SEND_PASS`，其他状态码抛 `FTP_PROTOCOL_ERROR` 类型的可中止异常（走 u2-l3 讲过的 `DlAbortEx` → tryReserved 换 URI 出口）。

**（5）逐目录 CWD 与 550**

[src/FtpNegotiationCommand.cc:285-294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L285-L294) 的 `sendCwdPrep` 把 URL 目录按 `/` 拆进 `cwdDirs_` 队列并以 `baseWorkingDir` 打头（URL 路径以 `/` 开头时拆出的首个元素为空串，`CWD /` 恰好回到根目录）。[L308-337](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L308-L337) 的 `recvCwd` 在非 250 时先 `poolConnection()` 归还连接，再对 `550` 抛 `RESOURCE_NOT_FOUND`（错误码与 HTTP 404 同类，u9-l3 的 error_code 体系）。

**（6）SIZE 与文件准备：onFileSizeDetermined**

`recvSize`（[src/FtpNegotiationCommand.cc:507-550](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L507-L550)）拿到 `213` 后有两条路径：

- 第一条连接（`getPieceStorage()` 为空）：进入 `SEQ_FILE_PREPARATION` 并调用 `onFileSizeDetermined(size)`；
- 后续分段连接（PieceStorage 已存在）：只做 `validateTotalLength` 校验长度一致，然后直接进入 PASV/PORT 段。

`onFileSizeDetermined`（[src/FtpNegotiationCommand.cc:388-505](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L388-L505)）是「长度确定」这一事件的统一处理点，与 HTTP 链里 HttpResponseCommand 定型文件名/长度的职责对等：设置 `FileEntry` 长度与保存路径、`preDownloadProcessing()`（可能挂起进文件预分配/校验队列，即 u3-l1 讲的 SequentialPicker 停车场）、处理 0 长度文件与「本地已有同长度文件」的完成捷径，最后 `getSegmentMan()->getSegmentWithIndex(getCuid(), 0)` 领取 segment——这保证了命令销毁重建后 AbstractCommand 的 segment 刷新逻辑不会拿不到段。

若服务器不支持 SIZE（非 213），`recvSize` 调 `markTotalLengthIsUnknown()` 并以 `onFileSizeDetermined(0)` 进入「长度未知」模式：断点续传与分段下载被禁用，GrowSegment（u3-l4）接管。

#### 4.2.4 代码实践

**实践目标**：通过日志观察一次完整 FTP 协商的命令序列，并与上面的状态图逐一对号。

**操作步骤**：

1. 准备一个可访问的 FTP 服务器。可选方案（任选其一，均**待本地验证**可用性）：
   - 公共镜像，如 `ftp://ftp.gnu.org/gnu/`（GNU FTP）；
   - 本地自建：`python3 -m pip install pyftpdlib` 后运行 `python3 -m pyftpdlib -p 2121 -d /tmp/ftpdir`（先在 `/tmp/ftpdir` 放一个测试文件）。
2. 用 info 级日志下载（示例为自建方案）：
   ```console
   $ ./src/aria2c --log=ftp.log --log-level=info -d /tmp/out ftp://127.0.0.1:2121/testfile
   ```
3. 提取命令序列：
   ```console
   $ grep -A1 'Requesting:' ftp.log
   $ grep -A2 'Response received:' ftp.log | head -60
   ```

**需要观察的现象**：日志应依次出现 `USER ********`、`PASS ********`、`TYPE I`、`PWD`、`CWD /`（或 URL 对应目录）、`SIZE testfile`、`PASV`（或 `EPSV`）、`REST 0`、`RETR testfile` 的请求行，以及 `220/331/230/200/257/250/213/227/350/150` 的应答；下载完成后还能看到一条 `226` 应答日志。

**预期结果**：日志顺序与 4.2.2 状态图一致；每个命令行都带相同 `CUID#` 前缀，验证「一条命令链一个 cuid」。若 pyftpdlib 或远端服务器行为不同（例如直接 `230` 免密），对照 `recvUser` 的 230/331 分叉解释差异。具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么从连接池复用的 FTP 连接可以直接从 `SEQ_SEND_CWD_PREP` 开始，而不是从头登录？池的 key 里为什么有用户名？

答案：池中的连接已经完成登录且保持打开，服务器端的会话状态（当前用户、TYPE 设置）仍在；`PWD` 的结果（baseWorkingDir）在回池时被存进池条目的 options，复用时通过构造参数传回，因此无需重新 PWD。key 含用户名是因为不同账号在同一条连接上不能混用会话状态。

**练习 2**：`recvRest` 收到非 350 的应答时，什么情况下抛异常、什么情况下容忍？

答案：见 [src/FtpNegotiationCommand.cc:846-862](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L846-L862)：仅当「请求的续传偏移不为 0」且服务器拒绝 REST 时抛 `FTP server doesn't support resuming.`（`CANNOT_RESUME`）；偏移本来就是 0 时，REST 失败无关紧要（从头下载即可），继续 `SEQ_SEND_RETR`。

**练习 3**：URL `ftp://host/a/b/file.tar.gz` 在 `sendCwdPrep` 之后 `cwdDirs_` 队列内容是什么（假设 baseWorkingDir 为 `/`）？

答案：`["/", "a", "b"]`。`sendCwdPrep` 先 push `/`，再把 URL 目录部分 `/a/b` 按 `/` 切分，得到空串、`a`、`b`，其中空串被跳过（`split` 忽略空段），因此依次发出 `CWD /`、`CWD a`、`CWD b` 三条命令，全部 250 后才进入 SIZE。

### 4.3 FTP 命令链（下）：数据连接、数据泵与 226 收尾

#### 4.3.1 概念说明

协商完成只是「服务器答应给数据」，真正的字节搬运和 HTTP 一样由 `DownloadCommand` 数据泵完成（u4-l1 讲过它的读-过滤-写盘-自循环）。FTP 链在此之上多出两个协议特有问题：

1. **数据连接从哪来**：被动模式下 `preparePasvConnect` 主动连向 PASV 应答给的地址端口；主动模式下 `waitConnection` 接受服务器的反向连接。
2. **怎么知道传输完整结束**：数据连接 EOF 不等于传输成功，权威判定是控制连接上的 `226 Transfer complete`。aria2 为此专设 `FtpFinishDownloadCommand`。

#### 4.3.2 核心流程

```text
被动模式：recvPasv/recvEpsv 解析出端口
  → preparePasvConnect：
      无代理 → dataSocket_->establishConnection(服务器地址, pasvPort)
               （注意：连的是控制连接对端 endpoint.addr，而不是 PASV 应答里的
                 地址——EPSV 语义推广到 PASV，规避恶意 PASV 应答指向第三方）
      有代理 → SEQ_RESOLVE_PROXY → CONNECT 隧道三步（复用 HttpConnection，
               目标伪造成 ftp://host:pasvPort 的 URI，见 u4-l4）
  → SEQ_SEND_REST_PASV：查 SO_ERROR 确认数据连接建立无误 → REST/RETR

主动模式：recvRetr 收到 150 后
  → SEQ_WAIT_CONNECTION：serverSocket_->acceptConnection() 接进服务器的连接

RETR 应答 150/125 → SEQ_NEGOTIATION_COMPLETED
  → FtpDownloadCommand（dataSocket 当读源，ctrlSocket 随身携带）
      数据泵循环：读数据连接 → StreamFilter → 写 PieceStorage
  → 数据收满（写偏移 == 文件长度）时 FtpDownloadCommand::prepareForNextSegment
  → FtpFinishDownloadCommand 等控制连接 226 → poolSocket 回池
```

#### 4.3.3 源码精读

**（1）被动连接的落点：preparePasvConnect**

[src/FtpNegotiationCommand.cc:689-723](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L689-L723)：

```cpp
auto endpoint = getSocket()->getPeerInfo();
A2_LOG_INFO(fmt(MSG_CONNECTING_TO_SERVER, getCuid(), endpoint.addr.c_str(), pasvPort_));
dataSocket_ = std::make_shared<SocketCore>();
dataSocket_->establishConnection(endpoint.addr, pasvPort_, false);
```

注意它连接的地址取自**控制连接的对端**（`getPeerInfo()`），端口用 PASV/EPSV 应答解析出的 `pasvPort_`。也就是说，即使 PASV 应答里带了地址，aria2 也不采信——这可以防住经典的「PASV 应答劫持」攻击（恶意服务器诱导客户端连向第三方主机的任意端口）。随后 `setWriteCheckSocket(dataSocket_)`：非阻塞连接完成以「可写」为信号（u2-l4 的三段式建连）。

**（2）主动模式的 accept：waitConnection**

[src/FtpNegotiationCommand.cc:876-910](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L876-L910)：`recvRetr` 收到 `125/150` 后，被动模式直接完成协商；主动模式则把读检查从控制 socket 切到 `serverSocket_`（等服务器的反向连接），`waitConnection` 里 `acceptConnection()` 拿到 `dataSocket_` 后完成协商。这里能看出两种模式对命令链的真实影响：**主动模式必须先收到 RETR 的 150 才能等连接，被动模式必须先建好数据连接才敢发 RETR**——顺序相反。

**（3）FtpDownloadCommand：数据泵的 FTP 特化**

[src/FtpDownloadCommand.cc:51-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpDownloadCommand.cc#L51-L84)：构造函数把 `dataSocket` 交给 DownloadCommand 基类（包成 SocketRecvBuffer 作为读源），同时持有 `ftpConnection_` 与 `ctrlSocket_` 备用。覆写的 `prepareForNextSegment` 是关键：

```cpp
if (getOption()->getAsBool(PREF_FTP_REUSE_CONNECTION) &&
    getFileEntry()->gtoloff(getSegments().front()->getPositionToWrite()) ==
        getFileEntry()->getLength()) {
  getDownloadEngine()->addCommand(make_unique<FtpFinishDownloadCommand>(
      ..., ftpConnection_, ..., ctrlSocket_));
  ...
  return true;   // 本命令销毁，接力棒交给 Finish 命令
}
else {
  return DownloadCommand::prepareForNextSegment();
}
```

当本 segment 的写偏移（经 `gtoloff` 换算到文件级偏移）到达文件末尾时，FTP 侧不去走基类「换段/换连接重协商」的默认路径，而是挂起一个 `FtpFinishDownloadCommand` 去等 `226`——因为对 FTP 而言，数据收满 ≠ 服务器认为传输完成。

**（4）FtpFinishDownloadCommand：为 226 而存在的命令**

[src/FtpFinishDownloadCommand.cc:71-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpFinishDownloadCommand.cc#L71-L119) 直接覆写了 `execute()`（不走 AbstractCommand 骨架，注释说明 `_segments` 为空）：读事件或超时驱动 `ftpConnection_->receiveResponse()`；`226` 且允许复用时 `poolSocket(...)` 把控制连接连同 baseWorkingDir 一起回池；非 226 只记一条 `Bad status for transfer complete.` 日志；任何 `RecoverableException` 都被吞掉并记日志——因为数据已经完整落盘，收尾阶段的协议瑕疵不值得让整个下载失败。最后按 `downloadFinished()` 决定正常退出还是 `prepareForRetry`。

#### 4.3.4 代码实践

**实践目标**：对比主动/被动模式下日志与行为的差异，直观理解数据连接的建立方向。

**操作步骤**：

1. 在 4.2.4 的本地 FTP 环境上再做一次主动模式下载（**待本地验证**；pyftpdlib 默认支持主动模式）：
   ```console
   $ ./src/aria2c --ftp-pasv=false --log=ftp-active.log --log-level=info \
       -d /tmp/out2 ftp://127.0.0.1:2121/testfile
   ```
2. 对比两份日志：`grep -E 'PORT|PASV|EPSV|EPRT|Connecting' ftp*.log`。
3. 若在 NAT 后面的机器上对公网 FTP 服务器执行 `--ftp-pasv=false`，观察错误（此环境可能没有，属可选项）。

**需要观察的现象**：被动模式日志出现 `PASV` 请求与 `227` 应答，随后一条 `Connecting to <服务器IP>:<数据端口>`；主动模式日志出现 `PORT h1,h2,h3,h4,p1,p2` 请求与 `200` 应答，但**没有**第二条外连日志（连接由服务器发起进来）。

**预期结果**：两种模式都能完成下载，且文件校验一致；日志差异集中在数据连接建立段，印证 4.3.2 的两条分支。具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `preparePasvConnect` 不使用 PASV 应答中返回的 IP 地址？

答案：防 PASV 劫持。若采信应答地址，恶意服务器可让客户端向任意第三方主机的任意端口发起连接（端口扫描跳板）。aria2 只采信端口，地址一律取控制连接的真实对端，两个信息都来自可信信道。

**练习 2**：`FtpFinishDownloadCommand` 为什么覆写 `execute()` 而不是实现 `executeInternal()`？

答案：AbstractCommand 的 `execute()` 骨架会做 segment 刷新、超时判定、`executeInternal` 分流等针对「正在下载的命令」的工作，而 Finish 命令没有 segment、职责单一（等 226、回池）。它的注释明确说明 `_segments` 为空，走骨架反而会触发不适用的路径，因此直接覆写 `execute()` 自己管理 requeue（`addCommandSelf`）与退出。

**练习 3**：数据连接已经 EOF、文件字节数也符合 SIZE，aria2 为什么还要等 226？

答案：EOF 只说明 TCP 流结束，不说明服务器端认为传输成功（例如服务器中途出错也可能关闭连接，字节数碰巧一致的情况虽少但可能发生，尤其长度未知模式）。`226 Transfer complete` 是 FTP 协议对「传输完整」的权威确认，所以收尾命令以它为准；不过 aria2 在非 226 时也只记日志不判失败，因为本地数据完整性另有 piece/校验和体系把关。

### 4.4 Sftp 命令链：单连接上的 SSH 会话

#### 4.4.1 概念说明

SFTP 命令链是 FTP 链的「同构压缩版」：入口、Initiate、ConnectCommand、连接池、Finish 收尾全部照搬，唯独协商状态机被大幅缩短——因为 SSH 把「传输层安全 + 认证 + 文件操作」全部浓缩在一条连接里：

| 职责 | FTP 链的实现 | SFTP 链的实现 |
| --- | --- | --- |
| 安全通道 | 无（或外层代理隧道） | SSH 握手（密钥交换 + 主机密钥校验） |
| 认证 | `USER` + `PASS` | `libssh2_userauth_password` |
| 定位到文件 | `PWD` + 逐级 `CWD` | 直接按绝对路径 `sftp_open` |
| 获取长度/mtime | `SIZE`（+ `MDTM`） | 打开句柄上 `fstat` |
| 断点续传 | `REST <offset>` | 句柄上 `seek64` |
| 数据通道 | 第二条 TCP 连接（PASV/PORT） | 同一条连接，`sftp_read` |
| 传输完成确认 | 控制连接 `226` | 无对应物，`sftp_close` 关句柄即收尾 |
| 协商状态数 | 30+（Seq 枚举） | 6（`SEQ_HANDSHAKE` 到 `SEQ_NEGOTIATION_COMPLETED`） |

SFTP 也**没有** TYPE/PWD/CWD 的概念，传输恒为二进制；也没有「多镜像目录跳转」问题，路径直接来自 URI（`getPath()` 把 dir+file 拼接并 percentDecode，见 [src/SftpNegotiationCommand.cc:330-335](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L330-L335)）。

#### 4.4.2 核心流程

`SftpNegotiationCommand::Seq` 只有 6 个工作状态（[src/SftpNegotiationCommand.h:47-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.h#L47-L58)）：

```text
SEQ_HANDSHAKE        SocketCore::sshHandshake
                     （首次调用创建 SSHSession；完成后若设置了
                       --ssh-host-key-md 则校验主机密钥指纹）
SEQ_AUTH_PASSWORD    sshAuthPassword（用户名/密码仍来自 AuthConfig 体系）
SEQ_SFTP_OPEN        sshSFTPOpen（sftp 子系统 init + 打开远端文件）
SEQ_SFTP_STAT        sshSFTPStat（取 totalLength 与 mtime）
                       ├─ 首条连接：onFileSizeDetermined（文件准备，
                       │   与 FTP 版同构）→ 经 FILE_PREPARATION 挂起，
                       │   校验/预分配完成后回到 SEQ_SFTP_SEEK
                       └─ 复用/后续连接：validateTotalLength → SEQ_SFTP_SEEK
SEQ_SFTP_SEEK        sshSFTPSeek(segment->getPositionToWrite())
                       —— REST 的等价物；之后
SEQ_NEGOTIATION_COMPLETED  创建 SftpDownloadCommand（单 socket 数据泵）
```

推进机制与 FTP 不同：FTP 用「处理函数返回 bool + while 循环」，SFTP 用 `for(;;)` 大循环 + `goto again`：

```text
executeInternal():
  for (;;):
    switch (sequence_):
      某步的 ssh* 调用返回 false（WOULDBLOCK）
        → goto again          // 跳出循环
      某步完成 → sequence_ 前进，继续 for 循环（一步接一步连推）
      SEQ_NEGOTIATION_COMPLETED → 创建数据泵，return true 销毁
  again:
    addCommandSelf()          // 重排自己
    if (socket wantWrite) setWriteCheckSocket(...)
    return false
```

#### 4.4.3 源码精读

**（1）构造函数：主机密钥指纹的预先解析**

[src/SftpNegotiationCommand.cc:69-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L69-L89)：构造时读取 `--ssh-host-key-md`（格式 `type=hex`，如 `sha-1=AA:BB:...`，由 ChecksumOptionHandler 校验合法类型 sha-1/md5，见 [src/OptionHandlerFactory.cc:1276-1278](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1276-L1278)），把 hashType 与二进制 digest 拆存下来，供握手完成后比对。这是 SFTP 特有的防中间人机制——FTP 明文协议没有对应物。

**（2）executeInternal 的六步循环**

[src/SftpNegotiationCommand.cc:93-195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L93-L195)。每个状态的形状高度一致，以握手为例：

```cpp
case SEQ_HANDSHAKE:
  setReadCheckSocket(getSocket());
  if (!getSocket()->sshHandshake(hashType_, digest_)) {
    goto again;                     // WOULDBLOCK，等下轮事件
  }
  A2_LOG_DEBUG(... "SSH handshake success" ...);
  sequence_ = SEQ_AUTH_PASSWORD;
  break;
```

STAT 状态（[L123-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L123-L143)）承担了 FTP 链中 SIZE+MDTM 的双重职责：一次 `fstat` 同时拿 `totalLength` 和 `mtime`，后者经 `updateLastModifiedTime` 供 `--remote-time` 落盘时间戳使用。SEEK 状态（[L144-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L144-L157)）把 segment 的 `getPositionToWrite()` 作为远端句柄偏移——注意这是**逐连接的**偏移：每个分段连接各自 open 同一个远端文件、各自 seek 到自己的段起点，这与 FTP 每条连接各自 REST 完全对应。

**（3）SocketCore 的 ssh 系列与 SSHSession**

[src/SocketCore.cc:1040-1075](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1040-L1075) 的 `sshHandshake` 是三值约定的翻译层：先清 `wantRead_/wantWrite_`，未就绪则按 `sshCheckDirection()`（内部调 `libssh2_session_block_directions`）设置其一再返回 false。指纹校验值得一看（[L1061-1073](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1061-L1073)）：摘要不匹配直接抛 `Unexpected SSH host key: expected ..., actual ...`。

[src/SSHSession.cc:124-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L124-L133) 的 `init` 里 `libssh2_session_set_blocking(ssh2_, 0)` 一行决定了整套交互模式：此后所有 libssh2 调用（[handshake L162-172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L162-L172)、[authPassword L193-204](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L193-L204)、[sftpOpen L206-227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L206-L227)、[sftpStat/sftpSeek L229-244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L229-L244)）都遵循 `EAGAIN → SSH_ERR_WOULDBLOCK` 的统一翻译。

**（4）SftpDownloadCommand：为什么需要写检查补偿**

数据泵本体复用 DownloadCommand：`SocketCore::readData` 检测到 `sshSession_` 存在时改走 `sshSession_->readData`（即 `libssh2_sftp_read`），WOULDBLOCK 时按方向置 `wantRead_/wantWrite_`，见 [src/SocketCore.cc:841-863](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L841-L863) 与 [src/SSHSession.cc:150-160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SSHSession.cc#L150-L160)。

[src/SftpDownloadCommand.cc:63-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpDownloadCommand.cc#L63-L102) 有两处 FTP 版没有的补偿：

```cpp
// prepareForNextSegment 末尾：
// sftp may not get incoming data.  Enable write check to make this command invoke.
setWriteCheckSocket(getSocket());

// shouldEnableWriteCheck：
return getSocket()->wantWrite() || !getSocket()->wantRead();
```

原因：SSH 层的读进度不一定表现为 socket 可读——libssh2 可能需要先写（发 ACK/窗口调整）才能推进下一次读，此时命令若只挂读事件就会饿死。因此 SFTP 数据泵额外挂写事件，用「可写」作为唤醒信号之一；而 FTP 数据泵只挂 `dataSocket_` 的读事件即可。`shouldEnableWriteCheck`（[L99-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpDownloadCommand.cc#L99-L102)）被 DownloadCommand 的限速/事件管理逻辑回调，决定写检查的启停。

**（5）SftpFinishDownloadCommand：没有 226 的收尾**

[src/SftpFinishDownloadCommand.cc:68-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpFinishDownloadCommand.cc#L68-L118) 与 FTP 版结构相同，但等待的对象是 `sshSFTPClose()`（关闭远端文件句柄）成功，随后 `poolSocket` 把整条 SSH 连接回池（options 传空串——FTP 传 baseWorkingDir，SFTP 没有工作目录概念，源码注释 `we don't need options`）。`SftpDownloadCommand::prepareForNextSegment` 的收尾条件与 FTP 版一致：写偏移到达文件长度且允许复用（`PREF_FTP_REUSE_CONNECTION` 同时管辖 FTP 与 SFTP 的连接复用）。

#### 4.4.4 代码实践

**实践目标**：用最贴近真实的方式跑通一条 SFTP 链（本地 openssh），观察「六步协商」的日志轨迹。

**操作步骤**（待本地验证；需要本机装有 openssh-server 并可登录自己）：

1. 确认本地 sshd 在运行：`systemctl status ssh`（或 `ss -tlnp | grep :22`）。准备测试文件 `dd if=/dev/urandom of=/tmp/sftp-test.bin bs=1M count=4`。
2. 用 aria2 下载（密码认证；若你的 ssh 用密钥认证，SFTP 密码认证会被服务器拒绝，此时可临时在本地开一个允许密码的 sshd 实例，或跳到步骤 4 的源码阅读部分）：
   ```console
   $ ./src/aria2c --log=sftp.log --log-level=debug \
       -d /tmp/out sftp://$USER:你的密码@127.0.0.1/tmp/sftp-test.bin
   ```
3. 检索协商日志：`grep -E 'SSH handshake|SSH authentication|SFTP file|SFTP File|SFTP seek' sftp.log`。
4. （无环境替代方案）纯源码阅读：在 [src/SftpNegotiationCommand.cc:93-195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L93-L195) 的每个 `A2_LOG_DEBUG/INFO` 处，抄录日志格式串，推演一次成功下载应出现的日志行序列。

**需要观察的现象**：debug 日志按序出现 `SSH handshake success`、`SSH authentication success`、`SFTP file /tmp/sftp-test.bin opened`、`SFTP File ..., size=4194304, mtime=...`、`SFTP seek to 0`（或续传时的非零偏移）。

**预期结果**：日志行数远少于 FTP（FTP 一轮协商有约 18 条命令/应答日志，SFTP 只有 5 条左右），直观印证「单连接压缩了会话状态」。文件下载后 `cmp /tmp/out/sftp-test.bin /tmp/sftp-test.bin` 应无差异。具体输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：FTP 用 `REST`、SFTP 用 seek 实现断点续传，两者在「谁持有偏移状态」上有什么本质区别？

答案：FTP 的偏移是**服务器端会话状态**——REST 之后紧跟着的 RETR 才从该偏移开始发送，状态存活于控制连接的会话上下文；SFTP 的偏移是**客户端持有的句柄属性**——`libssh2_sftp_seek64` 直接修改本地打开的 sftph 句柄的读写位置，后续 `libssh2_sftp_read` 从该位置继续，不涉及额外的协议往返。

**练习 2**：`SftpDownloadCommand` 为什么要在 `prepareForNextSegment` 里强制 `setWriteCheckSocket`，而 `FtpDownloadCommand` 不需要？

答案：SFTP 的读进度被 SSH 加密层/流控层包裹，libssh2 在等待应用读之前可能需要先完成写方向的动作（如发送协议数据），此时 socket 可能只呈现可写而不可读；若只挂读事件，命令永远不被唤醒。FTP 数据连接是裸 TCP，读事件可靠反映数据到达，无需写事件补偿。

**练习 3**：`--ssh-host-key-md` 未设置时，`sshHandshake` 对未知主机密钥采取什么行为？这个选项解决什么风险？

答案：未设置时 `hashType_` 为空，握手后跳过指纹比对（[src/SocketCore.cc:1061](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1061) 的 `if (!hashType.empty())`），即默认信任任意主机密钥（交互式 ssh 客户端在此会问「是否接受新指纹」，aria2 作为非交互程序只能由选项显式固定）。该选项用于固定期望指纹，防止中间人攻击：摘要不匹配立即中止下载。

## 5. 综合实践

把本讲三个模块串成一个对照实验（对应本讲的 practice_task）：**分别用 FTP 和 SFTP 下载同一批文件，对照日志写出两条命令链的「命令序列对照表」**。

### 5.1 实践目标

1. 拿到一份真实的 FTP 协商命令/应答序列和一份 SFTP 协商日志序列；
2. 手工建立「FTP 协商步 ↔ SFTP 协商步」的映射，验证 4.4.1 的对照表；
3. 体会两条链在入口（InitiateConnectionCommandFactory）、出口（DownloadCommand 数据泵 + Finish 命令）上完全同构，差异全部封装在中间的协商状态机里。

### 5.2 操作步骤

1. 准备两个数据源（均可**待本地验证**）：
   - FTP：`python3 -m pyftpdlib -p 2121 -d /tmp/share`（`/tmp/share` 放入同一个测试文件 `sample.bin`）；
   - SFTP：本机 openssh-server，把 `sample.bin` 放到 `/tmp`。
2. 各下载一次（注意 `-V` 会关掉分段，让日志更干净）：
   ```console
   $ ./src/aria2c -V --log=ftp.log  --log-level=info -d /tmp/o1 ftp://127.0.0.1:2121/sample.bin
   $ ./src/aria2c -V --log=sftp.log --log-level=debug -d /tmp/o2 \
       sftp://USER:PASS@127.0.0.1/tmp/sample.bin
   ```
3. 提取序列：
   ```console
   $ grep -A1 'Requesting:' ftp.log | grep -v '^--'
   $ grep -A3 'Response received:' ftp.log | head -80
   $ grep -E 'SSH |SFTP ' sftp.log
   ```
4. 填写对照表（示例骨架，请用你的实际日志填写）：

   | 阶段 | FTP（命令/应答） | SFTP（日志事件） |
   | --- | --- | --- |
   | 通道建立/安全 | （明文，无） | SSH handshake success |
   | 认证 | USER/331 → PASS/230 | SSH authentication success |
   | 传输模式 | TYPE I/200 | （无对应） |
   | 定位文件 | PWD/257 → CWD×N/250 | SFTP file opened |
   | 元数据 | SIZE/213（+MDTM/213） | SFTP File size=…, mtime=… |
   | 数据连接/偏移 | PASV/227 → REST/350 | SFTP seek to 0 |
   | 开始传输 | RETR/150 | （数据泵立即启动） |
   | 完成确认 | 226（FtpFinishDownloadCommand） | sftp close（SftpFinishDownloadCommand） |

5. 进阶（可选）：对同一文件再用 `-s2`（两个分段连接）下载一次 FTP 版，观察日志中出现**两个 cuid**、两次 `PASV+REST+RETR`，验证「每条连接独立协商、独立数据连接、各自 REST 到不同偏移」。

### 5.3 需要观察的现象与预期结果

- FTP 日志的命令序列与 4.2.2 状态图一致，且所有行共享同一 `CUID#`（进阶步骤中为两个）；
- SFTP 日志只有约 5 条协商事件，且没有第二条连接的 `Connecting` 日志；
- 两份产出文件与源文件 `cmp` 一致。
- 若某一步日志与预期不符（例如服务器直接 230 免密、或 pyftpdlib 不支持 MDTM），回到 4.2.3 对应的 `recvXxx` 函数找出解释——这本身就是最好的练习。具体输出**待本地验证**。

## 6. 本讲小结

- **FtpConnection 是编解码器，FtpNegotiationCommand 是状态机**：前者负责命令文本拼装、应答码解析与粘包处理，后者用 30 余个 `Seq` 状态驱动 RFC 959 会话，二者职责正交、可独立阅读。
- **FTP 的完整协商链**：`220 问候 → USER(331/230) → PASS(230) → TYPE(200) → PWD(257) → CWD×N(250) → [MDTM(213)] → SIZE(213) → PASV/EPSV(227/229) 或 EPRT/PORT(200) → REST(350) → RETR(125/150) → 数据传输 → 226`；连接池命中时从 `SEQ_SEND_CWD_PREP` 快进。
- **主动/被动模式的差异落在数据连接建立段**：被动模式客户端先连数据端口再发 REST/RETR，且地址强制取控制连接对端防劫持；主动模式客户端先监听并 PORT/EPRT，收到 150 后 `accept` 服务器的反向连接。
- **双通道的接力棒**：`FtpDownloadCommand` 以 `dataSocket_` 为读源跑 DownloadCommand 数据泵，收满后由 `FtpFinishDownloadCommand` 等控制连接的 `226` 并回池（连 baseWorkingDir 一起存入池条目）。
- **SFTP 链是 FTP 链的同构压缩**：入口、连接池、Finish 收尾全部复用，协商压缩为「握手→密码认证→open→fstat→seek」六步，全部在一条 SSH 连接内完成；`--ssh-host-key-md` 提供唯一的安全增强点（主机密钥指纹固定）。
- **libssh2 非阻塞三值约定**贯穿 SFTP 全链：`EAGAIN → WOULDBLOCK → wantRead_/wantWrite_ → 命令重排等事件`；`SftpDownloadCommand` 的写检查补偿是这条约定在数据泵上的最后一块拼图。

## 7. 下一步学习建议

- **u4-l6（流式过滤）**：本讲的数据泵直接复用了 `DownloadCommand`，下一讲下潜到数据泵内部的 StreamFilter 链，看 chunked/gzip 如何在写盘前处理——FTP/SFTP 的响应体同样会流经这条链（如 `--ftp-type=ascii` 场景之外的编码处理）。
- **预习 u5 单元（磁盘 I/O）**：本讲两处出现 `onFileSizeDetermined → preDownloadProcessing → initPieceStorage`，这条「长度确定后」的路径正是 u5-l1/u5-l2（DiskWriter、PieceStorage）的入口，建议顺着 `getRequestGroup()->preDownloadProcessing()` 调用链预读一遍。
- **源码延伸阅读**：对照 [src/SftpNegotiationCommand.cc:197-310](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L197-L310) 与 [src/FtpNegotiationCommand.cc:388-505](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L388-L505) 这两份几乎逐行同构的 `onFileSizeDetermined`，思考：如果要做一次去重重构，抽公共基类的难点在哪里（提示：两者的 Seq 枚举与挂起语义不同）。
