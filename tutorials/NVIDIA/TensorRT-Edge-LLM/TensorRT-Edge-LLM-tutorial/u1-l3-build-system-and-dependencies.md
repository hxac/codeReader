# u1-l3 构建系统与依赖

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 TensorRT Edge-LLM 的 C++ 构建脚本（`CMakeLists.txt` / `cpp/CMakeLists.txt`），知道一条最小化构建命令里每个参数的意义；
- 解释 `TRT_PACKAGE_DIR`、`CUDA_CTK_VERSION`、`LD_LIBRARY_PATH` 这三个最常踩坑的配置项分别做什么；
- 理解 Git 子模块（googletest / nlohmann/json / NVTX）在构建中的作用，以及为什么必须先 `git submodule update --init`；
- 分清 Python 端的「基础依赖」「tools 可选依赖」「server 可选依赖」三者的边界与各自服务哪一段流水线。

承接上一讲：上一讲我们建立了「检查点 → ONNX → engine → 推理」的全局地图。本讲解决一个现实问题——**这条流水线的两端（Python 导出前端 与 C++ 运行时）到底怎么在本机被编译/安装出来，才能跑起来**。

## 2. 前置知识

- **CMake**：C/C++ 项目的「构建脚本生成器」。你写一份 `CMakeLists.txt`，它根据平台、依赖、选项生成对应的 `make` / `ninja` 文件。本讲只需理解「选项（`option`/`-D` 变量）」「库目标（`add_library`）」「子目录（`add_subdirectory`）」这几个概念。
- **静态库 vs 共享库**：静态库（`.a`）在链接期被「复制」进可执行文件；共享库（`.so`）在运行期才被加载。本讲会看到为什么 TensorRT 插件必须是共享库。
- **Git 子模块（submodule）**：把另一个 Git 仓库嵌套进当前仓库某个子目录，作为依赖。它不会随 `git clone` 自动拉取，需要单独初始化。
- **环境变量与动态链接**：Linux 加载共享库时，会按 `LD_LIBRARY_PATH` 指定的目录去查找。装好的库若不在默认搜索路径里，运行时就要靠它补上。

> 不熟悉以上概念没关系，下面都会结合本项目源码逐一说明。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt` | 顶层 C++ 构建脚本：声明项目、CUDA/TensorRT 依赖、编译选项、子目录与单元测试/Python 绑定的开关。 |
| `cpp/CMakeLists.txt` | 定义 C++ 端四个库目标：`edgellmKernels` / `edgellmCore` / `edgellmBuilder`（静态）与 `NvInfer_edgellm_plugin`（共享）。 |
| `cmake/FindTensorRT.cmake` | 自定义的「查找 TensorRT」模块，`TRT_PACKAGE_DIR` 在这里被解析为搜索根目录。 |
| `pyproject.toml` | Python 包的元数据：基础依赖、tools/server 可选依赖、六个命令行入口（`[project.scripts]`）。 |
| `requirements.txt` | Python 基础依赖的「锁定清单」（与 `pyproject.toml` 的 `dependencies` 一致）。 |
| `requirements-server.txt` | 实验性服务端（FastAPI/Uvicorn/pybind11）依赖清单。 |
| `.gitmodules` | 声明三个 Git 子模块：googletest、nlohmann/json、NVTX。 |
| `AGENTS.md` | 项目协作指南，给出了四类标准构建命令与环境变量铁律。 |
| `docs/source/user_guide/getting_started/installation.md` | 官方安装文档，含各边缘平台（Thor/Orin/Spark）的具体 CMake 参数表。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 CMake 构建系统**（C++ 端）、**4.2 Python 包与依赖**（pyproject）、**4.3 Git 子模块**（submodule）。

---

### 4.1 CMake 构建系统（C++ 端）

#### 4.1.1 概念说明

C++ 运行时不是「一个可执行文件」，而是**一组库目标**加上若干**示例可执行文件**。顶层 `CMakeLists.txt` 负责统筹：声明项目、定位 CUDA 与 TensorRT、设置编译选项，然后把实际编译工作交给子目录（`cpp/`、`examples/`）。

理解 C++ 构建的关键在于三个事实：

1. **TensorRT 是外部依赖**，不会随本项目源码提供，必须由系统（JetPack / DriveOS SDK / 手动安装）准备好，CMake 只负责「找到它」。`TRT_PACKAGE_DIR` 就是告诉 CMake「去哪个根目录找 TensorRT」的提示。
2. **本项目同时构建静态库与一个共享插件库**。插件必须共享，因为 TensorRT 引擎在**运行时**动态加载插件符号——静态链接会把插件符号埋进可执行文件，TensorRT 反而找不到。
3. **构建是分层的**：顶层管全局选项，`cpp/CMakeLists.txt` 管库目标的源文件集合，`examples/CMakeLists.txt` 管示例程序。

#### 4.1.2 核心流程

一次 `cmake .. && make` 的执行流程可概括为：

```text
1. 顶层 CMakeLists.txt
   ├─ 声明项目(tensorrt_edgellm_sdk)、C++17/CUDA17、编译警告(-Werror)
   ├─ 定位 CUDA：用 CUDA_CTK_VERSION(默认12.8) 推导 CUDA_DIR
   ├─ 设置 CMAKE_CUDA_ARCHITECTURES(80;86;89 [+100a;120])
   ├─ 处理 NVTX 选项(ENABLE_NVTX_PROFILING，默认 OFF)
   ├─ find_package(TensorRT REQUIRED COMPONENTS OnnxParser)  ← 这里读 TRT_PACKAGE_DIR
   ├─ add_subdirectory(cpp)        → 生成 edgellmKernels/Core/Builder + 插件.so
   ├─ add_subdirectory(examples)   → 生成 llm_build/llm_inference 等可执行文件
   └─ 若 BUILD_UNIT_TESTS=ON       → 拉 googletest 子模块，编译 unitTest
