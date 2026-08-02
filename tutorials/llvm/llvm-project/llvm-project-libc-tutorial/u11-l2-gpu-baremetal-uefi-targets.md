# 特殊目标：GPU、baremetal 与 UEFI

## 1. 本讲目标

本讲把视线从「主流的 Linux Full 构建」移开，考察 LLVM-libc 三类**非主流目标**：GPU、baremetal、UEFI。前几讲（尤其 [u1-l4 构建模式](u1-l4-build-modes-overlay-vs-full.md) 与 [u8-l2 程序启动流程](u8-l2-program-startup-crt.md)）建立的认知是：libc 走 runtimes 交叉构建、用 `crt1.o`+`do_start` 装配 `main`。本讲要回答的是——**当目标环境根本没有内核、没有 ELF 链接器、甚至没有单一程序入口时，这套机制如何变形**。

学完后你应能做到：

- 说清 GPU 目标如何借 offloading 运行时让 GPU **内核**调用 libc，以及它为何必须用 `llvm-link` 把入口点合并成单一 bitcode；
- 解释 `llvm_link_bitcode` 这条 CMake 规则做了什么、为什么它取代了旧的链接器 `-r`/`-flto` 方案；
- 描述 baremetal 最小足迹构建与「自带 startup」的真实形态（含 MMU、scatter-loading）；
- 说出 UEFI 固件应用的入口约定（`EfiMain`）与当前支持范围；
- 对照三类目标的「程序入口」根本差异，理解 LLVM-libc「retargetable（可重定向）」设计的落点。

## 2. 前置知识

阅读本讲前，请先确认以下概念（均在前置讲义中建立）：

- **Full 模式与 runtimes 交叉构建**：`LLVM_LIBC_FULL_BUILD=ON` 产出独立的 `libc.a`/`libm.a`，通过目标三元组（target triple）驱动交叉构建（见 [u1-l4](u1-l4-build-modes-overlay-vs-full.md)）。
- **入口点（entrypoint）与静态库聚合**：每个公开函数是独立构建单元，`add_entrypoint_library` 把一批入口点聚合成 `.a`（见 [u2-l3 CMake 构建规则详解](u2-l3-cmake-build-rules.md)）。
- **程序启动链 `crt1.o → _start → do_start → main`**：Linux 上 `crt1.o` 由多个可重定位对象合并而成，`do_start` 初始化 TLS 后调用 `main`（见 [u8-l2](u8-l2-program-startup-crt.md)）。
- **平台配置树 `config/<os>/<arch>/`**：决定「某平台支持哪些函数」（见 [u2-l4](u2-l4-platform-config.md)）。

本讲会反复用到一个新名词：

- **bitcode（`.bc`）**：LLVM IR 的二进制序列化形式。GPU 目标不以机器码 `.o` 为最终形态，而是把整个库链成**一个** `.bc`（单一 LLVM IR 模块），交给设备工具链在 LTO 阶段一并优化。`llvm-link` 就是把多个 `.bc`/`.o` 合并成单一 IR 模块的工具。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/gpu/building.rst` `docs/gpu/using.rst` `docs/gpu/motivation.rst` | GPU 构建方式、两种使用模式、能力与限制 |
| `docs/uefi/building.rst` `docs/uefi/using.rst` | UEFI 构建命令与支持现状 |
| `startup/gpu/start.cpp` `startup/gpu/CMakeLists.txt` | GPU 启动内核 `_begin`/`_start`/`_end` 及其构建 |
| `startup/baremetal/aarch64/start.cpp` `startup/baremetal/init.cpp` `startup/baremetal/fini.cpp` `startup/baremetal/CMakeLists.txt` | baremetal 自带启动：`_start`→`do_start`、`init/fini_array`、MMU |
| `startup/uefi/crt1.cpp` `startup/uefi/CMakeLists.txt` `config/uefi/app.h` | UEFI 固件入口 `EfiMain` 与应用属性 |
| `cmake/modules/LLVMLibCLibraryRules.cmake` | `llvm_link_bitcode` / `add_bitcode_entrypoint_library` 规则 |
| `lib/CMakeLists.txt` | GPU 目标额外生成 `.bc` 库 |
| `CMakeLists.txt`（顶层） | GPU 构建强制要求 `llvm-link` |

## 4. 核心概念与源码讲解

### 4.1 GPU 目标与 offload 模型

#### 4.1.1 概念说明

GPU 支持（AMDGPU `amdgcn-amd-amdhsa`、NVPTX `nvptx64-nvidia-cuda`）的目标是「让 GPU 加速器上也能用一部分 C 标准库」。它有两层含义：

1. **作为 offloading 语言（CUDA/HIP/OpenMP）的设备端补充库**：用户在主机代码里调用 `printf`、`strlen`，编译器把这些调用编译进 GPU 内核，链接时把 GPU 版 libc 拉进来。这是「把 GPU 当作需要标准系统工具的目标」，类似各厂商自带的设备库。
2. **把 GPU 当作 hosted（托管）目标直接编译**：用 `clang --target=amdgcn-amd-amdhsa` 像交叉编译 CPU 那样，给 GPU 提供完整的「`crt1.o` + `libc.a`」组合，配一个 `amdhsa-loader`/`nvptx-loader` 把可执行文件在 GPU 上启动。这一模式主要用于在 GPU 上直接跑单元测试。

GPU 因没有「系统 libc」可回退，天生走 Full 模式（`LLVM_LIBC_FULL_BUILD` 必须为 ON）。

GPU 的能力受执行模型硬约束：OpenCL 式执行模型无法安全提供互斥锁，因此**文件缓冲、线程、locale、time 等不实现**；`errno` 不能用线程局部存储（TLS），只能做成**原子且全局**的。需要主机服务的函数（如 `printf` 输出、`fopen`）则通过 **RPC（远程过程调用）** 回到主机执行。

#### 4.1.2 核心流程

GPU 库的产出有三种形态，对应两种使用方式：

```text
                ┌─────────────────────────────────────────────┐
