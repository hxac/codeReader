# 环境准备与编译安装全流程

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局认识：推理子仓 `inference/ascendc` 的核心产出有两个包——AscendC 算子库编译出的 **CANN 自定义算子 run 包**（发动机），以及 `torch_ops_extension` 打出的 **wheel 包**（方向盘）。本讲解决「发动机怎么造出来」：

1. 知道如何准备一个能编译昇腾算子的环境（Docker 镜像、CANN 包、`set_env.sh`）。
2. 能读懂 `build.sh` 的核心参数（`-c`/`-n`/`--tiling_key`/`-u` 等），并说出每个参数被翻译成了哪个 CMake 变量。
3. 能描述一条完整链路：`bash build.sh ...` → 参数解析 → `cmake_config` → CMake 收集算子并编译 → CPack 打出 `CANN-omni_custom_ops-<version>-linux.<arch>.run` → 安装到 CANN `vendors` 目录并 `source set_env.bash` 生效。

读完本讲，你应该可以在拿到一台昇腾机器后独立完成「编译 → 安装 → 验证」三步，或者在**没有硬件**的环境里，仅凭源码就能画出这条构建流水线。

## 2. 前置知识

- **CANN**：昇腾异构计算架构的软件栈（驱动、运行时、编译器、算子库的合集）。编译本项目时，CANN 提供头文件（`aclnn` 系列）、链接库（如 `libops_base.so`）、算子构建工具（`op_build`）等几乎所有外部依赖。没有 CANN 包，本仓库无法编译。
- **toolkit（开发套件包）**：CANN 面向开发者的安装包，通常位于 `/usr/local/Ascend/ascend-toolkit/latest` 或用户目录 `~/Ascend/ascend-toolkit/latest`。本讲的「环境」主要就是指它。
- **SOC 版本（`ascend910b` / `ascend910_93` / `ascend950`）**：昇腾芯片的具体型号代号。同一份算子源码要按不同芯片分别编译出二进制，所以编译时必须用 `-c` 指明目标芯片。
- **毕昇编译器（bisheng）**：昇腾提供的 C/C++ 编译器，随 CANN 包发布。`build.sh` 启动时会显式检查 `bisheng` 命令是否可用，找不到直接报错退出。
- **CMake 三阶段**：配置（configure，`cmake ..` 解析 `CMakeLists.txt` 生成构建系统）→ 构建（build，`cmake --build .` 编译目标）→ 安装/打包（install/package，把产物按 `install()` 规则归档并打包）。`build.sh` 本质上是这三阶段的「参数翻译器 + 驾驶员」。
- **run 包（自解压包）**：一个自带压缩数据段的 shell 脚本，执行 `./xxx.run --install-path=...` 即可自解压安装。CANN 生态用它分发自定义算子包，底层由 makeself 技术生成。
- **vendors 目录**：CANN 加载第三方自定义算子的标准位置 `<toolkit>/opp/vendors/<vendor_name>`。本项目产出的 vendor 名是 `omni_custom_transformer`（在 `CMakeLists.txt` 中定义）。

> 承接 u1-l1：算子源码按 `op_api / op_host / op_kernel` 三层组织。本讲不深入算子内部，只看构建系统如何把它们装配成 run 包；算子目录的逐文件解剖留给下一讲 u1-l3。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [inference/ascendc/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md) | 官方环境准备 / 编译 / 安装说明（镜像地址、docker 命令、安装命令都在这里） |
| [inference/ascendc/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh) | 编译总入口：参数解析、环境检查、拼 CMake 变量、驱动不同构建目标 |
| [inference/ascendc/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt) | 顶层 CMake 工程：定义 opapi/opsproto/optiling 等产物目标、收集算子目录、CPack 打 run 包 |
| [inference/ascendc/cmake/config.cmake](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake) | 环境检查（Python3、CANN 路径）、路径配置、版本兼容校验、调用 prepare.sh 预构建 |
| [inference/ascendc/cmake/variables.cmake](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/variables.cmake) | 全局变量：安装前缀指向 `output/`、`op_build` 工具路径等 |
| [inference/ascendc/cmake/func.cmake](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake) | 构建辅助函数，本讲重点是其中的 `op_add_subdirectory`（算子目录收集与 `-n` 过滤） |
| [inference/ascendc/cmake/scripts/prepare.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/scripts/prepare.sh) | 预构建脚本：在正式编译前用 `PREPARE_BUILD=ON` 再起一层内部 CMake，生成 autogen 中间产物 |

## 4. 核心概念与源码讲解

### 4.1 Docker 环境与 CANN 依赖

#### 4.1.1 概念说明

算子编译是「host 侧交叉编译」：在一台 aarch64 服务器上，用 CANN 提供的工具链把 C++ 源码编译成在昇腾 NPU 上运行的库与二进制。因此编译环境必须满足三个条件：

1. 有 CANN 开发包（含 `bisheng` 编译器与 `op_build` 算子构建工具）；
2. 有目标芯片型号对应的运行时头文件与库；
3. 环境变量已注入（`source set_env.sh` 之后 `ASCEND_HOME_PATH` 等变量才存在）。

官方推荐直接使用预置镜像，避免手工装配这些依赖。

#### 4.1.2 核心流程

```text
docker pull 镜像(A2/A3/A5)
        │
docker run（直通 /dev/davinci* 等 NPU 设备、挂载驱动目录）
        │
docker attach 进容器
        │
source /usr/local/Ascend/ascend-toolkit/set_env.sh   ← 环境变量注入
        │
bisheng / op_build 可用                              ← build.sh 会检查
```

