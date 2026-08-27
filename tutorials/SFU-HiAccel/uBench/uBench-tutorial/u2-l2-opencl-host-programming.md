# 主机端编程模型：xcl2 封装、OpenCL 对象与内存 bank 绑定

## 1. 本讲目标

读完本讲，你应该能够：

1. 独立跟踪 `host.cpp` 中从设备枚举到内核返回的完整标准序列：`get_xil_devices` → `Context/CommandQueue` → `Program` → `Kernel` → `Buffer` → `setArg` → `enqueueTask` → `finish`。
2. 解释 `cl_mem_ext_ptr_t` 这个 Xilinx 扩展结构体的三个字段，以及两种 bank 标志写法（`XCL_MEM_DDR_BANK1` 与 `n | XCL_MEM_TOPOLOGY`）分别如何把缓冲区绑定到指定 DDR/HBM bank，并理解主机端 flag 必须与 `ubench.ini` 的 `sp=` 行一一对应。
3. 说出 `common/includes/xcl2` 提供的 `get_xil_devices`、`read_binary_file`、`is_emulation`、`aligned_allocator` 各自的用途。

本讲是读懂仓库中**所有**主机程序的钥匙：uBench 每个微基准的 `host.cpp` 都共享同一套骨架，只有 bank flag、参数个数和计分公式不同。

## 2. 前置知识

### 2.1 主机 / 设备异构模型

FPGA 加速程序分成两端：

- **主机端（host）**：运行在 x86 服务器（或 ZCU104 上的 ARM）上的普通 C++ 程序，负责准备数据、把数据搬进 FPGA 的全局内存、启动内核、收回结果。
- **设备端（device）**：编译成 `xclbin` 位流的 HLS 内核（上一讲 `u2-l1` 讲过的 `krnl_ubench`），只能访问 FPGA 板上的全局内存（Alveo U200 的 DDR4、U280 的 HBM2）。

主机不能直接调用设备上的函数，只能通过 OpenCL/XRT 运行时「远程遥控」：写寄存器、搬内存、发启动命令。

### 2.2 OpenCL 对象层级

OpenCL 用一组 C++ 对象描述这台「遥控器」，创建顺序严格分层：

```
Platform（厂商，如 "Xilinx"）
  └── Device（一块 U200/U280 卡）
        └── Context（设备上的资源容器）
              ├── CommandQueue（命令队列：向设备下达搬运/启动命令）
              ├── Program（加载 xclbin 二进制）
              │     └── Kernel（程序里的一个内核，可指定具体计算单元 CU）
              └── Buffer（设备全局内存上的缓冲区）
```

uBench 使用的是 OpenCL C++ 绑定（`cl2.hpp` 里的 `cl::Context`、`cl::Kernel` 等），再叠加 Xilinx 扩展头 `CL/cl_ext_xilinx.h`（提供 `cl_mem_ext_ptr_t` 与 `XCL_MEM_*` 标志）。

### 2.3 本讲要用到的两个旧概念

- **m_axi 与 s_axilite**（详见 u2-l1）：内核的 `m_axi` 端口是内核主动读全局内存的「数据通道」；`s_axilite` 的 `control` 寄存器组则是主机「遥控」内核的通道——`setArg` 写的就是这组寄存器。
- **仿真模式**（详见 u1-l3）：`make check TARGET=sw_emu` 运行时 XRT 会设置环境变量 `XCL_EMULATION_MODE`，主机程序靠它区分仿真与真机。

### 2.4 为什么需要 bank 绑定

U200 有两条独立 DDR4 通道（bank 0/1），U280 有 32 个 HBM 伪通道（bank 0~31）。链接期 `ubench.ini` 的 `sp=` 行把内核端口**连线**到某个 bank；运行期主机创建的 `Buffer` 也必须**放置**在同一个 bank——两端对不上，轻则数据落错通道测不出真实带宽，重则运行时报错。`cl_mem_ext_ptr_t` 就是 Xilinx 给 `cl::Buffer` 增加「指定 bank」能力的扩展参数。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) | 本讲精读对象：DDR 版读带宽微基准的主机程序（178 行） |
| [ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp) | HBM 版对照：仅 bank 标志写法不同 |
| [common/includes/xcl2/xcl2.hpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp) | xcl2 公共库头文件：`OCL_CHECK` 宏、`aligned_allocator`、`xcl` 命名空间声明 |
| [common/includes/xcl2/xcl2.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp) | xcl2 实现：设备枚举、xclbin 读取、仿真判断的具体代码 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) | 内核签名：`setArg` 的位置参数要与它对齐 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini) | 链接配置：`sp=` 行决定内核端口连到哪个 bank，主机 flag 必须与之匹配 |
| [ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini) | HBM 版链接配置：`sp=` 指到 `HBM[0]` |

## 4. 核心概念与源码讲解

### 4.1 设备与程序初始化

#### 4.1.1 概念说明

主机程序的第一项工作是「找到一块 Xilinx 卡并把 xclbin 灌进去」。这段代码完全是模板化的：uBench 仓库里每个 `host.cpp` 的开头几十行几乎逐字相同，全部来自 Xilinx Vitis 示例的 `xcl2` 辅助库 + 标准设备探测循环。理解一次，处处能读。

