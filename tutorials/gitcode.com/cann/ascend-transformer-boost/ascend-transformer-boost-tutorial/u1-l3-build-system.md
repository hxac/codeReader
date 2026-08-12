# 构建系统与编译运行

## 1. 本讲目标

本讲解决一个问题：**拿到 ATB 源码后，如何把它编译成可运行的加速库，并配好运行环境？**

学完后你应该能够：

- 看懂顶层 `CMakeLists.txt` 里每一个 `option()` 的作用，并知道它们如何影响最终产物（库的路径、是否带测试、是否带 Python 绑定等）。
- 说清楚 `bash scripts/build.sh` 这一条命令背后到底做了哪几件事（拉第三方、跑 cmake、装库、打包）。
- 理解 CXX11 ABI 是什么、为什么它决定了安装路径里有 `cxx_abi_0` / `cxx_abi_1` 两套目录，以及为什么切换 ABI 必须加 `--clean-first`。
- 会用 `source output/atb/set_env.sh` 配好运行时环境变量，并知道几个常用的运行时调优开关。

本讲是动手编译 ATB 的「说明书」，也是后续所有「跑示例 / 跑测试」讲义的前置依赖。

## 2. 前置知识

在进入源码之前，先用大白话建立几个概念。

**CMake 是什么？**
C/C++ 项目通常不手写编译命令，而是写一份 `CMakeLists.txt`「声明式」地告诉构建系统：要编译哪些源码、链接哪些库、产物放在哪。CMake 读取这份清单后，生成具体的构建指令（如 Makefile）。`option(名字 "描述" ON/OFF)` 是 CMake 里的「开关」，命令行可以用 `-D名字=ON` 来拨动它。

**什么是 ABI（Application Binary Interface）？**
ABI 是「二进制接口」的约定，规定了函数参数怎么传、`std::string`/`std::list` 等标准库对象在内存里长什么样。GCC 5 之后，C++ 标准库（libstdc++）有两套不兼容的 ABI，用一个宏 `_GLIBCXX_USE_CXX11_ABI` 切换：

- `=1`（新 ABI）：`std::string` 是「真对象」，指针 + 长度 + 容量都内嵌。
- `=0`（老 ABI）：`std::string` 是「指向实现对象的指针」。

两套 ABI 编译出来的 `.so` **不能互相链接**，否则会出现符号找不到、运行时崩溃等诡异错误。因此 ATB 把两套 ABI 的产物分别放到 `cxx_abi_0` 和 `cxx_abi_1` 两个目录里，井水不犯河水。PyTorch 本身也用某个 ABI 编译，ATB 必须和它一致才能配合使用。

**昇腾的 CANN toolkit 是什么？**
CANN 是昇腾 NPU 的基础软件栈（驱动 + 编译器 + 运行时 + 算子库）。ATB 是建在 CANN 之上的加速库，编译和运行都依赖 CANN 提供的头文件和动态库，环境变量 `ASCEND_HOME_PATH` 指向 CANN 的安装根目录。

**构建产物（output 目录）长什么样？**
编译成功后会在仓库根目录生成 `output/`，里面有：动态库 `libatb.so`（推理）、`libatb_train.so`（训练）、`libasdops.so`（算子包）、`set_env.sh`（环境变量脚本），以及一个 `.run` 自解压安装包。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt` | 顶层 CMake 入口：定义所有 `option()`、ABI 处理、安装路径、`add_subdirectory`、`install` 规则。 |
| `src/CMakeLists.txt` | 源码子目录的 CMake：用 `GLOB_RECURSE` 自动收集源码，定义 `atb` / `atb_train` 等库目标并安装到 `lib`。 |
| `scripts/build.sh` | **一键编译入口脚本**。解析命令行参数、自动探测 ABI、拉取并编译第三方（MKI 等）、调用 cmake 编译 ATB、打包成 `.run`。 |
| `scripts/set_env.sh` | 运行时环境变量脚本。计算 `ATB_HOME_PATH`、设置 `LD_LIBRARY_PATH`，并预置一批运行时调优开关。 |
| `docs/compile_and_build.md` | 官方编译说明文档，列出所有 `build.sh` 子命令与产物文件清单。 |

## 4. 核心概念与源码讲解

### 4.1 CMake 构建选项与产物布局

#### 4.1.1 概念说明

ATB 的构建系统是「**顶层 CMakeLists.txt 声明开关 + build.sh 脚本拨动开关**」的两层结构。顶层 `CMakeLists.txt` 只负责**声明**有哪些编译选项（用 `option()`）、源码在哪、产物装哪；真正决定「这次编译成什么样」的是 `build.sh` 传进来的 `-D` 参数。

理解这一层的关键有三点：

1. **每个 `option()` 都是一个独立的开关**：要不要测试、要不要 Python 绑定、要不要自定义算子、用哪种 ABI……它们默认值大多为 `OFF`，按需打开。
2. **ABI 开关会改变安装路径**：这是 ATB 最容易踩坑的地方。`USE_CXX11_ABI` 同时决定了编译宏 `_GLIBCXX_USE_CXX11_ABI` 和安装目录是 `cxx_abi_0` 还是 `cxx_abi_1`。
3. **CMake 自动收集源码**：`src/CMakeLists.txt` 用 `GLOB_RECURSE` 递归扫描，往目录里加 `.cpp` 就自动入编，不用手动登记（详见 u1-l2）。

#### 4.1.2 核心流程

顶层 CMake 的执行顺序：

```
cmake_minimum_required / project / set(CMAKE_CXX_STANDARD 17)   # 1. 基本环境
        │
        ▼