2. make -j
   └─ 按依赖关系编译上述目标，产物落到 build/ 下
3. 运行前
   └─ export LD_LIBRARY_PATH=$TRT_PACKAGE_DIR/lib:$LD_LIBRARY_PATH
```

注意第 1 步里的「读 `TRT_PACKAGE_DIR`」：它发生在 `find_package(TensorRT)` 内部，由 `cmake/FindTensorRT.cmake` 处理。

#### 4.1.3 源码精读

**(1) 顶层项目声明与语言标准**

[CMakeLists.txt:16-26](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L16-L26) 声明了项目名 `tensorrt_edgellm_sdk`、版本 `0.9.1`，启用 C/C++/CUDA 三种语言，并强制 C++17 与 CUDA 17 标准。记住版本号 0.9.1 与 git 提交历史里的 release 标签对应。

顶层还设置了 `-Werror`（[CMakeLists.txt:28-34](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L28-L34)），这意味着**任何编译警告都会让构建失败**——这是为什么本项目代码风格（如 east-const）要求很严。

**(2) CUDA 版本变量名的「陷阱」**

[CMakeLists.txt:54-62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L54-L62) 是新手最容易踩的坑：

```cmake
if(DEFINED CUDA_VERSION)
  message(FATAL_ERROR "CUDA_VERSION can cause ambiguity ... Please use -DCUDA_CTK_VERSION ...")
endif()
set_ifndef(CUDA_CTK_VERSION 12.8)
set_ifndef(CUDA_DIR /usr/local/cuda-${CUDA_CTK_VERSION})
```

它**主动拒绝** `-DCUDA_VERSION`，因为该名字会和 CUDA 头文件里的宏冲突；必须改用 `-DCUDA_CTK_VERSION`。`set_ifndef` 是本项目自定义的「若未定义则赋默认值」宏（[CMakeLists.txt:40-45](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L40-L45)），默认 CUDA 12.8、CUDA 目录 `/usr/local/cuda-12.8`。

CUDA 架构（SM 版本）默认编译 `80;86;89`，CUDA 12.8+ 再追加 `100a;120`（[CMakeLists.txt:64-69](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L64-L69)）。交叉编译（`AARCH64_BUILD`）时则跳过这一步，改由工具链文件决定。

**(3) `TRT_PACKAGE_DIR` 真正被消费的地方**

顶层 [CMakeLists.txt:129-130](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L129-L130) 调用 `find_package(TensorRT REQUIRED COMPONENTS OnnxParser)`。该命令会加载自定义模块 [cmake/FindTensorRT.cmake:16-23](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/FindTensorRT.cmake#L16-L23)：

```cmake
if(DEFINED TRT_PACKAGE_DIR AND NOT DEFINED TENSORRT_ROOT)
  set(TENSORRT_ROOT ${TRT_PACKAGE_DIR})
