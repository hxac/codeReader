# 环境搭建与人体模型资产准备

## 1. 本讲目标

学完本讲，你应该能够：

1. 按照仓库要求创建 `pear` conda 环境，正确安装 `requirements.txt` 以及两个「特殊安装」的包：`pytorch3d` 和 `chumpy`。
2. 知道 SMPL、SMPL-X、FLAME、SMPLX2SMPL 四类人体模型资产分别要放在 `assets/` 下的哪个子目录、哪些文件仓库已经自带、哪些必须手动下载，并理解**为什么**要这样放（由源码中的加载路径决定，而不是随意约定）。
3. 读懂 `app.py` 顶部的环境校验代码：它如何用 `import chumpy` / `import pytorch3d` / `from pytorch3d import _C` 快速判断环境是否可用，以及那段被注释掉的 `migrate_precompiled_packages()` 是干什么的。

本讲是纯「动手准备」的一讲，不涉及任何模型推理逻辑，但如果没有把这一讲做完，后面所有讲义的代码实践都跑不起来。

## 2. 前置知识

- **conda 与 pip**：conda 用来创建隔离的 Python 环境（决定 Python 解释器版本），pip 用来在环境内装包。PEAR 要求 Python 3.9.22（README 中指定）。
- **PyTorch 与 CUDA**：PyTorch 是深度学习框架；CUDA 是 NVIDIA 的 GPU 计算栈。`torch==2.0.1+cu118` 里的 `+cu118` 表示「绑定 CUDA 11.8 编译的版本」，这类带本地版本标签的包通常要从 PyTorch 官方源安装。不过 README 明确说明版本不强制，多数兼容组合都能工作。
- **编译型扩展（以 pytorch3d 为例）**：`pytorch3d` 是 Facebook Research 开源的 3D 几何库，核心算子（光栅化、顶点变换等）用 C++/CUDA 编写。用 `pip install git+...` 安装时，pip 会在你机器上**现场编译**这些算子，因此编译时机器上必须已经装好 torch——这正是需要 `--no-build-isolation` 的原因（见 4.1.3）。
- **人体参数化模型与许可证**：SMPL / SMPL-X / FLAME 是马克斯·普朗克研究所（MPI）发布的参数化人体/人头模型，**受许可证保护，不能随代码仓库分发**。所以仓库里只有加载代码，没有模型文件本身，你需要到官网注册并手动下载。这也是本讲存在的根本原因。
- **pickle 反序列化与 chumpy**：`.pkl` 模型文件（如 `SMPL_NEUTRAL.pkl`）是用 Python 的 pickle 格式保存的，文件内部嵌有 `chumpy` 的数组对象；读取时环境里必须装有 chumpy，否则 `pickle.load` 会直接报 `ModuleNotFoundError`。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| [requirements.txt](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt) | 全部 Python 依赖清单，含精确版本号 |
| [README.md](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md) | 环境安装命令（62-74 行）与模型资产放置说明（77-93 行），是本讲唯一的「官方说明书」 |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | Gradio 视频演示入口；顶部 1-65 行是环境迁移与环境校验代码，是 4.3 节的精读对象 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | EHM 人体模型构造函数，接收 `flame_assets_dir` / `smplx_assets_dir` 两个资产目录参数 |
| [models/modules/smplx/SMPLX.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py) | 从 `assets/SMPLX` 读取 `SMPLX_NEUTRAL_2020.npz` 与 `flame_generic_model.pkl` |
| [models/modules/flame/FLAME.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py) | 从 `assets/FLAME` 读取 `FLAME2020/generic_model.pkl` 等文件 |
| [utils/smplx2smpl_joints.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/smplx2smpl_joints.py) | 训练/评测时从 `assets/SMPL`、`assets/SMPLX2SMPL` 读取 SMPL 与转换矩阵 |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 推理入口之一，第 54/66 行展示了资产目录在入口处的传法 |

另外建议在本机执行 `ls assets/` 对照 4.2 节的目录表，感受「仓库自带的文件」和「需要你补齐的文件」的差别。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**requirements.txt 依赖清单**、**assets 目录约定**、**app.py 顶部的环境校验代码**。

### 4.1 requirements.txt 依赖清单

#### 4.1.1 概念说明

`requirements.txt` 是 Python 项目的依赖声明文件，`pip install -r requirements.txt` 会逐行安装。PEAR 的这份清单有三个特点值得注意：

1. **大部分包带精确版本号**（如 `numpy==1.22.4`、`gradio==4.44.1`），这是一个研究型项目的典型做法——作者只在自己验证过的组合上保证能跑。
2. **两个最麻烦的包根本不在清单里**：`pytorch3d` 和 `chumpy` 需要单独用特殊命令安装（见 4.1.3），因为它们一个需要现场编译、一个的构建脚本在 pip 的隔离构建模式下会失败。
3. **依赖可以按功能分组理解**，而不是死记 70 多行包名。理解分组后，将来报 `ImportError` 时你能立刻定位是哪类功能缺失。

#### 4.1.2 核心流程

安装流程（来自 README）：

