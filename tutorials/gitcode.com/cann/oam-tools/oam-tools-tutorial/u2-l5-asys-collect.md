# u2-l5 asys collect 子系统：故障信息采集框架

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `src/asys/collect/asys_collect.py` 的总调度逻辑：它按什么顺序调用哪些采集函数、EP 环境与 RC 环境走哪条分支。
2. 理解 trace、日志、stacktrace、coretrace 等采集子模块在目录结构、入口函数形态、线程模型上的共性。
3. 掌握「新增一个采集项」的扩展点：新目录 + `collect_xxx(output_root_path)` 入口函数 + 在 `AsysCollect.collect()` 中加一行调用。
4. 能独立追踪一条「命令行参数 → 总调度 → 具体采集模块」的调用链。

本讲承接 u2-l1（asys 入口主流程）与 u2-l2/u2-l3（命令行体系与公共设施）。回忆 u2-l1 的结论：`asys.py` 通过 `EXECUTE_CMD_FUNC` 字典把 `collect` 子命令分发到 `AsysCollect` 类的无参构造 + `run()` 方法。本讲就深入这个类的内部。

## 2. 前置知识

- **输出目录**：u2-l1 讲过，collect 是「落盘型」命令，入口会创建 `asys_output_<毫秒时间戳>` 目录，其路径通过 `ParamDict().asys_output_timestamp_dir` 交接给采集方。本讲所有采集函数的第一个参数 `output_root_path` 就是这个目录。
- **EP 环境与 RC 环境**：asys 用 `ParamDict().get_env_type()` 区分部署形态。EP（Edge/端侧完整环境，有驱动和 Device）走「主机日志 + msnpureport 导出设备文件」路径；RC（资源受限环境）只收集 RC 侧日志。这个判断贯穿整个 collect 流程。
- **FileOperate**：u2-l3 介绍的静态文件工具类（`common/file_operate.py`），子模块里以 `from common import FileOperate as f` 的形式使用，提供 `collect_file_to_dir`、`collect_dir`、`walk_dir`、`create_dir` 等方法，支持 `COPY_MODE` 与 `MOVE_MODE` 两种模式。
- **ctypes 调 so**：u2-l3 的 `device.py` 已展示过 asys 用 ctypes 直调驱动 so 的手法；本讲 stacktrace 模块用同样的手法调 `libascend_trace` 库发信号、解析 bin 文件。
- **daemon 线程**：Python `threading.Thread(daemon=True)` 表示主进程退出时该线程直接被丢弃，不会阻塞进程退出。collect 中进度条线程和并行解析线程都用 daemon 线程，保证异常退出时进程不被卡住。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/asys/collect/asys_collect.py` | 总调度：`AsysCollect` 类，编排所有采集子模块 |
| `src/asys/collect/log/host_log_collect.py` | 主机侧日志采集（message、CANN 日志、atrace、安装日志） |
| `src/asys/collect/log/device_log_collect.py` | 设备侧日志采集（从 msnpureport 导出目录分拣） |
| `src/asys/collect/log/rc_log_collect.py` | RC 环境日志采集 |
| `src/asys/collect/trace/trace_collect.py` | atrace 二进制（.bin）转文本（.txt）解析器 |
| `src/asys/collect/stacktrace/stacktrace_collect.py` | `-r=stacktrace` 模式：向进程发信号导出栈 core 文件 |
| `src/asys/collect/stacktrace/interface.py` | `AscendTraceDll`：ctypes 封装的发信号与 bin 解析接口 |
| `src/asys/collect/coretrace/coretrace_collect.py` | coretrace 文本栈的地址符号化（addr2line） |
| `src/asys/collect/graph/graph_collect.py` | 图（Graph）文件采集，入口 `collect_graph` |
| `src/asys/collect/ops/ops_collect.py` | ops 包/算子信息采集，入口 `collect_ops` |
| `src/asys/collect/data_dump/data_dump_collect.py` | DataDump 数据采集，入口 `collect_data_dump` |

目录结构是完全统一的：`collect/<模块名>/<模块名>_collect.py`，每个模块一个包，入口统一是模块级函数 `collect_xxx(output_root_path)`（stacktrace 例外，它是类形态，原因见 4.4）。

## 4. 核心概念与源码讲解

### 4.1 asys_collect.py：采集总调度

#### 4.1.1 概念说明

`asys collect` 要一次拿到「故障现场」的所有材料：日志、图、dump 数据、ops 信息、trace、软硬件状态、健康检查。如果这些材料由使用者逐个手工收集，既容易漏也慢。`AsysCollect` 就是总调度员：它不改各个子模块，只负责按固定顺序把每个子模块的入口函数调用一遍，并把统一的输出目录递给它们。

#### 4.1.2 核心流程

`AsysCollect.run()` 的分派逻辑（伪代码）：

```text
run():
    if run_mode == "stacktrace":      # 特殊模式，交给专门的类
        return AsysStackTrace().run()
    else:                              # 默认全量采集
        task_res = collect()           # 依次调度各采集子模块
        clean_work()                   # 删除 msnpureport 的临时导出目录
        return task_res
