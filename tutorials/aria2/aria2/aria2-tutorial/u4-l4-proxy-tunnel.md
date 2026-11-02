# 代理与隧道：HTTP 代理及 CONNECT 隧道

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 aria2 走 HTTP 代理的两种方式——**绝对 URI 的 GET**（把完整 URL 写进 HTTP 请求行，交给代理代取）与 **CONNECT 隧道**（先让代理打通一条到目标主机的裸 TCP 管道，再在上面跑原生协议）——分别适用于哪些协议、由哪些代码决定。
2. 跟踪代理决策链：`--http-proxy` / `--https-proxy` / `--ftp-proxy` / `--all-proxy` / `--no-proxy` 五组选项如何被 `getProxyUri` / `createProxyRequest` / `resolveProxyMethod` 一步步求值为「用不用代理、用哪种代理方式」。
3. 精读代理命令对的公共骨架 `AbstractProxyRequestCommand` / `AbstractProxyResponseCommand`，以及 `HttpProxyRequestCommand` 与 `FtpTunnel` 两族具体命令链，理解隧道建立后命令链如何「切回」普通协议命令。
4. 跟踪 `Proxy-Authorization` 认证头的生成流程，并解释为什么日志里它总是被替换成 `<snip>`。
5. 完成一次「本地代理 + 日志观察」实验：亲眼在 aria2 日志中看到 `CONNECT host:port HTTP/1.1` 何时出现、何时不出现。

本讲承接 u4-l1 建立的 HTTP 命令接力链认知：上一讲的四段接力（Initiate→Request→Response→Download）默认**直连**目标服务器；本讲要回答的问题是——当用户配置了代理时，这条接力链的前几棒是如何被「换人」的。

## 2. 前置知识

### 2.1 正向代理（forward proxy）

代理是一台位于客户端与目标服务器之间的中转机器。对 aria2 而言，使用代理意味着两件事的改变：

- **TCP 连接的目标**变成了代理服务器（DNS 解析的也是代理的主机名，不是目标主机）；
- **HTTP 报文的写法**可能改变（请求行里的路径换成完整 URL），或报文外面再包一层握手（CONNECT）。

### 2.2 HTTP 请求行的两种形态与 CONNECT 方法

按 HTTP/1.1 报文规范（RFC 9112），请求行中的目标有两种写法：

| 形态 | 请求行长什么样 | 谁能看懂 |
| --- | --- | --- |
| origin-form（源形式） | `GET /path/file HTTP/1.1` | 目标服务器自己 |
| absolute-form（绝对形式） | `GET http://host/path/file HTTP/1.1` | 代理（它据此知道该去找谁） |

普通浏览器和直连的 aria2 用 origin-form；**走代理的明文 HTTP 下载用 absolute-form**，代理收到后代替客户端去取回响应。

对 HTTPS 这招行不通：TLS 要求客户端与**目标服务器**端到端握手，中间的代理不可能替客户端完成加密协商（否则就是中间人攻击）。于是 HTTP 提供了专门的 `CONNECT` 方法：

```http
CONNECT www.example.com:443 HTTP/1.1
Host: www.example.com:443
```

代理收到后与 `www.example.com:443` 建立一条 TCP 连接，回 `200` 之后，这条代理连接就变成一条**不透明字节管道**——客户端在管道里直接和目标服务器完成 TLS 握手、发送 HTTP 请求，代理只负责转发字节。FTP、SFTP 这类非 HTTP 协议也可以借这条管道穿越只会说 HTTP 的代理。

### 2.3 代理认证：`Proxy-Authorization` 头

与 u4-l3 讲过的 `Authorization`（对目标服务器认证）平行，`Proxy-Authorization` 用来向**代理**证明身份，最常见同样是 Basic 方案：`Base64(用户名:密码)`。认证的是代理这一跳，与目标服务器无关。aria2 把代理的用户名密码直接内嵌在代理 URI 中传递（`http://USER:PASS@proxy:port/`）。

### 2.4 no-proxy 名单与 CIDR

和 curl/wget 一样，aria2 支持 `--no-proxy` 豁免名单，格式是逗号分隔的 `HOSTNAME,DOMAIN,NETWORK/CIDR`。其中 `192.168.0.0/16` 这种「网络地址/前缀位数」写法叫 CIDR（无类别域间路由），用来表示一段 IP 地址的集合。

### 2.5 从源码看：aria2 对代理 URI 的一个特殊处理

预告一个本讲源码精读里会看到的细节：aria2 **到代理这一跳永远是明文 HTTP**。即使你写 `--https-proxy=https://proxy:3128/`，选项处理器也会把协议改写回 `http`（见 4.1.3）；TLS 只发生在隧道内部、与目标服务器之间。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) | 代理决策层：`getProxyUri`/`createProxyRequest`/`resolveProxyMethod`/`inNoProxy` 都定义在这里 |
| [src/InitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc) | 建连命令的公共骨架：决定「解析谁的主机名」（代理或目标） |
| [src/HttpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc) | HTTP(S) 入口：按 proxyMethod 选择 GET 链或 CONNECT 链 |
| [src/FtpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc) | FTP/SFTP 入口：GET / 隧道双模式分流 |
| [src/AbstractProxyRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyRequestCommand.cc) | 代理命令对的「发」骨架：构造并发送 CONNECT |
| [src/AbstractProxyResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyResponseCommand.cc) | 代理命令对的「收」骨架：等 CONNECT 的 200 应答 |
| [src/HttpProxyRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyRequestCommand.cc) / [src/HttpProxyResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyResponseCommand.cc) | HTTP(S) 的 CONNECT 隧道命令对，接力回 `HttpRequestCommand` |
| [src/FtpTunnelRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelRequestCommand.cc) / [src/FtpTunnelResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelResponseCommand.cc) | FTP/SFTP 的 CONNECT 隧道命令对，接力回 `FtpNegotiationCommand`/`SftpNegotiationCommand` |
| [src/HttpProxyRequestConnectChain.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyRequestConnectChain.h) / [src/FtpTunnelRequestConnectChain.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelRequestConnectChain.h) / [src/HttpRequestConnectChain.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestConnectChain.h) | TCP 建连成功后「下一棒是谁」的三种控制链 |
| [src/ConnectCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc) | 非阻塞连接的确认者，通过 controlChain 分发下一棒 |
| [src/HttpRequest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc) | `createProxyRequest`（拼 CONNECT 报文）、`createRequest`（GET 模式的绝对 URI）、`getProxyAuthString`（认证头） |
| [src/HttpConnection.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc) | `sendProxyRequest` 把 CONNECT 报文送出去并登记在途请求 |
| [src/OptionHandlerFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc) / [src/OptionHandlerImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc) | 代理选项的注册与格式校验（`HttpProxyOptionHandler`） |
| [src/prefs.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc) | `V_GET`/`V_TUNNEL` 两个魔法值（`"get"`/`"tunnel"`） |

