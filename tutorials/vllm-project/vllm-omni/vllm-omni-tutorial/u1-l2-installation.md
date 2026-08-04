# 安装与环境：从源码安装 vLLM-Omni

## 1. 本讲目标

学完本讲后，你应当能够：

- 在 NVIDIA CUDA 与 AMD ROCm 环境下，按照官方 quickstart 从源码完成 vLLM-Omni 的安装，并了解 NPU（华为昇腾）下「vLLM + vLLM-Ascend + vLLM-Omni」三方钉版本的特殊流程；
- 说清楚「为什么 vLLM 与 vLLM-Omni 的主版本（major）与次版本（minor）必须一一对应」，并能在版本不一致时识别告警；
- 看懂 `setup.py` 是如何根据硬件（CUDA/ROCm/NPU/XPU/MUSA/CPU）自动选择依赖文件的；
- 读懂 `pyproject.toml` 中的包元数据、`console_script`（`vllm-omni`）与可选依赖（extras）的组织方式；
- 定位 `vllm_omni/version.py` 中的版本对齐告警逻辑，并理解它为什么必须在 `patch` 之前执行。

本讲承接上一讲 [u1-l1 vLLM-Omni 是什么](u1-l1-project-overview.md)：上一讲建立了「vLLM-Omni 是在 vLLM 之上做增量扩展」的全局认知，本讲解决「我该怎样把它真正装到机器上跑起来」。

## 2. 前置知识

在开始之前，建议你先了解以下几个概念。它们都不复杂，但能帮你理解后面的命令和源码。

### 2.1 什么是「在 vLLM 之上扩展」

vLLM-Omni **不是一个独立的推理引擎**，而是依附于 vLLM 的一层扩展。换句话说，你必须先装好 vLLM，再装 vLLM-Omni。这就是为什么官方安装步骤永远是「先 `pip install vllm`，再 `pip install -e .`（vllm-omni）」两步。两者通过同一套 Python 运行时协同工作，因此版本必须对齐。

### 2.2 setuptools 与 pyproject.toml

Python 包通常用 `setuptools` 打包。现代做法是把「包的描述信息」（名字、作者、依赖、入口脚本）写在 `pyproject.toml` 里，把「构建时的动态逻辑」（比如这里「根据硬件选依赖」）写在 `setup.py` 里。`pyproject.toml` 是「静态名片」，`setup.py` 是「动态安装脚本」。

### 2.3 setuptools-scm 与版本号

`setuptools-scm` 是一个把**版本号直接从 git 标签（tag）推导出来**的工具。这样开发者每打一个 git tag，版本号就自动更新，不需要手动维护版本字符串。它的输出会写进一个自动生成的 `_version.py` 文件。

### 2.4 uv

`uv` 是一个用 Rust 写的、极快的 Python 环境与包管理工具。官方 quickstart 全程使用 `uv`（`uv venv`、`uv pip install`）。如果你只有 `pip`，把 `uv pip install` 替换为 `pip install` 即可，逻辑一致。

### 2.5 硬件后端（backend）

深度学习最终要跑在加速卡上。常见的有：

| 缩写   | 厂商              | torch 中的判断                       |
|--------|-------------------|--------------------------------------|
| CUDA   | NVIDIA            | `torch.version.cuda is not None`     |
| ROCm   | AMD               | `torch.version.hip is not None`      |
| NPU    | 华为昇腾          | `torch.npu.is_available()`           |
| XPU    | Intel             | `torch.xpu.is_available()`           |
| MUSA   | 摩尔线程          | `torch.musa.is_available()`          |

vLLM-Omni 需要为不同硬件安装不同的依赖，这正是 `setup.py` 要解决的核心问题。其中 NPU 比较特殊——它的 vLLM 能力由独立的 `vllm-ascend` 项目提供，所以 NPU 上要装的东西更多（见 4.1.3）。

## 3. 本讲源码地图

本讲涉及的关键文件如下（永久链接均指向当前 HEAD `5215e03a`）：

| 文件 | 作用 |
|------|------|
| [docs/getting_started/quickstart.md](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/quickstart.md) | 官方快速上手文档，给出 CUDA/ROCm 的标准安装命令与版本对齐说明。 |
| [docs/getting_started/installation/README.md](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/README.md) | 安装总览，按硬件平台（GPU/NPU）分流到具体子文档；顶部用一条 `!!! important` 写明版本对齐铁律（v0.26.0 新增）。 |
| [docs/getting_started/installation/gpu/cuda.inc.md](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/gpu/cuda.inc.md) | CUDA 下的硬件要求、wheel、源码、Docker 安装细节；Docker `BASE_IMAGE` 已对齐到 v0.26.0。 |
| [docs/getting_started/installation/gpu/rocm.inc.md](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/gpu/rocm.inc.md) | ROCm 下的安装细节（gfx942、`--no-build-isolation` 等）。 |
| [docs/getting_started/installation/npu/npu.inc.md](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/npu/npu.inc.md) | NPU 下的安装细节，含「vLLM v0.26.0 + vLLM-Ascend `releases/v0.26.0rc` + vLLM-Omni」三方从源码构建步骤（v0.26.0 更新）。 |
| [pyproject.toml](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml) | 包的静态元数据：名字、Python 版本、可选依赖、`console_script`、插件入口。 |
| [setup.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py) | 动态安装脚本：检测硬件、选择依赖文件、生成带设备后缀的版本号。 |
| [vllm_omni/version.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/version.py) | 读取 `_version.py`，并在导入时自动检查 vLLM 与 vLLM-Omni 版本是否对齐。 |
| [vllm_omni/__init__.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/__init__.py) | 包入口，规定了「先做版本检查，再打 patch」的导入顺序。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：安装总览、硬件感知安装机制、版本号生成与包元数据、版本对齐告警机制。

