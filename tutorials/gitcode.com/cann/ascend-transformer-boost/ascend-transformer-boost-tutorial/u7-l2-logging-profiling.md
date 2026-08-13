# 日志与性能 Profiling

## 1. 本讲目标

当算子「能跑」之后，下一步一定是「能看」与「能量」：运行时发生了什么、哪个算子最慢、哪一行代码报了错。ATB 提供了两套互补的可观测工具：

- **日志体系**：用环境变量控制日志级别与输出位置，用 `ATB_LOG` 宏在源码里打点，用 `SetLogLevel`/`ResetLogLevel` 在运行中动态调级。
- **性能统计**：用 `torch_atb` 暴露的 `ProfStats` 采集每个算子的 Host 端耗时，定位热点。

学完本讲，你应当能够：

1. 说出控制 ATB 日志的关键环境变量，并按 CANN 版本选用正确的一组。
2. 读懂 `ATB_LOG` / `ATB_FLOG` / `ATB_LOG_IF` 宏的内部逻辑，理解「级别门控」为什么能省性能。
3. 用 Python 端的 `torch_atb.Prof` 拿到某个算子最近 N 次的耗时分布。

## 2. 前置知识

- **Host/Device 异步模型**：推理时 Host（CPU）把算子逐个下发到 Device（NPU）的执行流上，下发与计算异步进行（见 u1-l1）。日志大多打在 Host 侧，描述「我下发到了哪一步」；性能统计的耗时也是 Host 视角的「从 Setup 到 Execute 完成」。
- **两段式执行**：`Setup`（Host 校验、形状推导、Tiling、算 workspace）与 `Execute`（异步下发 Device）分两步（见 u1-l6）。`ProfStats` 测的正是这两步合起来的 Host 端墙钟时间。
- **OperationBase/Runner**：本讲引用的日志打点位于 Runner（`OpsRunner`、`Runner`）内部，对应 u3-l1/u3-l2 讲过的「Operation → Runner → KernelGraph → Kernel」链路。
- **LogLevel 概念**：日志按严重程度分级，只有「消息级别 ≥ 当前阈值」时才真正输出，低于阈值的消息在构造日志流之前就被短路丢弃，从而不产生格式化开销。

> 名词解释：
> - **Sink（日志下沉点）**：日志最终去往的目的地，ATB 用 `Mki::LogSinkStdout`（控制台）与 `Mki::LogSinkFile`（文件）两种。
> - **门控（gate）**：在拼装日志字符串之前先判断级别，不达标直接跳过，避免无谓的字符串拼接开销。
> - **thread_local**：C++ 线程局部存储，每个线程有自己独立的实例副本。`ProfStats` 用它保证多线程下耗时统计互不串扰。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/logging_and_debugging.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md) | 官方日志与调试指南，列全所有环境变量与调试工具 |
| [src/atb/utils/log.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h) | `ATB_LOG`/`ATB_FLOG`/`ATB_LOG_IF`/`ATB_CHECK` 宏定义，薄封装 MKI 日志流 |
| [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) | 公开 `atb::LogLevel` 枚举（5 档 + NONE） |
| [include/atb/utils.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/utils.h) | `atb::Utils::SetLogLevel` / `ResetLogLevel` 声明 |
| [src/atb/utils/utils.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp) | 动态调级实现：atb 级别 ↔ MKI 级别映射、Sink 增删 |
| [src/atb/runner/runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp) / [src/atb/runner/ops_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp) | 真实 `ATB_LOG` 打点样例 |
| [src/torch_atb/prof/prof_stats.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.h) / [prof_stats.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.cpp) | `ProfStats`：按算子名缓存最近 1000 次耗时 |
| [src/torch_atb/operation_wrapper.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp) | Python `forward()` 的 C++ 实现，每次调用后写一条耗时 |
| [src/torch_atb/bindings.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp) | 把 `ProfStats` 暴露为 Python 类 `torch_atb.Prof` |

## 4. 核心概念与源码讲解

### 4.1 日志体系：环境变量、LogLevel 与动态调级

#### 4.1.1 概念说明

ATB 的日志底层依赖 MKI 框架（`mki/utils/log/*`），但用户侧只需要关心三件事：**级别（Level）、输出位置（Sink）、触发方式（环境变量 or 运行时 API）**。

