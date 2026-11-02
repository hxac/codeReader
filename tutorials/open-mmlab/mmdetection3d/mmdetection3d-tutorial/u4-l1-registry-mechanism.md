# 注册器机制：17 个注册节点如何串起全库

## 1. 本讲目标

上一讲（u1-l5）我们读懂了配置文件：整个实验就是一棵 `dict(type='Xxx', ...)` 嵌套的树。但一直有个问题被刻意搁置——**配置里的 `type='VoxelNet'` 只是一个字符串，程序是怎么凭这串字符找到真正的 Python 类并实例化的？** 本讲就解剖这座「字符串 → 类」的路由中枢：注册器（Registry）。

学完本讲，你应该能够：

1. 说清注册器解决什么问题，以及「注册」这个动作是在什么时机发生的（提示：是 `import` 的副作用）。
2. 读懂 [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) 里每个节点的两个关键参数：`parent`（父注册器，决定能向谁「借」模块）和 `locations`（自动导入哪些目录）。
3. 解释一个 `type` 字符串的完整查找链路：本级注册表 → `locations` 懒导入 → `parent` 链上溯 → scope 前缀切换（`mmdet.` / `mmdet3d.`）。
4. 亲手注册一个自定义 Transform，并只凭 `dict(type='...')` 把它构建出来——这是后续 u8-l3 自定义模型的地基。

先纠正一个标题带来的小误会：文件头注释写着「17 个注册节点」，但实际数下来是 **21 个**——注释漂移了。这本身就是本讲的第一个教训：**数源码，别数注释**（见 4.2）。

## 2. 前置知识

本讲假设你已理解（u1-l4 / u1-l5）：

- **配置驱动**：`configs/` 下的 `.py` 被 `Config.fromfile` 执行，`dict(type='类名', 参数=值)` 是描述一切模块的统一语法，`type` 之外的键原样传给 `__init__`。
- **四件套**：`_base_/models`、`_base_/datasets`、`_base_/schedules`、`default_runtime.py` 经 `_base_` 继承组合成完整配置。

再补充四个本讲的新概念：

- **注册器（Registry）**：一个「名字 → 类」的全局查表对象，外加一个 `build(cfg)` 工厂方法。MMEngine 提供实现，各 mm 系项目（mmcv / mmdet / mmdet3d）各自实例化自己的一套。
- **工厂模式**：不直接调用类的构造函数，而是把「想造什么」写成数据（配置字典），交给工厂函数去查表、选类、传参、实例化。好处：实验描述与代码解耦，换模型只改字符串。
- **import 的副作用**：Python 中 `import` 一个模块会执行该模块的顶层代码。`@MODELS.register_module()` 是装饰器语法糖，它在**类定义被求值时**（即模块被 import 时）把类塞进注册表。所以「注册」不需要任何显式调用，import 即注册。
- **懒导入（lazy import）与 scope**：注册器记着「该去哪些目录找类」（`locations`），查表扑空时才去 import 那些目录；`scope`（默认 `'mmdet3d'`，由 `default_runtime.py` 第一行设定）决定查表先查哪个项目，`type='mmdet.Xxx'` 这样的前缀则显式跨项目借模块。

一个容易踩的坑先说破：**`import mmdet3d` 本身不会注册任何模型**。[mmdet3d/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py) 只做三件事——import 三个上游库、算版本号、跑版本断言（u1-l2 讲过），没有任何子模块导入。既然如此，`MODELS.build(dict(type='VoxelNet', ...))` 为什么能成功？答案就是 `locations` 懒导入，4.4 节揭晓。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) | 本讲主角：21 个注册节点的定义，每个都带 `parent` 和 `locations` |
| [mmdet3d/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py) | 包入口：证明 `import mmdet3d` 不触发任何注册 |
| [mmdet3d/models/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/__init__.py) | 用星号导入聚合 models 全部子目录——`locations` 懒导入的入口 |
| [mmdet3d/models/detectors/voxelnet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py) | 「注册一个类 + 在 `__init__` 里 build 子组件」的标准范例 |
| [mmdet3d/models/detectors/single_stage.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py) | 递归构建：backbone / neck / bbox_head 逐个 `MODELS.build` |
| [mmdet3d/datasets/transforms/loading.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py) | TRANSFORMS 注册样例；`BaseTransform` 基类来自 mmcv |
| [mmdet3d/models/task_modules/builder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/builder.py) | 0.x → 1.x 迁移痕迹：旧 builder 函数如今只是 `TASK_UTILS.build` 的别名 |
| [mmdet3d/apis/inference.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py) | `init_default_scope('mmdet3d')` + `MODELS.build` 的真实调用点 |
| [configs/_base_/default_runtime.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py) | `default_scope = 'mmdet3d'` 与 parent 借用 Hook 的运行证据 |
| [configs/_base_/models/pointpillars_hv_secfpn_kitti.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py) | `mmdet.` 前缀与 `mmdet3d.` 显式前缀的真实写法 |

说明：`mmengine.registry` 的实现不随本仓库分发（本环境也未安装 mmengine 源码），因此本讲解读 mmengine 侧机制时，只陈述其公开文档化的行为，并用 mmdet3d 仓库内的配置与调用点作为运行证据；不引用无法核实的 mmengine 行号。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：注册器解决什么问题、registry.py 全景、注册与构建的配对、查找链路（scope / parent / locations）。

### 4.1 注册器要解决什么问题：从字符串到类

#### 4.1.1 概念说明

配置文件是**纯数据**——一坨嵌套的 dict 和 list，里面没有任何 import、没有任何类引用。当 Runner 拿到 `cfg.model = dict(type='VoxelNet', ...)` 时，它需要一个办法把字符串 `'VoxelNet'` 变成 `mmdet3d.models.detectors.voxelnet.VoxelNet` 这个类对象。

最朴素的方案是手写一个巨型 if-else 或字典：

```python
# 示例代码：不使用注册器时的写法（想象一下维护成本）
if cfg['type'] == 'VoxelNet':
    model = VoxelNet(**其余参数)
elif cfg['type'] == 'VoteNet':
    model = VoteNet(**其余参数)
elif cfg['type'] == 'CenterPoint':
    ...
```