#### 4.1.3 源码精读

**① 官方镜像与容器拉起**。README 给出三个开源镜像（A2/A3/A5，CANN 版本分别为 8.5.0/8.5.0/9.0.0）：

[inference/ascendc/README.md:L201-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L201-L207) —— 列出 A2/A3/A5 三个 Docker 镜像的 `docker pull` 地址，镜像名中 `cann8.5.0`/`cann9.0.0` 即预装的 CANN 版本。

[inference/ascendc/README.md:L208-L240](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L208-L240) —— `docker run` 示例：`--device=/dev/davinci0` 直通 16 张 NPU 卡，挂载 `/usr/local/Ascend/driver`（宿主机驱动）、`/var/log/npu/`（NPU 日志）等，`--shm-size=128g --privileged`。这说明**驱动在宿主机、CANN 在容器内**，两者通过设备直通协作。

[inference/ascendc/README.md:L242-L246](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L242-L246) —— 进入容器后执行 `source /usr/local/Ascend/ascend-toolkit/set_env.sh` 注入 CANN 环境变量。

**② build.sh 的环境自检**。`build.sh` 在真正干活前做两件事：定位 CANN 包、确认 `bisheng` 存在：

[inference/ascendc/build.sh:L72-L82](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L72-L82) —— `set_env()` 先 `source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash`，再用 `which bisheng` 找毕昇编译器；找不到就打日志并 `exit 1`。这是最常见的第一道报错关卡。

[inference/ascendc/build.sh:L31-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L31-L37) —— 按 `id -u` 区分 root 与普通用户，给出两套默认 toolkit 路径（`/usr/local/Ascend/...` 与 `~/Ascend/...`）。

**③ CANN 包路径的多级探测**。除了默认路径，`build.sh` 还接受环境变量与显式参数：

[inference/ascendc/build.sh:L408-L421](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L408-L421) —— 探测优先级为：`-p|--package-path` 参数 > `ASCEND_HOME_PATH` 环境变量 > `ASCEND_OPP_PATH` 的父目录 > root/非 root 默认 toolkit 目录 > `~/Ascend/latest`；全都找不到则报错退出，提示用 `-p` 指定。找到的路径最终会作为 `-DCUSTOM_ASCEND_CANN_PACKAGE_PATH=...` 传给 CMake（见 4.2.3 ⑦）。

> 小提示：README「下载源码」一节的克隆地址是 `cann/omni-ops` 仓库；本手册针对 `openPangu-2.0-Op` 仓库，两者目录结构（`inference/ascendc`）一致，流程通用。

#### 4.1.4 代码实践

1. **实践目标**：在不装任何东西的前提下，确认一台机器是否具备编译条件，并整理出「缺什么」。
2. **操作步骤**：
   - 执行 `which bisheng; which cmake; which ccache; echo $ASCEND_HOME_PATH`，记录四条输出；
   - 执行 `ls /usr/local/Ascend/ascend-toolkit/latest 2>/dev/null || ls ~/Ascend/ascend-toolkit/latest 2>/dev/null`，确认默认 toolkit 目录是否存在；
   - 对照 [build.sh:L72-L82](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L72-L82) 写出：本机会命中 [build.sh:L408-L421](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L408-L421) 探测链的哪一环。
3. **需要观察的现象**：普通 x86 开发机/CI 容器上，通常 `bisheng` 为空、toolkit 目录不存在——这正是「无昇腾环境」的典型形态。
4. **预期结果**：得到一张环境自检清单（bisheng / cmake / ccache / CANN 路径四项的「有/无」结论）。无昇腾环境属于正常情况，本讲后续实践均提供源码阅读型替代方案。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 的 `docker run` 要挂载宿主机的 `/usr/local/Ascend/driver`？
**答案**：NPU 驱动装在宿主机、CANN 开发套件装在容器镜像里；容器内编译/运行算子时既要访问设备（`--device=/dev/davinci*`），也要通过挂载的驱动目录与硬件协同，驱动与 CANN 版本必须配套。

**练习 2**：`build.sh` 找不到 CANN 包时，有哪几种解决方式（按代码优先级）？
**答案**：① 显式传 `-p <cann安装路径>`；② 导出 `ASCEND_HOME_PATH`；③ 导出 `ASCEND_OPP_PATH`（取其父目录）；④ 把 CANN 装到默认位置（root 为 `/usr/local/Ascend/ascend-toolkit/latest`，普通用户为 `~/Ascend/ascend-toolkit/latest`）。

---

### 4.2 build.sh：参数解析与变量拼接

#### 4.2.1 概念说明

`build.sh` 是整个仓库唯一的编译入口。它的设计模式可以概括为一句话：**把 shell 参数逐个翻译成 `-D` 形式的 CMake 变量，拼进 `CUSTOM_OPTION` 字符串，最后用一次 `cmake ..` + 一次 `cmake --build` 驱动不同目标**。理解了这条「参数 → 变量 → 目标」的翻译链，后面所有构建行为都一目了然。

#### 4.2.2 核心流程