option(BUILD_TEST_FRAMEWORK ...) ... option(USE_CXX11_ABI ...)   # 2. 声明 13 个开关
        │
        ▼
设置 CXX_FLAGS（-Wall -Werror -fPIC ...） / Release 加 -s / Debug 加 -D_DEBUG
        │
        ▼
USE_CXX11_ABI ? → 设 _GLIBCXX_USE_CXX11_ABI 宏 + cxx_abi 变量
        │
        ▼
CMAKE_INSTALL_PREFIX = output/atb/cxx_abi_${cxx_abi}            # 3. 产物路径依赖 ABI
        │
        ▼
add_subdirectory(src)                                            # 4. 编译核心库
add_subdirectory(tests)      # 仅当开了测试开关
add_subdirectory(ops_customize)  # 仅当 BUILD_CUSTOMIZE_OPS
        │
        ▼
install(set_env.sh / ops_configs / libmki.so / include/atb)      # 5. 安装产物
```

注意第 3 步和第 4、5 步的因果关系：**ABI 决定了 install 前缀**，所以同一份源码用不同 ABI 编译两次，产物会分别落到 `output/atb/cxx_abi_0` 和 `output/atb/cxx_abi_1`，互不覆盖。

#### 4.1.3 源码精读

**(1) 全部编译选项的声明**

13 个 `option()` 集中在顶层 CMake 开头：

[CMakeLists.txt:21-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L21-L33) — 用 `option(变量名 "描述" 默认值)` 声明所有开关，这一段定义了 ATB 的全部可配置面。

关键几项含义：

| 选项 | 默认 | 作用 |
|------|------|------|
| `BUILD_PYBIND` | ON | 构建 Python 绑定（pybind11），关掉就不生成 Python 接口 |
| `BUILD_TEST_FRAMEWORK` | OFF | 构建测试框架（C++ 算子测试） |
| `USE_UNIT_TEST` / `USE_PYTHON_TEST` / `USE_FUZZ_TEST` 等 | OFF | 各类测试开关 |
| `BUILD_CUSTOMIZE_OPS` | OFF | 编译用户自定义算子目录 `ops_customize` |
| `USE_CXX11_ABI` | ON | C++11 ABI 开关，**影响编译宏和安装路径** |
| `USE_ASAN` | OFF | 开启 AddressSanitizer 内存检测 |
| `USE_MSSANITIZER` | OFF | 开启昇腾内存消毒器 |

紧接着一段 `message(STATUS ...)` 会把这些值全部打印出来，编译时终端能看到，方便确认实际生效的开关：

[CMakeLists.txt:35-47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L35-L47) — 把每个 option 的当前值打印到 cmake 配置日志。

**(2) ABI 如何同时影响「编译宏」和「安装路径」**

这是 ATB 构建系统最核心的设计点：

[CMakeLists.txt:70-79](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L70-L79) — 根据 `USE_CXX11_ABI` 设置 `_GLIBCXX_USE_CXX11_ABI` 宏并推导出 `cxx_abi` 变量，进而决定 `CMAKE_INSTALL_PREFIX`。

也就是说，同一个开关同时做了两件事：

- 给编译器加宏 `-D_GLIBCXX_USE_CXX11_ABI=1`（或 0），让 libstdc++ 走对应 ABI。
- 把安装目录设成 `output/atb/cxx_abi_1`（或 `cxx_abi_0`）。

这样两套 ABI 的库天然分目录存放，`set_env.sh` 也能按 ABI 找到正确的那一套（见 4.3）。

**(3) 编译选项：Release 去符号、Debug 留调试**

[CMakeLists.txt:61-65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L61-L65) — `Release` 类型用 `-s` 给链接器去掉符号表（库更小），非 Release 则加 `-D_DEBUG` 宏（便于调试）。`build.sh` 默认就是 `Release`，加 `--debug` 才切到 Debug。

另外全局给了一套严格的警告策略 `-Wall -Wextra -Werror`（[CMakeLists.txt:53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L53)），意味着**任何告警都会导致编译失败**——这也是为什么社区 PR 里常看到针对 `-Werror` 的修复提交。

**(4) 子目录与安装规则**

[CMakeLists.txt:106-121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L106-L121) — 条件性地把 `tests`、`ops_customize` 加入编译，并把 `set_env.sh`、`ops_configs`、`libmki.so`、`include/atb` 头文件安装到产物目录。

注意 `add_subdirectory(src)` 是**无条件**的——核心库永远要编；`tests` 和 `ops_customize` 是**条件性**的，由对应开关控制。

**(5) src 子目录：自动收集源码并产出四个库**

[src/CMakeLists.txt:20-30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L20-L30) — `GLOB_RECURSE` 递归扫描 `ops_infer` / `ops_train` / `ops_common` / `atb` 框架目录下所有 `.cpp`，分别组装成 `atb`（推理，动态+静态）、`atb_train`（训练，动态+静态）四个库目标。

这承接了 u1-l2 讲过的「往目录加 `.cpp` 即自动入编」的约定：扫描发生在 cmake **配置阶段**，所以新增源码文件后需要重新运行 cmake（`build.sh` 每次都会重跑 cmake，所以正常使用不会出问题）。

[src/CMakeLists.txt:42](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/CMakeLists.txt#L42) — 四个库最终 `install` 到 `lib` 子目录（即 `output/atb/cxx_abi_X/lib`）。

#### 4.1.4 代码实践

**实践目标：** 不实际编译，仅通过阅读 CMake 文件，预测一次「默认 Release 编译」会启用哪些开关、产物落在哪个目录。

**操作步骤：**

1. 打开顶层 [CMakeLists.txt](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt)，读 21–33 行的 `option()`，记录每个选项的**默认值**。
2. 假设你不传任何 `-D` 参数，回答：
   - `BUILD_PYBIND` 生效吗？（提示：看默认值）
   - `tests` 目录会被编译吗？（提示：看 106 行的条件，所有测试 option 默认都是 OFF）
   - 安装前缀是 `output/atb/cxx_abi_0` 还是 `cxx_abi_1`？（提示：`USE_CXX11_ABI` 默认 ON）
3. 用下面的命令在仓库里验证你的判断（只读，不编译）：

```bash
# 列出顶层 CMake 声明的所有 option 及默认值
grep -n '^option(' CMakeLists.txt
```

**需要观察的现象：** 命令会打印 13 行 `option(...)`，每行第三个字段就是默认值。

**预期结果：** 默认情况下 `BUILD_PYBIND=ON`、所有测试开关 `OFF`、`USE_CXX11_ABI=ON`，因此默认产物路径是 `output/atb/cxx_abi_1`。这一步只读不写，任何环境都能跑，无需 NPU。

#### 4.1.5 小练习与答案

**练习 1：** 如果只想编出 C++ 推理库，完全不碰 Python，应该关掉哪个 option？通过什么命令传给 cmake？

> **答案：** 关掉 `BUILD_PYBIND`。在 `build.sh` 层面没有直接开关，但 cmake 层面可以 `-DBUILD_PYBIND=OFF`。不过实际更常用的是反过来——需要 Python 时用 `bash scripts/build.sh --torch_atb`（见 4.2）。

**练习 2：** 为什么顶层 CMake 把安装前缀写成 `output/atb/cxx_abi_${cxx_abi}` 而不是固定路径？

> **答案：** 因为 `_GLIBCXX_USE_CXX11_ABI` 的两种取值产物互不兼容，必须物理隔离。用变量拼路径，保证编两次（一次 ABI=0，一次 ABI=1）能共存于同一棵 `output` 树下而不互相覆盖，`set_env.sh` 再按当前 ABI 选择正确那套。

**练习 3：** `-Werror`（CMakeLists.txt:53）对开发者意味着什么？

> **答案：** 把所有告警当错误，有任何告警就编译失败。好处是强制代码质量、避免告警堆积；代价是不同 GCC 版本的「新告警」会让原本能编的代码突然失败，社区需要持续修这类告警（例如最近一次提交 `2c7c1995` 修的就是 GCC 12 的 `-Werror=array-compare`）。

---

### 4.2 build.sh 一键编译流程

#### 4.2.1 概念说明

直接 `cmake && make` 是编不出 ATB 的——因为它还依赖几个**外部第三方库**（最关键的是 MKI，即 Mind-KernelInfra 算子基础设施），这些库要么要从远端拉取、要么要先用相同 ABI 编译一遍。`scripts/build.sh` 就是把这套「准备依赖 + cmake 编译 + 安装 + 打包」的全流程串起来的**总指挥**。

它的设计思路是：用**第一个位置参数**选「编译模式」（default / unittest / pythontest / clean …），用后续 `--xxx` **开关**微调（`--debug` / `--use_cxx11_abi=0` / `--clean-first` …）。模式决定「编什么 + 编完跑什么」，开关决定「怎么编」。

`build.sh` 还做了一件很贴心的事：**自动探测 ABI**。如果你不显式指定 `--use_cxx11_abi`，它会调用 Python 去问 PyTorch 是用哪种 ABI 编的，然后让 ATB 跟它保持一致——这避免了手滑选错 ABI 导致链接失败。

#### 4.2.2 核心流程

`bash scripts/build.sh [模式] [--开关 ...]` 的总流程：

```
fn_main 解析参数
   │   ├─ 第一个参数 → 模式（arg1：default/unittest/...）
   │   └─ 剩余参数 → 开关（--use_cxx11_abi / --debug / --clean-first / ...）
   ▼
