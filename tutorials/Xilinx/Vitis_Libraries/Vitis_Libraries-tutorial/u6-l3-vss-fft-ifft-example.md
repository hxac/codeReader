# 端到端 AIE 示例：vss_fft_ifft_1d

## 1. 本讲目标

前几讲我们分别学过 AIE 内核家族（u6-l2）、原生 XRT 主机控制（u4-l2）和 PL 数据搬运器（u5-l2）。这些零件单独看都懂了，但一个真正能上板的 AIE 系统是怎么把它们"焊"在一起的？本讲就用 dsp 库的 `vss_fft_ifft_1d`（一维 FFT/IFFT 的 Vector Sub-System 实现）这个**完整端到端示例**把所有零件串成一条线。

学完本讲，你应当能够：

- 说清一个完整 AIE 系统由哪几类文件构成（AIE 图源码、参数配置、PL 搬运、主机程序、构建脚本）。
- 解释 `my_params.cfg` 里的参数是如何被 Python 脚本消费、最终"长成"`config.h` 和 `libadf.a` 里的 AIE 图的。
- 复述 `vss_fft_ifft_1d.mk` 把 AIE 图 + 多个 HLS 转置内核链接成一个 `.vss` 单元的全过程。
- 对照 `example.mk` 的五个 make 目标，讲清从 XO 编译、xsa 链接、主机交叉编译、SD 卡打包到 hw_emu 启动运行的端到端流水。
- 理解 `SSR`（超采样并行度）和 `POINT_SIZE`（FFT 点数）如何决定 PLIO 流的数量与 AIE 图的拓扑结构。

## 2. 前置知识

本讲默认你已经具备以下概念（若不熟请先看对应讲义）：

- **L1/L2/L3 三层抽象与 PL/AIE 两种范式（u1-l3）**：知道 AIE 走 ADF 数据流图、跑在 Versal（VCK190 等）上。
- **HLS 五档大写 TARGET（u2-l3）**：本讲的 PL 内核仍用 `v++ -c --mode hls` 编译成 XO。
- **L2 v++ 构建三段流水 XO→xsa→xclbin（u5-l1）**：本讲会用到，但 AIE 系统多出一个 `.vss` 中间产物。
- **数据搬运器与 DDR↔AIE 桥接（u5-l2）**：知道 AIE 阵列只认无地址的 AXI Stream，靠 mm2s/s2mm 在 DDR 与流之间翻译。
- **原生 XRT C++ API（u4-l2）**：`xrt::device`/`load_xclbin`/`xrt::kernel`/`xrt::run`/`set_arg`/`start`/`wait`。
- **AIE 内核家族与三件式组织（u6-l2）**：`TT_`/`TP_` 模板参数命名、kernel+traits+utils 拆分、window/stream 两类数据交换。

补充两个本讲要用到的关键术语：

- **ADF 图（ADF graph）**：用 C++ 描述的 AIE 数据流图，继承自 `adf::graph`。图里声明 kernel（计算节点）和 connect（数据边）。编译后产出 `libadf.a`，由主机用 `xrt::graph` 控制。
- **PLIO**：PL 与 AIE 之间的物理流端口（PL I/O）。一个 `input_plio`/`output_plio` 对应一根 32/64/128 位的物理 AXI Stream 连线。`vss_fft_ifft_1d` 用 128 位 PLIO。
- **VSS（Vector Sub-System）**：dsp 库里把"AIE 图 + 若干 PL HLS 内核"打包成一个可复用单元的产物，扩展名 `.vss`，由 `v++ --link --mode vss` 链接生成。可以把一个 VSS 理解成"一个内含 AIE 子系统的大内核"。

## 3. 本讲源码地图

`vss_fft_ifft_1d` 的源码分两个目录：`dsp/L2/include/vss/vss_fft_ifft_1d/`（VSS 的"内核侧"模板与构建逻辑）与 `dsp/L2/examples/vss_fft_ifft_1d/`（示例工程，含主机、参数与端到端 make）。

| 文件 | 作用 |
| --- | --- |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp` | 顶层 AIE 图类 `tl_graph` 的声明：声明 SSR 路 PLIO 端口、实例化底层 FFT 图 `fftGraph`、在构造函数里把 PLIO 连到图上。VSS_MODE=1 用。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp` | AIE 图的 testbench `main()`：实例化 `tl_graph`，依次调用 `init()/run(NITER)/end()`。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie_front_only.hpp/.cpp` | VSS_MODE=2 专用的另一种顶层图（前端 AIE + 后端 RTL FFT）。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk` | VSS 的核心构建脚本：读参数、生成 config.h、编译 libadf.a、编译各 PL 转置 XO、链接成 `.vss`。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_params.cfg` | VSS 的"默认参数样例"（VSS_MODE=2），供参考。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py` | 根据 SSR 等参数**生成连接配置**（`sc=` 流连接）的 Python 脚本。 |
| `dsp/L2/examples/vss_fft_ifft_1d/my_params.cfg` | 示例实际使用的参数文件（VSS_MODE=1, SSR=4, POINT_SIZE=4096）。 |
| `dsp/L2/examples/vss_fft_ifft_1d/example.mk` | 示例的端到端 make：生成 vss、编译 mm2s/s2mm、链接 xsa、交叉编译 host、打包 SD 卡、跑 hw_emu。 |
| `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` | 主机程序：用 `xrt::graph` 控制 AIE 图、用 `xrt::kernel` 驱动 mm2s/s2mm、比对结果。 |
| `dsp/L2/examples/vss_fft_ifft_1d/system.cfg` | 示例的连接描述：声明 mm2s/s2mm 实例、DDR bank 绑定、PL↔PL 流连接。 |
| `dsp/L2/examples/vss_fft_ifft_1d/run_script.sh` | 嵌入式上板后执行 host.elf 的启动脚本（设环境变量、跑 host、打印 RC）。 |
| `dsp/L1/tests/hw/mm2s/mm2s.cpp` 与 `s2mm/s2mm.cpp` | PL 数据搬运内核源码（u5-l2 已讲，本讲作为系统中的零件引用）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应一条完整 AIE 系统的四个层面：**图的生命周期 → 参数与代码生成 → VSS 构建 → 端到端打包运行**。

