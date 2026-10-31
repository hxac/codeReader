# 环境搭建：OpenMMLab 栈与 MinkowskiEngine 安装

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 Ground3D-LMM 的依赖为什么「难装」：它同时依赖 OpenMMLab 栈（mmengine/mmcv/mmdet/mmdet3d）、两个稀疏卷积库（spconv 与 MinkowskiEngine）和一套 LLM 栈（transformers/peft），而这些包的 CUDA 产物必须与 PyTorch 版本、CUDA 版本、GPU 架构三方对齐。
2. 按 `docs/INSTALL.md` 的 **Option B** 手动完成环境搭建，尤其是两处源码编译：`mmcv==2.1.0`（`--no-build-isolation`）与 MinkowskiEngine（master 分支）。
3. 理解 `environment.yml` 为什么「刻意不完整」、`pyproject.toml` 为什么只声明极少的依赖。
4. 用仓库自带的验证脚本和 `import uniseg3d` 检查安装是否真正可用，并掌握「顺着 import 报错补依赖」的排查方法。

本讲**不涉及任何模型代码逻辑**——那是第 3、4 单元的事。我们只解决一件事：把环境装到能让 `import uniseg3d` 成功为止。

## 2. 前置知识

本讲是整套手册里最「运维味」的一讲，需要先了解几个基础概念：

- **conda 环境**：一个独立的 Python 解释器 + 包集合的沙盒。3D 深度学习项目的依赖版本冲突频繁，永远不要往 base 环境里装。
- **wheel 与源码编译**：`pip install xxx` 时，pip 优先下载预编译好的二进制包（wheel）；如果没有匹配的 wheel，pip 会下载源码（sdist），在你机器上现场编译。编译型扩展（含 CUDA 算子的包）要求本机有编译工具链（gcc、nvcc）。
- **CUDA 三方对齐**：一个含 CUDA 算子的包能否工作，取决于三个维度同时匹配——PyTorch 编译时用的 CUDA 版本（如 `2.4.1+cu118`）、本机安装的 CUDA 工具链（`CUDA_HOME` 指向的地方）、GPU 的计算架构（compute capability，如 A100 是 sm_80、H100 是 sm_90）。任何一环错位，轻则编译失败，重则编译成功但运行时报 `no kernel image is available`。
- **build isolation（构建隔离）**：pip 默认在临时目录里新建一个隔离环境来构建源码包，那个环境里只有 `pyproject.toml` 声明的构建依赖，**看不到你已装好的 torch**。凡是 `setup.py` 里需要在编译期 `import torch` 的包（mmcv 就是），都必须加 `--no-build-isolation` 关掉隔离。
- **OpenMMLab 栈**：mmengine（训练引擎与注册表）→ mmcv（底层算子与视觉工具）→ mmdet（2D 检测）→ mmdet3d（3D 检测/分割）。上一讲说过，Ground3D-LMM 的模型类全部注册在 mmdet3d 的 registry 里，所以这四层缺一不可。
- **稀疏卷积**：室内点云在体素化后绝大多数体素是空的，稀疏卷积只对非空体素做计算。本项目用到**两个**稀疏卷积库：spconv（跑 SpConvUNet 主干）和 MinkowskiEngine（做批组装与坐标量化，见 4.3 节）。
- **peft / LoRA**：LoRA 是只训练少量低秩矩阵的微调方法，peft 是它的官方实现库。安装上只需一个 `pip install peft`，但版本要与 transformers 配套。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/INSTALL.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md) | 安装主文档：测试环境表、Option A/B 两条安装路径、验证脚本、smoke test、常见报错 |
| [environment.yml](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml) | conda 依赖清单（版本锁定），**刻意不包含** mmcv/mmdet/mmdet3d/MinkowskiEngine |
| [pyproject.toml](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml) | 本仓库自身的 pip 包元数据，只声明纯 Python 依赖 |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | Install 段落给出了「environment.yml 不能独立完成安装」的明确警告 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 模型主文件，顶部 import 了 spconv / torch_scatter / MinkowskiEngine——这三个依赖「装不上就全盘 import 失败」的源头 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器，顶部 import transformers / peft / qwen_vl_utils |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | Qwen3-VL 注入点云 token 的实现，顶部 import hydra / nncore |
| [uniseg3d/\_\_init\_\_.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/__init__.py) | 包入口，`import uniseg3d` 时的 import 链起点 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**4.1 conda 环境与依赖清单**、**4.2 mmcv 源码编译**、**4.3 MinkowskiEngine 源码编译**、**4.4 本包安装与依赖验证**。

### 4.1 conda 环境与依赖清单

#### 4.1.1 概念说明

Ground3D-LMM 官方锁定的测试环境是：Ubuntu 20.04/22.04、Python 3.10、CUDA 11.8、PyTorch 2.4.1、A100 80GB 或 RTX 3090 24GB：

