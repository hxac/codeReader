# asys 命令行体系：Arg 枚举、ArgChecker 与 ParamDict

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 asys 的「声明式命令行」设计：`Arg` 枚举描述参数、`Command` 枚举组装子命令，`argparse` 只是执行引擎。
2. 读懂 `cmd_parser.py` 如何在运行时动态构建出 8 个子命令的解析器。
3. 理解 `arg_checker.py` 中每个校验函数的职责，以及校验为什么放在 `parse_args()` 之后而不是 argparse 的 `type=` 里。
4. 理解 `ParamDict` 这个全局单例如何把 `argparse.Namespace`「翻译」成业务模块统一读取的参数字典。
5. 能独立为 asys 新增一个命令行参数（含校验函数），知道要改哪几个位置。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用大白话解释）：

- **argparse**：Python 标准库的命令行解析模块。传统用法是一个一个手写 `parser.add_argument("--foo", type=int, ...)`。当子命令和参数很多时，这种写法会散落大量重复代码。
- **enum.Enum**：Python 枚举。asys 的巧妙之处在于：枚举成员的值不是简单的整数或字符串，而是一个**字典**，把参数的全部元数据（名字、类型、校验器、是否必选、帮助文案）打包在一起。
- **单例模式（Singleton）**：保证一个类在整个进程中只有一个实例。`ParamDict` 用单例实现，使得任何模块 `import` 后拿到的都是同一个参数容器，不需要把参数在函数间层层传递。
- **声明式 vs 命令式**：命令式是"一步步告诉计算机怎么做"；声明式是"只描述我要什么，执行细节交给框架"。asys 的命令行体系就是声明式的——你只声明参数长什么样，构建解析器、校验、分发都由框架代码统一完成。
- **术语「参数」（argument）与「子命令」（command）**：`asys collect --output=/tmp` 中，`collect` 是子命令，`--output=/tmp` 是该子命令的参数。每个子命令有自己独立的参数集合。

上一讲（u2-l1）我们看到 `asys.py` 的 `main()` 第二步就是调用 `CommandLineParser().parse()`。本讲就深入这一步内部的三大角色。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/asys/cmdline/cmd_parser.py` | 命令行解析中枢：定义 `Arg` 参数枚举、`Command` 子命令枚举、`CommandLineParser` 类（动态构建 argparse、执行校验、把结果写入 ParamDict） |
| `src/asys/cmdline/arg_checker.py` | 参数校验器：一组 `check_arg_*` 函数 + `ArgChecker` 枚举把它们收拢，供 `Arg` 声明时引用 |
| `src/asys/params/param_dict.py` | 全局参数单例：接收解析结果，按子命令翻译成业务统一的 key（如 `-d` → `device_id`） |

三个文件的依赖关系是单向的：

```
cmd_parser.py ──引用──> arg_checker.py（Arg 声明中的 KEY_CHECKER）
cmd_parser.py ──写入──> param_dict.py（parse() 末尾的 set_args）
业务模块（collect/info/...）──只读──> param_dict.py
```

## 4. 核心概念与源码讲解

### 4.1 Arg / Command 枚举：声明式地描述整个命令行

#### 4.1.1 概念说明

asys 有 8 个子命令、30 多个参数。如果用传统 argparse 写法，`cmd_parser.py` 会有几百行高度重复的 `add_argument` 调用，而且"某个参数需要什么校验"和"参数怎么解析"两件事纠缠在一起。

asys 的解法是**把参数当成数据而不是代码**：

- `Arg` 枚举的每个成员描述一个参数的全部属性（名字、类型、校验器、是否必选、可选值、帮助文案）。
- `Command` 枚举的每个成员描述一个子命令：它叫什么、包含哪些 `Arg`、一句话帮助。
- `CommandLineParser.__init__` 遍历这两个枚举，自动生成 argparse 的子解析器。

这样新增一个参数只需在 `Arg` 里加一个枚举成员、在对应 `Command` 的 `KEY_ARGS` 列表里引用它，不用碰任何构建逻辑。

#### 4.1.2 核心流程

```
启动 asys
  │
  ▼
import 阶段：Arg 枚举成员的值（字典）里引用 ArgChecker.XXX
  │
  ▼
