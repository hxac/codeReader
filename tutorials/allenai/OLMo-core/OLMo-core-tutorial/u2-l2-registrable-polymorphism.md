# u2-l2 Registrable 与多态配置字段

## 1. 本讲目标

上一讲（u2-l1）我们搞清楚了 `Config` 基类：一切配置都是可序列化的嵌套 dataclass，`as_config_dict()` 会注入 `_CLASS_` 字段，`from_dict()` 靠动态 import 还原确切子类。

但留了一个问题：**用户手写 YAML 时，凭什么要写 Python 的完整类路径？** 只想换个优化器，难道要写 `olmo_core.optim.adamw.AdamWConfig` 吗？

本讲回答这个问题。读完你应当能够：

1. 理解 `Registrable` 注册机制：`@基类.register("名字")` 如何把子类登记进一本「类注册簿」，以及 `type` 字段如何在反序列化时按名字查表选子类。
2. 解释优化器、调度器、attention 后端、data loader 为什么都设计成**多态 Config 字段**（字段类型标注为基类，实际可装任何注册子类）。
3. 动手为自定义子类注册名称，并通过 YAML 的 `type` 字段选择它、用 `build()` 得到对应实例。

> 说明：本讲 worker 环境未执行 Python，所有「预期结果」均为依据源码推演的结论，**待本地验证**。

## 2. 前置知识

承接 u2-l1 的结论（不重复展开）：

- `Config` 基类（`src/olmo_core/config.py`）：子类加 `@dataclass` 即免费获得序列化、嵌套组合、dotlist 覆盖能力。
- `as_config_dict()`：注入 `_CLASS_`（完整模块路径）的 JSON 安全字典，是官方存档格式。
- `from_dict()`：按 `_CLASS_` 动态 import 并还原确切子类，保证 round-trip 等价。
- 基类没有 `save()` 方法，落盘需自行把 `as_config_dict()` 的结果写文件。

本讲的新术语，用大白话解释：

| 术语 | 通俗解释 |
| --- | --- |
| 注册表（registry） | 一本「名字 → 类」的字典。登记过名字的类，之后可以只凭名字找到它。 |
| mixin（混入） | 一个只提供额外能力、不充当「主体基类」的类。`Registrable` 就是一个 mixin，把它和 `Config` 一起继承，就给配置类加装了「可注册」能力。 |
| 多态（polymorphism） | 字段的类型标注写的是基类，运行时实际装的可以是任何子类，调用方法时自动用子类的实现。 |
| `type` 字段 | 序列化输出里记录注册名的键，字面键名就是字符串 `"type"`。 |

一句话直觉：**`_CLASS_` 是「精确门牌号」（import 路径），`type` 是「花名」（注册名）。反序列化时优先按门牌号找人，找不到再按花名查通讯录。**

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `src/olmo_core/config.py` | `Registrable` / `decode` 的导入与再导出；`type` 的写入与两条还原路径 |
| `src/olmo_core/optim/config.py` | `OptimConfig`：混入 `Registrable` 的代表基类，`build()` 消费注册子类 |
| `src/olmo_core/optim/adamw.py` | `AdamWConfig` 注册为 `"adamw"` 的标准样例 |
| `src/olmo_core/optim/adam.py` | `AdamConfig`：最精简的注册样例 |
| `src/olmo_core/optim/scheduler.py` | `Scheduler` 族的注册（14 个注册名） |
| `src/olmo_core/nn/attention/base.py`、`src/olmo_core/nn/attention/__init__.py` | `SequenceMixerConfig` 族（attention 也注册在这里） |
| `src/olmo_core/data/data_loader.py` | `DataLoaderConfig` 族 |
| `src/olmo_core/train/train_module/transformer/config.py` | 多态字段的消费端（`optim: OptimConfig`） |
| `src/examples/llm/train.py` | 实战中给多态字段赋值 |
| `pyproject.toml` | `dataclass-extensions>=0.3.0` 是核心依赖 |

## 4. 核心概念与源码讲解

本讲对应两个最小模块：**Registrable**（4.1）与**多态反序列化**（4.2），外加一个把知识落地的支撑模块（4.3：多态字段在库中的分布与消费）。

### 4.1 Registrable：给 Config 家族装一本「类注册簿」

#### 4.1.1 概念说明

