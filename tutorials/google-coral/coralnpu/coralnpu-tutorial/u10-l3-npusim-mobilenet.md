# npusim 与 MobileNet 端到端

## 1. 本讲目标

本讲是「ML 工作负载与端到端运行」单元的收口。前面几讲我们分别学会了：用 RVV intrinsics 写向量程序（u10-l1）、把 TFLite Micro 的 int8 算子重写成向量后端友好的内核（u10-l2）、以及在 Verilator 仿真器上跑一个裸机二进制（u2-l3）。本讲把这三条线拧到一起，回答一个最实际的问题：

> **如何在没有真实芯片的情况下，把一个完整的 MobileNet 模型「端到端」地跑在 CoralNPU 上，并拿到推理结果与周期数？**

学完本讲你应该能够：

1. 说清 npusim 的「双体架构」：一个 Python 宿主脚本 + 一个 C++ 设备二进制，二者通过**共享内存里的命名符号**通信。
2. 理解 `CoralNPUV2Simulator` 这层 Python/C++ 仿真封装的作用、它如何搭建 highmem 地址空间、以及它暴露的关键接口。
3. 掌握「符号查表」这一贯穿始终的软硬协作契约：`extern "C"` 防止名字改编、pyelftools 从 ELF 符号表读地址。
4. 跟踪一条端到端推理流程：编译 → 定位符号 → 写输入 → 加载运行 → 读输出 → 查状态。
5. 把 npusim 与 u2-l3 讲过的 Verilator 仿真器区分开来，理解它为何更适合跑大模型。

## 2. 前置知识

本讲默认你已经具备以下认知（若生疏可回看对应讲义）：

- **CoralNPU 是 RV32 的裸机协处理器**：它没有操作系统、没有 `printf`/`scanf`，输入输出全靠「主机与内核共享 DTCM」这一模型（u2-l2、u2-l4）。
- **程序经 `coralnpu_v2_binary` 规则编译成 `.elf`**：该规则用平台切换切到 RISC-V 裸机平台、自动挂载 CRT、按 ITCM/DTCM 大小生成链接脚本，产出 `.elf/.bin/.vmem`（u2-l1、u2-l2）。
- **`__attribute__((section(".data")))` 把变量钉进确定的可加载区**：这是主机能「按名查址、按址读写」的前提（u2-l2）。
- **TFLite Micro（TFLM）算子已被改写**：`sw/opt/litert-micro` 下的卷积/全连接等用 RVV 重写，由标量核驱动、向量后端执行（u10-l2）。
- **Verilator 仿真器 `core_mini_axi_sim`**：用 SystemC 体系、靠 AXI slave 灌 ELF、靠 mailbox/`--instr_trace` 看输出（u2-l3）。

补充两个本讲会用到的术语：

- **npusim**：CoralNPU 的「功能性仿真器」（functional simulator）。它不逐拍模拟 RTL，而是用一个 C++ ISA 级模型（mpact 框架的 `CoralNPUSimulator`）执行 RISC-V 指令，因此**速度远快于 Verilator**，适合跑完整的大模型推理（数百万条指令），代价是看不到微架构级的时序细节。
- **pybind11**：一个把 C++ 类/函数暴露成 Python 模块的工具。npusim 的核心就是把 C++ 仿真器经 pybind11 包成一个 `coralnpu_v2_sim_pybind` 模块，再由 Python 脚本驱动。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [doc/tutorials/npusim_mobilenet_tutorial.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md) | 官方教程，讲清「Python 宿主 + C++ 设备」的双体模型与 5 步流水线，是本讲的主线。 |
| [sw/coralnpu_sim/coralnpu_v2_sim_utils.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py) | `CoralNPUV2Simulator` 包装类：构造 highmem 内存区、加载 ELF、读写内存/寄存器、查符号。是 Python 侧的主角。 |
| [tests/npusim_examples/npusim_run_mobilenet.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py) | 端到端示例的「宿主脚本」：定位 ELF、写随机输入、运行、打印 Top-1 与周期数。 |
| [tests/npusim_examples/run_full_mobilenet_v1.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc) | 端到端示例的「设备二进制」：搭 TFLM 解释器、跑 MobileNet、把结果拷回共享缓冲。 |
| [sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc) | pybind11 绑定层，把 C++ 仿真器的方法暴露成 Python 可调用接口。 |
| [tests/npusim_examples/BUILD](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/BUILD) | 定义 `npusim_run_mobilenet`（py_binary）与 `run_full_mobilenet_v1_binary`（coralnpu_v2_binary，开启 highmem）。 |
| [toolchain/coralnpu_tcm.ld.tpl](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl) 与 [rules/linker.bzl](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/linker.bzl) | 链接脚本模板与 highmem 起址规则，决定 `.data`/`.extdata` 各自落在哪段仿真地址空间。 |

## 4. 核心概念与源码讲解

### 4.1 双体架构：Python 宿主 + C++ 设备

#### 4.1.1 概念说明

跑一个 TFLite Micro 模型（如 MobileNet）需要两类完全不同的逻辑：

