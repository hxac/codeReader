# 开发环境搭建与依赖管理

## 1. 本讲目标

本讲解决一个最实际的问题：**在动手编译、运行 asc-tools 之前，我到底要准备哪些东西？**

读完本讲，你应当能够：

- 说清 asc-tools 的「运行态依赖」和「编译态依赖」分别是什么，两者有何区别。
- 根据「本地有没有 NPU 设备」和「想用商用版还是 master 版」这两个条件，在云开发环境、Docker、DevContainer 三种方式中做出合适选择。
- 独立安装并配置 CANN 包（toolkit + ops），用 `npu-smi info` 和 `ascend_toolkit_install.info` 验证安装是否成功。
- 读懂仓库里两个「一键准备依赖」的脚本 `install_deps.sh` 和 `install_dep_tar.py` 各自负责什么，并理解它们与文档之间的细微差异。

本讲承接 [u1-l1 项目定位与工具全景](./u1-l1-project-overview.md)（你已经知道 asc-tools 是「一个 C++ 核心 + 四个 Python 工具」）和 [u1-l2 目录结构](./u1-l2-directory-structure.md)（你已经知道源码放在 `cpudebug/`、`utils/` 等目录）。本讲只谈「把环境搭好」，**不**涉及编译命令细节（那是 u1-l4 的内容）。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念：

- **CANN**：昇腾异构计算软件栈（Compute Architecture for Neural Networks）。asc-tools 不是凭空运行的，它的 C++ 核心 `cpudebug` 在编译和运行时都要链接 CANN 提供的基础库（如 `securec`、`mmpa`、`acl_rt`）。所以 **CANN 包是 asc-tools 的地基**。
- **toolkit 包 / ops 包**：CANN 包分两块。toolkit 是核心工具链（必装）；ops 是各型号 NPU 的预置算子信息（可选，但部分样例编译需要）。
- **运行态依赖 vs 编译态依赖**：
  - 运行态依赖 = 算子真正跑起来需要的底层（NPU 驱动、固件）。如果你只在 CPU 域做孪生调试，可以**不装驱动**。
  - 编译态依赖 = 把 asc-tools 源码编译成 `.so` / `.run` 包需要的工具（gcc、cmake、python、ccache 等）。
- **SoC / 型号**：NPU 的具体型号，如 `910b`。ops 包要按型号下载。
- **`source` 环境变量**：Linux 里用 `source xxx.sh` 在**当前 shell** 里执行一段脚本，从而把里面的 `export PATH=...` 等设置生效。CANN 装好后要 `source set_env.sh` 才能让编译脚本找到它。
- **容器化（Docker / DevContainer）**：把整个开发环境（系统库 + 工具链）打包进一个隔离的「容器」里运行，保证每个人、每台机器上的环境完全一致。官方明确推荐用容器化方式准备环境。

## 3. 本讲源码地图

本讲涉及的文件都是「环境与依赖」相关的入口，不在 `cpudebug/` 业务代码里：

| 文件 | 作用 |
| --- | --- |
| [docs/00_quick_start.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md) | 官方快速入门，是本讲最主要的依据：环境准备方式、CANN 安装、依赖清单、编译步骤都在这里 |
| [install_deps.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh) | 一键安装**编译态系统依赖**的 shell 脚本（python/gcc/cmake/ccache/googletest 等） |
| [install_dep_tar.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_dep_tar.py) | 一键下载**第三方开源软件**压缩包的 Python 脚本（makeself/boost/mockcpp 等） |
| [.devcontainer/devcontainer.json](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/devcontainer.json) | DevContainer 的配置：如何挂载宿主机 NPU 设备与驱动 |
| [.devcontainer/requirements.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/requirements.txt) | DevContainer 内的 Python 依赖清单 |
| [build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh) | 编译入口脚本，其中有一段「寻找 CANN 包路径」的逻辑，是理解环境变量的关键 |
| [cmake/dependencies.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/dependencies.cmake) | CMake 层声明「要从 CANN 包里找哪些库」 |
| [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake) | 声明 asc-tools 自身版本号，以及它对其它 CANN 组件的运行时依赖 |

