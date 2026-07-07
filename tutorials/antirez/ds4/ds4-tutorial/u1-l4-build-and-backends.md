# 构建系统与多后端编译

## 1. 本讲目标

上一讲（[u1-l3 目录结构与源码地图](u1-l3-directory-map.md)）我们建立了一张「哪个文件负责什么」的心智地图，并且知道了**每个前端二进制 = 自己的前端 `.o` + 公共辅助 `.o` + 一组共享的 `CORE_OBJS`**。本讲要回答的问题是：**这些 `.o` 是怎么被编译、链接成可执行文件的？为什么同一份源码能在 Apple Metal、NVIDIA CUDA、AMD ROCm、纯 CPU 四条路径上跑？**

读完本讲，你应当能够：

- 看懂 `Makefile` 如何用 `ifeq ($(UNAME_S),Darwin)` 把 macOS 与 Linux 拆成两条完全不同的构建分支。
- 说出 `CORE_OBJS` 在四种后端下分别由哪些对象组成，以及为什么 CPU 构建没有「后端对象」。
- 区分 `make` / `make cuda-spark` / `make cuda-generic` / `make cuda CUDA_ARCH=...` / `make strix-halo` / `make cpu` 六个目标的差别，并解释**为什么在 Linux 上裸敲 `make` 只会打印帮助**。
- 理解 `-DDS4_NO_GPU` 与 `-DDS4_ROCM_BUILD` 两个编译期开关如何通过 C 预处理宏在「同一份 `ds4.c`」里切换出不同的后端代码路径。

## 2. 前置知识

在进入 `Makefile` 之前，先用大白话对齐几个概念：

- **编译期 vs 运行期**：C 程序在「编译时」由预处理器根据宏（如 `DS4_NO_GPU`）决定保留哪段代码、删掉哪段代码；在「运行时」再根据用户传的命令行参数（如 `--cpu`）做选择。ds4 同时用了两层：宏决定**哪些后端代码被编译进来**，参数决定**运行时默认走哪个**。
- **后端（backend）**：真正执行矩阵乘法、注意力、MoE 路由的计算核。ds4 把后端抽象成三种枚举值：`DS4_BACKEND_METAL`、`DS4_BACKEND_CUDA`、`DS4_BACKEND_CPU`（见 [ds4.h:19-23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L19-L23)）。注意 ROCm **复用** `DS4_BACKEND_CUDA` 这个枚举位，只是底层 `.o` 换成了 HIP 实现。
- **`CORE_OBJS`**：引擎核心对象集合，是所有前端二进制共享的「引擎本体」。上一讲已建立这个概念，本讲深入它如何随平台/后端变化。
- **nvcc / hipcc**：分别是 NVIDIA CUDA 和 AMD ROCm/HIP 的编译驱动。它们既能编译 `.cu` GPU 代码，也能像普通 `cc` 一样做最终链接，因此 ds4 在 Linux 上**用 nvcc/hipcc 而不是 cc 来做最终链接**，以便正确接上 GPU 运行时库。
- **`make` 的变量与条件**：`?=` 表示「未设置才赋默认值」（可被环境变量覆盖）；`+=` 表示追加；`ifeq (a,b)` 是字符串相等判断。本讲会反复用到这三者。

## 3. 本讲源码地图

本讲涉及的文件很少，但每一个都要精读：

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile) | 全部构建逻辑：平台分支、`CORE_OBJS` 组合、各后端目标、链接库。本讲的绝对主角。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 「Then build」与「Backends」两节给出面向用户的构建命令清单。 |
| [STRIXHALO.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/STRIXHALO.md) | Strix Halo（gfx1151）ROCm 后端的装机与构建手册，列出 ROCm 需要的系统库。 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | `default_backend()` 与 `parse_backend()`：运行期如何根据编译期宏决定默认后端。 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | `ds4_backend` 枚举定义，确认只有三种后端值。 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 平台分支与 CORE_OBJS

#### 4.1.1 概念说明

ds4 是跨平台的，但 macOS（Metal）和 Linux（CUDA/ROCm）的工具链差异极大：编译器不同（`cc` vs `nvcc` vs `hipcc`）、链接库不同（`-framework Metal` vs `-lcudart` vs `-lhipblas`）、连「默认敲 `make` 该做什么」都不同。`Makefile` 用一个最朴素但有效的手段处理这件事——**用 `uname -s` 探测操作系统，然后用 `ifeq` 把整个构建拆成两套几乎独立的规则**。

`CORE_OBJS` 是这两套规则共享的核心变量，它就是上一讲提到的「引擎本体对象集」。它的内容会随后端变化：Metal 构建里放 `ds4_metal.o`，CUDA 构建里放 `ds4_cuda.o`，ROCm 构建里放 `ds4_rocm.o`。理解了 `CORE_OBJS` 如何被拼装，就理解了「可替换后端」的工程落点。

#### 4.1.2 核心流程

整个 `Makefile` 顶层的控制流可以这样描述：

