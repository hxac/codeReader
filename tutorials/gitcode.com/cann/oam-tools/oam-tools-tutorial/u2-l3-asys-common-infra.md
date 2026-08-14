# asys 公共设施：日志、常量、设备与命令执行

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立阅读 `src/asys/common/` 目录下五个基础模块的源码，说出每个模块的职责边界。
2. 在自己的脚本中复用 `log.py` 的日志函数与 `const.py` 的 `RetCode` 返回码，写出风格与 asys 一致的代码。
3. 理解 `cmd_run.py` 提供的五种外部命令执行方式各自的适用场景。
4. 理解 `device.py` 如何用 `ctypes` 直接调用驱动动态库获取 NPU 设备信息，以及它为什么「不像前三个模块那么纯 Python」。
5. 会用 `file_operate.py` 的 `FileOperate` 静态方法做安全的文件与目录操作。

## 2. 前置知识

本讲需要以下背景概念，均已在 u1/u2 前几讲建立，这里做一句话回顾并补充新概念：

- **asys 的模块导入约定**：asys 内部所有模块都以 `src/asys/` 为导入根，即 `from common.log import ...` 而不是 `from src.asys.common.log import ...`。这意味着在自己的脚本里复用这些模块时，必须先把 `src/asys` 加入 `sys.path`（或 `PYTHONPATH`）。
- **`logging` 模块（Python 标准库）**：Python 内置的日志框架，`logging.basicConfig` 做全局一次性配置，`logging.info/warning/error` 输出日志，`logging.disable(level)` 可以临时压制不低于某级别的日志。`log.py` 就是对它的一个极薄封装。
- **`subprocess` 模块（Python 标准库）**：Python 执行外部 shell 命令的标准方式。`subprocess.run(...)` 同步执行并等待结束，返回对象的 `returncode`/`stdout`/`stderr` 分别是退出码、标准输出、标准错误；`subprocess.Popen(...)` 则拿到一个可逐行读取的进程对象。
- **`ctypes` 模块（Python 标准库）**：Python 调用 C 动态库（`.so`）的桥梁。`ctypes.cdll.LoadLibrary("libxxx.so")` 加载动态库后，可以直接调用其中导出的 C 函数；`ctypes.Structure` 用来在 Python 侧描述 C 结构体的内存布局。
- **DSMI 接口**：昇腾驱动提供的设备管理接口（Device Service Management Interface），以 `dsmi_*` 系列 C 函数导出在 `libdrvdsmi.so` 等动态库中，`npu-smi` 工具底层调用的就是它们。`device.py` 绕过 `npu-smi` 命令行，直接 `ctypes` 调这些函数。
- **EP / RC 环境**：昇腾的两种部署形态。EP（Edge/服务器 PCIe 卡形态）与 RC（Rocker/SoC 整机形态，如 Atlas 200 系列）。asys 会先探测环境类型再决定加载哪个 so、允许哪些子命令（u2-l1 提过 RC 环境只允许 `launch` 和部分 `collect`）。

## 3. 本讲源码地图

| 文件 | 作用 | 一句话定位 |
| --- | --- | --- |
| `src/asys/common/log.py` | 日志封装 | 5 个日志函数 + `close_log`/`open_log` 全局开关，全文件仅 70 行 |
| `src/asys/common/const.py` | 常量与枚举 | 返回码 `RetCode`、单例元类 `Singleton`、子命令名 `Constants`、各种阈值常量 |
| `src/asys/common/cmd_run.py` | 外部命令执行 | 5 种执行外部命令的函数，覆盖"查命令是否存在"到"实时回显" |
| `src/asys/common/device.py` | NPU 设备信息 | 用 `ctypes` 定义 DSMI 结构体并直调驱动 so，封装成 `DeviceInfo` 类 |
| `src/asys/common/file_operate.py` | 文件操作 | `FileOperate` 静态方法集：检查、读写、复制、移动、删目录 |
| `src/asys/drv/env_type.py` | （辅助）动态库加载 | `LoadSoType` 单例负责加载 `libdrvdsmi.so` 等 so，是 `device.py` 的依赖 |

> 阅读建议：按上表从上到下读，前三个是纯 Python、互相独立；`device.py` 依赖 `drv/env_type.py`，放最后。

## 4. 核心概念与源码讲解

### 4.1 日志封装：log.py

#### 4.1.1 概念说明

运维工具的日志有几个特殊需求：格式统一（便于事后 grep 排障）、线程安全（asys 的采集项会并发跑）、并且能**整体关闭**——u2-l1 讲过，`asys health/info` 这类"纯展示"命令在把结果打印到终端前会调用 `close_log()` 关掉冗余日志，避免日志和结果混在输出里。`log.py` 就是为这三点存在的最小封装。

#### 4.1.2 核心流程

