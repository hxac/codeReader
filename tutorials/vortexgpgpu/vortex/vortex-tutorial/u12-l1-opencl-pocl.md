# OpenCL 支持（PoCL）

## 1. 本讲目标

本讲讲解 Vortex 如何获得 **OpenCL 1.2** 支持。学完后你应当掌握：

- 一条标准的 OpenCL 主机程序在 Vortex 上运行时，API 调用从 `clCreateContext` 一路落到 `vortex.h` 运行时接口（再落到驱动后端）的完整路径。
- PoCL（Portable Computing Language）在其中的角色：它是 OpenCL 实现，把 OpenCL 程序映射到 Vortex 运行时与 VOLT 编译器。
- OpenCL 内核源码（`.cl`）如何被 PoCL 离线编译成 Vortex 设备镜像（`.vxbin`），并被 `vx_upload_kernel_file`/`vx_start` 启动。
- `tests/opencl` 测试套件的组织方式与运行方法。

本讲是上层软件栈的第一讲，承接 u3-l1（运行时公开 API）、u3-l3（驱动后端与 stub 动态分发）、u3-l4（`.vxbin` 加载与启动流程）与 u4-l1（内核入口模型）。请确认你已经理解「stub 按 `$VORTEX_DRIVER` 选择后端」与「`.vxbin` 多入口符号表」这两件事再继续。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

**OpenCL 是一套主机/设备分离的 API 标准。** 你写两段代码：一段是跑在普通 CPU 上的**主机程序**（host），用 `clCreateBuffer`、`clEnqueueNDRangeKernel` 这类 C API 编排数据与启动；另一段是跑在加速设备上的**内核**（kernel），用 OpenCL C 语言写成，文件后缀通常是 `.cl`。OpenCL 标准只规定 API 长什么样，不规定谁来实作。

**PoCL 是 OpenCL 标准的一种开源实现。** 它把主机侧的 OpenCL API 翻译成对具体设备的调用。对 Vortex 而言，PoCL 内部有一个「Vortex 设备后端」，这个后端调用的正是 Vortex 自己的主机运行时 `libvortex.so`（即 u3-l1 讲过的 `vortex.h` 接口）。换句话说，PoCL 是 OpenCL 世界与 Vortex 运行时世界之间的**适配层**。

**ICD（Installable Client Driver）是 OpenCL 的「驱动发现」机制。** 一台机器上可能装了多个 OpenCL 平台（PoCL、NVIDIA、Intel……）。主机程序不直接链接某个平台，而是链接一个中立的 **ICD loader**（`libOpenCL.so`）；loader 在运行时读取 `/etc/OpenCL/vendors/*.icd` 这样的「名片文件」，根据它去找真正的平台库（如 `libpocl.so`）并转发调用。理解这一点非常关键，因为 Vortex 正是利用 ICD 机制把 PoCL-Vortex 平台「注册」进系统的。

**VOLT**（Vortex-Optimized Lightweight Toolchain）是基于 LLVM 的 SIMT 编译器（见 README），负责把 OpenCL C 内核编译成 Vortex 能执行的 RISC-V 机器码。PoCL 在编译内核时会调用 VOLT。

如果这些概念还模糊，不用担心，下面的源码精读会逐一对照。

## 3. 本讲源码地图

本讲涉及的文件不多，但分散在测试、构建与 CI 三处：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 顶层说明，标注 OpenCL 1.2 支持、PoCL 依赖与快速运行命令 |
| `tests/opencl/Makefile` | OpenCL 测试套件的主控 Makefile：测试列表与默认排除表 |
| `tests/opencl/common.mk` | 单个 OpenCL 测试的共享构建/运行规则，是理解 PoCL 衔接的核心 |
| `tests/opencl/vecadd/main.cc` | 标准向量加法 OpenCL 主机程序样例 |
| `tests/opencl/vecadd/kernel.cl` | vecadd 的 OpenCL C 内核源码 |
| `tests/opencl/copybuf/main.cc` | 纯缓冲区拷贝测试，不启动内核，最适合验证基本通路 |
| `ci/blackbox.sh` | 统一启动器，能把 `--app` 解析到 `tests/opencl` 下 |
| `ci/register_icd.sh` | 把 PoCL-Vortex 平台注册进系统 ICD 的可选脚本 |
| `ci/toolchain_install.sh.in` | 下载并安装预编译 PoCL 工具链 |
| `ci/testcases/opencl.yaml` | CI 中 OpenCL 测试用例的声明式目录 |

> 提示：仓库里的 `docs/software.md` 目前只有一个标题（`# Vortex OpenCL Support`），没有正文。OpenCL 的真实集成细节并不在文档里，而散布在上述构建与测试文件中——这也是本讲以源码而非文档为主线的原因。

## 4. 核心概念与源码讲解

### 4.1 OpenCL 在 Vortex 上的总体路径

#### 4.1.1 概念说明

