# u3-l2 BaseModule、Configurable 与 Updateable 生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Configurable` 如何借助 `parse_structured` 把 yaml 里的一个 dict 变成带类型检查的强类型配置对象。
2. 掌握 `update_step` / `update_step_end` 两个钩子在整个训练循环中被调用的确切时机（批次开始前、批次结束后、加载权重后）。
3. 理解 `BaseObject` 与 `BaseModule` 的「解析配置 → configure → 可选加载权重 → 恢复步数相关状态」四步生命周期。
4. 理解 DreamCraft3D 的渐进式训练（分辨率爬坡、哈希编码层级解锁、扩散时间步区间调度）全部建立在同一套 `Updateable` 机制之上。
5. 能给自己的自定义组件（上一讲的渐变背景）实现 `update_step`，并验证它确实被训练循环驱动。

## 2. 前置知识

在学习本讲前，你需要了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **配置对象（dataclass）**：Python 的 `@dataclass` 装饰器可以快速定义一个只装数据的类。threestudio 给每个组件定义一个内嵌的 `Config` dataclass，声明它接受哪些字段、默认值是什么。
- **OmegaConf 的 structured mode**：`OmegaConf.structured(某个dataclass的实例)` 会把 dataclass 变成一个「带 schema 的配置对象」——多写的键会报错、类型不对会报错、漏写的键用默认值补齐。这是 threestudio 做 yaml 校验的核心手段。
- **钩子（hook）**：框架在固定时机调用的函数。你只负责实现函数体，框架负责调用。PyTorch Lightning 的 `on_train_batch_start` 就是一个钩子；threestudio 在它之上又造了一层自己的钩子（`update_step`）。
- **递归分发**：一个对象调用 `do_update_step` 时，会先遍历自己的所有属性，凡是实现了 `Updateable` 的属性也跟着被调用，形成一棵「更新树」。system 更新 → 带动 geometry/renderer/guidance 更新 → 再带动它们内部的编码器更新。
- **global_step 与 epoch**：PyTorch Lightning 中 `global_step` 是优化器更新次数的全局计数，`current_epoch` 是当前轮数。threestudio 所有「随训练进度变化」的逻辑都以这两个数为输入。
- **课程式（渐进式）训练**：一开始用低分辨率/低频细节/窄视角训练，随步数推进逐步放开。它能稳定早期优化，是 DreamCraft3D 四阶段之外另一维度的「由粗到细」。

上一讲（u3-l1）你已经掌握了注册机制：`find(X_type)(cfg.X)` 拿注册类 + 参数 dict 构造对象。本讲就回答：**这个构造过程内部发生了什么，以及构造出来的对象如何在训练中被持续「刷新」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/utils/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py) | 本讲主角。定义 `Configurable`、`Updateable`、`BaseObject`、`BaseModule` 四个基类与 `update_if_possible` 两个工具函数，全文件不到 120 行 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | 支撑工具：`get_device`、`load_module_weights`（含 epoch/global_step 的提取）、步数调度函数 `C()` |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | 消费现场。`BaseSystem` 在 Lightning 的各 batch 钩子里调用 `update_if_possible` / `do_update_step`，把更新分发下去 |
| [threestudio/data/uncond.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py) | 渐进式训练实例一：随机相机的分辨率里程碑与渐进视角 |
| [threestudio/models/networks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py) | 渐进式训练实例二：`ProgressiveBandHashGrid` 按步数解锁哈希编码层级 |
| [threestudio/models/guidance/deep_floyd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py) | 渐进式训练实例三：引导模型按步数调度扩散时间步区间 |

## 4. 核心概念与源码讲解

### 4.1 Configurable：yaml dict 如何变成强类型配置

#### 4.1.1 概念说明

threestudio 里每个可配置组件都遵守同一个约定：类内定义一个 `Config` dataclass，构造函数收一个 dict（来自 yaml），把它转成结构化配置存到 `self.cfg`。这一步带来三个好处：

1. **校验**：yaml 里拼错键名、写错类型，在构造瞬间就报错，而不是训练到一半才崩。
2. **默认值**：yaml 只需写少量键，其余用 dataclass 默认值补齐。
3. **类型提示**：子类写 `cfg: Config` 注解后，IDE 能对 `self.cfg.xxx` 自动补全。

#### 4.1.2 核心流程

```text
yaml 中的 X: {...}（dict）
        │
        ▼
find(X_type)(cfg.X)          # 注册机制（上一讲）
        │
        ▼
parse_structured(self.Config, cfg)
        │  即 OmegaConf.structured(self.Config(**cfg))
        ▼
self.cfg：带 schema 的强类型配置对象
```

