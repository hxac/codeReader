# asys 入口主流程：asys.py 如何分发子命令

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂 `src/asys/asys.py` 中 `main()` 的完整执行流程：参数去重 → 命令行解析 → 环境类型检查 → 配置加载 → 输出目录创建 → 子命令分发执行 → 结果压缩。
2. 理解 `EXECUTE_CMD_FUNC` 字典分发机制：为什么 asys 用一个字典就能把 8 个子命令映射到 8 个实现类。
3. 理解输出目录 `asys_output_<时间戳>` 的创建时机，以及 `--tar` 参数触发压缩的完整链路。

本讲是单元 2（asys 源码解析）的第一讲，后续讲义（命令行体系、公共设施、collect 子系统等）都会以本讲梳理的主流程为骨架。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **入口文件（entry point）**：程序开始执行的地方。asys 是纯 Python 工具，入口就是 `src/asys/asys.py`。在 [u1-l3](./u1-l3-directory-and-entrypoints.md) 中我们已经知道，仓库根目录有一个软链接 `asys -> ./src/asys/asys.py`，配合文件首行的 shebang（`#!/usr/bin/env python3`），安装后可以不带 `python3` 直接敲 `asys` 调用。
- **子命令（subcommand）**：类似 `git pull`、`git commit` 的设计——一个工具名后面跟一个动词，表示要做的事。asys 支持 `collect`、`launch`、`info`、`diagnose`、`health`、`analyze`、`config`、`profiling` 共 8 个子命令。
- **字典分发（dispatch by dictionary）**：Python 中"根据字符串找到要执行的类/函数"的常用手法。用 `if/elif` 写 8 个分支很冗长，用字典 `{命令名: 实现类}` 一行就能完成查找。
- **argparse**：Python 标准库的命令行解析器。asys 没有直接手写 argparse，而是封装了一层 `CommandLineParser`（下一讲 u2-l2 详讲），本讲只需知道 `asys_parser.parse()` 会把命令行参数解析结果存进全局的 `ParamDict`。
- **单例模式**：`ParamDict()` 每次调用拿到的其实是同一个对象，任何模块都能随时读取命令行参数，不需要层层传参。
- **tar 压缩**：Linux 经典打包格式。`.tar.gz` 表示先用 tar 打包再用 gzip 压缩。asys 采集完的信息默认留在目录里，只有用户显式传 `--tar` 时才压成 `.tar.gz` 并删除原目录。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/asys/asys.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py) | asys 总入口。参数去重、主流程编排、`EXECUTE_CMD_FUNC` 子命令分发、触发压缩 |
| [src/asys/common/task_common.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/task_common.py) | 任务公共设施。本讲关注 `create_out_timestamp_dir()`（创建时间戳输出目录），另有超时装饰器、进度条等工具 |
| [src/asys/common/compress_output_dir.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/compress_output_dir.py) | 输出目录压缩。`compress_output_dir_tar()` 把输出目录打成 `.tar.gz` 并删除原目录 |
| [src/asys/common/const.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py) | 常量定义。`Constants` 类提供 8 个子命令名的属性，`RetCode` 枚举定义返回码（本讲作为参照） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **asys.py 主流程**：`main()` 的守门逻辑与四步编排。
2. **EXECUTE_CMD_FUNC 子命令分发**：字典映射机制。
3. **输出目录创建与 tar 压缩**：`task_common.py` 与 `compress_output_dir.py` 的配合。

### 4.1 asys.py 主流程

#### 4.1.1 概念说明

`asys.py` 是 asys 的"总调度室"。它自己不做任何具体的采集、诊断工作，而是负责**守门**（拦截非法输入）和**编排**（按正确顺序调用各模块）。这种"入口薄、实现厚"的设计好处是：入口逻辑简单可靠，新增子命令时入口几乎不用改。

守门包括四件事：

1. **参数去重**：同一个参数在命令行出现两次直接拒绝。
2. **命令行解析**：交给 `CommandLineParser`，解析失败立即退出。
3. **环境类型检查**：判断当前是容器/物理机/RC（远端控制）等环境类型，某些命令在特定环境下不允许执行。
4. **配置文件加载**：读取 asys 的配置文件，失败则退出。