源码 entrypoints │  add_entrypoint_library ──► libc.a (IR 归档) │
(.cpp 编译成 IR) │                          └─► libm.a        │  直接编译 / hosted 测试
                │  add_bitcode_entrypoint_library             │
                │        └─► libc.bc (单一 IR 模块) ───────────│  设备库 / LTO
                │  add_startup_object(crt1) ──► crt1.o (IR)    │  启动内核
                └─────────────────────────────────────────────┘
   offloading 使用：clang -fopenmp --offload-arch=gfx90a ...   工具链自动链 libc.a + 跑 RPC server
   hosted 使用：    clang --target=amdgcn-amd-amdhsa -flto -lc crt1.o；再用 loader 启动
```

关键点：GPU 的 `.a`/`.o` 装的是 **LLVM IR（bitcode）**，不是机器码。AMDGPU 因 `lld` 尚未完全支持 ELF 链接，**始终要求 `-flto`**；最终可执行文件靠 loader（`amdhsa-loader`/`nvptx-loader`）拉起。

#### 4.1.3 源码精读

GPU 的支持函数表与限制在文档中诚实标注——大量函数需要 RPC：

- 文档说明 GPU 只实现 C 库子集，`errno` 是「原子且全局」、不提供互斥锁与文件缓冲：[docs/gpu/motivation.rst:L41-L47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/gpu/motivation.rst#L41-L47)（说明 GPU 执行模型的硬限制）。
- `stdio.h` 的 `printf`/`fopen`/`fread` 等标 `RPC Required`，意味着它们要回到主机执行：[docs/gpu/support.rst:L215-L254](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/gpu/support.rst#L215-L254)（标注每个函数是否需要 RPC）。
- 两种使用模式（offloading 补充库 vs 直接编译托管目标）的定义：[docs/gpu/using.rst:L15-L20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/gpu/using.rst#L15-L20)。
- AMDGPU 直接编译示例（注意 `-flto` 强制与 `crt1.o` 显式链接）：[docs/gpu/using.rst:L166-L169](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/gpu/using.rst#L166-L169)。
- 安装产物清单，其中 `libc.bc` 被描述为「单一 LLVM-IR bitcode blob，可像 NVIDIA/AMD 设备库那样使用」：[docs/gpu/building.rst:L154-L160](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/gpu/building.rst#L154-L160)。

构建侧的关键事实是：GPU 目标在配置期就把 `llvm-link` 列为**强制依赖**，找不到即 `FATAL_ERROR`：

- 顶层 `CMakeLists.txt` 在 `LIBC_TARGET_OS_IS_GPU` 时 `find_program` 定位 `llvm-link`，缺失则报致命错误：[CMakeLists.txt:L298-L304](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L298-L304)（说明 llvm-link 是 GPU 构建的必需工具）。

#### 4.1.4 代码实践

**实践目标**：通过阅读文档，建立「GPU 库 = IR 归档 + 单一 bitcode blob + IR 启动对象 + loader」的直觉。

**操作步骤**：

1. 打开 `docs/gpu/building.rst` 的「Build overview」一节（约 L130-L186）。
2. 列出 GPU 安装会产生的全部产物，并按用途分类（IR 归档 / bitcode blob / 启动对象 / loader / wrapper 头 / RPC server）。
3. 打开 `docs/gpu/support.rst`，统计 `stdio.h` 下「RPC Required」的函数占比，体会「GPU 上 stdio 基本是主机代理」。

**需要观察的现象**：`libc.a`、`libc.bc`、`crt1.o` 三者都强调「LLVM-IR」，没有任何一个是 GPU 机器码——这印证了 GPU 链接靠 LTO 在最后一步才生成机器码。

**预期结果**：你会得出「GPU 目标把『库』重新定义为『一份可被设备工具链内联优化的 IR』」的结论。如本地有 GPU 与已安装的 LLVM，可尝试文档中的 OpenMP 示例 `clang openmp.c -fopenmp --offload-arch=gfx90a`，否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GPU 目标「天生」走 Full 模式，而不像 Linux 那样有 Overlay 模式？
**答案**：Overlay 模式依赖「覆盖系统 libc 的少数符号、其余回退到系统 libc」。GPU 上根本没有「系统 libc」可回退，必须自带全部所需实现，因此只能是 Full。

**练习 2**：AMDGPU 示例命令里为什么必须带 `-flto`？
**答案**：因为 `lld` 对 AMDGPU 的 ELF 链接支持尚不完整，库以 bitcode 形式交付，需要在链接期做 LTO 才能生成最终 GPU 机器码（见 `docs/gpu/using.rst` 的 AMDGPU 小节）。

### 4.2 llvm_link_bitcode 规则：把入口点合并为单一 bitcode

#### 4.2.1 概念说明

上一模块提到 GPU 会产出 `libc.bc`——一份**单一 LLVM IR 模块**。把成百上千个入口点（每个编译成独立 `.o`/IR）「焊」成一个模块的工作，由 CMake 规则 `llvm_link_bitcode` 完成，底层调用 LLVM 工具 `llvm-link`。

为什么要焊成单一模块？因为设备工具链（尤其 NVPTX 的 `nvlink` 包装器、AMDGPU 的 LTO）需要的是「一个自包含的 IR 模块」，而不是一个普通归档。`llvm-link` 会做模块间的内联与符号解析，把跨翻译单元的调用**在 IR 层面**消解掉。

`llvm_link_bitcode` 是一条**通用**规则：它既被用来生成库（`libc.bc`），也被 GPU 的 `add_startup_object` 用来生成 `crt1.o`。这正是「llvm-link 已成为 GPU 构建的必需工具」的代码体现。

#### 4.2.2 核心流程

两条 CMake 规则的分工：

```text
llvm_link_bitcode(target)        # 原语：把若干 IR 输入链接成一个 .bc 输出
   └─ add_custom_command: ${LIBC_LLVM_LINK} ${INPUTS} -o ${OUTPUT}
   └─ add_custom_target:  声明为 ALL 目标，挂 TARGET_FILE 属性

