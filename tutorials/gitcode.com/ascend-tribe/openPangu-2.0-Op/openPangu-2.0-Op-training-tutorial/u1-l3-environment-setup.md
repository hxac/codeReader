# 开发环境搭建：镜像、容器与 CANN 环境变量

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚昇腾 NPU 开发环境的三大组成部分：宿主机驱动、容器内的 CANN 工具链、torch_npu 框架适配层，以及为什么这个项目推荐用 docker 镜像来开发。
2. 根据手上机器的芯片类型（A2 / A3 / A5），从 `ascendc/README.md` 中选出正确的 docker 镜像并完成 `docker pull`。
3. 逐行读懂 README 给出的 `docker run` 参考脚本：为什么要挂载 `/dev/davinci*` 设备节点、为什么要挂载宿主机的驱动目录，并能把它从 16 卡改写成 8 卡版本。
4. 在容器内完成 CANN 环境变量初始化（`source .../set_env.sh`），并理解 `build.sh` 是如何沿着环境变量一路找到 CANN 安装目录和 `bisheng` 编译器的。

本讲不涉及任何算子代码的修改，所有操作都只发生在你的开发机和容器里。

## 2. 前置知识

阅读本讲前，建议你先完成 u1-l1（项目全景）和 u1-l2（算子四层模型）。此外需要了解以下基础概念：

- **昇腾 NPU（华为昇腾处理器）**：华为的 AI 加速芯片。本仓库的算子就是跑在这类芯片上的。开发机上插了几张 NPU 卡，操作系统里就会出现 `/dev/davinci0`、`/dev/davinci1` 这样的设备文件。
- **CANN（Compute Architecture for Neural Networks）**：昇腾的计算架构软件栈，相当于 NPU 版的 "CUDA Toolkit"。它包含：
  - **算子编译工具链**：把 Ascend C 源码（`op_kernel` 目录下的 `.cpp`）编译成芯片可执行的指令，其中编译器叫 `bisheng`（毕昇）。
  - **运行时（Runtime）**：Host 侧的 aclnn 接口最终通过它把任务下发到设备。
  - **opp 算子库目录**：编译出的算子包最终安装到 `/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/` 下（u1-l4 详述）。
- **torch_npu**：PyTorch 的昇腾后端适配包，作用类似 GPU 上的 `torch.cuda`，让 `torch.Tensor` 能搬到 NPU 上运算。它与 CANN 版本必须严格配套。
- **docker 基础**：会使用 `docker pull`（拉镜像）、`docker run`（创建并启动容器）、`docker ps`（列出容器）即可。`-v 宿主机路径:容器路径` 表示把宿主机目录挂载进容器；`--device=/dev/xxx` 表示把宿主机设备文件透传给容器。
- **`source` 与环境变量**：`source xxx.sh` 是在**当前 shell** 里逐条执行脚本，因此脚本里 `export` 的环境变量在执行后仍然生效——这是 `set_env.sh` 必须用 `source` 而不能用 `bash` 执行的原因。

一个容易混淆的点先澄清：**驱动装在宿主机，工具链装在容器里**。NPU 驱动必须直接接触硬件内核，所以它属于宿主机；而 CANN、torch_npu 这些用户态软件放在镜像里，随时可以换版本，互不污染。docker run 脚本里大量挂载卷的存在，就是为了让容器能"够得着"宿主机的驱动。

## 3. 本讲源码地图

本讲涉及的文件很少，但每一处都要精读：

| 文件 | 作用 |
| --- | --- |
| `ascendc/README.md` | 项目官方说明书。第 162~217 行的「环境准备」章节是本讲的主线：下载源码、拉镜像、起容器、设环境变量 |
| `ascendc/build.sh` | 编译入口脚本。虽然编译流程在 u1-l4 才展开，但它**如何在环境里寻找 CANN**（`ASCEND_CANN_PACKAGE_PATH` 五级解析、`set_env()` 函数检查 `bisheng`）属于本讲"环境变量"主题 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | 只看一眼第 83~84 行的 `AddConfig("ascend910b"/"ascend910_93")`，用来印证"芯片代号"从哪来 |

注意：`set_env.sh`、`set_env.bash` 本身**不在仓库里**——它们由 CANN 安装包生成，位于容器内的 `/usr/local/Ascend/ascend-toolkit/` 下。本讲引用的是 README 中对它们的调用方式。

