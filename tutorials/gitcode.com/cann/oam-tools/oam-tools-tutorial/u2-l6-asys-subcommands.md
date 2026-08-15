# asys 业务子命令：launch、info、health、diagnose

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `AsysLaunch` 如何"复跑用户业务 + 全程采集"的完整机制（环境变量注入、子进程托管、Ctrl+C 信号处理、采集与清理）。
2. 说出 `asys info` 三种 run_mode（hardware/software/status）各自查询什么、数据从哪里来。
3. 理解 `AsysHealth` 的健康状态聚合逻辑（Critical > Alarm > Warning > Healthy）和"health 命令打印 / collect 复用落盘"的双行为设计。
4. 理解 `AsysDiagnose` 的 component（环境检测，转调 msaicerr）与硬件检测（hbm/cpu/stress/aicore_stl，走芯片 handler）两条分支。
5. 能独立追踪任意一个子命令从 `cmd_parser.py` 的 Arg 定义 → `ParamDict` → `EXECUTE_CMD_FUNC` 分发 → 实现类 `run()` 的完整调用链。

## 2. 前置知识

本讲假设你已学完 u2-l1（asys 入口主流程）和 u2-l5（collect 子系统）。在此基础上补充两个概念：

- **run_mode（运行模式）**：asys 的多个子命令都有一个"模式"参数（如 `info` 的 `--run_mode`、`diagnose` 的 `-r`）。它不是全局概念，而是每个子命令自己的分支开关，解析后统一存进 `ParamDict`，实现类在 `run()` 里读取并分派。
- **纯展示命令 vs 落盘命令**：u2-l1 讲过，info/diagnose/health 这类命令直接把结果打印到终端（并提前 `close_log()` 关掉冗余日志），不建输出目录；而 collect/launch/analyze 会建 `asys_output_<时间戳>` 目录落盘。本讲会看到一个有趣的交叉点：`AsysHealth` 同时服务两种场景——作为 `health` 命令时打印屏幕，被 collect/launch 复用时落盘保存。
- **EP 与 RC 环境**：EP（端侧/常规主机+Device 部署）与 RC（Robotic Compute，机驾类嵌入式环境）两类运行环境。RC 下日志目录结构、可用子命令都不同（asys.py 主流程里 RC 仅允许 launch 和不带 run_mode 的 collect）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/asys/asys.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py) | 入口：`EXECUTE_CMD_FUNC` 字典把 4 个子命令名映射到本讲的 4 个实现类 |
| [src/asys/cmdline/cmd_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py) | `Command` 枚举声明每个子命令接受哪些 Arg（本讲调用链的起点） |
| [src/asys/launch/asys_launch.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py) | launch 子命令：复跑业务并采集 |
| [src/asys/info/asys_info.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py) | info 子命令：软硬件信息与 Device 状态展示 |
| [src/asys/health/asys_health.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py) | health 子命令：设备健康检查 |
| [src/asys/diagnose/asys_diagnose.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py) | diagnose 子命令：组件检测与硬件检测 |

四个实现类都遵守 u2-l1 总结的隐式接口约定：**无参构造 + `run()` 方法**。`asys.py` 的分发代码只是 `obj = EXECUTE_CMD_FUNC.get(command)()` 然后 `obj.run()`，完全不关心子命令内部逻辑。

## 4. 核心概念与源码讲解

先看四个子命令在命令行层的"参数面"。[cmd_parser.py:210-257](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L210-L257) 的 `Command` 枚举声明了它们各自的参数集：

| 子命令 | 参数 | 一句话职责 |
| --- | --- | --- |
| launch | `task`（必选）、`output`、`tar` | 执行 task 命令并边跑边采集 |
| info | `run_mode`、`device` | 收集主机与 Device 的软硬件信息 |
| health | `device` | 检查设备健康状态 |
| diagnose | `run_mode`、`device`、`timeout`、`output` | 硬件/组件诊断 |

对应的分发字典在 [asys.py:61-67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L61-L67)：`consts.launch_cmd → AsysLaunch`、`consts.info_cmd → AsysInfo`、`consts.diagnose_cmd → AsysDiagnose`、`consts.health_cmd → AsysHealth`。下面逐个深入。

