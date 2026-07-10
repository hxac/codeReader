# 目录结构与代码组织

## 1. 本讲目标

上一讲我们建立了对 instant-ngp 的整体认知：它用秒级训练的小型 MLP 表达四种图形基元（NeRF / SDF / 图像 / 体素），核心创新是多分辨率哈希编码，应用层在本仓库、底层神经网络算法在外部依赖 tiny-cuda-nn 中。

本讲要解决的问题是：**这些代码在仓库里到底是怎么摆放的？** 学完后你应当能够：

- 说出 `src/`、`include/`、`configs/`、`dependencies/`、`scripts/`、`data/` 各自存放什么，以及它们如何协作。
- 把 `src/` 下任意一个 `.cu` 文件对应到 `include/neural-graphics-primitives/` 下的头文件。
- 解释 `configs/` 为什么按 `nerf/sdf/image/volume` 四个子目录组织（呼应上一讲的 `ETestbedMode`）。
- 识别 `dependencies/` 里的关键库，区分「git 子模块」和「直接 vendored 的目录」。
- 指出 `testbed.cu` / `testbed.h` 是整个项目的中枢，并说明它为什么那么大。

## 2. 前置知识

- **源文件与头文件**：C++ 项目里，`.cu` 是 CUDA 源文件（既写 C++ 也写 GPU 内核），`.cpp` 是纯 C++ 源文件；`.h` / `.cuh` 是头文件，声明类、函数、枚举，供别的源文件 `#include`。`include/` 目录放头文件，`src/` 目录放实现。
- **CMake**：跨平台的构建系统。`CMakeLists.txt` 是它的配置文件，描述「编译哪些源文件、链接哪些库、产出什么可执行文件」。本项目的 `CMakeLists.txt` 是理解目录协作的「总目录索引」。
- **git 子模块（submodule）**：一个 git 仓库里嵌套引用另一个 git 仓库。`.gitmodules` 文件登记了所有子模块的路径与来源 URL。克隆时必须加 `--recursive`，否则子模块目录是空的，编译会失败。
- **ETestbedMode**：上一讲提到，项目用 `ETestbedMode` 枚举区分四种基元模式 `Nerf / Sdf / Image / Volume`（外加 `None`）。本讲会看到这个枚举如何映射到目录结构。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| :--- | :--- |
| `README.md` | 项目说明，含「依赖致谢」一节列出的第三方库清单 |
| `CMakeLists.txt` | 构建配置，是理解目录协作的总索引：列出全部源文件、头文件目录、依赖库 |
| `.gitmodules` | 登记 10 个 git 子模块的路径与来源 URL |
| `include/neural-graphics-primitives/common.h` | 定义 `ETestbedMode` 枚举，是「四模式」概念的源头 |
| `include/neural-graphics-primitives/testbed.h` | 中枢类 `Testbed` 的声明（1294 行） |
| `src/testbed.cu` | 中枢类 `Testbed` 的核心实现（5672 行，全仓库最大） |

此外会用 `ls` / `wc -l` 浏览 `src/`、`include/`、`configs/`、`data/`、`scripts/`、`dependencies/` 的实际内容。

## 4. 核心概念与源码讲解

### 4.1 目录布局

#### 4.1.1 概念说明

instant-ngp 的仓库根目录大致可以分成几组职责清晰的目录：

| 目录 | 职责 |
| :--- | :--- |
| `src/` | CUDA / C++ 源文件（实现） |
| `include/neural-graphics-primitives/` | 头文件（声明），与 `src/` 一一对应 |
| `configs/` | 网络配置 JSON，按四种模式分子目录 |
| `dependencies/` | 第三方库（子模块 + vendored 目录） |
| `data/` | 四种基元的示例数据 |
| `scripts/` | Python 脚本（数据准备、自动化训练、渲染） |
| `docs/` | 文档（含 NeRF 数据采集建议） |
| `notebooks/` | Colab notebook |
| `cmake/` | CMake 辅助脚本 |
| `.github/workflows/` | CI 配置 |

