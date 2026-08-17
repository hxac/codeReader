# u1-l2 仓库目录结构与源码地图

## 1. 本讲目标

上一讲我们建立了 SiP（AscendSiPBoost）的能力全景：六大模块、Host/Device 分层、`asd` 接口前缀。本讲把镜头拉近到**仓库本身**，学完后你应该能够：

1. 说出仓库每个顶层目录（`core`、`ops`、`include`、`example`、`sip_pta`、`tests`、`configs`、`scripts`、`docs` 等）的职责。
2. 说清 `core` 与 `ops` 两个目录的分工：`core` 是 Host（主机 CPU）侧的算子管理与执行框架，`ops` 是 Device（NPU）侧的算子实现。
3. 根据**一个算子名**（如 `Conj`）推断出它的 API 声明、Host 实现、operation 注册、tiling、kernel 五类文件分别藏在哪个目录，形成一张可复用的「算子文件地图」。
4. 理解 `include/` 目录下公开 API 的组织方式：一个总入口 `asdsip.h` 聚合六个模块头文件。

这一讲是纯「读图」课：不编译、不运行，只用眼睛和 `grep`。但它决定的「定位源码能力」是后面所有讲义的基础。

## 2. 前置知识

- **Host 与 Device**：昇腾平台是「主机 + NPU 协处理器」结构。运行在你 CPU 上的代码叫 Host 侧代码，运行在 NPU 计算核（AI Core）上的代码叫 Device 侧代码（也称核函数、kernel）。SiP 用 C++ 写 Host 侧，用 AscendC 语言写 Device 侧。
- **算子（operator）**：一个完成特定数学运算的功能单元，比如共轭 `Conj`、点积 `Sdot`、矩阵乘 `Cgemm`。
- **tiling（切分）**：NPU 上一次算不完海量数据，需要 Host 侧先把数据切成若干块、决定用几个核，这段「切块计划」计算逻辑就叫 tiling。本讲只需知道它在 Device 目录里即可，u4-l2 会细讲。
- **ACL**：Ascend Computing Language，昇腾计算加速库，提供 `aclTensor`、`aclrtMalloc` 等基础类型与接口（来自外部 CANN 包，不在本仓库内）。
- **注册（registration）**：算子实现好后，需要用一个注册宏把自己「登记」进框架的算子表，框架才能按名字找到它。本讲会看到 `REG_OPERATION` 与 `REG_KERNEL_BASE` 两个宏。

## 3. 本讲源码地图

| 文件/目录 | 作用 | 本讲用它说明什么 |
| --- | --- | --- |
| `README.md` | 项目自述 | 六大模块自述、目录线索 |
| `CMakeLists.txt` | 顶层构建脚本 | 仓库三大编译单元（imported_libs/ops/core） |
| `include/asdsip.h` | 公开 API 总入口 | 聚合六个模块头文件 |
| `include/base_api.h` | Base 模块公开头文件 | 公开接口长什么样 |
| `core/include/base_inner_api.h` | Base 模块内部声明 | 「内部 API」与「公开 API」的区别 |
| `core/base/conj.cpp` | Conj 的 Host 实现 | core 侧的标准写法 |
| `ops/include/ops.h` | 算子表单例声明 | core 与 ops 之间的桥梁 |
| `ops/include/params/conj.h` | Conj 参数结构体 | ops 侧的参数定义规范 |
| `ops/base/conj/conj_operation.cpp` | Conj 的 operation 注册 | ops 侧第一层 |
| `ops/base/conj/conj/tiling/conj_tiling.cpp` | Conj 的 tiling | ops 侧第二层 |
| `ops/base/conj/conj/conj_kernel.cpp` | Conj 的 kernel 启动封装 | ops 侧第三层 |
| `ops/base/conj/conj/op_kernel/conj.cpp` | Conj 的 AscendC 核函数 | ops 侧第四层 |
| `configs/op_list.yaml` | 算子编译清单 | 从清单反查算子目录 |
| `docs/header_files_library_files.md` | 头文件与库文件官方说明 | 前缀-头文件-库对照表 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录速查

#### 4.1.1 概念说明

SiP 仓库的顶层目录不是随意堆放的，**目录划分直接对应架构分层**。先给出全表：