## 4. 核心概念与源码讲解

### 4.1 镜像选择：A2 / A3 / A5 与芯片代际

#### 4.1.1 概念说明

昇腾训练芯片有多个代际，本仓库 README 把开发环境分成 **A2、A3、A5** 三类，并分别为它们提供了开源镜像。选错镜像的后果很实际：CANN 版本与芯片不配套时，算子要么编译不过，要么运行时报 `socVersion` 不匹配。

镜像名本身是一份"配料表"，以 A3 为例：

```
swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130
└────────────── 镜像仓库地址 ──────────────┘└────── 镜像名 ──────┘└────────── tag：CANN 与 torch_npu 版本 ─────────┘
```

- `arm`：CPU 架构是 ARM（鲲鹏/飞腾服务器），在 x86 机器上拉了也跑不了；
- `openeuler-py311`：操作系统 openEuler，内置 Python 3.11；
- `a3`：芯片代际标识；
- tag `cann8.5.0-torch_npu2.9.0`：CANN 8.5.0 配 torch_npu 2.9.0，这是一对**严格配套**的版本组合。

#### 4.1.2 核心流程

选择镜像的决策链：

```text
查看手上机器的 NPU 型号（宿主机执行 npu-smi info）
        │
        ▼
确认芯片代际：A2 / A3 / A5
        │
        ▼
按 README「获取 docker 镜像」章节选镜像
  A2 → sub_base-arm-openeuler-py311-a2 : cann8.5.0-torch_npu2.9.0
  A3 → sub_base-arm-openeuler-py311-a3 : cann8.5.0-torch_npu2.9.0
  A5 → base-arm-openeuler-py311-a5     : cann9.0.0-torch_npu2.9.0.post2
        │
        ▼
docker pull <镜像>   →   后续编译时 build.sh -c <soc_version> 要与之匹配
```

镜像代际与编译参数 `-c`（soc_version）的对应关系：

| 机器芯片 | 镜像 | CANN 版本 | 编译用 soc_version |
| --- | --- | --- | --- |
| A3 | `sub_base-...-a3` | cann8.5.0 | `ascend910_93`（README 明确「以A3环境举例」后使用的就是它） |
| A2 | `sub_base-...-a2` | cann8.5.0 | `ascend910b`（依据算子注册值推断，待本地验证） |
| A5 | `base-...-a5` | cann9.0.0 | `ascend950`（依据 README 编译示例推断，待本地验证） |

其中 `ascend910b`、`ascend910_93` 两个值可以在算子源码里直接看到——每个算子的 `_def.cpp` 就是按这些字符串注册芯片支持的。

#### 4.1.3 源码精读

**① README「获取 docker 镜像」——三类镜像的原始出处**（[ascendc/README.md:L172-L178](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L172-L178)）：

这一节给出了三条 `docker pull` 命令，分别对应 A2/A3/A5。注意 A5 镜像名没有 `sub_` 前缀且使用更新的 cann9.0.0 与 `torch_npu2.9.0.post2`——代际越新，配套的 CANN 也越新。

**② README「编译执行」——soc_version 的合法取值**（[ascendc/README.md:L222-L240](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L222-L240)）：

README 写明 `soc_version` 的取值为 `ascend910b`、`ascend910_93`、`ascend950`，并给出 A3 环境的编译示例 `bash build.sh -c ascend910_93`。这就是上表"A3 → ascend910_93"的直接依据。

**③ 算子侧的芯片注册——`AddConfig`**（[ai_infra_aggregate_hidden_def.cpp:L83-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L84)）：

```cpp
this->AICore().AddConfig("ascend910b", aicore_config);
this->AICore().AddConfig("ascend910_93", aicore_config);
```

这两行说明：同一个算子可以为多个芯片各注册一份编译配置。这也解释了为什么 `build.sh -c` 是"按芯片选择编译目标"的开关——它决定了这次编译要为哪些 soc 生成产物。细节在 u1-l4 展开，这里只需建立"镜像代际 ↔ soc_version 字符串"的对应直觉。

#### 4.1.4 代码实践

**实践目标**：拉取与你机器匹配的镜像，并学会从镜像 tag 里读出配套版本。

**操作步骤**（以下为示例命令，需在配有 NPU 的宿主机上执行）：

1. 在宿主机执行 `npu-smi info`，记录芯片型号与卡数；
2. 按 4.1.2 的表选择镜像，执行拉取，例如 A3 环境：

