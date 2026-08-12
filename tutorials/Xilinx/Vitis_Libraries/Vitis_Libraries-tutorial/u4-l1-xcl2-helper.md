# xcl2 OpenCL 主机辅助库

## 1. 本讲目标

本讲是「主机端编程：OpenCL 与原生 XRT」单元的第一讲。学完后你应该能够：

- 说清 `aligned_allocator` 为什么要把内存按 4096 字节（页边界）对齐，以及不对齐会付出什么代价。
- 复述 `xcl::find_binary_file` 查找 `.xclbin` 的目录顺序与文件名模板，并能据此定位自己编出的 xclbin。
- 用 `xcl::get_xil_devices()` 在主机端枚举 Xilinx 加速设备。
- 区分 `is_emulation()` 与 `is_hw_emulation()` 两个仿真判定函数的触发条件。
- 把 xcl2 当作「跨库共用的主机端脚手架」来阅读，为下一讲原生 XRT API 打基础。

## 2. 前置知识

本讲假设你已经建立以下认知（来自 u1-l3）：

- **L2/L3 走 Vitis 流程**：内核被编译成 `.xclbin`（Xilinx 二进制容器），再由主机程序加载到加速卡上运行。
- **主机端运行时**有两套等价 API：传统的 **OpenCL**（`cl::Device`/`cl::Program`/`cl::Kernel`）和现代的 **原生 XRT C++**（`xrt::device`/`xrt::kernel`）。本讲聚焦 OpenCL 这条线所依赖的辅助库。
- **三档小写 target**：`sw_emu`（软件仿真）、`hw_emu`（硬件仿真）、`hw`（真实上板）。主机程序需要知道当前处于哪一档，才能找到对应名字的 xclbin。

几个术语解释：

- **xclbin**：编译好的加速内核二进制，文件扩展名 `.xclbin`（AWS F1 平台上是 `.awsxclbin`）。
- **CL_MEM_USE_HOST_PTR**：OpenCL 创建缓冲对象时的一种标志，表示「直接使用调用方提供的指针」，而不另外开辟一份主机内存。
- **页对齐（page-aligned）**：地址是页大小（通常 4096 字节）的整数倍。
- **inode**：文件系统里每个文件的唯一编号，`stat.st_ino`。同一个物理文件即使被多个路径访问，inode 也相同——xcl2 用它来辨别「同名文件被多条路径命中」与「真的发现了多个不同文件」。

## 3. 本讲源码地图

xcl2 是一个被多个库（utils、vision 等）各自拷贝到 `<lib>/ext/xcl2/` 的轻量辅助库。`utils/ext/README.md` 明确说明：这个目录的内容来自 Xilinx 的其他开源仓库，**除维护脚本外不要在此新建文件**。

| 文件 | 作用 |
| --- | --- |
| [utils/ext/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/README.md) | 标注 ext 目录来源与维护约定 |
| [utils/ext/xcl2/xcl2.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.hpp) | 头文件：`aligned_allocator` 模板 + `xcl` 命名空间下的函数声明与查找路径文档 |
| [utils/ext/xcl2/xcl2.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp) | 实现：设备枚举、二进制导入、xclbin 路径搜索、仿真判定 |

本讲涉及的最小模块：`aligned_allocator`、`find_binary_file` 查找顺序、`get_xil_devices`、`is_emulation`/`is_hw_emulation`。

## 4. 核心概念与源码讲解

### 4.1 aligned_allocator：页对齐分配器

#### 4.1.1 概念说明

主机程序要把一块内存交给设备当缓冲区。OpenCL 提供了 `CL_MEM_USE_HOST_PTR` 标志，意思是「请直接用我这片主机内存做缓冲，不要再开一份」。

但 XRT/OpenCL 运行时有一个隐含前提：**只有当用户指针是页对齐时，运行时才会真正直接使用它**。否则运行时为了保证 DMA 正确，不得不在背后偷偷再开一片页对齐的「影子缓冲」，于是每一次主机↔设备的数据搬运都会多出一次 `memcpy`：用户指针 ↔ 影子缓冲 ↔ 设备。

