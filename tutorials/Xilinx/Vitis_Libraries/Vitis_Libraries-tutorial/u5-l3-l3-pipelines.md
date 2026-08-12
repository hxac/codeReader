# 多内核流水线组合（L3）

## 1. 本讲目标

本讲承接 u5-l1（v++ L2 构建流程）与 u5-l2（数据搬运器），把视角从「单内核」抬升到「完整应用」。L3（Level 3）是 Vitis 加速库三层抽象的最高层，目标是把多个内核（或多个 L1 算法函数）组装成一条端到端的应用流水线，比如「读图 → 色彩转换 → 阈值分割 → 形态学 → 写回」一气呵成。

学完本讲你应该能够：

1. 说清 L3 相对 L2 的「组合价值」——为什么要把多个内核串起来。
2. 识别 L3 的两种组合范式：**硬件内 DATAFLOW 拼接**（vision）与 **主机侧软件 API 编排**（blas）。
3. 读懂多内核流水线的两种 host 控制方式：OpenCL 单内核 `enqueueTask` 与高层软件 API。
4. 认识 L3 `benchmarks` 评测目录的用途与组织方式。

## 2. 前置知识

在进入 L3 前，请先回忆以下来自前序讲义的概念：

- **L1/L2/L3 三层抽象**（u1-l3）：L1 是可复用算法原语（一个 HLS C++ 函数），L2 把原语包成可上板内核并配主机程序产出 xclbin，L3 把多个内核串成端到端流水线应用。不是每个库都三层齐全：dsp 目前只交付 L1/L2，而 **blas、vision 三层齐全**。
- **DATAFLOW 任务级流水**（u3-l2）：`#pragma HLS DATAFLOW` 把多个子函数经 `hls::stream` 串成并发流水线，端到端吞吐由最慢的 stage 决定。
- **v++ 系统构建流程**（u5-l1）：`flow=system`，走 v++ `-c/-l/--package` 三段，产出 xclbin；这与 L1 的 `flow=hls`（大写 TARGET 五阶段）是两套不同流程。
- **OpenCL/XRT 主机控制链**（u4-l1、u4-l2）：找设备 → 找 xclbin → 建 Program → 建 Kernel → 建缓冲 → setArg → 启动 → 等待 → 取回结果。

一个直观比喻：L2 内核像「单道工序的机床」，L3 则像「把多道工序串成一条流水车间」，原料从一头进、成品从另一头出，中间数据在片上流过而不必每步都回 DDR。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `vision/L3/README.md` | vision L3 总览：说明 examples/tests/benchmarks 三类目录与运行命令 |
| `vision/L2/README.md` | L2 总览，用于对照「单内核」与 L3「多内核应用」的边界 |
| `vision/L3/examples/colordetect/xf_colordetect_accel.cpp` | **核心样例**：一个 `color_detect` 顶层内核内用 DATAFLOW 串联 7 个视觉函数 |
| `vision/L3/examples/colordetect/xf_colordetect_tb.cpp` | 主机程序（OpenCL）：调用上述单内核，含输入输出与校验 |
| `vision/L3/examples/colordetect/description.json` | 用例元数据：声明 flow、containers（内核容器）、launch、平台白名单 |
| `vision/L3/benchmarks/colordetect/README.md` | L3 评测样例：colordetect 在 x86/ARM/FPGA 上的性能对比表 |
| `blas/L3/README.md` | blas L3 定位：「Vitis software APIs」软件库 |
| `blas/L3/tests/gemm/gemm_test.cpp` | **核心样例**：用 xfblas* 软件 API 完成 GEMM 端到端测试 |
| `blas/L3/benchmarks/` | GEMM/GEMV 评测目录，含 CPU/GPU 对标子目录 |

---

## 4. 核心概念与源码讲解

### 4.1 L3 流水线组合

#### 4.1.1 概念说明