| 组件 | 版本 | 来源 |
|---|---|---|
| OS | Ubuntu 20.04 / 22.04 | [docs/INSTALL.md:L5-L15](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L5-L15) 的测试环境表 |
| Python | 3.10 | 同上 |
| CUDA | 11.8 | 同上 |
| PyTorch | 2.4.1 | 同上 |

仓库给了两条安装路径（[docs/INSTALL.md:L17-L27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L17-L27)）：

- **Option A**：`conda env create -f environment.yml` 一步到位（推荐先试）；
- **Option B**：手动逐步安装——**如果 Option A 在某个 OpenMMLab 包上失败，回退到 Option B**。

关键认知：**Option A 并不能独立装完**。这不是缺陷，而是刻意设计——README 里有一段明确的警告：

> **Heads-up:** `conda env create -f environment.yml` does **not** fully work on its own —— 见 [README.md:L52-L56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L52-L56)

原因在 4.2/4.3 展开：`mmcv==2.1.0` 没有对应 torch 2.4.1 的预编译 wheel，MinkowskiEngine 必须从特定分支源码编译，而这两步需要交互式的环境变量设置，conda 清单表达不了。

#### 4.1.2 核心流程

Option A + 补装的整体流程：

```text
conda env create -f environment.yml     # 装：python3.10 + torch2.4.1+cu118 + mmengine
        │                                #     + spconv + torch-scatter + LLM栈 + 科学计算库
        ▼
（若失败 → 改走 Option B 手动逐步装，见 4.2）
        ▼
补装源码编译三件：mmcv → mmdet → mmdet3d → MinkowskiEngine   ← environment.yml 刻意不管的部分
        ▼
pip install -e .                        # 安装本仓库的 uniseg3d 包
        ▼
跑验证脚本 + import uniseg3d            # 见 4.4
```

#### 4.1.3 源码精读

先看 `environment.yml` 的开头——构建工具被刻意钉在最前面：

[environment.yml:L1-L14](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L1-L14) 声明了环境名 `ground3d`、三个 channel，并在 pip 依赖列表的**第一组**就安装 `setuptools<70`、`wheel`、`ninja`。注释写明原因：mmcv/MinkowskiEngine 的源码编译在较新 setuptools 上会报 `No module named 'pkg_resources'`，所以必须先把旧版 setuptools 钉进环境。

接着是 PyTorch 与 mmengine（[environment.yml:L16-L23](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L16-L23)）：torch/vision/audio 三个包都用 `+cu118` 后缀，并通过 `--extra-index-url` 指向 PyTorch 官方的 CUDA 11.8 wheel 源；mmengine 钉在 `0.10.7`。

然后是整个文件最重要的一段注释（[environment.yml:L25-L29](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L25-L29)）：**mmcv / mmdet / mmdet3d 被刻意不写进清单**，因为 mmcv 2.1.0 对 torch 2.4.1 没有预编译 wheel、必须用 `--no-build-isolation` 源码编译，MinkowskiEngine 0.5.4 也是源码构建，清单完成后还要按 `docs/INSTALL.md` 的步骤 3、4、4b 手动补装。

最后是 3D 稀疏算子与 LLM 栈（[environment.yml:L31-L41](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L31-L41)）：`spconv-cu118==2.3.8`、通过 PyG wheel 索引安装的 `torch-scatter==2.1.2`，以及 `transformers==4.57.6`、`peft==0.18.1`、`huggingface_hub`、`accelerate`、`deepspeed`。科学计算部分（[environment.yml:L43-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L43-L50)）则钉住 `numpy<2.0`（mmcv/mmdet3d 生态尚未全面兼容 numpy 2.x）。

这份清单与源码的对应关系可以直接验证——模型文件顶部就 import 了这些包：

- [uniseg3d/uniseg3d.py:L5-L7](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L5-L7) 同时导入 `spconv.pytorch`、`torch_scatter.scatter_mean` 和 `MinkowskiEngine`——这三个都是**顶层无条件 import**，任何一个缺失，`import uniseg3d` 立刻失败；
- [uniseg3d/query_decoder.py:L11-L13](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L11-L13) 导入 transformers 的 Qwen 模型类与 peft 的 LoRA 工具。

#### 4.1.4 代码实践

1. **实践目标**：确认 `environment.yml` 里装了什么、刻意漏了什么。
2. **操作步骤**：
   ```bash
   cd Ground3D-LMM              # 进入仓库根目录
   conda env create -f environment.yml
   conda activate ground3d
   pip list | grep -Ei "torch|mmengine|spconv|scatter|transformers|peft|setuptools|numpy"
   ```