### 4.1 AIE 图生命周期：main / init / run / end

#### 4.1.1 概念说明

一个 ADF 图在运行时有固定的生命周期，由三步构成：

1. **init()**：配置图——把 kernel 的运行时常量（如 FFT 旋转因子）算好、把图置到就绪态。
2. **run(n)**：启动图运行 `n` 次（iteration）。`n=-1` 表示自由运行，直到外部停止。
3. **end()**：结束图、释放资源。

这套生命周期有两套宿主：

- **AIE 仿真/x86 仿真**下，由 `aie.cpp` 里的原生 C++ `main()` 直接驱动（本模块讲）。
- **上板 / hw_emu** 下，图被打进 `libadf.a` 再打进 xclbin，由主机 `host.cpp` 用 `xrt::graph` 的 `reset()/run()/end()` 远程驱动（见 4.4）。

两套驱动共用同一个图类 `tl_graph`，区别只是"谁在叫 init/run/end"。

#### 4.1.2 核心流程

```text
aie.cpp 的 main():
  1. 全局构造 tl_graph 实例 fft_tb        ← 触发构造函数：建 PLIO、连边、实例化底层 fftGraph
  2. fft_tb.init()                         ← 图就绪
  3. fft_tb.run(NITER)                     ← 跑 NITER 次（aie.hpp 中 NITER=1）
  4. fft_tb.end()                          ← 收尾
```

注意 `aie.cpp` 里的 `NITER`（来自 `aie.hpp` 第 32 行 `#define NITER 1`）与主机 `host.cpp` 里的 `NUM_ITER=-1` 是**两个不同的量**：前者是 AIE 仿真时图自跑的迭代数，后者是上板时图的自由运行模式。

#### 4.1.3 源码精读

`aie.cpp` 极其简短，是整个 AIE 图的仿真入口：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp:20-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp#L20-L27) — 实例化顶层图 `fft_tb` 并依次调用 `init()/run(NITER)/end()`，这是 AIE 仿真下图的完整生命周期。

`fft_tb` 的类型 `tl_graph` 在 `aie.hpp` 里定义，它继承自 `adf::graph`，是本示例的"顶层 PLIO 包装图"。其核心是四组 SSR 路的 PLIO 端口数组：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:58-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L58-L61) — 声明 `back_i/back_o/front_i/front_o` 四组端口，每组都是 `std::array<..., SSR>` 个 PLIO。**SSR 直接决定了图的物理端口数**——这就是 SSR 与"流的数量"关系的源头。

构造函数做三件事：选 PLIO 位宽、实例化底层 FFT 图、循环连边：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:83-86](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L83-L86) — 按 `AIE_PLIO_WIDTH` 选 64/128 位 PLIO，再用一长串模板参数（`DATA_TYPE, TWIDDLE_TYPE, POINT_SIZE, FFT_NIFFT, SHIFT, API_IO, SSR, ROUND_MODE, SAT_MODE, TWIDDLE_MODE, POINT_SIZE_D1, CASC_LEN, USE_WIDGETS`）实例化底层 `fftGraph`。这些模板实参都来自 4.2 讲的代码生成。

连边循环把第 i 个 PLIO 端口连到 `fftGraph` 的第 i 个端口上：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:87-97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L87-L97) — `for (int i = 0; i < SSR; i++)` 循环 SSR 次，每次创建一个 `input_plio` 并 `connect<>(front_i[i].out[0], fftGraph.front_i[i])`。注意文件名里被插入了 `"_" + i + "_0"` 后缀（如 `input_front_0_0.txt`），这是 PLIO 文件 IO 的命名约定。

底层 `fftGraph` 的具体头文件是"按宏包含"的：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:33-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L33-L43) — `#define AIE_GRAPH vss_fft_ifft_1d_graph`，再用 `#include QUOTE(AIE_GRAPH.hpp)` 字符串化拼出 `vss_fft_ifft_1d_graph.hpp` 并包含。`QUOTE` 是标准的两次宏展开字符串化技巧。VSS_MODE=2 时这个宏会被改成 `vss_fft_ifft_1d_front_only`（见 4.3）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在不动手编译的前提下，把 `aie.cpp` 与 `aie.hpp` 的对应关系读通。
2. **操作步骤**：
   - 打开 `aie.cpp`，找到 `fft_tb` 的类型 `tl_graph`。
   - 打开 `aie.hpp`，确认 `class tl_graph : public graph`。
   - 数一数 `tl_graph` 一共声明了几个 PLIO 数组、每个数组多少个端口（用 SSR=4 代入）。
