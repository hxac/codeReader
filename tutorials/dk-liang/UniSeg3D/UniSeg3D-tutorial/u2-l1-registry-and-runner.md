# u2-l1 注册器机制与 Runner 生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚配置文件里的一句 `dict(type='UniSeg3D', ...)` 是如何一步步变成一个真正的 Python 对象的——即 mmengine 的 Registry（注册表）机制。
2. 掌握「配置字符串能被查到」的前提：定义类的模块必须先被 import。理解 `custom_imports = dict(imports=['uniseg3d'])`、`tools/train.py` 里的 `sys.path` 注入、`uniseg3d/__init__.py` 的 import 清单这三者如何串成一条注册触发链路。
3. 理解 `Runner.from_cfg(cfg)` 到底组装了哪些东西，`runner.train()` / `runner.test()` 的执行顺序，以及 Hook（钩子）在训练循环的哪些时机被回调。

本讲是全书第一块「机制性」地基：u1-l3 已经画出了代码地图，本讲回答「地图上的类是如何被配置文件点名并组装起来的」。后面所有讲义（模型、损失、评估）都会反复用到本讲的结论：**配置里的 `type='XXX'` 字符串 → 注册表查表 → 类实例化**。

## 2. 前置知识

本讲不需要任何深度学习数学，但需要几个通用编程概念。用最通俗的方式解释：

- **工厂模式（Factory pattern）**：不让调用方直接写 `UniSeg3D(...)` 这种具体类名，而是把「想要什么」写成数据（一个字符串 + 参数字典），交给一个「工厂函数」去生产对象。好处是调用方只依赖字符串，不依赖具体代码文件。
- **注册表模式（Registry pattern）**：工厂模式的具体实现方式。维护一张全局字典 `{名字 → 类}`，类定义时主动「登记」进这张表；工厂函数拿着名字来查表。mmengine 的 `Registry` 类就是这张字典的封装。
- **装饰器（decorator）**：`@MODELS.register_module()` 写在 `class Xxx:` 上一行，Python 解释器在**定义这个类的那一刻**执行装饰器，把 `Xxx` 登记进 `MODELS` 注册表。所以注册是 import 的副作用。
- **import 的副作用**：Python 第一次 `import uniseg3d` 时，会把 `uniseg3d/__init__.py` 从头到尾执行一遍；其中每行 `from .xxx import Yyy` 又会执行对应的 `uniseg3d/xxx.py`。文件里所有 `@MODELS.register_module()` 装饰器都会随之执行——**这就是「import 即注册」**。
- **Hook（钩子）**：训练循环在固定时机（每个 iter 前/后、每个 epoch 前/后……）回调的对象，用来做与主流程正交的事：打日志、存 checkpoint、调整学习率、清缓存等。可以理解为「订阅了训练循环事件」的插件。
- **epoch / iter**：iter（iteration，迭代）= 训练循环走一次「取一个 batch → 前向 → 反向 → 更新参数」；epoch = 把训练集完整过一遍。主配置用的是 `EpochBasedTrainLoop`（按 epoch 计数）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tools/train.py` | 训练入口（mmdet3d 官方拷贝） | `Config.fromfile`、`Runner.from_cfg`、`runner.train()` |
| `tools/test.py` | 测试/评估入口 | `cfg.load_from = checkpoint`、`runner.test()` |
| `uniseg3d/__init__.py` | 核心包的 import 清单 | 19 行 import 如何触发 29 个类的注册 |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 主配置（下文简称「主配置」） | `custom_imports`、model/dataloader/hooks/loops 各段与注册表的对应 |
| `uniseg3d/uniseg3d.py` | 主模型 | `@MODELS.register_module()` 装饰器、`__init__` 内的递归 build |
| `uniseg3d/unified_criterion.py` | 损失总调度 | `__init__` 内对 5 个子损失的递归 build |
| `uniseg3d/scannet_dataset.py` | 数据集类 | `@DATASETS.register_module()` 的最小注册示例 |

> mmengine / mmdet3d 本体（`Registry`、`Runner` 等类的实现）随 pip 包安装在 site-packages 里，不在本仓库中，因此本讲对这些类的描述以概念为主，无法给出仓库内的永久链接；涉及版本细节处会标注「以本地安装的 mmengine/mmdet3d 为准」。

## 4. 核心概念与源码讲解

### 4.1 Registry 与 MODELS.build 流程

#### 4.1.1 概念说明

UniSeg3D 的配置文件里没有一行 `import`，却写满了 `type='UniSeg3D'`、`type='SpConvUNet'`、`type='SparseMatcher'` 这样的字符串。把这些字符串变成对象的机制就是 **Registry（注册表）**：

- mmengine 定义了 `Registry` 类：内部维护一张 `{字符串名字 → 类}` 的字典，提供 `register_module()`（登记）和 `build(cfg)`（查表生产）两个核心能力。
- OpenMMLab 生态按「类别」分表：`MODELS`（模型与损失）、`TRANSFORMS`（数据变换）、`DATASETS`（数据集）、`METRICS`（评估指标）、`TASK_UTILS`（匹配器等工具）。本仓库从 `mmdet3d.registry` 导入这些表（见下方源码），mmdet3d 的表又挂在 mmengine 根表的父链上，并配合配置里的 `default_scope`（默认作用域，mmdet3d 的 `default_runtime.py` 里设为 `'mmdet3d'`，以本地安装的 mmdet3d 为准）决定查表顺序。
- `build(cfg)` 的规则很简单：`cfg` 是个 dict，取出 `cfg['type']` 当类名查表，**其余键值全部作为 `__init__` 的关键字参数**传给查到的类。

它解决的问题是「配置与代码解耦」：主配置只是纯数据，谁都能读；框架拿到数据后按名字装配。这样更换骨干网络、损失函数都不用改任何 Python 入口代码，改配置字符串即可。

#### 4.1.2 核心流程

`MODELS.build(dict(type='UniSeg3D', backbone=..., decoder=..., ...))` 的执行过程：

```text
MODELS.build(cfg)
 ├─ 取出 type = cfg['type'] = 'UniSeg3D'
 ├─ 注册表查表（当前 scope 优先，找不到再沿父注册表链向上找）
 ├─ 得到类 uniseg3d.uniseg3d.UniSeg3D
 ├─ 其余键（in_channels、backbone、decoder、criterion...）作为 kwargs
 │   调用 UniSeg3D.__init__(**kwargs)
 └─ UniSeg3D.__init__ 内部遇到子 dict 时【递归】再 build：
      self.unet     = MODELS.build(backbone)   # 'SpConvUNet'
      self.decoder  = MODELS.build(decoder)    # 'UnifiedQueryDecoder'
      self.lang     = MODELS.build(lang)       # 'LangModule'
      self.criterion= MODELS.build(criterion)  # 'ScanNetUnifiedCriterion'
      └─ ScanNetUnifiedCriterion.__init__ 内部再 build 5 个子损失
           └─ OVInstanceCriterion.__init__ 内部再 TASK_UTILS.build(matcher)
                └─ SparseMatcher.__init__ 内部再 build 3 个 cost
