# 配置系统：Python 配置文件与 --opts 覆盖机制

## 1. 本讲目标

学完本讲，你应该能够：

- 解释 `ConfigNode` 如何让普通 `dict` 支持 `cfg.train.lr` 这种属性式访问，以及它为什么对整个配置体系如此重要。
- 说出 `load_config` 是如何用一个 `.py` 文件「动态导入」出配置对象的，理解「配置即代码」这个设计取舍。
- 独立使用 `--opts "train.lr=3e-4"` 这类点路径覆盖任意嵌套配置字段，并能预测值会被解析成什么 Python 类型。
- 理解 `finalize_cfg` 钩子的执行时机，以及它派生出的 `checkpoint_dir` / `tensorboard_dir` 如何在保存 checkpoint 时被回写成一个可复现的 `train_config.py`。

本讲是第 1 单元的收尾：前面三讲已经知道了仓库结构（u1-l2）和入口自举（u1-l3），本讲把「入口拿到的那个 `args` 对象到底从哪来、长什么样」彻底讲透。

## 2. 前置知识

- **dict 的属性访问问题**：Python 里 `d["a"]` 是键访问，`d.a` 是属性访问，默认互不相通。配置对象层级深（`cfg["train"]["lr"]`），如果每个使用点都写方括号字符串，代码既难读又容易拼错键名。
- **「配置即代码」（config as code）**：传统项目用 YAML/JSON 存配置，但 YAML 表达不了「一个 Python 类」。DeepSpec 的配置文件直接是 Python 模块——所以配置里能写 `trainer_cls=Qwen3DSparkTrainer`，`train.py` 拿到后直接实例化。
- **动态导入**：`importlib` 可以在运行时按文件路径加载一个 `.py` 文件并执行它，不要求该文件在 `sys.path` 里。`load_config` 的核心就是这一行。
- **YAML 标量解析**：字符串 `"2048"`、`"true"`、`"None"` 需要被转换成 `int`、`bool`、`None` 才有用。DeepSpec 复用 `yaml.safe_load` 做这件事（命令行参数永远是字符串）。
- **点路径（dotted path）**：`train.lr=3e-4` 里的 `train.lr` 表示「顶层键 `train` 里的子键 `lr`」，即 `cfg["train"]["lr"]`。这是 `--opts` 的寻址语法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/utils/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py) | 配置系统全部核心：`ConfigNode`、`load_config`、`finalize_config`、`parse_opts_to_config` |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 一份真实配置文件样例：`model` / `train` / `logging` / `data` 四个字典 + `finalize_cfg` 钩子 |
| [deepspec/utils/constant/public.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py) | 公开环境的集中式路径与模型名常量（`BASE_CKPT_DIR`、`QWEN_3_4B` 等） |
| [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py) | 入口：`--config` + `--opts` 如何变成配置对象 |
| [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) | 启动脚本：`--opts` 的实战用法示例 |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | 保存 checkpoint 时把原始配置 + `--opts` 回写成 `train_config.py` |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 配置的消费方：`args.logging.checkpoint_dir`、`float(args.train.lr)` 等 |

## 4. 核心概念与源码讲解

### 4.1 ConfigNode：让 dict 支持属性访问

#### 4.1.1 概念说明

训练代码里到处要读配置，例如学习率、批大小、缓存路径。如果配置是普通 `dict`，每个读取点都要写 `self.args["train"]["lr"]`——三层方括号、三个字符串、零检查。`ConfigNode` 是一个 `dict` 子类，把「属性访问」重定向到「键访问」，于是可以写 `self.args.train.lr`。

它的价值有三点：

1. **可读性**：`args.model.target_layer_ids` 一眼可读，IDE 还能提示第一层键。
2. **统一类型**：配合 `to_config_node` 递归转换，配置树里任何深度的嵌套 dict 都是 `ConfigNode`，任何位置都能用属性语法。
3. **零迁移成本**：它仍是 `dict`，`json.dumps`、`cfg.get(...)`、`cfg["train"]` 全部照常工作。

#### 4.1.2 核心流程

