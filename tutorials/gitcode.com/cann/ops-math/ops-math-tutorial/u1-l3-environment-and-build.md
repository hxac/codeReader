# 环境准备与源码编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 搭建（或选择）编译 ops-math 所需的环境：NPU 驱动、CANN toolkit 包、基础依赖。
2. 看懂 `build.sh` 这个总入口的执行流程：它 source 了哪些子脚本、按什么顺序调用了什么函数。
3. 理解 `scripts/` 目录下各构建子脚本的分工：参数解析、全局配置、CMake 参数组装、库编译、打包、测试、样例、脚手架生成。
4. 理解根 `CMakeLists.txt` 中的关键 option（`ENABLE_TEST`、`ENABLE_BINARY`、`ENABLE_PACKAGE` 等）与 shell 层开关的对应关系。
5. 知道编译产物落在哪里（`build/` 与 `build_out/`），并独立完成一次完整编译（或仅编译 `add_example`）。

## 2. 前置知识

在动手编译之前，先通俗地理解几个概念：

- **Host 与 Device**：Host 指 CPU 侧（负责调度、形状推导、切分策略），Device 指 NPU 上的计算单元（AI Core / AI CPU，负责真正的计算）。因此编译产物既有 Host 侧的动态库（如 `libophost_math.so`、`libopapi_math.so`），也有 Device 侧的算子二进制（kernel bin）。
- **编译态 vs 运行态**：这是官方文档里非常重要的区分。
  - **编译态**：只编译不运行，只需安装 CANN toolkit 包，不需要 NPU 驱动。适合没有昇腾硬件、只想读代码/验证编译的开发者。
  - **运行态**：要真正在 NPU 上跑算子，需要驱动固件 + CANN toolkit 包 + CANN ops 包三者齐备。
- **CANN**：昇腾异构计算架构的软件栈总称。ops-math 的源码与 CANN 版本严格配套（第一讲已强调按 release 标签拉取源码），编译时它会把已安装的 CANN 包（通过 `ASCEND_HOME_PATH` 环境变量定位）作为依赖来使用。
- **CMake**：一个跨平台的构建系统生成器。它根据 `CMakeLists.txt` 里的声明生成具体的 make/ninja 构建脚本。ops-math 的 shell 层（build.sh）本质上是在「组装 CMake 参数、再调用 cmake」。
- **soc（System on Chip）**：NPU 芯片型号，如 `ascend910b`、`ascend950`。kernel 二进制是按芯片型号编译的，所以编译 kernel 时必须用 `--soc=` 指定型号。
- **run 包**：华为软件常用的自解压安装包格式（`.run` 后缀），编译产物最终会被打成一个 `cann-xxx-ops-math_xxx.run` 包用于安装。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `build.sh` | 构建总入口：source 全部子脚本，`main()` 里按顺序编排各构建阶段 |
| `scripts/build.conf.sh` | 全局配置：支持的目标列表、支持的芯片列表、合法参数白名单、CANN 头文件/库路径 |
| `scripts/build_options.sh` | 命令行参数解析与校验（`checkopts`）、帮助信息（`usage`）、参数互斥检查 |
| `scripts/build_cmake.sh` | 把 shell 变量翻译成 CMake 参数（`assemble_cmake_args`），并执行 `cmake` 初始化（`cmake_init`） |
| `scripts/build_lib.sh` | 真正的编译函数：`build_lib`（编译 so 库）、`build_binary`（编译 kernel 二进制）、`build_package`（打 run/rpm/deb 包） |
| `scripts/build_clean.sh` | 清理函数（`clean_build` / `clean_build_out` 等） |
| `scripts/build_ut.sh` | 单元测试构建与执行（`-u` 相关，第五单元详讲） |
| `scripts/build_example.sh` | 样例编译执行（`--run_example` 相关，下一讲会用到） |
| `scripts/build_genop.sh` | 新算子目录脚手架生成（`--genop`，第五单元详讲） |
| `CMakeLists.txt` | 根 CMake 工程声明：option 开关、编译模式、子目录挂载、打包入口 |
| `docs/zh/install/quick_install.md` | 官方环境部署文档：CANNLab / Docker / 手动 / Spack 四种方式 |
| `docs/zh/install/build.md` | 官方 build 参数说明文档 |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**build.sh（总入口）**、**scripts 构建脚本分工**、**CMake 构建体系与产物**。在进入它们之前，先用一个模块讲清环境准备。

### 4.1 环境准备：安装 CANN 与基础依赖