```

也就是说：**配置文件里的嵌套 dict 结构，会被逐层翻译成一层套一层的对象图**。整个 UniSeg3D 模型（骨干 + 解码器 + 语言分支 + 五路损失 + 匹配器 + 代价函数）就是这样从几段纯数据「长」出来的。

#### 4.1.3 源码精读

**① 登记侧：装饰器把类写进注册表。** 主模型和主数据集各用一行装饰器登记：

[uniseg3d/uniseg3d.py:259-260](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L259-L260) —— `UniSeg3D` 类定义前挂 `@MODELS.register_module()`。注意默认登记名就是类名字符串 `'UniSeg3D'`，与主配置 `model` 段的 `type='UniSeg3D'`（见下文 ③）严格对应；名字对不上，build 时会直接报「未注册」错误。

[uniseg3d/scannet_dataset.py:6-11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L6-L11) —— 数据集类从 `mmdet3d.registry` 导入 `DATASETS` 表并登记 `ScanNetUnifiedSegDataset`。`MODELS`、`TRANSFORMS`、`METRICS`、`TASK_UTILS` 同样都来自 `mmdet3d.registry`（如 [uniseg3d/transforms_3d.py:7-12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L7-L12) 的 `from mmdet3d.registry import TRANSFORMS`）——这再次印证 u1-l3 的结论：本仓库自己**不定义**注册表，只是向 OpenMMLab 的表里填条目。

**② 组装侧：`__init__` 里的递归 build。**

[uniseg3d/uniseg3d.py:311-314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314) —— `UniSeg3D.__init__` 把配置里的四个子 dict（`backbone`/`decoder`/`lang`/`criterion`）逐个 `MODELS.build` 成子模块。这就是 4.1.2 流程图中第二层的出处。

[uniseg3d/unified_criterion.py:12-16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L12-L16) —— 损失总调度再 build 出 5 个子 criterion（语义、实例、点提示、文本提示、对比），对应主配置 `criterion` 段里的五个嵌套 dict。再往下一层，[uniseg3d/instance_criterion.py:90-91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L90-L91) 用 `TASK_UTILS.build` 构造两个匹配器——同一次实例化可以跨表查询。

**③ 配置侧：`type` 字符串与参数。**

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:20-38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L20-L38) —— `model = dict(type='UniSeg3D', ..., backbone=dict(type='SpConvUNet', ...))`。这一段去掉所有 `type` 键后，剩余内容会原样成为 `UniSeg3D.__init__` 的实参：`data_preprocessor`、`in_channels=6`、`voxel_size=0.02`、`query_thr=0.5` 等。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:139-160](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L139-L160) —— 数据侧同理：`dataset_type = 'ScanNetUnifiedSegDataset'` 在 250-251 行被填进 `train_dataloader.dataset.type`；`train_pipeline` 列表里每个 dict 的 `type`（`LoadPointsFromFile`、`LoadAnnotations3D_`、`AddSuperPointAnnotations`……）也都会经 `TRANSFORMS` 表逐个实例化、串成管线（细节在 u3 展开）。

**④ 一个容易踩的坑：两个同名的 `train_cfg`。**

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:126-127](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L126-L127) —— 这是 **`model` 字典内部**的 `train_cfg=dict()`，消费方是模型的 `__init__`（[uniseg3d/uniseg3d.py:319-320](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L319-L320) 存成 `self.train_cfg`），承载的是推理阈值这类模型超参（`test_cfg` 里的 `topk_insts` 等）。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:320-325](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L320-L325) —— 这是**顶层**的 `train_cfg = dict(type='EpochBasedTrainLoop', ...)`，消费方是 Runner（见 4.3），决定训练循环本身。同名不同义、互不相干，靠「所在层级」区分。读配置时先看它在哪个字典里面，再判断谁消费它——这是「配置即代码」体系里最重要的读法。

#### 4.1.4 代码实践

**实践目标**：不用任何第三方库，手写一个 50 行的迷你 Registry，亲手实现「字符串 → 对象」与「递归组装」，把机制内化。

**操作步骤**：

1. 新建 `mini_registry_demo.py`（**示例代码**，与本仓库无关，放在教程目录或任意临时目录均可）：

```python
# 示例代码：手写迷你注册表，理解 mmengine Registry 的核心逻辑
class Registry:
    def __init__(self, name):
        self.name = name
        self._module_dict = {}          # {名字: 类}

    def register_module(self):
        def decorator(cls):
            self._module_dict[cls.__name__] = cls   # 登记发生在类定义那一刻
            return cls
        return decorator

    def build(self, cfg):
        cfg = dict(cfg)                 # 拷贝，避免改动原配置
        obj_type = cfg.pop('type')      # 取出名字，剩下的都是参数
        if obj_type not in self._module_dict:
            raise KeyError(f"'{obj_type}' 未注册到 {self.name}，"
                           f"可用: {list(self._module_dict)}")
        return self._module_dict[obj_type](**cfg)

