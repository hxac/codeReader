# 从源码构建、安装与运行测试

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 Triton 的三种安装方式（pip 预编译 wheel、源码 editable 安装、自定义 LLVM 安装），以及它们各自的适用场景。
- 理解 `pip install -e .` 背后到底发生了什么：它如何触发 `setup.py`、如何调用 CMake、又如何下载并校验一个固定版本的 LLVM。
- 读懂 `Makefile` 里的各个测试目标，知道 `make test` / `make test-nogpu` 分别跑什么、哪个需要 GPU、哪个不需要。
- 掌握 `MAX_JOBS`、`TRITON_BUILD_WITH_CCACHE`、`TRITON_BUILD_WITH_CLANG_LLD`、`--no-build-isolation` 等构建调优开关的作用，能在内存吃紧或重复构建时对症下药。
- 独立运行一个不依赖 GPU 的 lit 测试，验证本地工具链（`triton-opt` + `FileCheck`）可用。

本讲只解决「如何把 Triton 在本地装起来、如何验证装对了」这一个核心问题，是后续所有源码阅读和 kernel 实践的基础。

## 2. 前置知识

在进入正题前，先建立几个直觉。如果你已经学过 u1-l1，下面的概念会更容易接受。

- **Triton 是一个「编译器项目」而不仅是 Python 库。** 它的 Python 前端（`python/triton/`）只是冰山一角，真正干活的是用 C++/MLIR 写的编译器后端（`lib/`、`include/`），以及 NVIDIA/AMD 的硬件后端（`third_party/`）。因此「安装 Triton」天然包含两步：装 Python 包 + 编译 C++ 部分。
- **editable 安装（可编辑安装）。** `pip install -e .` 的意思是「把这个目录以『源码即安装』的方式装到当前 Python 环境里」。之后你修改 `python/triton/` 下的 `.py`，无需重装就能立即生效，非常适合边读源码边实验。它对应的 setuptools 命令是 `editable_wheel` / `develop`。
- **构建隔离（build isolation）。** 默认情况下，`pip install` 会临时建一个独立的环境来装构建工具（如 `setuptools`、`cmake`、`ninja`）。这能保证构建环境干净，但对 Triton 这种大型 C++ 项目，每次都用新环境会让 ninja 认为文件变了而频繁重链，所以官方建议加 `--no-build-isolation`。
- **MLIR 与 lit。** MLIR 是 LLVM 子项目，Triton 用它来定义自己的 IR 方言（TTIR/TTGIR）。`lit`（LLVM Integrated Tester）是 LLVM 生态的测试执行器，专门跑「带 `RUN:` 行」的文本测试，常配合 `FileCheck` 做模式匹配。这套测试不需要 GPU，是验证编译器改动的第一道关。
- **triton-opt。** 这是编译器自带的命令行驱动（源码在 `bin/triton-opt.cpp`），可以脱离 Python 运行时、独立地对一段 `.mlir` IR 跑 pass 管线。lit 测试主要就是调用它。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md) | 官方安装/构建/测试说明与调试开关一览，是本讲的「总纲」。 |
| [setup.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py) | Python 打包入口；定义了 `CMakeBuild`，把 CMake/Ninja 构建挂到 `pip install` 上。 |
| [python/build_helpers.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py) | 构建辅助库；负责解析构建目录、下载/校验 LLVM、下载 NVIDIA 工具链。 |
| [cmake/llvm-info.json](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json) | 锁定 Triton 所依赖的 LLVM 版本（`llvm_hash`）与各平台预编译包的 SHA256。 |
| [CMakeLists.txt](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/CMakeLists.txt) | CMake 顶层配置；声明构建开关、对接 `build_helpers.py` 解析三方依赖。 |
| [Makefile](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/Makefile) | 「开发常用命令的快捷方式」，封装了 `dev-install`、各种 `test-*` 目标。 |
| [test/lit.cfg.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py) | lit 测试套件的配置：注册 `triton-opt` 等工具、设置 `FileCheck`。 |
| [test/CMakeLists.txt](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/CMakeLists.txt) | 定义 `check-triton-lit-tests` 目标及其依赖。 |
| [AGENTS.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/AGENTS.md) | 面向贡献者的「构建与测试纪律」速查表，权威且简短。 |

记住一句话总纲：**`README` 给人看的步骤，`setup.py`+`build_helpers.py` 给机器执行的实现，`Makefile` 是两者之间的快捷封装，`AGENTS.md` 是踩坑后的纪律总结。**

## 4. 核心概念与源码讲解

### 4.1 安装方式

#### 4.1.1 概念说明

Triton 提供三条安装路径，按「省事程度」递减、按「可控程度」递增排列：