```
业务代码调用 log_info("xxx")
        │
        ▼
_log(logging.info, "xxx", force=False)
        │  加 _LOG_LOCK（RLock，线程安全）
        ▼
force 为 False ──► 直接 log_func("xxx") 输出
force 为 True  ──► 暂存当前 disable 级别
                  → logging.disable(NOTSET)（临时解除全局压制）
                  → 输出
                  → finally 恢复原 disable 级别
```

`close_log()` 的原理是调用 `logging.disable(INFO/DEBUG/WARNING)` 把这三个级别全部压掉，于是 `_log` 里的 `log_func` 调用不再产生输出；而 `force=True` 的调用（以及 `open_log()`）会临时解除压制，保证关键信息（如最终结果）仍能打出来。

#### 4.1.3 源码精读

全局格式与一次性配置，`LOG_FORMAT` 规定了所有 asys 日志的统一前缀 `[ASYS]`：

```python
LOG_FORMAT = "%(asctime)s [ASYS] [%(levelname)s]: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
_LOG_LOCK = threading.RLock()
```

这是 [src/asys/common/log.py:L25-L27](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L25-L27)，定义统一日志格式并用 `threading.RLock` 保证多线程写日志时的互斥。

核心分发函数 `_log`，用 `force` 参数实现"压不住的日志"：

```python
def _log(log_func, log_str, force=False):
    with _LOG_LOCK:
        if not force:
            log_func(log_str)
            return
        disable_level = logging.getLogger().manager.disable
        logging.disable(logging.NOTSET)
        try:
            log_func(log_str)
        finally:
            logging.disable(disable_level)
```

见 [src/asys/common/log.py:L30-L41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L30-L41)。`force=True` 时先记下当前压制级别，用 `logging.disable(NOTSET)` 解除压制、输出、再在 `finally` 中恢复——保证异常路径也不会丢失恢复。

对外暴露的 5 个日志函数与全局开关：

```python
def log_info(log_str, force=False):
    _log(logging.info, log_str, force)

def log_error(log_str):
    _log(logging.error, log_str)

def close_log():
    with _LOG_LOCK:
        logging.disable(logging.INFO)
        logging.disable(logging.DEBUG)
        logging.disable(logging.WARNING)
```

