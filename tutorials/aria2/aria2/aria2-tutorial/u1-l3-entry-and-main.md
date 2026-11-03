# 进程入口：main.cc 与启动流程

## 1. 本讲目标

上一讲（u1-l2）我们走通了「configure → make → `./src/aria2c`」的构建链路。本讲从产出的这个可执行文件出发，回答一个更本质的问题：

> 在终端敲下 `aria2c https://example.com` 回车之后、第一个字节开始下载之前，这个进程到底做了哪些事？

学完本讲，你应该能够：

1. 说出 aria2 里存在**两个 `main` 函数**（全局 `::main` 与命名空间内的 `aria2::main`），以及为什么这样分层。
2. 跟踪 `Platform` 的构造与析构：它用 RAII 完成对 OpenSSL/GnuTLS/libssh2/c-ares 等第三方库的**一次性全局初始化与对称清理**。
3. 解释 `Context` 如何把 `argv` 解析结果一步步转化为 `reqinfo`（一个 `MultiUrlRequestInfo` 对象），包括选项解析、日志初始化、Socket 全局配置和下载任务列表的创建分支。
4. 画出完整的启动时序：`::main` → `Platform` 构造 → `Context` 构造 → `reqinfo->execute()` → `Platform` 析构。
5. 用 gdb 断点（或在 main 中加打印重编译）亲手验证这条时序。

本讲只讲到「进入下载主循环之前」。主循环 `DownloadEngine::run()` 本身是下一单元（u2-l1）的主角。

## 2. 前置知识

### 2.1 RAII（Resource Acquisition Is Initialization）

C++ 中管理资源的一种惯例：在**构造函数**里获取资源（初始化库、加锁、打开文件），在**析构函数**里释放。只要把对象声明为栈上的局部变量，无论函数正常返回还是抛异常，析构都会执行，资源不会泄漏。aria2 用一个栈上的 `Platform` 对象来管理所有第三方库的全局初始化，就是这个手法。

### 2.2 standalone 与库两种运行模式

aria2 既是命令行程序，也可以被编译成库（libaria2，见 u8-l5）嵌入别的程序。因此入口代码要同时服务两种模式：

- **standalone 模式**（`aria2c` 命令行）：选项解析失败时直接 `exit()`，`--version`/`--help` 也直接退出进程。
- **库模式**：不能替宿主进程退出，失败时只能抛异常让调用者处理。

你会看到 `standalone` 这个布尔参数贯穿 `option_processing` 和 `Context` 的构造函数，就是用来切换这两种行为的。

### 2.3 进程退出码与 error_code

`main` 函数的返回值就是进程退出码（shell 里用 `echo $?` 查看）。aria2 把所有可能的退出结果定义成一个枚举 [src/error_code.h:L44-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L44-L79)：`FINISHED = 0` 表示成功，其余如 `UNKNOWN_ERROR = 1`、`NAME_RESOLVE_ERROR = 19`、`OPTION_ERROR = 28` 等各自对应一类失败。本讲的启动链路中，这个枚举值是唯一的「返回值通道」。

### 2.4 NLS 与 getopt

