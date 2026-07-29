# 目录结构与代码组织

## 1. 本讲目标

上一篇（u1-l2）你已经学会把仓库装好，并用 `ENABLE_TILE=1` 在 PTX 后端与 TileIR 后端之间切换。但从「能跑」到「能读源码」，你还需要一张地图：**TileIR 后端的代码到底放在哪里？哪些是上游 Triton 已有的，哪些是这个孵化器仓库新增的？**

本讲学完后，你应当能够：

1. 说出仓库顶层每个目录的职责，并区分「上游 Triton 原生目录」与「TileIR 新增内容」。
2. 在 `third_party/tileir/` 这棵子树里，立刻定位到「后端 Python」「C++ 转换 Pass」「lit 测试」「构建脚本」四类文件。
3. 理解 `setup.py` 如何把 `tileir` 注册为一个 **in-tree（仓库内置）后端**，并解释为什么在源码里直接找不到 `python/triton/backends/tileir/`。

本讲只读源码、不修改任何代码，是一节纯粹的「认路」课，为后续深入编译流水线（u1-l4、第二单元）打基础。

## 2. 前置知识

阅读本讲前，请确认你已理解上一篇讲义建立的几个概念：

- **孵化器仓库**：本仓库 = 上游 Triton + 一条新编译路径（CUDA Tile IR 后端），不是替换。
- **两个后端共享前端**：Python 内核 → TTIR 这一段是共用的，分歧从 TTIR 之后开始。
- **ENABLE_TILE=1**：运行期开关，决定激活哪个 driver。
- **oait / nvtriton**：oait 指上游 OpenAI Triton（走 PTX 后端，默认）；nvtriton 指本仓库发布的 wheel（走 TileIR 后端）。

本讲会反复用到的一个关键词是 **in-tree backend（仓库内置后端）**。Triton 允许第三方把后端代码放在 `third_party/<名字>/` 下，构建时自动注册，无需改动核心代码。`tileir` 就是这样一个 in-tree 后端，和它并列的还有 `nvidia`（PTX 后端）与 `amd`（ROCm 后端）。理解这一点，本讲的目录结构就迎刃而解。

## 3. 本讲源码地图

本讲主要阅读以下文件，理解它们如何「描述」并「组织」目录结构：

| 文件 | 作用 |
|------|------|
| `README.md` | 仓库总说明，包含 ChangeList，明确列出「改了哪些核心文件」「新增了哪些后端文件」 |
| `setup.py` | Python 打包与构建入口，决定 `tileir` 如何被发现、注册、链接进 `triton` 包 |
| `pyproject.toml` | 构建依赖声明（cmake / ninja / pybind11）与代码风格配置 |

此外会大量「浏览」目录而非逐行读代码：`third_party/tileir/`、`python/triton/`、顶层 `lib/` 与 `include/`。浏览用 `ls`/`find` 即可，不需要运行构建。

## 4. 核心概念与源码讲解

### 4.1 顶层目录概览：上游 Triton 与 TileIR 增量

#### 4.1.1 概念说明

本仓库根目录混合了两类东西：

- **上游 Triton 原生目录**：`python/`、`lib/`、`include/`、`bin/`、`cmake/`、`docs/`、`examples/`、`test/`、`unittest/`、`utils/`、`scripts/`。这些是 Triton 本体，TileIR 不重写它们。
- **TileIR 增量内容**：几乎全部集中在 `third_party/tileir/`，外加对极少数核心文件的「小范围修改」。

因此，判断一段代码归属哪一侧的最快方法是：**看路径前缀**。

- 路径以 `third_party/tileir/` 开头 → TileIR **新增**模块。
- 路径在 `python/triton/...`、`third_party/nvidia/...` 下且 README ChangeList 点名提到 → TileIR **修改**过的核心文件。
- 其余 `python/`、`lib/`、`include/` 下的内容 → 上游 Triton 原生，本仓库基本不动。