```text
1. 探测操作系统：UNAME_S := $(shell uname -s)
2. 设置通用编译选项 CFLAGS / OBJCFLAGS / LDLIBS
3. ifeq ($(UNAME_S),Darwin)
       ├── METAL_LDLIBS += -framework Foundation -framework Metal
       ├── CORE_OBJS      = ds4.o ds4_distributed.o ds4_ssd.o ds4_metal.o   # Metal 后端
       └── CPU_CORE_OBJS  = ds4_cpu.o ds4_distributed.o ds4_ssd.o           # CPU 无后端对象
   else  # Linux
       ├── 设置 CUDA / ROCm 工具链变量
       ├── CORE_OBJS      = ds4.o ds4_distributed.o ds4_ssd.o ds4_cuda.o    # CUDA 后端（默认）
       ├── CPU_CORE_OBJS  = ds4_cpu.o ds4_distributed.o ds4_ssd.o
       └── DS4_LINK       = $(NVCC) ...     # 用 nvcc 做最终链接
4. 定义 all / help / 各后端目标 / cpu 目标（两套，按平台分别生效）
5. 定义所有 .o 的编译规则（两套共享）
```

关键在于：**步骤 3 的两个分支各自只「活跃」一个**，所以同一个 `CORE_OBJS` 名字在 macOS 里指 Metal 对象、在 Linux 里指 CUDA 对象，互不干扰。

#### 4.1.3 源码精读

先看平台探测与通用选项（这部分两平台共享）：

[Makefile:1-16](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L1-L16) —— 用 `uname -s` 判断系统，macOS 用 `-mcpu=native`、Linux 用 `-march=native`；设定统一的 `-O3 -ffast-math` 与 `-std=c99`，链接基础库 `LDLIBS = -lm -pthread`。同时用 `wildcard` 收集 `metal/*.metal` 与 `rocm/*.cuh`，让这些 GPU 内核源文件成为后续 `.o` 规则的依赖。

接着是 **Darwin（macOS）分支**：

[Makefile:18-21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L18-L21) —— macOS 上 `CORE_OBJS` 末尾是 `ds4_metal.o`，并且 `METAL_LDLIBS` 追加了 `-framework Foundation -framework Metal` 两个系统框架。这就是 Metal 后端链接库的来源。

再看 **Linux 分支**（这是大头）：

[Makefile:22-41](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L22-L41) —— 这里一次设置了三套工具链：

- **CUDA**（[L24-33](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L24-L33)）：`NVCC` 默认指向 `/usr/local/cuda/bin/nvcc`；`NVCCFLAGS` 带 `--use_fast_math`；`CORE_OBJS` 末尾是 `ds4_cuda.o`；`CUDA_LDLIBS` 链接 `-lcudart -lcublas`，并给出了两条库搜索路径 `-L$(CUDA_HOME)/targets/sbsa-linux/lib -L$(CUDA_HOME)/lib64`（`sbsa` 是 ARM 服务器目标，对应 DGX Spark/GB10 这类 ARM 机型）。
- **ROCm**（[L34-37](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L34-L37)）：`HIPCC` 默认 `/opt/rocm/bin/hipcc`；`ROCM_ARCH` 默认 `gfx1151`（Strix Halo）；`ROCM_LDLIBS` 链接 `-lhipblas -lhipblaslt`。
- **链接器变量**（[L38-40](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L38-L40)）：`DS4_LINK ?= $(NVCC) $(NVCCFLAGS)` 与 `DS4_LINK_LIBS ?= $(CUDA_LDLIBS)`。这两个变量是 Linux 上**所有二进制最终链接步骤**用的命令和库。注意它们用 `?=`，意味着可以被 `make strix-halo` 在命令行上覆盖成 HIPCC 版本（见 4.2）。

> 为什么 Linux 上要用 nvcc/hipcc 来做最终链接，而不是用 `cc`？因为 `ds4_cuda.o` / `ds4_rocm.o` 里含有 GPU host 代码与运行时桩，必须由对应的编译驱动来链接才能正确接上设备运行时库。`DS4_LINK` 这个抽象让「换链接器」只改一个变量即可。

最后注意一个容易看漏的细节：在 Linux 分支里 `METAL_LDLIBS := $(LDLIBS)`（[L40](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L40)）被重置成纯基础库——这只是防止 Darwin 那行的 `-framework` 在 Linux 上误用，并不表示 Linux 真的会构建 Metal。

#### 4.1.4 代码实践

**实践目标**：亲手把 `CORE_OBJS` 在四种后端下的组成填进一张表，从而固化「可替换后端」的工程结构。

**操作步骤**：

1. 打开 [Makefile:18-41](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L18-L41)。
2. 分别记录 Darwin 分支与 Linux 分支里 `CORE_OBJS` 与 `CPU_CORE_OBJS` 的取值。
3. 再看 [Makefile:107-112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L107-L112) 的 `strix-halo` 目标如何用命令行覆盖 `CORE_OBJS`。