见 [src/asys/common/log.py:L48-L69](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/log.py#L48-L69)。注意只有 `log_info` 和 `log_warning` 带 `force` 参数，`log_error` 不带——错误日志从不被压制的设计并不存在于此文件，`log_error` 也走普通路径；真正"必须可见"的输出用的是 `log_info(msg, force=True)`。

#### 4.1.4 代码实践

**实践目标**：验证 `close_log()` 之后 `log_info` 静默、`force=True` 的 `log_info` 仍可见。

1. 操作步骤（示例代码，非项目原有代码）：

```python
# practice_log.py —— 放在仓库根目录运行
import sys
sys.path.insert(0, "src/asys")          # asys 的导入根是 src/asys
from common.log import log_info, log_error, close_log

log_info("before close: 普通信息")       # 会输出
log_error("before close: 错误信息")      # 会输出
close_log()
log_info("after close: 普通信息")        # 不输出
log_info("after close: 强制信息", force=True)  # 仍输出
```

2. 运行：`python3 practice_log.py`。
3. 观察现象：第 3、4 行日志出现在终端，第 6 行消失，第 7 行出现，且带 `[ASYS] [INFO]:` 前缀。
4. 预期结果：共看到 3 行日志。本实践不需要昇腾设备。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_LOG_LOCK` 用 `RLock` 而不是 `Lock`？

答案：`RLock` 允许同一线程重复加锁。虽然当前 `_log` 内部没有嵌套加锁，但 `close_log`/`open_log` 与 `_log` 共用一把锁，如果未来在持锁回调中再触发日志调用，`Lock` 会死锁而 `RLock` 不会。这是日志这类基础设施常见的防御性选择。

**练习 2**：`close_log()` 里连写了三次 `logging.disable(...)`，能否只写一次？

答案：`logging.disable(level)` 压制的是"不低于 level 的日志"，多次调用取最后一次生效。实际上只写 `logging.disable(logging.WARNING)` 即可同时压掉 INFO/DEBUG/WARNING；连写三次是刻意的保守写法，与 Python 版本行为解耦。

### 4.2 常量与返回码：const.py

#### 4.2.1 概念说明

一个多模块工具最容易散落的就是"魔法数字"——设备号上限、超时秒数、错误码……`const.py` 把它们全部收拢到一个文件，同时提供四个关键类：`RetCode`（统一返回码枚举）、`Singleton`（单例元类，u2-l2 见过 `ParamDict` 用它）、`ScreenResult`（健康检查屏幕结论）、`Constants`（子命令名集合）。此外还放了 DSMI 相关的映射表（UB 端口状态等）。

#### 4.2.2 核心流程

这个模块没有"流程"，它是纯数据声明。理解它的关键在于分清四类内容的消费方：

```
const.py
 ├── 数值常量（DEVICE_ID_MAX=63、DETECT_DEFAULT_TIMEOUT=600 ...）→ 各业务模块 import
 ├── 映射表（UB_ENTIRE_STATUS_MAP ...）→ device/health 展示时查表翻译状态码
 ├── RetCode 枚举     → 全工具统一的失败原因编号
 ├── Singleton 元类   → ParamDict、LoadSoType 等全局单例的基座
 └── Constants + consts 实例 → 子命令名字符串的唯一出处（EXECUTE_CMD_FUNC 的 key）
```

#### 4.2.3 源码精读

返回码枚举，每个值对应一类参数或 IO 错误：

```python
class RetCode(enum.Enum):
    SUCCESS = 0
    FAILED = 1
    ARG_PATH_INVALID = 2
    ARG_EMPTY_STRING = 3
    ...
    READ_FILE_FAILED = 10
    PERMISSION_FAILED = 11
```

见 [src/asys/common/const.py:L154-L166](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L154-L166)。u2-l2 提到 `ArgChecker` 校验失败时返回的就是这些值；`device.py` 里 `LoadSoType` 加载 so 失败也会返回 `RetCode.FAILED` 作为哨兵。

单例元类，用"类的类"实现只初始化一次：

```python
class Singleton(type):
    """ Singleton class """
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in Singleton._instances:
            Singleton._instances[cls] = super().__call__(*args, **kwargs)
        return Singleton._instances[cls]
```

见 [src/asys/common/const.py:L138-L151](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L138-L151)。原理：Python 中 `SomeClass()` 实际调用元类的 `__call__`，这里在元类层面拦截，第一次真正创建实例并缓存到 `_instances`，之后永远返回缓存。`clear()` 方法供测试里重置单例状态。

子命令名常量类与模块级实例：

```python
class Constants:
    @property
    def collect_cmd(self):
        return 'collect'
    ...
    @property
    def cmd_set(self):
        return [self.help_cmd, self.collect_cmd, ...]

consts = Constants()
```

见 [src/asys/common/const.py:L206-L249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L206-L249)。模块末尾直接创建实例 `consts`，其他模块 `from common.const import consts` 后即可使用，这是"一个模块一个全局常量对象"的简单模式。

另一个值得注意的常量是配置表路径，用 `pathlib` 相对当前文件定位（见 [src/asys/common/const.py:L104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L104)）：

```python
CONFIG_TABLE_FILE = Path(__file__).parent.parent / 'conf' / 'config_table.csv'
```

它指向 `src/asys/conf/config_table.csv`，即 u2-l8 将讲的 `asys config` 配置清单，这种"相对源码文件定位"保证不依赖运行时工作目录。

#### 4.2.4 代码实践

**实践目标**：验证 `Singleton` 元类的行为，并统计 `RetCode` 的全部取值。

1. 操作步骤（示例代码，非项目原有代码）：

```python
import sys
sys.path.insert(0, "src/asys")
from common.const import Singleton, RetCode
from common.log import log_info

class Config(metaclass=Singleton):
    def __init__(self):
        self.data = {}

a, b = Config(), Config()
log_info(f"同一实例: {a is b}")                 # True
log_info(f"RetCode 全部取值: {[m.name for m in RetCode]}")
```

2. 运行：`python3 practice_const.py`。
3. 观察现象：第一行输出 `True`；第二行列出 12 个枚举名。
4. 预期结果：两次 `Config()` 得到同一对象；`RetCode` 共 12 个成员（SUCCESS 到 PERMISSION_FAILED）。不需要昇腾设备。

#### 4.2.5 小练习与答案

**练习 1**：`RetCode` 为什么用 `Enum` 而不是模块级整数常量（如 `SUCCESS = 0`）？

答案：枚举成员自带名字，日志里打印 `RetCode.ARG_NO_EXIST_DIR` 比 `6` 可读；同时枚举类型约束了取值范围，传错字面量会在代码检查阶段暴露，而裸整数可以随意混用。

**练习 2**：`Singleton.clear()` 为什么是必需的？

答案：单例在整个进程生命周期共享，单元测试里若不清理，上一个用例构造的 `ParamDict`/`LoadSoType` 状态会泄漏到下一个用例，造成用例间相互影响。`clear()` 提供了测试间的重置入口。

### 4.3 外部命令执行：cmd_run.py

#### 4.3.1 概念说明

asys 的大量信息来源于执行外部命令：`npu-smi info`、`cat /var/log/...`、`dmesg`、`ps` 等。`cmd_run.py` 把 `subprocess` 的用法收拢成 5 个语义不同的函数，避免各采集模块自己拼 `subprocess.run` 导致行为不一致（编码、错误处理、日志各写一套）。它是"采集类模块最常用的公共设施"。

#### 4.3.2 核心流程

五个函数按"调用方拿到什么"分类：

| 函数 | 返回 | 典型场景 |
| --- | --- | --- |
| `check_command(cmd)` | `bool`（命令是否存在） | 执行前先探测 `npu-smi` 是否安装 |
| `run_linux_cmd(cmd, cmp_str="")` | `bool`（成功 或 stdout 等于 cmp_str） | 只关心成败的探测性命令 |
| `run_command(cmd)` | `str`（成功给 stdout，失败给错误摘要，找不到命令给 `'NONE'`） | 拿一行文本结果（如版本号） |
| `run_cmd_output(cmd)` | `(bool, str)` 元组（成功标志 + 完整输出） | 拿多行输出并自行判断成败 |
| `real_time_output(cmd, output=True)` | `bool`（退出码是否为 0） | 复跑用户业务时实时把输出透传给终端 |

另有 `popen_run_cmd(cmd)`：用 `os.popen` 执行并**屏蔽 stderr**（通过 `_IgnoreStderr` 上下文管理器临时把 fd 2 重定向到 `/dev/null`），适合第三方工具往 stderr 打噪音但只想要 stdout 的场合。

`run_command` 的错误处理决策树：

```
subprocess.run(cmd)
 ├── returncode == 0
 │    ├── stderr 非空 ──► 返回 'NONE'（认为结果不可信）
 │    └── stderr 为空 ──► 返回 stdout.strip()
 └── returncode != 0
      ├── stderr 含 'not found' ──► 返回 'NONE'（命令不存在）
      └── 否则 ──► log_debug 记录 ──► 返回 stderr（换行替换为两个空格）
```

#### 4.3.3 源码精读

命令存在性检查，跨 Windows/Linux：

```python
def check_command(command):
    os_type = get_os_type()
    if os_type == "Windows":
        cmd = f"where {command}"
    elif os_type == "Linux":
        cmd = f"which {command}"
    else:
        log_debug("Unsupported operating system.")
        return False
    ret = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return ret.returncode == 0
```

见 [src/asys/common/cmd_run.py:L33-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L33-L43)。`which` 找到命令返回 0，据此判断目标命令是否在 PATH 中——u2-l2 提过 `ArgChecker` 里"校验可执行命令"用的就是这类探测。

最常用的取输出函数：

```python
def run_cmd_output(command) -> [bool, str]:
    ret = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
                         env=os.environ)
    if ret.returncode == 0:
        return True, ret.stdout
    else:
        ret_err = ret.stderr
        log_debug('Run command: {0} failed, ret_code={1}, ret_err={2}'.format(command, ret.returncode, ret_err))
        return False, ret.stderr
