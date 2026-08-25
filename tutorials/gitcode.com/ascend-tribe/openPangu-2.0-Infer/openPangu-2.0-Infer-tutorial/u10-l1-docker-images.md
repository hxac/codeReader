# u10-l1 Docker 镜像分层构建（L1/L2）

## 1. 本讲目标

在 u1-l4 里，我们把 `DOCKER_IMAGE_ID` 当成一个「从镜像仓库拉下来的现成黑盒」。本讲打开这个黑盒：**推理镜像是怎么被造出来的**。读完本讲，你应该能够：

1. 说出 L1（设备/开发层）与 L2（应用/服务层）两级镜像各自的职责边界和安装内容；
2. 讲清 `Dockerfile.base` 的四个 stage 与 `Dockerfile.omniinfer` 的两个 stage 分别做了什么，以及两级镜像是如何被 `docker_build_run.sh` 串起来的；
3. 掌握 CANN 包 `whole`（整包）与 `split`（分包）两种安装模式的差异；
4. 独立使用 `docker_build_run.sh` 的关键参数（`--build-target`、`--custom-ops`、`--L1-image/--L2-image` 等）完成一次分层构建；
5. 理解自定义算子包（AscendC `.run` 包 + torch extension）注入镜像的完整链路，并能在镜像内验证其可加载。

## 2. 前置知识

本讲是「构建/二次开发」单元的第一篇，需要以下基础概念。已学过 u1、u2 讲义的读者可以快速扫过前两条。

- **Docker 镜像与层（layer）**：Dockerfile 中每条 `RUN`/`COPY`/`FROM` 都会产生一个只读层，层层叠加构成镜像。层是构建缓存的单位：某一层没变，重建时就能直接复用。注意一个细节——后续层里 `rm` 掉的文件，仍存在于先前层中，镜像体积并不会真正减小。
- **多阶段构建（multi-stage build）与 `--target`**：一个 Dockerfile 可以有多个 `FROM ... AS <名字>` 阶段（stage），各 stage 之间可用 `COPY --from=` 传递产物。`docker build` 不指定 `--target` 时，**只构建最后一个 stage**，最终镜像内容也来自最后一个 stage。
- **构建参数 `ARG` 与 `--build-arg`**：`ARG` 声明的变量由构建方通过 `--build-arg` 注入，只在该 stage 内有效（声明位置之后的 `RUN` 能读到它）。`FROM ${BASE_IMAGE}` 这种写法让「基础镜像是谁」本身也变成可注入的参数——这是本讲两级镜像衔接的关键。
- **CANN 与 torch_npu**（回顾 u2-l1）：CANN（Compute Architecture for Neural Networks）是昇腾 NPU 的异构计算架构，包含 toolkit（编译/运行工具）、kernels（预置算子库）、hccl（集合通信）等子包；`torch_npu` 是 PyTorch 的昇腾后端插件（提供 `torch.npu` 命名空间）。镜像里的 vLLM 是 `VLLM_TARGET_DEVICE=empty` 装出来的「空设备后端」壳，真正的 NPU 能力全部来自 omni-npu 插件（u2-l1 精读过的 entry points 机制）。
- **omniinfer 主仓**（回顾 u1-l2）：本开源仓把四大组件平铺在 `components/` 下；而上游形态是一个名为 `omniinfer` 的 monorepo，`build/build.sh -m omni-npu,omni-proxy` 是它的统一构建入口。本讲的镜像构建走的是**上游形态**：镜像内克隆 omniinfer 主仓再调 `build/build.sh`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/docker/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md) | 构建说明：本地包准备、镜像分层说明、参数表与示例（个别地方与脚本源码不一致，本讲会指出） |
| [tools/docker/Dockerfile.base](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base) | L1 镜像定义：编译 Python → 装 torch/torch_npu → 装 CANN，共 4 个 stage |
| [tools/docker/Dockerfile.omniinfer](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer) | L2 镜像定义：构建工具 → 自定义算子 → vLLM 空壳 + omniinfer，共 2 个 stage |
| [tools/docker/docker_build_run.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh) | 一键编排脚本：参数解析、校验、两段 `docker build`、可选起容器 |
| [tools/docker/build_whl.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh) | 在 L2 构建过程中执行：获取 vLLM 源码（空壳安装）+ omniinfer 源码并构建安装 |
| [tools/docker/codes/build_omni_ops.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_omni_ops.sh) | 自定义算子构建脚本之一：编译 omni-ops 的 AscendC 算子并安装 torch extension |
| [tools/docker/codes/build_cann_recipes_ops.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_cann_recipes_ops.sh) | 另一个算子脚本：编译 cann-recipes-infer 的自定义算子，套路与上者相同 |
| [tools/docker/requirements/common.txt](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/requirements/common.txt) | L2 构建 whl 所需的 pip 工具链依赖清单 |
| [tools/docker/install_python.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/install_python.sh) | 从 python.org 下载源码编译安装指定版本 Python（默认 3.11.12） |
| [tools/docker/start_server.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/start_server.sh) | L2 的 ENTRYPOINT：设默认环境变量并 `exec` vLLM OpenAI api_server |
| tools/docker/copy_data/ | 本地大包（whl、CANN `.run` 包）的统一投放目录，会被 `COPY` 进构建上下文 |
| tools/docker/Dockerfile.roma | 可选第三层：在 L2 之上创建 ma-user 用户并整理权限（ROMA 平台变体） |
| tools/docker/openEuler.repo | 写入镜像的 openEuler 22.03 LTS SP4 yum 源，构建时按架构替换 `aarch64` 字样 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**镜像分层**、**CANN 安装**、**构建脚本**、**自定义算子包注入**。

### 4.1 镜像分层：BASE → L1 → L2 的职责边界

#### 4.1.1 概念说明

官方 README 把镜像分成三层（层次越高依赖越低层）：

- **BASE_IMAGE**：系统级基础镜像（openEuler 系），由使用者自备，默认标签 `test-infer-base:0.1`；
- **L1 镜像**（`Dockerfile.base` 产出，脚本变量 `L1_IMAGE`）：开发/设备层，装 Python、torch/torch_npu、CANN 与驱动环境变量。它解决的是「**在这台 NPU 机器上，任意昇腾程序能不能跑**」的问题；
- **L2 镜像**（`Dockerfile.omniinfer` 产出，脚本变量 `L2_IMAGE`）：应用层，装 vLLM、omniinfer 组件、自定义算子与服务脚本。它解决的是「**omniinfer 推理服务本身在不在**」的问题。

为什么这么切？核心动机是**变更频率与复用**：CANN、torch_npu、Python 与硬件强绑定，版本一旦定下来几乎不动，做成 L1 一次构建、长期复用；而 omni-npu 代码、算子包、Python 依赖随业务频繁迭代，做成 L2 重建时只需引用 L1 的 tag（分钟级 vs 小时级的差别）。注意两级 `docker build` 都带了 `--no-cache`（后面 4.3.3 会看到），所以这里的「复用」发生在**镜像引用层**（L1 作为 L2 的 `BASE_IMAGE`），而不是 docker 层缓存层。

L1 与 L2 的组件清单（本讲综合实践要求你亲手整理，这里先给结论）：

