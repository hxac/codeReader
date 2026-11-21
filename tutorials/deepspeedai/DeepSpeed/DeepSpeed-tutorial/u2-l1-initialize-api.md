# u2-l1 initialize() 与三步接入法

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出 DeepSpeed 的标准接入代码：`deepspeed.initialize()` 包装模型 → 前向得到 loss → `backward` + `step`（或直接用 `engine.train_batch()` 一站式驱动）。
2. 解释 `initialize()` 内部如何依据「模型类型 + 配置内容」选择引擎类（`DeepSpeedEngine` / `DeepSpeedHybridEngine` / `PipelineEngine` / `DualPipeVEngine`）。
3. 说清配置传入的两条通道（`config` 参数 vs `args.deepspeed_config`）以及字符串路径如何被 `load_ds_config` 解析成字典。
4. 掌握本周期新增的 AutoSP 前置校验（PR #8457）：`compile.passes` 含 `autosp` 且 `sequence_parallel_size > 1` 时，要求 `compile.deepcompile = true` 且 ZeRO stage < 2，否则抛 `ValueError`；无 `mpu` 时还会自动按 `(data_parallel, sequence_parallel)` 创建 mesh device。
5. 复用 `tests/unit/simple_model.py` 里的 `SimpleModel` 搭建自己的本地实验台——这是 DeepSpeed 全仓测试共用的最小模型。

## 2. 前置知识

本讲默认你已读过 u1-l1（项目全景）和 u1-l3（安装与构建），知道 `deepspeed/` 是运行期主包、`import deepspeed` 不会初始化 CUDA。在此之上，补充几个 PyTorch 基础概念：

- **`torch.nn.Module`**：PyTorch 所有模型的基类。DeepSpeed 不修改你的模型定义，而是在 `initialize()` 时把模型「包装」进一个引擎对象，引擎持有模型并接管训练流程。
- **优化器（Optimizer）**：根据梯度更新参数的对象（如 `torch.optim.Adam`）。可以自己创建后传给 DeepSpeed，也可以只在 JSON 配置里声明，让 DeepSpeed 替你创建。
- **数据集与数据加载器**：`torch.utils.data.Dataset` 提供样本，`DataLoader` 负责批量取出。传给 DeepSpeed 的 `training_data` 是 Dataset，引擎会自建 DataLoader。
- **world_size 与 rank**：分布式训练中进程总数叫 world_size，每个进程有一个从 0 开始的编号 rank。单进程运行时 world_size = 1。
- **梯度累积（gradient accumulation）**：多个 micro-batch 的梯度累加后才更新一次参数，用来在有限显存下模拟大 batch。三者的关系是：

\[ \text{train\_batch\_size} = \text{train\_micro\_batch\_size\_per\_gpu} \times \text{gradient\_accumulation\_steps} \times \text{data\_parallel\_size} \]

- **引擎（Engine）**：DeepSpeed 的核心运行时对象。它包裹你的模型，额外提供 `train_batch()`、`backward()`、`step()`、`save_checkpoint()` 等方法，并在内部完成通信、混合精度、ZeRO 切分等全部重活。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [deepspeed/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py) | 公共 API 面。本讲主角 `initialize()` 在此定义，本周期新增 `_autosp_sequence_parallel_size()` 辅助函数与 AutoSP 前置校验 |
| [tests/unit/simple_model.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/simple_model.py) | 全仓共用的测试基建：`SimpleModel` 等最小模型、`random_dataset`/`random_dataloader`、`args_from_dict` 配置工厂 |
| [deepspeed/runtime/tensor_parallel/init_utils.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/runtime/tensor_parallel/init_utils.py) | `load_ds_config()`：把字符串配置（文件路径或 base64）解析成 dict |
| [deepspeed/comm/comm.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/comm/comm.py) | `initialize_mesh_device()`：创建带命名维度的 device mesh（AutoSP 自动建 mesh 时用到） |
| [tests/unit/v1/compile/test_compile_autosp.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/v1/compile/test_compile_autosp.py) | AutoSP 校验的回归测试，是本讲实践任务的「标准答案」 |

## 4. 核心概念与源码讲解

### 4.1 initialize 函数签名与引擎分发

#### 4.1.1 概念说明

`deepspeed.initialize()` 是训练侧唯一的入口函数（推理侧另有 `init_inference()`）。它做的事情一句话概括：**收下你的模型、优化器、数据和配置，返回一个「什么都管」的引擎**。

理解本模块要抓住两点：

1. **签名很长但必填项极少**。13 个参数里只有 `model` 是必填的（代码里有显式断言），配置必须通过 `config` 参数或 `args.deepspeed_config` 之一给出，其余全可选。
2. **引擎类不是你选的，是推断出来的**。模型是不是 `PipelineModule`、配置里有没有开 `hybrid_engine`，共同决定最终返回四种引擎中的哪一种。用户代码拿到手都是同一个接口。