```bash
docker pull swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130
```

3. 拉取完成后查看本地镜像：`docker images | grep omni`。

**需要观察的现象**：

- `docker pull` 会分层输出下载进度（基础 OS 层、CANN 层、torch 层），镜像体积较大（通常十几 GB），需要耐心；
- `docker images` 中能看到完整的 `REPOSITORY` 与 `TAG`。

**预期结果**：本地镜像列表中出现所选镜像。无 NPU 机器时本实践无法执行，可改为纸面练习：把三个镜像 tag 拆成"CANN 版本 / torch_npu 版本 / 架构 / Python 版本"四列的对照表（答案见 4.1.5 第 1 题）。镜像实际体积**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：三个镜像 tag 分别包含哪些信息？请填表。

答案：

| 镜像 | CANN | torch_npu | 架构/系统/Python |
| --- | --- | --- | --- |
| A2 | 8.5.0 | 2.9.0 | arm / openEuler / py311 |
| A3 | 8.5.0 | 2.9.0 | arm / openEuler / py311 |
| A5 | 9.0.0 | 2.9.0.post2 | arm / openEuler / py311 |

**练习 2**：为什么 A2 与 A3 的 CANN 版本相同也要用两个不同镜像？

答案：芯片代际不同，镜像内的 CANN 细分包/驱动配套、算子编译目标（soc：`ascend910b` vs `ascend910_93`）不同。CANN 大版本号相同不代表针对的芯片相同；README 为每个代际单独提供镜像，就是为了保证"镜像—芯片—编译参数"三者严格配套。

**练习 3**：在一台 x86 服务器上 `docker pull` 了 A3 镜像并 `docker run`，会发生什么？

答案：镜像基于 ARM 架构，在 x86 主机上无法原生运行（除非借助 qemu 模拟，性能极差且无法访问昇腾驱动），容器起不来或立即退出。必须使用 ARM 架构的昇腾训练服务器。

### 4.2 拉起 NPU 容器：docker run 参考脚本逐行拆解

#### 4.2.1 概念说明

docker 的默认隔离模型里，容器**看不到宿主机的任何硬件**。要让容器内的 CANN 操作 NPU，必须把两类东西"递"进容器：

1. **设备节点**：`/dev/davinci0` ~ `/dev/davinci15` 是 16 张 NPU 卡的设备文件；`davinci_manager`、`devmm_svm`、`hisi_hdc` 是管理、内存与通信相关的辅助设备节点；
2. **宿主机驱动目录与配置**：`/usr/local/Ascend/driver` 等。前文说过"驱动在宿主机"，容器要使用它就必须挂载进来。

README 的参考脚本还包含为**多卡集合通信**准备的网络与共享内存设置（`--net=host`、`--ipc=host`、`--shm-size=128g`），这是训练场景（HCCL 多卡 AllReduce 等）的常见需求。

> 说明：README 原文只给出了脚本本身，未逐条注释各挂载卷的用途。下面表格中的用途解释属于昇腾容器部署的通用约定，供理解参考；以你机器上的实际部署文档为准。

#### 4.2.2 核心流程

一条完整的"起容器"流水线：

```text
docker run -u root -itd --name omni_ops_training
    ├── 透传设备：--device=/dev/davinci0..15          （16 张 NPU 卡）
    │            --device=/dev/davinci_manager        （芯片管理节点）
    │            --device=/dev/devmm_svm              （设备内存管理）
    │            --device=/dev/hisi_hdc               （Host-Device 通信）
    ├── 挂载驱动：-v /usr/local/Ascend/driver:...     （宿主机 NPU 驱动）
    │            -v /etc/ascend_install.info:...      （驱动安装信息）
    ├── 挂载管理工具：npu-smi / dcmi / slog 等
    ├── 挂载网络配置：/etc/hccn.conf + --net=host      （机间互联）
    ├── 挂载代码与数据：/home、/data
    └── 训练通信参数：--ipc=host --shm-size=128g --privileged
            │
            ▼
   容器以 /bin/bash 作为主进程在后台运行（-d）
            │
            ▼
   docker attach omni_ops_training 进入容器
```

#### 4.2.3 源码精读

**① README「拉起 docker 容器」参考脚本**（[ascendc/README.md:L179-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L179-L207)）。只保留骨架的关键部分：

