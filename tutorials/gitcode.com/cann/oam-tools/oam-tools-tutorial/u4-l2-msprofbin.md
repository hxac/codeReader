# u4-l2 msprofbin：命令行入口与任务管理

## 1. 本讲目标

上一讲（u4-l1）我们搭好了 msprof 的整体骨架：C++ 侧 collector 负责采集，Python 分析 wheel 负责解析。本讲深入 collector 中最靠近用户的那一层——`msprofbin` 目录，学完后你应该能够：

1. 读懂 `msprof_bin.cpp` 中 `main()` 函数的完整启动顺序（环境保存 → 平台初始化 → 参数解析 → manager 创建 → 任务执行）。
2. 理解 `input_parser.cpp` 如何用 `OsalGetOptLong` + `LONG_OPTIONS` 表驱动地解析几十个命令行选项，并完成「校验 + 翻译进 ProfileParams」两步工作。
3. 理解 `msprof_manager.cpp` 如何根据参数把一次 msprof 调用路由到 AppMode / SystemMode / ParseMode / QueryMode / ExportMode / AnalyzeMode 六种运行模式之一。
4. 理解 `msprof_task.cpp` 中每个设备上的采集任务（MsprofTask / ProfSocTask / ProfRpcTask）的生命周期。

## 2. 前置知识

- **入口二进制**：用户在终端敲的 `msprof` 命令，安装后就是 msprofbin 构建目标（改名安装到 `tools/profiler/bin`）。本讲的 `main()` 就是这条命令的起点。
- **getopt_long 风格解析**：Linux C 标准库 `getopt_long` 用一张「长选项名 → 枚举值」的表来解析 `--xxx=yyy` 形式的参数。本项目用 OSAL 封装的 `OsalGetOptLong` + `LONG_OPTIONS` 表，思想相同：每读到一个选项，返回它的枚举值 `opt`，再按 `opt` 分发校验逻辑。
- **ProfileParams**：一个巨大的参数结构体（定义在 `message/prof_params.h`），是所有采集配置的唯一载体。命令行解析、运行模式检查、任务执行，全都围绕这一个结构体传递——这与 u4-l1 说的「多入口单收敛为 ProfileParams」呼应。
- **运行模式（RunningMode）**：msprof 一次调用只做一件事——要么带着业务程序采集（app）、要么做系统级采集（system）、要么对已有数据做 parse/query/export/analyze 四种离线分析。这件事的「身份」就叫运行模式。
- **单例（instance()）**：`MsprofManager::instance()`、`Platform::instance()` 等是典型的懒加载单例，进程内全局唯一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp` | main 入口：环境保存、平台初始化、参数解析、MsprofManager 创建与执行 |
| `src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp` | 命令行解析、逐项校验、写入 ProfileParams；同时实现 Args/ArgsManager 帮助信息 |
| `src/msprof/collector/dvvp/msprofbin/include/input_parser.h` | 定义参数枚举 `ArgsMsprofCmd`、选项表 `LONG_OPTIONS`、`InputParser` 类 |
| `src/msprof/collector/dvvp/msprofbin/src/msprof_manager.cpp` | MsprofManager：生成运行模式并委托执行 |
| `src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp` | RunningMode 基类与 App/System/Parse/Query/Export/Analyze 六个子模式 |
| `src/msprof/collector/dvvp/msprofbin/src/msprof_task.cpp` | MsprofTask / ProfSocTask / ProfRpcTask：单设备采集任务生命周期 |
| `docs/zh/profiling/msprof_cmd/general_collect_commands.md` | 用户视角的 msprof 采集命令文档（实践任务用） |

## 4. 核心概念与源码讲解

### 4.1 msprof_bin.cpp：main 函数与启动顺序

#### 4.1.1 概念说明

`msprof_bin.cpp` 是整个 `msprof` 命令的入口。它的设计哲学是「入口薄」：只负责按固定顺序串联各子系统，不包含任何业务逻辑。所有失败路径都直接 `return PROFILING_FAILED`（非 0 退出码），成功返回 `PROFILING_SUCCESS`（0）。

注意一个细节：`main` 通过第三个参数 `envp` 接收环境变量表——比标准的 `main(int, char**)` 多一个，因为 msprof 需要把父进程的完整环境保存下来，之后启动业务程序或分析脚本时原样传递。

#### 4.1.2 核心流程

`main()` 的初始化顺序可以画成：

```text
main(argc, argv, envp)
 ├─ (1) SetEnvList(envp)               # 把 envp 拆成 vector<string>，上限 4096 条
 ├─ (2) EnvManager::SetGlobalEnv()     # 保存全局环境
 ├─ (3) Platform::PlatformInitByDriver()  # 通过驱动探测平台（失败即退出）
 ├─ (4) Platform::Init()               # 平台模式初始化
 ├─ (5) MsopprofManager::MsopprofProcess()  # 尝试按子命令 msprof op 分流（算子 profiling）
 ├─ (6) InputParser::MsprofGetOpts()   # 解析命令行 → ProfileParams
 ├─ (7) MsprofManager::Init(params)    # 生成运行模式 + 模式级参数检查
 ├─ (8) signal(SIGINT, StopProfiling)  # 注册 Ctrl+C 处理
 └─ (9) MsprofManager::MsProcessCmd()  # 执行当前运行模式的任务
      └─ 成功且非动态采集 → PrintOutPutDir() 打印结果目录
