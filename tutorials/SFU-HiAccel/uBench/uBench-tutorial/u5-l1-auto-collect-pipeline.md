# auto_collect 总体流程：config.py 参数空间与批量生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `config.py` 中七个配置项各自控制什么，以及它们与「影响内存带宽的五因素」（内核频率、并发端口数、端口位宽、最大突发长度、连续访问数据量）的对应关系。
2. 跟踪 `generate_microbenchmarks.py` 的六层嵌套循环：它如何为每一个参数组合创建一个目录、依次调用四个生成器、并拼出目录名。
3. 理解 `runAll.sh` 的生成逻辑与内容结构，知道批量跑通所有设计时每一步发生了什么。
4. 在动手改参数之前，先在纸上推演会生成多少个目录、每个目录叫什么名字——这是使用 auto_collect 的核心安全网。

## 2. 前置知识

本讲不再深入内核与主机代码细节（那是 u2、u3 单元的内容），而是站在「工程组织」的视角看自动化。需要你先具备以下认知（前置讲义已建立）：

- **五件套骨架**（u1-l4）：一个微基准工程由 `krnl_config.h` + 内核源码 + `host.cpp` + `ubench.ini` + `Makefile` 构成。auto_collect 做的事情，就是用程序按参数组合批量生产这五件套。
- **手动调参的痛点**（u3-l2）：手动加端口要四处联动（内核签名、bundle、主机 `NUM_PORT`、ini 的 `sp=` 行），改位宽要动 `krnl_config.h`，改突发要动 pragma。每做一次参数实验都要人肉复制目录再改五六个地方——auto_collect 就是把这个过程脚本化。
- **DDR 与 HBM 的差异落点**（u3-l3）：内存类型是链接期决策（`ubench.ini` 的 `sp=` 行）加运行期决策（主机 `cl_mem_ext_ptr_t` 的 flag），内核源码本身不变。所以 `MEMORY_TYPE` 在 config 中是一个「结构体列表」，同时携带 bank 名（喂给 ini）和 flag（喂给主机）。
- **Python 2 遗留**（u1-l1、u4-l3 都提过）：这套脚本写于 Vitis 2020.2 时代，使用 Python 2 语法，顶层脚本有一处裸 `print` 语句，用 `python3` 直接运行会在解析阶段就报语法错误。

另外补充一个术语：**参数空间（parameter space）**。当我们说「七个维度的参数空间」时，指的是每个配置项是一个取值列表，所有维度的笛卡尔积（交叉积）构成全部要测的参数组合。目录总数等于各维度元素个数的连乘积：

\[ N = \prod_{i} |L_i| \]

其中 \( |L_i| \) 是第 \( i \) 个列表的长度。后面会看到有一个配置项（`CONSECUTIVE_DATA_SIZE`）不是列表而是字典，它不参与这个连乘。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ubench/offchip_bandwidth/datacenter/auto_collect/config.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py) | 用户配置文件，只有 14 行，定义七维参数空间 |
| [ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py) | 生成流水线主脚本：六层嵌套循环 + 调度四个生成器 + 汇总 runAll.sh |
| [ubench/offchip_bandwidth/datacenter/auto_collect/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/README.md) | auto_collect 使用说明，逐项解释七个配置参数 |
| [ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py) | 生成器：内核源码 `krnl_ubench.cpp` 与配置头 `krnl_config.h`（本讲只看它的函数签名，精读留给 u5-l2） |
| [ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py) | 生成器：主机程序 `host.cpp`（本讲只看签名） |
| [ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py) | 生成器：链接配置 `ubench.ini`（本讲只看签名） |
| [ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py) | 生成器：构建脚本 `Makefile`（本讲只看签名与 `--kernel_frequency` 落点） |

auto_collect 目录下一共就这七个文件（可用 `ls` 验证）：一个配置、一个主脚本、一份 README、四个生成器。本讲聚焦前三者，把四个生成器当作「黑盒函数」只关心其输入参数；u5-l2 会逐一打开这四个黑盒。

## 4. 核心概念与源码讲解

### 4.1 参数空间：config.py 的七个配置项

#### 4.1.1 概念说明

u3-l2 讲的是「手动改一个参数要动哪些文件」，本模块讲的是「把要扫的所有参数值写在一个地方」。`config.py` 就是整个流水线唯一的用户入口：你想做什么实验，只改这一个文件，然后运行主脚本，剩下全部自动完成。

七个配置项与五因素（u1-l1 建立的核心框架）的对应关系如下：