```bash
docker run -u root -itd --name omni_ops_training --ulimit nproc=65535:65535 --ipc=host \
    --device=/dev/davinci0     --device=/dev/davinci1 \
    ...（中间省略 davinci2 ~ davinci14）...
    --device=/dev/davinci15 \
    --device=/dev/davinci_manager --device=/dev/devmm_svm \
    --device=/dev/hisi_hdc \
    -v /home/:/home \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /etc/ascend_install.info:/etc/ascend_install.info -v /var/log/npu/:/usr/slog \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /etc/hccn.conf:/etc/hccn.conf \
    --net=host \
    --shm-size=128g \
    --privileged \
    swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130 /bin/bash
```

逐类拆解（完整清单见原文）：

| 参数 | 作用 |
| --- | --- |
| `-u root -itd` | 以 root 运行；`-i`/`-t` 分配交互终端，`-d` 后台常驻 |
| `--name omni_ops_training` | 容器名，README 默认此名，后续 `docker attach` 用它 |
| `--ulimit nproc=65535:65535` | 放宽进程数上限，避免多进程编译/训练时耗尽 |
| `--device=/dev/davinci0~15` | 透传全部 16 张卡；8 卡机器只保留 davinci0~7 即可 |
| `--device=.../davinci_manager、devmm_svm、hisi_hdc` | 管理节点、设备内存管理与主机-设备通信（昇腾通用约定） |
| `-v /usr/local/Ascend/driver:...` | 把宿主机 NPU 驱动挂进容器——"驱动在宿主机"的落点 |
| `-v /etc/ascend_install.info` | 驱动安装信息文件，运行时用于识别驱动版本 |
| `-v /var/log/npu/:/usr/slog` | NPU 日志目录，排查算子运行错误时看这里 |
| `-v /usr/local/bin/npu-smi、/usr/local/dcmi、/usr/local/sbin` | 芯片状态查询与管理工具 |
| `-v /etc/hccn.conf` + `--net=host` | 机间互联网络配置 + 共享宿主机网络栈 |
| `-v /home/:/home、/data:/data` | 代码与数据盘；克隆到 `/home/code` 下的源码因此容器内直接可见 |
| `--ipc=host --shm-size=128g` | 共享内存：多卡通信/数据加载需要大块共享内存 |
| `--privileged` | 特权模式，便于访问设备与 cgroup |
| 末尾镜像名 + `/bin/bash` | 用刚才拉的 A3 镜像创建容器，主进程为 bash |

**② 进入容器**（[ascendc/README.md:L208-L211](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L208-L211)）：

```bash
docker attach omni_ops_training
```

README 采用 `attach`（连接到容器主进程的终端）。一个通用 docker 注意点（README 未提及）：attach 进入后**不要输入 `exit`**——那会结束主进程 `/bin/bash`，整个容器随之停止；应使用 `Ctrl-p Ctrl-q` 分离，让容器继续后台运行。

**③ 源码获取路径**（[ascendc/README.md:L163-L170](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L163-L170)）：README 的 clone 示例拉取的是 `omni-ops` 仓库并 `cd omni-ops/training/ascendc`。你现在阅读的 openPangu-2.0-Op 仓库目录布局与之相同（同样是 `training/ascendc`），因此把本仓库克隆到 `/home/code/` 下、容器内走同一路径即可——`-v /home/:/home` 这个挂载让宿主机克隆的代码在容器内原样可见，无需再拷贝。

#### 4.2.4 代码实践

**实践目标**：把 README 的 16 卡参考脚本改写成 8 卡版本，并验证脚本语法。

**操作步骤**：

1. 复制 README 中完整的 `docker run` 命令；
2. 删除 `--device=/dev/davinci8` 到 `--device=/dev/davinci15` 共 8 行；
3. 其余参数（manager/devmm_svm/hisi_hdc、各挂载卷、网络项）**保持不变**；
4. 保存为 `run_8card.sh` 并在第一行加上 `#!/bin/bash`；
5. 校验语法：`bash -n run_8card.sh`（该命令只做语法解析不执行，任何环境都可运行）；
6. 有 NPU 环境时真正执行：`bash run_8card.sh`，然后 `docker ps` 查看容器状态，再 `docker attach omni_ops_training` 进入。

**需要观察的现象**：

- `bash -n` 无任何输出即语法通过；
- `docker ps` 中 `omni_ops_training` 状态为 `Up`；
- 容器内执行 `ls /dev/davinci*` 应只看到 0~7 号卡及 manager 等节点。