**需要观察的现象**：`ds4.o`、`ds4_distributed.o`、`ds4_ssd.o` 这三个对象在四种后端里**始终存在且不变**；只有第四个对象在 `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o` 之间切换；CPU 构建则干脆没有第四个对象，第一个对象也从 `ds4.o` 变成 `ds4_cpu.o`。

**预期结果**（可直接对照）：

| 构建路径 | CORE_OBJS（或等价组成） |
| --- | --- |
| macOS Metal（`make`） | `ds4.o ds4_distributed.o ds4_ssd.o ds4_metal.o` |
| Linux CUDA（`make cuda-*`） | `ds4.o ds4_distributed.o ds4_ssd.o ds4_cuda.o` |
| Linux ROCm（`make strix-halo`） | `ds4.o ds4_distributed.o ds4_ssd.o ds4_rocm.o`（命令行覆盖） |
| CPU（`make cpu`） | `ds4_cpu.o ds4_distributed.o ds4_ssd.o`（无后端对象） |

> 待本地验证：以上表格是从源码静态推演得到的；如果你在自己机器上 `make cpu` 后想确认，可对比构建日志里每个二进制的链接命令行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ds4_distributed.o` 和 `ds4_ssd.o` 在所有四种后端里都一样、从不替换？

> **参考答案**：因为分布式协议（TCP 帧编解码、route 组建）和 SSD 流式缓存（预算计算、mlock）都是**与计算后端无关**的纯 CPU 逻辑。它们调用后端的地方都通过 `ds4_gpu.h` 的抽象接口，所以一份 `.o` 就能跟任意后端链接。这正体现了「后端可替换、引擎核心稳定」的分层。

**练习 2**：`DS4_LINK ?= $(NVCC) $(NVCCFLAGS)` 为什么用 `?=` 而不是 `=`？

> **参考答案**：`?=` 表示「仅当未定义时赋值」，允许在命令行覆盖。`make strix-halo` 正是利用这一点，在命令行传入 `DS4_LINK="$(HIPCC) $(ROCM_CFLAGS)"`，从而把整条链接链从 nvcc 换成 hipcc，而无需修改 Makefile 本体。

---

### 4.2 各后端构建目标

#### 4.2.1 概念说明

`Makefile` 暴露给用户的「构建目标」其实就是 `make xxx` 里的那个 `xxx`。ds4 在两个平台上各定义了一套目标，且**默认目标（裸 `make`）的含义完全不同**：

- 在 macOS 上，`make` 直接构建 Metal 版的五个二进制（`ds4`、`ds4-server`、`ds4-bench`、`ds4-eval`、`ds4-agent`），因为 Metal 没有「显卡架构」这种需要用户指定的东西。
- 在 Linux 上，`make` **只打印帮助**，绝不隐式选择一个 CUDA 目标。原因是 CUDA 编译需要指定目标架构（`-arch=sm_XXX`），而选错架构会导致编译失败或性能低下，作者不愿替用户猜。

四个「真正的」Linux 后端目标 `cuda-spark` / `cuda-generic` / `cuda` / `strix-halo` 分别对应不同的硬件场景与编译参数。本模块逐一拆开。

#### 4.2.2 核心流程

Linux 上各后端目标的本质是「**用不同参数递归调用一次 `make`，重新构建五个标准二进制名**」：

```text
make cuda-spark   →  $(MAKE) -B ds4 ds4-server ... CUDA_ARCH=          # 空架构，让 nvcc 用默认
make cuda-generic →  $(MAKE) -B ds4 ds4-server ... CUDA_ARCH=native    # 探测本机架构
make cuda CUDA_ARCH=sm_120
                  →  $(MAKE) -B ds4 ds4-server ... CUDA_ARCH=sm_120     # 显式架构
make strix-halo   →  $(MAKE) -B ds4 ds4-server ... \
                       CORE_OBJS="... ds4_rocm.o" \
                       CFLAGS="$(CFLAGS) -DDS4_ROCM_BUILD" \
                       DS4_LINK="$(HIPCC) $(ROCM_CFLAGS)" \
                       DS4_LINK_LIBS="$(ROCM_LDLIBS)"
