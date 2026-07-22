# 测试激励与 OpenCL 宿主程序

## 1. 本讲目标

经过 u8-l1（内核接口）、u8-l2（解码算法）、u8-l3（优化指令）三讲，我们已经把 HLS 解码内核 `decode_kernel` 本身讲透了。但一个内核写完，还差两个"外层"才能真正跑起来、真正验证它：

- **谁来喂数据给它？** 内核需要 `ap_uint<64>` 打包的 int8 特征图。
- **谁把它放到 FPGA 上、按下"开始"键、再把结果取回来？** 这需要一段跑在 ARM CPU（PS）上的宿主程序。

本讲就回答这两个问题。读完本讲，你应当能够：

1. 看懂 `test_bench.cpp`：它如何构造 4 层打包输入、逐层调用 `decode_kernel`、把每层结果汇总到 `global_box_count`，以及在软件仿真里验证内核"接得上"。
2. 看懂 `decode.cpp`：标准 Vitis OpenCL 宿主程序的七步流程——找设备、读 xclbin、建 Program/Kernel、建 Buffer、setArg、Map、逐层"灌入→启动→取回"。
3. 理解 `cl::Event` profiling 如何把一次推理拆成 **write / kernel / read** 三段耗时，并能把它与 u7-l3 讲过的 `__TIC__/__TOC__` 细粒度计时区分开。

同时，本讲会**诚实地指出当前 HEAD 源码里几处需要注意/待验证的不一致**（内核名拼写、`out_box_num` 按值传递、profiling 块的语法），让你在读源码时不被这些"坑"绊倒。

## 2. 前置知识

- **HLS 内核的两种"跑法"**：Vitis HLS 提供两条路径。一是 **C 仿真 / 软件仿真（csim / x86sim）**——把内核当成普通 C++ 函数直接 `#include` 调用，不走任何硬件，速度快，用来验证算法逻辑；二是 **硬件部署**——内核已被综合成 PL 里的电路，跑在 FPGA 上，必须由一段宿主程序通过 OpenCL/XRT 驱动。`test_bench.cpp` 对应前者，`decode.cpp` 对应后者。
- **OpenCL/XRT 加速器模型**（见 u5-l1 的 PS/PL 架构）：PS（ARM）和 PL（FPGA）通过 AXI 总线共享 DDR 内存。宿主程序不直接"算"，而是：在 DDR 上开 buffer → 把输入数据 DMA 搬到 device → 启动内核 → 把输出 DMA 搬回 host。这套"搬数据 + 下命令"的 API 就是 OpenCL（Xilinx 用 `cl2.hpp` 封装）。
- **ap_uint<64> 打包**（见 u8-l1）：内核入口是 `ap_uint<64>*`，把 8 个 int8 塞进一个 64 位字，以填满 64 位 AXI 通道、提升带宽利用率。因此喂数据的一方（testbench 或 host）必须按"8 字节一组"的小端方式打包，内核才能用 `idx>>3` / `idx&0x7` 正确拆字节。
- **四层检测头**（见 u8-l2）：YOLOv8 的 P2/P3/P4/P5 四个检测头分别对应网格尺寸 `{200,100,50,25}`、步长 `{4,8,16,32}`，每层的特征元素数为 `layer_size² × 67`（67 = 4×16 距离 bin + 3 类别）。内核一次只处理**一层**，所以要被**逐层调用 4 次**。
- **cl::Event 与 profiling**：OpenCL 命令队列里每个异步操作（搬运、启动内核）都可以挂一个 `cl::Event`。若队列用 `CL_QUEUE_PROFILING_ENABLE` 创建，设备会为每个 event 记录纳秒级的开始/结束时间戳，宿主据此算出每段耗时。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [platform/post_processing/decode_krnl/test_bench.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp) | HLS C 仿真测试激励：构造 4 层打包输入、逐层调用 `decode_kernel`、汇总结果 | **模块 4.1 主角** |
| [platform/post_processing/decode_host/decode.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp) | OpenCL 宿主程序：在 PS 上加载 xclbin、驱动 PL 解码核、profiling | **模块 4.2、4.3 主角** |
| [platform/post_processing/decode_krnl/decode_kernel.h](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.h) | 内核函数原型（`extern "C" void decode_kernel(...)`） | 被两段程序共同调用的"契约" |
| [platform/post_processing/decode_krnl/decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) | 内核实现（u8-l1/u8-l2/u8-l3 已精读） | 交叉验证打包格式、参数顺序 |
| [platform/post_processing/decode_common/decode.h](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_common/decode.h) | OpenCL 头与 4K 对齐分配器（`cl2.hpp`、`aligned_allocator`） | host 的基础设施 |
| [platform/post_processing/decode_krnl/hls_config.cfg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg) | HLS 综合配置（`syn.top`、`tb.file`） | 确认内核名、testbench 文件 |

---

## 4. 核心概念与源码讲解

### 4.1 C++ testbench：用打包数据逐层验证内核

#### 4.1.1 概念说明