add_bitcode_entrypoint_library(target, base)   # 高层：基于已有静态库生成 .bc
   └─ llvm_link_bitcode(OUTPUT <dir>/<target>.bc, INPUTS $<TARGET_FILE:<base>>)
        # base 是 add_entrypoint_library 产出的 .a；把整个归档喂给 llvm-link
```

库层（`lib/CMakeLists.txt`）在 GPU 目标下，对每个归档（`libc`/`libm`/`libmvec`）额外调用 `add_bitcode_entrypoint_library`，产出同名的 `.bc`。

#### 4.2.3 源码精读

- `llvm_link_bitcode` 的定义：用 `cmake_parse_arguments` 解析 `OUTPUT/INPUTS/DEPENDS`，核心是一条 `add_custom_command`，命令是 `${LIBC_LLVM_LINK} ${ARG_INPUTS} -o ${ARG_OUTPUT}`，并挂上注释与 `TARGET_FILE` 属性：[cmake/modules/LLVMLibCLibraryRules.cmake:L93-L109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L93-L109)（定义 llvm-link 包装原语）。
- `add_bitcode_entrypoint_library` 把 `base_target_name`（一个 `.a` 归档）的 `TARGET_FILE` 作为 `llvm-link` 的输入，输出到库目录下的 `<target>.bc`：[cmake/modules/LLVMLibCLibraryRules.cmake:L111-L125](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L111-L125)（在静态归档之上叠出 bitcode 库）。
- 库层调用点：仅在 `LIBC_TARGET_OS_IS_GPU` 时，对每个归档额外生成 bitcode 版本，并设 `OUTPUT_NAME` 为 `<archive>.bc` 以便安装：[lib/CMakeLists.txt:L43-L56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/lib/CMakeLists.txt#L43-L56)（GPU 独有的 bitcode 库装配）。
- GPU 启动对象也走 `llvm-link`：`add_startup_object` 在 `LLVM_ENABLE_PER_TARGET_RUNTIME_DIR` 时，对 `crt1` 调用 `llvm_link_bitcode`，把对象文件合并成可安装的 `crt1.o`：[startup/gpu/CMakeLists.txt:L30-L37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/gpu/CMakeLists.txt#L30-L37)（启动对象经 llvm-link 物化为单一 .o）。

#### 4.2.4 代码实践

**实践目标**：跟踪 `libc.bc` 从「入口点对象」到「单一 bitcode blob」的完整生成路径。

**操作步骤**：

1. 在 `lib/CMakeLists.txt` 找到 `if(LIBC_TARGET_OS_IS_GPU)` 块（L43-L56），确认 `add_bitcode_entrypoint_library(libcbitcode libc ...)`。
2. 跟进 `cmake/modules/LLVMLibCLibraryRules.cmake` 的 `add_bitcode_entrypoint_library`（L119-L125），看它如何把 `libc`（一个 `.a`）的 `TARGET_FILE` 喂给 `llvm_link_bitcode`。
3. 再看 `llvm_link_bitcode`（L93-L109）的 `add_custom_command`，确认最终命令形如 `llvm-link <libc.a> -o .../libc.bc`。

**需要观察的现象**：bitcode 库的输入不是「一堆 `.o`」，而是「一个已经聚合好的 `.a` 归档」——即先走标准的 `add_entrypoint_library` 聚合，再让 `llvm-link` 把整个归档压成单一 IR 模块。

**预期结果**：你能画出 `entrypoint object → add_entrypoint_library(libc.a) → add_bitcode_entrypoint_library(libc.bc)` 的三段管线。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_bitcode_entrypoint_library` 的输入是 `$<TARGET_FILE:${base_target_name}>`（一个归档文件），而不是 `$<TARGET_OBJECTS:...>`（对象列表）？
**答案**：因为它复用了 `add_entrypoint_library` 已经聚合好的 `.a`。`llvm-link` 能直接吃归档并把其中所有成员模块链接成一个 IR 模块，无需在调用点重新枚举对象。

