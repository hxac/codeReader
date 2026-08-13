# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 ATB 仓库**顶层目录**各自存放什么内容，并区分「源码目录」「配置目录」「生成目录」。
- 理解 `src/` 下四个子目录（`atb` / `ops` / `kernels` / `torch_atb`）的**分层职责**，以及它们之间的依赖方向。
- 给定任意一个算子名字（如 `concat`、`linear`），能够**快速定位**它的 Operation 代码、Runner 代码、Kernel 代码、Python 绑定分别落在哪个目录。
- 看懂 CMake 是如何用「目录扫描 + 子目录嵌套」把这些散落的源码组织成动态库的。

本讲是「认知层」的第二篇，承接 [u1-l1 项目定位](u1-l1-project-overview.md) 建立的「分层架构 + 三大能力」心智模型，把那张架构图落到具体的文件夹上。后续每一篇算子讲义都会反复用到本讲建立的「目录定位能力」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **ATB 的定位**：它是夹在深度学习框架与 CANN/昇腾 NPU 之间的加速库，提供「融合算子 / 图算子 / 插件」三大能力。如果还不清楚，请先读 [u1-l1 项目定位、加速原理与整体架构](u1-l1-project-overview.md)。
- **Operation 与 Kernel 的粗略关系**：Operation 是「算子在框架层的抽象」（负责形状推导、参数解析、选 runner），Kernel 是「算子在设备上真正干活的代码」。本讲只谈它们各自住在哪个目录，不讲内部实现。
- **基本的 CMake 概念**：知道 `add_library`、`add_subdirectory`、`file(GLOB ...)` 大致是做什么的就够了，本讲会顺带解释。

两个名词解释，免得后面混淆：

- **推理算子（ops_infer）** vs **训练算子（ops_train）**：前者用于大模型推理（前向为主，含 KV Cache、采样等），后者用于训练（含反向、梯度）。本讲用得最多的是推理算子。
- **单算子（kernels）** vs **融合算子（mixkernels）**：单算子目录存放「一个独立功能」的 Kernel；融合算子目录存放「把多个步骤合并成一个 Kernel」的实现（如 FFN、Softmax 融合）。

## 3. 本讲源码地图

本讲「读」的不是某段算法代码，而是**仓库自身的组织方式**。关键参照物有三个：

| 文件 / 目录 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `README.md` | 项目说明，内含一张官方目录树 | 顶层目录的「权威答案」 |
| `CMakeLists.txt`（顶层） | 声明编译选项、头文件搜索路径、要编译哪些子目录 | 证明目录之间的依赖关系 |
| `src/CMakeLists.txt` | 用 `GLOB_RECURSE` 扫描各子目录、产出多个动态库 | 证明 `src` 内部的分层与产物 |

此外会点到（不深入）：`src/ops/ops_infer/`、`src/kernels/kernels/`、`src/torch_atb/`、`torch_atb/` 等目录内的具体算子样例，用来演示「横切定位」。

## 4. 核心概念与源码讲解

### 4.1 顶层目录全景与职责

#### 4.1.1 概念说明

一个加速库仓库的顶层目录通常分成三类：

1. **源码目录**：放你写、你编译的代码（`src/`、`include/`、`torch_atb/`）。
2. **配置 / 资源目录**：放算子规格、编译配置、文档、示例（`ops_configs/`、`ops_customize/`、`docs/`、`example/`、`scripts/`、`ci/`、`tests/`）。
3. **生成目录**：编译时才产生、不进版本库的目录（`3rdparty/`、`build/`、`output/`）。

把目录先归好类，后续定位就不会乱：找代码去源码目录，找约束去配置目录，找构建产物去生成目录。

#### 4.1.2 核心流程

定位一个顶层目录的职责，按下面三步走：

1. **先看 README 的目录树**——这是项目维护者给出的「权威说明」。
2. **再看 `.gitignore`**——区分哪些目录是「提交进仓库的」，哪些是「编译时生成的」。ATB 把 `3rdparty`、`build`、`output` 都加进了 `.gitignore`，所以你 `git clone` 后看不到它们，必须编译后才出现。
3. **最后看顶层 CMake**——确认每个目录在构建中扮演的角色（被安装、被编译、还是被依赖）。

