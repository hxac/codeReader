# 环境搭建、安装验证与功能运行示例

## 1. 本讲目标

学完本讲，你应该能够：

1. 按照官方 `quick_install.md` 文档，选择 WebIDE / Docker / 手动安装三种方式之一，准备好运行 oam-tools 所需的 CANN 环境。
2. 理解仓库根目录 `init_env.sh` 一键脚本做了什么：装系统依赖、装 CANN、加载环境变量、装 Python 依赖。
3. 完成环境变量加载（`source set_env.sh`），并用 `npu-smi info` 等命令验证驱动与 CANN 安装。
4. 用 `bash build.sh -u` 跑通组件测试，验证源码工作正常（对应「安装验证」）。
5. 按仓库 `examples/` 目录下的三个 `run.sh` 脚本，跑通 asys、msaicerr、msprof 的最小功能示例，并记录 asys 各子命令的输出目录结构。

本讲是入门单元的最后一讲：u1-l2 让你知道怎么编译打包，u1-l3 让你知道代码在哪里，本讲让你把环境真正跑起来，为 u2 单元精读 asys 源码做好环境与直觉准备。

## 2. 前置知识

- **驱动与固件**：昇腾 NPU 的底层软件。驱动负责管理 `/dev/davinci*` 等设备节点，固件是跑在芯片上的程序。没有驱动，`npu-smi info` 就无法工作。
- **CANN**：华为昇腾计算架构，是跑在驱动之上的算子库与工具链。oam-tools 的四个组件都以 CANN 安装目录为「家」——安装后会被释放到 `${CANN}/tools/` 下。
- **CANN toolkit 包 / ops 包**：toolkit 包是编译与基础能力（编译态也需要）；ops 包是芯片专属算子二进制（只运行时需要，即「运行态」）。两者用 `npu-smi info` 显示的芯片型号对应选择（如 `910B → 910b`、`910_93 → A3`、`950 → 950`）。
- **`set_env.sh` / `setenv.bash`**：CANN 安装后提供的环境变量脚本。执行 `source` 后，`PATH`、`LD_LIBRARY_PATH`、`ASCEND_HOME_PATH` 等变量会指向 CANN 安装目录，之后才能直接调用 `asys`、`msprof` 等工具。
- **UT（Unit Test）**：单元测试。`build.sh -u` 会在构建后自动执行 `scripts/run_tests.sh`，这就是本讲的「安装验证」手段。
- **`.run` 安装包**：昇腾软件的自解压安装格式。oam-tools 构建产物就是一个 `.run` 包（见 u1-l2）。

如果这些概念还模糊，建议先回顾 u1-l1（CANN、芯片型号部分）和 u1-l2（构建与打包部分）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/zh/quick_install.md` | 官方环境部署文档：三种环境搭建方式、依赖清单、源码下载、离线编译、环境验证与环境变量配置 |
| `init_env.sh` | 仓库自带的一键开发环境初始化脚本（装系统依赖 → 装 CANN → 配环境 → 装 Python 依赖） |
| `examples/README.md` | 三个功能示例的用途说明 |
| `examples/deploy.sh` | 依次调用三个组件示例脚本的总入口 |
| `examples/asys/run.sh` | asys 最小示例：执行 `asys health` |
| `examples/msaicerr/run.sh` | msaicerr 最小示例：加载 setenv 后执行 `msaicerr.py -e`（环境检查） |
| `examples/msprof/run.sh` | msprof 最小示例：加载 setenv 后用 `msprof` 命令采集 5 秒系统级 CPU/内存数据 |
| `build.sh`（节选） | `-u` 参数如何打开 UT 并驱动 `scripts/run_tests.sh` |
| `src/asys/asys.py`、`src/asys/common/task_common.py`、`src/asys/common/compress_output_dir.py` | 用于解释实践任务中观察到的 asys 输出目录行为 |

## 4. 核心概念与源码讲解

### 4.1 环境准备：quick_install 文档的三种方式

#### 4.1.1 概念说明

oam-tools 是「运行在 CANN 之上的运维工具」，所以跑它之前必须先有 CANN。官方文档 `docs/zh/quick_install.md` 把使用者分成两类场景：

- **编译态**：只编译 oam-tools 源码、不运行。只需安装 CANN toolkit 包。
- **运行态**：要真正运行工具（本讲的目标）。需要驱动固件 + toolkit 包 + ops 包三件套。

文档提供了三种搭建方式，按「有无昇腾设备」选择：

| 方式 | 适用人群 | 特点 |
| --- | --- | --- |
| WebIDE（CANNLab） | 无昇腾设备 | 在线环境，预装驱动固件与 CANN，源码默认在 `/mnt/workspace` |
| Docker | 有无设备均可，想快速搭环境 | 镜像预集成 CANN；跑样例需把宿主机 `/dev/davinci0` 等设备挂进容器 |
| 手动安装 | 有昇腾设备 | 自己装驱动、固件、toolkit 包、ops 包 |

#### 4.1.2 核心流程

以最完整的「手动安装 + 运行态」为例：

```
确认前置依赖版本（python>=3.10、gcc>=7.3、cmake>=3.16、ccache……）
    ↓
