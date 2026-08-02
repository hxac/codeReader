# Verilator 仿真框架深入

> 前置讲义：u1-l4《Verilator 仿真与测试用例》。本讲在那篇「跑通第一个测试用例」的基础上，把 `sim-verilator/` 仿真框架的内部实现彻底拆开，讲清楚 `libVentusRTL.so` 这个「GPU 板卡仿真模型」是如何用软件把硬件、物理内存、内核拆分三件事建模出来的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `libVentusRTL.so` 对外暴露的 C API 有哪些，以及它们之间的调用顺序（init → add_kernel → step 循环 → finish）。
- 读懂 `ventus_rtlsim_t` 这个核心结构体：它如何持有 DUT（被测硬件）、物理内存、CTA 派发器，以及 `step()` 一次时钟沿里做了哪些事。
- 理解 `step()` 主仿真循环里「负沿施加激励 → 正沿检测握手 → eval → 波形/快照」的四段式结构，以及半周期时间 `HALF_CYCLE_TIME=5` 与时钟的关系。
- 掌握 `Cta` 如何管理一串 `Kernel`、并把每个 kernel 的 workgroup 逐个填到 DUT 的 `host_req` 端口上派发。
- 掌握 `Kernel` 如何解析 `.metadata` 文件、维护每个 workgroup 的 WAITING/RUNNING/FINISHED 状态。
- 理解 `PhysicalMemory` 为什么用「按页分配的稀疏字典」而不是一大块连续内存来建模 DDR。
- 能在 `sim_main.cpp` 基础上，写出（或描述）一段调用 C API 的最小自定义 driver。

## 2. 前置知识

在进入源码前，先澄清几个本讲反复出现的概念。

**Verilator 与 DUT。** Verilator 把 Verilog/SystemVerilog 源码翻译成一个 C++ 类（本项目里叫 `Vdut`，见 [verilate.mk:L137](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L137) 的 `--prefix Vdut`）。这个类的实例 `dut` 就是被测硬件的软件化身：它有 `clock`、`reset` 等输入端口，也有 `io_host_req_*`、`io_mem_*` 等业务端口，调用 `dut->eval()` 就让所有组合逻辑和触发器推进一拍。仿真 driver 的本质就是「反复翻转 clock、施加激励、调用 eval」。

**软件 driver 与硬件 host。** 在真实硬件里，GPU 旁边有一个 host CPU 通过 AXI 总线派发 kernel（见 u7-l2）。而在仿真里，没有真实的 host CPU，于是 `sim-verilator/` 用 C++ 代码扮演 host 的角色：它读 `.metadata`/`.data` 文件，把 workgroup 一个个「喂」给 DUT 的 `host_req` 端口。`Cta` 类就是这个「软件 host」的核心。

**workgroup / warp / thread。** 沿用 u2-l1 与 u3 单元的约定：一个 kernel 被拆成若干 workgroup（WG，也叫 block/CU 任务），每个 workgroup 又含若干 warp（WF），每个 warp 含若干 thread。本讲关注 **driver 视角**：driver 只认 workgroup 这一级（对应硬件 `host_req` 的一次握手），warp 拆分由硬件 CTA 调度器（u3 单元）完成。

**FST 波形与快照（snapshot）。** FST（Fast Signal Trace）是 Verilator/GTKWave 使用的波形格式。仿真可以按时间区间导出 FST。本项目还实现了一种基于 `fork()` 的「快照回溯」机制：周期性地 fork 子进程冻结仿真状态，主进程出错时唤醒某个子进程、打开波形重新跑一段，用来定位 bug。

## 3. 本讲源码地图

本讲涉及的文件全部在 `sim-verilator/` 下，分工如下：

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `ventus_rtlsim.h` | C API 的公开头文件：`ventus_rtlsim_t` 前向声明、metadata/config/step_result 结构体、全部 `ventus_rtlsim_*` 函数原型 | API 契约 |
| `ventus_rtlsim.cpp` | C API 的薄封装：每个 `ventus_rtlsim_*` 函数转调 `ventus_rtlsim_t` 的方法 | API 入口 |
| `ventus_rtlsim_impl.hpp` | `ventus_rtlsim_t` 结构体的字段与方法声明、`snapshot_t` 定义 | 核心结构 |
| `ventus_rtlsim_impl.cpp` | 仿真核心实现：`constructor`/`step`/`destructor`/波形/快照 | **最关键** |
| `cta_sche_wrapper.hpp/.cpp` | `Cta` 类：管理 kernel 列表、逐 workgroup 派发与完成回收 | 软件派发器 |
| `kernel.hpp/.cpp` | `Kernel` 类：解析 `.metadata`、维护 workgroup 状态机 | 元数据容器 |
| `physical_mem.hpp/.cpp` | `PhysicalMemory` 类：按页分配的稀疏物理内存 | DDR 模型 |
| `sim_main.cpp` | mini driver 的 `main()`：调用 C API 的完整示例 | 实践范本 |
| `cmdarg.cpp` | 命令行解析：`--kernel`/`--dump-mem`/`-f` 等 | driver 周边 |
| `json2cpp.py` | 把 `parameters.json` 转成 `rtl_parameters.cpp` | 参数生成 |
| `verilate.mk` | 构建 `libVentusRTL.so` 的规则 | 构建链路 |
| `Makefile` | 构建 mini driver 可执行文件，include 了 `verilate.mk` | 构建链路 |

## 4. 核心概念与源码讲解

### 4.1 仿真框架总体架构与构建

#### 4.1.1 概念说明

`sim-verilator/` 的全部代码可以切成三块，这是 README 里明确给出的划分（见 [sim-verilator/README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/README.md)）：