make cpu          →  用 cc + -DDS4_NO_GPU 编出一套 *_cpu.o，再链接成五个标准二进制名
```

`-B` 是 `--always-make`，强制无条件重编，保证切后端时不会用到上一次的旧 `.o`。五个二进制的**可执行文件名永远相同**（始终叫 `./ds4`、`./ds4-server` 等），只是底层后端不同——这对前端脚本和文档很友好。

#### 4.2.3 源码精读

先看**两个平台各自的默认目标**。macOS 上 `make` 真的会构建：

[Makefile:45-53](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L45-L53) —— `all: ds4 ds4-server ds4-bench ds4-eval ds4-agent`，并且 `help` 文案明确说 `make` 会构建 Metal 版五个二进制。

而 Linux 上：

[Makefile:79-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L79-L91) —— `all: help`，**默认目标就是打印帮助**。`help` 文案列出了 `cuda-spark` / `cuda-generic` / `cuda CUDA_ARCH=` / `strix-halo` / `rocm` / `cpu` / `test` / `clean` 这些可用目标。这就是本讲标题里「普通 `make` 在 Linux 上只打印帮助」的源头——它是**故意为之**的安全设计。

接下来是**四个 Linux 后端目标**本身：

[Makefile:93-105](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L93-L105) —— 三个 CUDA 目标的差别全在 `CUDA_ARCH`：

- `cuda-spark`：传 `CUDA_ARCH=`（空）。结合 [L27-29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L27-L29) 的逻辑，空 `CUDA_ARCH` 会让 `NVCC_ARCH_FLAGS` 为空，即**不显式加 `-arch=`**。README 解释这是当前 DGX Spark / GB10 上最快的路径（见 [README.md:183-185](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L183-L185)）。
- `cuda-generic`：传 `CUDA_ARCH=native`，让 nvcc 自动探测本机显卡架构，适合「一台普通 CUDA 机器」。
- `cuda`：要求用户**必须显式传 `CUDA_ARCH=sm_NN`**，否则报错退出（[L100-104](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L100-L104) 的 `if [ -z "$(strip $(CUDA_ARCH))" ]` 检查）。用于交叉编译或需要锁定已知架构的场景。

[Makefile:107-114](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L107-L114) —— `strix-halo` 是整段 `Makefile` 里**唯一一个动用「命令行覆盖 Make 变量」**的目标，它一次性改了四样东西：

1. `CORE_OBJS="ds4.o ds4_distributed.o ds4_ssd.o ds4_rocm.o"` —— 把第四个对象换成 ROCm 实现；
2. `CFLAGS="$(CFLAGS) -DDS4_ROCM_BUILD"` —— 追加 ROCm 编译期宏；
3. `DS4_LINK="$(HIPCC) $(ROCM_CFLAGS)"` —— 链接器换成 hipcc；
4. `DS4_LINK_LIBS="$(ROCM_LDLIBS)"` —— 链接库换成 `-lhipblas -lhipblaslt`。

`rocm: strix-halo`（[L114](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L114)）只是个别名。STRIXHALO.md 也确认 `make strix-halo` 是 ROCm 的标准构建命令（[STRIXHALO.md:106-114](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/STRIXHALO.md#L106-L114)）。

再看**最终二进制的链接规则**。以 Linux 分支的 `ds4` 为例：

[Makefile:116-117](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L116-L117) —— `ds4: ds4_cli.o ds4_help.o linenoise.o $(CORE_OBJS)`，然后用 `$(DS4_LINK) -o $@ $^ $(DS4_LINK_LIBS)` 链接。这里 `$@` 是目标名、`$^` 是全部依赖。可以看到：**前端对象**（`ds4_cli.o`）+ **辅助对象**（`ds4_help.o`、`linenoise.o`）+ **`CORE_OBJS`**，正好是上一讲总结的三段式。其余四个二进制（`ds4-server` [L119-120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L119-L120)、`ds4-bench` [L122-123](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L122-L123)、`ds4-eval` [L125-126](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L125-L126)、`ds4-agent` [L128-129](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L128-L129)）结构完全一致，只是各自换上自己的前端对象与所需辅助对象（例如 `ds4-server` 多了 `ds4_kvstore.o rax.o`，`ds4-agent` 多了 `ds4_web.o ds4_kvstore.o linenoise.o`）。

作为对照，macOS 分支的 `ds4` 链接规则用的是 `$(CC) $(CFLAGS)` 与 `$(METAL_LDLIBS)`（[Makefile:55-56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L55-L56)），因为 Metal 后端的对象是用普通 `cc` 编译的 Objective-C（`.m`），不需要 nvcc 参与。

最后给出**各后端链接库总表**（从源码静态归纳）：

| 后端 | 构建命令 | 链接器 | 链接库（去 `-lm -pthread` 公共项后） |
| --- | --- | --- | --- |
| Metal | `make`（macOS） | `cc` | `-framework Foundation -framework Metal` |
| CUDA | `make cuda-spark` / `cuda-generic` / `cuda CUDA_ARCH=` | `nvcc` | `-lcudart -lcublas`（+ CUDA 库搜索路径） |
| ROCm | `make strix-halo` | `hipcc` | `-lhipblas -lhipblaslt` |
| CPU | `make cpu` | `cc` | （无额外库，仅 `-lm -pthread`） |

ROCm 还需要在系统层面预装 `hipcc`、`libhipblas-dev`、`libhipblaslt-dev`、`librocwmma-dev` 等，并补齐 rocWMMA 内部头，详见 [STRIXHALO.md:8-30](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/STRIXHALO.md#L8-L30)。

#### 4.2.4 代码实践

**实践目标**：解释「为什么 Linux 上裸 `make` 只打印帮助」，并把每个后端的链接库填出来。

**操作步骤**：

1. 读 [Makefile:79-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L79-L91)，找到 Linux 分支的 `all:` 目标，看它依赖什么。
2. 对比 macOS 分支的 `all:`（[Makefile:45-46](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L45-L46)）。
3. 读 README「Then build」与「Backends」两节（[README.md:142-149](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L142-L149)、[README.md:1174-1206](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1174-L1206)），看官方怎么说。
4. （可选）在 Linux 上亲手敲 `make`，观察输出。

**需要观察的现象**：Linux 上 `make` 不会编译任何东西，只会打印一段「DS4 build targets:」帮助，列出 `cuda-spark` 等目标；而 macOS 上 `make` 会真的开始编译 Metal。

**预期结果**：

> 「为什么只打印帮助」的根本原因：Linux 分支里 `all: help`（[Makefile:80](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L80)），而 `help` 只是一串 `@echo`。作者故意不设默认 CUDA 目标，因为 CUDA 编译必须指定目标架构（`-arch=sm_NN`），隐式猜一个会带来「编译失败」或「跑在错误的架构上性能很差」的体验，不如强迫用户在 `cuda-spark` / `cuda-generic` / `cuda CUDA_ARCH=` / `strix-halo` / `cpu` 里**显式选一个**。

> 待本地验证：如果你在本机 Linux 上执行 `make`，应当看到与 [Makefile:83-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L83-L91) 一致的帮助文本；本讲义编写时未在该环境实际运行该命令。

各后端链接库见上面的总表。

#### 4.2.5 小练习与答案

**练习 1**：`make cuda-spark` 和 `make cuda-generic` 在 `Makefile` 里的差别只有一行 `CUDA_ARCH=` 的取值。这一行是怎么在最终编译命令里产生不同效果的？

> **参考答案**：`CUDA_ARCH` 通过 [Makefile:27-29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L27-L29) 的 `ifneq` 被拼进 `NVCC_ARCH_FLAGS`：只有非空时才生成 `-arch=$(CUDA_ARCH)`，否则 `NVCC_ARCH_FLAGS` 为空。`NVCC_ARCH_FLAGS` 又被并入 `NVCCFLAGS`（[L30](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L30)），最终传给 `ds4_cuda.o` 的编译（[L211-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L211-L212)）。所以 `cuda-spark`（空）= 不加 `-arch`；`cuda-generic`（`native`）= `-arch=native`。

**练习 2**：`make strix-halo` 为什么必须同时改 `CORE_OBJS`、`CFLAGS`、`DS4_LINK`、`DS4_LINK_LIBS` 四个变量，少改一个会怎样？

> **参考答案**：四个变量分别管四件不可替代的事：`CORE_OBJS` 决定**链接哪个后端对象**（不改会链到 `ds4_cuda.o`）；`CFLAGS` 加 `-DDS4_ROCM_BUILD` 决定**预处理器激活哪些 ROCm 专属代码**；`DS4_LINK` 决定**用哪个链接器**（不改会用 nvcc 去链 hip 代码，失败）；`DS4_LINK_LIBS` 决定**接哪些运行时库**（不改会去找不存在的 `-lcudart`）。四个一起改，才能把整条 CUDA 链完整替换成 ROCm 链。

---

### 4.3 DS4_NO_GPU 开关与 CPU/ROCm 编译期切换

#### 4.3.1 概念说明

`DS4_NO_GPU` 是 ds4 最重要的编译期开关之一。它的作用不是「关掉某个功能」，而是**把整个引擎从「需要一个 GPU 后端对象」退化成「引擎核心自身就是 CPU 后端」**。这就是为什么 CPU 构建没有 `ds4_metal.o`/`ds4_cuda.o`/`ds4_rocm.o` 中的任何一个——`ds4.c` 在 `-DDS4_NO_GPU` 下被重新编译成 `ds4_cpu.o`，里面只保留 C 语言写成的参考实现。

与之并列的另一个开关是 `-DDS4_ROCM_BUILD`，它不删代码，而是**激活 ROCm 专属的优化路径**（如 rocWMMA、`gfx1151` 专属的专家缓存预取），并把 CLI 里的后端名从 `cuda` 改成 `rocm`。

理解这两个宏的关键是：**ds4 的后端选择是「编译期 + 运行期」两层叠加**。编译期宏决定「编译进来的代码是哪一套」；运行期函数 `default_backend()` 再根据这些宏决定「不传 `--backend` 时默认走哪个」。

#### 4.3.2 核心流程

CPU 构建的编译与链接流程：

```text
对每个引擎/前端源文件 X.c：
    cc -DDS4_NO_GPU -c X.c -o X_cpu.o        # 全部加 -DDS4_NO_GPU，产物改成 *_cpu.o