MODELS = Registry('models')

@MODELS.register_module()
class Head:
    def __init__(self, dim=4):
        self.dim = dim
    def __repr__(self):
        return f'Head(dim={self.dim})'

@MODELS.register_module()
class TinyNet:
    def __init__(self, dim=4, head=None):        # 模仿 UniSeg3D.__init__ 的递归 build
        self.head = MODELS.build(head or dict(type='Head', dim=dim))
    def __repr__(self):
        return f'TinyNet(head={self.head})'

# 「配置文件」：纯数据，一行 import 都没有
model_cfg = dict(type='TinyNet', head=dict(type='Head', dim=8))
print(MODELS.build(model_cfg))
```

2. 运行 `python mini_registry_demo.py`。
3. 把 `model_cfg` 里的 `type='Head'` 改成 `type='Foot'` 再运行，观察报错信息。
4. 把 `@MODELS.register_module()` 从 `Head` 上删掉再运行，观察报错——这就是「忘记 import 定义模块」时的真实后果。

**需要观察的现象**：

- 正常运行输出 `TinyNet(head=Head(dim=8))`：外层 build 触发内层 build，嵌套 dict 变成嵌套对象。
- 步骤 3 报 `KeyError: 'Foot' 未注册...`，并打印出当前可用名字列表——与 mmengine 的「未注册」报错同构。
- 步骤 4 同样报 KeyError：**装饰器没执行过 = 类不在表里**，为 4.2 的 `custom_imports` 埋下伏笔。

**预期结果**：如上所述。本实践为纯 Python，不依赖本仓库环境，可直接运行（本讲义撰写环境未安装 mmengine，故未代跑；逻辑简单，按上述代码逐行可核）。

#### 4.1.5 小练习与答案

**练习 1**：主配置 `criterion` 段里 `inst_criterion` 的 `matcher=dict(type='SparseMatcher', ...)`，最终由哪个注册表、在哪个类的 `__init__` 里完成实例化？

**答案**：`TASK_UTILS` 表、`OVInstanceCriterion.__init__`。依据：[uniseg3d/instance_criterion.py:90-91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L90-L91) 调用 `TASK_UTILS.build(matcher)`，而 `SparseMatcher` 登记在 [uniseg3d/instance_criterion.py:726-727](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L726-L727) 的 `@TASK_UTILS.register_module()` 下。

**练习 2**：如果把主配置 `model` 段的 `type='SpConvUNet'` 拼错成 `'SpConvUnet'`，会在哪个环节报错？为什么配置文件本身不会报错？

**答案**：在 `UniSeg3D.__init__` 执行到 `MODELS.build(backbone)` 时报「未注册」类 KeyError。因为配置文件只是纯数据（一个嵌套 dict），Python 加载它时不做任何检查；只有 build 查表那一刻才会发现表里没有这个名字。这也是「配置即代码」的代价：错误暴露得晚，需要实际构建才验证。

**练习 3**：`build(cfg)` 为什么要把 `cfg['type']` 之外的键全部当 `__init__` 参数？这带来什么约束？

**答案**：因为注册表不知道每个类的构造签名，只能按「名字之外皆参数」的约定透传。约束是：配置里写的每个键必须能被对应类的 `__init__` 接收（或被其 `**kwargs` 吞掉），否则实例化时报 `TypeError: unexpected keyword argument`。所以读配置时拿类的 `__init__` 签名对照，是判断「这个键管什么」的最可靠方法，例如 [uniseg3d/uniseg3d.py:285-308](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L285-L308) 的参数表与主配置 model 段逐键对应。

### 4.2 custom_imports 触发 uniseg3d 注册

#### 4.2.1 概念说明

4.1 的机制有一个隐含前提：**注册表查表时，类必须已经登记**。而登记发生在「类所在模块被 import」的那一刻。于是产生一个鸡生蛋问题：

- `tools/train.py` 只 import 了 `mmengine` 和 `mmdet3d`，从不 import `uniseg3d`（可以在 [tools/train.py:9-14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L9-L14) 里确认，没有任何一行涉及 uniseg3d）；
- 但主配置里写满了 `type='UniSeg3D'`、`type='LoadAnnotations3D_'` 这些只在本仓库里注册的名字。

解决方案就是配置文件第 5 行的 `custom_imports`：**把「要额外 import 的模块」也写进配置**，由 mmengine 在构建过程中代为 import（mmengine 提供 `import_modules_from_strings` 工具函数负责把模块名字符串变成 `importlib.import_module` 调用；该字段被消费的具体位置随 mmengine 版本可能有差异，可在本地安装的 mmengine 源码中搜索 `import_modules_from_strings` 确认）。import 一旦发生，`uniseg3d/__init__.py` 里的 import 链就会把 29 个类的装饰器全部执行一遍。

#### 4.2.2 核心流程

完整的注册触发链路（从脚本启动到注册表就绪）：

```text
./tools/dist_train.sh configs/uniseg3d_1xb4...py 1        (u1-l2 讲过的启动方式)
 └─ 注入 PYTHONPATH=<仓库根目录>，保证 uniseg3d 包可被找到
