# 环境准备与源码获取

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解「编译态」和「运行态」两种环境需求的差别，知道各自需要安装哪些组件。
2. 根据自己有无昇腾设备，从 CANNLab、Docker、手动安装三种方式中选出合适的部署路径。
3. 完成环境验证：用 `npu-smi info` 检查驱动、用 `ascend_toolkit_install.info` 查询 CANN 版本、用 `set_env.sh` 配置环境变量。
4. 理解 CANN 软件版本与本仓库 git 标签的配套关系，能用 `git clone -b <tag>` 拉取正确版本的源码，而不是盲目使用 master。

## 2. 前置知识

在动手之前，先弄清楚几个贯穿本讲的概念：

- **NPU 驱动（driver）/ 固件（firmware）**：直接和昇腾硬件打交道的最底层软件，安装在宿主机操作系统上。没有驱动，任何上层软件都无法使用 NPU。驱动属于**运行态依赖**——只编译算子、不实际运行的话，可以不装。
- **CANN（Compute Architecture for Neural Networks）**：华为面向 NPU 的异构计算架构，可以类比成「NPU 版的 CUDA Toolkit」。本讲涉及它的两个包：
  - **toolkit 包**（`Ascend-cann-toolkit_*.run`）：编译算子必需，提供编译器、头文件、库等开发工具。
  - **ops 包**（`Ascend-cann-<soc>-ops_*.run`）：包含已发布的官方算子产物，运行算子时依赖，仅编译时可不装。
- **版本配套**：本仓库源码跟随 CANN 版本发布，每个 CANN 版本对应仓库的一个 git 标签（如 `v9.0.0`）。源码和 CANN 包版本不匹配时，编译可能失败或行为异常。这是上一讲已经强调过的关键约束，本讲要把它落到实处。
- **`npu-smi`**：NPU 的系统管理接口工具，类似 GPU 世界的 `nvidia-smi`，用于查看设备状态。
- **`uname -m`**：查询 CPU 架构（`aarch64` 或 `x86_64`），下载 CANN 包和查安装信息时都要用到这个值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/install/quick_install.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md) | 官方环境部署文档，本讲的主线依据：三种安装方式、环境验证、环境变量配置 |
| [README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md) | 仓库首页，「版本配套」「环境准备」「源码下载」三节与本讲直接相关 |
| [install_deps.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh) | 基础依赖一键安装脚本：检测 OS、按版本要求安装 python/gcc/cmake 等 |
| [requirements.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/requirements.txt) | Python 三方库依赖清单，用 `pip3 install -r` 安装 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 环境部署**（CANNLab / Docker / 手动安装三条路径 + 验证 + 环境变量）和 **4.2 源码管理**（版本配套关系 + `git clone -b <tag>` + 仓库依赖安装）。

### 4.1 环境部署

#### 4.1.1 概念说明

「环境部署」要回答的问题是：**让一台机器具备编译（必要时运行）ops-transformer 算子的能力，到底需要装什么？**

官方文档先给出了一个重要的二分法——**编译态 vs 运行态**，见 [quick_install.md:L9-L12](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L9-L12)：

- **编译态**：只编译不运行 → 只需 CANN toolkit 包。
- **运行态**：要真正跑算子 → 驱动 + toolkit 包 + ops 包三者都要。

这个区分非常实用：如果你手头没有昇腾硬件（只有一台 x86 服务器或笔记本），你依然可以走「编译态」路线，完整体验 `build.sh` 编译流程（下一讲的内容）；只有到运行示例那一讲，才必须找到真实 NPU 或使用 NPU Simulator 仿真。

在此之上，官方提供了三种部署方式，对应三类读者，见 [quick_install.md:L14-L18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L14-L18)：

