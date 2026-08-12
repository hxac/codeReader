# 开发环境准备与一键编译构建

## 1. 本讲目标

本讲是 Ascend C 学习手册入门篇的第三讲。读完本讲后，你应该能够：

- 根据「是否有 NPU 设备」和「使用目标」从云开发环境、CANN 官方 Docker 镜像、DevContainer、手动安装四种方式中选出适合自己的环境准备路径。
- 完成 CANN 包的下载安装、环境变量（`set_env.sh`）配置与依赖检查，并能用 `npu-smi` 等命令验证环境。
- 读懂 `build.sh` 的命令行参数体系，特别是 `--pkg` 一键打包的执行路径。
- 理解顶层 `CMakeLists.txt` 与 `version.cmake` 构成的 CMake 工程骨架，以及编译产物 `cann-asc-devkit_*.run` 是如何生成的。

本讲承接 [u1-l1 项目定位与多层级 API 架构](u1-l1-project-overview-architecture.md) 与 [u1-l2 仓库目录结构与源码组织](u1-l2-repo-structure-source-layout.md)：你已经知道 asc-devkit 是承载 Ascend C API 的开源仓、`include`/`impl` 是镜像结构。本讲回答下一个最现实的问题——**「这套源码怎么编译、怎么装到我的 CANN 环境里」**。

## 2. 前置知识

- **CANN**：昇腾异构计算架构（Compute Architecture for Neural Networks），是华为为昇腾 NPU 提供的一整套软件栈，包含编译器、运行时、算子库等。asc-devkit 本身只是 CANN 中「Ascend C 语言部分」的开源镜像，编译它必须依赖一个已安装的 CANN 环境。
- **NPU 与驱动固件**：NPU 是昇腾神经网络处理单元（如 Atlas A2 系列）。驱动（driver）和固件（firmware）是让操作系统识别并调度 NPU 硬件的底层软件，运行算子时是必需的；但**仅编译源码、跑 CPU 仿真**则不强制要求驱动。
- **run 包**：CANN 生态惯用的自解压安装包（基于 makeself 工具制作），后缀是 `.run`。执行 `xxx.run --full` 即可把内容释放安装到指定目录。
- **CMake**：跨平台的 C/C++ 工程构建工具，通过 `CMakeLists.txt` 描述工程结构，再生成 Makefile 或 Ninja 文件。本仓要求 cmake ≥ 3.16.0。
- **环境变量**：`set_env.sh` 是 CANN 安装后生成的一个脚本，`source` 它会把编译器、运行时库的路径注入到 `LD_LIBRARY_PATH`、`PATH` 等变量中，让终端「认识」CANN。

如果你对这些概念还比较陌生，不用担心，本讲会结合真实脚本一步步说明。

## 3. 本讲源码地图

本讲围绕「环境准备 → 编译 → 打包 → 安装」这条主线，涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [docs/zh/quick_start.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md) | 官方快速开始文档，环境准备与编译安装的权威说明 |
| [build.sh](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh) | 仓库根目录的一键编译入口脚本，封装了 cmake/make/cpack 流程 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt) | 顶层 CMake 工程文件，组织 impl/tools/cmake/tests 等子目录 |
| [version.cmake](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/version.cmake) | 声明 asc-devkit 版本号与编译期/运行期对其他 CANN 组件的依赖 |
| [cmake/config.cmake](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/config.cmake) | CANN 包路径校验、安装前缀、构建类型等基础配置 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/package.cmake) | 打包配置：引入 makeself、收集安装脚本、调用 cpack 生成 run 包 |
| [.devcontainer/devcontainer.json](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/.devcontainer/devcontainer.json) | DevContainer 配置：如何把宿主机 NPU 设备映射进容器 |

---

## 4. 核心概念与源码讲解

### 4.1 环境准备与 CANN 安装

#### 4.1.1 概念说明

在编译 asc-devkit 源码之前，必须先有一套可用的 CANN 环境。原因正如前置讲义所说：asc-devkit 仓库只包含 Ascend C 的 API 源码（`include`/`impl`），而编译这些源码要用到的毕昇编译器（bisheng-compiler）、运行时（runtime）、元定义（metadef）等组件，都来自完整的 CANN 包。本节解决的问题是：**「我应该用哪种方式拿到这套 CANN 环境？」**

