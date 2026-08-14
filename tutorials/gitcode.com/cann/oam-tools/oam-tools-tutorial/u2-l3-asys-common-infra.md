# asys 公共设施：日志、常量、设备与命令执行

## 1. 本讲目标

上一讲（u2-l2）我们看清了 asys 的命令行体系：Arg 枚举描述参数、ArgChecker 校验、ParamDict 全局传参。那些"业务味道很浓"的模块之所以能写得简洁，是因为它们脚下踩着一层公共设施——`src/asys/common/` 目录。

学完本讲，你应该能够：

1. 掌握 `log.py` 的日志封装，理解 `log_info` / `log_error` / `close_log` / `open_log` 的设计意图，特别是 `force` 参数与"关日志"机制。
2. 掌握 `const.py` 中的 `RetCode` 返回码枚举、`Singleton` 元类和 `Constants` 常量类，理解 asys 统一的返回值与常量管理方式。
3. 理解 `cmd_run.py` 中 5 种外部命令执行方式的差异（拿字符串、拿布尔、拿实时输出……），能按场景选对函数。
4. 理解 `device.py` 如何用 `ctypes` 直接调用驱动 so 库（DSMI 接口）查询 NPU 设备信息，而不是每次都去拼接 `npu-smi` 命令。
5. 了解 `file_operate.py` 的文件操作工具类，作为后续阅读 collect 子系统（u2-l5）的铺垫。

## 2. 前置知识

本讲需要几个 Python 与系统层面的基础概念，用通俗语言解释：

- **logging 模块与全局日志级别**：Python 标准库 `logging` 提供 `debug/info/warning/error` 四级日志。`logging.disable(某个级别)` 可以"压制"该级别及更低级别的所有日志——这是 asys 实现"关日志"的核心手段。
- **`threading.RLock`（可重入锁）**：同一个线程可以重复获取的锁。asys 的日志函数在多线程采集场景下会被并发调用，加锁保证两条日志不会交叉打印成乱码。
- **subprocess 模块**：Python 在子进程中执行 shell 命令的标准方式。`subprocess.run` 会等命令结束并拿到返回码；`subprocess.Popen` 拿到一个"活"的进程对象，可以逐行读取输出——这就是"实时输出"的实现基础。
- **ctypes 与 so 动态库**：`ctypes` 是 Python 的外部函数接口，`ctypes.cdll.LoadLibrary("libxxx.so")` 可以直接加载 C/C++ 动态库并调用其中的 C 函数。NPU 驱动提供的 DSMI（Device System Management Interface）接口就是一组 so 库里的 C 函数，`device.py` 用 `ctypes.Structure` 描述 C 结构体、用 `ctypes.pointer` 传指针，完成 Python 与驱动的对话。
- **单例模式（Singleton）**：保证一个类全局只有一个实例。asys 用"元类（metaclass）"方式实现，写法在 `const.py` 里，本讲会精读。
- **返回码枚举**：工具类程序通常用整数码表示退出状态（0 = 成功）。asys 把所有返回码收进一个枚举，避免各处硬编码 `return 1` / `return 2` 语义不清。

## 3. 本讲源码地图

| 文件 | 作用 | 一句话概括 |
| --- | --- | --- |
| [src/asys/common/log.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py) | 日志封装 | 4 个级别的日志函数 + 关闭/恢复日志的开关 |
| [src/asys/common/const.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py) | 常量与返回码 | `RetCode` 枚举、`Singleton` 元类、`Constants` 常量类、各种魔法数字 |
| [src/asys/common/cmd_run.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py) | 外部命令执行 | 5 种执行 shell 命令的函数，对应 5 种"拿结果"的方式 |
| [src/asys/common/device.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py) | NPU 设备信息 | 用 ctypes 调 DSMI/HAL/AML 接口查芯片、健康、频率、内存等 |
| [src/asys/common/file_operate.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py) | 文件操作 | `FileOperate` 静态工具类：检查、创建、拷贝、移动、读写文件 |
| [src/asys/drv/env_type.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/drv/env_type.py) | so 库加载（辅助） | `LoadSoType` 单例负责加载 `libdrvdsmi.so` 等驱动库，是 `device.py` 的依赖 |