#### 4.1.2 核心流程

`initialize()` 从被调用到返回，按顺序经历以下阶段（行号见下文源码精读）：

```text
deepspeed.initialize(args, model, optimizer, ..., config)
  │
  ├─ 1. 打印版本日志；关闭可能在生效的 zero.Init 上下文
  ├─ 2. 断言 model 非空
  ├─ 3. 初始化分布式（init_distributed，按加速器选 backend）
  ├─ 4. 解析配置：config_params 兼容 → deepscale 弃用检查
  │      → args.deepspeed_config 与 config 二选一 → 字符串转 dict
  ├─ 5. AutoSP 前置校验（本周期新增，见 4.4）
  ├─ 6. 创建 mesh device（三分支，见 4.4.3）
  ├─ 7. merge_tp_model_init_into_config；按 autotp_size 设训练模式
  ├─ 8. 引擎分发：
  │      ├─ model 是 PipelineModule → PipelineEngine / DualPipeVEngine
  │      ├─ hybrid_engine.enabled  → DeepSpeedHybridEngine
  │      └─ 其余                    → DeepSpeedEngine
  ├─ 9. 恢复 zero.Init 上下文；配置 Python GC
  └─ 10. 返回 (engine, engine.optimizer, engine.training_dataloader, engine.lr_scheduler)
```

注意第 1 步和第 9 步成对出现：如果用户在模型构造期用了 `deepspeed.zero.Init()`（参数从出生就切分，见 u5-l2），`initialize` 会先关闭它、构造完引擎后再恢复，避免引擎构造过程意外触发参数切分。

#### 4.1.3 源码精读

**函数签名**——[deepspeed/__init__.py:L440-L454](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L440-L454)

这段定义了 `initialize` 的 13 个参数。最常用的五个：`model`（必填）、`optimizer`（可选，可交给配置）、`training_data`（Dataset）、`config`（dict 或路径）、`args`（含 `deepspeed_config` 字段的参数对象）。返回值是四元组。

**入口动作：日志、zero.Init 关闭、分布式初始化**——[deepspeed/__init__.py:L505-L519](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L505-L519)

这段先打印版本与 git hash（对照日志排障很有用），然后 `shutdown_init_context()` 关闭参数切分上下文，断言模型非空，最后调用 `dist.init_distributed()`。`dist` 是模块级延迟绑定的（u1-l2 讲过：`import deepspeed` 时它是 `None`，到引擎初始化才指向 `deepspeed.comm`），这里 `global dist; from deepspeed import comm as dist` 完成绑定。backend 名称由加速器抽象层决定（CPU 上是 gloo，CUDA 上是 nccl，详见 u3-l1/u3-l2）。

**引擎分发**——[deepspeed/__init__.py:L583-L627](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L583-L627)

这是本模块的核心分支，逐段看：

- L583 `if not isinstance(model, PipelineModule)`：非流水线路径。先构造 `DeepSpeedConfig`（配置解析的入口，u2-l2 详讲），再调 `set_optimizer_flags()` 给参数打 Muon 相关标记（Muon 优化器才用到，本讲不展开），然后看 `config_class.hybrid_engine.enabled`：开了混合引擎走 `DeepSpeedHybridEngine`（训练 + 推理一体化，u7-l4），否则走默认的 `DeepSpeedEngine`。
- L611 起 `else` 分支：模型是 `PipelineModule`（流水线并行，u6-l3）。注意 L612 的断言 `mpu must be None with pipeline parallelism`——流水线模型的并行信息由 `model.mpu()` 自带，不允许外部再传 `mpu`；L616 再按是否为 `DualPipeVModule` 选 `DualPipeVEngine` 或 `PipelineEngine`。

**返回值组装**——[deepspeed/__init__.py:L629-L640](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L629-L640)

恢复 zero.Init 上下文、关一下 Python GC 之后，把引擎身上的 optimizer / training_dataloader / lr_scheduler 直接摘出来打包返回。所以 `engine.optimizer` 和返回的第二个元素是同一个对象——不解包也能用。

#### 4.1.4 代码实践

**实践目标**：写出第一个 DeepSpeed 训练脚本，体验「三步接入法」。

**操作步骤**：

1. 把仓库的 `tests/unit` 目录加入 `PYTHONPATH`（脚本要复用 `SimpleModel`）：

```bash
cd /path/to/DeepSpeed
export PYTHONPATH=$PWD/tests/unit:$PYTHONPATH
```

2. 新建 `train_min.py`（示例代码，仿照 `tests/unit/simple_model.py` 的模式）：