fn_init_env                         # ① 自动探测 ABI（问 PyTorch）
   │
   ▼
按模式拼 COMPILE_OPTIONS（-DUSE_UNIT_TEST=ON 等）
   │
   ▼
case 模式 in
  default)     fn_build → generate_atb_version_info → fn_make_run_package ;;
  unittest)    fn_build → fn_run_unittest ;;
  clean)       删 build/ output/ 3rdparty/ ;;
  ...
esac
```

其中 `fn_build` 是真正的编译核心，它内部又分两步（正是文档说的「①拉取算子库/MKI并编译 ②加速库的编译」）：

```
fn_build
  ├─ fn_build_3rdparty_for_compile
  │     ├─ fn_build_nlohmann_json   # JSON 库
  │     ├─ fn_build_mki             # ① 拉取并编译 MKI（算子基础设施）
  │     ├─ fn_build_catlass         # 矩阵运算库
  │     ├─ fn_build_cann_dependency # 软链 CANN 编译器
  │     └─ fn_build_tbe_dependency  # 拷贝 libtbe_adapter.so
  │
  ├─ cmake $CODE_ROOT $COMPILE_OPTIONS          # ② 配置 ATB
  ├─ cmake --build . --parallel                  #    编译 ATB
  └─ cmake --install .                           #    安装到 output/atb/cxx_abi_X
       │
       （若 --torch_atb 且 ABI=1）
       └─ fn_build_torch_atb → fn_gen_atb_whl    # 额外编 Python 包