```

#### 4.1.3 源码精读

main 函数本体，涵盖上面 (1)~(9) 全部步骤：

[msprof_bin.cpp:87-141](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L87-L141) — `#ifdef __PROF_LLT` 分支下是 `LltMain`（低延迟测试用），否则是真正的 `main`。依次完成环境保存、两次平台初始化、无参数时打印用法、msopprof 子命令分流、参数解析、manager 初始化、注册 SIGINT、执行命令。

其中「argv 里只有 -h」是个特例：`HasHelpParamOnly()` 返回真时直接以成功退出（打印帮助不算失败）：

[msprof_bin.cpp:115-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L115-L117) — 仅当 `usedParams` 只含 `ARGS_HELP` 时提前返回成功。

Ctrl+C 的处理也很讲究——不是立刻杀进程，而是通知运行模式优雅停止：

[msprof_bin.cpp:76-84](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L76-L84) — `StopProfiling` 先 sleep 1 秒（等业务进程自己处理信号），再调用 `MsprofManager::instance()->NotifyStop()` 置停止标志。

#### 4.1.4 代码实践

**实践目标**：把 main 的启动顺序落到具体函数调用上。

**操作步骤**：

1. 打开 `src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp`，定位到 L89 的 `main`。
2. 逐行给 9 个阶段编号注释（对照 4.1.2 的流程图）。
3. 对每个阶段回答两个问题：失败时返回什么？该阶段位于哪个文件/类？

**需要观察的现象 / 预期结果**：你应该得到一张类似下表的清单（已按源码验证）：

| 阶段 | 函数调用 | 所在文件 | 失败处理 |
| --- | --- | --- | --- |
| 保存环境 | `SetEnvList` → `EnvManager::SetGlobalEnv` | msprof_bin.cpp / env_manager | 超限截断告警 |
| 平台初始化 | `Platform::PlatformInitByDriver` / `Platform::Init` | platform/platform | 直接 return FAILED |
| op 分流 | `MsopprofManager::MsopprofProcess` | msopprof_manager.cpp | 失败则继续走 msprof 主流程 |
| 参数解析 | `InputParser::MsprofGetOpts` | input_parser.cpp | nullptr → return FAILED |
| manager 初始化 | `MsprofManager::Init` | msprof_manager.cpp | 打印 "Start profiling failed" |
| 执行 | `MsprofManager::MsProcessCmd` | msprof_manager.cpp → running_mode.cpp | NOTSUPPORT 与 FAILED 分开提示 |

若在真实环境运行 `msprof --help`，可验证「仅 help 参数→成功退出码 0」这条路径（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 main 需要第三个参数 `envp`，而普通程序不需要？
**答案**：msprof 后续要以子进程方式启动用户业务程序（AppMode）和 Python 分析脚本（分析任务），并希望它们继承 msprof 收到的一致环境；因此在入口先把 `envp` 全量存进 `EnvManager`，之后 `RunningMode::SetEnvList` 再原样取出传给子进程（见 running_mode.cpp L217-220）。

**练习 2**：`StopProfiling` 里为什么先 `usleep(OSAL_TIMES_MILLIONS)` 再 NotifyStop？
**答案**：给信号留出传播时间——用户 Ctrl+C 时信号同时发给前台进程组里的业务进程，先等 1 秒让业务进程有机会自行收尾，再通知 msprof 侧置 `isQuit_` 标志进入优雅停止流程，避免把还在写数据的任务直接打断。

