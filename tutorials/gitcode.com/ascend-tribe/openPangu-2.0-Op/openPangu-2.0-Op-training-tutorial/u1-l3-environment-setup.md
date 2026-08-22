# 开发环境搭建：镜像、容器与 CANN 环境变量

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚昇腾 NPU 开发环境的三大组成部分——宿主机驱动、容器内的 CANN 工具链、torch_npu 框架适配层——以及为什么本项目推荐用 docker 镜像开发。
2. 根据手上机器的芯片类型（A2 / A3 / A5），从 `ascendc/README.md` 中选出正确的 docker 镜像并完成 `docker pull`。
3. 逐行读懂 README 给出的 `docker run` 参考脚本：为什么必须挂载 `/dev/davinci*` 设备节点和宿主机驱动目录，并能把它从 16 卡改写成 8 卡版本。
4. 在容器内完成 CANN 环境变量初始化（`source set_env.sh`），并顺着 `build.sh` 的源码看清这些环境变量是如何被消费的。

本讲不涉及任何算子代码修改，所有操作都只发生在你的开发机、docker 容器和 shell 环境里。这是后续一切编译（u1-l4）与测试（单元 8）的地基。

## 2. 前置知识

阅读本讲前，建议先完成 u1-l1（项目全景）和 u1-l2（算子四层模型）。此外需要了解以下基础概念：

- **昇腾 NPU**：华为的 AI 加速芯片。本仓库的 Ascend C 算子就跑在这类芯片上。开发机上每插一张 NPU 卡，操作系统中就会出现一个 `/dev/davinciN` 设备文件（N 从 0 开始编号）。
- **CANN（Compute Architecture for Neural Networks）**：昇腾的软件栈，可以类比成 NPU 版的 "CUDA Toolkit"。它包含：
  - **算子编译工具链**：把 `op_kernel` 目录下的 Ascend C 源码编译成芯片可执行指令，其中的编译器叫 `bisheng`（毕昇）；
  - **运行时（Runtime）**：Host 侧的 aclnn 接口最终通过它把任务下发到设备；
  - **opp 算子库目录**：编译出的自定义算子包最终安装到 `/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/` 下（u1-l4 详述安装过程）。
- **torch_npu**：PyTorch 的昇腾后端适配包，作用类似 GPU 上的 `torch.cuda`，让 `torch.Tensor` 能搬到 NPU 上运算。它与 CANN 版本必须严格配套——这正是镜像 tag 里把两个版本号写死的原因。
- **docker 基础**：会 `docker pull`（拉镜像）、`docker run`（创建并启动容器）、`docker ps`（列容器）即可。两个关键语法：`-v 宿主机路径:容器路径` 把宿主机目录挂载进容器；`--device=/dev/xxx` 把宿主机设备文件透传给容器。
- **`source` 与环境变量**：`source xxx.sh` 在**当前 shell** 里逐条执行脚本，因此脚本里 `export` 的变量在执行后仍然生效；而 `bash xxx.sh` 会开一个子 shell，变量随子 shell 一起消失。这就是 `set_env.sh` 必须 `source` 而不能 `bash` 执行的原因。

一个最容易混淆的点先澄清：**驱动装在宿主机，工具链装在容器里**。NPU 驱动必须直接接触硬件内核，所以属于宿主机；CANN、torch_npu 这些用户态软件放在镜像里，换版本只需换镜像，互不污染。`docker run` 脚本里大量挂载卷的存在，就是为了让容器"够得着"宿主机的驱动与管理设施。

## 3. 本讲源码地图

本讲涉及的源码文件很少，但每一处都要精读：

| 文件 | 作用 |
| --- | --- |
| `ascendc/README.md` | 项目官方说明书。第 162~217 行的「环境准备」章节是本讲主线：下载源码 → 拉镜像 → 起容器 → 设环境变量 |
| `ascendc/build.sh` | 编译入口脚本。编译全流程属于 u1-l4，但它**如何在环境里寻找 CANN**（`ASCEND_CANN_PACKAGE_PATH` 的五级解析、`set_env()` 检查 `bisheng`）属于本讲"环境变量"主题 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | 只看第 83~84 行的 `AddConfig("ascend910b"/"ascend910_93")`，用来印证 `-c` 参数里的芯片代号从哪来 |
| `ascendc/cmake/scripts/prepare.sh` | CMake 阶段的准备脚本，第 98~106 行的 `set_env()` 展示变量如何继续传给 cmake（本讲只作旁证） |

注意：`set_env.sh` 与 `set_env.bash` 本身**不在仓库里**——它们由 CANN 安装包生成，位于容器内 `/usr/local/Ascend/ascend-toolkit/` 与算子包安装目录下。本讲引用的是 README 中对它们的调用方式，以及 `build.sh` 中消费环境变量的真实代码。

