# HIP 支持（chipStar）

## 1. 本讲目标

本讲回答一个问题：**一个用 AMD HIP 写的 GPU 程序，是怎么在 Vortex 这个 RISC-V GPGPU 上编译并跑起来的？**

读完本讲，你应当能够：

1. 说出 HIP-on-Vortex 的完整路径 `chipStar → SPIR-V → POCL → Vortex`，并解释为什么是这条分层路径。
2. 区分「仓库内（in-tree）组件」与「外部工具链」的边界——Vortex 仓库只拥有测试源码、构建脚本和 CI 胶水，而 hipcc / `libCHIP.so` / 设备库全是外部安装的。
3. 读懂 `tests/hip/common.mk` 这个真正的「构建/运行引擎」：它如何用 chipStar 的 hipcc 把 HIP 编成内嵌 SPIR-V 的主机 ELF，再在运行时由 POCL 把 SPIR-V JIT 成 Vortex 的 `.vxbin`。
4. 理解 32 位（rv32）与 64 位（rv64）双宽支持的关键开关 `--offload-pointer-width=$(XLEN)`，以及 `tests/hip` 测试套件的组织方式。

本讲是专家层「上层软件栈」单元的第三篇，承接 u12-l1（OpenCL/PoCL）建立的对 POCL 与 ICD 机制的认知。

---

## 2. 前置知识

本讲假定你已经从前置讲义建立了以下认知，这里只做简要回顾与衔接：

- **HIP 是什么**：AMD 推出的类 CUDA 编程模型。它的主机 API 形如 `hipMalloc` / `hipMemcpy` / `<<<grid,block>>>` 启动语法 / `hipDeviceSynchronize`，几乎可以逐字对应 CUDA（`cudaMalloc` 等）。一个 HIP 程序分「主机代码」和「设备 kernel（`__global__` 函数）」两部分。
- **chipStar 是什么**：HIP 的开源实现。它把 HIP 程序翻译成 **SPIR-V**（Khronos 的跨平台中间表示），再借助一个「后端」来执行。chipStar 支持多种后端（`CHIP_BE`），其中 `CHIP_BE=opencl` 表示把 HIP 主机调用映射成 OpenCL 调用。
- **SPIR-V 是什么**：一种与厂商无关、与语言无关的二进制中间表示，既能表达 OpenCL（计算），也能表达 Vulkan（图形）。本讲里它扮演「HIP 设备代码的载体」，由 POCL 在运行时 JIT 成目标机器码。
- **承接 u12-l1（OpenCL/PoCL）**：你已经知道 Vortex 通过 PoCL（`libpocl.so`）获得 OpenCL 1.2 支持，PoCL 把 OpenCL 程序编译成 `.vxbin` 并经 `vortex.h` 运行时接口下发。**HIP 复用的正是这一整层 PoCL**——这是本讲最关键的一句话。
- **承接 u3-1/u3-3（运行时与 stub）**：主机侧 `libvortex.so` 是一个 stub 分发器，按环境变量 `$VORTEX_DRIVER`（`simx`/`rtlsim`/`opae`/`xrt`）dlopen 对应后端库。
- **承接 u2（配置系统）**：`XLEN`（32 或 64）是全树共享的字长配置，`VX_CFG_XLEN` 宏会在编译期与运行期同时生效。

> 一句话心智模型：**HIP-on-Vortex = 「HIP 套了件 OpenCL 的外套，再借 PoCL 这条已经在 u12-l1 里铺好的路走进 Vortex」**。理解了这一点，本讲剩下的就是工程细节。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`docs/designs/hip_on_vortex_chipstar.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md) | 设计文档：路径总览、仓库内组件表、32 位支持说明、未实现方向。本讲的「总纲」。 |
| [`tests/hip/common.mk`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk) | **真正的构建/运行引擎**：用 chipStar 的 hipcc 构建，在 POCL/chipStar 下运行。 |
| [`tests/hip/Makefile`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/Makefile) | 测试套件聚合：`TESTS` 列表、按后端排除（`EXCLUDE`）、`run-{driver}` 规则。 |
| [`tests/hip/vecadd/main.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp) | 真实 HIP 测试：向量加法 kernel。 |
| [`tests/hip/sgemm/main.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/sgemm/main.cpp) | 真实 HIP 测试：矩阵乘 kernel。 |
| [`ci/chipstar_install.sh.in`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in) | chipStar 的「生产脚本」：从源码构建 hipcc / `libCHIP.so` 到 `$TOOLDIR/chipstar`。 |
| [`ci/toolchain_install.sh.in`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in) | 拉取预编译 chipStar / PoCL tarball 的函数。 |
| [`ci/testcases/hip.yaml`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/hip.yaml) | CI 目录里的 `hip` 分类，声明各驱动下的 `run-{driver}`。 |
| [`README.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md) | 把 HIP 列为支持特性、把 chipStar 列为依赖。 |

---

## 4. 核心概念与源码讲解

### 4.1 HIP-on-Vortex 的总体路径

#### 4.1.1 概念说明

Vortex 本身是一颗 RISC-V GPGPU，它原生只认两种「上层语言入口」：一是 `vortex.h` 运行时 API（见 u3-1），二是经 PoCL 走的 OpenCL（见 u12-l1）。**HIP 并不是一条新的硬件通路**，而是一条「借用 OpenCL 通路」的软件路径。

这条路径用一个外部工具 **chipStar** 把 HIP 翻译成 SPIR-V，并把 HIP 的主机调用映射成 OpenCL 调用（`CHIP_BE=opencl`）。于是 HIP 程序在 Vortex 上的命运就变成了：

> **HIP 源码 →（chipStar hipcc 编译）→ 内嵌 SPIR-V 的主机 ELF →（运行时 POCL 把 SPIR-V JIT 成 `.vxbin`）→ 经 `libvortex.so` 在 simx/rtlsim/opae/xrt 上执行。**

