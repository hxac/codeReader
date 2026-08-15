# u2-l8 asys profiling 与 config：配置机制与性能数据采集

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 asys 的**两层"配置"**——静态配置文件 `asys.ini`（由 `AsysConfigParser` 加载）与设备侧配置项（由 `asys config` 子命令查询/恢复）——它们是两套完全不同的机制，不要混淆。
2. 读懂 `config/config_parser.py` 如何把 `asys.ini` 的配置翻译成环境变量名，并经 `ParamDict` 传递给 `launch` 等子命令。
3. 读懂 `config_cmd/asys_config.py` 与 `config_cmd/interface.py` 如何实现 `asys config --get/--restore stress_detect`。
4. 读懂 `profiling/asys_profiling.py` 如何把 `asys profiling` 的参数**拼装成一条 msprof 命令**并以子进程执行，理解 asys 与 msprof 的关系。

本讲承接 u2-l2（Arg 枚举、ParamDict）与 u2-l4（芯片适配层）的认知，是 asys 单元的收官讲。

## 2. 前置知识

- **配置文件 vs 配置项**：本讲会接触两个容易混淆的概念。`asys.ini` 是磁盘上的静态配置文件，控制 asys 自身行为（如 launch 时注入哪些环境变量）；`asys config` 子命令操作的则是**设备侧**（NPU 硬件）的配置，如压测检测相关的 AI Core / Bus 电压。
- **ini 格式**：Python 标准库 `configparser` 解析的格式，`[section]` 下跟 `key = value`。
- **NamedTuple**：具名元组，`collections.namedtuple` 的类型化写法，可以像类一样 `IniConfItem(para_name=..., ...)` 创建、按属性访问。
- **子进程拼接命令**：把用户参数拼成一条 shell 命令字符串，用 `subprocess.run(cmd, shell=True)` 执行。这是 asys 复用既有工具（msprof、msaicerr）的通用手法，u2-l7 的 aicore_error 模式已经见过一次。
- **ctypes 调闭源库**：`restore_stress_detect_config` 通过 `device_obj.ascend_ml.AmlStressRestore()` 调用 AML 诊断库恢复设备配置，这是 u2-l4 芯片适配层与 u2-l6 diagnose 用过的同一套机制。
- **LP（Low Power）模式**：低功耗运行模式。profiling 的 power 采集需要根据芯片当前是低功耗模式还是 AI Core 模式，决定向 msprof 传 `--sys-lp=on` 还是 `--ai-core=on`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/asys/config/config_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py) | 加载 `dependent_package.csv` 与 `asys.ini`，把 ini 项翻译后写入 ParamDict |
| [src/asys/conf/asys.ini](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/conf/asys.ini) | asys 的静态配置文件本体，目前只有 `[launch]` 一个 section |
| [src/asys/conf/dependent_package.csv](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/conf/dependent_package.csv) | 依赖包清单（包名 + 探测命令），供环境检查用 |
| [src/asys/config_cmd/asys_config.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/asys_config.py) | `asys config` 子命令实现类 AsysConfig |
| [src/asys/config_cmd/interface.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/interface.py) | stress_detect 配置项的 get/restore 具体实现 |
| [src/asys/profiling/asys_profiling.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py) | `asys profiling` 子命令实现类 AsysProfiling |
| [src/asys/cmdline/cmd_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py) | config / profiling 两个子命令的 Arg 与 Command 声明（u2-l2 已学） |
| [src/asys/common/path.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/path.py) | `get_project_conf()` 定位 conf 目录 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**配置文件加载（config_parser）**、**config 子命令（AsysConfig + interface）**、**profiling 子命令（AsysProfiling）**。

### 4.1 静态配置机制：config_parser.py 与 asys.ini

#### 4.1.1 概念说明

asys 的部分行为需要可配置而不想暴露成命令行参数——例如 `asys launch` 复跑业务时要给子进程注入哪些排障环境变量（u2-l6 讲过 AsysLaunch 注入 9 个环境变量）。这类"高级 knobs"放在 `asys.ini` 里。