## 4. 核心概念与源码讲解

### 4.1 环境准备总览与镜像选择：A2 / A3 / A5

#### 4.1.1 概念说明

README 把「环境准备」分成四步：下载源码、获取 docker 镜像、拉起容器、设置环境变量。其中最关键的选择是**镜像**——昇腾训练芯片有多个代际，README 按芯片分成 A2、A3、A5 三类，各自提供一份开源镜像。

选错镜像的后果非常实际：CANN 版本与芯片不配套时，算子要么编译不过，要么运行时 `socVersion` 校验失败。

镜像名本身就是一份"配料表"，以 A3 为例：

```
swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130
└────────── 镜像仓库地址 └──── 镜像名 ────┘└──── tag：CANN 与 torch_npu 版本 ────┘
```

- `arm`：CPU 架构是 ARM（鲲鹏/飞腾服务器）。在 x86 机器上拉了也跑不起来；
- `openeuler-py311`：操作系统 openEuler，内置 Python 3.11；
- `a2` / `a3` / `a5`：芯片代际标识；
- tag `cann8.5.0-torch_npu2.9.0`：CANN 8.5.0 配 torch_npu 2.9.0，这是一对**严格配套**的版本组合（A5 镜像是 `cann9.0.0` 配 `torch_npu2.9.0.post2`）。

芯片代际与 `build.sh -c` 参数里 `soc_version` 的对应关系：README 在拉起容器（用 A3 镜像）之后紧接着说"下面以 A3 环境举例"并给出 `bash build.sh -c ascend910_93`，可以确认 **A3 ↔ ascend910_93**。按同样规律，A2 对应 `ascend910b`、A5 对应 `ascend950`（README 第 223 行列出这三个合法值；A2/A5 的直接对应关系为推断，**待确认**）。

#### 4.1.2 核心流程

环境搭建总流程：

1. **下载源码**：`git clone` 仓库并进入 `training/ascendc` 目录；
2. **拉镜像**：按机器芯片类型 `docker pull` 对应的 A2/A3/A5 镜像；
3. **起容器**：用 README 的 `docker run` 脚本创建容器（4.2 节精读）；
4. **进容器**：`docker attach omni_ops_training`；
5. **设环境变量**：`source /usr/local/Ascend/ascend-toolkit/set_env.sh`（4.3 节精读）。

#### 4.1.3 源码精读

「环境准备」章节的入口在 [ascendc/README.md:L162-L170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L162-L170)：README 先给出下载源码的命令。注意一个细节——README 里的 clone 地址是 `gitcode.com:cann/omni-ops.git`，而你现在读的仓库是 `gitcode.com/ascend-tribe/openPangu-2.0-Op`；两者的 `training/ascendc` 目录结构一致，本文所有路径均以当前仓库为准，执行 README 命令时把仓库地址换成当前仓库即可。

三条镜像拉取命令在 [ascendc/README.md:L172-L178](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L172-L178)：README 明确说明"镜像是开源的，可以直接通过 docker pull 拉取对应 soc 的镜像"，并列出 A2（`cann8.5.0-torch_npu2.9.0`）、A3（`cann8.5.0-torch_npu2.9.0`）、A5（`cann9.0.0-torch_npu2.9.0.post2`）三行命令。三者的差异只有两处：镜像名里的 `a2/a3/a5` 与 tag 里的 CANN/torch_npu 版本。

`-c` 参数的合法取值在 [ascendc/build.sh:L55-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L55-L56) 的 usage 说明里也得到印证：`-c|--compute-unit` 指定芯片类型，**默认值就是 `ascend910_93`**，示例给出 `ascend910_93` 与 `ascend910b` 两种。这也解释了为什么后文所有 `bash build.sh` 示例都要显式带 `-c`——避免在错误代际的芯片上编译。

芯片代际在算子源码里的落点是 `AddConfig`，见 [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:L83-L84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L84)：算子原型同时为 `ascend910b` 与 `ascend910_93` 注册 AICore 配置。也就是说，你选的镜像决定了 `-c` 能填什么，而 `-c` 填的值必须与算子 `AddConfig` 注册过的芯片名对得上，否则该算子不可编译（详见 u9-l1 的多芯适配主题）。

#### 4.1.4 代码实践

**实践目标**：确认自己该用哪份镜像，并理解镜像 tag 里的版本配套关系。

**操作步骤**：

1. 在宿主机执行 `npu-smi info`（若宿主机装了 NPU 管理工具），确认芯片型号与卡数；若无法确认，向集群管理员询问机器是 A2、A3 还是 A5；
2. 按下表选择镜像并拉取（以 A3 为例）：

   ```bash
   docker pull swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130
   ```

3. 拉取完成后用 `docker images | grep omni` 确认镜像已在本地。