理解这些目录如何协作的钥匙是 `CMakeLists.txt`：它把 `src/` 里的源文件编译成一个静态库 `ngp`，再链接出 `instant-ngp` 可执行文件和 `pyngp` Python 模块；编译时通过 `NGP_INCLUDE_DIRECTORIES` 把 `include/` 和各个 `dependencies/` 子目录加进头文件搜索路径。

#### 4.1.2 核心流程

一次构建的数据流可以这样描述：

1. CMake 读 `CMakeLists.txt`，把 `src/*.cu` 收进 `NGP_SOURCES` 列表。
2. 把 `include`、`dependencies/<各库>` 加入 `NGP_INCLUDE_DIRECTORIES`，让源文件能 `#include` 到头文件。
3. 编译成静态库 `ngp`，链接 `tiny-cuda-nn` 等依赖。
4. `src/main.cu` 编译成 `instant-ngp` 可执行文件，链接 `ngp`。
5. `src/python_api.cu` 编译成 `pyngp` 共享库，链接 `ngp` + `pybind11`。
6. 运行时，`instant-ngp` 根据 CLI 参数加载 `data/` 下的数据，按模式读取 `configs/<mode>/` 下的 JSON 构建网络。

#### 4.1.3 源码精读

`CMakeLists.txt` 里登记全部源文件的列表，是浏览 `src/` 的最佳目录索引：

[CMakeLists.txt:L266-L282](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L266-L282) — `NGP_SOURCES` 列表，列出了除 `main.cu` / `python_api.cu` 之外所有要编进 `ngp` 静态库的源文件。注意其中四个 `testbed_*.cu`（nerf / sdf / image / volume）按模式拆分，加上 `testbed.cu` 本体，正好对应四种基元。

头文件搜索路径的设置分两段。通用依赖目录在这里：

[CMakeLists.txt:L209-L215](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L209-L215) — 把 `dependencies`、`filesystem`、`nanovdb`、`NaturalSort`、`tinylogger` 加入头文件路径。

而 `include` 本身在这里加入：

[CMakeLists.txt:L262](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L262) — `list(APPEND NGP_INCLUDE_DIRECTORIES "include")`，所以源文件里写 `#include <neural-graphics-primitives/testbed.h>` 就能找到 `include/neural-graphics-primitives/testbed.h`。

`src/` 与 `include/neural-graphics-primitives/` 的对应关系（节选）：

| `src/` 源文件 | 对应头文件 | 职责 |
| :--- | :--- | :--- |
| `testbed.cu` | `testbed.h` | 中枢 `Testbed` 类 |
| `testbed_nerf.cu` | `nerf.h` / `nerf_network.h` / `testbed.h` | NeRF 模式实现 |
| `testbed_sdf.cu` | `sdf.h` / `testbed.h` | SDF 模式实现 |
| `testbed_image.cu` | `testbed.h` | 图像模式实现 |
| `testbed_volume.cu` | `testbed.h` | 体素模式实现 |
| `nerf_loader.cu` | `nerf_loader.h` | NeRF 数据集加载 |
| `triangle_bvh.cu` | `triangle_bvh.cuh` | 网格 BVH 加速结构 |
| `marching_cubes.cu` | `marching_cubes.h` | 等值面提取 |
| `camera_path.cu` | `camera_path.h` | 相机路径关键帧 |
| `render_buffer.cu` | `render_buffer.h` | 渲染缓冲与 CUDA-GL 互操作 |
| `dlss.cu` | `dlss.h` | DLSS 超分 |
| `openxr_hmd.cu` | `openxr_hmd.h` | VR 头显 |
| `common_host.cu` | `common.h` / `common_host.h` | 通用工具与 `ETestbedMode` 定义 |
| `tinyexr_wrapper.cu` | `tinyexr_wrapper.h` | EXR 图像格式封装 |
| `tinyobj_loader_wrapper.cu` | `tinyobj_loader_wrapper.h` | OBJ 网格格式封装 |
| `python_api.cu` | `testbed.h` / `pybind11_vec.hpp` | pyngp 绑定 |
| `main.cu` | `testbed.h` | 程序入口 |