先看一张「调用栈」式的全景图，这是本讲的总纲：

```
主机 OpenCL 程序 (main.cc)
        │  clCreateContext / clEnqueueNDRangeKernel ...
        ▼
ICD loader (libOpenCL.so, 来自 ocl-icd)
        │  读 pocl-vortex.icd 名片，转发调用
        ▼
PoCL (libpocl.so, ICD-only 构建)
        │  实现 OpenCL 运行时；其 Vortex 设备后端调用 ↓
        ▼  (vortex.h API)
Vortex 运行时 (libvortex.so, stub 分发器)
        │  首次 vx_dev_open 时按 $VORTEX_DRIVER dlopen 后端
        ▼
驱动后端 (libvortex-simx.so / -rtlsim / -opae / -xrt)
        │  实际驱动 simx / RTL 仿真 / FPGA
        ▼
设备 (SimX 仿真器 / RTL / FPGA)
```

这条链路把 OpenCL 世界「翻译」成 Vortex 世界，关键有三处衔接：

1. **主机程序 ↔ ICD loader**：靠链接 `-lOpenCL`。
2. **ICD loader ↔ PoCL**：靠 `.icd` 名片文件 + `libpocl.so`。
3. **PoCL ↔ Vortex 运行时**：靠 PoCL 的 Vortex 设备后端调用 `vortex.h`（即 u3-l1 讲过的 `vx_dev_open`/`vx_mem_alloc`/`vx_start` 等接口）。

第 3 点是理解整讲的钥匙：**PoCL 对设备的每一次操作，最终都变成对 `vortex.h` 的一次调用**。例如：

- `clCreateBuffer` → `vx_mem_alloc`
- `clEnqueueWriteBuffer` → `vx_copy_to_dev`
- `clEnqueueReadBuffer` → `vx_copy_from_dev`
- `clBuildProgram`（编译内核）→ 调用 VOLT 生成 `.vxbin`，再 `vx_upload_kernel_file`
- `clEnqueueNDRangeKernel` → `vx_start`（启动内核）

而这些 `vx_*` 函数，正是 u3-l1 精读过的同步薄封装，它们最终经 stub 分发到 simx/rtlsim/opae/xrt 后端（u3-l3）。

#### 4.1.2 核心流程

把上面的映射整理成一个内核从启动到取回结果的最小流程：

1. 主机程序用标准 OpenCL API 建立上下文与命令队列。
2. `clCreateBuffer` 分配设备显存（PoCL 调 `vx_mem_alloc`）。
3. `clEnqueueWriteBuffer` 把主机数据拷到设备（PoCL 调 `vx_copy_to_dev`）。
4. `clBuildProgram` 触发 PoCL 用 VOLT 把 `.cl` 编译成 `.vxbin`，并用 `vx_upload_kernel_file` 上传（u3-l4）。
5. `clEnqueueNDRangeKernel` 启动内核（PoCL 调 `vx_start`，向 KMU 写一组 `VX_DCR_KMU_*` 寄存器并 launch，见 u3-l4/u11-l3）。
6. `clFinish` 阻塞等待（PoCL 调 `vx_ready_wait`）。
7. `clEnqueueReadBuffer` 取回结果（PoCL 调 `vx_copy_from_dev`）。

注意：OpenCL 是「异步提交 + 事件跟踪」的模型，而 `vortex.h` 的同步封装是「一次提交 + 一次等待」（u3-l1）。PoCL 在两者之间做了异步语义到 Vortex 同步原语的映射。

#### 4.1.3 源码精读

顶层声明：README 在 Software 一节明确列出 `OpenCL 1.2`，并在依赖里列出 PoCL：

- [README.md:42-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L42-L45) 标注 Vortex 支持 OpenCL 1.2 / Vulkan / HIP 三套上层 API。
- [README.md:70-71](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L70-L71) 把 POCL 列为会被 `toolchain_install.sh` 自动获取的预编译依赖。

而 `make install` 之后的安装布局是理解「下游如何接入」的关键——下游工具（mesa-vortex、pocl-vortex、chipstar）**只**通过 `$VORTEX_PATH` 和 pkg-config 与 Vortex 集成：

- [README.md:113-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L113-L120) 说明 `make install` 会在 `$VORTEX_PATH` 下铺出一个 sysroot（公共头文件、库、`vortex-runtime.pc`/`vortex-kernel.pc`），下游（pocl-vortex 等）只通过它接入，形状与 CUDA/ROCm/oneAPI SDK 一致。

这意味着 PoCL-Vortex 是一个**独立构建的外部项目**，它在编译时用 pkg-config 找到 Vortex 的头文件与库，在运行时调用 `vortex.h`。所以你在本仓库里搜不到 PoCL 的源码——它由 `ci/toolchain_install.sh.in` 以预编译 tarball 的形式下载（见 4.3 节）。

#### 4.1.4 代码实践