| 层 | 关键组件 |
| --- | --- |
| L1 | 源码编译的 Python 3.11.12、pip/setuptools/wheel、zeromq + msgpack-c（C 库）、torch + torch_npu + torchvision、CANN toolkit/kernels（whole 或 split）、可选 nnal、`~/.bashrc` 中的 Ascend 环境变量、常用调试工具（vim、net-tools、iproute 等） |
| L2 | whl 构建工具链（requirements/common.txt + yum 的 git/gcc/cmake/rpm-build 等）、可选自定义算子包（AscendC `.run` + torch extension）、`VLLM_TARGET_DEVICE=empty` 安装的 vLLM、omniinfer 主仓构建出的 omni-npu / omni-proxy 等模块、numba 与 hf_xet、ENTRYPOINT `start_server.sh` |

一个值得注意的关联：L1 里的 zeromq 与 msgpack-c 并非闲子——u4-l3 讲过的 ZMQ 心跳/回执链路、u6-l2 讲过的 omni-proxy APC msgpack 事件解析，它们的 C 运行库就是在 L1 里备好的。

#### 4.1.2 核心流程

```
BASE_IMAGE（openEuler 系统镜像，自备）
   │
   │  Dockerfile.base（4 个 stage）
   │    tmp_base：换 openEuler yum 源（按架构 sed）
   │    builder：源码编译 Python（仅取走 /usr/local）
   │    base：   Python 落地 + pip + zeromq + msgpack-c
   │    cann_pytorch：torch/torch_npu whl + CANN 安装 + bashrc 环境注入   ← 最终 stage
   ▼
L1_IMAGE（如 test-infer-meddle:0.1）
   │
   │  docker_build_run.sh 把 --build-arg BASE_IMAGE=${L1_IMAGE} 传给 ↓
   │  Dockerfile.omniinfer（2 个 stage）
   │    whl_builder：      构建工具 + requirements/common.txt
   │                       →（可选）逐个执行 codes/<op>.sh 装自定义算子
   │                       → build_whl.sh：vLLM 空壳安装 + omniinfer 构建
   │    omininfer_openai： numba/hf_xet + start_server.sh            ← --target 指定的 stage
   ▼
L2_IMAGE（如 test-infer-omniinfer:0.1）
   │
   │  Dockerfile.roma（可选：建 ma-user、整理 Ascend/nginx 权限）
   ▼
ROMA_IMAGE
```

要点：

1. `Dockerfile.omniinfer` 自己**从不引用 L1**——它只声明 `ARG BASE_IMAGE`。两级镜像的衔接完全由 `docker_build_run.sh` 在命令行上完成（`--build-arg BASE_IMAGE=${L1_IMAGE}`）。这意味着你也可以拿任何一个现成 L1 换掉 `--L1-image` 参数，L2 无感。
2. L1 构建不传 `--target`，所以产物取 Dockerfile.base 的**最后一个 stage** `cann_pytorch`；L2 构建显式 `--target omininfer_openai`（注意源码里就是拼成 `omininfer`，少了个 m，引用时必须逐字符一致）。
3. 上游 README 对分层的正式说明在 [tools/docker/README.md:15-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L15-L28)，可与上图互为印证。

#### 4.1.3 源码精读

**（1）Dockerfile.base 的 stage 划分。** 全文件 4 个 `FROM`，先用一条命令看清骨架（4.1.4 实践会真的跑它）：

[tools/docker/Dockerfile.base:3-8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L3-L8)——基础镜像参数化，并把本地 `openEuler.repo` 拷进系统后用 `sed` 把 repo 里的 `aarch64` 替换成 `ARCHITECTURE` 构建参数（这样 x86_64 构建也能复用同一个 repo 文件）：

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS tmp_base
ARG ARCHITECTURE
COPY openEuler.repo /etc/yum.repos.d/openEuler.repo
RUN sed -i "s/aarch64/${ARCHITECTURE}/g" /etc/yum.repos.d/openEuler.repo
```

[tools/docker/Dockerfile.base:10-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L10-L24)——`builder` 阶段装齐编译工具链（gcc/g++/make 与一堆 `*-devel`），然后调 `install_python.sh` 从 python.org 源码编译 Python（默认 3.11.12，见 [tools/docker/install_python.sh:42-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/install_python.sh#L42-L45) 的 `configure --enable-shared && make install`）：

```dockerfile
FROM tmp_base AS builder
...
COPY install_python.sh /tmp/install_python.sh
RUN ... /tmp/install_python.sh /usr/local ${PYTHON_VERSION}
```

[tools/docker/Dockerfile.base:28-37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L28-L37)——`base` 阶段只从 `builder` 抢走 `/usr/local`（Python 安装产物），编译工具链留在 builder 里**不进入最终镜像**。这正是多阶段构建的典型用法：编译依赖不污染运行时镜像。

```dockerfile
FROM tmp_base AS base
...
COPY --from=builder /usr/local /usr/local/
```

[tools/docker/Dockerfile.base:54-62](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L54-L62)——`base` 阶段源码编译 msgpack-c 6.1.0（关掉 examples/tests/boost 依赖），装到 `/usr`。这是给上层（omni-proxy 的 APC 事件解析，u6-l2）准备的 C 库。

**（2）Dockerfile.omniinfer 的 stage 划分。** [tools/docker/Dockerfile.omniinfer:1-3](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer#L1-L3) 声明了一串构建参数并进入 `whl_builder` 阶段：

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS whl_builder
ARG HTTP_PROXY
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG BRANCH
...
ARG CUSTOM_OPS
ARG NPU_PLATFORM
ARG INSTALL_MODULES
```