npu-smi info 查看 Name 列 → 查表得到 chip_type（910B→910b，910_93→A3，950→950）
    ↓
安装驱动与固件（参考 CANN 软件安装指南）
    ↓
安装 CANN toolkit 包：Ascend-cann-toolkit_${cann_version}_linux-${arch}.run
    ↓
安装 CANN ops 包：    Ascend-cann-${chip_type}-ops_${cann_version}_linux-${arch}.run
    ↓
pip3 install -r requirements.txt   （Python 运行时/UT 依赖）
    ↓
环境验证（见 4.1.3）→ source set_env.sh（见 4.4）
```

其中芯片型号匹配规则要注意：`npu-smi info` 实际可能显示带子型号的字符串（如 `910B1`/`910B4`），按「Name 列包含关键字」匹配即可；业内别称"910C"对应包名中的 `A3`。

#### 4.1.3 源码精读

文档规定了前置依赖清单与芯片对应表，这是你核对环境的依据：

- [docs/zh/quick_install.md:136-156](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L136-L156)：列出 python/gcc/cmake/ccache 版本要求、CANN 组合包命名格式，以及 `protobuf`、`pytest`、`googletest` 等只在执行 UT 时需要的依赖。注意区分两个 protobuf：C++ 编译用的 25.1（cmake 拉取）和 Python 包 `protobuf>=6.33.4`（PyPI 发版，版本号体系不同，二者不冲突）。
- [docs/zh/quick_install.md:163-174](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L163-L174)：芯片型号与 CANN ops 包对应关系表，以及「子型号按包含匹配」「910C=A3」两条匹配规则。
- [docs/zh/quick_install.md:302-332](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L302-L332)：环境验证三步——`npu-smi info` 验驱动、`cat .../ascend_toolkit_install.info` 与 `ascend_ops_install.info` 验 CANN 两包版本、`pytest --version` / `coverage --version` 验 Python 依赖。
- [docs/zh/quick_install.md:334-342](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L334-L342)：环境变量配置命令。默认路径安装（root）时执行 `source /usr/local/Ascend/cann/set_env.sh`；非 root 或指定路径安装时换成对应路径。

Docker 方式的设备挂载参数也在本文件中：[docs/zh/quick_install.md:69-98](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L69-L98)，其中 `--device /dev/davinci_manager`、`--device /dev/devmm_svm`、`--device /dev/hisi_hdc` 三个是必挂项，分别对应设备管理、设备内存管理和 HDC 通信通道。

#### 4.1.4 代码实践

1. **实践目标**：确认（或搭建）一个可用的运行态环境，并留下一份「环境快照」。
2. **操作步骤**：
   - 若无设备：按文档方式 1 使用 WebIDE，进入后源码在 `/mnt/workspace`。
   - 若用 Docker：按文档拉取镜像并用「场景1」参数启动容器（挂载 NPU 设备）。
   - 若有设备：按方式 3 安装驱动、固件与 CANN 两包。
   - 统一执行 `pip3 install -r requirements.txt`。
3. **需要观察的现象**：`npu-smi info` 能列出设备表格；两个 `install.info` 文件能 `cat` 出版本号；`pytest --version` 正常输出。
4. **预期结果**：三项验证全部通过，记下本机 `Name` 列的芯片型号（后续选 ops 包、跑 msaicerr `-e` 都会用到）。
5. 本讲在无设备环境下可只做「纸面流程」：把 4.1.2 的流程图抄一遍并标注每一步的输入输出。实际运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么「只编译不运行」可以不装驱动和 ops 包？

**答案**：编译只需要头文件、库和 CMake 工程文件，这些在 toolkit 包里；驱动是内核态的设备管理软件，ops 包是芯片上的算子二进制，二者都只在真正把算子/工具跑在 NPU 上时（运行态）才被用到。`quick_install.md` 开头的「编译态/运行态」说明（第 9-12 行）明确了这个区分。

**练习 2**：`npu-smi info` 显示 `910B3`，应该装哪个 ops 包？

**答案**：按「Name 列包含关键字」规则匹配到 `910B`，对应 `${chip_type}` 为 `910b`，即 `Ascend-cann-910b-ops_${cann_version}_linux-${arch}.run`（见 quick_install.md:165-174 的对应表与匹配说明）。

### 4.2 一键初始化：init_env.sh

#### 4.2.1 概念说明

手动按文档装环境步骤多，仓库因此提供了根目录的 `init_env.sh`，把「装系统依赖 → 装 CANN → 配置环境变量 → 装 Python 依赖」串成一条命令。quick_install.md 的 Docker 章节甚至给出了远程管道用法：

```bash
curl -fsSL https://raw.gitcode.com/cann/oam-tools/raw/master/init_env.sh | bash
```

理解这个脚本的价值在于：它把官方文档里的动作翻译成了可读的 shell 代码，读脚本是「文档之外的第二真相」。

#### 4.2.2 核心流程

`main()` 是总调度，流程为：

```
解析参数（--cann-version / --chip-type / --install-path / --skip-cann / --skip-ops）
    ↓
