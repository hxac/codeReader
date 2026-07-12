# Megatron 训练流程

## 1. 本讲目标

本讲是 Megatron-SWIFT 系列的第二篇。上一篇 `u9-l3` 解决了「架构与权重转换」，把 HF 权重搬进了 Megatron-Core（mcore）世界；本讲解决「**这些权重如何在多卡上跑起并行训练**」。读完本讲，你应当能够：

- 说清 `MegatronArguments` 里那一组并行参数（TP/PP/SP/CP/EP/VPP）各自的含义、默认值与相互约束，并能算出给定卡数下的数据并行度（DP）与梯度累加步数；
- 跟踪 `megatron_sft_main` 的完整执行路径：从命令行到 `MegatronSft.run()`，再到 `MegatronTrainer.train()` 的训练循环；
- 解释 TP/PP/SP/CP/EP/VPP 六种并行如何「切」模型与数据，以及 mcore 的 `mpu.initialize_model_parallel` 如何按这些参数划分进程组。

本讲承接 `u9-l3`（模型与权重已就位）与 `u5-l4`（普通 `swift sft` 的 SwiftSft 主流程），把训练器装配从 transformers 后端推广到 Megatron 后端。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（不熟悉也没关系，下面会用通俗语言再点一遍）：

- **Megatron 的并行家族**：训练大模型时单卡装不下，需要把模型/数据「切开」分到多张卡上。Megatron 用一组正交的切分维度组合实现这一点：
  - **TP（Tensor Parallel，张量并行）**：把单个线性层的权重矩阵**按列/行切开**分到多卡，每卡算一部分，再 all-reduce 拼起来。切的是「层内」。
  - **PP（Pipeline Parallel，流水并行）**：把模型的不同**层**分到不同卡（卡 0 放 1-16 层，卡 1 放 17-32 层），前向逐卡接力、反向逐卡回传。切的是「层间」。
  - **SP（Sequence Parallel，序列并行）**：在 TP 基础上，把 LayerNorm/Dropout 这些**不需要切权重**的算子按序列维再切一刀，省激活显存。它是 TP 的「附属品」，离开 TP 不成立。
  - **CP（Context Parallel，上下文并行）**：把**超长序列本身**按序列维切到多卡，靠环形通信做注意力，专治长文本训练。
  - **EP（Expert Parallel，专家并行）**：MoE（混合专家）模型专属，把不同的**专家**分到不同卡。
  - **VPP（Virtual Pipeline Parallel，虚拟流水并行）**：把 PP 的每个 stage 再细分成多个 chunk 交叉调度，减少流水「气泡」。
  - **DP（Data Parallel，数据并行）**：以上都切完，剩下的卡用来复制多份模型各训各的 batch。DP 不是用户直接指定的，而是「用剩余卡数自动算出来」。
- **micro-batch 与 num_microbatches**：PP 里前向接力一次只能处理一个很小的 batch（micro-batch）。为了填满流水线、掩盖气泡，一次全局 step 要把多个 micro-batch 灌进去，这个数叫 `num_microbatches`（也就是梯度累加次数）。
- **mpu（model parallel utility）**：mcore 里管理「进程组」的工具。并行训练的本质是「把全局进程划分成若干互相通信的子组」，mpu 就是这些子组的登记处。

一个直白的类比：把一栋大楼（模型）盖到几块地（GPU）上。TP 是把每面墙切开几家合砌；PP 是把不同楼层分给不同地块；SP 是连抹灰（LayerNorm）也分工；CP 是把超长走廊分段；EP 是把不同功能间（专家）分散；DP 是把整栋楼复制几份同时盖。`world_size`（总地块数）必须能被 `TP×PP×CP` 整除，剩下的就是 DP。

> 与 `u9-l2` 的区别提醒：`u9-l2` 讲的是**普通 swift 后端**（transformers + DeepSpeed）里的「序列并行」（Ulysses / Ring-Attention），通过 `--sequence_parallel_size` 触发，切的是序列/头维。本讲的 `sequence_parallel` 是 **mcore 原生**的 SP，绑定 TP，二者同名但机制不同，不要混淆。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [swift/megatron/arguments/megatron_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py) | `MegatronArguments`，定义 TP/PP/SP/CP/EP/VPP 等并行参数与 `_init_distributed`/`_init_vpp_size` |
| [swift/megatron/arguments/megatron_base_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_base_args.py) | `MegatronBaseArguments`，把 `sequence_parallel_size` 映射到 `context_parallel_size`，组合 `BaseArguments` |
| [swift/megatron/arguments/sft_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/sft_args.py) | `MegatronSftArguments`，sft 专用参数与输出目录/迭代数初始化 |
| [swift/megatron/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py) | `MegatronSft` 管道与 `megatron_sft_main` 入口 |
| [swift/megatron/trainers/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py) | `BaseMegatronTrainer`，模型/优化器装配与 `train`/`train_step` 训练循环 |
| [swift/megatron/trainers/trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/trainer.py) | `MegatronTrainer`，`forward_step` 与 `loss_func` |
| [swift/megatron/utils/megatron_lm_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py) | `initialize_megatron` / `_initialize_mpu`，调用 `mpu.initialize_model_parallel` 划分进程组 |
| [examples/megatron/sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/sft.sh) | 全量 sft 示例（Qwen2.5-7B，TP=2） |
| [examples/megatron/lora/dense.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/lora/dense.sh) | 稠密模型 LoRA 示例（TP=2，可对比显存） |
| [examples/megatron/lora/moe.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/lora/moe.sh) | MoE 模型 LoRA 示例（EP=2） |