```text
cfg = ConfigNode({"train": ConfigNode({"lr": 0.0006})})
读:  cfg.train.lr        → __getattr__("train") 先执行 → 返回内层 ConfigNode
                        → 再对内层 __getattr__("lr")   → 返回 0.0006
写:  cfg.train.lr = 1e-3 → 内层 __setattr__("lr", 1e-3) → self["lr"] = 1e-3（写进 dict）

键不存在时:
    cfg.train.typo       → 内层 self["typo"] 抛 KeyError
                        → 被 __getattr__ 捕获，转抛 AttributeError("typo")
```

关键规则只有两条：**读走 `__getattr__`（键访问的语法糖），写走 `__setattr__`（直接改 dict）**。因此「属性赋值」和「键赋值」完全等价——这一点在 4.3 讲 `_origin_opts` 的注入时会再用到。

#### 4.1.3 源码精读

类定义本体只有 12 行：

[deepspec/utils/config.py:L11-L22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L11-L22) —— `ConfigNode` 继承 `dict`；`__getattr__` 尝试 `self[item]`，`KeyError` 被转成 `AttributeError`（这是 Python 属性协议的惯例，能让 `hasattr(cfg, "x")` 正确返回 `False` 而不是抛异常）；`__setattr__` 直接写入 dict 键；`copy()` 保证浅拷贝结果仍是 `ConfigNode` 而不会退化成普通 `dict`。

属性语法要能用到任意深度，靠的是递归转换：

[deepspec/utils/config.py:L25-L34](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L25-L34) —— `to_config_node` 对 `ConfigNode`/`dict` 递归重建为 `ConfigNode`，对 `list`/`tuple` 递归处理每个元素，其他标量（int、str、类、函数……）原样返回。这样 `cfg.model` 里的 dict、乃至「list 里套 dict」的任意嵌套都支持属性访问。

配套的还有反向转换与序列化工具：

[deepspec/utils/config.py:L49-L60](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L49-L60) —— `jsonable` 把 `Path`、`tuple` 等转成 JSON 友好类型。

[deepspec/utils/config.py:L63-L77](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L63-L77) —— `CustomJSONEncoder` 让 `json.dumps` 能打印配置里「不是数据」的东西：函数渲染成 `<function 名字>`、类渲染成 `<class '名字'>`、`torch.dtype` 和 `Path` 转字符串。它被 [train.py:L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L35) 用来在训练启动时把完整配置打印到日志，`trainer_cls` 这一栏会显示成类名字符串。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `ConfigNode` 的读写语义与报错行为。
2. **操作步骤**：在仓库根目录（已 `pip install -r requirements.txt`）运行 `python` 进入交互环境，逐行执行（示例代码）：

   ```python
   from deepspec.utils.config import ConfigNode, to_config_node

   cfg = to_config_node({"train": {"lr": 6e-4}, "model": {"layers": [1, 9, 17]}})
   print(type(cfg.train))            # <class 'deepspec.utils.config.ConfigNode'>
   print(cfg.train.lr)               # 0.0006
   print(cfg.model.layers)           # [1, 9, 17]

   cfg.train.lr = 3e-4               # 属性写
   print(cfg["train"]["lr"])         # 0.0003 —— 与键写完全等价

   print(hasattr(cfg.train, "typo")) # False（KeyError 被转成 AttributeError）
   print(cfg.train.typo)             # AttributeError: typo
   print(cfg.get("train"))           # 属性语法不影响 dict 原生方法
   ```

