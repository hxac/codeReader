# Config 基类：一切配置的基石

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Config` 基类提供了哪些能力（`as_dict` / `as_config_dict` / `from_dict` / `from_yaml` / `from_json` / `from_file` / `merge` / `apply` / `validate` / `replace` / `copy`），以及**没有**提供哪些能力（比如它没有 `save()` 方法）。
2. 解释为什么 OLMo-core 把所有配置都设计成「可序列化的嵌套 dataclass」，而不是散落的命令行参数或普通 Python 对象。
3. 自己动手定义一个继承 `Config` 的实验配置类，完成「YAML 往返 + dotlist 覆盖」的全流程。

本讲是单元二的第一讲。上一单元（u1-l4）你已经看到 `examples/llm/train.py` 用五个 Config 积木组装出一个训练实验；本讲就钻进这些积木共同的「地基」—— [src/olmo_core/config.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py)。

## 2. 前置知识

本讲只需要 Python 基础，但下面几个概念要先讲清楚：

- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器让你只声明字段就能自动获得 `__init__`、`__repr__`、`__eq__` 等方法。`Config` 本身就是一个 dataclass，它的所有子类（`TransformerConfig`、`TrainerConfig`……）也都是。你可以把 dataclass 理解为「带类型标注、可比较、好打印的结构体」。
- **YAML / JSON**：两种最常见的文本配置格式。YAML 用缩进表示层级（`model:\n  d_model: 768`），JSON 用花括号。OLMo-core 两种都支持读取。
- **序列化（serialization）**：把内存里的对象变成可以写进文件的字典/文本的过程；反序列化则是反过来。**往返（round-trip）** 指的是「对象 → 字典 → 文件 → 字典 → 对象」之后还能不能得到等价的对象，即 \( f^{-1}(f(x)) = x \)。
- **dotlist（点号列表）**：形如 `"model.init_std=0.01"` 的字符串，用点号表示嵌套路径。`Config.merge()` 和 `from_dict(overrides=...)` 都吃这种格式，这是 `--train_module.optim.lr=6e-3` 命令行覆盖的底层机制（u2-l3 展开）。
- **StrEnum**：既 是字符串又是枚举的值。config.py 里定义了一个兼容老版本 Python 的 [StrEnum](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L39-L49)，以及一个枚举常见 torch 精度的 [DType](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L365-L392)（`float32`、`bfloat16`……），它们是配置字段里最常见的「非基本类型」。

一个容易混淆的点先说破：**`Config` 没有 `save()` 方法**。「存」这一侧由 `as_dict()` / `as_config_dict()` 输出普通字典，再由你用 `yaml.safe_dump` / `json.dump` 写盘；「读」这一侧才有现成的类方法 `from_yaml` / `from_json` / `from_file`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注度 |
| --- | --- | --- |
| `src/olmo_core/config.py` | **主角**。`Config` 基类、`StrEnum`、`DType`、`UNSET` 哨兵值，以及 `merge` 用到的两个私有工具函数 | ⭐⭐⭐ |
| `src/olmo_core/nn/config.py` | `ModuleConfig` / `ModelConfig`：`Config` 的两层子类，加上 `build()` 抽象方法和参数量属性，示范「Config 继承链」如何搭起来 | ⭐⭐ |
| `src/olmo_core/nn/transformer/config.py` | `TransformerConfig`：一个真实的大型嵌套 Config（含 `olmo2_190M` 等工厂方法），本讲实践的嵌套字段就用它 | ⭐⭐ |
| `src/olmo_core/internal/experiment.py` | 训练入口处用 `as_config_dict()` 把配置记录到 W&B/Comet 和每个 checkpoint 目录的真实用法 | ⭐ |
| `src/test/config_test.py` | Config 的单元测试，本讲多个断言的「事实依据」 | ⭐⭐ |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Config 基类**——它是什么、为什么整个库都以它为地基；
2. **yaml/json 序列化**——`as_dict` / `as_config_dict` 出去、`from_dict` / `from_yaml` / `from_json` 回来；
3. **dotlist 覆盖（merge）**——嵌套字段如何被一条字符串精准改写（为 u2-l3 的命令行覆盖打底）。

### 4.1 Config 基类：「一切皆 Config」的地基

#### 4.1.1 概念说明

训练一个大模型要做的每一个决定——模型多大、数据从哪来、学习率多少、多少步存一次 checkpoint——在 OLMo-core 里**全部**表达为 `Config` 子类的字段。你在 u1-l4 见过的 `model / dataset / data_loader / train_module / trainer` 五大积木，每一个都是一个 `Config` 子类的实例。

为什么非要这样设计？因为配置对象要满足三个苛刻条件：

1. **可序列化**：能整体写成 YAML/JSON 存进 checkpoint 目录，重启训练时原样读回来；
2. **可嵌套组合**：`TrainerConfig` 里装着 `CheckpointerConfig`，`TransformerConfig` 里装着 `LMHeadConfig`、`RoPEConfig`……树形结构要能整体进出序列化；
3. **可精准覆盖**：一条 `"model.init_std=0.01"` 字符串要能定位到树中任意深度的叶子并替换它，否则「换一个学习率重跑」就要改代码。

`Config` 基类把这三件事一次性解决，所以它的子类只需要「声明字段」，就免费获得序列化和 dotlist 覆盖能力。这就是 README 和 AGENTS.md 里说的「一切皆 Config」的真正含义。

#### 4.1.2 核心流程

一个 `Config` 子类的生命周期：

```text
┌─ 1. 定义：@dataclass class MyConfig(Config): 字段...
│
├─ 2. 构造：直接调用构造函数，或从文件加载（from_yaml / from_json）
│
├─ 3. 校验：validate()（多数子类会重写它做字段合法性检查）
│
├─ 4. 使用：
│     · ModuleConfig 子类：调 build() 生成真实 PyTorch 模块
│     · 顶层实验配置：交给 script_utils / internal.experiment 驱动训练
│
├─ 5. 记录：as_config_dict() → 写入 checkpoint 目录 / W&B
│
└─ 6. 恢复 / 变体：from_dict() 读回，或 merge() 生成改了几个字段的新配置
```

继承链示意（从地基到具体模型）：

```text
Config                        (config.py)
 └─ ModuleConfig              (nn/config.py，+ build() 抽象方法)
     └─ ModelConfig           (nn/config.py，+ num_params 等属性)
         └─ TransformerConfig (nn/transformer/config.py，真实模型配置)