**需要观察的现象**：`docker pull` 会分层输出下载进度，最后给出镜像摘要（DIGEST）；`docker images` 能看到完整镜像名与 tag。

**预期结果**：本地镜像列表中出现所选的 `sub_base-arm-openeuler-py311-aX` 镜像。若宿主机是 x86 架构，pull 可以成功但 run 时会报 `exec format error`——这说明确实需要 ARM 服务器。本实践需要真实环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：A5 镜像的 tag 是 `cann9.0.0-torch_npu2.9.0.post2-20260506153635`，其中 `post2` 和末尾长数字分别可能是什么信息？

**答案**：`post2` 是 torch_npu 2.9.0 的第二次后缀修订版（post-release），说明它对 2.9.0 打了补丁但仍算 2.9.0 系列；末尾的 `20260506153635` 形如时间戳（2026-05-06 15:36:35），是镜像构建日期，用来区分同版本的多次构建。依据：tag 命名惯例与另外两个镜像 tag 末尾的 `20260130`（短日期）格式一致；具体语义官方未在 README 中说明，属于合理推断。

**练习 2**：为什么 A3 镜像不能用 `--disable-check-compatible` 之外的方式在 A2 机器上编译算子？

**答案**：编译期兼容性校验会核对 CANN 包版本与目标芯片是否配套，A2 与 A3 对应的 `soc_version` 不同（`ascend910b` 与 `ascend910_93`），算子 `AddConfig` 注册的芯片集合也按代际区分；强行跳过校验（README 第 277 行 FAQ 提到 `--disable-check-compatible`）只是绕过提示，编出的产物在错误芯片上仍无法运行。FAQ 原文位置：[ascendc/README.md:L275-L277](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L275-L277)。

### 4.2 docker run 参考脚本逐行精读：设备透传与挂载卷

#### 4.2.1 概念说明

README 给出的 `docker run` 脚本长约 24 行，初看吓人，其实只有四类参数：

1. **运行方式**：`-u root -itd`（root 用户、交互式、后台运行）；
2. **设备透传**：`--device=/dev/davinci0` ~ `davinci15` 共 16 张卡，外加 3 个管理设备；
3. **目录挂载**：约 15 个 `-v` 卷，把宿主机的驱动、管理工具、配置、日志、数据盘接进容器；
4. **系统选项**：`--net=host`、`--shm-size=128g`、`--privileged`、`--ipc=host` 等。

README 本身没有逐条注释这些挂载，下面按路径语义与昇腾通用实践解读（这是教学解读，不是 README 原文）。

#### 4.2.2 核心流程

一个 NPU 容器要"活起来"，必须打通四条通道：

```text
① 计算通道   /dev/davinci0..15        每张卡一个字符设备，算子真正读写的地方
② 管理通道   /dev/davinci_manager      NPU 总管理设备
             /dev/devmm_svm            设备内存管理（含共享虚拟内存 SVM）
             /dev/hisi_hdc             Host-Device 通信通道（HDC）
③ 驱动通道   /usr/local/Ascend/driver  宿主机 NPU 驱动本体
             /etc/ascend_install.info  驱动安装信息（版本探测用）
④ 运维通道   npu-smi / dcmi / slog / hccn.conf / localtime 等管理工具与配置
```

容器启动后，镜像内的 CANN 通过 ③ 找到驱动、通过 ①② 操作硬件、通过 ④ 观测状态与网络互联。

#### 4.2.3 源码精读

完整的 `docker run` 脚本在 [ascendc/README.md:L179-L207](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L179-L207)，默认容器名 `omni_ops_training`。关键行摘录：

```bash
docker run -u root -itd --name omni_ops_training --ulimit nproc=65535:65535 --ipc=host \
    --device=/dev/davinci0     --device=/dev/davinci1 \
    ...（共 davinci0~davinci15 十六张卡）...
    --device=/dev/davinci_manager --device=/dev/devmm_svm \
    --device=/dev/hisi_hdc \
    -v /home/:/home \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /etc/ascend_install.info:/etc/ascend_install.info -v /var/log/npu/:/usr/slog \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi ...
    --net=host --shm-size=128g --privileged \
    swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130 /bin/bash
```

几个值得单独指出的参数：

- `-v /home/:/home`（第 194 行）：把整个 `/home` 原样挂进容器。README 的源码就放在 `/home/code/` 下，这样容器内外看到同一份代码，改代码不用同步；
- `-v /usr/local/Ascend/driver:...`（第 197 行）：**最关键的一行**。容器里装的是 CANN 工具链，而驱动必须用宿主机的——这一行让容器内的 CANN 能链接到真实驱动；
- `-v /etc/ascend_install.info:...`（第 198 行）：CANN 启动时会读这个文件探测驱动版本，缺少它常报"驱动不匹配"；
- `--ipc=host` 与 `--shm-size=128g`：PyTorch DataLoader 多进程靠共享内存传数据，这两项保证训练脚本不会被共享内存卡死；
- `--privileged`：特权模式，确保容器对透传进来的设备文件有完整操作权限；
- 末尾的镜像名决定整个环境代际——README 示例用的是 A3 镜像，与前文"以 A3 环境举例"呼应。