## 4. 核心概念与源码讲解

本讲的三个核心最小模块之上还有一层必须先讲清的**决策层**，因此按四个模块推进：4.1 决策层（用不用代理、用哪种方式）→ 4.2 代理命令对骨架 → 4.3 HttpProxy 链（HTTP/HTTPS 的 CONNECT）→ 4.4 FtpTunnel 链与 FTP 的 GET 模式。

### 4.1 代理决策层：选项、proxyRequest 与 GET/TUNNEL 之分

#### 4.1.1 概念说明

在任何一个 `AbstractCommand` 想发起连接之前，都要先回答三个问题：

1. **这个请求要走代理吗？** 由选项决定：目标协议是 `http` 查 `--http-proxy`，是 `https` 查 `--https-proxy`，是 `ftp`/`sftp` 查 `--ftp-proxy`；协议专属选项没有值时回退到 `--all-proxy`。再叠加 `--no-proxy` 豁免名单（命中主机名/域/CIDR 就直连）。
2. **代理是谁？** 决策结果被装进一个普通的 `Request` 对象（`proxyRequest`），它的 host/port 就是代理的地址——后续 DNS 解析与 TCP 连接全部以它为准。
3. **用哪种代理方式？** `resolveProxyMethod` 返回两个字符串常量之一：`V_GET`（"get"，绝对 URI 方式）或 `V_TUNNEL`（"tunnel"，CONNECT 隧道）。规则非常简短：`https` 和 `sftp` **永远**是隧道（加密/非 HTTP 协议没法伪装成 GET）；`http` 和 `ftp` 默认 GET，可被 `--proxy-method=tunnel` 强制为隧道。

注意 aria2 中**没有** `--ftp-via-http-proxy` 这样的开关，FTP 的方式选择与 HTTP 共用同一个 `--proxy-method` 选项。

#### 4.1.2 核心流程

```
getProxyUri(protocol, option):
    按协议选 (http-proxy | https-proxy | ftp-proxy) + 对应 -user/-passwd
    → makeProxyUri(): 解析代理 URI，用 -user/-passwd 覆盖内嵌 userinfo，重新拼装
    → 若为空 → 用 all-proxy + all-proxy-user/-passwd 再来一次
    → 仍为空 → 返回 ""（不走代理）

createProxyRequest():
    if 目标主机命中 no-proxy 名单 → 返回空
    proxy = getProxyUri(目标协议)
    if proxy 非空 → Request(proxy)（解析失败则放弃代理并记日志）

resolveProxyMethod(protocol):
    if --proxy-method == "tunnel" 或 protocol ∈ {https, sftp} → "tunnel"
    else → "get"

InitiateConnectionCommand::executeInternal():
    proxyRequest = createProxyRequest()
    解析 hostname = proxyRequest ? 代理主机 : 目标主机     ← 注意：DNS 解析对象换人了
    建连 → createNextCommand(..., proxyRequest)
```

#### 4.1.3 源码精读

**协议→选项的映射与回退**。[src/AbstractCommand.cc:663-681](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L663-L681) 的 `getProxyUri` 按目标协议选择选项组，`http`/`https`/`ftp`+`sftp` 各自映射到专属 Pref，找不到才落到 `--all-proxy`；[src/AbstractCommand.cc:628-643](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L628-L643) 的 `makeProxyUri` 负责把 `--xxx-proxy-user`/`--xxx-proxy-passwd` 两个伴生选项合并进代理 URI（选项里显式给出的用户名密码优先于 URI 内嵌的 userinfo）。回退逻辑在 [src/AbstractCommand.cc:648-658](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L648-L658)：专属代理 URI 为空时改用 `PREF_ALL_PROXY` 组再构造一次。

**豁免名单**。[src/AbstractCommand.cc:695-728](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L695-L728) 的 `inNoProxy` 把 `--no-proxy` 按逗号切开，逐项匹配：不含 `/` 的条目做域名匹配（`noProxyDomainMatch`），含 `/` 的条目按 CIDR 解析前缀位数后用 `inSameCidrBlock` 判断目标主机是否落在网段内。注释里作者也承认局限：这里没有先解析主机名再对全部 IP 求匹配。

**生成 proxyRequest**。[src/AbstractCommand.cc:737-757](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L737-L757) 的 `createProxyRequest`：先过 no-proxy 名单，再把代理 URI 塞进一个新建的 `Request` 对象；解析成功记一条 debug 日志 `Using proxy`，失败则降级为直连。这个函数在每个 `InitiateConnectionCommand` 开头被调用一次——[src/InitiateConnectionCommand.cc:74-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L74-L92) 中可以看到关键一行：**proxyRequest 非空时 hostname/port 换成代理的**（L79-86），随后 `resolveHostname` 解析的是代理主机名。这就是「走代理时 DNS 解析对象换人」的出处。

**GET 还是隧道**。[src/AbstractCommand.cc:868-876](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L868-L876)：

```cpp
const std::string&
AbstractCommand::resolveProxyMethod(const std::string& protocol) const
{
  if (getOption()->get(PREF_PROXY_METHOD) == V_TUNNEL || protocol == "https" ||
      protocol == "sftp") {
    return V_TUNNEL;
  }
  return V_GET;
}
```