`xcl2` 库（`common/includes/xcl2/`）把易错的样板代码封装成四个常用工具：

| 工具 | 位置 | 用途 |
| --- | --- | --- |
| `xcl::get_xil_devices()` | [xcl2.cpp:L64](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L64) | 枚举所有 Xilinx 平台上的加速器设备 |
| `xcl::read_binary_file()` | [xcl2.cpp:L66-L85](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L66-L85) | 把 `xclbin` 文件读进内存字节缓冲，供 `cl::Program` 使用 |
| `xcl::is_emulation()` | [xcl2.cpp:L87-L94](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L87-L94) | 检查环境变量 `XCL_EMULATION_MODE`，判断是否运行在仿真模式 |
| `aligned_allocator<T>` | [xcl2.hpp:L61-L76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L61-L76) | 4096 字节对齐的内存分配器，配合零拷贝缓冲使用 |

此外 [xcl2.hpp:L40-L46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L40-L46) 定义了贯穿全部主机代码的错误检查宏 `OCL_CHECK(error, call)`：先执行 `call`，若错误码非 `CL_SUCCESS` 就打印 `文件:行号` 与错误码并 `exit`。本讲后续所有 `OCL_CHECK(err, ...)` 都是这个宏。

#### 4.1.2 核心流程

设备初始化的执行过程：

```
1. 解析命令行参数：argv[1] = xclbin 文件路径
2. devices = xcl::get_xil_devices()          # 找 Xilinx 平台 → 列出加速器设备
3. fileBuf = xcl::read_binary_file(xclbin)   # xclbin 读入字节缓冲
4. bins = {{fileBuf.data(), fileBuf.size()}} # 包装成 Program::Binaries
5. for 每个候选设备 device[i]:
     a. 创建 Context（设备资源容器）
     b. 创建 CommandQueue（命令通道，带乱序+性能计数两个标志）
     c. 尝试 cl::Program(context, device, bins) 灌入 xclbin
     d. 成功 → 按 CU 名创建 Kernel 对象，break
        失败 → 打印错误，试下一块卡
6. 一块都没灌成功 → 退出
```

注意第 5 步的语义：多卡机器上逐块尝试，**第一块灌入成功的卡即被锁定**（`break`）。这是 Vitis 示例的通用写法，不是 uBench 特有的多卡调度。

#### 4.1.3 源码精读

**第一步：找到设备。** [host.cpp:L43-L48](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L43-L48) 用两行完成设备枚举与二进制加载：

```cpp
auto devices = xcl::get_xil_devices();
auto fileBuf = xcl::read_binary_file(binaryFile);
```

`get_xil_devices` 的实现在 [xcl2.cpp:L36-L64](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L36-L64)：先 `cl::Platform::get` 拿到系统里所有 OpenCL 平台，逐个比对平台名是否等于 `"Xilinx"`，找不到就直接 `exit`；找到后 `platform.getDevices(CL_DEVICE_TYPE_ACCELERATOR, &devices)` 只保留「加速器」类设备（GPU/CPU 会被过滤掉）。

`read_binary_file` 的实现在 [xcl2.cpp:L70-L84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L70-L84)：先用 `access(..., R_OK)` 检查文件存在（不存在则提示「please build」并退出——这就是忘跑 `make` 时看到的报错来源），再用 `ifstream` 二进制读入 `std::vector<unsigned char>`。

**第二步：包装二进制。** [host.cpp:L50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L50)：

```cpp
cl::Program::Binaries bins{{fileBuf.data(), fileBuf.size()}};
```

`Binaries` 是「字节指针 + 长度」对的向量。能这样从内存数组直接构造 `cl::Program`，依赖 [xcl2.hpp:L33-L37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L33-L37) 里的宏 `CL_HPP_ENABLE_PROGRAM_CONSTRUCTION_FROM_ARRAY_COMPATIBILITY`——这是 xcl2 头文件存在的意义之一：在包含 `cl2.hpp` 之前预设这些兼容宏。

**第三步：逐设备尝试建 Context / Queue / Program。** [host.cpp:L52-L65](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L52-L65)：

```cpp
for (unsigned int i = 0; i < devices.size(); i++) {
    auto device = devices[i];
    OCL_CHECK(err, context = cl::Context({device}, NULL, NULL, NULL, &err));
    OCL_CHECK(err,
              q = cl::CommandQueue(context, {device},
                                   CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE |
                                       CL_QUEUE_PROFILING_ENABLE, &err));
    cl::Program program(context, {device}, bins, NULL, &err);
    if (err != CL_SUCCESS) { /* 换下一块卡 */ } else { /* 创建 Kernel 并 break */ }
}
```

命令队列带了两个值得记住的标志：

- `CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE`：乱序队列。默认的顺序队列里，后入队的命令要等前一个完成才开始；乱序模式下向同一队列 `enqueueTask` 多个内核（对应 `NUM_KERNEL > 1` 的多 CU 微基准）可以真正并发执行——这对带宽测试是**必需**的，否则多 CU 会退化成串行。
- `CL_QUEUE_PROFILING_ENABLE`：允许运行时给队列里的命令打时间戳（本讲的主机代码并未读取事件时间戳，但它为 profile_summary 报告铺路，详见 u7-l3）。