CommandLineParser.__init__()
  ├─ 创建顶层 parser（prog="asys"）
  ├─ add_subparsers(dest='subparser_name')
  ├─ 遍历 Command 枚举：
  │    ├─ RC 环境 → 只保留 collect / launch（跳过其余子命令）
  │    ├─ config 子命令 → 走专用 __set_config_cmd_parser
  │    ├─ analyze 子命令 → 走专用 __set_analyze_cmd_parser（file/path 互斥）
  │    └─ 其余 → 通用循环：把 KEY_ARGS 里每个 Arg 翻译成 add_argument 调用
  ▼
parse() → parser.parse_args() → check_args() → ParamDict().set_args()
```

几个翻译规则值得记住（见 4.1.3 第三段代码）：

- 参数名是 `d`、`r`、`p` 这类单字符时加单横线 `-`，其余加双横线 `--`。
- `all`、`quiet` 是**开关型参数**（不带值），用 `action="store_true"`。
- 声明了 `KEY_CHOICES` 时 metavar 置空，让 argparse 自动把可选值展示成 `[a|b|c]`。

#### 4.1.3 源码精读

先看元数据 key 的定义和两个颜色标记——帮助文案里用黄色 `<Optional>` 标可选参数、红色 `<Positional>` 标"事实上的位置参数"（asys 里没有真正的位置参数，必选的 `-r`/`task` 用红色提示用户必须给）：

[cmd_parser.py:L28-L39](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L28-L39)

这段定义了 `KEY_NAME`、`KEY_TYPE`、`KEY_CHECKER`、`KEY_REQUIRED`、`KEY_CHOICES`、`KEY_METAVAR` 等 key 常量，以及 `OPTIONAL_Y`（黄色 `<Optional>`）和 `POSITIONAL_R`（红色 `<Positional>`）两个 ANSI 颜色标记。

接着看 `Arg` 枚举的两个典型成员：

[cmd_parser.py:L44-L62](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L44-L62)

- `TASK_DIR`：可选参数 `--task_dir`，校验器是 `ArgChecker.DIR_EXIST`（目录必须已存在）。
- `TASK`：必选参数 `task`，校验器是 `ArgChecker.EXECUTABLE`（必须是可执行的脚本/命令）。
- `TAR`：可选参数 `--tar`，校验器 `ArgChecker.TAR_CHECK`，取值 F/T/False/True。

注意 `KEY_CHECKER` 的值直接就是 `ArgChecker` 的枚举成员——**声明参数的同时就把校验器绑定好了**。

带可选值约束的例子（`DIS_RUN`，即 `diagnose -r`）：

[cmd_parser.py:L102-L108](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L102-L108)

`KEY_CHOICES` 限定 `-r` 只能取 `stress_detect / hbm_detect / cpu_detect / component / aicore_stl_detect` 之一，argparse 会自动拒绝其他取值（这层校验由 argparse 完成，不走 ArgChecker）。

再看 `Command` 枚举如何把参数组装成子命令：

[cmd_parser.py:L210-L257](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L210-L257)

每个子命令就是一个 `KEY_NAME`（如 `"collect"`）+ 一个 `KEY_ARGS` 列表（引用哪些 `Arg` 成员）+ 一句 `KEY_HELP`。例如 `ANALYZE` 挂了 10 个参数，`HEALTH` 只挂 1 个 `Arg.DEVICE`。

最后是核心的动态构建循环：

[cmd_parser.py:L276-L308](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L276-L308)

这段代码遍历 `Command` 枚举做四件事：RC 环境过滤（只留 collect/launch）、config 和 analyze 走专用分支、其余子命令逐个把 `KEY_ARGS` 翻译成 `add_argument` 调用。注意第 293 行的命名规则（单字符加 `-`）和第 300 行的开关型参数特判。

`analyze` 的 `--file` / `--path` 互斥通过 argparse 原生互斥组实现：

[cmd_parser.py:L331-L348](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L331-L348)

只有 `file` 和 `path` 进了 `add_mutually_exclusive_group`，其余参数照常加。

#### 4.1.4 代码实践

**实践目标**：验证"枚举声明 → help 输出"的映射关系，确认帮助文案确实来自 `Arg` 的 `KEY_HELP`。

**操作步骤**：

1. 在装好 asys 的环境（或仓库根目录 `src/asys/` 下）执行 `python3 asys.py -h`，再执行 `python3 asys.py analyze -h`。
2. 对比输出与 `cmd_parser.py` 中 `Command.ANALYZE` 的 `KEY_HELP`、`Arg.ANALYZE_RUN` 等成员的 `KEY_HELP`。
3. 试着传一个非法 choices 值：`python3 asys.py info -r=foo`，观察 argparse 的报错格式。

**需要观察的现象**：

- 顶层 help 列出 8 个子命令及其一句话描述（来自 `Command` 的 `KEY_HELP`）。
- `analyze -h` 中 `--file` 与 `--path` 显示在同一互斥组（usage 行中出现 `[--file FILE | --path PATH]`）。
- `-r=foo` 报错 `invalid choice: 'foo' (choose from 'hardware', 'software', 'status')`——这是 argparse 层的校验，先于 ArgChecker 执行。

**预期结果**：help 文案、可选值列表与枚举声明逐字对应。若在无昇腾设备环境运行，`-h` 通常仍可工作（构建 parser 不依赖设备），但走校验的命令会因设备探测失败而报错——此时以阅读源码为主，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Arg` 枚举中 `DEVICE` 和 `ANALYZE_DEVICE` 的 `KEY_NAME` 都是 `"d"`，为什么允许重复声明？