编排则是四步：解析参数 → 检查环境 → 加载配置并建输出目录 → 分发执行 + 可选压缩。

#### 4.1.2 核心流程

`main()` 的执行流程（编号对应源码中的注释编号）：

```text
main()
 ├─ _check_args_duplicate()          # 前置守门：参数去重
 ├─ signal(SIGINT, SIG_DFL)          # 恢复 Ctrl+C 默认行为
 ├─ 1. CommandLineParser().parse()   # 解析命令行 → ParamDict
 │     └─ 无子命令时：-h/--help 打印帮助返回 True，否则报错返回 False
 ├─ （条件）close_log()               # info/diagnose/health 等纯展示型命令关闭冗余日志
 ├─ 2. get_env_type()                # 环境类型检查；RC 环境只允许部分命令
 ├─ 2. AsysConfigParser().parse()    # 加载配置文件
 ├─ create_out_timestamp_dir()       # 创建 asys_output_<时间戳> 输出目录
 ├─ 3. EXECUTE_CMD_FUNC[command]().run()   # 字典分发，实例化并执行
 └─ 4. --tar 为真时 compress_output_dir_tar()  # 压缩输出目录
```

注意一个细节：源码里有两个"步骤 2"注释（环境检查和配置加载），这是上游代码的历史遗留，读者不要被编号迷惑——逻辑顺序本身是清晰的。

#### 4.1.3 源码精读

**参数去重函数**。[asys.py:43-50](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L43-L50)：`_check_args_duplicate()` 把 `sys.argv[1:]` 中每个带 `-` 前缀的参数取出参数名（`--output=/tmp` 只取 `--output`），用 `set` 去重后比较数量，出现重复即报错返回 `False`。这样 `asys info --device 0 --device 1` 这种有歧义的写法会在最前面被拦下。

```python
def _check_args_duplicate():
    input_args = [arg.split('=')[0] for arg in sys.argv[1:] if '-' in arg.split('=')[0]]
    args_no_duplicate = set(input_args)
    if len(input_args) > len(args_no_duplicate):
        log_error(f'Only one of the {list(args_no_duplicate)} args can be specified.')
        return False
    return True
```

**main 开头与命令行解析**。[asys.py:73-96](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L73-L96)：先做去重检查，再用 `signal.signal(signal.SIGINT, signal.SIG_DFL)` 恢复 Ctrl+C 的默认终止行为（避免 Python 把 KeyboardInterrupt 当异常打印一大段堆栈）。随后实例化 `CommandLineParser` 并 `parse()`——注意 `parse()` 内部遇到 `-h` 会抛 `SystemExit`，所以用 `try/except SystemExit` 捕获后优雅返回 `False`。解析成功后从 `ParamDict` 单例取出子命令名；取不到子命令时，若是 `-h/--help` 则打印帮助并返回 `True`，否则报错退出。

**纯展示型命令关闭日志**。[asys.py:98-104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L98-L104)：`info`、`diagnose`、`health` 这类命令的结果要直接打印到终端给用户看，如果同时输出 info/warning 级别的运行日志会污染输出，所以提前调用 `close_log()` 关闭日志。`config get` 和 `analyze` 的 `aicore_error` 模式同理。

**环境类型检查与 RC 限制**。[asys.py:106-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L106-L117)：`param_dict.get_env_type()` 获取执行环境类型；当环境是 `'RC'`（昇腾远端控制环境）时，只允许 `launch` 命令和**不带 `run_mode` 参数**的 `collect` 命令，其余命令直接拒绝。

**配置加载、建目录、分发、压缩**。[asys.py:119-143](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L119-L143)：`AsysConfigParser().parse()` 加载配置文件；`create_out_timestamp_dir()` 创建带时间戳的输出目录（4.3 节详讲）；随后从 `EXECUTE_CMD_FUNC` 字典取出实现类，`obj().run()` 一气呵成——实例化并执行；最后若 `--tar` 参数取值为 `'T'` 或 `'TRUE'`，调用 `compress_output_dir_tar()` 压缩输出目录。`main()` 的返回值 `task_res` 会成为进程退出码的依据。