#### 4.1.3 源码精读

`Configurable` 是最小实现，只有两行逻辑：

[threestudio/utils/base.py:11-18](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L11-L18) —— 定义内嵌空 `Config`，构造函数把传入 dict 经 `parse_structured` 转成 `self.cfg`。

`parse_structured` 本体只有两行：

[threestudio/utils/config.py:129-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L129-L131) —— 先用 `fields(**cfg)` 把 dict 展开成 dataclass 实例（多余键在这里直接触发 `TypeError`），再包成 OmegaConf structured 对象。

注意一个细节：`Configurable.__init__` 里调用了 `super().__init__()`，而 `Configurable` 自身没有父类（默认继承 object）。这是为多继承服务的——后面 `BaseModule(nn.Module, Updateable)` 通过 MRO 保证各父类构造函数都被正确执行。

#### 4.1.4 代码实践

写一个 10 行脚本（示例代码），直观感受 parse_structured 的校验行为：

```python
# test_parse.py（示例代码）
from dataclasses import dataclass
from threestudio.utils.config import parse_structured

@dataclass
class Config:
    color: tuple = (1.0, 1.0, 1.0)
    learned: bool = False

print(parse_structured(Config, {"color": (0.5, 0.5, 0.5)}))   # 正常：补默认值 learned=False
print(parse_structured(Config, None))                          # 正常：全用默认值
print(parse_structured(Config, {"colour": (1, 1, 1)}))         # 报错：键名拼错，立即暴露
```

1. 实践目标：验证「多余/拼错的键在构造瞬间报错」。
2. 操作步骤：在仓库根目录运行 `python test_parse.py`（前三行如报 tinycudann 导入错误，说明环境未装全，可先完成 u1-l2）。
3. 需要观察的现象：前两次打印出结构化配置；第三次抛出带 `unexpected keyword` 字样的 TypeError。
4. 预期结果：与描述一致。若 tinycudann 未安装导致 `import threestudio` 失败，可直接把 `parse_structured` 的两行逻辑抄进脚本本地复现（只依赖 omegaconf）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `parse_structured(Config, None)` 不报错？
答案：`fields(**cfg)` 展开成 `fields()`，所有字段走 dataclass 默认值；这也是为什么配置里可以整个省略某个 `X:` 段。

**练习 2**：如果子类想给配置加字段，`Config` 应该怎么写？
答案：继承父类的 Config，如 `class Config(ParentClass.Config): new_field: int = 0`。参考 `BaseLift3DSystem.Config(BaseSystem.Config)`（[threestudio/systems/base.py:213](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L213)）。

### 4.2 Updateable：update_step 钩子与递归分发

#### 4.2.1 概念说明

神经网络的参数由优化器更新，但训练中还有大量「非参数状态」需要随步数刷新：当前用哪个分辨率、哈希编码解锁到第几层、扩散模型采样哪个时间步区间、背景颜色渐变到什么程度……这些状态不在 `state_dict` 里，必须有人在每个训练步通知它们「现在是第几步」。`Updateable` 就是这套通知协议：

- `update_step(epoch, global_step, on_load_weights)`：批次**开始前**调用（下文 4.4 详述时机）。
- `update_step_end(epoch, global_step)`：批次**结束后**调用。
- `do_update_step` / `do_update_step_end`：框架侧入口，先递归更新所有 `Updateable` 属性，最后调用自身的 `update_step`。

#### 4.2.2 核心流程

`do_update_step` 的递归遍历逻辑：

```text
do_update_step(epoch, global_step):
    for attr in self.__dir__():            # 遍历所有属性名
        if attr 以 "_" 开头: continue        # 跳过私有属性
        module = getattr(self, attr)        # 取属性（取不到就跳过）
        if module 是 Updateable:
            module.do_update_step(...)      # 先递归：子组件更新
    self.update_step(...)                   # 后自己：本组件更新
```

这是一个**后序遍历**：叶子组件（如最内层的编码器）先收到通知，容器组件后收到。整棵「组件树」共用一次遍历，无需每个容器自己记得转发。

#### 4.2.3 源码精读

[threestudio/utils/base.py:22-36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L22-L36) —— `do_update_step`：`__dir__()` 列出全部属性名，跳过 `_` 开头的；`try/except` 包住 `getattr` 是为了跳过 property 等取值会出错的属性；对每个 `Updateable` 子组件递归，最后调用自身的 `update_step`。

