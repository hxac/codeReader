# 环境搭建与训练/推理脚本

> 本讲是单元 1 的第 2 讲。上一讲（u1-l1）我们已经知道 UniSeg3D 用一个模型统一六类 3D 分割任务、代码基于 mmengine 注册器机制组装；本讲解决一个最朴素的问题：**把环境搭起来，把训练/推理命令跑起来**，并读懂这几条命令背后的启动脚本源码。

## 1. 本讲目标

学完本讲，你应该能够：

1. 按照 README 给出的正确顺序安装全部依赖（PyTorch、OpenMMLab 系列、MinkowskiEngine、segmentator 等），并逐项用 `import` 验证安装成功。
2. 逐行读懂 `tools/dist_train.sh` 与 `tools/dist_test.sh`：随机端口探测、`PYTHONPATH` 注入、`torch.distributed.launch` 拉起多进程的完整机制。
3. 掌握 `tools/train.py` 与 `tools/test.py` 的命令行参数（`--launcher`、`--work-dir`、`--cfg-options`、`--resume`、`--amp` 等）与从配置加载到 `runner.train()` / `runner.test()` 的启动流程。
4. 独立完成单卡/多卡训练与推理评估命令的编写。

## 2. 前置知识

本讲会用到以下基础概念，先做通俗解释：

- **conda**：Python 的虚拟环境管理器。不同项目需要的 Python 版本和库版本经常互相冲突，conda 为每个项目创建一个隔离的「房间」（环境），本项目使用 `conda create -n uniseg3d python=3.10` 创建独立环境。
- **CUDA**：NVIDIA 的 GPU 并行计算平台。PyTorch 官方安装包按 CUDA 版本区分（如 `torch==1.13.0+cu117` 表示 CUDA 11.7），必须与本机 NVIDIA 驱动兼容。
- **wheel（预编译包）**：`pip install` 默认下载别人编译好的二进制包。`-f <url>`（find-links）让 pip 到指定索引页找包，OpenMMLab 与 PyG 都按「CUDA 版本 + torch 版本」提供预编译 wheel，比源码编译快得多、稳定得多。
- **源码编译安装**：MinkowskiEngine 与 segmentator 没有提供 wheel，需要在本机用 `python setup.py` 或 `cmake` 编译 CUDA 扩展，这一步最容易因 CUDA 版本不匹配而失败，也是整个安装流程中最耗时的一步。
- **分布式训练（DDP）**：多张 GPU 各跑一个进程，各自持有完整模型副本、处理不同数据，再通过反向梯度同步保持参数一致。`torch.distributed.launch` 是 PyTorch 提供的「多进程启动器」：它在每台机器上拉起 N 个进程，并注入 `RANK`、`WORLD_SIZE`、`LOCAL_RANK` 等环境变量。
- **master_port**：DDP 各进程间通过 TCP + NCCL 通信，需要一个端口做初始「会合」（rendezvous）。同一台机器上同时跑多个分布式任务时端口不能撞车——这正是 `dist_train.sh` 随机选端口的动机。
- **NCCL**：NVIDIA 的多 GPU 集合通信库，PyTorch DDP 底层默认用它同步梯度。
- **mmengine**：OpenMMLab 的训练框架引擎，提供 `Config`（配置加载）、`Registry`（注册器）、`Runner`（训练/验证/测试循环）。上一讲提过「配置里 `type='XXX'` 字符串如何变成类实例」，靠的就是它。
- **checkpoint（ckpt）**：模型权重文件。训练时从 `load_from` 指定的预训练权重热启动；测试时加载训练好的权重做推理。

## 3. 本讲源码地图

本讲涉及的文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md) | 安装步骤、数据准备、训练/推理命令的唯一权威来源 |
| [tools/dist_train.sh](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_train.sh) | 分布式训练启动脚本（选端口 → 拉起 train.py） |
| [tools/dist_test.sh](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh) | 分布式推理/评估启动脚本（多一个 checkpoint 参数） |
| [tools/train.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py) | 训练入口：加载配置 → 构建 Runner → `runner.train()` |
| [tools/test.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py) | 推理/评估入口：加载配置与权重 → 构建 Runner → `runner.test()` |

另外会顺带引用配置文件 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) 中两处预训练权重路径（第 44 行与第 313 行），用来说明「为什么训练前必须先下载两个权重文件」。

## 4. 核心概念与源码讲解

### 4.1 依赖安装清单与顺序

#### 4.1.1 概念说明

UniSeg3D 是一个「站在巨人肩膀上」的项目：深度学习框架用 PyTorch，训练框架用 OpenMMLab 系（mmengine + mmcv + mmdet + mmdetection3d），稀疏卷积同时用到 spconv（骨干网络）和 MinkowskiEngine（稀疏张量构建），超点生成依赖 segmentator，聚合操作依赖 torch-scatter，语言分支的分词依赖 ftfy/regex。

这些依赖之间存在**严格的安装顺序约束**：