**入口收尾**。[asys.py:146-149](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L146-L149)：`if __name__ == '__main__'` 块调用 `main()` 后执行 `clean_pycache()`，递归删除 asys 目录下所有 `__pycache__`，保持安装目录干净。

#### 4.1.4 代码实践

**实践目标**：通过给主流程加注释编号，确保自己能脱稿说出 `main()` 的每一步。

**操作步骤**：

1. 打开你本地仓库中的 `src/asys/asys.py`（只读阅读，不要提交改动）。
2. 准备一份个人副本：`cp src/asys/asys.py /tmp/asys_annotated.py`。
3. 在 `/tmp/asys_annotated.py` 中，按下面清单为每一步加 `# 步骤X` 注释：
   - 步骤 0a：参数去重（`_check_args_duplicate`）
   - 步骤 0b：恢复 SIGINT 默认行为
   - 步骤 1：命令行解析 + 取子命令
   - 步骤 1.5：纯展示型命令关闭日志
   - 步骤 2a：环境类型检查（含 RC 限制）
   - 步骤 2b：配置文件加载
   - 步骤 2c：创建时间戳输出目录
   - 步骤 3：字典分发执行
   - 步骤 4：按 `--tar` 参数压缩
4. 对照本讲 4.1.2 的流程图自查，是否有遗漏或顺序错误。

**需要观察的现象**：加注释的过程本身就是在验证你对流程的理解——如果某一行代码你不知道属于哪个步骤，说明该处需要回到 4.1.3 重新精读。

**预期结果**：注释完成后，`main()` 函数体（不含 docstring）应能被你用 8~10 行文字逐行复述。本实践为源码阅读型实践，无需运行环境，**待本地验证**的只有你的复述与他人（或本讲）的描述是否一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `parse()` 要用 `try/except SystemExit` 包起来？不包会发生什么？

**答案**：argparse 在遇到 `-h`、`--help` 或参数错误时会调用 `sys.exit()`，抛出 `SystemExit` 异常。如果不捕获，异常会一路冒泡到顶层，Python 打印 traceback 后以非零码退出，用户体验差。捕获后 `main()` 返回 `False`，由外层统一控制退出行为。

**练习 2**：`asys info` 执行时为什么要 `close_log()`，而 `asys collect` 不需要？

**答案**：`info` 的输出是直接给用户看的软硬件状态表格，混入 info/warning 级别运行日志会破坏输出格式；`collect` 的主要产物是落盘的输出目录，运行日志写进日志文件不影响用户，反而便于事后排查采集过程。

**练习 3**：在 RC 环境下执行 `asys collect -r ...`（带 `run_mode` 参数）会发生什么？对应哪行代码？

**答案**：会被拒绝。对应 [asys.py:111-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L111-L117)：`env_ret == 'RC'` 时，`command == collect_cmd and param_dict.get_arg("run_mode")` 为真，整个 `any([...])` 为真，记录错误日志 "The RC supports the launch command and the collect command without the -r parameter." 并返回 `False`。

### 4.2 EXECUTE_CMD_FUNC 子命令分发

#### 4.2.1 概念说明

`EXECUTE_CMD_FUNC` 是定义在模块顶层的字典，键是子命令名字符串，值是**类本身**（不是实例）。这是 Python 里最简洁的命令分发模式：

- 传统写法：`if command == 'collect': ... elif command == 'launch': ...` —— 8 个分支，新增命令要改入口逻辑。
- 字典写法：`EXECUTE_CMD_FUNC.get(command)` 一步取到类，`obj().run()` 统一调用 —— 新增子命令只需在字典里加一行。

能这样写的前提是：**所有子命令实现类遵守同一个约定——提供无参构造和 `run()` 方法**。这就是隐式的"命令接口"，Python 不需要显式继承基类，鸭子类型（duck typing）即可。

