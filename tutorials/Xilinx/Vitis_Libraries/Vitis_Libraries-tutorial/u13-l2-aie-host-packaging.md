# AIE 图主机控制与 SD 卡打包

## 1. 本讲目标

本讲承接 u13-l1（ADF 图、窗口/流与 PL↔AIE 边界）与 u5-l1（v++ L2 构建流程），把视线从「图源码本身」抬升到「如何用主机程序控制这张图，并把整个系统打包成一块能启动的 SD 卡」。

学完本讲你应当能够：

- 用 `xrt::graph` 的 `reset()` / `run()` / `end()` 三件套在主机端驱动一张 ADF 图，并解释它与仿真侧 `aie.cpp` 里 `init()/run()/end()` 的对应关系。
- 读懂 `system.cfg` 的 `nk` / `sp` / `sc` 三类声明，尤其是 `sc=` 如何描述 PL↔AIE 边界的 AXI Stream 连接。
- 说出 `v++ --package`（即 `-p`）在嵌入式 Versal 平台上生成 SD 卡时，每个 `--package.*` 选项各自负责塞进哪类文件。
- 沿着 `launch_hw_emu.sh → run_script.sh → host.elf` 这条链，解释 hw_emu 如何用 QEMU 启动整个系统并判定 PASS/FAIL。

本讲全部围绕 `dsp/L2/examples/vss_fft_ifft_1d` 这个 PL+AIE 混合示例展开。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（若不熟，先复习 u13-l1、u5-l1、u4-l2）：

- **ADF 图**：用 C++ 描述的数据流图，由 `graph`（容器类）、`kernel`（计算节点）、`connect`（数据边）组成，编译后变成 AI Engine 阵列上的可执行实体。
- **PL↔AIE 边界**：AIE 阵列只认无地址的 AXI Stream，而 DDR 只能按地址访问；边界靠 PL 侧的 `mm2s`（DDR→流）与 `s2mm`（流→DDR）两个搬运内核焊接，焊接点在 AIE 图里叫 **PLIO** 端口。
- **xclbin / XSA / libadf.a**：`v++ -l` 链接阶段，PL 内核拼成硬件容器 `.xsa`，AIE 图编译成 `libadf.a`；`v++ --package` 再把二者封装成最终的 `.xclbin`（以及嵌入式平台的整张 SD 卡）。
- **xrt::device / kernel / run / bo**：原生 XRT C++ 主机 API 的核心对象（u4-l2、u4-l3 已详述）。本讲新增的只是 `xrt::graph` 这一个 AIE 专有对象。
- **sw_emu / hw_emu / hw**：Vitis 三档小写 target。sw_emu 自 2025.1 起废弃；hw_emu 用 QEMU + RTL 仿真做开发迭代；hw 是上板交付。本讲聚焦 **hw_emu**，因为它正是「打包 SD 卡 + QEMU 启动」这一完整流程的档位。

一个贯穿全讲的关键直觉：**AIE 图和 PL 内核是两套执行体，主机是唯一的「总指挥」**。主机要同时点亮 AIE 图（`xrt::graph`）和 PL 内核（`xrt::kernel`/`xrt::run`），并靠 PL 搬运内核的「收够数据」来给整条流水线收尾。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `dsp/L2/examples/vss_fft_ifft_1d/` 下（图定义在其 `L2/include/vss/` 目录）：

| 文件 | 作用 |
| --- | --- |
| `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` | 主机程序：用 `xrt::graph` 控制 AIE 图，用 `xrt::kernel/run` 驱动 mm2s/s2mm，校验结果。 |
| `dsp/L2/examples/vss_fft_ifft_1d/system.cfg` | 链接阶段系统拓扑说明：内核实例化、存储端口绑 bank、AXI Stream 连接。 |
| `dsp/L2/examples/vss_fft_ifft_1d/example.mk` | 示例的「五脏俱全」构建脚本：vss 生成 → 编译/链接 → 交叉编译主机 → 打包 SD 卡 → 启动 hw_emu。 |
| `dsp/L2/examples/vss_fft_ifft_1d/run_script.sh` | 拷进 SD 卡、在 QEMU/板子 Linux 里执行的入口脚本：设环境变量、跑 `host.elf`、判定 RC。 |
| `dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/run.sh` | 通用包装脚本（AIE2PS 变体用），source 环境后调用 `run.sh`。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp` | 仿真侧（x86sim/aiesim）的图驱动 `main()`：`init()/run()/end()`。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp` | 顶层 PLIO 图类 `tl_graph` 的定义，声明了与 PL 对接的 `input_plio`/`output_plio` 数组。 |

记忆线索：**「图（aie.hpp/cpp）+ 桥（system.cfg）+ 兵（host.cpp）+ 粮（v++ --package 的 SD 卡）+ 令（run_script.sh）」**。

## 4. 核心概念与源码讲解

### 4.1 xrt::graph 控制

#### 4.1.1 概念说明

一张 ADF 图编译后，在 AI Engine 阵列上是一个有生命周期的运行实体。这个生命周期在不同运行环境下由不同主体驱动：

- **x86sim / aiesim（纯软件仿真）**：由图源码自带的 `aie.cpp` 里的 `main()` 驱动，调用 `init() → run(n) → end()`。
- **hw_emu / hw（QEMU 或真实板子）**：图被打进 `libadf.a` 再封进 xclbin，此时图没有自己的 `main()`，改由**主机程序**通过 `xrt::graph` 对象驱动。