注册器把这个映射表从「集中手写」变成「分散自报」：每个类在自己所在的文件里用一行装饰器声明「我叫这个名字，请登记」，注册器替你维护全局字典，并提供 `build(cfg)` 完成查表 + 实例化。新增一个模型，不需要修改任何中枢代码——这正是 configs 目录里能堆下上千个实验变体的前提。

#### 4.1.2 核心流程

注册器的最小工作原理可以概括成（伪代码）：

```text
# 注册侧：发生在「模块被 import 时」
@某注册器.register_module()
class VoxelNet: ...        # 装饰器执行 → 注册表['VoxelNet'] = VoxelNet

# 构建侧：发生在「build(cfg) 被调用时」
def build(cfg):
    cls = 注册表[cfg['type']]      # 字符串查表得到类
    其余参数 = cfg 去掉 type 键
    return cls(**其余参数)          # 实例化
```

于是「一个实验」的完整路径是（承接 u1-l5 的流程图）：

```text
config.py ──Config.fromfile──▶ cfg（纯数据）
    │
    ▼
Runner / init_model：cfg.model 交给 MODELS.build
    │  查表 'VoxelNet' → 类
    ▼
VoxelNet.__init__ 执行，内部又对 voxel_encoder / middle_encoder
等子 dict 逐个调用 MODELS.build —— 递归展开整棵配置树
```

#### 4.1.3 源码精读

先验证前置知识里那个关键事实——包入口确实不导入任何功能子模块：

[mmdet3d/__init__.py:L1-L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L1-L8)

```python
import mmcv
import mmdet
import mmengine
from mmengine.utils import digit_version

from .version import __version__, version_info
```

这里 import 了 mmcv / mmdet / mmengine（副作用之一：mmdet 自己的注册表在这一刻被填好，这是后面 `type='mmdet.Xxx'` 能用的前提之一），但**没有** `from . import models` 之类的语句；文件其余部分全是版本断言。也就是说：注册一定发生在「某个具体功能模块被 import」的时刻，而不是 `import mmdet3d` 的时刻。

再看「注册」这个动作长什么样——这是全库最短的注册样例：

[mmdet3d/models/detectors/voxelnet.py:L6-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L6-L12)

```python
from mmdet3d.registry import MODELS
...

@MODELS.register_module()
class VoxelNet(SingleStage3DDetector):
```

`@MODELS.register_module()` 不带参数，默认用**类名** `'VoxelNet'` 作为注册键。谁 import 了 `mmdet3d.models.detectors.voxelnet` 这个模块，`VoxelNet` 就进入 `MODELS` 的注册表——仅此而已，没有中央登记处。

最后看注册器文件自己的说明头，注意那个「17」：

[mmdet3d/registry.py:L1-L7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L1-L7)

```python
"""MMDetection3D provides 17 registry nodes to support using modules across
projects. Each node is a child of the root registry in MMEngine.

More details can be found at
https://mmengine.readthedocs.io/en/latest/tutorials/registry.html.
"""
```

注释说 17 个节点；实际用 `Registry(...)` 实例化的是 21 个（4.2 逐一清点）。注释在节点增加后忘了更新——读源码时永远以代码为准。

#### 4.1.4 代码实践

**实践目标**：不看任何框架，用 15 行代码手工实现「注册 + 构建」，体会注册器解决的问题。

**操作步骤**：把下面代码存成独立脚本 `mini_registry.py`（示例代码，不依赖 mmdet3d，任意 Python 环境可运行），然后执行 `python mini_registry.py`。

```python
# 示例代码：手写一个最小注册器
REGISTRY = {}                     # 名字 → 类 的全局表

def register(name):
    def deco(cls):                # 装饰器：类定义时立即执行
        REGISTRY[name] = cls
        return cls
    return deco

def build(cfg):                   # 工厂：dict → 实例
    cfg = dict(cfg)
    cls = REGISTRY[cfg.pop('type')]
    return cls(**cfg)

@register('AddOffset')            # 注册动作 = import 副作用
class AddOffset:
    def __init__(self, offset=1.0):
        self.offset = offset

if __name__ == '__main__':
    obj = build(dict(type='AddOffset', offset=2.5))   # 只凭字典构建
    print(type(obj).__name__, obj.offset)
```

**需要观察的现象**：脚本输出实例类型与参数值；注意 `@register('AddOffset')` 在类定义求值那一刻就把条目写进了 `REGISTRY`，`build` 里没有任何 if-else。

**预期结果**：打印 `AddOffset 2.5`。再试着把 `@register` 那行注释掉重跑，`build` 会抛 `KeyError: 'AddOffset'`——这正是「忘了注册」时的典型报错形态（MMEngine 里同样以 KeyError 报错，4.3.4 会遇到）。

#### 4.1.5 小练习与答案

**练习 1**：为什么注册器选择「装饰器分散注册」而不是「集中一个大字典」？
**参考答案**：集中字典意味着每新增一个类都要改中枢文件，产生合并冲突且必须让中枢 import 所有实现（容易循环导入）。分散注册把声明放在实现文件里，注册随 import 自动发生，模块间零耦合；代价是必须保证「实现模块被 import」——这正是 `locations` 机制存在的理由（见 4.4）。

**练习 2**：如果两个不同文件都执行 `@MODELS.register_module()` 且类同名，会发生什么？
**参考答案**：MMEngine 的 `register_module` 默认禁止覆盖重复键名（除非显式传 `force=True`），重复注册会直接报错，提示该名字已被占用。这是防止「悄悄被顶替」的保护机制。（具体报错文案待本地验证。）

**练习 3**：`import mmdet3d` 之后立刻 `MODELS.build(dict(type='VoxelNet', ...))` 能成功吗？为什么？
**参考答案**：能。`import mmdet3d` 确实没导入 models 子包，但 `MODELS` 节点声明了 `locations=['mmdet3d.models']`，查表扑空时 MMEngine 会临时 import 该目录（即执行 [mmdet3d/models/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/__init__.py) 的星号导入），注册随之完成后再查一次表。详见 4.4。