```

#### 4.1.3 源码精读

先看基类本身。类声明和它的「使用契约」写在 docstring 里：

[config.py:L55-L71](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L55-L71) —— `Config` 是一个 `@dataclass`，docstring 里两条「重要」提示就是使用契约：**子类必须重新加 `@dataclass` 装饰器**（否则字段不会被正确收集），且**字段只能用简单类型或嵌套的 Config**（因为要可序列化）。

[config.py:L73-L82](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L73-L82) —— 两个类级常量：`CLASS_NAME_FIELD = "_CLASS_"` 是序列化时注入的「类名字段」（4.2 节的主角）；`_IGNORE_FIELDS` 记录加载时应忽略的历史字段名，用于向后兼容（旧 checkpoint 里存在、新版本已删除的字段不会导致加载失败）。

除了序列化和 merge，基类还有四个小而常用的工具方法：

[config.py:L167-L189](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L167-L189) —— `apply(func)`：递归地对配置树里**每一个** Config 实例（包括自己）调用 `func`。典型用途是批量初始化 seed 之类的字段——不用自己写递归遍历。

[config.py:L191-L195](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L191-L195) —— `validate()`：默认空实现，子类重写它做字段校验（可能原地修改 self）。你会在 `TransformerConfig.__post_init__` 里看到同风格的校验逻辑。

[config.py:L227-L237](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L227-L237) —— `replace(**changes)` 和 `copy(deep=True)`：分别包装 `dataclasses.replace` 和 `copy.deepcopy`，都是「生成新对象、不动原对象」的不可变风格操作。`merge()` 也遵守这一风格（见 4.3）。

再看继承链如何长出「能 build 出真实模型」的能力：

[nn/config.py:L9-L16](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L9-L16) —— `ModuleConfig` 在 `Config` 之上只加了一个抽象方法 `build()`：「从图纸生成真实 `nn.Module`」。你在 u1-l4 看到的 `config.model.build(init_device="meta")` 走的就是它。

[nn/config.py:L19-L43](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L19-L43) —— `ModelConfig` 再加 `num_params` 等抽象属性，让配置在**不构建模型**的情况下就能报告参数量（综合实践会用到）。

一个真实的重型子科长这样：

[transformer/config.py:L335-L362](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L335-L362) —— `TransformerConfig` 的字段表：`d_model`、`vocab_size`、`n_layers` 是普通 int；`block`、`lm_head`、`embedding_norm` 是**嵌套的子 Config**；`dtype` 是 `DType` 枚举。注意它没有重写任何序列化方法——全部继承自 `Config`，这就是地基的威力。

[transformer/config.py:L647-L660](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L647-L660) —— 工厂方法 `olmo2_190M(vocab_size, **kwargs)`：用预设超参调 `llama_like(...)` 产出一个 `TransformerConfig` 实例，多余的 kwargs 还能继续覆盖。u3-l2 会系统讲这些预设，本讲实践直接拿它当「一个现成的嵌套 Config」用。

最后看一眼库自己怎么「记录」配置——这是 `Config` 设计动机的最直接证据：

[experiment.py:L449-L457](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/internal/experiment.py#L449-L457) —— 官方训练入口在 build 完组件后，立刻 `config.as_config_dict()` 并交给 `ConfigSaverCallback`，注释写明用途：「Record the config to W&B/Comet and each checkpoint dir」。整个实验的可复现性就押在这份字典上。

#### 4.1.4 代码实践

**实践目标**：亲手定义一个 `Config` 子类，验证「只声明字段就免费获得 as_dict 能力」。

**操作步骤**（环境要求：按 u1-l2 完成 `pip install -e '.[all]'`；注意 `config.py` 顶部 `import torch`，所以需要 torch 已安装）：

```python
# 文件名：my_config_demo.py（示例代码，非项目原有文件）
from dataclasses import dataclass
from typing import Optional