L2 解决「一个内核怎么上板」，L3 解决「一个完整应用怎么搭」。一个真实应用往往不是单一算子，而是多个算子的链条。例如颜色检测：先要把 RGB 转成 HSV，再做颜色阈值分割，再用形态学（腐蚀/膨胀）补全区域。如果每一拍都把中间图像写回 DDR、再由下一个 L2 内核读回来，会产生大量冗余访存，带宽与延迟都很差。

L3 的核心价值就是：**把多个算子拼成一条流水线，让中间数据在片上以 stream / `xf::cv::Mat` 形式直连流动，只对最原始输入和最终输出走 DDR**。这就把「N 次独立的 DDR 往返」压成「1 次进 + 1 次出」。

vision L3 README 一句话点明了这层定位：

> This directory contains full applications, formed by stitching a pipeline of Vitis Vision functions.
> —— [vision/L3/README.md:1-12](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/README.md#L1-L12)

对照 L2：

> Level 2 contains the OpenCL host-callable kernels and engines for various Vitis Vision functions.
> —— [vision/L2/README.md:1-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L2/README.md#L1-L9)

即 L2 交付的是「各个单独的内核」，L3 交付的是「把多个内核缝合成应用的完整工程」。

值得注意的是，Vitis 库里存在**两种风格迥异的 L3 组合范式**：

- **范式 A：硬件内 DATAFLOW 拼接**（vision 代表）。把多个 L1 算法函数写进**同一个 `extern "C"` 顶层函数**里，用 `#pragma HLS DATAFLOW` 让它们在硬件中并发流水，最终综合成**一个内核**。主机只看到一个内核，点火一次，整条流水在硬件里跑完。
- **范式 B：主机侧软件 API 编排**（blas 代表）。L3 交付一套高层 C++ 软件 API（如 `xfblasGemm`），主机像写普通 CPU 程序一样调用，底层封装了对设备内存分配、内核选择、数据搬运的管理。

下面分别精读。

#### 4.1.2 核心流程

**范式 A（vision colordetect）的数据流**：

```
DDR img_in ──► Array2xfMat ──► bgr2hsv ──► colorthresholding ──► erode
                                                                   │
              xfMat2Array ◄── erode ◄── dilate ◄── dilate ◄────────┘
                  │
              DDR img_out
```

关键点：所有中间图像（`imgHelper1..4`、`rgb2hsv`）都是片上 `xf::cv::Mat`，不落 DDR；整条链被 `#pragma HLS DATAFLOW` 包住，各 stage 并发执行。

DATAFLOW 流水线的吞吐与延迟遵循（承接 u3-l2）：

\[ \text{II}_{\text{pipeline}} = \max_{i=1}^{N}\, \text{II}_i \]

\[ \text{latency}_{\text{单帧}} \approx \sum_{i=1}^{N} \text{II}_i + (P - 1)\cdot \text{II}_{\text{pipeline}} \]

其中 \(N\) 是 stage 数、\(P\) 是像素数、\(\text{II}_i\) 是第 \(i\) 个 stage 的启动间隔。也就是说：**稳态吞吐由最慢 stage 决定**，而把多个算子缝进同一流水线，省掉的是中间结果反复进出 DDR 的开销。

**范式 B（blas GEMM）的控制流**：

```
xfblasCreate(xclbin, config_info, engine, numKernel)   // 加载容器、选择内核
        │
xfblasMallocRestricted(A/B/C)                           // 在设备上为矩阵分配内存
        │
xfblasSetMatrixRestricted(A/B/C)                        // 把主机矩阵搬入设备
        │
xfblasGemm(...)                                         // 触发 GEMM 计算
        │
xfblasGetMatrixRestricted(C)                            // 取回结果
        │
compareGemm(C, goldenC)                                 // 与 CPU 参考比对
```

这条链很像写 CPU 上的 BLAS：分配 → 送数 → 计算 → 取数。设备/内核细节被 API 藏了起来。

#### 4.1.3 源码精读

**(1) 范式 A：colordetect 的 DATAFLOW 流水线**

先看顶层内核签名——它就是一个普通的 `extern "C"` 函数，输入一张图、输出一张图：

```cpp
void color_detect(ap_uint<INPUT_PTR_WIDTH>* img_in,
                  unsigned char* low_thresh, unsigned char* high_thresh,
                  unsigned char* process_shape,
                  ap_uint<OUTPUT_PTR_WIDTH>* img_out,
                  int rows, int cols)
```

函数内部先声明一串**片上中间 `xf::cv::Mat`** 作为流水线的「传送带」，每两个相邻算子之间用一条 Mat 连接：

```cpp
xf::cv::Mat<...> imgInput(rows, cols);
xf::cv::Mat<...> rgb2hsv(rows, cols);
xf::cv::Mat<...> imgHelper1(rows, cols);
...
xf::cv::Mat<...> imgOutput(rows, cols);
```

详见 [vision/L3/examples/colordetect/xf_colordetect_accel.cpp:44-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L44-L50)：这里声明了输入、HSV 中转、4 个形态学辅助、输出共 7 个 Mat。

随后是整段的核心——`#pragma HLS DATAFLOW` 把 7 个视觉函数串成并发流水线：

```cpp
#pragma HLS DATAFLOW
xf::cv::Array2xfMat<...>(img_in, imgInput);                 // DDR → Mat
xf::cv::bgr2hsv<...>(imgInput, rgb2hsv);                    // 色彩转换
xf::cv::colorthresholding<...>(rgb2hsv, imgHelper1, ...);   // 阈值分割
xf::cv::erode<...>(imgHelper1, imgHelper2, _kernel);        // 腐蚀
xf::cv::dilate<...>(imgHelper2, imgHelper3, _kernel);       // 膨胀
xf::cv::dilate<...>(imgHelper3, imgHelper4, _kernel);       // 膨胀
xf::cv::erode<...>(imgHelper4, imgOutput, _kernel);         // 腐蚀
xf::cv::xfMat2Array<...>(imgOutput, img_out);               // Mat → DDR
```

完整代码见 [vision/L3/examples/colordetect/xf_colordetect_accel.cpp:62-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L62-L85)。注意几个要点：

- **只有 `img_in` 与 `img_out` 走 m_axi（DDR）**，中间 6 步全在片上 Mat 间直传——这正是 L3 相对「N 个独立 L2 内核」的带宽收益来源。
- 每个 `xf::cv::*` 函数模板都带 `XF_CV_DEPTH_*` 参数，用来指定该条 Mat 通道的 FIFO 深度（承接 u3-l2 的 dataflow 深度调优）。
- `erode`/`dilate` 用同一卷积核 `_kernel` 交替执行两次膨胀 + 两次腐蚀，是经典的「开闭运算」组合，用于平滑与填充颜色区域。

这正是 vision L3 README 所说的「stitching a pipeline of Vitis Vision functions」——缝合发生在硬件内部。

**(2) 范式 B：blas 的软件 API**

blas L3 的定位与 vision 完全不同，README 只有一行：

> Level 3: Vitis software APIs — This directory contains software libraries and APIs for Vitis software users.
> —— [blas/L3/README.md:1-2](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/README.md#L1-L2)

也就是说，blas 把 L3 做成了一套**供软件工程师使用的 API 库**（`blas/L3/include/sw/xf_blas.hpp`），而不是像 vision 那样交付一堆 accel 源码。GEMM 测试用例直接以「写 CPU BLAS」的姿势调用：

```cpp
xfblasEngine_t engineName = XFBLAS_ENGINE_GEMM;
xfblasStatus_t status = xfblasCreate(l_xclbinFile.c_str(), l_configFile, engineName, l_numKernel);
// ... 分配 / 送数 ...
status = xfblasGemm(XFBLAS_OP_N, XFBLAS_OP_N, m, n, k, 1, a, k, b, n, 1, c, n, l_numKernel - 1);
// ... 取回 ...
```

详见 [blas/L3/tests/gemm/gemm_test.cpp:87-93](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L87-L93)（`xfblasCreate` 建立 handle 与选择内核引擎）与 [blas/L3/tests/gemm/gemm_test.cpp:144-161](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L144-L161)（`SetMatrixRestricted` 送数 → `xfblasGemm` 计算 → `GetMatrixRestricted` 取数）。

注意 `xfblasCreate` 的第 4 个参数 `l_numKernel`（[gemm_test.cpp:80-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L80-L85)）：它允许在容器里实例化多个 GEMM 内核，API 内部按编号（`l_numKernel - 1`）路由到具体内核——这是 blas 的「多内核」体现，但被软件 API 完全封装，主机看不到 setArg/start 细节。

**两种范式的对照**：

| 维度 | 范式 A：vision colordetect | 范式 B：blas GEMM |
| --- | --- | --- |
| 组合发生在哪 | 硬件内（一个内核里的 DATAFLOW） | 主机侧（软件 API 编排） |
| 主机看到什么 | 一个 OpenCL 内核 `color_detect` | 一组 `xfblas*` C++ 函数 |
| 中间数据 | 片上 `xf::cv::Mat` 直连 | 由 API 管理的设备内存 |
| 典型用户 | HLS/视觉算法工程师 | 软件工程师 / 数据科学者 |
| 适合场景 | 像素流式、可 dataflow 的图像管线 | 矩阵级、API 友好的线性代数 |

#### 4.1.4 代码实践

**实践目标**：通过阅读 colordetect 源码，亲手数清这条 L3 流水线缝合了哪些视觉函数，并对比一个 L2 单内核示例，体会 L3 的「组合」含义。

**操作步骤**（源码阅读型实践，无需硬件）：

1. 打开 [vision/L3/examples/colordetect/xf_colordetect_accel.cpp:62-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_accel.cpp#L62-L85)。
2. 在 `#pragma HLS DATAFLOW` 之后，逐行列出被调用的 `xf::cv::*` 函数，给每个标注「输入 Mat → 输出 Mat」。
3. 统计：本内核总共缝合了几个独立算法函数？其中只有哪两个接触 DDR？
4. 任选一个 vision L2 单内核示例（例如 `vision/L2/examples/` 下某个只调一次算子的工程），对比它的 accel 文件里 `#pragma HLS DATAFLOW` 之后通常只有 1～2 个算子调用（Array2xfMat → 单算子 → xfMat2Array）。

**需要观察的现象**：

- colordetect 的 DATAFLOW 段有 **8 个函数调用**（含 `Array2xfMat` 与 `xfMat2Array` 两个边界转换），中间夹着 6 个真正的图像算子（bgr2hsv、colorthresholding、erode、dilate、dilate、erode）。
- 只有 `Array2xfMat`（读 `img_in`）与 `xfMat2Array`（写 `img_out`）两个边界函数接触 m_axi 端口；其余 6 个算子只读写片上 Mat。

**预期结果**：你会清楚看到 L3 = 「把 L2 级别的多个单算子缝合进同一个 dataflow 内核」。相比把每个算子做成独立 L2 内核、各自读写 DDR，colordetect 把 6 步中间结果全部留在片上，DDR 流量从「6 次读 + 6 次写」降到「1 次读 + 1 次写」。

> 说明：若想在本地真实构建，需 source Vitis/XRT、设好 `PLATFORM` 与 OpenCV 路径，按 vision/L3 README 执行 `make host xclbin TARGET=hw_emu && make run TARGET=hw_emu`（见 [vision/L3/README.md:13-24](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/README.md#L13-L24)）。该 hw_emu/hw 构建属重型档，耗时较长，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 colordetect 的 6 个中间算子拆成 6 个独立 L2 内核，每个内核各自读写 DDR，相比现在的单内核 DATAFLOW 方案，主要损失是什么？

**参考答案**：主要损失是 DDR 带宽与延迟——每步都要把整张中间图像写回 DDR 再读回，6 步意味着多次全图访存；而 DATAFLOW 方案让中间数据在片上 Mat 直传，只对原始输入和最终输出走 DDR。此外多次 host enqueue 也会增加调度开销。

**练习 2**：DATAFLOW 流水线的稳态吞吐由什么决定？若其中 `erode` 的 II 是 4，其余 stage 的 II 都是 1，整体稳态 II 是多少？

**参考答案**：由最慢 stage 决定，\(\text{II}_{\text{pipeline}}=\max_i \text{II}_i\)。本例中 erode 的 II=4 最大，故整体稳态 II=4（吞吐为 1/4 像素每周期）。要提升整体吞吐，应优先优化最慢的那个 stage。

**练习 3**：为什么说 vision colordetect 是「主机只看到一个内核」？主机程序里到底 `enqueue` 了几次内核？

**参考答案**：因为 7 个函数被缝合进同一个 `extern "C" color_detect` 顶层函数，综合后只产生一个内核实例。主机程序里对它只调用一次 `queue.enqueueTask(kernel)`（见下一节源码），中间没有任何多内核调度。

---

### 4.2 多内核 host 控制方式

#### 4.2.1 概念说明

「多内核流水线」这个名字容易让人以为主机要管理很多内核。实际上 L3 提供了**两个极端**让主机控制复杂度大幅下降：

- **极简控制（vision 范式）**：复杂度被推进了硬件。主机把整条流水当成一个内核，点火一次就完事。
- **零控制（blas 范式）**：复杂度被推进了软件 API。主机甚至不直接碰内核/缓冲，只调高层函数。

这跟 u4-l2 里「主机显式管理 mm2s + s2mm + AIE 图多个 run」的多内核并发控制是不同路线——那条路线把编排权完全交给主机，而 L3 两条范式都尽量替主机减负。

#### 4.2.2 核心流程

vision colordetect 主机控制链（OpenCL）：

```
find_binary_file("krnl_colordetect")
   → import_binary_file → Program → Kernel("color_detect")
   → 建 5 个 Buffer（in 图 / low 阈值 / high 阈值 / shape 核 / out 图）
   → setArg(0..6) 绑定 buffer 与 rows/cols
   → enqueueWriteBuffer(in 图) + enqueueMigrateMemObjects(阈值/核)
   → enqueueTask(kernel)        ← 只点火这一次
   → enqueueReadBuffer(out 图)
   → cv::absdiff 校验
```

整条链只 `enqueueTask` 一次，主机完全不需要知道内核内部其实跑了 7 个函数。

#### 4.2.3 源码精读

主机程序的内核获取与单次启动（OpenCL 路线，承接 u4-l1 的 xcl2 辅助库）：

```cpp
std::string binaryFile = xcl::find_binary_file(device_name, "krnl_colordetect");
cl::Program::Binaries bins = xcl::import_binary_file(binaryFile);
OCL_CHECK(err, cl::Program program(context, devices, bins, NULL, &err));
OCL_CHECK(err, cl::Kernel kernel(program, "color_detect", &err));
```

详见 [vision/L3/examples/colordetect/xf_colordetect_tb.cpp:170-176](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_tb.cpp#L170-L176)：用 xcl2 找到 xclbin → 建程序 → 取出唯一的 `color_detect` 内核。

随后绑参、送数、点火、取数：

```cpp
OCL_CHECK(err, err = kernel.setArg(0, buffer_inImage));   // ... setArg 0..6
OCL_CHECK(err, err = queue.enqueueMigrateMemObjects({buffer_lThres, buffer_hThres, buffer_shapeKrnl}, 0));
OCL_CHECK(err, err = queue.enqueueTask(kernel, NULL, &event));   // 单次点火
clWaitForEvents(1, (const cl_event*)&event);
queue.enqueueReadBuffer(buffer_outImage, ...);
```

详见 [vision/L3/examples/colordetect/xf_colordetect_tb.cpp:193-226](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_tb.cpp#L193-L226)。注意 `setArg` 的下标 0..6 与 4.1.3 里内核签名的参数位置一一对应（in、low、high、shape、out、rows、cols）。

> 对照 u4-l2：原生 XRT 路线下同样可以用 `xrt::kernel` + `xrt::run` + `set_arg/start/wait` 控制这个单内核；OpenCL 与原生 XRT 是等价两套 API（u4-l2）。本例用的是 OpenCL，因 vision 历史上以 OpenCL 为主。

而 blas 范式下，主机根本不出现 `Kernel`/`setArg` 这类调用，只调 `xfblasCreate` → `xfblasGemm` → `xfblasGetMatrixRestricted`（见 4.1.3 第 2 段引用），内核与缓冲细节全部封装在 `blas/L3/include/sw/xf_blas.hpp` 与 `blas/L3/src/sw/api.cpp` 实现里。

**用例元数据如何描述这个单内核**：colordetect 的 `description.json` 用 `containers` 字段声明了「一个容器、一个 accelerator」：

```json
"containers": [
  { "name": "krnl_colordetect",
    "accelerators": [
      { "name": "color_detect",
        "location": "LIB_DIR/L3/examples/colordetect/xf_colordetect_accel.cpp",
        "frequency": 300.0 } ],
    "frequency": 300.0 } ]
```

并声明 `"flow": "system"`（走 v++ 系统流程，对应 u5-l1）、`launch`（hw_emu/hw 的运行参数）、`testinfo`（构建时长/内存上限，承接 u2-l2 的元数据身份证概念）。这佐证了「L3 应用 = 一个由系统流程构建的内核容器」。

#### 4.2.4 代码实践

**实践目标**：对比 vision 与 blas 两条主机控制路径，体会「主机编排 vs 软件 API 封装」的差异。

**操作步骤**（源码阅读型实践）：

1. 在 [colordetect_tb.cpp:170-218](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/colordetect/xf_colordetect_tb.cpp#L170-L218) 中，圈出「找 xclbin → 建内核 → setArg → enqueueTask」这一段，数一下主机显式调用的 OpenCL 对象有几个（Program、Kernel、Buffer、queue…）。
2. 在 [gemm_test.cpp:87-161](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L87-L161) 中，列出主机调用的高层 API 函数序列（Create/Malloc/Set/Gemm/Get）。
3. 对比两份清单：哪一份出现了 `cl::Kernel`/`setArg`？哪一份完全没有？

**需要观察的现象**：

- vision 主机清单里有 `cl::Program`、`cl::Kernel`、`cl::Buffer`、`setArg`、`enqueueTask` 等显式设备/内核对象。
- blas 主机清单里只有 `xfblas*` 函数，没有任何 OpenCL/XRT 内核对象暴露给用户。

**预期结果**：vision 让主机直接面对「一个内核」，blas 让主机面对「一套矩阵 API」。前者更灵活可控，后者更易上手。

#### 4.2.5 小练习与答案

**练习 1**：colordetect 主机里 `enqueueTask(kernel)` 只调了一次，但内核内部却跑了 7 个函数。这是怎么做到的？

**参考答案**：因为这 7 个函数被缝合在同一个 `color_detect` 顶层函数里，并受 `#pragma HLS DATAFLOW` 控制，综合后是一个内核实例。主机点火这个内核一次，硬件内部的 dataflow 调度会让 7 个函数并发流水执行，主机无需逐个调度。

**练习 2**：假如某 L3 应用真的需要主机管理两个**独立**内核（例如一个预处理内核 + 一个推理内核，二者不能缝合进同一 dataflow），主机该用什么机制让它们并发？提示回顾 u4-l2。

**参考答案**：用两个 `xrt::run`（或两次 `enqueueTask`）分别 `start()` 各自的内核，再分别 `wait()`。因为 `start` 非阻塞、`wait` 阻塞，两个 run 就能在硬件里并发（参考 u4-l2 里 mm2s 与 s2mm 的并发点火）。这种「主机显式编排多内核」正是 L3 两条范式之外的第三种路线。

---

### 4.3 benchmarks 评测目录

#### 4.3.1 概念说明

`benchmarks` 目录回答「这个 L3 应用到底比 CPU 快多少」。它把一个完整 L3 应用打包成可直接构建的工程，并给出与 x86/ARM/GPU 等架构的对比数据。vision 与 blas 都提供了 L3 benchmarks，但形态各异：

- **vision L3/benchmarks**：每个 benchmark 是一个完整的 colordetect/blobfromimage 应用工程（含 accel + tb + Makefile），README 给出像素吞吐或帧率对比。
- **blas L3/benchmarks**：按算子分（gemm/gemv），gemm 下还带 `gemm_mkl`（对标 Intel MKL）子目录，gemv 下分 `cpu/` 与 `gpu/` 两个对标平台子目录。

benchmarks 与 tests 的区别：**tests 验证「对不对」（与参考模型比精度），benchmarks 衡量「快不快」（与其它架构比吞吐/延迟）**。

#### 4.3.2 核心流程

一个 benchmark 的典型生命周期：

```
准备输入数据（.bin 矩阵 / .jpeg 图像）
   → 运行加速实现，计时
   → 运行 CPU/参考实现，计时
   → 输出对比表（ms / images-per-sec / GFLOPS）
```

#### 4.3.3 源码精读

**(1) vision colordetect benchmark 的性能表**

[colordetect benchmark README](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/benchmarks/colordetect/README.md) 给出了同一 L3 应用在多架构上的耗时（4K/FULL-HD/SD 三档分辨率，x86 Xeon、x86 i7、ARM、FPGA HW 四种平台）。其结论一行：

> software colordetection cv::colordetect on x86 : 35 images(full-hd)/sec
> hardware accelerated xf::cv::colordetect on FPGA : 133 images(full-hd)/sec

即 FPGA 上的 L3 流水线把 FULL-HD 颜色检测做到 ~133 帧/秒，约为 x86 软件实现的 3.8 倍——这正是「L3 缝合多算子 + 片上 dataflow」带来的端到端收益。

**(2) blas benchmarks 的对标结构**

`blas/L3/benchmarks/` 下：
- `gemm/gemm_bench.cpp`：FPGA GEMM 评测主体，`gemm/gemm_mkl/` 是 Intel MKL 对照工程（含 `gemm_mkl_bench.cpp` 与 `run_gemm_mkl.sh`）。
- `gemv/cpu/` 与 `gemv/gpu/`：分别用 CPU 与 CUDA GPU 实现对照 GEMV。
- 数据位于 `gemm/data/float/` 与 `gemm/data/short/`，按矩阵规模（64/256/512/1024）提供预生成的 `matA_in_*.bin`、`matB_in_*.bin`、`matC_out_*.bin`，确保各平台跑同一组输入、结果可横向对比。

这种「同一份输入数据 + 多个平台实现 + 一张对比表」是 benchmarks 目录的通用组织法。数据文件随仓库分发（而非运行时随机生成），保证结果可复现。

#### 4.3.4 代码实践

**实践目标**：读懂一份 benchmark 的数据组织与对标方式，学会「用什么数据、和谁比、比什么指标」。

**操作步骤**（源码阅读型实践）：

1. 列出 `blas/L3/benchmarks/gemm/data/float/` 下的文件名，归纳命名规律（提示：`mat{A,B,C}_{in,out}_{行}_{列}.bin`）。
2. 打开 `gemm/gemm_mkl/run_gemm_mkl.sh`（如存在），看它如何驱动 MKL 对照。
3. 回到 vision colordetect benchmark README，记录 4K 分辨率下 FPGA HW 的耗时与 x86 Xeon 的耗时，算出加速比。

**需要观察的现象**：

- gemm 数据按「矩阵角色 × 方向 × 规模」命名，同一规模下 A/B/C 三矩阵齐全，便于任意平台直接读取。
- colordetect 4K 下：FPGA HW ≈ 28.15 ms，x86 Xeon ≈ 97.89 ms（**待本地验证**：以仓库 README 表格为准）。

**预期结果**：你能用一句话概括 benchmarks 的三要素——统一输入数据、多平台对照实现、可复现的吞吐/延迟指标。

#### 4.3.5 小练习与答案

**练习 1**：tests 与 benchmarks 都跑同一个算子，它们的目的是什么区别？

**参考答案**：tests 的目的是验证正确性，通常与 CPU 参考模型逐元素比精度、判 PASS/FAIL；benchmarks 的目的是衡量性能，给出与其它架构（x86/ARM/GPU/MKL）的吞吐或延迟对比。前者关心「对不对」，后者关心「快不快」。

**练习 2**：为什么 blas benchmarks 要把输入矩阵以 `.bin` 文件随仓库分发，而不是在程序里随机生成？

**参考答案**：为了保证不同平台（FPGA / MKL / CPU / GPU）跑的是**完全相同**的输入，从而横向对比才公平、可复现。若各平台各自随机生成，结果就失去了可比性。

---

## 5. 综合实践

**任务**：在 `vision/L3/examples` 下挑一个比 colordetect 更复杂的流水线示例（例如 `isppipeline`、`stereopipeline` 或 `all_in_one`），完成一次「L3 应用解剖」。

要求：

1. 打开它的 accel 源文件（通常形如 `xf_*_accel.cpp`），找到 `#pragma HLS DATAFLOW`。
2. 列出 DATAFLOW 段内被缝合的 `xf::cv::*` 函数清单，画出数据流图（哪些是边界函数接触 DDR，哪些是片上中转）。
3. 打开它的 `description.json`，确认 `"flow": "system"`、`containers` 里声明的 accelerator 名字与频率、`launch` 里 hw_emu/hw 的命令行参数。
4. 用一段话说明：这个 L3 应用相比把其中每个算子单独做成 L2 内核，省掉了哪些 DDR 往返。

**交付**：一张数据流图 + 一份「缝合函数清单」+ 一段带宽分析。

> 这个任务把本讲的三个最小模块（流水线组合、host 控制、与性能视角）串起来：你需要读组合（4.1）、确认主机面对的是单内核容器（4.2）、并以「省 DDR」这一性能直觉收尾（呼应 4.3 的 benchmarks 价值）。

---

## 6. 本讲小结

- **L3 = 完整应用**：把多个内核（或多个 L1 算法函数）缝合成端到端流水线，核心收益是让中间数据在片上流动，把「多次 DDR 往返」压成「一次进 + 一次出」。
- **两种 L3 组合范式**：vision 用「硬件内 DATAFLOW 拼接」（多函数缝进单内核），blas 用「主机侧软件 API 编排」（`xfblas*` 高层 API 封装内核细节）。
- **colordetect 样例**：在 `#pragma HLS DATAFLOW` 下缝合 7 个视觉函数（bgr2hsv、colorthresholding、erode×2、dilate×2 加边界转换），中间 6 步只在片上 `xf::cv::Mat` 间直传。
- **host 控制两条路**：vision 主机用 OpenCL 把整条流水当一个内核 `enqueueTask` 一次；blas 主机调 `xfblasCreate/Gemm/GetMatrix`，完全不碰内核对象。
- **description.json 用 `flow=system` + `containers`** 声明 L3 应用是一个走 v++ 系统流程的内核容器（承接 u5-l1）。
- **benchmarks 目录**回答「快不快」：统一输入数据 + 多平台对照 + 吞吐/延迟对比表；与 tests 的「对不对」互补。

## 7. 下一步学习建议

- **若想深入 vision**：本系列 u9（Vision 库）会系统讲 vision 的目录组织、OpenCV 映射与 PL/AIE-ML 内核，届时可把本讲的 colordetect 当作「L3 缝合」的具体实例。
- **若想深入 blas**：u8（BLAS 库）会讲 module/kernel/software-API 三级抽象与本讲提到的 `xfblas*` API、`run_test.py` 测试总线。
- **若想深入 AIE 流水线**：本讲的 DATAFLOW 是 PL 侧（HLS）的任务级流水；u13（AIE 编程模型深入）会讲 ADF 图如何用 window/stream 把多个 AIE 内核连成数据流图，是另一种「多内核组合」范式。
- **若关心构建**：u14-l1 会讲 `description.json` 的 `flow`/`launch`/`testinfo` 如何被 CI 识别，把本讲提到的元数据机制与 Jenkinsfile 串起来。