**答案**：`Arg` 枚举描述的是"参数规格模板"而不是唯一参数。两个成员的名字、类型、校验器相同，只是 `KEY_HELP` 不同（后者注明"仅对 aicore_error 生效"）。它们分别被 `Command` 的不同子命令引用，argparse 是按子命令各自构建的，所以 `--help` 文案可以按子命令差异化。

**练习 2**：`COLLECT_RUN`、`DIS_RUN`、`INFO_RUN`、`ANALYZE_RUN`、`PROFILING_RUN` 的 `KEY_NAME` 都是 `"r"`，这套设计带来了什么好处和代价？

**答案**：好处是用户交互统一——所有子命令都用 `-r` 指定"运行模式"，而每个子命令的取值集合（choices）各自独立，互不污染；代价是 `ParamDict.set_args()` 必须按子命令分支处理（见 4.3.3），不能一刀切地翻译。

**练习 3**：`metavar=" "`（空格）在 argparse 里起什么作用？

**答案**：`metavar` 控制 usage 帮助中参数占位符的显示。设为空格可以让 `--output ` 后面不显示 `OUTPUT` 之类的占位符，让 usage 行更简洁——asys 的几乎所有带值参数都做了这个处理；唯独带 `KEY_CHOICES` 的参数把 metavar 置为 `None`（见 `__init__` 中第 297-299 行），以便 argparse 自动渲染 `[stress_detect|hbm_detect|...]` 的可选值提示。

### 4.2 ArgChecker：参数合法性的第二道防线

#### 4.2.1 概念说明

argparse 能做的校验有限：类型转换失败、choices 不在列表内。但 asys 还需要校验很多**语义规则**：

- `--task_dir` 指向的目录必须真实存在且不含非法字符。
- `task`（launch 的必选参数）必须像一条可执行命令，比如不能只写 `python` 而不带脚本。
- `-d` 设备号不能超过本机实际设备数量——这需要**运行时查询设备**，argparse 完全做不到。

`arg_checker.py` 就是这些语义校验的集合。它把一组 `check_arg_*` 函数收拢进 `ArgChecker` 枚举，让 `Arg` 声明能以 `KEY_CHECKER: ArgChecker.DEVICE_ID` 的形式引用——本质上和 4.1 的思路一脉相承：**校验器也是可声明的数据**。

#### 4.2.2 核心流程

校验发生在 `parse()` 内、`parse_args()` 之后：

```
parse()
  ├─ parser.parse_args()        # 第一道防线：类型、choices、required、互斥
  ├─ check_args(args)           # 第二道防线：ArgChecker 语义校验
  │    ├─ match_command() 找到子命令的 Command 枚举
  │    ├─ 遍历该命令的 KEY_ARGS
  │    ├─ getattr(args, name) 取值；None（未传）则跳过
  │    └─ check_arg_with_checker() 调用校验函数，失败立即返回 FAILED
  └─ ParamDict().set_args(args) # 全部通过后才写入全局参数
```

