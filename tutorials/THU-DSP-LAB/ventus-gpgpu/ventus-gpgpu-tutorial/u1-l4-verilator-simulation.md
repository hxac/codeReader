# Verilator 仿真与测试用例

## 1. 本讲目标

上一讲（u1-l2）我们学会了用 `make verilog` 把 Chisel 源码变成一份 `GPGPU_top.v`。但光有一份 Verilog 还不能「跑程序」——我们还需要：

1. 把 Verilog 编译成一个可执行的仿真模型；
2. 给这个模型喂入一段 GPU 程序（kernel）和初始数据；
3. 让模型一拍一拍地推进时钟，观察它执行得对不对。

本讲就来打通这条「从 Verilog 到跑通第一个测试用例」的路径。学完后你应当能够：

- 说清 `sim-verilator/` 仿真框架由哪三部分组成，以及它们各自的作用。
- 用 `make -j run` / `make RELEASE=1 -j run` 构建并运行仿真，知道 Debug 与 Release 输出的区别。
- 读懂 `.metadata`、`.data` 测试用例文件的二进制结构，以及 `ventus_args.txt` 如何把测试用例组织起来。
- 读懂仿真程序打印的 warp 执行、访存、分支等输出格式。
- 使用 `--dump-mem` 把执行结果导出并核对正确性。

## 2. 前置知识

### 什么是 Verilator

Verilator 是一个开源的 Verilog/SystemVerilog 仿真器。和传统的解释型仿真器（如 VCS、ModelSim 一类的商用工具）不同，Verilator 把 Verilog **编译成 C++ 代码**，再和你的 C++ 驱动一起编译成一个可执行文件。这样做的好处是速度快，缺点是它主要支持**可综合**的设计，对一些行为级语法支持有限。Ventus 的 RTL 是可综合 Chisel 生成的，正好适合用 Verilator。

编译出来的可执行文件里，「硬件」变成了一堆 C++ 类，「时钟」变成了你手动调用的 `step()` 函数——每调用一次就推进一个仿真时间单位（一个时钟周期）。

### 什么是 kernel、workgroup、warp

在跑测试前，要先把这几个 GPU 编程模型术语和硬件概念对上（更详细的讲解在 u2-l1）：

- **kernel（内核函数）**：你要在 GPU 上运行的那段程序，例如「向量加法」。
- **workgroup / block（线程块，简称 WG）**：一个 kernel 被切成很多个 block，每个 block 是调度到 SM 上的基本单位。
- **warp / wavefront（线程束，简称 WF）**：一个 block 内部再切成若干 warp，一个 warp 是真正同步执行的若干线程。
- **thread（线程）**：最细粒度的执行单元。

本讲你不需要深入这些概念，只要知道：一个测试用例 = 一个 kernel 的元信息（多少个 block、每个 warp 多少线程……）+ 它运行需要的初始数据。

### 动态库与 C API

`libVentusRTL.so` 是一个 **共享库（动态库）**。它在 C++ 内部用 `extern "C"` 暴露出一组函数（C API），这样无论是 C 还是 C++ 写的上层驱动，都能通过 `dlopen`/链接的方式调用它。理解「库提供 API、驱动调用 API」这种分层，是看懂本讲代码的关键。

## 3. 本讲源码地图

本讲聚焦 `sim-verilator/` 目录，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [sim-verilator/README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/README.md) | 仿真框架的使用说明、三部分划分、构建命令 |
| [sim-verilator/Makefile](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/Makefile) | 构建 mini driver 可执行文件 `sim-VentusRTL`，并 `include` 下方 `verilate.mk` |
| [sim-verilator/verilate.mk](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk) | 构建 `libVentusRTL.so` 动态库（可独立工作） |
| [sim-verilator/sim_main.cpp](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp) | mini driver 的入口 `main()`，串起「配置→加载 kernel→运行→dump 结果」 |
| [sim-verilator/cmdarg.cpp](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp) | mini driver 的命令行参数解析（`--kernel`/`--dump-mem`/`-f` 等） |
| [sim-verilator/kernel.cpp](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp) | `Kernel` 类：解析 `.metadata`、记录 block 调度状态 |
| [sim-verilator/ventus_rtlsim.h](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h) | `libVentusRTL.so` 对外暴露的 C API 与 `ventus_kernel_metadata_t` 结构定义 |
| [sim-verilator/testcase/](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase) | 自带的 `vecadd`、`matadd` 测试用例（含 `.metadata`/`.data`/`ventus_cmdargs.txt`） |
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md) | 项目总 README，含 `.metadata`/`.data` 字段定义与「程序输出含义」说明 |