| 方式 | 适用人群 | 特点 |
| --- | --- | --- |
| CANNLab | 没有昇腾设备的开发者 | 在线一站式平台，云上已有 NPU 环境，默认装最新版 CANN |
| Docker | 有昇腾设备、想快速搭环境的开发者 | 镜像预集成 CANN 包和依赖，支持 A2/A3 系列 |
| 手动安装 | 有昇腾设备、想体验 master 或深度定制的开发者 | 灵活性最高，步骤也最多 |

注意两种「预集成」方式（CANNLab、Docker）都**默认安装最新版本 CANN 包**，所以后续拉源码时必须注意配套关系——这正是 4.2 节的主题。

#### 4.1.2 核心流程

以最常用的 Docker 方式为例，完整部署流程如下：

```text
① 宿主机装 NPU 驱动（运行态依赖）
      └─ npu-smi info 能显示设备信息 → 驱动 OK
② docker pull 拉取昇腾开发镜像（-devel 后缀 = 算子开发镜像）
③ docker run 以特定参数启动容器
      ├─ --device /dev/davinci0            映射 NPU 设备卡
      ├─ --device /dev/davinci_manager     设备管理接口
      ├─ --device /dev/devmm_svm           设备内存管理接口
      ├─ --device /dev/hisi_hdc            主机-设备通信接口
      └─ -v 挂载 dcmi / npu-smi / 驱动库 / 版本信息
④ 容器内验证：npu-smi info + 查 CANN 安装信息文件
⑤ source set_env.sh 使环境变量生效
```

手动安装方式则是「驱动 → toolkit 包 → ops 包」三步，核心安装命令为：

```bash
# toolkit 包（编译态必需）
bash ./Ascend-cann-toolkit_${cann_version}_linux-${arch}.run --install --install-path=${install_path}
# ops 包（运行态依赖，仅编译可不装）
bash ./Ascend-cann-${soc_name}-ops_${cann_version}_linux-${arch}.run --install --install-path=${install_path}
```

其中 `${arch}` 用 `uname -m` 查询；ops 包必须和 toolkit 包装在**相同路径**（root 用户默认 `/usr/local/Ascend`）。

#### 4.1.3 源码精读

**（1）Docker 镜像标签的命名规则**

镜像拉取示例见 [quick_install.md:L58-L63](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L58-L63)，这段示例命令演示了拉取一个 CANN 9.1.0-beta.1、910b 芯片、openEuler 24.03、Python 3.12 的开发镜像：

```bash
docker pull swr.cn-south-1.myhuaweicloud.com/ascendhub/cann:9.1.0-beta.1-910b-openeuler24.03-py3.12-devel
```

文档特别说明：**镜像标签格式为 `<CANN版本>-<芯片系列>-<操作系统>-<Python版本>-devel`**。这里第一个字段就是你后续选源码标签的依据——拉了 9.1.0 的镜像，就该配 `v9.1.0`（或配套分支）的源码。带 `-devel` 后缀的是算子开发镜像，内含编译依赖，本仓库开发必须选它。

**（2）docker run 的设备映射参数**

完整的容器启动命令见 [quick_install.md:L69-L71](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L69-L71)。关键点在于容器必须能「看到」宿主机的 NPU，文档用一张参数表逐项解释了每个 `--device` 和 `-v` 的用途（[quick_install.md:L75-L89](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L75-L89)），其中最核心的两条：

- `--device /dev/davinci0`：把宿主机第 0 张 NPU 卡映射进容器。有几张卡、要映射哪张，先在宿主机执行 `npu-smi info` 看设备编号再调整。
- `-v /usr/local/Ascend/driver/lib64/:...`：把宿主机驱动库挂载进容器——**驱动装在宿主机、CANN 装在容器内**，这是昇腾 Docker 部署的基本格局。

**（3）环境验证命令**

安装完成后的验证步骤见 [quick_install.md:L173-L195](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L173-L195)：