#### 4.1.3 源码精读

README 中直接给出了一张完整的目录树，这是本讲最重要的一份「地图」：

[README.md:L22-L54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L22-L54) —— ATB 仓库目录结构（官方权威版）。

为方便对照，把它归纳成下表（带 ✅ 的是默认进入版本库的目录，⚙️ 是编译生成的）：

| 目录 | 类别 | 职责 |
| --- | --- | --- |
| `include/` ✅ | 源码 | **公共头文件**，对外 API 都在这里（`include/atb/` 下） |
| `src/` ✅ | 源码 | **主体源代码**，本讲 4.2 的主角 |
| `torch_atb/` ✅ | 源码 | 顶层 Python 包（`__init__.py`），Python 侧入口 |
| `ops_configs/` ✅ | 配置 | 算子输入输出数据规格约束文件（如 `atb_ops_info.ini`） |
| `ops_customize/` ✅ | 源码/配置 | 用户自定义算子的独立开发目录，可单独编译 |
| `example/` ✅ | 配置 | 算子调用示例与可直接运行的 Demo |
| `tests/` ✅ | 配置 | 测试代码（framework / unittest / infratest / apitest 等） |
| `docs/` ✅ | 配置 | 文档（加速原理、编译、开发指南等） |
| `scripts/` ✅ | 配置 | 脚本（`build.sh`、`set_env.sh`） |
| `ci/` ✅ | 配置 | 持续集成配置 |
| `3rdparty/` ⚙️ | 生成 | 第三方依赖（如 `mki`、`nlohmannJson`），编译时拉取 |
| `build/` ⚙️ | 生成 | 构建中间产物 |
| `output/` ⚙️ | 生成 | 编译输出（安装产物在 `output/atb/`） |

顶层 CMake 进一步印证了这张地图。例如它声明了头文件搜索路径，把 `include`、`src`、`src/kernels/include` 都纳入其中：

[CMakeLists.txt:L81-L96](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L81-L96) —— 顶层 `include_directories`，说明源码目录的头文件从哪里找。

它还声明了「要编译哪些子目录」：

[CMakeLists.txt:L106-L116](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L106-L116) —— `add_subdirectory(src)`、条件性的 `add_subdirectory(tests)` 与 `add_subdirectory(ops_customize)`，对应上表的「源码 / 测试 / 自定义算子」三块。

> 小贴士：顶层还有一组 `option(...)` 编译开关（`BUILD_PYBIND`、`USE_CXX11_ABI` 等），它们决定「要不要把某块目录编进来」，详见 [CMakeLists.txt:L21-L33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L21-L33)。本讲只关心它们和目录的对应关系，具体编译选项是 [u1-l3 构建系统](u1-l3-build-system.md) 的主题。

#### 4.1.4 代码实践

**实践目标**：亲手核对 README 目录树与磁盘真实情况是否一致，并分辨出生成目录。

**操作步骤**：

1. 在仓库根目录执行 `ls -1F`，把输出与上表对照。
2. 执行 `git ls-files | head`，确认 `3rdparty`、`build`、`output` **不在**版本库里。
3. 执行 `cat .gitignore | grep -E '3rdparty|build|output'`，验证它们是被忽略的生成目录。

**需要观察的现象**：

- `ls` 能看到 `include/`、`src/`、`torch_atb/`、`ops_configs/` 等，但**看不到** `3rdparty/`、`build/`、`output/`。
- `.gitignore` 里能匹配到 `3rdparty`、`build`、`output` 三行。

**预期结果**：你将直观理解「为什么 README 画了 `3rdparty/build/output`，而刚 clone 下来的仓库里却找不到它们」——它们是编译产物。

> 待本地验证：在尚未安装 CANN 的纯阅读环境里，你仍可完成步骤 1、2、3；只是要真正生成 `output/` 需要 [u1-l3](u1-l3-build-system.md) 介绍的完整编译流程。