---

### 4.1 安装总览：前置条件与三步安装流程

#### 4.1.1 概念说明

vLLM-Omni 的安装可以概括成一句话：**先装对齐版本的 vLLM，再把 vLLM-Omni 以可编辑模式（`-e`）装进同一个环境**。

为什么要「可编辑模式」？因为 vLLM-Omni 还在快速演进，官方明确建议从源码安装，方便你随时 `git pull` 拿到最新代码、改动后立即生效，而不必反复重新打包。

从 v0.14.0 起，vLLM-Omni 形成了固定的发布节奏：**每个偶数号的 vLLM 次版本都对应一个 vLLM-Omni 稳定版**（0.16、0.18、0.20、0.22、0.26……）。因此「安装前先确认 vLLM-Omni 的版本，再装同主次的 vLLM」是第一条铁律——vLLM-Omni 0.26.x 必须配 vLLM 0.26.x。这一点在 v0.26.0 里被写进了安装文档最显眼的位置（见 4.1.3 的 `!!! important` 提示）。

#### 4.1.2 核心流程

标准 CUDA 安装的三步流程：

1. **建一个干净环境**：vLLM 会编译大量 CUDA kernel，与已有环境混装极易出问题，所以官方强调要「fresh new」环境。
2. **安装对齐版本的 vLLM**：用 `--torch-backend=auto` 让 uv 自动选匹配的 PyTorch 与 CUDA 组合。
3. **克隆并以可编辑模式安装 vLLM-Omni**：`uv pip install -e .`。

流程伪代码：

```
create_env(python==3.12)
install("vllm==0.26.0", torch_backend="auto")   # 必须与 vllm-omni 主次版本一致
clone("https://github.com/vllm-project/vllm-omni.git")
install_editable(".")                            # 触发 setup.py 的硬件感知逻辑
```

> **NPU（华为昇腾）略有不同**：它不是「vLLM + vLLM-Omni」两步，而是「vLLM + vLLM-Ascend + vLLM-Omni」三方都要钉在同一个 v0.26 发布线上。这是因为 NPU 上的 vLLM 能力由独立的 `vllm-ascend` 项目提供，vLLM-Omni 在其之上再叠加。具体命令见 4.1.3 的 NPU 源码段。

#### 4.1.3 源码精读

quickstart 文档给出了完整的安装命令。注意它对 CUDA 与 ROCm 分别给了一行 `vllm` 安装命令：

```bash
# On CUDA
uv pip install vllm==0.26.0 --torch-backend=auto

# On ROCm
uv pip install vllm==0.26.0+rocm723 --extra-index-url https://wheels.vllm.ai/rocm/0.26.0/rocm723
```