关键设计：**可选参数未传（值为 `None`）时直接跳过校验**，所以每个 `check_arg_*` 函数只需要处理"用户传了值"的情形。

#### 4.2.3 源码精读

`ArgChecker` 枚举把校验函数收拢（函数名即枚举成员的值）：

[arg_checker.py:L235-L245](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L235-L245)

8 个校验器分别对应：目录存在、目录可创建、可执行命令、tar 取值、设备号、文件/路径存在且可读、core 文件、符号路径。

最基础的三个字符级检查，被路径类校验复用：

[arg_checker.py:L32-L62](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L32-L62)

`path_str_check` 是组合检查：非空 → 无空格 → 只含 `[a-zA-Z0-9_-.\/]` 合法字符。`check_arg_exist_dir` 在此基础上追加 `os.path.isdir()` 判断。

设备号校验是唯一需要访问硬件的校验器：

[arg_checker.py:L173-L182](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L173-L182)

先做静态范围检查（`DEVICE_ID_MIN` 到 `DEVICE_ID_MAX`），再用 `DeviceInfo().get_device_count()` 查询本机实际设备数做动态检查——`-d` 不能指向不存在的设备。这也解释了为什么校验不能塞进 argparse 的 `type=`：`type` 函数虽然也会被调用，但错误信息格式不受控，且 asys 需要统一的 `RetCode` 返回体系。

`check_arg_create_dir`（`--output` 的校验器）展示了较复杂的反向扫描逻辑：

[arg_checker.py:L117-L162](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L117-L162)

如果目标目录不存在，就从其父目录逐级向上找第一个存在的祖先，确认可写后**顺带创建整个目录**。这是一个"校验即生效"的例子——校验通过时副作用已经完成。

校验的调用侧在 `cmd_parser.py`：

[cmd_parser.py:L387-L412](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L387-L412)

`check_args` 就是"第二道防线"的实现：遍历子命令声明的每个 `Arg`，取值非 `None` 则调用其绑定的 checker，任一失败立即返回 `RetCode.FAILED`，`main()` 随后退出。

#### 4.2.4 代码实践

**实践目标**：体会"两道防线"的分工——同一类非法输入，argparse 拦截什么、ArgChecker 拦截什么。

**操作步骤**：

1. `python3 asys.py info -r=foo` → 观察 argparse 的 choices 报错。
2. `python3 asys.py collect --task_dir=/not/exist/dir` → 观察 `check_arg_exist_dir` 的报错（日志格式为 `Argument "task_dir" is not an exist directory...`）。
3. `python3 asys.py collect --task_dir=/tmp/a b`（带空格）→ 观察 `space_check` 的报错。

**需要观察的现象**：三种非法输入的错误信息来源不同——第 1 步是 argparse 原生格式（含 `invalid choice`），第 2、3 步是 `log_error` 输出的 asys 自有格式。

**预期结果**：能够区分哪些合法性由 choices/互斥组保证，哪些由 ArgChecker 保证。无设备环境下第 2、3 步在到达设备探测前就会失败，可以完成观察；涉及 `-d` 的场景标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `check_arg_executable` 要专门拒绝"裸解释器"（如只有 `python` 没有脚本）？

**答案**：`launch` 的 `task` 参数语义是"执行一条业务命令并采集其运行期间的信息"。裸 `python` 会挂起等待交互输入，且没有可采集的业务。该函数用正则匹配 `sh|bash|python[0-9.]*` 开头但没有后续 `.sh`/`.py` 脚本的输入并拒绝，见 [arg_checker.py:L84-L114](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L84-L114)。

**练习 2**：`check_arg_tar` 允许 `F/T/FALSE/TRUE`，但 `ParamDict._set_arg_tar` 里做了 `args.tar.upper()`——为什么不直接在 checker 里把值规范化成布尔？

**答案**：职责分离。ArgChecker 只回答"合法吗"（是/否），不修改数据；数据的规范化（大小写归一）放在 `ParamDict` 入库时做。这样校验函数保持纯函数性质，易于测试。

