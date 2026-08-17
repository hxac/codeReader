# u3-l1 BaseTrainer 初始化：模型构建、冻结权重与 FSDP 包装

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出 `BaseTrainer.__init__` 里发生的每一件事：分布式初始化 → 断点发现 → 模型构建 → 断点权重恢复 → `torch.compile` → FSDP 包装 → 数据集校验 → 训练日程计算 → 优化器构建 → 断点训练状态恢复。
2. 解释为什么草稿模型的 `embed_tokens` 和 `lm_head` 直接从目标模型拷贝权重并**冻结**，而不是从头训练。
3. 独立推导 `gradient_accumulation_steps`、`samples_per_epoch`、`steps_per_epoch`、`max_train_steps` 的计算公式，并手工算出具体数值。
4. 理解 `Qwen3DSparkTrainer` 如何通过「模板方法」继承 `BaseTrainer`，只实现 `_build_draft_model` 和 `run_batch` 两个钩子。

本讲是第 3 单元（训练框架）的第一讲。我们只关心**训练开始之前发生了什么**；`train()` 主循环留到 u3-l2。

## 2. 前置知识

本讲默认你已读过：

- **u1-l4 配置系统**：`train.py` 把配置文件加载成 `ConfigNode`，所以 `self.args.train.lr` 这种属性访问其实就是读配置字典；配置里甚至直接存放 Python 类（如 `trainer_cls=Qwen3DSparkTrainer`）。
- **u1-l3 入口与分布式**：每个 GPU 一个 spawn 子进程，各自构造一个 Trainer；`init_dist` 用 `rank = node_rank × local_world_size + local_rank` 推导全局 rank。
- **u2-l6 训练侧读取**：训练数据不是 JSONL，而是落盘的**目标缓存**（target cache），由 `CacheDataset` 通过 mmap 读取。

还需要两个本讲新用到的基础概念：

- **FSDP（Fully Sharded Data Parallel）**：PyTorch 的分布式训练包装器。普通 DDP 在每张卡上都放一份完整模型副本，梯度同步后各自更新；FSDP 可以把**模型参数、梯度、优化器状态**切成碎片分散到各卡，用时再临时聚合，从而训练单卡放不下的模型。它还提供 `no_sync()`（梯度累积时跳过同步）等工具。本仓库把 FSDP 当作统一的分布式外壳使用，默认配置其实选了 `no_shard`（见 4.1.3）。
- **混合精度与主权重（master weights）**：模型用 bf16 计算速度快、省显存，但 bf16 精度低，直接用它累积参数更新会损失精度。常见做法是另外维护一份 float32 的「主权重」，优化器在主权重上做更新，再拷回 bf16 模型。本仓库的 `BF16Optimizer` 就是这个模式的实现（u3-l4 详讲）。

一个帮助理解的比喻：`__init__` 像是「开机自检」——它把训练所需的一切（卡、模型、数据、日程、优化器、上次进度）都准备好并互相校验，任何一个环节对不上就立刻 `assert` 失败，绝不让错误配置 silently 跑起来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 训练框架骨架：`BaseTrainer` 类、FSDP 包装辅助函数、训练日程计算函数。本讲主战场 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | `Qwen3DSparkTrainer` / `Gemma4DSparkTrainer`：继承 BaseTrainer，只填两个算法钩子 |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | 草稿模型本体，本讲只看 `initialize_embeddings_and_head`（冻结入口） |
| [deepspec/utils/optim.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py) | `BF16Optimizer`：只收集 `requires_grad=True` 的参数（与冻结直接相关） |
| [deepspec/data/target_cache_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py) | `validate_train_cache`：数据集与模型的「合同校验」 |
| [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py) | `init_dist`：rank/world_size 推导（u1-l3 已讲，本讲只引用） |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | `discover_latest_checkpoint`：找 `step_latest` 符号链接（u3-l5 详讲） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 官方 DSpark×Qwen3-4B 训练配置，本讲的数值示例来源 |

## 4. 核心概念与源码讲解

### 4.1 模块一：BaseTrainer.__init__ 流程

#### 4.1.1 概念说明

`BaseTrainer` 是所有算法训练器的公共骨架。DSpark、Eagle3 各自的训练器都继承它，**共享全部工程逻辑**（分布式、优化器、断点、日志），只覆盖少数算法相关的钩子（`_build_draft_model`、`run_batch`）。这是典型的**模板方法模式**：父类定流程，子类填内容。

`__init__` 的职责是把「一次训练」需要的所有静态资源装配到位。它做的事可以概括为四组：

1. **环境**：初始化分布式、精度、目录、日志；
2. **模型**：构建草稿模型（+冻结）、恢复权重、compile、FSDP 包装；
3. **数据**：打开 target cache 并校验一致性；
4. **日程与优化器**：算训练步数、建优化器、（若有断点）恢复训练进度。

