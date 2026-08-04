# 安装与环境构建方式

## 1. 本讲目标

vLLM 不是「纯 Python」库，它在分发时会附带一批用 C++/CUDA/Rust 写成的本地扩展（`.so` 文件）。这意味着「安装 vLLM」其实分两层：装 Python 包，以及（在需要时）编译这些本地扩展。本讲学完后，你应当能够：

1. 看懂 `pyproject.toml` 里声明的项目元数据、构建后端、Python 版本范围与 CLI 入口脚本。
2. 理解 `setup.py` 是如何作为「编译总指挥」，根据检测到的设备（CUDA/ROCm/CPU/…）决定要编译哪些扩展。
3. 说出 `VLLM_USE_PRECOMPILED` 预编译模式跳过了哪些步骤、又是从哪里下载现成的 `.so`。
4. 了解 `CMakeLists.txt` 在编译流程中承担的实际职责（调用 `nvcc`/`clang` 编译内核）。
5. 按照 `AGENTS.md` 的规范，用 `uv` + `.venv` 建立开发环境并运行 `pre-commit`。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 项目定位与核心特性](u1-l1-project-overview.md)，知道 vLLM 是一个推理引擎、PagedAttention 等优化依赖底层高性能内核。本讲会用到下面几个概念，先用大白话解释：

- **Python 包（package）与 wheel**：Python 项目通常打包成 `xxx.whl` 文件再安装，里面主要是 `.py` 源码。
- **本地扩展（native extension / C extension）**：用 C/C++/CUDA 写、编译成 `.so`（Linux 共享库）的代码。Python 通过 `import` 把它当模块加载，从而获得「跑得快」的能力。vLLM 的大量 GPU 内核就是这样实现的。
- **构建后端（build backend）**：把源码变成可安装包的工具链。vLLM 用的是 `setuptools`。
- **CMake**：一个跨平台的「编译工程管理器」，负责决定调用哪个编译器、为哪些 GPU 架构编译、链接哪些库。
- **uv**：一个用 Rust 写的、极快的 Python 包管理器，vLLM 项目规定用它（而不是裸 `pip`）来管理环境。
- **stable ABI（SABI）**：Python 的「稳定二进制接口」。用 `USE_SABI 3.x` 编译出的扩展，不绑定具体 Python 小版本，能在 3.10–3.14 之间通用，所以 vLLM 只需要为每种设备编一份扩展。

一句话总结本讲的核心矛盾：**vLLM 想既好装、又跑得快**。好装意味着「别让用户编译 GPU 内核」，跑得快意味着「必须有 GPU 内核」。预编译机制（`VLLM_USE_PRECOMPILED`）就是用来化解这个矛盾的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | 项目的「身份证」：声明名字、Python 版本、构建依赖、CLI 入口、uv 配置等静态信息。 |
| `setup.py` | 编译流程的「总指挥」：检测设备、组装要编译的扩展列表、决定是否走预编译、最终调用 `setup()`。 |
| `CMakeLists.txt` | 真正的「编译脚本」：被 `setup.py` 调起的 CMake 工程，负责用 `nvcc`/`clang` 把 `.cu`/`.cpp` 编成 `.so`。 |
| `vllm/envs.py` | 集中定义所有 `VLLM_*` 环境变量的默认值与解析逻辑，`setup.py` 与运行时都从这里读。 |
| `tools/build_rust.py` | 声明 Rust 扩展（`vllm-rs` 前端、`_rust_tool_parser`）的构建入口，供 `setup.py` 调用。 |
| `AGENTS.md` | 项目对贡献者规定的开发流程：必须用 `uv`、必须装 `pre-commit`、预编译安装的具体命令。 |

## 4. 核心概念与源码讲解

### 4.1 pyproject.toml：项目元数据与构建入口

#### 4.1.1 概念说明

`pyproject.toml` 是现代 Python 打包的标准配置文件。它告诉打包工具三件事：

1. 这个项目叫什么、支持哪些 Python 版本、有哪些入口脚本（声明性元数据）。
2. 用什么构建后端去构建它（`[build-system]`）。
3. 各种开发工具（ruff、mypy、uv）怎么配置。

注意：vLLM 的**依赖列表是动态的**（`dynamic = ["dependencies"]`），并不直接写在 `pyproject.toml` 里，而是由 `setup.py` 在构建时根据检测到的设备（CUDA/ROCm/CPU）去读不同的 `requirements/*.txt` 文件。这是理解本讲的关键——同一份源码，在不同设备上装出来依赖不同。