3. **需要观察的现象**：`pip list` 输出中应出现 `torch 2.4.1+cu118`、`mmengine 0.10.7`、`spconv-cu118 2.3.8`、`torch-scatter 2.1.2`、`transformers 4.57.6`、`peft 0.18.1`、`setuptools` 版本号 **小于 70**、`numpy` 版本号小于 2。
4. **预期结果**：同时确认输出里**没有** `mmcv`、`mmdet`、`mmdet3d`、`MinkowskiEngine`——这印证了 4.1.3 读到的注释。此时 `python -c "import uniseg3d"` 必然报 `ModuleNotFoundError`，属正常现象，后面的模块逐个补齐。
5. 以上命令我没有在本机执行，输出为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `setuptools<70` 要放在 pip 依赖清单的第一组，而不是和 torch 一起随便放？
**答案**：mmcv 和 MinkowskiEngine 需要**源码编译**，编译发生在 `pip install` 它们的时候；如果届时环境里已经是新版 setuptools（≥70，移除了 `pkg_resources`），编译脚本会直接报 `ModuleNotFoundError: No module named 'pkg_resources'`。构建工具必须先于任何需要编译的包就位（见 [environment.yml:L10-L14](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L10-L14) 的注释）。

**练习 2**：Option A 跑完 `conda env create` 后，环境里缺哪些包？分别用什么命令补？
**答案**：缺 `mmcv`（源码编译，`MMCV_WITH_OPS=1 pip install "mmcv==2.1.0" --no-build-isolation`）、`mmdet`/`mmdet3d`（`mim install`）、`MinkowskiEngine`（git clone 后 `python setup.py install`）。对应 [docs/INSTALL.md:L38-L68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L38-L68) 的步骤 3、4、4b。

### 4.2 mmcv 源码编译

#### 4.2.1 概念说明

mmcv 是 OpenMMLab 的底层包，分「纯 Python 部分」和「CUDA 算子部分」（2.x 版本用环境变量 `MMCV_WITH_OPS=1` 控制是否编译算子）。mmdet/mmdet3d 的很多数据变换和后处理算子都来自 mmcv 的 CUDA 扩展，所以装 mmcv 必须带算子。

OpenMMLab 官方只为常见的 torch × CUDA 组合发布预编译 wheel。**torch 2.4.1 不在 mmcv 2.1.0 的预编译列表里**，于是 `pip install mmcv==2.1.0` 会自动落入源码编译路径——这就是本模块要处理的全部麻烦的根源。

源码编译 mmcv 需要三件事同时满足：

1. `CUDA_HOME` 指向本机 CUDA 工具链（编译期要调用 nvcc）；
2. 环境里的 setuptools 是旧版（编译脚本依赖 `pkg_resources`）；
3. `--no-build-isolation`，让编译进程能看到环境里已装好的 torch（mmcv 的构建脚本要在编译期读取 torch 的 CUDA 配置）。

#### 4.2.2 核心流程

```text
export CUDA_HOME=/usr/local/cuda-11.8
pip install "setuptools<70" wheel ninja
MMCV_WITH_OPS=1 pip install "mmcv==2.1.0" --no-build-isolation
        │  （pip 找不到预编译 wheel → 下载 sdist → 现场编译 CUDA 算子）
        ▼
mim install "mmdet==3.3.0"
mim install "mmdet3d==1.4.0"
```

三个前置条件各对应一种典型失败：

| 缺失条件 | 典型报错 | 文档位置 |
|---|---|---|
| setuptools ≥ 70 | `No module named 'pkg_resources'` | [docs/INSTALL.md:L42-L48](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L42-L48) 注释 |
| 未加 `--no-build-isolation` | 隔离环境里看不到 torch，编译配置失败 | 同上 |
| CUDA 工具链不匹配 | `ModuleNotFoundError: No module named 'mmcv._ext'`（编译给了但版本错位） | [docs/INSTALL.md:L139-L145](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L139-L145) |

#### 4.2.3 源码精读

[docs/INSTALL.md:L38-L51](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L38-L51) 是 Option B 的步骤 3，先 `pip install -U openmim` 和 `mim install "mmengine==0.10.7"`，然后：

- [docs/INSTALL.md:L46-L48](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L46-L48)：设 `CUDA_HOME`、钉 `setuptools<70`、以 `MMCV_WITH_OPS=1` 加 `--no-build-isolation` 安装 mmcv 2.1.0。文件里的注释原话是「mmcv 2.1.0 对 torch 2.4.1 没有预编译 wheel，pip 会从源码构建」。
- [docs/INSTALL.md:L50-L51](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L50-L51)：mmcv 装好后用 `mim`（OpenMMLab 的包管理器，它会自动挑选与 mmcv 兼容的版本）装 mmdet 3.3.0 和 mmdet3d 1.4.0。

装错时的自救方法在 Common issues 一节：[docs/INSTALL.md:L139-L145](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L139-L145) 说明 `mmcv._ext` 找不到意味着 mmcv 是按另一组 torch/CUDA 编的，需要先 `pip uninstall -y mmcv mmcv-full` 再 `mim install --force-reinstall "mmcv==2.1.0"` 强制重编。