```text
git clone --recursive https://github.com/Pixel-Talk/PEAR.git   # --recursive 拉取子模块
cd PEAR
conda create -n pear python=3.9.22
conda activate pear
pip install -r requirements.txt                                # 第一步：装常规依赖
pip install "git+https://github.com/facebookresearch/pytorch3d.git" --no-build-isolation   # 第二步：编译安装 pytorch3d
pip install chumpy --no-build-isolation                        # 第三步：装 chumpy
```

依赖分组速查（行号见 4.1.3）：

| 分组 | 代表包 | 在 PEAR 中的用途 |
| --- | --- | --- |
| 深度学习基座 | `torch` / `torchvision` / `lightning` / `timm` | 模型训练与推理（`lightning.Fabric` 管理分布式训练） |
| 3D 渲染几何 | `pytorch3d`（清单外）、`pyrender`、`open3d`、`utils3d`、`roma` | 网格光栅化、旋转数学、网格可视化 |
| 图像与视频 I/O | `opencv-*`、`pillow`、`imageio`、`decord` | 读图、读视频、写 mp4 |
| 人体模型生态 | `smplx==0.1.28`、`chumpy`（清单外） | 读取官方 SMPL 系列模型文件 |
| 配置系统 | `omegaconf`、`configparser`、`ConfigArgParse` | YAML 配置加载（下一单元 u2-l1 精讲） |
| 演示与下载 | `gradio`、`huggingface_hub` | Web 演示界面、自动下载预训练权重 |
| 目标检测 | `ultralytics` | YOLOv8 多人检测（u2-l3 精讲） |
| 数据管线 | `pyarrow`、`webdataset` 生态相关 | 训练时读取 tar 分片数据集 |

#### 4.1.3 源码精读

requirements.txt 的头部固定了 PyTorch 三件套的版本：

```text
torch==2.0.1+cu118
torchvision
torchaudio
```

这段指定了 CUDA 11.8 版的 PyTorch 2.0.1，[requirements.txt:1-3](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L1-L3)。注意 `+cu118` 这种本地版本标签在默认 PyPI 源上通常搜不到，如果 `pip install -r requirements.txt` 在这一步报「找不到匹配的发行版」，需要追加 PyTorch 官方源或放宽版本（README 已声明版本不严格，待本地验证）。

科学计算与配置部分：

- [requirements.txt:35-36](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L35-L36) 把 `numpy` 钉在 `1.22.4`、`omegaconf` 钉在 `2.3.0`。numpy 这个旧版本 pin 值得留意：chumpy 是老包，对新版 numpy 的 API 变化敏感，环境里 numpy 过新时 chumpy 相关代码可能报错（待本地验证），排错时优先怀疑这里。
- [requirements.txt:64](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L64) 的 `smplx==0.1.28` 是官方 SMPL-X 的 pip 包。注意区分：PEAR **推理主链路用的是自己实现的** `models/modules/smplx/SMPLX.py`，而这个 pip 包主要被训练/评测侧的 `utils/smplx2smpl_joints.py` 使用（见 4.2.3）。

清单尾部一批不带版本号的包：

- [requirements.txt:66-73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L66-L73) 列出 `ultralytics`（YOLOv8）、`einops`、`timm`、`lightning` 等。`lightning` 是训练入口 `train_ehms.py` 依赖的分布式训练框架，`ultralytics` 是 `inference_images.py` 多人检测的来源。

两个特殊安装命令只出现在 README，不出现在 requirements.txt：

```bash
pip install -r requirements.txt
pip install "git+https://github.com/facebookresearch/pytorch3d.git" --no-build-isolation
pip install chumpy --no-build-isolation
```

见 [README.md:71-73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L71-L73)。而 [README.md:66-68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L66-L68) 明确注释：指定的 PyTorch / Python / CUDA 版本**不是硬性要求**，大多数兼容配置都能工作——排错时不必死磕精确版本号。

关于 `--no-build-isolation` 的含义：pip 默认在隔离的临时环境里执行构建脚本，那个环境里没有已安装的 torch 和 numpy。而 pytorch3d 编译 CUDA 扩展时需要 import torch 获取头文件路径，chumpy 的构建脚本依赖 numpy，因此两者都要加 `--no-build-isolation` 让构建过程直接使用当前环境。

#### 4.1.4 代码实践

**实践目标**：在你自己的机器上建出 `pear` 环境，完成三步安装。

**操作步骤**：

1. 克隆仓库（注意 `--recursive`）并进入目录：

   ```bash
   git clone --recursive https://github.com/Pixel-Talk/PEAR.git
   cd PEAR
   ```

2. 创建并激活环境：

   ```bash
   conda create -n pear python=3.9.22
   conda activate pear
   ```

3. 依次执行 README 的三条安装命令（见 4.1.2 流程）。pytorch3d 编译可能需要十几分钟到更久，属于正常现象。

**需要观察的现象**：

- 三条命令各自正常结束、无红色报错；
- `pip list | grep -E "torch|pytorch3d|chumpy"` 能看到 `pytorch3d` 与 `chumpy` 已在包列表中。

**预期结果**：环境内 `python -c "import torch; print(torch.__version__)"` 能输出版本号。