endif()
set(_trt_hints ${TENSORRT_ROOT} /usr /opt/tensorrt)
```

读法：`TRT_PACKAGE_DIR` 被当作**首要搜索根目录提示**，随后在该根下用标准后缀（`lib`、`lib64`、`include`）去找 `nvinfer` 库与 `NvInfer.h` 头文件（[cmake/FindTensorRT.cmake:25-39](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/FindTensorRT.cmake#L25-L39)）。如果没传，它会回退到 `/usr`、`/opt/tensorrt` 等系统路径——这正是 JetPack/DriveOS 把 TensorRT 装在 `/usr` 时**可以省略 `TRT_PACKAGE_DIR`** 的原因；而 x86 开发机往往装了多个版本，就需要显式指定来消歧。

找不到时报错信息（[cmake/FindTensorRT.cmake:52-56](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/FindTensorRT.cmake#L52-L56)）也提示用户「请指定 `-DTRT_PACKAGE_DIR=/path/to/TRT`」。

**(4) 四个库目标：三个静态 + 一个共享插件**

[cpp/CMakeLists.txt:104-144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L104-L144) 定义了核心库：

- `edgellmKernels`（静态，[cpp/CMakeLists.txt:104-112](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L104-L112)）：纯算子（common + kernels）。
- `edgellmCore`（静态，[cpp/CMakeLists.txt:116-136](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L116-L136)）：运行时主体（runtime + sampler + tokenizer + multimodal + kernels + common）。
- `edgellmBuilder`（静态，[cpp/CMakeLists.txt:139-144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L139-L144)）：ONNX → TRT 构建器（builder + common）。
- `NvInfer_edgellm_plugin`（**共享**，[cpp/CMakeLists.txt:146-163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L146-L163)）：自定义 TensorRT 插件。

插件为何是 `SHARED`？AGENTS.md 给了一句话结论（[AGENTS.md:98](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L98)）：「`NvInfer_edgellm_plugin` 是共享（而非静态）库，因为 TensorRT 在运行时动态加载插件。」源码里也能看到对应的链接选项（[cpp/CMakeLists.txt:162-163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L162-L163)）用 `-Wl,--exclude-libs,ALL` 隐藏从静态库拉进来的符号，只暴露插件导出点。

**(5) 开关：单元测试与 Python 绑定**

顶层用两个 `option` 控制可选构建（[CMakeLists.txt:162-163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L162-L163)）：`BUILD_UNIT_TESTS`（默认 OFF）、`BUILD_PYTHON_BINDINGS`（默认 OFF）。开启前者会拉取 googletest 子模块并编译 `unitTest`（[CMakeLists.txt:188-208](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L188-L208)），开启后者会编译 `experimental/pybind` 的 `_edgellm_runtime` 扩展（[CMakeLists.txt:210-212](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L210-L212)）。

还有第四个关键开关 `ENABLE_CUTE_DSL`（[CMakeLists.txt:165-186](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L165-L186)）：用于启用预构建的 CuTe DSL 算子（fmha/gdn/moe 等）。官方文档明确建议边缘构建都加 `-DENABLE_CUTE_DSL=ALL`，因为 Qwen3.5 等模型路径依赖它。

**(6) 四类标准构建命令**

AGENTS.md 把命令固化成一张表（[AGENTS.md:24-41](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L24-L41)），最常用的四种：

| 场景 | 命令 |
|------|------|
| 最小化构建 | `cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR && make -j$(nproc)` |
| 带单元测试 | `cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR -DBUILD_UNIT_TESTS=ON && make -j$(nproc)` |
| 交叉编译 AArch64 | `cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR -DAARCH64_BUILD=ON && make -j$(nproc)` |
| 启用 NVTX | `cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR -DENABLE_NVTX_PROFILING=ON && make -j$(nproc)` |

#### 4.1.4 代码实践

**实践目标**：理解「最小化构建」到底包含哪些步骤，并能解释 `TRT_PACKAGE_DIR`。

**操作步骤**（如果你本机有 CUDA + TensorRT，可实际执行；否则按源码阅读型实践理解）：

1. 先在仓库根目录确认顶层 CMake 脚本与查找模块存在。
2. 阅读 [cmake/FindTensorRT.cmake:16-23](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/FindTensorRT.cmake#L16-L23)，确认 `TRT_PACKAGE_DIR` 被赋给 `TENSORRT_ROOT`，然后进入 `_trt_hints`。
3. 假设你 TensorRT 装在 `/usr/local/TensorRT-10.x.x`，写出最小化构建命令：
   ```bash
   export TRT_PACKAGE_DIR=/usr/local/TensorRT-10.x.x
   mkdir -p build && cd build
   cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR
   make -j$(nproc)
   ```
4. 构建产物出现后，运行示例前补上库搜索路径：
   ```bash
   export LD_LIBRARY_PATH=$TRT_PACKAGE_DIR/lib:$LD_LIBRARY_PATH
   ./examples/llm/llm_build --help
   ```

**需要观察的现象**：`cmake ..` 输出里会打印 `Configurable variable CUDA_CTK_VERSION set to 12.8`（或你传入的值），以及 `TensorRT` 被找到的路径；`make` 结束后在 `build/` 下出现 `libNvInfer_edgellm_plugin.so`、若干 `.a` 静态库和 `examples/llm/llm_build` 等可执行文件。

**预期结果**：`llm_build --help` 能正常打印帮助，说明库与可执行文件链接成功。**待本地验证**（依赖真实 GPU/TensorRT 环境）。

> `TRT_PACKAGE_DIR` 的作用一句话总结：**它是告诉 CMake「去哪个根目录找 TensorRT 的 `lib/` 与 `include/`」的提示**；省略时回退到 `/usr`、`/opt/tensorrt`，多版本共存时必须显式指定以消歧。

#### 4.1.5 小练习与答案

**练习 1**：如果不小心传了 `-DCUDA_VERSION=12.8`，CMake 会怎样？

**答案**：构建会**立即失败**（`FATAL_ERROR`）。顶层 [CMakeLists.txt:55-60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L55-L60) 主动拒绝该变量名，要求改用 `-DCUDA_CTK_VERSION`。

**练习 2**：为什么 `NvInfer_edgellm_plugin` 是 `SHARED` 而其它三个库是 `STATIC`？

**答案**：TensorRT 引擎在**运行时**通过动态加载机制发现并注册插件符号；只有共享库（`.so`）才能被这样加载。静态库的符号会被埋进可执行文件，TensorRT 在运行期找不到。其余三个库（Kernels/Core/Builder）是项目内部使用的实现，静态链接进可执行文件或插件即可。

---

### 4.2 Python 包与依赖（pyproject）

#### 4.2.1 概念说明

Python 端（`tensorrt_edgellm` 包）是流水线的**导出前端**，跑在 x86 开发机上。它的依赖被刻意分成三档：

- **基础依赖**：导出 ONNX 所必需（torch/transformers/onnx 等）；
- **tools 可选依赖**：量化、LoRA 合并、词表裁剪、音频预处理所需的额外包（ModelOpt/datasets/librosa 等）；
- **server 可选依赖**：实验性 OpenAI 兼容服务端（FastAPI/Uvicorn/pybind11）。

这种分档的动机在安装文档里写得很直白（[docs/source/user_guide/getting_started/installation.md:94-108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/installation.md#L94-L108)）：可选 tools 依赖刻意不进基础环境，这样「只做导出」或「只跑服务端」的镜像就不会被量化、音频、LoRA-merge 等大包拖大。

另一个关键点：**核心依赖版本被严格锁定（pin）**。AGENTS.md 提醒（[AGENTS.md:101](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L101)）：`transformers`、`nvidia-modelopt`、`onnx`、`torch` 都被钉死版本，随意升级会破坏导出/量化。

#### 4.2.2 核心流程

```text
pyproject.toml
  ├─ [build-system]      → setuptools 构建
  ├─ [project]
  │    ├─ dependencies   → 基础依赖（导出必需，7 个锁定包）
  │    ├─ optional-dependencies
  │    │    ├─ tools     → 量化/LoRA/词表/音频 额外包
  │    │    ├─ server    → FastAPI/Uvicorn/pybind11
  │    │    └─ dev       → （空占位）
  │    └─ scripts        → 6 个 tensorrt-edgellm-* 命令 → 各自的 main 函数
  └─ 安装方式
       ├─ pip install .            → 仅基础
       ├─ pip install ".[tools]"   → 基础 + tools
       └─ pip install -r requirements-server.txt  → 服务端
