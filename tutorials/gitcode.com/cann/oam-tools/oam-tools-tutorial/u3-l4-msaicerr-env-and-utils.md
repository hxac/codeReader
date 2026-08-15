# u3-l4 msaicerr 环境检查与工具函数：dsmi_interface 与 utils

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 `dsmi_interface.py` 如何用 ctypes 直调 NPU 驱动库（`libdrvdsmi_host.so`、`libascend_hal.so`）获取设备数量、芯片型号和核心数。
2. 理解 `utils.py` 的「日志双通道」设计：INFO 上屏 + 全量落盘 `debug_info.txt`，以及 `ExceptionRootCause` 根因收集与 `@screen_error` 装饰器的配合。
3. 完整追踪 `-e` 环境检查的调用链：`main() → test_env() → get_soc_version() → AicoreErrorParser.run_test_env() → golden_op.py 子进程`，并列出它实际检查的每一项。

本讲是 msaicerr 单元的收官讲。前 three 讲（u3-l1 入口、u3-l2 报告解析、u3-l3 Dump 解析）讲的是「分析」链路，本讲讲的是「运行前自检」链路和贯穿全工具的公共设施。

## 2. 前置知识

- **DSMI**：Device Service Management Interface，昇腾驱动提供的设备管理 C 接口，编译成 `libdrvdsmi_host.so`。查询设备数、芯片型号这类「不经过 CANN 运行时、直接问驱动」的操作都走它。
- **HAL**：Hardware Abstraction Layer，驱动侧另一个库 `libascend_hal.so`，msaicerr 用它的 `halGetDeviceInfo` 查 AI Core / Vector Core 数量。
- **ctypes**：Python 标准库，允许不写任何 C 扩展代码就直接调用动态库里的 C 函数。调用时需要自己声明参数类型和返回类型，并用 `ctypes.Structure` 描述 C 结构体的内存布局。
- **golden op（金标算子）**：一个内置的、结果已知的极简样例算子（`AddCustom`，两个 float16 矩阵相加）。让它真实跑一遍「编译 → 下发 → 执行」，如果成功，说明驱动、CANN 工具链、芯片、运行时整条链路都正常。这是比逐项静态检查更可靠的「活性探针」思路。
- **soc_version**：芯片型号字符串（如 `Ascend910B2`、`Ascend950`），算子编译时必须与实际硬件匹配，否则编译产物无法在设备上运行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/msaicerr/ms_interface/dsmi_interface.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py) | 驱动接口封装：DSMI/HAL 两个 so 的 ctypes 包装，提供设备数、芯片信息、核心数查询 |
| [src/msaicerr/ms_interface/utils.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py) | 公共工具函数：日志双通道、根因收集、路径校验、命令执行、文件操作 |
| [src/msaicerr/msaicerr.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py) | 工具入口：`test_env()` 是 `-e` 模式的直接实现 |
| [src/msaicerr/ms_interface/aicore_error_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py) | `run_test_env()` 静态方法在环境检查链路的中段，负责启动 golden op 子进程 |
| [src/msaicerr/ms_interface/golden_op.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py) | 内置样例算子的运行脚本，作为子进程被拉起 |
| [src/msaicerr/ms_interface/constant.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py) | `Constant` 错误码枚举（如 `MS_AICERR_HARDWARE_ERR = 103`） |
| [docs/zh/msaicerr/environment_check.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/msaicerr/environment_check.md) | `-e` 模式的用户文档 |

## 4. 核心概念与源码讲解

### 4.1 dsmi_interface：用 ctypes 直调驱动库的设备探针

#### 4.1.1 概念说明

msaicerr 在两个时刻需要「绕过 CANN 运行时、直接问驱动」：

1. 入口处校验 `-dev` 指定的设备号是否合法（需要知道总设备数）。
2. 环境检查时获取芯片型号（`soc_version`）和核心数（golden op 的 `block_dim`）。

`DSMIInterface` 类把这两个动态库的 4 个 C 函数包装成 Python 方法，并统一了错误处理。这和 u2-l3 讲过的 asys `common/device.py` 是同一套思路（ctypes + so + 防御式退化），可以对照阅读——两个组件各自维护了一份类似封装，属于有意的组件间解耦。

#### 4.1.2 核心流程