[threestudio/utils/base.py:38-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L38-L48) —— `do_update_step_end` 与上面完全同构，只是末端调用 `update_step_end`。

[threestudio/utils/base.py:50-57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L50-L57) —— 两个默认空实现。注意注释里的警告：`on_load_weights=True` 时模型张量**不保证在同一设备上**，此时只应恢复纯数值状态，不要做涉及设备运算的评估。

[threestudio/utils/base.py:60-67](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L60-L67) —— `update_if_possible` / `update_end_if_possible`：给「不确定对方是否 Updateable」的调用方用的安全包装，`isinstance` 判断后才调用。dataset 就不是 `BaseModule`，但只要它混入了 `Updateable` 就能被刷新。

三个容易踩的坑：

1. 属性名以 `_` 开头的子组件**收不到更新**。所以源码里共享组件都存成 `self.geometry`、`self.renderer` 这类公开名。
2. 遍历的是「当前时刻」的属性，动态挂上去的属性下一步才会被遍历到。
3. 同一对象被两个容器同时引用会被更新两次——源码中 `MeshExporter` 与 system 各持一份 geometry/material/background，但导出走 `trainer.predict`，每个 batch 只刷一次，且导出组件的 `update_step` 多为幂等赋值，不会出错。

#### 4.2.4 代码实践

不改任何源码，先用纯 Python 验证递归分发顺序（示例代码）：

```python
# test_updateable.py（示例代码）
from threestudio.utils.base import Updateable

class Leaf(Updateable):
    def __init__(self, name): self.name = name
    def update_step(self, epoch, global_step, on_load_weights=False):
        print(f"leaf {self.name} @ step {global_step}")

class Node(Updateable):
    def __init__(self):
        self.child_a = Leaf("a")   # 公开名：会被遍历
        self._child_b = Leaf("b")  # 下划线开头：被跳过
    def update_step(self, epoch, global_step, on_load_weights=False):
        print(f"node @ step {global_step}")

Node().do_update_step(0, 100)
```

1. 实践目标：亲眼看到「叶子先于容器」与「下划线属性被跳过」。
2. 操作步骤：`python test_updateable.py`。
3. 需要观察的现象：输出顺序为 `leaf a` → `node`，`leaf b` 永远不出现。
4. 预期结果：与描述一致。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `do_update_step` 用 `self.__dir__()` 而不是 `self.__dict__`？
答案：`__dir__` 还覆盖 property、以及定义在类上的属性；更重要的是它对 `nn.Module` 也能列出通过 `register_buffer`/`register_parameter` 注册的成员，比 `__dict__` 覆盖面广。

**练习 2**：如果 `getattr` 抛异常会怎样？
答案：被裸 `except` 吞掉、`continue`，遍历继续——这是一个刻意的宽容设计，注释里点名了 property 这类取值即求值的属性。

**练习 3**：`update_step` 和 Lightning 自带的 `on_train_batch_start` 有何区别？
答案：前者是 threestudio 自己的协议，任何 `Updateable`（包括不是 `nn.Module` 的 dataset、编码器）都能实现；后者只有 `LightningModule`（即 system 一个对象）能实现。system 在自己的 `on_train_batch_start` 里把步数「翻译」给整棵组件树（见 4.4）。

### 4.3 BaseObject 与 BaseModule：四步生命周期

#### 4.3.1 概念说明

`BaseObject` 与 `BaseModule` 是所有 threestudio 组件的两个构造模板。`BaseModule` 额外继承 `nn.Module`（有参数、能存盘），geometry/material/background/renderer/guidance 全是 `BaseModule` 子类；`BaseObject` 则用于不需要参数的轻量对象。`BaseModule` 的构造函数固定走四步：

```text
① parse_structured(self.Config, cfg)   解析并校验配置
② get_device()                          绑定当前进程的 GPU
③ self.configure(...)                   子类在此组装网络结构
④ 若 cfg.weights 非空：
     load_module_weights 提取 state_dict + epoch + global_step
     → load_state_dict 装载参数
     → do_update_step(epoch, global_step, on_load_weights=True)
                                          用检查点里的步数恢复渐进状态
```

第 ④ 步是点睛之笔：**权重热启动时，渐进状态必须同步恢复到对应步数**，否则从 5000 步的检查点续训却用 0 步的分辨率/编码层级，训练会突然退化。

#### 4.3.2 核心流程

`configure` 与 `__init__` 的分工是模板方法模式的典型应用：