**预期结果**：8 卡容器正常启动，容器内可见 8 张卡。第 5 步可独立完成；第 6 步在无 NPU 环境下**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：容器已经挂载了 `/usr/local/Ascend/driver`，为什么还需要 `--device=/dev/davinci*`？

答案：两者缺一不可。`--device` 把设备文件（内核设备节点）透传进容器，让容器内的进程有权限打开 NPU 硬件；`-v /usr/local/Ascend/driver` 挂载的是驱动的用户态库与配置。没有设备节点，驱动库无硬件可操作；没有驱动挂载，进程找不到与内核驱动对话的用户态入口。

**练习 2**：去掉 `--shm-size=128g` 与 `--ipc=host` 可能引发什么问题？

答案：默认共享内存通常只有 64MB 量级，多卡训练的数据加载与集合通信需要大块共享内存，不足时会报 "out of shared memory" 类错误或性能骤降；`--ipc=host` 让容器与宿主机共享 IPC 命名空间，避免跨进程共享内存访问受限。这些属于训练容器通用配置，具体阈值随模型规模变化。

**练习 3**：宿主机执行 `docker attach omni_ops_training` 报 "No such container"，最可能的原因？

答案：容器未创建或已停止：可能是 `docker run` 那一步失败（如设备节点不存在、镜像没拉到），或之前 attach 后输入 `exit` 导致主进程退出、容器停止。用 `docker ps -a` 检查容器是否存在及其状态；若已退出，可 `docker start omni_ops_training` 后再 attach。

### 4.3 容器内 CANN 环境变量初始化：set_env.sh 与 build.sh 的寻路链

#### 4.3.1 概念说明

进入容器后，CANN 已经安装在 `/usr/local/Ascend/ascend-toolkit/` 下，但它的可执行文件（编译器 `bisheng`、脚本 `setenv.bash` 等）还不在默认 `PATH` 里，关键的安装路径也没有导出成环境变量。README 因此要求手动执行一次：

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
```

这一条命令会设置一批 `ASCEND_*` 环境变量（如 `ASCEND_HOME_PATH` 指向 toolkit 安装目录），并把 CANN 的 bin/lib 目录加入 `PATH`/`LD_LIBRARY_PATH`。它是后续一切编译（`build.sh`）和运行（aclnn 调用）的前置条件。

值得注意的是，**`build.sh` 并不盲信当前环境**：它会自己再找一遍 CANN 安装位置，找不到就报错退出。理解这条"寻路链"，是排查"为什么编译器找不到 bisheng"类问题的关键。

#### 4.3.2 核心流程

从进容器到具备编译条件的完整链路：

```text
docker attach 进入容器
        │
        ▼
source /usr/local/Ascend/ascend-toolkit/set_env.sh     ← README 要求的手动步骤
        │  （导出 ASCEND_HOME_PATH 等环境变量）
        ▼
bash build.sh ...
        │
        ├─► 解析 ASCEND_CANN_PACKAGE_PATH（五级优先链）：
        │      ① 命令行 -p 指定的路径
        │      ② 环境变量 ASCEND_HOME_PATH          ← set_env.sh 的贡献
        │      ③ 环境变量 ASCEND_OPP_PATH 的上级目录
        │      ④ /usr/local/Ascend/ascend-toolkit/latest   （root 用户默认）
        │      ⑤ ~/Ascend/ascend-toolkit/latest            （非 root 默认）
        │      都没有 → 报错退出，提示用 -p 指定
        │
        └─► set_env() 函数：
               source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash
               检查 bisheng 是否在 PATH 中，找不到则报错退出
```

#### 4.3.3 源码精读

**① README「设置环境变量」**（[ascendc/README.md:L213-L217](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L213-L217)）：

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
```

注意是 `source` 不是 `bash`：只有 source 才能让导出的变量留在当前终端。每开一个新 shell（或重新 attach）都要重做一次。

**② build.sh 的 `set_env()` 函数**（[ascendc/build.sh:L72-L82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L72-L82)）：

```bash
function set_env()
{
    source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash || echo "0"
    export BISHENG_REAL_PATH=$(which bisheng || true)
    if [ -z "${BISHENG_REAL_PATH}" ];then
        log "Error: bisheng compilation tool not found, Please check whether the cann package or environment variables are set."
        exit 1
    fi
}
```

