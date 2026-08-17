# 项目定位与能力全景：AscendSiPBoost 是什么

## 1. 本讲目标

这是 SiP 学习手册的第一讲。读完本讲，你应该能够：

1. 用一两句话向别人解释 **SiP（AscendSiPBoost，昇腾信号处理加速库）是什么**、它跑在什么硬件上、解决什么问题。
2. 说出 SiP 官方自述的**六大组成部分**（框架、FFT、BLAS、复数基础计算、信号领域融合算子、Solver），并能把每个部分对应到仓库里的真实目录。
3. 区分 **Host 侧框架**（负责算子管理、tiling、执行计划）与 **Device 侧算子**（真正的 NPU kernel）这两层职责。
4. 知道去哪里找**官方 API 文档、issue 入口、编译文档、返回码文档**，遇到问题时能自己导航。
5. 完成一份属于自己的「SiP 模块划分图 + 每模块代表算子清单」学习笔记。

本讲不要求你写代码，重点是**建立全景地图**——后面所有讲义都会挂在这张地图上。

## 2. 前置知识

本讲需要的背景概念很少，下面用通俗语言逐个解释：

- **NPU（神经网络处理器）**：专门为 AI/矩阵运算设计的加速芯片，类比 GPU，但架构不同。华为的 NPU 产品线叫**昇腾（Ascend）**。SiP 的所有算子最终都跑在昇腾 NPU 上。
- **Atlas 系列**：基于昇腾处理器构建的产品线，例如 Atlas A2 训练/推理系列、Atlas A3 系列、Ascend 950PR/950DT。
- **Host 与 Device**：在异构计算里，**Host 指 CPU 侧**（负责流程控制、参数准备、任务下发），**Device 指 NPU 侧**（负责真正的并行计算）。一个算子库的代码因此天然分成两层：Host 侧的"调度层"和 Device 侧的"计算层"。
- **算子（Operator）**：库暴露给用户的最小功能单元，例如"向量点积""FFT 变换"。SiP 里的算子名通常带 `asd` 前缀（如 `asdBlasSdot`）。
- **FFT（Fast Fourier Transform，快速傅里叶变换）**：把信号从时域转换到频域的经典算法，是雷达、通信、音频处理的基石。
- **BLAS（Basic Linear Algebra Subprograms）**：线性代数运算的事实标准接口集，分三级：Level 1 向量-向量运算（如点积）、Level 2 矩阵-向量运算（如矩阵乘向量）、Level 3 矩阵-矩阵运算（如矩阵乘法）。
- **ACL（Ascend Computing Language）**：昇腾的计算接口层，提供设备管理、内存管理（`aclrtMalloc`）、流（`aclrtStream`）、张量描述（`aclTensor`）等运行时能力。SiP 的公开接口建立在 ACL 之上。
- **信号处理（Signal Processing）**：对雷达、通信、声呐等场景中的数字信号做变换与分析，这正是 SiP 中 "SiP = Signal Processing" 的含义。

一个直观的心智模型：**SiP 之于信号处理，就像 cuBLAS/cuFFT 之于 NVIDIA GPU**——把领域里最常用的计算做成深度适配硬件的高性能算子库。

## 3. 本讲源码地图

本讲涉及的文件都以"阅读"为主，它们是理解 SiP 全景的三个入口：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md) | 仓库门面：项目定位、六大能力组成、环境构建、快速上手示例、贡献方式 |
| [docs/zh/Installation_Operation_Guide/overview.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/overview.md) | 官方简介与架构图说明：SiP 在昇腾算子技术栈中的位置、六大模块、支持的硬件型号 |
| [docs/header_files_library_files.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md) | 头文件与库文件说明：接口前缀 → 模块分类表、`asdsip.h` 总入口、库文件依赖关系 |

此外，本讲还会"隔窗观察"以下目录（只看结构，不读内部代码）：