`configs/` 按 `nerf / sdf / image / volume` 四个子目录组织，正好对应 `ETestbedMode` 的四个取值：

[include/neural-graphics-primitives/common.h:L149-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L149-L155) — `ETestbedMode` 枚举定义 `Nerf / Sdf / Image / Volume / None`。`configs/` 的四个子目录与之一一对应，每种模式有自己的 `base.json` 默认网络配置，以及若干编码变体（如 `hashgrid.json` / `frequency.json` / `oneblob.json`）。

#### 4.1.4 代码实践

**实践目标**：用命令亲手浏览目录，验证 `src/` 与 `include/` 的对应关系。

**操作步骤**：

1. 在仓库根目录执行 `ls -d */`，列出所有顶层目录。
2. 执行 `ls configs/`，确认有 `nerf / sdf / image / volume` 四个子目录。
3. 执行 `ls include/neural-graphics-primitives/`，数一下头文件数量。
4. 任选一个 `src/*.cu`（例如 `camera_path.cu`），用 `grep '#include' src/camera_path.cu` 查看它包含了哪些头文件，再到 `include/neural-graphics-primitives/` 下找到对应头文件。

**需要观察的现象**：

- `configs/` 下恰好四个子目录，与 `ETestbedMode` 的四个模式匹配。
- `src/` 里几乎每个 `.cu` 都能在 `include/neural-graphics-primitives/` 找到同名或近名的 `.h` / `.cuh`。

**预期结果**：`include/neural-graphics-primitives/` 下有 33 个头文件；`configs/nerf/` 下文件最多（含 `base.json` 及多种编码 / 层数变体），`configs/volume/` 下只有一个 `base.json`。

#### 4.1.5 小练习与答案

**练习 1**：`configs/nerf/` 下有 `base.json`、`hashgrid.json`、`frequency.json` 等多份配置，而 `configs/volume/` 下只有 `base.json`。为什么 NeRF 需要这么多变体？

**参考答案**：NeRF 是本项目最主要、调参空间最大的基元，作者为它准备了多种输入编码（哈希网格 / 频率 / OneBlob / 稠密网格 / 无编码）和不同网络规模（big / small / 不同隐藏层数）的对照配置，方便做消融实验；体素模式直接渲染 NanoVDB 网格、网络结构固定，所以只需一份默认配置。

**练习 2**：`src/` 下既有 `.cu` 也有 `.cpp`（`thread_pool.cpp`）。为什么 `thread_pool` 用 `.cpp` 而不是 `.cu`？

**参考答案**：线程池是纯 CPU 逻辑，不涉及 CUDA 内核，用 `.cpp` 即可，无需经过 `nvcc` 编译，编译更快、依赖更少。只有需要写 GPU 内核或调用 CUDA API 的文件才用 `.cu`。

---

### 4.2 第三方依赖

#### 4.2.1 概念说明

`dependencies/` 目录装着 21 个第三方库。上一讲强调过：**本仓库是应用层，底层 MLP 与哈希编码算法在 tiny-cuda-nn 里**。所以 `dependencies/tiny-cuda-nn` 是整个项目最关键的依赖——没有它，instant-ngp 的「神经网络」就不存在。

其余依赖按职责可分为几类：