```

`collect()` 主流程：

1. 参数检查：`--remote`/`--all`/`--quiet` 只允许配合 `-r=stacktrace` 使用，否则直接失败返回。
2. 启动一个 daemon 线程跑 `wait_view()`，在终端循环打印进度动画（`view.progress_display.waiting`）。
3. 按环境类型分流日志采集：EP 环境采主机日志 + 调 `msnpureport -f` 导出设备文件再分拣；RC 环境只采 RC 日志。
4. 依次调用 `collect_graph`、`collect_data_dump`、`collect_ops`、`collect_trace`。
5. 关闭已加载的 so（`LoadSoType().dll_close()`，避免后续采集 plog 干扰），再带超时地采状态信息（`AsysInfo`）与健康信息（`AsysHealth`）。
6. 置 `finish_flag = True` 结束进度线程，`t.join()` 等它退出。

#### 4.1.3 源码精读

总调度入口 `run()` 在这里分流 stacktrace 模式与默认采集模式，这与 u2-l1 讲的「无参构造 + run()」约定一致：

[src/asys/collect/asys_collect.py:154-L160](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L154-L160)

`collect()` 的采集顺序——注意所有子模块入口函数签名统一为「一个 `output_root_path` 参数」：

[src/asys/collect/asys_collect.py:104-L126](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L104-L126)

上面这段做了三件事：EP/RC 分流采日志（L104-L114）、顺序调四个采集入口（L117-L126）、其中 `collect_trace` 消费的正是日志采集阶段复制进 `dfx/atrace` 的 bin 文件（见 4.2 与 4.3 的衔接）。

进度条线程与结束同步：

[src/asys/collect/asys_collect.py:98-L102](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L98-L102)

[src/asys/collect/asys_collect.py:140-L141](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L140-L141)

`finish_flag` 是主线程与进度线程之间的开关量：采集全部完成后主线程置 True，进度线程的 `while not self.finish_flag` 循环随即退出。

状态/健康采集带超时保护，用装饰器实现（`GET_DEVICES_INFO_TIMEOUT`），超时不中断整个采集流程，只记一条错误日志：

[src/asys/collect/asys_collect.py:129-L138](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L129-L138)

`_device_file_export()` 展示了子模块与外部工具的协作方式：拼 shell 命令调 CANN 自带的 `msnpureport -f`，在输出目录下建 `export_tmp` 临时目录导出设备文件，失败则清理并返回 False：

[src/asys/collect/asys_collect.py:68-L90](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L68-L90)

#### 4.1.4 代码实践

1. **实践目标**：在源码层面验证「新增一个采集项只需在 `collect()` 里加一行」。
2. **操作步骤**：
   - 打开 `asys_collect.py`，在 L34-L41 的 import 区下方加一条（纸面操作，不真正改源码）：`from collect.env import collect_env`；
   - 在 `collect()` 中 `collect_trace(...)` 调用之后（L126 后）想象插入 `collect_env(self.output_root_path)`；
   - 新建 `src/asys/collect/env/env_collect.py`，写一个最小函数（示例代码）：

     ```python
     def collect_env(output_root_path):
         """示例代码：假想的环境变量采集项骨架"""
         import os
         from common import FileOperate as f
         target = os.path.join(output_root_path, "dfx", "env")
         f.create_dir(target)
         with open(os.path.join(target, "env.txt"), "w") as fw:
             fw.write(str(dict(os.environ)))
         return True
     ```

3. **需要观察的现象**：无需运行也能推理出输出布局——若真的执行 `asys collect`，输出目录会多出 `dfx/env/env.txt`。
4. **预期结果**：改动点只有「新目录 + 入口函数 + 一行调用」三处，中心代码其余部分零改动。此结论与 u2-l4 芯片适配层「新增芯片两个文件、中心零改动」的设计哲学一致。
5. 生成 .pyi / 运行行为：待本地验证（需要昇腾环境才能跑完整 `asys collect`）。

#### 4.1.5 小练习与答案

**练习 1**：`collect()` 为什么把 `AsysInfo`/`AsysHealth` 的采集放在最后，并且在之前调用 `LoadSoType().dll_close()`？

**答案**：源码 L128 注释写明「status 和 health 采集时会生成 plog」。asys 自己也是 CANN 生态程序，其通过 so 调用的设备查询会写 plog 日志；先关掉已加载的 so，再做状态/健康采集，可以让这两步产生的 plog 不混入前面「收集现场」阶段的日志，保证现场材料的时序纯净。放在最后也是因为它们是「当前时刻」的信息，而前面采集的是「故障发生时」的落盘材料。

**练习 2**：进度条线程为什么要设 `daemon=True`？`t.join()` 又是为什么？

**答案**：`daemon=True` 使主进程异常退出时进度线程被直接丢弃，避免一个只负责显示的线程阻塞进程退出（源码 L99-L101 的注释解释了 Python 主程序会等待非 daemon 子线程）。而 `t.join()` 是正常路径的收尾：`finish_flag=True` 后进度线程还需要一小段时间退出动画循环，join 保证终端输出不被后续内容打断、进程干净结束。

### 4.2 日志采集模块：collect/log

#### 4.2.1 概念说明

日志是最基础的故障证据。collect/log 包按「日志在哪」拆成三个文件：`host_log_collect.py`（主机侧）、`device_log_collect.py`（设备侧，从 msnpureport 导出目录分拣）、`rc_log_collect.py`（RC 环境）。每个文件都是若干 `collect_xxx` 小函数 + 一个汇总函数的扁平结构，是所有采集子模块里最简单的一个，适合作为理解「采集项长什么样」的样板。

#### 4.2.2 核心流程

以 `collect_host_logs` 为例，它编排四个小采集项，每项失败只 `log_warning` 并计数，最后汇总返回「是否全部成功」：

```text
collect_host_logs(output_root_dir):
    collect_messages   → /var/log/syslog 或 messages → dfx/log/host/message
    collect_cann_logs  → CANN 安装目录 plog 的 debug/run/security → dfx/log/host/cann/<type>
    collect_atrace_logs→ CANN 日志目录下的 atrace 目录 → dfx/atrace
    collect_install_logs → /var/log/ascend_seclog/ascend_install.log → dfx/log/host/install
    任一失败 warning + err+1；返回 err == 0
