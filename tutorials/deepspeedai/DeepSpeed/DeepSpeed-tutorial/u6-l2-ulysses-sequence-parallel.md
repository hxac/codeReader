# Ulysses 序列并行：DistributedAttention 与等分片防线

## 1. 本讲目标

学完本讲，你应该能够：

1. 推导 Ulysses（DeepSpeed 序列并行）对 attention 输入输出张量形状的完整变换：两次 all-to-all 如何把「序列切分、头完整」的布局变成「序列完整、头切分」的布局，再变回来。
2. 解释 Ulysses 为什么能把每个 rank 的注意力显存与通信量降到与 head 数 / SP 度相关，而不是与序列长度平方同阶放大。
3. 读懂 `_SeqAllToAll`、`single_all_to_all`、`_generate_layout_params` 这组通信原语的实现，理解「all-to-all 的反向还是 all-to-all」这一自共轭设计。
4. 说明 #9059 引入的等分片契约与两道防线：`_generate_layout_params` 在 scatter 前的整除断言（本地、零通信、总是生效），以及 `DistributedAttention(..., validate_shards=True)` 的 opt-in 元数据 all-gather 校验（跨 rank、在注意力数据交换之前于所有参与 rank 同步抛错、默认关闭零额外通信）。
5. 了解 auto_sp 自动判定器 `auto_wrap_model_for_sp` 的输入输出与边界（ViT 注意力自动包装、HuggingFace 风格 LLM 注意力只告警不包装）。
6. 知道 `DistributedAttention` 已从 `deepspeed.sequence` 包公开导出，而 `_SeqAllToAll` 保持私有。

## 2. 前置知识

在阅读本讲之前，你需要理解以下几个概念（不熟悉请先回顾 u3-l3 进程组管理）：

- **注意力计算的显存墙**：标准注意力要对 scores 矩阵做 softmax，scores 形状为 \([b, h, S, S]\)（batch、head、序列长、序列长），显存与计算量都是 \(O(b \cdot h \cdot S^2)\)。当序列长度 \(S\) 增大 10 倍，scores 显存增大 100 倍——这是长序列训练的第一面墙。
- **all-to-all 集合通信**：`dist.all_to_all_single(output, input)` 把 `input` 沿第 0 维等分成 P 份（P 为组内进程数），第 j 份发给组内第 j 个 rank，同时从每个 rank 收一份拼进 `output`。它是纯粹的「数据重排」：每个元素恰好被发送一次。
- **进程组（process group）**：`dist.new_group(ranks=[...])` 划出一个通信子集。序列并行只需要一个「序列并行组」，组内每个 rank 持有序列的一个分片。DeepSpeed 用 `groups._get_sequence_parallel_group()` 查询它（见 4.3 节）。
- **autograd.Function**：PyTorch 自定义可微算子的基类，需要实现 `forward` 与 `backward`。本讲会看到一个优雅的例子——all-to-all 的梯度仍然是 all-to-all。
- **为什么「报错时机」在分布式里是正确性问题**：如果一个 rank 在集合通信内部抛异常而其他 rank 还在等它，整个进程组会挂起（hang）。所以跨 rank 的校验必须让**所有**参与 rank 在进入数据通信**之前**一起拿到同样的错误信息。这是理解 #9059 设计的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `deepspeed/sequence/layer.py` | 本讲核心：`_SeqAllToAll`（autograd 封装）、`single_all_to_all`、`_generate_layout_params`（布局数学与整除断言）、`uneven_heads_all2all`（不均头路径）、`DistributedAttention`（用户接口，含 #9059 的 `validate_shards`） |
| `deepspeed/sequence/__init__.py` | 包导出：#9059 起公开导出 `DistributedAttention` |
| `deepspeed/sequence/auto_sp.py` | auto_sp 判定器入口 `auto_wrap_model_for_sp` |
| `deepspeed/sequence/autosp_detector.py` | 判定器的数据结构 `SPModelInfo` 与扫描函数 `detect_model_sp_info` |
| `deepspeed/utils/groups.py` | 序列并行组的查询接口（`_get_sequence_parallel_group` 等） |
| `docs/_tutorials/ds-sequence.md` | 官方教程：Megatron-DeepSpeed 接入方式与 `validate_shards` 契约说明 |
| `tests/unit/v1/sequence_parallelism/test_ulysses.py` | 本讲的测试锚点：all-to-all 往返一致性、SDPA 数值对照、不等分片拒绝、默认路径零元数据通信 |

## 4. 核心概念与源码讲解

### 4.1 Ulysses 的形状变换与通信量直觉

#### 4.1.1 概念说明

Ulysses（DeepSpeed 序列并行）解决的问题是：**序列太长时，单个 GPU 装不下一个头的完整 scores 矩阵**。

数据并行（DP）无法帮忙——DP 里每个 rank 拿不同的样本，序列维完全复制。Ulysses 的做法是把**序列维切分**给组内 P 个 rank：每个 rank 持有 \([b, S/P, h, d]\) 的激活（所有 h 个头、但只有 \(S/P\) 个 token）。

麻烦在于：注意力是「每个 token 看所有 token」的算子，序列被切碎后无法直接算。Ulysses 的关键观察是：**注意力的 head 维是天然可并行的**——不同的 head 之间互不通信。于是做一次维度的「互换」：

- 进注意力前：all-to-all 把「序列切分、头完整」换成「头切分、序列完整」；
- 注意力变成纯本地计算；
- 出注意力后：再一次 all-to-all 换回来。

#### 4.1.2 核心流程

设 SP 度为 \(P\)，全局序列长 \(S\)，batch 为 \(b\)，head 总数 \(h\)，head 维 \(d\)。以 batch-first 布局（`batch_dim_idx=0`）为例：