```text
BaseModule.__init__（固定流程，不可变）
    └── self.configure(*args, **kwargs)（子类覆写，自由组装）
```

子类从不覆写 `__init__`，只覆写 `configure`——这保证了「配置解析 → 设备绑定 → 权重加载」的顺序在全项目 40 多个组件中完全一致。

#### 4.3.3 源码精读

[threestudio/utils/base.py:89-102](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L89-L102) —— `BaseModule` 的头部与前两步：`nn.Module` 与 `Updateable` 多继承；`Config` 只声明一个可选 `weights` 字段（格式 `path/to/ckpt:module_name`）；构造函数完成解析配置、绑设备、调 `configure`。

[threestudio/utils/base.py:103-115](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L103-L115) —— 第 ④ 步与 `_dummy` 缓冲区。`weights` 用冒号切成路径与模块名；`load_module_weights` 返回三元组，装完参数立刻 `do_update_step(..., on_load_weights=True)` 恢复步数相关状态；最后注册一个零元素的 `_dummy` buffer（`persistent=False`，不进 state_dict），仅用于标记模型所在设备。

[threestudio/utils/base.py:117-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L117-L118) —— 默认空 `configure`，子类覆写点。

配套的两个 misc 工具：

[threestudio/utils/misc.py:28-29](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L28-L29) —— `get_device` 返回 `cuda:rank`，rank 来自环境变量（`LOCAL_RANK` 优先），单卡时就是 `cuda:0`。这就是每个组件构造时绑定的设备。

[threestudio/utils/misc.py:32-62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L32-L62) —— `load_module_weights`：从检查点取 `state_dict`，按 `module_name` 前缀过滤（正则剥掉前缀）或按 `ignore_modules` 排除；末尾把检查点里存的 `epoch` 和 `global_step` 一并返回——正是这两个返回值让第 ④ 步的「恢复渐进状态」成为可能。

`BaseObject` 是同样四步去掉权重加载的精简版：

[threestudio/utils/base.py:70-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L70-L86) —— `BaseObject(Updateable)`：解析配置 → 绑设备 → `configure`，没有 weights 字段。

再看一个 system 侧的对照（system 不是 `BaseModule`，但同样遵守「加载权重后恢复步数状态」的契约）：

[threestudio/systems/base.py:50-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L50-L56) —— `BaseSystem.load_weights`：`load_state_dict(strict=False)` 装载后，同样调用 `self.do_update_step(epoch, global_step, on_load_weights=True)`。这就是 u1-l4 说过「阶段间用 `system.weights` 热启动时分辨率等状态无缝衔接」的底层原因。

#### 4.3.4 代码实践

源码阅读型实践——追踪 `weights` 的完整数据流：

1. 实践目标：把「检查点里的 global_step」到「组件渐进状态」的通路走一遍。
2. 操作步骤：
   - 打开 [configs/dreamcraft3d-coarse-neus.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml)，找到 `system.weights`，其值形如 `path/to/nerf/ckpts/last.ckpt:system`；
   - 顺着冒号后面的 `system` 模块名，读 [threestudio/utils/misc.py:54-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L54-L60) 的正则过滤逻辑，确认只有 `system.` 前缀的键被保留；
   - 再读 [threestudio/systems/base.py:56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L56)，确认装载后立刻用检查点的 `epoch/global_step` 触发一次全树更新。
3. 需要观察的现象：纸面上画出 `ckpt["global_step"]` → `load_weights` → `do_update_step` → `RandomCameraIterableDataset.update_step`（换分辨率）与 `ProgressiveBandHashGrid.update_step`（解锁层级）两条传播路径。
4. 预期结果：两条路径都在 `do_update_step` 的递归遍历中汇合，且发生在任何训练 batch 之前。

#### 4.3.5 小练习与答案

**练习 1**：`_dummy` buffer 为什么设 `persistent=False`？
答案：不写入 state_dict，避免污染检查点；它唯一的作用是「标记模型状态」，让框架能以极小代价探测模块是否已搬到某设备。

**练习 2**：`BaseModule.__init__` 里加载权重为什么用 `map_location="cpu"`？
答案：先统一载入 CPU 再由 Lightning 的正常流程搬到 GPU，避免「检查点在哪张卡就被钉在哪张卡」，兼容多卡与无卡环境（源码注释也提醒此时设备不保证一致，故 `on_load_weights=True`）。

**练习 3**：`load_module_weights` 的 `module_name` 与 `ignore_modules` 为什么互斥？
答案：[misc.py:35-36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L35-L36) 显式 raise——一个是白名单（只留某前缀），一个是黑名单（去掉若干前缀），同时设置语义矛盾。