**第四步：按「内核名:CU 名」创建 Kernel 对象。** [host.cpp:L73-L84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L73-L84)：

```cpp
std::string cu_id = std::to_string(i + 1);
std::string krnl_name_full = krnl_name + ":{" + krnl_name + "_" + cu_id + "}";
OCL_CHECK(err, cmpt_krnl[i] = cl::Kernel(program, krnl_name_full.c_str(), &err));
```

拼出来的名字是 `krnl_ubench:{krnl_ubench_1}`——「内核 `krnl_ubench` 的 1 号计算单元 `krnl_ubench_1`」。这个 CU 名不是随便起的：`v++` 链接器按 `nk=`（number of kernels）指令生成 `krnl_ubench_1`、`krnl_ubench_2`…… 的实例名。看 [ubench.ini:L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L6) 的 `nk=krnl_ubench:1`，就知道主机端只能也只需要创建 1 个 Kernel 对象。用具体 CU 名创建的 Kernel 对象**只驱动该 CU**，这在多实例带宽测试里保证了每个队列命令的确定性。

最后 [host.cpp:L90-L93](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L90-L93)：一块都没成功就 `exit(EXIT_FAILURE)`。

#### 4.1.4 代码实践

**实践目标**：用「观察设备枚举」验证上述流程，而不需要理解后面的带宽逻辑。

**操作步骤**：

1. 打开 [host.cpp:L30-L93](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L30-L93)，给 [L63-L64](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L63-L64) 的打印语句后面追加一行（示例代码，仅用于本地观察，不要提交）：
   ```cpp
   std::cout << "  total candidate devices: " << devices.size() << std::endl;
   ```
2. 若本机装有 Vitis + XRT（见 u1-l3 的环境清单），运行 `make check TARGET=sw_emu`；没有硬件环境则跳过运行，做纯阅读。
3. 故意传一个不存在的 xclbin 路径运行（或阅读 [xcl2.cpp:L70-L74](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L70-L74)），观察/推演报错输出。

**需要观察的现象**（真机或 sw_emu 下，待本地验证）：

- 依次打印 `Found Platform`、`Platform Name: Xilinx`（来自 [xcl2.cpp:L48-L49](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L48-L49)）；
- `Trying to program device[0]: ...` 后紧跟 `Device[0]: program successful!`；
- 传入坏路径时打印 `ERROR: <文件名> xclbin not available please build` 后进程退出。

**预期结果**：确认设备发现顺序 = 平台扫描 → 加速器过滤 → 逐卡尝试灌 xclbin，与 4.1.2 的流程图一致。

#### 4.1.5 小练习与答案

**练习 1**：主机程序如何区分「仿真运行」和「真机运行」？代码在哪里判断？
**答案**：调用 `xcl::is_emulation()`，实现见 [xcl2.cpp:L87-L94](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L87-L94)——检查环境变量 `XCL_EMULATION_MODE` 是否存在（`make check` 在 sw_emu/hw_emu 下会自动设置它，见 u1-l3）。host.cpp 用它把 payload 压小以缩短仿真时间（[host.cpp:L102-L104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L102-L104)）。同文件还有更精细的 `is_hw_emulation()`（[L96-L103](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L96-L103)），只在值为 `hw_emu` 时为真。

**练习 2**：如果 `ubench.ini` 写 `nk=krnl_ubench:4` 而主机仍是 `NUM_KERNEL 1`，会发生什么？
**答案**：位流里生成 4 个 CU（`krnl_ubench_1` ~ `krnl_ubench_4`），主机只创建绑定 `krnl_ubench_1` 的 Kernel 对象并只启动它，其余 3 个 CU 空闲——测得的带宽只反映 1 个 CU。要让 4 个 CU 同时压测内存，需同步把 [host.cpp:L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15) 的 `NUM_KERNEL` 改为 4（循环 [L73-L84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L73-L84) 会自动创建 4 个 Kernel 对象）。

**练习 3**：`OCL_CHECK(err, context = cl::Context(...))` 中，如果 `cl::Context` 构造失败，程序行为是什么？
**答案**：宏展开为先执行赋值调用、再检查 `err`（[xcl2.hpp:L40-L46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L40-L46)）：非 `CL_SUCCESS` 时打印 `源文件:行号`、失败的调用文本和错误码，然后 `exit(EXIT_FAILURE)` 立即退出。注意宏注释提醒：对含模板化函数调用的表达式该宏不适用。

### 4.2 bank 绑定缓冲

#### 4.2.1 概念说明

这是本讲的核心模块：**如何把一个主机缓冲区放进 FPGA 的指定内存 bank**。

链条上有三个必须对齐的点：