注意键不是硬编码的字符串，而是 `consts.collect_cmd` 这样的属性访问——`consts` 是 [const.py:249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L249) 实例化的 `Constants` 单例，其属性定义在 [const.py:206-246](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L206-L246)，依次返回 `'collect'`、`'launch'`、`'info'`、`'diagnose'`、`'health'`、`'analyze'`、`'config'`、`'profiling'` 八个字符串。

#### 4.2.2 核心流程

```text
用户输入: asys collect --device 0
   │
   ├─ CommandLineParser 解析出 command = 'collect'
   │
   ├─ EXECUTE_CMD_FUNC.get('collect')  →  AsysCollect 类
   │
   ├─ obj = AsysCollect()              # 实例化
   │
   └─ obj.run()                        # 执行，返回 task_res
```

如果 `command` 不在字典中，`get()` 返回 `None`，`obj().run() if obj else False` 中的条件表达式会让 `task_res = False`，`main()` 返回 `False` 表示失败。（实际上 `CommandLineParser` 已经校验过子命令合法性，这里是双保险。）

#### 4.2.3 源码精读

**字典定义**。[asys.py:61-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L61-L70)：8 个子命令到 8 个实现类的映射。这些类由文件头部 [asys.py:31-38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L31-L38) 的 `from collect import AsysCollect` 等导入语句引入，每个子命令对应 `src/asys/` 下一个同名目录（`collect/`、`launch/`、`info/`……）。

```python
EXECUTE_CMD_FUNC = {
    consts.collect_cmd: AsysCollect,
    consts.launch_cmd: AsysLaunch,
    consts.info_cmd: AsysInfo,
    consts.diagnose_cmd: AsysDiagnose,
    consts.health_cmd: AsysHealth,
    consts.analyze_cmd: AsysAnalyze,
    consts.config_cmd: AsysConfig,
    consts.profiling_cmd: AsysProfiling
}
```

**分发执行**。[asys.py:132-134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L132-L134)：`get(command)` 取类，条件表达式处理类不存在的情况，`obj().run()` 实例化并一步执行。这就是整个 asys 的"总线"——所有子命令都从这两行进入自己的实现。

```python
obj = EXECUTE_CMD_FUNC.get(command)
task_res = obj().run() if obj else False
```

**子命令名常量的来源**（参照）。[const.py:211-241](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L211-L241)：`Constants` 类用 `@property` 把每个子命令名封装成属性，例如 `collect_cmd` 返回 `'collect'`。[const.py:243-246](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L243-L246) 的 `cmd_set` 属性把全部命令名汇总成列表，供命令行解析层做合法性校验。

#### 4.2.4 代码实践

**实践目标**：亲手写一个最小可运行的字典分发程序，体会 `EXECUTE_CMD_FUNC` 的机制。

**操作步骤**：

1. 新建 `/tmp/mini_dispatch.py`，写入以下内容（**示例代码**，非项目原有代码）：

```python
class CmdA:
    def run(self):
        print('A executed')
        return True

class CmdB:
    def run(self):
        print('B executed')
        return True

EXECUTE_CMD_FUNC = {'a': CmdA, 'b': CmdB}

if __name__ == '__main__':
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else None
    obj = EXECUTE_CMD_FUNC.get(command)
    print('result:', obj().run() if obj else False)
```

2. 分别执行：`python3 /tmp/mini_dispatch.py a`、`python3 /tmp/mini_dispatch.py b`、`python3 /tmp/mini_dispatch.py c`。

**需要观察的现象**：前两次分别打印 `A executed` / `B executed` 且 `result: True`；第三次（未知命令）不实例化任何类，直接打印 `result: False`——这正是 `obj().run() if obj else False` 的兜底分支。

**预期结果**：三个命令的输出与你对 `EXECUTE_CMD_FUNC.get(command)` + 条件表达式的理解一致。本实践可在任意有 Python 3 的机器运行；若暂无环境，可纸面推演输出，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果要给 asys 新增一个子命令 `asys report`，入口文件需要改哪几处？