### 4.1 launch：复跑业务并采集信息

#### 4.1.1 概念说明

`asys launch "<你的业务命令>"` 解决的问题是：**故障只在跑业务时出现**。它把用户的业务命令作为子进程托管起来，在启动前注入一组利于排障的环境变量（打开 GE 图 dump、提升日志配额、把 CANN 日志重定向到采集目录），业务结束后立刻复用 `AsysCollect`（u2-l5 讲过的采集总调度）把现场全部收走，最后清掉中间目录。

#### 4.1.2 核心流程

```text
run()
 ├─ launch()
 │   ├─ prepare_for_launch()      ① 注入 9 个环境变量 + 创建 2 个目录
 │   ├─ execute_task()            ② Popen 托管业务命令，守护线程转圈显示进度
 │   └─ task_out_collect()        ③ 保存 user_cmd 与 screen.txt，再调 AsysCollect.collect()
 └─ clean_work()                  ④ 删除 npu_collect_intermediates/ 与 export_tmp/ 中间目录
```

两个值得注意的设计：

- **进程组信号托管**：子进程用 `preexec_fn=os.setsid` 独立成组；用户按 Ctrl+C 时，自定义 handler 先 `SIGTERM` 杀掉整个业务进程组，再恢复默认 SIGINT 并给自己补一发信号，保证 asys 与业务一起干净退出。
- **中间目录与产物目录分离**：`NPU_COLLECT_PATH`（npu_collect_intermediates/）是驱动层日志的临时落脚点，`clean_work()` 在最后整体删除，最终 .tar.gz 里只剩规整后的 `dfx/` 产物。

#### 4.1.3 源码精读

**① 环境变量注入清单** —— [asys_launch.py:36-52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L36-L52)：构造函数从 `ParamDict().asys_output_timestamp_dir` 取输出目录、从 `get_ini()` 取用户配置，拼出 9 个环境变量。其中 `DUMP_GE_GRAPH`/`DUMP_GRAPH_LEVEL` 控制图 dump 开关与级别，`ASCEND_HOST_LOG_FILE_NUM` 硬编码为 1000（放宽日志文件数配额），三个路径类变量全部指向输出目录下的子目录——这是"排障配置一站式注入"的核心。

**② 目录准备** —— [asys_launch.py:54-69](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L54-L69)：`prepare_for_launch()` 逐个写入 `os.environ`，然后创建 `NPU_COLLECT_PATH` 与 `ASCEND_WORK_PATH`（atrace 日志落脚点），任一失败返回 `RetCode.FAILED`。

**③ 子进程托管与信号处理** —— [asys_launch.py:71-97](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L71-L97)：`execute_task()` 注册 Ctrl+C handler；`subprocess.Popen(shell=True, stdout=PIPE, stderr=STDOUT, preexec_fn=os.setsid)` 以新进程组启动业务并合并捕获输出；起一个 daemon 线程跑 `wait_view()` 进度动画（与 u2-l5 collect 的 `finish_flag` 手法一致）；`ParamDict().set_task_pid(pro.pid)` 把子进程 PID 登记进全局单例（供 stacktrace 等模块定位目标进程）；`communicate()` 收尾后置位 `finish_flag` 让进度线程退出，按 returncode 分级记录日志。

**④ 采集与复用 AsysCollect** —— [asys_launch.py:99-118](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L99-L118)：`task_out_collect()` 先按 EP/RC 环境决定日志子目录，把用户命令原文写进 `dfx/log/.../user_cmd`、屏幕输出写进 `screen.txt`；然后 `AsysCollect().collect()` 直接复用 u2-l5 讲过的七类采集总调度——launch 并没有自己的一套采集逻辑。

**⑤ 主入口与清理** —— [asys_launch.py:120-145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L120-L145)：`launch()` 串起三步；`run()` 是 `EXECUTE_CMD_FUNC` 约定入口，最后 `clean_work()` 删除两个中间目录（`npu_collect_intermediates/` 与 msnpureport 导出用的 `export_tmp/`）。