其中 ds4.c → ds4_cpu.o（引擎核心退化成 CPU 后端）

链接（以 ds4 为例）：
    cc -o ds4 ds4_cli_cpu.o ds4_help.o linenoise.o \
          ds4_cpu.o ds4_distributed.o ds4_ssd.o \
          -lm -pthread                          # 注意：没有任何后端对象，也没有 GPU 库
```

运行期的默认后端解析：

```text
default_backend():
    if 定义了 DS4_NO_GPU      → DS4_BACKEND_CPU
    elif 定义了 __APPLE__     → DS4_BACKEND_METAL
    else                      → DS4_BACKEND_CUDA     # ROCm 构建也落在这里
```

#### 4.3.3 源码精读

先看 **CPU 构建如何为每个源文件加 `-DDS4_NO_GPU`**。以引擎核心为例：

[Makefile:190-191](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L190-L191) —— `ds4_cpu.o: ds4.c ...` 然后 `$(CC) $(CFLAGS) -DDS4_NO_GPU -c -o $@ ds4.c`。注意它**和普通 `ds4.o`（[L142-143](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L142-L143)）共享同一个 `ds4.c` 源文件**，只是多了一个宏、换了个产物名。

同样的手法被施加到每一个前端源文件：

[Makefile:193-206](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L193-L206) —— `ds4_cli_cpu.o`、`ds4_server_cpu.o`、`ds4_bench_cpu.o`、`ds4_eval_cpu.o`、`ds4_agent_cpu.o` 全部用 `-DDS4_NO_GPU` 重新编译。这就是为什么 `make cpu` 的链接规则里（[L131-136](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L131-L136)）出现的是一串 `*_cpu.o` 而不是普通的 `.o`——它是一套**完全独立的、与 GPU 构建互不污染**的对象集。

那么 `-DDS4_NO_GPU` 在 `ds4.c` 内部到底切掉了什么？看几个典型位置：

[ds4.c:3371-3392](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3371-L3392) —— 一整段 `graph_stream_expert_table_make`（SSD 流式专家表的 GPU 构造函数）被包在 `#ifndef DS4_NO_GPU` 里。CPU 构建时这段代码直接被预处理器删除，因为 CPU 路径根本不需要「GPU 专家表」。