**答案**：至少三处——(1) 新建 `src/asys/report/` 目录并实现 `AsysReport` 类（含 `run()` 方法）；(2) 在 `asys.py` 头部加 `from report import AsysReport`；(3) 在 `EXECUTE_CMD_FUNC` 字典中加 `consts.report_cmd: AsysReport` 一项（同时在 `const.py` 的 `Constants` 类中新增 `report_cmd` 属性）。对比给 8 个 `if/elif` 分支加命令，改动点更少、更不容易漏。

**练习 2**：`EXECUTE_CMD_FUNC` 的值为什么是类而不是实例？

**答案**：字典在模块导入时就创建了，如果值是实例，8 个命令对象会在 asys 一启动时全部实例化，浪费资源且可能触发不必要的初始化副作用。存类、在确定命令后再 `obj()` 实例化，是"按需创建"，也避免不同子命令间的状态串扰。

**练习 3**：`obj().run() if obj else False` 中，如果用户传了非法子命令 `asys foo`，实际会走到 `else False` 吗？

**答案**：几乎不会。`CommandLineParser` 在步骤 1 已用 `cmd_set` 校验子命令合法性，非法命令在那一步就报错退出了；字典层面的兜底只是防御性编程的双保险。

### 4.3 输出目录创建与 tar 压缩

#### 4.3.1 概念说明

asys 有三类输出形态：

| 子命令 | 输出形态 |
| --- | --- |
| `info` / `health` / `diagnose` | 直接打印到终端，不建输出目录 |
| `collect` / `launch` / `analyze` | 创建 `asys_output_<时间戳>` 目录，采集/分析结果落盘 |
| 全部落盘命令 + `--tar T` | 目录再压缩为同名 `.tar.gz` 并删除原目录 |

负责这两步的是 `task_common.py` 的 `create_out_timestamp_dir()` 和 `compress_output_dir.py` 的 `compress_output_dir_tar()`。目录名带毫秒级时间戳，保证多次执行互不覆盖。

#### 4.3.2 核心流程

**创建输出目录**（`create_out_timestamp_dir`）：

```text
取当前命令
 ├─ 不在 [collect, launch, analyze] 中 → 直接返回 SUCCESS（不建目录）
 ├─ 计算父目录：--output 参数指定的目录，未指定则用当前路径
 ├─ 检查父目录写权限（os.access W_OK）→ 无权限返回 PERMISSION_FAILED
 ├─ 生成目录名：'asys_output_' + 本地时间 %Y%m%d%H%M%S%f（截去末 3 位 = 毫秒精度）
 ├─ 创建目录（FileOperate.create_dir）
 └─ 写入两处：模块级 _asys_output_path、ParamDict().asys_output_timestamp_dir
```

**压缩输出目录**（`compress_output_dir_tar`）：

```text
从 ParamDict 取输出目录
 ├─ 目录不存在 → 直接返回（静默跳过）
 ├─ 在父目录下创建 <目录名>.tar.gz（tarfile "w:gz" 模式）
 ├─ 把目录整体加入 tar 包（arcname 保持目录名，解压后不带父路径）
 └─ shutil.rmtree 删除原目录
```

时序上：创建发生在 `main()` 步骤 2c（子命令执行**之前**），压缩发生在步骤 4（子命令执行**之后**）——所以所有子命令实现都可以放心往 `ParamDict().asys_output_timestamp_dir` 里写文件。

#### 4.3.3 源码精读