```

注意 `collect_atrace_logs` 的目标目录是 `dfx/atrace`——这正是 4.3 中 `collect_trace` 的输入目录，日志采集与 trace 解析通过约定好的目录布局衔接。

#### 4.2.3 源码精读

CANN 日志采集体现了一个细节：`asys launch`（复跑业务）模式下用 `MOVE_MODE` 搬走日志，普通 collect 用 `COPY_MODE`——复跑场景下日志是 asys 自己刚产生的，搬走即清场，下轮复跑不残留：

[src/asys/collect/log/host_log_collect.py:48-L61](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/log/host_log_collect.py#L48-L61)

系统 message 日志采集，自动在 `syslog` 与 `messages` 两个名字间探测（不同 Linux 发行版命名不同）：

[src/asys/collect/log/host_log_collect.py:31-L37](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/log/host_log_collect.py#L31-L37)

汇总函数的错误计数模式——单项失败不中断整体：

[src/asys/collect/log/host_log_collect.py:71-L89](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/log/host_log_collect.py#L71-L89)

设备侧与 RC 侧入口（由总调度分别调用）：

- [src/asys/collect/log/device_log_collect.py:138-L145](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/log/device_log_collect.py#L138-L145)：`collect_device_logs` 从 msnpureport 导出目录分拣 device 日志（slogd、device app、device os、黑匣子 bbox 等）。
- [src/asys/collect/log/rc_log_collect.py:111-L120](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/log/rc_log_collect.py#L111-L120)：`collect_rc_logs` 汇总 RC 环境的 message、stackcore、install、bbox 等采集。

#### 4.2.4 代码实践

1. **实践目标**：数清一次 EP 环境 `asys collect` 总共会尝试采集多少类日志。
2. **操作步骤**：通读三个 `*_log_collect.py`，列出每个 `collect_*` 小函数对应的「源路径 → 输出子目录」。
3. **需要观察的现象**：若有昇腾环境，运行 `asys collect` 后 `find asys_output_*/dfx/log -type d` 查看实际生成的目录树；无环境则在纸面完成表格。
4. **预期结果**：主机侧 4 类（message、cann×3 种类型、atrace、install），设备侧按 `device_log_collect.py` 中的分拣函数计（messages、stackcore、bbox、host driver、slogd、device app、device os、device id、event 等），RC 侧 6 类左右。部分目录因源不存在而缺失是正常的——对应 `log_warning` 记录。
5. 待本地验证（无设备环境下只能完成纸面部分）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `collect_cann_logs` 对 debug/run/security 三类日志中不存在的源只 warning 却仍然调用 `f.collect_dir`？

**答案**：`collect_dir` 对不存在的源目录会返回 False，`ret = ret and ...` 把失败记入总结果；warning 提前告知使用者缺哪一类（源码 L55-L60 中 `env_path_name` 指明来自哪个环境变量路径）。这体现了 asys「单项失败不拖垮整体」的防御式风格：日志缺失很常见（例如 security 日志默认不开启），不应让整个 collect 失败。

**练习 2**：`collect_atrace_logs` 把日志复制到 `dfx/atrace`，这个目录随后被谁消费？

**答案**：被 4.3 的 `collect_trace`（[trace_collect.py:321-L324](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L321-L324) 中 `trace_path = os.path.join(output_root_path, "dfx", "atrace")`）消费——日志模块先把原始 .bin 搬到统一位置，trace 模块再就地解析成 .txt。两个模块通过目录约定解耦。

### 4.3 trace 采集：二进制解析器 ParseTrace

#### 4.3.1 概念说明

atrace 是昇腾设备侧的 trace 机制，落盘的是私有格式的 `.bin` 文件，人不可读。`trace_collect.py` 的职责是把输出目录下 `dfx/atrace` 里的所有 `.bin` 就地解析成 `.txt` 并删除原 bin。它是采集子模块中「解析型」模块的代表（coretrace 同类）：不做采集搬运，只做格式转换。

#### 4.3.2 核心流程

bin 文件的结构是「控制头 + 结构描述段 + 消息数据段」三级：

```text
parse_data_segment(fp, trace_file):
    parse_ctrl_head:     读 magic/version 校验（version 2/3 对应 magic 0xd928），
                         读 trace_type、struct_size/data_size、时区偏移、基准实时时间；
                         version 3 额外读 cpu_freq，用于把 cycle 换算为时间
    parse_struct_segment: 读结构体条目数，逐条读「结构名 + 字段名/类型/显示模式/长度」，
                         存入 struct_dict[struct_type] —— 相当于自带 schema
    逐条读消息:
        cycle(8B) + txt_size(4B) + busy(1B) + struct_type(1B)
        busy 消息跳过；否则查 struct_dict，按 schema 逐字段 unpack
        时间戳 = real_time + cycle（version 3 再除以 cpu_freq）+ 时区偏移