#### 4.1.1 概念说明

ops-math 不是独立编译的「裸项目」——它编译时要引用已安装 CANN 包里的头文件（如 `aclnn` 头文件、`graph` 头文件）和库。因此**先装 CANN、再编译本仓**是硬前提。同时，kernel 侧代码要用毕昇编译器（CANN 包自带）编译，Host 侧代码用系统 gcc 编译，这些都要求基础依赖版本达标。

#### 4.1.2 核心流程

官方提供四种搭建方式，按「有没有昇腾设备」选择：

1. **CANNLab**（无设备）：一站式在线开发平台，环境预装好，浏览器里写代码。
2. **Docker**（有设备）：拉取预集成 CANN 的镜像，把宿主机 NPU 设备挂载进容器。
3. **手动安装**（有设备）：装驱动 → 装 CANN toolkit 包（编译态必需）→ 装 CANN ops 包（运行态必需）→ 装基础依赖。
4. **Spack**：包管理器自动安装 CANN 与编译依赖。

手动安装路径下的关键命令：

```bash
# 编译态必需：CANN toolkit
bash ./Ascend-cann-toolkit_${cann_version}_linux-${arch}.run --install --install-path=${install_path}

# 基础依赖一键安装（python/gcc/cmake/dos2unix/make/patch 等）
bash install_deps.sh
pip3 install -r requirements.txt

# 让 ASCEND_HOME_PATH 等环境变量生效
source /usr/local/Ascend/cann/set_env.sh
```

#### 4.1.3 源码精读

环境安装的完整步骤在官方文档中：

