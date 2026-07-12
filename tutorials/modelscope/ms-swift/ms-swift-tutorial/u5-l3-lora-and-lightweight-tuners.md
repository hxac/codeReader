# LoRA 与轻量微调方法

## 1. 本讲目标

本讲深入 `swift/tuners/` 目录，讲清「轻量微调方法本身是怎么实现的、怎么注册的、怎么和 peft 库协作的」。学完后你应该能够：

- 说清 `SWIFT_MAPPING` 注册表的「(Config, Tuner) 配对」范式，并能据此读懂任意一个新 tuner。
- 用数学语言解释 LoRA 的低秩分解原理，并讲清 `lora_rank`/`lora_alpha`/`target_modules` 三个核心参数各自的含义与作用。
- 说出 ms-swift 的两个 LoRA 配置类（`LoraConfig` 与 `LoRAConfig`）分别对应 peft 后端与 swift 后端，并能解释 swift 格式与 peft 格式 checkpoint 互转的流程。
- 上手实践：用 `lora` 与 `llamapro` 两种 tuner 跑同一份数据，对比可训练参数量与显存。

## 2. 前置知识

本讲默认你已学完 **u5-l2（TunerPlugin 与模型适配）**，已经知道：

- 训练 pipeline 通过 `swift/pipelines/train/tuner.py` 里的 `TunerMixin.prepare_model` 调用 tuner；
- `tuner_plugin` 是「外层胶水」：`tuners_map` 只收录 `ia3`/`lora_llm`/`dummy` 三个需要自定义序列化的 tuner，其余 tuner（含 `lora`/`llamapro`）都走 `prepare_adapter` → `Swift.prepare_model`；
- `--target_modules all-linear` 会被 `find_all_linears` 或 `get_multimodal_target_regex` 展开成真实模块名。

如果上面这些术语你还陌生，建议先回到 u5-l2。本讲的视角从「pipeline 怎么调 tuner」下沉到「tuner 自己内部长什么样」。

补充几个本讲会用到的名词：

- **全量微调（full）**：更新模型所有参数，效果好但显存大。
- **参数高效微调（PEFT, Parameter-Efficient Fine-Tuning）**：冻结原模型绝大部分参数，只额外引入很少的可训练参数（称为「增量」或「adapter」）来学习新任务，显存占用大幅下降。
- **LoRA**：PEFT 家族里最流行的一种，用「低秩矩阵分解」来构造增量。
- **peft**：HuggingFace 的官方 PEFT 库，ms-swift 在其基础上做了封装与增强。

## 3. 本讲源码地图

本讲涉及的源码集中在 `swift/tuners/`，它是轻量微调方法的「实现层」，与 u5-l2 讲的 `swift/tuner_plugin/`「集成层」是上下游关系。

| 文件 | 作用 |
| --- | --- |
| `swift/tuners/mapping.py` | 定义 `SwiftTuners` 枚举与 `SWIFT_MAPPING` 注册表，把每个 tuner 的「配置类」和「实现类」配成一对。 |
| `swift/tuners/utils.py` | 定义 `SwiftConfig`（配置基类）、`SwiftOutput`（tuner 的统一返回）、`SwiftAdapter`（tuner 实现基类），以及 `swift_to_peft_format` 格式转换入口。 |
| `swift/tuners/base.py` | 定义 `SwiftModel`（swift 后端的模型包装器）与 `Swift`（对外门面：`prepare_model`/`save_to_peft_format`/`from_pretrained`）。 |
| `swift/tuners/lora.py` | swift 后端的 LoRA：`LoRAConfig` 与 `LoRA` 实现（挂载、回调、合并）。 |
| `swift/tuners/peft.py` | peft 后端的 LoRA 配置 `LoraConfig`，以及对 peft 库的「热补丁」与 ModelScope 下载封装。 |
| `swift/tuners/llamapro.py` | LLaMAPro：复制若干新 transformer block 插回模型，只训练新 block。 |
| `swift/tuners/adapter.py` | 经典 Adapter：在指定模块前插入一个瓶颈 MLP。 |
| `swift/tuners/lora_layers.py` | LoRA 的层实现（`LoraModel`/`LoraLayer`）与辅助函数 `mark_lora_as_trainable`/`lora_state_dict`。 |
| `swift/pipelines/train/tuner.py` | `prepare_adapter`：把命令行参数拼成 config，再交给 tuner，是「参数 → 配置」的翻译器。 |
| `swift/arguments/tuner_args.py` | `TunerArguments`：定义 `lora_rank`/`lora_alpha`/`target_modules` 等 CLI 参数。 |

## 4. 核心概念与源码讲解

### 4.1 SWIFT_MAPPING 注册表：轻量微调的「配置+实现」配对范式

#### 4.1.1 概念说明

ms-swift 内置了十多种轻量微调方法（LoRA、LLaMAPro、Adapter、Side、ReFT、NEFTune……）。这些方法原理各异，但都遵循同一个「契约」：