顺带一提，本仓库对 mmcv 的依赖是**间接**的——`uniseg3d` 里没有 `import mmcv`，但 `import mmdet3d`（比如 [uniseg3d/uniseg3d.py:L9-L11](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L9-L11) 导入的 `mmdet3d.registry` / `mmdet3d.models`）会在内部加载 mmcv 的算子，所以 mmcv 坏了照样全盘崩溃。

#### 4.2.4 代码实践

1. **实践目标**：验证 mmcv 的 CUDA 算子真的编译成功（比 `import mmcv` 更强的检查）。
2. **操作步骤**：
   ```bash
   conda activate ground3d
   # 先完成 4.2.2 的三条安装命令
   python -c "import mmcv, mmcv.ops; print('mmcv', mmcv.__version__, 'ops OK')"
   ```
3. **需要观察的现象**：`import mmcv.ops` 会触发 C++ 扩展 `mmcv._ext` 的加载——这是 CUDA 算子是否编译成功的试金石。
4. **预期结果**：打印 `mmcv 2.1.0 ops OK`。若在此步报 `ModuleNotFoundError: No module named 'mmcv._ext'`，按 [docs/INSTALL.md:L139-L145](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L139-L145) 卸载重装。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`--no-build-isolation` 到底关掉了什么？不加会发生什么？
**答案**：关掉了 pip 默认的「构建隔离」——即在临时环境里构建源码包的机制。mmcv 的构建脚本需要在编译期 import torch 来确定 CUDA 版本和编译参数；隔离环境里没有 torch，构建会失败或产出错误配置（解释见 4.2.1，命令见 [docs/INSTALL.md:L48](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L48)）。

**练习 2**：为什么用 `mim install` 而不是 `pip install` 装 mmdet/mmdet3d？
**答案**：mim 是 OpenMMLab 官方包管理器，安装时会检查整套 OpenMMLab 依赖的版本兼容性并自动补齐配套的 mmcv/mmengine 系列依赖，避免 pip 各装各的造成版本错配。文档在 [docs/INSTALL.md:L39-L51](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L39-L51) 统一用它装 mmengine/mmdet/mmdet3d。

### 4.3 MinkowskiEngine 源码编译

#### 4.3.1 概念说明

MinkowskiEngine 是 NVIDIA 开源的稀疏张量与稀疏卷积库。在 Ground3D-LMM 里它**不是可选依赖**——[docs/INSTALL.md:L57-L59](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L57-L59) 明确标注它是 REQUIRED（在 `uniseg3d/uniseg3d.py` 顶部导入），缺了它 `import uniseg3d` 直接失败。

它的具体职责（第 3 单元会细讲，这里只看一眼）：

- `ME.utils.batch_sparse_collate`：把一个 batch 里多个场景的点云拼成一份「坐标 + 特征」稀疏描述；
- `ME.TensorField` + `inverse_mapping`：把连续坐标量化到体素网格，并保留「每个原始点 → 所在体素」的反向映射——这正是后面超点池化对齐所需的基础设施。

它也是全环境**最脆**的一个依赖：需要 CUDA 工具链 + OpenBLAS 头文件，而且官方 v0.5.4 tag 与 torch 2.4 存在 CUDA 模板不兼容，必须改用 master 分支（文档测试用的 commit 是 `02fc608`）。

#### 4.3.2 核心流程

```text
sudo apt-get install -y libopenblas-dev        # 系统 OpenBLAS 头文件
export CUDA_HOME=/usr/local/cuda-11.8 MAX_JOBS=8
pip install "setuptools<70"
git clone https://github.com/NVIDIA/MinkowskiEngine.git
cd MinkowskiEngine
python setup.py install --blas=openblas --force_cuda
```

三个关键坑（全部来自文档注释）：

| 坑 | 原因 | 对策 | 文档位置 |
|---|---|---|---|
| OpenBLAS 选错来源 | conda-forge 的 `openblas-devel` 会触发运行时 `libgfortran.so.3 / GFORTRAN_1.0 not found` | 用**系统**包 `libopenblas-dev` | [docs/INSTALL.md:L60-L61](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L60-L61) |
| setuptools/pip 过新 | 新版丢弃了 ME 依赖的构建标志 | `pip install "setuptools<70"` | [docs/INSTALL.md:L63](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L63) |
| v0.5.4 tag 编译失败 | tag 版本对 torch 2.4 报 CUDA 模板错误 | 从 **master 分支**构建（自报版本仍是 0.5.4），测试 commit `02fc608` | [docs/INSTALL.md:L64-L68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L64-L68) |

`MAX_JOBS=8` 限制并行编译任务数，防止编译过程中内存耗尽（每个 nvcc 进程都很吃内存）。

#### 4.3.3 源码精读

依赖必须性的直接证据在模型主文件顶部：

