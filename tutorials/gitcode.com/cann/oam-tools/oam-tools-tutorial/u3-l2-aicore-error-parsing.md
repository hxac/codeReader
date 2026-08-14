# u3-l2 AI Core Error 报告解析：aicore_error_parser

## 1. 本讲目标

上一讲（u3-l1）我们读完了 msaicerr 的入口 `msaicerr.py`，知道了 `-p` 模式的两阶段结构：先由 `Collection` 把原始报告目录归档成 `info_<时间戳>/collection/` 布局，再交给 `AicoreErrorParser` 做深度解析。本讲就深入这第二阶段，学完后你应该能够：

1. 说出 `AicoreErrorParser.parse()` 十一步流水线每一步做什么、哪些步骤是"必须"、哪些是"尽力而为"。
2. 理解 msaicerr 如何从 plog 日志里"grep + 正则"出错误码、出错算子名、出错指令地址，以及错误码如何通过 `AIC_ERROR_INFO_DICT` 位映射翻译成人类可读的错误描述。
3. 理解 `AicErrorInfo` 这个"结果容器"如何把几十项解析结果组织成 `info.txt` 报告与根因结论。
4. 理解芯片适配层 `ascend_handler.py` 的"前缀匹配 + 目录自动发现"机制，以及芯片型号（Ascend910_96、Ascend950 等）到底在哪些环节影响解析流程。

## 2. 前置知识

- **AI Core Error**：昇腾 NPU 上 AI Core（向量/矩阵执行单元）在执行算子时发生的硬件级异常，会在 plog（进程日志）中打印一条带寄存器现场（错误码、PC 指针、各功能单元错误信息）的 `error info:` 记录。
- **plog**：CANN 各组件（GE、Runtime、驱动）按进程落盘的日志目录，`Collection` 阶段会把它搬运到 `<输出>/collection/plog`。msaicerr 解析阶段几乎所有的信息提取都是"对 plog 执行 grep 命令 + 正则捕获"完成的——这是本工具最鲜明的实现风格。
- **L0 / L1 日志级别**：CANN 异常日志的两种详略形态。L1 带 `[AIC_INFO]` 前缀的算子信息打印（dev_func、tiling_key、args 等），信息更全；L0 只有传统错误打印。解析器必须先探测日志是哪种形态再选对应解析策略。
- **FFTS+ 与 SK（SuperKernel）**：两类特殊执行场景。FFTS+ 场景的错误打印关键字是 `fftsplus task execute failed`；SK 场景只生成 `*_host.o` 而没有 device 侧的 `.o/.json/.cce`，解析器需要走特殊分支。
- **错误码位映射**：AI Core Error 的错误码是一个多比特位字段，每一个置位比特对应一种具体错误（如 bit 175 = `vec_err_parity_err`）。`Constant.AIC_ERROR_INFO_DICT` 就是"比特序号 → 中文错误描述"的映射表。
- **cce-objdump / llvm-objdump**：昇腾 CCE 编译器配套的反汇编工具，msaicerr 用它把出错算子的 `.o` 文件反汇编，定位出错指令行号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/msaicerr/msaicerr.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py) | 入口。`analyse_report_path()` 串联 Collection 与 AicoreErrorParser（u3-l1 已精读，本讲只引用衔接点） |
| [src/msaicerr/ms_interface/aicore_error_parser.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py) | 本讲主角：AI Core Error 解析器，约 1690 行，包含 11 步解析流水线 |
| [src/msaicerr/ms_interface/aic_error_info.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py) | 解析结果容器：几十项字段 + `analyse()` 报告生成 + `get_conclusion()` 根因推断 |
| [src/msaicerr/ms_interface/ascend_handler.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend_handler.py) | 芯片适配基类 `AscendHandlerBase`（前缀匹配、标杆算子编译） |
| [src/msaicerr/ms_interface/ascend910_96/ascend91096_handler.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend910_96/ascend91096_handler.py) 与 [src/msaicerr/ms_interface/ascend950/ascend950_handler.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend950/ascend950_handler.py) | 两款具体芯片的 handler（当前只声明芯片前缀） |
| [src/msaicerr/ms_interface/constant.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py) | 常量中枢：返回码、`AIC_ERROR_INFO_DICT` 位映射表、错误日志正则 `RegexPattern` |
| [src/msaicerr/ms_interface/utils.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py) | 公共设施：`get_inquire_result()`（grep+正则提取）与 `load_ascend_handlers()`（芯片 handler 自动发现） |
| [src/msaicerr/ms_interface/compile_file.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/compile_file.py) | 标杆算子（golden op）编译入口，按 soc_version 分发到芯片 handler |