#### 4.1.2 核心流程：用 README 的 ChangeList 做「分类锚点」

README 把改动分成两组，正好对应「核心改动」与「后端新增」：

```
### Triton's core files changes:      ← 修改了已有文件
### CUDA Tile IR Backend support:     ← 新增了后端代码
```

「核心改动」一组列出了被修改的文件名（不带完整路径），我们需要把它们映射到真实路径。「后端新增」一组则指出新代码的实现文件，它们都落在 `third_party/tileir/` 里。

#### 4.1.3 源码精读

先看 README 对仓库定位的一句话：

[README.md:40](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L40) ——「This incubator repo **adds** the CUDA Tile IR backend to Triton」，关键词是 adds（新增），点明这是叠加而非替换。

再看 ChangeList 的「核心文件改动」小节：

[README.md:80-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L80-L84) —— 列出三类核心改动。把其中点名的文件映射到仓库真实路径，就得到下表（路径已在本仓库中逐一确认存在）：

| README 点名的文件 | 仓库中的真实路径 | 改动主题 |
|------|------|------|
| `driver.py` / `compiler.py` | `python/triton/runtime/driver.py`、`python/triton/compiler/compiler.py` | `ENABLE_TILE=1` 时切换目标到 TileIR |
| `jit.py` / `nvidia/backend/driver.py` | `python/triton/runtime/jit.py`、`third_party/nvidia/backend/driver.py` | 编译失败时回退到 PTX 后端 |
| `core.py` / `semantic.py` / `tensor_descriptor.py` | `python/triton/language/core.py`、`python/triton/language/semantic.py`、`python/triton/tools/tensor_descriptor.py` | 把 host TMA API 降级为 TileIR 的 device TMA |

接着是「后端新增」小节：

[README.md:88-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L88-L92) —— 三条：①转换 Pass（TTIR→CUDA Tile IR），实现文件 `TritonToCudaTile.*`；②assume 重写 Pass，实现文件 `rewriteAssume.*`；③Python 代码「mostly aligned with `third_party/nvidia/backend`」（与 NVIDIA 后端结构基本对齐）。这三条全部对应 `third_party/tileir/` 下的文件，下一节展开。

> 提示：README 用的是历史文件名（`TritonToCudaTile.*`、`rewriteAssume.*`），与仓库当前的实际文件名（`TritonToTileIRPass.cpp`、`RewriteAssumeWithCudaTile.cpp`）略有出入。读源码时以实际文件为准，README 的「主题」描述仍然准确。

#### 4.1.4 代码实践：定位三类核心改动

1. **实践目标**：把 README ChangeList 里点名的核心文件，全部在仓库里找到真实路径，确认它们属于「修改」而非「新增」。
2. **操作步骤**：
   - 打开 [README.md:80-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L80-L84)。
   - 对 `driver.py`、`compiler.py`、`jit.py`、`core.py`、`semantic.py`、`tensor_descriptor.py`，用编辑器全局搜索或 `git ls-files | grep <名字>` 定位。
3. **需要观察的现象**：这些文件分散在 `python/triton/runtime/`、`python/triton/compiler/`、`python/triton/language/`、`python/triton/tools/`，**没有一个**落在 `third_party/tileir/` 下。
4. **预期结果**：得到与上文 4.1.3 表格一致的路径。结论：核心改动是「在原地处修改已有文件」，而不是新增文件。
5. 如果你的搜索结果与表格不一致，请确认你查的是本仓库（HEAD = `1bd89c0dfb66fc99d4d338af4baddd2874de9d87`），不同版本的 Triton 目录略有差异。

#### 4.1.5 小练习与答案

**练习 1**：顶层 `lib/` 和 `include/` 目录属于「上游 Triton 原生」还是「TileIR 新增」？为什么？