#### 4.1.2 核心流程

`pyproject.toml` 在安装时的参与路径：

1. 打包工具读取 `[build-system]`，安装构建依赖（含 `cmake`、`setuptools-rust`、`torch==2.13.0`）。
2. 调用 `build-backend = "setuptools.build_meta"`，转入 `setup.py`。
3. `setup.py` 读取 `[project]` 里的静态元数据，再补充动态字段（版本、依赖、扩展）。
4. `[project.scripts]` 注册一个名为 `vllm` 的命令行入口，安装后终端里就能直接敲 `vllm ...`。

#### 4.1.3 源码精读

构建依赖里最值得关注的是 `setuptools-rust`（用来编译 Rust 扩展）和被钉死的 `torch == 2.13.0`（扩展要链接 PyTorch 的 C++ 头文件，版本必须对齐）：

[pyproject.toml:1-14](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L1-L14) —— 声明构建后端为 `setuptools.build_meta`，并固定 torch 等构建期依赖。

Python 版本范围与「动态字段」声明（依赖、版本都由 `setup.py` 在构建时动态提供）：

[pyproject.toml:35-36](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L35-L36) —— `requires-python = ">=3.10,<3.15"`，并且 `version`/`dependencies`/`optional-dependencies` 都是动态的。

CLI 入口脚本：安装后 `vllm` 这个命令实际指向 `vllm.entrypoints.cli.main:main` 函数（这是下一讲 [u2-l2](u2-l2-cli-and-serve.md) 的起点）：

[pyproject.toml:43-44](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L43-L44) —— 注册 `vllm` 命令行入口。

uv 相关配置：把 `torch` 标记为「不做构建隔离」，这样 uv 安装时能复用已经装好的 torch，而不是在隔离环境里重新拉一份：

[pyproject.toml:193-194](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L193-L194) —— `[tool.uv] no-build-isolation-package = ["torch"]`。

#### 4.1.4 代码实践

1. **实践目标**：在不安装的情况下，仅靠阅读 `pyproject.toml` 回答几个事实性问题。
2. **操作步骤**：打开 `pyproject.toml`，定位到上述四个代码段。
3. **需要观察的现象**：注意 `[build-system].requires` 里既有 `cmake`/`ninja`（编译工具），又有 `setuptools-rust`（Rust 扩展），还有 `torch == 2.13.0`（运行期依赖居然也进了构建期）。
4. **预期结果**：你能用一句话解释「为什么构建 vLLM 必须先有 torch」——因为编译扩展时要链接 PyTorch 的 C++ ABI，所以 torch 既是构建依赖又是运行依赖。
5. 待本地验证：若你本地已 clone 仓库，可用 `grep -n "torch ==" pyproject.toml` 自行核对版本号。

#### 4.1.5 小练习与答案

**练习 1**：`requires-python = ">=3.10,<3.15"` 表示支持哪些 Python 版本？
**答案**：Python 3.10、3.11、3.12、3.13、3.14（含 3.10、不含 3.15）。`pyproject.toml` 第 24–28 行的 classifiers 也列出了这五个版本，两边一致。

**练习 2**：为什么 `dependencies` 是 `dynamic` 而不是直接写在 `pyproject.toml`？
**答案**：因为 vLLM 在 CUDA、ROCm、CPU、TPU、XPU 上的依赖不同（CUDA 需要 `torch==2.13.0` 等，CPU 不需要）。`setup.py` 会在构建时根据检测到的设备读取不同的 `requirements/*.txt`，再动态填入依赖，这样一份源码能适配多种硬件。

---

### 4.2 setup.py：编译流程的总指挥

#### 4.2.1 概念说明

`setup.py` 是 setuptools 时代的构建脚本。虽然现代项目偏好把配置都搬进 `pyproject.toml`，但 vLLM 的构建逻辑太复杂（要检测设备、按架构编内核、支持预编译），所以仍保留 `setup.py` 来承担所有「动态决策」。

可以把 `setup.py` 想成一条流水线：**读环境变量 → 检测设备 → 决定要编译哪些扩展 → 决定是否走预编译 → 调用 `setup()` 打包**。

#### 4.2.2 核心流程