```text
bash build.sh -n '算子名' -c ascend910_93 ...
   │
   ├─ ① while/case 解析全部命令行参数            (L262-L353)
   ├─ ② set_ut_mode：-u 时决定构建哪些 UT        (L178-L192)
   ├─ ③ 参数逐个追加进 CUSTOM_OPTION             (L358-L406)
   ├─ ④ 探测 CANN 包路径 → 追加两个 -D           (L408-L433)
   ├─ ⑤ 计算并行度 JOB_NUM（CPU 核数×2）         (L423-L431)
   ├─ ⑥ set_env（bisheng 检查）→ clean → ccache  (L439-L457)
   └─ ⑦ cd build/ 后按模式分派：
        ├─ ENABLE_TEST(-u)     → 两次 cmake 配置 + transformer_op_host_ut / transformer_op_api_ut
        ├─ --opapi/--ophost    → build_lib（打 lib 而不是 run 包）
        ├─ -b host             → cmake_config -DENABLE_OPS_KERNEL=OFF + build package + 拷 .run 到 output/
        ├─ -b kernel           → cmake_config -DENABLE_OPS_HOST=OFF + build ops_kernel
        └─ 默认                 → cmake_config + build_package（全量 run 包）
```

#### 4.2.3 源码精读

**① 参数解析**。经典的 `while + case` 循环：

[inference/ascendc/build.sh:L262-L353](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L262-L353) —— 逐个消费命令行参数：`-n` 存入 `ascend_op_name`、`-c` 存入 `ascend_compute_unit`、`--tiling_key` 存入 `TILING_KEY`、`-u` 置 `ENABLE_TEST=TRUE`、`-b` 存入 `BUILD`、`--opapi`/`--ophost` 追加 `BUILD_LIBS` 等；未知参数直接打印帮助并以状态 1 退出。`-h|--help` 在循环内就打印 `help_info` 并退出，因此 **`bash build.sh --help` 不需要任何昇腾环境即可运行**。

各核心参数含义（与 [help_info，build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L46-L65) 一致）：

| 参数 | 含义 | 多值写法 |
| --- | --- | --- |
| `-n, --op-name` | 只编译指定算子（目录名），默认全部 | 分号分隔并加引号：`-n "op1;op2"` |
| `-c, --compute-unit` | 目标芯片型号，默认 `ascend910_93` | `-c "ascend910b;ascend910_93"` |
| `--tiling_key` | 只编译指定 tiling key，默认全部 | `--tiling_key "1;2;3"` |
| `-u, --test` | 构建并运行单元测试（配合 `--ophost`/`--opapi` 细分） | 单开关 |
| `-b` | 构建模式：`host`（只编 host 侧）/ `kernel`（只编 kernel 侧） | 传值 |
| `-p` | 显式指定 CANN 包路径 | 传值 |
| `--verbose` | 打印更详细的编译信息 | 单开关 |
| `--disable-check-compatible` | 跳过与 CANN 的版本兼容校验 | 单开关 |

**② 参数 → CMake 变量**。这是本讲的枢纽代码：

[inference/ascendc/build.sh:L358-L406](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L358-L406) —— 非空的参数被逐个翻译追加进 `CUSTOM_OPTION`：`-c` → `-DASCEND_COMPUTE_UNIT=...`（L358-360）、`-n` → `-DASCEND_OP_NAME=...`（L362-364）、`--tiling_key` → `-DTILING_KEY=...`（L374-376）、`--enable_host_tiling` → `-DENABLE_HOST_TILING=true`（L386-388）、`-u` 追加 `-DENABLE_TEST=TRUE -DTESTS_UT_OPS_TEST=TRUE -DENABLE_UT_EXEC=TRUE`（L397-406）。

汇总表（参数 → CMake 变量 → 最终用途）：

| build.sh 参数 | CMake 变量 | 在 CMake 侧的作用 |
| --- | --- | --- |
| `-c` | `ASCEND_COMPUTE_UNIT` | 遍历芯片列表，按型号生成 kernel 二进制（`CMakeLists.txt` L726-733） |
| `-n` | `ASCEND_OP_NAME` | `op_add_subdirectory` 里过滤算子目录（见 4.3.3 ③） |
| `--tiling_key` | `TILING_KEY` | `add_ops_tiling_keys` 写入编译选项 ini（`CMakeLists.txt` L276-279） |
| `-u` | `ENABLE_TEST` 等 | 拉起 gtest/json 等 third_party（`CMakeLists.txt` L27-43） |
| `-p` | `CUSTOM_ASCEND_CANN_PACKAGE_PATH` | 定位 CANN 头文件与库（`config.cmake` L21-30） |
| `--op_debug_config` | `OP_DEBUG_CONFIG` | `add_opc_config` 注入算子调试配置（`CMakeLists.txt` L281-284） |

**③ CANN 路径与并行度收尾**：

[inference/ascendc/build.sh:L423-L433](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L423-L433) —— 并行度默认取 `CPU 核数 × 2`（可用环境变量 `OPS_CPU_NUMBER` 覆盖），最终把 `-DCUSTOM_ASCEND_CANN_PACKAGE_PATH=... -DCHECK_COMPATIBLE=...` 追加进 `CUSTOM_OPTION`。注意 `CHECK_COMPATIBLE` 初始值就是 `false`（[build.sh:L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L22)），`--disable-check-compatible`（[build.sh:L304-L307](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L304-L307)）是把它显式再置 `false`——即**当前代码默认不做版本校验**，README「遇到版本校验失败可用该参数跳过」的说明与这一现状一致；校验逻辑本身在 [config.cmake:L173-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L173-L194)（调用 `cmake/scripts/check_version_compatible.py` 比对 `version.info` 与 CANN 版本），仅在 `CHECK_COMPATIBLE=ON` 时执行。

**④ ccache 加速（可选）**：