CANN 版本为空 → 从 version.cmake 解析 set_cann_package(VERSION "x.y.z")
    ↓
install_system_deps   检查/安装 curl wget git cmake make g++（root 用 apt-get/yum）
    ↓
install_cann          detect_cann_path 探测已有安装；没有则按架构下载并安装 toolkit+ops 两个 .run 包
    ↓
setup_cann_env        source setenv.bash，并补建 pkg_inc 软链（mmpa/fmk/ts/adump）
    ↓
install_python_deps   pip install -r requirements.txt（本地没有则从远程拉）
    ↓
打印 Next steps 提示（build.sh / build.sh -u / build.sh -u --cov）
```

#### 4.2.3 源码精读

- [init_env.sh:425-511](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/init_env.sh#L425-L511)：`main()` 函数。参数 case 分支在 430-461 行；`install_system_deps` → `install_cann` → `setup_cann_env` → `install_python_deps` 的调用顺序在 490-499 行；最后 506-510 行打印的 "Next steps" 直接告诉你下一步就是 `bash build.sh -u`。
- [init_env.sh:59-85](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/init_env.sh#L59-L85)：`get_cann_version_from_cmake`。本地没有 `version.cmake` 时会用 `curl` 从远程仓拉一份，再用 sed 从 `set_cann_package(VERSION "...")` 里抠出版本号——这就是脚本「不传版本也能装」的原因。
- [init_env.sh:87-102](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/init_env.sh#L87-L102)：`get_ops_package_chip_type`，把 `910_93`/`910c`/`A3` 等各种写法归一化成包名里的 `A3`，与 quick_install.md 的芯片表互相印证。
- [init_env.sh:113-143](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/init_env.sh#L113-L143)：`detect_cann_path`，按 `/usr/local/Ascend/ascend-toolkit/latest`、`~/Ascend/cann` 等一组候选路径探测「已装好的 CANN」，找到含 `bin/setenv.bash` 的目录即认可。`setup_cann_env`（210-245 行）随后 `source` 这个 `setenv.bash` 并导出 `ASCEND_HOME_PATH`。
- [init_env.sh:339-393](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/init_env.sh#L339-L393)：`install_python_deps`，优先装本地 `requirements.txt`，找不到时回退到从远程仓拉取内容再装，装完打印 pytest/coverage 版本做自检。

#### 4.2.4 代码实践

1. **实践目标**：不真正安装，也能说出 `init_env.sh` 每一步会对你机器做什么。
2. **操作步骤**：
   - 在仓库根目录执行 `bash init_env.sh --help`，把打印的 Options 与源码 430-461 行的 case 分支逐一对照。
   - 执行 `bash init_env.sh --skip-cann`（只装依赖、跳过约 3GB 的 CANN 下载），观察日志中 `[INFO]` 前缀的分步输出。
3. **需要观察的现象**：日志依次出现 `Checking system dependencies...`、`Setting up CANN environment...`（若本机无 CANN 此步会报错退出，属预期）、`Checking Python dependencies...`。
4. **预期结果**：能对照日志说出当前卡在流程图的哪一步。CANN 相关步骤的实际效果**待本地验证**（需要能访问昇腾镜像源）。
5. 注意：`--skip-ops`（别名 `--toolkit-only`）对应文档里的「编译态」——只装 toolkit 不装 ops。

#### 4.2.5 小练习与答案

**练习 1**：`init_env.sh` 是如何决定要下载哪个版本的 CANN 包的？

**答案**：优先级是「命令行 `--cann-version` 显式指定 > 从仓库根 `version.cmake` 的 `set_cann_package(VERSION "...")` 解析」，本地文件缺失时还会从远程仓拉 `version.cmake` 兜底（`get_cann_version_from_cmake`，init_env.sh:59-85；main 中 463-473 行的两级判空）。

**练习 2**：为什么脚本里 `install_cann` 开头先调用 `detect_cann_path`？

**答案**：幂等性考虑——若机器上已有 CANN（探测到含 `bin/setenv.bash` 的目录），直接复用并跳过约 3GB 的下载安装（init_env.sh:145-152），避免重复安装破坏现有环境。

### 4.3 安装验证：build.sh -u 与组件测试

#### 4.3.1 概念说明

「装好了」不等于「能用」。oam-tools 的验证手段是用 `bash build.sh -u` 在构建完成后自动执行测试（UT），这同时也是上一讲 `init_env.sh` 结尾提示的第一条命令。对纯 Python 组件（asys、msaicerr），这一步还会跳过整个 C++ 编译，只生成必要的 `chip_handler.py`，验证成本很低。

#### 4.3.2 核心流程

```
bash build.sh -u [--component asys] [--ut|--st] [--cov]
    ↓ checkopts 把 -u 翻译成 ENABLE_UT=on、EXEC_TEST=on（build.sh:113-117）
    ↓ 纯 Python 组件 → skip_build=on，仅 generate_asys_chip_handler（build.sh:350-354）
    ↓ （否则走完整 cmake/make 构建）
    ↓ source ${ASCEND_HOME_PATH}/bin/setenv.bash（build.sh:363-368）
    ↓ 拼装 run_tests_args（--component/--ut/--st/--cov）
    ↓ bash scripts/run_tests.sh "${run_tests_args[@]}"（build.sh:385）
