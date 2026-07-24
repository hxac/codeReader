# 环境搭建与安装

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 SGLang-Omni 的**两条安装路径**（Docker 推荐 / 手动），并能选择适合自己的那一条。
- 在 Docker 容器里用 `uv` 创建虚拟环境并把 `sglang-omni` 以可编辑模式装好。
- 读懂 `pyproject.toml` 中那一长串依赖，理解 `torch==2.11.0`、`sglang==0.5.12.post1`、`flash-attn-4`、`nixl-cu13`、`mooncake-transfer-engine-cuda13` 这些**精确版本约束**背后的原因。
- 解释为什么「手动安装」必须先自己编译 UCX 1.20.x 和 flash-attn-4。
- 成功运行 `sgl-omni --help`，确认 CLI 入口可用。

本讲承接 [u1-l1 项目定位](u1-l1-project-overview.md)：上一讲我们知道了 SGLang-Omni 是「与上游 SGLang 组合（composing with）」的多阶段推理运行时。本讲就来把这套运行时在你本机跑起来——而你会在安装过程中，**亲手看到**这种「组合」关系如何体现在 Docker 基础镜像和依赖清单里。

## 2. 前置知识

在动手前，先理解几个本讲会用到的概念：

- **Docker 镜像（image）/ 容器（container）**：镜像是一个只读的「环境快照」，容器是镜像跑起来的实例。SGLang-Omni 的官方镜像已经把最难装的底层库（CUDA、UCX、flash-attn）都预置好了，所以我们强烈推荐用 Docker。
- **虚拟环境（virtual environment）**：Python 项目隔离依赖的机制。本讲用 `uv venv` 创建一个名为 `.venv` 的虚拟环境，让 `sglang-omni` 的依赖只装在这个目录里，不污染系统 Python。
- **可编辑安装（editable install）**：`pip install -e .`（本讲用 `uv pip install -v -e .`）会把包以「软链接」方式装好——你修改源码后无需重装即可生效，非常适合边读源码边改。
- **CUDA 13（cu130）**：NVIDIA GPU 计算栈的一个大版本。SGLang-Omni 的部分高性能传输库（nixl、mooncake）必须使用针对 CUDA 13 编译的 wheel（`-cu13` 后缀），否则会因为加载到 CUDA 12 的库而崩溃。
- **uv**：一个用 Rust 写的、极快的 Python 包管理器（pip 的替代品）。本仓库的安装说明全部基于 `uv`。

## 3. 本讲源码地图

本讲只涉及「如何把项目装上」这一件事，因此涉及的关键文件很少：

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/installation.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md) | 官方安装文档，列出了 Docker 与手动两条路径，是本讲的「操作手册」。 |
| [pyproject.toml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml) | 项目的「依赖清单与打包元数据」，定义了所有 Python 依赖、CLI 入口和可选扩展。 |
| [docker/Dockerfile](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docker/Dockerfile) | 构建 `lmsysorg/sglang-omni:dev` 镜像的配方，揭示「为什么 Docker 路径省事」。 |
| [sglang_omni/cli/\_\_init\_\_.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py) | `sgl-omni` 命令的真实入口，本讲用它验证安装是否成功。 |

> 提示：本讲里所有形如 `文件路径:L行` 的引用都是可点击的永久链接，指向当前 HEAD `cf61f234`。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：Docker 安装路径、`uv venv` 与可编辑安装、依赖版本约束、CLI 入口 `sgl-omni`。

### 4.1 Docker 安装路径（推荐）

#### 4.1.1 概念说明

SGLang-Omni 依赖一套**非常挑剔**的底层栈：特定版本的 CUDA、需要手工编译的 UCX、和 PyTorch 强绑定的 flash-attn-4。如果在你自己的机器上从头配齐这套栈，很容易在「版本不匹配」上耗掉一整天。

Docker 路径的思路是：**官方镜像已经把最难的部分做好了，你只需要在容器里装 Python 包那一层**。这正好呼应了上一讲「与 SGLang 组合」的定位——你会看到，omni 的镜像直接**继承**自 SGLang 的镜像。

