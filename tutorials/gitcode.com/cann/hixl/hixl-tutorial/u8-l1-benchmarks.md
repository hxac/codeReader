# u8-l1 性能基准测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `benchmarks/run_all_bench.sh` 与 `benchmarks/run_all_benchmarks.py` 两个编排入口的使用方式与参数。
2. 理解两类基准的分工：`comm_benchmark`（裸 block 传输带宽）与 `kv_benchmark`（KV Cache 池化场景 put/get）。
3. 看懂带宽指标的计算口径（十进制 GB/s、block 阶梯、loops 取平均），并学会对照 `benchmarks/performance.md` 判断实测数据是否正常、定位性能瓶颈。

## 2. 前置知识

- **基准测试（benchmark）**：用受控、可重复的负载测量系统的性能指标。本仓库关注两个指标：**带宽**（单位时间搬了多少字节）与**时延**（单次操作耗时多少微秒）。
- **有效带宽（goodput/GB/s）**：本仓库的带宽列统一按**十进制**计算，即 1 GB = \(10^9\) 字节；而命令行参数里的 `16K`/`128M` 按**二进制 1024** 解析（16K = 16 KiB = 16384 字节）。两者不要混淆。
- **block size 与传输总量**：comm 基准把总传输量（默认 128 MiB）切成若干等大的 block，例如 16K 档位一次下发 128MiB/16KiB = 8192 条 `TransferOpDesc`。block 越小、单条描述符的固定开销占比越高，带宽越低——这是解读表格时最关键的直觉。
- **方向记号**：沿用 u1-l5 建立的记号，`D2rD` = initiator 的 device 内存写往远端 device 内存，`r` 表示 remote；方向由「initiator 本地内存类型 × target 远端内存类型 × op（read/write）」唯一决定。
- **loops 与 warm-up**：完整跑一遍 block 阶梯（16K→32K→…→2M）称为一轮（loop）。首轮通常含缓存预热、建链摊销等一次性开销，所以稳态吞吐要看第二轮及以后，或直接设 `loops>1`。
- 依赖 u2-l5 的传输接口知识：`TransferSync` / `TransferAsync` / `GetTransferStatus`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [benchmarks/run_all_bench.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_bench.sh) | 一键入口：参数解析、检查构建产物、转发给 Python 编排器 |
| [benchmarks/run_all_benchmarks.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py) | 编排器：平台检测、枚举 transport×方向组合、拉起 target/initiator、跑 KV 套件、渲染报告 |
| [benchmarks/comm_benchmark/hixl_comm_bench.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/hixl_comm_bench.cpp) | 通信基准 C++ 主程序：解析参数后分派给 ServerRunner/ClientRunner |
| [benchmarks/comm_benchmark/common/client_runner.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc) | initiator 侧测量逻辑：计时、带宽计算、CSV/JSONL 落盘 |
| [benchmarks/kv_benchmark/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/CMakeLists.txt) | KV 基准的构建定义（链接 cann_hixl、adxl_static、acl_rt 等） |
| [benchmarks/kv_benchmark/hixl_kv_bench.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/hixl_kv_bench.cpp) | KV 基准 C++ 主程序：模拟 KV 池化场景的 put/get |
| [benchmarks/kv_defaults.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_defaults.py) | KV 基准按平台（a2/a3/a5）的默认进程数与 transport |
| [benchmarks/performance.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md) | 多平台性能数据汇总文档（手动维护），解读带宽表的样板 |
| [benchmarks/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/README.md) | 基准使用手册：环境检查、参数表、支持矩阵 |

## 4. 核心概念与源码讲解

### 4.1 一键基准入口：run_all_bench.sh 与 run_all_benchmarks.py

#### 4.1.1 概念说明

`run_all_bench.sh` 是面向人的薄封装，`run_all_benchmarks.py` 才是真正的编排器。分层的原因：shell 只负责「参数归一化 + 环境检查 + 确保有可执行文件」，而组合枚举、平台适配、进程拉起、报告渲染这类复杂逻辑放在 Python 里更好维护。这种「shell 入口 + Python 编排 + C++ 测量内核」的三层结构是本仓库基准体系的骨架。

#### 4.1.2 核心流程

