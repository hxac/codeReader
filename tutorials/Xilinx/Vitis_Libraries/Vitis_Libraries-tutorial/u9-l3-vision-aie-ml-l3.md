# AIE-ML 视觉函数与 L3 流水线

## 1. 本讲目标

本讲承接 u9-l2（PL 视觉内核与 L2 示例流程）和 u6-l2（dsp 的 AIE 内核家族与 x86sim/aiesim 双档仿真），把 vision 库的另外两条主线讲清楚：

1. **AIE-ML 视觉函数**：vision 库跑在 Versal AI Engine 的「机器学习增强」变体（AIE-ML）上的内核，它们放在哪里、怎么组织、如何在 VEK280 上仿真与构建。
2. **L3 多内核流水线**：把多个视觉内核缝合成一条端到端图像处理流水线的思想与真实示例。

学完后你应该能够：

- 说出 vision 库中 AIE-ML 内核的目录位置（`L1/include/aie-ml`）与类别划分，并解释它们与 PL（HLS）内核、与 dsp 库 AIE 内核的异同。
- 描述 AIE-ML 测试的四档仿真/构建流程：`x86sim`、`aiesim`、`hw_emu`、`hw`，以及它们由谁驱动（图的 `main()` 还是主机 `xrt::graph`）。
- 解释「智能分块」（tiler / stitcher）PL 内核如何架起 DDR 与 AIE 阵列之间的数据桥梁。
- 读懂一个 L3 流水线示例，列出它串联了哪些视觉内核，并说明 L3 相对单内核 L2 的价值。

## 2. 前置知识

在进入本讲前，确保你理解下面几个概念（前序讲义已建立）：

- **PL 与 AIE 两条加速路线**（u1-l3）：PL（可编程逻辑，即 FPGA）走 HLS→RTL，AIE（AI Engine）走 ADF 数据流图。本讲的主角 **AIE-ML** 是 AIE 的「机器学习增强」变体，硬件上是 Versal AI Edge 系列（典型板卡 **VEK280**），每个 AIE-ML tile 的向量/矩阵算力强于第一代 AIE（VCK190）。
- **L1/L2/L3 三层抽象**（u1-l3、u5-l3）：L1 是算法原语（头件），L2 把原语包成可上板内核 + 主机程序产出 `xclbin`，L3 把多个内核串成端到端流水线应用。
- **ADF 图与 window/stream**（u6-l2）：AIE 用 `adf::graph` 描述计算图，内核之间用 `window`（面向缓冲的批量数据）或 `stream`（逐元素流）交换数据；图有 `init()/run(n)/wait()/end()` 生命周期。
- **PL↔AIE 边界**（u5-l2）：AIE 阵列只认无地址的 AXI Stream，DDR 只能按地址访问，二者之间必须靠 PL 侧搬运内核（mm2s/s2mm）填平协议鸿沟。
- **原生 XRT 主机 API**（u4-l2、u13-1/l2）：主机用 `xrt::device/kernel/bo` 控制内核，AIE 图另由 `xrt::graph` 的 `reset()/run()/wait()/end()` 控制。

一个容易混的点：本仓库中 vision 的 L3 示例**目前全部是 PL（HLS）流水线**，AIE-ML 函数则位于 L1/L2。本讲会把这两件事都讲透，并明确区分。

## 3. 本讲源码地图

| 文件 / 目录 | 角色 |
| --- | --- |
| `vision/README.md` | vision 库总说明，明确「AIE-ML 函数在 VEK280 上验证」与 AIE-ML 开发流程四阶段。 |
| `vision/L3/README.md` | L3 目录说明：L3 = 多内核缝合的应用流水线。 |
| `vision/L1/include/aie-ml/` | AIE-ML 视觉原语头件全集（按 `imgproc` / `dnn` 分）。 |
| `vision/L1/include/aie/common/` | AIE 公共工具：智能分块、数据搬运器（`xfcvDataMovers.h`）、设备 traits。 |
| `vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp` | 典型 AIE-ML 内核：Resize 类，含定点缩放因子计算与 `aie_api` 向量运算。 |
| `vision/L2/tests/aie-ml/resize/.../8bit_aie_224x224_pl_1ch/` | 一个完整 AIE-ML 测试用例：`graph.h`/`graph.cpp`/`kernels.h`/`xf_resize.cc` + `Makefile` + `description.json` + `system.cfg` + `host.cpp`。 |
| `vision/L3/examples/colordetect/` | L3 PL 流水线示例：单 `extern "C"` 函数用 `DATAFLOW` 串联 7 个视觉算子。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**① AIE-ML 内核目录与图封装**、**② 仿真与构建流程（x86/aie/hw_emu）**、**③ L3 多内核图像处理流水线**。

---

### 4.1 AIE-ML 内核目录与图封装

#### 4.1.1 概念说明

vision 库的顶层 README 把开发范式分成三类——PL（HLS/RTL）、AIE、PL+AIE：

[vision/README.md:L57-L61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L57-L61) —— 说明所有单元级内核放在 `L1/include`，而 AI Engine 内核放在 `L1/include/aie`。

本讲关注的 AIE-ML 内核位于一个更细的子目录 `L1/include/aie-ml`，里面再按功能域分成两块：