- **NLS**（Native Language Support）：GNU 的国际化机制，`setlocale` + `bindtextdomain` 之后，`_("...")` 宏会自动翻译消息。
- **getopt_long**：C 标准的命令行解析函数族。`option_processing.cc` 顶部的 `extern char* optarg;`（[src/Context.cc:L82-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L82-L83) 处声明）就是为它准备的。选项系统的细节在 u1-l5 详讲，本讲只需知道 Context 第一步会调用它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/main.cc` | 进程真正的入口文件 | 两个 `main` 的分工、异常兜底 |
| `src/Platform.cc(.h)` | 第三方库全局初始化/清理 | `setUp()`/`tearDown()` 与 `initialized_` 防重入 |
| `src/Context.cc(.h)` | 把 argv 装配成可执行的下载会话 | 构造函数 160 余行的七个阶段 |
| `src/MultiUrlRequestInfo.cc(.h)` | 一次下载会话的编排者 | `execute()` = `prepare()` + `run()` + `getResult()` |
| `src/LogFactory.cc` | 全局日志器的配置工厂 | `setLogFile`/`reconfigure` 与启动横幅 |
| `src/option_processing.cc` | 选项与配置文件解析（u1-l5 详讲） | 在启动链路中的位置与提前退出的路径 |
| `src/console.cc` | stdout/stderr 的统一封装 | `initConsole(false)` |
| `src/error_code.h` | 退出码枚举 | 返回值通道 |

## 4. 核心概念与源码讲解

### 4.1 main 入口：两个 main 的分工

#### 4.1.1 概念说明

很多 C++ 项目只有 一个 `main`，aria2 却有两个，这是为了同时满足两个需求：

1. **库模式复用**：`aria2::main(int argc, char** argv)` 是纯逻辑版本——接收参数、返回退出码，不碰进程级的事（控制台初始化、Platform 生命周期）。libaria2 与测试代码都可以复用这套逻辑。
2. **进程级兜底**：全局 `::main` 只做三件事：初始化控制台、用栈对象 `Platform` 圈定库初始化的生命周期、捕获一切 `aria2::Exception` 把它转换成退出码。真正的下载逻辑一行不写。

这样分层后，「库初始化的错误处理」「进程退出码」「下载流程」三件事互不纠缠。

#### 4.1.2 核心流程

```text
::main(argc, argv)
  ├─ global::initConsole(false)        # 包装 stdout/stderr
  ├─ try:
  │    ├─ Platform platform            # 栈对象：构造=全局初始化，析构=清理
  │    └─ r = aria2::main(argc, argv)  # 真正的启动链路
  ├─ catch aria2::Exception:
  │    └─ 打印异常栈 → r = ex.getErrorCode()
  └─ return r                          # 退出码交还给操作系统
```

#### 4.1.3 源码精读

先看外层的全局 `main`：

- [src/main.cc:L85-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L85-L99)：全局入口。第一行 `aria2::global::initConsole(false)` 把 stdout/stderr 包装成 aria2 自己的输出对象（实现在 [src/console.cc:L53-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/console.cc#L53-L66)，非 Windows 下就是包着 `stdout`/`stderr` 的 `BufferedFile`）。随后声明栈对象 `Platform platform`，再调用 `aria2::main`。注意 `catch` 只捕获 `aria2::Exception`——aria2 内部所有异常都派生自它并自带错误码，其他异常（如 `std::bad_alloc`）会直接终止进程。

再看命名空间内的逻辑版 `main`：

- [src/main.cc:L54-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L54-L81)：主体只有三步——① 构造 `Context`（第 73 行，非 Windows 分支）；② 若 `context.reqinfo` 非空则调用 `context.reqinfo->execute()`（第 77-79 行）并取回退出码；③ 返回。`reqinfo` 为空的场景后面会讲（如 `--show-files` 打印完就返回）。
- [src/main.cc:L56-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L56-L71)：Windows 分支。`CommandLineToArgvW` 把宽字符命令行转成 UTF-8 的 `char**` 再喂给 `Context`，保证其余代码在所有平台看到的都是相同的 `argc/argv`。不了解 Windows 也可跳过。

#### 4.1.4 代码实践

1. **实践目标**：验证「`--version`/`--help` 在 `reqinfo` 创建之前就退出了进程」。
2. **操作步骤**：
   ```bash
   ./src/aria2c --version; echo "exit=$?"
   ./src/aria2c --help | head -5; echo "exit=${PIPESTATUS[0]}"
   ./src/aria2c; echo "exit=$?"        # 故意不给 URI
   ```
3. **需要观察的现象**：前两条命令打印版本/用法后正常结束；第三条打印错误与用法后以非零码结束。
4. **预期结果**：依据 [src/option_processing.cc:L195-L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L195-L217)，`--version`/`--help` 在选项解析阶段直接 `exit(FINISHED)`，此时 `MultiUrlRequestInfo` 根本没被创建，退出码为 0；不给 URI 时依据 [src/option_processing.cc:L314-L319](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L314-L319) 返回 `UNKNOWN_ERROR`，再经 [src/Context.cc:L159-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L159-L162) 的 `exit(rv)`，退出码应为 1。
5. 具体终端文本随版本略有差异，退出码数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Platform platform` 必须声明在 `try` 块内部，而不是 `try` 之前？

