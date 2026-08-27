# 测量方法学批判、性能剖析与仓库已知问题

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐项列出 uBench 主机端 `std::chrono` 计时窗口内混入的所有额外开销（setArg、内核启动、完成同步、时钟抖动），并对每项的量级做出有依据的估计。
2. 说出仓库里现成的三条「内核级数据」获取路径：`cl_event` 时间戳（队列已开 profiling）、`xrt.ini` 运行时开关、`make PROFILE=yes` + `perf_analyze` 构建期流程，并知道三者各自的代价与适用场景。
3. 拿着一份经过核实的「已知问题清单」阅读仓库，不再被文档滞后、Python 2 脚本、打印 bug 和硬编码魔数绊倒，并能对每个问题提出修复方向。

本讲是收官单元的最后一篇方法论讲义：前两篇（u7-l1、u7-l2）教你复用和扩展仓库，本讲教你**带着怀疑的眼光使用仓库的测量数字**。

## 2. 前置知识

- **计时窗口（timing window）**：两次取时刻之间的那段墙钟时间。uBench 主机在启动内核前 `now()` 一次、`q.finish()` 返回后 `now()` 一次，窗口内一切都会被计入带宽公式的分母。
- **内核启动开销（launch overhead）**：从主机调用 `enqueueTask` 到计算单元真正开始取数之间，经过 OpenCL 运行时 → XRT → 驱动 → PCIe/总线下发 → CU 启动的整条软件与硬件链路，量级通常是几十微秒。它是常数，不随 payload 变化。
- **`cl_event` 双时间戳**：创建命令队列时打开 `CL_QUEUE_PROFILING_ENABLE` 后，每个提交的命令都会附带 `CL_PROFILING_COMMAND_START` / `CL_PROFILING_COMMAND_END` 两个纳秒级时间戳，由驱动在命令真正开始/结束的时刻打上——这是「剥离启动开销」的标准手段。
- **`xrt.ini`**：XRT 运行时配置文件，放在可执行文件所在目录（或工作目录）即可生效，不需要重新编译。`[Debug]` 段的 `profile=true` / `timeline_trace=true` 让运行时自动收集 kernel 执行统计与时间线，产出 `profile_summary.csv` 等报告文件。
- **`--profile_kernel`（硬件监视器）**：`v++` 链接期选项，在 AXI 端口插入计数器 IP，统计每个端口的读写字节数与带宽，需要重新链接 xclbin。
- **中位数（median）**：把同一配置重复测 R 次取中间值，比单次测量抗离群点（操作系统调度抖动、中断）能力强。

本讲默认你已读过 u2-l3（带宽公式与计时窗口的推导）与 u4-l3（嵌入式 ZCU104 的 `xrt.ini` 与板上运行），不再重复公式逐项推导，而是在其上做「批判性审计」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp` | 被审计的主机程序：计时窗口、带宽公式、打印语句 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h` | `NUM_ITERATIONS = 10000` 的定义处（与魔数 0.000010000 配对） |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile` | `-O0` 编译、`check` 目标末尾的 `perf_analyze` 调用 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk` | `PROFILE := no` 默认值与 `--profile_kernel` 注入逻辑 |
| `ubench/offchip_latency/embedded/32bit_per_access/xrt.ini` | 仓库唯一的运行时 profile 开关样本（嵌入式） |
| `README.md`（根）与 `ubench/offchip_latency/datacenter/README.md` | 已知问题清单的「文档侧」证据 |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp` | 延迟版对照：payload 实际起点、"Latency" 误标 |
| `ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py` | Python 2 裸 print 的实证位置 |

## 4. 核心概念与源码讲解

### 4.1 误差源分析：主机计时到底测了什么

#### 4.1.1 概念说明

uBench 所有数据中心版主机程序都用同一套计时模板：`std::chrono` 包住「setArg → enqueueTask → finish」。这种方法**实现最简单，但分母里混进了与内存系统无关的软件开销**。要判断测出来的带宽可信到什么程度，必须先弄清楚窗口里到底有什么、每项多大、随 payload 怎么变化。

核心直觉：额外开销近似常数（µs 量级），内核时间随 payload 线性增长（最小档数百 µs，最大档数百 ms），所以**误差在小 payload 档最严重、在大 payload 档可忽略**——这正是微基准扫描里最需要警惕的一段。

#### 4.1.2 核心流程

主机计时窗口的内容分解：

```text
kernel_start = now()
 ├─ (1) setArg × (NUM_PORT+1) 次        ← 主机侧参数拷贝，被计入
 ├─ (2) enqueueTask                      ← 命令入队 + XRT/驱动下发，被计入
 │        └─ (3) CU 真正开始执行          ← 启动延迟，被计入
 │        └─ (4) 内核执行                 ← 我们真正想测的
 └─ q.finish()                           ← 完成上报 + 用户态唤醒，被计入