**实践目标**：在不跑程序的前提下，沿调用链走一遍，确认「OpenCL API → vortex.h」的映射是真实存在的。

**操作步骤**：

1. 打开 `tests/opencl/vecadd/main.cc`，找出它调用的每一条 `cl*` API（如 `clCreateBuffer`、`clEnqueueWriteBuffer`、`clEnqueueNDRangeKernel`、`clFinish`、`clEnqueueReadBuffer`）。
2. 对照 u3-l1 讲过的 `vortex.h` 接口表，把每条 `cl*` 写成它**应当**对应的 `vx_*` 调用。
3. 再打开 `tests/opencl/common.mk`，确认主机可执行文件同时链接了 `-lOpenCL` 与 `-lvortex`（见 4.2.3），这正是「同一进程里既有 ICD loader 又有 Vortex 运行时」的物证。

**需要观察的现象**：你会看到 OpenCL 主机程序的 API 序列与 Vortex 主机程序（如 `tests/regression/demo`）的 `vx_*` 序列几乎一一对应——因为 PoCL 做的就是这层翻译。

**预期结果**：写出一张形如「`clCreateBuffer` → `vx_mem_alloc`」「`clEnqueueNDRangeKernel` → `vx_start`」的对照表。映射关系的精确实现待本地用 PoCL 源码确认（本仓库不含 PoCL 源码）。

#### 4.1.5 小练习与答案

**练习 1**：为什么主机程序要同时链接 `-lOpenCL` 和 `-lvortex` 两个库，而不是只链接一个？

**参考答案**：`-lOpenCL` 是中立的 ICD loader，负责把 OpenCL 调用转发给被发现的平台库 `libpocl.so`；`-lvortex` 是 Vortex 运行时，PoCL 的 Vortex 设备后端在运行时要调用 `vortex.h` 接口，这些符号必须能在进程里解析到，因此主机可执行文件把 `libvortex.so` 也链接进来，使符号可被解析。

**练习 2**：如果一台机器上同时装了 NVIDIA 的 OpenCL 平台和 PoCL-Vortex 平台，ICD loader 如何决定用哪个？

**参考答案**：由 ICD 名片文件（`/etc/OpenCL/vendors/*.icd`）或环境变量 `OCL_ICD_VENDORS` 指定的名片目录决定。loader 枚举所有名片得到平台列表；主机程序通常取 `platforms[0]`（见 `copybuf/main.cc`），所以平台顺序很重要，Vortex 在 CI 中用 `OCL_ICD_VENDORS` 把 loader 钉死到只看 PoCL 的名片（见 4.2 节）。

---

### 4.2 PoCL 的 ICD 发现机制与运行环境

#### 4.2.1 概念说明

PoCL 在 Vortex 上是 **ICD-only** 构建的——也就是说它不以静态/动态库的形式被直接链接，而是被打包成一个「平台」，由 ICD loader 在运行时发现并加载。这样做的好处是 PoCL-Vortex 能与系统里其他 OpenCL 平台共存。

发现机制有两种：

- **系统级注册**（部署用，需要 root）：往 `/etc/OpenCL/vendors/` 写一个 `.icd` 名片文件。脚本 `ci/register_icd.sh` 干这件事。
- **进程级覆盖**（CI/测试用，无需 root）：设环境变量 `OCL_ICD_VENDORS` 指向一个只含 PoCL 名片的目录。Vortex 的测试走这条路，所以 CI 不需要 sudo。

名片文件本身只有一行：指向真正的平台库 `libpocl.so` 的绝对路径。

#### 4.2.2 核心流程

CI 运行一个 OpenCL 测试时的环境装配流程：

1. `toolchain_install.sh --pocl` 把预编译 PoCL 解压到 `$TOOLDIR/pocl`，并在其安装目录里写好 `etc/OpenCL/vendors/pocl.icd`（名片指向 `$TOOLDIR/pocl/lib/libpocl.so.*`）。
2. 运行测试时，`common.mk` 的 `run-simx` 规则把 `OCL_ICD_VENDORS` 钉到 `$POCL_PATH/etc/OpenCL/vendors`，让 ocl-icd loader **只**看到 PoCL 名片，避免误中并存的厂商 loader（如 CUDA 自带的 `libOpenCL`）。
3. 同一条命令把 `LD_LIBRARY_PATH` 铺好（包含 PoCL 的 `lib`、Vortex 运行时 `lib`、LLVM `lib`），并把 `VORTEX_DRIVER=simx` 设给 Vortex stub（u3-l3）。
4. loader 读名片 → 加载 `libpocl.so` → 主机程序的 `cl*` 调用进入 PoCL → PoCL 调 `vortex.h` → stub 选 simx 后端。

#### 4.2.3 源码精读

先看主机可执行文件如何链接两个库，以及 ICD loader 钉死的路径变量：