3. **需要观察的现象**：属性读写最终都落在 dict 上；访问不存在的键得到的是 `AttributeError` 而非 `KeyError`。
4. **预期结果**：如注释所示。`to_config_node` 把嵌套 dict 全部变成 `ConfigNode`，而 `layers` 列表保持普通 `list`（列表本身没有属性访问问题）。
5. 上述输出为「待本地验证」——请在本地跑一遍核对打印值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__getattr__` 要把 `KeyError` 转换成 `AttributeError`？

**答案**：因为 `cfg.train` 走的是属性访问协议。Python 的约定是：属性不存在应抛 `AttributeError`，这样 `hasattr()`、`getattr(x, "k", default)` 等内建工具才能正确工作；若抛 `KeyError`，`hasattr(cfg, "train")` 会直接崩溃而不是返回 `False`。

**练习 2**：`to_config_node` 为什么连 `list`、`tuple` 里的元素也要递归？

**答案**：嵌套 dict 可能藏在列表里（例如 `data` 里若有一个「每项都是 dict 的列表」）。只有递归进去，列表内部的 dict 才会变成 `ConfigNode`，属性访问在任意深度都成立。注意细节：`to_config_node` 保持 `tuple` 为 `tuple`，而反向的 `config_to_plain_dict`（[deepspec/utils/config.py:L37-L46](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L37-L46)）把 `tuple` 转成 `list`——因为 JSON 没有 tuple 类型。

**练习 3**：执行 `config._origin_config_path = "/x/y.py"` 时，走的是普通属性赋值还是别的路径？

**答案**：走 `ConfigNode.__setattr__`，即 `self["_origin_config_path"] = "/x/y.py"`——最终是往 dict 里插入一个顶层键。所以这个「看起来像内部字段」的东西会出现在 `json.dumps(config)` 的输出里（见 [train.py:L26-L27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L26-L27)）。

### 4.2 load_config：把 Python 文件变成配置对象

#### 4.2.1 概念说明

`load_config(path)` 接收一个配置文件路径（如 `config/dspark/dspark_qwen3_4b.py`），返回一个 `ConfigNode`。它解决三个问题：

1. **按路径加载**：配置文件不在 `deepspec` 包里，不在 `sys.path` 上，普通 `import` 做不到，必须用 `importlib` 按文件位置加载。
2. **「收集模块顶层变量」的约定**：执行完这个模块后，把所有非下划线开头、非模块类型的顶层名字收进一个 dict。配置作者只要在文件顶层写 `train = dict(...)`，使用者就能拿到 `cfg.train`。
3. **「配置即代码」**：配置文件可以 import Python 类（`Qwen3DSparkTrainer`）、常量（`BASE_CKPT_DIR`），甚至定义函数（`finalize_cfg`）。代价是配置文件必须能被成功 import——依赖没装好时连配置都读不了。

#### 4.2.2 核心流程

```text
load_config("config/dspark/dspark_qwen3_4b.py")
  ├─ importlib.util.spec_from_file_location(module_name, path)   # 按文件路径构造模块规格
  ├─ module_from_spec + spec.loader.exec_module(module)           # 真正执行配置文件
  ├─ for name in dir(module):                                     # 收集顶层名字
  │     跳过 "__" 开头（dunder）与 ModuleType（import 进来的模块）
  └─ to_config_node(config)                                       # 递归包成 ConfigNode