```text
DSMIInterface()
  ├─ ctypes.CDLL("libdrvdsmi_host.so")   # DSMI 库：设备数、芯片信息
  └─ ctypes.CDLL("libascend_hal.so")     # HAL 库：AI Core / Vector Core 数

get_device_count()
  └─ dsmi_get_device_count(&count) ─失败→ 返回 0 ─成功→ 返回 count[0]

get_chip_info(device_id)
  └─ dsmi_get_chip_info(dev, &stru) ─失败→ 返回 None ─成功→ 返回 DsmiChipInfoStru
       └─ .get_complete_platform() = chip_type + chip_name，如 "Ascend910B2"

get_vector_core_count / get_aicore_count(device_id)
  └─ halGetDeviceInfo(dev, module_type, INFO_TYPE_CORE_NUM, &num)
       ─符号不存在(AttributeError)或返回非 0→ 返回 0
```

注意一个设计取舍：DSMI 类方法失败返回 `None`/`0`（调用方需判空），HAL 类方法只打日志不抛异常——**失败永远以确定的占位值返回，不抛异常栈**，这是 msaicerr 全工具一致的代码气质。

#### 4.1.3 源码精读

**C 结构体的 Python 描述**。`DsmiChipInfoStru` 用三个 32 字节 char 数组对应驱动侧的芯片信息结构体，并提供两个解码方法：

[dsmi_interface.py:L52-L63](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L52-L63) —— `chip_type` 与 `chip_name` 拼接即为完整平台名（如 `Ascend` + `910B2`），这是 soc_version 的第一来源。

**构造时加载两个 so**：

[dsmi_interface.py:L71-L73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L71-L73) —— `ctypes.CDLL` 按名字加载驱动库（依赖 CANN 环境变量已 source，使 so 出现在动态库搜索路径中）。注意这里没有 try/except：so 加载失败会直接抛 `OSError`，由 msaicerr.py 入口处的全局 `sys.excepthook`（u3-l1 讲过）兜底置 `GLOBAL_RESULT = False`。

**设备数查询**：

[dsmi_interface.py:L75-L81](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L75-L81) —— 用 `(ctypes.c_int * 1)()` 造一个「长度为 1 的 int 数组」当出参指针，这是 ctypes 的惯用法；`restype = ctypes.c_int` 显式声明返回类型。

**统一错误翻译**：

[dsmi_interface.py:L119-L134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L119-L134) —— `_parse_error` 把驱动返回的错误码经 `DsmiErrorCode` 枚举（[L32-L49](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L32-L49)）译成可读名字打进 error 日志；枚举里没有的码走 `except ValueError` 分支打印原始值。返回 `True` 表示「有错误」，调用方据此返回 `None`/`0`。

**模块级便捷函数**：

[dsmi_interface.py:L137-L138](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L137-L138) —— `get_soc_version()` 固定查 device 0 的芯片型号。这是工具级假设：同机多卡 soc_version 一致。注意它没有判空——如果 `get_chip_info` 返回 `None`，这里会抛 `AttributeError`，最终被入口的 excepthook 捕获。

**核心数查询走 HAL 而非 DSMI**：