1. **先装 PyTorch**：因为 mmcv、torch-scatter 的预编译 wheel 是按「CUDA 版本 + torch 版本」编译的，pip 需要先知道本环境的 torch 版本才能选对包。
2. **再装 OpenMMLab 系**：mmdetection3d 依赖 mmdet 与 mmengine，README 中三个包都固定了版本，**不要随意升级**——mmdet3d 是从 git 的特定 commit 安装的，API 与这个 commit 严格对应。
3. **再源码编译 MinkowskiEngine 与 segmentator**：两者都是 CUDA 扩展，编译时链接的是当前环境里的 torch 与 CUDA。
4. **最后装杂项依赖**（spconv、torch-scatter、numpy 等）：spconv 与 torch-scatter 也要按 torch/CUDA 版本选 wheel。

#### 4.1.2 核心流程

```text
1. conda create -n uniseg3d python=3.10      # Python 3.10
2. pip install torch==1.13.0+cu117 ...         # PyTorch 1.13 + CUDA 11.7
3. pip install mmengine==0.10.3
   pip install mmdet==3.3.0
   pip install mmdetection3d @ fe25f7a (git commit)
   pip install mmcv==2.1.0 (cu117/torch1.13.0 预编译索引)
4. conda install openblas-devel
   编译 MinkowskiEngine (python setup.py install)
5. cmake 编译 segmentator (make && make install)
6. pip install spconv-cu117 / torch-scatter==2.1.1 / numpy / timm / ftfy / regex
```

依赖与用途对照表（安装时建议逐行核对）：

| 依赖 | 版本 | 安装方式 | 在项目中的用途 |
| --- | --- | --- | --- |
| python | 3.10 | conda | 运行时 |
| torch / torchvision / torchaudio | 1.13.0 / 0.14.0 / 0.13.0 (+cu117) | pip 预编译 | 深度学习框架 |
| mmengine | 0.10.3 | pip | Config / Registry / Runner 训练引擎 |
| mmdet | 3.3.0 | pip | mmdet3d 的依赖库 |
| mmdetection3d | commit `fe25f7a...` | pip git+ | 3D 数据集基类、评估工具等 |
| mmcv | 2.1.0 | pip 预编译索引 | OpenMMLab 基础算子库 |
| MinkowskiEngine | 源码 | `python setup.py install` | 稀疏张量（`TensorField`，模型 `collate` 用） |
| segmentator | 源码 | cmake + make | 生成 `super_points` 超点数据 |
| spconv-cu117 | — | pip | `SpConvUNet` 稀疏卷积骨干 |
| torch-scatter | 2.1.1 | pip 预编译索引 | `scatter_mean` 点→超点聚合 |
| numpy | 1.26.4 | pip | 数值计算 |
| timm | 0.9.16 | pip | OpenCLIP 的依赖 |
| ftfy / regex | 6.2.0 / 2023.12.25 | pip | OpenCLIP 文本分词 |

#### 4.1.3 源码精读

README 的 Installation 一节明确给出运行环境基线：Python 3.10、PyTorch 1.13、CUDA 11.7，见 [README.md:L33-L47](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L33-L47)。这里先创建 conda 环境，再从 PyTorch 官方索引安装带 `+cu117` 后缀的版本：

```
conda create -n uniseg3d python=3.10
pip install torch==1.13.0+cu117 torchvision==0.14.0+cu117 torchaudio==0.13.0 --extra-index-url https://download.pytorch.org/whl/cu117
```

OpenMMLab 四件套的安装命令见 [README.md:L54-L60](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L54-L60)。注意两点：mmdetection3d 用 `git+...@fe25f7a51d...` 钉死 commit；mmcv 通过 `-f` 指向 cu117/torch1.13.0 的预编译索引页，避免 pip 触发源码编译：

```
pip install mmengine==0.10.3
pip install mmdet==3.3.0
pip install git+https://github.com/open-mmlab/mmdetection3d.git@fe25f7a51d36e3702f961e198894580d83c4387b
pip install mmcv==2.1.0 -f https://download.openmmlab.com/mmcv/dist/cu117/torch1.13.0/index.html
```

MinkowskiEngine 的源码编译见 [README.md:L62-L69](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L62-L69)：先 `conda install openblas-devel` 提供 BLAS 头文件，再 clone 后用 `python setup.py install` 编译，并通过 `--blas_include_dirs=${CONDA_PREFIX}/include --blas=openblas` 指定 BLAS 路径。

segmentator 的 cmake 编译见 [README.md:L71-L84](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L71-L84)。它的 cmake 参数全部用 `python -c '...'` 动态取自当前环境（torch 的 cmake 路径、Python 头文件与库目录、Python 包目录），保证扩展编译进正确的解释器。特别注意 README 的提醒：**安装后不要删除这个文件夹，因为安装只是在 Python 目录里创建了一个符号链接**。

其余依赖（spconv、torch-scatter、numpy、timm、ftfy、regex）见 [README.md:L86-L94](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L86-L94)。