**待本地验证**：本讲义编写环境未实际执行安装，以上命令耗时、编译是否一次通过（依赖本机 CUDA toolkit、gcc 版本）均需在你的机器上验证。若 pytorch3d 编译失败，可先确认 `nvcc --version` 与 torch 的 CUDA 主版本一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pytorch3d` 和 `chumpy` 不写进 `requirements.txt`，而要单独用带 `--no-build-isolation` 的命令安装？

**参考答案**：pytorch3d 的核心算子是 C++/CUDA 扩展，从 git 源码安装时要在本机现场编译，编译过程需要读取当前环境里已装好的 torch 的头文件与 ABI 信息；chumpy 的构建脚本则依赖 numpy。pip 默认的隔离构建会在一个「干净」的临时环境里跑构建，那里没有 torch/numpy，导致构建失败。`--no-build-isolation` 让构建直接使用当前环境，问题即可规避。这类「安装顺序敏感」的包放进 requirements.txt 反而容易让别人踩坑，所以 README 选择单独给出命令。

**练习 2**：装完环境后运行推理脚本报 `ModuleNotFoundError: No module named 'chumpy'`，最可能的原因是什么？

**参考答案**：最可能是没有执行 `pip install chumpy --no-build-isolation`（它不在 requirements.txt 里），或者执行时没有激活 `pear` 环境（装到了别的 Python 里）。用 `which python` 与 `pip list | grep chumpy` 确认当前解释器和包状态即可定位。

**练习 3**：`requirements.txt` 里 `numpy==1.22.4` 这个偏旧的 pin，可能和哪个清单外的包有关？

**参考答案**：chumpy。chumpy 是较早的包，内部用到了旧版 numpy 的 API；把 numpy 升级到较新版本后 chumpy 可能无法工作，因此作者把 numpy 钉在 1.22.4（此关联为常见经验，待本地验证）。

### 4.2 assets 目录约定

#### 4.2.1 概念说明

PEAR 推理时需要两类「模型」：

- **网络权重**（`pear_model.pt`）：不需要你手动准备，首次运行时由 `huggingface_hub.hf_hub_download` 自动下载（[inference_wo_detect.py:56-58](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L56-L58)）。
- **人体模型模板资产**（SMPL-X 模板、FLAME 头模板等）：受许可证保护，**必须手动下载并放到 `assets/` 下的固定位置**。

「固定位置」不是文档上的随意约定，而是源码里写死的相对路径——入口脚本用字符串字面量把资产目录传给模型构造函数。所以理解 assets 约定的正确方法是：**反着读代码**，从「哪个类加载了哪个文件」推导出「文件必须放哪」。

另外要区分两类资产目录的使用场景：

- **推理必需**：`assets/FLAME`、`assets/SMPLX`（EHM_v2 与渲染器都要用）；
- **仅训练/评测需要**：`assets/SMPL`、`assets/SMPLX2SMPL`（只有 `models/pipeline/pipeline.py` 通过 `utils/smplx2smpl_joints.py` 使用）。只想跑推理的话，后两个目录缺失不影响；但建议一次配齐，后面单元五的训练实践会用到。

#### 4.2.2 核心流程

推理入口构造人体模型的调用链（以 `app.py` 为例）：

```text
app.py
 ├── BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)      # 渲染器：读 assets/SMPLX 下的 obj 拓扑/UV
 └── EHM_v2("assets/FLAME", "assets/SMPLX")                     # 统一人体模型
      ├── SMPLX(smplx_assets_dir="assets/SMPLX")
      │     ├── 读 assets/SMPLX/SMPLX_NEUTRAL_2020.npz          # 模板顶点/骨架/蒙皮权重（numpy npz）
      │     └── 读 assets/SMPLX/flame_generic_model.pkl         # FLAME 模板（在 SMPLX 目录下的副本）
      └── FLAME(flame_assets_dir="assets/FLAME")
            ├── 读 assets/FLAME/FLAME2020/generic_model.pkl     # FLAME 官方模型
            ├── 读 assets/FLAME/landmark_embedding.npy          # 关键点嵌入（仓库自带）
            └── 读 assets/FLAME/FLAME_masks/FLAME_masks.pkl     # 部位掩码（仓库自带）
```

训练侧另有：

```text
models/pipeline/pipeline.py ──import──> utils/smplx2smpl_joints.py（模块级立即执行）
 ├── 读 assets/SMPLX2SMPL/body_models/smplx2smpl.pkl            # SMPL-X→SMPL 顶点转换矩阵
 ├── 读 assets/SMPL/SMPL_NEUTRAL.pkl                            # SMPL 中性模板（chumpy pickle）
 ├── 读 assets/SMPLX2SMPL/SMPL_to_J19.pkl                       # J19 关节回归器
 └── 读 assets/SMPLX2SMPL/data/J_regressor_extra.npy            # 额外关节回归器