kernel_end = now()
kernel_time = end - start
```

各项误差源与量级估计（量级为推算，**待本地验证**）：

| # | 误差源 | 量级估计 | 随 payload | 方向 |
| --- | --- | --- | --- | --- |
| 1 | `setArg` 调用（本工程 3 次） | 每次 µs 级以下 | 不变 | 高估时间 |
| 2 | `enqueueTask` + 内核启动链路 | 10⁴–10⁵ ns（几十 µs） | 不变 | 高估时间 |
| 3 | `finish()` 完成中断到用户态返回 | µs 级 | 不变 | 高估时间 |
| 4 | `high_resolution_clock` 粒度 + 调度抖动 | 粒度 ~ns，抖动可达 µs–ms | 不变 | 双向，单次测量无防御 |
| 5 | 内核流水爬坡（真实内存系统行为） | 数十拍 | 绝对值不变 | 属于被测对象 |
| 6 | 单次测量、无统计处理 | 离群点直接影响结果 | — | 双向 |

量级推演（以本工程为例，理想 II=1、300 MHz）：

\[ T_{ideal} = \frac{NUM\_ITERATIONS \times (payload / WIDTH\_FACTOR)}{f_{clk}} \]

- 最小档 payload=256（1KB）：\(10000 \times 16 / 3\times10^8 \approx 533\,\mu s\)。若启动开销 30–100 µs，带宽被低估约 **5%–20%**。
- 最大档 payload=262144（1MB）：\(10000 \times 16384 / 3\times10^8 \approx 0.55\,s\)。同样的启动开销只占 **<0.02%**。

结论：这份微基准的**大 payload 档数字基本可信，小 payload 档系统性偏低**；而小 payload 恰好是「连续访问数据量」这条曲线最有信息量的起始段。

#### 4.1.3 源码精读

**计时窗口的起止位置**。计时器在缓冲区迁移完成之后、`setArg` 之前启动：

- [src/host.cpp:L149-L165](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L149-L165) — `kernel_start` 取时刻后，紧跟 `setArg`、`enqueueTask`、`q.finish()`，全部落在窗口内；`setArg` 与启动链路的开销因此被计入分母。

好消息是数据搬运确实被排除在窗口之外：

- [src/host.cpp:L142-L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L142-L147) — `enqueueMigrateMemObjects` 与其后的 `q.finish()` 在计时开始前完成，主机→设备拷贝不计入。这一处模板设计是对的。

**单次测量、无重复**。每个 payload 档只跑一次：

- [src/host.cpp:L100-L104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100-L104) — payload 倍增循环体里只包含一轮「分配→迁移→启动→计时」，没有重复 R 次取中位数的逻辑，离群点（一次调度抢占即可造成）直接进入报表。

**公式的魔数配对隐患**：

- [src/host.cpp:L172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) — `0.000010000` 就是 `NUM_ITERATIONS / 1e9` 的硬编码值。
- [src/krnl_config.h:L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7) — 真正的 `NUM_ITERATIONS = 10000` 定义在契约头里。改了这一行而不改 host.cpp 里的魔数，带宽结果**静默失真一万倍量级的比例**，编译器不会有任何抱怨。改进方向：host.cpp 已 `#include "krnl_config.h"`，直接写 `NUM_ITERATIONS / 1e9` 即可。

