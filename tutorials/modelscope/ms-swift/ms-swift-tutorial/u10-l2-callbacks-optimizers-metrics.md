# Callbacks、Optimizers 与 Metrics 扩展

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 ms-swift 中「训练过程回调」「优化器」「评测指标」三类扩展点各自的基类、注册表与 CLI 开关，并能复用 u1-l3 提出的「基类 + mapping + CLI 开关」三件套范式来看懂它们。
- 跟踪一条从命令行参数（`--callbacks`/`--optimizer`/`--eval_metric`）到 Trainer 内部装配的完整链路，理解它们分别挂在 `SwiftMixin` 的哪个钩子上。
- 区分两条指标通路：opt-in 的 HF 验证指标（`eval_metrics_map`）与始终在线的训练日志指标（`custom_metrics` + `MeanMetric`）。
- 自己实现并注册一个自定义 Callback、自定义 Optimizer、自定义 Metric，并用 `--external_plugins` 在不改动源码的前提下加载它们。

## 2. 前置知识

本讲是「扩展机制与二次开发」单元的第二篇，承接 u5-l1（TrainerFactory 与训练器体系）。请先确认你已经掌握：

- **三件套范式**（u1-l3）：ms-swift 的每个扩展点几乎都是「`base.py` 抽象基类 + `mapping.py` 的 `*_map` 注册表 + 一个 CLI 参数开关」的组合。学会一个，就会全部。
- **SwiftMixin 与 patcher**（u5-l1）：所有 Trainer 共享 `SwiftMixin`（夹在 HF Trainer 之上的混入层），`patcher.py` 在包导入时以猴子补丁替换 HF 默认回调。本讲的三类扩展都挂在 `SwiftMixin` 上。
- **dataclass 参数体系**（u2-l1）：`TrainingArguments` 是多继承拼装的参数对象，本讲涉及的 `optimizer`/`eval_metric`/`callbacks` 等字段都定义在 `swift/trainers/arguments.py` 的 `TrainArgumentsMixin` 里。

补充一个对 Callback 很关键的背景：ms-swift 的 `TrainerCallback` 直接继承自 transformers 的 `TrainerCallback`，因此它的钩子方法名（`on_train_begin`/`on_step_end`/`on_save`/`on_log` 等）与 HF 完全一致——你在 HF 文档里学到的回调写法在这里原样可用，ms-swift 只是在构造时多注入了 `args` 和 `trainer` 两个参数。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `swift/callbacks/base.py` | `TrainerCallback` 基类，仅持有 `args`/`trainer`，接口与 HF 一致 |
| `swift/callbacks/mapping.py` | `callbacks_map` 注册表，把字符串名映射到回调类 |
| `swift/callbacks/early_stop.py` | `EarlyStopCallback`：早停回调范例 |
| `swift/callbacks/perf_log.py` | `PerfMetricsLogCallback`：MFU 等性能指标日志范例 |
| `swift/callbacks/lisa.py` | `LISACallback`：随机激活若干层训练的范例 |
| `swift/optimizers/base.py` | `OptimizerCallback` 基类，定义「建优化器 + 建调度器」契约 |
| `swift/optimizers/mapping.py` | `optimizers_map` 注册表 |
| `swift/optimizers/muon.py` / `lorap.py` / `multimodal.py` / `galore/utils.py` | 四个自定义优化器范例 |
| `swift/metrics/base.py` | `EvalMetrics` 基类，定义 `compute_metrics` 与 `preprocess_logits_for_metrics` |
| `swift/metrics/mapping.py` | `eval_metrics_map` 注册表 |
| `swift/metrics/acc.py` / `utils.py` | acc 指标与 `MeanMetric` 累加器 |
| `swift/trainers/arguments.py` | `TrainArgumentsMixin`：`optimizer`/`eval_metric`/`callbacks` 字段与 `_init_callbacks` 自动注入 |
| `swift/trainers/mixin.py` | `SwiftMixin`：三类扩展的装配点（`_get_callbacks`/`create_optimizer`/`create_loss_and_eval_metric`/`log`） |
| `swift/trainers/patcher.py` | 猴子补丁，替换 HF 默认回调 |
| `swift/arguments/base_args/base_args.py` | `external_plugins` 机制：运行时加载外部插件 |

## 4. 核心概念与源码讲解

### 4.1 Callbacks：训练过程回调（callbacks_map 注册）

#### 4.1.1 概念说明

Callback（回调）是一种「在训练主循环的固定节点上插入自定义行为」的机制。HF Trainer 在训练过程中的关键节点（训练开始、每步开始/结束、保存、日志、评估、训练结束等）会依次调用所有已注册回调的对应 `on_xxx` 方法。ms-swift 没有另起炉灶，而是直接继承 HF 的 `TrainerCallback`，只是把构造签名改成 `(args, trainer)`，让回调能直接拿到参数对象和训练器本身——这样回调就不必从 `kwargs` 里七拐八绕地取值。