```

注意 `utils/smplx2smpl_joints.py` 的读取发生在**模块 import 时**（第 41-45 行是模块顶层语句），所以只要训练管线被 import 而这些文件缺失，就会立刻报错——这也是为什么训练前必须配齐 SMPL 与 SMPLX2SMPL。

#### 4.2.3 源码精读

README 的资产准备说明是总纲：

- [README.md:77-80](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L77-L80) 逐条给出四个模型的下载与放置要求：`SMPL_NEUTRAL.pkl` 放 `assets/SMPL`；`SMPLX_NEUTRAL_2020.npz` 放 `assets/SMPLX`；FLAME 的 `generic_model.pkl` 要**同时保存两份**——`assets/FLAME/FLAME2020/generic_model.pkl` 和 `assets/SMPLX/flame_generic_model.pkl`；`SMPLX2SMPL.zip` 解压即可。
- [README.md:82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L82) 提供打包下载全部资产的 Google Drive 链接。
- [README.md:84-93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L84-L93) 给出目标目录树。

「FLAME 模型要放两份」的原因直接写在 SMPLX 类的加载代码里：

```python
smplx_model_path = osp.join(smplx_assets_dir, 'SMPLX_NEUTRAL_2020.npz')
ss = np.load(smplx_model_path, allow_pickle=True)
smplx_model = Struct(**ss)

flame_model_path = osp.join(smplx_assets_dir, 'flame_generic_model.pkl')
with open(flame_model_path, 'rb') as f:
    ss = pickle.load(f, encoding='latin1')
    flame_model = Struct(**ss)
```

[models/modules/smplx/SMPLX.py:134-141](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L134-L141)：SMPLX 类在自己的资产目录里既读 `.npz`（numpy 原生格式，不需要 chumpy）也读 FLAME 的 `.pkl`（pickle，`encoding='latin1'`），因此 SMPLX 目录下必须有 FLAME 模型副本。

FLAME 类则从自己的目录按相对路径拼接文件名：

```python
flame_model_path = osp.join(flame_assets_dir, 'FLAME2020/generic_model.pkl')
flame_lmk_embedding_path = osp.join(flame_assets_dir, 'landmark_embedding.npy')
...
flame_mask_dir = osp.join(flame_assets_dir, "FLAME_masks/FLAME_masks.pkl")
```

[models/modules/flame/FLAME.py:82-89](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L82-L89)：注意 `FLAME2020/` 这层子目录是拼在代码里的，放错层级会 `FileNotFoundError`。同一段还读关键点嵌入与部位掩码——这两个文件仓库已经自带。

两个资产目录的「源头」是 EHM_v2 的构造函数签名：

```python
def __init__(self, flame_assets_dir, smplx_assets_dir, ...):
    self.smplx = SMPLX(smplx_assets_dir, ...)
    self.flame = FLAME(flame_assets_dir, ...)