- [tests/opencl/common.mk:34-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L34-L39) 解释了链接策略：PoCL 是 ICD-only 构建，所以主机程序链接系统 ocl-icd loader（`-lOpenCL`），由它经厂商 `.icd` 名片发现 Vortex 平台；`OCL_ICD_VENDORS` 把 loader 钉到 PoCL 的名片目录，`OCL_ICD_LIB_DIR` 钉住 ocl-icd loader 本身，避免并存厂商 loader（如 CUDA 的 `libOpenCL`）被选中。
- [tests/opencl/common.mk:113-114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L113-L114) 主机可执行文件同时链接 `-lvortex`（Vortex 运行时）与 `-lOpenCL`（ICD loader），是 4.1.5 练习 1 的直接物证。

再看 `run-simx` 规则如何把整个运行环境一次性铺好：

- [tests/opencl/common.mk:122-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L122-L124) `run-simx` 先重建 simx 后端库，再在运行时设置 `LD_LIBRARY_PATH`（ocl-icd/PoCL/Vortex 运行时/LLVM）、`OCL_ICD_VENDORS`（名片目录）、`VORTEX_DRIVER=simx`（stub 后端选择）以及 `POCL_CC_FLAGS`（内核编译参数，见 4.3 节）。一条命令把「OpenCL loader、PoCL 平台、Vortex 运行时后端、内核编译器」四件事全部就位。

接着看 ICD 名片的内容与系统级注册脚本：