`test_bench.cpp` 是 HLS 的**软件仿真测试激励（testbench）**。它把 `decode_kernel` 当成一个普通 C++ 函数 `#include "decode_kernel.h"` 直接调用，全程不涉及 FPGA、OpenCL、xclbin。它的价值是：

- **快**：纯 C++ 编译，秒级运行，可在综合前先确认"算法逻辑 + 数据接驳"是否正确。
- **可观察**：直接在 host 内存里读写输入输出，方便 `std::cout` 打印中间结果。
- **复现内核的"逐层"调用结构**：模拟真实场景里 host 会对内核调用 4 次（每层一次）。

需要强调的是：testbench 喂的是**合成的、确定性的伪数据**（`（idx % 127) - 63`），不是真实 DPU 输出的特征图。所以它验证的是"管道是否通"（能不能跑、跑出多少框、坐标量级是否合理），**不**验证检测精度。精度验证要靠模块 4.2 的 host 加载真实特征图文件来做。

#### 4.1.2 核心流程

testbench 的 `main()` 流程（伪代码）：

```
准备 4 组全局输出数组（容量 max_boxes_num=8192）
for layer in {200, 100, 50, 25}:          # 四个检测头
    total_size = layer² × 67
    PACKED_SIZE = ⌈total_size / 8⌉          # 需要多少个 64 位字
    把 int8 伪数据按 8 字节一组打包进 ap_uint<64>[PACKED_SIZE]
    准备一组 tmp 输出缓冲（容量 tmp_capacity=2048）
    decode_kernel(打包输入, layer_size, stride, tmp 输出..., tmp_box_num)
    把 tmp 结果追加到全局数组（偏移 = global_box_count）
    global_box_count += tmp_box_num
打印总框数与前 5 个框
```

两个关键容量常量值得记住：全局累计容量 `max_boxes_num = 8192`（[test_bench.cpp:13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L13)），单层临时容量 `tmp_capacity = 2048`（[test_bench.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L14)）。后者恰好等于内核的 `MAX_BOXES = 2048`（[decode_kernel.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L14)）——单层最多吐 2048 个候选框，四层累计最多 8192。

#### 4.1.3 源码精读

**(1) 四层配置与全局输出数组**

[test_bench.cpp:10-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L10-L27)：声明常量与四个全局 `static` 输出数组（x/y/w/h/cls/score），用 `static` 是为了让大数组分配在静态区而非栈上，避免栈溢出。`global_box_count` 是跨层累加写指针。

```cpp
const int num_layers   = 4;
const int num_classes  = 3;
const int output_dim   = num_classes + 64;   // 67
const int max_boxes_num = 8192;               // 全局累计容量
const int tmp_capacity   = 2048;              // 单层临时容量
...
int layer_sizes[num_layers] = {200, 100, 50, 25};
float layer_strides[num_layers] = {4.0f, 8.0f, 16.0f, 32.0f};
```

> 这里的 `{200,100,50,25}` / `{4,8,16,32}` 与 host（[decode.cpp:160-161](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L160-L161)）逐字一致——两段程序对"四层"的认知必须对齐。

**(2) 打包输入数据（与内核的 `ap_uint<64>` 接口对齐）**

[test_bench.cpp:33-47](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L33-L47)：按 8 字节一组把 int8 塞进 64 位字。

```cpp
const int WORD_BYTES = 8;
const int PACKED_SIZE = (total_size + WORD_BYTES - 1) / WORD_BYTES;  // 向上取整
ap_uint<64> input_data[PACKED_SIZE];
for (int i = 0; i < PACKED_SIZE; ++i) {
    ap_uint<64> word = 0;
    for (int b = 0; b < WORD_BYTES; ++b) {
        int idx = i * WORD_BYTES + b;
        if (idx < total_size) {
            int8_t val = (int8_t)((idx % 127) - 63);
            word.range(b * 8 + 7, b * 8) = (ap_uint<8>)val;   // 小端：低字节先放
        }
    }
    input_data[i] = word;
}
```

打包大小为 \(\lceil \text{total\_size}/8 \rceil\)。对最大层（200×200×67 = 2,680,000），`PACKED_SIZE = 335000`，恰好等于内核 `m_axi` 的 `depth=335000`（[decode_kernel.cpp:38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L38)）——这是 testbench 与内核打包约定自洽的铁证。字节顺序是**小端**（`b=0` 放最低字节），与内核的 `(int8_t)(word >> (byte_idx*8))`（[decode_kernel.cpp:109](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L109)）匹配。

**(3) 逐层调用内核 + 汇总到全局数组**

[test_bench.cpp:49-87](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L49-L87)：每层准备一组 `tmp_*` 缓冲，调用 `decode_kernel`，再把结果追加到全局数组。