```

配置文件本身的结构（以 DSpark + Qwen3-4B 为例）：

| 顶层键 | 内容摘要 | 谁来消费 |
| --- | --- | --- |
| `project_name` / `exp_name` / `seed` | 实验标识与随机种子 | `finalize_cfg`、`train.py` 的 `seed_all` |
| `model` | 目标模型、`block_size=7`、`num_draft_layers=5`、`target_layer_ids`、损失权重等 | `build_draft_config` / trainer |
| `train` | `trainer_cls`、`lr`、批大小、epoch、FSDP/compile 开关 | `BaseTrainer` |
| `logging` | `logging_steps`、`checkpointing_steps`（`finalize_cfg` 会往里补两个目录） | `training_logger`、`ckpt_manager` |
| `data` | `target_cache_path=None`、`chat_template="qwen"`、`max_length=4096` | 数据集构建 |
| `finalize_cfg` | 一个函数，留待 `finalize_config` 调用 | `parse_opts_to_config` 的收尾 |

#### 4.2.3 源码精读

加载器本体：

[deepspec/utils/config.py:L84-L98](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L84-L98) —— `load_config` 先取绝对路径、以文件名 stem 作为模块名，用 `importlib.util.spec_from_file_location` + `exec_module` 执行该文件；然后遍历 `dir(module)`，跳过 `__` 开头的名字和 `ModuleType` 类型的值（否则 `import os` 会把整个 `os` 模块收进配置），其余顶层名字全部收进 dict，最后 `to_config_node` 包装返回。

一份真实配置文件的头部：

[config/dspark/dspark_qwen3_4b.py:L1-L8](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L1-L8) —— 顶部 `from deepspec.trainer import Qwen3DSparkTrainer` 与 `from deepspec.utils.constant import BASE_CKPT_DIR, BASE_TB_DIR, QWEN_3_4B`。这两个 import 正是「配置即代码」的体现：`Qwen3DSparkTrainer` 这个**类本身**会成为配置里的一个值；`QWEN_3_4B` 等常量则避免在 12 份配置里重复硬编码模型名。注意这些 import 的名字会被 `load_config` 的过滤规则正确处理——模块（`os`）被排除，类和常量被保留。

配置的四个核心字典：

[config/dspark/dspark_qwen3_4b.py:L32-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45) —— `train` 字典，第一项就是 `trainer_cls=Qwen3DSparkTrainer`；[train.py:L36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L36) 用 `args.train.trainer_cls(local_rank, args)` 直接实例化它——入口完全不关心具体算法，换算法只换 `--config`。

[config/dspark/dspark_qwen3_4b.py:L52-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L52-L57) —— `data` 字典里 `target_cache_path=None` 是刻意的：巨大目标缓存（u1-l1 提过约 38 TB 量级）的路径必须通过 `--opts` 在启动时注入（见 train.sh），配置文件里不写死。

常量从哪来：

[deepspec/utils/constant/public.py:L4-L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L4-L12) —— 公开环境的常量层：`CACHE_DIR`、四个目标模型名（`QWEN_3_4B = "Qwen/Qwen3-4B"` 等）、`BASE_TB_DIR = ~/tensorboard`、`BASE_CKPT_DIR = ~/checkpoints`。

[deepspec/utils/constant/__init__.py:L1-L5](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/__init__.py#L1-L5) —— 常量层的环境切换：`try: import hfai → from .internal import *`，失败则 `from .public import *`。即内部集群环境用 `internal.py` 的路径常量，公开环境回落到 `public.py`——同一份配置文件在两种环境都能跑，只是 `BASE_CKPT_DIR` 等常量的取值不同。

#### 4.2.4 代码实践

1. **实践目标**：加载真实配置，观察「模块顶层变量 → ConfigNode 键」的映射。
2. **操作步骤**：仓库根目录下执行（示例代码）：

   ```python
   from deepspec.utils.config import load_config

   cfg = load_config("config/dspark/dspark_qwen3_4b.py")
   print(sorted(k for k in cfg if not k.startswith("_")))
   # 期望包含: project_name, exp_name, seed, model, train, logging, data, finalize_cfg

   print(cfg.train.trainer_cls)   # <class '...Qwen3DSparkTrainer'> —— 类是值
   print(cfg.model.block_size)    # 7
   print(cfg.data.max_length)     # 4096
   print("os" in cfg)             # False —— import 的模块被过滤掉了
   print(cfg.logging.get("checkpoint_dir"))  # None —— 此时 finalize 还没跑
   ```

3. **需要观察的现象**：`os`（配置文件 import 过的模块）没有出现在配置里；`trainer_cls` 是一个真实的类对象；`logging.checkpoint_dir` 尚不存在（要等 4.3 的 `finalize_config` 派生）。
4. **预期结果**：如注释所示，具体打印以本地为准（待本地验证）。
5. 注意：本脚本必须能 import `torch`/`transformers`（配置文件顶部 import 了 trainer），所以要在装好 `requirements.txt` 的环境中运行。

#### 4.2.5 小练习与答案

**练习 1**：如果 `load_config` 不跳过 `ModuleType`，会发生什么？

**答案**：配置文件里 `import os` 之后，`os` 这个名字会出现在 `dir(module)` 里且不以 `__` 开头，整个 `os` 模块会被收进配置 dict，`json.dumps` 打印启动配置时也会试图序列化它。过滤后配置只含「作者显式写下的数据与可调用对象」。

**练习 2**：把 `trainer_cls` 放进配置文件，好处和代价各是什么？

**答案**：好处是入口 `train.py` 完全算法无关——DSpark/Eagle3/DFlash 只差一个 `--config` 参数，不需要 if/else 分发（对比 `eval.py` 需要按 `architectures` 查 `EVALUATORS` 字典，因为 eval 的输入是 HF checkpoint 而非配置文件）。代价是配置不可序列化为纯 JSON、依赖必须装好、且配置文件能执行任意代码（在这个「研究者自己写配置」的场景里是可接受的）。

**练习 3**：`finalize_cfg` 在 `load_config` 阶段被执行了吗？

**答案**：没有。`load_config` 只是把 `finalize_cfg` 这个函数**作为值**收进配置（`cfg.finalize_cfg` 是 callable）。真正调用它的是 `finalize_config`（4.3 节），而后者在 `parse_opts_to_config` 的收尾处才被触发——即「先让 `--opts` 覆盖完原始字段，最后才派生」。

### 4.3 --opts 点路径覆盖与 finalize_cfg 钩子

#### 4.3.1 概念说明

`--opts` 解决的问题是：**不改配置文件地改配置**。target cache 路径每台机器不同、学习率要扫参、批大小看显存——这些都适合在命令行临时覆盖。整体流水线可以写成一次函数复合：

\[ \mathrm{cfg}_{\text{final}} \;=\; \mathrm{finalize}\bigl(\mathrm{apply}(\mathrm{opts},\ \mathrm{load}(\mathrm{config\_path}))\bigr) \]

即「加载 → 逐条覆盖 → 派生收尾」三步，全部发生在 [train.py:L25](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L25) 的 `parse_opts_to_config(args.opts, load_config(args.config))` 一行里。

`finalize_cfg` 钩子解决的问题是：**派生字段不该手写**。`checkpoint_dir` 由 `BASE_CKPT_DIR/project_name/exp_name` 拼出来、`tensorboard_dir` 同理——写在 `finalize_cfg` 里，改 `exp_name`（哪怕是通过 `--opts` 改的）目录就自动跟着变，不会出现「实验名改了、日志还写进旧目录」的事故。

而保存 checkpoint 时，这套机制还有第四步「回写」：把**原始配置文件 + `--opts` 覆盖记录**存进 checkpoint，让任何一次运行都可以被精确复现。

#### 4.3.2 核心流程

```text
train.py parse_args:
  --config config/dspark/dspark_qwen3_4b.py
  --opts "data.target_cache_path=/mnt/cache"      (action="append"，可重复)
  --opts "train.lr=3e-4"
        │
        ▼
