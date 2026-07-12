# 序列并行 Ulysses 与 Ring-Attention

## 1. 本讲目标

本讲聚焦 ms-swift 的「序列并行（Sequence Parallel, SP）」能力。学完后你应该能够：

- 理解为什么长文本训练的显存瓶颈集中在注意力，以及序列并行如何把这条序列**切分到多张卡**上协同计算。
- 看懂 `swift/sequence_parallel/` 这个模块的接入流程：`--sequence_parallel_size` 参数如何一路触发 `sequence_parallel.prepare()`，进而对 flash-attention、forward hook、loss 收集打上全局补丁。
- 区分并讲清两种互补的分片策略——**Ulysses（按头分片 + all-to-all）** 与 **Ring-Attention / zigzag（按序列分片 + 环形传 KV）**——以及 ms-swift 为什么把两者**组合**使用，从而解除「头数必须能被卡数整除」的硬约束。
- 能够用 `--sequence_parallel_size 2 --padding_free true --attn_impl flash_attn` 在长文本数据上跑训练，并对比开启前后的显存峰值。

本讲依赖 [u9-l1 分布式训练基础](u9-l1-distributed-training-basics.md)（torchrun 多卡启动、`get_dist_setting` 进程拓扑）与 [u4-l3 编码与 Packing 机制](u4-l3-encode-and-packing.md)（padding_free / varlen 的 `cu_seqlens`）。建议先读完这两篇再进入本讲。

## 2. 前置知识

### 2.1 为什么长文本训练吃显存

Transformer 单层前向的核心代价是自注意力。对一条长度为 \(L\)、hidden 维度为 \(d\)、注意力头数为 \(h\) 的序列，标准注意力的中间张量规模为：

\[
\text{attn matrix} \in \mathbb{R}^{h \times L \times L}
\]

显存随长度**平方**增长。`max_length` 从 8k 提到 65k，注意力矩阵面积放大约 66 倍。即便用 Flash Attention 把 \(L \times L\) 矩阵压成不落盘的 IO 感知算法，**K/V 缓存与 Q/K/V 投影本身的显存**仍随 \(L\) 线性增长，长文本很快就会把一张卡撑爆。

序列并行的核心思想：既然一条序列太长，那就**把同一条序列的不同部分分给多张卡**，每张卡只算序列的一段，再通过通信把结果拼回完整注意力。这与 u9-l1 讲的 DDP/DeepSpeed（按 **batch/参数** 切）正交——序列并行切的是 **序列维**。

### 2.2 两种切法：切头 vs 切序列

| 切法 | 切的是哪一维 | 通信原语 | 头数约束 |
|------|------------|---------|---------|
| **Ulysses** | 注意力头维 \(h\) | all-to-all | 头数必须能被 SP 卡数整除 |
| **Ring-Attention** | 序列维 \(L\) | 环形 P2P（轮流传 K/V） | 无头数约束 |
| **ms-swift 组合** | 先头后序列 | all-to-all + 环形 | 用 GCD 自动分配，约束被解除 |

后面三节会分别落到源码里讲清楚这两种切法。

### 2.3 关键术语速查

- **SP（Sequence Parallel）/ Ulysses 组**：按头分片的一组卡，组大小 `sp_world_size`。
- **RP（Ring Parallel）/ Ring 组**：按序列分片的一组卡，组大小 `rp_world_size`。
- **DP（Data Parallel）**：把不同 batch 分给不同组，组大小 `dp_world_size`。
- **`cu_seqlens`**：varlen/padding_free 模式下「序列边界」的累加索引，例如 `[0, 100, 300]` 表示 batch 内有两条序列，长度分别是 100 和 200。flash-attn varlen 内核靠它区分拼接在一起的多个序列。
- **zigzag（锯齿）**：把每条序列切成「前半 + 后半」两块分给 ring 中的对端，以保持因果掩码在环形传递中正确。

## 3. 本讲源码地图

本讲涉及的源码集中在 `swift/sequence_parallel/`，外加接入它的几处「调用方」：