```text
加载 vllm/envs.py（拿到所有 VLLM_* 默认值）
        │
        ▼
检测 VLLM_TARGET_DEVICE（cuda / rocm / xpu / cpu / empty）
        │
        ▼
根据设备组装 ext_modules（一组 CMakeExtension）
        │
        ├── 走「正常编译」：cmdclass["build_ext"] = cmake_build_ext
        │       └─ 调起 CMake，用 nvcc/clang 编出 .so
        │
        └── 走「预编译」(VLLM_USE_PRECOMPILED)：cmdclass["build_ext"] = precompiled_build_ext
                └─ 从 wheels.vllm.ai 下载现成 wheel，解压出 .so，跳过编译
        │
        ▼
get_vllm_version()  —— 计算带后缀的版本号（+cu129 / +precompiled / +cpu …）
get_requirements()  —— 按设备读 requirements/*.txt
        │
        ▼
setup(...)  —— 把以上结果交给 setuptools 生成 wheel / 安装
```

#### 4.2.3 源码精读

`setup.py` 通过「从文件路径动态加载模块」的方式读取 `envs.py`，因为此时 `vllm` 包还没装、不能直接 `import vllm.envs`：

[setup.py:44-54](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L44-L54) —— 用 `load_module_from_path` 加载 `vllm/envs.py` 与 `tools/build_rust.py`，取出 `VLLM_TARGET_DEVICE` 和 `VLLM_USE_PRECOMPILED`。

设备自动检测：在 Linux 上根据 `torch.version.cuda/hip/xpu` 是否存在，自动判定为 `cuda`/`rocm`/`xpu`，否则 `cpu`：

[setup.py:81-103](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L81-L103) —— 设备自动探测逻辑，并打印 `Auto-detected CUDA` 等日志。

每个要编译的扩展都被封装成一个 `CMakeExtension`（注意它的 `sources=[]`，真正的源码列表在 CMake 那边）：

[setup.py:184-187](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L184-L187) —— `CMakeExtension` 只是一个「指向某个 CMakeLists 目录」的占位扩展，并设置 `py_limited_api`（stable ABI）。

`cmake_build_ext.build_extensions()` 是真正调起 CMake 的地方：先对每个扩展做 `configure`（传 `-DVLLM_TARGET_DEVICE`、`-DNVCC_THREADS` 等），再用 `cmake --build` 编译，最后 `cmake --install` 把 `.so` 摆到正确位置：

[setup.py:324-381](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L324-L381) —— 编译并安装扩展的主流程；configure 阶段在 [setup.py:239-322](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L239-L322)。

最关键的「分叉点」——根据是否预编译选择不同的 `build_ext` 命令类：

[setup.py:1243-1256](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L1243-L1256) —— `build_ext` 在 `precompiled_build_ext`（跳过编译）与 `cmake_build_ext`（实际编译）之间二选一。

版本号计算 `get_vllm_version()` 会在版本后追加设备相关后缀（如 `+cu129`、`+precompiled`、`+cpu`），这也是你在 `pip show vllm` 时看到的本地版本号的由来：

[setup.py:1018-1059](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L1018-L1059) —— 按设备给版本号加后缀；预编译模式会加 `+precompiled`（第 1033–1034 行）。

#### 4.2.4 代码实践

1. **实践目标**：在不运行安装的前提下，把 `setup.py` 的「编译决策」讲清楚。
2. **操作步骤**：
   - 打开 `setup.py` 第 1117–1186 行，观察 `ext_modules` 是如何按 `_is_cuda()` / `_is_cpu()` / `_is_hip()` 等条件一点点 append 出来的。
   - 再看第 1240–1256 行的 `cmdclass` 选择。
3. **需要观察的现象**：你会发现 CUDA 设备会 append 一长串扩展（`_C_stable_libtorch`、`_moe_C_stable_libtorch`、`cumem_allocator`、各种 `_flashmla_C`、`_deep_gemm_C`），而 CPU 设备只 append `_C` / `_C_AVX512` / `_C_AVX2`。
4. **预期结果**：你能解释「为什么在 CPU 机器上 `pip install` vLLM 编出的扩展比 GPU 机器少很多」——因为很多内核（如 FlashAttention、Marlin、CUTLASS scaled_mm）只对特定 GPU 架构有意义。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`setup.py` 为什么不直接 `import vllm.envs`，而要用 `load_module_from_path`？
**答案**：因为 `setup.py` 在「构建期」运行，此时 `vllm` 还没被安装（甚至可能还没编译出 `.so`），直接 import 会失败或触发循环导入。从文件路径加载 `envs.py` 这个纯 Python 模块，能在不依赖已安装包的前提下拿到环境变量默认值。