**练习 2**：顶层 `CMakeLists.txt` 为何对 GPU 目标 `find_program(LIBC_LLVM_LINK)` 并在缺失时报 `FATAL_ERROR`？
**答案**：因为 GPU 的库（`libc.bc`/`libm.bc`）与启动对象（`crt1.o`）都依赖 `llvm-link` 物化，没有它 GPU 构建根本无法产出可用产物（见 4.1.3 引用的 L298-L304）。

### 4.3 baremetal 最小足迹构建

#### 4.3.1 概念说明

baremetal（裸机）目标指「没有操作系统、没有内核 syscall」的环境，典型场景是上电后直接跑在 CPU 上的固件式程序（如 QEMU 模拟的 AArch64/ARM 板）。它的特点是：

- **最小足迹**：不带线程、不带文件系统相关的入口点，构建关闭单元测试（`LIBC_ENABLE_UNITTESTS OFF`），只保留纯算法与必要启动。
- **自带 startup**：因为没有 `crt1.o` 来自系统的传统链，baremetal 在 `startup/baremetal/` 下提供自己的 `_start`→`do_start`，且 `do_start` 要亲自做硬件级初始化（设置异常向量、配置 MMU 页表、scatter-loading 拷 `.data`/清 `.bss`）。
- **依赖链接脚本提供的符号**：`__stack`、`__data_source/start/size`、`__bss_start/size` 等地址由链接脚本（linker script）给出，启动代码据此搬运内存。

baremetal 的 `crt1.o` 与 Linux 不同：它不是「多个对象 relocatable 合并」的产物，而通常就是架构目录下一个 `start.cpp` 编译出的对象（外加 `init`/`fini` 入口点）。

#### 4.3.2 核心流程

以 AArch64 baremetal 为例的启动链：

```text
上电 → _start (naked, .text.init.enter)
         │  mov sp, &__stack          ; 仅设栈指针
         │  bl do_start
         ▼
       do_start (LIBC_NAMESPACE)
         │  ① 设置 VBAR_EL1 → vector_table       ; 异常向量
         │  ② setup_mmu()                         ; 建 1GiB 块页表、开 MMU
         │  ③ 可选：开 FP/SVE 访问 (CPACR_EL1)
         │  ④ memcpy(.data) + memset(.bss)        ; scatter-loading
         │  ⑤ __libc_init_array()                 ; 跑 preinit/init 构造函数
         │  ⑥ _platform_init()                    ; weak 半主机初始化钩子
         │  ⑦ atexit(__libc_fini_array)           ; 登记 exit 时反向析构
         └─ ⑧ exit(main(0, 0))                    ; 进入用户代码
```

`__libc_init_array` 正向遍历 `__preinit_array` + `__init_array`；`__libc_fini_array` **反向**遍历 `__fini_array`（与析构语义一致）。

#### 4.3.3 源码精读