```

#### 4.2.3 源码精读

**(1) 两种参数：模式列表 vs 开关列表**

脚本一开头就列出了所有合法参数，分成两组：

[scripts/build.sh:41-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L41-L44) — `BUILD_OPTION_LIST` 是「编译模式」（default/unittest/clean/…），`BUILD_CONFIGURE_LIST` 是「编译开关」（`--use_cxx11_abi`/`--asan`/`--clean-first`/…）。`fn_main` 就是据此把命令行参数分流到 `arg1`（模式）和 case 分支（开关）。

**(2) 自动探测 ABI（fn_init_env）**

这是避免「手滑选错 ABI」的关键逻辑：

[scripts/build.sh:445-458](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L445-L458) — 如果用户没显式给 `--use_cxx11_abi`，就用 `python3 -c 'import torch; print(torch.compiled_with_cxx11_abi())'` 去问 PyTorch，把 ATB 的 ABI 设成和 PyTorch 一致；如果根本没装 torch，则默认 `ON`（新 ABI）。

这段同时也把 `PYTHON_INCLUDE_PATH`、`PYTORCH_INSTALL_PATH` 等路径探测好，因为顶层 CMake 的 `include_directories` / `link_directories` 要用到它们。

**(3) 准备第三方依赖（MKI 是重头戏）**

[scripts/build.sh:317-328](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L317-L328) — `fn_build_3rdparty_for_compile` 依次拉取/编译 nlohmann_json、MKI、catlass、CANN 依赖、tbe adapter。其中 MKI 是 ATB 的算子基础设施（Kernel 注册框架，详见 u3-l4），必须先编出来。

[scripts/build.sh:149-163](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L149-L163) — `fn_build_mki` 从 `gitcode.com/cann/ascend-boost-comm.git` 克隆 MKI 源码（按当前分支或默认 master），随后会用**和 ATB 相同的 ABI** 去编它（166–170 行 `--use_cxx11_abi` 透传）。这正是「①拉取算子库/MKI并编译」那一步。

**(4) 真正的 cmake 编译三连**

[scripts/build.sh:557-559](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L557-L559) — 标准 cmake 三连：`cmake 配置 → cmake --build 编译 → cmake --install 安装`。产物落到由 ABI 决定的 `output/atb/cxx_abi_X`。

注意 550–552 行有个小优化：如果系统装了 `ccache`，会自动启用做编译缓存，加速重复编译。

**(5) 打包成 .run 自解压安装包**

[scripts/build.sh:501-529](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L501-L529) — `fn_make_run_package` 把 `output/` 内容用 CANN 自带的 `makeself` 工具打成 `Ascend-cann-atb_{version}_linux-{arch}.run` 自解压包，用户拷到目标机器 `./xxx.run --install` 即可安装，不必再带源码。

**(6) 各模式如何组合（default 为例）**

[scripts/build.sh:860-869](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L860-L869) — 先把 `CMAKE_BUILD_TYPE / USE_CXX11_ABI / USE_ASAN / USE_MSSANITIZER` 拼进 `COMPILE_OPTIONS`，再在 `case` 里按模式追加。`default` 模式 = `fn_build`（编库）+ `generate_atb_version_info`（写 version.info）+ `fn_make_run_package`（打包）；`unittest` 模式则追加 `-DUSE_UNIT_TEST=ON` 并在编完后调用 `fn_run_unittest` 跑测试。

**(7) --clean-first：切换 ABI 的安全阀**

[scripts/build.sh:831-835](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L831-L835) — `--clean-first` 在编译前**先删掉** `build/`（cmake 缓存）、`output/`（旧产物）、`3rdparty/`（旧 MKI 等依赖）三个目录。为什么切换 ABI 必须加它，详见综合实践。

#### 4.2.4 代码实践

**实践目标：** 通过纯文本跟踪，把「`bash scripts/build.sh`（无参数）」这一条命令还原成具体动作序列。

**操作步骤：**

1. 在 `scripts/build.sh` 里定位 `fn_main`（755 行起），确认无参数时 `arg1` 落到 `default`。
2. 跟进 `default` 分支（864 行），列出它依次调用的三个函数。
3. 进入 `fn_build`（531 行），列出它在 `SKIP_BUILD=OFF` 时调用的依赖准备函数（547 行 `fn_build_3rdparty_for_compile`）和 cmake 三连（557–559 行）。
4. 用一条只读命令验证你的函数调用链：

```bash
# 看 default 分支和 fn_build 分别调用了哪些函数
grep -n 'fn_build\|generate_atb_version_info\|fn_make_run_package' scripts/build.sh | head
```

**需要观察的现象：** 你应该能在 `default)` 分支下看到 `fn_build`、`generate_atb_version_info`、`fn_make_run_package` 三个调用。

**预期结果：** 「无参数 build.sh」的动作序列是：探测 ABI → 准备第三方（含编 MKI）→ cmake 配置/编译/安装 ATB → 写 version.info → 打 .run 包。**待本地验证**：在有 CANN toolkit + NPU 的机器上实际跑一次，可看到终端先打印 `mki build by Develop mode`，再打印一系列 `cmake` 输出。

#### 4.2.5 小练习与答案

**练习 1：** `bash scripts/build.sh unittest` 和 `bash scripts/build.sh default` 编出来的库本身有区别吗？区别在哪一步？

> **答案：** 库本身的源码一样，但 `unittest` 模式额外加了 `-DUSE_UNIT_TEST=ON`，会把 `tests/` 目录下的单元测试可执行文件（如 `atb_unittest`）也编出来，并在编译后**立即运行**它们（`fn_run_unittest`）。`default` 不编测试也不跑测试。

**练习 2：** 如果机器上没装 PyTorch，`fn_init_env` 会怎样选 ABI？

> **答案：** 见 build.sh:447–451，探测到 `torch_not_exist` 时直接把 `USE_CXX11_ABI=ON`。因为没 PyTorch 就不存在「和谁对齐」的问题，默认走新 ABI。

**练习 3：** 为什么 `fn_build` 第一行就检查 `ASCEND_HOME_PATH` 是否为空（533–536 行）？

> **答案：** ATB 编译强依赖 CANN toolkit 的头文件和库，`ASCEND_HOME_PATH` 是 CANN 的安装根。顶层 CMake 的 `include_directories` / `link_directories`（CMakeLists.txt:91–104）大量引用 `$ENV{ASCEND_HOME_PATH}`。没 source CANN 的 `set_env.sh` 就来这里编，必然找不到头文件/库，所以脚本提前报错并提示「please source cann set_env.sh first」。

---

### 4.3 set_env.sh 环境变量配置

#### 4.3.1 概念说明

编完之后库躺在 `output/atb/cxx_abi_X/lib`，但你的程序怎么找到它？靠**环境变量**。ATB 提供了 `set_env.sh`，`source` 一下就把 `ATB_HOME_PATH`、`LD_LIBRARY_PATH`、`PATH` 等设好。

这里有个关键点：**有两份 `set_env.sh`**。

- 源码里的 `scripts/set_env.sh` 是**模板**，逻辑都在里面。
- 编译安装后，它被拷贝到 `output/atb/set_env.sh`（顶层 CMake 的 install 规则 [CMakeLists.txt:118](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L118)），README 让你 `source output/atb/set_env.sh`。

它同样会**自动探测 ABI**：你没指定时它去问 PyTorch，然后让 `ATB_HOME_PATH` 指向正确的 `cxx_abi_0` 或 `cxx_abi_1` 目录。除了「让程序找到库」，`set_env.sh` 还**预置了一批运行时调优开关**（如 workspace 内存分配算法、kernel 缓存个数、是否每个 kernel 都同步等），这些是后面 u7（性能与调试）讲义的前导。

#### 4.3.2 核心流程

`source output/atb/set_env.sh [--cxx_abi=0|1]` 的执行流程：

```
get_cxx_abi_option "$@"               # 解析 --cxx_abi=0/1；未给则问 PyTorch
        │
        ▼