[dsmi_interface.py:L92-L116](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L92-L116) —— vector core 与 AI core 数量通过 `halGetDeviceInfo` 查询，`MODULE_TYPE_VECTOR_CORE = 7`、`MODULE_TYPE_AICORE = 4`、`INFO_TYPE_CORE_NUM = 3` 三个常量（[L27-L29](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/dsmi_interface.py#L27-L29)）是驱动侧的模块类型/信息类型编码。两个方法结构完全对称，仅 module_type 不同。

#### 4.1.4 代码实践

**实践目标**：验证 `get_soc_version` 在无设备机器上的失败形态，理解「失败返回占位值」的契约。

1. 操作步骤：
   - 在已 source CANN 环境（`source /usr/local/Ascend/ascend-toolkit/set_env.sh`）的机器上执行：

   ```bash
   cd src/msaicerr
   python3 -c "
   from ms_interface.dsmi_interface import DSMIInterface
   d = DSMIInterface()
   print('device count:', d.get_device_count())
   print('chip info:', d.get_chip_info(0))
   print('vector core:', d.get_vector_core_count(0))
   print('aicore:', d.get_aicore_count(0))
   "
   ```

   - 在无昇腾设备的机器上重复执行第二步会因 so 加载失败抛 `OSError`，记录报错信息。
2. 需要观察的现象：有设备时 `get_chip_info(0)` 返回结构体对象，`device count` 与 `npu-smi info` 列出的卡数一致。
3. 预期结果：无设备环境下无法完成 `CDLL` 加载（或 `get_device_count` 返回 0 并打出 DSMI 错误日志）。此实践需要真实昇腾环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`get_soc_version()` 为什么敢不判断 `get_chip_info(0)` 的返回值是否为 `None`？这个风险由谁兜底？

**答案**：因为 msaicerr.py 入口在 [msaicerr.py:L41-L50](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L41-L50) 安装了全局 `sys.excepthook`，任何未捕获异常（包括这里的 `AttributeError`）都会被 `handle_exception` 捕获，置 `utils.GLOBAL_RESULT = False` 并打印栈。工具以「全局结果标志 + 退出码」收敛，而不是层层判空。

**练习 2**：`get_vector_core_count` 里的 `except AttributeError` 防的是什么？

**答案**：`self.drvhal.halGetDeviceInfo` 在 so 中不存在该符号时，ctypes 访问会抛 `AttributeError`。捕获后打 error 日志并返回 0——对应「旧版本驱动没有这个接口」的场景，属于典型的符号级防御（asys 的 device.py 也有同款三重防御，见 u2-l3）。

**练习 3**：`DsmiChipInfoStru.get_complete_platform()` 为什么是 `chip_type + chip_name` 拼接而不是单独用 `chip_name`？

**答案**：驱动侧把型号拆成两段存放，如 `chip_type="Ascend"`、`chip_name="910B2"`，业务侧需要的 soc_version（如 `Ascend910B2`）是两者的字符串拼接，拼接后还要 `decode("UTF-8")` 把 `c_char` 数组转成 str。

---

### 4.2 utils.py：日志双通道、根因收集与全局结果

#### 4.2.1 概念说明

`utils.py` 是 msaicerr 的公共函数集，本模块聚焦其中最核心的三件事：

1. **日志双通道**：`print_info_log` 同时「上屏 + 追加写 `debug_info.txt`」，而 warn/debug 只落盘不上屏——用户屏幕上只留关键信息，完整过程留档。
2. **根因收集**：`ExceptionRootCause` 单例收集错误链，`print_error_log` 通过检查调用栈来源决定「错误进根因列表还是上屏」。
3. **全局结果标志**：模块级 `GLOBAL_RESULT = True`，任何失败路径把它置 `False`，异常钩子也置它，最终决定工具的成败判定。

这三者共同回答一个问题：**一个跑十几分钟、内部几十个尽力而为步骤的分析工具，如何向用户报告「到底哪里出了问题」**。

#### 4.2.2 核心流程

```text
print_error_log(msg)
  ├─ 检查调用栈：是否有帧来自 collection.py / aicore_error_parser.py
  │    且 ExceptionRootCause().cache_error == True
  ├─ 是 → ExceptionRootCause().add_cause(msg)   # 进根因列表，不上屏（留给最终 info.txt）
  └─ 否 → _print_log("ERROR", msg)              # 直接上屏
  └─ 最后 → _print_log_to_txt(...)              # 无条件落盘 debug_info.txt

@screen_error 装饰器
  └─ 进入时把 cache_error 置 False、退出时恢复
     → 被装饰函数内部的一切错误都强制上屏（如 test_env）

GLOBAL_RESULT
  ├─ 初始 True
  ├─ 异常钩子 handle_exception / 各失败点置 False
  └─ main() 返回值即进程退出码
```

#### 4.2.3 源码精读

**全局结果标志与单例装饰器**：

[utils.py:L42-L53](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L42-L53) —— `GLOBAL_RESULT` 是模块级布尔（模块级变量天然全局单例）；`singleton` 是用闭包字典实现的经典单例装饰器，比 asys 的 Singleton 元类更轻。

**根因收集器与屏幕错误装饰器**：

[utils.py:L56-L82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L56-L82) —— `ExceptionRootCause` 维护 `causes` 列表与 `cache_error` 开关；`@screen_error` 在函数执行期间临时把开关拨到 `False`，让该函数内的错误走「上屏」分支而不是「攒进根因列表」。这是用调用栈 + 开关双重条件做日志路由的少见手法。

**日志双通道的底层实现**：

[utils.py:L95-L117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L95-L117) —— `_print_log` 带时间戳和 PID 打到 stdout 并 flush；`_print_log_to_txt` 追加写当前工作目录下的 `debug_info.txt`（这就是 environment_check.md 文档里提到的那份日志文件）；`print_info_log` 同时调用两者。注意 warn/debug 级别只调用 `_print_log_to_txt`，不上屏。

**带调用栈检查的 print_error_log**：

[utils.py:L138-L157](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L138-L157) —— `inspect.stack()` 拿整条调用栈，只要有一帧来自 `collection.py` 或 `aicore_error_parser.py`（即解析主链路），且开关打开，错误就进根因列表（最终汇总进结果 `info.txt`）；否则直接上屏。无论走哪条分支，都会落盘。

**路径校验三件套**（供 `-p`/`-d`/`-out` 等参数使用）：

[utils.py:L160-L216](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L160-L216) —— `check_path_special_character` 先拦空串、空格和特殊字符集；`check_path_valid` 按「输出目录不存在则创建 → 存在性 → 可读 → （目录）可写 → 文件/目录类型」的顺序逐项检查，任何一步失败都 `print_error_log` + 抛 `AicErrException(错误码)`。注意它被 `@screen_error` 装饰——参数错误必须立刻上屏，而不是攒进根因。

**命令执行**：

[utils.py:L219-L260](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L219-L260) —— `execute_command` 用 `SpooledTemporaryFile` 接住子进程 stdout/stderr 并返回 `(returncode, output)`，`file_out` 参数还能把 stdout 落成权限收紧的文件；`run_cmd_output` 是 shell=True 的轻量版，返回布尔。这两者是 u3-l2 报告解析中 grep 提取的执行底座（`get_inquire_result`，[L421-L430](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L421-L430)）。

#### 4.2.4 代码实践

**实践目标**：亲手观察「日志双通道」和「根因收集 vs 上屏」的路由差异。

1. 实践目标：验证同一句 `print_error_log` 在不同调用栈/开关状态下走不同分支。
2. 操作步骤：在 `src/msaicerr/` 下新建临时脚本 `tmp_log_demo.py`（示例代码，验证完删除）：

   ```python
   # 示例代码
   import sys
   sys.path.insert(0, '.')
   from ms_interface import utils

   utils.print_info_log("这条 info 会同时出现在屏幕和 debug_info.txt")
   utils.print_error_log("顶层调用：无 collection/aicore 栈帧 → 直接上屏")

   @utils.screen_error
   def fake_entry():
       utils.print_error_log("被 screen_error 装饰的函数内 → 强制上屏")
   fake_entry()

   rc = utils.ExceptionRootCause()
   rc.add_cause("手动塞进根因列表的一条错误")
   print("根因列表当前内容：\n" + rc.format_causes())
   ```

   执行 `python3 tmp_log_demo.py`，然后 `cat debug_info.txt`。
3. 需要观察的现象：屏幕上应出现两条 ERROR（顶层 + screen_error 内）；`debug_info.txt` 里 INFO/WARNING/ERROR 全量都在；根因列表只有手动塞入的一条。
4. 预期结果：确认「上屏有条件、落盘无条件」。本实践不依赖 NPU 设备，普通 Python 3 环境即可验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `print_warn_log` 不上屏而 `print_info_log` 上屏？

**答案**：设计上把 INFO 当作用户需要跟进的过程信息（如 "Total device count: 1"、"Get soc_version: xxx"），而 WARNING/DEBUG 属于排障细节，全部留在 `debug_info.txt` 中按需查看，避免长流程刷屏。

**练习 2**：`debug_info.txt` 写在哪个目录？入口处对此做了什么防御？

**答案**：写在执行 msaicerr.py 时的当前工作目录。入口 [msaicerr.py:L250-L253](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L250-L253) 在解析参数后检查当前目录可写、且已存在的 `debug_info.txt` 也可写，否则直接返回路径错误码——因为所有日志函数都依赖这个文件，它写不进等于全盲。

**练习 3**：`singleton` 装饰器和模块级 `GLOBAL_RESULT` 各自解决什么单例问题？

**答案**：`singleton` 装饰器让 `ExceptionRootCause` 这类需要跨模块共享状态的对象只实例化一次（`utils.ExceptionRootCause()` 在任何文件里调用拿到同一实例）；`GLOBAL_RESULT` 则更简单——模块本身在 Python 中只被 import 一次，模块级变量天然全局唯一，适合做布尔标志位。

---

### 4.3 -e 环境检查全链路：test_env 与 golden op

#### 4.3.1 概念说明

`-e` 模式的检查思路不是「逐项比对版本号清单」，而是**端到端活性测试**：拉起一个内置样例算子（golden op），让它真实走完「算子编译 → 核心数查询 → 下发执行 → 结果日志检查」。这条链路隐式覆盖了以下检查项：

| 检查项（隐式/显式） | 覆盖方式 | 对应函数 |
| --- | --- | --- |
| CANN 环境变量已 source | 显式：入口检查 `ASCEND_OPP_PATH` | [msaicerr.py:L239-L242](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L239-L242) |
| 当前目录可写（日志落盘） | 显式 | [msaicerr.py:L250-L253](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L250-L253) |
| 驱动可用、设备总数 | 显式：DSMI 查询 | `DSMIInterface.get_device_count`，经 [msaicerr.py:L148-L153](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L148-L153) |
| device_id 在范围内 | 显式 | `verify_device_id` + [msaicerr.py:L67-L73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L67-L73) |
| 芯片型号识别 | 显式：打印 soc_version | `get_soc_version`，[msaicerr.py:L161-L162](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L161-L162) |
| CANN 算子编译工具链（atc/ccec） | 隐式：golden op 编译能否产出 json | `get_compile_file`，[golden_op.py:L41-L44](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py#L41-L44) |
| soc_version 与实际芯片匹配 | 隐式：编译产物缺失即报不匹配 | [aicore_error_parser.py:L1269-L1276](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1269-L1276) |
| 运行时与驱动版本、设备内存 | 隐式：算子下发执行是否成功 | [golden_op.py:L55-L59](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py#L55-L59) |
| 硬件有无 AI Core Error | 隐式：执行后扫设备日志 | `search_aicerr_log`，[aicore_error_parser.py:L1282-L1289](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1282-L1289) |

#### 4.3.2 核心流程

```text
python3 msaicerr.py -e [-dev N]
  └─ main() 分发（-d > -p > -e 优先级，u3-l1 已讲）
       └─ test_env(device_id)
            ├─ ① check_device_valid → verify_device_id → DSMIInterface.get_device_count  # 设备数与卡号
            ├─ ② get_soc_version → DSMIInterface.get_chip_info(0)                        # 芯片型号
            └─ ③ AicoreErrorParser.run_test_env(soc_version, device_id)
                 ├─ 构造环境变量：ASCEND_SLOG_PRINT_TO_STDOUT=0、ASCEND_PROCESS_LOG_PATH=golden_op_目录
                 ├─ subprocess 拉起 python3 golden_op.py <soc_version> <device_id> <临时编译目录>
                 │    └─ GoldenOp.run_golden_op：
                 │         ├─ get_compile_file：编译 AddCustom 样例算子（校验 CANN 工具链 + soc 匹配）
                 │         ├─ get_block_dim：vector core 数为 0 则退回 aicore 数
                 │         └─ AscendOpKernelRunner 下发执行（校验运行时/驱动/内存）
                 ├─ 子进程退出码非 0 → False
                 ├─ 编译目录无 *_ADD_Custom*.json → False（soc_version 不匹配或工具链缺）
                 ├─ 从 json 取 kernelName，扫 golden_op 日志有无该 kernel 的 AI Core Error → 有则 False
                 └─ 清理临时目录 → True
       └─ 成功 → MS_AICERR_NONE_ERROR(0)；失败 → MS_AICERR_HARDWARE_ERR(103)
```

#### 4.3.3 源码精读

**入口分发与 `-e` 参数声明**：

[msaicerr.py:L222-L224](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L222-L224) —— `-e/--env` 是 `store_true` 开关；[L255-L260](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L255-L260) 按 `-d > -p > -e` 优先级分发，`-e` 落到 `test_env(args.device_id)`。`-dev` 参数通过 `RequireOtherArgs`（[L229-L233](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L229-L233)）约束为只能搭配 `-p` 或 `-e` 使用（u3-l1 已讲）。

**设备号校验**：

[msaicerr.py:L148-L153](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L148-L153) 与 [msaicerr.py:L67-L73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L67-L73) —— `verify_device_id` 先打印总设备数（文档输出示例的第一行 `[INFO] Total device count: 1` 就来自这里），再检查 `0 <= device_id < total`；`check_device_valid` 包装它并打印 `Valid device_id 0`。

**test_env 主体**：

[msaicerr.py:L156-L176](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L156-L176) —— 注意整个函数被 `@screen_error` 装饰：环境检查是用户主动运行的自检命令，其中任何错误都必须**立即上屏**而不是攒进根因列表——这正是 4.2 模块讲的装饰器在这里的实际用途。三步：设备号校验 → 取 soc_version → 跑 golden op；成功返回 `MS_AICERR_NONE_ERROR`（0），失败返回 `MS_AICERR_HARDWARE_ERR`（103，定义于 [constant.py:L56](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L56)）。`except BaseException` 兜住一切异常（包括 Ctrl+C 之外的所有退出路径）。

**run_test_env：拉起子进程并做三重判定**：

[aicore_error_parser.py:L1251-L1292](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1251-L1292) —— 用时间戳生成互不冲突的临时编译目录和 golden_op 日志目录；复制环境变量并追加三个：`ASCEND_SLOG_PRINT_TO_STDOUT=0`（日志不刷屏）、`ASCEND_PROCESS_LOG_PATH`（设备日志定向到 golden_op 目录，供后续扫错）、`PYTHONPATH`（让子进程能 import ms_interface）。随后 `subprocess.run` 拉起 golden_op.py，返回码非 0 即失败。成功后在编译目录 `rglob` 查找 `AddCustom*.json`，找不到说明编译没成功（soc_version 不匹配或 atc/ccec 缺失）；找到则读出 `kernelName`，调 `search_aicerr_log` 扫设备日志确认执行期间没有发生 AI Core Error。最后清理临时目录。

**golden op 本体**：

[golden_op.py:L40-L59](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py#L40-L59) —— `run_golden_op` 编译并执行 `AddCustom` 算子：输入是两个 256×32 的全 1 float16 矩阵，输出 16384 字节。`get_block_dim`（[L32-L38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py#L32-L38)）先查 vector core 数作为 block_dim，查不到（返回 0，即「AI Core + Vector 合体」的老架构芯片）退回 AI core 数——直接复用了 4.1 模块的 HAL 查询。执行结果文本含 "Execute single op case failed" 即失败。

**子进程入口契约**：

[golden_op.py:L62-L69](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/golden_op.py#L62-L69) —— 以 `__main__` 方式运行，命令行参数按位置传入 soc_version、device_id、临时目录，成功退出 0、失败退出 -1。父进程只看退出码 + 编译产物，两进程间无其他 IPC——简单的「退出码 + 文件」交接。

#### 4.3.4 代码实践

**实践目标**：在真实（或纸面模拟）环境运行 `python3 msaicerr.py -e -dev 0`，把每行输出对应到源码函数，形成「检查项 → 函数」对照表。

1. 操作步骤：
   - source CANN 环境后，在 `src/msaicerr/` 下执行 `python3 msaicerr.py -e -dev 0`。
   - 逐行记录输出（对照 docs/zh/msaicerr/environment_check.md 的输出示例）。
   - 打开 `debug_info.txt`，对比屏幕输出与落盘内容的差异。
   - 再执行一次 `python3 msaicerr.py -e -dev 999`（越界卡号），观察失败分支输出与退出码：`echo $?` 应为 1（`MS_AICERR_INVALID_PARAM_ERROR`）。
2. 需要观察的现象：
   - `[INFO] Total device count: N` ← `verify_device_id`（msaicerr.py:150）
   - `[INFO] Valid device_id 0` ← `check_device_valid`（msaicerr.py:72）
   - `[INFO] Get soc_version: xxx` ← `test_env` 第②步（msaicerr.py:162）
   - `[INFO] Start to test env with golden op.` ← `test_env` 第③步前（msaicerr.py:163）
   - 成功时 `[INFO] The built-in sample operator runs successfully...` ← msaicerr.py:166-168
   - 执行目录下出现又消失的 `temp_<时间戳>` / `golden_op_<时间戳>` 临时目录 ← run_test_env 的创建与清理（aicore_error_parser.py:1252-1254、1280-1291）
3. 预期结果：整理出 4.3.1 表格的完整「输出行 → 函数 → 链路文件」映射。需要真实昇腾设备 + CANN 环境；无设备时可在纸面上按上面的调用链完成对照，标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：golden op 编译成功、执行也返回 0，但 `run_test_env` 仍可能判定失败，是哪一步拦下的？

**答案**：`search_aicerr_log` 那一步（aicore_error_parser.py:1282-1289）：从编译产物 json 取出 `kernelName`，在 `ASCEND_PROCESS_LOG_PATH` 定向的 golden_op 目录日志中搜索该 kernel 的 AI Core Error 记录——即使算子「跑完」了，只要设备日志里有硬件错误痕迹也算环境异常，返回 False → 退出码 103。

**练习 2**：`test_env` 为什么加 `@screen_error`，而 `-p` 模式的 `analyse_report_path` 不加？

**答案**：`-e` 是用户主动发起的自检，用户此刻唯一关心的就是「哪里坏了」，错误必须实时上屏；而 `-p` 是长流程分析，中途的错误更适合被 `ExceptionRootCause` 收集、在最终 `info.txt` 里汇总呈现，避免几十条错误刷屏淹没重点。

**练习 3**：环境检查没有显式比对「CANN 版本号 ≥ 某版本」，这算缺陷吗？

**答案**：不算，是有意的设计取舍。静态版本比对清单维护成本高且容易漏；golden op 端到端跑通即证明「当前这套驱动 + CANN + 芯片 + 依赖」组合可用，版本不兼容会在编译失败（无 json 产物）或执行失败处自然暴露，且错误日志（debug_info.txt / golden_op 目录）会指向具体原因。代价是失败时定位信息比显式清单粗——所以失败分支的 error 文案里列出了可能原因（芯片不兼容、驱动问题、依赖缺失）。

## 5. 综合实践

**任务：给 `-e` 环境检查写一份「检查项溯源文档」。**

1. 通读 `msaicerr.py` 的 `main()` → `test_env()` 与 `aicore_error_parser.py` 的 `run_test_env()`，把 4.3.1 的表格扩充为完整文档，每个检查项写清：触发条件、对应源码文件与行号、失败时的输出文案、对应的退出码。
2. 对每个失败分支，在 `debug_info.txt` 中找到（或推演）会留下哪条日志，说明用户拿到退出码 1 / 103 时分别应该先查什么。
3. 进阶：对照 u2-l3 讲过的 asys `common/device.py` 与本讲 `dsmi_interface.py`，写一段 200 字左右的对比：两者在「ctypes 用法、so 加载时机、失败防御」上的异同（提示：asys 有 LoadSoType 单例缓存与 NOT_SUPPORT 占位值三重防御；msaicerr 构造函数直接 CDLL、靠全局 excepthook 兜底）。

产出是一份 Markdown 文档，可作为你团队内 msaicerr 排障的速查表。

## 6. 本讲小结

- `dsmi_interface.py` 用 ctypes 把 `libdrvdsmi_host.so`（设备数、芯片型号）和 `libascend_hal.so`（AI/Vector Core 数）包装成 4 个 Python 方法，失败统一返回 `None`/`0` 占位值，错误码经 `DsmiErrorCode` 枚举译成可读日志。
- `utils.py` 的日志是双通道：INFO 与部分 ERROR 上屏，全量级别追加写当前目录 `debug_info.txt`；`print_error_log` 用 `inspect.stack()` 检查调用栈来源，决定错误进 `ExceptionRootCause` 根因列表还是直接上屏，`@screen_error` 装饰器可强制后者。
- `GLOBAL_RESULT` 模块级标志 + 入口 `sys.excepthook` 共同构成「永不静默失败」的兜底网，`main()` 返回值即进程退出码。
- `-e` 环境检查走 `test_env()` → `run_test_env()` → golden_op 子进程的端到端活性测试：显式检查设备数/卡号/soc_version，隐式检查 CANN 工具链、soc 匹配、运行时与设备日志中的 AI Core Error。
- `test_env` 加 `@screen_error` 是「自检命令错误必须实时上屏」的场景化应用，与 `-p` 长流程「错误攒进根因列表」形成对照。

## 7. 下一步学习建议

本讲完成了 msaicerr 单元（u3）的全部内容。接下来进入 u4 单元「msprof：性能调优工具源码解析」，建议先读 u4-l1（msprof 总体架构），重点体会它与 msaicerr 的形态差异：C++ collector + Python 分析 wheel 的分离架构，以及 profapi 插件体系如何解决「多数据源接入」问题。如果你计划做二次开发，可以先回头对照 u6-l4 的三个扩展点，思考「为 msaicerr 新增一个环境检查项」应该改哪些文件（提示：入口无改动，主要在 `test_env`/`run_test_env` 链路上）。