```text
输入（序列并行视图，每 rank）        [b, S/P, h,   d]   所有头 + 本地序列分片
        │ 第一次 all-to-all（scatter 头维=2, gather 序列维=1）
中间（头并行视图，每 rank）          [b, S,   h/P, d]   完整序列 + 本地头分片
        │ local_attn(q, k, v)          ← 注意力在这里，完全不出卡
输出（头并行视图，每 rank）          [b, S,   h/P, d]
        │ 第二次 all-to-all（scatter 序列维=1, gather 头维=2）
输出（序列并行视图，每 rank）        [b, S/P, h,   d]
```

数值与显存的收益：

- 每 rank 的 scores 显存从 \(O(b \cdot h \cdot S^2)\) 降为 \(O\!\left(b \cdot \frac{h}{P} \cdot S^2\right)\)——按 head 数被 P 摊薄；
- 通信量：all-to-all 中每个元素恰好被发送一次。第一次交换每 rank 约收发 \(\frac{bShd}{P}\) 个元素（Q、K、V 三份），第二次交换输出一份（同样大小），合计每次注意力层每 rank 通信量约为

\[\frac{4 \cdot b \cdot S \cdot h \cdot d}{P}\]

  与 SP 度 \(P\) 成反比，且**只发生在 Q、K、V、输出四个张量上**——注意力内部的二次方张量完全不参与通信。这就是「Ulysses 把通信量降到与 head 数相关」的含义：切分粒度由 head 维决定（要求 \(h\) 能被 \(P\) 整除，见 4.2 节），通信被 head 维摊薄而不是被序列平方放大。

#### 4.1.3 源码精读

整个变换由 `DistributedAttention.forward` 编排，我们先看它在代码里留下的两处形状注释：

- [deepspeed/sequence/layer.py:L566-L568](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L566-L568) 注释 `#in shape : e.g., [s/p:h:]` 标出进入时是「本地序列分片、全部头」；
- [deepspeed/sequence/layer.py:L607-L618](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L607-L618) 注释 `#out shape : e.g., [s:h/p:]` 与 `#out e.g., [s/p::h]` 标出中间态与最终返回形状，两次 `_SeqAllToAll.apply` 之间夹着本地注意力 `self.local_attn(...)`。

