# 目录结构与源码组织

## 1. 本讲目标

CUTLASS 是一个有上千个头文件、覆盖三代 API 的大型模板库。第一次打开仓库时，最容易迷失在目录里。

学完本讲，你应该能够：

- 说出仓库**顶层目录**各自的职责（`include/`、`examples/`、`tools/`、`test/`、`python/`、`docs/`）。
- 区分两套并列的核心库：**`include/cutlass/`**（经典线性代数模板）与 **`include/cute/`**（CuTe 布局代数与 Atom 抽象），并理解它们为什么分工。
- 看懂 `include/cutlass/` 内部按「计算阶段」拆分的子目录（`gemm/`、`conv/`、`epilogue/`、`layout/`、`arch/` 等），并能凭目录名推测里面放的是什么代码。
- 拿到一个需求（例如「我想改卷积」），能直接定位到正确的目录，而不是满仓库乱翻。

本讲**只讲「东西放在哪、为什么放在那」**，不展开任何算法细节——那是后面进阶层讲义的任务。承接上一讲的结论：CUTLASS 是 header-only 库，编译发生在使用者实例化内核时，因此「读目录」就是「读这个库的地图」。

## 2. 前置知识

在开始之前，你需要先建立两个直觉（上一讲 `u1-l1` 已介绍，这里复习要点）：

1. **GEMM 的层次化分解**：一次大矩阵乘法被拆成 `device → threadblock → warp → thread/指令` 四层。CUTLASS 把每一层写在不同目录里，所以目录结构本质上是**层次结构的镜像**。
2. **三代 API 并存**：2.x（`device::Gemm` 经典四层）、3.x（基于 CuTe 的 kernel + collective + epilogue 三段式）、4.x（Python CuTe DSL）。三代代码都还在仓库里，必须能分辨「我现在看的是哪一代」。

两个本讲会用到的术语：

- **header-only（仅头文件库）**：整个库没有 `.cpp` 要单独编译，能力全在 `include/` 的头文件模板里。使用者 `#include` 的那一刻，编译才真正发生。
- **命名空间（namespace）**：C++ 里给符号分组的机制。CUTLASS 主要用 `cutlass::` 与 `cute::` 两个顶层命名空间，恰好对应 `include/` 下的两个目录。

## 3. 本讲源码地图

本讲涉及的「源码」其实就是**目录与少量入口头文件**。下表是本讲的关键坐标：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目自带的「Project Structure」章节，本讲目录划分的权威依据。 |
| `include/cutlass/gemm/gemm.h` | GEMM 模块的总入口之一，定义 GEMM 通用类型；它的 `#include` 揭示 cutlass 与 cute 的关系。 |
| `include/cute/config.hpp` | CuTe 库的编译配置入口，定义贯穿整个 cute 的宏。 |
| `include/cutlass/cutlass.h` | 整个 cutlass 库的总入口，定义 `Status` 等公共类型。 |

> 小提示：在 CUTLASS 里，**目录名往往就是命名空间名**。`include/cutlass/gemm/` 对应 `cutlass::gemm`，`include/cute/atom/` 对应 `cute::` 下的 Atom 相关类型。记住这条规律，目录读起来会快很多。

## 4. 核心概念与源码讲解

### 4.1 顶层目录布局

#### 4.1.1 概念说明

打开仓库根目录，你会看到几类文件混在一起：构建脚本（`*.cmake`、`CMakeLists.txt`）、文档（`README.md`、`CHANGELOG.md`、`Doxyfile`）、许可证（`LICENSE.txt`、`EULA.txt`），以及一组**目录**。对一个库来说，真正重要的是这些目录：

- `include/`：库本身，全部是头文件。这是你**唯一需要加进编译 include path** 的目录。
- `examples/`：SDK 示例，把模板拼成可运行程序的「菜谱」。
- `tools/`：实例库、profiler、utilities 等工具链。
- `test/`：基于 Google Test 的单元测试。
- `python/`：4.x 的 Python CuTe DSL 与代码生成器。
- `docs/`：Doxygen 生成的 HTML 文档产物。
- `media/`、`cmake/`：辅助资源与 CMake 模块。