from olmo_core.config import Config


@dataclass
class DataConfig(Config):
    path: str = "data/train.npy"
    sequence_length: int = 2048


@dataclass
class MyExperimentConfig(Config):
    save_folder: str = "runs/demo"
    seed: int = 1
    data: DataConfig = DataConfig()
    note: Optional[str] = None


if __name__ == "__main__":
    c = MyExperimentConfig()
    print(c.as_dict())
    print(c.as_dict(exclude_none=True))
```

**需要观察的现象**：第一行打印包含 `note: None`；第二行 `exclude_none=True` 后 `note` 消失；两种输出里 `data` 都已经被递归展开成普通 dict。

**预期结果**：与 [config_test.py:L7-L15](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L7-L15) 的断言完全同构——那个测试用 `MockConfig` 验证了 `as_dict()` 默认含 `None`、`exclude_none=True` 时剔除 `None`。本实践只是把同一个行为搬到一个带嵌套字段的配置上（嵌套递归行为的测试依据是 [config_test.py:L18-L53](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L18-L53)）。本环境未实际运行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@dataclass` 装饰器忘在 `MyExperimentConfig` 上，会发生什么？

**参考答案**：`Config` 基类自己带 `@dataclass`，所以类仍可实例化，但它**自己的**新字段（`save_folder` 等）不会被 `dataclasses.fields()` 收集到——`as_dict()` 会丢掉这些字段，`merge()` 也定位不到它们。这正是 [config.py:L60-L67](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L60-L67) docstring 里第一条「重要」提示要求子类重新装饰的原因。

**练习 2**：`apply()` 和直接写一个 `for` 循环遍历字段相比，好在哪里？

**参考答案**：`apply()` 会递归进入嵌套 dataclass、dict、list/tuple/set 的每一层（见 [config.py:L174-L189](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L174-L189)），对任意深度的配置树都成立；手写循环要自己处理所有容器类型，且每加一种嵌套字段就要回头改遍历代码。

### 4.2 yaml/json 序列化：一进一出，严丝合缝

#### 4.2.1 概念说明

序列化要解决的核心矛盾是：**配置树里有类型信息（这个 dict 应该还原成 `TransformerConfig` 而不是 `Bar`），而 YAML/JSON 里没有**。

OLMo-core 的解法分两半：

- **出去**（`as_dict` / `as_config_dict`）：把配置树递归转成普通字典；需要时在每个节点注入一个 `_CLASS_` 字段，写明「我原本是哪个类」（完整路径 `模块名.类名`），Registrable 子类还会额外带一个 `"type"` 注册名。
- **回来**（`from_dict` / `from_yaml` / `from_json` / `from_file`）：读入字典后递归扫描，看到 `_CLASS_` 就动态 import 对应类并重建实例；没有 `_CLASS_` 的节点则依据外层 dataclass 的类型标注还原。

两种出口的定位不同：

| 方法 | 丢失什么 | 注入什么 | 适用场景 |
| --- | --- | --- | --- |
| `as_dict()` | 什么都不丢 | 默认不注入 | 程序内转换、测试断言 |
| `as_config_dict()` | 丢 `None` 字段、私有字段；tuple/set 转 list；非基本类型转 `str` | `_CLASS_` + `"type"` | **写盘存档**（官方记录格式） |