`aligned_allocator` 就是一个 STL 风格的分配器，专门把内存按 4096 字节对齐分配，让你能安全地享受 `CL_MEM_USE_HOST_PTR` 的零拷贝红利。

#### 4.1.2 核心流程

```
std::vector<T, aligned_allocator<T>> v(n);
        │
        ├─ 分配阶段：posix_memalign(&ptr, 4096, n*sizeof(T))
        │            → 返回 4096 对齐的裸内存
        │
        └─ 释放阶段：free(p)
```

`posix_memalign` 是 POSIX 的内存分配接口，第一个参数是输出指针、第二个是对齐字节数（必须是 2 的幂且为 `sizeof(void*)` 的倍数，4096 满足）、第三个是字节数。它返回 0 表示成功，非 0 表示失败。

#### 4.1.3 源码精读

头文件里有一段很关键的注释，直接说明了「为什么要对齐」：

> 当用 `CL_MEM_USE_HOST_PTR` 创建缓冲时，只有用户指针恰好页对齐，运行时才会真正使用它；否则会被迫自建一份主机缓冲，所有数据搬运都要多一次 memcpy。

[utils/ext/xcl2/xcl2.hpp:41-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.hpp#L41-L59) 给出了实现，注释与代码紧密对应：

```cpp
template <typename T>
struct aligned_allocator {
    using value_type = T;
    T* allocate(std::size_t num) {
        void* ptr = nullptr;
        if (posix_memalign(&ptr, 4096, num * sizeof(T))) throw std::bad_alloc();
        return reinterpret_cast<T*>(ptr);
    }
    void deallocate(T* p, std::size_t num) { free(p); }
};
```

要点：

- 对齐值硬编码为 **4096**（一页）。
- 分配失败时抛 `std::bad_alloc`，符合 STL 分配器约定。
- 它是一个 struct 模板，常见用法是 `std::vector<float, aligned_allocator<float>>`，让整个容器的存储天然页对齐。

#### 4.1.4 代码实践

**实践目标**：直观感受「页对齐地址的特征」。

**操作步骤**：

1. 新建一个普通 C++ 文件（不依赖 Vitis 工具链，普通 g++ 即可），编写下面这段**示例代码**（非项目原有代码，仅用于演示分配器行为）：

```cpp
#include <iostream>
#include <vector>
#include "xcl2.hpp"   // 需要 -I 指向 utils/ext/xcl2
int main() {
    std::vector<float, aligned_allocator<float>> v(8);
    std::cout << "address = " << (void*)v.data() << "\n";
    std::cout << "mod 4096 = " << ((unsigned long)v.data() % 4096) << "\n";
    return 0;
}
```

2. 编译运行（普通 Linux g++ 即可，因为 `aligned_allocator` 只用了 `posix_memalign`/`free`）：

```bash
g++ -std=c++14 -I utils/ext/xcl2 demo.cpp -o demo && ./demo
```

**需要观察的现象**：打印的地址末尾几位通常是 `000`（十六进制），且 `mod 4096` 应为 `0`。

**预期结果**：`mod 4096 = 0`，证明 `aligned_allocator` 把数据落在了页边界上。若把分配器换回默认 `std::vector<float>`，余数通常不为 0。

> 说明：这一步只为验证对齐行为，不需要加速卡或 Vitis 环境，可在任意 Linux 上运行确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `aligned_allocator` 的对齐值从 4096 改成 1，主机程序还能跑吗？会有什么后果？

**答案**：能编译能跑，但失去了页对齐保证。运行时会发现指针不页对齐，于是自建影子缓冲，每次 DMA 前后多一次 `memcpy`，吞吐下降、延迟上升，但功能正确。

**练习 2**：为什么 `deallocate` 里可以直接用 `free(p)`，而不需要记录当初分配的字节数？

**答案**：因为底层用的是 C 运行时的 `posix_memalign`/`malloc` 族分配，`free` 会自己查分配头的元数据（大小、对齐），无需调用方提供。

---

### 4.2 find_binary_file：xclbin 的查找顺序

#### 4.2.1 概念说明

主机程序要加载内核，第一步就是「在文件系统里找到那个 `.xclbin`」。问题在于：xclbin 的文件名通常带上 **target**（`sw_emu`/`hw_emu`/`hw`）和 **device**（如 `xilinx_u50_gen3x16_xdma_201920_3`）后缀，不同构建产物散落在不同目录。手写路径既繁琐又易错。

`xcl::find_binary_file(device_name, xclbin_name)` 接收「设备名 + 内核基本名」，按一套约定的目录与命名模板去搜索，命中后返回完整路径。它还顺带处理了设备名里的冒号/点号、AWS F1 的 `.awsxclbin`、以及「多个同名文件」报错等细节。

#### 4.2.2 核心流程

整个搜索分三步：

```
① 定 mode（target 名）
   读 XCL_EMULATION_MODE / XCL_TARGET 两个环境变量
   ┌─ 都没设            → "hw"
   ├─ MODE=true, 无TARGET → "sw_emu"
   ├─ MODE=true, 有TARGET → 取 XCL_TARGET 的值
   └─ MODE=其他字符串     → 取该字符串本身

② 定搜索目录（按优先级）
   dirs = { $XCL_BINDIR（若已设）, "xclbin", "..", "." }
   └─ 若 XCL_BINDIR 未设，则跳过第一项

③ 在每个目录里按 4 种文件名模板匹配
   先试 .awsxclbin（AWS F1），找不到再试 .xclbin
   <dir>/<name>.<mode>.<device>.xclbin          ← 最具体
   <dir>/<name>.<mode>.<device_versionless>.xclbin
   <dir>/binary_container_1.xclbin               ← GUI 工程默认名
   <dir>/<name>.xclbin                           ← 最宽松
```

**设备名清洗**：真实设备名形如 `xilinx:xil-accel-rd-ku115:4ddr-xpr:3.2`，含冒号和点号，不能直接拼进文件名。代码把 `:` 和 `.` 都替换成 `_`，得到 `xilinx_xil-accel-rd-ku115_4ddr-xpr_3_2`；同时再生成一个「去掉版本号」的 `device_name_versionless` 变体，用来匹配不带版本号的旧产物。

**去重与冲突检测**：搜索用 `stat` 取每个命中文件的 inode。如果两条不同路径命中的是同一个 inode（同一物理文件），视为正常；如果 inode 不同，说明真有两个内容不同的 xclbin，直接 `exit(EXIT_FAILURE)` 报错，避免误用。

#### 4.2.3 源码精读

头文件里有一份给用户看的权威查找路径文档，值得通读（共 4 个目录 × 4 个模板 = 16 条候选路径）：

[utils/ext/xcl2/xcl2.hpp:64-96](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.hpp#L64-L96) 描述了完整搜索路径与函数签名：

```cpp
std::string find_binary_file(const std::string& _device_name,
                             const std::string& xclbin_name);
```

实现里「定 mode」的逻辑略绕，在 [utils/ext/xcl2/xcl2.cpp:85-110](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L85-L110)：

```cpp
char* xcl_mode = getenv("XCL_EMULATION_MODE");
char* xcl_target = getenv("XCL_TARGET");
std::string mode;
if (xcl_mode == NULL) {                       // 完全没设 → hw
    mode = "hw";
} else if (strcmp(xcl_mode, "true") == 0) {   // 老式写法 =true
    mode = (xcl_target == NULL) ? "sw_emu" : xcl_target;
} else {                                       // 新式写法直接给名字
    mode = xcl_mode;
}
```

注意三种合法写法：完全不设环境变量（`hw`）、`XCL_EMULATION_MODE=true` 配合可选 `XCL_TARGET`（老式 OpenCL 仿真触发方式）、`XCL_EMULATION_MODE=hw_emu`（直接写档名，现代推荐写法）。

搜索目录数组与「跳过未设的 `XCL_BINDIR`」在 [utils/ext/xcl2/xcl2.cpp:111-122](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L111-L122)：

```cpp
char* xcl_bindir = getenv("XCL_BINDIR");
const char* dirs[] = {xcl_bindir, "xclbin", "..", ".", NULL};
const char** search_dirs = dirs;
if (xcl_bindir == NULL) search_dirs++;   // 把第一项挪掉
```

四个文件名模板（`%1$s`=目录、`%2$s`=name、`%3$s`=mode、`%4$s`=device、`%5$s`=device_versionless）在 [utils/ext/xcl2/xcl2.cpp:171-175](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L171-L175)：

```cpp
const char* file_patterns[] = {
    "%1$s/%2$s.%3$s.%4$s.xclbin",     // <name>.<mode>.<device>.xclbin
    "%1$s/%2$s.%3$s.%5$s.xclbin",     // <name>.<mode>.<device_versionless>.xclbin
    "%1$s/binary_container_1.xclbin", // GUI 工程默认名
    "%1$s/%2$s.xclbin",               // <name>.xclbin
    NULL};
```

冲突检测（用 inode 区分「同文件多路径」与「真有两个文件」）在 [utils/ext/xcl2/xcl2.cpp:203-230](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L203-L230)，核心判断是：

```cpp
if (*xclbin_file_name && sb.st_ino != ino) {
    printf("Error: multiple xclbin files discovered ...\n");
    exit(EXIT_FAILURE);
}
```

#### 4.2.4 代码实践

**实践目标**：根据真实的产物文件名，反推 `find_binary_file` 会命中哪一条路径。

**操作步骤**：

1. 假设你在某 L2 example 目录下刚跑完 `make target TARGET=hw_emu`，产物位于 `./xclbin/` 目录，形如 `xclbin/krnl_meanstdev.hw_emu.xilinx_u50_gen3x16_xdma_201920_3.xclbin`。
2. 主机代码里调用 `xcl::find_binary_file(device_name, "krnl_meanstdev")`，其中 `device_name` 来自 `device.getInfo<CL_DEVICE_NAME>()`。
3. 设环境变量为 `export XCL_EMULATION_MODE=hw_emu`。

**需要回答**：

- 此时 `mode` 等于什么？
- `find_binary_file` 会优先在哪个目录、用哪条模板命中？

**预期结果**：`mode = "hw_emu"`；按目录顺序先查 `xclbin/`（因为 `XCL_BINDIR` 未设，被跳过的第一项之后第一个就是 `"xclbin"`），命中第一条模板 `xclbin/krnl_meanstdev.hw_emu.<device>.xclbin`。

> 说明：这是「源码阅读型实践」，无需真实硬件，对照上面的源码逻辑即可推得结论。若要真机确认，可在装有 XRT 的机器上让主机程序打印返回的路径串。

#### 4.2.5 小练习与答案

**练习 1**：如果不设任何环境变量直接跑主机程序，`find_binary_file` 会去找哪种 target 的 xclbin？

**答案**：`XCL_EMULATION_MODE` 未设时 `mode="hw"`，所以会去找 `*.hw.<device>.xclbin`——也就是真实上板的那一份，而不是仿真产物。

**练习 2**：为什么函数要在 `.awsxclbin` 全部找不到之后，才回头去找 `.xclbin`，而不是混在一起按目录优先？

**答案**：`.awsxclbin` 是 AWS F1 专用格式，与普通 `.xclbin` 互斥。先整体扫一遍 aws 模板可保证：在 AWS 环境下一定优先用 awsxclbin；在非 AWS 环境下因根本没有 awsxclbin，自然落入第二轮普通 xclbin 搜索，语义清晰且不会混用两种格式。

---

### 4.3 get_xil_devices：枚举 Xilinx 设备

#### 4.3.1 概念说明

一台机器上可能插了多块加速卡，也可能装了多个 OpenCL 平台（CPU、GPU、FPGA 各自注册一个平台）。主机程序需要从这堆平台里挑出「Xilinx 的 FPGA/ACCELERATOR 设备」。

`xcl::get_xil_devices()` 就是这层薄封装：它枚举所有 OpenCL 平台，找到名字叫 `"Xilinx"` 的那个，再从中取出所有 `CL_DEVICE_TYPE_ACCELERATOR` 类型设备。

#### 4.3.2 核心流程

```
get_xil_devices()
   └─ get_devices("Xilinx")
        ├─ cl::Platform::get(&platforms)         // 列出所有平台
        ├─ for 每个平台：
        │     比对 CL_PLATFORM_NAME == "Xilinx"
        │     命中则 break
        ├─ 若一圈没找到 → 打印错误并 exit(EXIT_FAILURE)
        └─ platform.getDevices(CL_DEVICE_TYPE_ACCELERATOR, &devices)
            → 返回设备向量
```

注意它过滤的是 **ACCELERATOR** 类型设备（FPGA/加速卡），不是 CPU 或 GPU。如果机器上没装 XRT 驱动、或没 source XRT 环境，`platforms` 里根本不会有 `"Xilinx"` 平台，函数会直接退出。

#### 4.3.3 源码精读

`get_xil_devices` 只是一行转发，在 [utils/ext/xcl2/xcl2.cpp:60-62](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L60-L62)：

```cpp
std::vector<cl::Device> get_xil_devices() {
    return get_devices("Xilinx");
}
```

真正的工作在 `get_devices`，[utils/ext/xcl2/xcl2.cpp:35-58](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L35-L58)。两处要点：

```cpp
cl::Platform::get(&platforms);                 // 枚举平台
...
if (platformName == vendor_name) { break; }    // 找 Xilinx 平台
...
if (i == platforms.size()) {                   // 没找到
    std::cout << "Error: Failed to find Xilinx platform" << std::endl;
    exit(EXIT_FAILURE);
}
platform.getDevices(CL_DEVICE_TYPE_ACCELERATOR, &devices);  // 只取加速器
```

配套的 `import_binary_file` 把找到的 xclbin 读进内存、转成 `cl::Program::Binaries`，在 [utils/ext/xcl2/xcl2.cpp:63-83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L63-L83)：它先用 `access(..., R_OK)` 校验可读，不可读就 `exit(EXIT_FAILURE)`，再以二进制流读入并返回。

#### 4.3.4 代码实践

**实践目标**：看真实主机代码如何把 `get_xil_devices` 与本讲其他 API 串成一条「找设备→拿设备名→找 xclbin→建 Program→建 Kernel」的链路。

**操作步骤**：

1. 打开 vision 库的真实示例 [vision/L3/examples/meanstdev_pipeline/xf_meanstdev_pipeline_tb.cpp:333-355](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/examples/meanstdev_pipeline/xf_meanstdev_pipeline_tb.cpp#L333-L355)。
2. 阅读这 20 行，标注每一步用到了哪个 xcl2 API。

真实代码节选：

```cpp
std::vector<cl::Device> devices = xcl::get_xil_devices();      // ① 枚举设备
cl::Device device = devices[0];
OCL_CHECK(err, cl::Context context(device, NULL, NULL, NULL, &err));
OCL_CHECK(err, cl::CommandQueue q(context, device, CL_QUEUE_PROFILING_ENABLE, &err));
OCL_CHECK(err, std::string device_name = device.getInfo<CL_DEVICE_NAME>(&err));  // ② 拿设备名
std::cout << "INFO: Device found - " << device_name << std::endl;

std::string binaryFile = xcl::find_binary_file(device_name, "krnl_meanstdev_nv122rgb"); // ③ 找 xclbin
cl::Program::Binaries bins = xcl::import_binary_file(binaryFile);   // ④ 读入
devices.resize(1);
OCL_CHECK(err, cl::Program program(context, devices, bins, NULL, &err));  // ⑤ 建 Program
OCL_CHECK(err, cl::Kernel krnl(program, "meanstdev_nv122rgb", &err));     // ⑥ 建 Kernel
```

**需要观察的现象**：`device_name` 这个变量既是给用户打印的「设备友好名」，又直接喂给了 `find_binary_file`——这正是 4.2 节里「设备名清洗」要处理冒号/点号的现实来源。

**预期结果**：能画出「`get_xil_devices` → `getInfo<CL_DEVICE_NAME>` → `find_binary_file` → `import_binary_file` → `cl::Program` → `cl::Kernel`」的调用链。

> 说明：此为源码阅读实践，无需运行。若要在真机验证，需装有 XRT 与至少一块 Xilinx 卡，运行后会打印 `INFO: Device found - <设备名>`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果机器上有两块 Xilinx 卡，`get_xil_devices()` 返回几个设备？示例代码用的是哪一块？

**答案**：返回 2 个设备（向量长度为 2）。示例代码用 `devices[0]`，即第一块；要换卡可改下标，但随后 `devices.resize(1)` 会把向量截断为只含第一个设备，供 `cl::Program` 构造使用。

**练习 2**：为什么没找到 Xilinx 平台时要 `exit(EXIT_FAILURE)` 而不是返回空向量？

**答案**：xcl2 的设计哲学是「找不到就快速失败」，让用户立刻看到「环境没装对」（多半是忘了 source XRT），而不是返回空向量、在后续 `devices[0]` 处发生更难理解的段错误。

---

### 4.4 is_emulation / is_hw_emulation：仿真模式判定

#### 4.4.1 概念说明

主机程序有时需要根据「当前是真机还是仿真」走不同分支，例如：

- 仿真模式下内核运行很慢，要减小测试数据量。
- `hw_emu` 下需要额外等待波形/日志产出，要调整超时。
- 真机下才打开高吞吐的大缓冲。

xcl2 提供两个布尔判定：`is_emulation()`（只要是任意仿真就真）与 `is_hw_emulation()`（只有硬件仿真才真）。两者都只看环境变量 `XCL_EMULATION_MODE`，但判断条件不同，常被初学者混淆。

#### 4.4.2 核心流程

```
XCL_EMULATION_MODE 的值         is_emulation()   is_hw_emulation()
─────────────────────────────── ──────────────── ─────────────────
未设置                          false            false
"sw_emu"                        true             false
"hw_emu"                        true             true
"true"（老式触发）              true             false
任意其他非空字符串              true             false
```

- `is_emulation()`：只要变量被设置（非 NULL）就返回 true，不管值是什么。
- `is_hw_emulation()`：更严格，必须值恰好等于 `"hw_emu"` 才返回 true。

#### 4.4.3 源码精读

`is_emulation` 只判存在性，[utils/ext/xcl2/xcl2.cpp:240-247](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L240-L247)：

```cpp
bool is_emulation() {
    char* xcl_mode = getenv("XCL_EMULATION_MODE");
    return xcl_mode != NULL;   // 只要设了就算仿真
}
```

`is_hw_emulation` 还要判值，[utils/ext/xcl2/xcl2.cpp:249-256](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.cpp#L249-L256)：

```cpp
bool is_hw_emulation() {
    char* xcl_mode = getenv("XCL_EMULATION_MODE");
    return (xcl_mode != NULL) && !strcmp(xcl_mode, "hw_emu");
}
```

声明在 [utils/ext/xcl2/xcl2.hpp:98-100](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.hpp#L98-L100)，同处还声明了一个相关函数 `is_xpr_device`（判断设备名是否含 `"xpr"` 子串，用于识别工程阶段未流片的 Xilinx 预发布器件）。

#### 4.4.4 代码实践

**实践目标**：用 shell 直观验证两个函数对同一环境变量的不同反应。

**操作步骤**：

1. 在 shell 里依次设置不同的 `XCL_EMULATION_MODE`，对照上面的真值表预测两个函数的返回值。
2. 若手头有可编译的 xcl2，可写一段**示例代码**（非项目原有）打印二者：

```cpp
#include <iostream>
#include "xcl2.hpp"
int main() {
    std::cout << "is_emulation   = " << xcl::is_emulation() << "\n";
    std::cout << "is_hw_emulation= " << xcl::is_hw_emulation() << "\n";
    return 0;
}
```

3. 分别用 `unset XCL_EMULATION_MODE`、`export XCL_EMULATION_MODE=sw_emu`、`export XCL_EMULATION_MODE=hw_emu` 三种情形运行。

**预期结果**：三种情形下，`(is_emulation, is_hw_emulation)` 分别为 `(0,0)`、`(1,0)`、`(1,1)`，与真值表一致。

> 说明：此函数不依赖加速卡，普通 Linux g++ 即可编译运行验证；若不编译，也可纯靠源码阅读得出结论。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `is_hw_emulation()` 不能简化成「`is_emulation()` 为真即认为硬件仿真」？

**答案**：因为 `sw_emu`（软件仿真）也会设置 `XCL_EMULATION_MODE`，使 `is_emulation()` 为真。但软件仿真根本不跑 RTL，与硬件仿真行为差异巨大（无时序、无资源报告）。必须用 `is_hw_emulation()` 严格判 `"hw_emu"` 才不会误判。

**练习 2**：`find_binary_file`（4.2 节）里的 `mode` 与本节两个判定函数读的是同一个环境变量吗？

**答案**：是的，都读 `XCL_EMULATION_MODE`（`find_binary_file` 还会顺带读 `XCL_TARGET`）。这意味着：当你 `export XCL_EMULATION_MODE=hw_emu` 后，主机既会走 `is_hw_emulation()` 为真的分支，也会让 `find_binary_file` 去找 `*.hw_emu.*.xclbin`——两套行为由一个变量统一驱动，这也是 xcl2 设计上的一致性所在。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「最小可阅读」的 OpenCL 主机骨架分析任务。

**任务**：下面是一段**示例骨架代码**（综合了本讲四个 API，仿照 vision 示例风格，非项目原有文件）：

```cpp
#include <vector>
#include <iostream>
#include "xcl2.hpp"

int main(int argc, char** argv) {
    // 1. 枚举设备
    std::vector<cl::Device> devices = xcl::get_xil_devices();
    cl::Device device = devices[0];

    // 2. 拿设备名（喂给 find_binary_file 做文件名拼接）
    cl_int err;
    std::string device_name = device.getInfo<CL_DEVICE_NAME>(&err);
    std::cout << "INFO: Device found - " << device_name << std::endl;

    // 3. 仿真判定（影响测试数据规模）
    if (xcl::is_emulation()) {
        std::cout << "INFO: running under emulation, using small dataset\n";
    }

    // 4. 找 xclbin 并读入
    std::string bin = xcl::find_binary_file(device_name, "my_kernel");
    cl::Program::Binaries bins = xcl::import_binary_file(bin);

    // 5. 用页对齐向量准备主机缓冲（之后用于 CL_MEM_USE_HOST_PTR）
    std::vector<float, aligned_allocator<float>> in(1024), out(1024);
    (void)in; (void)out;  // 真实场景会绑定到 cl::Buffer
    return 0;
}
```

**请你完成**：

1. 标注第 1～5 步各用了哪个 xcl2 API，并说出该 API 解决的问题。
2. 解释为什么第 2 步必须先拿到 `device_name`，第 4 步才能正确找到 xclbin（提示：回到 4.2 的文件名模板）。
3. 说明第 5 步把 `std::vector` 的分配器换成 `aligned_allocator` 后，后续用 `CL_MEM_USE_HOST_PTR` 创建 `cl::Buffer` 时省掉了哪一次内存拷贝。
4. 若当前 `XCL_EMULATION_MODE` 未设置，`find_binary_file` 会去找哪份 xclbin？`is_emulation()` 返回什么？

**参考答案要点**：

1. `get_xil_devices`（枚举）、`getInfo<CL_DEVICE_NAME>`（取设备名，OpenCL 原生）、`is_emulation`（仿真判定）、`find_binary_file`+`import_binary_file`（定位并读入 xclbin）、`aligned_allocator`（页对齐缓冲）。
2. 因为 xclbin 文件名模板 `<dir>/<name>.<mode>.<device>.xclbin` 的 `<device>` 段需要用清洗后的设备名填充，没有 `device_name` 就拼不出正确文件名。
3. 省掉了「运行时自建影子缓冲」与「用户指针↔影子缓冲」的那次 `memcpy`，直接对用户向量做 DMA。
4. `mode="hw"`，去找 `*.hw.<device>.xclbin`（真实上板产物）；`is_emulation()` 返回 false。

> 说明：本任务为源码阅读与推理型实践，无需硬件即可完成；若要在真机运行骨架，需要 XRT 环境与一块已下载 xclbin 的 Xilinx 卡。**待本地验证**。

## 6. 本讲小结

- xcl2 是被各库拷贝到 `<lib>/ext/xcl2/` 的共用 OpenCL 主机辅助库，封装了设备枚举、xclbin 定位、二进制导入与仿真判定四件杂事。
- `aligned_allocator` 用 `posix_memalign` 把内存按 **4096** 字节对齐，是享受 `CL_MEM_USE_HOST_PTR` 零拷贝的前提；不对齐会被运行时偷偷加一层影子缓冲。
- `find_binary_file` 的核心是「定 mode → 定目录 → 按模板匹配」三步：mode 由 `XCL_EMULATION_MODE`/`XCL_TARGET` 决定（默认 `hw`）；目录优先级为 `$XCL_BINDIR` > `xclbin` > `..` > `.`；文件名从「带 device 全名」到「裸名」共四级宽松度，并用 inode 检测真冲突。
- `get_xil_devices` 找到名为 `"Xilinx"` 的 OpenCL 平台，取其 `CL_DEVICE_TYPE_ACCELERATOR` 设备；找不到则快速失败 `exit`。
- `is_emulation` 只看 `XCL_EMULATION_MODE` 是否被设置，`is_hw_emulation` 还要求值等于 `"hw_emu"`；两者与 `find_binary_file` 读的是同一个环境变量，行为相互一致。

## 7. 下一步学习建议

本讲讲的是**传统 OpenCL** 主机线。下一讲 **u4-l2 原生 XRT C++ API** 将介绍更现代的 `xrt::device`/`xrt::kernel`/`xrt::run` 这套 API——它能做与 OpenCL 等价的事，但更简洁、更类型安全，是 dsp/solver 等 AIE 库默认采用的主机写法。

建议在进入下一讲前：

- 重读 [utils/ext/xcl2/xcl2.hpp:64-96](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/ext/xcl2/xcl2.hpp#L64-L96) 的查找路径文档，确保能默写出 4 个目录 × 4 个模板的搜索逻辑。
- 翻一个真实的 OpenCL 主机示例（如 vision L3 的 meanstdev_pipeline），确认你能在没有 xcl2 注释的情况下，认出 `get_xil_devices`/`find_binary_file`/`import_binary_file` 在主机代码里的固定位置——这个「找设备→找 xclbin→建 Program→建 Kernel」的骨架在下一讲的原生 XRT 里会以 `xrt::device`→`load_xclbin`→`xrt::kernel` 的形态再次出现。
