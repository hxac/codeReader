# 环境准备与安装构建

## 1. 本讲目标

本讲是「动手玩 tilelang-ascend」的第一步。学完之后，你应该能够：

1. 说出运行 tilelang-ascend 所需要的前置依赖（CANN、torch-npu）以及必须配置的环境变量（`ASCEND_HOME_PATH`）。
2. 区分 README 里给出的三条安装路径（直接装 wheel / 从源码构建 wheel / 从源码编译安装），并知道每条路径分别调用了哪个脚本。
3. 读懂 `build_wheel_ascend.sh` 与 `install_ascend.sh` 这两个脚本的执行流程。
4. 理解 `USE_ASCEND` 这个开关在 `CMakeLists.txt` 里如何决定「要不要把 Ascend 后端的 4 个源文件编进 `libtilelang.so`」。
5. 理解 `setup.py` 为什么要把 `catlass / pto-isa / shmem` 这三个子模块的头文件打进 wheel，以及这些头文件和 JIT（即时编译）的关系。

本讲承接 [u1-l1 项目定位与整体架构](./u1-l1-project-overview.md)：上一讲我们已经知道 tilelang-ascend 会把高层 DSL 经过多轮 lowering，最终生成 Ascend C / PTO 代码，再由毕昇编译器（bisheng）编成 `.so` 在 NPU 上跑。本讲就回答「这一整套东西到底怎么装到机器上、怎么编出来」。

## 2. 前置知识

- **CANN（Compute Architecture for Neural Networks）**：华为昇腾 NPU 的软件开发套件，相当于 NVIDIA 那边的 CUDA Toolkit。它提供编译器（包括毕昇 bisheng）、运行时库（`libruntime`、`libascendcl` 等）以及驱动接口。tilelang-ascend 在 JIT 阶段要调用 `bisheng`，在运行阶段要链接 `libruntime`，这些都来自 CANN。
- **torch-npu**：让 PyTorch 能在昇腾 NPU 上跑的适配层，作用类似 `torch` + CUDA。它不是编译 tilelang 本体所必需的，但是运行大多数 `examples/` 示例（用 `torch.randn` 在 NPU 上造张量）会用到。
- **wheel（`.whl`）**：Python 的标准二进制分发包。tilelang-ascend 的 wheel 里既有 Python 代码，也有预编译好的 `libtilelang.so`、`libtvm.so` 等 C++ 动态库，还有 JIT 阶段必须用到的 C++ 头文件目录。
- **JIT（Just-In-Time，即时编译）**：你写的 `@tilelang.jit` kernel 在**第一次被调用**时，才会被翻译成 Ascend C 代码、再用 bisheng 编译成 `.so` 并加载执行。这意味着即使你装好了 wheel，机器上依然需要有 CANN（提供 bisheng）和那些 C++ 头文件，否则 JIT 这一步会失败。
- **CMake / setuptools**：CMake 负责编译 C++ 部分（生成 `.so`），setuptools（`setup.py`）负责打包 Python wheel。tilelang-ascend 把这两者串起来：setuptools 在内部调用 CMake 完成 C++ 编译。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md) | 给出环境前提与三条安装路径的官方说明 |
| [install_ascend.sh](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh) | 「源码编译安装」路径的入口脚本（开发者常用） |
| [build_wheel_ascend.sh](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh) | 「从源码构建 wheel」路径的入口脚本 |
| [set_env.sh](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/set_env.sh) | 源码编译安装后，设置 `TL_ROOT` / `PYTHONPATH` 的小脚本 |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt) | C++ 部分的构建逻辑，含 `USE_ASCEND` 开关 |
| [setup.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py) | wheel 打包逻辑，负责把 C++ 库和头文件装进 wheel |
| [tilelang/env.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py) | 运行期自动探测 `ASCEND_HOME` 等路径的逻辑 |
| [tilelang/jit/adapter/libgen.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py) | JIT 阶段调用 bisheng 编译 kernel 的逻辑，能解释「为什么 wheel 里要带那些头文件」 |

后两个文件（`env.py`、`libgen.py`）虽然不在本讲的「最小模块」清单里，但它们是理解「为什么要这样打包」的关键证据，本讲会引用。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 前置依赖与三条安装路径**：CANN / torch-npu / 环境变量，以及 README 给出的三种安装方法。
- **4.2 构建脚本 `build_wheel_ascend.sh` 与 `install_ascend.sh`**：两个入口脚本的执行流程。
- **4.3 `CMakeLists.txt` 与 `USE_ASCEND` 开关**：C++ 后端如何按需编译。
- **4.4 `setup.py` 打包头文件与 JIT 的关系**：为什么 wheel 里要塞 `catlass/pto-isa/shmem` 的头文件。

