# u1-l3 目录结构与入口文件地图

## 1. 本讲目标

学完本讲，你应该能够：

- 独立说出 `src/` 下各组件目录（asys、msaicerr、msprof、hccl_test、operator_cmp、third_party）的职责。
- 准确写出四个组件各自的入口文件路径：`asys.py`、`msaicerr.py`、msprof 的 `msprof_bin.cpp`、hccl_test 的 `Makefile` + `hccl_test_main.cc`。
- 理解 `src/asys/asys` 这个软链接与 `.run` 包安装释放到 CANN `tools/` 目录之间的关系。
- 在源码中定位 asys 的子命令分发字典 `EXECUTE_CMD_FUNC`，并说出它支持的 8 个子命令。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **入口文件（entry point）**：程序启动时第一个被执行的文件。Python 工具通常是 `xxx.py` 中的 `main()` 函数；C++ 工具通常是包含 `int main()` 的 `.cpp` 文件；Makefile 项目则是构建系统读取的第一个 Makefile。
- **软链接（symbolic link）**：类似 Windows 的"快捷方式"。`src/asys/asys -> ./asys.py` 表示执行 `asys` 等价于执行 `asys.py`，它依赖 `asys.py` 第一行的 `#!/usr/bin/env python3`（shebang）来告诉操作系统用哪个解释器。
- **`install()` 规则（CMake）**：CMake 打包时的一条指令，声明"把哪些文件拷贝到安装包（进而安装到目标机器）的哪个目录"。`.run` 包安装时就是按这些规则把工具释放到 CANN 安装目录的 `tools/` 子目录下。
- **子命令（subcommand）**：类似 `git commit`、`git pull` 中的 `commit`、`pull`。asys 也有 `asys collect`、`asys info` 这样的用法，每个子命令对应一个实现类。
- **字典分发（dispatch table）**：用"字典 + 类"代替"一长串 if/else"来路由命令的设计模式。`{'collect': AsysCollect, 'info': AsysInfo, ...}`，查表拿到类，实例化后调用 `run()`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目总览，含目录结构说明和四个组件的运行示例 |
| `src/asys/asys.py` | asys 的 Python 入口：参数解析、环境检查、子命令分发 |
| `src/asys/common/const.py` | asys 常量定义，含子命令名字符串（`collect`、`info` 等） |
| `src/asys/cmdline/cmd_parser.py` | asys 命令行解析器，`Command` 枚举声明全部子命令及其参数 |
| `src/asys/CMakeLists.txt` | asys 的安装规则：整个目录释放到 `tools/ascend_system_advisor` |
| `src/msaicerr/msaicerr.py` | msaicerr 的 Python 入口：`-p`/`-d`/`-e` 三种模式分发 |
| `src/msaicerr/CMakeLists.txt` | msaicerr 的安装规则：释放到 `tools/msaicerr` |
| `src/hccl_test/Makefile` | hccl_test 的传统 Make 构建入口：编译 11 个集合通信测试可执行文件 |
| `src/hccl_test/common/src/hccl_test_main.cc` | hccl_test 各测试二进制共享的 `main()` 所在文件 |
| `src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp` | msprof C++ collector 的 `main()` 入口 |

## 4. 核心概念与源码讲解

### 4.1 仓库目录结构：一张地图

#### 4.1.1 概念说明

oam-tools 仓库采用"一个 `src/` 下并列多个独立组件"的布局。每个组件有自己的构建文件（CMakeLists.txt 或 Makefile），由根 `CMakeLists.txt` 统一挂载（这是 u1-l2 讲过的构建体系）。理解目录职责，是后续按需深入源码的前提——遇到问题时先知道"该去哪个目录找"。

#### 4.1.2 核心流程

仓库顶层目录职责一览：