## 4. 核心概念与源码讲解

### 4.1 AicoreErrorParser 解析流水线：从报告目录到解析结论

#### 4.1.1 概念说明

`AicoreErrorParser` 是 `-p` 模式的第二阶段引擎。它的输入并不是用户传给 `-p` 的原始报告目录，而是 `Collection` 归档后的输出目录（`info_<时间戳>/`，其下有 `collection/plog`、`collection/compile`、`collection/dump`、`collection/graph` 等子目录）。衔接代码在入口处只有三行：

[msaicerr.py:L112-L117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L112-L117) —— 先 `Collection(...).collect()` 归档现场，再构造 `AicoreErrorParser(output_path, args.device_id, collect_succ)` 并调用 `parse()`。注意第三个参数 `collect_succ`：如果采集阶段已经失败，解析器会在第 2 步直接走"空结论"分支。

解析器内部维护三个模式开关，在解析开始时一次性探测：

- `parse_level`：0 = L0 传统日志，1 = L1 带 `[AIC_INFO]` 的详细日志；
- `ffts_flag`：是否 FFTS+ 场景；
- `is_sk`：是否 SK（SuperKernel）场景。

#### 4.1.2 核心流程

`parse()` 的整体流水线（步骤编号与源码打印一致）：

```text
parse()
├── add_objdump_to_path()        # 预备：把 cce-objdump/llvm-objdump 加进 PATH
├── check_plog_info()            # 预备：探测 L0/L1、ffts_flag、is_sk 三个开关
├── Step 1  get_op_info()        # 必须：错误码/PC/算子名/kernel 文件 → AicErrorInfo
├── Step 2  创建 aicerror_0_<时间戳>/ 结果目录（Step1 失败则写空结论并返回）
├── Step 3  _get_graph_file()    # 非必须：GE 图文件中找 node 信息
├── Step 4  args before/after    # 非必须：对比算子执行前后参数是否被改写
├── Step 5  _decompile()         # 反汇编 .o，估算出错指令行号
├── Step 6  _check_atomic_clean()# 检查框架是否正确插入 memset/atomic_clean
├── Step 7  DumpDataParser       # 解析 dump 数据、生成 tiling data
├── Step 8  _get_sub_ptr()       # L0 专属：解析二级指针 tensor
├── Step 9  run_single_operator()# 单算子复现验证
├── Step 10 run_test_env()       # 用内置标杆算子验证环境是否健康
├── Step 11 _write_errorinfo_file() + _write_summary_file()
└── return get_return_code(info) # 按优先级链给出最终错误码
```

关键设计：**只有 Step 1 是决定成败的**——`get_op_info()` 返回 `None` 时直接写空结论、返回 `MS_AICERR_INVALID_SLOG_DATA_ERROR`；Step 3~10 全部是"尽力而为"，单项失败只打告警日志，不中断流水线。这和 asys 的"失败退化"哲学一脉相承（见 u2-l3）。

#### 4.1.3 源码精读

**（1）模式探测：check_plog_info()**

[aicore_error_parser.py:L257-L279](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L257-L279) —— 对整个采集目录做三次 grep：

- grep `\[AIC_INFO\] dev_func:` 命中 → `parse_level = 1`（L1 日志），否则 0；
- grep `fftsplus task execute failed` 命中 → `ffts_flag = True`；
- grep `Begin to dump callback exception` 命中 → `is_sk = True`（SK 场景只有 host.o）。

这三个开关随后决定后续几乎每个步骤走哪条分支——这是典型的"先识别输入形态，再分策略处理"的状态机式设计。

**（2）流水线主体：parse()**

[aicore_error_parser.py:L1397-L1417](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1397-L1417) —— 流水线开头：`add_objdump_to_path()` 准备反汇编工具，`check_plog_info()` 探测模式，随后 Step 1 调 `get_op_info()`；如果 `info is None or not self.collect_succ`，就在 `aicerror/` 目录写一个只含 `root_cause_conclusion`（空结论）的 `info.txt` 并返回 `MS_AICERR_INVALID_SLOG_DATA_ERROR`。

[aicore_error_parser.py:L1419-L1476](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1419-L1476) —— Step 2 用错误发生时间给结果目录命名（`aicerror_0_<err_time 格式化>`）；Step 3~7 依次做图节点提取、args 前后对比、反编译、atomic_clean 检查、dump 数据解析（dump 解析细节属于 `-d` 模式的 `DumpDataParser`，将在 u3-l3 展开）；Step 8 二级指针解析仅 L0 执行。

[aicore_error_parser.py:L1488-L1512](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1488-L1512) —— Step 9 单算子复现、Step 10 标杆算子环境验证（Step 10 失败仅打 info 日志跳过）、Step 11 写 `info.txt`（完整报告）与 `README.txt`（概要），最后 `return self.get_return_code(info)`。

