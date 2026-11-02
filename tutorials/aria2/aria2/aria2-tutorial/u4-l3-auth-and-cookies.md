# 认证与 Cookie：AuthConfig、Netrc 与 CookieStorage

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 aria2 认证体系的两层抽象——`AuthResolver`（决定用哪组凭证）与 `AuthConfig`（一组已确定的用户名/密码）——各自的职责边界。
2. 跟踪 `--http-user`、`--netrc`（`~/.netrc`）、`--load-cookies` 三个入口在源码中的取值优先级，解释「URI 内嵌 userinfo ＞ 命令行选项 ＞ netrc ＞ 匿名默认值」这条链是如何在代码里落地的。
3. 理解 `CookieStorage` 的反向域名标签树结构、LRU 域淘汰、每域 50 条上限，以及 `Cookie::match` 的域名/路径/secure/过期四重匹配规则。
4. 独立完成一次「准备 cookies.txt → `--load-cookies` 下载 → 在源码关键点加日志验证 Cookie 命中并随请求发出」的闭环实验。

本讲承接 u4-l2：上一讲我们看到 `HttpRequest::createRequest` 在**发送时刻**才把各种状态求值为请求头文本，本讲就专门解剖其中两个头——`Authorization:` 与 `Cookie:`——的完整来历。

## 2. 前置知识

### 2.1 HTTP Basic 认证（RFC 7617）

最简单的 HTTP 认证方案：客户端把 `用户名:密码` 拼接后做 Base64 编码，放进请求头：

```http
Authorization: Basic dXNlcjpwYXNz
```

其中 `dXNlcjpwYXNz` 就是 `user:pass` 的 Base64。它**不加密**，只是编码，所以 Basic 认证几乎总是配合 HTTPS 使用。服务器拒绝时返回 `401 Unauthorized`（这就是后面要讲的「挑战」）。

### 2.2 FTP 与 SFTP 的认证

FTP 协议在控制连接上用 `USER` / `PASS` 两条命令登录，没有 HTTP 那种头部机制，所以 FTP 的凭证不走 `Authorization` 头，而是直接传给 `FtpConnection`（详见 u4-l5）。但**决定用哪组用户名密码的逻辑与 HTTP 共用同一套工厂**，这正是本讲的抽象价值所在。匿名 FTP 使用 `anonymous` + 邮箱格式密码，aria2 的默认密码是 `ARIA2USER@`。

### 2.3 netrc 文件

`.netrc` 是一个历史悠久的约定（源自 BSD 的 `ftp` 命令）：把各主机的登录凭证集中存在家目录的文本文件里，权限必须是 600。格式是「关键字 + 值」的 token 序列：

```
machine ftp.example.org login alice password secret
default login anonymous password aria2@example.com
```

- `machine <名称>`：开始一条主机记录，`login`/`password`/`account` 设置字段。
- `default`：一条能匹配任何主机的兜底记录。
- `macdef`：定义宏，aria2 直接跳过宏体（直到空行）。
- 机器名以 `.` 开头表示允许匹配所有子域。

curl、wget、lftp 等工具都支持这个格式。

### 2.4 Cookie 与 RFC 6265

Cookie 是服务器借 `Set-Cookie` 响应头写入浏览器（这里是 aria2）的小段状态，客户端后续请求用 `Cookie` 请求头带回。每条 Cookie 的关键属性：

| 属性 | 含义 |
| --- | --- |
| name / value | 键值对本体 |
| domain | 所属域；`hostOnly=true` 时仅精确匹配该主机，否则允许子域后缀匹配 |
| path | 所属路径前缀 |
| expiryTime | 过期时刻；没有 Expires/Max-Age 的是**会话 Cookie**（session cookie），进程结束即失效 |
| secure | 仅经 HTTPS 发送 |
| httpOnly | 禁止非 HTTP 通道读取（aria2 解析但下载场景基本用不到） |

### 2.5 Netscape cookies.txt 七列格式

Firefox/Netscape 系导出的 Cookie 文件是 TSV（Tab 分隔），每行七列：

```
域名	是否跨子域	路径	是否仅安全连接	过期时间(epoch秒)	名字	值
```

例如（示例数据）：

```
.example.com	TRUE	/	FALSE	1798761600	session	abc123
```

域名为 `.example.com` 表示可在子域间共享；过期时间为 `0` 表示会话 Cookie。文件以 `#` 开头的行是注释——这个细节在本讲的实践环节会带来一个「意外」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/AuthConfig.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfig.h) / [src/AuthConfig.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfig.cc) | 一组确定的凭证（user/password）及其序列化 |
| [src/AuthResolver.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthResolver.h) / [src/AbstractAuthResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractAuthResolver.cc) / [src/DefaultAuthResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultAuthResolver.cc) | 「按主机名解析凭证」的接口与两级通用兜底 |
| [src/NetrcAuthResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetrcAuthResolver.cc) | 在 netrc 中查找凭证的解析器 |
| [src/Netrc.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc) | netrc 文件的解析与主机匹配 |
| [src/AuthConfigFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc) | 总装配：按协议分支、组合各入口优先级、维护 BasicCred 缓存 |
| [src/Cookie.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.h) / [src/Cookie.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc) | 单条 Cookie 的数据模型、匹配与 Netscape 格式序列化 |
| [src/cookie_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/cookie_helper.cc) | RFC 6265 的 Set-Cookie 解析、域名/路径匹配纯函数、日期解析 |
| [src/CookieStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc) | Cookie 仓库：反向域名树、LRU 淘汰、加载/保存 |
| [src/NsCookieParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NsCookieParser.cc) | Netscape cookies.txt 的逐行解析 |
| 挂接点：[src/HttpRequest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc)、[src/HttpRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc)、[src/HttpResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc)、[src/HttpSkipResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc)、[src/HttpResponse.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc)、[src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc) | 请求头生成、Set-Cookie 接收、401 处理、启动时加载 |

