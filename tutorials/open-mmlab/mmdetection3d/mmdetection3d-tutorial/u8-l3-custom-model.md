# 扩展实践：自定义模型与 example_project 模板

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 mmdet3d 官方划分的六类模型组件（encoder / backbone / neck / head / RoI extractor / loss），并知道每类的扩展套路是完全相同的。
2. 逐行读懂 `projects/example_project/` 这个官方模板：一个只打印 `Hello world!` 的 DummyResNet 是如何在不修改任何主库源码的前提下，替换掉 FCOS3D 的骨干网络的。
3. 解释 `custom_imports` 配置键的完整生效链路：谁在什么时机读取它、import 的副作用如何完成注册、`default_scope='mmdet3d'` 如何让 `type='DummyResNet'` 路由成功。
4. 独立完成闭环：写一个自定义 dense_head（或 backbone）子类 → 注册 → 用最小 config 挂接 → 不下载数据集也能验证它被正确构建。

本讲是单元 8「扩展开发」的模型侧实践，与上一讲（u8-l2 自定义数据集）合起来，构成「自己的数据 + 自己的模型」的完整二次开发能力。

## 2. 前置知识

本讲默认你已掌握前三层认知，这里只做一句话回顾，详细内容回看对应讲义：

- **注册器路由（u4-l1）**：配置里的 `dict(type='XXX')` 由注册器翻译成真实类。查找链是「本级注册表 → locations 懒导入 → parent 链上溯」三段式；`@MODELS.register_module()` 装饰器在模块被 **import 的那一刻** 以副作用完成注册——「import 即注册」是理解本讲 `custom_imports` 的钥匙。
- **配置继承（u1-l5）**：配置是普通 Python，`_base_` 声明继承来源，dict 递归合并、list 整体替换。本讲还会用到一个新语法：`_base_.model.backbone.type = 'DummyResNet'`，即用「点号路径」直接改写父配置里的某个深层字段。
- **Anchor3DHead（u5-l4）**：anchor-based 检测头有三个 1×1 卷积分支（分类 / 回归 / 方向），构造签名里嵌着 anchor_generator、bbox_coder、三个 loss 的子配置。本讲的实战就是继承它。
- **default_scope**：配置顶层的 `default_scope = 'mmdet3d'` 决定无前缀 `type` 先到哪个项目的注册器里找。FCOS3D 基座里写的 `type='mmdet.ResNet'` 则是显式跨库借用。

一个需要补充的新概念是**命名空间包（namespace package）**：Python 3.3 之后，一个目录即使没有 `__init__.py` 也能被当作包导入（PEP 420 隐式命名空间包）。本讲会验证 `projects/` 与 `projects/example_project/` 都没有 `__init__.py`，它们正是靠这一机制让 `import projects.example_project.dummy` 成立的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [projects/example_project/dummy/dummy_resnet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/dummy/dummy_resnet.py) | 模板核心：16 行的 DummyResNet，继承 mmdet 的 ResNet，注册进 mmdet3d 的 MODELS |
| [projects/example_project/dummy/\_\_init\_\_.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/dummy/__init__.py) | 包出口，导入并导出 DummyResNet，让 `custom_imports` 只需指向包名 |
| [projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py) | 模板配置：7 行完成「继承官方配置 + custom_imports + 换 backbone」 |
| [projects/example_project/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/README.md) | 社区项目 README 模板，含训练/测试命令与三阶段 checklist |
| [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) | 注册节点定义处，本讲关注 MODELS 节点 |
| [configs/_base_/models/fcos3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py) | 被替换的「受害者」：backbone 原本是 `type='mmdet.ResNet'` |
| [docs/en/advanced_guides/customize_models.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md) | 官方自定义模型指南：六类组件划分与「定义 → 导入 → 挂接」三步范式 |
| [mmdet3d/models/dense_heads/anchor3d_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py) | 实战对象的父类 Anchor3DHead |

## 4. 核心概念与源码讲解

### 4.1 六类模型组件与「定义 → 导入 → 挂接」三步范式

#### 4.1.1 概念说明