```

[models/modules/ehm/EHM_v2.py:14-19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L14-L19)：EHM_v2 只是转发目录参数，真正读文件的是 SMPLX 与 FLAME 两个类。

入口脚本用字面量传目录，例如 [app.py:146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L146) 的 `ehm = EHM_v2("assets/FLAME", "assets/SMPLX")` 和 [app.py:133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L133) 的 `body_renderer = BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)`；[inference_wo_detect.py:54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L54) 与 [inference_wo_detect.py:66](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L66) 完全一致。**相对路径意味着必须从仓库根目录启动脚本**，在别的目录下运行会找不到 assets。

训练侧的资产读取（模块顶层立即执行）：

```python
smplx2smpl = torch.from_numpy(joblib.load("assets/SMPLX2SMPL/body_models/smplx2smpl.pkl")['matrix']).unsqueeze(0).float().cuda()
smpl = SMPL("assets/SMPL/SMPL_NEUTRAL.pkl", gender='neutral').to(device)
J_regressor_extra = torch.tensor(pickle.load(open("assets/SMPLX2SMPL/SMPL_to_J19.pkl", 'rb'), encoding='latin1'), ...)
```

[utils/smplx2smpl_joints.py:41-45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/smplx2smpl_joints.py#L41-L45)。这里第 42 行的 `SMPL` 来自 pip 包 `smplx`（见 [utils/smplx2smpl_joints.py:20](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/smplx2smpl_joints.py#L20) 的 `from smplx import SMPL, SMPLX`，而非 PEAR 自己实现的 `models/modules/smplx/SMPLX.py`），读的正是需要 chumpy 反序列化的 `SMPL_NEUTRAL.pkl`。全仓库 import 该模块的只有 [models/pipeline/pipeline.py:37](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L37) 一处，属于训练链路（[models/pipeline/pipeline.py:96-99](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L96-L99) 还会再读一次同样的文件）。

还有一个容易被忽略的自带资产：[models/smplx/smplx_head.py:166](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L166) 加载 `assets/SMPLX/smpl_mean_params.npz` 作为参数回归的均值初始化——该文件仓库已自带，无需下载。

汇总成一张「资产对账表」（✅ = 推理必需，🔧 = 仅训练/评测需要，📦 = 仓库已自带）：

| 目标文件 | 获取方式 | 读取位置（代码写死的路径） | 状态 |
| --- | --- | --- | --- |
| `assets/SMPLX/SMPLX_NEUTRAL_2020.npz` | [smpl-x.is.tue.mpg.de](https://smpl-x.is.tue.mpg.de/download.php) | `SMPLX.py:134` | ✅ 手动下载 |
| `assets/SMPLX/flame_generic_model.pkl` | FLAME 官网模型的副本 | `SMPLX.py:138` | ✅ 手动放置（第二份） |
| `assets/FLAME/FLAME2020/generic_model.pkl` | [flame.is.tue.mpg.de](https://flame.is.tue.mpg.de/download.php) FLAME2020 | `FLAME.py:82` | ✅ 手动下载 |
| `assets/FLAME/landmark_embedding.npy` | 仓库自带 | `FLAME.py:83` | ✅ 📦 |
| `assets/FLAME/FLAME_masks/FLAME_masks.pkl` | 仓库自带 | `FLAME.py:88` | ✅ 📦 |
| `assets/SMPLX/`（obj 拓扑、UV、faces、均值参数等） | 仓库自带 | `app.py:133` 等 | ✅ 📦 |
| `assets/SMPL/SMPL_NEUTRAL.pkl` | [smpl.is.tue.mpg.de](https://smpl.is.tue.mpg.de/download.php) | `smplx2smpl_joints.py:42` | 🔧 手动下载 |
| `assets/SMPLX2SMPL/body_models/smplx2smpl.pkl` | 解压 `assets/SMPLX2SMPL.zip` | `smplx2smpl_joints.py:41` | 🔧 解压 |
| `assets/SMPLX2SMPL/SMPL_to_J19.pkl` | 解压 `assets/SMPLX2SMPL.zip` | `smplx2smpl_joints.py:44` | 🔧 解压 |
| `assets/SMPLX2SMPL/data/J_regressor_extra.npy` | 解压 `assets/SMPLX2SMPL.zip` | `smplx2smpl_joints.py:196` | 🔧 解压 |

（仓库当前 `assets/` 下已有 `FLAME/`、`SMPLX/`、`SMPLX2SMPL.zip` 及若干图片，`SMPL/` 目录需要你自行创建。）

#### 4.2.4 代码实践

**实践目标**：把三个授权模型文件放到正确位置，并用脚本验证「文件存在且可读」。

**操作步骤**：

1. 到三个官网分别注册并下载：`SMPL_NEUTRAL.pkl`（SMPL）、`SMPLX_NEUTRAL_2020.npz`（SMPL-X）、`generic_model.pkl`（FLAME2020）。嫌麻烦可用 README 提供的 Google Drive 打包链接。
2. 按下表放置（`cp` 命令在仓库根目录执行）：

   ```bash
   mkdir -p assets/SMPL assets/FLAME/FLAME2020
   cp <下载路径>/SMPL_NEUTRAL.pkl        assets/SMPL/
   cp <下载路径>/SMPLX_NEUTRAL_2020.npz  assets/SMPLX/
   cp <下载路径>/generic_model.pkl       assets/FLAME/FLAME2020/
   cp <下载路径>/generic_model.pkl       assets/SMPLX/flame_generic_model.pkl   # 第二份副本
   unzip assets/SMPLX2SMPL.zip -d assets/    # 仅训练需要
   ```

3. 用下面的**示例代码**（保存为 `check_assets.py`，放在仓库根目录）做存在性与可读性检查：

   ```python
   # 示例代码：验证 PEAR 资产文件是否就位且可解析
   import os
   import numpy as np

   required = [
       "assets/SMPLX/SMPLX_NEUTRAL_2020.npz",          # 推理必需
       "assets/SMPLX/flame_generic_model.pkl",          # 推理必需（FLAME 副本）
       "assets/FLAME/FLAME2020/generic_model.pkl",      # 推理必需
       "assets/SMPLX/smpl_mean_params.npz",             # 仓库自带，顺带检查
   ]
   train_only = [
       "assets/SMPL/SMPL_NEUTRAL.pkl",
       "assets/SMPLX2SMPL/body_models/smplx2smpl.pkl",
   ]

   for p in required + train_only:
       tag = "训练用" if p in train_only else "推理必需"
       print(f"[{'OK ' if os.path.exists(p) else '缺失'}] {tag:<4} {p}")

   # npz 是 numpy 原生格式，无需 chumpy 即可解析
   data = np.load("assets/SMPLX/SMPLX_NEUTRAL_2020.npz", allow_pickle=True)
   print("SMPLX_NEUTRAL_2020.npz 内含字段:", sorted(data.files)[:8], "...")
   ```

**需要观察的现象**：四个「推理必需」条目全部 `[OK ]`；`npz` 能打印出 `v_template`、`shapedirs` 之类的字段名。

**预期结果**：输出类似 `[OK ] 推理必需 assets/SMPLX/SMPLX_NEUTRAL_2020.npz` 的清单，且无 `FileNotFoundError`。

**待本地验证**：`npz` 字段名以你下载的模型版本实际输出为准；`.pkl` 文件的解析要等 chumpy 装好后在 4.3/5 节验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generic_model.pkl` 要在仓库里存两份（`assets/FLAME/FLAME2020/` 和 `assets/SMPLX/`）？