## 4. 核心概念与源码讲解

### 4.1 MegatronArguments 的并行参数体系

#### 4.1.1 概念说明

`MegatronArguments` 是 ms-swift 对 Megatron 训练参数的总封装，它的设计哲学和 `u2-l1` 讲过的 `BaseArguments` 一脉相承——**用 dataclass 字段把所有可调旋钮暴露出来，在 `__post_init__` 里做校验与推断**。区别在于：普通 swift 的参数围绕 transformers/DeepSpeed，而 `MegatronArguments` 围绕 mcore 的并行与优化器。

本模块只聚焦「并行参数」这一组旋钮。理解它们的关键是把握一条主线：**用户只指定 TP/PP/CP/EP/VPP，DP 是被算出来的；而 `global_batch_size` 必须能被 `micro_batch_size × DP` 整除，否则报错**。

#### 4.1.2 核心流程

并行参数的生命周期分三步：

```
① 用户在命令行指定：--tensor_model_parallel_size 2 --pipeline_model_parallel_size 1 ...
② MegatronArguments.__post_init__ → _init_distributed()：
     - 调 initialize_megatron → _initialize_mpu → mpu.initialize_model_parallel
       按 TP/PP/VPP/CP/EP/ETP 划分进程组，并打印拓扑
     - 算出 data_parallel_size = world_size // (TP × PP × CP)
     - 算出 num_microbatches = global_batch_size // (micro_batch_size × data_parallel_size)
③ _init_vpp_size() 处理虚拟流水（VPP）的派生约束
```

两个核心公式：

\[
\text{data\_parallel\_size} = \frac{\text{world\_size}}{\text{TP} \times \text{PP} \times \text{CP}}
\]

\[
\text{num\_microbatches} = \frac{\text{global\_batch\_size}}{\text{micro\_batch\_size} \times \text{data\_parallel\_size}}
\]

注意分子没有 EP/ETP——因为专家并行只切 MoE 的专家子模块，不参与「模型整体被复制几份」的计算。`num_microbatches` 本质就是**流水并行的梯度累加次数**（一个全局 step 灌多少个 micro-batch）。

#### 4.1.3 源码精读

**并行参数字段**：六个并行维度都集中在 `MegatronArguments` 的一段里，默认全为 1（即默认不并行，退化成单卡/纯 DP）：

[swift/megatron/arguments/megatron_args.py:588-614](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L588-L614) 定义了 `tensor_model_parallel_size`（TP，588 行）、`pipeline_model_parallel_size`（PP，589 行）、`decoder_first/last_pipeline_num_layers`（PP 首尾 stage 层数微调，590-591 行）、`sequence_parallel`（SP，598 行）、`context_parallel_size`（CP，599 行）与 `cp_partition_mode`（CP 切分方式 zigzag/contiguous，600 行）、`virtual_pipeline_model_parallel_size`（VPP，610 行）、`expert_model_parallel_size`（EP，613 行）与 `expert_tensor_parallel_size`（ETP，614 行）。这一段就是并行能力的全部开关。

**DP 与 num_microbatches 的推导**：这是理解并行参数如何落地的核心：

[swift/megatron/arguments/megatron_args.py:921-937](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L921-L937) 是 `_init_distributed`：第 922 行调 `initialize_megatron(self)` 初始化分布式与进程组；第 923-924 行算 `total_model_size = TP × PP × CP`；第 926 行 `data_parallel_size = world_size // total_model_size`；第 928-932 行校验 `global_batch_size` 必须能被 `micro_batch_size × data_parallel_size` 整除；第 932 行 `num_microbatches = global_batch_size // micro_batch_times_data_parallel_size`。第 933-937 行保证至少有一个 micro-batch。

**SP 的隐式约束**：mcore 原生 SP 依赖 TP，TP=1 时 SP 无意义，框架会静默关闭并打印说明。这是一个容易踩坑的点：

[swift/megatron/arguments/megatron_args.py:886-887](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L886-L887) `if self.sequence_parallel and self.tensor_model_parallel_size <= 1: self.sequence_parallel = False`。即「SP 必须配 TP 用」。

[swift/megatron/arguments/megatron_args.py:892-894](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L892-L894) 进一步约束 `tp_comm_overlap`（TP 通信与计算重叠）也只能在 SP 开启时用。

**VPP 的派生约束**：虚拟流水有几个隐式规则，框架在 `_init_vpp_size` 里处理：

[swift/megatron/arguments/megatron_args.py:976-996](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L976-L996) `_init_vpp_size`：支持用 `pipeline_model_parallel_layout` 字符串描述自定义层布局（977-987 行）；第 990-991 行把 VPP=1 归一化为 `None`（即「不开 VPP」）；第 992-994 行说明**不开 VPP 时，P2P 通信重叠与参数收集对齐都会被关闭**——因为这两个优化只在 interleaved 调度下才有意义。

**与「普通 swift 序列并行」的衔接**：`MegatronBaseArguments` 做了一个名字映射，避免与 `u9-l2` 的 `sequence_parallel_size` 冲突：