[inference/ascendc/build.sh:L141-L164](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L141-L164) —— `gen_bisheng()` 在 `build/gen_bisheng_dir/` 下生成一个名为 `bisheng` 的 wrapper 脚本（内容是 `ccache bisheng "$@"`），并把该目录前置到 `PATH`。这样后续所有对 `bisheng` 的调用都悄悄经过 ccache，二次编译显著提速；[build.sh:L443-L457](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L443-L457) 负责探测系统 ccache（或用 `--ccache` 显式指定、`--ccache false` 关闭）。

**⑤ 主流程分派**：

[inference/ascendc/build.sh:L459-L495](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L459-L495) —— `cd build/` 后按优先级分派：`-u` 走 UT 分支（全量时先用 `-DOP_HOST_UT=TRUE` 配置并构建 `transformer_op_host_ut`，删除 `CMakeCache.txt` 后再用 `-DOP_API_UT=TRUE` 构建 `transformer_op_api_ut`，见 L460-476）；`--opapi/--ophost` 走 `build_lib` 打库；`-b host` 在 `cmake_config` 时加 `-DENABLE_OPS_KERNEL=OFF` 并把 `build/*.run` 拷贝到 `output/`（L479-485）；`-b kernel` 加 `-DENABLE_OPS_HOST=OFF` 只构建 `ops_kernel` 目标；**默认分支**（L493-494）就是 `cmake_config` + `build_package`，即「配置 + 构建 package 目标」产出完整 run 包。

其中 `cmake_config` 与 `build` 两个基础函数非常薄：

[inference/ascendc/build.sh:L93-L107](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L93-L107) —— `cmake_config` 只做 `cmake .. ${CUSTOM_OPTION} ${extra_option}`；`build` 只做 `cmake --build . --target <target> -j<N> [--verbose]`。所有差异都体现在传进去的 `-D` 变量和 target 名上。

**⑥ `-u` 与 UT 目录检查**：

[inference/ascendc/build.sh:L178-L192](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L178-L192) —— `set_ut_mode`：`-u` 默认全量（`UT_TEST_ALL=TRUE`）；叠加 `--ophost` 或 `--opapi` 则只构建对应一类 UT。配合 [build.sh:L201-L256](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L201-L256) 的目录检查：若 `-n` 指定的算子有 `op_api/` 却没有 `tests/ut/op_api/`（或有 `op_host/` 却没有 `tests/ut/op_host/`），直接报错退出——即「有实现就必须有 UT」。UT 框架细节留到 u6-l1。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：把 `bash build.sh -n 'ai_infra_scatter_block_update' -c ascend910_93` 这条命令在源码层面完整「走一遍」，产出一份流程说明（有昇腾环境则额外真实执行）。
2. **操作步骤**：
   - **任何环境都可做**：在 `inference/ascendc` 下执行 `bash build.sh --help`，对照输出阅读 [build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L46-L65)，确认帮助文本与 case 分支一一对应（该命令在参数解析阶段即退出，不触碰 CANN 环境）；
   - 无环境（源码阅读型）：按 4.2.2 的 ①→⑦ 顺序，为每一步标注「所在文件:行号 + 关键变量取值」。对本次命令，写出：`ascend_op_name='ai_infra_scatter_block_update'`、`ascend_compute_unit=ascend910_93`，`CUSTOM_OPTION` 依次为 `-DBUILD_OPEN_PROJECT=ON`（初始值，[build.sh:L39-L40](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L39-L40)）→ `+ -DASCEND_COMPUTE_UNIT=ascend910_93` → `+ -DASCEND_OP_NAME=ai_infra_scatter_block_update` → `+ -DCUSTOM_ASCEND_CANN_PACKAGE_PATH=<探测结果> -DCHECK_COMPATIBLE=false`，最终走默认分支 `cmake_config` + `build_package`；
   - 有环境（真实执行型）：`cd inference/ascendc && bash build.sh -n 'ai_infra_scatter_block_update' -c ascend910_93`，记录终端关键输出。
3. **需要观察的现象**：
   - 无环境：核对流程说明时，能准确指出 `set_env`（bisheng 检查）发生在 `clean` 之前、`cd build` 之前；
   - 有环境：日志先打印 `Info: cmake config ...` 一长串 `-D` 变量（即 `CUSTOM_OPTION` 的真身），随后是编译输出，最后出现 README 提示的成功标志 `Self-extractable archive "CANN-omni_custom_ops-<cann_version>-linux.<arch>.run" successfully created.`（[README.md:L270-L279](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L270-L279)）。
4. **预期结果**：
   - 无环境：产出一份「参数解析 → cmake_config → build_package」的流程说明文档（含每个 `-D` 变量的来源行号）；本部分为源码阅读型实践，**命令真实运行结果待本地验证**；
   - 有环境：在 `build/`（及 README 所述 `output/`）目录记录到 `CANN-omni_custom_ops-<version>-linux.<arch>.run` 产物路径。`.run` 从 `build/`（`CPACK_PACKAGE_DIRECTORY`，见 4.3.3 ⑤）到 `output/` 的搬运细节由 CANN 包内的 makeself 脚本完成，仓库内不可见，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`bash build.sh -c "ascend910b;ascend910_93"` 中两个芯片型号是如何传给 CMake 的？
**答案**：`-c` 的值存入 `ascend_compute_unit`，在 [build.sh:L358-L360](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L358-L360) 被原样拼成 `-DASCEND_COMPUTE_UNIT=ascend910b;ascend910_93`（引号保证分号作为单参数传给 cmake）；CMake 侧把它按 `;` 解析为列表，并在 [CMakeLists.txt:L726-L733](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L726-L733) 对每个型号各建一个二进制编译目标。