1. **链接期（ini）**：`ubench.ini` 的 `sp=krnl_ubench_1.in0:DDR[1]` 把内核端口 `in0` 连到 DDR bank 1 的 AXI 通路；
2. **运行期（主机）**：`cl::Buffer` 通过 `cl_mem_ext_ptr_t.flags = XCL_MEM_DDR_BANK1` 把缓冲区**放置**在 DDR bank 1；
3. **数据通路**：内核从 `in0` 发出的读请求只会走 bank 1 的通路，读到的就是主机放进 bank 1 的数据。

两端任何一端改了 bank 而另一端没改，通路与数据就「擦肩而过」。u1-l4 把这称为「主机与 ini 的手工对齐」，本讲看清它的代码形态。

`cl_mem_ext_ptr_t` 是 Xilinx 对 OpenCL 的扩展结构体（声明于 `CL/cl_ext_xilinx.h`，host.cpp 在 [L10](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L10) 引入），三个字段：

| 字段 | 本工程取值 | 含义 |
| --- | --- | --- |
| `obj` | `read_source.data()` | 指向主机内存（配合 `CL_MEM_USE_HOST_PTR` 使用） |
| `param` | `0` | 附加参数（按 flag 语义使用，这里不用，置 0） |
| `flags` | `XCL_MEM_DDR_BANK1` 或 `n \| XCL_MEM_TOPOLOGY` | **bank 指定，本模块主角** |

flags 有两代写法，uBench 两种都用到了：

| 写法 | 出处 | 语义 |
| --- | --- | --- |
| `XCL_MEM_DDR_BANK1` 这类「bank 编号宏」 | DDR 版 [host.cpp:L119/L126](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L113-L128) | 旧式：直接给出 DDR bank 号（每个 bank 一个预定义宏，最多支持到 BANK3 左右） |
| `BANK_NAME(n)` 即 `n \| XCL_MEM_TOPOLOGY` | [host.cpp:L19-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28) | 新式「拓扑」写法：`n` 是内存拓扑图里的通道号，`XCL_MEM_TOPOLOGY` 表示按拓扑编号解释。U280 的 32 个 HBM 伪通道没有对应 32 个宏，只能用这种写法，于是主机顶部预铺了 `bank[32]` 常量表 |

#### 4.2.2 核心流程

每个 payload 档位下，缓冲创建与数据搬运的流程：

```
1. 分配对齐主机内存：vector<int, aligned_allocator<int>> read_source(dataSize)
2. 准备 NUM_KERNEL*NUM_PORT 个扩展指针 source_in_ext[i]：
     obj   = read_source.data()
     param = 0
     flags = bank 标志（DDR 版 XCL_MEM_DDR_BANK1 / HBM 版 bank[0]）
3. 创建 NUM_KERNEL*NUM_PORT 个 cl::Buffer：
     CL_MEM_READ_ONLY（内核只读）
     | CL_MEM_EXT_PTR_XILINX（启用扩展指针，即按 flags 定位 bank）
     | CL_MEM_USE_HOST_PTR（零拷贝：直接用对齐过的主机内存做后备）
4. enqueueMigrateMemObjects(..., 0)：把数据从主机搬入设备全局内存（0 = host→device）
5. q.finish()：等待全部搬运完成（此时尚未开始计时）
```

注意第 2、3 步的缓冲个数是 `NUM_KERNEL*NUM_PORT`：每个内核的每个端口各一个 Buffer。本工程 1×2=2 个，且两个 Buffer 的 `obj` 指向**同一块**主机内存——两个端口读同一份数据，对带宽测试完全够用。

#### 4.2.3 源码精读

**HBM bank 常量表。** [host.cpp:L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28)：

```cpp
#define MAX_HBM_BANKCOUNT 32
#define BANK_NAME(n) n | XCL_MEM_TOPOLOGY
const int bank[MAX_HBM_BANKCOUNT] = { BANK_NAME(0), ..., BANK_NAME(31) };
```

这段表在 DDR 版里其实**用不上**（DDR 版走 `XCL_MEM_DDR_BANK1` 宏），但两份 host.cpp 都带着它——它是为 HBM 平台准备的通用模板。HBM 版真正用它的地方见下。

**两处分支：诚实地说，它们没有差异。** [host.cpp:L113-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L113-L128)：

```cpp
if (xcl::is_emulation()) {
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = XCL_MEM_DDR_BANK1;   // 仿真分支
    }
}
else {
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = XCL_MEM_DDR_BANK1;   // 真机分支：与上面完全相同
    }
}
```

这是本讲最重要的「读码不轻信」时刻：**DDR 版两个分支逐字节相同**（都用 `XCL_MEM_DDR_BANK1`），HBM 版同样如此（[HBM host.cpp:L115-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L115-L128) 两个分支都用 `bank[0]`）。这个 if/else 很可能是从「仿真时 bank 无效、需不同处理」的旧模板演化来的残留骨架，实际没有分化。真正的 DDR/HBM 差异不在分支里，而在 flag 取值本身：

| 版本 | flags 取值 | 对应 ini 的 sp 行 |
| --- | --- | --- |
| DDR 版 | `XCL_MEM_DDR_BANK1`（[L119/L126](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L113-L128)） | [ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L3-L4) 的 `DDR[1]` |
| HBM 版 | `bank[0]` 即 `0 | XCL_MEM_TOPOLOGY`（[HBM host.cpp:L119/L126](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L115-L128)） | [ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L3-L4) 的 `HBM[0]` |