### 4.4 触发时机：BaseSystem 把 Lightning 钩子翻译成 update_step

#### 4.4.1 概念说明

`Updateable` 定义了协议，但谁在生产中调用它？答案是 `BaseSystem`——它是唯一被 Lightning Trainer 直接驱动的对象，由它把 Lightning 的 batch 钩子「翻译」成整棵组件树的更新。触发点共五处（train/validate/test/predict 的 batch start 与 batch end），规律统一：

- **batch 开始前**：`update_if_possible(self.dataset, ...)` 刷新数据集（换分辨率等），再 `self.do_update_step(...)` 刷新整棵组件树。
- **batch 结束后**：`update_end_if_possible(self.dataset, ...)` + `self.do_update_step_end(...)`。

为什么数据集要单独刷？因为 dataset 不是 system 的属性，不在 `do_update_step` 的遍历范围内，必须显式调用；而 `update_if_possible` 的 `isinstance` 检查让「数据集没实现 Updateable」也不会报错。

#### 4.4.2 核心流程

一个训练 batch 的完整时序：

```text
Lightning Trainer
 └─ BaseSystem.on_train_batch_start(batch, batch_idx)     ← 批次开始前
     ├─ preprocess_data(batch, "train")
     ├─ update_if_possible(self.dataset, epoch, global_step)   ① 数据集先更新（换分辨率）
     └─ self.do_update_step(epoch, global_step)                ② 组件树更新（叶子→根）
 └─ BaseSystem.training_step(batch, batch_idx)             ← 真正的前向/反向（子类实现）
 └─ BaseSystem.on_train_batch_end(outputs, batch, batch_idx)   ← 批次结束后
     ├─ update_end_if_possible(self.dataset, epoch, global_step)
     └─ self.do_update_step_end(epoch, global_step)
```

关键点：`update_step` 发生在 `training_step` **之前**，所以本批次前向用的已经是「最新档位」的分辨率/编码层级/时间步区间。另外 `on_load_weights=True` 的调用只出现在 4.3 的权重加载路径，训练循环里永远是 `False`。

还有一处细节：传给钩子的是 `true_global_step` 而非裸 `global_step`：

[threestudio/systems/base.py:69-74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L74) —— 非训练模式（如 `--export`）下 Lightning 的 `global_step` 是 0，`true_global_step` 用 `set_resume_status` 记录的检查点步数顶上，保证「按步数调度」的逻辑在导出/评估时也拿到正确数值。

#### 4.4.3 源码精读

[threestudio/systems/base.py:174-178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L174-L178) —— 训练批次开始：`preprocess_data` → `update_if_possible(dataset)` → `do_update_step`。这一行就是本讲实践任务要打断点的位置。

[threestudio/systems/base.py:114-119](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L114-L119) —— 训练批次结束：`update_end_if_possible(dataset)` → `do_update_step_end`。要区分清楚：`on_train_batch_end`（Lightning 钩子，触发 `update_step_end`）与 `update_step_end`（threestudio 钩子）不是一回事。

[threestudio/systems/base.py:180-196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L180-L196) —— validate/test/predict 三个阶段的 batch start 与训练完全同构，只是数据集来源不同（`val_dataloaders` / `test_dataloaders` / `predict_dataloaders`）。也就是说**评估和导出时组件同样会被刷新**，这一细节 u2-l4 讲过的「导出时 mesh-exporter 共享 system 组件」正依赖它。

[threestudio/systems/base.py:198-199](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L198-L199) —— `BaseSystem.update_step` 默认空实现，具体 system（如 dreamcraft3d）可覆写。

#### 4.4.4 代码实践

用「加日志」的方式观察触发链（不改源码的替代方案）：

1. 实践目标：记录每个训练步传给 `update_step` 的 epoch 与 global_step。
2. 操作步骤（二选一）：
   - 有 IDE 调试器：在 [threestudio/systems/base.py:178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L178) 的 `self.do_update_step(...)` 一行打断点，以 `python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train --max_steps 12` 等短训练启动（`--max_steps` 经 trainer 段覆盖，具体键以 parsed.yaml 为准），每停一次记录 `(epoch, global_step)`；
   - 不想动源码：直接复用 4.2.4 的 `test_updateable.py` 思路，把打印格式改成 `f"epoch={epoch} step={global_step}"`，手动循环 `for s in range(12): Node().do_update_step(0, s)` 模拟时序。