**（3）最终错误码优先级链：get_return_code()**

[aicore_error_parser.py:L1514-L1531](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1514-L1531) —— 按固定优先级检查各项结论，返回第一个命中的 `Constant.MS_AICERR_*` 错误码：框架未插 memset（105）> atomic 溢出（107）> 算子输入数据错误（104）> args 被踩（106）> 内存分配错误（102）> 单算子复现失败（101）> 环境异常（103）> 全部正常（0）。这条 if-elif 链就是 msaicerr 给用户的"根因排序"。

#### 4.1.4 代码实践

**实践：用 grep 手工模拟 check_plog_info()**

1. 实践目标：不运行 msaicerr，只用 shell 命令验证三个模式开关的探测逻辑，体会"grep 即解析"的实现风格。
2. 操作步骤：
   - 准备任意一个 CANN plog 目录（若没有，自己造一个文件 `/tmp/fake_plog/plog.log`，写入一行 `2026-08-14-10:00:00.000.000 ... [AIC_INFO] dev_func:my_op_kernel`，此行为示例内容，非项目原有日志）；
   - 依次执行（与源码三条命令一一对应）：
     ```bash
     grep "\[AIC_INFO\] dev_func:" -inrE /tmp/fake_plog
     grep "fftsplus task execute failed" -inrE /tmp/fake_plog
     grep "Begin to dump callback exception" -inrE /tmp/fake_plog
     ```
3. 需要观察的现象：第一条命令有输出（退出码 0），后两条无输出（退出码 1）。
4. 预期结果：按 check_plog_info 的逻辑可推出 `parse_level=1, ffts_flag=False, is_sk=False`。若把 `dev_func` 那行删掉再跑，`parse_level` 应变为 0。`utils.get_inquire_result()` 正是封装了"执行命令 + 判退出码 + 正则提取"这一套（见 [utils.py:L421-L430](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L421-L430)）。本实践可在任意 Linux 环境完成。

#### 4.1.5 小练习与答案

**练习 1**：如果采集目录里完全没有 plog（`collection/plog` 为空目录），`parse()` 会走到哪一步、返回什么错误码？

**答案**：Step 1 的 `get_op_info()` 先调 `get_dump_data_info()`，grep 不到 `dump exception to file` 会抛 `AicErrException(MS_AICERR_INVALID_PATH_ERROR)`，被 `get_op_info` 捕获后返回 `None`；`parse()` 随即走空结论分支，写出只含根因结论的 `info.txt` 并返回 `MS_AICERR_INVALID_SLOG_DATA_ERROR`（8）。参考 [aicore_error_parser.py:L429-L433](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L429-L433) 与 [L1413-L1417](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1413-L1417)。

**练习 2**：为什么 Step 3（GE 图提取）失败不中断流水线，而 Step 1 失败却直接返回？

**答案**：Step 1 提取的错误码/PC/算子名是所有后续步骤的输入源头，缺失则一切分析无从谈起；Step 3 的图文件只是补充 node 信息，缺失只影响报告完整度。源码中 Step 3 的 `_get_op_by_graph` 找不到时只 `print_warn_log` 并把 `info.graph_file` 置 `None`（[aicore_error_parser.py:L1429-L1430](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1429-L1430)）。

### 4.2 错误码与错误信息：从 plog 正则到 AIC_ERROR_INFO_DICT 位映射

#### 4.2.1 概念说明

这一模块回答两个问题：错误信息从哪里来（`get_op_info()` / `set_info()` 的 grep+正则提取），以及错误码怎么变成人话（`AicErrorInfo._get_aicerror_info()` 的位映射 + 各功能单元寄存器解析）。

AI Core Error 的错误码本质是"按比特置位的故障标志字段"：日志里打印的形如 `0x...` 的 `error code`，其中每个为 1 的比特对应 `Constant.AIC_ERROR_INFO_DICT` 里的一个条目（键是十进制比特序号，值是中文错误描述，如 `175: "vec_err_parity_err, VEC 中发生奇偶校验错误"`）。同时日志还会打印各功能单元（VEC/IFU/MTE/CUBE/CCU/BIU）的寄存器现场 `extra info`，解析器按位切出错误类型和出错地址。

#### 4.2.2 核心流程

**错误现场的提取（Step 1 内部）**：