parse_opts_to_config(opts, cfg):
  for 每条 opt:
      name, value = opt.split("=", 1)      # 只切第一个 =，值里可以再含 =
      parts = name.split(".")              # "train.lr" → ["train", "lr"]
      沿 parts[:-1] 逐层下钻:
          中间键不存在      → KeyError("Unknown config key in --opts: ...")
          中间键不是 mapping → TypeError("... is not a mapping")
      末键不存在             → KeyError（不允许凭空新建键！）
      current[末键] = to_config_node(yaml.safe_load(value))   # 字符串 → 标量
        │
        ▼
finalize_config(cfg):
  若 cfg["finalize_cfg"] 可调用 → cfg = finalize_cfg(cfg)
      派生 logging.checkpoint_dir / tensorboard_dir
        │
        ▼
train.py 回填来源信息: cfg._origin_config_path / cfg._origin_opts（两个顶层键）
        │
        ▼
保存 checkpoint 时 (ckpt_manager):
  把 _origin_config_path 指向的原文件复制为 checkpoint/train_config.py
  再把每条 opt 追加为一行 Python 赋值: train['lr'] = 0.0003
```

值得单独强调的两条设计纪律：

- **只允许覆盖已有键**。`--opts "model.new_key=1"` 直接 `KeyError`。这是刻意的防错：拼错键名（`train.lrr`）会立刻报错，而不是静默无效然后让你困惑为什么学习率没变。
- **值统一走 `yaml.safe_load`**。命令行参数是字符串，`"2048"` 要变 `int`、`"true"` 要变 `bool`、`"None"` 要变 `None`。注意科学计数法的一个经典坑：PyYAML 的隐式 float 解析通常要求带小数点，`3e-4` 可能被解析成**字符串** `'3e-4'` 而非浮点数——`train.sh` 的注释示例恰恰用的是 `3e-4`。稳妥写法是 `0.0003`。此行为与安装的 PyYAML 版本有关，标注「待本地验证」，实践环节会专门验证它。

#### 4.3.3 源码精读

入口侧的参数定义与三步流水线：

[train.py:L20-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L20-L28) —— `--config` 必填；`--opts` 用 `action="append"` 收成列表（同一旗标可重复多次）；第 25 行完成「加载→覆盖→派生」；第 26-27 行把**原始配置路径**与**原始 opts 列表**作为两个顶层键挂回配置——`ConfigNode.__setattr__` 使这个赋值等价于 `cfg["_origin_opts"] = ...`。这两个键就是回写机制的「证据链」。

覆盖机制的完整实现：

[deepspec/utils/config.py:L113-L131](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L113-L131) —— `parse_opts_to_config`。注意三个细节：(1) `opts` 为空时**也要**走 `finalize_config`——派生是无条件收尾；(2) `split("=", 1)` 只切第一个等号；(3) 覆盖值经过 `_parse_scalar` 再 `to_config_node`，所以写进来的若是 dict 字面量也会被正确包装。

[deepspec/utils/config.py:L80-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L80-L81) —— `_parse_scalar` 就是一行 `yaml.safe_load`，复用 YAML 的标量类型推断。

钩子的通用调用逻辑：

[deepspec/utils/config.py:L101-L110](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/config.py#L101-L110) —— `finalize_config`：若配置里存在可调用的 `finalize_cfg` 就以整个 cfg 调用它，返回值非 `None` 则采用返回值，最后再 `to_config_node` 重新包装一遍（钩子里可能塞入了普通 dict）。

钩子在 DSpark 配置里的具体实现：

[config/dspark/dspark_qwen3_4b.py:L60-L68](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L60-L68) —— `finalize_cfg` 从 cfg 读 `project_name`/`exp_name`，与 `BASE_CKPT_DIR`、`BASE_TB_DIR` 拼出 `logging.checkpoint_dir = ~/checkpoints/deepspec/dspark_block7_qwen3_4b` 与 `tensorboard_dir`，写回 `cfg["logging"]` 后返回 cfg。

派生结果的消费点：

[deepspec/trainer/base_trainer.py:L161-L173](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L161-L173) —— `BaseTrainer.__init__` 第 162 行读 `self.args.logging.checkpoint_dir` 作为断点发现与存盘的根目录，第 170-173 行把 `tensorboard_dir` 传给 `training_logger.init`——这就是 `finalize_cfg` 派生字段的最终去向。

shell 侧的实战语法：

[scripts/train/train.sh:L27-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L27-L40) —— 注释明确写了规则「`--opts` 可按点路径覆盖任意配置字段，值按 Python 标量（int/float/bool/str）解析，重复旗标设多个字段」，并给出 `data.target_cache_path`、`train.lr=3e-4`、`train.local_batch_size=4` 三个示例；第 38-40 行的真实命令用 `--opts "data.target_cache_path=..."` 注入缓存路径。

保存时的回写（`finalize_cfg` 与 checkpoint 的关联）：

[deepspec/trainer/ckpt_manager.py:L32-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L32-L45) —— `save_train_config` 把 `_origin_config_path` 指向的**原配置文件**逐字节复制进 checkpoint 目录、命名为 `train_config.py`；若 `_origin_opts` 非空，再在文件末尾追加 `# --opts overrides applied at save time` 注释和逐条赋值行。注意回写的是「原始文件 + 追加」，而不是序列化后的配置——因此 `finalize_cfg` 函数体本身也在存档里，加载这份存档时派生逻辑会原样重跑。