ms-swift 内置了 7 个回调，集中体现在 `callbacks_map` 里：`activation_cpu_offload`（FSDP 激活卸载到 CPU）、`adalora`（AdaLoRA 的步长调度）、`deepspeed_elastic`/`graceful_exit`（弹性训练与优雅退出）、`early_stop`（早停）、`lisa`（随机激活层训练）、`perf_log`（MFU 性能日志）。其中一部分会被框架根据其它参数**自动注入**，另一部分需要用户显式 `--callbacks` 指定。

#### 4.1.2 核心流程

回调从「字符串名」变成「在训练中生效的对象」，经历三步：

1. **参数收集**：用户在命令行写 `--callbacks early_stop perf_log`，或由 `TrainArgumentsMixin._init_callbacks` 根据其它参数自动追加（如设了 `--early_stop_interval` 就追加 `early_stop`）。
2. **查表实例化**：`SwiftMixin._get_callbacks` 遍历 `args.callbacks`，对每个名字查 `callbacks_map` 得到类，再 `callbacks_map[name](args, self)` 实例化。
3. **注入 Trainer**：实例化得到的回调列表在 `super().__init__(..., callbacks=callbacks)` 时传给 HF Trainer，HF 把它们存进 `self.callback_handler`，并在各节点统一调度。

用伪代码表示：

```text
args.callbacks = ['early_stop']            # 用户/自动注入
  ↓ _get_callbacks(args)
[callbacks_map['early_stop'](args, self)]  # 查表 + 实例化
  ↓ super().__init__(callbacks=...)
HfTrainer.callback_handler.callbacks       # HF 接管调度
  ↓ 训练循环各节点
EarlyStopCallback.on_save(...)             # 在 save 节点被调用
```

#### 4.1.3 源码精读

**基类**非常薄，只做「持有 args 与 trainer」这一件事——这就是 ms-swift 相对原生 HF 回调的唯一增强：