#### 4.1.5 小练习与答案

**练习 1**：你想找 ATB 对外的 C++ 公共头文件（如 `atb_infer.h`），应该去哪个目录？

> **答案**：`include/atb/`。它是顶层 `include/` 下唯一对外暴露的子目录，`atb_infer.h` 是汇总了各类对外 API 的总入口头文件。

**练习 2**：`ops_configs/` 和 `ops_customize/` 名字相近，它们职责有何不同？

> **答案**：`ops_configs/` 存放**内置算子**的输入输出规格约束（如 `atb_ops_info.ini`），是配置而非代码；`ops_customize/` 是**用户自定义算子**的独立开发空间，里面有完整的 Operation/Kernel 源码和独立 `build.sh`，可在不重编 ATB 的前提下单独编译。

---

### 4.2 src 子目录的分层划分

#### 4.2.1 概念说明

`src/` 是 ATB 的「主战场」，它内部又分成四个职责清晰的子目录。理解它们的关键是抓住一条**自上而下的调用链**：

```
框架层(atb)  →  算子层(ops)  →  Kernel 层(kernels)
                                         ↑
            Python 绑定层(torch_atb) 把以上能力暴露给 Python
```

四个子目录的分工：

| 子目录 | 角色 | 类比 |
| --- | --- | --- |
| `src/atb/` | **框架层**：Operation/Runner/Context 等基础设施 | 「发动机 + 传动系统」 |
| `src/ops/` | **算子层**：每个算子的 Operation、参数、形状推导、选 runner | 「一个个挡位」 |
| `src/kernels/` | **Kernel 层**：设备上真正执行的算子 Kernel（含 Tiling） | 「车轮落地」 |
| `src/torch_atb/` | **Python 绑定层**：用 pybind11 把上述能力包成 Python 模块 | 「方向盘与操控」 |

依赖方向是**单向**的：上层依赖下层（`ops` 依赖 `atb` 框架；`atb` 的 Runner 会调度 `kernels`）；反过来 Kernel 层不回头依赖某个具体 Operation。这种单向依赖是整个项目可维护的根基。

#### 4.2.2 核心流程

理解 `src/` 分层的最小流程：

1. **框架层 `src/atb/`** 提供 Operation、Runner、Context 等抽象基类与执行链路。它本身不知道任何具体算子。
2. **算子层 `src/ops/`** 为每个算子写一个 `XxxOperation`（继承框架基类），实现形状推导、参数解析，并决定用哪种 Runner。按用途再分三个子目录：
   - `ops_infer/`：推理算子（72 个，如 `linear`、`self_attention`、`kv_cache`）。
   - `ops_train/`：训练算子（含反向）。
   - `ops_common/`：推理与训练共用的公共代码。
3. **Kernel 层 `src/kernels/`** 存放真正跑在 NPU 上的 Kernel 代码，按形态再分：
   - `kernels/`：单算子 Kernel。
   - `mixkernels/`：融合算子 Kernel（多步合一，如 `kvcache`、`ffn`）。
   - `lcal/`：通信算子相关的底层实现。
   - `configs/`、`include/`、`tbe_adapter/`：构建配置、Kernel 公共头、TBE 适配器。
4. **Python 绑定层 `src/torch_atb/`** 用 `bindings.cpp` 等文件把 C++ 接口绑定成 `torch_atb` 这个 Python 模块；顶层的 `torch_atb/__init__.py` 则负责在 import 时加载编译好的动态库。

> 关于「Operation → Runner → Kernel」这条链路的内部细节，是 [u3 框架内核与执行链路](u3-l1-operation-base.md) 单元的主题。本讲只需记住它们分别住在 `ops`、（Runner 在 `atb` 与 `ops` 之间）、`kernels` 三个目录。

#### 4.2.3 源码精读

`src/CMakeLists.txt` 用变量记录了这些子目录的位置，是「分层」最直接的证据：

[src/CMakeLists.txt:L11-L14](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L11-L14) —— 定义 `ops_train_directory`、`ops_infer_directory`、`ops_common_directory`、`atb_directory` 四个变量，对应 `ops` 的三个子目录与 `atb` 框架目录。