- 一个 **配置类（Config）**：描述「这次训练用哪种方法、参数取值如何」，本质是一个 dataclass。
- 一个 **实现类（Tuner）**：描述「拿到配置后，怎么在模型上动手脚」，核心是一个静态方法 `prepare_model(model, config, adapter_name)`，返回一个 `SwiftOutput`。

`SWIFT_MAPPING` 就是把这两种类**一一配对**登记的注册表，key 是方法名字符串（如 `'LORA'`），value 是一个 `(Config类, Tuner类)` 元组。这是 ms-swift 全项目「基类 + 注册表」三件套范式（见 u1-l3）在 tuner 这里的具体落地。

要理解这套配对，先认识三个基类：

- `SwiftConfig`：所有 swift 原生 tuner 配置的基类，关键字段是 `swift_type`（声明自己属于哪种方法）。
- `SwiftAdapter`：所有 swift 原生 tuner 实现的基类，规定子类要实现 `prepare_model`/`activate_adapter` 两个静态方法。
- `SwiftOutput`：`prepare_model` 的统一返回值，里面装着 config 和一组「回调函数」（保存哪些权重、标记哪些参数可训练、如何分组给优化器）。

#### 4.1.2 核心流程

当 pipeline 决定使用某个 swift tuner（比如 `use_swift_lora=True` 的 LoRA）时，执行流程是：

```text
prepare_adapter(tuner.py)
   └─> 组装 LoRAConfig(swift_type='LORA', r=..., ...)
   └─> Swift.prepare_model(model, config)            # base.py 门面
         └─> 因 config 是 SwiftConfig ─> SwiftModel(model, config)
               └─> SwiftModel._prepare_model(model, config, 'default')
                     └─> adapter_cls = SWIFT_MAPPING[config.swift_type][1]   # 查表得实现类
                     └─> adapter_cls.prepare_model(model, config, 'default')  # 真正挂载
                           └─> 返回 SwiftOutput(config, callbacks...)
```

关键点：`SwiftModel._prepare_model` 通过 `config.swift_type` 在 `SWIFT_MAPPING` 里查到对应的实现类，再调用其 `prepare_model`。这就是「配置驱动、注册表派发」的核心。学会了这一点，读任何一个新 tuner 都是同一套路。

#### 4.1.3 源码精读

