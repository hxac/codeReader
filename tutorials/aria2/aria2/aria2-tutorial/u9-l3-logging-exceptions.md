# 日志与异常体系：诊断 aria2 的行为

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 aria2 的两级日志体系：`LogFactory` 如何配置全局唯一的 `Logger`，文件日志与控制台日志如何各自独立地按级别过滤。
2. 读懂 `Exception` 家族的「三明治」结构：抛出点文件行号、系统错误码（errNum）、应用错误码（error_code）与 cause 链如何在 `stackTrace()` 中逐层展开。
3. 沿着「抛出点 → AbstractCommand 的 catch → RequestGroup 记账 → 退出码」的完整链路，从一份失败下载的日志反推出问题源头。
4. 熟练使用 `--log`、`--log-level`、`--console-log-level` 组合出自己需要的观测粒度。

本讲属于「工程实践」单元，不再讲解新的下载机制，而是给你一副**透视眼镜**：前面所有讲义里出现的命令链、分段下载、BT 交互，一旦行为不符合预期，都可以用本讲的方法定位到具体源码位置。

## 2. 前置知识

- **日志级别**：日志系统给每条消息标注重要程度。aria2 定义五级，从低到高为 `DEBUG < INFO < NOTICE < WARN < ERROR`。每路输出（文件、控制台）各设一个最低级别，低于它的消息直接丢弃。
- **`__FILE__` 与 `__LINE__`**：C/C++ 预处理器宏，在编译时展开为当前源码文件名与行号。aria2 的日志宏和异常宏都用它自动记录「这句话/这个异常诞生于哪一行」，这是日志能与源码一一对应的根基。
- **errNum 与应用错误码的分工**：`errNum` 是操作系统层错误（Linux 上即 `errno`，如 `ECONNREFUSED`），`error_code::Value` 是 aria2 自己定义的应用层退出码（如 `NAME_RESOLVE_ERROR = 19`）。一个描述「系统说了什么」，一个描述「aria2 认为这是什么问题」。
- **异常链（cause chain）**：底层异常被上层捕获后，常被包装成新的异常再次抛出。aria2 用 `cause_` 智能指针把原始异常挂在新异常上，形成链表，`stackTrace()` 沿链打印——效果类似 Java 的异常栈，但链上的每一环都是 aria2 显式包装出来的。
- **cuid**：命令唯一 ID（Command Unique ID），贯穿一次下载的整条命令链（见 u4-l1）。日志里几乎所有消息都以 `CUID#xxx` 开头，是把日志行与命令链对应起来的钥匙。
- **前置讲义**：本讲依赖 u1-l3（`main` → `Platform` → `Context` → `MultiUrlRequestInfo::execute` 的启动链路）与 u4-l1（HTTP 命令接力、三类异常出口）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/LogFactory.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.h) | 全局 Logger 单例工厂；定义 `A2_LOG_*` 日志宏 |
| [src/LogFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc) | 日志配置的静态存储与生效逻辑 |
| [src/Logger.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.h) / [src/Logger.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc) | 真正写日志的对象：级别过滤、行头格式化、双色输出 |
| [src/Exception.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h) / [src/Exception.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.cc) | 异常基类：file/line/errNum/errorCode/cause 五要素与 `stackTrace()` |
| [src/RecoverableException.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RecoverableException.h)、[src/DlAbortEx.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DlAbortEx.h)、[src/DlRetryEx.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DlRetryEx.h)、[src/DownloadFailureException.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadFailureException.h) | 下载场景的异常子家族 |
| [src/UnknownOptionException.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownOptionException.cc) | 一个「自带专属错误码」的异常子类范例 |
| [src/error_code.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h) | 应用层退出码枚举总表（同时也是进程退出码） |
| [src/message.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h) | 全部消息模板的集中登记处（`MSG_*` / `EX_*`） |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) | 三类下载异常的统一捕捞点与日志记录点 |
| [src/NameResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc)、[src/HttpSkipResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc) | 两个典型异常抛出点：DNS 失败与 HTTP 404 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc)、[src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc)、[src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc)、[src/main.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc) | 错误码从异常到进程退出码的接力路径 |

## 4. 核心概念与源码讲解

### 4.1 LogFactory 与 Logger：两级日志的配置与输出

#### 4.1.1 概念说明

aria2 是单线程事件循环程序（见 u2-l1），所以它的日志系统不需要复杂的异步队列，核心问题只有两个：

1. **日志写到哪**——文件与控制台是两路**相互独立**的输出，各自有独立的级别门槛；
2. **写日志的代码如何做到零成本**——事件循环里到处是 `A2_LOG_DEBUG`，如果没开 debug 级别，格式化消息的代价必须趋近于零。

`LogFactory` 是一个纯静态类（构造函数私有），扮演「全局配置仓库 + 单例发放点」；`Logger` 是真正持有文件句柄、执行过滤与格式化的对象。全进程只有一个 `Logger` 实例，任何代码都通过 `A2_LOG_*` 宏拿它。

#### 4.1.2 核心流程

日志的完整生命周期：

```text
业务代码:  A2_LOG_INFO(fmt(MSG_XXX, ...))
              │
              ▼
LogFactory.h 的 A2_LOG 宏
   ① LogFactory::getInstance() 取单例
   ② logger->levelEnabled(level)?  ── 否 ──► 直接结束（fmt 已求值，但不再写）
              │ 是
              ▼
Logger::log(level, __FILE__, __LINE__, msg)
              │
              ▼
Logger::writeLog(level, file, line, msg, trace)
   ③ fileLogEnabled(level)? ── 是 ──► 写文件：完整行头 + 消息 + 异常栈
   ④ consoleLogEnabled(level)? ── 是 ──► 写控制台：短行头 + 消息 + 异常栈
```