接着它用 `file(GLOB_RECURSE ...)` 把每个子目录下的所有 `.cpp` 递归收集起来：

[src/CMakeLists.txt:L20-L23](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L20-L23) —— 分别递归扫描 `ops_infer`、`ops_train`、`ops_common`、`atb` 四处的源码。

这正是「往某个算子目录里加一个 `.cpp`，编译时会被自动纳入对应库」的机制来源。最后产出多个库：

[src/CMakeLists.txt:L27-L30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L27-L30) —— 由推理算子 + 框架 + 公共代码组成 `atb`（动态/静态），由训练算子 + 公共代码组成 `atb_train`。

依赖关系也写得很明确：

[src/CMakeLists.txt:L35-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L35-L36) —— `atb` 依赖 `mki asdops atb_mixops ascendcl ... hccl ...`（框架依赖 Kernel 层产物 `asdops`/`atb_mixops` 与通信库 `hccl`）；`atb_train` 又依赖 `atb`，印证了「训练依赖推理、推理依赖框架与 Kernel」的单向分层。

> 注：Kernel 层（`asdops`、`atb_mixops`、`lcal`）由 `src/kernels/` 下的 `add_subdirectory(kernels)` 单独构建，再被上层链接——这就是为什么 `kernels` 目录虽然和 `ops` 平级，却处在依赖链的更底层。

#### 4.2.4 代码实践

**实践目标**：在仓库里统计推理算子的数量，并确认 `src/ops` 的三个子目录都存在。

**操作步骤**：

1. 统计算子数量：`ls -1 src/ops/ops_infer/ | wc -l`。
2. 列出 `src/ops` 的子目录：`ls -1F src/ops/`。
3. 列出 `src/kernels` 的子目录：`ls -1F src/kernels/`。

**需要观察的现象**：

- `ops_infer` 下有约 70+ 个子目录（每个子目录就是一个推理算子）。
- `src/ops/` 下恰好有 `ops_common/`、`ops_infer/`、`ops_train/` 三个子目录。
- `src/kernels/` 下有 `configs/`、`include/`、`kernels/`、`lcal/`、`mixkernels/`、`tbe_adapter/` 等。

**预期结果**：你会得到一张「`src` 内部分层」的实证地图，与 4.2.1 的表格完全对应。

> 待本地验证：算子数量会随版本演进变化（本讲基于的 HEAD 下 `ops_infer` 为 72 个目录，其中还包含一个 `AGENTS.md` 说明文件，实际算子数约为 71 个）。

#### 4.2.5 小练习与答案

**练习 1**：如果你要新增一个推理算子 `foo`，它的 Operation 代码应该放在哪里？训练版本又放在哪里？

> **答案**：推理版放在 `src/ops/ops_infer/foo/`，训练版放在 `src/ops/ops_train/foo/`。只要把 `.cpp` 放进去，`GLOB_RECURSE` 就会自动把它们分别编进 `atb` 和 `atb_train`。

**练习 2**：为什么 `src/kernels/` 和 `src/ops/` 是平级目录，而不是把 Kernel 放到每个算子目录内部？

> **答案**：因为 Kernel 层是「设备相关、构建链独立」的产物（产出 `asdops`/`atb_mixops` 等库），和「设备无关、纯 C++ 框架逻辑」的 Operation 层有不同的编译流程与依赖。把它们平级分开，可以让 Kernel 层被多个上层（推理、训练、自定义算子）复用，也便于独立维护 Tiling、构建配置等 Kernel 专属关注点。

---

### 4.3 一个算子在仓库里的「横切」组织

#### 4.3.1 概念说明

前两节讲的是「目录」,这一节讲「**一个算子是如何被切散在多个目录里的**」。这是初学者最容易迷惑的点：搜 `concat` 你会发现它在好几个地方都出现——这并不是重复，而是同一个算子的**不同关注点**分别落地。

一个完整的算子通常由三类关注点组成：