| 目录/文件 | 职责 | 一句话记忆 |
| --- | --- | --- |
| `include/` | 公开 API 头文件，用户 `#include` 的入口 | 「对外承诺」 |
| `core/` | Host 侧实现：算子 API 实现、执行计划、缓存、日志等公共设施 | 管家（CPU 上） |
| `ops/` | Device 侧算子：operation 注册、tiling、AscendC kernel | 干活的（NPU 上） |
| `configs/` | `build_config.json`（芯片架构开关）+ `op_list.yaml`（算子编译清单） | 编译配置 |
| `cmake/` | `host_config.cmake` 与 `kernel_config.cmake` 两套编译配置 | Host/Device 分开编 |
| `example/` | 不依赖测试框架的算子调用 Demo（`example.cpp` + 按模块分类的 `A2/`） | 抄代码的起点 |
| `sip_pta/` | PyTorch 适配层（`csrc` C++ 绑定、`torch_sip` Python 包、`test`） | 给 Python 用户用 |
| `tests/` | 单元测试（`ut/`） | 质量保障 |
| `scripts/` | 构建、安装、测试、发布脚本 | 工具箱 |
| `docs/` | 中英文文档，`zh/` 下有 `API_Reference` 与 `Installation_Operation_Guide` | 说明书 |
| `imported_libs/` | 编译期从外部拉取的依赖（如 ascend-boost-comm） | 外援 |
| `build.sh` / `install_deps.sh` | 一键编译 / 一键装依赖 | 入口 |
| `version.info` | 版本与依赖版本要求 | 版本卡 |

#### 4.1.2 核心流程

顶层 `CMakeLists.txt` 揭示了仓库的编译骨架，只有三个真正的源码子目录被编入：

```text
CMakeLists.txt (顶层)
├── include(cmake/host_config.cmake)   ← Host 侧编译配置
├── add_subdirectory(imported_libs)    ← 外部依赖
├── add_subdirectory(ops)              ← Device 侧（内部再 include kernel_config.cmake）
└── add_subdirectory(core)             ← Host 侧
```

注意 `include/` 目录本身不参与编译产出（它只有头文件，通过 `include_directories` 暴露给编译器）；`example`、`tests`、`sip_pta` 都不在默认编译路径里（`tests` 只在 `TEST_TYPE` 环境变量设置时编入，`example` 的编译入口在 `example/build.sh`）。

#### 4.1.3 源码精读

先看 README 对「框架」职责的自述，注意它明确点出了 Device 侧二进制加载与 Host 侧 tiling 这两个关键词：

- [README.md:L27-L32](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L27-L32)：README 列出六大组成部分，其中「信号处理加速库框架：负责算子的管理，算子在 Device 侧的二进制加载以及 Host 侧的 tiling」——这句就是 `core` + `ops` 目录的官方职责描述。

再看顶层 CMake 如何组织三个编译单元：

- [CMakeLists.txt:L38-L38](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L38-L38)：顶层引入 `cmake/host_config.cmake`，Host 侧统一走这套配置。
- [CMakeLists.txt:L84-L86](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L84-L86)：`add_subdirectory(imported_libs)`、`add_subdirectory(ops)`、`add_subdirectory(core)` 三行，就是仓库源码的全部编译入口。

`ops` 与 `core` 使用**不同的编译配置**，这是 Host/Device 分离编译的直接证据：

