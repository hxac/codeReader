# u3-l5 · elementwise、embedding 与采样：最贴近上手的三族共享算子

前几讲我们解剖了 ops.rs 的包装骨架（u3-l1）、cuBLAS 线性层（u3-l2）和 paged 注意力（u3-l4）。本讲换一个视角：挑三族「小而完整」的共享算子——token embedding 查表、贪心 argmax、FlashInfer top-k/top-p 采样，外加 softcap/gelu 两个 elementwise 代表——把「一个简单 CUDA kernel + 它的 wrapper + 它在 KERNELS.md 里的登记」这条最小闭环完整走一遍。这三族算子是读者日后动手写第一个 kernel 时最好的参照物：逻辑简单、无 MoE/TP 复杂度、测试齐全。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 embedding_batch 的输入输出约定：token id 是 u32、输出是「token i 连续存于偏移 i × hidden」的列主序布局，并解释 vec4 向量化路径的启用条件与标量回退。
2. 独立读懂 argmax 家族的三种形态（单行、批式单块、两阶段 split），并解释「最低全局索引赢平局、NaN 永不赢」这条全序为什么靠 argmax_better 的严格大于实现。
3. 理解 gpu_sample_batch_into 的按行过滤设计：为什么贪心行必须走 argmax 路径、min_p 行为什么要分区成独立一遍、两个 philox seed 为什么要异或去相关。
4. 掌握 softcap/gelu 家族「f32 内部计算、单次舍入回 bf16」的位级等价约定，以及 launcher 侧 vec8/vec4 的自动分派。
5. 对以上每个 op，都能从 KERNELS.md 的 op_id 一路路由到 Rust wrapper、FFI symbol 与 csrc 源文件。

## 2. 前置知识

### 2.1 解码的两条路：贪心与随机采样

语言模型每一步输出的是整个词表上的 logits 向量 \( x \in \mathbb{R}^{V} \)。把它变成下一个 token 有两条路：

- 贪心（greedy）：直接取 \( \operatorname{argmax}_i x_i \)，确定性的。
- 随机采样（sampling）：先做温度 softmax

\[ p_i = \frac{\exp(x_i / T)}{\sum_j \exp(x_j / T)} \]

再从分布 \( p \) 中抽一个样本。温度 \( T \) 越低分布越尖：当 \( T \to 0 \) 时质量集中在最大 logit 上，采样结果收敛到贪心结果——这是本讲综合实践要亲手观察的现象。工程上 PegaInfer 不靠把温度设成 0 来做贪心，而是让贪心行走专用 argmax kernel：采样 kernel 的契约明确写着 temperature 必须 > 0，greedy rows belong on the argmax path, not here。