**练习 3**：如果给一个参数声明了 `KEY_CHECKER: None`（如 `TIMEOUT`），它的合法性靠什么保证？

**答案**：只靠 argparse 的 `type=int`（非整数会被 argparse 拒绝）。帮助文案中的取值范围（如 `[1, 604800]`）只是说明，代码中并未对该范围做强制校验——这是阅读源码才能发现的"文档强于代码"的例子。

### 4.3 ParamDict：参数的全局单例容器

#### 4.3.1 概念说明

`parse_args()` 返回的 `argparse.Namespace` 是个"哑对象"：属性名就是参数名（`args.d`、`args.task_dir`），不同子命令的同类参数语义不清（`-d` 到底是 device_id 还是别的）。如果各业务模块直接读 Namespace，就要处处处理 `getattr(args, 'x', None)` 这类琐碎细节。

`ParamDict` 解决两个问题：

1. **传递问题**：用 Singleton 元类保证全进程唯一实例，任何模块 `from params import ParamDict` 后 `ParamDict().get_arg("device_id")` 拿到的都是同一份数据，参数不用在函数签名间层层透传。
2. **翻译问题**：`set_args()` 按子命令把 Namespace 翻译成业务统一命名的 key——`-d` 变 `device_id`、`-r` 变 `run_mode`、`--tar` 的值统一大写、`--symbol_path` 按逗号切成列表。

它同时还是全局状态的集散地：输出目录（`asys_output_timestamp_dir`）、环境类型（`env_type`）、业务进程 PID（`task_pid`）、ini 配置都挂在这里。

#### 4.3.2 核心流程

```
CommandLineParser.parse() 成功
  └─ ParamDict().set_args(args)
       ├─ 记录子命令名 self.__command = args.subparser_name
       ├─ 按子命令分支（diagnose/health/info/analyze/config/collect/launch/profiling）
       │    每个分支调用若干 _set_arg_* 方法：
       │    ├─ _set_arg_d：-d → "device_id"
       │    ├─ _set_arg_r：-r → "run_mode"
       │    ├─ _set_arg_tar：--tar 值 upper() 后原样存
       │    ├─ _set_arg_symbol_path：按 "," 切成 list
       │    └─ _set_arg_common：通用参数（file/path/output/timeout...）原名存入
       └─ 末尾统一存 --output

业务模块（如 collect）读取：
  ParamDict().get_arg("task_dir") / get_arg("run_mode") ...
```

注意 `__add_arg` 的行为：**同名 key 只写第一次**，之后重复写入会被忽略（返回 `False`）——这是一种防覆盖保护。

#### 4.3.3 源码精读

类的骨架与 Singleton 元类：

[param_dict.py:L30-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L30-L43)

`class ParamDict(metaclass=Singleton)` 保证唯一实例；私有属性 `__args`、`__deps`、`__ini`、`__env_type` 等构成全局状态。`tools_path` 属性用文件位置反推 asys 工具根目录，常用于定位随包安装的资源。

读取接口：

[param_dict.py:L56-L74](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L56-L74)

`get_arg(key, default=False)` 是业务模块的标准读取入口——key 不存在时返回 `default` 而非抛异常，调用方用 `if ParamDict().get_arg("xxx"):` 判断参数是否设置。

翻译逻辑的核心（节选 diagnose 与 analyze 分支）：

[param_dict.py:L114-L141](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L114-L141)

`set_args` 按子命令分发：diagnose 存 `device_id / run_mode / timeout`；analyze 额外处理 `symbol_path` 的列表化。每个 `_set_arg_*` 内部都先判断 `is not None`（参数没传就不入库），最终 `__add_arg` 只在 key 不存在时写入：

[param_dict.py:L174-L179](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L174-L179)

另一个值得注意的细节：`_set_arg_common` 用 `eval(f"args.{arg_name}")` 动态取属性名（arg_name 来自代码内部枚举而非用户输入，因此这里没有注入风险，但这种写法值得留意）：

[param_dict.py:L109-L112](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L109-L112)

写回侧，`cmd_parser.py` 的 `parse()` 在一切校验通过后才调用 `set_args`：

[cmd_parser.py:L414-L433](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L414-L433)