两行对照就能记住规律：**ini 里 `sp` 指向哪个通道（编号 N），主机 flags 就写哪个编号（`XCL_MEM_DDR_BANKN` 或 `bank[N]`）**。

**创建 Buffer 与搬运。** [host.cpp:L130-L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L130-L147)：

```cpp
OCL_CHECK(err, source_in_buffer[i] =
                cl::Buffer(context,
                           CL_MEM_READ_ONLY | CL_MEM_EXT_PTR_XILINX |
                               CL_MEM_USE_HOST_PTR,
                           sizeof(int) * dataSize,
                           &source_in_ext[i], &err));
OCL_CHECK(err, err = q.enqueueMigrateMemObjects({source_in_buffer[i]},
                                                0 /* 0 means from host*/));
```

三个标志各自的作用：

- `CL_MEM_READ_ONLY`：设备侧只读（读带宽测试的输入缓冲）；
- `CL_MEM_EXT_PTR_XILINX`：第五个参数按 `cl_mem_ext_ptr_t*` 解释，flags 生效——没有它 bank 绑定 silently 失效；
- `CL_MEM_USE_HOST_PTR`：零拷贝。前提是主机指针**页对齐（4096 字节）**，这正是 `read_source` 用 `aligned_allocator<int>`（[L107](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L107)，分配器实现在 [xcl2.hpp:L61-L76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L61-L76)，内部 `posix_memalign(&ptr, 4096, ...)`）的原因。xcl2 头文件的注释（[L52-L60](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L52-L60)）解释了不对齐的后果：运行时会自建一个中转缓冲，每次搬运多一次 memcpy。

`enqueueMigrateMemObjects` 的第二参数 `0` 表示方向为主机→设备（`CL_MIGRATE_MEM_OBJECT_HOST` 才是设备→主机）。所有搬运都在计时窗口**之外**（`q.finish()` 在 [L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L147)，计时从 [L152](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L152) 才开始），保证测的只是内核执行。

#### 4.2.4 代码实践

**实践目标**：写出「任意端口数通用的 bank 绑定」工具函数，替换掉写死循环次数的重复代码。

**操作步骤**：

1. 阅读并确认现状：[host.cpp:L110-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L110-L128) 中分支内外两个循环体完全一致，且 flags 值与 [ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L3-L4) 的 `DDR[1]` 对应。
2. 在本地副本中新增如下函数（**示例代码**，非仓库原有）：

```cpp
// 示例代码：为 total = NUM_KERNEL*NUM_PORT 个端口生成 bank 绑定的扩展指针。
// bank_flags[k] 是第 k 个缓冲要落入的内存通道标志，
// DDR 平台传 XCL_MEM_DDR_BANK0/1/2/3，HBM 平台传 bank[0..31]。
std::vector<cl_mem_ext_ptr_t> make_ext_ptrs(
        int* host_ptr,
        const std::vector<unsigned int>& bank_flags) {
    std::vector<cl_mem_ext_ptr_t> ext(bank_flags.size());
    for (size_t k = 0; k < bank_flags.size(); k++) {
        ext[k].obj   = host_ptr;        // 零拷贝后备内存
        ext[k].param = 0;               // 不使用附加参数
        ext[k].flags = bank_flags[k];   // bank / 拓扑编号
    }
    return ext;
}
```

3. 用它替换原来的 if/else 两大段（保持行为不变）：

```cpp
// 示例代码：2 端口都绑 DDR bank1，与 ubench.ini 的 sp=...:DDR[1] 对齐
std::vector<unsigned int> flags(NUM_KERNEL * NUM_PORT, XCL_MEM_DDR_BANK1);
auto source_in_ext = make_ext_ptrs(read_source.data(), flags);
```

4. 进阶改法：让端口 0、1 分别绑不同 bank——把 `flags` 改为 `{XCL_MEM_DDR_BANK0, XCL_MEM_DDR_BANK1}`，**同时**把 [ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L3-L4) 改成 `in0:DDR[0]`、`in1:DDR[1]`，两端同步。

**需要观察的现象**：改动后程序行为不变（同一 payload 打印的带宽量级一致）；进阶改法在真机 U200 上可能测出**更高**总带宽（两端口分摊在两条独立 DDR 通道上）。sw_emu 下 bank 差异不体现物理效果。**待本地验证**（需要 U200 真机）。

**预期结果**：体会「主机 flags 向量 ↔ ini sp 行」必须逐端口对齐的约束；`NUM_PORT` 扩到 4 时只需改 `NUM_PORT` 宏和 `flags` 向量，不再复制粘贴循环。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `read_source` 必须用 `aligned_allocator` 而不是普通 `std::vector<int>`？
**答案**：因为 Buffer 用了 `CL_MEM_USE_HOST_PTR` 零拷贝模式，运行时只有在该主机指针页对齐（4096B）时才会直接拿它当设备缓冲后备；不对齐时运行时自建中转缓冲、每次搬运多一次 memcpy（[xcl2.hpp:L52-L60](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L52-L60) 注释）。`aligned_allocator` 用 `posix_memalign(&ptr, 4096, ...)` 保证对齐（[xcl2.hpp:L65-L71](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L65-L71)）。