3. **观察现象**：四组数组（front_i/front_o/back_i/back_o）× SSR(4) = 16 个 PLIO 端口；其中 front/back 各占一半。
4. **预期结果**：你能口头复述"SSR=4 → 共 16 个物理 PLIO，front 侧 8 个（4 in + 4 out）、back 侧 8 个"。这正是 system.cfg 里会出现 4 条 mm2s→front_transpose 流连接的原因。
5. **待本地验证**：端口数与 PLIO 文件命名后缀（`_0_0` … `_3_0`）的对应需在 AIE 仿真运行时确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SSR` 从 4 改成 1，`tl_graph` 的 PLIO 端口总数变成多少？AIE 图的并行度如何变化？
**答案**：四组数组各 1 个，共 4 个 PLIO 端口（front_i=1, front_o=1, back_i=1, back_o=1）。并行度从 4 路降到 1 路，理论吞吐约为原来的 1/4。

**练习 2**：`aie.cpp` 里 `run(NITER)` 的 `NITER` 与 `host.cpp` 里 `run(NUM_ITER)` 的 `NUM_ITER` 为什么可以取不同值？
**答案**：它们是两个独立宏。`aie.cpp` 的 `NITER`（=1）用于纯 AIE 仿真时图自跑的迭代数；`host.cpp` 的 `NUM_ITER`（=-1）用于上板时让图自由运行、由 s2mm 收够数据来终止。二者分属"仿真宿主"与"主机宿主"两条不同的执行路径。

---

### 4.2 参数配置与代码生成：从 my_params.cfg 到 config.h

#### 4.2.1 概念说明

AIE 图是 C++ 模板，模板参数（`POINT_SIZE`、`SSR`、`DATA_TYPE` 等）必须在**编译期**钉死。但 dsp 库不想让你直接改 `.hpp` 源码——它把所有可调参数集中到一个人类可读的 `.cfg` 文件（`my_params.cfg`），再用一组 Python 脚本把它翻译成：

- `config.h`：一组 `#define`，被 `aie.hpp`/`aie.cpp` 包含，从而把参数喂给 C++ 模板。
- `aie_params.cfg`：AIE 编译器（`v++ -c --mode aie`）的配置。
- 各 HLS 内核的 config：喂给 PL 转置内核的编译。

这就是"参数驱动代码生成"——**改一个 cfg，重新生成整个 AIE 子系统**。

#### 4.2.2 核心流程

```text
my_params.cfg  (你编辑)
     │
     ▼  update_cfg.py（补默认值、算 POINT_SIZE_D1）
INT_PARAMS.cfg（内部规范化参数）
     │
     ├──▶ gen_tb_from_cfg.py ──▶ config.h  (#define POINT_SIZE 4096 ...)
     ├──▶ extract_aie_cfg.py ──▶ aie_params.cfg (AIE 编译器配置)
     ├──▶ extract_hls_cfg.py ──▶ hls_params.cfg (PL HLS 编译配置)
     └──▶ create_config_json.py ──▶ metadata 校验 (meta_check)
```

`my_params.cfg` 里有一处关键概念：**分解 FFT 的二维分解**。一个 N 点一维 DFT

\[ X[k] = \sum_{n=0}^{N-1} x[n]\, W_N^{nk}, \quad W_N = e^{-j2\pi/N} \]

在 N = N₁ × N₂ 时可分解为一次二维 DFT 加旋转因子乘法（Cooley-Tukey）。本示例令

\[ N = \text{POINT\_SIZE} = \text{POINT\_SIZE\_D1} \times \text{POINT\_SIZE\_D2} \]

`POINT_SIZE_D1` 通常不手填，由 mk 按一张"经验平方根表"自动推断（见 4.3）。front/back/mid 三个"转置（transpose）"PL 内核就是负责在两次一维 FFT 之间做矩阵转置重排的。

#### 4.2.3 源码精读

先看示例真正使用的参数文件 `my_params.cfg`：

[dsp/L2/examples/vss_fft_ifft_1d/my_params.cfg:1-25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/my_params.cfg#L1-L25) — 三段：顶部的 `part=`/`freqhz=`（目标器件 xcvc1902 即 VCK190、PL 时钟 312.5MHz）；`[aie]` 段的 `enable-partition=6:35:fft_aie`（声明名为 `fft_aie` 的 AIE 分区）；`[APP_PARAMS]` 段是喂给 C++ 模板的应用参数（DATA_TYPE=cint32、POINT_SIZE=4096、SSR=4、VSS_MODE=1 等）。

几个最关键的参数含义：

| 参数 | 含义 | 本例取值 |
| --- | --- | --- |
| `POINT_SIZE` | FFT 点数 N | 4096 |
| `SSR` | 超采样并行度（PLIO 路数） | 4 |
| `DATA_TYPE` | 样本数据类型 | cint32（32 位复数） |
| `TWIDDLE_TYPE` | 旋转因子类型 | cint16 |
| `FFT_NIFFT` | 1=FFT，-1=IFFT | 1 |
| `VSS_MODE` | 1=AIE 全做（分解 FFT），2=前端 AIE + 后端 RTL xfft | 1 |
| `AIE_PLIO_WIDTH` | PLIO 物理位宽 | 128 |
| `API_IO` | 0=window 接口，1=stream 接口 | 0 |

`enable-partition` 里的 `fft_aie` 这个名字不是随便起的——它和主机里 `xrt::graph(...)` 引用的图名直接相关（见 4.4）。

再对比一下库自带的"默认参数样例" `vss_fft_ifft_1d_params.cfg`（VSS_MODE=2）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_params.cfg:9-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_params.cfg#L9-L27) — 与 `my_params.cfg` 的差异：`VSS_MODE=2`、`POINT_SIZE_D1=128`、`CASC_LEN=2`、`enable-partition` 名字是 `back_fft`。VSS_MODE=2 会触发 4.3 讲的另一条构建分支（带 RTL xfft_bd）。

参数如何变成 C++ 代码？入口在 `vss_fft_ifft_1d.mk` 的 `${AIE_HDR}` 规则（`AIE_HDR = config.h`）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:264-271](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L264-L271) — 先 `update_cfg.py` 把 `my_params.cfg` 规范化成 `internal_params.cfg`（补默认值、算 POINT_SIZE_D1），再 `gen_tb_from_cfg.py` 生成 `config.h`，最后 `extract_aie_cfg.py` 生成 `aie_params.cfg`。这三步是"cfg → 代码"的核心翻译链。