答案：若放在 `try` 之外而 `aria2::main` 抛异常，异常会被 `catch` 捕获后继续走 `return r`，此时 `platform` 的析构本来也能正常执行（栈展开会经过它）；但若异常发生在 `Platform` 自身构造中（例如 GnuTLS 初始化失败抛出 `DL_ABORT_EX`），放在 `try` 外会直接终止进程、跳过统一的错误打印。放在 `try` 内保证初始化失败也能被同一套 `catch` 转换成退出码并优雅退出。

**练习 2**：`aria2::main` 返回的 `error_code::Value` 与 `::main` 返回的 `int` 是什么关系？

答案：`error_code::Value` 是枚举（见 [src/error_code.h:L44-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L44-L79)），枚举值可隐式转换为 `int` 并作为进程退出码；`FINISHED=0` 恰好符合 POSIX「0 表示成功」的惯例。

### 4.2 Platform 生命周期：一次性全局初始化

#### 4.2.1 概念说明

aria2 依赖的几个第三方库要求「整个进程只初始化一次、退出前清理一次」：OpenSSL/GnuTLS（TLS）、libgcrypt（加密原语）、c-ares（异步 DNS）、libssh2（SFTP）、Winsock（Windows 网络栈）。`Platform` 类把这件琐碎的事收拢到一个对象里：

- 构造 → `setUp()`：逐个初始化（哪个库编译进来了才初始化哪个）。
- 析构 → `tearDown()`：严格按相反顺序清理。
- 静态布尔 `initialized_` 保证重复构造不会重复初始化（幂等）。

这就是上一讲「configure 探测 → `#ifdef` 条件编译」在运行期的直接体现：**你编译时选了哪些库，运行时就初始化哪些库**。

#### 4.2.2 核心流程

```text
Platform() → setUp():
  if already initialized → return false        # 幂等
  [HAVE_LIBGMP]      initGmp()
  [ENABLE_NLS]       setlocale + bindtextdomain（国际化）
  [HAVE_OPENSSL]     OpenSSL 3.x: 加载 legacy/default provider
                     旧版:  SSL_library_init 等
  [HAVE_LIBGCRYPT]   gcry_check_version + 关闭安全内存
  [HAVE_LIBGNUTLS]   gnutls_global_init + 注册日志回调
  [c-ares]           ares_library_init
  [HAVE_LIBSSH2]     libssh2_init
  [WINSOCK2]         WSAStartup
~Platform() → tearDown():
  清空 TLS 上下文 → 卸载各库 → 释放静态资源（OptionParser、prefs 表）
```

方括号里的宏都来自上一讲讲过的 `config.h`。

#### 4.2.3 源码精读

- [src/Platform.cc:L93-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L93-L102)：静态成员 `initialized_` 与内联的构造/析构——构造转调 `setUp()`，析构转调 `tearDown()`，RAII 的全部秘密就这两行。
- [src/Platform.cc:L104-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L104-L118)：`setUp()` 开头的防重入检查与国际化初始化。`initialized_` 置 true 后，后面每个库的初始化失败都直接抛 `DL_ABORT_EX`（会被 `::main` 捕获）。
- [src/Platform.cc:L120-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L120-L139)：OpenSSL 分支。OpenSSL 3.x 需要显式加载 provider——注释点明 `legacy` provider 是为了 RC4 算法（部分老 FTP/BT 站点加密用）。
- [src/Platform.cc:L147-L158](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L147-L158)：GnuTLS 分支，初始化后把 GnuTLS 的内部日志接到 aria2 的 DEBUG 日志（回调函数在 [src/Platform.cc:L80-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L80-L91)）。
- [src/Platform.cc:L160-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L160-L183)：c-ares、libssh2、Winsock 的初始化。
- [src/Platform.cc:L194-L237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L194-L237)：`tearDown()`。注意结尾 [L231-L236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L231-L236) 的注释：主动删除 `OptionParser` 与 prefs 表的静态资源，是为了让 valgrind 检查内存泄漏时不受「合法的静态分配」干扰——一个很工程化的小细节。

#### 4.2.4 代码实践

1. **实践目标**：把「编译期特性」与「运行期初始化」对应起来。
2. **操作步骤**：
   ```bash
   ./src/aria2c --version | grep -A2 Features    # 记下你这份二进制的特性集
   ```
   然后逐行核对 [src/Platform.cc:L110-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L110-L183) 中每个 `#ifdef` 块：Features 行里出现的 GnuTLS/OpenSSL、`GnuTLS`+c-ares 异步 DNS 等条目，分别对应哪个 `if` 块会真正执行。