| 目录 | 作用（本讲只建立印象） |
| --- | --- |
| `include/` | 公开头文件：`asdsip.h` 总入口 + `base_api.h`、`blas_api.h`、`fft_api.h`、`filter_api.h`、`interp_api.h`、`domain/rs_api.h` |
| `core/` | Host 侧实现：`base/`、`blas/`、`fft/`、`filter/`、`interpolation/`、`utils/` 等子目录 |
| `ops/` | Device 侧算子：每个算子一个目录，含 operation 注册、tiling、op_kernel |
| `sip_pta/` | PyTorch 适配层（torch_sip），把 SiP 能力暴露给 Python |
| `docs/` | 全部文档：编译、贡献、算子开发指南 + `docs/zh/` 下的 API 参考与安装指南 |

## 4. 核心概念与源码讲解

### 4.1 README 导读：从官方自述认识 SiP

#### 4.1.1 概念说明

README 是项目作者写给读者的"自我介绍"，是了解任何开源项目的第一手材料。SiP 的 README 结构清晰，共 7 个章节：内容总览、学习资源、什么是 SiP、环境构建、快速上手、自定义算子开发、参与贡献。

其中最关键的是第 2 章"什么是 SiP"——它用一段话定义了项目，并用一个列表给出六大能力组成。这段定义是本讲的锚点：

> Ascend Signal Processing Boost（昇腾信号处理加速库，下文简称为 SiP 库）基于华为 Ascend AI 处理器打造，深度适配硬件算力、存储及内存带宽特性，提供 FFT、BLAS、FIR 滤波、插值等高性能 NPU 算子，为信号处理领域提供高效可靠的算力加速。

拆开这句话，能读出四个信息：

1. **目标硬件**：华为 Ascend AI 处理器（昇腾 NPU），不是通用 CPU/GPU。
2. **优化手段**：深度适配硬件的算力、存储、内存带宽特性——即算子是"为这颗芯片量身定做"的，这也是它比通用实现快的根本原因。
3. **能力范围**：FFT、BLAS、FIR 滤波、插值等信号处理高频操作。
4. **服务对象**：信号处理领域的应用。

另外 README 开头标注了项目上线时间：2025 年 10 月首次上线，是一个较新的社区项目。

#### 4.1.2 核心流程

阅读 README 的推荐路线（也是本讲的阅读顺序）：

```text
内容总览（锚点导航）
   │
   ├─ 1. 学习资源 ──→ 记下三个入口：编译文档 / API 文档 / issue 地址
   │
   ├─ 2. 什么是 SiP ──→ 精读：一段定位 + 六大组成（本讲核心）
   │
   ├─ 3. 环境构建 ──→ 了解依赖与 CANN 安装（第 3 讲展开）
   │
   ├─ 4. 快速上手 ──→ 浏览 Sdot 示例，感受"固定调用套路"（第 5 讲动手）
   │
   ├─ 5. 自定义算子开发 ──→ 知道路径即可（u12 实战讲展开）
   │
   └─ 6/7. 参与贡献 / 参考文档 ──→ 了解协作方式
```

对初学者最有价值的预判：README 第 4 章的 C++ 示例虽然本讲不逐行讲，但它展示的 **"创建句柄 → MakePlan → 申请/绑定 workspace → 绑定流 → 执行 → 同步 → 销毁"** 固定套路，是 SiP 所有算子共用的编程模型，第 2 单元（u2-l2）会专门拆解。现在只需留下印象。

#### 4.1.3 源码精读

**① 项目定位原文**——[README.md:L23-L23](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L23-L23)：这一行就是上一节引用的完整定位句，是全仓库最值得背下来的一句话。

**② 首次上线时间**——[README.md:L3-L3](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L3-L3)：标注 2025 年 10 月项目首次上线。

**③ 六大能力组成列表**——[README.md:L25-L32](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L25-L32)：README 把接口功能分成六个部分，逐条对应：

| 行号 | 部分 | 一句话职责 |
| --- | --- | --- |
| L27 | 信号处理加速库框架 | 算子管理、Device 侧二进制加载、Host 侧 tiling；对上层提供单算子/多算子批量调用接口 |
| L28 | FFT 库 | 专用 NPU Kernel + PLAN 框架，支持 C2C、C2R、R2C |
| L29 | BLAS 库 | 依照 BLAS 标准定义，提供 Level 1 到 Level 3 接口 |
| L30 | 复数基础计算库 | 基础的复数类型算子支持 |
| L31 | 信号领域融合算子库 | PC、MTD、CFAR、Interpolation 等融合算子 |
| L32 | Solver 库 | 基于 BLAS 的复杂线性代数函数，如矩阵分解、特征值求解 |