注意 `aie.hpp` 第 29 行 `#include "config.h"` —— 这就是参数进入 C++ 模板的入口。所有你在 `aie.hpp` 里看到的裸符号 `POINT_SIZE`、`SSR`、`DATA_TYPE`，其定义全部来自这个生成的 `config.h`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：搞清"我改的 cfg 参数，是怎么传到 C++ 模板里的"。
2. **操作步骤**：
   - 在 `my_params.cfg` 里找到 `SSR=4`、`POINT_SIZE=4096`。
   - 在 `aie.hpp` 里搜索 `SSR`、`POINT_SIZE` 的使用处（如第 58、84 行）。
   - 在 `aie.hpp` 顶部确认 `#include "config.h"`（第 29 行）。
   - 想象 `config.h` 里会有 `#define SSR 4`、`#define POINT_SIZE 4096` 等行（由 `gen_tb_from_cfg.py` 生成）。
3. **观察现象**：`aie.hpp` 里没有任何对 `SSR` 的字面量赋值，它完全依赖生成的 `config.h`。
4. **预期结果**：你能画出 `my_params.cfg → internal_params.cfg → config.h → aie.hpp 模板实参` 这条数据流。
5. **待本地验证**：实际生成的 `config.h` 内容需在跑过 `make ... AIE_HDR` 后查看（其路径在 `OUTPUT_DIR/config.h`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么参数要走"cfg → Python → config.h"而不是直接在 `.hpp` 里 `#define`？
**答案**：集中配置 + 自动派生。`POINT_SIZE_D1`、各 HLS 内核 config、AIE 编译器 config 都要由同一组源参数派生，用 Python 生成可保证一致性、避免多处手改不同步；同时 cfg 对非内核工程师更友好。

**练习 2**：`my_params.cfg`（VSS_MODE=1）与 `vss_fft_ifft_1d_params.cfg`（VSS_MODE=2）相比，少了哪几个参数？
**答案**：少了 `ADD_BACK_TRANSPOSE`、`ADD_FRONT_TRANSPOSE`、`POINT_SIZE_D1`、`CASC_LEN`。这些在 VSS_MODE=1 下要么由 mk 自动推断（POINT_SIZE_D1），要么取默认值（CASC_LEN=1、两个 TRANSPOSE 由 `check_aie_bd_use.py` 自动判定）。

---

### 4.3 VSS 构建：vss_fft_ifft_1d.mk 与 .vss 产物

#### 4.3.1 概念说明

AIE 图（`libadf.a`）本身不能直接和 PL 内核连成一个可上板系统——还需要一组 PL HLS 内核（本例是各种 transpose 转置内核 + splitter/joiner）做数据重排，再把它们和 AIE 图一起"链接"成一个统一单元。dsp 库把这个统一单元叫 **VSS（Vector Sub-System）**，产物是 `.vss` 文件，由 `v++ --link --mode vss` 生成。

可以把 VSS 想成"一个内含 AIE 子系统 + 配套 PL 内核的复合大内核"。后续 `example.mk` 再把 mm2s/s2mm 和 `.vss` 一起链接成最终的 xclbin。

`vss_fft_ifft_1d.mk` 是这个 VSS 的构建大脑，它按 `VSS_MODE` 分两条路：

- **VSS_MODE=1（本例）**：FFT 前后端都由 AIE 图做（分解 FFT）。PL 侧只需 front/mid/back 三个转置内核（+ splitter/joiner 当 AIE buffer descriptor 可用时）。
- **VSS_MODE=2**：前端 AIE 图做第一维，后端用 Vivado 产出的 RTL FFT 内核 `xfft_bd` 做第二维，需要额外的 `license_check` 和 vivado TCL 调用。

#### 4.3.2 核心流程（VSS_MODE=1 的依赖链）

```text
my_params.cfg
   │
   ▼
INT_PARAMS.cfg ──┬──▶ config.h ──▶ libadf.a   (v++ -c --mode aie 编译 aie.cpp)
                 ├──▶ hls_params.cfg
                 │
                 ├──▶ ifft_front_transpose 配置 ──▶ FRONT_XO   (v++ -c --mode hls)
                 ├──▶ ifft_transpose 配置     ──▶ MID_XO
                 ├──▶ ifft_back_transpose 配置 ──▶ BACK_XO_GEN
                 └──▶ VSSCFG  (vss_fft_ifft_1d_con_gen.py 按 SSR 生成连接)
                                   │
                                   ▼
   vss_fft_ifft_1d.vss  ◀── v++ --link --mode vss --part $(PART) --config (VSSCFG + libadf.a + 各 XO)
```

关键点：所有 XO（PL 内核）和 libadf.a（AIE 图）都依赖同一份 `INT_PARAMS.cfg`，从而保证 AIE 图的 SSR/POINT_SIZE 与 PL 转置内核的维度严格一致——这是参数驱动构建的最大价值。

#### 4.3.3 源码精读

参数提取全靠 Python，`SSR`、`POINT_SIZE` 都是从 cfg 里 shell-out 调 `extract_param_cfg.py` 读出来的：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:99-110](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L99-L110) — 用 `$(shell ...)` 调 `extract_param_cfg.py` 把 SSR、POINT_SIZE、VSS_MODE、DATA_TYPE、API_IO、CASC_LEN、USE_WIDGETS 等全部从 `PARAMS_CFG` 读进 make 变量。后续整个 mk 都用这些 make 变量驱动。