注意一个细节：这些模块之间的 import 用的是 `from common.log import ...` 这种"从 common 开始"的路径，说明 asys 运行时 `src/asys/` 目录被加进了 `sys.path`（这就是为什么可以直接 `python3 asys.py` 启动）。自己写脚本实践时也要先把 `src/asys` 加入路径，否则 import 会失败。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**日志封装**、**常量与返回码**、**外部命令执行**、**设备信息封装**。

### 4.1 日志封装：log.py

#### 4.1.1 概念说明

运维工具对日志有两条互相矛盾的要求：

1. 排障时希望日志越多越好（debug 级别全开）；
2. 展示时希望终端干净——回顾 u2-l1 讲过的现象：`asys info` / `asys health` 这类"纯展示命令"要直印结果到终端，此时过程中产生的 info/warning 日志反而是噪音。

asys 的解法是：不搞复杂的日志框架，直接用标准 `logging` 的全局 disable 机制当开关——展示型命令开头调用 `close_log()` 关掉低级别日志，过程日志全部静音。

#### 4.1.2 核心流程

`_log()` 是所有日志函数的公共入口，流程如下：

```text
log_info(msg) ──► _log(logging.info, msg, force)
                     │
                     ├─ 拿 _LOG_LOCK（线程安全）
                     │
                     ├─ force=False ──► 直接调用 log_func(msg)
                     │
                     └─ force=True ──► 暂存当前 disable 级别
                                       ──► logging.disable(NOTSET)（临时全开）
                                       ──► 调用 log_func(msg)
                                       ──► finally 恢复原 disable 级别
```

`force=True` 的语义是"即使全局日志已被 close_log 关闭，这条也必须打出来"。典型用途：错误提示信息一定要让用户看见，不能被静音误伤。

#### 4.1.3 源码精读

日志格式与模块级初始化，只执行一次：

- [src/asys/common/log.py:25-27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L25-L27) — 定义统一的日志格式 `时间 [ASYS] [级别]: 消息`，调用 `logging.basicConfig` 完成 root logger 初始化，并创建一把可重入锁 `_LOG_LOCK`。这就是为什么业务代码 `import` 本模块即可用，无需再配置。

带 force 语义的核心分发函数：

- [src/asys/common/log.py:30-41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L30-L41) — `_log()` 先加锁；`force=False` 时直接输出；`force=True` 时保存 `logging.getLogger().manager.disable`（当前压制级别），临时 `logging.disable(logging.NOTSET)` 全开，输出后 `finally` 恢复，保证不破坏调用方设定的日志状态。

对外暴露的 5 个接口：

- [src/asys/common/log.py:44-57](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L44-L57) — `log_debug`、`log_info(force)`、`log_warning(force)`、`log_error` 四个级别函数，全部委托给 `_log`。注意 `log_error` 没有 force 参数——error 级别本就高于 close_log 压制的级别，天然不会被静音。

日志开关：

- [src/asys/common/log.py:60-69](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L60-L69) — `open_log()` 恢复所有日志（`disable(NOTSET)`）；`close_log()` 依次 `disable` INFO、DEBUG、WARNING 三个级别，之后这三级日志全部静音。对照 u2-l1 讲过的"纯展示命令提前 close_log()"，这里就是它的实现。

#### 4.1.4 代码实践

1. **实践目标**：亲手体验 close_log / force 的效果。
2. **操作步骤**：在仓库根目录写一个临时脚本（示例代码，不属于项目）：

   ```python
   import sys
   sys.path.insert(0, 'src/asys')   # 让 `from common.log import ...` 可用
   from common.log import log_info, log_error, close_log, open_log

   log_info("1: 正常状态，这条会打印")
   close_log()
   log_info("2: 已关日志，这条不会打印")
   log_info("3: force=True，关日志也能打印", force=True)
   log_error("4: error 级别，不会被 close_log 静音")
   open_log()
   log_info("5: 重新打开，这条又会打印")
   ```

   运行 `python3 tmp_log_demo.py`。