1. **框架接入**（Operation + Runner）：形状推导、参数解析、选择执行后端——住在 `src/ops/ops_infer/<算子名>/`。
2. **设备执行**（Kernel + Tiling）：真正在 NPU 上计算的代码——住在 `src/kernels/kernels/<算子名>/`（单算子）或 `src/kernels/mixkernels/<算子名>/`（融合算子）。
3. **Python 暴露**（绑定）：让 Python 能调用——住在 `src/torch_atb/`（C++ pybind 侧）+ 顶层 `torch_atb/`（Python 包）。

**注意一个重要事实**：并非每个算子都同时具备「自己的 Kernel」。有些算子（如 `linear`）的 Operation 会直接把计算路由到 CANN 的 `aclnn` 后端，于是你在 `src/kernels/` 下找不到 `linear` 目录，取而代之的是 `ops_infer/linear/` 下的 `linear_aclnn_runner.cpp`。这也是为什么同一个算子目录里常常同时存在 `*_ops_runner.cpp`（走自家 Kernel）和 `*_aclnn_runner.cpp`（走 CANN aclnn）两种 runner。

#### 4.3.2 核心流程

定位「算子 X 的各类代码」的标准动作：

```
1. 框架接入？  →  src/ops/ops_infer/X/   （X_operation.cpp、X_ops_runner.cpp、X_aclnn_runner.cpp）
2. 自家 Kernel？→ src/kernels/kernels/X/  （单算子）或 src/kernels/mixkernels/X/ （融合算子）
                      里面的「四件套」：X_kernel.cpp（计算）+ tiling/（切分）+ X_operation.cpp（Kernel 注册）
3. Python 调用？→ src/torch_atb/bindings.cpp（pybind 绑定） + torch_atb/__init__.py（Python 入口）
4. 规格约束？  →  ops_configs/  （算子输入输出规格 ini）
```

Kernel 目录里有一个反复出现的「**四件套**」约定（详见 [u3-l4 Kernel 层与 MKI 框架](u3-l4-kernel-mki.md)）：

- `X_kernel.cpp`：Kernel 的计算实现（CopyIn/Compute/CopyOut 三段式）。
- `tiling/X_tiling.{h,cpp}`：Tiling 算法，决定数据如何切分到各核。
- `X_operation.cpp`：Kernel 侧的 Operation 定义与注册（与 `ops` 层的 Operation 同名但职责不同，这里负责 MKI 注册）。
- `CMakeLists.txt`：本算子 Kernel 的构建规则。

#### 4.3.3 源码精读

以 **`concat`** 为例，它是一个「全家齐整」的典型算子，能同时在多个目录被找到：

- 框架接入（Operation + 多种 runner）在算子层：

  `src/ops/ops_infer/concat/` —— 内含 `concat_operation.cpp`、`concat_ops_runner.cpp`、`concat_aclnn_runner.cpp`。这里体现了 4.3.1 提到的「自家 runner + aclnn runner 并存」。

- 设备执行（Kernel 四件套）在 Kernel 层的单算子目录：

  `src/kernels/kernels/concat/` —— 内含 `concat_kernel/concat_kernel.cpp`（计算实现）、`tiling/concat_tiling.{h,cpp}`（Tiling）、`concat_operation.cpp`（Kernel 侧注册）。这就是 4.3.2 所说的「四件套」。

  注意这两个 `concat_operation.cpp` **不是同一个文件**：一个在 `ops` 层负责框架接入，一个在 `kernels` 层负责 Kernel 注册。它们靠算子名「concat」被关联起来。

再看一个反例 **`linear`**：它没有自家 Kernel 目录，而是把计算交给 CANN aclnn，因此你在 `src/kernels/` 下找不到 `linear`，只在算子层看到一组 aclnn runner：

`src/ops/ops_infer/linear/` —— 内含 `linear_operation.cpp`、`linear_ops_runner.cpp`、`linear_aclnn_runner.cpp`、`linear_dequant_aclnn_runner.cpp`、`linear_einsum_aclnn_runner.cpp`。一整套 aclnn 变体 runner 说明它走的是「适配 CANN 算子」的路线（详见 [u3-l3 AclnnRunner](u3-l3-aclnn-runner.md)）。

