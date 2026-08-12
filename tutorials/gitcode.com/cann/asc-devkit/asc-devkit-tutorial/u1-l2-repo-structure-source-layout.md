# 仓库目录结构与源码组织

## 1. 本讲目标

在上一篇（u1-l1）里，我们建立了 Ascend C「是什么」「有几层 API」的全局认知。本讲要回答下一个自然的问题：**这些 API 的源码究竟放在仓库的哪里？它们是怎么组织的？**

读完本讲，你应当能够：

- 说清 `asc-devkit` 仓库的顶层目录各自负责什么；
- 理解 `include`（声明）与 `impl`（实现）这两大目录的「镜像关系」——这是阅读本仓源码最重要的导航地图；
- 认识每一层 API 的主入口头文件（`kernel_operator.h`、`asc_simd.h`、`asc_simt.h` 等），并能快速定位到任意一层 API 的源码。

掌握这三点后，你就有了一把进入本仓源码的「钥匙」，后续每一篇讲义引用的源码文件你都能自己找到。

## 2. 前置知识

阅读本讲前，你需要先完成 **u1-l1（项目定位与多层级 API 架构）**，理解以下概念：

- Ascend C 的三类完备编程能力接口：框架编程 API（Tpipe/Tque）、基础 API（C++ Tensor）、语言扩展层 C API（SIMD/SIMT）；
- 两类效率工具：高阶 API、算子模板库。

此外，理解两个通用 C/C++ 工程概念会很有帮助：

- **头文件（.h）与实现（.cpp / _impl.h）分离**：声明放在头文件里给使用者 `#include`，真正的实现代码放在别处，编译时再链接到一起。
- **声明与实现分离的好处**：使用者只看声明就能写代码，不必关心内部细节；维护者可以单独替换实现而不破坏使用者的代码。

本仓的 `include` 与 `impl` 两大目录，正是这套「声明 / 实现分离」思想在大型项目中的落地。本讲会带你逐步看清。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 文件 / 目录 | 作用 |
|-------------|------|
| `README.md` | 项目说明，其中「目录结构说明」一节给出官方目录树 |
| `include/kernel_operator.h` | 基础 API（含框架编程）的主入口头文件 |
| `include/c_api/asc_simd.h` | 语言扩展层 C API（SIMD）的主入口头文件 |
| `include/simt_api/asc_simt.h` | SIMT API 的主入口头文件 |
| `include/aicpu_api/aicpu_api.h` | AI CPU API 的主入口头文件 |
| `impl/CMakeLists.txt` | impl 目录的构建脚本，揭示各 API 子目录如何被编译 |
| `include/`、`impl/` | 声明目录与实现目录，二者构成镜像 |

> 提示：本讲引用的永久链接基于当前 HEAD `4952d23d863568f8976789364b7af331909bb993`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：顶层目录说明、include/impl 镜像结构、关键入口头文件。

### 4.1 顶层目录说明

#### 4.1.1 概念说明

打开 `asc-devkit` 仓库根目录，你会看到一批顶层文件夹和几个关键文件。在动手编译之前，先弄清每个顶层目录的角色，能帮你避免「不知道去哪里找东西」的困惑。

`README.md` 的「目录结构说明」一节就给出了官方的目录树。本仓的核心定位是：**承载 Ascend C 的编程 API 和必要的 cmake 编译脚本**，即「算子开发所需的核心模块」。

#### 4.1.2 核心流程

理解顶层目录，可以顺着「源码从哪里来 → 怎么构建 → 怎么验证 → 怎么用」这条线索走：

1. **源码从哪里来**：API 的声明在 `include/`，实现在 `impl/`，构建脚手架在 `cmake/`；
2. **怎么构建**：根目录的 `build.sh` 驱动 `CMakeLists.txt`，调用 `cmake/` 下的模块，产出可安装的 run 包；
3. **怎么验证**：`tests/` 放单元测试（UT），`examples/` 放可运行的算子样例；
4. **怎么用**：开发者阅读 `docs/` 文档，在 `examples/` 里找范例，参考 `tools/`、`scripts/` 做辅助。

#### 4.1.3 源码精读

先看官方目录树（节选），它把每个顶层目录的职责说得很清楚：