torch.distributed.launch 拉起 tools/train.py
 └─ train.py 第 7 行 sys.path.append(仓库根目录)          ← 双保险
Config.fromfile(主配置)
 └─ 读到 custom_imports = dict(imports=['uniseg3d'])
 └─ mmengine 逐个 import：import uniseg3d
     └─ 执行 uniseg3d/__init__.py 的 19 行 import
         └─ 依次执行 uniseg3d/uniseg3d.py、query_decoder.py、
            transforms_3d.py ... 每个模块文件
             └─ 每遇到一个 @XXX.register_module() 就登记一个类
                 └─ 共 29 个类进入 5 张注册表
之后 Runner/MODELS.build 查 'UniSeg3D' 等名字 → 全部命中
```

#### 4.2.3 源码精读

**① 配置侧：一行触发。**

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:1-6](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L1-L6) —— 前两行 `_base_` 继承 mmdet3d 自带的 `default_runtime.py`（提供 `default_scope='mmdet3d'` 与运行时默认项，随 mmdet3d 包安装）和数据集基配置；第 5 行 `custom_imports = dict(imports=['uniseg3d'])` 是本仓库所有自定义类得以注册的总开关；第 6 行 `find_unused_parameters = True` 是 DDP 包装选项——UniSeg3D 是多任务模型，不是每个参数每步都参与反向传播，需要允许「未使用参数」。

**② 入口侧：为什么 import uniseg3d 能成功。**

[tools/train.py:6-7](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L6-L7) —— `sys.path.append(os.path.dirname(__file__) + '/..')` 把仓库根目录塞进 `sys.path`。这样即使不用 `dist_train.sh`（其自身也会注入 PYTHONPATH）、直接 `python tools/train.py ...` 启动，`import uniseg3d` 也能定位到仓库根下的 `uniseg3d/` 包。这是一行很朴素但不可或缺的「双保险」。

**③ 注册清单：19 行 import 与 29 个类。**

[uniseg3d/__init__.py:1-19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19) —— 包的入口只有 19 行 import（末尾空行共 21 行文件）。执行它们会把下列 29 个类全部登记（按注册表统计，逐条可由仓库内 `@register_module` 装饰器核对）：

| 注册表 | 数量 | 类（定义文件 : 装饰器行） |
| --- | --- | --- |
| `MODELS` | 13 | `UniSeg3D`(uniseg3d.py:259)、`SpConvUNet`(spconv_unet.py:94)、`QueryDecoder`(query_decoder.py:162)、`UnifiedQueryDecoder`(query_decoder.py:372)、`ScanNetUnifiedCriterion`(unified_criterion.py:6)、`ScanNetSemanticCriterion`(semantic_criterion.py:7)、`OVInstanceCriterion`(instance_criterion.py:85)、`PointPromptInstanceCriterion`(point_prompt_instance_criterion.py:48)、`TextPromptInstanceCriterion`(text_prompt_instance_criterion.py:48)、`ContrastiveCriterion`(contrastive_criterion.py:11)、`LangModule`(lang_module.py:38)、`OpenCLIPBackboneText`(openclip_backbone.py:17)、`Det3DDataPreprocessor_`(data_preprocessor.py:7) |
| `TRANSFORMS` | 9 | `LoadAnnotations3D_`(loading.py:11)、`NormalizePointsColor_`(loading.py:70)、`Pack3DDetInputs_`(formatting.py:11)、`TextPromptGeneration`(transforms_3d.py:12)、`TextPromptTest`(transforms_3d.py:63)、`PointPromptGeneration`(transforms_3d.py:112)、`PointPromptTest`(transforms_3d.py:167)、`ElasticTransfrom`(transforms_3d.py:237)、`AddSuperPointAnnotations`(transforms_3d.py:312) |
| `TASK_UTILS` | 4 | `QueryClassificationCost`(instance_criterion.py:646)、`MaskBCECost`(instance_criterion.py:673)、`MaskDiceCost`(instance_criterion.py:700)、`SparseMatcher`(instance_criterion.py:726) |
| `METRICS` | 2 | `PromptSupportedSegMetric`(unified_metric.py:24)、`PromptSupportedUnifiedSegMetric`(unified_metric.py:79) |
| `DATASETS` | 1 | `ScanNetUnifiedSegDataset`(scannet_dataset.py:10) |

**④ 注册粒度是「模块」而不是「导出名」**——一个容易被忽略的细节：

[uniseg3d/__init__.py:10-10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L10-L10) —— 这行只从 `transforms_3d` 导入了 `ElasticTransfrom` 和 `AddSuperPointAnnotations` 两个名字；但主配置 train_pipeline 里用到的 `TextPromptGeneration`、`PointPromptGeneration` 等也都能查到。原因是 Python import 执行的是**整个模块文件**：`from .transforms_3d import ElasticTransfrom` 会把 [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12-L312) 从头到尾跑一遍，文件里全部 6 个 `@TRANSFORMS.register_module()` 装饰器都会执行。所以判断「某个类注册没注册」，标准是**它所在的模块有没有被 import 到**，而不是它的名字有没有出现在 `__init__.py` 里。

#### 4.2.4 代码实践

**实践目标**：在本地训练环境（已按 u1-l2 装好 mmengine/mmdet3d）中，亲眼验证「import 前查不到、import 后查得到」。

**操作步骤**：

1. 在仓库根目录执行（**示例命令**）：

```bash
cd <仓库根目录>
python - <<'EOF'
from mmdet3d.registry import MODELS