设计文档把这条路径概括为 `chipStar → SPIR-V → POCL → Vortex`，同时支持 64 位（rv64）与 32 位（rv32）。设计动机很务实：**让 Vortex 的 `sw/` 运行时树完全不被 HIP 触碰**——所有承重的逻辑（hipcc、`libCHIP.so`、SPIR-V→Vortex 的 lowering、运行时）都在外部仓库里，由 CI 安装到 `$TOOLDIR`。

一个重要的边界结论（本讲会反复强调）：**这条路径结构上无法触及 Vortex 专有指令**（WMMA/WGMMA/TMA 等张量核与异步拷贝 intrinsic）。因为 SPIR-V 是个标准化的中间层，HIP 头文件里没有表达这些 Vortex 私有扩展的渠道。这是一个已知取舍，留待专门的「原生 HIP 工具链」方向解决（见 §4.1.2 末尾与文档 §5）。

#### 4.1.2 核心流程

整条路径分**编译期**与**运行期**两个阶段：

```text
编译期（构建主机二进制，离线完成）：
  main.cpp（HIP：__global__ kernel + hipMalloc/hipMemcpy/<<<>>>）
     │  chipStar 的 hipcc，带 --offload-pointer-width=$XLEN
     │  clang++ (llvm_vortex) --offload=spirv{32,64} → device.spv（Physical32/64）
     ▼
  主机 ELF：内嵌 SPIR-V fatbin，链接 libCHIP.so

运行期（程序启动后，JIT + 执行）：
  主机 ELF
     │  libCHIP.so（CHIP_BE=opencl）→ POCL libOpenCL → POCL 的 Vortex device
     │  POCL 把 SPIR-V JIT → riscv$XLEN → .vxbin（用 clang + vxbin.py）
     ▼
  libvortex.so 在 simx / rtlsim / opae / xrt 上执行
```

要点：

- **编译期只产出「内嵌 SPIR-V 的主机 ELF」**，并不直接产出 Vortex 的 `.vxbin`。`.vxbin` 是运行期才由 POCL JIT 出来的——这一点和 u12-l1 里 OpenCL 内核「运行时编译」的形态完全一致。
- **HIP 主机调用 → OpenCL 调用**：例如 `hipMalloc` 对应 `clCreateBuffer`（再下沉到 `vx_mem_alloc`），`<<<>>>` 启动对应 `clEnqueueNDRangeKernel`（再下沉到 `vx_start`）。这正是 chipStar 选 `CHIP_BE=opencl` 的效果，也正因为如此 HIP 才能免费复用 PoCL 这条已经铺好的路。
- **未实现方向**（文档 §5）：曾提出一套「原生 HIP 工具链」——`HIPVortex` Clang driver、基于 `vortex2.h` 的原生 `libhip_vortex` 运行时、`vortex_mlir` 方言。其最强动机正是「让 HIP 头文件能暴露 Vortex 专有 intrinsic（WMMA/WGMMA/TMA）」，这是 chipStar/SPIR-V 路径结构性做不到的事。目前仅有 stub，未实现。

#### 4.1.3 源码精读

设计文档开篇就给出了上面这条路径的权威版本：

[docs/designs/hip_on_vortex_chipstar.md:21-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L21-L32) —— 这是 HIP-on-Vortex 的「架构图」，明确画出编译期（hipcc → `device.spv`）与运行期（`libCHIP.so → POCL → Vortex`）两段，并点出 POCL 在运行时把 SPIR-V JIT 成 `.vxbin`。

紧接着文档强调了一个贯穿全讲的边界事实——Vortex 的 `sw/` 运行时树本身对 HIP 是「不可见」的：

[docs/designs/hip_on_vortex_chipstar.md:34-37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L34-L37) —— 所有承重逻辑（hipcc、`libCHIP.so`、设备库、SPIR-V→Vortex lowering、运行时）都是外部的，由 CI 安装到 `$TOOLDIR`。

README 把 HIP 列为支持的上层 API 之一，并把 chipStar 列为外部依赖：

[README.md:43-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L43-L45) —— `HIP` 与 OpenCL 1.2、Vulkan 并列为 Vortex 支持的编程模型。

[README.md:81](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L81) —— chipStar 出现在「依赖工具」清单里。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把「一条 HIP 调用的旅程」在脑中走一遍，建立分层心智模型。
2. **操作步骤**：打开 [docs/designs/hip_on_vortex_chipstar.md:21-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L21-L32) 的架构图，对照下表，把每一个 HIP 原语映射到它最终下沉到的 Vortex 层。

   | HIP 原语 | 经 chipStar 映射到 | 经 PoCL 下沉到（见 u12-l1） | 最终落到 |
   |---|---|---|---|
   | `hipMalloc` | `clCreateBuffer` | `vx_mem_alloc` | 设备显存分配（见 u3-2） |
   | `hipMemcpy(H2D)` | `clEnqueueWriteBuffer` | `vx_copy_to_dev` | CP 的 DMA 通路 |
   | `kernel<<<>>>` | `clEnqueueNDRangeKernel` | `vx_start` | 写 KMU DCR + CMD_LAUNCH（见 u3-4） |
   | `hipDeviceSynchronize` | `clFinish` | `vx_ready_wait` | 阻塞等待退休 |

3. **需要观察的现象**：你会发现 HIP 这一层的每一个原语，都能在 u12-l1 的 OpenCL 链路里找到一一对应——这印证了「HIP 只是套了件 OpenCL 外套」。
4. **预期结果**：能说出「为什么 Vortex 不需要为 HIP 写任何新运行时代码」——因为 `CHIP_BE=opencl` 让 chipStar 直接复用了 PoCL。
5. 本实践为源码阅读型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Vortex 选择 `chipStar → SPIR-V → POCL → Vortex` 这条「绕一圈」的路径，而不是为 HIP 写一个直接对接 `vortex.h` 的原生运行时？