atb_path = dirname(set_env.sh)         # 即 output/atb
        │
        ▼
export ATB_HOME_PATH = atb_path/cxx_abi_${cxx_abi}
export LD_LIBRARY_PATH = $ATB_HOME_PATH/lib:...:$LD_LIBRARY_PATH
export PATH = $ATB_HOME_PATH/bin:$PATH
        │
        ▼
export 一批 ATB_* 运行时调优开关（默认值）
```

#### 4.3.3 源码精读

**(1) ABI 自动探测函数**

[scripts/set_env.sh:12-40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/set_env.sh#L12-L40) — `get_cxx_abi_option` 先看命令行有没有 `--cxx_abi=0/1`；没有就调 `torch.compiled_with_cxx11_abi()` 问 PyTorch；连 torch 都没有就默认 `cxx_abi=1`。逻辑和 `build.sh` 的 `fn_init_env` 如出一辙，保证「编译时用的 ABI」和「运行时选的 ABI」判定方式一致。

**(2) 三个核心路径变量**

[scripts/set_env.sh:47-52](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/set_env.sh#L47-L52) — 设置三大路径：

- `ATB_HOME_PATH`：指向具体某套 ABI 的安装目录（`output/atb/cxx_abi_X`），是其它变量拼接的基础。
- `LD_LIBRARY_PATH`：把 `lib`（动态库）、`examples`、`tests/atbopstest` 加进来，让 `ld` 能找到 `libatb.so` 等。
- `PATH`：把 `bin` 加进来，方便直接调用 `atb_unittest` 等可执行文件。

注意 47 行还有一道安全检查：脚本名必须以 `set_env.sh` 结尾才生效，避免被误 source。

**(3) 运行时调优开关（预置默认值）**

[scripts/set_env.sh:54-63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/set_env.sh#L54-L63) — 这一批 `export ATB_*` 是 ATB 的运行时行为开关，几个值得记住的：

| 变量 | 默认 | 含义 |
|------|------|------|
| `ATB_WORKSPACE_MEM_ALLOC_ALG_TYPE` | 1 | workspace 内存分配算法：0 暴力、1 block 分配、2 有序 heap、3 block 合并（SOMAS 退化版） |
| `ATB_OPSRUNNER_KERNEL_CACHE_LOCAL_COUNT` / `_GLOABL_COUNT` | 1 / 5 | kernel 缓存个数，影响 Tiling 复用（范围 1~1024） |
| `ATB_STREAM_SYNC_EVERY_KERNEL_ENABLE` | 0 | 是否每个 Kernel 执行后都做流同步（调试用，开了会变慢） |
| `ATB_MATMUL_SHUFFLE_K_ENABLE` | 1 | matmul 的 Shuffle-K 优化，默认开 |
| `LCCL_DETERMINISTIC` | 0 | LCCL 通信是否保序（确定性 AllReduce） |

这些变量对接 u1-l5（Context/执行流）和 u7（性能 Profiling）的内容，现在先有个印象即可。

> 文档 [docs/compile_and_build.md:83-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/compile_and_build.md#L83-L85) 说明：`set_env.sh` 设的是**进程级**环境变量，用户进程结束后自动失效，不影响系统全局。

#### 4.3.4 代码实践

**实践目标：** 体验 ABI 自动探测如何改变 `ATB_HOME_PATH`。

**操作步骤：**

1. 假设你已完成一次默认编译，进入仓库根目录。
2. 用显式参数分别 source 两次（注意：source 会在当前 shell 改环境变量，建议在子 shell 里做）：

```bash
# 在子 shell 中观察 ABI=1 的情况
( source output/atb/set_env.sh --cxx_abi=1; echo "ABI=1 -> $ATB_HOME_PATH" )