```

`requirements.txt` 与 `requirements-server.txt` 是「平铺清单」版本的依赖，内容分别对应基础依赖与服务端依赖，便于不走 `pip install .` 的场景直接安装。

#### 4.2.3 源码精读

**(1) 基础依赖（锁定版）**

[pyproject.toml:24-32](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L24-L32) 列出 7 个基础包，全部锁定：`torch==2.12.0`、`transformers==5.9.0`、`onnx==1.19.0`、`onnxscript==0.7.0`、`safetensors==0.7.0`、`numpy==2.4.6`、`onnx-graphsurgeon==0.6.1`。这与 [requirements.txt:1-7](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/requirements.txt#L1-L7) 完全一致——后者就是这 7 个包的镜像。

**(2) tools 与 server 可选依赖**

[pyproject.toml:34-52](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L34-L52)：

- `tools`：`nvidia-modelopt==0.44.0`（量化核心）、`datasets`（校准数据）、`peft`（LoRA）、`librosa`/`soundfile`（音频）、`tiktoken`（分词）、`torchvision`、`einops`、`tqdm`、`backoff`。
- `server`：`fastapi==0.136.1`、`uvicorn==0.47.0`、`pybind11==2.13.6`，与 [requirements-server.txt:1-3](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/requirements-server.txt#L1-L3) 一致。
- `dev`：空数组占位。

**注意**：安装文档实际推荐用 `pip3 install -r requirements-server.txt` 安装服务端依赖（[installation.md:106-108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/installation.md#L106-L108)），它与 `pyproject` 的 `server` extra 内容等价，二选一即可。

**(3) 六个命令行入口**

[pyproject.toml:54-60](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L54-L60) 用 `[project.scripts]` 把命令名映射到 `main` 函数：

| 命令 | 入口 |
|------|------|
| `tensorrt-edgellm-quantize` | `tensorrt_edgellm.scripts.quantize:main` |
| `tensorrt-edgellm-export` | `tensorrt_edgellm.scripts.export:main` |
| `tensorrt-edgellm-insert-lora` | `tensorrt_edgellm.scripts.insert_lora:main` |
| `tensorrt-edgellm-process-lora` | `tensorrt_edgellm.scripts.process_lora_weights:main` |
| `tensorrt-edgellm-merge-lora` | `tensorrt_edgellm.scripts.merge_lora:main` |
| `tensorrt-edgellm-reduce-vocab` | `tensorrt_edgellm.scripts.reduce_vocab:main` |

`pip install .` 之后，这六个命令就被注册到 PATH，可以直接调用。这些入口的用途会在后续讲义（u1-l4 CLI 入口）详解。

**(4) 包发现范围与动态版本**

[pyproject.toml:65-67](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L65-L67) 表明打包范围包含 `tensorrt_edgellm*` 与 `experimental*`（实验性 Python API/服务端）。版本号是动态的，取自 `tensorrt_edgellm._version.__version__`（[pyproject.toml:73-74](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L73-L74)）。

#### 4.2.4 代码实践

**实践目标**：分清三档依赖，并组装出「只导出」「量化+导出」「跑服务端」三种安装命令。

**操作步骤**：

1. 打开 `pyproject.toml`，确认 `dependencies`（基础）与 `optional-dependencies.tools`（量化所需）的内容。
2. 对照 [installation.md:98-108](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/installation.md#L98-L108)，写出三种安装命令：
   ```bash
   # 仅基础导出能力
   pip3 install .
   # 量化/LoRA合并/词表裁剪/音频预处理
   pip3 install ".[tools]"
   # 实验性 OpenAI 兼容服务端
   pip3 install -r requirements-server.txt
   ```
3. （可选）安装后验证命令是否注册：
   ```bash
   tensorrt-edgellm-export --help
   ```

**需要观察的现象**：`pip install ".[tools]"` 会额外拉取 `nvidia-modelopt` 等大包；而 `pip install .` 不会。

**预期结果**：`tensorrt-edgellm-export --help` 能正常打印帮助。**待本地验证**（依赖 Python 环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pip install .` 不安装 `nvidia-modelopt`？