> **答案**：属于上游 Triton 原生。顶层 `lib/` 下是 `Analysis`、`Conversion`、`Dialect`、`Target`、`Tools`，这是 Triton 自身的 MLIR 方言与转换（如 ttir→llvm）；TileIR 的 C++ 全部在 `third_party/tileir/lib` 与 `third_party/tileir/include`，两者路径不同、互不混用。

**练习 2**：README 第 92 行说 TileIR 的 Python 代码「mostly aligned with `third_party/nvidia/backend`」。请说出这两个目录在文件构成上为什么相似。

> **答案**：因为它们都是遵循同一套「后端接口契约」的 in-tree 后端。每个后端都必须提供 `backend/compiler.py`、`backend/driver.py` 等文件（`setup.py` 会断言它们存在）。`third_party/nvidia/backend/` 下有 `__init__.py`、`compiler.py`、`driver.py`、`driver.c`，`third_party/tileir/backend/` 下也是同样的核心几件套，只是实现不同。

---

### 4.2 `third_party/tileir/` 结构：TileIR 后端的大本营

#### 4.2.1 概念说明

`third_party/tileir/` 是 TileIR 后端的所有「新增」内容所在。它内部按职责切成几块，分别对应一种产物：

- **后端 Python**：`backend/`，运行期负责编译选项、编译流水线、driver、启动器、错误分类。
- **C++ 转换 Pass**：`lib/` + `include/` + 根目录的 `triton_tileir.cc`，把 TTIR 翻译成 CUDA Tile IR。
- **lit 测试**：`test/FileCheck/`，用 `.mlir` 文件 + FileCheck 匹配验证每个 Pass 的输出。
- **构建脚本**：`CMakeLists.txt` + `scripts/`，克隆并补丁 NVIDIA cuda-tile、打 wheel 依赖补丁等。
- **工具**：`tools/triton-cuda-tile-opt/`，一个脱离 Python、可单独跑 Pass 的命令行工具。

#### 4.2.2 核心流程：一棵子树的分层

下面是 `third_party/tileir/` 的精简目录树（仅展开到关键文件，已在本仓库逐一确认）：

```
third_party/tileir/
├── CMakeLists.txt            # 【构建脚本】本子树的 CMake 入口
├── triton_tileir.cc          # 【C++ 转换 Pass】pybind 插件入口，把 Pass 暴露给 Python
├── README.md                 # TileIR 后端用户指南（构建/运行/限制）
├── PerformanceTuningTips.md  # 性能调优要点（occupancy/num_ctas 等）
│
├── backend/                  # 【后端 Python】运行期逻辑
│   ├── __init__.py
│   ├── compiler.py           #   编译流水线 make_ttir/make_tileir/make_cubin、TileIROptions
│   ├── driver.py             #   TileIRDriver / Launcher、tile 网格启动
│   ├── conf.py               #   TileIREnvConf：环境变量与 tileiras 路径解析
│   ├── errors.py             #   错误分类（OutOfResources/TileirasError/HitFallback 等）
│   ├── code_generator.py     #   AST→TTIR 前端的 TileIR 分支
│   └── driver.c              #   C 启动胶水代码（被编译进工具模块）
│
├── lib/                      # 【C++ 转换 Pass】实现
│   ├── TritonToTileIR/       #   主转换：TritonToTileIRPass.cpp、MapElementwiseExpansion.*、Utils.cpp
│   ├── Transform/            #   其它 Pass：AutoGenMemoryToken / LiftTTCFToSCF / RewriteAssumeWithCudaTile
│   └── Utils/                #   公共工具
│
├── include/                  # 【C++ 转换 Pass】头文件 + TableGen 定义
│   ├── TritonToTileIR/       #   Passes.h / Passes.td / TritonToTileIRPass.h / Utils.h
│   ├── Transform/            #   Passes.h / Passes.td
│   └── Utils/                #   Utils.h
│
├── test/
│   └── FileCheck/            # 【lit 测试】op-conversion.mlir、fma.mlir、barrier 等
│
├── tools/
│   └── triton-cuda-tile-opt/ # 独立 opt 工具：triton-cuda-tile-opt.cpp
│
└── scripts/                  # 【构建脚本】辅助脚本
    ├── build_cuda_tile.sh            # 克隆/构建 NVIDIA cuda-tile
    ├── patch_bytecode_utils.sh       # 给 cuda-tile 打补丁
    ├── patch_cuda_tile_i1_bytecode_compat.py
    ├── build_helper/Dockerfile.release
    ├── check_wheel_deps.py / copy_wheel_deps.py / wheel_deps.json
```