**练习 2**：把 HBM 版主机的 `bank[0]` 改成 `bank[1]`，但 ini 不动，会发生什么？
**答案**：ini 仍把 `in0/in1` 连到 `HBM[0]`（[HBM ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L3-L4)），而数据被放进 HBM bank 1：内核端口的连线和数据实际位置不匹配。典型结果要么是运行时报内存拓扑校验错误，要么数据通路落空——正确做法是同时把 sp 行改为 `HBM[1]`。

**练习 3**：`CL_MEM_EXT_PTR_XILINX` 标志漏写会发生什么？
**答案**：`cl::Buffer` 构造函数的第五个指针参数将不按 `cl_mem_ext_ptr_t*` 解释，`flags` 里的 bank 信息失效，缓冲由运行体默认分配（通常落到 bank0），与 ini 的 sp 连线错位。它就是「扩展指针生效」的开关。

### 4.3 内核启动

#### 4.3.1 概念说明

缓冲就位后，最后三步是「设参 → 启动 → 等完成」：

- **setArg**：主机把每个内核参数写进设备的 `s_axilite control` 寄存器组。回顾内核签名 [krnl_ubench.cpp:L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4)：`krnl_ubench(in0, in1, size)` 三个形参对应 arg0/arg1/arg2。`m_axi` 参数（`offset=slave`）写入的是**基地址寄存器**——Buffer 在设备内存里的起始地址；标量参数 `size` 写入普通寄存器。
- **enqueueTask**：向命令队列提交「启动这个 Kernel 对象绑定的 CU」。比通用的 `enqueueNDRangeKernel` 简单，因为 HLS 内核整体是单一任务（work size 恒为 1）。
- **finish**：阻塞主机直到队列里所有命令完成。`enqueueTask` 是异步的——返回只代表命令入队，内核可能还没跑完，必须 `finish`（或读事件）才能拿到完整执行时间。

#### 4.3.2 核心流程

```
dataSize = dataSize / WIDTH_FACTOR        # 单位换算：int 个数 → 512bit 宽字个数
for i in 0..NUM_KERNEL-1:                 # 逐内核
    for j in 0..NUM_PORT-1:
        setArg(j, source_in_buffer[i*NUM_PORT+j])   # 数据端口参数（基地址）
    setArg(j=NUM_PORT, dataSize)                    # size 标量参数
    enqueueTask(cmpt_krnl[i])                       # 启动（异步）
q.finish()                                # 等所有内核结束
<计时窗口在 enqueue 前后：见下>
```

单位换算是 setArg 前的关键一步：内核里 `in0[j]` 的 `j` 按 `INTERFACE_WIDTH`（512bit = 16 个 int）计数，所以主机要把「int 个数」除以 `WIDTH_FACTOR`（=16，见 [krnl_config.h:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L6)）再传给内核。这条「主机按 int 分配、内核按宽字消费」的换算约定在改 `DWIDTH` 时必须跟着变（u3-l2 会展开）。

#### 4.3.3 源码精读

**计时窗口包住设参与启动。** [host.cpp:L149-L166](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L149-L166)：

```cpp
auto kernel_start = std::chrono::high_resolution_clock::now();   // L152 开始计时

dataSize = dataSize / WIDTH_FACTOR;                              // L156 单位换算
int i, j = 0;
for (i = 0; i < NUM_KERNEL; i++) {
    for (j = 0; j < NUM_PORT; j++) {
        OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, source_in_buffer[i*NUM_PORT+j])); // L160
    }
    OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));       // L162 j==NUM_PORT==2
    OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));            // L164
}
q.finish();                                                      // L166 等内核结束
```

四个要点：