`as_config_dict()` 丢掉的东西听起来吓人，但没关系：`from_dict` 会用字段的**默认值**补齐缺失项，所以往返后仍是等价对象。

#### 4.2.2 核心流程

`as_dict()` 的递归规则（对配置树每个节点）：

```text
节点是 dataclass？
 ├─ 是 → 遍历 fields()，跳过：exclude 集合中的、init=False 的、
 │        （可选）值为 None 的、私有字段（下划线开头）
 │        → 子节点递归；可选注入 _CLASS_ / "type"
 ├─ 是 dict → 对每个 value 递归
 ├─ 是 list/tuple/set → json_safe 时统一变 list，否则保持原类型
 ├─ 是 None/float/int/bool/str → 原样返回
 └─ 其他（torch.dtype、Path 等）→ json_safe 时 str(d)，否则原样返回
```

`from_dict()` 的还原规则：

```text
输入 dict（可能先套用 overrides dotlist）
 ├─ 有 _CLASS_ 且能 import 到该类？
 │    ├─ 是 → 剔除 _IGNORE_FIELDS 中的历史字段，剔除 "type"
 │    │       → decode(该类, 剩余 dict) 构造实例（构造失败抛 OLMoConfigurationError，
 │    │         错误信息带上该节点在配置树中的路径前缀）
 │    └─ 否 → 当普通 dict 继续
 ├─ dict 的键若是纯数字字符串 → 转回 int 键（YAML/JSON 只能有字符串键）
 └─ 顶层结果若不是目标类 cls 的实例 → 再 decode(cls, dict)
```

往返保持律可以写成：

\[ \mathrm{from\_dict}\big(\mathrm{as\_config\_dict}(c)\big) \;\approx\; c \]

「≈」而非「=」的残余差异只来自：被丢的 `None` 字段用默认值补齐（值相同）、tuple/set 变 list（结构变了）、枚举经字符串还原。只要字段是 int/float/str/bool/None/嵌套 Config/list/dict，往返就是严格相等的。

#### 4.2.3 源码精读

**出去的一侧：**

[config.py:L84-L151](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L84-L151) —— `as_dict()` 全貌：7 个关键字参数（`exclude_none` / `exclude_private_fields` / `exclude` / `include_class_name` / `include_registered_name` / `json_safe` / `recurse`）控制输出形态。

[config.py:L110-L120](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L110-L120) —— `iter_fields` 内部函数：逐字段过滤。注意 `not field.init` 的字段（`field(init=False)` 声明的派生字段）永远不参与序列化。

[config.py:L122-L149](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L122-L149) —— 内层 `as_dict` 递归函数：就是上面流程图伪代码的原文。L129 注入 `_CLASS_`（`模块名.类名` 格式）；L130-L135 对 Registrable 子类注入注册名 `"type"`（找不到注册名时静默跳过）；L144-L147 是「非基本类型」的兜底分支。