#### 4.1.2 核心流程

阅读一个陌生的大型仓库，推荐这个固定顺序：

1. 先看根目录有哪些**目录**（忽略散落的配置文件）。
2. 找到「库本体」目录（CUTLASS 是 `include/`）。
3. 看「示例」目录（`examples/`），它告诉你库怎么用。
4. 看「测试」目录（`test/`），它告诉你库的行为契约。
5. 看「工具」目录（`tools/`），它通常是产出路径（profiling、实例化）。

这个顺序后面每个大型 C++ 项目都能复用。

#### 4.1.3 源码精读

CUTLASS 官方在 README 里专门有一节 **Project Structure** 描述目录组织，这是最权威的依据：

- [README.md:290-298](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L290-L298)：开头一句话点明「CUTLASS 由 header-only 库 + Utilities + Tools + Examples + 单元测试组成」，并指向更详细的 code_organization 文档。

构建入口与构建辅助脚本都放在根目录，例如顶层 [CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt) 负责 `add_subdirectory` 各子项目；`CUDA.cmake`、`cuBLAS.cmake`、`cuDNN.cmake` 则是按需查找依赖的模块文件。根目录还放了 `Doxyfile`（生成 `docs/` 下 API 文档的配置）。

#### 4.1.4 代码实践

**实践目标**：用最朴素的命令把顶层目录看清楚，建立「仓库全景」印象。

**操作步骤**（在仓库根目录执行）：

```bash
# 1. 只列出顶层「目录」（-F 给目录加 / 标记，grep 过滤出目录）
ls -F . | grep '/$'

# 2. 顺便看看根目录有哪些构建/说明文件
ls *.cmake CMakeLists.txt README.md CHANGELOG.md 2>/dev/null
```

**需要观察的现象**：第一条命令应输出 `cmake/ docs/ examples/ include/ media/ python/ test/ tools/` 这一组目录（顺序可能不同）。第二条命令应列出多个 `.cmake` 文件与 `CMakeLists.txt`。

**预期结果**：你会确认「库本体 = `include/`，其余目录都是围绕它的工具/示例/测试」。这一步无需 GPU，纯文件系统操作，一定能完成。

#### 4.1.5 小练习与答案

**练习 1**：根目录下的 `docs/` 目录里全是 `.html` 文件，这说明了什么？

**答案**：`docs/` 是 Doxygen **生成的产物**（由根目录的 `Doxyfile` 配置生成），不是手写源码。所以阅读 API 文档既可以本地生成，也可以直接看官方在线文档；如果要改文档内容，应该改 `include/` 里的注释，而不是改 `docs/` 里的 HTML。

**练习 2**：为什么 `include/` 是「唯一需要加进编译 include path 的目录」？

**答案**：因为 CUTLASS 是 header-only 库，所有能力都在 `include/` 的头文件模板里；`examples/`、`tools/`、`test/` 是独立可执行目标，各自有自己的 `CMakeLists.txt`，不会被使用者 `#include`。

---

### 4.2 include/cutlass 子模块职责

#### 4.2.1 概念说明

`include/cutlass/` 是 CUTLASS 的**经典库本体**，对应 `cutlass::` 命名空间。它的子目录大致按两种维度组织：

- **按计算阶段**：`gemm/`（矩阵乘）、`conv/`（卷积）、`epilogue/`（尾声后处理）、`transform/`（布局/类型/域变换）、`reduction/`（归约）。
- **按抽象层次**：`arch/`（指令级）、`thread/`（单线程 SIMT）、`warp/`、`threadblock/`、`device/`、`kernel/`、`collective/`（3.x 特有）。

这两条维度交叉，所以你会在 `gemm/` 下再次看到 `device/`、`threadblock/`、`warp/`、`thread/`、`kernel/`、`collective/` 等子目录——这正是 GEMM 层次化分解在文件系统上的投影。

