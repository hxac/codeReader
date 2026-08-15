# 环境准备与源码编译：build.sh 单算子构建全流程

## 1. 本讲目标

上一讲我们认识了 ops-nn 的定位和目录结构，本讲解决一个最实际的问题：**如何把仓库里的一份算子源码，变成 CANN 环境里可以调用的算子**。学完本讲，你应该能够：

1. 说清编译 ops-nn 需要哪些环境前提（CANN 包、环境变量、基础依赖工具）。
2. 掌握 `build.sh` 的 `--pkg`、`--soc`、`--ops`、`--vendor_name`、`-j` 等关键参数的含义与用法。
3. 理解 `build.sh` 背后的实际执行流程：参数解析 → 组装 cmake 参数 → cmake 构建 → 打包 run 包。
4. 知道编译产物 run 包安装到了哪里（`opp/vendors`）、为什么需要配置 `LD_LIBRARY_PATH`。
5. 能用 `--run_example` 编译并运行算子样例，完成「编译 → 安装 → 验证」的完整闭环。

## 2. 前置知识

阅读本讲前，建议先了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **CANN**：昇腾 NPU 的完整软件栈，编译 ops-nn 前必须先安装 CANN toolkit 包（类比：编译 CUDA 算子前要先装 CUDA Toolkit）。上一讲已经强调过，**源码分支与 CANN 包版本必须配套**。
- **编译态 vs 运行态**：这是官方文档 [docs/zh/install/quick_install.md:9-12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md#L9-L12) 中的区分——只编译不运行，只需 toolkit 包；要在真实 NPU 上跑算子，还需要驱动和 CANN ops 包。
- **run 包**：一个自解压安装包（类似 `.run` 安装脚本），编译产物会被打成 `cann-ops-nn-xxx.run`，执行它即完成安装。
- **soc_version**：NPU 芯片型号标识。Atlas A2 系列对应 `ascend910b`，Atlas A3 系列对应 `ascend910_93`，950 系列对应 `ascend950`。编译时通过 `--soc` 指定，因为不同芯片的指令集不同，kernel 二进制必须按芯片分别编译。
- **cmake**：C/C++ 的构建系统生成器。ops-nn 的构建入口虽然是 `build.sh`，但真正的编译由 cmake 驱动，`build.sh` 负责解析参数并翻译成 cmake 的 `-D` 变量。
- **LD_LIBRARY_PATH**：Linux 的动态库搜索路径环境变量。算子安装后，它的 `op_api` 动态库不在系统默认路径里，必须把这个路径加进来，样例程序才能链接和加载到它。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md) | 官方快速入门，给出编译→安装→验证的标准命令序列 |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | 整个仓库的构建入口：参数解析、cmake 调用、打包、样例运行 |
| [install_deps.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/install_deps.sh) | 基础依赖（python/gcc/cmake/pigz 等）的一键安装与静默检查 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt) | 顶层 cmake 工程定义，接收 build.sh 传入的全部 `-D` 变量 |
| [docs/zh/install/compile.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md) | 源码构建指南：自定义算子包 / ops-nn 包 / 静态库三种产物 |
| [docs/zh/install/quick_install.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md) | 环境部署指南：CANNLab / Docker / 手动安装三种方式 |

## 4. 核心概念与源码讲解

### 4.1 编译前的环境准备：依赖检查与 install_deps.sh

#### 4.1.1 概念说明

编译 ops-nn 不是 `git clone` 完就能直接 `bash build.sh` 的，它需要两层准备：