官方文档 [`quick_start.md`](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md) 用一张二维决策表来引导选择，两个维度是：

1. **本地是否有 NPU 设备**（决定你能不能真正在卡上跑算子）。
2. **使用目标**（社区体验/算子开发，还是生态开发者贡献 master 代码）。

#### 4.1.2 核心流程

下表是对官方四种环境准备方式的整理（依据 [docs/zh/quick_start.md:7-11](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L7-L11) 的决策表归纳）：

| 方式 | 是否需要 NPU | 适合场景 | 特点 |
| :--- | :---: | :--- | :--- |
| ① 云开发环境 CANNLab | 否 | 社区体验 / 算子开发 | 网页或 VSCode 远程接入，环境预装，零配置 |
| ② CANN 官方 Docker 镜像 | 是 | 社区体验 / 算子开发 | 镜像内已预装 CANN，挂载宿主机 NPU 设备即可 |
| ③ DevContainer | 是 | 生态开发者贡献 | 仓库内 `.devcontainer` 自动构建，内置完整工具链 |
| ④ 手动下载安装 CANN 包 | 均可 | 体验 master / 离线场景 | 最灵活，但需自行安装与配置 |

无论走哪条路径，最终都要落到同一个目标状态：**有一个可被 `source` 的 `set_env.sh`，且 `ASCEND_CANN_PACKAGE_PATH` 指向有效的 CANN 安装目录**。官方推荐优先使用容器化方式（方式 ②③），以保障开发体验一致性（见 [docs/zh/quick_start.md:12-16](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L12-L16) 的选择建议）。

整体流程可以用伪代码概括：

```text
选择环境方式（云/Docker/DevContainer/手动）
    ↓
（如需）下载并安装 CANN toolkit 包（必选）+ ops 包（可选）
    ↓
source ${ASCEND_INSTALL_PATH}/cann/set_env.sh
    ↓
npu-smi info  /  cat ascend_toolkit_install.info  （验证）
    ↓
确认 python>=3.9, gcc/g++>=7.3, cmake>=3.16, pkg-config>=0.29
```

#### 4.1.3 源码精读

**（1）CANN 包的下载安装命令**

CANN 包分为 toolkit 包（必选，提供编译器与运行时）和 ops 包（可选，部分样例依赖）。安装命令在 [docs/zh/quick_start.md:130-142](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L130-L142)：

```bash
# toolkit（必选）
chmod +x Ascend-cann-toolkit_${cann_version}_linux-$(uname -m).run
./Ascend-cann-toolkit_${cann_version}_linux-$(uname -m).run --install --install-path=${install_path}
# ops（可选）
chmod +x Ascend-cann-${soc_name}-ops_${cann_version}_linux-$(uname -m).run
./Ascend-cann-${soc_name}-ops_${cann_version}_linux-$(uname -m).run --install --install-path=${install_path}
```

这里 `${soc_name}` 是 NPU 型号（如 `910b`），`${install_path}` 默认 root 用户为 `/usr/local/Ascend`、非 root 用户为 `$HOME/Ascend`。toolkit 包和 ops 包必须装到同一个 `${install_path}` 下。

**（2）环境变量配置（set_env.sh）**

装好 CANN 包后，必须 `source` 它提供的 `set_env.sh` 让环境变量生效，见 [docs/zh/quick_start.md:181-188](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L181-L188)：

```bash
# 默认路径（root 用户）
source /usr/local/Ascend/cann/set_env.sh
# 指定路径
# source ${install_path}/cann/set_env.sh
```

云开发环境和官方 Docker 镜像会自动配置这一步，可以跳过。

**（3）环境验证**

[docs/zh/quick_start.md:160-174](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L160-L174) 给出两条验证命令：`npu-smi info` 检查驱动是否正常，`cat .../ascend_toolkit_install.info` 检查 CANN 包是否就位。

**（4）DevContainer 如何映射 NPU 设备**