1. **参数索引的巧用**：内层 `for (j = 0; j < NUM_PORT; j++)` 结束时 `j == NUM_PORT == 2`，紧接着 [L162](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L162) 直接用这个 `j` 设第 3 个参数（`size`）。参数顺序因此**隐式依赖内核签名的形参顺序**（先端口指针、最后标量）——改内核签名时这段必须联动，是隐蔽的耦合点。
2. **缓冲索引布局**：`source_in_buffer[i*NUM_PORT+j]` 按「内核 i 的端口 j」行优先展开，与 4.2 中 ext 指针的填写顺序一一对应。
3. **计时含启动开销**：`kernel_start` 在 `setArg` 之前、`kernel_end` 在 `q.finish()` 之后（[L168-L170](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L168-L170)），所以测得时间除内核执行外还包含 setArg、命令下发、调度延迟。这是主机计时法的固有系统误差，量化的批判放在 u2-l3 与 u7-l3。
4. **带宽换算**（预告 u2-l3）：[L172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 的 `payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT` 中，常数 \(0.000010000 = \text{NUM\_ITERATIONS}/10^9 = 10^4/10^9\)：每端口实际搬运 \( \text{payload} \times 4\,\text{B} \times \text{NUM\_ITERATIONS} \)（payload 个 int × 1 万次外层循环），除以时间化为 GB/s；`NUM_ITERATIONS=10000` 就藏在这个系数里（[krnl_config.h:L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7)）。

**一个阅读陷阱。** [L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173)：

```cpp
std::cout << "Payload Size: " << i*4/(1024.0*1024.0) << "MB - Bandwidth = " << ...
```

这里的 `i` 是 [L157](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157) 声明、setArg 循环复用的变量，循环结束后恒为 `NUM_KERNEL = 1`——打印出的 "Payload Size" 并非当前 payload 的字节数（想打印的应是 `payload*4/(1024.0*1024.0)`）。它只影响日志标签，不影响 `bw_result` 的计算；但读代码时不要被这行误导，也不要照抄这种变量复用风格。

#### 4.3.4 代码实践

**实践目标**：把「参数设置」从启动循环里独立出来，成为与 4.2 工具函数配套的可扩展设参函数，并验证参数序号与内核签名对齐。

**操作步骤**：

1. 抄录内核签名（[krnl_ubench.cpp:L4-L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L12)），列出形参顺序：`in0`(arg0)、`in1`(arg1)、`size`(arg2)、`return`(隐含)。
2. 在本地副本中新增（**示例代码**，非仓库原有）：

```cpp
// 示例代码：对任意 NUM_KERNEL/NUM_PORT 通用。
// 端口缓冲按行优先填入 arg0..arg(NUM_PORT-1)，size 填 arg NUM_PORT。
void set_args_and_launch(cl::CommandQueue& q,
                         std::vector<cl::Kernel>& kernels,
                         const std::vector<cl::Buffer>& buffers,
                         int size_in_wide_words) {
    cl_int err;
    const int nport = kernels.size() > 0 ? (int)(buffers.size() / kernels.size()) : 0;
    for (size_t k = 0; k < kernels.size(); k++) {
        for (int j = 0; j < nport; j++) {
            OCL_CHECK(err, err = kernels[k].setArg(j, buffers[k*nport + j]));
        }
        OCL_CHECK(err, err = kernels[k].setArg(nport, size_in_wide_words));
        OCL_CHECK(err, err = q.enqueueTask(kernels[k]));
    }
    q.finish();
}
```

3. 用 `set_args_and_launch(q, cmpt_krnl, source_in_buffer, dataSize / WIDTH_FACTOR)` 替换 [L155-L166](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L155-L166) 的设参启动段（注意 `dataSize/WIDTH_FACTOR` 要在计时起点之前算好，保持计时窗口一致）。
4. 修复 4.3.3 指出的打印陷阱：把 [L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) 的 `i*4` 改为 `payload*4`。
5. 有 Vitis 环境则 `make check TARGET=sw_emu` 编译运行验证；无则静态走查每个 `setArg` 的实参与内核形参类型/顺序一致。

**需要观察的现象**：重构后输出格式不变（修复打印后 Payload Size 档位依次显示约 0.001、0.002、…、1 MB，即 256→262144 个 int 的 4 倍字节数）。**待本地验证**（sw_emu 只验证功能链路，带宽数值无物理意义，见 u1-l3）。

**预期结果**：得到一组可复用的主机辅助函数（`make_ext_ptrs` + `set_args_and_launch`），后续 u3-l2 扩端口数实验时只需改宏，不用动设参逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `enqueueTask` 之后必须有 `q.finish()` 才能停表？
**答案**：`enqueueTask` 是异步提交——返回时内核可能尚未开始执行。`q.finish()`（[host.cpp:L166](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L166)）阻塞到队列中所有命令（含内核执行）完成；停表语句在其后（[L168](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L168)），时间差才是（近似的）内核执行时间。

**练习 2**：传给内核的 `size` 为什么是 `dataSize / WIDTH_FACTOR`？`WIDTH_FACTOR` 从哪来？
**答案**：主机缓冲按 `int`（4 字节）分配和迁移，内核端口的元素类型是 `INTERFACE_WIDTH = ap_uint<512>`，一次消费 16 个 int；`WIDTH_FACTOR = DWIDTH/32 = 16`（[krnl_config.h:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L6)）。内核循环 `for j < size` 按宽字计数（[krnl_ubench.cpp:L20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L20)），所以要把 int 个数除以 16。

**练习 3**：内核的 `return` 也映射到一个 `s_axilite` 端口（[krnl_ubench.cpp:L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L12)），主机需要为它 setArg 吗？
**答案**：不需要。`return` 是 HLS 的内核完成/中断握手信号，由运行时与调度器自动管理；主机 `setArg` 只针对显式形参（in0/in1/size）。`offset=slave` 的 m_axi 端口则不同——它们的「基地址」就是普通标量参数，由 `setArg(buffer)` 写入。

## 5. 综合实践

**任务：产出一份「带注释的 host.cpp 逐段说明 + 通用 bank 绑定工具」，并完成 DDR→双 bank 改造推演。** 这是本讲实践任务的完整版，把三个模块串起来。

1. **逐段注释**。对 [host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) 按下表分段，每段写 2~3 句中文注释（放进你自己的笔记或本地副本）：

   | 行区间 | 段落 | 注释必须回答的问题 |
   | --- | --- | --- |
   | [L10-L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L10-L16) | 头文件与规模宏 | `CL/cl_ext_xilinx.h` 带来什么？`NUM_KERNEL*NUM_PORT` 的乘积含义？ |
   | [L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28) | HBM bank 表 | `BANK_NAME(n)` 宏的位运算含义？DDR 版为何用不上它？ |
   | [L43-L50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L43-L50) | xcl2 三件套 | 三个函数各自的失败行为？ |
   | [L52-L93](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L52-L93) | 设备探测循环 | CU 名 `krnl_ubench:{krnl_ubench_1}` 与 ini 哪一行对应？两个队列标志的作用？ |
   | [L100-L108](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100-L108) | payload 扫描与主机缓冲 | 仿真下为何压到 256？为何用 aligned_allocator？ |
   | [L110-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L110-L128) | **两处分支** | **重点：逐字节比对后如实标注「两分支 flags 完全相同（都是 XCL_MEM_DDR_BANK1），差异为历史遗留空骨架」**，并与 HBM 版（两分支都是 `bank[0]`）对照 |
   | [L130-L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L130-L147) | Buffer 与搬运 | 三个 CL_MEM 标志各自作用？搬运为何不计入时间？ |
   | [L149-L174](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L149-L174) | 设参、启动、计时 | setArg 索引如何与内核签名对齐？L173 打印陷阱在哪？ |

2. **实现工具函数**：把 4.2.4 的 `make_ext_ptrs` 与 4.3.4 的 `set_args_and_launch` 落地到本地副本，替换对应段落，确认 `NUM_PORT` 改为 4 时（配合内核改造，见 u3-l2）主机侧只需改宏与 flags 向量。
3. **双 bank 改造推演**：写出「in0→DDR[0]、in1→DDR[1]」方案需要同步修改的完整文件清单与每一处的新值（`ubench.ini` 两行 sp、`host.cpp` 的 flags 向量），并回答：为什么理论上双 bank 比单 bank（现状：两端口共用 DDR[1]）更能测出 U200 的聚合带宽？（提示：单 bank 时两端口共享同一控制器的命令队列；双 bank 走两条独立通道。真机结果**待本地验证**。）

## 6. 本讲小结

- uBench 所有主机程序共享一套 OpenCL/XRT 骨架：`xcl::get_xil_devices` 枚举 Xilinx 加速器 → 逐卡尝试 `cl::Program` 灌 xclbin → 按 `内核名:{内核名_N}` 创建绑定 CU 的 Kernel 对象，CU 名必须与 `ubench.ini` 的 `nk=` 一致。
- `cl_mem_ext_ptr_t`（`obj/param/flags`）+ `CL_MEM_EXT_PTR_XILINX` 是把缓冲放进指定内存 bank 的机制；DDR 版用 `XCL_MEM_DDR_BANK1` 宏，HBM 版用 `bank[n] = n | XCL_MEM_TOPOLOGY` 拓扑写法，且 **flags 必须与 ini 的 `sp=` 行逐端口对应**——这是全仓库最重要的「两端对齐」约定。
- 本工程的 `is_emulation()/else` 两处 bank 分支实际**完全相同**（DDR、HBM 版皆然），是模板演化遗留的空骨架；读码要如实标注，不要脑补差异。
- `CL_MEM_USE_HOST_PTR` 零拷贝依赖 `aligned_allocator` 的 4096 字节页对齐，否则运行时暗中多一次 memcpy。
- `setArg` 按内核形参顺序位置编号（端口基地址在前、`size` 在后，`size` 需先除以 `WIDTH_FACTOR` 换算成宽字个数）；`enqueueTask` 异步启动、`q.finish()` 兜底；乱序队列标志是多 CU 并发测带宽的前提。
- 计时窗口包含 setArg 与调度开销，且 [L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) 的 "Payload Size" 打印复用了循环变量 `i`，标签值是错的——两个已知测量瑕疵，u2-l3/u7-l3 展开。

## 7. 下一步学习建议

下一讲 **u2-l3 测量方法学**将顺着本讲留下的两条线索深入：一是 payload 256→262144 倍增扫描与带宽公式 `payload*4*0.000010000/t*NUM_KERNEL*NUM_PORT` 的完整单位推导（本讲已给出 \(0.000010000 = \text{NUM\_ITERATIONS}/10^9\) 的速证）；二是本讲指出的主机计时误差（启动开销计入、打印标签错误）的系统分析，以及改用 `cl_event`（队列已开 `CL_QUEUE_PROFILING_ENABLE`）做内核级计时的方案。

源码层面建议接着做两件事：

1. 用 `diff` 对比 DDR 与 HBM 两份 host.cpp（路径见源码地图），亲手验证「除 bank 标志与个别缩进外完全一致」，为 u3-l3 的连接配置专题建立直觉；
2. 通读 [xcl2.hpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp) 全文（仅约 100 行），注意尚未用到的 `xcl::Stream` 类与 `is_xpr_device`——它们在本仓库微基准中未出现，但属于 Xilinx 示例生态的通用件（u7-l1 会盘点 common/ 全目录）。