**练习 2**：`bash build.sh -b host` 与默认全量构建相比少做了什么？
**答案**：`-b host` 在 `cmake_config` 时追加 `-DENABLE_OPS_KERNEL=OFF`（[build.sh:L479-L485](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L479-L485)），即跳过 NPU 侧 kernel 二进制编译，只构建 host 侧并打 run 包，随后把 `.run` 拷贝到 `output/`。适合只改了 `op_host`/`op_api` 代码时加快迭代。

**练习 3**：不加 `--ccache` 参数时脚本会怎么处理 ccache？
**答案**：[build.sh:L451-L457](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L451-L457) 会用 `which ccache` 探测系统 ccache，找到则追加 `-DENABLE_CCACHE=ON -DCUSTOM_CCACHE=<路径>` 并调用 `gen_bisheng` 生成 ccache 包装的 bisheng；找不到则维持 `-DENABLE_CCACHE=OFF`（`CMakeLists.txt` 中该 option 默认 ON，但无程序时不会生效），构建照常进行、只是没有缓存加速。

---

### 4.3 CMake 侧：算子收集、产物目标与 run 包打包

#### 4.3.1 概念说明

`build.sh` 把控制权交给 CMake 后，`CMakeLists.txt` 接手，做三件事：

1. **定义产物目标**：把所有算子的 `op_api`、`op_host`、`op_kernel` 代码分别链接成若干动态库（opapi/opsproto/optiling 等）；
2. **收集算子**：用 `file(GLOB)` 扫描 `src/ops-transformer` 与 `src/ops-nn` 下的算子目录，按 `-n` 过滤后逐个 `add_subdirectory`；
3. **打包**：用 CPack 的 External 生成器 + makeself，把 install 规则收集的全部文件打成自解压 run 包。

理解「算子目录 → CMake 目标 → run 包内路径」的映射，是本节目标。

#### 4.3.2 核心流程

```text
cmake ..（cmake_config）
   ├─ include cmake/config.cmake → 环境检查 + prepare.sh 预构建（生成 autogen）
   ├─ 定义顶层目标：opapi / opsproto / optiling / op_host_aclnn 系列
   ├─ op_add_subdirectory：GLOB 扫描算子目录，按 ASCEND_OP_NAME 过滤 → OP_DIR_LIST
   ├─ foreach OP_DIR：add_subdirectory（把每个算子的源码挂进目标）
   └─ CPack 配置（run 包名、External 生成器）
cmake --build . --target package
   └─ 编译各目标 → 按 install() 归档到 packages/vendors/omni_custom_transformer/... → makeself 打 .run
```

#### 4.3.3 源码精读

**① 顶层默认值**——所有 `-D` 变量的缺省都在这里：

[inference/ascendc/CMakeLists.txt:L13-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L13-L26) —— `BUILD_OPEN_PROJECT` 默认 ON（`build.sh` 初始 `CUSTOM_OPTION` 也会显式带上它）；`ASCEND_COMPUTE_UNIT` 默认 `ascend910_93`；`ASCEND_OP_NAME` 默认 `ALL`；`VENDOR_NAME` 固定为 `omni_custom_transformer`——这个值决定了安装后 vendors 目录名以及 `source .../vendors/omni_custom_transformer/bin/set_env.bash` 的路径。随后 include 的 6 个 cmake 模块（config/func/intf/tiling_sink/variables/ut）各司其职。

**② 三大产物动态库与安装位置**。run 包内部结构与这三个目标一一对应：

[inference/ascendc/CMakeLists.txt:L108-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L108-L155) —— `opapi` 共享库：聚合所有算子 `op_api/aclnn_*.cpp`，链接 CANN 的 `nnopbase`、`libops_base.so` 等，输出名改为 `cust_opapi`，安装到 `packages/vendors/${VENDOR_NAME}/op_api/lib`。这就是 u1-l1 说的「aclnn 接口层」的载体。

[inference/ascendc/CMakeLists.txt:L158-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L158-L201) —— `opsproto` 共享库：输出名 `cust_opsproto_rt2.0`，安装到 `op_proto/lib/linux/${CMAKE_SYSTEM_PROCESSOR}`。它承载算子原型（def）注册。

[inference/ascendc/CMakeLists.txt:L204-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L204-L257) —— `optiling` 共享库：输出名 `cust_opmaster_rt2.0`，安装到 `op_impl/ai_core/tbe/op_tiling/lib/linux/${CMAKE_SYSTEM_PROCESSOR}`，并额外生成一个 `compat/liboptiling.so` 软链（L259-274）兼容旧查找路径。它承载 tiling 实现。

**③ 算子目录收集与 `-n` 过滤**：

[inference/ascendc/cmake/func.cmake:L41-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L41-L80) —— `op_add_subdirectory` 用 `file(GLOB)` 扫描 `src/ops-transformer/**/**/CMakeLists.txt` 与 `src/ops-nn/**/**/CMakeLists.txt`（含 `ophost/` 变体），由文件路径反推算子名（目录名）；若 `ASCEND_OP_NAME` 非 `ALL`，则只保留列表内的算子（L62-68 的 `continue` 跳过其余）。这就是 `-n 'ai_infra_scatter_block_update'` 能把编译范围缩小到一个算子的机制。结果经排序去重后回传给顶层。

[inference/ascendc/CMakeLists.txt:L303-L305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L303-L305) 与 [L343-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L343-L352) —— 顶层调用该函数得到 `OP_DIR_LIST`，再逐个 `add_subdirectory`（优先挂 `ophost/` 子目录变体），把每个算子的源码、注册代码挂进前面的全局目标。