**打印 bug（报表侧误差）**：

- [src/host.cpp:L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) — 打印的 payload 写的是 `i*4/(1024.0*1024.0)`，而 `i` 是内层 setArg 循环的变量，退出循环后恒为 `NUM_KERNEL=1`，所以每行都打印 `0.0000038MB`。要还原每档的真实 payload 只能自己按倍增序列数。对照延迟版的正确写法 `payload*4/(1024.0*1024.0)`（见 4.3 节引用），可见这是带宽版抄改时引入的笔误。

**主机程序 `-O0` 编译**：

- [Makefile:L53](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L53) — `-Wall -O0 -g`。窗口内的主机代码只有几次运行时 API 调用，`-O0` 对计时影响很小，但它同样放大了主机侧指令噪声、且没有换来任何好处；把测量主机换成 `-O2` 是零成本的卫生改进。

**队列其实已经开了 profiling**——这是通往 4.2 节的钥匙：

- [src/host.cpp:L56-L61](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L56-L61) — 建队列时已经传了 `CL_QUEUE_PROFILING_ENABLE`。也就是说，**驱动侧时间戳采集随时可用**，主机代码却从未把 `cl::Event` 传给 `enqueueTask` 去读它——功能开了，数据扔了。

#### 4.1.4 代码实践

**实践目标**：用 `cl_event` 时间戳替换 chrono 计时，并量化两种口径的差值随 payload 的变化。

**操作步骤**（修改的是你自己复制出来的实验目录，不动仓库源码）：

1. 把 `read/DDR/2ports_512bit` 复制为 `read/DDR/2ports_512bit_evt`（同深度，Makefile 无需改动，理由见 u7-l2 的契约清单）。
2. 在复制品的 `src/host.cpp` 中，把 `enqueueTask` 改为带事件版本：

```cpp
// 示例代码：基于 cl_event 的内核级计时（替换原 enqueueTask 一行）
cl::Event evt;
OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i], NULL, &evt));
q.finish();
cl_ulong t_start = 0, t_end = 0;
evt.getProfilingInfo(CL_PROFILING_COMMAND_START, &t_start); // ns
evt.getProfilingInfo(CL_PROFILING_COMMAND_END,   &t_end);   // ns
double evt_sec = (t_end - t_start) * 1e-9;
```

3. 同时保留原 chrono 计时，把两个时间都打印出来。
4. 在同一 payload 下计算 `launch_gap = chrono_sec - evt_sec`，观察它是否近似常数。
5. 如有 Vitis 环境可先用 `make check TARGET=sw_emu` 验证功能正确（仿真下数值无物理意义，但能验证代码路径）。

**需要观察的现象**：

- `evt_sec` 在仿真下也会返回有效时间戳（数值无意义）；真机下它应明显小于 `chrono_sec`。
- 差值 `launch_gap` 不随 payload 变化（常数启动开销），因此 `chrono/evt` 比值在小 payload 最大。

**预期结果**：小 payload 档 `chrono` 口径带宽比 `evt` 口径低约 5%–20%，大 payload 档两者趋同（量级推算见 4.1.2，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：如果只把 `NUM_ITERATIONS` 从 10000 改成 20000（为了拉长计时窗口），不动 host.cpp 的魔数，报表带宽会怎样变化？

**答案**：内核时间翻倍，而公式里的 `0.000010000` 仍按 10000 次折算字节数，所以报表带宽**减半**——真实的字节搬运量翻倍了，但公式只承认一半。必须同步把魔数改为 `0.000020000`（或改成引用 `NUM_ITERATIONS`）。