[README.md:91-117](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/README.md#L91-L117) —— 这是 README 中「目录结构说明」的完整目录树，下面把它整理成更易读的表格。

| 顶层目录 | 职责（来自 README） | 本讲定位 |
|----------|---------------------|----------|
| `cmake/` | Ascend C 构建源代码 | 构建脚手架，被根 `CMakeLists.txt` 复用 |
| `docs/` | 项目文档介绍 | 学习资料入口 |
| `examples/` | Ascend C API 样例工程 | 后续动手实践的素材库 |
| `impl/` | Ascend C API 接口**实现**源代码 | 本讲重点之一 |
| `include/` | Ascend C API 接口**声明**源代码 | 本讲重点之一 |
| `scripts/` | 打包相关脚本 | 配合 `build.sh` 产出 run 包 |
| `tests/` | Ascend C API 的 UT 用例 | 看护 API 正确性 |
| `tools/` | Ascend C 工具源代码 | 辅助工具 |

根目录还有几个关键文件：`build.sh`（一键编译入口）、`CMakeLists.txt`（顶层 CMake 工程）、`version.cmake`（版本号）、`CONTRIBUTING.md`（贡献指南）、`CHANGELOG.md`（变更日志）。

这里要特别强调一个**对称关系**：README 里 `include/` 和 `impl/` 下都列出了完全相同的 7 个 API 子目录——`adv_api`、`aicpu_api`、`basic_api`、`c_api`、`simt_api`、`tensor_api`、`utils`。这并非巧合，而是本仓源码组织的一条核心线索，下一个小模块专门讲它。

#### 4.1.4 代码实践

**实践目标**：用目录浏览工具亲眼确认顶层目录与 API 子目录的对称性。

**操作步骤**：

1. 在仓库根目录执行 `ls -d */`，对照上表确认 8 个顶层目录都在；
2. 执行 `ls include/` 和 `ls impl/`，分别查看两个目录下的子目录；
3. 数一数两边是否都有 `adv_api / aicpu_api / basic_api / c_api / simt_api / tensor_api / utils` 这 7 个同名子目录。

**需要观察的现象**：`include/` 比 `impl/` 多了一个文件 `kernel_operator.h`（它是基础 API 的入口头文件），但两边的子目录名完全一致。

**预期结果**：你会看到一张「左声明、右实现」的对称表格，这就是镜像结构的直观证据。本讲后续会把这层直觉落到具体代码上。

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录下，哪个文件是一键编译的入口脚本？哪个文件记录版本号？

> **答案**：一键编译入口是 `build.sh`；版本号记录在 `version.cmake`。

**练习 2**：如果你想知道某一层 API「能做什么」，应该去哪个顶层目录找学习资料？如果想找可运行的范例，又该去哪里？

> **答案**：学习资料去 `docs/`，可运行范例去 `examples/`。

---

### 4.2 include / impl 镜像结构

#### 4.2.1 概念说明

`include` 与 `impl` 的镜像关系是本仓源码组织的「骨架」。理解它之后，你在仓库里找任何 API 都不会迷路。

直觉上可以这样记：

- **`include/` 是「菜单」**：里面是 API 的**声明**（函数 / 类的签名、注释），给算子开发者 `#include` 使用。它告诉你「有哪些接口、怎么调用」。
- **`impl/` 是「后厨」**：里面是 API 的**实现**（真正的代码逻辑），告诉你「这些接口内部是怎么做到的」。

两边按 API 层级一一对应：`include/basic_api` 对 `impl/basic_api`，`include/c_api` 对 `impl/c_api`，以此类推。这就是「镜像」二字的含义。

#### 4.2.2 核心流程

声明与实现是如何在编译时被关联起来的？大致流程如下：

1. 开发者在 `.asc` 源文件里 `#include "kernel_operator.h"` 这类**声明**头文件；
2. 声明头文件里，某些接口的实现其实写在 `impl/` 下对应的 `_impl.h`（内联实现）或 `.cpp`（编译单元）中；
3. 编译 Kernel 时，编译器会顺着 include 路径把 `impl/` 下的实现一并纳入；
4. 对需要编译成库的 API（如高阶 tiling），`impl/CMakeLists.txt` 负责把它们组织成构建目标；
5. 最终安装时，`include/` 与 `impl/` 两棵目录树都会被整体拷贝到 CANN 安装目录下。

注意一个细节：`impl/` 里除了和 `include/` 同名的接口实现文件（如 `kernel_operator_vec_binary_intf_impl.h` 对应声明 `kernel_operator_vec_binary_intf.h`），还多了一层**按芯片架构切分的子目录**（如 `dav_3510`、`npu_arch_2201`）。同一份声明，在不同芯片上有不同实现，靠这层目录来区分。这部分会在专家层讲义（u15-l2 多芯片适配）展开，本讲先建立印象。

#### 4.2.3 源码精读

先看 `impl/` 的构建脚本，它揭示了哪些 API 子目录被正式纳入编译目标：

[impl/CMakeLists.txt:12-16](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/CMakeLists.txt#L12-L16) —— 用 `add_subdirectory` 把 `adv_api`、`basic_api`、`c_api`、`tensor_api`、`utils` 这 5 个子目录纳入编译。这意味着这 5 个 API 子目录各自还有自己的 `CMakeLists.txt`，会被编译成库或头文件集合。

再往下看安装逻辑：

[impl/CMakeLists.txt:18-41](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/CMakeLists.txt#L18-L41) —— 在安装阶段，`include/` 与 `impl/` 两棵目录树被整体安装到 CANN 的 `asc` 目录下。这段代码直观印证了「声明与实现是一套、要一起部署」的设计。

镜像关系最生动的证据，是 C API 的入口头文件**直接伸手到 `impl/` 里取实现**：

[include/c_api/asc_simd.h:23](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/c_api/asc_simd.h#L23) —— 这一行 `#include "impl/c_api/instr_impl/npu_arch_2201/utils_impl/utils_impl.h"` 很关键：一个位于 `include/c_api/` 的声明文件，越过了目录边界，直接引用 `impl/c_api/instr_impl/npu_arch_2201/` 下的实现。它同时示范了两件事——

- 声明（include）依赖实现（impl）；
- 实现按芯片架构（`npu_arch_2201`）分目录存放。

类似地，在基础 API 里也能看到这种成对的命名约定。例如声明文件 `include/basic_api/kernel_operator_vec_binary_intf.h`（双目矢量计算接口声明），其实现就在 `impl/basic_api/kernel_operator_vec_binary_intf_impl.h`。命名规则很规律：声明去掉 `_impl`，实现加上 `_impl`。

最后看 `impl/basic_api/` 下的架构子目录，体会「同一声明、多份实现」：

`impl/basic_api/` 下既有大量 `_impl.h` 实现文件，也有按芯片架构切分的子目录，例如 `dav_3510`、`dav_c100`、`dav_c220`、`dav_l300`、`dav_l311`、`dav_m200`、`dav_m300`、`dav_m310`、`dav_m510`；而 C API 那边的架构目录用的是 `impl/c_api/instr_impl/npu_arch_2201` 与 `npu_arch_3510`。不同 API 层级的架构子目录命名风格略有差异（`dav_xxx` vs `npu_arch_xxxx`），但思想一致：用子目录隔离不同硬件的实现。

#### 4.2.4 代码实践

**实践目标**：亲手验证一对「声明 → 实现」的镜像对应关系，并理解声明如何依赖实现。

**操作步骤**：

1. 打开 [include/c_api/asc_simd.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/c_api/asc_simd.h)，定位到第 23 行，确认它引用了 `impl/c_api/instr_impl/npu_arch_2201/...`；
2. 在仓库根目录执行 `ls impl/c_api/instr_impl/`，确认存在 `npu_arch_2201`、`npu_arch_3510` 两个架构子目录；
3. 执行 `ls impl/basic_api/ | grep -E '^dav_'`，列出基础 API 下所有 `dav_xxx` 架构子目录；
4. 任选一对声明/实现，例如 `include/basic_api/kernel_operator_vec_binary_intf.h` 与 `impl/basic_api/kernel_operator_vec_binary_intf_impl.h`，分别打开看一眼，确认一个是接口声明、一个是实现。

**需要观察的现象**：声明文件里大多是类 / 函数的声明与注释；实现文件里则是真正的逻辑代码。两边文件名仅差一个 `_impl`。

**预期结果**：你能在脑子里画出一条线——「使用者 include 声明 → 声明引用 impl 实现 → impl 按 dav_xxx / npu_arch_xxxx 分架构」。这条线就是阅读本仓源码的导航路径。

#### 4.2.5 小练习与答案

**练习 1**：`include/basic_api/kernel_operator_data_copy_intf.h` 对应的实现文件应该叫什么名字？在哪个目录下？

> **答案**：应叫 `kernel_operator_data_copy_intf_impl.h`，位于 `impl/basic_api/` 下（仓中实际还存在更细分的 `kernel_operator_data_copy_base_impl.h`、`kernel_operator_data_copy_check.h` 等，命名遵循「声明 + `_impl`」的整体约定）。

**练习 2**：为什么 `impl/` 下会出现 `dav_3510`、`npu_arch_2201` 这类子目录，而 `include/` 下通常没有？

> **答案**：因为**声明是统一的、实现是分架构的**。同一套 API 接口对不同芯片暴露相同的声明（写在 `include/`），但底层实现因硬件而异，所以实现按架构分别放在 `impl/` 的子目录里，编译时再按 `--npu-arch` 选择对应的一份。

---

### 4.3 关键入口头文件

#### 4.3.1 概念说明

一层 API 通常包含几十上百个头文件（矢量计算、数据搬运、同步、类型……），逐个 `#include` 既繁琐又容易遗漏。于是每层 API 都提供一个**主入口头文件**：使用者只要 `#include` 它一个，就等于把这层 API 的全部能力都引了进来。

入口头文件本质上是「聚合器」：它本身几乎不写业务逻辑，只负责按正确顺序把这一层的各个子模块头文件汇总到一起。记住每层 API 的入口头，就等于拿到了这层 API 的总开关。

#### 4.3.2 核心流程

入口头文件的工作流程可以概括为三步：

1. **守门检查**：先做前置校验。例如纯 SIMT 编译模式下不允许使用 SIMD/基础 API 的入口，入口头文件会直接 `#error` 报错，避免误用；
2. **聚合子模块**：按依赖顺序 `#include` 本层各功能子目录的头文件（矢量计算、数据搬运、同步、系统变量……）；
3. **按架构开关补充**：某些能力只在特定芯片上可用，入口头文件会结合 `__NPU_ARCH__` 宏条件性地纳入对应头文件（如 `reg_compute` 仅在 3510 等架构下引入）。

这样，使用者一行 `#include` 就能获得「当前芯片上这层 API 的全部可用能力」。

#### 4.3.3 源码精读

**入口 1：基础 API（含框架编程）—— `kernel_operator.h`**

[include/kernel_operator.h:15-17](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/kernel_operator.h#L15-L17) —— 守门检查：如果启用了纯 SIMT 编译（`__NPU_COMPILER_INTERNAL_PURE_SIMT__`），直接报错，提示「`kernel_operator.h` 不能与 `--enable-simt` 同时使用」。这是防止 API 串味的保护措施。

[include/kernel_operator.h:26-29](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/kernel_operator.h#L26-L29) —— 聚合基础 API 的四大支柱：`kernel_tpipe.h`（Tpipe/Tque 框架，管内存与同步）、`kernel_tensor.h`（GlobalTensor/LocalTensor 数据结构）、`kernel_type.h`（数据类型）、`kernel_operator_intf.h`（各类计算 / 搬运 / 同步接口）。一句 `#include "kernel_operator.h"` 就把这四块都拉进来。

> 这正呼应了 u1-l1 的结论：框架编程 API 与基础 API 同属 C++ Tensor 体系，共用同一入口。

**入口 2：语言扩展层 C API（SIMD）—— `asc_simd.h`**

[include/c_api/asc_simd.h:26-35](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/c_api/asc_simd.h#L26-L35) —— 聚合 C API 的全部子模块：`atomic`（原子操作）、`cache_ctrl`（缓存控制）、`cube_compute`/`cube_datamove`（Cube 计算 / 搬运）、`misc`（杂项）、`scalar_compute`（标量计算）、`sync`（同步）、`sys_var`（系统变量，如 `asc_get_block_num`）、`vector_datamove`/`vector_compute`（矢量搬运 / 计算）。这就是纯 C 接口的完整能力清单。

[include/c_api/asc_simd.h:37-43](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/c_api/asc_simd.h#L37-L43) —— 按架构条件补充：当未定义 `__NPU_ARCH__` 或架构为 `3510` 时，额外纳入 `reg_compute`（寄存器计算：`reg_convert`、`reg_load`、`reg_store`、`reg_vector`）。这说明寄存器级接口并非所有芯片都有，需要按架构开启。

**入口 3 & 4：SIMT 与 AI CPU**

- SIMT API 的入口是 [include/simt_api/asc_simt.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/simt_api/asc_simt.h)，同目录下还有 `asc_bf16.h`、`asc_fp16.h`、`asc_fp8.h`、`device_functions.h`、`cooperative_groups.h` 等业界风格的头文件，体现 SIMT「类业界编程模型」的定位。
- AI CPU API 的入口是 [include/aicpu_api/aicpu_api.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/aicpu_api/aicpu_api.h)，其实现位于 `impl/aicpu_api/`（如 `aicpu_dump.cpp`）。

把四个入口汇总成一张速查表：

| API 层级 | 入口头文件 | 位置 | 典型 `#include` 一行 |
|----------|-----------|------|----------------------|
| 基础 API / 框架编程 | `kernel_operator.h` | `include/` | `#include "kernel_operator.h"` |
| 语言扩展层 C API（SIMD） | `asc_simd.h` | `include/c_api/` | `#include "asc_simd.h"` |
| SIMT API | `asc_simt.h` | `include/simt_api/` | `#include "asc_simt.h"` |
| AI CPU API | `aicpu_api.h` | `include/aicpu_api/` | `#include "aicpu_api.h"` |

> 高阶 API（`adv_api`）的入口与 tiling 强相关，会在进阶层讲义（u10、u6-l2）专门讲解，本讲不展开。

#### 4.3.4 代码实践

**实践目标**：定位并打开各层 API 的入口头文件，画出「include 目录 → impl 目录」的对应关系图。

**操作步骤**：

1. 用编辑器或 `Read` 工具打开 `include/kernel_operator.h` 与 `include/c_api/asc_simd.h`，分别数一数它们各 `#include` 了多少个子模块；
2. 对照上一节的速查表，找到 `include/simt_api/asc_simt.h` 与 `include/aicpu_api/aicpu_api.h`；
3. 画一张「两栏图」：左栏列出 7 个 API 子目录（`basic_api`、`c_api`、`simt_api`、`tensor_api`、`adv_api`、`aicpu_api`、`utils`），右栏对应 `impl/` 下同名的子目录，并在每个 API 子目录上标注它的职责（参考 README 目录树）；
4. 在图上额外标出 `impl/` 下的架构子目录（如 `dav_3510`、`npu_arch_2201`），用箭头表示「实现按芯片分流」。

**需要观察的现象**：

- `kernel_operator.h` 只聚合了 4 个核心头文件，体量很小；
- `asc_simd.h` 聚合了十多个 C API 子模块，并带一段条件编译；
- 两栏图里左右子目录名一一对应，右栏比左栏多出「架构子目录」这一层。

**预期结果**：你得到一张完整的「仓库源码导航图」。以后看到任何一篇讲义引用的源码路径，都能立刻判断它属于哪一层 API、是声明还是实现、是否与特定芯片架构相关。

> 说明：本实践为源码阅读型实践，不需要运行命令，重在动手画图与对照。如果你已按 u1-l3 配好环境，也可以试着在一个最小 `.asc` 文件里分别 `#include` 这几个入口头，观察编译器能否找到它们（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：一个算子开发者想用基础 API 的 `Add` 接口和 `DataCopy` 接口，最少需要 `#include` 哪个头文件？

> **答案**：只需 `#include "kernel_operator.h"`。因为 `Add`（矢量计算）、`DataCopy`（数据搬运）等接口都已由该入口聚合进来（经由 `kernel_operator_intf.h` 等子头）。

**练习 2**：为什么 `asc_simd.h` 里 `reg_compute` 相关头文件要包在 `#if (__NPU_ARCH__ == 3510)` 这样的条件里？

> **答案**：因为寄存器级计算接口（`reg_convert` / `reg_load` / `reg_store` / `reg_vector`）并非所有芯片都支持，目前仅在 3510 等架构上开放。用条件编译可以在不支持该能力的芯片上自动隐藏这些接口，避免误用导致编译失败。

**练习 3**：如果在纯 SIMT 编译模式下误用了 `#include "kernel_operator.h"`，会发生什么？

> **答案**：会触发 `kernel_operator.h` 第 16 行的 `#error`，编译器报错提示「`kernel_operator.h` cannot be used with compile flag --enable-simt enabled」，从而阻止 API 串味。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务。

**任务**：为 `asc-devkit` 仓库绘制一份「源码导航卡」。

要求在卡片上包含以下内容：

1. **顶层目录层**：列出 8 个顶层目录及其一句话职责（参考 4.1 的表格）；
2. **镜像层**：画一条横线把 `include/` 与 `impl/` 分为左右两栏，列出 7 个对称的 API 子目录，并各写一句职责（参考 4.2）；
3. **入口层**：在 `include/` 一侧标出 4 个关键入口头文件（`kernel_operator.h`、`asc_simd.h`、`asc_simt.h`、`aicpu_api.h`）的位置，并写出每个入口对应哪一层 API（参考 4.3 速查表）；
4. **架构分流层**：在 `impl/` 一侧标出 `dav_3510`、`npu_arch_2201` 等架构子目录，写明「同一声明、按芯片选实现」。

完成后，试着用这张卡片回答一个问题：**「我想看 SIMD 的矢量计算接口是怎么实现的，应该去哪个目录找？」**

> 参考答案：先从入口 `include/c_api/asc_simd.h`（4.3）找到它聚合了 `c_api/vector_compute/vector_compute.h`；这是声明，对应的实现要到 `impl/c_api/` 下找，并按架构（如 `impl/c_api/instr_impl/npu_arch_2201`）定位具体实现文件。

这个综合实践不要求运行代码，但要求你真正打开仓库、对照源码填写，而不是凭记忆。

## 6. 本讲小结

- `asc-devkit` 顶层目录分工清晰：`include`/`impl` 是源码核心，`cmake`/`build.sh` 负责构建，`docs`/`examples`/`tests`/`tools`/`scripts` 分别支撑文档、样例、测试、工具与打包。
- **`include`（声明）与 `impl`（实现）构成镜像**：两边的 7 个 API 子目录（`adv_api`、`aicpu_api`、`basic_api`、`c_api`、`simt_api`、`tensor_api`、`utils`）一一对应，命名遵循「声明 + `_impl` = 实现」的约定。
- `impl/` 比 `include/` 多一层**按芯片架构切分的子目录**（`dav_xxx`、`npu_arch_xxxx`），用来隔离同一接口的不同硬件实现。
- 每层 API 都有一个**主入口头文件**充当聚合器：基础 API 是 `kernel_operator.h`、SIMD C API 是 `asc_simd.h`、SIMT 是 `asc_simt.h`、AI CPU 是 `aicpu_api.h`。
- 入口头文件会做**守门检查**（禁止 API 串味）和**按架构条件补充**（如 `reg_compute` 仅 3510 等架构引入）。
- 看到 `impl/CMakeLists.txt` 把 `include/` 与 `impl/` 两棵树整体安装，就能理解「声明 + 实现」是一套、一起部署的设计。

## 7. 下一步学习建议

本讲让你拿到了仓库的「导航地图」，接下来建议：

1. **先动手把环境跑通**：进入 **u1-l3（开发环境准备与一键编译构建）**，学习 `build.sh --pkg` 与 CMake 工程结构，亲眼看到 `include/` 与 `impl/` 是如何被编译、安装到 CANN 目录的；
2. **再写第一个算子**：进入 **u2-l1（.asc 源文件与 Host/Device 混合编译模型）**，你会在样例里真正 `#include "kernel_operator.h"`，把本讲认识的入口头文件用起来；
3. **后续按 API 层级深入**：当你学到某一层 API（如基础 API u3-u5、C API u8、SIMT u9）时，随时回到本讲的「源码导航卡」，对照 `include/impl` 镜像结构定位该层的声明与实现源码。

> 建议把本讲的综合实践产物「源码导航卡」保留下来，它会贯穿你阅读整个学习手册的过程。
