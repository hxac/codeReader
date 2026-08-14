# u2-l7 asys analyze：AI Core Error 与 coredump 分析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `asys analyze` 支持的全部 `run_mode`，并理解它用一个字典完成模式分发的 设计。
2. 讲清 `aicore_error` 模式下 asys 与 msaicerr 两个工具的分工：asys 负责"采集现场 + 拼命令"，msaicerr 负责"深度解析"。
3. 跟踪 `coredump` 模式的完整流程：从 `__core_dump_analyze()` 到 `CoreDump.start_gdb()` 驱动 gdb、逐行解析输出、生成 stackcore 格式的 `.txt` 文件。
4. 理解 `--reg`（寄存器级别）和 `--symbol`（解析模式）两个参数如何影响 coredump 的输出。

## 2. 前置知识

- **run_mode（-r 参数）**：asys 的 `analyze` 子命令是一个"多模式解析器"，`-r` 决定本次要解析什么。本讲重点关注 `aicore_error` 与 `coredump` 两个模式，`trace`/`stackcore`/`coretrace`/`ub` 只做概览。
- **AI Core Error**：昇腾 AI Core（矩阵/向量计算单元）运行时发生的硬件级错误，业务日志中通常表现为 "there is an aicore error exception"。定位它需要解析 plog 日志和 dump 文件。
- **coredump 与 gdb**：进程异常退出（如 Segmentation fault）时，操作系统可生成 core 文件（进程内存映像）。gdb 是 GNU 调试器，用 `gdb <可执行文件> <core文件>` 可以查看崩溃时的堆栈、寄存器和内存映射。
- **stackcore 格式**：asys 内部统一的一种堆栈文本格式，包含 `[process]`（崩溃原因/pid/tid）、`[stack]`（各线程堆栈）和 `[maps]`（内存映射区间）三段，后续可被 `asys analyze -r=stackcore` 进一步符号化。
- **ParamDict**：asys 的全局参数单例（见 u2-l2），`AsysAnalyze` 构造时从它读取所有命令行参数。
- **工具间协作**：asys 安装后与 msaicerr 同处 CANN 的 `tools/` 目录下，两者通过"子进程调用 + 文件目录交接"配合，这是本讲最重要的架构看点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/asys/analyze/asys_analyze.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py) | `AsysAnalyze` 类：run_mode 分发、AI Core Error 模式（转调 msaicerr）、coredump 模式入口、UB 二进制转文本 |
| [src/asys/analyze/coredump_analyze.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py) | `CoreDump` 类：驱动 gdb 解析 core 文件，产出 stackcore 格式堆栈文本 |
| [src/asys/common/const.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py) | `REG_OFF/REG_THREAD/REG_STACK` 寄存器级别常量、`GDB_LAYER_MAX` 堆栈深度上限 |
| [src/asys/params/param_dict.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py) | `tools_path` 属性——定位 asys 自身安装目录，进而推导 msaicerr 的位置 |
| [docs/zh/asys/AI_Core_error_analysis.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/AI_Core_error_analysis.md) | AI Core Error 解析的用户文档（命令格式、参数说明） |
| [docs/zh/asys/coredump_files_parsing.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/coredump_files_parsing.md) | coredump 解析的用户文档（`--reg`/`--symbol` 参数、输出示例） |

## 4. 核心概念与源码讲解

### 4.1 AsysAnalyze 的 run_mode 分发设计

#### 4.1.1 概念说明

`asys analyze` 要处理六种完全不同的输入：atrace 文件、stackcore 文件、coretrace 文件、core 文件、AI Core Error 故障目录、UB 统计二进制。asys 没有为它们写六个子命令，而是用一个 `-r/--run_mode` 参数 + 一个"模式 → 方法"的字典完成分发。这与入口 `asys.py` 用 `EXECUTE_CMD_FUNC` 字典分发 8 个子命令（见 u2-l1）是同一套设计哲学：**字典即路由表，新增模式只需加一行**。

