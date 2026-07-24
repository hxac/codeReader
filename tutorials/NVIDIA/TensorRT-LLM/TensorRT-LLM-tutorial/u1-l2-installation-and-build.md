# 安装、容器与从源码构建

## 1. 本讲目标

上一讲（u1-l1）我们建立了 TensorRT-LLM 的「高空视图」：它是一个用专用 kernel、高效运行时与可扩展 Python 框架来优化 LLM 推理的库，遵循「Python 调度、C++ 加速」的设计。本讲则要解决一个更落地的问题——**怎么把它真正装到机器上并跑起来**。

学完本讲，你应当能够：

1. 说出 TensorRT-LLM 对 CUDA / PyTorch / Python / MPI 等关键依赖的版本要求，并判断本机是否满足。
2. 用「预构建容器」「pip 安装」「从源码构建」三种方式之一完成安装，并知道每种方式适合什么场景。
3. 读懂 `requirements.txt`、`pyproject.toml`、`setup.py` 这三个构建元数据文件，理解依赖是如何被声明、约束和打包的。
4. 定位从源码构建 C++ 组件的入口（`scripts/build_wheel.py`）与关键构建标志，了解 `TRTLLM_USE_PRECOMPILED` 这种「只改 Python、复用预编译二进制」的快速开发路径。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

- **什么是 wheel（`.whl`）？** Python 的标准二进制分发包格式。pip 安装一个库时，本质就是下载 wheel 并解压到 `site-packages`。TensorRT-LLM 的 wheel 里既有纯 Python 代码，也有编译好的 C++/CUDA 动态库（`.so` 文件），所以它是一个**二进制发行包**而不是纯 Python 包。
- **Python 版本约束（PEP 440）**：你会在依赖里看到形如 `torch>=2.11.0,<=2.13.0a0` 的写法。这表示「版本 ≥ 2.11.0 且 ≤ 2.13.0a0」的闭区间，`a0` 是 alpha 预发布版本。多个约束用逗号连接表示「同时满足」。`python_version >= "3.10"` 则是「仅当 Python 解释器版本 ≥ 3.10 才安装」的环境标记（marker）。
- **CUDA 与 GPU 架构**：CUDA 是 NVIDIA GPU 的并行计算平台；不同代际的 GPU（Ampere/Ada/Hopper/Blackwell）对应不同的「计算能力」（如 Hopper 是 `sm_90`）。编译 C++/CUDA 代码时要指定目标架构，否则会编译所有架构、非常耗时。
- **pip constraints（约束）文件**：用 `-c constraints.txt` 给 pip 指定「把这些包锁在某个版本」，常用于防止 pip 在解决依赖时擅自升级或降级某个关键包（本讲后面会看到锁住 `torch` 版本的实际例子）。
- **容器（Docker / NGC）**：容器把「操作系统 + 驱动 + 编译器 + 依赖」打包成一个可复制的环境。NVIDIA 的 NGC（GPU Catalog）提供了预先配好 CUDA、PyTorch 等的镜像，让你跳过繁琐的环境配置。

最后一点非常关键，承接上一讲：TensorRT-LLM 是「Python + C++ 混合」的库，**Python 部分可以通过 pip 直接装，但 C++/CUDA 部分要么来自预编译好的 wheel，要么需要在带 GPU 编译工具链的环境里从源码编译**。理解这一点，三种安装方式的区别就迎刃而解了。

## 3. 本讲源码地图

本讲涉及的文件都集中在仓库根目录和 `docs/source/installation/` 下：

| 文件 | 作用 |
|------|------|
| [requirements.txt](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt) | 运行时依赖清单，pip 安装时的「购物列表」 |
| [pyproject.toml](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/pyproject.toml) | 构建系统声明（build-system）与 lint/format/类型检查配置 |
| [setup.py](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py) | 真正的打包脚本：解析依赖、决定版本、声明入口命令、处理预编译二进制 |
| [tensorrt_llm/version.py](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/tensorrt_llm/version.py) | 单一版本号来源（`__version__`） |
| [docs/source/installation/installation-guide.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md) | 三种安装方式的官方指南（容器 / pip / 源码） |
| [docs/source/installation/containers.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/containers.md) | 三类容器镜像（devel / wheel / release）的说明 |
| [docs/source/installation/build-from-source.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/build-from-source.md) | 从源码构建的分步指南与 `build_wheel.py` 关键标志 |