`AsysConfigParser` 就是这个文件的加载器。它在 `asys.py` 主流程的第 5 步（配置加载）被调用，产物写入 `ParamDict` 单例，供后续子命令读取。

这里有一个关键的**翻译**设计：ini 里的键值是面向人的（`log_level = INFO`），而业务代码需要的是面向 CANN 的环境变量值（`ASCEND_GLOBAL_LOG_LEVEL=1`）。`ASYS_INI_VALUE_MAP` 就是这张翻译表。

#### 4.1.2 核心流程

```text
AsysConfigParser.parse()
  ├─ __parse_deps()                    # 读 dependent_package.csv → ParamDict.set_deps()
  └─ __parse_ini()                     # 读 asys.ini
       ├─ 取当前子命令名（ParamDict().get_command()）
       ├─ 命令名不是 ini 的 section → 直接返回（该命令无 ini 配置）
       └─ 遍历该 section 的每个 key=value：
            ├─ 查 ASYS_INI_VALUE_MAP 得 IniConfItem(para_name, conf_val_map, default_key)
            ├─ 值不在 conf_val_map → 用 default_key 对应值 + 打告警
            └─ ParamDict().set_ini(ini_name, ini_value)   # 以环境变量名做 key 存储
```

配置文件的定位由 [path.py:get_project_conf](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/path.py#L29-L36) 完成，三级兜底：先找 `sys.argv[0]` 同级的 `conf/`（软链接/安装态），再找项目根 `conf/`，最后找 `src/asys/conf/`（源码态）。

#### 4.1.3 源码精读

先看翻译表的数据结构。[config_parser.py:L31-L34](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L31-L34) 定义了每个配置项的三元组：翻译后的名字、合法值映射、默认值键。

[config_parser.py:L37-L59](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L37-L59) 是完整的 `ASYS_INI_VALUE_MAP`，定义了 asys 全部 7 个 ini 可配置项：

| ini 键（面向用户） | para_name（内部/环境变量名） | 合法值 → 翻译值 | 默认 |
| --- | --- | --- | --- |
| `graph` | graph | TRUE→1, FALSE→0 | TRUE |
| `ops` | ops | TRUE→1, FALSE→0 | TRUE |
| `dump_ge_graph` | DUMP_GE_GRAPH | 1/2/3 | 2 |
| `dump_graph_level` | DUMP_GRAPH_LEVEL | 1/2/3 | 2 |
| `log_level` | ASCEND_GLOBAL_LOG_LEVEL | DEBUG→0 … NULL→4 | INFO |
| `log_event_enable` | ASCEND_GLOBAL_EVENT_ENABLE | FALSE→0, TRUE→1 | TRUE |
| `log_print_to_stdout` | ASCEND_SLOG_PRINT_TO_STDOUT | FALSE→0, TRUE→1 | FALSE |

对照配置文件本体 [asys.ini:L1-L15](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/conf/asys.ini#L1-L15)：只有一个 `[launch]` section——也就是说这些配置目前只对 launch 子命令生效。

核心解析逻辑在 [config_parser.py:L77-L102](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L77-L102) 的 `__parse_ini()`。注意三处防御式设计：

- 当前命令没有对应 section 时静默返回成功（L84-L86），即"无配置也是一种正常状态"；
- ini 中出现未注册的键只打 warning 并跳过（L90-L92），不中断；
- 值非法时回退默认值并打 warning（L96-L99），同样不中断。

这与 u2-l3 总结的 asys "失败占位、不抛异常"气质一致。

配置的**消费方**印证了翻译的用途——[asys_launch.py:L43-L47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L43-L47) 用 `ParamDict().get_ini("DUMP_GE_GRAPH")` 等取值组装子进程环境变量；[graph_collect.py:L87](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/graph/graph_collect.py#L87) 和 [ops_collect.py:L206](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/ops/ops_collect.py#L206) 则用 `get_ini("graph") == "1"` 决定 launch 场景下是否采集图和 ops——也就是说改一行 ini 就能关掉 launch 的某类采集。

另外 [config_parser.py:L68-L75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L68-L75) 的 `__parse_deps()` 读取 [conf/dependent_package.csv](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/conf/dependent_package.csv)（内容为 `包名, 版本探测命令` 两列，如 `g++,g++ --version | grep g++`），存入 `ParamDict().set_deps()`，供 info 子命令展示软件依赖版本。

#### 4.1.4 代码实践

1. **实践目标**：验证"改 ini 一行，launch 行为改变"的完整链路。
2. **操作步骤**：
   - 打开 `src/asys/conf/asys.ini`，把 `graph = TRUE` 改为 `graph = FALSE`（只改这一个文件，勿改源码其他部分）。
   - 在有昇腾设备的环境执行 `asys launch "<任意可快速结束的命令>" -o /tmp/launch_out`；无设备环境则走纸面：对照本节源码说明每个 ini 键会翻译成什么值、注入到哪里。
   - 检查 `/tmp/launch_out` 产物中是否还有 graph 相关文件；再把 `log_level` 改成 `DEBUG` 重跑，观察日志量变化。
3. **需要观察的现象**：`graph = FALSE` 后 launch 产物中不再有 GE 图 dump 文件；命令仍成功退出（配置非法才会回退默认，合法值直接生效）。
4. **预期结果**：理解 `get_ini("graph") == "1"` 这个开关的判断点在 `graph_collect.py:87`。**待本地验证**（需要真实设备与 CANN 环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `asys.ini` 中 `log_level` 写成 `TRACE`（非法值），程序会崩溃吗？实际行为是什么？

**答案**：不会崩溃。`__parse_ini()` 在 [config_parser.py:L96-L99](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L96-L99) 发现 `conf_val_map.get("TRACE")` 为 None 后，回退到 `default_key`（`INFO`→`1`）并打一条 warning 日志。

**练习 2**：为什么 `asys collect` 不读取 `asys.ini`？

**答案**：`__parse_ini()` 以 `ParamDict().get_command()` 的结果作为 section 名查表（[config_parser.py:L83-L86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config/config_parser.py#L83-L86)），而 `asys.ini` 只有 `[launch]` section，其他命令查不到 section 就直接返回成功。

**练习 3**：`dump_ge_graph` 在 ini 里写 `2`，最终 launch 子进程拿到的环境变量是什么？

**答案**：`DUMP_GE_GRAPH=2`。`para_name` 是 `DUMP_GE_GRAPH`，映射 `{1:1, 2:2, 3:3}`，`AsysLaunch` 在 [asys_launch.py:L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/launch/asys_launch.py#L43) 用 `get_ini("DUMP_GE_GRAPH")` 取出后注入子进程环境。

### 4.2 config 子命令：AsysConfig 与 stress_detect

#### 4.2.1 概念说明

`asys config` 操作的是**设备侧配置**，与 4.1 的 ini 完全无关。它目前的命令行形态是：

```bash
asys config --get stress_detect -d <device_id>       # 查询压测检测相关配置（AI Core / Bus 电压）
asys config --restore stress_detect -d <device_id>   # 恢复默认（需 root）
```

注意：**当前源码中没有 `asys config set` 这种"设置"动作**，只有 `--get` 与 `--restore` 两个互斥的操作（命令行声明见 [cmd_parser.py:L248-L252](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L248-L252) 的 `Command.CONFIG`，以及 [cmd_parser.py:L168-L180](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L168-L180) 的 `Arg.GET`/`Arg.RESTORE`/`Arg.STRESS_DETECT`）。网上或旧文档若提到 `asys config set`，与本仓库 HEAD 不符。

#### 4.2.2 核心流程

```text
AsysConfig.run()
  └─ _check_support()                       # 五道闸门，任一失败即返回 False
       ├─ systemd-detect-virt 必须为 none   # 虚机/docker 不支持
       ├─ 必须是 root                       # restore 模式硬性要求
       ├─ 设备数 > 0 且 -d 在范围内
       ├─ 芯片在 AsysConfigSupportedChip 白名单内（u2-l4 的白名单机制）
       └─ 必须给 --get 或 --restore，且至少一个配置选项（stress_detect）
  ├─ get 模式    → interface.get_stress_detect_config()
  └─ restore 模式 → interface.restore_stress_detect_config()
```

`get_stress_detect_config` 的取数链：芯片白名单（读 `conf/config_table.csv`）→ `device_obj.get_device_aic_info()` / `get_device_bus_info()`（u2-l3/u2-l4 的 ctypes 设备查询）→ `generate_report` 生成表格打印到屏幕。

#### 4.2.3 源码精读

[asys_config.py:L37-L75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/asys_config.py#L37-L75) 的 `_check_support()` 集中体现了"先闸门后干活"：其中 L39 用 `run_linux_cmd('systemd-detect-virt', 'none')` 判断是否物理机——第二个参数是期望输出，匹配才返回真；L55 复用 u2-l4 讲过的 `AsysConfigSupportedChip` 白名单；L65-L73 把可选配置项收拢为列表 `self.options`，当前只有 `stress_detect` 一项，但列表结构天然为将来扩展留了位。

[asys_config.py:L91-L100](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/asys_config.py#L91-L100) 的 `run()` 仍是 asys 家族的标准接口"无参构造 + run()"（u2-l1），按模式分发到两个私有方法。

get 的具体实现在 [interface.py:L46-L65](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/interface.py#L46-L65)：逐项查询 AI Core 电压与 Bus 电压，任一成功即汇成表格输出。注意 L51/L54 的判断 `xxx != device_obj.UNSUPPORTED_KEY_WORDS[-1]`——这是 u2-l4 讲过的"不支持时返回 NOT_SUPPORT 占位值"约定，这里用占位值过滤掉芯片不支持的能力。

restore 的实现在 [interface.py:L68-L84](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/interface.py#L68-L84)：核心是 L75 的 `device_obj.ascend_ml.AmlStressRestore(ctypes.c_int32(device_id))`——直接 ctypes 调用闭源 AML 诊断库恢复压测配置，并用 `try/except AttributeError` 兜住 handler 未挂载 `ascend_ml` 属性的情况。

芯片白名单数据来自 CSV：[interface.py:L28-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/interface.py#L28-L43) 的 `_check_supported_chips()` 调 `FileOperate().read_config()`，后者由 [const.py:L104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L104) 指向 `src/asys/conf/config_table.csv`（表头 `option,get,set,restore`，每行给出各操作支持的芯片列表或 `ALL`），按 get/restore 两列取出白名单后与当前芯片信息做正则匹配。

#### 4.2.4 代码实践

1. **实践目标**：跑通（或纸面推演）`asys config --get stress_detect`，并定位每个输出项的取数代码。
2. **操作步骤**：
   - 在昇腾物理机（非虚机/容器）上执行 `asys config --get stress_detect -d 0`；若在无设备环境，改为阅读源码完成下表。
   - 对照源码填写：输出表格中"AI Core Voltage (MV)"来自哪个方法调用（提示：`interface.py:50-52`）；"Bus Voltage (MV)"来自哪个（`interface.py:53-55`）；两者都取不到时函数返回什么。
   - 再读 `conf/config_table.csv`，找到 `aic_voltage` / `bus_voltage`（对应 `ConfigOptionName`）两行的 get、restore 列，说明哪些芯片支持恢复。
3. **需要观察的现象**：get 成功时屏幕输出一张 `Device ID: x | CURRENT CONFIGURATION` 表格；在容器中执行则报"The config command cannot be executed on VMs and docker."
4. **预期结果**：两个电压值均来自 `device_obj` 的 ctypes 查询，全部失败时 `get_stress_detect_config` 返回 False 并打 error。**待本地验证**（需要真实设备）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_check_support()` 里 root 检查放在最前面之一，而不管用户是 get 还是 restore？这对 get 模式公平吗？

**答案**：源码 [asys_config.py:L43-L45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/asys_config.py#L43-L45) 的注释写着"restore mode only support root"，但检查本身是无条件的——即当前实现下 get 也要求 root。这是一个实现取舍（简单优先），说"对 get 模式偏严"是合理观察。

**练习 2**：如果某款新芯片不支持 Bus 电压查询，`get_stress_detect_config` 会崩溃吗？

**答案**：不会。`get_device_bus_info()` 返回的占位值等于 `UNSUPPORTED_KEY_WORDS[-1]`（NOT_SUPPORT 类占位）时，[interface.py:L54](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/config_cmd/interface.py#L54) 的条件判断直接跳过该行，只输出 AI Core 电压。

### 4.3 profiling 子命令：AsysProfiling 与 msprof 的衔接

#### 4.3.1 概念说明

`asys profiling` 回答的问题是："排障时想要设备的性能采样数据，但又不想学 msprof 的一长串参数"。它的实现哲学与 u2-l7 的 aicore_error 模式一模一样——**asys 不重复造轮子，只做面向排障场景的参数封装，底层转调 msprof 命令行**。asys 与 msprof 的关系是"排障入口 ↔ 性能采集引擎"，两者通过 shell 命令交互、零代码依赖。

命令行形态（[cmd_parser.py:L253-L257](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L253-L257)）：

```bash
asys profiling -r <aicore|dvpp|os|link|memory|power 组合> -p <秒> [-d 设备号] [-o 输出目录] [--aic_metrics=...]
```

`-r` 与 `-p` 是必选参数（[cmd_parser.py:L181-L193](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L181-L193)）。

#### 4.3.2 核心流程

```text
AsysProfiling.run()
  ├─ _check_param()
  │    ├─ period ∈ [1, MAX_PERIOD=2592000]
  │    ├─ 芯片在 AsysProfilingSupportedChip 白名单内（复用 u2-l4 机制）
  │    ├─ run_mode 集合 ⊆ {aicore, dvpp, os, link, memory, power}
  │    ├─ dvpp 模式需 handler.support_dvpp() 为真
  │    ├─ 决定 lp_mode：handler.need_lp_param() → LP；未选 aicore → AIC；否则 NO
  │    └─ 未指定 -o 时，生成 asys_profiling_result_<UTC 时间戳毫秒> 输出目录名
  ├─ 拼装基础命令：msprof --output=... --sys-period=... --sys-devices=...
  ├─ 对每个 run_mode 动态查找 concat_<mode> 方法并累加该模式的 msprof 专有参数
  └─ _run_cmd()：subprocess.run(shell=True) 执行，按返回码判定成败
```

run_mode 到 msprof 参数的映射表：

| run_mode | 拼接的 msprof 参数 | 说明 |
| --- | --- | --- |
| aicore | `--ai-core=on --aic-mode=sample-based --aic-metrics=<指标>` | 指标由 `--aic_metrics` 决定，默认 PipeUtilization |
| dvpp | `--dvpp-profiling=on` | 需芯片支持 |
| os | `--sys-profiling=on` | 系统信息 |
| link | `--sys-interconnection-profiling=on --sys-io-profiling=on` | 互联与 IO |
| memory | `--sys-hardware-mem=on --llc-profiling=read` | 硬件内存与 LLC |
| power | 视 lp_mode：`--ai-core=on` 或 `--sys-lp=on` | 低功耗模式相关 |

#### 4.3.3 源码精读

[asys_profiling.py:L31-L38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L31-L38) 构造函数照例从 `ParamDict` 取参（u2-l2 讲过的全局参数单例），`aic_metrics` 与 `period` 都带默认值兜底。

最精彩的是 [asys_profiling.py:L84-L96](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L84-L96) 的 `run()`——又一次"字符串拼方法名 + getattr"动态分发：

```python
for run_mode in self.run_modes:
    func_name = "concat_" + run_mode
    func = getattr(self, func_name, None)
    if func:
        cmd = func(cmd)
```

每个 run_mode 对应一个 `concat_<mode>` 静态/实例方法（[L40-L82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L40-L82)），只做一件事：往命令尾部追加该模式的 msprof 开关。**新增一种采集模式 = 加一个 `concat_xxx` 方法 + 把名字加进 `support_profiling_list`（[L28](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L28)）+ cmd_parser 帮助文案**，中心流程零改动——和 u2-l5 采集项扩展点同一设计哲学。

[asys_profiling.py:L98-L136](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L98-L136) 的 `_check_param()` 值得注意两点：

- L122 的 `getattr(handler, "support_dvpp", lambda: True)()`——用 getattr 默认值技巧实现"handler 没声明该能力就视为支持"，是模板方法模式"按需覆写"（u2-l4）的运行期变体。
- L125-L128 的 lp_mode 决策：芯片本身处于低功耗态（`handler.need_lp_param()` 为真）则 power 采集用 `--sys-lp=on`；用户没选 aicore 时补 `--ai-core=on`（[L68-L76](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L68-L76) 的 `concat_power` 按此消费）。三个模式常量定义在 [const.py:L66-L68](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L66-L68)。

最后 [asys_profiling.py:L138-L147](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L138-L147) 的 `_run_cmd()` 用 `subprocess.run(cmd, shell=True)` 执行拼好的 msprof 命令，按返回码打成功/失败日志。日志里提示"please wait about {period} seconds"——因为 msprof 会按 `--sys-period` 周期采样，命令耗时与周期正相关。

#### 4.3.4 代码实践

1. **实践目标**：不运行也能准确预测 asys profiling 会执行哪条 msprof 命令（"人肉 dry-run"）。
2. **操作步骤**：
   - 假设执行 `asys profiling -r aicore,os -p 10 -d 0`。
   - 对照 `run()` 与各 `concat_*` 方法，先在纸上写出完整命令。注意 `-r` 传入的是逗号串，`_check_param()` L113 会 `set(self.run_modes.split(','))`，因此拼接顺序取决于集合迭代顺序（无序）。
   - 在有设备的环境实际执行，`log_info` 会把完整命令打进日志，与自己写的对比。
3. **需要观察的现象**：日志中出现 `Start run: msprof --output=asys_profiling_result_<时间戳> --sys-period=10 --sys-devices=0 ...`，随后是 msprof 自身的输出。
4. **预期结果**：纸上命令与日志命令一致，形如 `msprof --output=... --sys-period=10 --sys-devices=0 --ai-core=on --aic-mode=sample-based --aic-metrics=PipeUtilization --sys-profiling=on`（aicore 与 os 两段参数顺序可能互换）。**待本地验证**（需要设备与 CANN 环境；无环境时纸面推演即为完成）。

#### 4.3.5 小练习与答案

**练习 1**：`asys profiling -r power -p 5`（不选 aicore）在低功耗芯片上最终拼出的 power 段参数是什么？

**答案**：`handler.need_lp_param()` 为真时 `lp_mode = LP_MODE_LP`，`concat_power` 拼接 `--sys-lp=on`；若芯片不需要 LP 参数，则因未选 aicore 而 `lp_mode = LP_MODE_AIC`，拼接 `--ai-core=on`（[asys_profiling.py:L68-L76](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L68-L76)）。

**练习 2**：如果用户执行 `asys profiling -r gpu -p 5`，会在哪一步、由哪行代码拦下？

**答案**：在 `_check_param()` 的 [asys_profiling.py:L113-L116](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/profiling/asys_profiling.py#L113-L116)，`{"gpu"}` 不是 `support_profiling_list` 的子集，打 error "Run mode type is unsupported" 并返回 False，`run()` 随之返回。

**练习 3**：`--aic_metrics` 什么时候生效？有哪些合法取值？

**答案**：仅当 `-r` 包含 `aicore` 时被 `concat_aicore` 拼进 `--aic-metrics`；合法值在 [cmd_parser.py:L194-L207](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L194-L207) 的 `KEY_CHOICES` 中声明（PipeUtilization、ArithmeticUtilization、Memory、MemoryL0、MemoryUB、ResourceConflictRatio、L2Cache、MemoryAccess），默认 PipeUtilization。

## 5. 综合实践

**任务：整理一张"asys 全量可配置项地图"并完成一次配置变更闭环。**

1. 通读 `src/asys/conf/asys.ini` 与 `ASYS_INI_VALUE_MAP`，列出 7 个 ini 配置项的「用户键 / 内部名 / 合法值 / 默认值 / 消费方」五列表格（消费方提示：`asys_launch.py:43-47`、`graph_collect.py:87`、`ops_collect.py:206`）。
2. 补上第二类配置项：`asys config` 的 `stress_detect`（get 查什么、restore 调什么库、白名单来自哪个 CSV），与 `asys profiling` 的 `-r/-p/--aic_metrics`（各自落到 msprof 的哪个参数）。
3. 任选一个 ini 项做变更闭环：例如把 `log_level` 从 `INFO` 改为 `DEBUG` → 在 `config_parser.py` 中找到读取代码（`__parse_ini()` L88-L100 与映射表 L50-L52）→ 说明/验证 launch 子进程将拿到 `ASCEND_GLOBAL_LOG_LEVEL=0`。
4. 交付物：一张 Markdown 表格 + 一段 200 字以内的"两类配置机制对比"（静态 ini 控制自身行为 vs config 子命令操作设备侧状态）。

本任务不需要昇腾设备即可完成（纸面推演），有设备则全部可实测。

## 6. 本讲小结

- asys 有**两套互不相干的"配置"**：`asys.ini` 静态配置文件（由 `AsysConfigParser` 加载、`ASYS_INI_VALUE_MAP` 翻译成环境变量名、经 ParamDict 分发）和 `asys config` 子命令（操作设备侧的压测检测电压配置）。
- `__parse_ini()` 按子命令名查 section，未注册的键、非法的值都只告警不中断，延续 asys 防御式风格；配置消费方主要是 launch 的环境变量注入与 collect 的采集开关。
- `asys config` 只有 `--get` 和 `--restore` 两个互斥动作（**没有 set**），当前唯一配置选项是 `stress_detect`；get 走 handler 的 ctypes 电压查询，restore 直接调闭源 AML 库 `AmlStressRestore`。
- `asys profiling` 是 msprof 的"排障友好封装"：校验参数与芯片白名单后，按 run_mode 动态查找 `concat_<mode>` 方法把 msprof 专有参数逐段拼上，最终 `subprocess.run(shell=True)` 转调 msprof，与 msprof 零代码依赖。
- 三处复用了前几讲的核心机制：`ParamDict` 单例取参（u2-l2）、`CommandSupportedChip` 白名单与 handler 能力开关（u2-l4）、子进程拼接命令复用兄弟工具（u2-l7）。

## 7. 下一步学习建议

asys 单元到此收官。接下来按依赖关系有两个方向：

- **进入 u3 单元（msaicerr）**：理解 `asys analyze` 转调的 msaicerr 内部如何解析 AI Core Error 报告与 Dump 文件，补全"采集 → 解析"链条的另一半。
- **进入 u4 单元（msprof）**：本讲只把 msprof 当作黑盒命令行引擎，u4-l2/u4-l3 将打开这个黑盒，读 `msprof_bin.cpp` 入口与 profapi 插件体系，理解 `--ai-core=on` 这些开关在 C++ 侧如何落地。