`POINT_SIZE_D1` 的自动推断（经验平方根表）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:133-141](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L133-L141) — 若用户没给 POINT_SIZE_D1，则按 VSS_MODE 推 POINT_SIZE_D2，再 `POINT_SIZE_D1 = POINT_SIZE / POINT_SIZE_D2`，保证 N = D1 × D2。对应 `host.cpp` 里那张 `fnPtSizeD1` 的查表（见 4.4）。

`VSS_MODE` 决定用哪个顶层 AIE 文件：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:143-148](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L143-L148) — VSS_MODE=1 用 `aie.cpp`（AIE 全做），否则用 `aie_front_only.cpp`（前端 AIE + 后端 RTL）。这就是 4.1 里 `AIE_GRAPH` 宏会指向不同图头文件的根本原因。

`libadf.a` 的编译（AIE 图编译）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:273-279](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L273-L279) — `v++ -c --mode aie` 编译 `${AIE_TL}`（aie.cpp），顺带把 `twiddle_rotator.cpp`、`fft_ifft_dit_1ch.cpp` 两个 AIE 运行时源（含 intrinsics，对应 u6-l2 讲的"L1/src/aie 放 intrinsics"约定）一起编译，产出 `libadf.a`。`${DSPLIB_OPTS}` 提供全部 include 路径与 `--mode aie --target=$(AIETARGET)`。

VSS_MODE=1 的最终链接：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:184-208](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L184-L208) — 收集 `VSS_DEPS`（VSSCFG + libadf.a + 各转置 XO），其中转置 XO 是否加入由 `ADD_FRONT/BACK_TRANSPOSE`（由 `check_aie_bd_use.py` 自动判定）决定；最后 `v++ --link --mode vss --part $(PART) --config $(VSS_DEPS)` 链接成 `.vss`。

`.vss` 链接命令本身（VSS_MODE=1 分支）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk:205-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L205-L206) — `v++ --link --mode vss`：`--mode vss` 是 VSS 专属模式，`--part` 指定目标器件（来自 cfg 的 `part=`），`--config` 把连接配置（VSSCFG，由 `vss_fft_ifft_1d_con_gen.py` 按 SSR 生成）和所有 XO、libadf.a 一并喂给链接器。