---

### 4.1 前置依赖与三条安装路径

#### 4.1.1 概念说明

tilelang-ascend 不是一个「装完就能跑」的纯 Python 库。它有两类东西必须在**你的机器上**提前存在：

1. **CANN**：提供 bisheng 编译器、`libruntime` 等运行时库。没有它，JIT 这一步无法把生成的 Ascend C 代码编成 `.so`，运行时也找不到 NPU 驱动接口。
2. **torch-npu**：大多数示例脚本用 PyTorch 在 NPU 上造张量、对拍结果，所以运行示例需要它。

这两者都是华为官方的软件，需要你自行从 [hiascend](https://www.hiascend.com/) 下载安装。tilelang-ascend 的脚本**不会**帮你装 CANN，它假设 CANN 已经就绪，并通过环境变量 `ASCEND_HOME_PATH` 去找到它。

#### 4.1.2 核心流程

安装 tilelang-ascend 的总体流程：

```text
1. 先装好 CANN（>= 8.3.RC1）与 torch-npu（>= 2.6.0.RC1）
2. source CANN 自带的 set_env.sh，让 bisheng / libruntime 进入环境
3. 设置 export ASCEND_HOME_PATH=.../ascend-toolkit/latest
4. 选一条安装路径：
   路径 A（推荐）：pip install tilelang-*.whl        —— 直接用预编译 wheel
   路径 B：./build_wheel_ascend.sh 然后 pip install dist/*.whl —— 自己编 wheel
   路径 C：bash install_ascend.sh 然后 source set_env.sh —— 源码就地安装（开发用）
```

路径 A 最省事，路径 C 最适合二次开发（改完 C++ 源码立刻能 `import tilelang` 生效）。三条路径本质上都在做同一件事：产出可被 `import tilelang` 的 Python 包 + 编好的 `libtilelang.so`。

#### 4.1.3 源码精读

README 的「Installation」一节明确写出了版本要求和三条路径。环境前提在 [README.md:78-83](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L78-L83)：要求 CANN 至少 8.3.RC1、torch-npu 至少 2.6.0.RC1，并先 `source` CANN 的 `set_env.sh`。

三条安装方法见 [README.md:85-133](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L85-L133)：

- 方法 1（推荐）：`pip install tilelang-*.whl`，前提是先 `export ASCEND_HOME_PATH`。
- 方法 2：`git clone --recursive` 后 `./build_wheel_ascend.sh`，再 `pip install dist/tilelang-*.whl`。
- 方法 3：`bash install_ascend.sh` 后 `source set_env.sh`。

注意 `git clone --recursive`：tilelang-ascend 依赖若干 git 子模块（TVM、catlass、pto-isa、shmem 等，见 [.gitmodules](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.gitmodules)）。`--recursive` 就是为了把这些子模块一起拉下来，否则后续编译会缺源码。

那「`ASCEND_HOME_PATH` 到底会被谁用到」？答案是：编译期 `build_wheel_ascend.sh` 会显式校验它，运行期 `tilelang/env.py` 也会自动探测它。`tilelang/env.py` 里的探测逻辑在 [tilelang/env.py:53-64](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L53-L64)：先读环境变量 `ASCEND_HOME_PATH`，再读 `ASCEND_HOME`，都没有就回退到 `/usr/local/Ascend/ascend-toolkit/latest`。这解释了为什么 README 反复强调要 `export ASCEND_HOME_PATH`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚你的机器上 CANN 装在哪里，以及 tilelang 会从哪里读它。
2. **操作步骤**：
   - 在终端执行 `echo $ASCEND_HOME_PATH`，看是否已设置；若没有，参考 README 设置。
   - 打开 [tilelang/env.py:53-64](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L53-L64)，确认探测顺序是 `ASCEND_HOME_PATH` → `ASCEND_HOME` → 默认路径。
3. **需要观察的现象**：`ASCEND_HOME_PATH` 指向的目录下应存在 `include/`、`lib64/`、`opp/version.info` 等子项。
4. **预期结果**：若三个位置都没有 CANN，运行期会在 `setup.py` 校验阶段（见 4.4）或 JIT 阶段报「ASCEND_HOME is not set or detected」之类的错误。
5. 本步骤不执行编译命令，属于阅读理解型实践，无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：README 要求 CANN 版本至少是多少？torch-npu 呢？

> **答案**：CANN 至少 [8.3.RC1](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L79)，torch-npu 至少 2.6.0.RC1。

**练习 2**：`tilelang/env.py` 中 `_find_ascend_home()` 的探测顺序是怎样的？

> **答案**：先读环境变量 `ASCEND_HOME_PATH`，再读 `ASCEND_HOME`，最后回退到 `/usr/local/Ascend/ascend-toolkit/latest`（见 [tilelang/env.py:53-64](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L53-L64)）。

**练习 3**：为什么 `git clone` 时要带 `--recursive`？

> **答案**：tilelang-ascend 通过 git 子模块依赖 TVM、catlass、pto-isa、shmem 等（见 `.gitmodules`）。`--recursive` 把它们一并拉下来，否则编译期会缺 TVM 源码、JIT 期会缺头文件。

---

### 4.2 构建脚本 `build_wheel_ascend.sh` 与 `install_ascend.sh`

#### 4.2.1 概念说明

`build_wheel_ascend.sh` 和 `install_ascend.sh` 是两条「从源码出发」的入口脚本，区别在于产物形态：

- `build_wheel_ascend.sh`：产物是一个 `dist/tilelang-*.whl` 文件，**可分发**，之后用 `pip install` 装到任意机器（前提是目标机有相同 CANN）。
- `install_ascend.sh`：产物是**就地**编译好的 `libtilelang.so` + `tilelang/` Python 目录，配合 `source set_env.sh` 直接让当前源码树变成「可 import 的包」，适合反复改 C++ 源码的开发场景。

两个脚本都接受 `--enable-llvm` 选项（让 TVM 用 LLVM 后端，通常用于 CPU 侧代码生成，Ascend 场景一般不需要）。

#### 4.2.2 核心流程

`build_wheel_ascend.sh` 的流程：

```text
1. 校验 ASCEND_HOME_PATH 已设置（否则直接退出）
2. export USE_ASCEND=true（关键！传给 setup.py）
3. 校验 Python >= 3.10
4. pip install requirements-build.txt + requirements.txt
5. git submodule update --init --recursive
6. 应用 3rdparty/patches/apply_tvm_patches.sh（给 tvm 子模块打本地补丁）
7. 清理旧的 dist/build/egg-info
8. python setup.py bdist_wheel  —— 交给 setup.py（内部再调 CMake）
9. 产物在 dist/tilelang-*.whl
```

`install_ascend.sh` 的流程更接近「手动 CMake」：

```text
1. 解析命令行选项（--enable-llvm/--enable-shmem/--enable-incremental/--enable-coverage）
2. 校验 Python >= 3.10
3. pip install requirements
4. git submodule update --init --recursive + 应用 tvm 补丁
5. 准备 build/ 目录，把 3rdparty/tvm/cmake/config.cmake 复制进去
6. 向 config.cmake 追加 set(USE_ASCEND ON) / set(USE_GTEST OFF)
7. cmake .. && make -j（用 50% 核数，防止 OOM）
8. （可选）编译 shmem 包
```

两个脚本共享一个关键步骤：**应用 `apply_tvm_patches.sh` 给 3rdparty/tvm 打补丁**。这些补丁是 tilelang-ascend 自己维护、暂时无法合并进所 pin 的 tvm 子模块提交点的修复（例如 issue #1207 的 dynamic-slice 支持）。这个步骤是幂等的，重复运行安全。

#### 4.2.3 源码精读

**`build_wheel_ascend.sh` 的 `ASCEND_HOME_PATH` 校验**见 [build_wheel_ascend.sh:29-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L29-L35)：若未设置则打印提示并 `exit 1`。这是为什么 README 反复强调要先 `export ASCEND_HOME_PATH`。

紧接着它在 [build_wheel_ascend.sh:41-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L41-L42) 把开关导出给 setup.py：

```bash
export USE_ASCEND=true
export USE_LLVM=$USE_LLVM
```

这一行 `USE_ASCEND=true` 是全流程的「总开关」，它会被 `setup.py` 读到（见 4.4），最终决定 CMake 是否编译 Ascend 后端。

脚本在打补丁时有详细注释，见 [build_wheel_ascend.sh:63-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L63-L66)：明确说明这一步和 `install_ascend.sh`、`setup.py` 共用同一个 `apply_tvm_patches.sh`，保证三条路径打到的补丁一致。补丁脚本本身的幂等与失败即报错行为见 [3rdparty/patches/apply_tvm_patches.sh:37-50](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/3rdparty/patches/apply_tvm_patches.sh#L37-L50)。

**`install_ascend.sh` 的 Python 版本校验**见 [install_ascend.sh:37-45](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L37-L45)：要求 Python >= 3.10。它支持的四个选项声明在 [install_ascend.sh:7-10](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L7-L10)。

`install_ascend.sh` 把开关写进 TVM 的 config.cmake，见 [install_ascend.sh:209-211](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L209-L211)：

```bash
if ! $INCREMENTAL_BUILD; then
    echo "set(USE_ASCEND ON)" >> config.cmake
    echo 'set(USE_GTEST OFF)' >> config.cmake
```

这里的 `set(USE_ASCEND ON)` 会被 tilelang 的 `CMakeLists.txt` 通过 `include(config.cmake)` 读到（见 4.3）。`make` 用 50% 核数的策略见 [install_ascend.sh:231-236](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L231-L236)，注释解释是为了避免把机器跑卡。

> 关于 `set_env.sh`：根目录的 [set_env.sh](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/set_env.sh) 很短，见 [set_env.sh:3-8](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/set_env.sh#L3-L8)，它只是把 `TL_ROOT` 指向源码根、把根加入 `PYTHONPATH`，并设置 `ACL_OP_INIT_MODE=1`（避免 torch_npu 触发 tvm 的重复初始化）。路径 C 跑完 `install_ascend.sh` 后 `source set_env.sh` 就能用源码树里的 tilelang 了。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解两个脚本对 `ASCEND_HOME_PATH` 和 `USE_ASCEND` 的不同处理方式。
2. **操作步骤**：
   - 阅读并对比 [build_wheel_ascend.sh:29-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L29-L42) 与 [install_ascend.sh:207-219](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L207-L219)。
3. **需要观察的现象**：`build_wheel_ascend.sh` 通过环境变量 `USE_ASCEND=true` 把开关传给 `setup.py`；而 `install_ascend.sh` 直接把 `set(USE_ASCEND ON)` 写进 `config.cmake`。两条路径用了**不同的传递机制**，但最终都让 CMake 看到同一个 `USE_ASCEND` 变量。
4. **预期结果**：能在两张纸上分别画出两个脚本里 `USE_ASCEND` 的传递链路。
5. 本步骤无需硬件，纯阅读。

#### 4.2.5 小练习与答案

**练习 1**：`build_wheel_ascend.sh` 如果发现 `ASCEND_HOME_PATH` 未设置会怎样？

> **答案**：打印错误提示并 `exit 1`，直接终止构建（见 [build_wheel_ascend.sh:29-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L29-L35)）。

**练习 2**：`install_ascend.sh` 支持哪些命令行选项？其中哪个是用来复用已有 build 目录、避免全量重编的？

> **答案**：支持 `--enable-llvm`、`--enable-shmem`、`--enable-incremental`、`--enable-coverage`（见 [install_ascend.sh:7-10](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L7-L10)）；`--enable-incremental` 用于增量编译（见 [install_ascend.sh:191-205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/install_ascend.sh#L191-L205)）。

**练习 3**：两个脚本都会调用 `apply_tvm_patches.sh`，这个步骤如果失败会怎样？

> **答案**：补丁脚本对无法应用的补丁会 `exit 1`（见 [apply_tvm_patches.sh:45-49](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/3rdparty/patches/apply_tvm_patches.sh#L45-L49)），避免「悄悄编出一个没打补丁的 TVM」。

---

### 4.3 `CMakeLists.txt` 与 `USE_ASCEND` 开关

#### 4.3.1 概念说明

`CMakeLists.txt` 是 C++ 部分的构建脚本，它的核心产物是 `libtilelang.so`（运行期被 Python 通过 ctypes/cython 加载）。tilelang-ascend 是 tile-lang 的多后端变体，同一份 `CMakeLists.txt` 既能编 CUDA、ROCm，也能编 Ascend。具体编哪个后端，由一组 `USE_*` 变量控制，Ascend 对应的就是 `USE_ASCEND`。

关键点：`USE_ASCEND` 决定了**是否把 Ascend 后端的 4 个源文件**（两个 codegen + 两个 runtime module）编进 `libtilelang.so`。如果不开，编译出来的库就**没有** Ascend codegen 能力，JIT 时无法生成 Ascend 代码。

#### 4.3.2 核心流程

CMake 构建的关键链路：

```text
1. CMakeLists.txt 顶部 include(${CMAKE_BINARY_DIR}/config.cmake)
   —— 把 build/config.cmake 里 set(USE_ASCEND ON) 引进来，于是 USE_ASCEND 变量被定义
2. 把 TVM 作为子项目加入（add_subdirectory(3rdparty/tvm)），因为 tilelang 依赖 TVM 的 IR
3. 基础源文件：glob src/*.cc、src/transform/*.cc、src/op/*.cc、src/layout/*.cc ...（所有后端共享）
4. if(USE_ASCEND)：再 glob 进 src/target/codegen_ascend.cc、codegen_ascend_pto.cc、
                               src/target/rt_mod_ascend.cc、rt_mod_ascend_pto.cc
5. add_library(tilelang_objs OBJECT ...) → 组装成 libtilelang.so，链接 tvm_runtime
```

也就是说，`USE_ASCEND` 是一个「按需拉入 Ascend 源文件」的条件编译开关。

#### 4.3.3 源码精读

`CMakeLists.txt` 在顶部通过 `include` 读入 config.cmake，见 [CMakeLists.txt:30-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L30-L35)：

```cmake
  if(EXISTS ${CMAKE_BINARY_DIR}/config.cmake)
    include(${CMAKE_BINARY_DIR}/config.cmake)
  elseif(EXISTS ${CMAKE_SOURCE_DIR}/config.cmake)
    include(${CMAKE_SOURCE_DIR}/config.cmake)
  endif()
```

这就是为什么 `install_ascend.sh` 往 `build/config.cmake` 写 `set(USE_ASCEND ON)` 能生效——CMake 会把它 include 进来。

`USE_ASCEND` 控制源文件拉入的核心代码见 [CMakeLists.txt:130-138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L130-L138)：

```cmake
if(USE_ASCEND)
  tilelang_file_glob(GLOB TILE_LANG_ASCEND_SRCS
    src/target/codegen_ascend.cc
    src/target/codegen_ascend_pto.cc
    src/target/rt_mod_ascend.cc
    src/target/rt_mod_ascend_pto.cc
  )
  list(APPEND TILE_LANG_SRCS ${TILE_LANG_ASCEND_SRCS})
endif()
```

可以看到，只有 `USE_ASCEND` 为真时，这 4 个 Ascend 专属文件才会被加入编译列表。它们正是后续 [u6-l2 双 Codegen](./u6-l2-dual-codegen.md) 会讲到的两条 codegen 路线（`ascendc` 与 `pto`）与对应运行时模块。

最终这些源文件被组装成共享库，见 [CMakeLists.txt:152](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L152)（`add_library(tilelang_objs OBJECT ...)`) 与 [CMakeLists.txt:210-211](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L210-L211)（`add_library(tilelang SHARED ...)` 并链接 `tvm_runtime`）。TVM 作为依赖被加入子项目见 [CMakeLists.txt:102-105](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L102-L105)。

> 对照：CUDA 的等价开关是 `USE_CUDA`（见 [CMakeLists.txt:121-128](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L121-L128)），ROCm 是 `USE_ROCM`（[CMakeLists.txt:141-147](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L141-L147)）。三者结构完全对称，理解了 Ascend 就理解了另两个。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：确认「关掉 `USE_ASCEND` 会少编哪些文件」。
2. **操作步骤**：
   - 打开 [CMakeLists.txt:107-147](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L107-L147)，分别列出 `USE_CUDA`、`USE_ASCEND`、`USE_ROCM` 各自拉入的源文件。
3. **需要观察的现象**：三个 `if(USE_*)` 块结构对称，每个块只拉入该后端专属的 `codegen_*.cc` 和 `rt_mod_*.cc`。
4. **预期结果**：你能说出「Ascend 后端 = 这 4 个文件」。
5. 无需硬件，纯阅读。

#### 4.3.5 小练习与答案

**练习 1**：`USE_ASCEND` 为 ON 时，会有哪 4 个 `.cc` 文件被加入编译？

> **答案**：`src/target/codegen_ascend.cc`、`src/target/codegen_ascend_pto.cc`、`src/target/rt_mod_ascend.cc`、`src/target/rt_mod_ascend_pto.cc`（见 [CMakeLists.txt:130-138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L130-L138)）。

**练习 2**：为什么 `install_ascend.sh` 写到 `build/config.cmake` 里的 `set(USE_ASCEND ON)` 能被 `CMakeLists.txt` 读到？

> **答案**：因为 `CMakeLists.txt` 顶部会 `include(${CMAKE_BINARY_DIR}/config.cmake)`（见 [CMakeLists.txt:30-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L30-L35)），而 `CMAKE_BINARY_DIR` 正是 `build/` 目录。

**练习 3**：如果有人误把 `libtilelang.so` 用「`USE_ASCEND=OFF`」编出来，JIT 一个 Ascend kernel 时会发生什么？

> **答案**：库里没有 Ascend codegen 和 runtime 模块，JIT 阶段无法把 TIR 翻译成 Ascend 代码 / 找不到 `target.build.tilelang_ascend` 等注册项，编译会失败。

---

### 4.4 `setup.py` 打包头文件与 JIT 的关系

#### 4.4.1 概念说明

`setup.py` 负责「把什么装进 wheel」。对 tilelang-ascend 来说，wheel 里除了 Python 代码和 `libtilelang.so`，还必须带上一批 C++ **头文件目录**：`3rdparty/catlass/include`、`3rdparty/shmem/include`、`3rdparty/shmem/src/device`、`3rdparty/pto-isa/include`。

为什么要带头文件？因为 tilelang-ascend 是 **JIT** 的：kernel 第一次运行时，框架会把生成的 Ascend C 代码交给 `bisheng` 编译成 `.so`，而这份生成的代码会 `#include` 来自 catlass / shmem / pto-isa 的模板头文件（这些是 AscendC/PTO 的 C++ 模板库）。所以哪怕用户只是 `pip install` 了一个 wheel，机器上也得有这些头文件，否则 JIT 时 bisheng 会报「找不到头文件」。

#### 4.4.2 核心流程

`setup.py` 的关键逻辑链：

```text
1. 读 USE_ASCEND 环境变量（build_wheel_ascend.sh 已 export USE_ASCEND=true）
2. 若 USE_ASCEND=true 但找不到 ASCEND_HOME，直接 raise ValueError
3. 自定义 build_ext（CMakeBuild）：在内部调用 cmake + make 编出 libtilelang.so 等动态库
4. 自定义 build_py（TileLangBuilPydCommand）：
   - 把编好的 libtvm*.so / libtilelang*.so 拷进 wheel 的 tilelang/lib/
   - 把 src/tl_templates（C++ 模板库）拷进 wheel
   - 把 3rdparty/catlass/include、shmem/*、pto-isa/include 拷进 wheel  ← 关键
5. python setup.py bdist_wheel 产物 dist/tilelang-*.whl 即包含上述全部
```

JIT 阶段（`tilelang/jit/adapter/libgen.py`）会反过来通过 `TL_ROOT` 找到这些被装进包里的头文件目录，作为 `-I` 参数传给 bisheng。这就形成了「打包 → 安装 → JIT」的闭环。

#### 4.4.3 源码精读

`setup.py` 先读环境变量，见 [setup.py:39](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L39)：

```python
USE_ASCEND = os.environ.get("USE_ASCEND", "False").lower() == "true"
```

然后做存在性校验，见 [setup.py:61-62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L61-L62)：`USE_ASCEND` 开了却探测不到 `ASCEND_HOME`，就 `raise ValueError`。这正是「装 wheel 前必须先 `export ASCEND_HOME_PATH`」的代码依据。

**把头文件拷进 wheel 的三段代码**，是本讲最关键的部分。catlass 见 [setup.py:534-548](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L534-L548)（`CATLASS_PREBUILD_ITEMS = ["3rdparty/catlass/include"]`）；shmem 见 [setup.py:550-565](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L550-L565)（包含 `3rdparty/shmem/include` 与 `3rdparty/shmem/src/device`）；pto-isa 见 [setup.py:567-581](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L567-L581)（`PTO_ISA_PREBUILD_ITEMS = ["3rdparty/pto-isa/include"]`）。三段都把源目录 `copy_tree` 到 `build_lib/tilelang/3rdparty/...`，最终随 wheel 发布。

`setup.py` 还会把 `USE_ASCEND ON` 写进临时 build 目录的 config.cmake，见 [setup.py:757-760](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L757-L760)：

```python
if USE_ASCEND:
    config_file.write("set(USE_ASCEND ON)\n")
    config_file.write("set(USE_CUDA OFF)\n")
    config_file.write("set(USE_ROCM OFF)\n")
```

这是「路径 B（build_wheel_ascend.sh）」传递 `USE_ASCEND` 给 CMake 的实际落点，与 4.2.3 里 `install_ascend.sh` 的写法对应。

**为什么 JIT 需要这些头文件？** 证据在 `tilelang/jit/adapter/libgen.py`。`LibraryGenerator.compile_lib()` 在构造 bisheng 命令行时，会用 `-I` 把这些目录喂给编译器。ascendc 路径见 [tilelang/jit/adapter/libgen.py:164-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L164-L166)：

```python
f"-I{TL_ROOT}/3rdparty/catlass/include",
f"-I{TL_ROOT}/3rdparty/shmem/include",
f"-I{TL_ROOT}/3rdparty/shmem/src/device",
```

pto 路径见 [tilelang/jit/adapter/libgen.py:205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L205)：`f"-I{TL_ROOT}/3rdparty/pto-isa/include"`。

而 `TL_ROOT` 的解析见 [tilelang/jit/adapter/libgen.py:16-33](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L16-L33)：先看 `TL_ROOT` 环境变量，否则回退到 `TILELANG_PACKAGE_PATH`（即已安装的 `tilelang/` 包目录）及其父目录，并要求其下存在 `3rdparty/`。

把这两段拼起来就得到了本讲的核心结论：

> **`setup.py` 把 `3rdparty/{catlass,shmem,pto-isa}/include` 打进 wheel，是为了让用户 `pip install` 后，JIT 阶段 `libgen.py` 能在已安装的包目录下（`TL_ROOT/3rdparty/...`）找到这些头文件，从而成功调用 bisheng 编译 kernel。** 这也是 [u1-l5 JIT 与运行总流程](./u1-l5-jit-and-pipeline.md) 的物质基础。

> 补充：wheel 的版本号里还会带上 CANN 版本标签（如 `+cann83x`），方便区分不同 CANN 编出来的包。相关逻辑见 [setup.py:135-146](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L135-L146)（`get_cann_version` 读 `opp/version.info`）与 [setup.py:149-184](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L149-L184)（拼到 local version）。这部分「待本地验证」具体 tag 字符串。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：亲自验证「打包 → JIT」的头文件传递链是闭合的。
2. **操作步骤**：
   - 阅读 [setup.py:534-581](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L534-L581)，记下被拷进 wheel 的 4 个目录。
   - 阅读 [tilelang/jit/adapter/libgen.py:16-33](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L16-L33) 与 [tilelang/jit/adapter/libgen.py:164-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L164-L166)，记下 JIT 实际 `-I` 的目录。
3. **需要观察的现象**：两边的目录集合应当一致（ascendc 用 catlass+shmem，pto 用 pto-isa）。
4. **预期结果**：你能用一句话解释「如果 `setup.py` 漏拷了 `catlass/include`，JIT 一个 ascendc kernel 会报什么错」。
5. 无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：`setup.py` 里 `USE_ASCEND` 这个变量是从哪里来的？

> **答案**：从环境变量 `USE_ASCEND` 读取（见 [setup.py:39](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L39)），而 `build_wheel_ascend.sh` 在 [build_wheel_ascend.sh:41](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh#L41) 处 `export USE_ASCEND=true`。

**练习 2**：`setup.py` 把哪几个 `3rdparty/` 头文件目录拷进了 wheel？

> **答案**：`3rdparty/catlass/include`、`3rdparty/shmem/include`、`3rdparty/shmem/src/device`、`3rdparty/pto-isa/include`（见 [setup.py:534-581](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L534-L581)）。

**练习 3**：用一句话说明「为什么 pip 装完 wheel 还需要机器上有 CANN」。

> **答案**：wheel 只带了 C++ 头文件和 `libtilelang.so`，但 JIT 阶段还需要 CANN 提供的 `bisheng` 编译器和 `libruntime` 等运行时库（见 [libgen.py:164-183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L164-L183) 的 `-lruntime`、`-lascendcl` 与 `ASCEND_HOME_PATH`），这些 CANN 不在 wheel 里。

---

## 5. 综合实践

本实践把第 4 节的 4 个模块串起来，对应本讲的核心任务：**配置 `ASCEND_HOME_PATH` 并运行 `build_wheel_ascend.sh`，检查产物 wheel 中是否包含 catlass/pto-isa/shmem 的 include 目录，并解释为什么 JIT 编译需要这些头部。**

> 说明：本实践需要一台已安装 CANN（≥ 8.3.RC1）的昇腾环境。如果你当前没有这样的机器，请按「无硬件替代方案」完成源码阅读与静态检查部分，并在能触发实际编译的步骤处标注「待本地验证」。

### 有硬件环境

1. **实践目标**：亲手编出一个 wheel，并验证它确实带了 JIT 所需的头文件。
2. **操作步骤**：
   ```bash
   # 1. 拉代码（带子模块）
   git clone --recursive https://github.com/tile-ai/tilelang-ascend.git
   cd tilelang-ascend

   # 2. 先 source CANN 自带的 set_env.sh（按你的实际安装路径）
   source /usr/local/Ascend/ascend-toolkit/set_env.sh

   # 3. 设置 ASCEND_HOME_PATH
   export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest

   # 4. 构建 wheel（不需要 LLVM，省略 --enable-llvm）
   ./build_wheel_ascend.sh

   # 5. 安装产物
   pip install dist/tilelang-*.whl
   ```
3. **检查 wheel 是否包含头文件目录**：不要解压 wheel，而是检查安装后的包目录：
   ```bash
   python -c "import tilelang, os; print(os.path.dirname(tilelang.__file__))"
   # 假设输出 <PYLIB>/site-packages/tilelang
   ls <PYLIB>/site-packages/tilelang/3rdparty
   # 期望看到 catlass  pto-isa  shmem 三个目录
   ls <PYLIB>/site-packages/tilelang/3rdparty/catlass/include   # 应非空
   ls <PYLIB>/site-packages/tilelang/3rdparty/pto-isa/include   # 应非空
   ls <PYLIB>/site-packages/tilelang/3rdparty/shmem/include     # 应非空
   ```
   你也可以直接 `unzip -l dist/tilelang-*.whl | grep 3rdparty` 在 wheel 文件里确认。
4. **需要观察的现象**：wheel（以及安装后的 `tilelang/3rdparty/`）下应能看到 `catlass/include`、`pto-isa/include`、`shmem/include`、`shmem/src/device` 这几个目录且非空；同时还应看到 `tilelang/lib/libtilelang.so`。
5. **预期结果**：所有上述目录都存在。
6. **验证「JIT 依赖这些头文件」**：临时把 `tilelang/3rdparty/catlass/include` 改名（如 `mv .../catlass/include .../catlass/include_bak`），然后跑 `cd examples/gemm && python example_gemm.py`。预期 JIT 阶段 bisheng 报「找不到 catlass 头文件」类错误；改回后恢复正常。**这一步的报错现象待本地验证**（取决于生成代码具体 `#include` 了哪些头）。
7. **用一句话回答实践任务里的问题**：JIT 在首次调用 kernel 时，会把 codegen 生成的 Ascend C 代码交给 bisheng 编译；生成的代码 `#include` 了 catlass/shmem（ascendc 路径）或 pto-isa（pto 路径）的模板头文件，`libgen.py` 用 `-I{TL_ROOT}/3rdparty/.../include` 把它们喂给 bisheng（[libgen.py:164-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L164-L166)、[libgen.py:205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L205)），所以 wheel 必须随包发布这些头文件，否则 JIT 失败。

### 无硬件替代方案（源码阅读 + 静态检查）

如果你没有昇腾机器，仍可完成本实践的理解部分：

1. 通读 [build_wheel_ascend.sh](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/build_wheel_ascend.sh) 全文，标注出：`ASCEND_HOME_PATH` 校验、`USE_ASCEND=true` 导出、子模块与补丁、`python setup.py bdist_wheel`。
2. 在 [setup.py:534-581](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/setup.py#L534-L581) 找到三段 `*_PREBUILD_ITEMS`，把它们拷贝的目标目录记下来。
3. 在 [libgen.py:142-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L142-L228) 找到 bisheng 命令行里所有 `-I{TL_ROOT}/3rdparty/...` 项，验证它与上一步的目录集合一致。
4. 画一张「打包（setup.py）→ 安装（pip）→ JIT（libgen.py 调 bisheng）」的头文件流转图，作为本实践的产出。实际编译与运行效果**待本地验证**。

## 6. 本讲小结

- 运行 tilelang-ascend 的前置是 **CANN（≥ 8.3.RC1）+ torch-npu（≥ 2.6.0.RC1）**，并通过 `ASCEND_HOME_PATH` 告诉框架 CANN 在哪。
- README 给出三条安装路径：**直接装 wheel**（推荐）/ **`build_wheel_ascend.sh` 编 wheel** / **`install_ascend.sh` 源码编译安装**；后两者都要先 `git clone --recursive` 拉子模块并打 tvm 补丁。
- `USE_ASCEND` 是贯穿全流程的总开关：`build_wheel_ascend.sh` 用环境变量传给 `setup.py`，`install_ascend.sh` 用 `set(USE_ASCEND ON)` 写进 `config.cmake`，最终被 `CMakeLists.txt` 读到，决定是否把 4 个 Ascend 后端源文件编进 `libtilelang.so`。
- `setup.py` 把 `libtilelang.so`、`src/tl_templates` 以及 `3rdparty/{catlass,shmem,pto-isa}` 的头文件目录都打进 wheel。
- 这些头文件是 **JIT 的必需品**：`libgen.py` 在调用 bisheng 时用 `-I{TL_ROOT}/3rdparty/.../include` 引用它们，因为 codegen 生成的 Ascend C 代码会 `#include` 这些模板库。
- 即使 `pip install` 了 wheel，机器上仍必须有 CANN（提供 bisheng 与 `libruntime`），因为 JIT 编译和运行都依赖它。

## 7. 下一步学习建议

装好之后，下一步自然是「跑通第一个算子并看懂它」。建议进入：

- [u1-l3 仓库目录结构与模块地图](./u1-l3-repo-layout.md)：先建立从前端 DSL 到 pass 再到 codegen/runtime 的整体模块心智模型。
- [u1-l4 第一个算子：运行并读懂 GEMM](./u1-l4-first-gemm.md)：亲手跑 `examples/gemm/example_gemm.py`，看到 `Kernel Output Match!`。
- [u1-l5 JIT 即时编译与运行总流程](./u1-l5-jit-and-pipeline.md)：本讲提到的 `libgen.py` / bisheng / `.so` 加载这一整套 JIT 链路，将在那里系统展开。

如果你想深入了解编译产物结构，可以提前浏览 [u6-l4 运行时加载与 Bisheng 设备编译](./u6-l4-runtime-bisheng.md)，它详细讲解 `libgen.py` 与 bisheng 的协作。
