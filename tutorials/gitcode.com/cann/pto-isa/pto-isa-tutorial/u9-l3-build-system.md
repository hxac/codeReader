# 构建体系与打包：CMake、build.sh 与 wheel

## 1. 本讲目标

前两讲我们分别学习了 Auto Mode（编译器接管样板代码）和自定义算子的完整交付（kernel + host 封装 + 框架集成）。本讲把视角再拉高一层，回答一个交付工程师必须搞清楚的问题：**这个仓库的「构建」到底在构建什么？产物如何变成可安装的包？**

1. 掌握 **CMake 结构**：读懂顶层 `CMakeLists.txt` 的 19 行正文，理解它是一个「打包工程」而非「编译工程」；理清仓库中多套独立 CMake 工程（tests / demos / kernels）与顶层打包工程的分工，以及 `__CPU_SIM`、`--cce-aicore-arch` 等条件编译开关在 CMake 层的组织方式。
2. 掌握 **build.sh**：理解这个 330 行的一键入口如何用状态标志分发到 `tests/run_st.sh`、`tests/script/build_st.py`、`tests/run_cpu.py` 和 CPack 打包流程；熟练使用 `--run_all`、`--a3`、`--sim`、`--cpu`、`--pkg` 等参数组合。
3. 掌握 **wheel 打包**：读懂 `setup.py` 如何把 header-only 的 C++ 库打成 Python wheel（`data_files` 装头文件），理解 `pyproject.toml` / `MANIFEST.in` / `setup.py` 三者的角色与版本号「三处同步」问题；亲手打一次包并解包验证。

## 2. 前置知识

本讲假设你已读过：

- **u1-l3 环境搭建与 CPU 仿真**：已经用 `python3 tests/run_cpu.py` 跑通过 CPU 路径，知道 `build.sh --cpu` 会转发到这个脚本。
- **u2-l4 统一入口与多后端切换**：`pto-inst.hpp` 按 `__CPU_SIM` / `__CCE_AICORE__` / `__COSTMODEL` 三个宏路由后端；`arch_macro.hpp` 把 `__NPU_ARCH__` 数字（2201/3101/9201…）翻译成架构宏。本讲要看清**这些宏是谁、在哪里定义的**——答案在 CMake。
- **u9-l1 Auto Mode**：`__PTO_AUTO__` 宏与编译器选项 `--cce-pto-auto-enable` 的关系，本讲会在 NPU 测试工程的 CMake 里再次遇到它。

再补充四个本讲新概念：

| 术语 | 通俗解释 |
|------|----------|
| **header-only 库** | 整个 `include/` 目录只有 `.hpp` 头文件、没有 `.cpp` 需要编译成库。使用者 `#include` 即用。因此「构建本仓库」实际是构建**使用它的测试 / 示例 / 算子工程**，或把它**打成可安装的包**。 |
| **CPack / makeself / 组件（COMPONENT）** | CPack 是 CMake 自带的打包前端；`run` 类型产出 makeself 自解压脚本（`.run` 文件），`rpm`/`deb` 产出 Linux 发行版安装包。`COMPONENT pto-isa` 把文件划成一个可独立安装的组件。 |
| **FetchContent** | CMake 在配置期下载外部仓库的机制。本仓库用它拉取共享的 CANN 工程化 cmake 仓库（提供 `init_cann_project`、`set_cann_cpack_config` 等函数）。 |
| **wheel 与 data_files** | wheel 是 Python 的标准二进制分发格式（本质是 zip）。对一个没有 Python 代码的 C++ 头文件库，`setup.py` 用 `data_files` 把头文件作为「数据」装进 wheel，安装后落在 `share/pto-isa/include/...` 下，供下游用 `sys.prefix` 拼路径找到。 |

## 3. 本讲源码地图

| 文件 | 作用 | 所属模块 |
|------|------|----------|
| [CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/CMakeLists.txt) | 顶层打包工程入口：拉取 cann-cmake、安装头文件与脚本、调 CPack | CMake 结构 |
| [cmake/fetch_cann_cmake.cmake](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/fetch_cann_cmake.cmake) | FetchContent 拉取共享 CANN cmake 仓库（三種来源：本地目录 / 本地 tar 包 / 在线 git） | CMake 结构 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake) | `pack_built_in()`：定义安装布局（include / pkg_inc / script / version.info）与 CPack 配置 | CMake 结构 |
| [version.cmake](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/version.cmake) | 声明包版本 9.1.0 与 8 个运行时依赖 | CMake 结构 |
| [cmake/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/README.md) | cmake 目录自述文档 | CMake 结构 |
| [build.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh) | 仓库一键入口：参数解析 + 按状态标志分发 | build.sh |
| [tests/run_st.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh) | NPU/仿真 ST 运行器（build.sh 的主要被调度者） | build.sh |
| [tests/script/build_st.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/build_st.py) | 按架构选择 ST 工程目录并驱动 cmake/make | build.sh |
| [setup.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/setup.py) | wheel 打包入口：`data_files` 收集 `include/` | wheel 打包 |
| [pyproject.toml](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/pyproject.toml) | 构建系统声明与项目元数据 | wheel 打包 |
| [MANIFEST.in](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/MANIFEST.in) | sdist 源码包文件清单 | wheel 打包 |
| [tests/cpu/st/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt) | CPU 仿真 ST 工程：`add_definitions(-D__CPU_SIM)` 的定义点 | CMake 结构 |
| [tests/npu/a2a3/src/st/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/CMakeLists.txt) | NPU ST 工程：bisheng 编译器与 CCE 选项 | CMake 结构 |
| [tests/npu/a2a3/src/st/testcase/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt) | `pto_vec_st` / `pto_cube_st` / `pto_mix_st` 用例骨架（核型架构标志） | CMake 结构 |