```

见 [src/asys/common/cmd_run.py:L72-L80](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L72-L80)。要点：显式 `encoding='utf-8'` 统一解码、透传 `env=os.environ` 保证子进程能看到 `LD_LIBRARY_PATH` 等环境、失败时用 `log_debug` 留痕但不打断调用方。

实时透传输出（`asys launch` 复跑业务时的关键路径）：

```python
def real_time_output(command, output=True) -> bool:
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                               universal_newlines=True, env=os.environ)
    if output:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    process.wait()
    return process.returncode == 0
```

见 [src/asys/common/cmd_run.py:L83-L91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L83-L91)。与 `subprocess.run` 的区别在于不等命令结束就开始逐行转发——用户的训练脚本跑到第 3 小时，前 2 小时的 print 不能攒到最后才吐出来。

屏蔽 stderr 的上下文管理器：

```python
class _IgnoreStderr:
    def __init__(self):
        self.null_fd = os.open(os.devnull, os.O_RDWR)
        self.save_fd = os.dup(2)

    def __enter__(self):
        os.dup2(self.null_fd, 2)
```

见 [src/asys/common/cmd_run.py:L94-L104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/cmd_run.py#L94-L104)。经典的双 fd 技巧：先备份 fd 2，进入时把 fd 2 指向 `/dev/null`，退出时恢复，`os.popen` 期间子进程的 stderr 就被丢弃了。

#### 4.3.4 代码实践

见本讲第 5 节综合实践（其中第 2 步就是用 `run_cmd_output` 执行 `npu-smi info` 并解析返回码与输出，正是规格要求的实践任务）。

快速热身（示例代码）：`python3 -c "import sys; sys.path.insert(0,'src/asys'); from common.cmd_run import check_command; print(check_command('ls'), check_command('no-such-cmd'))"` 应输出 `True False`。

#### 4.3.5 小练习与答案

**练习 1**：`run_command` 在 returncode 为 0 但 stderr 非空时返回 `'NONE'`，这个设计意图是什么？有什么代价？

答案：意图是"命令嘴上说成功但打了告警，输出不可信，宁可当作没有结果"，让调用方统一用 `'NONE'` 字符串判断。代价是调用方无法区分"命令不存在"、"命令失败"和"成功但有告警"三种情况——这也是为什么需要完整信息的场合应该改用 `run_cmd_output`。

**练习 2**：`real_time_output` 为什么用 `Popen` + 逐行迭代，而不用 `subprocess.run` 之后一次性打印？

答案：`subprocess.run` 会阻塞到命令退出才返回全部输出；复跑用户业务（训练脚本可能跑几小时）时，用户需要实时看到业务输出判断卡没卡。`Popen` 拿到进程对象后立即逐行 `write + flush`，实现了透传。另外它把 `stderr` 合并到 `stdout`（`stderr=subprocess.STDOUT`），保证业务的标准错误也按序可见。

### 4.4 NPU 设备信息封装：device.py

#### 4.4.1 概念说明

前面三个模块是"纯 Python 基础设施"，`device.py` 则是 asys 与昇腾驱动的交界处。`asys info/health` 展示的功耗、温度、HBM 用量、AI Core 使用率，不是解析 `npu-smi` 的文本输出，而是用 `ctypes` 直接加载驱动动态库（`libdrvdsmi.so`、`libascend_hal.so`、`libascend_ml.so`、`libascendcl.so`），按 C 结构体布局传指针、调函数、取结果。好处是结构化、无文本解析误差；代价是必须严格对齐 C 的内存布局。

so 的加载本身不在本文件，而在 `drv/env_type.py` 的 `LoadSoType` 单例中（见 [src/asys/drv/env_type.py:L30-L47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/drv/env_type.py#L30-L47)），它按需加载并缓存各 so，例如 RC 环境用 `libdrvdsmi.so`、EP 环境用 `libdrvdsmi_host.so`（见 [src/asys/drv/env_type.py:L70-L77](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/drv/env_type.py#L70-L77)）；环境类型则通过调 `libascend_hal.so` 的 `drvGetPlatformInfo` 探测（见 [src/asys/drv/env_type.py:L117-L130](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/drv/env_type.py#L117-L130)），返回 0 是 RC、1 是 EP。

#### 4.4.2 核心流程

`DeviceInfo` 每个查询方法的套路完全一致，可以总结成一条模板：

```
1. 准备 C 结构体指针:  p = ctypes.pointer(DsmiXxxStru())
2. 调用动态库函数:      ret = self.dsmi_handle.dsmi_xxx(device_id, p)
   （外包 try/except AttributeError：so 没加载成功时句柄是 RetCode.FAILED，
     访问其属性即抛 AttributeError → 返回 NOT_SUPPORT）
