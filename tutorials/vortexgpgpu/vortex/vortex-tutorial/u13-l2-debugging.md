# 调试追踪与 SimX-as-oracle

## 1. 本讲目标

Vortex 是一个同时维护 SimX 仿真器和 RTL 两套实现的「双引擎」项目，二者必须逐拍对齐（参见前置讲义 u7-l4 的 model_parity）。这种架构决定了 Vortex 的调试哲学与众不同：**当 RTL 调试陷入僵局时，不要继续戳 RTL，而是让 SimX 充当预言机（oracle），用 trace diff 把 bug 钉死在某一拍、某一条指令上**。

学完本讲，你应当能够：

- 用 `--debug=<level>` 在 SimX / RTL 上生成运行时 trace（`run.log`），并理解日志里 `TRACE` 行与 `DEBUG` 行的区别。
- 用 `ci/trace_csv.py` 把庞大的 `run.log` 压缩成一张按 UUID 排序、可直接 `diff` 的指令表（CSV）。
- 用 GDB + OpenOCD 经 RISC-V Debug Module 对设备内核做源码级单步调试。
- 用 `ci/perfetto.py` 把 trace 转成 Perfetto 可视化时间线，定位长延迟指令与流水线停顿。
- 完整复述 SimX-as-oracle 调试法的步骤、触发时机，以及「为何反过来不行」的原因。

## 2. 前置知识

本讲假设你已经读过前置讲义：

- **u5-l1 / u5-l2**：SimX 的 `SimObject`/`SimChannel`/`SimPlatform` 三基元，以及 SimX 如何用 `cycle()` 推进。
- **u6-x**：SimX 核心 6 级流水线 `Schedule → Fetch → Decode → Issue → Execute → Commit`，以及指令在流水线里以 `instr_trace_t`（带 `uuid`）流动。
- **u7-l4**：model_parity 门控——退休指令必须精确相等、周期数有容差，且「绝不能放宽容差吸收差异」。
- **u13-l1**：测试与回归流程，知道 `./ci/blackbox.sh` 的 `--driver` / `--app` / `--debug` / `--log` 等旋钮。

几个本讲会用到的关键术语：

- **trace**：程序运行时按周期打印的状态日志（`run.log`），含译码后的指令、寄存器值、流水线阶段时间戳等。
- **UUID**：每条指令实例的全局唯一编号，由调度器在 `schedule` 阶段分配。它是连接 SimX 与 RTL、连接 trace 行与 CSV 行的「主键」。
- **oracle（预言机）**：在两种实现里被无条件信任为正确的那一方。Vortex 选 SimX 当 oracle，因为它快、可断点、可任意插桩。
- **DTM（Debug Test Module）/ Debug Module**：符合 RISC-V External Debug 规范的片上调试模块，让 GDB 能经 OpenOCD 像调试普通 MCU 一样调试 Vortex 内核。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/debugging.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md) | 调试总览：`--debug` 分级、VCD 波形、FPGA scope、trace_csv 用法、**SimX-as-oracle 方法论**（本讲核心）。 |
| [ci/trace_csv.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py) | trace 清洗器：把 `run.log` 解析成按 UUID 排序的 CSV，是 trace-diff 的落地工具。 |
| [docs/kernel_debugging.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md) | 设备内核源码级调试：SimX `-d` 调试模式、OpenOCD、GDB 三端协作流程。 |
| [docs/perfetto_analysis.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md) | Perfetto 可视化指南：track 组织、长延迟指令定位、利用率分析。 |
| [ci/perfetto.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perfetto.py) | trace → Perfetto JSON 转换器（文档里写作 `vortex_perfetto.py`，仓库内实际文件名为 `ci/perfetto.py`）。 |
| [sim/simx/debug.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/debug.h) | SimX 的 `DP`/`DT` 打印宏，是所有 `DEBUG`/`TRACE` 行的源头。 |
| [sim/simx/main.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp) | SimX CLI 入口：`-d` 调试模式、`[VXDRV] START:` 标记、主循环。 |
| [sim/simx/core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp) | SimX 流水线：`schedule`/`fetch`/`commit` 三个 trace 钩子的发射点。 |

## 4. 核心概念与源码讲解

### 4.1 调试工具全景与 `--debug` 旋钮

#### 4.1.1 概念说明

Vortex 有四种执行后端（simx / rtlsim / opae / xrt，见 u1-l1），每种都能生成 trace，但「看得多清楚」差别很大：

- **SimX**：C++ 写的周期近似仿真器，运行快，可打印「译码后的指令 + 寄存器值 + 流水线阶段时间戳」。最适合调试内核逻辑。
- **RTL（rtlsim/opae）**：Verilator 或商业仿真器跑 Verilog，可生成 VCD 波形（`trace.vcd`），信号最全但运行慢、日志最难读。
- **FPGA**：板上运行，只能用专用 scope 抓有限信号。