> 备注：`scripts/build_wheel.py` 是从源码编译 C++ 的真正入口，但它不在本讲「关键源码」清单里——我们重点理解它**怎么被调用、有哪些关键标志**，而不是逐行读它的实现（那是后续讲义的内容）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**依赖声明**、**安装指南**、**构建脚本**。三者层层递进——先看依赖「要什么」，再看官方文档「怎么装」，最后看构建脚本「打包逻辑如何运作」。

### 4.1 依赖声明：requirements.txt 与 pyproject.toml

#### 4.1.1 概念说明

一个 Python 项目的「依赖」通常写在 `requirements.txt`（运行依赖清单）和 `pyproject.toml`（项目元数据与构建系统声明）里。TensorRT-LLM 的特殊之处在于：

- 它**强绑定**特定的 CUDA / PyTorch / NCCL / Triton 版本组合——因为这些库要和编译好的 C++/CUDA 二进制 ABI（应用二进制接口）对齐，版本错配会导致运行时崩溃。
- 它依赖大量 NVIDIA 生态包：`nvidia-modelopt`（量化）、`flashinfer-python`（注意力后端）、`nvidia-cutlass-dsl`（CuTe DSL 内核）、`nccl4py`（通信）等。

理解依赖声明，是为了在安装出问题时能快速定位「是哪个包的版本不对」。

#### 4.1.2 核心流程

`requirements.txt` 被 `setup.py` 读取，流程大致是：

```text
requirements.txt  ─┐
requirements-dev   ─┼─→ setup.py: parse_requirements() ─→ install_requires ─→ 写入 wheel ─→ pip install 时解析并拉取
constraints.txt    ─┘                                                extras_require(devel/mx)
```

- `requirements.txt` 里的 `-c constraints.txt` 会把约束信息一并合并进 `install_requires`。
- `--extra-index-url https://download.pytorch.org/whl/cu130` 告诉 pip：除了默认 PyPI，还要去 PyTorch 官方源拉 CUDA 13.0 版本的包。
- 最终这些依赖列表被 `setup()` 的 `install_requires=` 参数接收，固化进 wheel 的元数据。

#### 4.1.3 源码精读

先看 `requirements.txt` 的关键几行。第一行就揭示了「这是一个 CUDA 13 系列」的项目：

[requirements.txt:1-2](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L1-L2) —— 声明额外的 PyTorch CUDA 13.0 源，并引入约束文件 `constraints.txt`。

几条最关键的版本约束（决定能否安装成功）：

[requirements.txt:26](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L26) —— PyTorch 必须落在 `>=2.11.0, <=2.13.0a0` 区间，注释里还指明 NGC PyTorch 26.05 镜像用的是 `2.12.0a0`，说明了版本对齐的来源。

[requirements.txt:31](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L31) —— NCCL（多卡通信库）锁定在 `nvidia-nccl-cu13>=2.28.9,<=2.30.4`，注释明确「torch 2.11.0+cu130 依赖 nvidia-nccl-cu13==2.28.9」，体现了一环扣一环的 ABI 对齐。

[requirements.txt:35](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L35) 与 [requirements.txt:60](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L60) —— `transformers==5.5.4` 与 `flashinfer-python==0.6.15` 采用**精确钉版**（`==`），说明这两个上游接口变动频繁、必须严控版本。

[requirements.txt:76-77](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L76-L77) —— 带环境标记的依赖：`nvidia-cutlass-dsl[cu13]==4.5.0; python_version >= "3.10"`，表示仅 Python ≥ 3.10 才安装；下面一行注释说明 `nvidia-matmul-heuristics` 是 CuTe DSL autotuner 的 GEMM 启发式裁剪工具。

再看 `pyproject.toml` 的构建系统声明（这是 pip 知道「用什么来构建这个包」的入口）：