[ds4.c:3394-3396](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3394-L3396) —— 紧接着的 `ds4_streaming_manual_cache_safe_bytes` 在 `#ifdef DS4_NO_GPU` 分支里直接 `return 0`，因为 CPU 没有 GPU 显存预算的概念。整个 `ds4.c` 里有数十处这样的 `#ifdef DS4_NO_GPU`（可以用 `grep` 验证），它们共同把「需要 GPU 设备指针、命令缓冲、专家缓存预取」的代码全部排除，只留下纯 C 的参考计算路径。

`DS4_ROCM_BUILD` 的作用与之类似但方向相反——它**额外激活** ROCm 专属代码。看一个横跨两个宏的判断：

[ds4.c:79-89](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L79-L89) —— `ds4_backend_supports_ssd_streaming` 判断 CUDA 后端是否支持 SSD 流式时，用的是 `#if defined(DS4_ROCM_BUILD) || (!defined(DS4_NO_GPU) && !defined(__APPLE__))`。这个条件覆盖了「真正的 CUDA 构建」和「ROCm 构建」两种情况，意味着 ROCm 构建也获得了 SSD 流式能力（同样的判断模式在 `ds4.c` 与 `ds4_gpu.h` 里多次出现，例如 [ds4_gpu.h:106-112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L106-L112)）。

最后看**运行期默认后端如何呼应编译期宏**：

[ds4_cli.c:182-190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L182-L190) —— `default_backend()` 三个分支完全对应三种构建产物：`DS4_NO_GPU` → CPU、`__APPLE__` → Metal、其它（含 ROCm）→ CUDA。这解释了 README 那句「The default graph backend is Metal on macOS and CUDA in CUDA builds」（[README.md:1176](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1176)）：CPU 构建的默认是 CPU，而 ROCm 构建虽然底层是 HIP，但默认枚举值仍是 `DS4_BACKEND_CUDA`。

`parse_backend` 进一步印证 ROCm 对 `cuda` 枚举的复用：

[ds4_cli.c:165-180](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L165-L180) —— 在 `DS4_ROCM_BUILD` 下，字符串 `"rocm"` 被映射到 `DS4_BACKEND_CUDA`（[L168](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L168)），且帮助文案里合法后端列成 `metal, rocm, cpu`；非 ROCm 构建下则把 `"cuda"` 映射到 `DS4_BACKEND_CUDA`（[L170](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L170)），合法后端列成 `metal, cuda, cpu`。命令行 `--cpu` / `--metal` / `--cuda` / `--rocm` 这些开关则直接写死对应枚举值（[ds4_cli.c:1535-1544](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1535-L1544)）。

> 小结：`ds4.h` 里只有三种后端枚举（[ds4.h:19-23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L19-L23)），ROCm 并没有独立枚举，而是**复用 `DS4_BACKEND_CUDA` 这一位**，靠 `-DDS4_ROCM_BUILD` 宏在编译期区分底层实现。这是一种很省心的设计：上层引擎代码完全不用为 ROCm 单独写一套分支。

#### 4.3.4 代码实践

**实践目标**：用 `grep` 亲手数一下 `-DDS4_NO_GPU` 在 `ds4.c` 里切掉了多少段代码，从而建立「CPU 构建是一套大幅精简后的引擎」的直观感受。