#### 4.1.2 核心流程

`__init__`（[base_trainer.py:158-234](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L234)）的执行顺序伪代码：

```text
__init__(local_rank, args):
    # ── 第一组：环境 ──
    device, global_rank, world_size = init_dist(local_rank)   # NCCL 进程组
    precision_dtype   = {bf16|fp16|fp32}[args.train.precision]
    resume_ckpt_dir   = discover_latest_checkpoint(ckpt_root) # 找 step_latest 符号链接
    suspend_controller = SuspendController(device)            # 挂起信号（u3-l2）
    next_micro_step   = 0                                     # 训练进度的唯一真相源
    全局主进程: ensure_dir(ckpt_root)
    training_logger.init(...)

    # ── 第二组：模型 ──
    draft_model, tokenizer = build_models()        # 构建草稿模型 + 冻结 embed/lm_head（模块二）
    if resume_ckpt_dir:                            # 有断点 → 先把权重装进「裸」模型
        draft_model = load_resume_draft_model(...)
    model = draft_model
    if args.train.torch_compile:
        model = torch.compile(model, dynamic=True) # 先 compile
    model = FSDP(model, ...)                       # 后 FSDP 包装

    # ── 第三组：数据 ──
    train_dataset = CacheDataset(cache_dir=args.data.target_cache_path)
    validate_train_cache(train_dataset, draft_model, target_model_name_or_path)

    # ── 第四组：日程与优化器 ──
    (grad_accum_steps, samples_per_epoch, per_rank_samples, micro_batches,
     steps_per_epoch, max_train_steps, num_train_epochs) = _compute_training_schedule(...)
    optimizer = BF16Optimizer(draft_model, lr, total_steps, warmup_ratio, weight_decay)
    if resume_ckpt_dir:                            # 再恢复优化器/采样器等训练状态
        next_micro_step = load_training_state(...).next_micro_step
    info_board()                                   # 打印训练信息板
```

四个值得注意的**顺序设计**：

- **权重恢复（L176-183）发生在 compile 与 FSDP 之前**：`load_resume_draft_model` 面对的是未包装的原始模块，state dict 键名就是模型本来的键名，装完再统一包装，避免处理 FSDP 分片 state dict 的复杂度。
- **先 `torch.compile` 再 FSDP**（L185-188）：compile 作用在裸模块上，FSDP 包在最外层。这个顺序是 PyTorch 官方推荐的组合方式之一，能减少 FSDP 与编译图之间的干扰。
- **优化器建在 `self.draft_model`（未包装对象）而非 `self.model`（FSDP 壳）上**（L214-215）：因为 FSDP 用了 `use_orig_params=True`（见 4.1.3），参数对象在包装前后是同一批，梯度会原地写回这些参数，所以优化器拿裸对象的参数列表完全等价，还避开了「FSDP 壳上取参数」的坑。
- **两次断点恢复是分开的**：模型权重在包装前恢复，而 `next_micro_step`、优化器状态、采样器偏移在优化器建好之后恢复（L221-231），因为后者依赖前者的产物。

#### 4.1.3 源码精读

**① 环境初始化与断点发现**

[base_trainer.py:158-173](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L173)：构造函数开头先调 `init_dist(local_rank)` 拿到 `device / global_rank / world_size` 三个贯穿全程的量；然后从 `args.logging.checkpoint_dir`（由 u1-l4 讲过的 `finalize_cfg` 派生）发现旧断点；`self.next_micro_step = 0` 是训练进度的起点，后面每个 micro batch 加一。

`init_dist` 的实现在 [distributed.py:11-31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L11-L31)：`rank = node_rank × local_world_size + local_rank`，`world_size = node_world_size × local_world_size`，backend 为 NCCL（u1-l3 已详解）。

`discover_latest_checkpoint` 在 [ckpt_manager.py:25-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L25-L29)：只认 `checkpoint_dir/step_latest` 这个符号链接，返回它指向的真实目录；没有就返回 `None`，本次从头训。

**② 模型构建 → 恢复 → compile → FSDP 的四连**

[base_trainer.py:175-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L175-L188)：`build_models()`（模块二）返回草稿模型和 tokenizer；若有断点先把权重装进裸模型；随后 `torch.compile(self.model, dynamic=True)`（`dynamic=True` 让编译图适应变长序列，避免每个长度都触发重编译）；最后 `_wrap_with_fsdp` 完成分布式包装。

**③ FSDP 包装的关键字**

[base_trainer.py:287-293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L287-L293) 调用 [base_trainer.py:55-74](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L55-L74) 构造的 kwargs：