3. **需要观察的现象**：第 2 条消失，第 1、3、4、5 条出现在终端，且都带 `[ASYS] [级别]` 前缀。
4. **预期结果**：输出共 4 行日志，验证了 close_log 只压制 INFO/DEBUG/WARNING，而 `force=True` 与 error 级别可以穿透静音。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_LOG_LOCK` 用 `RLock` 而不是普通 `Lock`？

**答案**：`RLock` 允许同一线程重复加锁。`_log()` 在 `with _LOG_LOCK` 块内又调用了 `log_func`，如果日志系统内部（如自定义 handler）再回调到这些函数，普通 `Lock` 会死锁，`RLock` 则可重入。（同时多线程采集场景下加锁也防止日志行交叉。）

**练习 2**：`asys health` 执行过程中 log_info 的调用为什么不会污染终端输出？

**答案**：回顾 u2-l1，health 属于纯展示命令，入口在真正执行前调用 `close_log()` 压制了 INFO 及以下级别，过程日志全部静音；若确有必须让用户看到的信息，用 `log_info(msg, force=True)` 或 `log_error` 穿透。

### 4.2 常量与返回码：const.py

#### 4.2.1 概念说明

`const.py` 是 asys 的"字典本"，收纳三类东西：

1. **魔法数字的具名化**——设备号上限、频率类型编号、检测超时秒数等散落的数字，全部集中定义；
2. **返回码 `RetCode`**——统一退出状态枚举，注意校验类错误码（`ARG_*`）与 u2-l2 讲过的 ArgChecker 是配套的；
3. **基础设施类**——`Singleton` 元类与 `Constants` 常量类。

#### 4.2.2 核心流程

`Singleton` 元类的原理：普通类通过 `类名()` 创建实例时会调用元类的 `__call__`，Singleton 在这里拦截——第一次真正创建并缓存到 `_instances` 字典，之后每次都返回缓存的那一个：

```text
ParamDict() ──► Singleton.__call__
                 ├─ _instances 里没有 ──► 真正实例化并缓存
                 └─ _instances 里已有 ──► 直接返回缓存实例
```

#### 4.2.3 源码精读

业务常量集中区（节选）：

- [src/asys/common/const.py:22-25](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L22-L25) — 并发进程数 `PROCESSES_NUMBER`、设备号合法区间 `DEVICE_ID_MIN/MAX`（0~63，即最多 64 卡）。u2-l2 的 ArgChecker 校验设备号范围时用的依据就在这类常量里。

单例元类：

- [src/asys/common/const.py:138-151](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L138-L151) — `Singleton` 元类。`__call__` 按"类"为键缓存实例；额外提供 `clear()` 类方法删除缓存，主要用于单元测试——测试之间需要重置单例状态（例如清空 ParamDict 里上一次用例塞入的参数），避免用例相互污染。

返回码枚举：

- [src/asys/common/const.py:154-166](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L154-L166) — `RetCode`：`SUCCESS=0`、`FAILED=1` 两个通用码，加上 `ARG_PATH_INVALID`、`ARG_NO_EXECUTABLE` 等 9 个参数校验错误码。这些码与 u2-l2 的 ArgChecker 校验函数一一呼应：路径非法返回 2、目录不存在返回 6、命令不可执行返回 7……asys 的进程退出码语义全部出自这一处。

Constants 常量类与模块级实例：

- [src/asys/common/const.py:206-249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L206-L249) — `Constants` 用 `@property` 暴露 9 个子命令名字符串（含 `help`），`cmd_set` 属性把它们汇总成列表。文件末尾 `consts = Constants()` 创建模块级实例，业务代码 `from common.const import consts` 后用 `consts.collect_cmd` 这样的写法取值——u2-l1 讲过的 asys.py 主流程正是拿它和命令行输入比对。

#### 4.2.4 代码实践

1. **实践目标**：验证 Singleton 的"全局唯一"与 clear() 的可重置。
2. **操作步骤**：延续 4.1.4 的脚本目录设置，运行以下示例代码：

   ```python
   import sys
   sys.path.insert(0, 'src/asys')
   from common.const import Singleton

   class Config(metaclass=Singleton):
       def __init__(self):
           self.items = []

   a, b = Config(), Config()
   print(a is b)          # 预期 True
   Singleton.clear(Config)
   c = Config()
   print(a is c)          # 预期 False，clear 后产生了新实例
   ```

3. **需要观察的现象**：两次打印分别是 `True` 和 `False`。
4. **预期结果**：第一次证明单例生效；第二次证明 `clear()` 清掉缓存后，下一次实例化是全新对象——这正是 UT 框架在每个用例间重置 ParamDict 的手段。

#### 4.2.5 小练习与答案

**练习 1**：`RetCode.ARG_NO_EXECUTABLE = 7` 对应 u2-l2 中的哪个校验场景？

**答案**：对应 ArgChecker 中"命令行传入了要求可执行的命令（如 launch 的业务命令）但系统 PATH 中找不到该命令"的校验失败。asys 用统一返回码 7 退出，用户与上层脚本可凭码定位是"命令不存在"而非其他错误。

**练习 2**：为什么 `Constants` 用 `@property` 而不是直接定义类属性字符串？

**答案**：功能上等价，property 写法把"取子命令名"统一为方法调用形式，便于未来在不改调用方的情况下加入计算逻辑（如大小写归一化）；同时 `consts.collect_cmd` 的访问语法与普通属性一致，调用方无感知。这是一个风格取舍，读者写自己的工具时选类属性即可，更简单。

### 4.3 外部命令执行：cmd_run.py

#### 4.3.1 概念说明

asys 大量依赖外部命令收集信息：`npu-smi info`、`cat /dev/...`、各种 shell 工具。`cmd_run.py` 按调用方"想要什么形态的结果"提供了 5 个函数：

| 函数 | 返回值 | 适用场景 |
| --- | --- | --- |
| `run_command` | `str`（成功返回 stdout，失败返回错误串或 'NONE'） | 只关心命令输出文本，如查询版本号 |
| `run_cmd_output` | `[bool, str]`（成功标志 + 输出） | 既要知道成败又要拿到原文 |
| `run_linux_cmd` | `bool`（可附带 stdout 与期望值比对） | 判断型检查，如"某服务是否运行" |
| `real_time_output` | `bool`（过程逐行透传到终端） | 复跑业务进程，用户要看实时日志（launch 场景） |
| `popen_run_cmd` | `str`（静默吞掉 stderr） | 容忍失败的探测式命令，避免噪音 |

另有一个辅助函数 `check_command`：用 `which`/`where` 判断命令是否存在，是 u2-l2 ArgChecker 校验"可执行命令"的底层支撑。

#### 4.3.2 核心流程

以最常用的 `run_command` 为例：

```text
run_command(cmd)
  └─ subprocess.run(cmd, shell=True, 捕获 stdout/stderr, encoding='utf-8')
       ├─ returncode == 0 且 stderr 为空 ──► 返回 stdout.strip()
       ├─ returncode == 0 但 stderr 非空 ──► 返回 'NONE'（视为无有效数据）
       └─ returncode != 0
            ├─ stderr 含 'not found'   ──► 返回 'NONE'（命令不存在不算致命）
            └─ 否则                     ──► log_debug 记录失败细节，返回 stderr 摘要