#### 4.2.2 核心流程

理解 `include/cutlass/` 的子目录，可以用下面这张「职责速查表」：

| 子目录 | 职责 | 什么时候去看 |
| --- | --- | --- |
| `arch/` | 直接暴露硬件指令（如 `mma_sm80.h` 的 mma 指令） | 关心某代架构的指令封装时 |
| `gemm/` | 矩阵乘各层实现 | 写/读 GEMM 时（最高频） |
| `conv/` | 卷积（隐式 GEMM）各层实现 | 做卷积时 |
| `epilogue/` | GEMM/卷积的尾声后处理（缩放、激活、写回） | 要融合后处理时 |
| `layout/` | 矩阵/张量在内存里的布局标签 | 关心 RowMajor/ColumnMajor/TensorOp 时 |
| `transform/` | 布局/类型/域的变换 | 关心 im2col、类型转换时 |
| `pipeline/` | Hopper 异步流水线同步原语 | 读 warp-specialized 内核时 |
| `platform/` | CUDA 可用的「标准库」组件 | 需要容器/工具类型时 |
| `thread/`、`reduction/`、`detail/`、`experimental/` | 单线程运算、归约、内部细节、实验特性 | 按需 |

#### 4.2.3 源码精读

README 的 Project Structure 给出了官方对 `include/cutlass/` 各子目录的说明，这是本节的权威依据：

- [README.md:304-324](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L304-L324)：`cutlass/` 子树，逐行说明 `arch/`、`conv/`、`epilogue/`、`gemm/`、`layout/`、`platform/`、`reduction/`、`thread/`、`transform/` 的职责，并注明根目录散落的头文件是「核心词汇类型、容器与基本数值运算」。

我们再用一个真实入口文件验证「按阶段 + 按层次」的交叉结构。GEMM 模块的总入口 [include/cutlass/gemm/gemm.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm.h) 的文件头注释和 include 段：