先看注册表本身。[swift/tuners/mapping.py:16-27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/mapping.py#L16-L27) 用一个 `SwiftTuners` 类把所有方法名收成字符串常量（如 `LORA = 'LORA'`、`LLAMAPRO = 'LLAMAPRO'`），起「枚举」作用，避免到处写字符串字面量。

真正的注册表在 [swift/tuners/mapping.py:30-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/mapping.py#L30-L42)：

```python
SWIFT_MAPPING = {
    SwiftTuners.ADAPTER: (AdapterConfig, Adapter),
    SwiftTuners.LORA:    (LoRAConfig, LoRA),
    SwiftTuners.LLAMAPRO:(LLaMAProConfig, LLaMAPro),
    # ... 其余方法同理
}
```

每个 value 都是 `(配置类, 实现类)` 元组。`[0]` 取配置类，`[1]` 取实现类。

再看三个基类。配置基类 `SwiftConfig` 的核心是 `swift_type` 字段和「按 swift_type 反查重建 config」的 `from_pretrained`（[swift/tuners/utils.py:29-94](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L29-L94)）。注意 [swift/tuners/utils.py:86-88](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L86-L88)：加载时会断言 `swift_type` 必须在 `SWIFT_MAPPING` 里，再据此实例化正确的 Config 子类——这正是注册表「双向」发挥作用的地方。

实现基类 `SwiftAdapter` 规定契约（[swift/tuners/utils.py:278-284](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L278-L284)）：`prepare_model` 默认 `raise NotImplementedError`，强制子类实现。

统一返回值 `SwiftOutput`（[swift/tuners/utils.py:111-145](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L111-L145)）里最关键的是三个回调：

- `state_dict_callback`：从整模型 state_dict 里**挑出属于本 adapter 的权重**（保存时用）。
- `mark_trainable_callback`：**标记本 adapter 的参数为可训练**。
- `optimizer_group_callback`：把本 adapter 的参数**按需分成多组**给优化器（如 LoRA+ 给 lora_B 更高学习率）。

最后看派发核心。[swift/tuners/base.py:380-396](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L380-L396) 的 `SwiftModel._prepare_model` 做两件事：先按需**冻结全模型**（`requires_grad = False`，并打 `model_frozen` 标记防止重复冻结），再查表调用实现类：

```python
adapter_cls = SWIFT_MAPPING[config.swift_type][1]
if adapter_cls.has_additional_modules() and not getattr(model, 'model_frozen', False):
    for _, p in model.named_parameters():
        p.requires_grad = False
    model.model_frozen = True
config.has_additional_modules = adapter_cls.has_additional_modules()
return adapter_cls.prepare_model(model, config, adapter_name)
```

这段就是 u5-l2 讲过的「adapter 微调省显存的根基——先全冻再只放开增量」在实现层的真正落点。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「配置驱动、注册表派发」这条链路确实成立。
2. **步骤**：
   - 打开 [swift/tuners/mapping.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/mapping.py)，数一下 `SWIFT_MAPPING` 里共有多少种方法。
   - 打开 [swift/tuners/base.py:380-396](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L380-L396)，确认 `config.swift_type` 如何被用作查表 key。
   - 任意挑一个 tuner（如 `llamapro.py`），看它的 Config 类 `__post_init__` 是否确实把 `self.swift_type` 设成了 `SwiftTuners.LLAMAPRO`（见 [swift/tuners/llamapro.py:38-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L38-L40)）。
3. **观察现象**：每个 Config 子类都在 `__post_init__` 里给自己盖一个 `swift_type` 戳，这个戳就是注册表的 key。
4. **预期结果**：能画出 `config.swift_type ─→ SWIFT_MAPPING[type][1] ─→ Tuner.prepare_model` 的派发箭头图。

#### 4.1.5 小练习与答案

**练习 1**：如果想新增一种叫 `mytuner` 的轻量微调方法，需要在哪些地方动手？

**答案**：① 写 `MyConfig(SwiftConfig)`，在 `__post_init__` 里设 `self.swift_type = SwiftTuners.MYTUNER`；② 写 `MyTuner(SwiftAdapter)`，实现 `prepare_model` 返回 `SwiftOutput`；③ 在 `SwiftTuners` 加常量 `MYTUNER = 'mytuner'`，在 `SWIFT_MAPPING` 加一行 `SwiftTuners.MYTUNER: (MyConfig, MyTuner)`；④ 在 `prepare_adapter` 加一个 `elif args.tuner_type == 'mytuner':` 分支拼装 config。

**练习 2**：为什么 `SwiftConfig.from_pretrained` 要先断言 `swift_type in SWIFT_MAPPING`（[swift/tuners/utils.py:86-88](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L86-L88)）？

**答案**：因为加载时只读到了一个 JSON，必须靠 `swift_type` 反查 `SWIFT_MAPPING` 才能知道该用哪个 Config 子类来实例化。断言是为了在 checkpoint 的 `swift_type` 不合法时尽早报错，而不是等到后面某个字段对不上才崩。

---

### 4.2 LoRA 原理与 LoRAConfig 配置

#### 4.2.1 概念说明

LoRA（Low-Rank Adaptation）的核心想法是：微调时模型权重的变化量 ΔW 是「低秩」的，没必要更新整个大矩阵 W，只需学习一个小的低秩增量。

假设原线性层权重 \(W \in \mathbb{R}^{d \times k}\)，前向为 \(y = Wx\)。LoRA 把权重更新写成：

\[
W' = W + \Delta W, \qquad \Delta W = \frac{\alpha}{r} B A
\]

其中 \(A \in \mathbb{R}^{r \times k}\)、\(B \in \mathbb{R}^{d \times r}\)，秩 \(r \ll \min(d, k)\)。

- **参数量**：全量更新要 \(dk\) 个参数；LoRA 只额外引入 \(r(d+k)\) 个参数（A 和 B），当 \(r\) 很小时远小于 \(dk\)。
- **初始化**：A 用高斯随机初始化、B 初始化为零，于是训练开始时 \(\Delta W = BA = 0\)，模型输出与原模型**完全一致**，训练从一个好的起点出发。
- **缩放系数** \(\alpha/r\)：`lora_alpha`（\(\alpha\)）除以 `lora_rank`（\(r\)）。`alpha` 决定增量相对于原权重的「学习强度」，调大相当于放大 LoRA 分支的学习率；改变 `r` 时通常同步调 `alpha` 以保持缩放稳定。
- **目标模块**：不必对所有层都加 LoRA。`target_modules` 指定在哪些子模块（一般是注意力与 MLP 的线性层）上挂载 LoRA。`all-linear` 是一个特殊快捷值，表示「所有线性层」。

#### 4.2.2 核心流程

LoRA 的「挂载」流程（swift 后端，`LoRA.prepare_model`）：

```text
LoRA.prepare_model(model, config, adapter_name)
  └─> LoraModel(model, config, adapter_name)        # 遍历模型，把目标 Linear 替换成 LoraLayer
  └─> 注册三个回调：
        state_dict_callback     ─> lora_state_dict  (只挑 'lora_' 开头的权重)
        mark_trainable_callback ─> mark_lora_as_trainable (按 bias 策略放开)
        optimizer_group_callback─> LoRA+ 分组（可选）
  └─> return SwiftOutput(config, 三个回调)
```

`LoraLayer` 在原线性层旁边并联一条 \(B A\) 分支：前向时 \(y = Wx + \frac{\alpha}{r}B(Ax)\)。原 W 被冻结（不更新、不占优化器状态），只有 A、B 参与训练，这就是 LoRA 省显存的本质。

合并（merge）时（推理或 `export --merge_lora`）：把 \(BA\) 烘焙回 W，得到 \(W' = W + \frac{\alpha}{r}BA\)，之后不再需要 A、B，模型结构与原模型无异。

#### 4.2.3 源码精读

CLI 参数定义在 `TunerArguments`（[swift/arguments/tuner_args.py:126-140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/tuner_args.py#L126-L140)）：

```python
target_modules: List[str] = field(default_factory=lambda: ['all-linear'])
...
lora_rank: int = 8
lora_alpha: int = 32
lora_dropout: float = 0.05
lora_bias: Literal['none', 'all'] = 'none'
```

注意默认 `target_modules=['all-linear']`、`lora_rank=8`、`lora_alpha=32`，与官方示例脚本一致（见第 5 节）。

这些 CLI 参数在 `prepare_adapter` 里被翻译成 config（[swift/pipelines/train/tuner.py:152-168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L152-L168)）：

```python
lora_kwargs = {
    'r': args.lora_rank,
    'target_modules': target_modules,
    'lora_alpha': args.lora_alpha,
    'lora_dropout': args.lora_dropout,
    'bias': args.lora_bias,
    'modules_to_save': modules_to_save,
    'use_rslora': args.use_rslora,
    'use_dora': args.use_dora,
    'lorap_lr_ratio': args.lorap_lr_ratio,
    'init_lora_weights': args.init_weights,
}
if args.tuner_type in ('lora', 'longlora'):
    if args.use_swift_lora:
        lora_config = LoRAConfig(lora_dtype=args.lora_dtype, **lora_kwargs)
        model = Swift.prepare_model(model, lora_config)
```

注意字段名的映射：CLI 的 `lora_rank` → config 的 `r`，`lora_alpha` → `lora_alpha`，`target_modules` 先经 `get_target_modules` 展开再传入。

`get_target_modules`（[swift/pipelines/train/tuner.py:91-110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L91-L110)）负责把快捷值 `all-linear` 翻译成真实模块名：文本模型用 `find_all_linears(model)` 扫出所有 `nn.Linear`，多模态模型用 `get_multimodal_target_regex` 按 `freeze_llm/freeze_vit/freeze_aligner` 决定给哪个子模型挂 LoRA。

swift 后端的 `LoRAConfig` 与 `LoRA` 实现在 [swift/tuners/lora.py:17-72](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L17-L72)。`LoRAConfig` 在 `__post_init__` 给自己盖 `swift_type = SwiftTuners.LORA` 戳（[swift/tuners/lora.py:47-50](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L47-L50)），从而能被 `SWIFT_MAPPING` 派发。

`LoRA.prepare_model`（[swift/tuners/lora.py:76-156](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L76-L156)）核心就一句 `LoraModel(model, config, adapter_name)` 完成挂载，随后装配三个回调。其中保存回调指向 `lora_state_dict`——只保留名字里含 `lora_` 的权重（[swift/tuners/lora_layers.py:658-673](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora_layers.py#L658-L673)），这就是 LoRA checkpoint 体积很小的原因。

LoRA+ 的优化器分组回调（[swift/tuners/lora.py:91-150](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L91-L150)）把 `lora_B` 的参数单独分一组、乘上 `lorap_lr_ratio` 给更高学习率，这是 LoRA+ 论文「A、B 用不同学习率收敛更快」的实现。

合并逻辑在 `LoRA.unpatch_lora`（[swift/tuners/lora.py:167-192](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L167-L192)），最终调用 `LoraModel(...).merge_and_unload()` 把 \(BA\) 烘焙回 W。

#### 4.2.4 代码实践（参数映射追踪）

1. **目标**：确认 CLI 的 `--lora_rank 8 --lora_alpha 32 --target_modules all-linear` 真正生效到 config。
2. **步骤**：
   - 在 [swift/pipelines/train/tuner.py:152-168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L152-L168) 找到 `lora_kwargs`，记下 `lora_rank → r`、`lora_alpha → lora_alpha` 的映射。
   - 跟到 `get_target_modules`（同文件 L91-110），看清 `all-linear` 是怎么被替换成具体模块名列表的。
3. **观察现象**：日志里会打印 `lora_config: LoRAConfig(r=8, lora_alpha=32, target_modules=[...一堆真实模块名...])`。
4. **预期结果**：能用一句话说清「`--lora_rank` 越大 → 秩 r 越大 → 可训练参数越多但表达力越强」。

#### 4.2.5 小练习与答案

**练习 1**：把 `lora_rank` 从 8 调到 32，可训练参数量大约变为原来的几倍？

**答案**：约 \(\frac{32}{8} = 4\) 倍。因为每个目标层的 LoRA 参数量是 \(r(d+k)\)，与 \(r\) 成正比（忽略 bias）。

**练习 2**：为什么 LoRA 要把 B 初始化为 0、A 初始化为非零？

**答案**：这样初始 \(\Delta W = BA = 0\)，模型在训练第 0 步的输出与原模型完全一致，等价于从已预训练好的原模型「原地」开始学习增量，避免随机增量破坏预训练知识。

**练习 3**：`lora_alpha=32, lora_rank=8` 时，LoRA 增量的实际缩放系数是多少？

**答案**：\(\alpha / r = 32 / 8 = 4\)（若开启 `use_rslora` 则为 \(\alpha / \sqrt{r}\)）。

---

### 4.3 peft 集成与 swift/peft 双格式

#### 4.3.1 概念说明

ms-swift 的 LoRA 有两条实现路径，对应两个「后端」：

- **peft 后端**（默认，`--tuner_backend peft`）：直接用 HuggingFace `peft` 库的 `LoraModel`/`LoraLayer`，配置类是 ms-swift 在 `swift/tuners/peft.py` 里定义的 `LoraConfig`（继承自 `peft.LoraConfig`）。模型最终被包成 `PeftModel`。
- **swift 后端**（`--use_swift_lora`，兼容参数）：用 ms-swift 自己的 `SwiftModel` 包装器，配置类是 `swift/tuners/lora.py` 的 `LoRAConfig`（同时继承上面的 `LoraConfig` 与 `SwiftConfig`）。

为什么有两套？peft 后端生态兼容性好（checkpoint 与原生 peft 互通），是默认；swift 后端支持一些 peft 暂时没有的能力（如多 adapter 同时激活、更灵活的显存 offload），是 ms-swift 的增强。

二者产出的 checkpoint **格式不同**：

- **peft 格式**：peft 库原生格式，state_dict 的 key 带 `base_model.model.` 前缀，`lora_A`/`lora_B` 不带 adapter_name。
- **swift 格式**：ms-swift 自有格式，额外存一个 `swift_type` 字段标记类型，便于多 adapter 管理；多模态 lora_llm 还会额外产 `vit.safetensors`（见 u5-l2）。

两者可以通过 `swift_to_peft_format` 互转，方便与生态工具对接（如 vllm 直接加载 peft 格式 adapter）。

#### 4.3.2 核心流程

两个后端在 `prepare_adapter` 里的分叉（[swift/pipelines/train/tuner.py:164-218](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L164-L218)）：

```text
if args.use_swift_lora:        ─> LoRAConfig(swift) ─> Swift.prepare_model ─> SwiftModel
elif args.tuner_backend=='peft':─> LoraConfig(peft) ─> Swift.prepare_model ─> PeftModel
elif args.tuner_backend=='unsloth':─> UnslothModel.get_peft_model(...)
```

门面 `Swift.prepare_model`（[swift/tuners/base.py:702-720](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L702-L720)）根据 config 类型分流：

```python
if isinstance(config, (SwiftConfig, dict)):
    return SwiftModel(model, config, **kwargs)   # swift 后端
else:
    return get_peft_model(model, config, **kwargs)  # peft 后端
```

这是判定的关键：`LoRAConfig` 是 `SwiftConfig` 子类 → 走 `SwiftModel`；`LoraConfig`（peft.py）不是 `SwiftConfig` → 走 `get_peft_model`。

格式转换流程：

```text
swift checkpoint ──swift_to_peft_format──> peft checkpoint
                  (utils.py:423)
                  └─ 内部调用 Swift.save_to_peft_format (base.py:793-855)
```

#### 4.3.3 源码精读

peft 后端的配置类 [swift/tuners/peft.py:40-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L40-L83) 继承 `peft.LoraConfig`，额外加了 `lora_dtype`/`lorap_lr_ratio`/`lorap_emb_lr` 三个 swift 专属字段。它的 `to_peft_config`（[swift/tuners/peft.py:49-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L49-L54)）把这三个专属字段 pop 掉、转回纯 `peft.LoraConfig`；`save_pretrained`（[swift/tuners/peft.py:56-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L56-L64)）则把它们单独写进 `additional_config.json`，保证 swift 专属信息不丢失又能被标准 peft 读到。

swift 后端的 `LoRAConfig`（[swift/tuners/lora.py:17-72](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L17-L72)）多继承了 `LoraConfig`（peft.py 那个）和 `SwiftConfig`，所以它同时拥有 peft 字段和 `swift_type` 戳。它的 `can_be_saved_to_peft`（[swift/tuners/lora.py:52-56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L52-L56)）判断当前配置能否无损转成 peft 格式——用了 `use_qa_lora` 或 `use_merged_linear` 就不能转。

「热补丁」函数 `hot_patch_peft_module`（[swift/tuners/peft.py:323-383](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L323-L383)）在模块导入时（[swift/tuners/peft.py:416](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L416)）就地替换 peft 的若干方法，给 peft 打上 ms-swift 需要的增强，主要有：

- 给 `_create_and_replace` 套一层钩子，让 LoRA 支持 `NonDynamicallyQuantizableLinear`、并对 DeepSpeed ZeRO-3 做显存优化（[swift/tuners/peft.py:86-133](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L86-L133)）。
- 给 `LoraModel.__init__` 打补丁，支持 `lora_dtype` 转换（[swift/tuners/peft.py:345-363](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L345-L363)）。
- 把 `PeftModel.create_optimizer_param_groups` 换成支持 LoRA+ 的版本。

`wrap_module`（[swift/tuners/peft.py:386-413](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/peft.py#L386-L413)）则给 peft 的各类 `from_pretrained` 套一层壳：若传入的是 ModelScope hub id 而非本地路径，先 `snapshot_download` 拉到本地再交给 peft，使 peft 天然支持从魔搭下载。

格式互转入口 `swift_to_peft_format`（[swift/tuners/utils.py:423-431](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/utils.py#L423-L431)）：检查 checkpoint 目录下有没有 `default` 子目录来判断当前是不是 swift 格式，是的话调 `Swift.save_to_peft_format` 转换，否则原样返回。真正的 key 改写逻辑在 [swift/tuners/base.py:829-848](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L829-L848)：给 key 加上 `base_model.model.` 前缀、把 `lora_A.{adapter}.` 改回 `lora_A.`，并丢弃 swift 专属字段。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解 swift 与 peft 两种 checkpoint 的 key 差异。
2. **步骤**：
   - 读 `Swift.save_to_peft_format` 的 key 改写循环（[swift/tuners/base.py:829-843](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L829-L843)），列出它对 key 做了哪几种字符串替换。
   - 读 `SwiftModel.state_dict` 的 `peft_format=True` 分支（[swift/tuners/base.py:218-228](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L218-L228)），它做的是相反方向的替换。
3. **观察现象**：两段代码互为逆操作——swift→peft 加前缀、去 adapter_name；保存时若选 peft 格式则反向还原。
4. **预期结果**：能解释为什么 vllm 这类只认 peft 格式的工具，加载 swift 训出来的 LoRA 前需要先转换。

#### 4.3.5 小练习与答案

**练习 1**：默认情况下（`tuner_backend='peft'`），训练得到的是 swift 格式还是 peft 格式？

**答案**：peft 格式。因为默认走 `LoraConfig`（peft.py）→ `get_peft_model` → `PeftModel`，其 `save_pretrained` 直接产出标准 peft checkpoint。

**练习 2**：`LoRAConfig.can_be_saved_to_peft()` 什么时候返回 False？

**答案**：当 `use_qa_lora=True` 或 `use_merged_linear=True` 时返回 False（[swift/tuners/lora.py:52-56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora.py#L52-L56)），因为这两种配置 peft 库不支持，无法无损转换。

---

### 4.4 其他轻量微调器：LLaMAPro 与 Adapter

本节介绍两种与 LoRA 思路迥异的轻量微调器，为第 5 节的对比实践做铺垫。

#### 4.4.1 概念说明

- **LLaMAPro**：思路是「**加宽**模型」——在原有 transformer 层之间**插入若干全新的 block**，原 block 全部冻结，只训练新 block。优点是新 block 是完整 transformer 层，表达力比低秩矩阵强；缺点是参数量比 LoRA 大。它要求模型架构里有明确的 `o_proj`（注意力输出投影）和 `down_proj`（MLP 下投影），以便把新 block 的这两个投影**初始化为零**，保证插入后初始输出不变。
- **Adapter（Houlsby）**：思路是「**加瓶颈 MLP**」——在指定模块（一般是 MLP）的 forward 里串联一个小型瓶颈结构 \( \text{down} \to \text{act} \to \text{up} \)，只在瓶颈里训练。中间维度 `adapter_length`（默认 128）远小于隐藏维度，故参数很少。

#### 4.4.2 核心流程

LLaMAPro 挂载流程（[swift/tuners/llamapro.py:45-131](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L45-L131)）：

```text
LLaMAPro.prepare_model
  └─> 取 num_hidden_layers，断言能被 num_new_blocks 整除
  └─> 找到模型的 layer 列表 (model.layers)
  └─> 每隔 num_stride 层，deepcopy 一份当前层 ─> 新 block
  └─> 把新 block 的 o_proj/down_proj 权重清零（保证初始输出不变）
  └─> 把新 block 的所有子模块标记 plugin=True（只激活时才参与计算）
  └─> 用新列表替换 model.layers，更新 config.num_hidden_layers
  └─> mark_trainable_callback：只放开新 block 的参数
```

Adapter 挂载流程（[swift/tuners/adapter.py:65-119](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/adapter.py#L65-L119)）：

```text
Adapter.prepare_model
  └─> 遍历模块，匹配 target_modules（一般是 MLP）
  └─> 把该模块的 forward 方法替换成带 adapter 分支的新 forward（保留 forward_origin）
  └─> 给该模块挂一个 AdapterModule(线性↓-激活-线性↑) 实例
  └─> 推理时：out = x + AdapterModule(x)  （残差结构）
```

#### 4.4.3 源码精读

LLaMAPro 配置 [swift/tuners/llamapro.py:15-40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L15-L40) 关键字段是 `num_new_blocks`（插几个新层）和 `num_groups`（新层分几组，默认等于 `num_new_blocks`，即每隔 `num_hidden_layers/num_new_blocks` 层插一个）。

「复制 + 插入」的核心循环在 [swift/tuners/llamapro.py:82-95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L82-L95)：遍历原 layer 列表，每经过 `num_stride` 层就 `deepcopy` 一份当前层追加进新列表，并记下新层的索引。

「零初始化」保证插入不破坏原输出，见 [swift/tuners/llamapro.py:203-219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L203-L219)：对新 block 的 `o_proj` 和 `down_proj` 权重执行 `torch.zeros_like`。这与 LoRA 把 B 清零是同一思想——让增量初始为 0。

`mark_trainable_callback`（[swift/tuners/llamapro.py:120-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L120-L126)）只对新层索引的参数放开 `requires_grad`，原层保持冻结。

Adapter 的瓶颈 MLP 在 [swift/tuners/adapter.py:131-188](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/adapter.py#L131-L188)：`linear1(dim → adapter_length)` → 激活 → `linear2(adapter_length → dim)`，最后与输入做残差相加（`out = identity + out`，[swift/tuners/adapter.py:186-188](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/adapter.py#L186-L188)）。

两者在 `prepare_adapter` 的分支：LLaMAPro 见 [swift/pipelines/train/tuner.py:243-249](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L243-L249)，Adapter 见 [swift/pipelines/train/tuner.py:250-261](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L250-L261)。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解 LLaMAPro 为何依赖 `o_proj`/`down_proj`，Adapter 为何走「替换 forward」而非「替换模块」。
2. **步骤**：
   - 读 [swift/tuners/llamapro.py:167-173](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/llamapro.py#L167-L173)，看 `get_model_key_mapping` 的断言：模型架构必须有 `o_proj` 和 `down_proj`，否则 LLaMAPro 不可用。
   - 读 [swift/tuners/adapter.py:100-109](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/adapter.py#L100-L109)，看它如何用 `setattr(module, method_name, MethodType(_forward, module))` 把目标模块的 forward 动态替换掉，同时保留 `forward_origin_{adapter_name}`。
3. **观察现象**：LoRA/LLaMAPro 通过「改权重/改层列表」工作，Adapter 通过「猴子补丁方法」工作，是两种不同的干预粒度。
4. **预期结果**：能说清三种 tuner 各自「在模型上动手的位置」：LoRA 并联在 Linear 旁、LLaMAPro 插在层列表里、Adapter 串联在 forward 中。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LLaMAPro 要把新 block 的 `o_proj` 和 `down_proj` 清零，而不是清零整个新 block？

**答案**：新 block 是原 block 的完整副本，注意力输出和 MLP 输出都经过 `o_proj`/`down_proj`。只把这两个投影清零，就能让新 block 对外贡献为 0（初始不改变模型输出），同时新 block 内部的其它参数（Q/K/V、gate/up 投影）仍保留原 block 的良好初值，训练更稳。

**练习 2**：Adapter 的参数量主要由哪个参数决定？

**答案**：由 `adapter_length`（瓶颈中间维度，默认 128）决定。每个目标模块参数量约为 \(2 \times \text{dim} \times \text{adapter_length}\)，与 `adapter_length` 成正比。

---

## 5. 综合实践：lora 与 llamapro 对比微调

本实践把本讲主要内容串起来：用同一份小数据、同一个基座模型，分别跑 `lora` 与 `llamapro`，对比可训练参数量、显存占用，并验证你对 `lora_rank`/`lora_alpha`/`target_modules` 的理解。

### 5.1 实践目标

- 跑通两条 tuner 路径，观察日志里的 `trainable params` 与 `cuda memory`。
- 解释三种参数的作用，并能预测「调大 lora_rank」对参数量的影响。

### 5.2 操作步骤

以下命令基于官方示例 [examples/train/lora_sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh)（单卡、约 22GB 显存）。若显存不足，可把 `--model` 换成更小的 `Qwen/Qwen2.5-1.5B-Instruct`，并相应减小 `--max_length`。

**第一步：LoRA 微调**

```bash
CUDA_VISIBLE_DEVICES=0 swift sft \
    --model Qwen/Qwen2.5-7B-Instruct \
    --tuner_type lora \
    --dataset 'AI-ModelScope/alpaca-gpt4-data-en#200' \
    --torch_dtype bfloat16 \
    --num_train_epochs 1 \
    --per_device_train_batch_size 1 \
    --learning_rate 1e-4 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --target_modules all-linear \
    --max_length 1024 \
    --output_dir output-lora
```

**第二步：LLaMAPro 微调**（同基座、同数据，只改 tuner 与其专属参数）

```bash
CUDA_VISIBLE_DEVICES=0 swift sft \
    --model Qwen/Qwen2.5-7B-Instruct \
    --tuner_type llamapro \
    --dataset 'AI-ModelScope/alpaca-gpt4-data-en#200' \
    --torch_dtype bfloat16 \
    --num_train_epochs 1 \
    --per_device_train_batch_size 1 \
    --learning_rate 3e-4 \
    --llamapro_num_new_blocks 8 \
    --max_length 1024 \
    --output_dir output-llamapro
```

> 说明：LLaMAPro 没有 `lora_rank`/`lora_alpha`/`target_modules`，它用 `--llamapro_num_new_blocks` 控制插入多少新层（参考 [swift/pipelines/train/tuner.py:243-249](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L243-L249)）。`num_new_blocks` 必须能整除模型的 `num_hidden_layers`（Qwen2.5-7B 有 28 层，故 8 不行需改成 4 或 7；执行前先查模型层数，本例以「能整除」为准，**待本地验证**具体取值）。

**第三步：读取参数与显存**

训练启动后，日志会打印一行类似：

```
trainable params: 19,xxx,xxx || all params: 7,xxx,xxx,xxx || trainable%: 0.xx || cuda memory: x.xx GiB.
```

这行由 `SwiftModel.get_trainable_parameters` 生成（[swift/tuners/base.py:677-696](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L677-L696)）。把两次的 `trainable params` 和 `cuda memory` 分别记下来。

### 5.3 需要观察的现象

- LoRA 的 `trainable%` 通常在 1% 以下（仅 A、B 矩阵可训练）。
- LLaMAPro 的 `trainable params` 约等于 `num_new_blocks × 单层参数量`，明显大于 LoRA。
- 两者显存都远低于全量微调（因为优化器状态只覆盖可训练参数）。

### 5.4 预期结果与分析

| 维度 | LoRA | LLaMAPro |
| --- | --- | --- |
| 可训练参数 | 很少（与 `lora_rank`、目标层数成正比） | 较多（与 `num_new_blocks`、单层规模成正比） |
| 关键参数 | `lora_rank`/`lora_alpha`/`target_modules` | `llamapro_num_new_blocks`/`llamapro_num_groups` |
| 干预方式 | 在线性层旁并联低秩分支 | 在层列表里插入新 transformer 层 |
| checkpoint 体积 | 很小（只有 `lora_` 权重） | 较大（整层权重） |

参数解释（验证你的理解）：

- **`lora_rank`（r）**：低秩分解的秩。越大 → 增量表达力越强、可训练参数越多（约线性增长）。
- **`lora_alpha`（α）**：增量缩放系数分子，实际缩放为 α/r。调大相当于放大 LoRA 分支学习强度。
- **`target_modules`**：决定在哪些层挂 LoRA。`all-linear` 展开为所有线性层（[swift/pipelines/train/tuner.py:91-110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/tuner.py#L91-L110)）；缩小范围（如只挂 `q_proj`/`v_proj`）可进一步减参数。

> **待本地验证**：上述命令的实际显存与参数量数值依硬件、依赖版本而异，请以本地日志为准。若 `llamapro` 因 `num_new_blocks` 不能整除层数报错，请按报错提示调整为能整除的值。

## 6. 本讲小结

- `SWIFT_MAPPING` 是「(Config, Tuner) 配对」注册表，`config.swift_type` 是查表 key；`SwiftModel._prepare_model` 据此派发到具体实现类。
- 三个基类 `SwiftConfig`/`SwiftAdapter`/`SwiftOutput` 规定了统一契约：Config 带 `swift_type` 戳、Tuner 实现 `prepare_model`、用 `SwiftOutput` 回传 config 与三个回调（保存/标记可训练/优化器分组）。
- LoRA 用低秩分解 \(\Delta W = \frac{\alpha}{r}BA\) 构造增量；B 清零保证初始输出不变；`lora_rank`/`lora_alpha`/`target_modules` 是三个核心 CLI 参数，在 `prepare_adapter` 里翻译成 config。
- ms-swift 有两个 LoRA 配置类：`LoraConfig`（peft.py，peft 后端默认）与 `LoRAConfig`（lora.py，swift 后端）；`Swift.prepare_model` 按 config 是否为 `SwiftConfig` 分流到 `PeftModel` 或 `SwiftModel`。
- swift 与 peft 两种 checkpoint 格式可经 `swift_to_peft_format` 互转；`hot_patch_peft_module` 在导入时给 peft 打增强补丁，`wrap_module` 让 peft 支持从 ModelScope 下载。
- LLaMAPro 靠「插入新层 + 零初始化 o_proj/down_proj」工作，Adapter 靠「猴子补丁 forward + 瓶颈 MLP」工作，二者与 LoRA 代表三种不同的干预粒度。

## 7. 下一步学习建议

- 精读 [swift/tuners/lora_layers.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/lora_layers.py) 的 `LoraLayer`/`LoraModel`，理解 LoRA 前向「并联分支」与多 adapter 共享的实现细节。
- 阅读 u8-l1（模型导出与量化），看 `Swift.merge_and_unload`/`LoRA.unpatch_lora` 如何在 export 阶段把 LoRA 增量烘焙回基座。
- 尝试在 u10-l3（自定义模型、模板与 Agent 注册）的思路上，仿照 4.1.5 练习 1 新增一个最小自定义 tuner，跑通「注册 → 派发 → 训练」全链路。
- 若对 LoRA 变体感兴趣，可顺带阅读 `swift/tuners/` 下的 `longlora`、`neftune`、`side`、`reft` 等，它们都遵循本讲讲的同一套注册范式。