[deepspec/trainer/ckpt_manager.py:L48-L53](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L48-L53) —— `_render_opt_assignment` 把 `"train.lr=3e-4"` 渲染成一行合法 Python：`train['lr'] = 0.0003`。键部分把点路径逐段转成下标访问；值部分同样用 `yaml.safe_load` 解析后取 `repr`。这些追加行在模块顶层执行，发生在 `finalize_cfg` **被调用**之前（调用发生在加载方的 `finalize_config` 里），所以「先应用覆盖、后派生」的顺序在存档重放时依然成立。

一个值得记住的防御细节：即便 `lr` 意外被解析成字符串，[deepspec/trainer/base_trainer.py:L214-L220](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L214-L220) 构造 `BF16Optimizer` 时写的是 `lr=float(self.args.train.lr)`——显式 `float()` 兜底。但这是逐字段的人工防御，其他 float 字段未必都有，所以理解 `_parse_scalar` 的类型行为仍然重要。

#### 4.3.4 代码实践

本讲的主实践（对应任务书）——一个独立脚本，完整走一遍「加载 → 覆盖 → 派生 → 报错观察」：

1. **实践目标**：验证 `--opts` 覆盖、`finalize_cfg` 派生与错误行为，并实测科学计数法的类型坑。

2. **操作步骤**：在仓库根目录创建 `tmp_opts_demo.py`（示例代码，可放在仓库任意临时位置，勿提交）：

   ```python
   from deepspec.utils.config import load_config, parse_opts_to_config, _parse_scalar

   cfg = load_config("config/dspark/dspark_qwen3_4b.py")
   print("before:", cfg.train.lr, cfg.data.max_length, cfg.logging.get("checkpoint_dir"))

   opts = ["train.lr=0.0003", "data.max_length=2048"]
   cfg = parse_opts_to_config(opts, cfg)
   print("after: ", cfg.train.lr, type(cfg.train.lr).__name__,
         cfg.data.max_length, type(cfg.data.max_length).__name__)
   print("derived checkpoint_dir:", cfg.logging.checkpoint_dir)
   print("derived tensorboard_dir:", cfg.logging.tensorboard_dir)

   # 科学计数法坑：分别解析两种写法，对比类型
   print("yaml '3e-4'  ->", repr(_parse_scalar("3e-4")))
   print("yaml '0.0003'->", repr(_parse_scalar("0.0003")))

   # 故意写一个不存在的键，观察报错
   try:
       parse_opts_to_config(["train.lrr=1.0"], cfg)
   except KeyError as exc:
       print("KeyError:", exc)
   try:
       parse_opts_to_config(["model.sub.not_exist=1"], cfg)   # model.sub 本就不是 mapping
   except (KeyError, TypeError) as exc:
       print(type(exc).__name__, ":", exc)
   ```

   运行 `python tmp_opts_demo.py`。