**操作步骤**：

1. 在仓库根目录执行（只读统计，不修改任何文件）：

   ```sh
   grep -c 'DS4_NO_GPU' ds4.c
   grep -n '#ifdef DS4_NO_GPU\|#ifndef DS4_NO_GPU' ds4.c | head
   ```

2. 对比 [Makefile:190-206](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L190-L206)，确认 `make cpu` 给每个源文件都加了 `-DDS4_NO_GPU`。
3. 再看 [ds4_cli.c:182-190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L182-L190)，回答：「一个用 `make cpu` 编出来的 `./ds4`，如果不传任何 `--backend`，会走哪个后端？为什么？」

**需要观察的现象**：`grep -c` 会输出一个相当大的数字（数十处），说明 CPU 构建确实在编译期把大量 GPU 代码排除了；`default_backend()` 在 `DS4_NO_GPU` 下直接返回 `DS4_BACKEND_CPU`。

**预期结果**：

> 用 `make cpu` 编出来的 `./ds4`，不传 `--backend` 时会走 **CPU 后端**。原因是 `default_backend()` 在 `#ifdef DS4_NO_GPU` 分支里直接 `return DS4_BACKEND_CPU`（[ds4_cli.c:183-184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L183-L184)）。即便用户强行传 `--metal` 或 `--cuda`，也会因为对应后端对象根本没有被链接进来而在运行时报错——这也是 README 反复强调「不要把 CPU 路径当作生产目标」（[README.md:1203-1206](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1203-L1206)）的原因：它只是参考/调试用。

> 待本地验证：`grep -c` 的精确数字会随版本变化，本讲义不写死具体数值，请你自行运行确认。

#### 4.3.5 小练习与答案

**练习 1**：CPU 构建为什么「不需要也不能」链接 `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o` 中的任何一个？

> **参考答案**：因为 `-DDS4_NO_GPU` 在编译期就把 `ds4.c` 里所有调用 GPU 后端 API（`ds4_gpu_*`）的代码用 `#ifdef` 排除了，`ds4_cpu.o` 里不再含有对这些后端符号的引用；同时这些后端 `.o` 本身又依赖各自的 GPU 运行时库（Metal 框架 / cudart / hipblas），CPU 构建环境里不一定装了它们。所以 CPU 构建刻意只链 `ds4_cpu.o ds4_distributed.o ds4_ssd.o`（[Makefile:131-136](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L131-L136)），保持自包含。

**练习 2**：`-DDS4_ROCM_BUILD` 和 `-DDS4_NO_GPU` 这两个宏能不能同时定义？从 `default_backend()` 的角度推断会发生什么。

> **参考答案**：从 `default_backend()`（[ds4_cli.c:182-190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L182-L190)）看，`DS4_NO_GPU` 是第一个被判断的分支，一旦定义就直接返回 CPU，根本走不到后面的 `__APPLE__` / CUDA 判断。所以即便同时定义了 `DS4_ROCM_BUILD`，默认后端仍是 CPU。但实际上 `Makefile` 从不会把它们同时传（`strix-halo` 只加 `-DDS4_ROCM_BUILD`，`cpu` 只加 `-DDS4_NO_GPU`），这两个开关在设计上是互斥的两种构建模式。

**练习 3**：为什么 ROCm 构建复用 `DS4_BACKEND_CUDA` 枚举位，而不是新增一个 `DS4_BACKEND_ROCM`？

> **参考答案**：因为从「引擎核心」的视角看，CUDA 后端和 ROCm 后端实现的是**同一套 `ds4_gpu.h` 抽象接口**（张量 alloc/view/read/write、命令缓冲、专家缓存），引擎核心不需要、也不应该知道底层是 CUDA 还是 HIP。复用枚举位意味着 `ds4.c` 里所有 `if (backend == DS4_BACKEND_CUDA)` 的分支对 ROCm 自动生效，省掉了一整套重复的 ROCm 分支。`-DDS4_ROCM_BUILD` 只在少数需要 ROCm 专属优化的地方（如 rocWMMA 路径）做编译期区分。

## 5. 综合实践

**任务**：假设你拿到一台全新的 Linux 机器，要给 ds4 加一个「假想的第五种后端」，比如 Intel oneAPI（暂称 `oneapi`）。请基于本讲对 `Makefile` 的理解，**在纸上**设计出需要改动的地方（不需要真的写代码，只列改动点）。

要求覆盖以下几点：