两个常量定义在 [src/prefs.cc:146-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L146-L147)（`"get"` / `"tunnel"`），而 `--proxy-method` 的默认值与合法取值在 [src/OptionHandlerFactory.cc:1429-1438](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1429-L1438) 注册：默认 `V_GET`，只允许这两个值（`ParameterOptionHandler` 会拒绝其他取值）。帮助文本见 [src/usage_text.h:86-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L86-L93)（`--all-proxy` 的说明明确写了「用 `""` 覆盖先前定义的代理」）。

**代理 URI 的规范化**。[src/OptionHandlerImpl.cc:501-525](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L501-L525) 的 `HttpProxyOptionHandler::parseArg`：没写 scheme 就自动补 `http://`；解析后**强制 `us.protocol = "http"` 再存回**（L522）。这印证了 2.5 节的结论——到代理这一跳始终是明文 HTTP；写 `https://proxy` 并不会让客户端与代理之间加密。传空字符串（`--http-proxy=""`）则清空该代理，用于覆盖配置文件里的旧值。

#### 4.1.4 代码实践

1. **实践目标**：验证「配置代理后，连接与 DNS 的对象变成代理」，以及选项优先级（协议专属 > `--all-proxy`）。
2. **操作步骤**：
   - 准备一个不可达的代理地址，例如 `127.0.0.1:1`。
   - 运行 `./src/aria2c --http-proxy=http://127.0.0.1:1 http://example.com/ -l proxy1.log --log-level=info -d /tmp/a2proxy --allow-overwrite=true`。
   - 打开 `proxy1.log`，搜索 `Connecting to`。
   - 再运行 `./src/aria2c --all-proxy=http://127.0.0.1:1 --http-proxy=http://127.0.0.2:1 http://example.com/ ...`，同样看日志里连接的是哪个地址；接着追加 `--no-proxy=example.com` 再试一次。
3. **需要观察的现象**：第一次日志应出现形如 `CUID#N - Connecting to 127.0.0.1:1` 的行（而非 example.com 的 IP）；第二次应连接 `127.0.0.2:1`（专属选项覆盖 all-proxy）；加 `--no-proxy` 后应回到解析并连接 example.com 的 IP。
4. **预期结果**：与上面三条一一对应。日志依据：连接目标来自 [src/InitiateConnectionCommand.cc:74-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L74-L92)（proxyRequest 决定 hostname/port），连接日志由各 `InitiateConnectionCommand` 子类打印。由于本环境未执行下载，具体日志行**待本地验证**。
5. 若手头没有任何可用网络，也可以纯源码完成本实践：沿 `executeInternal → createProxyRequest → getProxyUri` 读一遍调用链，写出每个选项的取值优先级表。

#### 4.1.5 小练习与答案

**练习 1**：`--proxy-method` 的默认值是什么？对 `https://` 目标设置 `--proxy-method=get` 有没有效果？

答案：默认值是 `V_GET`（`"get"`，见 [OptionHandlerFactory.cc:1430-1431](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1430-L1431)）。对 https 目标**无效**：`resolveProxyMethod` 里 `protocol == "https"` 条件独立于选项判断，https 恒为 `V_TUNNEL`——TLS 无法被改写成绝对 URI 的 GET。

**练习 2**：`--http-proxy-user`/`--http-proxy-passwd` 与代理 URI 里内嵌的 `USER:PASS@` 同时存在时，哪个生效？

答案：`-user`/`-passwd` 选项生效。`makeProxyUri`（[AbstractCommand.cc:635-641](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L635-L641)）在选项 `defined` 时用选项值覆盖解析出的 `us.username`/`us.password`，再重新 `construct`。

**练习 3**：为什么 `getProxyUri` 对 `ftp` 和 `sftp` 返回同一组选项（`--ftp-proxy`），而 `resolveProxyMethod` 又把 sftp 单独判为隧道？

答案：两者回答的是不同的问题。前者是「代理地址从哪读」——FTP/SFTP 共用一组代理配置；后者是「流量怎么过代理」——sftp 流量是 SSH 协议，不可能写成 HTTP GET 的绝对 URI，所以必须 `CONNECT` 建隧道，`resolveProxyMethod` 对它无条件返回 `V_TUNNEL`。

### 4.2 AbstractProxyRequestCommand / AbstractProxyResponseCommand：代理命令对的公共骨架

#### 4.2.1 概念说明

CONNECT 隧道的建立过程被建模成一对命令：**请求命令**负责把 CONNECT 报文发出去，**响应命令**负责等代理回 200。HTTP(S) 与 FTP/SFTP 两个子系统需要的流程一模一样，只有「拿到 200 之后交给谁」不同。于是 aria2 把公共部分上提到两个抽象基类 `AbstractProxyRequestCommand` / `AbstractProxyResponseCommand`，把「下一棒」定为纯虚函数 `getNextCommand()`，由四个子类各自实现——又一次模板方法模式（对照 u2-l3 的 `AbstractCommand::executeInternal` 钩子）。

值得强调：这两个基类**只服务于 CONNECT 隧道**。GET 模式（绝对 URI）不需要它们——那种情况下 CONNECT 根本不会发生，命令链直接就是普通的 `HttpRequestCommand`。

#### 4.2.2 核心流程

```
AbstractProxyRequestCommand::executeInternal()
    if 发送缓冲空:
        构造 HttpRequest：setRequest(目标请求) + setProxyRequest(代理)
        httpConnection_->sendProxyRequest(...)   ← 拼出 CONNECT 报文并登记在途
    else:
        sendPendingData()                        ← 上次没发完，继续发
    if 发送缓冲空 → addCommand(getNextCommand())，本命令销毁
    else          → setWriteCheckSocket + addCommandSelf（等可写再跑）

AbstractProxyResponseCommand::executeInternal()
    httpResponse = httpConnection_->receiveResponse()
    if 还没收齐响应头 → addCommandSelf 等下一轮
    if 状态码 != 200  → 抛 DL_RETRY_EX(EX_PROXY_CONNECTION_FAILED)
    else              → addCommand(getNextCommand())，切回正常命令链
```

#### 4.2.3 源码精读