**参考答案**：因为有两个类各自从**自己的资产目录**读 FLAME 模板：`FLAME` 类拼 `flame_assets_dir/FLAME2020/generic_model.pkl`（FLAME.py:82），而 `SMPLX` 类拼 `smplx_assets_dir/flame_generic_model.pkl`（SMPLX.py:138）。两个目录参数由入口脚本分别传入 `"assets/FLAME"` 和 `"assets/SMPLX"`，代码里没有「跨目录共享」的逻辑，所以同一份文件必须在两个位置各放一份。

**练习 2**：你只想跑 `python app.py` 看看效果，`assets/SMPL/SMPL_NEUTRAL.pkl` 还没下载，会出问题吗？

**参考答案**：不会。`assets/SMPL` 只被 `utils/smplx2smpl_joints.py` 读取，而全仓库只有训练用的 `models/pipeline/pipeline.py:37` import 它；`app.py` 的 import 链不经过这个模块。推理必需的是 `assets/SMPLX` 与 `assets/FLAME` 下的三个文件。

**练习 3**：你在 `PEAR/scripts/` 目录下直接运行 `python ../app.py`，报找不到 assets 文件，为什么？

**参考答案**：所有资产路径都是相对路径字面量（如 `app.py:146` 的 `"assets/FLAME"`），解析时相对于**当前工作目录**而不是脚本所在目录。必须 `cd` 到仓库根目录再启动，否则 `osp.join("assets/FLAME", ...)` 拼出的路径不存在。

### 4.3 app.py 顶部的环境校验代码

#### 4.3.1 概念说明

`app.py` 是 Gradio 视频演示入口（u2-l4 精讲其界面逻辑），但它的**前 65 行与演示功能无关**，是纯粹的环境工程代码，分两段：

1. **第 7-54 行 `migrate_precompiled_packages()`**：一个「预编译包搬运」函数——如果 `pytorch3d` / `chumpy` import 不进来，就把预先放在仓库根目录的同名文件夹直接拷贝进 site-packages。这是为 HuggingFace Spaces 这类**不便现场编译**的托管环境准备的（Spaces 上编译 pytorch3d 又慢又容易失败，不如直接带一份编译好的文件夹）。注意第 57 行的调用语句**当前是被注释掉的**，本地运行时这段代码不执行，了解其意图即可。

2. **第 59-65 行环境校验**：真正生效的部分。用 `try/except` 包住三个 import（`chumpy`、`pytorch3d`、`from pytorch3d import _C`），成功则打印 `🎉 All systems go!`，失败则打印 `🚨 Validation failed: <异常>` 但**不中断程序**。这是「启动即自检」的实用模式：与其让用户在推理中途撞上一个莫名的 `ImportError`，不如一开场就把最脆弱的两件事报告出来。

为什么偏偏校验这两个包？因为它们是整个依赖树里**唯一需要现场编译/特殊安装**的环节（见 4.1），也是新环境里失败率最高的环节。`from pytorch3d import _C` 进一步探测编译扩展模块是否可用——`_C` 是 pytorch3d 的 C++/CUDA 扩展入口，只装了 Python 层而扩展没编译成功时，这一步会暴露问题。

#### 4.3.2 核心流程

`python app.py` 启动时，文件从上到下执行的早期阶段：

```text
定义 migrate_precompiled_packages()        # 仅定义，不执行
  │
  ├─ (调用语句被注释，跳过)
  ▼
try:
    import chumpy                          # 探测 1：chumpy 可用？（读 SMPL pkl 需要）
    import pytorch3d                       # 探测 2：pytorch3d Python 层可用？
    from pytorch3d import _C               # 探测 3：编译扩展可用？
    print("🎉 All systems go! PyTorch3D GPU: ...")
except Exception as e:
    print(f"🚨 Validation failed: {e}")    # 只打印，不退出
  │
  ▼
继续 import gradio / torch / 模型模块 ...
TORCH_DEVICE = cuda 可用 ? cuda : cpu      # app.py:122
```

关键设计点：

- 校验放在**一切重量级 import 之前**，保证无论环境多残缺，用户最先看到的就是这条诊断。
- 用 `except Exception` 而不是 `sys.exit(1)`，是「提示但不拦截」的取舍——某些环境下即便校验失败，后续功能也可能部分可用。
- 打印里的 `hasattr(_C, 'rasterize_meshes')` 把「编译扩展中能否找到网格光栅化入口符号」这个布尔值显示出来，作者用它作为 pytorch3d 编译扩展可用性的信号；输出 `True` 说明扩展正常加载。

#### 4.3.3 源码精读

生效的校验段：

```python
try:
    import chumpy
    import pytorch3d
    from pytorch3d import _C
    print(f"🎉 All systems go! PyTorch3D GPU: {hasattr(_C, 'rasterize_meshes')}")
except Exception as e:
    print(f"🚨 Validation failed: {e}")
```

[app.py:59-65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L59-L65)：三连 import 由浅入深——纯 Python 包 → 库的 Python 层 → 库的编译扩展层。任何一层失败都会进入 except 分支并打印原始异常信息。

被注释掉的迁移函数，其核心结构值得读一遍（面试常考的「运行时注入依赖」思路）：

```python
packages_to_check = {
    'pytorch3d': ['pytorch3d', 'pytorch3d-0.7.8.dist-info'],
    'chumpy': ['chumpy', 'chumpy-0.70.dist-info']
}
```