```

时间换算的量纲关系（version 3）：

\[ t = \text{real\_time} + \frac{\text{cycle}}{\text{cpu\_freq}} \times 10^{6} + \Delta t_{\text{tz}} \]

其中 cycle 是硬件周期计数，cpu_freq 以 GHz 计，乘 \(10^6\)（源码常量 `FREQ_GHZ_TO_KHZ`）换算到纳秒；`NS_TO_S = 10^9` 再换算成秒交给 `datetime.fromtimestamp`。

并发模型：`run()` 遍历目录为每个 `.bin` 起一个 daemon `Thread` 并行解析，最后 `join` 全部线程，进度条用 `out_progress_bar(count, num)` 反映（来自 u2-l1 见过的 `common/task_common.py`）。

#### 4.3.3 源码精读

字段类型到 `struct.unpack` 格式与字节的映射表 `UNPACK`，把 C 结构体的类型编码翻译成 Python 解包规则：

[src/asys/collect/trace/trace_collect.py:63-L83](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L63-L83)

控制头解析：magic/version 校验失败直接抛 `ValueError`，由上层 `parse()` 捕获后仅在文件模式下记日志：

[src/asys/collect/trace/trace_collect.py:148-L172](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L148-L172)

消息段解析中的时间戳计算（含 version 3 的 cpu_freq 分支）：

[src/asys/collect/trace/trace_collect.py:255-L260](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L255-L260)

多线程解析骨架：每个 `.bin` 一个线程，最后统一 join：

[src/asys/collect/trace/trace_collect.py:299-L318](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L299-L318)

模块入口 `collect_trace` 只有 4 行——定位目录、实例化、调 `run()`：

[src/asys/collect/trace/trace_collect.py:321-L324](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L321-L324)

解析成功后 `.bin` 被删除、只留 `.txt`（`start_parse_file` 中 `os.remove(trace_file)`）：

[src/asys/collect/trace/trace_collect.py:279-L286](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/trace/trace_collect.py#L279-L286)

#### 4.3.4 代码实践

1. **实践目标**：给 `ParseTrace` 类写一张「输入 / 输出 / 依赖」三栏说明书。
2. **操作步骤**：
   - **输入**：`run(trace_path, count)` 接收目录路径；真正被解析的是其中 `*.bin` 文件，格式由 `parse_ctrl_head`/`parse_struct_segment` 的读取顺序定义。
   - **输出**：与 bin 同名的 `.txt`（时间戳 + 结构名 + 字段值列表，见 L266 的格式化字符串），原 bin 删除；返回值为布尔。
   - **依赖公共设施**：`FileOperate.walk_dir`（遍历）、`task_common.out_progress_bar`（进度）、`log_error/log_warning`（日志，且被 `is_file` 开关控制静音）。
3. **需要观察的现象**：若手头有任何 atrace .bin 文件，可单独构造测试：建临时目录 `t/dfx/atrace` 放入 bin，写 3 行脚本调 `collect_trace("t")`（示例代码）：

   ```python
   import sys
   sys.path.insert(0, "src/asys")
   from collect.trace import collect_trace
   collect_trace("t")
   ```

4. **预期结果**：合法的 version 2/3 文件生成 `.txt`；版本不符的文件打印 "check the version" 错误日志且保留原 bin。
5. 待本地验证（需要有真实 atrace bin 文件）。

#### 4.3.5 小练习与答案

**练习 1**：`ParseTrace.__init__` 的 `is_file=False` 参数控制什么？为什么需要它？

**答案**：`error()`/`warning()` 方法只有 `is_file=True` 时才真正输出日志（L104-L110）。批量解析几十个 bin 时，个别坏文件报错是预期的，不刷屏；当解析对象是用户显式指定的单个文件时（is_file=True），报错必须让用户看见。这是「静音开关」与 u2-l3 日志全局 disable 机制配套的局部实现。

**练习 2**：`parse_data_segment` 中 `busy` 为真的消息为什么直接 `get_res_data` 跳过？

**答案**：busy 标志表示该消息是 CPU 占用统计类数据而非结构化消息（L252-L254），没有可按 schema 解析的字段内容，只需按 `msg_txt_size` 跳过等长字节，保证文件偏移不错位。

### 4.4 stacktrace 采集：信号触发式采集

#### 4.4.1 概念说明

前面的模块都是「收集已经落盘的东西」，stacktrace 相反：它要在采集时刻主动触发现场生成。用法是 `asys collect -r=stacktrace --remote=<PID> --all`：向目标进程发送实时信号 35（`SIGRTMIN+1`），昇腾运行时注册的信号处理函数会把各线程调用栈导出成 `stackcore_tracer_35_<pid>_*.bin`，asys 再等文件生成、调 so 里的接口把 bin 解析成 txt、搬进输出目录。它是采集子模块中唯一的「类形态 + 交互式（Y/N 确认）」模块，因为发信号有可能杀死禁用了信号接收的进程，必须让用户确认。

#### 4.4.2 核心流程

`AsysStackTrace.run()` 的防御式前置检查链 + 主流程：

```text
run():
    删除输出目录（本模式独占重建）
    ① _check_other_param:      --task_dir/--tar 不允许与本模式同用；timeout 规整到 [1,60]
    ② --remote 与 --all 必须同时出现
    ③ trace_dll 加载检查（libascend_trace）
    ④ _check_remote_id_validity: remote_id>1、进程存在、确为进程 PID
    ⑤ 非 --quiet 时 Y/N 确认（警告 ASCEND_COREDUMP_SIGNAL=none 的进程会被杀）
    ⑥ _set_trace_work_path:    优先读 /proc/<pid>/environ 里的 ASCEND_WORK_PATH，否则用户主目录
    ⑦ _check_collect_stacktrace_parallel: 禁止对同一进程并行采集
    ⑧ 记录已有 bin 数 → send_signal_to_pid（信号35）→ 轮询等待新 bin 生成且不再被 lsof 占用
    ⑨ parse_stackcore_bin_to_txt → 清理 dfx 日志 → collect_dir 拷入输出目录