最后看两个训练前置权重下载项（[README.md:L153-L157](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L153-L157)）：CLIP 预训练模型 `open_clip_pytorch_model.bin` 放入 `work_dirs/pretrained/convnext_large_d_320`，SSTNet 预训练模型 `sstnet_scannet.pth` 放入 `work_dirs/pretrained`。它们**必须**先就位，因为配置文件中两处硬编码了这两个路径：语言分支的 `checkpoint='work_dirs/pretrained/convnext_large_d_320/open_clip_pytorch_model.bin'`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L44](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L44)）和整模型的 `load_from = 'work_dirs/pretrained/sstnet_scannet.pth'`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L313)）。文件缺失时训练会在初始化语言分支或加载 checkpoint 时直接报错。

#### 4.1.4 代码实践

**实践目标**：搭建环境后，用一条命令逐项验证所有关键依赖可导入。

**操作步骤**：

```bash
conda create -n uniseg3d python=3.10
conda activate uniseg3d
# ……按 4.1.2 的顺序完成全部安装……
cd UniSeg3D  # 进入项目根目录

python - <<'EOF'
import importlib
for name, why in [
    ('torch',            '深度学习框架'),
    ('mmengine',         '训练引擎 Config/Registry/Runner'),
    ('mmdet',            'OpenMMLab 检测库（mmdet3d 依赖）'),
    ('mmdet3d',          '3D 数据集基类与评估工具'),
    ('mmcv',             'OpenMMLab 基础算子'),
    ('spconv',           'SpConvUNet 稀疏卷积骨干'),
    ('MinkowskiEngine',  '稀疏张量 TensorField'),
    ('torch_scatter',    'scatter_mean 聚合'),
    ('segmentator',      '超点生成（符号链接安装）'),
    ('uniseg3d',         '本项目包：触发全部模块注册'),
]:
    try:
        m = importlib.import_module(name)
        ver = getattr(m, '__version__', '(无 __version__)')
        print(f'[OK]   {name:18s} {ver:12s} {why}')
    except Exception as e:
        print(f'[FAIL] {name:18s} {type(e).__name__}: {e}')
EOF
```

**需要观察的现象**：每一行打印 `[OK]` 还是 `[FAIL]`。

**预期结果**：全部 `[OK]`。特别留意最后一条 `import uniseg3d`——它执行 [uniseg3d/__init__.py:L1-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19) 的一连串 import（模型、损失、数据变换、评估器等 19 个符号），任何一条依赖缺失或源码级错误都会在这里暴露。常见失败点：`MinkowskiEngine` 与 `segmentator` 编译失败（CUDA 版本不匹配）、`import uniseg3d` 报 `FileNotFoundError`（两个预训练权重未下载，见 4.1.3）。

> 待本地验证：MinkowskiEngine 与 segmentator 是否编译成功强依赖本机 CUDA 工具链，请在自己机器上实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须先装 PyTorch，再装 mmcv 和 torch-scatter，而不是随意顺序？

**答案**：mmcv 与 torch-scatter 的预编译 wheel 按「CUDA 版本 + torch 版本」区分。pip 安装时需要读取环境中已安装的 torch 版本来选择匹配的 wheel；如果先装它们，pip 找不到 torch 就可能退回源码编译或选错 CUDA 版本，导致运行时报 `undefined symbol` 之类的错误。

**练习 2**：README 特别提醒 segmentator 安装后「不要删除这个文件夹」，为什么？

**答案**：`make install` 并没有把源码拷进 Python 目录，而是只创建了一个符号链接指向编译目录。删除源文件夹会让链接悬空，`import segmentator` 随之失败。

**练习 3**：如果跳过下载 `sstnet_scannet.pth` 直接训练，会在什么环节、以什么形式报错？

**答案**：配置的 `load_from`（config 第 313 行）指向该文件。训练启动、`Runner` 尝试加载 checkpoint 时会抛出 `FileNotFoundError`（或 mmengine 包装的 `MMEngineError`），发生在任何迭代开始之前。同理，缺 CLIP 权重会在构建语言分支（config 第 44 行的 `checkpoint`）时更早报错。

### 4.2 dist_train.sh 与 dist_test.sh 逐行解读

#### 4.2.1 概念说明

这两个脚本只做三件事：**挑一个没被占用的随机端口 → 注入 PYTHONPATH → 用 `torch.distributed.launch` 启动对应入口脚本**。理解它们的收益很高：所有 OpenMMLab 项目的 dist 脚本几乎都长这样，读懂一份就读懂一片。

为什么需要随机端口？DDP 各进程启动时要通过一个 TCP 端口完成「会合」。如果你同时开两个训练任务且端口写死（比如都用 29500），第二个任务会因端口被占而启动失败。随机探测端口让多任务并存。

为什么需要 PYTHONPATH？仓库里的 `uniseg3d/`、`open_clip/` 是**源码目录**而非 pip 安装的包，必须让 Python 能「看见」项目根目录才能 `import uniseg3d`。

#### 4.2.2 核心流程

```text
dist_train.sh <config> <gpus> [额外参数...]
  ├─ 循环：随机生成端口 → nc 探测 → 空闲则跳出
  ├─ CONFIG=$1, GPUS=$2
  └─ PYTHONPATH=项目根:$PYTHONPATH
       python -m torch.distributed.launch \
         --nproc_per_node=$GPUS --master_port=$PORT \
         tools/train.py $CONFIG --launcher pytorch ${@:3}
```