### 4.2 mmdet3d/registry.py 全景：节点、parent 与 locations

#### 4.2.1 概念说明

[mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) 全文只做一件事：从 mmengine **借来 21 个根注册器作父节点**，再逐个声明自己的 `locations`。两个参数的含义：

- **`parent`（父注册器）**：本节点查不到的名字，可以沿着父链向上借。mmdet3d 的每个节点都以 mmengine 的同名节点为父。mmengine 的注册器是「根」，mmdet 的注册器同样是 mmengine 的孩子——mmdet3d 与 mmdet 是**兄弟**而非父子，所以互相借模块必须走 scope 前缀（4.4）。
- **`locations`（自动导入位置）**：一个模块路径列表。查表扑空时，MMEngine 会尝试 import 这些路径，让沿途的 `@register_module()` 生效，然后再查一次。它就是一张「注册节点 → 源码目录」对照表，u1-l4 曾用它当地图，本讲看清它的定义处。

#### 4.2.2 核心流程

21 个节点按职能分六组（组内共享相近的 locations）：

| 分组 | 节点（共 21 个） | locations | 管什么 |
| --- | --- | --- | --- |
| 训练引擎（9） | RUNNERS、RUNNER_CONSTRUCTORS、LOOPS、HOOKS、OPTIMIZERS、OPTIM_WRAPPERS、OPTIM_WRAPPER_CONSTRUCTORS、PARAM_SCHEDULERS、LOG_PROCESSORS | `mmdet3d.engine`（HOOKS 为 `mmdet3d.engine.hooks`） | Runner、训练循环、Hook、优化器与学习率调度 |
| 数据（3） | DATASETS、DATA_SAMPLERS、TRANSFORMS | `mmdet3d.datasets`（TRANSFORMS 为 `mmdet3d.datasets.transforms`） | 数据集类、采样器、数据管线变换 |
| 模型（4） | MODELS、MODEL_WRAPPERS、WEIGHT_INITIALIZERS、TASK_UTILS | `mmdet3d.models` | 检测器/骨干/头部/损失，以及 anchor 生成器、bbox coder、assigner 等任务工具 |
| 评估（2） | METRICS、EVALUATOR | `mmdet3d.evaluation` | KittiMetric、NuScenesMetric 等指标与评估器 |
| 可视化（2） | VISUALIZERS、VISBACKENDS | `mmdet3d.visualization` | Det3DLocalVisualizer 与存储后端 |
| 推理（1） | INFERENCERS | `mmdet3d.api.inferencers`（注意：实际包名是 `mmdet3d/apis`，见 4.2.3 末尾） | 四类 Inferencer |

注意一个规律：**mmdet3d 目前多数训练引擎类节点自己没有对应实现**（代码里留着 `TODO: update the location when mmdet3d has its own runner`），locations 只是指向目录备用，真正的类（如 `EpochBasedTrainLoop`、`SGD`）全靠 parent 链从 mmengine 借。

#### 4.2.3 源码精读

首先，所有父节点都来自 mmengine——文件开头一次性借齐：

[mmdet3d/registry.py:L9-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L9-L33)

```python
from mmengine.registry import DATA_SAMPLERS as MMENGINE_DATA_SAMPLERS
from mmengine.registry import DATASETS as MMENGINE_DATASETS
from mmengine.registry import EVALUATOR as MMENGINE_EVALUATOR
from mmengine.registry import HOOKS as MMENGINE_HOOKS
...
from mmengine.registry import Registry
```

`MMENGINE_XXX` 前缀只是为了命名区分，它们随后作为 `parent=` 传入。看三个代表性节点的完整定义：

[mmdet3d/registry.py:L36-L55](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L36-L55)

```python
RUNNERS = Registry(
    # TODO: update the location when mmdet3d has its own runner
    'runner',
    parent=MMENGINE_RUNNERS,
    locations=['mmdet3d.engine'])
...
HOOKS = Registry(
    'hook', parent=MMENGINE_HOOKS, locations=['mmdet3d.engine.hooks'])
```

`Registry` 的第一个位置参数 `'runner'` / `'hook'` 是**节点名**（报错信息里会用它，如「xxx is not in the model registry」），不是 scope。

[mmdet3d/registry.py:L58-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L58-L72)

```python
DATASETS = Registry(
    'dataset', parent=MMENGINE_DATASETS, locations=['mmdet3d.datasets'])
...
TRANSFORMS = Registry(
    'transform',
    parent=MMENGINE_TRANSFORMS,
    locations=['mmdet3d.datasets.transforms'])

MODELS = Registry(
    'model', parent=MMENGINE_MODELS, locations=['mmdet3d.models'])
```

[mmdet3d/registry.py:L109-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L109-L117)

```python
METRICS = Registry(
    'metric', parent=MMENGINE_METRICS, locations=['mmdet3d.evaluation'])
EVALUATOR = Registry(
    'evaluator', parent=MMENGINE_EVALUATOR, locations=['mmdet3d.evaluation'])

# manage task-specific modules like anchor generators and box coders
TASK_UTILS = Registry(
    'task util', parent=MMENGINE_TASK_UTILS, locations=['mmdet3d.models'])
```

TASK_UTILS 是个值得记住的节点：anchor 生成器、bbox coder、assigner、sampler 这些「任务工具」都注册在这里（4.3.3 有实例），它和 MODELS 共用 `mmdet3d.models` 目录。

最后看文件尾部的 INFERENCERS，它藏着一个真实的小瑕疵：

[mmdet3d/registry.py:L137-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L137-L141)

```python
# manage inferencer
INFERENCERS = Registry(
    'inferencer',
    parent=MMENGINE_INFERENCERS,
    locations=['mmdet3d.api.inferencers'])
```