连接配置怎么按 SSR 生成？看 `vss_fft_ifft_1d_con_gen.py`：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py:191-198](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d_con_gen.py#L191-L198) — `for i in range(SSR):` 循环 SSR 次，每次写一条 `sc = front_transpose.sig_o_{i}:ai_engine_0.<name>_PLIO_front_in_{i}`。**SSR 每加 1，PL↔AIE 的流连接就多一组**——这是 SSR 与"流数量/图结构"关系的脚本级证据。

脚本里还有一个派生量 `ssr_int`（内部流数）：

[dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py:108-110](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py#L108-L110) — `PL_READ_WIDTH=128`，`samples_per_read = 128 // sample_size`（cint32 时 sample_size=64，得 2），`ssr_int = SSR × samples_per_read`（SSR=4 时得 8）。这说明 PL 内核内部其实是 8 路窄流，经 splitter/joiner 汇聚成 SSR=4 路宽 PLIO 与 AIE 相连。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：在不编译的前提下，追踪 VSS_MODE=1 下 `.vss` 的全部输入。
2. **操作步骤**：
   - 在 `vss_fft_ifft_1d.mk` 里找到 `VSS_DEPS` 的累加（第 186–200 行）。
   - 列出它会包含哪些产物：`VSSCFG`、`libadf.a`、`MID_XO`（或 `MID_XO_SINGLE_BUFF`+splitter+joiner）、可选的 `BACK_XO_GEN`、`FRONT_XO`。
   - 反查每个 XO 的编译规则（如 `${FRONT_XO}` 在第 290–291 行），确认它们都是 `v++ --compile --mode hls`。
3. **观察现象**：`.vss` 的输入 = 1 个 AIE 库（libadf.a）+ N 个 PL HLS 内核（XO）+ 1 个连接配置（VSSCFG），且全部派生自同一 `INT_PARAMS.cfg`。
4. **预期结果**：你能写出"`.vss` = libadf.a ∪ {front/mid/back transpose XO} ∪ VSSCFG"这个等式。
5. **待本地验证**：实际 `VSS_DEPS` 的精确成员取决于 `check_aie_bd_use.py` 对 `HAS_BD_TRANSPOSE` 的判定，需在目标 AIE variant 上确认。

#### 4.3.5 小练习与答案

**练习 1**：VSS_MODE=1 和 VSS_MODE=2 在"第二维 FFT 由谁做"上有何区别？
**答案**：VSS_MODE=1 由 AIE 图（分解 FFT）全做，第二维也在 AIE 阵列里；VSS_MODE=2 把第二维交给 Vivado 生成的 RTL 内核 `xfft_bd`（每个 SSR 路一个实例），AIE 只做前端第一维，因此 mk 里多了 vivado TCL 调用和 license 检查。

**练习 2**：为什么 `check_aie_bd_use.py` 的结果（`HAS_BD_TRANSPOSE`）能改变 PL 内核的清单？
**答案**：若 AIE 的 buffer descriptor 能在片上完成内部转置，就不再需要 PL 侧的 front/back transpose 内核（`ADD_FRONT/BACK_TRANSPOSE=0`），改用 mid_transpose+splitter+joiner；否则必须保留 PL 转置内核。这是"AIE 能力探测 → 自动裁剪 PL 内核"的优化。

---

### 4.4 端到端打包运行：example.mk 的五段流水与主机控制

#### 4.4.1 概念说明

4.3 产出的是 `.vss`（AIE 子系统 + 转置内核）。但要真正跑起来，还需要：把 mm2s/s2mm 这两个 DDR 搬运内核加进来、链接成完整 xsa、交叉编译嵌入式主机、打包成 SD 卡、在 hw_emu 里启动。这五步全在 `example.mk` 里，对应五个 make 目标：

| 目标 | 作用 | 关键命令 |
| --- | --- | --- |
| `vss` | 生成 `.vss`（委托给 4.3 的 mk） | `make -f vss_fft_ifft_1d.mk ... vss` |
| `example_xclbin` | 编译 mm2s/s2mm 为 XO 并链接成 xsa | `v++ -c` / `v++ -l` |
| `example_host` | 交叉编译 host.cpp 为 host.elf | `aarch64-linux-gnu-g++` |
| `example_sd_card` | 打包成 SD 卡 + kernel.xclbin | `v++ --package` |
| `example_run` | 启动 hw_emu 并校验 | `launch_hw_emu.sh` |

主机侧 `host.cpp` 用 `xrt::graph` 控制 AIE 图、用 `xrt::kernel` 驱动 mm2s/s2mm，是 u4-l2 主机控制链在"AIE 混合系统"下的扩展。

#### 4.4.2 核心流程

```text
make all
  │
  ├─ vss            : .vss (AIE 图 + 转置内核)
  │
  ├─ example_xclbin : mm2s.cpp ──v++ -c──▶ mm2s_wrapper.xo
  │                  s2mm.cpp ──v++ -c──▶ s2mm_wrapper.xo
  │                  v++ -l --config system.cfg ──▶ kernel_pkg.xsa   (含 .vss)
  │
  ├─ example_host   : host.cpp ──aarch64-g++──▶ host.elf   (用 SYSROOT + XRT 库)
  │
  ├─ example_sd_card: v++ --package ──▶ kernel.xclbin + package_hw_emu/ (SD 卡: Image/rootfs/launch 脚本)
  │
  └─ example_run    : launch_hw_emu.sh ──▶ 跑 host.elf ──▶ grep "TEST PASSED, RC=0"
```

主机控制链（AIE 版）：

```text
xrt::device(0)
  → load_xclbin("kernel.xclbin")              得 uuid
  → xrt::graph(dev, uuid, "fft_aie_fft_tb")   取 AIE 图句柄
       .reset() / .run(NUM_ITER=-1)           图自由运行
  → xrt::kernel(mm2s_wrapper:{mm2s})          取 PL 搬运内核
  → xrt::kernel(s2mm_wrapper:{s2mm})
  → 建 bo、填输入、set_arg、start、wait       驱动 mm2s/s2mm
  → bo.sync(FROM_DEVICE)                       取回结果
  → 与 ref_output.txt 比对、判 PASS/FAIL
```

#### 4.4.3 源码精读

先看 `example.mk` 顶部——**注意它有自己的硬编码参数，与 `my_params.cfg` 是两套来源**：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:19-25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L19-L25) — `POINT_SIZE := 4096`、`SSR := 4`、`DATA_TYPE := cint32`、`NITER := 4`、`DATAWIDTH := 64` 直接写在 mk 里。这些值用来编译 mm2s/s2mm 和 host，**不从 `my_params.cfg` 读**。它们必须与 `my_params.cfg` 保持一致——这是本讲最重要的工程陷阱（见综合实践）。

`vss` 目标把活儿委托给 4.3 的 mk：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:27-29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L27-L29) — 先 `meta_check`（参数合法性 + metadata 校验），再 `clean vss`，`PARAMS_CFG=my_params.cfg` 指明参数来源。注意它把 `HELPER_ROOT_DIR` 设为 `${DSPLIB_ROOT_DIR}`，使库脚本路径正确。

`example_xclbin` 编译两个搬运内核并链接：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:32-34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L32-L34) — `v++ -c -DNSTREAM=$(SSR) -DPOINT_SIZE=$(POINT_SIZE) ... -k s2mm_wrapper` 把搬运内核的 `NSTREAM`/`POINT_SIZE` 用 example.mk 自己的变量实例化（所以 mm2s/s2mm 的流数跟的是 example.mk 的 SSR，不是 cfg）；`v++ -l --config system.cfg` 把 mm2s.xo、s2mm.xo、`.vss` 链成 `kernel_pkg.xsa`。这就是 u5-l1 三段流水里的"链接"段，AIE 系统多了 `.vss` 这个输入。

`example_sd_card` 把 xsa 封成 SD 卡：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42) — `v++ --package`（`-p`）把 `kernel_pkg.xsa` + `libadf.a` 打成 `kernel.xclbin`，并生成 `package_hw_emu/` 目录（含 rootfs、Image、launch 脚本）。`--package.defer_aie_run` 让 AIE 图不自动启动（留给主机控制），`--package.sd_file` 把 host.elf、输入数据、ref 数据都塞进 SD 卡。

`example_run` 启动 hw_emu 并校验：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:44-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L44-L46) — `launch_hw_emu.sh -no-reboot -run-app run_script.sh` 启动 QEMU + hw_emu 并自动跑 `run_script.sh`；最后 `grep "TEST PASSED, RC=0" qemu_output.log` 判定整条链是否通过。`run_script.sh` 里设了 `XCL_EMULATION_MODE=hw_emu` 等环境变量后执行 `./host.elf`（见 [run_script.sh:17-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L17-L32)）。