至于 **Python 侧**，绑定代码在：

`src/torch_atb/bindings.cpp` —— pybind11 绑定入口，把 C++ 的 Operation、Param 等类型暴露给 Python。

而 Python 用户实际 `import torch_atb` 触发的是顶层包：

`torch_atb/__init__.py` —— Python 包入口，在 import 时负责加载编译产物 `libatb.so` 等动态库（其内部有 `_load_atb_libs()` 函数）。

最后，**规格约束**统一放在配置目录：

`ops_configs/` —— 如 `atb_ops_info.ini` 约束每个算子的输入输出张量规格（详见 [u6-l4 算子交付件与配置体系](u6-l4-deliverables-config.md)）。

#### 4.3.4 代码实践

**实践目标**：把本讲的「目录定位」练成肌肉记忆——为一个真实算子建立完整的「地址簿」。

**操作步骤**：

1. 任选一个算子，推荐 `concat`（全家齐整）或 `kv_cache`（融合算子样例）。
2. 在仓库里依次定位它的四类位置，填入下表：

   | 关注点 | 目录路径 | 关键文件 |
   | --- | --- | --- |
   | 框架接入 (Operation/Runner) | `src/ops/ops_infer/<算子>/` | `*_operation.cpp`、`*_ops_runner.cpp`、`*_aclnn_runner.cpp` |
   | 设备执行 (Kernel) | `src/kernels/kernels/<算子>/` 或 `src/kernels/mixkernels/<算子>/` | `*_kernel.cpp`、`tiling/*_tiling.cpp` |
   | Python 绑定 | `src/torch_atb/` + `torch_atb/` | `bindings.cpp`、`__init__.py` |
   | 规格约束 | `ops_configs/` | `atb_ops_info.ini` 中对应段落 |

3. 执行 `ls src/ops/ops_infer/concat/` 和 `ls -R src/kernels/kernels/concat/`，逐项核对。

**需要观察的现象**：

- `concat` 同时出现在 `ops_infer` 和 `kernels` 两处。
- `kernels/kernels/concat/` 下能看到 `concat_kernel/`、`tiling/`、`concat_operation.cpp` 这「四件套」的成员。
- `kv_cache` 的 Kernel 不在 `kernels/` 而在 `mixkernels/kvcache/`（因为它是融合算子）。

**预期结果**：你将为所选算子产出一张「跨目录地址簿」，今后阅读任何 ATB 算子都能按这张表快速跳转。

> 待本地验证：不同算子的「齐全程度」不同。`concat`、`activation`、`kv_cache` 是齐全的样例；`linear` 则没有自家 Kernel 目录，可作为「反例」对照。

#### 4.3.5 小练习与答案

**练习 1**：搜 `concat` 出现了 `concat_operation.cpp` 两个同名文件，它们是同一个东西吗？

> **答案**：不是。一个在 `src/ops/ops_infer/concat/`（算子层，负责框架接入、形状推导、选 runner），另一个在 `src/kernels/kernels/concat/`（Kernel 层，负责 MKI 注册、把 Kernel 接入执行框架）。两者靠算子名关联，职责不同。

**练习 2**：为什么 `linear` 在 `src/kernels/` 下找不到对应目录，但算子仍能正常工作？

> **答案**：因为 `linear` 的 Operation 通过 `linear_aclnn_runner.cpp` 把计算路由到了 CANN 自带的 aclnn 算子后端，由 CANN 提供 Kernel，ATB 不再单独维护一份。这也解释了为什么算子目录里常同时存在 `*_ops_runner`（自家 Kernel）和 `*_aclnn_runner`（CANN 后端）两套实现。

**练习 3**：用户在 Python 里写 `import torch_atb` 时，背后依次经过哪些目录的代码？