#### 4.1.4 代码实践

**实践目标**：跑通一次最小 launch，对照源码验证 5 步流程。

**操作步骤**（需有昇腾设备环境，先 `source <CANN安装路径>/set_env.sh`）：

1. 在任意目录执行：`asys launch "python3 -c 'print(1)'"`。
2. 等待结束，查看生成的 `asys_output_<时间戳>` 目录：确认 `dfx/log/` 下有 `user_cmd`（内容应是你的命令原文）和 `screen.txt`（内容应是 `1`）。
3. 确认目录里**没有** `npu_collect_intermediates/`（被 clean_work 删掉了）。
4. 再执行 `asys launch "sleep 30"`，在 30 秒内按 Ctrl+C，观察 asys 与 sleep 是否一起退出、输出目录里是否仍留下了已采集内容。

**需要观察的现象**：步骤 2 中两个文件的存在证明 `task_out_collect()` 先于异常发生已落盘；步骤 4 证明进程组信号托管生效。

**预期结果**：正常路径得到完整 `dfx/` 产物；Ctrl+C 路径下 sleep 进程随之终止。无设备环境下的纸面替代：对照 [asys_launch.py:71-81](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L71-L81) 逐行写出 Popen 每个 kwargs 的作用。**待本地验证**（本讲义写作环境无昇腾设备）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `execute_task()` 要用 `preexec_fn=os.setsid`，而不是直接 Popen？
**答案**：`os.setsid` 让业务命令（含 shell=True 展开出的整棵子进程树）进入独立进程组。这样 Ctrl+C 的自定义 handler 才能用 `os.killpg(os.getpgid(pro.pid), SIGTERM)` 一次性杀掉整棵树，避免 shell 已死、孙进程残留继续占用 Device。

**练习 2**：launch 的采集能力是自己的吗？
**答案**：不是。`task_out_collect()` 里只额外保存了 `user_cmd` 与 `screen.txt` 两个文件，其余全部七类采集（日志、图、trace、状态、健康等）直接实例化 `AsysCollect`（u2-l5）复用，体现"采集能力单一出口"的设计。

**练习 3**：`ASCEND_PROCESS_LOG_PATH` 为什么指向输出目录内部而不是默认路径？
**答案**：把 CANN 运行时日志直接重定向进 `npu_collect_intermediates/task_launch_host_log`，业务一结束日志就在采集目录里，避免事后从系统日志目录（如 `~/ascend/log`）翻找，也让最终 tar 包自带完整现场。

### 4.2 info：软硬件信息与 Device 状态展示

#### 4.2.1 概念说明

`asys info` 是"体检报告生成器"，三种 run_mode 对应三类信息：

| run_mode | 内容 | 数据来源 |
| --- | --- | --- |
| hardware | 主机 CPU/内存/磁盘、Device 核数/架构、PCIe 插卡计数 | shell 命令（lscpu、df、lspci…）+ DeviceInfo 封装 |
| software | 内核/OS 版本、CANN 各包版本（EP）或驱动/固件/runtime 版本（RC）、依赖包、环境变量 | version.info 文件 + shell 命令 |
| status | 单设备实时状态：功耗、温度、健康、CPU/AICore/总线/内存四张表 | DeviceInfo 及芯片 handler（ctypes 调驱动 so，见 u2-l3/u2-l4） |

#### 4.2.2 核心流程

```text
run()
 ├─ 从 ParamDict 取 run_mode / device_id（缺省 0）
 └─ run_info(run_mode, device_id)   ← 带 @timeout_decorator 整体超时保护
     ├─ "hardware" → get_hardware_info()   打印 Host/Device/PCIe 三组表
     ├─ "software" → get_software_info()   打印 Host/Device 版本表
     └─ "status"   → get_status_info(id)   打印单设备五组表
```

表格数据统一走 `view.generate_report()` 渲染。另一个入口 `write_info()`（不接命令行参数，被 collect/launch 内部复用）则 `write_file=True` 落盘 `hardware_info.txt`/`software_info.txt`/`status_info.txt`——与 AsysHealth 一样是"一套实现、两种出口"。