```

关键设计：**这些函数永远不抛异常、不返回 None**，失败也有确定的字符串形态（`'NONE'` 或错误摘要），调用方无需层层 try/except。这是采集类代码的典型防御风格——单个命令失败不应该让整个采集流程崩溃。

`real_time_output` 则走另一条路：用 `Popen` 拿到活进程，逐行 `for line in process.stdout` 边收边写终端，最后 `wait()` 收返回码。

#### 4.3.3 源码精读

- [src/asys/common/cmd_run.py:33-43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L33-L43) — `check_command`：按操作系统选择 `which` 或 `where`，用返回码判断命令是否在 PATH 中，返回布尔值。

- [src/asys/common/cmd_run.py:57-69](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L57-L69) — `run_command`：核心执行函数。注意三个细节——`shell=True` 支持管道等 shell 语法；`encoding='utf-8'` 与 `env=os.environ` 保证中文与环境变量（如 `LD_LIBRARY_PATH`）正常；失败分支用 `log_debug` 记录（不打扰终端），错误串中的换行替换为空格避免污染单行展示。

- [src/asys/common/cmd_run.py:72-80](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L72-L80) — `run_cmd_output`：与 `run_command` 同构，但返回 `(bool, str)` 元组，成败显式化，适合"失败也要看 stderr 内容"的调用方。

- [src/asys/common/cmd_run.py:83-91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L83-L91) — `real_time_output`：`Popen` + `bufsize=1` + `universal_newlines=True` 实现行缓冲的逐行读取，边读边 `sys.stdout.write` 透传。asys launch 复跑用户训练任务时，业务日志能实时滚动，靠的就是它。

- [src/asys/common/cmd_run.py:94-116](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L94-L116) — `_IgnoreStderr` 上下文管理器 + `popen_run_cmd`：进入时把 fd 2（stderr）重定向到 `/dev/null`，退出时恢复。这样 `os.popen` 执行命令时第三方库打印的告警不会泄漏到终端，返回值仍是 stdout 文本。

#### 4.3.4 代码实践

见本讲第 5 节综合实践（实践任务的核心就是组合使用 log 与 cmd_run 执行 `npu-smi info`）。此处可以先做一个最小热身（示例代码）：

```python
import sys
sys.path.insert(0, 'src/asys')
from common.cmd_run import run_command, check_command