| 类别 | 依赖 | 用途 |
| :--- | :--- | :--- |
| 神经网络核心 | `tiny-cuda-nn` | 快速 CUDA 网络与输入编码（含哈希编码实现） |
| GUI / 窗口 | `imgui`、`glfw`、`gl3w`、`imguizmo` | Dear ImGui 界面、窗口与 OpenGL 上下文、变换 gizmo |
| Python 绑定 | `pybind11`、`pybind11_glm`、`pybind11_json` | C++↔Python 互操作 |
| VR / 超分 | `OpenXR-SDK`、`dlss` | OpenXR 头显、DLSS 超分 |
| 数据格式 | `nanovdb`、`tinyexr`、`tinyobjloader`、`stb_image` | 体素 / EXR / OBJ / PNG-JPEG 读写 |
| 光追 | `optix` | OptiX 头文件（硬件光追，加速网格 SDF 真值计算） |
| 工具 | `args`、`tinylogger`、`NaturalSort`、`filesystem`、`zlib`、`zstr` | 命令行解析、日志、自然排序、文件系统、压缩流 |

这些依赖有两种来源：**git 子模块**（在 `.gitmodules` 里登记，克隆时拉取）和 **直接 vendored 目录**（直接拷进仓库，不是子模块）。

#### 4.2.2 核心流程

- 克隆仓库时若加 `--recursive`，git 会按 `.gitmodules` 把 10 个子模块拉到 `dependencies/` 对应路径下。
- 若忘记 `--recursive`，子模块目录为空，`CMakeLists.txt` 会在配置阶段直接 `FATAL_ERROR` 报错并提示修复命令。
- CMake 用 `add_subdirectory` 把 `tiny-cuda-nn`、`glfw`、`OpenXR-SDK`、`pybind11`、`zlib`、`zstr` 等作为子项目一起编译；其余依赖（如 `imgui`、`nanovdb`、`stb_image`）则是「头文件库」，只需把目录加入 `NGP_INCLUDE_DIRECTORIES` 即可 `#include` 使用。

#### 4.2.3 源码精读

子模块登记表，最关键的是 tiny-cuda-nn：