3. **需要观察的现象**：
   - 覆盖前 `cfg.logging` 里没有 `checkpoint_dir`；覆盖并 finalize 后出现，形如 `~/checkpoints/deepspec/dspark_block7_qwen3_4b`。
   - `max_length` 变成 `int 2048`；`lr=0.0003` 是 `float`。
   - `_parse_scalar("3e-4")` 的返回类型——如果是 `str`，说明你的 PyYAML 版本确实不带小数点不解析为 float（这正是要避开的坑）；如果是 `float`，记录下版本号。
   - 不存在的键抛 `KeyError: 'Unknown config key in --opts: train.lrr'`；中间键不是 mapping 时抛 `TypeError`。
4. **预期结果**：覆盖与派生部分如上；科学计数法与具体报错文本「待本地验证」——请以实际输出为准。
5. 完成后删除临时脚本，保持仓库干净。

#### 4.3.5 小练习与答案

**练习 1**：`--opts "train.lr=3e-4"` 和 `--opts "train.lr=0.0003"` 效果一定一样吗？

**答案**：不一定。两者都会成功覆盖 `train.lr`，但值都要经过 `yaml.safe_load`；PyYAML 的隐式解析通常要求 float 带小数点，`3e-4` 可能得到字符串 `'3e-4'`。对 `lr` 这个字段有 `float(...)` 兜底所以侥幸无害，但换一个没有兜底的 float 字段就可能出问题。结论：优先写 `0.0003`（或先本地验证你的 PyYAML 行为）。

**练习 2**：为什么 `parse_opts_to_config` 在 `opts` 为空时也要调用 `finalize_config`？

**答案**：`finalize_cfg` 派生 `checkpoint_dir`/`tensorboard_dir` 是配置生效的必要步骤，与「是否发生覆盖」无关。若只在有 opts 时 finalize，无覆盖启动会缺目录字段，`BaseTrainer` 读 `args.logging.checkpoint_dir` 时就会炸。

**练习 3**：checkpoint 里回写的 `train_config.py` 为什么选择「复制原文件 + 追加赋值行」，而不是把最终配置 dump 成 Python/YAML？

**答案**：(1) 原文件里有 `finalize_cfg` 函数和 import，dump 会丢失这些「代码」部分；(2) 追加的 `train['lr'] = 0.0003` 在模块顶层执行、且发生在 `finalize_cfg` 被调用之前，重放时自动保持「先覆盖后派生」的语义；(3) diff 友好——存档与原始配置文件的差异就是那几行追加，一眼看出这次运行改了什么。