[app.py:10-13](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L10-L13)：声明要检查的两个包及对应的「包目录 + dist-info 元数据目录」名字。从 dist-info 名字还能读出作者预编译时用的版本：pytorch3d 0.7.8、chumpy 0.70——给你自己装包时提供了一个版本参照。

```python
try:
    importlib.import_module(pkg_name)
    print(f"✅ {pkg_name} is already available. Skipping.")
    continue
except ImportError:
    print(f"🔍 {pkg_name} not found. Preparing to migrate...")
```

[app.py:19-24](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L19-L24)：先用 `importlib.import_module` 做真实的 import 探测（比 `pip list` 更可靠，因为 pip 数据库可能与实际 import 状态不一致）；已可用则跳过。

```python
src = os.path.abspath(folder)
dst = os.path.join(target_site_packages, folder)
...
shutil.copytree(src, dst)
```

[app.py:26-41](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L26-L41)：把仓库根目录下的 `pytorch3d/`、`chumpy/` 文件夹整棵拷贝进 site-packages，相当于「手工安装」。

```python
importlib.invalidate_caches()
if target_site_packages not in sys.path:
    sys.path.insert(0, target_site_packages)
```

[app.py:43-46](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L43-L46)：拷贝后刷新 import 缓存并把 site-packages 置顶进 `sys.path`，让后续 import 能立刻找到新拷入的包。

[app.py:48-54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L48-L54) 还会把 torch 自带的 `lib` 目录追加到 `LD_LIBRARY_PATH`，解决「编译扩展链接的动态库找不到」这类问题——这是预编译二进制跨机器搬运时的常见补丁。而 [app.py:57](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L57) 的 `# migrate_precompiled_packages()` 表明该流程默认关闭。

校验段之后紧接的另一段防御性代码：

```python
try:
    import spaces
except ImportError:
    def spaces(func):
        return func
```

[app.py:86-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L86-L91)（[app.py:109-114](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L109-L114) 有一段重复实现）：`spaces` 是 HuggingFace Spaces 的专用装饰器包，本地没有时用一个「什么都不做」的同名函数兜底，让同一份代码既能跑在云端演示也能跑在本地。这种「可选依赖 + 空实现兜底」的模式在你自己的项目里也很实用。

#### 4.3.4 代码实践

**实践目标**：不启动完整的 Gradio 应用，单独复现 app.py 的环境校验逻辑，拿到「All systems go」判定。

**操作步骤**：

1. 在仓库根目录新建 `check_env.py`，内容为**示例代码**（把 app.py:59-65 的逻辑抽出来并加了一层资产检查）：

   ```python
   # 示例代码：PEAR 环境自检（复刻 app.py 顶部的校验逻辑）
   import os

   # 第一段：复刻 app.py:59-65 的三连 import 校验
   try:
       import chumpy
       import pytorch3d
       from pytorch3d import _C
       print(f"🎉 All systems go! PyTorch3D GPU: {hasattr(_C, 'rasterize_meshes')}")
   except Exception as e:
       print(f"🚨 Validation failed: {e}")

   # 第二段：chumpy 真正派上用场的场景——反序列化 SMPL 模型 pkl
   smpl_pkl = "assets/SMPL/SMPL_NEUTRAL.pkl"
   if os.path.exists(smpl_pkl):
       import pickle
       with open(smpl_pkl, "rb") as f:
           data = pickle.load(f, encoding="latin1")
       print(f"✅ {smpl_pkl} 反序列化成功，字段示例: {list(data.keys())[:5]}")
   else:
       print(f"⏭️ 跳过 pkl 检查（{smpl_pkl} 不存在，仅训练需要）")
   ```

2. 激活 4.1 节准备好的环境后运行：

   ```bash
   conda activate pear
   python check_env.py
   ```

3. 故意做一次「破坏性实验」再观察：`pip uninstall -y chumpy` 后重跑脚本，看完好环境与缺失环境输出的差别，然后 `pip install chumpy --no-build-isolation` 装回来。

**需要观察的现象**：

- 完好环境：第一行打印 `🎉 All systems go! ...`，且若 SMPL pkl 存在则第二段成功列出字段；
- 卸载 chumpy 后：第一段变为 `🚨 Validation failed: No module named 'chumpy'`，第二段（若走到）报同样的 `ModuleNotFoundError`。

**预期结果**：脚本以 `🎉 All systems go!` 开头即代表本讲全部环境目标达成。

**待本地验证**：`hasattr(_C, 'rasterize_meshes')` 在你的 pytorch3d 版本与编译方式下的取值、SMPL pkl 的字段名列表，均需以实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：校验代码为什么把三个 import 拆成「纯 Python 包 → 库的 Python 层 → 编译扩展」的顺序，而不是只写 `import pytorch3d`？

**参考答案**：因为故障分层。`import chumpy` 单独探测能区分「chumpy 没装」和「pytorch3d 没装」两种不同的缺失；`import pytorch3d` 与 `from pytorch3d import _C` 分开写，能区分「Python 层缺失」和「Python 层在但编译扩展没建成」——后者在源码编译安装 pytorch3d 时是典型失败模式。三连写法让 except 打印的异常信息直接指向问题层。