```text
oam-tools/
├── cmake/             # 构建配置（CMake 模块、第三方库下载脚本）
├── scripts/           # 辅助构建与检查脚本（oat_check.sh、run_tests.sh 等）
├── src/               # 源代码（本讲重点）
│   ├── asys/          # asys：故障信息收集工具（Python）
│   ├── msaicerr/      # msaicerr：AI Core Error 分析工具（Python）
│   ├── msprof/        # msprof：性能调优工具（C++ collector + Python 分析脚本）
│   ├── hccl_test/     # hccl_test：HCCL 性能测试工具（C++）
│   ├── operator_cmp/  # 算子比对工具
│   └── third_party/   # 依赖的第三方库头文件
├── test/              # UT/ST 测试用例
├── docs/              # 项目文档（中/英文）
├── init_env.sh        # 开发环境一键安装脚本
├── build.sh           # 项目编译脚本
├── CMakeLists.txt     # CMake 主配置文件
└── version.cmake      # 版本与依赖声明
```

#### 4.1.3 源码精读

这份目录结构与 README 中给出的说明完全一致，见 [README.md:36-58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L36-L58)，其中明确了 `src/` 下六个子目录的分工。

进入 `src/` 后还能进一步细分（本讲用 `ls` 实际确认过）：

- `src/asys/` 下按功能分目录：`collect/`（采集）、`launch/`（业务复跑）、`info/`、`health/`、`diagnose/`、`analyze/`（分析）、`cmdline/`（命令行）、`config/` 与 `config_cmd/`（配置）、`profiling/`（性能采集）、`common/`（公共设施）、`params/`（参数容器）——**每个子命令恰好对应一个同名目录**，这是后续 u2 单元逐个精读的地图。
- `src/msaicerr/` 下核心是 `msaicerr.py`（入口）+ `ms_interface/`（全部实现模块）+ `proto_parse/`（Dump 解析的 C++/protobuf 部分）。
- `src/msprof/` 下只有 `collector/`（`basic/` 与 `dvvp/` 两个 C++ 子模块）和 `inc/`；分析侧以 Python wheel 形式在打包期引入。
- `src/hccl_test/` 下是 `Makefile` + `common/`（共享骨架代码）+ `opbase_test/`（每个集合通信算子一个测试文件）+ `hostfile`（多机配置）。

#### 4.1.4 代码实践

1. **实践目标**：把目录结构"走"一遍，建立空间感。
2. **操作步骤**：在仓库根目录依次执行 `ls src/`、`ls src/asys/`、`ls src/msaicerr/`、`ls src/msprof/collector/`、`ls src/hccl_test/`。
3. **需要观察的现象**：asys 目录名与子命令名（collect、info、health……）一一对应；msprof 的 C++ 代码全部在 `collector/` 下。
4. **预期结果**：输出与本讲 4.1.2 的目录树一致。

#### 4.1.5 小练习与答案

**练习 1**：如果想给 asys 新增一个子命令 `asys dump`，应该在 `src/asys/` 下新建什么目录？还要动哪两个既有文件？

**答案**：新建 `src/asys/dump/` 目录存放实现类（模仿 `collect/` 等目录的组织方式）；同时要改 `src/asys/common/const.py`（加 `dump_cmd` 常量并加入 `cmd_set`）和 `src/asys/cmdline/cmd_parser.py`（在 `Command` 枚举中声明 DUMP 命令及参数），最后在 `asys.py` 的 `EXECUTE_CMD_FUNC` 中注册。

**练习 2**：`src/operator_cmp/` 和 `src/third_party/` 为什么不算"四大组件"？

**答案**：`operator_cmp` 是辅助性的算子比对工具，`third_party` 只存放第三方库头文件，二者都不在 README 宣传的 asys/msaicerr/msprof/hccl_test 四大运维能力版图内；但它们同样被根 CMakeLists 挂载进构建体系。

### 4.2 asys 入口：asys.py 与软链接

#### 4.2.1 概念说明

`src/asys/asys.py` 是 asys 工具的唯一 Python 入口。它本身几乎不含业务逻辑，只做"守门员"的工作：解析命令行、检查环境、加载配置、创建输出目录，然后把控制权交给对应子命令的实现类。