**练习 2**：为什么数据迁移（`enqueueMigrateMemObjects`）不放进计时窗口是正确设计，但首次访问的「流水爬坡」却应该保留在窗口内？

**答案**：迁移是测量仪器的一部分（把被测数据搬到现场），不是被测对象；而流水爬坡（内核开始发读请求到带宽稳定的过程）是内存系统自身的动态特性，正是微基准想刻画的行为之一。仪器开销要剔除，被测对象的物理性质要保留——区分标准永远是「它属于仪器还是属于被测对象」。

**练习 3**：单次测量取中位数为什么防不住「系统性的启动开销」？

**答案**：中位数只能抑制随机离群点（调度抖动、中断）；启动开销是**每次都在、方向一致**的系统性偏差（bias），重复一万次取中位数它也原样留在结果里。系统性偏差要用测量方法（`cl_event`）剔除，随机噪声才用统计手段（中位数）抑制——两者是正交的。

### 4.2 XRT profiling：拿到内核级数据的三条路

#### 4.2.1 概念说明

「内核级数据」指由驱动或硬件监视器在**命令真正开始/结束的时刻**记录的时间戳与流量统计，而不是主机墙钟。仓库里现成的获取路径有三条，代价与精度各不相同：

| 路径 | 开关位置 | 需要重建？ | 得到什么 |
| --- | --- | --- | --- |
| A. `cl_event` 时间戳 | 代码（队列已开 profiling） | 只改 host.cpp | 每条命令的精确起止 ns |
| B. `xrt.ini` 运行时收集 | 可执行文件目录放文件 | 否 | `profile_summary.csv`、时间线 trace |
| C. `--profile_kernel` 硬件监视器 | `make PROFILE=yes` | 需重新链接 | 每 AXI 端口读写字节/带宽报表 |

三条路可以叠加使用：A 给「净化后的内核时间」，C 给「每端口实际字节数」（可验证带宽公式分子是否真实），B 给全时间线（定位迁移、启动、执行的相对位置）。

#### 4.2.2 核心流程

```text
路径 B（运行时，嵌入式工程默认开启）:
  xrt.ini: [Debug] profile=true / timeline_trace=true
      → 应用运行时 XRT 自动收集
      → 生成 profile_summary.csv / trace 文件 / xrt.run_summary

路径 C（构建期，数据中心工程默认关闭）:
  make PROFILE=yes TARGET=hw DEVICE=<平台>
      → utils.mk 注入 --profile_kernel data:all:all:all
      → v++ 链接时在 AXI 端口插入硬件计数器
      → 运行后生成 profile_summary.csv
      → Makefile check 目标末尾 perf_analyze 把 csv 转成 html 报告
```

#### 4.2.3 源码精读

**仓库唯一的 `xrt.ini` 样本（嵌入式延迟工程）**：