端口随机数的构造值得拆开看。bash 的内建 `RANDOM` 每次产生一个 0–32767 的 15 位随机数：

\[ ((R_1 \ll 15) \,|\, R_2) \in [0,\ 2^{30}-1] \]

再取模并平移：

\[ \text{PORT} = \bigl(((R_1 \ll 15) \,|\, R_2) \bmod 49152\bigr) + 10000 \in [10000,\ 59151] \]

最低下限 10000 避开了 0–1023 的系统保留端口。

#### 4.2.3 源码精读

端口探测循环见 [tools/dist_train.sh:L1-L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_train.sh#L1-L10)：

```bash
while true
do
    PORT=$(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 ))
    status="$(nc -z 127.0.0.1 $PORT < /dev/null &>/dev/null; echo $?)"
    if [ "${status}" != "0" ]; then
        break;
    fi
done
```

`nc -z` 只探测端口是否有服务在监听而不真正传输数据：退出码 `0` 表示端口**被占用**（连接成功），非 `0` 表示**空闲**。因此循环语义是「随机选端口，直到选到一个没人的」。

参数与启动见 [tools/dist_train.sh:L12-L17](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_train.sh#L12-L17)：

```bash
CONFIG=$1
GPUS=$2

PYTHONPATH="$(dirname $0)/..":$PYTHONPATH \
python -m torch.distributed.launch --nproc_per_node=$GPUS --master_port=${PORT} \
    $(dirname "$0")/train.py $CONFIG --launcher pytorch ${@:3}
```

逐行解读：

- `CONFIG=$1`、`GPUS=$2`：第一个参数是配置文件路径，第二个是 GPU（进程）数，例如 `./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1`。
- `PYTHONPATH="$(dirname $0)/..":$PYTHONPATH`：`$0` 是 `./tools/dist_train.sh`，`dirname` 得 `./tools`，再 `/..` 得项目根目录，将其放到 `PYTHONPATH` 最前面，保证 `import uniseg3d`、`import open_clip` 命中仓库源码。
- `python -m torch.distributed.launch`：以模块方式启动 DPS 启动器；`--nproc_per_node` 指定本机进程数（通常一卡一进程），`--master_port` 指定会合端口。
- `$(dirname "$0")/train.py $CONFIG --launcher pytorch`：启动器给**每个进程**执行一遍 `train.py`，并自动传入 `--local_rank=<0..N-1>`（torch 1.13 传 `--local-rank`/`--local_rank` 两种形式之一），同时设置 `RANK`、`WORLD_SIZE` 等环境变量。
- `${@:3}`：bash 位置参数切片，把第 3 个及之后的参数原样透传给 `train.py`（例如追加 `--cfg-options` 覆盖）。

[tools/dist_test.sh](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh) 与训练版逐字符相同，仅两处差异：参数多一个 `CHECKPOINT=$2`、`GPUS=$3`（[tools/dist_test.sh:L12-L14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh#L12-L14)），透传切片相应变为 `${@:4}`（[tools/dist_test.sh:L16-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh#L16-L18)）。

对应 README 中的实际用法（[README.md:L153-L170](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L153-L170) 与 [README.md:L172-L185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L172-L185)）：

```shell
# 单卡训练（1 个进程，只可见 0 号卡）
CUDA_VISIBLE_DEVICES=0 ./tools/dist_train.sh \
    configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1

# 四卡训练（注意 config 也换成 4xb4 版本）
CUDA_VISIBLE_DEVICES=0,1,2,3 ./tools/dist_train.sh \
    configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py 4

# 推理评估
CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
    configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
    work_dirs/ckpts/model_best_2.pth 1
```

两点值得注意：`CUDA_VISIBLE_DEVICES` 在脚本外部控制进程能看到哪几张卡；多卡时换用 `4xb4` 配置（两份配置的差异在 u2-l2 精读）。另外 README L185 提到：PS、SS、IS、交互式、指代五个任务**在一次推理中同时评估**。

#### 4.2.4 代码实践

**实践目标**：不动用 GPU，单独验证端口随机逻辑与 `PYTHONPATH` 拼接，确认对脚本的理解。

**操作步骤**：

```bash
# 1) 观察随机端口分布：跑 5 次端口表达式
for i in 1 2 3 4 5; do
  bash -c 'PORT=$(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 )); echo $PORT'
done

# 2) 验证 PYTHONPATH 拼接结果（假设脚本位于 ./tools/ 下）
cd <项目根目录>
bash -c 'PYTHONPATH="$(dirname ./tools/dist_train.sh)/..":$PYTHONPATH; echo $PYTHONPATH'

# 3) 用 bash -x 单步观察端口探测循环（nc 不可用时 status 恒非 0，第一轮即跳出）
bash -x -c 'while true; do PORT=$(( ((RANDOM<<15)|RANDOM) % 49152 + 10000 )); status="$(nc -z 127.0.0.1 $PORT < /dev/null &>/dev/null; echo $?)"; if [ "${status}" != "0" ]; then break; fi; done; echo chosen=$PORT'
```

**需要观察的现象**：步骤 1 每次输出不同的 10000–59151 之间的整数；步骤 2 输出以项目根目录（`./tools/..` 形式）开头的路径；步骤 3 的 `+ PORT=...`、`+ status=...`、`+ break` 逐条单步出现。

**预期结果**：端口取值始终落在 \([10000, 59151]\) 区间内；`PYTHONPATH` 首段就是项目根目录，这正是 `import uniseg3d` 能命中的原因。

> 待本地验证：若机器未安装 `nc`（netcat），步骤 3 的探测恒视为空闲，效果等同于「随机选一个端口」，不影响正确性。

#### 4.2.5 小练习与答案

**练习 1**：`nc -z` 探测返回退出码 `0` 时脚本走哪个分支？该分支的语义是什么？

**答案**：`status == "0"` 时不满足 `if [ "${status}" != "0" ]`，循环继续再选一个端口。退出码 0 表示该端口已被某个服务监听（连接成功），不能用作新任务的 master_port。

**练习 2**：README 的多卡训练命令为什么同时改用 `configs/uniseg3d_4xb4_...py` 配置而不是继续用 `1xb4` 配置？

**答案**：`dist_train.sh` 本身对任何配置都能启动 4 个进程，但 `1xb4` 配置按单卡 batch size 4 调好了总 batch size、学习率与对应的环境设置；`4xb4` 按四卡重新调整了这些训练策略。只用脚本换卡数、不换配置，等效 batch size 会变成 4 倍而学习率等超参不匹配，训练效果偏离预期（具体差异项在 u2-l2 逐项分析）。

**练习 3**：如果想给 `train.py` 追加一个 `--work-dir /tmp/test_run`，该把参数放在命令的什么位置？

**答案**：放在第 2 个参数（GPUS）之后，它会经 `${@:3}` 透传给 `train.py`：`./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 --work-dir /tmp/test_run`。

### 4.3 train.py：参数与启动流程

#### 4.3.1 概念说明

[tools/train.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py) 第一行注释说明它是 mmdetection3d 官方 `tools/train.py` 的原样拷贝——这正是 OpenMMLab 生态的惯例：**入口脚本归框架管，项目特有的东西全部放进配置和 `uniseg3d/` 包**。所以读懂这一份，就读懂了所有 mmdet3d 项目的训练入口。

它的工作模式是「配置驱动」：命令行只负责指出配置文件和少量覆盖项，其余一切（模型、数据、优化器、调度器、钩子）由 `Runner.from_cfg(cfg)` 按配置组装，最后 `runner.train()` 进入训练循环（Runner 生命周期的细节在 u2-l1 展开）。

#### 4.3.2 核心流程

```text
main()
 1. parse_args()               # 解析命令行
 2. Config.fromfile(config)    # 加载 python 配置文件为 Config 对象
 3. cfg.launcher = launcher    # 记录启动方式（none/pytorch/slurm/mpi）
 4. cfg.merge_from_dict(命令行覆盖)
 5. 确定 work_dir              # 优先级：CLI > 配置段 > ./work_dirs/<config名>
 6. --amp / --auto-scale-lr / --resume 的三段可选处理
 7. Runner.from_cfg(cfg)       # 按配置组装 Runner（模型、数据、优化器、钩子）
 8. runner.train()             # 开始训练
```

#### 4.3.3 源码精读

脚本开头先把项目根目录追加进 `sys.path`，再导入 mmengine 组件（[tools/train.py:L1-L14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L1-L14)）：

```python
sys.path.append(os.path.dirname(__file__) + '/..')
from mmengine.config import Config, DictAction
from mmengine.registry import RUNNERS
from mmengine.runner import Runner
```

这是 4.2 中 `PYTHONPATH` 之外的**双保险**：即使不经过 dist 脚本、直接 `python tools/train.py` 运行，`import uniseg3d` 也能找到仓库源码。

参数定义集中在 `parse_args()`（[tools/train.py:L17-L62](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L17-L62)），重点参数：

| 参数 | 含义 |
| --- | --- |
| `config`（位置参数） | 训练配置文件路径 |
| `--work-dir` | 日志与 checkpoint 保存目录 |
| `--amp` | 开启自动混合精度训练 |
| `--auto-scale-lr` | 按批次大小自动缩放学习率（需配置含 `auto_scale_lr` 段） |
| `--resume [path]` | 断点续训；不带路径则自动找最新 checkpoint |
| `--cfg-options k=v ...` | 命令行覆盖配置中的任意键值（`DictAction` 解析） |
| `--launcher` | 启动方式：`none` / `pytorch` / `slurm` / `mpi` |

其中 `--local_rank` 参数（[tools/train.py:L55-L61](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L55-L61)）不是给用户用的，而是 `torch.distributed.launch` 自动传入的「本进程绑定第几张卡」编号；代码同时兼容 `--local_rank`（旧写法）与 `--local-rank`（torch ≥ 2.0 写法），并在环境变量 `LOCAL_RANK` 缺失时补写（[tools/train.py:L60-L61](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L60-L61)），供 mmengine 读取。

`main()` 的配置加工流程见 [tools/train.py:L65-L86](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L65-L86)：

```python
cfg = Config.fromfile(args.config)
...
cfg.launcher = args.launcher
if args.cfg_options is not None:
    cfg.merge_from_dict(args.cfg_options)
# work_dir 优先级: CLI > segment in file > filename
if args.work_dir is not None:
    cfg.work_dir = args.work_dir
elif cfg.get('work_dir', None) is None:
    cfg.work_dir = osp.join('./work_dirs',
                            osp.splitext(osp.basename(args.config))[0])
```

`Config.fromfile` 把 python 配置文件执行成一个可嵌套访问的 `Config` 对象；`merge_from_dict` 实现「命令行 > 配置文件」的覆盖语义；`work_dir` 未指定时默认落到 `./work_dirs/<配置文件名>/`。

三个可选功能段：`--amp` 把优化器包装类型换成 `AmpOptimWrapper` 并启用动态 loss scale（[tools/train.py:L88-L101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L88-L101)）；`--auto-scale-lr` 要求配置里存在 `auto_scale_lr.enable` 与 `auto_scale_lr.base_batch_size` 才能开启（[tools/train.py:L103-L113](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L103-L113)）；`--resume` 有两种形态——`auto` 时置 `cfg.resume=True` 并清空 `load_from`（自动找 work_dir 最新 checkpoint），指定路径时把该路径写进 `cfg.load_from`（[tools/train.py:L115-L121](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L115-L121)）。

收尾三行是整个入口的灵魂（[tools/train.py:L123-L133](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L123-L133)）：

```python
if 'runner_type' not in cfg:
    runner = Runner.from_cfg(cfg)
else:
    runner = RUNNERS.build(cfg)
runner.train()
```

默认走 `Runner.from_cfg(cfg)`：按配置一次性构建数据集、dataloader、模型（含注册器实例化的所有 `type='XXX'` 组件）、优化器与钩子，随后 `runner.train()` 进入训练循环。数据文件缺失时，报错就发生在 `from_cfg` 构建数据集这一步。

#### 4.3.4 代码实践

**实践目标**：不真正开训，先确认入口脚本的参数与配置加载路径符合预期。

**操作步骤**：

```bash
cd <项目根目录>

# 1) 查看完整参数说明（不触碰 GPU/数据）
python tools/train.py --help

# 2) 用 --cfg-options 做一次「干跑」：把 batch size 覆盖为 2、work-dir 指到临时目录
CUDA_VISIBLE_DEVICES=0 ./tools/dist_train.sh \
    configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 \
    --cfg-options train_dataloader.batch_size=2 \
    --work-dir /tmp/uniseg3d_dryrun

# 3) 观察启动日志的前若干行（work_dir、配置 dump、组件构建），数据未准备时记录报错位置
```

**需要观察的现象**：

1. 步骤 1 输出的参数列表应包含 4.3.3 表格中的所有参数。
2. 步骤 2 启动后，日志应出现 mmengine 的版本与环境信息、`work_dir` 指向 `/tmp/uniseg3d_dryrun`，并且配置被 dump 到该目录；随后开始构建模型（打印模型结构与参数量）。
3. 若尚未执行 u1-l4 的数据准备，预期在构建训练数据集阶段抛出找不到 `data/scannet/uniseg3d_infos_train.pkl`（或 bin 文件）的异常——记录完整 traceback，它会告诉你数据集对象在哪一行去读哪个文件。

**预期结果**：参数解析、配置加载、`--cfg-options` 覆盖全部生效（日志中的 batch_size 显示为 2）；在数据缺失的环境下，进程停止于数据集构建而非配置解析。这一步「失败得恰到好处」就证明环境与入口脚本都是通的。

> 待本地验证：具体报错信息与日志格式取决于 mmengine 版本与数据准备进度，请在本地实际运行并记录。

#### 4.3.5 小练习与答案

**练习 1**：`--launcher` 的四个取值分别用于什么场景？`dist_train.sh` 场景下应选哪个？

**答案**：`none` 单进程直接跑；`pytorch` 由 `torch.distributed.launch`（或 torchrun）多进程启动；`slurm` 由 SLURM 集群调度器 `srun` 启动；`mpi` 用 MPI 启动器。`dist_train.sh` 用 `torch.distributed.launch`，所以传 `--launcher pytorch`。

**练习 2**：`--resume` 不带参数与带一个 checkpoint 路径，行为有何不同？

**答案**：不带参数时取默认值 `auto`，代码置 `cfg.resume = True` 且 `cfg.load_from = None`，mmengine 会自动从 work_dir 中最新的 checkpoint 恢复（含优化器状态与迭代数）；带路径时同样置 `cfg.resume = True`，但把 `cfg.load_from` 设为该路径，从指定 checkpoint 恢复。

**练习 3**：为什么说「train.py 第一行注释（原样拷贝自 mmdetection3d）」体现了这个项目的工程风格？

**答案**：项目把所有定制内容（模型、损失、数据变换、评估器）都放进 `uniseg3d/` 包并通过注册器接入框架，入口与框架保持一致。好处是升级框架或对照官方文档时心智负担小；也让读者明白：想理解本项目「特有」的逻辑，应去读 `uniseg3d/` 包与配置文件，而不是入口脚本。

### 4.4 test.py：参数与启动流程

#### 4.4.1 概念说明

[tools/test.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py) 同样是 mmdetection3d 官方入口的原样拷贝，结构与 `train.py` 高度同构：加载配置 → 应用命令行覆盖 → 构建 Runner → `runner.test()`。最大的差异是：它多一个 `checkpoint` 位置参数，并且把该参数写入 `cfg.load_from`，让 Runner 在构建模型后加载训练好的权重。

#### 4.4.2 核心流程

```text
main()
 1. parse_args()               # config + checkpoint 两个位置参数
 2. Config.fromfile(config)
 3. cfg.launcher / cfg-options 覆盖 / work_dir（同 train.py）
 4. cfg.load_from = checkpoint # 关键差异：注入待评估权重
 5. 可选：--show/--show-dir 触发可视化钩子；--tta 启用测试时增强
 6. Runner.from_cfg(cfg) → runner.test()
```

#### 4.4.3 源码精读

参数定义见 [tools/test.py:L15-L68](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L15-L68)：与 `train.py` 相比，多了第二个位置参数 `checkpoint`（[tools/test.py:L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L19)），少了 `--amp` / `--resume` / `--auto-scale-lr` 这三个训练专用项，多了可视化（`--show`、`--show-dir`、`--score-thr`、`--task`、`--wait-time`）与 `--tta`（测试时增强）两组推理专用项。

与训练的关键差异在 [tools/test.py:L100-L126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L100-L126) 的这一行：

```python
cfg.load_from = args.checkpoint
```

命令行传入的 checkpoint 被直接写进配置，`Runner` 据此加载权重（例如 `work_dirs/ckpts/model_best_2.pth`）。也就是说，配置文件里的 `load_from` 在测试场景会被命令行参数覆盖。

`--tta` 分支（[tools/test.py:L128-L134](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L128-L134)）要求配置里提供 `tta_model` 与 `tta_pipeline` 两段，把测试管线换成多版本增广、模型换成封装后的 TTA 模型；UniSeg3D 的两份配置并未提供这两段，因此本项目常规评估不使用 `--tta`。

结尾同样是构建 Runner 并进入测试循环（[tools/test.py:L136-L146](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L136-L146)）：

```python
if 'runner_type' not in cfg:
    runner = Runner.from_cfg(cfg)
else:
    runner = RUNNERS.build(cfg)
runner.test()
```

`runner.test()` 会遍历测试集做前向推理，把预测交给配置里的评估器 `PromptSupportedUnifiedSegMetric`（注册名见 [uniseg3d/__init__.py:L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L12)）计算六任务指标——评估细节在 u6-l2 展开，此处只需知道「一次 `runner.test()` 同时评估 PS/SS/IS/交互式/指代五任务」（README [L185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L185) 的 Note）。

#### 4.4.4 代码实践

**实践目标**：确认测试入口的参数与配置注入行为，并与 `train.py` 做对比。

**操作步骤**：

```bash
cd <项目根目录>

# 1) 对比两个入口的参数差异
python tools/train.py --help > /tmp/train_help.txt
python tools/test.py  --help > /tmp/test_help.txt
diff /tmp/train_help.txt /tmp/test_help.txt

# 2) 下载官方 checkpoint（README Inference 节给的链接）放入 work_dirs/ckpts
mkdir -p work_dirs/ckpts
# wget -O work_dirs/ckpts/model_best_2.pth \
#   https://github.com/dk-liang/UniSeg3D/releases/download/v1.0/model_best_2.pth

# 3) 不带 checkpoint 直接运行，观察报错（参数校验发生在哪一步）
python tools/test.py configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py
```

**需要观察的现象**：

1. 步骤 1 的 diff 应显示：`test.py` 独有 `checkpoint`、`--show`、`--show-dir`、`--score-thr`、`--task`、`--wait-time`、`--tta`；`train.py` 独有 `--amp`、`--auto-scale-lr`、`--resume`。
2. 步骤 3 缺少位置参数时，argparse 应立刻打印 usage 错误（`the following arguments are required: checkpoint` 之类）并退出，说明 checkpoint 是必填位置参数。

**预期结果**：两个入口的参数集差异与 4.4.3 的描述一致；缺参报错来自 argparse 的参数解析层，而非配置加载层。

> 待本地验证：帮助文本措辞随 argparse 版本略有差异，以本地输出为准。完整评估（步骤 2 + dist_test.sh）依赖 u1-l4 的数据准备与本讲环境搭建全部完成，属于第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：`test.py` 中 `cfg.load_from = args.checkpoint` 与 `train.py` 中配置文件自带的 `load_from` 有何语义区别？

**答案**：训练中的 `load_from`（指向 SSTNet 预训练权重）是**热启动**——只取权重、从头计迭代；测试中命令行注入的 checkpoint 是**待评估对象**——Runner 加载它后在测试集上前向并计算指标。前者是初始化手段，后者是评估对象。

**练习 2**：为什么 `test.py` 没有 `--resume` 参数？

**答案**：`--resume` 服务于断点续训（恢复优化器状态与迭代数），测试没有优化器和迭代进度的概念，只需加载一次完整权重，`checkpoint` 位置参数足矣。

**练习 3**：如果想在评估时临时降低推理阈值（例如 config 中 `test_cfg` 的某阈值），用哪个参数实现？

**答案**：用 `--cfg-options`，例如 `./tools/dist_test.sh <config> <ckpt> 1 --cfg-options model.test_cfg.sp_score_thr=0.3`（键名以实际配置为准），它会经 `${@:4}` 透传给 `test.py` 并 `merge_from_dict` 进配置。`test_cfg` 各阈值的含义与代码落点在 u6-l1 精读。

## 5. 综合实践

**任务：从零跑通「环境 → 验证 → 训练启动 → 推理评估」全链路，并在每一站记录证据。**

1. **环境**：按 4.1 安装全部依赖，运行 4.1.4 的验证脚本，保存输出（要求 10 行全 `[OK]`）。
2. **权重就位**：按 README 下载 3 个文件——`open_clip_pytorch_model.bin` 到 `work_dirs/pretrained/convnext_large_d_320/`、`sstnet_scannet.pth` 到 `work_dirs/pretrained/`、`model_best_2.pth` 到 `work_dirs/ckpts/`，用 `ls -l` 记录路径与大小。
3. **训练启动演练**：执行 4.3.4 的干跑命令；若已完成 u1-l4 的数据准备，让训练跑起来并观察前若干个迭代的日志（loss 键名、学习率、日志落盘位置 `/tmp/uniseg3d_dryrun`）；若数据未就绪，记录报错的 traceback 并回答：报错发生在 `Config.fromfile`、`Runner.from_cfg` 还是 `runner.train()` 阶段？对应 `train.py` 的哪一行？
4. **推理评估**：数据就位后运行：

   ```shell
   CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
       configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
       work_dirs/ckpts/model_best_2.pth 1
   ```

   观察日志末尾是否一次性打印全景（PQ）、语义（mIoU）、实例（mAP）、交互式（AP）、指代（mIoU）等多组指标，并与 README Models 表格（[README.md:L200-L254](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L200-L254)）中 `UniSeg3D*` 一行的数值（71.3 / 76.9 / 59.3 / 54.5 / 29.6）做对比，记录差距。

5. **复盘**：把以上四步的证据（命令、关键日志行、报错位置）整理成一页「环境验收报告」。以后换机器复现环境时，这份报告就是 checklist。

> 待本地验证：第 3、4 步的现象取决于数据与 GPU 是否就绪；本讲义编写时未实际执行训练/评估，所有「预期现象」需在本地验证后修正为你机器上的真实输出。

## 6. 本讲小结

- 依赖安装有严格顺序：**PyTorch → OpenMMLab 四件套（版本钉死）→ 源码编译 MinkowskiEngine 与 segmentator → spconv/torch-scatter 等**，核心原因 wheel 按 torch+CUDA 版本匹配。
- 训练前必须下载两个预训练权重（CLIP 与 SSTNet），因为配置第 44 行与第 313 行硬编码了它们的路径。
- `dist_train.sh` / `dist_test.sh` 的本质是三件事：**随机探测空闲端口（`nc -z`，端口落在 10000–59151）→ 把项目根目录注入 `PYTHONPATH` → 用 `torch.distributed.launch` 按每卡一进程拉起入口脚本**，多余参数经 `${@:3}` / `${@:4}` 透传。
- `train.py` 与 `test.py` 都是 mmdet3d 官方入口的原样拷贝：命令行只负责定位配置与少量覆盖（`--cfg-options` 优先级最高），一切由 `Runner.from_cfg(cfg)` 组装，最后 `runner.train()` / `runner.test()` 驱动。
- 两者关键差异：`test.py` 多一个必填的 `checkpoint` 位置参数并写入 `cfg.load_from`；少了训练专用参数（`--amp`/`--resume` 等），多了可视化与 `--tta`。
- 一次 `dist_test.sh` 评估同时产出 PS/SS/IS/交互式/指代五个任务的指标。

## 7. 下一步学习建议

- 下一讲 **u1-l3（仓库目录结构与代码地图）**：环境通了之后，系统逛一遍 `uniseg3d/`、`open_clip/`、`configs/`、`tools/`、`data/` 各目录，统计 `register_module` 注册清单，建立源码导航能力。
- 如果你想先搞清「`type='UniSeg3D'` 字符串是怎么变成类的」，可以提前阅读 **u2-l1（注册器机制与 Runner 生命周期）**，本讲 4.3 中 `Runner.from_cfg(cfg)` 一行正是那讲的主角。
- 训练真正跑起来需要数据：进入 **u1-l4（数据准备）**，学习用 `batch_load_scannet_data.py` 与 `create_data.py` 生成 points/instance_mask/semantic_mask/super_points 四类 bin 文件与 infos pkl，并下载两个官方 pkl 与类别嵌入文件。
- 建议把本讲综合实践的「环境验收报告」保留下来：后续讲义的代码实践都默认在能 `import uniseg3d` 成功的环境中进行。