> **参考答案**：因为这条路径能**完全复用 u12-l1 已铺好的 PoCL 层**，让 Vortex 的 `sw/` 运行时树对 HIP 零改动（设计文档明确说「untouched by HIP」）。代价是结构上无法暴露 Vortex 专有 intrinsic——这是为「工程复用」付出的「能力边界」代价，文档 §5 把原生工具链列为未实现的未来方向。

**练习 2**：在 `chipStar → SPIR-V → POCL → Vortex` 路径中，`.vxbin` 是在哪一步产生的？

> **参考答案**：在**运行期**由 POCL 把 SPIR-V JIT 成 `.vxbin`（用 clang + `vxbin.py`）。编译期产出的只是「内嵌 SPIR-V fatbin 的主机 ELF」，并不直接产出 `.vxbin`。这与 u12-l1 中 OpenCL 内核「运行时编译」的形态一致。

---

### 4.2 仓库内组件与外部工具链的分工

#### 4.2.1 概念说明

上一个模块说了「外部工具链」这个概念，本模块讲清楚**到底什么是仓库内的、什么是外部的**。设计文档给出了一张「仓库内组件表」，它揭示了一个刻意的设计取舍：

- **仓库内（in-tree）**：只有测试源码（`tests/hip/*`）、构建/运行引擎（`common.mk`）、CI 安装/回归胶水（`ci/chipstar_install.sh.in`、`ci/toolchain_install.sh.in`、`ci/testcases/hip.yaml`）。
- **外部（out-of-tree，版本化在外部仓库）**：hipcc 编译器、`libCHIP.so` 运行时、HIP 设备库（`hipspv-spirv{32,64}.bc`）、SPIR-V→Vortex 的 lowering、llvm_vortex、PoCL。

文档甚至特意指出：**仓库里没有任何 HIP 运行时垫片（shim）**——`sw/runtime/{device.cpp,vortex2.h,vortex-kernel.pc.in}` 里出现的 chipStar/hipcc 字样只是「给下游消费者命名」的注释，不是代码。

> 一个有用的判别原则：如果你在 `sw/` 下找不到任何 HIP 专有代码，那是因为「HIP 专有代码」根本不存在于本仓库——它活在 `vortexgpgpu/chipStar` 这个 fork 里。

#### 4.2.2 核心流程

外部工具链的「安装生命周期」是一条三段式的生产—打包—消费链：

```text
生产（producer）          打包（package）              消费（consumer）
ci/chipstar_install       ci/toolchain_prebuilt        ci/toolchain_install
.sh.in                    .sh.in --chipstar            .sh.in --chipstar
   │                          │                             │
   │ git clone chipStar       │ tar 打包 $TOOLDIR/chipstar   │ wget chipstar.tar.bz2
   │ vortex_3.x               │ 上传到 release 仓库          │ 解压到 $TOOLDIR/chipstar
   │ cmake 构建                │                              │
   ▼                          ▼                              ▼
$TOOLDIR/chipstar         chipstar.tar.bz2            $TOOLDIR/chipstar（hipcc + libCHIP.so）
（含 hipcc/libCHIP.so）
```

关键配置点是 chipStar 构建时的 `-DCHIP_TARGET_POINTER_WIDTHS="32;64"`：它让**单个** `libCHIP.so` 和 hipcc 同时服务于 rv32 与 rv64 两类 Vortex 设备，并产出两份设备库 bitcode（`hipspv-spirv32.bc` + `hipspv-spirv64.bc`）。这是 32 位支持能在工具链层面成立的根基（详见模块 4.4）。

大多数用户只跑「消费」这一步（拉预编译 tarball），永远不会运行生产脚本。

#### 4.2.3 源码精读

仓库内组件的权威清单是设计文档的组件表：

[docs/designs/hip_on_vortex_chipstar.md:42-49](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L42-L49) —— 这张表把每个仓库内路径（安装脚本、`common.mk`、两个测试、CI 目录）的职责一行说清。

紧跟着的「无运行时垫片」声明值得记住，它防止你在 `sw/` 里徒劳地找 HIP 代码：

[docs/designs/hip_on_vortex_chipstar.md:51-53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L51-L53) —— `sw/runtime` 里对 chipStar/hipcc 的引用只是注释，不是代码。

生产脚本 `ci/chipstar_install.sh.in` 的核心是这段 CMake 配置，注意双宽开关与对外部 llvm-vortex / PoCL 的依赖：