主机 `host.cpp` 的 AIE 图控制——这是 u4-l2 主机链的 AIE 扩展：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:112-125](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L112-L125) — `xrt::device(0)` → `load_xclbin("kernel.xclbin")` → `xrt::graph(my_device, xclbin_uuid, "fft_aie_fft_tb")` 取图句柄，再 `reset()` / `run(NUM_ITER)`。`NUM_ITER=-1`（第 83 行）表示图自由运行，由 s2mm 收够数据终止。`"fft_aie_fft_tb"` 这个名字对应 `aie.cpp` 里实例 `fft_tb` 在 `fft_aie` 分区下的全名。

PL 搬运内核控制（与 u4-l2 一致）：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:131-135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L131-L135) — 用 `"mm2s_wrapper:{mm2s}"` / `"s2mm_wrapper:{s2mm}"` 取内核，`{mm2s}` 是实例名，对应 `system.cfg` 里 `nk = mm2s_wrapper:1:mm2s` 声明的实例。

bo 与并发点火——和 u4-l2 同构，但本例的并发是"mm2s 灌数据 ↔ AIE 图算 ↔ s2mm 收数据"三段同时在硬件里跑：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:196-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L196-L206) — `mm2s_run.start()` 与 `s2mm_run.start()` 都非阻塞提交后，主机只在 `s2mm_run.wait()` 阻塞等"收够"；AIE 图在 `run(-1)` 下独立运行。三段流水并发，主机零线程，整条链由 s2mm 收够终止（u4-l2 已建立的心智模型，此处套用到 PL+AIE 混合系统）。

结果校验——参考模型不是 bit 精确：

[dsp/L2/examples/vss_fft_ifft_1d/host.cpp:234-249](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L234-L249) — 逐样本与 `ref_output.txt` 比对，`level = (1 << 8)` 即允许实/虚部各有 256 的绝对误差，超过才标记 `***` 并置 FAIL。cint32 定点 FFT 的有限位宽与饱和会引入数值偏差，故用容差阈值而非逐位比较（u6-l1 已解释定点非 bit 精确，此处是其工程化判定）。

`system.cfg` 描述搬运内核的实例与 DDR 绑定：

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:10-14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L14) — `nk = mm2s_wrapper:1:mm2s` 声明 1 个 mm2s 实例（名 mm2s），`sp=mm2s.mem:LPDDR` 把它的 m_axi 端口绑到 LPDDR bank（即主机 `group_id(0)` 的来源，u4-l3 已讲）。

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:23-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L31) — `sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0` … 共 4 条（SSR=4），把 mm2s 的 4 路输出流连到 front_transpose；反向 4 条把 back_transpose 连到 s2mm。**4 条 = SSR=4 的直接体现**。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把 `example.mk` 五个目标与构建流水阶段对上号。
2. **操作步骤**：
   - 列出 `all: vss example_xclbin example_host example_sd_card example_run`（第 49 行）。
   - 对每个目标，定位它的关键命令（vss→vss_fft_ifft_1d.mk；xclbin→v++ -c/-l；host→aarch64-g++；sd_card→v++ --package；run→launch_hw_emu.sh）。
   - 在 `system.cfg` 里数 mm2s 的输出流条数，与 SSR 对比。
3. **观察现象**：五个目标恰好对应"生成 AIE 子系统 → 加搬运内核链接 → 编主机 → 打 SD 卡 → 跑仿真"五阶段；`system.cfg` 的 sc 条数随 SSR 线性增长。
4. **预期结果**：你能口述"要把这个示例跑起来，机器上至少要有 Vitis、XRT、`PLATFORM`、`SYSROOT`（含 rootfs/Image）四样东西，并依次跑五个 make 目标"。
5. **待本地验证**：实际 hw_emu 运行耗时极长（`description.json` 里 `vitis_hw_emu` 限 470 分钟、40GB 内存），需在真实环境验证。

#### 4.4.5 小练习与答案

**练习 1**：主机里 `xrt::graph(..., "fft_aie_fft_tb")` 的字符串从哪来？
**答案**：它对应 AIE 图实例 `fft_tb`（`aie.cpp` 第 20 行）在分区 `fft_aie`（`my_params.cfg` 的 `enable-partition=6:35:fft_aie`）下的全名。该名字在 AIE 编译时固化进 `libadf.a`，主机必须用完全相同的字符串引用，否则找不到图。

**练习 2**：为什么 `example_sd_card` 要加 `--package.defer_aie_run`？
**答案**：不加的话 SD 卡启动时 AIE 图会自动运行；但本示例的设计是"由主机 `host.cpp` 用 `xrt::graph.reset()/run()` 显式控制图的生命周期"（先准备好 mm2s/s2mm 再启动图）。`defer_aie_run` 把启动权交给主机，符合这一设计。

---

## 5. 综合实践

**任务**：修改 `my_params.cfg` 里的 `SSR`（或 `POINT_SIZE`），按 `example.mk` 的说明重新生成 vss，并定量解释该参数对**PLIO 流的数量**与**AIE 图/连接结构**的影响。这是把本讲四个模块串起来的综合练习。

### 步骤

1. **改参数（两处都要改！）**：
   - 把 `my_params.cfg` 第 23 行的 `SSR=4` 改为 `SSR=2`。
   - **同时**把 `example.mk` 第 21 行的 `SSR := 4` 也改为 `SSR := 2`。
   - 思考：为什么必须改两处？（见下方"陷阱"）
2. **重新生成 vss**：在 `dsp/L2/examples/vss_fft_ifft_1d/` 下执行
   ```bash
   make -f example.mk vss PLATFORM=<你的 VCK190 平台> DSPLIB_ROOT_DIR=<dsp 库根目录>
   ```
   观察构建日志里 Python 打印的 SSR、POINT_SIZE_D1、POINT_SIZE_D2。