| 文件 | 作用 |
|------|------|
| [swift/sequence_parallel/sequence_parallel.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py) | 核心编排器 `SequenceParallel` 单例：`prepare()` 接入、device mesh 划分、输入切分/拼回、flash-attn 全局补丁。 |
| [swift/sequence_parallel/ulysses.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py) | Ulysses 实现：`DistributedAttention` + all-to-all 头重排。 |
| [swift/sequence_parallel/ring_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ring_utils.py) | 环形通信 `RingComm`：异步 send/recv K/V。 |
| [swift/sequence_parallel/zigzag_ring_attn.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py) | zigzag Ring-Attention 前向/反向（借用 ring-flash-attention）。 |
| [swift/sequence_parallel/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/utils.py) | 损失/tensor 的可微 gather、`SequenceParallelSampler`/`Dispatcher`。 |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | 调用方：在 `_prepare_model_tokenizer` 里触发 `sequence_parallel.prepare()`。 |
| [swift/trainers/seq2seq_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py) | 训练器侧：每个 step 切分 labels、用 SP 专用损失聚合。 |
| [examples/train/sequence_parallel/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/sequence_parallel/) | 8 个可运行示例脚本（sft/dpo/grpo/emb/reranker/seq_cls/512k/qwen3.5）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **4.1 `sequence_parallel.prepare` 接入**——一个参数如何激活整套并行。
2. **4.2 Ulysses 分片**——按头切 + all-to-all。
3. **4.3 Ring-Attention / zigzag**——按序列切 + 环形传 KV。

---

### 4.1 `sequence_parallel.prepare` 接入

#### 4.1.1 概念说明

ms-swift 把序列并行做成一个**可选开关**：默认 `sequence_parallel_size=1`，框架退化为普通训练；一旦设成大于 1，pipeline 就会调用 `sequence_parallel.prepare(...)` 完成两件事：

1. **划分通信域（device mesh）**：把全部 GPU 划成「DP × RP × SP」三维，分别对应数据并行、环形并行、序列（Ulysses）并行。
2. **打全局补丁**：把 transformers 内部的 flash-attention 调用、模型 forward 的输入入口、MoE 辅助损失收集，**猴子补丁**替换成支持切分的版本。

这样做的好处是：模型代码本身完全不改，所有并行逻辑都集中在 `sequence_parallel.py` 里，对用户和上层 trainer 透明。

#### 4.1.2 核心流程

`prepare()` 的调用链很短。先看调用方——[swift/pipelines/train/sft.py:52-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L52-L54) 在加载完模型后判断开关：

```python
if args.sequence_parallel_size > 1:
    sequence_parallel.prepare(
        args.sequence_parallel_size, model=self.model, tokenizer=self.processor, padding_free=args.padding_free)
```