- `use_orig_params=True`：FSDP 包装后**保留原始参数对象**（而不是换成扁平化大 tensor）。这正是「优化器建在 `draft_model` 上也能工作」的前提，也兼容 `torch.compile`。
- `mixed_precision`：参数与 buffer 都转成 `args.train.precision` 指定的 dtype（映射表见 [base_trainer.py:34-38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L34-L38)）。
- `sharding_strategy`：由配置字符串查表（[base_trainer.py:40-47](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L40-L47)），支持 `full_shard / shard_grad_op / no_shard / hybrid_shard / hybrid_shard_zero2`；选择 hybrid 系策略时会额外构建 `(replicate, shard)` 二维 device mesh（[base_trainer.py:67-73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L67-L73)）。

注意：官方配置 [config/dspark/dspark_qwen3_4b.py:43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L43) 默认 `sharding_strategy="no_shard"`。也就是说默认配置下 FSDP 不切分参数，只充当统一的分布式外壳（提供 `no_sync`、`clip_grad_norm_` 等工具）。一个合理的推断是：草稿主干只有 5 层，模型很小，切分的通信开销可能得不偿失——此为源码阅读推断，仓库未直接说明。

**④ 数据集校验**

[base_trainer.py:190-195](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L190-L195)：用 u2-l6 讲过的 `CacheDataset` 打开缓存目录，然后 `validate_train_cache` 做三项「合同校验」，实现于 [target_cache_dataset.py:203-218](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L203-L218)：

| 校验项 | 含义 |
| --- | --- |
| `target_layer_ids` 一致 | 缓存里存的 K 层隐状态，必须恰好是草稿 config 声明要用的那几层 |
| `hidden_size` 一致 | 缓存每层宽度必须等于草稿模型 `config.hidden_size` |
| `target_model_name_or_path` 一致 | 缓存必须由**本次训练指定的同一个目标模型**生成（字符串级比对） |

这是 u2-l4「manifest 是元数据合同」在训练侧的兑现：缓存和模型对不上就立刻失败，而不是跑出莫名其妙的 loss。

**⑤ 子类如何接入：dspark_trainer.py**

[dspark_trainer.py:14-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L14-L39)：`Qwen3DSparkTrainer` 只做了三件事——声明 `data_collator_cls = CacheCollator`（供 `_build_train_dataloader` 使用）；实现 `_build_draft_model`（用 `build_qwen3_draft_config` 从目标 config 派生草稿 config，再实例化 `Qwen3DSparkModel`）；实现 `run_batch`（一次前向 + `compute_dspark_loss`，属于 u4 的内容）。

[dspark_trainer.py:42-48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L42-L48)：`Gemma4DSparkTrainer` 更薄，只换了 `_build_draft_model` 里的模型类和 config 构造器，其余（包括 `run_batch`）全部继承自 Qwen3 版。这就是「一套 BaseTrainer 骨架服务多个模型族」的实物证据。

#### 4.1.4 代码实践

**实践目标**：不看讲义，只靠读源码还原 `__init__` 的装配顺序，并理解每一步依赖谁。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [base_trainer.py:158-234](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L234)，给每一行语句标注它属于「环境 / 模型 / 数据 / 日程与优化器」哪一组。
2. 画出依赖箭头：例如 `optimizer` ← `max_train_steps` ← `_compute_training_schedule` ← `len(train_dataset)` ← `CacheDataset` ← `args.data.target_cache_path`。
3. 回答三个问题（答案见 4.1.5）：为什么权重恢复在 FSDP 包装之前？为什么优化器建在 `draft_model` 而不是 FSDP 壳上？`validate_train_cache` 若被删除，最早什么时候会暴露错误？

**需要观察的现象 / 预期结果**：你会得到一张 10 余个节点、按拓扑序排列的装配图；图中没有任何环——`__init__` 是严格的线性装配流水线，这也是它容易断点续训的原因（按相反顺序拆即可）。「`validate_train_cache` 删除后错误何时暴露」——预期答案是：要等到第一个 batch 真正前向时才会因张量形状/语义不匹配报错（甚至更糟，shape 碰巧一致时静默输出错误 loss），这正是该校验存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `torch.compile` 放到 FSDP 包装**之后**（即 `compile(FSDP(model))`），与本仓库顺序相比可能有什么不同？

**答案**：本仓库是 `FSDP(torch.compile(model))`——编译发生在裸模块上，编译图针对模型本身，FSDP 在外层做参数聚合与梯度同步。反过来 `torch.compile(FSDP(...))` 会把 FSDP 的通信与 reshape 一并编进图，图更复杂、更容易触发重编译或与 `use_orig_params` 的参数视图行为相互干扰。PyTorch 官方对二者组合给出的可行顺序正是「先 compile 后 FSDP」。（基于源码顺序与 PyTorch 文档的推断，具体行为版本相关。）

