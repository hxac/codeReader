# 环境搭建与安装

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 slime 推荐的**三种安装入口**（官方 Docker 镜像、`build_conda.sh`、`pip` 可编辑安装）各自的定位与适用场景。
- 看懂 [setup.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py) 的打包配置：包名、版本、`python_requires`、打包范围与依赖来源。
- 理解 [requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt) 为什么**只列纯 Python 依赖**，而把 torch / SGLang / Megatron-LM / flash-attn 等重型 CUDA 库排除在外。
- 明白 slime 对 **Megatron-LM**（通过 `PYTHONPATH` 或可编辑安装）和 **SGLang** 的依赖关系。
- 在一个干净环境里用 `pip install -e . --no-deps` 把 slime 装成可编辑包，并验证 `import slime` 成功。

承接上一讲：你已经知道 slime 的核心包是 `slime/` + `slime_plugins/`，入口是 `train.py` / `train_async.py`。本讲解决「这些代码到底依赖什么、怎么装起来」。

---

## 2. 前置知识

在动手之前，先建立三个直觉。本讲不会用到高深的 RL 概念，但需要你理解下面的工程概念：

- **Python 包（package）与可编辑安装（editable install）**：`pip install -e .`（`-e` 表示 editable）会把当前目录「软链接」进 Python 环境的 `site-packages`，于是你修改源码后无需重装即可生效。这对阅读源码、边改边试非常重要。slime 在镜像和 conda 脚本里都用这种方式安装自己。
- **纯 Python 依赖 vs. 原生（native/CUDA）依赖**：像 `requests`、`pyyaml` 这种装上就能用的叫纯 Python 依赖；而 `torch`、`flash-attn`、`transformer_engine` 需要和 CUDA 版本、GPU 架构、编译器匹配，一旦装错版本会运行时报错。slime 故意把这两类分开管理。
- **PYTHONPATH**：一个环境变量，Python 启动时会把它里面的目录加到模块搜索路径最前面。slime 用它来定位「放在仓库之外」的 Megatron-LM 源码。

一句话总结 slime 安装的核心矛盾：**slime 自己是纯 Python，但它必须和一整套被精确定版（pinned）的 CUDA 原生库共存**。本讲其余部分都在解释这个矛盾怎么解决。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [setup.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py) | slime 的打包配置 | 包名、版本、`python_requires`、打包范围、依赖来源 |
| [requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt) | 纯 Python 运行依赖列表 | 列了什么、**没列**什么（torch/SGLang/Megatron） |
| [build_conda.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh) | 非 Docker 下的 conda 全量构建脚本 | 如何从零搭起含原生库的完整环境 |
| [docker/Dockerfile](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/Dockerfile) | 官方镜像构建过程 | 镜像里预装了什么、slime 怎么被装进去 |
| [pyproject.toml](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml) | 构建后端与代码风格/测试配置 | 构建 backend、isort/black/ruff/pytest 配置 |
| [docs/en/get_started/quick_start.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md) | 官方快速上手文档 | Docker 镜像拉取与启动命令、硬件支持 |

> 提示：本讲引用的「源码」多为构建/打包脚本与文档，而非 Python 业务代码——因为「安装」本身就是由这些脚本定义的。

---

## 4. 核心概念与源码讲解

### 4.1 安装方式全景：三种入口与推荐顺序

#### 4.1.1 概念说明

slime 提供三条安装路径，复杂度递增，**强烈推荐优先用 Docker**：

1. **官方 Docker 镜像 `slimerl/slime:latest`（推荐）**：开箱即用，所有依赖、补丁、CUDA 原生库都已配齐。
2. **`build_conda.sh`（Docker 不便时）**：从零用 micromamba 搭建一个与镜像等价的 conda 环境，需要自己处理编译。
3. **`pip install -e . --no-deps`（仅升级 slime 自身）**：不安装任何依赖，只把 slime 这个纯 Python 包装成可编辑模式，用于在已有镜像里把 slime 升级到最新代码。

官方在快速上手文档里说得很直白：因为 slime 可能包含对 sglang/megatron 的临时补丁（patch），为避免环境踩坑，**强烈建议用最新 Docker 镜像**。

#### 4.1.2 核心流程

```text
你是新用户？
 ├─ 有 Docker + NVIDIA GPU？ ── 是 ──> docker pull slimerl/slime:latest（推荐）
 │                                        └─ 想升级 slime 源码：cd /root/slime && git pull && pip install -e . --no-deps
 ├─ 只能用 conda？ ───────────── 是 ──> bash build_conda.sh（从零构建原生库）
 └─ 只想在已有镜像里读/改源码？ ─────> pip install -e . --no-deps
```