```text
get_op_info()
├── get_dump_data_info()     # grep 'dump exception to file' → (thread_id, dump 文件名)
├── grep 'error info:' + AICORE_ERR_OCCUR 正则（失败则换 OST 版正则）
│      → [{err_time, thread_id, dev_id, core_id, error_code, start_pc, current_pc, extra_info}, ...]
├── 按 err_time 排序，用 dump 的 thread_id 匹配出本错误的那条记录
├── grep 'RUNTIME' 提取 block_dim
└── set_info(aic_err_ret, ...) → AicErrorInfo
```

**V300 错误码合成**：当从日志拿到的 `error_code` 为 0 时，解析器会从 `The extend info: errcode:` 打印中取三个分段码 \(c_0, c_1, c_2\)，按位拼接成一个新的 128+ 位错误码：

\[ \text{new\_code} = c_0 \;|\; (c_1 \ll 64) \;|\; \Bigl(\bigl(((c_2 \gg 32) \ll 17)\ \&\ (c_2\ \&\ 0x1FFFF)\bigr) \ll 128\Bigr) \]

**错误码到描述的翻译（报告第 2 节）**：`hexstr_to_list_bin` 把十六进制错误码转成置位比特序号列表 → 逐个查 `AIC_ERROR_INFO_DICT` → 按错误类别（vec/ifu/mte/cube/ccu/biu）分别调用 `_analyse_*_errinfo()` 从 extra_info 中按位切出 `err_type` 与 `err_addr`。

#### 4.2.3 源码精读

**（1）错误日志正则：RegexPattern.AICORE_ERR_OCCUR**

[constant.py:L395-L404](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L395-L404) —— 两个带命名分组的长正则，从 `error info:` 日志行里捕获 `err_time/thread_id/dev_id/core_id/error_code/start_pc/current_pc/extra_info`。`AICORE_ERR_OCCUR_OST` 是 outstanding（多错误）场景的变体，额外捕获 `s_start_pc`（第二个错误的起始 PC）。这就是 msaicerr 对 plog 日志格式的"接口契约"。

**（2）错误现场提取主函数：get_op_info()**

[aicore_error_parser.py:L429-L475](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L429-L475) —— 先拿 dump 文件信息；再 grep `error info:` 用主正则提取（[L436-L443](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L436-L443)），不命中换 OST 正则；[L445](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L445) 按 `err_time` 排序（None 排最后）保证取到的是真实时间序；[L452-L460](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L452-L460) 用 dump 记录的 `thread_id` 与错误记录的 `thread_id` 做一致性校验——对不上就认为"dump 数据与错误进程不是同一个"而放弃解析；最后 grep `RUNTIME` 取最大 `block_dim` 后进入 `set_info()`。

**（3）组装结果：set_info()**

[aicore_error_parser.py:L376-L427](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L376-L427) —— 把正则结果逐项填入 `AicErrorInfo`：`error_code_all`（完整 errcode 打印）、`extra_info`（各单元寄存器错误码，由 `_get_extra_info()` 用 6 条小正则从 extra_info 文本中抽 IFU/CCU/BIU/CUBE/MTE/VEC 各键，见 [L281-L301](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L281-L301)）、算子四元组（stream_id/task_id/node_name/kernel_name）、kernel 三件套路径（`.o/.json/.cce`）、tiling 信息、driver AI Core 数（ctypes 调 `libruntime.so` 的 `rtGetAiCoreCount`，[L319-L332](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L319-L332)）。[L424-L425](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L424-L425) 即 V300 错误码合成的触发点（error_code 为 "0"/"0x0" 时）。

**（4）V300 错误码合成的位运算**

[aicore_error_parser.py:L303-L317](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L303-L317) —— grep `The extend info: errcode:` 捕获三个分段码，按上文公式移位、掩码、按位或合并为一个新错误码字符串（`str(hex(new_code))`）。这一步只做位拼接，不做字典翻译——翻译统一发生在报告生成阶段。

**（5）位映射字典：AIC_ERROR_INFO_DICT**

[constant.py:L102-L175](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L102-L175)（节选）—— 键为比特序号（103~175 等），值为"错误名, 中文描述"。条目按功能单元分组：`vec_*`（向量单元）、`mte_*`（搬运单元）、`cube_*`/`fixp_*`（矩阵/定标）、`ccu_*`（标量控制）、`biu_*`（总线接口）、`ifu_*`（取指单元）。同一个错误码可能多个比特同时置位，因此翻译时会对每类别去重只报一次。

**（6）翻译与按位解析：_get_aicerror_info() / find_extra_pc()**