记忆口诀：**「backend 跑、lib/include 转、test 验、scripts 建」**。

#### 4.2.3 源码精读

`setup.py` 在注册后端时，会强制要求每个后端的 `backend/` 目录必须含两个文件：

[setup.py:91-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L91-L92) —— 对 `compiler.py` 与 `driver.py` 做存在性断言。这正是 `third_party/tileir/backend/` 里一定有 `compiler.py`、`driver.py` 的原因——它们是后端接口契约的硬性要求。

`pyproject.toml` 则声明了构建这套 C++ 需要的工具链：

[pyproject.toml:1-3](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/pyproject.toml#L1-L3) —— `cmake>=3.20,<4.0`、`ninja>=1.11.1`、`pybind11>=2.13.1`。也就是说，`lib/` 下的 C++ Pass 是通过 CMake + Ninja 编译、再用 pybind11 暴露成 Python 可调用模块（即 `triton_tileir.cc` 的产物）。

#### 4.2.4 代码实践：四类文件归位

1. **实践目标**：在 `third_party/tileir/` 内，把下列四类文件各举出 1～2 个具体路径。
2. **操作步骤**：在仓库根目录执行只读浏览（任选其一）：
   ```bash
   ls -1 third_party/tileir/backend/
   ls -1 third_party/tileir/lib/TritonToTileIR/ third_party/tileir/lib/Transform/
   ls -1 third_party/tileir/test/FileCheck/
   ls -1 third_party/tileir/scripts/
   ```
3. **需要观察的现象**：每个目录下的文件扩展名有明显特征——`.py` 是后端 Python，`.cpp`/`.h`/`.td` 是 C++ 转换 Pass，`.mlir` 是 lit 测试，`.sh`/`.py`(scripts 下)是构建脚本。
4. **预期结果**（参考答案）：
   - 后端 Python：`backend/compiler.py`、`backend/driver.py`
   - C++ 转换 Pass：`lib/TritonToTileIR/TritonToTileIRPass.cpp`、`include/TritonToTileIR/Passes.td`、`triton_tileir.cc`
   - lit 测试：`test/FileCheck/op-conversion.mlir`、`test/FileCheck/fma.mlir`
   - 构建脚本：`CMakeLists.txt`、`scripts/build_cuda_tile.sh`
5. 这些命令只是列目录，不依赖 GPU 或构建产物，本地一定能跑；若 `ls` 报「No such file」，说明你不在仓库根目录。

#### 4.2.5 小练习与答案

**练习 1**：`include/` 下有大量 `.td` 文件（如 `Passes.td`）。它们和 `.cpp` 是什么关系？

> **答案**：`.td` 是 TableGen 定义文件，用声明式语法描述 Pass 的名称、选项、依赖方言；构建时由 TableGen 生成对应的 `.h`/`.cpp.inc` 模板代码，再被 `.cpp` 引用。所以 `.td` 是 Pass 的「规格说明」，`.cpp` 是「实现」。

**练习 2**：根目录的 `triton_tileir.cc` 为什么单独放在 `third_party/tileir/` 顶层，而不是放进 `lib/`？

> **答案**：它是整个 C++ 子树的 **pybind 入口**，负责把 `lib/` 下所有 Pass 汇总注册、编译成一个可被 Python `import` 的扩展模块。把它放在顶层，体现它是「汇总层」而非某个具体 Pass 的实现。

**练习 3**：`backend/driver.c` 是 Python 还是 C++？

> **答案**：它是 C 源码，但归属「后端 Python」这一类，因为它被 `backend/driver.py` 在运行期编译成启动胶水工具模块（用于以 tile 为单位启动内核）。它不是 MLIR 转换 Pass，所以不在 `lib/`。

---

### 4.3 构建与测试目录：`tileir` 如何被「接」进 Triton

#### 4.3.1 概念说明

光有代码还不够，必须让 Triton 在构建期和运行期都「认识」`tileir`。这由三套机制完成：

1. **后端发现**：`setup.py` 把 `tileir` 和 `nvidia`、`amd` 一起列入 in-tree 后端清单。
2. **运行期注册**：生成 `triton.backends` entry point，使 `import triton` 后能按名字找到后端。
3. **符号链接就位**：构建时在 `python/triton/backends/tileir` 建一个指向 `third_party/tileir/backend` 的软链接，让后端 Python 代码「看起来」在 triton 包内。

一个关键「坑」：**在源码树里直接找不到 `python/triton/backends/tileir/`**。这不是遗漏，而是因为它是构建时才创建的软链接。同样，C++ 转换 Pass 通过 CMake 的 `add_subdirectory(third_party/tileir)` 被编译并链入 pybind 插件。

#### 4.3.2 核心流程：从「声明后端」到「能被 import」

```text
setup.py 列出 in-tree 后端 [nvidia, amd, tileir]
        │
        ├─► CMake: add_subdirectory(third_party/tileir)  → 编译 lib/ 下 C++ → pybind 插件
        │
        ├─► 构建时建软链: python/triton/backends/tileir → third_party/tileir/backend
        │
        └─► 生成 entry point: tileir = triton.backends.tileir
                              │
运行期 import triton ─► 读 entry point ─► 加载 backends/tileir(软链) ─► ENABLE_TILE=1 时选中
```

#### 4.3.3 源码精读

**第一步：声明 in-tree 后端清单。**

[setup.py:375](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L375) —— `backends = [*BackendInstaller.copy(["nvidia", "amd", "tileir"]), *BackendInstaller.copy_externals()]`。注意 `tileir` 与 `nvidia`、`amd` 并列，地位完全相同——这就是「in-tree 后端」的本质。

**第二步：把后端「安装」到 triton 包路径下（用软链接）。**

[setup.py:94](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L94) —— `install_dir = .../python/triton/backends/<名字>`，计算出后端应落位的「目标路径」。

[setup.py:427-432](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L427-L432) —— `add_link_to_backends` 对每个后端调用 `update_symlink(install_dir, backend_dir)`，即把 `python/triton/backends/tileir` 建成指向 `third_party/tileir/backend` 的软链接。这就是为什么源码里 `python/triton/backends/` 只有 `__init__.py`、`compiler.py`、`driver.py`，没有 `tileir/` 子目录——它要等你 `pip install -e .` 之后才出现。

**第三步：生成运行期 entry point。**

[setup.py:517](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L517) —— `entry_points["triton.backends"] = [f"{b.name} = triton.backends.{b.name}" for b in backends]`，为每个后端生成一条 `tileir = triton.backends.tileir` 入口。Triton 运行期就是靠它「按名字发现后端」。

**第四步：CMake 编译 C++ Pass。**

[setup.py:284](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L284) —— `TRITON_CODEGEN_BACKENDS` 被设为 `nvidia;amd;tileir`，传给 CMake。

顶层 [CMakeLists.txt:320](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L320) —— `add_subdirectory(third_party/${CODEGEN_BACKEND})`，于是 `third_party/tileir/CMakeLists.txt` 被执行，`lib/` 下的 C++ Pass 被编译并链入 `triton_tileir.cc` 产生的 pybind 插件。

#### 4.3.4 代码实践：验证「软链接不在源码里」

1. **实践目标**：亲眼确认 `python/triton/backends/tileir/` 在源码树里不存在，并预测它会在哪一步、以什么形式出现。
2. **操作步骤**（只读）：
   ```bash
   ls -la python/triton/backends/
   ```
3. **需要观察的现象**：目录里只有 `__init__.py`、`compiler.py`、`driver.py` 三个普通文件，**没有** `nvidia/`、`amd/`、`tileir/` 子目录。
4. **预期结果**：结合 4.3.3 的源码，结论是——这三个子目录要等执行 `pip install -e .`（触发 `plugin_develop` → `add_link_to_backends`）之后，才以软链接形式出现，分别指向 `third_party/{nvidia,amd,tileir}/backend`。
5. 待本地验证：如果你已按 u1-l2 装过本仓库，可以在安装环境里再 `ls -la` 一次，应该能看到 `tileir -> ../../../../third_party/tileir/backend` 这样的软链。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `python/triton/backends/` 里不直接放 `tileir/` 的代码副本，而要用软链接？

> **答案**：因为代码的「真相源」是 `third_party/tileir/backend/`。软链接既能让 triton 包在运行期「看到」后端，又保证开发时只维护一份代码（改 `third_party/tileir/backend/compiler.py` 立即生效，无需同步副本）。可编辑安装 `pip install -e .` 尤其依赖这一点。

**练习 2**：如果删掉 `setup.py:375` 里的 `"tileir"` 字符串，会发生什么？

> **答案**：`tileir` 不再被列为 in-tree 后端 → 不会生成 `tileir = triton.backends.tileir` entry point，也不会建软链、不会 `add_subdirectory(third_party/tileir)`。结果：C++ Pass 不编译、Python 后端不被发现，`ENABLE_TILE=1` 时找不到 TileIRDriver。

**练习 3**：`TRITON_CODEGEN_BACKENDS`（setup.py 传给 CMake）和 `triton.backends` entry point 分别在哪个阶段起作用？

> **答案**：前者是**构建期**变量，决定 CMake 编译哪些后端的 C++（编译时）；后者是**运行期**入口，决定 `import triton` 后能按名字发现哪些后端（导入时）。两者覆盖同一组后端，但分工不同。

## 5. 综合实践

把本讲三节串起来，画一张「带分类标注的仓库目录树」。这是本讲的交付物，也是后续读源码时的速查表。

**任务**：在一张图里标出「后端 Python」「C++ 转换 Pass」「lit 测试」「构建脚本」四类文件的具体路径，并用不同记号区分「TileIR 新增」与「TileIR 修改的核心文件」。

**建议步骤**：

1. 先按 4.1，把 README ChangeList 点名的核心文件（`driver.py`/`compiler.py`/`jit.py`/`core.py`/`semantic.py`/`tensor_descriptor.py`）标为「✏️ 修改」。
2. 再按 4.2，把 `third_party/tileir/` 下的文件按四类归类标注（`backend/` = 后端 Python；`lib/`+`include/`+`triton_tileir.cc` = C++ 转换 Pass；`test/FileCheck/` = lit 测试；`CMakeLists.txt`+`scripts/` = 构建脚本）。
3. 最后按 4.3，用虚线画出构建期产生的软链：`python/triton/backends/tileir ⇢ third_party/tileir/backend`。

**参考答案树**（可直接对照自检）：

```text
Triton-to-tile-IR/
│
├─ python/triton/                      ✏️ 上游 Triton 核心（含 TileIR 局部修改）
│   ├─ runtime/driver.py               ✏️ 修改：ENABLE_TILE 切换 driver
│   ├─ runtime/jit.py                  ✏️ 修改：编译失败回退 PTX
│   ├─ compiler/compiler.py            ✏️ 修改：target.backend 改写
│   ├─ language/core.py                ✏️ 修改：host TMA 降级
│   ├─ language/semantic.py            ✏️ 修改：is_tileir() 分支
│   ├─ tools/tensor_descriptor.py      ✏️ 修改：descriptor 拆解
│   └─ backends/                       仅 __init__.py / compiler.py / driver.py
│        └─ tileir ⇢ (构建期软链) ──────┐
│                                      │
├─ lib/  include/  bin/  cmake/        上游 Triton 原生 C++（未被 TileIR 改动）
│                                      │
└─ third_party/                        ▼
    ├─ nvidia/   amd/   proton/        并列的 in-tree 后端（对照参考）
    └─ tileir/                           ★ TileIR 全部新增内容 ★
        ├─ backend/  {compiler,driver,conf,errors,code_generator}.py  【后端 Python】
        │          driver.c
        ├─ triton_tileir.cc            【C++ 转换 Pass】pybind 入口
        ├─ lib/  (TritonToTileIR/ Transform/ Utils/) 【C++ 转换 Pass】实现
        ├─ include/ (.h .td)           【C++ 转换 Pass】头文件 + TableGen
        ├─ test/FileCheck/  *.mlir     【lit 测试】
        ├─ tools/triton-cuda-tile-opt/ 独立 opt 工具
        ├─ scripts/  *.sh *.py         【构建脚本】cuda-tile 克隆/补丁
        └─ CMakeLists.txt              【构建脚本】子树 CMake 入口
```

> 说明：以上树是根据当前 HEAD（`1bd89c0dfb66fc99d4d338af4baddd2874de9d87`）的源码如实整理的；其中「构建期软链」一条需要你本地 `pip install -e .` 之后才能在文件系统里看到，待本地验证。

## 6. 本讲小结

- 顶层目录 = 上游 Triton 原生目录 + TileIR 增量；判断归属看路径前缀：`third_party/tileir/` 是新增，`python/triton/...` 里 README ChangeList 点名的是修改。
- TileIR 后端的所有新增内容集中在 `third_party/tileir/`，内部分四类：`backend/`（后端 Python）、`lib/`+`include/`+`triton_tileir.cc`（C++ 转换 Pass）、`test/FileCheck/`（lit 测试）、`CMakeLists.txt`+`scripts/`（构建脚本）。
- `setup.py:375` 把 `tileir` 与 `nvidia`、`amd` 并列为 in-tree 后端；`setup.py:517` 生成 `triton.backends` entry point 用于运行期发现。
- 源码树里找不到 `python/triton/backends/tileir/` 是正常的——它是构建期由 `setup.py:427-432` 建立的软链，指向 `third_party/tileir/backend`。
- C++ Pass 通过 `CMakeLists.txt:320` 的 `add_subdirectory(third_party/tileir)` 编译，再经 `triton_tileir.cc` 暴露成 Python 可调用模块。
- 本讲只读不写，记住一句口诀即可定位绝大多数文件：**「backend 跑、lib/include 转、test 验、scripts 建」**。

## 7. 下一步学习建议

认路完成后，下一讲 **u1-l4「端到端编译链路总览」** 会把这张地图「动」起来：顺着 `third_party/tileir/backend/compiler.py` 的 `add_stages`，走通 `make_ttir → make_tileir → make_cubin` 三段式流水线，并看清哪一步交给了 `triton_tileir.cc` 暴露的 C++ Pass、哪一步交给了外部 `tileiras`。

建议在进入 u1-l4 前，先随手做两件小事巩固本讲：

- 打开 `third_party/tileir/backend/compiler.py` 和 `third_party/tileir/triton_tileir.cc` 各扫一眼，对照本讲的分类标注，确认你能在几秒内说出它们分别属于哪一类。
- 翻一翻 `third_party/tileir/test/FileCheck/op-conversion.mlir`，感受一下 lit 测试「输入 IR + 预期输出」的样子，为第四单元（u4-l1）的 lit/FileCheck 专题埋个伏笔。