注意级别判定用 `level >= logLevel_` 的数值比较：五级枚举定义为 `1<<0, 1<<1, ...` 即 1、2、4、8、16，恰好随重要程度递增，所以直接比大小就等价于「不低于门槛」。

配置的生效时机在 `Context` 构造函数中（承接 u1-l3 的启动七阶段）：`--log`、`--log-level`、`--console-log-level`、`--quiet`、`--enable-color` 五个选项在此一次性灌入 `LogFactory`，随后 `reconfigure()` 重新打开日志文件，紧接着写下启动横幅与版本信息。

#### 4.1.3 源码精读

**（1）日志宏：先查门槛再写**

[LogFactory.h:L117-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.h#L117-L148) 定义了全部日志宏。`A2_LOG` 是通用骨架：块作用域内先取单例，`levelEnabled` 不过关就不调用 `log()`：

```cpp
#define A2_LOG(level, msg)                                                     \
  {                                                                            \
    const std::shared_ptr<aria2::Logger>& logger =                             \
        aria2::LogFactory::getInstance();                                      \
    if (logger->levelEnabled(level))                                           \
      logger->log(level, __FILE__, __LINE__, msg);                             \
  }
```

两点值得注意：

- `__FILE__`、`__LINE__` 由宏自动捕获，所以**日志行头里的文件行号就是调用 A2_LOG 的那一行**，可直接点开对照源码；
- `A2_LOG_EX` 变体多一个 `Exception&` 参数，会把异常的 `stackTrace()` 附在消息后面——这是 4.3 节诊断流程的关键载体。

`A2_LOG_DEBUG_ENABLED`（[LogFactory.h:L117-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.h#L117-L118)）单独存在，供「构造消息本身很贵」的场合先判断再拼字符串。

**（2）LogFactory 的静态配置与三特殊值文件名**

[LogFactory.cc:L46-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L46-L51) 是进程启动时的默认配置：

```cpp
std::string LogFactory::filename_ = DEV_NULL;      // 不写文件
std::shared_ptr<Logger> LogFactory::logger_;
bool LogFactory::consoleOutput_ = true;
Logger::LEVEL LogFactory::logLevel_ = Logger::A2_DEBUG;        // 文件门槛默认 DEBUG
Logger::LEVEL LogFactory::consoleLogLevel_ = Logger::A2_NOTICE; // 控制台门槛默认 NOTICE
```

[LogFactory.cc:L106-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L106-L118) 的 `setLogFile` 处理三个特殊值：`"-"` 映射为 `DEV_STDOUT`（日志写标准输出），空串映射为 `DEV_NULL`（关闭文件日志），其余按普通路径打开。`openLogger`（[LogFactory.cc:L53-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L53-L64)）里有个性能注释：当 `filename_` 是 `DEV_NULL` 时**根本不打开文件**，避免「格式化一堆消息再写进黑洞」的浪费。

**（3）选项默认值：`--log` 一开就是全量 debug**

文件级别默认值来自选项注册表：[OptionHandlerFactory.cc:L423-L425](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L423-L425) 中 `PREF_LOG_LEVEL` 默认 `V_DEBUG`；控制台级别默认 `V_NOTICE`（[OptionHandlerFactory.cc:L186-L192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L186-L192)）。组合起来的实际效果是：**平时控制台只显示 NOTICE 以上；一旦指定 `--log=文件`，无需再加 `--log-level=debug`，文件里就是全量 DEBUG 日志**。

**（4）配置灌入点与启动横幅**

[Context.cc:L171-L186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L171-L186)：五个选项写入 LogFactory，`--quiet` 关闭控制台输出，`reconfigure()` 重开文件后连续写下横幅、版本、编译器与依赖库信息、`MSG_LOGGING_STARTED`（"Logging started."，定义于 [message.h:L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L140)）。这份「启动指纹」能帮你确认日志文件确实被打开、以及当前二进制的特性集。

**（5）Logger 的双路输出与行头格式**

[Logger.cc:L89-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L89-L99) 是两路过滤判定：文件路要求 `level >= logLevel_ && fpp_`（句柄存在），控制台路要求 `consoleOutput_ && level >= consoleLogLevel_`。`levelEnabled` 是两者的或——任何一路要写，消息就会走 `log()`。

行头格式在两个模板函数里：

- 文件路 [Logger.cc:L122-L138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L122-L138)：`YYYY-MM-DD hh:mm:ss.microsec [LEVEL] [src/Foo.cc:123] `；
- 控制台路 [Logger.cc:L163-L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L163-L181)：`MM/DD hh:mm:ss [LEVEL] `，且 `colorOutput_` 开启时按级别着色（[Logger.cc:L142-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L142-L159)，DEBUG 白、INFO 青、NOTICE 绿、WARN 黄、ERROR 红）。

注意**控制台行头没有文件行号，文件行头才有**——这也是排障时优先看文件日志的原因之一。

[Logger.cc:L192-L208](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L192-L208) 的 `writeLog` 是最终汇聚点：两路各自「写行头 → 写消息 → 写异常栈 → flush」。文件每条都 flush（[Logger.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Logger.cc#L63-L64) 打开的是 `BufferedFile::APPEND` 追加模式），保证崩溃前的最后几行不丢。

**（6）附带的联动：GnuTLS 日志级别**

[LogFactory.cc:L66-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LogFactory.cc#L66-L80) 的 `adjustDependentLevels` 会在每次改级别时顺带调 `gnutls_global_set_log_level`：aria2 开 DEBUG 才让 GnuTLS 也输出调试日志。这体现了 LogFactory 作为「所有日志输出口径」的统一管理地位。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「文件全量、控制台精简」的双路差异。
2. **操作步骤**（需要已按 u1-l2 构建出 `src/aria2c`；以下命令均待本地验证）：
   ```bash
   # a) 只开文件日志
   ./src/aria2c --log=aria2.log https://example.com/ >/dev/null 2>&1; echo "exit=$?"
   # b) 日志直接打到终端（"-" 表示 stdout）
   ./src/aria2c --log=- --log-level=info https://example.com/ | head -40
   # c) 对比文件里各级别的行数
   grep -c ' \[DEBUG\] ' aria2.log; grep -c ' \[INFO\] ' aria2.log; grep -c ' \[NOTICE\] ' aria2.log
   ```
3. **需要观察的现象**：a) 的终端几乎安静（只有进度与结果摘要），而 `aria2.log` 里出现大量 DEBUG 行；b) 的输出行头是 `MM/DD hh:mm:ss [LEVEL]` 短格式且无文件行号；c) 三个计数呈 DEBUG 远大于 INFO、INFO 大于 NOTICE 的金字塔。
4. **预期结果**：日志文件第一屏应能看到 `Logging started.` 横幅（Context.cc:L186 写入）以及形如 `2024-.. .. [INFO] [src/Context.cc:186] Logging started.` 的长行头。
5. 以上运行现象**待本地验证**；若 `--log-level` 缺省时 DEBUG 计数为 0，请检查是否显式传了 `--log-level=notice` 之类的参数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `A2_LOG_DEBUG` 宏要先调用 `levelEnabled` 再调用 `log`，而不是把过滤完全交给 `Logger::log` 内部？

**答案**：宏的 `if` 只能省掉 `log()` 调用本身；调用方传入的 `msg` 参数（通常是 `fmt(...)` 的返回值）在进入函数前就已求值完成。`levelEnabled` 的真正意义在于给调用方一个更早的判断点（`A2_LOG_DEBUG_ENABLED`），让「构造消息本身很贵」的代码可以连 `fmt` 都跳过。而 `writeLog` 内部的 `fileLogEnabled/consoleLogEnabled` 双路过滤仍不可省，因为两路门槛不同。

**练习 2**：`--quiet` 和 `--console-log-level=error` 都能减少控制台输出，区别是什么？

**答案**：`--quiet` 走 [Context.cc:L175-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L175-L177) 调 `LogFactory::setConsoleOutput(false)`，直接关闭控制台日志这一路（`consoleOutput_` 为 false，任何级别都不写）；后者只是把控制台门槛提高到 ERROR，WARN 及以下被过滤，ERROR 仍会显示。文件日志两者都不影响。

### 4.2 Exception 家族与 error_code：错误的三张身份证

#### 4.2.1 概念说明

aria2 全部错误处理建立在一个自研异常体系上（而非直接使用 `std::runtime_error` 系）。基类 `Exception` 给每个异常配了**三张身份证**：

1. **出生地**：`file_` + `line_`，由抛出宏自动捕获；
2. **系统层错误**：`errNum_`，通常就是 `errno`，回答「操作系统报告了什么」；
3. **应用层错误**：`errorCode_`，即 `error_code::Value` 枚举值，回答「aria2 把这次失败归类为什么」，它最终会成为**进程退出码**与 RPC 的 `errorCode` 字段。

此外还有第四个要素 `cause_`：指向被包装的底层异常，形成因果链。`stackTrace()` 把整条链打印出来，就是你在日志里看到的「异常栈」。

在下载场景中，实际抛出的几乎总是 `Exception` 的孙辈——`RecoverableException` 的三个子类（承接 u4-l1 的三级出口）：

| 异常类 | 语义 | 典型后果 |
| --- | --- | --- |
| `DlRetryEx` | 本次尝试失败，值得重试 | `prepareForRetry` 重建 CreateRequestCommand |
| `DlAbortEx` | 该 URI 不可用，换下一个 | `tryReserved` 换 URI / 镜像 |
| `DownloadFailureException` | 整个任务无法继续 | 对 RequestGroup 置 halt，任务终止 |

而 `error_code.h` 的枚举是全项目共享的「错误分类字典」，同时就是退出码表——这是 aria2 一个非常实用的设计：**shell 里 `echo $?` 看到的数字，可以直接在 `error_code.h` 里查表**。

#### 4.2.2 核心流程

异常从抛出到携带错误码出厂的流程：

```text
抛出点:  throw DL_ABORT_EX2(MSG_RESOURCE_NOT_FOUND, error_code::RESOURCE_NOT_FOUND)
           │ 宏展开为 DlAbortEx(__FILE__, __LINE__, msg, code)
           ▼
Exception 构造:  file_/line_ = 抛出行, errorCode_ = 3, errNum_ = 0
           │
           ▼ （上层捕获后决定去向，见 4.3）
包装点:  DL_ABORT_EX2(fmt("URI=%s", uri), err)      ← 以原异常为 cause 再造一层
           │ 构造: errorCode_ 继承自 cause.errorCode_
           ▼
记录点:  A2_LOG_ERROR_EX(MSG_DOWNLOAD_ABORTED, 包装后的异常)
           │ Logger::log(..., const Exception&) → ex.stackTrace()
           ▼
日志输出:
  Exception: [src/AbstractCommand.cc:351] errorCode=3 URI=http://...
    -> [src/HttpSkipResponseCommand.cc:218] errorCode=3 Resource not found
```

`stackTrace()` 的格式约定（对应 [Exception.cc:L100-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.cc#L100-L121)）：

- 首行 `Exception: [文件:行] [errNum=N ]errorCode=码 消息`；
- 每个 cause 缩进两格，前缀 `->`，逐层列出直到链尾。

#### 4.2.3 源码精读

**（1）Exception 基类的五要素与克隆协议**

[Exception.h:L47-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h#L47-L91) 定义了基类。五个私有成员（[Exception.h:L48-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h#L48-L60)）：

```cpp
const char* file_;        // 抛出点文件（__FILE__）
int line_;                // 抛出点行号（__LINE__）
int errNum_;              // 系统错误码（Linux 下即 errno）
std::string msg_;         // 人类可读消息
error_code::Value errorCode_;            // 应用错误码（= 退出码）
std::shared_ptr<Exception> cause_;       // 被包装的底层异常
```

其中 [Exception.h:L58-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h#L58-L60) 有一行关键注释：`cause_` "is the root cause of this exception"——链上最后一环才是真正的祸根。

受保护的纯虚 `copy()`（[Exception.h:L62-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h#L62-L63)）是协变克隆协议：异常按值被 catch 后若要再包装进新异常，必须通过 `cause.copy()` 生成堆上副本，否则会发生对象切片（子类信息丢失）。

**（2）错误码继承规则**

六个构造重载里最值得注意的是 [Exception.h:L70-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.h#L70-L72) 声明、[Exception.cc:L61-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.cc#L61-L70) 实现的「msg + cause」版本：`errorCode_(cause.errorCode_)`——**包装层若不显式给新码，就继承底层异常的错误码**。这保证了一条异常链对外只呈现一个统一的错误分类，日志里两层都是 `errorCode=3` 正源于此。

**（3）stackTrace() 的逐层展开**

[Exception.cc:L100-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.cc#L100-L121)：先打印自身 `[file:line]`、可选的 `errNum=`、`errorCode=` 与 `what()`，然后 `while (e)` 沿 `cause_` 链逐层打印，每层前缀 `  -> `。`errNum_` 非零才输出，所以网络类异常（带 errno）与协议类异常（errNum 为 0）的栈行格式略有差异——排障时看到 `errNum=111` 就该想到 `ECONNREFUSED` 一类系统错误。

**（4）下载三兄弟与抛出宏**

[RecoverableException.h:L41-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RecoverableException.h#L41-L62) 继承 Exception 并 override `copy()`。`DlAbortEx`（[DlAbortEx.h:L41-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DlAbortEx.h#L41-L63)）与 `DlRetryEx`（[DlRetryEx.h:L41-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DlRetryEx.h#L41-L57)）各自提供一组便捷宏：

```cpp
#define DL_ABORT_EX(arg)  DlAbortEx(__FILE__, __LINE__, arg)
#define DL_ABORT_EX2(arg1, arg2) DlAbortEx(__FILE__, __LINE__, arg1, arg2)
#define DL_RETRY_EX2(arg1, arg2) DlRetryEx(__FILE__, __LINE__, arg1, arg2)
```

约定俗成的编号：`EX`（1 参）只有消息；`EX2`（2 参）第二个参数是 `error_code::Value` 或 cause 异常；`EX3` 三个参数。**手写 `DlAbortEx("...")` 而不用宏是常见错误**——那样 file/line 全丢，日志栈里将出现 `[?:0]` 一般无从定位的空信息（实际上不写参数则 file 为任意值，宏的存在就是强制携带出生地）。

`DownloadFailureException` 的类注释（[DownloadFailureException.h:L41-L44](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadFailureException.h#L41-L44)）明确了分工：抛它表示「整个 RequestGroup 应当中止」；`DlAbortEx` 只是「中止一个请求」。

**（5）自定义错误码子类范例：UnknownOptionException**

[UnknownOptionException.cc:L44-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownOptionException.cc#L44-L59) 展示了「专属错误码异常」的标准写法：

```cpp
UnknownOptionException::UnknownOptionException(const char* file, int line,
                                               const std::string& unknownOption)
    : RecoverableException(file, line,
                           fmt(MESSAGE, unknownOption.c_str()),  // "Unknown option '%s'"
                           error_code::OPTION_ERROR),
      unknownOption_(unknownOption)
```

消息模板 `MESSAGE` 在匿名命名空间里经 `_()` 国际化包装（`_()` 宏见 [common.h:L69-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/common.h#L69-L82)，启用 NLS 时映射 gettext，否则原样返回）。这个异常被 `option_processing` 捕获（[option_processing.cc:L293-L297](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L293-L297)），打印候选建议后 `return e.getErrorCode()`——即退出码 28。它还额外携带 `unknownOption_` 成员供 `showCandidates` 做模糊推荐，体现了「异常不仅传消息，也可传结构化上下文」。

**（6）退出码总表**

[error_code.h:L44-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L44-L79) 定义全部 33 个值。挑高频几项：

| 码 | 名称 | 含义 |
| --- | --- | --- |
| 0 | `FINISHED` | 全部任务成功 |
| 1 | `UNKNOWN_ERROR` | 未分类错误（异常未显式给码时的默认值） |
| 2 | `TIME_OUT` | 命令超时（对应 [AbstractCommand.cc:L340](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L340) 的 `EX_TIME_OUT`） |
| 3 | `RESOURCE_NOT_FOUND` | HTTP 404 / FTP 550 等资源不存在 |
| 7 | `IN_PROGRESS` | 有任务因退出信号中断但未失败（配合 `-c` 续传） |
| 19 | `NAME_RESOLVE_ERROR` | DNS 解析失败 |
| 23 | `HTTP_TOO_MANY_REDIRECTS` | 重定向超过 20 次上限 |
| 28 | `OPTION_ERROR` | 命令行/配置文件选项非法 |
| 32 | `CHECKSUM_ERROR` | 校验和不匹配 |

消息文本统一登记在 [message.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h)：`MSG_*` 偏运行轨迹（多数带 `CUID#%` 前缀），`EX_*` 偏异常消息（如 [message.h:L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L147) `MSG_RESOURCE_NOT_FOUND`、[L260](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L260) `EX_RESOLVE_HOSTNAME`、[L288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L288) `EX_EXCEPTION_CAUGHT`）。需要新消息时加在这里，而不是散落在业务代码里拼字符串。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发一次「带专属错误码的异常」，观察退出码 28 与错误消息的国际化和候选推荐。
2. **操作步骤**（待本地验证）：
   ```bash
   ./src/aria2c --maximum-concurrent-downloads-typo=3 http://example.com/ ; echo "exit=$?"
   ```
3. **需要观察的现象**：终端先打印 Usage 摘要，随后给出类似 `Maybe 'maximum-concurrent-downloads' is mistyped?` 的候选提示；`echo $?` 输出 28。
4. **预期结果**：对照源码，退出码来自 [option_processing.cc:L293-L297](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L293-L297) 的 `return e.getErrorCode()`，而 `e` 的 `OPTION_ERROR` 码在 [UnknownOptionException.cc:L46-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownOptionException.cc#L46-L47) 构造时写入；该返回值经 [Context.cc:L158-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L158-L162) 在 standalone 模式下直接 `exit(rv)`——**启动期异常不经过 DownloadEngine，退出码走的是这条捷径**。
5. 运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Exception` 为什么必须把 `copy()` 设为纯虚函数，而不是直接存 `std::shared_ptr<Exception> cause_` 就完事？

**答案**：catch 到的异常是按值捕获的静态类型（如 `Exception& e` 实际引用动态子类对象），若直接用 `e` 拷贝构造 cause，会发生对象切片，子类专属数据（如 `UnknownOptionException::unknownOption_`）丢失。`copy()` 由每个子类 override，返回 `make_shared<具体子类>(*this)` 的完整深拷贝，保证异常链上每一环都保留真实类型。

**练习 2**：一次下载最终以退出码 2（TIME_OUT）结束，但日志的异常栈里出现了两层 `errorCode=2`，为什么两层相同？

**答案**：外层是 `AbstractCommand` 捕获后用 `DL_ABORT_EX2/ DL_RETRY_EX2(fmt(...), err)` 包装的新异常，使用了「msg + cause」构造重载，按 [Exception.cc:L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Exception.cc#L67) 的 `errorCode_(cause.errorCode_)` 规则继承底层码。整条链共享同一个错误码是设计意图：对外分类唯一，定位则看链上各自的 file:line。

**练习 3**：`error_code::UNDEFINED = -1` 用在哪里？

**答案**：它是 `RequestGroup::lastErrorCode_` 的初始值（[RequestGroup.cc:L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L147)），表示「尚未发生任何错误」。任务收尾时若仍是 UNDEFINED 且非用户移除，则按 [RequestGroup.cc:L200-L205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L200-L205) 兜底为 `UNKNOWN_ERROR(1)` 或 `IN_PROGRESS(7)`。它从不作为退出码出现。

### 4.3 失败诊断流程：从日志一行反查抛出点

#### 4.3.1 概念说明

前两节攒齐了零件，本节把它们串成一条**端到端的错误传递管道**：

```text
异常抛出（协议命令链深处）
  → AbstractCommand::execute 的三段 catch（记录日志 + 记账 + 决定去向）
  → RequestGroup::lastErrorCode_（任务级记忆）
  → DownloadResult（任务收尾快照）
  → RequestGroupMan::getDownloadStat（全局统计，取最后一个失败码）
  → MultiUrlRequestInfo::getResult（进程退出码）
  → main 返回 → shell 的 $?
```

理解这条管道后，「日志里的一行 ERROR」就不再是一串孤立文字，而是管道某个横截面的投影：**行头告诉你谁记录的，异常栈告诉你谁抛的，errorCode 告诉你任务最终怎么收场，`$?` 告诉你进程怎么收场**。

#### 4.3.2 核心流程

以一次 HTTP 404 为例（默认选项 `--max-file-not-found=0`、`--max-tries=5`）：

1. `HttpSkipResponseCommand::processResponse` 判定 `statusCode == 404`，因 `--max-file-not-found` 为 0，抛 `DL_ABORT_EX2(MSG_RESOURCE_NOT_FOUND, RESOURCE_NOT_FOUND)`——DlAbortEx，判「该 URI 无药可救」。
2. 异常沿命令链冒泡到 `AbstractCommand::execute` 的 `catch (DlAbortEx&)`：
   - `setLastErrorCode(3, ...)` 写入 RequestGroup；
   - 用 `A2_LOG_ERROR_EX` 记 ERROR 日志，消息是 `MSG_DOWNLOAD_ABORTED`，异常对象是**新包装的一层**（cause 指向第 1 步的原始异常），于是日志里出现两层栈；
   - `addURIResult` 把该 URI 标记为失败结果；
   - `tryReserved()` 尝试下一个备用 URI，没有则任务走向终结。
3. 任务 numCommand 归零后被收割为 `DownloadResult`，`result` 字段取自 `downloadResult()` 映射——这里就是 `lastErrorCode_ = 3`。
4. `MultiUrlRequestInfo::getResult` 聚合所有 DownloadResult：有失败项时退出码取**最后一个**失败项的 result（3），全部完成则 `FINISHED(0)`；若只是被 Ctrl-C 打断且无错误，则 `IN_PROGRESS(7)`。
5. `main` 把该值原样 `return`，成为进程退出码。

若是 DNS 失败：异常可以出自两处——异步解析路径（c-ares）在 [AbstractCommand.cc:L782-L804](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L782-L804)，同步回退路径在 [NameResolver.cc:L50-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc#L50-L69)——两处都抛 `NAME_RESOLVE_ERROR(19)`，后续管道与 404 完全一致。

#### 4.3.3 源码精读

**（1）抛出点 A：HTTP 404**

[HttpSkipResponseCommand.cc:L204-L241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L204-L241) 是 HTTP 状态码 ≥400 的总裁决处，4xx/5xx 各有归宿：

```cpp
case 404:
  if (getOption()->getAsInt(PREF_MAX_FILE_NOT_FOUND) == 0) {
    throw DL_ABORT_EX2(MSG_RESOURCE_NOT_FOUND, error_code::RESOURCE_NOT_FOUND);
  }
  throw DL_RETRY_EX2(MSG_RESOURCE_NOT_FOUND, error_code::RESOURCE_NOT_FOUND);
```

同一个 404 有两种抛法：`--max-file-not-found=0`（默认，注册于 [OptionHandlerFactory.cc:L863-L864](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L863-L864)）时抛 DlAbortEx **立即弃用该 URI**；设为正数 N 时抛 DlRetryEx，计入「文件未找到」计数、达 N 次才弃用（语义是「镜像站可能不同步，多试几个再放弃」）。同一个错误码、两种异常类型、两种恢复策略——错误码描述「发生了什么」，异常类型描述「打算怎么办」，两轴正交。

**（2）抛出点 B：DNS 解析失败**

异步路径（承接 u2-l4 的 AsyncNameResolverMan）：[AbstractCommand.cc:L782-L804](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L782-L804)，解析器状态 `-1`（失败）抛：

```cpp
case -1:
  ...
  throw DL_ABORT_EX2(
      fmt(MSG_NAME_RESOLUTION_FAILED, getCuid(), hostname.c_str(),
          asyncNameResolverMan_->getLastError().c_str()),
      error_code::NAME_RESOLVE_ERROR);
```

同步路径（无 c-ares 或 `--async-dns=false`）：[NameResolver.cc:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc#L55-L61)，`getaddrinfo` 返回非零即抛 `EX_RESOLVE_HOSTNAME` + `gai_strerror(s)` 详情，错误码同样是 19。**两条路径殊途同归**，所以无论构建是否带 c-ares，诊断方法一致。

**（3）统一捕捞点：AbstractCommand::execute 的三段 catch**

[AbstractCommand.cc:L346-L412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L346-L412) 是全下载命令链共用的异常汇（模板方法骨架见 u2-l3）。`catch (DlAbortEx&)`（L346-L363）最能体现「日志 + 记账 + 去向」三合一：

```cpp
catch (DlAbortEx& err) {
  requestGroup_->setLastErrorCode(err.getErrorCode(), err.what());   // 记账
  if (req_) {
    A2_LOG_ERROR_EX(                                                  // 记日志
        fmt(MSG_DOWNLOAD_ABORTED, getCuid(), req_->getUri().c_str()),
        DL_ABORT_EX2(fmt("URI=%s", req_->getCurrentUri().c_str()), err)); // 包装成两层链
    fileEntry_->addURIResult(req_->getUri(), err.getErrorCode());     // URI 级记账
    ...
  }
  onAbort();
  tryReserved();                                                      // 换备用 URI
  return true;                                                        // 本命令销毁
}
```

注意 `A2_LOG_ERROR_EX` 的第二个参数不是 `err` 本身，而是**现场新建的包装异常**（cause = err）。这就是日志里两层异常栈的来源：外层 `[src/AbstractCommand.cc:351]` 标注「在这里记录」，内层 `-> [src/HttpSkipResponseCommand.cc:218]` 标注「祸根在这」。

`catch (DlRetryEx&)`（L364-L397）以 INFO 级记录 `MSG_RESTARTING_DOWNLOAD`，累加尝试计数并与 `--max-tries`（默认 5，[OptionHandlerFactory.cc:L873-L874](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L873-L874)）比较，超限才升级为 ERROR + `MSG_MAX_TRY` 并走 abort 流程。`catch (DownloadFailureException&)`（L398-L412）则 `setHaltRequested(true)` 终结整个任务组。

**（4）错误码的落袋与聚合**

`setLastErrorCode`（[RequestGroup.h:L447-L453](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L447-L453)）只记第一次……确切地说是「最后一次」写入的码与消息。任务收尾时 [RequestGroup.cc:L193-L208](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L193-L208) 完成最终映射：完成 → FINISHED；用户移除 → REMOVED(31)；被信号打断且无错误 → IN_PROGRESS(7)；从未出错却失败 → UNKNOWN_ERROR(1)；否则返回 `lastErrorCode_`。

[RequestGroupMan.cc:L621-L648](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L621-L648) 的 `getDownloadStat` 遍历全部 DownloadResult，`lastError` 取**遍历中最后一个非成功结果**的码。控制台的 "Download Results" 表（[RequestGroupMan.cc:L696-L751](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L696-L751)）里每行 `OK/ERR/INPR/RM` 状态字同样来自 `dr->result`。RPC 侧 `tellStatus` 的 `errorCode`/`errorMessage` 字段（[RpcMethodImpl.cc:L827-L831](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L827-L831)）也是同一来源。

**（5）退出码拍板与兜底 catch**

[MultiUrlRequestInfo.cc:L302-L346](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L302-L346) 的 `getResult`：`allCompleted()` 为真返回 FINISHED；否则按「有无 IN_PROGRESS」取 7 或 `s.getLastErrorResult()`。而 [MultiUrlRequestInfo.cc:L348-L366](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L348-L366) 的 `execute` 里还有一道兜底：`e_->run()` 本身抛出的 `RecoverableException`（正常情况下命令层已就地消化，极少冒泡到这里）被捕获并以 `EX_EXCEPTION_CAUGHT` 记 ERROR，然后仍走 `getResult` 结算。

最后一关在 [main.cc:L85-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/main.cc#L85-L99)：若异常一路冒泡到 `::main`（例如 Context 构造失败），直接向 stderr 打印 `EX_EXCEPTION_CAUGHT` + 完整 `stackTrace()`，并把 `ex.getErrorCode()` 作为返回值：

```cpp
catch (aria2::Exception& ex) {
  aria2::global::cerr()->printf("%s\n%s\n", EX_EXCEPTION_CAUGHT,
                                ex.stackTrace().c_str());
  r = ex.getErrorCode();
}
return r;
```

至此，`error_code` 的三重身份闭环：**异常内码 = 任务结果码 = 进程退出码**。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：分别触发 DNS 失败与 HTTP 404，从日志摘录异常栈，写出两次失败的退出码并定位抛出点源码行。
2. **操作步骤**（以下命令**待本地验证**；`.invalid` 是 RFC 2606 保留的永不解析域名，example.com 的 `/nonexistent-404` 返回 404）：
   ```bash
   # 实验一：DNS 失败
   ./src/aria2c --log=dns.log --log-level=info --max-tries=1 \
       "http://this-host-does-not-exist.invalid/file.iso"
   echo "exit=$?"                          # 记录退出码 A

   # 实验二：HTTP 404
   ./src/aria2c --log=404.log --log-level=info --max-tries=1 \
       "https://example.com/nonexistent-404-path"
   echo "exit=$?"                          # 记录退出码 B
   ```
3. **需要观察的现象**：
   - 两个日志文件中各找 `ERROR` 行（形如 `... [ERROR] [src/AbstractCommand.cc:35N] CUID#N - Download aborted. URI=...`，N 为 349~351 附近的具体行号，取决于编译器对跨行宏 `__LINE__` 的取值），其后紧跟异常栈：
     ```text
     Exception: [src/AbstractCommand.cc:351] errorCode=19 URI=http://this-host-does-not-exist.invalid/file.iso
       -> [src/AbstractCommand.cc:789] errorCode=19 CUID#7 - Name resolution for this-host-does-not-exist.invalid failed:...
     ```
     （404 场景的内层应为 `[src/HttpSkipResponseCommand.cc:218] errorCode=3 Resource not found`。）
   - 控制台末尾的 Download Results 表中两行均为 `ERR` 状态。
4. **预期结果与对照**：
   - 退出码 A = **19**（`NAME_RESOLVE_ERROR`，[error_code.h:L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L65)）；异常栈内层文件行对应异步解析抛出点 [AbstractCommand.cc:L789-L792](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L789-L792)（若你的构建带 c-ares 且未关 `--async-dns`；否则是 [NameResolver.cc:L58-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc#L58-L61)）。
   - 退出码 B = **3**（`RESOURCE_NOT_FOUND`，[error_code.h:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L49)）；抛出点 [HttpSkipResponseCommand.cc:L216-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L216-L222)。
   - 补充实验：给实验二加 `--max-file-not-found=3` 再跑一次，观察日志中异常类型从「立即 aborted」变为反复 `Restarting the download`（DlRetryEx 路径，[AbstractCommand.cc:L364-L397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L364-L397)），最终退出码仍为 3。
5. 若本机无法访问外网，DNS 实验仍可进行（`.invalid` 无需出网），404 实验可改用本地任意 HTTP 服务器的不存在路径；无本地服务时标注**待本地验证**即可。

#### 4.3.5 小练习与答案

**练习 1**：日志里一条 ERROR 的行头与异常栈首行都指向 `src/AbstractCommand.cc` 中 catch 块内同一个语句附近（行号可能相差一两行），这两个位置各来自哪个宏、分别回答什么问题？

**答案**：行头来自 `A2_LOG_ERROR_EX` 宏捕获的 `__FILE__/__LINE__`（[AbstractCommand.cc:L349-L351](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L349-L351) 这条日志语句）；异常栈首行来自同一条语句里 `DL_ABORT_EX2(...)` 宏捕获的包装异常构造位置。两者都在 `catch (DlAbortEx&)` 块内、相隔一两行（跨行宏调用在不同编译器下对 `__LINE__` 的取值还可能不同），分别回答「谁把这条日志写出来的」和「这个包装异常在哪诞生」。

**练习 2**：任务 A 404 失败（码 3）、任务 B 校验失败（码 32）、任务 C 被用户 remove（码 31），三个任务同批下载，进程退出码是多少？若只有任务 C，退出码又是多少？

**答案**：`getDownloadStat`（[RequestGroupMan.cc:L642-L645](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L642-L645)）只把**非 FINISHED/IN_PROGRESS/REMOVED** 的结果计入 error 与 lastError，遍历取最后一个 → 退出码 32。只有任务 C 时 `removed` 不算错误，`allCompleted` 判定下（remove 视作完结）进程返回 0（FINISHED）。

**练习 3**：为什么 aria2 把超时（TIME_OUT=2）抛成 `DlRetryEx` 而不是 `DlAbortEx`？

**答案**：见 [AbstractCommand.cc:L340](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L340)：超时多半是瞬时拥塞或慢站点，值得整队重试（配合 `--retry-wait` 与 IP 标坏逻辑，L316-L339 还会先尝试换 IP）；而 404 这类资源性错误重试无益。异常类型承载「恢复策略」，这正是 u4-l1 三级出口在本讲的回声。

## 5. 综合实践

**任务：为三种典型失败各建一张「诊断卡片」。**

1. **准备**：按 u1-l2 构建项目，准备三个可复现的失败：
   - DNS 失败：`http://nx.invalid/f`（`.invalid` 域保证失败）；
   - HTTP 404：任一可访问站点的不存在路径；
   - 连接拒绝：`http://127.0.0.1:1/f`（1 端口几乎必拒，触发 `errNum=111` 一类的系统错误栈）。
2. **执行**：对每个案例运行
   ```bash
   ./src/aria2c --log=case.log --log-level=info --max-tries=1 "<URI>"; echo "exit=$?"
   ```
3. **产出**（每张卡片五行）：
   | 字段 | 填写方法 |
   | --- | --- |
   | 退出码 | `echo $?`，回 [error_code.h:L44-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L44-L79) 查名称 |
   | 异常链 | 日志中 `Exception:` 起的两行，逐环点开源码链接 |
   | 抛出宏 | 确认是 `DL_ABORT_EX2` 还是 `DL_RETRY_EX2`，写出判断依据（catch 后日志是 `Download aborted` 还是 `Restarting the download`） |
   | 恢复动作 | 对照 [AbstractCommand.cc:L346-L412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L346-L412) 说明 aria2 接下来做了什么（tryReserved / prepareForRetry / halt） |
   | errNum | 栈中出现 `errNum=` 时查出对应 errno 符号（如 `errno -l | grep 111`） |
4. **验收**：三张卡片能互相区分——同样是「下载失败」，你应该能说清 DNS 失败与连接拒绝的 errNum/抛出点差异（前者无 errNum、抛在解析器；后者带 errNum、抛在连接检查），以及 404 与前两者在异常类型上的差异。
5. 运行结果**待本地验证**；本实践的价值在于流程跑通，不依赖特定站点的存活性。

## 6. 本讲小结

- `LogFactory` 是静态配置仓库 + 单例发放点，`A2_LOG_*` 宏自动携带 `__FILE__/__LINE__` 并先过 `levelEnabled` 门槛；文件与控制台两路输出**各自独立过滤**，默认「文件 DEBUG、控制台 NOTICE」，所以 `--log=文件` 一开即是全量日志。
- 日志行头格式两路不同：文件路带微秒时间戳与 `[src/文件:行]`，控制台路只有短时间戳与级别（可着色）；`A2_LOG_*_EX` 变体会把异常 `stackTrace()` 追加在消息后。
- `Exception` 五要素（file、line、errNum、errorCode、cause）构成自研异常体系；`error_code::Value` 身兼三职——异常内码、任务结果码、进程退出码；「msg + cause」构造重载让包装层自动继承底层错误码，整条异常链对外分类唯一。
- 下载异常三分天下：`DlRetryEx` 重试、`DlAbortEx` 换 URI、`DownloadFailureException` 终结任务组；抛出宏（`DL_ABORT_EX2` 等）强制携带抛出点位置，`error_code.h` 是退出码查询总表。
- 统一捕捞点在 `AbstractCommand::execute` 的三段 catch：先 `setLastErrorCode` 记账、再以「新包装异常 + 原因链」记 ERROR 日志、最后决定去向；错误码经 `lastErrorCode_ → DownloadResult → getDownloadStat → MultiUrlRequestInfo::getResult → main` 五棒接力成为 `$?`。
- 诊断口诀：**行头找记录者、`->` 链找祸根、errorCode 查表、`$?` 验收**；启动期异常（选项错误等）走 `option_processing`/`::main` 的捷径，不经过 DownloadEngine。

## 7. 下一步学习建议

- **u9-l5（测试体系）**：用 CppUnit 用例复现本讲的异常路径——例如在测试里直接构造 404 响应头，验证抛出的错误码，比真实网络更可控。
- **u9-l6（二次开发实战）**：试着新增一个带专属 `error_code` 的异常并接入本讲管道（抛出 → 捕获 → 退出码），这是对错误体系的终极检验。
- **延伸阅读源码**：[src/message.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h) 通读一遍消息模板，能大幅提升读日志的语感；[src/OptionHandlerException.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerException.h) 是另一个「异常携带结构化上下文」（Pref ID）的范例；[src/SessionSerializer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SessionSerializer.cc) 展示错误码如何影响哪些任务被写入 `--save-session` 会话文件。