- **级别**有两套表述，不要混淆：
  - 内部 MKI 级别（`Mki::LogLevel`）：`TRACE < DEBUG < INFO < WARN < ERROR < FATAL`，共 6 档，`ATB_LOG` 宏直接用它。
  - 公开 `atb::LogLevel`：`DEBUG < INFO < WARN < ERROR < NONE`，共 5 档，`atb::Utils::SetLogLevel` 用它。
  - 环境变量级别（CANN 统一编号）：`0=DEBUG, 1=INFO, 2=WARNING, 3=ERROR, 4=NULL`。
- **触发方式**：进程启动前用环境变量设；进程跑起来后用 `SetLogLevel` 改、`ResetLogLevel` 还原。8.5 及以后版本才支持运行时动态调级。

#### 4.1.2 核心流程

```text
启动阶段（环境变量）                  运行阶段（API）
─────────────────────                ──────────────────────────
ASCEND_GLOBAL_LOG_LEVEL   ─┐         atb::Utils::SetLogLevel(X)
ASCEND_MODULE_LOG_LEVEL   ─┼─► MKI      ─► atb LogLevel → MKI LogLevel
ASCEND_SLOG_PRINT_TO_STDOUT         ─► LogCore.SetLogLevel(...)
ASCEND_PROCESS_LOG_PATH   ─┘        ─► 必要时补挂 Sink
                                       （关闭后再开需要重新 AddSink）
        │                                     │
        └────────────►  Mki::LogCore（单例） ◄┘
                              │
                    GetLogLevel() 作为门控阈值
                              │
                  ATB_LOG_* 宏逐条比对、决定是否输出
```

关键点：**环境变量是初始态，API 是覆盖态**；`ResetLogLevel` 不是「关掉日志」，而是「回到环境变量当初设的那个级别」。

#### 4.1.3 源码精读

公开的 `atb::LogLevel` 定义在 types.h，是一个 `enum class`：

- [types.h:76-82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L76-L82) — 5 档公开级别，`NONE` 表示关闭日志。

运行时调级接口声明在 utils.h：