进入容器的命令在 [ascendc/README.md:L208-L211](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L208-L211)：`docker attach omni_ops_training`。注意 `attach` 是直接接到容器主进程的 shell；如果你习惯 `docker exec -it omni_ops_training bash` 开第二个 shell，效果等同且更安全（attach 退出时若误按 `Ctrl-P Ctrl-Q` 以外的方式 detach/exit，可能连容器一起停掉）。

#### 4.2.4 代码实践

**实践目标**：把 README 的 16 卡脚本改写成可配置卡数的 8 卡版本，并理解每一处删改的影响。

**操作步骤**：

1. 复制 README 第 183~206 行脚本到本地文件 `run_8card.sh`；
2. 删除 `--device=/dev/davinci8` 到 `--device=/dev/davinci15` 共 8 行，只保留 `davinci0` ~ `davinci7`；
3. 其余参数（3 个管理设备、全部 `-v` 挂载、`--net=host` 等）**一个都不动**；
4. 在有 docker 的机器上执行 `bash -n run_8card.sh` 校验续行符 `\` 没有抄错。

**需要观察的现象**：`bash -n` 无输出即语法正确；实际执行后 `docker ps` 能看到 `omni_ops_training` 处于 `Up` 状态。

**预期结果**：容器内 `ls /dev/davinci*` 只能看到 0~7 八个设备节点。若误删了 `--device=/dev/davinci_manager`，容器能启动但任何 NPU 操作都会失败——这正是"计算设备与管理设备缺一不可"的直接验证。本实践需要真实 NPU 宿主机，**待本地验证**（无 docker 环境时至少完成 `bash -n` 语法校验）。

#### 4.2.5 小练习与答案

**练习 1**：`--shm-size=128g` 去掉会发生什么？为什么 README 要显式写它？

**答案**：docker 默认 `/dev/shm` 只有 64MB，PyTorch DataLoader 的 worker 进程通过共享内存回传 batch 数据，超限会报 "DataLoader worker exited unexpectedly" 一类错误。昇腾分布式训练的通信与数据搬运对共享内存需求更大，所以显式放大到 128g。这是 PyTorch/容器通用知识，与 NPU 无关，但训练脚本必备。

**练习 2**：脚本里 `-v /var/log/npu/:/usr/slog` 把宿主机 NPU 日志目录挂成了容器内的 `/usr/slog`。为什么不直接挂到同路径？

**答案**：容器内的 CANN/驱动组件按约定路径 `/usr/slog` 写日志，而宿主机上该目录是 `/var/log/npu/`。挂载源路径与目标路径可以不同，正好完成"容器内约定路径 → 宿主机实际路径"的映射，宿主机运维与容器内组件各看各的熟悉路径，互不干扰。

**练习 3**：`-itd` 三个字母各是什么意思？去掉 `d` 会怎样？

**答案**：`-i` 保持标准输入打开，`-t` 分配伪终端，两者合用才能得到一个可交互的 shell；`-d` 让容器在后台运行。去掉 `d`，容器会占住当前终端，一旦退出 shell（exit），`/bin/bash` 主进程结束，容器随之停止。README 用 `-itd` + `docker attach` 的组合，就是"先后台起、再连进去"。

### 4.3 容器内 CANN 环境变量初始化：set_env.sh 的调用与消费

#### 4.3.1 概念说明

进了容器只是"人进去了"，CANN 还没"上线"。CANN 的二进制、库、头文件分散在 `/usr/local/Ascend/ascend-toolkit/` 的多个子目录里，靠一组环境变量串起来：

- `ASCEND_HOME_PATH`：CANN 安装根（如 `/usr/local/Ascend/ascend-toolkit/latest`）；
- `ASCEND_OPP_PATH`：算子库 opp 目录；
- `PATH` / `LD_LIBRARY_PATH`：让 shell 找得到 CANN 工具、让动态链接器找得到 `.so`。

`set_env.sh` 就是把这些变量一次性 `export` 进当前 shell 的脚本（由 CANN 安装包生成，不在本仓库中）。它必须 `source` 执行——用 `bash` 跑等于在子 shell 里 export，脚本一结束变量全没了。

环境变量不是终点，而是**被 build 脚本消费的输入**。`build.sh` 会沿着一串线索找到 CANN 包路径，然后 `source` 它的 `setenv.bash`、检查 `bisheng` 编译器是否存在。

#### 4.3.2 核心流程

环境变量从产生到消费的完整链路：

```text
容器内 shell
  └─ source /usr/local/Ascend/ascend-toolkit/set_env.sh     # ① 产生 ASCEND_HOME_PATH 等变量
       │
       ▼
