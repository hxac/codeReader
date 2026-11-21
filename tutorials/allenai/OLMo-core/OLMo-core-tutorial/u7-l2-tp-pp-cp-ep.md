# u7-l2 张量、流水线、上下文与专家并行

## 1. 本讲目标

上一讲（u7-l1）我们弄清了数据并行（DDP/FSDP/HSDP）：**切数据、不切模型**。本讲补齐剩下的四个并行维度，它们都涉及「把模型或序列切开分给不同的 rank」：

1. **张量并行（TP）**：把单个权重矩阵按维度切开，多张卡合力算一个矩阵乘法。
2. **流水线并行（PP）**：把模型按层切成若干段，每段住在一组卡上，数据像流水线一样流过。
3. **上下文并行（CP）**：把输入序列按长度切开，专门对付超长上下文。
4. **专家并行（EP）**：把 MoE 的专家按「个」切开，每张卡放一部分专家。

学完本讲你应该能够：

- 说明 TP/PP/CP/EP 各自切分的对象、引入的通信模式与适用场景；
- 理解多维并行组合时 `build_world_mesh()` 如何划分进程组（网格维度的先后顺序是一条硬规则）；
- 为一个 8 卡（或多卡）训练任务写出组合并行配置；
- 讲清张量并行下 `CrossEntropyLoss` 的跨 rank 归约：为什么规约结果是 `Partial` 而不是 `Replicate`，为什么 mean 归约必须除以**全局**有效 token 数——这是提交 `075a7892d` 修复的真实 bug。

## 2. 前置知识

本讲默认你已读过 u7-l1（数据并行）。再补几个 PyTorch 分布式概念，它们是理解源码的词汇表：

- **rank / world size**：分布式训练里每个进程有一个编号（rank），进程总数是 world size。8 卡单机通常是 `torchrun --nproc-per-node=8` 拉起 8 个进程。
- **DeviceMesh（设备网格）**：把所有进程组织成一个多维数组的对象。比如 8 个进程可以组织成 `(2, 4)` 的二维网格。网格的每个维度有一个名字（如 `dp`、`tp`），取某个维度的子网格就得到「一个并行维度专属的进程组」。
- **DTensor 与 Placement（布局）**：DTensor 是「分布在网格上的张量」，它用 placement 描述每个 rank 持有的局部张量与全局张量的关系：
  - `Shard(d)`：每个 rank 持有第 `d` 维切出来的一块（拼起来才是完整的）；
  - `Replicate()`：每个 rank 持有一模一样的完整副本；
  - `Partial()`：每个 rank 持有**一部分和**，只有把所有 rank 的值逐元素加起来（all-reduce）才是正确值。**本讲第 4.1 节的主角就是它**。
- **三个集合通信原语**（通俗版）：
  - all-reduce：所有 rank 各出一个数，大家最后都拿到总和（或均值）；
  - all-gather：每个 rank 出自己那块，最后大家都有完整拼接结果；
  - all-to-all：每个 rank 把手里的数据按目的地拆开，各自送往不同 rank，常用于「换一个维度切分」。
- **微批次（micro-batch）**：PP 里把一个训练批切成小块依次流入流水线；也用于 DP 下的梯度累积（u6-l2 已见）。
- **气泡（bubble）**：流水线中某些 rank 因等待邻居而空闲的时间比例。

数学公式本讲用两处：TP 的均值损失 \( \text{mean loss} = \frac{\sum_{\text{rank}} \text{local sum}}{\text{global valid tokens}} \)，以及 PP 的气泡率 \[ \text{bubble} = \frac{p - 1}{m + p - 1} \] （\(p\) 为流水线级数，\(m\) 为微批数）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/distributed/parallel/tensor_parallel.py` | `TensorParallelConfig` 与 TP 专用的 `SequenceParallel` 包装 |
| `src/olmo_core/distributed/parallel/pipeline_parallel.py` | `PipelineParallelConfig`、调度类型枚举、stage 分配逻辑、`PipelineSchedule` 驱动器 |
| `src/olmo_core/distributed/parallel/context_parallel.py` | `ContextParallelConfig` 与 cp↔hp（序列分片↔头分片）的 AllToAll 工具 |
| `src/olmo_core/distributed/parallel/expert_parallel.py` | `ExpertParallelConfig`（目前极简） |
| `src/olmo_core/distributed/parallel/__init__.py` | `build_world_mesh()`：多维并行的进程组划分中枢，以及各 `get_*_mesh()` 取子网格函数 |
| `src/olmo_core/train/train_module/transformer/config.py` | 四个 `Transformer*ParallelConfig` 包装类（含 CP 的 zig-zag/llama3/ulysses 预设、PP 的切分点） |
| `src/olmo_core/train/train_module/transformer/common.py` | `parallelize_model()`：按固定顺序把各并行策略应用到模型上 |
| `src/olmo_core/train/train_module/transformer/pipeline_train_module.py` | `TransformerPipelineTrainModule`：PP 模式下的训练模块（先切模型再并行化） |
| `src/olmo_core/nn/transformer/model.py` | 模型侧的 `apply_tp / apply_pp / apply_cp / apply_ep` 入口 |
| `src/olmo_core/nn/cross_entropy_loss.py` | TP 感知的交叉熵损失，`075a7892d` 修复所在地 |
| `src/test/nn/cross_entropy_loss_test.py` | TP 损失的多进程对拍测试（本讲实践的依据） |
| `src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py` | 官方 32B 脚本（纯 HSDP） |
| `src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py` | 官方长上下文脚本（HSDP + CP 组合实例） |

## 4. 核心概念与源码讲解

### 4.1 张量并行（TP）：TensorParallelConfig 与「按维度切权重」

#### 4.1.1 概念说明

数据并行的每张卡都拿着**完整的模型**；当单个权重矩阵大到一张卡放不下（或放得下但算得太慢）时，就需要张量并行：把矩阵本身切开。

TP 的基本单位是 Megatron 式的「列并行 + 行并行」组合：

- 注意力层的 QKV 投影按**头（head）**切开（每卡拿到若干完整的头），输出投影按同样的头切法切开，两者拼起来一层的通信只需前后各一次 all-reduce；
- 前馈网络（FFN）按**中间维**切开；
- embedding 与 LM head 按**词表维**切开。

因为激活值在 TP 组内沿序列维分片（`Shard(1)`）流动，夹在两个并行线性层之间的 LayerNorm 也要跟着做**序列并行**（每个 rank 只对自己那段序列做归一化，再在内部完成所需的统计量通信）——这就是 `tensor_parallel.py` 里 `SequenceParallel` 类存在的意义。

TP 与其他维度的本质区别：**TP 是「多卡合作算一个算子」，卡间通信发生在每一层的前向/反向里**，因此 TP 组通常要求高带宽互联（NVLink 同机），度数一般不超过 8。这也解释了实践任务里的问题「为什么 attention 适合 TP」：注意力按头切分后各头的计算几乎独立，切分不需要改变数学结果，通信量小且规整。

#### 4.1.2 核心流程

给模型开 TP 的整体流程（由 u6-l2 讲过的 `parallelize_model()` 驱动）：

```text
1. build_world_mesh() 建好带 "tp" 维的网格（见 4.5）
2. get_tp_mesh(world_mesh) 取出 TP 子网格
3. model.apply_tp(tp_mesh)：
   a. embeddings        → RowwiseParallel，输出 Shard(1)，权重按词表维切
   b. embedding_norm    → SequenceParallel
   c. 每个 block        → block.apply_tp()：
      - 入口 PrepareModuleInput: 确保激活是 Shard(1)
      - attention / feed_forward → 列并行 + 行并行
      - 各 LayerNorm   → SequenceParallel
   d. lm_head           → w_out 列并行（词表维切），loss 部分做 TP 归约
   e. 若 tie_word_embeddings → 重新把 lm_head 指回切好的 embedding 权重