`Registrable` 不是 OLMo-core 自己实现的，而是来自 AI2 的独立小包 `dataclass-extensions`（在 [pyproject.toml:L22](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L22) 中声明为核心依赖 `dataclass-extensions>=0.3.0`）。

它解决的问题是：

- `_CLASS_` 要求写完整 Python 路径，对手写 YAML 的用户太不友好，而且类一旦重命名/搬家，旧配置全部失效。
- 注册名（如 `adamw`）短、稳定、可读，用户在 YAML 里写一行 `type: adamw` 就能选定实现。
- 允许第三方（比如你自己）把自定义子类注册进同一本注册簿，**不改库源码就能插入新实现**。

用法约定（全库一致）：给基类混入 `Registrable`，然后用装饰器 `@基类.register("名字")` 登记子类。**注册表挂在「被 register 的那个基类」上**，子类自身并没有这本簿子——这一点有官方注释直接佐证，见 4.1.3 最后一条引用。

#### 4.1.2 核心流程

注册的动作可以概括为（伪代码，依据库内用法与 `config.py` 官方注释推断，实际实现见 dataclass-extensions 源码）：

```text
@OptimConfig.register("adamw")
        │
        ▼
OptimConfig 的注册表["adamw"] = AdamWConfig      # 名字 → 类 的映射，记在基类上
```

形式化地说，注册表就是一本字典：

\[ \text{registry}(\text{OptimConfig}) = \{\ \text{name} \mapsto \text{subclass}\ \} \]

于是「按名字找类」就是一次字典查找：`"adamw"` → `AdamWConfig`。

配套的两个关键方法（都在本仓库源码中被直接调用，可验证）：

- `register(name)`：装饰器，把类登记进基类注册表。
- `get_registered_name()`：反查「我这个类登记的名字是什么」，未登记会抛 `ValueError`。

#### 4.1.3 源码精读

**① 导入与再导出。** OLMo-core 在 config 模块里导入并顺手再导出 `Registrable`，方便其他模块统一从 `olmo_core.config` 拿：

- [src/olmo_core/config.py:L24](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L24)：`from dataclass_extensions import Registrable, decode`——本讲两大主角的进口处。
- [src/olmo_core/config.py:L30-L36](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L30-L36)：`__all__` 里包含 `"Registrable"`，注释写明 *re-exported for convenience*。同包导入的 `decode` 函数则是 4.2 节反序列化的执行者。

**② 混入基类。** 以优化器为例，`OptimConfig` 在继承 `Config` 的同时混入 `Registrable`：