bash build.sh ...
  ├─ ② 解析 ASCEND_CANN_PACKAGE_PATH（五级 fallback，见 4.3.3）
  ├─ ③ set_env()：source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash
  │       └─ 用 which bisheng 检查编译器，找不到直接报错退出
  └─ ④ 把路径写进 cmake 参数 -DCUSTOM_ASCEND_CANN_PACKAGE_PATH=...
```

#### 4.3.3 源码精读

README 中设置环境变量的一行命令在 [ascendc/README.md:L213-L217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L213-L217)：`source /usr/local/Ascend/ascend-toolkit/set_env.sh`。这是**每次新开 shell 都要做**的事——`docker attach` 重新连入、`docker exec` 新开终端，环境变量都不会自动继承。

`build.sh` 消费环境变量的第一步是五级 fallback 解析，见 [ascendc/build.sh:L407-L420](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L407-L420)：

```bash
if [ -n "${ascend_package_path}" ];then          # ① 命令行 -p|--package-path 显式指定
    ASCEND_CANN_PACKAGE_PATH=${ascend_package_path}
elif [ -n "${ASCEND_HOME_PATH}" ];then           # ② set_env.sh 导出的变量（最常命中）
    ASCEND_CANN_PACKAGE_PATH=${ASCEND_HOME_PATH}
elif [ -n "${ASCEND_OPP_PATH}" ];then            # ③ 由 OPP 路径反推上一级
    ASCEND_CANN_PACKAGE_PATH=$(dirname ${ASCEND_OPP_PATH})
elif [ -d "${DEFAULT_TOOLKIT_INSTALL_DIR}" ];then  # ④ 默认安装路径存在
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_TOOLKIT_INSTALL_DIR}
elif [ -d "${DEFAULT_INSTALL_DIR}" ];then        # ⑤ 兜底默认路径
    ASCEND_CANN_PACKAGE_PATH=${DEFAULT_INSTALL_DIR}
else
    log "Error: Please set the toolkit package installation directory through parameter -p|--package-path."
    exit 1
fi
```

两个默认目录的定义在 [ascendc/build.sh:L32-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L32-L36)：普通用户是 `~/Ascend/ascend-toolkit/latest`，root 用户是 `/usr/local/Ascend/ascend-toolkit/latest`。这也解释了一个现象：**容器里以 root 操作时，即使忘了 source，只要 CANN 装在默认路径，第 ④ 级也能救回来**——但规范做法仍然是先 `source set_env.sh`，让第 ② 级命中，避免多版本 CANN 共存时找错包。

第二步是 `set_env()` 函数，见 [ascendc/build.sh:L72-L82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L72-L82)：

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

它先 source CANN 包内的 `setenv.bash`（注意与 README 让你 source 的 `set_env.sh` 是**两个不同的脚本**：前者在具体版本包里、由 build.sh 自动调用，后者在 `ascend-toolkit/` 顶层、由人手动调用），再用 `which bisheng` 验证毕昇编译器已进入 `PATH`。找不到就直接 `exit 1`——这是"环境没配好"最常见的第一道报错。

第三步，解析出的路径最终传给 cmake，见 [ascendc/build.sh:L432](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L432)：`-DCUSTOM_ASCEND_CANN_PACKAGE_PATH=${ASCEND_CANN_PACKAGE_PATH}`，从此进入 CMake 世界（u1-l4 的内容）。旁证一例：cmake 阶段同样把这个变量继续下传，见 [ascendc/cmake/scripts/prepare.sh:L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/scripts/prepare.sh#L113)。

此外还有一个"第二份"环境脚本：自定义算子包装好之后，README 要求再 source 一次 vendors 下的 `set_env.bash`，见 [ascendc/README.md:L258-L264](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L258-L264)（`/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/bin/set_env.bash`）。它的作用是把**新装的自定义算子包**注册进运行时查找路径——没有它，aclnn 接口找不到你刚编译的算子。这属于 u1-l4 的安装环节，这里先记住"装完包还有第二次 source"。

#### 4.3.4 代码实践

**实践目标**：在容器内完成 CANN 环境变量初始化，亲眼看到 `set_env.sh` 导出了哪些变量。

**操作步骤**：

1. `docker attach omni_ops_training` 进入容器；
2. 先看初始状态：`env | grep -i ascend`（大概率是空的或只有少量变量）；
3. 执行 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`；
4. 再次执行 `env | grep -i ascend`，对比前后差异；
5. 验证编译器可见：`which bisheng`。