status 模式还有一个配置过滤机制：`__check_support_read_option()` 依据配置表（`asys config` 管理的那些选项，u2-l8 会讲）里每个选项的 GET 白名单 + 当前芯片型号，决定某一行状态要不要显示——同一份代码在不同芯片上输出的行集不同。

#### 4.2.3 源码精读

**① 三模式分派与超时保护** —— [asys_info.py:402-419](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L402-L419)：`run_info()` 按 run_mode 三路分派；整个查询包在 `@timeout_decorator(GET_DEVICES_INFO_TIMEOUT)` 里——设备异常时驱动接口可能长时间不返回，超时后 `run()` 捕获 TimeoutError 并提示用户检查硬件故障，而不是无限挂死。

**② hardware：shell 命令即数据源** —— [asys_info.py:77-91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L77-L91)：主机信息完全靠拼接 shell 命令（`lscpu`、`cat /proc/cpuinfo`、`df -k /` 等）经 `run_command` 取回；[asys_info.py:58-75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L58-L75) 的 PCIe 统计用 `lspci | grep -E 'd100|d500|d801|d802|d803|d806'`（昇腾 PCI 设备 ID）过滤，"正常数 = 总数 − rev ff（异常）数"是简单算术推导。Device 侧核数见 [asys_info.py:92-119](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L92-L119)：`get_device_info_loop` 逐卡循环 + 总数按 `单卡数 × 卡数` 展开。

**③ software：EP/RC 双分支读版本** —— [asys_info.py:189-211](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L189-L211)：EP 环境从 `<ascend_home>/cann/share/info/<包名>/version.info` 逐包读版本；RC 环境则读 `/var/davinci/driver/version.info`、`/fw/version.info` 与 runtime 的 version.info（带 `latest` 软链失败后回退固定路径的两级尝试）。

**④ status：四张状态表与配置过滤** —— [asys_info.py:349-400](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L349-L400)：`get_status_info()` 先写公共行（芯片名、功耗、温度、健康），再依次调 `__add_status_cpu_info` / `__add_status_aic_info` / `__add_status_bus_info` / `__add_status_memory_info` 填 CPU/AI Core/总线/内存四张表，空表删除后渲染。注意 [asys_info.py:273](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L273) 与 [asys_info.py:298](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L298)：AI Core 与总线信息刻意用 `get_device(device_id)`（u2-l4 的芯片工厂，拿到的是具体型号 handler）而非通用 `DeviceInfo`——精度更高的查询走芯片特化实现，查不到再退回通用接口，这正是芯片适配层"按需覆写、失败退化"哲学的消费现场。

**⑤ 落盘出口** —— [asys_info.py:421-425](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L421-L425)：`write_info()` 三个 `write_file=True`，status 逐卡追加写同一个 `status_info.txt`。

#### 4.2.4 代码实践

**实践目标**：验证 hardware 模式"shell 命令即数据源"，不依赖 Device 也能看懂一半输出。

**操作步骤**：

1. 在有设备的环境执行 `asys info`（默认 hardware）与 `asys info -r software`、`asys info -r status -d 0`，对比三张输出表。
2. 把 [asys_info.py:79-85](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L79-L85) 里的 5 条主机查询命令逐条复制到终端手工执行，核对与 asys 输出的 Host Info 表是否一致。
3. 无设备环境：只做步骤 2，同样能验证主机侧逻辑。

**需要观察的现象**：Host Info 五行的值与手工执行命令的输出一一对应。

**预期结果**：hardware 表 = 5 条 shell 命令结果 + DeviceInfo 查询结果的拼接。**待本地验证**（Device 部分需真实硬件）。

#### 4.2.5 小练习与答案

**练习 1**：`asys info -r status` 为什么整个包一层 `@timeout_decorator`？
**答案**：status 查询直调驱动 so（ctypes），设备异常（如 PCIe 挂死）时调用可能永不返回；超时装饰器让 asys 以明确报错退出（"Please check for malfunctions"），把"挂死"转成"可诊断的超时"。