**④ 学习资源三入口**——[README.md:L15-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L15-L19)：

- L17：编译与构建文档 `./docs/compilation_build.md`（仓库内）；
- L18：官方 API 文档（昇腾社区网站，介绍接口与术语）；
- L19：问题报告入口 `https://gitcode.com/cann/sip/issues`（提 issue 的地方）。

**⑤ Sdot 调用套路预览**——[README.md:L222-L243](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L222-L243)：这段示例代码展示了调用 `asdBlasSdot`（向量点积）的完整步骤——L222-L223 创建句柄、L228 `asdBlasMakeDotPlan`、L229-L233 查询并绑定 workspace、L236 绑定流、L239 执行、L240 同步、L243 销毁句柄。注意 L222-L243 中**没有一行真正的"计算代码"**——计算全部发生在 Device 侧的 kernel 里，Host 侧只做流程编排。这正是"框架负责管理、算子负责计算"分层思想的直接证据。

#### 4.1.4 代码实践

**实践名称：给 README 做"三入口 + 一套路"标注**（源码阅读型实践）

1. **实践目标**：把 README 的关键信息点亲自定位一遍，确认自己能快速回到这些位置。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，用编辑器搜索/定位以下四处并打上书签或注释笔记：
     a. L23 的项目定位句；
     b. L25-L32 的六大组成列表；
     c. L17-L19 的三个学习资源链接；
     d. L239 的 `asdBlasSdot(...)` 调用行。
   - 把 L18 的 API 文档链接和 L19 的 issue 链接在浏览器中各打开一次，确认能访问（若内网受限，记录"待本地验证"即可）。
3. **需要观察的现象**：六个部分列表中，哪些部分你在名字上就能猜到用途，哪些完全陌生（通常是 PC、MTD、CFAR 这类雷达术语，先不深究）。
4. **预期结果**：学习笔记中出现一张四行表：定位句行号、六大部分行号范围、资源入口行号、Sdot 调用行号，以及每个链接的可访问性记录。
5. 若 API 文档网站无法访问，属于正常网络限制，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：README 中说 SiP "深度适配硬件算力、存储及内存带宽特性"，这和"随便写一个 C++ FFT 函数"有什么本质区别？

**参考答案**：普通 C++ 实现只面向通用 CPU，无法利用 NPU 的多核向量算力、片上存储和高内存带宽；SiP 的算子针对昇腾硬件特性（核数、存储层级、带宽）做了专门切分与调度设计，因此能发挥硬件的全部性能。这也解释了为什么 SiP 的算子代码里会出现 tiling（数据切分）这种硬件相关概念。

**练习 2**：README 第 4 章的 Sdot 示例里，为什么 Host 侧看不到任何点积求和的循环代码？

**参考答案**：因为实际的点积计算 \( \langle x, y \rangle = \sum_{i=1}^{n} x_i y_i \) 发生在 Device（NPU）侧的 kernel 中。Host 侧代码只负责：创建句柄、制定执行计划（MakePlan）、准备 workspace、绑定流、下发执行指令、同步等待结果。这是 Host/Device 分层的典型体现。

**练习 3**：如果你想给 SiP 报一个 bug，应该去哪个入口？

**参考答案**：README L19 给出的 issue 入口 `https://gitcode.com/cann/sip/issues`，在 gitcode 仓库的 issue 区提交。

### 4.2 六大模块地图：能力清单与代码落点

#### 4.2.1 概念说明

README 说的"六个部分"是**能力视角**的划分；而仓库代码是按**目录视角**组织的。本模块要做的事，就是把两种视角对齐，得到一张"能力 → 目录 → 接口前缀 → 代表算子"的地图。

先明确六个部分各自是什么：