try:                                   # import 之前先查一次
    MODELS.get('UniSeg3D')
    print('before import: found')
except KeyError:
    print('before import: KeyError, UniSeg3D 尚未注册')

import uniseg3d                        # 触发注册

print('after import :', MODELS.get('UniSeg3D'))
print('transforms   :', MODELS.root)   # 观察根注册表结构（可选）
EOF
```

2. 把 `import uniseg3d` 换成 `from uniseg3d.transforms_3d import ElasticTransfrom`，重复实验，确认 `PointPromptGeneration` 等未导出的类也已被注册。
3. 再验证配置驱动的路径：`python -c "from mmengine.config import Config; cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py'); print(cfg.custom_imports)"`，确认配置里确实携带该字段。

**需要观察的现象**：第一次查询抛 `KeyError`（类不在表里）；`import uniseg3d` 之后 `MODELS.get('UniSeg3D')` 返回 `uniseg3d.uniseg3d` 模块下的 `UniSeg3D` 类对象本身；步骤 2 中换任何 import 形式，只要触达模块，6 个 transforms 全部可用。

**预期结果**：如上。**待本地验证**（本讲义撰写环境未安装 mmengine/mmdet3d，无法代跑；`Registry.get` 未命中抛 `KeyError` 是 mmengine 的标准行为，如与本地版本不符，以本地实测为准）。

#### 4.2.5 小练习与答案

**练习 1**：如果注释掉主配置第 5 行的 `custom_imports`，改在 `tools/train.py` 顶部手动加一行 `import uniseg3d`，功能上等价吗？有什么副作用？

**答案**：功能上等价——两种方式都只是让 `uniseg3d/__init__.py` 被 import 一次。副作用是：入口脚本被改动（该文件本是 mmdet3d 官方拷贝，见 [tools/train.py:1-1](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L1-L1) 的注释），且换用其他入口（如 `tools/test.py` 或自写脚本）时容易忘记加，而配置文件跟着配置走、天然全局生效。这正是 `custom_imports` 存在的意义：把「依赖哪些自定义模块」声明在配置里，而不是散落在代码里。

**练习 2**：为什么 `tools/train.py` 里没有任何 `import uniseg3d`，训练却能用 `type='UniSeg3D'`？

**答案**：三层机制叠加：`custom_imports` 声明（[主配置 L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L5-L5)）→ mmengine 在构建过程中代为 import → `uniseg3d/__init__.py` 的 import 链执行全部装饰器。入口脚本只负责「读配置 + 建 Runner + 开跑」。

**练习 3**：想给仓库新增一个自定义数据增强类 `MyAug`，除了写类和 `@TRANSFORMS.register_module()`，还必须保证什么？

**答案**：必须保证定义它的模块会被 import 到——最直接的做法是把该模块（或其中的类）加进 [uniseg3d/__init__.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19) 的 import 清单（或在配置的 `custom_imports.imports` 里追加你的模块名），否则装饰器永远不执行，build 时报「未注册」。

### 4.3 Runner 生命周期与常用 hooks

#### 4.3.1 概念说明

如果说 Registry 是「零件目录」，Runner 就是**总装车间 + 总指挥**：

- `Runner.from_cfg(cfg)`：按配置把所有零件装配齐全——模型、数据加载器、优化器包装、学习率调度器、评估器、各种 hook、日志与可视化设施，并把「训练循环 / 验证循环 / 测试循环」对象也建好。
- `runner.train()` / `runner.test()`：开动流水线。train 会先做分布式初始化、随机种子设置（读配置的 `randomness` 段）、权重加载（`load_from`）等准备工作，然后进入训练循环；循环每走一步，都在固定时机回调各个 hook。

Hook 机制让「与主流程正交的功能」以插件形式存在：日志、checkpoint、学习率调度都不写死在循环里，循环只负责在 `before_train_iter`、`after_train_iter` 等时间点喊一声，注册进来的 hook 各自响应。mmengine 的标准 hook 触发点包括 `before_run/after_run`、`before_train/after_train`、`before_train_epoch/after_train_epoch`、`before_train_iter/after_train_iter` 以及对应的 `*_val_*`、`*_test_*` 系列。

#### 4.3.2 核心流程

`runner.train()` 的时序（含 hook 触发点）：

```text
runner.train()
 ├─ 分布式环境初始化（launcher != 'none' 时）、按 randomness 设置随机种子
 ├─ 权重加载：load_from（预训练权重） / resume（断点续训）
 ├─ before_run(hooks)
 ├─ self.train_loop.run()                    # EpochBasedTrainLoop
 │   for epoch in 1..max_epochs:             # 512
 │     ├─ before_train_epoch
 │     │   （DataLoader 每个 epoch 开始重新迭代；DDP 下 sampler 按种子重排）
 │     for data in train_dataloader:         # batch_size=4
 │       ├─ before_train_iter                # EmptyCacheHook 在这里清缓存
 │       ├─ model.train_step(data, optim_wrapper)
 │       │    └─ 前向 + 算损失 + 反向（数据先经 Det3DDataPreprocessor_ 批组装）
 │       ├─ optim_wrapper.update_params()    # 梯度裁剪 max_norm=10 后更新
 │       ├─ after_train_iter
 │       │    ├─ ParamSchedulerHook.step()   # PolyLR 推进学习率
 │       │    └─ LoggerHook.every_n_iters()  # 每 50 iter 打一行日志
 │     ├─ after_train_epoch
 │     │    └─ CheckpointHook.every_n_epochs()  # 每 8 epoch 存一次，最多留 3 个
 │     └─ 到达 val_interval 时：val_loop.run() → 遍历验证集 → evaluator 计算
 │        mIoU/mAP/PQ 等指标 → save_best 依据指标存最优 checkpoint
 └─ after_run(hooks)