**需要观察的现象**：source 之后多出 `ASCEND_HOME_PATH`、`ASCEND_OPP_PATH` 等变量，`PATH` 与 `LD_LIBRARY_PATH` 中出现 `ascend-toolkit` 相关路径；`which bisheng` 输出一个真实路径。

**预期结果**：与 `build.sh` 第 ② 级 fallback 呼应——`ASCEND_HOME_PATH` 非空时，`ASCEND_CANN_PACKAGE_PATH` 就取它。如果 `which bisheng` 为空，说明 source 的 CANN 与镜像不配套，这正是 build.sh L79 报错的场景。本实践需要真实容器环境，**待本地验证**；无 NPU 环境时可改做源码阅读实践——对照 build.sh L407-L420 写出五级 fallback 的命中顺序表，并标注每一级依赖的变量由谁设置（命令行参数 / set_env.sh / 系统默认）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `source set_env.sh` 之后新开一个 `docker exec` 终端，环境变量又没了？

**答案**：环境变量是 shell 进程的属性。`source` 只影响当前 shell 进程及其子进程；`docker exec` 启动的是一个全新 shell，不继承之前那个 shell 的变量。解决办法：每次新终端重新 source，或把 source 命令写进容器内 `~/.bashrc`。

**练习 2**：`build.sh` 的 `set_env()`（L72-L82）里有一行 `source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash || echo "0"`，为什么 source 失败只是 echo "0" 而不直接退出？

**答案**：`|| echo "0"` 把失败"吞"掉继续执行，真正的安全网在后面：接下来用 `which bisheng` 判断编译器是否可用，找不到才 `exit 1`。也就是说 build.sh 关心的不是 setenv.bash 本身是否执行成功，而是它的**最终效果**（bisheng 进 PATH）。若 setenv.bash 失败但 bisheng 恰好已在 PATH 里（比如外层已手动 source 过），编译仍能继续，这是一种宽松兜底策略。

**练习 3**：README 让你 source 的 `set_env.sh`、算子包安装后的 `set_env.bash`、build.sh 内部 source 的 `setenv.bash`，三者分别位于哪里、各由谁调用？

**答案**：① `/usr/local/Ascend/ascend-toolkit/set_env.sh`——CANN 顶层环境脚本，人手动 source（README L216）；② `.../opp/vendors/omni_training_custom_transformer/bin/set_env.bash`——自定义算子包安装后生成，人手动 source（README L261），作用是把新算子注册进运行时；③ `$ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash`——CANN 版本包内脚本，由 `build.sh` 的 `set_env()` 函数自动调用（L74）。三者名字相似、角色不同：前两个面向"运行/调用算子"，第三个面向"编译算子"。

### 4.4 环境自检：确认驱动、CANN 与 torch_npu 三层就绪

#### 4.4.1 概念说明

环境搭好后不要急着编译，先做一次分层自检。回顾第 2 节的三层结构，每一层各有一个检查点：

| 层 | 检查命令 | 验证什么 |
| --- | --- | --- |
| 宿主机驱动（经挂载进入容器） | `npu-smi info` | 设备透传与驱动挂载是否打通 |
| CANN 工具链 | `which bisheng` | set_env.sh 是否生效、编译器是否在 PATH |
| torch_npu | `python -c "import torch; import torch_npu; print(torch_npu.__version__)"` | 框架适配层是否与镜像配套 |

#### 4.4.2 核心流程

```text
npu-smi info 正常？──否──> 检查 docker run 的 --device 与 /usr/local/Ascend/driver 挂载
      │是
which bisheng 有输出？──否──> 重新 source set_env.sh；仍失败则检查镜像 CANN 版本
      │是
import torch_npu 成功？──否──> 检查镜像 tag 的 torch_npu 版本与 python 版本
      │是
环境就绪，可以进入 u1-l4 的编译环节
```

#### 4.4.3 源码精读

自检逻辑并非凭空设计，`build.sh` 自身就用代码实现了第二层检查：[ascendc/build.sh:L76-L81](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L76-L81) 中 `which bisheng` 为空即打印 "Error: bisheng compilation tool not found" 并 `exit 1`。我们的自检清单就是把这条内置检查扩展到驱动层与框架层。

torch_npu 与 CANN 的配套关系写在镜像 tag 里（[ascendc/README.md:L172-L178](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L172-L178)）——A3 镜像是 `cann8.5.0-torch_npu2.9.0`。如果后续安装的 torch_npu 与之不符，最典型的症状是 `import torch_npu` 时报 CCL/驱动符号找不到。README FAQ（[ascendc/README.md:L275-L277](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L275-L277)）还提示：使用与镜像不配套的 CANN 包编译时需要 `--disable-check-compatible`——这从侧面说明"配套"是整个环境的第一约束。

#### 4.4.4 代码实践

**实践目标**：在容器内跑完三层自检，拿到环境的三项证据。