3. 需要观察的现象：`global_step` 从 0 严格递增 1；第 0 个 epoch 内 epoch 恒为 0；`update_step` 在每次 `training_step` 之前各触发一次（整棵树一次递归完成，不是每组件一次遍历）。
4. 预期结果：日志共 12 组，步数连续。短训练需要 GPU 与全套环境；无环境时用模拟方案，现象一致。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果把分辨率切换逻辑写进 `update_step_end` 而不是 `update_step`，会有什么后果？
答案：切换会晚一个批次生效——`update_step_end` 在 `training_step` 之后才跑，当前批次已经用旧分辨率做完了前向。

**练习 2**：为什么 validate/test/predict 也要刷组件树？
答案：评估与导出需要与训练一致的渐进状态（如正确的哈希层级、正确的引导时间步区间），否则渲染出的结果与训练中看到的不一致。

**练习 3**：`update_if_possible(self.dataset, ...)` 若删掉会发生什么？
答案：数据集（含随机相机的分辨率里程碑、渐进视角）不再被刷新，`height/width` 永远停在初始档——因为 dataset 不是 system 的属性，`do_update_step` 的属性遍历够不到它。

### 4.5 渐进式训练：同一机制的三种应用

#### 4.5.1 概念说明

`Updateable` + `C()` 调度（u2-l2 讲过 `C()` 的四元组插值；本讲从「谁在何时调用它」的角度收口）构成了 DreamCraft3D 全部渐进行为的骨架。三个代表性应用：

| 应用 | 组件 | 刷新的内容 | 配置来源 |
| --- | --- | --- | --- |
| 分辨率爬坡 | `RandomCameraIterableDataset` | `height/width/batch_size` 档位 | `resolution_milestones: [3000]` |
| 编码层级解锁 | `ProgressiveBandHashGrid` | 哈希编码 `mask` 与 `current_level` | `start_level/start_step/update_steps` |
| 扩散时间步区间 | `deep-floyd-guidance` 等 | `min/max_step_percent` | `C()` 四元组 |

#### 4.5.2 核心流程

编码层级解锁的数学含义：掩码从 0 逐步抬到 1，等价于给高频信号一个「渐进打开」的窗口。以 `ProgressiveBandFrequency` 为例，第 \(k\) 个频带的掩码为

\[ 
\text{mask}_k = \frac{1 - \cos\!\big(\pi \cdot \mathrm{clip}(\frac{s}{S} \cdot K - k,\ 0,\ 1)\big)}{2},\quad k=0,\dots,K-1 
\]

其中 \(s\) 是当前 global_step，\(S\) 是 `n_masking_step`（解锁总步数），\(K\) 是频带数。低频带（小 \(k\)）先到 1，高频带随后；这保证网络先学低频轮廓再学高频细节，避免高频噪声在训练早期主导优化。

#### 4.5.3 源码精读

**实例一：分辨率爬坡。**

[threestudio/data/uncond.py:106-116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L106-L116) —— `RandomCameraIterableDataset.update_step`：用 `bisect_right(resolution_milestones, global_step) - 1` 定位当前档位，更新 `height/width/batch_size` 与对应的单位焦距射线方向表，再调 `progressive_view`。构造时预先生成所有档位的 `directions_unit_focals`（[uncond.py:93-96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L93-L96)），切换只是换引用，零重复计算。

[threestudio/data/uncond.py:122-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L122-L131) —— `progressive_view`：\(r=\min(1, s/(P+1))\)（\(P\) 为 `progressive_until`），仰角/方位角范围从「参考视角附近」线性插值到完整范围——训练初期只看正面，逐步扩大到环绕全周，缓解多面不一致。

coarse-nerf 配置里的对应数值：[configs/dreamcraft3d-coarse-nerf.yaml:19-30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L19-L30) —— 随机相机 `[128,384]` 两档、里程碑 3000 步、`progressive_until: 200`。

单图数据集侧的同构实现：[threestudio/data/image.py:239-251](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L239-L251) —— `update_step_` 同样用 bisect 换档，并重新加载对应分辨率的参考图；[image.py:283-285](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L283-L285) —— 外层 `update_step` 先刷自己再刷内嵌的 `random_pose_generator`，手工完成一次两级分发（这是「容器手动转发」的例子，因为 `random_pose_generator` 是构造函数里自管的属性而非 dataset 的公开子组件树的一部分）。

**实例二：哈希编码层级解锁。**

[threestudio/models/networks.py:129-151](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L129-L151) —— `ProgressiveBandHashGrid(Updateable)`：包一层 tiny-cuda-nn 的 HashGrid，维护 `current_level` 与全零 `mask`。