**练习 2**：`__init__` 里 `info_board()` 用的是 `print_on_local_main`（[base_trainer.py:240-249](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L240-L249)），每个节点各打印一份。这和 `print_on_global_main` 有什么取舍？

**答案**：`print_on_local_main` 判据是 `torch.cuda.current_device() == 0`（[distributed.py:38-40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L38-L40)），多机时每台机器的 0 卡进程都会打印。信息板内容（数据条数、步数）理论上各节点一致，多打几份是冗余；但它的好处是能顺带确认**每个节点**都走到了装配终点、参数一致——若某台机器没打印，说明它卡在前面。这是以冗余换可观测性的选择。

### 4.2 模块二：build_models 冻结 embed/lm_head

#### 4.2.1 概念说明

`build_models()` 要回答的问题是：**草稿模型的词嵌入（input embedding）和输出投影（lm_head）从哪来？**

答案非常果断：直接从目标模型拷贝权重，然后**冻结**（`requires_grad=False`）。理由有三层：

1. **语义必须对齐**。投机解码中草稿和目标共用同一个词表。草稿模型读的是 token id（经 `embed_tokens` 变成向量）、输出的是词表上的 logits（经 `lm_head` 投影）。如果这两块和目标模型不一致，草稿学到的分布就没有意义。锁定它们，等于把「输入输出接口」钉死，训练只需专注学中间的小主干。
2. **这两块非常大**。以 Qwen3-4B 为例，`hidden_size=2560`、词表 151936：`embed_tokens` 约 \(2560 \times 151936 \approx 3.89 \times 10^8 \) 个参数，`lm_head`（Qwen3 不绑定权重）又是一个同量级矩阵——两者合计约 7.8 亿参数，比 5 层草稿主干本身大得多。冻结它们后，`BF16Optimizer` 里 `[p for p in model.parameters() if p.requires_grad]`（[optim.py:93](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L93)）会直接跳过这 7.8 亿参数：**不需要 fp32 主权重、不需要 AdamW 的一阶/二阶动量**，省下数 GB 显存和大量通信。
3. **它们本来就「是对的」**。目标模型经过完整训练，其嵌入空间质量远高于随机初始化；草稿训练的目标是「逼近目标分布」，接口直接复用目标是最强先验。

一个类比：目标模型是一台成熟的翻译机，草稿模型是给它配的「预判小助手」。小助手不需要重新发明字母表（embedding）和词典（lm_head），直接借用翻译机的，只需学会「在翻译机思考到一半时猜它接下来要查哪个词」。

#### 4.2.2 核心流程

`build_models()`（[base_trainer.py:251-282](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L251-L282)）：

```text
build_models():
    tokenizer     = AutoTokenizer.from_pretrained(目标模型路径)      # 分词器与目标一致
    target_config = AutoConfig.from_pretrained(目标模型路径)
    draft_model   = _build_draft_model(target_config, model_args)   # 子类钩子：派生 config + 建模型
    draft_model.to(device, precision_dtype)

    # 只为了拿嵌入和输出投影，把整个目标模型搬到 CPU：
    target_model = AutoModelForCausalLM.from_pretrained(路径, dtype=precision)
                   .to("cpu").eval()
    embed = target_model.get_input_embeddings()
    lm_head = target_model.get_output_embeddings()
    assert 两者非 None
    draft_model.initialize_embeddings_and_head(embed, lm_head, freeze=True)
    del target_model    # 立刻释放
    return draft_model, tokenizer
```

注意三个细节：

- 目标模型放在 **CPU** 上加载（`.to(device="cpu")`），不占用宝贵的 GPU 显存；拷完权重立即 `del`。
- 目标模型被切成 `.eval()` 模式——虽然只读权重不前向，但这是防御性写法。
- 注释（[base_trainer.py:267-268](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L267-L268)）明确说明：训练阶段加载目标 checkpoint **仅用于**初始化冻结的 embed/lm_head，不做其他事。

#### 4.2.3 源码精读

**① 冻结的落点：initialize_embeddings_and_head**

[modeling.py:270-283](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L270-L283)（Qwen3 DSpark 版；gemma4、eagle3 各自也有同签名实现）：

```python
def initialize_embeddings_and_head(self, *, embed_tokens, lm_head, freeze=True):
    assert self.embed_tokens.weight.shape == embed_tokens.weight.shape   # 形状必须完全一致
    assert self.lm_head.weight.shape == lm_head.weight.shape
    with torch.no_grad():                        # 纯拷贝，不记梯度
        self.embed_tokens.weight.copy_(embed_tokens.weight.detach())
        self.lm_head.weight.copy_(lm_head.weight.detach())
    if freeze:
        self.set_embedding_head_trainable(False)
```