```

其中 TP 下的交叉熵损失归约（本讲重点，修复于 `075a7892d`）分三步：

1. **序列分片求和**：每个 rank 对自己那段序列的 token 损失求**和**（注意：即使调用者要 mean，底层也先用 sum，见 4.1.3）；
2. **全局归一**：mean 模式下，用 `get_full_tensor()` 把「全局有效 token 数」all-reduce 到每个 rank，再相除；
3. **Partial → Replicate 规约**：每个 rank 手里的标量只是部分和，DTensor 布局标成 `Partial()`，从 `Partial` 变成 `Replicate` 时 DTensor 自动插入一次 all-reduce，所有 rank 才拿到同一个全局损失。

为什么必须这么绕？写成一个公式：设第 \(r\) 个 rank 的局部损失和为 \(S_r\)、局部有效 token 数为 \(n_r\)，则正确均值是

\[ \text{mean loss} = \frac{\sum_r S_r}{\sum_r n_r} \]

而「各 rank 先算自己的均值再平均」得到的是 \( \frac{1}{p}\sum_r \frac{S_r}{n_r} \)——只有所有 \(n_r\) 相等时两者才 coincidentally 相等。一旦某个 rank 的序列段几乎全是 padding（`ignore_index=-100`），后者就会严重失真，甚至当 \(n_r = 0\) 时除零产生 NaN。

#### 4.1.3 源码精读

**配置类本体**。[tensor_parallel.py:L17-L40](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/tensor_parallel.py#L17-L40)：`TensorParallelConfig` 只有两个字段——`degree`（TP 度数）与 `enable_async`（实验性异步 TP，会打开 torch inductor 的 micro-pipeline 并为 TP 组开启对称内存）。TP 的「怎么切」不写在配置里，而是写在模型的 `apply_tp` 里。

**模型侧入口**。[model.py:L923-L985](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L923-L985)：`Transformer.apply_tp()` 先做两项能力检查（EMO 路由不支持 TP；tie 权重 + fused_linear 损失不支持 TP，[L938-L946](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L938-L946)），然后按 embedding → blocks → lm_head 的顺序套用并行计划：embedding 用 `RowwiseParallel`（输入 `Replicate`、输出 `Shard(1)`，[L955-L964](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L955-L964)）；最后如果权重共享，把被 `parallelize_module` 拆散的 tie 重新接上（[L978-L982](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L978-L982)）。

**块级序列并行入口**。[block.py:L167-L177](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L167-L177)：每个 block 先用 `PrepareModuleInput` 声明「我要 `Shard(1)` 的输入」，DTensor 若发现输入布局不符会自动插入重分片通信。

**SequenceParallel 包装**。[tensor_parallel.py:L43-L68](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/tensor_parallel.py#L43-L68)：在 torch 原生 `SequenceParallel` 之上增加了 `output_layouts` 支持（默认输出 `Shard(sequence_dim)`），并在输出布局不符时异步重分片（`async_op=True`）。

**TP 损失的三段式（修复核心）**。[cross_entropy_loss.py:L152-L215](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L152-L215) 的 `apply_tp()` 注册了三段布局约定：

1. 模块输入：logits/labels 都要求 `Shard(1)`（沿序列维分片，[L160-L166](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L160-L166)）——所以这里的损失是**序列并行**的：每个 rank 用完整词表算自己那段 token 的 CE；
2. 内层输出：reduction 为 none 时保持 `Shard(1)`（逐 token 损失），否则 `Shard(0)`（标量套一个长度 1 的维度，[L177-L192](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L177-L192)）；
3. **外层输出**：[L194-L197](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L194-L197)——这就是修复的那一行：

```python
# Reducing a sharded loss produces a partial sum, which must be reduced across ranks
# before returning a replicated (or local) loss. Declaring it replicated skips that step.
expected_output_layout = Shard(shard_dimension) if self.reduction == "none" else Partial()
```

**前向里的求和与归一**。[cross_entropy_loss.py:L119-L150](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L119-L150)：`forward()` 在 TP 开启时先 `ce_loss.sum()`（把每 rank 的局部损失加成标量），mean 模式下再用 `get_full_tensor((labels != ignore_index).sum())` 拿到全局有效 token 数相除（[L133-L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L133-L143)）。注释点破了动机：「对各 rank 的均值再取平均会给有效 token 数不同的分片同等权重」。

两个精妙细节：

- 内层为什么先强制 sum？[L51-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L51-L58)：`reduction="sum" if self.tp_enabled and self.reduction == "mean" else self.reduction`——TP + mean 时底层一律算 sum，归一化留到外层用全局计数做；
- 全局计数怎么来的？`labels` 是 `Shard(1)` 的 DTensor，对它做 `.sum()` 得到的天然是 `Partial` 布局的 DTensor；`get_full_tensor()`（[utils.py:L514-L518](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L514-L518)）调用 `full_tensor()` 触发 all-reduce，让每个 rank 都拿到分母 \(\sum_r n_r\)。分子分母的通信各一次，就这么拼出了正确的均值。

**修复前后的对比**（可用 `git show 075a7892d -- src/olmo_core/nn/cross_entropy_loss.py` 复核）：

| | 修复前 | 修复后 |
| --- | --- | --- |
| reduction=sum 的输出布局 | `Replicate()`（错误声明） | `Partial()` |
| sum 模式结果 | 每 rank 拿到**局部和**，无 all-reduce（2 卡实测 3.03 vs 参考 6.13） | all-reduce 后每 rank 拿到全局和 |
| mean 模式 | 各 rank 先算本地均值（分片全被 ignore 时是 0/0=NaN），且无全局归一 | 局部 sum + 全局有效 token 数归一，全 ignore 分片安全 |

#### 4.1.4 代码实践：从测试反推修复行为（源码阅读型，无需 GPU）

1. **实践目标**：不看结论，仅凭测试用例和源码推断「把 `Partial()` 误标为 `Replicate()` 会发生什么」。
2. **操作步骤**：
   - 通读 [cross_entropy_loss_test.py:L84-L142](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/cross_entropy_loss_test.py#L84-L142)。注意参数化里的 `ignore_shard`：当它为 True 时 [L114-L115](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/cross_entropy_loss_test.py#L114-L115) 把 `labels[:, :S//2]` 全部置为 -100——也就是说**某个 TP 分片上没有任何有效 token**；
   - 单进程参考值在测试主进程先算好（`loss_fn` 不开 TP），再交给 `run_distributed_test` 用 2 进程 NCCL 对拍（[L125-L142](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/cross_entropy_loss_test.py#L125-L142)）：分布式侧的 loss 和 logits 梯度都必须与单进程参考逐项一致（[L74-L81](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/cross_entropy_loss_test.py#L74-L81)）；
   - 运行 `git show 075a7892d -- src/olmo_core/nn/cross_entropy_loss.py`，对照 diff 复述三处变化（import `Partial`、内层强制 sum、外层 `Partial()`）。
3. **手工数值推演**（可在纸上完成）：设 2 个 rank，rank0 的有效 token 损失为 `[1, 2, 3]`（\(S_0=6, n_0=3\)），rank1 为 `[4]`（\(S_1=4, n_1=1\)）。
   - 正确 mean：\((6+4)/(3+1) = 2.5\)；
   - 错误的「均值再平均」：\((6/3 + 4/1)/2 = 3\)；
   - sum 误标 `Replicate()`：rank0 记 6、rank1 记 4（正确值是 10），**不同 rank 数值不一致且不报错**。
4. **需要观察的现象**：`expected_output_layout` 的类型如何决定 `PrepareModuleOutput` 是否插入 all-reduce；`Replicate() → Replicate()` 是 no-op，`Partial() → Replicate()` 才会触发规约。
5. **预期结果**：能说出「误标 Replicate 不会崩溃，而是静默返回 rank 局部值」——这比崩溃更危险，因为 loss 曲线看起来仍然合理，而依赖 loss 的逻辑（如 u5-l3 的 SkipStep loss-spike 检测、日志聚合）全都失真；mean 模式下梯度也因错误归一化而错误。（GPU 上运行该测试需 `@requires_multi_gpu`，本机无 GPU 时跳过属正常。）

#### 4.1.5 小练习与答案

**练习 1**：TP 下为什么 mean 归约不能让每个 rank 先 `.mean()` 再平均？
**答案**：各 rank 序列分片中的有效 token 数 \(n_r\) 不同（padding/ignore 分布不均），\(\frac{1}{p}\sum_r \frac{S_r}{n_r} \neq \frac{\sum_r S_r}{\sum_r n_r}\)；极端情况某个 \(n_r=0\) 会除零。正确做法是先局部 sum、再除以全局有效 token 数（[cross_entropy_loss.py:L133-L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/cross_entropy_loss.py#L133-L143)）。

**练习 2**：`Partial()` 和 `Shard(0)` 都表示「每个 rank 持有的不是完整结果」，区别是什么？
**答案**：`Shard(0)` 的语义是「全局张量沿第 0 维切开、拼起来即完整」，补全用 all-gather；`Partial()` 的语义是「各 rank 的值需要**逐元素相加**才正确」，补全用 all-reduce。每个 rank 的局部损失和不是全局损失的某一切片，而是它的一部分加项，所以这里必须是 `Partial`。

**练习 3**：为什么 `enable_async` 是实验性选项，而普通 TP 是默认能力？
**答案**：异步 TP（[tensor_parallel.py:L33-L40](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/tensor_parallel.py#L33-L40)）依赖 torch inductor 的 micro-pipeline TP 与对称内存（symmetric memory），通过编译期微流水线让通信与计算重叠，属较新的 PyTorch 特性；普通 TP 走的是稳定的 `parallelize_module` 路径。

### 4.2 流水线并行（PP）：PipelineParallelConfig 与「按层切模型」

#### 4.2.1 概念说明

当模型**深度**太深、一层层堆下来单卡放不下时，PP 把 Transformer 按层切成若干**阶段（stage）**，不同阶段放在不同 rank 上：stage 0 有 embedding，最后一个 stage 有 LM head，中间靠点对点（P2P）通信传递激活值和梯度。

PP 的代价是**气泡**：邻居没算完自己就得等。经典 1F1B 调度下气泡率为 \(\text{bubble} = \frac{p-1}{m+p-1}\)，所以微批数 \(m\) 要远大于级数 \(p\) 才划算。**交错式（interleaved）**调度把每个 rank 的层拆成多个不连续的 stage（stage 数是 rank 数的两倍），进一步压小气泡——这就是 `stage_ids_this_rank()` 存在的原因。

PP 与其他维度的本质区别：**PP 切的是「层间」边界，通信只在边界处发生（激活/梯度的 P2P），频率远低于 TP 的每层 all-reduce**，因此 PP 组可以跨机器走较慢的网络。

#### 4.2.2 核心流程

PP 训练模块的构建流程（比其他维度多一步「先切模型」）：

```text
TransformerPipelineTrainModule.__init__():
1. build_world_mesh(...)                 # 建带 "pp" 维的网格
2. pp_mesh = get_pp_mesh(world_mesh)     # PP 子网格；算出 prev/next 邻居 rank
3. pp_config.split_model(model, ...)     # deepcopy 整模型 → 每个 stage 丢掉不属于自己的层
4. parallelize_model(model_parts, pp_enabled=True, ...)
   # 对每个 stage 再依次应用 FP8/CP/TP/EP/AC/compile/DP
5. 逐 model_part 构建优化器（每个 stage 一个优化器）
```

stage 分配有两种风格（`PipelineSplitStyle`）：

- `loop`：rank \(r\) 拿 stage \(r, r+p, r+2p, \dots\)（循环取）；
- `v`：rank \(r\) 拿「一头一尾」成对 stage（\((r,\ \text{last}-r)\)），V 形排布让首尾阶段（embedding / LM head）的负载错开。

一个训练 step 里，`PipelineSchedule.step()` 把全局批切成 `num_microbatches` 份，交给具体的调度实现驱动所有 stage 的前向/反向。

#### 4.2.3 源码精读

**配置类与调度枚举**。[pipeline_parallel.py:L94-L149](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L94-L149)：`PipelineParallelConfig` 的必填字段只有 `degree`；`schedule` 默认 `interleaved_1F1B`，可选值见 [PipelineScheduleType L33-L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L33-L80)（1F1B、GPipe、zero-bubble 变体及 `custom_*` 系列）。注意 docstring 的警告：zero-bubble 变体目前有若干问题（含与 `torch.compile` 不兼容）。其余字段多是自定义 P2P 传输的调优项（独立 P2P 进程组、NCCL CTA 上下限等，[L115-L146](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L115-L146)）。

**stage 分配算法**。[pipeline_parallel.py:L273-L296](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L273-L296)：`stage_ids_this_rank(pp_rank, num_stages)` 先校验 `num_stages % degree == 0`，然后按风格返回该 rank 的 stage 元组：loop 风格为 `tuple(pp_rank + s * degree for s in range(stages_per_rank))`；v 风格要求每 rank 恰好 2 个 stage，用首尾配对的 `stage_v_pairs` 查表。配套的 `final_stage_rank()`（[L252-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L252-L259)）指出最后一个 stage 住哪个 rank（loop 是 `degree-1`，v 是 0）。

**切分点**。[train_module/transformer/config.py:L87-L116](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L87-L116)：`TransformerPipelineParallelConfig.get_split_points()` 在用户没给 `split_points`（[L76-L85](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L76-L85)）时自动生成：单 stage 调度每 rank 1 个 stage、多 stage 调度默认 2 个，总 stage 数超过层数直接报错，中间 stage 优先多分一层余数。

**切模型**。[train_module/transformer/config.py:L118-L171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L118-L171)：`split_model()` 先做调度与 stage 实现的一致性校验，然后逐 stage `copy.deepcopy(model)`、非首 stage 置空 embeddings、丢掉区间外的层。两个值得注意的硬约束：tied word embeddings 不支持 PP（[L147-L152](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L147-L152)，embedding 和 LM head 分居两个 stage 无法共享张量）；`custom_*` 调度必须搭配自定义 stage 实现。

**调度驱动器**。[pipeline_parallel.py:L747-L846](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L747-L846)：`PipelineSchedule` 构造函数里有个重要的**现实约束**（[L753-L756 与 L796-L803](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L796-L803)）：枚举里列出的标准 PyTorch 调度（1F1B、GPipe 等）**并未在当前 train module 上接线**，实际可用的是 `custom_interleaved_1F1B` 和 `custom_1F1B_V` 两个自定义调度。`num_microbatches` 缺省等于 PP 度数（[L805-L806](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L805-L806)）。`step()`（[L862-L919](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L862-L919)）只在持有首/末 stage 的 rank 上分别传入 `input_ids`/保留 `target`，并支持仅本步临时改写微批数（推理时一条序列一个微批）。

**训练模块侧的串联**。[pipeline_train_module.py:L141-L197](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/pipeline_train_module.py#L141-L197)：构建 world mesh → 取 PP 子网格并记录 prev/next 邻居 → `split_model` 切出 stages → 对切好的 `model_parts` 调 `parallelize_model(pp_enabled=True)` → 逐 part 建优化器。另注意 [L163-L167](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/pipeline_train_module.py#L163-L167)：在切分前先从完整模型记下 `num_flops_per_token`，否则每 rank 只统计到自己那段会少算。模型侧 `apply_pp` 本体在 [model.py:L906-L921](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L906-L921)（标记 `_pp_enabled` 并下发到各 block）。

#### 4.2.4 代码实践：手算 stage 分配与气泡（纸笔 + 源码阅读）

1. **实践目标**：对 `degree=2、总 stage 数=4、loop 风格` 手工推出每个 rank 持有的 stage，验证与源码公式一致。
2. **操作步骤**：
   - 读 `stage_ids_this_rank`（[L273-L296](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L273-L296)），代入 `pp_rank=0/1, num_stages=4, degree=2`；
   - 再代入 v 风格公式（`stage_v_pairs = zip(range(2), range(3, 1, -1))`）对比；
   - 用气泡率公式算 `p=2, m=4` 与 `p=2, m=16` 的差异。
3. **需要观察的现象**：loop 风格下 rank0 = (0, 2)、rank1 = (1, 3)；v 风格下 rank0 = (0, 3)、rank1 = (1, 2)——v 风格把首（含 embedding）和尾（含 LM head）stage 放在同一个 rank 上。
4. **预期结果**：气泡率分别约 \(1/5 = 20\%\) 与 \(1/17 \approx 5.9\%\)，即微批数越大流水线越满。
5. 以上为纯推演，无需运行；若想看真实调度时间线，可读 `debug_save_pp_schedule`（[L701-L744](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/pipeline_parallel.py#L701-L744)），它能把 `pipeline_order` 画成时间线图（需 matplotlib，属 dev extra）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PP 不支持 `tie_word_embeddings=True`？
**答案**：tie 要求 embedding 权重和 LM head 权重是**同一个张量**，而 PP 把它们放在不同 rank 的 stage 上（首 stage 与末 stage），无法共享存储（[config.py:L147-L152](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L147-L152)）。对照 4.1：TP 下 tie 是支持的，因为两个模块在同 rank 上、只是同样按词表维分片（[model.py:L978-L982](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L978-L982) 重新 tie）。

**练习 2**：`split_points=[0, 2]` 配 4 层模型会得到什么？
**答案**：3 个 stage：stage0 只有 embedding（block 0 之前就切开）、stage1 含 block 0/1、stage2 含 block 2/3 和 LM head（字段文档见 [config.py:L76-L85](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L76-L85)）。

**练习 3**：为什么 PP 的通信可以容忍跨节点网络，而 TP 通常不行？
**答案**：PP 只在 stage 边界传激活/梯度（P2P，每个微批每边界一次），频率低；TP 在**每一层**的前向/反向都要 all-reduce，带宽和延迟敏感，所以 TP 组应落在 NVLink 域内（这也是 `build_world_mesh` 把 tp 放在网格最后一维的原因之一，见 4.5）。

### 4.3 上下文并行（CP）：ContextParallelConfig 与「切序列」

#### 4.3.1 概念说明

长上下文训练（如 64K 序列）的瓶颈往往不是权重而是**激活**：注意力分数矩阵随序列长度平方增长。CP 把**序列维**切给多个 rank，每个 rank 只持有 \(T/\text{CP}\) 的 token。

难点在注意力：一个 token 要 attend 到整段序列的 KV。两条路线：

- **Ring attention（环注意力）**：KV 块在 CP 组内沿环轮转，每转一步算一部分注意力，u3-l3 已见过其 zig-zag / llama3 两种负载均衡（应对因果掩码下前后段计算量不均的问题）；
- **Ulysses**：先用一次 all-to-all 把数据从「序列分片、全头」换成「全序列、头分片」（此时就是普通的头并行注意力），算完再换回来。

本讲源码文件 `context_parallel.py` 里的 `cp2hp / hp2cp` 系列函数正是 Ulysses 路线的通信内核（原本为混合架构的序列混合器服务，头维/序列维互换是通用需求）。

CP 的另一个关键认识（在 4.5 的网格折叠规则里体现）：**CP 组内每个 rank 都持有完整模型参数**，只是处理不同的序列段。因此对「参数同步」而言，CP 组就像一组额外的 DP 副本——梯度要在 DP×CP 的并集上归约。

#### 4.3.2 核心流程

Ulysses 风格一次注意力的数据流（以 QKV 打包张量为例，`[B, T/CP, 3, H, D]`）：

```text
序列分片（CP 布局）                 头分片（HP 布局）
[B, T/CP, 3, H, D]  --cp2hp-->  [B, T, 3, H/CP, D]
      ↑                              ↓  普通注意力（每 rank 算自己的头）
[B, T/CP, 3, H, D]  <--hp2cp--  [B, T, 3, H/CP, D]
```

`cp2hp` 的本地部分是一串纯 reshape：把头维 H 拆成 `(CP, H/CP)`、把「CP 那一维」换到最前，拆成 CP 份，第 `i` 份经 all-to-all 送往 rank `i`；收到的 CP 份再沿序列维拼回全长。整个过程不搬运多余数据——每个 rank 发出的数据量等于收到的数据量。

配置层面，`TransformerContextParallelConfig` 要求在 `ring` 与 `uly` 两种风格中**必选其一**，并提供 `zig_zag` / `llama3` / `ulysses` 三个工厂方法。

#### 4.3.3 源码精读

**配置类**。[context_parallel.py:L10-L19](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/context_parallel.py#L10-L19)：`ContextParallelConfig` 只有 `degree` 一个字段——纯声明「我要 CP」，风格细节在 transformer 包装类里。

**Transformer 包装与预设**。[train_module/transformer/config.py:L278-L321](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L278-L321)：`__post_init__` 校验 ring/uly 恰给其一（[L287-L294](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L287-L294)）；`zig_zag(degree, head_stride)` 用 zig-zag 负载均衡的 ring 风格；`llama3(degree, head_stride)` 用 llama3 负载均衡（官方 OLMo-3 长上下文脚本用的就是它）；`ulysses(degree)` 用 Ulysses。

**cp→hp 的 reshape 链**。[context_parallel.py:L22-L64](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/context_parallel.py#L22-L64)：`all_to_all_single_cp2hp` 接受 `[B, T/CP, H, D]`，核心三步：

```python
# [B, T/CP, H, D] -> [B, T/CP, CP, H/CP, D] -> [CP, B, T/CP, H/CP, D]
input_split = input_.view(B, t_local, world_size, h_out, d_in).permute(2, 0, 1, 3, 4)
exchanged = all_to_all_single(cp_group, input_split.flatten(0, 3))
# 收到后：[CP, B, T/CP, H/CP, D] -> [B, T, H/CP, D]
exchanged = exchanged.view(world_size, B, t_local, h_out, d_in).permute(1, 0, 2, 3, 4)
exchanged = exchanged.reshape(B, t_local * world_size, h_out, d_in)
```

注意 view 把头维 `H` 解释成 `CP × H/CP`——第 `i` 个头块天然就是发给 rank `i` 的那份。反向 `hp2cp` 在 [L131-L173](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/context_parallel.py#L131-L173)（拆的是序列维），打包 QKV 的版本在 [L242-L272](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/context_parallel.py#L242-L272)；批量版（多条张量合并成一次 all-to_all）在 [L67-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/context_parallel.py#L67-L128)。

**模型侧入口**。[model.py:L987-L1008](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L987-L1008)：`apply_cp()` 按风格构建负载均衡器（`ring.load_balancer.build(cp_mesh)` 或 `uly...`），下发到每个 block 与 lm_head。

**官方用例**。[OLMo-3-1025-7B-long-context.py:L83-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L83-L89)：长上下文阶段 `dp_config` 用 `shard_degree=1` 的 HSDP（即纯复制 DP），`cp_config=TransformerContextParallelConfig.llama3(degree=8, head_stride=4)`——8 卡一个 CP 组吃 64K 序列。

#### 4.3.4 代码实践：单进程模拟 cp2hp 的形状魔法（可本地运行）

1. **实践目标**：不启动任何分布式进程，仅用 view/permute/cat 复现 `cp2hp` 的数据搬运，验证「序列分片 → 头分片」的语义正确性。
2. **操作步骤**：运行下面这段**示例代码**（非项目源码，模拟 `world_size=2` 的 all-to-all）：

```python
import torch

CP, B, T, H, D = 2, 1, 8, 4, 3
t_local, h_out = T // CP, H // CP

x = torch.arange(B * T * H * D, dtype=torch.float32).reshape(B, T, H, D)
# 每个 CP rank 各持有一段序列（其余维度完整）
shards = [x[:, r * t_local : (r + 1) * t_local].clone() for r in range(CP)]

def cp2hp_local(inp):  # 对应源码 L48-L53 的本地拆分
    split = inp.view(B, t_local, CP, h_out, D).permute(2, 0, 1, 3, 4)
    return list(split.unbind(0))  # 第 i 块将发往 rank i

outgoing = [cp2hp_local(s) for s in shards]
# 模拟 all-to-all：rank r 收到所有 rank 发来的「第 r 块」
def assemble(chunks):  # 对应源码 L58-L59 的收到端重组
    return torch.stack(chunks).permute(1, 0, 2, 3, 4).reshape(B, t_local * CP, h_out, D)

hp = [assemble([outgoing[src][r] for src in range(CP)]) for r in range(CP)]
for r in range(CP):
    assert torch.equal(hp[r], x[:, :, r * h_out : (r + 1) * h_out])
print("cp2hp 语义验证通过：rank 拿到全长序列 + 自己的头块")
```

3. **需要观察的现象**：`outgoing[src][r]` 的形状是 `[B, T/CP, H/CP, D]`（发出的每块已经既切了序列又切了头）；`hp[r]` 的形状是 `[B, T, H/CP, D]`。
4. **预期结果**：断言全部通过——rank `r` 最终持有**完整序列**的第 `r` 个头块，与输入的连续切片逐一相等。
5. 此脚本纯 CPU 单进程即可运行；若想验证真实多进程版本，需 `torchrun --nproc-per-node=2`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：ring attention 和 Ulysses 各自的通信原语是什么？各适合什么场景？
**答案**：ring 风格在 CP 组内环形传递 KV 块（多轮 P2P），适合超长序列（每 rank 始终只存 \(T/CP\) 激活）；Ulysses 用两次 all-to-all 换切分维度，实现简单、能与头并行注意力复用，但要求头数能被 CP 度整除（`H/CP`）。

**练习 2**：为什么 `get_dp_mesh()`（数据加载视角）**不**把 CP 维折进 DP 维，而 `get_dp_model_mesh()`（参数同步视角）要折？
**答案**：同一 CP 组的各 rank 处理**同一段数据的不同序列分片**，数据加载器必须给它们喂同一份数据，所以 CP 不能算独立的数据并行 rank；但每个 CP rank 都持有一整份参数、各自产生梯度，参数/梯度同步必须覆盖 DP×CP 并集（[parallel/__init__.py:L333-L346](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L333-L346) 的注释原文说明了这一点）。

**练习 3**：`zig_zag` 与 `llama3` 两种负载均衡分别解决什么问题？
**答案**：因果注意力下序列前段的 token 要 attend 的 KV 少、后段多，朴素均分会让 CP 各 rank 计算量悬殊。zig-zag 把序列首尾交错配对分片、llama3 用另一种交错编排，都是让每个 rank 拿到的「总注意力工作量」尽量均衡（u3-l3 介绍过这两种模式）。

### 4.4 专家并行（EP）：ExpertParallelConfig 与「切专家」

#### 4.4.1 概念说明

MoE 模型（u3-l5）有 \(N\) 个专家 MLP，路由器给每个 token 选 top-k 个。**专家并行**把专家按「个」整数划分：EP 度为 8、64 个专家时每 rank 放 8 个。这回答了实践任务的另一半「为什么 MoE 专家适合 EP」：

- 专家彼此完全独立（各自是一个完整 MLP），按专家切不需要像 TP 那样改动任何算子内部——切分粒度天然就是模块级；
- 路由本来就是「把 token 送到指定专家」的操作，EP 只是把「送到哪个专家」具体化为「送到哪个 rank」（u3-l5 讲过的 permute → 分组计算 → unpermute 三段式）；
- 相比之下对专家做 TP（把每个专家的矩阵再切丝）会让本已稀疏的 GEMM 变得更碎、通信更频繁。

OLMo-core 里 EP 有两条硬约束（源码里都有显式检查）：

1. **与 TP 互斥**；
2. **目前只能搭配 HSDP**，且 EP 度必须等于 HSDP 的分片度——EP 直接**复用** `dp_shard` 网格维度，不新建 `ep` 维。

#### 4.4.2 核心流程

```text
build_world_mesh(ep=..., dp=HSDP):
1. 校验 world size 能被 ep.degree 整除
2. 校验 tp is None（EP 与 TP 互斥）
3. 校验 dp 是 HSDP，且 ep.degree == shard_degree（复用 dp_shard 维）
4. 网格维度：pp?, dp_replicate, dp_shard(==ep), cp?, tp?（没有独立的 "ep" 维）

parallelize_model(ep_config=...):
1. 取 ep_mesh = get_ep_mesh(world_mesh)   # 实际就是 dp_shard 子网格
2. 校验模型是 MoE（否则报错）
3. MoETransformer.apply_ep(ep_mesh) → 每个 MoE block.apply_ep
```

由于 EP 维就是 `dp_shard` 维，同一条维度在数据加载视角被解释为「这些 rank 各拿不同数据」（`get_dp_mesh` 把 dp×ep 折叠成数据并行），在专家视角被解释为「这些 rank 各持不同专家」——一份网格两种解读。

#### 4.4.3 源码精读

**配置类**。[expert_parallel.py:L6-L15](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/expert_parallel.py#L6-L15)：全文件只有一个 `degree` 字段的 dataclass——EP 的全部复杂度都在 mesh 组合与 MoE 模块内部（`nn/moe/`），配置本身极简。Transformer 包装类 `TransformerExpertParallelConfig`（[train_module/transformer/config.py:L324-L328](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L324-L328)）也只是继承，无新增字段。

**mesh 组合的 EP 分支**。[parallel/__init__.py:L190-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L190-L206)：三连校验——整除、与 TP 互斥（`"expert parallelism is mutually exclusive with tensor parallism"`）、非 HSDP 直接抛错并留有 TODO（DTensor 跨网格操作尚不支持）。HSDP 分支在 [L225-L231](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L225-L231)：`ep.degree != shard_degree` 即报错，因为要复用同一维。

**取 EP 子网格的三级 fallback**。[parallel/__init__.py:L456-L475](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L456-L475)：`get_ep_mesh` 依次找 `ep` 维 → `ep_shard` 维 → `dp_shard` 维。前两种来自独立的专家网格（如 OLMoDDP 训练模块用的 `ep_mp/ep_dp` 体系，[L100-L102](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L100-L102)），第三种正是本讲的 HSDP 复用路径。

**应用侧**。[common.py:L84-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L84-L92)：EP 应用前先验证 `m.is_moe`（非 MoE 模型配 EP 是配置错误）；TP+EP 的 NotImplementedError 也在 TP 分支再拦一次（[L74-L76](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L74-L76)）。模型本体 [model.py:L1482-L1487](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1482-L1487)：只对 `is_moe` 的 block 下发 `apply_ep`。

**与 FSDP 的配合**。[model.py:L1489-L1507](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1489-L1507)：`prepare_experts_for_fsdp` 在套 FSDP 前单独处理专家模块，且当 PP/EP/TP 已启用时设置 `reshard_after_forward=False`（专家参数前向后不立即重分片，为路由的二次访问留参数）。

#### 4.4.4 代码实践：解读一条 EP 配置约束链（源码阅读型）

1. **实践目标**：解释「EP 为什么必须搭配 HSDP 且度数相等」，并追踪所有会拦截错误配置的代码点。
2. **操作步骤**：
   - 假设 8 卡跑 MoE，想配 `dp=fsdp, ep=2`：沿 [parallel/__init__.py:L190-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L190-L206) 找到会抛出的异常文案；
   - 改成 `dp=hsdp(2 副本 × 4 分片), ep=4`：确认合法（EP 度 == shard 度），并写出 8 个 rank 在 `(dp_replicate=2, dp_shard=4)` 网格中的坐标；
   - 再配 `tp=2` 同时 `ep=4`：找到两处拦截（mesh 侧 [L195-L198](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L195-L198) 与 `parallelize_model` 侧 [common.py:L74-L76](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L74-L76)）。
3. **需要观察的现象**：EP 没有独立的网格维度名，`get_ep_mesh` 走的是 `dp_shard` fallback。
4. **预期结果**：能复述约束链：TP×EP 互斥 → EP 仅 HSDP → EP 度 == 分片度；正确配置下 rank `(rep, shard)` 持有第 `shard` 组专家的完整副本 `rep` 份。
5. 本实践为纯阅读推演；如需实跑可参考 `src/examples/moe/train.py`（需 GPU，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：EP 复用 `dp_shard` 维有什么直接好处？
**答案**：不多建进程组（NCCL 通信器数量随维度组合增长，每个都有建立与显存成本）；同时梯度归约语义自然成立——`dp_shard` 组内的 all-reduce 同时完成了「不同专家副本间」的梯度同步，FSDP 对专家的分片也落在同一维上。

**练习 2**：如果把 EP 配到非 MoE 的稠密 Transformer 上会发生什么？
**答案**：`parallelize_model` 里显式抛 `OLMoConfigurationError("Expert parallelism is only valid for MoE models")`（[common.py:L88-L91](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L88-L91)）。

**练习 3**：为什么说「EP 切分不改算子、TP 切分要改算子」？
**答案**：EP 的切分单位是整个专家模块，每个专家在单 rank 上仍是普通稠密 MLP，前向代码不变，只是 token 要先 all-to-all 到专家所在的 rank（u3-l5 的 permute/unpermute）；TP 要把一个 `nn.Linear` 的权重矩阵沿维拆开、在算子层面插入分块乘法与通信，必须依赖 `parallelize_module`/DTensor 改写模块（对比 4.1 的 `apply_tp`）。

### 4.5 多维并行的组合：build_world_mesh 与 parallelize_model

#### 4.5.1 概念说明

前面四个维度从来不是孤立使用的：一个真实的大模型训练往往是 `PP × HSDP × CP` 或 `HSDP(replicate × shard[=EP])` 的组合。OLMo-core 用**一个多维 DeviceMesh** 统一表达所有维度，`build_world_mesh()` 是组装中枢。

两条贯穿性的规则：

1. **网格维度顺序固定**：`pp → dp（或 dp_replicate, dp_shard）→ ep → cp → tp`。这个顺序不是随便排的——它决定了哪些 rank 在网格上相邻（相邻 ≈ 更可能落在同一机内高速互联域），通信最频繁的 tp 放最后一维（组内 rank 连续编号，最容易整体落进单节点）；通信最稀疏的 pp 放第一维（天然跨节点）。
2. **度数从 world size 里逐级扣除**：先扣 PP，再扣 CP、TP、EP，剩下的全是 DP 度。任何一步除不尽都会在配置期报错，而不是训练期静默出错。

另一个重要主题是**同一网格的多视角**：`get_dp_model_mesh()`（给 FSDP/DDP 用）与 `get_dp_mesh()`（给数据加载器用）对同一 world mesh 的折叠规则不同——EP 维在两个视角都折进 DP（EP rank 拿不同数据、也各自产生梯度），而 **CP 维只在模型视角折叠**（CP rank 拿相同数据的不同段）。u7-l1 提过这两个函数的差异，本讲补全了「为什么」。

#### 4.5.2 核心流程

```text
build_world_mesh(dp, tp, cp, pp, ep):
1. 全为 None → 退化为纯 DP 一维网格
2. 任一模型并行存在 → dp 必须给出
3. 依次校验并扣除：pp.degree、cp.degree、tp.degree、ep.degree（都需整除且 ≥1）
   EP 额外：与 TP 互斥；非 HSDP 报错；HSDP 时要求 ep.degree == shard_degree
4. 按固定顺序堆维度名：pp?, (dp_replicate, dp_shard)? | dp?, ep?, cp?, tp?
5. init_device_mesh(...) 建网格；顺手初始化 DP 进程组

parallelize_model(model, ..., 各种 config):      # 固定应用顺序
PP → FP8 → CP → TP → EP → 激活检查点 → compile → DP → init_weights
```

8 卡的几种组合示例（world size = 8）：

| 组合 | 网格形状 | 每 rank 持有 |
| --- | --- | --- |
| 纯 HSDP（默认每节点一副本，单机 8 卡） | `dp_replicate=1 × dp_shard=8` | 1/8 参数（分片） |
| DDP + TP=4 | `dp=2 × tp=4` | 整模型副本（与另 1 rank 共享同 1/4 权重分片） |
| HSDP(2×2) + CP=2 | `dp_replicate=2 × dp_shard=2 × cp=2` | 1/2 参数 × 1/2 序列 |
| PP=2 + DP=4 | `pp=2 × dp=4` | 一半层 × 完整那半层的参数 |

#### 4.5.3 源码精读

**建网格与度数校验**。[parallel/__init__.py:L116-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L116-L259)：函数开头即守门——world mesh 全局只许建一次（[L157-L158](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L157-L158)）；全部为 None 时退化为纯 DP 网格（[L163-L164](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L163-L164)）；任一模型并行存在但没给 dp 时抛错（[L166-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L166-L169)）。度数扣除链在 [L171-L206](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L171-L206)：注意扣除顺序是 pp → cp → tp → ep（与维度堆叠顺序 pp → dp → ep → cp → tp 不同，两处顺序不必一致，校验只关心整除性）。

**维度堆叠顺序**。[parallel/__init__.py:L208-L249](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L208-L249)：代码注释就是文档——`# Pipeline parallel first.`、`# And lastly tensor parallel.`。HSDP 时 DP 拆成 `dp_replicate, dp_shard` 两维，EP 复用 `dp_shard`；非 HSDP 时 EP 独立成 `ep` 维（但如前所述该分支目前必然报错）。

**模型视角的折叠**。[parallel/__init__.py:L311-L348](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L311-L348)：`_get_model_mesh` 先把 `dp×ep` 折成 `dp_ep`（或 `ep_replicate×ep_shard` 折成 `dp`），再把 cp 维并入**紧邻其前的 dp 维**成 `dp_cp`——注释原文：「CP 组在参数同步意义上就是一组额外的 DP 副本，梯度要在 DP 与 CP rank 的并集上归约」。随后 [get_dp_model_mesh L351-L365](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L351-L365) 取所有 `dp*` 前缀的维度给 FSDP/DDP。

**数据视角的折叠**。[parallel/__init__.py:L368-L406](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L368-L406)：`get_dp_mesh` 同样折 EP（EP rank 应拿不同数据），但**拒绝**已折叠 CP 的网格（[L400-L403](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L400-L403)：同 CP 组必须拿同一份数据）。

**应用顺序**。[common.py:L34-L158](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L34-L158) 的 `parallelize_model()`：PP 最先（[L52-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L52-L57)，因为切模型必须发生在其他一切之前）→ FP8 → CP（[L65-L71](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L65-L71)）→ TP（[L73-L82](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L73-L82)）→ EP（[L84-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L84-L92)）→ 激活检查点 → compile → DP（[L115-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L115-L145)，最外层包裹）→ 最后统一 `init_weights`（[L147-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)，顺序原因 u3-l6/u6-l2 已述：初始化要在分片之后，保证各 rank 切到的分片一致）。这也复刻了 u6-l2 讲过的顺序契约：**模型并行在 build 之后、优化器 build 之前**。

**官方脚本的组合实例**。

- OLMo-2 32B（[OLMo-2-0325-32B-train.py:L81-L86](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L81-L86)）：**没有用 TP/PP/CP/EP**，纯 `hsdp` + bf16 参数/fp32 归约，`num_replicas=get_world_size() // 64`（每 64 卡一个副本、副本内分片度 64，注释 `# NOTE: tune this` 提示按集群微调）；≥1024 卡时改小 microbatch 并把激活检查点改为只包 FFN（[L95-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L95-L100)）。这说明：**32B 这个规模纯 HSDP 就够了，没必要上模型并行**。
- OLMo-3 7B 长上下文（[OLMo-3-1025-7B-long-context.py:L83-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L83-L89)）：`hsdp(shard_degree=1)`（即纯复制）+ `cp=llama3(degree=8)` + float8。组合出来的网格是 `dp_replicate × dp_shard(=1) × cp(8)`——DP 度=world_size/8。

#### 4.5.4 代码实践：读官方脚本、画 8 卡网格（本讲主实践）

1. **实践目标**：把本讲四个维度 + DP 的组合规则落到真实脚本上，产出一张 8 卡进程组划分图。
2. **操作步骤**：
   - 精读 [OLMo-2-0325-32B-train.py:L66-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L66-L100)：写出它的网格维度与度数（注意 `num_replicas` 公式在 512 卡时给出 `8 × 64` 的 HSDP 网格；8 卡时该公式不适用——这脚本本来就是为大规模集群写的）；
   - 精读 [OLMo-3-1025-7B-long-context.py:L83-L94](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L83-L94)，写出 8 卡下的网格（`dp_replicate=1, dp_shard=1, cp=8`）；
   - 自己设计两个 8 卡方案并画图：① `DDP(2) × TP(4)`；② `PP(2) × HSDP(1×4)`。对每个 rank 标注 `(pp, dp/tp/cp, …)` 坐标、持有的层范围/权重分片、以及它参加的各通信组（TP all-reduce、DP 梯度归约、PP P2P）；
   - 用 `--dry-run` 验证其一：`torchrun --nproc-per-node=8 src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py --dry-run /tmp/lc-dryrun`（需要满足脚本的数据路径等前提；无 GPU/数据时此步**待本地验证**，可退化为只读脚本源码推演）。
3. **需要观察的现象**：网格维度顺序中 tp 永远最后一维——`DDP(2)×TP(4)` 下 rank 0-3 是一个 TP 组、rank 4-7 是另一个 TP 组，两组各配一个 DP 副本；dry-run 日志会打印 `Built 3D device mesh with shape (...)` 之类的 `get_device_mesh_info()` 输出。
4. **预期结果**：能对任一方案回答三个问题——每个参数张量被切到哪些 rank？梯度在哪个组上归约？一个全局批如何划分到各 DP rank？并能解释「attention 适合 TP（按头切不改数学、通信规整且频繁宜同机）而 MoE 专家适合 EP（专家独立、粒度即模块、路由天然按专家分发 token）」。
5. 画图与推演无需任何环境；dry-run 需完整训练环境（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么网格维度顺序是 `pp → dp → ep → cp → tp`，把 tp 放最后？
**答案**：DeviceMesh 展平后，靠后的维度其组内 rank 编号更紧凑，更容易整组落进单节点的高速互联（NVLink）域。TP 通信最频繁（每层 all-reduce）因此放最后；PP 通信最稀疏（只在 stage 边界 P2P）因此放最前、可横跨节点（[parallel/__init__.py:L208-L249](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/parallel/__init__.py#L208-L249) 的堆叠注释：`Pipeline parallel first` / `lastly tensor parallel`）。

**练习 2**：`get_dp_model_mesh` 与 `get_dp_mesh` 对 CP 维的处理差异会导致什么具体行为？
**答案**：FSDP 用前者——CP 维折进 DP，8 卡 `HSDP(1×4)×CP(2)` 的参数同步组是 8 个 rank（每 rank 都持整参数副本要同步梯度）；数据加载器用后者——CP 不折，同 CP 组的两个 rank 拿到**同一份** batch 再各自切序列段，保证拼起来正好是完整数据。

**练习 3**：如果想要 `PP=2, TP=2, DP=2`（共 8 卡），`build_world_mesh` 会怎么校验？
**答案**：world size 8 扣掉 pp 2 → 4，扣掉 tp 2 → 2，剩下的 2 就是 DP 度（FSDP/DDP 皆可）。网格形状 `(pp=2, dp=2, tp=2)`。`parallelize_model` 里 PP 先切模型，TP 作用在每个 stage 内部，DP 最后包裹。任何一度除不尽（比如 8 卡想 `pp=3`）都会在 `build_world_mesh` 里抛 `OLMoConfigurationError`。

## 5. 综合实践

**任务：为「8 卡训一个 16 层 MoE 小模型 + 32K 长序列」设计并行方案并论证。**

1. **方案设计**：模型是 MoE（n_layers=16, 32 专家），序列 32K。请给出你的并行配置组合（写出 `TransformerTrainModuleConfig` 的 `dp_config / tp_config / cp_config / ep_config / pp_config` 各取什么），要求：
   - MoE 部分用 EP——注意 EP 的约束链（仅 HSDP、与 TP 互斥、度数=分片度）；
   - 长序列用 CP（在 `zig_zag / llama3 / ulysses` 里选一个并说明理由）；
   - 若你同时想要 TP，说明为什么做不到、如何取舍。
2. **网格图**：画出最终 8 卡的 DeviceMesh（维度名 + 度数），对 rank 3 标注：它参加哪些进程组、持有哪些专家、拿到数据的哪一段、序列的哪一段。
3. **损失归约复核**：结合 4.1，写出你的方案下 mean loss 的分子与分母分别需要在哪些 rank 集合上归约（提示：TP 开与不开答案不同；CP 各 rank 的有效 token 数也不同——想想 `get_full_tensor` 的全局计数在 CP 场景是否仍然正确，以及为什么）。
4. **验收**：把方案写成一段 Python 配置代码（示例代码，无需真实运行），对照 `build_world_mesh` 的校验链逐条自查会不会抛错。

本任务串起本讲全部模块：TP 的切分对象与损失归约（4.1）、PP 的取舍（4.2，多数小模型方案里你会主动放弃 PP）、CP 的序列切分与预设（4.3）、EP 的约束（4.4）、以及组合与网格规则（4.5）。

## 6. 本讲小结

- **TP 切权重矩阵**（attention 按头、FFN 按中间维、embedding/lm_head 按词表维），每层通信 all-reduce，宜同机；TP 下的 CE 损失 = 局部 sum + 全局有效 token 数归一 + `Partial()→Replicate()` 规约——`075a7892d` 修复的正是把输出布局误标 `Replicate()` 导致静默跳过 all-reduce、每 rank 拿到局部值的 bug。
- **PP 按层切模型**：`split_model` 用 deepcopy+丢层生成 stage，loop/v 两种 stage 分配风格，实际接线的是 `custom_interleaved_1F1B` 与 `custom_1F1B_V` 调度；tied embeddings 不支持 PP。
- **CP 切序列**：ring（zig-zag/llama3 负载均衡）或 Ulysses（cp2hp/hp2cp 两次 all-to-all 换分片维度）；参数同步视角下 CP 组等价于额外 DP 副本，但数据加载时同 CP 组必须拿同一份数据。
- **EP 切专家**：配置极简但约束强——与 TP 互斥、仅 HSDP、度数=分片度（复用 `dp_shard` 维）；专家按「个」整分，不改算子只搬 token。
- **组合中枢 `build_world_mesh`**：维度顺序固定 `pp → dp(×2 维 HSDP) → ep → cp → tp`，度数从 world size 逐级扣除；`parallelize_model` 按固定顺序应用（PP 最先、DP 最后、init_weights 收尾）。
- 官方脚本的现实参考：OLMo-2 32B 纯 HSDP 就够；OLMo-3 7B 长上下文用 HSDP(shard=1)+CP(llama3, 8)。

## 7. 下一步学习建议

- **下一讲 u7-l3（分布式工具与测试基础设施）**：动手写多进程测试，把本讲的 `run_distributed_test` / `requires_multi_gpu` 用法弄透——4.1 的 CE 损失测试就是范例。
- 回读 u3-l3 的 ring attention 与 KV cache 部分，把 CP 的负载均衡（zig-zag/llama3）与注意力后端实现对应起来；回读 u3-l5 的 permute/unpermute，对应 EP 的 token 搬运。
- 若要继续追 PP 的自定义调度实现，读 `src/olmo_core/train/train_module/transformer/pipeline/pipeline_schedule.py`（`PipelineSchedule` 驱动器引用的 `CustomScheduleInterleaved1F1B` / `CustomSchedule1F1BV` 所在地）。
- 结合 u10-l2（CUDA 内核）看 EP 场景下 grouped GEMM 如何与专家分组配合；结合 u10-l1（float8）注意 `parallelize_model` 里 FP8 与 CP/TP 的先后关系。