**练习 4**：如果用 `--opts "exp_name=my_ablation"` 启动，checkpoint 会存到哪里？

**答案**：`finalize_cfg` 在覆盖**之后**运行，读到的 `exp_name` 已是 `my_ablation`，因此目录为 `~/checkpoints/deepspec/my_ablation`（`BASE_CKPT_DIR` 取自当前环境的常量层）。这也解释了为什么 `load_training_state` 提示强制重开训练的办法是「改 exp_name 或删 step_latest」——换名字就换了目录，自然从零开始。

## 5. 综合实践

**任务：复刻一次「配置存档与重放」迷你流水线**（无 GPU、无真实训练，纯配置层）。

要求写一个脚本 `tmp_roundtrip_demo.py`（示例代码），模拟 `train.py` 启动 + `ckpt_manager` 回写的完整闭环：

1. 用 `load_config` 加载 `config/eagle3/eagle3_qwen3_4b.py`（换一份配置，检验你对任意配置的泛化理解）。
2. 模拟命令行：`opts = ["data.max_length=2048", "train.num_train_epochs=1"]`，调用 `parse_opts_to_config` 得到最终 cfg，记录 `cfg.logging.checkpoint_dir`。
3. 模拟回写：用 `shutil.copy` 把原配置复制到 `tmp_ckpt/train_config.py`，再仿照 `_render_opt_assignment`（或直接 import 它）把每条 opt 追加为一行 `键['子键'] = 值` 赋值。
4. 重放：用 `load_config` 加载 `tmp_ckpt/train_config.py`，再 `parse_opts_to_config([], cfg)`（空 opts 只触发 finalize）。
5. 断言：重放后的 `data.max_length`、`train.num_train_epochs`、`logging.checkpoint_dir` 与第 2 步的结果完全一致。

**预期结果**：两边逐字段相等——这证明了「原始文件 + 追加赋值」的存档能无损还原一次运行的有效配置，正是断点续训（u3-l5 将详讲）能拿到一致配置的原因。目录操作请用临时路径并在结束后清理。整个流程「待本地验证」。

## 6. 本讲小结

- `ConfigNode` 是 `dict` 子类：读走 `__getattr__`（键访问语法糖，`KeyError`→`AttributeError`），写走 `__setattr__`（直接改 dict），配合 `to_config_node` 递归包装后任意深度都支持 `cfg.train.lr` 式访问。
- `load_config` 用 `importlib` 按路径执行配置文件，收集所有非 dunder、非模块的顶层名字——因此配置里可以放类（`trainer_cls`）、常量和函数，这是「配置即代码」的根基。
- 集中式常量层 `deepspec/utils/constant/` 通过 try-import 在内部环境（hfai/internal）与公开环境（public）间切换，`BASE_CKPT_DIR`、`QWEN_3_4B` 等常量被 12 份配置共享。
- `--opts` 的点路径覆盖由 `parse_opts_to_config` 实现：逐段下钻、只允许覆盖已有键（拼错立刻 `KeyError`）、值经 `yaml.safe_load` 做标量类型推断（注意 `3e-4` 可能是字符串的坑）。
- `finalize_cfg` 钩子在所有覆盖之后运行，负责派生 `logging.checkpoint_dir` / `tensorboard_dir`；保存 checkpoint 时 `save_train_config` 把「原配置文件 + 每条 opt 渲染成的一行赋值」回写为可运行的 `train_config.py`，实现运行的可复现性。

## 7. 下一步学习建议

配置系统是第 1 单元的收官。接下来进入第 2 单元的数据流水线，建议按此顺序：

- **u2-l1（数据下载与切分）**：看配置之外的第一个数据脚本，理解三阶段中「数据准备」的输入输出。
- **u2-l2（对话模板与 loss_mask）**：本讲提到的 `data.chat_template="qwen"` 字段将在 `parser.py` 里被真正消费，你会看到配置字段如何落到代码。
- 顺带留意：`data.target_cache_path` 这个本讲反复用作示例的字段，将在 u2-l4/u2-l5 揭晓它指向的二进制缓存协议——那也是 `--opts` 最重要的一次实战注入。