对应的测试（本讲实践的依据来源）：`test/AuthConfigFactoryTest.cc`、`test/NetrcTest.cc`、`test/CookieHelperTest.cc`、`test/CookieStorageTest.cc`、`test/NsCookieParserTest.cc`。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：4.1 认证的两层抽象 → 4.2 AuthConfigFactory 与取值优先级 → 4.3 Netrc 解析 → 4.4 Cookie 数据模型与匹配 → 4.5 CookieStorage 仓库。

### 4.1 AuthConfig 与 AuthResolver：认证的两层抽象

#### 4.1.1 概念说明

aria2 把「认证」拆成两个正交的问题：

- **AuthConfig：答案**。一组已经确定的 `(用户名, 密码)`，再无歧义。它的全部工作就是保存这两个字符串并能拼出 `user:password` 文本。
- **AuthResolver：决策过程**。给定一个主机名，回答「这次连接该用哪组 AuthConfig」。答案可能来自命令行选项、netrc 文件、URI 内嵌的 userinfo，或者「没有认证」。

为什么值得分两层？因为「凭证从哪来」有五六种来源、还要按协议（HTTP/FTP/SFTP）区分，而「凭证怎么用」是稳定的——HTTP 拼成 `Authorization: Basic ...` 头，FTP 填进 `USER`/`PASS` 命令。把易变的来源决策收进 Resolver，把稳定的载体留给 AuthConfig，新增一种来源（比如未来的凭据管理器）就不必触碰协议层。

#### 4.1.2 核心流程

```text
发送 HTTP 请求前（HttpRequest::createRequest）
  │
  ├─ authConfigFactory->createAuthConfig(request, option)   ← 每次发请求都重新求值
  │      │
  │      └─ 按协议分派到 createHttpAuthResolver / createFtpAuthResolver
  │             │
  │             └─ resolver->resolveAuthConfig(host)
  │                    ├── ① 用户显式指定（URI userinfo / --http-user）
  │                    ├── ② netrc 中该主机的条目
  │                    └── ③ 默认值（FTP 匿名；HTTP 则为空＝不带认证）
  │
  ├─ 若得到 AuthConfig → 生成 "Authorization: Basic base64(user:password)"
  └─ 若为 nullptr      → 不发送 Authorization 头
```

关键点：**认证是逐请求求值的，不是启动时定死**。同一个任务重定向到另一台主机后，下一次 `createRequest` 会重新解析。

#### 4.1.3 源码精读

**AuthConfig 本体极小**——两个字符串加一个拼接函数：