- [utils.h:91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/utils.h#L91) — `SetLogLevel(atb::LogLevel)`，返回 `Status`，成功为 `NO_ERROR`。
- [utils.h:98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/utils.h#L98) — `ResetLogLevel()`，恢复到环境变量设置的级别。

`SetLogLevel` 的实现值得细读，因为它揭示了一个内部约定——`NONE` 没有对应的 MKI 级别，于是用 `FATAL` 当「日志已关闭」的哨兵：

- [utils.cpp:279-308](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L279-L308) — `SetLogLevel` 全流程。当传入 `NONE` 时，先把 Stdout 与 File 两个 Sink 都移除，再把内部级别设到 `FATAL`（见 [utils.cpp:281-286](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L281-L286)）。重新打开日志时，若发现 Sink 已被清空，会按当前 `ASCEND_SLOG_PRINT_TO_STDOUT` 配置补挂 File（必要时加 Stdout）Sink（见 [utils.cpp:299-306](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L299-L306)）。这说明 **「关日志」是删 Sink + 抬高阈值，「开日志」是加 Sink + 降阈值**，二者对称。

`ResetLogLevel` 则先读环境变量、反推出 atb 级别，再调一次 `SetLogLevel`：

- [utils.cpp:310-334](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L310-L334) — 优先读模块级 `ASCEND_MODULE_LOG_LEVEL`（高优先级），其次全局级 `ASCEND_GLOBAL_LOG_LEVEL`（见 [utils.cpp:312-313](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L312-L313)），都缺省时回落到默认 `ERROR`（[utils.cpp:315](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L315)）。这与文档「优先级 ASCEND_MODULE_LOG_LEVEL > ASCEND_GLOBAL_LOG_LEVEL」一致。

环境变量的权威清单在官方文档，且按 CANN 版本分了三组：

- [logging_and_debugging.md:11-15](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L11-L15) — **8.3 之前**用 `ASDOPS_LOG_LEVEL` / `ASDOPS_LOG_TO_STDOUT` / `ASDOPS_LOG_TO_FILE` / `ASDOPS_LOG_TO_FILE_FLUSH` / `ASDOPS_LOG_PATH`，默认级别 `ERROR`，调试建议 `DEBUG`/`INFO`。
- [logging_and_debugging.md:19-21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L19-L21) — **8.3 起**改用 CANN 统一变量 `ASCEND_GLOBAL_LOG_LEVEL`（0~4）、`ASCEND_SLOG_PRINT_TO_STDOUT`、`ASCEND_PROCESS_LOG_PATH`。
- [logging_and_debugging.md:25-28](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L25-L28) — **8.5 起**新增高优先级的 `ASCEND_MODULE_LOG_LEVEL=OP=0`（`OP` 即 ATB 所在模块），可单独把 ATB 调到 DEBUG 而不影响其它模块。

> 实践经验：从 8.5 起，定位 ATB 自身问题最常用 `export ASCEND_MODULE_LOG_LEVEL=OP=0`（只放大 ATB 日志）+ `export ASCEND_SLOG_PRINT_TO_STDOUT=1`（直接看控制台），既详细又不被海量 CANN 日志淹没。

文档还给出动态调级的示例代码：

- [logging_and_debugging.md:44-64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L44-L64) — 在 `main` 里用 `atb::Utils::SetLogLevel(atb::LogLevel::NONE)` 临时静默，再用 `ResetLogLevel()` 恢复。

#### 4.1.4 代码实践

**实践目标**：确认环境变量能改变日志输出量，并理解版本差异。

**操作步骤**（均为环境准备命令，需在装有 CANN/昇腾环境上运行）：

1. 先 `source output/atb/set_env.sh` 配好环境。
2. 选择一组与本机 CANN 版本匹配的变量。8.5 及以上推荐：
   ```bash
   export ASCEND_MODULE_LOG_LEVEL=OP=0   # 仅把 ATB(OP 模块)调到 DEBUG
   export ASCEND_SLOG_PRINT_TO_STDOUT=1  # 日志打到控制台
   export ASCEND_PROCESS_LOG_PATH=$PWD/atb_log  # 日志文件目录
   ```
3. 运行一个已有 demo，例如 `example/op_demo` 下任一算子的 `bash build.sh`（参考 u2-l1）。
4. 改成 `export ASCEND_MODULE_LOG_LEVEL=OP=3`（ERROR）再跑一次，对比日志行数。

**需要观察的现象**：`OP=0` 时控制台刷出大量带 `Decoder_layer...`、`...runner graph`、`launchParam` 等关键字的 ATB 日志；`OP=3` 时几乎只剩报错。

**预期结果**：日志量随级别数字增大而显著减少，证实「级别门控」生效。

> 说明：本机是否装有昇腾环境未知，若无法运行请标注「待本地验证」；即便不能跑，也可以只做「设置变量 → 读文档 → 预期行为」的纸面推演。

#### 4.1.5 小练习与答案

**练习 1**：`ASCEND_GLOBAL_LOG_LEVEL` 与 `ASCEND_MODULE_LOG_LEVEL` 同时设置且冲突时，哪个生效？

> **答案**：`ASCEND_MODULE_LOG_LEVEL` 生效（优先级更高）。`ResetLogLevel` 的源码（[utils.cpp:319-328](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L319-L328)）也是先判模块级、`else if` 才看全局级。

**练习 2**：把日志级别设成 `NONE` 后，再 `SetLogLevel(INFO)`，日志能恢复吗？为什么源码里要 `AddSink`？

> **答案**：能恢复。因为 `NONE` 会把 Stdout/File 两个 Sink 都删掉，仅靠 `SetLogLevel` 降阈值是不够的——没有 Sink 日志无处可去。所以源码在 [utils.cpp:299-304](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/utils.cpp#L299-L304) 检测到「Sink 被清空」时重新补挂 Sink。

---

### 4.2 ATB_LOG 宏家族：统一日志门面

#### 4.2.1 概念说明

`ATB_LOG` 是 ATB 内部统一的日志打点入口，定义在 `log.h`。它对 MKI 的 `Mki::LogStream` 做了一层薄封装，提供三类宏：

- `ATB_LOG(LEVEL)` / `ATB_LOG_LEVEL`：流式输出，如 `ATB_LOG(INFO) << "msg" << x;`
- `ATB_FLOG(LEVEL, fmt, ...)`：printf 风格格式化输出。
- `ATB_LOG_IF(condition, LEVEL)`：条件成立才输出（等价于 `if(cond) ATB_LOG(LEVEL)`，但更简洁、不易写错大括号）。
- `ATB_CHECK(condition, logExpr, handleExpr)`：断言，条件不满足时打日志并执行 handleExpr。

每个宏内部都做了**级别门控**：先比较「本条消息级别 ≥ 全局阈值」才构造 `LogStream`，否则整条 `<<` 链路被跳过，连参数都不格式化。这是高频打点不拖慢推理的关键。

#### 4.2.2 核心流程

以 `ATB_LOG(INFO) << GetLogPrefix() << ...` 为例，宏展开后等价于：

```text
if (Mki::LogLevel::INFO >= Mki::LogCore::Instance().GetLogLevel())  // 门控
    Mki::LogStream(__FILE__, __LINE__, __FUNCTION__, Mki::LogLevel::INFO) << ...  // 真正输出
```

- 门控不成立 → 直接短路，`GetLogPrefix()` 等右侧表达式**根本不会被求值**。
- 门控成立 → 构造临时 `LogStream`，链式 `<<` 拼装消息，析构时把完整一行送往已注册的 Sink。

`ATB_FLOG` 多一步 `.Format(format, __VA_ARGS__)`，把 printf 风格串格式化后再写入流。

#### 4.2.3 源码精读

两个「分发宏」是理解全家的钥匙——`ATB_LOG(level)` 只是把 `level` 拼成 `ATB_LOG_##level`：

- [log.h:24-25](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L24-L25) — `ATB_LOG(level)` → `ATB_LOG_##level`，`ATB_FLOG(level, fmt, ...)` → `ATB_FLOG_##level(fmt, ...)`。所以 `ATB_LOG(INFO)` 实际展开为 `ATB_LOG_INFO`。

以 `ATB_LOG_INFO` 为例看门控写法：

- [log.h:37-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L37-L39) — 先 `if (INFO >= LogCore::GetLogLevel())`，再构造 `LogStream`。`ATB_LOG_TRACE/DEBUG/WARN/ERROR/FATAL`（[log.h:31-48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L31-L48)）结构完全一致，仅级别不同。`ATB_FLOG_*`（[log.h:50-67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L50-L67)）多了 `.Format(...)`。

条件输出宏：

- [log.h:27-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L27-L29) — `ATB_LOG_IF(cond, level)` = `if (cond) ATB_LOG(level)`。常用于「出错才打日志」。

下面看真实打点样例（Runner 内部）：

- [runner.cpp:149-153](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/runner.cpp#L149-L153) — 当 `ATB_STREAM_SYNC_EVERY_RUNNER_ENABLE` 开启时，每个 Runner 执行后做流同步；同步失败才用 `ATB_LOG_IF(retCode != 0, ERROR)` 打错误。注意 `GetLogPrefix()` 会带 `XXX_层号_节点序号:执行次数` 的命名，正是读日志时定位算子的关键。
- [ops_runner.cpp:1048](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L1048) — 流式用法 `ATB_LOG(INFO) << GetLogPrefix() << " node[" << nodeId << "] " << node.GetName() << " aclrtSynchronizeStream."`，把节点序号与名字拼进日志，对应文档「`AttentionRunner_87_0:12` 表示第 87 层第 0 个节点第 12 次执行」。
- [ops_runner.cpp:1034](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L1034) — 落盘张量（SaveLaunchParam）时用 INFO 记录目录，便于精度比对（见 4.1 文档 msit dump）。
- [ops_runner.cpp:1040-1041](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L1040-L1041) — 调试用 `ATB_LOG_IF(... != ..., FATAL)`：同一 Kernel 前后 global tiling 不一致就报致命错误，用于排查 tiling 复用 bug。

最后注意 `log.h` 还承载了对外错误描述结构：

- [log.h:69-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L69-L78) — `atb::ExternalError`（含 `errorType`/`errorDesc`/`errorData`/`solutionDesc`）及其 `operator<<`，让错误信息也能流式打日志。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读理解「门控短路」，而不是真去改源码（本讲不改源码）。

**操作步骤**：

1. 打开 [log.h:37-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L37-L39)，把 `ATB_LOG(INFO) << GetLogPrefix() << ...`（[ops_runner.cpp:1048](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/ops_runner.cpp#L1048)）手工展开成 `if + LogStream`。
2. 思考：若全局阈值是 `ERROR`，那么 `GetLogPrefix()` 会被调用吗？

**需要观察的现象（纸面推演）**：展开后是 `if (INFO >= ERROR)` —— 不成立（`INFO < ERROR`），整个 `if` 体被跳过，`GetLogPrefix()`、`node.GetName()` 都不求值。

**预期结果**：在 ERROR 级别下，`ATB_LOG(INFO)` 的开销接近零（一次整数比较），这正是 ATB 敢在热路径大量埋点的原因。

> 这是「源码阅读型实践」，无需运行环境即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ATB_LOG(INFO) << func()` 在高级别阈值下是安全的？如果改成 `func(); ATB_LOG(INFO) << ...` 呢？

> **答案**：宏把整条语句包在 `if` 内，`func()` 是 `<<` 的操作数，门控不成立时根本不求值，故无副作用风险（前提是 `func()` 没有必须执行的副作用）。若把 `func()` 提到 `ATB_LOG` 之外单独调用，则无论级别如何都会执行，埋点就不再「免费」了。

**练习 2**：`ATB_LOG_IF(ret != 0, ERROR) << msg` 与 `if (ret != 0) ATB_LOG(ERROR) << msg` 等价吗？

> **答案**：等价。见 [log.h:27-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/utils/log.h#L27-L29)，宏就是展开成后者。`ATB_LOG_IF` 只是更紧凑、避免手写 `if` 时漏写大括号或多语句悬空。

---

### 4.3 ProfStats：Python 端算子耗时统计

#### 4.3.1 概念说明

`ProfStats` 是 `torch_atb` 提供的**轻量 Host 端耗时统计器**。它不做设备级 profiling（那要靠 `msProf`，见 4.1 文档），而是回答一个朴素问题：「我用 Python 调了某个 ATB 算子 N 次，每次 `forward()` 在 Host 侧花了多久？」

它的设计有三个特点：

1. **按算子名分桶**：以 `opName`（即 `Operation::GetName()`）为 key，每个算子维护一个耗时列表。
2. **滑动窗口**：每个算子最多保留最近 1000 次（`MAX_RUN_TIMES`），超出就丢弃最旧的，避免长跑把内存撑爆。
3. **线程局部**：`GetProfStats()` 返回 `thread_local` 实例，多线程推理时各算各的、互不串扰。

#### 4.3.2 核心流程

```text
Python: op.forward([in])
        │
        ▼  (pybind11)
C++:  OperationWrapper::Forward(inTensors)
        ├─ Mki::Timer runTimer;            // 开始计时
        ├─ Setup(inTensors, outTensors);   // Host: 校验/形状/Tiling
        ├─ Execute();                       // 异步下发 Device
        └─ ProfStats::GetProfStats().SetRunTime(opName, runTimer.ElapsedMicroSecond());
                       │
                       ▼
        runTimeStatsMap[opName].push_back(微秒)  // 满 1000 先 erase 最旧
                       │
Python: torch_atb.Prof.get_prof_stats().get_run_time_stats(opName)
        ◄── 返回 std::vector<uint64_t>（最近 N 次耗时）
```

注意：这里的耗时是 **Host 墙钟时间**（从 Setup 开始到 Execute 返回），不等同于 Device 上的 Kernel 执行时间。它更适合定位「Host 下发慢（Host Bound）」与「某个算子 Setup 特别贵」之类的问题。

#### 4.3.3 源码精读

先看声明（prof_stats.h）：

- [prof_stats.h:19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.h#L19) — `constexpr size_t MAX_RUN_TIMES = 1000;`，滑动窗口上限。
- [prof_stats.h:21-31](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.h#L21-L31) — `ProfStats` 类：`GetProfStats()` 取实例，`SetRunTime(opName, runTime)` 写入，`GetRunTimeStats(opName)` 读出，私有成员 `std::map<std::string, std::vector<uint64_t>> runTimeStatsMap`。

再看实现（prof_stats.cpp）：

- [prof_stats.cpp:19-23](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.cpp#L19-L23) — `GetProfStats` 返回 `thread_local` 单例，这是「多线程互不干扰」的保证。
- [prof_stats.cpp:25-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.cpp#L25-L32) — `SetRunTime`：取/建该算子的 vector，长度达到 `MAX_RUN_TIMES` 就 `erase(begin())` 丢最旧，再 `push_back`。这是一段标准的「定长环形缓冲（用 vector 模拟）」。
- [prof_stats.cpp:34-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.cpp#L34-L41) — `GetRunTimeStats`：找不到 key 返回空 vector，找到返回拷贝。

写入点在 Python `forward` 的 C++ 实现里：

- [operation_wrapper.cpp:231-242](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L231-L242) — `Forward` 全貌：构造 `Mki::Timer runTimer`（[operation_wrapper.cpp:233](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L233)），跑完 `Setup` + `Execute` 后，调 `ProfStats::GetProfStats().SetRunTime(GetName(), runTimer.ElapsedMicroSecond())`（[operation_wrapper.cpp:240](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/operation_wrapper.cpp#L240)）。`GetName()` 即算子名，作为分桶 key。

最后是 Python 暴露（bindings.cpp）：

- [bindings.cpp:45-47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L45-L47) — 把 `ProfStats` 注册为 Python 类 `Prof`，暴露静态方法 `get_prof_stats()`（返回引用）与实例方法 `get_run_time_stats(opName)`。所以在 Python 里写作 `torch_atb.Prof.get_prof_stats().get_run_time_stats(name)`。

> 这也解释了 u2-l2 讲过的一点：Python 端一行 `forward()` 等价于 C++ 的 `Setup + Execute + 流同步`，而 `ProfStats` 正好挂在这条统一通路上，所以**任何**用 `torch_atb.Operation` 调用的算子都会被自动统计，无需额外开关。

#### 4.3.4 代码实践

**实践目标**：用 `torch_atb.Prof` 拿到某算子的耗时列表，并算出平均/最大值。

**操作步骤**（依赖 torch_atb 与昇腾 NPU，无环境时标注「待本地验证」）：

1. 参照 u2-l2，用 `torch_atb` 创建一个 `Linear` 算子并在 `.npu()` 上反复 `forward`。
2. 跑若干轮后，读取统计：
   ```python
   import torch_atb
   # op 为已创建并 forward 过的 Operation
   name = "Linear"  # 即 op 对应的 GetName，不同 Param 名字可能带后缀
   times = torch_atb.Prof.get_prof_stats().get_run_time_stats(name)
   if times:
       print(f"count={len(times)} avg={sum(times)//len(times)}us max={max(times)}us")
   ```

**需要观察的现象**：`times` 是一列整数（微秒）；反复 `forward` 后列表增长，最多保留 1000 条；首几次通常偏大（含首次 Setup/Tiling/Kernel 编译缓存未命中），之后趋于稳定。

**预期结果**：能拿到非空列表并打印统计；若返回空列表，说明 `name` 与 `GetName()` 不符（可先 `print(op...)` 或看日志确认真实算子名），或该线程尚未调用过该算子（`thread_local`，须在同一线程读取）。

> 待本地验证：具体算子名字符串与耗时数值取决于本机芯片与输入规模。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ProfStats` 用 `thread_local` 而不是全局单例？

> **答案**：多线程推理（如多 stream、多 worker）时，不同线程并发调用 `forward`，若共用一个 `map` 既要加锁又会把不同线程的耗时混在一起，既慢又失真。`thread_local`（[prof_stats.cpp:21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/prof/prof_stats.cpp#L21)）让每线程独立统计，无锁且隔离。代价是必须在**同一线程**读取才有数据。

**练习 2**：`SetRunTime` 测的是「Setup + Execute」的 Host 时间，它能反映 Kernel 在 NPU 上的真实执行耗时吗？为什么？

> **答案**：不能完全反映。`Execute` 只是异步下发，函数返回时 Kernel 未必算完；测到的是「下发开销 + 可能的部分等待」，而非纯 Device 计算时间。要测 Kernel 真实耗时需用 `msProf` 采集 Device 侧 profile（见 [logging_and_debugging.md:121-138](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L121-L138)）。`ProfStats` 更适合排查 Host Bound 与 Setup 开销。

## 5. 综合实践

把本讲三块串起来，设计一个「定位热点算子」的小任务：

1. **开详细日志**：`export ASCEND_MODULE_LOG_LEVEL=OP=0`、`export ASCEND_SLOG_PRINT_TO_STDOUT=1`，跑一段用 `torch_atb` 组的图算子（参考 u2-l2 / u5-l4）。
2. **读日志定位结构**：在输出里搜 `runner graph`、`launchParam` 关键字，对照 [logging_and_debugging.md:72-82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md#L72-L82) 的命名规则（`算子名_层号_节点序号:执行次数`），找到耗时可疑的算子名。
3. **量耗时**：用 `torch_atb.Prof.get_prof_stats().get_run_time_stats(算子名)` 拿到该算子近 1000 次 Host 耗时，算 avg/max。
4. **动态降噪**：定位完成后，在代码里 `atb::Utils::SetLogLevel(atb::LogLevel::NONE)` 临时关掉 DEBUG 日志（避免日志本身拖慢），事后再 `ResetLogLevel()` 恢复。
5. **（进阶）Device 侧验证**：若怀疑是 Kernel 本身慢而非 Host 下发慢，改用 `msProf` 采集，看 `op_summary_*.csv` 里该算子的真实执行时间与 cache 命中率。

> 交付物：一段「日志关键字 → 算子名 → ProfStats 耗时 → 结论（Host Bound or Kernel Bound）」的排查记录。无昇腾环境时，至少完成 1、2 两步的纸面推演并标注「待本地验证」。

## 6. 本讲小结

- ATB 日志底层是 MKI 的 `LogCore` + Sink（Stdout/File），公开面收敛为 `atb::LogLevel`（DEBUG/INFO/WARN/ERROR/NONE）。
- 环境变量按 CANN 版本分三组：8.3 前用 `ASDOPS_LOG_*`，8.3 起用 `ASCEND_GLOBAL_LOG_LEVEL` 等，8.5 起可用高优先级的 `ASCEND_MODULE_LOG_LEVEL=OP=0` 单独放大 ATB 日志。
- `ATB_LOG`/`ATB_FLOG`/`ATB_LOG_IF` 宏都内置「级别门控」：消息级别低于阈值时整条 `<<` 链短路、参数不求值，故热路径埋点近乎免费。
- `atb::Utils::SetLogLevel`/`ResetLogLevel` 支持运行时动态调级；`NONE` 用 MKI 的 `FATAL` 当「关闭」哨兵并删 Sink，重开时补挂 Sink。
- `ProfStats`（Python 类 `torch_atb.Prof`）以算子名为 key、`thread_local` 单例、滑动窗口 1000 条，自动统计每次 `forward()` 的 Host 端耗时，适合排查 Host Bound 与 Setup 开销，不等于 Kernel 真实执行时间。
- 日志与 ProfStats 互补：日志告诉你「跑到哪、对不对」，ProfStats 告诉你「哪个算子在 Host 侧最贵」，Device 侧真实耗时则交给 `msProf`。

## 7. 下一步学习建议

- **测试体系**：日志和耗时统计常被写进测试断言与基准，建议接着读 u7-l3「测试框架与算子测试」，看 `tests/` 如何用 JSON 驱动做精度与性能测试。
- **编译与 Sanitizers**：若日志暴露出内存类异常，可结合 u7-l4「编译选项、ABI 与 Sanitizers」用 ASAN/MSAN 复现定位。
- **深入官方文档**：[docs/logging_and_debugging.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/logging_and_debugging.md) 还讲了 `msProf`（性能）、`msit dump`/`msprobe`（精度）、`msDebug` 与 `AscendC_Dump`（单算子调试），是本讲 Device 侧工具的权威补充。
- **回到链路**：想理解日志里的 `runner graph`、`launchParam` 到底对应哪段代码，重温 u3-l2（Runner 体系）与 u3-l4（Kernel/MKI）。