1. **信号处理加速库框架**：整个库的"骨架"。它不直接面向用户提供某个数学功能，而是承担三件公共事务：管理所有算子（注册与查找）、在 Device 侧加载算子二进制、在 Host 侧做 tiling（把大数据切分给多个 NPU 核）。它还向上提供单算子调用、多算子批量调用的统一入口。
2. **FFT 库**：快速傅里叶变换家族，支持 C2C（复数→复数）、C2R（复数→实数）、R2C（实数→复数）三种类型，有自己的 PLAN 框架（执行计划）。
3. **BLAS 库**：按 BLAS 标准实现的线性代数算子，覆盖 Level 1（向量）、Level 2（矩阵-向量）、Level 3（矩阵-矩阵），大量支持复数类型（接口名以 C 开头，如 Cgemm）。
4. **复数基础计算库（Base）**：提供最基础的复数张量操作（逐元素乘、共轭、轴交换等），供用户组合使用。
5. **信号领域融合算子库**：面向具体信号处理场景的组合算子，如 PC（脉冲压缩）、MTD（动目标检测）、CFAR（恒虚警检测）、插值——每个都对应雷达/通信里的完整处理环节。
6. **Solver 库**：基于 BLAS 组合出的更复杂线性代数求解能力，如矩阵分解、特征值求解。

需要注意一个重要事实：**README 的能力清单是"规划全景"，当前版本未必全部交付**。官方 overview 文档明确标注了各部分的交付状态（见 4.2.3 源码精读第 ② 点）：复数基础计算库"本期暂不提供"、Solver"本期不提供"、融合算子库"本期提供部分插值算子"。但对照源码目录会发现文档略滞后于代码——例如 `core/base/` 下已经存在 `conj.cpp`、`mul.cpp`、`swaplast2axes.cpp` 等基础算子实现，BLAS 模块也已提供矩阵求逆（CmatinvBatched）。**学习源码时要以代码为准，文档为辅**，这是读任何快速迭代项目的基本功。

#### 4.2.2 核心流程

用户一次算子调用在六大模块/两层代码中的穿越路径：

```text
用户程序（C++ 或 PyTorch）
    │  调用 asdXxx 接口
    ▼
include/xxx_api.h            ← 公开 API 声明（Base/FFT/BLAS/Filter/Interp/Domain 六类前缀）
    │
    ▼
core/xxx  Host 侧实现        ← 参数组装、执行计划（Plan）、workspace 管理
    │                         （框架部分：算子管理、tiling 发起点）
    ▼
ops/xxx   Device 侧算子      ← operation 注册、tiling 切分、AscendC kernel 二进制
    │                         （框架部分：Device 侧二进制加载）
    ▼
昇腾 NPU 硬件                 ← 真正的并行计算
```

辅助通道：`sip_pta/`（PyTorch 适配层）把 `include/` 的接口包装成 Python 可调用的 torch 扩展，让 AI 模型场景也能用上 SiP。

对照点：README L27 说框架负责"算子在 Device 侧的二进制加载以及 Host 侧的 tiling"——上图 `core/` 与 `ops/` 之间的协作正是这句话的代码形态。

#### 4.2.3 源码精读

**① README 六大组成原文**——[README.md:L25-L32](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L25-L32)：能力清单的第一手定义（4.1.3 已逐条翻译）。