| config 配置项 | 对应因素 / 类别 | 形态 | 在生成五件套时的落点 |
|---|---|---|---|
| `KERNEL_FREQ` | 因素 1：内核频率 | 列表 | 生成 Makefile 的 `--kernel_frequency` |
| `NUM_CONCURRENT_PORT` | 因素 2：并发端口数 | 列表 | 内核签名端口个数 + ini 的 `sp=` 行数 + 主机 `NUM_PORT` |
| `PORT_WIDTH` | 因素 3：端口位宽 | 列表 | 生成的 `krnl_config.h` 的 `DWIDTH` |
| `MAX_BURST_LENGTH` | 因素 4：最大突发长度 | 列表 | 内核 pragma 的 `max_read/write_burst_length` |
| `CONSECUTIVE_DATA_SIZE` | 因素 5：连续访问数据量 | **字典（起止范围）** | 主机 payload 扫描循环的起点与终点 |
| `ACCESS_TYPE` | 附加维度：读 / 写 | 列表 | 内核端口方向（`in*` / `out*`）、ini 端口名、主机缓冲区属性 |
| `MEMORY_TYPE` | 附加维度：DDR / HBM | **字典列表** | ini 的 `sp=` 目标 bank 名 + 主机 `cl_mem_ext_ptr_t` 的 flag + runAll 的 DEVICE |

注意两个特殊形态：

- `CONSECUTIVE_DATA_SIZE` 是 `{'START_SIZE':1, 'STOP_SIZE':1024}`，单位 KB。它不是「多个取值中选一个」，而是定义**每个**生成的工程内部 payload 扫描的起止范围（1 KB 到 1024 KB = 1 MB，与 u2-l3 分析过的手写版 host.cpp 从 256 扫到 262144 个 int 即 1 KB→1 MB 完全一致）。所以它**不参与目录数的连乘**——连续访问数据量是在运行时由主机逐档扫的，不是靠生成多个目录实现的。
- `MEMORY_TYPE` 的每个元素是一个四字段字典：`BANK_TYPE`（进目录名）、`BANK_FLAG`（喂给主机生成器）、`BANK_NAME`（喂给 ini 生成器）、`DEVICE_NAME`（喂给 runAll.sh 的 `DEVICE=`）。一个维度同时携带三份下游信息，这是「跨工具契约」（u3-l3 讲过 ini 与主机必须手工对齐）在生成器层面的解法——由同一个数据源派生，天然对齐。

#### 4.1.2 核心流程

读 config 并计算参数空间规模的过程可以描述为：

```text
读取 config.py 的七个配置项
    │
    ├─ 六个列表维度：KERNEL_FREQ × NUM_CONCURRENT_PORT × PORT_WIDTH
    │                × MAX_BURST_LENGTH × ACCESS_TYPE × MEMORY_TYPE
    │   → 目录总数 N = 各列表长度连乘
    │
    └─ 一个范围维度：CONSECUTIVE_DATA_SIZE
        → 不影响 N，只写进每个 host.cpp 的 payload 循环边界
```

默认配置下：\( N = 1 \times 1 \times 1 \times 5 \times 2 \times 2 = 20 \) 个设计目录。观察默认值可以发现作者实验设计的侧重：频率、端口数、位宽都只取单值（300 MHz、4 端口、128 bit），唯独 `MAX_BURST_LENGTH` 一口气取 5 档（16/32/64/128/256）——突发长度是五个因素里最「隐形」的一个（改它不改变任何源码结构，只改 pragma 数值），也因此最值得系统性扫描。

#### 4.1.3 源码精读

config.py 全文只有 14 行，先看六个标量维度：

[config.py:L7-L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L7-L12) — 依次定义内核频率（单值 300）、并发端口数（单值 4）、端口位宽（单值 128）、最大突发长度（5 档）、连续访问数据量起止范围（1 KB→1024 KB，注释明确单位是 KB）、访问类型（读/写两种）。

再看结构化的内存类型维度：

[config.py:L13-L14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L13-L14) — `MEMORY_TYPE` 是两个字典组成的列表：DDR 条目携带 U200 平台名 `xilinx_u200_xdma_201830_2`，HBM 条目携带 U280 平台名 `xilinx_u280_xdma_201920_3`；两者的 `BANK_FLAG` 当前都是 `'0 | XCL_MEM_TOPOLOGY'`（拓扑式编址，即 bank 0，与 u2-l2 讲过的 HBM 风格 flag 相同），`BANK_NAME` 分别为 `DDR[0]` 与 `HBM[0]`（u3-l3 讲过：这会原样进入 ini 的 `sp=` 行）。