`xrt::graph` 是 XRT 原生 C++ API 中 AIE 专有的对象（OpenCL 路线没有等价物，所以凡涉及 AIE 的混合系统几乎都用原生 XRT）。它提供三个核心方法，与仿真侧三件套一一对应：

| 主机侧（`xrt::graph`） | 仿真侧（`aie.cpp`） | 语义 |
| --- | --- | --- |
| `reset()` | `init()` | 把图复位到起始状态、装载 PDI。 |
| `run(iter)` | `run(n)` | 启动图运行 `iter` 轮；`iter=-1` 表示自由运行（永不自停）。 |
| `end()` | `end()` | 停止并结束图，释放运行资源。 |

构造 `xrt::graph` 时要传三个参数：`device`、`xclbin_uuid`、以及**图的名字字符串**。这个名字必须与 AIE 编译时写进 `libadf.a` 的逻辑图名一致（记录在编译产物 `aie_control_config.json` 里）。

#### 4.1.2 核心流程

主机控制一条 PL+AIE 流水线的典型时序如下（伪代码）：

```
device = xrt::device(0)
uuid   = device.load_xclbin("kernel.xclbin")

# 1) 先点亮 AIE 图
graph  = xrt::graph(device, uuid, "<graph_name>")
graph.reset()          # 复位/装载
graph.run(NUM_ITER)    # NUM_ITER=-1 => 自由运行

# 2) 再点亮 PL 搬运内核
mm2s = xrt::kernel(device, uuid, "mm2s_wrapper:{mm2s}")
s2mm = xrt::kernel(device, uuid, "s2mm_wrapper:{s2mm}")
mm2s_run = xrt::run(mm2s);  mm2s_run.set_arg(0, in_bo)
s2mm_run = xrt::run(s2mm);  s2mm_run.set_arg(0, out_bo)

# 3) 并发点火，靠 s2mm 收够数据来收尾
mm2s_run.start()
s2mm_run.start()
s2mm_run.wait()        # 流水线的「终点闸门」
mm2s_run.wait()

# 4) 取回结果
out_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE)
```

关键设计：本例的图被设置成**自由运行**（`NUM_ITER=-1`），图本身不会停下来；整条流水线的终止信号来自 `s2mm`——它搬运够预定的数据量后自然结束，`s2mm_run.wait()` 随即返回。因此主机**没有显式调用 `graph.end()`**：数据流下游（s2mm）一旦停摆，上游（mm2s、AIE 图）也就无数据可处理，整个系统随之停转。这是「以数据流自身为节拍器」的典型写法。

> 与仿真侧对照：`aie.cpp` 的 `main()` 用 `init()/run(NITER)/end()` 三步显式管理生命周期，因为仿真里没有 s2mm 这类 PL 内核来做终止判定，图必须自己跑固定轮数后 `end()`。

#### 4.1.3 源码精读

主机要使用 `xrt::graph`，需要额外包含 `experimental/` 下的三个 AIE 头文件（普通 `xrt_kernel.h` 不含 AIE 能力）：

[host.cpp:29-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L29-L31) —— 引入 AIE 专有的实验性 XRT 头件（`xrt_aie.h`、`xrt_graph.h`、`xrt_ip.h`）。

随后是固定的 device→xclbin→graph 三步，以及图的复位与启动：

[host.cpp:110-125](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L110-L125) —— `load_xclbin` 后构造 `xrt::graph(my_device, xclbin_uuid, "fft_aie_fft_tb")`，紧接着 `reset()` 与 `run(NUM_ITER)`。注意这里**只调用了 `reset()` 和 `run()`，没有 `end()`**——这正是自由运行模式的特点。

`NUM_ITER` 的取值是理解整条流水线终止机制的关键：

[host.cpp:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L83) —— `static constexpr int32_t NUM_ITER = -1;` 注释直白写道「让图跑着，由 s2mm 来终止」。

主机点完图之后，才轮到 PL 搬运内核登场（注意 `xrt::kernel` 第二个参数里的 `{mm2s}` 是实例名，要和 `system.cfg` 的 `nk=` 声明对齐，这是 u4-l2 已讲过的约定）：

[host.cpp:131-141](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131-L141) —— 取出 mm2s/s2mm 两个 PL 内核并各建一个 `xrt::run`。

最后是「点火 + 闸门」段，这是整条流水线并发与终止的核心：

[host.cpp:196-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L196-L206) —— `mm2s_run.start()` 与 `s2mm_run.start()` 把两个 PL 内核并发提交给硬件；随后 `s2mm_run.wait()` 阻塞到 s2mm 收够数据，`mm2s_run.wait()` 再收 mm2s 的尾。主机全程不创建任何线程，并发发生在硬件里的数据流上。

对照仿真侧的图驱动，可以清楚看到两种生命周期管理方式的镜像关系：