# 在子 shell 中观察 ABI=0 的情况
( source output/atb/set_env.sh --cxx_abi=0; echo "ABI=0 -> $ATB_HOME_PATH" )
```

3. 不带参数再试一次，看它探测成什么：

```bash
( source output/atb/set_env.sh; echo "auto -> $ATB_HOME_PATH" )
```

**需要观察的现象：** 前两次 `ATB_HOME_PATH` 末尾分别是 `cxx_abi_1` 和 `cxx_abi_0`；第三次取决于你装的 PyTorch 用哪种 ABI。

**预期结果：** 同一份 `output/atb/` 下，`set_env.sh` 能按 ABI 指向不同的子目录。**待本地验证**：如果还没编译出 `output/` 目录，可先只读地看脚本逻辑（12–52 行）确认路径推导，等编译后再实际 source。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `ATB_HOME_PATH` 要拼上 `cxx_abi_${cxx_abi}`，而不是直接指向 `output/atb`？

> **答案：** 因为两套 ABI 的库互不兼容且共存于 `output/atb/cxx_abi_0` 和 `cxx_abi_1`。运行时必须明确选一套，否则 `LD_LIBRARY_PATH` 可能链到错误 ABI 的 `libatb.so`，导致与 PyTorch 链接失败。

**练习 2：** `ATB_STREAM_SYNC_EVERY_KERNEL_ENABLE=1` 什么时候有用？

> **答案：** 调试时。它强制每个 Kernel 执行后都做流同步，让错误暴露在确切的位置，便于定位；但会严重拖慢速度，生产环境必须保持默认 0。这呼应了 u1-l1 讲过的「Host 异步下发」模型。

**练习 3：** 同样是「探测 ABI」，`build.sh` 的 `fn_init_env` 和 `set_env.sh` 的 `get_cxx_abi_option` 为什么要各写一份？

> **答案：** 一个在**编译期**（决定库怎么编、装哪个目录），一个在**运行期**（决定程序加载哪个目录的库）。两者用同样的探测逻辑（问 `torch.compiled_with_cxx11_abi()`），保证编译和运行选到同一套 ABI，避免错配。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（对应本讲的 practice_task）。

### 任务：列出 5 个编译选项 + 解释为什么切换 ABI 必须 --clean-first

**第一部分：列出至少 5 个编译选项及其作用。**

阅读 [CMakeLists.txt:21-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/CMakeLists.txt#L21-L33) 与 [docs/compile_and_build.md:28-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/compile_and_build.md#L28-L54)，整理一张表（参考答案）：

| 编译选项 / 开关 | 怎么给 | 作用 |
|------|------|------|
| `USE_CXX11_ABI` | `--use_cxx11_abi=0/1` | 切换 C++11 ABI，同时改变编译宏与安装目录 `cxx_abi_0/1` |
| `BUILD_PYBIND` | cmake `-DBUILD_PYBIND=OFF` | 是否构建 Python 绑定 |
| `--debug` | `build.sh --debug` | 切到 Debug 构建类型，加 `-D_DEBUG`、不去符号 |
| `--asan` | `build.sh --asan` | 开 AddressSanitizer，强制 Debug |
| `BUILD_CUSTOMIZE_OPS` | `build.sh customizeops` | 编译 `ops_customize` 自定义算子目录 |
| `--torch_atb` | `build.sh --torch_atb` | 额外编 pybind11 并生成 torch_atb 的 whl 包 |
| `BUILD_TEST_FRAMEWORK` | `build.sh testframework` | 编译测试框架 |

（你只需任选 5 个，说出作用即可。）

**第二部分：解释为什么切换 ABI 必须加 `--clean-first`。**

对照 [scripts/build.sh:831-835](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L831-L835) 和 README 的说明（[README.md:143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L143)），切换 ABI 不清理会有三类残留导致出错：

1. **CMake 缓存残留**：`build/` 里的 `CMakeCache.txt` 缓存了上一次的 `CMAKE_INSTALL_PREFIX` 和 `_GLIBCXX_USE_CXX11_ABI`。不删 `build/`，cmake 会复用旧值，新指定的 ABI 不生效。
2. **libtbe_adapter.so 拷贝残留**：`fn_build_tbe_dependency` 从 nnal 把 `libtbe_adapter.so` 拷到 `output/atb/cxx_abi_X/lib`。若旧 ABI 的拷贝还在，新 ABI 编译时可能链到「用旧 ABI 编的 adapter」，导致 ABI 不匹配或链接错误（脚本在 `fn_copy_tbe_adapter` 287–294 行还专门做了 ABI 一致性校验来兜底）。
3. **MKI 第三方库残留**：`3rdparty/mki/lib/libmki.so` 是用旧 ABI 编的。ATB 链接它时会因为 ABI 不一致而失败。

`--clean-first` 一次性删掉 `build/`、`output/`、`3rdparty/` 三个目录，让 `fn_build_mki`、`fn_build_tbe_dependency`、cmake 全部从干净状态重来，从而保证整条工具链 ABI 一致。同理，如果安装/更换 PyTorch 导致自动探测出的 ABI 变了，也要 `--clean-first` 全量重编。

**验收：** 把上面两部分用自己的话写成一段笔记。若手头有编译环境，按下面的顺序亲自验证一次（**待本地验证**）：

```bash
# 1) 先用默认 ABI（假设探测为 1）编一次
bash scripts/build.sh
ls output/atb/          # 应看到 cxx_abi_1