参见 [docs/getting_started/quickstart.md:L13-L30](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/quickstart.md#L13-L30)，这段代码做了什么：列出从建环境、装 vLLM 到克隆并 `uv pip install -e .` 安装 vLLM-Omni 的完整流程。

前置条件明确要求 Linux + Python 3.12，见 [docs/getting_started/quickstart.md:L8-L12](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/quickstart.md#L8-L12)：

```text
- OS: Linux
- Python: 3.12
```

紧接着的注释说明了版本对齐的重要性，见 [docs/getting_started/quickstart.md:L34-L37](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/quickstart.md#L34-L37)：这段提示「vLLM 与 vLLM-Omni 必须主次版本一致，否则导入时会看到告警」，并指出 `--omni` 标志失效通常源于 vLLM 版本过低（vLLM Omni 0.26.0 起不再劫持 vLLM 入口，必须 vLLM ≥ 0.26.0）。

**版本对齐政策（v0.26.0 新增）**：安装总览文档现在在最顶部用一条 `!!! important` 提示明确写出对齐要求，见 [docs/getting_started/installation/README.md:L3-L4](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/README.md#L3-L4)：

```text
vLLM-Omni is released against the matching upstream vLLM major/minor version.
vLLM-Omni 0.26.x requires vLLM 0.26.x; the stable v0.26.0 instructions pin vLLM 0.26.0.
```

这段代码做了什么：把「主次版本对齐」从一句口口相传的经验，升级成安装文档的醒目开场白，并明确「稳定版 v0.26.0 的安装步骤一律 pin vLLM 0.26.0」。CUDA 子文档开头也把原来的「vLLM-Omni depends vLLM」改写成了「depends on the matching major/minor release of vLLM」（vLLM-Omni 0.26.x release line uses vLLM 0.26.x），与这条铁律呼应。

CUDA 的硬件要求是「计算能力 7.0 及以上」，见 [docs/getting_started/installation/gpu/cuda.inc.md:L1-L5](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/gpu/cuda.inc.md#L1-L5)；ROCm 则验证在 gfx942（MI300 系列），见 [docs/getting_started/installation/gpu/rocm.inc.md:L1-L5](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/gpu/rocm.inc.md#L1-L5)。

**CUDA Docker 构建**：如果想自定义底层 vLLM 版本构建镜像，`BASE_IMAGE` 现在指向 v0.26.0，见 [docs/getting_started/installation/gpu/cuda.inc.md:L122-L129](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/gpu/cuda.inc.md#L122-L129)：

```bash
DOCKER_BUILDKIT=1 docker build \
  -f docker/Dockerfile.cuda \
  --build-arg BASE_IMAGE=vllm/vllm-openai:v0.26.0 \
  -t vllm-omni-cuda .
```

这段代码做了什么：`vllm-omni` 的 CUDA Dockerfile 以官方 `vllm-openai` 镜像为底座，再在其上装 vLLM-Omni。把 `BASE_IMAGE` 对齐到 v0.26.0，就是保证底座 vLLM 与要叠加的 vLLM-Omni 0.26.x 主次版本一致——又是同一条铁律在 Docker 层面的体现。

**NPU 从源码构建（v0.26.0 更新）**：NPU 上要先把 vLLM 与 vLLM-Ascend 都钉到 v0.26 发布线，再装 vLLM-Omni。注意 vLLM-Ascend 用的是 `releases/v0.26.0rc` 分支，见 [docs/getting_started/installation/npu/npu.inc.md:L55-L73](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/npu/npu.inc.md#L55-L73)：

```bash
# Pin vLLM and vLLM-Ascend to the v0.26 release line
git clone -b v0.26.0 https://github.com/vllm-project/vllm.git
cd vllm
VLLM_TARGET_DEVICE=empty pip install -v -e .
cd ..

git clone -b releases/v0.26.0rc https://github.com/vllm-project/vllm-ascend.git
cd vllm-ascend
pip install -v -e .
cd ..

# Install vLLM-Omni from the latest main branch
git clone https://github.com/vllm-project/vllm-omni.git
cd vllm-omni
pip install -v -e . --no-build-isolation
# or VLLM_OMNI_TARGET_DEVICE=npu pip install -v -e .
export VLLM_WORKER_MULTIPROC_METHOD=spawn
```

这段代码做了什么：vLLM 用 `VLLM_TARGET_DEVICE=empty` 做一次「空设备」可编辑安装（只为提供 Python 包，不编译任何后端 kernel），真正的 NPU 适配由 `vllm-ascend`（`releases/v0.26.0rc`）提供，最后才轮到 vLLM-Omni 以 `--no-build-isolation` 复用当前环境来构建。三方同处一条 v0.26 线，正是 4.4 节版本对齐检查在 NPU 上也能成立的前提。v0.26.0 还把旧版里写死的 `cd /vllm-workspace/vllm-omni` 改成了更通用的 `cd vllm-omni`，并补上了 `cd vllm` / `cd vllm-ascend` / `cd ..` 的目录切换，让三段克隆互不干扰。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍 CUDA 源码安装，确认环境可用。

**操作步骤**：

```bash
# 1. 安装 uv（若尚未安装），见 python_env_setup.inc.md 的推荐
#    curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 建一个干净环境
uv venv --python 3.12 --seed
source .venv/bin/activate

# 3. 安装对齐版本的 vLLM
uv pip install vllm==0.26.0 --torch-backend=auto

# 4. 克隆并以可编辑模式安装 vLLM-Omni
git clone https://github.com/vllm-project/vllm-omni.git
cd vllm-omni
uv pip install -e .
```

**需要观察的现象**：

- 第 3 步会下载体积较大的 vLLM wheel 与 PyTorch，耗时取决于网络；
- 第 4 步执行 `uv pip install -e .` 时，终端会打印 `Detected CUDA backend from torch`、`Loading requirements from: .../requirements/cuda.txt`、`Loaded N requirements for cuda`、`Generated version: ...` 等行——这些都是 `setup.py` 的输出，正是 4.2 节要讲的内容。

**预期结果**：

- 安装结束后运行下面命令能打印出版本号（具体字符串与 git 状态有关，**待本地验证**）：

```bash
python -c "import vllm_omni; print(vllm_omni.__version__)"
```

例如当前仓库 `git describe` 为 `v0.26.0-7-g5215e03a`，装在 CUDA 上后版本号大致形如 `0.26.0.post7+g5215e03a`（CUDA 不加设备后缀，详见 4.3 节）。具体以本机实际输出为准。

> 如果运行环境没有 GPU，可通过 `VLLM_OMNI_TARGET_DEVICE=cpu uv pip install -e .` 强制按 CPU 依赖安装，用于阅读源码。
>
> 若在 NPU 上，则改走 4.1.3 的三方安装；安装日志同样会打印 `Detected NPU backend` 与 `Loading requirements from: .../requirements/npu.txt`。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么不建议把 vLLM-Omni 装进一个已经装了其它 PyTorch/CUDA 版本的旧环境？

**参考答案**：vLLM 会针对特定 CUDA 与 PyTorch 版本编译大量 kernel，存在二进制不兼容风险；混装极易出现 `undefined symbol`、NCCL 冲突等问题。官方明确建议用「fresh new」环境，参见 cuda.inc.md 中关于 fresh environment 的说明。

**练习 2**：官方为什么推荐 `uv pip install -e .`（带 `-e`）而不是普通安装？

**参考答案**：vLLM-Omni 迭代很快，可编辑模式让你 `git pull` 后改动立即生效、改源码调试也无需重新打包。

**练习 3**：在 NPU 上，为什么 vLLM 要用 `VLLM_TARGET_DEVICE=empty` 安装，而不是直接 `pip install -e .`？

**参考答案**：NPU 上真正的算子后端由 `vllm-ascend` 提供，vLLM 本体只需以「空设备」模式装出 Python 包即可，不必（也无法在普通环境里）编译 CUDA/ROCm kernel。如果让 vLLM 默认探测设备，会试图编译不匹配的后端。所以先用 `VLLM_TARGET_DEVICE=empty` 装一个「壳」，再由 `vllm-ascend` 注入 NPU 能力，最后装 vLLM-Omni。见 [docs/getting_started/installation/npu/npu.inc.md:L57-L60](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/installation/npu/npu.inc.md#L57-L60)。

---

### 4.2 硬件感知安装机制：setup.py 的设备检测与依赖路由

#### 4.2.1 概念说明

`pyproject.toml` 里把 `dependencies` 声明成了 `dynamic`（动态）。这意味着依赖列表不是写死的，而是由 `setup.py` 在安装时**根据你机器上的硬件**动态决定。这样做的好处是：用户只要一句 `pip install vllm-omni`，就能自动拿到正确平台的依赖，不需要手写 `[cuda]`、`[rocm]` 这样的 extras。

这套机制的核心是 `setup.py` 里的两个函数：

- `detect_target_device()`：判断当前硬件后端；
- `get_install_requires()`：根据后端去 `requirements/` 目录读对应的依赖文件。

#### 4.2.2 核心流程

设备检测遵循一套**优先级**，从高到低：

```
1. 环境变量 VLLM_OMNI_TARGET_DEVICE          （显式覆盖，最高优先级）
2. READTHEDOCS 环境变量                       （文档构建走 CPU，避免拉 ~2GB CUDA 库）
3. torch 后端探测 (cuda → rocm → npu → xpu → musa)
4. 兜底：CPU
```

依赖加载流程：

```
device = detect_target_device()
file = requirements/{device}.txt          # 例如 requirements/cuda.txt
load_requirements(file)                   # 支持文件内的 -r common.txt 递归加载
→ install_requires
```

`requirements/` 目录下有 7 个文件：`common.txt`（所有平台共享）、`cuda.txt`、`rocm.txt`、`npu.txt`、`xpu.txt`、`musa.txt`、`cpu.txt`。平台文件用 `-r common.txt` 把公共依赖引入，再追加各自专属依赖。

#### 4.2.3 源码精读

`detect_target_device()` 的优先级 1 是环境变量覆盖，见 [setup.py:L55-L64](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L55-L64)：

```python
target_device = os.environ.get("VLLM_OMNI_TARGET_DEVICE")
if target_device:
    valid_devices = ["cuda", "rocm", "npu", "xpu", "musa", "cpu"]
    if target_device.lower() in valid_devices:
        return target_device.lower()
```

这段代码做了什么：读取 `VLLM_OMNI_TARGET_DEVICE`，若有效（属于 6 个合法设备之一）就直接采用，允许用户强制指定平台。

优先级 1.5 是 ReadTheDocs 特判，见 [setup.py:L67-L69](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L67-L69)：文档构建机器没有 GPU 且内存受限（1GB 上限），用 CPU 依赖避免拉入约 2GB 的 CUDA 库把构建拖进 swap。

优先级 2 是 torch 后端探测，见 [setup.py:L74-L120](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L74-L120)，按 cuda → rocm → npu → xpu → musa 顺序逐个判断。其中 ROCm 分支还会顺手卸载与 ROCm 冲突的 `onnxruntime`，见 [setup.py:L82-L86](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L82-L86)：

```python
if torch.version.hip is not None:
    uninstall_onnxruntime()
    return "rocm"
```

依赖加载函数 `get_install_requires()` 见 [setup.py:L220-L239](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L220-L239)：用检测到的设备拼接出 `requirements/{device}.txt` 路径，再交给 `load_requirements()`。

`load_requirements()` 见 [setup.py:L185-L217](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L185-L217)，这段代码做了什么：逐行读依赖文件，跳过空行与注释，并支持 `-r common.txt` 这种递归 include 指令——这就是平台文件能复用公共依赖的原因。

实际依赖文件示例。公共依赖（所有平台共享）见 [requirements/common.txt:L1-L36](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/requirements/common.txt#L1-L36)，其中关键几行：

```text
transformers >= 5.5.3
diffusers==0.38.0
accelerate==1.12.0
cache-dit==1.3.0
```

CUDA 专属依赖见 [requirements/cuda.txt:L1-L5](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/requirements/cuda.txt#L1-L5)：

```text
-r common.txt
onnxruntime>=1.23.2
fa3-fwd==0.0.3
```

ROCm 专属依赖见 [requirements/rocm.txt:L1-L2](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/requirements/rocm.txt#L1-L2)，把 `onnxruntime` 换成了 ROCm 版本：

```text
-r common.txt
onnxruntime-rocm>=1.22.2
```

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式验证「同一句 `pip install` 会因硬件不同而装入不同依赖」。

**操作步骤**：

1. 打开 `setup.py`，定位 `detect_target_device()`（[setup.py:L43](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L43)）。
2. 假设你的机器是 NVIDIA GPU，沿着代码走一遍：`torch.version.cuda is not None` 命中 → 返回 `"cuda"`。
3. 跟着 `get_install_requires()` 走：它会去读 `requirements/cuda.txt`，而该文件第一行 `-r common.txt` 又把公共依赖拉进来。
4. 对比 `requirements/rocm.txt` 与 `requirements/cuda.txt` 的差异（前者用 `onnxruntime-rocm`，后者用 `onnxruntime` + `fa3-fwd`）。

**需要观察的现象**：

- 两个平台文件都只有两三行，差异极小，真正的依赖大头都在 `common.txt`。

**预期结果**：

- 你能用自己的话解释：「装 vLLM-Omni 时，依赖列表 = `common.txt` ∪ `{device}.txt`」。

> **待本地验证**：在装有 ROCm 的机器上执行 `VLLM_OMNI_TARGET_DEVICE=rocm uv pip install -e .`，观察终端打印 `Detected ROCm backend from torch`、`Found onnxruntime installed, uninstalling for ROCm compatibility...`，以及最终 `Loading requirements from: .../requirements/rocm.txt`。

#### 4.2.5 小练习与答案

**练习 1**：在一台没有 GPU 的文档构建服务器上安装 vLLM-Omni，会走哪个分支？为什么？

**参考答案**：会走 CPU 分支。原因是 ReadTheDocs 环境会设置 `READTHEDOCS` 环境变量，`detect_target_device()` 优先级 1.5 直接返回 `"cpu"`，避免拉入约 2GB 的 CUDA 库把受限内存的文档构建拖垮。见 [setup.py:L67-L69](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L67-L69)。

**练习 2**：为什么 ROCm 分支里要调用 `uninstall_onnxruntime()`？

**参考答案**：普通版 `onnxruntime` 与 ROCm 依赖冲突，ROCm 平台需要 `onnxruntime-rocm`。`setup.py` 在检测到 ROCm 时主动卸载普通版，避免后续冲突。见 [setup.py:L18-L40](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L18-L40) 与 [setup.py:L82-L86](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L82-L86)。

---

### 4.3 版本号生成与包元数据：pyproject.toml + setuptools-scm

#### 4.3.1 概念说明

版本号在本项目里有两层含义：

1. **打包版本号**：由 `setup.py` 的 `get_vllm_omni_version()` 从 git 标签推导，并追加「设备后缀」（如 `+rocm`、`+npu`），写入自动生成的 `_version.py`。
2. **导入版本号**：`vllm_omni/version.py` 在运行时从 `_version.py` 读取，供 `vllm_omni.__version__` 使用，并用于版本对齐检查。

`pyproject.toml` 则定义了「包的静态名片」：包名、支持的 Python 版本、可选依赖（extras）、命令行入口（console_script）、以及给 vLLM 的插件入口。

#### 4.3.2 核心流程

版本号生成流程：

```
git tag (例如 v0.26.0)
   │  setuptools-scm 推导
   ▼
base version (例如 0.26.0.post7+g5215e03a)
   │  追加设备后缀
   ▼
final version (CUDA: 不加后缀; ROCm: .rocm; NPU: .npu; ...)
   │  write_to
   ▼
vllm_omni/_version.py  →  __version__ / __version_tuple__
```

设备后缀的追加规则（`sep` 的选择是为了避免出现两个 `+`）：

- 若 base version 已含 `+`（dev 版本常见，如 `0.14.1.dev23+g1a2b3c4`），分隔符用 `.`；
- 否则用 `+`；
- CUDA 特殊：**不加任何后缀**（与上游 vLLM 保持一致）。

docstring 给出的示例见 [setup.py:L129-L132](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L129-L132)：

```text
- 0.14.0+cuda
- 0.14.1.dev23+g1a2b3c4.rocm
- 0.15.0+npu
```

#### 4.3.3 源码精读

`get_vllm_omni_version()` 的设备后缀逻辑见 [setup.py:L123-L182](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L123-L182)。其中分隔符与后缀追加见 [setup.py:L154-L175](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L154-L175)：

```python
sep = "+" if "+" not in version else "."
device = detect_target_device()

if device == "cuda":
    pass                       # CUDA 不加后缀，对齐 vLLM
elif device == "rocm":
    version += f"{sep}rocm"
elif device == "npu":
    version += f"{sep}npu"
...
```

这段代码做了什么：根据检测到的设备，给版本号追加对应后缀，并通过 `SETUPTOOLS_SCM_PRETEND_VERSION` 让 setuptools-scm 把最终版本写入 `_version.py`。

包元数据（`pyproject.toml`）要点：

- 构建系统与动态字段。`version` 与 `dependencies` 都是动态的，分别由 setuptools-scm 与 setup.py 提供，见 [pyproject.toml:L1-L11](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L1-L11)：

```toml
[build-system]
requires = ["setuptools>=77.0.3,<81.0.0", "wheel", "setuptools-scm>=8.0"]
build-backend = "setuptools.build_meta"

[project]
name = "vllm-omni"
dynamic = ["version", "dependencies"]
```

- Python 版本约束。注意 `requires-python` 允许 `>=3.10,<3.14`，但官方 quickstart **推荐 3.12**，见 [pyproject.toml:L14](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L14) 与 [docs/getting_started/quickstart.md:L8-L12](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/getting_started/quickstart.md#L8-L12)。
- 可选依赖（extras）。提供 `dev`（测试/类型检查）、`demo`（Gradio 演示）、`docs`（文档构建）、`quack`（Blackwell FP8）、`fa4`（FlashAttention-4）等，见 [pyproject.toml:L36-L125](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L36-L125)。
- console_script（命令行入口）。注册了 `vllm-omni` 命令，指向 CLI 主入口，见 [pyproject.toml:L133-L134](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L133-L134)：

```toml
[project.scripts]
vllm-omni = "vllm_omni.entrypoints.cli.main:main"
```

- vLLM 插件入口。这是安装层面一个关键设计：把「注册 omni 模型」挂到 vLLM 的 `general_plugins` 上，使那些**只 import 了 vllm、没 import vllm_omni 的子进程**也能自动加载 omni 架构，见 [pyproject.toml:L140-L141](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L140-L141)：

```toml
[project.entry-points."vllm.general_plugins"]
vllm_omni_register_models = "vllm_omni.engine.arg_utils:register_omni_models_to_vllm"
```

- setuptools-scm 配置仅需出现即启用，见 [pyproject.toml:L151-L152](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L151-L152)。

#### 4.3.4 代码实践

**实践目标**：用源码阅读验证「版本号来自 git tag + 设备后缀」，并理解 `_version.py` 是构建时生成的。

**操作步骤**：

1. 在仓库根目录执行 `git describe --tags`，你会看到类似 `v0.26.0-7-g5215e03a`（标签 `v0.26.0` 之后有 7 个提交）。
2. 打开 `setup.py` 的 [get_vllm_omni_version()](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L123-L182)，对照「sep 选择 + 设备后缀」逻辑，推导 CUDA 下最终版本号。
3. 注意 `_version.py` 在源码里**不存在**（它被 `.gitignore` 忽略，仅在安装/构建时由 setuptools-scm 写出）。可以用 `ls vllm_omni/_version.py` 确认；安装后它才会出现。

**需要观察的现象**：

- 源码检出阶段 `vllm_omni/_version.py` 不存在，此时 `import vllm_omni` 会触发 `version.py` 的 `ImportError` 兜底，把 `__version__` 设为 `"dev"`；
- 执行 `uv pip install -e .` 后，`_version.py` 才被生成，`__version__` 变为真实版本号。

**预期结果**：

- 你能解释：`__version__` 的值 = `setuptools-scm(git tag)` + `设备后缀`，且 CUDA 不加后缀。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CUDA 不加 `+cuda` 后缀，而 ROCm/NPU 要加？

**参考答案**：为了与上游 vLLM 保持一致——vLLM 的 CUDA 版本号本身不带后缀。vLLM-Omni 在 CUDA 上刻意不加后缀，便于版本对齐检查时直接比较主次版本；而 ROCm/NPU 等平台加上后缀以区分二进制来源。见 [setup.py:L160-L163](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/setup.py#L160-L163)。

**练习 2**：`[project.entry-points."vllm.general_plugins"]` 这一段解决了什么问题？

**参考答案**：vLLM 会在自身 `load_general_plugins()` 中加载所有声明在该入口的插件。vLLM-Omni 通过它把「注册 omni 模型」注册进去，使得那些只 import 了 vllm 的 worker 子进程（它们并不 import vllm_omni）也能自动发现并加载 omni 架构。这是「vLLM-Omni 扩展而非重写 vLLM」在打包层面的体现。见 [pyproject.toml:L136-L141](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/pyproject.toml#L136-L141)。

---

### 4.4 版本对齐告警机制：version.py 与导入顺序

#### 4.4.1 概念说明

vLLM-Omni 与 vLLM 是「贴身扩展」关系：vLLM-Omni 内部大量代码直接调用 vLLM 的内部函数、改写 vLLM 的类。一旦两者的主次版本（major.minor）不一致，运行时很容易出现「函数签名变了」「类被删了」之类的崩溃。

为此，vLLM-Omni 在**包被导入的最早期**就做一次版本对齐检查：如果发现 vLLM 的主次版本和自己不一致，就抛出一个 `RuntimeWarning`，提醒用户先对齐版本。这是你在本讲实践任务里要故意触发的那个告警。这条检查与 4.1 节「偶数次版本对齐发布节奏」互为表里：发布节奏保证「有对齐的版本可装」，运行时检查则保证「装错了能立刻被发现」。

#### 4.4.2 核心流程

对齐检查的判断逻辑非常简洁：

```
omni_major_minor = vllm_omni.__version_tuple__[:2]
vllm_major_minor = vllm.__version_tuple__[:2]

if 任一方是 (0,0)（dev 兜底）:
    跳过                              # 无法可靠比较
elif omni_major_minor != vllm_major_minor:
    发出 RuntimeWarning
```

并且，这个检查必须在「打 patch 之前」执行，原因写在 `__init__.py` 的注释里：如果版本不一致，patch 阶段对 vLLM 的导入本身就可能抛异常，那么版本告警就永远没机会打印出来。因此正确的导入顺序是：

```
1. 导入 version（含版本对齐检查）   ← 必须最先
2. 导入 patch（改写 vLLM）
3. 注册自定义 configs
4. 暴露 OmniModelConfig
5. 懒加载 Omni / AsyncOmni
```

#### 4.4.3 源码精读

`version.py` 先尝试从自动生成的 `_version.py` 读取版本，失败则兜底为 `"dev"`，见 [vllm_omni/version.py:L10-L23](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/version.py#L10-L23)：

```python
try:
    from ._version import __version__, __version_tuple__
except ImportError as e:
    warnings.warn(...)           # 提示「开发模式下尚未构建」
    __version__ = "dev"
    __version_tuple__ = (0, 0, "dev")
```

核心对齐函数 `warn_if_misaligned_vllm_version()` 见 [vllm_omni/version.py:L26-L48](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/version.py#L26-L48)：

```python
omni_ver = __version_tuple__[:2]
vllm_ver = vllm_version_tuple[:2]
if omni_ver == (0, 0) or vllm_ver == (0, 0):
    return                       # dev 版本，跳过
if omni_ver != vllm_ver:
    warnings.warn(
        "vLLM and vLLM-Omni appear to have mismatched major/minor versions:\n"
        f" --> vLLM-Omni version {__version__}\n"
        f" --> vLLM version {vllm_version}\n"
        "This will likely cause compatibility issues.",
        RuntimeWarning,
    )
```

这段代码做了什么：只比较主版本与次版本（即 `version_tuple` 的前两位），不一致就发 `RuntimeWarning`。

该函数在模块导入时自动执行，见 [vllm_omni/version.py:L54-L58](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/version.py#L54-L58)：

```python
try:
    warn_if_misaligned_vllm_version()
except ModuleNotFoundError:
    pass                         # vLLM 未安装（如文档构建），静默跳过
```

「先做版本检查再打 patch」的顺序约束，明确写在 `__init__.py` 的注释里，见 [vllm_omni/__init__.py:L15-L27](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/__init__.py#L15-L27)：

```python
# We import version early, because it will warn if vLLM / vLLM Omni
# are not using the same major + minor version (if vLLM is installed).
# We should do this before applying patch, because vLLM imports might
# throw in patch if the versions differ.
from .version import __version__, __version_tuple__  # isort:skip

try:
    from . import patch
except ModuleNotFoundError as exc:
    ...
```

#### 4.4.4 代码实践

**实践目标**：故意制造一次版本不匹配，触发对齐告警，理解告警的触发条件与时机。

**操作步骤**（在已按 4.1 节装好 vLLM-Omni 0.26.x 的环境中进行）：

1. 先确认当前版本正常：

```bash
python -c "import vllm_omni, vllm; print('omni', vllm_omni.__version__); print('vllm', vllm.__version__)"
```

2. 用环境变量把对齐检查的告警显示出来（默认 Python 可能隐藏 `RuntimeWarning`）：

```bash
python -W error::RuntimeWarning -c "import vllm_omni"
```

3. **故意制造不匹配**：把 vLLM 降级到一个主次版本不同的版本（仅用于观察告警，操作有风险，建议在临时环境中进行）：

```bash
# 仅作演示；这会破坏可用性，观察完告警后请重装回对齐版本
uv pip install "vllm==0.25.0"
python -W default::RuntimeWarning -c "import vllm_omni"
```

**需要观察的现象**：

- 第 3 步会打印类似下面的告警（具体版本号以本机为准，**待本地验证**）：

```text
vLLM and vLLM-Omni appear to have mismatched major/minor versions:
 --> vLLM-Omni version 0.26.0...
 --> vLLM version 0.25.0
This will likely cause compatibility issues.
```

- 该告警在 `import vllm_omni` 的**第一时间**出现（早于任何业务代码），印证了「版本检查在 patch 之前」。

**预期结果**：

- 你能用一句话解释触发条件：「当且仅当两者的主次版本元组（`version_tuple[:2]`）不同，且都不是 dev 兜底值时，才告警。」

> 实验结束后，请把 vLLM 装回对齐版本：`uv pip install vllm==0.26.0 --torch-backend=auto`。

#### 4.4.5 小练习与答案

**练习 1**：为什么对齐检查只比较 `version_tuple[:2]`（主.次），而不是完整版本号？

**参考答案**：补丁版本（patch，即第三位）通常只含 bug 修复，API 兼容；而主版本、次版本的变更才可能改动公开/内部接口。vLLM-Omni 紧贴 vLLM 内部实现，因此只需保证主次版本对齐即可。见 [vllm_omni/version.py:L33-L40](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/version.py#L33-L40)。

**练习 2**：如果把 `__init__.py` 里「先 import version、再 import patch」的顺序颠倒，会有什么后果？

**参考答案**：版本不一致时，`import patch` 阶段对 vLLM 的导入可能直接抛异常，导致程序在打印版本告警之前就崩溃；用户因此看不到「版本不匹配」的友好提示，更难定位问题。这正是注释强调顺序的原因。见 [vllm_omni/__init__.py:L15-L19](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/__init__.py#L15-L19)。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一次「从零到验证」的端到端安装与诊断。

**任务**：在一台 NVIDIA GPU 机器（或 GPU 容器）上，完成下列全部步骤并记录每一步的关键输出。

1. **建环境 + 装 vLLM**：按 quickstart 创建 Python 3.12 环境，安装 `vllm==0.26.0 --torch-backend=auto`。
2. **源码安装 vLLM-Omni**：`git clone` 后 `uv pip install -e .`，记录终端里 `setup.py` 打印的 `Detected ... backend`、`Loading requirements from:`、`Generated version:` 三行——它们分别对应 4.2 节的设备检测、依赖路由与 4.3 节的版本生成。
3. **核对版本**：运行 `python -c "import vllm_omni, vllm; print(vllm_omni.__version__, vllm.__version__)"`，确认两者主次版本一致。
4. **核对入口**：运行 `vllm-omni --version`（这是 4.3 节的 console_script）与 `vllm-omni -h`，确认命令可用（CLI 深入机制留待 [u1-l5 在线服务初体验](u1-l5-online-quickstart.md)）。
5. **触发并理解告警**：按 4.4.4 的步骤，临时降级 vLLM 制造不匹配，捕获 `RuntimeWarning`，然后在 `version.py` 中定位产生该告警的代码行；最后把 vLLM 装回对齐版本。
6. **产出一份安装报告**：包含环境信息（OS、Python、GPU）、安装命令、`setup.py` 三行关键输出、最终 `__version__`、以及告警截图/文本。

**验收标准**：

- 能复现「正常安装无告警」与「版本不匹配有告警」两种状态；
- 能在源码中指出「设备检测、依赖路由、版本生成、版本对齐」分别由哪个文件的哪段代码负责；
- 能说清楚 NPU 与 CUDA 安装流程的关键差异（三方钉版本 vs 两步安装）。

> 若本机没有 GPU，可全程用 `VLLM_OMNI_TARGET_DEVICE=cpu` 完成步骤 1–4 与版本对齐实验（步骤 5），同样能覆盖本讲除 NPU 三方安装外的全部知识点。

## 6. 本讲小结

- vLLM-Omni 必须**先装对齐版本的 vLLM，再以可编辑模式从源码安装**，官方推荐 Python 3.12 + Linux + 干净环境；从 v0.14.0 起每个偶数号 vLLM 次版本都对应一个 vLLM-Omni 稳定版（0.26.x ↔ vLLM 0.26.x）。
- v0.26.0 把这条「主次版本对齐」铁律写进了安装文档顶部（`!!! important`），并把 CUDA Docker `BASE_IMAGE` 与 NPU 的 vLLM/vLLM-Ascend 都对齐到 v0.26 发布线。
- `setup.py` 通过 `detect_target_device()` 按「环境变量 → READTHEDOCS → torch 后端探测 → CPU 兜底」的优先级判定硬件，再从 `requirements/{device}.txt` 加载平台依赖，实现了「一句 `pip install` 自动适配 CUDA/ROCm/NPU/XPU/MUSA」。
- 版本号由 setuptools-scm 从 git 标签推导，并按设备追加后缀（CUDA 不加、其余加 `+rocm`/`+npu` 等），写入自动生成的 `_version.py`。
- `pyproject.toml` 定义了包元数据、可选依赖（dev/demo/docs/quack/fa4 等）、`vllm-omni` console_script，以及把 omni 模型注册挂到 vLLM 的 `general_plugins` 入口。
- `vllm_omni/version.py` 在导入早期比较 vLLM 与 vLLM-Omni 的主次版本，不一致即发 `RuntimeWarning`；该检查**必须早于 patch**，否则版本不一致时 patch 阶段就会先崩溃。

## 7. 下一步学习建议

- 装好之后，下一步建议阅读 [u1-l3 源码地图：目录结构与包布局](u1-l3-directory-map.md)，建立对 `vllm_omni/` 各子目录（entrypoints/engine/diffusion/config/...）的整体认知。
- 想立刻跑通第一个模型，可直接跳到 [u1-l4 离线推理初体验：用 Omni 类生成图像](u1-l4-offline-quickstart.md)。
- 想理解 `vllm serve --omni` 与 `vllm-omni` 命令行入口的拦截机制，阅读 [u1-l5 在线服务初体验](u1-l5-online-quickstart.md)，它会深入 `vllm_omni/entrypoints/cli/main.py`。
- 对版本对齐背后的 patch 机制（为什么版本不一致会导致 patch 崩溃）感兴趣，可提前预览 [u2-l1 patch 机制：vLLM-Omni 如何无缝改写 vLLM](u2-l1-patch-mechanism.md)。
