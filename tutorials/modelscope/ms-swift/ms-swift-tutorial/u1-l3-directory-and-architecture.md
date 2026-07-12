# 目录结构与模块化架构

## 1. 本讲目标

通过本讲，你将：

- 理解 ms-swift 的 `swift/` 包为什么采用「一级目录 = 一个功能模块」的设计。
- 看懂 `swift/__init__.py` 顶层的 `_LazyModule` 懒加载机制：为什么 `import swift` 不会一次性把几十个子模块全部加载进来。
- 拿到一份「一级模块职责速查表」，知道每个目录（arguments / pipelines / template / model / dataset / trainers 等）负责什么。
- 理解 Architecture.md 中描述的「注册表 + 基类继承」式扩展思路，为后续自定义模型、模板、Loss 打下地基。

学完本讲，你应该能在不打开源码的情况下，凭借目录名猜出某个功能（比如自定义损失函数）大致应该去哪个目录改。

## 2. 前置知识

阅读本讲前，建议你已经：

- 完成上一讲（u1-l2 安装与环境依赖），知道 `swift` 命令是怎么被注册出来的，以及 `framework.txt` 是核心依赖集。
- 了解一点 Python 包的概念：`__init__.py` 是一个包的入口文件，`import swift.xxx` 会触发对应模块的加载。
- 听说过「懒加载（lazy loading）」这个词——意思是「用到才加载，不用就不加载」。

两个本讲会用到的术语：

- **懒加载（Lazy Import）**：把真正耗时的 `import` 动作推迟到「第一次访问某个名字」时才执行。好处是启动快、依赖轻。
- **注册表（Mapping）**：ms-swift 里大量出现的 `_map` / `_MAPPING` 字典，把一个字符串名字映射到一个类，实现「按名字取实现」。

如果你对「为什么 `import swift` 能既暴露 `swift.SftArguments`、又不会在导入时把 vllm/torch 全部拉起来」感到好奇，本讲会给你答案。

## 3. 本讲源码地图

本讲只聚焦「包结构」这一层，涉及的关键文件很少：

| 文件 | 作用 |
| --- | --- |
| `swift/__init__.py` | `swift` 包的入口，通过 `_LazyModule` 注册所有对外暴露的名字，但延迟真正加载。 |
| `swift/utils/import_utils.py` | 提供 `_LazyModule` 这个类的实现，是懒加载的核心。 |
| `docs/source_en/Customization/Architecture.md` | 官方架构说明，逐模块解释职责与「如何自定义扩展」。 |

本讲**不会**深入任何子模块的内部实现（那是 u2 ~ u10 的事），只看「目录长什么样、怎么组织、为什么这么组织」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. swift 包顶层 `_LazyModule` 懒加载机制
2. 一级模块职责速查
3. 模块化与可扩展设计

---

### 4.1 swift 包顶层 `_LazyModule` 懒加载机制

#### 4.1.1 概念说明

一个朴素的问题：为什么不在 `swift/__init__.py` 里直接写一行行 `from .trainers import Seq2SeqTrainer`、`from .infer_engine import VllmEngine`……

因为 ms-swift 整合了 transformers / peft / trl / vllm / sglang / lmdeploy / megatron 等一大堆重型依赖。如果一上来就全量 import，那么用户只是想 `import swift` 看一下版本号，也会被迫加载 vllm、megatron 这些未必安装、且非常耗时的库。这会带来：

- 启动慢（动辄几秒到十几秒）。
- 安装门槛高（少装一个可选依赖就直接 import 失败）。
- 内存浪费（用不到的模块也常驻内存）。

解决方案就是**懒加载**：`swift/__init__.py` 先只声明「我对外暴露哪些名字、每个名字藏在哪个子模块里」，但不真正 import；等到用户**第一次访问**某个名字（比如 `swift.SftArguments`）时，再去把对应的子模块加载进来。

这正是 transformers 库自己使用、并被 ms-swift 借鉴的 `_LazyModule` 模式。