```python
# 示例代码：最小 DeepSpeed 训练脚本
import torch
import deepspeed
from simple_model import SimpleModel, random_dataset

hidden_dim = 32
model = SimpleModel(hidden_dim)  # 一层 Linear + CrossEntropyLoss

config = {
    "train_micro_batch_size_per_gpu": 4,   # micro batch
    "gradient_accumulation_steps": 2,      # 单进程: 4 x 2 x 1 = train_batch_size 8
    "optimizer": {"type": "Adam", "params": {"lr": 1e-3}},
}

engine, optimizer, dataloader, lr_scheduler = deepspeed.initialize(
    model=model,
    training_data=random_dataset(total_samples=64,
                                 hidden_dim=hidden_dim,
                                 device=torch.device("cpu"),
                                 dtype=torch.float32),  # 显式 fp32，避免与 fp32 权重 dtype 不匹配
    config=config,  # 直接传 dict，无需先写 JSON 文件
)

for step in range(3):
    loss = engine.train_batch()  # 一站式：取数据 + 前向 + 反向 + 优化器 step
    print(f"step {step}: loss = {loss.item()}")
```

3. 运行 `python train_min.py`。

**需要观察的现象**：

- 启动时打印一行 `DeepSpeed info: version=..., git-hash=..., git-branch=...`（来自 L505 的日志）。
- 每步输出一个 loss 数值，且整体趋势下降。

**预期结果**：3 步训练全部跑通。本讲义写作环境无法执行 Python，具体 loss 数值**待本地验证**；脚本结构与 `tests/unit/simple_model.py` 中大量测试的用法一致，若报错优先检查 `PYTHONPATH` 与 deepspeed 版本。

#### 4.1.5 小练习与答案

**练习 1**：`initialize()` 的 13 个参数里，哪一个是唯一必须显式传入的？配置可以不传吗？

答案：`model` 是唯一必填参数（L512 有 `assert model is not None`）。配置不能完全不给——必须通过 `config` 参数或 `args.deepspeed_config` 之一提供，否则 L539 的断言 `"DeepSpeed requires --deepspeed_config to specify configuration file"` 会失败。

**练习 2**：一个模型被 `deepspeed.initialize()` 包装后返回的类型，由哪几个条件决定？列出所有可能的引擎类。

答案：两个条件——模型是否为 `PipelineModule`（或其子类 `DualPipeVModule`）、配置中 `hybrid_engine.enabled` 是否为真。四种引擎：`DeepSpeedEngine`（默认）、`DeepSpeedHybridEngine`（开混合引擎）、`PipelineEngine`（流水线模块）、`DualPipeVEngine`（DualPipeV 流水线模块）。

**练习 3**：为什么 `initialize()` 开头要调 `zero.partition_parameters.shutdown_init_context()`，结尾又恢复？

答案：用户可能在模型构造期启用了 `deepspeed.zero.Init()`（让参数从创建起就被 ZeRO-3 切分）。引擎构造过程中要读取完整的模型结构（如构造 `DeepSpeedConfig`、给参数打标记），如果切分上下文一直开着，这些读取会看到被切分后的占位参数。先关掉、构造完引擎再恢复，能保证两个阶段互不干扰。

### 4.2 SimpleModel：可复用的最小实验模型

#### 4.2.1 概念说明

读 DeepSpeed 源码最省力的路径是先读它的测试基建。`tests/unit/simple_model.py` 是全仓数百个测试共享的「实验台」，里面有各种最小模型、数据工厂和配置工厂。掌握它之后，你可以用几十行代码复现任何一个测试里的场景——本讲义的所有实践都基于它。

`SimpleModel` 是其中最小的模型：一层（或多层）`Linear` 加一个 `CrossEntropyLoss`。它的 forward 直接返回**标量 loss**而不是 logits，这正是 DeepSpeed 引擎最喜欢的形态——引擎拿到返回值后做 loss 缩放和反向。

#### 4.2.2 核心流程

`SimpleModel` 家族的协作方式：

```text
args_from_dict(tmpdir, config_dict)     # 把 dict 写成 JSON 文件，挂到 args.deepspeed_config
        │
        ▼
deepspeed.initialize(args, model, ...)  # 引擎包装（配置走 args 通道）
        │
        ▼
random_dataloader(model, ...)           # 用 engine.train_micro_batch_size_per_gpu() 决定 batch 大小
        │
        ▼
for batch in loader: loss = model(batch[0], batch[1])   # 测试里常见的半手动循环
```

关键设计：`random_dataloader` 在**引擎创建之后**调用，因为 batch 大小要问引擎（`model.train_micro_batch_size_per_gpu()`，此时 `model` 已经是引擎了）。而 4.1.4 的实践用了另一种方式——把 Dataset 直接传给 `initialize` 的 `training_data`，让 `train_batch()` 内部自建 DataLoader 自动迭代（两条路径的差别在 u2-l4 详讲）。

#### 4.2.3 源码精读