#### 4.1.2 核心流程

Docker 路径分三步：

1. `docker pull` 拉取官方镜像 `lmsysorg/sglang-omni:dev`。
2. `docker run` 启动容器，关键 flag 含义：
   - `--gpus all`：把宿主机 GPU 透传进容器（没有它就跑不了 GPU 推理）。
   - `--shm-size 32g`：放大共享内存，多阶段之间用共享内存传张量时需要。
   - `--ipc host` / `--network host` / `--privileged`：共享 IPC 命名空间、共享网络、提权，这些是跨进程张量传输（CUDA IPC）和多 worker 通信所需要的宽松环境。
3. 进入容器后，克隆仓库、建虚拟环境、`uv pip install`。

#### 4.1.3 源码精读

官方文档 [docs/get_started/installation.md:L5-L36](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md#L5-L36) 给出了 Docker 路径的完整命令。其中容器内安装部分是：

```bash
git clone git@github.com:sgl-project/sglang-omni.git
cd sglang-omni

uv venv .venv -p 3.12
source .venv/bin/activate

uv pip install -v -e .   # drop `-e` for a non-editable install
```

这段命令里：`-p 3.12` 指定用 Python 3.12 建虚拟环境；`-e .` 是可编辑安装；`-v` 打开详细日志，方便观察依赖解析过程。

**为什么 Docker 路径这么省事？** 答案在镜像构建配方 [docker/Dockerfile:L1-L22](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docker/Dockerfile#L1-L22)：

```dockerfile
FROM lmsysorg/sglang:dev AS base

# install UCX
RUN git clone https://github.com/openucx/ucx.git \
    && cd ucx \
    && git checkout v1.20.x \
    && ./autogen.sh \
    && ./contrib/configure-release-mt  ... \
    && make -j && make -j install-strip && ldconfig
```

第一行 `FROM lmsysorg/sglang:dev` 是关键：**omni 镜像的上层是 SGLang 官方镜像**。SGLang 镜像里已经预装好了 CUDA、flash-attn、sglang 自身；omni 镜像只在其之上**额外编译了 UCX**（`v1.20.x` 分支，带 CUDA + verbs 支持）。这就是 u1-l1 所说「组合关系」在工程上的具象——omni 不重新发明轮子，而是站在 SGLang 的肩膀上。

#### 4.1.4 代码实践

> 本实践目标：拉起官方容器并进入交互式 shell。

1. 拉取镜像：`docker pull lmsysorg/sglang-omni:dev`。
2. 启动容器（参考文档 [docs/get_started/installation.md:L16-L24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md#L16-L24) 的参数）。
3. 进入后执行 `which ucx_info` 或 `ucx_info -v`，观察容器里 UCX 是否就绪。

需要观察的现象：容器内 UCX 版本号应显示 `1.20.x`；GPU 应可见（`nvidia-smi` 有输出）。

预期结果：你得到一个「GPU 可用、UCX/flash-attn/CUDA 已就绪」的干净环境，只剩下 Python 包还没装。

> 说明：本讲实践需要一台带 NVIDIA GPU 且已配置 Docker + NVIDIA Container Toolkit 的机器。若你当前环境没有 GPU，可跳过实际运行，仅做源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `docker run` 要加 `--shm-size 32g`？去掉会有什么风险？

> **参考答案**：SGLang-Omni 的多阶段之间会用共享内存（shared memory）传张量。Docker 默认共享内存只有 64MB，远不够放 GPU 张量，会引发「无法分配共享内存」类的运行时错误。放大到 32g 是为跨阶段数据传输留足空间。

**练习 2**：`docker/Dockerfile` 第 1 行 `FROM lmsysorg/sglang:dev` 体现了 omni 与 sglang 什么关系？

> **参考答案**：体现了 u1-l1 讲过的「组合（composing with）」关系——omni 镜像直接以 sglang 镜像为基础层，复用其 CUDA/flash-attn/sglang，只在其上追加自己需要的 UCX，避免重复构建整套底层栈。

---

### 4.2 uv venv 与可编辑安装

#### 4.2.1 概念说明

进入容器（或手动环境）后，下一步是把 `sglang-omni` 这个 Python 包装上。本仓库统一用 **uv** 来管理虚拟环境和安装。`uv venv .venv` 会创建一个隔离的 `.venv` 目录，`uv pip install -v -e .` 则把当前目录（`.`，即仓库根目录）以**可编辑模式**安装进去。

可编辑模式的好处是：你在 `sglang_omni/` 下改任何源码，无需重新安装就能立刻生效。这对「边读源码边动手」的学习方式非常友好。

#### 4.2.2 核心流程

安装一个 Python 包的内部步骤可以理解为：

```text
uv pip install -v -e .
   │
   ├─ 读取 pyproject.toml 的 [project].dependencies（依赖清单）
   ├─ 解析依赖树（resolver），在版本约束下选出一组兼容版本
   ├─ 下载 wheel / sdist，按拓扑顺序安装
   └─ 把 [project.scripts] 里声明的命令注册到 .venv/bin/
```

最后一步很重要：`sgl-omni` 这个命令就是在这里被「创建」出来的——它实际上指向 `.venv/bin/sgl-omni`，再转发到 Python 模块 `sglang_omni.cli:app`。

#### 4.2.3 源码精读

仓库的 Python 版本声明在 [pyproject.toml:L11](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L11)：

```toml
requires-python = ">=3.10"
```

这里允许 Python 3.10 及以上。但官方安装文档统一用 `-p 3.12`（见 [docs/get_started/installation.md:L32](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md#L32)）。两者并不矛盾：`>=3.10` 是「最低门槛」，而 3.12 是「推荐/经过验证」的版本。学习时建议**照搬文档用 3.12**，减少踩坑概率。

而 CLI 命令的「出生地」是 [pyproject.toml:L86-L88](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L86-L88)：

```toml
[project.scripts]
sgl-omni = "sglang_omni.cli:app"
sgl-omni-router = "sglang_omni_router.serve:main"
```

这段配置告诉打包工具：安装后请生成两个命令——`sgl-omni`（指向 `sglang_omni.cli` 模块里的 `app` 对象）和 `sgl-omni-router`（指向路由器的入口）。所以「装完包就有 `sgl-omni` 命令」不是魔法，而是这里声明出来的。

#### 4.2.4 代码实践

> 本实践目标：建好 `.venv` 并以可编辑模式安装，确认 `sgl-omni` 命令出现。

1. 在仓库根目录执行 `uv venv .venv -p 3.12`，然后 `source .venv/bin/activate`。
2. 执行 `uv pip install -v -e .`（详细模式），观察 resolver 如何解析这一长串依赖。
3. 安装完成后执行 `which sgl-omni`，确认它指向 `.venv/bin/sgl-omni`。

需要观察的现象：`-v` 日志里会陆续打印每个依赖的解析与安装过程；最后 `which sgl-omni` 应返回 `.venv` 内的路径。

预期结果：`.venv/bin/` 下出现 `sgl-omni` 可执行脚本，`import sglang_omni` 可以成功。

> 待本地验证：实际安装耗时与机器、网络有关，第一次拉取 torch/sglang 等大包可能较久。

#### 4.2.5 小练习与答案

**练习 1**：`uv pip install -e .` 和 `uv pip install .`（无 `-e`）有什么区别？本讲为什么推荐前者？

> **参考答案**：`-e` 是可编辑安装，包以「指向源码目录」的方式登记，改源码即时生效；不带 `-e` 则是把代码拷贝到 site-packages，改源码需重装。本讲推荐前者，因为后续讲义会频繁阅读并小改源码（加日志、改参数），可编辑模式免去反复重装。

**练习 2**：为什么 `requires-python = ">=3.10"`，但文档却让你用 3.12？

> **参考答案**：`>=3.10` 只是「能跑」的下限保证；3.12 是开发与 CI 验证过的推荐版本，依赖链（尤其编译型扩展）在 3.12 上最稳。两者不冲突，照文档用 3.12 最省心。

---

### 4.3 依赖版本约束

#### 4.3.1 概念说明

`pyproject.toml` 里的依赖清单是理解 SGLang-Omni 的「骨架图」——它告诉你这个运行时由哪些零件拼成、各零件之间有多紧密。本仓库的依赖有一个鲜明特征：**大量精确 pin（`==`）和窄区间约束**，把 torch、sglang、transformers、flash-attn、kernels 锁成一个必须整体升级的「栈」。

这不是过度工程，而是因为 ML 推理栈里 ABI（二进制接口）兼容性极脆弱：torch 大版本一动，flash-attn、torchcodec、numba 都可能跟着崩。理解这些约束，你才能在「装不上」时知道该查哪里。

#### 4.3.2 核心流程

可以把依赖按职责分成几组：

| 组别 | 代表依赖 | 约束 | 作用 |
| --- | --- | --- | --- |
| AR 执行引擎 | `sglang==0.5.12.post1` | 精确 pin | 上一讲的「组合对象」，提供 prefill/decode 调度 |
| 张量核心 | `torch==2.11.0` | 精确 pin | 一切张量运算的根基 |
| 衍生自 torch | `torchvision==0.26.0`、`torchaudio==2.11.0`、`torchcodec==0.11.1` | 精确 pin | 必须随 torch 主版本走 |
| 注意力加速 | `flash-attn-4>=4.0.0b9,<4.0.0b16` | 上界限 | `<4.0.0b16` 为避开 cutlass-dsl 冲突 |
| 算子集 | `kernels>=0.14.0,<0.15` | 窄区间 | 必须匹配 transformers/sglang 栈 |
| 模型/分词 | `transformers==5.6.0` | 精确 pin | HF 模型与分词器 |
| 跨阶段传输 | `nixl-cu13>=1.1.0`、`mooncake-transfer-engine-cuda13>=0.3.10` | cu13 专用 | u1-l1 所述「跨阶段传输」的数据平面后端 |

「栈」的耦合度可以用一个简化的依赖关系近似理解：

\[
\text{torch } 2.11.0 \;\longrightarrow\; \{\text{torchvision}, \text{torchaudio}, \text{torchcodec}, \text{numba}\}
\]

\[
\text{sglang } 0.5.12.post1 \;\longleftrightarrow\; \{\text{torch } 2.11.0,\; \text{transformers } 5.6.0,\; \text{kernels } 0.14.x,\; \text{diffusers } 0.37.0\}
\]

箭头表示「必须跟随」，双向箭头表示「互相锁定」。这意味着**升级任意一个，往往要整体重测整条链**。

#### 4.3.3 源码精读

核心依赖声明在 [pyproject.toml:L12-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L12-L78)。挑几条最关键、且带有「为什么」注释的来看：

```toml
"torch==2.11.0",          # PyTorch for tensor operations
...
"sglang==0.5.12.post1",
"flash-attn-4>=4.0.0b9,<4.0.0b16",  # Avoid cutlass-dsl>=4.5.2 conflict
"kernels>=0.14.0,<0.15",  # Match Transformers 5.6 / SGLang 0.5.12.post1 stack (ships kernels 0.14.1)
"nixl-cu13>=1.1.0",  # CUDA 13 relay wheel; the generic nixl/-cu12 wheel breaks on cu130
"mooncake-transfer-engine-cuda13>=0.3.10",  # CUDA 13 relay wheel; generic ... pulls cu12 and breaks import mooncake on cu130
```

逐条解读：

- **`torch==2.11.0`**：精确 pin。torch 是整条 ML 链的地基，没有浮动空间。
- **`sglang==0.5.12.post1`**：精确 pin。这是 omni「组合」的 AR 引擎，omni 直接复用它的 prefill/decode，版本必须严格对齐。
- **`flash-attn-4>=4.0.0b9,<4.0.0b16`**：注意它是 beta 版（`b9`），且设了**上界 `<4.0.0b16`**——注释说「避开 cutlass-dsl>=4.5.2 冲突」。也就是说，flash-attn 太新会拖进一个不兼容的 cutlass-dsl，进而和 sglang 的 `nvidia-cutlass-dsl` pin 打架。安装文档里手动路径明确要求装 `>=4.0.0b9,<4.0.0b16`（见 [docs/get_started/installation.md:L43](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md#L43)）。
- **`kernels>=0.14.0,<0.15`**：注释明确「匹配 Transformers 5.6 / SGLang 0.5.12.post1 栈」。这是「栈耦合」的典型例子。
- **`nixl-cu13>=1.1.0` 与 `mooncake-transfer-engine-cuda13>=0.3.10`**：这是 u1-l1 提到的「跨阶段传输」的**数据平面后端**（relay）。关键在 **`cu13` 后缀**：通用的 `nixl` / `mooncake-transfer-engine`（不带 cu13）会拉到 CUDA 12 的二进制，在 CUDA 13.0（cu130）环境里 import 时直接崩溃。注释里两次强调 "breaks on cu130"，这正是为什么要**点名**用 cu13 专用 wheel。

另外还有两处值得注意：

```toml
[project.optional-dependencies]
audar-tts = [
    "llama-cpp-python==0.3.34",
    "neucodec==0.0.6",
]
```

这是 [pyproject.toml:L80-L84](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L80-L84) 的**可选依赖**（extra）。意思是：只有当你需要 AudAR-TTS 这个模型家族时，才需要 `pip install -e ".[audar-tts]"` 额外装这两个包。普通安装不会拉它们——这是一种按需加载的依赖分组。

以及一个 uv 专用的覆盖：

```toml
[tool.uv]
override-dependencies = [
    "protobuf>=6.31.1,<7.0.0",
]
```

[pyproject.toml:L90-L93](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L90-L93) 这里用 uv 的 `override-dependencies` 强行把传递依赖 `protobuf` 锁在 `>=6.31.1,<7.0.0`，即使某个上游包声明想要别的版本。这是处理「上游版本冲突」时的兜底手段。

#### 4.3.4 代码实践

> 本实践目标：把抽象的「版本约束」变成可观察的事实。

1. 安装完成后，在 `.venv` 里执行 `uv pip list | grep -E '^(torch|sglang|transformers|flash-attn-4|kernels|nixl-cu13|mooncake-transfer-engine-cuda13) '`。
2. 对照 [pyproject.toml:L12-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L12-L78)，逐一核对实际装上的版本是否落在声明的约束区间内。
3. 额外验证：`python -c "import nixl; print('nixl ok')"` 与 `python -c "import mooncake; print('mooncake ok')"`，确认 cu13 wheel 在当前 CUDA 下能正常 import。

需要观察的现象：实际版本应全部满足约束；两个 import 命令应不报错。

预期结果：你得到一张「声明约束 vs 实际版本」的对照表，且 nixl/mooncake 可正常导入。

> 待本地验证：`import mooncake` 是否成功取决于容器 CUDA 版本是否为 cu130。若环境不符，import 可能失败——这恰好印证了 cu13 wheel 的必要性。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `nixl-cu13` 要带 `cu13` 后缀，而不直接用 `nixl`？

> **参考答案**：不带后缀的通用 `nixl` wheel 内部链接的是 CUDA 12 的二进制，在 CUDA 13.0（cu130）环境里 `import nixl` 会崩溃。带 `-cu13` 后缀的是专门为 CUDA 13 编译的 wheel，能正常加载。注释里 "the generic ... wheel breaks on cu130" 说的就是这件事。

**练习 2**：`flash-attn-4` 为什么是 `>=4.0.0b9,<4.0.0b16` 这种「带下界又带上界」的区间，而不是直接 `==4.0.0b9`？

> **参考答案**：下界 `>=4.0.0b9` 保证拿到修好已知 bug 的版本；上界 `<4.0.0b16` 是为了**阻止**解析器装到 `>=b16` 的版本，因为那里会引入 `cutlass-dsl>=4.5.2`，与 sglang 的 `nvidia-cutlass-dsl` pin 冲突。区间约束在「允许小步升级」和「挡住不兼容大版本」之间取了平衡。

**练习 3**：`[project.optional-dependencies]` 里的 `audar-tts` 在什么时候才需要装？

> **参考答案**：只有当你要服务 AudAR-TTS 模型家族时才需要，用 `uv pip install -e ".[audar-tts]"` 额外装 `llama-cpp-python` 和 `neucodec`。普通安装不会拉它们，避免给不需要这些重依赖的用户增加负担。

---

### 4.4 CLI 入口 sgl-omni

#### 4.4.1 概念说明

装完包后，你最常打交道的命令就是 `sgl-omni`。它是 SGLang-Omni 对外暴露的**命令行入口**，基于 [Typer](https://typer.tiangolo.com/)（一个用类型注解生成 CLI 的库）构建。

`sgl-omni` 当前注册了子命令：

- `sgl-omni serve`：启动 OpenAI 兼容的 API 服务（u1-l4 会详细讲）。
- `sgl-omni config`：查看/导出管线配置（u1-l5 会详细讲）。

本讲我们只用 `sgl-omni --help` 来**验证安装是否成功**——如果这条命令能正常打印帮助，说明包装好了、入口脚本注册对了、核心依赖也都能 import。

#### 4.4.2 核心流程

当你敲 `sgl-omni --help` 时，发生的事情是：

```text
shell 找到 .venv/bin/sgl-omni   （由 [project.scripts] 生成）
   │
   └─ 执行入口点 sglang_omni.cli:app
         │
         └─ Typer 的 app 对象解析 --help，打印子命令列表
```

`app` 是一个 `Typer()` 实例，它通过 `add_typer` / `command` 把 `config` 和 `serve` 挂上去。Typer 会把挂载结构自动转成 `--help` 里的命令树。

#### 4.4.3 源码精读

入口脚本映射在 [pyproject.toml:L87](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L87)（`sgl-omni = "sglang_omni.cli:app"`），而 `app` 的真实定义在 [sglang_omni/cli/\_\_init\_\_.py:L1-L14](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py#L1-L14)：

```python
from typer import Typer

from .config import config_app
from .serve import serve as _serve

app = Typer()

# Register the subcommands.
app.add_typer(config_app, name="config")
app.command(
    "serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)(_serve)
```

解读：

- 第 6 行 `app = Typer()` 创建顶层命令组。
- 第 9 行 `app.add_typer(config_app, name="config")` 把 `config_app`（定义在 `cli/config.py`）挂为 `config` 子命令组。
- 第 10-12 行把 `serve` 函数注册为 `serve` 命令，并设置 `allow_extra_args=True, ignore_unknown_options=True`——这意味着 `serve` 会**原样吞下并转发**它不认识的 flag。这是为什么 `sgl-omni serve` 能接受那么多模型相关参数（这些 flag 由 `serve` 内部再解析）。

> 注：第 9 行的 `config_app` 来自 [sglang_omni/cli/config.py:L13-L15](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/config.py#L13-L15)，它本身又是一个 `Typer()`，里面有 `view`/`export` 命令——这就是为什么命令是 `sgl-omni config view` 这种两层结构。

#### 4.4.4 代码实践

> 本实践目标：用 `sgl-omni --help` 作为安装成功的「冒烟测试」。

1. 确认已 `source .venv/bin/activate`。
2. 执行 `sgl-omni --help`。
3. 再分别执行 `sgl-omni config --help` 和 `sgl-omni serve --help`，观察子命令与参数。

需要观察的现象：`--help` 应列出 `serve` 和 `config` 两个子命令，且无 ImportError、无 traceback。

预期结果：看到结构化帮助文本，证明 `typer`、`sglang_omni` 及其导入链全部正常。如果此处报错，几乎可以肯定是依赖版本没对齐（回到 4.3 节排查）。

> 说明：这是后续所有讲义的前置检查点——只要 `sgl-omni --help` 能跑通，本课程后面的实践才有意义。

#### 4.4.5 小练习与答案

**练习 1**：`sgl-omni` 这个 shell 命令是凭空出现的吗？它是怎么被创建的？

> **参考答案**：不是凭空出现。`pyproject.toml` 的 `[project.scripts]` 里声明了 `sgl-omni = "sglang_omni.cli:app"`，安装时打包工具据此在 `.venv/bin/` 下生成一个同名启动脚本，该脚本调用 Python 入口点 `sglang_omni.cli:app`。所以卸载包后这个命令也会消失。

**练习 2**：为什么 `serve` 命令要设 `allow_extra_args=True, ignore_unknown_options=True`？

> **参考答案**：`serve` 需要把大量模型/运行时相关的 flag（如模型路径、张量并行度等）**透传**给内部真正的启动逻辑（`launch_server`）。设这两个选项后，Typer 不会因为遇到未知 flag 就报错，而是把多余参数原样收集起来交给 `serve` 处理，从而让单个命令能承载极其丰富的参数。

## 5. 综合实践

把本讲四个模块串起来，完成一次**端到端的安装冒烟**：

1. **选路径**：评估你的机器条件，在「Docker 推荐」和「手动安装」之间选一条，并说出你选择的理由（提示：手动安装要先自己编译 UCX 1.20.x 和 flash-attn-4，见 [docs/get_started/installation.md:L38-L44](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/installation.md#L38-L44)）。
2. **建环境**：用 `uv venv .venv -p 3.12` + `source .venv/bin/activate` 建虚拟环境。
3. **装包**：执行 `uv pip install -v -e .`，**记录日志里出现的 3 个最重的依赖**（提示：大概率是 `torch`、`sglang`、`flash-attn-4`）。
4. **验约束**：用 `uv pip list` 核对这 3 个依赖的实际版本，对照 [pyproject.toml:L12-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L12-L78) 的约束，确认全部落在区间内。
5. **验入口**：执行 `sgl-omni --help`，确认能列出 `serve` 和 `config` 两个子命令。

> 完成标志：你能在不查文档的情况下，向同事解释「为什么 omni 镜像 FROM sglang 镜像」「为什么 nixl 要带 cu13 后缀」「sgl-omni 命令是哪里来的」这三件事。

> 待本地验证：第 3 步的实际耗时与最重依赖取决于网络与机器，请在本地观察真实结果。

## 6. 本讲小结

- SGLang-Omni 提供 **Docker（推荐）** 和 **手动** 两条安装路径；Docker 镜像 `lmsysorg/sglang-omni:dev` 以 SGLang 镜像为基底、额外编译 UCX 1.20.x，体现了「与 SGLang 组合」的关系。
- 无论哪条路径，Python 侧都用 `uv venv .venv -p 3.12` 建环境、`uv pip install -v -e .` 做可编辑安装。
- `pyproject.toml` 把 `torch==2.11.0`、`sglang==0.5.12.post1`、`transformers==5.6.0`、`flash-attn-4`、`kernels` 锁成一个必须整体升级的紧耦合「栈」，版本约束背后都有明确的技术原因。
- 跨阶段传输依赖 `nixl-cu13`、`mooncake-transfer-engine-cuda13` 必须**点名用 CUDA 13 专用 wheel**，否则在 cu130 环境会 import 崩溃。
- 可选依赖 `[audar-tts]` 是按需加载的模型家族扩展；`[tool.uv]` 用 override 兜底传递依赖冲突。
- CLI 入口 `sgl-omni` 由 `[project.scripts]` 声明、Typer 实现，含 `serve` 和 `config` 两个子命令；`sgl-omni --help` 是最直接的安装成功冒烟测试。

## 7. 下一步学习建议

- 装好之后，建议先读 [u1-l3 目录结构与代码组织](u1-l3-directory-layout.md)，把 `sglang_omni/` 下各子包的职责对号入座，为阅读源码建立「地图」。
- 接着进入 [u1-l4 启动 API Server 与第一次请求](u1-l4-start-server-first-request.md)，真正用 `sgl-omni serve` 跑通一次请求。
- 想深入理解本讲提到的「跨阶段传输」是怎么工作的，可以在学完进阶单元后回头看 [pyproject.toml:L34-L35](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L34-L35) 对应的 `relay` 与 `comm` 子包。
- 若你对依赖管理本身感兴趣，可对比阅读 [pyproject.toml:L90-L93](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L90-L93) 的 `[tool.uv]` 段，理解 uv 的 override 机制如何解决传递依赖冲突。