3. **对比连接结构**：找到重新生成的 `vss_fft_ifft_1d_connectivity.cfg`（即 VSSCFG），与原 `system.cfg` 比 `sc =` 条数。
4. **定量分析**（无需上板，纸笔即可）：
   - PLIO 端口数：由 [aie.hpp:58-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L58-L61) 的 `std::array<..., SSR>` 决定，SSR 4→2 后端口数从 16 降到 8。
   - PL↔AIE 流连接：由 [con_gen.py:197-198](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py#L197-L198) 的 `for i in range(SSR)` 决定，每侧从 4 条降到 2 条。
   - 内部窄流 `ssr_int = SSR × samples_per_read`：cint32 时 samples_per_read=2，故 4→2 后 `ssr_int` 从 8 降到 4。
   - 理论吞吐：PLIO 总带宽约与 SSR 成正比，SSR 减半 → 吞吐约减半；代价是 AIE tile 占用与 PL 资源也约减半。

### 需要观察的现象与预期结果

- 重新生成的连接配置里 `sc =` 条数应从每组 4 条变为 2 条。
- AIE 图占用的 tile 数、PL 转置内核的端口数应随 SSR 同步缩减。
- 若只改了 `my_params.cfg` 没改 `example.mk`，则 `.vss` 用 SSR=2 生成、但 mm2s/s2mm 仍按 SSR=4 编译（`-DNSTREAM=4`），二者端口数不匹配，链接阶段会报流连接错误——这正好验证下面的陷阱。

### ⚠️ 关键陷阱：两套参数源

`my_params.cfg`（喂给 AIE 图与 PL 转置内核，经 4.2/4.3 的代码生成）与 `example.mk` 顶部的硬编码变量（喂给 mm2s/s2mm 编译与 host，见 [example.mk:19-25](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L19-L25)）是**两个独立来源**，仅靠人工约定保持一致。改 SSR/POINT_SIZE 必须两边同步，否则 AIE 侧与 PL 搬运侧的流数/维度对不上，链接失败。这是真实工程中最容易踩的坑。

> 说明：以上步骤涉及 Vitis/AIE 工具链与 VCK190 平台，完整 `make all`（尤其 `example_run` 的 hw_emu）耗时极长（`description.json` 标注上限 470 分钟）。若本地无环境，可只做"改参数 + 纸笔定量分析 + 读生成的 connectivity cfg"部分，标记为待本地验证。

## 6. 本讲小结

- 一个完整 AIE 系统的文件构成：AIE 图源码（`aie.hpp`/`aie.cpp`）+ 参数配置（`my_params.cfg`）+ PL 搬运（mm2s/s2mm）+ 转置内核 + 主机（`host.cpp`）+ 两层 make（`vss_fft_ifft_1d.mk` 与 `example.mk`）。
- ADF 图生命周期是 `init()→run(n)→end()`：仿真下由 `aie.cpp` 的 `main()` 驱动，上板下由主机 `xrt::graph` 的 `reset()/run()/end()` 驱动，两套共用同一图类。
- 参数驱动代码生成：`my_params.cfg → update_cfg.py → gen_tb_from_cfg.py → config.h`，`config.h` 里的 `#define` 直接喂给 C++ 模板，保证 AIE 图与 PL 内核维度一致。
- VSS（Vector Sub-System）= AIE 图（`libadf.a`）+ 若干 PL 转置内核 + 连接配置，由 `v++ --link --mode vss` 链接成 `.vss`；`VSS_MODE` 决定后端由 AIE（mode 1）还是 RTL `xfft_bd`（mode 2）做。
- `example.mk` 五目标对应端到端五阶段：`vss`（AIE 子系统）→ `example_xclbin`（加搬运内核链接）→ `example_host`（交叉编译）→ `example_sd_card`（打包）→ `example_run`（hw_emu 校验）。
- **SSR 直接决定 PLIO 流的数量**：`aie.hpp` 的 `std::array<..., SSR>` 与 `con_gen.py` 的 `for i in range(SSR)` 是同一事实的 C++ 侧与脚本侧证据。
- **最大工程陷阱**：`my_params.cfg` 与 `example.mk` 顶部硬编码变量是两套独立参数源，改 SSR/POINT_SIZE 必须两边同步。

## 7. 下一步学习建议

- **u12-l1（dataflow、SSR、II 调优）**：本讲只定性讲了 SSR 对吞吐的影响，下一阶段会定量分析 SSR、datawidth、II 如何共同决定吞吐与面积，可把本讲的 `SSR=2/4` 案例作为练手素材。
- **u13-l1（ADF 图、window/stream 与 PL↔AIE 边界）**：本讲的 `tl_graph` 是顶层 PLIO 包装图，底层 `fftGraph` 的 kernel/connect 细节、window 与 stream 的区别、PLIO vs GMIO 的边界桥接方式，留待 AIE 编程模型深入讲。
- **u13-l2（AIE 图主机控制与 SD 卡打包）**：本讲的 `xrt::graph` 控制、`--package.defer_aie_run`、`launch_hw_emu.sh` 只点到为止，下讲会完整展开 `system.cfg` 连接描述与 SD 卡生成细节。
- **u14-l3（基准评测与对标）**：本讲 `host.cpp` 用 `level=(1<<8)` 容差判 PASS/FAIL，下讲会系统讲各库 benchmarks 如何与参考模型比对、如何解读吞吐/延迟指标。
- 继续阅读：`dsp/L2/tests/vss/vss_fft_ifft_1d/`（VSS 的库内测试，比 example 更全的 param_set）、`dsp/L2/include/vss/scripts/` 下的 Python 脚本（理解整条代码生成链的实现）。