**操作步骤**：

1. `npu-smi info`：观察输出的卡数（应等于 docker run 透传的 `davinciN` 数量）与每张卡的状态；
2. `which bisheng`：确认输出非空；
3. `python3 -c "import torch; import torch_npu; print(torch_npu.__version__)"`；
4. （可选进阶）`python3 -c "import torch; import torch_npu; print(torch.npu.is_available())"`。

**需要观察的现象**：① 输出一张 NPU 状态表，卡数与 `--device` 数量一致；② 输出 bisheng 的绝对路径；③ 打印 torch_npu 版本号（A3 镜像应为 2.9.0 系列）；④ 输出 `True`。

**预期结果**：四项全部通过即环境就绪。任何一项失败，按 4.4.2 的流程图定位到对应层排查。本实践需要真实 NPU 容器，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`npu-smi info` 能看到 8 张卡，但 `torch.npu.is_available()` 返回 `False`，问题最可能出在哪一层？

**答案**：驱动层没问题（npu-smi 走的是挂载进来的宿主机管理工具），问题在 torch_npu↔CANN 配套层：常见原因是 `set_env.sh` 没 source（运行时库不在 `LD_LIBRARY_PATH`）或 torch_npu 版本与镜像 CANN 不配套。先重新 source 再查版本对。

**练习 2**：为什么 `build.sh` 只检查 `bisheng` 而不检查 npu-smi 或 torch_npu？

**答案**：因为 `build.sh` 的职责只是**编译**算子包：编译期需要的是毕昇编译器与 CANN 头文件/库，不需要真实硬件（npu-smi）也不需要 PyTorch。运行与测试是另一环节（st 测试、torch_ops_extension），那时才依赖设备与 torch_npu。这体现了"编译环境"与"运行环境"的分离——也是为什么 UT（单元 8）能在无硬件环境下部分运行。

## 5. 综合实践

把本讲三步操作（拉镜像、起容器、source 环境脚本）封装成一个可复用脚本 `setup_env.sh`。目标：换一台机器、换一代芯片时，只改脚本顶部的变量即可。

以下为**示例代码**（非仓库原有文件，请保存到仓库外的个人目录，不要写进源码树）：

```bash
#!/bin/bash
# setup_env.sh —— openPangu 2.0 训练算子库开发环境一键搭建（示例代码）
# 用法: bash setup_env.sh pull|run|env
set -euo pipefail

##### 可按机器情况修改的参数 #####
CHIP="a3"            # 芯片代际: a2 / a3 / a5（对应 build.sh -c 的 ascend910b / ascend910_93 / ascend950，A2/A5 对应关系待确认）
NUM_CARDS=8          # 卡数: 8 或 16，决定 --device=/dev/davinciN 的数量
CONTAINER="omni_ops_training"

case "${CHIP}" in
  a2) IMAGE="swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a2:cann8.5.0-torch_npu2.9.0-20260130" ;;
  a3) IMAGE="swr.cn-east-4.myhuaweicloud.com/omni/sub_base-arm-openeuler-py311-a3:cann8.5.0-torch_npu2.9.0-20260130" ;;
  a5) IMAGE="swr.cn-east-4.myhuaweicloud.com/omni/base-arm-openeuler-py311-a5:cann9.0.0-torch_npu2.9.0.post2-20260506153635" ;;
  *) echo "不支持的芯片代际: ${CHIP}"; exit 1 ;;
esac

do_pull() { docker pull "${IMAGE}"; }

do_run() {
  DEVICES=""
  for i in $(seq 0 $((NUM_CARDS - 1))); do
    DEVICES="${DEVICES} --device=/dev/davinci${i}"     # 计算通道: 每张 NPU 一个字符设备
  done
  docker run -u root -itd --name "${CONTAINER}" \
    --ulimit nproc=65535:65535 --ipc=host \
    ${DEVICES} \
    --device=/dev/davinci_manager \                    # 管理通道: NPU 总管理设备
    --device=/dev/devmm_svm \                          # 管理通道: 设备内存管理(SVM)
    --device=/dev/hisi_hdc \                           # 管理通道: Host-Device 通信
    -v /home/:/home \                                  # 源码目录: 容器内外共享同一份代码
    -v /data:/data \                                   # 数据盘
    -v /etc/localtime:/etc/localtime \                 # 时区保持一致
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \  # 驱动通道: 宿主机 NPU 驱动本体(容器内只有工具链)
    -v /etc/ascend_install.info:/etc/ascend_install.info \  # 驱动通道: 驱动版本探测信息
    -v /var/log/npu/:/usr/slog \                       # 运维通道: NPU 日志映射到容器约定路径
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \ # 运维通道: NPU 状态查看工具
    -v /sys/fs/cgroup:/sys/fs/cgroup:ro \              # 运维通道: cgroup 只读挂载
    -v /usr/local/dcmi:/usr/local/dcmi \               # 运维通道: DCMI 管理接口
    -v /usr/local/sbin:/usr/local/sbin \               # 运维通道: 管理侧工具
    -v /etc/hccn.conf:/etc/hccn.conf \                 # 运维通道: 卡间互联网络配置
    -v /root/.pip:/root/.pip -v /etc/hosts:/etc/hosts \
    -v /usr/bin/hostname:/usr/bin/hostname \
    --net=host --shm-size=128g --privileged \
    "${IMAGE}" /bin/bash
  echo "容器已启动，执行 docker attach ${CONTAINER} 进入"
}

do_env() {
  # 进入容器后执行: 初始化 CANN 环境变量（每次新开 shell 都要重做）
  cat <<'EOF'
请在容器内执行：
  source /usr/local/Ascend/ascend-toolkit/set_env.sh
  which bisheng                 # 自检: 应输出编译器路径
  npu-smi info                  # 自检: 应看到透传的 NPU 卡
EOF
}

case "${1:-}" in
  pull) do_pull ;;
  run)  do_run ;;
  env)  do_env ;;
  *)    echo "用法: bash setup_env.sh pull|run|env"; exit 1 ;;
esac
```