[pyproject.toml:4-6](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/pyproject.toml#L4-L6) —— `[build-system]` 声明用 `setuptools >= 64` 和 `pip >= 24` 作为构建后端。注意：TensorRT-LLM 用的是传统的 `setup.py` + setuptools，而不是更新的 PEP 621 `[project]` 表，所以真正的依赖、版本、入口命令都写在 `setup.py` 里。

> 小结：`pyproject.toml` 在这个项目里主要管「构建后端 + lint/格式化/类型检查（ruff/yapf/isort/mypy）配置」，而依赖与打包逻辑的实际控制权在 `setup.py`。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你学会从依赖清单里读出版本要求。

1. **实践目标**：判断「一台装了 CUDA 12.x、PyTorch 2.6、Python 3.11」的机器能否直接 pip 安装当前版本的 TensorRT-LLM。
2. **操作步骤**：
   - 打开 [requirements.txt](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt)，定位第 1 行（`cu130`）和第 26 行（torch 区间）。
   - 在本机执行（仅查询，不安装）：
     ```bash
     python3 -c "import sys, torch; print('python', sys.version.split()[0]); print('torch', torch.__version__); print('cuda', torch.version.cuda)"
     nvidia-smi | grep "CUDA Version"
     ```
3. **需要观察的现象**：本机 PyTorch 是否带 `+cu130` 后缀、`torch.version.cuda` 是否为 `13.x`、驱动支持的 CUDA 版本是否 ≥ 13.2。
4. **预期结果**：上述「CUDA 12 / PyTorch 2.6」的机器**不满足**当前 HEAD 的要求——本项目已升级到 CUDA 13 系、PyTorch ≥ 2.11.0。需要先按本讲 4.2 节升级 PyTorch 到 `cu130` 构建，或直接用 NGC 容器。
5. 关于版本号本身：[tensorrt_llm/version.py:15](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/tensorrt_llm/version.py#L15) 显示当前版本为 `1.3.0rc23`（rc = release candidate 预发布）。

#### 4.1.5 小练习与答案

**练习 1**：`torch>=2.11.0,<=2.13.0a0` 允许安装 `torch==2.13.0` 正式版吗？为什么？

**参考答案**：不能。`<=2.13.0a0` 把上界钉在了 `2.13.0` 的 alpha 预发布版本，而 PEP 440 规则下 `2.13.0`（正式版）比 `2.13.0a0`（预发布版）**更晚**，因此正式版超出上界、被排除。这个写法的意图是「允许 2.13 的预发布版，但不允许 2.13 正式版之后任何更高的版本」。

**练习 2**：为什么 `transformers` 和 `flashinfer-python` 用 `==` 精确钉版，而 `torch` 用区间？

**参考答案**：`transformers`/`flashinfer` 这两个上游库接口与小版本强相关、容易引入不兼容改动，精确钉版最安全；`torch` 给了一个区间，是为了兼容 PyPI 公开版与 NGC 镜像内部版（如 `2.12.0a0`），同时通过约束文件锁住下限，兼顾灵活性与稳定性。

---

### 4.2 安装指南：三种方式与适用场景

#### 4.2.1 概念说明

官方文档把安装方式按「从简到繁」排成三种。理解它们的区别，本质是回答一个问题：**C++/CUDA 二进制从哪里来？**

- **方式一 预构建容器**：二进制 + Python + 全套依赖都在 NGC 镜像里，开箱即用。
- **方式二 pip 安装**：二进制被打包进 PyPI 上的 wheel，pip 负责拉取；但你得自己备好 CUDA、PyTorch、MPI 等前置条件。
- **方式三 从源码构建**：二进制由你在本地编译，最灵活但最重，适合要改 C++ 代码或贡献代码的开发者。

#### 4.2.2 核心流程

```text
┌─ 我只想跑模型？         → 方式一（容器）或 方式二（pip）
├─ 我想改 Python 代码？   → 方式二 + TRTLLM_USE_PRECOMPILED（复用预编译二进制）
└─ 我想改 C++/CUDA 或贡献？ → 方式三（从源码构建）
```

容器侧还分三种镜像，理解它们能帮你选对起点：

| 镜像 | 内容 | 适合谁 |
|------|------|--------|
| `devel` | 只有构建依赖，不含源码/wheel | 想挂载自己的源码、从源码构建 |
| `wheel` | 在 devel 基础上编译出 wheel（中间产物，不单独发布） | 通常不直接用 |
| `release` | 在 devel 基础上装好预编译 wheel，开箱即用 | 只想运行 |

#### 4.2.3 源码精读

官方总览把三种方式集中在一处：

[installation-guide.md:6](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L6) —— 明确「安装方式按从简到繁排列」，并提示先查 Supported Hardware 页确认 GPU 兼容（Blackwell / Hopper / Ada / Ampere）。

**方式一：预构建容器**——

[installation-guide.md:16-28](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L16-L28) —— `docker pull nvcr.io/nvidia/tensorrt-llm/release:x.y.z` 后用一长串 `docker run` 标志启动，并给出最小自检 `python3 -c "import tensorrt_llm"`。

> 这串 `docker run` 标志值得记住，后面从源码构建也要复用：`--ipc host`（避免多进程共享内存 `Bus error`）、`--gpus all`（透传 GPU）、`--ulimit memlock=-1 --ulimit stack=67108864`（无限锁定内存 + 64MB 栈，防止深层 C++/CUDA 调用栈溢出）。

**方式二：pip 安装**——核心是「先备前置依赖，再装 wheel」：

[installation-guide.md:40-54](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L40-L54) —— 前置条件：安装 CUDA Toolkit 13.2 并设好 `CUDA_HOME`；手动装 PyTorch CUDA 13.0 版（`pip3 install torch==2.11.0 torchvision --index-url https://download.pytorch.org/whl/cu130`，与 4.1 节看到的 `cu130` 源呼应）；装 `libopenmpi-dev`（MPI 多卡所需），分离式服务还需 `libzmq3-dev`。

[installation-guide.md:74-76](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L74-L76) —— 真正的安装命令：`pip3 install --ignore-installed pip setuptools wheel && pip3 install tensorrt_llm`。先升级 pip/setuptools/wheel 是为了避免老版本 pip 解析依赖出问题。

文档还专门提醒了一个**高频踩坑点**：

[installation-guide.md:109-119](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L109-L119) —— 在 Ubuntu 22.04 等系统上，pip 可能把已装的 `cu130` 版 PyTorch 卸掉、换成默认的 `cu128` 版，导致安装失效。解决办法是写一个约束文件把 torch 锁在当前版本：`echo "torch==$CURRENT_TORCH_VERSION" > /tmp/torch-constraint.txt`，再用 `-c` 引入。这正是「pip constraints 文件」的典型用法，呼应 4.1.2 节。

**方式三：从源码构建**——文档把细节交给单独页面：

[installation-guide.md:121-123](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L121-L123) —— 指向 Build from Source 页，并强调「只为想修改/定制/贡献的开发者」。

容器镜像的细节在 containers 页，三类镜像的对照表是理解容器体系的关键：

[containers.md:7-11](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/containers.md#L7-L11) —— 表格列出 `devel` / `wheel` / `release` 三阶段的用途与对应 NGC 镜像。注意 `wheel` 是中间阶段、不单独发布。

[containers.md:16-25](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/containers.md#L16-L25) —— `devel` 与 `release` 镜像可直接 pull：`docker pull nvcr.io/nvidia/tensorrt-llm/devel:x.y.z` / `release:x.y.z`。

#### 4.2.4 代码实践

**实践目标**：动手验证「方式一 容器」是最省事的路径，并对比它和 pip 的差异。

1. **操作步骤**（需要本机有 Docker + NVIDIA Container Toolkit；若没有则改为阅读型实践）：
   ```bash
   # 拉取 release 镜像（用最近可用版本替换 x.y.z）
   docker pull nvcr.io/nvidia/tensorrt-llm/release:x.y.z
   # 启动容器
   docker run --rm -it --ipc host --gpus all \
       --ulimit memlock=-1 --ulimit stack=67108864 \
       -p 8000:8000 nvcr.io/nvidia/tensorrt-llm/release:x.y.z
   # 进入容器后自检
   python3 -c "import tensorrt_llm; print(tensorrt_llm.__version__)"
   ```
2. **需要观察的现象**：容器内 `import tensorrt_llm` 无报错，并能打印出版本号（与 4.1 节 `version.py` 的 `1.3.0rc23` 一致或为对应镜像版本）。
3. **预期结果**：容器内开箱即用，无需手动装 CUDA/PyTorch；这正是容器相对 pip 的核心优势。
4. **若无法运行**：明确写「待本地验证」。退而求其次，阅读 [containers.md:84-103](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/containers.md#L84-L103) 的 `make -C docker release_build` 一键构建 release 镜像流程，理解 `CUDA_ARCHS="89-real;90-real"` 参数如何把编译限制在 Ada+Hopper 以加速构建。

#### 4.2.5 小练习与答案

**练习 1**：你只想快速试用 TensorRT-LLM 跑一个模型，本机是干净的 Ubuntu + NVIDIA 驱动，哪种方式最省事？

**参考答案**：方式一（预构建 release 容器）。它自带 CUDA、PyTorch、MPI 和编译好的二进制，`docker run` 后即可 `import tensorrt_llm`，完全跳过前置依赖配置。pip 方式需要你自己装 CUDA Toolkit 13.2、cu130 版 PyTorch、MPI 等。

**练习 2**：为什么 `docker run` 必须加 `--ipc host`？

**参考答案**：TensorRT-LLM 运行时（尤其在多进程/多卡、分离式服务场景）依赖共享内存做进程间通信。容器默认的 IPC 命名空间限制过小，会触发 `Bus error (core dumped)`。`--ipc host` 让容器共享宿主机的 IPC 命名空间，文档在 build-from-source 页的「Note on Docker flags」里也明确强调了这一点。

---

### 4.3 构建脚本：setup.py 与 build_wheel.py

#### 4.3.1 概念说明

`setup.py` 是 setuptools 的打包入口。当 pip 构建或安装这个包时，会执行 `setup.py`，由里面的 `setup(...)` 调用来决定：包名叫什么、版本是多少、要打哪些文件进 wheel、声明哪些命令行入口（console scripts）、列出哪些运行依赖。

TensorRT-LLM 的 `setup.py` 有两个特别之处：

1. 它是一个**二进制发行**（`BinaryDistribution`）：因为 wheel 里必须包含 `.so` 动态库，所以强制 setuptools 当作「带扩展模块的包」处理。
2. 它支持 **TRTLLM_USE_PRECOMPILED** 机制：当设置这个环境变量时，`setup.py` 会去下载一份官方预编译 wheel，把里面的二进制抽出来铺到工作目录，从而**跳过本地 C++ 编译**——这是「只改 Python 代码」时的极速开发路径。

而真正负责编译 C++/CUDA 的是 `scripts/build_wheel.py`（本讲只了解它的调用方式）。

#### 4.3.2 核心流程

```text
pip install tensorrt_llm
   │
   ├─ 正常路径: 从 PyPI 下载官方预编译 wheel（二进制已含在里面）→ 直接安装
   │
   └─ 开发路径 (pip install -e . 或 .):
         setup.py 运行
            ├─ TRTLLM_USE_PRECOMPILED 已设置?
            │     是 → download_precompiled() / extract_from_precompiled() 把二进制铺进工作目录
            │     否 → 需要工作目录里已有编译产物（由 scripts/build_wheel.py 生成）
            ├─ sanity_check(): 确认 bindings/ 模块存在（否则提示先跑 build_wheel.py）
            ├─ get_version(): 读 version.py；editable 模式追加 git commit hash
            └─ setup(...): 打包 Python + 二进制 + 声明入口命令/依赖
```

#### 4.3.3 源码精读

先看版本号如何确定。`setup.py` 从 `version.py` 读取，editable 安装时还会追加 git commit：

[setup.py:63-93](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L63-L93) —— `get_version()` 打开 `tensorrt_llm/version.py` 取 `__version__`；若是 `develop`/`editable_wheel` 模式，用 `git rev-parse --short=10 HEAD` 取短 commit，拼成形如 `1.3.0rc23+58d8964d13` 的 PEP 440 本地版本段，方便区分开发装。

打包前的完整性检查——确保 C++ 绑定已存在：

[setup.py:53-60](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L53-L60) —— `sanity_check()` 检查 `tensorrt_llm/bindings` 目录是否存在；若不存在就抛错，并直接给出可操作提示：「请先执行 `scripts/build_wheel.py`，再 `pip install -e .`」。这条错误信息本身就是一份微型排错指南。

**TRTLLM_USE_PRECOMPILED 机制**——避免本地编译的关键：

[setup.py:368-380](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L368-L380) —— 读取环境变量 `TRTLLM_USE_PRECOMPILED` 和 `TRTLLM_PRECOMPILED_LOCATION`，只要二者任一被设置就进入「复用预编译」分支；若只给 `=1` 则用当前 `version.py` 的版本号去 PyPI 下载匹配 wheel（`download_precompiled`），再 `extract_from_precompiled` 把二进制解到工作目录。

[setup.py:193-212](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L193-L212) —— `download_precompiled()` 用 `pip download tensorrt_llm==<version> --extra-index-url=https://pypi.nvidia.com` 拉取官方 wheel，体现了「TensorRT-LLM 的官方预编译 wheel 发布在 NVIDIA 的 PyPI 镜像上」。

入口命令（console scripts）——这就是 `trtllm-serve` 等命令的来源：

[setup.py:467-473](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L467-L473) —— 声明三个命令行入口：`trtllm-bench`（基准，见 u11-l3）、`trtllm-serve`（在线服务，见 u1-l3/u11-l1）、`trtllm-eval`（评测），分别映射到 `tensorrt_llm.commands` 下的 `main` 函数。装好包后这些命令就会出现在终端里。

可选依赖与 Python 版本要求：

[setup.py:475-478](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L475-L478) 与 [setup.py:483](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L483) —— `extras_require` 提供 `devel`（开发依赖）和 `mx`（`modelexpress==0.4.1`，用于定义模型）两个可选附加；`python_requires=">=3.10, <4"` 限定 Python 解释器版本。classifiers 里也标注支持 Python 3.10 与 3.12。

最后，`build_wheel.py` 的关键标志（来自 build-from-source 文档，本讲理解「怎么用」即可）：

[build-from-source.md:65-78](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/build-from-source.md#L65-L78) —— 典型开发构建：`python3 scripts/build_wheel.py --use_ccache -a "90-real" --skip_building_wheel --linking_install_binary` 再 `pip install -e .`。`--use_ccache` 用编译缓存加速增量重建；`-a "90-real"` 只编译 Hopper 架构以大幅缩短编译时间；`--skip_building_wheel` 跳过打包；`--linking_install_binary` 用软链接代替复制，改了 C++ 立刻生效。

[build-from-source.md:90-98](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/build-from-source.md#L90-L98) —— 纯 Python 开发的最快路径：`TRTLLM_USE_PRECOMPILED=1 pip install -e .`，下载匹配 `version.py` 的预编译 wheel、抽取二进制，**完全不编译 C++**。可用 `TRTLLM_USE_PRECOMPILED=x.y.z` 指定版本，或 `TRTLLM_PRECOMPILED_LOCATION=<url/路径>` 指定来源。

#### 4.3.4 代码实践

**实践目标**：通过 `setup.py` 理解「我装完包之后，`trtllm-serve` 命令是怎么出现在终端里的」。

1. **操作步骤**：
   - 阅读 [setup.py:467-473](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/setup.py#L467-L473)，找到三个 `console_scripts` 入口。
   - 在已安装好 TensorRT-LLM 的环境（容器或 pip 装好）里执行：
     ```bash
     which trtllm-serve trtllm-bench trtllm-eval
     trtllm-serve --help | head -n 20
     ```
2. **需要观察的现象**：这三个命令都在可执行路径里，且 `trtllm-serve --help` 能打印帮助——说明 pip 安装时确实执行了 `setup.py` 的 `entry_points`，生成了对应的可执行脚本。
3. **预期结果**：命令路径通常在 `site-packages` 同级的 `bin/` 下；帮助信息会显示 serve 子命令。
4. **若未安装**：明确写「待本地验证」。可改为阅读 [tensorrt_llm/commands/serve.py](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/tensorrt_llm/commands/serve.py) 的 `main` 函数，确认它正是 entry_point 指向的目标。

#### 4.3.5 小练习与答案

**练习 1**：你只想改一行 Python 代码看效果，完全不想等 C++ 编译，应该用什么命令？

**参考答案**：`TRTLLM_USE_PRECOMPILED=1 pip install -e .`。它复用官方预编译 wheel 里的二进制、跳过本地 C++ 编译，并以 editable 模式安装，Python 改动立即生效。

**练习 2**：`sanity_check()` 在什么情况下会报错？报错时它给出的建议是什么？

**参考答案**：当 `tensorrt_llm/bindings` 目录（C++ 绑定模块）不存在时报错。它建议：「请先执行 `scripts/build_wheel.py`，再运行 `pip install -e .`」。这通常发生在未设置 `TRTLLM_USE_PRECOMPILED`、又没有先编译 C++ 就直接 editable 安装时。

**练习 3**：editable 安装（`pip install -e .`）后的版本号长什么样？为什么？

**参考答案**：形如 `1.3.0rc23+58d8964d13`——`+` 后面是 10 位 git 短 commit hash。`get_version()` 检测到 `develop`/`editable_wheel` 参数时会追加这个 PEP 440 本地版本段，便于区分同一基线版本的不同开发安装。

---

## 5. 综合实践

把三个模块串起来，完成一次「为本机选择并实施安装方案」的小任务。

**任务**：写一份一页纸的《我的环境安装方案》，要求：

1. **盘点本机环境**：运行 `python3 --version`、`nvidia-smi`（看驱动支持的 CUDA 版本）、`python3 -c "import torch; print(torch.__version__, torch.version.cuda)"`（若有 torch）、`docker --version`。
2. **对照要求**：参照 [requirements.txt:1](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L1) 与 [requirements.txt:26](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/requirements.txt#L26)（cu130、torch ≥ 2.11.0）、[installation-guide.md:40-54](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L40-L54)（CUDA Toolkit 13.2、libopenmpi-dev），逐项判断「满足 / 不满足 / 待升级」。
3. **做出选择并说明理由**：从「pip 安装 / 容器 / 源码构建」三选一，并写出适用场景。例如：
   - 只想跑模型、本机 CUDA 不匹配 → 选 **release 容器**（理由：跳过前置依赖）。
   - 想改 Python 代码、本机能装 cu130 torch → 选 **pip + `TRTLLM_USE_PRECOMPILED=1 pip install -e .`**（理由：免编译、改动即生效）。
   - 要改 C++/CUDA 内核 → 选 **源码构建**（`build_wheel.py -a "<arch>" --use_ccache`）。
4. **列出风险点**：参照 [installation-guide.md:109-119](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/installation/installation-guide.md#L109-L119) 写出 pip 方式可能踩的「torch 被 cu128 替换」坑及用 constraints 文件的对策。

**交付物**：一段 Markdown，包含「环境盘点表 + 选定方案 + 命令清单 + 风险对策」。完成后你就具备了为任意一台 NVIDIA 机器规划 TensorRT-LLM 安装的能力。

## 6. 本讲小结

- TensorRT-LLM 是 **Python + C++/CUDA 混合**库：Python 可 pip 装，C++ 二进制要么来自预编译 wheel，要么本地编译——这是理解三种安装方式的钥匙。
- **依赖强绑定** CUDA 13 系列（`cu130`）、PyTorch `>=2.11.0,<=2.13.0a0`、Python `>=3.10`，关键包（transformers/flashinfer）精确钉版，版本错配是安装失败的主因。
- **三种安装方式**按从简到繁：预构建容器（开箱即用）→ pip 安装（需自备 CUDA/PyTorch/MPI）→ 从源码构建（最灵活，面向 C++ 开发/贡献者）。
- **容器三镜像** `devel`（仅依赖，挂源码自建）/ `wheel`（中间产物）/ `release`（开箱即用），`docker run` 必须带 `--ipc host` 等标志。
- **`setup.py`** 是打包核心：解析依赖、确定版本（editable 追加 commit hash）、声明 `trtllm-serve` 等命令入口、强制按二进制包处理。
- **`TRTLLM_USE_PRECOMPILED=1 pip install -e .`** 是纯 Python 开发的极速路径，复用预编译二进制、跳过本地编译。

## 7. 下一步学习建议

装好之后，下一步自然是**真正跑通第一个模型**，这正是下一讲 **u1-l3「首次运行：LLM API 与 trtllm-serve」** 的内容——你会用 Python LLM API 离线推理、用 `trtllm-serve` 启动 OpenAI 兼容服务，并理解 tokenizer/detokenizer 如何被 LLM 托管。

如果你对打包/构建的工程细节更感兴趣，可以提前扩展阅读：

- [scripts/build_wheel.py](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/scripts/build_wheel.py) —— 看 C++ 编译的完整流程与所有构建标志（后续涉及 C++ 的讲义会用到）。
- [cpp/tensorrt_llm/CMakeLists.txt](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/cpp/tensorrt_llm/CMakeLists.txt) —— C++ 侧的 CMake 构建配置（对应 u2-l3「C++ 核心与 nanobind 绑定」）。
- [docs/source/supported-hardware.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/supported-hardware.md) —— 确认你的 GPU 架构（Blackwell/Hopper/Ada/Ampere）与 `-a` 标志取值的对应关系。