3. 检查返回码:          self.check_status(ret, "...") → False 则返回 NOT_SUPPORT
4. 读取并换算:          value = p.contents.xxx  （必要时 //1024、*0.1 等单位换算）
```

以功耗为例的数值换算：DSMI 返回的是 0.1W 为单位的整数，`round(power * 0.1, 1)` 才得到瓦特数；电压同理 `* 0.01 * 1000` 得毫伏；内存容量 `// 1024` 由字节换算成 MB（310 芯片除外）。

#### 4.4.3 源码精读

构造函数拿齐四类动态库句柄：

```python
class DeviceInfo:
    UNSUPPORTED_KEY_WORDS = [NOT_SUPPORT]

    def __init__(self):
        self.dsmi_handle = LoadSoType().get_drvdsmi_env_type()
        self.hal_handle = LoadSoType().get_drvhal_env_type()
        self.ascend_ml = LoadSoType().get_ascend_ml()
        self.ascend_cl = LoadSoType().get_ascend_cl()
```

见 [src/asys/common/device.py:L185-L193](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L185-L193)。注意 `LoadSoType()` 是单例，四个句柄在进程内只加载一次；任何一个加载失败，句柄值是 `RetCode.FAILED`（整数 1），后续方法用它调函数会抛 `AttributeError`，被各方法的 `except AttributeError` 兜住。

C 结构体的 Python 侧镜像（以 HBM 信息为例）：

```python
class DsmiHBMInfoStru(ctypes.Structure):
    _fields_ = [
        ("memory_size", ctypes.c_ulonglong),
        ("freq", ctypes.c_uint),
        ("memory_usage", ctypes.c_ulonglong),
        ("temp", ctypes.c_int),
        ("bandwith_util_rate", ctypes.c_uint),
    ]
```

见 [src/asys/common/device.py:L114-L121](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L114-L121)。`_fields_` 的字段顺序与类型必须与驱动头文件中的 C 结构体逐字节一致，否则读到的是错位内存。文件开头 L81 起还定义了 `DsmiChipInfoStru`、`DsmiPowerInfoStru`、`AmlCpuInfo` 等十余个结构体（见 [src/asys/common/device.py:L81-L183](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L81-L183)）。

统一的状态检查与错误码翻译：

```python
@staticmethod
def check_status(ret, msg="Failed to query data"):
    if ret == 0:
        return True
    msg += ", %s" % DSMI_ERROR_CORE.get(ret)
    log_debug(msg)
    return False
```