**SimpleModel 定义**——[tests/unit/simple_model.py:L20-L36](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/simple_model.py#L20-L36)

构造函数建一个 `ModuleList`（`nlayers` 默认 1）和 `CrossEntropyLoss`；`empty_grad=True` 时额外建一个 `linear2`——它在 forward 里根本不被使用，专门用来测试「参数收不到梯度」的场景（u5-l3 会用到）。forward 里多层时做 `linears[i // 2](x) + l(x)` 的残差式叠加，单层时就是普通线性变换，最后返回交叉熵损失。

**random_dataset / random_dataloader**——[tests/unit/simple_model.py:L245-L256](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/simple_model.py#L245-L256)

`random_dataset` 生成 `total_samples` 个随机向量和 `[0, hidden_dim)` 的随机整数标签，打包成 `TensorDataset`。注意 `random_dataloader` 的 `dtype` 参数默认取 `preferred_dtype()`（CPU 上支持 bf16 时返回 `torch.bfloat16`，见 [tests/unit/common.py:L528-L534](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/common.py#L528-L534)），而模型权重默认 fp32——所以 4.1.4 的实践脚本显式传了 `dtype=torch.float32` 避免 dtype 不匹配。

**配置工厂三件套**——[tests/unit/simple_model.py:L268-L290](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/simple_model.py#L268-L290)

`create_config_from_dict` 把 dict 序列化成 JSON 文件；`create_deepspeed_args` 造一个只含 `deepspeed=True`（和可能的 `local_rank`）的参数对象；`args_from_dict` 把两者拼起来——产出 `args.deepspeed_config` 指向临时 JSON 文件的标准参数对象。这就是「args 通道」的标准构造法，与直接传 `config` dict 的「config 通道」形成对照（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：改造 `SimpleModel`，验证你对引擎包装的理解。

**操作步骤**：

1. 在 4.1.4 的脚本里把 `SimpleModel(hidden_dim)` 换成 `SimpleModel(hidden_dim, nlayers=3)`。
2. 再换成 `SimpleModel(hidden_dim, empty_grad=True)`。
3. 每次改动后打印 `engine.module` 的类型和参数列表：`print(type(engine.module)); print([n for n, _ in engine.module.named_parameters()])`。

**需要观察的现象**：

- `engine.module` 就是你的原始模型对象（引擎只是包装它，不是复制）。
- `nlayers=3` 时 forward 走残差叠加分支；`empty_grad=True` 时参数列表多出一个 `linear2.weight/bias`。

**预期结果**：`nlayers=3` 正常训练；`empty_grad=True` 时训练仍能跑（取决于 ZeRO stage，可能有未使用参数的警告）。具体行为**待本地验证**——`linear2` 不参与前向，其梯度处理方式正是 u5-l3（ZeRO-1/2）和 u5-l6（ZeRO-3 未用参数梯度修复 #8981）的主题，可以先记下疑问。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SimpleModel.forward` 返回 loss 而不是 logits？这对 DeepSpeed 有什么好处？

答案：DeepSpeed 引擎在训练时需要拿到标量 loss 来做梯度累积下的 loss 缩放（fp16 动态缩放）和 `backward(loss)`。模型直接返回 loss，`engine.train_batch()` 或 `engine(x, y)` 拿到返回值即可直接进入反向，用户代码无需额外写 `loss.backward()`。

**练习 2**：`random_dataloader` 为什么要求在 `deepspeed.initialize()` 之后调用？

答案：它用 `model.train_micro_batch_size_per_gpu()` 决定 DataLoader 的 batch_size，而这个方法只在模型被包装成引擎之后才存在（配置中的 `train_micro_batch_size_per_gpu` 经引擎挂到模型上）。包装前调用会报 `AttributeError`。

**练习 3**：`args_from_dict(tmpdir, config_dict)` 最终产出的 `args` 对象上，`deepspeed_config` 属性的值是什么类型？

答案：字符串——临时 JSON 文件的路径（`create_config_from_dict` 的返回值）。它不是 dict；dict → JSON 文件路径的转换就是为了让配置走 `args` 通道。

### 4.3 参数 args 与 config 双通道

#### 4.3.1 概念说明

`initialize()` 的配置来源有两扇门：

- **config 通道**：`config` 参数直接传 dict 或字符串（路径/base64）。最直接，适合自己写脚本。
- **args 通道**：`args` 参数携带 `args.deepspeed_config`（文件路径）。适合框架集成场景——HuggingFace Trainer、`deepspeed` 启动器都会把配置路径塞进 args 里传下来。

两条通道互斥：都给会直接断言失败；都不给也断言失败。另有 `config_params`（`config` 的旧名，向后兼容别名）和 `args.deepscale_config`（DeepScale 时代的遗物，会打弃用警告）两个历史包袱。

#### 4.3.2 核心流程

配置解析的完整决策链（`initialize` 内 L523-L542）：

```text
config 为空且 config_params 非空？ ──是──> config = config_params     # 兼容旧名
args 带 deepscale_config？        ──是──> 警告并转移到 deepspeed_config
args.deepspeed_config 非空？      ──是──> 断言 config 为空，config = args.deepspeed_config
config 仍为 None？                ──是──> 断言失败（必须有配置）
config 是字符串？                 ──是──> load_ds_config(str) 解析
                                  ──否──> 本来就是 dict，直接用
```

`load_ds_config` 对字符串的两步尝试：先当**文件路径**用 `hjson.load` 打开（hjson 兼容 JSON 但允许注释和尾逗号，且用 `dict_raise_error_on_duplicate_keys` 拒绝重复键）；打不开再尝试**base64 解码**后按 hjson 解析——这个 base64 通道不是装饰品，`deepspeed` 启动器在把配置传递给各节点进程时会用到编码后的配置串（u1-l4 讲过启动器用 base64 编码资源信息，同一手法）。

#### 4.3.3 源码精读

**双通道裁决**——[deepspeed/__init__.py:L521-L542](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L521-L542)

按顺序处理四个来源：`config_params` 别名回填（L523-524）→ `deepscale_config` 弃用警告与转移（L527-533）→ `args.deepspeed_config` 与 `config` 参数二选一断言（L536-538）→ 最后兜底断言配置必须存在（L539）。L541-542 把非 dict 的 config 交给 `load_ds_config`。

**load_ds_config 的三形态解析**——[deepspeed/runtime/tensor_parallel/init_utils.py:L19-L33](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/runtime/tensor_parallel/init_utils.py#L19-L33)

dict 原样返回；字符串先按文件路径走 `hjson.load`（带重复键检查钩子），失败后尝试 base64 解码再按 hjson 解析；两种都失败抛出说明清楚的 `ValueError`。

**args 通道的参数是怎么来的**——[deepspeed/__init__.py:L678-L691](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L678-L691)

`add_config_arguments(parser)` 给用户的 `argparse` 解析器注册 `--deepspeed`（布尔开关）和 `--deepspeed_config`（JSON 路径）。你的训练脚本开头调用它，命令行的 `--deepspeed_config ds_config.json` 就会落到 `args.deepspeed_config` 上，这正是 args 通道的典型来源。

#### 4.3.4 代码实践

**实践目标**：亲手触发双通道的冲突断言，并验证三种合法传法等价。

**操作步骤**：

1. 复制 4.1.4 的脚本，把 `initialize(...)` 调用改成同时给两处配置：

```python
# 示例代码：故意触发双通道冲突
args = argparse.Namespace(deepspeed_config="path/to/config.json")  # args 通道
engine, *_ = deepspeed.initialize(args=args, model=model, config=config)  # config 通道也给了
```

2. 运行并记录报错信息；然后删掉冲突项，分别用三种方式各跑一次：(a) `config=dict`；(b) 先 `json.dump` 写文件再 `config="config.json"`；(c) args 通道 `args.deepspeed_config="config.json"`。

**需要观察的现象**：

- 步骤 1 抛出 `AssertionError`，消息包含 `"Not sure how to proceed, we were given deepspeed configs in the deepspeed arguments and deepspeed.initialize() function call"`。
- 步骤 2 的三种方式训练行为完全一致（loss 序列相同）。

**预期结果**：冲突断言必现（L537 的 `assert config is None`）；三种合法传法等价。loss 具体数值**待本地验证**（同一随机种子下三种方式应输出相同序列，可作为等价性的判据）。

#### 4.3.5 小练习与答案

**练习 1**：`config_params` 和 `deepscale_config` 分别是什么？为什么还留在代码里？

答案：`config_params` 是 `config` 参数的旧名，为不破坏老脚本保留（`config is None and config_params is not None` 时自动顶上）；`args.deepscale_config` 是更早的 DeepScale 时代的配置字段，遇到会打弃用警告并转移到 `deepspeed_config`。两者都是向后兼容的历史包袱。

**练习 2**：配置字符串既能是文件路径又能是 base64 串，`load_ds_config` 如何区分？

答案：不预先区分，而是先试后试：`os.path.exists` 成立就当文件读；否则尝试 `base64.urlsafe_b64decode` 后按 hjson 解析；两者都失败才抛 `ValueError`。文件路径天然不会是合法 base64+JSON，base64 串也大概率不是存在的路径，顺序尝试即可安全区分。

**练习 3**：为什么用 `hjson` 而不是标准 `json` 解析配置文件？

答案：hjson 是 JSON 的超集，允许注释、尾逗号和不加引号的键——训练配置需要注释来说明调参意图。同时 `object_pairs_hook=dict_raise_error_on_duplicate_keys` 弥补了 hjson 默认容忍重复键的隐患：配置里同一个键写两遍会直接报错，而不是静默取最后一个。

### 4.4 AutoSP 前置校验与 mesh device 自动创建（本周期 #8457 新增）

#### 4.4.1 概念说明

AutoSP（自动序列并行）是 DeepCompile 编译器的一个 pass：在编译后的计算图内部把输入序列切分到多个 rank 上（思想与 u6-l2 的 Ulysses 同源，但由编译器自动插入通信）。本周期 PR #8457 修正了 AutoSP 的正确性（SP 组上聚合 loss 与梯度），同时在 `initialize()` 里加上了**前置校验**和 **mesh device 自动创建**，防止用户配出一个「静默算错」的环境。

为什么要在 `initialize()` 就校验？源码注释给出了精确理由：**如果 AutoSP 后端没有真正运行，每个 rank 都会在完整序列上训练，而引擎却仍会把序列并行各 rank 的梯度加起来**——结果不是报错而是悄悄错掉。与其让错误在几千步训练后才暴露，不如在初始化的第一分钟就 `ValueError`。这是「fail fast」原则的教科书式应用。

校验触发的判定函数 `_autosp_sequence_parallel_size()` 很谨慎：只有 `compile.passes` 列表里**明确含有** `"autosp"` 时才去读 `sequence_parallel_size`，否则一律返回 1（即不触发校验）——单纯配了 `sequence_parallel_size` 而没开 autosp pass 的老配置完全不受影响。

#### 4.4.2 核心流程

AutoSP 相关的完整判定流程：

```text
_autosp_sequence_parallel_size(config):
    compile.passes 不含 "autosp"          → 返回 1（什么都不做）
    否则返回 config.sequence_parallel_size（默认 1）

initialize 内（sp = 上面返回值 > 1 时）:
    compile.deepcompile 不为 true          → ValueError: "AutoSP requires DeepCompile; ..."
    ZeRO stage >= 2（含 bool 形式取 int）  → ValueError: "AutoSP does not support ZeRO stage N; ..."
    
mesh device 创建（三分支，按优先级）:
    1. 显式 mesh_param 传入                → 用它建 (data_parallel, sequence_parallel) mesh
    2. config 同时含 data_parallel_size 和 sequence_parallel_size → 按配置建
    3. mpu 为 None 且 sp > 1（AutoSP 场景）→ 校验 world_size % sp_size == 0（否则 ValueError）
                                            → 自动建 (world_size // sp_size, sp_size) mesh
```

为什么分支 3 需要自动建 mesh？源码注释同样给出理由：AutoSP 在**编译图内部**切分序列，但**引擎**（引擎侧的梯度归约）仍然需要序列并行这个维度——在 SP 组的 rank 之间求和梯度、在 DP 组的 rank 之间取平均。没有 mesh，引擎就不知道哪些 rank 属于同一个 SP 组。

#### 4.4.3 源码精读

**判定函数**——[deepspeed/__init__.py:L433-L437](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L433-L437)

从 config dict 里取 `compile.passes`（空则视为空列表），不含 `"autosp"` 返回 1；含则返回 `sequence_parallel_size`（默认 1）。注意它操作的是**原始 config dict**而非 `DeepSpeedConfig` 对象——校验发生在 `DeepSpeedConfig` 构造之前，只依赖原始字典字段。

**两道校验**——[deepspeed/__init__.py:L544-L556](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L544-L556)

第一道：`compile.deepcompile` 不为 `True` 直接抛 `"AutoSP requires DeepCompile; set compile.deepcompile to true"`——只有 DeepCompile 编译器真正运行 AutoSP 后端。第二道：解析 ZeRO stage 时特意处理了 `zero_optimization` 的**布尔弃用形式**（`isinstance(zero_config, bool)` 时取 `int()`，True 即 stage 1），然后 stage >= 2 抛 `"AutoSP does not support ZeRO stage N; use ZeRO stage 0 or 1"`——ZeRO 2/3 会切分梯度/参数，与引擎在 SP 维度上的梯度聚合冲突。

**mesh device 三分支**——[deepspeed/__init__.py:L558-L576](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/__init__.py#L558-L576)

L559-561 处理显式 `mesh_param`；L563-567 处理配置里同时给了 `data_parallel_size` 与 `sequence_parallel_size` 的情况；L568-576 是本周期新增的第三分支——`mpu is None` 且 AutoSP 激活时，先检查 `dist.get_world_size() % sp_size != 0` 就抛带具体数值的 `ValueError`，再调 `dist.initialize_mesh_device((world_size // sp_size, sp_size), ("data_parallel", "sequence_parallel"))` 自动补建。

**initialize_mesh_device 的实现**——[deepspeed/comm/comm.py:L812-L824](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/comm/comm.py#L812-L824)

它断言通信后端已初始化，然后委托给 `cdb.init_device_mesh(mesh_shape, dim_names)`（PyTorch 的 device mesh，把 world 按形状切成带名字的维度网格）；后端不支持时只打 warning 并返回 `None`。

**回归测试（本讲的「标准答案」）**——[tests/unit/v1/compile/test_compile_autosp.py:L428-L442](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/v1/compile/test_compile_autosp.py#L428-L442)

`test_rejects_autosp_without_deepcompile` 构造 `passes=["autosp"]` + `sequence_parallel_size=2` 但不开 `deepcompile` 的配置，断言 `initialize` 抛出匹配 `"AutoSP requires DeepCompile"` 的 `ValueError`——注意这个用例**没有** CPU skip 标记，说明单进程 CPU 就能复现。相邻的 [L404-L426](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/v1/compile/test_compile_autosp.py#L404-L426) 参数化验证 ZeRO stage 2/3 被拒（CPU skip），[L444-L459](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/v1/compile/test_compile_autosp.py#L444-L459) 验证布尔形式 `zero_optimization: True`（即 stage 1）能通过校验。

#### 4.4.4 代码实践

**实践目标**：亲手触发 AutoSP 前置校验，并对照测试确认报错信息。

**操作步骤**：

1. 在 4.1.4 脚本的基础上新增一段（示例代码）：

```python
# 示例代码：验证 AutoSP 前置校验的报错
bad_config = {
    "train_micro_batch_size_per_gpu": 4,
    "compile": {"passes": ["autosp"]},   # 开了 autosp pass……
    "sequence_parallel_size": 2,          # ……SP size > 1
    # 但没有 compile.deepcompile = true
}
try:
    deepspeed.initialize(model=SimpleModel(hidden_dim), config=bad_config)
except ValueError as e:
    print("caught:", e)
```

2. 运行脚本，记录 `ValueError` 的完整消息。
3. 把 `"deepcompile": True` 加进 `compile` 字典再跑——这次应越过第一道校验；如果你的环境是单进程（world_size=1），会在 mesh device 分支遇到 `world size 1 is not divisible by sequence_parallel_size 2` 的 `ValueError`（分支 3 的整除断言）。
4. 对照 [tests/unit/v1/compile/test_compile_autosp.py:L428-L442](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/v1/compile/test_compile_autosp.py#L428-L442) 的断言文字，确认你抓到的异常和 CI 测的一致。

**需要观察的现象**：

- 步骤 2 打印 `caught: AutoSP requires DeepCompile; set compile.deepcompile to true`。
- 步骤 3 换成整除性报错（单进程时），消息含具体的 world size 和 sp_size 数值。

**预期结果**：两道校验按 4.4.2 的流程图依次命中。第一道校验发生在任何 world size 检查之前，因此单进程 CPU 环境即可复现（这也解释了为什么对应测试不设 CPU skip）；第二道（ZeRO stage）的复现需真实多卡环境（对应测试 CPU skip），**待本地验证**（GPU 环境可参照 L404-L426 的参数化用例跑 `pytest tests/unit/v1/compile/test_compile_autosp.py -k "rejects"`）。

#### 4.4.5 小练习与答案

**练习 1**：配置里写了 `sequence_parallel_size: 4` 但 `compile.passes` 是 `[]`，`initialize()` 会校验失败吗？

答案：不会。`_autosp_sequence_parallel_size()` 先检查 `compile.passes` 是否含 `"autosp"`，不含就直接返回 1，根本不读 `sequence_parallel_size`。校验只针对「明确开了 autosp pass」的配置，这正是新校验不破坏存量配置的关键设计。

**练习 2**：AutoSP 为什么不支持 ZeRO stage 2/3？源码注释是怎么解释的？

答案：源码注释（L545-546）说明：没有 AutoSP 后端时每个 rank 在完整序列上训练、引擎却仍会在 SP rank 上累加梯度——而 ZeRO 2/3 场景下这些 stage「编译时不带 AutoSP」且与 SP 梯度聚合的切分方式冲突，故只允许 stage 0/1。核心是：只有 DeepCompile 运行 AutoSP 后端，且它不与 ZeRO 2/3 组合。

**练习 3**：分支 3 自动建 mesh 时，`(data_parallel, sequence_parallel)` 两个维度各取什么值？为什么需要整除检查？

答案：SP 维度取配置的 `sp_size`，DP 维度取 `world_size // sp_size`（例如 world_size=8、sp=2 时 mesh 是 `(4, 2)`）。整除检查是因为 device mesh 必须恰好铺满所有 rank——world size 不能整除 SP size 时存在无法分配的剩余 rank，只能报错，不能静默丢弃。

## 5. 综合实践

把本讲四个模块串成一个可复用的实验台脚本（示例代码）：

```python
# ds_playground.py（示例代码）：本讲综合实践
import argparse
import torch
import deepspeed
from simple_model import SimpleModel, random_dataset, create_config_from_dict


def base_config():
    return {
        "train_micro_batch_size_per_gpu": 4,
        "gradient_accumulation_steps": 2,  # train_batch_size = 4 x 2 x 1 = 8
        "optimizer": {"type": "Adam", "params": {"lr": 1e-3}},
    }


def run_training(config_dict):
    """模块 4.1/4.2：三步接入法 + SimpleModel 实验台。"""
    model = SimpleModel(hidden_dim=32)
    engine, optimizer, dataloader, lr_scheduler = deepspeed.initialize(
        model=model,
        training_data=random_dataset(64, 32, torch.device("cpu"), dtype=torch.float32),
        config=config_dict)
    for step in range(3):
        print(f"step {step}: loss = {engine.train_batch().item()}")


def run_autosp_check():
    """模块 4.4：验证 AutoSP 前置校验。"""
    cfg = base_config()
    cfg["compile"] = {"passes": ["autosp"]}
    cfg["sequence_parallel_size"] = 2  # 未开 deepcompile → 应被拒绝
    try:
        deepspeed.initialize(model=SimpleModel(32), config=cfg)
    except ValueError as e:
        print("AutoSP check caught:", e)


if __name__ == "__main__":
    run_training(base_config())      # 先证明实验台能正常训练
    run_autosp_check()               # 再证明错误配置会被 fail fast
```

任务清单：

1. 跑通 `run_training`，记录三步 loss（验证 4.1 的三步接入法与 4.2 的 SimpleModel）。
2. 把 `base_config()` 分别通过「dict 直传」「写 JSON 文件传路径」「塞进 `args.deepspeed_config`」三种方式喂给 `initialize`（验证 4.3 的双通道），确认 loss 序列一致。
3. 跑 `run_autosp_check`，确认异常消息与 `tests/unit/v1/compile/test_compile_autosp.py` 的断言文字一致（验证 4.4）。
4. 进阶：把 `run_autosp_check` 的配置补上 `"deepcompile": True` 与 `"zero_optimization": {"stage": 2}`，看报错是否变成 ZeRO stage 那一道（GPU 环境，待本地验证）。

脚本运行结果（具体 loss 数值与异常消息）**待本地验证**。

## 6. 本讲小结

- `deepspeed.initialize()` 是训练侧唯一入口：必填只有 `model`，配置必须来自 `config` 参数或 `args.deepspeed_config` 之一，返回 `(engine, optimizer, dataloader, lr_scheduler)` 四元组。
- 引擎类由推断得出：`PipelineModule` → `PipelineEngine`/`DualPipeVEngine`；开 `hybrid_engine` → `DeepSpeedHybridEngine`；默认 `DeepSpeedEngine`。引擎构造期间会临时关闭 `zero.Init` 上下文。
- 配置字符串先按文件路径（hjson + 重复键检查）解析、再回退 base64——后者服务于启动器的编码传递；`config_params` 与 `deepscale_config` 是向后兼容的历史通道。
- 本周期 #8457 新增 AutoSP 前置校验：`compile.passes` 含 `autosp` 且 SP size > 1 时强制 `deepcompile=true`、ZeRO stage < 2，把「静默算错」变成初始化即报的 `ValueError`；判定函数对未开 autosp pass 的配置完全无感。
- 同一 PR 还补了 mesh device 自动创建：无 `mpu` 且 AutoSP 激活时按 `(world_size // sp_size, sp_size)` 建 `(data_parallel, sequence_parallel)` mesh（整除性检查先行），因为引擎侧梯度归约仍需要 SP 维度。
- `tests/unit/simple_model.py` 是全仓共享的实验台：`SimpleModel` 返回标量 loss、`random_dataset` 造数据、`args_from_dict` 造 args 通道配置——自己动手做实验优先复用它。

## 7. 下一步学习建议

- **u2-l2（配置系统）**：本讲把 config dict 当黑盒用了，下一讲深入 `DeepSpeedConfig`，搞清 batch size 推导公式、各配置段的校验与展开。
- **u2-l3（引擎构造与销毁）**：本讲只看到引擎被选出来，下一讲逐段读 `DeepSpeedEngine.__init__` 的初始化阶段清单与 `destroy()` 生命周期。
- **u2-l4（训练主循环）**：本讲用了 `engine.train_batch()` 一站式接口，下一讲拆开它的内部时序，并学会 `engine.backward`/`engine.step` 的手动模式。
- 源码延伸阅读：`deepspeed/runtime/engine.py` 的 `DeepSpeedEngine.__init__`（配合 u2-l3），以及 `tests/unit/v1/compile/test_compile_autosp.py` 全文——它同时覆盖 AutoSP 的正确性用例，是理解序列并行语义的最佳测试标本。
