# SFT 训练主流程 SwiftSft

## 1. 本讲目标

前面几讲我们分别拆解了支撑训练的「零件」：参数体系（u2）、模型加载（u3-l1）、对话模板（u3-l3）、数据编码与打包（u4-l3）、训练器派发（u5-l1）、Tuner 适配（u5-l2）。本讲要回答一个整合性的问题：

> 当你在命令行敲下 `swift sft ...`，这些零件是按什么顺序、被谁串成一条完整训练流水线的？

学完本讲你应当能够：

- 说清 `SwiftPipeline` 基类与 `SwiftSft` 的关系（模板方法模式：基类定骨架、子类填 `run`）。
- 掌握 `SwiftSft.__init__` 里 `_prepare_model_tokenizer` / `_prepare_template` / `_prepare_flash_ckpt` 这「三连准备」各自做了什么、为什么要放在构造函数里。
- 读懂 `run()` 如何把「数据集准备 → `prepare_model` → `TrainerFactory` 装配 → `train()`」编排在一起，并指出**数据集编码发生在哪个阶段**。
- 理解 `@RayHelper.worker` / `@RayHelper.function` 装饰器让同一份代码在「单机直跑」和「Ray 分布式远端」之间透明切换的机制。

## 2. 前置知识

本讲是 u5 训练单元的收束篇，需要以下前置（对应讲义依赖）：

- **u3-l3 Template 体系**：知道 `template.encode` 把 messages 变成 `input_ids/labels/loss_scale`，以及 `support_padding_free` 约束。
- **u4-l3 编码与 Packing**：知道 `EncodePreprocessor`、`LazyLLMDataset`、`PackingDataset` 的职责与「编码可以被延迟」的事实。
- **u5-l1 TrainerFactory**：知道 `TrainerFactory.get_trainer_cls(args)` 按 `task_type` 派发 Trainer 类，Trainer 以 template 为中心装配。
- **u5-l2 TunerPlugin**：知道 `TunerMixin.prepare_model` 是真正指挥「冻结基座 + 挂载增量」的地方，`is_adapter` 区分 adapter 微调与全量微调。

此外需要两个通用概念：

- **模板方法模式（Template Method）**：父类定义算法骨架（一组按序调用的步骤），把其中可变的步骤声明为抽象方法，交由子类实现。本讲会看到 `SwiftPipeline.main()` 就是骨架，`run()` 是交由子类实现的步骤。
- **装饰器（Decorator）**：Python 中用 `@xxx` 语法把一个函数/类「包」一层，在不改原代码的前提下追加行为。`@RayHelper.function` 就是给方法套一层「本地直跑 or 远端分发」的判断壳。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/pipelines/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py) | `SwiftPipeline` 抽象基类，定义所有管道（sft/rlhf/export/infer…）的公共骨架：参数解析、设种子、`main()` 模板方法、抽象 `run()`。 |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | `SwiftSft` 管道，SFT 训练的总编排者：构造期「三连准备」、`run()` 编排数据集/模型/Trainer、`train()` 启动训练并收尾。`sft_main` 是对外入口。 |
| [swift/pipelines/train/tuner.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py) | `TunerMixin`：把 u5-l2 讲过的「模型适配」能力以 mixin 形式注入 `SwiftSft`；`prepare_adapter` 是各轻量微调方法的配置派发函数。 |
| [swift/ray_utils/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py) | `RayHelper`：`worker`/`function` 两个装饰器，让管道方法在单机/分布式两种运行形态间透明切换。 |
| [swift/utils/processor_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/processor_utils.py) | `ProcessorMixin`：提供 `tokenizer` 属性，从 `processor` 里透明取 tokenizer，是 `SwiftPipeline` 的另一个父类。 |
| [swift/cli/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py) | `swift sft` 子命令的真实执行脚本（被 main.py 经 torchrun 拉起），最终调用 `sft_main()`。 |

## 4. 核心概念与源码讲解

### 4.1 SwiftPipeline 基类：所有管道的公共骨架

#### 4.1.1 概念说明

ms-swift 有很多「管道（Pipeline）」：`SwiftSft`（训练）、`SwiftRLHF`（强化学习）、`SwiftExport`（导出）、`SwiftInfer`（推理）、`SwiftApp`（交互界面）…… 它们的「开头」都长得一样：解析命令行参数 → 打日志 → 设随机种子 → 跑业务逻辑 → 打日志。如果把这段重复逻辑抄到每个子类里，既冗余又难维护。

`SwiftPipeline` 就是为消除这段重复而生的抽象基类。它用**模板方法模式**：把「解析参数、设种子、计时」这些固定步骤写死在基类的 `main()` 里，而把「真正的业务逻辑」声明为抽象方法 `run()`，交给每个子类去实现。于是子类只需关心 `run()`，其余骨架自动复用。