[.gitmodules:L13-L15](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/.gitmodules#L13-L15) — `tiny-cuda-nn` 子模块，指向 `NVlabs/tiny-cuda-nn`。这是底层神经网络框架的来源。

子模块缺失时的保护性检查：

[CMakeLists.txt:L42-L48](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L42-L48) — 检查 `dependencies/glfw/CMakeLists.txt` 是否存在；若不存在，报致命错误并提示运行 `git submodule update --init --recursive`。这就是「子模块没拉全就编译失败」的根因。

把 tiny-cuda-nn 作为子项目编译：

[CMakeLists.txt:L94-L98](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L94-L98) — `add_subdirectory(dependencies/tiny-cuda-nn)`，并关闭它的 benchmark / examples 以加快编译。注意第 100 行 `set(CMAKE_CUDA_ARCHITECTURES ${TCNN_CUDA_ARCHITECTURES})`：本项目的 GPU 架构选择其实委托给了 tiny-cuda-nn 的 `TCNN_CUDA_ARCHITECTURES` 变量。

README「Thanks」一节也列出了主要开源依赖，可与 `dependencies/` 目录相互印证：

[README.md:L327-L335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L327-L335) — 致谢清单，依次提到 tiny-cuda-nn、tinyexr、tinyobjloader、stb_image、Dear ImGui、Eigen、pybind11 等，并提示「其余见 `dependencies` 文件夹」。

#### 4.2.4 代码实践

**实践目标**：区分 `dependencies/` 下哪些是 git 子模块、哪些是 vendored 目录，并理解可选依赖如何开关。

**操作步骤**：

1. 执行 `cat .gitmodules`，列出全部子模块路径。
2. 执行 `ls dependencies/`，列出全部依赖目录。
3. 对比两份清单：出现在 `.gitmodules` 里的是子模块，只出现在 `ls` 里的是 vendored 目录。
4. 阅读 `CMakeLists.txt` 第 22-27 行的 `NGP_BUILD_WITH_*` 系列开关。

**需要观察的现象**：

- `.gitmodules` 登记了 10 个子模块：`pybind11`、`glfw`、`args`、`tinylogger`、`tiny-cuda-nn`、`imgui`、`dlss`、`zstr`、`zlib`、`OpenXR-SDK`。
- `dependencies/` 下有 21 个目录，多出来的（如 `gl3w`、`imguizmo`、`nanovdb`、`optix`、`stb_image`、`tinyexr`、`tinyobjloader`、`NaturalSort`、`filesystem`、`pybind11_glm`、`pybind11_json`）是 vendored 目录。

**预期结果**：能列出 10 个子模块与 11 个 vendored 目录；并理解 `NGP_BUILD_WITH_GUI / OPTIX / PYTHON_BINDINGS / VULKAN / RTC` 五个开关分别对应 GUI、光追、Python 绑定、DLSS、运行时编译这五项**可选**能力，而 `tiny-cuda-nn` 是**必选**依赖（无开关）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tiny-cuda-nn` 是 git 子模块，而 `stb_image` 是 vendored 目录？

**参考答案**：`tiny-cuda-nn` 是 NVIDIA 维护、会持续更新的活跃项目，用子模块可以随上游版本同步更新、且不把它的源码拷进本仓库历史；`stb_image` 是稳定的单文件头文件库，基本不再变动，直接 vendored 进来更简单，免去子模块管理成本。

**练习 2**：若编译时只要 `pyngp` 不要 GUI，应该怎么做？

**参考答案**：加 `-DNGP_BUILD_WITH_GUI=off`（可选再加 `-DNGP_BUILD_EXECUTABLE=off` 只留 Python 模块）。这样会跳过 `glfw`、`imgui`、`OpenXR-SDK`、`dlss` 等 GUI 相关依赖的编译，但仍会编译 `pyngp`。

---

### 4.3 中枢文件定位

#### 4.3.1 概念说明

整个 instant-ngp 围绕一个巨型类 `Testbed` 组织。它承载了四种模式的全部状态、训练循环、渲染管线、GUI、文件加载、VR 等几乎所有职责，是一个典型的「上帝对象（god object）」。

`Testbed` 的声明在 `include/neural-graphics-primitives/testbed.h`（1294 行），核心实现在 `src/testbed.cu`（5672 行，全仓库最大的源文件）。此外，四种模式各自的训练 / 渲染实现被拆分到 `src/testbed_nerf.cu`、`src/testbed_sdf.cu`、`src/testbed_image.cu`、`src/testbed_volume.cu` 中——它们都是 `Testbed` 类成员函数的实现，只是按模式分文件存放。

因此「中枢」有两层含义：

1. `testbed.cu` + `testbed.h` 是体积最大、职责最集中的文件。
2. 其余 `testbed_*.cu` 都是 `Testbed` 类的「分支实现」，围绕 `testbed.cu` 展开。

#### 4.3.2 核心流程

`Testbed` 类的关键入口（后续讲义会逐个深入，这里只定位）：

- `load_file(path)`：根据文件后缀与内容自动判别它是训练数据、网络配置、快照还是相机路径，并路由到对应加载函数。
- `reset_network(...)`：根据 JSON 配置重建 Loss / Optimizer / Encoding / Network / Trainer 五大对象。
- `frame()`：主帧循环，驱动 GUI、训练、渲染交替进行。
- `train()` / `train_and_render()`：按 `m_testbed_mode` 分发到 `train_nerf` / `train_sdf` / `train_image` / `train_volume`。

这些方法都用 `m_testbed_mode` 这个成员来决定走哪条模式分支。

#### 4.3.3 源码精读

`Testbed` 类的声明起点：

[include/neural-graphics-primitives/testbed.h:L71](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L71) — `class Testbed {` 开始。这个类从第 71 行一直延伸到头文件末尾附近，囊括了四种模式的全部成员与几百个方法。

记录当前模式的成员变量：

[include/neural-graphics-primitives/testbed.h:L635](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L635) — `ETestbedMode m_testbed_mode = ETestbedMode::None;`。`Testbed` 内几乎所有的分发逻辑都读这个变量来决定走 NeRF / SDF / Image / Volume 哪条路径。

`testbed.cu` 里两个代表性方法的定义位置：

[src/testbed.cu:L353](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L353) — `void Testbed::load_file(const fs::path& path)`，文件加载与模式自动识别的入口。

[src/testbed.cu:L4160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4160) — `void Testbed::reset_network(bool clear_density_grid)`，从 JSON 重建网络对象的入口。

为什么 `testbed.cu` 会膨胀到 5672 行？因为它把「所有模式共用的骨架」都放在了一个类里：构造析构、文件加载分发、网络重建、主帧循环、训练分发、渲染分发、快照保存加载、GUI 绘制、相机路径、多 GPU 协调……再叠加四种模式各自的细节，自然体量巨大。这也是后续单元要把 `Testbed` 拆成「主循环 / 文件加载 / 网络配置 / NeRF / SDF / 图像 / 体素」多个角度分别讲的原因。

#### 4.3.4 代码实践

**实践目标**：用 `wc -l` 量化 `src/` 下各文件大小，找出最大的三个，并理解 `testbed.cu` 为何是中枢。

**操作步骤**：

1. 在仓库根目录执行 `wc -l src/*.cu | sort -n`，按行数升序列出所有 CUDA 源文件。
2. 观察最大的三个文件。
3. 执行 `wc -l src/testbed.cu src/testbed_nerf.cu src/testbed_sdf.cu src/testbed_image.cu src/testbed_volume.cu`，看五个 `testbed_*.cu` 的体量对比。
4. 执行 `wc -l include/neural-graphics-primitives/testbed.h`，看头文件规模。

**需要观察的现象**：

- `src/` 下最大的三个 `.cu` 文件依次是 `testbed.cu`、`testbed_nerf.cu`、`testbed_sdf.cu`。
- 五个 `testbed_*.cu` 加起来超过 1.1 万行，其中 `testbed.cu` 一家独大。

**预期结果**（本仓库在当前 HEAD 下的真实行数）：

| 文件 | 行数 |
| :--- | :--- |
| `src/testbed.cu` | 5672 |
| `src/testbed_nerf.cu` | 3764 |
| `src/testbed_sdf.cu` | 1682 |
| `include/neural-graphics-primitives/testbed.h` | 1294 |
| `src/testbed_volume.cu` | 702 |
| `src/testbed_image.cu` | 549 |

`testbed.cu` 成为中枢的原因：它实现了 `Testbed` 类所有模式共用的骨架（帧循环、文件分发、网络重建、快照、GUI、多 GPU），而 NeRF / SDF / 图像 / 体素的模式专属逻辑分别下沉到对应的 `testbed_*.cu`。NeRF 是最复杂的模式，所以 `testbed_nerf.cu` 仅次于 `testbed.cu` 排第二。

#### 4.3.5 小练习与答案

**练习 1**：`Testbed` 类的声明在 `testbed.h`，但它的方法实现分散在 `testbed.cu`、`testbed_nerf.cu`、`testbed_sdf.cu` 等多个文件里。C++ 允许这样做吗？

**参考答案**：允许。C++ 一个类的成员函数可以分布在任意多个 `.cu` / `.cpp` 文件里实现，只要每个实现都写成 `ReturnType Testbed::method_name(...)` 的限定名形式即可。本项目正是按模式把 `Testbed` 的方法拆到不同文件，避免单个文件过大。

**练习 2**：如果你要找「主帧循环」的实现，应该先看哪个文件？

**参考答案**：先看 `src/testbed.cu`。`frame()` 是所有模式共用的骨架，定义在 `testbed.cu` 中；它内部按 `m_testbed_mode` 调用各模式的 `train_*` / `render_*`，那些模式专属实现才在 `testbed_nerf.cu` 等文件里。

---

## 5. 综合实践

**任务**：以 NeRF 模式为例，画出「从示例数据到中枢 Testbed」的文件协作图。

请按以下步骤完成一份文字版协作图（无需运行代码，源码阅读型实践）：

1. **数据层**：查看 `data/nerf/fox/` 目录（`ls data/nerf/fox/`），确认里面是 NeRF 数据集（`transforms.json` + 图片）。
2. **配置层**：打开 `configs/nerf/base.json`，确认它包含 `encoding` / `network` / `optimizer` / `loss` 四大块配置。
3. **加载层**：在 `src/testbed.cu` 的 `load_file`（第 353 行）附近阅读，理解它如何把一个目录路由到 NeRF 模式的数据加载函数 `load_nerf`（实现在 `src/nerf_loader.cu`，声明在 `include/neural-graphics-primitives/nerf_loader.h`）。
4. **网络构建层**：在 `src/testbed.cu` 的 `reset_network`（第 4160 行）附近阅读，理解它如何读取 `configs/nerf/base.json` 并构造网络对象。
5. **中枢层**：在 `include/neural-graphics-primitives/testbed.h` 第 71 行起阅读 `Testbed` 类声明，找到 `m_testbed_mode`（第 635 行）。

**产出**：用一张表格或箭头图，把上面五层涉及的文件串起来，形如：

```
data/nerf/fox/  ──(load_file)──>  src/testbed.cu (Testbed)
                                      │
                                      ├──(load_nerf)──> src/nerf_loader.cu + include/.../nerf_loader.h
                                      └──(reset_network, 读 configs/nerf/base.json)──> 构造网络
```

**预期结果**：你能用一句话说清「fox 数据如何被 `Testbed` 接收、并用 `configs/nerf/base.json` 配置网络」，并指出每一步对应的源文件与头文件。这为第二单元深入 `Testbed` 架构打下地图基础。

## 6. 本讲小结

- 仓库分为 `src/`（实现）、`include/neural-graphics-primitives/`（声明）、`configs/`（按四模式分目录的 JSON）、`dependencies/`（第三方库）、`scripts/`（Python 自动化）、`data/`（示例数据）等职责清晰的目录。
- `src/` 与 `include/neural-graphics-primitives/` 几乎一一对应，`CMakeLists.txt` 的 `NGP_SOURCES` 与 `NGP_INCLUDE_DIRECTORIES` 是浏览这套对应关系的索引。
- `configs/` 按 `nerf / sdf / image / volume` 分子目录，与 `ETestbedMode` 的四个取值一一对应。
- `dependencies/` 含 21 个第三方库，其中 10 个是 git 子模块（最关键的是 `tiny-cuda-nn`），其余为 vendored 目录；子模块缺失时 `CMakeLists.txt` 会在配置阶段报致命错误。
- `tiny-cuda-nn` 是必选依赖（底层神经网络算法所在），GUI / OptiX / Python / Vulkan / RTC 五项是可选能力，由 `NGP_BUILD_WITH_*` 开关控制。
- `testbed.cu`（5672 行）与 `testbed.h`（1294 行）是整个项目的中枢，`Testbed` 类承载全部状态与分发逻辑，四个 `testbed_*.cu` 是它按模式拆分的成员函数实现。

## 7. 下一步学习建议

本讲只画了「地图」，还没有进入任何一栋建筑。下一步建议：

- 进入第二单元 **u2-l1「Testbed 类与四种模式」**：精读 `testbed.h` 的 `Testbed` 类声明，看清 `ETestbedMode`、`set_mode`、`m_nerf/m_sdf/m_image/m_volume` 四个内嵌状态结构、以及顶层的 loss / optimizer / encoding / network / trainer 五大网络成员。
- 若想先跑起来再读源码，可先跳到 **u1-l3「构建与编译」** 和 **u1-l4「命令行运行与示例场景」**，亲手编译并加载 `data/nerf/fox`。
- 阅读源码时，把本讲的「`src/`↔`include/` 对应表」和「`configs/` 四模式」两张表放在手边，随时定位文件。