[uniseg3d/uniseg3d.py:L5-L7](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L5-L7) 无条件导入了三个 CUDA 库：`spconv.pytorch`（稀疏卷积主干用）、`torch_scatter.scatter_mean`（超点池化用）、`MinkowskiEngine`。由于 [uniseg3d/\_\_init\_\_.py:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/__init__.py#L1) 的第一行就是 `from .uniseg3d import UniSeg3D`，这三个 import 是 `import uniseg3d` 的第一道关卡。

ME 在模型里的实际使用位置（本讲只需看懂「它被用来组装稀疏批」）：[uniseg3d/uniseg3d.py:L399-L415](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L399-L415) 中，`ME.utils.batch_sparse_collate` 把每个场景的点坐标除以 `voxel_size` 得到体素坐标并拼接，`ME.TensorField` 把连续特征场转成稀疏张量，`field.inverse_mapping(...)` 取回每个点所属体素的索引。这段代码就是「MinkowskiEngine 装不上会死」的原因——它在每个 batch 的特征提取入口被调用。

安装命令本身见 [docs/INSTALL.md:L57-L68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L57-L68)（Option B 步骤 4b），每行前面都有注释解释为什么这么做。

同一小节里 spconv 和 torch-scatter 的安装（[docs/INSTALL.md:L53-L55](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L53-L55)）则是「预编译 wheel 路线」：`spconv-cu118==2.3.8` 按后缀选 CUDA 版本；torch-scatter 必须带 `-f https://data.pyg.org/whl/torch-2.4.0+cu118.html` 指向 PyG 的 wheel 索引，否则 pip 会尝试源码编译且大概率失败（[docs/INSTALL.md:L160-L162](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L160-L162)）。spconv 的 GPU 架构坑见 [docs/INSTALL.md:L154-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L154-L158)：H100（sm_90）需要换 `spconv-cu120` + PyTorch CUDA 12.x，老架构（sm_60 以下）要自己编译。

#### 4.3.4 代码实践

1. **实践目标**：验证 MinkowskiEngine 与 spconv 均可导入且能在 GPU 上创建稀疏张量。
2. **操作步骤**：
   ```bash
   # 完成 4.3.2 的编译安装后，回到仓库根目录
   python - <<'EOF'
   import MinkowskiEngine as ME
   import spconv.pytorch as spconv
   import torch
   print('ME     :', ME.__version__)
   print('spconv :', spconv.__version__ if hasattr(spconv, '__version__') else 'installed')

   # 最小稀疏张量构造：两个体素坐标 (batch_idx, x, y, z) + 1 维特征
   coords = torch.IntTensor([[0, 1, 1, 1], [0, 2, 2, 2]]).cuda()
   feats = torch.FloatTensor([[1.0], [2.0]]).cuda()
   st = spconv.SparseConvTensor(feats, coords, spatial_shape=[10, 10, 10], batch_size=1)
   print('spconv sparse tensor OK, indices shape:', st.indices.shape)

   fld = ME.TensorField(features=feats, coordinates=coords)
   print('ME TensorField OK, sparse tensor shape:', fld.sparse().shape)
   EOF
   ```
3. **需要观察的现象**：三段输出依次证明 ME 导入成功、spconv 能在 GPU 上构造 `SparseConvTensor`（这正是 [uniseg3d/uniseg3d.py:L457](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L457) 里主干用的数据结构）、ME 的 `TensorField` 量化正常。
4. **预期结果**：无报错打印出各版本与形状。若在无 GPU 机器上运行会报 CUDA 相关错误——本验证必须在有 NVIDIA GPU 的机器上做。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `pip install MinkowskiEngine==0.5.4`？
**答案**：两个原因：(a) PyPI 上没有可用的预编译 wheel，本来就要源码编译；(b) v0.5.4 的 **tag** 对 torch 2.4 编译时会报 CUDA 模板错误，文档要求改从 master 分支构建（测试 commit `02fc608`），master 自报版本仍是 0.5.4（[docs/INSTALL.md:L64-L68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L64-L68)）。

**练习 2**：文档为什么强烈建议用系统的 `libopenblas-dev` 而不是 conda-forge 的 openblas？
**答案**：conda-forge 的 `openblas-devel` 编出的库在运行时会报 `libgfortran.so.3 / GFORTRAN_1.0 not found`（gfortran 运行时版本错位），而系统包依赖完整（[docs/INSTALL.md:L60-L61](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L60-L61) 注释）。

**练习 3**：spconv 报 `CUDA error: no kernel image is available`，你的 GPU 是 H100，怎么办？
**答案**：预编译 spconv wheel 不含 sm_90 的核。换 `spconv-cu120` 并整体切换到 PyTorch CUDA 12.x（[docs/INSTALL.md:L154-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L154-L158)）；注意这意味着 torch 也得重装成 cu120 版本，回到 4.1 重新对齐三方版本。

### 4.4 本包安装与依赖验证

#### 4.4.1 概念说明

前三步装的都是「别人的包」，最后一步把本仓库自己装进环境：`pip install -e .`（editable / 可编辑安装——源码改动立即生效，不需要重复安装，适合跟着本手册改代码学习的场景）。

看 [pyproject.toml:L33-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L33-L43) 会发现它的 `dependencies` 少得可怜：只有 `huggingface_hub`、`plyfile`、`numpy<2.0`、`scipy` 四项。注释解释了原因：**torch、spconv、torch-scatter、mmcv 这些重型 CUDA 依赖被刻意不写进来**，因为它们需要与 CUDA 匹配的特定 wheel，必须由 environment.yml / INSTALL.md 的流程先装好。也就是说，`pip install -e .` 的前提是前面所有步骤已经完成——这个包的元数据假设你处于一个「已经装好的 Ground3D 环境」里。

[pyproject.toml:L58-L59](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L58-L59) 的 `packages = ["uniseg3d"]` 说明只有 `uniseg3d` 目录是 Python 包；`tools/`、`configs/` 不参与安装，永远是仓库里的脚本和配置文件。

最后还有一个**文档没写全的坑**：源码 import 的三个包不在任何依赖清单里。`import uniseg3d` 的 import 链是：

```text
import uniseg3d
  └─ uniseg3d/__init__.py:L1-L3
       ├─ from .uniseg3d import ...      → uniseg3d.py:L5-L7   需要 spconv / torch_scatter / MinkowskiEngine ✅已装
       └─ from .query_decoder import ... → query_decoder.py:L20 需要 point_qwen3_vl（本仓库文件）
                                          → query_decoder.py:L23 需要 qwen_vl_utils ⚠️未列于任何清单
                                             └─ point_qwen3_vl.py:L7-L9 需要 hydra、nncore ⚠️未列于任何清单
```

`qwen_vl_utils`（[uniseg3d/query_decoder.py:L23](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L23)，处理 Qwen-VL 的图像输入）、`hydra` 与 `nncore`（[uniseg3d/point_qwen3_vl.py:L7-L9](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L7-L9)）在 environment.yml、INSTALL.md、pyproject.toml 里都没有出现（可以用 grep 复核）。它们是否会被已列出的依赖间接装上**待本地验证**；如果 `import uniseg3d` 报这三个名字的 `ModuleNotFoundError`，按报错逐个 `pip install qwen-vl-utils hydra-core nncore` 即可。这本身就是一条重要方法论：**判断依赖是否齐全，以源码 import 为准，而不是以文档清单为准**。

#### 4.4.2 核心流程

```text
pip install -e .                          # 步骤 7（docs/INSTALL.md:L79-L80）
        ▼
跑官方 verify 脚本                        # 检查 10 个关键包的版本（docs/INSTALL.md:L83-L101）
        ▼
python -c "import uniseg3d"               # 更强的端到端检查：整条 import 链
        ▼
（可选）跑 smoke test 构建完整模型          # 需要 Qwen3-VL-4B 权重（docs/INSTALL.md:L117-L133）
```

#### 4.4.3 源码精读

- **editable 安装**：[docs/INSTALL.md:L79-L80](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L79-L80) 的步骤 7；README 的安装段（[README.md:L42-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L42-L50)）同样以它收尾，并指路 INSTALL.md 处理源码编译部分。
- **官方 verify 脚本**：[docs/INSTALL.md:L83-L101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L83-L101) 一口气导入 torch、mmengine、mmcv、mmdet、mmdet3d、spconv、torch_scatter、transformers、peft、MinkowskiEngine 并打印版本与 GPU 数；期望输出（torch 2.4.1+cu118、mmcv 2.1.0、ME 等）在 [docs/INSTALL.md:L103-L115](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L103-L115)。
- **smoke test**：[docs/INSTALL.md:L117-L133](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L117-L133) 不需要任何数据，直接 `Config.fromfile` 读 eval 配置、`MODELS.build(cfg.model)` 构建完整 4.47B 参数模型——这是「环境 + 权重下载」双重验证，成功打印 `OK: model built. 4.47B params total.`（[docs/INSTALL.md:L135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L135)）。注意它会触发 Qwen3-VL-4B 基座权重的下载（约 9 GB 量级），首次运行较慢。
- **入口脚本与本包的关系**：[tools/test.py:L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L6) 在脚本开头把仓库根目录 `sys.path.append` 进来，所以即便不装包、只用 `PYTHONPATH=.` 也能让 `tools/*.py` 找到 `uniseg3d`；editable 安装则让你在任何目录都能 `import uniseg3d`。
- **显存门槛**：[docs/INSTALL.md:L168-L176](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L168-L176) 给出三档：推理单场景约 16 GB；LoRA 微调 batch=1 约 40 GB；batch=4 约 80 GB。24 GB 显卡想训练，需把配置里的 `max_point_tokens` 从 2000 降到 1500 和/或缩短 `max_seq_length`。

#### 4.4.4 代码实践

1. **实践目标**：完成本包安装，并用「官方脚本 + import 链」两层验证环境真正可用。
2. **操作步骤**：
   ```bash
   cd <你的 Ground3D-LMM 仓库根目录路径>
   pip install -e .

   # 第一层：官方 verify（逐字来自 docs/INSTALL.md:L86-L100）
   python -c "
   import torch, mmengine, mmcv, mmdet, mmdet3d, spconv, torch_scatter, transformers, peft
   import MinkowskiEngine
   print('torch    :', torch.__version__, 'CUDA:', torch.version.cuda)
   print('MinkowskiEngine:', MinkowskiEngine.__version__)
   print('GPUs     :', torch.cuda.device_count())
   "

   # 第二层：端到端 import 链检查
   python -c "import uniseg3d; print('uniseg3d import OK')"
   ```
3. **需要观察的现象**：第一层输出与 [docs/INSTALL.md:L103-L115](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L103-L115) 的期望值一致；第二层若报 `ModuleNotFoundError`，**看报错的模块名**——如果是 `qwen_vl_utils` / `hydra` / `nncore`，属于 4.4.1 分析的文档遗漏，`pip install qwen-vl-utils hydra-core nncore` 后重试；如果是别的包名，说明前面某个步骤没装好，回到对应模块。
4. **预期结果**：最终打印 `uniseg3d import OK`。此时环境已足以进入第 1 单元第 4 讲的推理实践（还需要下载 checkpoint 与数据，那是下一讲的内容）。
5. 待本地验证（尤其是 `qwen_vl_utils`/`hydra`/`nncore` 是否需要手动补装这一点，请以你的实际报错为准）。

#### 4.4.5 小练习与答案

**练习 1**：`pip install -e .` 之后，`pyproject.toml` 里声明的四个依赖（huggingface_hub、plyfile、numpy、scipy）分别在仓库哪里被用到？
**答案**：`huggingface_hub` 用于从 HF 下载数据集/模型（README 的 `snapshot_download` 示例）；`plyfile` 在可视化模块 [uniseg3d/grounding_viz.py:L17](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L17) 导入，用于写 PLY 点云文件；`scipy` 的 `linear_sum_assignment` 出现在多个匹配损失里（如 [uniseg3d/text_prompt_instance_criterion.py:L4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L4)）；`numpy` 则无处不在。可见这四项都是纯 Python 可装、无 CUDA 代价的包，符合「本包元数据只管轻依赖」的设计。

**练习 2**：官方 verify 脚本已经全绿，为什么还要跑 `import uniseg3d` 这第二层检查？
**答案**：verify 只覆盖文档列出的 10 个包；而 `import uniseg3d` 走的是 4.4.1 画出的**完整 import 链**，能暴露文档未列出的依赖（qwen_vl_utils/hydra/nncore）以及任何「装上了但导入即崩」的问题（例如 ME 动态链接库缺失）。验证环境要验证到「你真正要用的那行 import」为止。

**练习 3**：为什么 `tools/` 和 `configs/` 不打包进 pip 包？
**答案**：它们不是库代码：`tools/*.py` 是命令行入口脚本（由用户在仓库根目录用 `PYTHONPATH=.` 或 `pip install -e .` 后直接运行），`configs/*.py` 是 mmengine 的配置文件（按相对路径被 `Config.fromfile` 读取）。[pyproject.toml:L58-L59](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L58-L59) 的 `packages = ["uniseg3d"]` 只把 `uniseg3d` 作为可分发代码。

## 5. 综合实践

**任务：在一台全新机器上从零搭好 Ground3D-LMM 环境，并产出一份「安装核对清单」。**

1. **实践目标**：把本讲四个模块串成一条完整的安装流水线，最终以 `import uniseg3d` 成功为验收标准。
2. **操作步骤**：
   1. 先通读 [docs/INSTALL.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md) 全文（约 180 行），对照本讲 4.1-4.4 建立整体地图；
   2. 尝试 Option A：`conda env create -f environment.yml && conda activate ground3d`；失败则整体放弃该环境，按 Option B 从头逐步装（步骤 1-2 建环境与 torch → 步骤 3 mmcv/mmdet/mmdet3d → 步骤 4/4b spconv、torch-scatter、MinkowskiEngine → 步骤 5-6 LLM 与杂项 → 步骤 7 `pip install -e .`）；
   3. 每完成一步，在下表打勾并记录实际版本号；
   4. 全部完成后跑 4.4.4 的两层验证；有余力再跑 [docs/INSTALL.md:L117-L133](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L117-L133) 的 smoke test（会自动下载 Qwen3-VL-4B 基座权重，耗时较长，且预期打印 `OK: model built. 4.47B params total.`——待本地验证）。
3. **安装核对清单**（建议复制到自己的笔记里逐项勾选）：

   | # | 检查项 | 依据 | 勾选 |
   |---|---|---|---|
   | 1 | torch 2.4.1+cu118，`torch.cuda.is_available()` 为 True | INSTALL.md L13, L35-L36 | ☐ |
   | 2 | setuptools < 70、wheel、ninja 已就位 | INSTALL.md L47 | ☐ |
   | 3 | mmengine 0.10.7 | INSTALL.md L40 | ☐ |
   | 4 | mmcv 2.1.0 且 `import mmcv.ops` 成功 | INSTALL.md L48, L139-L145 | ☐ |
   | 5 | mmdet 3.3.0、mmdet3d 1.4.0 | INSTALL.md L50-L51 | ☐ |
   | 6 | spconv-cu118 2.3.8；torch-scatter 2.1.2（带 PyG `-f` 索引装的） | INSTALL.md L54-L55 | ☐ |
   | 7 | MinkowskiEngine 从 master（commit 02fc608）编译成功，`ME.__version__` 可打印 | INSTALL.md L57-L68 | ☐ |
   | 8 | transformers 4.57.6、peft 0.18.1 | INSTALL.md L71 | ☐ |
   | 9 | `pip install -e .` 成功 | INSTALL.md L79-L80 | ☐ |
   | 10 | 官方 verify 脚本输出与 L103-L115 一致 | INSTALL.md L83-L115 | ☐ |
   | 11 | `python -c "import uniseg3d"` 成功（必要时补装 qwen-vl-utils/hydra-core/nncore） | query_decoder.py L23、point_qwen3_vl.py L7-L9 | ☐ |

4. **预期结果**：清单 11 项全勾。此时你拥有一个能 `import uniseg3d`、能构建 4.47B 模型的完整环境——下一讲直接用它跑第一次真实推理。
5. 本综合实践的所有命令均来自 [docs/INSTALL.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md)，我在本讲义撰写过程中未实际执行，安装耗时与各步骤成功率「待本地验证」。

## 6. 本讲小结

- Ground3D-LMM 的环境难装是**结构性的**：OpenMMLab 栈 + 两个稀疏卷积库 + LLM 栈，CUDA 产物必须与 PyTorch 版本、CUDA 工具链、GPU 架构三方对齐（[docs/INSTALL.md:L3](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L3)）。
- `environment.yml` **刻意不完整**：mmcv/mmdet/mmdet3d/MinkowskiEngine 都不在里面，Option A 之后必须按 INSTALL.md 步骤 3、4、4b 手动补装（[environment.yml:L25-L29](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/environment.yml#L25-L29)）。
- mmcv 2.1.0 对 torch 2.4.1 没有预编译 wheel，源码编译三要素：`CUDA_HOME`、`setuptools<70`、`--no-build-isolation`（[docs/INSTALL.md:L42-L48](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L42-L48)）。
- MinkowskiEngine 是最脆的一环：系统版 `libopenblas-dev`、`setuptools<70`、从 master（02fc608）而非 v0.5.4 tag 构建（[docs/INSTALL.md:L57-L68](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L57-L68)）；它被 `uniseg3d/uniseg3d.py:L7` 顶层导入，用于稀疏批组装与坐标量化（L399-L415），装不上则 `import uniseg3d` 必败。
- 本包 `pip install -e .` 只声明四个纯 Python 依赖，重型 CUDA 依赖全靠前置环境（[pyproject.toml:L33-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L33-L43)）。
- 验证要验证到 import 链：官方 verify 脚本之外，`import uniseg3d` 能暴露文档未列出的依赖（qwen_vl_utils / hydra / nncore）——**以源码 import 为准，而不是以文档清单为准**。

## 7. 下一步学习建议

环境就绪后，按依赖顺序继续：

- **下一讲（u1-l3）「仓库结构与 OpenMMLab 注册机制」**：搞清楚 `configs/*.py` 里的 `type='Grounded_LMM_main'` 字符串如何通过 mmdet3d registry 解析到 `uniseg3d` 包里的真实类——你会更深刻地理解本讲装的 mmengine/mmdet3d 在框架层扮演的角色。
- **u1-l4「快速上手：用预训练权重跑一次接地推理」**：用刚建好的环境下载 checkpoint、跑 `tools/test.py`，完成第一次端到端推理。
- **源码阅读建议**：本讲反复出现的 [uniseg3d/uniseg3d.py:L399-L415](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L399-L415)（ME 批组装与量化）不必现在读懂，到第 3 单元「超点池化」一讲会逐行拆解。
- **排障参考**：安装中遇到本讲未覆盖的报错，先查 [docs/INSTALL.md:L137-L167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L137-L167) 的 Common issues 一节（覆盖 `mmcv._ext`、`libcuda.so.1`、spconv 架构、torch-scatter、transformers 版本五类问题）。