```
run_all_bench.sh
  ├─ 解析参数（--loops/--device-ids/--platform/--skip-comm/--skip-kv/--hixl-option/--output）
  ├─ check_env：找不到 npu-smi 时尝试 source /usr/local/Ascend/cann/set_env.sh
  ├─ 检查 build/benchmarks/comm_benchmark/hixl_comm_bench 是否存在
  │    └─ 不存在则自动 bash build.sh --examples
  └─ python3 benchmarks/run_all_benchmarks.py <归一化后的参数>

run_all_benchmarks.py main()
  ├─ detect_platform()：npu-smi 探测 a2/a3/a5（可 --platform 跳过）
  ├─ run_comm_suite()：枚举 transport × initiator_mem × target_mem × op 全组合
  │    ├─ 逐组合 run_combo()：先起 target 进程，sleep 2 秒后起 initiator，等待退出
  │    └─ 每个组合用不同 hixl 端口（base 17000，每个组合 +2）
  ├─ run_kv_benchmarks()：对 3 个模型逐个调 run_kv_benchmark.py
  └─ render_perf_report()：把 CSV 渲染成 perf.md
```

#### 4.1.3 源码精读

**入口脚本只做三件事**。参数解析支持 `--loops 10` 与 `--loops=10` 两种写法；`--hixl-option` 可重复出现，逐个透传给通信基准（对应 `hixl_comm_bench` 的 `-H=KEY=VALUE`，即 HIXL `Initialize()` 选项）：