见 [src/asys/common/device.py:L195-L201](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L195-L201)。DSMI 返回 0 表示成功；非 0 时用文件头部的 `DSMI_ERROR_CORE` 字典（L51-L79，如 `13: "device busy"`、`65534: "not support"`）把驱动错误码翻译成可读说明写进 debug 日志。

一个完整的查询方法（功耗）：

```python
def get_device_power(self, device_id):
    p_power_info = ctypes.pointer(DsmiPowerInfoStru())
    try:
        ret = self.dsmi_handle.dsmi_get_device_power_info(device_id, p_power_info)
    except AttributeError:
        return NOT_SUPPORT
    if not self.check_status(ret, "Get power info failed"):
        return NOT_SUPPORT
    return round(p_power_info.contents.power * 0.1, 1)  # power unit is W
```

见 [src/asys/common/device.py:L421-L429](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L421-L429)。这是 4.4.2 模板的标准落地：指针 → 调用 → `check_status` → 换算单位。文件中 `get_device_temperature`（L431）、`get_device_frequency`（L441）、`get_device_utilization_rate`（L476）、`get_device_memory_info`（L496）、`get_device_hbm_info`（L512）等二十余个方法都是同一模板，只是结构体与换算不同。

健康状态查询展示了"数值翻译成展示文案"的又一形态：

```python
def get_device_health(self, device_id):
    device_health_status = {0: "Healthy", 1: "Warning", 2: "Alarm", 3: "Critical"}
    ...
    device_health_count = p_health_count.contents.value
    if device_health_count in device_health_status.keys():
        return device_health_status.get(device_health_count)
    return UNKNOWN
```

见 [src/asys/common/device.py:L341-L355](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L341-L355)。`asys health` 终端上看到的 Healthy/Warning 字样即来源于此。

#### 4.4.4 代码实践

**实践目标**：在无设备环境下体会 `DeviceInfo` 的失败兜底路径（这条路径不需要昇腾硬件也能走通）。

1. 操作步骤（示例代码，非项目原有代码）：

```python
import sys
sys.path.insert(0, "src/asys")
from common.device import DeviceInfo
from common.log import log_info

dev = DeviceInfo()                       # 无驱动环境下句柄为 RetCode.FAILED
log_info(f"device count = {dev.get_device_count()}")
log_info(f"power = {dev.get_device_power(0)}")
```

2. 运行：`python3 practice_device.py`（在有驱动的机器上则返回真实值）。
3. 观察现象：无驱动环境输出 `device count = 0`、`power = -`（`NOT_SUPPORT`）；同时 `log_debug` 级别的 so 加载错误不出现在终端（默认 INFO 级别）。
4. 预期结果：脚本不抛异常正常退出——这正是 `except AttributeError` + `NOT_SUPPORT` 哨兵设计的意义。有昇腾设备时的真实返回值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DeviceInfo` 每个方法都要 `try/except AttributeError`，而不是在 `__init__` 里检查句柄？

答案：句柄加载失败时值是 `RetCode.FAILED`（枚举对象），用 `self.dsmi_handle.dsmi_get_xxx` 访问不存在的属性才抛 `AttributeError`。在 `__init__` 里检查也可以，但要为四个句柄分别写分支且无法覆盖"so 加载成功但缺少某导出符号"的情况；统一在调用点捕获 `AttributeError` 以 `NOT_SUPPORT` 降级，保证任何一个库缺失只影响对应查询项，不拖垮整个命令。

**练习 2**：`get_device_memory_info` 里为什么要特判 `"310 " in self.get_chip_info(device_id)`？

答案：见 [src/asys/common/device.py:L496-L510](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/device.py#L496-L510)。310 系列老芯片的 DSMI 返回的 memory_size 单位与其他芯片不同（其他芯片需要 `// 1024` 从字节换算成 MB，310 不需要），这是历史兼容：同一套代码服务多代硬件时，单位差异只能在取数处就地修正。

### 4.5 文件操作：file_operate.py

#### 4.5.1 概念说明

采集类工具的一半工作是搬文件：把散落各处的日志、dump、trace 复制/移动进 `asys_output_<时间戳>` 目录。`file_operate.py` 提供 `FileOperate` 静态方法集，把"先检查存在性和权限、再操作、失败打日志返回 False"的防御性套路固化下来，全模块无状态、全是 `@staticmethod`，调用方不需要实例化。

#### 4.5.2 核心流程

方法按功能分四组：

```
检查类:   check_file / check_dir / check_exists / check_emtpy / check_access / check_valid_dir
读写类:   read_file（按扩展名分派：.ini→ConfigParser，.csv→二维列表，其他→全文 str）
          write_file / append_write_file（写前自动创建父目录）
目录类:   create_dir（mode=0o750）/ remove_dir / delete_dirs / walk_dir / list_dir
搬运类:   copy_file_to_dir / copy_dir / move_file_to_dir / move_dir
          collect_file_to_dir / collect_dir（mode='m' 移动 / 'c' 复制 的统一入口）
```