- [ci/register_icd.sh:23-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/register_icd.sh#L23-L24) 系统级注册把名片写到 `/etc/OpenCL/vendors/pocl-vortex.icd`，这是 ocl-icd 与 Khronos 参考加载器都遵循的标准位置。
- [ci/register_icd.sh:32-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/register_icd.sh#L32-L41) 名片文件只有一行：`libpocl.so` 的绝对路径。脚本头部注释明确说明它是**可选**的部署路径，CI/测试不走它（用 `OCL_ICD_VENDORS`，免 sudo）。

最后看工具链安装时如何把名片放进 PoCL 安装目录：

- [ci/toolchain_install.sh.in:110-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L110-L123) `pocl()` 函数下载预编译 PoCL 到 `$TOOLDIR/pocl`，并因其「ICD-only 构建」而在 `$TOOLDIR/pocl/etc/OpenCL/vendors/pocl.icd` 写入 `libpocl.so` 的绝对路径——这正是 `common.mk` 里 `OCL_ICD_VENDORS` 默认指向的目录。

#### 4.2.4 代码实践

**实践目标**：弄清两种 ICD 发现路径的差异。

**操作步骤**：

1. 读 `ci/register_icd.sh` 的头部注释（第 1–20 行），用自己的话说明「系统级注册」与「进程级 `OCL_ICD_VENDORS`」分别用于什么场景。
2. 读 `tests/opencl/common.mk` 第 34–39 行与第 122–124 行，回答：CI 为什么不写 `/etc/OpenCL/vendors/`？

**需要观察的现象**：你会看到 CI 路径全程不需要 root，而系统级注册脚本明确要求 `sudo`。

**预期结果**：CI 用 `OCL_ICD_VENDORS` 做进程级隔离，既免 sudo，又能避免误中并存厂商平台；系统级注册只用于真实部署。

#### 4.2.5 小练习与答案

**练习**：`OCL_ICD_LIB_DIR`（`common.mk:39`）的作用是什么？为什么需要它？

**参考答案**：它钉住 ocl-icd loader 本身的查找目录，确保进程加载的是系统 ocl-icd 的 `libOpenCL.so`，而不是被 `LD_LIBRARY_PATH` 里某个并存厂商（如 CUDA）自带的 `libOpenCL.so` 抢先加载——后者可能不认识 `pocl-vortex.icd` 名片或行为不一致。

---

### 4.3 OpenCL 内核的设备编译：从 `.cl` 到 `.vxbin`

#### 4.3.1 概念说明

OpenCL 内核是**运行时编译**的：主机程序读入 `.cl` 源码，调用 `clCreateProgramWithSource` + `clBuildProgram`，由 OpenCL 实现（PoCL）在程序执行期间把它编译成设备代码。对 Vortex 而言，「设备代码」就是一份 `.vxbin`（u3-l4 讲过的格式：镜像 + 可选的 `VXSYMTAB` 多入口尾部）。

PoCL 编译 Vortex 内核时调用的是 **VOLT**（基于 LLVM）。为了让 PoCL 知道「编译 Vortex 内核要用哪个编译器、用什么 flags、用什么链接脚本」，`common.mk` 把整套 VOLT 工具链参数通过一组以 `POCL_VORTEX_` 开头的环境变量在**运行时**传给可执行程序，PoCL 在 `clBuildProgram` 时读取它们。

#### 4.3.2 核心流程

内核编译的数据流：

1. 主机程序 `read_kernel_file("kernel.cl")` 读入源码字节。
2. `clCreateProgramWithSource` + `clBuildProgram` 触发 PoCL 的离线编译。
3. PoCL 读 `POCL_VORTEX_CFLAGS`（编译选项）、`POCL_VORTEX_LDFLAGS`（链接选项）、`POCL_VORTEX_BINTOOL`（镜像打包工具）、`LLVM_PREFIX`（VOLT/LLVM 路径）。
4. PoCL 调用 VOLT 把 `.cl` 编译成 RISC-V 目标代码（带 `+xvortex` 目标特性），用 `vxbin.py` 打包成 `.vxbin`。
5. PoCL 内部用 `vx_upload_kernel_file` 上传该镜像，`clCreateKernel("vecadd")` 通过名字查 `VXSYMTAB` 得到入口 PC（u3-l4/u4-l1）。
6. `clEnqueueNDRangeKernel` 调 `vx_start` 启动。

这里有一条贯穿前序讲义的主线：内核镜像的打包工具 `vxbin.py`、多入口符号表 `VXSYMTAB`、启动入口 `__vx_cta_entry`、KMU 启动寄存器 `VX_DCR_KMU_*`——这些在 u3-l4 与 u4-l1 里讲过的机制，**对 OpenCL 内核与原生 Vortex 内核是完全相同的**。PoCL 只是换了一种方式（运行时编译而非提前编译）产出同一份 `.vxbin`。

#### 4.3.3 源码精读

VOLT 工具链参数的定义全在 `common.mk`：

- [tests/opencl/common.mk:46-51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L46-L51) `VX_CFLAGS` 给设备侧编译定下目标：`--target=riscv$(XLEN)-unknown-elf`、`-O3 -mcmodel=medany`、bare-metal 的 `-nostdlib -fno-rtti -fno-exceptions`，并带上 `-Xclang -target-feature -Xclang +xvortex`（VOLT 的 Vortex 目标特性）与 `+zicond`。这是「让 LLVM/VOLT 生成 Vortex SIMT 代码」的关键开关。
- [tests/opencl/common.mk:58](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L58) `VX_LDFLAGS` 用 Vortex 内核链接脚本 `link$(XLEN).ld`、入口地址 `STARTUP_ADDR`，并静态链接 `libvortex2.a`（设备侧内核运行时，即 u4-l1 的 KMU 版 `libvortex2`）。这证明 OpenCL 内核与原生内核共用同一份设备侧运行时。
- [tests/opencl/common.mk:60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L60) `VX_BINTOOL` 把镜像打包工具设为 `vxbin.py`（用 `llvm-objcopy`），正是 u3-l4/u4-l1 讲过的、产出 `VXSYMTAB` 多入口尾部的那个脚本。

这些参数通过 `POCL_CC_FLAGS` 在运行时传给可执行程序：

- [tests/opencl/common.mk:68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/common.mk#L68) 把 `LLVM_PREFIX`、`POCL_VORTEX_BINTOOL`、`POCL_VORTEX_CFLAGS`、`POCL_VORTEX_LDFLAGS` 打包进 `POCL_CC_FLAGS`，它出现在每条 `run-*` 规则的命令行前缀里（如 `common.mk:124`），作为环境变量被 PoCL 在 `clBuildProgram` 时读取。

再看一个最简内核源码，理解「OpenCL C 写法」如何对应到 Vortex SIMT 模型：

- [tests/opencl/vecadd/kernel.cl:1-9](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/vecadd/kernel.cl#L1-L9) vecadd 内核用 `get_global_id(0)` 取得全局线程编号，做一次加法。VOLT 会把每个 OpenCL 工作项映射到 Vortex 的一个 SIMT 线程；`get_global_id` 这类内建函数由 VOLT 编译成对 CTA CSR 的读取（u4-l2 讲过的硬件派生模型）。

#### 4.3.4 代码实践

**实践目标**：确认 OpenCL 内核与原生 Vortex 内核共用同一套设备侧运行时与打包工具。

**操作步骤**：

1. 在 `tests/opencl/common.mk` 找到 `VX_LDFLAGS`（第 58 行），确认它链接的是 `libvortex2.a`（与 `tests/regression` 下原生测试用的是同一个设备侧库）。
2. 找到 `VX_BINTOOL`（第 60 行），确认打包工具是 `sw/kernel/scripts/vxbin.py`。
3. 对照 u3-l4 讲过的 `.vxbin` 结构（16 字节头 + 镜像 + 可选 `VXSYMTAB` 尾部），理解 PoCL 编译出的内核镜像与原生 `.vxbin` 在格式上没有区别。

**需要观察的现象**：你会看到 OpenCL 与原生路径在「镜像格式、设备侧运行时、启动方式」三层完全一致，区别只在「源码是运行时编译还是提前编译」。

**预期结果**：能解释为什么 PoCL 可以直接复用 `vx_upload_kernel_file`/`vx_start`——因为它产出的就是标准 `.vxbin`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `POCL_VORTEX_*` 这些编译参数要在**运行时**（作为环境变量）传给程序，而不是在主机程序编译时就固定？

**参考答案**：因为 OpenCL 内核是运行时编译的（`clBuildProgram` 在程序运行时才发生），PoCL 需要在那一刻拿到「用哪个编译器、什么 flags」。把这些参数作为环境变量在运行时注入，可以让同一份主机二进制在不同 XLEN（32/64）、不同配置（`CONFIGS`）下重用，而无需重新编译主机程序。

**练习 2**：vecadd 的 `kernel.cl` 里没有出现任何 `vx_*` 内联函数，它是怎么变成 Vortex SIMT 代码的？

**参考答案**：VOLT 编译器把 OpenCL C 的内建（如 `get_global_id`、工作项维度的 `__kernel`）自动映射到 Vortex 的 SIMT 抽象——工作项对应线程、`get_global_id` 编译成读 CTA CSR。开发者写的是可移植的 OpenCL C，SIMT 化由 VOLT 完成，这正是 PoCL+VOLT 提供「写 OpenCL 就能跑在 Vortex 上」体验的核心。

---

### 4.4 tests/opencl 测试套件的组织

#### 4.4.1 概念说明

`tests/opencl` 是一组完整的 OpenCL 测试程序，覆盖从向量加法（vecadd）、矩阵乘（sgemm）到图形学/科学计算（backprop、bfs、hotspot、pathfinder 等）众多 workload。每个子目录是一个独立的 OpenCL 程序，包含主机代码（`main.cc`）、内核源码（`kernel.cl`）和一个极简 `Makefile`，共享的构建规则集中放在 `common.mk`。

有两点工程纪律值得注意：

- **默认排除表（EXCLUDE）**：有些测试需要特殊配置（如原子指令需 A 扩展、image 测试需 image-enabled PoCL），因此被排除在「默认扫描」之外，需用专门的配置或专门路径运行。
- **声明式 CI 目录**：CI 里用 `ci/testcases/opencl.yaml` 声明每条用例（用哪个 app、哪些 driver、什么 configs），由 pytest 框架消费（见 u13-l4）。

#### 4.4.2 核心流程

运行 OpenCL 测试有三种入口，粒度从粗到细：

1. **整套扫描**：`make -C tests/opencl run-simx`，跑所有未排除的测试（受 `tests/opencl/Makefile` 的 `TESTS`/`EXCLUDE` 控制）。
2. **单个测试**：`make -C tests/opencl/vecadd run-simx`，只跑 vecadd（EXCLUDE 表对单个测试目录无效）。
3. **统一启动器**：`./ci/blackbox.sh --app=opencl/vecadd`（或 `--app=copybuf`），由 `blackbox.sh` 解析到 `tests/opencl/<app>`。

#### 4.4.3 源码精读

先看主控 Makefile 的测试列表与排除逻辑：

- [tests/opencl/Makefile:5-12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/Makefile#L5-L12) 主测试列表 `TESTS`，涵盖 vecadd/sgemm/copybuf/backprop/bfs/hotspot 等数十个 workload。
- [tests/opencl/Makefile:14-37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/Makefile#L14-L37) `EXCLUDE` 表及注释：`histogram`/`atomicreduce`/`hybridsort` 用了 `atomic_add`（RVA `amo*.w`），只在开启 A 扩展时跑；`image_*` 需要 image-enabled PoCL 构建；`copybuf`、`transpose`、`lbm` 等也默认排除。这些被排除项需用专门配置/路径运行（见 `opencl.yaml`）。
- [tests/opencl/Makefile:50-51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/Makefile#L50-L51) 实际生效测试集 `ACTIVE_TESTS = $(filter-out $(EXCLUDE),$(TESTS))`，每后端再用 `backend_tests` 做二次过滤。

再看一个标准主机程序的控制流（vecadd），它是理解「OpenCL 主机程序长什么样」的范本：

- [tests/opencl/vecadd/main.cc:162-187](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/vecadd/main.cc#L162-L187) 典型的 OpenCL 初始化：`clGetPlatformIDs` → `clGetDeviceIDs` → `clCreateContext` → `clCreateBuffer` ×3 → 读 `kernel.cl` → `clCreateProgramWithSource` → `clBuildProgram`（触发 PoCL+VOLT 编译）→ `clCreateKernel("vecadd")`。
- [tests/opencl/vecadd/main.cc:208-220](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/vecadd/main.cc#L208-L220) 数据上传与启动：`clEnqueueWriteBuffer`（→ `vx_copy_to_dev`）→ `clEnqueueNDRangeKernel`（→ `vx_start`）→ `clFinish`（→ `vx_ready_wait`）。
- [tests/opencl/vecadd/main.cc:222-238](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/vecadd/main.cc#L222-L238) 取回并校验：`clEnqueueReadBuffer`（→ `vx_copy_from_dev`），对比 CPU 参考结果，打印 `PASSED!`。

纯拷贝测试 copybuf（不启动内核，只验证缓冲区操作）是验证基本通路的最小例子：

- [tests/opencl/copybuf/main.cc:24-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/opencl/copybuf/main.cc#L24-L45) 用 `clCreateBuffer` + `clEnqueueCopyBuffer` + `clEnqueueReadBuffer` 做一次设备内拷贝并校验。它不涉及内核编译，最适合验证「ICD loader → PoCL → Vortex 运行时 → 后端」这条通路本身是否通。

统一启动器对 opencl 的解析：

- [ci/blackbox.sh:111-112](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L111-L112) `set_app_path()` 会在 `tests/regression` 之后、`tests/hip` 之前尝试 `tests/opencl/$APP`。注意解析顺序：由于 `regression` 优先（`blackbox.sh:103-104`），对 `vecadd` 这种在 regression 和 opencl 下**都存在**的名字，`--app=vecadd` 实际会命中 `tests/regression/vecadd`（原生版，用 `kernel.cpp`）。要跑 OpenCL 版，应写 `--app=opencl/vecadd`（被更靠前的 `tests/$APP` 规则 `blackbox.sh:101-102` 解析为 `tests/opencl/vecadd`）或 `--app=copybuf`（只存在于 opencl 下）。
- [ci/blackbox.sh:30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L30) 帮助文本说明 `--app` 可以是 `regression/graphics/mpi/opencl/hip` 任意子目录下的测试。

最后看 CI 声明式目录：

- [ci/testcases/opencl.yaml:1-20](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/opencl.yaml#L1-L20) 用例 `isa-1`/`isa-2` 用 `via: make-run`、`dir: tests/opencl`、`target: run-{driver}` 跑整套默认扫描（分别 simx/rtlsim），xlen 覆盖 32/64；`hybridsort` 等需 A 扩展的用 `via: blackbox` + `configs: "-DVX_CFG_EXT_A_ENABLE"` 单独声明。

#### 4.4.4 代码实践

**实践目标**：在 SimX 上跑通一个真实的 OpenCL 内核，并解释 API 落点。

**操作步骤**（需要已安装 PoCL 工具链，见 4.2 节）：

1. 从 build 目录先确保工具链就绪：`./ci/toolchain_install.sh --pocl`（待本地验证：若已装可跳过）。
2. 跑 copybuf（最小通路验证，无内核编译）：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=copybuf
   ```
   或直接用 Make：
   ```sh
   make -C tests/opencl/copybuf run-simx
   ```
3. 跑 vecadd（含内核编译与启动）：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=opencl/vecadd
   ```
4. 改变规模重跑，观察行为：vecadd 的 `OPTS` 默认 `-n64`（见 `vecadd/Makefile:18`），可在 blackbox 用 `--args="-n128"` 调整（待本地验证）。

**需要观察的现象**：

- copybuf 应打印 `ALL TESTS PASSED`（见 `copybuf/main.cc:182`）。
- vecadd 应打印 `PASSED!`（见 `vecadd/main.cc:235`）。
- 启动时命令行会回显一长串 `LD_LIBRARY_PATH=... OCL_ICD_VENDORS=... VORTEX_DRIVER=simx ...`，这是 `common.mk:124` 铺设的运行环境。

**预期结果**：程序退出码 0 且打印 `PASSED`。若提示找不到 OpenCL 平台，多半是 `OCL_ICD_VENDORS` 没指向 PoCL 名片目录或 PoCL 工具链未安装。**若本地未安装 PoCL 工具链无法运行，请改做 4.1.4 的源码阅读型实践**——跟踪 `vecadd/main.cc` 的每条 `cl*` API 到 `vortex.h` 的映射，这也是本讲实践任务的核心。

> 注意：copybuf 在 `tests/opencl/Makefile` 的 EXCLUDE 表里（`Makefile:26`），所以 `make -C tests/opencl run-simx` 整套扫描**不会**包含它；必须用上面单目录或 blackbox 的方式直接运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `histogram`、`atomicreduce` 被排除在默认扫描之外？

**参考答案**：它们用了 `atomic_add`，对应 RISC-V「A」扩展的 `amo*.w` 指令；默认扫描用的是不开 A 扩展的配置，所以这些测试必须带 `-DVX_CFG_EXT_A_ENABLE` 才能正确编译运行（见 `Makefile:15-17` 与 `opencl.yaml` 的 hybridsort 用例）。

**练习 2**：`./ci/blackbox.sh --app=vecadd` 和 `./ci/blackbox.sh --app=opencl/vecadd` 跑的是同一个程序吗？

**参考答案**：不是。前者因 `blackbox.sh` 的解析顺序（regression 优先于 opencl）命中 `tests/regression/vecadd`（原生 Vortex 版，内核是 `kernel.cpp`，直接用 `vortex.h`）；后者命中 `tests/opencl/vecadd`（OpenCL 版，内核是 `kernel.cl`，经 PoCL+VOLT）。两者验证的是同一件计算事（向量加），但走的是两条不同的软件栈路径。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端的小任务：

**任务**：选择 `vecadd` 这个 OpenCL 程序，画出从「主机 `main` 函数第一条 OpenCL 调用」到「SimX 仿真器里一条 CTA 被 KMU 唤醒」的完整调用链，并在每一跳标注：调用了什么 API、跨了哪一层、用了哪个环境变量或配置。

**要求覆盖的跳数**：

1. `clGetPlatformIDs` 如何经 ICD loader 找到 PoCL（涉及 `OCL_ICD_VENDORS`、`pocl.icd`）。
2. `clBuildProgram` 如何触发 PoCL 用 VOLT 编译 `kernel.cl` 成 `.vxbin`（涉及 `POCL_VORTEX_CFLAGS`/`+xvortex`/`vxbin.py`）。
3. `clCreateKernel("vecadd")` 如何经 `VXSYMTAB` 名字解析得到入口 PC（关联 u3-l4/u4-l1）。
4. `clEnqueueNDRangeKernel` → `vx_start` → 写 `VX_DCR_KMU_*` → KMU 派发 CTA（关联 u3-l4/u11-l3）。
5. `VORTEX_DRIVER=simx` 如何让 stub 选 simx 后端（关联 u3-l3）。

**交付物**：一张调用链图 + 一份「跳数 → 涉及的源码文件与行号」清单。如果你本地装了 PoCL，再实际跑一遍 `./ci/blackbox.sh --driver=simx --app=opencl/vecadd`，把命令回显的环境变量标注到图上对应的跳数。

这个任务把「OpenCL 标准 API、ICD 发现、PoCL 适配、VOLT 编译、Vortex 运行时、stub 后端分发、KMU 启动」七件事串成一条线，是对本讲和 u3 单元的综合检验。

## 6. 本讲小结

- Vortex 的 OpenCL 1.2 支持由 **PoCL** 提供，PoCL 是 OpenCL 标准的开源实现，其 Vortex 设备后端调用本仓库的 `vortex.h` 运行时接口——OpenCL 的每条 `cl*` API 最终都落到一条 `vx_*` 调用上。
- 完整调用链是：主机程序 → ocl-icd loader（`-lOpenCL`）→ PoCL（`libpocl.so`）→ Vortex 运行时（`libvortex.so`，stub 分发）→ 后端（simx/rtlsim/opae/xrt）→ 设备。
- PoCL 是 **ICD-only** 构建，靠 `.icd` 名片被 loader 发现；CI 用 `OCL_ICD_VENDORS` 做进程级、免 sudo 的发现，真实部署用 `ci/register_icd.sh` 写 `/etc/OpenCL/vendors/`。
- OpenCL 内核是**运行时编译**的：PoCL 读 `POCL_VORTEX_*` 环境变量，调用 VOLT（带 `+xvortex` 目标特性）把 `.cl` 编译并用 `vxbin.py` 打包成 `.vxbin`——与原生 Vortex 内核共用同一镜像格式、同一设备侧运行时 `libvortex2.a`、同一启动机制。
- `tests/opencl` 是一组标准 OpenCL 测试，主控 `Makefile` 用 `TESTS`/`EXCLUDE` 管理默认扫描（原子/image 类需特殊配置），可用 `make -C tests/opencl/<app> run-simx` 或 `./ci/blackbox.sh --app=opencl/<app>` 运行单例。
- 注意 `blackbox.sh` 的 app 解析顺序：regression 优先于 opencl，故 `--app=vecadd` 命中的是原生版，OpenCL 版要写 `--app=opencl/vecadd`。

## 7. 下一步学习建议

- **继续上层栈**：本单元后续两讲是 u12-l2（Vulkan / mesa-vortex / vortexpipe）与 u12-l3（HIP / chipStar），它们与 OpenCL/PoCL 是平行的「上层 API → Vortex 运行时」适配关系，对比三者的适配层设计会加深理解。
- **回顾运行时主线**：如果本讲里「stub 后端分发」「`.vxbin` 加载」「KMU 启动」还不够清晰，建议重读 u3-l3、u3-l4、u4-l1，它们是本讲所有「落到 vortex.h」说法的根基。
- **读 PoCL 源码**：本仓库不含 PoCL 源码，如果你想确认「`clEnqueueNDRangeKernel` 到底在 PoCL 内部如何调 `vx_start`」，可去 [PoCL 项目](http://portablecl.org/) 找其 Vortex 设备后端（pocl-vortex）的实现。
- **跑更多测试**：在 SimX 上跑 `sgemm`、`backprop`、`bfs` 等 OpenCL workload（注意 image_* 与 atomic 类需专门配置），观察不同 workload 对 Vortex 硬件配置（cores/warps/threads）的敏感度，为 u13-l3（性能分析）做铺垫。