**② overview 的交付状态标注**——[overview.md:L12-L17](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/overview.md#L12-L17)：官方安装指南逐条复述六大模块，并额外标注：L15 复数基础计算库"本期暂不提供"；L16 融合算子库"本期提供部分插值算子"；L17 Solver"本期不提供"。

**③ 目标场景与硬件**——[overview.md:L3-L3](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/overview.md#L3-L3)：一句话点明两类使用场景——"AI 模型场景（支持 PyTorch 调用）、信号处理场景（支持 C++ 直接调用）"，对应仓库的 `sip_pta/` 与 `include/` + `core/` 两套入口。[overview.md:L21-L23](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/Installation_Operation_Guide/overview.md#L21-L23)：列出支持的硬件为 Atlas A2 训练/推理系列、Atlas A3 训练/推理系列、Ascend 950PR/950DT（README L130 提到编译配置默认启用 `ascend910b` 与 `ascend950` 两种芯片架构）。

**④ 接口前缀 → 模块分类表**——[header_files_library_files.md:L9-L16](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L9-L16)：这张表是"看接口名猜模块"的钥匙：

| 接口前缀 | 模块 | 示例 |
| --- | --- | --- |
| `swapLast2Axes` / `asdMul` | Base（基础模块） | 轴交换、逐元素乘 |
| `asdFft*` | FFT | 1D/2D/3D 变换，C2C/C2R/R2C |
| `asdBlas*` | BLAS | 矩阵乘、向量点积、三角求解 |
| `asdConvolve*` | Filter（滤波） | 一维卷积，full/same/valid |
| `asdInterp*` | Interpolation（插值） | 基于系数的插值 |
| `rs*` | Domain（领域） | 雷达场景 Sinc 插值 |

**⑤ asdsip.h 总入口**——[header_files_library_files.md:L24-L24](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L24-L24)：`asdsip.h` 聚合全部公开 API 头文件（内部依次 include `base_api.h`、`blas_api.h`、`fft_api.h`、`filter_api.h`、`interp_api.h`、`domain/rs_api.h`），用户只需一个 include 即可使用全部接口。仓库 `include/` 目录的实际文件与该描述完全一致。

**⑥ 库文件分层**——[header_files_library_files.md:L40-L44](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L40-L44)：编译产物分四层——`libasdsip.so`（主用户库，聚合全部模块）、`libasdsip_static.a`（静态版本）、`libasdsip_core.so`（算子核心运行时：算子注册、Kernel 加载与调度即 Ops 单例、tiling 逻辑——这正是"框架"部分的落地）、`libasdsip_host.so`（主机端辅助）。[header_files_library_files.md:L51-L53](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L51-L53)：第三方依赖——MKI 内核抽象框架库（提供 Tensor/Kernel/Operation 抽象）、`libascendcl.so`（ACL 运行时）、`libaclnn.so`（aclnn 算子库）。

**⑦ 目录实证**（本讲用 `ls` 观察即可）：`core/` 下有 `base`、`blas`、`fft`、`filter`、`interpolation`、`utils` 六个子目录；`ops/` 下有 `base`、`blas`、`fft`、`filter` 等子目录且每个算子独占一个目录（如 `ops/base/conj`、`ops/blas/cgemm`、`ops/blas/dot`）。**`core` 与 `ops` 的同名子目录一一对应**：core 是 Host 实现，ops 是 Device 实现。

#### 4.2.4 代码实践

**实践名称：用 ls 验证"能力 → 目录"映射**（源码阅读型实践，无需硬件）

1. **实践目标**：亲手验证六大模块在仓库目录中的落点，确信地图不是纸上谈兵。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```sh
     ls include/ core/ ops/
     ls core/base/ ops/base/ ops/blas/
     ls sip_pta/csrc/
     ```
   - 把输出整理成一张三列表：`include 头文件 | core 子目录 | ops 子目录`。
   - 额外检查：`ops/blas/` 下数一数有多少个算子目录（如 `cgemm`、`dot`、`cmatinv_batched` 等），感受 BLAS 模块的规模。
3. **需要观察的现象**：`include/` 里的六个 API 头文件名与 `core/`、`ops/` 的子目录名是否对得上；`core/base/` 下是否真的存在文档说"暂不提供"的基础算子实现（如 `conj.cpp`、`mul.cpp`）。
4. **预期结果**：得到一张与 4.2.2 路径图对应的实证表，并发现"文档交付状态标注滞后于代码"这一现象（`core/base/` 与 `include/base_api.h` 均已存在）。
5. 本实践只涉及 `ls`，结果可当场确认，无需「待本地验证」标注。

#### 4.2.5 小练习与答案

**练习 1**：接口 `asdBlasCgemm`、`asdFftExecC2C`、`asdConvolve`、`rsInterpolationBySinc` 分别属于哪个模块？

**参考答案**：按前缀判断——`asdBlas*` → BLAS 模块；`asdFft*` → FFT 模块；`asdConvolve*` → Filter（滤波）模块；`rs*` → Domain（领域）模块。

**练习 2**：`core/blas/cgemm.cpp`（Host 实现）和 `ops/blas/cgemm/`（Device 算子目录）是什么关系？为什么同一个算子要拆成两处？

**参考答案**：前者是 Host 侧实现，负责参数校验、组装执行描述、走框架下发；后者是 Device 侧算子，包含 operation 注册、tiling 切分和真正跑在 NPU 上的 kernel 源码。拆开是因为 Host（CPU，流程控制）与 Device（NPU，并行计算）是两种完全不同的执行环境和编程模型——Host 侧是普通 C++，Device 侧是 AscendC 核函数。

**练习 3**：overview 文档说复数基础计算库"本期暂不提供"，但你在仓库里能找到哪些与之矛盾的证据？

**参考答案**：`include/base_api.h` 公开了基础算子接口；`core/base/` 目录下有 `conj.cpp`、`mul.cpp`、`complex_mul.cpp`、`swaplast2axes.cpp` 等实现；`ops/base/` 下有 `conj`、`mul`、`swaplast2axes` 的 Device 算子目录；`docs/zh/API_Reference/base/` 下还有 asdMul、swapLast2Axes 的 API 文档。结论：文档的"本期"标注滞后于代码发展，学习时应以源码为准。

### 4.3 文档导航：官方文档入口与仓库内文档体系

#### 4.3.1 概念说明

学一个新库，"会找文档"和"会读代码"同样重要。SiP 的文档分散在**三个层面**：

1. **仓库外官方文档**（昇腾社区网站）：最权威的 API 参考、术语解释、CANN 安装指南、环境变量参考——由 README 的链接进入。
2. **仓库内 docs/ 目录**：随代码一起版本化管理的文档，分中文 `docs/zh/` 与若干英文版（`*_en.md`），包括编译指南、算子开发教程、贡献指南、API 参考、返回码说明等。
3. **代码内注释与头文件**：接口行为的最终事实来源。

三层文档的可信度排序是：**头文件/源码 ≥ 仓库内文档 ≥ 网站文档（更新周期长）**。遇到不一致时，优先信代码。

#### 4.3.2 核心流程

"遇到问题 → 查哪份文档"的决策路线：

```text
我想了解某个接口的参数和行为
  ├─ 优先：docs/zh/API_Reference/<模块>/<算子>.md（仓库内，与代码同版本）
  └─ 权威：昇腾社区 SiP API 文档（README L18 链接）

我想编译/安装 SiP
  ├─ docs/compilation_build.md（编译命令详解）
  ├─ docs/zh/Installation_Operation_Guide/installtion_guide.md（安装部署）
  └─ docs/header_files_library_files.md（头文件与库文件对照）

调用返回了非 0 状态码
  └─ docs/zh/context/SiP返回码.md（返回码含义）

我想开发一个新算子
  └─ docs/developing_a_simple_operator.md（官方算子开发教程）

我想提交贡献
  ├─ docs/contributing_guide.md（贡献指南）
  └─ https://gitcode.com/cann/sip/issues（问题反馈）

我想在 PyTorch 里用 SiP
  └─ sip_pta/README.md（torch_sip 构建与使用）
```

#### 4.3.3 源码精读

**① 三个官方入口**——[README.md:L15-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L15-L19)：编译文档（仓库内 `./docs/compilation_build.md`）、API 文档（昇腾社区网站，含接口与术语）、问题报告（gitcode issue）。

**② 仓库内文档根目录结构**（用 `ls docs/` 实证）：

| 文件/目录 | 内容 |
| --- | --- |
| `docs/compilation_build.md` | 编译命令说明（有英文版 `*_en.md`） |
| `docs/developing_a_simple_operator.md` | 从开发一个简单算子出发——算子开发教程 |
| `docs/contributing_guide.md` | 贡献指南 |
| `docs/header_files_library_files.md` | 头文件与库文件说明（本讲第三个源码文件） |
| `docs/zh/API_Reference/` | 按模块组织的 API 参考：`BLAS/`（20+ 篇，如 `Cgemm.md`、`Dot.md`、`CmatinvBatched.md`）、`FFT/`（`FFT_1D/2D/3D.md`、`Istft.md`、`FFT公共接口.md`）、`base/`（`asdMul.md`、`swapLast2Axes.md`）、`Filter/asdConvolve.md`、`Interpolation/asdInterpWithCoeff.md`、`Domain/rsInterpolationBySinc.md`、`Header_file_list.md` |
| `docs/zh/Installation_Operation_Guide/` | 安装与使用指南：`overview.md`（本讲第二个源码文件）、`installtion_guide.md`、`environment_variable.md`、`operator_usage_guide.md`、各模块使用指导（`BLAS.md`、`FFT.md`、`base.md`、`Filter.md`、`Interpolation.md`、`Domain.md`）等 |
| `docs/zh/context/` | 背景资料：`SiP返回码.md`（返回码对照）、`code-of-conduct.md`（行为准则）、`infra-faqs.md` 等 |

**③ 返回码文档的存在**——[docs/zh/context/SiP返回码.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/context/SiP返回码.md)：SiP 接口用统一的 `AspbStatus` 类型返回结果，出错时先查这份文档（第 u2-l3 讲会深入学习错误处理机制）。

**④ PyTorch 适配文档**——[sip_pta/README.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/sip_pta/README.md)：torch_sip 扩展的构建与使用说明，是 AI 模型场景（overview L3 所述"支持 PyTorch 调用"）的入口。

#### 4.3.4 代码实践

**实践名称：文档寻宝三连**（源码阅读型实践，无需硬件）

1. **实践目标**：把 4.3.2 决策路线亲手走一遍，确认每类问题都有明确的文档落点。
2. **操作步骤**：
   - 任务 A：在 `docs/zh/API_Reference/BLAS/` 中找到 `Cgemm.md`，记下它定义的是哪一级 BLAS 操作（提示：矩阵-矩阵乘法属 Level 3）。
   - 任务 B：在 `docs/zh/context/SiP返回码.md` 中任选一个错误码，抄录其含义一行。
   - 任务 C：打开 `docs/developing_a_simple_operator.md`，只看目录/标题结构，记下开发一个算子大致要经过几步（不用读细节）。
3. **需要观察的现象**：API 参考文档的命名是否与接口前缀对应（如 `Domain/rsInterpolationBySinc.md` ↔ `rs*` 前缀）；返回码文档是否按数值或枚举组织。
4. **预期结果**：笔记中新增三行记录，分别对应"查接口""查错误""查开发流程"三类入口的真实命中。
5. 全部为本地文件阅读，可当场完成；社区网站链接若无法访问，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：调用 `asdFftExecC2C` 想核对参数含义，应该按什么顺序查文档？

**参考答案**：先查仓库内 `docs/zh/API_Reference/FFT/` 下的公共接口与 1D/2D/3D 文档（与当前代码同版本），再对照 `include/fft_api.h` 中的声明（最终事实来源），必要时到 README L18 的昇腾社区 API 文档交叉验证。

**练习 2**：仓库内文档和社区网站文档打架时听谁的？为什么？

**参考答案**：听源码和头文件的。文档有更新周期，`docs/zh/` 虽与代码同仓库、相对较新，但仍可能滞后；`include/` 下的头文件声明与 `core/` 的实现才是接口行为的最终依据（本讲 4.2.5 练习 3 的"复数基础计算库"就是文档滞后于代码的实例）。

**练习 3**：`docs/zh/Installation_Operation_Guide/` 和 `docs/zh/API_Reference/` 两个目录的分工是什么？

**参考答案**：前者是"怎么装、怎么配、怎么用"的操作面文档（overview、安装、环境变量、各模块使用指导）；后者是"接口长什么样"的参考面文档（每个算子的参数、约束、行为说明）。

## 5. 综合实践

**综合实践：绘制你的 SiP 模块划分图 + 代表算子清单**

这是本讲的收官任务，产出一份后续所有讲义都会用到的学习笔记。

1. **实践目标**：把本讲三个最小模块（README 导读、六大模块地图、文档导航）的成果浓缩成一张图和一张表。
2. **操作步骤**：
   - 步骤 1：用任何你顺手的工具（Markdown/mermaid、纸笔拍照、draw.io）画一张模块划分图，至少包含六个区块：**框架、FFT、BLAS、Base（复数基础计算）、融合算子（Filter/Interpolation/Domain）、PyTorch 适配（sip_pta）**，并标注每块的 Host 侧目录（`core/xxx`、`include/xxx_api.h`）与 Device 侧目录（`ops/xxx`）。
   - 步骤 2：为每个模块列出 2 个代表性算子名，写入同一份笔记。
   - 步骤 3：在图上用虚线标出"调用穿越路径"：用户程序 → include → core → ops → NPU，并把 PyTorch 适配画成搭在 include 之上的旁路。
   - 步骤 4：在笔记末尾附"文档导航"小节，记录 4.3.2 决策路线中你验证过的入口。
3. **需要观察的现象**：画图时会自然暴露你尚未理解的关系（例如"框架"没有独立目录——它的能力分布在 `core/utils` 与 `libasdsip_core.so` 中），把这些疑点记成"待解惑清单"，它们正是后续讲义要回答的问题。
4. **预期结果（参考答案）**：一张模块图 + 下表（供自查，建议先自己写完再对照）：

| 模块 | 公开接口头文件 | Host 目录 | Device 目录 | 代表算子（2 个） |
| --- | --- | --- | --- | --- |
| 框架 | （无独立算子，提供调用骨架） | `core/utils/` | （能力融入各算子目录与 `libasdsip_core.so`） | 单算子调用、多算子批量调用（能力而非算子） |
| FFT | `include/fft_api.h` | `core/fft/` | `ops/fft/` | `asdFftExecC2C`、`asdFftExecIstft` |
| BLAS | `include/blas_api.h` | `core/blas/` | `ops/blas/` | `asdBlasSdot`、`asdBlasCgemm` |
| Base | `include/base_api.h` | `core/base/` | `ops/base/` | `asdMul`、`swapLast2Axes`（另有 `asdConj`） |
| 融合算子 | `filter_api.h` / `interp_api.h` / `domain/rs_api.h` | `core/filter/`、`core/interpolation/` | `ops/filter/`、`ops/blas/interpolation` | `asdConvolve`、`asdInterpWithCoeff`（领域：`rsInterpolationBySinc`） |
| PyTorch 适配 | — | `sip_pta/`（csrc 绑定 + MixCache） | （复用 ops 的 Device 算子） | `asd_mul`（csrc/base）、BLAS/FFT 绑定算子 |

   - 你的表允许与此表有出入，但每个条目都应该能在仓库里指出对应文件。
5. 本实践无需硬件与编译，全部结论可通过读文件与 `ls` 验证。

## 6. 本讲小结

- **SiP（AscendSiPBoost）是面向昇腾 NPU 的信号处理加速库**，深度适配硬件算力/存储/带宽，提供 FFT、BLAS、滤波、插值等高性能算子，2025 年 10 月首次上线。
- 官方自述六大组成：**框架、FFT 库、BLAS 库、复数基础计算库、信号领域融合算子库、Solver 库**；当前版本以 FFT、BLAS、基础计算与插值类融合算子为主要交付，Solver 尚未成体系提供。
- 代码按 **Host（`include/` + `core/`）与 Device（`ops/`）两层**组织：框架承担算子管理、Device 二进制加载与 Host tiling；真正的计算在 Device 侧 kernel。
- 接口名前缀是模块地图的钥匙：`asdFft*`/`asdBlas*`/`asdConvolve*`/`asdInterp*`/`rs*` 分别对应 FFT/BLAS/Filter/Interpolation/Domain；`asdsip.h` 是总入口头文件。
- 文档分三层：**仓库内 `docs/`（优先）、社区网站 API 文档（权威参考）、源码头文件（最终事实）**；文档可能滞后于代码，以源码为准。
- 两类使用场景两套入口：**C++ 直接调用（`include/` 接口）与 PyTorch 调用（`sip_pta/` 适配层）**。

## 7. 下一步学习建议

下一讲（u1-l2《仓库目录结构与源码地图》）将把本讲"隔窗观察"的目录逐个打开，教你建立**从算子名快速定位五个关键文件**（API 声明、Host 实现、operation 注册、tiling、kernel）的能力。

在进入下一讲之前，建议你：

1. 重读 [README.md:L163-L275](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/README.md#L163-L275) 的 Sdot 完整示例，试着不看讲解说出每一步在干什么——这是第 5 讲跑通 example 的预习。
2. 浏览 [docs/zh/API_Reference/Header_file_list.md](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/zh/API_Reference/Header_file_list.md)，数一数公开接口总量，感受库的能力规模。
3. 带着"待解惑清单"进入下一讲——凡是画模块图时标不出来的目录，都是下一步要消灭的疑点。