参数 `sequence_parallel_size` 定义在 [swift/arguments/base_args/template_args.py:138](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py#L138)，文档（L88-89）明确它目前支持 CPT / SFT / DPO / GRPO 四种任务。

进入 `prepare()` 本体（[sequence_parallel.py:334-367](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L334-L367)），它的骨架是：

```python
def prepare(self, sp_size, model, tokenizer, padding_free):
    self.num_heads = ...get_config_attr(model.config, 'num_key_value_heads')   # 优先取 KV 头数
    self.world_size = sp_size
    ...
    if not SequenceParallel._global_inited:        # 全局初始化只做一次
        self._init_device_mesh()                   # 1. 划分 DP×RP×SP 通信域
        self._prepare_flash_attn(llm_model)        # 2. 给 flash-attn 打补丁
        SequenceParallel._global_inited = True
    self._prepare_forward_hook(llm_model)          # 3. 给模型 forward 入口打 hook
    if model.model_info.is_moe_model:
        self._prepare_moe_aux_loss(llm_model)      # 4. MoE 模型额外收集 aux loss
    ...
    if self.rp_world_size > 1 and not self.padding_free:
        raise NotImplementedError(...'needs --padding_free true')   # 关键约束
```

几个要点：

- **头数取的是 `num_key_value_heads`**（GQA 模型的 KV 头数），因为 Ulysses 在 all-toall 时实际是切 K/V 的头；取不到才退回 `num_attention_heads`。这点直接决定了后面 SP/RP 的分配（见 4.2）。
- **全局补丁只打一次**：`_global_inited` 标志保证 flash-attn 的猴子补丁不会因为多次构造而重复替换。而 `_prepare_forward_hook` 则每次 `prepare` 都注册，因为它要把输入按当前序列长度切分。
- **Ring-Attention 强制要求 `padding_free`**：`rp_world_size > 1` 时若没开 `--padding_free true` 直接抛 `NotImplementedError`。原因留到 4.3 讲。

#### 4.1.3 源码精读：device mesh 怎么划

`_init_device_mesh()`（[sequence_parallel.py:635-656](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L635-L656)）是理解 SP 与 RP 如何分工的钥匙：

```python
_, _, world_size, _ = get_dist_setting()                       # 来自 torchrun 的总卡数
self.dp_world_size = world_size // self.world_size
self.sp_world_size = math.gcd(self.num_heads, self.world_size)  # SP 取头数与卡数的最大公约数
self.rp_world_size = self.world_size // self.sp_world_size      # 剩余的全给 Ring
```

这段代码的精髓在 `sp_world_size = gcd(num_heads, world_size)`：

- **SP 组大小被夹在「能整除头数」的最大值上**，保证 Ulysses 的头切分永远成立（头数一定能被 `sp_world_size` 整除）。
- **多出来的并行度全部丢给 RP**（Ring-Attention 按序列切，不碰头维）。

举个数值例子（设 KV 头数 = 8）：

| 总卡数 `world_size` | `gcd(8, world_size)` = SP | RP = world_size/SP | mesh 形状 |
|---|---|---|---|
| 8 | 8 | 1 | 纯 Ulysses |
| 4 | 4 | 1 | 纯 Ulysses |
| 16 | 8 | 2 | Ulysses(8) × Ring(2) |
| 6 | 2 | 3 | Ulysses(2) × Ring(3) |

第三、四行正是「组合模式」——后面 4.3 会解释，正是因为 RP 吸收了头数整除不了的余量，**组合时头数约束被解除**。

之后用 `init_device_mesh` 建成二维或三维 mesh（[L649-656](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L649-L656)），并通过 `_dim_group`/`_dim_rank` 暴露 `sp_group`/`rp_group`/`dp_group` 三个进程组（[L658-700](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L658-L700)），后续通信都围绕这三个组进行。

#### 4.1.4 源码精读：输入如何被切分

模型 forward 前，`pre_forward_split_hook`（[sequence_parallel.py:272-298](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L272-L298)）会把 `input_ids`/`position_ids`/`attention_mask` 等喂进 `pad_and_split_inputs`，后者（[L537-627](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L537-L627)）完成「先 pad 到能整除、再按 zigzag 顺序切分」：

```python
# sequence_parallel.py: pad 到能被 2*world_size 整除（ring 需要），再 split
input_ids = self.split(input_ids, dim=1, position_ids=real_position_ids)
labels = torch.roll(labels, shifts=-1, dims=-1)            # 对齐下一个 token 的预测目标
labels = self.split(labels, dim=-1, position_ids=real_position_ids)
```

`split()`（[L492-514](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L492-L514)）的纯 Ulysses 分支最直观——按 `sp_rank` 取第 rank 块；ring 分支则先用 `_split_packed` 做锯齿重排（见 4.3.3）。注意 `labels` 与 `loss_scale` 在切之前都做了 `torch.roll(shifts=-1)`，这与 u5/u4 讲过的「预测下一个 token」对齐方式一致，只不过在 SP 下是在切分前一次性 roll。

#### 4.1.5 代码实践：跟踪 prepare 的调用点

**实践目标**：在不真正起多卡训练的前提下，搞清「一个参数如何激活整套补丁」。

**操作步骤**：

1. 打开 [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L48-L54)，找到 `_prepare_model_tokenizer`，确认 `sequence_parallel.prepare(...)` 的调用条件是 `args.sequence_parallel_size > 1`。
2. 跳进 [sequence_parallel.py:334](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L334) 的 `prepare`，依次定位 `_init_device_mesh` → `_prepare_flash_attn` → `_prepare_forward_hook` 三个动作。
3. 在 `_init_device_mesh`（[L642-647](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L642-L647)）手算：假设你的模型 `num_key_value_heads=8`，分别对 `world_size ∈ {2, 4, 8, 16}` 算出 `sp_world_size` 与 `rp_world_size`。

**需要观察的现象**：你会看到 `world_size=16` 时 SP 仍是 8、RP 变成 2——这正是「头数约束把多余的并行度推给 Ring」的体现。

**预期结果**：与 4.1.3 表格一致。

**待本地验证**：真实 `num_key_value_heads` 请以你模型的 `config.json` 为准（如 Qwen2.5-3B 为 2，GQA 头数很少，组合模式几乎是必须的）。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `_prepare_flash_attn` 要用 `_global_inited` 标志保护，而 `_prepare_forward_hook` 不用？

> **答案**：flash-attn 的补丁是**模块级全局替换**（替换 `ALL_ATTENTION_FUNCTIONS` 字典、`masking_utils` 函数），重复替换会把「原始函数」指针覆盖掉、再也回不去，故必须只做一次；而 forward hook 注册在具体模型实例上，重建模型时需要重新挂，所以每次 `prepare` 都执行。

**练习 2**：若用户设 `--sequence_parallel_size 4` 但忘了加 `--padding_free true`，会发生什么？

> **答案**：若该配置下 `rp_world_size > 1`（即需要 ring-attention），`prepare` 末尾会抛 `NotImplementedError: ... needs --padding_free true`（[L365-367](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L365-L367)）；若纯 Ulysses（`rp_world_size==1`）则不报错，因为 Ulysses 不依赖 padding_free。

---

### 4.2 Ulysses 分片

#### 4.2.1 概念说明

Ulysses（出处：DeepSpeed）的思路是：**不在序列维切，而在注意力头维切**。

标准注意力里，每个头独立计算 \(Q_i K_i^\top\)，头与头之间互不通信。Ulysses 利用这一点，让 `sp_world_size` 张卡**每张只持有一部分头**。但问题来了：每张卡持有的只是「序列全长 × 部分头」，要算完整注意力，必须让每张卡拿到「部分序列 × 全部头」。这就需要一次 **all-to-all** 通信把数据「转置」一下。

#### 4.2.2 核心流程与数学

设序列长 \(L\)、头数 \(h\)、SP 卡数 \(P\)（满足 \(h \% P = 0\)）。每张卡初始持有 \(Q \in \mathbb{R}^{L \times (h/P) \times d}\)（序列满、头分片）。Ulysses 一次前向：

1. **all-to-all 转置**：通信后每张卡持有 \(Q' \in \mathbb{R}^{(L/P) \times h \times d}\)（序列分片、头满）。
2. **本地注意力**：每张卡在自己的 \(L/P\) 段上跑标准 flash-attention。
3. **all-toall 转置回**：把输出重新转回 \(L \times (h/P) \times d\)。

由于每张卡只处理 \(L/P\) 长度，注意力中间显存从 \(O(hL^2)\) 降到 \(O(h(L/P)^2) \cdot P\) 总和，单卡峰值随 \(P\) 线性下降。

#### 4.2.3 源码精读：all-toall 的形状编排

Ulysses 的实现核心是 [ulysses.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py)。`_generate_layout_params`（[ulysses.py:10-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L10-L28)）描述了转置前后张量如何 reshape/permute：

```python
# scatter_idx >= 2 分支（输入是 [bs, local_seq_len, num_total_head, head_dim]）
assert num_total_head % seq_world_size == 0, 'Number of heads must be divisible by sp size!'
pre_all2all_inp_shape  = [bs, local_seq_len, seq_world_size, num_total_head // seq_world_size, head_dim]
pre_all2all_permute_idx = (2, 0, 1, 3, 4)   # 把 seq_world_size 维提到最前，准备 all-to-all
post_all2all_res_shape = [bs, seq_world_size * local_seq_len, num_total_head // seq_world_size, head_dim]
```

注意那个 `assert`——**头数必须能被 SP 卡数整除**，这就是 Ulysses 的硬约束。`single_all_to_all`（[L56-71](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L56-L71)）调用 `dist.all_to_all_single` 完成实际通信：

```python
input_t = pre_all2all_fun(...)            # reshape + permute
output = torch.empty_like(input_t)
dist.all_toall_single(output, input_t, group=group)   # 一次集合通信
res = post_all2all_fun(output)            # permute + reshape 回去
```

它被包成可微的 `_SeqAllToAll`（[L74-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L74-L92)），**反向就是再 all-toall 一次**（scatter/gather 索引互换）——这是 Ulysses 反向传播几乎零额外成本的关键：

```python
@staticmethod
def backward(ctx, *grad_output):
    return None, _SeqAllToAll.apply(ctx.group, *grad_output, ctx.gather_idx, ctx.scatter_idx), None, None
```

#### 4.2.4 源码精读：DistributedAttention 的三段式

`DistributedAttention.forward`（[ulysses.py:110-147](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L110-L147)）把 Ulysses 和 Ring 串成一个统一管线，结构非常清晰：

```python
# gather ulysses first, ring-attention next
if sp_world_size > 1:                              # ① Ulysses: 头重排，all-to-all
    query_layer = _SeqAllToAll.apply(sp_group, query, scatter_idx, gather_idx)
    key_layer   = _SeqAllToAll.apply(sp_group, key,   scatter_idx, gather_idx)
    value_layer = _SeqAllToAll.apply(sp_group, value, scatter_idx, gather_idx)
...
context_layer = self.local_attn(query_layer, key_layer, value_layer, ...)   # ② 本地注意力
...
if sp_world_size > 1:                              # ③ Ulysses: 转置回
    output = _SeqAllToAll.apply(sp_group, context_layer, gather_idx, scatter_idx)
```

注释 `gather ulysses first, ring-attention next` 道出了组合顺序：**先 Ulysses 把头拼满，再（可选）Ring 把序列接力**。`local_attn` 在纯 Ulysses 时就是原始 flash-attention；当 `rp_world_size > 1` 时它被替换成 zigzag ring 版本（见 4.3）。

> 这段代码也解释了为什么 `prepare` 里要按 `gcd` 切分：`_SeqAllToAll` 内部的 assert 要求头数能被 `sp_world_size` 整除，而 `sp_world_size = gcd(...)` 天然满足。

#### 4.2.5 代码实践：观察 all-toall 的可微性

**实践目标**：理解 Ulysses 的反向传播为何几乎是免费的。

**操作步骤**：

1. 阅读 [ulysses.py:74-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L74-L92) 的 `_SeqAllToAll`。
2. 对比 `forward`（用 `single_all_to_all(input, scatter_idx, gather_idx)`）与 `backward`（用 `_SeqAllToAll.apply(..., gather_idx, scatter_idx)`）。
3. 思考：梯度流经 all-toall 后，方向是「再转一次回去」还是「求导出新算子」？

**需要观察的现象**：反向没有写任何新的求导公式，只是把 scatter/gather 索引对调再调一次 forward。

**预期结果**：all-toall 是一个「转置」算子，其雅可比的转置还是同一个转置，故反向 = 再 all-toall 一次。这就是 Ulysses 通信开销可预测、反向无需额外显存的根本原因。

**待本地验证**：若你想亲眼看到，可在单机多卡上构造 `[1, L, h, d]` 张量，对其调用 `_SeqAllToAll.apply` 前后，再用 `torch.autograd.gradcheck`（需 double 精度）验证梯度。

#### 4.2.6 小练习与答案

**练习 1**：为什么 Ulysses 的硬约束是「头数能被 SP 卡数整除」而不是「序列长能被整除」？

> **答案**：因为 Ulysses 在头维切分——每张卡拿 `num_heads / sp_world_size` 个完整的头。如果头数不能被卡数整除，就无法把头均分给各卡；而序列长只要在切之前 pad 到能整除即可，不构成结构性约束。

**练习 2**：GQA 模型（`num_key_value_heads` 很小，如 2 或 4）对纯 Ulysses 意味着什么？

> **答案**：`num_key_value_heads=2` 时，`sp_world_size = gcd(2, world_size)` 最多是 2，意味着纯 Ulysses 最多只能用 2 张卡做序列并行。要用更多卡，必须靠 Ring-Attention 接力（`rp_world_size > 1`），这正是 ms-swift 默认组合两者的原因。

---

### 4.3 Ring-Attention / zigzag

#### 4.3.1 概念说明

Ring-Attention（出处：Ring Attention with Blockwise Transformers）切的是**序列维**：把一条长序列切成 \(P\) 段，分给 \(P\) 张卡，每张卡只持有「1 段 Q + 1 段 K/V」。要算完整注意力，卡之间组成一个**环**，K/V 沿环逐跳传递，每张卡在收到每一段 K/V 时累积一次局部注意力。这样：

- **没有头数约束**——切的是序列不是头。
- 单卡 K/V 显存从 \(O(L)\) 降到 \(O(L/P)\)。
- 代价是 \(P\) 轮 P2P 通信，但可以和 attention 计算**重叠**。

**zigzag（锯齿）** 是一种具体的切分策略：不把序列简单等分成连续的 \(P\) 段，而是先把序列**对半切**，再把前半/后半交错分配。这样做的好处是：在 causal（因果）注意力下，每张卡只需在特定 step 计算特定 half，能精确复用 flash-attn varlen 内核、避免无谓计算。

#### 4.3.2 核心流程：环形 KV 传递

设 ring 大小为 \(P\)（即 `rp_world_size`），每张卡 rank \(r\)。前向主循环 \(P\) 步（[zigzag_ring_attn.py:334-381](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L334-L381)）：

```
for step in 0..P-1:
    异步把自己的 K,V 发给下一跳，同时接收上一跳的 K,V     # 重叠通信
    根据当前 step 与 rank 的关系，选 Q 的前半/后半与 K/V 算局部 attention
    用 update_out_and_lse 把局部结果累加进全局 out / lse
```

其中 `out / lse` 是在线 softmax 的标准累积量（`lse` = log-sum-exp），多块注意力结果用 log-sum-exp 公式合并：

\[
\text{out}_{\text{new}} = \sigma(\text{lse}_{\text{block}} - \text{lse}) \cdot \text{out}_{\text{block}} + \bigl(1-\sigma(\text{lse}_{\text{block}} - \text{lse})\bigr) \cdot \text{out}
\]

这部分在 `update_out_and_lse`（[L69-99](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L69-L99)）里实现，等价于 `lse_new = lse + log(1 + exp(lse_block - lse))` 的稳定数值形式。

#### 4.3.3 源码精读：环形通信 RingComm

[ring_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ring_utils.py) 的 `RingComm` 是一个极简的环形 P2P 通信器。构造时算好「发往下一跳、接收上一跳」的全局 rank（[L17-22](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ring_utils.py#L17-L22)）：

```python
self.send_rank = (self.rank + 1) % self.world_size
self.recv_rank = (self.rank - 1) % self.world_size
self.send_rank = dist.get_global_rank(self._process_group, self.send_rank)
self.recv_rank = dist.get_global_rank(self._process_group, self.recv_rank)
```

`send_recv_kv`（[L49-58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ring_utils.py#L49-L58)）把 K 和 V 的 isend/irecv 一起 `commit`，做到「发 K/V 与收 K/V 并行、且与计算可重叠」：

```python
def send_recv_kv(self, k, v, k_buffer=None, v_buffer=None):
    next_k = self.send_recv(k, k_buffer)
    next_v = self.send_recv(v, v_buffer)
    self.commit()           # 批量提交 4 个 P2P op（K/V 的 send+recv）
    return next_k, next_v
```

#### 4.3.4 源码精读：zigzag 的 half-index

zigzag 的精髓在于「前后半」索引。`get_half_index`（[zigzag_ring_attn.py:13-38](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L13-L38)）根据 `cu_seqlens`（varlen 序列边界）返回前半或后半的切片。前向循环里最关键的判断（[L363-377](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L363-L377)）：

```python
if step == 0:
    block_out, block_lse = forward(q, k, v, True, ...)            # causal=True，本地块
elif step <= comm.rank:
    k0, v0 = k[half_index0], v[half_index0]
    block_out, block_lse = forward(q, k0, v0, False, ...)         # 用对端前半，causal=False
else:
    block_out, block_lse = forward(q1, k, v, False, ...)          # 用自己的后半，causal=False
```

源码里那张注释表（[L338-360](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L338-L360)）画出了 `world_size=4`、共 8 块时每一步哪些块需要算、是否带 causal。这正是 zigzag 命名的由来——序列被锯齿状地分给环形中的对端，使得每一步都能精确地只算因果允许的交集。

整个前向/反向被封装成 `ZigZagRingFlashAttnVarlenFunc`（[L583-679](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L583-L679)，一个 `torch.autograd.Function`），由 [sequence_parallel.py:209-219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L209-L219) 的 `local_flash_attn._attention` 在 `rp_world_size > 1` 分支里调用：

```python
output = zigzag_ring_flash_attn_varlen_func(
    query, key, value,
    cu_seqlens=cu_seqlens, max_seqlen=max_seqlen,
    causal=module.is_causal, ...,
    group=self.rp_group)
```

#### 4.3.5 源码精读：为何 Ring 必须 padding_free

回看 `local_flash_attn._attention`（[sequence_parallel.py:222-235](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/sequence_parallel.py#L222-L235)）的 ring 分支，它依赖 `cu_seq_lens_q`/`position_ids` 来描述「拼接后的多条序列边界」。这只有 **padding_free（varlen）** 模式才有——常规 batch 模式下每条序列被 pad 到等长，注意力里混着大量 pad token，zigzag 切分与 `cu_seqlens` 就对不上了。这就是 `prepare` 末尾那句 `rp_world_size > 1 需 --padding_free true` 的根因。这也呼应了 u4-l3 讲过的「packing 必然强制开启 padding_free」——序列并行同样建立在 varlen 之上。

#### 4.3.6 源码精读：反向也要环形

反向传播（[zigzag_ring_attn.py:388-580](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L388-L580)）比前向复杂得多：它需要先**重算前向**保存每步的 `out_lse`，再用 `lse_grad`（[L278-302](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L278-L302)）把全局 dout 反推成每块的 `grad_block_out/grad_block_lse`，最后再环形传递 `dk/dv`。两个 `RingComm`（`kv_comm` 与 `d_kv_comm`）分别在前向 KV 和反向 dKV 上做环传。这部分是借用 [ring-flash-attention](https://github.com/zhuzilin/ring-flash-attention) 的成熟实现，本讲只要求读者理解「反向同样走环形、且需要重算前向」这一结构。

#### 4.3.7 组合时头数约束为何被解除（实践任务的核心）

现在回答本讲实践任务要解释的问题。把 4.1 与 4.3 串起来：

- **纯 Ulysses** 的约束是 `num_heads % world_size == 0`（[ulysses.py:20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/ulysses.py#L20)）。若头数小于卡数（GQA 常见），纯 Ulysses 直接不可用。
- **组合模式** 下，`sp_world_size = gcd(num_heads, world_size)` 把 Ulysses 部分限制在「能整除头数」的最大值，剩下的 `rp_world_size = world_size / sp_world_size` 由 Ring-Attention 承担。Ring 切的是序列维（[zigzag_ring_attn.py:209-219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L209-L219)），**完全不碰头维**，所以对头数没有要求。

于是「头数必须能被卡数整除」这条约束，被「多余的并行度转给 Ring」这一机制**自动解除**。例如 `num_key_value_heads=2`、`world_size=8`：`gcd=2`，SP=2（Ulysses 用 2 卡，每卡 1 个头），RP=4（Ring 用 4 组、每组 2 卡接力），总共仍用满 8 卡，而纯 Ulysses 在这种配置下根本跑不起来。

#### 4.3.8 代码实践：长文本 SP 训练显存对比

**实践目标**：用 `--sequence_parallel_size 2` 在长文本上训练，对比开启前后显存峰值，并验证组合模式解除头数约束。

**操作步骤**：

1. 准备一个长文本数据集（如 `AI-ModelScope/LongAlpaca-12k`），并准备一个 KV 头数较少的模型（如 Qwen2.5-3B，`num_key_value_heads=2`），以便观察组合模式。
2. 先跑**基线**（不开 SP，单卡或 2 卡 DDP）：

   ```bash
   # 基线：2 卡，不开序列并行，max_length 拉到接近显存上限
   NPROC_PER_NODE=2 swift sft \
       --model Qwen/Qwen2.5-3B-Instruct \
       --dataset 'AI-ModelScope/LongAlpaca-12k' \
       --tuner_type lora --target_modules all-linear \
       --torch_dtype bfloat16 \
       --max_length 32768 \
       --attn_impl flash_attn \
       --padding_free true \
       --per_device_train_batch_size 1 \
       --gradient_accumulation_steps 4 \
       --logging_steps 1
   ```
3. 再跑**序列并行版**（加 `--sequence_parallel_size 2`，其余参数尽量不变）：

   ```bash
   NPROC_PER_NODE=2 swift sft \
       --model Qwen/Qwen2.5-3B-Instruct \
       --dataset 'AI-ModelScope/LongAlpaca-12k' \
       --tuner_type lora --target_modules all-linear \
       --torch_dtype bfloat16 \
       --max_length 32768 \
       --attn_impl flash_attn \
       --padding_free true \
       --sequence_parallel_size 2 \
       --per_device_train_batch_size 1 \
       --gradient_accumulation_steps 4 \
       --logging_steps 1
   ```
4. 对比两次训练日志里 `memory(GiB)` 字段（参考 [examples/train/sequence_parallel/sequence_parallel_qwen3_5.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/sequence_parallel/sequence_parallel_qwen3_5.sh) 的日志样例，`memory(GiB)` 就是峰值显存）。

**需要观察的现象**：

- 序列并行版的单卡峰值显存应明显低于基线（注意力中间张量被切到 2 卡）。
- 由于 Qwen2.5-3B 的 `num_key_value_heads=2`，`gcd(2, 2)=2`，此时 `sp_world_size=2`、`rp_world_size=1`，是纯 Ulysses。
- 把 `NPROC_PER_NODE` 提到 4（`sequence_parallel_size=4`）：`gcd(2,4)=2`，于是 SP=2、**RP=2**——框架自动切到组合模式。若把模型换成 `num_key_value_heads` 更大的模型，SP/RP 的分配会相应变化。

**预期结果**：

- `memory(GiB)` 随 `sequence_parallel_size` 增大而下降；长文本越长，下降越明显（因为注意力代价是 \(O(L^2)\)）。
- 在 `num_key_value_heads=2`、4 卡的例子里，训练能正常启动而非报「头数不能被卡数整除」——这就是组合模式解除约束的直接证据。

**待本地验证**：实际显存数字与本机 GPU 型号、flash-attn 版本强相关，请以本地实测为准；若 flash-attn 未装或 `attn_impl` 非 `flash_attn`，序列并行不会生效（补丁打不上）。

#### 4.3.9 小练习与答案

**练习 1**：Ring-Attention 的通信次数与 `rp_world_size` 是什么关系？为什么仍可能比朴素 attention 快？

> **答案**：每个 ring 组前向需要 `rp_world_size` 轮 P2P（`for step in range(world_size)`）。但每轮的 K/V send/recv 是异步的，可与本地 attention 计算**重叠**；同时每张卡的 K/V 显存降到 `1/rp_world_size`，使原本放不下的长序列变得可训练——速度的收益主要来自「能跑」与「显存不爆」，而非单步通信量减少。

**练习 2**：为什么 zigzag 要把序列对半切再交错分配，而不是直接连续等分？

> **答案**：在 causal 注意力下，序列前半的 Q 只需关注自己的 K/V，后半的 Q 才需要关注前半+自己。zigzag 让每个 rank 持有「前半 + 后半」的两块拼盘，配合 `half_index0/half_index1` 与每步的 causal 开关（[L363-377](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L363-L377)），可以精确复用 flash-attn varlen 内核、避免对 pad 或无效块的计算，从而兼顾正确性与效率。

---

## 5. 综合实践

把三个模块串起来，做一个「诊断 + 选型」的小任务：

**场景**：你拿到一台 8 卡机器，要训练 Qwen2.5-3B（`num_key_value_heads=2`）做 65k 长文本 LoRA 微调，参考 [examples/train/sequence_parallel/sequence_parallel.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/sequence_parallel/sequence_parallel.sh)（8×A100，`--sequence_parallel_size 8`）。

**任务步骤**：

1. **预测分配**：手算 `gcd(num_key_value_heads=2, world_size=8)`，写出 `sp_world_size` 与 `rp_world_size`，判断这台机器上跑的是纯 Ulysses 还是组合模式。
2. **解释约束解除**：用 4.3.7 的结论，说明为什么 `num_key_value_heads=2` 时纯 Ulysses 顶多用 2 卡，而这里却能用满 8 卡——多余的并行度去了哪里？
3. **核对参数**：打开示例脚本，确认它同时带了 `--padding_free true` 和 `--attn_impl flash_attn`，并用本讲源码解释这两个参数缺一不可的原因（前者满足 ring 的 varlen 要求，后者是补丁挂载点）。
4. **解读日志**：参考 [sequence_parallel_qwen3_5.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/sequence_parallel/sequence_parallel_qwen3_5.sh) 末尾的日志样例（`memory(GiB)` ≈ 68），体会 8×80GiB 跑 65k 长文本时显存仍很紧绷——序列并行是把这件事变得「可行」的关键。
5. **(可选) 跑通**：在有足够显存的机器上，用更小的 `max_length`（如 8192）跑一次 `--sequence_parallel_size 2` 与不开 SP 的对比，记录 `memory(GiB)`。

**预期产出**：一段说明文字 + 一张 SP/RP 分配表，能清晰回答「为什么这台机器需要组合 Ulysses 与 Ring-Attention，且为什么必须开 padding_free 与 flash_attn」。

> **待本地验证**：第 5 步的实际显存与速度以本地 GPU 为准。若无 8 卡环境，可只完成 1-4 步的源码阅读与推演。

## 6. 本讲小结

- 序列并行切的是**序列/头维**，与 u9-l1 的 DDP/DeepSpeed（切 batch/参数）正交，专治长文本训练的注意力显存爆炸。
- ms-swift 用一个开关 `--sequence_parallel_size` 激活：pipeline 在 [sft.py:52-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L52-L54) 调 `sequence_parallel.prepare()`，完成 device mesh 划分 + flash-attn / forward hook / MoE loss 的全局补丁。
- **Ulysses** 在头维切分，靠 all-toall 把「序列满/头分」转成「序列分/头满」，反向近乎免费（反向 = 再 all-toall 一次），硬约束是「头数能被 SP 卡数整除」。
- **Ring-Attention / zigzag** 在序列维切分，靠环形 P2P 传 K/V + 在线 softmax（`update_out_and_lse`）累加，无头数约束，但要求 `padding_free`（varlen 的 `cu_seqlens`）。
- **组合**：`sp_world_size = gcd(num_heads, world_size)`，多余的并行度交给 `rp_world_size`。正是这一步**解除**了「头数必须被卡数整除」的约束——头数不够时，Ring 接力补足。
- 反向传播同样走环形并需重算前向；MoE 模型的 aux loss、logits/labels 也经 `GatherLoss`/`GatherTensor`（[utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/utils.py)）按可微方式拼回全长。

## 7. 下一步学习建议

- **进入 Megatron 体系**：序列并行是 Megatron 大并行（TP/PP/CP/EP）中的一块拼图。建议接着读 [u9-l3 Megatron-SWIFT 架构总览](u9-l3-megatron-swift-overview.md)，看 Megatron 如何把 TP（张量并行）与 SP/CP（上下文并行）组合成完整方案。
- **读相关源码**：
  - 想深入 Ring-Attention 的反向细节，精读 [zigzag_ring_attn.py:388-580](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/zigzag_ring_attn.py#L388-L580) 与上游 [ring-flash-attention](https://github.com/zhuzilin/ring-flash-attention)。
  - 想理解 SP 下损失如何正确聚合，读 [utils.py 的 GatherLoss](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/sequence_parallel/utils.py#L30-L62) 与 [trainers/utils.py 的 per_token_loss_func_sp](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/utils.py#L142-L171)。
  - 想看 SP 在 RL/评测/分类任务里的用法，浏览 [examples/train/sequence_parallel/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/sequence_parallel/) 下 8 个脚本（grpo / dpo / emb / reranker / seq_cls / 512k 等）。
- **动手验证**：在 2 卡机器上跑一次 4.3.8 的对比实践，亲眼看到 `memory(GiB)` 下降，是巩固本讲概念最直接的方式。