---

### 4.2 input_parser.cpp：表驱动的命令行解析

#### 4.2.1 概念说明

`InputParser` 负责「字符串命令行 → 结构化 ProfileParams」。它解决三个问题：

1. **选项识别**：msprof 有 60 多个选项，全部登记在 `LONG_OPTIONS` 表里，`OsalGetOptLong` 每次返回一个枚举值。
2. **校验**：每个选项的取值范围、白名单、平台可用性各不相同，由一组 `CheckXxxValid` 函数处理。
3. **翻译**：命令行形态（Hz 频率、on/off 字符串）与内部形态（毫秒/微秒采样间隔）不同，校验通过后立即换算写入 `params_`。

还有一个容易被忽略的角色：`ArgsManager` 负责按当前平台**动态生成** `--help` 输出——帮助文案本身是按平台能力裁剪的。

#### 4.2.2 核心流程

解析主循环（`MsprofGetOpts`）：

```text
MsprofGetOpts(argc, argv)
 ├─ CheckInputArgsLength              # 总长度防御
 ├─ SplitApplicationArgv              # 把 msprof 选项与用户程序参数切成两段
 ├─ while (opt = OsalGetOptLong(...))
 │    ├─ opt == ARGS_HELP → 打印帮助并返回
 │    ├─ PreCheckPlatform(opt)        # 该选项在当前平台是否被禁止
 │    └─ ProcessOptions(opt)
 │         ├─ 按 opt 区间选校验器：
 │         │    ARGS_OUTPUT..ARGS_RULE / NTS → MsprofCmdCheckValid（值校验）
 │         │    ARGS_ASCENDCL..ARGS_ANALYZE  → MsprofSwitchCheckValid（on/off 开关）
 │         │    ARGS_AIC_FREQ..EXPORT_MODEL_ID → MsprofFreqCheckValid（数值范围）
 │         │    ARGS_HOST_SYS..HOST_SYS_USAGE → MsprofHostCheckValid（主机侧）
 │         └─ MsprofDynamicCheckValid（dynamic/pid/delay/duration 一律再查一次）
 ├─ HandleApp()                       # 新命令风格：msprof [选项] ./main args
 ├─ CheckDynProfValid / CheckMstxValid
 ├─ CheckOutputValid                  # --output 目录存在性/权限/软链接
 └─ ParamsCheck()                     # 补全 result_dir 默认值 → 返回 params_
```

频率参数的换算体现了「翻译」职责：用户给的是 Hz，内部存的是采样间隔毫秒数：

\[ \text{interval}_{ms} = \frac{1000}{\text{freq}_{Hz}} \qquad \text{interval}_{us} = \frac{1000000}{\text{freq}_{Hz}} \]

#### 4.2.3 源码精读

参数枚举与选项表（两张表一一对应，枚举值即表项的 val）：