#### 4.1.2 核心流程

```text
asys analyze -r=<mode> ...
    │
    ├─ AsysAnalyze.__init__()   # 从 ParamDict 读取 file/path/exe_file/core_file/symbol/...
    │
    └─ run()
        ├─ 防循环拷贝检查：path 与 output 目录重叠则报错退出
        └─ mode_function 字典按 run_mode 取方法并调用
            ├─ trace / stackcore / coretrace → __atrace_analyze()
            ├─ coredump                      → __core_dump_analyze()
            ├─ aicore_error                  → __aicore_error_analyze()
            └─ ub                            → __ub_analyze()
```

注意 `trace`/`stackcore`/`coretrace` 三个模式共用 `__atrace_analyze()`：它们都是"文件/目录 → 复制到输出目录 → 交给对应 Parse 类"的解析型流程（这些 Parse 类在 u2-l5 collect 子系统中已讲过，本讲不再展开）。

#### 4.1.3 源码精读

构造函数把所有参数从 ParamDict 拉到实例属性，`output` 直接取入口创建好的时间戳输出目录（见 u2-l1 的 `create_out_timestamp_dir()`）：

[src/asys/analyze/asys_analyze.py:42-52](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L42-L52) — `AsysAnalyze.__init__()` 读取 `file`/`path`/`exe_file`/`core_file`/`symbol`/`symbol_path`/`run_mode` 等参数，`get_param_arg()` 把"未传的可选参数"统一归一为 `None`（ParamDict 中可选参数以 `False` 占位的约定，见 u2-l6）。

[src/asys/analyze/asys_analyze.py:334-349](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L334-L349) — `run()` 方法：先用 `os.path.relpath(...).endswith("..")` 判断 `--path` 是否与输出目录重叠（防止把输出拷进输入造成循环拷贝，对应 AI_Core_error_analysis.md 的"注意事项 2"），再用 `mode_function` 字典完成六种模式到四个私有方法的映射。

顺带一提 `ub` 模式（[asys_analyze.py:440-455](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L440-L455)）：它遍历模块顶部的 `ub_file_names` 列表，用 `getattr(self, "_convert_ub_" + 文件名)` 动态找到对应的 struct 解包函数，把六种 UB 统计 `.bin` 文件转成可读 `.txt`——这是"文件名 → 方法名"的第二层字典式分发。

#### 4.1.4 代码实践

1. **实践目标**：验证 run_mode 与处理函数的映射关系。
2. **操作步骤**：
   - 阅读 [asys_analyze.py:340-347](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L340-L347) 的 `mode_function` 字典；
   - 在已安装 asys 的环境执行 `asys analyze -h`（或直接读 u2-l2 讲过的 `cmd_parser.py` 中 analyze 子命令的 Arg 定义），对比帮助信息里的 `-r` 取值列表。
3. **需要观察的现象**：帮助信息中 `-r` 的 choices 与字典的六个键是否一一对应（`trace`/`stackcore`/`coretrace`/`coredump`/`aicore_error`/`ub`）。
4. **预期结果**：完全一致；若不一致，说明 choices 定义与分发字典出现了不同步（可作为贡献点）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `trace`/`stackcore`/`coretrace` 三个模式可以共用一个 `__atrace_analyze()`？

**答案**：因为三者的处理骨架相同——都是"把 `--file` 或 `--path` 指定的源文件复制到输出目录，再交给对应的解析类（`ParseTrace`/`ParseStackCore`/`ParseCoreTrace`）"。差异只在构造哪个解析类，`__atrace_analyze()` 内部已按 `run_mode` 分支选择，所以入口层可以合并为一个方法。

**练习 2**：如果要新增一个 `run_mode`（例如解析一种新的日志格式），至少要改 `AsysAnalyze` 的哪几处？

**答案**：新增一个私有方法（如 `__xxx_analyze()`），并在 `run()` 的 `mode_function` 字典中加一行映射；此外还需在 `cmd_parser.py` 的 Arg/Command 定义中扩展 `-r` 的 choices（u2-l2 讲过）。`AsysAnalyze` 类本身的中心逻辑不用动。