[aie.cpp:20-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp#L20-L27) —— 全局图实例 `fft_tb` 在 `main()` 里依次 `init()` / `run(NITER)` / `end()`，三步齐全。这与 `host.cpp` 只用 `reset()`/`run()` 形成对照，原因正是仿真侧没有 s2mm 来做终止判定。

> 旁注：`fft_aie_fft_tb` 这个字符串是 AIE 编译进 `libadf.a` 的逻辑图名。它并不直接出现在 `aie.hpp` 源码里，而是由图实例的层级命名经编译器派生、登记在编译产物 `aie_control_config.json` 中。主机必须传这个名字，否则 `xrt::graph` 构造会找不到图。若你改了图的顶层命名，主机里的这个字符串也要同步改。

#### 4.1.4 代码实践

**实践目标**：把主机对 AIE 图的控制与对 PL 内核的控制画在一条时间线上，理解「谁先点火、谁当闸门」。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 `host.cpp`，按行号顺序列出 `xrt::graph` / `xrt::kernel` / `xrt::run` 的构造点与 `reset`/`run`/`start`/`wait` 调用点。
2. 把它们画成一条竖直时间轴，标注每一步是「图侧」还是「PL 侧」。
3. 在 `s2mm_run.wait()` 这一步旁边批注：「此处理论上会阻塞多久？」

**需要观察的现象（推理）**：

- `graph.run()` 在 `mm2s_run.start()` **之前**：图先就绪等待数据，搬运器再开始喂。
- `s2mm_run.wait()` 在 `mm2s_run.wait()` **之前**：流水线从下游开始停摆。

**预期结果**：时间轴应呈现「图复位 → 图启动 → PL 启动 → 下游 wait → 上游 wait → 取回结果」的顺序，且图启动早于 PL、下游 wait 早于上游 wait。

**待本地验证**：若你有 hw_emu 环境，可在 `reset()` 与 `run()` 之间各加一行 `std::cout` 打印时间戳，实测两者间隔（复位/装载 PDI 的耗时）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `host.cpp` 不调用 `graph.end()`，而 `aie.cpp` 必须调用？

**参考答案**：`host.cpp` 把图设成自由运行（`NUM_ITER=-1`），整条流水线由 s2mm 收够数据来终止，`s2mm_run.wait()` 返回后主机进程即将退出，无需显式 `end()`。`aie.cpp` 是纯仿真驱动，没有 s2mm 这类 PL 终止源，图必须自己跑完固定轮数后 `end()` 才能干净收尾。

**练习 2**：如果把 `NUM_ITER` 从 `-1` 改成 `4`，整条流水线的行为会怎样？

**参考答案**：图会在运行 4 轮后自行 `end()` 停下。若此时 s2mm 尚未收够预定的全部数据，主机端的 `s2mm_run.wait()` 可能永远等不到足够数据而挂起；反之若 s2mm 的数据量本就按 4 轮配齐，则两者刚好吻合。所以改 `NUM_ITER` 必须同时核对 s2mm 的搬运量（`DDR_BUFFSIZE_O_BYTES` 等）。

---

### 4.2 system.cfg 连接描述

#### 4.2.1 概念说明

`system.cfg` 是 `v++ -l`（链接）阶段的「系统拓扑说明书」。它告诉链接器三件事：

- **`nk=`（number of kernels）**：每个 PL 内核要实例化几个、各叫什么实例名。
- **`sp=`（storage port）**：每个内核的 `m_axi` 存储主端口绑到哪片 DDR/HBM bank（这是 u4-l3 里主机 `group_id` 的源头）。
- **`sc=`（stream connection）**：AXI Stream 端口之间怎么两两相连——这是 PL↔AIE 边界的「接线表」。

对 AIE 系统，`sc=` 尤其重要：PL 搬运内核的 `axis` 端口与 AIE 图的 PLIO 端口就是靠它焊接的。此外还有两个非连接性段落：`freqhz=`（各内核时钟频率）和 `[vivado]`（实现阶段的物理优化开关）。

#### 4.2.2 核心流程

读 `system.cfg` 的顺序建议为「时钟 → 实例化 → 存储绑定 → 流连接 → 物理优化」：

```
freqhz=<Hz>:<每个内核的 ap_clk>          # 时钟
nk  = <kernel>:<数量>:<实例名>            # 实例化
sp  = <实例>.<m_axi端口>:<bank>           # 存储端口绑 bank
sc  = <源实例>.<out流> : <目的实例>.<in流> # AXI Stream 连接
[vivado] ...                              # 实现/PAR 选项
```

判读 `sc=` 的关键规律：PL 搬运器的流端口数 = AIE 图的 PLIO 端口数 = SSR 路并行。本例 SSR=4，故 mm2s 输出 4 条流、s2mm 输入 4 条流，`sc=` 条数合计 8 = 2×SSR。这条规律在 u13-l1 已建立，本讲用真实 `system.cfg` 复核。

#### 4.2.3 源码精读

文件最上面先给所有相关内核定统一的时钟频率（312.5 MHz）：

[system.cfg:2](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L2) —— `freqhz=312500000` 列出 `mm2s.ap_clk`、`s2mm.ap_clk` 以及三个 transpose 内核的时钟。

接着是 `[connectivity]` 段。先是内核实例化（`nk=`）——声明 `mm2s_wrapper` 与 `s2mm_wrapper` 各实例化 1 个，实例名分别为 `mm2s`、`s2mm`：

[system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11) —— `nk = mm2s_wrapper:1:mm2s` 与 `nk = s2mm_wrapper:1:s2mm`。这里的实例名 `mm2s` 正是 `host.cpp` 里 `xrt::kernel(..., "mm2s_wrapper:{mm2s}")` 大括号里要填的值。

存储端口绑定把两个搬运器都绑到同一片 LPDDR：

[system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) 与 [system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33) —— `sp=mm2s.mem:LPDDR` 与 `sp=s2mm.mem:LPDDR`，二者同绑单片 LPDDR（单 bank，是 u4-l3 讲过的带宽反例，但本例数据量小、够用）。

核心是 `sc=` 段——PL↔AIE 边界的接线表。mm2s 的 4 条输出流接到前转置（front_transpose）的 4 条输入流；后转置（back_transpose）的 4 条输出流接到 s2mm 的 4 条输入流：

[system.cfg:23-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L31) —— 8 条 `sc=`，左侧 4 条接 mm2s→front_transpose，右侧 4 条接 back_transpose→s2mm。下标 `sig_o_0..3` / `sig_i_0..3` 正好对应 SSR=4 的 4 路并行流。

最后是 `[vivado]` 段，开启实现阶段的物理优化，并启用统一 AIE 流程让 Vivado 报告里能看到 AIE 资源：

[system.cfg:39-44](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L39-L44) —— `phys_opt_design` 与 `post_route_phys_opt_design` 两个 `is_enabled=1`，以及 `param=project.enableUnifiedAIEFlow=true`（这是收敛时序、定位资源的开关，u12-2 已涉及）。

#### 4.2.4 代码实践

**实践目标**：用 SSR 验证 `sc=` 条数的「2×SSR」规律，理解改 SSR 时 `system.cfg`（或它的生成器）要同步改什么。

**操作步骤**：

1. 数 `system.cfg` 里 `sc=` 的总条数，记为 N_sc。
2. 在 `my_params.cfg` / `example.mk` 里查到 `SSR` 的值，记为 SSR。
3. 计算 2×SSR，与 N_sc 对比。
4. 思考：如果要把 SSR 从 4 改成 2，`sc=` 该有几条？

**需要观察的现象**：

- N_sc 应等于 8，SSR 应等于 4，2×SSR=8，三者吻合。
- 改 SSR=2 后，`sc=` 应为 4 条（mm2s 出 2 条 + s2mm 入 2 条）。

**预期结果**：`sc=` 条数严格随 SSR 线性变化。注意 `system.cfg` 在本示例里是**手写静态文件**，但 `sc=` 里的转置内核（`front_transpose`/`back_transpose`）及其端口其实由 VSS 代码生成（见 4.3 的 `vss` 目标），改 SSR 时要确认这些生成物与 `sc=` 对齐。

**待本地验证**：本仓库 `dsp/L2/include/vss/vss_fft_ifft_1d/` 下有 `vss_fft_ifft_1d_hdl_con_gen.py` 等连接生成脚本，可阅读它们确认 `sc=` 是否部分由脚本生成。

#### 4.2.5 小练习与答案

**练习 1**：`sp=mm2s.mem:LPDDR` 这条声明最终影响了主机代码里的哪个值？

**参考答案**：它决定了 `mm2s` 内核第 0 个 `m_axi` 参数对应的存储端口组号，也就是 `host.cpp` 里 `mm2s.group_id(0)` 的返回值（本例该端口绑到 LPDDR，对应一个具体的 bank 号）。创建 `mm2s_bo` 时把这个组号传给 `xrt::bo`，才能保证缓冲落在 mm2s 可访问的 bank 上。

**练习 2**：为什么 `sc=` 里两端端口的下标必须一一对应（`sig_o_0` 对 `sig_i_0`，而不是 `sig_o_0` 对 `sig_i_1`）？

**参考答案**：AXI Stream 连接是点对点的物理接线，下标代表 SSR 并行通路编号。第 0 路输入数据必须送到第 0 路处理单元，否则 AIE 图里按通路编号重排的交错布局会被打乱，FFT 结果出错。这也是 u6-l1 讲过的 SSR 数据重排约定在系统接线层的体现。

---

### 4.3 v++ --package SD 卡打包

#### 4.3.1 概念说明

`v++ --package`（短选项 `-p`）是 v++ 三段流水线的最后一段（前两段是 `-c` 编译、`-l` 链接）。它的任务是把链接产物封装成**最终可交付的形态**：

- 纯 PL 的数据中心卡（x86 主机 + PCIe）→ 封装成一个 `.xclbin`。
- 嵌入式 Versal/AIE 平台（aarch64 Linux）→ 封装成一个 `.xclbin` **外加一整张 SD 卡**，因为嵌入式板子靠 SD 卡启动：卡上要有启动加载器、Linux 内核镜像（`Image`）、根文件系统（`rootfs`）、设备树覆盖（`dtbo`）、可编程器件镜像（`pdi`）、xclbin、主机可执行文件和数据文件。

嵌入式平台因此比 PCIe 平台多依赖三样主机侧没有的东西：**sysroot**（交叉编译主机程序用的 C 库/头件根）、**rootfs**（`rootfs.ext4`，塞进 SD 卡的根文件系统）、**kernel image**（`Image`，Linux 内核镜像）。这三者通常来自 AMD 提供的 Common Image。

AIE 系统打包还有一个特别关键的选项 `--package.defer_aie_run`：它告诉打包器「**不要在 xclbin 加载时自动启动 AIE 图**」，而把图的启动权交给主机程序的 `xrt::graph.run()`。这与 4.1 的主机控制模型是配套的——若无此选项，图会在加载时自启，主机的 `reset()/run()` 时序就乱了。本例显式带上这个选项，正是为了让主机当「总指挥」。

#### 4.3.2 核心流程

`example.mk` 的打包目标（`example_sd_card`）分两步：

```
# 第 1 步：生成仿真配置（hw_emu 需要）
emconfigutil --platform ${PLATFORM} --od ./
#   => 产出 emconfig.json

# 第 2 步：封装 xclbin + SD 卡
v++ -t hw_emu --platform ${PLATFORM} \
    -o kernel.xclbin \
    -p kernel_pkg.xsa libadf.a \                  # 输入：链接产物(XSA) + AIE 容器(libadf.a)
    --package.defer_aie_run \                     # 把图启动权交给主机
    --package.out_dir package_hw_emu \            # 输出目录
    --package.rootfs ${SYSROOT}/../../rootfs.ext4 # 根文件系统
    --package.generate_sdcard \                   # 生成完整 SD 卡
    --package.kernel_image ${SYSROOT}/../../Image # Linux 内核镜像
    --package.boot_mode sd \                      # SD 启动模式
    --package.sd_file run_script.sh \             # 以下都是要拷进 SD 卡的文件
    --package.sd_file host.elf \
    --package.sd_file emconfig.json \
    --package.sd_file data/input_front.txt \
    --package.sd_file data/ref_output.txt
```

把 `--package.*` 选项按职能分四组记忆：

| 组别 | 代表选项 | 塞进 SD 卡的东西 |
| --- | --- | --- |
| 输入 | `-p kernel_pkg.xsa libadf.a` | 链接好的硬件容器 + AIE 图（工具据此生成 `xclbin`、`pdi`、`dtbo`） |
| 启动镜像 | `--package.rootfs` / `--package.kernel_image` / `--package.boot_mode sd` | `rootfs.ext4`、`Image`、启动加载脚本 |
| 行为开关 | `--package.defer_aie_run` / `--package.generate_sdcard` / `--package.out_dir` | 控制图启动时机、要求生成 SD 卡、指定输出目录 |
| 附加文件 | `--package.sd_file <f>`（可重复） | 主机可执行、运行脚本、仿真配置、测试数据 |

打包前还有一步「交叉编译主机」，它把 `host.cpp` 编成 aarch64 的 `host.elf`（因为板子上跑的是 aarch64 Linux，不是 x86）：

```
aarch64-linux-gnu-g++ -o host.elf host.cpp \
    --sysroot=$(SYSROOT) \
    -I$(SYSROOT)/usr/include/xrt \
    ... -ladf_api_xrt -lxrt_coreutil ...
```

注意它链接了 `-ladf_api_xrt`——这正是 `xrt::graph` 这类 AIE 控制 API 的运行时实现库，主机用它去驱动 AIE 图。

#### 4.3.3 源码精读

`example.mk` 顶部把 `my_params.cfg` 的关键参数固定成 Make 变量，这些变量既喂给主机编译（`-DSSR=...`），也喂给 VSS 生成：

[example.mk:19-25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L19-L25) —— `POINT_SIZE=4096`、`SSR=4`、`DATA_TYPE=cint32`、`NITER=4` 等。u6-3 已强调过：`my_params.cfg` 与 `example.mk` 是两套独立参数源，改 SSR/POINT_SIZE 必须两边同步。

打包之前先要有「VSS 生成」和「编译/链接」两步产物。`vss` 目标调用库里的 `vss_fft_ifft_1d.mk`，按 `my_params.cfg` 生成 AIE 图与转置内核源码（产出 `vss_fft_ifft_1d/vss_fft_ifft_1d.vss` 与 `libadf.a`）：

[example.mk:27-29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L27-L29) —— `vss` 目标，先 `meta_check` 校验参数再 `clean vss` 生成。

`example_xclbin` 目标是 v++ 的前两段：`v++ -c` 把 mm2s/s2mm 各编成一个 `.xo`，`v++ -l` 把两个 XO 加上 VSS 产物链接成 `kernel_pkg.xsa`：

[example.mk:31-34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L31-L34) —— 两条 `v++ -c`（mm2s、s2mm）+ 一条 `v++ -l --config system.cfg -o kernel_pkg.xsa`。注意链接时 `-t hw_emu` 与打包、运行三段的 target 必须一致。

`example_host` 目标用 aarch64 交叉编译器编出 `host.elf`：

[example.mk:36-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L37) —— 用 `${XILINX_VITIS}/gnu/aarch64/.../aarch64-linux-gnu-g++` 配合 `--sysroot=$(SYSROOT)` 编译，链接 `-ladf_api_xrt` 提供 `xrt::graph` 运行时。

本模块的核心——`example_sd_card` 目标，即那条「五脏俱全」的 `v++ --package` 命令：

[example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42) —— 先 `emconfigutil` 生成 `emconfig.json`，再 `v++ -p kernel_pkg.xsa libadf.a` 带上一长串 `--package.*` 选项，产出 `kernel.xclbin` 与 `package_hw_emu/` 目录（SD 卡内容 + 启动脚本）。`--package.defer_aie_run` 尤其重要，它把图的启动权交给主机。

最后，`all` 目标把五步串成一条流水线：

[example.mk:49](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L49) —— `all: vss example_xclbin example_host example_sd_card example_run`，五目标顺序依赖，一行 `make -f example.mk all` 走完从源码到 hw_emu 运行的全过程。

#### 4.3.4 代码实践

**实践目标**：把那条冗长的 `v++ --package` 命令拆成「输入 / 启动镜像 / 行为开关 / 附加文件」四组，列出 SD 卡里到底有哪些文件。

**操作步骤**：

1. 打开 `example.mk` 第 42 行，把所有 `--package.*` 选项抄出来。
2. 按上面那张四组表分类。
3. 推断：最终 `package_hw_emu/` 目录里会出现哪些文件？分两类——工具自动生成的（`xclbin`、`pdi`、`dtbo`、`launch_hw_emu.sh`、SD 卡镜像等）与 `--package.sd_file` 显式拷入的。

**需要观察的现象（推理）**：

- 显式拷入的文件：`run_script.sh`、`host.elf`、`emconfig.json`、`data/input_front.txt`、`data/ref_output.txt`。
- 工具自动生成的文件：`kernel.xclbin`、`*.pdi`（可编程器件镜像）、`*.dtbo`（设备树覆盖）、`launch_hw_emu.sh`（QEMU 启动脚本）、`qemu_output.log`（运行后产生）、SD 卡启动镜像（`boot.scr`、`Image`、`rootfs` 等）。

**预期结果**：你能凭命令预测 SD 卡内容，而无需真的跑一遍打包。

**待本地验证**：实际执行 `make -f example.mk example_sd_card ...` 后，用 `ls package_hw_emu/` 与 `ls package_hw_emu/sd_card/` 核对预测。

#### 4.3.5 小练习与答案

**练习 1**：去掉 `--package.defer_aie_run` 会怎样？

**参考答案**：xclbin 加载时 AIE 图会被自动启动。于是主机还没来得及 `reset()`/`run()`，图就已经在跑了，主机后续的 `reset()` 会把已运行的图强行打断复位，行为不确定；且数据时序（mm2s 还没开始喂）会错乱。这个选项是「主机当总指挥」模型的前提。

**练习 2**：为什么 `example_host` 必须交叉编译（aarch64），而不能用本机 g++？

**参考答案**：hw_emu 用 QEMU 模拟的是 Versal 的 aarch64 Linux，板子上的 `host.elf` 必须是 aarch64 指令集；本机 g++ 编出的是 x86 可执行，在 QEMU/板子上无法运行。交叉编译还需要 `--sysroot` 提供 aarch64 的 glibc 与 XRT 运行时库（`-lxrt_coreutil`、`-ladf_api_xrt`）。

---

### 4.4 hw_emu 启动流程

#### 4.4.1 概念说明

hw_emu（hardware emulation）用 **QEMU** 模拟整个 Versal 系统——包括 PS（处理器系统，跑 Linux）、PL（可编程逻辑，跑 RTL 仿真）和 AIE 阵列。它比纯软件仿真保真得多（PL 走真实 RTL），又比上板便宜得多（不用占物理板子），是 AIE 系统开发迭代的主战场。

`v++ --package` 生成的 `launch_hw_emu.sh` 是一键启动这个模拟系统的脚本。它的典型用法是：

```
./package_hw_emu/launch_hw_emu.sh -no-reboot -run-app <run_script.sh>
```

- `-no-reboot`：复用已启动的 QEMU 实例，不每次重启（加速迭代）。
- `-run-app <script>`：QEMU 起来后在模拟的 Linux 里执行这个脚本。

被 `-run-app` 指定的 `run_script.sh` 就是真正干活的入口：它在模拟 Linux 里设置环境变量（关键的有 `XCL_EMULATION_MODE=hw_emu`），然后跑 `host.elf`，根据返回码打印 `TEST PASSED, RC=0` 或 `TEST FAILED`。主机的 `std::cout` 输出和这行 PASS/FAIL 标记都被 QEMU 重定向到 `qemu_output.log`，外层 Make 用 `grep "TEST PASSED, RC=0"` 抓这行来判定整个 build 的成败。

#### 4.4.2 核心流程

从 `make example_run` 到看到 PASS 的完整链路：

```
make example_run
  └─ ./package_hw_emu/launch_hw_emu.sh -no-reboot -run-app run_script.sh
       └─ QEMU 启动 Versal 模拟系统（PS+PL+AIE），挂载 SD 卡镜像
            └─ 在模拟 Linux 里执行 run_script.sh
                 ├─ export XCL_EMULATION_MODE=hw_emu     # 告诉 XRT 走 hw_emu 后端
                 ├─ export XILINX_VITIS=/mnt, XILINX_XRT=/usr
                 ├─ cp platform_desc.txt /etc/xocl.txt  # 让驱动认得 xclbin
                 ├─ ./host.elf                           # 主机程序（4.1 的全部控制逻辑）
                 └─ 据 $? 打印 TEST PASSED / FAILED
       └─ 全程 stdout => qemu_output.log
  └─ grep "TEST PASSED, RC=0" qemu_output.log || exit 1
```

`XCL_EMULATION_MODE=hw_emu` 这个环境变量是 u4-1 讲过的 XRT 仿真模式开关——`xcl2` 的 `is_hw_emulation()` 读的就是它，主机 `load_xclbin` 时 XRT 也凭它选 hw_emu 后端、加载对应的仿真 xclbin。

#### 4.4.3 源码精读

`example_run` 目标就是「启动 hw_emu + 抓 PASS」两行：

[example.mk:44-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L44-L46) —— 调 `launch_hw_emu.sh -no-reboot -run-app run_script.sh`，然后 `grep "TEST PASSED, RC=0"` 抓日志，抓不到就 `exit 1` 让 CI 判负。

被传入的 `run_script.sh` 是模拟 Linux 内的入口，逐行拆解：

[run_script.sh:17-20](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L17-L20) —— 设 `LD_LIBRARY_PATH`、`XCL_EMULATION_MODE=hw_emu`、`XILINX_VITIS=/mnt`、`XILINX_XRT=/usr`。这些路径（`/mnt`、`/usr`）是模拟 Linux 里约定好的挂载点。

[run_script.sh:21-23](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L21-L23) —— 若存在 `platform_desc.txt` 就拷成 `/etc/xocl.txt`，让 XOCL 驱动据此识别平台。

[run_script.sh:24-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L24-L32) —— 执行 `./host.elf`，捕获返回码 `$?`，非 0 打印 `TEST FAILED`、为 0 打印 `TEST PASSED, RC=0`，最后 `exit $return_code`。这行 `TEST PASSED, RC=0` 正是外层 `grep` 的靶子。

旁注：`scripts_mk/` 下还有一组给 AIE2PS（新一代 Versal，如 VEK385）平台用的变体脚本。`run.sh` 是一个薄包装，先 source QEMU 环境再调用真正的运行脚本：

[scripts_mk/run.sh:14-19](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/run.sh#L14-L19) —— source `${XILINX_VITIS}/data/emulation/qemu/.../environment-setup-x86_64-petalinux-linux` 后执行 `./run.sh`。这套机制针对 AIE2PS 板子用 `wic` 工具往磁盘镜像里拷文件（见 `scripts_mk/run_copy_wic.sh`），比本例（vck190）的 SD 卡流程更复杂，是 u15-2 嵌入式部署会展开的内容。

最后，本示例的 CI 元数据声明它就是一个 hw_emu canary（金丝雀）用例：

[description.json:5-22](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L5-L22) —— `flow=system`、平台白名单 `vck190`、`launch` 段 `target=hw_emu`、`pre_build` 跑 `make -f example.mk all`、`testinfo` 给 hw_emu 配了 40960 MB 内存与 470 分钟时限。这正是 CI（顶层 Jenkinsfile）识别并调度这个 hw_emu 用例的依据。

#### 4.4.4 代码实践

**实践目标**：解释 `launch_hw_emu.sh` 如何把「启动 QEMU → 跑 host → 判 PASS」三件事串起来，并理解 `grep` 为何是可靠的判定手段。

**操作步骤**：

1. 对照本节的核心流程图，在 `example.mk:44-46`、`run_script.sh`、`host.cpp` 三个文件间画一张调用关系图：谁调用谁、输出流向哪。
2. 在 `host.cpp` 里找到最终决定返回码的语句（提示：`return (flag)`）。
3. 追踪：`flag` 由什么决定？追溯到结果校验段的误差阈值。

**需要观察的现象（推理）**：

- `host.elf` 的返回码 = `flag`，`flag` 由 `ref_output.txt` 与实际输出逐样本比对、误差超过 `level = (1<<8) = 256` 则置位。
- 这条返回码经 `run_script.sh` 变成 `TEST PASSED, RC=0` 字符串，再被 `example_run` 的 `grep` 捕获。

**预期结果**：判定链为 `host.cpp 的 flag → host.elf 返回码 → run_script.sh 的 PASS/FAIL 行 → example_run 的 grep → Make 退出码`。任何一环出错，`grep` 都抓不到 `TEST PASSED, RC=0`，CI 即判负。

**待本地验证**：实际跑一次 hw_emu 后，打开 `package_hw_emu/qemu_output.log`，找到 `--- PASSED ---`（来自 host.cpp 末尾）与 `TEST PASSED, RC=0`（来自 run_script.sh）两行，确认它们都在。

#### 4.4.5 小练习与答案

**练习 1**：为什么判定成功用 `grep "TEST PASSED, RC=0"` 而不是检查 `launch_hw_emu.sh` 的退出码？

**参考答案**：`launch_hw_emu.sh` 的退出码反映的是「QEMU 与脚本能否跑完」，未必等价于「主机程序逻辑通过」。主机程序的成败由 `run_script.sh` 打印的 `TEST PASSED/FAILED` 行体现，它直接来自 `host.elf` 的返回码。抓这行字符串是把「应用级成败」冒泡到「CI 级判定」的最稳健方式——即便 QEMU 本身有非零退出，只要应用跑了且返回 0，日志里就有这行。

**练习 2**：`-no-reboot` 选项在迭代开发时有什么实际好处？

**参考答案**：它复用已经启动的 QEMU 实例，避免每次重跑都经历完整的 Versal 系统冷启动（装载 PDI、启动 Linux，耗时可达数分钟）。代价是图/PL 的状态可能残留，所以涉及硬件状态变更时仍需重启。对于只改主机代码、重编 `host.elf` 后重跑的场景，`-no-reboot` 能显著缩短迭代周期。

---

## 5. 综合实践

**任务**：对照 `example.mk` 的 `example_sd_card` 与 `example_run` 两个目标，画出一张「从打包到 PASS」的完整流程图，并解释 `launch_hw_emu.sh` 如何启动整个系统、`v++ --package` 又把哪些文件塞进了 SD 卡。

**操作步骤**：

1. **拆包**：把 `example.mk:42` 的 `v++ --package` 命令拆成「输入 / 启动镜像 / 行为开关 / 附加文件」四组（参考 4.3.4），列出每组的具体选项。
2. **列 SD 卡清单**：据此预测 `package_hw_emu/` 目录下的文件，标注每个文件是「工具自动生成」还是「`--package.sd_file` 显式拷入」。特别标注：哪个文件是 AIE 图的可编程镜像（`pdi`）、哪个是设备树覆盖（`dtbo`）、哪个是主机可执行（`host.elf`）、哪个是运行入口（`run_script.sh`）。
3. **画启动链**：从 `make example_run` 出发，画出 `launch_hw_emu.sh → QEMU → run_script.sh → host.elf → qemu_output.log → grep` 的完整链路，在 `host.elf` 这一节点上展开 4.1 的图控制时序（`graph.reset → graph.run → mm2s/s2mm start → s2mm wait → sync`）。
4. **解释一个耦合点**：在图上标出 `--package.defer_aie_run`（4.3）与 `graph.reset()/run()`（4.1）的对应关系——前者把图启动权让给后者。
5. **定位判定靶子**：在 `run_script.sh` 里找到那行 `TEST PASSED, RC=0`，回溯它来自 `host.elf` 的返回码，再回溯到 `host.cpp` 的 `flag` 与误差阈值 `level`。

**预期产出**：一张包含「打包产物清单 + 启动调用链 + 图控制时序 + 判定回溯」四要素的流程图（手绘或文字版均可）。

**待本地验证**：若条件允许，执行 `make -f example.mk all PLATFORM=<vck190 xpfm> DSPLIB_ROOT_DIR=<dsp 库根> SYSROOT=<...>`，用 `ls` 与 `cat` 核对你的预测；本流程属重型 hw_emu（470 分钟级），无环境时以源码阅读型实践为准。

## 6. 本讲小结

- **`xrt::graph` 是 AIE 图的主机侧遥控器**：`reset()/run()/end()` 对应仿真侧 `init()/run()/end()`。本例用 `NUM_ITER=-1` 让图自由运行，靠 `s2mm` 收够数据来终止整条流水线，因此主机只调 `reset()/run()` 不调 `end()`。
- **主机是 PL+AIE 系统的总指挥**：先 `graph.run()` 让图就绪，再 `mm2s/s2mm.start()` 点火搬运，靠 `s2mm_run.wait()` 当闸门收尾——全程不创建线程，并发发生在硬件数据流上。
- **`system.cfg` 是系统拓扑说明书**：`nk=` 实例化内核、`sp=` 绑存储 bank（决定主机 `group_id`）、`sc=` 接 AXI Stream（PL↔AIE 边界接线表，条数 = 2×SSR）。
- **`v++ --package` 在嵌入式平台产出整张 SD 卡**：输入 `kernel_pkg.xsa + libadf.a`，配 `--package.rootfs/kernel_image/generate_sdcard/boot_mode` 产出启动镜像，配 `--package.sd_file` 拷入主机与数据；`--package.defer_aie_run` 是把图启动权交给主机的前提。
- **hw_emu 用 QEMU 跑整个 Versal 系统**：`launch_hw_emu.sh -run-app run_script.sh` 启动模拟系统并执行入口脚本，脚本设 `XCL_EMULATION_MODE=hw_emu` 后跑 `host.elf`，外层靠 `grep "TEST PASSED, RC=0"` 判 CI 成败。
- **一条贯穿的判定链**：`host.cpp 的 flag → host.elf 返回码 → run_script.sh 的 PASS 行 → example_run 的 grep → Make 退出码`。

## 7. 下一步学习建议

本讲把「主机控制 AIE 图 + 打包部署」走通到了 hw_emu 这一档。建议接着往两个方向深入：

- **上板（hw）部署**：学习 u15-2（完整部署：hw 构建、SD 卡与已知问题），理解 hw 档相对 hw_emu 的代价、嵌入式平台对 sysroot / Common Image / SD 卡的完整依赖，以及 `platform_map.json` 的平台重定向。
- **跨库组合与依赖**：学习 u15-1（依赖图与跨库组合），理解 `dependency.json` 表达的 utils ← data_mover ← dsp ← solver 依赖闭包，以及在同一工程里同时引入多个库 include 路径的工程做法。
- **性能与瓶颈定位**：若你已完成一次 hw_emu 运行，可回头结合 u12-2（资源/时序：URAM、HBM/DDR 分区与报告），读 `system.cfg` 的 `sp=` 与 `[vivado]` 段如何影响带宽与时序收敛。
- **扩展阅读源码**：`dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk`（VSS 生成的全流程）、`scripts_mk/run_copy_wic.sh`（AIE2PS 平台用 `wic` 往磁盘镜像拷文件的变体流程）。