**只在需要落盘的命令建目录**。[task_common.py:45-51](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/task_common.py#L45-L51)：`create_out_timestamp_dir()` 第一件事就是判断当前命令是否属于 `collect`/`launch`/`analyze`，不属于则直接返回 `SUCCESS`——这就是 `info` 等纯展示命令不会有输出目录的原因。内嵌的 `init_output_dir_parent()` 优先取 `--output` 参数，缺省落到执行时所在目录。

**权限检查与时间戳命名**。[task_common.py:53-65](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/task_common.py#L53-L65)：先用 `os.access(output_dir, os.W_OK)` 检查父目录可写；再用 `datetime.now(timezone.utc)` 取 UTC 时间、`astimezone()` 转本地时区后格式化 `%Y%m%d%H%M%S%f`，`[:-3]` 把微秒（6 位）截成毫秒（3 位），拼出形如 `asys_output_20260814103000123` 的目录名；创建成功后把绝对路径同时写入模块级变量 `_asys_output_path`（供 `get_asys_output_path()` 查询，见 [task_common.py:38-42](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/task_common.py#L38-L42)）和 `ParamDict().asys_output_timestamp_dir`（供压缩模块取用）。

```python
utc_dt = datetime.now(timezone.utc)
dir_name = 'asys_output_' + utc_dt.astimezone().strftime('%Y%m%d%H%M%S%f')[:-3]
...
ParamDict().asys_output_timestamp_dir = _asys_output_path
```

**压缩与删除原目录**。[compress_output_dir.py:26-39](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/compress_output_dir.py#L26-L39)：从 `ParamDict` 取出输出目录，存在性检查通过后，用 `tarfile.open(..., "w:gz")` 在**输出目录的父目录**下创建同名 `.tar.gz`，`tar.add(output_dir, arcname=...)` 保证包内路径以目录名开头（解压即还原）；最后 `shutil.rmtree(output_dir)` 删掉原目录——压缩是"替换"而非"复制"。

**触发时机**。[asys.py:138-140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L138-L140)：`main()` 的第 4 步，仅当 `--tar` 参数取值为 `'T'` 或 `'TRUE'`（不区分大小写的写法由参数解析层归一化）时调用压缩。

#### 4.3.4 代码实践

**实践目标**：在有昇腾设备的环境验证"目录 → tar 包"的完整生命周期；无设备则完成调用链追踪。

**操作步骤**（有环境时）：

1. 进入任意可写目录，执行 `asys collect --tar T`（若安装了 CANN 环境；仓库源码方式可执行 `python3 src/asys/asys.py collect --tar T`，需先解决模块导入路径）。
2. 执行 `ls` 观察产物：应看到 `asys_output_<时间戳>.tar.gz` 而**没有**同名目录。
3. 再执行一次 `asys collect`（不带 `--tar`），确认这次留下的是未压缩的目录。
4. `tar -tzf asys_output_*.tar.gz | head` 查看包内结构。

**操作步骤**（无环境时，源码阅读型实践）：

1. 从 [asys.py:128](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L128) 的 `create_out_timestamp_dir()` 调用点出发，沿调用链画出发目录创建的完整路径：`main()` → `task_common.create_out_timestamp_dir()` → `ParamDict` 写入。
2. 再从 [asys.py:139-140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L139-L140) 出发画压缩链：`main()` → `compress_output_dir.compress_output_dir_tar()` → `ParamDict` 读出。
3. 用一句话标出两条链的"交接点"。

**需要观察的现象**：`--tar T` 时目录被 tar 包替换；不带 `--tar` 时目录保留。包内第一层就是 `asys_output_<时间戳>/` 目录本身。

**预期结果**：交接点是 `ParamDict().asys_output_timestamp_dir`——创建方写入、压缩方读取，两者相隔整个子命令执行期。环境相关命令**待本地验证**；调用链追踪可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `create_out_timestamp_dir()` 里要先做 `os.access(output_dir, os.W_OK)` 检查，而不是直接创建让系统报错？

**答案**：提前检查可以在采集开始前就给用户明确的错误信息（"No write permission to asys output root directory"）并返回 `PERmission_FAILED` 这个语义化返回码（[const.py:166](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L166) 定义为 11）。如果直接创建，错误会以 OS 层的 PermissionError 异常形式在采集中途爆出，用户既看不懂也浪费了已经开始的采集工作。

**练习 2**：`strftime('%Y%m%d%H%M%S%f')[:-3]` 中 `[:-3]` 的作用是什么？

**答案**：`%f` 输出微秒，固定 6 位数字；`[:-3]` 截掉最后 3 位，把时间精度从微秒降到毫秒，目录名形如 `asys_output_20260814103000123`（17 位数字 = 年月日时分秒各 2 位共 14 位 + 毫秒 3 位）。

**练习 3**：如果 `compress_output_dir_tar()` 执行到一半（tar 包已建、原目录未删）进程被杀，会留下什么？下次执行会冲突吗？

**答案**：会留下一个可能不完整的 `.tar.gz` 和原输出目录。但不会冲突：因为目录名带毫秒级时间戳，下次执行会生成**新的**目录名和新的 tar 包名，旧的不完整产物只是残留垃圾，需要用户手动清理。这也是时间戳命名"互不覆盖"设计的另一面。

## 5. 综合实践

**任务：给 asys 入口画一张"全景执行图"并做一次纸面走查。**

综合本讲三个模块，完成以下 deliverable（全部基于真实源码，无需设备）：

1. **画主流程图**：以 4.1.2 的流程图为底稿，补上每个步骤触达的具体文件与函数，形成跨文件调用图，至少覆盖：
   - `asys.py:main()` 的 4 个编号步骤；
   - `common/task_common.py:create_out_timestamp_dir()` 的分支（建目录 / 不建目录 / 权限失败）；
   - `common/compress_output_dir.py:compress_output_dir_tar()` 的触发条件（`--tar ∈ {T, TRUE}`）。
2. **纸面走查 3 条命令**，写出每条命令经过的分支和最终产物：
   - `asys -h` → 应走 `print_help` 分支，无目录、无压缩；
   - `asys info` → 应触发 `close_log()`，`create_out_timestamp_dir` 直接返回 SUCCESS，无目录；
   - `asys collect --tar T` → 应建时间戳目录、执行采集、最后目录被 `.tar.gz` 替换。
3. **验证**：如果手头有可用环境，用真实命令跑一遍第 2 步的三条命令核对；没有则对照源码逐条自查，并标注「待本地验证」。

这个任务把"入口编排 → 字典分发 → 目录生命周期"三个模块串成一条线，完成后你就掌握了 asys 全部子命令的共同骨架，后续讲义（u2-l2 命令行体系、u2-l5 collect 子系统）都是在往这条骨架的特定环节上"挂肉"。

## 6. 本讲小结

- `asys.py` 是薄入口：只做守门（参数去重、解析、环境检查、配置加载）与编排，不包含任何业务逻辑。
- `main()` 四步主流程：① 命令行解析入 `ParamDict` → ② 环境类型检查（RC 环境限制命令集）+ 配置加载 + 建输出目录 → ③ `EXECUTE_CMD_FUNC` 字典分发 `obj().run()` → ④ 按需 tar 压缩。
- `EXECUTE_CMD_FUNC` 用"命令名 → 类"的字典实现分发，所有实现类遵守"无参构造 + `run()` 方法"的隐式接口；新增子命令只需加一个目录、一行 import、一行字典项。
- 只有 `collect` / `launch` / `analyze` 会创建 `asys_output_<毫秒时间戳>` 目录；`info`/`health`/`diagnose` 等纯展示命令会提前 `close_log()` 并直接打印终端。
- 输出目录路径通过 `ParamDict().asys_output_timestamp_dir` 在"创建方"（`task_common.py`）与"压缩方"（`compress_output_dir.py`）之间交接，压缩是替换式的：生成 `.tar.gz` 后删除原目录。
- 源码中存在两个"步骤 2"注释（环境检查与配置加载），是历史遗留，理解流程时以实际执行顺序为准。

## 7. 下一步学习建议

下一讲 **u2-l2《asys 命令行体系：Arg 枚举、ArgChecker 与 ParamDict》** 将深入本讲的步骤 ①：`CommandLineParser` 如何用 `Arg` 枚举声明式地构建 argparse、`ArgChecker` 如何校验参数、`ParamDict` 单例如何在整个进程内传递参数。

建议提前浏览的源码：

- [src/asys/cmdline/cmd_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py) —— 本讲中"黑盒"的 `asys_parser.parse()` 的实现。
- [src/asys/params/param_dict.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py) —— 本讲反复出现的 `ParamDict()` 的真身。
- 想先看某个子命令实现的话，可以从 [src/asys/health/asys_health.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/health/asys_health.py) 入手，验证它确实只提供 `run()` 方法就能被 `EXECUTE_CMD_FUNC` 分发。