[aic_error_info.py:L289-L316](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py#L289-L316) —— `hexstr_to_list_bin` 把错误码转成置位比特列表，逐比特查字典取描述，并按类别去重后分发到 `_analyse_vec/ifu/mte/cube/ccu/biu_errinfo()` 六个解析器，拼出报告第 2 节"AI Core DFX Register"。

[aic_error_info.py:L356-L379](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py#L356-L379) —— 以 `_analyse_ifu_errinfo()` 为例：从 extra_info 中抽出 IFU 寄存器十六进制值，用 `get_01_from_hexstr` 按位切片——`bit[50:48]` 是错误类型（查 `SOC_ERR_INFO_DICT` 翻译），`bit[47:2]` 是出错地址（右补两个 0 得到"猜测地址"）。其余五个单元的解析器结构完全相同，只是位段定义不同。

**（7）估算出错 PC：find_extra_pc()**

[aic_error_info.py:L318-L354](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py#L318-L354) —— 错误码中藏着一个 `Error PC [9:2]` 位段，与日志中的 `current_pc` 低位拼接可以"估算"出更精确的出错指令地址，供 Step 5 反汇编后定位行号使用（消费方在 [aicore_error_parser.py:L889-L913](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L889-L913) 的 `_get_err_pc()`）。

#### 4.2.4 代码实践

**实践：脱离设备，用 Python 直接调用位映射翻译一个错误码**

1. 实践目标：验证 `AIC_ERROR_INFO_DICT` 位映射机制，理解"错误码 = 比特标志字段"。
2. 操作步骤（任意有 Python 3 的环境，无需昇腾设备）：
   ```bash
   cd src/msaicerr
   python3 - <<'EOF'
   from ms_interface.constant import Constant
   from ms_interface import utils
   # 构造一个示例错误码：bit 175（vec 奇偶校验错误）与 bit 46 同时置位
   code = (1 << 175) | (1 << 46)
   bits = utils.hexstr_to_list_bin(hex(code))
   for b in bits:
       print(b, "->", Constant.AIC_ERROR_INFO_DICT.get(b, "未收录"))
   EOF
   ```
   说明：`hexstr_to_list_bin` 是 msaicerr 真实在用的工具函数（`_get_aicerror_info` 第一步就调它），错误码取值是示例构造，非项目原有数据。
3. 需要观察的现象：输出两行，175 对应 `vec_err_parity_err...`，46 对应字典中 bit 46 的条目。
4. 预期结果：与 [aic_error_info.py:L292-L297](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py#L292-L297) 的循环逻辑一致——每个置位比特都翻译成一条描述，`_get_aicerror_info` 再按前缀去重。若本机 Python 缺少 msaicerr 依赖（如 numpy），此实践标注为待本地验证，可退化为直接 `import` constant.py 后查字典。

#### 4.2.5 小练习与答案

**练习 1**：`get_op_info()` 里 thread_id 匹配失败（dump 数据 pid 与 rts pid 不一致）时会发生什么？

**答案**：[aicore_error_parser.py:L458-L460](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L458-L460) 打印 "Dump data pid is not the same with rts pid." 并返回 `None`，`parse()` 走空结论分支。这是防止把 A 进程的 dump 数据安到 B 进程错误上的防御性设计。

**练习 2**：为什么需要 `AICORE_ERR_OCCUR_OST` 这个备用正则？

**答案**：outstanding（一次异常上报多个错误）场景下日志格式不同：`start_pc` 变成 `first pc start`/`second pc start` 两条。主正则匹配不到时才换 OST 正则（[aicore_error_parser.py:L438-L443](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L438-L443)），并且在 [L455-L456](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L455-L456) 用 `s_start_pc` 覆盖 `start_pc`——第二个错误才是需要分析的那个。

**练习 3**：`AIC_ERROR_INFO_DICT` 中键 21 也可能出现在 `_analyse_mte_errinfo` 的分支选择里（`elif err_bit == 21`），这两处对键 21 的使用是一回事吗？

**答案**：不是。字典里的 21 是"错误码第 21 比特置位 → mte_aipp 类错误"的描述键；而 [aic_error_info.py:L396-L401](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aic_error_info.py#L396-L401) 中 `err_bit == 21` 是在已知是 MTE 类错误后，再依据具体置位比特选择"错误类型子字典"（AIPP_ERR_INFO_DICT）来翻译 `mte_err_type`——一个是错误码到错误名的映射，一个是错误名到细分地址/类型表的分派。

### 4.3 芯片适配层：ascend_handler 与 soc_version 的分流作用

#### 4.3.1 概念说明

先澄清一个容易误解的点：**解析流水线本身（L0/L1/FFTS/SK 分支）是按"日志形态"分流的，不是按芯片型号分流的**。芯片型号真正介入的位置有两个：

1. **Step 10 环境验证**：需要用当前芯片的 `soc_version` 编译一个内置标杆算子（golden op）跑一遍，验证环境健康。不同芯片编译方式不同，于是有了 `AscendHandlerBase` 及其子类。
2. **soc_version 的确定链**：优先通过 DSMI 接口查真实芯片平台；查不到再从出错算子的 `.cce` 文件头部注释里正则提取；再兜底默认 `Ascend910B2`。这个版本字符串随后决定选中哪个 handler。

当前仓库内的 handler 非常薄——`Ascend91096Handler` 与 `Ascend950Handler` 各只有一个类属性 `handle_chip_pre`（芯片前缀），其余能力全部继承自基类。950 目录下额外带了专属的 `compile_op.py`（CompileOP 编译封装）与 `ascend_c_template.py`（Ascend C 算子模板），说明 950 的标杆算子编译走独立实现。

与 asys 的对比（承接 u2-l4）：asys 的芯片分发是**构建期模板生成** `chip_handler.py`；msaicerr 则是**运行期目录扫描动态 import**——`utils.load_ascend_handlers()` 扫描 `ms_interface/` 下所有 `ascend*` 目录中的 `ascend*handler.py` 文件并加载。新增芯片只需加目录，中心代码零改动，两者哲学一致但实现时机不同。

#### 4.3.2 核心流程

```text
Step 10 run_test_env(soc_version, device_id)
└── get_soc_version(cce_file)                     # 确定芯片版本字符串
    ├── 首选：DSMIInterface().get_chip_info(0).get_complete_platform()
    └── 兜底：get_soc_version_from_cce(cce_file)  # 从 .cce 注释提取 // ..."AscendXXX"
        └── 再兜底：默认 "Ascend910B2"
└── golden_op.py 编译标杆算子
    └── compile_file.get_compile_file(soc_version, temp_dir)
        ├── utils.load_ascend_handlers()          # 扫描 ascend* 目录动态加载 handler
        ├── 逐个 handler.is_chip_handler(soc_version)  # 前缀 startswith 匹配
        ├── 命中 → handler.get_compile_file(...)  # 芯片专属编译（如 950 的 CompileOP）
        └── 全不命中 → get_compile_from_tik(...)  # 通用 TIK 编译兜底
```

#### 4.3.3 源码精读

**（1）soc_version 确定链**

[aicore_error_parser.py:L1336-L1346](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1336-L1346) —— `get_soc_version()`：先经 DSMI 查真实平台，异常则回落到从 cce 文件提取。

[aicore_error_parser.py:L1231-L1248](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L1231-L1248) —— `get_soc_version_from_cce()`：正则 `//.*?(Ascend.*?)"` 从 cce 源文件注释中抓 `AscendXXX`；`Ascend310B` 会被归一成 `Ascend310B1`；彻底失败则返回默认 `Ascend910B2`（只告警不失败——又一次"失败退化"）。

**（2）handler 基类：AscendHandlerBase**

[ascend_handler.py:L26-L74](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend_handler.py#L26-L74) —— 基类只提供三样东西：类属性 `handle_chip_pre`（默认空串）；`get_compile_file()` 用 `CompileOP` 编译内置算子（把 `handle_chip_pre` 传给编译器做芯片定制）；`is_chip_handler(soc_version)` 用 `soc_version.startswith(self.handle_chip_pre)` 做前缀匹配。`run_dirty_ub()`（L30-L63）是脏 UB 检测场景用的算子编译+运行流程，同样是"按芯片前缀参数化"的消费者。

**（3）两款具体 handler**

[ascend91096_handler.py:L21-L23](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend910_96/ascend91096_handler.py#L21-L23) 与 [ascend950_handler.py:L21-L23](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend950/ascend950_handler.py#L21-L23) —— 两个子类目前都只声明 `handle_chip_pre = "Ascend910_96"` / `"Ascend950"`，无任何方法覆写。差异落在 950 目录自带的 `compile_op.py`/`ascend_c_template.py`（基类 `get_compile_file` import 的 `ms_interface.ascend950.compile_op` 只有 950 实现，见 [ascend_handler.py:L20](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/ascend_handler.py#L20)——注意这是模块级固定 import，910_96 场景同样使用该实现，属于当前实现的一个耦合点）。

**（4）handler 自动发现：load_ascend_handlers()**

[utils.py:L433-L449](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L433-L449) —— 扫描 `ms_interface/` 下所有以 `ascend` 开头的子目录，加载其中 `ascend*handler.py` 文件为 `ms_interface.<目录>.<模块名>`，收集 handler 类列表。这是纯运行期插件发现，无构建期步骤。

**（5）分发点：compile_file.get_compile_file()**

[compile_file.py:L67-L73](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/compile_file.py#L67-L73) —— 加载全部 handler，逐个前缀匹配 `soc_version`，命中则用该 handler 编译标杆算子；全部不命中回落到 `get_compile_from_tik()`（基于 tbe/tik 的通用编译，[L25-L64](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/compile_file.py#L25-L64)，CANN 环境缺 tbe 时同样只告警返回空）。`run_dirty_ub.py` 第 72 行有一处同构的 handler 分发。

#### 4.3.4 代码实践

**实践：打印当前已注册的芯片 handler 清单**

1. 实践目标：直观验证 handler 的"目录扫描 + 动态加载"机制。
2. 操作步骤（无需昇腾设备）：
   ```bash
   cd src/msaicerr
   python3 - <<'EOF'
   from ms_interface import utils
   for h in utils.load_ascend_handlers():
       print(h.__name__, "-> 前缀:", h.handle_chip_pre)
   # 再验证前缀匹配逻辑（示例调用）
   handler = utils.load_ascend_handlers()[0]
   print(handler.is_chip_handler("Ascend910_96") if handler.handle_chip_pre == "Ascend910_96"
         else handler.is_chip_handler("Ascend950xxx"))
   EOF
   ```
3. 需要观察的现象：输出 `Ascend91096Handler -> 前缀: Ascend910_96` 与 `Ascend950Handler -> 前缀: Ascend950` 两行（顺序取决于目录扫描）。
4. 预期结果：即使不接任何 NPU，`load_ascend_handlers()` 也能成功加载（它只做文件系统扫描与 import）。若本机缺 numpy 等依赖导致 import 失败，标注待本地验证，可退化为纯阅读：对照 [utils.py:L439-L446](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/utils.py#L439-L446) 说出扫描条件（目录以 `ascend` 开头 + 文件以 `ascend` 开头且以 `handler.py` 结尾）。

#### 4.3.5 小练习与答案

**练习 1**：如果要支持一款新芯片 Ascend960，最少需要改哪些文件？

**答案**：新建目录 `ms_interface/ascend960/ascend960_handler.py`，内容照抄现有 handler，只改 `handle_chip_pre = "Ascend960"`。`load_ascend_handlers()` 会自动发现它，`compile_file.get_compile_file()` 自动分发——无需修改任何中心代码。若新芯片标杆算子编译方式特殊，再在该目录下放专属 `compile_op.py`（参照 ascend950 目录）。

**练习 2**：`is_chip_handler` 用 `startswith` 前缀匹配而不是全等比较，有什么好处和风险？

**答案**：好处是 `Ascend950` 前缀可以覆盖 `Ascend950A`、`Ascend950Pro` 等派生型号命名，与 asys 芯片适配层"关键字子串匹配"的思路一致；风险是若两款芯片前缀互为前缀（如 `Ascend910` 与 `Ascend910_96`），匹配结果取决于 handler 列表顺序，可能分错。当前仓库两款前缀（`Ascend910_96`、`Ascend950`）互不为前缀，暂无此问题。

**练习 3**：为什么 soc_version 的确定要有 DSMI → cce 文件 → 默认值 三级兜底？

**答案**：解析对象是离线报告目录，不一定在有该芯片的机器上执行：DSMI 查询需要本机装了驱动且设备在位；cce 文件是报告里自带的（`collection/compile/<kernel>.cce`），离线也可解析；两者都失败时给出默认值 `Ascend910B2` 只打告警，保证 Step 10 不因版本未知而崩溃——三级兜底分别覆盖"在线本机芯片 / 离线报告芯片 / 完全未知"三种场景。

## 5. 综合实践

**构造最小假报告目录，反推 `-p` 模式对输入的要求**（本讲规格指定的实践任务）：

1. **实践目标**：通过"喂一个不完整的报告目录"观察解析器卡在哪一步，从而反推 `AicoreErrorParser` 对输入布局的完整要求。

2. **操作步骤**：

   a. 构造目录（文件名布局依据源码反推，内容均为示例构造，非项目原有数据）：

   ```bash
   mkdir -p /tmp/fake_report/plog
   # 依据 AICORE_ERR_OCCUR 正则反推的"最小合法"错误日志行
   cat > /tmp/fake_report/plog/plog-8_1.log <<'EOF'
   2026-08-14-10:00:00.123.456 ERROR runtime (pid:4321) ...
   EOF
   ```

   注意：真实的 `collection/` 布局（plog、compile、dump、graph 子目录）是由 `Collection` 阶段从 `-p` 指向的原始报告目录归档生成的，所以这里喂给 `-p` 的是"原始报告目录"，不必手工摆 `collection/`。

   b. 执行（需已 source CANN 环境变量）：

   ```bash
   cd src/msaicerr
   python3 msaicerr.py -p /tmp/fake_report -out /tmp/fake_out -dev 0
   echo "exit_code=$?"
   ls -R /tmp/fake_out 2>/dev/null
   ```

3. **需要观察并记录的内容**：
   - 退出码是多少？对应 `Constant.MS_AICERR_*` 中的哪一个（查 [constant.py:L40-L60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/constant.py#L40-L60)）？
   - 终端告警/错误日志停在哪一步（Collection 的某一步？还是 parse 的 Step 1 `dump exception to file` 找不到？）；
   - `/tmp/fake_out/info_<时间戳>/` 下生成了哪些目录和文件（是否只有 `aicerror/info.txt` 空结论）。

4. **预期结果**（按源码推演，具体现象待本地验证）：
   - **无昇腾设备的环境**：入口 `check_device_valid(args.device_id)`（[msaicerr.py:L85-L87](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/msaicerr.py#L85-L87)）依赖 DSMI 查设备数，会先失败并返回 `MS_AICERR_INVALID_PARAM_ERROR`(1)——这本身就是第一个"输入要求"：解析前必须能访问到设备。
   - **有设备的环境**：假 plog 里没有 `dump exception to file` 打印，`get_dump_data_info()` 抛异常 → `get_op_info()` 返回 `None` → 生成 `aicerror/info.txt`（内容为 `get_conclusion()` 的兜底结论"信息不足或格式不正确"）并返回 8。随后可逐项向假日志里补内容再重跑：补一条匹配 `AICORE_ERR_OCCUR` 的 `error info:` 行、补 `Aicore kernel execute failed` 行……每补一项，解析就前进一步——这正是"反推输入要求"的过程。

5. **纸面替代方案**（无任何环境时）：对照 [aicore_error_parser.py:L198-L249](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L198-L249) 与 [L345-L374](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/ms_interface/aicore_error_parser.py#L345-L374)，列出 plog 中必须存在的 4 类打印关键字（`error info:`、`dump exception to file`、`Aicore kernel execute failed|AI Core kernel execution failed`、`RUNTIME ... blockDim=`）及其作用，写成一张表。

## 6. 本讲小结

- `AicoreErrorParser.parse()` 是一条 11 步流水线：只有 Step 1（提取错误码/PC/算子名）是成败关键，Step 3~10（图信息、args 对比、反汇编、memset 检查、dump 解析、二级指针、单算子复现、环境验证）全部"尽力而为"，单项失败不中断。
- 解析开始先用 `check_plog_info()` 探测三个模式开关：`parse_level`（L0/L1 日志形态）、`ffts_flag`、`is_sk`，后续步骤按开关分流——分流依据是日志形态而非芯片型号。
- 信息提取的统一范式是 `utils.get_inquire_result()`："执行 grep 命令 + 命名分组正则捕获"；`RegexPattern.AICORE_ERR_OCCUR(_OST)` 定义了错误日志行的接口契约，dump 与错误的 `thread_id` 一致性校验防止张冠李戴。
- 错误码是比特标志字段：`AIC_ERROR_INFO_DICT` 把置位比特翻译成中文描述，六个 `_analyse_*_errinfo()` 再从 extra_info 寄存器值中按位切出错误类型与出错地址；error_code 为 0 时还有 `_get_v300_error_code()` 的三段移位合成。
- `AicErrorInfo` 是约 50 个字段的结果容器：`analyse()` 拼出 6 大节 `info.txt` 报告，`get_conclusion()` 按优先级链推断根因，`get_return_code()` 把结论映射为 `MS_AICERR_*` 退出码。
- 芯片适配层是"运行期目录扫描 + 前缀匹配"：`load_ascend_handlers()` 自动发现 `ascend*/ascend*handler.py`，`is_chip_handler()` 用 startswith 分发；芯片型号只影响 Step 10 标杆算子编译与 soc_version 确定（DSMI → cce 注释 → 默认 Ascend910B2 三级兜底）。

## 7. 下一步学习建议

下一讲（u3-l3）将进入 `-d` 模式：《Dump 文件解析：Python 与 C++/protobuf 协作》——本讲 Step 7 中一闪而过的 `DumpDataParser` 将成为主角，你会看到 `dump_data.proto` 如何定义 Dump 数据结构，以及 `dump_proto_to_json` 这个 C++ 工具如何被 Python 调用。建议提前浏览 [src/msaicerr/proto_parse/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msaicerr/proto_parse) 目录与 [docs/zh/msaicerr/Dump_files_parsing.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/msaicerr/Dump_files_parsing.md)。如果想先横向对比芯片适配机制，可回看 u2-l4 中 asys 的构建期模板方案，与本讲的运行期扫描方案对照思考。