**答案**：因为 `nvidia-modelopt` 在 `optional-dependencies.tools` 而非 `dependencies` 里。基础安装只覆盖导出 ONNX 所需的 7 个包；量化能力属于可选 tools，需要 `pip install ".[tools]"` 显式启用，以保持基础镜像精简。

**练习 2**：`transformers` 从 5.9.0 升级到更高版本会有什么风险？

**答案**：高风险。AGENTS.md（[AGENTS.md:101](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L101)）明确这些核心依赖被钉死版本，升级可能破坏 config 解析、检查点加载或 ONNX 导出的兼容性，需先验证再升级。

---

### 4.3 Git 子模块（submodule）

#### 4.3.1 概念说明

本项目没有把第三方依赖直接复制进仓库，而是用 Git 子模块引用了三个外部项目，全部放在 `3rdParty/` 下。子模块的好处是依赖版本可追踪、不膨胀主仓库；代价是 **`git clone` 默认不拉取子模块内容**，必须单独初始化。

AGENTS.md 把「初始化子模块」列为铁律之一（[AGENTS.md:18](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L18)）。

#### 4.3.2 核心流程

```text
git clone <repo>          # 主仓库代码到位，3rdParty/ 各子目录为空
git submodule update --init   # 拉取三个子模块到指定 commit
   ├─ 3rdParty/googletest    → 单元测试(BUILD_UNIT_TESTS=ON) 时才编译
   ├─ 3rdParty/nlohmannJson  → C++ JSON 解析（common 头文件引用）
   └─ 3rdParty/NVTX          → NVTX profiling(ENABLE_NVTX_PROFILING=ON) 时才引用
```