---

### 4.2 AI Core Error 解析：asys 与 msaicerr 的分工

#### 4.2.1 概念说明

AI Core Error 的完整定位需要两步：先把故障现场的日志和 dump 文件收集齐，再对它们做深度解析。oam-tools 把这两步拆给了两个工具：

- **asys** 擅长"采集"：复用 u2-l5 讲过的 `AsysCollect` 采集子系统；
- **msaicerr** 擅长"解析"：其 `-p` 模式专门解析 AI Core Error 报告目录（见 u3 单元）。

`__aicore_error_analyze()` 就是这两者的粘合剂。用户文档 [AI_Core_error_analysis.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/AI_Core_error_analysis.md) 中"`--path` 不配置时 asys 工具会自动收集这些故障信息"这句话，对应的正是源码里"无 path 则先跑一次 AsysCollect"的分支。

#### 4.2.2 核心流程

```text
asys analyze -r=aicore_error [--path=P] -d=dev
    │
    ├─ 定位 msaicerr：<asys安装目录>/../msaicerr/msaicerr.py（找不到 → 报错退出）
    │
    ├─ if --path 已给：
    │      直接用它作为待解析目录
    ├─ else：
    │      AsysCollect().run() 现场采集 → 采集输出目录作为待解析目录
    │
    ├─ 拼命令：python <msaicerr.py> -p <待解析目录> -dev <device_id> -out <输出目录父目录>
    └─ real_time_output(cmd) 实时透传 msaicerr 的输出，随后清理 asys 自己的空输出目录
```

关键点：asys 与 msaicerr 之间**没有函数调用或 import**，而是"子进程 + 目录交接"——这保证两个工具完全解耦，msaicerr 可以单独发布、单独使用。

#### 4.2.3 源码精读

[src/asys/analyze/asys_analyze.py:416-438](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L416-L438) — `__aicore_error_analyze()` 全貌：定位 msaicerr 路径、按 `--path` 有无走"直接解析 / 先采集再解析"两条支路、拼出 msaicerr 命令行并用 `real_time_output` 执行。

msaicerr 的路径不是硬编码的，而是从 asys 自己的安装位置推导：