[input_parser.h:40-118](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/include/input_parser.h#L40-L118) — `ArgsMsprofCmd` 枚举按「cmd / switch / number / host」四段组织全部参数 ID，注释里标注了默认值与范围（如 `ARGS_AIC_FREQ // 100 1-100 hz`）。

[input_parser.h:125-205](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/include/input_parser.h#L125-L205) — `LONG_OPTIONS` 表：每行是「选项名、是否带参、回填枚举值」，例如 `{"sys-period", OSAL_OPTIONAL_ARG, nullptr, ARGS_SYS_PERIOD}`。这张表就是命令行选项的唯一定义点。

解析主循环：

[input_parser.cpp:737-785](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L737-L785) — `MsprofGetOpts`：循环取选项，遇 `ARGS_HELP` 清空已记录参数并打印用法；每个选项先过平台黑名单（`PreCheckPlatform`）再进 `ProcessOptions`；循环结束后依次做动态采集参数、mstx 域、输出目录与全局参数检查。

新命令风格的精髓——msprof 选项与业务程序参数的分界：

[input_parser.cpp:793-813](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L793-L813) — `SplitApplicationArgv`：从头扫描，凡是以 `--` 开头的都算 msprof 的选项；遇到第一个非 `--` 参数，剩下的全部塞进 `params_->application`（业务程序及其参数），并停止选项计数。

按枚举区间分发的校验路由：

[input_parser.cpp:828-852](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L828-L852) — `ProcessOptions`：`cmdInfo.args[opt] = OsalGetOptArg()` 取出选项值，然后按 `opt` 落在哪个枚举区间选择校验函数族；不在任何区间则打印用法。

频率校验 + Hz→ms 换算：

[input_parser.cpp:2067-2120](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L2067-L2120) — `MsprofFreqCheckValid`：按选项分组做范围检查（sys/pid 采样 1-10Hz，aic/aiv/io 1-100Hz 等）。

[input_parser.cpp:688-735](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L688-L735) — `MsprofFreqTransferParams`：`intervalTransfer = HZ_CONVERT_MS / interval`，把 Hz 换算成毫秒写入对应 `*_sampling_interval` 字段。

平台黑名单（同一选项在 Mini/MDC/Cloud 等不同形态上可用性不同）：

[input_parser.cpp:196-217](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L196-L217) — `PreCheckPlatform`：soc 侧运行时把 host 专属选项加入黑名单；命中黑名单则报 "unrecognized option"。

帮助信息按平台动态裁剪：

[input_parser.cpp:2530-2575](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L2530-L2575) — `ArgsManager` 构造函数先放入 output/application/ascendcl 等通用参数，再由 `AddArgs()`（L2480-2504）按 `Platform::CheckIfSupport` 逐组追加平台特有参数。

#### 4.2.4 代码实践

**实践目标**：把一条文档中的真实命令逐个选项映射到源码解析位置。

**操作步骤**：

1. 打开用户文档 [general_collect_commands.md:39](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L39)，取示例命令：

   ```bash
   msprof --output=/home/projects/output /home/projects/main parameter1 parameter2
   ```

2. 对照下表在源码中逐项定位（均已按当前 HEAD 验证行号）：

   | 命令行片段 | 解析位置 | 说明 |
   | --- | --- | --- |
   | `--output=/home/projects/output` | 枚举 `ARGS_OUTPUT`（input_parser.h L42）；值校验 `CheckOutputValid`（input_parser.cpp L1327-1373） | 检查路径长度/非法字符/软链接、创建目录、校验写权限，最后 `CanonicalizePath` 写入 `params_->result_dir` |
   | `/home/projects/main` | `SplitApplicationArgv`（L793-813）识别为业务程序起点 | 非选项参数起，后续全部进 `params_->application` |
   | `parameter1 parameter2` | 同上，随 application 一起收集 | `HandleApp()`（L818-826）取 basename 作 `app` |
   | （整体） | `ParamsCheck`（L919-970） | 未显式指定 output 时按 `ASCEND_WORK_PATH` 环境变量或当前目录兜底 |
3. 在有昇腾设备的环境实际运行这条命令，观察终端打印（待本地验证）。

**预期结果**：能说出「`--output` 的合法性检查发生在 CheckOutputValid，业务程序与 msprof 选项的分界由 SplitApplicationArgv 判定」这两句话，并能在 30 秒内从命令行选项名查到 LONG_OPTIONS 表项与校验函数。

#### 4.2.5 小练习与答案

**练习 1**：用户传 `--aic-freq=200`（超过 100Hz 上限），错误在哪一层被拦下？
**答案**：`ProcessOptions` 判断 `opt` 落在 `ARGS_AIC_FREQ..ARGS_EXPORT_MODEL_ID` 区间，转给 `MsprofFreqCheckValid`（input_parser.cpp L2088-2093），`CheckArgRange(cmdInfo, opt, 1, 100)` 报 "invalid int value"，`MsprofGetOpts` 返回 nullptr，main 直接失败退出。

**练习 2**：为什么 `--help` 的帮助输出在不同机器上可能不一样？
**答案**：帮助列表由 `ArgsManager` 构造时按 `Platform::instance()->CheckIfSupport(...)` 和 `ConfigManager::instance()->GetPlatformType()` 动态拼装（如 AddAivArgs 只在 MDC 平台追加 aiv 参数，AddDvvpArgs 只在支持 DVPP 的平台追加），所以不同平台能力集不同，可见选项也不同。

**练习 3**：`msprof ./main arg1` 与 `msprof --application="./main arg1"` 两种写法在源码层如何统一？
**答案**：前者的 `./main arg1` 被 `SplitApplicationArgv` 收进 `params_->application`，后者经 `CheckAppValid` 拆出程序与参数；`HandleApp()`（L818-826）统一裁决——若 `--application` 已填（`params_->app` 非空）则清空 application 以其优先，否则从 application[0] 取 basename 填入 `app`。

---

### 4.3 msprof_manager.cpp + running_mode.cpp：运行模式的生成与执行

#### 4.3.1 概念说明

`MsprofManager` 是「路由器」：它不做具体工作，只根据 ProfileParams 判断这次调用是哪种运行模式，生成对应的 `RunningMode` 子类对象，再委托它执行。六种模式分两组：

- **采集组**（`GenerateCollectRunningMode`）：`AppMode`（跟着业务程序采）、`SystemMode`（系统级采集，不依赖业务程序）。
- **分析组**（`GenerateAnalyzeRunningMode`）：`ParseMode` / `QueryMode` / `ExportMode` / `AnalyzeMode`（对已有数据目录做离线处理）。

`RunningMode` 基类用三个参数集合约束每种模式的合法输入：`whiteSet_`（白名单：允许）、`neccessarySet_`（必需）、`blackSet_`（禁止）。这是典型的「状态 + 规则集」设计，替代大量 if-else 组合判断。

#### 4.3.2 核心流程

```text
MsprofManager::Init(params)
 ├─ GenerateRunningMode()
 │    ├─ GenerateCollectRunningMode()
 │    │    ├─ app 非空 或 动态采集开启 → AppMode
 │    │    ├─ devices 非空             → SystemMode（sys-devices）
 │    │    ├─ host_sys 非空            → SystemMode（host-sys）
 │    │    └─ hostSysUsage 非空        → SystemMode（host-sys-usage）
 │    └─ GenerateAnalyzeRunningMode()
 │         ├─ parseSwitch==on   → ParseMode
 │         ├─ querySwitch==on   → QueryMode
 │         ├─ exportSwitch==on  → ExportMode
 │         └─ analyzeSwitch==on → AnalyzeMode
 │    （都未命中 → 打印帮助，失败）
 └─ ParamsCheck() → rMode_->ModeParamsCheck()   # 模式级规则集检查

MsProcessCmd() → rMode_->RunModeTasks()          # 多态执行
```

`RunModeTasks` 的通用骨架（以 AppMode 为例）：启动采集 → 等待结束 → `UpdateOutputDirInfo` 找回结果目录 → `CheckAnalysisEnv` 定位 Python 分析脚本 → 逐目录执行 `StartExportTask` / `StartQueryTask`。

#### 4.3.3 源码精读

模式生成的优先级链：

[msprof_manager.cpp:116-151](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_manager.cpp#L116-L151) — `GenerateCollectRunningMode`：app > devices > host_sys > hostSysUsage 顺序试探，命中即用 `MSVP_MAKE_SHARED2` 创建对应模式；helper（协处理器）侧系统采集直接拒绝。

[msprof_manager.cpp:153-176](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_manager.cpp#L153-L176) — `GenerateAnalyzeRunningMode`：parse > query > export > analyze 四个开关依次判断。

执行入口：

[msprof_manager.cpp:74-81](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_manager.cpp#L74-L81) — `MsProcessCmd` 只有一行实质代码：`rMode_->RunModeTasks()`，全部多态分发。

规则集检查机制：

[running_mode.cpp:78-110](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L78-L110) — `CheckForbiddenParams`（used ∩ black 非空即报错）与 `CheckNeccessaryParams`（necessary − used 非空即报错），用集合运算表达参数互斥/依赖关系。

[running_mode.cpp:1425-1431](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L1425-L1431) — ParseMode 的三个集合：必需 `--output` 与 `--parse`，禁止 `--query/--export/--analyze/--rule/--clear`。QueryMode（L1479-1485）、ExportMode（L1529-1536）、AnalyzeMode（L1587-1593）同构，只是集合内容不同。

SystemMode 的必需参数：

[running_mode.cpp:837-853](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L837-L853) — SystemMode 构造函数：`neccessarySet_ = { ARGS_SYS_PERIOD }`——系统采集必须给 `--sys-period`。

AppMode 的完整任务链（采集 → 找结果 → 自动分析）：

[running_mode.cpp:656-711](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L656-L711) — `AppMode::RunModeTasks`：补默认参数 → 启动业务子进程并等待 → 从记录文件找回结果目录 → 逐目录 Export + Query；找不到数据时提示检查业务是否调用了 aclInit/GEInitialize。

C++ 与 Python 分析侧的唯一接缝（u4-l1 讲过的路径契约在这里落地）：

[running_mode.cpp:535-572](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L535-L572) — `CheckAnalysisEnv`：以自身可执行文件位置为基准，拼出 `profiler_tool/analysis/msprof/msprof.py` 并校验存在与可执行；之后所有分析任务都以 `<pythonPath> msprof.py <子命令> -dir=<结果目录>` 的子进程方式执行（如 `StartParseTask` L275-314 拼 `import` 子命令、`StartExportTask` L434-475 拼 `export` 子命令）。

#### 4.3.4 代码实践

**实践目标**：给六种运行模式各构造一条「最小触发命令」并验证模式判定顺序。

**操作步骤**：

1. 阅读 `GenerateCollectRunningMode` 与 `GenerateAnalyzeRunningMode`，填写下表：

   | 目标模式 | 最小命令（示例） | 判定依据（源码行） |
   | --- | --- | --- |
   | AppMode | `msprof ./main` | app 非空（msprof_manager.cpp L118-121） |
   | SystemMode(sys-devices) | `msprof --sys-devices=0 --sys-period=10` | devices 非空（L123-130） |
   | SystemMode(host-sys) | `msprof --host-sys=cpu --sys-period=10 --host-sys-pid=1234` | host_sys 非空（L132-139） |
   | ParseMode | `msprof --output=<dir> --parse=on` | parseSwitch（L155-158） |
   | QueryMode | `msprof --output=<dir> --query=on` | querySwitch（L160-163） |
   | ExportMode | `msprof --output=<dir> --export=on` | exportSwitch（L165-168） |
   | AnalyzeMode | `msprof --output=<dir> --analyze=on` | analyzeSwitch（L170-173） |
2. 思考验证题：`msprof --output=<dir> --parse=on --query=on` 会进哪个模式？
3. 在有环境时实际运行验证（待本地验证）。

**预期结果**：第 2 题答案是 ParseMode——`GenerateAnalyzeRunningMode` 按固定顺序短路返回，parse 先于 query 判断；但 ParseMode 的 `blackSet_` 含 `ARGS_QUERY`，`CheckForbiddenParams` 会报 "The argument --query is forbidden when --parse is not empty"，进程失败退出。这也说明「模式判定」与「模式内合法性」是两道独立的闸门。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AppMode 的 `whiteSet_` 几乎包含全部参数，而 ParseMode 只允许 3 个？
**答案**：AppMode 是完整采集流程，所有采集开关都合法，只需 `OutputUselessParams` 对白名单外参数给告警；ParseMode 是对既有数据的离线解析，采集开关全部无意义，所以用 `blackSet_` 硬禁止、`neccessarySet_` 强制 `--output` 与 `--parse`，防止用户误以为设置了采集参数。

**练习 2**：SystemMode 为什么把 `ARGS_SYS_PERIOD` 放进必需集？
**答案**：系统采集没有业务进程来自然界定结束时机，必须由 `--sys-period` 指定采样时长；`SystemMode::RunModeTasks` 里 `WaitSysTask` 按该值循环等待（running_mode.cpp L1152-1165），缺了它采集永远不会结束。

**练习 3**：msprof 进程结束后自动输出的 "Data is saved in xxx" 来自哪里？
**答案**：main 末尾的 `PrintOutPutDir()`（msprof_bin.cpp L51-59）读取 `MsprofManager::instance()->rMode_->jobResultDir_`，而 jobResultDir_ 由各模式的 `UpdateOutputDirInfo` 在任务结束后从结果目录记录文件或参数中回填。

---

### 4.4 msprof_task.cpp：单设备采集任务的生命周期

#### 4.4.1 概念说明

运行模式解决「做什么」，任务（Task）解决「在某一台设备上怎么做」。`MsprofTask` 是基类，封装了采集任务的通用节奏；两个子类对应两种数据通道：

- `ProfSocTask`：宿主侧直接创建 JobAdapter 采集（走 SOC 通道）。
- `ProfRpcTask`：通过 devmgr RPC 连接设备侧进程采集（需要 `pfDevMgrInit` 建链）。

SystemMode 里看到的 `StartHostTask`/`StartDeviceTask`（running_mode.cpp L1060-1137）分别创建这两种任务，并登记进 `taskMap_`（按 job_id 索引）与 `taskList_`（保序，用于逆序停止）。

#### 4.4.2 核心流程

单个任务的执行节奏（`MsprofTask::Run`）：

```text
Run(errorContext)
 ├─ CreateCollectionTimeInfo(start)   # 写 start_info 文件（记录采集开始时间）
 ├─ jobAdapter_->StartProf(params_)   # 下发采集配置，开始采集
 ├─ WaitStopReplay()                  # 条件变量阻塞，等 Stop() 信号
 ├─ jobAdapter_->StopProf()           # 停止采集
 ├─ GetHostAndDeviceInfo()            # 生成 info.json（设备/版本信息）
 └─ CreateCollectionTimeInfo(end)     # 写 end_info 文件
```

停止路径：`MsprofManager::NotifyStop()` → 置 `isQuit_` → SystemMode::StopTask 逆序对每个任务 `Stop()`（唤醒条件变量）+ `Wait()`（Join 线程并 Flush 落盘）。

#### 4.4.3 源码精读

任务主循环：

[msprof_task.cpp:67-94](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_task.cpp#L67-L94) — `MsprofTask::Run`：do-while(0) 串联五个步骤，任一步失败即跳出；核心是 `WaitStopReplay()` 条件变量等待——采集线程启动后就安静阻塞，直到外部调用 `Stop()` 唤醒。

停止与落盘：

[msprof_task.cpp:96-125](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_task.cpp#L96-L125) — `Stop()` 只做 `PostStopReplay()` 唤醒；`Wait()` Join 线程后调 `WriteDone()`，从 `UploaderMgr` 取出该 job 的 uploader，`Flush()` 缓冲数据并由 transport 写 done 标记文件——保证分析侧能看到「这个 job 的数据已写完」。

两个子类的初始化差异：

[msprof_task.cpp:255-265](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_task.cpp#L255-L265) — `ProfSocTask::Init`：用 `JobSocFactory` 按设备号创建 JobAdapter 即完成。

[msprof_task.cpp:282-305](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_task.cpp#L282-L305) — `ProfRpcTask::Init`：先 `LoadDevMgrAPI` 动态加载 devmgr 接口，校验 `profiling_period > 0`，再 `pfDevMgrInit` 与设备建链，最后创建 `JobDeviceRpc` 适配器；`PROFILING_NOTSUPPORT` 会被单独放行（对应 main 里「容器/虚拟实例不支持系统采集」的提示）。

任务的创建现场（连接 4.3 与 4.4）：

[running_mode.cpp:1060-1101](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L1060-L1101) — `SystemMode::StartHostTask`：生成 host 侧参数副本 → 写 sample.json → 创建 FileTransport/Uploader → new `ProfSocTask` → `Init()` + `Start()` → 登记进 taskMap_/taskList_。

#### 4.4.4 代码实践

**实践目标**：追踪一次系统采集中「Ctrl+C → 数据完整落盘」的调用链。

**操作步骤**：

1. 从 msprof_bin.cpp L122 的 `signal(SIGINT, StopProfiling)` 出发。
2. 依次定位并抄下每个函数所在文件与行号：
   - `MsprofManager::NotifyStop`（msprof_manager.cpp L66-72）→ 置 `rMode_->isQuit_ = true`
   - `SystemMode::StopTask`（running_mode.cpp L1139-1150）→ 逆序 `Stop()` + `Wait()`
   - `MsprofTask::Stop` / `Wait`（msprof_task.cpp L96-110）→ 唤醒条件变量、Join 线程
   - `MsprofTask::WriteDone`（msprof_task.cpp L112-125）→ Flush + WriteDone 标记
3. 注意 `RunningMode::WaitRunningProcess`（running_mode.cpp L485-523）里 `if (isQuit_) { StopNoWait(); }` 的轮询点——等待业务进程期间每秒检查一次退出标志。

**需要观察的现象 / 预期结果**：你应该得到一条 6 跳的链路图；结论是 Ctrl+C 不会丢弃已采集数据——停止路径先唤醒任务线程、Flush 缓冲、写 done 标记，再退出 msprof。若在有设备环境运行 `msprof --sys-devices=0 --sys-period=10 --sys-profiling=on` 并中途 Ctrl+C，可在输出目录看到带 `.done` 标记的文件（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`ProfSocTask` 与 `ProfRpcTask` 的本质区别是什么？
**答案**：数据通道不同。ProfSocTask 在宿主本地经 JobSocFactory 创建适配器直接采集；ProfRpcTask 依赖动态加载的 devmgr API（`pfDevMgrInit`）与设备侧建立 RPC 连接，由远端执行采集、本机接收数据，因此它额外要求 `profiling_period > 0` 并需要等数据同步信号（`WaitSyncDataCtrl`）。

**练习 2**：`start_info` / `end_info` / `info.json` 这些控制文件是谁写的、给谁看的？
**答案**：都由 MsprofTask 在 Run 过程中经 `UploaderMgr::UploadCtrlFileData` 写入结果目录（msprof_task.cpp L155-193、L195-230），分别记录采集起止时间（含 clockMonotonicRaw，用于多设备时间对齐）与设备版本信息；消费者是 Python 分析侧——分析脚本靠它们还原时间轴与设备上下文。

**练习 3**：为什么 `SystemMode::StopTask` 要逆序遍历 taskList_？
**答案**：任务按启动顺序 append 进 taskList_，逆序停止模拟「后启动的先停」的栈式收尾，先停较晚建立的采集链路，再停最早的基础任务，降低停止过程中数据通道互相等待导致死锁的风险。

---

## 5. 综合实践

把本讲四个模块串成一条完整的「一次 msprof 调用」追踪报告。选下面这条系统采集命令作为标本：

```bash
msprof --output=/tmp/prof_out --sys-devices=0 --sys-period=10 --sys-profiling=on
```

要求产出一份 Markdown 报告，包含三张表：

1. **启动顺序表**：main 的 9 个阶段，每行给出函数名、文件:行号、本命令下该阶段的实际效果（提示：本命令无业务程序，`MsopprofProcess` 返回失败继续主流程；`MsprofGetOpts` 依次解析 4 个选项，其中 `--sys-period=10` 走 `MsprofFreqCheckValid` → `CheckSysPeriodValid`（input_parser.cpp L1839-1865）→ `MsprofFreqUpdateParams` 写入 `profiling_period`）。
2. **模式判定表**：本命令命中 `GenerateCollectRunningMode` 的哪个分支（devices 非空 → SystemMode），并列出 SystemMode 的 `ModeParamsCheck` 五步检查（CheckNeccessaryParams / DataWillBeCollected / OutputUselessParams / CheckHostSysParams / HandleProfilingParams）各自的输入与结果。
3. **任务链路表**：从 `StartSysTask`（running_mode.cpp L1397-1423）到 `ProfSocTask` 的 Init/Run/Stop/Wait，标注每一步产生的文件（PROF_XXX 目录、output 记录文件、sample.json、start_info 等）。

最后回答一个开放问题：如果把 `--sys-profiling=on` 去掉，命令还能跑吗？会走到哪一步失败？（提示：`SystemMode::DataWillBeCollected`，running_mode.cpp L920-937。）此实践全部可纸面完成；有昇腾设备时可实际运行对照（待本地验证）。

## 6. 本讲小结

- `main()` 是薄入口：环境保存 → 驱动/平台两级初始化 → msopprof 分流 → 参数解析 → MsprofManager 初始化 → 注册 SIGINT → 委托运行模式执行，任何一步失败立即非 0 退出。
- 命令行解析是表驱动的：`LONG_OPTIONS` 表 + `ArgsMsprofCmd` 枚举是全部选项的唯一定义点，`ProcessOptions` 按枚举区间路由到四族校验器；校验与「Hz→毫秒」翻译在同一层完成，结果统一收敛进 ProfileParams。
- 运行模式是策略模式 + 规则集：MsprofManager 按 app > devices > host_sys > hostSysUsage > parse > query > export > analyze 的优先级生成六种 RunningMode 之一；每种模式用白名单/必需/禁止三个集合声明自己的合法参数空间。
- 分析任务是子进程：所有离线分析（parse/query/export/analyze）都以 `<python> profiler_tool/analysis/msprof/msprof.py <子命令> -dir=<目录>` 方式执行，C++ 与 Python 侧仅靠路径契约衔接。
- 采集任务是「启动-阻塞-唤醒-落盘」节奏：MsprofTask::Run 在 StartProf 后阻塞在条件变量上，Stop 链路（NotifyStop → StopTask → Stop/Wait → WriteDone）保证 Ctrl+C 时缓冲数据 Flush 完整并写 done 标记。

## 7. 下一步学习建议

下一讲（u4-l3）将进入 `profapi` 插件体系：本讲 `MsprofTask` 里的 `jobAdapter_->StartProf(params_)` 之下，正是 prof_plugin_manager 管理的 acl/runtime/tx 等数据源插件在真正采集。建议先自行浏览 `src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h`，带着「ProfileParams 里的开关如何变成具体插件的订阅」这个问题进入下一讲。