这段做了两件事：先 source CANN 自带的 `setenv.bash` 补全编译环境，再确认 `bisheng`（Ascend C 的编译器）能被 `which` 找到，找不到直接终止并提示检查 CANN 包与环境变量。它由主流程在清理构建目录后调用（[ascendc/build.sh:L438](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L438)）。

**③ `ASCEND_CANN_PACKAGE_PATH` 的五级解析**（[ascendc/build.sh:L407-L420](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L407-L420)）：

```bash
if [ -n "${ascend_package_path}" ];then          # ① 命令行 -p 优先
    ASCEND_CANN_PACKAGE_PATH=${ascend_package_path}
elif [ -n "${ASCEND_HOME_PATH}" ];then           # ② set_env.sh 导出的变量
    ASCEND_CANN_PACKAGE_PATH=${ASCEND_HOME_PATH}
elif [ -n "${ASCEND_OPP_PATH}" ];then            # ③ 由 OPP 路径反推
    ASCEND_CANN_PACKAGE_PATH=$(dirname ${ASCEND_OPP_PATH})
elif [ -d "${DEFAULT_TOOLKIT_INSTALL_DIR}" ];then # ④ root 默认路径存在
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_TOOLKIT_INSTALL_DIR}
elif [ -d "${DEFAULT_INSTALL_DIR}" ];then        # ⑤ 非 root 默认路径
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_INSTALL_DIR}
else
    log "Error: Please set the toolkit package installation directory through parameter -p|--package-path."
    exit 1
fi
```

而 ④⑤ 中的默认路径在脚本开头按用户身份二选一（[ascendc/build.sh:L31-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L31-L37)）：root 用 `/usr/local/Ascend/ascend-toolkit/latest`，普通用户用 `~/Ascend/ascend-toolkit/latest`。解析出的路径连同兼容性开关一起传给 CMake（[ascendc/build.sh:L432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L432)：`-DCUSTOM_ASCEND_CANN_PACKAGE_PATH=... -DCHECK_COMPATIBLE=...`）。

**④ 与版本校验的关系**：如果使用"非标准镜像"里不配套的 CANN 包，README 的 FAQ 建议编译时加 `--disable-check-compatible` 跳过版本校验（[ascendc/README.md:L275-L277](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L275-L277)）。该参数在 build.sh 中的落点就是把 `CHECK_COMPATIBLE` 置为 false（[ascendc/build.sh:L303-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L303-L306)）。优先用配套镜像，跳过校验只是应急手段。

**⑤ 预告：第二个 set_env**。编译出的算子包安装后，README 还要求再 source 一次 `.../opp/vendors/omni_training_custom_transformer/bin/set_env.bash`（[ascendc/README.md:L253-L264](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L253-L264)），让运行时能找到刚安装的自定义算子。这属于 u1-l4 的内容，本讲只需知道"环境变量初始化在装完算子包后还有一轮"。

#### 4.3.4 代码实践

**实践目标**：验证环境变量链是否打通——这是能否进入 u1-l4 编译环节的体检项。

**操作步骤**（在容器内执行，示例命令）：

1. `source /usr/local/Ascend/ascend-toolkit/set_env.sh`
2. `echo $ASCEND_HOME_PATH` —— 应输出 toolkit 安装路径；
3. `which bisheng` —— 应输出 bisheng 的完整路径（build.sh 检查的就是它）；
4. `cd /home/code/<你的仓库>/training/ascendc && bash build.sh -h` —— 查看帮助，确认脚本可用且能看到 `-c` 默认值为 `ascend910_93`（[ascendc/build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L46-L65)）；
5. 反向实验：新开一个 shell **不** source set_env.sh，直接执行 `echo $ASCEND_HOME_PATH`，观察输出为空。

**需要观察的现象**：

- 第 2/3 步有非空输出；第 4 步打印 Usage 帮助文本；
- 第 5 步输出为空——证明这些变量只活在 source 过的 shell 里。