## 4. 核心概念与源码讲解

`sim-verilator/` 的 README 把整个框架清晰地分成了三部分（[sim-verilator/README.md:22-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/README.md#L22-L30)）：

1. **`libVentusRTL.so` 动态库**：对「Chisel 硬件 + 物理内存 + 内核函数拆分」三部分的建模，相当于一张 GPU 板卡的仿真模型。
2. **mini driver**：一个示例性的小型驱动，读 `.metadata`/`.data` 生成测试用例并驱动上面的库。
3. **`testcase/`**：少量自带测试用例及其 `ventus_args.txt` 配置。

下面我们按这三个最小模块逐一拆解。

### 4.1 libVentusRTL.so：GPU 板卡的仿真模型

#### 4.1.1 概念说明

`libVentusRTL.so` 是整个仿真的「被测对象」。它把三样东西打包进一个动态库里：

- **Chisel 硬件**：经过 Verilator 编译后的 RTL 模型。注意，仿真用的顶层不是上一讲的 `GPGPU_top`，而是一个**仿真专用的包装** `GPGPU_SimTop`（见 [ventus/src/top/Mem_SimWrapper.scala:82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/Mem_SimWrapper.scala#L82)），它把 GPU 核心和一个仿真用的内存模型连在一起。
- **物理内存（physical memory）**：用软件模拟的、按页分配的内存，存放指令和数据的初值。
- **内核函数拆分（kernel decomposition）**：把一个 kernel 按 workgroup 拆开，逐个喂给硬件的 CTA 调度器。

这个库对外暴露一组 C API（声明在 `ventus_rtlsim.h`），上层驱动靠这些 API 来初始化、推进时钟、加入 kernel、读写物理内存。

#### 4.1.2 核心流程

构建 `libVentusRTL.so` 的流程（由 `verilate.mk` 驱动）：

1. **生成 Verilog**：用 Mill 调用 `top.emitVerilog`，生成仿真顶层并改名为 `dut.v`。
2. **生成 RTL 参数**：从 `parameters.json` 用 `json2cpp.py` 生成 `rtl_parameters.cpp`。
3. **Verilator 编译**：把 `dut.v` 和一组 C++ 源文件（硬件胶水、物理内存、CTA 派发包装、API 实现等）一起喂给 Verilator，生成静态库 `libVdut.a`。
4. **链接动态库**：把导出 API 的目标文件与 `libVdut.a`、`libverilated.a` 链接成最终的 `libVentusRTL.so`。

库对外 API 的典型调用时序（驱动侧视角）：

```
ventus_rtlsim_get_default_config()   // 取一份默认配置
  -> 修改 config（仿真时长、波形、物理内存等）
ventus_rtlsim_init(&config)          // 初始化，返回 sim 句柄
ventus_rtlsim_add_kernel__(...)      // 向 GPU 推入一个 kernel（含 metadata）
ventus_rtlsim_pmemcpy_h2d(...)       // 把 .data 初值写入物理内存
loop:
  ventus_rtlsim_step(sim)            // 推进 1 个仿真时间单位，返回状态
  直到 error / idle / time_exceed
ventus_rtlsim_pmemcpy_d2h(...)       // 读回结果内存（dump-mem）
ventus_rtlsim_finish(sim, false)     // 收尾
```

#### 4.1.3 源码精读

`verilate.mk` 里列出了构成动态库的全部 C++ 源文件（[sim-verilator/verilate.mk:63-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L63-L64)）：`ventus_rtlsim.cpp` 是导出 API 的入口，配合 `kernel.cpp`（kernel 容器）、`physical_mem.cpp`（物理内存）、`cta_sche_wrapper.cpp`（CTA 派发）、`ventus_rtlsim_impl.cpp`（仿真循环实现）等协同工作。

生成 Verilog 的规则展示了仿真顶层与综合顶层的区别（[sim-verilator/verilate.mk:145-148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L145-L148)）：

```makefile
$(VLIB_SRC_V) parameters.json &: $(VLIB_SRC_SCALA)
	cd .. && ./mill ventus[6.4.0].runMain top.emitVerilog
	mv GPGPU_SimTop.v $(VLIB_SRC_V)
```

这段规则说明：只要 `ventus/src` 下的任何 Scala 文件变了，就重新跑 `top.emitVerilog` 生成 `GPGPU_SimTop.v` 并改名为 `dut.v`（`VLIB_SRC_V`）。对比上一讲，综合用的是 `make verilog` → `top.GPGPU_gen` → `GPGPU_top.v`；**仿真用的是 `top.emitVerilog` → `GPGPU_SimTop`**。两者是不同的顶层入口。

链接出动态库的最终规则（[sim-verilator/verilate.mk:161-166](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L161-L166)）把 Verilator 产出的 `libVdut.a`、`libverilated.a` 与导出 API 的对象文件链接成 `libVentusRTL.so`，并依赖 `spdlog`、`fmt`、`pthread`、`z`、`atomic` 等库。

对外 API 在 `ventus_rtlsim.h` 中声明。其中推进仿真的核心函数（[sim-verilator/ventus_rtlsim.h:118-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L118-L121)）：

```c
// Calculate 1 unit-time of simulation.
// Return the result of this step: ok, error, time_exceed, or idle.
DLL_PUBLIC const ventus_rtlsim_step_result_t* ventus_rtlsim_step(ventus_rtlsim_t* sim);
```

每调用一次 `ventus_rtlsim_step` 就推进一个仿真时间单位，返回值用 `ventus_rtlsim_step_result_t` 表示三种终止条件（[sim-verilator/ventus_rtlsim.h:82-86](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L82-L86)）：`error`（致命错误或 RTL 调了 `$finish`）、`time_exceed`（超过最大仿真时间）、`idle`（所有 kernel 都跑完了）。主机与设备内存之间搬运数据则用 `ventus_rtlsim_pmemcpy_h2d` / `ventus_rtlsim_pmemcpy_d2h`（[sim-verilator/ventus_rtlsim.h:162-164](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L162-L164)）。

#### 4.1.4 代码实践

**实践目标**：单独构建 `libVentusRTL.so`，验证它能独立产出。

**操作步骤**：

1. 进入 `sim-verilator/` 目录。
2. 执行 `make -f verilate.mk`（Debug）或 `make -f verilate.mk RELEASE=1`（Release）。
3. 在 `build/libVentusRTL/debug/`（或 `release/`）下确认存在 `libVentusRTL.so`。

**需要观察的现象**：构建过程会先调用 `mill ... top.emitVerilog` 生成 `dut.v`，再由 Verilator 编译出 `libVdut.a`，最后链接出 `.so`。

**预期结果**：`build/libVentusRTL/<模式>/libVentusRTL.so` 文件生成成功。如果首次构建提示找不到 Scala 依赖，说明需要先在仓库根目录执行 `make init`（见 u1-l2）。**待本地验证**：构建耗时与机器核心数有关，Verilator 编译大设计较慢。

#### 4.1.5 小练习与答案

**练习 1**：为什么仿真用 `GPGPU_SimTop` 而不是直接用 `GPGPU_top`？

**参考答案**：`GPGPU_top` 是面向综合/上板的顶层，端口是 AXI 等「裸」接口；而仿真需要一个能直接用软件读写内存的环境。`GPGPU_SimTop` 在 `GPGPU_top` 外面包了一层仿真用的内存模型（见 `Mem_SimWrapper.scala`），让 Verilator 驱动可以方便地注入指令和数据初值，所以仿真走 `top.emitVerilog` → `GPGPU_SimTop` 这条路。

**练习 2**：`ventus_rtlsim_step` 的返回值有 `idle` 这一项，它表示什么？驱动据此会做什么？

**参考答案**：`idle` 表示当前没有 kernel 在运行（所有提交的 kernel 都已完成）。驱动一旦看到 `idle`（且非 `error`/`time_exceed`），就知道可以停止循环、开始 dump 结果内存并收尾了。

---

### 4.2 mini driver：驱动 `libVentusRTL.so` 的示例程序

#### 4.2.1 概念说明

`libVentusRTL.so` 只是一个库，本身不能直接「运行测试」。还需要一个可执行程序来调用它的 API。**mini driver**（编译产物叫 `sim-VentusRTL`）就是这个示例程序：它读取命令行参数，从 `.metadata`/`.data` 文件构造 kernel，喂给仿真库，再把结果 dump 出来。

它带一个简陋的命令行接口，可以用 `-f ventus_args.txt` 从文件读取一长串参数。它的源码很简短（`sim_main.cpp` + `cmdarg.cpp` + `kernel.cpp`），正是初学者理解「如何驱动这个 GPU 仿真模型」的最佳入口。

#### 4.2.2 核心流程

mini driver 的 `main()` 做四件事：

1. **配置仿真**：调用 `ventus_rtlsim_get_default_config` 取默认配置，再按命令行覆盖（仿真时长、波形等）。
2. **解析参数生成 kernel**：解析 `--kernel ...`，读 `.metadata`/`.data` 构造 `Kernel` 对象，再通过回调加入仿真库。
3. **运行仿真**：循环调用 `ventus_rtlsim_step` 直到出现终止条件。
4. **导出结果**：对命令行里 `--dump-mem` 指定的每个地址区间，用 `ventus_rtlsim_pmemcpy_d2h` 读回并按 4 字节一行打印。

构建上，`Makefile` 先 `include verilate.mk`，所以一旦 `libVentusRTL.so` 不存在会自动先构建它，再把 `sim_main.cpp`、`cmdarg.cpp`、`kernel.cpp` 三个源文件编译链接成 `sim-VentusRTL`。

#### 4.2.3 源码精读

mini driver 的源文件清单和最终可执行名在 `Makefile` 里给出（[sim-verilator/Makefile:37-41](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/Makefile#L37-L41)）：

```makefile
SRC_CXX = sim_main.cpp cmdarg.cpp kernel.cpp
...
APP = $(DIR_BUILDOBJ)/sim-VentusRTL
```

`Makefile` 通过 `include verilate.mk`（[sim-verilator/Makefile:50](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/Makefile#L50)）把动态库的构建规则引进来，因此 `make` 时缺库会自动补建。Debug/Release 由 `RELEASE` 变量切换（`-g -O0` vs `-O2`），输出分别在 `build/driver_example/debug` 与 `release` 子目录。

`main()` 的整体骨架（[sim-verilator/sim_main.cpp:22-61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L22-L61)）先取默认配置，再解析参数。当不带任何命令行参数运行时，它会自动套用默认参数（[sim-verilator/sim_main.cpp:47-55](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L47-L55)）：

```cpp
if (argc == 1) { // Default arguments
    puts("[Info] using default cmdline arguments: -f ventus_args.txt");
    args.push_back("-f");
    args.push_back("ventus_args.txt");
}
```

也就是说，直接跑 `./sim-VentusRTL` 等价于 `./sim-VentusRTL -f ventus_args.txt`，而仓库里的 `ventus_args.txt` 是一个指向 `testcase/matadd/ventus_cmdargs.txt` 的软链接。

仿真的主循环非常直白（[sim-verilator/sim_main.cpp:74-80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L74-L80)）：一直 `step`，直到出现 `error`/`idle`/`time_exceed` 任一终止条件就退出。

`--dump-mem` 的实现在循环结束后（[sim-verilator/sim_main.cpp:90-103](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L90-L103)）：对每个地址区间 `[begin, end]` 用 `ventus_rtlsim_pmemcpy_d2h` 拷出一块内存，再每 4 字节按 `dump-mem: mem[0x... +: 4] = 0x...` 格式打印，这正是我们核对执行结果的依据。

命令行参数的解析在 `cmdarg.cpp` 的 `parse_arg` 中（[sim-verilator/cmdarg.cpp:20-117](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L20-L117)）。其中 `-f FILE` 会切到该文件所在目录去解析它（因为 `.metadata`/`.data` 用的是相对路径，见 [sim-verilator/cmdarg.cpp:33-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L33-L64)），并支持 `#` 注释。`--kernel` 参数形如 `name=vecadd,metafile=vecadd.metadata,datafile=vecadd.data`，由 `cmdarg_kernel` 解析（[sim-verilator/cmdarg.cpp:71-78](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L71-L78) 与 [sim-verilator/cmdarg.cpp:119-185](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L119-L185)）。全部支持的参数及其含义见帮助文本（[sim-verilator/cmdarg.cpp:237-261](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L237-L261)），可执行 `./sim-VentusRTL --help` 查看。

README 也把常用参数总结得很清楚（[sim-verilator/README.md:53-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/README.md#L53-L57)）：`-f` 读参数文件、`--waveform` 导出 FST 波形、`--dump-mem 0x90001000,0x90001020` 导出指定地址范围（4 字节一行）。

#### 4.2.4 代码实践

**实践目标**：构建 mini driver 并查看它支持的命令行参数。

**操作步骤**：

1. 进入 `sim-verilator/` 目录。
2. 执行 `make -j`（仅构建，不运行）。Debug 产物在 `build/driver_example/debug/sim-VentusRTL`。
3. 执行 `./build/driver_example/debug/sim-VentusRTL --help`。

**需要观察的现象**：`--help` 会打印 `cmdarg_help` 里的全部参数说明，包括 `--kernel` 的 `name/metafile/datafile` 子参数、`--dump-mem BEGIN,END`、`--waveform`、`--sim-time-max`、`--snapshot` 等。

**预期结果**：看到一段参数帮助文本。注意默认配置里仿真时长上限是 `sim_time_max = 200000`（见 [sim-verilator/sim_main.cpp:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L35)），可以用 `--sim-time-max` 覆盖。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `-f ventus_args.txt` 在解析时会先把当前工作目录切到该文件所在目录？

**参考答案**：因为 `ventus_args.txt` 里的 `--kernel` 用的是相对路径（如 `metafile=vecadd.metadata`），而 `.metadata`/`.data` 必须和该 txt 在同一目录。`cmdarg.cpp` 在解析 `-f` 时先 `canonical` 得到绝对路径、`current_path` 切到其父目录（[sim-verilator/cmdarg.cpp:40-42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L40-L42)），解析完再切回原目录，这样相对路径才能正确找到测例文件。这也是 README 强调「`ventus_args.txt` 必须和 `.metadata`/`.data` 同路径」的原因。

**练习 2**：`make -j run` 和 `make -j` 有什么区别？

**参考答案**：`make -j` 只构建出 `sim-VentusRTL` 可执行文件（`default: $(APP)`，[sim-verilator/Makefile:48](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/Makefile#L48)）；而 `make -j run` 在构建完成后会额外执行该可执行文件（`run` 目标，[sim-verilator/Makefile:83-86](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/Makefile#L83-L86)），即「构建并立即跑一遍默认测例」。

---

### 4.3 testcase：`.metadata`、`.data` 与 `ventus_args.txt`

#### 4.3.1 概念说明

测试用例由三类文件组成：

- **`.metadata`**：描述 kernel 的元信息——起始 PC、grid 三维规模、每个 warp 的线程数、每个 block 的 warp 数、寄存器用量、各缓冲区基址/大小等。它是**驱动用的**结构（注意 `ventus_rtlsim.h:21` 的注释：「这个 metadata 是供驱动使用的，而不是给硬件的」）。
- **`.data`**：按 `.metadata` 中各 buffer 顺序排列的初始化数据，包括指令码、输入参数、私有内存初值等。
- **`ventus_cmdargs.txt`**（常软链成 `ventus_args.txt`）：一份命令行参数清单，至少含一行 `--kernel name=...,metafile=...,datafile=...`，把上面两个文件串起来。

仓库自带 `vecadd`（向量加法）和 `matadd`（矩阵加法）两个测例。README 也明确说明：这种 `.metadata`+`.data` 的方式目前仅做兼容保留，新生成测例推荐用完整工具链（ventus-env + POCL）驱动 `libVentusRTL.so`（[sim-verilator/README.md:30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/README.md#L30)）。

#### 4.3.2 核心流程

**`.metadata` 文件结构**：本质是一个紧凑的二进制结构体序列化，每个 `uint64_t` 占 8 字节、写成两行（每行 4 字节的十六进制）。其字段定义见 README（[README.md:128-152](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L128-L152)），驱动侧等价结构在 `ventus_rtlsim.h`（[sim-verilator/ventus_rtlsim.h:21-42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L21-L42)）。关键字段：

| 字段 | 含义 |
| --- | --- |
| `startaddr` | 指令起始地址（PC） |
| `kernel_size[3]` | grid 三维规模（每个 kernel 的 workgroup 数目） |
| `wf_size` | 每个 warp 的 thread 数目 |
| `wg_size` | 每个 workgroup 的 warp 数目 |
| `metaDataBaseAddr` | CSR_KNL 的值 |
| `ldsSize` / `pdsSize` | 每个 workgroup 的 local memory 大小 / 每个 thread 的 private memory 大小 |
| `sgprUsage` / `vgprUsage` | 每个 warp 使用的标量 / 向量寄存器数目 |
| `pdsBaseAddr` | private memory 基址（按 `wf_size*wg_size*pdsSize` 偏移分配给各 workgroup） |
| `num_buffer` | buffer 数目（含指令 buffer、参数 buffer、私有内存等） |
| `buffer_base[]` / `buffer_size[]` / `buffer_allocsize[]` | 各 buffer 的基址 / 实际使用大小 / 分配大小 |

**`.data` 文件结构**：按 buffer 顺序连续存放，总长度为 `sum(buffer_size)` 字节（[README.md:158-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L158-L167)）。每行 4 字节，由驱动按小端序解析后写入对应 `buffer_base`。

**驱动如何把它们加载进仿真**：`Kernel` 构造时调用 `initMetaData` → `readHexFile` → `assignMetadata` 把 `.metadata` 反序列化到结构体（[sim-verilator/kernel.cpp:150-189](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L150-L189)）；`.data` 则在 kernel 被激活时由回调 `kernel_load_data_callback` 逐 buffer 读入并通过 `ventus_rtlsim_pmemcpy_h2d` 写进物理内存（[sim-verilator/sim_main.cpp:109-144](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L109-L144)）。

#### 4.3.3 源码精读：以 `vecadd` 为例

`vecadd` 的参数文件只有一行（[sim-verilator/testcase/vecadd/ventus_cmdargs.txt:7](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/ventus_cmdargs.txt#L7)）：

```
--kernel name=vecadd,metafile=vecadd.metadata,datafile=vecadd.data
```

其中 `vecadd.metadata` 和 `vecadd.data` 都是软链，分别指向 `vecadd_32b8w8t.metadata` / `vecadd_32b8w8t.data`（即「32 个 block」规模的版本）。

手工解析这份 `.metadata`（每个字段占 2 行，低 32 位在前）。以 `vecadd_32b8w8t.metadata` 为例（[sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata:1-28](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata#L1-L28)）：

| 字段 | 行号 | 解析值 |
| --- | --- | --- |
| `startaddr` | L1-L2 | `0x80000000` |
| `kernel_id` | L3-L4 | `0` |
| `kernel_size[0]` | L5-L6 | `0x20` = 32（block 数） |
| `kernel_size[1]` / `[2]` | L7-L10 | `1` / `1` |
| `wf_size` | L11-L12 | `8`（每 warp 8 线程） |
| `wg_size` | L13-L14 | `4`（每 block 4 warp） |
| `metaDataBaseAddr` | L15-L16 | `0x90404000` |
| `ldsSize` / `pdsSize` | L17-L20 | `0x1000` / `0x1000` |
| `sgprUsage` / `vgprUsage` | L21-L24 | `0x40` / `0x40`（各 64） |
| `pdsBaseAddr` | L25-L26 | `0x90004000` |
| `num_buffer` | L27-L28 | `7` |

随后是 3 组各 7 个 `uint64_t`：`buffer_base`、`buffer_size`、`buffer_allocsize`（[sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata:29-70](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata#L29-L70)）。其中三个 `0x1000` 字节的 buffer 分别位于 `0x90000000`、`0x90001000`、`0x90002000`，对应向量加法的输入 A、输入 B、输出 C；指令 buffer 在 `0x80000000`（`buffer_size=0x478`）。

`.data` 文件每行是一个 4 字节十六进制字，驱动按**小端序**还原成 32 位整数（[sim-verilator/sim_main.cpp:124-132](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L124-L132)）。例如 `vecadd_32b8w8t.data` 开头几行（[sim-verilator/testcase/vecadd/vecadd_32b8w8t.data:1-4](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/vecadd_32b8w8t.data#L1-L4)）是 `00000000`、`3f800000`、`40000000`、`40400000`，按 IEEE-754 单精度浮点解释分别是 `0.0`、`1.0`、`2.0`、`3.0`……即输入数组是一串递增的浮点数。

> 关于程序输出格式：README 的「Understanding Program Output」一节给出了 RTL 仿真打印的格式说明（[README.md:192-211](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L192-L211)），例如 `warp 3 0x800001d4 0x0042a303 x 6 90000000` 表示 warp 3 执行地址 `0x800001d4` 的指令，向标量寄存器 6 写入 `90000000`；`v 5 0001 ...` 表示对向量寄存器 5、按掩码 `0001`（只有最后一个线程活跃）写入。访存与分支输出格式见 [README.md:225-252](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L225-L252)（`lsu.r`/`lsu.w`、`setrpc`/`vbranch`/`join`）。需要说明：仓库里 `testcase/*/*.log` 文件是参考 ISA 模拟器（SPIKE）的 `core N: ...` 格式追踪，并非 RTL 的 `warp N ...` 输出；实际 RTL 打印格式以运行仿真后 `logs/ventus_rtlsim.log` 中的内容为准。**待本地验证**：不同版本下 RTL 打印的具体前缀可能略有差异。

#### 4.3.4 代码实践

**实践目标**：读懂一份 `.metadata` 并预测 kernel 规模。

**操作步骤**：

1. 打开 `sim-verilator/testcase/matadd/matadd.metadata`。
2. 按「每字段 2 行、低 32 位在前」的规则，依次解出 `startaddr`、`kernel_size[3]`、`wf_size`、`wg_size`、`num_buffer`。
3. 与 README 的结构定义（[README.md:128-152](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L128-L152)）逐字段对照。

**需要观察的现象**：`matadd` 的 `kernel_size[0]`（L5-L6）为 `0x4`=4，`wf_size`（L11-L12）为 `8`，`wg_size`（L13-L14）为 `8`。

**预期结果**：你能独立算出该 kernel 共有 `4×1×1=4` 个 workgroup，每个 workgroup 含 8 个 warp、每个 warp 8 个线程。`num_buffer` 同样是 7（L27-L28）。

#### 4.3.5 小练习与答案

**练习 1**：`.metadata` 里 `pdsBaseAddr` 是「整个 kernel 的私有内存基址」，驱动如何把它换算成「每个 workgroup 的私有内存起始地址」？

**参考答案**：README 注明，`pdsBaseAddr` 会被驱动/测试激励按 `wf_size * wg_size * pdsSize` 的步长偏移，换算成每个 workgroup 的起始地址（[README.md:140-143](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L140-L143)）。即第 *i* 个 workgroup 的私有内存基址 ≈ `pdsBaseAddr + i * wf_size * wg_size * pdsSize`。

**练习 2**：为什么说 `.metadata` 是「给驱动用的，而不是给硬件的」？

**参考答案**：硬件（GPU 核心）在运行时并不直接读取这份完整的 `.metadata` 文件；驱动（`Kernel` 类 + CTA 派发包装）解析它，从中取出 `kernel_size`/`wf_size`/`wg_size`/`sgprUsage`/`vgprUsage` 等，把 kernel 拆成一个个 workgroup，计算每个 workgroup 的资源（寄存器基址、私有内存基址、local memory 偏移等），再以硬件期望的格式（如 `wf_tag`、CSR 初值）派发给 CTA 调度器。所以这份结构是驱动层的视图（见 `ventus_rtlsim.h:21` 的注释）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的仿真与验证。

**任务**：运行 `vecadd` 测试用例，导出输出缓冲区，核对向量加法结果是否正确。

**步骤**：

1. 进入 `sim-verilator/` 目录，确认 `ventus_args.txt` 当前指向哪个测例（仓库默认软链到 `testcase/matadd/`）。我们要跑 `vecadd`，所以直接用 vecadd 的参数文件构建并运行：

   ```bash
   make RELEASE=1 -j
   ./build/driver_example/release/sim-VentusRTL \
     -f testcase/vecadd/ventus_cmdargs.txt \
     --dump-mem 0x90002000,0x90002040
   ```

   > 说明：`make run` 默认执行 `$(APP)` 且不带额外参数，会走默认的 `-f ventus_args.txt`（即 matadd）。要换测例，最稳妥的是先 `make -j` 构建出 `sim-VentusRTL`，再手动用 `-f` 指定参数文件运行。

2. 回顾 4.3.3 的解析：`vecadd` 三个 `0x1000` 字节 buffer 分别是输入 A（`0x90000000`）、输入 B（`0x90001000`）、输出 C（`0x90002000`）。所以 dump 输出区 `0x90002000` 即可看到结果。

3. 仿真结束后，stdout 会按 4 字节一行打印 `dump-mem: mem[0x90002000 +: 4] = 0x...`。把每个结果按 IEEE-754 单精度浮点解释（例如 `0x40000000`=`2.0`）。

4. 用同样的方式额外 dump 输入区核对：

   ```bash
   ./build/driver_example/release/sim-VentusRTL \
     -f testcase/vecadd/ventus_cmdargs.txt \
     --dump-mem 0x90000000,0x90000010 \
     --dump-mem 0x90001000,0x90001010 \
     --dump-mem 0x90002000,0x90002010
   ```

   （注意 `--dump-mem` 的两个地址都必须给出，且 `begin ≤ end`，见 [sim-verilator/cmdarg.cpp:187-226](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cmdarg.cpp#L187-L226)。）

**验收标准**：对每个下标 *i*，`C[i] == A[i] + B[i]`（浮点加法）。输入 A 以 `0,1,2,3,...` 递增，按此推算 B 与 C 的关系是否成立。若结果吻合，说明 RTL 正确执行了这段向量加法 kernel。

**延伸**（可选）：加 `--waveform` 重新跑一次，到 `logs/` 下用 gtkwave 打开生成的 FST 波形，观察 host 派发 kernel 到 SM 执行的信号变化——这是后续 u2/u3 单元深入硬件细节的预告。

## 6. 本讲小结

- `sim-verilator/` 仿真框架由三部分组成：`libVentusRTL.so`（被测的 GPU 板卡仿真模型）、mini driver（驱动该库的示例程序 `sim-VentusRTL`）、`testcase/`（自带测试用例）。
- 仿真顶层是 `GPGPU_SimTop`（由 `top.emitVerilog` 生成），不同于综合用的 `GPGPU_top`；`verilate.mk` 负责 Verilog 生成、Verilator 编译与动态库链接。
- 用 `make -j run`（Debug）或 `make RELEASE=1 -j run`（Release）即可「构建并运行」；Debug/Release 产物分别在 `build/**/debug`、`build/**/release`。
- mini driver 的 `main()` 走「配置 → 解析参数生成 kernel → 循环 `step` → dump 结果」四步，命令行参数可用 `--help` 查看，`-f ventus_args.txt` 从文件读参数。
- `.metadata`（驱动视角的 kernel 元信息，每 `uint64_t` 两行）+ `.data`（按 buffer 顺序的初始化数据，小端序）+ `ventus_cmdargs.txt` 共同描述一个测试用例。
- `--dump-mem BEGIN,END` 在仿真结束后按 4 字节一行导出物理内存，是核对执行结果正确性的关键手段。

## 7. 下一步学习建议

- **想理解 GPU 编程模型**：进入 u2-l1，系统学习 grid/block/warp/thread 层级，以及本讲遇到的 `wf_size`/`wg_size`/CSR 等概念在硬件中的对应。
- **想理解 kernel 是怎么被派发到 SM 的**：本讲的 `cta_sche_wrapper.cpp` 只展示了驱动侧的「拆分」，真正的硬件调度在 u3 单元（CTA 调度器）。
- **想理解仿真模型内部**：u7-l3 会深入 `ventus_rtlsim_impl.cpp` 的仿真循环、快照机制与物理内存实现。
- **想用完整工具链跑自己的 OpenCL 程序**：参考 [ventus-env](https://github.com/THU-DSP-LAB/ventus-env)，POCL 会自动导出 `.metadata`/`.data`，免去手工构造测例。