[swift/callbacks/base.py:L9-L13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/callbacks/base.py#L9-L13) —— `TrainerCallback` 继承 HF 的同名类，构造时把 `args` 和 `trainer` 存为属性，其余 `on_xxx` 钩子全部沿用 HF 接口。

**注册表**用字典把字符串名映射到类，注释里那句 `# Add your own ...` 与 metrics/optimizers 完全同构：

[swift/callbacks/mapping.py:L9-L17](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/callbacks/mapping.py#L9-L17) —— `callbacks_map` 收录 7 个内置回调；文件顶部 `from .xxx import XxxCallback` 是「导入即注册」的体现。

**自动注入逻辑**在参数后置初始化里，框架会根据其它参数悄悄把需要的回调塞进 `args.callbacks`：

[swift/trainers/arguments.py:L231-L240](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/arguments.py#L231-L240) —— `_init_callbacks` 依据 `lisa_activated_layers`/`tuner_type=='adalora'`/`early_stop_interval`/FSDP `activation_cpu_offload` 四个条件自动追加对应回调。注意 `perf_log` 不在其中，它只能靠用户显式 `--callbacks perf_log` 启用。

[swift/trainers/arguments.py:L242-L251](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/arguments.py#L242-L251) —— `__post_init__` 末尾调用 `_init_callbacks()`，并在设了 `vit_lr`/`aligner_lr` 时把 `optimizer` 自动切到 `multimodal`（这条线在 4.2 节展开）。

**装配点**在 `SwiftMixin.__init__` 中，回调被实例化后通过 `callbacks=` 传给 HF Trainer：

[swift/trainers/mixin.py:L155-L159](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L155-L159) —— `_get_callbacks` 逐个查 `callbacks_map` 实例化。mixin.py 顶部 [L44 与 L158](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L44-L44) 处 `from swift.callbacks import callbacks_map` 即此处所用。

**两个范例回调**展示了两类典型用法：

[swift/callbacks/early_stop.py:L15-L34](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/callbacks/early_stop.py#L15-L34) —— `EarlyStopCallback.on_save` 在每次保存后比较 `state.best_metric`，若连续 `early_stop_interval` 次未改善就把 `control.should_training_stop` 置真，停止训练。`greater_is_better` 决定用 `np.greater` 还是 `np.less`（该字段是 HF `TrainingArguments` 原生字段，由 sft_args 推断）。

[swift/callbacks/perf_log.py:L36-L79](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/callbacks/perf_log.py#L36-L79) —— `PerfMetricsLogCallback` 在 `on_init_end` 估算设备理论 TFLOPS（优先读 `DEVICE_TFLOPS` 环境变量，否则跑一次矩阵乘法基准测试），在 `on_step_begin/end` 计时，在 `on_log` 把 MFU 写进 `logs`。MFU 的计算为：

\[
\text{MFU} = \frac{\text{state.total\_flos}}{\text{elapsed}\ \times\ \text{max\_tflops}\ \times\ 10^{12}}
\]

即实际每秒浮点运算量除以集群理论峰值。注意 `state.total_flos` 是 HF 跨所有 rank 累加的全局值，故分母用的是集群总 GPU 数。

**第三类范例**展示「回调直接改模型状态」的用法：

[swift/callbacks/lisa.py:L42-L57](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/callbacks/lisa.py#L42-L57) —— `LISACallback.on_step_begin` 每隔 `step_interval` 步重新随机选 `n_layers` 层解冻、其余冻结，实现 LISA（Layerwise Importance Sampling Attention）训练。它直接操作 `self.trainer.model` 的 `requires_grad`，是「回调驱动训练策略」的典型。

> 还有一条与 HF 默认回调相关的线：[swift/trainers/patcher.py:L105-L108](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/patcher.py#L105-L108) 在包导入时把 `trainer.DEFAULT_PROGRESS_CALLBACK`/`DEFAULT_CALLBACKS`/`PrinterCallback` 替换成 ms-swift 增强版（带 `logging.jsonl` 落盘、剩余时间估算、最后一步强制保存）。这是 u5-l1 已讲过的猴子补丁，这里只需记住：你看到的训练日志格式来自这层补丁，而非 `callbacks_map`。

#### 4.1.4 代码实践

**实践目标**：实现一个自定义回调 `MemReporterCallback`，在训练开始时打印各 GPU 显存占用，并通过 `--external_plugins` + `--callbacks` 加载它，不修改任何源码。

**操作步骤**：

1. 新建文件 `~/mem_plugin.py`，内容如下（示例代码，非项目原有代码）：

   ```python
   # 示例代码：自定义回调，训练开始时打印显存
   from swift.callbacks import TrainerCallback, callbacks_map
   from swift.utils import get_logger, get_max_reserved_memory

   logger = get_logger()


   class MemReporterCallback(TrainerCallback):

       def on_train_begin(self, args, state, control, **kwargs):
           mem = get_max_reserved_memory()  # 单位 GiB
           logger.info(f'[MemReporter] train begin, reserved memory: {mem:.2f} GiB')


   # 注册：直接往全局 map 里塞，等价于修改 mapping.py
   callbacks_map['mem_reporter'] = MemReporterCallback
   ```

2. 跑一个最小 sft 并启用它（复用 u1-l5 的 self-cognition 小数据集）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen3-4B \
       --dataset 'swift/self-cognition#200' \
       --train_type lora \
       --max_steps 20 \
       --output_dir output/mem-cb-test \
       --external_plugins ~/mem_plugin.py \
       --callbacks mem_reporter
   ```

3. 同样跑一次不加 `--callbacks mem_reporter` 的版本作为对照。

**需要观察的现象**：

- 训练日志开头是否出现 `[MemReporter] train begin, reserved memory: ... GiB`。
- 对照组是否没有这一行。

**预期结果**：实验组在 `Train` 进度条出现前打印一次显存；对照组无此行。若报 `KeyError: 'mem_reporter'`，多半是 `--external_plugins` 路径不对或插件文件未执行到注册那行——可先 `python -c "import importlib.util, sys; print('ok')"` 排查。

> 若本地没有 GPU 或拉模型受阻，可退化为「源码阅读型实践」：阅读 `PerfMetricsLogCallback.on_log`，说明为何 MFU 的分母要用 `world_size * gpus_per_process` 而非单卡 TFLOPS，并把答案写在笔记里（待本地验证实际数值）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PerfMetricsLogCallback` 没有出现在 `_init_callbacks` 里？它怎么才会被启用？

**参考答案**：`_init_callbacks` 只自动追加 `lisa`/`adalora`/`early_stop`/`activation_cpu_offload` 四个有触发条件的回调；`perf_log` 是纯可选的性能观测工具，没有自动触发条件，必须用户显式 `--callbacks perf_log` 启用。

**练习 2**：`EarlyStopCallback` 依赖 `state.best_metric`，这个值由谁更新？为什么 `on_save` 而不是 `on_evaluate` 里判断？

**参考答案**：`state.best_metric` 由 HF Trainer 在评估后结合 `metric_for_best_model` 更新。早停写在 `on_save` 是因为 ms-swift 推荐 `eval_steps == save_steps`（见 arguments.py 字段注释），保存节点即评估节点，且在保存时判断能确保「最佳模型已落盘后再决定是否停」。

**练习 3**：如果想在 `on_log` 里往日志加一个自定义字段（如当前学习率的平方），该往哪个变量写？

**参考答案**：往 `logs` 字典里写键值对即可（参考 `PerfMetricsLogCallback.on_log` 里的 `logs['MFU'] = ...`）。`logs` 会被 HF 进一步传给 `PrinterCallback`/`ProgressCallback` 打印并落盘 `logging.jsonl`。

### 4.2 Optimizers：自定义优化器（optimizers_map）

#### 4.2.1 概念说明

`OptimizerCallback` 是 ms-swift 对「优化器与学习率调度器创建」的抽象。HF Trainer 原本用 `create_optimizer`/`create_scheduler` 两个方法直接建优化器，ms-swift 把它们抽到一个回调对象里，好处是：换优化器策略时只需写一个新回调类、覆写一两个方法，而不必继承整个 Trainer。这与 u5-l1 讲的「Trainer 以 template 为中心装配」一脉相承——Trainer 把建优化器的责任委托给 `optimizer_callback`。

内置 6 个优化器回调：`default`（走 HF 默认 AdamW）、`galore`（GaLore 低秩投影优化器）、`lorap`（LoRA+，A/B 矩阵不同学习率）、`muon`（Muon，对二维参数用牛顿-舒尔茨迭代更新）、`muonclip`（Muon + 梯度裁剪）、`multimodal`（ViT/Aligner/LLM 三段不同学习率）。

#### 4.2.2 核心流程

优化器回调的生命周期与 Trainer 的创建阶段绑定：

1. **选择与实例化**：`SwiftMixin.__init__` 里 `self.optimizer_callback = optimizers_map[args.optimizer or 'default'](args, self)`，`args.optimizer` 为 `None` 时走 `default`。
2. **延迟创建**：Trainer 后续调用 `create_optimizer_and_scheduler(num_training_steps)`，转发给 `self.optimizer_callback.create_optimizer_and_scheduler(...)`。
3. **落位**：回调把建好的 optimizer/scheduler 赋给 `trainer.optimizer` 与 `trainer.scheduler`（或 `trainer.lr_scheduler`），训练循环即可使用。

```text
args.optimizer = 'galore'  (或 None → 'default')
  ↓ SwiftMixin.__init__
self.optimizer_callback = optimizers_map[...](args, self)
  ↓ trainer.train() 阶段
self.create_optimizer_and_scheduler(max_steps)
  → self.optimizer_callback.create_optimizer_and_scheduler(max_steps)
  → trainer.optimizer / trainer.lr_scheduler 就位
```

#### 4.2.3 源码精读

**基类**用三个方法定义契约：`create_optimizer_and_scheduler`（总入口，串起另两个）、`create_optimizer`、`create_scheduler`。后两者默认委托给 HF Trainer 的同名静态方法：

[swift/optimizers/base.py:L14-L56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/base.py#L14-L56) —— `OptimizerCallback` 默认实现把 `trainer.optimizer`/`trainer.scheduler` 交给 HF 原生逻辑。子类通常只需覆写 `create_optimizer`（如 lorap/muon/multimodal）或 `create_optimizer_and_scheduler`（如 galore，需同时定制 scheduler）。

**注册表**与 callbacks 同构：

[swift/optimizers/mapping.py:L9-L16](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/mapping.py#L9-L16) —— `optimizers_map` 含 6 个条目，`'default'` 指向基类本身。

**装配点**在 `SwiftMixin` 中，注意它把创建责任整体委托给回调：

[swift/trainers/mixin.py:L87-L87](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L87-L87) —— 构造期选定回调：`optimizers_map[args.optimizer or 'default'](args, self)`。

[swift/trainers/mixin.py:L1056-L1070](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1056-L1070) —— `create_optimizer_and_scheduler`/`create_optimizer`/`create_scheduler` 三个方法都只是转发给 `self.optimizer_callback`，自身仅做少量收尾（如过滤空参数组、修复 deepspeed + cosine_with_min_lr 的兼容问题）。

**四个范例**呈现两种覆写粒度：

[swift/optimizers/multimodal.py:L43-L74](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/multimodal.py#L43-L74) —— `MultimodalOptimizerCallback.create_optimizer` 仅覆写建优化器：借助 `model_arch` 的 `vision_tower`/`aligner`/`language_model` 三段前缀把参数分成三组，分别配 `vit_lr`/`aligner_lr`/`learning_rate`。这正是 arguments.py 里「设了 `vit_lr` 或 `aligner_lr` 就自动切到 `multimodal`」的落地。

[swift/optimizers/lorap.py:L7-L34](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/lorap.py#L7-L34) —— `LorapOptimizerCallback` 优先调用 `model.create_optimizer_param_groups`（LoRA+ 模型自带的方法，给 A/B 矩阵不同学习率），若模型没有该方法则回退到默认的「衰减/不衰减」两组参数。

[swift/optimizers/muon.py:L8-L46](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/muon.py#L8-L46) —— `MuonOptimizerCallback.create_optimizer` 把模型参数按「二维且非 embedding/lm_head」分到 `muon_params`（用 Muon 更新），其余分到 `adamw_params`（用 AdamW 更新）。它还演示了一个常见模式：从 GitHub 拉取第三方实现（`git_clone_github('.../Moonlight.git')`）并 `sys.path.append` 后再 import。

[swift/optimizers/galore/utils.py:L222-L246](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/optimizers/galore/utils.py#L222-L246) —— `GaloreOptimizerCallback` 覆写的是 `create_optimizer_and_scheduler`（而非 `create_optimizer`），因为它要同时定制 scheduler（GaLore 每个参数可能有独立 optimizer/scheduler，包成 `GaloreOptimizerWrapper`/`GaloreSchedulerWrapper`）。GaLore 的核心思想是对权重梯度 \(G\) 做低秩投影：周期性地对 \(G^\top G\) 做 SVD 取前 \(r\) 个奇异向量得投影矩阵 \(P\)，在 \(P\) 张成的低秩子空间里用 AdamW 更新，再投影回原空间：

\[
G_{\text{low}} = P\,\text{AdamW}(P^\top G),\qquad W \leftarrow W - \eta\, P\, G_{\text{low}}
\]

从而把大权重矩阵的优化器状态量降到 \(O(r)\) 而非 \(O(d)\)。`update_proj_gap` 控制多久重算一次 \(P\)。

#### 4.2.4 代码实践

**实践目标**：用 `--use_galore true` 与 `--optimizer lorap` 分别跑一次全参微调，对比可训练参数量与显存。

**操作步骤**：

1. 参考 [examples/train/tuners/galore/train_galore.sh:L1-L18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/tuners/galore/train_galore.sh#L1-L18) 跑 GaLore：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen2.5-7B-Instruct \
       --tuner_type full \
       --dataset 'swift/self-cognition#1000' \
       --num_train_epochs 1 \
       --per_device_train_batch_size 1 \
       --learning_rate 1e-5 \
       --gradient_accumulation_steps 16 \
       --logging_steps 5 \
       --model_author swift --model_name swift-robot \
       --use_galore true --galore_optim_per_parameter true
   ```

2. 再用 LoRA+ 跑一个 LoRA 微调作为对照（lorap 仅在 tuner 为 lora 时有意义）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen2.5-7B-Instruct \
       --tuner_type lora \
       --dataset 'swift/self-cognition#1000' \
       --num_train_epochs 1 \
       --learning_rate 1e-4 \
       --logging_steps 5 \
       --model_author swift --model_name swift-robot \
       --optimizer lorap
   ```

**需要观察的现象**：

- 两次训练启动日志里 `trainable params` 与 `trainable percent` 的差异。
- 训练日志里 `memory(GiB)` 字段的峰值差异（GaLore 注释标注约 38GiB）。

**预期结果**：GaLore + full 的可训练参数为全模型参数，但优化器状态因低秩投影显著小于标准 AdamW；lorap + lora 的可训练参数极少（仅 LoRA 增量），显存最低。具体数值待本地验证。

> 若显存不足，可退化为源码阅读型实践：阅读 `_create_optimizer_and_scheduler`（galore/utils.py L85-L172），说明 `optim_per_parameter=True` 时为何要为每个参数建独立 optimizer 与 scheduler，并把 `update_proj_gap` 乘 2 的原因写下来。

#### 4.2.5 小练习与答案

**练习 1**：`args.optimizer` 为 `None` 时会发生什么？为什么 `default` 指向基类 `OptimizerCallback` 本身？

**参考答案**：`optimizers_map[args.optimizer or 'default']` 会取 `'default'`，即基类 `OptimizerCallback`。基类的 `create_optimizer`/`create_scheduler` 默认委托给 `HfTrainer.create_optimizer`/`create_scheduler`，故等价于「走 HF 原生 AdamW + 你指定的 lr_scheduler_type」，即不引入任何自定义逻辑。

**练习 2**：`MultimodalOptimizerCallback` 是怎么被自动启用的？用户需要显式写 `--optimizer multimodal` 吗？

**参考答案**：通常不需要。arguments.py 的 `__post_init__` 里 `if self.optimizer is None and (self.vit_lr is not None or self.aligner_lr is not None): self.optimizer = 'multimodal'`，即只要设了 `--vit_lr` 或 `--aligner_lr`，就会自动切到 `multimodal`。当然用户也可以显式指定。

**练习 3**：为什么 `GaloreOptimizerCallback` 覆写 `create_optimizer_and_scheduler` 而不是只覆写 `create_optimizer`？

**参考答案**：GaLore 在 `optim_per_parameter=True` 时会为每个参数建独立 optimizer 和对应 scheduler，并用 `GaloreSchedulerWrapper`/`GaloreOptimizerWrapper` 把它们打包成一个「复合优化器/调度器」整体返回。这必须在一个方法里同时建好两者并赋值给 `trainer.optimizer`/`trainer.lr_scheduler`，不能拆成两次独立调用，否则 wrapper 无法成对构造。

### 4.3 Metrics：评测指标（eval_metrics_map）

#### 4.3.1 概念说明

Metrics 扩展点管的是「验证时怎么打分」。这里必须先讲清一个容易混淆的点——ms-swift 里其实有**两条**指标通路：

1. **opt-in 的 HF 验证指标**：用 `--eval_metric <name>` 选一个 `EvalMetrics` 子类，它产出 HF Trainer 的 `compute_metrics` 与 `preprocess_logits_for_metrics` 两个回调，在验证阶段由 HF 调用，算出 `eval_xxx` 指标。这就是 `eval_metrics_map`。它当前仅支持 sft/pretrain/reranker/embedding 任务（RLHF 不走此机制）。
2. **始终在线的训练日志指标**：Trainer 内部维护 `self.custom_metrics`（一个 `defaultdict(MeanMetric)`），在训练/评估的前向里由 `_compute_acc` 等方法往里 `update` 累加值，再由覆写过的 `log()` 在每次日志时 `compute` 出 `train_acc`/`eval_acc` 等。这条线用 `MeanMetric`，不经过 `eval_metrics_map`。

本节聚焦第一条（`eval_metrics_map`），但会顺带说明第二条，因为二者共享 `metrics/` 目录与「累加器」思想。

内置 5 个指标：`acc`（token/seq 准确率）、`nlg`（rouge/bleu 生成指标）、`infonce`/`paired`（embedding 指标）、`reranker`（排序指标）。

#### 4.3.2 核心流程

`EvalMetrics` 的两条方法对应 HF 验证流水线的两个钩子：

1. **logits 预处理**：HF 在收集到模型 logits 后、算指标前，先调 `preprocess_logits_for_metrics(logits, labels)`。常见操作是 `argmax` 把 vocab 维 logits 压成预测 token id（见 `AccMetrics`），或截断对齐预测与标签。
2. **指标计算**：HF 把（预处理后的 predictions, label_ids）打包成 `EvalPrediction`，调 `compute_metrics(eval_prediction)` 返回 `Dict[str, float]`，这些键值会以 `eval_` 前缀出现在验证日志里。

装配链路：

```text
args.eval_metric = 'nlg'
  ↓ SwiftMixin.create_loss_and_eval_metric(args)
eval_metric = eval_metrics_map['nlg'](args, self)
res['compute_metrics']            = eval_metric.compute_metrics
res['preprocess_logits_for_metrics'] = eval_metric.preprocess_logits_for_metrics
  ↓ kwargs 传入 HfTrainer.__init__
HfTrainer 在 evaluate() 时调用这两个回调 → 输出 eval_rouge-1 等
```

#### 4.3.3 源码精读

**基类**定义两个方法的契约：`compute_metrics` 必须实现（抽象），`preprocess_logits_for_metrics` 有默认实现（原样返回 logits）：

[swift/metrics/base.py:L11-L22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/base.py#L11-L22) —— `EvalMetrics` 同样在构造时持有 `args`/`trainer`。

**注册表**：

[swift/metrics/mapping.py:L10-L18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/mapping.py#L10-L18) —— `eval_metrics_map` 收录 5 个指标，注释明确「The metric here will only be called during validation」。

**装配点**把两个回调塞进传给 HF Trainer 的 kwargs：

[swift/trainers/mixin.py:L1046-L1054](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1046-L1054) —— `create_loss_and_eval_metric` 在 `args.eval_metric` 非空时实例化指标对象，把它的两个方法挂进 `res`；同一处还处理 `--loss_type`（u10-l1 的自定义 loss），二者合并进 `kwargs` 后在 [L118](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L118-L118) `kwargs.update(self.create_loss_and_eval_metric(args))` 传给 `super().__init__`。

**范例指标 acc**：

[swift/metrics/acc.py:L44-L60](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/acc.py#L44-L60) —— `AccMetrics.compute_metrics` 调用模块级 `compute_acc` 算 token/seq 准确率；`preprocess_logits_for_metrics` 把 logits `argmax(dim=-1)` 成预测 id。`compute_acc` 里 `labels[..., 1:]` / `preds[..., :-1]` 的错位对齐是因果语言模型的标准处理（预测第 t+1 个 token 用第 t 位的 logits）。

**第二条通路（始终在线的训练指标）**用 `MeanMetric` 累加器：

[swift/metrics/utils.py:L73-L113](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/utils.py#L73-L113) —— `MeanMetric` 维护 `state` 与 `count`，`update` 累加、`compute` 求均值；`compute` 里若 `dist.is_initialized()` 还会跨 rank `all_reduce` 求全局均值，保证多卡日志一致。

[swift/trainers/mixin.py:L108-L111](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L108-L111) —— `self.custom_metrics` 分 `train`/`eval` 两套 `defaultdict(MeanMetric)`。

[swift/trainers/mixin.py:L1137-L1140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1137-L1140) —— `_compute_acc`（在 [trainer.py:L69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer.py#L69-L69)、seq2seq_trainer.py、reranker_trainer.py 的 `compute_loss` 里被调用）把每步的 acc 列表 `update` 进 `custom_metrics`。

[swift/trainers/mixin.py:L976-L1011](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L976-L1011) —— `compute_custom_metrics` 遍历 `custom_metrics` 逐个 `compute()`+`reset()`，结果合并进 `logs`；覆写过的 `log()` 在每次日志时调用它，故训练日志里始终能看到 `train_acc`/`eval_acc`，与是否设了 `--eval_metric` 无关。

> 关键区分：`--eval_metric acc` 走的是 HF 验证循环（`EvalMetrics`），产出 `eval_token_acc`/`eval_seq_acc`；而 `custom_metrics` 走的是训练前向里的 `_compute_acc`（用 `MeanMetric`），产出日志里的 `train_acc`/`eval_acc`。两者计算口径相近但通路不同，不要混为一谈。

#### 4.3.4 代码实践

**实践目标**：用 `--eval_metric nlg` 在「预测式生成」验证里观察 rouge/bleu 指标，并对照阅读 `NlgMetrics` 与 `compute_acc`，理解两条指标通路的差异。

**操作步骤**：

1. 参考 [examples/train/predict_with_generate/train.sh:L30-L30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/predict_with_generate/train.sh#L30-L30) 的 `--metric_for_best_model rouge-l` 用法，跑一个带生成式评估的小训练：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen3-4B \
       --dataset 'swift/self-cognition#500' \
       --tuner_type lora \
       --eval_steps 50 --save_steps 50 \
       --num_train_epochs 1 \
       --eval_metric nlg \
       --predict_with_generate true \
       --metric_for_best_model rouge-l \
       --output_dir output/nlg-test
   ```

2. 阅读源码：打开 `swift/metrics/nlg.py`，对照 `compute_rouge_bleu` 看 rouge-1/rouge-2/rouge-l/bleu-4 是如何用 jieba 分词后计算的。

**需要观察的现象**：

- 验证日志里是否出现 `eval_rouge-1`/`eval_rouge-l`/`eval_bleu-4` 等字段。
- 训练日志里始终存在的 `train_acc`（来自 `custom_metrics`，与 `--eval_metric` 无关）。

**预期结果**：开启 `predict_with_generate` 后，验证阶段会真正生成文本并算 rouge/bleu；`train_acc` 则无论是否设 `--eval_metric` 都会出现。具体指标数值待本地验证。

> 退化实践（无 GPU）：阅读 `compute_acc`（acc.py L10-L41），说明 `acc_strategy='token'` 与 `'seq'` 的差别，以及 `cu_seqlens` 分支为何只在 `masks.shape[0]==1` 时生效（提示：与 padding_free 压平后的 batch 结构有关）。

#### 4.3.5 小练习与答案

**练习 1**：`EvalMetrics.preprocess_logits_for_metrics` 默认实现是什么？为什么 `AccMetrics` 要覆写它？

**参考答案**：默认实现原样返回 logits（base.py L21-L22）。`AccMetrics` 覆写它是因为准确率只需要预测类别而非完整 logits 概率分布——`argmax(dim=-1)` 把 `[batch, seq, vocab]` 压成 `[batch, seq]` 的预测 id，既省内存又让 `compute_metrics` 直接做整数比较。

**练习 2**：`--eval_metric` 设了 `acc`，训练日志里 `train_acc` 来自哪条通路？它和 `eval_token_acc` 是同一个计算吗？

**参考答案**：`train_acc` 来自始终在线的 `custom_metrics` 通路（`_compute_acc` + `MeanMetric`），与 `--eval_metric` 无关；`eval_token_acc`（若设了 `--eval_metric acc`）来自 HF 验证循环里的 `AccMetrics.compute_metrics`。两者都用 `compute_acc`，口径相近但通路不同、触发时机不同（前者每步训练前向、后者验证阶段）。

**练习 3**：为什么 `MeanMetric.compute` 里要做 `dist.all_reduce`？不做会怎样？

**参考答案**：多卡 DDP 下每张卡只看到部分 batch，单卡算的均值不是全局均值。`all_reduce` 把各卡的 `state` 与 `count` 求和后再除，得到全局准确均值。不做的话各卡日志里的 `train_acc` 会各不相同，且都偏离真值。

## 5. 综合实践

把三类扩展串起来：实现一个**自定义回调 + 自定义指标**，打包进同一个 `--external_plugins` 文件，跑一次小 sft 验证二者能协同工作。

1. 新建 `~/combo_plugin.py`（示例代码）：

   ```python
   # 示例代码：同时注册一个回调与一个指标
   from swift.callbacks import TrainerCallback, callbacks_map
   from swift.metrics import EvalMetrics, eval_metrics_map
   from swift.utils import get_logger, get_max_reserved_memory
   from transformers.trainer_utils import EvalPrediction
   from typing import Dict

   logger = get_logger()


   class StepCounterCallback(TrainerCallback):
       """每 10 步打印一次显存，训练结束打印总步数。"""

       def on_step_end(self, args, state, control, **kwargs):
           if state.global_step % 10 == 0:
               logger.info(f'[StepCounter] step={state.global_step} mem={get_max_reserved_memory():.2f}GiB')

       def on_train_end(self, args, state, control, **kwargs):
           logger.info(f'[StepCounter] training finished at step={state.global_step}')


   class LengthMetrics(EvalMetrics):
       """验证时统计预测 token 长度的均值（演示自定义指标的最小骨架）。"""

       def compute_metrics(self, eval_prediction: EvalPrediction) -> Dict[str, float]:
           preds = eval_prediction.predictions
           labels = eval_prediction.label_ids
           masks = labels != -100
           lengths = masks.sum(axis=-1)
           return {'pred_len_mean': float(lengths.mean())}


   callbacks_map['step_counter'] = StepCounterCallback
   eval_metrics_map['length'] = LengthMetrics
   ```

2. 跑训练：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift sft \
       --model Qwen/Qwen3-4B \
       --dataset 'swift/self-cognition#300' \
       --tuner_type lora \
       --max_steps 30 \
       --eval_steps 30 --save_steps 30 \
       --eval_metric length \
       --external_plugins ~/combo_plugin.py \
       --callbacks step_counter \
       --output_dir output/combo-test
   ```

3. 检查：日志里应同时出现 `[StepCounter]` 行（来自回调）与 `eval_pred_len_mean`（来自指标）；同时 `train_acc` 仍由 `custom_metrics` 通路照常输出。

**预期结果**：三类扩展点（callbacks / metrics，外加未在此命令显式用的 optimizers）共享同一套注册与加载机制——`--external_plugins` 导入文件时执行模块顶层语句，把类塞进对应 `*_map`，随后 `--callbacks`/`--eval_metric`/`--optimizer` 即可按名引用。这就是 ms-swift「三件套」的威力：学会一个注册表，就掌握了全部扩展点。具体数值与日志细节待本地验证。

## 6. 本讲小结

- ms-swift 的 callbacks/optimizers/metrics 三类扩展点完全遵循 u1-l3 的「三件套」范式：`base.py` 基类 + `mapping.py` 的 `*_map` 注册表 + 一个 CLI 开关（`--callbacks`/`--optimizer`/`--eval_metric`），且基类构造统一为 `(args, trainer)`，让扩展能直拿参数与训练器。
- **Callbacks**：`TrainerCallback` 继承 HF 同名类，钩子名与 HF 一致；`_init_callbacks` 会按其它参数自动注入 `lisa`/`adalora`/`early_stop`/`activation_cpu_offload`，`perf_log` 等需显式 `--callbacks`；回调在 `_get_callbacks` 里查表实例化后经 `super().__init__(callbacks=...)` 交 HF 调度。
- **Optimizers**：`OptimizerCallback` 用 `create_optimizer_and_scheduler`/`create_optimizer`/`create_scheduler` 三方法定义契约，`SwiftMixin` 把建优化器的责任整体委托给它；子类按需覆写一两个方法即可（multimodal/lorap/muon 覆写 `create_optimizer`，galore 覆写 `create_optimizer_and_scheduler`）。
- **Metrics** 有两条通路：opt-in 的 `eval_metrics_map`（`EvalMetrics` 产出 HF 的 `compute_metrics`/`preprocess_logits_for_metrics`，仅验证时调用，仅限 sft/pretrain/reranker/embedding）与始终在线的 `custom_metrics`（`MeanMetric` 累加器 + 覆写的 `log()`，产出 `train_acc`/`eval_acc`，跨 rank all_reduce）。
- `--external_plugins`（`_import_external_plugins`）让你不改源码即可把自定义类塞进任意 `*_map`，是二次开发的统一入口；它兼容旧名 `--custom_register_path`。
- 三类扩展都挂在 `SwiftMixin` 上，与 u5-l1 讲的 TrainerFactory 派发、patcher 猴子补丁共同构成 Trainer 的可扩展骨架。

## 7. 下一步学习建议

- 下一篇 **u10-l3 自定义模型、模板与 Agent 注册** 会把同样的「三件套」思路用到 `register_model`/`register_template`/`agent_template_map`，建议对照本讲理解注册表范式的复用。
- 若你想深入优化器原理，可继续阅读 `swift/optimizers/galore/galore_projector.py` 与 `swift/optimizers/muonclip.py`，结合 GaLore 论文（arXiv:2403.03507）理解低秩投影与牛顿-舒尔茨迭代。
- 想了解 RL 场景下的回调用法，可阅读 `swift/rlhf_trainers/grpo_trainer.py` 中 `add_callback(SyncRefModelCallback)` 与 `AsyncGenerateCallback` 的注入点，它们是「回调驱动 RL 训练」的实例。
- 建议回头重读 `swift/trainers/mixin.py` 的 `__init__`（L75-L149），把本讲的三类装配点与 u5-l1 的 template/data_collator/loss 装配点连成一张完整的 Trainer 装配图。