在 u4-l1 我们从注册器视角看全库，本讲换到**开发者视角**：当你想改模型时，改的东西永远是下面六类组件之一。官方文档 [customize_models.md:3-10](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md#L3-L10) 给出的分类是：

| 组件类 | 职责 | 3D 项目中的例子 |
| --- | --- | --- |
| encoder | 体素/柱体编码（backbone 之前） | HardVFE、PointPillarsScatter |
| backbone | 提取特征图 | ResNet（借自 mmdet）、SECOND |
| neck | backbone 与 head 之间的过渡 | FPN、SECONDFPN |
| head | 任务输出（框/掩码预测） | Anchor3DHead、VoteHead |
| RoI extractor | 从特征图抽 RoI 特征 | H3DRoIHead 相关、PartAggregationROIHead |
| loss | head 内的损失计算 | FocalLoss、L1Loss |

关键认知：**这六类的扩展套路是完全相同的**，因为它们全部注册在同一个 `MODELS` 节点下（RoI head 内部的 assigner/sampler/coder 走 `TASK_UTILS`）。学会替换其中一类，其余五类只是换父类、换配置字段名。

#### 4.1.2 核心流程

官方文档为每类组件给出的开发流程都是同一个三步范式（以 backbone 为例）：

```
第 1 步 定义：新建文件（如 mmdet3d/models/backbones/second.py 或任意自己的包），
        写类 + @MODELS.register_module() 装饰器
第 2 步 导入：让 Python 进程 import 这个模块（两条路线，见 4.3）
        路线 A：把模块加进 mmdet3d/models/backbones/__init__.py
        路线 B：在 config 里写 custom_imports，不动主库源码
第 3 步 挂接：config 里 model.backbone=dict(type='SECOND', arg1=xxx, ...)
        Runner 构建时 MODELS.build 递归实例化
```

注意第 2 步是唯一容易出错的环节：装饰器只在你 **import 该模块时** 执行。文件放在那里而从未被 import，注册表里就没有它，`MODELS.build` 会报 `XXX is not in the model registry` 错误。

#### 4.1.3 源码精读

注册的目标节点在 [mmdet3d/registry.py:70-72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L70-L72)：

```python
# mangage all kinds of modules inheriting `nn.Module`
MODELS = Registry(
    'model', parent=MMENGINE_MODELS, locations=['mmdet3d.models'])
```

这 3 行说明：MODELS 的 parent 是 mmengine 的根 MODELS（所以 `type='mmdet.ResNet'` 能借到 2D 库的类），locations 指向 `mmdet3d.models`（所以懒导入只在主库目录里找）。

官方文档对 backbone 三步范式的完整示范在 [customize_models.md:92-121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md#L92-L121)：定义一个继承 `BaseModule` 的 SECOND 类并装饰注册（L93-91 的代码块），然后给出两条导入路线（L95-109），最后在 config 中以 `dict(type='SECOND', arg1=xxx)` 挂接（L111-121）。其中路线 B 的原文是：

```python
custom_imports = dict(
    imports=['mmdet3d.models.backbones.second'],
    allow_failed_imports=False)
```

含义：让 MMEngine 主动 import 列出的模块，且导入失败时直接抛错而不是静默跳过（`allow_failed_imports=False`）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「import 即注册」——注册表内容随 import 动作变化。

**操作步骤**（示例代码，在装好环境的任意目录运行）：

```python
# check_registry.py
from mmdet3d.registry import MODELS

# 第一阶段：只 import 注册器，不 import 任何模型代码
try:
    MODELS.get('Anchor3DHead')
    print('第一阶段: Anchor3DHead 已在注册表中')
except Exception as e:
    print(f'第一阶段: 查不到 -> {type(e).__name__}')

# 第二阶段：import 主库模型包
import mmdet3d.models  # noqa: F401  触发全量 import，注册副作用发生
print('第二阶段: Anchor3DHead ->', MODELS.get('Anchor3DHead'))
```

**需要观察的现象**：第一阶段是否报 `KeyError`（取决于你的环境中是否有其他代码先 import 过 mmdet3d.models）不重要，关键对比是第二阶段必然能取到 `Anchor3DHead` 类对象本身。

**预期结果**：第二阶段打印出 `<class 'mmdet3d.models.dense_heads.anchor3d_head.Anchor3DHead'>`。若第一阶段就打印「已在注册表中」，说明该环境有自动 import 机制介入，可换一个干净解释器重试。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：官方文档把 HardVFE、PointPillarsScatter 归为哪一类组件？它们出现在 model 配置的哪个字段里？

答案：归为 encoder 类（更细分为 voxel encoder 与 middle encoder）。在 VoxelNet 系配置里对应 `model.voxel_encoder` 与 `model.middle_encoder` 两个字段，例如 [configs/_base_/models/second_hv_secfpn_kitti.py:13-18](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L13-L18) 的 `voxel_encoder=dict(type='HardSimpleVFE')` 与 `middle_encoder=dict(type='SparseEncoder', ...)`。

**练习 2**：为什么说「文件存在但从未被 import」是新组件最常见的失败原因？

答案：`@MODELS.register_module()` 是装饰器，它的执行时机是模块被 Python import 时。Runner 构建时 `MODELS.build` 只会在注册表里查 `type` 字符串；没被 import 过的模块其类不在注册表里，于是抛出「不在 registry」的 KeyError。这就是为什么官方为每个新组件都强制要求第 2 步「导入」。

### 4.2 example_project 模板解剖：16 行换掉一个骨干网络

#### 4.2.1 概念说明

`projects/` 目录是 mmdet3d 的「前沿孵化区」（u1-l4 已介绍 BEVFusion、DETR3D 等真实项目），而 `example_project` 是官方刻意留下的**最小可行模板**：它的全部功能是给 FCOS3D 的 ResNet 骨干加一行打印。这个「什么都没干」的例子恰恰是最好的教材——它把「不改主库一行代码、新增自己的包、注册、配置挂接」的完整骨架剥到最简，任何真实项目（换 backbone、加 head、改损失）都只是在这个骨架上加重载方法。

模板的目录结构：

```
projects/example_project/
├── README.md          # 社区项目说明模板（含三阶段 checklist）
├── dummy/             # 唯一的 Python 包
│   ├── __init__.py    # from .dummy_resnet import DummyResNet
│   └── dummy_resnet.py# 模板核心，16 行
└── configs/
    └── fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py
```

注意两个事实（可自行验证）：`projects/` 与 `projects/example_project/` 目录下**都没有** `__init__.py`，只有最里层的 `dummy/` 有。也就是说外两层是 PEP 420 隐式命名空间包——这正是「projects 目录天然就是一组可插拔扩展」的实现基础。

#### 4.2.2 核心流程

模板的生效链路可以画成一条单向链：

```
python tools/train.py projects/example_project/configs/fcos3d_dummy-....py
  └─ Config.fromfile 解析出 custom_imports = dict(imports=['projects.example_project.dummy'])
      └─ Runner 构建早期（初始化 default scope 阶段）执行该导入：
          import projects.example_project.dummy
              └─ 触发 dummy/__init__.py → from .dummy_resnet import DummyResNet
                  └─ 类定义执行到 @MODELS.register_module() → DummyResNet 进入 mmdet3d 的 MODELS
  └─ Runner 组装 model：MODELS.build(model) 递归展开
      └─ backbone=dict(type='DummyResNet', ...) 命中刚注册的类
          └─ DummyResNet.__init__ 第一行 print('Hello world!')
  └─ 其余 depth=101、frozen_stages 等参数原样透传给父类 mmdet 的 ResNet
```

链路上有一个隐含前提：`projects.example_project.dummy` 这串 import 路径必须能被 Python 解析，即**仓库根目录要在 `sys.path` 上**（u1-l2 讲过 `pip install -e .` 可编辑安装会把仓库根挂进 `sys.path`）。若你的环境没生效，兜底做法是在仓库根目录运行命令前设置 `PYTHONPATH=$PWD`。

#### 4.2.3 源码精读

模板核心 [dummy_resnet.py:1-15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/dummy/dummy_resnet.py#L1-L15)，全文仅 15 行：

```python
from mmdet.models.backbones import ResNet      # L1: 跨库继承 2D 库的 ResNet

from mmdet3d.registry import MODELS            # L3: 导入 3D 库的注册节点


@MODELS.register_module()                      # L6: 注册进 mmdet3d 的 MODELS
class DummyResNet(ResNet):                     # L7: 继承关系在此声明
    """Implements a dummy ResNet wrapper for demonstration purpose.
    Args:
        **kwargs: All the arguments are passed to the parent class.
    """

    def __init__(self, **kwargs) -> None:      # L13: 吞掉所有关键字参数
        print('Hello world!')                  # L14: 唯一的"新功能"
        super().__init__(**kwargs)             # L15: 原样转发给父类
```

四个要点：

1. **L1 + L6 的组合是「跨库继承 + 本库注册」**：类来自 `mmdet`，却注册到 `mmdet3d` 的 MODELS。这演示了一个重要自由度——你的新组件可以继承任何已注册类（哪怕在别的库），只要注册目标与配置的 `default_scope` 对得上。
2. **`**kwargs` 透传**是模板的精髓：因为配置只替换 `type` 字段（见 4.3.3），父配置里 `depth=101, frozen_stages=1, ...` 等十来个参数会原封不动流进 `__init__`。用 `**kwargs` 接住再转发，就不必逐个抄写父类签名。
3. 注册的副作用入口在包出口 [dummy/\_\_init\_\_.py:1-3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/dummy/__init__.py#L1-L3)：`from .dummy_resnet import DummyResNet` + `__all__`。因此 config 的 `custom_imports` 只需要写到**包名** `projects.example_project.dummy`，不必写到模块文件。
4. 训练与测试命令收录在 [README.md:19-31](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/README.md#L19-L31)：

```bash
python tools/train.py projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py
python tools/test.py projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py ${CHECKPOINT_PATH}
```

README 里还有一份三阶段 checklist（Milestone 1「可进 projects」→ 2「训练结果正确」→ 3「可进核心包」，见 [README.md:69-115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/README.md#L69-L115)），它描述了社区项目从孵化到合入主库的生命周期，其中最后一条「把模块搬进核心包」正是 4.1 路线 A 与路线 B 的分界线。

#### 4.2.4 代码实践

**实践目标**：验证模板端到端可用，亲眼看 `Hello world!` 在什么阶段打印。

**操作步骤**：

1. 在仓库根目录执行 README 里的训练命令（数据集缺失也没关系，我们只看构建模型的瞬间）：

```bash
python tools/train.py projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py
```

2. 若提示找不到 `projects.example_project`，改为 `PYTHONPATH=$PWD python tools/train.py ...` 再试。
3. 观察终端输出的时间顺序：`Hello world!` 出现在「Loads checkpoint by http...」/「build model」日志附近，还是在数据集报错之前。

**需要观察的现象**：`Hello world!` 只打印一次，且出现在 Runner 组装 model 阶段（远早于第一个 iteration）；随后若本机没有 nuScenes 数据，进程会在 dataloader 构建或首轮取数时报错退出。

**预期结果**：`Hello world!` 的出现证明 custom_imports → 注册 → build 三环全部接通——即使训练因缺数据而中止，「模型构建」这一步已经成功。缺数据时的具体报错文本（FileNotFoundError 还是 assert 失败）待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：DummyResNet 为什么必须写 `**kwargs` 而不是把 `depth`、`frozen_stages` 等参数逐个列出？

答案：因为配置端只替换 `backbone.type`，其余参数（`depth=101`、`num_stages=4`、`out_indices`、`frozen_stages`、`norm_cfg`、`init_cfg` 等，见 [configs/_base_/models/fcos3d.py:10-21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L10-L21)）会被 `MODELS.build` 原样作为关键字参数传进构造函数。用 `**kwargs` 接住全部再 `super().__init__(**kwargs)` 转发，类就能跟随父配置自动适配参数变化；逐个列参数则父配置每改一处都要同步改子类签名。

**练习 2**：如果把 `custom_imports` 里的 `'projects.example_project.dummy'` 改成 `'projects.example_project.dummy.dummy_resnet'`，模板还能工作吗？

答案：能。`dummy/__init__.py` 已经 import 了 `dummy_resnet`，所以指向包或指向模块文件最终都会执行 `dummy_resnet.py` 的类定义与装饰器，两种写法等价。区别只在语义：指向包更稳（包内多模块时不用逐个列举），官方文档在 [customize_models.md:49-54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md#L49-L54) 的示例则直接指向模块文件。

**练习 3**：`projects/` 目录下没有 `__init__.py`，为什么 `import projects.example_project.dummy` 不报错？

答案：Python 3.3+ 的 PEP 420 隐式命名空间包机制允许无 `__init__.py` 的目录作为包被导入，前提是路径上每一级都能被找到（即仓库根目录在 `sys.path` 上）。`projects` 与 `projects.example_project` 是命名空间包，最里层 `dummy/` 则是带 `__init__.py` 的常规包。

### 4.3 custom_imports 机制与配置挂接：两种导入路线的分岔口

#### 4.3.1 概念说明

4.1 的三步范式中，第 2 步「导入」有两条互斥路线，选择标准是**你是否愿意修改主库源码**：

| 路线 | 做法 | 适合场景 | 代价 |
| --- | --- | --- | --- |
| A：改 `__init__.py` | 把新模块加进 `mmdet3d/models/xxx/__init__.py` | 模块已成熟，打算合入核心包（checklist Milestone 3） | 修改了主库源码，升级主库要处理冲突 |
| B：`custom_imports` | 配置文件里写 `custom_imports = dict(imports=[...])` | 外置开发、社区 projects、快速实验 | 每个用到该模块的配置都得带上这一行 |

example_project 选择了路线 B，因为 projects 的定位就是「不改主库」。官方文档在每类组件的「Import the module」小节都并列给出这两条路线（backbone 的对应段落是 [customize_models.md:93-109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md#L93-L109)）。

配套的还有配置侧的**点号改写语法**：`_base_.model.backbone.type = 'DummyResNet'`。它不是普通变量赋值，而是告诉 Config 系统「去 `_base_` 继承来的那棵配置树里，把 `model.backbone.type` 这一个字段改掉，其余保持」。这比重新抄一遍整个 `backbone=dict(...)` 安全得多——不会漏抄或抄错父配置的参数。

#### 4.3.2 核心流程

`custom_imports` 是 MMEngine Config 系统的顶层保留键，它的完整生命周期：

```
config 文件（含 custom_imports 键）
  → Config.fromfile 读入，custom_imports 作为普通顶层键暂存
  → Runner.from_cfg / Runner.__init__ 早期初始化 default scope 时，
    MMEngine 读取配置中的 custom_imports，
    通过 mmengine.utils.import_modules_from_strings 逐个 import 列出的模块
  → import 触发模块顶层代码：@MODELS.register_module() 执行，类进注册表
  →（之后）MODELS.build(model) 递归构建，type 字符串命中注册表
```

两个关键细节：

1. **时机**：导入发生在「构建任何模块之前」。你不需要怀疑先后顺序——注册一定发生在 build 之前，这是 MMEngine 设计好的顺序（具体实现位于 MMEngine 的 `DefaultScope` 初始化流程，见 MMEngine 官方文档 Registry 章节的「跨项目调用模块」小节）。
2. **路由**：导入完成后，`type='DummyResNet'` 为什么查得到？因为模板继承链最终引入了 [default_runtime.py:1](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L1) 的 `default_scope = 'mmdet3d'`，无前缀 type 先在 mmdet3d 的 MODELS 里找——而 DummyResNet 注册的正是这个节点（[dummy_resnet.py:3](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/dummy/dummy_resnet.py#L3) `from mmdet3d.registry import MODELS` 决定了落点）。若把 `default_scope` 改成别的值，或把注册节点换成 mmengine 根节点，路由就会失败。

一个容易踩的坑：**重复注册**。同一个模块被 import 两次通常无害（Python 模块缓存保证只执行一次），但如果同一个类名被两条路径注册到同一节点两次（例如你既手写了注册又继承了别人的注册名），`register_module()` 默认会因键冲突报错，需要显式传 `force=True` 才能覆盖。

#### 4.3.3 源码精读

模板配置 [fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py:1-7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py#L1-L7) 全文只有 7 行，逐行拆解：

```python
_base_ = [
    '../../../configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py'  # noqa
]                                                    # L1-3: 继承官方 FCOS3D 全套配置

custom_imports = dict(imports=['projects.example_project.dummy'])  # L5: 路线 B 导入

_base_.model.backbone.type = 'DummyResNet'           # L7: 点号语法，只换 type 一个字段
```

第 7 行的「受害者」是 [configs/_base_/models/fcos3d.py:10-21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L10-L21) 的 backbone 定义（原本是）：

```python
backbone=dict(
    type='mmdet.ResNet',      # 被 L7 替换成 'DummyResNet'（注意前缀 mmdet. 也一并被换掉）
    depth=101,                # 以下参数全部原样保留，经 **kwargs 流入 DummyResNet
    num_stages=4,
    out_indices=(0, 1, 2, 3),
    frozen_stages=1,
    norm_cfg=dict(type='BN', requires_grad=False),
    norm_eval=True,
    style='caffe',
    init_cfg=dict(
        type='Pretrained',
        checkpoint='open-mmlab://detectron2/resnet101_caffe')),
```

`_base_` 继承链再往上一层是 [configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py:1-15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/fcos3d/fcos3d_r101-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py#L1-L15)，它组合了 `nus-mono3d.py`（数据）、`models/fcos3d.py`（模型）、`mmdet-schedule-1x.py`（日程）、`default_runtime.py`（运行时）四个基座，并增量加上 DCN 卷积参数（`dcn=dict(type='DCNv2', ...)`）——这也是点号/增量修改思想的官方示范。

最后注意一个对比点：被替换的 `type='mmdet.ResNet'` 带前缀（显式跨库借用），替换后的 `'DummyResNet'` 不带前缀（靠 default_scope 落到 mmdet3d 的 MODELS）。**替换 type 字段会连前缀语义一起换掉**，如果你的新类注册在别的 scope，记得写全前缀（如 `'mmdet.MyResNet'`）。

#### 4.3.4 代码实践

**实践目标**：验证「点号改写只动一个字段、其余参数保留」的合并行为。

**操作步骤**：

```bash
# 用官方工具展开模板配置（print_config 会递归展开所有 _base_ 继承）
python tools/misc/print_config.py \
    projects/example_project/configs/fcos3d_dummy-resnet-caffe-dcn_fpn_head-gn_8xb2-1x_nus-mono3d.py
```

**需要观察的现象**：在展开后的完整配置中定位 `model.backbone` 块，检查：`type` 是否为 `DummyResNet`；`depth`、`frozen_stages`、`style`、`init_cfg` 等是否与 [configs/_base_/models/fcos3d.py:10-21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/fcos3d.py#L10-L21) 完全一致；`model.neck`、`model.bbox_head` 是否原样来自 FCOS3D 基座；`custom_imports` 键是否也出现在展开结果里。

**预期结果**：backbone 块除了 `type` 之外与父配置一字不差，neck/bbox_head 完全未被触碰。这验证了「最小侵入替换」。展开输出的具体排版待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：路线 A（改 `__init__.py`）和路线 B（`custom_imports`）各自在什么时机生效？本质区别是什么？

答案：两者都在「模块被 import 时」生效，本质没有区别——都是触发装饰器副作用。区别只在**谁发起 import**：路线 A 把 import 语句写进主库的 `mmdet3d/models/xxx/__init__.py`，只要 `import mmdet3d` 就连带生效；路线 B 把 import 交给 MMEngine 在 Runner 初始化时按配置执行，主库源码零改动。官方文档同一小节并列展示两条路线，见 [customize_models.md:93-109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_models.md#L93-L109)。

**练习 2**：`_base_.model.backbone.type = 'DummyResNet'` 与在子配置里重写整个 `model = dict(backbone=dict(type='DummyResNet', ...))` 相比，优势在哪里？

答案：点号写法是「单字段手术」，Config 合并时只覆盖 `model.backbone.type` 这一个叶子，父配置的其余参数自动继承；重写整个 dict 则要自己负责抄全 `depth=101`、`frozen_stages=1`、`init_cfg` 等全部参数，漏一项就得到行为不同的模型，且主库升级基座配置后不会自动跟进。

**练习 3**：如果模板配置删掉第 5 行的 `custom_imports`，训练命令会报什么错？为什么？

答案：`MODELS.build` 展开 `model.backbone` 时，在 mmdet3d 的 MODELS（及其 parent 链）里查不到 `'DummyResNet'`，抛出 `KeyError: 'DummyResNet is not in the model registry'` 一类的错误。原因是没有任何代码 import 过 `dummy_resnet` 模块，装饰器从未执行，注册表里没有这个键。

### 4.4 实战：自定义 dense_head 子类接入训练闭环

#### 4.4.1 概念说明

前三节是「读模板」，本节「写模板」：把 example_project 的骨架套到一个 3D 原生组件上。选 dense_head 有两个原因：一是它承接 u5-l4 对 Anchor3DHead 的精读，二是 head 是最常被二次开发的组件（改损失权重、加辅助分支、改正负样本策略都发生在这里）。

两种改造深度的取舍：

- **替换型**（本节示范）：新类继承旧类、只改构造期行为（加打印、改默认值）。风险最低，因为 forward/loss 全部复用父类。
- **扩展型**：重写 `loss_by_feat`、`get_targets` 等方法改训练逻辑。此时必须先读懂父类契约——Anchor3DHead 的构造签名里嵌着 anchor_generator、bbox_coder 与三个 loss 的子配置，它们在 `__init__` 内部由 `TASK_UTILS.build` / `MODELS.build` 实例化（[anchor3d_head.py:99-115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L99-L115)），你的子类不需要碰这些，透传即可。

#### 4.4.2 核心流程

完整闭环五步（与 4.1 三步范式对应，只是落到具体组件）：

```
1. 建包：在仓库根下新建 my_project/（含 __init__.py 的常规包，或仿 example_project 用命名空间包）
2. 写类：my_head.py 中继承 Anchor3DHead，@MODELS.register_module() 注册，
        __init__ 用 *args/**kwargs 透传 + 自己的新参数
3. 挂配置：新 config 继承 SECOND KITTI 官方配置，
        custom_imports = dict(imports=['my_project.my_head'])
        _base_.model.bbox_head.type = 'MyAnchor3DHead'
        （可选）_base_.model.bbox_head.my_tag = 'xxx'
4. 无数据验证：小脚本手动走一遍「Config.fromfile → import → init_default_scope → MODELS.build」
5. 训练验证：python tools/train.py my_config.py，观察构建阶段打印
```

第 4 步里的 `init_default_scope('mmdet3d')` 是仓库 tools 脚本的标准前置动作（例如 [tools/analysis_tools/get_flops.py:66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L66)、[tools/analysis_tools/benchmark.py:37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L37)）——脱离 Runner 手动 build 时，必须自己补上这个 scope 初始化，无前缀 type 才能正确路由。

#### 4.4.3 源码精读

实战对象挂接点在 SECOND 基座 [configs/_base_/models/second_hv_secfpn_kitti.py:30-35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L30-L35)：

```python
bbox_head=dict(
    type='Anchor3DHead',     # 我们要替换成 'MyAnchor3DHead' 的字段
    num_classes=3,
    in_channels=512,
    feat_channels=512,
    use_direction_classifier=True,
    ...
```

父类的注册与签名（我们继承的接口面）：

- [anchor3d_head.py:22-23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L22-L23)：`@MODELS.register_module()` 装饰 + `class Anchor3DHead(Base3DDenseHead, AnchorTrainMixin)`——注意它还混入了 AnchorTrainMixin，提供 anchor_target 单/多级两种目标计算路径，子类自动继承。
- [anchor3d_head.py:50-81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L50-L81)：构造签名全部带默认值，这意味着透传式子类的风险很低——父配置传什么就用什么。
- [anchor3d_head.py:113-115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L113-L115)：`self.loss_cls = MODELS.build(loss_cls)` 等三行展示「head 内部再 build 子配置」的递归工厂模式——这也是你之后扩展 loss 的挂接点（改 config 里 `loss_bbox=dict(type=...)` 即可，甚至不用写子类）。

SECOND 的 KITTI 训练配置 [configs/second/second_hv_secfpn_8xb6-80e_kitti-3d-3class.py:1-5](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/second/second_hv_secfpn_8xb6-80e_kitti-3d-3class.py#L1-L5) 是纯 `_base_` 组合文件（模型 + 数据 + 日程 + 运行时四件套），我们的新配置将以它为父。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：仿照 example_project，写一个自定义 dense_head 子类并注册，在不下载数据集的前提下验证它能被 config 驱动构建，再接入 train loop。

**操作步骤**：

第 1 步，在仓库根目录新建自己的扩展包（与 projects 平级即可，注意不要放进 `mmdet3d/` 主库）：

```
my_project/
├── __init__.py        # 空文件即可
├── my_head.py
└── configs/
    └── second_my-head_kitti.py
```

第 2 步，写 `my_project/my_head.py`（示例代码，非项目原有文件）：

```python
from mmdet3d.models.dense_heads import Anchor3DHead
from mmdet3d.registry import MODELS


@MODELS.register_module()
class MyAnchor3DHead(Anchor3DHead):
    """Anchor3DHead 的演示子类：仅增加一个自定义参数和一条初始化日志。"""

    def __init__(self, *args, my_tag='default', **kwargs):
        print(f'[MyAnchor3DHead] initialized with my_tag={my_tag}')
        super().__init__(*args, **kwargs)
```

第 3 步，写 `my_project/configs/second_my-head_kitti.py`（示例代码）：

```python
_base_ = '../../../configs/second/second_hv_secfpn_8xb6-80e_kitti-3d-3class.py'

custom_imports = dict(imports=['my_project.my_head'], allow_failed_imports=False)

_base_.model.bbox_head.type = 'MyAnchor3DHead'
_base_.model.bbox_head.my_tag = 'kitti-run'
```

第 4 步，写无数据验证脚本 `verify_build.py`，放在仓库根目录（示例代码）：

```python
from mmengine.config import Config
from mmengine.registry import init_default_scope
from mmengine.utils import import_modules_from_strings

from mmdet3d.registry import MODELS

cfg = Config.fromfile('my_project/configs/second_my-head_kitti.py')

# 手动执行 custom_imports（Runner 里由 MMEngine 自动做，这里等价复现）
import_modules_from_strings(**cfg.custom_imports)

# 手动初始化 scope（Runner 里同样自动做，见 get_flops.py:66 的标准写法）
init_default_scope(cfg.get('default_scope', 'mmdet3d'))

head = MODELS.build(cfg.model.bbox_head)
print('built type ->', type(head))
assert type(head).__name__ == 'MyAnchor3DHead'
```

第 5 步，运行验证（在仓库根目录，确保 `my_project` 可被导入，必要时加 `PYTHONPATH=$PWD`）：

```bash
python verify_build.py
```

第 6 步（可选，需 KITTI 数据），接入真实 train loop：

```bash
python tools/train.py my_project/configs/second_my-head_kitti.py --work-dir work_dirs/my_head_test
```

**需要观察的现象**：

- 第 5 步应打印两条信息：`[MyAnchor3DHead] initialized with my_tag=kitti-run` 与 `built type -> <class 'my_project.my_head.MyAnchor3DHead'>`。
- 故意删掉 `import_modules_from_strings` 那行重跑，应看到 `KeyError: 'MyAnchor3DHead is not in the model registry'`——亲手复现「import 即注册」的因果关系。
- 故意把 config 里 `my_tag` 那行删掉重跑，打印应变为 `my_tag=default`——验证自定义参数走的是 config → 构造函数的通道。

**预期结果**：第 5 步两个断言通过即证明「子类定义 → 注册 → custom_imports → config 挂接 → build」全链路打通；第 6 步若数据就绪，`[MyAnchor3DHead]` 日志应出现在 tools/train.py 构建模型阶段，随后训练正常迭代（训练数值应与官方 SECOND 基线一致，因为我们没有改任何计算逻辑）。本实践的运行输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：把实践中的替换目标从 `bbox_head` 换成 `neck`（比如给 SECONDFPN 加打印），需要改动哪几处？

答案：三处——(1) 新模块里改为 `from mmdet3d.models.necks import SECONDFPN` 并继承它；(2) config 里改为 `_base_.model.neck.type = 'MySECONDFPN'`；(3) `custom_imports` 指向新模块文件。注意 neck 在 VoxelNet 模型配置里的字段名是 `neck`（见 [configs/_base_/models/second_hv_secfpn_kitti.py:25-29](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/second_hv_secfpn_kitti.py#L25-L29)），挂接字段名必须与检测器 `extract_feat` 里实际读取的属性名一致。

**练习 2**：只想把 SECOND 的回归损失从 SmoothL1Loss 换成 L1Loss，不写任何子类，怎么做？

答案：直接在配置里覆盖 head 的子配置即可，这正是 [anchor3d_head.py:113-115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L113-L115) 递归 build 暴露的自由度：

```python
_base_.model.bbox_head.loss_bbox = dict(
    type='mmdet.L1Loss', loss_weight=2.0)
```

不需要 `custom_imports`，因为 L1Loss 是 mmdet 已注册组件。规律：**换「已有注册组件」零代码，换「新类」才需要本讲的注册流程**。

**练习 3**：为什么验证脚本里必须调用 `init_default_scope`，而跑 tools/train.py 时从来不用手写这句？

答案：`tools/train.py` 把配置交给 `Runner.from_cfg`，Runner 初始化时自动完成 default scope 的设置；而验证脚本绕过了 Runner 直接 `MODELS.build`，`type='MyAnchor3DHead'` 与 head 内部 `type='mmdet.FocalLoss'` 这类键的 scope 解析就没了依据。仓库自己的分析工具在脱离 Runner 用注册器时也同样补这一句，如 [tools/analysis_tools/benchmark.py:37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L37) 的 `init_default_scope('mmdet3d')`。

## 5. 综合实践

把本讲全部内容串成一个「迷你扩展项目」：

1. **建包**：在仓库根目录创建 `my_project/`，含 `__init__.py`、`my_head.py`、`my_backbone.py`、`configs/`。
2. **双组件注册**（综合 4.2 模板 + 4.4 实战）：
   - `my_backbone.py`：仿 DummyResNet 写 `MySECOND(SECOND)`，`__init__` 打印一句话，`**kwargs` 透传；
   - `my_head.py`：写 `MyAnchor3DHead(Anchor3DHead)`，带一个自定义参数。
3. **一份配置挂两个自定义组件**（综合 4.3 点号语法）：

```python
_base_ = '../../../configs/second/second_hv_secfpn_8xb6-80e_kitti-3d-3class.py'

custom_imports = dict(imports=['my_project.my_backbone', 'my_project.my_head'])

_base_.model.backbone.type = 'MySECOND'
_base_.model.bbox_head.type = 'MyAnchor3DHead'
_base_.model.bbox_head.my_tag = 'dual-custom'
```

（示例代码；注意 `custom_imports` 的 `imports` 是列表，可一次导入多个模块。）

4. **验证三连**：
   - 用 `tools/misc/print_config.py` 展开配置，确认两处 type 都已替换、其余参数未动；
   - 用 4.4.4 的 `verify_build.py` 思路同时 build `cfg.model.backbone` 与 `cfg.model.bbox_head`，确认打印与类型；
   - 有 KITTI 数据的话，跑 `tools/train.py` 若干 iteration，在日志里找到两条自定义打印出现的时刻，确认它们都发生在第 1 个 iteration 之前。
5. **对照 checklist 自查**：打开 [projects/example_project/README.md:69-83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/projects/example_project/README.md#L69-L83)，逐条核对 Milestone 1 的「模块注册进 mmdet3d.registry.MODELS 且可被 config 配置」「docstring」两项你是否已达成——这就是你向 projects 目录投稿的第一步。

## 6. 本讲小结

- mmdet3d 官方把模型组件分为六类（encoder / backbone / neck / head / RoI extractor / loss），全部注册在同一 MODELS 节点下，扩展套路统一为「定义类 + 注册 → 导入 → config 挂接」三步。
- example_project 模板用 16 行演示了完整骨架：跨库继承（mmdet 的 ResNet）+ 本库注册（mmdet3d 的 MODELS）+ `**kwargs` 透传 + 点号语法最小替换配置。
- 「import 即注册」是全库的命门：`@MODELS.register_module()` 只在模块被 import 时执行，两条导入路线（改主库 `__init__.py` vs 配置 `custom_imports`）的本质都是触发这次 import。
- `custom_imports` 由 MMEngine 在 Runner 初始化早期读取并执行；路由成功还依赖 `default_scope='mmdet3d'` 与注册节点的一致性；脱离 Runner 手动 build 时要用 `init_default_scope('mmdet3d')` 补齐这一环。
- 替换 vs 扩展是两个深度：只换 `type`（甚至只改 `loss_bbox=dict(type=...)` 子配置）是零代码替换；重写 head 方法才是扩展，动手前必须先读懂父类契约（如 Anchor3DHead 的构造签名与 train_mixins 混入）。

## 7. 下一步学习建议

- **下一讲（u8-l4）**：走进真实的 projects 生态——BEVFusion 的 BEV 级多传感器融合与 DETR3D 的 query-based 检测，看它们如何复用本讲的骨架（自己的包 + 注册器 + config）实现前沿算法，并对比点级融合与 BEV 级融合（u6-l6 埋的伏笔在此收束）。
- **源码阅读建议**：横向浏览 `projects/` 下任一真实项目（如 DETR3D）的目录结构，与 example_project 逐项对照，你会发现多出来的只是算法本体，骨架完全一致。
- **动手方向**：把 4.4 的替换型子类升级为扩展型——重写 `Anchor3DHead.loss_by_feat` 中方向损失的权重（配合 u5-l4、u5-l7 的损失知识），跑通一次真正的行为级自定义训练。
- **查漏补缺**：若对「注册表冲突 / force 覆盖 / scope 切换」仍有疑问，回看 u4-l1，并配合 MMEngine 官方文档 Registry 章节的「跨项目调用模块」小节食用。