```

`runner.test()` 与之类似但没有训练步：只跑 `test_loop`，逐批前向得到预测，交给 evaluator（`val_evaluator`/`test_evaluator` 段构建）汇总指标。

#### 4.3.3 源码精读

**① 训练入口：从命令行到 Runner 的六步。**

[tools/train.py:69-69](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L69-L69) —— `cfg = Config.fromfile(args.config)`：把配置文件（含 `_base_` 继承）解析成一个 `Config` 对象；此时 `custom_imports` 等字段只是数据。

[tools/train.py:75-77](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L75-L77) —— 命令行参数回写配置：`--launcher` 决定单卡还是分布式（u1-l2 讲过 dist_train.sh 传 `pytorch`）；`--cfg-options key=value` 经 `merge_from_dict` 覆盖配置任意层级（u2-l2 的实践会用到）。

[tools/train.py:80-86](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L80-L86) —— `work_dir` 三级优先级：命令行 `--work-dir` > 配置内 `work_dir` > 默认 `./work_dirs/<配置文件名>`。日志、checkpoint 都落在这里。

[tools/train.py:89-113](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L89-L113) —— `--amp` 混合精度与 `--auto-scale-lr` 学习率自动缩放：都是改写配置的开关，再次体现「入口只改配置、不碰逻辑」。

[tools/train.py:116-121](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L116-L121) —— `--resume`：断点续训，写进 `cfg.resume` / `cfg.load_from`。

[tools/train.py:123-133](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L123-L133) —— 收官两步：配置无 `runner_type` 时用默认 `Runner.from_cfg(cfg)`（本仓库所有配置都走这条），否则经 `RUNNERS` 注册表构建自定义 Runner；最后 `runner.train()` 启动 4.3.2 的时序。

**② 测试入口：多一个必填 checkpoint。**

[tools/test.py:100-123](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L100-L123) —— 与 train.py 平行的流程：加载配置、回写 launcher/work_dir。

[tools/test.py:123-123](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L123-L123) —— 关键差异：`cfg.load_from = args.checkpoint`，把命令行第二个位置参数（checkpoint 路径，[tools/test.py:19-19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L19-L19) 必填）写进配置，Runner 启动时加载权重。

[tools/test.py:136-146](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L136-L146) —— 同样是「from_cfg + 启动」，但最后调 `runner.test()`：只前向、不反向，结果交 evaluator。

**③ 配置侧：Runner 要消费的各段。**

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:247-266](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L247-L266) —— `train_dataloader` 三件套：`batch_size=4`、`num_workers=16`、`dataset=dict(type='ScanNetUnifiedSegDataset', ...)`（dataset 内部再含 `pipeline`，逐项 build 成变换链）；`val_dataloader`/`test_dataloader` 复用 test_pipeline。这些段由 Runner 的 dataloader 构建流程消费。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:295-302](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L295-L302) —— `optim_wrapper`（AdamW，lr=1e-4，梯度裁剪 max_norm=10）与 `param_scheduler`（PolyLR，power=0.9，跨满 512 epoch）。注意 `AdamW`、`SGD` 这类优化器同样来自注册表——Registry 不只管模型。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:304-311](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L304-L311) —— hook 设置：`custom_hooks` 里 `EmptyCacheHook(after_iter=True)` 每个 iter 清一次 GPU 缓存（点云场景显存压力大）；`default_hooks.checkpoint` 每 8 epoch 存一次、最多保留 3 个、并以 `all_ap/miou/pq` 三个指标择优保存（`save_best`，指标键与 u6-l2 的评估器输出对应）；`default_hooks.logger` 每 50 iter 打一行训练日志。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:313-318](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L313-L318) —— `load_from = 'work_dirs/pretrained/sstnet_scannet.pth'`：SSTNet 预训练权重作为初始化（这就是 u1-l2 强调「训练前必须下载两个预训练权重」的原因之一）；`randomness = dict(seed=1, diff_rank_seed=True)` 固定基础种子、各 rank 派生不同种子，保证可复现。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:320-325](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L320-L325) —— 三个循环：`train_cfg` 是 `EpochBasedTrainLoop`（`max_epochs=512`，`dynamic_intervals=[(1,16),(epoch-16,1)]` 表示前期每 16 epoch 验证一次、最后 16 个 epoch 每次都验证）；`val_cfg`/`test_cfg` 分别是 `ValLoop`/`TestLoop`。再次提醒：这里的 `train_cfg` 与 `model` 内部那个 `train_cfg=dict()`（[主配置 L126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L126-L126)）同名不同物。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:284-293](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L284-L293) —— `val_evaluator`/`test_evaluator` 指向 `PromptSupportedUnifiedSegMetric`（METRICS 表），test 循环结束后由它汇总六任务指标（u6-l2 专题）。

#### 4.3.4 代码实践

**实践目标**：把「配置段 → Runner 组件 → 日志输出」三方对上号，学会读 mmengine 训练日志。

**操作步骤**：

1. 按 u1-l2 的方式启动一次短训练（数据未就绪时，能在报数据缺失错误前打印出模型结构与配置即可，记录到哪一步为止）：

```bash
./tools/dist_train.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1
```

2. 打开 `work_dirs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified/` 下最新的日志文件（mmengine 同时写 `.log` 文本与 json），完成下表对照（**示例表格，待本地验证**）：

| 日志中的现象 | 来源配置段 | 来源组件/hook |
| --- | --- | --- |
| 开头完整 dump 的配置与「Hooks will be executed」风格的 hook 清单 | `default_hooks` + `custom_hooks` + mmdet3d `default_runtime` 默认项 | Runner 组装完成后打印 |
| 每 50 iter 一行 `Epoch(train) [x][yyy/zzz] ... loss: ...` | `default_hooks.logger.interval=50` | `LoggerHook` 的 `after_train_iter` |
| `lr: ...` 数值随训练缓慢下降 | `param_scheduler = PolyLR(power=0.9)` | `ParamSchedulerHook` |
| 每 8 epoch 出现 `Saving checkpoint` | `default_hooks.checkpoint.interval=8` | `CheckpointHook` 的 `after_train_epoch` |
| 验证阶段输出 mIoU/mAP/PQ | `val_evaluator` + `train_cfg.dynamic_intervals` | `ValLoop` + `PromptSupportedUnifiedSegMetric` |

3. 用 `--cfg-options default_hooks.logger.interval=1` 重跑，确认日志变成每 iter 一行——命令行覆盖直接改变了 hook 行为，验证 ① 中 `merge_from_dict` 的作用。

**需要观察的现象**：日志行的出现频率与配置数值一一联动；`interval=1` 后输出密度明显变大；若数据未准备好，日志应停在 dataloader 构建或首次取数处，而模型结构、hook 清单已经打印——说明「组装」先于「数据消费」。

**预期结果**：如上表所述逐项对应。**待本地验证**（本讲义撰写环境无法运行训练，具体日志措辞以本地 mmengine/mmdet3d 版本为准）。

#### 4.3.5 小练习与答案

**练习 1**：`runner.train()` 开始正式迭代之前，会做哪三件准备工作？分别读配置的哪些字段？

**答案**：分布式/环境初始化与随机种子设置（读 `launcher` 与 `randomness`，见 [主配置 L315-L318](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L315-L318)）；权重加载（读 `load_from`，即 [L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L313-L313) 的 SSTNet 预训练；resume 时读 `cfg.resume`）；随后 `before_run` 触发 hooks，进入 [train_cfg](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L320-L323) 声明的 `EpochBasedTrainLoop`。

**练习 2**：`tools/train.py` 和 `tools/test.py` 都以「build runner → 启动」结尾，两句启动代码差在哪？为什么 test 必须多传一个位置参数？

**答案**：train 是 `runner.train()`（[tools/train.py:133](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L133-L133)），test 是 `runner.test()`（[tools/test.py:146](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L146-L146)）。test 需要加载已训练好的权重才有意义，所以 [tools/test.py:19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L19-L19) 把 checkpoint 设为必填位置参数，并在 [L123](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/test.py#L123-L123) 写入 `cfg.load_from`；而 train 的权重来自配置里的 `load_from`（预训练初始化）或从零/断点训练。

**练习 3**：想把 checkpoint 保留策略改成「每个 epoch 都存、全部保留」，只改配置怎么写？

**答案**：把 [主配置 L307-L310](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L306-L311) 的 `default_hooks.checkpoint` 改为 `dict(interval=1)`（去掉 `max_keep_ckpts` 或设很大值；`save_best` 可保留）。不需要改任何 Python 代码——这正是 hook 机制 + 配置驱动的设计意图。也可以不改文件，用 `--cfg-options default_hooks.checkpoint.interval=1` 临时覆盖验证。

## 5. 综合实践

**任务**：写一个完全脱离 UniSeg3D、只依赖 mmengine 的最小可运行 demo——自定义一个模型和一个数据集、注册它们、用 `Config` 字典构建 `Runner` 跑通几个 epoch 的「玩具训练」。跑通它，就等于亲手复刻了本讲的全部三个模块：注册（4.1）、import 触发（4.2）、Runner + hooks + loops（4.3）。

**操作步骤**：

1. 在装有 mmengine 的环境中，仓库根目录外任意位置新建 `mini_runner_demo.py`（**示例代码**）：

```python
# 示例代码：最小 mmengine Runner 演示（无需 mmdet3d / 本仓库代码）
import torch
import torch.nn as nn
from mmengine.config import Config
from mmengine.model import BaseModel
from mmengine.registry import MODELS, DATASETS
from mmengine.runner import Runner