- 架构相关 `_start` 是裸函数（`gnu::naked`），只做「设栈指针 + 跳 `do_start`」两件事：[startup/baremetal/aarch64/start.cpp:L190-L196](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/aarch64/start.cpp#L190-L196)（最小程序入口）。
- `do_start` 的全流程：设异常向量、`setup_mmu()`、scatter-loading（`memcpy`/`memset`）、`__libc_init_array()`、`atexit` 登记 `__libc_fini_array`，最后 `exit(main(0,0))`：[startup/baremetal/aarch64/start.cpp:L156-L187](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/aarch64/start.cpp#L156-L187)（baremetal 的 do_start）。
- scatter-loading 与构造函数调用这两步：[startup/baremetal/aarch64/start.cpp:L178-L186](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/aarch64/start.cpp#L178-L186)（拷 `.data`、清 `.bss`、跑 init_array、登记 fini、进 main）。
- `__libc_init_array` 遍历 preinit 与 init 数组，正向调用每个构造函数：[startup/baremetal/init.cpp:L18-L25](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/init.cpp#L18-L25)（C++ 全局构造的标准触发点）。
- `__libc_fini_array` **反向**遍历 fini 数组：[startup/baremetal/fini.cpp:L18-L22](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/fini.cpp#L18-L22)（析构逆序约定）。
- baremetal 的 `crt1` 是「架构子目录里那个 `crt1`」的 `ALIAS`，构建系统按 `LIBC_TARGET_ARCHITECTURE` 进入对应子目录，找不到则只发警告：[startup/baremetal/CMakeLists.txt:L54-L66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/baremetal/CMakeLists.txt#L54-L66)（架构分派 + ALIAS + 缺架构降级为警告）。

#### 4.3.4 代码实践

**实践目标**：理解 baremetal 启动代码如何「不依赖内核」地完成 Linux `do_start` 由内核代劳的工作。

**操作步骤**：

1. 对照 [u8-l2](u8-l2-program-startup-crt.md) 中 Linux `do_start` 的清单（解析 argv/envp/auxv、算 load bias、定位 PT_TLS、init_tls、set_thread_ptr、atexit、init_array、main）。
2. 在 `startup/baremetal/aarch64/start.cpp` 的 `do_start`（L156-L187）里逐项找：baremetal 做了哪些、省略了哪些。
3. 特别注意 baremetal **没有 TLS 初始化、没有 argv/envp 解析**——它直接 `main(0, 0)`。

**需要观察的现象**：baremetal 把 Linux 交给内核的「内存就绪」工作（MMU、`.data`/`.bss`）自己扛了，但完全不做 TLS/线程相关初始化，因为它就是单线程裸机环境。

**预期结果**：列出 baremetal `do_start` 相对 Linux `do_start` 的「补做项」（MMU、scatter-load）与「省略项」（TLS、auxv、argv）。

#### 4.3.5 小练习与答案

**练习 1**：baremetal 的 `_start` 为什么是 `gnu::naked` 且基本只有两条汇编指令？
**答案**：上电时栈未初始化、没有 C 运行时可用，`naked` 函数避免编译器生成 prologue/epilogue（会误用栈），所以只能「设 sp + 跳转」二步走，把真正的初始化交给 `do_start`（L190-L196）。

**练习 2**：`__libc_fini_array` 为什么反向遍历，而 `__libc_init_array` 正向遍历？
**答案**：构造与析构是栈式配对——后构造的对象应先析构。故 init 正向、fini 反向（对比 init.cpp L18-L25 与 fini.cpp L18-L22）。

### 4.4 UEFI 固件应用支持

#### 4.4.1 概念说明

UEFI（统一可扩展固件接口）是 PC 启动后、操作系统加载前的固件环境。LLVM-libc 的 UEFI 支持目标是「为 UEFI 协议提供一个标准 libc 前端」，让现有应用更容易移植成 UEFI 镜像（`.efi`，PE 格式）。

UEFI 的程序模型与 CPU 程序截然不同：

- **入口不是 `_start`/`main`，而是 `EfiMain(ImageHandle, SystemTable)`**：固件加载镜像后直接调用 `EfiMain`，把「镜像句柄」与「系统表」两个 UEFI 核心对象作为参数传入。所有 UEFI 服务（输出、文件、内存分配）都经由系统表上的函数指针表访问。
- libc 的 `crt1` 把这两个对象存进一个全局 `AppProperties app`，让后续 stdio/stdlib 实现能取用，然后调用用户的 `main`，并把 `main` 的返回值经 `errno_to_uefi_status` 转成 `EFI_STATUS` 返回给固件。

UEFI 当前是「早期 bring-up」阶段，**仅支持 x86_64**（aarch64/riscv64 尚未启用），支持函数表是占位符，真正的「事实来源」是源码树里的 `config/uefi/entrypoints.txt`。

#### 4.4.2 核心流程

```text
固件加载 .efi → EfiMain(ImageHandle, SystemTable)
                   │  ① app.system_table  = SystemTable   ; 存进全局 AppProperties
                   │  ② app.image_handle  = ImageHandle
                   └─ ③ return errno_to_uefi_status(main(0, nullptr, nullptr))
                                                    ↑
                                  用户 main 的 int 返回值 → EFI_STATUS
```

注意 `main` 此时拿到的 argc/argv/envp 是 `0/nullptr/nullptr`——argv 解析（需要 `EFI_SHELL_PROTOCOL`、UTF16→UTF8 转换）在源码里被标注为 TODO，尚未实现。

#### 4.4.3 源码精读

- `EfiMain` 的实现：把两个 UEFI 句柄存入 `app`，再 `errno_to_uefi_status(main(0, nullptr, nullptr))` 返回；注释点明 argv 解析仍是 TODO：[startup/uefi/crt1.cpp:L24-L31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/uefi/crt1.cpp#L24-L31)（UEFI 固有入口）。
- `AppProperties` 结构：持有 `EFI_SYSTEM_TABLE *system_table` 与 `EFI_HANDLE image_handle`，并被声明为 `[[gnu::weak]]`：[config/uefi/app.h:L20-L27](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/uefi/app.h#L20-L27)（捕获 UEFI 应用属性）。
- UEFI 构建命令与「仅 x86_64」限制：[docs/uefi/building.rst:L16-L18](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/uefi/building.rst#L16-L18)（架构范围）；完整 CMake 配置见 [docs/uefi/building.rst:L36-L44](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/uefi/building.rst#L36-L44)（目标三元组 `x86_64-unknown-uefi-llvm`、需要 `clang`+`lld`）。
- 支持现状说明：函数表是占位符，真正事实来源是源码树 `config/uefi/entrypoints.txt`：[docs/uefi/support.rst:L10-L14](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/uefi/support.rst#L10-L14)。
- UEFI `crt1` 的构建：用与 GPU/baremetal 类似的 `add_startup_object`，依赖 `libc.config.app_h` 与 `libc.src.__support.OSUtil.uefi.uefi_util`：[startup/uefi/CMakeLists.txt:L32-L39](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/uefi/CMakeLists.txt#L32-L39)。

#### 4.4.4 代码实践

**实践目标**：看清 UEFI 入口如何把「固件世界」与「C `main` 世界」衔接起来。

**操作步骤**：

1. 阅读 `startup/uefi/crt1.cpp` 的 `EfiMain`（L24-L31），画出「`EfiMain` 参数 → `app` 全局 → `main` → `EFI_STATUS` 返回」的数据流。
2. 打开 `config/uefi/app.h`（L20-L27），确认 `AppProperties` 只有两个字段。
3. 对照 GPU 的 `config/gpu/app.h`（仅一个 `env_ptr`），体会「不同目标的『应用属性』结构差异极大」。

**需要观察的现象**：UEFI 把固件句柄原封不动存起来供后续 OS 抽象层（`OSUtil/uefi/`）使用，而 GPU 只存一个 env 指针、baremetal 干脆只用链接脚本符号——三种目标对「应用上下文」的需求完全不同。

**预期结果**：用一句话概括 UEFI 的入口约定——「固件调 `EfiMain` 给两个句柄，libc 存下后转调 `main`，再把返回值译成 `EFI_STATUS`」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 UEFI 的入口是 `EfiMain` 而不是 `main`？
**答案**：UEFI 固件加载 PE 镜像后，按 UEFI 规范调用的是 `EfiMain(ImageHandle, SystemTable)`，签名固定。`main` 是 C 标准入口，由 libc 的 `crt1` 在 `EfiMain` 内部转调（L24-L31）。

**练习 2**：UEFI 当前为何「仅 x86_64」？
**答案**：UEFI 后端在 `clang` 里仍是实验特性，aarch64/riscv64 目标尚未启用（见 building.rst L16-L18）。

### 4.5 三类特殊 startup 的对照

#### 4.5.1 概念说明

前三个模块分别讲了三类目标的启动，本模块把它们并排放，提炼出「程序入口」的根本差异——这正是理解 LLVM-libc retargetable 设计的关键。

三者的核心差别在**「谁调用 `main`、调用几次、入口前要做什么」**：

| 维度 | Linux Full | GPU | baremetal | UEFI |
| --- | --- | --- | --- | --- |
| 首个入口 | `_start`（crt1.o） | `_begin`/`_start`/`_end` 内核 | `_start`（naked） | `EfiMain`（固件调） |
| `main` 调用次数 | 1 次 | **每个活动线程各调 1 次**（atomic OR 累积返回码） | 1 次 | 1 次 |
| 入口前准备 | 内核已铺好栈/auxv | offloading 运行时已跑全局构造/析构 | **几乎全无**，`do_start` 自建 MMU | 固件提供系统表 |
| argv/envp | 内核经栈传递 | 由运行时传入 | 无（`main(0,0)`） | 待实现（`main(0,nullptr,nullptr)`） |
| 库形态 | 机器码 `.a` | **IR `.a` + 单一 `.bc`** | 机器码 `.a` | 机器码 `.a` |
| 返回 | `exit(main(...))` | OR 累积后由 loader 收尾 | `exit(main(0,0))` | `errno_to_uefi_status(main(...))` |

#### 4.5.2 核心流程

把 GPU 启动单独拎出来，因为它最反直觉：

```text
GPU 程序启动（SPMD 模型）
  offloading 运行时 / loader 依次启动三个内核：
   1) _begin(argc, argv, env)    → 原子写入 app.env_ptr          （单次/每核）
   2) _start(argc, argv, envp, ret) → 每个 active thread 调 main，
                                      __atomic_fetch_or(ret, main(...))  （并发！）
   3) _end()                     → 单线程调 __cxa_finalize(nullptr)（跑 atexit）
```

对比 Linux/baremetal「一个 `_start` 顺序串起 do_start 再调一次 main」，GPU 的 `_start` 是**被并发启动的内核**，`main` 在每个线程上都跑——这是 SPMD（单程序多数据）执行模型对「程序入口」概念的彻底重写。

#### 4.5.3 源码精读

- GPU 启动内核全貌：`_begin` 原子存 env 指针、`_start` 原子 OR 累积每个线程 `main` 的返回值、`_end` 单线程跑 `__cxa_finalize`：[startup/gpu/start.cpp:L25-L45](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/gpu/start.cpp#L25-L45)（GPU 三内核）。
- 关键细节：`_start` 用 `__atomic_fetch_or(ret, main(...), RELAXED)` 把多线程返回码合并进 `ret`，注释说明「以用户启动 `_start` 内核时的每个活动线程」调用 `main`：[startup/gpu/start.cpp:L33-L38](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/gpu/start.cpp#L33-L38)（main 的并发入口）。
- 三个内核都带 `[[gnu::visibility("protected"), clang::device_kernel]]`，标明它们是「设备内核」而非普通主机函数：[startup/gpu/start.cpp:L25-L26](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/gpu/start.cpp#L25-L26)。
- GPU 的「应用属性」结构只有 `env_ptr` 一个字段，比 UEFI/Linux 都简陋：[config/gpu/app.h:L19-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/gpu/app.h#L19-L21)（DataEnvironment）。
- GPU `crt1` 的构建注册，`-ffreestanding`/`-fno-builtin` 避免编译器对 `main` 调用告警，并依赖 RPC client 与 GPU utils：[startup/gpu/CMakeLists.txt:L40-L53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/gpu/CMakeLists.txt#L40-L53)。

#### 4.5.4 代码实践

**实践目标**：通过对照阅读，建立「同一份 libc 源码，三种启动模型」的全局图景。

**操作步骤**：

1. 同时打开 `startup/gpu/start.cpp`、`startup/baremetal/aarch64/start.cpp`、`startup/uefi/crt1.cpp`。
2. 在每个文件里定位「调用 `main` 的那一行」，记录：调用前做了什么、调用后如何收尾。
3. 对比三者的「应用属性全局变量」：GPU 是 `DataEnvironment app`（start.cpp L21）、UEFI 是 `AppProperties app`（crt1.cpp L15）、baremetal 没有这种全局（直接用链接脚本符号）。

**需要观察的现象**：三者都用 `LIBC_NAMESPACE_DECL` 包裹内部符号（承接 [u2-l2](u2-l2-implementation-standard-and-macros.md) 的命名空间约定），但「`main` 何时被调、被调几次」完全不同——这正是「retargetable」要付出的代价与换来的灵活性。

**预期结果**：你能口头复述「Linux 调一次、GPU 每线程调一次、baremetal 自己建 MMU 后调一次、UEFI 在 `EfiMain` 里调一次」四种形态。

#### 4.5.5 小练习与答案

**练习 1**：GPU 的 `_start` 为什么用 `__atomic_fetch_or` 而非直接 `return main(...)`？
**答案**：`_start` 是被多个线程并发执行的内核，没有单一返回值。用原子 OR 把所有线程 `main` 的返回码合并进调用方传入的 `ret`，loader 据此得到一个聚合退出码（start.cpp L33-L38）。

**练习 2**：三类目标中，哪一个「最不依赖运行时环境替它做事」？为什么？
**答案**：baremetal。GPU 依赖 offloading 运行时跑全局构造/析构，UEFI 依赖固件给系统表，Linux 依赖内核铺好栈与 auxv；而 baremetal 的 `do_start` 从「设异常向量、建页表、开 MMU」做起，几乎所有事都自己扛（start.cpp L156-L187）。

## 5. 综合实践

本实践把 4.1–4.5 串成一条完整的「GPU vs baremetal 启动对照 + bitcode 合并溯源」调查链。

**任务背景**：团队要为一个新的「自带 GPU 协处理器 + 裸机主控」的嵌入式平台评估启动方案，需要你解释清楚 GPU 与 baremetal 在「程序入口」上的根本差异，并说明 GPU 的 `crt1.o` 与 `libc.bc` 到底是怎么用 `llvm-link` 合并出来的、为什么这比旧的链接器 `-r`/`-flto` 方案更好。

**操作步骤**：

1. **对比启动实现**。打开 `startup/gpu/start.cpp` 与 `startup/baremetal/aarch64/start.cpp`，分别写出二者「从入口到 `main`」的最短路径。重点回答：
   - baremetal 的 `_start`→`do_start` 中，有哪几件事（MMU、scatter-load、init_array）是 GPU 启动**完全不做**的？为什么 GPU 可以不做？（提示：offloading 运行时与 GPU 执行模型各自替它做了什么。）
   - GPU 的 `main` 与 baremetal 的 `main` 在「被调用次数」上的根本差异是什么？引用 `startup/gpu/start.cpp` L33-L38 的 `__atomic_fetch_or` 说明。
2. **追踪 bitcode 合并**。阅读 `startup/gpu/CMakeLists.txt`（L30-L37）与 `cmake/modules/LLVMLibCLibraryRules.cmake`（L93-L125），说明：
   - GPU 的 `crt1.o` 是如何由 `llvm_link_bitcode` 从对象文件合并而成的；
   - `libc.bc` 又是如何由 `add_bitcode_entrypoint_library` 在 `libc.a` 归档之上叠出来的（`lib/CMakeLists.txt` L43-L56）；
   - 为什么顶层 `CMakeLists.txt`（L298-L304）要把 `llvm-link` 列为 GPU 构建的强制依赖。
3. **解释为何取代 `-r`/`-flto` 旧方案**。基于上述证据组织一段说明：旧的「链接器 `-r`（可重定位合并）或 `-flto` LTO」方案依赖**链接器**对 GPU 目标的支持，而 GPU 工具链（尤其 NVPTX 的 `nvlink` 包装器）链接能力很有限；改用 `llvm-link` 直接在 **LLVM IR 层**合并模块，绕开了设备链接器的能力短板，且产出的单一 `.bc` 模块可被任何设备工具链作为「自包含设备库」消费。引用 `docs/gpu/building.rst` L154-L160 对 `libc.bc` 的描述作为佐证。

**预期产出**：一份一页备忘录，包含（a）GPU/baremetal 启动路径对比表；（b）`crt1.o` 与 `libc.bc` 的 `llvm-link` 生成链示意图；（c）一段「为何 `llvm-link` 取代 `-r`/`-flto`」的论证。

**验证**：把备忘录里的每一条结论回链到本讲引用的具体源码行号；凡无法在源码中定位的断言，标注「待确认」或「待本地验证」（例如真正跑一次 GPU 构建需要 AMD/NVIDIA GPU 与已装 LLVM）。

## 6. 本讲小结

- **GPU 目标**把 libc 重新定义为「IR 归档 + 单一 bitcode blob」，服务于 offloading 语言（设备端补充库）与 hosted 直接编译两种模式；受执行模型限制，不提供互斥锁/文件缓冲，`errno` 为原子全局，主机服务经 RPC 回调。
- **`llvm_link_bitcode`** 是包装 `llvm-link` 的 CMake 原语，把若干 IR 输入合并成单一 `.bc` 模块；`add_bitcode_entrypoint_library` 在已有 `.a` 归档之上叠出 `.bc`，GPU 的 `crt1.o` 也经它物化——故 `llvm-link` 是 GPU 构建的强制依赖。
- **baremetal** 是最小足迹、自带 startup 的裸机目标：`_start` 仅设栈，`do_start` 自建 MMU 页表、做 scatter-loading、跑 init_array 后调 `main(0,0)`，完全不为 TLS/线程操心。
- **UEFI** 入口是固件调用的 `EfiMain(ImageHandle, SystemTable)`，libc 把二者存进 `AppProperties app` 再转调 `main`，返回值译成 `EFI_STATUS`；当前仅 x86_64，bring-up 阶段。
- **三类特殊 startup 的根本差异**在于「谁调 `main`、调几次」：GPU 每线程并发调一次（原子 OR 累积返回码）、baremetal 自建硬件环境后调一次、UEFI 在 `EfiMain` 内调一次——这正是 retargetable 设计要兼顾的多样性。
- 把库以 bitcode 交付、用 `llvm-link` 在 IR 层合并，绕开了 GPU 设备链接器能力有限的短板，取代了旧的链接器 `-r`/`-flto` 方案。

## 7. 下一步学习建议

- **延续启动主题**：重读 [u8-l2 程序启动流程](u8-l2-program-startup-crt.md)，把 Linux 的 `do_start` 与本讲 baremetal 的 `do_start` 做一次完整逐项 diff，巩固「内核代劳 vs 自力更生」的对照。
- **GPU 深入**：阅读 `docs/gpu/rpc.rst` 与 `src/__support/RPC/`，理解 GPU 上 `printf`/`fopen` 等如何经 RPC 客户端回到主机；再读 `docs/gpu/testing.rst` 看 GPU 单元测试如何用 loader 跑。
- **构建机制深入**：阅读 [u2-l3 CMake 构建规则详解](u2-l3-cmake-build-rules.md) 与 `cmake/modules/LLVMLibCLibraryRules.cmake` 全文，把 `add_entrypoint_library`/`add_bitcode_entrypoint_library`/`add_entrypoint_external` 三条规则的关系理清。
- **移植实战**：结合 [u11-l1 移植到新平台](u11-l1-porting-new-platform.md) 与本讲，规划一个「baremetal 新架构」的最小 bring-up（建 `config/` 树、写一条 `_start`+`do_start`、注册第一波纯算法入口点）。
