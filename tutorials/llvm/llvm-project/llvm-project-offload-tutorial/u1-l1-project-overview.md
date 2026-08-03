# 项目定位与 OpenMP 目标卸载概念

## 1. 本讲目标

本讲是整本《LLVM Offload 子项目学习手册》的第一篇。读完本讲，你应该能够：

- 说出 **LLVM Offload 子项目**是什么、它解决了哪一类问题，以及它支持哪些主机和设备架构。
- 用通俗语言描述 **OpenMP 目标卸载（target offloading）** 的基本执行模型：什么是主机、什么是设备，`target` / `target data` / `map` 子句分别扮演什么角色。
- 区分项目内部两个层次：传统的 **libomptarget 运行时** 与仍在开发中的统一 **liboffload 新 API**，理解它们各自面向谁、各自处于什么成熟度。

本讲**不要求你事先会编译或运行**任何代码。它只负责建立"全局认识"，真正动手构建和运行是第二、第四讲的事。

## 2. 前置知识

阅读本讲前，最好对下面这些概念有最粗浅的了解；如果没有也没关系，本讲会顺带解释。

- **编译器与运行时（runtime）的分工**：编译器（如 Clang）把源码翻译成机器码，而有些功能（比如把代码搬到 GPU 上跑）需要一个在程序运行期间一直陪着的"助手库"，这个库就叫运行时库。Offload 子项目的大部分内容就是这个运行时库。
- **加速器 / 协处理器（accelerator / co-processor）**：CPU 之外、专门干某类活的硬件，比如 GPU、FPGA、AI 加速卡。它们通常有自己的指令集，和 CPU（主机）不是同一种架构。
- **OpenMP**：一种用于共享内存并行编程的 API 标准，通过编译指示（pragma）告诉编译器"这段循环可以并行"。其中 `target` 指令族是 OpenMP 用来把代码"卸载"到加速器上的扩展。
- **主机（host）与设备（device）**：在卸载语境里，"主机"指运行主程序的 CPU，"设备"指被卸载代码执行的加速器。

> 术语提示：后面会反复出现 **"卸载（offload）"** 一词，它特指"把一段计算从主机搬到设备上去执行，必要时再把数据搬回来"这件事。

## 3. 本讲源码地图

本讲主要读懂项目自己写的"说明书"类文件，目的是建立全局认识，暂不深入实现代码。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md) | 子项目的总入口说明：定位、目标范围、成熟度、社区会议信息。 |
| [README.txt](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt) | 传统 `libomptarget` 运行时的简短说明，列出已测试的主机架构与支持的卸载设备架构。 |
| [Maintainers.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/Maintainers.md) | 维护者名单，说明这个子项目由谁负责，是判断"去哪里提问题"的依据。 |
| [docs/index.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/docs/index.md) | Sphinx 文档主入口，目前主要聚合 `offload-api`（即 liboffload 的 API 文档）。 |
| [liboffload/README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md) | liboffload 子目录的说明，是理解"新统一 API"与 libomptarget 关系的关键。 |

这五个文件就够你回答"这个项目是什么"。后面的章节会把它们逐段读给你看。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Offload 子项目是什么**——回答"定位与目标"。
2. **OpenMP 目标卸载执行模型**——回答"它在服务的那件事到底怎么运作"。
3. **libomptarget 与 liboffload 的关系**——回答"项目内部为什么要分两层"。

### 4.1 Offload 子项目是什么

#### 4.1.1 概念说明

LLVM 是一个编译器基础设施项目，里面包含了很多"子项目"（subproject），比如 `clang`、`llvm` 核心、`compiler-rt`、`openmp` 等。**Offload** 就是其中一个相对新的子项目。

它的使命用一句话概括：**为加速器和协处理器提供工具、运行时和 API，让用户能把代码跑在与主机架构可能不同的设备上。**

关键词有三个，对应项目里三类东西：

- **工具（tooling）**：比如查看设备信息的 `llvm-offload-device-info`、重放内核的 `llvm-omp-kernel-replay`、生成 API 头文件的 `offload-tblgen`（这些在 `tools/` 目录，后续讲义会讲）。
- **运行时（runtimes）**：程序运行期间一直陪伴的库，最核心的就是 `libomptarget`，负责"把数据搬过去、把内核启动起来、再把结果搬回来"。
- **API**：面向其他语言运行时或工具开发者暴露的统一接口，即仍在开发中的 `liboffload`。