采集模块的典型用法是 `collect_file_to_dir(src, dst, mode)`：u2-l1 讲过的"collect 结果默认留在目录、按 `--tar` 参数压缩"，压缩前的归拢就是靠它完成的。

#### 4.5.3 源码精读

写文件前自动补父目录：

```python
@staticmethod
def write_file(file_path, info):
    if not file_path:
        return
    file_dir = os.path.split(file_path)[0]
    if file_dir and not os.path.exists(file_dir) and not FileOperate.create_dir(file_dir):
        log_error("Create path directory: \"{}\" failed in write file.".format(file_dir))
        return
    with open(file_path, mode="w", encoding=ENCODE_UTF_8) as f:
        f.write(info)
```

见 [src/asys/common/file_operate.py:L107-L116](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L107-L116)。采集输出的目录层级是运行时动态决定的（设备号、时间戳等拼出来），"写哪补哪"免去调用方逐级建目录的负担。

按扩展名分派的读取：

```python
@staticmethod
def read_file(file_path):
    if file_path.endswith(".ini"):
        cf = configparser.ConfigParser()
        cf.read(file_path, encoding=ENCODE_UTF_8)
        return cf
    elif file_path.endswith(".csv"):
        ...
    else:
        with open(file_path, mode="r", encoding=ENCODE_UTF_8) as f:
            return f.read()
```

见 [src/asys/common/file_operate.py:L129-L145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L129-L145)。返回类型随扩展名变化（ConfigParser 对象 / list[list[str]] / str），调用方按文件类型已知这一约定。

防嵌套复制的保护：

```python
@staticmethod
def copy_dir(source_dir_path, target_dir_path):
    ...
    if os.path.relpath(source_dir_path, target_dir_path).endswith(".."):
        log_error("The output directory cannot be in the data directory.")
        return False
    shutil.copytree(source_dir_path, target_dir_path)
```

见 [src/asys/common/file_operate.py:L166-L175](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L166-L175)。若目标目录在源目录内部，`copytree` 会边复制边发现新文件、无限递归撑爆磁盘；用相对路径是否以 `..` 开头提前拦截。这是采集场景特有的坑——输出目录很容易被用户指到数据目录里面。

配置清单解析（为 u2-l8 的 `asys config` 供数据）：

```python
@staticmethod
def _read_config():
    """读取config配置清单并解析成字典"""
    config_table = {}
    with open(CONFIG_TABLE_FILE, newline='') as f:
        data = csv.reader(f)
        _, cfg_get, cfg_set, cfg_restore = next(data)
        for row in data:
            config_table[row[0]] = {
                cfg_get: row[1].split(","),
                cfg_set: row[2].split(","),
                cfg_restore: row[3].split(",")
            }
    return config_table
```

见 [src/asys/common/file_operate.py:L238-L251](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L238-L251)。首行是表头（get/set/restore 三列的键名），之后每行一个配置项，逗号分隔多个 so 路径；外层 `read_config`（L225-L236）再兜住文件不存在、无权限、格式错误三种异常。

#### 4.5.4 代码实践

**实践目标**：验证"写文件自动建父目录"与"输出目录嵌进数据目录被拒绝"两个行为。

1. 操作步骤（示例代码，非项目原有代码）：

```python
import sys
sys.path.insert(0, "src/asys")
from common.file_operate import FileOperate
from common.log import log_info

FileOperate.write_file("/tmp/asys_prac/a/b/note.txt", "hello")   # 自动建两级目录
log_info(f"文件已写入: {FileOperate.check_file('/tmp/asys_prac/a/b/note.txt')}")
log_info(f"嵌套复制被拒: {FileOperate.copy_dir('/tmp/asys_prac', '/tmp/asys_prac/inner')}")
```

2. 运行：`python3 practice_file.py`。
3. 观察现象：第一行输出 `True`（目录被自动创建）；第二行输出 `False` 且终端有一条 `log_error` 说明输出目录不能在数据目录内。
4. 预期结果：无需设备即可验证；结束后可 `rm -rf /tmp/asys_prac` 清理。

#### 4.5.5 小练习与答案

**练习 1**：`check_emtpy`（注意源码中的拼写）在什么输入下返回 True？