[swift/megatron/arguments/megatron_base_args.py:34-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_base_args.py#L34-L39) 第 35 行 `self.sequence_parallel_size = self.context_parallel_size`——把上层（复用自普通 swift）的 `sequence_parallel_size` 直接等同于 mcore 的 `context_parallel_size`；第 39 行 `self.seq_length = self.packing_length or self.max_length` 给流水并行确定固定序列长度。这解释了为什么 `u9-l2` 那套长文本并行参数在 Megatron 后端会自动落到 CP 上。

#### 4.1.4 代码实践

**实践目标**：不跑训练，纯靠读源码与手算，验证你对 DP / num_microbatches 推导的理解。

**操作步骤**：

1. 打开示例 [examples/megatron/sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/sft.sh)，记录关键值：`NPROC_PER_NODE=2`、`tensor_model_parallel_size=2`、`pipeline_model_parallel_size`（未指定，默认 1）、`context_parallel_size`（未指定，默认 1）、`micro_batch_size=16`、`global_batch_size=16`。
2. 套用公式手算：`world_size = 2`，`total_model_size = TP×PP×CP = 2×1×1 = 2`，故 `data_parallel_size = 2 // 2 = 1`，`num_microbatches = 16 // (16×1) = 1`。
3. 改造练习：若把该脚本改成 `NPROC_PER_NODE=4`、`tensor_model_parallel_size=2`、`pipeline_model_parallel_size=2`，其余不变，手算 `data_parallel_size` 与 `num_microbatches`。
4. 验证：在第 3 步配置下，若再把 `global_batch_size` 改成 17（质数），预测框架会报什么错，并对照 [megatron_args.py:929-931](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L929-L931) 确认。

**需要观察的现象**：第 2 步手算得到 `data_parallel_size=1, num_microbatches=1`，意味着该示例其实是「2 卡 TP、无 DP、无梯度累加」；第 3 步应得到 `data_parallel_size=1, num_microbatches=1`（因为 `world_size=4, total_model_size=4`）；第 4 步 `17 % (16×1) != 0`，会抛 `ValueError`。

**预期结果**：能凭公式正确算出任意 TP/PP/CP/卡数组合下的 DP 与 num_microbatches，并理解「`global_batch_size` 必须被 `micro_batch_size × DP` 整除」这条硬约束。

**待本地验证**：第 4 步的报错信息请以本地实际运行为准（公式推导是确定的）。

#### 4.1.5 小练习与答案

**练习 1**：一台 8 卡机器上跑 `--tensor_model_parallel_size 2 --pipeline_model_parallel_size 2`，`data_parallel_size` 是多少？若想多复制一份模型并行训（DP=2），需要多少卡？

> **答案**：`data_parallel_size = 8 // (2×2×1) = 2`。要 DP=2 则 `world_size = TP×PP×CP×DP = 2×2×1×2 = 8`，仍是 8 卡即可——即 8 卡天然分成 2 组 TP=2/PP=2 的副本各自训。

**练习 2**：为什么 `num_microbatches` 公式里没有 EP（专家并行）？

> **答案**：EP 只把 MoE 的**专家子模块**分散到多卡，模型整体的「前向接力单位」仍是 micro-batch，`num_microbatches` 描述的是一个全局 step 灌多少 micro-batch，与专家怎么分布无关。EP 影响的是「某个 token 该发给哪个专家所在的卡」（专家路由与 all-to-all），不影响 batch 维的累加计数。

**练习 3**：用户写了 `--sequence_parallel true` 但没写 `--tensor_model_parallel_size`，会发生什么？

> **答案**：TP 默认为 1，`__post_init__` 检测到 `sequence_parallel=True and TP<=1`，会**静默**把 `sequence_parallel` 置回 `False`（[megatron_args.py:886-887](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L886-L887)）。不会报错，但 SP 实际没生效——这是常见踩坑点。

---

### 4.2 megatron_sft_main 训练主流程

#### 4.2.1 概念说明

`megatron_sft_main` 是 `megatron sft` 命令的终点站。它做的事和普通 `swift sft`（见 `u5-l4` 的 `SwiftSft`）在**骨架上完全一样**——解析参数、准备模板与数据集、装配训练器、开训——差别只在于底层的「模型」与「训练器」换成了 mcore 版本。

ms-swift 用**继承复用**来共享骨架：`MegatronSft` 直接继承普通 `SwiftSft`，只覆写少数几个钩子（`prepare_trainer`、`_set_seed`、`run`、`__init__`），而不是重写整个流水线。这与 `u7-l1` 里 `SwiftRLHF` 继承 `SwiftSft` 的思路完全一致——**「覆写钩子而非重写骨架」**。

#### 4.2.2 核心流程

`megatron sft` 的端到端执行路径：

```
megatron sft ...
  └─ cli_main(is_megatron=True)        # u9-l3 讲过：_megatron/main.py 路由 + torchrun
     └─ swift/cli/_megatron/sft.py     # 直接 from swift.megatron import megatron_sft_main
        └─ megatron_sft_main(args)
           └─ MegatronSft(args).main()         # SwiftPipeline 模板方法（u5-l4）
              ├─ MegatronSft.__init__:          # 三连准备
              │    ├─ 用 torch.device('meta') 建空壳模型 + 加载 processor
              │    ├─ _prepare_template()        # 复用自 SwiftSft
              │    └─ args.save_args()           # 落盘 args.json
              └─ MegatronSft.run():              # 覆写后的 run
                   ├─ _prepare_dataset()          # 编码数据集（复用自 SwiftSft）
                   ├─ args.init_iters(...)        # 推算 train_iters/eval_iters
                   ├─ prepare_trainer()           # 按 task_type 选 Trainer
                   │    └─ MegatronTrainer(args, template)
                   └─ trainer.train(train_dataset, val_dataset)
```

注意一个反直觉的设计：`MegatronSft.__init__` 用 `torch.device('meta')` 建的是**空壳模型**，真正带权重的 mcore 模型是在 `MegatronTrainer.prepare_model` 里才建的。这样做是为了让模板能拿到 processor/词表、并尽早写 args.json，而把昂贵的建模型推迟到训练器内部。

#### 4.2.3 源码精读

**入口**：`megatron_sft_main` 极其简短，遵循 swift 全包「`*_main` 即管道的 `.main()`」的约定：

[swift/megatron/pipelines/train/sft.py:96-97](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L96-L97) `megatron_sft_main` 直接 `return MegatronSft(args).main()`。`.main()` 来自 `SwiftPipeline` 基类（`u5-l4`），负责解析参数、设种子、计时，再调 `run()`。

**继承与钩子覆写**：`MegatronSft` 继承 `SwiftSft`，只覆写需要定制的地方：

[swift/megatron/pipelines/train/sft.py:26-37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L26-L37) `args_class = MegatronSftArguments`（27 行）把参数类换成 Megatron 版；`prepare_trainer`（30-37 行）按 `task_type` 派发：`embedding` → `MegatronEmbeddingTrainer`，`reranker`/`generative_reranker` → `MegatronRerankerTrainer`，其余 → `MegatronTrainer`。这与普通 swift 的 `TrainerFactory` 派发（`u5-l1`）思路一致，只是改成显式 if/elif。

**空壳模型 + 模板准备**：`__init__` 的关键技巧是用 `meta` 设备建模型，避免在准备阶段就占满显存：

[swift/megatron/pipelines/train/sft.py:42-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L42-L64) 第 60-61 行 `with torch.device('meta'): self.model, self.processor = args.get_model_processor(...)`，第 56-59 行多模态模型用 `return_dummy_model=True`、纯文本用 `load_model=False`——两种情况都**不真正加载权重**；第 62 行 `_prepare_template()` 拿到训练模板；第 63 行 `args.save_args(args.output_dir)` 落盘 args.json（支撑「训练即所见，推理即所得」）；第 64 行 `self.template.use_megatron = True` 给模板打上 Megatron 模式标记，影响其 data_collator 行为。

**run 主流程**：`run` 是真正的训练编排，结构上与 `SwiftSft.run` 平行：

[swift/megatron/pipelines/train/sft.py:66-93](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L66-L93) 第 68-69 行准备数据集并调 `args.init_iters(...)` 推算训练/评估迭代数；第 70 行 `trainer = self.prepare_trainer()` 装配训练器；第 72 行 `trainer.train(...)` 开训；`finally` 块（73-88 行）收尾：记录 checkpoint、在 last_rank 画图、写 logging.jsonl；第 91-92 行 `dist.destroy_process_group()` 销毁进程组（注意注释说明它**故意不放在 finally 里**，避免异常时进程挂起导致异常无法传播）。

**迭代数推算**：`init_iters` 把 `num_train_epochs` / `save_strategy` 等换算成具体的 `train_iters` / `eval_iters` / `save_steps`：

[swift/megatron/arguments/megatron_args.py:1017-1054](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L1017-L1054) `init_iters` 用 `step_batch_size = micro_batch_size × data_parallel_size` 把数据集条数换算成 step 数；GRPO 时还会乘 `num_generations`（1020 行）；streaming 数据集要求显式指定 `--train_iters`（1033-1035 行）。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，理清 `MegatronSft` 复用了 `SwiftSft` 的哪些能力、覆写了哪些钩子。

**操作步骤**：

1. 对比阅读 [sft.py:26-93](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L26-L93)（MegatronSft）与普通 `swift/pipelines/train/sft.py`（SwiftSft，见 `u5-l4`），列出 `MegatronSft` 覆写的方法名。
2. 在 `MegatronSft` 里找出三个「复用自 SwiftSft」的调用：`_prepare_template`、`_prepare_dataset`、`get_model_processor`，确认它们都来自父类。
3. 跟踪 `prepare_trainer` 的派发：阅读 [trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/trainer.py) 与 `base.py`，确认 `MegatronTrainer` 继承自 `BaseMegatronTrainer`。
4. 观察一个真实示例 [examples/megatron/lora/dense.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/lora/dense.sh)，找到它对应 `megatron sft` 命令里哪些参数会进入 `MegatronSftArguments.__post_init__`。

**需要观察的现象**：`MegatronSft` 覆写的方法很少（`__init__`/`run`/`prepare_trainer`/`_set_seed`），其余逻辑全靠继承复用；`prepare_trainer` 是一个清晰的 task_type→Trainer 派发表。

**预期结果**：能用自己的话说出「Megatron 后端的 SFT 主流程与普通 swift 同构，差异只在训练器装配与模型加载」。

**待本地验证**：本实践为源码阅读型，无需运行；若想运行，参考 4.3.4 的可执行实践。

#### 4.2.5 小练习与答案

**练习 1**：`MegatronSft.__init__` 为什么要用 `torch.device('meta')` 建模型，而不是直接加载真实权重？

> **答案**：`__init__` 阶段只需要 processor/词表与模板，真正的 mcore 模型（带权重、按 TP/PP 切分）在 `MegatronTrainer.prepare_model` 里才建。用 `meta` 设备建空壳不分配显存/内存，既能让模板拿到必要的结构信息，又避免在准备阶段就把显存撑爆。真实权重由 `bridge.load_weights`（u9-l3）在训练器里灌入。

**练习 2**：`dist.destroy_process_group()` 为什么不放在 `run` 的 `finally` 块里？

> **答案**：因为分布式训练中，若某些 rank 抛异常而 `finally` 调用了 `destroy_process_group`，其他还在通信的 rank 会因为对端消失而挂起，导致异常无法正常传播、问题难以排查。把销毁放在 `try/finally` 之外（正常路径末尾），让异常先完整暴露，再由外部清理（见 [sft.py:89-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/sft.py#L89-L92) 的注释）。

**练习 3**：`MegatronSft` 与 `SwiftSft` 共享 `_prepare_dataset`，这意味着数据预处理（编码、packing）逻辑是一样的吗？

> **答案**：是的，数据集的加载、清洗（u4-l1/u4-2）、编码与 packing（u4-l3）都复用普通 swift 的同一套 `RowPreprocessor`/`EncodePreprocessor`/`PackingDataset`。Megatron 后端只是换了「模型 + 训练器」，数据侧不变——这正是 ms-swift 模块化设计的收益。差别只在 `template.use_megatron = True` 后，data_collator 会产出 mcore 期望的 `packed_seq_params` 等字段。

---

### 4.3 MegatronTrainer 与 TP/PP/SP/CP/EP/VPP 并行策略落地

#### 4.3.1 概念说明

有了参数（4.1）和主流程（4.2），本模块回答最后一个问题：**六种并行策略在训练循环里到底怎么生效？** 答案分两层：

- **进程组划分层**：`mpu.initialize_model_parallel` 按 TP/PP/VPP/CP/EP/ETP 把所有 rank 划分成若干互相通信的子组。划分好之后，模型里每个算子「知道」自己属于哪个组、该跟谁通信——这是并行的「基础设施」。
- **训练循环层**：`MegatronTrainer.train` 用 `get_forward_backward_func()` 拿到 mcore 的流水并行调度器，在一个 step 内把 `num_microbatches` 个 micro-batch 灌进流水线，前向接力 + 反向回传 + 梯度同步。TP/SP/CP/EP 的切分则藏在模型的每个算子里（由 mcore + TE 实现），对训练器透明。

一句话总结：**用户调 TP/PP/CP/EP 参数 → mpu 划进程组 + 模型算子按组切分 → 训练器用流水调度器跑 forward_backward**。

#### 4.3.2 核心流程

```
MegatronTrainer.__init__（base.py）
  ├─ prepare_model(): get_mcore_model + bridge.load_weights + wrap_model(包 DDP/PP wrapper)
  ├─ get_optimizer_and_scheduler(): get_megatron_optimizer + lr scheduler
  ├─ data_collator = template.data_collator（带 padding_to）
  ├─ _load_checkpoint(): 恢复 mcore_model / mcore_adapter
  └─ 注册 callbacks（print/default_flow/report_to）

train(train_dataset, val_dataset)
  ├─ setup_training(): setup_model_training + 准备数据迭代器
  │    └─ VPP 时为每个 vp_stage 各准备一份数据迭代器
  └─ while iteration < train_iters:
       run_train_step
         ├─ train_step:
         │    forward_backward_func = get_forward_backward_func()  # 选 pipeline 调度器
         │    metrics = forward_backward_func(forward_step, ..., num_microbatches)
         │    optimizer.step(); opt_param_scheduler.step(global_batch_size)
         └─ 按 state.should_log/should_eval/should_save 触发日志/评估/保存

forward_step(data_iterator, model)   # 每个微批次调一次
  ├─ get_batch → data_collator 产出的 input_ids/labels/loss_scale/packed_seq_params
  ├─ output_tensor = model(**data)   # 模型内部按 TP/SP/CP/EP 切分与通信
  └─ return output_tensor, loss_func  # 把 loss 计算推迟到 pipeline 最后一个 stage
```

流水并行的精髓在于 `forward_step` 返回的是 `(output_tensor, loss_func)` 而不是算好的 loss——因为中间 stage 没有完整的 label，只有流水线最后一个 stage 才能算 loss。`get_forward_backward_func()` 会根据是否启用 VPP 选 `forward_backward_pipelining_with_interleaving` 或 `forward_backward_pipelining_without_interleaving`。

#### 4.3.3 源码精读

**进程组划分**：这是六种并行落地的总开关，由 `_initialize_mpu` 完成：

[swift/megatron/utils/megatron_lm_utils.py:58-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py#L58-L83) `_initialize_mpu`：第 60-62 行初始化 `torch.distributed`；第 71-78 行调 `mpu.initialize_model_parallel`，把 TP/PP/VPP/CP/EP/ETP 全部传入——这一步就把全局 rank 切成了若干正交子组；第 80-83 行在 master rank 打印拓扑 `TP/PP/VPP/CP/EP/ETP`。划分完成后，模型构建时（`get_mcore_model`）每个线性层会根据自己所在的 TP/PP 组决定如何切权重，这是 mcore + TransformerEngine（TE）负责的，ms-swift 不重复实现。

[swift/megatron/utils/megatron_lm_utils.py:86-97](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py#L86-L97) `initialize_megatron` 在 `_initialize_mpu` 之后设随机种子、为 MoE 模型初始化 aux loss 缩放（95-97 行）——注意 EP（专家并行）的负载均衡 aux loss 需要这个全局缩放因子，它依赖 `1/num_microbatches`。

**模型与优化器装配**：`BaseMegatronTrainer.__init__` 把并行训练需要的全部组件装好：

[swift/megatron/trainers/base.py:60-107](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L60-L107) 第 68 行 `self.prepare_model()` 建真实 mcore 模型；第 75 行 `get_optimizer_and_scheduler`（注意它用了 `use_distributed_optimizer`，把优化器状态也按 DP 切分，进一步省显存）；第 76 行 `_get_data_collator` 给 collator 注入 `padding_to`；第 83 行 `_load_checkpoint` 恢复 mcore 权重；第 97-99 行注册 callbacks；第 101-102 行若开了 `tp_comm_overlap` 则初始化 TP 通信组。

**TP/SP 配套**：`prepare_model` 里 `wrap_model` 会把模型包成 mcore 的 `DistributedDataParallel`（DDP），SP 的激活切分在算子里生效：

[swift/megatron/trainers/base.py:186-193](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L186-L193) `prepare_model`：`get_mcore_model` 建按 TP/PP 切好的模型 → `self.config.bridge` 取桥接（u9-l3）→ `bridge.load_weights` 灌 HF 权重 → `wrap_model` 包并行 wrapper。

**流水并行训练循环**：这是 PP/VPP 的核心：

[swift/megatron/trainers/base.py:735-739](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L735-L739) `train` 极简：`setup_training` 后 `while iteration < train_iters: run_train_step`。

[swift/megatron/trainers/base.py:654-665](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L654-L665) `setup_training`：第 657-662 行是 **VPP 的特殊处理**——启用虚拟流水时，要为**每一个 vp_stage** 各准备一份独立的数据迭代器（因为 interleaved 调度下不同 chunk 要交替喂数据）；不开 VPP 则只准备一份。

[swift/megatron/trainers/base.py:914-946](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L914-L946) `train_step`：第 916 行 `get_forward_backward_func()` 选流水调度器（自动按是否 VPP 选 interleaved 版）；第 926-934 行 `forward_backward_func(..., num_microbatches=args.num_microbatches, forward_only=False)` 就是「灌 num_microbatches 个 micro-batch 走完一遍前向+反向」；第 936 行 `optimizer.step()`；第 940 行 `opt_param_scheduler.step(increment=args.global_batch_size)` 按**全局 batch** 推进学习率（不是按 micro-batch）。

**forward_step 与 loss 推迟**：流水中间 stage 算不了 loss，故返回 `loss_func` 交由最后一个 stage 执行：

[swift/megatron/trainers/trainer.py:106-130](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/trainer.py#L106-L130) `forward_step`：第 108 行 `get_batch` 取一个 micro-batch（注意它从 `model.module.module.vp_stage` 拿当前 VPP stage，107 行）；第 114 行 `model(**data)` 前向（TP/SP/CP/EP 切分藏在模型内）；第 116-129 行按 `task_type` 选 `loss_func`（seq_cls 用分类损失，否则用语言模型损失），用 `partial` 绑定 label/loss_scale 后返回——`get_forward_backward_func` 会在合适的 stage 调用它。

[swift/megatron/trainers/trainer.py:48-75](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/trainer.py#L48-L75) `loss_func`：第 58 行 `loss_mask = labels != -100`（与 u3-l3 的 loss_scale 机制呼应，只在回答 token 上算 loss）；第 63 行 `torch.cat([sum(losses*mask), mask.sum()])` 同时返回**总 loss** 和**有效 token 数**，二者相除才是 per-token 平均 loss（per-token loss 由 `calculate_per_token_loss` 控制）；第 67 行 `all_reduce` 在 DP（含 CP）组上归约——注意是 DP 组不是 TP/PP 组，因为只有 DP 副本之间需要聚合统计量。

**SP/CP/EP 在算子层的体现**：这些并行的实际切分不在训练器里，而在模型前向里，但训练器有几个配套点值得注意：

- **CP 的 loss 聚合**：[trainer.py:1046-1059](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/trainer.py#L1046-L1059) 的 `get_last_tokens` 在 `context_parallel_size > 1` 时先 `reconstruct_tensor_cp` 把按 CP 切开的序列拼回来（用于分类任务取最后一个 token）。
- **EP 的 aux loss 缩放**：[base.py:136-159](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L136-L159) 的 `_log_callback` 用 `moe_loss_scale = 1 / num_microbatches / n_steps` 聚合 MoE 负载均衡 loss——这正是 EP 训练特有的统计量。
- **优化器分布式**：`use_distributed_optimizer=True`（默认）让优化器状态按 DP 切分，与 TP/PP 正交。

#### 4.3.4 代码实践

**实践目标**：在 2 卡上用 LoRA 跑通一个稠密模型（或 MoE 模型）的 Megatron 训练，记录 TP/PP/EP 配置与训练速度，并与普通 `swift sft` 对比。

**操作步骤**（需要已按 [docs/source_en/Megatron-SWIFT/Quick-start.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Megatron-SWIFT/Quick-start.md) 安装好 transformer-engine / apex / mcore-bridge，且 ≥2 张 GPU）：

1. **稠密模型 LoRA（推荐先跑这个）**：直接运行官方示例，它就是 2 卡 TP=2 的 LoRA SFT：
   ```bash
   bash examples/megatron/lora/dense.sh
   ```
   该脚本（[lora/dense.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/lora/dense.sh)）用 Qwen2.5-7B-Instruct + `--tuner_type lora --tensor_model_parallel_size 2 --sequence_parallel true`，注释里写明 LoRA 约 `2 * 14GiB, 0.45s/it`，全量约 `2 * 70GiB, 0.61s/it`。

2. **MoE 模型 LoRA（可选）**：若有多卡且想体验 EP，运行：
   ```bash
   bash examples/megatron/lora/moe.sh
   ```
   该脚本（[lora/moe.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/lora/moe.sh)）用 Qwen3-30B-A3B + `--expert_model_parallel_size 2`，注释标明约 `2 * 62GiB, 5.10s/it`。

3. **记录并行配置**：训练开始后，master rank 会打印一行类似 `TP: 2, PP: 1, VPP: None, CP: 1, EP: 1, ETP: 1`（来自 [megatron_lm_utils.py:80-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py#L80-L83)）。把它抄下来，对照你预期的 `data_parallel_size` 与 `num_microbatches`。

4. **与普通 swift sft 对比速度**：用相同模型、相同数据、相同 LoRA 配置，跑一次普通 `swift sft`（单卡或 DeepSpeed zero2），记录 `s/it`。例如：
   ```bash
   # 普通后端对照（参考 examples/train/lora_sft.sh 改造）
   CUDA_VISIBLE_DEVICES=0,1 NPROC_PER_NODE=2 \
   swift sft --model Qwen/Qwen2.5-7B-Instruct --tuner_type lora \
       --dataset 'AI-ModelScope/alpaca-gpt4-data-en#500' \
       --deepspeed default-zero2 --max_length 2048 --learning_rate 1e-4 \
       --output_dir swift_output/compare
   ```

**需要观察的现象**：
- Megatron 后端启动日志里有 `TP/PP/CP/EP` 拓扑打印，且 `data_parallel_size`、`num_microbatches` 与你手算一致；
- 训练日志每 `logging_steps`（默认 5）打印一次 `metrics`，含 loss 与 grad_norm（见 [base.py:870-876](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L870-L876)）；
- LoRA 模式下显存远低于全量（dense.sh 注释：14GiB vs 70GiB）；
- 在 7B 这种规模上，Megatron 的 `s/it` 通常**不一定**比普通 swift + DeepSpeed 快——TP 有通信开销，Megatron 的优势在更大模型（如 30B+ MoE）与更长序列时才显著。

**预期结果**：能跑通一次 `megatron sft --tuner_type lora`，并记录下 TP=2（dense）/EP=2（moe）配置与 `s/it`；理解「Megatron 不是万能加速器，它的价值在于支撑超大模型/长序列/复杂并行的可扩展性」。

**待本地验证**：实际显存与速度强依赖你的 GPU 型号、TE/flash-attn 版本与数据；本实践的数值（14GiB、0.45s/it 等）来自脚本注释，仅作量级参考，请以本地实测为准。若无 GPU 环境，可退化为源码阅读型实践：跟踪 [base.py:914-946](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L914-L946) 的 `train_step`，逐行说明 `forward_backward_func` 如何用 `num_microbatches` 填充流水线。

#### 4.3.5 小练习与答案

**练习 1**：`forward_step` 为什么返回 `(output_tensor, loss_func)` 而不是直接返回算好的 loss？

> **答案**：因为流水并行（PP）下，只有流水线**最后一个 stage** 拥有完整的 label、能算 loss，中间 stage 只有激活值。`get_forward_backward_func` 把 `forward_step` 在各 stage 执行，前向传到最后一 stage 时才调用返回的 `loss_func` 算 loss 并开始反向。若没有 PP（PP=1），则单一 stage 自己调 `loss_func`，逻辑一致。

**练习 2**：`loss_func` 返回的 `loss` 张量长度是 2（`[sum(losses*mask), mask.sum()]`），而不是一个标量，为什么？

> **答案**：因为要算 **per-token 平均 loss**。第一个元素是有效 token 的 loss 总和，第二个是有效 token 数，二者相除（在日志里 `[0]/[1]`，见 [base.py:181-183](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L181-L183)）才是平均 loss。分开传输是为了让 DP 组的 all-reduce 能同时聚合「总 loss」和「总 token 数」，避免不同 rank 样本数不同造成的平均偏差。

**练习 3**：开 VPP（`--virtual_pipeline_model_parallel_size 2`）后，`setup_training` 为什么要为每个 vp_stage 各准备一份数据迭代器？

> **答案**：VPP（interleaved pipeline）把每个 PP stage 再切成多个 chunk 交叉调度，不同 chunk 在同一时刻处理的是**不同的 micro-batch**。为了让每个 chunk 都能独立取到数据，需要为每个 vp_stage 准备一份独立的数据迭代器（见 [base.py:657-662](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/trainers/base.py#L657-L662)）。不开 VPP 时所有 micro-batch 共用一份迭代器即可。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「**读懂并行配置 → 跑通训练 → 对比后端**」的小任务：

**任务**：为团队写一份「Megatron-SWIFT 2 卡训练速查表」，要求：

1. **并行算力表**：给定 2 卡、`--tensor_model_parallel_size 2 --sequence_parallel true`，用本讲公式（[megatron_args.py:921-937](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L921-L937)）填出下表，全部手算并写明过程：

   | 配置 | TP | PP | CP | EP | world_size | data_parallel_size | num_microbatches（mbs=16, gbs=16） |
   | --- | --- | --- | --- | --- | --- | --- | --- |

2. **执行链追踪**：画出从 `megatron sft` 命令到 `MegatronTrainer.train_step` 内 `forward_backward_func` 的调用链，标注每一跳所在文件（参考 4.2.2 的流程图与 4.3.3 的源码引用）。

3. **实测对比**：在本地跑 `examples/megatron/lora/dense.sh`（Megatron 后端）与等价的 `swift sft`（DeepSpeed zero2 后端），各记录 `s/it` 与峰值显存，填入下表并写一段结论说明「哪种后端在什么场景更快」。

   | 后端 | s/it | 峰值显存 | 备注 |
   | --- | --- | --- | --- |

**验证方式**：把速查表交给一位没读过 megatron 源码的同学，让他据此回答「2 卡训练 7B 模型，TP 该设几？为什么不设 PP？SP 能不能单独开？」。如果他答出「TP=2、PP=1（层数少不值得切）、SP 不能单独开必须配 TP」，说明你的速查表抓住了本讲核心。

**待本地验证**：第 3 步的 `s/it` 与显存数值强依赖硬件，请以本地实测为准；若无可用的多卡 GPU 环境，第 3 步可降级为「阅读 dense.sh 与 moe.sh 的注释，摘录官方给出的显存/速度量级」。

## 6. 本讲小结

- `MegatronArguments` 把 TP/PP/SP/CP/EP/VPP 六种并行参数暴露为 dataclass 字段（[megatron_args.py:588-614](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L588-L614)）；用户只指定这六个，DP 由 `world_size // (TP×PP×CP)` 算出，`num_microbatches`（梯度累加）由 `global_batch_size // (micro_batch_size × DP)` 算出，且后者必须整除否则报错。
- SP（mcore 原生序列并行）依赖 TP，TP≤1 时被静默关闭（[megatron_args.py:886-887](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L886-L887)）；它与 `u9-l2` 普通 swift 的 `sequence_parallel_size`（映射到 CP）同名但机制不同；VPP 有若干派生约束（不开时关闭 P2P 重叠等）。
- `megatron_sft_main` → `MegatronSft(args).main()`，`MegatronSft` 继承普通 `SwiftSft`，只覆写 `__init__/run/prepare_trainer` 等钩子——「覆写钩子而非重写骨架」；`__init__` 用 `torch.device('meta')` 建空壳模型，真实 mcore 模型在 `MegatronTrainer.prepare_model` 才建。
- `prepare_trainer` 按 `task_type` 派发到 `MegatronTrainer`/`MegatronEmbeddingTrainer`/`MegatronRerankerTrainer`；`run()` 顺序为 `_prepare_dataset` → `init_iters` → `prepare_trainer` → `trainer.train`，收尾在 last_rank 画图、写 logging.jsonl、销毁进程组。
- 六种并行靠 `mpu.initialize_model_parallel`（[megatron_lm_utils.py:71-78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py#L71-L78)）划分进程组，模型的算子切分由 mcore+TE 实现，对训练器透明；训练循环用 `get_forward_backward_func()` 选流水调度器，按 `num_microbatches` 灌 micro-batch。
- 流水并行的精髓是 `forward_step` 返回 `(output_tensor, loss_func)`——loss 只在最后一个 stage 算；`loss_func` 返回 `[总loss, 有效token数]` 两元组以支持 per-token 平均与 DP 组精确归约；VPP 时每个 vp_stage 各一份数据迭代器。

## 7. 下一步学习建议

- **Megatron 上的强化学习**：本讲的 `MegatronTrainer` 是基础，下一篇可进入 `u7-l1/u7-l2` 在 Megatron 后端的落地——阅读 [swift/megatron/pipelines/train/rlhf.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/train/rlhf.py) 与 [examples/megatron/grpo/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/grpo)，看 GRPO 如何在 TP/PP 之上叠加 vLLM rollout 与权重热同步。
- **跨机训练**：本讲示例都在单机多卡，跨机需用 `NNODES/MASTER_ADDR/NODE_RANK`（参考 [examples/megatron/multi-node/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/multi-node)），并务必设置共享的 `MODELSCOPE_CACHE` 以避免数据预处理不一致导致挂起。
- **Ray 调度**：若想了解跨机/异步 RL 如何用 Ray 编排 Megatron，可衔接 `u9-l5` 的 `RayHelper`，阅读 [swift/ray_utils/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ray_utils/base.py) 与 `MegatronArguments` 的 `use_ray` 字段。
- **官方文档**：[docs/source_en/Megatron-SWIFT/Command-line-parameters.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Megatron-SWIFT/Command-line-parameters.md) 给出了全部 Megatron 参数的完整说明，是本讲字段速查的权威补充。