#### 4.1.2 核心流程

懒加载的执行过程可以用下面这段伪代码描述：

```text
# 1. 解释器执行 import swift
swift/__init__.py 被执行:
    - 不真正 import 子模块
    - 构造 _import_structure 字典: {子模块名: [对外暴露的名字列表]}
    - 用 _LazyModule 实例替换 sys.modules['swift']

# 2. 用户访问 swift.SftArguments
    - 触发 _LazyModule.__getattr__('SftArguments')
    - 在 _class_to_module 字典中查到 'SftArguments' 属于 'arguments' 子模块
    - 真正执行 importlib.import_module('swift.arguments')
    - 从该子模块取出 SftArguments
    - setattr 缓存到实例上 (下次访问直接命中, 不再重复 import)
    - 返回 SftArguments
```

关键点有三：

- **声明与加载分离**：`__init__.py` 里只写「声明」，真正的 `import` 被推迟。
- **按名查模块**：靠一张「名字 → 子模块」的反查表（`_class_to_module`）来定位。
- **首次访问后缓存**：用 `setattr` 把结果挂回实例，避免重复 import。

#### 4.1.3 源码精读

先看 `swift/__init__.py` 的整体骨架。文件第 4 行先引入懒加载的「引擎」：

[swift/\_\_init\_\_.py:L4-L4](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/__init__.py#L4) —— 从 `utils.import_utils` 引入 `_LazyModule`，这是后续一切懒加载的基础。

接着是一个 `if TYPE_CHECKING:` 块。`TYPE_CHECKING` 在程序真正运行时为 `False`，只在静态类型检查器（如 mypy、IDE）分析代码时为 `True`。所以这个块里的 `from .xxx import ...` 在运行时**根本不会执行**，它的唯一作用是让 IDE 能自动补全：

[swift/\_\_init\_\_.py:L6-L27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/__init__.py#L6-L27) —— 仅用于 IDE 补全的「假 import」，列出 `swift` 对外暴露的全部名字（如 `BaseArguments`、`get_template`、`Swift` 等）。

真正在运行时生效的是 `else:` 分支里的 `_import_structure` 字典。它的结构是 `{子模块名: [该模块对外暴露的名字]}`。注意它只声明了「名字属于哪个子模块」，并没有真正 import 任何子模块：

[swift/\_\_init\_\_.py:L29-L56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/__init__.py#L29-L56) —— `_import_structure` 声明表。例如 `'arguments'` 子模块对外暴露 `SftArguments`、`RLHFArguments` 等；`'template'` 只暴露 `get_template`。

最后，把这张表交给 `_LazyModule`，并用它替换 `sys.modules[__name__]`。这一步是懒加载的「替换开关」——从此 `import swift` 拿到的就不再是普通的模块对象，而是一个会「按需加载」的 `_LazyModule` 实例：

[swift/\_\_init\_\_.py:L60-L66](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/__init__.py#L60-L66) —— 把 `swift` 这个名字在 `sys.modules` 中替换成 `_LazyModule` 实例，完成懒加载接管。

再看 `_LazyModule` 的实现（位于 `swift/utils/import_utils.py`）。

类的定义与文档注释，说明它的目的就是「只在对象被请求时才执行对应的 import」：

[swift/utils/import\_utils.py:L64-L70](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/import_utils.py#L64-L70) —— `_LazyModule` 继承自 `ModuleType`，注释提到灵感来自 optuna。

构造函数把 `_import_structure` 拆成两个内部结构：

- `self._modules`：所有子模块名的集合。
- `self._class_to_module`：一张「对外名字 → 子模块名」的反查表，供后续按名定位。

[swift/utils/import\_utils.py:L71-L85](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/import_utils.py#L71-L85) —— 构造函数建立 `_modules` 集合与 `_class_to_module` 反查表，并设置 `__all__` 供 IDE 补全。

懒加载的核心在 `__getattr__`。当你访问 `swift.SftArguments` 时，Python 找不到这个属性，就会回调到这个方法。它的判断分三支：

1. 名字在 `_objects`（额外对象）里 → 直接返回。
2. 名字是一个子模块名（在 `_modules` 里）→ 加载该子模块并返回。
3. 名字是一个对外暴露的类/函数（在 `_class_to_module` 里）→ 先定位到所属子模块，加载它，再从中取出该名字。
4. 都不匹配 → 抛 `AttributeError`。

[swift/utils/import\_utils.py:L97-L109](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/import_utils.py#L97-L109) —— `__getattr__` 的三路分支：模块、类/函数、未知。注意第 108 行的 `setattr(self, name, value)` 把结果缓存到实例上，使后续访问命中普通属性查找、不再重复 import。

真正执行 import 的小工具只有一行，用 `importlib.import_module` 加载相对路径的子模块：

[swift/utils/import\_utils.py:L111-L112](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/import_utils.py#L111-L112) —— `_get_module` 用 `importlib.import_module('.' + module_name, self._name)` 加载真正的子模块。

#### 4.1.4 代码实践

> 这是一个**源码阅读 + 动手验证**型实践，不需要 GPU。

实践目标：亲眼看到「懒加载」的「按需触发」效果。

操作步骤：

1. 进入安装好 ms-swift 的环境（按 u1-l2 用 `pip install -e .` 安装即可）。
2. 启动一个 Python 解释器（`python`）。
3. 执行下面这段验证脚本（这是**示例代码**，可直接复制运行）：

```python
import sys
import swift  # 注意：此时还没有真正加载大部分子模块

# 观察：哪些 swift 子模块已经被加载进 sys.modules？
swift_submods = [k for k in sys.modules if k.startswith('swift.')]
print('import swift 后已加载的 swift.* 子模块数量:', len(swift_submods))
print(swift_submods[:10])
```

4. 接着访问一个对外名字，触发懒加载：

```python
_ = swift.SftArguments  # 第一次访问，触发 swift.arguments 的真正加载

swift_submods_after = [k for k in sys.modules if k.startswith('swift.')]
print('访问 SftArguments 后已加载的 swift.* 子模块数量:', len(swift_submods_after))
print('是否新出现了 swift.arguments:', 'swift.arguments' in sys.modules)
```

需要观察的现象：

- 第一步打印的子模块数量应该**很少**（基本只有 `swift.utils` 之类被 `_LazyModule` 本身依赖的模块）。
- 第二步访问 `swift.SftArguments` 后，`swift.arguments` 才出现在 `sys.modules` 中，数量明显增加。

预期结果：

- `import swift` 几乎是「零成本」的，不会强行加载 vllm/megatron 等重型可选依赖。
- 只有当真正用到某个名字时，才会按需加载对应子模块。

> 如果你的环境里 `import swift` 本身就报 `ModuleNotFoundError`，多半是核心依赖没装齐，请回到 u1-l2 检查 `framework.txt` 是否完整安装。运行细节「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `swift/__init__.py` 第 60 行的 `sys.modules[__name__] = _LazyModule(...)` 注释掉，会发生什么？

参考答案：`import swift` 后，`swift` 仍是普通模块对象，里面并没有 `SftArguments`、`get_template` 这些属性（因为 `else` 分支只是定义了字典，没有真正 import）。访问 `swift.SftArguments` 会直接报 `AttributeError`。这一行是懒加载生效的「替换开关」。

**练习 2**：`if TYPE_CHECKING:` 块里的 `from .trainers import Seq2SeqTrainer` 在程序运行时会被执行吗？它的作用是什么？

参考答案：运行时不会执行（`TYPE_CHECKING` 在运行时为 `False`）。它的作用纯粹是给 IDE 和类型检查器看，让它们知道 `swift` 对外有这些名字，从而提供自动补全与类型提示。

---

### 4.2 一级模块职责速查

#### 4.2.1 概念说明

ms-swift 4.x 的设计原则是「**功能模块分布在一级目录**」。也就是说，`swift/` 下面每一个目录几乎都对应一个独立的功能领域，目录名即职责名。这种「扁平 + 按领域切分」的组织方式有两个好处：

- **好找**：想改某个功能，直接进对应目录；目录名就是索引。
- **好扩展**：要加一个新功能（比如一种新的奖励函数），不需要动其他目录，只在对应目录里加文件、加注册即可。

理解了这一点，后续看任何一篇源码讲义时，你都能先在脑海里定位它属于哪个一级模块。

#### 4.2.2 核心流程

下面这张「职责速查表」综合了 `swift/__init__.py` 的 `_import_structure` 与 Architecture.md 的官方说明。建议把它当作后续学习的「目录索引」。

| 一级模块 | 职责（一句话） | 对外暴露的代表名字 | 后续讲义 |
| --- | --- | --- | --- |
| `arguments` | 命令行参数定义，按任务派生 | `SftArguments` / `RLHFArguments` | u2 |
| `model` | 模型加载与注册（含 ModelMeta） | `get_model_processor` / `get_processor` | u3 |
| `template` | 对话模板：messages → input_ids | `get_template` | u3 |
| `dataset` | 数据集加载、预处理、packing | `load_dataset` / `EncodePreprocessor` | u4 |
| `trainers` | pretrain/SFT/分类等任务的 Trainer | `Seq2SeqTrainer` / `Trainer` | u5 |
| `tuners` | 轻量微调方法（LoRA 等）的底层实现 | `Swift` | u5 |
| `tuner_plugin` | 把 tuner 挂到模型上的「插件」抽象 | `Tuner` / `PeftTuner` | u5 |
| `pipelines` | 各子命令的主流程入口 | `sft_main` / `infer_main` / `export_main` | u5/u8 |
| `infer_engine` | 推理引擎（多后端） | `VllmEngine` / `TransformersEngine` | u6 |
| `loss` / `loss_scale` | 自定义损失 / token 权重控制 | `BaseLoss` / `LossScale` | u10 |
| `metrics` | 评测指标 | `eval_metrics_map` | u10 |
| `optimizers` | 自定义优化器 | `optimizers_map` | u10 |
| `callbacks` | 训练过程回调 | `callbacks_map` | u10 |
| `agent_template` | Agent 工具调用模板 | `BaseAgentTemplate` | u10 |
| `cli` | `swift` 命令行入口与路由 | （入口脚本，非库 API） | u1-l4 |
| `megatron` | Megatron-SWIFT 高性能训练 | （独立子系统） | u9 |
| `rl_core` / `rlhf_trainers` / `rollout` / `rewards` | 强化学习相关 | （GRPO/DPO 等） | u7 |
| `sequence_parallel` | 长文本序列并行 | （Ulysses/Ring） | u9 |
| `ray` / `ray_utils` | Ray 分布式调度 | （跨机训练） | u9 |
| `ui` | `swift web-ui` 图形界面 | （Gradio） | u10 |

> 注意：表中「对外暴露的代表名字」一列可在 `swift/__init__.py` 的 `_import_structure` 里逐一对应查到。`cli` / `megatron` / `rl_core` 等模块多数不在 `swift` 顶层对外暴露，而是通过子命令或子系统独立调用。

#### 4.2.3 源码精读

Architecture.md 在文末有一节「Introduction to Other Directory Structures」，逐条解释了这些目录的职责，这是官方钦定的「目录说明书」：

[docs/source\_en/Customization/Architecture.md:L217-L233](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Architecture.md#L217-L233) —— 官方对 arguments / cli / config / dataloader / dataset / infer_engine / megatron / model / pipelines / rlhf_trainers / rollout / rewards / template / trainers / ui 等目录的逐条说明。

其中两条特别值得记住（因为后续讲义会反复用到）：

- `cli` 的等价关系：`swift sft ...` 等价于 `python swift/cli/main.py sft ...`，也等价于 `python swift/cli/sft.py ...`。这正是下一讲（u1-l4）要拆解的 CLI 分发机制。
- `pipelines` 是「主函数管道」，`sft_main / rlhf_main / infer_main / export_main` 等都住在这里。也就是说，你在命令行敲的每一条 `swift xxx`，最终都会落到 `pipelines` 里对应的 `xxx_main` 函数上。

如果你想在源码里印证上表，最直接的方式是看 `_import_structure` 里每个子模块对外暴露了哪些名字——它就是「这个目录对外的公开 API」：

[swift/\_\_init\_\_.py:L29-L56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/__init__.py#L29-L56) —— 每个一级模块对外暴露的代表名字都登记在此。

#### 4.2.4 代码实践

实践目标：用源码自动生成「名字 → 子模块」对照表，验证上表。

操作步骤（示例代码）：

```python
import swift

# _LazyModule 内部的反查表：对外名字 -> 所属子模块
table = swift._class_to_module
for name in ['SftArguments', 'get_template', 'VllmEngine', 'load_dataset', 'Seq2SeqTrainer']:
    print(f'{name:20s} -> swift.{table[name]}')
```

需要观察的现象：每个对外名字都被准确映射到一个一级子模块，与上表一致。

预期结果：例如 `SftArguments -> swift.arguments`、`get_template -> swift.template`、`VllmEngine -> swift.infer_engine`、`load_dataset -> swift.dataset`、`Seq2SeqTrainer -> swift.trainers`。

> 说明：`_class_to_module` 是 `_LazyModule` 的内部属性（带下划线，属私有 API），此处仅用于学习观察，不要在生产代码中依赖它。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：我想自定义一个评测指标，应该去哪个一级目录？

参考答案：`swift/metrics/`。根据 Architecture.md，ms-swift 用 `metrics` 模块统一管理评测指标，自定义时继承 `base.py` 中的 `EvalMetrics` 基类，并在 `metrics/mapping.py` 注册，训练时用 `--eval_metric` 指定。这会在 u10-l2 详讲。

**练习 2**：`swift sft` 命令的真正业务逻辑（训练主流程）写在哪个目录？

参考答案：写在 `swift/pipelines/train/sft.py`（对应 `SwiftSft` 与 `sft_main`）。`cli` 目录只负责命令路由与启动，真正的训练编排逻辑在 `pipelines` 里。这会在 u5-l4 详讲。

---

### 4.3 模块化与可扩展设计

#### 4.3.1 概念说明

ms-swift 的「模块化」不只是把代码分目录放，更重要的是它给每个可扩展点都设计了一套统一的**扩展范式**：

> **基类（base） + 注册表（mapping） + CLI 参数开关**

也就是说，几乎每一个「你想自定义的东西」——模型、模板、Loss、优化器、Callback、Tuner、奖励函数、评测指标——都遵循同样的套路：

1. 在对应模块的 `base.py` 里定义一个基类，规定你必须实现哪些方法（契约）。
2. 写一个子类继承它，实现这些方法。
3. 在对应模块的 `mapping.py` 里把你的子类注册进一个字典，给一个名字。
4. 训练时用一个 CLI 参数（如 `--loss_type`、`--callbacks`、`--optimizer`）按名字指定，框架就会从注册表里取出你的实现。

这种统一范式的好处是：**学会自定义一个扩展点，就等于学会了所有扩展点**。

#### 4.3.2 核心流程

下面这张表把 Architecture.md 里讲到的可扩展点整理成「基类 / 注册表 / CLI 开关」三件套，方便对照：

| 扩展点 | 基类（base.py） | 注册表（mapping.py） | CLI 开关 |
| --- | --- | --- | --- |
| 对话模板 | `Template` | `TEMPLATE_MAPPING` | `--template` |
| Agent 模板 | `BaseAgentTemplate` | `agent_template_map` | `--agent_template` |
| 损失函数 | `BaseLoss` | `loss_map` | `--loss_type` |
| token 权重 | `LossScale` | `loss_scale_map` | `--loss_scale` |
| 评测指标 | `EvalMetrics` | `eval_metrics_map` | `--eval_metric` |
| 优化器 | `OptimizerCallback` | `optimizers_map` | `--optimizer` |
| 训练回调 | `TrainerCallback` | `callbacks_map` | `--callbacks` |
| Tuner 插件 | `Tuner` | `tuners_map` | `--tuner_type` |

> 注意：上表中 `TEMPLATE_MAPPING` / `tuners_map` 等注册表的确切命名以对应模块源码为准（部分在 u3/u5/u10 讲义中会逐一精读）；Architecture.md 对 `callbacks` / `loss` / `loss_scale` / `metrics` / `optimizers` / `tuner_plugin` / `agent_template` 这几个扩展点有明确的「基类 + mapping 文件路径」说明，可作为权威出处。

#### 4.3.3 源码精读

Architecture.md 开篇一句话点明了整套设计的指导思想——功能模块分布在一级目录、便于自定义扩展：

[docs/source\_en/Customization/Architecture.md:L3-L3](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Architecture.md#L3-L3) —— 「ms-swift 4.0 adopts a modular design, with functional modules distributed in first-level directories, making it convenient for developers to perform custom extensions.」

以 Loss 扩展点为例，文档明确说明了「基类 + mapping + CLI 开关」三件套的用法。文档第 55 行指明 mapping 文件位于 [swift/loss/mapping.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/mapping.py)；随后说明自定义 Loss 需继承 `BaseLoss`、实现 `__call__` 返回一个标量 Tensor，注册后用 `--loss_type` 指定：

[docs/source\_en/Customization/Architecture.md:L55-L65](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Architecture.md#L55-L65) —— Loss 扩展点的官方说明：mapping 文件路径 + `BaseLoss` 基类契约 + `--loss_type` 开关。

再以 Tuner Plugin 为例，文档说明了三个必须实现的静态/实例方法（`prepare_model` / `save_pretrained` / `from_pretrained`），它们分别对应训练前挂载、训练中保存、推理时加载三个时机：

[docs/source\_en/Customization/Architecture.md:L142-L148](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Architecture.md#L142-L148) —— Tuner 扩展点：`Tuner` 基类与 `prepare_model / save_pretrained / from_pretrained` 三个契约方法。

可以看出，每个扩展点的「形状」都高度一致：一个基类定契约、一个 mapping 做注册、一个 CLI 参数做开关。理解了这个范式，后面看到任何一个 `*_map` / `*_MAPPING`，你都能猜到它是某个扩展点的注册表。

#### 4.3.4 代码实践

实践目标：在源码中找到所有「基类 + 注册表」对，体会统一范式。

操作步骤（**源码阅读型实践**，无需运行）：

1. 打开 Architecture.md，定位以下小节并阅读：Callbacks、Loss、Loss Scale、Metrics、Optimizers、Tuner Plugin。
2. 对每个小节，记下三件事：
   - 基类名（在哪个 `base.py`）。
   - mapping 文件路径（文档里通常会给出 GitHub 链接）。
   - 对应的 CLI 参数名。
3. 用 `Glob` 在 `swift/` 下查找所有名为 `mapping.py` 的文件：

```bash
# 在项目根目录执行
ls swift/*/mapping.py 2>/dev/null
```

需要观察的现象：你会看到 `callbacks/mapping.py`、`loss/mapping.py`、`loss_scale/mapping.py`、`metrics/mapping.py`、`optimizers/mapping.py`、`tuner_plugin/mapping.py`、`agent_template/mapping.py` 等一批同名的 mapping 文件，它们就是各扩展点的注册表。

预期结果：你能整理出一张与「4.3.2 核心流程」中表格一致的对照表，从而确信「基类 + mapping + CLI 开关」是贯穿全项目的一致范式。

> 若 `ls` 输出与预期不符（例如某个 mapping 文件不存在），以你本地实际 `git` HEAD 为准，并把差异记下来。该结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：Architecture.md 说自定义 Callback 需要继承哪个基类？注册后用哪个 CLI 参数指定？

参考答案：继承 `TrainerCallback`（位于 `swift/callbacks/base.py`），接口与 transformers 的 `TrainerCallback` 一致；在 `swift/callbacks/mapping.py` 注册后，训练时用 `--callbacks` 指定。

**练习 2**：为什么 ms-swift 要把每个扩展点都设计成「基类 + 注册表 + CLI 开关」这种统一范式，而不是各写各的？

参考答案：统一范式带来三个好处——一是降低学习成本（学会一个就会全部）；二是 CLI 层可以用统一的方式按名字取实现，无需为每种扩展点写特判；三是新功能只需「加子类 + 注册一行」，不改既有代码，符合开闭原则。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿任务：

> **任务：依据 Architecture.md 与 `swift/__init__.py`，绘制一张 ms-swift「模块依赖与扩展点草图」。**

要求在草图上标注：

1. **入口层**：`swift/__init__.py` 通过 `_LazyModule` 暴露哪些对外名字（从 `_import_structure` 抄），以及 `cli` 与 `pipelines` 的「命令 → `*_main` 函数」对应关系（如 `swift sft` → `sft_main`）。
2. **核心数据流层**：按训练时的先后顺序，画出 `arguments`（参数）→ `model`（模型）→ `template`（模板）→ `dataset`（数据）→ `trainers`（训练器）这条主线，并标注每个环节对外暴露的代表名字。
3. **扩展点层**：在草图边缘列出 6 个以上「基类 + mapping + CLI 开关」三件套（Loss / LossScale / Metrics / Optimizers / Callbacks / Tuner Plugin / Agent Template / Template 任选）。

操作建议：

- 用任意画图工具（纸笔、draw.io、Mermaid 均可）。
- 主线用实线，扩展点用虚线框单独圈出，体现「核心流程 + 可插拔扩展」的分层。
- 对照本讲「4.2.2 核心流程」的速查表与「4.3.2 核心流程」的三件套表来画。

完成后，你应该能指着草图回答：自定义损失函数去哪个目录？训练主流程在哪个目录？为什么 `import swift` 不会很慢？——如果三个问题都能答上来，本讲就过关了。

## 6. 本讲小结

- ms-swift 采用「一级目录 = 一个功能模块」的扁平组织，目录名即职责名，便于定位与扩展。
- `swift/__init__.py` 用 `_LazyModule` 实现懒加载：只声明「名字属于哪个子模块」，真正 import 推迟到首次访问，从而 `import swift` 又快又轻。
- `_LazyModule` 的核心是 `__getattr__`：按名查 `_class_to_module` 反查表 → 加载对应子模块 → `setattr` 缓存。
- Architecture.md 是官方钦定的「目录说明书」，文末的目录结构清单是后续学习的索引。
- 全项目的扩展点遵循统一范式：**基类（base） + 注册表（mapping） + CLI 参数开关**。
- 记住三件套范式后，看到任何一个 `*_map` / `*_MAPPING`，都能猜到它对应一个可自定义的扩展点。

## 7. 下一步学习建议

下一讲 **u1-l4 CLI 入口与命令分发** 会从「目录」下沉到「命令」：拆解 `swift/cli/main.py` 的 `ROUTE_MAPPING` 子命令路由、`use_torchrun` 多卡启动判定，以及 YAML/JSON 配置如何被解析成命令行参数。

建议你在进入下一讲前：

- 先做一遍本讲的「综合实践」草图，把目录与扩展点的全局图景印在脑子里。
- 可选阅读 `docs/source_en/Customization/Architecture.md` 全文（不长），重点看 Callbacks / Loss / Tuner Plugin 三个小节，对「基类 + mapping」范式建立直觉。
- 后续 u2（参数体系）、u3（模型与模板）、u5（训练器）会逐个深入本讲速查表里的核心模块，到时候可以回头对照本讲的速查表，巩固「它在哪、负责什么」。