**「发」骨架**。[src/AbstractProxyRequestCommand.cc:52-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyRequestCommand.cc#L52-L65) 的构造函数：基于已建好的 socket 现场创建一个 `HttpConnection`（复用 u4-l2 讲过的那套发送/接收设施），超时取 `--connect-timeout`，并注册写事件检查——因为 CONNECT 报文可能因内核发送缓冲满而发不完。[src/AbstractProxyRequestCommand.cc:69-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyRequestCommand.cc#L69-L92) 的 `executeInternal` 只做三件事：缓冲空则新建 `HttpRequest` 并设置 `setUserAgent`/`setRequest`/`setProxyRequest` 三个成员，交给 `sendProxyRequest`；不空则 `sendPendingData` 续传；最后按「是否发完」决定派发下一棒还是自循环。这正对应 u2-l3 的约定：返回 `true` 即销毁，返回 `false` 必须先 `addCommandSelf`。

**CONNECT 报文长什么样**。[src/HttpRequest.cc:289-316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L289-L316) 的 `createProxyRequest` 拼出完整的 CONNECT 请求文本：请求行是 `CONNECT 目标主机:端口 HTTP/1.1`，随后 `User-Agent` 与 `Host` 头；若代理 URI 带用户名，再附 `Proxy-Authorization` 头。注意 `getURIHost()`/`getPort()` 取的是**目标请求**的 host/port——CONNECT 的对象是目标，不是代理。

**「收」骨架**。[src/AbstractProxyResponseCommand.cc:65-79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyResponseCommand.cc#L65-L79)：`receiveResponse` 没凑齐响应头就自循环等待；状态码不是 200 一律抛 `DL_RETRY_EX(EX_PROXY_CONNECTION_FAILED)`（错误文本见 [src/message.h:222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L222)「Proxy connection failed.」）。按 u4-l1 的三级出口模型，`DL_RETRY_EX` 属于**可重试**异常：`AbstractCommand` 捕获后 `prepareForRetry`，从 CreateRequestCommand 重来——所以 407（代理要认证而凭证缺失）、403（代理拒绝 CONNECT）最终表现为反复重试直到 `--max-tries` 耗尽。只有 200 才走到 `getNextCommand()` 把接力棒交出去。

**发送与登记**。[src/HttpConnection.cc:142-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L142-L146) 的 `sendProxyRequest` 调 `createProxyRequest` 拿到文本后复用 `sendRequest`（[src/HttpConnection.cc:125-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L125-L134)）：先把文本推进 `SocketBuffer` 部分发送，再把 `HttpRequest` 压进 `outstandingHttpRequests_` 在途队列。于是后续 `receiveResponse`（[src/HttpConnection.cc:148-167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L148-L167)）解析到的响应头会自动与这个 CONNECT 请求配对——隧道建立阶段复用了 u4-l2 讲过的整套「在途请求 FIFO」机制。

**日志里的 `<snip>`**。[src/HttpConnection.cc:99-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L99-L123) 的 `eraseConfidentialInfo` 在把请求/响应写进日志前，把 `Authorization`、`Proxy-Authorization`、`Cookie`、`Set-Cookie` 四种头的值替换为 `<snip>`——所以你在日志里能看到 CONNECT 的请求行与 `Host`，却看不到代理密码。

#### 4.2.4 代码实践

1. **实践目标**：观察「代理返回非 200 → EX_PROXY_CONNECTION_FAILED → 按 DL_RETRY_EX 语义重试」的完整行为。
2. **操作步骤**：找一个会拒绝 CONNECT 的代理。最简单的办法是拿任意 HTTP 服务器冒充代理——例如 `python3 -m http.server 3128`，它对 CONNECT 请求会返回 5xx/4xx（甚至直接断开）。然后运行 `./src/aria2c --https-proxy=http://127.0.0.1:3128 https://example.com/ --max-tries=2 -l proxy2.log --log-level=info -d /tmp/a2proxy --allow-overwrite=true`。
3. **需要观察的现象**：日志中先出现 `Requesting:\nCONNECT example.com:443 HTTP/1.1...`（`MSG_SENDING_REQUEST`，见 [src/message.h:47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L47)），随后出现 `Proxy connection failed.`，且该组合重复出现直到重试次数用尽。
4. **预期结果**：重试次数与 `--max-tries=2` 一致，最终任务以错误结束。若冒充代理直接断开连接，则表现为 `EX_GOT_EOF` 等其他可重试错误，同样走重试路径。具体报错文本**待本地验证**。
5. 源码阅读型替代：不通网络也能完成——在 `AbstractProxyResponseCommand.cc:74-76` 处想象插入一行日志，沿 u4-l1 的异常出口表写出这条 `DL_RETRY_EX` 会经过哪几个函数（`executeInternal` 抛出 → `AbstractCommand::execute` 捕获 → `prepareForRetry` → 新建 CreateRequestCommand）。

#### 4.2.5 小练习与答案

**练习 1**：`AbstractProxyRequestCommand` 为什么要自带一个 `HttpConnection`，而不是复用后续命令的那个？

答案：代理命令对运行在「连接刚建立、隧道尚未打通」的阶段，此时还不存在任何 `HttpConnection`——它由本类构造函数现场创建（[AbstractProxyRequestCommand.cc:59-60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractProxyRequestCommand.cc#L59-L60)），并通过子类 `getNextCommand()` 里的 `getHttpConnection()` 传递给响应命令，保证「发 CONNECT」与「收 200」使用同一个在途请求队列与接收缓冲。

**练习 2**：CONNECT 的响应是 407 时，aria2 会像 u4-l3 的 `--http-auth-challenge` 那样缓存凭证再主动应答一次吗？

答案：不会。`AbstractProxyResponseCommand` 对非 200 一律抛 `DL_RETRY_EX`，没有任何 407 专用的挑战-应答逻辑；代理凭证只来自代理 URI（或 `-user`/`-passwd` 选项），在发送 CONNECT 前就已经拼进 `Proxy-Authorization` 头（见 4.3.3 的 `getProxyAuthString`）。

**练习 3**：这两个抽象基类为什么不直接定义在 `HttpProxyRequestCommand` 里、让 FTP 子类复制一份？

答案：因为 HTTP(S) 隧道与 FTP/SFTP 隧道的「发 CONNECT / 等 200」完全同构，差异仅在下一棒（`HttpRequestCommand` vs `FtpNegotiationCommand`/`SftpNegotiationCommand`）。把差异点收敛为纯虚 `getNextCommand()`，公共流程只需维护一份。

### 4.3 HttpProxy 命令链：CONNECT 隧道的建立与切回

#### 4.3.1 概念说明

对 `https://` 目标（以及被强制隧道的 `http://` 目标），u4-l1 的四段接力在「Initiate」与「Request」之间被**插入**一段隧道建立流程：

```
CreateRequestCommand
  → HttpInitiateConnectionCommand        （解析代理主机名，连代理）
  → ConnectCommand                        （确认非阻塞连接成功，按 controlChain 分发）
  → HttpProxyRequestCommand               （发 CONNECT）          ★ 代理命令
  → HttpProxyResponseCommand              （等 200）              ★ 代理命令
  → HttpRequestCommand                    （TLS 握手 + 发真实请求，origin-form）
  → HttpResponseCommand → HttpDownloadCommand ...（与直连完全相同）
```

关键设计：`ConnectCommand` 本身不知道下一棒是谁，它通过一个 `ControlChain` 对象获知——HTTP(S) 有两条链（`HttpProxyRequestConnectChain` 走隧道、`HttpRequestConnectChain` 走 GET/直连），FTP 侧还有一条 `FtpTunnelRequestConnectChain`。这样新增协议时无需改动 `ConnectCommand`。

隧道打通后「切回」的方式极其朴素：`HttpProxyResponseCommand::getNextCommand()` 直接 new 一个普通的 `HttpRequestCommand`，**不再传递 proxyRequest**——于是后续请求行是 origin-form、TLS 握手也发生在隧道内的 socket 上，命令链与直连完全一致。代理的存在感在 200 应答之后就彻底消失了。

#### 4.3.2 核心流程

```
HttpInitiateConnectionCommand::createNextCommand(proxyRequest 非空):
    pool = popPooledSocket(目标host, 目标port, 代理host, 代理port)   ← 代理感知的连接池
    proxyMethod = resolveProxyMethod(目标协议)
    if 没有可复用连接:
        establishConnection(代理地址)
        c = ConnectCommand(带 proxyRequest)
        tunnel → c.setControlChain(HttpProxyRequestConnectChain)
        get    → c.setControlChain(HttpRequestConnectChain)        ← GET 模式不需要代理命令！
    else:
        get → HttpRequestCommand + setProxyRequest（继续绝对 URI 请求）
        tunnel → HttpRequestCommand（隧道早已打通，直接用）
```

GET 模式的要点：`HttpRequestConnectChain` 在派发 `HttpRequestCommand` 时会把 `ConnectCommand` 携带的 `proxyRequest` 一并传下去（见 4.3.3 最后一条），使真实请求以绝对 URI 形式发出——所以明文 HTTP 走代理时**一个代理命令都不会出现**，u4-l1 的接力链原封不动，只是请求行的写法变了。

#### 4.3.3 源码精读

**入口分流**。[src/HttpInitiateConnectionCommand.cc:71-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L71-L117) 是 HTTP(S) 走代理的总入口。L77-80 先查连接池——注意 `popPooledSocket` 的四个参数同时携带**目标**主机端口与**代理**主机端口，保证「经代理 A 到目标 T」的连接不会被「经代理 B」或「直连 T」的任务错拿；L81 求 `proxyMethod`；L82-102 建新连接并按方法装配 controlChain（隧道 → `HttpProxyRequestConnectChain`，GET → `HttpRequestConnectChain`）；L104-116 是连接池命中的快路径：GET 模式给 `HttpRequestCommand` 补挂 `proxyRequest`，隧道模式什么也不用补——CONNECT 属于一次性行为，池中这条 socket 的隧道早已建立。

**ControlChain 三行核心**。[src/HttpProxyRequestConnectChain.h:48-57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyRequestConnectChain.h#L48-L57)：TCP 连接确认后，`ConnectCommand::executeInternal` 调 `chain_->run(this, e)`（[src/ConnectCommand.cc:102-112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L102-L112)），链对象创建 `HttpProxyRequestCommand`、置为 `STATUS_ONESHOT_REALTIME` 让引擎立刻执行。对照 [src/HttpRequestConnectChain.h:50-61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestConnectChain.h#L50-L61)：它创建的是 `HttpRequestCommand`，并且 L57 的 `c->setProxyRequest(t->getProxyRequest())` 把代理信息传了下去——这就是 GET 模式绝对 URI 的来源。

**两棒代理命令**。[src/HttpProxyRequestCommand.cc:56-61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyRequestCommand.cc#L56-L61) 的 `getNextCommand` 返回 `HttpProxyResponseCommand`（连同 `HttpConnection`、socket 一起交接）；[src/HttpProxyResponseCommand.cc:58-63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpProxyResponseCommand.cc#L58-L63) 在 200 之后返回**不带 proxyRequest 的** `HttpRequestCommand`——隧道内的一切与直连无异。

**切回后的 TLS 握手**。[src/HttpRequestCommand.cc:123-136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L123-L136)：`executeInternal` 开头若协议是 `https` 就在当前 socket 上 `tlsConnect(目标主机名)`；非阻塞未完成则按 `wantRead`/`wantWrite` 重挂事件自循环（u2-l4 的三段式）。注意 `tlsConnect` 的参数是**目标**主机——SNI 与证书校验都以目标服务器为准，代理对 TLS 层完全不可见。

**绝对 URI 与代理认证头**。[src/HttpRequest.cc:149-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L149-L166)：`createRequest` 里只要 `proxyRequest_` 非空，请求行就改用 `getCurrentURI()`（完整绝对 URL）而非 `getDir()+getFile()+getQuery()`（origin-form）；L216-220 追加 `Connection: Keep-Alive`；L221-223 在代理 URI 带用户名时附上 `Proxy-Authorization`。认证头的拼装在 [src/HttpRequest.cc:318-326](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L318-L326) 的 `getProxyAuthString`：`Basic Base64(代理用户名:代理密码)`——与目标服务器的 `Authorization`（u4-l3）平行而独立，两套凭证可以同时出现在一个请求里。

#### 4.3.4 代码实践

1. **实践目标**：在日志中亲眼看到「https 走代理出现 CONNECT，http 走代理出现绝对 URI 的 GET」。
2. **操作步骤**（只需一个能访问的代理，无需抓包）：
   - 设代理为 `PROXY=http://代理地址:端口`（局域网代理或自建，见第 5 节综合实践）。
   - A 组：`./src/aria2c --https-proxy=$PROXY https://example.com/ -l a.log --log-level=info -d /tmp/a2proxy --allow-overwrite=true`
   - B 组：`./src/aria2c --http-proxy=$PROXY http://example.com/ -l b.log --log-level=info -d /tmp/a2proxy --allow-overwrite=true`
   - 分别在 `a.log` / `b.log` 中搜索 `Requesting:`（或直接搜 `CONNECT`）。
3. **需要观察的现象**：A 组日志应出现 `CONNECT example.com:443 HTTP/1.1` 请求行，其后跟着 `Response received:` 与 `200`，再往后是 TLS 建立与 origin-form 的 `GET / HTTP/1.1`；B 组日志中**没有** CONNECT，请求行应为 `GET http://example.com/ HTTP/1.1`（绝对 URI）。
4. **预期结果**：与上面对应，两组日志形成「同一代理、两种方式」的对照。依据：请求行文本由 [HttpRequest.cc:289-316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L289-L316)（CONNECT）与 [HttpRequest.cc:149-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L149-L166)（绝对 URI）生成，并经 `MSG_SENDING_REQUEST` 以 info 级别落日志。能否跑通取决于代理是否放行对应站点，**待本地验证**。
5. 补充一个纯阅读实验：对照 A、B 两组日志中的命令名序列（`HttpProxyRequestCommand` 的进入痕迹即 CONNECT 请求行；B 组从 `Requesting` 直接是 GET），验证 4.3.1 的两张接力图。

#### 4.3.5 小练习与答案

**练习 1**：明文 `http://` 走代理时，命令链里为什么一个 `*ProxyCommand` 都没有？

答案：因为 GET 模式不需要隧道。`HttpInitiateConnectionCommand` 在 `proxyMethod == V_GET` 时给 `ConnectCommand` 装的是 `HttpRequestConnectChain`（[HttpInitiateConnectionCommand.cc:94-96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L94-L96)），连接成功后直接进入普通 `HttpRequestCommand`，仅靠 `proxyRequest` 让请求行变成绝对 URI。代理命令对只在 `V_TUNNEL` 时登场。

**练习 2**：连接池命中的隧道连接为什么可以跳过 CONNECT 直接发请求？池是怎么避免「直连连接」被当成「隧道连接」误用的？

答案：隧道是在连接建立时一次性打通的，socket 的字节流属性此后不变，所以复用时无需重做。池的键包含目标主机端口**和**代理主机端口（[HttpInitiateConnectionCommand.cc:77-80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L77-L80)），四元组不匹配就取不到，直连与经代理的连接因此隔离。

**练习 3**：`Proxy-Authorization` 与 `Authorization` 能否出现在同一个请求中？

答案：能。前者认证代理（凭证来自代理 URI），后者认证目标服务器（凭证来自 u4-l3 的 AuthConfig 体系），两者来源与拼装位置都独立（[HttpRequest.cc:221-229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L221-L229) 相邻的两段 if）。GET 模式下两者都会随绝对 URI 请求发出；隧道模式下 `Proxy-Authorization` 只出现在 CONNECT 里，隧道内的真实请求只带 `Authorization`。

### 4.4 FtpTunnel 链：FTP/SFTP 穿透代理的两种办法

#### 4.4.1 概念说明

FTP 不是 HTTP，SFTP 更不是，它们没法直接塞进 HTTP 报文。aria2 给出两条路：

- **GET 模式（`ftp://` 默认）**：把整个 FTP URI 当作绝对 URL 放进 HTTP `GET` 请求行，请求代理**代替自己去取 FTP 文件**。这要求代理支持「HTTP 前端 + FTP 后端」的协议转换（squid 等传统代理支持）。此时 aria2 侧完全退化为一次普通 HTTP 下载：方法被强制改成 GET，命令链就是 HTTP 那套。FTP 的凭证（用户名）被插进 URL（`ftp://USER@host/...`）传给代理。
- **隧道模式（`sftp://` 恒为隧道；`ftp://` 可用 `--proxy-method=tunnel` 强制）**：先 CONNECT 到 FTP 服务器，然后在管道里跑**原生 FTP/SFTP 会话**（u4-l5 将精读的 `FtpNegotiationCommand` / `SftpNegotiationCommand`）。代理只转发字节，不做协议转换。

所以「FTP over HTTP 代理多出哪几个命令」的答案是分情况的：GET 模式一个都不多（只是请求行变形）；**隧道模式多出 `FtpTunnelRequestCommand` 与 `FtpTunnelResponseCommand` 两棒**（外加它们赖以接力的 `ConnectCommand` controlChain）。

#### 4.4.2 核心流程

```
FtpInitiateConnectionCommand::createNextCommand(proxyRequest 非空)
  → createNextCommandProxied():
      proxyMethod = resolveProxyMethod(ftp|sftp)
      连接池查询：GET 按目标host/port+代理host/port；隧道额外带上 FTP 用户名
      无池可用:
        GET     → 强制 setMethod(GET)，ConnectChain = HttpRequestConnectChain
                  → HttpRequestCommand（绝对 URI，ftp://USER@host/...）
        TUNNEL  → ConnectChain = FtpTunnelRequestConnectChain
                  → FtpTunnelRequestCommand（CONNECT）
                  → FtpTunnelResponseCommand（等 200）
                  → sftp ? SftpNegotiationCommand : FtpNegotiationCommand(SEQ_SEND_CWD_PREP)
      池命中:
        TUNNEL  → 直接 FtpNegotiationCommand(SEQ_SEND_CWD_PREP) / SftpNegotiationCommand
        GET     → HttpRequestCommand + setProxyRequest
```

`SEQ_SEND_CWD_PREP` 这个起点值得注意：隧道复用（或刚建立）的连接此前已完成 FTP 登录，恢复会话时从「进入工作目录」开始，而不是重发 USER/PASS。

#### 4.4.3 源码精读

**分流与连接池差异**。[src/FtpInitiateConnectionCommand.cc:80-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L80-L130) 的 `createNextCommandProxied`：L87 求 `proxyMethod`（注释明确「sftp always use tunnel mode」）；L90-103 两种模式查池方式不同——GET 模式与 HTTP 一样按四元组，隧道模式额外传入** FTP 用户名**（隧道里的 FTP 会话与登录用户绑定，不同用户不能共用一条控制连接）。L112-129 新建连接时按方法装配：GET 分支 L115-119 先 `getRequest()->setMethod(Request::METHOD_GET)` 把方法强制为 GET 再挂 `HttpRequestConnectChain`；隧道分支 L120-122 挂 `FtpTunnelRequestConnectChain`。

**隧道命令对与「切回」**。[src/FtpTunnelRequestCommand.cc:57-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelRequestCommand.cc#L57-L62) 下一棒是 `FtpTunnelResponseCommand`；[src/FtpTunnelResponseCommand.cc:61-74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelResponseCommand.cc#L61-L74) 在 200 之后按协议分流：`sftp`（且编译了 libssh2）交给 `SftpNegotiationCommand`，否则交给 `FtpNegotiationCommand`——从这里开始命令链与直连 FTP 完全一致，代理退场。controlChain 一侧的 [src/FtpTunnelRequestConnectChain.h:48-57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelRequestConnectChain.h#L48-L57) 与 HTTP 版逐行同构，只是创建的命令换成 `FtpTunnelRequestCommand`。

**池命中后的两种恢复**。[src/FtpInitiateConnectionCommand.cc:132-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L132-L168)：隧道命中（L132-148）按协议直接给 `SftpNegotiationCommand`（SEQ_SFTP_OPEN）或带 `options`（内含 baseWorkingDir）的 `FtpNegotiationCommand`（SEQ_SEND_CWD_PREP）；GET 命中（L150-167）再次强制 GET 并给 `HttpRequestCommand` 挂上 `proxyRequest`。总分流函数在 [src/FtpInitiateConnectionCommand.cc:224-234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L224-L234)，与直连路径 `createNextCommandPlain`（L170-222）二选一。

**GET 模式下的 URL 改写**。[src/HttpRequest.cc:150-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L150-L157)：GET 模式构造请求行时，若目标是 `ftp`、URI 未内嵌用户名且 AuthConfig 有用户名，就把用户名插进 URI 变成 `ftp://USER@host/...`（`uri.insert(6, ...)` 的 6 正是 `ftp://` 的长度）——FTP 凭证以这种方式交给代理。另外 [src/HttpRequest.cc:208-213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L208-L213) 的注释解释了为什么 FTP over HTTP 代理不带 Range 终点：代理的 FTP 转换不支持 end byte。

#### 4.4.4 代码实践

1. **实践目标**：用源码+日志对照，说出「FTP over HTTP 代理」在两种 `--proxy-method` 下各自多出的命令。
2. **操作步骤**：
   - 阅读并画出 `createNextCommandProxied` 的两条分支的命令序列图（源码阅读型，必定可完成）。
   - 若有支持 FTP 的代理：分别运行
     `./src/aria2c --ftp-proxy=$PROXY ftp://ftp.gnu.org/gnu/ --proxy-method=get ... -l ftp-get.log --log-level=info` 与
     `./src/aria2c --ftp-proxy=$PROXY ftp://ftp.gnu.org/gnu/ --proxy-method=tunnel ... -l ftp-tunnel.log --log-level=info`。
   - 在 `ftp-get.log` 中搜索 `Requesting:`，看请求行是否为 `GET ftp://... HTTP/1.1`；在 `ftp-tunnel.log` 中搜索 `CONNECT`，并观察其后是否出现 FTP 会话痕迹（如 `220` 问候语、`USER`/`PASS`，来自 `FtpNegotiationCommand` 的日志）。
3. **需要观察的现象**：GET 日志里是 HTTP 命令链（无 FTP 命令），隧道日志里先 CONNECT 再接 FTP 控制协议交互。
4. **预期结果**：与源码分流一致。多数现代代理已不支持 FTP 转换或默认封锁，实际能否成功**待本地验证**；无论下载成败，仅观察日志中的命令形态也是有价值的实验。
5. 思考题（源码可答）：SFTP 为什么在源码里被注释为「always use tunnel mode」？

#### 4.4.5 小练习与答案

**练习 1**：FTP 隧道模式下连接池为什么要区分 FTP 用户名，而 GET 模式不用？

答案：隧道里跑的是原生 FTP 会话，一条已登录的控制连接绑定特定用户，换用户必须换连接（[FtpInitiateConnectionCommand.cc:95-103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L95-L103) 把 `createAuthConfig(...)->getUser()` 传给 `popPooledSocket`）；GET 模式下每次都是独立的 HTTP 请求，用户名只是 URL 的一部分，连接本身无会话状态，按四元组即可。

**练习 2**：隧道打通后 `FtpTunnelResponseCommand` 交给 `FtpNegotiationCommand` 的起始状态码是什么？为什么不是从登录开始？

答案：新建隧道时走构造函数默认值 `SEQ_RECV_GREETING`（[FtpTunnelResponseCommand.cc:71-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpTunnelResponseCommand.cc#L71-L73) 不传状态参数，默认值见 [FtpNegotiationCommand.h:162-167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.h#L162-L167)），从接收服务器问候语开始走完整登录；连接池命中复用时给的是 `SEQ_SEND_CWD_PREP`（[FtpInitiateConnectionCommand.cc:143-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc#L143-L147)），因为该连接上的 FTP 会话已登录，只需回到工作目录即可继续取文件。u4-l5 将展开整个状态机。

**练习 3**：`ftp://user:pass@host/file` 走 GET 模式代理时，user 会按 4.4.3 的「插入用户名」逻辑再插一次吗？

答案：不会。该逻辑的条件是 `request_->getUsername().empty()`（URI 未内嵌用户名）时才用 AuthConfig 的用户名插入（[HttpRequest.cc:150-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L150-L157)）；URI 已带 userinfo 时直接使用原 URI。

## 5. 综合实践

**任务：一份「协议 × 代理方式 → 命令链与报文形态」对照表的完整推导与验证。**

1. **准备本地代理**（任选其一）：
   - `sudo apt install squid` 后用默认配置（端口 3128）即可观察 CONNECT；或
   - `pip install mitmproxy && mitmproxy --mode regular --listen-port 3128`；或
   - 任何局域网内现成代理。
2. **准备一个明文 HTTP 目标**：`python3 -m http.server 8000`，放一个稍大的文件供下载。
3. **跑四组实验**（都加 `-l 日志 --log-level=info -d /tmp/a2proxy --allow-overwrite=true`）：
   - ① `http://127.0.0.1:8000/文件` + `--http-proxy=http://127.0.0.1:3128`
   - ② `https://example.com/` + `--https-proxy=http://127.0.0.1:3128`
   - ③ `http://example.com/` + `--http-proxy=http://127.0.0.1:3128 --proxy-method=tunnel`
   - ④ `ftp://ftp.gnu.org/gnu/` + `--ftp-proxy=http://127.0.0.1:3128`（分别用 `--proxy-method=get` 与 `tunnel` 各跑一次）
4. **整理观察**：每组在日志中找三类证据——(a) `Connecting to` 连的是代理还是目标；(b) `Requesting:` 的请求行形态（绝对 URI 的 GET / CONNECT / origin-form）；(c) 隧道组里 CONNECT 之后紧跟什么（TLS 痕迹或 FTP 控制协议）。
5. **产出**：一张五行表格：`场景 | 连接目标 | 请求行形态 | 经过的命令链 | 多出的代理命令`。预期（由源码推出，供核对）：

   | 场景 | 连接目标 | 请求行形态 | 命令链（关键段） | 多出的代理命令 |
   | --- | --- | --- | --- | --- |
   | http + 默认 GET | 代理 | `GET http://... HTTP/1.1` | Connect→HttpRequest→HttpResponse→Download | 无 |
   | https（恒隧道） | 代理 | `CONNECT host:443` 后 `GET / HTTP/1.1` | Connect→HttpProxyRequest→HttpProxyResponse→HttpRequest(TLS)→… | 2 个 |
   | http + 强制隧道 | 代理 | `CONNECT host:80` 后 origin-form | Connect→HttpProxyRequest→HttpProxyResponse→HttpRequest→… | 2 个 |
   | ftp + GET | 代理 | `GET ftp://USER@host/... HTTP/1.1` | Connect→HttpRequest→…（HTTP 链） | 无 |
   | ftp/sftp + 隧道 | 代理 | `CONNECT host:21/22` 后原生 FTP/SFTP | Connect→FtpTunnelRequest→FtpTunnelResponse→Ftp/SftpNegotiation→… | 2 个 |

6. **可选加深**：`sudo tcpdump -A -s0 'port 3128'` 抓包，验证明文代理跳上 CONNECT 与绝对 URI 均可裸眼可见，而隧道内（https）内容不可读——印证 2.2/2.5 节。
7. 所有涉及真实网络与代理的观察结果均**待本地验证**；若无法安装代理，可仅凭第 4、5 步的源码依据完成表格推导，并阅读 [src/HttpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc) 与 [src/FtpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpInitiateConnectionCommand.cc) 逐行核对表中每一格。

## 6. 本讲小结

- aria2 走 HTTP 代理有两种方式：**GET（绝对 URI）** 用于明文 `http` 与（默认）`ftp`，**CONNECT 隧道** 用于 `https`/`sftp`（恒为隧道）及被 `--proxy-method=tunnel` 强制的 `http`/`ftp`；决策函数是 `AbstractCommand::resolveProxyMethod`。
- 代理地址按「协议专属选项 > `--all-proxy`」解析（`getProxyUri`），受 `--no-proxy`（主机/域/CIDR）豁免；结果装进 `proxyRequest`，使 DNS 解析与 TCP 连接的目标换成代理。到代理这一跳被选项处理器强制为明文 HTTP。
- `AbstractProxyRequestCommand`/`AbstractProxyResponseCommand` 是 CONNECT 隧道的公共「发/收」骨架：发 CONNECT、等 200，非 200 抛 `EX_PROXY_CONNECTION_FAILED` 走可重试出口；下一棒由子类的 `getNextCommand()` 决定。
- 隧道打通即「切回」：HTTP(S) 回到不带 proxyRequest 的 `HttpRequestCommand`（TLS 在隧道内与目标握手），FTP/SFTP 回到 `FtpNegotiationCommand`/`SftpNegotiationCommand`，此后命令链与直连完全一致；`ConnectCommand` 通过 `ControlChain` 三选一决定入口。
- 代理认证独立于目标认证：`Proxy-Authorization: Basic ...` 由代理 URI 的 userinfo 生成（`getProxyAuthString`），与 `Authorization` 可并存；日志中两者都被 `eraseConfidentialInfo` 替换为 `<snip>`。
- GET 模式下一个代理命令都不会出现——只有请求行从 origin-form 变为绝对 URI（FTP 还会改写成 `ftp://USER@host/...`）；隧道模式则固定多出 Request/Response 两个代理命令。

## 7. 下一步学习建议

- **u4-l5（FTP 与 SFTP 命令链）**：本讲隧道打通后交给的 `FtpNegotiationCommand` 正是下一讲主角，`SEQ_SEND_CWD_PREP` 等状态码会在那里展开成完整状态机；SFTP 侧的 `SftpNegotiationCommand` 同样承接自本讲。
- **u4-l6（流式过滤）**：GET 模式的 FTP over HTTP 代理本质是一次 HTTP 下载，若响应带 chunked/gzip，过滤链如何挂接在 `DownloadCommand` 上是自然的后续。
- **回看 u4-l2**：本讲大量复用 `HttpConnection` 的在途请求队列与 `HttpHeaderProcessor`，若对其增量解析仍有疑问，建议重读那一讲的报文解析部分。
- **u8-l3（内嵌 HTTP 服务器）**：aria2 自己也实现了一个 HTTP 服务端（RPC 用），与本讲的客户端视角互为镜像，读完可对照 `HttpHeaderProcessor::CLIENT_PARSER` 与服务端模式的差异。
