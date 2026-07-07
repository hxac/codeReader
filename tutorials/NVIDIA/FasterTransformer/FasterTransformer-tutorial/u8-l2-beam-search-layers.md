# Beam search 层（含 online beam search）

## 1. 本讲目标

在 u8-l1 中我们看到，`DynamicDecodeLayer` 在 `beam_width > 1` 时会把解码后端切到 beam search。本讲就钻进这个后端，把「每一步如何在词表 logits 上选出最优的若干条候选序列」彻底讲清。读完本讲你应当：

- 说清 `BeamSearchLayer` / `OnlineBeamSearchLayer` 与 `BaseBeamSearchLayer` 的继承关系，以及为什么子类只需重写 `invokeSoftMax`。
- 复述 `BaseBeamSearchLayer::forward` 的三步固定骨架（penalty → topk → 间接表更新）。
- 理解 `beam_search_penalty_kernels` 在选 token 前对 logits 做的 bias / temperature / repetition / min_length 修正。
- 掌握朴素 beam search 的「log prob 累加 + 两阶段 topk + 状态更新」流程，以及 topk kernel 的并行策略。
- 区分 `OnlineBeamSearchLayer` 引入的 `BeamHypotheses` 结构与朴素 beam search 的核心差别。
- 会用 length penalty 的数学公式解释它如何影响最终候选排序。

## 2. 前置知识

本讲默认你已读过 u8-l1（`DynamicDecodeLayer` 的统一外观与 runtime_arg 机制）与 u5-l2（`Decoding::forward` 的逐步生成主循环）。回顾几个关键概念：

- **logits**：模型在每一步对词表每个词输出的原始分数，形状 `[local_batch_size, beam_width, vocab_size_padded]`。它还不是概率，要经过 softmax 才归一化。
- **beam search**：每一步同时维护 `beam_width` 条「部分序列」（beam），从每条 beam 的词表分布里挑出若干候选，再汇总收缩回 `beam_width` 条，以此在序列空间里做宽度受限的搜索。
- **cum_log_probs**：一条 beam 到目前为止的累计对数概率。beam search 用它（而非单步概率）来给整条序列打分，因为对数概率可累加。
- **finished / end_id**：当某条 beam 生成出结束符 `end_id` 时标记为 finished，不再参与后续扩展。
- **cache_indirection**：beam search 选出新 beam 后，KV cache 需要按新的 beam 归属重排；FT 用一张「间接寻址表」做逻辑重排而非物理拷贝（详见 u6-l2）。本讲的 `update_indir_cache_kernelLauncher` 就是负责更新这张表的。

还需要的几个数学事实：

- 对数域的加法对应概率域的乘法：\(\log(p_1 \cdot p_2) = \log p_1 + \log p_2\)，所以「累计对数概率」就是把每步 token 的对数概率相加。
- 数值稳定的 softmax：先减去最大值再取指数，避免上溢。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [BaseBeamSearchLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.h) | 抽象基类，定义 `forward` 公共骨架与三个纯虚接口 `allocateBuffer / invokeSoftMax`，以及 `update_indir_cache_kernelLauncher` 声明。 |
| [BaseBeamSearchLayer.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu) | 基类实现：构造/析构、`forward` 三步骨架、`update_indir_cache_kernel` 间接表更新 kernel。 |
| [BeamSearchLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.h) / [.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu) | 朴素 beam search：用 `invokeTopkBeamSearch` 做两阶段 topk，softmax 在前、topk 在后，分开物化 log prob 缓冲。 |
| [OnlineBeamSearchLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.h) / [.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu) | 在线 beam search：用 `invokeTopkSoftMax` 把 softmax+topk+BeamHypotheses 处理融进一组 kernel。 |
| [beam_search_topk_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.h) | 朴素 topk kernel 集合：`beam_topK_kernel`/`batch_topK_kernel`（diversity 分支）、`topk_stage_1_opt3`/`topk_stage_2_opt3`（标准分支）、`apply_length_penalty`、`BeamHypotheses` 结构定义。 |
| [beam_search_penalty_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu) | 选 token 前的 logits 修正：`add_bias_temperature`、`apply_repetition_penalty`、`apply_min_length_penalty`，统一入口 `invokeAddBiasApplyPenalties`。 |

## 4. 核心概念与源码讲解

### 4.1 类继承体系与 forward 公共骨架

#### 4.1.1 概念说明

FT 的 beam search 后端是一个标准的「模板方法」结构：把「每一步都一样的流程」抽到基类，把「选 token 的具体策略」留给子类。

继承关系如下：

```
DynamicDecodeBaseLayer                       // 最底层，提供 stream/allocator/cublas 环境
      ▲
      │
BaseBeamSearchLayer<T>                       // 模板方法基类：forward 三步骨架
      ▲
      ├── BeamSearchLayer<T>                 // 朴素 beam search（先 softmax 再 topk）
      └── OnlineBeamSearchLayer<T>           // 在线 beam search（融合 softmax+topk）
```