print(check_command("npu-smi"))        # 有昇腾环境为 True；无环境为 False
print(run_command("uname -r"))         # 返回内核版本字符串
print(repr(run_command("ls /not/exist")))  # 失败路径：观察返回的是错误摘要而非异常
```

观察第三行：返回的是 stderr 摘要字符串而不是抛异常——这就是"永不崩溃"的防御风格。**待本地验证**（不同环境输出不同）。

#### 4.3.5 小练习与答案

**练习 1**：要在采集脚本里执行 `npu-smi info | grep -i error` 并拿到文本，选哪个函数？为什么？

**答案**：`run_command`。命令含管道符，需要 `shell=True`（该模块所有函数均如此）；只关心输出文本、不在乎显式成败标志，`run_command` 返回 `str` 最贴合。

**练习 2**：`run_command` 在什么情况下返回字符串 `'NONE'`？这样设计有什么好处和隐患？

**答案**：两种情况：returncode 为 0 但 stderr 非空；或失败且 stderr 含 `not found`（命令不存在）。好处是调用方拿到的永远是字符串，可用统一的 `== 'NONE'` 或关键字判断"无数据"，不需要异常处理。隐患是真实输出恰好等于 `NONE` 时会被误判（概率极低但存在），且丢失了精确返回码——需要精确信息时应改用 `run_cmd_output`。

### 4.4 设备信息封装：device.py

#### 4.4.1 概念说明

查询 NPU 状态有两条路：一是拼 `npu-smi info` 命令再解析文本（脆弱、慢、格式随版本变），二是直接调用驱动暴露的 C 接口。`device.py` 选择第二条路——用 `ctypes` 加载驱动 so 库，按 C ABI 传结构体指针，一次调用拿到强类型数据。

它依赖 `src/asys/drv/env_type.py` 的 `LoadSoType` 单例来加载四类库：

| 句柄 | so 库 | 提供的能力 |
| --- | --- | --- |
| `dsmi_handle` | `libdrvdsmi.so`（EP 环境为 `libdrvdsmi_host.so`） | 设备数量、健康状态、温度、功耗、频率、利用率、HBM、错误码 |
| `hal_handle` | `libascend_hal.so` | 芯片型号信息、AI Core/AICPU/CCPU 核数、物理 ID 映射 |
| `ascend_ml` | `libascend_ml.so`（仅 toolkit 包） | CPU/AICore/Bus/HBM 电压频率 |
| `ascend_cl` | AscendCL | NPU 架构号（`aclrtGetDeviceInfo`） |

#### 4.4.2 核心流程

每个查询方法的套路高度一致，以查温度为例：

```text
get_device_temperature(device_id)
  1. p_temperature = ctypes.pointer(ctypes.c_int())   # 准备一个 C int 指针当"出参"
  2. ret = dsmi_handle.dsmi_get_device_temperature(
              c_int32(device_id), p_temperature)       # 调 C 函数，指针传入
  3. 异常兜底：AttributeError（so 里没有该符号）──► 返回 NOT_SUPPORT
  4. check_status(ret)：ret != 0 ──► log_debug 记录 + 返回 NOT_SUPPORT
  5. 读取 p_temperature.contents.value 即温度值