```

#### 4.3.3 源码精读

- [build.sh:113-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L113-L117)：`-u` 参数的处理——同时置 `ENABLE_UT="on"` 和 `EXEC_TEST="on"`，即「构建 + 执行测试」。
- [build.sh:168-179](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L168-L179)：`--component`（限定测试组件）、`--ut`（只跑 UT）、`--st`（只跑 ST）三个范围控制参数。
- [build.sh:350-354](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L350-L354)：纯 Python 组件快车道——`is_python_only_component` 命中时跳过 C++ 构建，只生成 `chip_handler.py`。这正是 u1-l2 讲过的机制，在验证场景下的直接收益是「秒级进入测试」。
- [build.sh:363-390](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L363-L390)：测试执行段。先 source CANN 的 `setenv.bash`（注意这里再次出现环境变量加载，说明**测试对 CANN 环境有硬依赖**），再把 shell 层参数透传给 `scripts/run_tests.sh`，失败则整体以非零码退出。

#### 4.3.4 代码实践

1. **实践目标**：在当前环境跑通一次 asys 的 UT，作为「源码与环境都正常」的验证。
2. **操作步骤**：
   - 加载 CANN 环境变量（见 4.4）。
   - 在仓库根目录执行：`bash build.sh -u --component asys --ut`。
   - 若只想验证 msaicerr：`bash build.sh -u --component msaicerr --ut`。
3. **需要观察的现象**：终端先打印 `skip oam_tools build for python-only component: asys`（证明走了快车道），随后 pytest 逐条输出用例结果。
4. **预期结果**：`Execute run_tests.sh successful.`。若失败，最常见的两个原因是没 source CANN 环境变量、或 `pip3 install -r requirements.txt` 没执行。实际结果**待本地验证**。
5. 无设备环境同样可以跑这条命令：asys 的 UT 大量使用 mock，不要求真实 NPU（这正是「纯 Python 组件快车道」存在的意义）。

#### 4.3.5 小练习与答案

**练习 1**：`build.sh -u` 和 `build.sh -u --noexec` 有什么区别？

**答案**：`-u` 同时置 `ENABLE_UT=on` 与 `EXEC_TEST=on`；`--noexec` 把 `EXEC_TEST` 置回 `off`（build.sh:141-144），效果是照常构建但不真正执行测试，适合只想确认编译产物完整性的场景。

**练习 2**：为什么 `build.sh` 在执行测试前要 source `${ASCEND_HOME_PATH}/bin/setenv.bash`？

**答案**：因为测试运行的是安装态/接近安装态的工具代码，依赖 `LD_LIBRARY_PATH`、`ASCEND_HOME_PATH` 等指向 CANN 安装目录的环境变量；找不到该文件时脚本只 WARNING 并跳过（build.sh:363-368），后续测试可能因找不到动态库而失败。

### 4.4 功能示例：examples 脚本与 set_env 加载

#### 4.4.1 概念说明

环境验证通过后，下一步是验证功能。仓库在 `examples/` 下提供了三个「装完即跑」的最小示例，[examples/README.md:5-9](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/examples/README.md#L5-L9) 对它们的定位是：

| 样例 | 说明 |
| --- | --- |
| asys 信息 | 使用 asys 基础命令，入门首选 |
| msaicerr 环境检测 | 运行内置 sample 算子验证软硬件环境 |
| msprof 系统采集 | 采集 5 秒系统级 CPU/内存性能数据 |

三个脚本有一个共同前奏：**先定位 CANN 安装目录并 source `setenv.bash`**，再调用安装在该目录 `tools/` 下的工具（回顾 u1-l3：`.run` 包安装后把工具释放到 `${CANN}/tools/`）。

#### 4.4.2 核心流程

三个脚本的结构对比：

```
examples/asys/run.sh:     直接执行 asys health（依赖用户事先 source set_env.sh，工具已在 PATH 中）