[threestudio/models/networks.py:158-167](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L158-L167) —— `update_step` 按步数线性抬升 `current_level`（每 `update_steps` 步升一级，封顶 `n_level`），并把掩码前 `current_level * n_features_per_level` 个元素置 1；前向（[networks.py:153-156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L153-L156)）用该掩码屏蔽未解锁层级的特征。

coarse-nerf 配置对应：[configs/dreamcraft3d-coarse-nerf.yaml:71-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L71-L73) —— `start_level: 8`（先解锁到约 200 分辨率的层级）、`start_step: 2000`、`update_steps: 500`：前 2000 步只用粗层级，之后每 500 步放一层细网格。

**实例三：扩散时间步区间调度。**

[threestudio/models/guidance/deep_floyd_guidance.py:490-500](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L490-L500) —— `update_step` 每步用 `C()` 重新计算梯度裁剪阈值与 `min/max_step_percent`（扩散时间步采样区间），再 `set_min_max_steps` 生效。SDS 蒸馏里「先大噪声塑形、后小噪声修细节」的课程正是靠这里驱动（原理将在 u7-l2 展开）。

顺带一提非递归的手动刷新：[threestudio/models/networks.py:24-27](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L24-L27) —— `ProgressiveBandFrequency` 在构造函数里主动调 `self.update_step(None, None)` 初始化掩码，因为构造时刻还不在训练循环里，必须自举一次。

#### 4.5.4 代码实践

参数观察实践（无需 GPU 的部分可选做）：

1. 实践目标：理解 `start_step/update_steps` 对层级解锁节奏的控制。
2. 操作步骤：
   - 读 [configs/dreamcraft3d-coarse-nerf.yaml:66-73](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L66-L73) 的 `pos_encoding_config`，手算各步数的 `current_level`：`level(s) = min(8 + max(s-2000,0)//500, 16)`；
   - 用示例脚本验证：

```python
# test_level.py（示例代码）
def level(s, start_level=8, start_step=2000, update_steps=500, n_level=16):
    return min(start_level + max(s - start_step, 0) // update_steps, n_level)

for s in [0, 1999, 2000, 2500, 3000, 6000, 99999]:
    print(s, level(s))
```

   - 把配置中 `update_steps` 改成 250（命令行 `system.geometry.pos_encoding_config.update_steps=250` 覆盖，不必改 yaml），预期解锁节奏加快一倍。
3. 需要观察的现象：脚本输出 0/1999 步都是 8 级，2000 步起每 500 步升 1 级，6000 步后封顶 16 级。
4. 预期结果：与手算一致；改参数后的训练差异需 GPU 短跑观察，属可选项。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：分辨率换档为什么用 `bisect_right(...) - 1` 而不是线性查找？
答案：里程碑列表有序，二分查找 \(O(\log n)\)；且 `-1` 后第一个档位（里程碑设为 `-1`，见 [uncond.py:91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L91)）覆盖所有小于首个里程碑的步数。

**练习 2**：`progressive_view` 中 `progressive_until: 0` 会怎样？
答案：\(r = \min(1, s/1)\)，第 1 步起 \(r=1\)，视角范围立即取满——即关闭渐进视角，从头全范围采样。

**练习 3**：`guidance.update_step` 里为什么不直接在 `training_step` 里每步算 `C()`？
答案：也可以，但 `update_step` 把「步数→超参」的重算集中到一处、与渲染前向解耦；同时 `on_load_weights=True` 的恢复路径也复用同一入口，保证热启动时区间立刻正确。

## 5. 综合实践

**任务：给上一讲的渐变背景加上「按步数渐变颜色」，并验证更新链路。**

背景：u3-l1 的综合实践里你已经用 `custom_import` 注册了一个垂直渐变背景。本实践让它「动起来」——从第 2000 步起，背景颜色随 global_step 从白色线性过渡到深蓝，过渡期 1000 步。

**步骤（示例代码，基于 u3-l1 的 `vertical-gradient-background` 扩展）：**

1. 在你的扩展模块（例如 `my_ext/gradient_background.py`）中给类加上 `update_step`：