## 4. 核心概念与源码讲解

### 4.1 CMake 结构：一个「打包工程」+ N 个「消费工程」

#### 4.1.1 概念说明

初学者最容易产生的误解是：顶层 `CMakeLists.txt` 负责编译整个仓库。**恰恰相反**——PTO 是 header-only 库，真正要编译的是「消费这些头文件的工程」，它们各自有独立的 CMake 工程：

- `tests/cpu/st/CMakeLists.txt`：CPU 仿真 ST（u1-l3、u3-l4 已反复使用）；
- `tests/npu/a2a3/src/st/CMakeLists.txt`、`tests/npu/a5/src/st/` 等：各架构 NPU ST；
- `demos/*/CMakeLists.txt`、`kernels/*/CMakeLists.txt`：独立示例与算子工程。

而顶层 `CMakeLists.txt` 的唯一职责是**把 `include/` 头文件、安装脚本和版本信息组装成 CANN 风格的安装包**（makeself `.run` / `.rpm` / `.deb`）。理解了这一定位，后面所有细节都顺理成章。

#### 4.1.2 核心流程

顶层 CMake 的配置流程：

```text
cmake -S . -B build -D PACKAGE_TYPE=run
  ├─ include(cmake/fetch_cann_cmake.cmake)   # 必须在 project() 之前！
  │    └─ FetchContent 拉取 gitcode.com/cann/cmake（tag master-042）
  │         └─ 得到 init_cann_project / set_cann_cpack_config / CANN_CMAKE_DIR …
  ├─ project(pto-isa)
  ├─ init_cann_project() + add_cann_target_options()
  ├─ 设置 PACKAGE_TYPE 缓存变量（run / rpm / deb，默认 run）
  ├─ include(cmake/package.cmake)  → pack_built_in() 定义 install 规则与 CPack 配置
  ├─ include(version.cmake)        → 版本 9.1.0 与运行时依赖清单
  └─ make package                  → 产出 build_out/ 下的安装包
```

`pack_built_in()` 定义的安装布局（CPack 组件 `pto-isa`）：

```text
<安装根>/
├── share/info/pto_isa/version.info          # 由 build/version.pto-isa.info 改名而来
├── share/info/pto_isa/script/               # 安装/校验脚本（来自 scripts/package 与 cann-cmake）
├── <arch>-linux/include/                    # include/ 全量头文件（排除各 README）
└── <arch>-linux/pkg_inc/                    # 内部头文件目录（当前仅 .gitkeep 占位）
```

#### 4.1.3 源码精读