```cpp
decode_kernel(
    input_data, tmp_boxes_x, tmp_boxes_y, tmp_boxes_w, tmp_boxes_h,
    tmp_cls, tmp_score, tmp_box_num);     // 注意：tmp_box_num 按值传入
...
for (int i = 0; i < tmp_box_num; ++i) {   // 把 tmp 追加到全局
    int dst = global_box_count + i;
    out_boxes_x[dst] = tmp_boxes_x[i]; ...
}
global_box_count += tmp_box_num;
```

**(4) 最终汇总打印**

[test_bench.cpp:89-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L89-L99)：打印总框数与前 5 个框的 `cx/cy/w/h/class/score`。

> ⚠️ **代码现状注意（待本地验证）**：内核原型里 `out_box_num` 是**按值传递**的 `int`（[decode_kernel.h:18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.h#L18)），testbench 也按值传入 `tmp_box_num`。而内核内部执行了 `out_box_num = total_boxes;`（[decode_kernel.cpp:335](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L335)）。在纯 C++ 语义（csim）下，按值传递意味着这个回写**对调用者不可见**，`tmp_box_num` 会一直是 0，于是 `global_box_count` 也会一直是 0、打印出 "Total boxes decoded: 0"。在真实硬件里该标量走 `s_axilite` 寄存器（[decode_kernel.cpp:49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L49)），host 通过寄存器映射读回，行为不同。这是读 testbench 时最该警惕的一处，建议你实际 csim 跑一遍确认现象。

#### 4.1.4 代码实践

**实践目标**：亲手构造 `ap_uint<64>` 打包缓冲，确认 testbench 与内核的打包约定一致。

**操作步骤**（纯 C++ 思维实验，**待本地验证**实际运行结果）：

1. 打开 [test_bench.cpp:37-46](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L37-L46)，在打包循环结束后加一段断言式打印，验证"打包进去再按内核方式拆出来"是否等于原值：

   ```cpp
   // 示例代码（非项目原有）：自检打包/拆包一致性
   int probe = 5;                                  // 任选一个元素下标
   int w_idx = probe >> 3, b_idx = probe & 0x7;     // 与内核 decode_kernel.cpp:105-106 同款位运算
   int8_t back = (int8_t)(input_data[w_idx] >> (b_idx * 8));
   std::cout << "probe=" << probe << " back=" << (int)back << '\n';
   ```

2. 对照 [decode_kernel.cpp:104-110](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L104-L110) 确认 `word_idx = idx>>3`、`byte_idx = idx&0x7` 的拆字节方式与你自检代码一致。

**需要观察的现象**：

- `back` 应等于 `(probe % 127) - 63`，即打包与拆包可逆，证明 testbench 的小端打包与内核读取端序对齐。
- 把 `probe` 改成 `total_size-1`（最后一个有效字节）也能正确取回，说明 `PACKED_SIZE` 的向上取整与 `if (idx < total_size)` 守卫配合无误。

**预期结果**：自检打印的 `back` 与手算值完全相等。若不等，最可能的原因是端序弄反（把 `b*8` 当成了高位字节）。

> ⚠️ 如前述，由于 `out_box_num` 按值传递，**不要期待** `Total boxes decoded` 一定非零——请以本地 csim 实跑为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么单层临时容量 `tmp_capacity` 取 2048 而不是和全局一样取 8192？

**参考答案**：因为内核单次调用（单层）最多输出 `MAX_BOXES = 2048` 个候选框（[decode_kernel.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L14)），内核在 `tb < MAX_BOXES` 时才写框（[decode_kernel.cpp:230](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L230)）。单层 tmp 给到 2048 刚好与内核上限对齐，再大是浪费；全局 8192 是四层累计的上界。

**练习 2**：把 `(idx % 127) - 63` 换成真实 DPU 输出后，testbench 还能验证什么、不能验证什么？

**参考答案**：能验证"接驳"——打包格式、逐层调用、坐标量级（`box_cx/cy/w/h` 乘了 `layer_stride` 后应在 `[0, 800)` 像素范围内）；仍不能直接验证"检测精度"，因为精度需要对照 xView3 标注、走 u3-l4 的匈牙利匹配评估流程。testbench 是结构/管道验证，不是精度评估。

---

### 4.2 OpenCL 宿主程序：在 PS 上驱动 PL 解码核

#### 4.2.1 概念说明

`decode.cpp` 是**真实部署路径**上的宿主程序。它交叉编译到 ARM（`Set(VitisArch arm64)`，见 [decode_host/CMakeLists.txt:37](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/CMakeLists.txt#L37)），在 KV260 的 PS 上运行，通过 OpenCL/XRT 把解码内核（已综合进 xclbin）调度到 PL 上执行。

它和 testbench 的本质区别：

| 维度 | test_bench.cpp（模块 4.1） | decode.cpp（本模块） |
|------|---------------------------|----------------------|
| 运行位置 | 开发机 / csim | KV260 的 ARM（PS） |
| 调用方式 | 直接 `#include` 函数调用 | OpenCL `enqueueTask` |
| 内核形态 | C++ 源码函数 | 已综合的 xclbin 电路 |
| 输入来源 | 内存里合成 `(idx%127)-63` | **随机模式** 或 **从文件加载真实特征图** |
| 数据搬运 | 无（共享进程内存） | host↔device 经 DDR DMA |

本模块要建立的核心心智模型是**标准 Vitis OpenCL 宿主七步流程**，并把 `decode.cpp` 的每一行映射到这七步上。

#### 4.2.2 核心流程

```
① 找平台/设备：遍历 OpenCL 平台，找名为 "Xilinx" 的 ACCELERATOR 设备
② 读 xclbin：把比特流文件整体读进内存 buf
③ 建 Program/Kernel：从二进制建 cl::Program，取名为 decode_krnl 的 cl::Kernel
④ 建 Buffer：为 input_data（只读）与 7 个输出（只写）各开一个 cl::Buffer
⑤ setArg：按顺序绑定内核参数；记住 layer_size/layer_stride 的参数下标
⑥ Map：用 enqueueMapBuffer 把 Buffer 映射成 host 指针
⑦ 逐层循环（4 次）：
     灌入输入（写 host 指针）→ 重设 layer_size/layer_stride 标量参数
     enqueueMigrateMemObjects(input)        # host→device
     enqueueTask(kernel)                     # 启动 PL 内核
     enqueueMigrateMemObjects(outputs, HOST) # device→host
     q.finish()                              # 等本层完成
（可选）profiling 三段计时
```

其中第 ⑦ 步是 host 真正"驱动"内核的地方，也是实践任务的重点。

#### 4.2.3 源码精读

**(1) 找 Xilinx 设备**

[decode.cpp:95-113](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L95-L113)：遍历所有 OpenCL 平台，找到名为 `"Xilinx"` 的平台后，在其下找 `CL_DEVICE_TYPE_ACCELERATOR` 设备。KV260 上这个设备就是 PL 侧的 XRT 加速器。

**(2) 读 xclbin 进内存**

[decode.cpp:115-128](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L115-L128)：以二进制方式读整个 xclbin 到 `char* buf`，再用 `cl::Program::Binaries` 包装。`xclbin` 是把解码内核综合、链接后的 FPGA 比特流 + 元数据（见 u8-l5）。

**(3) 从二进制建 Program，取 Kernel**

[decode.cpp:130-153](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L130-L153)：逐个设备尝试 `cl::Program(context, {device}, bins, ...)`，成功后取内核对象：

```cpp
OCL_CHECK(err, krnl_decode = cl::Kernel(program, "decode_krnl", &err));
```

> ⚠️ **代码现状注意（待本地验证）**：这里查的名字是 `"decode_krnl"`，但内核函数实际叫 `decode_kernel`（[decode_kernel.cpp:23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L23)、[decode_kernel.h:8](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.h#L8)），HLS 配置也写明 `syn.top=decode_kernel`、`package.ip.name=decode_kernel`（[hls_config.cfg:11-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/hls_config.cfg#L11-L13)）。xclbin 里注册的内核名来自 HLS 顶层函数名，即 `decode_kernel`。因此 `cl::Kernel(program, "decode_krnl", ...)` 的名字拼写不一致，通常会让 `err != CL_SUCCESS` 并被 `OCL_CHECK` 触发 `exit`。实际部署时这处拼写需要对齐（改 host 字符串或改内核名）——请以你本机构建的 xclbin 里实际注册的内核名为准。

**(4) 建 Buffer + setArg + 记住标量参数下标**

[decode.cpp:163-190](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L163-L190)：先开 8 个 `cl::Buffer`（1 个 `READ_ONLY` 输入 + 7 个 `WRITE_ONLY` 输出），再用自增计数器 `narg` 逐个 `setArg`。关键是把 `layer_size`、`layer_stride` 的参数下标**记下来**：

```cpp
int narg = 0;
OCL_CHECK(err, err = krnl_decode.setArg(narg++, input_data));
int layer_size_arg_index = narg++;      // 记住 layer_size 的下标
int layer_stride_arg_index = narg++;    // 记住 layer_stride 的下标
OCL_CHECK(err, err = krnl_decode.setArg(narg++, out_boxes_x));
... // 其余 6 个输出
```

注意 `narg++` 在两条注释掉的 `setArg` 之后仍然递增，保证下标与内核原型（[decode_kernel.h:8-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.h#L8-L19)）的参数顺序对齐：0=input_data, 1=layer_size, 2=layer_stride, 3..8=六个输出数组, 9=out_box_num。

**(5) Map：把 Buffer 映射成 host 指针**

[decode.cpp:203-218](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L203-L218)：`enqueueMapBuffer` 把 device buffer 映射成一段 host 可读写的指针。输入用 `CL_MAP_WRITE`，输出用 `CL_MAP_READ`。之后 host 就像操作普通数组一样 `ptr_input_data[i] = ...`。

**(6) 逐层循环：灌入→重设标量→搬入→启动→搬回**

[decode.cpp:220-258](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L220-L258) 是本模块最核心的一段：

```cpp
for (size_t layer = 0; layer < num_layers; ++layer) {
    int total_size = feat_layer_sizes[layer]*feat_layer_sizes[layer]*output_dim;
    // —— 灌入输入：文件加载 或 随机模式 ——
    if (dataFileLoaded) { /* 从 file_data[layer] 拷进 ptr_input_data */ }
    else {
        for (int i = 0; i < total_size; i++)
            ptr_input_data[i] = (i % 127) - 63;        // 随机/合成模式
    }
    // —— 重设本层的两个标量参数 ——
    OCL_CHECK(err, err = krnl_decode.setArg(layer_size_arg_index, feat_layer_size));
    OCL_CHECK(err, err = krnl_decode.setArg(layer_stride_arg_index, feat_layer_stride));
    // —— 搬入 → 启动 → 搬回 ——
    q.enqueueMigrateMemObjects({input_data}, 0, nullptr, &evt_write);          // host→device
    q.enqueueTask(krnl_decode, nullptr, &evt_kernel);                          // PL 内核
    q.enqueueMigrateMemObjects({out_boxes_x,...,out_box_num},
                               CL_MIGRATE_MEM_OBJECT_HOST, nullptr, &evt_read); // device→host
    q.finish();
}
```

这里就是实践任务问的"**逐层重设逻辑**"：每个内核对象只 `setArg` 绑定一次 buffer 指针，但 `layer_size`/`layer_stride` 是逐层变化的标量，所以用记住的下标 `layer_size_arg_index`/`layer_stride_arg_index` **每层覆写**（[decode.cpp:246-247](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L246-L247)），无需重建内核。

**(7) 输入数据两种来源**

- **文件加载**：[decode.cpp:224-236](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L224-L236)，由命令行第 2 个参数传入数据文件，`loadDataFile`（[decode.cpp:21-43](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L21-L43)）按行读成 4 个 `int8_t` 数组（每层一个），用于喂**真实** DPU 特征图做精度验证。
- **随机模式**：[decode.cpp:238-242](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L238-L242)，不传数据文件时用 `(i % 127) - 63` 合成数据，仅验证 host↔device 管道与内核能否跑通。

这与 README 描述一致："either with pre-computed feature map outputs from the YOLOv8m model or letting the script generate random inputs for testing"（[README.md:34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L34)）。

> ⚠️ **代码现状注意**：host 声明了 `conf_thresh = 0.25f`（[decode.cpp:158](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L158)），但内核把 `conf_thresh` 硬编码为 `0.9f`（[decode_kernel.cpp:56](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L56)），且 host 并未把该值作为内核参数传入——host 的 `conf_thresh` 实际未被使用。以内核的 0.9 为准。

#### 4.2.4 代码实践

**实践目标**：说清 testbench 与 host 的输入来源差异，并定位 host 里逐层重设标量参数的逻辑。

**操作步骤**（源码阅读型实践）：

1. **对比输入来源**。打开两段代码：
   - testbench：[test_bench.cpp:42](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L42) `int8_t val = (int8_t)((idx % 127) - 63);`——**只有合成模式**，没有文件加载分支。
   - host：[decode.cpp:224-242](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L224-L242)——`if (dataFileLoaded)` 文件加载 **else** 随机模式，二选一。

   把两者的差异填进下表（答案见后）。

   | | testbench | host（随机模式） | host（文件模式） |
   |---|---|---|---|
   | 数据来源 | ? | ? | ? |
   | 是否打包成 ap_uint<64> | ? | ?（否，直接 int8） | ? |

2. **定位逐层重设逻辑**。在 [decode.cpp:182-183](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L182-L183) 找到两个下标的定义，在 [decode.cpp:246-247](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L246-L247) 找到它们的逐层覆写。回答：为什么 buffer 指针只需 `setArg` 一次，而这两个标量却要每层重设？

**需要观察的现象 / 预期结果**：

- 表格答案：testbench 用合成数据**且**显式打包成 `ap_uint<64>`（因为直接调函数、与内核 `ap_uint<64>*` 签名对齐）；host 不论哪种模式都写进 `int8_t* ptr_input_data`（**不**手动打包），打包/拆包由 AXI"只搬字节"在 device 侧隐式完成（内核仍以 `ap_uint<64>*` 读同一片内存，见 u8-l1 的类型双关）。
- 标量重设原因：`cl::Buffer` 句柄在整个循环里不变，绑定一次即可；而 `layer_size`/`layer_stride` 每层取值不同（200/4、100/8、50/16、25/32），必须每层用记住的下标覆写，否则内核会拿上一层的尺寸/步长解码当前层，结果全错。

#### 4.2.5 小练习与答案

**练习 1**：`MAX_FEATURE_SIZE = 2680000`（[decode.cpp:14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L14)）这个数是怎么来的？为什么输入 buffer 按它分配就够四层用？

**参考答案**：\(200 \times 200 \times 67 = 2{,}680{,}000\)，即最大层（P2，200×200）的特征元素数。其余三层（100/50/25）元素数更少，复用同一块输入 buffer 时只需写 `total_size` 个字节（[decode.cpp:234-241](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L234-L241)），所以按最大层一次分配即可覆盖所有层。

**练习 2**：host 的 `OCL_CHECK` 宏（[decode.cpp:1-6](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L1-L6)）在 OpenCL 调用失败时会做什么？这对调试"内核名拼写不一致"有什么帮助？

**参考答案**：它先执行调用，若返回的 `err != CL_SUCCESS` 就打印文件名、行号、调用的表达式与错误码，然后 `exit(EXIT_FAILURE)`。所以若 `cl::Kernel(program, "decode_krnl", &err)` 因名字不匹配而失败，会在 [decode.cpp:145](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L145) 处打印错误码（通常是 `CL_INVALID_KERNEL_NAME` 对应的数值）并退出——这正是定位拼写问题最快的方式。

---

### 4.3 Event profiling：测量 write/kernel/read 三段耗时

#### 4.3.1 概念说明

u7-l3 讲过两种性能测量：`DEEPHI_DPU_CONSUMING_TIME`（量纯 DPU 段）与 `DEEPHI_PROFILING`（打开 `__TIC__/__TOC__` 宏的**函数内**细粒度计时）。本模块讲的是**第三种、更底层的** OpenCL `cl::Event` profiling——它不依赖 Vitis AI 的宏，而是 OpenCL 标准机制：给命令队列里的每个异步操作挂一个 event，设备记录该操作的纳秒级起止时间戳，host 据此把一次"灌入→启动→取回"拆成三段。

三段语义：

- **write（host→device）**：`enqueueMigrateMemObjects(input, 0, ...)` 把输入特征图从 DDR 的 host 区 DMA 到 device 可见区——即"把数据搬上去"。
- **kernel**：`enqueueTask` 期间 PL 内核真正在 FPGA 上解码——即"算"。
- **read（device→host）**：`enqueueMigrateMemObjects(outputs, CL_MIGRATE_MEM_OBJECT_HOST, ...)` 把 7 个输出缓冲搬回 host——即"把结果取下来"。

三者之和就是单层的端到端耗时。对解码核而言，write 与 read 是数据搬运开销，kernel 是纯计算；三者的比例能告诉你瓶颈在"搬"还是在"算"，为 u8-l3 的优化提供依据。

#### 4.3.2 核心流程

```
创建命令队列时带 CL_QUEUE_PROFILING_ENABLE       # 让设备记录时间戳
为 write/kernel/read 三个操作各准备一个 cl::Event
每层循环里把 event 指针传给 enqueueMigrate/Task
循环结束后（若开启 profiling）：
    对每个 event 取 CL_PROFILING_COMMAND_START / END（纳秒）
    write_ns  = write_end  - write_start
    kernel_ns = kernel_end - kernel_start
    read_ns   = read_end   - read_start
    打印三段 + 总和（换算成 ms）
```

关键前提：命令队列必须在创建时加 `CL_QUEUE_PROFILING_ENABLE`（[decode.cpp:138](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L138)），否则 event 不带时间戳。

#### 4.3.3 源码精读

**(1) 三个 event 与 profiling 开关**

[decode.cpp:89-92](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L89-L92)：

```cpp
bool profile_cl = false;
cl::Event evt_write;   // host -> device copy (migrate in)
cl::Event evt_kernel;  // kernel run
cl::Event evt_read;    // device -> host copy (migrate out)
```

**(2) 把 event 挂到三个操作上**

[decode.cpp:250-256](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L250-L256)：每个 `enqueue*` 的最后一个参数就是 event 指针。

```cpp
q.enqueueMigrateMemObjects({input_data}, 0, nullptr, &evt_write);
q.enqueueTask(krnl_decode, nullptr, &evt_kernel);
q.enqueueMigrateMemObjects({out_boxes_x,...,out_box_num},
                           CL_MIGRATE_MEM_OBJECT_HOST, nullptr, &evt_read);
```

**(3) 提取时间戳并算三段耗时**

[decode.cpp:260-285](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L260-L285)：

```cpp
if profile_cl {                                  // 见下方"代码现状注意"
    auto get_ns = [&](const cl::Event &e, cl_profiling_info info) -> cl_ulong {
        return e.getProfilingInfo<cl_ulong>(info, &prof_err);
    };
    cl_ulong write_start  = get_ns(evt_write,  CL_PROFILING_COMMAND_START);
    cl_ulong write_end    = get_ns(evt_write,  CL_PROFILING_COMMAND_END);
    cl_ulong kernel_start = get_ns(evt_kernel, CL_PROFILING_COMMAND_START);
    cl_ulong kernel_end   = get_ns(evt_kernel, CL_PROFILING_COMMAND_END);
    cl_ulong read_start   = get_ns(evt_read,   CL_PROFILING_COMMAND_START);
    cl_ulong read_end     = get_ns(evt_read,   CL_PROFILING_COMMAND_END);

    double write_ns  = double(write_end)  - double(write_start);
    double kernel_ns = double(kernel_end) - double(kernel_start);
    double read_ns   = double(read_end)   - double(read_start);

    printf("Host->Device (write) time:  %.3f ms\n", write_ns/1e6);
    printf("Kernel execution time:      %.3f ms\n", kernel_ns/1e6);
    printf("Device->Host (read) time:   %.3f ms\n", read_ns/1e6);
    printf("Total (write+kernel+read):  %.3f ms\n", (write_ns+kernel_ns+read_ns)/1e6);
}
```

`getProfilingInfo<cl_ulong>(CL_PROFILING_COMMAND_START/END)` 返回设备时钟下的纳秒时间戳。两段相减即该操作的真实墙钟耗时（device 侧测量，比 host 端 `std::chrono` 更准，因为排除了命令入队到真正执行的延迟）。

> ⚠️ **代码现状注意（待本地验证）**：[decode.cpp:260](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L260) 写作 `if profile_cl {`，**缺少括号**，标准 C++ 无法编译（应为 `if (profile_cl)`）；且 `profile_cl` 在 [decode.cpp:89](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L89) 初始化为 `false` 且全程未被赋值。也就是说，**profiling 块按现状既编译不过、即便修正语法也永不执行**。要真正拿到三段耗时，需要：(a) 把 `if profile_cl {` 改成 `if (profile_cl)`；(b) 让 `profile_cl` 能被置真（例如读一个环境变量或命令行开关）。这一段体现的是"profiling 应当长什么样"的设计意图，实际使用前需按本机环境补全——以本地实跑为准。

#### 4.3.4 代码实践

**实践目标**：理解三段耗时的物理含义，并设计一个最小的"打开 profiling"改造。

**操作步骤**（源码阅读 + 改造设计，**待本地验证**）：

1. 在 [decode.cpp:260-285](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L260-L285) 里，把每个 `printf` 对应到一次 `enqueue*` 调用，写出三段的因果链：
   - `write_ns` ← `evt_write` ← [decode.cpp:250](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L250) 的 `enqueueMigrateMemObjects({input_data}, 0, ...)`：把输入特征图从 host 搬到 device。
   - `kernel_ns` ← `evt_kernel` ← [decode.cpp:253](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L253) 的 `enqueueTask`：PL 上跑解码。
   - `read_ns` ← `evt_read` ← [decode.cpp:256](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L256) 的 `enqueueMigrateMemObjects({...outputs}, CL_MIGRATE_MEM_OBJECT_HOST, ...)`：把 7 个输出搬回 host。

2. 设计最小改造（**示例代码**，非项目原有）：用环境变量控制 profiling 开关，替换硬编码的 `false`：

   ```cpp
   // 示例代码：把 decode.cpp:89 的 bool profile_cl = false; 改为
   bool profile_cl = (std::getenv("PROFILE_CL") != nullptr);
   // 并把 decode.cpp:260 的 if profile_cl { 改为 if (profile_cl) {
   ```

**需要观察的现象 / 预期结果**：

- 改造后 `PROFILE_CL=1 ./decode_host decode.xclbin feat.txt` 会逐层打印三段耗时。
- 预期 `kernel` 段（纯 PL 解码）应远小于 u7-l3 里 CPU 软件 `YOLOV8_DECODING` 段——这正是把后处理下放到 HLS 核的收益。`write`/`read` 段是不可避免的 DDR 搬运开销；若它们占比反超 `kernel`，说明瓶颈在带宽而非算力，应回到 u8-l1 的打包/bundle 设计上想办法。

#### 4.3.5 小练习与答案

**练习 1**：本讲的 `cl::Event` profiling 与 u7-l3 的 `DEEPHI_PROFILING`（`__TIC__/__TOC__`）有何区别？各自能量到什么？

**参考答案**：`cl::Event` profiling 是 OpenCL 标准机制，粒度是**命令队列里的整条操作**（一次搬运、一次 enqueueTask），在 device 侧用纳秒时间戳量，适合拆 write/kernel/read 三大段。`__TIC__/__TOC__` 是 Vitis AI 的宏，插在**函数内部**任意两行之间（如 PRE→DPU→YOLOV8_DECODING→SORT），量的是 host 函数里更细的子段。前者粗、偏硬件搬运；后者细、偏 host 侧代码段。两者互补：先用 `__TIC__/__TOC__` 发现解码段是大头（u6-l3/u7-l3 的结论），再把该段下放到 HLS 核，最后用 `cl::Event` 量搬/算比例。

**练习 2**：为什么必须用 `CL_QUEUE_PROFILING_ENABLE` 创建命令队列（[decode.cpp:138](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L138)）才能拿到时间戳？不加会怎样？

**参考答案**：OpenCL 规定只有带 `CL_QUEUE_PROFILING_ENABLE` 的队列才会让设备为每个 event 记录 `CL_PROFILING_COMMAND_START/END`。不加这个标志，`getProfilingInfo` 返回的是未定义/零值，三段耗时无法测量。这是一个"开关"——profiling 会带来少量运行时开销，所以默认关闭、按需打开。

---

## 5. 综合实践

**任务：跟踪一个检测头（P2 层）从 testbench 到 host 的完整数据路径，并对照两段程序的打包约定。**

请按下列步骤完成（源码阅读型，**待本地验证**实跑）：

1. **选定 P2 层参数**：`layer_size=200`、`stride=4`、`output_dim=67`。计算 `total_size` 与 `PACKED_SIZE`，确认 `PACKED_SIZE=335000` 与内核 `depth=335000`（[decode_kernel.cpp:38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L38)）一致。

2. **testbench 侧**：在 [test_bench.cpp:37-46](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L37-L46) 追踪一个具体字节（如 `idx=67`，即第 1 个 anchor 的第 1 个类别 logit）如何被 `word.range(b*8+7, b*8)` 放进某个 64 位字的某个字节位。

3. **host 侧**：在 [decode.cpp:239-241](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L239-L241) 确认 host 用 `int8_t* ptr_input_data` 写同一个 `idx`，**没有**显式打包。然后回答：host 写的是 `int8_t`、内核读的是 `ap_uint<64>`，为什么两者能对上？（提示：回到 u8-l1 的"AXI 只搬字节 + 类型双关"——同一片 DDR 内存，host 视角是 2680000 个 `int8_t`，内核视角是 335000 个 `ap_uint<64>`，二者在字节层面完全重合。）

4. **取回路径**：在 [decode.cpp:256](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_host/decode.cpp#L256) 指出哪 7 个 buffer 被搬回 host，并对照内核的 6 个输出数组（`out_boxes_x/y/w/h`、`out_cls`、`out_score`）+ 1 个框数（`out_box_num`）。

5. **诚实记录坑点**：在笔记里写下本讲指出的三处源码现状问题（内核名 `decode_krnl` vs `decode_kernel`、`out_box_num` 按值传递、`if profile_cl {` 缺括号），标注你打算如何在本机环境逐一验证/修正。

**预期产出**：一张"P2 层数据路径图"，标清 testbench 与 host 在"打包"上的差异（显式 `ap_uint<64>` vs 隐式 `int8_t`），以及 host 七步流程里 P2 层对应的行号区间。

## 6. 本讲小结

- **testbench（`test_bench.cpp`）是软件仿真激励**：把 `decode_kernel` 当普通函数调用，构造 4 层 `ap_uint<64>` 打包输入（小端、8 字节一组），逐层调用并汇总到 `global_box_count`；它验证"管道通不通"，不验证精度。
- **打包约定是两段程序与内核的共同契约**：最大层 `PACKED_SIZE=335000` 与内核 `m_axi depth=335000` 自洽；testbench 显式打包，host 写 `int8_t` 靠 AXI 字节搬运隐式对齐。
- **host（`decode.cpp`）是标准 Vitis OpenCL 七步流程**：找设备→读 xclbin→建 Program/Kernel→建 Buffer→setArg→Map→逐层"灌入/重设标量/搬入/启动/搬回"。
- **逐层重设靠记住的参数下标**：`layer_size_arg_index`/`layer_stride_arg_index` 让 buffer 绑定一次、标量每层覆写，无需重建内核对象。
- **`cl::Event` profiling 把一次推理拆成 write/kernel/read 三段**：须用 `CL_QUEUE_PROFILING_ENABLE` 建队列；它与 u7-l3 的 `__TIC__/__TOC__` 宏互补，前者量硬件搬运三段、后者量 host 函数内细段。
- **诚实面对源码现状**：当前 HEAD 存在内核名拼写不一致（`decode_krnl` vs `decode_kernel`）、`out_box_num` 按值传递、profiling 块缺括号且开关硬编码 false 等问题，实际部署/测量前需按本机环境核对修正。

## 7. 下一步学习建议

- **走向端到端部署**：本讲讲清了"内核怎么被喂数据、怎么被驱动"，下一篇 **u8-l5（HLS 综合配置与平台打包）** 会把 `hls_config.cfg`→`.xo`→xclbin→`decodeapp`/`decode_host` 交叉编译→`dtg_output`/shell 部署的完整链条补上，把 testbench/host 与 u5 的固件三件套串成可上板运行的整体。
- **回到性能闭环**：带着本讲的 write/kernel/read 三段视角重读 u7-l3 的 `inference_breakdown.jpg`，体会"为什么要把 CPU 软件 `YOLOV8_DECODING` 下放到本核"——这正是 u9-l2 性能-精度-功耗权衡的硬件落点。
- **建议继续阅读的源码**：`decode_host/CMakeLists.txt`（看清 x86sim 仿真目标与 hw 交叉编译目标的差异、`SYSROOT`/`VITIS_PLATFORM_PATH` 等硬编码绝对路径为何要改）、`decode_common/decode.h`（`aligned_allocator` 的 4K 对齐为何对 AXI DMA 必要）。