`--debug=<level>` 是 `ci/blackbox.sh` 暴露的统一旋钮，它最终把 `DEBUG_LEVEL` 翻译成构造期宏 `-DVX_DBG_DEBUG_LEVEL=<level>`（参见 u2-l2 的配置值流）。level 越大，打印越啰嗦；文档建议 diff trace 时用 `--debug=3`。

> ⚠️ 文档明确提示：blackbox 在硬件配置与上次相同时**不会**重建驱动。改了源码后必须加 `--rebuild=1`，否则你 trace 到的还是旧二进制（参见 [docs/debugging.md:L3-L9](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L3-L9)）。

#### 4.1.2 核心流程

生成一条可被 trace_csv 消费的 SimX trace：

```
ci/blackbox.sh --driver=simx --app=demo --debug=3 --log=run_simx.log
        │
        ├─ DEBUG_LEVEL=3 注入 simx 编译 → DP/DT 宏阈值=3
        ├─ 启动 simx，打印 "CONFIGS: ..." 头部 + "[VXDRV] START: ..." 标记
        ├─ 每周期 DP/DT 宏按 level 决定是否打印 DEBUG/TRACE 行 → run_simx.log
        └─ 程序结束，刷缓存、读退出码
```

`--debug` 在四种后端的产出对照：

| 后端 | 启动命令 | 产出 |
| --- | --- | --- |
| SimX | `--driver=simx --debug=3` | `run.log`（文本 trace） |
| RTL | `--driver=rtlsim --debug=1` | `run.log` + `trace.vcd` 波形 |
| RTL 全量波形 | `CONFIGS="-DTRACING_ALL" --driver=rtlsim --debug=1` | 含 `/libs/` 内部信号的 `trace.vcd` |
| FPGA | `--driver=fpga --scope` | 限定于 `hw/scripts/scope.json` 信号的 `trace.vcd` |

#### 4.1.3 源码精读

