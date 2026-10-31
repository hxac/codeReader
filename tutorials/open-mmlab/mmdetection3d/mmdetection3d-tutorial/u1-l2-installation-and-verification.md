# 第 2 讲：环境安装与版本验证

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MMDetection3D 的完整依赖链条（PyTorch → mmengine / mmcv / mmdet → mmdet3d），并理解为什么必须按这个顺序安装。
2. 读懂 `setup.py` 和 `requirements/` 目录，明白 `pip install -e .` 执行时到底发生了什么、装了哪些包、没装哪些包。
3. 逐行解释 `mmdet3d/__init__.py` 中对 mmengine、mmcv、mmdet 的三条版本断言，理解「左闭右开区间」的含义以及断言失败的两种典型报错（`ModuleNotFoundError` 与 `AssertionError`）。
4. 用 `pip list`、`import mmdet3d`、`mmdet3d.__version__` 三种手段验证安装是否成功。

上一讲（u1-l1）我们提到：MMDetection3D 立于 MMEngine、MMCV、MMDetection 三层之上，「包入口以左闭右开区间断言版本」。本讲就把这句话拆开，看清它的每一行实现。

## 2. 前置知识

- **pip 与 conda**：pip 是 Python 包管理器，conda 既能管理包也能管理 Python 解释器本身。深度学习项目通常用 conda 先造一个独立的 Python 环境，再用 pip 装包，避免污染系统 Python。
- **可编辑安装（editable install）**：`pip install -e .` 把当前目录的源码以「软链接」方式装进环境。之后你修改仓库里的任何 `.py` 文件，无需重新安装即可生效——这是阅读源码、做二次开发的标准姿势。
- **语义化版本（semver）**：版本号形如 `主版本.次版本.修订号`，例如 `2.1.0`；`rc4` 表示第 4 个候选发布版（release candidate），正式版发布前的一个预发布标签。
- **版本区间**：`>=2.0.0rc4,<2.2.0` 表示「不小于 2.0.0rc4 且小于 2.2.0」。数学上这是一个**左闭右开区间** \([2.0.0\text{rc4},\ 2.2.0)\)：下界包含、上界不包含。
- **MIM**：OpenMMLab 自家的包管理器（`pip install openmim` 后得到 `mim` 命令）。它能根据你环境里的 PyTorch/CUDA 版本，自动挑选兼容的 mmcv 预编译 wheel，比裸 pip 省心。
- **Python 的 `assert` 语句**：`assert 条件, 提示信息` 在条件为假时抛出 `AssertionError` 并打印提示信息。它常被用来做「防御式检查」——在错误刚要发生时就拦下来，并告诉用户怎么修。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [docs/en/get_started.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md) | 官方安装指南 | 环境前提、推荐安装命令、验证方法 |
| [setup.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py) | pip 安装入口 | 依赖声明、extras、`.mim` 目录软链 |
| [requirements.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements.txt) | 全量依赖汇总（4 个子文件） | `-r` 递归引用语法 |
| [requirements/runtime.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/runtime.txt) | 最小运行时依赖 | 默认安装到底装了什么 |
| [requirements/mminstall.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/mminstall.txt) | MIM 元数据中的版本约束 | 与运行时断言的细微差异 |
| [mmdet3d/\_\_init\_\_.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py) | 包入口 | 三条版本断言（本讲核心） |
| [mmdet3d/version.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py) | 版本号定义 | `__version__ = '1.4.0'` |
| [docker/Dockerfile](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docker/Dockerfile) | 官方容器环境 | 一份「可执行的安装清单」 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**依赖链条全景** → **setup.py 与 requirements** → **版本断言机制** → **安装验证四步法**。

### 4.1 依赖链条全景：从 PyTorch 到 mmdet3d

#### 4.1.1 概念说明

MMDetection3D 不是一个自包含的模型库。它的代码大量调用三个上游库的能力：

- **mmengine**：训练引擎。Runner、配置系统、注册器、Hook 机制都由它提供——没有它，`tools/train.py` 一行都跑不起来。
- **mmcv**：视觉基础库。图像后处理、视频工具，以及最关键的**预编译 CUDA 算子**（3D 框的 NMS、rotated IoU 等底层运算都打包在 mmcv 里，mmdet3d 自己不编译 CUDA 代码，见 4.2 节）。
- **mmdet**：2D 检测库。单目 3D 检测（如 FCOS3D）和多模态检测（如 MVXNet）直接复用它的 2D backbone、FPN、RoIHead 等组件。

这三个库又都依赖 PyTorch。所以完整的依赖链条是四层：

```text
PyTorch (+ CUDA)
   └── mmengine  —— 训练引擎
   └── mmcv      —— 算子与视觉基础库（依赖 PyTorch 的 CUDA 版本！）
   │     └── mmdet —— 2D 检测组件（依赖 mmcv、mmengine）
   └──────────────── mmdet3d —— 本项目
```

**为什么安装顺序重要？** 因为 mmcv 的预编译版本必须与本机 PyTorch 版本和 CUDA 版本严格匹配。如果先装 mmcv 再换 PyTorch，mmcv 里的 CUDA 算子就会加载失败。这就是官方推荐「先 PyTorch，再 mmengine/mmcv/mmdet，最后 mmdet3d」的原因。