**④ 由 def 文件自动生成 aclnn 代码与预构建**：

[inference/ascendc/CMakeLists.txt:L386-L398](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L386-L398) —— 遍历 `op_host_aclnn` 目标收集到的 `*_def.cpp` 源文件，把 `xxx_def.cpp` 映射为待生成的 `aclnn_xxx.cpp`、`xxx_proto.cpp` 等文件名（`string(REGEX REPLACE "_def$" ...)`）。真正的生成动作由 [CMakeLists.txt:L593-L642](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L593-L642) 的 `opbuild_gen_*` 自定义目标完成——调用 CANN 包的 `op_build` 工具从 def 注册信息反推生成 aclnn/proto 骨架。工具路径定义在 [variables.cmake:L73-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/variables.cmake#L73-L77)（`<CANN>/tools/opbuild/op_build`）。

[inference/ascendc/cmake/config.cmake:L196-L243](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L196-L243) —— CMake **配置阶段**会执行 `cmake/scripts/prepare.sh`（非 UT 构建时 `ENABLE_OPS_KERNEL` 默认 ON，见 [config.cmake:L38-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L38-L43)）；它把 `ASCEND_COMPUTE_UNIT`、`ASCEND_OP_NAME`、`TILING_KEY` 等以 `;`→`::` 转换后传下去。prepare.sh 内部会以 `PREPARE_BUILD=ON` 再起一层 CMake（[prepare.sh:L108-L118](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/scripts/prepare.sh#L108-L118)），预先产出 autogen 中间文件。即整体是「外层 CMake → prepare.sh → 内层 CMake」的两级结构，初学阶段只需记住：**配置阶段就发生了大量代码生成**。

**⑤ NPU 二进制与 CPack 打包**：

[inference/ascendc/CMakeLists.txt:L720-L734](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L720-L734) —— `ENABLE_OPS_KERNEL` 开启时创建 `ops_kernel` 目标，对 `ASCEND_COMPUTE_UNIT` 列表中**每种芯片**调用 `add_bin_compile_target` 生成对应二进制。这解释了为什么 `-c` 传多个型号会成倍增加编译时间。

[inference/ascendc/CMakeLists.txt:L736-L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L736-L780) —— 打包三部曲：
- `modify_vendor`（L736-751）：从 CANN 包的工程模板（[config.cmake:L68-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L68-L77) 定位的 `tools/ascend_project`）拷贝 `install.sh/upgrade.sh` 并用 `sed` 把其中的 `vendor_name=customize` 替换为 `omni_custom_transformer`；
- `gen_version_info`（L753-766）：调用 `gen_version_info.sh` 生成 `version.info`（README 目录树里看到的 `version.info` 是**构建产物**，源码树中并不存在）；
- CPack（L768-780）：`CPACK_PACKAGE_FILE_NAME = CANN-omni_custom_ops-${CANN_VERSION}-linux.${CMAKE_SYSTEM_PROCESSOR}.run`，生成器为 `External` + `makeself.cmake`，即 run 包文件名的唯一出处。安装前缀 `CMAKE_INSTALL_PREFIX` 被指到源码树 `output/`（[variables.cmake:L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/variables.cmake#L98)，另见 [config.cmake:L152-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L152-L155) 的默认值修正逻辑）。

#### 4.3.4 代码实践

1. **实践目标**：不运行 CMake，纯静态读出「算子目录 → 产物库 → run 包内路径」的映射表。
2. **操作步骤**：
   - 打开 [CMakeLists.txt:L150-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L150-L155)、[L196-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L196-L201)、[L247-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L247-L257) 三段，抄下三个目标的 `OUTPUT_NAME` 与 `DESTINATION`；
   - 执行 `ls src/ops-transformer/index/ai_infra_scatter_block_update/`，确认该目录有自己的 `CMakeLists.txt`，从而会被 [func.cmake:L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L45) 的 GLOB 命中；
   - 把结果整理成三列表格（目标名 / so 输出名 / run 包内路径）。
3. **需要观察的现象**：三个库的安装路径前缀完全一致（`packages/vendors/omni_custom_transformer/`），差异只在中间层级（`op_api/lib`、`op_proto/lib/linux/<arch>`、`op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>`）。
4. **预期结果**：得到如下映射表（路径以源码为准）：

   | CMake 目标 | so 输出名 | run 包内路径（相对包根） |
   | --- | --- | --- |
   | opapi | cust_opapi | packages/vendors/omni_custom_transformer/op_api/lib |
   | opsproto | cust_opsproto_rt2.0 | packages/vendors/omni_custom_transformer/op_proto/lib/linux/\<arch\> |
   | optiling | cust_opmaster_rt2.0 | packages/vendors/omni_custom_transformer/op_impl/ai_core/tbe/op_tiling/lib/linux/\<arch\> |

#### 4.3.5 小练习与答案

**练习 1**：为什么删除某个算子目录后，不需要修改 `CMakeLists.txt` 也能让构建系统「忘掉」它？
**答案**：算子发现靠 [func.cmake:L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L45) 的 `file(GLOB)` 动态扫描，目录（连同其 `CMakeLists.txt`）消失后自然不再被收集；顶层只在 [CMakeLists.txt:L343-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L343-L352) 遍历扫描结果。新增算子同理（u6-l3 会利用这一点）。

**练习 2**：算子 `op_host/` 目录下的 `*_def.cpp` 文件在构建中起了什么双重作用？
**答案**：一方面作为 `op_host_aclnn` 系列目标的源码被编译；另一方面 [CMakeLists.txt:L386-L398](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L386-L398) 从文件名（去掉 `_def` 后缀）推导出要生成的 `aclnn_*.cpp`/`*_proto.cpp` 清单，由 `op_build` 工具生成代码骨架。

**练习 3**：run 包名 `CANN-omni_custom_ops-<version>-linux.<arch>.run` 中各段分别由哪段代码决定？
**答案**：`omni_custom_ops` 与 `<version>` 出自 [CMakeLists.txt:L774](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L774) 的 `CPACK_PACKAGE_FILE_NAME` 模板（`CANN_VERSION` 来自版本校验流程或 CANN 包信息）；`linux.<arch>` 是 `${CMAKE_SYSTEM_PROCESSOR}` 拼接（如 `linux.aarch64`）；`.run` 后缀本身就是文件名模板的一部分。

---

### 4.4 run 包安装与 vendors 目录生效

#### 4.4.1 概念说明

run 包不是「解压即用」。CANN 运行时只在固定位置（`<toolkit>/opp/vendors/<vendor_name>`）发现自定义算子，因此安装的实质是：**把 run 包内容释放到 CANN 的 opp 目录下，再 source 厂商提供的 `set_env.bash`，把算子的 so 路径注入 `LD_LIBRARY_PATH` 等环境变量**。之后 aclnn 调用才能找到自定义算子的接口与实现。

#### 4.4.2 核心流程

```text
cd output
chmod +x CANN-omni_custom_ops-<version>-linux.<arch>.run
./CANN-omni_custom_ops-<version>-linux.<arch>.run --quiet --install-path=<toolkit>/opp
        │  （install.sh 将包内 packages/vendors/omni_custom_transformer/ 释放到目标）
        ▼
<toolkit>/opp/vendors/omni_custom_transformer/
        ├── op_api/lib/cust_opapi.so            ← aclnn 接口
        ├── op_proto/lib/linux/<arch>/cust_opsproto_rt2.0.so
        ├── op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>/cust_opmaster_rt2.0.so
        ├── op_impl/ai_core/tbe/.../（kernel 二进制与源码）
        └── bin/set_env.bash
        │
source <toolkit>/opp/vendors/omni_custom_transformer/bin/set_env.bash
        ▼
环境变量注入完成，自定义算子可被 aclnn / torch_npu 调用
```

#### 4.4.3 源码精读

**① 官方安装三步**：

[inference/ascendc/README.md:L281-L292](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L281-L292) —— `chmod +x` 赋可执行权限；`--quiet --install-path=/usr/local/Ascend/ascend-toolkit/latest/opp` 静默安装到 opp 目录；随后 `source .../opp/vendors/omni_custom_transformer/bin/set_env.bash` 生效。README 明确说明安装前提：**run 包与已装 CANN 套件包的 CPU 架构必须一致**。

**② 安装脚本的来源**：run 包内的 `install.sh/upgrade.sh` 并不在本仓库里，而是构建时从 CANN 包模板拷贝并改写 vendor 名：

[inference/ascendc/CMakeLists.txt:L736-L751](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L736-L751) —— `modify_vendor` 目标把 `${ASCEND_PROJECT_DIR}/scripts/*`（`ASCEND_PROJECT_DIR` 由 [config.cmake:L68-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L68-L77) 定位到 CANN 包的 `tools/ascend_project` 或 `tools/op_project_templates/ascendc/customize`）拷进构建目录，`sed` 替换 `vendor_name=customize` → `vendor_name=omni_custom_transformer`，再随包安装。`bin/set_env.bash` 也由这套随包脚本在安装时布置——因此本仓库源码中搜不到它的生成处。

**③ vendors 目录结构与 4.3 的 install 规则逐条对应**：安装后每个子目录都来自 4.3.3 ②的某条 `install()`（`op_api/lib` ← opapi 目标；`op_proto/lib/linux/<arch>` ← opsproto；`op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>` ← optiling；kernel 实现文件 ← [CMakeLists.txt:L665-L698](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L665-L698) 把 `op_kernel` 源码与目录安装到 `${VENDOR_NAME}_impl/ascendc/<算子名>`）。**CANN 正是通过这套标准布局，在运行时把「自定义算子」与「内置算子」统一管理**。

**④ 双包视角的另一半**：run 包装好后，PyTorch 侧还需要安装 wheel 包（`torch_ops_extension/build_and_install.sh`，[README.md:L294-L301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L294-L301)），产出 `omni_custom_ops-1.0-<python_version>-<arch>.whl`。这是 u1-l4 的主题，本讲只需记住顺序：**先 run 包、后 wheel 包**（wheel 里的适配层要 dlopen run包提供的 aclnn 符号）。

#### 4.4.4 代码实践

1. **实践目标**：把「安装命令 → vendors 目录结构 → 环境变量」串成一条可核对的链。
2. **操作步骤**：
   - 无环境：对照 [README.md:L286-L292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L286-L292) 三条命令，在 4.3.4 得到的映射表上补一列「安装后的绝对路径」（把 `packages/vendors/...` 前缀替换为 `<toolkit>/opp/vendors/...`）；
   - 有环境：执行安装后 `ls /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_custom_transformer/` 与 `ls .../op_api/lib/`，再 `source .../bin/set_env.bash && echo $LD_LIBRARY_PATH | tr ':' '\n' | grep -i omni`。
3. **需要观察的现象**：vendors 目录下的子目录名与 4.3.4 表格的 run 包内路径逐层一致；source 之后 `LD_LIBRARY_PATH` 中出现 vendors 下的 lib 路径。
4. **预期结果**：无环境——完成一张「install 目标 → 包内路径 → 安装后绝对路径」三列对照表；有环境——确认 `cust_opapi.so` 等三个 so 落位且环境变量已注入（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：把 run 包安装到 `/home/me/Ascend/ascend-toolkit/latest/opp` 时报架构不匹配，最可能的原因是什么？
**答案**：run 包是在 aarch64 环境编译的（文件名含 `linux.aarch64`），而目标 CANN 套件是 x86_64（或相反）。README [L283-L288](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L283-L288) 明确要求安装前保证 run 包与 CANN 开发套件包的 CPU 架构一致。

**练习 2**：为什么安装完必须 `source set_env.bash`，不 source 会怎样？
**答案**：CANN 按环境变量（如 `LD_LIBRARY_PATH`、`ASCEND_OPP_PATH` 相关路径）查找自定义算子的 so 与注册信息；不 source 则进程加载不到 `cust_opapi.so` 等，调用 aclnn 接口时会报找不到符号/算子。这正是 u1-l1 所说「run 包是发动机、装好还要接上传动」的一环。

---

## 5. 综合实践

**任务：编写你自己的《openPangu 推理算子构建手册》一页纸**（无需硬件即可完成）。

以 `bash build.sh -n 'ai_infra_scatter_block_update' -c ascend910_93` 为分析对象，产出一张覆盖全链路的流程说明，要求包含四部分：

1. **环境段**：列出编译前置条件（bisheng、CANN 包、python3——python3 检查见 [config.cmake:L14-L18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/config.cmake#L14-L18)），并标注每项在哪个脚本/哪一行被检查。
2. **参数段**：把该命令涉及的所有 `-D` 变量按「来源参数 → 追加位置行号 → CMake 侧消费位置」列成表（至少包含 `ASCEND_OP_NAME`、`ASCEND_COMPUTE_UNIT`、`CUSTOM_ASCEND_CANN_PACKAGE_PATH`、`BUILD_OPEN_PROJECT` 四项）。
3. **产物段**：从 [CMakeLists.txt:L768-L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L768-L780) 推导 run 包文件名模板，画出包内 `vendors/omni_custom_transformer/` 的目录树并标注每个子目录由哪个 CMake 目标产出。
4. **安装段**：写出三条安装/生效命令，并预测 `source set_env.bash` 前后 `LD_LIBRARY_PATH` 的差异。

完成后自测：遮住讲义，仅凭你的一页纸向同事讲清「一条 `build.sh` 命令从敲下到 run 包落盘经历了什么」。有昇腾环境的读者，可再真实执行一遍并对照你的手册修正差异（`.run` 在 `build/` 与 `output/` 间的实际落位建议现场确认）。

## 6. 本讲小结

- 编译环境 = 预置 Docker 镜像（A2/A3/A5）+ 容器内 `source set_env.sh`；`build.sh` 启动即检查 `bisheng`，CANN 包路径按 `-p` > `ASCEND_HOME_PATH` > `ASCEND_OPP_PATH` > 默认 toolkit 目录的优先级探测。
- `build.sh` 是「参数翻译器」：`-c`→`ASCEND_COMPUTE_UNIT`、`-n`→`ASCEND_OP_NAME`、`--tiling_key`→`TILING_KEY`、`-u`→`ENABLE_TEST`，全部拼进 `CUSTOM_OPTION` 后用一次 `cmake_config`（`cmake ..`）+ `cmake --build --target` 驱动不同目标。
- 主流程按 `-u`（UT 两段构建）> `--opapi/--ophost`（打 lib）> `-b host/kernel`（裁剪构建）> 默认（全量 `build_package`）的优先级分派；`-n` 的过滤在 CMake 侧由 `op_add_subdirectory` 的 GLOB + 名单比对实现。
- run 包内三大产物：`cust_opapi.so`（op_api/lib）、`cust_opsproto_rt2.0.so`（op_proto）、`cust_opmaster_rt2.0.so`（op_tiling），均 install 到 `packages/vendors/omni_custom_transformer/` 下，最终由 CPack External + makeself 打成 `CANN-omni_custom_ops-<version>-linux.<arch>.run`。
- 安装三步：`chmod +x` → `--install-path=<toolkit>/opp` → `source .../vendors/omni_custom_transformer/bin/set_env.bash`；`version.info`、`install.sh`、`set_env.bash` 都是构建/安装期产物，源码树中没有。
- 顺序上「先 run 包、后 wheel 包」：本讲的 run 包提供算子本体，u1-l4 的 wheel 提供PyTorch 调用入口。

## 7. 下一步学习建议

- **下一讲 u1-l3《解剖一个算子目录：以 ScatterBlockUpdate 为例》**：本讲我们把算子当黑盒从构建视角看过了一遍，下一讲打开黑盒，逐文件讲解 `docs/op_api/op_host/op_kernel/tests` 五件套的职责分工，为第 2 单元的三层结构精读做准备。
- 若你手头有昇腾环境，建议先跑通本讲的 `-n 'ai_infra_scatter_block_update' -c ascend910_93`，并保留 `build/` 目录——后续讲义讲到的 `cust_opapi.so`、autogen 文件都能在里面找到实物。
- 想提前理解 `build.sh -u` 背后的 UT 机制，可先浏览 `src/tests/ut/framework_normal/` 目录；系统讲解在 u6-l1。