- **设备侧逻辑（device）**：真正「在 CoralNPU 上」执行的代码——搭建 TFLM 解释器、加载模型、`Invoke()` 跑推理。它必须被编译成 CoralNPU 能执行的 RV32 `.elf`。
- **宿主侧逻辑（host）**：在开发机（x86 PC）上执行的 Python 脚本——控制仿真器、注入输入图像、等待执行结束、读取推理结果、打印统计。它不会跑在 CoralNPU 上。

难点在于：这两侧跑在不同的机器（一个是被仿真的 32 位 RISC-V 核，一个是 PC 上的 Python 进程），它们之间**没有操作系统、没有 RPC**，怎么交换数据？

CoralNPU 的答案就是贯穿全手册的那个模型：**共享内存 + 命名符号**。设备代码把要交换的缓冲区声明成带固定名字的全局变量，宿主脚本解析 ELF 拿到这些名字对应的地址，然后直接「按址读写」仿真器的内存空间。这样输入输出就像两条传送带：宿主往 `inference_input` 地址写、设备从中读；设备往 `inference_output` 地址写、宿主从中读。

#### 4.1.2 核心流程

官方教程把这条流水线总结为 5 步：

```text
1. Compile  : run_full_mobilenet_v1.cc  ──coralnpu_v2_binary──▶  .elf（含未改编的符号 + 静态地址）
2. Locate   : Python npusim 解析 .elf，得到 inference_input / inference_output 的地址
3. Write    : Python 把（随机）输入数据写到 inference_input 地址
4. Run      : Python 启动仿真器；C++ 把 input 拷进模型、计算、把结果拷到 inference_output
5. Read     : 仿真结束；Python 从 inference_output 地址读出结果并校验
```

关键点在于第 1 步产出的 `.elf` **同时承载了「指令」和「符号地图」**：指令供核执行，符号地图供宿主定位缓冲区。这两者缺一不可。

#### 4.1.3 源码精读

教程开篇一句话点明了双体模型，两个组件分别叫 host 与 device：