# ---- 模块 1：注册一个最小模型（模仿 BaseModel 的 loss 协议）----
@MODELS.register_module()
class TinyModel(BaseModel):
    def __init__(self, dim=4, data_preprocessor=None, init_cfg=None):
        super().__init__(data_preprocessor=data_preprocessor, init_cfg=init_cfg)
        self.lin = nn.Linear(dim, 1)

    def forward(self, x, y=None, data_samples=None, mode='tensor'):
        pred = self.lin(x)
        if mode == 'loss':                       # train_step 期待返回 loss 字典
            return dict(loss=((pred - y) ** 2).mean())
        return pred                              # mode='tensor'/'predict' 直接给输出

# ---- 模块 2：注册一个最小数据集（学习 y = sum(x)）----
@DATASETS.register_module()
class TinyDataset(torch.utils.data.Dataset):
    def __init__(self, size=8, dim=4):
        self.size, self.dim = size, dim
    def __len__(self):
        return self.size
    def __getitem__(self, idx):
        x = torch.randn(self.dim)
        return dict(x=x, y=x.sum().reshape(1))   # 键名 = forward 的形参名

# ---- 模块 3：纯 dict 配置 → Config → Runner（“配置即代码”）----
cfg = Config(dict(
    model=dict(type='TinyModel', dim=4),         # 注册表查名
    work_dir='work_dirs/mini_demo',
    train_dataloader=dict(
        batch_size=2, num_workers=0,
        dataset=dict(type='TinyDataset', size=8, dim=4),
        sampler=dict(type='DefaultSampler', shuffle=True),
        collate_fn=dict(type='DefaultCollate')),
    optim_wrapper=dict(optimizer=dict(type='SGD', lr=0.1)),
    param_scheduler=[],
    train_cfg=dict(by_epoch=True, max_epochs=3, val_interval=0),  # 不做验证
    default_hooks=dict(logger=dict(type='LoggerHook', interval=1)),
    default_scope=None,                          # 直接用 mmengine 根注册表
    launcher='none',
))