`set_embedding_head_trainable(False)`（[modeling.py:285-287](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L285-L287)）就是对两个模块调 `requires_grad_(False)`。两行 `assert` 保证草稿与目标的词表、hidden 宽度逐位一致——这是「接口钉死」的硬约束。

**② 冻结如何传导到优化器**

`BF16Optimizer.__init__`（[optim.py:84-106](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/optim.py#L84-L106)）第一行就按 `requires_grad` 过滤：

```python
self.model_params = [p for p in model.parameters() if p.requires_grad]   # 冻结参数被排除
self.fp32_params  = [p.detach().clone().to(torch.float32) for p in self.model_params]  # 只为主干建 fp32 主权重
self.optimizer    = torch.optim.AdamW(self.fp32_params, lr=lr, weight_decay=weight_decay)
```

于是被冻结的 embed/lm_head：不进 AdamW、不占 fp32 主权重和动量、反向时也不产生梯度（`requires_grad=False` 的子图直接被 autograd 剪枝）。

**③ 冻结与 FSDP 的配合**

冻结发生在 `build_models` 阶段（FSDP 包装之前），所以 FSDP 看到的模型里这两块参数已经带 `requires_grad=False`；`use_orig_params=True` 又保证这些参数对象全程不变。默认 `no_shard` 策略下参数本就不切分；即便切分，无梯度参数同样省去梯度分片的通信。

#### 4.2.4 代码实践

**实践目标**：定量感受「冻结省了多少参数与显存」。

**操作步骤**（纸笔 + Python，无需下载模型）：

1. 读 [config/dspark/dspark_qwen3_4b.py:10-30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30)，确认 `num_draft_layers=5`。
2. 用 Qwen3-4B 的公开规格（`hidden_size=2560`，词表 151936）手算：
   - 冻结参数量 = \(2 \times 2560 \times 151936\)；
   - 估算若**不**冻结，AdamW 额外要为它们存多少状态（fp32 主权重 + 两个 fp32 动量 ≈ 12 bytes/参数）。
3. 用下面的脚本复算（示例代码）：

```python
# 示例代码：估算冻结 embed/lm_head 的收益
hidden, vocab = 2560, 151936
frozen = 2 * hidden * vocab
print(f"冻结参数量: {frozen/1e6:.1f}M")            # 预期约 778M
print(f"若不冻结, AdamW 额外状态 ≈ {frozen*12/1e9:.2f} GB (fp32)")
```

**需要观察的现象 / 预期结果**：冻结参数约 **778M**；若不冻结，仅这两块的优化器状态就要约 **9.3 GB** 显存——比 5 层草稿主干的参数本身还大。跑通脚本后对照数值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build_models` 里要 `assert (target_lm_head is not None) and (target_embed_tokens is not None)`？什么模型会触发 None？

**答案**：`AutoModelForCausalLM.get_output_embeddings()` 对**权重绑定**（tied embeddings，即 lm_head 与 embed_tokens 共享一张矩阵）或结构特殊的模型可能返回 `None`。DeepSpec 的草稿模型独立持有 `self.lm_head`（见 [modeling.py:246](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L246) 单独构造了 `nn.Linear`），需要显式的 lm_head 权重来拷贝；目标模型拿不到就拷不了，所以在装配最早处 assert 失败，避免后面 `copy_` 到 None 崩溃。

**练习 2**：如果把 `freeze=True` 改成 `False`，训练还能跑吗？会发生什么？

**答案**：能跑，语义也大体不变（初始权重仍来自目标模型），但代价立涨：`BF16Optimizer` 会把约 778M 参数纳入优化，多出约 9 GB 级的 fp32 主权重 + AdamW 动量，反向图变大、通信变多；而且让嵌入和输出投影漂移，可能反而破坏「与目标共用接口」的对齐假设。仓库选择冻结是精度先验 + 工程收益的双赢。

**练习 3**：`initialize_embeddings_and_head` 里为什么用 `copy_` 而不是直接把目标模型的 Module 对象赋给草稿模型？

**答案**：`copy_` 是**把数据拷进草稿模型自己持有的 Parameter**，草稿仍是独立的模块树（后续 `del target_model` 也不影响它）；直接赋 Module 对象则会共享存储，目标模型释放后草稿参数悬空，且 FSDP/compile 对参数归属的假设也会被打破。`detach()` 进一步保证拷贝来源不带计算图。

### 4.3 模块三：_compute_training_schedule

#### 4.3.1 概念说明

训练日程（schedule）要回答：**一个 epoch 有多少步？总共训练多少步？梯度累积多少次才算一个优化器步？**

DeepSpec 用两级 batch 词汇：

- `local_batch_size`：每个进程（每张卡）一次 micro batch 吃几条样本。target cache 样本是「整个对话的 K 层隐状态」，单条就非常大（这就是默认 `local_batch_size=1` 的原因，见 [config/dspark/dspark_qwen3_4b.py:38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L38)）。
- `global_batch_size`：一次**优化器步**（参数真正更新一次）所消费的全局样本数，是实验语义上的「有效 batch」。

两者之间的桥就是**梯度累积**：每卡各自连做若干次前向反向（用 `no_sync` 攒梯度），攒够次数再统一同步并 `optimizer.step()`。

#### 4.3.2 核心流程

三个纯函数（[base_trainer.py:77-135](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L77-L135)）层层递进：

**① 梯度累积步数**（[base_trainer.py:77-86](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L77-L86)）：

\[ \text{grad\_accum} = \frac{B_{global}}{W \times B_{local}} \]

其中 \(W\) 是 world_size。断言 \(B_{global} \bmod (W \times B_{local}) = 0\)——除不尽说明有效 batch 拼不出来，立即失败。

**② 每 epoch 样本数**（[base_trainer.py:89-95](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L89-L95)）：

\[ \text{samples\_per\_epoch} = \left\lfloor \frac{N}{B_{global}} \right\rfloor \cdot B_{global} \]

即把数据集**向下取整到 global batch 的整数倍**，尾部不足一批的样本直接丢弃（保证每个 epoch 恰好由整数个全局 batch 组成，各卡样本数也整除）。断言结果大于 0，防数据集太小。

**③ 汇总**（[base_trainer.py:98-135](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L98-L135)）：

```text
per_rank_samples_per_epoch = samples_per_epoch // world_size        # 每卡每 epoch 样本数
micro_batches_per_epoch    = per_rank_samples // local_batch_size   # 每卡每 epoch micro batch 数
steps_per_epoch            = micro_batches // grad_accum           # 每 epoch 优化器步数
```

三者相乘可验证恒等式：

\[ \text{steps\_per\_epoch} = \frac{\text{samples\_per\_epoch}}{B_{global}} \]

**总步数**的双向规则（[base_trainer.py:119-126](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L119-L126)）：默认 `max_train_steps=None` 时由 epoch 数推出总步数；若显式给了 `max_train_steps`，则反推 `num_train_epochs = ceil(max_steps / steps_per_epoch)`（epochs 退化为展示值，步数才是硬约束）。

返回的七元组被 `__init__` 一次解包（[base_trainer.py:197-212](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L197-L212)），其中 `max_train_steps` 随即喂给 `BF16Optimizer` 作为学习率调度的 `total_steps`——所以**日程必须先于优化器计算**，这解释了 `__init__` 里两者的先后顺序。

#### 4.3.3 源码精读

以官方默认配置（[config/dspark/dspark_qwen3_4b.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45)：`local_batch_size=1`、`global_batch_size=512`、`num_train_epochs=10`、`max_train_steps=None`）代入，假设单机 8 卡、缓存里 10 万条样本：

| 量 | 计算 | 结果 |
| --- | --- | --- |
| `gradient_accumulation_steps` | 512 ÷ (8 × 1) | **64** |
| `samples_per_epoch` | ⌊100000 ÷ 512⌋ × 512 = 195 × 512 | **99840**（丢弃尾部 160 条） |
| `per_rank_samples_per_epoch` | 99840 ÷ 8 | **12480** |
| `micro_batches_per_epoch` | 12480 ÷ 1 | **12480** |
| `steps_per_epoch` | 12480 ÷ 64 | **195** |
| `max_train_steps` | 10 × 195 | **1950** |

也就是说：**每张卡连续做 64 次 micro batch 的前向反向，才触发一次参数更新**；每个 epoch 全局消费 99840 条样本（10 个 epoch 共 998400 条），正好 195 个完整全局批。

这个 `steps_per_epoch=195` 会进一步流向 u3-l2 的主循环：`total_micro_steps = max_train_steps × gradient_accumulation_steps`（[base_trainer.py:361](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L361)），而断点续训的采样器偏移由 `next_micro_step × local_batch_size` 直接算出（[base_trainer.py:365-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L365-L368)）——日程是进度换算的基准系。

#### 4.3.4 代码实践

**实践目标**：手工推导训练日程，再用 Python 复算验证（本讲规定的核心实践）。

**操作步骤**：

1. **手算**：world_size=8、local_batch_size=1、global_batch_size=512、dataset_size=100000、num_train_epochs=10、max_train_steps=None，按 4.3.2 的公式逐步写出梯度累积步数、每 epoch 样本数、每 epoch 步数、总步数。
2. **复算**：运行下面脚本（示例代码，逐行复刻 [base_trainer.py:77-135](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L77-L135) 的逻辑）：

```python
# 示例代码：复算 _compute_training_schedule
import math

def compute_schedule(*, world_size, dataset_size, local_batch_size,
                     global_batch_size, num_train_epochs, max_train_steps=None):
    denom = world_size * local_batch_size
    assert global_batch_size % denom == 0, "global_batch_size 必须能被 world_size*local_batch_size 整除"
    grad_accum = global_batch_size // denom
    samples_per_epoch = (dataset_size // global_batch_size) * global_batch_size
    assert samples_per_epoch > 0, "数据集凑不出一个完整全局批"
    per_rank = samples_per_epoch // world_size
    micro_batches = per_rank // local_batch_size
    steps_per_epoch = micro_batches // grad_accum
    if max_train_steps is None:
        max_steps, epochs = num_train_epochs * steps_per_epoch, num_train_epochs
    else:
        max_steps, epochs = max_train_steps, math.ceil(max_train_steps / steps_per_epoch)
    return grad_accum, samples_per_epoch, per_rank, micro_batches, steps_per_epoch, max_steps, epochs

print(compute_schedule(world_size=8, dataset_size=100000, local_batch_size=1,
                       global_batch_size=512, num_train_epochs=10))
```

3. **对照**：脚本输出应与你手算的 4.3.3 表格一致。
4. **扩展**：再算两组边界情形——(a) `global_batch_size=500`；(b) `dataset_size=300`；(c) `max_train_steps=1000`（其余同题设）。

**需要观察的现象 / 预期结果**：

- 主例输出 `(64, 99840, 12480, 12480, 195, 1950, 10)`，与手算一致；
- (a) 500 % (8×1) = 4 ≠ 0，触发整除断言，报错信息正是 [base_trainer.py:81-85](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L81-L85) 的文案；
- (b) ⌊300/512⌋ = 0 → `samples_per_epoch=0`，触发「数据集太小」断言（[base_trainer.py:91-94](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L91-L94)）；
- (c) `max_train_steps=1000` 生效，`num_train_epochs` 被反推为 `ceil(1000/195) = 6`。

若你手算与脚本不一致，优先检查是否忘了「向下取整到全局批整数倍」这一步。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `samples_per_epoch` 要向下取整到 `global_batch_size` 的整数倍，而不是像普通 DataLoader 那样保留不满一批的尾部？

**答案**：分布式训练要求每个优化器步各卡消费**完全相同数量**的样本。若保留尾部，最后一个批各卡样本数不齐，要么有的卡空转（引入同步死等），要么需要复杂的填充逻辑。取整丢尾是「丢弃 ≤ global_batch_size-1 条样本」换「每卡每 epoch 样本数、micro batch 数严格整除」的干净做法；100000 条只丢 160 条（0.16%），代价极小。

**练习 2**：同一份缓存，训练中途从 8 卡扩到 16 卡恢复训练，日程会发生什么变化？安全吗？

**答案**：`grad_accum` 变为 512÷16=32，`per_rank_samples_per_epoch` 变为 6240，`micro_batches_per_epoch` 变为 6240，`steps_per_epoch` 仍为 195，`max_train_steps` 不变。每卡的样本流和断点偏移换算（`next_micro_step × local_batch_size` 是每卡样本数）会按新 world_size 解释，`load_training_state` 传入了当前的 `local_batch_size / grad_accum / micro_batches` 正是为了做这类换算（[base_trainer.py:221-230](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L221-L230)）。但已训练的 `next_micro_step` 对应的全局样本进度换算是否严丝合缝，取决于 ckpt_manager 的恢复逻辑——该细节留待 u3-l5 检验，此处标注「待确认」。

**练习 3**：`info_board` 打印 `Gradient accumulation steps = 64`、`Steps per epoch = 195`。若你在日志里看到某个 epoch 实际执行了 196 个优化器步，最可能哪里出了问题？

**答案**：按本模块的推导这不可能发生——`steps_per_epoch = micro_batches_per_epoch // grad_accum` 保证整除，尾批已在 `samples_per_epoch` 取整时丢弃，且 DataLoader `drop_last=True`（[base_trainer.py:311](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L311)）再次兜底。若真出现，应怀疑：观测脚本把跨 epoch 的步数计到了一起、或断点恢复后 `next_micro_step` 未对齐 `grad_accum` 边界。排查入口是 `global_step = next_micro_step // grad_accum`（[base_trainer.py:236-238](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L236-L238)）。

## 5. 综合实践

**任务：为一次「假想的 DSpark×Qwen3-4B 训练」写一份启动评审表**（全程纸笔 + 本地 Python，无需 GPU 和真实缓存）。

假设你拿到如下运行计划，需要在上真机前评审它：

- 机器：2 节点 × 8 卡（world_size=16）；缓存样本数 N = 250000；
- 覆盖参数（--opts 语法见 u1-l4）：`train.local_batch_size=2`、`train.global_batch_size=512`、`train.num_train_epochs=3`、`train.max_train_steps=None`。

要求完成三件事：

1. **日程推演**：手算 `grad_accum / samples_per_epoch / steps_per_epoch / max_train_steps`，再用 4.3.4 的 `compute_schedule` 脚本以 `world_size=16` 复算验证；并回答：每卡每个 epoch 实际消费多少条样本？
2. **显存预算**：按 4.2.4 的方法计算冻结 embed/lm_head 省下的优化器状态显存；结合 `sharding_strategy="no_shard"`（每卡一份完整模型）说明这份节省在每张卡上都成立。
3. **装配顺序走查**：对照 [base_trainer.py:158-234](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L234) 写出这次启动的时序清单：`init_dist`（16 进程各一次）→ `discover_latest_checkpoint`（首次为 None）→ `build_models`（16 进程各自从 CPU 加载目标模型拷权重再释放）→ `validate_train_cache`（校验三条合同）→ `_compute_training_schedule` → `BF16Optimizer` → `info_board`。标注每一步读配置的哪个字段。

**参考答案要点**：

1. `grad_accum = 512 ÷ (16×2) = 16`；`samples_per_epoch = ⌊250000/512⌋×512 = 488×512 = 249856`（丢 144 条）；`steps_per_epoch = 249856 ÷ 512 = 488`；`max_train_steps = 3×488 = 1464`；每卡每 epoch 消费 249856 ÷ 16 = **15616** 条。
2. 冻结约 778M 参数 → 每卡省约 9.3 GB 优化器状态；`no_shard` 下每卡各持完整模型，该节省逐卡复制。
3. 时序清单应与 4.1.2 伪代码一致；配置字段分别来自 `train.precision`、`logging.checkpoint_dir`、`model.target_model_name_or_path`、`data.target_cache_path`、`train.local_batch_size/global_batch_size/num_train_epochs/max_train_steps`、`train.lr/warmup_ratio/weight_decay`。

## 6. 本讲小结

- `BaseTrainer.__init__` 是一条**严格线性**的装配流水线：环境（`init_dist`、断点发现、日志）→ 模型（构建 → 恢复权重 → `torch.compile` → FSDP）→ 数据（`CacheDataset` + 三项合同校验）→ 日程与优化器（`_compute_training_schedule` → `BF16Optimizer` → 恢复 `next_micro_step`）。
- 草稿模型的 `embed_tokens` 和 `lm_head` **直接从目标模型 CPU 上拷贝并冻结**：接口与目标严格对齐是语义要求，顺带省下约 778M 参数的 fp32 主权重与 AdamW 动量（Qwen3-4B 规格下约 9 GB 级显存）。
- 训练日程由三个纯函数推出：\(\text{grad\_accum} = B_{global}/(W \cdot B_{local})\)，\(\text{samples\_per\_epoch}\) 向下取整到全局批整数倍，\(\text{steps\_per\_epoch} = \text{samples\_per\_epoch}/B_{global}\)；默认 8 卡配置下 64 次梯度累积对应一次参数更新。
- FSDP 以 `use_orig_params=True` + 混合精度包装，默认配置选 `no_shard`（小模型不切分，只借 FSDP 的分布式外壳）；参数对象跨包装不变，所以优化器建在裸 `draft_model` 上依然正确。
- `Qwen3DSparkTrainer` 只填 `_build_draft_model` 与 `run_batch` 两个钩子，`Gemma4DSparkTrainer` 再薄一层只换模型类——「一套骨架服务所有算法×模型族」是本仓库最重要的架构决定。

## 7. 下一步学习建议

- **u3-l2 训练主循环**：`__init__` 装配好的 `next_micro_step / gradient_accumulation_steps / model.no_sync()` 如何在 `train()` 里协同工作，`SuspendController` 如何实现挂起保存——本讲的日程推导将直接被引用。
- **u3-l4 优化器与调度**：`BF16Optimizer` 的 fp32 主权重机制、`WarmupScheduler` 两阶段切换；本讲只触及它的参数过滤行为。
- **u3-l5 检查点管理**：`discover_latest_checkpoint` 找到的 `step_latest` 是怎么原子写入的，`load_training_state` 如何把 `next_micro_step` 与采样器偏移对齐（可顺带验证练习 2 中「待确认」的扩卡恢复问题）。
- 想提前接触算法侧的读者，可先浏览 [dspark_trainer.py:25-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L39) 的 `run_batch`，它就是 u4 系列的入口预告。