**实践步骤**：

1. 把脚本保存到仓库外的个人目录（例如 `/home/<你>/bin/setup_env.sh`）；
2. 无 NPU 环境时，至少执行 `bash -n setup_env.sh` 做语法校验——没有任何输出即通过；
3. 对照 [ascendc/README.md:L183-L206](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L183-L206) 逐行核对：确认脚本没有遗漏任何 `--device` 与 `-v` 挂载（README 还挂载了 `/home`、`/data` 之外的所有卷，脚本已全部保留）；
4. 有 NPU 环境时依次执行 `bash setup_env.sh pull` → `bash setup_env.sh run` → `docker attach omni_ops_training` → 按 `bash setup_env.sh env` 的提示在容器内 source 并自检。

**预期结果**：`bash -n` 静默通过；真实环境上三步走完后，4.4 节的三项自检全部通过。脚本中 `do_run` 与 README 原脚本的唯一结构性差异是用 `for i in $(seq ...)` 生成设备列表，使卡数可配置。

## 6. 本讲小结

- 昇腾开发环境分三层：**宿主机驱动**（经 `--device` 与 `-v /usr/local/Ascend/driver` 进入容器）、**容器内 CANN 工具链**（镜像内置，含 bisheng 编译器）、**torch_npu**（镜像内置，与 CANN 版本严格配套，写在镜像 tag 里）。
- 镜像按芯片代际分 A2/A3/A5 三类；README 的示例确立了 A3 ↔ `ascend910_93` 的对应（`build.sh -c` 默认值也是 `ascend910_93`），A2/A5 的对应关系为推断、待确认。
- `docker run` 脚本 = 16 个 `davinciN` 计算设备 + 3 个管理设备（`davinci_manager`/`devmm_svm`/`hisi_hdc`）+ 约 15 个挂载卷；其中驱动目录挂载与 `ascend_install.info` 是容器内 CANN 能驱动硬件的关键。
- 环境变量的生命周期：`source set_env.sh` 导出 `ASCEND_HOME_PATH` 等 → `build.sh` 按五级 fallback（`-p` 参数 → `ASCEND_HOME_PATH` → `ASCEND_OPP_PATH` → 两个默认目录）解析 `ASCEND_CANN_PACKAGE_PATH` → `set_env()` source 包内 `setenv.bash` 并检查 `bisheng` → 路径传给 cmake。
- 环境自检三件套：`npu-smi info`（驱动层）、`which bisheng`（工具链层，build.sh 内置了同款检查）、`import torch_npu`（框架层）。
- 算子包装好后还有第二次 source：`opp/vendors/.../bin/set_env.bash` 把新算子注册进运行时（u1-l4 展开）。

## 7. 下一步学习建议

环境就绪后，进入 **u1-l4（编译与安装：build.sh、CMake 与自定义算子 run 包）**：动手执行 `bash build.sh -n 'ai_infra_aggregate_hidden;ai_infra_aggregate_hidden_grad' -c ascend910_93`，观察 `set_env()` 之后 `clean → cmake_config → build_package` 的完整流程，并把产物 run 包安装到 vendors 目录、完成第二次 `source set_env.bash`。

想提前了解环境变量在编译系统里的完整去向，可以顺带浏览 [ascendc/cmake/scripts/prepare.sh:L108-L128](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/scripts/prepare.sh#L108-L128)（`CUSTOM_ASCEND_CANN_PACKAGE_PATH` 如何继续传给 cmake 与 make）；想了解多芯片适配的全貌，可预习单元 9 的 u9-l1。