#### 4.1.3 源码精读

官方镜像的拉取与启动命令在快速上手文档中给出：

[docs/en/get_started/quick_start.md:30-38](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L30-L38) — 拉取 `slimerl/slime:latest` 并以交互模式启动容器。注意几个关键启动参数：

- `--gpus all`：把宿主机全部 GPU 透传进容器。
- `--ipc=host` 与 `--shm-size=16g`：共享内存，分布式训练/推理靠它做进程间通信。
- `--ulimit memlock=-1 --ulimit stack=67108864`：放开内存锁与栈大小，NCCL 多卡通信需要。

镜像内 slime 已预装；要更新到最新版时：

[docs/en/get_started/quick_start.md:44-49](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L44-L49) — `cd /root/slime && git pull && pip install -e . --no-deps`。注意这里用的是 `--no-deps`：镜像里原生库的版本是精心锁定的，**绝不能让 pip 重新解析依赖去覆盖它们**。

镜像的标签体系在 [docker/README.md:27-30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/README.md#L27-L30) 说明：`slimerl/slime:latest` 跟踪 CUDA 12 构建；另外有 `-cu129`（CUDA 12.9）与 `-cu130`（CUDA 13，Blackwell 架构）后缀标签，与 SGLang 基础镜像对齐。

硬件支持方面（[docs/en/get_started/quick_start.md:12-24](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L12-L24)）：H 系列（H100/H200）有 CI 保护、推荐生产；B200 系列步骤相同、基本功能稳定但目前缺 CI 保护；AMD GPU 另见平台支持教程。

#### 4.1.4 代码实践

**实践目标**：亲手拉取并启动官方镜像，确认环境就绪。

**操作步骤**：

1. 确认宿主机已装 NVIDIA 驱动与 Docker（`nvidia-smi` 能看到 GPU）。
2. 执行：
   ```bash
   docker pull slimerl/slime:latest
   docker run --rm --gpus all --ipc=host --shm-size=16g \
     --ulimit memlock=-1 --ulimit stack=67108864 \
     -it slimerl/slime:latest /bin/bash
   ```
3. 进入容器后执行 `python -c "import slime; print('ok')"` 与 `nvidia-smi`。

**需要观察的现象**：容器进入后位于 `/root/slime`；`import slime` 不报错；`nvidia-smi` 列出 GPU。

**预期结果**：命令成功、看到 GPU 列表。若 `nvidia-smi` 在容器内失败，多半是宿主机的 NVIDIA Container Toolkit 未装。

**说明**：本实践需要真实 GPU 与 Docker 环境，结果「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么官方文档建议用 Docker 而不是直接 `pip install`？
  - **答案**：slime 依赖一组被精确定版的 CUDA 原生库（torch+cu129、sglang-kernel、flash-attn、transformer_engine 等）以及对 sglang/megatron 的临时补丁，手动拼装极易踩坑；镜像把这些全部固化，保证开箱一致。
- **练习 2**：`slimerl/slime:latest` 对应的是 CUDA 12 还是 CUDA 13 构建？
  - **答案**：CUDA 12。它跟踪 CUDA 12 构建；CUDA 13（Blackwell）用 `latest-cu130` 标签。

---

### 4.2 setup.py 打包配置

#### 4.2.1 概念说明

[setup.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py) 是 slime 作为 Python 包的「身份证」：它声明包名、版本、支持的 Python 版本、打包范围、以及依赖从哪里来。读懂它，你就知道 `pip install -e .` 到底做了什么。

#### 4.2.2 核心流程

`setup.py` 的执行逻辑可以概括为三步：

1. 用 `_fetch_requirements("requirements.txt")` 把依赖文件读成一个列表。
2. 定义一个自定义 `bdist_wheel` 类，强制把 wheel 标记为「非纯 Python」（`root_is_pure = False`），并打上平台标签。
3. 调用 `setup(...)` 把以上信息登记给 setuptools。

#### 4.2.3 源码精读

最关键的是 `setup(...)` 这一段：

[setup.py:32-50](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L32-L50) — 打包配置主体。逐项解读：

- `name="slime"`、`version="0.3.1"`：包名与版本。当前发布版本为 0.3.1（与 git 历史中的 `[release] bump to v0.3.1` 一致）。
- `packages=find_packages(include=["slime*", "slime_plugins*"])`：只打包 `slime` 与 `slime_plugins` 两个包树。这正对应上一讲说的「核心包 + 插件包」，而 `scripts/`、`tools/`、`examples/` 不被打包——它们是外围资源。
- `install_requires=_fetch_requirements("requirements.txt")`：依赖**直接读取 `requirements.txt`**，不重复维护一份列表。

依赖读取函数：

[setup.py:8-10](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L8-L10) — 逐行读 `requirements.txt`，去掉空行和以 `#` 开头的注释行。所以 `requirements.txt` 里的 `# needed for debugging...` 这种行内注释不会被当作依赖。

Python 版本约束：

[setup.py:40-48](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L40-L48) — `python_requires=">=3.10"`，classifiers 进一步标明支持 3.10 / 3.11 / 3.12，并把环境标记为 `GPU :: NVIDIA CUDA`、主题为 `Artificial Intelligence` 与 `Distributed Computing`。

自定义 wheel 类：

[setup.py:14-28](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L14-L28) — 把 wheel 设为非纯 Python（`root_is_pure = False`），并在 Linux 上打 `manylinux1_x86_64` 标签。这是因为 slime 内含需要编译的 C++/CUDA 扩展子目录（如 `slime/backends/megatron_utils/kernels/int4_qat`），打包时需要平台相关标签。

#### 4.2.4 代码实践

**实践目标**：记录 `setup.py` 中的版本号与 `python_requires`，并理解它们对安装的影响。

**操作步骤**：

1. 打开 [setup.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py)，记录 `version` 与 `python_requires`。
2. 在仓库根目录执行 `python -c "import sys; print(sys.version_info[:2])"` 确认当前 Python 版本 ≥ 3.10。
3. 执行 `python setup.py --version`（或 `pip show slime` 安装后）查看版本号。

**需要观察的现象**：版本号为 `0.3.1`；`python_requires` 为 `>=3.10`。

**预期结果**：在 Python 3.10/3.11/3.12 下可正常安装；低于 3.10 会被 pip 直接拒绝并报版本不符。

**说明**：命令执行结果「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：`find_packages(include=["slime*", "slime_plugins*"])` 排除了哪些目录？为什么要排除？
  - **答案**：排除了 `scripts/`、`tools/`、`examples/`、`tests/`、`docs/` 等。因为它们是启动脚本、工具或测试资源，不是被 `import` 的库代码，打进包里反而会污染命名空间。
- **练习 2**：为什么说 `install_requires` 与 `requirements.txt` 是「同一份数据」？
  - **答案**：`setup.py` 用 `_fetch_requirements` 直接读取 `requirements.txt`，二者不会脱节；维护依赖只需改一处。

---

### 4.3 requirements.txt 依赖列表

#### 4.3.1 概念说明

[requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt) 列出 slime 的**纯 Python 运行依赖**。理解这份清单的关键不是「列了什么」，而是「**故意没列什么**」——torch、SGLang、Megatron-LM、flash-attn、transformer_engine、apex 这些重型 CUDA 库一个都不在里面。

#### 4.3.2 核心流程

按职能把 `requirements.txt` 里的依赖分组：

| 职能 | 依赖 | 说明 |
| --- | --- | --- |
| 编排 | `ray[default]` | 用 Ray 做分布式 GPU 编排（对应 `slime/ray/`） |
| 推理路由 | `sglang-router>=0.3.0` | SGLang router 的 Python 侧 |
| LLM/Agent 客户端 | `openai`, `anthropic`, `openai-agents`, `httpx[http2]` | 调用各类 LLM 与 agent 协议 |
| 工具/沙箱协议 | `mcp[cli]`, `e2b` | MCP 协议、E2B 沙箱（agent RL 用） |
| 模型/数据 | `transformers`, `datasets`, `accelerate`, `safetensors` | 加载 HF 模型与数据集 |
| 日志/监控 | `wandb`, `tensorboard` | 训练日志 |
| 权重同步编解码 | `xxhash`, `blake3`, `zstandard` | disk delta 权重同步的校验和与压缩 |
| 视觉/数学 | `qwen_vl_utils`, `pillow`, `pylatexenc` | VLM 数据与数学答案解析 |
| 其他 | `numba`, `omegaconf`, `pyyaml`, `ring_flash_attn`, `blobfile`, `memray` | 数值、配置、注意力、调试 |

> 注意 `xxhash` 后面的注释 [requirements.txt:25](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt#L25)：明确标注用于 `disk delta weight sync (checksum + codec)`，这正是后面 U5「权重同步」会讲到的增量传输机制。

#### 4.3.3 源码精读

整份清单：

[requirements.txt:1-26](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt#L1-L26) — 全部 26 行纯 Python 依赖。注意几个细节：

- 第 9 行 `memray` 带行内注释 `# needed for debugging`，`_fetch_requirements` 会保留 `memray` 本身而剥离注释。
- 大多数依赖**没有版本锁定**（如 `transformers`、`ray[default]`），只有 `sglang-router>=0.3.0` 给了下限。这说明 slime 对纯 Python 依赖较宽容，真正的「硬版本约束」全部落在原生库上（由 Dockerfile / build_conda.sh 锁定）。
- **没有 `torch`、没有 `sglang`、没有 `megatron`、没有 `flash-attn`、没有 `transformer_engine`、没有 `apex`**。这不是遗漏，而是设计——它们的版本必须和 CUDA、GPU 架构、彼此之间严格匹配，写在 `requirements.txt` 里只会引发错误的自动升级。

#### 4.3.4 代码实践

**实践目标**：用对比法理解「纯 Python 依赖 vs. 原生依赖」的边界。

**操作步骤**：

1. 打开 [requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt)，把每个依赖按「纯 Python（装上即用）/ 原生（需 CUDA 编译）」两列归类。
2. 思考：为什么 `xxhash` 在这里，而 `torch` 不在？
3. 用 `pip download --no-deps xxhash -d /tmp/xxhash_pkg` 观察它只是一个轻量轮子，对比 torch 的体积。

**需要观察的现象**：`xxhash` 等是几 KB~几百 KB 的纯 Python/小轮子；torch 是几百 MB 的 CUDA 轮子。

**预期结果**：你会直观感受到把这两类混在一起管理是危险的，从而理解 slime 把原生库交给 Docker/conda 脚本统一锁版的用心。

**说明**：下载结果「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：`requirements.txt` 里唯一带版本下限的是哪个依赖？
  - **答案**：`sglang-router>=0.3.0`。因为 slime 用到了该 router 的特定能力（构建脚本里还断言 `sglang_router.__version__` 包含 `'slime'` 字样）。
- **练习 2**：如果有人在 `requirements.txt` 里加一行 `torch==2.11.0`，会发生什么风险？
  - **答案**：pip 会从 PyPI 默认源拉到 cu13 版的 torch，覆盖镜像里精心锁定的 `torch==2.11.0+cu129`，导致 CUDA 版本不匹配、运行时崩溃。这也是镜像里 slime 用 `--no-deps` 安装的根本原因。

---

### 4.4 build_conda.sh 与 Docker：重型依赖的构建

#### 4.4.1 概念说明

`requirements.txt` 故意不装原生库，那这些库从哪来？答案是 [build_conda.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh)（conda 路径）和 [docker/Dockerfile](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/Dockerfile)（Docker 路径）。这两份脚本本质做同一件事：**搭一个含精确定版原生库的环境，再把 slime 用 `--no-deps` 装进去**。读懂其中一份，就理解了 slime 的完整依赖拓扑。

#### 4.4.2 核心流程

`build_conda.sh` 的构建顺序可以拆成五段：

```text
1. 建 conda 环境      micromamba create -n slime python=3.12
2. 装 CUDA 原生底座   cuda=12.9.1 / nccl / cudnn / rust
3. 装 SGLang 全家桶    clone sglang -> editable 安装 -> force-reinstall torch+cu129/sglang-kernel/sgl-deep-gemm
                      -> transformer_engine / apex / flash-attn / flash-linear-attention
4. 装 Megatron-LM     clone Megatron-LM -> checkout 固定 commit -> pip install -e . --no-build-isolation
5. 装 slime + 补丁     pip install -r requirements.txt
                      pip install -e . --no-deps   ← 关键：先装纯 Python 依赖，再 no-deps 装 slime
                      打 sglang/megatron 补丁
```

关键版本在脚本顶部集中声明：

[build_conda.sh:30-34](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L30-L34) — `SGLANG_VERSION="v0.5.15.post1"`、`MEGATRON_COMMIT="1dcf0dafa884ad52ffb243625717a3471643e087"` 等。这些 commit 哈希是 slime 验证过的「黄金组合」，注释里还提醒要与 `docker/Dockerfile` 的对应 ARG 保持同步。

#### 4.4.3 源码精读

**(1) slime 自己怎么被装进去**——这是全脚本最值得记的两行：

[build_conda.sh:173-174](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L173-L174) — 先 `pip install -r requirements.txt` 装纯 Python 依赖，再 `pip install -e . --no-deps` 装 slime 本体。脚本上方的注释（[build_conda.sh:168-172](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L168-L172)）解释得很清楚：用 `--no-deps` 是为了防止 pip 重新解析依赖、覆盖前面精心锁定的原生库（torch+cu129、sglang-kernel+cu129…）。Dockerfile 里是同样的两步（[docker/Dockerfile:103-105](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/Dockerfile#L103-L105) 与 [docker/Dockerfile:149-152](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/Dockerfile#L149-L152)）。

**(2) Megatron-LM 如何成为可 import 的依赖**：

[build_conda.sh:154-163](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L154-L163) — 把 Megatron-LM 克隆到 `$BASE_DIR/Megatron-LM`，`git checkout` 到固定 commit，再 `pip install -e . --no-build-isolation`。这样 `import megatron` 就能直接工作。注意 `--no-build-isolation`：Megatron 的 `setup.py` 要编译 C++ 扩展（`megatron.core.datasets.helpers_cpp`），需要找到当前环境里已装的 pybind11，注释 [build_conda.sh:159-162](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L159-L162) 解释了不用隔离的原因。

除了「可编辑安装让 `import megatron` 生效」之外，slime 运行转换工具时还会用 `PYTHONPATH` 显式定位 Megatron 源码：

[docs/en/get_started/quick_start.md:86](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L86) — 运行 `tools/convert_hf_to_torch_dist.py` 时用 `PYTHONPATH=/root/Megatron-LM`。也就是说，Megatron-LM 在 slime 里**既可通过可编辑安装被 import，也可通过 PYTHONPATH 定位**——后者在你想换一个 Megatron 源码树时很方便。

**(3) SGLang 全家桶的精确定版**：

[build_conda.sh:71-76](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L71-L76) — force-reinstall `torch==2.11.0+cu129`、`sglang-kernel==0.4.4`、`sgl-deep-gemm==0.1.4`，全部从 `cu129` 索引拉取。注释 [build_conda.sh:56-63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L56-L63) 解释了为何要这样做：SGLang 的 editable 安装会拉入 cu13 的 nvidia 运行时库，必须强制换回 cu12 版本并修复 `site-packages/nvidia/*` 共享目录。

**(4) 补丁与最终自检**：

[build_conda.sh:188-228](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L188-L228) — 按 Dockerfile 相同顺序对 sglang 与 megatron 打补丁（`docker/patch/${PATCH_VERSION}/` 下的 `.patch` 文件）。脚本最后还做了一次健康自检：

[build_conda.sh:230-240](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh#L230-L240) — 断言 `torch/torchaudio/torchvision` 版本精确等于 `2.11.0+cu129`，并断言 `torch.ops.torchvision.nms` 存在。这等于在构建末尾给环境盖了个「合格章」。

#### 4.4.4 代码实践

**实践目标**：验证 slime 用 `--no-deps` 可编辑安装并成功导入（本讲义规格指定的核心实践）。

**操作步骤**：

1. 进入一个干净环境（最好就是官方镜像，或已按 `build_conda.sh` 搭好的 conda 环境），确认 Python ≥ 3.10：
   ```bash
   python -c "import sys; print(sys.version_info[:2])"
   ```
2. 进入 slime 仓库根目录，执行：
   ```bash
   pip install -e . --no-deps
   ```
3. 验证导入与版本：
   ```bash
   python -c "import slime; print('slime import OK ->', slime.__file__)"
   pip show slime | grep -E "Version|Requires-Python|Location"
   ```

**需要观察的现象**：

- 第 2 步因为 `--no-deps`，pip 只处理 slime 一个包，几乎瞬间完成、不会去拉 torch 等。
- 第 3 步 `import slime` 成功（因为 [slime/__init__.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/__init__.py) 是空文件，导入无副作用），并打印出仓库内的真实路径（可编辑安装的特征）。
- `pip show` 显示 `Version: 0.3.1`、`Requires-Python: >=3.10`。

**预期结果**：`import slime` 不报错；版本 `0.3.1`；`python_requires` 为 `>=3.10`。

**说明**：本实践需要真实 Python 环境，命令执行结果「待本地验证」。若在没有原生库的纯净环境里执行第 2 步，slime 能装上、`import slime` 也能成功，但**真正跑训练时**仍会因缺 torch/sglang/megatron 而失败——这恰好印证了「slime 自己是纯 Python，但运行离不开原生库」。

#### 4.4.5 小练习与答案

- **练习 1**：`build_conda.sh` 里为什么要先 `pip install -r requirements.txt`、再 `pip install -e . --no-deps`，而不是直接 `pip install -e .`（让 pip 自动装依赖）？
  - **答案**：直接 `pip install -e .` 会让 pip 按依赖图重新解析，可能从 PyPI 默认源拉到 cu13 版的 torch 等覆盖已锁定的 cu129 版本。先装纯 Python 依赖、再用 `--no-deps` 装 slime，可以保证原生库版本不被破坏。
- **练习 2**：Megatron-LM 在 slime 环境里有哪两种被「找到」的方式？
  - **答案**：(a) 把 Megatron-LM 用 `pip install -e .` 装成可编辑包，使 `import megatron` 直接可用；(b) 运行 `tools/convert_*.py` 等脚本时用 `PYTHONPATH=/root/Megatron-LM` 显式指向源码树。
- **练习 3**：脚本末尾的 `assert torch.__version__ == "2.11.0+cu129"` 起什么作用？
  - **答案**：构建末尾的健康自检，确认前面那一长串 force-reinstall 确实把 torch 锁定到了 cu129 版本，没被 SGLang editable 安装带入的 cu13 版本污染。

---

## 5. 综合实践

**任务**：为 slime 的三种安装方式做一份「决策与依赖清单」。

1. 画一张表，三列分别是 **Docker / conda / pip-only**，行包括：适用场景、是否自带原生库、是否自带 sglang/megatron 补丁、典型命令、能否直接跑训练。
2. 基于 [requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt) 与 [build_conda.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh)，列出 slime「运行训练」实际需要、但**不在** `requirements.txt` 里的 5 个关键原生依赖（提示：torch、sglang、sglang-kernel、Megatron-LM、flash-attn / transformer_engine / apex 任选）。
3. 在官方镜像内执行 4.4.4 的 `pip install -e . --no-deps` + `import slime` 验证，并把 `pip show slime` 的输出贴进你的笔记。

这个任务把本讲的三个最小模块（requirements 依赖列表 / setup 打包配置 / build_conda 环境）串成一张完整的「依赖从哪来、怎么装」的图，也是下一讲「运行第一个训练」的前置准备。

---

## 6. 本讲小结

- slime 推荐**三种安装入口**：Docker 镜像 `slimerl/slime:latest`（首选）、`build_conda.sh`（无 Docker）、`pip install -e . --no-deps`（仅升级 slime 自身）。
- [setup.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py) 声明：包名 `slime`、版本 `0.3.1`、`python_requires=">=3.10"`、打包范围为 `slime*` 与 `slime_plugins*`，依赖直接读 `requirements.txt`。
- [requirements.txt](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt) **只列纯 Python 依赖**（ray、sglang-router、transformers、wandb、xxhash 等），故意**不列** torch/sglang/megatron/flash-attn 等重型 CUDA 库。
- 原生库由 [build_conda.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/build_conda.sh) / [docker/Dockerfile](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docker/Dockerfile) 统一锁定到「黄金组合」（如 sglang `v0.5.15.post1`、Megatron 固定 commit、torch `2.11.0+cu129`）。
- slime 依赖 **Megatron-LM**（可编辑安装或 `PYTHONPATH=/root/Megatron-LM`）与 **SGLang**；二者还需打 `docker/patch/` 下的临时补丁。
- 无论 Docker 还是 conda，slime 都用「先 `pip install -r requirements.txt`、再 `pip install -e . --no-deps`」的两步法，防止 pip 覆盖已锁定的原生库。

---

## 7. 下一步学习建议

- 下一讲 **u1-l4 运行第一个训练：脚本与参数** 将带你解析 `scripts/run-qwen3-4B.sh`，把本讲装好的环境真正跑起来。建议先在镜像里完成本讲的 `import slime` 验证。
- 如果你想深入「为什么 Megatron 需要 torch_dist 格式、为何要转权重」，可以先跳到 **u1-l5 模型权重转换**，它解释了 [tools/convert_hf_to_torch_dist.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py) 与 `PYTHONPATH=/root/Megatron-LM` 的配合。
- 对构建系统本身感兴趣的读者，可继续阅读 [pyproject.toml](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml)（构建后端 setuptools、isort/black/ruff 风格、pytest markers），它在 **u8-l3 参数体系** 与 **u8-l6 测试与 CI** 中会再次出现。