- [include/cutlass/gemm/gemm.h:31-33](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm.h#L31-L33)：注释写明「Defines common types used for all GEMM-like operators」——即这个文件只放**跨所有 GEMM 变体的公共类型**，具体实现分散在 `device/`、`threadblock/` 等子目录。
- [include/cutlass/gemm/gemm.h:36-42](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm.h#L36-L42)：它的 `#include` 列表同时拉入了 `cutlass/cutlass.h`、`cutlass/layout/matrix.h`、`cutlass/gemm/gemm_enumerated_types.h`，**以及 `cute/layout.hpp`**。这一行非常关键：它说明 3.x 之后，`cutlass::gemm` 已经**依赖 `cute::`** 了——两套库不是割裂的，而是 cutlass 站在 cute 之上。

在这个文件里还能看到一个真实的公共类型定义：

- [include/cutlass/gemm/gemm.h:51-55](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm.h#L51-L55)：`enum class ScalingKind { kTensorwise, kBlockwise };`——缩放方式枚举，正是「所有 GEMM 变体都要用到的公共类型」的一个具体例子。

#### 4.2.4 代码实践

**实践目标**：亲手验证「目录名 = 职责」，并用文件计数感受各子模块的体量。

**操作步骤**（在仓库根目录执行）：

```bash
# 1. 列出 include/cutlass 下的子目录
ls -d include/cutlass/*/

# 2. 数一数各核心子目录里有多少个头文件
for d in gemm conv epilogue layout arch pipeline transform; do
  printf "%-12s %s 个 .h/.hpp\n" "$d" "$(find include/cutlass/$d -type f \( -name '*.h' -o -name '*.hpp' \) | wc -l)"
done
```

**需要观察的现象**：第一条命令应输出 `arch/ conv/ epilogue/ gemm/ ...` 等子目录。第二条命令会打印每个子目录的头文件数量，你会看到 `gemm/` 和 `arch/` 的文件数明显多于其他目录——这与「GEMM 是库的核心、arch 覆盖多代架构指令」一致。

**预期结果**：你能用真实数字回答「哪个子目录最重」，而不是凭空猜测。具体数字随版本变化，以你本地实际输出为准（无需 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：如果你想给一个 GEMM 加一个 ReLU 激活的后处理，应该去哪个目录找相关代码？

**答案**：`include/cutlass/epilogue/`。激活、缩放、偏置这类「矩阵乘完之后」的处理都属于 epilogue 阶段（3.x 还会在 `epilogue/fusion/` 下提供组合式的融合访客树 EVT，那是进阶讲义的内容）。

**练习 2**：`include/cutlass/gemm/gemm.h` 为什么要 `#include "cute/layout.hpp"`？这说明了两套库什么关系？

**答案**：因为 3.x 的 GEMM 类型用 CuTe 的 `Layout` 来描述张量布局。这说明在 3.x 之后，`cutlass::`（经典库）**构建在 `cute::`（布局代数）之上**，cute 是更底层的公共抽象。

---

### 4.3 include/cute 核心抽象

#### 4.3.1 概念说明

`include/cute/` 是 CUTLASS 3.x 引入的 **CuTe（CUTLASS Tensor Operations）** 库，对应 `cute::` 命名空间。如果说 `include/cutlass/` 是「现成的菜」，那 `include/cute/` 就是「一套厨具与一套做菜代数」。

CuTe 的核心思想是把「数据长什么样」和「数据放在哪」彻底抽象成两个正交概念：

- **Layout**：一个纯函数，把多维坐标映射到一维索引。它只描述「形状 + 步长」，不关心数据本身。
- **Tensor**：`Layout` 加上一个数据指针/引擎，组成可访问的张量。
- **Atom**：把硬件指令（如一条 mma 指令）封装成可复用的「计算原子」或「拷贝原子」。

这套抽象的好处是：同一个 `cute::copy` 或 `cute::gemm` 算法，配上不同的 Layout/Atom 就能跑在不同架构、不同内存空间上，做到了算法与硬件解耦。

#### 4.3.2 核心流程

`include/cute/` 的子目录同样按职责划分，但维度和 `cutlass/` 不同：

| 子目录 | 职责 |
| --- | --- |
| 根目录散落头文件 | 核心类型：`Shape`、`Stride`、`Layout`、`Tensor`（如 `layout.hpp`、`tensor.hpp`、`int_tuple.hpp`） |
| `algorithm/` | 作用于 Tensor 的核心算法：`copy.hpp`、`gemm.hpp`、`fill.hpp`、`clear.hpp` |
| `arch/` | 「裸」的 PTX 指令包装（如 `mma_sm80.hpp`、`copy_sm90_tma.hpp`），最贴近硬件 |
| `atom/` | 基于 `arch/` 构建的元信息：`mma_atom.hpp`（`Mma_Atom`/`TiledMma`）、`copy_atom.hpp`（`Copy_Atom`/`TiledCopy`）、各 `*_traits_sm*.hpp` |
| `container/` | `array`、`tuple`、`bit_field` 等容器与底层类型 |
| `numeric/` | 整数常量、积分比率等编译期数值工具（`integral_constant.hpp`、`int.hpp`） |
| `util/` | 调试与可视化工具：`print.hpp`、`print_tensor.hpp`、`print_latex.hpp`、`print_svg.hpp` |

注意一个容易混的点：**`cute/arch/` 与 `cutlass/arch/` 都封装硬件指令**，但 `cute/arch/` 是给 CuTe Atom 用的「裸 PTX」，`cutlass/arch/` 是给经典 2.x 层次用的「带 traits 的指令」。两套并存是三代 API 共存的结果。

#### 4.3.3 源码精读

README 对 `include/cute/` 子树的官方说明：

- [README.md:326-340](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L326-L340)：`cute/` 子树，说明 `algorithm/`（copy/gemm 等核心操作）、`arch/`（裸 PTX 包装）、`atom/`（基于 arch 的元信息，含 `mma_atom.hpp`、`copy_atom.hpp` 与各 `*sm*.hpp`），并注明根目录散落头文件是「核心库类型 Shape/Stride/Layout/Tensor 及其运算」。

CuTe 库的配置入口 [include/cute/config.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/config.hpp) 揭示了这套库的「跨编译环境」设计：

- [include/cute/config.hpp:31-47](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/config.hpp#L31-L47)：这里用 `#if defined(__CUDACC__)` 等条件，把 `CUTE_HOST_DEVICE`、`CUTE_DEVICE`、`CUTE_HOST` 等宏在不同编译环境（普通 C++、NVCC、NVRTC 运行时编译）下映射成不同的 `inline`/`__forceinline__ __host__ __device__`。这说明 CuTe 的代码从一开始就要**同时能被 host 编译器、nvcc、甚至 Python DSL 的 NVRTC 路径编译**——这是 4.x Python DSL 能复用同一套抽象的基础。

从 4.1.3 节我们已经看到 `cutlass/gemm/gemm.h` 反向 `#include "cute/layout.hpp"`。两个库的关系可以画成：

```
   使用者代码
       |
       v
 include/cutlass/   (经典库：gemm/conv/epilogue 各层实现，cutlass::)
       |
       v  (3.x 起依赖)
  include/cute/     (布局代数：Layout/Tensor/Atom，cute::)
       |
       v
   硬件指令 (PTX / cute/arch/)
```

即：cute 是更底层的「地基」，cutlass 是建在地基上的「楼房」。

#### 4.3.4 代码实践

**实践目标**：在文件层面确认 cute 的「核心类型在根、算法在 algorithm、指令在 arch」的布局。

**操作步骤**（在仓库根目录执行）：

```bash
# 1. 确认核心类型头文件就在 cute 根目录
ls include/cute/layout.hpp include/cute/tensor.hpp include/cute/int_tuple.hpp include/cute/stride.hpp

# 2. 看 algorithm 目录里有哪些「动作」
ls include/cute/algorithm/

# 3. 看 arch 目录如何按 SM 版本组织指令封装
ls include/cute/arch/ | grep -E 'mma_sm|copy_sm'
```

**需要观察的现象**：

1. 第 1 步应能列出 4 个文件（说明核心类型确实在根目录）。
2. 第 2 步应看到 `copy.hpp`、`gemm.hpp`、`fill.hpp`、`clear.hpp` 等算法文件。
3. 第 3 步应看到 `mma_sm70.hpp`、`mma_sm80.hpp`、`mma_sm90.hpp`、`mma_sm100.hpp`、`copy_sm90_tma.hpp` 等——**文件名直接编码了架构版本**（sm70=Volta, sm80=Ampere, sm90=Hopper, sm100=Blackwell）。

**预期结果**：你能凭文件名（如 `mma_sm90_gmma.hpp`）推断出「这是 Hopper 架构 wgmma 指令的封装」，建立「文件名 → 架构 → 指令」的直觉。无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：`include/cute/algorithm/copy.hpp` 和 `include/cute/algorithm/gemm.hpp` 为什么不和具体的硬件指令放在一起？

**答案**：因为 CuTe 把「算法」和「硬件」解耦了。`copy`/`gemm` 是通用的算法模板，它通过传入的 `Tensor`（含 `Layout`）和 `Atom`（封装硬件指令）来决定具体怎么执行。同一个 `cute::gemm`，配上 SM80 的 atom 就跑 Ampere，配上 SM90 的 atom 就跑 Hopper——算法代码本身不用改。

**练习 2**：`cute/arch/mma_sm90.hpp` 与 `cute/atom/mma_traits_sm90.hpp` 各自放什么？

**答案**：`arch/` 放「裸」的 PTX 指令包装（直接对应一条硬件指令的汇编内联）；`atom/` 放「元信息（traits）」，把 arch 里的裸指令包装成带形状/类型/线程布局信息的结构体，方便上层 `Mma_Atom`/`TiledMma` 使用。简单说：arch 是「能发出指令」，atom 是「知道这条指令的形状和怎么用它」。

---

### 4.4 tools 与 examples 概览

#### 4.4.1 概念说明

库本体之外，仓库还有两个对学习至关重要的目录：

- **`examples/`（SDK 示例库）**：每个子目录是一个独立可编译的小程序，演示 CUTLASS 模板怎么拼成一个真实计算。它们是你**最好的学习教材**——比文档更具体。子目录用数字前缀排序，从 `00_basic_gemm` 一直到上百号，覆盖 GEMM、卷积、低精度、Hopper/Blackwell、融合等几乎所有场景。
- **`tools/`（工具链）**：三个子目录：
  - `tools/library/`：**实例库（Instance Library）**，用代码生成器批量实例化大量 CUTLASS 模板，供 profiler 调用。
  - `tools/profiler/`：**cutlass_profiler**，一个命令行程序，用来运行并测量库里各种内核的性能。
  - `tools/util/`：**utilities**，管理设备张量、参考实现、随机初始化、I/O 等辅助类。

#### 4.4.2 核心流程

学习时它们各自的用法：

1. **想看「怎么用」** → 去 `examples/`，挑一个编号最小的相关示例（如 GEMM 选 `00_basic_gemm`）。
2. **想看「大批量内核怎么生成」** → 去 `tools/library/` 和它的代码生成器 `python/cutlass_library/`。
3. **想「测性能」** → 编译并运行 `tools/profiler/` 产出的 `cutlass_profiler`。
4. **需要「参考实现 / 张量辅助」** → 去 `tools/util/`（注意：示例和测试常 `#include` 这里的 helper）。

另外两个相关目录：

- **`test/unit/`**：Google Test 单元测试，按顶层命名空间组织（`gemm/`、`conv/`、`epilogue/`、`cute/`、`layout/`、`pipeline/` 等），入口是 `test/unit/test_unit.cpp`，整体编译目标叫 `test_unit`。
- **`python/`**：4.x 的 Python 路径，核心是 `python/CuTeDSL/`（CuTe DSL 源码）和 `python/cutlass_library/`（C++ 实例库的代码生成器）。

#### 4.4.3 源码精读

README 的 Tools 小节给出了官方说明：

- [README.md:348-364](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L348-L364)：`tools/` 子树，说明 `library/`（实例库，含所有支持模板的实例化）、`profiler/`（运行 library 中操作的命令行工具）、`util/`（设备张量管理、GEMM 参考实现、随机初始化、I/O 等辅助类）。

README 的 Test 小节：

- [README.md:366-371](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L366-L371)：说明 `test/unit/` 是用 Google Test 写的单元测试，既演示 Core API 的基本用法，也完整测试 GEMM 计算。

实例库与代码生成器的对应关系也藏在目录里：`tools/library/` 下的实例是「生成出来的」，而生成它们的 Python 脚本在 `python/cutlass_library/`（例如 `python/cutlass_library/gemm_math.py`）。这是一条重要的「产出链」：

```
python/cutlass_library/*.py  (生成脚本)
        |  生成
        v
tools/library/               (实例化的 C++ 内核)
        |  被调用
        v
tools/profiler/              (cutlass_profiler 命令行测量)
```

#### 4.4.4 代码实践

**实践目标**：在文件系统里走通「示例 → 工具 → 测试」三件套，并找到一个能立刻上手的入门示例。

**操作步骤**（在仓库根目录执行）：

```bash
# 1. 数一数有多少个示例，并看编号最小的几个
ls -d examples/*/ | wc -l
ls -d examples/0* examples/1* 2>/dev/null | head

# 2. 确认入门示例 00_basic_gemm 存在，并看它有没有自己的 CMakeLists.txt
ls examples/00_basic_gemm/

# 3. 确认 tools 的三个子目录
ls -d tools/*/

# 4. 确认 test/unit 按命名空间组织，并找到测试入口
ls -d test/unit/*/
ls test/unit/test_unit.cpp
```

**需要观察的现象**：

1. 第 1 步应显示约一百来个示例目录（具体数字随版本变），编号最小的有 `00_basic_gemm`、`01_cutlass_utilities` 等。
2. 第 2 步应看到 `00_basic_gemm/` 里有 `basic_gemm.cu` 和 `CMakeLists.txt`——说明每个示例都是**独立可编译目标**。
3. 第 3 步应输出 `tools/library/ tools/profiler/ tools/util/`。
4. 第 4 步应看到 `test/unit/` 下有 `gemm/`、`conv/`、`cute/` 等子目录，以及入口文件 `test_unit.cpp`。

**预期结果**：你建立起「学任何一个 CUTLASS 主题，都能在 examples 找到对应示例、在 test/unit 找到对应测试」的信心。这一步是纯文件操作，无需 GPU；后续真正编译运行示例是 `u1-l2`（构建）和 `u1-l6`（第一个 GEMM）的内容。

#### 4.4.5 小练习与答案

**练习 1**：`tools/library/` 里的内核实例和 `examples/` 里的示例有什么本质区别？

**答案**：`tools/library/` 是**用 Python 脚本（`python/cutlass_library/`）批量自动生成的**「实例化好的内核」，主要供 `cutlass_profiler` 调用测性能，强调「覆盖面」；`examples/` 是**手写的、面向学习者的可读程序**，强调「演示用法」。前者量大、机器生成，后者量少、人工编写、注释清楚。

**练习 2**：如果你要给 CUTLASS 的 GEMM 写一个最小验证程序，应该参考 `examples/` 还是 `test/unit/`？

**答案**：两者都可以，但起点不同。`examples/00_basic_gemm` 是「最小可运行程序」，适合照着写自己的应用；`test/unit/gemm/` 是「带断言的测试」，适合理解「给定输入，正确输出应该是什么」。学习阶段建议先看 example，验证行为时再看 test。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**目录职责思维导图**任务（本讲主实践任务）。

**任务**：在仓库中分别定位 GEMM、卷积、epilogue、layout 对应的头文件目录，然后画一张「目录职责思维导图」。

**步骤**：

1. **定位**：用 `ls` 或 `find` 找出下面四个职责对应的目录路径（每个填一个）：
   - GEMM 实现 → `include/cutlass/______/`
   - 卷积实现 → `include/cutlass/______/`
   - 尾声后处理 → `include/cutlass/______/`
   - 布局标签 → `include/cutlass/______/`

2. **扩展**：再为每个职责找出一个**对应的示例目录**和**对应的单元测试目录**（提示：示例在 `examples/`，测试在 `test/unit/`，都按相同主题命名）。

3. **画图**：用任何你顺手的工具（纸笔、Markdown 缩进、mermaid）画出一张思维导图，根节点是「CUTLASS 仓库」，二级节点是 `include/cutlass`、`include/cute`、`examples`、`tools`、`test`，三级节点是你定位到的具体子目录，旁边标注一句话职责。

**参考答案（第 1 步）**：

- GEMM → `include/cutlass/gemm/`
- 卷积 → `include/cutlass/conv/`
- epilogue → `include/cutlass/epilogue/`
- layout → `include/cutlass/layout/`

**一个可参考的 Markdown 缩进式思维导图骨架**：

```
CUTLASS 仓库
├── include/                    # 库本体（header-only，唯一要加进 include path 的）
│   ├── cutlass/                # 经典库 cutlass::
│   │   ├── gemm/               # 矩阵乘各层（device/threadblock/warp/thread/collective）
│   │   ├── conv/               # 卷积（隐式 GEMM）各层
│   │   ├── epilogue/           # 尾声后处理（缩放/激活/写回/EVT）
│   │   ├── layout/             # 布局标签（RowMajor/ColumnMajor/...）
│   │   ├── arch/               # 指令级封装（mma_sm80.h 等）
│   │   └── pipeline/           # Hopper 异步流水线同步原语
│   └── cute/                   # CuTe 布局代数 cute::（3.x 地基）
│       ├── (根) layout.hpp / tensor.hpp   # Layout / Tensor 核心类型
│       ├── algorithm/          # copy / gemm / fill 等通用算法
│       ├── arch/               # 裸 PTX 指令包装（mma_sm*.hpp / copy_sm*_tma.hpp）
│       └── atom/               # Mma_Atom / Copy_Atom / TiledMma / TiledCopy
├── examples/                   # SDK 示例（每个子目录是独立可编译程序）
├── tools/
│   ├── library/                # 实例库（由 python/cutlass_library 生成）
│   ├── profiler/               # cutlass_profiler 命令行测量工具
│   └── util/                   # 张量管理/参考实现/随机初始化
├── test/unit/                  # Google Test 单元测试（目标 test_unit）
└── python/CuTeDSL/             # 4.x Python CuTe DSL
```

完成这张图后，你面对 CUTLASS 仓库就不会再迷路了——任何需求都能先落到某个目录，再深入。

## 6. 本讲小结

- 仓库顶层分两块：**`include/`（库本体，header-only，唯一要加进编译 include path）** 和围绕它的 `examples/`、`tools/`、`test/`、`python/`、`docs/`。
- `include/cutlass/`（`cutlass::`）是经典库，子目录**按计算阶段**（`gemm/conv/epilogue/layout/transform`）与**按抽象层次**（`arch/thread/warp/threadblock/device/kernel/collective`）交叉组织，是 GEMM 层次化分解的文件镜像。
- `include/cute/`（`cute::`）是 3.x 引入的 CuTe 布局代数库，核心是 `Layout`/`Tensor`/`Atom` 三大抽象，子目录分 `algorithm/`（通用算法）、`arch/`（裸 PTX）、`atom/`（指令元信息）等。
- **两套库不是割裂的**：3.x 起 `cutlass::gemm` 已经 `#include "cute/layout.hpp"`，即 cutlass 建立在 cute 之上，cute 是更底层的地基。
- `examples/` 是手写的可读教材（约上百个，编号排序），`tools/library/` 是 Python 脚本批量生成的实例库（供 profiler 调用），`tools/profiler/` 是性能测量命令行，`test/unit/` 是 Google Test 测试（目标 `test_unit`）。
- 一个贯穿全讲的规律：**目录名 ≈ 命名空间 ≈ 职责**，且 `cute/arch/` 的文件名直接编码架构版本（`sm70/sm80/sm90/sm100`），凭名字就能定位。

## 7. 下一步学习建议

本讲只讲了「东西放在哪」。接下来建议：

- **`u1-l4`（数值类型与基础容器）**：进入 `include/cutlass/` 根目录散落的头文件，看 `numeric_types.h`、`array.h`，认识 `half_t`、`bfloat16_t`、`float8_e4m3_t` 等类型。
- **`u1-l5`（矩阵布局基础）**：进入 `include/cutlass/layout/`，结合本讲对 layout 目录的认识，深入 `RowMajor`/`ColumnMajor` 与 `TensorRef`。
- **`u1-l6`（第一个 GEMM）**：进入 `examples/00_basic_gemm/`，把「目录认知」变成「能跑的程序」。

如果你急于了解 3.x，可以预先浏览 [CuTe 快速入门文档](https://docs.nvidia.com/cutlass/latest/media/docs/cpp/cute/00_quickstart.html) 与 README 指向的 [Code Organization 文档](https://docs.nvidia.com/cutlass/latest/media/docs/cpp/code_organization.html)，但建议先把 `u1` 的基础打牢，进阶层 `u2` 会正式进入 CuTe 的 Layout/Tensor/Atom 抽象。