顺序是：config 互斥预扫描 → `parse_args()` → `check_args()` → `ParamDict().set_args(args)`。保证进入全局容器的参数一定是合法参数。

#### 4.3.4 代码实践

**实践目标**：写一个最小脚本，直接复用 asys 的三大 cmdline 组件跑通"解析 → 校验 → 入库"全链路，观察 ParamDict 的翻译结果。

**操作步骤**：

1. 进入 `src/asys/` 目录（保证包内相对 import 可用）。
2. 新建 `/tmp/demo_param.py`（**示例代码**，不是仓库文件）：

```python
import sys
sys.argv = ["asys", "diagnose", "-r=component", "-d=0", "--timeout=30", "--output=/tmp/asys_demo"]
sys.path.insert(0, ".")

from cmdline.cmd_parser import CommandLineParser
from params import ParamDict

ret = CommandLineParser().parse()
print("parse ret:", ret)
pd = ParamDict()
print("command:", pd.get_command())
print("run_mode:", pd.get_arg("run_mode"))
print("device_id:", pd.get_arg("device_id"))
print("timeout:", pd.get_arg("timeout"))
print("output:", pd.get_arg("output"))
```

3. 执行 `python3 /tmp/demo_param.py`。
4. 把 `sys.argv` 换成 `["asys", "analyze", "-r=coredump", "--symbol_path=/tmp/a,/tmp/b"]` 再跑一次，观察 `symbol_path` 的值变成了列表。

**需要观察的现象**：`-r` 的值以 `run_mode` 这个 key 被读出；`-d` 变成 `device_id`；`symbol_path` 被逗号切分。

**预期结果**：第一次输出 `command: diagnose, run_mode: component, timeout: 30`。`-d=0` 的校验依赖 `DeviceInfo().get_device_count()`，在无昇腾设备环境可能报错——此时把 `-d=0` 从 argv 中去掉再观察其余参数；设备相关行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`get_arg` 的默认返回值是 `False` 而不是 `None`，这有什么影响？

**答案**：调用方可以直接写 `if ParamDict().get_arg("quiet"):` 把 `False` 当"未设置"处理，布尔语义明确。但如果某参数的合法值可能是 `0`（如 `--symbol 0`），`if ParamDict().get_arg("symbol"):` 会把 0 误判为假——读取这类参数时应显式与默认值比较或先判断 key 存在性。

**练习 2**：`set_args` 里为什么每个分支都以 `if self.__command == consts.xxx_cmd:` 独立判断，而不是 `elif`？

**答案**：`__command` 只会等于其中一个子命令名，`elif` 在语义上等价；用独立 `if` 使得每个子命令的处理块可以独立 `return`（如 health、info、config 分支提前返回，跳过末尾通用的 `_set_arg_common(args, "output")`）。这是按子命令"短路"的写法选择。

**练习 3**：`ParamDict` 里除了 `__args` 还存了 `__ini`、`__env_type`、`__task_pid`，为什么这些也要放进同一个单例？

**答案**：它们都是"一次探测、处处使用"的全局运行时上下文：env_type 在 `cmd_parser.py` 构建 parser 时就要读（RC 环境过滤子命令），`task_pid` 是 launch 拉起业务后采集模块要用的进程号。放进同一个单例避免再造多个全局对象，也符合"入口写入、下游只读"的数据流向。

## 5. 综合实践

把本讲三个模块串起来，完成讲义规格中指定的任务：**为 asys 新增一个假想参数 `--mock`（int 型、可选、带 help 文案）**，并写出它的 ArgChecker 校验函数。全程在纸面/临时副本完成，不改仓库源码（若要动手，改完记得还原）。

**第一步：声明 Arg 枚举成员。** 参照 [cmd_parser.py:L189-L193](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L189-L193) `PERIOD` 的格式（int 型可选参数的标准写法），在 `Arg` 枚举中加入（**示例代码**）：

```python
MOCK = {
    KEY_NAME: "mock", KEY_TYPE: int, KEY_CHECKER: ArgChecker.MOCK_CHECK, KEY_REQUIRED: False, KEY_METAVAR: " ",
    KEY_HELP: f"{OPTIONAL_Y} Specifies the mock level for testing, value range: [1, 100]. Defaults to 1."
}
```