1. **`libVentusRTL.so` 动态库**——对「Chisel 硬件 + 物理内存 + 内核拆分」三部分的软件建模，相当于一块 GPU 板卡的仿真模型，对外暴露一组 C API。
2. **mini driver**——一个示例程序（可执行文件 `sim-VentusRTL`），通过读 `.metadata`/`.data` 文件生成测试用例，调用上述 C API 驱动仿真。
3. **`testcase/`**——少量测试用例及其 `ventus_cmdargs.txt` 配置。

三者关系：mini driver 链接 `libVentusRTL.so`，后者内部封装了 Verilator 生成的 `Vdut`。也就是说，「硬件」是 `.so` 的一部分，「如何驱动硬件」是 mini driver 的事。这种切分让你可以完全不碰 mini driver，自己写程序链接 `.so` 来跑仿真——这正是 ventus-env 完整工具链的做法。

`libVentusRTL.so` 把三件事捏在一个库里：硬件（`Vdut`）、物理内存（`PhysicalMemory`）、内核拆分（`Cta`+`Kernel`，即把 kernel 拆成 workgroup 喂给硬件）。这三者共同由核心结构体 `ventus_rtlsim_t` 持有。

#### 4.1.2 核心流程

构建链路有两条，分别由 `verilate.mk` 和 `Makefile` 描述：

```text
(1) 构建 libVentusRTL.so（verilate.mk）
    ventus/src/*.scala
        │  mill ventus[6.4.0].runMain top.emitVerilog
        ▼
    GPGPU_SimTop.v  ──mv──▶  dut.v          （仿真顶层，外挂内存模型）
    parameters.json ──json2cpp.py──▶ rtl_parameters.cpp
        │
        │  verilator -cc --build --prefix Vdut  dut.v  *.cpp
        ▼
    libVdut.a + libverilated.a
        │  g++ -shared  (仅链接 VLIB_OBJ_EXPORT 即 ventus_rtlsim.cpp 的 .o)
        ▼
    libVentusRTL.so

(2) 构建 mini driver（Makefile，include verilate.mk）
    sim_main.cpp cmdarg.cpp kernel.cpp  ──g++──▶  sim-VentusRTL
        （链接 -lVentusRTL）
```

注意几个关键点：

- 仿真用的 Verilog 顶层是 `GPGPU_SimTop`（由 `top.emitVerilog` 生成，外挂仿真内存模型），**不是**综合用的 `GPGPU_top`，详见 u1-l4。生成规则见 [verilate.mk:L145-L149](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L145-L149)。
- `rtl_parameters.cpp` 不是手写的，而是 `json2cpp.py` 从 `parameters.json` 生成的（[verilate.mk:L148-L149](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L148-L149)），它定义了 `rtl_parameters` 这个 `unordered_map<string,uint32_t>`，供 C API `ventus_rtlsim_get_parameter` 查询硬件规模。
- 决定哪些符号导出成 `.so` 公开 API 的，是 [verilate.mk:L63-L64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L63-L64) 的 `VLIB_SRC_CXX_EXPORT = ventus_rtlsim.cpp`，最终链接时只把这些对象文件的符号设为可见（`-fvisibility=hidden` + `__attribute__((visibility("default")))`，见 [verilate.mk:L119](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L119) 与 [ventus_rtlsim.h:L7-L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L7-L9)）。

#### 4.1.3 源码精读

C API 的公开头文件定义了三个核心结构体。

**metadata 结构体**——这是 driver 传递给 `.so` 的「一个 kernel 的全部信息」：

[sim-verilator/ventus_rtlsim.h:L21-L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L21-L42) 定义了 `ventus_kernel_metadata_t`，注意头文件里的注释明确写着「这个 metadata 是供驱动使用的，而不是给硬件的」。它包含 `startaddr`（起始 PC）、`kernel_size[3]`（workgroup 的三维数目）、`wf_size`（每 warp 的 thread 数，对应 CSR_NUMT）、`wg_size`（每 workgroup 的 warp 数，对应 CSR_NUMW）、各类寄存器用量（`sgprUsage`/`vgprUsage`）、`pdsBaseAddr`，以及三个数组 `buffer_base`/`buffer_size`/`buffer_allocsize` 描述这块 kernel 用到的若干内存 buffer。

**config 结构体**——控制仿真行为：

[sim-verilator/ventus_rtlsim.h:L44-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L44-L80) 定义了 `ventus_rtlsim_config_t`，含 `sim_time_max`（最大仿真时间）、`log`（日志文件/控制台两套 sink）、`pmem`（页大小、是否自动分配）、`waveform`（FST 波形的起止时间与层级）、`snapshot`（快照时间间隔与最大数量）、`verilator`（传给 Verilator 运行时的 argc/argv，例如随机种子）。

**step 结果**——每次 `step()` 的返回：