**练习 2**：`get_vllm_version()` 在预编译模式下会给版本加什么后缀？这个后缀有什么用？
**答案**：加 `+precompiled`（见第 1033–1034 行）。它用来区分「这份包是用预编译 `.so` 装的」还是「本地从源码编译的」，方便排查兼容性问题（预编译的 `.so` 是按官方 nightly wheel 的构建环境编的，可能与本地工具链不完全一致）。

---

### 4.3 VLLM_USE_PRECOMPILED：预编译安装

#### 4.3.1 概念说明

从源码编译 vLLM 的 GPU 扩展非常耗时（可能几十分钟到数小时，取决于 GPU 架构数量），而且需要正确版本的 CUDA toolkit、`nvcc` 等。这对只想用 vLLM 跑推理的用户很不友好。

预编译模式的思路很简单：**官方 CI 已经为各种 CUDA 版本和架构编好了 `.so`，打成 nightly wheel 放在 `wheels.vllm.ai`；本地构建时只下载这些 `.so`，跳过所有编译**。这也是 `AGENTS.md` 推荐给「只改 Python 代码」的贡献者的安装方式。

#### 4.3.2 核心流程

预编译模式的判定与执行：

```text
VLLM_USE_PRECOMPILED=1
        │
        ▼  envs.py: VLLM_USE_PRECOMPILED = True
（也会被 VLLM_PRECOMPILED_WHEEL_LOCATION 隐式置真）
        │
        ▼  setup.py: USE_PRECOMPILED_EXTENSIONS = True
        │
        ├─ cmdclass["build_ext"] = precompiled_build_ext   ← run()/build_extensions() 直接 return，不编译
        │
        └─ precompiled_wheel_utils.determine_wheel_url()   ← 决定从哪下载
                │
                │   1. VLLM_PRECOMPILED_WHEEL_LOCATION（用户指定本地/远程 wheel）
                │   2. 否则按系统 CUDA 版本选 variant（cu129 / cu130）
                │   3. 按 commit（默认取 main 分支 base commit）从 wheels.vllm.ai 取 metadata.json
                ▼
        extract_precompiled_and_patch_package()
                │  把 wheel 里的 vllm/_C.abi3.so、_C_stable_libtorch.abi3.so、vllm-rs 等
                │  解压到源码树对应位置，并补丁 package_data
                ▼
        最终 setup() 把这些现成的 .so 打进本地包
```

#### 4.3.3 源码精读

`precompiled_build_ext` 是一个「什么都不做」的命令类——它的 `run()` 和 `build_extensions()` 都是空实现，从而让 setuptools 跳过编译：

[setup.py:460-468](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L460-L468) —— 预编译模式下 `build_ext` 直接打印 `Skipping build_ext` 并返回。

`determine_wheel_url()` 按优先级决定下载哪个 wheel：用户显式指定 > 按 CUDA 版本自动选 variant > 默认 variant：

[setup.py:648-745](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L648-L745) —— 选择预编译 wheel 的 URL；它会调用 `detect_system_cuda_variant()` 把 CUDA 12 映射成 `cu129`、CUDA 13 映射成 `cu130`（见 [setup.py:538-579](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L538-L579)）。

`extract_precompiled_and_patch_package()` 里有一份「精确成员清单」，列出了要从 wheel 里抽出的所有 `.so` 文件——这就是预编译模式为你「省掉」的编译产物：

[setup.py:776-798](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L776-L798) —— 待提取的 `.so` 清单，包括 `vllm/_C_stable_libtorch.abi3.so`、`vllm/_moe_C_stable_libtorch.abi3.so`、`vllm/cumem_allocator.abi3.so`、FlashAttention 的 `_vllm_fa2_C`/`_vllm_fa3_C` 等，以及 Rust 前端 `vllm/vllm-rs`。

环境变量的解析集中在 `envs.py`，注意 `VLLM_USE_PRECOMPILED` 还会在设置了 `VLLM_PRECOMPILED_WHEEL_LOCATION` 时被隐式置真：