插入位置建议：放在 `Arg` 枚举尾部（`AIC_METRICS` 之后），因为枚举成员的排列顺序只影响代码可读性，不影响功能。

**第二步：挂到子命令。** 选一个宿主子命令（比如 `Command.COLLECT`），把 `Arg.MOCK` 追加到其 `KEY_ARGS` 列表（对应 [cmd_parser.py:L212-L218](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L212-L218)）。由于 `mock` 不是 `d/r/p`、不在 `["all", "quiet"]`、没有 choices，它会自动落入通用 `add_argument` 分支——不需要改任何构建逻辑，这正是声明式设计的收益。

**第三步：写校验函数。** 在 `arg_checker.py` 中仿照 `check_arg_tar` 的简洁风格（**示例代码**）：

```python
def check_arg_mock(arg_name, arg_val):
    if arg_val < 1 or arg_val > 100:
        log_error("Argument \"{}\" value is range of [1, 100], input: \"{}\"".format(arg_name, arg_val))
        return RetCode.FAILED
    return RetCode.SUCCESS
```

并把它注册进 `ArgChecker` 枚举：`MOCK_CHECK = check_arg_mock`（加在 [arg_checker.py:L235-L245](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py#L235-L245) 的枚举尾部）。

**第四步：让参数进入 ParamDict。** 在 `param_dict.py` 的 `set_args()` 的 `collect` 分支中加一行 `self._set_arg_common(args, "mock")`（对应 [param_dict.py:L149-L156](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L149-L156)），业务侧即可用 `ParamDict().get_arg("mock")` 读取。

**验证**：`python3 asys.py collect --mock=200` 应触发 `check_arg_mock` 报错；`--mock=5` 应正常通过解析（后续因无设备/无采集环境而失败属于预期）。完整运行效果「待本地验证」。

**检查清单**（新增一个 asys 参数需要动的 4 个位置）：

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1 | `cmdline/cmd_parser.py` | `Arg` 枚举加成员 |
| 2 | `cmdline/cmd_parser.py` | `Command` 的 `KEY_ARGS` 引用 |
| 3 | `cmdline/arg_checker.py` | 校验函数 + `ArgChecker` 枚举注册 |
| 4 | `params/param_dict.py` | `set_args()` 对应分支 `_set_arg_*` |

## 6. 本讲小结

- asys 用**声明式命令行**设计：`Arg` 枚举成员的值是元数据字典（名字/类型/校验器/必选/choices/help），`Command` 枚举把 `Arg` 组装成 8 个子命令，`CommandLineParser.__init__` 遍历枚举动态生成 argparse 子解析器。
- 校验分两道防线：argparse 负责**类型、choices、required、互斥**（第一道）；`ArgChecker` 枚举收拢的 `check_arg_*` 函数负责**语义校验**（目录存在、设备号不越界、可执行命令等），在 `parse_args()` 之后由 `check_args()` 统一驱动。
- 未传的可选参数（值为 `None`）直接跳过 ArgChecker；`check_arg_create_dir` 这类校验器还带"校验即创建目录"的副作用。
- `ParamDict` 是 Singleton 全局单例：`set_args()` 按子命令把 `Namespace` 翻译成业务统一 key（`-d`→`device_id`、`-r`→`run_mode`、`--tar` 归一大写、`--symbol_path` 切列表），`__add_arg` 先到先得防覆盖。
- 新增一个参数只需动 4 处：`Arg` 枚举、`Command.KEY_ARGS`、`ArgChecker`（可选）、`set_args()` 分支——构建与分发逻辑完全不用碰。

## 7. 下一步学习建议

本讲搞清楚了"参数怎么进来、怎么存"，下一讲 **u2-l3（asys 公共设施）** 将讲解这些参数的下游依赖：`common/log.py` 的日志封装（ArgChecker 里的 `log_error` 就来自它）、`common/const.py` 的 `RetCode` 与 `consts`、`cmd_run.py` 外部命令执行、`device.py` 设备信息。之后再进入 u2-l4（芯片适配层）与 u2-l5（collect 采集框架），那里会大量出现 `ParamDict().get_arg(...)` 的读取代码，建议阅读时留意业务模块是如何与本讲的参数容器衔接的。