**练习 2**：为什么 status 里 AI Core 信息用 `get_device(device_id)` 而 CPU 计数用 `self.device_info`？
**答案**：`get_device()` 是 u2-l4 的芯片工厂，返回具体型号 handler（如 Ascend950Handler 覆写了更高精度的电压/频率查询）；CPU 计数各芯片差异小，通用 `DeviceInfo` 足够。这是"特化查询走 handler、通用查询走基类"的分工。

**练习 3**：`__software_set_dep`（读 dependent_package.csv 逐项执行命令）为什么只在 write_file=True 时调用？
**答案**：依赖包清单信息量大但排障价值主要在落盘归档时；交互式查看时省略可以让终端输出聚焦版本信息，避免刷屏（参见 [asys_info.py:228-230](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/info/asys_info.py#L228-L230) 的条件分支）。

### 4.3 health：设备健康检查

#### 4.3.1 概念说明

`asys health` 回答一个问题：**这台 Device 现在健康吗？** 它逐设备查两个东西——健康状态（Healthy/Warning/Alarm/Critical/Unknown）与错误码列表（`[error_code, error_msg]` 对），然后视调用者是谁选择输出方式：作为独立 `health` 命令时打印表格；被 collect/launch 复用时写入 `health_result.txt`。

#### 4.3.2 核心流程

```text
run()
 ├─ get_device_count() 取设备数，None 则失败返回
 ├─ device_id = ParamDict().get_arg("device_id")
 │    False（未传 -d）→ 检查所有设备；否则只查指定卡
 ├─ run_health_check(devices) → {device_id: [status, [[code, msg], ...]]}
 └─ 按 ParamDict().get_command() 分流：
      health 命令 → _print_screen()（无 -d 显示汇总+逐卡简表；有 -d 显示错误码明细前 5 条）
      其他命令   → _save_file()（逐卡写全量错误码进 health_result.txt）
```

多卡汇总规则（`_get_highest_status`）：任一卡 Unknown → 整机 Unknown；否则取最严重等级，严重度从高到低为 Critical > Alarm > Warning > Healthy。

#### 4.3.3 源码精读

**① 最高严重度聚合** —— [asys_health.py:35-53](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py#L35-L53)：按 Unknown → Critical → Alarm → Warning → Healthy 的短路顺序逐级判断，任何一级命中即返回；全不命中兜底返回 Unknown。"有 Unknown 直接 Unknown"是保守设计——信息不全时宁可不让用户误判为健康。

**② 数据获取** —— [asys_health.py:75-84](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py#L75-L84)：`run_health_check()` 注释写了 "Multi-thread parallel execution"，当前实现是顺序循环逐卡调 `get_device_health()` 与 `get_device_errorcode()`（u2-l3 讲过的 device.py ctypes 封装，失败退化为 NOT_SUPPORT 占位值）。

**③ 双出口分流** —— [asys_health.py:107-128](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py#L107-L128)：`run()` 里 `ParamDict().get_command() == consts.health_cmd` 是关键判断——同一个类被 EXECUTE_CMD_FUNC 和 collect 子系统两处实例化，靠"当前是哪个命令"决定打印还是落盘。`_print_screen()`（[asys_health.py:86-105](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py#L86-L105)）在带 `-d` 时最多展示 5 条错误码并以 `......` 截断，全量明细留给落盘文件。

#### 4.3.4 代码实践

**实践目标**：观察 `-d` 有无对 health 输出形态的影响。

**操作步骤**（需设备环境）：

1. 执行 `asys health`，记录输出：应是一张汇总表（Overall Health 取最高严重度）加每卡一行状态。
2. 执行 `asys health -d 0`，记录输出：应是单卡明细表，列出错误码与描述（健康时 ErrorCode Num 为 0）。
3. 在 collect 产物目录中找 `health_result.txt`（执行 `asys collect` 后进入 `asys_output_<时间戳>/` 查找），对比它与步骤 1/2 输出的差异。

**需要观察的现象**：无 `-d` 是"概览"，有 `-d` 是"明细"；collect 场景下同样内容进了文件而非终端。

**预期结果**：三处输出同源（`run_health_check` 返回的同一个 ret 结构），仅展示层不同。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：8 卡中 7 卡 Healthy、1 卡 Warning，无 `-d` 时 Overall Health 显示什么？
**答案**：Warning。`_get_highest_status` 聚合取最严重等级，与多少卡健康无关。

**练习 2**：`_print_screen` 里 `device_id is False` 用 False 而不是 None 表示"未指定"，这个约定从哪来？
**答案**：来自 u2-l2 讲过的 `ParamDict.set_args()`：可选参数未传时统一在 ParamDict 中以 False 占位，业务侧因此用 `is False` 判断"用户没传"，与"传了 0 号卡"（`device_id == 0`）区分开。

**练习 3**：如果要把 health 检查改成真正的多线程并发，改哪里？
**答案**：`run_health_check()`（[asys_health.py:75-84](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py#L75-L84)）——把 for 循环换成 `threading.Thread` 按卡并发（参考 u2-l5 collect 或 diagnose 里已有的 daemon 线程 + join 写法），返回结构不变，下游零改动。

### 4.4 diagnose：综合与组件检测

#### 4.4.1 概念说明

`asys diagnose` 是"主动压测式体检"，与 health 的"读当前状态"不同——diagnose 会**主动施加负载**来暴露潜在硬件故障。`run()` 按 run_mode 分成两条完全不同的路：

| run_mode | 路径 | 实现方式 |
| --- | --- | --- |
| component | `env_detect()` | 转调兄弟工具 msaicerr 的 `-e` 环境检查（子进程方式） |
| hbm_detect / cpu_detect / stress_detect / aicore_stl_detect | `hardware_detect()` | 加载 `libascend_ml.so`（AML 诊断库），经芯片 handler 执行检测 |

#### 4.4.2 核心流程

```text
run()
 ├─ run_mode == "component" → env_detect()
 │    ├─ 定位 msaicerr.py（ParamDict().tools_path 向上一级的兄弟目录）
 │    ├─ 逐设备执行 python msaicerr.py --env -dev=<id>（子进程）
 │    └─ 汇总 PASS/FAIL，FAIL 时回显 msaicerr 输出并返回 False
 └─ 其他 → hardware_detect()
      ├─ get_diagnose_devices_chip_info()  芯片白名单过滤（910B/910_93/950/910_96）
      ├─ aicore_stl 模式额外只保留 950 设备
      ├─ _check_support()  虚机/root/opp_kernel/timeout 范围前置检查
      ├─ 加载 so（aml_aicore_stl.so 或 libascend_ml.so）
      ├─ ChipHandler().get_handler(chip_info).run_diagnose(...)  ← 芯片适配层入口
      ├─ 未参检设备补 WARN 占位
      └─ print_save()  打印表格 + 可选写 diagnose_result_<UTC时间戳>.txt
```

#### 4.4.3 源码精读

**① 双路分派** —— [asys_diagnose.py:294-300](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L294-L300)：`run()` 只有两行分支，component 走环境检测、其余走硬件检测。模式常量定义在 [asys_diagnose.py:37-40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L37-L40)。

**② component 模式 = 复用 msaicerr** —— [asys_diagnose.py:255-292](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L255-L292)：`run_msaicerr_cmd()` 用 `sys.executable` 拼出 `python msaicerr.py --env -dev=<id>` 子进程（u3 单元会精读 msaicerr 的 -e 实现）；msaicerr 路径来自 `ParamDict().tools_path.parents[1].joinpath("msaicerr", "msaicerr.py")`——依赖 .run 包把两个工具装进同一 tools/ 目录这一安装布局（u1-l3）。注意 FAIL 分支里对 `debug_info.txt` 可写性的检查：msaicerr 失败时想在工作目录留 debug_info.txt，asys 先确认可写再把 msaicerr 输出回显给用户。

**③ 硬件检测的前置闸门** —— [asys_diagnose.py:137-176](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L137-L176)：`_check_support()` 依次检查：非 VM/容器（`systemd-detect-virt` 返回 none 才算物理机）、必须 root、stress 模式要求 5 个 opp_kernel 包（ops_cv/ops_legacy/ops_math/ops_nn/ops_transformer，见 [asys_diagnose.py:41](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L41)）已安装、hbm/cpu 模式的 timeout 必须落在各自下限到 DETECT_MAX_TIMEOUT 区间。压测类操作风险高，所以闸门格外多。

**④ 芯片白名单与 950 专属模式** —— [asys_diagnose.py:184-203](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L184-L203)：`get_diagnose_devices_chip_info()` 用 u2-l4 讲过的 `AsysDiagnoseSupportedChip` 白名单逐卡过滤，不支持的卡打日志跳过；[asys_diagnose.py:307-317](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L307-L317) 的 `__filter_aicore_stl_devices()` 进一步把 aicore_stl 模式收窄到芯片名含 "950" 的卡并逐卡告警。

**⑤ 真正干活的 run_diagnose** —— [asys_diagnose.py:205-253](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L205-L253)：`hardware_detect()` 加载对应 so（aml_aicore_stl.so 或 libascend_ml.so——即 u1-l2 讲过的闭源 bundle），起 daemon 线程转进度动画，然后 `ChipHandler().get_handler(chip_info).run_diagnose(...)` 把执行交给芯片 handler（u2-l4 提过：Ascend91093Handler 覆写 run_diagnose 实现按逻辑主卡多线程诊断）。未参检的卡在结果里补 `[WARN, "0"]`/WARN 占位，保证多卡场景下表格每卡都有行。

**⑥ 结果输出** —— [asys_diagnose.py:97-135](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L97-L135)：`print_save()` 先打印表格（无 `-d` 时多卡结果一致则显示 "Pass - All" 压缩形态），再按 `-o` 参数决定是否写 `diagnose_result_<UTC时间戳精确到毫秒>.txt`；WARN 结果会附一句 "please analyze aml logs"（[asys_diagnose.py:247-248](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L247-L248)），引导用户去看 AML 诊断库日志。

#### 4.4.4 代码实践

**实践目标**：在无设备环境下走通 component 模式的"路径定位"逻辑。

**操作步骤**：

1. 读 [asys_diagnose.py:263-273](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L263-L273)，写出 msaicerr.py 路径的计算式。
2. 在已安装 oam-tools 的环境执行 `asys diagnose -r component -d 0`，观察它是否成功调起 msaicerr（ps 里能看到 `msaicerr.py --env -dev=0` 子进程）。
3. 换一个不存在 msaicerr 的目录结构模拟（例如只拷贝 asys 目录单独运行），验证会命中 "cannot be found, please install the whole package" 报错。

**需要观察的现象**：component 模式的成败完全取决于 msaicerr 是否安装在约定相对位置。

**预期结果**：完整安装时输出环境检测 PASS/FAIL 表格；残缺安装时报上述错误。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：diagnose 与 health 的本质区别是什么？
**答案**：health 是**被动读取**当前健康状态与错误码（DSMI 查询接口）；diagnose 是**主动施加负载**（AML 压测库）暴露潜在故障，因此需要 root、物理机、opp_kernel 安装等更严格的前置条件。

**练习 2**：为什么 aicore_stl_detect 要单独加载 `libaml_aicore_stl.so` 而不是复用 `libascend_ml.so`？
**答案**：aicore_stl（AI Core STL 检测）是 Ascend950 专属的新检测能力，独立成 so 使其按需加载——非 950 场景不必携带该依赖，这也和 u1-l2 讲过的闭源 bundle 按需下载机制呼应（见 [asys_diagnose.py:222-229](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L222-L229) 的分支加载）。

**练习 3**：`hardware_detect()` 里"未参检设备补 WARN"那几行（[asys_diagnose.py:241-245](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/diagnose/asys_diagnose.py#L241-L245)）为什么必要？
**答案**：多卡机器上部分卡可能不在白名单或（stl 模式下）非 950 被过滤，若直接渲染 ret，这些卡会在结果表中整行缺失，用户无法区分"没测"和"漏显"；补 WARN 占位保证结果表覆盖全部卡，语义是"该卡未参检，需关注"。

## 5. 综合实践

**任务：手绘 `asys launch` 的端到端调用链图**（本讲规格指定的实践，纯源码阅读型，无设备也能完成）。

从命令行参数定义出发，跟踪 `asys launch "python3 train.py" -o ./out` 一路到采集落盘，画出调用链并标注每一步所在的文件与关键行号。参考骨架（请补全每一步的"发生了什么"）：

```text
① cmd_parser.py:219-224  Command.LAUNCH 声明参数集 [TASK, OUTPUT, TAR]
② cmd_parser.py:49-53    Arg.TASK：名字 task、校验器 EXECUTABLE、必选、位置参数
③ （你补）cmdline/arg_checker.py   EXECUTABLE 校验做了什么？
④ （你补）params/param_dict.py     set_args() 把 task/output 翻译成什么 key？
⑤ （你补）asys.py:128      create_out_timestamp_dir() 创建了什么目录？
⑥ （你补）asys.py:133      EXECUTE_CMD_FUNC.get("launch") 拿到哪个类？
⑦ （你补）launch/asys_launch.py:36-52   __init__ 注入了哪些环境变量？
⑧ （你补）launch/asys_launch.py:71-97   Popen 如何托管子进程？
⑨ （你补）launch/asys_launch.py:114     AsysCollect 从哪里 import、collect() 做了什么（回看 u2-l5）？
⑩ （你补）launch/asys_launch.py:136-140 clean_work 删了哪些目录？
```

**验收标准**：

1. 图上至少出现 6 个文件节点，每个节点标一行说明。
2. 能回答两个"分叉问题"：参数在哪一步从 Namespace 变成业务 key？采集能力在哪一步从 launch 模块"交接"给 collect 模块？
3. 把同一套画法套用到 `asys health`（链路最短：cmd_parser → ParamDict → AsysHealth.run → DeviceInfo），对比两条链的长度差，体会"纯查询命令"与"托管型命令"的复杂度差异。

## 6. 本讲小结

- 四个子命令类都遵守"无参构造 + `run()`"的隐式接口，由 [asys.py:61-67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L61-L67) 的 `EXECUTE_CMD_FUNC` 字典分发，入口对实现零耦合。
- `AsysLaunch` = 环境变量注入 + 进程组托管子进程 + 复用 `AsysCollect` 采集 + 中间目录清理；采集能力不自建，单一出口在 collect 子系统。
- `AsysInfo` 三模式（hardware/software/status）分别以 shell 命令、version.info 文件、芯片 handler 的 ctypes 查询为数据源，整体带超时保护，status 行集按芯片配置白名单动态过滤。
- `AsysHealth` 以"最高严重度"聚合多卡状态（Unknown > Critical > Alarm > Warning > Healthy），靠 `ParamDict().get_command()` 区分"health 命令打印"与"collect 复用落盘"双出口。
- `AsysDiagnose` 双路：component 模式以子进程转调兄弟工具 msaicerr 的环境检查（依赖 .run 包安装布局）；硬件检测模式经芯片白名单过滤、多重前置闸门后，把执行交给 `ChipHandler` 分发的具体型号 handler 与闭源 AML 诊断库。
- 贯穿四者的公共手法：`ParamDict` 全局参数单例（可选参数以 False 占位）、daemon 线程 + `finish_flag` 进度动画、`generate_report` 统一表格渲染、失败退化为占位值而非异常。

## 7. 下一步学习建议

- 下一讲 u2-l7 讲 `asys analyze`：AI Core Error 报告与 coredump 文件的解析流程，它承接本讲 launch/collect 采回来的故障文件，完成"采集 → 解析"闭环。
- 横向延伸：u3-l1 起精读 msaicerr，你会看到本讲 `run_msaicerr_cmd()` 转调的 `--env` 检查在另一侧是如何实现的。
- 源码补读建议：`src/asys/view/table.py` 的 `generate_report()`（四条命令共用的表格渲染器）和 `src/asys/view/progress_display.py` 的 `waiting()`（进度动画），两者都很短，读完能补全本讲所有输出形态的最后一环。