runner = Runner.from_cfg(cfg)                    # 对应 tools/train.py 第 126 行
runner.train()                                   # 对应 tools/train.py 第 133 行
```

2. 运行 `python mini_runner_demo.py`。
3. 逐行对照：`model` 段对应 [uniseg3d/uniseg3d.py:311-314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314) 的递归组装、`train_cfg` 段对应 [主配置 L320-L323](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L320-L323) 的循环声明、`default_hooks.logger` 对应 [主配置 L311](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L306-L311)——只是规模缩小了几百倍。
4. 把 `interval=1` 改成 `interval=2`，观察日志密度减半；把 `max_epochs=3` 改成 `1`，确认只跑一轮。

**需要观察的现象**：

- 控制台出现 mmengine 的标准训练日志（每 iter 一行，含 `loss` 与 `lr`），`work_dirs/mini_demo` 目录被自动创建，内有日志文件与按 `CheckpointHook` 默认间隔保存的 checkpoint；
- `loss` 从约 1 量级随 epoch 明显下降（学习目标 `y=sum(x)` 是线性任务，`nn.Linear(4,1)` 能很快拟合）；
- 修改 `interval`/`max_epochs` 后行为立即随之变化，无需改任何框架代码。

**预期结果**：以上全部成立即demo 跑通。**待本地验证**——本讲义撰写环境未安装 mmengine，无法代跑；若你的 mmengine 版本对 `Config(dict)` 或某些默认键有不同要求，按报错逐条补齐即可（缺什么键补什么键），这个「按报错补配置」的过程本身就是对 Runner 组装清单的最好练习。

## 6. 本讲小结

- **Registry 是名字→类的全局字典**：`build(cfg)` 取出 `cfg['type']` 查表，其余键全部作为 `__init__` 关键字参数；`UniSeg3D.__init__` 内部逐层 `MODELS.build` / `TASK_UTILS.build`，把配置的嵌套 dict 递归翻译成「骨干+解码器+语言分支+五路损失+匹配器」的对象图。
- **注册是 import 的副作用**：`@XXX.register_module()` 在类定义那一刻执行；`custom_imports = dict(imports=['uniseg3d'])`（主配置第 5 行）让 mmengine 代为 import，`uniseg3d/__init__.py` 的 19 行 import 触发全仓库 29 个类的注册（MODELS 13 / TRANSFORMS 9 / TASK_UTILS 4 / METRICS 2 / DATASETS 1）。
- **注册粒度是模块不是导出名**：判断一个类注册没注册，看它所在的模块有没有被 import 到（如 `transforms_3d.py` 只被导入 2 个名字，6 个类却全部注册）。
- **入口脚本只做三件事**：`Config.fromfile` 读配置、命令行参数回写配置（launcher/work_dir/cfg-options/resume/load_from）、`Runner.from_cfg(cfg)` 后 `runner.train()`/`runner.test()`；两个 `train_cfg` 同名键分别被模型（推理阈值）和 Runner（训练循环）消费。
- **Runner 负责总装与指挥**：from_cfg 装配模型、dataloader、优化器、调度器、评估器、hooks 与三个 loop；train() 先做种子设置与权重加载再进入 `EpochBasedTrainLoop`，每 iter 在 `before/after_train_iter` 等时机回调 hook（LoggerHook 每 50 iter 打日志、CheckpointHook 每 8 epoch 存档并按 all_ap/miou/pq 择优、EmptyCacheHook 每 iter 清缓存、ParamSchedulerHook 推进 PolyLR）。

## 7. 下一步学习建议

本讲解决的是「机制」：字符串如何变成对象、入口如何驱动一切。下一讲 **u2-l2《精读主配置：模型、数据管线与训练策略》** 将把这套机制用在真实配置上：逐段精读 `uniseg3d_1xb4` 与 `uniseg3d_4xb4` 两份配置的每个 `type` 与每个参数，并用 `--cfg-options` 做覆盖实验。此后进入单元 3 的数据流（建议先读 `uniseg3d/scannet_dataset.py` 与 `uniseg3d/loading.py`，它们是本讲 `DATASETS`/`TRANSFORMS` 注册项里最简单的两个）。想深入 mmengine 本体，可在本地安装目录阅读 `mmengine/registry/registry.py` 与 `mmengine/runner/runner.py`——本讲的每个结论都能在那里找到出处。