> **答案**：先执行顶层 `torch_atb/__init__.py`（Python 包入口，负责 `_load_atb_libs()` 加载动态库）；这些动态库中的 Python 绑定则由 `src/torch_atb/bindings.cpp`（pybind11）在编译期生成。换句话说，`torch_atb/` 是壳，`src/torch_atb/` 是绑定实现。

---

## 5. 综合实践

**任务**：为仓库画一张「目录→产物→依赖」三列对照表，并用一个算子把整张表串起来。

要求：

1. **目录列**：列出 `src/atb`、`src/ops/ops_infer`、`src/ops/ops_train`、`src/ops/ops_common`、`src/kernels/kernels`、`src/kernels/mixkernels`、`src/kernels/lcal`、`src/torch_atb`、`include/atb`、`ops_configs`、`ops_customize`。
2. **产物列**：对照 [src/CMakeLists.txt:L27-L30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L27-L30) 与 [src/CMakeLists.txt:L35-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L35-L36)，写出每个目录最终贡献给哪个库（`atb`、`atb_train`、`asdops`、`atb_mixops` 等）。
3. **依赖列**：用箭头画出 `atb_train → atb → (asdops, atb_mixops, hccl)` 的依赖方向，并在表下用一句话解释「为什么训练库依赖推理库」。
4. **串联**：选 `concat` 算子，在表旁标注它的 Operation 落在哪一行、Kernel 落在哪一行、Python 绑定落在哪里。

完成后，你就拥有了一份可用于后续所有算子讲义的「全局导航图」。

> 待本地验证：产物库的名字以 `src/CMakeLists.txt` 的实际 `add_library` 与 `target_link_libraries` 为准；本实践不依赖编译，纯阅读即可完成。

## 6. 本讲小结

- 顶层目录分三类：**源码目录**（`include/`、`src/`、`torch_atb/`）、**配置/资源目录**（`ops_configs/`、`ops_customize/`、`example/`、`tests/`、`docs/`、`scripts/`、`ci/`）、**生成目录**（`3rdparty/`、`build/`、`output/`，被 `.gitignore` 忽略）。
- `src/` 内部是**单向分层**：框架层 `atb` → 算子层 `ops`（`ops_infer`/`ops_train`/`ops_common`）→ Kernel 层 `kernels`（`kernels`/`mixkernels`/`lcal` 等），Python 绑定层 `torch_atb` 把它们暴露给上层。
- `src/CMakeLists.txt` 用 `GLOB_RECURSE` 自动收集各子目录源码，产出 `atb`、`atb_train` 等库；`ops` 加文件即自动入编。
- 一个算子是**横切**分布在多个目录的：框架接入在 `src/ops/ops_infer/<算子>/`，设备执行在 `src/kernels/.../<算子>/`（含 Kernel 四件套），Python 绑定在 `src/torch_atb/` + `torch_atb/`，规格约束在 `ops_configs/`。
- 并非每个算子都有自家 Kernel：`linear` 等算子通过 `*_aclnn_runner` 路由到 CANN 后端，`src/kernels` 下没有对应目录；`concat`、`kv_cache` 则是「全家齐整」的样例。

## 7. 下一步学习建议

- 下一讲 [u1-l3 构建系统与编译运行](u1-l3-build-system.md) 会解释本讲反复出现的 `CMakeLists.txt` 选项与 `scripts/build.sh`，把「目录」和「编译产物」连起来。
- 想深入 `src/atb` 框架层的内部结构，可先读 [u1-l5 Context 上下文](u1-l5-context.md)、[u1-l6 Operation 接口](u1-l6-operation-interface.md)，再到 [u3 框架内核与执行链路](u3-l1-operation-base.md) 单元。
- 想验证本讲的「横切」组织，建议读一个真实算子的 Operation，例如 `src/ops/ops_infer/linear/linear_operation.cpp`，对照 [u4-l1 Linear 算子族](u4-l1-linear-family.md)。
- 对 Kernel 四件套与 Tiling 感兴趣的读者，可跳到 [u3-l4 Kernel 层与 MKI 框架](u3-l4-kernel-mki.md) 与 [u6-l2 自定义 Kernel 开发](u6-l2-custom-kernel.md)。