官方教程 [docs/_tutorials/ds-sequence.md:L39-L53](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ds-sequence.md#L39-L53) 给出了 Megatron-DeepSpeed 的最小接入示例：把自己的 `CoreAttention` 包进 `DistributedAttention`，forward 改为调用包装后的模块。文档同页声称 64 张 A100 上可训练长达 100 万 token 的序列。

#### 4.1.4 代码实践

**实践目标**：不用跑分布式，先在纸上（或 Python 里）验证你对形状变换的理解。

1. 取 \(P=4, b=2, S=8, h=8, d=16\)，手算三个视图下每个 rank 的张量形状。
2. 阅读测试 [tests/unit/v1/sequence_parallelism/test_ulysses.py:L173-L210](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L173-L210)（`TestUlyssesAll2AllLayout`）。这个类**不需要进程组**：它用 `_emulate_all_to_all`（CPU 版 all-to-all 模拟，L156-L161）驱动 `_generate_layout_params` 的布局数学，逐元素断言「序列并行分片 → 头并行分片」与反向都精确等于用 `narrow` 切出的参考分片。
3. 用如下方式在本地跑它（纯 CPU，无需 GPU/NCCL）：

```bash
cd <仓库根目录>
CUDA_VISIBLE_DEVICES='' DS_ACCELERATOR=cpu PYTHONPATH="$PWD" \
  python -m pytest -q "tests/unit/v1/sequence_parallelism/test_ulysses.py::TestUlyssesAll2AllLayout" -v
```

**需要观察的现象**：`batch_dim_idx` 0/1 与 `seq_world_size` 2/4 的全部参数化组合通过；每个用例断言 `torch.equal`（逐位相等，不是容差近似）。

**预期结果**：8 个用例全部通过——布局变换是纯重排，不改动任何数值。若本机无法运行，此步骤为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：\(S=16384\)、\(h=32\)、\(P=8\) 时，Ulysses 把每 rank 的 scores 显存降低到几分之一？

**答案**：\(h/P = 4\) 个头，scores 从 \(b \cdot 32 \cdot S^2\) 降为 \(b \cdot 4 \cdot S^2\)，即原来的 \(1/8\)。

**练习 2**：如果 \(h=7\)、\(P=4\)，head 维还能整除切分吗？DeepSpeed 怎么处理？

**答案**：不能整除。`single_all_to_all` 会在 `num_kv_heads % seq_world_size != 0` 时转入 `uneven_heads_all2all` 不均头路径（见 4.2.3），按 `[2,2,2,1]` 这类不均分片交换。

**练习 3**：为什么第二次 all-to-all 的 scatter 维与第一次相反？

**答案**：第一次 scatter 头维（把头分发给拥有对应序列分片的 rank）、gather 序列维；交换后 rank 持有完整序列、本地头。注意力输出仍是「完整序列、本地头」布局，要回到「本地序列、全部头」就必须把序列维 scatter 出去、把头维 gather 回来——两个维度角色互换。

### 4.2 all_to_all 通信原语：_SeqAllToAll 与布局参数生成

#### 4.2.1 概念说明

`_SeqAllToAll`（torch.autograd.Function）是 Ulysses 唯一的通信算子。它的核心设计是**自共轭**：all-to-all 是一个置换操作，它的逆操作还是 all-to-all（scatter 维与 gather 维互换即可）。于是：

- forward：一次 all-to-all；
- backward：把上游梯度再做一次 scatter/gather 互换的 all-to-all，正好把梯度送回数据来的地方。

布局数学（reshape/permute 的下标推导）被抽出成纯函数 `_generate_layout_params`，这让「算形状」可以脱离进程组单测（4.1.4 正是这么做的）。

#### 4.2.2 核心流程

`single_all_to_all(input, scatter_idx, gather_idx, batch_dim_idx, group)` 的执行过程：

1. 解析 KV head 数（`_resolve_kv_heads`，仅在 scatter 方向能从头张量读出）；
2. 若 `num_kv_heads` 不能被 SP 度整除 → 转 `uneven_heads_all2all`（显式 split sizes 的 all-to-all）；
3. 否则调 `_generate_layout_params` 得到四元组：`pre_all2all_permute_idx / pre_all2all_inp_shape / post_all2all_permute_idx / post_all2all_res_shape`；
4. `pre_all2all_fun`：reshape + permute，把要 scatter 的维度折叠到第 0 维的「rank 槽位」上；
5. `dist.all_to_all_single` 等分交换第 0 维；
6. `post_all2all` 返回的闭包做 permute + reshape，还原成目标布局。

以「scatter 序列维」方向（scatter_idx<2、batch-first）为例，`input=[b, S, h/P, d]`（本 rank 持有全局序列）：

```text
reshape  → [b, P, S/P, h/P, d]      把序列维显式拆成「rank 槽 × 本地长」
permute  → [P, b, S/P, h/P, d]      rank 槽转到 dim 0（all_to_all_single 等分这里）
all-to-all → 每个 rank 拿到所有 rank 的第 r 块
permute  → [b, S/P, P, h/P, d]
reshape  → [b, S/P, h, d]           P 份头拼回完整 head 维
```

#### 4.2.3 源码精读

- [deepspeed/sequence/layer.py:L49-L94](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L49-L94) `_generate_layout_params` 全文。注意它的四个分支：`batch_dim_idx`（0=batch 在前，1=序列在前）× `scatter_idx<2`（scatter 序列）或 `scatter_idx>=2`（scatter 头）。**#9059 在两个「scatter 序列」分支里各加了一条整除断言**：
  - [deepspeed/sequence/layer.py:L56-L59](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L56-L59)（batch-first 分支）解包出 `global_seq_len` 后立即断言 `global_seq_len % seq_world_size == 0`，否则抛出带具体数值的 `AssertionError`；
  - [deepspeed/sequence/layer.py:L75-L78](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L75-L78)（seq-first 分支）同样的断言。
  
  为什么必须在这里断言？因为若不整除，下一步 `input.reshape([bs, P, S/P, ...])` 会抛出一条难懂的形状错误，掩盖真实原因（测试 [tests/unit/v1/sequence_parallelism/test_ulysses.py:L954-L959](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L954-L959) 的 docstring 就写着「fail clearly before a reshape can obscure the error」）。相反，「scatter 头」方向的整除检查（`num_total_head % seq_world_size`，L67/L88）是历史已有的。
- [deepspeed/sequence/layer.py:L276-L313](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L276-L313) `single_all_to_all`：L292-L294 判定不均头路径（且断言其不支持 `async_op`，避免把均分模型也推上慢路径）；L296-L303 生成布局、预处理、发起 `all_to_all_single`；L305-L309 是 overlap 通信的异步分支，把 work 句柄挂到 handle 上延迟到反向再等待。
- [deepspeed/sequence/layer.py:L163-L273](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L163-L273) `uneven_heads_all2all`：当 head 数不能被 P 整除（如 7 头 4 卡分成 `[2,2,2,1]`）时，用 `get_shard_size_list`（AutoTP 的切分助手）算出每个 rank 的头数，再以显式 `input_split_sizes/output_split_sizes` 调 `all_to_all_single`。
- [deepspeed/sequence/layer.py:L377-L468](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L377-L468) `_SeqAllToAll`：L400 在 forward 里解析并缓存 `num_kv_heads` 到 ctx（反向看到的张量已是分片后的，读不出总数，只能重放）；L462-L468 的 backward 把 `ctx.gather_idx` 与 `ctx.scatter_idx` 互换后再 `apply` 一次自身——这就是自共轭。
- 顺带一提 [deepspeed/sequence/layer.py:L332-L374](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L332-L374)：还有一个注册为 `deepspeed::dim_zero_all_to_all` 自定义算子的 `_dim_zero_all_to_all`，给 `torch.compile` 提供可追踪的反向（普通 autograd.Function 会被内联导致梯度为零），本讲不展开。

#### 4.2.4 代码实践

**实践目标**：直接驱动 `_generate_layout_params`，验证不可整除的序列长度在 scatter 前被断言拒绝。

1. 精读 [tests/unit/v1/sequence_parallelism/test_ulysses.py:L954-L959](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L954-L959) `test_sequence_scatter_rejects_indivisible_length`：构造长度 5 的序列、SP 度 2，参数化两种 batch 布局。
2. 本地复现（示例代码，单进程即可，不需要分布式）：

```python
import torch
from deepspeed.sequence.layer import _generate_layout_params

# batch-first：[b=2, s=5, h=2, d=8]，scatter 维=1（序列），SP 度=2
try:
    _generate_layout_params(1, 0, 2, torch.empty(2, 5, 2, 8))
    print("没有抛错——异常！")
except AssertionError as e:
    print(f"被整除断言拒绝: {e}")
```

3. 运行同款测试：

```bash
CUDA_VISIBLE_DEVICES='' DS_ACCELERATOR=cpu PYTHONPATH="$PWD" \
  python -m pytest -q tests/unit/v1/sequence_parallelism/test_ulysses.py \
  -k "test_sequence_scatter_rejects_indivisible_length" -v
```

**需要观察的现象**：异常消息里同时出现序列长度 5 与 SP 度 2（`Sequence length (5) must be divisible ... (2)`）。

**预期结果**：断言在 reshape 之前触发，错误信息可直接定位问题参数。手跑脚本与 pytest 均应看到上述消息（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`_generate_layout_params` 的整除断言为什么放在「scatter 序列」分支而不是「scatter 头」分支也要新加一条？

**答案**：scatter 头方向历史上已有 head 整除断言（L67/L88）；#9059 补的是反方向——rank 此刻持有**全局**序列（变量名 `global_seq_len`），要把它按 P 等分发给各 rank，长度不整除时后续 reshape 会失败，所以必须先断言。

**练习 2**：`_SeqAllToAll.backward` 为什么不需要重新推导布局？

**答案**：它把 ctx 里缓存的 scatter/gather 维度互换后再次 `_SeqAllToAll.apply`——all-to-all 的逆仍是 all-to-all，前向的布局代码自然复用。

**练习 3**：异步 overlap 路径（`async_op=True`）为什么在不均头路径上被直接断言拒绝？

**答案**：见 L292-L293：不均头实现依赖同步的显式 split sizes 交换，不支持 async_op；若把可整除的模型也推上该路径，会破坏 q/k 的 overlap 通信（测试 `test_even_count_keeps_the_fast_path_and_its_async_op` 固化了这一点）。

### 4.3 DistributedAttention：包装器主流程与公开导出

#### 4.3.1 概念说明

`DistributedAttention` 是用户直接接触的类：把**任意**本地注意力模块（`local_attn`，只要接受 `q, k, v`）包装成序列并行版本。它本身不含注意力数学，只负责「换布局 → 调本地注意力 → 换回来」，再加上两件事：

- **GQA 感知的 head 切分**：分组查询注意力（GQA）下 query 头数多于 KV 头数，切分必须按 KV 组进行，否则一个 query 头会与它的 KV 头分离；
- **可选的 overlap 通信**：传入 `sp_stream` 时用独立流把 q/k 的通信与计算重叠。

#9059 之后，这个类新增第三个职责：opt-in 的跨 rank 分片校验（4.4 节），并从 `deepspeed.sequence` 包公开导出。

#### 4.3.2 核心流程

`forward(query, key, value, batch_dim_idx, rotary_pos_emb=None, ...)`：

```text
0. （可选）validate_shards=True 时先做跨 rank 元数据校验   ← #9059，4.4 节
1. 从 key 张量解析 num_kv_heads（GQA 切分基准）
2. 三次 _SeqAllToAll：q、k、v 分别从「序列分片」换到「头分片」
   （sp_overlap_comm 时在此穿插流同步与 hook 注册）
3. 若提供 rotary_pos_emb，对 q/k 应用旋转位置编码
4. context = self.local_attn(q, k, v, ...)                ← 本地注意力
5. 一次 _SeqAllToAll：context 从「头分片」换回「序列分片」
6. 返回 [b, S/P, h, d]（或 seq-first 布局）
```

#### 4.3.3 源码精读

- [deepspeed/sequence/layer.py:L471-L490](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L471-L490) 类 docstring，逐参数说明。#9059 新增的 `validate_shards` 文档（L482-L489）把契约写得非常清楚：每个 forward 在注意力数据集合通信之前检查 Q、K、V 各自的序列长度在序列组内一致；每次 forward 增加一次元数据 all-gather 与 host 同步；默认关闭；**全组必须一致开启并参与每次 forward**（默认路径上不加通信就无法检测开关不一致）；只查序列长度不查其他形状/dtype/设备；Q 与 KV 长度可以互不相同；**不做任何 padding**——分片对齐、padding、attention 掩码与 loss 掩码全由调用方负责。
- [deepspeed/sequence/layer.py:L492-L518](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L492-L518) 构造函数：`validate_shards: bool = False`（L500）与 `self.validate_shards = validate_shards`（L509）。注意 `num_kv_heads` 按实例保存（L506-L508 注释：防止第二个不同 head 数的模型决定第一个模型的切分方式——测试 `TestUlyssesKVHeadCount` 固化了这个回归）。
- [deepspeed/sequence/layer.py:L581-L584](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L581-L584) num_kv_heads 的解析点：从 key 张量读出头数并注释了 GQA 理由——切分按 KV 组进行，query 头必须落在持有其 KV 头的 rank 上。
- [deepspeed/sequence/layer.py:L586-L596](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L586-L596) q/k/v 三次 all-to-all；[L613](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L613) 本地注意力调用；[L615-L616](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L615-L616) 输出的反向交换（注意这次传入的 scatter/gather 参数是互换的）。
- **公开导出**：[deepspeed/sequence/__init__.py:L13](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/__init__.py#L13) `from deepspeed.sequence.layer import DistributedAttention`——#9059 加入的一行。此后推荐写法是 `from deepspeed.sequence import DistributedAttention`；`_SeqAllToAll` 因带下划线保持私有，不应从包级导入。
- **序列并行组从哪来**：[deepspeed/utils/groups.py:L884-L911](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/utils/groups.py#L884-L911) 提供 `_get_sequence_parallel_world_size/_get_sequence_parallel_rank/_get_sequence_parallel_group` 三个查询函数，底层是 mesh device 或 mpu。引擎侧的接线在 [deepspeed/runtime/engine.py:L2245-L2256](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/runtime/engine.py#L2245-L2256)：`initialize` 时传 `sequence_parallel_size` 配置即可拿到 `ds_engine.seq_parallel_group`（用法见测试 `test_mesh_device_creation`，[tests/unit/v1/sequence_parallelism/test_ulysses.py:L96-L113](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L96-L113)）。

#### 4.3.4 代码实践

**实践目标**：写一个单机 4 进程的 DistributedAttention 示例，验证 Ulysses 输出与「完整 attention」逐元素一致（容差内）。模板来自 [tests/unit/v1/sequence_parallelism/test_ulysses.py:L780-L852](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L780-L852) `TestDistributedAttentionSDPA`（它在 SP=2 下做了同样的事）。

操作步骤（示例代码，需保存为 `ulysses_check.py`）：

```python
# 示例代码：torchrun --nproc_per_node=4 ulysses_check.py
import os
import torch
import torch.nn.functional as F
import deepspeed.comm as dist
from deepspeed.accelerator import get_accelerator
from deepspeed.sequence import DistributedAttention  # #9059 后的公开导出

def local_attention(q, k, v):
    # q/k/v: [b, S, h_local, d] —— 本地头、完整序列
    return F.scaled_dot_product_attention(
        q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)).transpose(1, 2).contiguous()

def main():
    dist.init_distributed()  # 或 torch.distributed.init_process_group("gloo"/"nccl")
    rank, world_size = dist.get_rank(), dist.get_world_size()
    group = dist.get_world_group()
    device = get_accelerator().current_device_name()
    b, S, h, d = 2, 8, 8, 16
    seq_dim = 1  # batch-first

    gen = torch.Generator().manual_seed(7384)
    inputs = [torch.randn(b, S, h, d, generator=gen).to(device) for _ in range(3)]
    weight = torch.randn(b, S, h, d, generator=gen).to(device)

    # 参考：单进程完整 attention（每个 rank 用相同输入算同样的参考）
    leaves = [t.detach().clone().requires_grad_() for t in inputs]
    ref_out = local_attention(*leaves)
    ref_grads = torch.autograd.grad((ref_out * weight).sum(), leaves)

    # Ulysses：按序列维切块，本 rank 只持一份
    def shard(t):
        return t.chunk(world_size, dim=seq_dim)[rank].contiguous().requires_grad_()

    local = [shard(t) for t in inputs]
    dist_attn = DistributedAttention(local_attention, group, scatter_idx=2, gather_idx=seq_dim)
    out = dist_attn(*local, batch_dim_idx=0)
    grads = torch.autograd.grad((out * shard(weight)).sum(), local)

    for got, want in zip((out, *grads), (ref_out, *ref_grads)):
        torch.testing.assert_close(got, shard(want), atol=2e-5, rtol=2e-5)
    print(f"[rank {rank}] OK: Ulysses 输出与完整 attention 一致")

main()
```

运行：`torchrun --nproc_per_node=4 ulysses_check.py`（CPU 上可用 `CUDA_VISIBLE_DEVICES='' DS_ACCELERATOR=cpu torchrun --nproc_per_node=4 ulysses_check.py`，后端为 gloo）。

**需要观察的现象**：4 个 rank 都打印 OK；输出与 QKV 梯度都在 `atol=2e-5` 容差内与单进程参考一致。

**预期结果**：全部通过——两次 all-to-all 是无损重排，数值差异只来自本地 SDPA 的浮点顺序。若本机没有多进程环境，此实践待本地验证，但可直接跑同款测试：`CUDA_VISIBLE_DEVICES='' DS_ACCELERATOR=cpu PYTHONPATH="$PWD" python -m pytest -q tests/unit/v1/sequence_parallelism/test_ulysses.py -k TestDistributedAttentionSDPA`。

#### 4.3.5 小练习与答案

**练习 1**：`DistributedAttention` 的默认参数 `scatter_idx=2, gather_idx=0` 对应哪种输入布局？

**答案**：seq-first 的 `[s, b, h, d]`（`batch_dim_idx=1`）：head 在第 2 维（scatter），序列在第 0 维（gather）。这正是 Megatron-DeepSpeed 的布局；batch-first 时应传 `gather_idx=1`（如 4.3.4 示例）。

**练习 2**：为什么 `num_kv_heads` 要「按实例」保存，而不是进程级全局变量？

**答案**：同一进程可能装多个模型（如冻结 teacher + 训练 student），头数不同。历史上它进程级缓存，第一个走不均路径的模型会决定后来所有模型的切分——`TestUlyssesKVHeadCount.test_second_model_does_not_reshard_the_first`（[tests/unit/v1/sequence_parallelism/test_ulysses.py:L683-L775](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L683-L775)）就是针对这个回归的测试族。

**练习 3**：`from deepspeed.sequence.layer import DistributedAttention`（旧教程写法）与 `from deepspeed.sequence import DistributedAttention` 都能用吗？

**答案**：都能。后者是 #9059 在 [deepspeed/sequence/__init__.py:L13](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/__init__.py#L13) 新增的公开导出，是现在的推荐写法；`_SeqAllToAll` 带下划线保持私有。

### 4.4 #9059 等分片契约：validate_shards 校验与两道防线

#### 4.4.1 概念说明

Ulysses 的隐含前提一直是「**每个序列组 rank 的序列分片等长**」。`all_to_all_single` 不带 split sizes 时假定各方数据等长——分片不等长时，依后端与版本不同会**挂起、报错或静默算错**（issue #7384 就是两卡不等长直接 hang 的复现）。而且不等长分片里每个 rank 只看得见自己的张量，**本地断言根本探测不到**。

#9059 由此建立两道防线：

| 防线 | 位置 | 通信开销 | 捕获的错误 | 默认状态 |
|------|------|----------|-----------|----------|
| 整除断言 | `_generate_layout_params`（4.2.3） | 零 | 本 rank 持有的全局序列不能被 P 整除 | 总是开启 |
| `validate_shards=True` | `DistributedAttention._validate_shard_lengths` | 每 forward 一次元数据 all-gather + host 同步 | 组内 rank 之间 Q/K/V 序列长度不一致 | 默认关闭 |

第二条防线的报错设计必须满足分布式约束：**所有参与 rank 在注意力数据交换之前一起抛 ValueError**。如果只有一个 rank 在集合通信内部报错，其余 rank 会卡死在 all-to-all 里。校验本身用的 `dist.all_gather` 是全组都完成的集合通信——每个 rank 拿到全组元数据后各自独立判定、各自抛错，天然同步。

#### 4.4.2 核心流程

`validate_shards=True` 时每个 forward 的前置步骤：

```text
1. 取序列维 seq_dim = 1 - batch_dim_idx
2. 打包长度向量 lengths = [q.shape[seq_dim], k.shape[seq_dim], v.shape[seq_dim]]
   （int64 张量，放在 query.device 上）
3. dist.all_gather(gathered, lengths, group=self.spg)   ← 全组完成，无 rank 落单
4. 逐个比较：Q 的长度在全组是否一致？K 呢？V 呢？        ← 三者分别比较，Q 与 K/V 可不同
5. 任一不一致 → 拼「每个 group rank 的长度明细」→ raise ValueError
   （此刻还没碰任何数据 all-to-all，所有 rank 一起失败，无人挂起）
```

关键契约细节（docstring 与 [docs/_tutorials/ds-sequence.md:L55-L60](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ds-sequence.md#L55-L60) 明确写入）：

- **opt-in 且默认零开销**：不开就不做任何元数据通信；
- **全组一致开启并参与每次 forward**：默认路径上不加通信就无法检测「有人开了有人没开」；
- **只校验序列长度**：形状其他维度、dtype、设备不在检查范围；
- **Q 与 KV 长度可互不相同**（交叉注意力里 Q、KV 序列长本就允许不同），但**各自**必须在组内一致；
- **不做 padding**：分片对齐、padding、attention 掩码、loss 掩码仍是调用方责任（曾有 PR 提议在 attention 内部补零，但补零 key 仍会参与 softmax、破坏归一化而被否——#9059 选择显式报错而非静默修补）。

#### 4.4.3 源码精读

- [deepspeed/sequence/layer.py:L520-L536](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L520-L536) `_validate_shard_lengths` 全文：L521 由 `batch_dim_idx` 推出序列维；L522-L524 把三个长度打成一个小张量；L525-L527 在 `self.spg` 上 all-gather（**用的是序列组而不是世界组**——多个独立 SP 子组可以各自用不同长度互不干扰，见下）；L529-L533 对 `('query', 'key', 'value')` 分别比对全组长度，不一致时把 `group rank {r}: {len}` 明细拼进消息；L534-L536 抛出 `ValueError`。
- [deepspeed/sequence/layer.py:L563-L564](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L563-L564) forward 的第一个动作就是这行可选校验——位于 q/k/v 任何一次 `_SeqAllToAll.apply`（数据交换）之前，这是「报错先于通信」的字面落点。
- 测试锚点（全部在 [tests/unit/v1/sequence_parallelism/test_ulysses.py](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py)）：
  - [L855-L904](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L855-L904) `test_equal_shards_match_sdpa`：开启校验后，等长分片（含 Q/KV 不等长的交叉注意力）输出与梯度仍和参考一致——校验不改变数值行为；
  - [L906-L935](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L906-L935) `test_unequal_shards_fail_before_data_exchange`：monkeypatch `dist.all_to_all_single` 为「一旦被调就失败」，证明不等长时错误发生在数据交换之前；参数化 mismatch 的张量（query/key/value）、两种 rank 顺序 `(3,2)`/`(2,3)`、两种 batch 布局，且**复用同一模块改元数据再调**，确保校验不会被第一次 forward 的结果缓存骗过；
  - [L937-L951](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L937-L951) `test_default_does_not_exchange_metadata`：monkeypatch `dist.all_gather` 为「一旦被调就失败」，证明默认路径（含 backward）零元数据通信；
  - [L962-L993](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L962-L993) `test_validation_uses_sequence_group_ranks`：world_size=4 建两个不相邻子组 `[0,2]`、`[1,3]`，两组各自长度不同也能通过；构造组内不等长时，报错明细用的是 **group rank**（组内编号）而非世界 rank——因为校验的 all-gather 发在序列组上。

#### 4.4.4 代码实践

**实践目标**：构造 SP=2 的不等长分片，验证开启 `validate_shards` 后所有 rank 在数据交换前收到带各 rank 长度明细的 ValueError；再确认默认路径不做元数据通信；最后确认校验用的是序列组而非世界组。

操作步骤（示例代码，保存为 `ulysses_validate.py`，参考测试 L906-L951 的写法）：

```python
# 示例代码：torchrun --nproc_per_node=2 ulysses_validate.py
import torch
import pytest  # 仅用其异常断言；也可手写 try/except
import deepspeed.comm as dist
from deepspeed.accelerator import get_accelerator
from deepspeed.sequence import DistributedAttention

dist.init_distributed()
group = dist.new_group(ranks=[0, 1])   # 简化：直接用整个世界当序列组
rank = dist.get_rank(group)
device = get_accelerator().current_device_name()

attn = DistributedAttention(lambda q, k, v: q + k + v, group,
                            scatter_idx=2, gather_idx=1, validate_shards=True)

# 两种 rank 顺序 × 两种 batch 布局，逐一触发
for batch_dim_idx in (0, 1):
    for my_len, peer_len in ((3, 2), (2, 3)):
        length = my_len
        shape = (2, length, 4, 8) if batch_dim_idx == 0 else (length, 2, 4, 8)
        q = torch.zeros(shape, device=device)
        try:
            attn(q, q, q, batch_dim_idx)
            raise RuntimeError(f"rank{rank}: 没有抛 ValueError——异常！")
        except ValueError as e:
            msg = str(e)
            assert "sequence lengths" in msg
            # 报错带各 group rank 的长度明细：两个 group rank 的长度都要出现
            assert f"group rank 0: {my_len}" in msg and f"group rank 1: {peer_len}" in msg
            print(f"[rank {rank}] ValueError: {msg[:120]} ...")
print(f"[rank {rank}] done")
```

然后按测试逐条验证契约：

1. **数据交换前报错**：monkeypatch 思路照抄 [tests/unit/v1/sequence_parallelism/test_ulysses.py:L917-L922](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L917-L922)——把 `dist.all_to_all_single` 换成 `pytest.fail`，确认它从未被调用。
2. **默认路径零元数据通信**：跑 [L937-L951](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L937-L951) `test_default_does_not_exchange_metadata`（monkeypatch `dist.all_gather`）：

```bash
CUDA_VISIBLE_DEVICES='' DS_ACCELERATOR=cpu PYTHONPATH="$PWD" \
  python -m pytest -q tests/unit/v1/sequence_parallelism/test_ulysses.py \
  -k "test_default_does_not_exchange_metadata" -v
```

3. **序列组而非世界组**：读（或跑）[L962-L993](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L962-L993) `test_validation_uses_sequence_group_ranks`，注意其中 `[0,2]` 与 `[1,3]` 两个子组各自持有长度 2 与 3（`length = 2 + world_rank % 2`），却都能通过校验——因为各自组内一致。

**需要观察的现象**：两个 rank **同时**打印 ValueError（没有 rank 卡死）；错误消息形如 `DistributedAttention requires equal sequence shards across ranks: query sequence lengths [group rank 0: 3, group rank 1: 2]`；默认路径测试通过（all_gather 从未被调）。

**预期结果**：上述全部符合即验证了「报错先于数据交换、全组同步、默认零开销、按序列组校验」四条契约。多进程脚本待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_validate_shard_lengths` 用 `dist.all_gather` 而不是 `dist.gather`（gather 到 rank 0 再广播）？

**答案**：目标不是「某人知道结果」，而是「每个 rank 都拿到全组元数据」。all_gather 让所有 rank 独立得出同一结论并同时抛错；gather 到单点再广播要两步通信，且单点抛错时其他 rank 仍在等广播，反而引入挂起风险。

**练习 2**：如果 rank0 构造了 `validate_shards=True` 的实例而 rank1 忘了开会怎样？

**答案**：rank0 的校验 all-gather 会等 rank1 参与——但 rank1 直接进入数据 all-to-all，两边集合通信对不上，轻则挂起。这正是 docstring 强调「全组必须一致开启并参与每次 forward」「默认路径上无法检测开关不一致」的原因：开关一致性是使用者的责任，不是库能免费验证的。

**练习 3**：Q 长度 3、K/V 长度 5、全组一致——校验会报错吗？

**答案**：不会。Q 与 KV 的长度允许互不相同（交叉注意力语义）；校验只要求**每个张量自己的**长度在组内一致。`test_equal_shards_match_sdpa` 参数化 `(q_len, kv_len)` 取 `(3, 2)` 等组合正是验证这一点。

### 4.5 auto_sp 自动判定器

#### 4.5.1 概念说明

`DistributedAttention` 要求输入是 `(query, key, value)` 三张量接口（Megatron 风格），而 HuggingFace 模型的注意力层是 `hidden_states` 单张量接口——两者不兼容，手工包装需要对每个模型改代码。`auto_sp` 判定器试图自动化这件事：

- **输入**：一个多模态模型 + 序列并行组；
- **输出**：原地替换后的同一模型——ViT 编码器的注意力层被换成序列并行版本 `UlyssesSPViTAttention`；
- **边界**：LLM 解码器注意力（HF 接口）只发告警不包装，需要用户手工配置；视觉-语言投影层同理。

判定器的核心数据结构是 `SPModelInfo`，扫描逻辑与包装策略分离：`detect_model_sp_info` 负责识别，`auto_wrap_model_for_sp` 负责决策。

#### 4.5.2 核心流程

```text
auto_wrap_model_for_sp(model, process_group)
  ├─ info = detect_model_sp_info(model)        # 按「模块类名注册表」递归扫描
  │    ├─ vit_attn_modules: [(dotted_name, module), ...]
  │    ├─ llm_attn_modules: [(dotted_name, module), ...]
  │    └─ vision_projection_module: (name, module) 或 None
  ├─ 若两类注意力都为空 → ValueError（提示往注册表加类名）
  ├─ 每个 ViT 注意力 → UlyssesSPViTAttention(module, group, has_cls_token=...)
  ├─ 每个 LLM 注意力 → logger.warning（接口不兼容，跳过自动包装）
  └─ 检测到视觉投影层 → logger.warning（Phase 2 未自动化，手工用 ModalityFusionSPAdapter）
  返回 model（原地修改后的同一对象）
```

#### 4.5.3 源码精读

- [deepspeed/sequence/auto_sp.py:L5-L30](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/auto_sp.py#L5-L30) 模块 docstring 里的标准用法：先 `deepspeed.initialize`，再取 `groups._get_sequence_parallel_group()`，最后 `auto_wrap_model_for_sp(model, process_group=sp_group)`——**调用时点在建模之后、训练开始之前**。
- [deepspeed/sequence/autosp_detector.py:L73-L81](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/autosp_detector.py#L73-L81) `SPModelInfo`：三个字段分别是 ViT 注意力 `(名字, 模块)` 列表、LLM 注意力列表、最外层视觉投影层（只记第一个，避免嵌套投影被重复包装，见 [L109-L113](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/autosp_detector.py#L109-L113)）。
- [deepspeed/sequence/autosp_detector.py:L89-L115](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/autosp_detector.py#L89-L115) `detect_model_sp_info`：按模块类名是否命中 `_VIT_ATTN_CLASSNAMES` / `_LLM_ATTN_CLASSNAMES` 注册表分类——**识别依据是类名而不是结构**，新模型需要把类名加进注册表。
- [deepspeed/sequence/auto_sp.py:L85-L94](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/auto_sp.py#L85-L94) ViT 包装循环：查 `_VIT_HAS_CLS_TOKEN` 注册表决定 `has_cls_token`（未知类默认 True，保守回退）。
- [deepspeed/sequence/auto_sp.py:L99-L113](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/auto_sp.py#L99-L113) LLM 只告警的理由写在注释里：HF `hidden_states` 接口与 `DistributedAttention` 的 Q/K/V 接口不兼容，静默包装会在第一次 forward 就出错，所以宁可逐层告警让用户手工配置。
- [deepspeed/sequence/auto_sp.py:L118-L124](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/auto_sp.py#L118-L124) 视觉投影层的 Phase 2 告警：跨模态融合层的 gather/scatter 目前需要手工包 `ModalityFusionSPAdapter`（同文件 L8-L9 从 `autosp_fusion` 导出 Llava/InternVL/Qwen2VL 三个适配器）。

#### 4.5.4 代码实践

**实践目标**：跟踪一次 `auto_wrap_model_for_sp` 的调用链，理解「判定器输出什么、包装器做什么」；有条件时在一个小型多模态模型上实际触发。

操作步骤（源码阅读型实践）：

1. 从 [deepspeed/sequence/auto_sp.py:L42-L80](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/auto_sp.py#L42-L80) 的函数签名与 ValueError 分支入手，回答：扫描结果为空时用户该改哪个文件？（答案：`autosp_detector.py` 的两个类名注册表。）
2. 打开 `deepspeed/sequence/autosp_vit.py`，找到 `UlyssesSPViTAttention.__init__`，确认它内部持有一个 `DistributedAttention` 实例与 `has_cls_token` 标志（CLS token 不参与序列切分的特殊处理）。
3. （可选，需多模态模型与多进程，待本地验证）仿照 docstring 的四行用法在 SP=2 下包装一个小型 ViT 模型，观察日志里 `AutoSP: wrapped N ViT attention layer(s)` 与 LLM 告警。

**需要观察的现象**：步骤 3 中日志先打印 ViT 包装数量，再对每个 LLM 注意力打印一条 warning；未识别任何注意力时抛 ValueError。

**预期结果**：与上述源码行为一致；无模型环境时完成步骤 1-2 的调用链笔记即可。

#### 4.5.5 小练习与答案

**练习 1**：判定器为什么对 LLM 注意力「只告警、不包装」？

**答案**：`_LLM_ATTN_CLASSNAMES` 里的类都是 HF `hidden_states` 接口，包装成 `DistributedAttention` 后第一个 forward 就会因接口不匹配出错——静默替换只会把错误推迟并变得更难排查，所以明确跳过并逐层告警。

**练习 2**：`detect_model_sp_info` 靠什么识别注意力层？新架构模型如何接入？

**答案**：靠模块类名命中 `_VIT_ATTN_CLASSNAMES` / `_LLM_ATTN_CLASSNAMES` 注册表；新模型把自己的注意力类名加进对应注册表（ValueError 的提示信息也是这么说的）。

**练习 3**：`auto_wrap_model_for_sp` 的调用时点有什么要求？

**答案**：模型已在正确设备上、在 `deepspeed.initialize` 之后、训练循环开始之前；进程组来自 `groups._get_sequence_parallel_group()`。函数原地修改并返回同一 model 对象。

## 5. 综合实践

把本讲内容串成一个「Ulysses 契约检查器」小项目（在 4.3.4 脚本基础上扩展，单机 2 进程即可）：

1. **正确性基线**（4.1/4.3）：SP=2、等长分片，验证 `DistributedAttention` 的输出与 QKV 梯度都和单进程完整 SDPA 一致（`torch.testing.assert_close`）。
2. **防线一生效**（4.2）：把全局序列长改成奇数（如 5）并直接调用 `_generate_layout_params(1, batch_dim_idx, 2, tensor)`，捕获并打印整除断言的错误消息，确认它出现在任何 reshape 报错之前。
3. **防线二生效**（4.4）：开 `validate_shards=True`，让 rank0 持 3 个 token、rank1 持 2 个，确认**两个 rank 同时**收到带 `group rank 0: 3, group rank 1: 2` 明细的 ValueError，且 monkeypatch 的 `dist.all_to_all_single` 从未被调用。
4. **默认零开销**（4.4）：关掉开关重跑第 3 步的等长场景，monkeypatch `dist.all_gather` 证明默认路径（含 backward）零元数据通信。
5. **写下结论**：用一段话回答「为什么两道防线缺一不可」——本地断言看不见别的 rank，跨 rank 校验要付出每次 forward 的元数据通信，所以后者必须是 opt-in。

## 6. 本讲小结

- Ulysses 用两次 all-to-all 在「序列切分/头完整」与「头切分/序列完整」两种布局间互换，注意力计算完全本地化；每 rank 的 scores 显存与通信量都按 head 维被 SP 度摊薄（通信 ≈ \(4bShd/P\) 个元素，仅涉及 Q/K/V/输出四个张量）。
- `_SeqAllToAll` 利用「all-to-all 的逆仍是 all-to-all」实现自共轭反向；布局数学是纯函数 `_generate_layout_params`，可脱离进程组在 CPU 上逐位单测。
- #9059 确立等分片契约的两道防线：`_generate_layout_params` 的整除断言（本地、零通信、总开）与 `DistributedAttention(..., validate_shards=True)` 的元数据 all-gather 校验（跨 rank、数据交换前全组同步抛 ValueError、默认关闭零开销、不做任何 padding）。
- 校验分别比较 Q/K/V 三者的序列长度且 Q 与 K/V 可不同；用的是**序列组**的 group rank；开关必须全组一致并参与每次 forward。
- auto_sp 判定器 `auto_wrap_model_for_sp` 自动包装 ViT 注意力、对 HF 接口的 LLM 注意力告警跳过，识别依据是模块类名注册表。
- `DistributedAttention` 已从 `deepspeed.sequence` 公开导出；`_SeqAllToAll` 保持私有。

## 7. 下一步学习建议

- **下一讲 u6-l8（FPDT 与 Ulysses 卸载）**：同一个 #9059 还给 `fpdt_layer.py` 带来了几何校验与因果尾部 padding——当序列连「分片后都装不下」时，FPDT 按全局 chunk 分块训练，是 Ulysses 的直接延伸。
- **u6-l7（VocabTP 损失算子）**：序列并行之后，损失计算也能按词表并行，与 AutoTP 的 `vocab_parallel_lm_head` 配合。
- **继续阅读源码**：`deepspeed/sequence/autosp_vit.py`（ViT 注意力的 SP 包装与 CLS token 特判）、`deepspeed/sequence/autosp_fusion.py`（跨模态融合适配器）、`deepspeed/runtime/sequence_parallel/parallel_state_sp.py`（SP 组的构建，`TestUlyssesCheckpointLoad` 用到它做检查点兼容）。
- **官方文档**：`docs/_tutorials/ulysses-offload.md`（FPDT 的 SP 全局损失归一化契约）与 `docs/_tutorials/ds-sequence.md` 的 validate_shards 段落。