- [ops/CMakeLists.txt:L14-L14](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/CMakeLists.txt#L14-L14)：`ops` 内部引入的是 `cmake/kernel_config.cmake`——kernel（Device 二进制）专用配置，与顶层 `core` 用的 `host_config.cmake` 区分开。

`core` 下每个模块目录都是「一个目录编成一个静态库」的风格，以 `core/base` 为例：

- [core/base/CMakeLists.txt:L11-L13](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/base/CMakeLists.txt#L11-L13)：`file(GLOB ...)` 抓取目录下全部 `.cpp`，`add_library(base STATIC ...)` 编成静态库 `base`。也就是说：**在 core/base 里新增一个 `xxx.cpp` 文件，无需改 CMake 就会被编进 base 库**。

#### 4.1.4 代码实践

1. **实践目标**：不借助本讲义，独立验证顶层目录职责表。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，对照 4.1.1 的表格逐项确认。
   - 执行 `grep -n "ConjOperation" configs/op_list.yaml`，找到算子在编译清单中的登记行号。
   - 执行 `ls core core/base ops ops/base`，观察 `core` 与 `ops` 下的模块子目录名。
3. **需要观察的现象**：
   - `op_list.yaml` 中 `ConjOperation` 出现在第 73 行附近，其下缩进挂着 `ConjC64Kernel` 与 `ascend910b: true`。
   - `core` 下有 `base/blas/fft/filter/interpolation/utils`；`ops` 下有 `base/blas/fft/filter/include/utils`——两边前四个模块名是镜像的。
4. **预期结果**：你会得到一张与 4.1.1 一致的目录清单，并且发现「core 与 ops 的模块子目录基本同名」这一规律。
5. 本实践的命令均为只读操作，在任何机器上都可执行，无需 NPU。

#### 4.1.5 小练习与答案

**练习 1**：`example`、`tests`、`sip_pta` 三个目录为什么不在顶层 CMake 的 `add_subdirectory` 列表里？

**答案**：它们不是库本身的组成部分。`tests` 仅在设置环境变量 `TEST_TYPE` 时才被 `add_subdirectory(tests)` 编入（见 [CMakeLists.txt:L44-L82](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L44-L82)）；`example` 有自己独立的 `example/build.sh` 编译入口；`sip_pta` 是独立的 Python 扩展工程，用自己的 `setup.py` 构建。

**练习 2**：如果我想知道当前仓库依赖的 CANN 包版本要求，看哪个文件？

**答案**：根目录的 `version.info`，其中记录了 `required_package_toolkit_version=">=9.0.0"` 等约束；README 的「安装前准备」一节（[README.md:L42-L52](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L42-L52)）则列出 python/gcc/cmake 等基础工具版本。

### 4.2 core 与 ops：Host 侧与 Device 侧的分工

#### 4.2.1 概念说明

这是本讲最重要的一个模块。SiP 把一个算子的完整实现**切成两半**：

- **`core/`（Host 侧）**：跑在 CPU 上。负责接收用户调用（`asdBlasSdot`、`AsdSip::Conj` 等）、检查参数、组装 `OpDesc`（算子名 + 参数）、执行 tiling 前的调度、管理执行计划（Plan）与缓存、申请 workspace、最终把 kernel「下发」到 NPU。用户最终链接的 `libasdsip.so` 主要来自这里。
- **`ops/`（Device 侧）**：描述「NPU 上到底跑什么」。每个算子一个目录，内含三层：operation（把算子注册进框架并选择 kernel）、tiling（数据切块计划）、op_kernel（AscendC 核函数源码）。这些源码会被编译成 NPU 上执行的二进制。

两边靠**字符串名字**连接：Host 侧在 `OpDesc.opName` 里写下 `"ConjOperation"`，Device 侧的注册宏把 `ConjOperation` 这个名字登记进算子表，运行时按名字配对。

一个例外值得先知道：`core` 与 `ops` 的**模块子目录**（base/blas/fft/filter）基本镜像对应，但不绝对——例如插值 Interpolation 的公开接口是 `include/interp_api.h`，Host 实现分布在 `core/interpolation/` 与 `core/blas/interpolation.cpp`，而 Device 侧目录在 `ops/blas/interpolation/` 与 `ops/blas/interpbycoeff/`。所以**目录只是粗地图，算子名字（opName）才是精确坐标**。

#### 4.2.2 核心流程

以 `Conj`（复数共轭）为例，一次调用穿越两界的完整链路是五跳：

```text
第1跳  调用方代码
        │  AsdSip::Conj(inTensor, outTensor, stream, workspace)
        ▼
第2跳  core/base/conj.cpp（Host 实现）
        │  组装 OpDesc：opName = "ConjOperation"，参数 = OpParam::Conj
        │  调用 RunAsdOps(...)                       ← core/utils/ops_base.cpp:152
        ▼
第3跳  框架查表：Ops 单例按 "ConjOperation" 找 Operation
        │  ops/include/ops.h 的 GetOperationByName
        ▼
第4跳  ops/base/conj/conj_operation.cpp（Device 侧入口）
        │  ConjOperation::GetBestKernel → GetKernelByName("ConjC64Kernel")
        │  InferShapeImpl 推导输出张量的 dtype/dims
        ▼
第5跳  ops/base/conj/conj/ 下三层
        │  conj_kernel.cpp（kernel 启动封装，REG_KERNEL_BASE 注册）
        │  tiling/conj_tiling.cpp（Host 上算切块：coreNum/len/tail）
        │  op_kernel/conj.cpp（AscendC 核函数，NPU 上执行）
```

关键认知：**tiling 代码虽然放在 `ops/`（Device 目录），但它跑在 Host CPU 上**——它只是为 NPU 准备参数。真正跑在 NPU 上的只有 `op_kernel/` 里的核函数。

#### 4.2.3 源码精读

**第 2 跳：core 侧的标准写法。** `core/base/conj.cpp` 全文不到 40 行，是 core 侧「直调式算子」的模板：

- [core/base/conj.cpp:L20-L38](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/base/conj.cpp#L20-L38)：`Conj` 函数只做三件事——把 `opName` 设为字符串 `"ConjOperation"`、塞入参数结构体 `OpParam::Conj`、调用 `RunAsdOps` 把活儿交给框架。这就是 core 侧的全部职责：**不做数学，只做调度**。
- [core/utils/ops_base.cpp:L152-L152](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/utils/ops_base.cpp#L152-L152)：`RunAsdOps` 的定义所在行，它是 core 与 ops 之间的总闸门（u3-l1 整讲拆解它）。

**第 3 跳：连接两个目录的桥梁。** `ops/include/ops.h` 声明了算子表单例：

- [ops/include/ops.h:L22-L57](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/include/ops.h#L22-L57)：`Ops` 单例类。注意它的三个公开方法正是三种查询：`GetAllOperations()`（L35，列出全部算子）、`GetOperationByName(opName)`（L42，按算子名查）、`GetKernelInstance(kernelName)`（L49，按 kernel 名查）。core 侧的 `RunAsdOps` 内部就用它完成第 3 跳。

**第 4 跳：ops 侧的注册与选择。**

- [ops/base/conj/conj_operation.cpp:L19-L42](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj_operation.cpp#L19-L42)：`ConjOperation` 继承 `OperationBase`；`GetBestKernel`（L22-L26）返回 `GetKernelByName("ConjC64Kernel")`——同一个 operation 可以按条件返回不同 kernel，这就是「多数据类型/多架构」的选择点；`InferShapeImpl`（L29-L40）把输出张量的 dtype/format/dims 设成与输入一致（共轭不改形状）；最后一行 `REG_OPERATION(ConjOperation)`（L42）完成登记，登记的名字恰好是 core 侧写的 `"ConjOperation"`。
- [ops/include/params/conj.h:L20-L33](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/include/params/conj.h#L20-L33)：参数结构体 `OpParam::Conj`，必须实现 `operator==`（供框架比对参数）与 `ToString()`（供日志打印）。所有算子的参数结构体都住在 `ops/include/params/` 下，一个算子一个头文件。

**第 5 跳：kernel 三层。**

- [ops/base/conj/conj/conj_kernel.cpp:L55-L70](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/conj_kernel.cpp#L55-L70)：`ConjC64Kernel` 在基类 `ConjKernel` 之上只增加一条检查——输入 dtype 必须是 `TENSOR_DTYPE_COMPLEX64`（复数，C64 由此得名）；末尾 `REG_KERNEL_BASE(ConjC64Kernel)` 登记 kernel。基类部分（L23-L52）的 `InitImpl` 会调用 tiling。
- [ops/base/conj/conj/tiling/conj_tiling.cpp:L20-L49](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/tiling/conj_tiling.cpp#L20-L49)：`ConjTiling` 在 Host 上查平台核数（L22 `PlatformInfo::Instance().GetCoreNum(...)`），把总元素数均分成每核 `len`、末核 `tail`，填入 `ConjTilingData`（L39-L42），最后 `SetBlockDim(needCoreNum)` 告诉运行时启动多少个核（L44）。
- [ops/base/conj/conj/op_kernel/conj.cpp:L36-L44](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/op_kernel/conj.cpp#L36-L44)：真正的核函数 `extern "C" __global__ __aicore__ void conj(...)`——先 `InitTilingData` 把 Host 准备的切块参数从全局内存读进核内，再 `op.Init(...)`、`op.Process()` 执行。类的完整实现（`CopyIn/Compute/CopyOut` 三段式）在同目录 [ops/base/conj/conj/op_kernel/conj.h:L25-L59](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/op_kernel/conj.h#L25-L59)。

#### 4.2.4 代码实践

1. **实践目标**：用 `grep` 亲自走一遍「名字连接两界」的链路，验证 4.2.2 的五跳图。
2. **操作步骤**：
   ```sh
   # 第2跳证据：core 侧写了哪个 opName？
   grep -n 'opName' core/base/conj.cpp
   # 第3~4跳证据：这个名字在 ops 侧哪里注册？
   grep -rn 'REG_OPERATION(ConjOperation)' ops/
   # 第4~5跳证据：operation 选了哪个 kernel？
   grep -n 'GetKernelByName' ops/base/conj/conj_operation.cpp
   grep -rn 'REG_KERNEL_BASE(ConjC64Kernel)' ops/
   # 编译清单证据：这个 kernel 给哪种芯片编译？
   grep -n -A 2 'ConjOperation' configs/op_list.yaml
   ```
3. **需要观察的现象**：每条命令都恰好命中一处（conj 是最简单的算子，没有重名干扰）；`op_list.yaml` 中 `ConjC64Kernel` 下挂着 `ascend910b: true`。
4. **预期结果**：五跳中每一跳的文件与行号都能被 grep 复现，与你阅读 4.2.3 的链接一致。
5. 全部为只读命令，无需编译环境，结果可立即验证。

#### 4.2.5 小练习与答案

**练习 1**：tiling 文件放在 `ops/`（Device 目录）里，为什么说它跑在 Host 上？

**答案**：判断代码跑在哪侧要看它「被编译成什么、被谁调用」。`conj_tiling.cpp` 被编进 Host 侧的算子核心运行时库，由 `ConjKernel::InitImpl`（[conj_kernel.cpp:L45-L51](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/conj_kernel.cpp#L45-L51)）在 kernel 下发前调用；它产出的 `ConjTilingData` 只是作为**数据**传给核函数。真正带 `__aicore__` 标记、在 NPU 上执行的是 `op_kernel/conj.cpp` 里的代码。

**练习 2**：`ConjC64Kernel` 中「C64」指什么？如果你想让它支持 complex32 数据类型，需要改动哪一层？

**答案**：C64 指 `TENSOR_DTYPE_COMPLEX64`（64 位复数，实部虚部各 32 位 float），检查逻辑在 [conj_kernel.cpp:L62-L68](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/conj_kernel.cpp#L62-L68)。要支持新 dtype，需要新增一个子 kernel（含新的 `CanSupport` 检查与对应核函数实现）、在 `GetBestKernel` 里按 dtype 分派、并在 `configs/op_list.yaml` 中登记新 kernel——这正是 u12 实战要做的事。

**练习 3**：为什么 `core/base` 下新增 `.cpp` 文件不用改 CMake，而 `ops` 下新增算子目录却要在 `op_list.yaml` 登记？

**答案**：`core/base/CMakeLists.txt` 用 `file(GLOB *.cpp)` 抓取全部源文件（[core/base/CMakeLists.txt:L11-L11](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/base/CMakeLists.txt#L11-L11)）；而 `ops` 下的算子源码要用 AscendC 工具链编译成 NPU 二进制，`op_list.yaml` 的 `Operation → Kernel → 架构` 三级清单决定了哪些 kernel 为哪些芯片编译（如 [configs/op_list.yaml:L73-L75](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/configs/op_list.yaml#L73-L75)），漏登记则该 kernel 不会被编译。

### 4.3 公开头文件：include/ 目录的组织方式

#### 4.3.1 概念说明

`include/` 是 SiP 对外的唯一「橱窗」，目前只有 8 个条目：

```text
include/
├── asdsip.h          ← 总入口：聚合下面全部模块头文件
├── base_api.h        ← Base：swapLast2Axes、asdMul
├── blas_api.h        ← BLAS：asdBlas* 系列
├── blas_common.h     ← BLAS 公共类型
├── fft_api.h         ← FFT：asdFft* 系列
├── filter_api.h      ← Filter：asdConvolve*
├── interp_api.h      ← Interpolation：asdInterp*
└── domain/rs_api.h   ← Domain 领域接口：rs*
```

组织规则有三条：

1. **一个模块一个头文件**，文件名即模块名，接口前缀与模块一一对应（`asdFft*` → `fft_api.h`）。
2. **总入口聚合一切**：用户可以只 `#include "asdsip.h"`，也可以按需包含单个模块头文件以减小编译依赖。
3. **公开 ≠ 全部**：`include/` 只放「承诺给用户」的接口（参数是 `aclTensor*` 等 ACL 类型）；core 内部以 `Mki::Tensor` 为参数的函数声明放在 `core/include/base_inner_api.h` 这类**内部头文件**里，不对外发布。

第 3 条有个绝佳例子：Conj 的 Host 实现存在（`core/base/conj.cpp`），但它的声明在内部头文件里；`include/base_api.h` 只公开了 `swapLast2Axes` 和 `asdMul`。所以「仓库里有某个算子的实现」和「用户能直接调用它」是两回事。

#### 4.3.2 核心流程

用户代码使用 SiP 的包含路径是：

```text
应用程序
  │  #include "asdsip.h"            （或按需 #include "blas_api.h" 等）
  ▼
include/xxx_api.h ──声明──▶ 用户调用 asdXxx(...)
  ▲                                │ 链接
  │                                ▼
  └──依赖── core/utils 头文件    libasdsip.so（core 编译产物 + 调度 ops 侧 kernel）
```

安装后（u1-l4 会实践），这些头文件位于安装目录 `include/` 下，库文件位于 `lib/` 下，对应关系由官方文档 `docs/header_files_library_files.md` 给出。

#### 4.3.3 源码精读

- [include/asdsip.h:L14-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/asdsip.h#L14-L19)：总入口的全部内容就是六个 `#include`：`base_api.h`、`blas_api.h`、`fft_api.h`、`filter_api.h`、`interp_api.h`、`domain/rs_api.h`。没有任何一行代码逻辑——聚合即全部职责。
- [include/base_api.h:L17-L24](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/base_api.h#L17-L24)：Base 模块公开的全部接口只有三个函数声明：`swapLast2AxesGetWorkspaceSize`、`swapLast2Axes`、`asdMul`，参数类型是 `aclTensor*`/`void* stream`。注意这里**没有 Conj**。
- [core/include/base_inner_api.h:L18-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/include/base_inner_api.h#L18-L19)：`Conj` 的真实声明在这里，参数是 `Mki::Tensor`（框架内部张量类型），证实它是内部接口。Conj 目前作为官方算子开发教程的示例存在（见 `docs/developing_a_simple_operator.md`），也是其他模块内部会用到的基础算子。
- [docs/header_files_library_files.md:L9-L16](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L9-L16)：官方「接口名前缀 - 所属模块」对照表：`swapLast2Axes`/`asdMul` → Base，`asdFft*` → FFT，`asdBlas*` → BLAS，`asdConvolve*` → Filter，`asdInterp*` → Interpolation，`rs*` → Domain。拿到一个陌生接口名，看前缀就能定位模块头文件。

#### 4.3.4 代码实践

1. **实践目标**：验证「前缀 → 头文件」的映射，并体会总入口与单模块头文件的关系。
2. **操作步骤**：
   - 执行 `ls include/ include/domain/`，对照 4.3.1 的树确认结构。
   - 执行 `grep -c "asdFft" include/fft_api.h`、`grep -c "asdBlas" include/blas_api.h`，统计各前缀在自己模块头文件中的出现次数。
   - 阅读下面的最小示例（**示例代码**，非项目原有文件），体会按需包含的写法：
     ```c++
     // 示例代码：只使用 Base 模块时，可只包含 base_api.h 而非总入口
     #include "base_api.h"   // 只引入 swapLast2Axes / asdMul 声明

     int main()
     {
         size_t wsSize = 0;
         // 仅查询 workspace 大小，此处不真正执行（编译验证包含路径是否正确）
         AsdSip::AspbStatus ret = AsdSip::swapLast2AxesGetWorkspaceSize(wsSize);
         (void)ret;
         return 0;
     }
     ```
   - 若本机已按 u1-l3/u1-l4 完成编译安装，可尝试编译上述片段并链接 `libasdsip.so` 验证；若尚未安装，则本步骤**待本地验证**（编译需要 CANN 环境与安装好的头文件/库）。
3. **需要观察的现象**：`asdFft` 只在 `fft_api.h` 大量出现，`asdBlas` 只在 `blas_api.h` 出现，前缀与头文件严格对应。
4. **预期结果**：整理出一张与官方文档 L9-L16 一致的「前缀-头文件」对照表；示例代码在头文件路径配置正确时可通过编译。
5. 纯 grep 部分立即可验证；编译部分依赖安装环境，未安装时标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：用户程序同时用到 FFT 和卷积，应该怎么写 include？两种写法分别是什么？

**答案**：写法一（粗粒度）：`#include "asdsip.h"`，一次性引入全部六大模块；写法二（细粒度）：`#include "fft_api.h"` 加 `#include "filter_api.h"`，只引入需要的两个模块。两种都合法，细粒度可减少不必要的编译依赖。

**练习 2**：`AsdSip::Conj` 存在于 `core/base/conj.cpp`，为什么在 `include/base_api.h` 里找不到它？

**答案**：因为 Conj 是内部接口：声明位于内部头文件 [core/include/base_inner_api.h:L18-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/include/base_inner_api.h#L18-L19)，参数使用框架内部类型 `Mki::Tensor` 而非公开的 `aclTensor*`。`include/` 只发布以 ACL 类型为参数的稳定接口。这也提醒我们：阅读源码时「能找到实现」不等于「能直接调用」。

**练习 3**：`libasdsip.so`、`libasdsip_core.so`、`libmki_static.a` 三者是什么关系？

**答案**：依据 [docs/header_files_library_files.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md) 的库文件说明：`libasdsip.so` 是用户应链接的主库，聚合全部模块；`libasdsip_core.so` 是算子核心运行时库（含 Ops 单例、kernel 加载调度，内部静态链接 MKI）；`libmki_static.a` 是 MKI 框架库（提供 Tensor/Kernel/Operation 抽象），由 `libasdsip_core.so` 内部链接，用户通常无需直接关心。顶层 CMake 同时把这两个 MKI 库安装到 `lib` 目录（[CMakeLists.txt:L88-L89](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/CMakeLists.txt#L88-L89)）。

## 5. 综合实践：绘制 Conj 算子文件地图

**任务**：不看本讲义 4.2 节，仅凭「算子名 Conj」从零定位它的全部源码，填出下面这张地图，并用一句话说明每个文件在调用链中的角色。

| 层 | 我找到的文件路径（相对仓库根） | 角色一句话 |
| --- | --- | --- |
| 内部 API 声明 | ？ | 声明 `AsdSip::Conj`（内部 `Mki::Tensor` 版本） |
| Host 实现 | ？ | 组装 `OpDesc`，调用 `RunAsdOps` |
| 参数结构体 | ？ | 定义 `OpParam::Conj`（含 `==` 与 `ToString`） |
| operation 注册 | ？ | `REG_OPERATION` 登记 + `GetBestKernel` 选 kernel |
| tiling | ？ | 按核数切分数据，填 `ConjTilingData` |
| kernel 启动封装 | ？ | `REG_KERNEL_BASE` 登记 `ConjC64Kernel`，触发 tiling |
| AscendC 核函数 | ？ | `__aicore__ void conj(...)`，NPU 上执行 |
| 编译登记 | ？ | `ConjOperation → ConjC64Kernel → ascend910b` |

**操作步骤**：

1. 从编译清单入手：`grep -n "Conj" configs/op_list.yaml` 拿到 `ConjOperation` 与 `ConjC64Kernel` 两个名字。
2. 用两个名字反查注册点：`grep -rn "REG_OPERATION(ConjOperation)" ops/` 与 `grep -rn "REG_KERNEL_BASE" ops/base/conj/`。
3. 从注册文件顶部的 `#include "conj.h"` 与 `#include "tiling/conj_tiling.h"` 顺藤摸瓜找到 tiling 与核函数。
4. 反方向从 core 查：`grep -rn "ConjOperation" core/` 定位 Host 侧入口，再看它 include 的头文件找到声明处。

**参考答案**（本讲义已逐一验证过行号）：

| 层 | 文件 | 验证锚点 |
| --- | --- | --- |
| 内部 API 声明 | `core/include/base_inner_api.h` | [L18-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/include/base_inner_api.h#L18-L19) |
| Host 实现 | `core/base/conj.cpp` | [L20-L38](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/base/conj.cpp#L20-L38) |
| 参数结构体 | `ops/include/params/conj.h` | [L20-L33](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/include/params/conj.h#L20-L33) |
| operation 注册 | `ops/base/conj/conj_operation.cpp` | [L19-L42](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj_operation.cpp#L19-L42) |
| tiling | `ops/base/conj/conj/tiling/conj_tiling.cpp` | [L20-L49](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/tiling/conj_tiling.cpp#L20-L49) |
| kernel 启动封装 | `ops/base/conj/conj/conj_kernel.cpp` | [L55-L70](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/conj_kernel.cpp#L55-L70) |
| AscendC 核函数 | `ops/base/conj/conj/op_kernel/conj.cpp`（类实现在同目录 `conj.h`） | [L36-L44](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/ops/base/conj/conj/op_kernel/conj.cpp#L36-L44) |
| 编译登记 | `configs/op_list.yaml` | [L73-L75](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/configs/op_list.yaml#L73-L75) |

这张地图的方法论可以复用到任何算子：**`op_list.yaml` 拿名字 → grep 注册宏 → 顺 include 摸到 tiling 与核函数 → 反查 core 侧入口**。下一讲搭建编译环境后，你还可以用同样的方法对照 `mul` 算子（它多出 `arch35` 架构分支，目录里多了 `op_kernel/arch35/` 与 `tiling/arch35/` 子目录），检验自己是否真正掌握了这张地图。

## 6. 本讲小结

- 顶层目录即架构：`include`（公开 API）、`core`（Host 侧框架与实现）、`ops`（Device 侧算子）是三大源码主体，顶层 CMake 只编 `imported_libs`/`ops`/`core` 三块。
- `core` 管调度不管数学：以 `core/base/conj.cpp` 为证，Host 实现只组装 `OpDesc`（算子名字符串 + 参数结构体）后调用 `RunAsdOps`。
- `ops` 是算子的家：每个算子一个目录，固定四层——operation 注册、参数结构体（`ops/include/params/`）、tiling、AscendC 核函数；core 与 ops 靠 `opName` 字符串配对。
- tiling 虽在 Device 目录但跑在 Host 上；真正在 NPU 上执行的是 `op_kernel/` 里带 `__aicore__` 的核函数。
- `include/` 是橱窗不是全貌：`asdsip.h` 聚合六个模块头文件，接口前缀（`asdFft*`/`asdBlas*`/`asdConvolve*`/`asdInterp*`/`rs*`）与模块一一对应；内部接口（如 Conj）声明在 `core/include/` 而非公开头文件。
- 定位任何算子源码的五步法：`op_list.yaml` 拿名字 → `REG_OPERATION` 找注册 → include 链摸 tiling/kernel → 反查 core 入口 → 对照 `docs/developing_a_simple_operator.md` 交叉验证。

## 7. 下一步学习建议

下一讲（u1-l3 环境搭建与编译构建）将把这张静态地图「激活」：安装 CANN toolkit/ops 包、执行 `install_deps.sh`、按机器情况修改 `configs/build_config.json` 的芯片架构开关，并跑通 `bash build.sh` 全量编译。届时你会看到 `core` 与 `ops` 分别产出哪些库文件和 kernel 二进制，从产物角度反证本讲的目录分工。

在进入下一讲之前，建议先做两件热身阅读：

1. 通读 [docs/developing_a_simple_operator.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/developing_a_simple_operator.md)，官方教程正是以 Conj 为例逐文件讲解新增算子流程，与本讲 4.2 的五跳链路互相印证。
2. 浏览 `example/A2/` 目录（`BASE/BLAS/Domain/FFT/Filter/Interpolation` 六个子目录），体会「示例目录也按模块分类」的组织一致性，为 u1-l5 跑通第一个 example 做准备。