#### 4.1.2 核心流程

从"用户写了一段要卸载的代码"到"代码真的在设备上跑起来"，粗略经历下面几步（本讲只建立印象，细节留给后续讲义）：

```text
用户源码（含 OpenMP target 指令）
        │  Clang 编译
        ▼
主机可执行文件 + 设备镜像（device image，二进制）
        │  程序启动，运行时库 libomptarget 陪伴
        ▼
运行时识别设备 → 加载设备镜像 → 搬运数据 → 启动内核 → 回收结果
```

注意：编译阶段就把"给设备用的机器码"准备好了，运行时负责的是**调度、搬运和启动**，而不是翻译。这也是为什么这个项目叫"运行时（runtime）"。

#### 4.1.3 源码精读

先看项目总入口 [README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md) 的开篇定位：

> [README.md:L3-L7](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md#L3-L7) 说明：Offload 子项目的目标是提供"工具、运行时和 API"，让用户能在与主机架构**可能不同**的加速器或协处理器上执行代码；长远来看，CPU、GPU、FPGA、AI/ML 加速器、分布式资源等各类目标都在范围内。

这一段是整个项目的"宪法"。注意它强调"may or may not match the architecture of their host"——设备架构和主机架构可以不一样，这正是卸载（offload）区别于普通并行的关键。

接着看成熟度声明：

> [README.md:L9-L14](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md#L9-L14) 说明：**对 OpenMP 卸载用户来说，项目已经成熟可用**；而最终统一的 API 设计仍在开发中。这一句非常重要，它直接奠定了本讲后面"libomptarget 已成熟、liboffload 在开发中"的结论。

再看传统运行时的 [README.txt](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt)，它给出了具体硬件支持范围：

> [README.txt:L8-L23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt#L8-L23) 说明：当前库只在 Linux 上测试过；支持的主机架构有 Intel 64、IBM Power（大/小端）、ARM AArch64；支持的**卸载设备架构**包括 x86_64、Power、AArch64，以及 NVIDIA CUDA GPU 和 AMD GPU。

读这段时要分清两类清单：**主机架构**（程序整体运行的 CPU）和**设备架构**（被卸载代码执行的加速器）。注意"设备"里既包含普通 CPU 架构（也就是说可以把代码"卸载"到另一块 CPU 上），也包含 GPU。这也呼应了 README.md 说的"all kinds of targets"。

> 术语提示：把代码"卸载"到一块 CPU（host 插件）听起来奇怪，但它是项目的参考实现——后续 u3-l3 会专门走读最简单的 host 插件。

最后看治理信息 [Maintainers.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/Maintainers.md)：

> [Maintainers.md:L9-L13](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/Maintainers.md#L9-L13) 说明：当前维护者是 Johannes Doerfert 和 Joseph Huber。遇到设计问题、提 patch 时，这两位是最终决策者。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，不需要编译。

1. **实践目标**：用一句话概括"Offload 子项目解决什么问题"，并说出它支持的两类设备架构代表（CPU 类 / GPU 类）。
2. **操作步骤**：
   - 打开本讲引用的 [README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md)，找到第 3–7 行的定位句。
   - 打开 [README.txt](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt)，把"主机架构"和"设备架构"两份清单抄下来。
3. **需要观察的现象**：你会注意到"设备架构"清单比直觉更宽——它包含了 CPU 架构，说明卸载的目标不一定是 GPU。
4. **预期结果**：你能写出类似"Offload 让 LLVM 能把代码运行在与主机架构可能不同的设备上；当前设备支持 x86_64/Power/AArch64 等 CPU，以及 NVIDIA/AMD GPU"的句子。
5. 如果无法本地确认某些架构是否还在维护，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：README.md 说目标"in the long run"包括 FPGA、AI/ML 加速器等，但 README.txt 只列了 CPU 和 GPU。这两份文件矛盾吗？为什么？
> **参考答案**：不矛盾。README.md 描述的是**长远愿景**（in the long run），README.txt 描述的是**当前 `libomptarget` 已测试支持**的具体清单。愿景大于现状是正常的。

**练习 2**：为什么 README.txt 要把"主机架构"和"设备架构"分成两份清单？
> **参考答案**：因为卸载天然涉及两套架构：主机架构决定主程序和运行时本身在哪跑，设备架构决定被卸载的代码能在哪跑，两者可以不同，所以必须分别声明。

### 4.2 OpenMP 目标卸载执行模型

#### 4.2.1 概念说明

Offload 子项目目前最成熟的用户是 **OpenMP 卸载用户**。所以要理解这个项目，先要理解 OpenMP 是怎么"卸载"代码的。这一节讲的是 OpenMP 语言层面的概念（不是本项目内部代码）。

OpenMP 用编译指示来标注"这段代码要搬到设备上"。最核心的三个概念：

- **`target` 区域（target region）**：一段要在设备上执行的代码。进入区域时代码"切换"到设备上跑，离开时再"切回"主机。
- **`target data` 区域**：本身不执行计算，只负责**声明在这段范围内，某些数据要存在于设备上**，并控制数据在主机和设备之间的搬运时机。
- **`map` 子句**：附加在 `target` / `target data` 上，告诉运行时"这块主机内存该如何映射到设备"，常见方向有：
  - `to`：进入区域时，把数据从主机**拷到**设备。
  - `from`：离开区域时，把数据从设备**拷回**主机。
  - `tofrom`：两个方向都做（最常见）。
  - `alloc`：只在设备上分配，不搬运。

下面是一段**示例代码**（仅为说明概念，非项目自带示例），帮助你建立直觉：

```c
// 示例代码：演示 OpenMP target 卸载的典型写法
#include <stdio.h>
int main(void) {
    const int N = 1024;
    float a[N], b[N], c[N];
    for (int i = 0; i < N; ++i) { a[i] = i; b[i] = 2 * i; }

    // target 区域：循环在设备上并行执行
    #pragma omp target map(to: a, b) map(from: c)
    #pragma omp parallel for
    for (int i = 0; i < N; ++i) {
        c[i] = a[i] + b[i];
    }

    printf("c[0]=%f c[N-1]=%f\n", c[0], c[N-1]);
    return 0;
}
```

读这段示例时，把三件事对应起来：

1. `#pragma omp target` —— 触发一次**内核启动**（在设备上跑那段循环）。
2. `map(to: a, b)` —— 进入区域前，运行时把 `a`、`b` 从主机搬到设备。
3. `map(from: c)` —— 离开区域后，运行时把 `c` 从设备搬回主机。

这三件事，恰好就是本项目的 `libomptarget` 运行时要干的核心活。

#### 4.2.2 核心流程

把上面那段示例的执行过程画成时间线：

```text
主机侧                        设备侧
──────                        ──────
[运行到 target 指令]
   │
   ├─ map(to): 把 a,b 拷贝过去 ──────────► [设备收到 a,b]
   ├─ 启动内核(parallel for)  ──────────► [设备执行 c[i]=a[i]+b[i]]
   │   (运行时在这里等待/同步)              │
   ◄──────────── 完成信号 ────────────────┘
   ├─ map(from): 把 c 拷贝回来 ◄────────── [设备送回 c]
   ▼
[继续主机代码，打印 c]
```

整个过程里，**"何时搬数据、往哪搬、何时启动内核、何时同步"** 都由运行时根据 `map` 子句和 `target` 指令来决定。编译器（Clang）负责把这些指令翻译成对运行时入口函数的调用（这些入口函数在 u1-l5 会讲），运行时负责真正去搬、去启动。

#### 4.2.3 源码精读

本节是概念讲解，没有直接对应的实现源码（实现要等到第二单元）。但有两处项目文档能印证这个执行模型：

> [README.md:L9](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md#L9) 说明：项目明确把"For OpenMP offload users"作为当前可用对象，印证了上面描述的 OpenMP 卸载模型就是本项目的主战场。

> [README.txt:L2-L3](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt#L2-L3) 说明：这套库的全名是 "LLVM OpenMP Offloading Runtime Library (libomptarget)"，即它最初、最核心的身份就是 OpenMP 的卸载运行时。

> 提示：本讲不展开运行时内部如何实现 `map`、如何启动内核。那是 u2-l5（target data 流程）和 u2-l6（内核启动流程）的内容。本节只要建立"指令 → 运行时动作"的直觉即可。

#### 4.2.4 代码实践

1. **实践目标**：在不编译的前提下，能口头追踪一段带 `map` 的 `target` 代码的数据流向。
2. **操作步骤**：
   - 回看 4.2.1 的示例代码。
   - 把 `map(to: a, b) map(from: c)` 改成 `map(tofrom: a)`，在心里重新走一遍 4.2.2 的时间线。
3. **需要观察的现象**：体会"改成 `tofrom` 后，`a` 既会被搬过去、也会被搬回来，而 `b`、`c` 不再参与搬运"。
4. **预期结果**：你能说出"进入区域搬 `a` 过去、启动内核、离开区域搬 `a` 回来"。
5. 真正编译运行需要 u1-l2（构建）和 u1-l4（工具链）的知识，本讲暂不做，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果一个变量只在设备上用作临时空间，既不需要初始值、也不需要把结果带回主机，该用哪种 `map` 方向？
> **参考答案**：`alloc`。它只负责在设备上分配空间，不做任何方向的搬运。

**练习 2**：`target data` 区域和 `target` 区域有什么本质区别？
> **参考答案**：`target` 区域会真正在设备上**执行**一段代码并启动内核；`target data` 区域只负责**管理数据的存在与搬运**，本身不执行设备计算。运行时对两者都会处理 `map` 子句，但只有 `target` 会触发内核启动。

### 4.3 libomptarget 与 liboffload 的关系

#### 4.3.1 概念说明

到目前为止你可能会问：项目里同时出现 `libomptarget` 和 `liboffload`，它们是什么关系？这是本讲最容易被初学者搞混的一点，专门拿出来讲。

- **libomptarget**：传统、成熟的 OpenMP 卸载运行时。它**绑定 OpenMP**，直接为 Clang 编译出的 OpenMP 卸载程序服务。README.md 说的"For OpenMP offload users, the project is ready and fully usable"指的就是它。
- **liboffload**：一个**正在开发中（work-in-progress）的新 API**。它的目标是提供一层**不绑定 OpenMP** 的统一抽象，让各种卸载语言运行时（不只是 OpenMP）都能复用底层的设备插件。README.md 说的"The final API design is still under development"指的就是它。

一句话总结它们的关系：**libomptarget 是当前主力；liboffload 是面向未来的、更通用的统一 API，它建在已有的设备插件之上。**

#### 4.3.2 核心流程

从分层角度看，两者和底层插件的关系如下：

```text
        用户程序（OpenMP 卸载）           其他卸载语言运行时（未来）
                │                              │
        ┌───────▼────────┐           ┌─────────▼─────────┐
        │  libomptarget  │           │     liboffload    │  ← 统一、不绑定 OpenMP
        │ (成熟/绑定OMP) │           │  (开发中/通用API) │
        └───────┬────────┘           └─────────┬─────────┘
                │            都建立在            │
                └────────► 设备插件 ◄────────────┘
                      (plugins-nextgen: CUDA/AMDGPU/Level Zero/host)
                                │
                                ▼
                          真实硬件设备
```

关键点：**libomptarget 和 liboffload 都不是从零实现设备交互，它们都依赖同一套底层设备插件（plugins-nextgen）**。区别只在"上层 API 面向谁"：libomptarget 面向 OpenMP，liboffload 面向通用卸载。这也是后续第三单元要专门讲插件架构的原因——插件才是真正和硬件打交道的地方。

#### 4.3.3 源码精读

liboffload 的定位写得非常清楚，看 [liboffload/README.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md)：

> [liboffload/README.md:L3-L6](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md#L3-L6) 说明：liboffload 是"work-in-progress 的新 API"，它**建立在已有的插件实现之上**，提供一层适合实现**多种**卸载语言运行时（而不仅仅是 OpenMP）的抽象。这句直接定义了 4.3.1 的结论。

再看它对测试与成熟度的说明：

> [liboffload/README.md:L19-L28](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md#L19-L28) 说明：可以用环境变量 `OFFLOAD_TRACE=1` 打开 API 调用追踪；并明确写出"The host plugin is not currently supported"——这印证了 liboffload 还不完整、仍在开发中，而成熟的 libomptarget 是支持 host 插件的。

最后回到文档主入口 [docs/index.md](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/docs/index.md)：

> [docs/index.md:L6-L13](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/docs/index.md#L6-L13) 说明：Sphinx 文档目前只聚合了 `offload-api`（即 liboffload 的 API 文档）。这暗示了项目的文档化重心正逐步向新 API 倾斜，但实现层面的主力仍是 libomptarget。

#### 4.3.4 代码实践

1. **实践目标**：写出 libomptarget 与 liboffload 的"一句话定位 + 一个关键差异"。
2. **操作步骤**：
   - 重读 [README.md:L9-L14](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md#L9-L14) 中关于成熟度的句子。
   - 重读 [liboffload/README.md:L3-L6](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md#L3-L6) 中关于"不仅仅 OpenMP"的句子。
3. **需要观察的现象**：你会看到两份文档在"是否绑定 OpenMP""是否成熟"两个维度上正好互补。
4. **预期结果**：你能写出类似"libomptarget 是成熟、绑定 OpenMP 的运行时；liboffload 是开发中、不绑定 OpenMP 的统一 API，二者共享底层设备插件"的总结。
5. 如果想进一步验证"二者共享插件"这一点，需要阅读插件目录源码，属 u3-l1 范畴，本讲标注「待后续讲义确认」。

#### 4.3.5 小练习与答案

**练习 1**：假如你想给一种**新的、非 OpenMP** 的卸载语言写运行时，应该直接用 libomptarget 吗？
> **参考答案**：不理想。libomptarget 绑定 OpenMP，更适合 OpenMP 卸载。新语言运行时更适合基于 liboffload 这层通用抽象来构建（这正是 liboffload 的设计目的）。

**练习 2**：`OFFLOAD_TRACE` 和 libomptarget 的调试手段是同一套吗？
> **参考答案**：不是。`OFFLOAD_TRACE` 专门用于追踪 **liboffload** 的 API 调用；libomptarget 有自己的一套环境变量（如 `LIBOMPTARGET_INFO` / `LIBOMPTARGET_DEBUG`），这些会在 u1-l4 详细讲。

## 5. 综合实践

把本讲三个模块串起来，完成一份**"项目认知卡片"**（一页纸即可）：

1. **定位**：用一句话写出 LLVM Offload 子项目解决什么问题（依据 [README.md:L3-L7](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.md#L3-L7)）。
2. **硬件范围**：列出至少 2 种主机架构和 3 种设备架构（依据 [README.txt:L8-L23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/README.txt#L8-L23)），并标注哪个清单里出现了 GPU。
3. **执行模型**：画一张"主机 → 搬数据 → 设备执行内核 → 搬回数据 → 主机"的简单时间线，并在每个箭头上标注对应的 OpenMP 概念（`map(to)` / `target` / `map(from)`）。
4. **两层关系**：写明 libomptarget（成熟、绑定 OpenMP）与 liboffload（开发中、通用）的差异，以及"二者共享底层设备插件"这一点（依据 [liboffload/README.md:L3-L6](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/README.md#L3-L6)）。

完成后，你应该能用这一页纸向一个完全没接触过该项目的人解释清楚"它是什么"。这份卡片也建议你保留，后续每学完一讲都可以回头补充实现细节。

## 6. 本讲小结

- LLVM **Offload 子项目**为加速器/协处理器提供工具、运行时和 API，目标是把代码运行在与主机架构可能不同的设备上。
- 当前对 **OpenMP 卸载用户**已成熟可用，统一的 API（liboffload）仍在开发中。
- 支持的设备架构涵盖 CPU（x86_64/Power/AArch64）与 GPU（NVIDIA CUDA、AMD），主机仅在 Linux 上测试过。
- OpenMP 目标卸载的核心模型是：用 `target` 触发设备内核、用 `map` 子句控制主机↔设备的数据搬运，运行时负责把这些指令落实成真实的搬运和启动动作。
- 项目内部分两层：**libomptarget**（绑定 OpenMP 的成熟运行时）与 **liboffload**（不绑定 OpenMP 的通用新 API），二者都建立在同一套底层设备插件之上。
- 本讲只建立了全局认识，尚未涉及任何编译、运行或源码实现细节。

## 7. 下一步学习建议

有了全局认识后，建议按下面的顺序继续：

- **u1-l2 构建系统与依赖**：学会如何把项目真正编译出来，理解插件选择（cuda/amdgpu/level_zero/host）。
- **u1-l3 目录结构与模块全景**：建立"目录 → 职责"的映射，方便后续快速定位源码。
- **u1-l4 工具链、编译运行与设备信息**：第一次真正编译运行一个卸载程序，并用工具观察设备。
- **u1-l5 编译器-运行时契约与核心数据结构**：看 Clang 生成的 `__tgt_*` 入口如何与运行时对接，正式进入实现层面。

如果你只想先看"最简单的参考实现长什么样"，也可以在学完 u1-l2、u1-l3 后直接跳到 **u3-l3 host 插件完整走读**——host 插件不依赖 GPU，是最容易读懂的插件骨架。