**预期结果**：环境链打通，`build.sh -h` 正常输出。无 NPU 环境时第 1~4 步无法执行；第 5 步的现象在任何 Linux 机器上都可复现（source 的语义与是否装 CANN 无关）。容器内具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bash /usr/local/Ascend/ascend-toolkit/set_env.sh` 不起作用，必须 `source`？

答案：`bash xxx.sh` 会启动一个子 shell 执行脚本，脚本里 `export` 的变量随子 shell 退出而消失，当前终端一无所获；`source` 在当前 shell 内逐条执行，导出的变量留在当前会话。这也是为什么每开新终端都要重新 source。

**练习 2**：不执行任何 source，直接 `bash build.sh`，按 4.3.2 的解析链会发生什么？

答案：build.sh 并不必然失败——它会沿五级链继续找：`ASCEND_HOME_PATH` 为空、`ASCEND_OPP_PATH` 为空，则尝试默认路径；在 root 容器里 `/usr/local/Ascend/ascend-toolkit/latest` 通常存在（镜像内已装 CANN），于是 `ASCEND_CANN_PACKAGE_PATH` 落到默认路径，随后 `set_env()` source 其 `bin/setenv.bash` 仍可能把环境补起来。但如果 CANN 装在非默认位置且未 source，五级全落空，脚本报 "Please set the toolkit package installation directory through parameter -p|--package-path" 并退出。结论：README 让你先 source，是为了显式固定路径来源、不依赖默认值侥幸命中。

**练习 3**：`which bisheng` 没有输出，build.sh 会怎样？你该按什么顺序排查？

答案：build.sh 的 `set_env()` 检测到 `BISHENG_REAL_PATH` 为空即打印 "bisheng compilation tool not found" 并 `exit 1`，编译不会开始。排查顺序：(1) 是否 source 过 `set_env.sh`；(2) `echo $ASCEND_HOME_PATH` 是否正确指向 toolkit 目录；(3) 该目录的 `bin/` 下是否存在 bisheng（即 CANN 是否完整安装）；(4) 必要时用 `build.sh -p <路径>` 显式指定安装路径。

## 5. 综合实践

把本讲三个模块串成一个可复用的 `setup_env.sh`（以下为**示例代码**，非仓库原有文件，请保存在教程练习目录而非源码目录）：

```bash
#!/bin/bash
# setup_env.sh —— openPangu 2.0 训练算子库环境一键脚本（示例代码）
# 用法：
#   bash setup_env.sh pull  a3                 # 拉取 A3 镜像
#   bash setup_env.sh run   a3 8               # 创建 8 卡容器（卡数可选 8/16）
#   bash setup_env.sh enter                    # 进入容器
#   bash setup_env.sh env                      # 容器内执行：初始化 CANN 环境变量
set -e

IMAGE_A2="swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a2:cann8.5.0-torch_npu2.9.0-20260130"
IMAGE_A3="swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130"
IMAGE_A5="swr.cn-east-4.myhuaweicloud.com/omni/base-arm-openeuler-py311-a5:cann9.0.0-torch_npu2.9.0.post2-20260506153635"
CONTAINER_NAME="omni_ops_training"    # README 默认容器名

pick_image() {  # 代际 → 镜像
    case "$1" in
        a2) echo "$IMAGE_A2" ;;
        a3) echo "$IMAGE_A3" ;;
        a5) echo "$IMAGE_A5" ;;
        *) echo "未知代际: $1（可选 a2/a3/a5）" >&2; exit 1 ;;
    esac
}

case "$1" in
pull)  # 模块①：镜像拉取
    docker pull "$(pick_image "$2")"
    ;;
run)   # 模块②：创建容器。$2=代际 $3=卡数
    SOC="$2"; CARDS="${3:-8}"
    [ "$CARDS" = "8" ] || [ "$CARDS" = "16" ] || { echo "卡数仅支持 8/16" >&2; exit 1; }

    DEV_ARGS=""
    for i in $(seq 0 $((CARDS - 1))); do          # 只透传前 N 张卡
        DEV_ARGS="$DEV_ARGS --device=/dev/davinci$i"
    done

    docker run -u root -itd --name "$CONTAINER_NAME" \
        --ulimit nproc=65535:65535 --ipc=host \
        $DEV_ARGS \
        --device=/dev/davinci_manager --device=/dev/devmm_svm \
        --device=/dev/hisi_hdc \
        -v /home/:/home \
        -v /data:/data \
        -v /etc/localtime:/etc/localtime \
        -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
        -v /etc/ascend_install.info:/etc/ascend_install.info \
        -v /var/log/npu/:/usr/slog \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /sys/fs/cgroup:/sys/fs/cgroup:ro \
        -v /usr/local/dcmi:/usr/local/dcmi -v /usr/local/sbin:/usr/local/sbin \
        -v /etc/hccn.conf:/etc/hccn.conf -v /root/.pip:/root/.pip \
        -v /etc/hosts:/etc/hosts -v /usr/bin/hostname:/usr/bin/hostname \
        --net=host --shm-size=128g --privileged \
        "$(pick_image "$SOC")" /bin/bash
    ;;