答案：见 [src/asys/common/file_operate.py:L54-L60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/file_operate.py#L54-L60)。路径为空、路径不存在、路径不是目录、或是空目录四种情况都返回 True——语义是"没有可读内容"。拼写 emtpy 是源码既有的历史笔误，调用方按原名使用（也提醒我们：公共 API 的名字一旦发布就很难改）。

**练习 2**：`collect_file_to_dir(source, target, mode)` 的 `mode` 取什么值时是移动？取其他值会发生什么？

答案：`mode == MOVE_MODE`（即 `'m'`）时走 `move_file_to_dir`；`'c'`（COPY_MODE）时复制；其他值记录 `log_error("Unknown mode in collect file.")` 并返回 `False`，不做任何文件操作。

## 5. 综合实践

把本讲四个模块串起来，完成规格要求的任务：**写一个 30 行以内的小脚本，复用 asys 公共设施执行 `npu-smi info` 并规范地记录日志**（示例代码，非项目原有代码）：

```python
# practice_infra.py —— 放在仓库根目录，python3 practice_infra.py 运行
import sys
sys.path.insert(0, "src/asys")                       # ① 接入 asys 导入根

from common.log import log_info, log_error            # ② 日志设施
from common.cmd_run import check_command, run_cmd_output   # ③ 命令执行设施
from common.const import RetCode                      # ④ 返回码

def main() -> RetCode:
    if not check_command("npu-smi"):                  # 探测命令是否存在
        log_error("npu-smi 不在 PATH 中，请先安装驱动并 source set_env.sh")
        return RetCode.FAILED

    ok, output = run_cmd_output("npu-smi info")       # 执行并拿 (成败, 输出)
    if not ok:
        log_error(f"npu-smi info 执行失败: {output.strip()[:200]}")
        return RetCode.FAILED

    log_info(f"命令执行成功，共输出 {len(output.splitlines())} 行")
    for line in output.splitlines():
        if "910B" in line or "910_93" in line or "950" in line:
            log_info(f"检测到昇腾芯片行: {line.strip()}")
    return RetCode.SUCCESS

if __name__ == "__main__":
    ret = main()
    log_info(f"最终返回码: {ret}")
    sys.exit(ret.value)
```

操作步骤与观察点：

1. **实践目标**：在一个脚本里同时用到 `check_command`（执行前探测）、`run_cmd_output`（执行并解析返回码与输出）、`log_info/log_error`（规范日志）、`RetCode`（统一返回值）。
2. 在有昇腾设备的环境运行，应看到 `[ASYS] [INFO]` 格式的行数统计与芯片行匹配结果，退出码 0；把 `npu-smi` 从 PATH 移除（或直接把命令改成不存在的名字）再跑，应看到 `log_error` 输出且退出码 1。
3. 在无设备环境，`check_command` 就会返回 False 走失败分支——这本身就是一次"错误路径"的完整体验。芯片行匹配的输出内容**待本地验证**（本环境无昇腾设备）。
4. 思考题（衔接下一讲）：如果把这个脚本里的 `run_cmd_output("npu-smi info")` 换成 `DeviceInfo().get_device_count()`，你能不解析任何文本就拿到设备数量——这正是 4.4 两条取数路线的取舍。

## 6. 本讲小结

- `log.py` 用 70 行完成了 asys 的日志规范：统一 `[ASYS]` 前缀、`RLock` 线程安全、`force=True` 穿透 `close_log()` 压制，支撑了 info/health 等"纯展示"命令的静音需求。
- `const.py` 是全工具的常量与"语言级设施"来源：`RetCode` 统一 12 类返回码，`Singleton` 元类支撑 `ParamDict` 与 `LoadSoType` 等全局单例，`consts.cmd_set` 是子命令名的唯一出处。
- `cmd_run.py` 提供 5 种语义分明的执行方式：`check_command` 查存在、`run_command` 拿单值、`run_cmd_output` 拿完整输出、`real_time_output` 实时透传（asys launch 复跑业务的关键）、`popen_run_cmd` 屏蔽 stderr。
- `device.py` 是 asys 与驱动的交界：`ctypes.Structure` 逐字节对齐 C 结构体，`LoadSoType` 单例按 EP/RC 环境加载 so，每个查询方法走"指针 → 调用 → check_status 翻译错误码 → 单位换算"模板，任何一环失败都以 `NOT_SUPPORT` 降级而不崩溃。
- `file_operate.py` 用无状态静态方法固化"先查权限再操作"的防御套路，`copy_dir` 的嵌套检查防住了采集场景特有的无限递归复制。
- 五个模块合起来构成 asys 所有子命令的"地基"：u2-l5 将讲的 collect 各采集子模块，几乎每一个都同时 import 这里的三到四个设施。

## 7. 下一步学习建议

下一讲 **u2-l4「asys 芯片适配层：supported_chip 与各型号 handler」** 将沿着 `device.py` 的多芯片问题继续深入：当 910B、910_93、950 对同一查询的行为出现差异时，asys 如何用 `chip_handler.py.in` 模板生成统一的处理器接口。建议先自行浏览 `src/asys/common/supported_chip.py`，带着一个问题去读：它识别芯片时用到的芯片名字符串，正是本讲 `DeviceInfo.get_chip_info()` 返回的 `chip_type chip_name chip_ver` 拼接结果。之后按依赖顺序进入 u2-l5（collect 采集框架）时，你会不断在本讲这五个文件里重逢。