- [ubench/offchip_latency/embedded/32bit_per_access/xrt.ini:L1-L3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/xrt.ini#L1-L3) — 三行配置打开 `profile` 与 `timeline_trace`。u4-l3 已讲过它随 `sd_card` 打包上板，使 ZCU104 每次运行都产出剖析文件。

数据中心工程目录下没有任何 `xrt.ini`（已用 `find` 核实：全仓库仅嵌入式三处），但 XRT 的读取规则是**可执行文件所在目录/工作目录**——所以把上面这三行存成文件放进数据中心工程目录即可无重建启用路径 B。

**`PROFILE` 开关与硬件监视器注入**：

- [utils.mk:L6-L11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L6-L11) — `PROFILE := no` 为默认；`make PROFILE=yes` 时向 `LDCLFLAGS` 追加 `--profile_kernel data:all:all:all`，即「所有内核所有端口插数据监视器」。紧随其后的 `DEBUG := no` / `--dk list_ports`（[utils.mk:L13-L19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L13-L19)）则是调试端口选项，与测量无关但常一起介绍。

**`perf_analyze` 后处理挂在 `check` 目标末尾**：

- [Makefile:L110-L135](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L110-L135) — `check` 目标先运行应用，最后两行在 x86 下执行 `perf_analyze profile -i profile_summary.csv -f html`，把 XRT 生成的 csv 转成可视化 html 报告。
- [Makefile:L157](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L157) — `clean` 目标删除 `profile_*`，佐证这套流程的产物命名。

注意一个隐患：`check` 目标**无条件**执行 `perf_analyze`，但 `profile_summary.csv` 只有在 profiling 开启（路径 B 或 C）时才会生成；默认配置（无 xrt.ini、`PROFILE=no`）下这一行可能因找不到输入文件而报错（**待本地验证**——2020.2 版 XRT 对默认运行是否落盘 csv 的行为需实测确认）。

#### 4.2.4 代码实践

**实践目标**：为一个数据中心微基准同时启用路径 B 与路径 C，拿到 html 版带宽报告，并与主机报表互相印证。

**操作步骤**：

1. 复制嵌入式样本到 read/DDR 工程目录（内容完全一致）：

```bash
cp ubench/offchip_latency/embedded/32bit_per_access/xrt.ini \
   ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/
```

2. 若有硬件与 Vitis 环境，用路径 C 重建并运行：

```bash
cd ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit
make check TARGET=hw DEVICE=<你的平台> PROFILE=yes
# 运行结束后查看 profile_summary.html（由 check 末尾的 perf_analyze 生成）
```

3. 打开报告，找到 `krnl_ubench_1` 的执行时间与各 `gmem` 端口的读写字节数。
4. 交叉验证：端口读字节数 ÷ 报告内核时间，与 host.cpp 打印的 `Bandwidth` 对比。
5. 无硬件时执行「源码阅读型实践」：走读 [Makefile:L118-L135](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L118-L135) 与 [utils.mk:L6-L11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L6-L11)，画出「PROFILE=yes → LDCLFLAGS → v++ -l → xclbin 内监视器 → 运行 → csv → perf_analyze → html」的变量传递链。

**需要观察的现象**：报告里每端口字节数是否 ≈ `payload × 4 × NUM_ITERATIONS / WIDTH_FACTOR × WIDTH_FACTOR / 8` 字节（即 `payload×4×NUM_ITERATIONS` 字节每端口）；内核时间是否小于 host.cpp 打印的 `Execution time`。

**预期结果**：端口字节数与公式分子一致（验证分子真实性）；报告内核时间 < 主机 Execution time，差值即启动开销。真机数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么路径 C（`--profile_kernel`）能验证带宽公式的**分子**，而 `cl_event` 只能净化**分母**？

**答案**：硬件监视器挂在 AXI 端口上，数的是实际通过端口的字节数——它独立回答「到底搬了多少数据」，可检验公式分子（`payload×4×NUM_ITERATIONS×NUM_PORT`）是否与物理事实一致；`cl_event` 只给命令的起止时刻，能把启动开销从分母里剥掉，但对搬了多少字节一无所知。分子用 C 验证、分母用 A 净化，两者合起来才是一份可审计的测量。

**练习 2**：给嵌入式工程关掉 `xrt.ini` 的 profile 会发生什么？为什么数据中心工程默认不开启？

**答案**：板上不再生成 profile 报告，但应用照常运行、可能略微更快（收集有开销）。嵌入式默认开启是因为板上调试手段少、一次上板成本高，顺手收集数据是划算的；数据中心默认关闭则是为了测量的「干净」——剖析本身有开销，正式采集带宽数字时应关闭再测。

**练习 3**：`make PROFILE=yes` 后直接 `make check`（不 clean）会生效吗？

**答案**：大概率不会完整生效。`--profile_kernel` 走 `LDCLFLAGS`，在 `v++ -l` 链接阶段注入监视器；如果 `ubench.xclbin` 已存在且 Makefile 判定其比依赖新，链接步骤会被跳过，监视器不会插入。改了链接选项应先 `make clean`（或删掉 `build_dir.*`）再重建（具体依赖判定以 Makefile L98-100 的规则为准，**待本地验证**）。

### 4.3 已知问题清单：仓库考古与待确认项

#### 4.3.1 概念说明

「已知问题」不是贬义——所有研究型代码仓库都带有文档滞后与历史包袱。把它们列成清单的目的，是让读者**第一次踩坑之前就知道坑在哪**，并且理解每类问题的成因（抄自 Vitis 示例模板、README 从旧版本代码生成、脚本用当年的默认 Python），从而能自己修。

#### 4.3.2 核心流程

问题按「会误导测量结论 → 会妨碍编译运行 → 只是观感问题」排序， severity 递减。

#### 4.3.3 源码精读

**① 根 README 的 latency 链接指向不存在的路径**

- [README.md:L50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L50) — 链接写成 `ubench/off-chip_latency`（带连字符），而实际目录是 `ubench/offchip_latency`，点击即 404。同一段里的正文拼写也是小写 "off-chip Memory Latency"，与其他两节的大小写风格不一致，属笔误。

**② 多处 README 链接指向 `2020.2` 分支**

- [README.md:L36-L46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L36-L46) — 四条「详细指南」链接都指向 `tree/2020.2/...`。仓库默认分支是 `main`，这些链接是否可用取决于远端是否保留 `2020.2` 分支；更稳妥的写法是相对路径指向本仓库当前分支的目录。

**③ latency README 的头文件名与 payload 范围均与代码不符**

- [ubench/offchip_latency/datacenter/README.md:L6-L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/README.md#L6-L15) — README 让读者去改 `krnl_ubench.h` 并给出 `payload(256)` 的代码片段；但实际文件是 `src/krnl_config.h`（目录里根本没有 `krnl_ubench.h`，已核实），且真实循环从 `payload(16)` 起。
- [src/host.cpp:L99](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L99) — `for (int payload(16); ...)`：16 个 int = 64B，与 README 正文 L3 的「64B to 1MB」一致、与 README 代码片段的 256 不一致——正文改过、片段没改的典型文档滞后。
- [src/host.cpp:L195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L195) — 输出行把吞吐（GB/s）标成 `Latency`。换算关系（u4-l2 已推导）：\( latency_{ns} = WIDTH\_FACTOR \times 4 / bw_{result}\)。顺带注意这里 payload 打印用的是 `payload*4/...`，是对的——反衬带宽版 L173 的 `i*4/...` 确系笔误。

**④ auto_collect 脚本是 Python 2 语法**

- [generate_microbenchmarks.py:L91](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L91) — 裸 `print "..."` 语句；流带宽版同构脚本同样位置（`streaming_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py` L66）也有。Python 3 下直接 `SyntaxError`。此外脚本里的整数除法用 `/`（Python 2 语义），迁移时需一并改为 `//`（u5-l2 已详述迁移清单）。

**⑤ 空分支骨架与其他测量卫生问题**（已在 4.1 详述，此处归档）：

- [src/host.cpp:L115-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L115-L128) — `is_emulation()` 与 `else` 两个分支逐行相同（都是 `XCL_MEM_DDR_BANK1`），是模板遗留的空骨架；改 bank 时容易误以为只需改一处。
- `perf_analyze` 无条件执行（[Makefile:L134](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L134)，见 4.2.3 的隐患分析，**待本地验证**）。

汇总表：

| 问题 | 位置 | 影响 | 修复方向 |
| --- | --- | --- | --- |
| latency 链接 404 | 根 README L50 | 找不到指南 | 改为 `offchip_latency` |
| 链接指向 2020.2 分支 | 根 README L36-L46 | 分支不存在即失效 | 改相对路径 |
| README 指向不存在的 `krnl_ubench.h` | latency datacenter README L6 | 照做会迷路 | 改为 `src/krnl_config.h` |
| README 片段 payload=256 ≠ 代码 16 | 同上 L12-L15 | 复现实验对不上 | 以 src/ 为准改文档 |
| "Latency" 单位标 GB/s | latency host.cpp L195 | 误读结果 | 换算或改打印 |
| payload 打印恒 0.0000038MB | bandwidth host.cpp L173 | 报表不可用 | 改 `payload*4` |
| 魔数 0.000010000 硬编码 | bandwidth host.cpp L172 | 改 NUM_ITERATIONS 即静默失真 | 引用契约头常量 |
| Python 2 脚本 | auto_collect `generate_microbenchmarks.py` L91/L66 | Python 3 无法运行 | print 加括号、`/`→`//` |
| 空分支骨架 | bandwidth host.cpp L115-L128 | 改 bank 漏改分支 | 合并分支 |
| `-O0` 编译主机 | Makefile L53 | 微小噪声 | 改 `-O2` |

#### 4.3.4 代码实践

**实践目标**：建立你自己的「待确认项」台账，并为每个问题定位证据行号。

**操作步骤**：

1. 用下面的命令系统性扫描仓库，复现本节清单并寻找新条目：

```bash
# 找出所有与实际目录名不匹配的文档链接模式
grep -rn "off-chip_latency" --include="*.md" .
# 找出所有指向 2020.2 分支的链接
grep -rn "tree/2020.2" --include="*.md" .
# 找出 Python 2 裸 print（排除 print( 形式）
grep -rn "print " --include="*.py" . | grep -v "print("
# 确认文档提到的头文件是否存在
find . -name "krnl_ubench.h"
```

2. 对每条命中记录：文件、行号、实际行为、建议修复，填入自己的表格。
3. 挑一条低风险问题（如根 README 的链接拼写）在你自己的 fork 中修复，练习「文档也是代码」的维护流程。

**需要观察的现象**：上述每条 grep 的命中数与本讲清单是否一致；是否发现清单之外的新问题（例如其他 README 的同类滞后）。

**预期结果**：`off-chip_latency` 命中 1 处（根 README L50）；`tree/2020.2` 命中 4 处；裸 print 命中 auto_collect 各套脚本；`krnl_ubench.h` 的 find 无结果。均可本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「README 代码片段滞后」在这类仓库里高发？

**答案**：README 里的片段是手工粘贴的静态快照，没有编译器检查。代码演进了（payload 256→16、文件改名），快照不会自动更新。u7-l1 讲过 Xilinx 示例仓库本有 `readme_gen.py` 从 `description.json` 单一来源生成 README 的机制，但 uBench 没有配置 description.json，文档链处于「手工维护、无一致性检查」状态——这正是滞后高发的结构性原因。

**练习 2**：判断下列现象属于「系统性误差」「随机误差」还是「文档问题」：(a) payload 打印恒为 0.0000038MB；(b) 同一 payload 多次运行带宽波动 ±3%；(c) 启动开销计入分母。

**答案**：(a) 文档/报表问题（确定性的打印 bug，不影响计算值，只影响展示）；(b) 随机误差（用多次测量取中位数抑制）；(c) 系统性误差（方向恒定、幅度近似常数，需换测量口径剔除）。三者的处置手段完全不同：修打印、取中位数、换 cl_event——先分类，再动手。

## 5. 综合实践

**任务：撰写一份《uBench 读带宽微基准测量误差分析与改进报告》**，把本讲三个模块串成一份完整文档。建议结构：

1. **误差源清单与量级估计**：按 4.1.2 的表格扩充，对每项给出「窗口内/外」「常数/随 payload 变化」「消除手段」三列；用理想时间公式 \( T_{ideal} = NUM\_ITERATIONS \times (payload/WIDTH\_FACTOR)/f_{clk} \) 算出 11 个 payload 档的理论内核时间，并估算每档的启动开销相对占比，画出「误差 vs payload」的定性曲线。

2. **改进方案设计（内核级计时 + 多次取中位数）**：给出伪代码，两条正交手段同时上：

```text
# 示例伪代码：cl_event 内核级计时 + R 次取中位数
for payload in [256, 512, ..., 262144]:
    setup_buffers(payload)          # 分配、迁移，不计时
    times = []
    for r in 1..R:                  # R = 5 或更多
        evt = enqueueTask(kernel, event=evt)
        finish()
        times.append(evt.END_ns - evt.START_ns)   # 剔除启动开销
    t_med = median(times)                         # 剔除随机离群
    bw = payload * 4 * NUM_ITERATIONS * NUM_PORT / t_med   # 引用契约头常量，禁用魔数
    print(payload * 4 / 1MB, bw)                  # 修掉打印 bug
```

3. **profiling 验证计划**：说明如何用 `make PROFILE=yes` + `perf_analyze` 的每端口字节数验证公式分子、用报告内核时间验证你的 `t_med`。

4. **仓库待确认项汇总**：把 4.3.3 的表格抄录并补充你自己 grep 出的新条目，每条附证据行号与「已确认/待本地验证」标记（如 `perf_analyze` 在无 profile 时的行为、2020.2 分支是否存在）。

无硬件也能完成第 1、2、4 部分；第 3 部分可写成实验计划，标注「待本地验证」。

## 6. 本讲小结

- 主机 chrono 计时窗口混入 setArg、内核启动链路、完成同步与调度抖动：误差是**方向恒定的系统性偏差**，量级几十 µs，在小 payload 档可造成 5%–20% 的带宽低估，大 payload 档可忽略。
- 系统性偏差用测量口径剔除（`cl_event` 双时间戳——队列的 `CL_QUEUE_PROFILING_ENABLE` 其实早已打开），随机噪声用统计手段抑制（多次测量取中位数）；两者正交，缺一不可。
- 仓库有三条现成的内核级数据路径：`cl_event`（只改代码）、`xrt.ini`（不重建，嵌入式工程已内置）、`make PROFILE=yes` → `--profile_kernel` → `perf_analyze`（硬件监视器，可独立验证带宽公式的分子）。
- 公式魔数 `0.000010000` 与 `NUM_ITERATIONS` 的硬编码配对是最危险的一处静默失真点；payload 打印 bug 让带宽版报表的档位信息完全丢失。
- 已知问题清单（404 链接、2020.2 分支链接、`krnl_ubench.h` 幻影文件、payload 256≠16、"Latency" 误标、Python 2 脚本、空分支骨架、`-O0`）均已核实并给出证据行号；读文档永远以 `src/` 与 ini 为准。

## 7. 下一步学习建议

本讲是学习手册的收官篇。接下来建议：

1. **动手收官**：完成第 5 节综合实践的报告，并把 4.1.4 的 `cl_event` 改造实做在你自己 fork 的实验目录里——这是把整本手册的测量知识变成肌肉记忆的最后一步。
2. **对照论文**：拿 FPGA 2021 论文（根 README 引用）中的实测曲线，检查论文报告的带宽口径与本仓库主机计时口径的关系，思考「论文数字里含多少启动开销」。
3. **延伸阅读源码**：`common/utility/`（u7-l1 讲过）里的工具链可以帮你把修复后的 README 重新生成化；Vitis 文档中 `xrt.ini` 的 `[Debug]` 段与 `--profile_kernel` 选项还有 `data`/`stall`/`exec` 等更细的监视维度，值得在真机上逐一试过。
4. **如果继续二次开发**：回到 u7-l2 的 mixed_rw 基准，把本讲的「cl_event + 中位数 + 引用常量」三条规范直接写进它的 host.cpp，让你的新基准从第一天起就是一份可审计的测量仪器。