```

等待逻辑（L110-L128）每 0.5 秒一轮：先看 bin 文件数量是否比采集前多，多了之后继续轮询 `lsof` 直到写文件的进程释放句柄，总时长受 `--timeout`（默认 `CHECK_BIN_DEFAULT_TIMEOUT`，上限 60 秒）约束。

#### 4.4.3 源码精读

类定义与参数读取——直接从 `ParamDict` 取 u2-l2 讲过的统一 key：

[src/asys/collect/stacktrace/stacktrace_collect.py:35-L47](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/stacktrace_collect.py#L35-L47)

从目标进程的 `/proc/<pid>/environ` 里解析 `ASCEND_WORK_PATH` 定位 bin 文件生成路径（注意 environ 以 `\0` 分隔的特殊格式）：

[src/asys/collect/stacktrace/stacktrace_collect.py:49-L72](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/stacktrace_collect.py#L49-L72)

轮询等待 bin 文件生成并确认写入完成（lsof 探测句柄释放）：

[src/asys/collect/stacktrace/stacktrace_collect.py:110-L128](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/stacktrace_collect.py#L110-L128)

run() 主流程中发信号前后的关键步骤串联：

[src/asys/collect/stacktrace/stacktrace_collect.py:224-L276](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/stacktrace_collect.py#L224-L276)

其中信号发送与 bin 解析的实现不在本文件，而在其基类 `AscendTraceDll`——ctypes 调 so 中的 `sigqueue`（信号编号 `SIGRTMIN + 1`，即 35）和 `AtraceStackcoreParse`：

[src/asys/collect/stacktrace/interface.py:36-L57](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/interface.py#L36-L57)

[src/asys/collect/stacktrace/interface.py:59-L75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/stacktrace/interface.py#L59-L75)

这延续了 u2-l3 `device.py` 的手法：Python 做流程编排，重活交给 so 里的 C 接口。

#### 4.4.4 代码实践

1. **实践目标**：梳理 stacktrace 模块对操作系统机制的依赖清单。
2. **操作步骤**：通读 `stacktrace_collect.py` 与 `interface.py`，把每个 `_xxx` 私有方法依赖的 OS 机制列成表：`/proc/<pid>/environ`（读环境）、`os.kill(pid, 0)`（探测进程存活）、`ps -p`/`ps -efT`（进程/线程查询）、`lsof`（文件句柄）、`sigqueue`（ctypes 实时信号）、`ls -lt | wc -l`（文件计数）。
3. **需要观察的现象**：任选一台 Linux 机器（无需昇腾环境），验证 `cat /proc/self/environ | tr '\0' '\n'` 能按行打印环境变量——这正是 L54 `env_content.split('\0')` 处理的格式。
4. **预期结果**：确认该模块本质上是一套「纯用户态 OS 机制 + 一个厂商 so」的组合；也解释了为什么它有大量 PermissionError/FileNotFoundError 的退化分支（读别人进程的 environ 经常没权限）。
5. 待本地验证（Linux 通用机制部分可直接验证；发信号部分需要昇腾运行时）。

#### 4.4.5 小练习与答案

**练习 1**：`_check_collect_stacktrace_parallel` 是怎么判定「有另一个 asys 正在对同一进程采栈」的？

**答案**：先用 `ps -ef | grep asys collect | grep stacktrace`（排除当前进程父进程）找出其他采栈命令行，用正则 `--remote[ =](\d+)` 抽出它们的 remote_id；若与自己的 remote_id 相同直接拒绝；否则再取自己进程的所有线程 TID，与 remote_id 合并去重比较——发现重复说明目标进程的线程里有挂起的采集任务，同样拒绝（L194-L212）。本质是用进程表当「分布式锁」。

**练习 2**：为什么 `AsysStackTrace` 要在 run() 开头 `f.remove_dir(self.output)` 删掉输出目录？

**答案**：stacktrace 模式的输出目录由入口按时间戳新建（u2-l1），本模式产物只有栈文件，删除重建保证目录里不残留上次内容；而全量 collect 模式多模块共用同一目录，绝不能这么做。这也解释了 4.1 中 `--task_dir`/`--tar` 与本模式互斥的检查：那两个参数影响的是目录创建与压缩行为，与本模式的独占用法冲突。

### 4.5 采集子模块的结构共性与新增采集项扩展点（含 coretrace 对比）

#### 4.5.1 概念说明

把四个模块并排看，能提炼出 collect 子系统的「框架约定」。框架不存在于任何基类中，而是靠目录约定 + 入口函数签名约定 + 输出目录布局约定形成的隐式契约。coretrace 是最后一个证据：它与 trace 结构几乎同构，但没人强迫它这样写——是约定在起作用。

#### 4.5.2 核心流程

一个「标准采集项」的解剖：

| 约定 | 内容 | 体现 |
| --- | --- | --- |
| 目录约定 | `collect/<name>/<name>_collect.py` | 所有子模块 |
| 入口签名 | `collect_xxx(output_root_path)` 模块级函数 | log/graph/ops/data_dump/trace |
| 输出布局 | 产物统一放 `output_root_path/dfx/...` 下 | `dfx/log`、`dfx/atrace` 等 |
| 错误风格 | 单项失败 warning，不抛异常不中断 | `collect_host_logs` 的 err 计数 |
| 并发模型 | 逐文件 daemon Thread + join + 进度条 | trace 与 coretrace 的 `run()` |
| 例外 | 交互式/独占型模块用类形态 | `AsysStackTrace` |

coretrace 与 trace 的同构对照（都是「解析型」模块）：

```text
trace:     ParseTrace.run(trace_path)        遍历目录 → 每文件一线程 → bin→txt → 删原文件
coretrace: ParseCoreTrace.run(coretrace_path) 遍历目录 → 每文件一线程 → 文本栈地址→符号行 → 原地覆写
```

coretrace 的解析内容不同：它读的是已被导出成文本的 coretrace 栈文件，先把每行按格式分类（Signal/PID/栈帧 `#n`/内存映射段），把映射段攒进 `parse_data.maps`（二进制名 → 地址区间）；遇到栈帧行时用地址落在哪个区间找到所属二进制，换算偏移后调外部工具 `addr2line -Cifps -e <bin> -a <addr>` 把地址翻译成「函数名+源码行」，最后原地覆写文件。