1. **pip 预编译 wheel**：`pip install triton`，直接下载官方编译好的二进制包。适合只「用」不「改」的人。
2. **源码 editable 安装**：克隆仓库后 `pip install -r python/requirements.txt` 再 `pip install -e .`。适合要读源码、改 Python、偶尔动 C++ 的开发者。本讲的默认路径。
3. **自定义 LLVM 安装**：自己从源码编译一份 LLVM，再让 Triton 链接它。适合要魔改 LLVM 或身处无法联网下载预编译包的内网环境的人。

关键区别在于：第 1 种完全跳过本地编译；第 2 种会本地编译 C++，但 LLVM 仍用官方预编译包；第 3 种连 LLVM 也自己编译。理解这条递进关系，就能根据报错快速定位问题出在「装 Python 包」「编译 C++」还是「找 LLVM」。

#### 4.1.2 核心流程

以最常用的「源码 editable 安装」为例，整体流程是：

```text
git clone triton
  └─ pip install -r python/requirements.txt     # 装构建工具: cmake, ninja, nanobind, lit ...
       └─ pip install -e .                       # 入口
            ├─ setup.py: get_packages()/get_package_dirs()  # 收集 Python 模块
            ├─ setup.py: CMakeBuild.build_extension()       # 触发 CMake 构建
            │     ├─ build_helpers.py: 下载并校验 LLVM      # (见 4.2)
            │     └─ cmake + ninja                          # 编译 lib/, third_party/
            ├─ setup.py: add_link_to_backends()             # 给 nvidia/amd 后端建符号链接
            └─ editable_wheel                              # 以源码形式注册到 site-packages
```

其中「装构建依赖」和「装 Triton 本体」被刻意分成两条命令，是因为 `requirements.txt` 里是构建时（build-time）依赖，而 `pip install -e .` 在构建隔离模式下会临时另起一个环境、看不到你刚装的 `cmake`/`ninja`。这也是后面为什么推荐 `--no-build-isolation` 的原因。

#### 4.1.3 源码精读

README 给出的官方步骤，是本模块的「黄金标准」，务必背下来：

[README.md:41-49](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L41-L49) — 源码安装的两条命令：先装 `python/requirements.txt` 构建依赖，再 `pip install -e .`。

构建时依赖清单很简短，全是「造轮子的工具」：

[python/requirements.txt](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/requirements.txt) — 固定版本的关键构建工具：`cmake>=3.20,<4.0`、`ninja>=1.11.1`、`nanobind==2.10.2`（用于生成 Python 绑定）、`lit`。

`setup.py` 末尾的 `setup(...)` 调用是打包的真正入口，注意两点：第一，扩展模块是一个 `CMakeExtension`，意味着「编译 C++」被伪装成了一个 Python 扩展；第二，版本号由 `TRITON_VERSION`（当前 `3.8.0`）加上 git 后缀动态拼出。

[setup.py:620-621](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L620-L621) — `ext_modules=[CMakeExtension("triton", "triton/_C/")]` 把 CMake 构建注册为名为 `triton._C` 的扩展。

[setup.py:574-575](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L574-L575) — 版本号 `3.8.0` 拼接 git 后缀，支持 Python 3.10–3.14。

而 editable 模式之所以能让「改源码即生效」，靠的是 `setup.py` 里覆盖的几个 setuptools 命令类，它们在真正构建前先给 `third_party` 下的后端目录建好符号链接：

[setup.py:496-500](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L496-L500) — `plugin_editable_wheel` 调用 `add_links(external_only=False)`，把 `nvidia`/`amd` 后端、`language/extra`、`tools/extra`、Proton 都以软链接挂到 `python/triton/` 下。

#### 4.1.4 代码实践

**实践目标**：在不实际破坏环境的前提下，搞清楚 editable 安装会注册哪些 Python 模块。

**操作步骤**（源码阅读型，无需真正安装）：

1. 在仓库根目录阅读 [setup.py:416-435](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L416-L435) 的 `get_packages()`，列出它 `yield` 出的包名。
2. 注意循环里除了 `find_packages(where="python")`，还会为每个后端 yield `triton.backends.<name>`，并在启用 Proton 时 yield `triton.profiler`。
3. 在 Python 里模拟「装好之后的导入视图」（示例代码，非项目原有）：

```python
# 示例代码：理解 editable 安装后，triton 包里会有哪些子模块
import importlib.util
# editable 安装会把 python/ 映射为 triton 包根，third_party/nvidia/backend 映射为 triton.backends.nvidia
expected = [
    "triton",                 # 来自 python/triton
    "triton.language",        # python/triton/language
    "triton.backends.nvidia", # 软链接到 third_party/nvidia/backend
    "triton.backends.amd",    # 软链接到 third_party/amd/backend
    "triton.profiler",        # 软链接到 third_party/proton/proton (默认启用)
]
for m in expected:
    print(m, "->", importlib.util.find_spec(m) is not None)
```

**需要观察的现象**：在已经 `pip install -e .` 的环境里，上面 `find_spec` 全部返回非 `None`；而只 `pip install triton`（预编译包）的环境里，`triton.backends.nvidia` 仍存在但指向 wheel 内的真实目录而非软链接。