#### 4.1.2 核心流程

官方推荐的安装流程（最佳实践路径）：

```text
Step 0  安装 Miniconda
Step 1  conda create --name openmmlab python=3.8   # 独立环境
Step 2  安装 PyTorch（GPU 版或 CPU 版）
Step 3  pip install -U openmim                     # 装 MIM
Step 4  mim install mmengine                       # 引擎
        mim install 'mmcv>=2.0.0rc4'               # 算子库（MIM 自动匹配 CUDA）
        mim install 'mmdet>=3.0.0'                 # 2D 组件
Step 5  git clone mmdetection3d && pip install -v -e .   # 源码安装
```

注意一个容易忽略的细节：**PyTorch 不在 mmdet3d 的依赖清单里**（见 4.2.3 节），必须由你自己先装好。这不是遗漏，而是刻意设计——PyTorch 必须匹配本机 GPU 的 CUDA 版本，写死在依赖里反而会帮倒忙。

#### 4.1.3 源码精读

**① 环境前提。** 官方文档明确写出操作系统与版本要求：

> MMDetection3D works on Linux, Windows (experimental support) and macOS. It requires Python 3.7+, CUDA 10.0+, and PyTorch 1.8+.

见 [docs/en/get_started.md:7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L7)，这句话划定了本讲所有操作的下限：Python 3.7 以上、CUDA 10.0 以上、PyTorch 1.8 以上。

**② conda 环境创建。** [docs/en/get_started.md:17-20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L17-L20) 给出创建并激活环境的命令：

```shell
conda create --name openmmlab python=3.8 -y
conda activate openmmlab
```

**③ 用 MIM 安装三件套。** [docs/en/get_started.md:44-49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L44-L49) 是安装的核心命令：

```shell
pip install -U openmim
mim install mmengine
mim install 'mmcv>=2.0.0rc4'
mim install 'mmdet>=3.0.0'
```

这里 mmcv 的下界写成 `2.0.0rc4` 不是随手写的——mmcv 2.0.0rc4 是一次重要的重命名节点（旧的 `mmcv-full` 更名为 `mmcv`），之前的 API 与 mmdet3d 1.x 不兼容。文档里还有一条备注（[docs/en/get_started.md:51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L51)）：如果不需要 CUDA 算子，可以装轻量版 `mmcv-lite`。