- [src/olmo_core/optim/config.py:L56-L60](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/config.py#L56-L60)：`@dataclass class OptimConfig(Config, Registrable, Generic[Opt], metaclass=ABCMeta)`——一个类同时拿到「Config 的序列化能力」和「Registrable 的注册能力」，这正是 mixin 的用法。

**③ 标准注册样例：AdamWConfig。**

- [src/olmo_core/optim/adamw.py:L233-L249](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adamw.py#L233-L249)：装饰器 `@OptimConfig.register("adamw")` 叠在 `@dataclass` 之上，把 `AdamWConfig(OptimConfig[torch.optim.AdamW])` 登记为名字 `adamw`。类体只有 `lr/betas/eps/weight_decay/foreach/fused` 六个超参字段，加一个 `optimizer()` 类方法返回真正的 `torch.optim.AdamW`。
- 最精简的对照样例是 [src/olmo_core/optim/adam.py:L9-L24](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adam.py#L9-L24) 的 `AdamConfig`：25 行以内完成「注册 + 定义超参 + 指定优化器类」三件事，这就是接入一个新优化器的全部样板。
- 同文件里 [src/olmo_core/optim/adamw.py:L252-L291](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adamw.py#L252-L291) 的 `SkipStepAdamWConfig` 注册为 `"skip_step_adamw"`——同一个基类下可以登记任意多个子类，各自独立成条目。

**④ 序列化侧：注册名写进 `type` 键。**

- [src/olmo_core/config.py:L128-L135](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L128-L135)：`as_dict()` 中 `include_registered_name` 分支——对每个 `Registrable` 实例调用 `d.get_registered_name()`，把结果写到 `out["type"]`；`except ValueError: pass` 说明**未注册的类会静默跳过**（比如中间抽象基类从未登记过名字）。
- [src/olmo_core/config.py:L153-L165](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L153-L165)：`as_config_dict()` 默认打开 `include_registered_name=True`，所以官方存档里每个多态节点同时携带 `_CLASS_` 和 `type` 两条线索。

**⑤ 注册表在父类上的官方证据。**

- [src/olmo_core/config.py:L276-L279](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L276-L279)：注释原文——*"Remove the `type` field since the class is already resolved via `_CLASS_`. This avoids a registry lookup on the resolved subclass, whose own `_registry` may be empty (registrations live on the parent class)."* 这段注释一次讲清三件事：(a) `decode` 确实会按 `type` 做注册表查找；(b) 注册表（属性名 `_registry`）挂在父类上；(c) 解析到确切子类后必须先弹掉 `type`，否则会在子类那本空簿子上白找一遍。

#### 4.1.4 代码实践

**实践目标**：数清全库有多少个注册点、分成几族，建立「Registrable 是全库通用模式」的直观印象。

**操作步骤**：

1. 在仓库根目录执行（只读 grep，不运行 Python）：

```bash
grep -rn "\.register(" src/olmo_core | sort
```

2. 把输出按「注册到哪个基类」分组统计。

**需要观察的现象**：输出共 32 行（截至当前 HEAD），可分成：

| 注册目标基类 | 数量 | 代表注册名 |
| --- | --- | --- |
| `OptimConfig` | 9 | `adamw`、`lion`、`muon`、`nor_muon`、`dion`、`noop`… |
| `Scheduler` | 14 | `cos_with_warmup`、`wsd`、`sequential`… |
| `SequenceMixerConfig`（attention 族） | 4 | `attention`、`gated_delta_net`、`kimi_delta_attention`… |
| `DataLoaderConfig` | 2 | `numpy`、`composable` |
| 其他（非 Config 体系） | 3 | HF 的 `AutoConfig.register`×2、`GantryCallback`×1，属于 transformers/gantry 自己的注册机制，与本讲的 Config 体系无关 |

**预期结果**：四族恰好对应「优化器、调度器、注意力后端、数据加载器」四个可插拔子系统——这正是规格里问的「为何它们都设计成多态 Config 字段」的第一层证据。（行数为静态 grep 所得，可本地复现。）

#### 4.1.5 小练习与答案

**练习 1**：`@OptimConfig.register("adamw")` 这个名字登记在哪本注册表里？`AdamWConfig` 自己名下有这本簿子吗？

**答案**：登记在 `OptimConfig` 的注册表里——因为 `register` 是在 `OptimConfig` 上调用的。`AdamWConfig` 自身的 `_registry` 可能为空，依据就是 [src/olmo_core/config.py:L276-L279](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L276-L279) 的官方注释（*registrations live on the parent class*）。

**练习 2**：`as_dict()` 的 `include_registered_name` 分支为什么包着 `try/except ValueError`？

**答案**：因为 `get_registered_name()` 对从未注册过名字的类会抛 `ValueError`，捕获后跳过即可（[src/olmo_core/config.py:L130-L135](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L130-L135)）。这是刻意设计：继承 `Registrable` 的中间基类（如 `MatrixAwareOptimConfig`）不必都有注册名。

**练习 3**：仿照 `AdamConfig`，给 `torch.optim.SGD` 写一个注册为 `"sgd"` 的 `SGDConfig`，最少需要哪几样东西？

**答案**：三样——① `@OptimConfig.register("sgd")` 装饰器；② `@dataclass class SGDConfig(OptimConfig[torch.optim.SGD])`，字段只放 `lr`、`momentum` 等超参；③ 实现 `optimizer()` 类方法返回 `torch.optim.SGD`（模板对照 [src/olmo_core/optim/adam.py:L9-L24](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adam.py#L9-L24)）。`build()` 由 `OptimConfig` 基类代劳。

### 4.2 多态反序列化：一个 `type` 键如何选对子类

#### 4.2.1 概念说明

先定义**多态字段**：类型标注为某个 Registrable 基类的配置字段。例如训练模块配置里 `optim: OptimConfig`——标注是基类，实际可以装 `AdamWConfig`、`LionConfig`、`MuonConfig`……中任何一个。这让「换优化器」从改代码变成换一个子类（或 YAML 里换一个 `type` 字符串）。

反序列化（从 dict/YAML 还原成对象）时有两条路径：

1. **`_CLASS_` 路径（精确还原）**：dict 里有完整类路径 → 动态 import → 直接用该类构造。这是 `as_config_dict()` 往返的主路径。
2. **`type` 路径（注册表查找）**：dict 里只有 `type: adamw` → 在基类注册表里查名字 → 得到子类再构造。这是手写 YAML 的路径。

两条路径的优先级规则（源码可证）：**`_CLASS_` 优先；一旦解析成功，`type` 键会被主动删除**，防止后续在子类那本空注册表上误查（见 4.1.3 ⑤ 的注释）。若 `_CLASS_` 解析失败（比如 import 不到），保留的 `type` 还能兜底走注册表查找。

#### 4.2.2 核心流程

`from_dict()` 的决策流程（对照源码 4.2.3 ①）：

```text
from_dict(data)
│
├─ 若给了 overrides（dotlist）：先 _set_nested 写进 data
│
├─ decode_data(data) 递归下钻：
│     每个 dict 节点——
│       ├─ 含 _CLASS_ 且能 import？ ──► 删掉 type 键，decode(该类, 其余字段)
│       └─ 否则 ──► 原样返回普通 dict（type 键仍在）
│
└─ 顶层结果还不是 cls 的实例？ ──► decode(cls, dict)
                                   └─ dataclass_extensions 在这里按 type
                                      查 cls 的注册表，选子类构造
```

关键点：`decode(cls, data)` 是外部库 `dataclass-extensions` 提供的构造函数（[src/olmo_core/config.py:L24](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L24) 导入）。它「见到 `type` 键就在注册表里查子类」的行为，由 OLMo-core 自己的注释与调用方式佐证（4.1.3 ⑤），其内部实现属于外部库，本讲不深入。

#### 4.2.3 源码精读

**① `from_dict` 全景与 `_CLASS_` 分支。**

- [src/olmo_core/config.py:L239-L258](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L239-L258)：`from_dict` 入口，内部定义 `resolve_cls`（把 `_CLASS_` 的模块路径动态 import 成类）与 `decode_data`（递归遍历）。
- [src/olmo_core/config.py:L258-L289](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L258-L289)：`decode_data` 的核心分支——先递归处理所有子节点构造 `new_dict`；若节点带 `_CLASS_` 且解析成功，则应用 `_IGNORE_FIELDS` 兼容规则、**`new_dict.pop("type", None)` 删掉 type 键**，最后 `decode(cls_o, new_dict)` 构造实例。构造失败会抛带字段路径的 `OLMoConfigurationError`，方便定位是哪个嵌套字段坏了。
- [src/olmo_core/config.py:L297-L305](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L297-L305)：先应用 dotlist `overrides`，再对顶层结果做收尾——`isinstance(decoded, cls)` 不满足就 `decode(cls, decoded)`。**手写 YAML（只有 `type` 没有 `_CLASS_`）的场景正是从这里进入注册表查找的**。

**② `merge()` 为什么也带注册名。**

- [src/olmo_core/config.py:L197-L225](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L197-L225)：`merge()` 走「`as_dict(include_class_name=True, include_registered_name=True)` 转 dict → `_set_nested` 改值 → `from_dict` 重建」路线。转换时同时带上 `_CLASS_` 和 `type` 两条线索：`_CLASS_` 解析成功时优先；万一解析失败（如类被改名搬走），保留的 `type` 仍能让 `decode` 走注册表兜底。这也是下一讲 u2-l3 命令行覆盖能「改多态字段内部超参」的基础。

**③ YAML/JSON 入口。**

- [src/olmo_core/config.py:L317-L327](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L317-L327)：`from_yaml` / `from_json` 只是「读文件成 dict → `from_dict`」，不做任何额外的类型魔法——多态还原完全发生在 `from_dict` 里。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：自定义一个注册到 `OptimConfig` 的优化器配置（带权重衰减开关的 `MyAdamWConfig`），验证两条还原路径都能选对子类，且 `build()` 得到的是你配置驱动的真实优化器。

**操作步骤**：

1. 新建 `my_optim.py`（**示例代码**，非项目源码）：

```python
# my_optim.py —— 示例代码
from dataclasses import dataclass

import torch

from olmo_core.optim import OptimConfig
from olmo_core.optim.adamw import AdamWConfig


@OptimConfig.register("my_adamw")      # 登记进 OptimConfig 的注册表
@dataclass
class MyAdamWConfig(AdamWConfig):
    """带权重衰减开关的 AdamW 配置（示例）。"""

    wd_multiplier: float = 1.0        # 开关：设为 0.0 即关闭权重衰减

    def build(self, model, strict=True):
        # 把开关折算成 weight_decay，再复用父类的 build()。
        # 不能直接 super().build()：基类 build 会把 as_dict() 的全部字段
        # 塞给 torch.optim.AdamW 构造函数，多出来的 wd_multiplier 会报 TypeError
        # （见 src/olmo_core/optim/config.py build() 的 kwargs 处理）。
        base = AdamWConfig(
            lr=self.lr,
            betas=self.betas,
            eps=self.eps,
            weight_decay=self.weight_decay * self.wd_multiplier,
        )
        return base.build(model, strict=strict)
```

2. 新建 `verify_my_adamw.py`（**示例代码**），分别走两条还原路径：

```python
# verify_my_adamw.py —— 示例代码
import yaml

import my_optim  # noqa: F401  必须 import，注册才会发生！
from olmo_core.optim import OptimConfig

model = torch.nn.Linear(4, 4)

# 路径 A：注册表查找——手写 YAML，只写 type，不写 _CLASS_
with open("my_adamw.yaml", "w") as f:
    yaml.safe_dump({"type": "my_adamw", "lr": 1.0e-4, "wd_multiplier": 0.0}, f)
cfg = OptimConfig.from_yaml("my_adamw.yaml")
print("路径 A 得到：", type(cfg))
assert isinstance(cfg, my_optim.MyAdamWConfig)

# 路径 B：_CLASS_ 精确还原——as_config_dict 往返
dumped = cfg.as_config_dict()
print("存档形态：", dumped)          # 应同时含 _CLASS_ 和 type 两个键
cfg2 = OptimConfig.from_dict(dumped)
assert isinstance(cfg2, my_optim.MyAdamWConfig)

# build 得到真实优化器
opt = cfg2.build(model)
print(type(opt), opt.param_groups[0]["lr"], opt.param_groups[0]["weight_decay"])
```

（`verify_my_adamw.py` 顶部需要 `import torch`。）

3. 运行：`python verify_my_adamw.py`。

**需要观察的现象**：

- 路径 A 打印的 `type(cfg)` 是 `MyAdamWConfig` 而非 `OptimConfig`——尽管 `from_yaml` 是在基类上调用的；
- `dumped` 里同一层级出现 `_CLASS_: my_optim.MyAdamWConfig` 与 `type: my_adamw` 两个键；
- 忘记 `import my_optim` 时路径 A 报错（找不到注册名 `my_adamw`）。

**预期结果**：最后一行打印 `torch.optim.AdamW`、`lr = 0.0001`、`weight_decay = 0.0`（`1e-2 × 0.0`，开关生效）。**待本地验证**——本 worker 环境未执行 Python；若路径 A 报注册名错误，先确认 `import my_optim` 已执行、注册名拼写一致；若路径 B 报 import 错误，确认从 `my_optim.py` 所在目录运行（`_CLASS_` 记录的是 `my_optim.MyAdamWConfig`，加载时要能 import 到它）。

#### 4.2.5 小练习与答案

**练习 1**：`as_config_dict()` 的输出里，`_CLASS_` 和 `type` 各记录什么？反序列化时谁优先？

**答案**：`_CLASS_` 记录完整模块路径（写入处 [src/olmo_core/config.py:L128-L129](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L128-L129)），`type` 记录注册名（[src/olmo_core/config.py:L130-L133](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L130-L133)）。`from_dict` 中 `_CLASS_` 优先：解析成功就 `pop("type")` 后直接构造（[src/olmo_core/config.py:L269-L288](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L269-L288)）。

**练习 2**：手写 YAML 里写 `optim: {type: adamw, lr: 1e-3}`，经 `TransformerTrainModuleConfig.from_yaml()` 加载后，`optim` 字段是什么类型的对象？凭什么？

**答案**：`AdamWConfig`。字段类型标注是基类 `OptimConfig`（见 4.3.3 ①），dict 无 `_CLASS_` 时由 `decode(cls, dict)` 按注册名 `"adamw"` 在 `OptimConfig` 注册表查到 `AdamWConfig`（查表行为由 [src/olmo_core/config.py:L276-L279](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L276-L279) 注释佐证）。**待本地验证**。

**练习 3**：为什么 `from_dict` 在 `_CLASS_` 解析成功后要主动删掉 `type` 键，而不是留着？

**答案**：因为注册表挂在父类上，解析出的子类自己的 `_registry` 可能为空；若把 `type` 留给 `decode`，它会在子类那本空簿子上做注册表查找而失败。官方注释原文见 [src/olmo_core/config.py:L276-L279](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L276-L279)。

### 4.3 多态字段的消费端：为什么四个子系统都这么设计

#### 4.3.1 概念说明

有了注册机制，再看库的设计就顺理成章：**凡是「同一个接口、多种实现、需要运行时选择」的地方，字段都标注为 Registrable 基类**。这带来的工程收益：

- **可替换**：换优化器 = 换一个子类实例（或 YAML 里的 `type` 字符串），消费方代码零改动——这是开闭原则（对扩展开放、对修改关闭）的通俗版。
- **可存档**：`as_config_dict()` 自动为每个多态节点写 `type`，实验配置天然可复现。
- **可扩展**：第三方注册新子类即可接入（4.2.4 的 `MyAdamWConfig` 就是演示）。
- **统一出口**：不管哪个子类，都实现同一个 `build()`，调用方拿到的是真实组件（优化器/调度器/注意力层/数据加载器）。

为什么恰好是优化器、调度器、注意力、数据加载器这四族？因为它们都满足「接口稳定、实现多样、按实验选择」的特征：优化器有 AdamW/Lion/Muon…；调度器有 cosine/WSD/sequential…；序列混合器有标准 attention、gated delta net、Kimi delta attention…；数据加载器有 numpy 与 composable 两代 API。

#### 4.3.2 核心流程

一个多态字段从声明到生效的完整生命周期：

```text
① 声明：字段标注为基类          optim: OptimConfig
② 赋值：装入任意注册子类        optim=AdamWConfig(lr=1e-3)
③ 序列化：自动附上 type        {..., "_CLASS_": "...AdamWConfig", "type": "adamw", "lr": 1e-3}
④ 还原：_CLASS_ 优先/type 查表  from_dict → AdamWConfig 实例
⑤ 构建：统一出口 build()       调用子类的 optimizer() 等 → torch.optim.AdamW
```

#### 4.3.3 源码精读

**① 优化器与调度器字段（训练模块上）。**

- [src/olmo_core/train/train_module/transformer/config.py:L400-L404](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/train_module/transformer/config.py#L400-L404)：`TransformerTrainModuleConfig` 里 `optim: OptimConfig` 与 `scheduler: Optional[Scheduler] = None`——两个多态字段并排。注意标注是**基类**，这就是 Python 侧的多态声明。
- [src/examples/llm/train.py:L178-L193](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L178-L193)：官方示例给这两个字段分别赋 `AdamWConfig(...)`（带 `OptimGroupOverride` 给 embedding 免权重衰减）和 `CosWithWarmup(warmup_steps=100)`——同一字段换个构造调用即可换实现。
- [src/examples/moe/train.py:L106-L114](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/moe/train.py#L106-L114)：MoE 示例按分支条件给 `optim` 字段装 `AdamWConfig(...)` 或 `SkipStepAdamWConfig(...)`——两个注册名不同的子类进同一字段，序列化后各自的 `type` 不同，互不混淆。

**② 注意力族（SequenceMixerConfig）。**

- [src/olmo_core/nn/attention/base.py:L66-L67](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/base.py#L66-L67)：`class SequenceMixerConfig(ModuleConfig, Registrable, Generic[SeqMixer])`——把「序列混合器」（注意力及其变体）抽象成可注册基类。
- [src/olmo_core/nn/attention/__init__.py:L354-L366](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/__init__.py#L354-L366)：`@SequenceMixerConfig.register("attention")` 登记 `AttentionConfig`，其内部再用 `AttentionType` 枚举细分实现。
- [src/olmo_core/nn/transformer/config.py:L168](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L168)：`TransformerConfig.sequence_mixer: SequenceMixerConfig = field(default=UNSET)`——Transformer 的序列混合器字段标注为基类，所以标准 attention、Mamba 类（`gated_delta_net`，注册于 [src/olmo_core/nn/attention/recurrent.py:L331](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/recurrent.py#L331)）都能装进来。

**③ 数据加载器族。**

- [src/olmo_core/data/data_loader.py:L1094-L1107](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/data/data_loader.py#L1094-L1107)：`DataLoaderConfig(Config, Registrable, Generic[L])` 基类 + `@DataLoaderConfig.register("numpy")` 登记的 `NumpyDataLoaderConfig`；[src/olmo_core/data/composable/data_loader.py:L68](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/data/composable/data_loader.py#L68) 再登记 `"composable"`——新一代 composable API（u4-l2 的主角）就是靠注册机制与旧 API 共存于同一字段类型下，可平滑切换。

**④ `build()` 如何消费注册子类。**

- [src/olmo_core/optim/config.py:L156-L179](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/config.py#L156-L179)：`OptimConfig.build()` 的关键三步——抽象方法 `optimizer()` 由**子类**实现（`AdamWConfig` 返回 `torch.optim.AdamW`，见 [src/olmo_core/optim/adamw.py:L247-L249](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adamw.py#L247-L249)）；`build()` 把自身字段 `as_dict()` 后（弹出 `group_overrides/compile/fixed_fields` 三个框架字段）喂给该优化器类构造。多态的落点就在这：**同一份 build 代码，因注册子类不同而产出不同的优化器**。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：亲手把四个多态字段的位置抄成一张表，验证「字段标注 = Registrable 基类」的规律。

**操作步骤**：

1. 在仓库根目录执行（只读 grep）：

```bash
grep -n "optim: OptimConfig\|scheduler: Optional\[Scheduler\]\|sequence_mixer: SequenceMixerConfig" \
  src/olmo_core/train/train_module/transformer/config.py \
  src/olmo_core/nn/transformer/config.py
```

2. 打开 [src/olmo_core/script_utils.py:L32](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L32)，观察 `ExperimentConfig.data_loader` 字段的标注用的是**具体类** `NumpyDataLoaderConfig`（而非 `DataLoaderConfig` 基类），想一想这说明了什么。

3. 把结果整理成四列的表：字段名 / 所在文件与行号 / 标注的基类 / 该基类的注册名清单（用 4.1.4 的 grep 输出填）。

**需要观察的现象**：前三处 grep 命中的字段标注都是基类；而 `script_utils.py` 的 `data_loader` 却用了具体类。

**预期结果**：规律成立——核心配置（train module、transformer）大量使用多态字段；而 `ExperimentConfig` 作为官方脚本的窄口径入口，刻意把 data loader 收窄到 `NumpyDataLoaderConfig` 以简化脚本。这是一个很好的对照：**多态是能力，收窄是选择**，框架把两者都留给了你。第 2 步的观察待你本地确认。

#### 4.3.5 小练习与答案

**练习 1**：若想把 `sequence_mixer` 字段从标准 attention 换成 `gated_delta_net`，`TransformerConfig` 里 `sequence_mixer` 字段的类型标注需要改吗？

**答案**：不需要。字段标注是基类 `SequenceMixerConfig`（[src/olmo_core/nn/transformer/config.py:L168](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L168)），`gated_delta_net` 注册到同一基类（[src/olmo_core/nn/attention/recurrent.py:L331](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/recurrent.py#L331)），配置系统层面直接兼容。（能否真的训起来还取决于两边超参是否匹配，但那不是配置系统的障碍。）

**练习 2**：`OptimGroupOverride`（[src/olmo_core/optim/config.py:L43-L53](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/config.py#L43-L53)）只继承了 `Config`，没混入 `Registrable`。为什么？

**答案**：它只是一个「参数名模式 + 选项」的简单数据容器，没有一族需要运行时互换的实现。`Registrable` 服务于「一接口多实现、按名选择」的场景，不是所有 Config 都需要——这是设计判断：注册名和注册表是成本（要维护名字、要防冲突），只在需要可插拔处付费。

**练习 3**：注册名和 Python 类名是什么关系？`SkipStepAdamWConfig` 的注册名是什么？

**答案**：注册名是登记时人为指定的字符串，与类名无需一致，但全库习惯取「能看懂的小写蛇形名」。`SkipStepAdamWConfig` 注册为 `"skip_step_adamw"`（[src/olmo_core/optim/adamw.py:L252-L254](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/optim/adamw.py#L252-L254)）。序列化与 YAML 选择用的都是注册名，而非类名。

## 5. 综合实践

把本讲三块知识串成一个完整小任务：**给你的训练实验插一个自定义优化器，全程不碰 OLMo-core 源码。**

任务：完成并扩展 4.2.4 的两个文件，最终做到「一份 YAML 驱动一次可复现的构建」。

1. **注册**（4.1）：在 `my_optim.py` 里把 `MyAdamWConfig` 注册为 `"my_adamw"`，确认 `import my_optim` 后注册生效。
2. **手写 YAML 选择**（4.2）：写 `my_adamw.yaml`，只含 `type: my_adamw` 与超参，用**基类** `OptimConfig.from_yaml()` 加载，断言得到 `MyAdamWConfig` 实例。
3. **往返验证**（4.2）：对加载结果执行 `as_config_dict()` → `yaml.safe_dump` 落盘 → 重新 `from_yaml`，断言两次结果相等（`==`，dataclass 自动按字段比较），确认 round-trip 无损。
4. **命令行覆盖预演**（衔接 u2-l3）：对第 2 步得到的 `cfg` 执行 `cfg.merge(["lr=3e-4", "wd_multiplier=1.0"])`，断言返回的是**新对象**且两个字段已更新、类型仍是 `MyAdamWConfig`——merge 重建时 `_CLASS_`/`type` 两条线索保住了子类身份。
5. **构建验证**（4.3）：用 `torch.nn.Linear(4, 4)` 当占位模型执行 `build()`，检查返回对象的类型与 `param_groups[0]` 里的 `lr`、`weight_decay` 是否与配置一致（`wd_multiplier=1.0` 时 weight_decay 应为 `1e-2`）。

**预期结果**：全部断言通过；第 4 步证明 dotlist 覆盖不会丢子类类型，这正是官方脚本敢用 `--train_module.optim.lr=6e-3` 覆盖嵌套多态字段的底层原因（下讲 u2-l3 展开）。整体**待本地验证**；若第 2 步失败，检查是否忘记 `import my_optim`；若第 3 步失败，检查是否从 `my_optim.py` 同目录运行（`_CLASS_` 要能被 import）。

## 6. 本讲小结

- `Registrable` 是外部包 `dataclass-extensions` 提供的 mixin，`config.py` 导入并再导出；用法是 `@基类.register("名字")` 把子类登记进**基类的**注册表（子类自身 `_registry` 可能为空，有官方注释佐证）。
- 序列化双线索：`as_config_dict()` 给每个多态节点同时写 `_CLASS_`（完整类路径）与 `type`（注册名）。
- 反序列化优先级：`from_dict` 先按 `_CLASS_` 动态 import，成功则删掉 `type` 直接构造；手写 YAML（只有 `type`）则由外部库 `decode` 在基类注册表里查名选子类。
- 多态字段 = 类型标注为基类的字段（`optim: OptimConfig`、`sequence_mixer: SequenceMixerConfig`…），换实现只需换子类/YAML 里的 `type` 字符串，消费方零改动。
- 优化器、调度器、注意力后端、数据加载器四族共享这一模式，因为它们都是「接口稳定、实现多样、运行时选择」；`build()` 是统一出口。
- 接入自定义实现的三件套：注册装饰器 + 超参字段 + `optimizer()`（或相应 `build()`）实现——`AdamConfig` 是 25 行的完整模板。

## 7. 下一步学习建议

- **下一讲 u2-l3（命令行覆盖与 dotlist 合并）**：本讲 4.2.3 ② 已看到 `merge()` 携带 `include_registered_name=True` 的原因，下讲沿这条线讲清 `--train_module.optim.lr=6e-3` 从命令行到字段的完整链路，以及 `script_utils.py` 的官方脚本入口。
- **单元五 u5-l1（优化器配置体系）**：本讲的 `MyAdamWConfig` 只是入门；`OptimGroupOverride` 按参数名分组覆盖、`fixed_fields` 与 checkpoint 恢复的配合都在 `src/olmo_core/optim/config.py` 里等着精读。
- **单元三 u3-l3（注意力与可插拔后端）**：看 `SequenceMixerConfig` 注册族如何撑起 attention/Mamba 等多种序列混合器的切换。
- 随手练习：把 4.1.4 的 grep 表存下来，后面每学一个单元，就回看对应注册族的一个注册名是如何被 `build()` 消费的。