```bash
# 检查驱动：能正常显示设备信息即驱动正常
npu-smi info
# 检查 CANN toolkit 包版本（docker 和手动安装场景，默认路径）
cat /usr/local/Ascend/cann/${arch}-linux/ascend_toolkit_install.info
# 检查 CANN ops 包版本
cat /usr/local/Ascend/cann/${arch}-linux/ascend_ops_install.info
```

注意路径区分场景：CANNLab 场景下路径前缀是 `/home/developer/Ascend/cann/...`，Docker 和手动安装场景是 `/usr/local/Ascend/cann/...`。记下这里读到的版本号，它决定了 4.2 节你要拉哪个标签的源码。

**（4）环境变量配置**

最后一步见 [quick_install.md:L197-L206](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L197-L206)：

```bash
# 默认路径安装，root 用户（非 root 用户把 /usr/local 换成 ${HOME}）
source /usr/local/Ascend/cann/set_env.sh
```

`set_env.sh` 会把 CANN 的二进制目录、库路径等注入当前 shell 的环境变量（`PATH`、`LD_LIBRARY_PATH` 等）。它是**会话级**的——每开一个新终端都要重新 source，很多人第一次编译报「找不到编译器/头文件」就是忘了这一步。后续使用 `build.sh` 编译前，务必确认已执行。

#### 4.1.4 代码实践

**实践目标**：完成一次环境部署与验证，并留下环境信息记录。

**操作步骤**（按自己的条件选一条路径）：

1. 无昇腾设备：进入仓库 GitCode 主页，点击 `CANNLab` 按钮创建云上 NPU 环境（参考 [quick_install.md:L20-L32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L20-L32)）。
2. 有昇腾设备 + Docker：先在宿主机确认 `npu-smi info` 有输出（没有则按文档装驱动），再依次执行 `docker pull` 和文档给出的 `docker run` 命令进入容器。
3. 有昇腾设备 + 手动安装：按 [quick_install.md:L91-L134](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L91-L134) 安装 toolkit 包（以及需要运行时的 ops 包）。
4. 统一执行验证命令，并 `source set_env.sh`。

**需要观察的现象**：

- `npu-smi info` 输出中包含 NPU 卡号、型号、驱动/固件版本。
- `cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend_toolkit_install.info` 输出一个版本号字符串（如 `9.1.0` 一类）。

**预期结果**：把两条输出记录下来，形成一条「我的环境档案」：

```text
NPU 型号：____________（npu-smi info 中获取）
驱动版本：____________
CANN toolkit 版本：____________（ascend_toolkit_install.info 中获取）
CPU 架构：____________（uname -m）
```

这份档案在 4.2 的实践中会直接用到。若你在纯 x86 无 NPU 的编译态环境，`npu-smi info` 一项标注「无 NPU（编译态）」即可。本实践的运行结果**待本地验证**（取决于你的实际环境）。

#### 4.1.5 小练习与答案

**练习 1**：你的服务器上没有任何昇腾硬件，但你想先学习算子的 host 侧编译流程，最少需要安装什么？

**答案**：只需安装 CANN toolkit 包（编译态）。驱动和 ops 包都是运行态依赖，不运行算子可以不装；参考 [quick_install.md:L9-L12](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L9-L12)。

**练习 2**：Docker 容器里执行 `npu-smi info` 报「找不到设备」，最可能漏了 `docker run` 的哪个参数？

**答案**：最可能漏了 `--device /dev/davinci0`（NPU 设备卡映射）或 `-v /usr/local/Ascend/driver/lib64/` 驱动库挂载。参数含义见 [quick_install.md:L75-L89](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L75-L89)；设备编号要先在宿主机上用 `npu-smi info` 确认。

**练习 3**：为什么每次新开终端都要重新执行 `source /usr/local/Ascend/cann/set_env.sh`？

**答案**：`set_env.sh` 修改的是当前 shell 进程的环境变量（`PATH`、`LD_LIBRARY_PATH` 等），环境变量不跨会话持久化。想一劳永逸可以把 source 命令写进 `~/.bashrc`。