# 2) 切到 ABI=0，故意不加 --clean-first，观察可能的报错
bash scripts/build.sh --use_cxx11_abi=0
# 预期：可能因缓存/残留出现 ABI 不匹配或链接错误

# 3) 正确做法：切 ABI 前先清理
bash scripts/build.sh --use_cxx11_abi=0 --clean-first
ls output/atb/          # 应只看到 cxx_abi_0（output 被清过）
```

## 6. 本讲小结

- ATB 构建是「**顶层 CMake 声明开关 + build.sh 拨动开关**」两层结构；13 个 `option()` 决定编什么、怎么编、装哪里。
- `USE_CXX11_ABI` 是最关键的开关，它**同时**决定编译宏 `_GLIBCXX_USE_CXX11_ABI` 和安装目录 `output/atb/cxx_abi_0|1`，两套 ABI 产物物理隔离。
- `bash scripts/build.sh` 是总指挥，背后做四件事：**自动探测 ABI → 拉取并编译第三方（MKI 等）→ cmake 编译安装 ATB → 打 .run 包**。
- `fn_init_env` 通过 `torch.compiled_with_cxx11_abi()` 自动让 ATB 的 ABI 与 PyTorch 对齐，避免手滑选错。
- **切换 ABI 必须 `--clean-first`**，因为 CMake 缓存、`libtbe_adapter.so` 拷贝、MKI 库三处残留都会导致 ABI 不匹配。
- 运行时 `source output/atb/set_env.sh` 设置 `ATB_HOME_PATH` / `LD_LIBRARY_PATH`，并预置一批运行时调优开关（内存分配算法、kernel 缓存、同步策略等）。

## 7. 下一步学习建议

学会了「怎么编、怎么配环境」之后，建议：

1. **下一讲 u1-l4（核心数据类型）**：进入 `include/atb/types.h`，认识 `Tensor`、`VariantPack`、`Status` 等数据结构——这是调用算子前必须理解的「数据载体」。
2. **想要跑通第一个示例**：可以直接跳到 u2-l1（C++ 单算子 demo）或 u2-l2（Python torch_atb），把本讲编出的库真正用起来；遇到 ABI 链接错误时，回头查本讲的 `--clean-first` 部分。
3. **想深入构建细节**：阅读 [docs/compile_and_build.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/compile_and_build.md) 的「Key ATB Files」一节，对照 `output/` 目录里实际生成的文件清单。进阶的编译选项（ASAN/MSAN、测试开关的组合）会放在 u7-l4 讲。