目录下还有一个软链接 `src/asys/asys -> ./asys.py`（用 `ls -la` 可确认，链接目标仅 9 字节即 `./asys.py`）。它的意义在于：安装到 CANN `tools/` 目录后，用户可以不带 `python3` 前缀、不带 `.py` 后缀直接执行 `asys`，体验上像一个原生命令。

#### 4.2.2 核心流程

`main()` 的执行过程可以概括为五步（编号与源码注释一致）：

```text
asys 命令行输入
    │
    ├─ 前置：_check_args_duplicate() 去重检查（同一参数只允许出现一次）
    ├─ 前置：注册 SIGINT 信号处理（Ctrl+C 直接终止）
    │
    ├─ 1. CommandLineParser 解析命令行 → ParamDict 全局参数容器
    │       └─ 无子命令时打印 help 退出
    ├─ 2. 检查执行环境类型（RC 环境只支持 collect/launch）
    ├─ 2'. AsysConfigParser 读取配置文件
    ├─    create_out_timestamp_dir() 创建带时间戳的输出目录
    │
    ├─ 3. EXECUTE_CMD_FUNC.get(command) 查表得到实现类 → obj().run()
    │
    └─ 4. 若指定 --tar=T/TRUE，compress_output_dir_tar() 压缩输出目录
```

子命令分发的关键数据结构就是字典 `EXECUTE_CMD_FUNC`：键是子命令名字符串，值是处理类。这比一串 `if command == 'collect': ... elif ...` 更易扩展——新增子命令只需加一行注册。

#### 4.2.3 源码精读

入口文件头部导入各子命令实现类，这些 import 正是子命令目录与入口的连接点：[src/asys/asys.py:19-38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L19-L38)。这段代码从 `collect`、`launch`、`info`、`diagnose`、`health`、`analyze`、`config_cmd`、`profiling` 各目录导入 `AsysCollect`、`AsysLaunch` 等 8 个实现类。

子命令分发字典：[src/asys/asys.py:61-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L61-L70)。这段代码定义了 `EXECUTE_CMD_FUNC`，把 `consts.collect_cmd`（值为 `'collect'`）等 8 个子命令名映射到对应处理类。

子命令名字符串的定义处在常量模块：[src/asys/common/const.py:206-249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L206-L249)。`Constants` 类以 property 形式给出 `help`/`collect`/`launch`/`info`/`diagnose`/`health`/`analyze`/`config`/`profiling` 九个名字（`help` 仅用于打印帮助，不进分发字典），最后实例化为模块级单例 `consts`。

主流程中执行分发的那两行：[src/asys/asys.py:132-134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L132-L134)。这段代码用 `EXECUTE_CMD_FUNC.get(command)` 查表，取到类后 `obj().run()` 执行任务——"字典分发"模式落到实处。