### 4.2 源码管理

#### 4.2.1 概念说明

「源码管理」要回答的问题是：**如何保证手里的源码和环境里的 CANN 包是配套的？**

上一讲已经建立了「版本配套是关键约束」的认知，本节看官方是如何把这个约束落成操作规范的。README 的版本配套一节（[README.md:L31-L34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L31-L34)）明确说了两件事：

1. CANN 软件版本与本项目标签的对应关系，记录在 [release-management 仓库](https://gitcode.com/cann/release-management)的版本说明里——这是配套关系的**权威查询入口**。
2. **使用 master 分支可能存在版本不匹配的风险**——不要图省事直接 clone master。

为什么配套这么重要？因为本仓库的算子在编译时会引用 CANN toolkit 提供的头文件和接口（如 tiling 框架、Ascend C 原语），CANN 版本演进中这些接口会变化；源码标签和 toolkit 版本错位，轻则编译报错，重则编译通过但行为异常。

#### 4.2.2 核心流程

```text
① 从环境档案拿到 CANN 版本号（4.1.4 实践的产出）
② 到 release-management 仓库查该 CANN 版本对应的源码标签
③ git clone -b <tag> https://gitcode.com/cann/ops-transformer.git
④ 在仓库根目录执行 bash install_deps.sh 安装系统级依赖
⑤ 执行 pip3 install -r requirements.txt 安装 Python 依赖
```

本仓库实际的标签形态（在仓库中执行 `git tag` 可见）是一系列以 CANN 版本命名的标签，例如 `v8.5.0`、`v9.0.0`、`v9.0.1`、`v9.1.0`、`v9.2.0-beta.1` 等，同时远端还维护着同名分支（如 `origin/9.0.0`、`origin/9.1.0`）。README 的示例（[README.md:L42-L47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L42-L47)）用的是不带 `v` 前缀的分支名：

```bash
# 通用命令：git clone -b ${tag_version} https://gitcode.com/cann/ops-transformer.git
git clone -b 9.0.0 https://gitcode.com/cann/ops-transformer.git
```

即 CANN 版本为 9.0.0 时，拉取 `9.0.0` 分支（或 `v9.0.0` 标签）的源码。另外 [README.md:L49](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L49) 提示：若环境中已存在配套分支源码可跳过下载——例如 CANNLab 默认已在 `/mnt/workspace/gitCode` 提供了最新版本 CANN 对应的源码。

#### 4.2.3 源码精读

**（1）基础依赖清单**

[quick_install.md:L138-L147](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L138-L147) 列出了本项目的全部系统级依赖及版本要求：

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| python | >= 3.7.0（建议 <= 3.10） | |
| gcc | >= 7.3.0 | |
| cmake | >= 3.18.4 | |
| pigz | >= 2.4（可选） | 提升打包速度 |
| dos2unix | — | 处理换行符 |
| make / patch | — | |
| googletest | 建议 release-1.11.0 | 仅执行 UT 时依赖 |

注意最后一条：googletest 只有跑单元测试才需要，这为下一单元「编译最小算子」进一步减轻了负担。

**（2）install_deps.sh：一键安装脚本的骨架**

脚本入口和总体流程见 [install_deps.sh:L604-L622](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L604-L622)，`main` 函数按固定顺序编排各安装函数：

```bash
main() {
    detect_os
    install_python
    install_python_deps
    install_gcc
    install_patch
    install_cmake
    install_pigz
    install_dos2unix
    install_googletest
}
```

每个安装函数都遵循同一模式：**先检查版本是否达标，达标就跳过，不达标才安装**。以 cmake 为例，[install_deps.sh:L414-L427](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L414-L427) 先解析 `cmake --version` 输出的版本号，与要求的 `3.18.4` 做比较，满足即返回——这意味着在已配置好的 Docker 镜像里重跑脚本是安全幂等的。

版本比较由通用函数 `version_ge` 完成（[install_deps.sh:L37-L52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L37-L52)）：把 `xx.xx.xx` 格式的版本号按 `.` 切成数组逐段比较，缺位补 0。操作系统探测则由 `detect_os` 完成（[install_deps.sh:L54-L97](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L54-L97)），通过 `/etc/debian_version`、`/etc/redhat-release` 等特征文件把系统归为 debian（apt）/rhel（dnf 或 yum）/euler/macos 四类，不支持的系统直接退出并提示手动安装。

脚本还支持自定义 PyPI 镜像源参数（[install_deps.sh:L102-L145](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L102-L145)）：`bash install_deps.sh -url "<镜像地址> <信任主机>"`。默认策略在 [install_deps.sh:L219-L233](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L219-L233)：先试清华镜像，失败自动切换华为云镜像，再失败则报错退出。

**（3）requirements.txt：Python 三方库依赖**

[requirements.txt:L1-L12](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/requirements.txt#L1-L12) 是 pip 依赖清单，共 12 项：

```text
pyyaml
absl-py>=2.0.0
jinja2>=3.1.0
numpy<2.0
decorator
sympy
scipy
attrs
protobuf
psutil
packaging>=26.0
setuptools>=59.0.0
```

两个值得注意的细节：`numpy<2.0` 用**上界**锁定大版本（numpy 2.x 有破坏性 API 变更）；`pyyaml`、`jinja2` 等在编译流程中会被构建脚本用来解析配置、渲染模板。用 `pip3 install -r requirements.txt` 一次装齐（见 [quick_install.md:L167-L171](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/quick_install.md#L167-L171)）。

#### 4.2.4 代码实践

**实践目标**：拉取与你环境配套的源码版本，并完成仓库依赖安装，最终写出一份「CANN 版本 ↔ 源码标签」对应记录。

**操作步骤**：

1. 取出 4.1.4 实践记录的 CANN toolkit 版本号（设为 `X.Y.Z`）。
2. 到 [release-management 仓库](https://gitcode.com/cann/release-management) 查 `X.Y.Z` 对应的本仓库标签/分支名。
3. 克隆配套源码：

   ```bash
   git clone -b X.Y.Z https://gitcode.com/cann/ops-transformer.git
   cd ops-transformer
   ```

4. 安装系统级依赖与 Python 依赖：

   ```bash
   bash install_deps.sh
   pip3 install -r requirements.txt
   ```

5. 在仓库中执行 `git tag | grep <主版本>` 与 `git branch -a`，观察标签与分支的命名规律（本仓库可见 `v8.5.0`、`v9.0.0`、`v9.0.1`、`v9.1.0` 等标签及 `origin/8.5.0`、`origin/9.0.0` 等远端分支）。

**需要观察的现象**：

- `install_deps.sh` 逐项输出 `Checking Python / GCC / CMake ...`，已满足版本的依赖显示 `meets requirements` 并跳过。
- `git branch -a` 能看到与 CANN 版本号同名的远端分支。

**预期结果**：写下三行记录——「CANN toolkit 版本 X.Y.Z ↔ 源码标签 vX.Y.Z / 分支 X.Y.Z；clone 命令：`git clone -b X.Y.Z ...`」。之后所有编译、运行实践都在这个配套的代码版本上进行。若你暂时没有任何环境，可先在 GitHub/GitCode 页面浏览 release-management 仓库的版本说明完成第 2、5 步的「纸面版」；完整流程**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：同事告诉你「直接 clone master 分支最新代码就行，反正最新」，这个说法有什么问题？

**答案**：README 明确警告使用 master 分支可能存在版本不匹配的风险（[README.md:L31-L34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L31-L34)）。master 对应的是开发中的最新代码，而环境里的 CANN 包是某个发布版本；源码引用的 CANN 接口一旦超前或滞后于已装的 toolkit，编译就会出问题。正确做法是按 release-management 仓库的对应关系选择配套标签。

**练习 2**：`install_deps.sh` 在已经装好依赖的 Docker 镜像里再跑一遍，会重复安装或报错吗？

**答案**：不会。脚本对每个依赖都先做版本检查，满足要求即跳过（如 cmake 的检查逻辑在 [install_deps.sh:L420-L427](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L420-L427)），是幂等的；Python 依赖部分还会在检测到 numpy 已装时直接跳过固定版本的安装（[install_deps.sh:L193-L201](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/install_deps.sh#L193-L201)）。

**练习 3**：`requirements.txt` 里为什么 `numpy` 写的是 `numpy<2.0` 而其他包大多只写下界？

**答案**：numpy 2.0 相对 1.x 有破坏性 API 变更，许多依赖 numpy C/Python 接口的构建与测试脚本在 2.x 下会失效，所以要用上界把大版本锁在 1.x；其他包向新版本兼容性较好，只需保证不低于某个特性引入的版本即可。见 [requirements.txt:L4](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/requirements.txt#L4)。

## 5. 综合实践

**任务：搭建并归档一个「可复现」的算子开发环境。**

把 4.1 和 4.2 两个实践串起来，在你的环境（物理机、Docker 或 CANNLab）中完成：

1. 按选定方式完成驱动/CANN 包部署，记录 `npu-smi info` 与 `ascend_toolkit_install.info` 的关键输出。
2. `source set_env.sh` 后，执行 `which cmake && gcc --version && python3 --version`，确认三个基础工具可用且版本达标。
3. 按 release-management 的配套关系 `git clone -b <tag>` 拉取源码，运行 `install_deps.sh` 和 `pip3 install -r requirements.txt`。
4. 产出一份 `my-env.md` 环境档案，包含：NPU 型号与驱动版本、CPU 架构、CANN toolkit/ops 版本、源码标签、基础依赖版本表，以及每个关键命令的原始输出摘录。

这份档案是后续所有讲义实践的环境基线：下一讲进入 `build.sh` 构建体系时，你会反复用到其中的 CANN 版本与源码路径；当实践出现诡异报错时，第一反应也应是核对档案中的版本配套关系是否仍然成立。

## 6. 本讲小结

- 环境需求分**编译态**（只需 CANN toolkit 包）和**运行态**（驱动 + toolkit + ops 包），无 NPU 硬件也能走编译态学习路线。
- 三种部署方式：CANNLab（无设备）、Docker（有设备快速搭建，`-devel` 后缀镜像含开发依赖）、手动安装（最灵活）；前两者默认最新版 CANN，更要重视源码配套。
- Docker 部署的核心是设备映射：`--device /dev/davinci0` 等参数把宿主机 NPU 和驱动库暴露给容器。
- 环境验证三板斧：`npu-smi info` 查驱动、`ascend_toolkit_install.info` / `ascend_ops_install.info` 查 CANN 版本、`source set_env.sh` 配环境变量（每个新会话都要执行）。
- 源码必须与 CANN 版本配套：查 release-management 仓库确定标签，用 `git clone -b <tag>` 拉取，不要随意用 master。
- 仓库依赖分两层：`install_deps.sh` 装系统级依赖（幂等、支持自定义镜像源、googletest 仅 UT 需要），`requirements.txt` 装 Python 三方库（numpy 锁定 `<2.0`）。

## 7. 下一步学习建议

环境就绪后，下一讲 [u1-l4-build-system.md](u1-l4-build-system.md) 将拆解 `build.sh` 与 CMake 构建体系，你会第一次真正执行 `bash build.sh --ophost --ops=add_example` 完成最小算子编译。建议提前浏览 [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) 的帮助入口和 [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md)，带着「我的 CANN 版本和 `--soc` 参数怎么对应」这个问题进入下一讲。