## 4. 核心概念与源码讲解

### 4.1 三种环境准备方式对比

#### 4.1.1 概念说明

asc-tools 的环境准备方式不是唯一的。快速入门在最开头就用一张决策表告诉你该怎么选，决策依据是两个维度：

1. **本地有没有 NPU 设备**（有没有插昇腾卡）。
2. **使用目标**：是想体验/开发算子（用 CANN 商用版或社区版），还是想做生态贡献（跟 CANN master 主线）。

由此衍生出三种官方推荐方式：**云开发环境**、**基于 CANN 镜像的 Docker**、**DevContainer**。

为什么需要分这么多方式？因为 asc-tools 既要能在「真有 NPU 卡」的机器上跑（驱动 + 固件齐全），也要能让「没有 NPU 卡」的开发者只靠 CPU 就完成编译和 CPU 域孪生调试——这正是 [u1-l1](./u1-l1-project-overview.md) 讲过的「把 NPU 问题前移到 CPU 域」理念的体现。

#### 4.1.2 核心流程

快速入门给出的决策矩阵可以画成下面这张表（含义与原文一致）：

| 本地 NPU | 目标：商用/社区版 | 目标：生态开发（master） |
| :---: | :---: | :---: |
| 无 NPU | 云开发环境 | 云开发环境 + 手动装 CANN master |
| 有 NPU | 基于 CANN 镜像的 Docker | DevContainer + 手动装 CANN master |

三种方式的本质差异：

| 方式 | 是否需要本机 NPU | CANN 包 | 适合人群 |
| --- | :---: | --- | --- |
| 云开发环境（CANNLab） | 否（用云上的） | 已预装 | 无卡用户、快速体验 |
| CANN 镜像 Docker | 是 | 镜像内已预装 | 有卡用户、标准开发体验 |
| DevContainer | 是 | **需手动装** | 生态贡献者、要跑 UT/编译源码 |

一个关键区别：**云开发环境与 CANN 镜像 Docker 都已预装 CANN 包并配好环境变量；而 DevContainer 只把宿主机驱动以只读方式挂进来，CANN toolkit 和 ops 包必须在容器启动后手动安装。**

#### 4.1.3 源码精读

决策矩阵出自快速入门开头：[docs/00_quick_start.md:L5-L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L5-L16) 按两个维度给出四种组合，并在 TIP 中强调「推荐基于容器化技术」以及「仅体验编译安装 + CPU 仿真运行的用户不要求主机带 NPU」。

**云开发环境**的定位见 [docs/00_quick_start.md:L18-L40](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L18-L40)：它提供在线 ARM 架构环境，已装好驱动固件、软件包和依赖，仅适用于 Atlas A2 系列产品，通过 WebIDE 或 VSCode 接入。

**基于 CANN 镜像的 Docker** 见 [docs/00_quick_start.md:L42-L103](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L42-L103)。它的核心是一长串 `docker run` 参数，把宿主机的 NPU 设备和管理接口映射进容器：

```bash
docker run --name <cann_container> \
    --ipc=host --net=host --privileged \
    --device /dev/davinci0 \
    --device /dev/davinci_manager \
    --device /dev/devmm_svm \
    --device /dev/hisi_hdc \
    -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    ...
```

其中 `/dev/davinci0` 是第 0 张 NPU 卡，`davinci_manager`/`devmm_svm`/`hisi_hdc` 是设备管理、显存管理、主机设备通信三类控制接口。文档特别提醒：`davinci0` 的编号要按 `npu-smi info` 实际显示的设备号来改。

**DevContainer** 的说明见 [docs/00_quick_start.md:L105-L112](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L105-L112)。它基于 VS Code Dev Containers，用仓库内 `.devcontainer` 配置自动构建一致环境，内置 conda、Python 工具链。注意它和 Docker 方式的根本区别在那一行 NOTE：**DevContainer 仅挂载宿主机 NPU 驱动（只读），CANN toolkit 和 ops 包需在容器启动后手动安装。**