三个子模块的「使用时机」不同：nlohmann/json 在常规构建里就被引用；googletest 只在开单元测试时编译；NVTX 只在开 profiling 时引用。但无论哪种，**子模块目录必须先有内容**，否则对应功能会因找不到文件而失败。

#### 4.3.3 源码精读

**子模块声明**：[.gitmodules:1-9](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/.gitmodules#L1-L9) 声明了三个子模块及其路径与上游 URL。

**JSON 头在常规构建中被引用**：顶层 `COMMON_INCLUDE_DIRS` 包含 `3rdParty/nlohmannJson/include`（[CMakeLists.txt:137-143](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L137-L143)），所以即便最小化构建也需要该子模块到位。

**googletest 仅在单元测试时启用**：[CMakeLists.txt:188-192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L188-L192) 在 `BUILD_UNIT_TESTS` 为真时才 `add_subdirectory(3rdParty/googletest)`。

**NVTX 仅在 profiling 时启用**：[CMakeLists.txt:103-127](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L103-L127) 检查 `ENABLE_NVTX_PROFILING`，启用时定位 `3rdParty/NVTX/include/nvtx3`；若头文件不存在则 `FATAL_ERROR`，并提示运行 `git submodule update --init 3rdParty/NVTX`（[CMakeLists.txt:116-122](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L116-L122)）。

#### 4.3.4 代码实践

**实践目标**：验证子模块缺失会导致构建失败，并学会初始化。

**操作步骤**：

1. 在仓库根目录执行（只读地）查看子模块状态：
   ```bash
   git submodule status
   ```
2. 对照 [.gitmodules:1-9](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/.gitmodules#L1-L9)，确认三个路径。
3. 初始化命令（AGENTS.md 推荐写法）：
   ```bash
   git submodule update --init --recursive
   ```

**需要观察的现象**：未初始化时 `3rdParty/nlohmannJson/include` 为空，`cmake ..` 能过但 `make` 会在引用 `<nlohmann/json.hpp>` 处报找不到头文件；初始化后即可正常编译。

**预期结果**：`git submodule status` 三个条目前不再有 `-` 前缀（`-` 表示未初始化）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `git clone` 之后 `3rdParty/googletest` 是空的？

**答案**：Git 默认不拉取子模块内容，子模块目录只记录一个「指针」（指向某 commit）。需要 `git submodule update --init` 才会把实际文件拉下来。

**练习 2**：最小化构建（不开单元测试、不开 NVTX）是否需要 googletest 子模块？

**答案**：编译期不需要——googletest 仅在 `BUILD_UNIT_TESTS=ON` 时才 `add_subdirectory`（[CMakeLists.txt:188-192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L188-L192)）。但 nlohmann/json 在常规构建里就被引用（[CMakeLists.txt:137-143](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L137-L143)），所以**最小化构建至少必须初始化 nlohmann/json 子模块**。

---

## 5. 综合实践

把本讲三个模块串起来，完成「从空仓库到可运行 C++ 示例」的完整命令清单。这是本讲的主实践任务，对照 AGENTS.md 的命令表（[AGENTS.md:24-41](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L24-L41)）。

**任务**：写出在本机执行最小化 C++ 构建所需的全部命令，并解释 `TRT_PACKAGE_DIR`。

**步骤**：

1. **克隆并初始化子模块**（子模块模块）：
   ```bash
   git clone https://github.com/NVIDIA/TensorRT-Edge-LLM.git
   cd TensorRT-Edge-LLM
   git submodule update --init --recursive   # 拉取 googletest / nlohmannJson / NVTX
   ```
2. **设定 TensorRT 根目录**（CMake 模块）：
   ```bash
   export TRT_PACKAGE_DIR=/usr/local/TensorRT-10.x.x   # 换成你的实际路径；JetPack 下可设 /usr
   ```
   `TRT_PACKAGE_DIR` 的作用：作为 `cmake/FindTensorRT.cmake` 的**首要搜索根提示**，告诉 CMake 去哪个目录的 `lib/`、`include/` 下找 `nvinfer` 库与 `NvInfer.h`；省略时回退到 `/usr`、`/opt/tensorrt`，多版本 x86 环境必须显式指定以消歧。
3. **配置并构建**（CMake 模块）：
   ```bash
   mkdir -p build && cd build
   cmake .. -DTRT_PACKAGE_DIR=$TRT_PACKAGE_DIR
   make -j$(nproc)
   ```
4. **设置库搜索路径并验证**：
   ```bash
   export LD_LIBRARY_PATH=$TRT_PACKAGE_DIR/lib:$LD_LIBRARY_PATH
   ./examples/llm/llm_build --help
   ```

**进阶**（选做）：

- 开单元测试：在第 3 步的 cmake 命令加 `-DBUILD_UNIT_TESTS=ON`，构建后跑 `./build/unitTest`。
- 对照官方平台表（[installation.md:346-356](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/installation.md#L346-L356)），说明 Jetson Thor 边缘构建为什么还要加 `-DCMAKE_TOOLCHAIN_FILE`、`-DEMBEDDED_TARGET`、`-DCUDA_CTK_VERSION`、`-DENABLE_CUTE_DSL=ALL`。

**预期结果**：`llm_build --help` 正常输出帮助文本，证明 Python（虽此处未装，但流水线上游需要）与 C++ 运行时构建链路打通。**待本地验证**（依赖真实 CUDA + TensorRT 环境）。

## 6. 本讲小结

- C++ 构建分三层：顶层 `CMakeLists.txt` 管全局选项与依赖定位，`cpp/CMakeLists.txt` 产出三个静态库（Kernels/Core/Builder）+ 一个**共享**插件库 `NvInfer_edgellm_plugin`，`examples/` 产出可执行文件。
- `TRT_PACKAGE_DIR` 是 TensorRT 的「搜索根提示」，由 `cmake/FindTensorRT.cmake` 消费；省略时回退到 `/usr`、`/opt/tensorrt`，多版本 x86 环境必须显式指定。
- CUDA 版本变量必须叫 `CUDA_CTK_VERSION`（默认 12.8），传 `CUDA_VERSION` 会被主动拒绝；运行时还要靠 `LD_LIBRARY_PATH` 让动态链接器找到 TensorRT 库。
- Python 依赖分三档：基础（导出必需，7 个锁定包）、`tools`（量化/LoRA/词表/音频）、`server`（FastAPI/Uvicorn/pybind11），核心包版本被严格 pin。
- Git 子模块有三个：nlohmann/json（常规构建即引用）、googletest（仅单元测试）、NVTX（仅 profiling）；`git clone` 后必须 `git submodule update --init` 才有内容。
- 插件库必须是共享库，因为 TensorRT 在运行时动态加载插件符号——这是本项目 C++ 构建最关键的「为什么」之一。

## 7. 下一步学习建议

- 构建能跑通后，下一讲 **u1-l4 CLI 入口与包导出** 会把本讲提到的六个 `tensorrt-edgellm-*` 命令逐一对应到 `scripts/` 下的 `main` 函数，并展示 Python 包的对外 API。
- 想立刻端到端跑通流水线，可跳到 **u1-l5 端到端流水线实战**，把 export → build → inference 三步串起来。
- 深入 C++ 库目标内部，可后续阅读 **u4（C++ 引擎构建器）** 与 **u5（C++ 运行时核心）**；本讲的 `edgellmCore`/`edgellmBuilder`/插件库正是那些讲义的前置产物。
- 想理解 FMHA 算子的 SM 特异性编译（`cpp/CMakeLists.txt` 顶部的 `FMHA_ALL_SM_VERSIONS` 逻辑），可留到 **u8-2 自定义 CUDA 算子** 再展开。