基类 [`BaseBeamSearchLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.h#L24-L68) 持有 `vocab_size_` / `vocab_size_padded_` 以及一块 topk 工作区 `topk_softmax_workspace_`，并声明三个纯虚函数：两个重载的 `allocateBuffer` 与 `invokeSoftMax`。子类只需要实现这三个函数，公共的 `forward` 完全由基类提供。

#### 4.1.2 核心流程

无论朴素还是在线，beam search 每一步的 `forward` 都固定做三件事：

1. **penalty**：在原始 logits 上叠加偏置、做温度缩放、应用重复惩罚与最小长度惩罚——即 `invokeAddBiasApplyPenalties`。
2. **invokeSoftMax（子类实现）**：把修正后的 logits 转 log 概率、累加到 `cum_log_probs`、做 topk 选出本步 token、更新 `finished` / `sequence_length` / `parent_ids` 等状态。朴素版与在线版差异全在这一步。
3. **间接表更新**：当 `beam_width > 1` 时，调用 `update_indir_cache_kernelLauncher` 写入 `tgt_cache_indirection`，供下一步按新 beam 归属读取 KV cache（承接 u6-l2 的 cache_indirection 机制）。

#### 4.1.3 源码精读

公共 `forward` 入口 [`BaseBeamSearchLayer::forward(TensorMap*, TensorMap*)`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L180-L286) 严格按上述三步组织。其中第一步 penalty 的调用：

[BaseBeamSearchLayer.cu:237-260](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L237-L260) ——把 logits 连同偏置、temperature、repetition/presence penalty、min_length 交给 `invokeAddBiasApplyPenalties` 原地修正。

第二步把控制权交回子类：

[BaseBeamSearchLayer.cu:262](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L262) ——`invokeSoftMax(output_tensors, input_tensors)` 是纯虚调用，运行期分派到 `BeamSearchLayer` 或 `OnlineBeamSearchLayer` 的实现。

第三步间接表更新只在多 beam 时执行：

[BaseBeamSearchLayer.cu:264-280](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L264-L280) ——`beam_width > 1` 时调用 `update_indir_cache_kernelLauncher`，把本步的 `parent_ids`（即每条新 beam 来自哪条旧 beam）写进 `tgt_cache_indirection`。

需要特别说明的是 `forward` 的「张量约定」。基类在注释里固定了输入/输出张量集合：

[BaseBeamSearchLayer.cu:183-208](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L183-L208) ——输入至少 7 项（`logits` / `step` / `ite` / `end_id` / `src_cache_indirection` 等，penalty 与 runtime 参数可选），输出至少 5 项（`output_ids` / `cum_log_probs` / `parent_ids` / `sequence_length` / `tgt_cache_indirection` 等）。这套约定由 u8-l1 的 `DynamicDecodeLayer` 在 `forward` 里组装好再传入，二者通过名字（`TensorMap`）耦合。

#### 4.1.4 代码实践

**实践目标**：确认「三步骨架 + 子类只重写一个虚函数」这一模板方法结构。

**操作步骤**：

1. 打开 `BaseBeamSearchLayer.h`，找到三个纯虚函数声明。
2. 打开 `BeamSearchLayer.h` 与 `OnlineBeamSearchLayer.h`，确认两个子类的 `private` 段都只重写了 `allocateBuffer`（两个重载）与 `invokeSoftMax`，没有任何 `forward`。
3. 在 `BaseBeamSearchLayer.cu` 的 `forward` 里，依次定位三处调用：`invokeAddBiasApplyPenalties` → `invokeSoftMax` → `update_indir_cache_kernelLauncher`。

**需要观察的现象**：两个子类的 `.cu` 文件里都搜不到 `forward` 的定义；`forward` 只在基类出现一次。

**预期结果**：这正是模板方法的特征——流程在基类固化，差异点（选 token）被隔离到 `invokeSoftMax`。后续若要新增第三种 beam search 策略，只需再写一个子类重写 `invokeSoftMax`，无需改 `forward`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseBeamSearchLayer` 把 `allocateBuffer` 和 `invokeSoftMax` 设成纯虚，而 `forward` 不是虚函数？

**参考答案**：`forward` 描述的是「对所有 beam search 都成立的三步流程」，没有差异，放基类一份即可复用；`allocateBuffer` 的大小取决于子类选 token 算法所需的工作区（朴素版与在线版工作区布局不同），`invokeSoftMax` 是真正的策略差异点，故二者必须由子类各自实现。

**练习 2**：`forward` 的第三步（间接表更新）为什么用 `if (beam_width > 1)` 守卫？

**参考答案**：`beam_width == 1` 时只有一条 beam，不存在「新 beam 来自哪条旧 beam」的问题，cache 无需重排，间接表无意义；只有多 beam 时才需要把本步的 beam 归属写进 `tgt_cache_indirection` 供下一步读取 KV cache。

---

### 4.2 penalty 阶段：在选 token 前修正 logits

#### 4.2.1 概念说明

在 topk 选 token 之前，FT 允许通过一组「惩罚/修正」参数对原始 logits 做调整，对应 u8-l1 介绍的 runtime_arg。beam search 的 penalty 由 [`beam_search_penalty_kernels.cu`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu) 提供，统一入口是 `invokeAddBiasApplyPenalties`，包含四种修正：

- **bias**：词表级偏置（embedding_bias），给某些词整体加分/减分。
- **temperature（温度）**：用 \(1/T\) 缩放 logits。\(T>1\) 放大概率分布、采样更随机；\(T<1\) 让分布更尖锐。
- **repetition / presence penalty**：抑制已生成词的重复。
- **min_length**：在序列长度未达 `min_length` 时屏蔽 `end_id`，强制模型继续生成。

#### 4.2.2 核心流程

`invokeAddBiasApplyPenalties` 按条件串行启动最多三个 kernel：

```
invokeAddBiasApplyPenalties(step, logits, ...)
├── if bias!=null || temperature!=1 || vocab!=vocab_padded:
│     add_bias_temperature     // 加偏置 + 温度缩放 + padding 区置 -INF
├── if repetition_penalty_type != None && step>0 && 惩罚值非默认:
│     apply_repetition_penalty // 对已出现过的 token 调整其 logit
└── if step - max_input_length < min_length:
      apply_min_length_penalty  // 屏蔽 end_id 的 logit
```

注意三种惩罚的「默认值」由 [`getDefaultPenaltyValue`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/penalty_types.h#L32-L43) 决定：乘法型重复惩罚默认 1.0（无影响）、加法型（presence）默认 0.0（无影响），与默认值相等时跳过 kernel，省一次启动。

重复惩罚有两种类型（[penalty_types.h:26-30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/penalty_types.h#L26-L30)）：

- `Multiplicative`（repetition_penalty）：\(\text{logit}' = \text{logit} / p\)（logit>0）或 \(\text{logit}' = \text{logit}\cdot p\)（logit<0），\(p>1\) 时压制已出现词。
- `Additive`（presence_penalty）：\(\text{logit}' = \text{logit} - p\)，\(p>0\) 时无差别减分。

#### 4.2.3 源码精读

温度+偏置 kernel [`add_bias_temperature`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L24-L50)：

[beam_search_penalty_kernels.cu:40-49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L40-L49) ——`inv_temp = 1/(temperature+1e-6)`，对词表内索引做 `(logits+bias)*inv_temp`，对 padding 区（`i >= vocab_size`）置 `-MAX`，保证后续 softmax 中 padding 词概率为 0。

最小长度惩罚 kernel [`apply_min_length_penalty`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L156-L173)：

[beam_search_penalty_kernels.cu:169-172](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L169-L172) ——当 `sequence_lengths[bbid]+1-max_input_length < min_length`（即已生成 token 数还不到 min_length）时，把该 beam 的 `end_id` 位置 logit 置 `-MAX`，阻止过早结束。注释说明 `sequence_lengths` 表示 KV cache 长度，需要 `+1`。

重复惩罚 kernel [`apply_repetition_penalty`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L88-L154) 的关键逻辑：

[beam_search_penalty_kernels.cu:121-126](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L121-L126) ——`IS_ADDITIVE` 为 false 时走乘法分支（logit>0 除以 p、logit<0 乘以 p），为 true 时走减法分支。kernel 先沿 `parent_ids` 回溯本条 beam 历史上生成过的所有 token（[L128-L145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L128-L145)），把它们在当前 logits 里对应位置一一改写。

#### 4.2.4 代码实践

**实践目标**：理解 `min_length` 惩罚的触发条件与效果。

**操作步骤**：

1. 阅读 [apply_min_length_penalty](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L156-L173) 与 `invokeAddBiasApplyPenalties` 中调用它的守卫条件 [beam_search_penalty_kernels.cu:258-266](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_penalty_kernels.cu#L258-L266)。
2. 设想一次生成：`max_input_length=5`，`min_length=3`，思考当 `step` 分别为 5、6、7 时 kernel 是否启动。
3. （可选）参考测试 [`tests/unittests/test_penalty_kernels.cu`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/tests/unittests/test_penalty_kernels.cu)，看它如何构造 logits 与期望输出验证 temperature/repetition 的正确性。

**需要观察的现象**：守卫条件是 `step - max_input_length < min_length`。`step` 从 1 开始计数生成步，`step - max_input_length` 就是「已生成的新 token 数」。

**预期结果**：上例中 step=5、6 时 `step-max_input_length` 分别为 0、1，均 < 3，惩罚启动，`end_id` 被屏蔽；step=7 时为 2 仍 < 3 启动；直到 step≥8（已生成 ≥3 个新 token）才不再屏蔽 `end_id`，模型此时被允许输出结束符。

> 待本地验证：若你已编译 `tests/unittests`，可运行 `test_penalty_kernels` 用例确认上述阈值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_bias_temperature` 要把 `i >= vocab_size` 的位置置成 `-MAX`？

**参考答案**：FT 为了对齐会把词表补齐到 `vocab_size_padded`（通常是 8 的倍数，承接 u6-l1 的 `vocab_size_padded_`）。padding 区不是真实词，置 `-MAX` 后 softmax 中 \(e^{-\text{MAX}}\approx 0\)，确保它们永远不会被选为 token。

**练习 2**：repetition_penalty 的乘法分支为什么对正负 logit 分别用「除以 p」和「乘以 p」？

**参考答案**：\(p>1\) 时，正 logit 除以 p 会变小、负 logit 乘以 p 会变更负，两种情况都在「降低该词的概率」，从而抑制重复；若统一用除法，负 logit 反而会变大（更接近 0），效果就反了。

---

### 4.3 朴素 BeamSearchLayer：log prob 累加与两阶段 topk

#### 4.3.1 概念说明

[`BeamSearchLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.h#L25-L66) 是「先做完整 softmax 再做 topk」的朴素实现。它显式物化一份 `[local_batch_size*beam_width, vocab_size_padded]` 的 float log 概率缓冲 `float_log_prob_buf_`（[BeamSearchLayer.h:43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.h#L43)），把每条 beam 在词表上的对数概率全部算出来，再交给 topk kernel 选候选。

这种做法直观、易于实现「diversity_rate」「length penalty」等特性，代价是要读写一次完整的词表概率矩阵（vocab 通常几万，访存开销大）。在线变体（4.4 节）正是为了消除这次完整物化而设计。

#### 4.3.2 核心流程

`BeamSearchLayer::invokeSoftMax` 分三步：

```
invokeSoftMax(output, input)
├── invokeLogProbAddCumLogProb     // 对每条 (batch,beam) 做 softmax，
│                                  //   结果 += cum_log_probs[bid]，写成 log 概率
├── invokeTopkBeamSearch           // 两阶段 topk 选出本步每条 batch 的 beam_width 个 token
└── invokeUpdateStates             // 由 topk 结果回填 parent_ids/sequence_length/
                                  //   finished/cum_log_probs/output_ids
```

第一步把单步 logits 转成「累计到当前的 log 概率」。设某条 beam 当前累计 \(\text{cum}\)，本步 logits 为 \(z\)，则：

\[
p_i = \mathrm{softmax}(z)_i = \frac{e^{z_i - \max z}}{\sum_j e^{z_j-\max z}}, \qquad
\text{log\_prob}_i = \log p_i + \text{cum}
\]

即新的累计对数概率 = 旧累计 + 本步 token 的对数概率。这样 topk 比较的就是「选了这个 token 后，整条序列的对数概率」。

第二步两阶段 topk 的概念（以 `beam_width=4` 为例）：

1. **每条 beam 取自己的 top 候选**：4 条 beam 各自在词表上找若干最优 token，概念上得到 4×4 = 16 个 `(beam, token)` 候选对。
2. **跨 beam 合并收缩**：把 16 个候选拌在一起，取整体最优的 4 个，作为本步选出的 4 条新 beam。

这正是「在 vocab logits 上选出 4×4 候选并收缩回 4 条 beam」的实现原理（具体 kernel 细节见 4.4 节，那里把朴素版的 `beam_topK_kernel`/`batch_topK_kernel` 与标准版的 `topk_stage_1/2` 一起讲）。

#### 4.3.3 源码精读

softmax+累加 kernel [`logProbAddCumLogProb`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L23-L72)：每个 block 处理一条 (batch,beam)。

[BeamSearchLayer.cu:56-70](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L56-L70) ——先用 `blockReduceMax` 求最大值做数值稳定，再用 `blockReduceSum` 求和，最后写入 \(\log(p_i)+\text{cum}\)。当 beam 已 finished（[L39-L43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L39-L43)），只把 `end_id` 位置保留旧累计、其余置 `-FLT_MAX`，相当于「冻结」该 beam。

`invokeSoftMax` 主流程在 [BeamSearchLayer.cu:171-270](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L171-L270)。其中 `BeamHypotheses` 仅在「输出含 `beam_hyps` 且 `diversity_rate==0`」时填充——这是朴素 beam search 与在线变体共享的「收集已完成 beam」结构（4.4 节详述）：

[BeamSearchLayer.cu:224-235](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L224-L235) ——把 `step` / `ite` / `output_ids_src` / `parent_ids_src` / `length_penalty` 等填进 `beam_hyps`，供 topk kernel 在遇到 `end_id` 时记录已完成路径。

最后一步 `invokeUpdateStates`（[BeamSearchLayer.cu:255-268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L255-L268)）调用 [`updateStatesKernel`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L92-L136) 把 topk 选出的「打包 id」拆开回填各状态数组：

[BeamSearchLayer.cu:113-L126](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L113-L126) ——topk 返回的 `word_ids[index]` 编码了「来自哪条旧 beam」与「选了哪个 token」两个信息，用 `beam_id = (word_ids/vocab_size) % beam_width`、`word_id = word_ids % vocab_size` 拆开。这正是「收缩回 4 条 beam」后，每条新 beam 知道自己父亲是谁的依据。

#### 4.3.4 代码实践

**实践目标**：用一个最小数值例子手算「log prob 累加 + 4×4 收缩」的过程。

**操作步骤**：设 `beam_width=2`（便于手算），词表 `vocab_size=4`，两条 beam 的上一步 cum_log_probs 为 `[cum0=-1.0, cum1=-2.0]`，本步 logits 为：

```
beam0 logits: [1.0, 2.0, 0.0, 0.0]   → softmax ≈ [0.21, 0.58, 0.10, 0.10]
beam1 logits: [0.0, 0.0, 3.0, 1.0]   → softmax ≈ [0.06, 0.06, 0.47, 0.06]（示意）
```

1. 对每条 beam 计算 \(\log p_i + \text{cum}\)，得到两条 beam 在 4 个词上的累计 log 概率。
2. 在这 2×4=8 个候选里取最大的 2 个，作为本步选出的 2 条新 beam。
3. 查 [updateStatesKernel](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L92-L136) 里 `beam_id`、`word_id` 的拆解式，确认 topk 返回的「打包 id」如何对应到「旧 beam + 新 token」。

**需要观察的现象**：收缩后的两条新 beam 可能都来自同一条旧 beam（例如 beam0 的两个词都很好），也可能各来自一条——这就是 beam search 的「重排」本质。

**预期结果**：每条新 beam 的 `parent_ids` 记录其父 beam 编号，`cum_log_probs` 更新为新累计值，`output_ids` 追加新 token，`finished` 在选到 `end_id` 时置位。

#### 4.3.5 小练习与答案

**练习 1**：为什么 topk 比较的是 \(\log p_i + \text{cum}\) 而不是单步 \(\log p_i\)？

**参考答案**：beam search 的目标是最大化「整条序列」的概率，即各步概率之积。对数域里乘积变加法，所以累计 \(\log p\) 等价于序列概率的对数。若只比单步概率，会偏好「当前这一步最可能」而非「整条序列最优」，与 beam search 的目标不符。

**练习 2**：`logProbAddCumLogProb` 在 beam 已 finished 时把非 `end_id` 位置置 `-FLT_MAX`，目的是什么？

**参考答案**：冻结该 beam，使其在后续 topk 中除 `end_id`（保持原累计）外不可能再被选中扩展，避免已完成的序列继续参与生成，同时保留它已有的累计分数用于最终排序。

---

### 4.4 topk kernel 的两阶段并行选择与 length penalty

#### 4.4.1 概念说明

朴素 beam search 的 topk 由 [`invokeTopkBeamSearch`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L582-L666) 统一入口，内部分两条实现路径，由 `diversity_rate` 是否为 0 决定：

- **diversity 分支**（`diversity_rate != 0`）：[`beam_topK_kernel`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L43-L91) 每条 beam 取 top-k，再 [`batch_topK_kernel`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L93-L118) 跨 beam 合并。这一路径恰好对应「4 条 beam 各取 4 候选 → 4×4 合并回 4」的标准描述。
- **标准分支**（`diversity_rate == 0`）：[`topk_stage_1_opt3`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L156-L223) 把每条 beam 的词表切给多个 block 并行找 top-k，[`topk_stage_2_opt3`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L225-L359) 合并并处理 `BeamHypotheses`。

length penalty 把「累计 log 概率」归一化成可与不同长度序列公平比较的分数：

\[
\text{score} = \frac{\log p_{\text{cum}}}{\text{length}^{\,\text{length\_penalty}}}
\]

由 [`apply_length_penalty`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L33-L41) 实现。`length_penalty>1` 偏好更长序列、`<1` 偏好更短、`==0` 关闭。

#### 4.4.2 核心流程

`beam_width=4`、diversity 分支的 4×4 收缩流程：

```
输入 log_probs: [batch, 4, vocab]    每条 beam 一行词表 log 概率
        │
        ▼  beam_topK_kernel  (grid = batch*4 个 block，每 block 处理一行)
每条 beam 取 top-4 → 4 beams × 4 = 16 个候选  (即 4×4)
        │   （写入时对第 i 名加 diversity_rate*i 偏置）
        ▼  batch_topK_kernel (grid = batch 个 block，每 block 合并 16 个)
16 个候选取整体 top-4 → 本步选出的 4 条新 beam
```

标准分支的差别在于 stage1 把「每条 beam 找 top-k」并行化得更细：用 `BLOCKS_PER_BEAM_` 个 block 分摊一条 beam 的词表扫描（每 block 各找 top-k，再在 stage2 合并 `k*k*BLOCKS_PER_BEAM` 个候选），从而加速大词表场景。两条路径最终都输出「打包 id」给 `updateStatesKernel` 拆解。

length penalty 在两处影响最终选择：

1. `beam_topK_kernel`（diversity 分支）在取 top 时直接对 score 应用 length penalty（[L72-L76](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L72-L76)）。
2. `topk_stage_2_opt3`（标准分支）在收集已完成 beam 时用 length penalty 计算归一化分数 `normed_score`，与历史最差已完成 beam 比较以决定是否替换（[L285-L339](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L285-L339)）。

#### 4.4.3 源码精读

length penalty 公式实现：

[beam_search_topk_kernels.cu:34-41](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L34-L41) ——`length_penalty==0 || length==1` 时直接返回原值（避免除以 1 或关闭惩罚时的多余运算），否则返回 \(\text{log\_prob}/\text{length}^{\,\text{length\_penalty}}\)。

diversity 偏置写入：

[beam_search_topk_kernels.cu:85-90](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L85-L90) ——`beam_topK_kernel` 在写每条 beam 的 top-k 时，对第 `i` 名的分数叠加 `diversity_rate*i`，用 `cub::BlockReduce<TopK<T,MAX_K>>` 做块内归约选出 top-k（[L80](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L80)）。`TopK<T,MAX_K>` 是一个维护前 `MAX_K` 大的结构体，`reduce_topk_op` 是合并两个 `TopK` 的算子。

标准分支的 beam_width 派发：

[beam_search_topk_kernels.cu:624-650](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L624-L650) ——通过 `CASE_K` 宏对 `beam_width ∈ {1,4,10,16,32,64}` 各自实例化不同 `(BLOCK_SIZE, BLOCKS_PER_BEAM)` 组合（如 `beam_width=4` 用 `BLOCKS_PER_BEAM=8`），其余值回退到通用的 `topk_stage_1_opt2_general`。diversity 分支只支持 `{1,4,16,32,64}`，其余直接报错（[L659-L661](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L659-L661)）。

`BeamHypotheses` 结构定义在 [beam_search_topk_kernels.h:31-58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.h#L31-L58)，它持有 `output_ids_tgt`（已完成 beam 的 token 路径）、`normed_scores`（长度归一化分数）、`min_normed_scores`（每 batch 当前最差已完成分数）、`num_beams`（已收集的已完成 beam 数）。注释（[L23-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.h#L23-L30)）说明了它与传统实现的差别：传统做法是一条 beam finished 后就剔除、只在剩余 `beam_width-1` 条上继续；而这种实现记录已完成路径后，**仍以 `beam_width` 条继续搜索**，收集满 `beam_width` 条已完成序列后再按归一化分数排序。

#### 4.4.4 代码实践

**实践目标**：解释 `beam_width=4` 时「4×4 候选收缩回 4 条 beam」的实现，并说明 length penalty 如何影响最终选择。

**操作步骤**：

1. 打开 [`invokeTopkBeamSearch`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L582-L666)。先看 diversity 分支的 `CASE_K_DIV(4, 256, 256)`（[L655](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L655)），它启动 `beam_topK_kernel<T, 4, 256>`：grid 为 `batch_size*beam_width = batch*4`，每 block 处理一条 beam，取出该 beam 的 top-4。
2. 确认 stage1 写出的候选数为 `batch * beam_width * MAX_K = batch*4*4`，即每 batch 16 个候选（4×4）。
3. 接着 `batch_topK_kernel<T, 4>`（grid 为 `batch`）把每 batch 的 16 个候选取整体 top-4，收缩回 4 条 beam。
4. 再看标准分支的 `topk_stage_2_opt3` 在 [L285-L339](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L285-L339)：当一个候选的 token 是 `end_id` 时，用 `apply_length_penalty` 算 `normed_score`，把它存入 `BeamHypotheses.normed_scores` 并更新 `min_normed_scores`。
5. 跟踪 `num_beams == k`（即已收集满 4 条已完成 beam）后的逻辑（[L295-L321](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/beam_search_topk_kernels.cu#L295-L321)）：新候选的 `normed_score` 必须超过 `min_normed_scores` 才能替换最差的那条已完成 beam，否则提前 `is_stop` 结束。

**需要观察的现象**：diversity 分支里「4×4→4」是字面成立的（16 个候选取 4）；标准分支因为 `BLOCKS_PER_BEAM=8`，stage1 实际产出 `4*4*8=128` 个候选再合并，但概念上仍是「每条 beam 贡献若干候选、跨 beam 收缩回 beam_width 条」。

**预期结果**：你能用一句话说清 length penalty 的影响——它把「累计 log 概率」除以 \( \text{length}^{\,\text{length\_penalty}} \) 得到归一化分数，使得不同长度的已完成序列能公平比较；`length_penalty` 越大越偏好长序列。最终输出时，是从 `BeamHypotheses` 收集的已完成 beam 里按 `normed_scores` 选最优，而非简单取 cum_log_probs 最大的那条。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `invokeTopkBeamSearch` 要为不同 `beam_width` 用宏分别实例化 kernel，而不是写一个通用 kernel？

**参考答案**：`TopK<T,MAX_K>` 把 `MAX_K` 作为模板参数，编译期确定后可用 `#pragma unroll` 完全展开循环、把 top-k 维护逻辑固化进寄存器，性能远高于运行期变长的通用版本。代价是只能支持有限的 `beam_width` 集合（这里 {1,4,10,16,32,64}），其余回退到较慢的 `_general` kernel。

**练习 2**：设 `length_penalty=1.0`，两条已完成 beam 的 cum_log_probs 分别为 -10（长度 5）和 -12（长度 10），哪条更优？

**参考答案**：归一化分数分别为 \(-10/5^1 = -2.0\) 与 \(-12/10^1 = -1.2\)，后者更大，故长度 10 的那条更优。注意「cum_log_probs 更大」(-10 > -12) 的那条反而落选——这正是 length penalty 的作用：不除以长度时短序列天然占优，归一化后才能体现「每步平均概率」更高的长序列。

---

### 4.5 OnlineBeamSearchLayer 与 BeamHypotheses：在线 beam search

#### 4.5.1 概念说明

[`OnlineBeamSearchLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.h#L24-L63) 是 FT 当前推荐的 beam search 后端（u8-l1 中 `DynamicDecodeLayer` 在 `beam_width>1` 时实际走的就是它）。与朴素版「先写完整 log 概率矩阵再 topk」不同，它调用 [`invokeTopkSoftMax`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/online_softmax_beamsearch_kernels.h#L22-L39)（来自 `online_softmax_beamsearch_kernels`），把 softmax、cum_log_probs 累加、topk 选择、`BeamHypotheses` 收集融进一组 kernel，避免朴素版那次完整的词表概率物化。

两者的核心差别可归纳为：

| 维度 | BeamSearchLayer（朴素） | OnlineBeamSearchLayer（在线） |
| --- | --- | --- |
| softmax 与 topk | 分离：先全量 softmax 写缓冲，再 topk | 融合：softmax+topk+收集 一次完成 |
| 词表概率矩阵 | 显式物化 `float_log_prob_buf_` | 不全量物化，在线维护部分和 |
| 工作区大小 | 与 `batch*beam*vocab` 成正比（[BeamSearchLayer.cu:297-301](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L297-L301)） | 与 `batch*beam*BEAM*VOC_PARTS` 成正比（[OnlineBeamSearchLayer.cu:190-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L190-L192)） |
| BeamHypotheses 收集 | 仅 `diversity_rate==0` 且输出含 `beam_hyps` 时启用 | 始终启用（`output_log_probs` 直接进 beam_hyps） |
| 状态更新 | `invokeUpdateStates` | `invokeUpdate` |

#### 4.5.2 核心流程

`OnlineBeamSearchLayer::invokeSoftMax`（[OnlineBeamSearchLayer.cu:88-176](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L88-L176)）只做两步：

```
invokeSoftMax(output, input)
├── 填充 BeamHypotheses（含 log_probs_src、length_penalty、end_ids）
├── invokeTopkSoftMax   // 融合 softmax+topk+BeamHypotheses，输出 ids/cum_log_probs/output_log_probs
└── invokeUpdate        // 回填 parent_ids/sequence_length/finished/output_ids
```

注意它没有独立的「softmax 累加」步骤——累加发生在 `invokeTopkSoftMax` 内部（在线 softmax 在数值上等价于先全量 softmax 再累加，但用「边扫描边维护 top-k 部分和」的方式实现，故称 online）。

#### 4.5.3 源码精读

`invokeSoftMax` 把 `BeamHypotheses` 填得更完整：

[OnlineBeamSearchLayer.cu:129-143](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L129-L143) ——相比朴素版多了 `log_probs_src`（指向 `output_log_probs`）与 `end_ids`，因为在线版始终启用已完成 beam 收集，需要每步的对数概率来回溯路径。

融合 kernel 调用：

[OnlineBeamSearchLayer.cu:145-161](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L145-L161) ——`invokeTopkSoftMax` 直接吃原始 `logits`（注意第二参数 bias 传 `nullptr`，因为 bias 已在前一步 penalty 阶段处理），输出 `cum_log_probs`、`output_log_probs`、`ids`，并原地更新 `finished` / `sequence_length`。

工作区分配 [`OnlineBeamSearchLayer::allocateBuffer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L184-L197)：

[OnlineBeamSearchLayer.cu:188-196](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L188-L196) ——工作区按「最多支持 beam_width=64、每次检查 2×beam_width 候选、词表最多分 128 个 part」上界分配（`SMALL_TOP_K_SOFTMAX_MAX_VOC_PARTS=128`、`MAX_K=4`，[L21-L22](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L21-L22)）。即工作区与 `vocab_size` 无关、只与 `batch` 和固定上界有关，这正是融合实现的显存优势。

朴素版工作区对比 [BeamSearchLayer.cu:297-301](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L297-L301)：朴素版除 topk 工作区外还要 `sizeof(float)*batch*beam*vocab_size_padded` 的 log 概率缓冲，词表越大越费显存。

#### 4.5.4 代码实践

**实践目标**：对比两种实现的工作区大小依赖，理解「在线」为何省显存。

**操作步骤**：

1. 分别打开 [BeamSearchLayer::allocateBuffer](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L278-L303) 与 [OnlineBeamSearchLayer::allocateBuffer](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L184-L197)。
2. 列出两者 `reMalloc` 的字节数公式，标注哪些项依赖 `vocab_size`。
3. 代入一组真实数值：`batch=8, beam_width=4, vocab_size_padded=51200`，估算朴素版额外需要多少 MB 的 log 概率缓冲。

**需要观察的现象**：朴素版公式里有一项 `sizeof(float) * batch * beam * vocab_size_padded`；在线版公式里没有 `vocab_size` 因子。

**预期结果**：朴素版额外缓冲 ≈ \(4 \times 8 \times 4 \times 51200 \approx 6.5\) MB；在线版不随词表增长。大词表、大 batch 时在线版的显存与访存优势显著，这正是它成为默认后端的原因。

> 待本地验证：若你打开了 `FT_DEBUG_LEVEL=DEBUG`，可在 `allocateBuffer` 的 `FT_LOG_DEBUG(__PRETTY_FUNCTION__)` 后观察两次 forward 间的 `reMalloc` 是否命中 REUSE（承接 u2-l2）。

#### 4.5.5 小练习与答案

**练习 1**：在线版为何把 bias 参数传 `nullptr`（[OnlineBeamSearchLayer.cu:146](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L145-L161)）？

**参考答案**：bias 与 temperature 等修正已由 `BaseBeamSearchLayer::forward` 第一步的 `invokeAddBiasApplyPenalties` 原地写回 logits，到 `invokeSoftMax` 时 logits 已是修正后的，无需重复加 bias（`invokeTopkSoftMax` 第二参数传的就是 `(const T*)(nullptr)`，见 [OnlineBeamSearchLayer.cu:145-146](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L145-L146)）。这也体现了三步骨架里 penalty 与 topk 的清晰分工。

**练习 2**：朴素版与在线版都用到 `BeamHypotheses`，二者的启用条件有何不同？

**参考答案**：朴素版只在 `output_tensors` 含 `beam_hyps` **且** `diversity_rate==0` 时填充并使用 `BeamHypotheses`（[BeamSearchLayer.cu:224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.cu#L224)）；在线版只要 `output_tensors` 含 `beam_hyps` 就启用（[OnlineBeamSearchLayer.cu:130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.cu#L130)），且还会回填 `log_probs_src`，始终走「收集已完成 beam 后按归一化分数排序」的路径。

---

## 5. 综合实践

把本讲的知识串起来：模拟一次 `beam_width=4` 的单步 beam search，画出从 logits 到新 beam 的完整数据流。

任务要求：

1. **输入**：写出 `forward` 接收的 `input_tensors` 与 `output_tensors` 的关键字段（参考 [BaseBeamSearchLayer.cu:183-208](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L183-L208)）。
2. **penalty**：标注 `invokeAddBiasApplyPenalties` 会对 logits 做哪几种修正，以及各自的触发条件。
3. **选 token**：分别画出朴素版（`invokeLogProbAddCumLogProb` → `invokeTopkBeamSearch` → `invokeUpdateStates`）与在线版（`invokeTopkSoftMax` → `invokeUpdate`）的调用序列。
4. **4×4 收缩**：用 diversity 分支的 `beam_topK_kernel` + `batch_topK_kernel` 解释 16 个候选如何收缩回 4 条 beam，并指出 `updateStatesKernel` 里 `beam_id = (word_ids/vocab_size) % beam_width` 这一步的作用。
5. **length penalty**：给出 `apply_length_penalty` 的公式，解释 `BeamHypotheses.min_normed_scores` 在收集满 4 条已完成 beam 后如何决定是否替换最差的那条。
6. **间接表**：说明 `update_indir_cache_kernelLauncher` 写入的 `tgt_cache_indirection` 如何与下一步的 KV cache 读取（u6-l2）衔接。

完成后再回答一个开放问题：若你要新增一个「带约束的 beam search」（例如强制包含某词表子集），按本讲的模板方法结构，你只需要新增哪一层、重写哪个虚函数？为什么 `forward` 不用改？

## 6. 本讲小结

- FT 的 beam search 后端是「模板方法」：`BaseBeamSearchLayer::forward` 固化 penalty → `invokeSoftMax` → 间接表更新 三步骨架，子类 `BeamSearchLayer` / `OnlineBeamSearchLayer` 只重写 `invokeSoftMax`。
- penalty 阶段由 `invokeAddBiasApplyPenalties` 统一入口，按条件启动 bias/temperature、repetition/presence、min_length 三类修正，默认值相等的修正会被跳过。
- 朴素 `BeamSearchLayer` 显式物化 `[batch*beam, vocab]` 的 log 概率缓冲，流程是「softmax+累加 → 两阶段 topk → 状态更新」。
- topk 选择本质是「每条 beam 取 top-k → 跨 beam 合并收缩回 beam_width 条」；`beam_width=4` 时即 4×4=16 候选收缩回 4。不同 beam_width 用宏分别实例化以做编译期展开优化。
- length penalty 把累计 log 概率除以 \( \text{length}^{\,\text{length\_penalty}} \) 得到归一化分数，影响已完成 beam 在 `BeamHypotheses` 中的收集与替换决策。
- 在线 `OnlineBeamSearchLayer` 用 `invokeTopkSoftMax` 融合 softmax+topk+收集，工作区不再依赖 vocab_size，是大词表大 batch 场景的默认后端。

## 7. 下一步学习建议

- 本讲聚焦 beam search，u8-l3 会讲另一条解码后端——sampling（Top-K / Top-P），可与本讲的「确定性 topk 选择」对照，理解「为什么 sampling 不需要 cache_indirection 重排」。
- 想看 beam search 在端到端生成中如何被驱动，回到 u5-l2（`Decoding::forward`）与 u6-l1（`ParallelGpt::forward`），观察 `beam_width` 如何把控制流切到本讲的层。
- KV cache 的「按 beam 归属重排」细节在 u6-l2，本讲的 `tgt_cache_indirection` 正是那套间接寻址机制的写入端。
- 若对 kernel 级优化感兴趣，可深入阅读 `online_softmax_beamsearch_kernels.cu`（本讲只引用了它的头文件），看「在线 softmax + top-k」如何用单遍扫描维护部分和。