1. **CANN 侧**：已安装与源码分支配套的 CANN toolkit 包，并且执行过 `set_env.sh` 让 `ASCEND_HOME_PATH` 等环境变量生效。`build.sh` 中大量路径都是从 `ASCEND_HOME_PATH` 拼出来的，没有它编译必然失败。
2. **主机侧**：python（>= 3.7.0）、gcc（>= 7.3.0）、cmake（>= 3.16.0）、pigz、dos2unix 等基础工具，版本要求见 [docs/zh/install/quick_install.md:139-147](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md#L139-L147)。

`install_deps.sh` 就是把第二层准备自动化的脚本：它能探测操作系统（debian/rhel/euler/macos），按需安装各依赖。

#### 4.1.2 核心流程

`install_deps.sh` 的安装流程（其 `main` 函数）：

```text
detect_os（识别发行版，选定包管理器 apt/dnf/yum/brew）
  → install_python（>= 3.7.0）
  → install_gcc（>= 7.3.0）
  → install_cmake（>= 3.16.0）
  → install_pigz（可选，>= 2.4，加速打包）
  → install_dos2unix / install_patch
  → install_pkg_config / install_googletest（UT 依赖）
```

除了「主动安装」，`install_deps.sh` 还提供一个**静默检查**函数 `check_dependencies_silent`，它被 `build.sh` 在启动时调用：只检查、不安装，缺依赖就打印缺失清单并退出。这保证了你不会编译到一半才因为缺工具失败。

#### 4.1.3 源码精读

先看 `build.sh` 如何引入并调用依赖检查：

- [build.sh:34-39](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L34-L39)：`source "./install_deps.sh"` 之后立即执行 `check_dependencies_silent "$@"`，失败即 `exit 1`。注意这里有个细节：检查是**带参数**的——传了 `--pkg` 才额外检查 pigz 和 dos2unix，因为只有打包路径才用到它们。

再看 `check_dependencies_silent` 的实现：

- [install_deps.sh:425-511](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/install_deps.sh#L425-L511)：先用关联数组 `req_versions` 声明每个依赖的最低版本（如 `req_versions["CMake"]="3.16.0"`），再用内部的 `check_deps` 逐个执行 `command -v` 探测命令是否存在、版本是否达标，缺漏项收集到 `missing_deps` 数组，最后统一打印并提示用户执行 `bash install_deps.sh`。

版本比较依赖一个小工具函数：

- [install_deps.sh:28-43](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/install_deps.sh#L28-L43)：`version_ge` 按 `.` 分割版本号逐段比较，是典型的 shell 版本比较写法。

CANN 侧的环境变量则在 `build.sh` 顶部就被消费：

- [build.sh:138-150](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L138-L150)：从 `ASCEND_HOME_PATH` 派生出 `INCLUDE_PATH`（头文件）、`EAGER_LIBRARY_PATH`（`libascendcl` 等运行时库）、`GRAPH_LIBRARY_PATH`（图模式库）等路径。这些变量如果因为没 `source set_env.sh` 而为空，后续链接必然报错。官方推荐的配置命令见 [docs/zh/install/quick_install.md:197-206](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md#L197-L206)：默认路径安装时执行 `source /usr/local/Ascend/cann/set_env.sh`。

#### 4.1.4 代码实践

1. **实践目标**：确认本机依赖是否满足编译要求，体验 `check_dependencies_silent` 的静默检查行为。
2. **操作步骤**：
   - 在项目根目录执行 `bash install_deps.sh`，观察它逐项输出 Python/GCC/CMake 的当前版本与检查结论。
   - 再执行 `bash build.sh --pkg --soc=ascend910b --ops=add_example`，观察脚本启动时是否先打印依赖检查相关信息（若依赖齐全则直接进入编译流程）。
3. **需要观察的现象**：`install_deps.sh` 对每个依赖输出「meets requirements」或触发安装；`build.sh` 在依赖缺失时打印 `Missing dependencies:` 清单而不是开始编译。
4. **预期结果**：所有依赖达标后，`build.sh` 能顺利走到 `CMAKE_ARGS: ...` 的输出阶段。
5. 本实践依赖真实环境，输出细节「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build.sh` 要在开头调用 `check_dependencies_silent` 而不是直接开始编译？

**参考答案**：编译和打包过程分散在 cmake、g++、makeself 等多个工具链环节，如果等编译中途才因缺 pigz/dos2unix 失败，用户会浪费大量编译时间。入口处静默检查能把「环境问题」和「代码问题」在最早的时间点分开，报错信息也更能指向真实原因（提示执行 `bash install_deps.sh`）。

**练习 2**：只在编译态（不运行算子）使用 ops-nn，需要安装 NPU 驱动和 CANN ops 包吗？

**参考答案**：不需要。按 [docs/zh/install/quick_install.md:9-12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/quick_install.md#L9-L12) 的定义，编译态只需 CANN toolkit 包；驱动和 ops 包是运行态依赖。

### 4.2 build.sh 参数体系：从命令行到 cmake 变量

#### 4.2.1 概念说明

`build.sh` 是一个近 2000 行的 bash 脚本，但它对用户暴露的接口很克制：所有行为都由命令行参数控制。本讲最核心的一组参数是：

| 参数 | 含义 | 默认值 |
|---|---|---|
| `--pkg` | 构建算子包（含 kernel 二进制的 run 包） | 未开启 |
| `--soc=<版本>` | 目标芯片型号，逗号分隔可多个 | 未指定时按 `ascend910b` 处理 |
| `--ops=<算子名>` | 只编译指定算子（snake 命名，逗号分隔） | 不指定则编译全部算子 |
| `--vendor_name=<名>` | 自定义算子包的厂商名 | `custom` |
| `-j<n>` | 编译线程数 | 8 |
| `--run_example` | 编译并运行算子样例 | 未开启 |

理解这些参数的关键是：**`build.sh` 本身不做编译，它把参数翻译成 cmake 的 `-D` 变量，再调用 cmake**。所以读懂「参数 → cmake 变量」的映射，就读懂了整个构建系统的入口逻辑。

#### 4.2.2 核心流程

参数处理的主干流程：

```text
main "$@"
  └─ checkopts "$@"          # 解析全部命令行参数，设置一堆 shell 变量
       ├─ check_option_validity   # 校验选项拼写是否合法
       ├─ getopts 循环            # 逐个消费 -j/-O/-u 和 --xxx 选项
       ├─ check_param             # 检查参数组合合法性（如 --pkg 不能与 -u 同用）
       ├─ set_create_libs         # 决定要构建哪些库（ophost_nn/opapi_nn 等）
       └─ set_ut_mode             # UT 模式下决定测试目标
  └─ assemble_cmake_args     # 把 shell 变量拼成 CMAKE_ARGS 字符串
  └─ cmake_init / cmake ...  # 真正进入 cmake 构建
```

以快速入门命令 `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16` 为例：

1. `--pkg` → `ENABLE_PACKAGE=TRUE`、`ENABLE_BINARY=TRUE`；
2. `--ops=add_example` → `COMPILED_OPS=add_example`，同时 `ENABLE_CUSTOM=TRUE`（标记这是「自定义算子包」而不是整包）；
3. `--soc=...` → 归一化为短名（如 `ascend910b`）后传给 cmake 的 `ASCEND_COMPUTE_UNIT`；
4. `-j16` → `THREAD_NUM=16`，传给 `cmake --build -- -j 16`。

#### 4.2.3 源码精读

**（1）脚本支持的全部选项清单**

- [build.sh:23-32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L23-L32)：`SUPPORTED_SHORT_OPTS` 和 `SUPPORTED_LONG_OPTS` 两个数组穷举了所有合法选项，这是参数校验的「单一事实来源」。看一眼这个数组就能知道脚本全部能力（`--pkg`、`--jit`、`--simulator`、`--genop=`、`--run_example` 等）。

**（2）soc 支持列表与架构映射**

- [build.sh:14-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L14-L17)：`SUPPORT_COMPUTE_UNIT_SHORT` 列出全部支持的芯片短名；`SOC_TO_ARCH` 关联数组把 soc 映射到硬件架构号（如 `ascend910b` → `2201`、`ascend950` → `3510`），这个映射在仿真（`--simulator`）路径中用来定位仿真库。

**（3）核心选项在 getopts 中的解析**

- [build.sh:864-866](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L864-L866)：`ops=*` 分支把算子列表存入 `COMPILED_OPS` 并置 `ENABLE_CUSTOM=TRUE`——这就是「指定了 `--ops` 就构建自定义算子包」这一行为的出处。
- [build.sh:874-875](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L874-L875)：`soc=*` 分支只做一件事：存入 `COMPUTE_UNIT`，归一化在后面统一处理。
- [build.sh:913-916](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L913-L916)：`pkg` 分支同时置起 `ENABLE_PACKAGE` 和 `ENABLE_BINARY` 两个开关，后者控制是否编译 kernel 二进制（对比 `--jit` 只打包不编 kernel）。

**（4）参数组合合法性检查**

- [build.sh:474-492](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L474-L492)：`check_param` 明确禁止 `--pkg` 与 `-u`（UT 模式）、`--ophost`/`--opapi`/`--opgraph`（单库构建模式）混用。这些模式互斥是因为它们对应完全不同的 cmake 目标。

**（5）shell 变量 → cmake 变量的翻译**

- [build.sh:1044-1056](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1044-L1056)：`custom_cmake_args` 把 `COMPILED_OPS`（逗号改分号）翻译成 `-DASCEND_OP_NAME`，把 `VENDOR_NAME` 翻译成 `-DVENDOR_NAME`。
- [build.sh:1119-1146](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1119-L1146)：soc 归一化逻辑——把用户传入的型号转小写后与支持列表做前缀匹配（列表已按字符串长度降序排序避免前缀误匹配，见 [build.sh:18-19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L18-L19)），最终拼出 `-DASCEND_COMPUTE_UNIT=<短名>`；不支持的型号直接报 `The soc [...] is not support.`。

**（6）cmake 侧的接收**

- [CMakeLists.txt:96-105](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt#L96-L105)：顶层 CMakeLists 用 `CACHE STRING` 接收 `ASCEND_COMPUTE_UNIT`（默认 `ascend910b`）和 `VENDOR_NAME`（默认 `custom`）。这就是为什么 QUICKSTART 中不传 `--soc` 也能编——默认按 910b 处理。
- [CMakeLists.txt:50-79](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt#L50-L79)：`ENABLE_PACKAGE`、`ENABLE_CUSTOM`、`PACKAGE_TYPE` 等几十个 `option()` 一一对应 `build.sh` 传入的 `-D` 变量，构成两套体系之间的完整契约。

**（7）官方帮助信息本身就是好文档**

- [build.sh:160-171](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L160-L171)：`--pkg` 场景的帮助文本，逐项解释了 `--pkg`/`--soc`/`--vendor_name`/`--ops`/`-j`/`--pkg-type` 等参数。执行 `bash build.sh --pkg --help` 即可查看（帮助分场景：`--pkg`、`--opkernel`、`-u`、`--run_example` 等各有专属段落，分发逻辑见 [build.sh:776-808](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L776-L808)）。

#### 4.2.4 代码实践

1. **实践目标**：不真正编译，仅通过源码阅读和帮助命令掌握 `build.sh` 的参数面。
2. **操作步骤**：
   - 执行 `bash build.sh --pkg --help`、`bash build.sh --run_example --help`，对比两个场景支持的参数差异。
   - 故意执行一个非法组合：`bash build.sh --pkg -u`，观察报错。
   - 再执行 `bash build.sh --pkg --soc=ascend999 --ops=add_example`（故意写错的型号），观察 soc 校验报错。
3. **需要观察的现象**：非法组合触发 `[ERROR] --pkg cannot be used with test(-u, --ophost, etc.)`；非法 soc 触发 `The soc [ascend999] is not support.` 并打印用法。
4. **预期结果**：三类报错分别来自 `check_param`、soc 归一化逻辑和 `check_option_validity`，与上面精读的代码一一对应。
5. 报错文案细节「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`--ops=add_example` 与 `--vendor_name=myname` 都会把 `ENABLE_CUSTOM` 置为 TRUE，这背后「自定义算子包」和「ops-nn 整包」的区别是什么？

**参考答案**：按 [docs/zh/install/compile.md:26-32](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L26-L32)：自定义算子包只含部分算子，以**挂载**方式装到 `opp/vendors` 下，不改变原 CANN 包内容且优先级更高；ops-nn 整包则**完整替换** CANN 包中对应的算子部分。两者传参规则的总结见 [docs/zh/install/compile.md:64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L64)——`--vendor_name` 和 `--ops` 都不传时编译的就是 ops-nn 包。

**练习 2**：`--jit` 和 `--pkg` 都能产出自定义 run 包，差别在哪？

**参考答案**：`--pkg` 置起 `ENABLE_BINARY=TRUE`，包里带预编译的 kernel 二进制；`--jit` 走 [build.sh:1020-1023](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1020-L1023) 的分支，把 `ENABLE_BINARY` 压回 FALSE，即 [build.sh:160-161](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L160-L161) 帮助文本所述的 "Build run pkg without kernel bin"——kernel 在安装后即时编译。

**练习 3**：为什么 `SUPPORT_COMPUTE_UNIT_SHORT` 要按字符串长度降序排序？

**参考答案**：见 [build.sh:18-19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L18-L19) 的注释：soc 匹配用的是子串包含（前缀匹配），若短名排在长名前面，`ascend910_93` 会被 `ascend910` 提前命中导致映射错误，所以先按长度降序排序保证最长匹配优先。

### 4.3 编译主流程：main 函数的调度与 cmake 构建

#### 4.3.1 概念说明

参数解析完成后，`build.sh` 的 `main` 函数按固定顺序调度几个构建阶段。理解这条主链路，以后编译出错时就能定位到「卡在哪一层」。同时要建立两个目录概念：

- `build/`：cmake 的构建目录（中间产物、编译临时文件、样例可执行文件都在这里）。
- `build_out/`：最终交付物目录，run 包落地在这里。

#### 4.3.2 核心流程

`--pkg --ops=xxx` 场景下 main 的调度顺序（伪代码）：

```text
main:
  checkopts "$@"                      # 解析参数
  assemble_cmake_args                 # 拼 CMAKE_ARGS
  clean_build_binary                  # 清理上次的 binary/autogen 等中间目录
  cmake_init                          # 建 build/、build_out/，首次 cmake 预处理（PREPROCESS_ONLY=ON）
  parse_op_dependencies               # 单算子模式：解析该算子的依赖算子（保证被依赖算子也被编译）
  cmake (ENABLE_GEN_ACLNN=ON)         # 生成 aclnn 适配代码
  cmake (正式配置)
  build_lib                           # 编译 ophost/opapi 等宿主库
  build_binary                        # 编译各 soc 的 kernel 二进制
  build_torch_extension_whl           # （可选）torch_extension whl
  build_pkg                           # 打包 → build_out/cann-ops-nn-*.run
```

其中 `build_binary` 内部还分三步：先单独编译 tiling 库并在 `build/opp` 下做软链接伪装成安装布局（[build.sh:1259-1286](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1259-L1286)），再执行 `prepare_binary_compile_<soc>` 生成编译命令脚本，最后批量编译 kernel。这些细节初学阶段不必深究，记住「tiling 库先行、kernel 随后、最后打包」即可。

#### 4.3.3 源码精读

**（1）路径定义**

- [build.sh:126-132](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L126-L132)：`BASE_PATH`（仓库根目录）、`BUILD_PATH`（`build/`）、`BUILD_OUT_PATH`（`build_out/`）三个全局路径在这里定死，后续所有阶段都引用它们。

**（2）cmake 初始化**

- [build.sh:1155-1167](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1155-L1167)：`cmake_init` 创建 `build/` 与 `build_out/`，删除旧的 `CMakeCache.txt`（避免上次配置残留干扰），然后以 `PREPROCESS_ONLY=ON` 做一次预处理配置。

**（3）单算子依赖解析**

- [build.sh:1417-1424](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1417-L1424)：`parse_op_dependencies` 调用 `scripts/util/dependency_parser.py` 分析 `--ops` 指定算子的依赖算子。这就是为什么单算子编译出的包也能正常工作——它依赖的其他算子会被一并编译进来。
- 对应的调度入口在 [build.sh:1883-1887](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1883-L1887)：`COMPILED_OPS` 非空时走依赖解析分支。

**（4）main 的完整调度**

- [build.sh:1856-1916](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1856-L1916)：`main` 函数全貌。注意 `--run_example` 是**提前短路**的（[build.sh:1877-1880](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1877-L1880)），它只编译运行样例、不重新构建算子包；`--pkg` 路径则依次经过 `build_lib` → `build_binary` → `build_pkg`。
- [build.sh:1336-1377](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1336-L1377)：`build_pkg` 先 `clean_build_out` 清空产物目录，再执行 cmake 的 `package` 目标打出 run/rpm/deb 包。成功标志 `Self-extractable archive "cann-ops-nn-custom_linux-${arch}.run" successfully created.` 与 QUICKSTART 中承诺的一致（[docs/QUICKSTART.md:66-72](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L66-L72)）。

**（5）每行日志带时间戳**

- [build.sh:1918-1919](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1918-L1919)：`main` 的输出整体过一个 `while read` 循环，为每行加上 `[YYYY-MM-DD HH:MM:SS]` 前缀。全量编译可能跑几十分钟，带时间戳的日志能帮你判断卡在哪个阶段。

**（6）离线编译的支持**

- [build.sh:881-883](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L881-L883)：`--cann_3rd_lib_path=` 参数允许指定第三方依赖（protobuf、eigen 等，见 [docs/zh/install/compile.md:9-21](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L9-L21) 的清单）的离线存放目录；联网环境下这些依赖会自动下载，无需关心。

#### 4.3.4 代码实践

1. **实践目标**：通过日志和目录变化，把 `--pkg` 编译流程的各阶段「可视化」。
2. **操作步骤**：
   - 执行编译命令（soc 按实际环境取值）：`bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`。
   - 编译过程中另开终端观察 `ls build/` 与 `ls build_out/` 的变化。
   - 编译结束后回看完整日志，按时间戳找出 `build tiling start`、`binary build start`、`build pkg start` 三条分界线（分别对应 [build.sh:1264](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1264)、[build.sh:1302](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1302)、[build.sh:1337](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1337)）。
3. **需要观察的现象**：`build_out/` 在打包开始前被清空、随后出现 `cann-ops-nn-custom_linux-*.run`；日志每阶段之间有明显的时间间隔。
4. **预期结果**：最终打印 `Self-extractable archive "cann-ops-nn-custom_linux-${arch}.run" successfully created.`
5. 本实践需在配套昇腾环境执行，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cmake_init` 要先删掉 `build/CMakeCache.txt`？

**参考答案**：cmake 的 cache 会记住上次配置时的所有 `-D` 变量（比如上次的 `--soc`）。如果这次换了 soc 或换了算子列表而缓存未清，旧值可能残留导致「传了参数不生效」。删缓存保证每次构建的配置完全由本次命令行决定。

**练习 2**：`--run_example` 为什么不触发 `--pkg` 那套编译流程？

**参考答案**：见 [build.sh:1877-1880](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1877-L1880)，`--run_example` 在 main 中提前 `exit`，只做「找到样例 cpp → g++ 编译 → 运行」三步。它的前提是算子包**已经安装**到 CANN 环境，因此修改样例代码后重跑 `--run_example` 不需要重新编译算子包（QUICKSTART 第四节也专门强调了这一点）。

### 4.4 产物安装与运行验证：run 包、vendors 目录与 --run_example

#### 4.4.1 概念说明

编译成功只是第一步，要让 CANN 运行时「看得见」你的算子，还需要：

1. **安装 run 包**：执行 `build_out` 下的 run 包，把算子安装到 `${ASCEND_HOME_PATH}/opp/vendors/<vendor_name>_nn`。vendors 是 CANN 预留的自定义算子挂载点，优先级高于内置算子。
2. **配置动态库路径**：把 vendors 下的 `op_api/lib` 加入 `LD_LIBRARY_PATH`，样例程序才能加载到自定义算子的 aclnn 接口库。
3. **运行样例验证**：`--run_example` 会自动完成「找样例 → g++ 编译 → 执行」三步，是最省事的验证方式。

`--run_example` 的参数格式为 `bash build.sh --run_example <算子名> <运行模式> [包模式 --vendor_name=名]`，其中运行模式 `eager` 对应 aclnn 直调样例（`test_aclnn_*.cpp`），`graph` 对应图模式样例（`test_geir_*.cpp`）；包模式 `cust` 表示链接自定义算子包。

#### 4.4.2 核心流程

`--run_example add_example eager cust --vendor_name=custom` 的执行过程：

```text
build_example
  ├─ 在仓库内 find "*/add_example/examples/test_aclnn_*.cpp"   # 定位样例源文件
  │    （按 --soc 追加 arch35/arch22/arch20 等架构专属样例目录）
  └─ 对每个样例 build_single_example
       ├─ 计算 cust 模式的头文件/库文件路径：
       │    ${ASCEND_HOME_PATH}/opp/vendors/custom_nn/op_api/{include,lib}
       ├─ export LD_LIBRARY_PATH=<vendors lib>:$LD_LIBRARY_PATH
       ├─ g++ 样例.cpp -I ... -L ... -lcust_opapi -lopapi_math -lascendcl ... -o test_aclnn_add_example
       └─ 执行生成的可执行文件，tail -n 10 展示输出
```

#### 4.4.3 源码精读

**（1）安装位置与环境变量的官方约定**

- [docs/QUICKSTART.md:74-88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L74-L88)：安装命令 `./build_out/cann-ops-nn-*linux*.run`；安装位置 `${ASCEND_HOME_PATH}/opp/vendors`；环境变量 `export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_nn/op_api/lib:${LD_LIBRARY_PATH}`。注意目录名是 `custom_nn`——厂商名 `custom` 加上仓库后缀 `_nn`。
- [docs/zh/install/compile.md:78-88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L78-L88)：补充了两个运维要点——自定义安装路径时需 `source ${install_path}/vendors/${vendor_name}_nn/bin/set_env.bash`；卸载用 vendors 目录下的 `scripts/uninstall.sh`。

**（2）样例查找逻辑**

- [build.sh:1590-1624](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1590-L1624)：`build_example` 用 `find ../ -path "*/${OP_NAME}/examples/${pattern}*.cpp"` 定位样例；并按 `--soc` 值追加架构专属样例目录（`ascend950/ascend350` → `arch35`、`ascend910b` → `arch22`、`ascend310p` → `arch20`）。`pattern` 在 [build.sh:1597-1604](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1597-L1604) 由运行模式决定：`eager` → `test_aclnn_`、`graph` → `test_geir_`。
- [build.sh:1647-1653](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1647-L1653)：找不到样例时报 `doesn't have ${EXAMPLE_MODE} examples`；有失败样例时汇总成功/失败清单并 `exit 1`。

**（3）cust 模式的链接细节**

- [build.sh:1542-1565](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1542-L1565)：`build_single_example` 的 eager + cust 分支。若未显式传 `--vendor_name`，默认取 `custom`（[build.sh:1544-1546](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1544-L1546)）；若设置了 `ASCEND_CUSTOM_OPP_PATH` 环境变量则从中取路径，否则从 `${ASCEND_HOME_PATH}/opp/vendors/${VENDOR_NAME}_nn/op_api/` 取 include/lib。最关键的一行是 `export LD_LIBRARY_PATH=${cust_rpath_flags}:${LD_LIBRARY_PATH}`——这就是「为什么装完包还要配 LD_LIBRARY_PATH」在脚本里的自动化实现。g++ 链接的库包括 `-lcust_opapi -lopapi_math -lascendcl -lnnopbase`，并通过 `-Wl,-rpath` 把路径写进可执行文件。

**（4）执行与结果展示**

- [build.sh:1581-1587](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1581-L1587)：编译后直接执行 `build/test_aclnn_<example>`，`tail -n 10` 截取最后 10 行输出；用 `PIPESTATUS[0]` 取真实退出码（避免管道掩盖失败）。成功时打印 `Run test_aclnn_add_example success.`。

**（5）预期输出**

- [docs/QUICKSTART.md:100-112](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L100-L112)：样例逐元素打印两个输入与相加结果（如 `first input[0] is: 1.000000, second input[0] is: 1.000000, result[0] is: 2.000000`），看到它即证明「编译 → 安装 → 调用」全链路打通。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：完整走通单算子「编译 → 安装 → 环境变量 → 样例验证」闭环，并理解每一步在磁盘和环境上留下了什么。
2. **操作步骤**：
   1. 配置 CANN 环境变量：`source /usr/local/Ascend/cann/set_env.sh`（默认路径；非默认路径替换为 `${install_path}/cann/set_env.sh`）。
   2. 进入仓库根目录，编译（`${soc_version}` 按实际芯片取 `ascend910b` / `ascend910_93` / `ascend950`）：
      ```bash
      bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16
      ```
   3. 确认 `build_out/` 下出现 `cann-ops-nn-custom_linux-${arch}.run`，执行安装：
      ```bash
      ./build_out/cann-ops-nn-*linux*.run
      ```
   4. 检查安装产物：`ls ${ASCEND_HOME_PATH}/opp/vendors/custom_nn/`，应能看到 `op_api` 等目录。
   5. 配置运行时库路径：
      ```bash
      export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_nn/op_api/lib:${LD_LIBRARY_PATH}
      ```
   6. 运行样例验证：
      ```bash
      bash build.sh --run_example add_example eager cust --vendor_name=custom
      ```
3. **需要观察的现象**：
   - 步骤 2 结束时打印 `Self-extractable archive "cann-ops-nn-custom_linux-${arch}.run" successfully created.`；
   - 步骤 4 能列出 vendors 下的安装目录；
   - 步骤 6 打印加法结果并显示 `Run test_aclnn_add_example success.`；
   - 额外做一个小实验：**先注释掉步骤 5 的 export 再跑步骤 6**，观察脚本内部自动补 `LD_LIBRARY_PATH` 后是否仍能成功（对照 4.4.3 第 3 点的源码行为）。
4. **预期结果**：输出形如 `add_example first input[0] is: 1.000000, second input[0] is: 1.000000, result[0] is: 2.000000` 的逐元素加法结果。
5. 本实践必须在配套昇腾环境执行，具体输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：安装后算子包落在 `opp/vendors/custom_nn`，这个目录名是怎么拼出来的？如果编译时加 `--vendor_name=myteam`，目录会变成什么？

**参考答案**：目录名 = `VENDOR_NAME`（默认 `custom`，见 [CMakeLists.txt:102-104](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt#L102-L104)）+ 仓库后缀 `_nn`。加 `--vendor_name=myteam` 后目录为 `opp/vendors/myteam_nn`，相应地 `LD_LIBRARY_PATH` 和 `--run_example --vendor_name=myteam` 都要跟着改。

**练习 2**：`--run_example` 的第三个位置参数 `cust` 与不传时有何区别？

**参考答案**：不传 `cust`（`PKG_MODE` 为空）时走 [build.sh:1566-1571](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1566-L1571) 分支，链接的是 CANN 内置的 `opapi_nn` 库，用于验证内置算子；传 `cust` 时链接 vendors 下自定义包的 `libcust_opapi`，用于验证自己刚编译安装的算子。本讲的场景（验证 add_example）必须用 `cust`。

**练习 3**：样例程序执行失败时，`build.sh` 是怎么感知的？

**参考答案**：[build.sh:1581-1587](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1581-L1587) 中样例输出经过 `| tail -n 10` 管道，普通 `$?` 只会拿到 tail 的退出码，所以脚本专门用 bash 的 `PIPESTATUS[0]` 取管道第一个命令（样例本体）的真实退出码，非 0 则计入失败列表，最终使整个 `--run_example` 以非 0 退出。

## 5. 综合实践

把本讲四个模块串成一条完整的「环境体检 + 构建闭环」任务：

1. **环境体检**：执行 `bash install_deps.sh`，记录每个依赖的版本检查结果；执行 `npu-smi info` 和 `cat /usr/local/Ascend/cann/${arch}-linux/ascend_toolkit_install.info`，确认驱动与 CANN 版本，并核对源码分支是否与之配套（配套关系查 release-management 仓库）。
2. **单算子构建**：执行 `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16`，在日志中标注出 4.3 节提到的三个阶段分界线，确认 `build_out/` 下的 run 包文件名（记下其中编码的厂商名和 arch）。
3. **安装与验证**：安装 run 包，`ls` 确认 `opp/vendors/custom_nn` 结构，执行 `--run_example` 记录加法输出。
4. **对照源码写一份《构建流程笔记》**：把你实际执行的每条命令，对应到 `build.sh` 中的具体函数（如 `--ops` → `checkopts` 的 `ops=*` 分支 → `custom_cmake_args` → cmake 的 `ASCEND_OP_NAME`），形成一条「命令 → shell 变量 → cmake 变量 → 产物」的完整追踪链。

完成这份笔记后，你对 ops-nn 构建系统的理解就不再是「背命令」，而是「能顺着源码解释每一步」。

## 6. 本讲小结

- 编译 ops-nn 需要两层准备：CANN 侧（toolkit 包 + `set_env.sh` 使 `ASCEND_HOME_PATH` 生效）和主机侧（python/gcc/cmake 等，可用 `install_deps.sh` 一键安装，`build.sh` 启动时会静默复查）。
- `build.sh` 是参数解析与调度外壳，真正编译由 cmake 驱动；`--pkg`/`--soc`/`--ops`/`--vendor_name` 分别映射为 cmake 变量 `ENABLE_PACKAGE`/`ASCEND_COMPUTE_UNIT`/`ASCEND_OP_NAME`/`VENDOR_NAME`。
- `--ops` 单算子编译会自动解析依赖算子一并编译，产物是挂载式的自定义算子包，安装到 `opp/vendors/<vendor>_nn`，不污染原 CANN 包。
- `--run_example <算子> eager cust` 自动完成样例查找、g++ 编译（链接 vendors 下的 `libcust_opapi`）、执行和结果截取，是最快的验证闭环。
- `LD_LIBRARY_PATH` 指向 vendors 下的 `op_api/lib` 是自定义算子可被加载的关键，脚本在 cust 模式下也会自动补上。
- `--run_example` 与 `--pkg` 互不触发：改样例代码只需重跑前者，改算子源码才需要重走「编译 → 安装」。

## 7. 下一步学习建议

下一讲（u1-l3《算子工程的目录解剖》）将打开 `examples/add_example` 目录，看清这次编译出来的到底是什么——`op_host`、`op_kernel`、`op_api`、`op_graph` 等交付件各自的作用。建议提前浏览：

- [examples/add_example/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/CMakeLists.txt)：单算子是如何被 cmake 收集进整体构建的。
- [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md)：官方对算子目录约定的完整说明。

如果本讲的实践尚未在真实环境跑通，强烈建议先在 CANNLab 或 Docker 环境完成 4.4.4 的主实践再继续——后续所有讲义都建立在这个闭环之上。