**① 顶层入口只有 19 行正文**。[CMakeLists.txt:L11-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/CMakeLists.txt#L11-L29) 中，第 12 行 `include(cmake/fetch_cann_cmake.cmake)` 出现在第 13 行 `project(pto-isa)` **之前**——这不是随手写的顺序：

```cmake
include(cmake/fetch_cann_cmake.cmake)   # L12：先拉取公共函数库
project(pto-isa)                        # L13：后声明工程

init_cann_project()                     # L15：来自被拉取的 cann-cmake
add_cann_target_options()               # L16
```

原因藏在 [cmake/fetch_cann_cmake.cmake:L11-L14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/fetch_cann_cmake.cmake#L11-L14)：文件用 `if(NOT PROJECT_SOURCE_DIR)` 做守卫——一旦 `project()` 执行过，`PROJECT_SOURCE_DIR` 已被定义，整段拉取逻辑会被跳过。所以「先 include 后 project」是硬性要求。

**② cann-cmake 的三种来源**（离线优先，在线兜底）。[cmake/fetch_cann_cmake.cmake:L13-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/fetch_cann_cmake.cmake#L13-L35) 按顺序尝试：

1. `${CANN_3RD_LIB_PATH}/cann-cmake` 目录直接 include（完全离线，对应 `build.sh --cann_3rd_lib_path`）；
2. `${CANN_3RD_LIB_PATH}/cmake-master-042.tar.gz` 本地压缩包（`FetchContent_Declare(URL ...)`）；
3. 都没有则 `GIT_REPOSITORY https://gitcode.com/cann/cmake.git`、`GIT_TAG master-042`、`GIT_SHALLOW TRUE` 在线浅克隆。

这套设计让有内网镜像的 CI 不依赖公网，而个人开发者开箱即用。

**③ 版本与依赖**。[version.cmake:L11-L20](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/version.cmake#L11-L20) 声明 `set_cann_package(pto-isa VERSION "9.1.0")`，并为 runtime、opbase、asc-devkit、metadef、ge-executor、bisheng-compiler、asc-tools、ge-compiler 共 8 个组件设置 `CUR_MAJOR_MINOR_VER`（当前主次版本）的运行时依赖——安装 `.run` 包时，`share/info/pto_isa/script/` 里的校验脚本会据此检查环境版本。

**④ 安装规则：include 与 pkg_inc 双目录**。[cmake/package.cmake:L75-L102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L75-L102) 把仓库 `include/` 安装到 `${CMAKE_SYSTEM_PROCESSOR}-linux/include`，把 `pkg_inc/` 安装到同级 `pkg_inc`，并用 `REGEX ... EXCLUDE` 剔除各级 README：

```cmake
set(pto_source ${CMAKE_CURRENT_SOURCE_DIR}/include)
install(DIRECTORY ${pto_source}/
    DESTINATION ${CMAKE_SYSTEM_PROCESSOR}-linux/include   # 对外头文件
    ...)
# pkg_inc: 存放非对外暴露的（internal）头文件，仅内部模块使用。
set(pto_pkg_inc_source ${CMAKE_CURRENT_SOURCE_DIR}/pkg_inc)
install(DIRECTORY ${pto_pkg_inc_source}/
    DESTINATION ${CMAKE_SYSTEM_PROCESSOR}-linux/pkg_inc   # 内部头文件
    ...)
```

注释（L88-L90）说明得很清楚：`pkg_inc` 是「非对外暴露的 internal 头文件」目录，安装后顶层会由公共基础块创建 `pkg_inc -> <arch>-linux/pkg_inc` 软链接。当前仓库的 `pkg_inc/` 下只有 `.gitkeep` 占位和 README，是**预留给未来内部头文件**的位置——这一点在 4.3 节讨论 wheel 时还要再对照一次。

**⑤ 架构探测与包名修正**。[cmake/package.cmake:L14-L23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291b52e8003a8a/cmake/package.cmake#L14-L23) 用 `CMAKE_SYSTEM_PROCESSOR` 区分 `x86_64` / `aarch64`；[L143-L151](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L143-L151) 处理了一个典型的 CPack 坑：组件安装开启后 CPack 会把组件名再拼一次，产生 `cann-pto-isa-pto-isa` 这种重复名，所以必须显式设置 `CPACK_RPM_PTO_ISA_PACKAGE_NAME "cann-pto-isa"` 等变量。最后 [L153-L158](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L153-L158) 把一切交给 cann-cmake 提供的 `set_cann_cpack_config()`，输出目录指向源码树的 `build_out/`。

**⑥ 条件编译开关在哪里定义？——CPU 侧**。u2-l4 讲过 `__CPU_SIM` 宏路由后端，现在给出它的定义点：[tests/cpu/st/CMakeLists.txt:L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L31) 一行 `add_definitions(-D__CPU_SIM)` 对整个工程生效。同文件 [L17-L23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L17-L23) 决定语言标准：默认 C++20，GCC ≥ 14 或显式开启 BF16 选项时升到 C++23（`std::bfloat16_t` 需要）。[L62-L103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L62-L103) 是一段很实用的 GTest 兜底逻辑：优先 `find_package(GTest)`，找不到就 FetchContent 拉 googletest v1.14.0，让 macOS 构建自包含。每个用例目录里只有一行 [pto_cpu_sim_st(tadd)](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10)，由 [tests/cpu/st/testcase/CMakeLists.txt:L11-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L11-L35) 的函数把 `main.cpp`（+ 可选 `NAME_kernel.cpp`）编成 gtest 可执行。

**⑦ 条件编译开关在哪里定义？——NPU 侧**。[tests/npu/a2a3/src/st/CMakeLists.txt:L25-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/CMakeLists.txt#L25-L35) 强制要求 `ASCEND_HOME_PATH` 环境变量（来自 CANN 的 `set_env.sh`），并把编译器切到 **bisheng**（毕昇，即 CCE）。`__CCE_AICORE__` 不需要手动定义——它是 bisheng 以 `-xcce` 模式编译设备代码时自动预定义的（这正是 u2-l4 说「由 CCE 编译器自动预定义」的落地处）。[L76-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/CMakeLists.txt#L76-L96) 定义 CCE 专属编译选项（栈大小、溢出记录等），并有两个开关：`DEBUG_MODE` 加 `--cce-enable-print`（允许设备侧打印），`AUTO_MODE` 加 `--cce-pto-auto-enable`——后者就是 u9-l1 Auto Mode 的编译器开关。

架构选择发生在用例骨架函数里：[tests/npu/a2a3/src/st/testcase/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt#L11-L19) 的 `pto_vec_st` 给 kernel 动态库加 `--cce-aicore-arch=dav-c220-vec`；同文件还有 `pto_cube_st`（`dav-c220-cube`）与 `pto_mix_st`（`dav-c220`）两个变体。`dav-c220` 就是 A2/A3 的芯片家族代号，编译器据此设置 `__NPU_ARCH__=2201`，再由 u2-l4 讲过的 `arch_macro.hpp` 翻译成 `PTO_NPU_ARCH_A2A3`。host 侧 `main.cpp` 则是普通 C++ 可执行，两目标通过 `target_link_libraries(${NAME} PRIVATE ${NAME}_kernel ...)` 缝合（[L33-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt#L33-L38)）——这与 u9-l2 讲过的「kernel 动态库 + host 可执行」双目标构建完全同构。

真机 / 仿真的切换也是编译期条件，用 CMake 生成器表达式完成：[L35-L36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/CMakeLists.txt#L35-L36) 中 `$<$<STREQUAL:${RUN_MODE},sim>:runtime_camodel>` 与 `$<$<STREQUAL:${RUN_MODE},npu>:runtime>`——`RUN_MODE=sim` 链接 CAMS 仿真器运行时，`RUN_MODE=npu` 链接真机运行时。`RUN_MODE` 与 `SOC_VERSION` 由 `build_st.py` 通过 `-D` 传入（见 4.2 节）。

#### 4.1.4 代码实践

**实践目标**：不实际配置 NPU 工程，仅通过源码阅读画出「仓库内 CMake 工程分布图」，并定位三个关键开关的定义行。

**操作步骤**：

1. 在仓库根目录执行下面命令，列出所有 CMake 工程入口（每个含 `project()` 的 `CMakeLists.txt`）：

   ```bash
   grep -l "^project(" --include=CMakeLists.txt -r . | sort
   ```

2. 统计各工程的宏开关定义点：

   ```bash
   grep -rn "add_definitions(-D__CPU_SIM\|--cce-aicore-arch\|--cce-pto-auto-enable" \
        --include=CMakeLists.txt tests/ | head -20
   ```

3. 在 [cmake/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/README.md#L13-L21) 中核对每个 cmake 文件的职责描述与你读到的实现是否一致。

**需要观察的现象**：步骤 1 会返回多个彼此独立的工程（`tests/cpu/st`、`tests/cpu/comm/st`、`tests/npu/a2a3/src/st`、`tests/npu/a5/src/st`、`demos/cpu/gemm_demo`、各 `kernels/*` 等），它们没有任何一个 include 顶层 `CMakeLists.txt`——印证「顶层只管打包」；步骤 2 会看到 `__CPU_SIM` 只在 CPU 工程定义、`--cce-aicore-arch` 只在 NPU 工程出现。

**预期结果**：你能画出一张三列的表——「工程路径 / 后端宏 / 架构标志」，例如 `tests/cpu/st → -D__CPU_SIM → 无`、`tests/npu/a2a3/src/st → bisheng 自动定义 __CCE_AICORE__ → dav-c220`、`demos/cpu/gemm_demo → __CPU_SIM __PTO_AUTO__（见 [demos/cpu/gemm_demo/CMakeLists.txt:L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L26)）→ 无」。命令输出以本地为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `include(cmake/fetch_cann_cmake.cmake)` 必须写在 `project(pto-isa)` 之前？如果把两行对调会发生什么？

**答案**：`fetch_cann_cmake.cmake` 用 `if(NOT PROJECT_SOURCE_DIR)` 做守卫。`project()` 执行后会定义 `PROJECT_SOURCE_DIR`，对调后守卫条件为假，整个 FetchContent 拉取被跳过，后续 `init_cann_project()` 等函数不存在，CMake 配置直接报「Unknown CMake command」错误。

**练习 2**：`include/` 和 `pkg_inc/` 在 CANN 安装包中各装到什么路径？为什么要分两个目录？

**答案**：分别装到 `<arch>-linux/include` 与 `<arch>-linux/pkg_inc`，安装后顶层生成 `pkg_inc` 软链接。`include` 是对外承诺稳定的公开头文件；`pkg_inc` 留给不对外暴露、仅内部模块使用的头文件（当前仓库中只有占位文件），分开存放可以在包级别约束 API 边界。

**练习 3**：CPU 工程里 `__CPU_SIM` 是全局 `add_definitions`，而 NPU 工程里 `__CCE_AICORE__` 却找不到定义语句，为什么？

**答案**：`__CCE_AICORE__` 由 bisheng 编译器在 `-xcce` 设备编译模式下自动预定义（`CMAKE_CCE_COMPILE_OPTIONS` 的 `-xcce` 触发），属于编译器内建宏；而 CPU 侧用的是普通 g++/clang，没有任何内建宏可依赖，必须由 CMake 显式注入 `-D__CPU_SIM`。

### 4.2 build.sh：一键入口的状态标志分发

#### 4.2.1 概念说明

`build.sh` 是仓库对外的统一操作入口。它的设计模式非常经典：**解析参数 → 置位状态标志 → main 里按标志依次调用职能函数**。它自己几乎不干活，所有重活都委托给专职脚本：

| 状态标志 | 触发函数 | 实际执行者 |
|----------|----------|-----------|
| `--run_simple` | `run_simple_st` | `tests/run_st.sh`（精选子集） |
| `--run_all` | `run_all_st` | `tests/run_st.sh --all`（全量 ST） |
| `--comm` | `run_comm_st` | `tests/run_st.sh --comm`（通信 ST） |
| `--build` | `build_only` | `tests/script/build_st.py`（只编译不运行） |
| `--cpu` | `run_cpu_st` | `tests/run_cpu.py` + `tests/run_costmodel_tests.sh` |
| `--pkg [--pkg-type=T]` | `build_package` | `cmake -D PACKAGE_TYPE=T && make package` |
| `--a3` / `--a5` | （修饰符） | 决定上面各函数操作 a2a3 还是 a5 工程 |
| `--sim` / `--npu` | （修饰符） | 决定 `RUN_TYPE`，传给 `run_st.sh` 为 `--sim`/`--npu` |
| `--auto_mode` | （修饰符） | 追加 `--auto_mode` 给 `run_st.sh` |
| `--cpu_bf16` | （修饰符） | 给 `run_cpu.py` 加 `--enable-bf16` |

#### 4.2.2 核心流程

```text
bash build.sh <flags>
  └─ main()
       ├─ checkopts：getopt 解析长选项，置位 ENABLE_* / RUN_TYPE / PACKAGE_TYPE
       ├─ RUN_TYPE==sim → ulimit -n 65535        # 仿真器要打开大量文件描述符
       └─ 依序检查各 ENABLE_* 标志并调用对应函数（可组合，一次跑多件事）
            ├─ run_all_st → ./tests/run_st.sh --a3|--a5|--a3_a5 --sim|--npu --all [--auto_mode]
            ├─ build_only → python3 tests/script/build_st.py -r npu -v a3|a5 -t all
            ├─ build_package → 清空 build/ 与 build_out/ → cmake -D PACKAGE_TYPE=… → make package
            └─ run_cpu_st → run_cpu.py [--enable-bf16] --clean --verbose
                          → 三个 demo（gemm / flash_attn / mla）
                          → bash tests/run_costmodel_tests.sh
```

注意架构默认值的一个不对称细节：`build_only` 在未指定 `--a3/--a5` 时默认编 **a5**（[build.sh:L187](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L187)），而 `run_all_st` / `run_simple_st` 默认跑 **a3**（[build.sh:L204](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L204)）。使用时最好显式带上架构标志，不要依赖默认值。

#### 4.2.3 源码精读

**① 参数解析骨架**。[build.sh:L73-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L73-L95) 先把十几个状态变量全部初始化（避免未定义变量隐患），再用 `getopt -a` 解析长选项：

```bash
ENABLE_A3=FALSE
ENABLE_A5=FALSE
...
RUN_TYPE="npu"          # 默认真机
PACKAGE_TYPE="run"      # 默认 makeself run 包

parsed_args=$(getopt -a -o j:hvuO: -l help,verbose,cov,make_clean,noexec,pkg,pkg-type:,run_all,a3,a5,sim,npu,comm,cpu,cpu_bf16,auto_mode,run_simple,build,cann_3rd_lib_path: -- "$@")
```

每个 `case` 分支只做一件事——置位标志（如 [L105-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L105-L108) 的 `--run_all` 置 `ENABLE_BUILD_ALL=TRUE`），**解析与执行彻底分离**。

**② `--run_all` 的完整链路**。[build.sh:L247-L267](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L247-L267) 按四个布尔组合出 `--a3` / `--a5` / `--a3_a5` 之一（都没给则默认 `--a3`），拼上 `--$RUN_TYPE --all` 与可选的 `--auto_mode`，交给 `tests/run_st.sh`。`run_st.sh` 的用法说明见 [tests/run_st.sh:L29-L49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_st.sh#L29-L49)：平台（`--a3/--a5/--a3_a5`）、模式（`--simple/--all`，必选其一）、运行目标（`--sim/--npu`）是三个正交维度，文档里还给出了典型组合 `ulimit -n 65536 && ./tests/run_st.sh --a3 --sim --all`——与 `main()` 里 `--sim` 自动放宽文件描述符上限（[build.sh:L303-L305](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L303-L305)）相呼应。

**③ `--build` 与 build_st.py 的架构路由**。[build.sh:L176-L190](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L176-L190) 的 `build_only` 调用 `python3 tests/script/build_st.py -r npu -v a3|a5 -t all`。注意 `-r` 恒为 `npu`——**编译产物与运行模式无关**（运行时按 `RUN_MODE` 选链接库），所以只需编一次。`build_st.py` 侧的路由逻辑在两处：[tests/script/build_st.py:L86-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/build_st.py#L86-L96) 把短名映射成 `SOC_VERSION`（a3→`Ascend910B1`、a5→`Ascend950PR_9599`、a6→`dav_9201`、kirinX90→`KirinX90`…），[L107-L116](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/build_st.py#L107-L116) 把短名映射到工程目录（a3→`tests/npu/a2a3/src/st`、a5→`tests/npu/a5/src/st`、a6→`tests/npu/a6/src/st`…），然后 [L39-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/build_st.py#L39-L42) 以 `cmake -DRUN_MODE=… -DSOC_VERSION=… -DTEST_CASE=… [-DAUTO_MODE=ON]` 配置后并行 make。这就是 4.1 节那些 CMake 变量的注入源头。

**④ `--cpu`：回到初学者路径**。[build.sh:L233-L245](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L233-L245) 是 CPU 侧全量验证：先 `run_cpu.py --clean --verbose` 全量重编跑 ST，再依次跑 gemm / flash_attn / mla 三个 demo，最后 `run_costmodel_tests.sh` 跑 CostModel 测试——正好覆盖 u1-l3（run_cpu.py）与 u10-l3（CostModel）两条线。

**⑤ `--pkg`：打包函数**。[build.sh:L282-L292](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L282-L292) 先删掉旧 `build/` 与 `build_out/` 保证干净，再进 `build/` 执行 `cmake ${CMAKE_ARGS} -D PACKAGE_TYPE=${PACKAGE_TYPE} .. && make package`——这正是 4.1 节顶层 CMake 工程的消费方式，产物落在 `build_out/`。

#### 4.2.4 代码实践

**实践目标**：在不接触 NPU 的前提下，用 `build.sh --cpu` 走通一条完整分发链路，并对照源码确认每一步调用了什么。

**操作步骤**：

1. 查看帮助（无副作用）：

   ```bash
   bash build.sh --help
   ```

2. 触发 CPU 路径（需要 u1-l3 搭好的 C++20 工具链，脚本会自动补装依赖）：

   ```bash
   bash build.sh --cpu 2>&1 | tee /tmp/build_cpu.log
   ```

3. 打开日志，按顺序找出这四段输出的分界：`Start to run cpu st`（ST 全量）→ `Start to run demo: gemm`（若脚本打印类似标记）→ `flash_attn` → costmodel 测试。

4. 回到 [build.sh:L233-L245](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L233-L245)，把日志里出现的每条命令与源码里的每一行一一对应，画一张「标志 → 函数 → 实际命令」的调用图。

**需要观察的现象**：`--cpu` 会先做全量清理重编（`--clean --verbose`），耗时较长；三个 demo 各自输出带 `perf:` 的性能行（u1-l3 讲过 demo 的输出约定）；整个过程不触碰 `ASCEND_HOME_PATH`。

**预期结果**：CPU ST 全部 PASS、三个 demo 输出正确性与性能信息、costmodel 测试通过。本讲义写作环境未执行该命令，具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`bash build.sh --run_all --sim` 与 `bash build.sh --a3 --sim --run_all` 有何区别？

**答案**：功能等价但显式程度不同。两者最终都会执行 `./tests/run_st.sh --a3 --sim --all`。前者依赖 `run_all_st` 里「未指定架构时默认 a3」的兜底分支；后者显式置位 `ENABLE_A3=TRUE`。显式写法更安全——不受默认值调整影响（注意 `build_only` 的默认值是 a5 而非 a3，默认值并不统一）。

**练习 2**：为什么 `main()` 里 `RUN_TYPE == "sim"` 时要先执行 `ulimit -n 65535`？

**答案**：CAMS 仿真器运行为真机构建的 ST 时会加载大量动态库、打开大量文件句柄，Linux 默认的 1024 个文件描述符上限容易不够，导致莫名失败。所以在进入仿真流程前把软上限放宽到 65535。`tests/run_st.sh` 的使用示例也要求用户在直接调用时自己执行 `ulimit -n 65536`。

**练习 3**：`build.sh --build`（`build_only`）为什么给 `build_st.py` 传 `-r npu` 而不是根据 `--sim`/`--npu` 传相应值？

**答案**：因为「编译」只需要产出目标文件和链接好的用例可执行，而 sim/npu 的差异在 4.1 节讲过是链接 `runtime_camodel` 还是 `runtime`——这个差异由 CMake 变量 `RUN_MODE` 在**配置期**决定。`build_only` 场景下编出来的就是 `-r npu` 指定的真机版；若要跑仿真版，应通过 `run_st.sh --sim` 走完整流程（它会用 `RUN_MODE=sim` 重新配置）。（补充：`build_st.py` 的 `-r` 参数本身支持 sim/npu 两值，`build_only` 固定选了 npu。）

### 4.3 wheel 打包：把 header-only 库装进 Python 生态

#### 4.3.1 概念说明

CANN 安装包（`.run`/`.rpm`/`.deb`）面向系统级部署，而 **wheel 面向 Python 生态分发**：下游项目在自己的 `setup.py`/`CMakeLists` 里用 `sys.prefix + "/share/pto-isa/include"` 之类的路径就能定位头文件，不必手动拷贝 `include/` 目录。这是 u9-l2 讲过的 `demos/baseline/add` 那种「把 op_extension 打成 wheel」模式在仓库自身上的应用——只不过这次 wheel 里装的不是 `.so`，而是纯头文件。

三个文件的分工：

| 文件 | 角色 |
|------|------|
| `pyproject.toml` | 声明构建后端（`setuptools.build_meta`）与项目元数据（名称、版本、Python 版本要求） |
| `setup.py` | 动态逻辑：`os.walk("include")` 收集头文件到 `data_files` |
| `MANIFEST.in` | 控制 **sdist（源码包）** 内容：LICENSE、README、`recursive-include include *` |

还有一个容易被忽略的工程细节：**版本号 9.1.0 出现在三处**——[pyproject.toml:L15](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/pyproject.toml#L15)、[setup.py:L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/setup.py#L31) 与 [version.cmake:L11](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/version.cmake#L11)（`set_cann_package(pto-isa VERSION "9.1.0")`）。发版时必须三处同步修改，否则 wheel 版本与 CANN 包版本会漂移。

#### 4.3.2 核心流程

```text
python3 -m build --wheel
  ├─ 读 pyproject.toml：构建后端 setuptools.build_meta，隔离环境装 setuptools>=45 + wheel
  ├─ 执行 setup.py：
  │    ├─ find_packages(exclude=["tests*","demos*","scripts*","kernels*"])  → 无 Python 包
  │    └─ get_include_files()：os.walk("include") 逐目录收集
  │         每个含文件的目录 root → (share/pto-isa/<root>, [root/文件...])
  └─ 产出 dist/pto_isa-9.1.0-py3-none-any.whl
       └─ 解包后：share/pto-isa/include/pto/pto-inst.hpp、… + pto_isa-9.1.0.dist-info/

pip install dist/pto_isa-9.1.0-*.whl
  └─ data_files 安装到 <sys.prefix>/share/pto-isa/include/…
```

关键点：wheel 里**没有 Python 代码**（`find_packages` 排除掉 tests/demos/scripts/kernels 后实际找不到任何包），头文件全部作为 `data_files` 携带。wheel 文件名中的 `py3-none-any` 也印证了这一点——纯平台无关的数据包。

#### 4.3.3 源码精读

**① 头文件收集函数**。[setup.py:L16-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/setup.py#L16-L26)：

```python
def get_include_files():
    include_dir = "include"
    data_files = []
    for root, _, files in os.walk(include_dir):
        if files:
            install_dir = os.path.join("share/pto-isa", root)
            file_paths = [os.path.join(root, f) for f in files]
            data_files.append((install_dir, file_paths))
    return data_files
```

逐行读：`os.walk("include")` 遍历出每个子目录 `root`（如 `include/pto/npu/a2a3`），映射规则是 **`include/<子路径>` → `share/pto-isa/include/<子路径>`**，目录里的全部文件作为第二元组元素。因此安装后的关键路径是 `<prefix>/share/pto-isa/include/pto/pto-inst.hpp`——统一入口头（u2-l4）在 wheel 里的落点可以被精确预测。

**② setup 调用**。[setup.py:L29-L37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/setup.py#L29-L37)：`packages=find_packages(exclude=["tests*", "demos*", "scripts*", "kernels*"])` 排除一切非交付目录；`data_files=get_include_files()` 携带全部头文件；`include_package_data=True` 配合 `MANIFEST.in`。注意 `include/` 本身**不在**排除清单里——它是唯一的交付物。

**③ 构建系统声明**。[pyproject.toml:L9-L19](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/pyproject.toml#L9-L19)：`requires = ["setuptools>=45", "wheel"]` + `build-backend = "setuptools.build_meta"` 是 PEP 517 标准写法，`python3 -m build` 会在隔离虚拟环境中安装这两个构建依赖再执行构建（离线环境需 `--no-isolation` 并自备依赖）。`requires-python = ">=3.9"` 与 classifiers 中的 3.9–3.12 对应。

**④ sdist 清单**。[MANIFEST.in:L1-L4](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/MANIFEST.in#L1-L4)：`recursive-include include *` 让源码包（`python3 -m build --sdist`）也带上头文件；`include LICENSE / README.md / README_zh.md` 补齐许可与说明。要区分：**MANIFEST.in 管的是 sdist 里有什么；wheel 里有什么由 `data_files` 决定**。

**⑤ wheel 路径 vs CMake 包路径——一个必须澄清的区别**。学习任务里常问「头文件是不是被打进 `pkg_inc/include`」——**不是**，两套打包体系的布局不同：

| 打包体系 | 头文件落点 | 依据 |
|----------|-----------|------|
| Python wheel | `share/pto-isa/include/pto/...` | [setup.py:L22](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/setup.py#L22) 的 `os.path.join("share/pto-isa", root)` |
| CANN run/rpm/deb 包 | `<arch>-linux/include/`（公开）与 `<arch>-linux/pkg_inc/`（内部，当前为占位） | [cmake/package.cmake:L75-L102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L75-L102) |

`pkg_inc` 只存在于 CANN 包体系；wheel 里没有也不需要它（wheel 的 `include/` 已经是全量公开头文件）。做解包验证时要按各自体系的真实路径检查，不能用错预期。

#### 4.3.4 代码实践

**实践目标**：亲手产出 wheel，解包验证头文件完整性，并与源码树做数量对账。

**操作步骤**：

1. 准备构建工具（一次性）：

   ```bash
   python3 -m pip install --user build
   ```

2. 在仓库根目录构建 wheel：

   ```bash
   python3 -m build --wheel
   ```

3. 检查产物（wheel 本质是 zip）：

   ```bash
   ls -la dist/
   unzip -l dist/pto_isa-9.1.0-py3-none-any.whl | head -30
   ```

4. 验证统一入口头存在、且路径符合预期（**不是** `pkg_inc`，见 4.3.3 ⑤）：

   ```bash
   unzip -l dist/pto_isa-9.1.0-py3-none-any.whl | grep -E "pto-inst\.hpp|include/pto/common/pto_tile\.hpp"
   ```

5. 数量对账——wheel 里的头文件数应与源码树一致：

   ```bash
   find include -type f | wc -l
   unzip -l dist/pto_isa-9.1.0-py3-none-any.whl | grep "share/pto-isa/include/" | wc -l
   ```

6. （可选）真实安装一次并定位安装路径：

   ```bash
   python3 -m pip install --user dist/pto_isa-9.1.0-py3-none-any.whl
   python3 -c "import sysconfig; print(sysconfig.get_paths()['data'])"
   # 头文件应位于上面打印的目录下的 share/pto-isa/include/pto/
   ```

**需要观察的现象**：步骤 3 的列表里除 `.dist-info/` 元数据外，全是 `share/pto-isa/include/...` 前缀的条目；步骤 4 能 grep 到 `pto-inst.hpp`；步骤 5 两个数字一致（wheel 侧可能因目录条目多计 1，差额为目录行，可加 `grep -v "/$"` 过滤）。

**预期结果**：`dist/pto_isa-9.1.0-py3-none-any.whl` 生成，`share/pto-isa/include/pto/pto-inst.hpp` 等 90+ 指令头文件完整在内。若步骤 2 在隔离环境拉取 setuptools 失败（无网络），改用 `python3 -m build --wheel --no-isolation`（需本地已装 setuptools+wheel）。本讲义写作环境未执行打包，文件名与条目数待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：这个仓库没有任何 Python 代码，为什么能打 wheel？打出来的 wheel 里装的是什么？

**答案**：wheel 不要求包内有 Python 代码。`setup.py` 用 `data_files` 把 `include/` 下所有头文件作为数据文件装进 wheel，安装后落到 `<prefix>/share/pto-isa/include/`；元数据（名称、版本、依赖）记录在 `.dist-info/` 里。下游通过 `sys.prefix` 拼路径取用头文件。

**练习 2**：如果发布 9.2.0 版本，需要改哪些文件？漏改会怎样？

**答案**：至少三处——`pyproject.toml` 的 `version`、`setup.py` 的 `version=`、`version.cmake` 的 `set_cann_package(pto-isa VERSION ...)`。漏改 `pyproject.toml` 会导致 `python3 -m build` 以旧版本元数据构建（PEP 621 元数据优先）；漏改 `setup.py` 则 `pip show` 显示旧版本；漏改 `version.cmake` 则 CANN 安装包版本与依赖检查（`CUR_MAJOR_MINOR_VER` 匹配）出错，出现「wheel 9.2 配 CANN 包 9.1」的漂移。

**练习 3**：`MANIFEST.in` 写了 `recursive-include include *`，把它删掉后 `python3 -m build --wheel` 的产物会变吗？`--sdist` 呢？

**答案**：wheel 不受影响——wheel 的内容由 `data_files`/`packages` 决定，`MANIFEST.in` 不参与；sdist 会缺失 `include/`（还缺 LICENSE/README），得到的源码包不再完整、无法从中重新构建。这正说明「MANIFEST.in 管 sdist，data_files 管 wheel」。

## 5. 综合实践

设计一份**「PTO 头文件发布检查单」**，把本讲三个模块串起来。假设你要把当前 HEAD 的头文件同时以 wheel 和 CANN run 包两种形态发布：

1. **版本一致性检查**：`grep -n "9.1.0" pyproject.toml setup.py version.cmake`，确认三处版本一致；不一致则先改齐。
2. **wheel 侧**：执行 4.3.4 的步骤 1–5，记录 wheel 文件名、`pto-inst.hpp` 的完整包内路径、头文件数量对账结果。
3. **CANN 包侧（源码阅读）**：通读 [cmake/package.cmake](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L12-L159) 的 `pack_built_in()`，写出该包安装后 `include`、`pkg_inc`、`share/info/pto_isa` 三个目录各自的内容来源；然后在有 CANN 环境的机器上执行 `bash build.sh --pkg`（默认 run 包）或 `bash build.sh --pkg --pkg-type=deb`，检查 `build_out/` 产物名是否形如 `cann-pto-isa_9.1.0_linux-<arch>.deb`（rpm/deb 文件名在 [cmake/package.cmake:L146-L148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/package.cmake#L146-L148) 显式设定）。无 CANN 环境则本步标注「待本地验证」。
4. **对照表**：在检查单末尾附上两张包的头文件落点对照表（wheel：`share/pto-isa/include`；CANN 包：`<arch>-linux/include` + `<arch>-linux/pkg_inc`），并各写一句「下游如何找到头文件」。

完成标志：这份检查单可以直接交给下一位发布执行者，照单操作即可完成一次无遗漏的双形态发布。

## 6. 本讲小结

- 顶层 `CMakeLists.txt` 是**打包工程**而非编译工程：它 FetchContent 拉取共享 cann-cmake（本地目录 / 本地 tar / 在线 git 三级来源），把 `include/` 与 `pkg_inc/` 安装成 CANN 风格布局，最终经 `set_cann_cpack_config` 产出 `.run`/`.rpm`/`.deb`；真正编译代码的是 tests/demos/kernels 下各自独立的 CMake 工程。
- 条件编译开关的组织：CPU 工程显式 `add_definitions(-D__CPU_SIM)`（C++20/23）；NPU 工程用 bisheng 编译器，`__CCE_AICORE__` 由 `-xcce` 自动预定义，架构由 `--cce-aicore-arch=dav-c220[-vec|-cube]` 决定（对应 `__NPU_ARCH__=2201`），Auto Mode 对应 `--cce-pto-auto-enable`，sim/npu 差异体现为链接 `runtime_camodel` 还是 `runtime`。
- `build.sh` 是「解析与执行分离」的状态标志分发器：`--run_all/--run_simple/--comm` 走 `tests/run_st.sh`，`--build` 走 `build_st.py`（a3→`tests/npu/a2a3/src/st`、a5→`tests/npu/a5/src/st`，`SOC_VERSION` 如 `Ascend910B1`/`Ascend950PR_9599`），`--cpu` 走 `run_cpu.py` + costmodel，`--pkg` 走 CPack；注意 build 默认 a5、run 默认 a3 的不对称。
- wheel 打包靠 `setup.py` 的 `data_files`：`os.walk("include")` 把每个子目录映射到 `share/pto-isa/<子路径>`，产出无 Python 代码的 `pto_isa-9.1.0-py3-none-any.whl`；`pyproject.toml` 声明构建后端，`MANIFEST.in` 只管 sdist。
- 版本号 9.1.0 在 `pyproject.toml` / `setup.py` / `version.cmake` 三处冗余，发版必须同步；wheel 的头文件落点是 `share/pto-isa/include`，与 CANN 包的 `<arch>-linux/include` + `pkg_inc` 是两套不同布局，验证时不可混用预期。

## 7. 下一步学习建议

本讲完成了「交付形态」层面的闭环。接下来两条路线任选：

- **测试体系路线（u10-l1）**：本讲多次看到 `build_st.py -t all` 与 `run_st.py -t <case> -g <gtest_filter>`，下一讲将深入 `tests/` 目录，解剖 ST 用例的「kernel / main / gen_data / CMakeLists」四件套结构与 `run_st.py` 的过滤机制，并动手为 TMul 新建一个用例。
- **仿真器内幕路线（u10-l2）**：若你更关心 `--sim` 背后 CAMS 仿真器与 PTO 自带 CPU 仿真（`NPUMemoryModel`、`cpu_stub` 桩机制）的关系与本讲 4.1 节的 `__CPU_SIM` 工程如何组织，可直接跳读 u10-l2。

此外建议把 [cmake/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/cmake/README.md) 与 [build.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/build.sh#L32-L42) 的 usage 输出放在手边，作为日常构建的速查表。