`mmdet3d.api`（单数）这个包**并不存在**——实际目录是 `mmdet3d/apis`（复数，见 u1-l4 的目录图）。也就是说这个节点的 locations 自动导入是落空的；实际使用中 Inferencer 靠显式导入完成注册，比如 [mmdet3d/apis/__init__.py:L6-L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/__init__.py#L6-L8) 直接 `from .inferencers import ... LidarDet3DInferencer ...`。后果在 4.4.5 留了一道验证题。这也再次说明：`locations` 不是魔法，它只是「import 哪个路径」的清单，路径写错就静默失效。

#### 4.2.4 代码实践

**实践目标**：用程序数出 21 个节点，并打印任一节点的 locations，验证 4.2.2 的表格。

**操作步骤**（需按 u1-l2 装好 mmdet3d）：

```python
# registry_census.py
from mmdet3d import registry

own = [k for k in vars(registry) if k.isupper() and not k.startswith('MMENGINE_')]
print(len(own))                      # 预期 21
for name in ['MODELS', 'TRANSFORMS', 'HOOKS', 'INFERENCERS']:
    node = getattr(registry, name)
    print(name, '->', node.locations, '| scope =', node.scope)
```

**需要观察的现象**：第一行输出的数字；四个节点各自的 locations 与 scope。

**预期结果**：打印 `21`；`MODELS -> ['mmdet3d.models']`，`TRANSFORMS -> ['mmdet3d.datasets.transforms']` 等；scope 应为 `'mmdet3d'`（registry.py 里没人显式传 scope，MMEngine 会根据 Registry 实例化所在的包自动推断）。若你的 mmengine 版本没有 `locations` / `scope` 属性，可改试 `node._import_locations`（属性名以本地版本为准，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`Registry('model', ...)` 里的 `'model'` 字符串什么时候会出现在你眼前？
**参考答案**：在构建失败时报错信息里——MMEngine 会提示「XxxClass is not in the model registry」。看到「xxx registry」字样就能反推出是哪个节点查表失败，这是排查 `type` 写错 / 忘注册的第一线索。

**练习 2**：为什么 `OPTIMIZERS` 这类节点 mmdet3d 里几乎没有自己的实现，却还要定义？
**参考答案**：占位 + 确立查找链。定义子节点后，`default_scope='mmdet3d'` 下查找优化器会先查 mmdet3d 的 OPTIMIZERS（自己没有），再沿 parent 链到 mmengine 借到 `SGD` / `AdamW`。将来 mmdet3d 想加自己的优化器，只需在 `mmdet3d.engine` 下注册，配置无需改动。

**练习 3**：METRICS 的 locations 是 `mmdet3d.evaluation`（包），而 HOOKS 是 `mmdet3d.engine.hooks`（子包）。为什么粒度不同？
**参考答案**：懒导入 import 的是包的 `__init__.py`。`mmdet3d/evaluation/__init__.py` 会导入 metrics 子模块，一层就够；而 `mmdet3d/engine/__init__.py` 下还有 optimizers 等其他关注点，把 locations 收窄到 `engine.hooks` 可以少加载无关模块。粒度选择是「导入副作用范围」与「覆盖面」的权衡。

### 4.3 注册与构建：@register_module() 与 .build() 的配对

#### 4.3.1 概念说明

注册器两侧各有一个动作：

- **注册侧** `@XXX.register_module()`：默认以类名为键；也可以传 `name='别名'`（可同时叠多个装饰器注册多个名字）。注册进哪个节点，完全由「你 import 的是哪个节点」决定，与类继承体系无关。
- **构建侧** `XXX.build(cfg)`：`cfg` 必须含 `type` 键；查到类后其余键原样作 `__init__` 关键字参数。**递归性**来自一个朴素事实：外层类在自己的 `__init__` 里对子 dict 再调 `build`，于是一行 `MODELS.build(cfg.model)` 就能滚雪球式实例化整棵配置树。

一个重要推论：因为构建时传的是「子 dict」，所以**类的默认参数值也可以写成 `dict(type=...)`**——换组件连配置文件都不用改，改默认值即可。

#### 4.3.2 核心流程

```text
MODELS.build(cfg.model)            # cfg.model = dict(type='VoxelNet', voxel_encoder=..., ...)
  │ 查表：'VoxelNet' → VoxelNet 类
  ▼
VoxelNet.__init__(voxel_encoder=..., middle_encoder=..., backbone=..., ...)
  │ 内部逐个：self.voxel_encoder = MODELS.build(voxel_encoder)   # PillarFeatureNet
  │          self.middle_encoder = MODELS.build(middle_encoder) # PointPillarsScatter
  │          （父类 SingleStage3DDetector 再 build backbone/neck/bbox_head）
  ▼
一棵由真实对象组成的模型树；每个节点的 __init__ 都是下一层 build 的发起者
```

#### 4.3.3 源码精读

**注册侧六连看**——六个不同节点上的注册长什么样：

模型节点（检测器）：

[mmdet3d/models/detectors/voxelnet.py:L11-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L11-L12)

```python
@MODELS.register_module()
class VoxelNet(SingleStage3DDetector):
```

数据变换节点（注意基类 `BaseTransform` 来自 **mmcv**，不是 mmengine）：

[mmdet3d/datasets/transforms/loading.py:L8-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L8-L19)

```python
from mmcv.transforms import LoadImageFromFile
from mmcv.transforms.base import BaseTransform
...
from mmdet3d.registry import TRANSFORMS


@TRANSFORMS.register_module()
class LoadMultiViewImageFromFiles(BaseTransform):
```

任务工具节点（anchor 生成器）：

[mmdet3d/models/task_modules/anchor/anchor_3d_generator.py:L11-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/anchor/anchor_3d_generator.py#L11-L12)

```python
@TASK_UTILS.register_module()
class Anchor3DRangeGenerator(object):
```

指标、Hook、可视化节点：

- [mmdet3d/evaluation/metrics/kitti_metric.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L19-L20)：`@METRICS.register_module()` + `class KittiMetric(BaseMetric)`——val_evaluator 里的 `type='KittiMetric'` 由它路由。
- [mmdet3d/engine/hooks/visualization_hook.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L19-L20)：`@HOOKS.register_module()` + `class Det3DVisualizationHook(Hook)`——default_runtime 里的 `type='Det3DVisualizationHook'` 由它路由。
- [mmdet3d/visualization/local_visualizer.py:L41-L42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L41-L42)：`@VISUALIZERS.register_module()` + `class Det3DLocalVisualizer(DetLocalVisualizer)`——继承自 **mmdet** 的可视化器再注册进 mmdet3d 节点，跨库复用的直接例子。

**别名注册**——Inferencer 是全库唯一用 `name=` 的地方，而且叠了两个装饰器、注册两个名字：

[mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L28-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L28-L30)

```python
@INFERENCERS.register_module(name='det3d-lidar')
@INFERENCERS.register_module()
class LidarDet3DInferencer(Base3DInferencer):
```

于是 `'LidarDet3DInferencer'`（类名）与 `'det3d-lidar'`（连字符风格的任务名）都能查到同一个类——u1-l3 用过的命令行风格任务名就是这么来的。

**构建侧**——递归工厂的两层真实代码。外层（VoxelNet 自己 build 两个专属子组件）：

[mmdet3d/models/detectors/voxelnet.py:L33-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L33-L34)

```python
        self.voxel_encoder = MODELS.build(voxel_encoder)
        self.middle_encoder = MODELS.build(middle_encoder)
```

父类（backbone / neck / bbox_head 的 build 发生在 SingleStage3DDetector 的 `__init__`）：

[mmdet3d/models/detectors/single_stage.py:L45-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L45-L52)

```python
        super().__init__(
            data_preprocessor=data_preprocessor, init_cfg=init_cfg)
        self.backbone = MODELS.build(backbone)
        if neck is not None:
            self.neck = MODELS.build(neck)
        bbox_head.update(train_cfg=train_cfg)
        bbox_head.update(test_cfg=test_cfg)
        self.bbox_head = MODELS.build(bbox_head)
```

注意 `bbox_head.update(...)`：把 train_cfg / test_cfg 塞进子 dict **再** build，这是「配置在传递途中被加工」的常见手法。

transform 也会组合 transform（DataBaseSampler 内部构建一个点云加载器，u3-l7 讲过）：

[mmdet3d/datasets/transforms/dbsampler.py:L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L121)

```python
        self.points_loader = TRANSFORMS.build(points_loader)
```

**一处历史化石**——0.x 时代每个子系统有自己的 builder 函数，1.x 统一进注册器后，旧接口只剩别名和弃用警告：

[mmdet3d/models/task_modules/builder.py:L8-L17](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/builder.py#L8-L17)

```python
BBOX_ASSIGNERS = TASK_UTILS
BBOX_SAMPLERS = TASK_UTILS
BBOX_CODERS = TASK_UTILS


def build_assigner(cfg: ConfigType, **default_args) -> Any:
    """Builder of box assigner."""
    warnings.warn('``build_assigner`` would be deprecated soon, please use '
                  '``mmdet3d.registry.TASK_UTILS.build()`` ')
    return TASK_UTILS.build(cfg, default_args=default_args)
```

三个旧注册器名直接赋值成 `TASK_UTILS`——assigner / sampler / bbox coder 在 1.x 里共用一个节点，这是 u5-l6 讲任务模块时要接上的伏笔。

最后，`locations` 懒导入的入口就是这个聚合文件，import 它等于 import models 全部子目录：

[mmdet3d/models/__init__.py:L1-L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/__init__.py#L1-L16)

```python
from mmdet3d.models.layers.fusion_layers import *  # noqa: F401,F403
from .backbones import *  # noqa: F401,F403
from .data_preprocessors import *  # noqa: F401,F403
...
from .voxel_encoders import *  # noqa: F401,F403
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成规格里的核心任务——定义一个新的 `BaseTransform` 子类，用 `TRANSFORMS.register_module()` 注册，然后**只凭字典**把它构建出来，并观察注册失败时的报错形态。

**操作步骤**：在仓库根目录新建 `my_transform.py`（这是你自己新建的练习文件，不属于仓库源码），内容如下：

```python
# 练习代码：仿照 mmdet3d/datasets/transforms/loading.py 的注册写法
from mmcv.transforms.base import BaseTransform   # 与 loading.py:9 同源
from mmdet3d.registry import TRANSFORMS


@TRANSFORMS.register_module()
class MyNormalizeRange(BaseTransform):
    """把 results['points'] 截断到给定范围并记录一条日志。"""

    def __init__(self, pcd_range=(-40, -40, -3, 40, 40, 1), tag='demo'):
        self.pcd_range = pcd_range
        self.tag = tag

    def transform(self, results):
        import numpy as np
        pts = np.asarray(results['points'])
        x0, y0, z0, x1, y1, z1 = self.pcd_range
        inside = ((pts[:, 0] >= x0) & (pts[:, 0] <= x1) &
                  (pts[:, 1] >= y0) & (pts[:, 1] <= y1) &
                  (pts[:, 2] >= z0) & (pts[:, 2] <= z1))
        results['points'] = pts[inside]
        results[f'{self.tag}_num_kept'] = int(inside.sum())
        return results


if __name__ == '__main__':
    # 1) 只凭字典构建（不写 MyNormalizeRange(...)）
    cfg = dict(type='MyNormalizeRange', tag='run1', pcd_range=(-10, -10, -2, 10, 10, 2))
    t = TRANSFORMS.build(cfg)
    print('built:', type(t).__name__, '| tag =', t.tag)

    # 2) 真实数据走一遍 transform
    import numpy as np
    fake = {'points': np.random.default_rng(0).uniform(-50, 50, (1000, 3))}
    out = t(fake)
    print('kept', out['run1_num_kept'], 'of 1000 points')

    # 3) 注册失败路径：构建一个不存在的类型
    try:
        TRANSFORMS.build(dict(type='NoSuchTransform'))
    except Exception as e:
        print('error type:', type(e).__name__)
        print('message head:', str(e)[:120])
```

运行 `python my_transform.py`。

**需要观察的现象**：

1. 第 1 步打印的实例类型与参数——字典里的 `tag` / `pcd_range` 是否原样出现在实例属性上。
2. 第 2 步保留的点数是否在按范围裁剪后的合理比例内（范围体积占 \((20 \times 20 \times 4) / (100 \times 100 \times 6) \approx 2.7\%\)，1000 个均匀随机点大约保留 20~40 个）。
3. 第 3 步抛出的异常类型与报错信息开头——记下它提到的 registry 名字。

**预期结果**：`built: MyNormalizeRange | tag = run1`；保留点数为几十个量级；第 3 步抛 `KeyError`，报错信息会指出该类型不在 transform 注册表中（MMEngine 的 `build_from_cfg` 以 KeyError 报「找不到类型」，具体文案随版本略有差异，**待本地验证**）。

**延伸一步**（验证懒导入对本实践的影响）：把脚本拆成两半——A 文件只写类定义与注册，B 文件 `import A` 后再 build；然后在 B 里**不 import A**、直接 `TRANSFORMS.build(dict(type='MyNormalizeRange'))`，观察报错。你的自定义类不在任何 `locations` 清单里，没人会替你 import 它——这就是为什么自定义模块要么显式导入、要么借助 u8-l3 将讲到的 `custom_imports` 配置。

#### 4.3.5 小练习与答案

**练习 1**：`@INFERENCERS.register_module(name='det3d-lidar')` 为什么额外注册一个连字符风格的名字？
**参考答案**：类名（`LidarDet3DInferencer`）适合 Python 代码里 `from ... import`；连字符名（`det3d-lidar`）是 Inferencer 体系的「任务标识符」，供按任务名路由（还有 `det3d-mono`、`det3d-multi_modality`、`seg3d-lidar`）。一个类两个键，两种使用方式各取所需。

**练习 2**：`SingleStage3DDetector.__init__` 里为什么能对 `bbox_head` 直接 `.update()`，而 `backbone` 不行？
**参考答案**：`.update()` 是 dict 的方法；`backbone` 参数允许传「已构建好的 nn.Module 实例」或 None 之外的形态时就不能假定它是 dict。此处实现假定 `bbox_head` 一定是 dict（配置传参的标准形态），于是可以就地加工后再 build——这是一种「接口约定 + 快速失败」的取舍。

**练习 3**：注册键默认取类名。那么 `VoxelNet` 与 `PointPillars` 是什么关系？配置里为什么找不到 `type='PointPillars'`？
**参考答案**：mmdet3d 1.x 中**不存在**注册名为 `'PointPillars'` 的类。PointPillars 是一种「配置方案」：检测器骨架用通用的 `VoxelNet`（`configs/_base_/models/pointpillars_hv_secfpn_kitti.py` 第 4 行 `type='VoxelNet'`），「柱体」特性由 `voxel_encoder=PillarFeatureNet`（第 14 行）+ `middle_encoder=PointPillarsScatter`（第 21 行）这对组件体现。配置系统的组合性让「算法名」退化为组件组合的标签，而不必是类名。

### 4.4 查找链路：scope 前缀、parent 回退与 locations 懒导入

#### 4.4.1 概念说明

现在把三个机制拼成一条完整的查找链。当 `XXX.build(cfg)` 拿到 `type` 字符串时，MMEngine 的行为（按其官方文档语义）：

1. **解析前缀**：`type='mmdet.Resize'` 拆成 scope=`'mmdet'` + 名字=`'Resize'`；无前缀则用当前 scope（由 `default_scope` 决定，mmdet3d 全部配置第一行都是 `default_scope = 'mmdet3d'`）。
2. **scope 一致或无前缀**：先查**本级**注册表；扑空则触发 **locations 懒导入**（import 4.2 表中的目录，注册生效）再查一次；仍扑空则沿 **parent 链**向上（mmengine 根注册器）找。
3. **scope 不一致（带前缀）**：通过 MMEngine 的 DefaultScope 机制**切换到目标项目的同名注册器**，再按第 2 步查——这就是 mmdet3d 配置能写 `type='mmdet.FocalLoss'` 的原因。

三条链各有真实证据，下面逐一验收。

#### 4.4.2 核心流程

```text
type = 'mmdet.FocalLoss'          type = 'VoxelNet'                 type = 'IterTimerHook'
        │                                 │                                │
        ▼                                 ▼                                ▼
[前缀 mmdet ≠ 默认 mmdet3d]      [无前缀，本级 = mmdet3d MODELS]    [无前缀，本级 = mmdet3d HOOKS]
切换到 mmdet 的 MODELS               查本级：无（没人 import 过）        查本级：无
        │                                 ▼                                ▼
查 mmdet 注册表：命中             locations 懒导入 mmdet3d.models    parent 链 → MMENGINE_HOOKS
        │                                 ▼                                ▼
FocalLoss 类                    星号导入触发海量注册 → 再查：命中     mmengine 的 IterTimerHook 类
                                    │
                                    ▼
                                VoxelNet 类
```

三个入口分别对应配置里的跨库损失（4.4.3 第一段）、`init_model` 的模型构建（4.4.3 第三段）、default_runtime 的 Hook 借用（4.4.3 第二段）。

#### 4.4.3 查找链路的运行证据

**证据一：scope 前缀写法**。PointPillars 基础配置的损失函数全部借自 mmdet：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L52-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L52-L62)

```python
        loss_cls=dict(
            type='mmdet.FocalLoss',
            use_sigmoid=True,
            gamma=2.0,
            alpha=0.25,
            loss_weight=1.0),
        loss_bbox=dict(
            type='mmdet.SmoothL1Loss', beta=1.0 / 9.0, loss_weight=2.0),
```

`'mmdet.'` 前缀让查找切到 MMDetection 的 MODELS 注册表。前缀不只出现在配置里，**源码里的默认参数同样这么写**：[mmdet3d/models/dense_heads/anchor3d_head.py:L69-L78](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L69-L78) 中 `loss_cls` 的默认值就是 `dict(type='mmdet.CrossEntropyLoss', ...)`。

也可以显式写自家前缀消除歧义。同一份配置的 assigner 里：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L64-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L64-L72)

```python
    train_cfg=dict(
        assigner=[
            dict(  # for Pedestrian
                type='Max3DIoUAssigner',
                iou_calculator=dict(type='mmdet3d.BboxOverlapsNearest3D'),
```

`'Max3DIoUAssigner'` 不带前缀（默认 scope=mmdet3d，直接命中自家 TASK_UTILS），`'mmdet3d.BboxOverlapsNearest3D'` 则显式点名自家 scope——两种写法都指向 mmdet3d 的实现，显式前缀的好处是即使这段 dict 被挪到别的项目配置里也不会被误解析。

**证据二：parent 链借用**。看 default_runtime 的一组 Hook：

[configs/_base_/default_runtime.py:L1-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L1-L9)

```python
default_scope = 'mmdet3d'

default_hooks = dict(
    timer=dict(type='IterTimerHook'),
    logger=dict(type='LoggerHook', interval=50),
    param_scheduler=dict(type='ParamSchedulerHook'),
    checkpoint=dict(type='CheckpointHook', interval=-1),
    sampler_seed=dict(type='DistSamplerSeedHook'),
    visualization=dict(type='Det3DVisualizationHook'))
```

前五个 Hook（IterTimerHook / LoggerHook / ParamSchedulerHook / CheckpointHook / DistSamplerSeedHook）在 mmdet3d 的 HOOKS 节点里**根本没有注册**——它们全是 mmengine 的类，靠 parent 链（`HOOKS = Registry('hook', parent=MMENGINE_HOOKS, ...)`）借来；只有最后一个 `Det3DVisualizationHook` 是 mmdet3d 自己注册的（4.3.3 已看它的注册行）。同一行配置里五个借的、一个自家的，这就是 parent 机制每天都在运转的证据。第 17 行的 `log_processor = dict(type='LogProcessor', ...)` 同理，`LogProcessor` 也来自 mmengine。

**证据三：懒导入 + 显式初始化 scope**。u1-l3 用过的 `init_model` 是最浓缩的范例：

[mmdet3d/apis/inference.py:L66-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L66-L69)

```python
    convert_SyncBN(config.model)
    config.model.train_cfg = None
    init_default_scope(config.get('default_scope', 'mmdet3d'))
    model = MODELS.build(config.model)
```

这里**没有一行** `import mmdet3d.models`，`MODELS.build` 却能造出 VoxelNet——全靠 locations 懒导入。而 `init_default_scope`（从 `mmengine.registry` 导入，见 [mmdet3d/apis/inference.py:L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L14)）先行设置默认 scope，配置深处那些 `mmdet.` 前缀的子 dict 才能正确切换注册器。同一函数里还有第二处构建，用 `lazy_init=True` 只取 metainfo 不加载数据：

[mmdet3d/apis/inference.py:L91-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L91-L94)

```python
        test_dataset_cfg = deepcopy(config.test_dataloader.dataset)
        # lazy init. We only need the metainfo.
        test_dataset_cfg['lazy_init'] = True
        metainfo = DATASETS.build(test_dataset_cfg).metainfo
```

DATASETS 节点的构建同样依赖懒导入（`locations=['mmdet3d.datasets']`）。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「懒导入前后注册表从空到满」的过程，并验证跨库前缀路由。

**操作步骤**：在仓库根目录运行（需已安装 mmdet3d、mmdet、mmengine）：

```python
# lazy_import_probe.py
from mmengine.config import Config
from mmengine.registry import init_default_scope
from mmdet3d.registry import MODELS, TRANSFORMS

print('before build, own MODELS entries:', len(MODELS.module_dict))

init_default_scope('mmdet3d')                     # 仿照 inference.py:68
cfg = Config.fromfile(
    'configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py')

model = MODELS.build(cfg.model)                   # 触发懒导入 + 递归构建
print('after build, own MODELS entries:', len(MODELS.module_dict))
print('model type:', type(model).__name__)
print('voxel_encoder:', type(model.voxel_encoder).__name__)
print('loss_cls:', type(model.bbox_head.loss_cls).__name__)

# 跨库前缀：在 mmdet3d 的 TRANSFORMS 里借 mmdet 的 Resize
resize = TRANSFORMS.build(dict(type='mmdet.Resize', scale=(640, 480)))
print('resize from:', type(resize).__module__, type(resize).__name__)
```

**需要观察的现象**：第一次 `len(MODELS.module_dict)` 的值；build 成功后该值的暴涨；`loss_cls` 的实际类型来自哪个库；`mmdet.Resize` 构建出的对象的 `__module__`。

**预期结果**：第一次打印 `0`（至此没人 import 过 `mmdet3d.models`）；`MODELS.build` 内部懒导入后，第二次打印为几百量级；`model type: VoxelNet`；`voxel_encoder: PillarFeatureNet`；`loss_cls` 是 mmdet 的 `FocalLoss`（其 `__module__` 以 `mmdet.` 开头）；`resize from: mmdet.models... Resize`。整个过程在 CPU 即可完成（PointPillars 组件不含稀疏卷积 CUDA 算子）。属性名 `module_dict` 与各打印值的精确形态**待本地验证**——若 `module_dict` 不可用，改查 `MODELS._module_dict`。

**如果懒得跑代码**，最低成本的替代验证：`grep -n "import mmdet3d.models" mmdet3d/apis/inference.py` 一无所获，而 u1-l3 的 demo 确实构建出了模型——懒导入是唯一解释。

#### 4.4.5 小练习与答案

**练习 1**：把 `default_scope` 从 `'mmdet3d'` 改成 `'mmdet'`，PointPillars 配置还能跑吗？会发生什么？
**参考答案**：不能正常跑。默认 scope 变为 mmdet 后，无前缀的名字会先查 mmdet 的注册表：`'VoxelNet'`、`'PillarFeatureNet'`、`'Anchor3DHead'` 等 mmdet 均未注册，而 mmdet 的注册器与 mmdet3d 互为兄弟（parent 都是 mmengine），parent 链**不会**横向借到对方，于是构建直接 KeyError。scope 前缀（`mmdet3d.Xxx`）在此场景反而成了救命的显式声明。

**练习 2**：`INFERENCERS` 的 locations 写成了不存在的 `mmdet3d.api.inferencers`（4.2.3）。设计一个实验判断它造成的实际影响。
**参考答案**：在新开的 Python 会话里只执行 `from mmdet3d.registry import INFERENCERS`，然后 `print(INFERENCERS.get('det3d-lidar'))`。若返回 `None`（或 build 报 KeyError），说明懒导入确实因路径拼错而失效，注册只能靠显式 `from mmdet3d.apis import ...`（[mmdet3d/apis/__init__.py:L6-L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/__init__.py#L6-L8)）完成；若居然能查到，则说明你的 mmengine 版本对该错误位置另有处理。两种结果都是有效结论（**待本地验证**）。

**练习 3**：为什么 `mmdet3d/__init__.py` 要 `import mmdet`？删掉这行对注册器机制有什么影响？
**参考答案**：`import mmdet3d` 时顺带把 mmdet 的全部模块 import 一遍，mmdet 自己的注册表随之填充、其 scope 也在 MMEngine 的全局映射中登记。删掉后，只要任何地方显式 import 过 mmdet（或安装了它且某处触发导入），`mmdet.` 前缀仍可用；但「只 import mmdet3d 就能用跨库组件」的保证会失效，某些只依赖这一副作用的调用路径可能报「scope 未注册」或 KeyError。版本断言（u1-l2）的存在也说明这条依赖链是被有意钉死的。

## 5. 综合实践

把本讲四个模块串成一次「注册器体检」，产出一张你自己的路由验收表。任务：对下面 5 个问题逐项给出「命令/代码 + 观察到的结果」：

| # | 验证问题 | 用到的机制 | 预期观察 |
| --- | --- | --- | --- |
| 1 | mmdet3d 到底定义了几个注册节点？ | 4.2 节点清点 | 21 个（注释里的 17 是漂移） |
| 2 | 懒导入前注册表是空的吗？ | 4.4 证据三 | `MODELS.module_dict` 长度为 0 → build 后暴涨 |
| 3 | 自定义类能否只凭字典构建？ | 4.3.4 主实践 | `TRANSFORMS.build(dict(type='MyNormalizeRange'))` 成功 |
| 4 | 跨库前缀真的切了注册表吗？ | 4.4 证据一 | `mmdet.Resize` 构建出的对象 `__module__` 在 mmdet 下 |
| 5 | default_runtime 的 Hook 是谁的类？ | 4.4 证据二 | 前 5 个 Hook 的类定义在 mmengine，最后 1 个在 mmdet3d |

第 5 项的做法提示：`from mmengine.registry import HOOKS as MMHOOKS` 之外，最直接的是对 `default_hooks` 里的每个 `type` 字符串，用 `from mmdet3d.registry import HOOKS; cls = HOOKS.build(dict(type=...))` 后打印 `cls.__module__`——模块路径会明白告诉你类来自 `mmengine.hooks...` 还是 `mmdet3d.engine.hooks...`（注意 IterTimerHook 等 Hook 构造参数各异，若个别 Hook 需要参数，改用 `HOOKS.get(type_str).__module__` 即可，`.get` 不实例化只查表）。

完成后再回头修改 4.3.4 的 `MyNormalizeRange`：给它加一个 `name='my-norm'` 的别名注册（参照 [lidar_det3d_inferencer.py:L28-L29](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L28-L29) 的叠装饰器写法），确认两个键都能 build 到同一个类。这张表和这个别名实验，就是你下一阶段做自定义模型（u8-l3）时的排障模板。

## 6. 本讲小结

- 注册器是「名字 → 类」的全局查表 + `build(cfg)` 工厂；`dict(type=...)` 里的字符串靠它路由到真实类，**注册发生在模块被 import 的那一刻**（装饰器副作用），而 `import mmdet3d` 本身不注册任何东西。
- [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) 定义 **21 个**节点（文件头注释的 17 已过时），每个节点两个关键参数：`parent=MMENGINE_*` 构成向 mmengine 的父链，`locations` 是「查表扑空时自动 import 哪些目录」的清单，即注册节点 → 源码目录对照表。
- 查找链路三段式：**本级注册表 → locations 懒导入 → parent 链上溯**；带前缀的 `type='mmdet.Xxx'` 则经 DefaultScope 切到目标项目的同名注册器。`default_runtime.py` 里 5 个借来的 mmengine Hook 与 1 个自家 Hook 同框，是 parent 链的日常运行证据。
- 构建是**递归**的：`MODELS.build(cfg.model)` 之后，每个类在自己的 `__init__` 里继续 build 子 dict（VoxelNet → PillarFeatureNet / PointPillarsScatter，SingleStage3DDetector → backbone / neck / bbox_head），一行 build 滚出整棵对象树。
- 别名注册 `name=` 可一个类多个键（Inferencer 的 `det3d-lidar`）；构建失败以 KeyError 报「不在某 registry」，报错里的 registry 名是排障第一线索。
- 两个诚实的坑：`'PointPillars'` 不是注册名，PointPillars = `VoxelNet` + `PillarFeatureNet` + `PointPillarsScatter` 的组合；`INFERENCERS` 的 locations 把 `apis` 误写为 `api`，自动导入落空，靠显式 import 兜底。

## 7. 下一步学习建议

注册器是模型世界的门，下一讲 **u4-l2 检测器分层体系** 就穿过这扇门：梳理 `Base3DDetector` → `SingleStage3DDetector` / `TwoStage3DDetector` / `MVXTwoStageDetector` 的继承树，看 `loss` / `predict` / `_forward` 三种模式如何消费本讲熟悉的 `build` 出来的对象。

在进入下一讲前，建议做两个热身阅读：

1. [mmdet3d/models/detectors/base.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py) 的 `__init__` 与 `forward`——找一找本讲的 `MODELS.build` 在哪里出现（提示：`data_preprocessor`）。
2. MMEngine 官方注册器教程（[mmdet3d/registry.py:L5-L6](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L5-L6) 注释里给出的链接）——本讲对 mmengine 侧只讲了文档化的行为，想看 `DefaultScope` 与懒导入的逐行实现，那是权威出处。

之后再沿主线推进：u4-l3 会看到 `Det3DDataPreprocessor` 内部如何把 u3 系列讲的数据管线产物（points 键）体素化——数据侧与模型侧在那里正式会师。