DevContainer 的「只读挂载驱动」体现在配置文件里：[.devcontainer/devcontainer.json:L27-L34](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/devcontainer.json#L27-L34) 中所有与驱动相关的挂载都带 `,readonly`：

```json
"mounts": [
    "source=ascendc-ccache,target=/root/.ccache,type=volume",
    "source=/usr/local/dcmi,target=/usr/local/dcmi,type=bind,readonly",
    "source=/usr/local/bin/npu-smi,target=/usr/local/bin/npu-smi,type=bind,readonly",
    "source=/usr/local/Ascend/driver,target=/usr/local/Ascend/driver,type=bind,readonly",
    "source=/etc/ascend_install.info,target=/etc/ascend_install.info,type=bind,readonly"
]
```

而把多张 NPU 卡（`davinci0`～`davinci7`）和三个控制设备显式列在 `runArgs` 里：[.devcontainer/devcontainer.json:L10-L25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/devcontainer.json#L10-L25)。同时还挂了一个命名卷 `ascendc-ccache` 到 `/root/.ccache`，用于跨容器重建保留 ccache 缓存、加快二次编译。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你在动手前先看清三种方式的边界。

1. **实践目标**：能够向别人解释「为什么 DevContainer 要手动装 CANN，而 Docker 镜像不用」。
2. **操作步骤**：
   - 打开 [docs/00_quick_start.md:L5-L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L5-L16)，确认决策矩阵。
   - 打开 [.devcontainer/devcontainer.json:L27-L34](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/devcontainer.json#L27-L34)，找出挂载列表里有没有 `toolkit` 或 `cann` 目录的挂载。
3. **需要观察的现象**：挂载列表里只有 `driver`、`dcmi`、`npu-smi`、`ascend_install.info`，**没有** CANN toolkit 安装目录。
4. **预期结果**：因为 toolkit 没被挂进容器，所以容器里没有 `set_env.sh`，自然需要在容器内手动安装 CANN 包。这就是文档那句 NOTE 的技术原因。
5. 结论性结果：待本地验证（建议你在自己的 DevContainer 里 `ls /usr/local/Ascend/` 确认只看到 `driver` 而看不到 `cann`）。

#### 4.1.5 小练习与答案

**练习 1**：一个没有 NPU 卡、只想体验 CPU 域孪生调试的用户，应该选哪种方式？需要装驱动吗？

> **答案**：选**云开发环境**（或任何能装 CANN 包的无卡环境）。文档 TIP 明确：仅体验「编译安装 + 仿真环境运行算子」的用户不要求主机带 NPU，可跳过驱动和固件安装，直接装 CANN 包。

**练习 2**：`.devcontainer/devcontainer.json` 里为什么要单独挂一个 `ascendc-ccache` 命名卷到 `/root/.ccache`？

> **答案**：DevContainer 每次重建都是一个新容器，本地文件系统会被重置。把 ccache 缓存放在 Docker 命名卷（volume）里，可以让编译缓存跨容器重建存活，避免每次都全量重编，加快二次编译速度。

### 4.2 CANN 包安装与环境变量配置

#### 4.2.1 概念说明

无论你选哪种环境准备方式，最终都要落到同一件事：**机器上得有一个可用、且环境变量配好的 CANN 包**。云环境和 Docker 镜像替你做完了这步；手动安装（或 DevContainer）则需要你自己来。

CANN 包分两部分：

- **toolkit 包**（必选）：核心工具链，提供编译器、运行时库、`set_env.sh` 等。
- **ops 包**（可选）：按 SoC 型号提供预置算子信息。部分样例编译依赖它，完整体验建议安装。

二者**必须装到同一个 `install_path`**，因为运行时要按统一目录去找。

#### 4.2.2 核心流程

手动安装的标准流程是三步：

```text
1. 下载 → 2. 安装(.run 文件) → 3. source set_env.sh 让环境变量生效
```

安装命令的核心是 `.run` 自解压脚本：

```bash
chmod +x Ascend-cann-toolkit_${cann_version}_linux-$(uname -m).run
./Ascend-cann-toolkit_${cann_version}_linux-$(uname -m).run --install --install-path=${install_path}
```

默认安装路径：root 用户 `/usr/local/Ascend`，非 root 用户 `$HOME/Ascend`。

装完后让环境变量生效：

```bash
source /usr/local/Ascend/cann/set_env.sh        # 默认路径，root
# source ${install_path}/cann/set_env.sh        # 自定义路径
```

最后做两件验证：用 `npu-smi info` 看驱动是否正常；用 `cat .../ascend_toolkit_install.info` 看 CANN 是否装好。

#### 4.2.3 源码精读

**下载与安装**出自 [docs/00_quick_start.md:L114-L151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L114-L151)。其中商用/社区版从 CANN 官网下载，master 版从 master OBS 镜像下载「日期最新」的包；ops 包按 `${soc_name}`（如 `910b`）选择。

**环境验证**出自 [docs/00_quick_start.md:L153-L174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L153-L174)：

```bash
npu-smi info                                                                # 检查 NPU 设备/驱动
cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_toolkit_install.info   # 检查 toolkit
cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_ops_install.info       # 检查 ops
```

文档特别注明：CANNLab 场景下要把 `/usr/local` 替换为 `/home/developer`。

**环境变量配置**出自 [docs/00_quick_start.md:L176-L188](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L176-L188)，并强调云环境和 Docker 镜像已自动配好，可跳过。

**为什么 `source set_env.sh` 这么关键？** 看编译脚本就明白了。`build.sh` 里有一段「按优先级寻找 CANN 包路径」的逻辑：[build.sh:L265-L290](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L265-L290)

```bash
if [ -n "${cann_path}" ];then
  ASCEND_CANN_PACKAGE_PATH=${cann_path}            # 优先级1: 命令行 -p|--cann_path
elif [ -n "${ASCEND_HOME_PATH}" ];then
  ASCEND_CANN_PACKAGE_PATH=${ASCEND_HOME_PATH}     # 优先级2: 环境变量 ASCEND_HOME_PATH
elif [ -n "${ASCEND_OPP_PATH}" ];then
  ASCEND_CANN_PACKAGE_PATH=$(dirname ${ASCEND_OPP_PATH})  # 优先级3: ASCEND_OPP_PATH
elif [ -d "${DEFAULT_TOOLKIT_INSTALL_DIR}" ];then  # 优先级4: 默认 toolkit 目录
  ASCEND_CANN_PACKAGE_PATH=${DEFAULT_TOOLKIT_INSTALL_DIR}
elif [ -d "${DEFAULT_INSTALL_DIR}" ];then          # 优先级5: 默认安装目录
  ASCEND_CANN_PACKAGE_PATH=${DEFAULT_INSTALL_DIR}
else
  log "Error: Please set the cann package installation directory through parameter -p|--cann_path."
  exit 1
fi
source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash || echo "0"
```

`ASCEND_HOME_PATH`、`ASCEND_OPP_PATH` 这些环境变量正是 `set_env.sh` 负责导出的。也就是说：**如果你忘了 `source set_env.sh`，又没在默认路径装 CANN，又没传 `-p`，编译就会直接报错退出。** 这段逻辑揭示了 `set_env.sh` 在整个工具链里的「总线」地位。

CMake 层面，`dependencies.cmake` 把这个路径设为查找前缀，并声明要从 CANN 包里找哪些库：[cmake/dependencies.cmake:L11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/dependencies.cmake#L11) 设 `CMAKE_PREFIX_PATH` 为 `${ASCEND_CANN_PACKAGE_PATH}/`，随后 [cmake/dependencies.cmake:L24-L29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/dependencies.cmake#L24-L29) 依次 `find_cann_package` 找 `unified_dlog`、`securec`、`mmpa`、`acl_rt`、`pvmodel`。这些都是 CANN 包提供的库——若 CANN 没装好，这一步必然失败。

asc-tools 自身还声明了对其它 CANN 组件的运行时依赖：[version.cmake:L11-L19](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11-L19)

```cmake
set(ASC_TOOLS_VERSION "9.1.0")
set_cann_package(asc-tools VERSION ${ASC_TOOLS_VERSION})
set_cann_build_dependencies(runtime "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(runtime "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(ge-executor "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(metadef "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(asc-devkit "CUR_MAJOR_MINOR_VER")
```

这解释了 [u1-l1](./u1-l1-project-overview.md) 提到的一个硬约束：**asc-tools 不能独立升级，必须搭配版本匹配的 CANN 包**。`CUR_MAJOR_MINOR_VER` 表示「与当前大版本.小版本对齐」，所以 master 分支要用最新的 CANN master 包，特定 tag 要用对应官网包。

#### 4.2.4 代码实践

这是本讲的核心实践任务。

1. **实践目标**：在你准备好的环境里完成 CANN 包安装与变量配置，并用两条命令验证成功。
2. **操作步骤**：
   - （若用 DevContainer / 手动安装）按 [docs/00_quick_start.md:L128-L151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L128-L151) 安装 toolkit（必选）与 ops（可选，同 SoC、同路径）。
   - 执行 `source /usr/local/Ascend/cann/set_env.sh`（按你的实际安装路径调整）。
   - 执行验证命令：
     ```bash
     npu-smi info
     cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_toolkit_install.info
     ```
3. **需要观察的现象**：
   - `npu-smi info` 能输出 NPU 卡的型号、健康状态等信息（若你只做 CPU 仿真、没装驱动，这步可能无输出，属正常）。
   - `ascend_toolkit_install.info` 能打印出 toolkit 的版本字段。
4. **预期结果**：两条命令至少第二条应稳定返回版本信息；`echo $ASCEND_HOME_PATH` 应能看到 CANN 路径，证明 `set_env.sh` 生效。
5. 本地验证：待本地验证（不同环境路径不同，请以你机器上的实际输出为准）。

#### 4.2.5 小练习与答案

**练习 1**：toolkit 包和 ops 包的 `--install-path` 能不能不一样？

> **答案**：不能。文档 [docs/00_quick_start.md:L147-L151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L147-L151) 明确「toolkit 包和 ops 包需相同」。因为运行时按统一目录查找算子信息，分开装会导致 ops 找不到。

**练习 2**：你忘了 `source set_env.sh`，也没传 `-p`，CANN 又装在默认路径 `/usr/local/Ascend/ascend-toolkit/latest`。编译会失败吗？

> **答案**：不会失败。看 [build.sh:L280-L281](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L280-L281)，当前面三个条件都不满足时，会回退到检查 `DEFAULT_TOOLKIT_INSTALL_DIR`（root 是 `/usr/local/Ascend/ascend-toolkit/latest`），存在就用它。只有五个条件全不满足时才报错退出。

### 4.3 编译态与运行态依赖清单

#### 4.3.1 概念说明

CANN 包是「外部地基」，除此之外，编译 asc-tools 源码本身还需要一组**系统工具链**（gcc、cmake、python…）和一组**第三方开源库**（boost、googletest、mockcpp…）。仓库提供了两个脚本帮你一键准备：

| 脚本 | 语言 | 负责装什么 |
| --- | --- | --- |
| `install_deps.sh` | shell | **系统工具链**：python、gcc/g++、cmake、ccache、pigz、googletest、lcov、pytest |
| `install_dep_tar.py` | python | **第三方开源软件压缩包**：makeself、boost、googletest、mockcpp、cann-cmake |

二者职责完全不重叠：前者用系统包管理器（apt/dnf/yum）装可执行工具；后者只负责把编译期要用的源码包下载到指定目录，下载后由 CMake 在编译时拉起构建。

#### 4.3.2 核心流程

**`install_deps.sh` 的执行流程**是一个典型的「先检测、后安装」模式：

```text
main()
  ├─ detect_os()       判断 debian(apt) 还是 rhel(dnf/yum)
  ├─ install_python()  检测版本 ≥ 3.7.0，不够才装
  ├─ install_py_pkg()  装 pytest ≥ 5.4.2、pytest-cov ≥ 2.8.1
  ├─ install_gcc()     检测版本 ≥ 7.3.0
  ├─ install_cmake()   检测版本 ≥ 3.16.0
  ├─ install_ccache()  检测版本 ≥ 4.8.2
  ├─ install_pigz()    询问后安装
  ├─ install_googletest() 检测 ≥ 1.11.0
  └─ install_lcov()    UT 覆盖率工具
```

每个 `install_xxx` 都遵循同一个套路：先用 `version_ge` 比较当前版本与要求版本，达标就跳过，不达标才调包管理器安装。版本比较函数 [install_deps.sh:L28-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L28-L43) 按 `.` 分段逐段比较。

`main` 函数把所有安装步骤串起来：[install_deps.sh:L423-L442](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L423-L442)

```bash
main() {
    ...
    detect_os
    install_python
    install_py_pkg pytest "5.4.2"
    install_py_pkg pytest-cov "2.8.1"
    install_gcc
    install_cmake
    install_ccache
    install_pigz
    install_googletest
    install_lcov
    ...
}
```

**`install_dep_tar.py` 的执行流程**更简单：把一组 URL 写死在列表里，逐个用 `urllib.request.urlretrieve` 下载到目标目录。URL 列表见 [install_dep_tar.py:L49-L56](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_dep_tar.py#L49-L56)：

```python
tar_urls = [
    ".../makeself-release-2.5.0-patch1.tar.gz",
    ".../boost_1_87_0.tar.gz",
    ".../googletest-1.14.0.tar.gz",
    ".../mockcpp-2.7.tar.gz",
    ".../mockcpp-2.7_py3-h3.patch",
    ".../cmake-master-003.tar.gz",
]
```

下载函数 [install_dep_tar.py:L18-L35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_dep_tar.py#L18-L35) 会自动建目录、按 URL 最后一段作为文件名落盘，单个下载失败只打印告警、不中断整体。

用法：

```bash
python3 install_dep_tar.py --dest_dir=${your_3rd_party_path}
```

注意 `--dest_dir`（也可写 `-d`）默认是当前目录，见 [install_dep_tar.py:L40-L47](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_dep_tar.py#L40-L47)。

#### 4.3.3 源码精读

**完整的依赖清单（权威来源是文档）**：[docs/00_quick_start.md:L235-L348](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L235-L348)。整理成表：

| 依赖 | 版本要求 | 是否必选 | 说明 |
| --- | --- | :---: | --- |
| python | ≥ 3.7.0（建议 ≥ 3.9.x） | 是 | 3.7/3.8 已 EOL，即将停止支持 |
| gcc / g++ | 7.3.x ～ 14.x | 是 | **gcc 与 g++ 版本必须一致** |
| cmake | ≥ 3.16.0 | 是 | |
| ccache | ≥ 4.6.1 | 是 | 文档建议 release-v4.6.1 |
| setuptools | ≥ 45.2.0 | 是 | `pip3 install setuptools` |
| lcov | ≥ 1.13 | 否 | 仅 UT 覆盖率需要 |
| pytest | ≥ 8.3.2 | 否 | 仅 UT 需要 |
| coverage | ≥ 4.5.4 | 否 | 仅 UT 需要 |
| googletest | 建议 1.11.0 | 否 | 仅 C++ UT 需要 |

**值得注意的细节：文档与脚本的版本号并不完全一致。** 文档说 ccache ≥ 4.6.1，但脚本 `install_ccache` 里写的是：[install_deps.sh:L260-L273](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L260-L273)

```bash
install_ccache() {
    # ccache version >= 4.8.2
    ...
    local req_ver="4.8.2"
```

脚本要求 4.8.2，文档要求 4.6.1。这类「文档与脚本漂移」在真实项目里很常见，遇到版本问题时**以脚本/实际报错为准**，这是读源码带来的额外收获。

**OS 检测**是脚本分支的依据：[install_deps.sh:L45-L66](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L45-L66) 通过 `/etc/debian_version` 与 `/etc/redhat-release` 判断是 debian 系（apt）还是 rhel 系（优先 dnf，回退 yum），都不匹配就退出。

**root 与非 root 的处理**：[install_deps.sh:L68-L77](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L68-L77)，root 用户不加 `sudo`，非 root 自动加。

**gcc 安装**对老系统有特殊处理：[install_deps.sh:L154-L205](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L154-L205)，debian 系装 gcc-9 并用 `update-alternatives` 设为默认；RHEL 7 走 SCL 的 devtoolset-9。

**第三方开源软件的完整清单**在文档里也有一张表，与脚本的 URL 列表一一对应：[docs/00_quick_start.md:L219-L234](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L219-L234)，包含 makeself 2.5.0、boost 1.87.0、googletest 1.14.0、mockcpp 2.7（含 patch）、cann-cmake master-003。这套包是**离线编译**场景必需的——文档在 [docs/00_quick_start.md:L207-L234](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L207-L234) 说明：编译环境若无法联网，需要手动下载这些包 + 闭源 cpudebug 包，上传到 `{your_3rd_party_path}`，再用 `bash build.sh --pkg --cann_3rd_lib_path={your_3rd_party_path}` 编译。

**Python 运行依赖**（DevContainer 场景）见 [.devcontainer/requirements.txt:L1-L26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/requirements.txt#L1-L26)，除 `setuptools`/`pytest`/`coverage` 外，还包含 `numpy`、`scipy`、`pybind11`、`onnx`、`protobuf`、`pre-commit`、`oat-py` 以及按平台/Python 版本分发的 `torch` / `torch_npu` / `tensorflow`。这反映了 asc-tools 的 Python 工具链（尤其是 UT 与算子精度比对）依赖较重，DevContainer 把它们一次性装好。

#### 4.3.4 代码实践

1. **实践目标**：不用真跑安装，通过读脚本，判断在你当前机器上 `install_deps.sh` 会跳过哪些步骤、实际执行哪些。
2. **操作步骤**：
   - 在你的机器上先查版本：
     ```bash
     python3 --version; gcc --version | head -1; cmake --version | head -1; ccache --version | head -1
     ```
   - 对照 [install_deps.sh:L423-L442](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L423-L442) 的调用顺序，逐个判断：你的版本是否满足 `install_python`(≥3.7.0)、`install_gcc`(≥7.3.0)、`install_cmake`(≥3.16.0)、`install_ccache`(≥4.8.2)。
   - 用脚本里的 `version_ge` 思路手算一次：假设你的 ccache 是 4.6.1，比较 `4.6.1` 与 `4.8.2`，按 [install_deps.sh:L28-L43](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L28-L43) 的逐段比较，第一段 4==4，第二段 6<8，返回「不达标」，于是脚本会触发安装。
3. **需要观察的现象**：你预测的「跳过/安装」结论，与脚本实际行为是否一致。
4. **预期结果**：版本达标项打印 `meets requirements` 直接 return；不达标项进入安装分支。
5. 真实执行：待本地验证（可在一个干净容器里实际跑一次 `bash install_deps.sh` 对照）。

#### 4.3.5 小练习与答案

**练习 1**：`install_dep_tar.py` 下载的 `googletest-1.14.0` 和 `install_deps.sh` 里装的 `googletest` 是同一个东西吗？为什么要分开？

> **答案**：不是一回事、目的不同。`install_deps.sh` 的 `install_googletest` 把 gtest **装到系统**（`/usr/lib` 下 `.a`），供 C++ UT 直接链接，要求 ≥ 1.11.0；`install_dep_tar.py` 下载的是 `googletest-1.14.0` **源码包**到第三方目录，供 CMake 在编译期按需构建（如 mockcpp 等依赖）。一个是系统级安装、一个是源码包备用，所以分开。

**练习 2**：仔细看 `install_deps.sh` 的 `install_ccache` 函数（[L260-L295](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_deps.sh#L260-L295)），第 278 行有一条 `apt list caache -a`，你发现了什么？

> **答案**：包名拼写成了 `caache`（应为 `ccache`）。这是一个真实存在的笔误——`apt list caache -a` 查不到有效版本，`apt_ver` 会为空，进而影响后面的版本比较判断。这正是「读源码比读文档更能发现真相」的典型例子，也提醒我们：脚本里看似正常的分支可能因为一个 typo 而失效。

## 5. 综合实践

把本讲三个模块串起来，完成一次「为 asc-tools 准备一套可编译环境」的完整推演。假设你是**一名有 NPU 卡、想跟 CANN master 主线做生态贡献的开发者**：

1. **选方式**：根据 [docs/00_quick_start.md:L5-L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L5-L16) 的决策矩阵，你应选 **DevContainer + 手动装 CANN master**。请说明为什么不是 Docker 镜像方式（提示：镜像里是商用/社区版，且 DevContainer 更适合跑 UT 和贡献代码）。
2. **配 CANN**：进入容器后，按 [docs/00_quick_start.md:L114-L151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L114-L151) 下载并安装 master 版 toolkit，再 `source set_env.sh`，用 [docs/00_quick_start.md:L153-L174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L153-L174) 的命令验证。
3. **备依赖**：因为要走编译，对照 [docs/00_quick_start.md:L235-L348](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L235-L348) 检查 gcc/cmake/python/ccache；若离线，用 `python3 install_dep_tar.py --dest_dir=${your_3rd_party_path}`（[install_dep_tar.py:L38-L58](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/install_dep_tar.py#L38-L58)）拉齐第三方包。
4. **自检**：在执行编译（u1-l4 的 `bash build.sh --pkg`）之前，对照本讲小结确认：CANN 路径可被 `build.sh` 找到（五种优先级，见 [build.sh:L274-L287](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L274-L287)），系统工具链达标，第三方包就位。

产出一张「我的环境自检表」，列出每一项的当前版本、要求版本、是否达标。这张表就是你下一讲顺利编译的通行证。具体能否编译通过：待本地验证。

## 6. 本讲小结

- asc-tools 的依赖分两层：**运行态**（NPU 驱动/固件，仅 CPU 仿真可省）和**编译态**（gcc/cmake/python/ccache + 第三方源码包）。CANN 包是两者共同的地基。
- 三种环境准备方式按「有无 NPU」+「商用/master」选择：**云开发环境**（无卡）、**CANN 镜像 Docker**（有卡、预装 CANN）、**DevContainer**（有卡、只挂驱动、CANN 需手动装、适合贡献）。
- CANN 包分 **toolkit（必选）+ ops（可选，按 SoC）**，必须装同路径；装完务必 `source set_env.sh`，它导出的 `ASCEND_HOME_PATH` 等变量是 `build.sh` 定位 CANN 的关键。
- `install_deps.sh` 用「先检测版本后安装」的模式装系统工具链；`install_dep_tar.py` 只负责下载第三方源码包，两者职责不重叠。
- 验证安装用 `npu-smi info` + `cat .../ascend_toolkit_install.info`；asc-tools 不能独立升级，须搭配版本匹配的 CANN 包（见 `version.cmake`）。
- 读源码能发现文档未提的细节：脚本里 ccache 要求 4.8.2（文档写 4.6.1），且 `install_ccache` 中存在 `caache` 拼写问题——遇到版本疑虑以脚本/实际报错为准。

## 7. 下一步学习建议

环境搭好之后，下一步自然是**真正编译一次并跑通第一个样例**，这正是下一讲 [u1-l4 一键编译与运行第一个样例](./u1-l4-build-and-first-sample.md) 的内容：它会讲解 `build.sh --pkg` 的完整产物路径、run 包安装，以及用 `cmake -DCMAKE_ASC_RUN_MODE=cpu` 跑通 add 样例。

如果你想提前拓展阅读，建议看：
- [build.sh:L265-L290](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L265-L290) 的 CANN 路径解析，理解编译脚本如何衔接本讲配置的环境变量。
- [.devcontainer/Dockerfile](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/.devcontainer/Dockerfile)，看 DevContainer 如何从零构建出本讲描述的「内置 conda/Python 工具链」环境。