`SwiftPipeline` 还多继承了 `ProcessorMixin`（[swift/utils/processor_utils.py:16-30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/processor_utils.py#L16-L30)），后者提供一个 `tokenizer` 只读属性——当 `processor` 是多模态的 `ProcessorMixin`（含 `.tokenizer` 子对象）时自动下钻，是纯文本则直接就是 tokenizer。这让管道代码可以统一写 `self.tokenizer`，不必关心模型是纯文本还是多模态。

#### 4.1.2 核心流程

`SwiftPipeline` 定义的生命周期是：

```text
new SwiftPipeline(argv)
   ├─ _parse_args(argv)      # 命令行/已解析对象 → self.args，校验剩余参数
   ├─ 打印 args
   ├─ _set_seed()            # 用 args.seed + rank 设全局随机种子
   └─ _compat_dsw_gradio()   # 魔搭 DSW 平台的 Gradio 路径兼容（仅 web-ui/app 生效）

pipeline.main()
   ├─ 打印开始时间、swift 版本
   ├─ self.run()             # 抽象方法，子类实现（SwiftSft.run 即训练主流程）
   └─ 打印结束时间
```

注意分工：**构造函数只做「轻量准备」（解析参数、设种子），不做重活**；真正耗时的模型加载、训练都在 `run()` 里，由 `main()` 调用。这种「构造即返回、运行在 main」的设计，是让 Ray 能在构造期就把 worker 拉起来的前提（见 4.3）。

#### 4.1.3 源码精读

基类定义与构造函数——它继承 `ABC`（抽象）和 `ProcessorMixin`（提供 `tokenizer`），声明类属性 `args_class` 作为参数类的「占位」，子类覆盖为具体类型（`SwiftSft` 覆盖成 `SftArguments`）：

[swift/pipelines/base.py:14-22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L14-L22) —— `SwiftPipeline` 是抽象基类，构造函数解析参数、设种子。

参数解析 `_parse_args` 是 u2-l1 讲过的闭环入口：传入若是 `args_class` 实例则原样返回（方便测试/复用），否则用 `parse_args`（基于 HfArgumentParser）解析，并对 `remaining_argv`（没匹配到任何字段的剩余参数）做拦截——默认报错，`--ignore_args_error` 时仅 warning：

[swift/pipelines/base.py:31-41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L31-L41) —— `_parse_args` 解析命令行并校验剩余参数，拼错的参数名会在这里被拦下。

`main()` 就是模板方法本身——固定地「计时 → run → 计时」，把可变业务完全委托给 `self.run()`：

[swift/pipelines/base.py:49-58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L49-L58) —— `main()` 是骨架，`run()` 是 `@abstractmethod`，强制子类实现。

#### 4.1.4 代码实践

**实践目标**：验证「基类定骨架、子类填 run」的模板方法结构。

**操作步骤**：

1. 在已安装 ms-swift 的环境里，打开 Python 交互终端。
2. 执行下面这段「示例代码」（非项目原有代码），构造一个最小管道子类并观察调用顺序：

```python
# 示例代码：用于观察 SwiftPipeline 的模板方法行为
from swift.pipelines.base import SwiftPipeline

class DemoPipe(SwiftPipeline):
    args_class = None  # 跳过参数解析以简化演示

    def _parse_args(self, args):
        # 覆盖掉参数解析，避免需要真实参数类
        self.args = args
        return args

    def run(self):
        print('  -> run() 被调用，业务逻辑在这里')
        return 'done'

p = DemoPipe('some-args')
print('构造完成，run 尚未执行')
result = p.main()
print('main 返回:', result)
```

**需要观察的现象**：终端会先打印 `swift.__version__` 与开始时间，再打印 `-> run() 被调用`，最后打印结束时间——证明 `main()` 确实把控制权交给了子类的 `run()`。

**预期结果**：`main()` 的日志包裹住 `run()` 的输出，验证模板方法模式生效。若想看真实子类，可读 `SwiftSft` 的 `run`（4.4 节）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SwiftPipeline` 的 `run()` 上面的 `@abstractmethod` 去掉，会出什么问题？
**参考答案**：子类忘记实现 `run()` 时不再报错，`main()` 里的 `self.run()` 会调用到基类的空 `pass` 实现，训练静默地「什么都没干」就结束。`@abstractmethod` 的意义就是把这个错误提前到实例化阶段。

**练习 2**：为什么 `_parse_args` 要额外接受「已经是 `args_class` 实例」的入参直接返回？
**参考答案**：为了支持「外部已经解析好参数对象、直接复用」的场景（如测试、或在 RLHF 管道里把同一份参数对象传给复用的 Sft 逻辑），避免重复解析命令行。

---

### 4.2 SwiftSft 准备阶段：构造函数里的「三连准备」

#### 4.2.1 概念说明

`SwiftSft` 是 SFT 训练的总编排者。它的类声明体现了 ms-swift 的典型组合方式——多继承加 mixin：

```python
@RayHelper.worker(group=['default'])
class SwiftSft(SwiftPipeline, TunerMixin):
    args_class = SftArguments
```

- 继承 `SwiftPipeline`：拿到 `main()` 骨架与参数解析能力。
- 混入 `TunerMixin`：拿到 `prepare_model()` 方法（u5-l2 讲过），让管道能调用「冻结基座 + 挂载 LoRA 增量」的适配逻辑。
- `args_class = SftArguments`：把基类的占位换成具体的 SFT 参数类。

最有意思的是它的**构造函数把模型加载、模板实例化放在了「构造期」而非 `run()` 里**。这看起来违反了 4.1 节「构造只做轻量准备」的原则，背后其实是为了配合 Ray 分布式：构造函数在 worker 进程里执行，把昂贵的模型加载就地完成，后续 `run()` 直接用现成的 `self.model` 即可。`@RayHelper.worker` 装饰器会改写 `__init__` 来支持这一点（详见 4.3）。

#### 4.2.2 核心流程

`SwiftSft.__init__` 的「三连准备」：

```text
__init__(args)
   ├─ super().__init__(args)            # SwiftPipeline 构造：解析参数、设种子
   ├─ self.train_msg = {}               # 训练信息收集字典（供训练后落盘）
   ├─ _prepare_model_tokenizer()        # ① 加载 (model, processor)，按需接入序列并行
   ├─ _prepare_template()               # ② 实例化 template 并切到 train 模式，校验 padding_free
   └─ _prepare_flash_ckpt()             # ③ 按需启用 dlrover 异步 checkpoint
```

注意这三步的**依赖顺序**：模板实例化可能要用到 processor（tokenizer），所以模型/processor 必须先于模板加载；而 `_prepare_template` 里对 `support_padding_free` 的校验又依赖模板是否多模态（来自 `args.model_meta`，在加载模型时被回填）。顺序不是随便排的。

#### 4.2.3 源码精读

类声明与构造函数——三连准备的入口：

[swift/pipelines/train/sft.py:21-31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L21-L31) —— `SwiftSft` 继承 `SwiftPipeline + TunerMixin`，构造函数依次准备模型、模板、flash checkpoint。

**① `_prepare_model_tokenizer`**：调用 `args.get_model_processor()`（u3-l1 讲过的「id → (model, processor)」入口，[swift/arguments/base_args/base_args.py:331-350](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L331-L350)）加载基座模型与 processor；若开启序列并行（`sequence_parallel_size > 1`）则就地 `sequence_parallel.prepare` 给模型打补丁；最后调 `_prepare_generation_config` 用推理参数补全 `generation_config`：

[swift/pipelines/train/sft.py:48-62](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L48-L62) —— 加载模型与 processor，接入序列并行，补全 generation_config。

**② `_prepare_template`**：调 `args.get_template(self.processor)`（[swift/arguments/base_args/base_args.py:319-329](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L319-L329)）实例化模板；`template.set_mode('train')` 切到训练模式（让 encode 产出 labels，见 u3-l3）；若模板需要模型（`template.use_model`，如某些模板要在编码时跑模型取 logits）则把 `self.model` 挂上去；接着做 **padding_free/packing 兼容性校验**——当 `support_padding_free is None` 时按「是否多模态」推断（多模态默认不支持，见 u3-l4），强行对不支持的模板开 `padding_free` 或 `packing` 会直接报错：

[swift/pipelines/train/sft.py:64-76](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L64-L76) —— 实例化训练模板并校验 padding_free/packing 兼容性。

**③ `_prepare_flash_ckpt`**：若 `args.use_flash_ckpt` 为真，尝试导入 dlrover 的异步 checkpoint 模块；导入失败则给出明确的安装提示。它本身没有重逻辑，是一个「按需启用」的开关：

[swift/pipelines/train/sft.py:33-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L33-L39) —— 按需启用 dlrover 异步 checkpoint。

#### 4.2.4 代码实践

**实践目标**：理解构造期三连准备的顺序与依赖。

**操作步骤**：

1. 通读 [swift/pipelines/train/sft.py:26-31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L26-L31) 的 `__init__`。
2. 追踪依赖链：`_prepare_template` 里第 67 行 `args.get_template(self.processor)` 需要一个非 None 的 processor，而 processor 来自第 51 行 `_prepare_model_tokenizer` 的返回值。思考：如果把这两行调用顺序对调，会发生什么？
3. 再看 [swift/pipelines/train/sft.py:71-75](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L71-L75) 的 `support_padding_free` 推断：`args.model_meta.is_multimodal` 是在哪一步被填好的？（提示：在 `_prepare_model_tokenizer` 加载模型时，`ModelMeta` 被挂回 `model.model_meta`，见 u3-l1。）

**需要观察的现象**：你会发现三连之间存在严格的「模型 → 模板」数据依赖，顺序不可打乱。

**预期结果**：能用自己的话解释「为什么模型加载必须在模板实例化之前」——因为模板需要 processor（tokenizer），且 padding_free 校验依赖 `model_meta.is_multimodal`，而这两者都是加载模型的产物。

#### 4.2.5 小练习与答案

**练习 1**：`_prepare_template` 里为什么要单独 `template.set_mode('train')`？
**参考答案**：同一个 Template 类既服务训练也服务推理（u3-l3、u6）。训练模式下 `encode` 会产出 `labels`（非回答段填 `-100`）；推理模式下 labels 为 None。管道显式切到 train 模式，确保后续数据编码产出的 labels 可用于算 loss。

**练习 2**：`self.train_msg = {}` 这个字典最终会被写到哪个文件？（可先猜，再到 4.4 节验证。）
**参考答案**：会被 `_save_trainer_state` 汇总并 `append_to_jsonl` 写入 `output_dir/logging.jsonl`（见 4.4 源码精读），记录 checkpoint 路径、global_step、loss 曲线、显存峰值等。

---

### 4.3 RayHelper 装饰器：单机直跑与分布式远端的透明切换

#### 4.3.1 概念说明

你会注意到 `SwiftSft` 的几乎所有方法上都挂着 `@RayHelper.function(group='default')`，类上挂着 `@RayHelper.worker(group=['default'])`。这层装饰器是 ms-swift 实现「单卡与多机分布式用同一份代码」的关键。

它的核心思想是**双模透明**：

- **单机/未启用 Ray 时**：装饰器几乎什么都不做——`RayHelper.ray_inited()` 返回 False，装饰器直接调用原函数，开销可忽略。于是 `swift sft` 在单卡上跑和没有这层装饰器一模一样。
- **启用 Ray 时**（通过 `try_init_ray()` 初始化，[swift/cli/sft.py:17-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L17-L18)）：`@RayHelper.worker` 把类变成 Ray 的 remote actor，`@RayHelper.function` 把方法调用转成对远端 actor 的分发，并按 `dispatch`/`execute`/`collect` 策略聚合结果。

两个装饰器分工：

- `worker` 装饰**类**：决定「这个类要不要被搬到 Ray actor 上」。单机时原样返回类；启用 Ray 且当前进程是 driver 时，用 `ray.remote(cls)` 包装，并改写 `__init__` 以便 driver 构造时自动创建远端 worker 副本。
- `function` 装饰**方法**：决定「这次方法调用在本地跑还是分发给 worker 跑」。单机时直接跑；启用 Ray 时，driver 端把调用分发给所有/部分 worker 并收集结果，worker 端则真正执行原逻辑。

#### 4.3.2 核心流程

`@RayHelper.function` 包裹的方法，其执行分三条路径（由运行时形态决定走哪条）：

```text
被装饰方法 wrapper(self, *args):
   if not ray_inited():                  # 单机：直跑
       return func(self, *args, **kwargs)
   if is_worker():                       # 分布式 worker 端：真正干活
       if group not in self.group:
           if is_called_from_init(): return None   # 别组 init 期间的调用，忽略
           else: raise ValueError()
       return func(self, *args, **kwargs)
   else:                                 # 分布式 driver 端：分发 + 收集
       if is_called_from_init(): return None       # init 阶段每个 worker 自己跑
       result = execute_all_sync(group, dispatch, execute, func_name, ...)
       return collect_func(collect, result)
```

`dispatch` 决定参数怎么切（`'all'` 每个 worker 拿全量、`'slice'` 负载均衡切片、或自定义可调用对象），`execute` 决定谁来跑（`'first'` 只让 0 号 worker、`'all'` 全体），`collect` 决定结果怎么聚合（`'none'` 原样、`'flatten'` 拍平、或自定义）。`SwiftSft` 的方法大多用默认值（`all`/`all`/`none`），即「每个 worker 都做同样的事、结果原样返回」，这与数据并行的语义一致：每个 rank 各自加载一份模型、各自训练自己的数据分片。

#### 4.3.3 源码精读

`worker` 装饰器——单机时原样返回类，Ray driver 端则 `ray.remote(cls)` 包装并改写 `__init__`，让 driver 在构造对象时顺手创建远端 worker 副本：

[swift/ray_utils/base.py:91-118](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L91-L118) —— `worker` 装饰类：决定类是否成为 Ray actor，并在 driver 端拦截 `__init__` 创建 worker。

注意第 94-97 行的「双重保护」：`if not RayHelper.ray_inited(): return cls`（单机直通）、`if RayHelper.is_worker(): return cls`（worker 端不再重复包装）。只有 driver 端且 Ray 已初始化时才进入改造分支。

`function` 装饰器——核心是第 167-168 行的「单机直跑」短路，以及 worker/driver 分流：

[swift/ray_utils/base.py:142-191](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L142-L191) —— `function` 装饰方法：单机直跑、worker 执行、driver 分发三态。

`is_called_from_init` 这个小技巧值得注意（[swift/ray_utils/base.py:61-74](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L61-L74)）：它通过检查调用栈里有没有 `__init__` 帧来判断「当前是不是在构造期」。在构造期，driver 端的方法调用直接返回 None——因为每个 worker 的构造是各自独立完成的，driver 不需要也不能替 worker 跑构造期的方法。这正是 4.2 节「构造期就加载模型」能配合 Ray 工作的原因：每个 worker 在自己的 `__init__` 里各自加载模型，driver 不会插手。

回到 `SwiftSft`，看装饰器的实际落点——类上的 `@RayHelper.worker`，`__init__` 里那三连准备方法都挂了 `@RayHelper.function`：

[swift/pipelines/train/sft.py:48-65](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L48-L65) —— `_prepare_model_tokenizer` 和 `_prepare_template` 都被 `@RayHelper.function` 包裹，单机时等同普通方法。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读」方式确认装饰器在单机下「几乎无开销直通」。

**操作步骤**：

1. 阅读 [swift/ray_utils/base.py:166-168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L166-L168)：`wrapper` 的第一件事是 `if not RayHelper.ray_inited(): return func(self, *args, **kwargs)`。
2. 阅读 `ray_inited` 的实现 [swift/ray_utils/base.py:77-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py#L77-L83)：它先尝试 `import ray`，导入失败（单机没装 ray）直接返回 False。
3. 对比 [swift/cli/sft.py:17-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L17-L18)：`try_init_ray()` 只在显式配置 Ray 参数时才真正 `ray.init()`，否则空操作。

**需要观察的现象**：单机场景下，`ray_inited()` 始终为 False，所有 `@RayHelper.function` 方法都走第一行的短路，直接执行原函数。

**预期结果**：能解释「为什么单卡用户完全感受不到 Ray 的存在」——因为装饰器在 `ray_inited()` 为 False 时零成本直通，Ray 相关代码路径根本不会被执行。

#### 4.3.5 小练习与答案

**练习 1**：`dispatch='slice'` 和 `dispatch='all'` 的区别是什么？`SwiftSft` 的方法为什么大多用默认的 `'all'`？
**参考答案**：`'slice'` 把列表型参数按 worker 数均分（负载均衡），适合「把数据集分给各 rank」；`'all'` 让每个 worker 拿到完全相同的参数。`SwiftSft` 的准备方法（加载模型、设模板）需要每个 rank 各自完成一份相同的工作（数据并行下每卡都要有完整模型），所以用 `'all'`；真正的数据分片发生在更底层的 `safe_ddp_context` 与 dataloader 的 DistributedSampler 里，而非靠 `dispatch='slice'`。

**练习 2**：为什么 `__init__` 里调用被装饰的 `_prepare_model_tokenizer`，在 driver 端却不会把模型加载分发出去？
**参考答案**：因为 `is_called_from_init()` 在构造期返回 True，driver 端的 `wrapper` 此时直接 `return None`，不触发 `execute_all_sync`。每个 worker 在自己的进程里独立执行自己的 `__init__`（包括加载模型），互不干扰。

---

### 4.4 run() 主编排：数据集准备、prepare_model、Trainer 装配与训练启动

#### 4.4.1 概念说明

`run()` 是 `SwiftSft` 真正的「导演」，它把前面所有讲义里的零件按正确顺序拼装成一次训练。可以这样理解它的四个动作：

1. **准备数据集**：从命令行指定的数据集名/路径，经过加载（u4-l1）→ 预处理（u4-l2）→ 编码（u4-l3），得到 token 级别的训练集与验证集。
2. **落盘参数**：`args.save_args()` 把当前参数写进 `output_dir/args.json`，供推理/导出回载（u2-l1 的「训练即所见，推理即所得」）。
3. **适配模型**：`self.prepare_model(...)`（来自 `TunerMixin`）把基座模型改造成可训练的微调模型——冻结基座、挂 LoRA 等。
4. **装配并启动 Trainer**：`TrainerFactory.get_trainer_cls(args)` 选 Trainer 类，用模型/参数/模板/数据集构造它，最后 `self.train(trainer)` 调 `trainer.train()` 开训。

本节要特别回答实践任务里的核心问题：**数据集编码到底发生在哪个阶段？** 答案是：编码不是「一次性全做完」，而是分两层——`_encode_dataset` 先给每条样本补一个 `lengths` 字段（用于 packing 装箱统计），真正的 `input_ids` 编码在 `_post_process_datasets` 里（或被 `LazyLLMDataset` 延迟到训练取数时）才完成。

#### 4.4.2 核心流程

`run()` 的完整编排（这就是实践任务要画的时序图）：

```text
SwiftSft.main()                         # 基类模板方法
  └─ SwiftSft.run()
       ├─ _prepare_dataset()
       │     ├─ _get_dataset()
       │     │     └─ args.load_dataset()           # u4-1：加载 + u4-2：预处理成 messages
       │     ├─ _encode_dataset()                   # 给样本补 lengths（不固化 token）
       │     │     └─ AddLengthPreprocessor / EncodePreprocessor
       │     ├─ concat_datasets()                   # 拼接多数据集
       │     └─ _post_process_datasets()            # ★ 真正编码 / LazyLLM / Packing 在这里
       │           ├─ LazyLLMDataset(template.encode, ...)   # 延迟编码容器
       │           └─ PackingDataset(...) if args.packing     # 装箱（强制 padding_free）
       ├─ args.save_args()                           # 落盘 args.json
       ├─ self.prepare_model(args, model, template, train_dataset)   # u5-2：冻结+挂载
       ├─ TrainerFactory.get_trainer_cls(args)       # u5-1：按 task_type 选 Trainer
       ├─ trainer = trainer_cls(model, training_args, template, train_dataset, val_dataset, ...)
       └─ self.train(trainer)
              ├─ _get_resume_checkpoint(trainer)     # 断点续训检查
              ├─ trainer.train(resume_checkpoint)    # ★ 真正训练循环（HF Trainer）
              └─ _save_trainer_state(trainer)        # 收尾：checkpoint 路径、loss 曲线落盘
```

数据集编码发生的位置：`_prepare_dataset` 内部的 `_post_process_datasets` 阶段（即 `run()` 的第一步内部）。若开启 `lazy_tokenize`，则连这一步都只是包一层 `LazyLLMDataset`，真正的 `template.encode` 被推迟到训练时 dataloader 取数才执行。

#### 4.4.3 源码精读

`run()` 全貌——四步编排，注意 `prepare_model` 在数据集之后、Trainer 构造之前：

[swift/pipelines/train/sft.py:159-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L159-L185) —— `run()` 编排：准备数据集 → 落盘参数 → `prepare_model` 适配 → TrainerFactory 装配 → `train()` 启动。

第 170 行的注释透露一个关键细节：「Some tuners require train_dataset and data_collator for preparation: LoRA-GA」——LoRA-GA 这种初始化方式需要在挂载 LoRA 时就用真实数据跑前向算初始化方向，所以 `prepare_model` 必须在拿到 `train_dataset` 之后才能调用。这也是为什么 `prepare_model` 没有「图省事」放到构造函数里：它依赖数据集。

`_prepare_dataset` 编排数据链路，并保留对 RLHF 的兼容（GRPO/GKD 不在 SFT 这里预处理，因为它们的「编码」语义不同，由 RLHF 管道接管）：

[swift/pipelines/train/sft.py:96-123](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L96-L123) —— `_prepare_dataset` 串联加载、补长度、拼接、后处理；`pre_process=False` 时（GRPO/GKD）提前返回不编码。

`_post_process_datasets` 是「编码真正发生」的地方：默认把数据集包进 `LazyLLMDataset`（运行时按需 `template.encode`）；若开启 packing 则再套 `PackingDataset`（装箱，u4-3 讲过 packing 必然强制 padding_free）；流式模式下用 `EncodePreprocessor` 直接编码。注意它对 `predict_with_generate` 的 val_dataset 做了豁免（验证时要生成文本，不能预先把 labels 填死）：

[swift/pipelines/train/sft.py:125-157](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L125-L157) —— `_post_process_datasets`：LazyLLMDataset 包装 / PackingDataset 装箱 / 流式编码三选一。

`_encode_dataset` 做的是「轻量预处理」：默认用 `AddLengthPreprocessor` 只追加 `lengths` 字段（给 packing 统计用，不固化 token），仅当 `truncation_strategy == 'split'`（预训练截断拼接）时才用完整 `EncodePreprocessor`。它还顺手把 `template.model` 临时置 None 防止序列化模型对象：

[swift/pipelines/train/sft.py:298-337](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L298-L337) —— `_encode_dataset` 默认只补 lengths，把重编码推迟到 `_post_process_datasets`。

`prepare_model` 来自 `TunerMixin`（u5-l2 详解过）：按 `is_adapter` 分流——adapter 微调先 `requires_grad_(False)` 冻结全模型，再按 `tuner_type` 派发到 `tuner.prepare_model`（少数自定义序列化的 tuner）或通用的 `prepare_adapter`（lora/llamapro/vera 等绝大多数）；全量微调则 `requires_grad_(True)` 后按比例冻结：

[swift/pipelines/train/tuner.py:338-389](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L338-L389) —— `TunerMixin.prepare_model`：adapter 分支冻结基座再挂增量；full 分支解冻后选择性冻结。

Trainer 装配——`TrainerFactory.get_trainer_cls(args)` 选类（u5-l1），再用「模型 + training_args + template + 数据集」构造。注意 `template=self.template` 被显式传入：Trainer 的 `data_collator` 与 loss 计算都委托给 template（u5-1 讲过的「以 template 为中心装配」）：

[swift/pipelines/train/sft.py:176-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L176-L185) —— 选 Trainer 类并构造，`template` 一并注入，最后 `self.train(trainer)`。

`train()` 与收尾——先算出 `resume_checkpoint`（支持断点续训、dlrover flash ckpt、DeepSpeed elastic 通用 checkpoint 回退），再 `trainer.train()`，最后在 `finally` 里无论成功失败都 `_save_trainer_state` 落盘训练信息：

[swift/pipelines/train/sft.py:254-265](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L254-L265) —— `train()`：算续训点 → `trainer.train()` → finally 落盘状态。

`_save_trainer_state` 把 `last/best_model_checkpoint`、`global_step`、`log_history`（loss 曲线）、显存峰值等汇总进 `self.train_msg`，写进 `output_dir/logging.jsonl`——这就是 4.2 练习 2 的答案：

[swift/pipelines/train/sft.py:220-231](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L220-L231) —— 训练信息汇总进 `train_msg` 并落盘 `logging.jsonl`。

#### 4.4.4 代码实践

**实践目标**：梳理 `SwiftSft.main` 的执行顺序，画出从参数解析到 `trainer.train` 的时序图，并指出数据集编码发生在哪个阶段。

**操作步骤**：

1. 在单卡环境跑通 [examples/train/lora_sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh) 里的 LoRA SFT 示例（用 Qwen2.5-7B-Instruct + alpaca 数据各 500 条 + self-cognition 500 条）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen2.5-7B-Instruct --tuner_type lora \
       --dataset 'AI-ModelScope/alpaca-gpt4-data-zh#500' \
                 'AI-ModelScope/alpaca-gpt4-data-en#500' \
                 'swift/self-cognition#500' \
       --target_modules all-linear --output_dir output \
       --dataset_num_proc 4
   ```

   若显存不足，可换更小模型（如 `Qwen/Qwen3-4B`）并减小 `--max_length`。

2. 训练过程中，在终端日志里按出现顺序标记以下关键行，它们正好对应时序图的节点：
   - `Global seed set to ...`（基类 `_set_seed`）
   - `model_info: ...`（`_prepare_model_tokenizer`）
   - `args: ...` 后紧跟 `model.generation_config: ...`（构造期收尾）
   - `train_dataset: ...` / `Dataset Token Length: ...`（`_prepare_dataset` → `_show_dataset`，编码已完成或已包装）
   - `model_parameter_info: ...`（`prepare_model` 之后，能看到可训练参数量大幅减少——LoRA 生效）
   - `The logging file will be saved in: .../logging.jsonl`（`train()` 开始）
   - 训练进度条 `{'loss': ...}`（`trainer.train()` 循环）

3. 基于日志顺序，画出 4.4.2 节那段时序图的「真实观测版」，标出每个日志行对应的源码行号。

**需要观察的现象**：`Dataset Token Length` 这条日志出现在 `model_parameter_info` 之前——证明**数据集编码（至少是 lengths 统计与 LazyLLM 包装）发生在 `prepare_model` 与 Trainer 构造之前**。同时注意 `model_parameter_info` 显示的可训练参数占总参数的极小比例（通常 <1%），印证 LoRA 只挂了增量。

**预期结果**：得到一张与 4.4.2 节一致的时序图，并能明确指出「编码发生在 `run()` 的第一步 `_prepare_dataset` 内部，具体在 `_post_process_datasets` 阶段；若开启 `lazy_tokenize` 则进一步延迟到训练取数时」。若本地不具备 GPU 无法实跑，可标注「待本地验证」并改为纯源码阅读：对照 [swift/pipelines/train/sft.py:159-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L159-L185) 手动排出顺序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `args.save_args()` 写 args.json 的位置在「数据集准备之后、prepare_model 之前」，而不是构造函数里？
**参考答案**：因为 `_prepare_dataset` 与 `_prepare_template` 等步骤可能会在 `__post_init__` 之外再次「校准」一些参数（例如 padding_free 与多模态的推断、problem_type 的回填）。等这些推断完成后再落盘，才能保证 args.json 反映的是真正生效的配置，推理/导出回载时才能精确复现。

**练习 2**：若把 `--packing` 打开，数据集会经历哪几层包装？
**参考答案**：先在 `_encode_dataset` 补 `lengths`，再在 `_post_process_datasets` 先包 `LazyLLMDataset`（延迟编码），最后包 `PackingDataset` 做装箱（u4-3 讲过 packing 会强制 padding_free，由 `_prepare_template` 的校验保证模板支持）。训练时 collator 再用 `packing_row` 把 batch 压平、position_ids 每条从 0 重置。

**练习 3**：`run()` 第 170 行为什么把 `train_dataset` 传给 `prepare_model`？
**参考答案**：少数 tuner（注释点名的 LoRA-GA）的初始化需要用真实数据跑前向、用 `template.data_collator` 组织 batch 来计算初始化方向，所以挂载增量这一步依赖数据集。通用 LoRA 不用，但统一传参让接口一致。

## 5. 综合实践

把本讲串起来，完成一次「全链路追踪 + 改造观察」：

1. **追踪**：以 `swift sft` 命令为起点，写出从 [swift/cli/sft.py:13-20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L13-L20) 的 `sft_main()` → `SwiftSft(args).main()` → 构造期三连 → `run()` 四步 → `trainer.train()` 的完整调用栈，标注每一步对应的源码文件与行号。

2. **观察**：跑通 4.4.4 的 LoRA SFT 示例，从日志里定位「数据集编码完成」与「模型适配完成」两个时间点，确认前者先于后者。

3. **改造**（源码阅读型，**不修改源码**，仅用参数复现）：用同一份命令分别尝试三组配置并对比日志里的 `Dataset Token Length` 统计与可训练参数量：
   - 基线：`--tuner_type lora`（默认不 packing）。
   - 开 packing：加 `--packing true --packing_length 4096`，观察样本数是否减少、平均长度是否接近 4096。
   - 全量微调：`--tuner_type full`，观察 `model_parameter_info` 里可训练参数是否变成 100%。

   对比后用自己的话解释：packing 改变的是「数据效率」（更多有效 token/批），而 `tuner_type` 改变的是「参数效率」（训多少参数），两者是正交的两个维度（呼应 u1-l1 的训练三维度）。

> 说明：若本地无 GPU，第 2、3 步可标注「待本地验证」，重点完成第 1 步的纯源码追踪与时序图绘制。

## 6. 本讲小结

- `SwiftPipeline` 是所有管道的抽象基类，用**模板方法模式**把「解析参数 + 设种子 + 计时」固定在 `main()` 骨架里，把业务逻辑声明为抽象 `run()` 交子类实现。
- `SwiftSft` 通过多继承 `SwiftPipeline + TunerMixin` 同时获得「骨架 + 模型适配能力」，构造函数里完成「加载模型 → 实例化训练模板 → 按需启用 flash ckpt」三连准备，顺序受数据依赖严格约束。
- 数据集编码分两层：`_encode_dataset` 默认只补 `lengths`，真正的 `input_ids` 编码在 `_post_process_datasets` 里（或被 `LazyLLMDataset` 延迟到训练取数时），**编码发生在 `run()` 第一步 `_prepare_dataset` 内部，早于 `prepare_model` 与 Trainer 构造**。
- `run()` 四步编排为：准备数据集 → `save_args` 落盘 → `prepare_model` 适配（冻结基座 + 挂增量）→ `TrainerFactory` 装配 Trainer → `train()` 启动。`prepare_model` 之所以在数据集之后，是因为 LoRA-GA 等少数 tuner 的初始化依赖真实数据。
- `@RayHelper.worker`/`@RayHelper.function` 实现单机/分布式双模透明：`ray_inited()` 为 False 时零成本直通，单卡用户完全无感；启用 Ray 后则把类变 actor、方法调用变远端分发。
- 训练收尾在 `finally` 里 `_save_trainer_state`，把 checkpoint 路径、loss 曲线、显存峰值等写入 `output_dir/logging.jsonl`，与训练期落盘的 `args.json` 共同支撑「训练即所见，推理即所得」。

## 7. 下一步学习建议

- **进入推理引擎**：本讲到 `trainer.train()` 为止，模型产物如何被加载推理？下一单元 u6「推理引擎」从 `BaseInferEngine` 抽象讲起（u6-l1），它会回载本讲落盘的 `args.json`，复用同一套 template。
- **横向对比 RLHF 管道**：u7-l1 的 `SwiftRLHF` **继承自 `SwiftSft`**，复用本讲的 `_prepare_model_tokenizer`/`_prepare_template`/`_get_dataset`，只覆盖 `_prepare_dataset`（GRPO/GKD 的 `pre_process=False` 分支就是为它留的）。读 `SwiftRLHF` 能加深对本讲「为什么这里要留口子」的理解。
- **纵向深入 Trainer**：本讲把 Trainer 当黑盒（只调 `trainer.train()`）。若想了解前向/反向/优化器/梯度累积的细节，建议阅读 `swift/trainers/seq2seq_trainer.py` 与 `swift/trainers/mixin.py`（u5-l1 已铺垫）。
- **扩展到分布式**：本讲的 RayHelper 是分布式「调度层」，而真正的多卡数据并行靠 torchrun + DDP/DeepSpeed。建议接着学 u9-l1「分布式训练基础」，把「单卡 SwiftSft」放到多卡语境下重新理解一遍。