[ci/chipstar_install.sh.in:145-153](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in#L145-L153) —— 用 `$TOOLDIR/llvm-vortex` 的 clang/clang++ 构建 chipStar，开启 `CHIP_TARGET_POINTER_WIDTHS="32;64"`，并依赖 `llvm-config` 与 `llvm-spirv`。

而双宽开关本身定义在脚本顶部，是 32 位支持的源头：

[ci/chipstar_install.sh.in:61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in#L61) —— `CHIP_TARGET_POINTER_WIDTHS="32;64"`，一次构建产出 32/64 两套设备库 bitcode。

安装结束时的摘要明确列出了产物清单（hipcc、`libCHIP.so`、`hipspv-spirv{32,64}.bc`），这也是消费端会拿到的东西：

[ci/chipstar_install.sh.in:176-183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in#L176-L183) —— 产物清单与验证命令 `make -C tests/hip run-simx`。

消费端的拉取函数极其简短——就是 wget 一个 tarball 解压到 `$TOOLDIR`：

[ci/toolchain_install.sh.in:127-133](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L127-L133) —— `chipstar()` 函数：下载并解压预编译 chipStar。

PoCL 的拉取函数略多一步——它会重写 vendor `.icd` 文件指向重定位后的 `libpocl.so`，这是 ICD 平台发现机制能工作的前提（见 u12-l1）：

[ci/toolchain_install.sh.in:110-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/toolchain_install.sh.in#L110-L125) —— `pocl()` 函数：拉取 PoCL 并重生成 `pocl.icd`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲眼确认「仓库内无 HIP 运行时代码」这一边界结论。
2. **操作步骤**：
   - 阅读 [ci/chipstar_install.sh.in:61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in#L61) 与 [ci/chipstar_install.sh.in:176-183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/chipstar_install.sh.in#L176-L183)，记录 chipStar 安装后 `$TOOLDIR/chipstar` 下会有哪些产物。
   - 在仓库内搜索 `libCHIP` 或 `hipcc` 的**代码**引用（排除注释与 `.mk`/`.sh.in`），验证它们只出现在外部工具链调用处。
3. **需要观察的现象**：所有「HIP 专有逻辑」的引用都集中在 `ci/*.sh.in`（安装/打包）与 `tests/hip/common.mk`（调用 hipcc），`sw/` 下没有实现。
4. **预期结果**：能口头说出「chipStar 的 hipcc 在 `$CHIPSTAR_PATH/bin/hipcc`，运行时库在 `$CHIPSTAR_PATH/lib/libCHIP.so`，设备库在 `$CHIPSTAR_PATH/lib/hip-device-lib/hipspv-spirv{32,64}.bc`」。
5. 本实践为源码阅读型，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`CHIP_TARGET_POINTER_WIDTHS="32;64"` 这个 CMake 变量解决了什么问题？

> **参考答案**：它让**一次** chipStar 构建同时产出 32 位与 64 位两套设备库 bitcode（`hipspv-spirv32.bc` + `hipspv-spirv64.bc`），从而让单个 hipcc / `libCHIP.so` 能同时服务于 rv32 与 rv64 两类 Vortex 设备。没有它，就得为每个字长单独构建一套 chipStar。

**练习 2**：为什么说「在 `sw/runtime` 下找不到 HIP 运行时垫片」？

> **参考答案**：因为 HIP-on-Vortex 选了 `CHIP_BE=opencl`，HIP 主机调用被映射成 OpenCL 调用，再由 PoCL 下沉到 `vortex.h` 运行时。HIP 专有的运行时（`libCHIP.so`）是**外部**的，由 chipStar 提供；Vortex 仓库只负责测试源码与构建胶水。`sw/runtime` 里对 hipcc 的字样引用只是命名下游消费者的注释。

---

### 4.3 common.mk：真正的构建/运行引擎

#### 4.3.1 概念说明

如果说前两个模块是「地图」，本模块就是「发动机」。`tests/hip/common.mk` 是所有 `tests/hip/*` 测试共享的构建规则，它做三件事：

1. **构建主机二进制**：用 chipStar 的 hipcc 把 HIP 源码（主机 + 设备）一次编成内嵌 SPIR-V 的主机 ELF，链接 `libCHIP.so`。
2. **构建 Vortex 运行时驱动**：按目标后端 make 出对应的 `libvortex.so`（simx/rtlsim/opae/xrt）。
3. **运行**：在 POCL/chipStar 下跑这个主机 ELF，由 POCL 在运行时把 SPIR-V JIT 成 Vortex `.vxbin`。

本模块最重要的认知是「两套标志、两个时机」：

- **`HIPCC_FLAGS`（编译期，喂给 hipcc）**：决定主机 ELF 怎么编、SPIR-V 用什么指针宽度。
- **`VX_CFLAGS` / `VX_LDFLAGS` / `VX_BINTOOL`（运行期，由 POCL 喂给 clang）**：这些**不是**直接给 hipcc 用的，而是通过 `POCL_VORTEX_*` 环境变量在运行期传递——当 POCL 把 SPIR-V JIT 成 `.vxbin` 时，它用这些标志重新调用 clang + `vxbin.py`。这与 u12-l1 里 `tests/opencl/common.mk` 的形状一致（注释也明说「Same shape as tests/opencl/common.mk」）。

#### 4.3.2 核心流程

构建与运行的两条时间线：

```text
【构建期 make all / $(PROJECT)】
  hipcc（CHIPSTAR_PATH/bin/hipcc）
     + HIPCC_FLAGS：-std=c++17、--offload-pointer-width=$(XLEN)、--hip-path、
                    -L chipStar/lib -Wl,-rpath（链 libCHIP.so）
     + 先决条件：libvortex2.a（sw/kernel）、libvortex.so（sw/runtime/stub）
     ▼
  主机 ELF（PROJECT），内嵌 SPIR-V fatbin，符号链接 libCHIP.so / libvortex.so

【运行期 make run-simx】
  1. make 出后端 libvortex.so（sw/runtime/simx）
  2. 设环境：
     - OCL_ICD_VENDORS=$(POCL_PATH)/etc/OpenCL/vendors  ← 平台发现
     - VORTEX_DRIVER=simx                                ← stub 分发（见 u3-3）
     - POCL_CC_FLAGS：POCL_VORTEX_XLEN / POCL_VORTEX_CFLAGS / POCL_VORTEX_LDFLAGS
                      / POCL_VORTEX_BINTOOL / POCL_PATH_LLVM_SPIRV / POCL_IGNORE_CL_STD
  3. 执行 ./$(PROJECT)
     → libCHIP.so（CHIP_BE=opencl）→ POCL → JIT SPIR-V → riscv$XLEN → .vxbin
     → libvortex.so → Vortex 设备
```

关键机制详解：

- **平台发现（ICD）**：chipStar 链接系统 OpenCL ICD loader（`libOpenCL.so.1`）；PoCL 是 ICD-only 构建，自带一个 vendor `.icd` 名片文件。`OCL_ICD_VENDORS` 指向 PoCL 的 vendors 目录，loader 据此发现 Vortex 平台，**无需 `LD_PRELOAD` 垫片**。`OCL_ICD_LIB_DIR` 则把 ocl-icd loader 钉在主机上其它 vendor loader（如 CUDA、Xilinx XRT）之前。
- **JIT 标志注入**：POCL 在运行期需要 clang 把 SPIR-V 翻成 RISC-V 机器码并打包成 `.vxbin`。`VX_CFLAGS` 里那串 `--target=riscv$(XLEN)-unknown-elf`、`+xvortex` target-feature、`-DVX_CFG_XLEN=$(XLEN)`、链接 `libvortex2.a` 与 `baremetal/libclang_rt.builtins-riscv$(XLEN).a` 等，都是为这一步准备的。它们经 `POCL_VORTEX_CFLAGS/LDFLAGS/BINTOOL` 传给 POCL，形状与 u12-l1 的 OpenCL 路径相同。
- **CL 标准差异兜底**：chipStar 发出的 SPIR-V 带 `-cl-std=CL3.0`，而 Vortex 上报 OpenCL 1.2，POCL 默认会拒绝。`POCL_IGNORE_CL_STD=1` 关掉这个检查——对 SPIR-V 输入是安全的（内核已预编译，OpenCL C 版本号不适用）。
- **llvm-spirv 路径重定位**：预编译 `libpocl` 把构建机的 `llvm-spirv` 路径烧死在里面，换台机器就会 exec 失败、导致每个 HIP 程序构建中止。`POCL_PATH_LLVM_SPIRV=$(LLVM_PATH)/bin/llvm-spirv` 把它指回 llvm-vortex 的副本。

#### 4.3.3 源码精读

文件头部的注释本身就是一份「能力声明 + 已知陈旧说明」。注意第 5-7 行那段「chipStar HIP is currently rv64-only」——这是设计文档 §5 点名的**陈旧注释**（rv32 缺口早已补上，但这几行没同步更新）。这是一个很好的「批判性阅读源码」的例子：权威以设计文档为准，不要盲信代码注释。

[tests/hip/common.mk:1-12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L1-L12) —— 三件事的概述（hipcc→SPIR-V、构建运行时驱动、POCL/chipStar 运行），以及工具路径变量 `CHIPSTAR_PATH`/`HIPCC`。

`HIPCC_FLAGS` 里最关键的一行是 `--offload-pointer-width=$(XLEN)`——它决定内嵌 `.hipInfo` 选用的 SPIR-V 指针宽度，使发出的 SPIR-V 能被对应字长的 POCL Vortex 设备接受（POCL 在 rv64 上拒绝 Physical32，反之亦然）：

[tests/hip/common.mk:86-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L86-L91) —— `HIPCC_FLAGS` 与 `--offload-pointer-width=$(XLEN)`。

`VX_CFLAGS` 是「POCL 在 JIT 时重传给 clang 的设备侧标志」。注意 `+xvortex` target-feature（Vortex 专有 ISA 扩展，见 u4-2）、`-DVX_CFG_XLEN=$(XLEN)`（配置宏，见 u2）、以及按 XLEN 切换的 `march/mabi`：

[tests/hip/common.mk:41-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L41-L54) —— 设备侧编译标志，含 RISC-V target、Vortex target-feature、字长切换。

这些设备侧标志经 `POCC_CC_FLAGS`（即运行期环境 `POCL_CC_FLAGS`）传给 POCL。这一段集中体现了「两套标志、两个时机」的分工：

[tests/hip/common.mk:65-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L65-L84) —— POCL 运行期环境：`POCL_IGNORE_CL_STD`、`POCL_VORTEX_XLEN`、`POCL_PATH_LLVM_SPIRV`、`POCL_VORTEX_BINTOOL/CFLAGS/LDFLAGS`。

其中 `VX_BINTOOL` 把 `vxbin.py`（见 u4-1）交给 POCL，正是「SPIR-V → `.vxbin`」打包这步的工具：

[tests/hip/common.mk:60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L60) —— `VX_BINTOOL` 指向 `llvm-objcopy` + `vxbin.py`。

ICD 平台发现机制（与 u12-l1 一致）：

[tests/hip/common.mk:67-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L67-L75) —— `OCL_ICD_VENDORS` 指向 PoCL 的 vendor `.icd`，无需 `LD_PRELOAD` 垫片即可发现 Vortex 平台。

构建规则用「hipcc 一次调用同时处理主机 + 设备」产出内嵌 SPIR-V 的主机 ELF，并依赖先建好的 `libvortex2.a` 与 `libvortex.so`：

[tests/hip/common.mk:139-140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L139-L140) —— `$(PROJECT)` 规则：hipcc 一次编译出内嵌 SPIR-V 的主机 ELF。

`run-simx` 规则展示了完整的运行期环境编排：先 make 后端 `libvortex.so`，再设 `OCL_ICD_VENDORS` + `VORTEX_DRIVER=simx` + 一长串 `LD_LIBRARY_PATH` + `POCL_CC_FLAGS`，最后执行二进制：

[tests/hip/common.mk:142-144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L142-L144) —— `run-simx`：构建 simx 后端 + 设运行期环境 + 执行。其余 `run-rtlsim/run-opae/run-xrt` 形状相同，只换 `VORTEX_DRIVER` 与 FPGA 相关变量。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：理清「SPIR-V 是怎么在运行期变成 `.vxbin` 的」这条数据流。
2. **操作步骤**：
   - 在 [tests/hip/common.mk:41-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L41-L54) 找到 `VX_CFLAGS`，圈出 `--target=riscv$(XLEN)`、`+xvortex`、`-DVX_CFG_XLEN=$(XLEN)` 三项。
   - 在 [tests/hip/common.mk:60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L60) 找到 `VX_BINTOOL`（含 `vxbin.py`）。
   - 在 [tests/hip/common.mk:76-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L76-L84) 看它们如何被包进 `POCL_VORTEX_CFLAGS/LDFLAGS/BINTOOL` 传给 POCL。
   - 把这条链与 u4-1 的 `vxbin.py`、u3-4 的 `.vxbin` 加载联系起来。
3. **需要观察的现象**：`VX_CFLAGS` 并没有被 `hipcc` 直接使用，而是「打包」进了 `POCL_VORTEX_*` 环境变量——证明这些标志是给**运行期 POCL 的 JIT clang** 用的，不是给编译期 hipcc 用的。
4. **预期结果**：能画出 `SPIR-V →（POCL 调 clang，带 VX_CFLAGS）→ riscv 机器码 →（vxbin.py 打包）→ .vxbin` 这条链。
5. 本实践为源码阅读型，无需运行命令；若本地已按 u1-l3 装好工具链，可尝试 `make -C tests/hip/vecadd run-simx`（见 §5）。

#### 4.3.5 小练习与答案

**练习 1**：`VX_CFLAGS` 里的标志，是在编译期喂给 hipcc，还是在运行期喂给 POCL 调用的 clang？为什么？

> **参考答案**：在**运行期**喂给 POCL 调用的 clang。因为编译期 hipcc 只产出内嵌 SPIR-V 的主机 ELF，并不产出 RISC-V 机器码；真正的 RISC-V 机器码与 `.vxbin` 是运行期 POCL 把 SPIR-V JIT 时才生成的。`VX_CFLAGS` 经 `POCL_VORTEX_CFLAGS` 等环境变量传给 POCL，由 POCL 在 JIT 时重传给 clang。

**练习 2**：`POCL_IGNORE_CL_STD=1` 解决了什么冲突？为什么对 SPIR-V 输入是安全的？

> **参考答案**：chipStar 发出的 SPIR-V 带 `-cl-std=CL3.0`，而 Vortex 上报 OpenCL 1.2，POCL 默认会以 `CL_BUILD_PROGRAM_FAILURE` 拒绝。`POCL_IGNORE_CL_STD=1` 关掉版本检查。对 SPIR-V 输入安全，是因为此时内核已经预编译成 SPIR-V，OpenCL C 的源语言版本号已不适用。

---

### 4.4 tests/hip 测试套件与 rv32/rv64 双宽支持

#### 4.4.1 概念说明

`tests/hip` 下有四个 HIP 测试，但只有两个在默认回归里跑：

- **vecadd**：向量加法（`C = A + B`），1 维 grid/block，最简单的 HIP kernel。
- **sgemm**：矩阵乘（列主序 `C = A * B`），2 维 grid/block。
- **histogram / atomicreduce**：用到 `atomicAdd`（RVA 的 `amo*.w` 原子指令），**默认排除**，只有开启 A 扩展（`CONFIGS=-DVX_CFG_EXT_A_ENABLE`）时才跑。

排除机制由 `tests/hip/Makefile` 的 `TESTS`/`EXCLUDE` 表管理，与 u12-l1 里 OpenCL 测试受 `TESTS`/`EXCLUDE` 表管理的形状一致。

本模块的第二个主题是**双宽支持**：rv32 与 rv64 都能端到端跑 HIP。这是一个值得专门讲的「能力」，因为它的成立依赖于编译期、工具链、运行期三处的协同：

- **工具链层**：chipStar 用 `CHIP_TARGET_POINTER_WIDTHS="32;64"` 一次构建出双宽设备库（模块 4.2）。
- **编译期**：`common.mk` 传 `--offload-pointer-width=$(XLEN)`，让 hipcc 发出 `Physical32` 或 `Physical64` 的 SPIR-V。
- **运行期**：`POCL_VORTEX_XLEN=$(XLEN)` 让 POCL 的 Vortex 设备以正确字长（`address_bits=32/64`）接收对应 SPIR-V。

rv32 `vecadd` 和 `sgemm` 在 SimX 上 PASS；更广的 chipStar 一致性冒烟测试在 rv32 上是「混合」（约 36% 通过，长尾问题记录在 fork 的 `known-failures-vortex32.txt`）。

#### 4.4.2 核心流程

以 vecadd 为例，一个 HIP kernel 的完整生命周期：

```text
1. 主机准备：rand 生成 h_a/h_b，CPU 算参考解 h_ref
2. 设备分配：hipMalloc(d_a/d_b/d_c)            → clCreateBuffer → vx_mem_alloc
3. 上传：hipMemcpy(H2D) h_a→d_a, h_b→d_b       → clEnqueueWriteBuffer → vx_copy_to_dev
4. 查询设备能力：hipGetDeviceProperties         → 据 maxThreadsPerBlock 钳制 block_size
5. 启动：vecadd<<<grid,block>>>(...)             → clEnqueueNDRangeKernel → vx_start（写 KMU）
6. 同步：hipDeviceSynchronize                    → clFinish → vx_ready_wait
7. 取回：hipMemcpy(D2H) d_c→h_c                 → clEnqueueReadBuffer → vx_copy_from_dev
8. 校验：fp_close(h_c, h_ref)，打印 PASSED!/FAILED!
```

注意第 4 步「查询设备能力再钳制 block_size」——这与 u1-l4 里 demo 主机用 `vx_device_query` 查 `NUM_THREADS` 决定启动维度是同一个思想：**启动维度要适配设备的实际规模**。vecadd 把 block_size 钳到 `maxThreadsPerBlock`，sgemm 则把二维 `block_dim*block_dim` 钳到该上限。

双宽的指针宽度协同：

```text
编译期 hipcc：--offload-pointer-width=$(XLEN)
   ├─ XLEN=64 → Physical64 SPIR-V → POCL rv64 device（address_bits=64）接受
   └─ XLEN=32 → Physical32 SPIR-V → POCL rv32 device（address_bits=32）接受
运行期 POCL ：POCL_VORTEX_XLEN=$(XLEN) 必须与 SPIR-V 宽度一致，否则 POCL 拒绝
```

POCL 在 rv64 上拒绝 `Physical32`、在 rv32 上拒绝 `Physical64`，故编译期与运行期的 XLEN 必须匹配——这正是 `common.mk` 同时传 `--offload-pointer-width=$(XLEN)` 与 `POCL_VORTEX_XLEN=$(XLEN)` 的原因。

#### 4.4.3 源码精读

vecadd 的 kernel 本体——典型的 HIP `__global__` 函数，用 `blockIdx/blockDim/threadIdx` 算全局线程 id：

[tests/hip/vecadd/main.cpp:27-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp#L27-L32) —— vecadd kernel：`C[gid] = A[gid] + B[gid]`。

主机侧的「分配 + 上传」对应 CUDA 风格的 `hipMalloc`/`hipMemcpy`：

[tests/hip/vecadd/main.cpp:80-86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp#L80-L86) —— `hipMalloc` 三个设备缓冲 + `hipMemcpy` 上传源数据。

启动前查询设备能力以钳制 block_size（与 u1-l4 的 `vx_device_query` 思想一致）：

[tests/hip/vecadd/main.cpp:91-100](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp#L91-L100) —— `hipGetDeviceProperties` 取 `maxThreadsPerBlock`，钳制 block_size。

HIP 风格的 `<<<grid,block>>>` 启动语法，后接 `hipDeviceSynchronize`：

[tests/hip/vecadd/main.cpp:103-104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp#L103-L104) —— `vecadd<<<dim3(grid),dim3(block)>>>` 启动 + `hipDeviceSynchronize`。

sgemm 是 2 维版本，kernel 用双层 `blockIdx.x/y` + `threadIdx.x/y` 算行列号，做 K 维归约：

[tests/hip/sgemm/main.cpp:29-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/sgemm/main.cpp#L29-L39) —— sgemm kernel：列主序 `C[col*N+row] += A[k*N+row]*B[col*N+k]`。

[tests/hip/sgemm/main.cpp:112-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/sgemm/main.cpp#L112-L123) —— sgemm 启动：把二维 `block_dim*block_dim` 钳到 `maxThreadsPerBlock` 后用 `<<<grid,block>>>` 启动。

测试套件的聚合与排除——`histogram`/`atomicreduce` 因依赖 A 扩展默认排除：

[tests/hip/Makefile:5-11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/Makefile#L5-L11) —— `TESTS := vecadd sgemm histogram atomicreduce`，`EXCLUDE := histogram atomicreduce`（注释说明它们用 `atomicAdd`，需 A 扩展）。

双宽支持的设计文档说明，记录了 rv32 端到端可用的现状与一致性长尾：

[docs/designs/hip_on_vortex_chipstar.md:57-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/hip_on_vortex_chipstar.md#L57-L74) —— §3 32 位支持：`--offload-pointer-width` + `POCL_VORTEX_XLEN` + 双宽设备库三者协同；rv32 vecadd/sgemm 在 SimX 上 PASS，更广冒烟测试约 36% 通过。

CI 目录把 `hip` 列为一个测试分类，按驱动（simx/rtlsim/opae/xrt）声明 `run-{driver}`：

[ci/testcases/hip.yaml:1-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/hip.yaml#L1-L33) —— `hip` 分类，每个驱动一条 `make-run` 用例，默认 xlen 同时跑 32 与 64、tier=smoke。

#### 4.4.4 代码实践（可运行，附源码阅读回退）

1. **实践目标**：在 SimX 上跑通一个 HIP kernel，并解释它的编译与执行。
2. **操作步骤**：
   - 前置：按 u1-l3 / u2 装好工具链（含 `--chipstar` 与 `--pocl`），并在 `build/` 里 `../configure --xlen=64`。
   - 运行：`make -C tests/hip/vecadd run-simx`（等价于 `./ci/regression.sh --test hip` 中 vecadd 部分）。
   - 换 32 位：在另一棵 `build32/` 树里 `../configure --xlen=32`，再 `make -C tests/hip/vecadd run-simx`，观察 rv32 也能跑通。
3. **需要观察的现象**：标准输出依次打印 `Allocate device buffers` → `Upload source buffers` → `Execute the kernel 'vecadd'` → `block_size=... (device max=...)` → `Elapsed time: ... ms` → `Download destination buffer` → `Verify result` → `PASSED!`。
4. **预期结果**：退出码为 0，末尾打印 `PASSED!`。rv32 与 rv64 均应通过（设计文档确认 vecadd/sgemm 在 SimX 上 PASS）。
5. **若本地无法运行**（工具链未装）：改为源码阅读型——对照 [tests/hip/vecadd/main.cpp:27-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/vecadd/main.cpp#L27-L32) 的 kernel 与 [tests/hip/common.mk:142-144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L142-L144) 的运行规则，**笔头**推演一次 `hipMalloc→hipMemcpy→<<<>>>→hipDeviceSynchronize` 在分层路径上的下沉过程（参考模块 4.1.4 的映射表），并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `histogram` 和 `atomicreduce` 默认被排除在回归之外？

> **参考答案**：因为它们用了 `atomicAdd`，对应 RISC-V「A」原子扩展（`amo*.w`）。默认回归构建的是「无原子」配置，故在 `tests/hip/Makefile` 的 `EXCLUDE` 里排除；只有显式带 `CONFIGS="-DVX_CFG_EXT_A_ENABLE"` 时才跑。

**练习 2**：rv32 与 rv64 双宽支持，需要在哪三处保持 XLEN 一致？

> **参考答案**：① 工具链层 chipStar 用 `CHIP_TARGET_POINTER_WIDTHS="32;64"` 一次构建出双宽设备库；② 编译期 hipcc 传 `--offload-pointer-width=$(XLEN)` 发出对应宽度的 SPIR-V；③ 运行期 POCL 传 `POCL_VORTEX_XLEN=$(XLEN)` 以匹配字长的设备接收。三处必须一致，否则 POCL 会因 Physical32/64 与设备 `address_bits` 不匹配而拒绝。

**练习 3**：vecadd 主机为什么在启动前要调 `hipGetDeviceProperties`？

> **参考答案**：为了把 `block_size` 钳到设备的 `maxThreadsPerBlock` 之内，避免在小规模设备上触发 `hipErrorLaunchFailure`。这与 u1-l4 里 demo 主机用 `vx_device_query` 查 `NUM_THREADS` 决定启动维度是同一思想：启动维度要适配设备实际规模。

---

## 5. 综合实践

**任务**：用 `tests/hip` 中的 sgemm/vecadd 例子，完整解释一个 HIP kernel 从源码到在 Vortex 上执行的全过程，并画出分层路径图。

建议步骤：

1. **读源码**：打开 [tests/hip/sgemm/main.cpp:29-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/sgemm/main.cpp#L29-L39) 的 sgemm kernel 与 [tests/hip/sgemm/main.cpp:112-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/sgemm/main.cpp#L112-L123) 的启动代码，确认它就是一个标准 HIP 程序（`__global__` + `hipMalloc` + `<<<>>>`）。

2. **画编译期路径**：从 `main.cpp` 出发，经过 chipStar 的 hipcc（[tests/hip/common.mk:139-140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L139-L140)），产出「内嵌 SPIR-V fatbin 的主机 ELF」。标注 hipcc 用到的关键开关 `--offload-pointer-width=$(XLEN)`（[tests/hip/common.mk:86-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L86-L91)）。

3. **画运行期路径**：从 `./sgemm` 启动出发，经过 `libCHIP.so`（`CHIP_BE=opencl`）→ POCL → 把 SPIR-V JIT 成 `.vxbin`（用 clang 带 `VX_CFLAGS` + `vxbin.py`，见 [tests/hip/common.mk:41-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L41-L84)）→ `libvortex.so`（`VORTEX_DRIVER=simx`，见 [tests/hip/common.mk:142-144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/hip/common.mk#L142-L144)）→ Vortex 设备。

4. **标注复用关系**：在图上用虚线圈出「这一段就是 u12-l1 的 PoCL 层」，指出 HIP 之所以能跑，是因为 chipStar 选了 `CHIP_BE=opencl`，免费搭车 PoCL。

5. **标注能力边界**：在图上注明「SPIR-V 这一层结构上无法表达 Vortex 专有 intrinsic（WMMA/WGMMA/TMA）」，这是 chipStar 路径的固有取舍。

6. **（可选）运行验证**：`make -C tests/hip/sgemm run-simx`，确认打印 `PASSED!`；再用 `--xlen=32` 的树跑一次 rv32，验证双宽。

**交付物**：一张分层路径图（编译期 + 运行期两栏）+ 一段说明，解释「为什么 Vortex 的 `sw/` 树对 HIP 零改动，HIP 程序却仍能跑」。

---

## 6. 本讲小结

- HIP-on-Vortex 的路径是 `chipStar → SPIR-V → POCL → Vortex`：chipStar 把 HIP 编成内嵌 SPIR-V 的主机 ELF，POCL 在运行时把 SPIR-V JIT 成 Vortex 的 `.vxbin`，最后经 `libvortex.so` 在 simx/rtlsim/opae/xrt 上执行。
- HIP 之所以能跑而 `sw/` 树零改动，是因为 chipStar 选了 `CHIP_BE=opencl`——HIP 主机调用被映射成 OpenCL 调用，免费复用了 u12-l1 已铺好的 PoCL 层。
- 仓库内（in-tree）只有测试源码、`tests/hip/common.mk` 构建引擎和 CI 胶水；hipcc / `libCHIP.so` / 设备库全是外部工具链，由 `ci/chipstar_install.sh.in`（生产）→ `ci/toolchain_prebuilt`（打包）→ `ci/toolchain_install.sh.in --chipstar`（消费）三段式安装到 `$TOOLDIR`。
- `common.mk` 的核心是「两套标志、两个时机」：`HIPCC_FLAGS`（编译期喂 hipcc）与 `VX_CFLAGS/VX_LDFLAGS/VX_BINTOOL`（运行期经 `POCL_VORTEX_*` 喂给 POCL 调用的 clang + `vxbin.py`）。
- rv32/rv64 双宽支持依赖三处 XLEN 协同：工具链 `CHIP_TARGET_POINTER_WIDTHS="32;64"`、编译期 `--offload-pointer-width=$(XLEN)`、运行期 `POCL_VORTEX_XLEN=$(XLEN)`。
- 已知边界：chipStar/SPIR-V 路径结构上无法暴露 Vortex 专有 intrinsic（WMMA/WGMMA/TMA），原生 HIP 工具链（`libhip_vortex` / MLIR）是未实现方向；`common.mk` 头部「rv64-only」注释是设计文档点名的陈旧注释，以文档为准。

---

## 7. 下一步学习建议

- **横向对照 OpenCL 路径**：回到 u12-l1，把 `tests/opencl/common.mk` 与本讲的 `tests/hip/common.mk` 并排读，体会「`VX_CFLAGS` + `POCL_VORTEX_*` + `vxbin.py`」这套 JIT 管线是如何被两条上层语言（OpenCL、HIP）共享的。
- **向下追到 `.vxbin` 与 KMU**：POCL JIT 出的 `.vxbin` 与原生 Vortex 内核用同一镜像格式，启动都走 KMU。可结合 u3-4（`.vxbin` 加载）与 u4-1（`__vx_cta_entry` 入口）理解「为什么 POCL 产出的 `.vxbin` 能被设备侧运行时无差别启动」。
- **理解能力边界的来由**：若你对「让 HIP 暴露 TCU/DXA intrinsic」感兴趣，可阅读 `docs/proposals/hip_support_proposal.md`（设计文档 §5 提到的未实现原生工具链方向），对比 chipStar 路径与原生路径的取舍。
- **图形栈的平行故事**：u12-l2（Vulkan/mesa）是与 HIP 平行的「上层语言适配」案例，但走的是图形（custom-1 opcode）而非计算（OpenCL/SPIR-V）路径，可对照阅读以理解 Vortex 支持多种上层语言的不同策略。