3. **需要观察的现象**：Features 列表与源码中会执行的分支一一对应；没编进去的库，对应分支在预处理阶段就被裁掉了。
4. **预期结果**：例如 Features 含 `GnuTLS` 时，`gnutls_global_init()` 一定在你的进程启动时执行过；含 `libssh2` 时 `libssh2_init()` 执行过。这张映射表正是上一讲「configure → AC_DEFINE → 条件编译 → FeatureConfig 上报」链条的运行期终点。

#### 4.2.5 小练习与答案

**练习 1**：如果用户代码（或测试框架）构造了两个 `Platform` 对象，会发生什么？

答案：什么坏事都不会发生。第二个对象构造时 `setUp()` 在 [src/Platform.cc:L106-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L106-L109) 检查 `initialized_` 已为 true，直接返回 false，不重复初始化；但它析构时 `tearDown()` 仍会把 `initialized_` 置回 false 并执行清理——所以「栈上恰好一个 Platform」才是安全用法，这也是它只出现在 `::main` 里的原因。

**练习 2**：`tearDown()` 里为什么要先调用 `SocketCore::setClientTLSContext(nullptr)` 这类「清空 TLS 上下文」的语句，再去卸载 OpenSSL/GnuTLS？

答案：若顺序反过来，SocketCore 里还持有指向已卸载库内部数据的上下文，后续任何一次访问都是悬垂指针。先把引用清空，再卸载库，是经典的逆序清理。

**练习 3**：`Platform::setUp()` 里哪些初始化与「网络协议」无关？

答案：`HAVE_LIBGMP`（大数运算，用于 BT 的 DHT 节点 ID 计算）与 `ENABLE_NLS`（本地化文本）与网络无关；其余各项都直接服务于 TLS、DNS、SSH 或 Winsock。

### 4.3 Context 装配：从 argv 到 reqinfo

#### 4.3.1 概念说明