[config.py:L153-L165](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L153-L165) —— `as_config_dict()`：一行调用把 5 个开关全部打开，注释说明定位是「适合**记录**配置的 JSON 安全字典」。[experiment.py:L456](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/internal/experiment.py#L456) 就是它的最大用户。

**回来的一侧：**

[config.py:L239-L305](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L239-L305) —— `from_dict()` 类方法全貌。

[config.py:L249-L256](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L249-L256) —— `resolve_cls`：把 `"olmo_core.nn.transformer.config.TransformerConfig"` 这样的字符串拆出模块和类名，动态 `import_module` 后取属性。这是 `_CLASS_` 机制的发动机。

[config.py:L258-L295](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L258-L295) —— `decode_data` 递归函数。三处值得细看：

- L260-L262：注释自述为 HACK——把纯数字字符串键转回 int 键，专为 `block_overrides`（以层号为键的字段）在 YAML 中被迫变成字符串键的场景兜底；
- L269-L279：看到 `_CLASS_` 就解析类、剔除 `_IGNORE_FIELDS` 历史字段、并删掉 `"type"`（类已通过 `_CLASS_` 确切解析，无需再查注册表）；
- L281-L288：构造失败时抛 `OLMoConfigurationError`，且错误消息带上该节点在配置树中的路径（`prefix`），报错时能直接定位到 `model.block.attention.rope` 这样的位置。

[config.py:L297-L305](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L297-L305) —— 收尾：先套用可选的 `overrides` dotlist（和 4.3 的 merge 同一套机制），再递归解码；顶层若还不是 `cls` 的实例，用 `dataclass_extensions` 库的 `decode` 按类型标注收尾。

**文件入口的三个包装：**

[config.py:L307-L315](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L307-L315) —— `from_file()`：按扩展名分发，`.yml`/`.yaml` 走 YAML、`.json` 走 JSON，其他后缀直接抛 `OLMoConfigurationError`（不给静默猜测的机会）。

[config.py:L317-L327](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L317-L327) —— `from_json()` / `from_yaml()`：都是「打开文件 → 解析成 dict → 交给 `from_dict`」。注意打开用的是 `cached_path`（来自 `cached_path` 库），所以路径可以是本地文件，也可以是 http(s) URL——官方脚本可以直接 `from_yaml("https://...")` 拉取远端配置。

**行为的事实依据（测试）：**

[config_test.py:L18-L53](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L18-L53) —— `test_nested_configs`：完整验证了嵌套 dataclass 的 `as_dict` 递归展开、`from_dict` 无类名还原（`foo1 == foo` 且 `foo1.bar` 还是 `Bar` 实例）、带 overrides 的加载、`recurse=False` 的行为，以及 `as_config_dict()` 的确切输出形状（`_CLASS_` 字段的格式在这看得一清二楚）。

[config_test.py:L56-L69](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L56-L69) —— `test_json_safe_dump`：tuple/set 在 `as_config_dict()` 里都变 list。这解释了 4.2.1 表格里「tuple/set 转 list」那一格。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：完成规格指定的任务——定义含嵌套 `TransformerConfig` 的 `MyExperimentConfig`，保存为 YAML 再加载回来验证往返一致，然后用 `merge()` 用 dotlist 覆盖嵌套字段。

**操作步骤**：

```python
# 文件名：my_config_roundtrip.py（示例代码，非项目原有文件）
from dataclasses import dataclass

import yaml

from olmo_core.config import Config
from olmo_core.nn.transformer.config import TransformerConfig


@dataclass
class MyExperimentConfig(Config):
    save_folder: str = "runs/my-exp"
    model: TransformerConfig = None  # 构造时传入


if __name__ == "__main__":
    # 1. 组装：工厂方法产出一个真实的嵌套 Config
    config = MyExperimentConfig(
        save_folder="runs/demo",
        model=TransformerConfig.olmo2_190M(vocab_size=50304),
    )

    # 2. 存：as_config_dict() 输出 JSON 安全字典，safe_dump 写 YAML
    with open("my-exp.yaml", "w") as f:
        yaml.safe_dump(config.as_config_dict(), f)

    # 3. 取：from_yaml 读回（内部走 from_dict）
    loaded = MyExperimentConfig.from_yaml("my-exp.yaml")
    assert isinstance(loaded.model, TransformerConfig)
    assert loaded.model.d_model == config.model.d_model      # 768
    assert loaded.model.n_layers == config.model.n_layers    # 12
    assert loaded == config, "round-trip should be equal"

    # 4. 改：merge 用 dotlist 覆盖嵌套字段，返回新对象
    variant = loaded.merge(["model.init_std=0.01", "save_folder=runs/demo2"])
    assert variant.model.init_std == 0.01
    assert variant.save_folder == "runs/demo2"
    assert loaded.model.init_std != variant.model.init_std    # 原对象未被修改

    print("YAML 内容头部：")
    print(open("my-exp.yaml").read()[:400])
```

**需要观察的现象**：

1. `my-exp.yaml` 里每个配置节点都带 `_CLASS_: olmo_core....` 字段，Registrable 子节点还带 `type` 字段；
2. `loaded == config` 通过——往返严格一致（`init_std` 等有默认值字段被 `as_config_dict` 省略后由 `from_dict` 补回默认值）；
3. `merge` 之后 `loaded` 原对象不变——`merge` 是「复制后改」而非原地修改。

**预期结果**：断言全部通过。依据：往返与省略-补默认值行为同 [config_test.py:L18-L53](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L18-L53)；`as_config_dict` 输出可被 YAML 安全序列化，依据是 [experiment.py:L455-L457](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/internal/experiment.py#L455-L457) 用它做正式存档格式。若你的字段里含 tuple/set，`==` 可能因 list 化而失败（[config_test.py:L56-L69](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L56-L69) 展示了该转换），本例字段不含这些类型。本环境未能实际运行，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接 `yaml.safe_dump(config.as_dict())` 存档，而要 `as_config_dict()`？

**参考答案**：`as_dict()` 会原样保留 torch dtype、Path 等非基本类型（[config.py:L144-L149](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L144-L149) 的兜底分支在 `json_safe=False` 时直接返回原对象），`safe_dump` 对它们要么报错要么产生无法还原的表示；且不注入 `_CLASS_`，读回时嵌套节点的具体子类只能依赖外层类型标注推断。`as_config_dict()` 把这些全变成字符串并注入类名，才是官方记录格式。

**练习 2**：YAML 里没有 int 键，`block_overrides: {"3": {...}}` 读回来怎么变成 `3: {...}`？

**参考答案**：[config.py:L260-L262](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L260-L262) 的 HACK：`decode_data` 在递归 dict 时把「看起来像整数的字符串键」转回 int，注释明确说这是为了 `block_overrides` 这类以层号为键的字段。

**练习 3**：把一个字段声明为 `field(init=False)`（派生字段），它在 `as_dict()` 输出里吗？

**参考答案**：不在。[config.py:L112-L113](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L112-L113) 的 `iter_fields` 会跳过 `not field.init` 的字段——这类字段本来就是由其他字段算出来的，序列化它们属于冗余信息。

### 4.3 dotlist 覆盖：merge 如何精准改写树中一片叶子

#### 4.3.1 概念说明

`merge(dotlist)` 接收一组 `"a.b.c=value"` 字符串，返回一棵**新的**配置树，树上只有被点到的叶子换了值。它是三件事的物理基础：

- 官方脚本的 `--model.init_std=0.01` 命令行覆盖（u2-l3 详讲）；
- `from_dict(data, overrides=[...])` 在加载时顺手改字段；
- 「拿别人的配置文件，改三个数，变成我的实验」这一日常操作。

#### 4.3.2 核心流程

```text
merge(["model.init_std=0.01", "save_folder=runs/x"])
 │
 ├─ 1. _clean_opts：逐条清洗字符串
 │      · 没有 "=" → 值默认为 "true"（开关型参数写 --use-flash 就行）
 │      · 键名 strip(" -") 且把 "-" 换成 "_"（所以 --save-folder=x 也认）
 │      · 值用 yaml.safe_load 解析："0.01"→float，"6e-3"→float，
 │        "true"→bool，"[1,2]"→list（天然支持列表/布尔值）
 │
 ├─ 2.（可选）prefix="foo"：只保留 "foo." 开头的条目并剥掉前缀
 │      —— 用于「一棵大树里只改某个子树」的场景
 │
 ├─ 3.（可选）strict=False：丢弃指向不存在字段的条目而不是报错
 │
 ├─ 4. self.as_dict(include_class_name=True, include_registered_name=True)
 │      先整体转 dict（带上 _CLASS_，保证 from_dict 能还原所有子类）
 │
 ├─ 5. _set_nested：沿点号路径逐层下钻改值
 │      · 中间节点是 dict → 按字符串键下钻
 │      · 中间节点是 list → 按整数下标下钻（"block_overrides.3.xxx" 可用）
 │
 └─ 6. from_dict(改完的 dict) → 新的 Config 实例（原对象不动）
```

#### 4.3.3 源码精读

[config.py:L197-L225](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L197-L225) —— `merge()` 主体：docstring 强调「creating a new object」。L207-L212 是 prefix 过滤（`k.replace(f"{prefix}.", "", 1)` 只剥第一次出现，防止误伤后续同名段）；L214-L220 是 strict 模式过滤，判据是「键等于某字段名，或以 `字段名.` 开头」；L222-L225 是「转 dict → 逐条改 → from_dict 重建」三步。

[config.py:L330-L345](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L330-L345) —— `_set_nested`：用 `key.split(".", 1)` 递归下钻；dict 分支按字符串键、list 分支按 `int(key)` 下标；路径尽头把值写进去。写到非 dict/list 的位置会 `ValueError`，这就是「点错路径」时的报错来源。

[config.py:L352-L362](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L352-L362) —— `_clean_opt`：单条 dotlist 的清洗器。四个行为都值得记住：无 `=` 默认 `true`；键名两端的空格和 `-` 被剥掉（这就是命令行 `--foo` 风格能直接进来的原因）；键中的 `-` 转 `_`；值交给 `yaml.safe_load`，因此布尔、浮点、列表、null 全部合法。

[config_test.py:L72-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L72-L85) —— `test_non_strict_merge`：`merge(["--z=b", "--bar.x=0", "--baz.booz=0"], strict=False)`，验证 `--` 前缀被清洗、嵌套覆盖生效、指向不存在字段的 `baz.booz` 在非严格模式下被静默丢弃。

[config_test.py:L88-L101](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L88-L101) —— `test_merge_with_prefix`：dotlist 全带 `foo.` 前缀、`merge(..., prefix="foo")`，验证只有该子树的覆盖生效。

[config_test.py:L38-L41](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L38-L41) —— 同一测试里还验证了 `from_dict(data, overrides=["bar.x=0"])`：**加载时**覆盖与 `merge`（**加载后**覆盖）共用 `_clean_opts` + `_set_nested` 这套底层（对照 [config.py:L297-L299](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L297-L299)）。

#### 4.3.4 代码实践

**实践目标**：用真实测试同款的三种姿势（普通 merge、非严格 merge、带 prefix 的 merge）各改一次嵌套字段，把 4.3.2 的流程图在脑子里跑一遍。

**操作步骤**：

```python
# 文件名：my_merge_demo.py（示例代码，非项目原有文件）
from dataclasses import dataclass

from olmo_core.config import Config


@dataclass
class OptimConfig(Config):
    lr: float = 1e-3
    weight_decay: float = 0.1


@dataclass
class MyExperimentConfig(Config):
    exp_name: str = "base"
    optim: OptimConfig = OptimConfig()


if __name__ == "__main__":
    c = MyExperimentConfig()

    # 姿势一：普通 dotlist
    a = c.merge(["optim.lr=6e-3"])
    assert a.optim.lr == 6e-3 and c.optim.lr == 1e-3  # 原对象不变

    # 姿势二：-- 前缀 + 无值开关 + 非严格模式
    b = c.merge(["--exp-name=exp2", "--optim.weight_decay=0.0", "--typo.field=1"], strict=False)
    assert b.exp_name == "exp2" and b.optim.weight_decay == 0.0

    # 姿势三：prefix 只取子树
    d = c.merge(["--my.optim.lr=3e-4", "--exp-name=ignored"], prefix="my")
    assert d.optim.lr == 3e-4 and d.exp_name == "base"
```

**需要观察的现象**：三种姿势返回的都是新对象；姿势二里 `--typo.field` 在 `strict=False` 下被丢弃、换成默认 `strict=True` 再跑会报 `ValueError`（`_set_nested` 下钻失败）；姿势三里不带前缀的 `exp-name` 条目被忽略。

**预期结果**：断言全部通过——三个姿势分别一一对应 [config_test.py:L72-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L72-L85)、[L88-L101](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L88-L101) 与 [L40-L41](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L40-L41) 已验证的行为。本环境未实际运行，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`merge(["model.block_overrides.3.attention.dropout=0.1"])` 为什么能合法地用数字下标？

**参考答案**：`block_overrides` 是 dict，序列化后数字键变字符串；`from_dict` 侧有数字字符串键转 int 的 HACK（[config.py:L260-L262](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L260-L262)），`merge` 侧的 `_set_nested` 对 dict 用字符串键、对 list 用 `int(key)` 下标（[config.py:L330-L345](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L330-L345)），两侧配合使数字路径始终可用。

**练习 2**：为什么 `merge` 要先 `as_dict(include_class_name=True, include_registered_name=True)` 再改再 `from_dict`，而不是直接在 dataclass 上改字段？

**参考答案**：在 dict 上「点号路径下钻 + 赋值」比在强类型 dataclass 上做同样的事简单得多（不用反射 setattr、不用处理嵌套构造）；而带上 `_CLASS_`/`"type"` 后，`from_dict` 能把每个节点还原成**原本的子类**（[config.py:L269-L288](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L269-L288)），多态信息不丢——这也正是下一讲（u2-l2 Registrable）的入口问题。

## 5. 综合实践

**任务：做一次「配置即代码」的模型变体实验。** 把本讲三个模块串起来：定义 → 存档 → 往返 → merge 变体 → 对比参数量。

```python
# 文件名：my_variant_experiment.py（示例代码，非项目原有文件）
from dataclasses import dataclass

import yaml

from olmo_core.config import Config
from olmo_core.nn.transformer.config import TransformerConfig


@dataclass
class TrainSettings(Config):
    lr: float = 3e-4
    duration: str = "10000stp"


@dataclass
class MyExperimentConfig(Config):
    exp_name: str = "olmo2-190M-base"
    train: TrainSettings = TrainSettings()
    model: TransformerConfig = None


if __name__ == "__main__":
    # 1. 基线配置
    base = MyExperimentConfig(
        exp_name="olmo2-190M-base",
        train=TrainSettings(lr=3e-4),
        model=TransformerConfig.olmo2_190M(vocab_size=50304),
    )

    # 2. merge 出一个「小一点 + 学习率不同」的变体（一条链上改多层嵌套）
    variant = base.merge(
        ["exp_name=olmo2-190M-small", "train.lr=1e-3", "model.n_layers=6"]
    )

    # 3. 不构建模型，直接从 Config 读参数量（ModelConfig 的能力）
    print(f"{base.exp_name}:    {base.model.num_params / 1e6:.1f}M params")
    print(f"{variant.exp_name}: {variant.model.num_params / 1e6:.1f}M params")

    # 4. 把两份配置都存档，肉眼对比 YAML 差异
    for cfg in (base, variant):
        with open(f"{cfg.exp_name}.yaml", "w") as f:
            yaml.safe_dump(cfg.as_config_dict(), f)

    # 5. 验证变体配置能原样读回（checkpoint 恢复的前提）
    reloaded = MyExperimentConfig.from_yaml("olmo2-190M-small.yaml")
    assert reloaded == variant
```

**验收标准**：

1. 两个参数量都打印成功，且 variant 明显更小（`n_layers` 从 12 降到 6）——依据 [nn/config.py:L23-L27](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L23-L27) 的 `num_params` 抽象属性，`TransformerConfig` 的实现在 [transformer/config.py:L507](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L507)；
2. `diff` 两个 YAML 文件，差异恰好是 `exp_name`、`train.lr`、`model.n_layers` 三处（外加 `model` 节点里受层影响的派生内容）；
3. `reloaded == variant` 通过。

注意：`merge(["model.n_layers=6"])` 只改配置树的这个字段，不会触发 `olmo2_190M` 工厂方法重新计算配套超参——工厂方法里的 `kwargs.pop("n_layers", 12)` 只在**构造时**生效。所以「改配置」和「换工厂预设」是两种不同的操作，后者才保证超参自洽。本综合实践在本环境未实际运行，待本地验证。

## 6. 本讲小结

- `Config`（[config.py:L55-L71](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L55-L71)）是全库配置的地基：子类只需声明字段，就免费获得序列化、嵌套组合与 dotlist 覆盖；子类必须重新加 `@dataclass`，字段只能用简单类型或嵌套 Config。
- 序列化出口有两个：`as_dict()`（无损、程序内用）和 `as_config_dict()`（JSON 安全、带 `_CLASS_`/`type`、官方存档格式，[experiment.py:L455-L457](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/internal/experiment.py#L455-L457) 用它记录每次实验）；入口有 `from_dict` / `from_yaml` / `from_json` / `from_file`，`from_dict` 靠 `_CLASS_` 动态 import 还原确切子类。**Config 没有 `save()` 方法**，写盘要自己 `yaml.safe_dump(as_config_dict())`。
- `merge(dotlist)` 走「转 dict → `_set_nested` 下钻改值 → `from_dict` 重建」的路线，返回新对象；`_clean_opt` 让它天然兼容 `--foo-bar=x` 风格的命令行参数，值为空默认 `true`，值用 YAML 规则解析。
- 继承链 `Config → ModuleConfig → ModelConfig → TransformerConfig` 展示了地基如何长出 `build()` 和 `num_params` 等模型侧能力——配置不只是记录，还是「图纸」。
- 所有行为断言都有测试背书：[src/test/config_test.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py) 的五个测试分别覆盖 as_dict、嵌套往返、json_safe、非严格 merge、prefix merge。

## 7. 下一步学习建议

下一讲 **u2-l2「Registrable 与多态配置字段」**：本讲反复出现的 `"type"` 字段和「注册名」到底是什么？当一个字段的类型是抽象基类（比如优化器 `OptimConfig`）时，YAML 里一行 `type: adamw` 如何在运行时解析成 `AdamWConfig`？这就是 `dataclass_extensions` 的 `Registrable` 混入（在 [config.py:L24-L25](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L24-L25) 引入并再导出）要解决的问题。

再往后 **u2-l3** 会把本讲的 `merge` + `_clean_opt` 接到真实命令行上，讲清 `--train_module.optim.lr=6e-3` 从 `sys.argv` 到配置树的完整链路。

阅读源码时建议带着两个问题：(1) `from_dict` 里 `_CLASS_` 与 `"type"` 两个机制为什么要分开存在（提示：一个还原确切类，一个走注册表选择子类）？(2) 如果让你给 `Config` 加一个 `save(path)` 方法，你会基于 `as_dict` 还是 `as_config_dict` 实现，为什么？