README 对这七个配置项逐一给出了说明与示例：

[README.md:L6-L47](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/README.md#L6-L47) — 按端口数、位宽、突发长度、连续数据量、访问类型、DDR/HBM、频率七个条目解释 config.py 的写法，每条附配置片段。另外注意 [README.md:L2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/README.md#L2) 的措辞是 "four parameters" 却紧接着列了 5 个编号条目、下文又展开 7 个配置项——这是仓库文档计数不一致的小瑕疵，以 config.py 实际的七个配置项为准（与 u1-l2 的结论一致：目录名与文档有滞后时，以代码为准）。

#### 4.1.4 代码实践

**实践目标**：不动任何代码，纯靠读 config.py 计算「当前默认配置会生成几个目录、叫什么名字」。

**操作步骤**：

1. 打开 `ubench/offchip_bandwidth/datacenter/auto_collect/config.py`，数出每个列表的长度。
2. 按公式 \( N = \prod |L_i| \) 连乘（`CONSECUTIVE_DATA_SIZE` 是字典，跳过）。
3. 按 4.2.3 将要讲的目录名模板，写出其中任意三个目录的全名。

**需要观察的现象**：`MAX_BURST_LENGTH` 是唯一长度大于 1 的标量维度，目录名的差异只会出现在 burst 段。

**预期结果**：\( N = 1 \times 1 \times 1 \times 5 \times 2 \times 2 = 20 \)。目录名形如 `RD_DDR_300MHz_4port_128bit_16max_burst_length`（burst 段取 16/32/64/128/256，访问类型 × 内存类型共 4 种组合，4 × 5 = 20）。此结果为静态推演，与脚本实际行为的一致性在 4.2.4 / 综合实践中验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CONSECUTIVE_DATA_SIZE` 用字典 `{START_SIZE, STOP_SIZE}` 而其他维度用列表？如果它也改成列表（如 `[1, 4, 16, 64, 256, 1024]`），生成行为会怎么变？

**答案**：连续访问数据量的本质是「运行时逐档扫描」的量——u2-l3 精读过的手写版 host.cpp 就是在一个二进制内用 payload 倍增循环从 1 KB 扫到 1 MB，一次运行拿到整条带宽-数据量曲线。所以它只需要告诉主机生成器「扫描窗口的边界」，不需要为每个取值生成一个目录。若改成列表，按主脚本的写法它甚至不会被遍历（主脚本的六层循环里根本没有它），目录数不变，只是 `START_SIZE`/`STOP_SIZE` 这种字典取法会失效导致脚本报 `KeyError`。

**练习 2**：`MEMORY_TYPE` 里为什么要同时放 `BANK_NAME`、`BANK_FLAG`、`DEVICE_NAME` 三个字段？删掉 `DEVICE_NAME` 会破坏什么？

**答案**：因为一个「内存类型」选择要在三个不同工具链产物里落地：`BANK_NAME` 进链接期 `ubench.ini` 的 `sp=` 行（v++ 用），`BANK_FLAG` 进运行期 `host.cpp` 的 `cl_mem_ext_ptr_t` flags（XRT 用），`DEVICE_NAME` 进 `runAll.sh` 的 `make check DEVICE=...`（构建系统用，U200 对应 DDR、U280 对应 HBM）。删掉 `DEVICE_NAME` 后，[generate_microbenchmarks.py:L61](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L61) 取 `memory_type['DEVICE_NAME']` 时会抛 `KeyError`。这正是 u3-l3 指出的「跨工具契约无编译期检查」问题在生成器方案下的缓解：三份信息同源派生，改一处全联动。

**练习 3**：默认 config 的 `BANK_FLAG` 是 `'0 | XCL_MEM_TOPOLOGY'`，而手写版 read/DDR 工程用的是 `XCL_MEM_DDR_BANK1` 风格宏（u2-l2 讲过）。两者什么关系？

**答案**：它们都是 `cl_mem_ext_ptr_t.flags` 的取值方式，且都表示「bank 0 / 第 0 个内存通道」：`0 | XCL_MEM_TOPOLOGY` 是拓扑式编址（数字即 bank 序号），`XCL_MEM_DDR_BANKn` 是老式 bank 掩码宏。生成的工程选了拓扑式写法（与 HBM 版一致），好处是 DDR/HBM 两种内存可以用同一套 flag 模板，只换 `BANK_NAME`。

### 4.2 生成主流程：六层嵌套循环与目录命名

#### 4.2.1 概念说明

`generate_microbenchmarks.py` 是流水线的「总调度」。它做三件事：

1. **建根目录**：在当前工作目录下创建 `uBenchDesignDir/`，所有生成的设计都放里面。
2. **遍历参数空间**：六层嵌套 for 循环（频率 → 端口数 → 位宽 → 突发 → 访问类型 → 内存类型），每个叶子组合生成一个完整微基准目录。
3. **汇总批跑脚本**：每生成一个设计就往 `runAll.sh` 追加两行（进入目录 + make check），最后写到 `uBenchDesignDir/` 根下。

它把五件套的生产外包给四个生成器模块，自己只负责「循环 + 目录 + 调度」：

| 生成器调用 | 产物 | 关键输入参数 |
|---|---|---|
| `generateMakefile(kernel_freq)` | `Makefile` | 频率 |
| `generateConnectivity(access_type, num_concurrent_port, bank_name)` | `ubench.ini` | 访问类型、端口数、bank 名 |
| `generateHostCode(access_type, num_concurrent_port, port_width, start, stop, bank_flag)` | `src/host.cpp` | 访问类型、端口数、位宽、payload 起止、bank flag |
| `generateKernelCode(access_type, num_concurrent_port, port_width, max_burst_length)` | `src/krnl_ubench.cpp` + `src/krnl_config.h` | 访问类型、端口数、位宽、突发长度 |

注意参数的流向恰好印证了 u3-l2 的「手动改参联动表」：位宽只进内核/主机生成器（`krnl_config.h` 派生），突发只进内核生成器（pragma），频率只进 Makefile 生成器，端口数进三者，内存类型进 ini 与主机。**自动化的本质就是把我们上一单元手工做过的联动表编码成函数参数。**

#### 4.2.2 核心流程

```text
os.getcwd() 记为 baseDir
在 baseDir 下创建 uBenchDesignDir/
初始化 runall_script = ['#!/bin/bash\n\n']

for kernel_freq in KERNEL_FREQ:            # 第 1 层
  for num_concurrent_port in ...:          # 第 2 层
    for port_width in ...:                 # 第 3 层
      for max_burst_length in ...:         # 第 4 层
        for access_type in ...:            # 第 5 层
          for memory_type in ...:          # 第 6 层（最内层）
            目录名 = f"{access_type}_{BANK_TYPE}_{freq}MHz_{port}port_{width}bit_{burst}max_burst_length"
            mkdir uBenchDesignDir/目录名/ 并 chdir 进去
            generateMakefile(freq)                      → Makefile
            generateConnectivity(type, ports, bank)     → ubench.ini
            mkdir src/ 并 chdir 进去
            generateHostCode(...)                       → src/host.cpp
            generateKernelCode(...)                     → src/krnl_ubench.cpp, src/krnl_config.h
            chdir 回 baseDir
            runall_script += [cd 目录; \n, make check TARGET=hw DEVICE=...; \n\n]

chdir 到 uBenchDesignDir/，把 runall_script 写入 runAll.sh
print "Microbenchmark Generation Done!"
```

两个值得注意的执行细节：

- **工作目录敏感**：脚本用 `os.getcwd()` 作 baseDir，且生成器都以相对文件名写文件（依赖先 `os.chdir` 进目标目录），因此**必须 cd 到 auto_collect 目录内运行**；又因为 `from config import *`（[generate_microbenchmarks.py:L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L7)）按模块搜索路径找 config，从别处运行连 import 都会失败。
- **不可重复执行**：[generate_microbenchmarks.py:L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L16) 用的是 `os.mkdir`（不是 `os.makedirs(..., exist_ok=True)`），第二次运行会因 `uBenchDesignDir` 已存在而直接抛异常。重跑前要先删掉旧目录。

目录命名模板（对应 u1-l2 讲过的「目录名即参数组合」约定）：

\[ \text{name} = \text{ACCESS}\_\text{BANK}\_\text{FREQ}\,\text{MHz}\_\text{N}\,\text{port}\_\text{W}\,\text{bit}\_\text{B}\,\text{max\_burst\_length} \]

六个循环变量全部编入目录名，因此**目录名与参数组合一一对应、可反解**——这是批量实验后整理数据的关键属性。

#### 4.2.3 源码精读

先看初始化段：

[generate_microbenchmarks.py:L13-L20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L13-L20) — 记录启动目录 baseDir、拼出 `uBenchDesignDir` 绝对路径并 `os.mkdir` 创建；随后初始化 runAll 脚本内容列表，先放入 bash shebang 与空行。

接着是六层循环的骨架与目录名拼接：

[generate_microbenchmarks.py:L22-L35](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L22-L35) — 六层 for 从 `KERNEL_FREQ` 到 `MEMORY_TYPE` 逐层嵌套；叶子处用字符串拼接构造 `benchmarkDesignName`（访问类型_内存类型_频率MHz_端口数port_位宽bit_突发max_burst_length），创建该目录并 `os.chdir` 进入，为后续生成器的相对路径写入做准备。

然后是四次生成器调度：

[generate_microbenchmarks.py:L37-L58](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L37-L58) — 依次调用 `generateMakefile`（频率）、`generateConnectivity`（访问类型 + 端口数 + `BANK_NAME` 生成 ubench.ini）、创建 `src/` 后调用 `generateHostCode`（访问类型 + 端口数 + 位宽 + 起止数据量 + `BANK_FLAG` 生成 host.cpp）、`generateKernelCode`（访问类型 + 端口数 + 位宽 + 突发长度生成内核与配置头）。注意四次调用的参数并集恰好覆盖 config 的全部信息——除了一项：`KERNEL_FREQ` 只喂给了 Makefile 生成器（见下）。

频率的最终落点在 Makefile 生成器内部：

[makefile_gen.py:L75](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L75) — 生成的 Makefile 中 `CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps --kernel_frequency <freq>`，即频率通过 v++ 的 `--kernel_frequency` 选项在 HLS 综合时指定。这印证了 u3-l2 的结论：手写示例工程的 Makefile 没有这个选项（频率由 HLS 默认时序收敛决定），`--kernel_frequency` 是 auto_collect 生成版的独有增强。

内核生成器内部还会先生成配置头：

[kernelcode_gen.py:L23-L26](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L23-L26) — `generateKernelCode` 的第一件事是调用 `generateKernelConfigCode(port_width)` 生成 `krnl_config.h`（位宽写入 `DWIDTH`，`INTERFACE_WIDTH`/`WIDTH_FACTOR` 由此派生，u2-l1 讲过派生链），然后再拼 `krnl_ubench.cpp`。也就是说每个设计目录的 `src/` 下有三个文件：`krnl_config.h`、`krnl_ubench.cpp`、`host.cpp`，加上目录级的 `Makefile` 与 `ubench.ini`，正好是 u1-l4 的五件套。

文件尾部还有一段被注释掉的历史代码：

[generate_microbenchmarks.py:L74-L89](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L74-L89) — 一段 KNN 单 PE 设计空间探索的残迹（调 HLS 综合并解析 csynth 报告中的 BRAM/DSP/FF/LUT/URAM 利用率），说明这套「config + 生成器」架构最初也服务于案例研究的设计空间探索，后被裁剪为纯微基准生成。阅读时可整段跳过。

最后一行是 Python 2 的裸 print 语句：

[generate_microbenchmarks.py:L91](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L91) — `print "Microbenchmark Generation Done!"`。经逐脚本核对，整个 auto_collect 目录**只有这一行**是 Python 2 专有语法（其余四个生成器里的 print 都带括号，两个版本兼容）。这意味着用 Python 3 迁移顶层脚本只需把这一行改成 `print("Microbenchmark Generation Done!")`——但注意 Python 3 在解析阶段就会拒绝裸 print，所以**不改这一行，python3 连脚本都启动不了**；反之有 python2 环境则可直接运行。

#### 4.2.4 代码实践

**实践目标**：验证「纸上推演的目录清单」与「脚本实际生成的目录清单」一致（默认配置即可）。

**操作步骤**：

1. 推演：按 4.1.4 写出默认配置的 20 个目录名。
2. 把整个 auto_collect 目录复制到仓库外的一个临时目录（例如 `cp -r ubench/offchip_bandwidth/datacenter/auto_collect /tmp/auto_collect_try`），避免在仓库里留下生成物、也避免误改源码。
3. `cd /tmp/auto_collect_try`，二选一：
   - 有 python2：`python2 generate_microbenchmarks.py`
   - 只有 python3：先把第 91 行改为 `print("Microbenchmark Generation Done!")`，再 `python3 generate_microbenchmarks.py`
4. `ls uBenchDesignDir/ | sort > /tmp/actual.txt`，与你的推演清单 `diff` 对比。
5. 重复运行一次脚本，观察报错。

**需要观察的现象**：生成的每个目录内有 `Makefile`、`ubench.ini`、`src/`（含 `krnl_config.h`、`krnl_ubench.cpp`、`host.cpp`）；第 5 步会因 `uBenchDesignDir` 已存在抛异常。

**预期结果**：20 个目录、名字与推演一致；`diff` 为空；重复运行报 `FileExistsError`（Python 3）或 `OSError: File exists`（Python 2）。本实践不依赖 Vitis/硬件，任何装了 Python 的机器都能做（实际输出待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：脚本为什么在创建设计目录后要 `os.chdir` 进去、生成完又 `chdir(baseDir)` 回来？如果全部改成绝对路径写入但不 chdir，功能是否等价？

**答案**：因为四个生成器（makefile_gen 等）内部都用相对文件名（如 `open('ubench.ini', 'w')`）写文件，主脚本靠 chdir 把「当前目录」切换到目标设计目录，使生成器的相对路径落到正确位置；`src/` 下的两个文件同理靠先 chdir 进 `src/` 实现。改成绝对路径写入功能等价（文件内容不变），但需要给每个生成器加路径参数，改动面更大——现状是「主脚本管目录、生成器管内容」的职责分离。

**练习 2**：六层循环的嵌套顺序（频率最外、内存类型最内）会影响生成结果的集合吗？会影响什么？

**答案**：不影响生成的目录集合（笛卡尔积与循环顺序无关），也不影响目录名。它影响的是（a）目录在文件系统里被创建的先后顺序，从而影响 runAll.sh 中命令的排列顺序（runAll 按生成顺序追加），以及（b）若中途出错，已完成的目录是哪些（最内层是内存类型，意味着每轮先做完 DDR 再做 HBM）。对批量实验而言，runAll 的执行顺序通常无测量上的影响。

**练习 3**：主脚本 import 了 `re`、`math`、`subprocess`、`shutil` 四个标准库但正文从未使用（只有被注释的历史代码用到）。这说明了什么？有什么风险？

**答案**：说明脚本是从更早、更复杂的版本（那段 KNN 设计空间探索代码需要 subprocess 跑综合、解析报告）裁剪而来，裁剪时只删了正文没删 import。风险很小（四个都是标准库，import 必然成功），但它和 L74-L89 的注释代码一起构成「死代码」，阅读时应能识别并跳过，不要在这些行里寻找现行为。

### 4.3 runAll 批跑：从生成到执行

#### 4.3.1 概念说明

生成 20 个目录只是准备了 20 份五件套源码，还差「编译 + 上板运行」才能拿到数据。`runAll.sh` 就是把所有设计的构建运行命令串成一个顺序执行的批处理脚本，让你在真机环境里一条命令跑完整个参数扫描。

它与生成阶段的关系是「边生成边追加」：主脚本在每生成一个设计后立刻往 `runall_script` 列表追加两行命令，全部循环结束后统一写入 `uBenchDesignDir/runAll.sh`。

#### 4.3.2 核心流程

```text
（生成循环内，每设计两条 + 一个空行）
cd <uBenchDesignDir 的绝对路径>/<设计目录>;
make check TARGET=hw DEVICE=<该设计的 DEVICE_NAME>;

（循环结束后）
chdir uBenchDesignDir/
把累积的所有行写入 runAll.sh
```

执行 `bash runAll.sh` 时的语义：逐个进入设计目录，跑 `make check TARGET=hw DEVICE=...`——u1-l3 讲过 `make check` 会串联 all/build/exe 并自动运行应用。三点关键属性：

1. **绝对路径**：`cd` 的路径来自 `os.path.join(uBenchDesignDir, benchmarkDesignName)`，而 `uBenchDesignDir` 基于 `os.getcwd()` 是绝对路径，所以 runAll.sh 从任何位置执行都能正确进入目录（前提是目录没被移动）。
2. **TARGET=hw 硬编码**：所有命令固定打真机位流（编译耗时以小时计）；想先做功能验证需手动改成 `TARGET=sw_emu`，且需按 u1-l3 准备 emconfig.json。
3. **DEVICE 逐设计切换**：DDR 设计用 U200 平台名、HBM 设计用 U280 平台名——一份 runAll 跨两块卡跑，前提是两块卡对应的 shell 都装好。

#### 4.3.3 源码精读

runAll 内容的累积发生在生成循环的末尾：

[generate_microbenchmarks.py:L60-L63](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L60-L63) — 每个设计生成完毕后，向 `runall_script` 追加 `cd <设计目录绝对路径>;` 与 `make check TARGET=hw DEVICE=<DEVICE_NAME>;` 两行加一个空行，然后 `os.chdir(baseDir)` 回到根目录继续下一轮循环。`TARGET=hw` 与 `DEVICE_NAME` 都在此处定死。

汇总写入：

[generate_microbenchmarks.py:L65-L71](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L65-L71) — 循环结束后 chdir 到 `uBenchDesignDir/`，以写模式打开 `runAll.sh`，`f.seek(0)`（对新建文件是冗余的模板残留，无害）后 `writelines` 一次性写入全部累积行。runAll.sh 因此位于所有设计目录的上一级，与 20 个设计目录平级。

对应地，auto_collect 的 README 对产物位置与用法只有一句概述：

[README.md:L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/README.md#L4) — 说明生成的设计放在 `./uBenchDesignDir`，可用 `runAll.sh` 自动跑完所有微基准设计。README 没有展开 runAll 的内部结构（如 TARGET=hw、跨平台 DEVICE 切换），这些要从上面两处源码自行获得——再次体现「以代码为准」的阅读原则。

#### 4.3.4 代码实践

**实践目标**：不运行任何硬件命令，仅通过阅读源码复写出一份 runAll.sh 的预期内容。

**操作步骤**：

1. 在 4.2.4 生成的 `uBenchDesignDir/` 里 `cat runAll.sh`（或在你手写的推演清单基础上按循环顺序给每个目录补两行命令）。
2. 数一数总行数，检查 cd 与 make 行是否成对、顺序是否与目录生成顺序一致（频率→端口→位宽→突发→访问→内存的字典序）。
3. 找出 `DEVICE=` 后面出现的不同取值及其出现条件。

**需要观察的现象**：每个 DDR 设计的 DEVICE 是 `xilinx_u200_xdma_201830_2`，HBM 设计是 `xilinx_u280_xdma_201920_3`；两者交替出现（内存类型是最内层循环）。

**预期结果**：20 个设计 → 40 条命令行 + 20 个空行 + 1 行 shebang + 1 空行 = 62 行；`grep -c '^cd ' runAll.sh` 与 `grep -c '^make check' runAll.sh` 都等于 20（以实际生成文件为准，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：runAll.sh 里 `make check TARGET=hw` 失败在其中一个设计上（比如某组参数时序收敛失败），后续设计还会执行吗？如何改进？

**答案**：会继续执行——追加的命令以 `;` 结尾而非 `&&`，且脚本没有 `set -e`，单条命令失败不中断 bash 继续读下一条。这其实是批量扫描期望的行为（一个设计失败不应废掉整晚的实验）。若想在结束时汇总失败清单，可以包一层 `make check ... || echo "$PWD FAILED" >> ../failures.log` 之类的记录（改进思路，仓库当前未提供）。

**练习 2**：为什么 runAll.sh 用绝对路径 `cd`，而不是 `cd ./设计目录名`？

**答案**：因为脚本执行者的当前目录不可预知（可能在 `uBenchDesignDir` 里执行 `bash runAll.sh`，也可能在别处执行其绝对路径）。生成时 `uBenchDesignDir` 已是绝对路径（`os.getcwd()` 拼接而来），写入绝对路径使命令对执行位置不敏感。代价是目录整体移动后脚本失效，需重新生成。

**练习 3**：默认 20 个设计全部 `TARGET=hw` 跑完需要多久量级？这提示了什么实验策略？

**答案**：u1-l3 讲过 hw 位流的 v++ 编译是小时级操作，20 个设计顺序编译加运行是「隔夜」量级；且 HBM 设计还需要 U280 卡在场。这提示两点：（a）调试阶段先把 runAll 里的 `TARGET=hw` 改成 `sw_emu` 快速验证全部工程能编译能跑（虽然带宽数值无物理意义，u1-l3 的结论）；（b）正式扫描前用 4.1 的连乘公式控制目录数——每多一个维度取值，编译时间成倍上涨。

## 5. 综合实践

**任务：一次受控的参数空间扩展——先推演，后验证。**

你在 u3-l2 手工构造过 `4ports_256bit`（auto_collect 的手工版），现在反过来走自动化路径：

1. **改配置**（在临时副本里，不动仓库）：把 `config.py` 改为
   `KERNEL_FREQ = [300]`、`NUM_CONCURRENT_PORT = [2, 4]`、`PORT_WIDTH = [512]`、`MAX_BURST_LENGTH = [16, 256]`，其余两项保持默认。
2. **纸上推演**：按 \( N = 1 \times 2 \times 1 \times 2 \times 2 \times 2 = 16 \) 个目录，按循环顺序（端口数在 burst 之外层）写出全部 16 个目录名，例如第一个是 `RD_DDR_300MHz_2port_512bit_16max_burst_length`，最后一个（端口 4、burst 256、写、HBM）是 `WR_HBM_300MHz_4port_512bit_256max_burst_length`。
3. **运行验证**：按 4.2.4 的步骤跑脚本（python2，或先迁移第 91 行的 print 后用 python3），`ls uBenchDesignDir/ | sort` 与推演清单 diff，应当完全一致（待本地验证）。
4. **抽查内容**：任选一个 `RD_DDR_..._256max_burst_length` 目录，打开生成的 `src/krnl_config.h` 核对 `DWIDTH` 是否为 512，打开 `src/krnl_ubench.cpp` 数一数 `in0/in1` 两个端口与 `max_read_burst_length=256`，打开 `ubench.ini` 核对 `sp=` 是否两条且指向 `DDR[0]`，打开 `host.cpp` 找 `NUM_PORT` 与 payload 循环边界（1 KB→1 MB）。这五处抽查覆盖了 u3-l2 联动表的全部落点。
5. **对比手写版**：把你抽查的 2port/512bit 生成目录与手写工程 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/` 做 diff，记录差异清单（例如生成版内核带 `sum` 输出口、Makefile 带 `--kernel_frequency`），为 u5-l2 的生成器精读建立问题清单。

**预期结果**：16 个目录名与推演逐一致；抽查五处落点全部与配置对应；与手写版的 diff 差异集中在生成器的模板选择上。全程不需要 Vitis 与硬件；第 3 步起的所有输出以本地实际运行结果为准。

## 6. 本讲小结

- `config.py` 用七个配置项定义参数空间：六个列表维度（频率、端口数、位宽、突发长度、访问类型、内存类型）决定目录总数（连乘），`CONSECUTIVE_DATA_SIZE` 是唯一的范围维度、只写进 host.cpp 的 payload 扫描边界，不增加目录数。
- 七个配置项是五因素的两方扩展：前五项一一对应「频率/端口数/位宽/突发/连续数据量」，另加访问类型与内存类型两个实验维度；`MEMORY_TYPE` 用四字段字典同时喂 ini（BANK_NAME）、主机（BANK_FLAG）、runAll（DEVICE_NAME），把 u3-l3 的跨工具契约变成同源派生。
- `generate_microbenchmarks.py` 用六层嵌套循环遍历参数空间，每个叶子组合生成一个目录名形如 `RD_DDR_300MHz_4port_128bit_16max_burst_length` 的五件套工程，生产外包给四个生成器，参数流向与 u3-l2 的手动联动表完全吻合。
- 脚本是 Python 2 代码但全目录仅第 91 行裸 `print` 一处不兼容语法；必须从 auto_collect 目录内运行（`os.getcwd()` + `from config import *`）；`os.mkdir` 决定它不可重复执行，重跑前要删 `uBenchDesignDir`。
- `runAll.sh` 在生成循环里边生成边追加，内容为每个设计「cd 绝对路径 + `make check TARGET=hw DEVICE=...`」；命令以 `;` 结尾故单设计失败不中断批跑；DDR/HBM 设计分别绑定 U200/U280 平台名。
- 使用 auto_collect 的纪律：改 config 前先算连乘积推演目录数与目录名，调试期把 TARGET 降到 sw_emu，正式扫描控制维度取值数以约束编译总时长。

## 7. 下一步学习建议

本讲把四个生成器当黑盒，只看了函数签名与调用参数。下一讲 **u5-l2（四个代码生成器）** 将逐一打开黑盒：`kernelcode_gen.py` 如何按端口数拼接内核签名与 pragma、`hostcode_gen.py` 如何把连续数据量范围写进 payload 循环、`connectivity_gen.py` 如何生成 sp/slr/nk 三指令、`makefile_gen.py` 的模板结构，并完成四个脚本的 Python 3 迁移实战。建议你在进入 u5-l2 前，先完成本讲综合实践第 5 步的 diff 清单——带着「生成版与手写版差在哪」的具体问题去读生成器源码，效率远高于通读。之后再进入 u5-l3，对比 streaming 与 embedded 两套 auto_collect 变体的参数集差异。