enter)  # 进入容器（用 Ctrl-p Ctrl-q 分离，勿用 exit）
    docker attach "$CONTAINER_NAME"
    ;;
env)   # 模块③：容器内初始化 CANN 环境变量并自检
    source /usr/local/Ascend/ascend-toolkit/set_env.sh
    echo "ASCEND_HOME_PATH = $ASCEND_HOME_PATH"
    echo "bisheng          = $(which bisheng)"
    ;;
*)
    echo "用法: bash setup_env.sh {pull a2|a3|a5 | run a2|a3|a5 [8|16] | enter | env}" >&2
    exit 1
    ;;
esac
```

要求完成：

1. **语法校验（任何机器可做）**：`bash -n setup_env.sh`，无输出即通过；
2. **逐条注释核对**：对照 [ascendc/README.md:L179-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L179-L207) 检查脚本中每个挂载卷是否与 README 一致，并在脚本里为每个 `-v` 补一行注释说明其用途（参考 4.2.3 的表格）；
3. **有 NPU 环境时**：依次执行 `pull a3` → `run a3 8` → `enter` → （容器内）`bash setup_env.sh env`，确认 `env` 子命令输出的两个变量非空；
4. **回答思考题**：如果要在同一台机器上同时保留 A2 与 A5 两套环境，脚本需要改哪些地方？（提示：`CONTAINER_NAME` 与镜像一一对应。）

预期结果：`bash -n` 通过；有 NPU 时容器启动且 `env` 自检输出非空路径。无 NPU 环境时完成第 1、2、4 步即可，第 3 步**待本地验证**。

## 6. 本讲小结

- 昇腾算子开发环境分三层：**宿主机驱动 + 容器内 CANN 工具链 + torch_npu**，驱动必须留在宿主机，工具链放进镜像，docker run 脚本里的大量挂载就是为打通两者。
- 镜像按芯片代际三选一：A2/A3 用 cann8.5.0-torch_npu2.9.0，A5 用 cann9.0.0-torch_npu2.9.0.post2；镜像代际要与编译参数 `-c` 的 soc_version（`ascend910b` / `ascend910_93` / `ascend950`）匹配，其中 A3 → `ascend910_93` 由 README 示例直接确认。
- docker run 的关键三类参数：`--device=/dev/davinci*` 透传 NPU 设备节点、`-v .../driver` 等挂载宿主机驱动与管理工具、`--ipc=host --shm-size=128g --net=host` 保障多卡训练通信；8 卡机器删掉多余 `--device` 行即可。
- 进容器后必须 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`，且用 `source` 而非 `bash`，每开新终端都要重来。
- `build.sh` 对环境的依赖有明确落点：五级优先链解析 `ASCEND_CANN_PACKAGE_PATH`（`-p` > `ASCEND_HOME_PATH` > `ASCEND_OPP_PATH` > root/非 root 默认目录），再由 `set_env()` source `setenv.bash` 并强校验 `bisheng` 存在，找不到即退出。
- 环境异常时的两个应急线索：非配套 CANN 用 `--disable-check-compatible` 跳过版本校验（README FAQ）；装完算子包后还有第二轮 `set_env.bash` 要 source（u1-l4 详述）。

## 7. 下一步学习建议

环境就绪后，下一讲 **u1-l4《编译与安装：build.sh、CMake 与自定义算子 run 包》**将沿着本讲打通的环境走完第一条完整链路：`build.sh -c <soc> -n <算子名>` 的参数解析与 `clean → cmake_config → build_package` 流程、`CMakeLists.txt` / `cmake/config.cmake` 的工程组织、以及把产物 run 包安装到 `opp/vendors` 并 source 第二个 `set_env.bash`。建议提前浏览 [ascendc/build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L46-L65) 的帮助文本和 [ascendc/README.md:L219-L273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L219-L273) 的「编译执行」章节，带着"我容器里这条命令会发生什么"的问题去读。若你对芯片适配的更多细节好奇，可以在学完 u1-l4 后回看本讲的 `AddConfig` 引用，它将在 u9-l1（多芯片适配）中展开。