- `imgproc/`：约 45 个图像处理函数，覆盖色彩空间转换（`xf_rgb2YCrCb`、`xf_hsv2rgb`、`xf_rgba2gray` 等）、多种缩放（`xf_resize_aie2`、`xf_resize_bicubic`、`xf_nv12resize_aieml`、`xf_polyphase_resize`）、滤波（`xf_filter2d_16b_aieml`、`xf_yuy2_filter2d_aieml`）、去马赛克（`xf_demosaicing_aie2`）、阈值/归一化/掩码、非极大值抑制（`xf_nms`）、转置（`xf_transpose_1c`）等。
- `dnn/`：面向深度学习预处理的函数（如 `xf_resize_norm`，把缩放与归一化合并成一步）。

公共支撑放在 `L1/include/aie/common/`：智能分块（`smartTile.hpp`、`smartTilerStitcher.hpp`）、数据搬运器（`xfcvDataMovers.h` 及其 `_plio`/`_gmio` 变体）、设备能力 traits（`xf_aie_device_traits.hpp`）。

> 与 dsp 库 AIE 内核（u6-l2）对比：dsp 用「kernel + traits + utils」三件式、且多为 `stream` 接口的复数信号处理；vision 的 AIE-ML 内核则是**一个 C++ 类 + `runImpl(adf::input_buffer/adf::output_buffer, ...)` 的 `window` 接口**，靠 `aie_api` 的向量 intrinsic 一次处理一整块 tile。二者都遵循「类定义放头件、不含图级细节」的约定，但 vision 更强调「逐 tile 的 2D 图像块」这一抽象。

AIE-ML 与第一代 AIE 的硬件差异（向量位宽、矩阵单元）决定了这些函数**只验证于 VEK280**：

[vision/README.md:L7-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L7) —— 明确「AIE-ML functions are verified on VEK280 board」。而 PL 内核则在 zcu102/zcu104/vck190/U50/U200 上验证。这条信息正是本讲实践任务要确认的事实。

#### 4.1.2 核心流程

一个 AIE-ML 视觉函数从「原语」变成「可仿真系统」，要经过三层封装：

```text
L1/include/aie-ml/xxx.hpp          ← 算法类：Resize::runImpl(input_buf, output_buf, 参数)
        │  （被测试用例包装）
L2/tests/aie-ml/<func>/.../
   ├── kernels.h        ← Runner 类：run() 调原语 + REGISTER_FUNCTION 注册
   ├── xf_*.cc          ← Runner::run 的实现（include 原语头件）
   ├── graph.h          ← adf::graph：实例化 kernel、连 PLIO、设 dimensions、指定 source
   ├── graph.cpp        ← 仿真用 main()：init/update/run/wait/end
   ├── host.cpp         ← 上板/hw_emu 用主机：xrt::graph + xfcvDataMovers
   ├── system.cfg       ← 链接拓扑：Tiler/stitcher PL 内核 ↔ ai_engine 端口
   ├── Makefile         ← 四档 TARGET 的构建/运行规则
   └── description.json ← CI/工具链元数据（flow、平台白名单、aiecontainers）
```

数据在系统里这样流动（这是 AIE-ML 视觉用例的标配「夹心」结构）：

```text
DDR 图像 ── host2aie ──▶ [Tiler_top (PL)] ──切分成 tiles──▶ ai_engine_0 (AIE-ML 图)
                                                              │ 逐 tile 处理
                                                              ▼
                        [stitcher_top (PL)] ◀──拼回整图── aie2host ──▶ DDR 输出
```

- **Tiler**（PL 内核，来自 `L1/lib/hw/tiler.xo`）把整幅图像按固定 tile 尺寸切成若干 2D 块，每块打平成一个 `window` 喂给 AIE。
- **AIE-ML 内核**对每个 tile 独立处理（如缩放），窗口大小由 `adf::dimensions(...)` 在编译期钉死。
- **Stitcher**（PL 内核，`L1/lib/hw/stitcher.xo`）把处理后的 tiles 按元数据拼回完整图像写回 DDR。

这与 u5-l2 讲的 mm2s/s2mm 桥接思想一致，区别在于 vision 用的是更高层的 `xF::xfcvDataMovers<TILER/STITCHER>` 抽象，且自带「分块—拼接」语义。

#### 4.1.3 源码精读

**① L1 原语类**——以 Resize 为例。`xf_resize_aie2.hpp` 定义类 `Resize`，核心是两个 `runImpl` 重载：一个接受裸指针（tile 内部逐行处理用），一个接受 ADF `window` 缓冲（图级入口）：

[vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp:L32-L32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp#L32-L32) —— 声明 `class Resize`。

[vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp:L107-L120](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp#L107-L120) —— 两个 `runImpl` 重载：`L107-L116` 是基于裸指针的 tile 级实现，`L118-L120` 是基于 `adf::input_buffer/adf::output_buffer` 的图级入口（window 接口）。

注意类里大量用 `::aie::vector<int32_t,8>`、`::aie::accum<acc32,32>` 等 `aie_api` 类型（见 `init_pos_wt_accum`，L50-L66），这是 AIE-ML 专属的向量 intrinsic——它们不会被图级编译器解析，而要等核级编译阶段才生成机器码。缩放因子用 16 位定点表示，固定换算函数在 `compute_scalefactor<16>` 中（`xf_aie_sw_utils.hpp`）。

**② Runner 类（kernels.h）**——把原语类包成图可识别的内核：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/kernels.h:L26-L33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/kernels.h#L26-L33) —— `class ResizeRunner` 持有 `run(input_buffer, output_buffer, scale_x, scale_y)`，并用 `static void registerKernelClass() { REGISTER_FUNCTION(ResizeRunner::run); }` 把 `run` 注册为图的入口。`REGISTER_FUNCTION` 是 ADF 框架宏，告诉图编译器「这个函数是一个可调度的内核端口」。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/xf_resize.cc:L20-L26](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/xf_resize.cc#L20-L26) —— `ResizeRunner::run` 的实现：构造 `xf::cv::aie::Resize` 并调用 `runImpl`，把 tile 的图像高度、通道数等编译期常量传入。`.cc` 单独成文件，正是因为它含会被核级编译处理的实现细节。

**③ ADF 图（graph.h）**——声明拓扑：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/graph.h:L35-L64](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/graph.h#L35-L64) —— `class resizeGraph : public adf::graph`。要点：
- `L40-L43`：声明一个 `input_plio in1`、一个 `output_plio out1`（仿真下分别绑到 `data/input.txt`、`data/output.txt`），外加两个 `port<input> scalex/scaley`（运行时参数）。
- `L46`：`k = kernel::create_object<ResizeRunner>()` 实例化内核对象（对比 dsp 多用 `kernel::create` 函数式）。
- `L48-L49`：`input_plio::create("DataIn0", adf::plio_128_bits, "data/input.txt")` —— PLIO 端口名 `DataIn0`/`DataOut0` 必须与 `system.cfg` 里的 `ai_engine_0.DataIn0` 对齐，位宽 128 bit。
- `L52-L55`：`connect<>(in1.out[0], k.in[0])` 接数据流；`connect<parameter>(scalex, async(k.in[1]))` 用 `async` 把 `scalex/scaley` 绑到内核的 `in[1]/in[2]` 参数端口。
- `L57-L58`：`adf::dimensions(k.in[0]) = {TILE_WINDOW_SIZE_IN}` 声明每个 window 的字节数（编译期常量）。
- `L61`：`source(k) = "xf_resize.cc"` 指定核级源文件。

**④ system.cfg（链接拓扑）**——描述 PL↔AIE 连接：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/system.cfg:L1-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/system.cfg#L1-L7) —— `nk=Tiler_top:1` 与 `nk=stitcher_top:1` 各实例化一个 PL 搬运器；`stream_connect=Tiler_top_1.OutputStream:ai_engine_0.DataIn0` 把 Tiler 的输出流接到 AIE 图的 `DataIn0`（正是上面 PLIO 端口名），`ai_engine_0.DataOut0:stitcher_top_1.InputStream` 把 AIE 输出接回 stitcher。这条 `system.cfg` 复刻了 4.1.2 中的「夹心」数据流。

> 小结：vision AIE-ML 的封装链是 **原语类（window 接口）→ Runner 类（REGISTER_FUNCTION）→ adf::graph（PLIO + async 参数）→ system.cfg（Tiler/stitcher 桥接）**。`L50-L66` 的向量运算留给核级编译，所以实现必须放 `.cc` 而非图头件——这点和 dsp 的「`.cpp` 含 intrinsic」拆分理由相同（u6-l2）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立「原语类 → Runner → 图 → system.cfg」四层映射的肌肉记忆。

**步骤**：

1. 打开 `vision/L1/include/aie-ml/imgproc/xf_resize_aie2.hpp`，找到 `class Resize` 的两个 `runImpl` 重载，确认它们的形参类型。
2. 打开 `vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/kernels.h`，确认 `ResizeRunner::run` 的形参顺序与原语 `runImpl` 的 `window` 重载一致。
3. 打开同目录 `graph.h`，确认 `connect<>(in1.out[0], k.in[0])` 中 `k.in[0]` 对应数据 window，`async(k.in[1])`、`async(k.in[2])` 对应 `scale_x/scale_y`。
4. 打开 `system.cfg`，确认 `ai_engine_0.DataIn0` / `DataOut0` 与 `graph.h` 里 `input_plio::create("DataIn0", ...)` 的名字一字不差。

**需要观察的现象**：四层之间靠「端口名 + 形参下标」对齐，任何一处名字写错，链接阶段就会报「unconnected port」或运行时拿不到数据。

**预期结果**：你能画出一条从 `Resize::runImpl` 到 `stitcher_top_1` 的完整调用/连接链。

**待本地验证**：若你有 VEK280 环境，可进一步用 `aiesim` 跑（见 4.2），在 `Work/` 下看到生成的图连接报告。

#### 4.1.5 小练习与答案

**练习 1**：`graph.h` 里 `scalex`/`scaley` 用 `connect<parameter>(..., async(...))`，而 `in1` 用普通 `connect<>`。两者有何区别？

**答案**：`in1` 是**数据流**（每运行一次就消费一个 window 的图像数据），用普通 `connect`；`scalex/scaley` 是**运行时参数**（一次配置、全程只读的缩放因子），用 `connect<parameter>` + `async` 表示它走异步参数通道、不占用数据流带宽。

**练习 2**：为什么 `ResizeRunner::run` 的实现放在 `xf_resize.cc` 而不是直接写在 `kernels.h` 里？

**答案**：因为 `xf_resize.cc` 会 `#include "imgproc/xf_resize_aie2.hpp"`，后者含 `aie_api` 向量 intrinsic，这些只能在**核级编译**阶段被处理（u6-l2 讲过的两阶段编译）。把实现放 `.cc` 并由 `source(k) = "xf_resize.cc"` 显式喂给核级编译器，才能正确生成 AIE 机器码；`kernels.h` 只保留无 intrinsic 的类声明与注册宏。

---

### 4.2 仿真与构建流程：x86sim / aiesim / hw_emu / hw

#### 4.2.1 概念说明

AIE-ML 用例的「仿真阶梯」比纯 PL 多出两档。README 把 AIE-ML 的开发流程列为四个阶段：

[vision/README.md:L121-L127](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L121-L127) —— 明确 AIE-ML 开发（`L1/include/aie-ml` + `L2/tests/aie-ml`）支持：AIE simulation、X86 simulation、Hardware emulation、Hardware build and run。

对应到 Makefile 的小写 `TARGET`，共四档（外加一个被显式禁用的 `sw_emu`）：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile:L21-L21](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile#L21-L21) —— 用法提示：`make all TARGET=<hw_emu/hw/x86sim/aiesim> PLATFORM=<FPGA platform>`。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile:L63-L65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile#L63-L65) —— 显式报错：自 2025.1 起 `sw_emu` 不再支持（与 u5-l1 的小写 target 退役趋势一致）。

四档保真度与代价由低到高：

| TARGET | 驱动者 | 保真度 | 典型用途 | 代价 |
| --- | --- | --- | --- | --- |
| `x86sim` | 图的 `main()`（`__X86SIM__`） | 最低（纯 C++ 功能仿真，不映射硬件） | 快速验证算法正确性 | 秒~分钟级 |
| `aiesim` | 图的 `main()`（`__AIESIM__`） | 周期近似的 AIE 验证 | 验证图拓扑、吞吐、`runtime<ratio>` | 分钟~小时级 |
| `hw_emu` | 主机 `host.cpp`（`xrt::graph`） | 含 PL 仿真 + AIE，接近真实 | 端到端功能验证（含 tiler/stitcher） | 小时级（大图更久） |
| `hw` | 主机 `host.cpp` + SD 卡上板 | 真实硬件 | 交付/性能评测 | 小时级构建 + 上板 |

> 注意驱动者的分裂：**`x86sim` 与 `aiesim` 由 `graph.cpp` 里的 `main()` 驱动**（因为这两档不构建完整主机/ xclbin）；**`hw_emu` 与 `hw` 由 `host.cpp` 驱动**。这正是 `graph.cpp` 的 `main()` 要用宏包起来的原因。

#### 4.2.2 核心流程

构建流程的关键是一个 `TARGET → AIETARGET` 的映射，它决定 AIE 编译器按哪一档后端编译：

```text
TARGET = x86sim          ─▶ AIETARGET = x86sim   ─▶ AIE 编译为 x86 仿真模型
TARGET = aiesim / hw_emu / hw ─▶ AIETARGET = hw  ─▶ AIE 编译为真实硬件模型
```

之后：

- `x86sim`：直接 `$(X86SIMULATOR) --pkg-dir=Work` 跑图 `main()`，读 `data/input.txt`、写 `data/output.txt`。
- `aiesim`：`$(AIESIMULATOR) --pkg-dir=Work --profile` 跑周期近似的图 `main()`。
- `hw_emu`：先把图编成 `libadf.a`，再 `v++ --link` 把 tiler/stitcher XO 与 AIE 容器链成 `xclbin`，编译主机后在 `XCL_EMULATION_MODE=hw_emu` 下运行；嵌入式（aarch64）则打包 SD 卡用 QEMU 启动 `launch_hw_emu.sh`。
- `hw`：同上但 `-t hw`，产出真实 SD 卡，拷贝到 VEK280 上板运行。

#### 4.2.3 源码精读

**① AIETARGET 映射**——Makefile 用一个 `filter` 把四档 `TARGET` 归并到两个 AIE 后端：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile:L119-L123](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile#L119-L123) —— `ifneq ($(filter aiesim hw_emu hw, $(TARGET)),) AIETARGET := hw else AIETARGET := x86sim endif`。即只有 `x86sim` 走 x86 后端，其余三档都走 hw 后端。

**② 图 main() 的仿真驱动**——`graph.cpp` 的 `main()` 用宏门控，只在两档仿真下编译：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/graph.cpp:L24-L39](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/graph.cpp#L24-L39) —— `#if defined(__AIESIM__) || defined(__X86SIM__)` 包住 `main()`，里面依次 `resize.init()` → `update(scalex/scaley)` → `run(1)` → `wait()` → `end()`，缩放因子由 `compute_scalefactor<16>` 算出。注意 `run(1)` 表示图只跑一次（处理一个 tile window）。

**③ 主机驱动（hw_emu/hw）**——`host.cpp` 用 `xrt::graph` 控制图，并经 `xF::xfcvDataMovers` 驱动 tiler/stitcher：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp:L107-L108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp#L107-L108) —— 声明两个数据搬运器：`xF::xfcvDataMovers<xF::TILER, uint8_t, TILE_HEIGHT_IN, TILE_WIDTH_IN, 16> tiler(...)` 与 `<xF::STITCHER, int8_t, ...> stitcher(...)`。模板参数钉死 tile 尺寸与位宽。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp:L114-L120](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp#L114-L120) —— 在 `#if !__X86_DEVICE__` 下：`xrt::graph(gpDhdl, xclbin_uuid, "resize")` 按名打开图（名字 `"resize"` 对应 `graph.cpp` 的全局实例 `resizeGraph resize;`），随后 `gHndl.reset()`、`gHndl.update("resize.k.in[1]", scale_x_fix)` 用「图名.内核.端口」路径写入缩放参数。这一段与 u13-2 讲的 `xrt::graph` 控制模型完全一致。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp:L131-L137](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/host.cpp#L131-L137) —— 主循环：`tiler.host2aie_nb(&src_hndl, ...)` 非阻塞地把图像灌进 Tiler，`stitcher.aie2host_nb(&dst_hndl, ...)` 非阻塞接收结果；`gHndl.run(tiles_sz[0]*tiles_sz[1])` 让图跑「tile 行数 × 列数」次（即处理整幅图所有 tile），`gHndl.wait()` 等图结束。`_nb` 后缀 = non-blocking，是并发的来源。

**④ 运行规则**——Makefile 的 `run` 目标按 TARGET 分派：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile:L303-L311](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/Makefile#L303-L311) —— `x86sim` 调 `$(X86SIMULATOR) --pkg-dir=$(AIE_PKG_DIR)`；`aiesim` 调 `$(AIESIMULATOR) --pkg-dir=$(AIE_PKG_DIR) --profile`。

**⑤ CI 元数据**——`description.json` 把四档仿真都登记进 `testinfo.targets`，并锁定平台：

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json:L5-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json#L5-L7) —— `"platform_allowlist": ["vek280"]`：AIE-ML 只在 VEK280 上跑。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json:L132-L138](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json#L132-L138) —— `targets` 含 `vitis_hw_emu / vitis_hw_build / vitis_hw_run / vitis_aie_x86sim / vitis_aie_sim`，即比纯 PL 用例多出 `vitis_aie_x86sim` 与 `vitis_aie_sim` 两档（对比 4.3 的 colordetect 只有前三档）。

[vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json:L92-L107](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/tests/aie-ml/resize/downscale/8bit_aie_224x224_pl_1ch/description.json#L92-L107) —— `aiecontainers` 段把 `graph.cpp` 编进 `libadf.a`，列出 `xf_resize.cc` 为核级源文件。

> 一个工程上的坑（README 已记录）：少数 AIE-ML 用例因输入图很大，`hw_emu` 耗时极长；`hls2rgb` 的 aiesim、若干带 URAM 的 pipeline 在 `hw_emu` 因工具问题失败，但其他 target 正常——见 `vision/README.md` 的 Known issues 段。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解「同一份 `graph.cpp`/`host.cpp`，四档 TARGET 分别走哪条执行路径」。

**步骤**：

1. 在 `Makefile` 中找到 `AIETARGET` 的赋值（L119-L123），写出 `TARGET=x86sim` 与 `TARGET=hw_emu` 时 `AIETARGET` 分别等于什么。
2. 在 `graph.cpp` 确认 `main()` 被哪两个宏包住，推断 `hw_emu` 下 `graph.cpp` 的 `main()` 是否会被编译进可执行文件。
3. 在 `host.cpp` 找到 `#if !__X86_DEVICE__`，推断 `x86sim` 下 `xrt::graph` 相关代码是否生效。
4. 在 `description.json` 的 `testinfo.targets` 里数清楚 AIE-ML 用例比 PL 用例多了哪两档。

**需要观察的现象**：仿真档（`x86sim`/`aiesim`）与上板档（`hw_emu`/`hw`）走**两套完全不同的驱动代码**，靠宏在编译期切换。

**预期结果**：

| TARGET | AIETARGET | 驱动代码 | 编译进主机？ |
| --- | --- | --- | --- |
| `x86sim` | `x86sim` | `graph.cpp` 的 `main()` | 否（不构建主机） |
| `aiesim` | `hw` | `graph.cpp` 的 `main()` | 否 |
| `hw_emu` | `hw` | `host.cpp` + `xrt::graph` | 是 |
| `hw` | `hw` | `host.cpp` + SD 卡上板 | 是 |

**待本地验证**：在有 Vitis + VEK280 平台的环境下执行 `make run TARGET=x86sim PLATFORM=vek280`，应直接得到 `data/output.txt`，与 `data/output_ref.txt` 比对判 PASS/FAIL。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `graph.cpp` 的 `main()` 要用 `#if defined(__AIESIM__) || defined(__X86SIM__)` 包起来，而不是无条件编译？

**答案**：在 `hw_emu`/`hw` 档，系统由 `host.cpp` 的 `main()` 驱动（它用 `xrt::graph` 控制图）。如果 `graph.cpp` 的 `main()` 也无条件存在，就会与 `host.cpp` 的 `main()` 冲突（重复定义 `main`）。所以只在仿真档（`__AIESIM__`/`__X86SIM__`）才编译图的 `main()`。

**练习 2**：`gHndl.run(tiles_sz[0]*tiles_sz[1])` 里的参数为什么是「tile 行数 × 列数」，而 `graph.cpp` 的仿真 `main()` 里却是 `run(1)`？

**答案**：图每 `run(1)` 处理一个 tile window。仿真档用 `data/input.txt` 只装了一个 tile 的数据，故 `run(1)` 即可；上板档要处理整幅图像，需要 `行数×列数` 个 tile 全部流过图，所以传 tile 总数。

---

### 4.3 L3 多内核图像处理流水线

#### 4.3.1 概念说明

L3 把视角从「单内核」抬升到「完整应用」。L3 README 一句话定义：

[vision/L3/README.md:L1-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/README.md#L1-L6) —— L3 目录是「由一串 Vitis Vision 函数缝合而成的完整应用」，`examples/` 提供 OpenCL 主机代码 + C++ 加速代码，`examples/<function>/config` 提供可改的配置头，`tests/` 与 `benchmarks/` 分别做仿真/构建与跨架构性能对比。

L3 的核心价值（与 u5-l3 一致）：**让中间数据在片上以 `xf::cv::Mat` 直连流动，把「多次 DDR 往返」压成「一次进、一次出」**。在 PL 路线里，这通过把多个算子写进**同一个 `extern "C"` 顶层函数**、用 `#pragma HLS DATAFLOW` 串成任务级流水线来实现，综合后得到单个内核，主机只调用一次。

> 诚实的边界说明：本仓库 `vision/L3/examples/` 下 22 个示例**目前都是 PL（HLS）流水线**（用 `grep` 在 L3 examples 中搜不到任何 `aie-ml`/`L1/include/aie` 引用）。AIE-ML 函数停留在 L1/L2 层。也就是说，vision 的「AIE-ML 函数」与「L3 流水线」目前是两条独立的主线——本讲把它们放在一篇，是因为它们都属于 u9 单元的「vision 进阶」，但你要清楚 L3 示例本身不调用 AIE-ML。

#### 4.3.2 核心流程

以 `colordetect`（颜色检测）为例。它的算法目标是：从一幅 RGB 图中标记出落在指定 HSV 颜色范围内的区域，并用形态学操作（腐蚀/膨胀）把零散的标记连成片。L3 的做法是把这些步骤缝进一个内核：

```text
img_in(DDR)
  │ Array2xfMat          ← 把 AXI 数组转成片上 xf::cv::Mat
  ▼
imgInput ─▶ bgr2hsv ─▶ rgb2hsv(HSV)
                              │ colorthresholding（按 low/high 阈值筛颜色）
                              ▼
                         imgHelper1
                              │ erode  ─▶ imgHelper2
                              │ dilate ─▶ imgHelper3
                              │ dilate ─▶ imgHelper4
                              │ erode  ─▶ imgOutput
                              ▼
                         xfMat2Array ─▶ img_out(DDR)
```

所有 `imgHelper*` 与 `imgInput/imgOutput` 都是片上 `xf::cv::Mat`，`DATAFLOW` 让它们并行流转——稳态吞吐由最慢的那个算子决定（u3-l2、u5-l3 已建立这一结论）。

#### 4.3.3 源码精读

**① 顶层函数签名**——单个 `extern "C"` 内核，多个 `m_axi` 端口：

[vision/L3/examples/colordetect/xf_colordetect_accel.cpp:L19-L27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L19-L27) —— `void color_detect(img_in, low_thresh, high_thresh, process_shape, img_out, rows, cols)`。输入图像、低/高阈值、形态学核（`process_shape`）和输出图像各占一个 `m_axi` bundle（`gmem0..gmem4`），是典型的多 DDR 端口 PL 内核。

**② DATAFLOW 与片上 Mat**——流水线主体：

[vision/L3/examples/colordetect/xf_colordetect_accel.cpp:L44-L50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L44-L50) —— 声明 7 个 `xf::cv::Mat`（输入、HSV 中间、4 个 helper、输出），它们是片上缓存，由 `DATAFLOW` 调度成 FIFO。

[vision/L3/examples/colordetect/xf_colordetect_accel.cpp:L61-L85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L61-L85) —— `#pragma HLS DATAFLOW` 后依次调用：`Array2xfMat`（L65，DDR→Mat）→ `bgr2hsv`（L68）→ `colorthresholding`（L71-L72）→ `erode`（L75-L76）→ `dilate`（L77-L78）→ `dilate`（L79-L80）→ `erode`（L81-L82）→ `xfMat2Array`（L85，Mat→DDR）。共 7 个算子缝在一条流水线里，主机只看到 `color_detect` 这一个内核。

**③ 主机侧**——`description.json` 把这个单内核登记为容器，主机（`xf_colordetect_tb.cpp` + `ext/xcl2/xcl2.cpp`）只调用一次：

[vision/L3/examples/colordetect/description.json:L98-L110](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/description.json#L98-L110) —— `containers` 只有一个 `krnl_colordetect`，其 `accelerators` 只有一个 `color_detect`（300 MHz），印证「L3 PL 流水线综合成单内核」。

[vision/L3/examples/colordetect/description.json:L7-L14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/description.json#L7-L14) —— `platform_allowlist: ["vck190", "u50"]`、`blocklist: ["u280","u250"]`：colordetect 是 PL 内核，跑在 vck190（Versal PL）与 U50（Alveo）上，与 4.2 的 AIE-ML 用例（仅 vek280）形成对照。

[vision/L3/examples/colordetect/description.json:L151-L156](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/description.json#L151-L156) —— `testinfo.targets` 只有 `vitis_hw_emu / vitis_hw_build / vitis_hw_run`，**没有** `vitis_aie_*`——再次印证这是纯 PL 流水线，不走 AIE 仿真档。

> 对比 AIE-ML 单内核用例（4.1/4.2）与 L3 PL 流水线（本节）：前者数据流是 `Tiler→AIE 图→Stitcher`，主机用 `xrt::graph` 控制；后者数据流是片上 `Mat` 直连，主机用普通 `xrt::kernel`（经 xcl2/OpenCL）调用一次。两者都是「多算子缝合」，但缝合位置不同——AIE 在图内连、PL 在 DATAFLOW 内连。

#### 4.3.4 代码实践（源码阅读型 —— 对应总实践任务的后半）

**目标**：拆解一个 L3 流水线，列出它串联的所有内核（本讲总实践任务的后半部分）。

**步骤**：

1. 打开 `vision/L3/examples/colordetect/xf_colordetect_accel.cpp`，在 `DATAFLOW` 区段（L62-L85）从上到下数出所有 `xf::cv::` 调用。
2. 为每个调用写一行：算子名 → 输入 Mat → 输出 Mat → 作用。
3. 打开 `config/xf_config_params.h`（或 `xf_colordetect_accel_config.h`），找到 `FILTER_SIZE`、`ITERATIONS`、`MAXCOLORS` 等宏，推断它们分别控制流水线的哪一段。
4. 浏览 `vision/L3/examples/` 下其他流水线（如 `isppipeline`、`resize_pipeline`、`stereopipeline`），对比它们各串了哪些算子。

**需要观察的现象**：`DATAFLOW` 区段的算子顺序就是图像数据的处理顺序；中间 Mat 的 `XF_CV_DEPTH_*` 模板参数控制各段 FIFO 深度，深度不足会导致死锁或 II 升高。

**预期结果**（colordetect 的算子链）：

| 顺序 | 算子 | 作用 |
| --- | --- | --- |
| 1 | `Array2xfMat` | DDR 数组 → 片上 Mat |
| 2 | `bgr2hsv` | RGB → HSV 色彩空间 |
| 3 | `colorthresholding` | 按阈值筛出目标颜色 |
| 4 | `erode` | 腐蚀，去小噪点 |
| 5 | `dilate` | 膨胀，连片 |
| 6 | `dilate` | 膨胀，强化区域 |
| 7 | `erode` | 腐蚀，收紧边界 |
| 8 | `xfMat2Array` | 片上 Mat → DDR 数组 |

**待本地验证**：若你有 vck190 或 U50 环境，可 `make host xclbin TARGET=hw_emu && make run TARGET=hw_emu`（按 `vision/L3/README.md` 的命令），用 `data/colordetect_128x128.jpeg` 跑出检测结果。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `#pragma HLS DATAFLOW`，这条 colordetect 流水线的吞吐会发生什么变化？

**答案**：没有 `DATAFLOW`，HLS 会把 7 个算子**顺序执行**——前一个算子把整幅图写完 `Mat`，后一个才开始读，每个中间 `Mat` 都要存下完整一帧。结果是：吞吐大幅下降（无法任务级并行），且片上缓存需求暴增（`Mat` 退化成完整帧缓存而非流式 FIFO）。`DATAFLOW` 是把这些算子变成并发 stage 的关键（u3-l2）。

**练习 2**：colordetect 的 `description.json` 里 `testinfo.targets` 没有 `vitis_aie_sim`，这说明什么？如果要把它改成 AIE-ML 流水线，至少需要新增哪些文件？

**答案**：没有 `vitis_aie_*` 说明 colordetect 是纯 PL（HLS）流水线，不经过 AIE 仿真。改成 AIE-ML 流水线至少需要：`graph.h`/`graph.cpp`（ADF 图）、`kernels.h` + `*.cc`（Runner）、`system.cfg`（tiler/stitcher 连接）、AIE-ML 版各算子的 L1 头件，并把 `description.json` 的 `aiecontainers`、`platform_allowlist`（改 vek280）、`testinfo.targets`（加 `vitis_aie_*`）相应改掉。本仓库目前没有这样的 AIE-ML L3 示例。

---

## 5. 综合实践

把本讲的两条主线串成一个完整任务。

**任务**：完成本讲指定的实践——「阅读 vision README 的 AIE 部分，说明 AIE-ML 函数在哪块板子验证；并挑一个 L3 流水线示例描述它串了哪些内核」。

**操作步骤**：

1. **定位 AIE-ML 验证平台**。打开 `vision/README.md`，在「Hardware and Software Requirements」段找到「AIE-ML functions are verified on ___ board」，填上板子名（L7）。再翻到 AIE-ML 开发流程段（L121-L127），列出它的四个开发阶段。
2. **交叉验证**。打开 `vision/L2/tests/aie-ml/resize/.../description.json`，确认 `platform_allowlist` 与 README 的说法一致；记下 `testinfo.targets` 列出的全部仿真/构建档位，指出哪两档是 AIE-ML 独有（PL 用例没有的）。
3. **选一个 L3 流水线并拆解**。推荐 `colordetect`（结构清晰）。打开 `xf_colordetect_accel.cpp` 的 `DATAFLOW` 区段，按 4.3.4 的表格列出入流/出流的全部算子；再确认它的 `description.json` 里 `platform_allowlist` 与 AIE-ML 用例**不同**（vck190/u50 vs vek280），从而亲手验证「AIE-ML 函数在 L1/L2、L3 流水线目前是 PL」这一结论。
4. **画一张对比表**（建议手画或 Markdown 表格）：列出「AIE-ML 单内核用例」与「PL L3 流水线」在 *数据搬运*、*主机控制对象*、*缝合方式*、*仿真档位*、*目标平台* 五个维度的差异。

**需要观察的现象**：你会清楚地看到两类系统在「主机怎么控制」与「数据怎么流」上的根本差别——AIE-ML 走 `xrt::graph` + tiler/stitcher，PL L3 走普通内核调用 + 片上 `Mat` 直连。

**预期结果**：

| 维度 | AIE-ML 单内核用例（resize） | PL L3 流水线（colordetect） |
| --- | --- | --- |
| 数据搬运 | Tiler/stitcher PL 内核 + `xF::xfcvDataMovers` | `Array2xfMat`/`xfMat2Array` 直接 DDR 读写 |
| 主机控制对象 | `xrt::graph`（reset/update/run/wait/end） | `xrt::kernel` / OpenCL（一次调用） |
| 缝合方式 | ADF 图内 `connect`（算子在 AIE 阵列内） | HLS `DATAFLOW`（算子在单 PL 内核内） |
| 仿真档位 | x86sim / aiesim / hw_emu / hw | hw_emu / hw |
| 目标平台 | vek280（Versal AI Edge） | vck190、u50 等 |

**待本地验证**：步骤 1-3 纯源码阅读即可完成；步骤 4 的「跑起来」需要对应硬件（VEK280 / vck190+SD 卡 / U50）与 Vitis 2026.1+ 环境。

## 6. 本讲小结

- vision 的 **AIE-ML 内核**位于 `L1/include/aie-ml/{imgproc,dnn}`，约 45 个图像处理函数，**只在 VEK280 上验证**（README L7）；公共支撑（智能分块、数据搬运器）在 `L1/include/aie/common`。
- AIE-ML 内核的封装链是「**原语类（window 接口）→ Runner 类（`REGISTER_FUNCTION`）→ `adf::graph`（PLIO + async 参数）→ `system.cfg`（Tiler/stitcher 桥接）**」；PL↔AIE 边界由 `Tiler_top`/`stitcher_top` 两个 PL 内核切分/拼回图像 tile。
- AIE-ML 有**四档仿真/构建**：`x86sim`（功能）、`aiesim`（周期近似）由 `graph.cpp` 的 `main()` 驱动；`hw_emu`、`hw` 由 `host.cpp` 的 `xrt::graph` 驱动；`AIETARGET` 把四档 `TARGET` 归并到 `x86sim`/`hw` 两个 AIE 后端；`sw_emu` 自 2025.1 起禁用。
- 主机控制 AIE 图的模式：`xrt::graph(gpDhdl, uuid, "<图名>")` 打开图，`reset()`/`update("图.内核.端口", 值)`/`run(tile数)`/`wait()`/`end()` 控制生命周期；数据由 `tiler.host2aie_nb` / `stitcher.aie2host_nb` 非阻塞搬运。
- **L3 流水线**把多个算子缝合成完整应用；本仓库的 L3 示例目前**全部是 PL（HLS）流水线**，靠单个 `extern "C"` 顶层函数 + `#pragma HLS DATAFLOW` + 片上 `xf::cv::Mat` 直连实现，主机只调用一次；与 AIE-ML 用例在平台（vck190/u50 vs vek280）、仿真档位、控制对象上形成对照。
- colordetect 是典型 L3 PL 流水线，串联 `Array2xfMat → bgr2hsv → colorthresholding → erode → dilate → dilate → erode → xfMat2Array` 共 7 个算子。

## 7. 下一步学习建议

- **深入 AIE 图编程模型**：本讲的 `xrt::graph` 控制、PLIO/window、PL↔AIE 边界只是入门。建议继续学 u13-1（ADF 图、window/stream 与边界）与 u13-2（AIE 图主机控制与 SD 卡打包），它们以 dsp 的 vss_fft_ifft_1d 为蓝本讲得更系统。
- **多读几个 AIE-ML 内核**：挑 `L1/include/aie-ml/imgproc/` 下的 `xf_filter2d_16b_aieml.hpp`、`xf_demosaicing_aie2.hpp`、`xf_resize_bicubic.hpp`，对比它们与 `xf_resize_aie2.hpp` 在 tile 处理上的异同，体会「逐 tile 向量化」这一通用范式。
- **L3 流水线进阶**：浏览 `vision/L3/examples/isppipeline`（ISP 完整流水线，算子更多）、`stereopipeline`（双目立体视觉）、`resize_pipeline`，体会不同应用如何选择缝合的算子；它们都是 PL 流水线，可作为「自己设计 L3」的模板。
- **跨库对照**：回到 u6-l2/u6-l3，对比 dsp 的 AIE 图（`stream` 接口、`kernel::create`）与 vision 的 AIE-ML 图（`window` 接口、`kernel::create_object`、智能分块），理解同一 ADF 框架在不同领域库上的用法差异。