```

三条防御线值得记住：**so 加载失败**（`LoadSoType` 返回 `RetCode.FAILED`）、**符号不存在**（`AttributeError`）、**调用返回非 0**（DSMI 错误码），全部退化为 `NOT_SUPPORT`（`'-'`）而非崩溃——与 cmd_run 的哲学一致：采集工具任何单项失败都不能中断整体。

#### 4.4.3 源码精读

DSMI 错误码字典与 C 结构体定义：

- [src/asys/common/device.py:51-79](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L51-L79) — `DSMI_ERROR_CORE` 把驱动返回码映射为人话（1 = 设备不存在、6 = 内存不足……），用于日志排查。
- [src/asys/common/device.py:114-121](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L114-L121) — `DsmiHBMInfoStru`：用 `ctypes.Structure` 的 `_fields_` 精确复刻 C 侧 HBM 信息结构体的内存布局（总容量、频率、已用、温度、带宽利用率），字段顺序与类型必须和驱动头文件一字不差，否则读到的是错位数据。

DeviceInfo 类的构造与通用校验：

- [src/asys/common/device.py:185-201](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L185-L209) — `DeviceInfo.__init__` 通过 `LoadSoType` 单例拿四个库句柄；`check_status` 静态方法：返回 0 通过，非 0 则拼上 `DSMI_ERROR_CORE` 的描述打 debug 日志并判失败。所有查询方法都复用它。`get_device_info_loop` 则是"遍历所有设备取第一个有效值"的小工具。

典型查询方法三例：

- [src/asys/common/device.py:341-355](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L341-L355) — `get_device_health`：调 `dsmi_get_device_health`，把 0/1/2/3 映射为 Healthy/Warning/Alarm/Critical。asys health 子命令的结论就来自这里。
- [src/asys/common/device.py:431-439](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L431-L439) — `get_device_temperature`：最标准的"指针出参"模式，对应 4.4.2 的流程图。
- [src/asys/common/device.py:496-525](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L496-L525) — `get_device_memory_info` / `get_device_hbm_info`：结构体出参模式。注意两处换算：内存容量除以 1024（字节→KB 级单位统一），310 芯片例外不做换算；HBM 的已用量 `usage / MEMEORY_CONVERT_RATIO` 也在此处理。单位换算收口在查询层，上层展示代码拿到即用。

依赖的 so 加载单例（drv 模块）：

- [src/asys/drv/env_type.py:70-90](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/drv/env_type.py#L70-L90) — `LoadSoType.get_drvdsmi_env_type()` 按 EP（边缘容器）环境与否选择 `libdrvdsmi_host.so` / `libdrvdsmi.so` 并缓存；`get_ascend_ml()` 只在 EP 环境加载 `libascend_ml.so`（该库仅在 toolkit 包中）。加载失败返回 `RetCode.FAILED` 而不是异常——device.py 里 `if self.ascend_ml == RetCode.FAILED` 的判断由此而来。

#### 4.4.4 代码实践

1. **实践目标**：无昇腾环境下也能安全体验 DeviceInfo 的防御性设计。
2. **操作步骤**：运行以下示例代码（需要 ctypes，无需真实设备）：

   ```python
   import sys
   sys.path.insert(0, 'src/asys')
   from common.device import DeviceInfo
   from common.const import NOT_SUPPORT

   dev = DeviceInfo()                      # 无驱动环境下句柄为 RetCode.FAILED
   print(dev.get_device_count())           # 预期 0
   print(dev.get_device_health(0))         # 预期 Unknown
   print(dev.get_device_temperature(0))    # 预期 '-'（NOT_SUPPORT）
   ```

3. **需要观察的现象**：三行输出分别为 `0`、`Unknown`、`-`，且过程不抛异常。
4. **预期结果**：验证"so 缺失/符号缺失/调用失败"全部退化为占位值的防御设计。**待本地验证**（有驱动的机器上会返回真实值）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `device.py` 里到处是 `try ... except AttributeError`？

**答案**：`ctypes.CDLL` 对象访问不存在的符号（so 版本没有该函数）时抛 `AttributeError`。不同版本驱动/不同芯片提供的 DSMI 接口集合不同，捕获该异常并返回 `NOT_SUPPORT`，使 asys 能在"接口存在性不确定"的现实下统一工作。

**练习 2**：`get_device_power` 返回前为什么要 `power * 0.1`？

**答案**：DSMI 返回的功耗原始值单位是 0.1W，乘 0.1 换算成瓦。同理 `get_device_voltage` 乘 `0.01 * 1000`（原始单位 0.01V → 换算为 mV）。C 接口出于精度用整数传输，换算职责落在 Python 封装层。

### 4.5 文件操作：file_operate.py（导读）

本讲规格以 log/const/cmd_run/device 四个模块为主，`file_operate.py` 作为地图中的第五个文件在此做导读，细节留待 u2-l5（collect 子系统）展开。

- [src/asys/common/file_operate.py:34-66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L34-L66) — `FileOperate` 静态工具类的检查类方法：`check_file` / `check_dir` / `check_exists` / `check_emtpy` / `check_access`，全部先判空再委托 `os.path` / `os.access`，返回布尔。

- [src/asys/common/file_operate.py:107-145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L107-L145) — 读写三件套：`write_file` / `append_write_file`（写前自动创建父目录）与 `read_file`——`read_file` 按扩展名分流：`.ini` 返回 `configparser` 对象、`.csv` 返回二维列表、其余返回整个文本。

- [src/asys/common/file_operate.py:197-215](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L197-L215) — `collect_file_to_dir` / `collect_dir`：以 `MOVE_MODE`('m') / `COPY_MODE`('c') 两种模式归集文件或目录，是 collect 子系统把散落文件收进输出目录的统一入口。

## 5. 综合实践

把本讲的 log、const、cmd_run 串起来，完成规格中要求的 30 行以内小脚本：记录两条日志、执行 `npu-smi info` 并解析返回码与输出。

**任务**：在仓库根目录创建 `tmp_npu_probe.py`（示例代码，验证后可删除，不要提交到仓库）：

```python
import sys
sys.path.insert(0, 'src/asys')          # asys 的模块以 src/asys 为 import 根
from common.log import log_info, log_error
from common.cmd_run import check_command, run_cmd_output
from common.device import DeviceInfo
from common.const import RetCode