```python
# my_ext/gradient_background.py（示例代码）
import torch
import threestudio
from threestudio.models.background.base import BaseBackground

@threestudio.register("vertical-gradient-background")
class VerticalGradientBackground(BaseBackground):
    @dataclass
    class Config(BaseBackground.Config):
        n_output_dims: int = 3
        color_top: tuple = (1.0, 1.0, 1.0)   # 起始：白
        color_bottom: tuple = (0.05, 0.1, 0.3) # 终止：深蓝
        start_step: int = 2000
        end_step: int = 3000

    def configure(self) -> None:
        self.register_buffer("top", torch.as_tensor(self.cfg.color_top, dtype=torch.float32))
        self.register_buffer("bottom", torch.as_tensor(self.cfg.color_bottom, dtype=torch.float32))
        self.progress = 0.0   # 渐变进度，update_step 里刷新

    def update_step(self, epoch, global_step, on_load_weights=False):
        if on_load_weights:
            return  # 设备未定，只跳过；数值状态由 global_step 每步重算，天然可恢复
        self.progress = min(max((global_step - self.cfg.start_step) /
                                (self.cfg.end_step - self.cfg.start_step), 0.0), 1.0)

    def forward(self, dirs):
        t = torch.linspace(0, 1, dirs.shape[1], device=dirs.device).view(1, -1, 1, 1)
        top = self.top * (1 - self.progress) + ...   # 按 self.progress 在两组颜色间插值
        return ...                                   # 其余同 u3-l1 的实现
```

（`forward` 的完整插值请补全：本质是对 `color_top/color_bottom` 各自乘 `(1-p)/p` 后再生成渐变；关键是把 `self.progress` 当作唯一随步数变化的量。）

2. 在 coarse-nerf 配置上启用（命令行覆盖即可）：

```bash
python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
    --custom_import my_ext.gradient_background \
    --background_type vertical-gradient-background \
    --max_steps 3200   # 具体键名以 parsed.yaml 的 trainer 段为准
```

3. 观察两点：
   - `outputs/<name>/<tag>/save/` 下的训练渲染图：前 2000 步背景保持起始色，2000–3000 步逐渐变深，之后稳定在终止色；
   - 结合 4.4.4 的日志/断点，确认每次 `on_train_batch_start` 传进来的 `global_step` 与渲染图文件名里的 `it{N}` 一致。

4. 思考题（呼应 4.3）：把这个背景的 `update_step` 改成在 `forward` 里实时根据一个外部传入的 step 计算，会发生什么？
   答案：功能上可能等价，但会失去「`on_load_weights` 恢复」「与框架统一调度」的好处，且每前向多一次分支计算；更重要的是脱离了 threestudio 的统一协议，导出/评估路径（predict 阶段的 batch start 也会刷）就拿不到正确状态。

> 说明：本实践需要完整 GPU 环境与短训练（约 3200 步），渲染图颜色演变属「待本地验证」；不依赖 GPU 的部分（注册、构造、`do_update_step` 手动调用打印 `progress`）可用 4.2.4 的方式先行验证。

## 6. 本讲小结

- `Configurable`/`BaseObject`/`BaseModule` 用内嵌 `Config` dataclass + `parse_structured` 实现「dict 进、强类型配置出」，配置错误在构造瞬间暴露。
- `BaseModule` 的生命周期固定四步：解析配置 → 绑设备 → `configure` 组装 → 可选加载权重并用检查点的 `epoch/global_step` 调 `do_update_step(on_load_weights=True)` 恢复渐进状态。
- `Updateable` 是递归更新协议：`do_update_step` 后序遍历所有公开的 `Updateable` 属性（叶子先更新），子类只需覆写 `update_step`/`update_step_end`。
- 触发时机由 `BaseSystem` 决定：各阶段 batch start 先刷 dataset 再刷整棵组件树（`update_step`），batch end 刷 `update_step_end`；评估与导出同样会被刷新。
- DreamCraft3D 的所有渐进行为——分辨率里程碑、渐进视角、哈希编码层级解锁、扩散时间步区间——都是「`update_step` 收到 `(epoch, global_step)` + `C()`/bisect 计算新档位」这同一个模式的应用。

## 7. 下一步学习建议

本讲搞定了「单个组件的构造与生命周期」。下一讲 **u3-l3 BaseSystem 与 BaseLift3DSystem：三维系统的组装**将视角上升到 system 层：`BaseLift3DSystem.configure` 如何用 `find` 把 geometry/material/background/renderer 四类组件拼装成可训练系统、`geometry_convert_from` 如何跨阶段转换几何表示、`parse_optimizer` 如何按模块名分配学习率。建议预先浏览 [threestudio/systems/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py) 与 [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) 的 `configure` 部分，并留意其中对 `update_step` 的又一次覆写——那正是本讲协议在真实系统里的落点。