[vllm/envs.py:637-645](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/envs.py#L637-L645) —— `VLLM_USE_PRECOMPILED` 与 `VLLM_USE_PRECOMPILED_RUST` 的解析逻辑；默认值见 [vllm/envs.py:95-96](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/envs.py#L95-L96)。

#### 4.3.4 代码实践

1. **实践目标**：用预编译模式安装 vLLM，并解释它跳过了哪些步骤；若无网络/无 GPU，则改为源码阅读型实践。
2. **操作步骤（有网络与 CUDA 环境）**：
   ```bash
   uv venv --python 3.12
   source .venv/bin/activate
   VLLM_USE_PRECOMPILED=1 uv pip install -e . --torch-backend=auto
   ```
3. **操作步骤（离线阅读型）**：打开 [setup.py:776-798](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L776-L798) 的 `.so` 清单，逐个对照 4.4 节里 `CMakeLists.txt` 定义的扩展目标，理解「这些 `.so` 本来要靠 CMake 编出来」。
4. **需要观察的现象**：安装日志里应出现 `Skipping build_ext: using precompiled extensions.`，以及若干 `[extract] vllm/_C_stable_libtorch.abi3.so` 之类的行；不会出现 `nvcc` 调用。
5. **预期结果**：能说清楚预编译模式跳过了「CMake configure + nvcc 编译 + 链接 `.so`」这一整段，改为从远程 wheel 直接抽取现成 `.so`。
6. 待本地验证：是否真的能跑通取决于网络可达 `wheels.vllm.ai` 与本地 CUDA 版本是否被支持。

#### 4.3.5 小练习与答案

**练习 1**：除了显式设置 `VLLM_USE_PRECOMPILED=1`，还有什么办法能让 vLLM 走预编译？
**答案**：设置 `VLLM_PRECOMPILED_WHEEL_LOCATION` 指向一个本地或远程 wheel，也会让 `envs.py` 把 `VLLM_USE_PRECOMPILED` 置真（见 [vllm/envs.py:638-641](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/envs.py#L638-L641)）。另外 Docker 构建上下文里会用 `VLLM_DOCKER_BUILD_CONTEXT` 强制使用预编译产物。

**练习 2**：预编译下载的 `.so` 是按什么规则选择 CUDA 变体的？
**答案**：由 `detect_system_cuda_variant()` 决定（[setup.py:538-579](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L538-L579)）：优先看 `VLLM_MAIN_CUDA_VERSION`，其次 `torch.version.cuda`，再次 `nvidia-smi` 输出，最后回退默认值；CUDA 主版本 12 → `cu129`，13 → `cu130`。

---

### 4.4 CMakeLists.txt：C++/CUDA 扩展的实际编译

#### 4.4.1 概念说明

`setup.py` 只负责「要不要编译、编译哪些扩展」，真正「怎么编译」由 `CMakeLists.txt` 说了算。它是 CMake 工程的入口，定义了：

- 用什么 C++/CUDA 标准（vLLM 用 C++20 / CUDA 20）。
- 支持哪些 Python 版本、哪些 GPU 架构。
- 每个扩展（如 `_C_stable_libtorch`）由哪些 `.cu`/`.cpp` 源文件组成、链接哪些库。
- 用 `FetchContent` 从 GitHub 拉取第三方依赖（如 NVIDIA CUTLASS）。

如果你只是用 vLLM 跑推理，基本不用读这个文件；但若你要为新型号 GPU 加内核、或排查编译失败，就必须看懂它。

#### 4.4.2 核心流程

CMake 编译一个扩展的过程：

```text
cmake_build_ext.configure()
        │  传入 -DVLLM_TARGET_DEVICE / -DNVCC_THREADS / -DCMAKE_BUILD_TYPE 等
        ▼
CMakeLists.txt 解析
        │
        ├─ 选 GPU 语言（CUDA 或 HIP）与目标架构集合 CUDA_ARCHS
        ├─ 对每个 .cu 源文件，按它支持的架构 set_gencode_flags_for_srcs()
        ├─ FetchContent 拉取 cutlass 等依赖
        └─ define_extension_target(name, SOURCES ..., USE_SABI 3, WITH_SOABI)
                │
                ▼
cmake --build . --target _C_stable_libtorch ...
        │  调用 nvcc/clang 编译、链接
        ▼
产出 vllm/_C_stable_libtorch.abi3.so
```

为什么编译这么慢？因为每个 `.cu` 内核要为**每一个**目标 GPU 架构（如 7.5、8.0、8.6、9.0、10.0、12.0 …）单独生成一份机器码。架构越多，编译时间越长。设并行的并行度上限由 `MAX_JOBS` 与 `NVCC_THREADS` 控制（见 [setup.py:197-234](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L197-L234)）。

#### 4.4.3 源码精读

工程基本设置：要求 CMake ≥ 3.26，语言为 C++，标准 C++20，并定义 `VLLM_TARGET_DEVICE` 缓存变量（默认 `cuda`）：

[CMakeLists.txt:1-35](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L1-L35) —— 工程声明与设备变量；注意它要求 GCC ≥ 11.3，因为 PyTorch 的 C++20 头文件需要完整 C++20 支持。

支持的 Python 版本（需与 `pyproject.toml` 保持一致）：

[CMakeLists.txt:49](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L49) —— `PYTHON_SUPPORTED_VERSIONS` 列出 3.10–3.14。

按 `nvcc` 版本选择支持的 CUDA 架构集合（这是编译时间的最大变量）：

[CMakeLists.txt:114-132](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L114-L132) —— 不同 CUDA toolkit 版本对应的 `CUDA_SUPPORTED_ARCHS`；CUDA 13.4 起还会加入 Rubin(10.7) 等新架构。

最重要的扩展目标 `_C_stable_libtorch`：它把一大堆量化、注意力、采样、MoE 相关的 `.cu` 内核编进同一个 `.so`，并设置 stable ABI：

[CMakeLists.txt:1134-1145](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L1134-L1145) —— `define_extension_target(_C_stable_libtorch ...)`；它的源文件清单在上方 [CMakeLists.txt:398-428](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L398-L428) 一带逐步累加。

用 `FetchContent` 从 GitHub 拉 CUTLASS（NVIDIA 的高性能矩阵运算模板库），编译时按需 include：

[CMakeLists.txt:466-479](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L466-L479) —— 声明并拉取 cutlass，版本固定为 `v4.4.2`。

#### 4.4.4 代码实践

1. **实践目标**：在 `CMakeLists.txt` 里定位「一个具体的扩展目标由哪些源文件构成」，理解扩展与源码的对应关系。
2. **操作步骤**：
   - 在 `CMakeLists.txt` 中搜索 `define_extension_target`，找到 `_moe_C_stable_libtorch` 的定义（约第 1368 行）。
   - 往上找到 `VLLM_MOE_EXT_SRC` 变量，看它 append 了哪些 `csrc/libtorch_stable/moe/*.cu` 文件。
3. **需要观察的现象**：你会看到 MoE 相关内核（`topk_softmax_kernels.cu`、`moe_wna16.cu` 等）被集中编进 `_moe_C_stable_libtorch.abi3.so`，而通用内核编进 `_C_stable_libtorch.abi3.so`。
4. **预期结果**：能回答「`_moe_C_stable_libtorch.abi3.so` 这个文件里大概装了哪类内核」——混合专家（MoE）的 topk 路由与 wNa16 量化 GEMM 内核。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 vLLM 的 GPU 编译特别慢？有什么环境变量可以控制编译并行度？
**答案**：因为每个 `.cu` 内核要为多种 GPU 架构分别生成机器码，架构数 × 内核数 = 大量编译单元。`MAX_JOBS` 控制总体并行编译数（默认取 CPU 数），`NVCC_THREADS` 控制 `nvcc` 单进程内的线程数（设置后会相应调低 `MAX_JOBS` 以免过载，见 [setup.py:197-234](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L197-L234)）。

**练习 2**：`USE_SABI 3` 和 `WITH_SOABI` 在扩展目标里起什么作用？
**答案**：`USE_SABI 3` 让扩展按 Python 稳定 ABI（3.x）编译，从而一份 `.so` 可被 3.10–3.14 共用；`WITH_SOABI` 让文件名带上 ABI 后缀（如 `.abi3.so`）。这正是 [setup.py:776-798](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L776-L798) 里那些 `*.abi3.so` 文件名的由来。

---

### 4.5 uv venv 与开发流程规范

#### 4.5.1 概念说明

vLLM 的 `AGENTS.md`（项目对所有贡献者的强制规范）明确要求：**永远不要用系统 `python3` 或裸 `pip`/`pip install`**，一切 Python 命令都要走 `uv` 和 `.venv/bin/python`。这套规范保证每个人本地环境一致，也避免污染系统 Python。

`uv` 是一个用 Rust 写的极速包管理器，能替代 `pip`/`virtualenv`/`pip-tools` 等一串工具。`pre-commit` 则是在每次 `git commit` 前自动跑代码风格检查（ruff、mypy 等）的钩子。

#### 4.5.2 核心流程

从零搭建开发环境的标准步骤（来自 `AGENTS.md`）：

```text
1. 安装 uv
        curl -LsSf https://astral.sh/uv/install.sh | sh
2. 建立 3.12 虚拟环境并激活
        uv venv --python 3.12
        source .venv/bin/activate
3. 安装 lint 工具并装 pre-commit 钩子
        uv pip install -r requirements/lint.txt
        pre-commit install
4. 安装 vLLM（按是否改 C/C++ 二选一）
        ── 只改 Python：VLLM_USE_PRECOMPILED=1 uv pip install -e . --torch-backend=auto
        ── 也改 C/C++：    uv pip install -e . --torch-backend=auto
5. （可选）装测试依赖跑测试
        uv pip install -r requirements/test/cuda.in
        .venv/bin/python -m pytest tests/path/to/test_file.py -v
```

`-e .` 表示「可编辑安装」（editable），即把当前源码目录链接进环境，改完 `.py` 立即生效，不必重装。`--torch-backend=auto` 让 uv 自动选择合适的 torch 安装后端（CUDA/CPU）。

#### 4.5.3 源码精读

`requirements/` 目录按设备拆分依赖，`cuda.txt` 通过 `-r common.txt` 继承通用依赖，再追加 CUDA 专属依赖：

[requirements/cuda.txt:1-12](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/requirements/cuda.txt#L1-L12) —— `cuda.txt` 先 `-r common.txt`，再钉死 `torch==2.13.0`、`torchaudio==2.11.0` 等；`get_requirements()`（[setup.py:1062-1114](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L1062-L1114)）就是按设备选择读哪个文件。

`common.txt` 里是与设备无关的核心运行依赖（transformers、tokenizers、fastapi、pydantic、pyzmq 等）：

[requirements/common.txt:1-58](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/requirements/common.txt#L1-L58) —— 通用依赖清单；注意里面有 `pyzmq`（V1 进程间通信用，对应 [u3-l1](u3-l1-v1-process-architecture.md)）和 `setproctitle`（给各进程改名以便调试）。

> 说明：本节引用的 `AGENTS.md` 命令来自项目根目录的 `AGENTS.md`（即 `CLAUDE.md` 里 `@AGENTS.md` 引用的文件），它规定了上面的 uv / pre-commit / `VLLM_USE_PRECOMPILED=1` 安装命令。完整原文请直接阅读仓库中的 `AGENTS.md`「Environment setup」「Installing dependencies」「Tests」三节。

#### 4.5.4 代码实践

1. **实践目标**：用 `uv` 建立一个干净的 3.12 虚拟环境，并验证「激活后 python 指向 `.venv`」。
2. **操作步骤**：
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh   # 已装可跳过
   uv venv --python 3.12
   source .venv/bin/activate
   which python            # 应指向 .../.venv/bin/python
   python --version        # 应为 3.12.x
   ```
3. **需要观察的现象**：激活后命令行提示符出现 `(.venv)` 前缀，`which python` 不再指向系统 Python。
4. **预期结果**：你得到一个隔离的、与系统 Python 无关的开发环境，后续所有 `uv pip install` 都只影响这个 `.venv`。
5. 待本地验证：具体 3.12 小版本与网络下载是否成功取决于本机；若无法联网，可改为阅读 `AGENTS.md` 复述上述五步流程。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `AGENTS.md` 禁止用系统 `python3` 或裸 `pip`？
**答案**：为了保证所有贡献者在一致的、隔离的环境中工作，避免污染系统 Python、也避免「我这能跑你那不能跑」。`uv` 还能利用 `pyproject.toml` 里的 `[tool.uv]` 配置（如 `no-build-isolation-package = ["torch"]`）做更精细的构建控制。

**练习 2**：`VLLM_USE_PRECOMPILED=1 uv pip install -e .` 和 `uv pip install -e .`（不带预编译）的区别是什么？分别适用于什么场景？
**答案**：前者跳过本地编译、直接下载官方预编译 `.so`，速度快，适合「只改 Python 代码」的贡献者；后者在本地用 CMake/nvcc 真正编译扩展，耗时但能反映你改动的 C++/CUDA 代码效果，适合「修改了 `csrc/` 下内核」的贡献者。两者都用 `-e` 做可编辑安装。

## 5. 综合实践

把本讲的四个核心知识点（`pyproject.toml` 元数据、`setup.py` 编译决策、`VLLM_USE_PRECOMPILED` 预编译、`CMakeLists.txt` 实际编译）串起来，完成下面这个「安装方式对照」任务：

**任务**：假设你要在本机部署 vLLM 用于推理，但不确定该用哪种安装方式。请按下面的步骤排查并决策。

1. **看元数据**：打开 [pyproject.toml:35-36](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/pyproject.toml#L35-L36)，确认你的 Python 版本在 `>=3.10,<3.15` 范围内。
2. **看设备检测**：阅读 [setup.py:81-103](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L81-L103)，结合本机是否有 CUDA toolkit 与 `torch.version.cuda`，预测 `VLLM_TARGET_DEVICE` 会被自动判定成什么。
3. **选安装方式**：
   - 只想跑推理、有网络、CUDA 版本受支持 → 用 `VLLM_USE_PRECOMPILED=1`（对应 4.3 节），并指出它跳过了 4.4 节里 `define_extension_target` 那一整套编译。
   - 要改 C++/CUDA 内核 → 用普通 `uv pip install -e .`（对应 4.2 + 4.4 节），并说清它会调起 `cmake_build_ext` 编出 `_C_stable_libtorch.abi3.so` 等产物。
4. **建环境**：按 4.5 节的 `uv venv --python 3.12` 流程建立隔离环境。
5. **产出**：写一段话，说明「在你这台机器上，vLLM 安装会走哪条路径、最终在 `.venv/lib/python3.12/site-packages/vllm/` 下出现哪些 `.so` 文件、它们分别由哪个 `CMakeLists.txt` 目标产生」。若无法实际安装，则改为「源码阅读型」产出：对照 [setup.py:776-798](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L776-L798) 的清单与 [CMakeLists.txt:1134-1145](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/CMakeLists.txt#L1134-L1145) 的目标，把每个 `.so` 与其编译目标一一对应。

## 6. 本讲小结

- vLLM 不是纯 Python 库：它附带一批 C++/CUDA/Rust 编译出的 `.so` 扩展，安装分「装 Python 包」和「编译/获取扩展」两层。
- `pyproject.toml` 给出静态元数据与构建后端，依赖列表是**动态**的，由 `setup.py` 按设备读取不同 `requirements/*.txt` 决定。
- `setup.py` 是编译总指挥：检测 `VLLM_TARGET_DEVICE` → 组装 `ext_modules` → 在 `cmake_build_ext`（真编译）与 `precompiled_build_ext`（跳过编译）之间二选一。
- `VLLM_USE_PRECOMPILED=1` 会从 `wheels.vllm.ai` 下载官方预编译的 `.so`，跳过全部本地编译，是只改 Python 代码时的推荐安装方式。
- `CMakeLists.txt` 负责真正的编译：选 GPU 架构、为每个 `.cu` 设 gencode、用 `define_extension_target` 定义 `_C_stable_libtorch` 等扩展目标，并用 stable ABI 让一份 `.so` 跨 Python 版本通用。
- 项目强制使用 `uv` + `.venv` + `pre-commit` 管理开发环境，禁止系统 `python3` 与裸 `pip`。

## 7. 下一步学习建议

装好 vLLM 之后，下一步自然是「跑起来」。建议：

1. 学习 [u1-l3 仓库目录结构总览](u1-l3-repo-structure.md)，建立对 `vllm/` 包内各子目录（`entrypoints`、`v1`、`engine`、`model_executor` 等）的整体地图感，为后续源码阅读打基础。
2. 接着学 [u2-l1 离线推理：LLM 类与 generate/chat](u2-l1-offline-llm-class.md)，用 `vllm.LLM` 跑通你的第一次推理，验证本讲搭好的环境确实可用。
3. 如果你对构建链路本身感兴趣，可以继续精读 `setup.py` 的 `precompiled_wheel_utils`（[setup.py:501-935](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/setup.py#L501-L935)）和 `cmake/utils.cmake`，理解 wheel 元数据下载与 gencode 设置的细节。