log_info("开始探测 NPU 环境")
if not check_command("npu-smi"):
    log_error("npu-smi 命令不存在，请确认已安装驱动并 source set_env.sh")
    sys.exit(RetCode.ARG_NO_EXECUTABLE.value)   # 退出码 7，复用 RetCode 语义

ok, out = run_cmd_output("npu-smi info")
log_info(f"命令执行结果: {'成功' if ok else '失败'}")
print(out)

dev = DeviceInfo()
log_info(f"检测到设备数量: {dev.get_device_count()}")
```

**操作步骤**：

1. `python3 tmp_npu_probe.py` 运行（有昇腾设备的环境）；无设备环境同样可跑，重点观察防御分支。
2. 把 `log_error` 那行改成 `log_info("...", force=True)` 前先在脚本开头加 `close_log()`，验证 force 穿透效果。

**需要观察的现象与预期结果**：

- 有设备：打印 `[ASYS] [INFO]` 与 `[ASYS] [ERROR]` 两条带格式日志、`npu-smi info` 的完整表格输出、设备数量。
- 无设备：日志提示命令不存在，进程以退出码 7 结束（`echo $?` 可验证）——正是 const.py 中 `RetCode.ARG_NO_EXECUTABLE` 的值。
- 全程无 Python 异常栈，即使驱动缺失 `DeviceInfo()` 也能正常构造。

## 6. 本讲小结

- `log.py`：薄封装标准 logging，`close_log()`/`open_log()` 用全局 disable 机制当开关，服务"纯展示命令要干净终端"的需求；`force=True` 与 error 级别可穿透静音。
- `const.py`：`RetCode` 统一退出码语义（含 9 个参数校验码），`Singleton` 元类（含测试用 `clear()`）支撑 ParamDict 等全局单例，`Constants` 类集中管理子命令名。
- `cmd_run.py`：5 个函数对应 5 种取结果方式（文本 / bool+文本 / bool / 实时透传 / 静默文本），共同哲学是"永不抛异常、失败有确定形态"。
- `device.py`：用 ctypes 直调 DSMI/HAL/AML so 库查设备信息，三重防御（so 缺失、符号缺失、调用失败）统一退化为 `NOT_SUPPORT`；单位换算收口在封装层。
- `file_operate.py`：静态工具类收纳检查、读写、归集文件操作，是 collect 子系统的地基（u2-l5 展开）。
- 这些公共设施共同塑造了 asys 的代码气质：防御式、无异常栈、失败占位值——理解这一点，阅读后面任何子命令源码都会顺畅得多。

## 7. 下一步学习建议

下一讲 **u2-l4（asys 芯片适配层）** 将深入 `common/supported_chip.py` 与 `chip_handler.py.in` 模板机制，看 asys 如何用本讲认识的这些设施支持 910B、910_93、950 等多款芯片的差异化管理——其中 `device.py` 查到的芯片信息正是分流依据。

建议继续阅读的源码：

- `src/asys/drv/env_type.py` 剩余部分（`get_env_type` 如何判断 EP/RC 环境）；
- `test/ut/asys` 下针对 common 模块的测试（若有），看单例 clear 的真实用法；
- 带着本讲结论重读 `src/asys/asys.py` 的 `close_log()` 调用点，印证 u2-l1 的主流程。