- [docs/zh/install/quick_install.md:9-L18](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/quick_install.md#L9-L18) —— 文档开篇即区分「编译态」（只装 toolkit）与「运行态」（驱动 + toolkit + ops 包），并用表格对比 CANNLab / Docker / 手动 / Spack 四种安装方式的适用场景。
- [docs/zh/install/quick_install.md:137-L172](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/quick_install.md#L137-L172) —— 基础依赖清单（python >= 3.7.0、gcc/g++ >= 7.3.0、cmake >= 3.16.0、pigz、dos2unix、make、patch、googletest），以及用 `install_deps.sh` 一键安装、`pip3 install -r requirements.txt` 安装 python 依赖的步骤。
- [docs/zh/install/quick_install.md:234-L243](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/quick_install.md#L234-L243) —— 环境变量生效命令：`source /usr/local/Ascend/cann/set_env.sh`。这一步会在 shell 中导出 `ASCEND_HOME_PATH`，后面的构建脚本正是靠它找到 CANN 安装位置。

`set_env.sh` 的效果可以在构建配置脚本中直接看到——`build.conf.sh` 基于这些环境变量拼出 CANN 头文件与库的搜索路径：

- [scripts/build.conf.sh:32-L40](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build.conf.sh#L32-L40) —— 从 `${ASCEND_HOME_PATH}/include`、`${ASCEND_HOME_PATH}/lib64` 等路径导出 `INCLUDE_PATH`、`EAGER_LIBRARY_PATH` 等变量。如果没 source `set_env.sh`，这些路径全部为空，编译必然失败。这就是「先装 CANN、配好环境变量，再编译」的源码级证据。

#### 4.1.4 代码实践

1. **实践目标**：确认本机（或你选定的开发环境）满足编译态条件。
2. **操作步骤**：
   1. 检查 CANN 是否安装：`ls /usr/local/Ascend/cann/`（或你的安装路径）。
   2. source 环境变量：`source /usr/local/Ascend/cann/set_env.sh`。
   3. 验证：`echo $ASCEND_HOME_PATH` 应输出 CANN 安装路径；`cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_toolkit_install.info` 可查 toolkit 版本（参照 quick_install.md「环境验证」一节）。
   4. 检查基础依赖：`gcc --version`（>= 7.3.0）、`cmake --version`（>= 3.16.0）、`python3 --version`。
3. **需要观察的现象**：`ASCEND_HOME_PATH` 非空；各工具版本满足要求。
4. **预期结果**：环境就绪，可以进入 4.4 的编译实践。若没有昇腾设备，可改用 CANNLab 或 Docker（`-devel` 后缀的算子开发镜像）环境，本步骤逻辑相同。
5. 本步骤的具体输出依赖你的环境，涉及安装的操作请实际执行后记录，「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：只想读代码并验证「改动能编过」，不打算在 NPU 上运行，最少需要安装什么？

答案：只需 CANN toolkit 包（编译态），不需要 NPU 驱动，也不需要 CANN ops 包。依据是 quick_install.md 第 9-12 行的说明。

**练习 2**：编译时 shell 脚本是如何定位 CANN 头文件的？

答案：`set_env.sh` 导出 `ASCEND_HOME_PATH`，`scripts/build.conf.sh` 第 32-40 行基于它导出 `INCLUDE_PATH`、`ACLNN_INCLUDE_PATH`、`COMPILER_INCLUDE_PATH` 等路径变量供编译使用。

### 4.2 build.sh：构建总入口

#### 4.2.1 概念说明

`build.sh` 是用户与构建系统之间唯一的接口。它本身几乎不包含构建逻辑，而是做三件事：

1. **定路径**：确定仓库根目录、构建目录 `build/`、产物目录 `build_out/`。
2. **载入子脚本**：source `scripts/` 下 8 个子脚本，把所有函数加载进来。
3. **编排流程**：`main()` 根据解析出的开关变量，按条件依次调用「建库 → 编 kernel → 打包 → 测试 → 跑样例 → 生成脚手架」各阶段。

理解了这个「入口 + 编排」结构，面对几十个编译参数就不会迷路：所有参数最终都只是让 `main()` 里某个 `if` 分支生效。

#### 4.2.2 核心流程

```text
bash build.sh <参数>
   │
   ├─ 无参数？ → 打印 usage 并退出
   │
   └─ main()
       ├─ checkopts "$@"          # 解析参数，设置 ENABLE_* / OP_* 等开关变量
       ├─ assemble_cmake_args     # 把开关变量翻译成 CMAKE_ARGS（-DXXX=YYY）
       ├─ clean_build_binary      # 清理旧的构建产物
       ├─ cmake_init              # mkdir build/ build_out/，cd build && cmake ..
       ├─ ENABLE_CREATE_LIB?  → build_lib          # 编译 ophost/opapi 等 .so 库
       ├─ (ENABLE_BINARY 且非 JIT)? → build_binary # 编译 kernel 二进制
       ├─ ENABLE_STATIC?      → build_static_lib   # 编译静态库
       ├─ ENABLE_PACKAGE?     → build_package      # 打 run/rpm/deb 包到 build_out/
       ├─ ENABLE_TEST?        → build_ut           # 单元测试
       ├─ ENABLE_RUN_EXAMPLE? → build_example      # 编译并运行样例
       ├─ ENABLE_GENOP?       → gen_op             # 生成新算子目录
       └─ ENABLE_GENOP_AICPU? → gen_aicpu_op       # 生成 AICPU 新算子目录
```

注意最后一行：整个 `main` 的输出会通过 `while read` 循环加上时间戳前缀再打印，方便回查每条日志的时间。

#### 4.2.3 源码精读

- [build.sh:14-L19](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L14-L19) —— 定义三个核心路径变量：`BASE_PATH`（仓库根）、`BUILD_PATH`（`build/`，CMake 工作目录）、`BUILD_OUT_PATH`（`build_out/`，最终产物输出目录），以及仓库名 `REPOSITORY_NAME="math"`（后面库名都叫 `ophost_math`、`opapi_math` 就是来自它）。
- [build.sh:21-L28](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L21-L28) —— 依次 source 8 个子脚本：配置（conf）、清理（clean）、参数（options）、cmake（cmake）、库编译（lib）、UT（ut）、样例（example）、脚手架（genop）。这就是 4.3 节要讲的分工图。
- [build.sh:30-L64](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L30-L64) —— `main()` 全部编排逻辑。几个值得注意的细节：
  - 第 37 行 `ENABLE_CREATE_LIB` 为 TRUE 才 `build_lib`；该变量由 `set_create_libs()` 推导（`--pkg` 或 `--ophost`/`--opapi` 等都会置位）。
  - 第 40 行的条件 `[[ "$ENABLE_BINARY" == "TRUE" || "$ENABLE_CUSTOM" == "TRUE" ]] && [[ "$ENABLE_JIT" == "FALSE" ]]`：`--jit` 表示「图运行态会在线编译 kernel」，打整包时可以跳过 kernel 二进制编译以提速，所以 JIT 模式下不走 `build_binary`。
  - 第 46-51 行：只有 `--pkg` 时才打包；`--static` 会额外产出一个静态库压缩包。
- [build.sh:66-L69](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L66-L69) —— 不带任何参数执行 `bash build.sh` 会直接打印 usage 并退出（`usage` 定义在 `build_options.sh` 中）。这是查看所有可用参数的最快方式。
- [build.sh:70](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/build.sh#L70) —— `main "$@" 2>&1 | while ... date ...` 把全部输出加上 `[YYYY-MM-DD HH:MM:SS]` 时间戳。

#### 4.2.4 代码实践

1. **实践目标**：不真正编译，只通过帮助系统摸清 build.sh 的能力面。
2. **操作步骤**：
   1. 在仓库根目录执行 `bash build.sh`（无参数），观察默认 usage 输出。
   2. 执行 `bash build.sh --pkg --help`，注意帮助内容变成了「Package Build Options」——`--help` 会根据同命令行中出现的其他参数切换到对应场景的分主题帮助（这段逻辑在 `build_options.sh` 的 `checkopts` 里，见 4.3.3）。
   3. 再试试 `bash build.sh -u --help`、`bash build.sh --run_example --help`，对比输出的差异。
3. **需要观察的现象**：分主题帮助各自列出该场景专用的参数与示例命令。
4. **预期结果**：你能说出「打自定义算子包」场景下的一条示例命令（如 `bash build.sh --pkg --soc=ascend910b --ops=add,sub`）。
5. 该实践只读脚本不编译，无需 NPU 环境，「待本地验证」（输出内容以实际终端为准）。

#### 4.2.5 小练习与答案

**练习 1**：`build.sh` 里 `REPOSITORY_NAME="math"` 这个变量会影响什么？

答案：它被拼进库名——`scripts/build_options.sh` 的 `set_create_libs()` 用它构造 `ophost_math`、`opapi_math`、`opgraph_math`、`oponnx_plugin_math`、`optf_plugin_math` 这组构建目标名（见 build_options.sh 第 460 行），最终产物即 `libophost_math.so` 等动态库。

**练习 2**：为什么 `--jit` 模式下 `main()` 跳过 `build_binary`？

答案：`--jit` 用于静态图整包场景，图的运行态会对 kernel 做在线编译，离线编译 kernel 二进制没有必要，跳过可以显著提升打包速度（参数语义见 [docs/zh/install/build.md:45](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/build.md#L45)，跳过逻辑见 build.sh 第 40 行的条件）。

### 4.3 scripts/ 子脚本分工：从命令行到 CMake 参数

#### 4.3.1 概念说明

`scripts/` 下的子脚本各自负责构建流水线的一段。把它们想象成一条「参数加工流水线」：

```text
命令行参数
  → build_options.sh（解析/校验，得到 ENABLE_* 开关变量）
  → build_cmake.sh（assemble_cmake_args 把开关翻译成 -DXXX=YYY）
  → build_cmake.sh（cmake_init 在 build/ 下执行 cmake）
  → build_lib.sh（cmake --build 逐目标编译/打包）
```

而 `build.conf.sh` 是全流水线共享的「常量表」，`build_clean.sh`、`build_ut.sh`、`build_example.sh`、`build_genop.sh` 则是挂在 `main()` 各分支上的专项执行器。

#### 4.3.2 核心流程

参数解析（`checkopts`）内部依次完成：

1. 初始化几十个开关变量为 `FALSE`（`ENABLE_TEST`、`ENABLE_PACKAGE`、`OP_KERNEL` 等）。
2. 先整体扫一遍参数做合法性预检（不认识的短/长参数直接报错）。
3. 处理 `--help`：根据同命令行的其他参数选择分主题帮助。
4. 用 `getopts` 逐个消费参数，把值写进开关变量（如 `--pkg` → `ENABLE_BINARY=TRUE; ENABLE_PACKAGE=TRUE`）。
5. 收尾三连：`check_param`（互斥校验）→ `set_create_libs`（确定要建哪些库）→ `set_ut_mode`（确定要跑哪些 UT 目标）。

#### 4.3.3 源码精读

- [scripts/build.conf.sh:13-L24](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build.conf.sh#L13-L24) —— 全局「白名单」：`RELEASE_TARGETS` 列出 7 个发布目标（ophost/opapi/opgraph/opkernel/opkernel_aicpu/onnxplugin/tfplugin），`SUPPORT_COMPUTE_UNIT_SHORT` 列出 13 个支持的芯片型号（ascend910b、ascend950、ascend310p……），`SUPPORTED_SHORT_OPTS`/`SUPPORTED_LONG_OPTS` 是合法参数表。`check_option_validity()` 就是用这张表拒绝非法参数的。
- [scripts/build_options.sh:13-L56](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_options.sh#L13-L56) —— `usage()` 函数的「package」分主题帮助：列出 `--pkg`、`--soc`、`--ops`、`-j`、`--build-type` 等参数及官方示例命令。其他分主题（opkernel/test/run_example/genop……）结构相同。
- [scripts/build_options.sh:707-L845](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_options.sh#L707-L845) —— `checkopts` 的参数消费主体（`getopts` 循环）。几个关键映射：`--ops=add,sub` 同时置 `COMPILED_OPS` 与 `ENABLE_CUSTOM=TRUE`（第 726-729 行）；`--pkg` 置 `ENABLE_BINARY` 与 `ENABLE_PACKAGE`（第 770-773 行）；`--soc=` 写入 `COMPUTE_UNIT`（第 736-738 行）；`--make_clean` 直接清理并退出（第 805-810 行）。
- [scripts/build_options.sh:348-L453](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_options.sh#L348-L453) —— `check_param()` 的互斥规则，例如「`--pkg` 不能与 `-u`、`--ophost`、`--opapi`、`--opgraph` 同用」「`--static` 只能搭配 `--pkg`」「`--simulator` 必须指定 `--soc`」。看懂这段，参数报错时就不必瞎猜。
- [scripts/build_options.sh:455-L487](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_options.sh#L455-L487) —— `set_create_libs()`：整包模式（`--pkg` 且非 custom）一次性建 5 个库；单库模式按 `--ophost`/`--opapi`/`--opgraph`/`--onnxplugin`/`--tfplugin` 各自追加目标；`--opkernel` 置 `ENABLE_BINARY=TRUE` 走 kernel 编译。
- [scripts/build_cmake.sh:23-L153](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_cmake.sh#L23-L153) —— `assemble_cmake_args()`：把每个 shell 开关翻译成一个 `-D` 参数追加到 `CMAKE_ARGS`，如 `ENABLE_TEST=TRUE` → `-DENABLE_TEST=TRUE`、`--soc=` → `-DASCEND_COMPUTE_UNIT=...`（第 119-150 行还会做芯片型号的模糊匹配与合法性检查）。这一函数是 shell 世界与 CMake 世界之间的「翻译官」。
- [scripts/build_cmake.sh:155-L169](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_cmake.sh#L155-L169) —— `cmake_init()`：创建 `build/` 与 `build_out/` 目录，删除旧的 `CMakeCache.txt`（保证每次全新配置），然后 `cd build && cmake ${CMAKE_ARGS} ..`。注意 genop 模式直接 return——生成脚手架不需要 CMake。
- [scripts/build_lib.sh:13-L27](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_lib.sh#L13-L27) —— `build_lib()`：逐个 `cmake --build . --target ${lib} -j ${THREAD_NUM}` 编译 `BUILD_LIBS` 中的目标（线程数默认 8，来自 `checkopts` 第 595 行的 `THREAD_NUM=8`，可用 `-j16` 覆盖）。
- [scripts/build_lib.sh:29-L100](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_lib.sh#L29-L100) —— `build_binary()`：kernel 二进制编译比 so 复杂得多——先跑 `gen_bin_scripts` 生成编译脚本，再创建软链 `op_impl/ai_core/tbe/op_tiling/liboptiling.so → libophost_math.so`（第 56-59 行），导出 `ASCEND_CUSTOM_OPP_PATH`，按芯片逐个执行 `prepare_binary_compile_${unit}` 与 `binary` 目标。kernel 日志位于 `build/binary/${soc}/bin/build_log`（见 [docs/zh/install/build.md:51](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/install/build.md#L51)）。
- [scripts/build_lib.sh:137-L177](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_lib.sh#L137-L177) —— `build_package()`：先 `clean_build_out` 清空产物目录，再执行 CMake 的 `package` 目标生成安装包；`--pkg-type=all` 时循环打 run/rpm/deb 三种包，rpm/deb 包随后被 `collect_rpm_deb_package` 拷贝到 `build_out/`。

#### 4.3.4 代码实践

1. **实践目标**：亲手把一条编译命令「翻译」成 CMake 参数，验证你理解了参数流水线。
2. **操作步骤**：
   1. 阅读命令：`bash build.sh --pkg --soc=ascend910b --ops=add -j16`。
   2. 对照 `checkopts`（build_options.sh 第 707-845 行）逐参数写出它设置的变量：`ENABLE_PACKAGE=TRUE`、`ENABLE_BINARY=TRUE`、`ENABLE_CUSTOM=TRUE`、`COMPILED_OPS=add`、`COMPUTE_UNIT=ascend910b`、`THREAD_NUM=16`。
   3. 对照 `assemble_cmake_args`（build_cmake.sh）写出最终 `CMAKE_ARGS` 里应出现的片段：`-DENABLE_BINARY=TRUE -DENABLE_CUSTOM=TRUE -DASCEND_OP_NAME=add -DENABLE_PACKAGE=TRUE -DPACKAGE_TYPE=run -DASCEND_COMPUTE_UNIT=ascend910b`。
   4. 如果环境可用，真实执行该命令，在终端最前面找到 `CMAKE_ARGS: ...` 这行打印（来自 build.sh 第 33 行），与你手写的对照。
3. **需要观察的现象**：手写推导与脚本实际打印的 `CMAKE_ARGS` 是否一致（顺序可能不同，关注键值对集合）。
4. **预期结果**：集合一致即通过；若不一致，回到 `checkopts` 找你漏掉的映射。
5. 第 4 步需要完整编译环境，「待本地验证」；前三步纯源码阅读，随时可做。

#### 4.3.5 小练习与答案

**练习 1**：用户执行 `bash build.sh --soc=ASCEND910B`（大写），会失败吗？

答案：不会失败。`assemble_cmake_args` 第 120 行先用 `tr '[:upper:]' '[:lower:]'` 把型号转为小写再做匹配，所以大写输入会被规整为 `ascend910b`。

**练习 2**：`bash build.sh --pkg -u` 会发生什么？为什么？

答案：直接报 `[ERROR] --pkg cannot be used with -u` 并退出。这是 `check_param()` 第 357-361 行的互斥规则：打包模式与测试模式的 CMake 配置相互冲突（打包要求 Release 优化，测试默认 `-O0 -g`）。

**练习 3**：想清理所有编译产物重新来过，用什么命令？

答案：`bash build.sh --make_clean`。`checkopts` 第 805-810 行会依次调用 `clean_build`、`clean_build_out`、`clean_third_party` 后直接退出。

### 4.4 CMake 构建体系与 build_out 产物

#### 4.4.1 概念说明

shell 层只负责「翻译参数 + 调 cmake」，真正的编译组织在 CMake 层。根 `CMakeLists.txt` 做了几件事：

- 声明工程与外部依赖（从已安装的 CANN 包取头文件和库，第三方库 eigen/protobuf/json 放在 `third_party`）。
- 定义一批 `option(...)` 开关，与 shell 层传入的 `-D` 参数一一对应。
- 根据开关把子目录挂进构建树：`common/` 必编；正常模式逐类别挂 `conversion/math/random`，`--experimental` 模式改挂 `experimental/`；`--ops=` 指定的算子若在 `examples/` 下则挂 `examples/`；`-u` 时挂 `tests/ut`。
- 收尾时生成算子信息文件、符号表，并在 `--pkg` 模式下执行打包。

编译产物分两个目录：

- `build/`：CMake 工作目录（对象文件、生成的脚本、kernel 编译中间产物如 `build/binary/${soc}/bin/build_log`）。
- `build_out/`：最终交付物目录，`build_package` 开头会 `clean_build_out` 清空它，然后放入 run/rpm/deb 包（run 包文件名形如 `cann-${soc_name}-ops-math_${cann_version}_linux-${arch}.run`）。

#### 4.4.2 核心流程

```text
cmake -DENABLE_XXX=... ..        # cmake_init 发起配置
   │
   ├─ add_subdirectory(common)                    # 公共代码，总是编译
   ├─ ENABLE_EXPERIMENTAL?
   │    ├─ 是 → add_subdirectory(experimental)    # 只编用户自定义算子
   │    └─ 否 → 逐个 add_subdirectory(${OPS_CATEGORY_LIST})  # conversion/math/random
   ├─ ASCEND_OP_NAME 命中 examples/ 下算子? → add_subdirectory(examples)
   ├─ ENABLE_TEST? → add_subdirectory(tests/ut)
   │
   └─ cmake --build . --target <lib|binary|package>   # build_lib/build_binary/build_package 调用
```

其中 `ASCEND_OP_NAME` 就是 shell 层 `--ops=` 逗号列表转分号后的结果——这就是「只编译指定算子」的实现机制：CMake 只把相关算子的构建规则纳入构建树。

#### 4.4.3 源码精读

- [CMakeLists.txt:11-L23](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L11-L23) —— `cmake_minimum_required(VERSION 3.16)`（对应依赖表里的 cmake >= 3.16.0）；工程名 `math`；`ASCEND_INSTALL_PATH` 缺省时取环境变量 `ASCEND_HOME_PATH`——再次印证「编译依赖已安装的 CANN 包」。
- [CMakeLists.txt:39-L59](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L39-L59) —— 全部编译开关 option：`ENABLE_TEST`、`ENABLE_BINARY`、`ENABLE_CUSTOM`、`ENABLE_PACKAGE`、`ENABLE_EXPERIMENTAL`、`ENABLE_ASAN`、`ENABLE_VALGRIND` 等，以及 UT 分目标开关（`OP_HOST_UT`、`OP_API_UT`……）。它们与 `assemble_cmake_args` 产出的 `-D` 参数一一对应，是两层的「对接协议」。
- [CMakeLists.txt:64-L77](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L64-L77) —— `PACKAGE_TYPE`（run/rpm/deb/all，非法值直接 FATAL_ERROR）、`ASCEND_COMPUTE_UNIT`（默认 `ascend910b`）、`ASCEND_OP_NAME`（「指定编译的算子」）、`VENDOR_NAME`（默认 `custom`）。注意第 77 行 `ASCEND_ALL_COMPUTE_UNIT` 列出全部芯片——与 `build.conf.sh` 的 `SUPPORT_COMPUTE_UNIT_SHORT` 呼应。
- [CMakeLists.txt:79-L95](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L79-L95) —— 编译模式选择逻辑：未显式指定 `-O` 时，开测试则 `-O0` 且 Debug，否则 `-O2` 且 Release。这解释了为什么 UT 与打包互斥——两者要求的编译配置根本不同。
- [CMakeLists.txt:129-L151](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L129-L151) —— 子目录挂载逻辑：`common` 恒编；`ENABLE_EXPERIMENTAL` 二选一（experimental 或三大算子目录）；第 138-151 行检查 `ASCEND_OP_NAME` 中是否有算子存在于 `examples/` 下（例如 `add_example`），有则挂 `examples` 目录——这正是本讲实践任务「仅编译 add_example」的源码依据。
- [CMakeLists.txt:160-L173](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L160-L173) —— `check_compiled_ops()` 校验指定算子是否真的被编译到，以及非测试模式下生成算子信息与符号表（`gen_ops_info_and_python`、`gen_norm_symbol`/`gen_cust_symbol`）。
- [CMakeLists.txt:175-L187](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/CMakeLists.txt#L175-L187) —— `ENABLE_PACKAGE` 时 include `cmake/package.cmake` 并按 `ENABLE_CUSTOM` 走 `pack_custom()`（自定义算子包）或 `pack_built_in()`（内置整包），对应 shell 层 `build_package()` 调用的 `package` 目标。
- [scripts/build_lib.sh:137-L177](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/scripts/build_lib.sh#L137-L177) —— 打包时先 `clean_build_out` 清空产物目录再打新包，rpm/deb 产物由 `collect_rpm_deb_package` 拷入 `build_out/`（第 207-221 行）。因此 **`build_out/` 里永远只有最近一次打包的产物**。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：完成一次真实编译，并弄清产物位置。
2. **操作步骤**：
   1. 确认 4.1 的环境实践已通过（CANN 已装、`set_env.sh` 已 source）。
   2. 全量编译（时间较长，取决于机器）：
      ```bash
      bash build.sh --pkg --soc=ascend910b -j16
      ```
   3. 或者只编译一个算子（推荐，快得多；`add_example` 是脚手架算子，位于 `examples/` 下）：
      ```bash
      bash build.sh --pkg --soc=ascend910b --ops=add_example -j16
      ```
   4. 编译结束后查看产物：
      ```bash
      ls -lh build_out/
      ```
   5. 查看中间产物与 kernel 日志：
      ```bash
      ls build/
      ls build/binary/ 2>/dev/null
      ```
3. **需要观察的现象**：
   - `build_out/` 下出现 `.run` 安装包（文件名包含 `ops-math`、芯片名、版本号）；
   - `build/` 下有 `CMakeCache.txt` 及各目标构建中间文件；
   - 终端每行日志带时间戳前缀；执行初期有 `CMAKE_ARGS: ...` 打印。
4. **预期结果**：拿到 run 包即编译成功；记录 run 包的完整文件名和路径（下一讲安装它并运行 AddExample）。若失败，优先检查：是否 source 了 `set_env.sh`、`--soc` 是否写对、基础依赖版本。
5. 本实践依赖真实 NPU/CANN 环境（编译态至少需要 toolkit 包），「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`--ops=add_example` 是如何让 CMake 只编译这个算子的？

答案：shell 层把逗号列表转成分号串写入 `CMAKE_ARGS`（build_cmake.sh 第 14-17 行 `-DASCEND_OP_NAME=add_example`）；根 CMakeLists.txt 第 138-151 行检测到 `examples/add_example/CMakeLists.txt` 存在，于是 `add_subdirectory(examples)`，构建树里只包含该算子的规则。

**练习 2**：为什么说 `build_out/` 里「永远只有最近一次的打包产物」？

答案：`build_package()` 第一行就是 `clean_build_out`（build_lib.sh 第 139 行），每次打包前先清空 `build_out/`，之后才生成新包。

**练习 3**：不开 `-u`、不指定 `-O` 时，Host 侧代码以什么优化级别编译？

答案：`-O2` 且 `CMAKE_BUILD_TYPE=Release`。依据 CMakeLists.txt 第 84-95 行：`ENABLE_TEST` 为假时 `COMPILE_OP_MODE=-O2`、`CMAKE_BUILD_TYPE=Release`；开 `-u` 则变为 `-O0 -g` + Debug。

## 5. 综合实践

**任务：给 build.sh 画一张「参数 → 阶段 → 产物」的流程说明书。**

1. 选一条你感兴趣的编译命令，例如：
   `bash build.sh --pkg --soc=ascend950 --ops=add,sub --build-type=Debug -j8`
2. 在源码中完成三次跟踪并写成笔记：
   - **参数层**：在 `scripts/build_options.sh` 的 `checkopts` 中找到每个参数设置的变量，注意 `--build-type=Debug` 会通过 `check_param` 的哪些校验。
   - **翻译层**：在 `scripts/build_cmake.sh` 的 `assemble_cmake_args` 中写出对应的 `CMAKE_ARGS` 片段。
   - **执行层**：在 `build.sh` 的 `main()` 中标出哪些 `if` 分支会执行、各分支分别调用 `scripts/build_lib.sh` 里的哪个函数。
3. 若有可用环境，实际执行该命令（换成本机真实芯片型号），核对终端打印的 `CMAKE_ARGS` 与你的推导，并记录 `build_out/` 中出现的产物文件名。
4. 最后用三五行总结：这条命令从回车到产出 run 包，经历了哪几个阶段。

这个任务把本讲三个模块（入口编排、脚本分工、CMake 体系）串成了一条链，完成它就意味着你已能独立读懂任意编译命令的行为。

## 6. 本讲小结

- 环境分**编译态**（只需 CANN toolkit）与**运行态**（驱动 + toolkit + ops 包）；编译前必须 `source set_env.sh` 让 `ASCEND_HOME_PATH` 生效，否则 `build.conf.sh` 拼出的头文件/库路径全部为空。
- `build.sh` 是纯编排层：定义 `build/`、`build_out/` 路径，source 8 个 `scripts/` 子脚本，`main()` 按 `ENABLE_*` 开关依次执行建库、编 kernel、打包、测试、样例、脚手架各阶段。
- 参数流水线是「`checkopts` 解析 → `assemble_cmake_args` 翻译成 `-D` 参数 → `cmake_init` 配置 → `build_lib/build_binary/build_package` 逐目标构建」。
- 根 `CMakeLists.txt` 的 `option` 与 shell 层 `-D` 参数一一对应；`--ops=` 通过 `ASCEND_OP_NAME` 只把指定算子挂进构建树；测试模式固定 `-O0 -g`、发布模式默认 `-O2`。
- 产物分两层：`build/` 是 CMake 工作目录（含 kernel 编译日志），`build_out/` 是最终 run/rpm/deb 包的输出目录，且每次打包前会被清空。
- `bash build.sh`（无参数）或 `bash build.sh <场景参数> --help` 可查看总览/分主题帮助，是探索构建能力的最快入口。

## 7. 下一步学习建议

下一讲（u1-l4「跑通第一个算子」）将使用本讲产出的 run 包：安装算子包、配置环境变量，并编译运行 `AddExample` 样例，走通「编译 → 安装 → 调用」的端到端链路。建议提前浏览 `docs/QUICKSTART.md` 和 `examples/add_example/README.md`。若你想更早接触 `--run_example` 参数（编译并直接执行 `test_aclnn_xxx.cpp` 样例），可以顺带阅读 `scripts/build_example.sh`，它在下一讲会正式登场。