如果你选择方式 ③ DevContainer，仓库内的 `.devcontainer/devcontainer.json` 已经把宿主机的 NPU 设备节点映射进容器，见 [.devcontainer/devcontainer.json:10-25](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/.devcontainer/devcontainer.json#L10-L25)：

```json
"runArgs": [
    "--ipc=host",
    "--net=host",
    "--privileged",
    "--device=/dev/davinci0",
    ...
    "--device=/dev/davinci_manager",
    "--device=/dev/devmm_svm",
    "--device=/dev/hisi_hdc"
]
```

关键点：`--device=/dev/davinciN` 把宿主机第 N 张 NPU 卡映射进容器，`--privileged` 赋予完整设备访问权限；而宿主机驱动 `/usr/local/Ascend/driver` 以**只读**方式挂载（见 [.devcontainer/devcontainer.json:27-34](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/.devcontainer/devcontainer.json#L27-L34)），所以 **CANN toolkit/ops 包仍需在容器启动后手动安装**（见 [docs/zh/quick_start.md:111-112](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L111-L112) 的说明）。

> ⚠️ **配套版本约束**：本仓依赖其他 CANN 开源仓，**暂不支持独立升级**。master 分支必须搭配最新的 CANN master 包，特定 Tag 须搭配对应版本的商用/社区包（见 [docs/zh/quick_start.md:233-237](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L233-L237)）。下一节在 `version.cmake` 中会看到这种依赖的精确声明。

#### 4.1.4 代码实践

> 这是一个**源码阅读 + 命令实操型**实践。如果你的机器暂无 CANN 环境，可只完成第 1、2 步的阅读部分。

1. **实践目标**：把本地的 CANN 环境变量配置好，并验证 CANN 包与驱动状态。

2. **操作步骤**：
   - 打开 [docs/zh/quick_start.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md)，对照决策表确认自己属于哪个场景。
   - 在终端执行 `source ${你的安装路径}/cann/set_env.sh`（云环境/Docker 用户可跳过）。
   - 执行 `npu-smi info` 查看 NPU 状态。
   - 执行 `echo $ASCEND_CANN_PACKAGE_PATH`（若无输出，可改查 `echo $ASCEND_HOME_PATH`）。

3. **需要观察的现象**：`npu-smi info` 应输出一张包含设备型号、健康状态的表格；`set_env.sh` 执行后不应有任何报错。

4. **预期结果**：环境变量被正确注入，后续运行 `bash build.sh` 时不会出现「找不到 CANN 包」的错误。

5. **若无法运行**：标注「待本地验证」。仅阅读源码也能完成本讲后续内容。

#### 4.1.5 小练习与答案

**练习 1**：某开发者的笔记本没有 NPU，只是想快速体验编译 asc-devkit 源码并跑 CPU 仿真，应该选哪种环境方式？是否需要安装驱动？

> **参考答案**：推荐「云开发环境 CANNLab」或「手动安装 CANN 包」。仅体验编译 + 仿真运行算子时不要求主机带 NPU，可跳过安装驱动固件，直接装 CANN 包即可（依据 [docs/zh/quick_start.md:12-16](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L12-L16)）。

**练习 2**：`set_env.sh` 必须在每次打开新终端时都 `source` 一次吗？如何让它一劳永逸？

> **参考答案**：`source` 的作用只对当前终端进程生效，新开终端需要重新执行。可以把 `source /usr/local/Ascend/cann/set_env.sh` 这一行追加到 `~/.bashrc`（或 `~/.zshrc`），让每次登录自动执行。

---

### 4.2 build.sh 编译参数

#### 4.2.1 概念说明

CANN 工程通常用 CMake 描述、用 make/ninja 构建、用 cpack 打包，三套工具的参数互不相同，对初学者负担很重。`build.sh` 就是 asc-devkit 提供的一层**封装脚本**：它把常用的 cmake 选项、编译线程数、打包类型、测试目标等翻译成一组易记的命令行参数（如 `--pkg`、`--adv_test`、`-j 8`），内部再拼出完整的 cmake 命令。本节目标是让你看懂这个脚本的参数体系，尤其是 `--pkg` 这条最常用的路径。

#### 4.2.2 核心流程

`build.sh` 的整体执行流程如下（对应脚本里的 `main()` 函数）：

```text
bash build.sh <参数>
    ↓
check_param_with_help   解析并校验参数（非法参数直接退出）
    ↓
set_options             把 --pkg/-j/-t 等写入变量 PKG/THREAD_NUM/TEST...
    ↓
set_env                 定位 CANN 安装目录并 source set_env.sh
    ↓
拼接 CUSTOM_OPTION（-DASCEND_CANN_PACKAGE_PATH=... 等 cmake 定义）
    ↓
按优先级分支：
   TEST      → build_test()      构建并运行全部 UT
   TEST_PART → build_test_part() 按模块批跑 UT
   PKG       → build_package()   打 run 包
   (无上述)   → cmake_config + build all  默认编译
```

build.sh 顶层用 `set -e` 保证任何命令失败立即退出（[build.sh:12](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L12)），脚本开头集中声明了它支持的全部短选项与长选项（[build.sh:14-17](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L14-L17)）。

#### 4.2.3 源码精读

**（1）关键目录与默认变量**

脚本一开始就定义了 `BUILD_DIR`（中间产物）和 `OUTPUT_DIR`（最终产物）这两个最重要的目录，见 [build.sh:19-27](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L19-L27)：

```bash
CURRENT_DIR=$(dirname $(readlink -f ${BASH_SOURCE[0]}))
BUILD_DIR=${CURRENT_DIR}/build
OUTPUT_DIR=${CURRENT_DIR}/build_out
...
THREAD_NUM=32
BUILD_TYPE="Release"
PACKAGE_TYPE="run"
```

也就是说：编译中间产物落在仓库根的 `build/`，最终的 run 包落在 `build_out/`；默认 32 线程、Release 构建、打包类型为 `run`。

**（2）`--pkg` 系列参数（本讲重点）**

打包相关的帮助信息集中在 [build.sh:49-69](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L49-L69)，核心参数含义如下：

| 参数 | 说明 |
| :--- | :--- |
| `--pkg` | 编译并打包（生成 run 包） |
| `--pkg-type=<TYPE>` | 指定包类型：`run` / `rpm` / `deb` / `all` 或逗号组合，默认 `run` |
| `-p, --cann_path` | 指定 CANN 安装目录，root 默认 `/usr/local/Ascend/cann`，非 root 默认 `$HOME/Ascend/cann` |
| `-j` | 编译线程数，默认 32，超过 CPU 核数会自动下调 |
| `--cann_3rd_lib_path` | 三方库依赖目录（离线下载场景使用） |
| `--asan` | 开启 AddressSanitizer 内存检测 |

`--pkg-type` 的合法取值由 `check_param_pkg_type()` 校验，只接受 `run/rpm/deb/all` 或 `deb,rpm` 组合（见 [build.sh:365-377](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L365-L377)）。

**（3）`set_env()`：定位 CANN 并 source set_env.sh**

这是连接「环境准备」与「编译」的桥梁，见 [build.sh:577-602](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L577-L602)：

```bash
set_env() {
  if [ "${USER_ID}" != "0" ]; then
    DEFAULT_TOOLKIT_INSTALL_DIR="${HOME}/Ascend/cann"
  else
    DEFAULT_TOOLKIT_INSTALL_DIR="/usr/local/Ascend/cann"
  fi

  if [ -n "${cann_path}" ];then
    ASCEND_CANN_PACKAGE_PATH=${cann_path}                 # ① 优先用 -p 传入
  elif [ -n "${ASCEND_HOME_PATH}" ];then
    ASCEND_CANN_PACKAGE_PATH=${ASCEND_HOME_PATH}           # ② 其次用已有环境变量
  ...
  else
    log "Error: Please set the cann package installation directory through parameter -p|--cann_path."
    exit 1
  fi

  source $ASCEND_CANN_PACKAGE_PATH/set_env.sh || echo "0"  # ③ 关键：source set_env.sh
}
```

这段代码揭示了 CANN 路径的查找优先级：**命令行 `-p` > `ASCEND_HOME_PATH` 环境变量 > `ASCEND_OPP_PATH` > 默认安装目录**。如果都没找到就报错退出。这也解释了为什么上一节强调必须先配置好 `set_env.sh`。

**（4）`build_package()`：打包入口**

确定路径后，`main()` 把所有 cmake 定义拼进 `CUSTOM_OPTION`，最后调用 `build_package()`，见 [build.sh:630-635](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L630-L635)：

```bash
function build_package(){
  CUSTOM_OPTION="${CUSTOM_OPTION} -DENABLE_TEST=OFF -DCMAKE_BUILD_TYPE=${BUILD_TYPE} -DENABLE_BUILD_DEVICE=${ENABLE_BUILD_DEVICE} -DPACKAGE_TYPE=${PACKAGE_TYPE}"
  cmake_config
  build package
  collect_package_artifacts
}
```

`cmake_config` 执行 `cmake ..`，`build package` 实际执行 `cmake --build . --target package`（即调用 cpack 打包），最后 `collect_package_artifacts` 把生成的包拷贝到 `build_out/`。

**（5）`main()` 的分支逻辑**

最关键的调度逻辑在 [build.sh:809-819](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L809-L819)：

```bash
if [ -n "${TEST}" ]; then
  build_test
elif [ -n "$TEST_PART" ]; then
  build_test_part
elif [ -n "${PKG}" ]; then
  CUSTOM_OPTION="${CUSTOM_OPTION} ..."
  build_package
else
  cmake_config
  build all
fi
```

注意 `--pkg` 与 `-t/--test` **互斥**（由 `check_param_test_pkg()` 在 [build.sh:340-345](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L340-L345) 强制校验），二者不能同时使用。

#### 4.2.4 代码实践

1. **实践目标**：用 `build.sh --pkg` 完成一次完整编译，理解参数如何映射到 cmake 选项。

2. **操作步骤**：
   - 确认已 `source set_env.sh`（见 4.1.4）。
   - 进入仓库根目录，先查看帮助：`bash build.sh --pkg -h`（注意 `-h` 会根据上下文显示打包相关帮助，见 [build.sh:266-319](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L266-L319) 的上下文帮助逻辑）。
   - 执行一键编译打包：`bash build.sh --pkg -j 8`（`-j 8` 限制为 8 线程，便于观察）。

3. **需要观察的现象**：脚本会逐行打印带时间戳的 `[时间] Info: ...` 日志；先执行 `cmake ..` 配置，再执行 `cmake --build . --target package`；最后 `collect_package_artifacts` 把包拷到 `build_out/`。

4. **预期结果**：`build_out/` 目录下出现一个名为 `cann-asc-devkit_${cann_version}_linux-$(uname -m).run` 的文件（产物命名见 [docs/zh/quick_start.md:231](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L231)）。把它的完整文件名记录下来。

5. **若编译失败**：最常见原因是 CANN 版本不配套，对照 [docs/zh/quick_start.md:233-237](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L233-L237) 检查版本。如无法运行，标注「待本地验证」。

> 💡 **安装产物**：编译成功后，进入 `build_out` 执行 `./cann-asc-devkit_*.run --full` 即可把内容安装到默认路径 `/usr/local/Ascend`，并**覆盖**原 CANN 包中的 Ascend C 内容（见 [docs/zh/quick_start.md:240-248](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L240-L248)）。这是把「自己改过的 Ascend C」生效到本机环境的标准方式。

#### 4.2.5 小练习与答案

**练习 1**：执行 `bash build.sh --pkg --pkg-type=rpm` 会发生什么？如果改成 `--pkg-type=zip` 呢？

> **参考答案**：`rpm` 合法，脚本会额外生成 rpm 包并拷贝到 `build_out/`。`zip` 不在合法集合 `{run, rpm, deb, all, deb,rpm}` 中，`check_param_pkg_type()`（[build.sh:365-377](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L365-L377)）会报 `[ERROR] --pkg-type must be run, rpm, deb, all ...` 并退出。

**练习 2**：`build.sh` 如何避免 `-j` 设得过大拖垮机器？

> **参考答案**：`check_param_j()`（[build.sh:321-331](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L321-L331)）会比较 `THREAD_NUM` 与 `CPU_NUM`（来自 `/proc/cpuinfo`），一旦超过核数就给出 `[WARNING]` 并自动下调到核数；同时非正整数会被拒绝。

---

### 4.3 CMake 工程结构

#### 4.3.1 概念说明

`build.sh` 只是外壳，真正描述「工程怎么编译」的是顶层 [`CMakeLists.txt`](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt) 与它 include 的若干 `cmake/*.cmake` 文件。理解 CMake 工程结构，能帮你回答三个问题：① 哪些子目录会被编译？② 编译需要依赖哪些外部组件？③ 打包逻辑写在哪里？本模块把这三个问题与对应的源码点一一对应。

#### 4.3.2 核心流程

顶层 CMakeLists.txt 的组织脉络：

```text
CMakeLists.txt
  ├── cmake_minimum_required(3.16.0)
  ├── include(cmake/fetch_cann_cmake.cmake)   # 引入 CANN 公共 cmake 模块
  ├── project(asc-devkit)
  ├── add_subdirectory(impl)                   # 编译 impl 下的各 API 实现
  ├── add_subdirectory(tools)
  ├── add_subdirectory(cmake)
  ├── include(version.cmake)                   # 引入版本与依赖声明
  └── if(PACKAGE_OPEN_PROJECT)                 # --pkg 时启用
        └── include(cmake/package.cmake)       # 打包配置（makeself/cpack）
```

简言之：`--pkg` 模式会额外打开 `PACKAGE_OPEN_PROJECT` 开关，从而把 `cmake/package.cmake` 的打包逻辑挂进来，最终由 cpack 产出 run 包。

#### 4.3.3 源码精读

**（1）顶层工程骨架**

顶层 [CMakeLists.txt:11-16](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt#L11-L16) 设定工程名与最小 CMake 版本，并引入 CANN 官方的 cmake 模块：

```cmake
cmake_minimum_required(VERSION 3.16.0)
include(cmake/fetch_cann_cmake.cmake)
project(asc-devkit)

init_cann_project(PREPEND_MODULE_PATH)
add_cann_target_options()
```

紧接着用三个 `add_subdirectory` 把要编译的子目录挂上来（[CMakeLists.txt:42-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt#L42-L44)）：`impl`（API 实现主体）、`tools`、`cmake`。其中 `impl` 正是 [u1-l2 仓库目录结构与源码组织](u1-l2-repo-structure-source-layout.md) 讲到的镜像结构中「实现」那一侧。

**（2）测试与打包的条件挂载**

`tests` 和打包逻辑都是条件包含的，见 [CMakeLists.txt:53-63](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt#L53-L63)：

```cmake
if(ENABLE_TEST)
  add_subdirectory(tests)        # 仅在 -t/--test 时编译 tests 目录
endif()

if(PACKAGE_OPEN_PROJECT)         # 仅在 --pkg 时启用打包
  cmake_minimum_required(VERSION 3.16)
  project(asc-devkit VERSION 1.0.0)
  include(CMakePrintHelpers)
  include(cmake/package.cmake)
  include(cmake/version_info.cmake)
endif()
```

这解释了 `build.sh` 里两个开关的含义：`-DENABLE_TEST=ON` 对应 UT 编译，`-DPACKAGE_OPEN_PROJECT=ON` 对应打包。注意 `build.sh` 在 `main()` 中正是通过设置这两个 cmake 定义来切换模式（见 [build.sh:772-790](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L772-L790)）。

**（3）CANN 路径强校验**

CMake 侧对 CANN 包路径有一道硬性检查，见 [cmake/config.cmake:11-13](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/config.cmake#L11-L13)：

```cmake
if (NOT EXISTS "${ASCEND_CANN_PACKAGE_PATH}")
    message(FATAL_ERROR "${ASCEND_CANN_PACKAGE_PATH} does not exist, please install the cann package and set environment variables.")
endif()
```

这与 `build.sh` 的 `set_env()` 形成双保险：脚本负责找到路径并 `-DASCEND_CANN_PACKAGE_PATH=...` 传给 cmake，cmake 再校验路径真实存在。如果路径不存在，cmake 配置阶段就直接 `FATAL_ERROR` 退出。

**（4）版本与依赖声明**

[`version.cmake`](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/version.cmake) 是理解「为什么 asc-devkit 不能独立升级」的关键，见 [version.cmake:11-20](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/version.cmake#L11-L20)：

```cmake
set_cann_package(asc-devkit VERSION "9.2.0")

set_cann_build_dependencies(runtime "CUR_MAJOR_MINOR_VER")
set_cann_build_dependencies(metadef "CUR_MAJOR_MINOR_VER")

set_cann_run_dependencies(runtime "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(ge-executor "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(metadef "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(bisheng-compiler "CUR_MAJOR_MINOR_VER")
set_cann_run_dependencies(tbe-tik "CUR_MAJOR_MINOR_VER")
```

可以读出三件事：① asc-devkit 当前版本是 `9.2.0`；② **编译期**依赖 `runtime` 和 `metadef`；③ **运行期**依赖 `runtime`、`ge-executor`、`metadef`、`bisheng-compiler`（毕昇编译器）、`tbe-tik`。`CUR_MAJOR_MINOR_VER` 表示必须主次版本号一致（即 `9.2`），这就是「配套版本」约束的技术来源。

**（5）打包产物如何落到 build_out**

最后看打包配置。[`cmake/package.cmake`](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/package.cmake) 引入 makeself 来制作自解压 run 包（[cmake/package.cmake:11](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/package.cmake#L11)），并在末尾把 cpack 的输出目录指向仓库根的 `build_out`（[cmake/package.cmake:81-88](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/cmake/package.cmake#L81-L88)）：

```cmake
if (NOT ENABLE_COV AND NOT ENABLE_UT)
    set_cann_cpack_config(
        asc-devkit
        OUTPUT "${CMAKE_SOURCE_DIR}/build_out"
        ENABLE_DEVICE "${ENABLE_BUILD_DEVICE}"
        PACKAGE_TYPE "${PACKAGE_TYPE}"
    )
endif()
```

这就是为什么 `build.sh` 最终能在 `build_out/` 下找到 run 包，再由 `collect_package_artifacts` 拷贝归整。

#### 4.3.4 代码实践

1. **实践目标**：通过阅读 CMake 文件，画出「参数 → cmake 开关 → 产物」的映射关系。

2. **操作步骤**：
   - 打开 [build.sh:760-820](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L760-L820) 的 `main()`，找出它为 `--pkg` 模式设置了哪些 `-D` 定义（提示：`PACKAGE_OPEN_PROJECT`、`ENABLE_BUILD_DEVICE`、`PACKAGE_TYPE`）。
   - 在 [CMakeLists.txt](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/CMakeLists.txt) 中搜索 `PACKAGE_OPEN_PROJECT`，确认它如何触发 `cmake/package.cmake`。
   - 在 [version.cmake](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/version.cmake) 中记录 asc-devkit 的版本号与运行期依赖的 5 个组件名。

3. **需要观察的现象**：你会看到一条清晰的链路——`--pkg`（bash 参数）→ `PACKAGE_OPEN_PROJECT=ON`（cmake 定义）→ `cmake/package.cmake`（打包逻辑）→ `build_out/*.run`（产物）。

4. **预期结果**：在笔记中画出下面这张映射表：

   | bash 参数 | cmake 定义 | 触发的 CMake 文件 | 产物 |
   | :--- | :--- | :--- | :--- |
   | `--pkg` | `PACKAGE_OPEN_PROJECT=ON` | `cmake/package.cmake` | `build_out/cann-asc-devkit_*.run` |
   | `-t/--test` | `ENABLE_TEST=ON` | `tests/`（add_subdirectory） | UT 可执行与日志 |

5. **若无法编译**：标注「待本地验证」，仅完成阅读映射即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么即使你不写测试，`version.cmake` 里声明的 `bisheng-compiler` 依赖仍然必须满足？

> **参考答案**：因为 `set_cann_run_dependencies(bisheng-compiler ...)` 把毕昇编译器列为**运行期**依赖，而 Ascend C 的算子源码（`.asc`）正是由毕昇编译器编译成设备代码的。缺少它，即便能完成本仓的 cmake 配置，也无法真正编译运行算子。

**练习 2**：如果把顶层 `CMakeLists.txt` 里的 `add_subdirectory(impl)` 注释掉，`bash build.sh --pkg` 还能成功吗？为什么？

> **参考答案**：能打出 run 包流程本身不一定会立刻报错，但产物里会缺失 Ascend C 的核心 API 实现（impl 目录下的头文件/库不会被安装），安装后用到的算子 API 会找不到实现。`impl` 是整个工程真正的内容主体，不可省略。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次**从环境准备到产物安装**的完整闭环。这是一个贯穿型任务，建议在有 CANN 环境（云环境/Docker/DevContainer 均可）的机器上完成。

**任务**：在本地或容器中跑通「装 CANN 包 → 编译 asc-devkit → 安装 run 包 → 验证覆盖」全流程。

**步骤**：

1. **环境自检**：执行 `npu-smi info` 与 `source ${安装路径}/cann/set_env.sh`，确认 `ASCEND_CANN_PACKAGE_PATH`（或 `ASCEND_HOME_PATH`）有值。若使用 DevContainer，参考 [.devcontainer/devcontainer.json:10-25](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/.devcontainer/devcontainer.json#L10-L25) 确认设备已映射。

2. **依赖核对**：对照 [docs/zh/quick_start.md:206-211](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L206-L211) 检查 `python>=3.9`、`gcc/g++>=7.3`（且版本一致）、`cmake>=3.16`、`pkg-config>=0.29`。

3. **一键编译**：在仓库根目录执行 `bash build.sh --pkg -j 8`，观察 [build.sh:630-635](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L630-L635) 描述的三步（`cmake_config` → `build package` → `collect_package_artifacts`）。

4. **记录产物**：列出 `build_out/` 目录内容，记下 run 包的完整文件名（应为 `cann-asc-devkit_${版本}_linux-$(uname -m).run`）。

5. **安装覆盖**：进入 `build_out` 执行 `./cann-asc-devkit_*.run --full`（参考 [docs/zh/quick_start.md:243-248](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/quick_start.md#L243-L248)），观察它如何覆盖原 CANN 包中的 Ascend C 内容。

6. **版本印证**：打开 [version.cmake:11-20](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/version.cmake#L11-L20)，核对你记录的版本号与文件中 `VERSION "9.2.0"` 是否一致，并确认运行期依赖的 5 个组件。

**验收标准**：能口述「`--pkg` → `PACKAGE_OPEN_PROJECT` → `cmake/package.cmake` → `build_out/*.run`」这条链路，并解释为何不能脱离配套 CANN 版本独立升级。若全程无法在本地运行，至少完成步骤 1、2、6 的源码阅读部分，并对其余步骤标注「待本地验证」。

## 6. 本讲小结

- 环境准备有四种方式（云开发环境、CANN 官方 Docker 镜像、DevContainer、手动安装），核心是按「是否有 NPU」与「使用目标」二选，最终都要让 `set_env.sh` 可被 `source`。
- CANN 包分 toolkit（必选）与 ops（可选），两者须装到同一 `${install_path}`；仅编译 + 仿真时不强制安装驱动。
- `build.sh` 是 CMake/make/cpack 的封装，`--pkg` 是最常用的打包入口，`-p` 指定 CANN 路径、`-j` 控制线程数、`--pkg-type` 选包格式，且 `--pkg` 与 `--test` 互斥。
- CANN 路径查找优先级为「`-p` 参数 > `ASCEND_HOME_PATH` > `ASCEND_OPP_PATH` > 默认目录」，`build.sh` 与 `cmake/config.cmake` 双重校验该路径。
- 顶层 `CMakeLists.txt` 通过 `add_subdirectory(impl)` 挂载 API 实现主体，通过 `PACKAGE_OPEN_PROJECT` 条件引入 `cmake/package.cmake` 完成打包。
- `version.cmake` 声明 asc-devkit 版本为 `9.2.0`，并锁定 runtime/metadef/bisheng-compiler 等组件的配套版本，这是「不能独立升级」的根因。

## 7. 下一步学习建议

掌握了环境准备与编译流程后，你已经具备了「把 Ascend C 源码变成可运行产物」的能力。接下来建议：

- 进入 **u2 单元（第一个算子——编程模型入门）**，学习 [u2-l1 .asc 源文件与 Host/Device 混合编译模型](u2-l1-asc-file-host-device-model.md)，亲手编写并编译第一个 `hello_world` 与矢量加法算子。
- 如果你对工程化打包与编译模式（动态/静态库、AOT、aclrtc）更感兴趣，可以提前浏览 [examples/01_simd_cpp_api/02_features/04_compile/README.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/02_features/04_compile/README.md)，这部分会在专家层 **u15 编译工程与多芯片适配** 中系统讲解。
- 想了解 CI 如何用 `build.sh` 批量跑测试，可先阅读 [build.sh:676-720](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/build.sh#L676-L720) 的 `build_test_part()`，对应专家层 **u16 测试体系与二次开发贡献**。