- [benchmarks/run_all_bench.sh:L70-L108](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_bench.sh#L70-L108)：`while/case` 逐个消费参数，把 shell 风格参数（如 `--skip-kv`）翻译成 Python 风格（`--skip_kv`）。
- [benchmarks/run_all_bench.sh:L110-L114](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_bench.sh#L110-L114)：显式拒绝双机编排——`--target-host` 传非 `127.0.0.1` 直接报错，提示改用 `run_comm_benchmark.py --role=target/initiator`。这是一个重要的边界：**一键脚本仅支持单机**。
- [benchmarks/run_all_bench.sh:L127-L132](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_bench.sh#L127-L132)：检查二进制是否存在，缺失则调用 `bash build.sh --examples` 现场构建（呼应 u1-l2：`--examples` 联动打开样例与基准开关）。

**编排器的组合枚举**。四个维度的笛卡尔积决定要跑多少组通信测试：

- [benchmarks/run_all_benchmarks.py:L49-L55](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L49-L55)：定义维度常量——内存类型 `{device, host}`、操作 `{write, read}`、block 阶梯固定 `16K:2M`（16KiB 到 2MiB 按 2 倍递增共 8 档）、KV 模型 `['deepseek-r1', 'glm5', 'deepseek-v4']`；每个平台的 transport 列表不同（A2 只有 hccs+roce，A3 加 fabric_mem，A5 是 roce/fabric_mem/uboe/ub_rtp/ub）。
- [benchmarks/run_all_benchmarks.py:L260-L265](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L260-L265)：`_comm_combo_iter` 用 `itertools.product` 枚举 `transport × initiator_mem × target_mem × op` 全组合——以 A3 为例，3 transport × 2 × 2 × 2 = 24 组（其中 hccs 不支持的组合会被跳过）。

**平台约束在编排层就过滤掉**，避免起无意义的进程：

- [benchmarks/run_all_benchmarks.py:L244-L249](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L244-L249)：`_hccs_supports_combo` 编码了 HCCS 的支持矩阵——A2 仅 D2rD/rD2D，A3 额外支持 H2rD/rD2H（即 host↔device 组合），A5 完全不支持 HCCS。
- [benchmarks/run_all_benchmarks.py:L326-L331](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L326-L331)：不支持的组合打印 `[SKIP]` 日志并直接返回，不计数。

**进程对拉起与方向命名**：

- [benchmarks/run_all_benchmarks.py:L110-L148](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L110-L148)：`_make_cmd` 分别拼 target 与 initiator 的命令行。target 带 `--local_engine=<host>:<port>`（带端口即 server，呼应 u2-l1）；initiator 的 `--local_engine` 端口 = target 端口 + 1，`--remote_engine` 指向 target。initiator 还携带 `--block_sizes`、`--loops`、`--op`、`-H=` 选项等测量参数。
- [benchmarks/run_all_benchmarks.py:L190-L199](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L190-L199)：`run_combo` 先 `Popen` server，`sleep(2)` 等它监听就绪，再起 client；client 超时 300 秒、server 收尾超时 10 秒，双双 `rc==0` 才算该组合成功。
- [benchmarks/run_all_benchmarks.py:L231-L241](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L231-L241)：`_compute_direction` 把「initiator 内存 × target 内存 × op」翻译成方向记号，例如 device+device+write → `D2rD`、device+host+read → `rH2D`。write 与 read 的记号方向相反，因为 op 是按 initiator 视角定义的（u2-l2）。

**报告生成**：

- [benchmarks/run_all_benchmarks.py:L380-L395](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L380-L395)：`render_perf_report` 调用 `benchmarks/performance/render_perf_md.py`，把 `comm_benchmark/output/` 下的 `comm_result_*.csv` 渲染成 `perf.md`（含表格与折线图）。注意区分：`perf.md` 是脚本自动生成的**当前平台**数据，`performance.md` 是开发者手动汇总的**多平台**数据。

#### 4.1.4 代码实践

**实践目标**：不跑任何测量，只读编排源码，回答「一键脚本到底会跑几组通信测试」。

**操作步骤**：

1. 阅读 [benchmarks/run_all_benchmarks.py:L49-L57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L49-L57) 与 [L244-L265](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L244-L265)。
2. 分别对 a2 / a3 / a5 手工计算组合数：`len(transports) × 2(内存) × 2(内存) × 2(op)`，再减去 hccs 被跳过的组合。
3. 用 Python 验证（本机即可，无需 NPU）：

```bash
cd <仓库根目录>
python3 - <<'EOF'
import sys
sys.path.insert(0, 'benchmarks')
from run_all_benchmarks import _comm_combo_iter, _hccs_supports_combo
for plat in ('a2', 'a3', 'a5'):
    total = skipped = 0
    for transport, im, tm, op in _comm_combo_iter(plat):
        total += 1
        if transport == 'hccs' and not _hccs_supports_combo(plat, im, tm):
            skipped += 1
    print(f'{plat}: 枚举 {total} 组, 跳过 {skipped} 组, 实际执行 {total - skipped} 组')
EOF
```

**需要观察的现象**：三行的「实际执行」数字。
**预期结果**（待本地验证）：a2 = 2×8 − 6（hccs 仅 D2D 两方向保留）= 10 组；a3 = 3×8 − 12（hccs 保留 4 组）= 12 组；a5 = 5×8 = 40 组（无 hccs）。如果与你手算一致，说明你已读懂枚举与过滤逻辑。

#### 4.1.5 小练习与答案

**练习 1**：`run_all_bench.sh --hixl-option 'LocalCommRes={"version":"1.3"}'` 中的选项最终传到哪里？
**答案**：shell 收集进 `EXTRA_PY` 数组（[run_all_bench.sh:L92-L97](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_bench.sh#L92-L97)），作为 `--hixl_option` 传给 Python，再在 `_make_cmd` 中变成 `-H=KEY=VALUE` 附到 initiator/target 命令行（[run_all_benchmarks.py:L125-L128](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L125-L128)），最终由 `hixl_comm_bench` 塞进 HIXL 的 `Initialize()` options。注意它只作用于通信基准，KV 基准不受影响。

**练习 2**：为什么每个组合要分配不同的 hixl 端口？
**答案**：见 [run_all_benchmarks.py:L343-L347](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L343-L347)，端口按 `base_hixl_port(17000) + port_offset*2` 递增，target 与 initiator 各占一个。组合是串行执行的，但上一组合的进程可能尚未完全退出（TIME_WAIT 等），复用端口会 bind 失败；错开端口是最简单的规避手段。

---

### 4.2 comm_benchmark：hixl_comm_bench 与方向矩阵

#### 4.2.1 概念说明

`comm_benchmark` 测的是 HIXL 引擎的**裸传输能力**：给定 transport（hccs/roce/fabric_mem/uboe/…）、方向（8 种）、block 大小（16K→2M 阶梯），测有效带宽。它不模拟任何业务，就是反复调用 `TransferSync`/`TransferAsync` 搬一块注册好的内存。与 u1-l3 的 quickstart 相比，它多了三件事：**block 阶梯**（同一总量按不同粒度切分）、**多轮重复**（消除偶然误差）、**结果落盘**（CSV/JSONL 供渲染报告）。

角色模型与 quickstart 一致：target 是被动方（注册内存等待连接），initiator 是主动方（建链并发起 read/write）。

#### 4.2.2 核心流程

```
hixl_comm_bench main()
  ├─ BuildFromArgv / Validate / ApplyTransportEnvironment   # 参数解析与校验
  ├─ LogCommBenchConfig                                       # 打印配置（含 loops=1 warm-up 提示）
  └─ RunCommBench
       ├─ role == target  → ServerRunner: Init → Run → Shutdown
       └─ role == initiator → ClientRunner: Init → Run → Shutdown

ClientRunner 每个 block 档位的测量（同步模式）：
  trans_num = transfer_size / block_size          # 如 128M/16K = 8192 条
  start = steady_clock::now()
  TransferSync(remote_engine, op, descs)           # 一次性批量下发
  time_us = now() - start
  throughput = transfer_size / 1e9 / (time_us / 1e6)   # 十进制 GB/s
```

带宽公式：

\[
\text{bandwidth (GB/s)} = \frac{\text{transfer\_size（字节）}}{10^9 \times t\text{（秒）}}
\]

异步模式则把时间拆成 **submit**（下发 `TransferAsync` 的耗时）与 **wait**（轮询 `GetTransferStatus` 到终态的耗时）两段，总耗时 = submit + wait，可以据此判断瓶颈在提交路径还是数据面。

#### 4.2.3 源码精读

- [benchmarks/comm_benchmark/hixl_comm_bench.cpp:L132-L150](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/hixl_comm_bench.cpp#L132-L150)：`main` 的全部逻辑——解析、校验、应用 transport 环境变量、打印配置、进入 `RunCommBench`。主程序不含任何测量代码，测量全部委托给 Runner。
- [benchmarks/comm_benchmark/hixl_comm_bench.cpp:L109-L130](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/hixl_comm_bench.cpp#L109-L130)：`RunCommBench` 按角色二分：target 走 `ServerRunner`，initiator 走 `ClientRunner`，各自 `Init → Run → Shutdown`。
- [benchmarks/comm_benchmark/hixl_comm_bench.cpp:L102-L106](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/hixl_comm_bench.cpp#L102-L106)：`loops=1` 时主动打印提示「首轮常为 warm-up，稳态吞吐看第二轮或设 loops>1」——这是读数时最容易踩的坑。
- [benchmarks/comm_benchmark/common/client_runner.cc:L56-L59](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L56-L59)：带宽口径常量——`kDecimalBytesPerGb = 10^9`、`kMicrosecondsPerSecond = 10^6`，并注释明确「带宽用十进制 GB/s，参数 K/M/G/T 按二进制 1024 解析」。
- [benchmarks/comm_benchmark/common/client_runner.cc:L486-L503](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L486-L503)：同步测量核心——`trans_num = transfer_size / block_size` 条描述符一次 `TransferSync` 下发（批量是第一公民，u2-l5），用 `steady_clock` 计时，按上式算吞吐。计时器用 `steady_clock` 而非 `system_clock`，因为后者可能被 NTP 跳变。
- [benchmarks/comm_benchmark/common/client_runner.cc:L513-L536](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L513-L536) 与 [L557-L589](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L557-L589)：异步路径——`SubmitAsyncRequests` 按 `async_batch_num` 分批 `TransferAsync` 拿 `TransferReq`；`WaitAsyncRequests` 以 60 秒为 deadline 轮询 `GetTransferStatus`（1 微秒间隔），结果把耗时拆成 submit/wait/total 三段分别记录。
- [benchmarks/comm_benchmark/common/client_runner.cc:L296-L298](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L296-L298)：CSV 表头 `benchmark,pattern,...,bandwidth_gbps,ops_per_sec,avg_latency_us,p99_us,error_count,consistency`——渲染 `perf.md` 的数据来源；多轮 loop 的带宽在汇总输出时取平均（[L275-L276](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L275-L276)）。

#### 4.2.4 代码实践

**实践目标**：理解「block 越小带宽越低」并验证带宽换算。

**操作步骤**：

1. 阅读 [client_runner.cc:L486-L503](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L486-L503)，确认 `trans_num` 与吞吐公式。
2. 手工推演：设 `transfer_size = 128 MiB = 134217728 字节`。若某档位实测耗时 5618 µs，则吞吐 = 134217728 / 10⁹ / (5618/10⁶) ≈ 23.89 GB/s。
3. 对照 [performance.md:L50-L57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L50-L57)（A3 单机 ROCE hixl_cs 表）：16K 档 18.216 GB/s、64K 档 23.540 GB/s、2M 档 23.854 GB/s——小 block 明显吃亏，大 block 趋近链路上限约 23.8 GB/s。
4. 计算每条描述符的固定开销量级：16K 档 128MiB/16KiB = 8192 条，若比 2M 档（64 条）慢 24%，可粗估每条 desc 的额外开销约为 (总耗时差)/描述符数差，数量级通常在亚微秒级。

**需要观察的现象**：16K→2M 带宽单调上升并收敛到平台上限。
**预期结果**：ROCE 在 A3 上收敛约 23.8 GB/s；若你本地实测某档位显著低于 performance.md 同列（如低于 80%），先排查 loops 是否为 1（warm-up）、设备间连通性（`hccn_tool -ping`）与 TLS 配置（见 [benchmarks/README.md:L21-L65](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/README.md#L21-L65)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 说「总数据量 128MiB」但带宽单位是 GB/s？
**答案**：两者口径不同但各自自洽——`--transfer_size=128M` 中的 M 按二进制 1024 解析（128 MiB = 134217728 字节），而带宽按十进制 10⁹ 字节每 GB 计算，见 [client_runner.cc:L56-L58](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L56-L58)。换算时分子用字节数即可，不要把 128M 先折成「0.128 GB」再去除。

**练习 2**：异步模式日志里 `total: X us (submit: Y, wait: Z)`，若 Y 占比很高说明什么？
**答案**：submit 是发起 `TransferAsync` 的主机侧耗时，wait 是等数据面真正搬完的时间。submit 占比高说明 CPU 提交路径（描述符构造、引擎入队）是瓶颈，可以尝试增大 block size 或调整 `--async_batch_num`；wait 占比高则说明瓶颈在链路带宽本身，属正常形态。参见 [client_runner.cc:L580-L589](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/client_runner.cc#L580-L589)。

**练习 3**：直接运行 binary 与用 Python 启动器跑，默认参数有何关键差异？
**答案**：binary 默认 `loops=1` 且 `block_sizes` 缺省等于 `transfer_size`（单档），Python 启动器默认 `loops=5`、`block_sizes=16K:2M`。直接跑 binary 若不调参数，得到的是含 warm-up 的单档结果，不能与 performance.md 直接对比。

---

### 4.3 kv_benchmark：KV Cache 池化场景

#### 4.3.1 概念说明

`comm_benchmark` 回答「链路最 多能跑多快」，`kv_benchmark` 回答「贴近真实业务的 KV Cache 池化负载下能跑多快」。它模拟的形态是：多个推理 rank 共享同一份 KV Cache（shared 策略），rank 0 负责 put（写入全部 key），所有 rank 并行 get（读取）。key 数量、模型层数、attention 类型（MLA/DSA/SWA）都来自 `config/models.json` 的模型规格，因此带宽数字里含了 slice 切分、多线程并发传 key 等真实开销，通常低于裸 comm 基准。

与通信基准的另一区别：KV 基准**单进程模拟一个 rank**，由 `run_kv_benchmark.py` 拉起 N 个进程（默认 8）协同工作，transport 默认随平台。

#### 4.3.2 核心流程

```
run_all_benchmarks.py::run_kv_benchmarks
  └─ 对每个模型（deepseek-r1 / glm5 / deepseek-v4）：
       python3 kv_benchmark/scripts/run_kv_benchmark.py \
         --bench_bin=build/benchmarks/kv_benchmark/hixl_kv_bench \
         --model=<m> --transport=<平台默认> \
         --num_processes=<len(devices)> --devices=<csv> --output_dir=...
            └─ 再拉起 num_processes 个 hixl_kv_bench 进程（每 rank 一个）
                 ├─ rank 0：put 全部 key（写入远端 KV 池）
                 ├─ 所有 rank：并行 get（从远端读回本地 device buffer）
                 └─ 输出 CSV/JSON（bandwidth_gbps、avg_latency_us、p99_us、total_bytes）
```

`total_bytes` 按 slice 实际汇总（尊重 `max_key_count`），不是「单 key 大小 × key 数」的粗略乘积。

#### 4.3.3 源码精读

- [benchmarks/run_all_benchmarks.py:L202-L228](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L202-L228)：`run_kv_benchmarks` 逐模型调用 KV 启动脚本，传 `--num_processes=len(devices)`、`--platform`、`--transport`，返回成功数。
- [benchmarks/run_all_benchmarks.py:L410-L422](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L410-L422)：`main` 中 KV 分支——二进制缺失只警告不报错（跳过 KV），transport 与设备列表来自 `kv_defaults` 的平台默认值。
- [benchmarks/kv_defaults.py:L33-L43](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_defaults.py#L33-L43)：平台默认——每平台 8 个 rank 进程；transport A2=roce，A3/A5=fabric_mem（呼应 u5：FabricMem 是 A3/A5 上 KV 场景的推荐路径）。
- [benchmarks/kv_benchmark/CMakeLists.txt:L11-L18](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/CMakeLists.txt#L11-L18)：`hixl_kv_bench` 由主程序、传输执行器与 kvstore 四个源文件（kvstore/segment_manager/model_config/kv_slice_layout）组成，编译选项 `-O2 -Wall -Werror -ftrapv`（有符号溢出即陷阱，基准代码对数值正确性要求苛刻）。
- [benchmarks/kv_benchmark/CMakeLists.txt:L37-L46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/CMakeLists.txt#L37-L46)：链接 `cann_hixl`（HIXL 引擎）、`adxl_static`、`acl_rt`、`ascend_hal`——直接复用 u3-l5 讲过的 proxy 底座。
- [benchmarks/kv_benchmark/hixl_kv_bench.cpp:L44-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/hixl_kv_bench.cpp#L44-L79)：默认常量——每 rank 本地 device buffer 下限 1 GiB、默认 8 进程、base 端口 19000、warmup=1 / repeat=10、同步超时 300 秒；带宽同样按十进制 GB/s（`kDecimalBytesPerGb`）。它直接 include `hixl/hixl.h` 与 `fabric_mem/fabric_mem_transfer_service.h`，说明 KV 基准同时驱动了引擎公开 API 与 FabricMem 内部服务。
- 模型规格见 [benchmarks/README.md:L366-L376](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/README.md#L366-L376)：deepseek-r1（61 层 MLA）、glm5（78 层 MLA+DSA）、deepseek-v4（Hybrid CSA/HCA+SWA，`max_key_count=1` 只传 key0）。

#### 4.3.4 代码实践

**实践目标**：不依赖硬件，通过配置文件理解 KV 负载如何由模型规格决定。

**操作步骤**：

1. 打开 [benchmarks/kv_benchmark/config/models.json](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_benchmark/config/models.json)，找到 `deepseek-r1` 条目，记下层数、每层 tensor 形状与 attention 类型。
2. 估算单 key 字节数：KV 每 key 字节 ≈ 层数 × 每层 slice 字节之和（按 slice 布局 `kv_slice_layout` 汇总）。
3. 用 `--key_counts=16,32,48,64` 的默认阶梯估算 `total_bytes` 随 key 数的线性增长。
4. 若有 8 卡环境，运行 `python3 benchmarks/kv_benchmark/scripts/run_kv_benchmark.py --model=deepseek-r1`（transport 走平台默认），观察 CSV 中 `bandwidth_gbps`、`p99_us` 列。

**需要观察的现象**：key 数增大时吞吐上升（摊薄固定开销）而 p99 时延基本平稳；KV 带宽低于同平台 comm 基准大 block 档位。
**预期结果**：第 1–3 步为纸面推演，第 4 步**待本地验证**（需要 8 张同平台 NPU）。若实测 KV 吞吐远低于 comm 基准，先确认 transport 是否一致（例如 A3 上两者都用 fabric_mem 才可对比）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 KV 基准默认 transport 在 A3/A5 上是 fabric_mem 而不是 roce？
**答案**：见 [kv_defaults.py:L39-L43](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/kv_defaults.py#L39-L43)。KV 池化是超节点内大流量场景，FabricMem 经统一编址可达百 GB/s 级（performance.md A3 单机 D2rD 2M 档约 165 GB/s），远高于 RoCE 的约 23.8 GB/s；A2 不支持 FabricMem 故退回 roce。这与 u5-l1 讲的 FabricMem 设计动机一致。

**练习 2**：KV 基准与 comm 基准的进程模型有何不同？
**答案**：comm 基准一次测量只拉起 1 个 target + 1 个 initiator；KV 基准一次测量拉起 `num_processes`（默认 8）个 rank 进程，rank 0 put、全体 get，模拟多推理 rank 共享 KV 池的并发读写，见 [run_all_benchmarks.py:L416-L421](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/run_all_benchmarks.py#L416-L421) 与 README 的 shared 策略说明。

---

### 4.4 性能文档：解读 performance.md 与瓶颈定位

#### 4.4.1 概念说明

[benchmarks/performance.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md) 是多平台实测数据的汇总（A3、A2 两章，各分单机/双机），由开发者手动从各平台 `perf.md` 中摘录维护。它的价值是**基线**：你本地跑出的数字应与同平台、同 transport、同方向的列大体吻合；显著偏低即是排查信号。读懂它需要掌握三条读表规则。

#### 4.4.2 核心流程（读表三步法）

1. **定位行列**：行是 block 档位（16K→2M），列是 8 个方向；表按 transport 分组（FabricMem → ROCE → HCCS），FabricMem 再分 AICPU 展开 / Host 展开，ROCE/HCCS 再分 hixl_cs / 通信域两条路径。
2. **横向看收敛**：沿行向下（block 变大），带宽应单调上升并收敛到该 transport 的平台上限（A3 单机：ROCE ≈ 23.8，HCCS D2rD ≈ 165，FabricMem AICPU ≈ 170，单位 GB/s）。
3. **纵向看方向**：同一行内对比方向，可发现链路不对称。例如 A3 单机 HCCS 2M 档 D2rD 164.6 vs rD2D 165.3 基本对称，而 16K 档 18.0 vs 17.2 已有小幅差异；涉及 host 内存的方向（D2rH/rH2D 等）系统性低于纯 device 方向，因为多了一段 host 总线。

瓶颈定位的因果链：

```
实测偏低
  ├─ 16K 小 block 档低           → 正常：描述符固定开销占比高，看大 block 档
  ├─ 所有档位均匀低 ~20%          → 环境：连通性（hccn_tool ping）/ TLS / 频率锁定
  ├─ loops=1 且仅首轮低           → warm-up：改 loops>1 取第二轮
  ├─ 异步 submit 占比高           → 提交路径瓶颈：增大 block / 调 async_batch_num
  └─ 双机比单机明显低             → 跨机链路（RoCE 网卡/交换机）上限，对照性能文档双机表
```

#### 4.4.3 源码精读

- [benchmarks/performance.md:L9-L16](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L9-L16)：官方读表指南——总数据量 128MiB；数值为有效带宽 GB/s；「不支持」表示该平台该路径不支持该方向；HCCS/ROCE 分 hixl_cs 与通信域两条路径，FabricMem 分 AICPU/Host 展开（Host 为 4 流并发）。
- [benchmarks/performance.md:L20-L31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L20-L31)：A3 单机 FabricMem（AICPU 展开）表——D2rD 从 16K 的 16.1 GB/s 爬升到 2M 的 165.5 GB/s；同表 H2rH 列 2M 档仅 32.4 GB/s，印证「host 参与的路径系统性偏低」。这张表可与 u5-l4 的 FabricMem 统计观测互相印证。
- [benchmarks/performance.md:L46-L57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L46-L57)：A3 单机 ROCE（hixl_cs）表——各方向 64K 以后全部收敛到约 23.5–23.9 GB/s，这就是该平台 RoCE 链路的实际上限；与 u1-l1 介绍的「RDMA 跨主机约 22GB/s」量级吻合。
- [benchmarks/performance.md:L72-L83](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L72-L83)：A3 单机 HCCS（hixl_cs）表——D2rD/rD2D 收敛约 165 GB/s，D2rH/rH2D 等标「不支持」，与 `_hccs_supports_combo`（4.1.3）的过滤规则一一对应：**文档里的「不支持」就是编排层 `[SKIP]` 的结果**。
- [benchmarks/README.md:L142-L145](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/README.md#L142-L145)：明确 `perf.md`（自动、单平台）与 `performance.md`（手动、多平台）的关系——对比基线时要用后者。

#### 4.4.4 代码实践

**实践目标**：从 performance.md 提炼一张「平台带宽上限速查表」。

**操作步骤**：

1. 打开 [performance.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md)，对每个「transport × 机器形态」取 2M 档 D2rD 列作为该路径的收敛带宽。
2. 填出下表（示例答案已给出，来自 A3 章）：

| transport（A3） | 单机 D2rD @2M (GB/s) | 双机 D2rD @2M (GB/s) |
| --- | --- | --- |
| FabricMem AICPU | 165.513 | 105.036 |
| FabricMem Host(4流) | 181.431 | 125.167 |
| ROCE hixl_cs | 23.854 | 23.933 |
| HCCS hixl_cs / 通信域 | 164.610 / 158.812 | —（hixl_cs 双机无表）/ 89.985 |

3. 用 A2 章（[L168-L258](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md#L168-L258)）再填一张 A2 表，对比两平台 HCCS 上限差异（A2 约 20–27 GB/s vs A3 约 165 GB/s）。
4. 记录一个反直觉现象并解释：双机 HCCS（通信域）D2rD 仅 89.985 GB/s，低于单机的 158.812——跨机 HCCS 需经过级联互联，带宽折半。

**需要观察的现象**：表格能直观暴露「FabricMem 大 block 才能跑满」「HCCS 不支持 host 方向」等结论。
**预期结果**：完成两张速查表；数字直接摘自文档，无需本地验证。

#### 4.4.5 小练习与答案

**练习 1**：A3 单机 ROCE 表里 16K 档各方向约 18–19 GB/s，而 32K 档跳到约 23 GB/s，为什么 16K 掉得不多？
**答案**：RoCE 单条 WR（工作请求）的网卡处理开销小，且 16K 已足够让 DMA 引擎流水化；对比 HCCS 16K 档只有 18 GB/s（上限 165 的约 11%），RoCE 的曲线「更平」是因为它的上限本来就低（23.8），固定开销被上限掩盖。这也是选型直觉：**上限越高的链路，越怕小 block**。

**练习 2**：「hixl_cs」与「通信域」两条路径分别对应什么？
**答案**：hixl_cs 是 HIXL Engine 的 CS 数据面（u4 讲的 `src/hixl/cs/` 模块）；通信域是 HCCL 原生路径（legacy 版本建链方式，即 u1-l5 提到的 `--version=0` HCCL 方式）。两列并列是为了对比新旧数据面在同链路上的性能差异，例如 A2 ROCE 通信域 16K 档仅 0.716 GB/s，远低于 hixl_cs 的 18.306。

---

## 5. 综合实践

**任务：跑一次通信基准并与官方数据对比分析（即本讲的 practice_task）。

在有昇腾硬件的环境上：

1. **准备**：`bash build.sh --examples`（u1-l2），按 [benchmarks/README.md:L21-L65](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/README.md#L21-L65) 用 `hccn_tool` 确认所选两卡互通、TLS 配置一致。
2. **快跑单方向**（推荐先做，验证环境）：

   ```bash
   python3 benchmarks/comm_benchmark/scripts/run_comm_benchmark.py \
     --direction=D2rD --transport=hccs --device_ids=0,1 --loops=5
   ```

3. **全量跑**：`bash benchmarks/run_all_bench.sh --loops 5 --device-ids 0,1`（用两卡即可；KV 套件会因设备数不足自动取前 N 个），结束后打开 `benchmarks/perf.md`。
4. **记录**：抄录 D2rD 方向 16K/64K/256K/2M 四档带宽。
5. **对比**：与 [performance.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance.md) 同平台、同 transport、同方向的列逐档对比，计算比值，按 4.4.2 的因果链给出结论（正常 / warm-up / 环境问题 / 提交瓶颈）。

**无硬件时的替代任务（源码阅读型）**：完成 4.1.4 的组合数验证 + 4.4.4 的两张速查表，并写一段 200 字分析：为什么 FabricMem Host 展开在 512K 以下远逊于 AICPU 展开（提示：Host 路径逐 op 发 `aclrtMemcpyAsync`，见 u5-l3；小 block 时逐条开销远大于 AICPU 内核批量下发）。

**预期结果**（待本地验证）：实测各档带宽与 performance.md 同列偏差在 ±10% 以内；16K 档偏差最大属正常。

## 6. 本讲小结

- 基准体系是「shell 入口（run_all_bench.sh）→ Python 编排（run_all_benchmarks.py）→ C++ 测量内核（hixl_comm_bench / hixl_kv_bench）」三层结构；一键脚本仅支持单机，双机需用 `run_comm_benchmark.py --role`。
- 编排器用 `itertools.product` 枚举 transport×内存×内存×op 全组合，并在 `_hccs_supports_combo` 中按平台过滤不支持的组合——performance.md 里的「不支持」列正是这些 `[SKIP]` 的留痕。
- comm 基准测裸 block 带宽：128MiB 总量按 16K→2M 阶梯切分，`TransferSync` 批量下发、`steady_clock` 计时；带宽按**十进制 GB/s**，参数 K/M 按二进制 1024，首轮是 warm-up。
- kv 基准测业务形态带宽：8 进程模拟推理 rank，rank 0 put、全体 get，模型规格（MLA/DSA/SWA）来自 models.json；A3/A5 默认走 fabric_mem。
- 瓶颈定位三板斧：横向看大 block 收敛值是否达到平台上限；纵向看方向不对称（host 路径低是常态）；异步日志拆 submit/wait 区分提交瓶颈与链路瓶颈。
- `perf.md` 是自动生成的单平台数据，`performance.md` 是手动维护的多平台基线，对比要用后者。

## 7. 下一步学习建议

- 下一讲 u8-l2（测试体系与单测编写）转向正确性维度：gtests 如何组织、如何为新功能补测试。
- 想深挖性能数据的来源，可回读 u8-l3（profiling 与统计）了解引擎侧埋点；回读 u5-l3/u5-l4 了解 FabricMem 两条路径的统计口径。
- 建议通读 [benchmarks/comm_benchmark/common/benchmark_config.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/comm_benchmark/common/benchmark_config.cpp) 与 [benchmarks/performance/render_perf_md.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/benchmarks/performance/render_perf_md.py)，弄清 CSV 每列如何变成 perf.md 表格，为自定义基准报告打基础。