1. **新对象**：你需要新增哪个 `.o`？它由哪个源文件编译来？参照 [Makefile:208-215](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L208-L215) 的 `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o` 规则写一条对应的编译规则。
2. **新目标**：参照 `strix-halo`（[Makefile:107-112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L107-L112)），写一个 `make oneapi` 目标，明确它要覆盖哪几个 Make 变量（`CORE_OBJS`、`CFLAGS`、`DS4_LINK`、`DS4_LINK_LIBS`）。
3. **链接库**：你的 oneAPI 后端需要链接哪些库？（参考 [STRIXHALO.md:8-19](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/STRIXHALO.md#L8-L19) 怎样为 ROCm 列系统库。）
4. **编译期宏**：你是否需要一个 `-DDS4_ONEAPI_BUILD`？它会像 `DS4_ROCM_BUILD` 那样在 [ds4.c:82](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L82) 这类条件里出现吗？`parse_backend`（[ds4_cli.c:165-180](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L165-L180)）要怎么改？
5. **帮助文案**：`make` 的 help（[Makefile:82-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L82-L91)）要加一行什么？

**参考思路**（不是唯一答案）：

- 新增 `ds4_oneapi.o`，由 `ds4_oneapi.cpp`（或 `.cu` 风格的源）用 `icpx` 编译，规则仿照 [Makefile:214-215](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L214-L215)。
- `make oneapi` 目标仿照 `strix-halo`，把 `CORE_OBJS` 末尾换成 `ds4_oneapi.o`，`DS4_LINK` 换成 `icpx`，`DS4_LINK_LIBS` 换成 oneAPI 的数学库（如 `-lsycl -lonemkl`），并加 `-DDS4_ONEAPI_BUILD`。
- 是否复用 `DS4_BACKEND_CUDA` 枚举位取决于 oneAPI 后端是否也实现 `ds4_gpu.h` 接口；若是，则像 ROCm 一样复用最省事；若接口差异大，才需要在 [ds4.h:19-23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L19-L23) 新增枚举值。
- help 文案加一行 `@echo "  make oneapi             Build oneAPI for Intel GPUs"`。
- 最重要的一点：因为整个改动**只发生在 `Makefile` 与新增的 `ds4_oneapi.*` 文件里，不动 `ds4.c` 的引擎核心**，这正是 ds4「后端可替换」架构的价值。

## 6. 本讲小结

- ds4 的 `Makefile` 用 `ifeq ($(UNAME_S),Darwin)` 把构建拆成 macOS（Metal）与 Linux（CUDA/ROCm）两套几乎独立的规则，**同一变量名 `CORE_OBJS` 在两个分支里取值不同**。
- `CORE_OBJS = ds4.o + ds4_distributed.o + ds4_ssd.o + 一个后端 .o`；后端 `.o` 在 `ds4_metal.o`/`ds4_cuda.o`/`ds4_rocm.o` 之间切换；CPU 构建则没有后端对象，且第一个对象变成 `ds4_cpu.o`。
- 在 Linux 上裸 `make` **只打印帮助**（`all: help`），因为 CUDA 编译必须显式指定目标架构，作者不替用户猜；四个真正的后端目标是 `cuda-spark`/`cuda-generic`/`cuda CUDA_ARCH=`/`strix-halo`，外加 `cpu`。
- `make strix-halo` 是唯一用「命令行覆盖 Make 变量」手法的目标，一次性改写 `CORE_OBJS`、`CFLAGS`、`DS4_LINK`、`DS4_LINK_LIBS`，把整条 CUDA 链替换成 ROCm 链。
- `-DDS4_NO_GPU` 是 CPU 构建的核心开关：它让每个源文件被重编成 `*_cpu.o`，并在编译期用 `#ifdef` 切掉所有 GPU 代码，使「引擎核心自身退化为 CPU 后端」。
- `-DDS4_ROCM_BUILD` 不删代码而是激活 ROCm 专属优化；ROCm 在枚举层复用 `DS4_BACKEND_CUDA`，靠编译期宏区分底层实现，`default_backend()` 与 `parse_backend()` 把这套编译期/运行期的叠加选择串了起来。

## 7. 下一步学习建议

- 本讲解决的是「怎么把项目编出来」，下一讲 [u1-l5 下载模型与首次运行](u1-l5-download-and-first-run.md) 会用 `download_model.sh` 拉一个 GGUF，用 `./ds4 -p "..."` 跑通第一次推理，建议紧接着做。
- 如果你对「后端对象到底实现了什么接口」好奇，可以先跳到 [u5-l1 GPU 张量抽象与 CPU 后端](u5-l1-gpu-abstraction-and-cpu.md)，看 `ds4_gpu.h` 定义的 `ds4_gpu_tensor` 生命周期与命令原语，那会让你更明白为什么 Metal/CUDA/ROCm 三个 `.o` 能彼此替换、又为什么 CPU 不需要这套抽象。
- 想提前理解 `-DDS4_NO_GPU` 在 `ds4.c` 里具体切掉了哪些大块代码，可以直接 `grep -n 'DS4_NO_GPU' ds4.c`，对照本讲引用的 [ds4.c:3371-3396](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3371-L3396) 阅读几处典型的 `#ifdef` 分支。
- 如果你的目标硬件是 Strix Halo，建议把 [STRIXHALO.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/STRIXHALO.md) 完整读一遍——它除了构建命令，还包含 ROCm 装机、内核参数（`amdgpu.gttsize` 等）与权限配置，是 `make strix-halo` 之外同样关键的一环。