examples/msaicerr/run.sh: CANN_ROOT = ${ASCEND_INSTALL_PATH:-${ASCEND_HOME_PATH:-/usr/local/Ascend/cann}}
                          → source $CANN_ROOT/bin/setenv.bash
                          → python3 $CANN_ROOT/tools/msaicerr/msaicerr.py -e

examples/msprof/run.sh:   CANN_ROOT 同上 → source setenv.bash
                          → $CANN_ROOT/tools/profiler/bin/msprof --output=./msprof_output
                            --host-sys-usage=cpu,mem --sys-period=5

examples/deploy.sh:       依次 bash asys/run.sh → msaicerr/run.sh → msprof/run.sh
```

注意 CANN_ROOT 的三级兜底写法 `${ASCEND_INSTALL_PATH:-${ASCEND_HOME_PATH:-默认路径}}`：优先用显式指定的安装路径，其次用环境变量里的 CANN 家目录，最后落到默认安装路径。这是本仓库脚本访问 CANN 的惯用模式。

#### 4.4.3 源码精读

- [examples/asys/run.sh:18-20](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/examples/asys/run.sh#L18-L20)：去掉版权头后整个脚本只有两行——`set -e` 和 `asys health`。它能「裸调」`asys`，是因为安装后软链接/可执行文件已在 PATH 中（见 u1-l3 的软链接机制），前提是用户当前 shell 已 source 过 `set_env.sh`。
- [examples/msaicerr/run.sh:18-23](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/examples/msaicerr/run.sh#L18-L23)：完整展示「定位 CANN_ROOT → source setenv.bash → 调用 `$CANN_ROOT/tools/` 下工具」三段式。`msaicerr.py -e` 是环境检查模式（运行内置 sample 算子验证软硬件，-p/-d/-e 三模式见 u1-l3）。
- [examples/msprof/run.sh:18-25](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/examples/msprof/run.sh#L18-L25)：同样三段式，最后调用 `$CANN_ROOT/tools/profiler/bin/msprof`，参数含义：`--output` 指定输出目录、`--host-sys-usage=cpu,mem` 只采集主机侧 CPU 与内存、`--sys-period=5` 采样周期 5 秒——与 README 里「采集 5 秒系统级数据」的描述一一对应。
- [examples/deploy.sh:18-23](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/examples/deploy.sh#L18-L23)：总入口，`cd` 到脚本所在目录后按 asys → msaicerr → msprof 顺序串跑，配合 `set -e` 任一失败即停。
- 手动 source 环境变量的官方命令见 [docs/zh/quick_install.md:334-342](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/quick_install.md#L334-L342)：`source /usr/local/Ascend/cann/set_env.sh`（非 root 换 `${HOME}` 路径）。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：跑通 asys 的三个子命令（health / info / collect），并记录它们各自的输出形态差异——这是理解 asys「输出目录 + tar 压缩」机制的第一手材料。
2. **操作步骤**（需要昇腾设备或 WebIDE；无设备则完成纸面部分）：
   ```bash
   # 0. 加载环境（按实际安装路径调整）
   source /usr/local/Ascend/cann/set_env.sh

   # 1. 跑仓库示例（等价于 bash examples/asys/run.sh）
   asys health

   # 2. 再各跑一遍 info 与 collect
   asys info
   asys collect

   # 3. 执行 msaicerr / msprof 示例，观察另外两个组件的输出
   bash examples/msaicerr/run.sh
   bash examples/msprof/run.sh
   ```
3. **需要观察的现象**（对照源码验证）：
   - `asys health` / `asys info`：直接向终端打印检查/信息结果。注意 [src/asys/asys.py:98-104](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L98-L104)——info/diagnose/health 这类「输出即答案」的子命令会主动关闭 info/warning 级日志，保证终端输出干净。
   - `asys collect`：在执行目录下生成 `asys_output_<UTC时间戳到毫秒>` 目录。命名来自 [src/asys/common/task_common.py:59-62](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/task_common.py#L59-L62)：`'asys_output_' + strftime('%Y%m%d%H%M%S%f')[:-3]`。
   - 若命令带了 tar 参数，`collect` 结束后目录会被压成同名 `.tar.gz` 并删除原目录——逻辑在 [src/asys/common/compress_output_dir.py:26-39](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/common/compress_output_dir.py#L26-L39)；触发条件见 [src/asys/asys.py:138-140](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L138-L140)（`tar` 参数为 `T`/`TRUE` 时才压缩）。
   - `msprof` 示例结束后 `./msprof_output/` 下出现性能数据文件。
4. **预期结果**：填出下面这张表（左侧已给出，右侧由你运行后补全）：

   | 子命令 | 输出去向 | 目录/文件形态 |
   | --- | --- | --- |
   | `asys health` | 终端标准输出 | 无输出目录（待本地验证具体项目） |
   | `asys info` | 终端标准输出 | 无输出目录（待本地验证） |
   | `asys collect` | 执行目录下 `asys_output_时间戳/` | 内含各采集项子目录（trace/log 等，待本地验证具体清单） |

5. 纸面替代方案（无设备时必做）：阅读 [src/asys/asys.py:73-143](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py#L73-L143) 的 `main()`，把「建目录 → 分发执行 → tar 压缩」三步在纸上标注到对应行号，并回答：为什么 health/info 没有留下 `asys_output_` 目录，而 collect 留下了？（提示：目录每次运行都会创建，是否被清理取决于内容与 tar 参数。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `examples/asys/run.sh` 里没有 source setenv.bash，而 msaicerr/msprof 的脚本里有？

**答案**：asys 安装后通过软链接直接暴露在 PATH 中（u1-l3 讲过的 `asys -> ./asys.py` 与 CMake install 规则），脚本假设用户已在当前 shell 完成环境加载；msaicerr 与 msprof 的脚本则用 `${ASCEND_INSTALL_PATH:-${ASCEND_HOME_PATH:-/usr/local/Ascend/cann}}` 定位 CANN_ROOT 并自行 source `$CANN_ROOT/bin/setenv.bash`，做到「开箱即跑」不依赖用户事先的操作（对比 examples/msaicerr/run.sh:19-22 与 examples/asys/run.sh:18-20）。

**练习 2**：`examples/msprof/run.sh` 的 `--sys-period=5` 和 README 说的「采集 5 秒」是什么关系？

**答案**：`--sys-period=5` 是系统级指标的采样周期参数，与 examples/README.md 描述的「采集 5 秒系统级 CPU/内存性能数据」场景对应（examples/msprof/run.sh:25）。注意脚本本身没有显式的采集时长参数，完整参数说明在 `docs/zh/profiling/msprof_cmd/` 下，u4-l5 会展开。

**练习 3**：三个示例脚本都有的 `set -e` 起什么作用？

**答案**：任何一条命令返回非零就立即终止脚本且以失败码退出，避免「第一步环境加载失败、后续步骤拿着错误环境继续跑」产生误导性输出；`deploy.sh` 串跑三个脚本时也靠它实现「一失败即停」（examples/deploy.sh:18）。

## 5. 综合实践

**任务：从裸机到「三组件全绿」的完整环境验收单。**

假设你拿到一台新申请的 Atlas A2 服务器，请产出一份验收清单（Markdown 文件即可，放在仓库外），包含：

1. **环境段**：执行 `npu-smi info`、`cat .../ascend_toolkit_install.info`、`cat .../ascend_ops_install.info`、`pytest --version` 的实际输出，并判断芯片型号与 ops 包是否匹配（依据 quick_install.md 的芯片表）。
2. **变量段**：`source /usr/local/Ascend/cann/set_env.sh` 前后各执行一次 `echo $ASCEND_HOME_PATH`，对比差异。
3. **验证段**：执行 `bash build.sh -u --component asys --ut`，粘贴末尾的 `Execute run_tests.sh successful.`（或失败原因分析）。
4. **功能段**：依次执行 `bash examples/deploy.sh` 中的三个子脚本，按 4.4.4 的表格记录 asys health/info/collect 的输出目录结构，以及 msaicerr `-e` 的检查项输出、`./msprof_output/` 的产物清单。
5. **溯源段**：对功能段观察到的每一个现象（如 `asys_output_` 目录名、tar.gz 的生成），给出对应的源码行号引用（本讲 4.4.3/4.4.4 已给出入口，鼓励用 Grep 追得更深）。

无设备时，第 1、4 段标注「待本地验证」，但第 5 段的溯源必须完成——这正是「源码阅读型实践」。

## 6. 本讲小结

- 运行 oam-tools 前需要「运行态」环境：驱动固件 + CANN toolkit 包 + 与芯片型号匹配的 ops 包；`npu-smi info` 的 Name 列是选包依据（910B→910b、910_93→A3、950→950）。
- `init_env.sh` 是文档的代码化：装系统依赖 → 探测/安装 CANN → source `setenv.bash` → 装 Python 依赖，支持 `--skip-cann`、`--skip-ops`、`--chip-type` 等参数。
- `source set_env.sh` 是使用工具的前提；仓库脚本惯用 `${ASCEND_INSTALL_PATH:-${ASCEND_HOME_PATH:-/usr/local/Ascend/cann}}` 三级兜底定位 CANN。
- `bash build.sh -u` 是官方的安装/环境验证手段，纯 Python 组件走「跳过 C++ 构建」的快车道，测试执行前还会再 source 一次 `setenv.bash`。
- `examples/` 下三个脚本分别是三个组件的最小功能示例：`asys health`、`msaicerr -e`、`msprof --host-sys-usage=cpu,mem --sys-period=5`，可用 `deploy.sh` 一键串跑。
- asys 的输出行为由 `main()` 统一控制：每次运行创建 `asys_output_<时间戳>` 目录，health/info 类命令输出到终端且关闭冗余日志，collect 的产物在满足条件时被 tar 成 `.tar.gz`。

## 7. 下一步学习建议

入门单元到此完成：你已经知道 oam-tools 是什么（u1-l1）、怎么构建（u1-l2）、代码在哪（u1-l3）、怎么跑起来（本讲）。接下来进入 u2 单元精读 asys 源码：

- 下一讲 u2-l1「asys 入口主流程」将逐段拆解本讲反复出现的 [src/asys/asys.py](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/asys.py) 的 `main()`，你已经有了它的运行体感。
- 建议提前浏览 `docs/zh/asys/README.md` 的命令示例，并对照本讲 4.4.4 观察到的输出目录，思考每个子命令的实现目录（`src/asys/health/`、`src/asys/info/`、`src/asys/collect/`）里会写什么。
- 如果本讲的 UT 验证没有跑通，优先解决环境问题再进入 u2——后续讲义的代码实践都默认环境可用。