[AuthConfig.cc:L52-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfig.cc#L52-L69)
`getAuthText()` 把用户名和密码拼成 `user:password`；静态工厂 `create()` 规定**用户名为空即视为「无认证」**，返回 `nullptr` 而不是构造一个空凭证。这个约定贯穿全体系：所有解析器最后都调它，用户名为空就自动「降级为不带认证」。

**AuthResolver 是一个只有纯虚函数的接口**：

[AuthResolver.h:L47-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthResolver.h#L47-L53)
```cpp
class AuthResolver {
public:
  virtual ~AuthResolver() = default;
  virtual std::unique_ptr<AuthConfig>
  resolveAuthConfig(const std::string& hostname) = 0;
};
```

**AbstractAuthResolver 提供两级通用兜底**：

[AbstractAuthResolver.cc:L45-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractAuthResolver.cc#L45-L68)
它保存两组凭证——`userDefinedUser_/Password_`（由选项如 `--http-user` 注入）和 `defaultUser_/Password_`（如 FTP 的匿名对），并提供 `getUserDefinedAuthConfig()` / `getDefaultAuthConfig()` 两个取值函数。注意两者都走 `AuthConfig::create`，所以「用户名为空」的兜底同样生效：HTTP 场景没人调用 `setDefaultCred`，默认用户名是空串，`getDefaultAuthConfig()` 返回 `nullptr`，最终效果就是**不带 Authorization 头**。

**求值时机与头的生成**（承接 u4-l2）：

[HttpRequest.cc:L144-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L144-L146)
`createRequest()` 第一行就调用 `createAuthConfig`，把结果缓存在 `authConfig_` 成员里。

[HttpRequest.cc:L224-L229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L224-L229)
```cpp
if (authConfig_) {
  auto authText = authConfig_->getAuthText();
  std::string val = "Basic ";
  val += base64::encode(std::begin(authText), std::end(authText));
  builtinHds.emplace_back("Authorization:", val);
}
```
这正是 RFC 7617 的逐字实现：`"Basic " + base64(user:password)`。工厂指针是 `HttpRequestCommand` 在创建 `HttpRequest` 时注入的（[HttpRequestCommand.cc:L97-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L97-L98)，同一行还注入了 CookieStorage——见 4.5）。

#### 4.1.4 代码实践

**实践目标**：验证 `Authorization` 头的生成与 Base64 编码。

1. 操作步骤：
   - 在终端执行 `printf 'alice:secret' | base64`，记下输出（应为 `YWxpY2U6c2VjcmV0`）。
   - 找一个需要 Basic 认证的测试服务（例如本地用 `python3 -m http.server` 加一层简单认证，或使用任何 httpbin 类服务），运行：
     `./src/aria2c --http-user=alice --http-passwd=secret -l=auth.log --log-level=info <URL>`
   - 若无现成服务，可改用 `curl -u alice:secret -v <URL>` 对比观察请求头。
2. 需要观察的现象：aria2 能成功通过认证；日志或服务端访问日志中出现 `Authorization: Basic YWxpY2U6c2VjcmV0`。
3. 预期结果：手工 Base64 结果与线上抓到的头一致，证明 4.1.3 引用的那段代码就是头的唯一产地。
4. 具体网络环境因人而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AuthConfig::create("", "whatever")` 必须返回 `nullptr` 而不是空用户的凭证？
**答案**：Basic 认证里空用户名没有意义；返回 `nullptr` 让「无认证」成为一等状态，下游 `HttpRequest` 只需判空即可决定是否生成头，避免了「空凭证头」这种非法状态扩散。

**练习 2**：`DefaultAuthResolver`（[DefaultAuthResolver.cc:L40-L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultAuthResolver.cc#L40-L50)）和 `NetrcAuthResolver` 的 `resolveAuthConfig` 第一步完全相同，区别在哪里？
**答案**：两者都先试 `getUserDefinedAuthConfig()`；区别在兜底路径——`DefaultAuthResolver` 拿不到就直接返回 `getDefaultAuthConfig()`（不查任何文件），`NetrcAuthResolver` 则先查 netrc，查不到才落到默认值，且可用 `ignoreDefault` 跳过 netrc 的 `default` 条目（见 4.2/4.3）。

### 4.2 AuthConfigFactory：取值优先级与 401 挑战应答

#### 4.2.1 概念说明

`AuthConfigFactory` 是认证体系的总装配车间，由 `DownloadEngine` 独占持有（每个进程一个），内部保存两样东西：

1. 解析好的 `Netrc` 对象（启动时从 `~/.netrc` 加载）；
2. `basicCreds_`：一个按 `(host, port, path)` 组织的 `BasicCred` 集合，用于 `--http-auth-challenge=true` 模式下「被动应答」的凭证缓存。

它要回答的核心问题是：**给定一个 `Request`（协议、URI userinfo、主机、目录）和 `Option`，该用哪组凭证？**

#### 4.2.2 核心流程

`createAuthConfig` 的判定树（默认 `--http-auth-challenge=false` 时）：

```text
request 是 http/https？
 ├─ 否（ftp/sftp）→ 见下方 FTP 分支
 └─ 是
    ├─ URI 内嵌 userinfo（http://user:pass@host/）？ → 直接用它（最高优先级）
    └─ 否 → createHttpAuthResolver(op)->resolveAuthConfig(host)
         ├── --http-user 非空 → 用之
         ├── 否则查 netrc 的 machine 条目（default 条目被 ignoreDefault 跳过）
         └── 都没有 → nullptr（不带认证头）

FTP/SFTP 分支：
 ├─ URI 内嵌用户名 + 密码 → 直接用
 ├─ URI 内嵌用户名、无密码
 │    ├─ netrc 中该主机条目的 login 恰好等于此用户名 → 用 netrc 密码
 │    └─ 否则 → 用户名 + --ftp-passwd 的值
 └─ 无 userinfo → createFtpAuthResolver：--ftp-user → netrc（含 default 条目）
      → 匿名默认 anonymous/ARIA2USER@
```

`--http-auth-challenge=true` 时 HTTP 分支变为「被动模式」：平时不带 Authorization；收到 `401` 后调 `activateBasicCred` 把凭证登记进 `basicCreds_` 并重试一次，之后的请求按 `(host, port, path)` 前缀直接取缓存。

#### 4.2.3 源码精读

**总入口 `createAuthConfig`**：

[AuthConfigFactory.cc:L59-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L59-L124)
先按协议分流（L63 判 http/https，L92 判 ftp/sftp，其余协议返回 nullptr）。HTTP 分支里：`--http-auth-challenge` 开启时（L64-80），URI userinfo 会经 `updateBasicCred` 登记进缓存再返回；否则优先 URI userinfo（L82-85），再走解析器（L87-88）。FTP 分支的精髓在 L100-114：用户名在 URI 里但密码缺失时，临时构造一个 `NetrcAuthResolver` 查 netrc，**并且校验 netrc 条目的 login 与 URI 用户名一致**（L107）才采用——防止拿别人的密码配自己的用户名。

**两个解析器的差异**：

[AuthConfigFactory.cc:L126-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L126-L160)
- HTTP 版（L126-142）：`--no-netrc` 时用 `DefaultAuthResolver`（完全不碰 netrc）；否则用 `NetrcAuthResolver` 并调用 `ignoreDefault()`——**HTTP 不接受 netrc 的 default 兜底条目**（安全考虑：不希望一个松散的 default 把任意主机的请求都带上凭证）。用户级凭证来自 `--http-user/--http-passwd`；不设默认值。
- FTP 版（L144-160）：同样二选一，但**不** ignoreDefault，且 `setDefaultCred(AUTH_DEFAULT_USER, AUTH_DEFAULT_PASSWD)`。匿名对定义在 [AuthConfigFactory.cc:L50-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L50-L53)：`anonymous` / `ARIA2USER@`。

**BasicCred 与最长路径匹配**：

[AuthConfigFactory.cc:L202-L230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L202-L230)
`BasicCred` 构造时给 path 补尾部 `/`（L211-213），保证前缀比较以目录为单位。`operator<` 在 host、port 相同时按 **path 长度降序** 排列（L227-229 中 `path_ > cred.path_` 使长路径「更小」、排在前面）。

[AuthConfigFactory.cc:L232-L246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L232-L246)
`findBasicCred` 从 `lower_bound` 起线性扫描同主机的候选，返回**第一个满足「请求路径以凭证 path 为前缀」**的条目。由于集合按路径长度降序，命中的就是最长匹配——例如 `/download/` 与 `/` 两条凭证并存时，请求 `/download/file` 会先匹配前者。

**401 挑战的处理位置**：

[HttpSkipResponseCommand.cc:L207-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L207-L215)
```cpp
case 401:
  if (getOption()->getAsBool(PREF_HTTP_AUTH_CHALLENGE) &&
      !httpResponse_->getHttpRequest()->authenticationUsed() &&
      getDownloadEngine()->getAuthConfigFactory()->activateBasicCred(
          getRequest()->getHost(), getRequest()->getPort(),
          getRequest()->getDir(), getOption().get())) {
    return prepareForRetry(0);
  }
  throw DL_ABORT_EX2(EX_AUTH_FAILED, error_code::HTTP_AUTH_FAILED);
```
三个条件依次是：选项开启、**本次请求尚未带过认证**（`authenticationUsed()` 即 [HttpRequest.cc:L361](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L361) 的 `authConfig_.get() != nullptr`，防止带错密码导致 401→重试→再 401 的死循环）、且能解析出凭证。满足则 `activateBasicCred`（[AuthConfigFactory.cc:L178-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L178-L200)）登记后重试；否则抛 `HTTP_AUTH_FAILED`（错误码 35，见 u9-l3）。

#### 4.2.4 代码实践

**实践目标**：用测试用例验证优先级链「URI userinfo ＞ --http-user ＞ netrc ＞ 无认证」。

1. 操作步骤：打开 [test/AuthConfigFactoryTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AuthConfigFactoryTest.cc)，搜索 `createAuthConfig` 相关用例（如 `testCreateAuthConfig_http`、`testCreateAuthConfig_httpFtp` 一类命名），逐个阅读其 arrange-assert 结构；对照本节判定树，把每个用例的输入标注到判定树的相应分支上。
2. 需要观察的现象：测试如何构造 `Request`（含/不含 userinfo）、`Option`（注入 PREF_HTTP_USER）、`Netrc`（`setNetrc`）三个变量，断言 `createAuthConfig` 返回的 user/password。
3. 预期结果：每条断言都能在 4.2.2 的判定树上找到唯一路径；如发现某个用例覆盖了判定树没写的分支，回头补全你自己的流程图。
4. 进阶（可选）：仿照现有用例写一个「URI 有 userinfo 但 --no-netrc 且 --http-user 也存在」的新用例，预期 userinfo 胜出，跑 `make check` 验证。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`--http-auth-challenge` 默认值是什么？开启后行为有何变化？
**答案**：默认 `false`（[OptionHandlerFactory.cc:L1108-L1110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1108-L1110)，默认 `A2_V_FALSE`）。关闭时每个请求都主动带 Authorization；开启后先「裸发」，收到 401 才激活凭证重试一次并缓存到 `basicCreds_`，之后同 (host, port, path) 前缀的请求直接命中缓存。

**练习 2**：为什么 FTP 的解析器可以接受 netrc 的 `default` 条目，HTTP 却要 `ignoreDefault()`？
**答案**：FTP 生态本来就有匿名兜底（anonymous），default 条目无害；HTTP 带凭证意味着把密码主动发给服务器，若一条松散的 `default` 让任意主机的请求都携带同一组密码，就是敏感信息泄漏。所以 HTTP 侧显式屏蔽（[AuthConfigFactory.cc:L136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L136)）。

**练习 3**：`findBasicCred` 为什么必须「最长路径优先」？
**答案**：HTTP Basic 凭证在 RFC 语义上按保护空间（realm + path 前缀）划分。站点可能对 `/admin/` 与 `/public/` 配不同账号，若短路径（如 `/`）先命中，访问 `/admin/x` 就会带上错误凭证。集合按 path 长度降序排列后线性扫描首个前缀匹配，天然实现最长匹配。

### 4.3 Netrc 文件解析与 NetrcAuthResolver

#### 4.3.1 概念说明

`Netrc` 类负责把 `~/.netrc` 文本解析成一组 `Authenticator` 记录并按主机名查找；`NetrcAuthResolver` 把它接入 4.1 的解析器接口。注意分工：**Netrc 只管「存储 + 查找」，不管优先级**——优先级（用户选项是否压过 netrc）永远由上层 Resolver 编排。

#### 4.3.2 核心流程

解析是一个逐 token 的状态机：

```text
初始 state = GET_TOKEN
对每行的每个 token：
  GET_TOKEN 态：
    "machine"  → 落盘上一条记录，开新 Authenticator，state=SET_MACHINE
    "default"  → 落盘上一条记录，开新 DefaultAuthenticator（match 恒真）
    "login"/"password"/"account" → 当前记录设置对应字段（下一 token 是值）
    "macdef"   → 跳过整个宏体（读到空行）
    其他       → 若还没有当前记录则报解析错误
  等待值态（SET_MACHINE 等）：把 token 写入字段，回到 GET_TOKEN
EOF 时若仍在等值 → 报错；最后一条记录落盘
```

查找时对 `authenticators_` 顺序 `find_if`，**第一条 match 的记录生效**——因此 netrc 中记录的书写顺序就是优先级。

#### 4.3.3 源码精读

**主机匹配规则**：

[Netrc.cc:L63-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc#L63-L66)
`Authenticator::match` 委托给 `util::noProxyDomainMatch(hostname, machine_)`，其实现位于 [util.cc:L2370-L2376](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2370-L2376)：机器名以 `.` 开头（且不是 IP）时做后缀匹配（子域通配），否则要求全等。`DefaultAuthenticator::match` 恒真（[Netrc.cc:L97-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc#L97-L100)），所以它放在文件末尾就是兜底，放前面就是「全抢」。

**解析状态机**：

[Netrc.cc:L130-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc#L130-L224)
逐行读取（`#` 开头跳过，L160-162），用 `util::splitIterM` 按空格/Tab 切成 token 流后驱动上面的状态机。注意它是 **token 级而非行级**：`machine x login y password z` 写在一行也合法。`macdef` 宏体由 [Netrc.cc:L112-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc#L112-L127) 的 `skipMacdef` 整段吞掉。文件打不开或 IO 错误抛 `DL_ABORT_EX`，该异常在启动路径被捕获后 netrc 直接不启用（见下）。

**顺序查找**：

[Netrc.cc:L248-L259](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Netrc.cc#L248-L259)
`findAuthenticator` 用 `std::find_if` + `AuthHostMatch` 谓词（L233-246），返回首条匹配的裸指针，全不匹配返回 nullptr。

**NetrcAuthResolver 的编排**：

[NetrcAuthResolver.cc:L46-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetrcAuthResolver.cc#L46-L78)
`resolveAuthConfig`：先用户级凭证（`--http-user` 等），没有则 `findNetrcAuthenticator`。后者有三级出口：netrc 未加载 → 默认值；netrc 查无 → 默认值；查到但 `ignoreDefault_` 且命中条目恰是 `default`（machine 为空）→ 也退回默认值；其余用 netrc 条目的 login/password 构造 AuthConfig。`ignoreDefault_` 正是 4.2 中 HTTP 侧显式打开的那个开关。

**启动时加载与权限检查**：

[MultiUrlRequestInfo.cc:L231-L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L231-L250)
`MultiUrlRequestInfo::prepare()` 中创建 `AuthConfigFactory`；当 `--no-netrc` 关闭且 netrc 文件存在时，先检查文件权限（组/其他可读写则记 NOTICE 拒绝使用，L240-243），否则 `Netrc::parse` 后 `setNetrc` 注入工厂。`--netrc-path` 默认 `$HOME/.netrc`（[OptionHandlerFactory.cc:L1287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1287)），`-n` 是 `--no-netrc` 的短选项（[OptionHandlerFactory.cc:L884](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L884)）。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍 netrc 的「解析 → 匹配 → 采用」链路。

1. 准备 `~/.netrc`（示例数据，权限须为 600：`chmod 600 ~/.netrc`）：
   ```
   # 我的 FTP 凭证（示例数据）
   machine ftp.example.org login alice password secret
   machine .files.example.org login bob password hunter2
   default login anonymous password me@example.com
   ```
2. 验证子域匹配：把 `ftp.example.org` 换成一个你可访问的主机名后运行
   `./src/aria2c ftp://ftp.example.org/somefile -l=netrc.log --log-level=info -o /dev/null`
   再把 URL 主机改成 `ftp.files.example.org` 观察是否命中第二条（后缀匹配）。
3. 阅读型验证（无需网络）：打开 [test/NetrcTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/NetrcTest.cc)，找到 `testFindAuthenticator` 一类用例，抄录它使用的 fixture 文本与断言，对照 4.3.2 状态机手工「人肉解析」一遍，核对每条记录的 machine/login/password。
4. 需要观察的现象：步骤 2 中若把 `chmod 644 ~/.netrc`，日志应出现权限警告（对应 [MultiUrlRequestInfo.cc:L240-L243](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L240-L243)），且 netrc 不生效。
5. 预期结果：能准确预测任意主机名会命中哪条记录。网络部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`machine .example.org` 与 `machine example.org` 在匹配上有什么区别？
**答案**：前者是后缀匹配（`www.example.org`、`a.example.org` 都命中），后者只精确匹配 `example.org` 本身——依据是 `noProxyDomainMatch` 对首字符 `.` 的特判（[util.cc:L2372-L2375](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2372-L2375)）。

**练习 2**：`default` 条目写在 `machine` 条目之前会发生什么？
**答案**：`findAuthenticator` 顺序扫描返回首条 match，而 `DefaultAuthenticator::match` 恒真，所以 default 会吞掉所有查找，后面的 machine 条目永远轮不到。惯例是把 default 放最后。

**练习 3**：FTP 场景下「URI 只有用户名没有密码」时 netrc 如何参与？
**答案**：`createAuthConfig` 的 FTP 分支临时构造 `NetrcAuthResolver` 查主机对应条目，只有当条目的 login 与 URI 用户名一致时才采用其密码，否则退回 `--ftp-passwd`（[AuthConfigFactory.cc:L100-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AuthConfigFactory.cc#L100-L115)）。

### 4.4 Cookie：RFC 6265 的数据模型与匹配规则

#### 4.4.1 概念说明

`Cookie` 类是单条 Cookie 的完整快照：九个属性字段（见 [Cookie.h:L46-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.h#L46-L61)：expiryTime/creationTime/lastAccessTime、name/value、domain/path、persistent/hostOnly/secure/httpOnly）。本模块回答两个问题：

1. **一条 Cookie 什么时候随请求发出？**——`Cookie::match` 的四重判定。
2. **一条 Set-Cookie 字符串如何变成 Cookie 对象？**——`cookie::parse` 的 RFC 6265 算法。

#### 4.4.2 核心流程

`Cookie::match(requestHost, requestPath, now, secure)` 的判定顺序：

```text
任一为假即整体不匹配：
  ① secure_ ⇒ secure        （secure Cookie 只走 HTTPS）
  ② ¬isExpired(now)          （持久 Cookie 过期即淘汰；会话 Cookie 永不过期）
  ③ pathMatch(requestPath, path_)
然后做域判定：
  hostOnly_ ? requestHost == domain_          （精确匹配）
            : domainMatch(requestHost, domain_)（允许子域后缀匹配）
```

域与路径匹配的形式化定义（与源码逐字对应）：

\[ \mathrm{domainMatch}(h, d) \iff h = d \;\lor\; \big(h \text{ 以 } d \text{ 结尾} \,\land\, h[|h|-|d|-1] = \text{‘.’} \,\land\, h \text{ 非 IP}\big) \]

\[ \mathrm{pathMatch}(p, c) \iff p = c \;\lor\; \big(p \text{ 以 } c \text{ 开头} \,\land\, (c \text{ 以 } \text{‘/’} \text{ 结尾} \,\lor\, p[|c|] = \text{‘/’})\big) \]

即域匹配要求「整段标签对齐」（`www.example.com` 匹配 `example.com`，但 `baddomain.com` 不匹配 `domain.com`——差异处必须是 `.`）；路径匹配要求「目录边界对齐」（`/a/b` 匹配 `/a`，`/abc` 不匹配 `/a`）。

#### 4.4.3 源码精读

**match 的实现**：

[Cookie.cc:L82-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L82-L96)
```cpp
if ((secure_ && !secure) || isExpired(date) ||
    !cookie::pathMatch(requestPath, path_)) {
  return false;
}
if (hostOnly_) {
  return requestHost == domain_;
}
else {
  return cookie::domainMatch(requestHost, domain_);
}
```
短路求值把最廉价的否决条件放在前面。

**会话 Cookie 的过期特例**：

[Cookie.cc:L109-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L109-L112)
`isExpired` 返回 `persistent_ && base > expiryTime_`——非持久（会话）Cookie 恒不过期，进程退出时随 CookieStorage 一起消失（除非 `--save-cookies`，见 4.5：保存时会话 Cookie 也被写出，但 expiry 写 0）。

**两个匹配纯函数**：

[cookie_helper.cc:L407-L420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/cookie_helper.cc#L407-L420)
`domainMatch` 与 `pathMatch` 的实现就是 4.4.2 两个公式的直译，域匹配额外排除 IP 主机（IP 不能谈子域）。

**Set-Cookie 解析（RFC 6265 §5.2）**：

[cookie_helper.cc:L219-L394](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/cookie_helper.cc#L219-L394)
`cookie::parse` 先按第一个 `;` 与 `=` 切出 name/value（无 `=` 直接拒收，L233-235），再逐段解析 `expires`/`max-age`/`domain`/`path`/`secure`/`httponly` 属性。三处关键裁决：
- L352-362：有 Max-Age 或 Expires 才算持久 Cookie，否则 expiry 置最大值且 `persistent=false`（会话 Cookie）。
- L364-374：未写 domain 则 `hostOnly=true` 且域取请求主机；写了 domain 则必须 `domainMatch(请求主机, 该域)`，否则**整条拒收**——服务器不能给无关域种 Cookie。
- `expires` 的日期解析 [cookie_helper.cc:L83-L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/cookie_helper.cc#L83-L217) 逐字实现 RFC 6265 §5.1.1 的宽松日期算法（大小写不敏感月份缩写、两位年份补全、闰年校验等），是本模块里最长的一段纯函数。

**接收侧的入口**（承接 u4-l2 的 HttpHeader 白名单——`Set-Cookie` 是允许重复的少数头之一）：

[HttpResponse.cc:L134-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L134-L143)
`retrieveCookie` 用 `equalRange(SET_COOKIE)` 取出全部同名头，逐条 `parseAndStore`。调用点在 [HttpResponseCommand.cc:L156-L158](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L156-L158)：状态码校验通过后立刻执行——即使这次下载失败，Cookie 也已经入库。

**发送侧的入口**：

[HttpRequest.cc:L233-L246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L233-L246)
`createRequest` 里若注入了 `cookieStorage_`，就调 `criteriaFind(host, path, now, protocol=="https")` 收集命中 Cookie，以 `name=value;` 形式拼进 `Cookie:` 头（`secure` 参数由协议是否 https 决定，正是 4.4.2 判定①的输入）。

#### 4.4.4 代码实践

**实践目标**：用测试验证四重匹配规则（无需网络）。

1. 阅读 [test/CookieHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/CookieHelperTest.cc) 中 `testDomainMatch`、`testPathMatch`、`testParse` 相关用例，抄下每一组输入输出。
2. 在纸上把每组 `domainMatch` 输入代入 4.4.2 的公式，确认手工结果与断言一致；特别关注「`baddomain.com` vs `domain.com`」「`/abc` vs `/a`」这类边界。
3. 进阶：仿照现有写法构造一条 `Set-Cookie: sid=1; Domain=other.com; Path=/`，先按源码预测 `cookie::parse` 返回 nullptr（域不匹配被拒收），再到测试里验证你的预测。
4. 预期结果：能不看源码说出任意 (Cookie, 请求) 组合是否匹配。此实践为纯阅读＋本地测试，**待本地验证**（`make check -C test` 或按 u1-l2 的构建步骤运行）。

#### 4.4.5 小练习与答案

**练习 1**：`secure` Cookie 为什么在 `match` 的第一个条件就被否决，而不是入库时就分开存？
**答案**：secure 属性约束的是**发送通道**而非存储；同一条 Cookie 在 https 请求命中、http 请求不命中，入库时无法预知后续请求协议，所以匹配时以 `secure` 入参动态判定（[Cookie.cc:L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L86)，入参来自 [HttpRequest.cc:L238](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L238) 的 `getProtocol() == "https"`）。

**练习 2**：`hostOnly` 什么时候为 true？
**答案**：两种情况：服务器 Set-Cookie 未写 Domain 属性（域即请求主机，只许精确匹配，[cookie_helper.cc:L364-L368](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/cookie_helper.cc#L364-L368)）；或从 Netscape 文件加载时第二列为 FALSE / 域是 IP（见 4.5.3 的 NsCookieParser）。

**练习 3**：`Set-Cookie: a=1`（无任何属性）会得到什么 Cookie？
**答案**：hostOnly=true、domain=请求主机、path=defaultPath（由请求目录推导）、expiry=最大值、persistent=false 的会话 Cookie。

### 4.5 CookieStorage：反向域名树、LRU 淘汰与文件 I/O

#### 4.5.1 概念说明

`CookieStorage` 是进程内唯一的 Cookie 仓库（由 `DownloadEngine` 持有），职责：

1. **存储**：把 Cookie 按域组织进一棵「反向域名标签树」——根节点挂 TLD 标签（`com`、`org`…），逐级向下是 `example`、`www`，即 `www.example.com` 的 Cookie 存在路径 `root → com → example → www` 的节点上。
2. **检索**：`criteriaFind` 沿请求主机的标签链下行，逐节点收集命中 Cookie，最后按 RFC 6265 §5.4 排序（路径深者先、同深创建早者先）。
3. **容量治理**：每域最多 50 条（超限先清过期、再踢最久未访问）；跟踪过 Cookie 的域节点超过 2000 个时整体 LRU 淘汰 10%。
4. **持久化**：`load` 自动嗅探 SQLite（Firefox3/Chromium，需编译期 sqlite3 支持）或 Netscape 文本格式；`saveNsFormat` 写临时文件再原子 rename。

#### 4.5.2 核心流程

```text
启动（MultiUrlRequestInfo::prepare）
  --load-cookies=path ⇒ CookieStorage::load(path, now)
      读文件头 16 字节，"SQLite format 3\0"？
        是 → Sqlite3MozCookieParser（失败再试 Chromium 版）   [需 HAVE_SQLITE3]
        否 → NsCookieParser::parse（Netscape 文本，逐行七列）
      每条 Cookie → store() → 按域标签倒序建树 → DomainNode::addCookie

下载中
  响应到达 ⇒ HttpResponse::retrieveCookie → cookie::parse + parseAndStore
  发请求前 ⇒ HttpRequest::createRequest → criteriaFind → Cookie: 头

退出（MultiUrlRequestInfo::getResult）
  --save-cookies=path ⇒ saveNsFormat：写 path+"__temp" → renameTo(path)
```

域名树与查找的关系：请求 `www.example.com` 时从根出发依次找 `com → example → www`，每经过一个**存有 Cookie 的**节点就尝试 `findCookie`。由于 `example.com`（域 Cookie）与 `www.example.com`（hostOnly Cookie）是树上不同节点，一次下行即可同时命中父域 Cookie 与本域 Cookie——这正是反向标签树的设计动机。

#### 4.5.3 源码精读

**Netscape 七列的逐行解析**：

[NsCookieParser.cc:L56-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NsCookieParser.cc#L56-L107)
`parseNsCookie` 按 Tab 切列后：不足 6 列拒收（L63-65）；域名前导 `.` 剥掉（L66）；`goodPath` 要求路径以 `/` 开头；过期时间按 double 解析再截断（兼容 Chromium 导出的亚秒精度，L73-80）；**第 0 列 expiry 为 0 视为会话 Cookie**——`persistent=false` 且 expiry 置最大值（L95-98）；`hostOnly` 取决于第二列是否 `TRUE` 或域是否 IP（L100-101）。外层 [NsCookieParser.cc:L109-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NsCookieParser.cc#L109-L139) 逐行读取，`#` 开头整行跳过（L130-132）——注意这会把浏览器导出的 `#HttpOnly_` 前缀行也当作注释丢弃，这是与 curl/wget 行为不同的一个细节。

**反向标签树与数值主机特判**：

[CookieStorage.cc:L233-L246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L233-L246)
`splitDomainLabel` 按点切分域名；数值主机（IP）不切分、整体作为一个标签（挂在根下）。树的形态定义见 [CookieStorage.h:L207-L223](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.h#L207-L223) 的注释：根无 Cookie，`lruTracker_` 只跟踪「有 Cookie 或曾有 Cookie」的节点。

**store：建树入库**：

[CookieStorage.cc:L274-L296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L274-L296)
先做全局域数检查（≥2000 触发 LRU 淘汰，常量在 [L226-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L226-L231)：`DOMAIN_EVICTION_TRIGGER=2000`、`DOMAIN_EVICTION_RATE=0.1`），然后按标签倒序逐级 `findNext`/`addNext` 下钻到目标节点，`addCookie` 成功后刷新该节点的 LRU 时间。

**DomainNode::addCookie：每域容量治理**：

[CookieStorage.cc:L83-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L83-L139)
逻辑分四路：节点首次有 Cookie 且新 Cookie 已过期 → 拒收；同键（domain+path+name，`operator==` 见 [Cookie.cc:L98-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L98-L102)）已存在且新值已过期 → **删除旧条目**（过期响应是删 Cookie 的手段）；同键存在且未过期 → 覆盖但**继承 creationTime**（RFC 6265 要求保留创建时间）；全新条目且已达 `MAX_COOKIE_PER_DOMAIN=50`（[CookieStorage.h:L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.h#L132)）→ 先清过期，仍满则替换 lastAccessTime 最小的一条。

**criteriaFind：下行收集 + RFC 排序**：

[CookieStorage.cc:L394-L424](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L394-L424)
沿请求主机标签链逐级下行，每个节点先刷新 lastAccess/LRU 再 `findCookie`（[L68-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L68-L81) 内逐条 `Cookie::match`）。收集完用 `CookiePathDivider`（L319-337，数路径深度）＋ `OrderByPathDepthDesc`（L354-375）排序，比较器注释直接引用 RFC 6265 §5.4：路径深者在前，同深创建早者在前。

**load 与 saveNsFormat**：

[CookieStorage.cc:L435-L481](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L435-L481)
`load` 打开文件读 16 字节头，与 `"SQLite format 3\0"` 逐字节比较决定走 sqlite3 解析器（Firefox3 失败自动降级 Chromium）还是 `NsCookieParser`；未编译 sqlite3 却遇到 SQLite 文件则抛异常（L464-468），最终被 catch 记 ERROR 返回 false。
[CookieStorage.cc:L483-L512](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L483-L512)
`saveNsFormat` 遍历 `lruTracker_` 中每个节点，`Cookie::toNsCookieFormat`（[Cookie.cc:L114-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L114-L146)，注意它**只写 LRU 跟踪中的节点**，且非 hostOnly 域会补回前导 `.`）写出，先写 `__temp` 再 `renameTo` 原子替换。

**三个挂接点**：

- 加载：[MultiUrlRequestInfo.cc:L217-L229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L217-L229)（`--load-cookies`）；
- 保存：[MultiUrlRequestInfo.cc:L305-L307](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L305-L307)（`--save-cookies`，注意在 `getResult()` 里、`showDownloadResults` 之前执行）；
- 注入：[HttpRequestCommand.cc:L97-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L97-L98) 把引擎的 CookieStorage 裸指针交给每个 `HttpRequest`。

> 关于大纲中的表述更正：任务描述里提到的 `CookieStorage::parseCookie` 在源码中**并不存在**——真实的入库存径是 `load`（文件）与 `parseAndStore`（Set-Cookie 字符串），请求侧匹配是 `criteriaFind`。下面的综合实践按真实函数展开。

#### 4.5.4 代码实践

**实践目标**：验证「--load-cookies 装载 → criteriaFind 命中 → Cookie 头发出」全链路。

1. 准备 `cookies.txt`（示例数据，注意用真实 Tab 分隔；时间戳需大于当前时间）：
   ```
   # Netscape HTTP Cookie File
   .example.com	TRUE	/	FALSE	1798761600	token	deadbeef
   ```
2. 加日志（这是本实践的动手部分）：在以下三处各加一行 `A2_LOG_INFO(fmt(...))` 后重新编译（构建步骤见 u1-l2）：
   - [CookieStorage.cc:L471](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L471) 附近：打印 `NsCookieParser` 解析出的条数；
   - [CookieStorage.cc:L414](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L414) 附近（`criteriaFind` 下行循环内）：打印每个经过的节点标签与该节点命中数；
   - [HttpRequest.cc:L243-L244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L243-L244) 附近：打印最终拼出的 `Cookie:` 头值。
3. 运行（`-v` 关掉分段以简化日志）：
   `./src/aria2c --load-cookies=cookies.txt -l=cookie.log --log-level=info -s1 -x1 -v http://example.com/ -o /dev/null`
4. 需要观察的现象：
   - 日志先出现「Loaded cookies from ...」与解析条数 1；
   - `criteriaFind` 依次经过 `com → example` 节点（`example.com` 的域 Cookie 在 `example` 节点命中）；
   - `Cookie: token=deadbeef;` 头随请求发出。
5. 变体实验：把 cookies.txt 的域改成 `notexample.com` 再跑一次，预期 `criteriaFind` 找不到对应节点、请求头里没有 Cookie——这就是域匹配在树上的体现。
6. 预期结果：三处日志串成一条证据链。具体输出依赖站点响应，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `criteriaFind` 从根下行时要「每经过一个节点都尝试收集」，而不是只查请求主机的完整链尾节点？
**答案**：域 Cookie（如 `.example.com`）登记在 `example` 节点，而请求 `www.example.com` 的链尾是 `www` 节点；只有沿途逐节点匹配，父域的域 Cookie 才能被子域请求带上，这正是 RFC 域匹配语义在树上的投影。

**练习 2**：`--save-cookies` 保存的文件里，会话 Cookie 会长什么样？
**答案**：会被写出（`saveNsFormat` 不筛 persistent），但 `toNsCookieFormat` 在非持久时把过期列写 0（[Cookie.cc:L136-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Cookie.cc#L136-L141)）；下次 `--load-cookies` 读回时 expiry=0 又按会话 Cookie 处理，语义闭环。

**练习 3**：某域写入了第 51 条 Cookie，`DomainNode::addCookie` 会依次做什么？
**答案**：先删除该域内所有已过期 Cookie；若仍满 50 条，则用新 Cookie **原地替换** lastAccessTime 最小的那条（[CookieStorage.cc:L106-L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L106-L123)）；否则直接追加。

## 5. 综合实践

**任务：把五个模块串成一次「带认证与 Cookie 的完整下载」。**

场景设定（示例）：假设 `http://test.local/file` 需要 Basic 认证，且服务器只对携带特定 Cookie 的请求放行。

1. **认证链**：写 `~/.netrc`（600 权限）含 `machine test.local login alice password secret`，**不使用** `--http-user`，运行下载并开 debug 日志。对照 4.2 的判定树确认：HTTP 分支 → 无 userinfo → 无 --http-user → netrc machine 条目（default 被 ignoreDefault 屏蔽）→ Authorization 头出现。再分别用 `--http-user=bob`（应压过 netrc）与 `--no-netrc`（应彻底无认证、收到 401）重复，验证优先级。
2. **挑战链**：加 `--http-auth-challenge=true` 并故意给错密码，观察：首个请求无 Authorization → 401 → `activateBasicCred` 也解析出错误凭证重试一次 → 再次 401 → 抛 `HTTP_AUTH_FAILED` 退出。数一数总共发了几次请求，与 [HttpSkipResponseCommand.cc:L207-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L207-L215) 的 `authenticationUsed` 防死循环逻辑对账。
3. **Cookie 链**：构造 `cookies.txt`（按 4.5.4 步骤 1），用 `--load-cookies` 与 `--save-cookies=out.txt` 完成「装载 → 命中 → 请求 → 接收新 Set-Cookie → 保存」一轮；diff 输入输出两个文件，解释每一处差异（提示：域 Cookie 的前导 `.`、覆盖时的 creationTime 继承、会话 Cookie 的 0 过期列）。
4. **产物**：写一份一页纸的《aria2 认证与 Cookie 行为报告》，包含：优先级判定树（标注每次实验命中的分支）、三张日志摘录（加载/匹配/发出）、cookies.txt diff 及逐差异解释。

本实践覆盖 4.1-4.5 全部模块；无法访问真实受限站点时，可用本地 `python3` 起一个返回 401 与 Set-Cookie 的最小 HTTP 服务代替。**待本地验证**。

## 6. 本讲小结

- 认证分两层：`AuthConfig` 是确定的 (user, password)，`AuthResolver` 按 hostname 决定用哪组；`AuthConfigFactory` 按协议（http/https 与 ftp/sftp 两分支）编排「URI userinfo ＞ --http-user/--ftp-user ＞ netrc ＞ 默认值（仅 FTP 有匿名对 anonymous/ARIA2USER@）」的优先级。
- HTTP 侧 netrc 的 `default` 条目被 `ignoreDefault` 屏蔽，FTP 侧可用；netrc 是 token 级状态机解析、按书写顺序首个 match 生效，`.` 前缀机器名做子域后缀匹配。
- `--http-auth-challenge=true` 时认证改为被动应答：裸发 → 401 → `activateBasicCred` 按 (host, port, path) 最长前缀登记缓存并重试一次，`authenticationUsed` 防死循环。
- Cookie 判定是四重匹配：secure 通道、isExpired（会话 Cookie 恒不过期）、pathMatch 目录边界、domain/hostOnly 域匹配；Set-Cookie 解析严格遵循 RFC 6265，域不匹配整条拒收。
- `CookieStorage` 用反向域名标签树组织 Cookie，`criteriaFind` 沿树下行逐节点收集并按「路径深者先、创建早者先」排序；容量治理分两级——每域 50 条（先清过期再踢最旧）与全局 2000 域触发 LRU 淘汰 10%；`load` 嗅探 SQLite/Netscape 两种格式，`saveNsFormat` 临时文件＋rename 原子写出。

## 7. 下一步学习建议

- **u4-l4（代理与隧道）**：代理场景下认证还有第三条路——`getProxyAuthString`（[HttpRequest.cc:L221-L223](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L221-L223) 的 `Proxy-Authorization` 头），与本讲的 Basic 头生成同源，建议对照阅读。
- **u4-l5（FTP/SFTP 命令链）**：本讲只讲了 FTP 凭证「怎么决定」，那一讲讲凭证「怎么用」——`FtpInitiateConnectionCommand`/`FtpNegotiationCommand` 中 `createAuthConfig` 的四个调用点如何把 AuthConfig 送进 USER/PASS 命令。
- **u9-l2（ValueBase 与解析器骨架）**：本讲的 Netrc/NsCookieParser 都是手写状态机；`Set-Cookie` 之外更复杂的 bencode/JSON 解析共享一套 `StructParserStateMachine` 骨架，可对照体会两种解析器设计风格的取舍。
- 继续阅读建议：通读 [test/CookieStorageTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/CookieStorageTest.cc) 中 `testDomainIsFull`（源码注释指名参考它，见 [CookieStorage.cc:L227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CookieStorage.cc#L227)），把 LRU 域淘汰的边界行为用测试跑一遍。