`Context` 是一个只有两个成员的轻量结构（声明见 [src/Context.h:L48-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.h#L48-L57)：构造参数 + `reqinfo`），但它的构造函数是整个启动流程中最长的一段。它扮演「总装车间」：把命令行参数、配置文件、运行环境（rlimit、网卡、IPv6）全部消化，最终产出一个 `MultiUrlRequestInfo`（存入 `reqinfo`）。

之所以把它做成构造函数而非普通函数，是为了库模式下调用者只需在栈上声明一个 `Context` 就完成全部装配，失败时以异常形式报告。

#### 4.3.2 核心流程

`Context` 构造函数（[src/Context.cc:L153-L316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L153-L316)）可拆成七个阶段：

```text
① 选项解析     option_processing()：argv + 配置文件 + 环境变量 → Option 对象；
              失败时 standalone 直接 exit；--version/--help 也从这里提前退出
② BT 静态身份 生成 peer id / peer agent（仅 ENABLE_BITTORRENT）
③ 日志初始化   LogFactory 配置（文件/级别/颜色/静默）+ 写启动横幅
④ 运行环境     rlimit(NOFILE) 调整；IPv6 禁用、DSCP、收包缓冲、网卡绑定等
              SocketCore 全局配置
⑤ 建任务列表   按 --torrent-file > --metalink-file > --input-file > 命令行 URI
              的优先级创建 RequestGroup 列表；--show-files 时打印文件清单后
              直接 return（reqinfo 保持为空）
⑥ 清理选项     删除只对命令行 URI 有效的一次性选项（PREF_OUT 等）
⑦ 组装结果     reqinfo = MultiUrlRequestInfo(requestGroups, op, uriListParser)
```

注意⑤的四级分支是互斥的：给了 `--torrent-file` 就忽略后面的来源，依此类推。

#### 4.3.3 源码精读

- [src/Context.cc:L153-L166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L153-L166)：阶段①。`option_processing`（主体在 [src/option_processing.cc:L174-L344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L174-L344)）先扫一遍参数处理 `--no-conf`/`--conf-path`/`--version`/`--help`，再读配置文件、用环境变量覆盖代理设置，最后把命令行选项与 URI 分别收进 `op` 与 `args`。返回非 `FINISHED` 时，standalone 模式直接 `exit(rv)`——这就是上一节实践中「不给 URI 退出码为 1」的出处。细节留给 u1-l5。
- [src/Context.cc:L167-L186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L167-L186)：阶段②③。先为 BT 生成进程级唯一的 peer id；随后连续调用 `LogFactory::setLogFile/setLogLevel/setConsoleLogLevel/setColorOutput`，`--quiet` 时再关掉控制台输出，`reconfigure()` 让配置生效，最后写出形如 `<<--- --- --- --->>` 的横幅与版本、编译器、操作系统、所用类库五条 INFO 日志——**这五条日志是本讲实践里判断「Context 已执行到阶段③」的路标**。
- [src/LogFactory.cc:L106-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L106-L118)：`setLogFile` 的三态处理：`-` 表示写 stdout、空串表示丢弃（内部记为 `DEV_NULL`，见 [src/LogFactory.cc:L46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L46) 与 [L53-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L53-L64) 的注释——不打开空设备是为了省下消息格式化的开销）。`reconfigure()`（[L82-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L82-L94)）负责关旧文件、按新配置重开。
- [src/Context.cc:L188-L227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L188-L227)：阶段④上半。依据 `--rlimit-nofile` 抬高进程可打开文件数上限（多连接下载很吃描述符），失败只告警不中止。
- [src/Context.cc:L221-L245](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L221-L245)：阶段④下半。`--disable-ipv6` 时把全局协议族锁定为 IPv4；设置 DSCP 与收包缓冲；`net::checkAddrconfig()` 探测本机地址配置；`--interface`/`--multiple-interface` 决定后续所有 socket 绑定哪块网卡。这些全是对 `SocketCore` 类静态状态的一次性设置，之后创建的每个连接都会继承。
- [src/Context.cc:L246-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L246-L289)：阶段⑤的四级分支。`--show-files` 命中时（如 [L250-L253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L250-L253)）打印种子/Metalink 文件清单后直接 `return`——此时 `reqinfo` 为空，于是 [src/main.cc:L77-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L77-L79) 的 `if (context.reqinfo)` 判空跳过执行，进程以默认的 `FINISHED` 退出。四个 `createRequestGroupFor*` 函数来自 `download_helper.cc`，是 u3-l3 的主角。`--deferred-input` 时只保存 `uriListParser`，把解析推迟到运行期。
- [src/Context.cc:L291-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L291-L302)：阶段⑥。注释解释了原因：`op` 之后会被当作「新建 RequestGroup 的模板」（例如 RPC 动态添加任务时），若不删掉 `--out` 这类只应作用于命令行 URI 的选项，会给将来的任务带来意外影响。
- [src/Context.cc:L304-L315](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L304-L315)：阶段⑦。既无任务、又没开 RPC、也没有延迟输入时只打印「没有可下载的文件」；否则创建 `MultiUrlRequestInfo` 存入 `reqinfo`，同时写一条 `Downloading N item(s)` 的 NOTICE 日志。

#### 4.3.4 代码实践

1. **实践目标**：用日志文件亲眼看到 Context 的阶段③与阶段⑦。
2. **操作步骤**：
   ```bash
   ./src/aria2c -l /tmp/aria2-startup.log https://example.com
   head -20 /tmp/aria2-startup.log
   grep -n "Downloading" /tmp/aria2-startup.log
   ```
3. **需要观察的现象**：日志开头应出现 `<<--- --- --- --->>` 横幅、`aria2 1.x.x`、编译器与平台、操作系统、所用类库等行；往下能找到 `Downloading 1 item(s)`。
4. **预期结果**：横幅五行来自 [src/Context.cc:L179-L186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L179-L186)，`Downloading 1 item(s)` 来自 [src/Context.cc:L309-L312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L309-L312)。文件日志默认级别为 DEBUG（[src/LogFactory.cc:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L49)），所以 INFO 级横幅必然可见。日志里各行的具体措辞「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`aria2c --show-files=xxx.torrent` 运行后为什么退出码是 0，且没有任何下载发生？

答案：`--show-files` 命中 [src/Context.cc:L250-L253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L250-L253)，打印文件清单后构造函数直接 return，`reqinfo` 保持空指针；[src/main.cc:L76-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L76-L80) 中 `exitStatus` 初值就是 `FINISHED(0)`，`if` 判空跳过执行。

**练习 2**：阶段⑥删除 `PREF_OUT` 等选项的注释说「op 会作为新 RequestGroup 的模板」。请举一个不删会出问题的场景。

答案：假设命令行用 `--out=a.txt` 下载后进程又开着 RPC（`--enable-rpc`），随后客户端调用 `aria2.addUri` 添加新任务。若 `op` 里残留 `PREF_OUT=a.txt`，新任务也会被命名为 `a.txt`，甚至与旧任务冲突。删除动作保证「一次性选项」只影响本次命令行。

**练习 3**：`--deferred-input` 为什么不能与 `--save-session` 同时生效？（提示：[src/option_processing.cc:L338-L342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L338-L342)）

答案：延迟输入意味着启动时不读 input 文件、任务在运行期按需加入；而 `--save-session` 在退出时序列化「当前已知任务」。两者组合会在首次启动时保存一个不含 input 文件任务的空会话，覆盖掉源文件信息，因此源码检测到组合后直接移除 `--deferred-input` 并记一条警告。

### 4.4 MultiUrlRequestInfo：进入主循环前的最后一站

#### 4.4.1 概念说明

`Context` 产出 `reqinfo` 后，`aria2::main` 只再做一件事：`reqinfo->execute()`。`MultiUrlRequestInfo`（名字直译「多 URL 请求信息」）因此是**一次下载会话的总编排者**：它拿着任务列表（`requestGroups_`）和全套选项（`option_`），负责「准备好引擎 → 跑主循环 → 收尾结算」三段，并把最终退出码交还调用者。

它的 `execute()` 只有 18 行（[src/MultiUrlRequestInfo.cc:L348-L366](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L348-L366)），却固定了整个会话的骨架——这是阅读大型代码时最值得先找的那类「骨架函数」。

#### 4.4.2 核心流程

```text
execute():
  prepare() ≠ 0        → return UNKNOWN_ERROR     # 准备失败
  e_->run()            # 下载主循环（DownloadEngine，u2-l1 详讲）
  getResult()          # 保存 cookie/server-stat/会话；打印结果汇总；
                       # 计算退出码（全部完成=FINISHED，否则取最后错误码）
  resetSignalHandlers()

prepare() 内部（按顺序）:
  ① Notifier 单例                     # 全局事件通知器
  ② RPC over TLS 时创建服务端 TLS 上下文
  ③ DownloadEngineFactory().newDownloadEngine(option, requestGroups)
     → 产出 DownloadEngine e_          # 任务从此移交给引擎
  ④ WebSocket 会话管理器（--enable-rpc 时）
  ⑤ 加载 --load-cookies；解析 ~/.netrc；装配 AuthConfigFactory
  ⑥ 创建客户端 TLS 上下文（证书/CA/校验开关）
  ⑦ 加载 server-stat；设置 StatCalc（进度条）；注册信号处理器
```

信号处理值得单独一提（Ctrl-C 的优雅退出）：

```text
handler(SIGINT):                     # 第一次 Ctrl-C
  globalHaltRequested: 0 → 1         # 请求引擎优雅停止
handler(SIGINT):                     # 引擎已收尾(=2)后再按
  2 → 3                              # 强制退出
DownloadEngine 收尾完毕: 2 → 4/5      # src/DownloadEngine.cc:L250,L259,L149
```

即：**第一次 Ctrl-C 优雅停止（保存进度），收尾期间再按一次立即终止**。

#### 4.4.3 源码精读

- [src/MultiUrlRequestInfo.cc:L348-L366](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L348-L366)：`execute()` 骨架。`prepare()`、`e_->run()`、`getResult()` 三段一目了然；`run()` 外面套的 try-catch 只是把意外异常记入日志（源码注释自己都在怀疑是否必要）。
- [src/MultiUrlRequestInfo.cc:L177-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L177-L207)：`prepare()` 开头：重置 `globalHaltRequested`、创建 Notifier 单例；若 `--enable-rpc --rpc-secure` 则先造服务端 TLS 上下文——注释强调必须在创建 DownloadEngine **之前**装进 `SocketCore`；随后 [L206-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L206-L207) 用工厂创建引擎，注释点明「RequestGroups 从此移交（transfer）给 DownloadEngine」——这就是 `requestGroups_` 之后为空的原因。
- [src/MultiUrlRequestInfo.cc:L217-L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L217-L250)：加载 cookies.txt、解析 netrc（权限过松会告警并跳过）、装配 `AuthConfigFactory`——u4-l3 的伏笔。
- [src/MultiUrlRequestInfo.cc:L252-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L252-L289)：创建客户端 TLS 上下文（证书、CA、`--check-certificate`）、加载 server-stat、按 `--quiet` 选择 `NullStatCalc` 或 `ConsoleStatCalc`（[L133-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L133-L144)）——控制台进度条就是它画的。
- [src/MultiUrlRequestInfo.cc:L94-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L94-L128)：信号处理函数 `handler`。结合 [src/DownloadEngine.cc:L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L89)（变量定义）与 [L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L250)（收尾后置 2）即可还原上面的状态机。注册动作在 [src/MultiUrlRequestInfo.cc:L368-L397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L368-L397)，`SIGPIPE`/`SIGCHLD` 被直接忽略（避免下载中写断管退出、避免僵尸子进程）。
- [src/MultiUrlRequestInfo.cc:L302-L346](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L302-L346)：`getResult()`。按选项保存 cookies 与 server-stat；非 `--quiet` 时打印每个任务的下载结果；[L320-L330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L320-L330) 计算退出码：全部完成得 `FINISHED`，还有任务在进行则得 `IN_PROGRESS(7)`，否则取最后一个失败码；最后按 `--save-session` 序列化会话文件（RPC/队列化部署的关键能力）。
- [src/MultiUrlRequestInfo.h:L84-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.h#L84-L100)：头文件里对 `execute/prepare/getResult` 的注释，是理解三段式设计意图的最佳入口。

#### 4.4.4 代码实践

1. **实践目标**：体验 Ctrl-C 的优雅退出路径，并把它映射到源码。
2. **操作步骤**：
   ```bash
   ./src/aria2c -l /tmp/aria2-ctrlc.log \
       https://speed.hetzner.de/100MB.bin        # 任一足够大的文件
   # 下载中途按一次 Ctrl-C，等它退出后：
   echo $?
   grep -in "resume\|exception\|error" /tmp/aria2-ctrlc.log | tail
   ```
3. **需要观察的现象**：第一次 Ctrl-C 后 aria2 打印类似 `aria2 will resume download if the transfer is restarted.` 的提示并保存 `.aria2` 控制文件后退出。
4. **预期结果**：提示语来自 [src/MultiUrlRequestInfo.cc:L166-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L166-L175) 的 `printMessageForContinue()`，它在 `getResult()` 中因「未全部完成」被调用（[L320-L322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L320-L322)）；此时退出码应为 `IN_PROGRESS(7)` 或错误码而非 0。再次运行同一命令应从断点续传。具体提示文本与退出码「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `prepare()` 里创建 `DownloadEngine` 的语句旁边注释说「RequestGroups will be transferred to DownloadEngine」？

答案：`newDownloadEngine` 的第二参是 `std::move(requestGroups_)`（[src/MultiUrlRequestInfo.cc:L206-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L206-L207)），移动语义把任务列表的所有权整个交给引擎，由引擎的 `RequestGroupMan` 接管调度。此后 `MultiUrlRequestInfo` 只负责收尾，不再触碰单个任务。

**练习 2**：`SIGPIPE` 为什么被设为忽略（`SIG_IGN`）？

答案：对端提前关闭连接时，继续向该 socket 写会触发 `SIGPIPE`，默认行为是杀死进程。下载器频繁面对半关闭的连接（对端超时、服务器掐断），忽略该信号后写失败只会以错误码返回，由命令链正常走重试逻辑。

**练习 3**：`--quiet` 会影响本模块中的哪两处行为？

答案：其一，`prepare()` 里 [L133-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L133-L144) 改用 `NullStatCalc`，控制台不再画进度条；其二，`getResult()` 里 [L313-L318](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L313-L318) 跳过结果汇总打印（`printMessageForContinue` 亦然，见 [L166-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L166-L175)）。注意 Context 构造时 `--quiet` 还顺带关闭了控制台日志输出（[src/Context.cc:L175-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L175-L177)）。

## 5. 综合实践：亲手记录一次完整的启动时序

这是本讲规格中指定的实践任务：验证 `Platform` 构造、`Context` 构造、`reqinfo->execute()` 三步的先后顺序。推荐用 gdb（无需改代码重编译）；构建机没有 gdb 时可改用方案 B。

**方案 A：gdb 断点（推荐）**

1. 实践目标：用断点顺序证明本讲的启动时序图。
2. 操作步骤：
   ```bash
   gdb --args ./src/aria2c https://example.com
   (gdb) break aria2::Platform::setUp
   (gdb) break aria2::Context::Context
   (gdb) break aria2::MultiUrlRequestInfo::execute
   (gdb) run
   #   每次命中后用 continue 走到下一个断点
   (gdb) continue
   ```
   说明：断在 `setUp()` 而不是 `Platform::Platform()`，因为后者是内联构造（[src/Platform.cc:L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Platform.cc#L100)），可能被优化掉；`setUp` 是离线函数，必有符号。在 `Context::Context` 断点处可用 `finish` 观察构造耗时与返回，再 `continue` 进入 `execute`。
3. 需要观察的现象：断点命中顺序严格为 `Platform::setUp` → `Context::Context` → `MultiUrlRequestInfo::execute`。
4. 预期结果：与 [src/main.cc:L88-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L88-L92) 的语句顺序完全一致；在 `execute` 内部单步还可看到它先调 `prepare()`（[src/MultiUrlRequestInfo.cc:L350](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L350)），再进入 `e_->run()`。记录每个断点时的调用栈（`bt`），即是一份现成的启动时序笔记。

**方案 B：加打印后重编译（可选）**

在自己 fork 的构建目录中，于 [src/main.cc:L90-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L90-L91) 的 `Platform platform;` 前后、`aria2::main` 调用前后各加一行 `fprintf(stderr, ...)`（示例代码，非项目原有），重新 `make -j$(nproc)` 后运行同样的下载命令对照输出顺序。注意本讲义的考核环境不修改源码，此方案请仅在自己的副本中练习。

**写进笔记的内容建议**：① 三步时序 + 每步调用栈首帧；② `Context` 七阶段各自产出的证据（横幅日志、`Downloading N item(s)`）；③ 退出码（成功下载后应为 0，对应 `getResult()` 返回 `FINISHED`）；④ Ctrl-C 一次的退出码与日志证据。

## 6. 本讲小结

- aria2 有**两个 main**：全局 `::main` 负责进程级事务（控制台、Platform RAII、异常→退出码），`aria2::main` 负责「Context 构造 → reqinfo->execute()」的纯逻辑链路，供命令行与库模式共用。
- `Platform` 用 RAII + 静态 `initialized_` 幂等地完成 OpenSSL/GnuTLS/libgcrypt/c-ares/libssh2/Winsock 的全局初始化与逆序清理；哪些分支执行完全由编译期 `#ifdef` 决定。
- `Context` 构造函数是启动总装车间，分七阶段：选项解析 → BT peer id → 日志初始化 → rlimit/Socket 全局配置 → 按 torrent/metalink/input-file/URI 四级分支创建 RequestGroup → 清理一次性选项 → 产出 `reqinfo`。
- `--version`、`--help`、`--show-files`、参数错误都走**提前退出**路径，`reqinfo` 为空或进程直接 exit，主循环根本不会启动。
- `MultiUrlRequestInfo::execute()` 固定了会话骨架 `prepare() → e_->run() → getResult()`；`prepare()` 创建 DownloadEngine 并移交任务、装配 cookie/netrc/TLS/信号处理，`getResult()` 负责保存与结算退出码。
- Ctrl-C 的优雅退出由 `globalHaltRequested` 状态机实现：第一次置 1 请求优雅停止，引擎收尾后置 2，期间再按置 3 强制退出。

## 7. 下一步学习建议

本讲停在 `e_->run()` 的大门口。下一讲 **u2-l1（DownloadEngine：事件驱动主循环）** 将进入这扇门：`run()` 如何循环取出到期命令、处理 socket 读写事件、执行周期任务。建议先自读 [src/MultiUrlRequestInfo.cc:L177-L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L177-L300) 中 `prepare()` 装配的每个对象（CookieStorage、AuthConfigFactory、StatCalc），它们都是主循环的常客；再预习 `src/DownloadEngine.cc` 的 `run()` 与 `waitData()`。若你对命令行选项如何变成 `Option` 对象感兴趣，可以并行阅读 **u1-l5（命令行选项系统）**，它把本讲一笔带过的 `option_processing` 展开细讲。