**预期结果**：理解 editable 安装的本质是「一套精心安排的软链接 + 一个 CMake 扩展」，而非把文件复制进 `site-packages`。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方要求先 `pip install -r python/requirements.txt` 再 `pip install -e .`，而不是合并成一条？

> **参考答案**：`requirements.txt` 里是构建工具（cmake/ninja/nanobind/lit）。若直接 `pip install -e .` 且开启了构建隔离，pip 会临时另起一个隔离环境装这些工具，可能与你预期的版本不符；而且隔离环境的临时 cmake 路径每次都变，会让 ninja 误判文件改动而大量重链。所以官方推荐显式先装依赖，配合 `--no-build-isolation` 让 `pip install -e .` 复用这些工具。

**练习 2**：`setup.py` 里 `get_packages()` 对 `triton.backends.nvidia` 这样的包是怎么「凭空」生成的？它在文件系统里并不在 `python/` 下。

> **参考答案**：`nvidia`/`amd` 后端实际位于 `third_party/<name>/backend/`。`get_package_dirs()` 通过 `yield (f"triton.backends.{backend.name}", backend.backend_dir)` 把这个外部目录映射成 Python 包路径；`add_link_to_backends()` 再用软链接把 `third_party/nvidia/backend` 链接到 `python/triton/backends/nvidia`。于是对 Python 来说，它就像住在 `triton.backends.nvidia` 一样。

### 4.2 CMake/LLVM 构建

#### 4.2.1 概念说明

这一模块解决最容易被卡住的一步：**编译 C++/MLIR 部分**。Triton 用 LLVM 来为 GPU/CPU 生成机器码，而 **LLVM 没有稳定的 C++ API**——不同 commit 的 LLVM 不能随意混用。所以 Triton 必须锁定一个精确的 LLVM 版本，构建时要么下载官方为此版本预编译好的 LLVM，要么你自己编译同一版本的 LLVM。

这里的「版本」不是 `llvm-19` 这种粗粒度标签，而是一个 **40 位 git commit hash**（`llvm_hash`）。Triton 的 C++ 代码就是针对这个具体 commit 写的，差一个 commit 都可能编不过。理解了这一点，你就明白为什么 `cmake/llvm-info.json` 这么重要。

#### 4.2.2 核心流程

构建 C++ 部分的完整决策树如下：

```text
pip install -e .
  └─ setup.py: CMakeBuild.build_extension()
       ├─ 确定 build 目录: build_helpers.get_cmake_dir()
       │     └─ 默认 build/cmake.<platform>-<pyimpl>-<pyver>/，可用 TRITON_BUILD_DIR 覆盖
       ├─ build_helpers.write_thirdparty_cmake_vars(packages=["llvm","json"])
       │     ├─ 读 cmake/llvm-info.json -> llvm_hash[:8] + build_number + 平台后缀
       │     ├─ 拼出预编译包 URL: https://oaitriton.blob.core.windows.net/public/llvm-builds/<name>.tar.gz
       │     ├─ 若 LLVM_SYSPATH 已设 -> 用本地的，不下载
       │     ├─ 否则 -> 下载到 ~/.triton/llvm/，并校验 SHA256
       │     └─ 写出 LLVM_INCLUDE_DIRS / LLVM_LIBRARY_DIR 给 CMake
       ├─ cmake -G Ninja <args>           # 配置
       ├─ cmake --build . -j<MAX_JOBS>     # 编译
       └─ cmake --build . --target mlir-doc
```

平台后缀的选取逻辑（`ubuntu-x64` / `almalinux-x64` / `macos-arm64` 等）依据 glibc 版本和架构决定，目的是匹配 ABI 兼容的预编译包。

#### 4.2.3 源码精读

版本锁文件只有十几行，却是整个构建的「单一事实来源」：