[doc/tutorials/npusim_mobilenet_tutorial.md:7-9](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md#L7-L9) — 指出仿真一个 TFLM 模型需要两个组件：Python 宿主脚本（控制仿真器、注入输入、提取输出）与 C++ 设备二进制（搭 TFLM 解释器跑推理）。

这两个组件在 BUILD 里就是两个不同类型的 Bazel 目标——一个是 `py_binary`（宿主），一个是 `coralnpu_v2_binary`（设备）：

[tests/npusim_examples/BUILD:18-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/BUILD#L18-L29) — `py_binary(name="npusim_run_mobilenet")` 是宿主，它的 `data` 里挂着设备二进制 `:run_full_mobilenet_v1_binary`（这样 bazel run 时能用 runfiles 找到 .elf），`deps` 里挂着仿真封装库与 numpy。

[tests/npusim_examples/BUILD:31-50](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/BUILD#L31-L50) — `coralnpu_v2_binary(name="run_full_mobilenet_v1_binary")` 是设备，注意它把 `itcm_size_kbytes` 与 `dtcm_size_kbytes` 都设成了 `1024`（默认是 8/32），并开启 `semihosting = True`。这个放大 TCM 的设置正是「highmem」配置，4.2 节会讲为什么必须放大。

#### 4.1.4 代码实践

**目标**：用眼睛把「双体」对应到具体文件，并理解它们的产物。

**步骤**：

1. 打开 [tests/npusim_examples/BUILD](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/BUILD)，分别定位 `py_binary`（宿主）与 `coralnpu_v2_binary`（设备）两个目标。
2. 确认宿主的 `data` 字段引用了设备二进制；这是「宿主能在运行时找到设备 .elf」的关键。
3. 读官方教程的 Summary 一节，把 5 步流水线抄下来。

**需要观察的现象**：宿主目标和设备目标是**两个独立的可执行实体**，分别用不同 Bazel 规则、产出不同产物（一个 `.py` 入口、一个 `.elf`），唯一的纽带是 `data` 引用与共享符号名。

**预期结果**：你能用一句话讲清「bazel run tests/npusim_examples:npusim_run_mobilenet 这个 Python 进程，会在内部启动仿真器去执行另一个叫 run_full_mobilenet_v1_binary 的 RISC-V 程序」。**待本地验证**：实际执行该命令观察是否真的打印出周期数与 Top-1。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把宿主脚本也用 `coralnpu_v2_binary` 编译？
**答案**：因为宿主脚本依赖 numpy、bazel runfiles、pyelftools 等 Python 生态，必须跑在 PC 的 Python 解释器上；而 `coralnpu_v2_binary` 产出的是 RV32 裸机 `.elf`，根本没有操作系统和 Python 运行时。两者运行环境完全不同，故用 `py_binary` 与 `coralnpu_v2_binary` 两种规则分别处理。

**练习 2**：宿主和设备之间没有 RPC，靠什么交换数据？
**答案**：靠「仿真器内存空间里的命名符号」。设备把缓冲区声明成带名字的全局变量并钉进确定段，宿主解析 ELF 拿到这些符号的地址，然后直接读写仿真器对应地址的内存，等价于一条共享内存传送带。

### 4.2 CoralNPUV2Simulator：仿真封装与 highmem 地址空间

#### 4.2.1 概念说明

`CoralNPUV2Simulator` 是 Python 侧的主角，定义在 [sw/coralnpu_sim/coralnpu_v2_sim_utils.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py)。它本身不实现仿真，而是「**C++ 仿真器的薄包装**」——类注释直说 `Wrapper for CoralNPUV2SimulatorPy providing helper methods`。它解决三件事：

1. **配置仿真器**：构造时设定一组内存区（ITCM/DTCM/EXTMEM/DDR）、是否在 ebreak 退出、是否启用半主机（semihosting HTIF）。
2. **简化调用**：把 C++ 侧稍显底层的方法（如 `WriteMemory` 要传指针和长度）包装成 Pythonic 的接口（如 `write_memory` 接受 numpy 数组）。
3. **查符号**：提供 `get_elf_entry_and_symbol` 用 pyelftools 从 ELF 读入口地址与符号地址。

底层 C++ 仿真器来自 mpact 框架（`@coralnpu_mpact//sim:coralnpu_simulator`），经 [sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc) 用 pybind11 暴露成模块 `coralnpu_v2_sim_pybind`。Python 包装类与 C++ pybind 类的关系是：`CoralNPUV2Simulator`（utils.py）→ `coralnpu_v2_sim_pybind.CoralNPUSimulatorPy`（pybind 胶水）→ `coralnpu::sim::CoralNPUSimulator`（mpact C++ 模型）。

**为什么需要 highmem？** 默认 CoralNPU 的 ITCM 只有 8KB、DTCM 32KB——这点空间根本装不下 MobileNet 的模型权重（一个 int8 量化模型动辄几百 KB 到几 MB）和 TFLM 的工作区（`tensor_arena` 在本例里被设成了 **4MB**）。所以跑大模型时必须切到「highmem」配置：把 ITCM/DTCM 各放大到 1MB，并启用 EXTMEM（4MB @ `0x20000000`）来放 4MB 的 arena，再启用 DDR 放更大的负载。仿真器侧用 `highmem_ld=True` 搭建对应的地址空间，链接脚本侧用「非默认 TCM 大小」触发 highmem 的 DTCM 起址。两侧必须匹配，否则设备访问的地址在仿真器里不存在。

#### 4.2.2 核心流程

构造一个 highmem 仿真器的流程：

```text
CoralNPUV2Simulator(highmem_ld=True, exit_on_ebreak=True)
  ├── 建一个 CoralNPUSimulatorOptions
  ├── highmem_ld=True 时：
  │     ├── itcm_region : 0x0          , 1MB  , RWX
  │     ├── dtcm_region : 0x00100000   , 1MB  , RW      ← highmem 的 DTCM 起址
  │     ├── extmem_region: 0x20000000  , 4MB  , RW      ← 放 4MB tensor_arena
  │     ├── ddr_region  : 0x80000000   ,128MB , RWX
  │     ├── initial_misa_value = 0x40201120  ← RV32 + I/M/F/V
  │     └── options.memory_regions = [上面四块]
  ├── exit_on_ebreak=True 时：options.exit_on_ebreak = True
  └── self.sim = CoralNPUSimulatorPy(options)   ← 真正构造 C++ 仿真器
```

之后典型的「跑一段程序」生命周期是：`load_program(elf) → run()（非阻塞）→ wait()（阻塞到结束）`，期间用 `write_memory`/`read_memory` 注入与提取数据，用 `get_cycle_count` 拿周期数。

#### 4.2.3 源码精读

构造函数集中体现 highmem 地址空间的搭建：

[sw/coralnpu_sim/coralnpu_v2_sim_utils.py:23-36](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py#L23-L36) — 构造函数。`highmem_ld=True` 分支里依次创建 ITCM/DTCM/EXTMEM/DDR 四个内存区，设 `initial_misa_value`，把它们装进 `options.memory_regions`；`exit_on_ebreak=True` 分支设退出开关；最后用 `CoralNPUSimulatorPy(self.options)` 真正造出 C++ 仿真器实例。

四个区与 misa 的取值，对照表如下（请与下文链接脚本互相印证）：

| 区 | 起址 | 长度 | 权限 | 用途 |
| --- | --- | --- | --- | --- |
| itcm_region | `0x0` | `0x100000`(1MB) | RWX | 代码 `.text`、只读 `.rodata`（含模型权重） |
| dtcm_region | `0x100000` | `0x100000`(1MB) | RW | `.data`（`inference_input/output/status`）、`.bss`、栈、堆 |
| extmem_region | `0x20000000` | `0x400000`(4MB) | RW | `.extdata`（4MB `tensor_arena`） |
| ddr_region | `0x80000000` | `0x8000000`(128MB) | RWX | 更大负载 `.ddr_data` |

`initial_misa_value = 0x40201120` 是机器 ISA（`misa`）寄存器初值，编码了仿真器要「认」的指令集。读者可自行解码：最高两位 `MXL`（bits[31:30]）为 `01`，表示 32 位；扩展位（bits[25:0] = `0x201120`）置位的字母为 I(bit8)/F(bit5)/M(bit12)/V(bit21)，即 **RV32 + I/F/M/V**，与项目的 `rv32imf_zve32x` 定位一致（V 在 32 位嵌入式里对应 Zve32x）。

这些内存区的权限枚举由 pybind 层导出，`kReadWriteExecute` 等就是在那里定义的：

[sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc:208-216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc#L208-L216) — 把 C++ 的 `MemoryPermission` 枚举（kNone/kRead/kWrite/kExecute/…/kReadWriteExecute）导出成 Python 可用常量，供上面 `_create_memory_region` 使用。

读写内存的 Python 包装做了类型检查并把 numpy 数组转成 C++ 期望的 `uint8` 视图：

[sw/coralnpu_sim/coralnpu_v2_sim_utils.py:99-105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py#L99-L105) — `write_memory(address, data)`：强制 `data` 必须是 numpy 数组（否则 `TypeError`），非 `uint8` 则 `.view(np.uint8)`，再调底层 `WriteMemory(address, data, len(data))`。这正是 4.1 节「按址写传送带」的落点。

其底层 C++ 实现用 pybind11 的 `buffer_info` 拿到 numpy 数组的裸指针，再交给 mpact 仿真器写入，并校验写入长度：

[sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc:143-158](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_pybind.cc#L143-L158) — `WriteMemory`：`input_buffer.request()` 取出 buffer 指针，调 `sim_.WriteMemory(address, ptr, length)`，若返回的已写字节数不等于请求长度则记 ERROR 日志。

链接脚本一侧，highmem 的 DTCM 起址由「TCM 大小是否偏离默认值」自动触发：

[rules/linker.bzl:26-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/linker.bzl#L26-L30) — 默认 `dtcm_origin = "0x00010000"`；只要 `itcm_size_kbytes != 8` 或 `dtcm_size_kbytes != 32`，就把起址切到 highmem 的 `"0x00100000"`。本例 BUILD 里两个都设了 1024，故 DTCM 落在 `0x00100000`，与上表 dtcm_region 完全吻合。

链接脚本模板则把这些区段与 section 对应起来：

[toolchain/coralnpu_tcm.ld.tpl:5-10](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L5-L10) — `MEMORY` 块声明了 ITCM/DTCM/EXTMEM/DDR 四个区。注意 EXTMEM 固定 `ORIGIN = 0x20000000, LENGTH = 4096K`，DDR 固定 `0x80000000`，与仿真器 highmem 区一致。

[toolchain/coralnpu_tcm.ld.tpl:125-132](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L125-L132) — `.extdata` 段 `> EXTMEM`，即所有 `__attribute__((section(".extdata")))` 的变量都落到 `0x20000000` 起的 EXTMEM。4MB 的 `tensor_arena` 就走这条路。

#### 4.2.4 代码实践

**目标**：验证「仿真器 highmem 地址空间」与「链接脚本 section 归属」两侧一致，并定位驱动仿真的关键接口。

**步骤**：

1. 在 [coralnpu_v2_sim_utils.py:27-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py#L27-L33) 抄下四个内存区的起址与长度。
2. 在 [rules/linker.bzl:26-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/linker.bzl#L26-L30) 确认 highmem 的 DTCM 起址是 `0x00100000`，与仿真器 dtcm_region 的 `0x100000`（即 `0x00100000`）相等。
3. 在 [coralnpu_v2_sim_utils.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py) 里逐个记录驱动仿真的关键接口及其参数：`load_program(elf_path, entry_point)`、`run()`、`wait()`、`write_memory(addr, np_array)`、`read_memory(addr, length)`、`get_cycle_count()`、`get_elf_entry_and_symbol(filename, symbol_names)`。

**需要观察的现象**：仿真器四个区的起址/长度，应当与链接脚本 `MEMORY` 块里 ITCM/DTCM/EXTMEM/DDR 的 `ORIGIN`/`LENGTH` 逐一对应；任何一个对不上，设备访问就会落到仿真器「不存在的地址」而失败。

**预期结果**：你能填出一张「section → 链接区 → 仿真器区 → 实际落点」的对照表（例如 `.extdata` → EXTMEM → `0x20000000`/4MB → `tensor_arena`）。**待本地验证**：实际构造一个 `CoralNPUV2Simulator(highmem_ld=True)` 并 `read_memory` 某个 `.extdata` 地址，确认不会报越界。

#### 4.2.5 小练习与答案

**练习 1**：如果 BUILD 里把 `dtcm_size_kbytes` 改回默认 32、但宿主脚本仍用 `highmem_ld=True`，会发生什么不一致？
**答案**：链接脚本一侧因 TCM 大小回到默认，DTCM 起址会变回 `0x00010000`（且 DTCM 只有 32KB）；而仿真器一侧 `highmem_ld=True` 仍把 DTCM 放在 `0x00100000`/1MB。于是设备代码里 `.data` 段（如 `inference_input`）按链接脚本落在 `0x00010000` 附近，宿主却从 `0x00100000` 附近读写，地址对不上；更糟的是 32KB DTCM 装不下 ~147KB 的 `inference_input`，链接就会溢出失败。

**练习 2**：`run()` 之后为什么还要调一次 `wait()`？
**答案**：因为 `run()` 是**非阻塞**的——它只是让仿真器在后台开始执行（参考 `coralnpu_v2_sim_test.py` 里 `test_halt` 的注释 "Run is non-blocking"）。`wait()` 才是阻塞调用，它会等到程序结束（命中 ebreak 或跑完）才返回，之后才能安全地 `read_memory` 读取结果。

### 4.3 ELF 符号契约：extern "C" 与符号查表

#### 4.3.1 概念说明

双体架构能工作的前提，是宿主**能从 `.elf` 里查出设备缓冲区的地址**。这依赖一个软硬协作的「符号契约」，由两端共同遵守：

- **设备端（C++）**：把要交换的变量声明在 `extern "C"` 块里。C++ 默认会对函数/变量名做「名字改编」（name mangling，比如把 `inference_input` 改成 `_Z16inference_input` 之类带类型信息的符号），导致 ELF 符号表里的名字面目全非。`extern "C"` 强制使用 C 语言链接规则，**禁止改编**，符号表里就是干净的 `"inference_input"`。
- **宿主端（Python）**：用 pyelftools 打开 `.elf`，读其符号表（`SHT_SYMTAB`），按名字查出每个符号的 `st_value`（即它在 RISC-V 地址空间里的地址），同时从 ELF 头读出 `e_entry`（程序入口地址）。

这条契约的本质是：**符号名是双方共同约定的「钥匙」，地址是双方共同信任的「锁」**。设备把缓冲区钉在固定段（`.data`/`.extdata`），链接器给它们分配静态地址，宿主按名查到地址后就能精确读写。

#### 4.3.2 核心流程

符号查表的流程：

```text
设备侧编译：
  extern "C" { int8_t inference_input[...] __attribute__((section(".data"))); }
    ── 链接器分配静态地址 ──▶  ELF 符号表：  inference_input  -> 0x00100000+x

宿主侧查表：
  get_elf_entry_and_symbol(elf_path, ['inference_input', ...])
    ├── open(elf_path, 'rb') → ELFFile
    ├── entry_point = elf_file.header['e_entry']        ← 程序入口 PC
    ├── symtab = 第一个 SHT_SYMTAB 段
    └── for name in names: symbol_map[name] = symtab[name].st_value
```

注意几个细节：入口地址来自 ELF 头的 `e_entry`（在 CoralNPU 上就是 `_start`，复位后从这里取指）；找不到的符号会被记成 `0`，宿主据此用 `if symbol_map.get('inference_input'):` 跳过空指针操作。

#### 4.3.3 源码精读

设备端把三个共享缓冲放进 `extern "C"`，并各自钉进特定段：

[tests/npusim_examples/run_full_mobilenet_v1.cc:51-60](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc#L51-L60) — `extern "C"` 块里声明 `inference_status`（初值 -1）、`inference_input[224*224*3]`（钉 `.data`）、`inference_output[5]`（钉 `.data`）、`tensor_arena[4MB]`（钉 `.extdata`）。`extern "C"` 保证符号不被 C++ 改编，宿主能按原名 `"inference_input"` 查址。

官方教程专门解释了为何要 `extern "C"`：

[doc/tutorials/npusim_mobilenet_tutorial.md:28-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md#L28-L30) — 把变量放进 `extern "C"` 是为了禁止 C++ 名字改编，让 Python 能在 ELF 里按原始名字（如 `"inference_input"`）查到地址。

宿主侧的查表实现集中在 `get_elf_entry_and_symbol`：

[sw/coralnpu_sim/coralnpu_v2_sim_utils.py:116-130](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py#L116-L130) — 打开 ELF，`entry_point = elf_file.header['e_entry']` 取入口；若有 `symbol_names`，则取第一个 `SHT_SYMTAB` 段，逐个 `get_symbol_by_name`，命中则记 `entry['st_value']`，未命中记 `0`；返回 `(entry_point, symbol_map)`。注意它依赖 `pyelftools`（BUILD 里 `coralnpu_v2_sim_utils_lib` 依赖 `requirement("pyelftools")`）。

宿主脚本用查到的地址去读写：

[tests/npusim_examples/npusim_run_mobilenet.py:25](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L25) — 一次性查三个符号：`inference_status`、`inference_input`、`inference_output`，得到 `entry_point` 与 `symbol_map`。

[tests/npusim_examples/npusim_run_mobilenet.py:28-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L28-L30) — `if symbol_map.get('inference_input'):` 守卫后，造 `224*224*3` 个随机 int8 输入，`write_memory(symbol_map['inference_input'], input_data)` 把它写到设备缓冲。

#### 4.3.4 代码实践

**目标**：亲手走一遍「按名查址」，确认 `extern "C"` 与 pyelftools 这条契约成立。

**步骤**：

1. 编译设备二进制（**待本地验证**，需要 Bazel 与工具链）：`bazel build //tests/npusim_examples:run_full_mobilenet_v1_binary`。
2. 用 `readelf -s` 或 `nm` 查看产出的 `.elf`，确认 `inference_input`、`inference_output`、`inference_status`、`tensor_arena` 都是**未改编的原名**（这正是 `extern "C"` 的功劳）。
3. 用 `pyelftools` 写 3 行 Python：打开该 `.elf`，读 `e_entry`，从 `SHT_SYMTAB` 查 `inference_input` 的 `st_value`，打印其十六进制地址。
4. 对照 4.2 节的内存区表，判断该地址是否落在 DTCM 区（`0x00100000`–`0x00200000`）内；同样查 `tensor_arena` 是否落在 EXTMEM（`0x20000000` 起）。

**需要观察的现象**：`inference_input`/`inference_output` 的地址落在 DTCM，`tensor_arena` 的地址落在 EXTMEM；且所有符号名都未被改编。若某个符号在表里是 `_Z...` 形式，说明漏了 `extern "C"`。

**预期结果**：你拿到与 4.2 节一致的「符号 → 段 → 区」归属，验证契约成立。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `extern "C"`，`get_elf_entry_and_symbol` 传 `['inference_input']` 会怎样？
**答案**：C++ 会把 `inference_input` 改编成带类型的乱码符号，符号表里不再有原名 `"inference_input"`，`get_symbol_by_name` 返回 `None`，于是 `symbol_map['inference_input'] = 0`；宿主 `if symbol_map.get('inference_input'):` 因 `0` 为假而跳过写输入，模型实际上跑的是未初始化的垃圾数据。

**练习 2**：为什么 `inference_status` 初值设成 `-1`，`main` 成功返回前才置 `0`？
**答案**：这是一种简单的「成功旗」。`-1` 表示「尚未成功完成」；若程序中途崩溃/未跑完，宿主读到的仍是 `-1`，可据此判定失败。只有 `main` 走到最后一行 `inference_status = 0` 才表示推理成功，宿主读到 `0` 即可信任输出。

### 4.4 设备端 C++ 推理与端到端流程串讲

#### 4.4.1 概念说明

最后把整条链路串起来。设备端的 `main()` 本质是一段标准的 TFLite Micro 推理骨架，只是把它「嫁接」到 CoralNPU 的共享内存 I/O 上。其结构是：

1. **注册算子**：用一个 `MicroMutableOpResolver<10>` 注册 MobileNet 用到的算子。其中 **`Conv2D` 与 `DepthwiseConv2D` 故意注册成 CoralNPU 自定义内核**（`Register_CONV_2D()`/`Register_DEPTHWISE_CONV_2D()`，即 u10-l2 讲过的 RVV 重写版），其余算子（Reshape/AveragePool2D/Softmax/…）用 TFLM 默认实现。
2. **搭解释器**：`MicroInterpreter(model, op_resolver, tensor_arena, kTensorArenaSize)`，`AllocateTensors()` 分配张量。
3. **桥接共享缓冲**：用 `memcpy` 把 `inference_input`（宿主已写好）拷进模型的输入张量；`Invoke()` 跑推理；再把输出张量的前 5 个字节拷回 `inference_output`（供宿主读）。
4. **置成功旗**：`inference_status = 0; return 0;`，CRT 随后执行停机序列（`mpause`/ebreak），仿真器因 `exit_on_ebreak=True` 而退出。

注意这里的 `memcpy` 用的是 `coralnpu_v2::opt::Memcpy`（来自 `sw/opt/rvv_opt.h`），即 u10-l1 讲过的 RVV 向量化 memcpy——连搬运都走向量后端，把整条链路的「向量基因」贯彻到底。

#### 4.4.2 核心流程

把宿主与设备两侧合起来，端到端时序如下：

```text
宿主 npusim_run_mobilenet.py            设备 run_full_mobilenet_v1.cc
─────────────────────────────            ─────────────────────────────
CoralNPUV2Simulator(highmem_ld=True)
get_elf_entry_and_symbol(elf, [3 个符号])
load_program(elf, entry_point)           （ELF 灌入 ITCM/DTCM/EXTMEM）
write_memory(inference_input, 随机 int8)  ◀── 主机写共享缓冲
run(); wait()                            ──▶ CRT _start → main():
                                           RegisterOps(把 Conv/DWConv 换成 RVV 内核)
                                           MicroInterpreter + AllocateTensors
                                           Memcpy(input_tensor <─ inference_input)
                                           Invoke()  ← MobileNet 逐层算子（向量后端）
                                           Memcpy(inference_output <─ output_tensor)
                                           inference_status = 0; return 0
                                           CRT 停机(ebreak) ▶ 仿真器退出
get_cycle_count()                        ◀── 拿周期数
read_memory(inference_output, 5)         ◀── 读 Top-5 输出
read_memory(inference_status, 1)         ◀── 读成功旗
print(Top-1 / cycles / status)
```

#### 4.4.3 源码精读

设备端的算子注册是「让 MobileNet 用上 CoralNPU 向量内核」的关键：

[tests/npusim_examples/run_full_mobilenet_v1.cc:32-48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc#L32-L48) — `RegisterOps` 把 `Conv2D`、`DepthwiseConv2D` 分别绑定到 `Register_CONV_2D()`/`Register_DEPTHWISE_CONV_2D()`（即 u10-l2 的 RVV 重写内核），其余 8 个算子用 TFLM 默认。这正是 u10-l2 讲的「拿 TFLM 默认 registration、只替换 invoke」的接入点。

设备端 `main` 的推理骨架与共享缓冲桥接：

[tests/npusim_examples/run_full_mobilenet_v1.cc:62-95](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc#L62-L95) — `main`：`GetModel(...)` 加载模型 → `RegisterOps` → 构造 `MicroInterpreter`（用 4MB `tensor_arena`）→ `AllocateTensors` → `Memcpy(input, inference_input, bytes)` 注入输入 → `Invoke()` 推理 → `Memcpy(inference_output, output, 5)` 导出 5 个输出 → `inference_status = 0; return 0`。注意 `Memcpy` 是 `coralnpu_v2::opt::Memcpy`（RVV 向量化版）。

宿主侧的运行与结果回收，与上面设备 `main` 严格对应：

[tests/npusim_examples/npusim_run_mobilenet.py:32-44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L32-L44) — `run()` 启动后台执行、`wait()` 阻塞到结束；`get_cycle_count()` 打印周期数；`read_memory(inference_output, 5)` 读 5 字节输出并 `np.argmax` 打印 Top-1；最后 `read_memory(inference_status, 1)[0]` 读成功旗。

教程对这条端到端流程的概括：

[doc/tutorials/npusim_mobilenet_tutorial.md:92-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md#L92-L97) — 总结 5 步流水线：Compile 产出含静态地址符号的 `.elf` → Python 定位 `inference_input/output` 地址 → Python 写 mock 输入 → 运行仿真、设备搬运+计算+搬运 → Python 读输出校验。

教程还提示了一个性能注意点：`printf` 经 HTIF 半主机实现，会拖慢仿真，做性能剖析时应少用：

[doc/tutorials/npusim_mobilenet_tutorial.md:37-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md#L37-L38) — `printf` 由半主机经 HTIF 支持，但有开销、影响仿真性能，做完整性能剖析时应限制输出。这也解释了为何 BUILD 要开 `semihosting = True`。

#### 4.4.4 代码实践（本讲综合实践见第 5 节，此处为定向小实践）

**目标**：跟踪一条「随机输入 → Top-1 输出」的端到端调用链，并理解周期数的来源。

**步骤**：

1. 读 [npusim_run_mobilenet.py:29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L29) 确认输入是 `np.random.randint(-128, 127, size=(224*224*3,), dtype=np.int8)`，即一张随机噪声「图像」。
2. 在 [run_full_mobilenet_v1.cc:79](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc#L79) 与 [run_full_mobilenet_v1.cc:91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/run_full_mobilenet_v1.cc#L91) 标出两次 `Memcpy` 的方向（输入进模型、输出出模型）。
3. 在 [npusim_run_mobilenet.py:35](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L35) 找到周期数打印，理解 `get_cycle_count()` 反映的是**整个程序**（含 CRT、TFLM 初始化、逐层算子）的总周期，而非单算子。

**需要观察的现象**：因为是随机输入，Top-1 类别没有意义，但 `inference_status` 应为 `0`（推理成功），周期数应是「百万级」（MobileNet 即便量化也有上百万条指令）。

**预期结果**：你能画出第 4.4.2 节那张端到端时序图，并解释周期数为何偏大。**待本地验证**：实际 `bazel run tests/npusim_examples:npusim_run_mobilenet`，记录打印的周期数与 `inference_status`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MobileNet 里只有 Conv2D/DepthwiseConv2D 用了 CoralNPU 自定义内核，而 Softmax/Mean 等用 TFLM 默认？
**答案**：Conv/DepthwiseConv 是计算密集型算子，最吃算力，用 RVV 重写后能压榨 MAC/向量后端的吞吐（见 u10-l2、u7-l4）；而 Softmax/Mean/Reshape 等是轻量或重排算子，TFLM 默认的标量实现已够用，重写收益小。这是一种「只优化热点」的工程取舍。

**练习 2**：npusim 跑出的 `get_cycle_count()` 和 Verilator 跑出的周期数，可信度有何区别？
**答案**：npusim 用的是 mpact ISA 级功能模型，主要保证「功能正确」（指令语义对、结果对），其周期数是粗略的指令/模型估算，不反映真实微架构时序（如流水线气泡、cache miss、总线仲裁）；Verilator 逐拍模拟 RTL，周期数贴近真实芯片。因此做**功能验证/快速回归**用 npusim，做**精确性能评估**用 Verilator。

## 5. 综合实践

**任务**：以 MobileNet 示例为模板，跑通并改造一个「单算子」端到端流程，把本讲四个模块的知识串起来。

**背景**：仓库里其实已有一组更小的单算子示例（`tests/cocotb/tutorial/tfmicro/` 下的 `npusim_conv2d.py`、`npusim_depthwise_conv.py` 等），它们与 MobileNet 示例同构，只是把「整网」换成「单层算子」，更适合做改造练习。

**操作步骤**：

1. **跑通基线**（**待本地验证**）：执行 `bazel run tests/npusim_examples:npusim_run_mobilenet`，确认能打印 `cycles taken by the simulation ...`、`Output info: Top index ...`、`inference_status 0`。把周期数记下来作为基线。
2. **画地址地图**：用第 4.2 节的方法，填出下表（符号 → 段 → 仿真器区 → 起址区间）：

   | 符号 | section | 链接区 | 仿真器区 | 地址区间 |
   | --- | --- | --- | --- | --- |
   | `_start`(entry) | `.text` | ITCM | itcm_region | `0x0`–`0x100000` |
   | `inference_input` | `.data` | DTCM | dtcm_region | `0x100000`–`0x200000` |
   | `inference_output` | `.data` | DTCM | dtcm_region | 同上 |
   | `tensor_arena` | `.extdata` | EXTMEM | extmem_region | `0x20000000`–`0x20400000` |

   （区间端点请用 `readelf`/`nm` 实测确认。）

3. **改造输入**：把 [npusim_run_mobilenet.py:29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/npusim_examples/npusim_run_mobilenet.py#L29) 的随机输入改成「全 0」或「全某个固定 int8 值」，重新跑，观察 Top-1 是否变化、周期数是否基本不变（输入值通常不影响指令数，只影响数据）。
4. **关掉半主机看性能**：参照教程关于 `printf` 拖慢仿真的提示（[npusim_mobilenet_tutorial.md:37-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/tutorials/npusim_mobilenet_tutorial.md#L37-L38)），设想把 `run_full_mobilenet_v1.cc` 里的若干 `printf` 注释掉（**仅作思考/本地实验，不要提交对源码的修改**），预测周期数会下降，并解释原因。

**预期结果**：

- 你能复现端到端推理并拿到 `inference_status == 0` 与一个百万级周期数。
- 你能用一张表讲清「宿主写的字节如何精确落到设备 `inference_input`、设备写的输出如何被宿主 `inference_output` 读到」。
- 你能解释 npusim 与 Verilator 在「功能 vs 时序」上的分工。

> 提示：本实践的核心不是「算得准」（输入是随机的，结果无意义），而是**走通软硬协作的链路**——这正是本讲的真正目标。

## 6. 本讲小结

- npusim 采用**双体架构**：Python 宿主脚本（`npusim_run_mobilenet.py`，`py_binary`）控制仿真，C++ 设备二进制（`run_full_mobilenet_v1.cc`，`coralnpu_v2_binary`）跑 TFLM 推理，二者经**共享内存 + 命名符号**通信。
- `CoralNPUV2Simulator`（[coralnpu_v2_sim_utils.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_utils.py)）是 C++ mpact 仿真器的薄包装，`highmem_ld=True` 搭建 ITCM/DTCM/EXTMEM/DDR 四区，与链接脚本的高 mem 配置（`dtcm_origin=0x00100000`）必须严格匹配。
- **符号契约**靠两端共守：设备用 `extern "C"` 禁止名字改编、用 `__attribute__((section(...)))` 钉段；宿主用 pyelftools 的 `get_elf_entry_and_symbol` 按名查址（`e_entry` + `SHT_SYMTAB` 的 `st_value`）。
- 设备 `main` 是标准 TFLM 骨架：注册算子（Conv/DWConv 换成 u10-l2 的 RVV 内核）→ 解释器 → `Memcpy` 桥接共享缓冲 → `Invoke` → 写成功旗；连搬运都走 RVV 向量化的 `coralnpu_v2::opt::Memcpy`。
- 端到端 5 步流水线：Compile → Locate → Write → Run → Read；`run()` 非阻塞、`wait()` 阻塞，`exit_on_ebreak=True` 让 CRT 停机后仿真器退出。
- **npusim 重功能、Verilator 重时序**：npusim 用 ISA 级模型，速度快，适合跑完整大模型做功能验证与快速回归；要精确评估微架构性能则需回到 Verilator。

## 7. 下一步学习建议

- **回到 Verilator 跑同一模型**：把 MobileNet 的 `.elf` 放到 `core_mini_axi_sim`（u2-l3/u11-l1）上跑，对比 npusim 的周期数，体会「功能模型 vs RTL」的时序差异。
- **深入算子层**：本讲把 Conv2D 当黑盒，建议接着读 u10-l2 的 `sw/opt/litert-micro/conv.cc`、`fully_connected.cc` 与 `accumulator_util.h`，看清权重/激活如何组织成 MAC 引擎的 wide/narrow 输入，再结合 u7-l4 的 MAC 外积引擎理解硬件侧。
- **扩展 npusim 用法**：阅读 [sw/coralnpu_sim/coralnpu_v2_sim_test.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/coralnpu_sim/coralnpu_v2_sim_test.py)，学习断点（`set_sw_breakpoint`/`clear_sw_breakpoint`）、单步（`step`）、异步停机（`halt`）、读写寄存器等调试接口——它们能把 npusim 当成「带 GDB 能力的快速模拟器」用。
- **进入验证流程单元**：若关心 RTL 正确性与覆盖率，进入第 11 单元（Verilator/VCS/UVM/cocotb/FPGA），把 npusim 的功能结果当作「参考输出」去比对 RTL 仿真，体会协同验证（参考 u9-l2 的 spike cosim 思路）。