**打印宏（trace 的源头）。** [sim/simx/debug.h:L31-L65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/debug.h#L31-L65) 定义了两族宏：`DP`（打 `DEBUG ` 前缀，用于指令译码、寄存器值等**语义**信息）和 `DT`（打 `TRACE <周期>: ` 前缀，用于流水线**时序**信息）。关键门槛是这一行：

```cpp
if ((lvl) <= VX_DBG_DEBUG_LEVEL) { std::cout ... }
```

只有 `lvl <= VX_DBG_DEBUG_LEVEL` 时才打印。`VX_DBG_DEBUG_LEVEL` 默认是 3（见 [debug.h:L16-L18](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/debug.h#L16-L18)），`--debug=N` 把它覆盖成 N。所以 `--debug=1` 只剩 `lvl<=1` 的行（很安静），`--debug=3` 全开。另一处要点：在 `NDEBUG` 下这些宏全部变空操作（[debug.h:L70-L76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/debug.h#L70-L76)），所以 release 构建里完全不出 trace。

**`[VXDRV] START:` 标记。** trace_csv 与 perfetto 都靠这行定位「程序真正开始」。它由 SimX 入口在加载镜像后、进主循环前打印：[sim/simx/main.cpp:L151](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L151)（RTL 侧对应 [sim/rtlsim/main.cpp:L124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/rtlsim/main.cpp#L124)）：

```cpp
std::cout << "[VXDRV] START: program=" << program << std::endl;
```

**流水线三阶段的 trace 钩子。** SimX 在 `schedule`、`commit` 各打一条 `DT(3, ...)`，分别对应一条指令的「出生」与「退休」时间戳，这正是 trace_csv 计算延迟的依据：

- 调度发射：[sim/simx/core.cpp:L332](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L332) — `DT(3, ... << "-pipeline schedule: " << *trace)`，记录 `schedule` 时间戳。
- 取指完成：[sim/simx/core.cpp:L375-L379](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L375-L379) — `DP(1, "Fetch: ... (#" << trace->uuid << ")")`，把 UUID 显式附在行尾。
- 退休：[sim/simx/core.cpp:L723](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L723) — `DT(3, ... << "-pipeline commit: " << *trace)`，记录 `commit` 时间戳。

#### 4.1.4 代码实践

1. **实践目标**：亲手生成一份带语义信息的 SimX trace，并对比不同 debug level 的噪声量。
2. **操作步骤**：
   ```bash
   cd <你的 build 目录>
   # 安静档：只剩 lvl<=1 的行
   ./ci/blackbox.sh --driver=simx --app=demo --debug=1 --log=run_d1.log
   # 啰嗦档：全开，便于后续 trace_csv
   ./ci/blackbox.sh --driver=simx --app=demo --debug=3 --log=run_d3.log
   wc -l run_d1.log run_d3.log
   ```
3. **观察现象**：`run_d3.log` 行数远多于 `run_d1.log`；开头应有 `CONFIGS:` 头部与 `[VXDRV] START: program=...`；正文里能搜到 `TRACE ... -pipeline schedule:`、`DEBUG Instr:` 等行。
4. **预期结果**：两份日志都应能看到程序正常结束、退出码为 0；`d3` 的每条指令都带 `(#<uuid>)` 后缀。
5. 若运行环境无工具链，本步骤标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--debug=0` 之后日志几乎空了，但程序仍能正常跑完？
**答案**：`--debug=0` 把 `VX_DBG_DEBUG_LEVEL` 设成 0，所有 `lvl>=1` 的 `DP/DT` 宏都不打印；但宏只是 `std::cout` 的开关，不影响仿真逻辑，所以程序行为不变（见 [debug.h:L31-L35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/debug.h#L31-L35)）。

**练习 2**：改了 `hw/rtl/core/VX_core.sv` 后跑 rtlsim，trace 里却还是旧行为，最可能的原因是什么？
**答案**：blackbox 在硬件配置未变时不重建驱动；需要加 `--rebuild=1` 强制重建（[docs/debugging.md:L3-L9](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L3-L9)）。

### 4.2 trace_csv.py：把巨量 trace 压成可比较的指令表

#### 4.2.1 概念说明

`--debug=3` 生成的 `run.log` 动辄几百万行，逐行阅读不现实。`ci/trace_csv.py` 是一台「trace 清洗机」，它做三件事：

1. 从无结构的文本日志里，按 UUID 把分散在多行的同一指令信息（schedule 时间戳、源/目的寄存器值、commit 时间戳）**归并**成一行。
2. 把每条指令的关键字段写成统一的 CSV 列：`uuid, PC, opcode, core_id, warp_id, tmask, destination, operands`。
3. **按 UUID 排序**输出——这是关键，使得 SimX 与 RTL 两份 CSV 可以直接 `diff`，第一条不一致行就是 bug。

它的设计哲学直接服务于 model_parity：UUID 是 SimX 与 RTL 共享的「指令主键」，只要两侧给同一条指令分配相同的 UUID（同一调度序），diff 就有定义。

#### 4.2.2 核心流程

trace_csv 的主流程（[ci/trace_csv.py:L582-L588](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L582-L588)）：

```
main()
 ├─ load_config(log)          # 从日志头部正则解析 CONFIGS: 拿到 num_threads/socket_size 等
 ├─ split_log_file(log)       # 按每个 "[VXDRV] START" 把多 launch 日志切成子段
 └─ write_csv(sublogs, csv, type)
        ├─ type=simx  → parse_simx(sublog)    # 解析 "TRACE <cyc>: ... schedule/commit" + "DEBUG Instr/Src/Dest"
        ├─ type=rtlsim→ parse_rtlsim(sublog)  # 解析 "cluster#-socket#-core#-<module> <action>" 行
        ├─ entries.sort(key=uuid)             # ★ 按 UUID 排序
        └─ csv.DictWriter 写出 8 列
```

UUID 的生命周期（以 `parse_simx` 为例，[ci/trace_csv.py:L76-L271](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L76-L271)）：

- `schedule` 行 → 用 UUID 建条目，记 PC/wid/tmask，记 `schd_ticks[uuid]`。
- `operands` 行 → 记 `op_ticks[uuid]`，并算出 Schedule 延迟 = operands 时间戳 − schedule 时间戳。
- `commit` 行 → 算出 Execute 延迟，把累积的源/目的寄存器字符串收尾，**append 到 entries 并删除 `instr_data[uuid]`**。

注意 `parse_simx` 用 `instr_data` 字典按 UUID 聚合，而不是「最近一条指令」——因为新版流水线 SimX 里，不同 uop 的 Src/Dest 行会交错，必须靠行尾的 `(#uuid)` 标签归位（见源码注释 [trace_csv.py:L84-L88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L84-L88)）。这正好对应前置讲义 u6-l3 讲过的「分包写回、按 num_pkts 释放」的流水线行为。

#### 4.2.3 源码精读

**UUID 排序——trace diff 成立的前提。** [ci/trace_csv.py:L546](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L546)：

```python
entries.sort(key=lambda x: (int(x['uuid'])))
```

注释说明（[trace_csv.py:L72-L74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L72-L74)）：第一列是 UUID，内容按 UUID 排序，你可以用它追踪同一条指令在 RTL 或 SimX 上的执行——这正是用 SimX 调试 RTL 的核心手段。

**SimX 端 DEBUG 行的归并。** `parse_simx` 识别三种 DEBUG 行（[trace_csv.py:L83-L91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L83-L91)）：

```python
debug_instr_pattern = r"DEBUG Instr:\s+([a-zA-Z0-9_\.]+),?\s+.*#(\d+)"
debug_src_pattern   = r"DEBUG Src\d+ Reg:\s+(.+?)\s*\(#(\d+)\)\s*$"
debug_dest_pattern  = r"DEBUG Dest Reg:\s+(.+?)\s*\(#(\d+)\)\s*$"
```

这三行分别对应 SimX 在 `core.cpp` 的 `DP(1, "Instr: ...")`（[core.cpp:L461](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L461)）、`operands.cpp` 的 `log_src_operand`（[operands.cpp:L36-L62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/operands.cpp#L36-L62)）、`opc_unit.cpp` 的 writeback（[opc_unit.cpp:L151-L163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/opc_unit.cpp#L151-L163)）打出的内容。trace_csv 与 SimX 源码是「生产者—消费者」契约关系：SimX 改了打印格式，trace_csv 的正则就得跟着改。

**RTL 端解析。** `parse_rtlsim`（[trace_csv.py:L335-L525](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L335-L525)）识别 `cluster#-socket#-core#-<module>` 拓扑前缀，按 `module+action` 判定四个阶段（schedule/decode/dispatch/commit，[trace_csv.py:L389-L392](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L389-L392)），并把 RTL 的 `core_id` 由 (cluster,socket,core) 重建（[trace_csv.py:L422](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L422)）。两侧最终落到**相同的 8 列 CSV**，所以可直接 diff。

**附带产出：延迟统计。** 解析过程中三个 `PerfCounter`（Schedule/Issue/Execute）会按 UUID 累计每条指令的各阶段周期数，最后打印 avg/min/max 及对应的 UUID（[trace_csv.py:L50-L55](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L50-L55)）。即便不做 diff，这也是快速定位「哪条指令最慢」的捷径。

#### 4.2.4 代码实践

1. **实践目标**：用 trace_csv 把 demo 程序的 SimX trace 转成 CSV，并定位某条具体指令的执行记录。
2. **操作步骤**：
   ```bash
   # 1) 生成 trace
   ./ci/blackbox.sh --driver=simx --app=demo --debug=3 --log=run_simx.log
   # 2) 清洗成 CSV
   ./ci/trace_csv.py -t simx run_simx.log -o trace_simx.csv
   # 3) 看前几行（含表头）
   head -5 trace_simx.csv
   # 4) 任取一个 uuid，例如 3，定位它的完整记录
   grep '^3,' trace_simx.csv
   ```
3. **观察现象**：终端会先打印三行 `Schedule/Issue/Execute latency: avg=... min=... (#<uuid>) max=... (#<uuid>)`；CSV 第一列为 uuid，已升序排列；`grep '^3,'` 能命中一条带 PC/opcode/tmask/destination/operands 的完整行。
4. **预期结果**：你能读出 uuid=3 这条指令的 PC、opcode、它读了哪些寄存器（operands）、写了哪个寄存器（destination）。把它与 `run_simx.log` 里 `(#3)` 的原始行对照，应能逐字对应。
5. 若无法本地运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 trace_csv 要按 UUID 排序，而不是按日志出现顺序（时间戳）输出？
**答案**：因为 SimX 与 RTL 是两套独立实现，同一条指令在两侧的打印顺序可能因流水线差异而不同；但两侧为同一条指令分配的 UUID 一致。按 UUID 排序后，两侧 CSV 的第 N 行描述同一条指令，`diff` 的输出才有意义（[trace_csv.py:L72-L74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L72-L74)）。

**练习 2**：`parse_simx` 为什么不用「最近一条 `DEBUG Instr` 行」来归属 `Src/Dest` 行，而要每行带 `(#uuid)`？
**答案**：新版流水线 SimX 中，多个 uop 的 Src/Dest 打印会交错（一条指令的操作数收集与另一条的写回混在一起），按「最近指令」归位会张冠李戴；行尾 `(#uuid)` 才是可靠主键（[trace_csv.py:L84-L88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L84-L88)）。

### 4.3 SimX-as-oracle：用 trace diff 锁定 RTL bug

#### 4.3.1 概念说明

这是 Vortex 调试方法论里**最重要、也最反直觉**的一招，原文见 [docs/debugging.md:L76-L93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L76-L93)。

场景：你在 RTL 上遇到一个 bug——稀疏模式的数值错误、流水线竞争、那种「rtlsim 跑出来结果接近但不对、且失败无法定位到单个模块」的问题。直觉是继续在 RTL 里加 `$display`、改信号、重跑。文档的建议恰恰相反：**停下戳 RTL，切换到 SimX-as-oracle 策略**。

核心思想：SimX 比 rtlsim 快几个数量级、可用普通 C++ 调试器断点、可任意插桩；而 rtlsim 开箱只给你输出值和断言失败。所以应当把 SimX 当作「可信参照系」，让 RTL 去对齐 SimX，而不是反过来。

#### 4.3.2 核心流程

文档给出了完整的方法论，可归纳为「一个前提 + 三个操作步」。前提是：**当 RTL 架构本身是新的，先让 SimX 镜像 RTL 的真实结构**。三个操作步构成 trace-diff 闭环：

```
前提（仅当 RTL 引入了 SimX 还没有的新架构）：
  扩展 SimX C++ 模型，使其镜像 RTL 的真实结构管线：
  相同的 FU 边界、相同的 uop 展开、相同的 SRAM 布局、
  相同的元数据流、相同共享资源的 client-port 形状。
  （SimX 语义应追踪 RTL，而非陈旧参考实现。）

操作闭环：
  ① 让 SimX 先通过那个失败的测试
       —— SimX 快、可断点，修 SimX 比修 rtlsim 便宜得多。
  ② 两侧加匹配的 trace dump
       —— 用同一份 CSV 格式（cycle/UUID/指令/FU dispatch/
          SRAM 读写地址+数据/scoreboard 冒险），以 trace_csv 的
          UUID 排序格式为起点，按需加模块专属列。
  ③ diff 两侧 trace
       —— `diff trace_simx.csv trace_rtlsim.csv`，
          第一处分歧就是 bug。
```

诊断目标的转变是这套方法的精髓：

- 传统 RTL 调试：你只看到 `actual=9.69 vs expected=10.33`，从结果反推原因，靠猜。
- trace-diff：你直接读到「UUID=#1234、第 5678 拍，RTL 在此处与 oracle 分歧」——精确到指令与周期。

**何时触发**（[docs/debugging.md:L88-L92](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L88-L92)）：

- 数值错误在多次 RTL 试探性修改后依旧存在。
- bug 跨越不止一个模块（单元测试抓不到）。
- 你正忍不住到处撒 `$display`——这正是该转投 trace-diff 闭环的信号。

**为何不能反过来（RTL-as-oracle）**（[docs/debugging.md:L93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L93)）：rtlsim 开箱给你的只有输出值和断言失败；SimX 进程内运行、接受任意插桩、可在用户设的断点处停下。所以 SimX 才是杠杆点。

#### 4.3.3 源码精读

SimX-as-oracle 不绑定某个具体源文件，而是 Vortex 工程纪律的总纲。它在仓库里有两处权威表述，互为印证：

1. **方法论本体**：[docs/debugging.md:L76-L93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L76-L93)，给出完整的「前提 + 三步 + 触发时机 + 为何不反过来」论述。
2. **配套工具链**：trace_csv 的 UUID 排序设计（[trace_csv.py:L546](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/trace_csv.py#L546)）正是步骤 ②③ 的物理基础——没有「同 UUID 同行」，diff 就无从谈起。

这套方法与前置讲义 u7-l4 的 model_parity 是一体两面：model_parity 是「平时保持两侧一致」的**纪律**，SimX-as-oracle 是「一旦不一致」的**排障流程**。两者共享同一条底线——绝不能用「放宽容差」来吸收差异。

#### 4.3.4 代码实践

1. **实践目标**：在 demo 这种「已知正确」的程序上，dry-run 一遍 SimX-as-oracle 的 trace-diff 闭环，体会「第一处分歧即 bug」。
2. **操作步骤**：
   ```bash
   # ① 两侧各跑一遍，同 app 同 configs，都用 debug=3
   ./ci/blackbox.sh --driver=rtlsim --app=demo --debug=3 --log=run_rtlsim.log
   ./ci/blackbox.sh --driver=simx   --app=demo --debug=3 --log=run_simx.log
   # ② 两侧分别清洗成 UUID 排序的 CSV
   ./ci/trace_csv.py -t rtlsim run_rtlsim.log -o trace_rtlsim.csv
   ./ci/trace_csv.py -t simx   run_simx.log   -o trace_simx.csv
   # ③ diff
   diff trace_rtlsim.csv trace_simx.csv | head -40
   ```
3. **观察现象**：对一个稳定的 demo，理想情况下两份 CSV 的指令序列高度一致（数值字段可能因 lane 顺序表示而有少量格式差异）；`diff` 输出里第一处「opcode/operands/destination」的实质分歧，就是模拟的「bug 锚点」。现实中当 RTL 真有 bug 时，这一行会直接告诉你 UUID 与 PC。
4. **预期结果**：你能用一句话写出「分歧出现在 uuid=#X、PC=0xY」。即便 demo 无分歧，你也走通了整条流水线。
5. 说明 SimX-as-oracle 的步骤（用本节「前提 + 三步」作答）。
6. 若无 rtlsim 工具链，本实践可只跑 simx 一侧验证 trace_csv 流程，diff 部分标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么说「到处撒 `$display`」是切换到 trace-diff 的信号？
**答案**：`$display` 是无结构、无法跨实现对齐的临时手段；当你需要撒很多个，说明 bug 跨模块、定位不到单点——正是 trace_csv + UUID diff 的目标场景（[docs/debugging.md:L91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debugging.md#L91)）。

**练习 2**：步骤「前提」何时才需要执行？能不能跳过？
**答案**：仅当 RTL 引入了 SimX 尚未镜像的新架构（新 FU、新 uop 展开、新 SRAM 布局等）时才需要；如果两侧结构本就对齐（model_parity 平时保持），可直接从「让 SimX 先过测试」开始。跳过前提的前提是 SimX 已经是 RTL 当前结构的忠实模型。

### 4.4 源码级 kernel 调试：GDB + OpenOCD + Perfetto 可视化

#### 4.4.1 概念说明

trace diff 解决的是「RTL 与 SimX 对不齐」的硬件侧问题；但有时 bug 出在**设备内核（kernel.cpp）本身**——逻辑写错了，这时你需要像调试普通 CPU 程序一样单步、看变量。Vortex 在 SimX 里内置了符合 RISC-V External Debug 规范的 Debug Module（DTM），让 GDB 经 OpenOCD 接入，实现源码级调试。

调试链路是三端协作：

```
riscv64-unknown-elf-gdb  ──(TCP 3333)──  OpenOCD  ──(Remote Bitbang, 端口 9824)──  simx -d（内含 Debug Module）
```

- **simx `-d`**：以调试模式启动，加载程序后**挂起（halt）**，等待调试器接管，并开放 Remote Bitbang 端口。
- **OpenOCD**：把 GDB 的 RMI（Remote Serial Protocol）翻译成 RISC-V Debug Module 的 Remote Bitbang 信号。
- **GDB**：加载带符号的 ELF（`fibonacci.elf`），下断点、单步、查寄存器。

另外，当你想看的不是「单条指令」而是「整段执行的性能形态」（哪条指令慢、warp 是不是在停顿），文本 trace 又不够直观时，就轮到 Perfetto：`ci/perfetto.py` 把 `run.log` 转成 Chrome Trace JSON，在浏览器里看到每条指令的生命周期切片、warp 状态计数器、各级 cache 事件。

#### 4.4.2 核心流程

**GDB 调试流程**（[docs/kernel_debugging.md:L70-L109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md#L70-L109)）：

```
终端1:  simx -d [-p 9824] [-V] <prog.bin>      # 启动并挂起
终端2:  openocd -f vortex.cfg                   # 连接 simx，开放 3333 给 GDB
终端3:  riscv64-unknown-elf-gdb <prog.elf>
        (gdb) target remote localhost:3333
        (gdb) monitor reset halt
        (gdb) set $pc = 0x80000000
        (gdb) break main
        (gdb) continue
```

**关键约束**：SimX、kernel 库（`libvortex.a`）、测试二进制三者的 `XLEN` 必须一致（都用 64 或都用 32），否则链接报错或运行时崩溃（[docs/kernel_debugging.md:L63-L68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md#L63-L68)）。64 位构建会同时打开双精度浮点（EXT_D）。

**Perfetto 流程**（[docs/perfetto_analysis.md:L33-L52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L33-L52)）：

```
run.log ──ci/perfetto.py -t {simx|rtlsim} [-c] [--cycle-min/max] [--values]──> *.json(.gz)
                                                                            │
                                       上传到 Perfetto UI / VS Code 扩展 ◄────┘
```

Perfetto 里的关键 track（[docs/perfetto_analysis.md:L107-L138](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L107-L138)）：

- **warp 指令生命周期**：每条指令按 UUID 画成一段 async slice，从首次观测阶段到 commit；同一 warp track 上还有 `schedule/decode/dispatch/execute/commit` 即时阶段标记。
- **warp 状态计数器**：`active` / `stalled` / `active_threads`，一眼看出是「没活干」还是「在等待」。
- **cache/内存事件**：`dcache:miss`、`l2:hit`、`mem:req` 等，按 icache/dcache/l2/l3/mem 分 track。

#### 4.4.3 源码精读

**SimX 调试模式入口。** [sim/simx/main.cpp:L49-L77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L49-L77) 解析 `-d`（debug mode）、`-p`（Remote Bitbang 端口，默认 9823）、`-V`（Debug Module 冗长日志）：

```cpp
while ((c = getopt(argc, argv, "shdp:V")) != -1) {
  ...
  case 'd': debug_mode = true; ...
  case 'p': rbb_port = ...;
  case 'V': debug_verbose = true;
}
```

调试分支与普通分支的分歧在 [main.cpp:L154-L214](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L154-L214)：调试模式下不走普通 `while (processor.cycle())`，而是进入一个由 Debug Module 驱动的会话——只在 hart 未被 halt 时推进仿真，并在程序自然结束时通知 Debug Module（`dm.notify_program_completed`），让调试器仍可继续检查状态。

**SimX CLI 旋钮一览。** [docs/kernel_debugging.md:L136-L145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md#L136-L145) 列出：`-d`（调试）、`-p`（端口）、`-V`（冗长）、`-c/-w/-t`（核数/warp/线程）。

**Perfetto 转换器 CLI。** [ci/perfetto.py:L1128-L1144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perfetto.py#L1128-L1144) 定义了与文档一致的选项：`-t/--type`、`--values {none,dest,all}`、`--freq-mhz/--cycle-ns`（把周期映射到真实时间）、`--cycle-min/--cycle-max`（只导出某周期窗口，对大日志至关重要）、`--no-vxdrv-start`（日志缺 `[VXDRV] START:` 时强制解析）、`--parent-flow`（画 parent→uop 流箭头）。它用 `VXDRV_START_RE`（[perfetto.py:L140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perfetto.py#L140)）门控 RTL 日志的解析起点。

> 📌 文档 [docs/perfetto_analysis.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md) 把脚本称作 `vortex_perfetto.py`，但仓库内的实际文件是 `ci/perfetto.py`；命令请以 `ci/perfetto.py` 为准。

#### 4.4.4 代码实践（源码阅读型 + 可选实操）

1. **实践目标**：走通一次三端 GDB 调试会话；并把一份 trace 转成 Perfetto 可视化。
2. **操作步骤**（GDB，需先按 [docs/kernel_debugging.md:L19-L39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md#L19-L39) 用 `XLEN=64` 构建 simx/kernel/tests）：
   ```bash
   # 终端1
   ./build/sim/simx/simx -d build/tests/kernel/fibonacci/fibonacci.bin
   # 终端2
   openocd -f vortex.cfg
   # 终端3
   riscv64-unknown-elf-gdb build/tests/kernel/fibonacci/fibonacci.elf
   (gdb) target remote localhost:3333
   (gdb) monitor reset halt
   (gdb) break fibonacci
   (gdb) continue
   (gdb) info registers
   ```
   Perfetto（可选）：
   ```bash
   ./ci/blackbox.sh --driver=simx --app=demo --debug=3 --log=run.log
   python3 ci/perfetto.py run.log -t simx -c --cycle-min 0 --cycle-max 5000 -o demo.json.gz
   # 把 demo.json.gz 拖进 https://ui.perfetto.dev
   ```
3. **观察现象**：GDB 侧程序应停在 `fibonacci` 入口；`info registers` 能看到 RISC-V 整型寄存器。Perfetto 侧展开 `Vortex GPU 1`，能看到 `cluster0-socket0-core0: warp0` track 上的指令切片与 `warp0 state` 计数器。
4. **预期结果**：能用 GDB 单步过 `fibonacci` 的几条指令；能在 Perfetto 里点开某条长切片，读到它的 `uuid/op/ex/PC`。
5. 若本机无 OpenOCD/GDB 或无法联网打开 Perfetto UI，对应步骤标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果 simx 用 `XLEN=64` 构建，但测试 ELF 是 32 位的，会怎样？
**答案**：要么链接期报错，要么运行期因寄存器宽度/ABI 不匹配而失败；三者（simx、kernel 库、测试二进制）的 `XLEN` 必须一致（[docs/kernel_debugging.md:L63-L68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/kernel_debugging.md#L63-L68)）。

**练习 2**：Perfetto 的 `--cycle-min/--cycle-max` 解决什么问题？
**答案**：完整 trace 转 JSON 会非常大、难以加载；限定一个周期窗口只导出感兴趣区段，显著减小文件、加快渲染（[docs/perfetto_analysis.md:L47-L52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L47-L52)）。

## 5. 综合实践

把本讲四条线索串起来，模拟一次完整的「RTL 数值错误」排障：

**背景**：假设你改动了 RTL 的 LSU，`tests/regression/demo` 在 rtlsim 上结果偶发出错（actual 与 expected 差几个值），而在 SimX 上完全正确。

1. **生成两侧 trace**（同 app、同 configs、同 debug 级别）：
   ```bash
   ./ci/blackbox.sh --driver=rtlsim --app=demo --debug=3 --log=run_rtlsim.log
   ./ci/blackbox.sh --driver=simx   --app=demo --debug=3 --log=run_simx.log
   ```
2. **清洗成 UUID 排序的 CSV**：
   ```bash
   ./ci/trace_csv.py -t rtlsim run_rtlsim.log -o trace_rtlsim.csv
   ./ci/trace_csv.py -t simx   run_simx.log   -o trace_simx.csv
   ```
3. **执行 SimX-as-oracle 的 diff**：`diff trace_rtlsim.csv trace_simx.csv`，定位第一处实质分歧，记下 `uuid` 与 `PC`。
4. **回溯原始日志**：用记下的 uuid 在 `run_rtlsim.log` 里 `grep '(#<uuid>)'`，看这条指令的完整生命周期，确认是哪一拍、哪个字段（operands/destination）开始错。
5. **可视化佐证**（可选）：用 `ci/perfetto.py` 把 `run_rtlsim.log` 转成 Perfetto JSON，定位该 uuid 切片，看它是否卡在某个 cache miss 或 scoreboard 冒险。
6. **交付物**：写一段「bug 锚点报告」——`uuid=#X, PC=0xY, 第 Z 拍，RTL 的 <字段> 与 SimX 分歧，怀疑 <模块>`。

完成这个综合实践，你就把「`--debug` 生成 trace → trace_csv 清洗 → SimX-as-oracle diff → Perfetto 可视化」这条 Vortex 调试主链路完整跑通了一遍。

> 注：本综合实践依赖完整的 simx + rtlsim 工具链。若环境不具备，至少完成第 1–3 步的 simx 单侧，理解 diff 语义即可，rtlsim 侧标注「待本地验证」。

## 6. 本讲小结

- `--debug=<level>` 通过编译期宏 `VX_DBG_DEBUG_LEVEL` 控制 SimX 的 `DP/DT` 打印宏；level 越大越啰嗦，diff trace 用 `--debug=3`；改源码后务必 `--rebuild=1`。
- `ci/trace_csv.py` 把无结构的 `run.log` 按 UUID 归并、清洗成 8 列 CSV 并**按 UUID 排序**，使 SimX 与 RTL 两份 CSV 可直接 `diff`；UUID 是连接两侧、连接 trace 行与 CSV 行的主键。
- **SimX-as-oracle** 是 Vortex 调试的核心方法论：当 RTL 调试陷入僵局，别戳 RTL——（前提：让 SimX 镜像 RTL 结构）①让 SimX 先过测试 ②两侧加匹配 CSV dump ③diff 找首个分歧；触发信号是「bug 跨模块」或「忍不住到处撒 `$display`」。
- 设备内核逻辑错误走 GDB 三端链路：`simx -d` 挂起 → OpenOCD 翻译 → GDB 源码级单步；三者 `XLEN` 必须一致。
- `ci/perfetto.py`（文档称 `vortex_perfetto.py`）把 trace 转成 Perfetto JSON，用指令生命周期切片、warp 状态计数器、cache 事件定位性能形态问题。
- 这套方法与 model_parity（u7-l4）一体两面：model_parity 是「平时保持一致」的纪律，SimX-as-oracle 是「一旦不一致」的排障流程，共享「绝不放宽容差吸收差异」的底线。

## 7. 下一步学习建议

- **接 u13-l3（性能计数器与 roofline 分析）**：本讲的 Perfetto 是「形态可视化」，下一讲是「量化计数器」——`--perf=1` 暴露的调度器利用率、流水线停顿、指令混合、内存延迟，以及 `perf/roofline.py`。两者搭配可定位「慢在哪、为什么慢」。
- **接 u13-l4（CI 与 model_parity 门控）**：理解 trace diff 之所以可靠，是因为 CI 用退休指令精确相等 + 周期容差自动守护；本讲的 SimX-as-oracle 是 model_parity 的「人工排障版」。
- **回看 u7-l4 / u5-l3**：本讲的「SimX 当 oracle」依赖 SimX 是 RTL 的忠实模型，其根基是 u5-l3 的「基数规则」与 u7-l4 的 lockstep 纪律；若想加深「为何 SimX 可信」，可重读这两讲。
- **延伸阅读**：[docs/debug_mode.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/debug_mode.md)（调试模式硬件支持）、[RISC-V Debug Specification](https://github.com/riscv/riscv-debug-spec)、[Perfetto UI](https://ui.perfetto.dev)。