#### 4.5.3 源码精读

coretrace 逐行分类的状态机：`parse_line` 按行首 token 分派，映射段行被吸收进 `maps`、栈帧行被符号化、uburma/davinci_manager 噪声行被丢弃：

[src/asys/collect/coretrace/coretrace_collect.py:103-L132](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/coretrace/coretrace_collect.py#L103-L132)

栈帧行符号化：判断地址落入区间，算偏移 `fp - start_addr - shift`（非 0 号帧固定减 4），拼 addr2line 命令行：

[src/asys/collect/coretrace/coretrace_collect.py:78-L101](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/coretrace/coretrace_collect.py#L78-L101)

与 trace 完全同构的多线程骨架 + 类级共享的「缺失二进制」去重告警集合（`missing_binary` 是类属性，配 `Lock` 保证多线程安全，同一缺失文件只 warning 一次）：

[src/asys/collect/coretrace/coretrace_collect.py:40-L59](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/coretrace/coretrace_collect.py#L40-L59)

[src/asys/collect/coretrace/coretrace_collect.py:177-L195](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/coretrace/coretrace_collect.py#L177-L195)

graph/ops/data_dump 三个「搬运型」模块的入口签名与 trace 完全一致（节选定义处）：

- [src/asys/collect/graph/graph_collect.py:86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/graph/graph_collect.py#L86)
- [src/asys/collect/ops/ops_collect.py:302](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/ops/ops_collect.py#L302)
- [src/asys/collect/data_dump/data_dump_collect.py:67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/data_dump/data_dump_collect.py#L67)

#### 4.5.4 代码实践（本讲综合实践预热）

1. **实践目标**：完成规格要求的任务——总结一个采集子模块的输入输出与依赖，再设计「环境变量采集项」骨架。
2. **操作步骤**：
   - 任选 `trace_collect.py`（或本讲 4.3 已给出的三栏说明书），核对：输入（`dfx/atrace` 下 .bin）、输出（同名 .txt + 布尔返回）、依赖（`FileOperate`、`out_progress_bar`、日志）。
   - 新增采集项骨架（示例代码，纸面设计）：

     ```text
     src/asys/collect/env/
     ├── __init__.py            # from collect.env.env_collect import collect_env
     └── env_collect.py

     # env_collect.py 骨架
     def collect_env(output_root_path):
         """采集本机与进程相关的关键环境变量到 dfx/env/env.txt"""
         # 1. 选源：os.environ + 目标进程 environ（可选）
         # 2. 建目标目录：os.path.join(output_root_path, "dfx", "env")
         # 3. 失败只 log_warning 返回 False，不抛异常 —— 遵守框架错误风格
         ...
     ```

   - 在 `asys_collect.py` 的 `collect()` 中 `collect_trace` 之后加 import 与一行调用。
3. **需要观察的现象**：纸面检查清单——目录命名符合 `collect/<name>/<name>_collect.py`？入口签名是 `collect_xxx(output_root_path)`？产物落在 `dfx/` 下？单项失败不中断？
4. **预期结果**：三处改动（新包、入口函数、一行调用）即完成扩展；对照 u6-l1 可知还应补 `test/ut/asys` 下的单测。
5. 待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：coretrace 的 `missing_binary` 为什么定义为类属性并配 `Lock`，而不是实例属性？

**答案**：解析是多线程的（每个文件一个 Thread，多个线程共享同一个 `ParseCoreTrace` 语义目标），缺失的符号二进制往往在多个文件里重复出现。类属性让所有线程共享一个去重集合，`Lock` 保证 `in` 判断与 `add` 的原子性（L56-L60），效果是同一缺失文件全进程只 warning 一次，避免刷屏。

**练习 2**：如果想新增的采集项需要用户交互（比如确认后才执行），应该采用函数形态还是类形态？依据是什么？

**答案**：类形态，参照 `AsysStackTrace`：它有独立参数集（remote/all/quiet/timeout）、独占输出目录、交互确认和复杂前置检查链，`AsysCollect.run()` 用 `run_mode` 分派把它整体切出去（asys_collect.py L155-L156）。纯搬运/解析型采集项才用函数形态挂在 `collect()` 主流程里。

## 5. 综合实践

**任务：追踪一条完整的 collect 调用链并设计一个新采集项。**

1. 从 u2-l2 的 `cmd_parser.py` 中找到 `collect` 子命令的 `Arg` 定义（重点看 `-r/--run_mode`、`--remote`、`--all`），确认它们如何进入 `ParamDict`。
2. 沿 `asys.py → EXECUTE_CMD_FUNC["collect"] → AsysCollect.run()` 进入本讲的 `collect()`，画出完整调用图：标注 EP/RC 两条分支、7 个采集入口的调用顺序、进度线程与主线程的交互点。
3. 任选 `dfx/atrace` 这条数据线，标注它经过的模块：`collect_atrace_logs`（复制进目录）→ `collect_trace`（解析 bin 为 txt），说明两个模块如何通过目录约定解耦。
4. 完成纸面设计：新增「环境变量采集项」（4.5.4 的骨架），写出改动文件清单（应为 3 个新文件/改动点）与输出目录布局。

验收标准：不看讲义能向别人讲清「asys collect 一次运行都发生了什么」，并说出新增采集项的最小改动集。

## 6. 本讲小结

- `AsysCollect` 是纯编排层：`run()` 按 `run_mode` 切换 stacktrace/全量采集两条路径，`collect()` 按固定顺序调度 7 类采集入口，进度条用 daemon 线程 + `finish_flag` 开关协作。
- 采集子模块统一遵循隐式框架契约：`collect/<name>/<name>_collect.py` 目录、`collect_xxx(output_root_path)` 入口签名、产物落 `dfx/` 布局、单项失败不中断。
- 子模块分三类形态：搬运型（log/graph/ops/data_dump，靠 `FileOperate` 复制/搬移）、解析型（trace/coretrace，多线程逐文件转换格式）、交互触发型（stacktrace，信号 + 确认 + 轮询）。
- trace 模块演示了「自带 schema 的二进制解析」：控制头校验 magic/version，结构段即 schema，消息段按 schema 解包，时间戳用 real_time + cycle/freq 换算。
- stacktrace 模块演示了「Python 编排 + so 干重活」：ctypes 调 `sigqueue` 发信号 35、调 `AtraceStackcoreParse` 解析 bin，前置检查链与 ps/lsof 轮询全部用用户态 OS 机制。
- 新增一个采集项的最小改动集是三个：新包目录、入口函数、`collect()` 里一行调用——与 u2-l4 芯片适配层「中心代码零改动」的设计哲学一脉相承。

## 7. 下一步学习建议

- 下一讲 u2-l6 将逐个阅读 `launch`、`info`、`health`、`diagnose` 四个业务子命令，其中 `AsysInfo`/`AsysHealth` 已在本讲 `collect_status_info`/`collect_health_info` 中作为被调度方露面，可提前留意它们的 `write_info()`/`run()` 接口。
- 若想深入数据内容而非流程，可阅读 `docs/zh/asys/` 下的用户文档（如 coredump 解析相关篇目），对照 u2-l7 的 analyze 子命令。
- 建议顺带精读 `src/asys/common/file_operate.py` 的 `collect_dir`/`collect_file_to_dir` 实现，理解 COPY_MODE/MOVE_MODE 的差异与失败返回值约定——它是所有搬运型模块的地基。