**练习 2**：`migrate_precompiled_packages()` 为什么在函数开头先用 `importlib.import_module` 探测，而不是无条件拷贝？为什么它的调用被注释掉了？

**参考答案**：先探测是为了幂等与安全——本地正常 `pip install` 的包不应被仓库里携带的预编译版本覆盖（覆盖可能带来版本错配），所以「已可用则跳过」。调用被注释，说明作者在本地/常规环境下希望走正常安装路径，只在 HuggingFace Spaces 这类特定托管环境才需要手动启用搬运逻辑。

**练习 3**：如果不希望环境校验失败时程序「带病继续运行」，你会怎么改 [app.py:59-65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L59-L65)？

**参考答案**：在 except 分支末尾加 `raise`（重新抛出捕获的异常）或 `sys.exit(1)`，把「提示」升级为「拦截」。代价是环境不完整时连部分功能也无法体验——这是一个工程取舍，作者选择了宽容模式。

## 5. 综合实践

**任务：完成 PEAR 的「开机自检」——一份环境与资产的完整验收。**

把本讲三个模块串起来，从零走到能通过 app.py 的启动校验：

1. **环境**（对应 4.1）：创建 `pear` 环境，按顺序安装 `requirements.txt`、pytorch3d、chumpy 三步，记录每步耗时与是否有告警。
2. **资产**（对应 4.2）：下载三个授权模型文件并按对账表放置，解压 `SMPLX2SMPL.zip`，运行 `check_assets.py` 确认全部 `[OK ]`。
3. **验收**（对应 4.3）：运行 `check_env.py`，以输出 `🎉 All systems go!` 作为最终通过标准；同时用 `python -c "import cv2, lightning, ultralytics, smplx; print('deps ok')"` 验证推理/训练入口的关键依赖（`lightning` 供 `train_ehms.py`，`ultralytics` 供 `inference_images.py`）。
4. **记录**：把三个命令的输出粘贴到一个 `setup_notes.md`（放在仓库外或加入 `.gitignore`，避免污染仓库），形成你自己的装机日志——下次换机器或帮同学排错时直接对照。

**通过标准**：`check_env.py` 打印 `🎉 All systems go!` 且 `check_assets.py` 无「缺失」条目。

**待本地验证**：全套流程的通过情况取决于本机 CUDA、gcc 与网络条件，本讲义编写环境未实际执行。

完成本实践后，你就拥有了跑通下一讲（u1-l4 用三个入口脚本做第一次推理）的全部前置条件。

## 6. 本讲小结

- PEAR 的依赖分两层：`requirements.txt` 覆盖常规包（numpy 钉在 1.22.4、smplx 0.1.28、lightning、ultralytics 等），而 `pytorch3d` 与 `chumpy` 必须按 README 用 `--no-build-isolation` 单独安装，因为它们的构建依赖当前环境里已装好的 torch / numpy。
- `assets/` 的目录结构由源码反推而来：`EHM_v2("assets/FLAME", "assets/SMPLX")` 把目录传给 `SMPLX` / `FLAME` 类，后者用 `osp.join` 拼出写死的文件名——所以 `SMPLX_NEUTRAL_2020.npz`、`FLAME2020/generic_model.pkl`、`flame_generic_model.pkl`（FLAME 模型的第二份副本）的位置不可改动，且脚本必须从仓库根目录启动。
- 三个授权文件（SMPL_NEUTRAL.pkl、SMPLX_NEUTRAL_2020.npz、generic_model.pkl）需手动下载；`assets/SMPL` 与 `assets/SMPLX2SMPL` 仅训练链路使用，推理可以暂缺。
- `app.py` 顶部 7-54 行的 `migrate_precompiled_packages()` 是给 HuggingFace Spaces 准备的「预编译包搬运」方案（当前调用被注释），59-65 行的三连 import 校验是真正生效的环境自检，`🎉 All systems go!` 即通过。
- 「可选依赖 + 空实现兜底」（`import spaces` 的 try/except）是让同一份代码兼顾云端演示与本地运行的实用模式。

## 7. 下一步学习建议

- 环境验收通过后，直接进入 **u1-l3（仓库目录结构与代码地图）** 和 **u1-l4（跑通第一次推理）**：用 `python inference_images.py --input_path example/images` 和 `python app.py` 产出第一批网格结果，直观感受本讲准备的资产如何被用起来。
- 想深挖本讲提到的资产在模型内部如何被消费，可提前浏览 `models/modules/smplx/SMPLX.py` 的 `__init__`（哪些张量从 npz 注册为 buffer）——这会在单元四（u4-l1）系统精讲。
- `migrate_precompiled_packages()` 涉及的 `site.getsitepackages()`、`sys.path`、`LD_LIBRARY_PATH` 是 Python 打包与动态链接的通用知识，建议结合 Python 官方文档的 `importlib`、`site` 章节补一遍，对你维护任何深度学习环境都有帮助。
- 预训练权重 `pear_model.pt` 的自动下载机制（`hf_hub_download`）将在 u2-l2 走读 `inference_wo_detect.py` 时详细展开。