[src/asys/analyze/asys_analyze.py:417-422](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L417-L422) — `ParamDict().tools_path.parents[1].joinpath("msaicerr", "msaicerr.py")`：`tools_path` 是 `src/asys/params/` 的上两级（即 asys 安装根目录），`.parents[1]` 再上一层到 `tools/`，然后拼上 `msaicerr/msaicerr.py`。对应 `tools_path` 定义见 [src/asys/params/param_dict.py:41-43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/params/param_dict.py#L41-L43)。这解释了报错信息为什么是 "please install the whole package"——只拷 asys 不装 msaicerr 时该文件不存在。

[src/asys/analyze/asys_analyze.py:426-434](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L426-L434) — 两条支路拼出的命令：`{sys.executable} {msaicerr_path} -p <目录> -dev <device_id> -out <output_path>`。无 `--path` 时先 `AsysCollect().run()` 现场采集（失败则整体失败），再用采集输出目录 `output_root_path` 作为 `-p` 入参——这就是文档说"自动收集会受环境变量影响，执行 asys 命令时环境变量值需与业务运行时一致"的原因：采集位置由 `ASCEND_PROCESS_LOG_PATH`、`DUMP_GRAPH_PATH` 等环境变量决定。

[src/asys/analyze/asys_analyze.py:436-438](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L436-L438) — `real_time_output(cmd)`（u2-l3 讲过的实时透传执行方式）让 msaicerr 的解析进度直接打印到终端；结束后 `clean_output()` 删掉 asys 入口创建的时间戳目录——因为真正的产物由 msaicerr 写到 `-out` 指定的父目录，asys 自己的目录此时是空的。

#### 4.2.4 代码实践

1. **实践目标**：把用户文档的描述映射到真实函数调用链（本讲综合实践的前半部分）。
2. **操作步骤**：
   - 通读 [AI_Core_error_analysis.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/AI_Core_error_analysis.md)；
   - 在 `src/asys/analyze/asys_analyze.py` 中依次定位：`run()` 的字典分发 → `__aicore_error_analyze()` → `AsysCollect().run()` → `real_time_output(cmd)`；
   - 追一步 msaicerr 侧：在 `src/msaicerr/msaicerr.py` 中 grep `-p` 参数的处理，确认它把目录交给哪个类（u3-l1 会精读）。
3. **需要观察的现象**：文档中每句话（自动收集、环境变量影响、output 目录约束）都能在源码中找到一个对应代码点。
4. **预期结果**：得到一条"文档语句 → 源码行号"的对照清单。若本机装了 CANN 且有昇腾环境，可实际执行 `asys analyze -r=aicore_error -d 0` 观察终端透传的 msaicerr 输出；无环境则为源码阅读型实践，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`__aicore_error_analyze()` 里 `output_path = os.path.dirname(self.output)`，为什么取输出目录的**父目录**而不是输出目录本身？

**答案**：msaicerr 的 `-out` 语义是"结果输出目录"，msaicerr 会自己在其下建带时间戳的子目录；而 `self.output` 是 asys 入口已建好的时间戳目录。若直接传入会造成双层时间戳嵌套，且该目录随后被 `clean_output()` 删除。取父目录让 msaicerr 在干净位置自建产物目录。

**练习 2**：为什么 asys 不直接 import msaicerr 的解析函数，而要走子进程？

**答案**：两个工具安装位置相邻但彼此独立发布、独立演进；子进程 + 命令行参数 + 目录交接的协作方式让二者零代码依赖，任一工具单独存在时另一个只是报"找不到路径"的错，不会 import 失败导致崩溃。这也与 u2-l6 中 diagnose 转调 msaicerr 环境检查的方式一致。

---

### 4.3 coredump 解析主流程：CoreDump 如何驱动 gdb

#### 4.3.1 概念说明

`asys analyze -r=coredump` 解决的问题是：进程崩溃产生的 core 文件是二进制内存映像，人无法直接阅读。`CoreDump` 类的做法不是自己解析 ELF core 格式，而是**把 gdb 当作解析引擎**：启动一个 gdb 子进程，向它的 stdin 写 gdb 命令，读取 stdout 的文本输出，再用正则逐行提炼出崩溃信息、各线程堆栈和内存映射，最终拼成 stackcore 格式的 `.txt` 文件。这与 u2-l3 的"外部命令执行"设施、u2-l5 的"解析型采集子模块"一脉相承。

#### 4.3.2 核心流程

```text
asys analyze -r=coredump --exe_file=E --core_file=C [--reg=N --symbol=M]
    │
    ├─ __core_dump_analyze()：
    │    ├─ 前置检查：gdb 已安装？exe_file/core_file 已传？
    │    ├─ CoreDump(E, C, symbol, output).start_gdb("[process]\n")
    │    └─ 把返回的 stack_txt 写入 stackcore_<exe名>_<pid>_<毫秒时间戳>.txt
    │
    └─ CoreDump.start_gdb()：
         ├─ Popen(["gdb", exe_file, core_file])
         ├─ 依次写入 5 条 gdb 命令：
         │    info inferiors / info sharedlibrary /
         │    thread apply all bt 32 / info proc mappings / quit
         ├─ communicate() 拿全部输出，逐行解析：
         │    "Program terminated..." → 崩溃原因
         │    "* N process ..."     → pid
         │    "Thread N (LWP ...)"  → 线程名（切换当前线程上下文）
         │    "#N 0xADDR ..."       → 堆栈行 → bt_info[thread]
         │    "Start Addr ... objfile"/十六进制区间行 → map_info
         ├─ （按 --reg 级别）再次驱动 gdb 收集寄存器
         └─ _process_stack_txt() 拼装 [process]/[stack]/[maps] 三段文本
```

#### 4.3.3 源码精读

入口侧的三道前置检查：

[src/asys/analyze/asys_analyze.py:397-414](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L397-L414) — `__core_dump_analyze()`：先用 `check_command("gdb")` 确认 gdb 存在（对应 coredump_files_parsing.md "依赖 gdb，需提前安装"的注意事项），再强制要求 `--exe_file` 与 `--core_file` 成对出现；`start_gdb()` 返回 `(stack_txt, pid)`，`pid == 0` 表示解析失败；成功则以 `stackcore_<exe名>_<pid>_<毫秒时间戳>.txt` 命名落盘。

驱动 gdb 的命令序列：

[src/asys/analyze/coredump_analyze.py:262-276](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L262-L276) — `start_gdb()` 用 `subprocess.Popen(["gdb", exe_file, core_file])`（命令由 [coredump_analyze.py:109-111](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L109-L111) 的 `_get_gdb_cmd()` 给出），依次写入 `info inferiors`（拿 pid）、`info sharedlibrary`、`thread apply all bt 32`（所有线程的 32 层堆栈，32 来自 [const.py:63](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L63) 的 `GDB_LAYER_MAX`）、`info proc mappings`（内存映射），最后 `quit` + `y` 退出，`communicate()` 一次性取回输出。

逐行解析的"状态机"：

[src/asys/analyze/coredump_analyze.py:282-308](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L282-L308) — for 循环遍历 gdb 输出行：`Program terminated` 提取崩溃信号；`Current thread` 提取崩溃 tid；以 `*` 开头且含 `process` 的行提取 pid；`Thread N (LWP x)` 行切换 `thread_name` 上下文——之后的 `#N` 堆栈行都会归到该线程名下；其余行交给 `collect_info()`。另有两个防御分支：exe 文件不存在直接返回，"core file may not match specified executable file" 只告警不中断（对应文档"需保证 exe_file 与 core_file 匹配，否则解析结果错误"）。

堆栈行与映射行的分拣：

[src/asys/analyze/coredump_analyze.py:113-134](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L113-L134) — `collect_info()` 是行分类器：`#数字` 开头 → 追加进 `bt_info[thread]`；`Start ... objfile` 表头或合法映射行（`check_map_line` 校验前三列均为十六进制）→ 追加进 `map_str` 并把连续同名 objfile 的区间**合并**进 `map_info`（起址取最小、止址取最大）——合并是为了后续 `view_map()` 二分匹配更快。

最终拼装：

[src/asys/analyze/coredump_analyze.py:312-331](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L312-L331) — `_process_stack_txt()`：先写 `[process]` 段（crash reason/pid/tid），再按线程写 `[stack]` 段（每线程堆栈经 `parse_stackcore()` 加工），最后附上 `[maps]` 段——这正是 [coredump_files_parsing.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/coredump_files_parsing.md) 中输出示例的三段结构。

#### 4.3.4 代码实践

1. **实践目标**：不依赖昇腾设备，亲手制造一个 core 文件并理解 gdb 交互。
2. **操作步骤**（任意 Linux 环境）：
   - `ulimit -c unlimited`，编译一个会崩溃的小程序（示例代码，非项目代码）：
     ```c
     // crash.c —— 示例代码
     #include <string.h>
     int main() { char *p = 0; memcpy(p, "x", 1); return 0; }
     ```
     `gcc -g crash.c -o crash && ./crash`，在当前目录得到 `core*` 文件（若无，检查 `/proc/sys/kernel/core_pattern`）；
   - 手动执行 `gdb ./crash core -batch -ex "info inferiors" -ex "thread apply all bt 32" -ex "info proc mappings"`，对照 [coredump_analyze.py:270-273](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L270-L273) 观察同样的四条命令输出什么文本。
3. **需要观察的现象**：输出中能找到 `Program terminated with signal ...`（SIGSEGV）、`Thread 1 (LWP ...)`、`#0 0x... in ...` 堆栈行、`Start Addr ... objfile` 映射表头。
4. **预期结果**：这些正是 `start_gdb()` 逐行解析的目标行——你会直观理解"asys 只是 gdb 输出的逐行分拣器"。若系统限制无法生成 core 文件，改为直接阅读本节源码完成对照，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`start_gdb()` 里 `stderr=subprocess.STDOUT`，为什么把错误输出混进标准输出？

**答案**：gdb 的很多告警（如符号文件缺失、"No such file or directory"）走 stderr。合并进 stdout 后，逐行解析循环才能用 `if f"{self.exe_file}: No such file or directory" in line` 这类判断捕获致命错误并提前返回，`No ...` 开头的行也能触发"共享库符号缺失"告警。

**练习 2**：`bt_info` 和 `map_info` 为什么一个用 dict、一个用 list？

**答案**：`bt_info` 以线程名为 key、堆栈行列表为 value，天然是"分组"结构，dict 支持按线程 O(1) 存取；`map_info` 是有序的地址区间序列，`view_map()` 需要线性扫描区间做地址归属判断，且写入时要与"上一行"合并区间，list 的顺序性正合适。

---

### 4.4 coredump 的堆栈加工：--symbol 与 --reg 两个旋钮

#### 4.4.1 概念说明

gdb 原始的 backtrace 输出对故障定位并不总是友好：地址行没有归属库信息、`in ??` 行无符号。`CoreDump` 提供两个参数控制加工方式：

- **`--symbol`（解析模式，0/1，默认 0）**：0 = 把带地址的行解析成"地址 + 归属动态库"的 stackcore 格式，其余标 `Ignore`；1 = 只对 `in ??` 行做上述解析，其余保留 gdb 原文（含函数名、源码位置）。
- **`--reg`（寄存器级别，0/1/2，默认 0）**：0 = 不附加寄存器；1 = 每线程一条；2 = 每层栈帧一条（文档明确"占用 Host 资源较多，比较耗时"）。

#### 4.4.2 核心流程

```text
parse_stackcore(bt_lines)                      get_threads_stacks_reg_info()
  ├─ symbol=1 且行不含 "in ??"：  --reg=0 → {}        （不加）
  │     原样保留 gdb 行            --reg=1 → 单 gdb 会话逐线程
  ├─ 地址非法 → "Ignore"          --reg=2 → 进程池并行为
  └─ 其余 → view_map()：                每线程每栈帧发 gdb 命令
        用堆栈地址在 map_info
        区间内定位归属 objfile，
        输出 "#NN addr 起址 库名"
寄存器名随架构自适应：
  x86_64 → rbp/rsp/rip；aarch64 → x29/sp/pc
```

#### 4.4.3 源码精读

寄存器级别的三分支：

[src/asys/analyze/coredump_analyze.py:252-260](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L252-L260) — `get_threads_stacks_reg_info()` 按 `self.reg`（构造时从 ParamDict 的 `reg` 参数取值）分发：`REG_OFF/REG_THREAD/REG_STACK` 三个常量定义在 [const.py:45-47](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/const.py#L45-L47)。

级别 2 的并行实现：

[src/asys/analyze/coredump_analyze.py:202-215](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L202-L215) — `_get_reg_info_level_stack()` 用 `multiprocessing.Pool(cpu_count() - 1)` 为每个线程异步提交 `thread_stacks_reg_info()`（[coredump_analyze.py:41-84](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L41-L84)），每个子进程独立开一个 gdb 会话，逐栈帧发 `frame N` + `info reg`，结果经 `Manager().Queue()` 汇回——这是"耗时"的原因：gdb 会话数 = 线程数。

架构自适应：

[src/asys/analyze/coredump_analyze.py:30-38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L30-L38) — `_get_reg_info_cmd()` 用 `platform.machine()` 区分：x86_64 查 `rbp rsp rip`，aarch64 查 `x29 sp pc`（帧指针/栈指针/程序计数器的不同叫法），未知架构返回空串直接跳过寄存器收集。

地址归属与输出格式：

[src/asys/analyze/coredump_analyze.py:136-157](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L136-L157) — `view_map()` 把 backtrace 行中的地址与 `map_info` 各区间比较，命中则输出 `#NN 0x地址 0x区间起址 objfile路径`，并调用 `_stack_add_reg()` 追加该栈帧的寄存器行；未命中且 `symbol=1` 时保留原文。

[src/asys/analyze/coredump_analyze.py:183-200](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L183-L200) — `parse_stackcore()` 的三路分拣：`symbol=1` 且非 `in ??` 行原样保留；地址非法的行输出 `Ignore`（对应文档"地址不存在或栈溢出可能导致无法解析"）；其余交给 `view_map()`。开头的 `#0 -> #00` 补零是为了让 `#2`、`#10` 等栈帧号对齐排版。

#### 4.4.4 代码实践

1. **实践目标**：对比 `--symbol` 两种取值与 `--reg` 三种取值的输出差异。
2. **操作步骤**：在 4.3.4 实践得到的 crash/core 文件基础上（或已安装环境）依次执行：
   ```bash
   asys analyze -r=coredump --exe_file=./crash --core_file=./core --symbol=0 --reg=0
   asys analyze -r=coredump --exe_file=./crash --core_file=./core --symbol=1 --reg=2
   ```
   打开输出目录中的 `stackcore_crash_<pid>_<时间戳>.txt` 对比。
3. **需要观察的现象**：第一次输出中无符号行显示 `Ignore`、无寄存器；第二次保留 gdb 原文（函数名/文件行号），且每层栈帧后多出 `fp = ... sp = ...` / `pc = ...` 行（aarch64 命名）。
4. **预期结果**：与 [coredump_files_parsing.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/coredump_files_parsing.md) 中两个输出示例分别对应。无运行环境时，可对照该文档的两个示例代码块逐行指出差异来源（`parse_stackcore()` 的哪个分支产生了这一行）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `--reg=2` 要开多个 gdb 进程并行，而 `--reg=1` 只用一个？

**答案**：级别 1 每线程只需一组寄存器，单个 gdb 会话内顺序执行 `thread N` + `info reg` 即可（[coredump_analyze.py:217-250](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/coredump_analyze.py#L217-L250)）；级别 2 要对每线程×每栈帧发 `frame N` + `info reg`，串行会话内切换帧的开销随线程数平方增长，且单会话 stdin/stdout 是顺序流无法并行，因此按线程拆成多个 gdb 子进程用 `Pool` 并行，用 `Manager().Queue()` 汇聚结果。

**练习 2**：`--symbol=0` 时输出中 `Ignore` 行的含义是什么，为什么不当错误处理？

**答案**：`Ignore` 表示该 backtrace 行的地址列不是合法十六进制（如纯符号行或栈被破坏的行），无法在 `map_info` 中定位归属库，故跳过解析但保留行号占位，维持堆栈帧编号的完整性。这符合 asys "失败占位、不中断"的整体防御式风格（u2-l3）。

---

## 5. 综合实践

**任务：写出 asys analyze 解析 AI Core Error 的完整函数链说明（约 200 字）。**

这是本讲规格中指定的实践。步骤：

1. 通读用户文档 [docs/zh/asys/AI_Core_error_analysis.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/AI_Core_error_analysis.md)，记下用户视角的三步：指定 `-r=aicore_error`、可选 `--path`、读终端提示的 `info.txt`。
2. 在源码中按顺序定位并标注这条链：
   - `asys.py` 的 `EXECUTE_CMD_FUNC` 把 `analyze` 映射到 `AsysAnalyze`（u2-l1 已讲）；
   - [asys_analyze.py:334-349](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L334-L349) `run()` 字典分发到 `__aicore_error_analyze`；
   - [asys_analyze.py:417-422](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L417-L422) 经 `tools_path` 推导 msaicerr 路径；
   - [asys_analyze.py:426-434](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L426-L434) 无 `--path` 时 `AsysCollect().run()` 现场采集（u2-l5 的采集子系统）；
   - [asys_analyze.py:436-438](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/analyze/asys_analyze.py#L436-L438) `real_time_output` 子进程执行 `msaicerr.py -p ... -dev ... -out ...`；
   - msaicerr 侧入口 `src/msaicerr/msaicerr.py` 的 `-p` 分支（预告 u3-l1/l2）。
3. 参考答案（约 200 字）：

   > 用户执行 `asys analyze -r=aicore_error [--path=目录] -d=设备号` 后，`asys.py` 经 `EXECUTE_CMD_FUNC` 分发到 `AsysAnalyze.run()`，后者按 run_mode 字典进入 `__aicore_error_analyze()`。该函数先由 `ParamDict().tools_path.parents[1]` 定位同级安装的 `msaicerr/msaicerr.py`，找不到即报错退出；若用户给了 `--path` 直接采用，否则先调用 `AsysCollect().run()` 按当前环境变量现场采集日志与 dump 文件，取其输出目录作为待解析目录。随后拼出 `python msaicerr.py -p <目录> -dev <id> -out <输出父目录>` 命令，用 `real_time_output` 实时透传执行，msaicerr 解析报告目录中的 plog 与 dump 文件，生成含定位提示的 `info.txt`，最后 asys 清理自己的空时间戳目录。整个过程 asys 只做"路由 + 采集 + 拼命令"，深度解析完全由 msaicerr 完成。

## 6. 本讲小结

- `AsysAnalyze.run()` 用 `mode_function` 字典把 6 种 `run_mode` 分发到 4 个私有方法，与入口层的 `EXECUTE_CMD_FUNC` 是同一套"字典即路由表"设计。
- `aicore_error` 模式下 asys 与 msaicerr 通过"子进程 + 目录交接"协作：asys 定位同级安装的 msaicerr、必要时先用 `AsysCollect` 现场采集，再拼 `msaicerr.py -p ... -dev ... -out ...` 命令实时透传执行——采集与解析两个工具完全解耦。
- `coredump` 模式的核心是把 gdb 当解析引擎：`start_gdb()` 向 gdb 子进程写 5 条命令，再对输出做逐行状态机式分拣，收进 `bt_info`（按线程的堆栈）与 `map_info`（合并后的地址区间）。
- `--symbol` 决定堆栈行的加工方式（0=地址归属库+Ignore；1=只解析 `in ??` 行），`--reg` 决定寄存器附加级别（0/1/2，级别 2 用 multiprocessing Pool 并行开多个 gdb 会话，故最耗时）。
- 最终产物是 stackcore 格式的 `stackcore_<exe>_<pid>_<时间戳>.txt`（`[process]/[stack]/[maps]` 三段），可再经 `asys analyze -r=stackcore` 做符号化，形成解析闭环。
- 解析全程遵守 asys 的防御式风格：gdb 缺失、exe/core 不匹配、地址非法等失败都以告警或 `Ignore` 占位，尽量产出可用的部分结果。

## 7. 下一步学习建议

- 下一讲 u2-l8 将讲 asys 的配置机制（`config_parser.py`）与 `profiling` 子命令，补齐 asys 的最后一块拼图。
- 本讲多次"预告"了 msaicerr：建议直接进入 u3-l1（msaicerr 入口与 `-p/-d/-e` 三模式）和 u3-l2（`AicoreErrorParser` 报告解析），把本讲 4.2 节的协作链在 msaicerr 侧走完。
- 想深入 coredump 解析的读者，可继续阅读 `collect/stackcore` 与 `collect/coretrace` 的 Parse 类源码（u2-l5 已概述），理解 stackcore 文件的"下游"符号化流程。