[sim-verilator/ventus_rtlsim.h:L82-L86](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h#L82-L86) 定义了 `ventus_rtlsim_step_result_t`，三个 bool：`error`（致命错误或 RTL `$finish()`）、`time_exceed`（超过 `sim_time_max`）、`idle`（所有 kernel 跑完）。driver 的主循环就是反复 `step()` 直到三者之一为真。

`json2cpp.py` 把硬件参数喂给 driver 的链路：

[sim-verilator/json2cpp.py:L18-L39](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/json2cpp.py#L18-L39) 读取 `parameters.json`，把每个 bool/int/str 项转成 `{ "key", int }` 写进 `rtl_parameters.cpp` 里的 `std::unordered_map<std::string, uint32_t>`。注意它跳过了 `l2cache_*` 等无法转成单个 int 的键（[json2cpp.py:L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/json2cpp.py#L42)），所以 `rtl_parameters` 只含扁平的数值参数（如 `num_sm`、`num_warp`、`num_thread`）。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍构建链路，看清 `libVentusRTL.so` 由哪些源文件组成、`rtl_parameters.cpp` 是怎么来的。

**操作步骤**：

1. 进入 `sim-verilator/`，执行 `make -f verilate.mk verilog`（或顶层 `make verilog`），观察是否生成 `dut.v` 与 `parameters.json`。
2. 执行 `python3 json2cpp.py`（需先有 `parameters.json`），查看生成的 `rtl_parameters.cpp`，确认它是一个 `unordered_map`，并找到 `num_sm`/`num_warp`/`num_thread` 三个键的值。
3. 对照 [verilate.mk:L64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L64) 的 `VLIB_SRC_CXX` 列表，数一下 `libVentusRTL.so` 编进了哪几个 `.cpp`。

**需要观察的现象**：`rtl_parameters.cpp` 里的数值应与 u2-l3 讲的 `parameters.scala` 中的默认规模（`num_sm=2`、`num_warp=8`、`num_thread=32`）一致。

**预期结果**：`rtl_parameters.cpp` 中能看到类似 `{ "num_sm", 2 }`、`{ "num_warp", 8 }`、`{ "num_thread", 32 }` 的条目。若 `parameters.json` 尚未生成，则此步需要先完成 Verilog 生成（依赖 mill 与子模块，耗时较长）——这种情况标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `json2cpp.py` 要跳过 `l2cache_cache` 这类键？
**答案**：因为这些键的值是嵌套对象/字符串，无法转成单个 `uint32_t`；`rtl_parameters` 只承载扁平数值参数，供 `ventus_rtlsim_get_parameter` 按名查询。

**练习 2**：`libVentusRTL.so` 与 mini driver 各自包含 `kernel.cpp`，这两份 `Kernel` 代码是同一份吗？为什么两边都要编进去？
**答案**：是同一份源文件。`.so` 编进 `kernel.cpp` 是因为 `Cta`（在 `.so` 内）持有 `std::shared_ptr<Kernel>`；mini driver 也编进它，是因为 driver 要自己构造 `Kernel` 对象再通过 `add_kernel__delay_data_loading` 传进去。两边都需要 `Kernel` 的完整定义。

---

### 4.2 ventus_rtlsim_t：仿真核心对象与 C API

#### 4.2.1 概念说明

`ventus_rtlsim_t` 是整个仿真框架的「中枢对象」：它同时持有 DUT（硬件）、`PhysicalMemory`（DDR 模型）和 `Cta`（派发器），并把 Verilator 的上下文 `VerilatedContext`、FST 波形句柄 `VerilatedFstC`、快照状态都收拢在自己内部。所有 C API 函数本质上都是「拿到 `ventus_rtlsim_t*` 指针 → 调它的某个方法」。

对外它只暴露 C 接口（`extern "C"` + 不透明指针 `ventus_rtlsim_t*`），内部却是 C++ 类。这种「C API 包 C++ 实现」的设计让 `.so` 能被任意语言（C、Python ctypes、Rust 等）调用，而实现上仍能享受 RAII、`std::shared_ptr`、spdlog 等现代 C++ 设施。

#### 4.2.2 核心流程

一个完整仿真的生命周期是：

```text
ventus_rtlsim_get_default_config()   // 拿推荐默认配置
        │
        ▼
ventus_rtlsim_init(config)           // new ventus_rtlsim_t + constructor
        │   内部：建 logger、建 VerilatedContext、new Vdut、new Cta、
        │         建 PhysicalMemory、打开 FST、fork 初始快照、dut_reset()
        ▼
ventus_rtlsim_add_kernel(...)        // 可多次调用，往 Cta 里塞 kernel
ventus_rtlsim_pmemcpy_h2d(...)       // 把 .data 载入物理内存
        │
        ▼
┌───── ventus_rtlsim_step(sim) ──────┐   // 每次推进 1 个时间单位（半个时钟沿）
│   返回 step_result{error,           │
│       time_exceed, idle}            │
└─────────────────────────────────────┘
        │  循环直到 error/idle/time_exceed
        ▼
ventus_rtlsim_finish(sim, false)     // destructor + delete
        │   若出错且开了快照：rollback 到最旧快照重跑一段
        ▼
   （结束）
```

`step()` 一次调用推进的时间量是 `HALF_CYCLE_TIME = 5`（[ventus_rtlsim_impl.cpp:L25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L25)），同时翻转一次 `dut->clock`。因此一个完整时钟周期（一个上升沿+一个下降沿）需要 **两次 `step()`**，共 \( 2 \times 5 = 10 \) 个时间单位——这正是 git 历史里 `period=10` 的由来。仿真时间与周期数的换算关系为：

\[
\text{周期数} = \frac{\text{仿真时间}}{10}, \qquad \text{step 调用次数} = \frac{\text{仿真时间}}{5}
\]

#### 4.2.3 源码精读

先看 `ventus_rtlsim_t` 这个结构体持有什么：

[sim-verilator/ventus_rtlsim_impl.hpp:L23-L47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.hpp#L23-L47) 定义了它的全部成员：`logger`（spdlog）、`contextp`（Verilator 上下文）、`dut`（`Vdut*` 硬件）、`tfp`（FST 波形）、`cta`（派发器）、`snapshots`（快照状态）、`config`、`step_status`、`pmem`（`unique_ptr<PhysicalMemory>`）。方法有 `constructor`/`step`/`destructor`/`dut_reset`/`waveform_dump`/`snapshot_fork`/`snapshot_rollback`/`snapshot_kill_all`。注意它是用 `extern "C" struct` 声明的，这正是「C ABI 包 C++ 实现」的关键。

C API 的薄封装在 `ventus_rtlsim.cpp`，例如 init 与 step：

[sim-verilator/ventus_rtlsim.cpp:L39-L48](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.cpp#L39-L48) 里，`ventus_rtlsim_init` 就是 `new ventus_rtlsim_t()` + `constructor(config)`；`ventus_rtlsim_step` 就是 `return sim->step()`。所有 API 都是这种一行转调。

`constructor()` 负责把所有零件装配起来：

[sim-verilator/ventus_rtlsim_impl.cpp:L116-L221](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L116-L221)。它依次：拷贝并校验 config（缺日志/波形文件名则补默认）、用 spdlog 建双 sink（文件 + 彩色控制台）的 logger（[L136-L170](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L136-L170)）、建 `VerilatedContext` 并装载 verilator 运行时参数（[L173-L187](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L173-L187)）、`new Vdut()`/`new Cta`/`make_unique<PhysicalMemory>`（[L190-L192](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L190-L192)）、打开 FST 并注册 SIGINT/SIGABRT 处理函数（[L196-L213](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L196-L213)），最后 `snapshot_fork()` 建初始快照 + `dut_reset()` 复位硬件（[L219-L220](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L219-L220)）。

`step()` 是整个框架的心脏，分四段：

[sim-verilator/ventus_rtlsim_impl.cpp:L223-L351](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L223-L351)。

第一段，**先判终止条件**（[L224-L229](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L224-L229)）：若已经 `gotFinish()`/`gotError()` 或超时，直接返回不推进。

第二段，**时钟翻转 + 时间递增**（[L235-L236](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L235-L236)）：

```cpp
contextp->timeInc(HALF_CYCLE_TIME);
dut->clock = !dut->clock;
```

第三段，**按电平施加激励**。当 `dut->clock == 0`（下降沿前）时（[L242-L275](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L242-L275)）：调用 `cta->apply_to_dut(dut)` 把下一个 workgroup 写到 `host_req` 端口；置 `host_rsp_ready=1` 准备收完成信号；并处理物理内存读写——若 `io_mem_rd_en` 则从 `pmem` 读数据填回 `io_mem_rd_data`，若 `io_mem_wr_en` 则把 `io_mem_wr_data` 按字节掩码 `io_mem_wr_mask` 写进 `pmem`。当 `dut->clock == 1`（上升沿前）时（[L281-L300](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L281-L300)）：检测 valid&ready 握手是否成立，成立则 `cta->wg_dispatched()` 记一笔派发、或 `cta->wg_finish(wg_id)` 记一笔完成；并把 `icache_invalidate` 信号送一拍。

第四段，**eval + 波形 + 快照**（[L305-L341](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L305-L341)）：`dut->eval()` 让硬件推进，`waveform_dump()` 按时间区间落 FST，每到 `snapshot.time_interval` 倍数则 `snapshot_fork()` 建新快照。

> 关键理解：**激励在 eval 之前写端口，握手在 eval 之前/之后判断**。负沿（clock=0）写输入激励再 eval，正沿（clock=1）检测上一拍的握手结果——这模拟了真实同步电路里「上升沿采样」的行为。

波形按区间输出：

[sim-verilator/ventus_rtlsim_impl.cpp:L509-L520](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L509-L520)。只有当前时间落在 `[time_begin, time_end)` 区间内才 `tfp->dump(time)`；若是快照子进程则无条件 dump（因为回溯后一定要录波形）。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `step()` 源码，弄清「一次 step 对应多少仿真时间、几个时钟沿」，并验证公式。

**操作步骤**：

1. 打开 [ventus_rtlsim_impl.cpp:L235](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L235)，确认 `HALF_CYCLE_TIME` 与 `dut->clock` 翻转。
2. 在 `sim_main.cpp` 里找到主循环 [sim_main.cpp:L75-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L75-L80)，它设的 `sim_time_max = 200000`（[sim_main.cpp:L35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L35)）。
3. 用公式计算：200000 时间单位最多能跑多少个完整时钟周期？

**需要观察的现象/预期结果**：每两个 `step()` 调用构成一个完整时钟周期。\( 200000 / 10 = 20000 \) 个周期上限。也就是说 `sim_time_max` 的单位是「时间单位」而非「周期数」，换算时记得除以 10。这是一个常被忽略的坑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `step()` 里读内存（`io_mem_rd_en`）在 `clock==0` 分支、而握手判断在 `clock==1` 分支？
**答案**：`clock==0`（下降沿前）是施加输入激励的时机——DUT 给出读请求地址，driver 立即把数据准备好放到 `io_mem_rd_data`，随后 eval。`clock==1`（上升沿前）是检测「上一拍 valid 与 ready 是否同时为高」即握手 fire 的时机，因为同步电路在上升沿采样。

**练习 2**：`ventus_rtlsim_is_idle` 是怎么判断「所有 kernel 跑完」的？
**答案**：见 [ventus_rtlsim.cpp:L51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.cpp#L51)，它转调 `sim->cta->is_idle()`，而 `Cta::is_idle()` 判断的是 kernel 列表是否为空（[cta_sche_wrapper.cpp:L42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L42)）——所有 kernel 都跑完被移出列表，才算 idle。

---

### 4.3 Cta：从 kernel 列表到逐 workgroup 派发

#### 4.3.1 概念说明

`Cta`（CTA scheduler wrapper）是「软件 host」的核心。它维护一个 kernel 队列 `m_kernels`，每个时钟周期决定「现在该把哪个 workgroup 喂给硬件」。它对硬件暴露的是 workgroup 粒度（对应 `host_req` 一次握手），对上层（driver）接收的是 kernel 粒度（`kernel_add`）。

注意区分两个概念：硬件里的 `cta_scheduler_top`（u3 单元）是 RTL，负责把一个 workgroup 拆成 warp 调度到各 SM；这里的 `Cta` 是 C++，负责在软件侧把 kernel 拆成 workgroup 一个个递给硬件的 `host_req` 端口。二者一软一硬、名字相近但职责不同。

#### 4.3.2 核心流程

`Cta` 的运转围绕三个方法：

```text
kernel_add(k)        // driver 调用：把一个 Kernel 塞进队列尾部（尚未激活）
    │
    ▼
apply_to_dut(dut)    // 每个 clock==0 半拍调用：决定并施加本拍要派发的 WG
    │  ① 当前 kernel 还在分派？是→继续取它的下一个 WG
    │  ② 否→尝试激活下一个 kernel（须当前 kernel 已结束）
    │  ③ 把 WG 的全部字段写到 dut->io_host_req_bits_*
    │  ④ 置 dut->io_host_req_valid = true
    ▼
wg_dispatched()      // clock==1 且 host_req 握手 fire 时调用：标记该 WG 已派出
wg_finish(wg_id)     // 收到 host_rsp 完成信号时调用：标记 WG 完成；全完成则移除 kernel
```

关键约束：**目前必须等当前 kernel 完全结束，才能开始派发下一个 kernel**。这是因为尚未实现虚拟内存，kernel 间的资源/地址不能重叠——见 [cta_sche_wrapper.cpp:L51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L51) 的 TODO 注释。

#### 4.3.3 源码精读

`Cta` 的字段与接口：

[sim-verilator/cta_sche_wrapper.hpp:L8-L33](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.hpp#L8-L33)。核心字段 `m_kernels`（kernel 队列）、`m_kernel_idx_dispatching`（当前正在分派的 kernel 下标，初始 -1）、`m_kernel_id_next`/`m_kernel_wgid_base_next`（分配全局 kernel id 与 workgroup id 基址的计数器）。

派发的核心逻辑在 `apply_to_dut`：

[sim-verilator/cta_sche_wrapper.cpp:L44-L96](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L44-L96)。先看 kernel 切换（[L50-L65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L50-L65)）：若当前 kernel 已分派完或不存在，且（队列已到尾 **或** 当前 kernel 尚未结束），则置 `host_req_valid=false` 不分派；否则激活下一个 kernel，调用 `kernel->activate(m_kernel_id_next++, m_kernel_wgid_base_next)`，并把 `wgid_base` 步进该 kernel 的 workgroup 总数（[L62-L63](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L62-L63)）。注意 [L61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L61) 有断言 `m_kernel_wgid_base_next <= 0xEFFFFFFF`，因为当前实现中 workgroup id 不回收，需防溢出。

然后看 workgroup 字段填充（[L68-L93](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L68-L93)）：把 kernel 的 `num_wf`、`wf_size`、各类寄存器总量（注意 `sgpr_size_total = sgpr_per_wf * num_wf`，见 [L82-L83](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L82-L83)）、`start_pc`、`csr_knl`、`pds_baseaddr`（每个 WG 的 pds 基址还要加上 `idx_in_kernel * num_wf * num_thread * pds_per_thread` 偏移，见 [L90-L91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L90-L91)）逐字段写到 `dut->io_host_req_bits_*`。这些字段正好对应 u7-l2 讲的 `host2CTA_data`，最终被硬件的 `CTAinterface` 接收。

完成回收在 `wg_finish`：

[sim-verilator/cta_sche_wrapper.cpp:L98-L119](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L98-L119)。它遍历 kernel 队列，用 `is_wg_belonging(wgid)` 找到这个完成信号属于哪个 kernel（[L102](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L102)），调用 `kernel->wg_finish(wgid)` 标记单个 WG 完成；若整个 kernel 全部完成（`is_finished()`），则 `deactivate()`、从队列移除、并把 `m_kernel_idx_dispatching` 减一（可能减到 -1，[L109-L114](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L109-L114)）。

#### 4.3.4 代码实践

**实践目标**：追踪一个 workgroup 从 `apply_to_dut` 写端口到 `wg_dispatched` 标记、再到 `wg_finish` 回收的完整往返。

**操作步骤**：

1. 在 [ventus_rtlsim_impl.cpp:L283-L292](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L283-L292) 找到派发握手检测：`host_req_valid && host_req_ready` 成立时调用 `cta->wg_dispatched()`，并打一条 debug 日志。
2. 在 [ventus_rtlsim_impl.cpp:L294-L297](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L294-L297) 找到完成握手检测：`host_rsp_valid && host_rsp_ready` 成立时取出 `wg_id` 调 `cta->wg_finish(wg_id)`。
3. 跟踪 `wg_id` 的来源：派发时它是 `io_host_req_bits_host_wg_id`（去程），完成时它是 `io_host_rsp_bits_inflight_wg_buffer_host_wf_done_wg_id`（回程）。这就是 driver 与硬件之间靠 `wg_id` 配对的机制。

**需要观察的现象**：打开波形（`--waveform`）或在日志里看 `block?? dispatched to GPU` 与 `block?? finished` 两条 debug 记录的先后与 `wg_id` 对应关系。

**预期结果**：每个 workgroup 先出现一条 dispatched 日志（去程握手），运行一段时间后出现一条 finished 日志（回程握手），两者 `wg_id` 一致。运行较大 kernel 时会看到多组 dispatched/finished 交错。具体日志条数取决于 kernel_size，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Cta` 要用 `m_kernel_wgid_base_next` 给每个 kernel 分配一段连续的 workgroup id 区间，而不是每个 kernel 都从 0 开始编号？
**答案**：因为硬件回传的完成信号只携带全局 `wg_id`（见 `host_rsp_bits_..._wg_id`），driver 必须能凭这个全局 id 反查到「属于哪个 kernel 的第几个 WG」。给每个 kernel 分配不重叠的 id 区间（`wgid_base + idx`），`wg_finish` 才能用 `is_wg_belonging` 定位。

**练习 2**：`apply_to_dut` 末尾有一句 `assert(0)`（[cta_sche_wrapper.cpp:L95](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/cta_sche_wrapper.cpp#L95)），在什么情况下会触发？
**答案**：理论上不会触发。它表示「既没能切到下一个 kernel、当前 kernel 又不在分派态」这一不应出现的状态——这是防御性编程，一旦命中说明 `Cta` 内部状态机被破坏。

---

### 4.4 Kernel：元数据解析与 workgroup 状态机

#### 4.4.1 概念说明

`Kernel` 是单个 kernel 的元数据容器。它做三件事：

1. **解析 `.metadata` 文件**：把 hex 文本读成一组 `uint64_t`，再按固定顺序赋给 metadata 结构体的各字段。
2. **维护 workgroup 状态机**：每个 workgroup 有 WAITING/RUNNING/FINISHED 三态，用 `m_wg_status` 向量记录，派发时 WAITING→RUNNING，完成时 RUNNING→FINISHED。
3. **三维 workgroup 索引遍历**：用 `m_next_wg` 这个 `dim3_t` 按 x→y→z 的顺序逐一产出要派发的 workgroup。

`.metadata` 文件的格式在 u1-l4 已介绍过：每个 `uint64_t` 占两行（低字在前，小端序），整文件是一串这样的 64 位整数。`Kernel` 用一个 `assignMetadata` 函数按固定字段顺序把它们「对号入座」。

#### 4.4.2 核心流程

```text
构造 Kernel(name, metafile, datafile)
    │  readHexFile(metafile) → vector<uint64_t>
    │  assignMetadata(...)   → 填 m_metadata 各字段、m_grid_dim、m_wg_status
    ▼
activate(kernel_id, wgid_base)   // 被 Cta 调用：赋 id、调 load_data_callback 载入 .data
    │
    ▼
get_next_wg_*() / wg_dispatched()  // Cta 每派发一个 WG：读下一个 WG 信息、状态 WAITING→RUNNING
    │                                  m_next_wg 按 increment_x_then_y_then_z 步进
    ▼
wg_finish(wgid)                   // 收到完成：RUNNING→FINISHED
    │
    ▼
is_finished()?                    // 所有 WG 都 FINISHED？
    └─是→ deactivate()（调 finish_callback）→ Cta 把它移出队列
```

workgroup 在 kernel 内的一维索引按 x 优先展开：

\[
\text{idx} = x + \text{grid}\_x \cdot y + \text{grid}\_x \cdot \text{grid}\_y \cdot z
\]

#### 4.4.3 源码精读

状态枚举与三维步进：

[sim-verilator/kernel.cpp:L11-L22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L11-L22) 的 `increment_x_then_y_then_z`：先 `x++`，`x` 越界则归零并 `y++`，`y` 越界则归零并 `z++`。这正是三维 NDRange 的标准遍历顺序。状态三态定义在 [kernel.hpp:L91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.hpp#L91)。

hex 文件解析：

[sim-verilator/kernel.cpp:L94-L141](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L94-L141) 的 `readHexFile`。它逐字符读，遇到换行标记 `leftside`（表示该 64 位数的高位部分写在下一行——这是 `.metadata`「每个 uint64 占两行」的实现细节），每凑满 64 位（16 个 hex 字符）push 一个数。

字段对号入座：

[sim-verilator/kernel.cpp:L150-L189](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L150-L189) 的 `assignMetadata`。顺序是：`startaddr`、`kernel_id`、`kernel_size[3]`、`wf_size`、`wg_size`、`metaDataBaseAddr`、`ldsSize`、`pdsSize`、`sgprUsage`、`vgprUsage`、`pdsBaseAddr`、`num_buffer`，然后是三个长度均为 `num_buffer` 的数组 `buffer_base`/`buffer_size`/`buffer_allocsize`。**这个顺序就是 `.metadata` 文件里数值的物理顺序**——要手工核对某测例的某字段，就按这个顺序数。

激活与状态查询：

[sim-verilator/kernel.cpp:L191-L206](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L191-L206) 的 `activate`：赋 `kernel_id`/`wgid_base`，若有 `load_data_callback` 则调用它（这正是「延迟载入数据」的钩子），最后置 `m_is_activated=true`。[L215-L238](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L215-L238) 定义了几个状态查询：`is_finished`（全部 FINISHED）、`is_running`（有任一 RUNNING）、`is_dispatching`（已激活且还有 WG 没派完）；`wg_finish` 把对应 idx 的状态从 RUNNING 改 FINISHED。

#### 4.4.4 代码实践

**实践目标**：手工解析一个真实 `.metadata` 文件，验证 `assignMetadata` 的字段顺序。

**操作步骤**：

1. 打开 [sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/testcase/vecadd/vecadd_32b8w8t.metadata)（每两行一个 `uint64`，低字在前）。
2. 按 `assignMetadata` 顺序数：第 1 个值 `0x80000000` 是 `startaddr`；第 3 个值 `0x20`=32 是 `kernel_size[0]`（即 32 个 workgroup）；第 6 个值 `0x8` 是 `wf_size`（每 warp 8 线程）；第 7 个值 `0x4` 是 `wg_size`（每 workgroup 4 个 warp）；第 14 个值 `0x7` 是 `num_buffer`=7。
3. 接着 7 个值是 `buffer_base`：`0x90000000`、`0x90001000`、`0x90002000`、`0x90003000`、`0x80000000`、`0x90004000`、`0x90404000`。其中 `0x80000000` 与 `startaddr` 一致，是给硬件用的 metadata buffer。

**需要观察的现象**：手工数出来的字段值应当与 `vecadd`（C=A+B，32 个 block）的语义自洽——例如 workgroup 总数 = `kernel_size[0]*kernel_size[1]*kernel_size[2]` = 32*1*1 = 32。

**预期结果**：`buffer_base[2]=0x90002000` 很可能是存放加法结果 C 的输出 buffer（待本地验证：仿真后用 `--dump-mem 0x90002000,0x90002040` 看该区段是否为 A+B 的和）。这个练习同时串起了本讲与 u1-l4 的 `--dump-mem` 用法。

#### 4.4.5 小练习与答案

**练习 1**：`.metadata` 里 `wf_size`/`wg_size` 与硬件参数 `num_thread`/`num_warp` 是什么关系？
**答案**：`wf_size` 对应每 warp 的线程数（硬件 `num_thread`，即 CSR_NUMT），`wg_size` 对应每 workgroup 的 warp数（CSR_NUMW）。测例文件名 `vecadd_32b8w8t` 里的 `8w8t` 即 `wg_size=8`、`wf_size=8`。它们必须与硬件 `parameters.scala` 的规模匹配，否则硬件资源不够装。

**练习 2**：`Kernel` 有两个构造函数（[L55](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L55) 与 [L71](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L71)），分别给谁用？
**答案**：前者从 `.metadata`/`.data` 文件构造，给 mini driver（cmdarg 路径）用；后者直接接收一个 `metadata_t*` 结构体指针，给完整工具链（ventus-env）用——后者不需要 hex 文件，由上层程序直接填好 metadata 传入。

---

### 4.5 PhysicalMemory：分页式物理内存

#### 4.5.1 概念说明

`PhysicalMemory` 是 DDR 内存的软件模型。最朴素的实现是「分配一大块 `uint8_t` 数组模拟整个地址空间」，但 GPU 的地址空间很大（几十 GB），实际仿真只用到零星几段，全分配会撑爆内存。于是这里采用**按页分配的稀疏字典**：用一个 `std::map<paddr_t, uint8_t*>` 只保存真正被用到的页，每页大小默认 4096 字节。

这模拟的是「真实硬件内存」的行为：真实 DDR 上每个地址都能读写，未初始化的部分读出来是 0（或随机）。`auto_alloc` 选项控制是否在首次访问未分配页时自动分配——开启后行为更接近真实硬件。

#### 4.5.2 核心流程

```text
page_alloc(base)    // 显式分配一页（base 按 pagesize 对齐），m_map[base] = new uint8_t[pagesize]
    │
    ▼
write(paddr, data, mask, size)   // DUT 写内存（带字节掩码）
    │  ① 跨页？递归拆成「本页剩余 + 下一页」两段
    │  ② 本页未分配？auto_alloc 则自动分配，否则 critical 报错返回 false
    │  ③ 按掩码逐字节写入 m_map[page_base] + offset
    ▼
read(paddr, data, size)          // DUT 读内存
    │  ① 跨页？递归
    │  ② 本页未分配？报错并填 0（读未分配页得 0）
    │  ③ memcpy 出来
```

页基地址的计算：

\[
\text{page\_base} = \text{paddr} - (\text{paddr} \bmod \text{pagesize})
\]

#### 4.5.3 源码精读

类的字段：

[sim-verilator/physical_mem.hpp:L10-L32](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.hpp#L10-L32)。核心是 `std::map<paddr_t, uint8_t*> m_map`——键是页基地址，值是该页的字节数组。`m_auto_alloc`/`m_pagesize` 是配置。

带掩码的写：

[sim-verilator/physical_mem.cpp:L30-L55](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L30-L55)。先处理跨页（[L34-L39](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L34-L39)）：若 `[paddr, paddr+size)` 跨过页边界，把写拆成本页尾段 + 递归写下一页。再看本页是否已分配（[L40-L47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L40-L47)）：未分配时 `auto_alloc` 自动建页，否则 `critical` 报错。最后按掩码逐字节写入（[L48-L53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L48-L53)）。这个带掩码版本正是 `step()` 里处理 DUT `io_mem_wr_*` 时调用的——见 [ventus_rtlsim_impl.cpp:L260-L274](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L260-L274)，driver 把 `io_mem_wr_mask`（每个 bit 对应一字节）展开成 `bool[]` 再传入。

读：

[sim-verilator/physical_mem.cpp:L80-L98](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L80-L98)。同样处理跨页；区别在于读未分配页时不是报错终止，而是填 0 返回（[L90-L94](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L90-L94)），因为「读一块从没写过的内存得 0」是合理的硬件行为。

析构：

[sim-verilator/physical_mem.cpp:L100-L107](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/physical_mem.cpp#L100-L107)。若非 auto_alloc 且仍有页未释放，打 warn（内存泄漏提醒），然后逐页 `delete[]`。

#### 4.5.4 代码实践

**实践目标**：看清 DUT 的内存读写如何落到 `PhysicalMemory`，并理解 `auto_alloc` 的作用。

**操作步骤**：

1. 在 `step()` 的 `clock==0` 分支找到内存读 [ventus_rtlsim_impl.cpp:L255-L258](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L255-L258) 与内存写 [L260-L274](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L260-L274)。注意读用 `pmem->read`，写用带掩码的 `pmem->write`。
2. 对比两种 driver 配置：mini driver 设了 `pmem.auto_alloc = true`（[sim_main.cpp:L36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L36)），而 `ventus_rtlsim_get_default_config` 默认是 `auto_alloc = 0`（[ventus_rtlsim.cpp:L19](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.cpp#L19)）。
3. 思考：若用默认配置（不自动分配）跑 vecadd，会怎样？

**需要观察的现象/预期结果**：不开 `auto_alloc` 时，driver 必须在 `add_kernel` 之前用 `ventus_rtlsim_pmem_page_alloc` 或 `ventus_rtlsim_pmemcpy_h2d`（其内部会触发分配）把 kernel 用到的页先分配好，否则 DUT 一旦写未分配页就会 `critical` 报错、读未分配页得全 0。mini driver 靠 `auto_alloc=true` 规避了显式分配的麻烦。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `write` 跨页用递归，而不是循环？
**答案**：递归写法把「本页尾段 + 后续段」自然拆分，后续段再次走同一套逻辑（可能再跨页），代码简洁。由于一次写最多跨少数几页，递归深度很小，不会有栈溢出风险。

**练习 2**：读未分配页返回 0、写未分配页（非 auto_alloc）报错，这两种处理为什么不一致？
**答案**：读未定义内存得 0 是可接受的硬件行为（仿真不必为此中断），所以 `read` 填 0 继续；但写未分配页通常意味着 driver 没有正确载入数据或地址算错，是真实 bug 的信号，所以 `write` 用 `critical` 显著告警，便于及早发现。

---

## 5. 综合实践

**综合任务**：在 `sim_main.cpp` 的基础上，理解并「口述」一个调用 `libVentusRTL.so` C API 的最小自定义 driver，把本讲四个最小模块（`ventus_rtlsim_t`/`Cta`/`Kernel`/`PhysicalMemory`）串成一条完整的调用链。然后对照真实 `sim_main.cpp` 检查你的理解。

下面是这条调用链应当包含的步骤（用 C API 表述）：

```text
1. ventus_rtlsim_get_default_config(&config);   // 拿默认配置
   // 按需修改 config：sim_time_max、pmem.auto_alloc、waveform 等
2. ventus_rtlsim_init(&config);                  // 装配 ventus_rtlsim_t（含 DUT/Cta/PhysicalMemory）

3. // 准备一个 kernel 的 metadata（可手填，或让 mini driver 的 kernel_load_data_callback 读 .data）
4. ventus_rtlsim_pmemcpy_h2d(sim, buf_base, data, size);  // 把 .data 载入物理内存
5. ventus_rtlsim_add_kernel__delay_data_loading(
        sim, &metadata, load_cb, finish_cb);     // 塞进 Cta 队列（此时还未激活）

6. while (1) {                                   // 主循环
       const ventus_rtlsim_step_result_t* r = ventus_rtlsim_step(sim);
       if (r->error || r->idle || r->time_exceed) break;
   }
   // 内部每个 step：Cta.apply_to_dut 派发 WG → DUT eval → PhysicalMemory 读写 → 握手检测

7. ventus_rtlsim_pmemcpy_d2h(sim, out, out_base, size);  // 取回结果内存
8. ventus_rtlsim_finish(sim, false);             // 析构（出错则快照回溯）
```

**对照真实代码验证**：

- 步骤 1-2：见 [sim_main.cpp:L31-L61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L31-L61)。注意真实 driver 用了 `add_kernel__delay_data_loading`（[L67](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L67)），把载入 `.data` 的动作延迟到 kernel 被 `activate` 时（即 `load_data_callback`，定义在 [sim_main.cpp:L109-L144](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L109-L144)）。
- 步骤 6：见 [sim_main.cpp:L75-L80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L75-L80)。
- 步骤 7：见 [sim_main.cpp:L90-L103](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L90-L103)，按 `--dump-mem` 给的范围每 4 字节打印一行。
- 步骤 8：见 [sim_main.cpp:L104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/sim_main.cpp#L104)。

**进阶（可选）**：仿照 `kernel_load_data_callback`，写一个自己的 `finish_callback`，在 kernel 跑完时打印一行 `kernel ?? done`。把它传给 `ventus_rtlsim_add_kernel`，重新跑 vecadd，观察回调触发时机（应在 `deactivate` 时，即 [kernel.cpp:L211-L212](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/kernel.cpp#L211-L212)）。这一步需要修改 mini driver 源码并重新 `make`，属于本讲的可选动手任务。

## 6. 本讲小结

- `sim-verilator/` 分三块：`libVentusRTL.so`（建模硬件+物理内存+内核拆分）、mini driver（示例调用者）、`testcase/`。`libVentusRTL.so` 用 C API 包 C++ 实现，便于任何语言调用。
- `ventus_rtlsim_t` 是中枢，持有 `Vdut`（硬件）、`PhysicalMemory`（DDR）、`Cta`（派发器）三大件；所有 `ventus_rtlsim_*` C API 都是对其方法的薄封装。
- `step()` 是四段式主循环：判终止 → 翻转 clock 并 `timeInc(5)` → 按电平施加激励（负沿 `cta->apply_to_dut` + 内存读写，正沿检测 `host_req`/`host_rsp` 握手）→ `eval` + 波形 + 快照。一个完整时钟周期 = 两次 step = 10 个时间单位。
- `Cta` 维护 kernel 队列，每拍决定派发哪个 workgroup、把字段写到 `host_req_bits_*`；靠全局不重叠的 `wg_id` 区间让完成信号能反查回所属 kernel。
- `Kernel` 解析 `.metadata`（每 `uint64` 两行、按 `assignMetadata` 固定顺序对号入座），用 `m_wg_status` 维护每个 workgroup 的 WAITING/RUNNING/FINISHED 三态。
- `PhysicalMemory` 用 `std::map<页基址, 页指针>` 的稀疏字典模拟 DDR，按需分页、支持跨页读写与字节掩码写；`auto_alloc` 决定首次访问未分配页时是自动建页还是报错。

## 7. 下一步学习建议

- **协同仿真**：本讲的 `step()` 里有一段被 `#ifdef ENABLE_GVM` 包起来的代码（[ventus_rtlsim_impl.cpp:L343-L348](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L343-L348)），它正是下一讲 u7-l4《GVM 协同仿真与对拍》的入口——RTL 与 SPIKE 参考模型逐拍比对寄存器结果。
- **快照机制深挖**：本讲只点到 `snapshot_fork`/`snapshot_rollback`（基于 `fork()` 子进程 + `SIGRTMIN` 信号唤醒），若对调试 RTL 死锁/挂死感兴趣，可精读 [ventus_rtlsim_impl.cpp:L410-L497](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L410-L497)，并结合 Verilator 的 process-level clone 文档理解。
- **完整工具链**：mini driver 的 `.metadata`/`.data` 路径仅作兼容保留，官方推荐用 ventus-env 完整工具链（POCL + driver）驱动 `libVentusRTL.so`。学完本讲后，建议阅读 ventus-env 仓库，看它如何直接构造 `ventus_kernel_metadata_t` 调用同一套 C API。
- **硬件侧对应**：`Cta.apply_to_dut` 写的 `host_req_bits_*` 字段，对应 u7-l2 的 `AXI4Lite2CTA` 寄存器映射与 u3 单元的 `CTAinterface`；可回看这两讲，把「软件 driver 写什么」与「硬件 host 怎么收」对齐。