**④ MIM 依赖元数据。** MIM 安装时读取的版本约束写在 [requirements/mminstall.txt:1-3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/mminstall.txt#L1-L3)：

```text
mmcv>=2.0.0rc4,<2.2.0
mmdet>=3.0.0,<3.3.0
mmengine>=0.7.1,<1.0.0
```

请把这三行和 4.3 节的运行时断言对比着看——两者的区间**并不完全一致**（mmengine 下界 `0.7.1` 比断言的 `0.8.0` 更松）。也就是说，即便 MIM 替你装好了依赖，最终把关的仍然是 `mmdet3d/__init__.py` 里的断言。这个差异我们留到 4.3.1 详细分析。

**⑤ 一份「可执行的安装清单」。** 官方 Dockerfile 把整个安装流程固化成了脚本，见 [docker/Dockerfile:1-5](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docker/Dockerfile#L1-L5)（基础镜像锁定 PyTorch 1.9.0 + CUDA 11.1 + cuDNN 8）和 [docker/Dockerfile:30-38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docker/Dockerfile#L30-L38)（先用 `mim install` 装三件套，再 `git clone -b dev-1.x` 并 `pip install -e .`）。Dockerfile 就是官方安装文档的「机器可读版」，顺序和文档完全一致。

#### 4.1.4 代码实践

**实践目标**：摸清你当前 Python 环境里已经装了哪些依赖、版本是多少，判断离「能装 mmdet3d」还差什么。

**操作步骤**：

```shell
# 1. 激活你的环境（若已创建）
conda activate openmmlab

# 2. 列出与本讲相关的包及版本
pip list | grep -iE "torch|mmcv|mmengine|mmdet|numpy|numba"
```

**需要观察的现象**：输出是一个「包名 + 版本号」的列表。逐行核对：有没有 `torch`？有没有 `mmcv` / `mmengine` / `mmdet`？

**预期结果**：

- 如果是全新环境，大概率只有 `pip` 自带的几个包，什么都缺——这正是我们从 Step 0 开始的原因。
- 如果之前装过 OpenMMLab 项目，可能已有部分依赖。此时重点看 **mmcv 是否落在 \([2.0.0\text{rc4},\ 2.2.0)\)、mmdet 是否落在 \([3.0.0\text{rc5},\ 3.4.0)\)、mmengine 是否落在 \([0.8.0,\ 1.0.0)\)**——这三个区间就是 4.3 节断言的判据。

本实践只读不写，不会改动环境。（具体输出取决于你的机器，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么官方推荐用 MIM 装 mmcv，而不是直接 `pip install mmcv`？

**参考答案**：mmcv 包含预编译的 C++/CUDA 扩展，其 wheel 必须与本机 PyTorch 版本和 CUDA 版本严格对应。裸 pip 只会从 PyPI 拿一个默认构建，很容易装到不匹配的版本；MIM 会探测环境里的 torch/CUDA 组合，自动选择正确的预编译源（[docs/en/get_started.md:228-232](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L228-L232) 也说明了不用 MIM 时需要手动指定 find-url 的原因）。

**练习 2**：mmdet3d 依赖 mmdet（2D 检测库）的主要原因是什么？

**参考答案**：单目 3D 检测（FCOS3D、SMOKE 等）本质上是「从 2D 图像特征回归 3D 框」，多模态检测（MVXNet）也要用 2D backbone / FPN / RoIHead 提取图像特征。这些组件 mmdet 已经实现且久经考验，直接复用避免重复造轮子。这一点在 u1-l1 的生态关系图里已经出现过，后续 u6-l5、u6-l6 会走进源码验证。

---

### 4.2 setup.py 与 requirements：`pip install -e .` 背后发生了什么

#### 4.2.1 概念说明

`setup.py` 是 Python 的打包描述文件：当你在仓库根目录执行 `pip install -e .` 时，pip 会读取它，知道「这个包叫什么、版本是多少、依赖哪些包、要安装哪些文件」。它描述的是**安装时的元数据**；而 `mmdet3d/__init__.py` 里的断言是**运行时的检查**——两者一个管「装」，一个管「用」，共同构成双保险。

requirements 目录则把依赖按用途分组，避免「装个训练库还要顺带装文档工具」：

| 文件 | 内容 | 对应 pip extras |
| --- | --- | --- |
| `requirements/runtime.txt` | 最小运行时依赖（10 个包） | 默认安装 |
| `requirements/tests.txt` | pytest 等测试工具 | `.[tests]` |
| `requirements/optional.txt` | Waymo 数据集 SDK 等可选依赖 | `.[optional]` |
| `requirements/build.txt` | 空（当前为空文件） | `.[build]` |
| `requirements/mminstall.txt` | MIM 元数据 | `.[mim]` |

#### 4.2.2 核心流程

`pip install -e .` 执行时的关键步骤：

```text
1. pip 读取 setup.py
2. setup.py 顶层 import torch          ← 所以必须先装好 PyTorch！
3. get_version() 读 mmdet3d/version.py 拿到版本号 '1.4.0'
4. add_mim_extention() 在 editable 模式下
   把 tools/ configs/ demo/ 等软链到 mmdet3d/.mim/
5. setup() 声明 install_requires = runtime.txt 的 10 个包
   （注意：mmcv/mmengine/mmdet/torch 都不在其中）
6. pip 解析并安装这些依赖，再把 mmdet3d 以可编辑方式挂进环境
```

#### 4.2.3 源码精读

**① 依赖声明：默认只装 runtime 的 10 个包。** [setup.py:217-224](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L217-L224) 写明了默认依赖与四组扩展依赖：

```python
install_requires=parse_requirements('requirements/runtime.txt'),
extras_require={
    'all': parse_requirements('requirements.txt'),
    'tests': parse_requirements('requirements/tests.txt'),
    'build': parse_requirements('requirements/build.txt'),
    'optional': parse_requirements('requirements/optional.txt'),
    'mim': parse_requirements('requirements/mminstall.txt'),
},
```

这段代码解释了一个常见困惑：**`pip install -e .` 之后 mmcv 并不会被自动装上**——因为 `install_requires` 指向的是 [requirements/runtime.txt:1-10](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/runtime.txt#L1-L10)：

```text
lyft_dataset_sdk
networkx>=2.5
numba # you should install numba==0.53.0 if your environment is cuda-9.0
numpy
nuscenes-devkit
open3d
plyfile
scikit-image
# by default we also use tensorboard to log results
tensorboard
trimesh
```

注意里面**没有 torch、没有 mmcv/mmengine/mmdet**。这些「大件」全部留给用户按官方顺序手动安装。想要可选依赖时，用 `pip install -v -e .[optional]` 或 `[all]` 语法（[docs/en/get_started.md:78](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L78) 说明了这四种 extras key）。

**② requirements.txt 的聚合语法。** [requirements.txt:1-4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements.txt#L1-L4) 用 `-r` 指令把四个子文件串成一份全量清单，`pip install -r requirements.txt` 或 `.[all]` 会安装全部：

```text
-r requirements/build.txt
-r requirements/optional.txt
-r requirements/runtime.txt
-r requirements/tests.txt
```

顺带一个诚实的观察：[requirements/build.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/build.txt) 在当前 HEAD 是一个**空文件**（extras 里的 `build` 键实际装不到任何东西）。读源码时遇到这类「历史遗留」不必惊讶，也不必脑补内容。

**③ 版本号从哪里来：exec 技巧。** [setup.py:21-27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L21-L27)：

```python
version_file = 'mmdet3d/version.py'

def get_version():
    with open(version_file, 'r') as f:
        exec(compile(f.read(), version_file, 'exec'))
    return locals()['__version__']
```

它把 [mmdet3d/version.py:3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py#L3) 的内容读出来用 `exec` 执行，取出 `__version__`（当前值为 `'1.4.0'`）。为什么要绕这一圈，而不是直接 `from mmdet3d.version import __version__`？因为 `import mmdet3d.version` 会先执行包入口 `mmdet3d/__init__.py`，而包入口一开头就要 `import mmcv`（见 4.3 节）——打包机器上未必装着这些依赖。`exec` 方式让 setup.py 只读文件、不触发导入链。这是打包脚本里很常见的惯用法。

**④ `.mim` 目录软链：为 `mim download` 铺路。** [setup.py:138-189](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L138-L189) 的 `add_mim_extention()`（函数名拼写少了个 `s`，也是一处可爱的历史细节）在 editable 安装时执行软链：

```python
filenames = [
    'tools', 'configs', 'demo', 'model-index.yml', 'dataset-index.yml'
]
...
if mode == 'symlink':
    os.symlink(src_relpath, tar_path)
```

见 [setup.py:161-163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L161-L163) 与 [setup.py:178-180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L178-L180)。效果是把这些目录链接到 `mmdet3d/.mim/` 下，这样即便你是源码安装，`mim download mmdet3d --config xxx` 依然能通过 `.mim/model-index.yml` 找到配置和权重的对应关系。安装完成后可以 `ls mmdet3d/.mim/` 亲自确认。

**⑤ mmdet3d 本体不编译任何 CUDA 扩展。** [setup.py:225](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L225) 写着 `ext_modules=[]`——空的扩展模块列表。文件上方虽然定义了 `make_cuda_ext()` 辅助函数（[setup.py:30-59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L30-L59)），但在当前 HEAD 并未被调用，属于旧版遗留。CUDA 算子全部由 mmcv 提供，所以「装 mmcv 时选对 CUDA 版本」比「装 mmdet3d 时编译」重要得多。不过文档也明确说明：纯 CPU（无 CUDA）环境目前不被支持（[docs/en/get_started.md:118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L118)）。

**⑥ 哪些目录会被打包。** [setup.py:205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L205)：`packages=find_packages(exclude=('configs', 'tools', 'demo'))`——只有 `mmdet3d` 包本身进入 site-packages，configs/tools/demo 通过第 ④ 点的 `.mim` 机制间接提供。这也解释了为什么源码安装后你仍然要**在仓库根目录下**运行 `python tools/train.py ...`（配合脚本里改写的 `PYTHONPATH`，见 [docs/en/get_started.md:296-302](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L296-L302)）。

#### 4.2.4 代码实践

**实践目标**：从源码以可编辑模式安装 mmdet3d，并观察安装前后环境发生的变化。

**操作步骤**：

```shell
# 前提：按 4.1 的顺序已装好 torch + mmengine + mmcv + mmdet
cd mmdetection3d
pip install -v -e .        # -v 显示详细日志，-e 可编辑模式

# 安装完成后观察三件事：
pip show mmdet3d           # 1. 元数据：版本、位置
ls mmdet3d/.mim/           # 2. .mim 软链是否生成
pip list | grep -iE "numba|open3d|trimesh|plyfile"   # 3. runtime 依赖是否被补装
```

**需要观察的现象**：

1. `pip show` 输出的 `Version` 字段与 `Location` 字段（可编辑安装会指向你的仓库路径，而不是 site-packages 里的一个拷贝）。
2. `mmdet3d/.mim/` 目录下出现 `tools`、`configs`、`demo` 等软链。
3. `-v` 详细日志里可以看到 pip 逐条解析 `runtime.txt` 依赖并安装的过程。

**预期结果**：`Version: 1.4.0`（与 [mmdet3d/version.py:3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py#L3) 一致）；`.mim` 目录包含软链；numba/open3d/trimesh/plyfile 出现在 `pip list` 中。若网络受限，open3d 等大包安装可能较慢或失败——失败时 mmdet3d 本体仍可能装上，但后续某些数据管线（如 nuScenes 可视化）会缺依赖。具体现象待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：同事抱怨「我 `pip install -e .` 装完了 mmdet3d，结果 `import mmdet3d` 报 `ModuleNotFoundError: No module named 'mmcv'`」。是 setup.py 坏了吗？

**参考答案**：不是。`install_requires` 指向的 [requirements/runtime.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/runtime.txt) 里根本没有 mmcv——官方设计就是让用户先用 MIM 装好 mmcv/mmengine/mmdet，再装 mmdet3d。让同事按 4.1.2 的流程补装三件套即可。

**练习 2**：为什么 `get_version()` 用 `exec` 读文件而不是直接 import？

**参考答案**：直接 `import mmdet3d.version` 会连带执行 `mmdet3d/__init__.py`，而包入口第一行就 `import mmcv`、`import mmdet`、`import mmengine`——打包环境里不一定有这些库。`exec` 只执行 version.py 自己的内容，把版本号取出来而不触发整条导入链（见 [setup.py:24-27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/setup.py#L24-L27)）。

**练习 3**：`pip install -e .[tests]` 和 `pip install -e .` 的区别是什么？

**参考答案**：前者会额外安装 [requirements/tests.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/tests.txt) 里声明的 pytest、flake8、parameterized 等测试工具（对应 setup.py 中 `extras_require` 的 `tests` 键），方便跑 `tests/` 目录下的单元测试；后者只装 runtime 的 10 个包。

---

### 4.3 `mmdet3d/__init__.py` 的版本断言：运行时的最后防线

#### 4.3.1 概念说明

包入口 `mmdet3d/__init__.py` 一共 38 行，核心就是**三条版本断言**。它解决的问题是：pip 的依赖解析只保证「装的时候满足约束」，但实际环境千奇百怪——有人用 `--no-deps` 跳过依赖、有人手动 `pip install -U` 把某个库升破了、有人用 MIM 装到了边界版本。任何一种情况都可能让 mmdet3d 在运行到某个深层算子时才神秘崩溃。与其让用户在几百行 traceback 里猜原因，不如在 `import mmdet3d` 的第一秒就把不兼容的环境拦下来，并用一句人话告诉用户怎么修。

值得对比的是**三道版本闸门**的微妙差异（以当前 HEAD 为准）：

| 闸门 | 位置 | mmengine 约束 | mmdet 约束 |
| --- | --- | --- | --- |
| MIM 元数据 | `requirements/mminstall.txt` | `>=0.7.1,<1.0.0` | `>=3.0.0,<3.3.0` |
| 运行时断言 | `mmdet3d/__init__.py` | `>=0.8.0,<1.0.0` | `>=3.0.0rc5,<3.4.0` |
| 文档命令 | `docs/en/get_started.md` | 最新版 | `>=3.0.0` |

注意 mmengine 一列：MIM 允许装 0.7.1，但断言要求至少 0.8.0——也就是说 **MIM 装出来的环境仍可能过不了 import 这一关**。这正是「运行时断言是最后防线」的含义：前两道闸门只是尽力而为，只有断言是每次 import 都会执行的硬检查。反过来，mmdet 的上界断言（`<3.4.0`）比 mminstall（`<3.3.0`）更宽松，说明断言与元数据的维护并不总是同步的——读源码时要养成「以代码为准」的习惯。

#### 4.3.2 核心流程

`import mmdet3d` 时，包入口的执行顺序：

```text
1. import mmcv       ── 若未安装 → ModuleNotFoundError（根本走不到断言！）
2. import mmdet      ── 同上
3. import mmengine   ── 同上
4. 读取三个库的 __version__，用 digit_version() 转成可比较的元组
5. 依次断言 mmcv / mmengine / mmdet 落在各自兼容区间内
   ── 任一失败 → AssertionError，提示应安装的版本区间
6. 全部通过 → 从 .version 导入 __version__，import 成功
```

三条断言用的是同一个数学模式——**左闭右开区间**。以 mmcv 为例：

\[
\text{兼容区间} = [\,v_{\min},\ v_{\max}) = \{\,v \mid v_{\min} \le v < v_{\max}\,\}
\]

判据等价于对「数字化」后的版本做两次比较：

\[
\text{digit\_version}(v_{\min}) \;\le\; \text{digit\_version}(v_{\text{已安装}}) \;<\; \text{digit\_version}(v_{\max})
\]

为什么上界不封死到「小于等于」？因为 OpenMMLab 的惯例是：下一个次版本（如 mmcv 2.2.0）可能引入破坏性 API 变更，但正式发布前谁也不能保证；而区间内的修订版（2.1.x）只修 bug 不改接口。左闭右开正好表达「下界版本开始可用，直到下一个潜在不兼容版本之前」。

`digit_version` 是 mmengine 提供的工具，作用是把 `'2.0.0rc4'` 这样的字符串解析成可以按字典序比较的元组。本仓库自己的 [mmdet3d/version.py:7-25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py#L7-L25) 里有一个同思路的 `parse_version_info`，其文档字符串给出了直观例子：`"1.3.0"` 解析为 `(1, 3, 0)`，`"2.0.0rc4"` 解析为 `(2, 0, 0, 'rc4')`。`digit_version` 的具体编码属于 mmengine 的实现细节，此处不展开。

#### 4.3.3 源码精读

**① 先导入三个上游库。** [mmdet3d/\_\_init\_\_.py:1-7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L1-L7)：

```python
import mmcv
import mmdet
import mmengine
from mmengine.utils import digit_version

from .version import __version__, version_info
```

关键点：**断言拦不住「没装」，只能拦「装错」**。如果 mmcv 根本不存在，`import mmcv` 这一行就会抛 `ModuleNotFoundError`，根本轮不到断言发言。这两种报错的区分是本讲实践的主角。

**② 三组版本常量。** [mmdet3d/\_\_init\_\_.py:9-19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L9-L19)：

```python
mmcv_minimum_version = '2.0.0rc4'
mmcv_maximum_version = '2.2.0'
mmcv_version = digit_version(mmcv.__version__)

mmengine_minimum_version = '0.8.0'
mmengine_maximum_version = '1.0.0'
mmengine_version = digit_version(mmengine.__version__)

mmdet_minimum_version = '3.0.0rc5'
mmdet_maximum_version = '3.4.0'
mmdet_version = digit_version(mmdet.__version__)
```

三组区间汇总成一张速查表（装环境前对着它选版本）：

| 库 | 兼容区间 | 语义 |
| --- | --- | --- |
| mmcv | \([2.0.0\text{rc}4,\ 2.2.0)\) | 2.0.0rc4 起可用，2.1.x 最稳，2.2.0 起不保证 |
| mmengine | \([0.8.0,\ 1.0.0)\) | 0.8.0 ~ 0.x 任意版本 |
| mmdet | \([3.0.0\text{rc}5,\ 3.4.0)\) | 3.0.0rc5 ~ 3.3.x |

**③ 三条断言。** 以 mmcv 为例，[mmdet3d/\_\_init\_\_.py:21-24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L24)：

```python
assert (mmcv_version >= digit_version(mmcv_minimum_version)
        and mmcv_version < digit_version(mmcv_maximum_version)), \
    f'MMCV=={mmcv.__version__} is used but incompatible. ' \
    f'Please install mmcv>={mmcv_minimum_version}, <{mmcv_maximum_version}.'
```

`assert` 的条件部分就是左闭右开区间的直接翻译（`>=` 下界 且 `<` 上界）；提示信息则把**当前版本**和**正确区间**都打出来，用户看一眼就知道该怎么改命令。mmengine 与 mmdet 的两条断言结构完全相同，分别位于 [mmdet3d/\_\_init\_\_.py:26-30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L26-L30) 和 [mmdet3d/\_\_init\_\_.py:32-36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L32-L36)。

**④ 版本号的唯一真身。** 断言之后，包入口从 `.version` 导出 `__version__`，其值定义在 [mmdet3d/version.py:3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py#L3)：`__version__ = '1.4.0'`。发版时只需改这一个文件，setup.py 会通过 4.2.3 的 `exec` 技巧自动读到新值——单一数据源，避免两处版本号打架。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：亲手触发两种版本失败，弄清「缺库」与「版本不兼容」在报错上的本质区别，并验证 `mmdet3d.__version__`。

**操作步骤**：

```shell
# 步骤 1：正常验证（前提是 4.2.4 的安装已完成）
python -c "import mmdet3d; print(mmdet3d.__version__)"
```

预期打印 `1.4.0`。

```shell
# 步骤 2：卸载 mmcv，制造「缺库」场景
pip uninstall -y mmcv
python -c "import mmdet3d"
```

预期抛出 `ModuleNotFoundError: No module named 'mmcv'`——注意这个异常来自 [mmdet3d/\_\_init\_\_.py:2](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L2) 的 `import mmcv`，**不是** AssertionError。

```shell
# 步骤 3：装回 mmcv，恢复环境
mim install 'mmcv>=2.0.0rc4,<2.2.0'
```

```python
# 步骤 4：不重装、用"伪造版本"触发断言（示例代码，建议存成 trigger_assert.py 运行）
import mmcv
mmcv.__version__ = '9.9.9'   # 伪造一个远超上界的版本号
import mmdet3d                # 这次轮到断言发言了
```

原理：Python 的模块缓存——先 `import mmcv` 再改它的 `__version__` 属性，之后 `import mmdet3d` 时包入口里的 `import mmcv` 拿到的是 `sys.modules` 中**同一个已被改过属性的对象**，于是 `digit_version('9.9.9')` 超出区间，断言失败。

**需要观察的现象**：对比步骤 2 与步骤 4 的两种报错——异常类型（`ModuleNotFoundError` vs `AssertionError`）、异常来源行号（第 2 行的 import vs 第 21 行的 assert）、信息内容（只有模块名 vs 带完整修复建议）。

**预期结果**：步骤 4 的报错信息形如：

```text
AssertionError: MMCV==9.9.9 is used but incompatible. Please install mmcv>=2.0.0rc4, <2.2.0.
```

文本直接对应 [mmdet3d/\_\_init\_\_.py:23-24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L23-L24) 的 f-string。步骤 4 依赖模块缓存机制，若在你环境中行为不同，可直接 `pip install mmcv==2.2.0`（真实越界版本）达到同样效果——但事后记得装回兼容版本。以上均为基于源码的预期，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：装了 mmcv `2.2.0` 能通过 `import mmdet3d` 吗？装 mmengine `0.7.5` 呢？

**参考答案**：都不能。mmcv 2.2.0 违反上界（区间为 \([2.0.0\text{rc}4, 2.2.0)\)，上界不含），会在 [mmdet3d/\_\_init\_\_.py:21-24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L24) 被拦；mmengine 0.7.5 违反下界 0.8.0，会在 [mmdet3d/\_\_init\_\_.py:26-30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L26-L30) 被拦。有趣的是后者恰好在 mminstall.txt 允许范围内（`>=0.7.1`），印证了 4.3.1 的「断言是最后防线」。

**练习 2**：为什么断言写成两个比较的 `and`，而不是 `mmcv_minimum_version <= mmcv.__version__ < mmcv_maximum_version` 直接比较字符串？

**参考答案**：字符串比较是按字符逐位进行的，`'2.10.0' < '2.2.0'` 在字符串序里成立，但在版本语义里是错的（2.10.0 > 2.2.0）。必须先用 `digit_version` 把版本数字化成元组再做比较。顺带一提，断言比较的是 `digit_version(...)` 的结果而不是原始字符串，正是因为这个原因。

**练习 3**：如果把 `assert` 全部删掉，mmdet3d 会立刻坏掉吗？

**参考答案**：不一定立刻——版本恰好兼容时一切照常。但一旦用户环境越界（例如 mmcv 升到 2.2.0 改了 API），错误会推迟到运行深处的某个算子调用时才爆发，traceback 与根因相距十万八千里。断言的价值不是「让代码能跑」，而是「让错误尽早、带修复建议地暴露」。

---

### 4.4 安装验证四步法

#### 4.4.1 概念说明

「装完了」不等于「装对了」。验证应该分四层递进，每层回答一个问题：

| 层次 | 问题 | 手段 | 失败说明什么 |
| --- | --- | --- | --- |
| 1. 装上了吗 | 包存在吗 | `pip list` / `pip show` | 安装步骤没执行完 |
| 2. 版本对吗 | 依赖区间满足吗 | `pip list` 对照 4.3.3 速查表 | 需要升降级某个库 |
| 3. 能导入吗 | 断言通过吗 | `import mmdet3d` | 断言信息会直接给出修复命令 |
| 4. 能推理吗 | 前向跑得通吗 | 跑 demo（下一讲 u1-l3 的主题） | 缺权重、缺数据或 CUDA 不匹配 |

前两层是「静态检查」，后两层是「动态检查」。只做静态检查的环境，经常在第 4 层才暴露 CUDA 版本不匹配这类问题。

#### 4.4.2 核心流程

```text
pip list ──→ 对照三区间 ──→ import mmdet3d ──→ 跑最小推理
   │             │               │                  │
 缺包则补装    越界则重装     断言报错则照提示修   报错则查 FAQ
```

遇到问题时，官方排障入口是 [docs/en/notes/faq.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/faq.md)（[docs/en/get_started.md:289-292](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L289-L292) 指明的排障路径）。

#### 4.4.3 源码精读

**① 官方给出的最简验证代码。** [docs/en/get_started.md:263-267](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L263-L267)（Colab 小节）：

```python
import mmdet3d
print(mmdet3d.__version__)
# Example output: 1.4.0, or an another version.
```

这三行就是第 3 层验证的全部：能 import 说明三条断言全过，能打印版本说明 `from .version import __version__` 也正常。

**② 第 4 层验证的官方流程。** [docs/en/get_started.md:126-137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L126-L137)：先用 MIM 下载配置与权重，再跑点云 demo：

```shell
mim download mmdet3d --config pointpillars_hv_secfpn_8xb6-160e_kitti-3d-car --dest .

python demo/pcd_demo.py demo/data/kitti/000008.bin \
    pointpillars_hv_secfpn_8xb6-160e_kitti-3d-car.py \
    hv_pointpillars_secfpn_6x8_160e_kitti-3d-car_20220331_134606-d42d15ed.pth --show
```

仓库里自带了示例点云 `demo/data/kitti/000008.bin`，所以第 4 层验证不需要你自备数据。无显示器的远程服务器可去掉 `--show`，预测结果会保存为 JSON（[docs/en/get_started.md:142-144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L142-L144)）。这层验证的完整剖析是下一讲（u1-l3）的内容，本讲只需跑到第 3 层。

#### 4.4.4 代码实践

**实践目标**：把四层验证走一遍，产出一页「环境体检报告」。

**操作步骤**：

```shell
# 第 1 层：装上了吗
pip show mmdet3d | head -3

# 第 2 层：版本对吗（把输出与 4.3.3 的速查表对照）
pip list | grep -iE "^(mmcv|mmengine|mmdet|mmdet3d|torch) "

# 第 3 层：能导入吗
python -c "import mmdet3d; print('mmdet3d', mmdet3d.__version__)"

# 第 4 层：能推理吗（可选，需要下载权重；详细解读见 u1-l3）
# mim download mmdet3d --config pointpillars_hv_secfpn_8xb6-160e_kitti-3d-car --dest .
# python demo/pcd_demo.py demo/data/kitti/000008.bin <config> <ckpt>
```

**需要观察的现象**：每一层的输出是否落在预期内；第 3 层若抛异常，异常类型是 `ModuleNotFoundError`（回到第 1 层补装）还是 `AssertionError`（回到第 2 层对照区间）。

**预期结果**：第 1 层显示 `Version: 1.4.0`；第 2 层三个库都落在兼容区间内；第 3 层打印 `mmdet3d 1.4.0`；第 4 层（若执行）在画布或 `outputs/pred/000008.json` 中给出车辆检测结果。第 4 层需要下载约几十 MB 的权重，网络受限时可跳过，留待 u1-l3。具体现象待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`import mmdet3d` 成功，但跑 demo 时报 CUDA 相关错误。这说明四层验证中哪一层缺失了？

**参考答案**：第 4 层。import 只验证了「依赖版本区间满足」，而 demo 前向会真正调用 mmcv 的 CUDA 算子，此时才暴露 PyTorch/CUDA/mmcv 三者编译目标不匹配的问题。这正是四层递进的必要性：静态检查替代不了动态检查。

**练习 2**：`pip list` 里看到 mmcv 版本是 `2.2.0`，但 `import mmdet3d` 居然没报错。可能是什么原因？

**参考答案**：最可能的是 `import` 到了别的环境里的 Python（没激活正确环境），或者 `sys.path` 里存在旧版本 mmcv 抢先被找到。用 `python -c "import mmcv; print(mmcv.__file__, mmcv.__version__)"` 确认实际加载路径。按断言逻辑（[mmdet3d/\_\_init\_\_.py:21-24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L24)），真加载到 2.2.0 时必然报 AssertionError。

---

## 5. 综合实践

**任务：从零搭建一个可被断言「放行」的环境，并写下一页环境体检报告。**

把本讲四个模块串成一条完整流水线（建议在全新 conda 环境中执行）：

1. **建环境**（4.1）：`conda create --name openmmlab python=3.8 -y && conda activate openmmlab`，按 [docs/en/get_started.md:22-34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/get_started.md#L22-L34) 安装 PyTorch。
2. **装三件套**（4.1）：`pip install -U openmim`，然后依次 `mim install mmengine`、`mim install 'mmcv>=2.0.0rc4'`、`mim install 'mmdet>=3.0.0'`。
3. **源码安装 mmdet3d**（4.2）：`git clone https://github.com/open-mmlab/mmdetection3d.git -b dev-1.x && cd mmdetection3d && pip install -v -e .`，确认 `.mim` 软链生成。
4. **静态验证**（4.4）：`pip list` 对照三区间速查表，记录每个库的版本。
5. **动态验证**（4.3）：`python -c "import mmdet3d; print(mmdet3d.__version__)"`。
6. **破坏性实验**（4.3）：卸载 mmcv 观察 `ModuleNotFoundError`；再用伪造版本（或真实安装一个越界版本）触发 `AssertionError`，把两种报错截图/复制进报告；最后恢复环境。
7. **产出**：写一份 `environment-report.md`，包含依赖版本表、两条报错原文，以及用一句话解释「为什么断言的下界用 `>=`、上界用 `<`」。

完成标准：第 5 步打印出版本号、第 6 步两种报错都被观察到、第 7 步报告能自圆其说。全程不需要任何数据集和 GPU 训练。

## 6. 本讲小结

- MMDetection3D 的依赖链是四层结构：PyTorch → mmengine/mmcv/mmdet → mmdet3d，安装顺序不可颠倒，核心原因是 mmcv 的预编译算子必须匹配本机 torch + CUDA 组合。
- `pip install -e .` 只安装 [requirements/runtime.txt](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/requirements/runtime.txt) 的 10 个包；torch 与三件套都要手动预装，可选依赖通过 `.[tests]` / `.[optional]` 等 extras 提供。
- `setup.py` 里藏着三个实用细节：用 `exec` 读版本号避免触发导入链；editable 安装时把 configs/tools 软链进 `mmdet3d/.mim/`；`ext_modules=[]` 说明 mmdet3d 本体不编译 CUDA 扩展（算子全部来自 mmcv）。
- [mmdet3d/\_\_init\_\_.py:21-36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L36) 的三条断言用左闭右开区间 \([v_{\min}, v_{\max})\) 拦截不兼容版本，是每次 import 都执行的「最后防线」；它拦「装错」不拦「没装」——缺库报 `ModuleNotFoundError`，越界报带修复建议的 `AssertionError`。
- 版本号唯一真身在 [mmdet3d/version.py:3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/version.py#L3)（`'1.4.0'`），setup.py 与 `mmdet3d.__version__` 都源自它。
- 验证安装要分四层递进：pip list（装上）→ 对照区间（装对）→ import（断言通过）→ 跑 demo（真能用），静态检查不能替代动态检查。

## 7. 下一步学习建议

环境就绪后，下一讲 **u1-l3《跑通第一个推理 Demo》** 将深入 `demo/pcd_demo.py` 与 `mmdet3d/apis/inference.py`，把本讲第 4 层验证升级成一次完整的 3D 检测体验——你会看到 `init_model` 如何从配置 + 权重构建模型，`inference_detector` 的输出 `Det3DDataSample` 里到底装了什么。

如果想提前热身，可以顺手翻一翻这两个文件（只看轮廓，不必读懂）：

- [mmdet3d/apis/inference.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py) —— `init_model` / `inference_detector` 的所在。
- [docs/en/notes/faq.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/notes/faq.md) —— 安装出问题时的第一求助站，很多坑前人已踩过。

再往后，u1-l4 将带你俯瞰整个仓库目录结构，u1-l5 进入贯穿全库的配置系统——那是理解 MMDetection3D 一切的钥匙。