安装释放规则：[src/asys/CMakeLists.txt:9-13](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/CMakeLists.txt#L9-L13)。这段 `install(DIRECTORY ...)` 把整个 `${ASYS_DIR}`（包括软链接 `asys`）原样拷贝到 `tools/ascend_system_advisor` 下，所以安装后两种调用方式都可用（见 [README.md:199-208](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L199-L208)）：

```bash
python3 ${ASCEND_INSTALL_PATH}/tools/ascend_system_advisor/asys/asys.py -h
${ASCEND_INSTALL_PATH}/tools/ascend_system_advisor/asys/asys -h
```

#### 4.2.4 代码实践

1. **实践目标**：找到 `EXECUTE_CMD_FUNC` 并列出它支持的 8 个子命令。
2. **操作步骤**：
   ```bash
   # 在仓库根目录执行
   grep -n "EXECUTE_CMD_FUNC" src/asys/asys.py
   grep -n "_cmd$\|_cmd(" src/asys/common/const.py
   # 验证软链接
   ls -la src/asys/asys
   ```
3. **需要观察的现象**：`grep` 应定位到 `asys.py` 第 61 行的字典定义；`const.py` 中应看到 9 个 `xxx_cmd` 属性（其中 `help_cmd` 不在字典里）；`ls -la` 显示 `asys -> ./asys.py`。
4. **预期结果**：`EXECUTE_CMD_FUNC` 支持的 8 个子命令为 `collect`、`launch`、`info`、`diagnose`、`health`、`analyze`、`config`、`profiling`，分别映射到 `AsysCollect`、`AsysLaunch`、`AsysInfo`、`AsysDiagnose`、`AsysHealth`、`AsysAnalyze`、`AsysConfig`、`AsysProfiling`。也可对照 `cmd_parser.py` 的 `Command` 枚举（[src/asys/cmdline/cmd_parser.py:210-257](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/cmd_parser.py#L210-L257)）交叉验证：枚举恰好定义了这 8 个命令（COLLECT/LAUNCH/DIAGNOSE/HEALTH/INFO/ANALYZE/CONFIG/PROFILING）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `EXECUTE_CMD_FUNC` 里没有 `help`？

**答案**：`help` 不触发具体任务。`main()` 在解析不到子命令时（例如只输入 `asys` 或 `asys -h`）直接调用 `asys_parser.print_help()` 后返回，不走分发字典（见 [src/asys/asys.py:90-93](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L90-L93)）。

**练习 2**：安装后用户执行 `asys`（不带 `python3`）为什么能运行？

**答案**：`asys.py` 第一行是 `#!/usr/bin/env python3` shebang，内核据此选择解释器；而软链接 `asys -> ./asys.py` 被 CMake 的 `install(DIRECTORY)` 原样保留到安装目录，所以直接执行 `asys` 等价于执行 `asys.py`。

**练习 3**：`main()` 末尾 `create_out_timestamp_dir()` 和 `compress_output_dir_tar()` 分别解决什么问题？

**答案**：前者在执行任务前创建带时间戳的输出目录，避免多次运行的产物互相覆盖；后者在用户指定 `--tar=T/TRUE` 时把输出目录打成 tar 包，便于故障信息的归档与传输（见 [src/asys/asys.py:128-140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L128-L140)）。

### 4.3 msaicerr 入口：msaicerr.py 的三种模式

#### 4.3.1 概念说明

`src/msaicerr/msaicerr.py` 是 msaicerr 的唯一入口。与 asys 的"子命令"风格不同，msaicerr 采用"互斥模式参数"风格：三个开关 `-p`（解析 AI Core Error 报告目录）、`-d`（解析 Dump 文件）、`-e`（环境检查）决定走哪条处理路径。入口文件同样只负责参数校验与分发，实现在 `ms_interface/` 包中。

#### 4.3.2 核心流程

```text
python msaicerr.py [参数]
    │
    ├─ argparse 定义 -p / -d / -e / -out / -dev / -dtype
    │   （-out、-dev、-dtype 通过 RequireOtherArgs 约束必须与 -p/-d/-e 搭配）
    ├─ 检查 ASCEND_OPP_PATH 环境变量（未设置说明 CANN 未装好，直接退出）
    ├─ 检查当前目录可写（工具要落 debug_info.txt 日志）
    │
    └─ 按 args 优先级分发：
        ├─ args.data     → convert_dump_data()   → DumpDataParser 解析 Dump 文件
        ├─ args.report_path → analyse_report_path() → Collection 收集 + AicoreErrorParser 解析
        └─ args.env      → test_env()            → 运行内置样例算子检查环境
```

另外注意文件开头安装了全局异常钩子 `sys.excepthook = handle_exception`：任何未捕获异常都会把 `utils.GLOBAL_RESULT` 置为 `False`，保证进程退出码能如实反映"出错了"。这是命令行工具的一个实用技巧。

#### 4.3.3 源码精读

全局异常钩子：[src/msaicerr/msaicerr.py:41-50](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L41-L50)。这段代码定义 `handle_exception` 并挂到 `sys.excepthook`，使未捕获异常不至于被吞掉。

参数定义与搭配约束：[src/msaicerr/msaicerr.py:214-237](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L214-L237)。这段代码用 argparse 声明六个参数，其中 `-out`/`-dev`/`-dtype` 使用自定义 `RequireOtherArgs` action，强制它们只能与 `-p`/`-d`/`-e` 一起出现。

三种模式的分发逻辑：[src/msaicerr/msaicerr.py:255-260](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L255-L260)。这段代码按 `args.data` > `args.report_path` > `args.env` 的优先级把请求路由到 `convert_dump_data`、`analyse_report_path` 或 `test_env` 三个函数。

`-p` 模式的核心链路：[src/msaicerr/msaicerr.py:111-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L111-L117)。这段代码先由 `Collection` 收集报告目录信息，再交给 `AicoreErrorParser.parse()` 完成错误码解析——这两个类都实现在 `ms_interface/` 包里。

入口的安装释放：[src/msaicerr/CMakeLists.txt:38-40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/CMakeLists.txt#L38-L40)。msaicerr 整目录被释放到 `tools/msaicerr`，安装后入口位于 `${ASCEND_INSTALL_PATH}/tools/msaicerr/msaicerr.py`（用法见 [README.md:224-239](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L224-L239)）。

#### 4.3.4 代码实践

1. **实践目标**：验证入口的参数定义与实际 `-h` 输出一致。
2. **操作步骤**：
   ```bash
   # 只读参数帮助，不需要昇腾设备（-h 在环境变量检查之前由 argparse 处理不了，
   # 实际会先走 ASCEND_OPP_PATH 检查，所以更稳妥的是纯源码梳理）
   grep -n '"-p"\|"-d"\|"-e"\|"-out"\|"-dev"\|"-dtype"' src/msaicerr/msaicerr.py
   # 若本机已安装 CANN 并 source set_env.sh，可再执行：
   python3 src/msaicerr/msaicerr.py -h
   ```
3. **需要观察的现象**：grep 定位到 6 个参数的定义行；`-h` 输出的参数列表与源码一一对应。
4. **预期结果**：六个参数为 `-p/--report_path`、`-d/--data`、`-e/--env`、`-out/--output_path`、`-dev/--device_id`、`-dtype/--dest_dtype`。若本机无 CANN 环境导致 `-h` 无法运行，属正常现象，以源码梳理结果为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：同时传 `-p dir -d file` 会走哪条路径？

**答案**：走 `-d`（Dump 解析）。分发处 `if args.data:` 排在最前（[src/msaicerr/msaicerr.py:255-257](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L255-L257)），`-d` 优先级最高。

**练习 2**：`-dev 8` 在只有 4 张卡的机器上会发生什么？

**答案**：`analyse_report_path` 开头调用 `check_device_valid` → `verify_device_id`，通过 `DSMIInterface().get_device_count()` 取真实卡数，`device_id >= total_device_count` 即判定非法并返回参数错误码（见 [src/msaicerr/msaicerr.py:67-73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L67-L73) 与 [src/msaicerr/msaicerr.py:148-153](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L148-L153)）。

### 4.4 C++ 组件入口：hccl_test 与 msprof

#### 4.4.1 概念说明

两个 C++ 组件的"入口"概念与 Python 不同：它们没有 `main.py`，而是"构建入口 + main 函数"两层。

- **hccl_test**：构建入口是 `src/hccl_test/Makefile`。它是一个传统 Make 工程，把 `common/src/*.cc`（共享骨架）与 `opbase_test/` 下某一个算子测试文件编译成 `bin/<算子>_test` 可执行文件。`main()` 只有一份，在 `common/src/hccl_test_main.cc` 中，被所有测试二进制复用。
- **msprof**：C++ 采集侧（collector）的入口是 `src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp` 中的 `main()`。分析侧不在这个仓库里手写，而是以 `msprof` Python wheel 的形式在打包期拷入 `.run` 包，安装后释放到 `tools/profiler/profiler_tool/`（README 第 244-249 行有说明）。

#### 4.4.2 核心流程

hccl_test 的构建流程（Make 视角）：

```text
make（或 make HCCL_TEST_LOG_ENABLE）
    │
    ├─ CXXFLAGS：C++11 + 一组安全加固编译选项（-fstack-protector-strong、PIE 等）
    ├─ LIST 变量列出 11 个目标：all_gather_test、all_reduce_test、alltoall_test …
    ├─ 每个目标通过 `目标名: SRC = xxx_rootinfo_test.cc` 绑定自己的算子测试源文件
    │
    └─ 通用规则：g++ 编译 common/src/*.cc + opbase_test/$(SRC) → bin/$@（链接 -lhccl -lacl_rt -lmpi -lmsprofiler）
```

#### 4.4.3 源码精读

测试目标清单：[src/hccl_test/Makefile:37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37)。这行 `LIST` 变量枚举了 11 个要编译的集合通信测试目标（all_gather、all_gatherv、all_reduce、alltoallv、alltoall、broadcast、reduce_scatter、reduce_scatterv、reduce、scatter、alltoallvc）。

目标到源文件的绑定与编译规则：[src/hccl_test/Makefile:56-82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L56-L82)。这段代码先把每个目标名绑定到 `opbase_test/` 下对应的 `.cc` 文件，再用一条通用模式规则用 g++ 把公共源码加该文件编译链接为 `bin/` 下的可执行文件。

共享 main 函数：[src/hccl_test/common/src/hccl_test_main.cc:26](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L26)。所有 11 个测试二进制的 `main()` 都来自这一个文件，算子差异由链接进来的不同测试类体现。

msprof collector 入口：[src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp:89](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L89)。这是 `int main(int argc, const char **argv, const char **envp)` 所在行，即 msprof C++ 采集侧的进程入口（深入留待 u4 单元）。

安装后布局对照（msprof 分析侧）：[README.md:244-249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L244-L249)。wheel 在构建期被拷到 `msprofbin/` 并打入 `.run` 包，安装时自动解包到 `tools/profiler/profiler_tool/`，用户无需手动 `pip install`。

#### 4.4.4 代码实践

1. **实践目标**：梳理"四个组件入口文件"的完整清单。
2. **操作步骤**：
   ```bash
   grep -n "int main" src/hccl_test/common/src/hccl_test_main.cc
   grep -n "int main" src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp
   head -1 src/asys/asys.py src/msaicerr/msaicerr.py
   grep -n "^LIST" src/hccl_test/Makefile
   ```
3. **需要观察的现象**：两个 C++ 文件各有唯一的 `int main`；两个 Python 文件首行均为 shebang；Makefile 的 LIST 有 11 个目标。
4. **预期结果**：入口清单为
   - asys：`src/asys/asys.py`（软链接 `src/asys/asys`），安装后 `tools/ascend_system_advisor/asys/asys.py`
   - msaicerr：`src/msaicerr/msaicerr.py`，安装后 `tools/msaicerr/msaicerr.py`
   - msprof（C++ 采集侧）：`src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp` 的 `main()`；分析侧入口安装后为 `tools/profiler/profiler_tool/analysis/msprof/msprof.py`
   - hccl_test：构建入口 `src/hccl_test/Makefile`，运行入口是它产出的 `bin/<算子>_test`（共享 `hccl_test_main.cc` 的 main）

#### 4.4.5 小练习与答案

**练习 1**：为什么 hccl_test 的 11 个测试程序只写一份 `main()`？

**答案**：所有集合通信测试的流程骨架相同（初始化 HCCL 通信域、准备缓冲区、执行算子、校验/统计）。把骨架放在 `common/`（含共享 main），每个算子只实现差异部分，避免 11 份重复代码——这是典型的模板方法思想，u5 单元会展开。

**练习 2**：msprof 在这个仓库里找不到 Python 分析脚本的源码，是缺失吗？

**答案**：不是。分析侧以独立维护的 `msprof` wheel 形式在构建打包期引入（拷贝到 `msprofbin/` 再打入 `.run` 包），本仓库只包含 C++ collector 与 `inc/` 头文件，这是"采集与分析解耦"的架构选择。

## 5. 综合实践

**任务：绘制"仓库 → 安装包 → 可执行入口"的全链路地图。**

1. 在仓库根目录用 `ls`/`grep` 完成 4.1.4、4.2.4、4.3.4、4.4.4 四个小实践，收集事实。
2. 画一张三列对照表（纸面或 Markdown 均可）：

   | 组件 | 仓库内入口 | 安装后入口（CANN `tools/` 下） |
   | --- | --- | --- |
   | asys | `src/asys/asys.py` + 软链接 `asys` | `tools/ascend_system_advisor/asys/asys(.py)` |
   | msaicerr | `src/msaicerr/msaicerr.py` | `tools/msaicerr/msaicerr.py` |
   | msprof | `.../msprofbin/src/msprof_bin.cpp`（C++）+ 外部 wheel | `tools/profiler/profiler_tool/` |
   | hccl_test | `src/hccl_test/Makefile` → `bin/*_test` | 待确认（以实际安装产物为准） |

3. 在表中为每个组件补一列"入口分发方式"：asys 是字典分发（`EXECUTE_CMD_FUNC`，8 个子命令），msaicerr 是三模式 if 分发（`-p`/`-d`/`-e`），hccl_test 与 msprof 是直接 `main()`。
4. 最后回答一个问题：如果 `src/asys/asys` 软链接意外丢失，安装后的哪种调用方式会失效？（答案：不带 `python3` 的 `asys` 直接调用方式失效，`python3 .../asys.py -h` 仍可用。）

## 6. 本讲小结

- 仓库源码集中在 `src/` 下六个目录：asys、msaicerr、msprof、hccl_test 四大组件加 operator_cmp、third_party，目录职责与 README 声明一一对应。
- asys 入口 `asys.py` 的 `main()` 是五步守门流程（查重、解析、环境检查、配置与输出目录、分发执行），业务在各子命令目录中。
- `EXECUTE_CMD_FUNC` 字典把 8 个子命令（collect/launch/info/diagnose/health/analyze/config/profiling）映射到实现类，是典型的字典分发模式；命令名常量定义在 `common/const.py`。
- msaicerr 入口 `msaicerr.py` 用 `-p`/`-d`/`-e` 三个互斥模式参数路由到报告解析、Dump 解析、环境检查三条路径，并用 `sys.excepthook` 保证异常时退出码正确。
- C++ 组件的入口是"构建入口 + main 函数"：hccl_test 由 Makefile 编出 11 个测试二进制共享一份 `main()`；msprof 的 C++ 入口在 `msprof_bin.cpp`，分析侧来自打包期引入的 Python wheel。
- CMake 的 `install(DIRECTORY)` 规则决定了安装布局：asys 释放到 `tools/ascend_system_advisor`，msaicerr 释放到 `tools/msaicerr`；软链接 `asys -> ./asys.py` 因整目录拷贝而被保留。

## 7. 下一步学习建议

下一讲（u1-l4）将把地图"跑起来"：完成 CANN 环境准备、`source set_env.sh`、`.run` 包安装验证（`build.sh -u`），并按 `examples/` 脚本验证 asys 与 msaicerr 的基本功能。之后再进入 u2 单元，从 `asys.py` 出发逐个精读子命令实现。建议提前熟读本讲的 `EXECUTE_CMD_FUNC` 与 `Command` 枚举两处源码，它们是 u2 全部讲义的导航起点。