[tools/docker/Dockerfile.omniinfer:17-25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer#L17-L25)——把本地 `codes/` 与 `requirements/` 拷成 `/workspace/dist/`，装上 whl 构建工具链（pip 依赖清单见 [tools/docker/requirements/common.txt:1-15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/requirements/common.txt#L1-L15)：setuptools-scm、pybind11、Cython、build、pytest 全家桶等，都是「构建 omniinfer 各组件 wheel 时的工具」，不是运行时依赖）。

[tools/docker/Dockerfile.omniinfer:68-90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer#L68-L90)——第二个 stage `omininfer_openai` 基于 `whl_builder`，补装 numba 与高版本 hf_xet（注释说明是为了修 HuggingFace 模型下载问题），最后放入 `start_server.sh` 并设为 ENTRYPOINT：

```dockerfile
FROM whl_builder AS omininfer_openai
...
RUN ... pip install ... numba && pip install -U hf_xet
COPY start_server.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/start_server.sh
ENTRYPOINT ["start_server.sh"]
```

这也解释了 README 最后的提醒（[tools/docker/README.md:98-100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L98-L100)）：镜像默认 ENTRYPOINT 是 `start_server.sh`，所以调试时起容器要加 `--entrypoint=bash` 才能拿到交互 shell。

`start_server.sh` 本身值得一读（[tools/docker/start_server.sh:10-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/start_server.sh#L10-L27)）：它把 GLOO_SOCKET_IFNAME、VLLM_USE_V1、VLLM_WORKER_MULTIPROC_METHOD=fork 等默认值收敛到运行时（`${VAR:-default}` 写法），随后 `source ~/.bashrc`——这一步正是 L1 注入的 Ascend 环境与算子脚本追加的 `set_env.bash` 能在服务进程里生效的原因；最后 [tools/docker/start_server.sh:40-55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/start_server.sh#L40-L55) `exec` 起 vllm api_server，命令行尾部 `"$@"` 允许 `docker run` 追加参数覆盖默认值（构建脚本的 `--model ${MODEL_NAME}` 就是这样传进去的）。

#### 4.1.4 代码实践：数 stage、画依赖图

1. **实践目标**：不构建镜像，仅凭静态阅读确认两级 Dockerfile 的 stage 结构与最终产出的 stage。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   grep -n "^FROM" tools/docker/Dockerfile.base tools/docker/Dockerfile.omniinfer tools/docker/Dockerfile.roma
   ```

   然后把每个 stage 的名字、`FROM` 谁、`COPY --from` 谁，整理成 4.1.2 那样的依赖图。
3. **需要观察的现象**：`Dockerfile.base` 有 4 个 `FROM`（tmp_base、builder、base、cann_pytorch）；`Dockerfile.omniinfer` 有 2 个（whl_builder、omininfer_openai）；`Dockerfile.roma` 只有 1 个。
4. **预期结果**：L1 构建未传 `--target`，产物取最后一个 stage `cann_pytorch`（包含 Python、torch_npu、CANN）；`builder` 的编译工具链不进 L1（只有 `/usr/local` 被拷走）。L2 构建显式 `--target omininfer_openai`，该 stage 又 `FROM whl_builder`，因此 vLLM、omniinfer、算子包都在最终 L2 里。
5. 以上 grep 命令在任意有 bash 的机器即可运行；真实构建行为「待本地验证」（需要 aarch64 机器与本地包，见综合实践）。

#### 4.1.5 小练习与答案

**练习 1**：如果不传 `--build-arg BASE_IMAGE`，`docker build -f Dockerfile.omniinfer` 会发生什么？
**答案**：`FROM ${BASE_IMAGE}` 中未定义的 ARG 展开为空字符串，`FROM` 会因缺少基础镜像而报错。所以 L2 构建必须由 `docker_build_run.sh` 注入 `BASE_IMAGE=${L1_IMAGE}`（见 4.3.3），单独手敲 docker build 也要自己带上这个 `--build-arg`。

**练习 2**：为什么把「源码编译 Python」放在 `builder` stage，而不是直接在最终 stage 里编译？
**答案**：多阶段构建隔离编译污染。Python 只需要 `/usr/local` 下的产物，`COPY --from=builder /usr/local /usr/local/` 把成果带走，而 gcc、`*-devel` 头文件等上百 MB 编译工具链留在 builder，不进入最终 L1 镜像，兼顾了「能用官方指定版本 Python」与「镜像尽量小」。

**练习 3**：L1 里预装 zeromq 和 msgpack-c，对应本项目哪两条链路？
**答案**：zeromq 对应 ZMQ 控制面——PD 分离的心跳/回执（u4-l3 的 5568/15566 端口）与 omni-proxy 订阅引擎 KV 事件（u6-l2）；msgpack-c 对应 omni-proxy APC 模块解析 msgpack 格式的 KV 事件负载。

### 4.2 CANN 安装：whole 与 split 两种模式

#### 4.2.1 概念说明

L1 的最后一段（`cann_pytorch` stage）先按 CPU 架构装 torch 系 whl，再装 CANN。CANN 安装有两种模式，由 `--cann-install-mode` 控制：

- **whole（默认）**：拿两个官方整包——`Ascend-cann-toolkit_*.run`（工具链全家桶）+ `*-cann-kernels_*.run`（预置算子库）——直接安装。包大、依赖少、步骤简单。
- **split**：把 CANN 拆成 runtime/opp/toolkit/compiler/hccl/aoe/opp_kernel 等多个子包按需安装。包更细、可裁剪，但需要自己凑齐分包，且对 toolkit/compiler/hccl 三个包要加 `--pylocal`（python 组件随包安装到 CANN 目录而非系统 site-packages；该参数语义以 CANN 官方安装指南为准，待确认）。

与之配套的「安装前准备」（[tools/docker/README.md:1-14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L1-L14)）要求把以下本地包放进 `tools/docker/copy_data/`（aarch64 环境）：

- `torchvision-0.24.0-...-aarch64.whl` 与 `torch_npu-2.9.0.post2-...-aarch64.whl`（版本必须匹配：该 torchvision 依赖 torch-2.9.0）；
- `Atlas-A3-cann-kernels_8.3.T1_...run` 与 `Ascend-cann-toolkit_8.3.T1_...run`；
- 自定义算子包代码（放 `tools/docker/codes/`）。

**架构差异要点**：aarch64 上 pip 装 `torch_npu`/`torchvision` 时会自动从索引拉取匹配的 torch；x86_64 上必须自己把对应版本 torch 的 whl 也放进 `copy_data/`，否则 torch 装不上（README 第 11 条注意事项）。

#### 4.2.2 核心流程

`cann_pytorch` stage 的执行顺序：

1. 建目录 `/workspace/copy_data` 与 `/usr/local/Ascend/driver`，`COPY copy_data /workspace/copy_data/` 把本地包搬进构建上下文；
2. 按 `ARCHITECTURE` 分支安装 torch 系 whl（x86_64 找三个 whl；aarch64 找两个）；
3. 装 CANN（split 或 whole 二选一的大 if）；
4. 向 `~/.bashrc` 追加 Ascend 环境变量（toolkit 路径、setenv 脚本、driver 库路径、HCCL 重试开关）；
5. 收尾：可选执行 `EXECUTE_CMD`、删掉 bashrc 的 TMOUT 超时限制、`rm -rf /workspace/copy_data`。

注意第 5 步的 `rm` 只在最终层删文件，先前 COPY 层仍保留这些 `.run`/`.whl` 大包，镜像体积并没有真正瘦下来（docker 层语义，见第 2 节前置知识）。

#### 4.2.3 源码精读

[tools/docker/Dockerfile.base:74-78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L74-L78)——准备目录并把本地大包拷入镜像（这就是 copy_data 目录的 consumed 点）：

```dockerfile
RUN mkdir -p /workspace/copy_data && \
    mkdir -p /usr/local/Ascend/driver
COPY copy_data /workspace/copy_data/
```

[tools/docker/Dockerfile.base:81-98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L81-L98)——按架构装 torch 系 whl。aarch64 分支只用 `find` 挑出 torch_npu 与 torchvision 两个本地 whl 安装（torch 由 pip 依赖自动解析下载）；x86_64 分支还要多找一个 `torch-*x86_64.whl`；其他架构直接 `exit 1` 终止构建：

```dockerfile
if [ "$ARCHITECTURE" = "x86_64" ]; then \
    TORCH_FILE=$(find . -type f -name "torch-*x86_64.whl" | head -n 1); \
    TORCHNPU_FILE=$(find . -maxdepth 1 -type f -name "torch_npu-*x86_64.whl" | head -n 1); \
    ...
elif [ "$ARCHITECTURE" = "aarch64" ]; then \
    TORCHNPU_FILE=$(find . -maxdepth 1 -type f -name "torch_npu-*aarch64.whl" | head -n 1); \
    ...
else \
    echo "ERROR: Unsupported ARCHITECTURE..."; exit 1; \
fi
```

同一条 RUN 的尾部（[tools/docker/Dockerfile.base:100-102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L100-L102)）还装了 sudo、net-tools、iproute、vim 等运维常用工具并清理 yum 缓存——这是 L1「拿来就能调试」的底气。

[tools/docker/Dockerfile.base:108-144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L108-L144)——**split 分支**。逐个探测并安装 `CANN-runtime`、`CANN-opp`、`CANN-toolkit`（`--full --pylocal`）、`CANN-compiler`（`--full --pylocal`）、`CANN-hccl`（`--full --pylocal`）、`CANN-aoe`、`Ascend*-opp_kernel`，先处理 tfadapter 的 fwkplugin；随后把 `ASCEND_TOOLKIT_HOME=/usr/local/Ascend/latest`、`source /usr/local/Ascend/latest/bin/setenv.bash`、driver 的 `LD_LIBRARY_PATH`、`HCCL_OP_RETRY_ENABLE="L0:0, L1:0, L2:0"` 依次写进 `~/.bashrc`（均为「存在才装」的探测式安装，`if [ -f ./xxx*.run ]`）：

```dockerfile
if [ "$CANN_INSTALL_MODE" = "split" ]; then \
    ...
    if [ -f ./CANN-toolkit-*.run ]; then \
        echo y | ./CANN-toolkit-*.run --full --pylocal; \
    fi; \
    ...
    echo "export ASCEND_TOOLKIT_HOME=/usr/local/Ascend/latest" >> ~/.bashrc && \
    echo "source /usr/local/Ascend/latest/bin/setenv.bash" >> ~/.bashrc && \
    ...
```

[tools/docker/Dockerfile.base:145-167](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L145-L167)——**whole 分支**。`find` 挑出 toolkit 整包 `--install`，source 其 `set_env.sh`；再装 kernels 包；nnal 包（推理加速库，存在才装）装完把 `source /usr/local/Ascend/nnal/atb/set_env.sh` 写入 bashrc；最后是 driver 兼容处理：宿主机若挂了 driver（`/usr/local/Ascend/driver/bin/setenv.bash` 存在）就 source 它，否则把 driver 库路径与 toolkit 的 stub 库路径塞进 `LD_LIBRARY_PATH`（容器内没有驱动时用 stub 头撑过链接期）：

```dockerfile
TOOLKITFILE=$(find ./ -name "Ascend-cann-toolkit_*.run" -type f) && \
echo y | ./$TOOLKITFILE --install && \
echo 'source /usr/local/Ascend/ascend-toolkit/set_env.sh' >> ~/.bashrc && \
...
if [ -f /usr/local/Ascend/driver/bin/setenv.bash ]; then \
    echo 'source /usr/local/Ascend/driver/bin/setenv.bash' >> ~/.bashrc; \
else \
    DRIVER_LIB_PATH="/usr/local/Ascend/driver/lib64/driver" && \
    TOOLKIT_RUNTIME_LIB_PATH="/usr/local/Ascend/ascend-toolkit/latest/runtime/lib64/stub" && \
    echo "export LD_LIBRARY_PATH=$DRIVER_LIB_PATH:\$LD_LIBRARY_PATH:$TOOLKIT_RUNTIME_LIB_PATH/" >> ~/.bashrc; \
fi
```

[tools/docker/Dockerfile.base:168-170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L168-L170)——两个模式共用的收尾：`EXECUTE_CMD` 是给特殊环境留的构建期后门（可注入任意 shell 命令）；删 TMOUT 是防止交互 shell 被系统自动登出；最后清理 copy_data。

一个关键设计：**所有 Ascend 环境都写在 `~/.bashrc`**。因为镜像的 ENTRYPOINT 与后续所有 `docker exec` 的会话都会走 `source ~/.bashrc`（start_server.sh 第 29 行亦然），这样环境变量和 `set_env` 脚本就随每个 shell 自动生效，不需要每条命令手工 source。

#### 4.2.4 代码实践：whole vs split 差异清单

1. **实践目标**：不运行构建，仅通过阅读两段分支，整理出 split 模式比 whole 模式多处理的 CANN 子包与参数差异。
2. **操作步骤**：打开 [tools/docker/Dockerfile.base:106-170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.base#L106-L170)，用两种颜色/记号分别标注 split 分支（L108-144）与 whole 分支（L145-166）里出现的 `.run` 包名与安装参数，制成对照表。
3. **需要观察的现象**：split 对 toolkit/compiler/hccl 三个包用 `--full --pylocal`；whole 的 toolkit 用 `--install` 且不带 `--pylocal`；split 里有 runtime/opp/aoe/opp_kernel/tfadapter，whole 里对应能力由整包统一提供。
4. **预期结果**：split 子包清单 ≈ {tfadapter(fwkplugin), CANN-runtime, CANN-opp, CANN-toolkit, CANN-compiler, CANN-hccl, CANN-aoe, Ascend*-opp_kernel}；whole 清单 = {Ascend-cann-toolkit 整包, A*-cann-kernels} + 可选 nnal（nnal 在 split 分支同样可选，两分支末尾都有）。两分支都注入 bashrc 环境，只是 setenv 脚本路径不同（split 用 `/usr/local/Ascend/latest/bin/setenv.bash`，whole 用 `/usr/local/Ascend/ascend-toolkit/set_env.sh`）。
5. 纯文本阅读即可完成；真实安装行为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`--cann-install-mode split` 时，如果 copy_data 里没放 `CANN-runtime-*.run`，构建会失败吗？
**答案**：不会失败——split 分支对每个子包都是 `if [ -f ... ]` 探测式安装，缺哪个就静默跳过哪个。风险是产出的 L1 缺组件，问题会推迟到运行时才暴露（例如缺 hccl 时多机集合通信起不来）。这正是 u1-l5「排障要尽早看构建日志」的原因之一。

**练习 2**：为什么 aarch64 分支不装 torch 的 whl，x86_64 分支却必须装？
**答案**：aarch64 上 pip 安装 torch_npu/torchvision 时可以从 pip 索引自动解析并下载相匹配的 torch 包；x86_64 环境往往访问不到对应版本，需要使用者自行提供 `torch-*x86_64.whl`（README 第 11 条）。Dockerfile 用 `find . -name "torch-*x86_64.whl"` 显式找本地包，找不到就装错/装不上，构建失败。

**练习 3**：`echo y | ./xxx.run --install` 里管道输入的 `y` 是干什么的？
**答案**：CANN 安装器是交互式的，会询问是否同意许可协议等问题；`echo y` 把确认应答通过 stdin 喂给它，实现非交互式安装。这是 Dockerfile 里安装 `.run` 包的标准写法。

### 4.3 构建脚本：docker_build_run.sh 的一键编排

#### 4.3.1 概念说明

`docker_build_run.sh` 是把「L1 构建 → L2 构建 → （可选 Roma）→（可选起容器）」串成一条命令的编排脚本，约 300 行 bash，结构非常典型，可以概括为：

```
默认值 → print_help → parse_long_option（手写 case） → 主解析循环
      → 校验（BUILD_TARGET / CANN_INSTALL_MODE / NPU_PLATFORM）
      → 按分支执行 docker build / docker run
```

它与 u4-l4 精读过的 `pd_run.sh` 是同一种「手写 case 长参数解析」风格；与 u1-l2 的 `build/build.sh` 一样采用 fail-fast（`set -exo pipefail`，任何一步失败立刻终止并回显执行的命令）。

#### 4.3.2 核心流程

```
docker_build_run.sh
 ├─ 校验 BUILD_TARGET ∈ {L1, L2, both, skip}、CANN_INSTALL_MODE ∈ {whole, split}、NPU_PLATFORM ∈ {910B, 910C}
 ├─ BUILD_TARGET=L1 或 both：
 │    docker build --no-cache -f Dockerfile.base --build-arg ... -t ${L1_IMAGE} .
 │    若 BUILD_TARGET=L1 → 到此结束（exit 0，不会构建 L2，也不会起容器）
 ├─ BUILD_TARGET=L2 或 both：
 │    docker build --no-cache -f Dockerfile.omniinfer --build-arg BASE_IMAGE=${L1_IMAGE}
 │                 --target omininfer_openai -t ${L2_IMAGE} .
 ├─ BUILD_FOR_ROMA=True 且非 L1：docker build -f Dockerfile.roma --build-arg BASE_IMAGE=${L2_IMAGE} -t ${ROMA_IMAGE}
 └─ START_SERVER=True：docker run（--net=host --privileged + 三个 NPU 设备文件 + 环境变量）跑 start_server.sh
```

**关键参数速查**（完整表见 [tools/docker/README.md:34-53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L34-L53)，脚本内帮助见 [tools/docker/docker_build_run.sh:40-77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L40-L77)）：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--build-target` | both | L1 / L2 / both / skip（skip 跳过两级构建；README 参数表未列 skip，以脚本 help 与校验为准） |
| `--cann-install-mode` | whole | L1 的 CANN 安装模式（4.2） |
| `--base-image` | test-infer-base:0.1 | L1 的输入基础镜像（自备） |
| `--L1-image` / `--L2-image` | test-infer-meddle:0.1 / test-infer-omniinfer:0.1 | 两级产物的 tag |
| `--arch` | aarch64 | 目标架构，替换 yum repo 与选择 whl 分支 |
| `--npu-platform` | 910C | 910B 或 910C，决定算子编译的 compute-unit（4.4） |
| `--custom-ops` | 空 | 逗号分隔的算子构建脚本名（不带 .sh），如 `build_cann_recipes_ops,build_omni_ops` |
| `--branch` | master | L2 内拉取 omniinfer 主仓的分支/tag |
| `--install-modules` | omni-npu,omni-proxy | 传给 omniinfer `build/build.sh -m` 的模块列表 |
| `--vllm-version` | v0.12.0 | L2 内拉取 vLLM 的分支/tag |
| `--python-version` | 3.11.12 | L1 源码编译的 Python 版本 |
| `--start-server` | True | 构建完是否自动起容器跑 start_server.sh |

#### 4.3.3 源码精读

[tools/docker/docker_build_run.sh:5-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L5-L36)——全部默认值集中在文件头，是「这个脚本管哪些事」的一览表：

```bash
ARCH="aarch64"
CANN_INSTALL_MODE="whole"
CUSTOM_OPS=""
NPU_PLATFORM="910C"
START_SERVER="True"
PYTHON_VERSION="3.11.12"
BASE_IMAGE=test-infer-base:0.1
L1_IMAGE=test-infer-meddle:0.1
L2_IMAGE=test-infer-omniinfer:0.1
BRANCH=master
BUILD_TARGET="both"
INSTALL_MODULES="omni-npu,omni-proxy"
VLLM_VERSION="v0.12.0"
```

[tools/docker/docker_build_run.sh:80-152](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L80-L152)——`parse_long_option` 是一张「参数名 → 变量」的手写 case 表，主循环（[L157-173](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L157-L173)）逐对消费 `--xxx value`。注意兜底分支的行为（[L146-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L146-L149)）：未知参数只打印告警和帮助文本，函数仍 `return 0`——**拼错参数名不会终止构建**，脚本会带着默认值继续跑。README 示例 3（[tools/docker/README.md:83-94](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L83-L94)）里的 `--omni-version-num "dev_v1.0.0"` 就中了这个招：该参数在 case 表里不存在，实际会被忽略，omniinfer 版本仍由 `--branch` 控制。排障口诀与 u4-l4 一致：**以 `==== Current Configuration ====` 回显为准**（[L177-198](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L177-L198)）。

```bash
*)
    echo "Unknown option: $1" >&2
    print_help
    ;;
esac
return 0
```

[tools/docker/docker_build_run.sh:200-216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L200-L216)——三段白名单校验，非法取值直接 `exit 2`。这里能看到 README 表格里没写的第四种 BUILD_TARGET：

```bash
if [[ ! "${BUILD_TARGET}" =~ ^(L1|L2|both|skip)$ ]]; then ... exit 2; fi
if [[ ! "${CANN_INSTALL_MODE}" =~ ^(whole|split)$ ]]; then ... exit 2; fi
if [[ ! "${NPU_PLATFORM}" =~ ^(910B|910C)$ ]]; then ... exit 2; fi
```

[tools/docker/docker_build_run.sh:219-237](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L219-L237)——L1 分支。注意三个细节：`--no-cache`（放弃层缓存、全量重建）；`BASE_IMAGE=${BASE_IMAGE}` 作为 L1 的输入；`BUILD_TARGET=L1` 时构建完 `exit 0`，**天然不会触发起容器**，很适合「只做设备层」的场景（本讲综合实践第 2 步就靠它）：

```bash
if [[ "${BUILD_TARGET}" == "L1" || "${BUILD_TARGET}" == "both" ]]; then
    docker build --progress=plain --no-cache -f Dockerfile.base \
        --build-arg ARCHITECTURE="${ARCH}" ... -t ${L1_IMAGE} .
    if [[ "${BUILD_TARGET}" == "L1" ]]; then
        echo "BUILD_TARGET=L1 selected — finished building L1 image. Exiting."
        exit 0
    fi
```

[tools/docker/docker_build_run.sh:245-261](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L245-L261)——L2 分支。**两级镜像的衔接点就在这一行 `--build-arg BASE_IMAGE=${L1_IMAGE}`**（L253）；`--target omininfer_openai` 钉死产出 stage；CUSTOM_OPS/NPU_PLATFORM/BRANCH/INSTALL_MODULES/VLLM_VERSION 在这里进入 L2 构建环境（4.4 与 4.1 的参数在此汇合）：

```bash
docker build --progress=plain --no-cache -f Dockerfile.omniinfer \
    --build-arg HTTP_PROXY="${PROXY}" ... \
    --build-arg BASE_IMAGE=${L1_IMAGE} \
    --build-arg CUSTOM_OPS="${CUSTOM_OPS}" \
    --build-arg INSTALL_MODULES="${INSTALL_MODULES}" \
    --build-arg VLLM_VERSION="${VLLM_VERSION}" \
    --target omininfer_openai \
    -t ${L2_IMAGE} .
```

[tools/docker/docker_build_run.sh:279-293](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L279-L293)——`--start-server True` 时的自动起容器命令。NPU 容器三要素与 u1-l4 呼应：透传 `/dev/davinci_manager`、`/dev/hisi_hdc`、`/dev/devmm_svm` 三个设备文件，`--net=host`，再加 `--privileged` 与 `--shm-size=500g`；`-e PORT/-e MODEL_NAME` 会成为 start_server.sh 的环境变量，尾部 `--model "${MODEL_NAME}"` 经 ENTRYPOINT 的 `"$@"` 传入 vllm 命令。**源码观察（坑）**：该命令引用的镜像变量是 `${USER_IMAGE}`（L289），而脚本从头到尾只定义过 `L2_IMAGE`，从未给 `USER_IMAGE` 赋值——展开为空后 docker 会把 `--model` 当成镜像名而报错。因此当前版本建议一律 `--start-server False`，构建完自己 `docker run`（README 示例 3 也是这么写的）。

```bash
docker run --rm -it --shm-size=500g \
    --net=host --privileged=true \
    --device=/dev/davinci_manager \
    --device=/dev/hisi_hdc \
    --device=/dev/devmm_svm \
    -e PORT=8301 ... \
    ${USER_IMAGE} \
    --model "${MODEL_NAME}"
```

#### 4.3.4 代码实践：不构建也能观察编排逻辑

1. **实践目标**：验证你对脚本分支流转的理解，全程不触发任何 `docker build`。
2. **操作步骤**（在 `tools/docker` 目录下）：

   ```bash
   # 步骤 A：纯打印帮助（脚本不会做任何事）
   bash docker_build_run.sh --help

   # 步骤 B：skip 模式 + 不起服务，观察编排分支的回显
   bash docker_build_run.sh --build-target skip --start-server False \
       --L1-image my-l1:0.1 --L2-image my-l2:0.1 --custom-ops build_omni_ops
   ```

3. **需要观察的现象**：步骤 B 依次打印 `==== Current Configuration ====` 配置块（确认 CUSTOM_OPS、L1/L2 tag 已被正确解析）、`Skipping Dockerfile.base build (BUILD_TARGET=skip)`、`Skipping L2 image build (BUILD_TARGET=skip)`、`Skipping Roma image build ...`、`Skipping starting apiserver (START_SERVER=False)`，然后正常退出。
4. **预期结果**：退出码 0，且没有任何 docker 命令被实际执行——这证明 BUILD_TARGET=skip 是「只看编排不构建」的安全演练模式。再试试故意拼错参数 `--L2-imagee xxx`，观察「Unknown option」告警后脚本仍然继续，加深对 4.3.3 兜底分支的印象。
5. 步骤 A/B 只依赖 bash 与 echo，「待本地验证」的真实构建行为不在本实践范围内。

#### 4.3.5 小练习与答案

**练习 1**：`--build-target` 的合法取值有哪些？README 参数表和脚本是否一致？
**答案**：校验正则允许 `L1|L2|both|skip` 四种（docker_build_run.sh L201）。README 参数表只写了 `L1|L2|both`，脚本内 `print_help` 与校验逻辑都包含 `skip`——又一处「以源码为准」。

**练习 2**：想只重建 L2（改了 omni-npu 代码），应该怎么组合参数？前提是什么？
**答案**：`bash docker_build_run.sh --build-target L2 --L1-image <已有L1 tag> --L2-image <新tag> --branch <代码版本> --start-server False`。前提是本地已经有一个构建好的 L1 镜像 tag——L2 分支会把 `BASE_IMAGE` 设成 `--L1-image` 的值，若给的是不存在的默认 tag `test-infer-meddle:0.1`，docker 会尝试从远端拉取并失败（README 示例 3 的注意事项说的正是这件事）。

**练习 3**：脚本为什么在 L1 构建后对 `BUILD_TARGET=L1` 特判 `exit 0`，而不是让流程自然走到最后？
**答案**：防止误触发后续动作。不提前退出的话，流程会继续尝试 L2 构建（基于刚出的 L1，通常能成）和 `--start-server True` 的自动起容器（当前版本还引用了未定义的 `${USER_IMAGE}`，必然失败）。显式 `exit 0` 让「只做 L1」的语义干净利落。

### 4.4 自定义算子包注入：从 --custom-ops 到 torch.ops.custom

#### 4.4.1 概念说明

u3-l2 精读模型时见过 `torch.ops.custom`（DSA 打分、稀疏 FA 等定制算子）与 `torch.ops.vllm`（不透明包装）——这些算子的二进制并不是凭空来的，而是在 **L2 构建期**编译进镜像的。注入机制概括为一句话：

> `--custom-ops` 传的是「`tools/docker/codes/` 下某个 `.sh` 脚本的名字（不带扩展名）」，Dockerfile 会逐个执行这些脚本；每个脚本负责「编译 AscendC 算子 → 打成 `.run` 包安装进 CANN 的 opp/vendors 目录 → 把 set_env 写进 bashrc → 编译安装 torch extension（Python 侧绑定）」。

三个关键约定：

1. **命名即路由**：`--custom-ops build_omni_ops` 会在镜像内寻找 `/workspace/dist/codes/build_omni_ops.sh`（本地 `tools/docker/codes/build_omni_ops.sh` 被 `COPY codes /workspace/dist/codes/` 搬进去的），找不到就报错退出。
2. **算子源码要自备**：脚本默认在 `/workspace/dist/codes/omni-ops`（或 `cann-recipes-infer`）找算子源码目录——也就是 README 说的「自定义算子包代码放在 codes 路径下」。
3. **平台感知**：`--npu-platform` 决定 AscendC 编译目标，910C 是默认，910B 会追加 `--compute-unit ascend910b`。

顺带说明 L2 内部的另外两步（与算子注入同处 `whl_builder` stage）：`build_whl.sh` 先克隆/复用 vLLM 源码并以 `VLLM_TARGET_DEVICE=empty` 可编辑安装（u2-l1 讲过的「空壳 vLLM」，默认版本 `v0.12.0`，可用 `--vllm-version` 覆盖），再克隆/复用 omniinfer 主仓（`--branch`，默认 master）并执行 `bash build/build.sh -m "${install_modules}"`——正是 u1-l2 精读过的那个组件级构建入口。

#### 4.4.2 核心流程

```
--custom-ops "build_omni_ops,build_cann_recipes_ops"
        │ (docker_build_run.sh L98/L254: --build-arg CUSTOM_OPS=...)
        ▼
Dockerfile.omniinfer 的 whl_builder stage
  echo "$CUSTOM_OPS" | tr ',' '\n'      ← 按逗号拆成多行
  逐个 op：
     /workspace/dist/codes/${op}.sh --npu-platform ${NPU_PLATFORM}
     缺脚本 → echo "no matching script" + exit 1（fail fast）
        ▼
build_omni_ops.sh 内部（以 omni-ops 为例）
  1. source CANN set_env.sh（拿到 Ascend 编译环境）
  2. cd omni-ops/inference/ascendc && bash build.sh [--compute-unit ascend910b]
  3. 找 output/*.run → 安装到 $ASCEND_PATH/latest/opp（落入 opp/vendors/omni_custom_ops）
  4. echo "source .../vendors/omni_custom_ops/bin/set_env.bash" >> ~/.bashrc
  5. cd torch_ops_extension && bash build_and_install.sh（Python 绑定）
        ▼
最终 L2：start_server.sh → source ~/.bashrc → vllm 进程可 torch.ops.load_library / import 扩展
```

#### 4.4.3 源码精读

[tools/docker/Dockerfile.omniinfer:27-55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer#L27-L55)——算子注入的总开关。先向 bashrc 写入 `HCCL_IF_BASE_PORT=59000` 与 HCCL 重试开关；若 `CUSTOM_OPS` 非空，先装一组算子构建的固定依赖（google/expecttest/hypothesis/psutil/scipy/attrs/numpy/protobuf 的钉版本组合），然后 `tr ',' '\n'` 拆列表、逐个执行脚本：

```dockerfile
if [ -n "${CUSTOM_OPS}" ]; then \
    pip install ... google==3.0.0 expecttest==0.1.6 hypothesis==6.82.0 ...; \
    echo "${CUSTOM_OPS}" | tr ',' '\n' | while IFS= read -r op; do \
        op="$(echo "$op" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"; \
        [ -z "$op" ] && continue; \
        for script in /workspace/dist/codes/"$op".sh; do \
            if [ -f "$script" ]; then \
                found=1; chmod +x "$script" && /bin/bash "$script" --npu-platform "${NPU_PLATFORM}" \
                || { echo "Error: failed to run $script"; exit 1; }; \
            fi; \
        done; \
        if [ $found -eq 0 ]; then echo "Error: no matching script found ..."; exit 1; fi; \
    done; \
fi
```

两个值得注意的点：① 单个算子脚本执行失败会 `exit 1` 终止整个 L2 构建（fail fast，宁可构建失败也不带残缺算子出镜像）；② README 第 96 行说算子脚本应放在 `ops_code` 路径下，而 Dockerfile 实际查找的是 `/workspace/dist/codes/`（即本地 `tools/docker/codes/`）——README 与源码不一致时，以 Dockerfile 为准。

[tools/docker/codes/build_omni_ops.sh:46-64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_omni_ops.sh#L46-L64)——算子脚本主体：探测 Ascend 路径（有 `ascend-toolkit` 子目录用之，否则用 `/usr/local/Ascend`）并 source `set_env.sh`；进入 `omni-ops/inference/ascendc` 调 AscendC 的 `build.sh`，平台不是默认 910C 时降 compute-unit 到 `ascend910b`：

```bash
if [ "$npu_platform" != "$DEFAULT_NPU_PLATFORM" ]; then
    bash build.sh --disable-check-compatible --compute-unit ascend910b
else
    bash build.sh --disable-check-compatible
fi
```

[tools/docker/codes/build_omni_ops.sh:66-77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_omni_ops.sh#L66-L77)——编译产物是 `output/` 下的 `.run` 包，安装目标为 `$ASCEND_PATH/latest/opp`（装完落在 `opp/vendors/omni_custom_ops`，这正是 u3-l2 里 `torch.ops.custom` 的算子仓库）；随后把 vendors 目录里的 `set_env.bash` 追加进 `~/.bashrc`，让运行期 shell 自动拿到算子库路径：

```bash
RUN_FILE=$(find . -maxdepth 1 -name "*.run" | head -n1)
chmod +x "$RUN_FILE"
"./$RUN_FILE" --quiet --install-path=$ASCEND_PATH/latest/opp
echo "source $ASCEND_PATH/latest/opp/vendors/omni_custom_ops/bin/set_env.bash" >> ~/.bashrc
```

[tools/docker/codes/build_omni_ops.sh:79-81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_omni_ops.sh#L79-L81)——最后编译安装 torch extension（`torch_ops_extension/build_and_install.sh`），把 AscendC 算子包装成 `torch.ops.*` 可调用的 Python 扩展。兄弟脚本 [tools/docker/codes/build_cann_recipes_ops.sh:57-80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_cann_recipes_ops.sh#L57-L80) 流程完全同构，只是源码目录（`cann-recipes-infer`）、产物名（`CANN-custom_ops-*.run`）与 vendors 目录名（`customize`）不同——仿照它写一个新脚本是接入第三方算子包的标准姿势。

[tools/docker/build_whl.sh:44-46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L44-L46)——同 stage 的另一条腿：vLLM 空壳安装。先卸载可能残留的 `omni_infer vllm`，再以 `VLLM_TARGET_DEVICE=empty` + `TORCH_DEVICE_BACKEND_AUTOLOAD=0` 可编辑安装 vLLM（`-e /opt/vllm`，改 vLLM 源码即时生效），这正是 u2-l1「空设备后端 + 插件接管」部署形态的镜像侧来源：

```bash
pip3 uninstall -y omni_infer vllm || true
TORCH_DEVICE_BACKEND_AUTOLOAD=0 VLLM_TARGET_DEVICE=empty \
    pip3 install --no-cache-dir -e /opt/vllm --no-build-isolation
```

[tools/docker/build_whl.sh:57-72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L57-L72)——omniinfer 主仓获取的三级回退：`dist/codes/omniinfer` 本地有则直接用（离线构建通道）；否则从 gitee 克隆指定 `BRANCH`；最后调 `build/build.sh -m "${install_modules}"`（`INSTALL_MODULES` 默认 `omni-npu,omni-proxy`，来自编排脚本的 `--install-modules`）：

```bash
if [ -d "${BASE_DIR}/dist/codes/omniinfer" ]; then
    cp -r "${BASE_DIR}/dist/codes/omniinfer" "${BASE_DIR}/"
else
    git clone --depth 10 -b "${branch}" https://gitee.com/omniai/omniinfer.git
fi
cd ${BASE_DIR}/omniinfer && bash build/build.sh -m "${install_modules}"
```

**源码细节（进阶观察）**：[tools/docker/build_whl.sh:22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L22) 的 `[ -d "$/opt/vllm" ]` 中 `$` 后跟 `/` 不会被 shell 展开，测试的是字面量路径 `$/opt/vllm`——该分支永远为假，「vllm 已存在则跳过」实为死代码，每次构建都会走 else 分支重新检查/克隆。阅读这类脚本时要习惯「注释意图 ≠ 实际行为」，动手前先验证。

#### 4.4.4 代码实践：追踪一次算子注入的完整调用链

1. **实践目标**：给定 `--custom-ops build_omni_ops --npu-platform 910B`，写出从命令行参数到 `torch.ops` 可调用算子的每一跳，标注文件与行号。
2. **操作步骤**：
   1. 从 [tools/docker/docker_build_run.sh:98-99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L98-L99) 找到 `--custom-ops`/`--npu-platform` 的解析；
   2. 在 [tools/docker/docker_build_run.sh:247-258](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/docker_build_run.sh#L247-L258) 确认两者如何变成 `--build-arg`；
   3. 在 [tools/docker/Dockerfile.omniinfer:38-54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.omniinfer#L38-L54) 找到拆分循环与脚本调用点（含 `--npu-platform` 透传）；
   4. 在 [tools/docker/codes/build_omni_ops.sh:57-81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/codes/build_omni_ops.sh#L57-L81) 标出 910B 分支（`--compute-unit ascend910b`）、`.run` 安装目标、bashrc 注入、torch extension 安装四个动作。
3. **需要观察的现象**：链路上共 4 次跨文件交接（编排脚本 → docker build-arg → Dockerfile 循环 → 算子脚本），每次交接的「协议」分别是变量名、build-arg 名、脚本文件名、命令行参数。
4. **预期结果**：得到一条类似 4.4.2 流程图的带行号调用链，并能回答：为什么算子装完必须写 `~/.bashrc`？（因为 start_server.sh 及一切后续 shell 都靠 `source ~/.bashrc` 拿到 vendors 的 set_env，漏写则 vllm 进程找不到算子动态库。）
5. 纯源码阅读即可完成；镜像内实际可 import 的扩展模块名以构建日志（`build_and_install.sh` 的输出）为准，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`--custom-ops` 传 `my_ops`，需要准备什么文件、放在哪？
**答案**：需要在 `tools/docker/codes/` 下提供 `my_ops.sh`（可仿照 build_omni_ops.sh），并把该脚本要用的算子源码也放进 `codes/`（或让脚本自行下载）。构建时 `COPY codes /workspace/dist/codes/` 会把它搬进镜像，Dockerfile 按 `/workspace/dist/codes/my_ops.sh` 路由执行。注意 README 写的 `ops_code` 路径与实际不符，以 Dockerfile 的 `codes` 为准。

**练习 2**：`--npu-platform 910B` 会改变算子构建的什么？
**答案**：编排脚本校验后经 `--build-arg NPU_PLATFORM` 传入，Dockerfile 调脚本时透传 `--npu-platform 910B`；build_omni_ops.sh 里非 910C 平台会追加 `--compute-unit ascend910b`，让 AscendC 按 910B 的计算单元编译（build_cann_recipes_ops.sh 同理）。在 910B 机器上用默认 910C 产物会出现算子与硬件不兼容。

**练习 3**：为什么算子包要拆成「AscendC `.run` 装进 opp/vendors」和「torch extension」两段，而不是一次搞定？
**答案**：两者职责不同：`.run` 包安装的是 CANN 侧的算子二进制与描述（供图编译/调度系统识别，落在 `opp/vendors/<name>`），torch extension 提供的是 PyTorch 侧的 Python 绑定（让 `torch.ops.<namespace>.<op>` 可从 Python 调用，u3-l2 的 `torch.ops.custom` 即消费此层）。分开安装还让同一批 AscendC 算子可以被非 torch 的执行框架复用。

## 5. 综合实践

**任务**：亲手完成一次「L1 单独构建 → L2 带自定义算子构建 → 镜像内验证」的全流程（即本讲规格指定的实践任务）。需要一台 aarch64 构建机与 Docker 环境；本讲义基于源码静态推导，以下命令的**实际运行结果待本地验证**。

### 第 1 步：准备本地包

按 [tools/docker/README.md:1-14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/README.md#L1-L14) 把 torch_npu/torchvision whl 与 CANN toolkit/kernels 两个 `.run` 包放入 `tools/docker/copy_data/`；把 omni-ops 算子源码目录放入 `tools/docker/codes/`（`build_omni_ops.sh` 默认在 `/workspace/dist/codes/omni-ops` 找它）。

### 第 2 步：只构建 L1

```bash
cd tools/docker
bash docker_build_run.sh --build-target L1 \
    --pip-index-url "https://mirrors.huaweicloud.com/repository/pypi/simple" \
    --pip-trusted-host "mirrors.huaweicloud.com" \
    --L1-image my-l1:0.1
```

`BUILD_TARGET=L1` 会在 L1 构建完成后 `exit 0`（4.3.3），不会碰 L2 也不会起容器。观察 `--progress=plain` 日志里四个 stage 的推进与 CANN 安装输出。

### 第 3 步：盘点 L1 组件清单

```bash
docker run --rm -it --entrypoint=bash my-l1:0.1 -c '
  python -V;
  pip list 2>/dev/null | grep -Ei "torch|numpy";
  ls /usr/local/Ascend/;
  grep -E "Ascend|HCCL" ~/.bashrc'
```

预期：Python 3.11.12；torch/torch_npu/torchvision 三件套；`/usr/local/Ascend/` 下有 `ascend-toolkit`（whole 模式）或 `latest`（split 模式）；bashrc 里有 setenv 与 LD_LIBRARY_PATH 注入。

### 第 4 步：基于该 L1 构建 L2 并注入算子

```bash
bash docker_build_run.sh --build-target L2 \
    --L1-image my-l1:0.1 \
    --L2-image my-l2:0.1 \
    --custom-ops build_omni_ops \
    --start-server False
```

注意必须显式给 `--L1-image`（否则用默认 tag 会找不到基础镜像），且 `--start-server False` 避开 4.3.3 指出的 `${USER_IMAGE}` 问题。

### 第 5 步：验证 L2 组件与算子可加载

```bash
docker run --rm -it --entrypoint=bash my-l2:0.1 -c '
  source ~/.bashrc;
  pip list 2>/dev/null | grep -Ei "vllm|omni";
  python -c "import torch, torch_npu; import vllm; print(vllm.__version__)";
  ls /usr/local/Ascend/latest/opp/vendors/ 2>/dev/null || ls /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/ 2>/dev/null'
```

预期：pip 列表出现 vllm（`-e` 可编辑安装指向 `/opt/vllm`）与 omniinfer 构建出的 omni-npu、omni-proxy 模块；`import vllm` 成功；vendors 目录下出现 `omni_custom_ops`。若要进一步验证算子扩展，torch_ops_extension 构建日志会打印可 import 的扩展模块名，按日志提示 import（模块名以日志为准）。

### 第 6 步：整理 L1/L2 组件对照表

把第 3、5 步的实际输出整理成 4.1.1 表格的「实测版」，并记录：两级构建各自耗时、L2 单独重建（改 `--branch` 后重复第 4 步）耗时——后者应显著小于前者，这就是分层的主要收益。

## 6. 本讲小结

- 推理镜像分两层：**L1（Dockerfile.base）** 管「设备能不能跑」——源码编译 Python、torch/torch_npu、CANN 与 Ascend 环境变量；**L2（Dockerfile.omniinfer）** 管「服务在不在」——vLLM 空壳、omniinfer 组件（`build/build.sh -m`）、自定义算子、`start_server.sh` ENTRYPOINT。
- 两级镜像靠 `docker_build_run.sh` 的 `--build-arg BASE_IMAGE=${L1_IMAGE}` 衔接，Dockerfile 本身不感知 L1 的存在；L1 构建后 `exit 0`，天然支持「只做设备层」。
- CANN 有 whole（toolkit 整包 + kernels）与 split（runtime/opp/toolkit/compiler/hccl/aoe 等分包探测式安装）两种模式；aarch64 只需 torch_npu/torchvision 本地 whl，x86_64 还必须自备匹配版本的 torch whl。
- 自定义算子注入链是「`--custom-ops` 脚本名 → codes/ 目录路由 → AscendC 编译成 `.run` 装 opp/vendors → set_env 写入 bashrc → torch extension 绑定」，`--npu-platform` 决定 compute-unit。
- 读这个脚本要学会「以源码为准」：README 参数表缺 `skip`、示例里的 `--omni-version-num` 会被静默忽略、`ops_code` 路径与实际 `codes` 不符、`--start-server True` 分支引用了未定义的 `${USER_IMAGE}`、`build_whl.sh` 里 `$/opt/vllm` 判断恒为假——这五处都以脚本与 Dockerfile 的实际行为为准。

## 7. 下一步学习建议

本讲完成了「镜像怎么来」，下一讲 **u10-l2（omni-npu 测试体系与本地跑测）** 将讲「改了代码怎么验证」：unit/integration 分层、`run_tests.sh` 与 conftest，正好可以复用本讲构建出的 L1/L2 镜像作为跑测环境。如果想继续深入本讲的线索，建议：

1. 对照阅读顶层 [build/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/build/build.sh)（u1-l2 精读过），理解 `build_whl.sh` 调用的 `-m omni-npu,omni-proxy` 在组件级构建里如何分发；
2. 回看 u2-l1 的 `pyproject.toml` entry points，体会「L2 镜像里 pip 装出的 omni_npu 包」与「vLLM 启动时发现插件」之间的闭环；
3. 阅读 [tools/docker/Dockerfile.roma](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/Dockerfile.roma)，思考在 L2 之上再造一层用户权限镜像的取舍（何时值得加层、何时直接改 L2）。