[cmake/llvm-info.json:1-13](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json#L1-L13) — `llvm_hash` 锁定 LLVM 精确 commit，`sha256sum` 为每个平台预编译包提供校验值。当前 `llvm_hash` 为 `850a2b1b...`，即 Triton 会用 LLVM `850a2b1b`。

`build_helpers.py` 把这个 hash 翻译成下载 URL 与本地目录名：

[python/build_helpers.py:350-369](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py#L350-L369) — 读取 `llvm-info.json`，取 `llvm_hash[:8]` 与平台后缀拼出包名 `llvm-<rev>-<suffix>-<build_number>`，构造 blob 存储 URL，并附带 `sha256sum`。同时建一个不含版本号的稳定软链接 `llvm-<suffix>`。

下载后必须校验完整性，避免传输损坏或被替换：

[python/build_helpers.py:237-254](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py#L237-L254) — `_validate_sha256` 用 `expected_sha256` 校验下载包；不匹配会删档并抛错，除非显式设 `TRITON_UNSAFE_DISABLE_SHA_CHECK`。

构建目录的派生规则决定了你该去哪里找产物（如 `triton-opt`）：

[python/build_helpers.py:28-39](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py#L28-L39) — 默认 `build/cmake.<plat>-<impl>-<ver>/`，可用环境变量 `TRITON_BUILD_DIR` 覆盖。这也是 `Makefile` 里 `BUILD_DIR` 的来源。

`setup.py` 的 `CMakeBuild.build_extension` 是把 CMake 挂到 pip 上的「中枢」，最值得逐行读。它组装传给 CMake 的参数：

[setup.py:275-291](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L275-L291) — 核心 cmake 参数：`-G Ninja`、`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`（生成 `compile_commands.json` 供 IDE）、`-DTRITON_BUILD_PYTHON_MODULE=ON`、`-DTRITON_CODEGEN_BACKENDS=nvidia;amd`、`-DTRITON_CACHE_PATH=...`、`-DTRITON_VERSION=...`。

构建类型（Debug/Release/带断言的 Release）由环境变量决定：

[setup.py:129-140](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L129-L140) — `get_build_type()` 的优先级：`DEBUG` > `REL_WITH_DEB_INFO` > `TRITON_REL_BUILD_WITH_ASSERTS` > `TRITON_BUILD_WITH_O1` > 默认 `TritonRelBuildWithAsserts`。**默认就开断言**，这是为了在开发期能尽早暴露 C++ 假设错误。

`-j` 并发数由 `MAX_JOBS` 控制，这是控制内存用量的主阀门：

[setup.py:304-305](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L304-L305) — `max_jobs = os.getenv("MAX_JOBS", str(2 * os.cpu_count()))`，默认两倍核数；OOM 时调小。

一大批构建开关通过「透传」机制从环境变量进入 CMake，`TRITON_BUILD_WITH_CCACHE` 就在其中：

[setup.py:337-358](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L337-L358) — `passthrough_args` 列表，把 `TRITON_BUILD_WITH_CCACHE`、`TRITON_PARALLEL_LINK_JOBS`、`TRITON_OFFLINE_BUILD`、`LLVM_SYSPATH` 等环境变量转成 `-D<VAR>=<value>` 传给 CMake。

CMake 顶层声明了对应的 option，注意 `TRITON_BUILD_WITH_CCACHE` 默认就是 `ON`：

[CMakeLists.txt:14-22](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/CMakeLists.txt#L14-L22) — 关键 option：`TRITON_BUILD_PYTHON_MODULE`、`TRITON_BUILD_PROTON`(默认 ON)、`TRITON_BUILD_UT`(默认 ON)、`TRITON_BUILD_WITH_CCACHE`(默认 ON)、`TRITON_OFFLINE_BUILD`(默认 OFF)。

最后，如果你要走「自定义 LLVM」这条路，官方提供了一键脚本，它会从同一个 `llvm_hash` 拉源码并编译：

[scripts/build-llvm-project.sh:9](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/scripts/build-llvm-project.sh#L9) — 脚本用 `jq` 从 `cmake/llvm-info.json` 读取 `llvm_hash`，保证自编译的 LLVM 与 Triton 期望的版本完全一致。

#### 4.2.4 代码实践

**实践目标**：不实际触发构建，手动复现一遍「由 `llvm_hash` 得到预编译包 URL」的推导，验证你理解了版本锁机制。

**操作步骤**（纯源码阅读型）：

1. 打开 [cmake/llvm-info.json](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/cmake/llvm-info.json)，记下 `llvm_hash` 前 8 位（`850a2b1b`）与 `build_number`（`1`）。
2. 对照 [python/build_helpers.py:354-358](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py#L354-L358)，确认包名规则 `llvm-{rev}-{system_suffix}-{build_number}` 与 URL 前缀。
3. 判断你本机的 `system_suffix`：阅读 [python/build_helpers.py:313-349](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/build_helpers.py#L313-L349) 的平台分支（x86_64 Linux + glibc>2.28 → `ubuntu-x64`）。

**预期结果**（示例代码，非项目原有）：

```python
# 示例代码：手动复现 URL 推导
import json, platform
info = json.load(open("cmake/llvm-info.json"))
rev = info["llvm_hash"][:8]            # '850a2b1b'
build_number = info["build_number"]    # 1
suffix = "ubuntu-x64"                  # 假设你是 x86_64 + 较新 glibc
name = f"llvm-{rev}-{suffix}-{build_number}"
url = f"https://oaitriton.blob.core.windows.net/public/llvm-builds/{name}.tar.gz"
print(url)
# -> https://oaitriton.blob.core.windows.net/public/llvm-builds/llvm-850a2b1b-ubuntu-x64-1.tar.gz
print("expected sha256:", info["sha256sum"][suffix])
```

**需要观察的现象**：URL 与上方注释一致；SHA256 能在 `llvm-info.json` 的 `sha256sum` 字段里对上号。

> 注：若你本机架构/系统不在预编译覆盖范围内（如某些非 x86/arm 平台），上述函数会打印「proceeding with user-configured LLVM from source build」并返回空 URL，此时必须提供 `LLVM_SYSPATH`，否则构建无法下载 LLVM。这一点「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Triton 要用 40 位的 LLVM commit hash 而不是「LLVM 19.x」这种版本号？

> **参考答案**：LLVM 的 C++ API 在同一个 release 周期内也可能变动，且 Triton 直接依赖大量 MLIR 内部接口。用精确 commit hash 能保证 Triton 的 C++ 代码与 LLVM 的 API 严格匹配，避免「明明都是 19.x 却编不过」的玄学问题。

**练习 2**：下载的 LLVM 存在哪里？如何让构建复用已有的本地 LLVM 而不重新下载？

> **参考答案**：默认存在 `~/.triton/llvm/` 下（受 `TRITON_HOME` 影响），并以 `llvm-<suffix>` 软链接指向带版本号的目录。要复用本地 LLVM，设置环境变量 `LLVM_SYSPATH`（或 `LLVM_INCLUDE_DIRS`+`LLVM_LIBRARY_DIR`）指向你自编译的 LLVM 安装目录；`build_helpers.py` 检测到 `LLVM_SYSPATH` 后会跳过下载。完整流程见 `make dev-install-llvm`。

### 4.3 测试运行

#### 4.3.1 概念说明

Triton 有两套并行的测试体系，分工明确：

| 体系 | 位置 | 执行器 | 是否需要 GPU | 测什么 |
|------|------|--------|--------------|--------|
| **lit 测试** | `test/`（如 `test/Triton/*.mlir`） | `lit` + `FileCheck` + `triton-opt` | **不需要** | 编译器 pass 的 IR 变换是否正确 |
| **pytest 测试** | `python/test/`、`python/tutorials/` | `pytest`（+`xdist` 并行） | 大部分**需要** | 端到端 kernel 在真实硬件上的正确性与性能 |

lit 测试是「编译器开发者的快速反馈环」：每条 `RUN:` 行就是一条命令，`CHECK:` 行用模式匹配断言 IR 输出。它纯文本、无 GPU、毫秒级，是改 C++ pass 后的首选验证手段。pytest 测试则更重，需要先把 Triton 装好、有 GPU 才能跑，覆盖面更广（单元、回归、Gluon、Proton、microbenchmark）。

#### 4.3.2 核心流程

两类测试的触发路径：

```text
lit 测试（无 GPU）:
  make test-lit        # = ninja -C $BUILD_DIR check-triton-lit-tests
    └─ CMake 目标 check-triton-lit-tests (test/CMakeLists.txt)
         └─ 依赖: triton-opt, triton-tensor-layout, triton-llvm-opt
         └─ lit 读 test/lit.cfg.py，对每个 .mlir 执行 RUN 行
              └─ triton-opt foo.mlir | FileCheck foo.mlir

pytest 测试（多数需 GPU）:
  make test-unit       # python/test/unit 下，需 GPU
  make test-nogpu      # = test-lit + test-cpp + 两个前端 pytest，无 GPU
  make test            # = test-lit + test-cpp + 全套 python 测试，需 GPU
```

关键点：**`make test-nogpu` 是无 GPU 环境能跑到的最全测试组合**，它 = `test-lit`（lit）+ `test-cpp`（C++ 单测）+ 两个纯前端的 pytest。

#### 4.3.3 源码精读

Makefile 把常用命令封装得清清楚楚，先看测试目标的分层：

[Makefile:27-33](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/Makefile#L27-L33) — `test-lit` 与 `test-cpp` 分别跑 `check-triton-lit-tests` 与 `check-triton-unit-tests`，都是 ninja 目标。

[Makefile:82-88](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/Makefile#L82-L88) — `test-nogpu = test-lit + test-cpp + 两个前端 pytest`；`test = test-lit + test-cpp + test-python`（含全套 GPU pytest）。

lit 目标在 CMake 里这样定义，注意它的依赖列表——这就是为什么改了 `triton-opt` 后 lit 会自动重编：

[test/CMakeLists.txt:15-32](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/CMakeLists.txt#L15-L32) — `TRITON_TEST_DEPENDS = triton-opt; triton-tensor-layout; triton-llvm-opt`，`check-triton-lit-tests` 依赖它们，并指定 `FileCheck` 路径。

lit 配置文件声明了「哪些后缀算测试文件」以及「去哪找工具」：

[test/lit.cfg.py:20](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py#L20) — `config.suffixes = ['.mlir', '.ll']`，只有这两种后缀会被当作 lit 测试。

[test/lit.cfg.py:58-70](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py#L58-L70) — 注册 `triton-opt`、`triton-llvm-opt`、`mlir-translate`、`llc` 等工具，并把构建目录的 `bin/` 加入 `PATH`。

一个 lit 测试长什么样？第一行 `RUN:` 就是它的全部执行逻辑：

[test/Triton/ops.mlir:1](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/Triton/ops.mlir#L1) — `// RUN: triton-opt %s | FileCheck %s`：用 `triton-opt` 处理本文件，再用 `FileCheck` 对照本文件里的 `CHECK:` 行做断言。

最后看 pytest 的配置，理解它如何把 tutorials/examples 也纳入测试：

[pytest.ini:1-5](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/pytest.ini#L1-L5) — `python_files = test_*.py *_test.py tutorials/*.py examples/*.py`，所以 `python/tutorials/01-vector-add.py` 这类文件里的 `test_*` 函数也会被 pytest 收集。`markers = interpreter` 标注了「解释器模式可跑」的测试（无 GPU 时用得上）。

pytest 的依赖（含 `pytest-xdist` 做并行、`numpy`/`scipy` 做数值对照）单独列在测试需求文件里：

[python/test-requirements.txt](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/test-requirements.txt) — 测试期依赖：`pytest`、`pytest-xdist`、`numpy`、`scipy>=1.7.1`、`expecttest` 等。

#### 4.3.4 代码实践

**实践目标**：运行一个不依赖 GPU 的 lit 测试，确认本地编译出的 `triton-opt` 与 `FileCheck` 工具链正常工作。

**操作步骤**：

1. 确认已 `pip install -e .`，并找到 build 目录（见 4.2）：

```bash
PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())'
# 例如输出: /home/you/triton/build/cmake.linux-x86_64-cpython-3.12
```

2. 显式构建 `triton-opt`（首次会比较久）：

```bash
make triton-opt        # 等价于 ninja -C $BUILD_DIR triton-opt
```

3. 运行一个 lit 测试（参考 [AGENTS.md:8](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/AGENTS.md#L8) 的写法）：

```bash
cd "$(PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())')"
lit -v test/Triton/ops.mlir
```

**需要观察的现象**：lit 输出里出现 `Expected Passes: N` 且 `Unexpected Failures: 0`；展开的命令行包含 `triton-opt .../ops.mlir | FileCheck .../ops.mlir`。

**预期结果**：`ops.mlir` 全部通过，证明 `triton-opt` 能正确解析 TTIR 方言、`FileCheck` 能匹配输出。

> 若本机没有 GPU 或尚未完成完整构建，可只跑 `make test-lit`（lit 全集）或 `make test-nogpu`，都不需要 GPU。若你没有构建环境，此步「待本地验证」，但你可以阅读 [test/Triton/ops.mlir](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/Triton/ops.mlir) 理解每个 `CHECK` 期望匹配什么。

#### 4.3.5 小练习与答案

**练习 1**：`make test`、`make test-nogpu`、`make test-lit` 三者的包含关系是什么？

> **参考答案**：`make test = test-lit + test-cpp + test-python`（全套，需 GPU）；`make test-nogpu = test-lit + test-cpp + 两个前端 pytest`（无需 GPU）；`make test-lit` 仅跑 lit（`check-triton-lit-tests`，无需 GPU）。即 `test-lit ⊂ test-nogpu ⊂ test`，随包含关系递增逐步引入对 GPU 的依赖。

**练习 2**：为什么 lit 测试不需要 GPU，而 pytest 测试大多需要？

> **参考答案**：lit 测试用 `triton-opt` 直接在 IR 层验证 pass 的变换结果（输入输出都是 `.mlir` 文本），根本不启动 kernel、不碰硬件；而 pytest 测试大多是端到端的——编译 kernel、在真实 GPU 上启动、对照 PyTorch/NumPy 结果，所以需要 GPU。这也是为什么无 GPU 的贡献者主要靠 `test-lit` 验证编译器改动。

### 4.4 构建调优

#### 4.4.1 概念说明

Triton 的 C++ 部分体量大、编译久，首次全量构建动辄几十分钟。好在官方提供了一组「调优开关」，主要解决三类痛点：

- **太慢**：用 `ccache` 缓存、用 `clang+lld` 加速链接。
- **OOM**：用 `MAX_JOBS` 限制并发、用 `TRITON_PARALLEL_LINK_JOBS` 限制并行链接。
- **重复构建浪费**：用 `--no-build-isolation` 避免 ninja 因 cmake 路径变化而全量重链。

理解这些开关的本质：它们大多是「环境变量」，被 `setup.py` 的透传机制或 CMake 的 option 读取，最终变成 `-D` 参数或编译器标志。

#### 4.4.2 核心流程

构建调优开关的作用位置：

```text
pip install -e . [调优环境变量]
  ├─ TRITON_BUILD_WITH_CCACHE=1      -> CMake option (默认 ON) -> 启用 ccache
  ├─ TRITON_BUILD_WITH_CLANG_LLD=1   -> setup.py 设置 C/CXX 编译器为 clang, 链接器为 lld
  ├─ MAX_JOBS=N                      -> setup.py: build_args += ['-jN']
  ├─ TRITON_PARALLEL_LINK_JOBS=N     -> 透传给 CMake, 限制并行链接数
  ├─ --no-build-isolation (pip 选项) -> 复用当前环境的 cmake/ninja, 路径稳定
  ├─ TRITON_BUILD_DIR=...            -> build_helpers: 改变 build 目录位置
  └─ TRITON_HOME=...                 -> 改变 ~/.triton 缓存与下载目录
```

#### 4.4.3 源码精读

README 的「Tips for building」是这一模块的总览，务必熟读：

[README.md:117-135](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L117-L135) — 官方调优建议：`TRITON_BUILD_WITH_CLANG_LLD`（lld 链接更快）、`TRITON_BUILD_WITH_CCACHE`、`TRITON_HOME`（改缓存位置）、`MAX_JOBS`（限内存）、`--no-build-isolation`（让 nop 构建更快）。

[README.md:137-139](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L137-L139) — 构建会生成 `compile_commands.json`，供 VSCode/clangd 做 C++ 智能提示。

`clang+lld` 的实现在 `setup.py`，它直接改写 cmake 的编译器/链接器标志：

[setup.py:307-315](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L307-L315) — 设 `TRITON_BUILD_WITH_CLANG_LLD` 时，把 C/CXX 编译器设为 `clang`/`clang++`，链接器设为 `lld`，并加 `-fuse-ld=lld`。

并发与离线等开关走透传列表，ccache 与并行链接都在其中：

[setup.py:337-358](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L337-L358) — 透传列表里含 `TRITON_BUILD_WITH_CCACHE`、`TRITON_PARALLEL_LINK_JOBS`、`TRITON_OFFLINE_BUILD`。设了哪个环境变量，就被转成同名 `-D` 参数。

`TRITON_OFFLINE_BUILD` 专为无法联网的内网/沙箱环境设计，它会禁止任何下载：

[setup.py:150-163](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L150-L163) — `is_offline_build()` 读 `TRITON_OFFLINE_BUILD`；开启后所有依赖必须用 `*_SYSPATH` 显式提供，缺失即报错。

`--no-build-isolation` 的好处源自一个细节：构建隔离会让每次 pip 调用使用不同的 cmake 符号链接，迫使 ninja 重链大量 `.a`：

[README.md:133-135](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L133-L135) — 解释 `--no-build-isolation` 为何能让 nop（无改动）构建更快。

最后，`AGENTS.md` 给出的纪律值得单独记下——它规定了「什么时候该 `make`、什么时候不该」：

[AGENTS.md:4-5](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/AGENTS.md#L4-L5) — 改了 native/compiler 才需要 `make` 重建；只改 Python 或 `python/triton_kernels` 不要 `make`。

#### 4.4.4 代码实践

**实践目标**：为一次「内存友好且可缓存」的源码构建组合出正确的环境变量，并解释每个开关的依据。

**操作步骤**：

1. 假设你的机器内存有限（如 16GB），又希望重复构建尽量快。请组合下列开关（示例命令，需结合本机情况调整）：

```bash
# 示例代码：内存受限 + 启用缓存的构建命令
export TRITON_BUILD_WITH_CCACHE=true       # 透传给 CMake, 命中缓存 (默认就 ON, 这里显式声明)
export MAX_JOBS=4                          # 限制编译并发, 防 OOM (默认 2*核数)
export TRITON_PARALLEL_LINK_JOBS=2         # 链接最吃内存, 单独再限一道
pip install -e . --no-build-isolation -v   # 复用已装的 cmake/ninja, 路径稳定, verbose 看过程
```

2. 构建后确认 `ccache` 真的在工作（需要系统已装 ccache）：

```bash
ccache -s    # 查看命中/未命中统计；二次构建后 hits 应明显上升
```

3. 确认 build 目录与 `compile_commands.json` 已生成：

```bash
PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())'
ls "$(PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())')/compile_commands.json"
```

**需要观察的现象**：第二次 `pip install -e .`（无改动）应明显快于首次；`ccache -s` 的 `Hits` 增加；`compile_commands.json` 存在且可被 clangd 读取。

**预期结果**：理解「`MAX_JOBS` 控编译并发、`TRITON_PARALLEL_LINK_JOBS` 控链接并发、`ccache` 控跨构建缓存、`--no-build-isolation` 控单次构建内的稳定性」这四件事是正交的，可以组合使用。

> 是否真的能加速、加速多少「待本地验证」，取决于机器配置与已装工具（clang/lld/ccache 需自行安装）。

#### 4.4.5 小练习与答案

**练习 1**：构建时报「out of memory / killed」，应该先调哪个开关？为什么？

> **参考答案**：先调小 `MAX_JOBS`（如 `MAX_JOBS=4` 甚至更小）。因为 `MAX_JOBS` 控制 ninja 的编译并发，默认是 `2*核数`，在内存小的机器上多个 C++ 编译单元同时跑会爆内存。如果链接阶段才 OOM（通常更吃内存），再额外用 `TRITON_PARALLEL_LINK_JOBS` 限制并行链接数。

**练习 2**：`TRITON_BUILD_WITH_CCACHE` 在 CMake 层默认是 ON，但构建却没明显变快，可能的原因？

> **参考答案**：该 option 只是告诉 CMake「启用 ccache」，真正起作用要求系统里已安装 `ccache` 且在 `PATH` 中；若没装，开关形同虚设。另外，首次构建无缓存可命中，自然不会更快；只有重复构建、且源文件没大改时，ccache 的加速才明显。最后，若用了构建隔离且没加 `--no-build-isolation`，cmake 路径变化可能导致缓存键失效。

## 5. 综合实践

把四个模块串起来，完成一次「从零到验证」的端到端流程。如果你没有 GPU，可以全程用无 GPU 路径；如果有 GPU，再多走一步 GPU 测试。

**任务**：在本地从源码完成一次可编辑安装，找到构建产物，跑通一个 lit 测试，并用调优开关改善重复构建体验。

**步骤**：

1. **安装构建依赖与本体**（参考 [README.md:41-49](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md#L41-L49)）：

```bash
git clone https://github.com/triton-lang/triton.git && cd triton
pip install -r python/requirements.txt
TRITON_BUILD_WITH_CCACHE=true MAX_JOBS=4 pip install -e . --no-build-isolation -v
```

2. **记录 build 目录**，并验证 LLVM 下载与校验机制理解无误：

```bash
BUILD_DIR=$(PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())')
echo "$BUILD_DIR"                       # 记录构建产物位置
ls "$BUILD_DIR/bin/triton-opt"          # 编译出的命令行驱动
ls ~/.triton/llvm/ 2>/dev/null          # 下载并校验过的 LLVM (受 TRITON_HOME 影响)
```

3. **跑一个 lit 测试**（无需 GPU）：

```bash
make triton-opt
cd "$BUILD_DIR" && lit -v test/Triton/ops.mlir
# 或退而求其次: make test-nogpu
```

4. **（可选，有 GPU）跑一个 pytest**，验证端到端：

```bash
cd python/test/unit/language && pytest -s --tb=short test_core.py -k "test_add"
```

5. **画一张流程图**：把「`pip install -e .` → setup.py → CMakeBuild → build_helpers(下 LLVM) → cmake/ninja → triton-opt → lit` 这条链路手绘出来，并标注每一步对应的源码文件与行号。

**验收标准**：

- 能说出 build 目录的派生规则（`build/cmake.<plat>-<impl>-<ver>/`）。
- 能解释 `llvm_hash` 如何决定下载哪个预编译 LLVM 包。
- lit 测试至少有一个 `Expected Passes > 0` 且无失败。
- 能根据「OOM / 太慢 / 重复构建慢」三种症状分别给出对应开关。

## 6. 本讲小结

- Triton 的安装分三档：`pip install triton`（预编译）、`pip install -e .`（源码 editable）、自定义 LLVM；默认走第二档。
- `setup.py` 的 `CMakeBuild` 把 CMake/Ninja 构建伪装成 Python 扩展，editable 模式靠软链接把 `third_party` 后端挂到 `triton.backends.*`。
- C++ 部分依赖一个被 `cmake/llvm-info.json` 的 `llvm_hash` 精确锁定的 LLVM 版本；默认下载官方预编译包并做 SHA256 校验，也可用 `LLVM_SYSPATH` 指向自编译版本。
- 测试有两套：`test/` 下的 lit（无 GPU，验证 IR pass）和 `python/test/` 下的 pytest（多需 GPU，端到端）；`make test-nogpu` 是无 GPU 环境的最全组合。
- 关键调优开关：`MAX_JOBS`（限编译并发防 OOM）、`TRITON_PARALLEL_LINK_JOBS`（限链接并发）、`TRITON_BUILD_WITH_CCACHE`（缓存）、`--no-build-isolation`（稳定构建路径）。
- 改动纪律：动了 native/compiler 才需要 `make` 重建；只改 Python 不需要。

## 7. 下一步学习建议

本讲让你「装得起来、跑得通测试」，接下来建议：

1. **先做 u1-l3《仓库与目录结构地图》**，把 `python/`、`lib/`、`third_party/`、`test/` 四大块在脑子里定位清楚，再读具体源码就不迷路。
2. **接着做 u1-l4《编写并运行第一个 Triton kernel》**，亲手写一个 vector-add，用本讲装好的环境 + `TRITON_INTERPRET=1`（无需 GPU）跑起来，形成「安装 → 写 kernel → 看产物」的完整闭环。
3. **进阶时回看本讲**：当你开始改 C++ pass（u7/u10），会发现 `triton-opt`、lit 测试、`MLIR_ENABLE_DUMP` 是日常工具；届时再重读 4.3 与 4.4 会有更深体会。建议同时阅读 [AGENTS.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/AGENTS.md) 的全部内容。
