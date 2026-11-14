# FPDT 与 Ulysses 卸载：超长序列分块训练

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 FPDT（Fully Pipelined Distributed Transformer，全流水线分布式 Transformer）解决什么问题：把超出单卡显存的超长序列按全局 chunk 切块、逐块做前向与反向，并用主机内存双缓冲暂存中间 chunk，让「整段序列装不进显存」的训练成为可能。
2. 追踪 `FPDT_InputConstruct` 的分块几何：全局序列长度 \(S\)、全局 chunk 大小 \(C\)（`ds_sequence_parallel_fpdt_chunk_size`）、SP 并行度 \(P\) 与 rank 四者如何决定每个 rank 分到哪些 token，以及 #9059 新增的几何校验与因果尾部 padding 规则。
3. 读懂 `_FPDTGPUAttentionImpl_` 与 `_FPDTGPUOffloadingAttentionImpl_` 两条分块注意力路径：分块因果注意力的数学、流式 softmax 合并、自定义 backward，以及 `SequenceChunk` 双缓冲如何把 chunk 在 GPU 与 pinned 主机内存之间搬运。
4. 掌握 `FPDT_LogitsLoss` 的分块损失计算与「SP 全局损失归一化」契约：为什么零权重位置必须先把损失置零（`inf * 0 = NaN`）、为什么必须用全局权重和归一化（允许某个 rank 一个有效 token 都没有）。
5. 了解 `ds_sequence_parallel_fpdt_*` 配置族的三个开关及其进入代码的路径。

## 2. 前置知识

本讲是 Ulysses 单元的收官篇，直接建立在以下认知之上（先回顾，再继续）：

- **序列并行与 Ulysses（u6-l2）**：Ulysses 用两次 all-to-all 在「序列切分、头完整」与「序列完整、头切分」两种布局间互换，让每个 rank 的注意力计算本地化。FPDT 的每个 chunk 内部**仍然复用这套 all-to-all 机制**（直接调用 `deepspeed/sequence/layer.py` 的 `single_all_to_all`）。一句话概括：FPDT = 逐 chunk 的 Ulysses + 跨 chunk 的流式 softmax + 主机卸载。
- **因果注意力（causal attention）**：第 \(i\) 个 token 只能注意不晚于 \(i\) 的 token。把序列切成若干 chunk 后，这个性质导出本讲最重要的结构规律：query chunk \(i\) 对 key chunk \(k\) 的计算只在 \(k \le i\) 时存在，且只有对角块（\(k = i\)）需要三角掩码，非对角块（\(k < i\)）是全量注意力。
- **自定义 autograd Function（承接 u2-l4 的训练循环知识）**：FPDT 的注意力、FFN、损失全部写成 `torch.autograd.Function` 子类，forward 里手工控制「保存什么、丢什么」，backward 里手工重算。这是显存优化的核心手段——PyTorch 默认自动求导会保留所有中间激活，而 FPDT 只留最少的东西。
- **pinned memory 与异步拷贝（u5-l4 已接触）**：页锁定（pin）的主机内存才能让 `copy_(non_blocking=True)` 的 H2D/D2H 拷贝真正异步，与独立 CUDA stream 配合构成双缓冲流水线。
- **flash-attention**：单块内的高效注意力内核。FPDT 直接使用其私有接口 `_flash_attn_forward` / `_flash_attn_backward`，要求 flash-attn >= 2.5（含 2.6.x / 2.7.x；2.7.0 起 `window_size` 拆成两个 int 参数、forward 返回值从 8 个减为 4 个，代码里做了分支适配）。
- **NLL 损失与 ignore_index**：`F.nll_loss` 通常用 `ignore_index=-100` 跳过无效标签。本讲会解释为什么 FPDT 的 padding 标签**不能**用 -100，而要用 0 加零权重。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/sequence/fpdt_layer.py](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py) | FPDT 全部核心实现（约 1390 行）：`FPDT_InputConstruct`（输入切分与 padding）、`_FPDTGPUAttentionImpl_` / `_FPDTGPUOffloadingAttentionImpl_`（两条分块注意力 autograd 路径）、`SequenceChunk`（双缓冲）、`FPDT_FFN`、`FPDT_LogitsLoss`、`FPDT_Attention`（用户入口） |
| [deepspeed/sequence/layer.py](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py) | Ulysses 基础原语：`single_all_to_all`、`apply_rotary_pos_emb`、`_generate_layout_params`（u6-l2 已精读，本讲看它们如何被逐 chunk 复用） |
| [docs/_tutorials/ulysses-offload.md](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md) | 官方教程：能力数字、`ds_sequence_parallel_fpdt_*` 配置族、padding 与 SP 全局损失归一化契约 |
| [tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py) | `FPDT_InputConstruct` 的纯 CPU 单测（本讲主要实践对象，无需 GPU 与进程组） |
| [tests/unit/v1/sequence_parallelism/test_ulysses.py](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py) | 分布式回归：offloading 输出一致性、padding 与未 padding 参考逐项一致（#9059）、GQA 反向 |

## 4. 核心概念与源码讲解

### 4.1 FPDT 定位与 ds_sequence_parallel_fpdt_* 配置族

#### 4.1.1 概念说明

长序列训练的显存墙不只是「参数有多大」，更是「激活有多大」：一条 \(S = 262144\)（256K）的样本，即使模型本身只有 6.7B，整段序列经过每层产生的 QKV、注意力输出、FFN 中间量也要按 \(S\) 线性堆在显存里。

FPDT（Ulysses-Offload 背后的系统）的思路是「**时间换空间**的流水线」：

1. 把全局序列切成若干大小为 \(C\) 的 **chunk**，前向与反向都逐 chunk 进行，任意时刻只保留当前正在计算的少数几个 chunk 的激活；
2. 每块内部的注意力复用 Ulysses 的 all-to-all（头切分、序列拼全），所以单块注意力仍是完整正确的；
3. 不在使用的 chunk 用 `SequenceChunk` 双缓冲搬到 pinned 主机内存，装载/卸载与计算在独立 CUDA stream 上重叠。

官方教程给出的能力数字：8B 模型 4 卡训 2M 上下文、70B 模型 32 卡训 4M 上下文，MFU 超过 50%（见 [docs/_tutorials/ulysses-offload.md:6](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L6) 与 [docs/_tutorials/ulysses-offload.md:12](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L12)）。FPDT 还可与 ZeRO 叠加——模型状态由 ZeRO 切分，激活由 FPDT 分块，两者互不冲突。

配置族（Megatron-DeepSpeed 脚本参数，不是 DeepSpeed JSON 配置）定义在教程中（[docs/_tutorials/ulysses-offload.md:31-33](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L31-L33)）：

| 参数 | 类型 / 默认值 | 含义 |
| --- | --- | --- |
| `ds_sequence_parallel_fpdt` | bool / false | 是否启用 FPDT |
| `ds_sequence_parallel_fpdt_chunk_size` | int / 65536 | 全局 chunk 大小 \(C\)：无论序列多长，FPDT 每次只处理 \(C\) 个 token |
| `ds_sequence_parallel_fpdt_offloading` | bool / false | 是否用主机内存卸载 chunk（决定走哪条注意力路径） |

这三个开关由 Megatron-DeepSpeed 侧消费：前两个决定模型代码是否实例化 `FPDT_Attention` 及其 `chunk_size`，第三个最终以 `cpu_offloading=True` 传入 forward（测试里用 `SimpleNamespace` + 关键字参数模拟，见后文 4.2.4）。DeepSpeed 侧真正读到的只有 `args.ds_sequence_parallel_fpdt_chunk_size`（`FPDT_InputConstruct` 构造参数）。

#### 4.1.2 核心流程

一次 FPDT 训练步骤的数据流：

```text
tokens [B, S, ...]
   │  FPDT_InputConstruct（可能 padding 到 S'，然后按 chunk 负载均衡切分）
   ▼
每个 SP rank 持 [B, S'/P] 的 token / labels / loss_mask（全局重排后的 mask 除外）
   │  transpose 成 [S'/P, B, H]（序列优先布局）
   ▼
FPDT_Attention（逐 chunk：QKV 投影、all-to-all、RoPE、分块 flash-attn、流式合并、all-to-all 回）
   ├── 全局 chunk 数为 1 或 cpu_offloading=False → _FPDTGPUAttentionImpl_（全驻 GPU）
   └── 否则 → _FPDTGPUOffloadingAttentionImpl_（SequenceChunk 双缓冲主机卸载）
   ▼
FPDT_FFN（逐 chunk 的 MLP + bias-GELU，backward 按块重算）
   ▼
FPDT_LogitsLoss（逐 chunk 投影词表 + NLL，allgather 成 [S', B] 的全局损失）
   ▼
使用方按「SP 全局损失归一化契约」加权求和（4.5 节）
```

#### 4.1.3 源码精读

用户入口是 `FPDT_Attention`。构造时先强制检查 flash-attn 可用性，然后由 forward 按「是否卸载 + 全局 chunk 数」选择实现路径：

[deepspeed/sequence/fpdt_layer.py:1149-1152](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1149-L1152)

```python
if _flash_attn_forward is None or _flash_attn_backward is None:
    raise ImportError(
        "DeepSpeed FPDT requires flash-attn (>=2.5, including 2.6.x and 2.7.x). Please install it with `pip install flash-attn --no-build-isolation`."
    )
```

这段代码在模块顶部 try-import 失败时直接报错：FPDT 的两条路径都离不开 flash-attn 内核，版本要求 >= 2.5。

[deepspeed/sequence/fpdt_layer.py:1175-1199](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1175-L1199)

```python
def forward(self, layernorm_output, attention_mask, inference_params,
            rotary_pos_emb, cpu_offloading=True) -> Tensor:
    self.num_chunks_attn = layernorm_output.shape[0] * dist.get_world_size(self.spg) // self.chunk_size

    if not cpu_offloading or self.num_chunks_attn == 1:
        output = _FPDTGPUAttentionImpl_.apply(...)
    else:
        output = _FPDTGPUOffloadingAttentionImpl_.apply(...)

    output = output.flatten(2).permute(1, 0, 2).contiguous()
    output = torch.matmul(output, self.qkv_dense_weight.t())
```

这段代码是两条路径的分发点：`layernorm_output.shape[0]` 是本 rank 的本地序列长（序列优先布局），乘上 SP 世界大小再除以全局 chunk 大小 `self.chunk_size`（即教程里的 \(C\)，构造默认 65536，见 [deepspeed/sequence/fpdt_layer.py:1145-1146](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1145-L1146)），得到**全局 chunk 总数**。`cpu_offloading=False` 或全局只有一个 chunk 时走 `_FPDTGPUAttentionImpl_`（省掉卸载开销）；否则走 `_FPDTGPUOffloadingAttentionImpl_`。注意力输出还要过一次输出投影（`qkv_dense_weight`）。

投影尺寸的推断在同文件（[deepspeed/sequence/fpdt_layer.py:1159-1162](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1159-L1162)）：`projection_size = kv_channels * num_attention_heads`，`kv_projection_size = kv_channels * num_key_value_heads`——**FPDT 是 GQA 感知的**，K/V 头数可以少于 Q 头数，这个信息会一路传给 all-to-all（见 4.3.3）。

#### 4.1.4 代码实践

**实践目标**：建立 FPDT 的「几何直觉」——给定 \(S, C, P\) 手算每个 rank 的切分结果，与代码推导一致。

**操作步骤**：

1. 阅读 [docs/_tutorials/ulysses-offload.md:59-66](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L59-L66) 的 Megatron-DeepSpeed 启动参数片段，确认三个配置开关如何出现在真实命令行里。
2. 在纸上为教程的主线配置算一遍：\(S = 262144\)（256K 上下文）、\(C = 65536\)、\(P = 4\)（4 卡 SP）。
3. 再算极端案例：\(S = 2097152\)（2M）、\(C = 65536\)、\(P = 4\)。
4. 对照 `FPDT_Attention.forward` 里 `num_chunks_attn` 的公式验证你的全局 chunk 数。

**需要观察的现象**：每 rank 本地序列长 \(S/P\)、每 rank chunk 数、本地 chunk 大小 \(C/P\) 三者之间的关系。

**预期结果**（可手算验证）：

- 256K 案例：全局 chunk 数 \(= 262144 / 65536 = 4\)；本地序列长 \(65536\)；本地 chunk 大小 \(65536 / 4 = 16384\)。
- 2M 案例：全局 chunk 数 \(32\)；每 rank 分 8 个 chunk，本地 chunk 仍是 \(16384\) token。`num_chunks_attn` 的公式「本地长 × P // C」给出 \(524288 × 4 / 65536 = 32\)，一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么全局 chunk 大小 \(C\) 必须能被 SP 并行度 \(P\) 整除（代码里显式校验，见 4.2.3）？

**答案**：每个全局 chunk 在 chunk 内还要做一次 Ulysses all-to-all，把「本 rank 的 chunk 切片、头完整」换成「完整 chunk 序列、头切分」；序列在 SP 组内必须均分，因此本地 chunk 大小 \(C/P\) 必须是整数。`C % P != 0` 时后续的 reshape/all-to-all 会得到错误布局，不如在入口就拒绝。

**练习 2**：`ds_sequence_parallel_fpdt_offloading=false` 时 FPDT 还省显存吗？

**答案**：仍然省。`_FPDTGPUAttentionImpl_` 逐 chunk 计算、流式合并 softmax、backward 手工逐对重算，注意力统计的峰值依旧是「常数个 chunk」而非整段序列的分数矩阵；只是 q/k/v/o/lse 的所有 chunk 会一直驻留 GPU（为 backward 保存）。卸载路径进一步把不用的 chunk 搬去主机。

**练习 3**：教程说 FPDT 保留 vanilla transformer 的全精度，结合 4.1.3 与 4.3.3 的代码说说「全精度」体现在哪。

**答案**：分块注意力在 fp32 中做流式 lse 合并（`update_out_and_lse` 里 `block_out.to(torch.float32)`，见 4.3.3），数值上等价于整段 softmax；不引入低比特近似或稀疏化——分块只是计算顺序的重排。

### 4.2 FPDT_InputConstruct：几何校验、负载均衡分块与因果尾部 padding

#### 4.2.1 概念说明

`FPDT_InputConstruct` 是 FPDT 的「入口适配器」：模型其余部分期望每个 SP rank 拿到自己那份输入，而 FPDT 要求这份输入是**按 chunk 负载均衡交错分布**的（而非简单的前半/后半切分）。它解决三件事：

1. **几何校验**（#9059 新增）：\(S\)、\(C\)、\(P\)、rank 的合法性在做任何除法/切片之前检查，错误信息带上完整几何上下文；
2. **因果尾部 padding**（#9059 新增）：\(S\) 不是 \(C\) 的整数倍时，在序列尾部补零凑整，并保证**真实 token 的行为与对齐输入完全一致**；
3. **负载均衡分块**：把全局 chunk 轮流分给各 rank（round-robin），使每个 rank 的计算量相等。

为什么 round-robin 而不是连续切分？因为分块注意力的计算量沿序列不均匀（第 \(i\) 个 query chunk 要算 \(i+1\) 个块对）。若连续切分，靠后的 rank 计算量是靠前 rank 的数倍。交错分配让每个 rank 都从每个全局 chunk 里拿一段，总量精确相等：每 rank 恰好 \(S'/C\) 个大小 \(C/P\) 的本地 chunk。

padding 的正确性依赖**因果注意力**：补在尾部的零 token 位置在所有真实 token 之后，真实 token 永远看不到它们，因此前向输出与梯度都不受影响；padding 自己产生的损失由零权重屏蔽。这正是契约要求 `attention_mask=None` 的原因——一旦有自定义掩码，「尾部不可见」不再由因果性保证。

#### 4.2.2 核心流程

**几何与推导**（记 padding 后的全局长度为 \(S'\)）：

\[ padding = (-S) \bmod C \]
\[ S' = S + padding \]
\[ num\_chunk\_per\_gpu = S' / C \]
\[ local\_seq\_len = S' / P \]
\[ local\_chunk = C / P \]

**校验四连**（任一失败抛 `ValueError`，信息含 S/C/P/rank 全部数值）：

1. \(S > 0\)、\(C > 0\)、\(P > 0\)；
2. \(0 \le rank < P\)；
3. \(C \bmod P = 0\)；
4. （仅当需要 padding 时）`loss_mask` 非空且 `attention_mask is None`。

**补零规则**：

| 张量 | padding 方式 | 备注 |
| --- | --- | --- |
| `tokens` | 尾部补 0 | 真实 token 看不到补零（因果） |
| `labels` | 尾部补 **0**（不是 -100） | `FPDT_LogitsLoss.backward` 直接用 labels 做索引，不认 `ignore_index`（见 4.5） |
| `loss_mask` | 尾部补 0（零权重） | 屏蔽 padding 损失的唯一手段 |
| `position_ids` | 从最后位置续接 +1, +2, ... | 保持位置连续，RoPE 语义不变 |
| `attention_mask` | 必须为 None | 因果性即掩码 |

**generate() 的索引数学**：设本地 chunk 大小 \(c = C/P\)，全局共 \(S'/c\) 个本地 chunk。第 \(j\) 个全局 chunk（覆盖位置 \([jC, (j+1)C)\)）的第 \(r\) 段属于 rank \(r\)。等价地，rank \(r\) 拿到的位置集合是每个全局 chunk 内的第 \(r\) 个子段（\(j\) 从 0 取到 \(S'/C - 1\)）：

\[ \bigcup_j [jC + rc, jC + (r+1)c) \]

`generate()` 同时返回一份**全局 gather 顺序**的 `loss_mask`（覆盖整个 \(S'\)，按 SP rank 顺序拼接），与 `FPDT_LogitsLoss` 返回的全局损失布局对齐——这是 4.5 归一化契约的另一半。

#### 4.2.3 源码精读

几何校验与 padding 前置条件：

[deepspeed/sequence/fpdt_layer.py:92-109](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L92-L109)

```python
global_seq_len = tokens.shape[1]
global_chunk_size = args.ds_sequence_parallel_fpdt_chunk_size
geometry = f"(S=..., C=..., P=..., rank=...)"        # 四个数值拼进错误信息（节选）
if global_seq_len <= 0 or global_chunk_size <= 0 or sp_size <= 0:
    raise ValueError(...)
if sp_rank < 0 or sp_rank >= sp_size:
    raise ValueError(...)
if global_chunk_size % sp_size != 0:
    raise ValueError(...)

padding = (-global_seq_len) % global_chunk_size
if padding:
    if loss_mask is None:
        raise ValueError(f"FPDT padding requires a loss_mask to exclude padded tokens ...")
    if attention_mask is not None:
        raise ValueError(f"FPDT padding requires attention_mask=None for causal attention ...")
```

这段代码是 #9059 的第一道防线：所有除法（`//`）发生之前，几何合法性已检查完毕；`geometry` 变量把 S/C/P/rank 四个数值拼成上下文附在每条错误信息里（此处节选）；`(-S) % C` 是「凑整到下一个 C 的倍数」的惯用写法；padding 生效的两个前置条件（有 loss_mask、无 attention_mask）也在改动张量之前拒绝。

补零与位置续接：

[deepspeed/sequence/fpdt_layer.py:118-127](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L118-L127)

```python
# FPDT_LogitsLoss.backward directly indexes labels rather than honoring ignore_index=-100.
# Negative labels can select the wrong class or be out of range; label 0 with zero loss weight
# safely excludes padding.
labels = append_zeros(labels)
loss_mask = append_zeros(loss_mask)
if position_ids is not None:
    offsets = torch.arange(1, padding + 1, dtype=position_ids.dtype, device=position_ids.device)
    offsets = offsets.reshape(1, padding, *([1] * (position_ids.dim() - 2)))
    position_ids = torch.cat((position_ids, position_ids[:, -1:] + offsets), dim=1)
global_seq_len += padding
```

这段代码完成三件事：labels/loss_mask 尾部补零（注释解释了为何用 0 而非 -100——backward 的负索引会回绕选错类别或越界）；position_ids 从最后一个位置续接（`position_ids[:, -1:] + [1..padding]`，支持任意尾部广播维度）；更新全局长度。注意所有拼接都产生**新张量**，不修改调用方输入（测试专门断言了这一点）。

`generate()` 的负载均衡索引：

[deepspeed/sequence/fpdt_layer.py:148-163](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L148-L163)

```python
totalChunks = self.global_seq_len // self.chunk_size            # chunk_size 此处是本地 chunk = C // P
token_chunk_idx = torch.arange(self.global_seq_len, ...) // self.chunk_size
chunk_to_gpu = torch.arange(totalChunks, ...).reshape(self.num_chunk_per_gpu, -1).t().contiguous()

gather_chunk = chunk_to_gpu.flatten().unsqueeze(1).contiguous()
mask = gather_chunk == token_chunk_idx
indices = mask.nonzero(as_tuple=False)
gather_indices = indices[:, 0]
token_chunk_indices = indices[:, 1]
indices = torch.cat([token_chunk_indices[gather_indices == i] for i in range(gather_chunk.shape[0])])
...
indices = indices.reshape(-1, self.chunk_size)[self.num_chunk_per_gpu * self.sp_rank:
                                               self.num_chunk_per_gpu * (self.sp_rank + 1)].flatten()
```

逐行解读：`token_chunk_idx` 给每个全局位置标上它属于哪个本地 chunk；`chunk_to_gpu` 先 reshape 成「每 rank chunk 数 × P」再转置，得到「P × 每 rank chunk 数」——第 \(r\) 行是 rank \(r\) 拥有的本地 chunk 编号 \(\{r, r+P, r+2P, \ldots\}\)，这就是 round-robin；`mask/nonzero` 的重排把索引整理成「按目标 rank 分组、组内保持源顺序」的全序 `indices`；最后一行按 rank 切出本 rank 的行块。`loss_mask` 在 rank 切片**之前**就用完整 `indices` 重排（[deepspeed/sequence/fpdt_layer.py:160](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L160)），所以它是全局长度、gather 顺序——与 4.5 的损失布局严格对齐。

测试里有一个精炼的「预言机」（oracle）复现这套切分：

[tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py:19-22](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py#L19-L22)

```python
def rank_indices(length, chunk, size):
    # Oracle: split ordinary global chunks evenly among ranks, in source order.
    chunks = torch.arange(length).split(chunk)
    return [torch.cat([part.tensor_split(size)[rank] for part in chunks]) for rank in range(size)]
```

这段代码用「全局 chunk 逐个 `split`、每块内 `tensor_split` 给各 rank、再拼接」的朴素写法给出期望结果，与 `generate()` 的向量化实现互为对照——两套实现一致即证明索引数学正确（断言的是契约而非实现细节）。

#### 4.2.4 代码实践

**实践目标**：在纯 CPU 环境（无需 GPU、无需进程组）验证 `FPDT_InputConstruct` 的几何校验与长度 19 的 padding 行为。

**操作步骤**：

1. 跑通构造器全套单测（不依赖分布式，`FPDT_InputConstruct` 不触碰进程组）：

   ```bash
   pytest tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py -v
   ```

   覆盖：padding 数值与 rank 顺序（`test_padding_values_and_rank_order`，参数化 length 取 8/3/9/19 × size 取 1/2/4）、对齐输入的可选字段透传（`test_aligned_optional_fields_and_mask`）、非连续输入（`test_padded_optional_fields_and_noncontiguous_input`）、九组非法几何（`test_invalid_geometry`）、缺 loss_mask（`test_padding_requires_loss_weights`）、带 attention_mask（`test_padding_rejects_attention_mask`）。

2. 把下面的脚本存为独立文件运行（示例代码，非项目原有）：

   ```python
   # 示例代码：长度 19 的 padding 契约逐张量断言（CPU 即可）
   import torch
   from types import SimpleNamespace
   from deepspeed.sequence.fpdt_layer import FPDT_InputConstruct

   S, C, P, B = 19, 8, 2, 2
   tokens = torch.arange(1, B * S + 1, dtype=torch.float32).reshape(B, S)
   labels = torch.arange(B * S, dtype=torch.int64).reshape(B, S) % 7
   weights = torch.ones(B, S)
   positions = torch.arange(S, dtype=torch.int32).repeat(B, 1)
   args = SimpleNamespace(ds_sequence_parallel_fpdt_chunk_size=C)

   b0 = FPDT_InputConstruct(tokens, labels, weights, None, positions, args, P, rank=0)
   assert (b0.global_seq_len, b0.local_seq_len, b0.num_chunk_per_gpu, b0.chunk_size) == (24, 12, 3, 4)

   t, l, w, m, p = b0.generate()
   assert m is None                                            # attention_mask 原样透传
   assert t.shape == l.shape == (B, 12) and w.shape == (B, 24)  # w 是全局 gather 顺序
   # rank 0 持全局位置 0-3 / 8-11 / 16-19；第 19 位是补零 token，标签为 0
   assert torch.equal(t[0], torch.tensor([1., 2, 3, 4, 9, 10, 11, 12, 17, 18, 19, 0]))
   assert l[0][-1].item() == 0                                 # padded label = 0
   assert torch.equal(p[0], torch.tensor([0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19]))  # 位置 19 续接
   assert w[0].sum() == 19.0 and w[0][-1] == 0.0               # 只有真实 token 有权重

   b1 = FPDT_InputConstruct(tokens, labels, weights, None, positions, args, P, rank=1)
   t1 = b1.generate()[0]
   # rank 1 持 4-7 / 12-15 / 20-23：最后一段全是 padding（4 个零）
   assert torch.equal(t1[0][8:], torch.zeros(4))

   # 对齐输入（S=24）完全不走 padding 分支，对象原样透传
   toks24 = torch.randn(B, 24)
   mask24 = torch.ones(B, 1, 24, 24, dtype=torch.bool)
   b2 = FPDT_InputConstruct(toks24, None, None, mask24, None, args, P, rank=0)
   assert b2.tokens is toks24 and b2.generate()[3] is mask24
   ```

3. 对照第 2 步断言与 [tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py:41-46](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py#L41-L46) 的期望构造（`expected` 列表的写法与你手写的语义相同）。

**需要观察的现象**：padding 后 rank 1 的最后一个本地 chunk 全是零；`w`（全局 mask）的长度是 24 而非 12；对齐输入时构造器连对象身份都不变（`is` 判定）。

**预期结果**：脚本全部断言通过；`pytest` 全绿（本讲义写作环境未执行，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：\(S=3, C=8, P=2\) 时，哪个 rank 一个有效 token 都拿不到？

**答案**：padding \(= (-3) \bmod 8 = 5\)，\(S' = 8\)，本地 chunk \(c = 4\)。rank 0 拿位置 0-3（3 个真实 + 1 个补零），rank 1 拿位置 4-7——全是 padding，**零个有效 token**。这正是损失归一化必须用 SP 全局权重和的动机（4.5），也是回归测试参数化里专门放 `length=3` 的原因。

**练习 2**：为什么 padded labels 用 0 而不用 -100？

**答案**：`FPDT_LogitsLoss.backward` 用 `grad_2d[arange_1d, labels[...]] -= 1` 直接按标签索引（见 4.5.3），-100 作为负索引会回绕到倒数第 100 个类别（词表小时直接越界报错）；前向的 `F.nll_loss` 虽然认 `ignore_index`，但 backward 不认。0 是合法类别索引，配合零 loss 权重即可安全把 padding 从损失与梯度中排除。

**练习 3**：`generate()` 返回的 `loss_mask` 为什么不在 rank 切片之后才重排（像 tokens 那样只给本 rank 一份）？

**答案**：`FPDT_LogitsLoss` 的输出是 allgather 后的**全局**损失 \([S', B]\)（按 SP rank 顺序拼接），使用方要在全局布局上做掩码加权。给一份同顺序的全局 mask，使用方就不需要自己再做一次 all-reduce 拼接（教程明确说「the returned mask needs no further all-reduce」，[docs/_tutorials/ulysses-offload.md:55](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L55)）。

### 4.3 _FPDTGPUAttentionImpl_：分块因果注意力的自定义 autograd

#### 4.3.1 概念说明

这是 FPDT 的数学心脏。它把「整段序列的因果注意力」重写成「chunk 对的 flash-attn + 流式合并」，并用 `torch.autograd.Function` 手写 backward，从而：

- 前向只为 backward 保存 q/k/v/输出/lse（每 chunk 一份），不再保留整段 softmax 概率矩阵；
- 每对一个 chunk 对调用一次 flash-attn，单块内仍是精确注意力；
- 反向逐 chunk 对调用 `_flash_attn_backward`，中间梯度以 fp32 累加。

关键洞察是分块因果结构：对 query chunk \(i\)，只需遍历 key chunk \(k \le i\)。对角块（\(k = i\)）传 `causal=True` 让 flash-attn 施加三角掩码；非对角块（\(k < i\)）是普通全量注意力；\(k > i\) 直接跳过。于是每 rank 的 flash-attn 调用次数为：

\[ N(N+1)/2 \quad (N 为全局 chunk 数) \]

另一个核心是**流式 softmax 合并**（online softmax 思想）：注意力输出 \(out = \sum_j p_j v_j\) 的分母是全局 log-sum-exp。逐块累加时保存已合并部分的 \(out\) 与 \(\mathrm{lse}\)，新块到来时按指数加权修正——不需要任何块同时持有全部分数。

#### 4.3.2 核心流程

**前向**（对每个 query chunk \(i = 0..N-1\)）：

1. 取本 rank 输入的第 \(i\) 个本地 chunk（大小 \(C/P\)），做 QKV 一次投影；
2. Q/K/V 分别 `single_all_to_all`（Ulysses 换布局）：得到完整 chunk 序列（长度 \(C\)）、头已按 SP 切分——chunk 内的 Ulysses；
3. 若有 RoPE，用**全局位置**的对应切片施加（`pos_emb[:, C*i : C*(i+1)]`）；
4. 对每个已就绪的 key chunk \(k \le i\) 调 flash-attn（对角块 causal），用 `update_out_and_lse` 流式合并；
5. 全部 key 块合并完后，输出再 all-to-all 回「序列切分、头完整」布局，拼接成最终输出；
6. `requires_grad` 时把 q/k/v/o/lse 存进 ctx（此路径全部驻留 GPU）。

**流式合并公式**（fp32 中执行）：

\[ \mathrm{lse}_{new} = \mathrm{lse} + \log(1 + e^{\mathrm{lse}_{b} - \mathrm{lse}}) \]
\[ out_{new} = e^{\mathrm{lse} - \mathrm{lse}_{new}} \cdot out + e^{\mathrm{lse}_{b} - \mathrm{lse}_{new}} \cdot out_{b} \]

其中 \(\mathrm{lse}_b\) 是新块的 log-sum-exp，\(out_b\) 是新块的（未归一化）加权和。两式分别对应代码中的 `log1p(exp(...))` 与指数重加权。

**反向**（结构对称）：

1. 输出梯度先按 chunk 切开、all-to-all 到头切分布局；
2. 双层循环：外层遍历 key chunk \(i\)，内层遍历 \(q_i \ge i\)，逐对调 `_flash_attn_backward` 得到 \(dq_{q_i}, dk_i, dv_i\)，fp32 累加；
3. K/V 梯度做 RoPE 反向（`apply_rotary_pos_emb_backward`）后 all-to-all 回，与对应输入 chunk 做矩阵乘得到 QKV 权重/偏置梯度，并把 \(dk, dv\) 的贡献加进输入梯度；
4. Q 梯度同理，另一轮循环处理；输入梯度按 chunk 拼接返回。

#### 4.3.3 源码精读

逐 chunk 的 QKV 投影与 all-to-all：

[deepspeed/sequence/fpdt_layer.py:242-258](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L242-L258)

```python
for i in range(num_chunks):
    st = chunk_size * i
    ed = st + chunk_size
    qkv_chunk = torch.matmul(layernorm_output[st:ed], qkv_linear_weight.t()) + qkv_linear_bias
    q_chunk = qkv_chunk[:, :, :projection_size]...permute(1, 0, 2, 3).contiguous()  # b, l, nh, hd
    q_chunk = single_all_to_all(q_chunk, scatter_idx, gather_idx, 0, spg, num_kv_heads=num_kv_heads)
    global_q_chunk_len = q_chunk.shape[1]
    if rotary_pos_emb is not None:
        q_chunk = apply_rotary_pos_emb(q_chunk,
                                       pos_emb_cos[:, global_q_chunk_len * i:global_q_chunk_len * (i + 1)],
                                       pos_emb_sin[:, global_q_chunk_len * i:global_q_chunk_len * (i + 1)])
    global_q.append(q_chunk)
```

这段代码展示 chunk 内的完整 Ulysses 流程：QKV 用**一次**融合投影（`qkv_linear_weight` 三段拼成）算出，Q 切出来 all-to-all 后序列长变为 `global_q_chunk_len`（本地 chunk × P = \(C\)），RoPE 取全局位置的第 \(i\) 段切片。`num_kv_heads=num_kv_heads` 让 all-to-all 感知 GQA（K/V 头少于 Q 头时走 [deepspeed/sequence/layer.py:276-313](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/layer.py#L276-L313) 中的 uneven 分支）。

分块因果的块对循环：

[deepspeed/sequence/fpdt_layer.py:276-314](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L276-L314)

```python
for k_i in range(len(global_k)):
    causal_chunk = i == k_i
    # flash-attn >= 2.7.0 split window_size into left/right ints and
    # reduced the forward return from 8 values to 4.
    if flash_attn_version >= version.parse("2.7.0"):
        block_out, block_lse, _, _ = _flash_attn_forward(global_q[i], global_k[k_i], global_v[k_i],
                                                         ctx.dropout_p, ctx.softmax_scale,
                                                         causal=causal_chunk, ...)
    ...
    global_o[i], global_lse[i] = update_out_and_lse(global_o[i], global_lse[i], block_out, block_lse)
```

这段代码就是分块因果规则的落地：只有对角块传 `causal=True`，非对角块（\(k_i < i\)，能进入循环的前提）是全量注意力；每次结果经 `update_out_and_lse` 并入累计值。注释记录了 flash-attn 2.7.0 的接口变化，代码按版本三分支。

流式合并的数学实现：

[deepspeed/sequence/fpdt_layer.py:41-56](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L41-L56)

```python
def _update_out_and_lse(out, lse, block_out, block_lse):
    block_out = block_out.to(torch.float32)
    block_lse = block_lse.transpose(-2, -1).unsqueeze(dim=-1)
    new_lse = lse + torch.log1p(torch.exp(block_lse - lse))
    out = torch.exp(lse - new_lse) * out + torch.exp(block_lse - new_lse) * block_out
    lse = new_lse
    return out, lse
```

这段代码与 4.3.2 的两条公式一一对应：`log1p(exp(diff))` 数值上稳定（diff 为大负数时趋于 0），合并全程 fp32——这就是「全精度流水线」的落点。外层包装 [deepspeed/sequence/fpdt_layer.py:59-77](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L59-L77) 处理首块初始化（直接采纳）与切片更新两种情况。

backward 的块对循环与 fp32 累加：

[deepspeed/sequence/fpdt_layer.py:408-426](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L408-L426)

```python
for i in range(num_chunks):          # 外层：key/value chunk
    k_chunk = global_k[i]
    v_chunk = global_v[i]
    for q_i in range(num_chunks):    # 内层：只算 q_i >= i
        no_computation = q_i < i
        if no_computation:
            continue
        causal_chunk = q_i == i
        ...
        dq_this = torch.zeros(global_q[0].shape, dtype=dtype, device=device)
        dk_this = torch.zeros(global_k[0].shape, dtype=dtype, device=device)
        dv_this = torch.zeros(global_v[0].shape, dtype=dtype, device=device)
```

随后（[deepspeed/sequence/fpdt_layer.py:429-486](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L429-L486)）按版本调 `_flash_attn_backward` 填充三个 `*_this`，然后 `dq[q_i].add_(dq_this.to(torch.float))` 等三行把本对贡献累进 fp32 缓冲——与 forward 的块对集合完全对称。循环外，K/V 梯度做 RoPE 反向 + all-to-all 回原布局，与输入 chunk 矩阵乘得到 `grad_qkv_linear_weight/bias` 并叠加输入梯度（[deepspeed/sequence/fpdt_layer.py:488-518](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L488-L518)）；Q 梯度在第二轮循环处理并顺次消费输入张量（[deepspeed/sequence/fpdt_layer.py:523-546](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L523-L546)）；最终返回拼接的输入梯度与权重梯度（[deepspeed/sequence/fpdt_layer.py:550-553](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L550-L553)）。

RoPE 反向 `apply_rotary_pos_emb_backward`（[deepspeed/sequence/fpdt_layer.py:33-38](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L33-L38)）是旋转嵌入的转置（伴随）运算——旋转的逆向仍是带符号翻转的旋转。

#### 4.3.4 代码实践

**实践目标**：用「块对计数 + 测试断言」双重验证分块因果结构与 GQA 反向的正确性认知。

**操作步骤**：

1. 手算下表并填空（\(N\) = 全局 chunk 数）：

   | \(N\) | 前向 flash-attn 调用 / rank | 反向 `_flash_attn_backward` 调用 / rank | 其中 causal 块数 |
   | --- | --- | --- | --- |
   | 1 | 1 | 1 | 1 |
   | 4 | ？ | ？ | 4 |
   | 32（2M 上下文案例） | ？ | ？ | 32 |

2. 阅读 [tests/unit/v1/sequence_parallelism/test_ulysses.py:523-524](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L523-L524) 起的 `TestFPDTAttentionGQABackward`：它对 `num_kv_heads` 取 2/4/8 参数化，断言非卸载路径的输出与 QKV 权重/偏置/输入梯度都和参考一致，且 **K/V 梯度的形状随 K/V 头数走**（而非 Q 头数）——这是 GQA 感知 all-to-all 的回归。
3. （有 GPU 时）运行该测试：`pytest "tests/unit/v1/sequence_parallelism/test_ulysses.py::TestFPDTAttentionGQABackward" -v`。

**需要观察的现象**：计数表中的因果块数恒等于 \(N\)（每行一个对角块）；总块数是三角数。

**预期结果**：填空答案为 \(4 \to 10\)、\(32 \to 528\)（\(4 × 5 / 2 = 10\)，\(32 × 33 / 2 = 528\)）。GPU 测试通过情况**待本地验证**（需要 flash-attn 与多进程环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 (query chunk \(i\), key chunk \(k\)) 只在 \(k \le i\) 时计算，而不是让每对都算再加掩码？

**答案**：因果性使 \(k > i\) 的块对全部被掩码为零，计算是纯浪费；直接跳过把每 rank 的核调用从 \(N^2\) 降到 \(N(N+1)/2\)，对 \(N=32\) 是 1024 降到 528，省近一半。\(k < i\) 的块对没有任何掩码约束（整块全可见），无需传 causal；只有对角块需要三角掩码。

**练习 2**：`update_out_and_lse` 若在 bf16 里做合并会怎样？

**答案**：合并涉及 \(e^{\mathrm{lse}_{b} - \mathrm{lse}}\) 这类数量级悬殊的权重，低精度浮点的指数范围与尾数精度会造成明显舍入误差甚至下溢为 0。代码显式 `block_out.to(torch.float32)` 并让累计量保持 fp32，只在块结果并完后转回原 dtype（[deepspeed/sequence/fpdt_layer.py:316](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L316)），这就是「全精度」的出处。

**练习 3**：前向为什么把 `attention_mask is None` 写成断言（[deepspeed/sequence/fpdt_layer.py:213](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L213)）？

**答案**：分块因果结构只定义了「无显式掩码的因果注意力」——块间跳过 + 对角 causal 就是全部掩码语义。任意自定义掩码无法分解到块对粒度，也与尾部 padding 的正确性论证冲突。这与 `FPDT_InputConstruct` 的 padding 契约（要求 `attention_mask=None`）是同一约束在两端的体现。

### 4.4 SequenceChunk 双缓冲与 _FPDTGPUOffloadingAttentionImpl_

#### 4.4.1 概念说明

`SequenceChunk` 是 FPDT 的显存调度单元：一个 chunk 同时在 **pinned 主机内存**（权威副本）与 **GPU**（计算副本）间持有，任意时刻 GPU 上只驻留「正在计算 + 预取中的下一块」。「双缓冲」指装载下一块与计算当前块并行——经典的生产者-消费者流水线。

`_FPDTGPUOffloadingAttentionImpl_` 与 4.3 的非卸载版本**数学完全相同**（同样的块对集合、同样的流式合并），区别只在数据驻留：q/k/v/o/lse/输入全部包成 `SequenceChunk`，用三条 CUDA stream（计算、卸载专用、通用卸载）交错装载/回写。backward 也同样流水化：梯度输出边消费边 all-to-all，dq 用一组 `SequenceChunk` 轮转。

#### 4.4.2 核心流程

`SequenceChunk` 的状态机：

```text
构造(chunk, is_in_use)          load_to_gpu()              offload()
  ├─ GPU 张量 ──pin+copy──▶ cpu_chunk（权威副本）
  └─ gpu_chunk = chunk（若 is_in_use）      gpu_chunk 分配并从 cpu 拷入     gpu_chunk 置 None
overwrite_to_cpu(): gpu_chunk 拷回 cpu_chunk（梯度累加结果回写主机）
```

卸载前向的流水（对每个 query chunk \(i\)）：

1. 取输入头部 \(C/P\) 行做 QKV 投影；同时把这段输入包成 `SequenceChunk` 存主机（backward 要用）；
2. all-to-all + RoPE 后，把 q/k/v 各包成 `SequenceChunk(is_in_use=True)`（构造即建立 pinned 副本）；
3. 块对循环：用 `get_gpu_chunk()` 喂 flash-attn；**用完的 kv 块立刻 `offload()`，下一块提前 `load_to_gpu()`**（除非下一块就是当前块，如 \(N=1\)）；当前 q 块内层循环结束也卸载；
4. 最后一个块对算完时，**预取第 0 块** q/k/v/o/lse——为 backward 起步做准备；
5. 输出 all-to-all 回原布局拼接；o/lse 也包成 `SequenceChunk` 存主机。

backward 结构相同，只是方向反过来：梯度按块 all-to-all 进来包成 `SequenceChunk`，dq 是一组主机双缓冲的累加器（每算完一个 q 块 `overwrite_to_cpu` 回写），权重梯度在 GPU 上 fp32 累加。

#### 4.4.3 源码精读

`SequenceChunk` 的构造与装载：

[deepspeed/sequence/fpdt_layer.py:558-582](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L558-L582)

```python
def __init__(self, chunk: torch.Tensor, device=None, is_in_use=False):
    self.chunk_shape = chunk.shape
    self.chunk_dtype = chunk.dtype
    self.device = chunk.device if device is None else device
    if get_accelerator().on_accelerator(chunk):
        cpu_chunk = get_accelerator().pin_memory(torch.empty(chunk.shape, dtype=chunk.dtype, device='cpu'),
                                                 make_copy=False)
        cpu_chunk.copy_(chunk, non_blocking=True)
    else:
        cpu_chunk = chunk
    self.cpu_chunk = cpu_chunk
    self.gpu_chunk = chunk if is_in_use else None

def load_to_gpu(self):
    gpu_chunk = torch.empty(self.chunk_shape, device=self.device, dtype=self.chunk_dtype)
    gpu_chunk.copy_(self.cpu_chunk, non_blocking=True)
    self.gpu_chunk = gpu_chunk
```

这段代码是双缓冲的原子操作：构造时若数据在加速器上就立即建 pinned 主机副本（`pin_memory` + 异步拷贝，走的往往是调用方指定的 offload stream）；`load_to_gpu` 反向搬运。`is_in_use=True` 表示「这块马上要算」，GPU 张量保留。卸载与回写见 [deepspeed/sequence/fpdt_layer.py:594-601](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L594-L601)：`offload()` 直接置 None 释放显存，`overwrite_to_cpu()` 把 GPU 上的新值（典型是累加后的梯度）拷回主机权威副本。

三条流的建立：

[deepspeed/sequence/fpdt_layer.py:675-677](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L675-L677)

```python
offload_stream = get_accelerator().Stream()
general_offload_stream = get_accelerator().Stream()
compute_stream = get_accelerator().default_stream()
```

`offload_stream` 负责 q/k/v/o/lse 的装载卸载，`general_offload_stream` 负责输入与梯度这类「一次性」搬运，计算走默认流——三者通过 `wait_stream` / `synchronize` 建立依赖。

预取与卸载决策：

[deepspeed/sequence/fpdt_layer.py:771-800](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L771-L800)

```python
can_offload_kv = True
if k_i != (len(global_k) - 1) or i != (num_chunks - 1):
    if k_i != (len(global_k) - 1):
        next_kv_compute_chunk_idx = k_i + 1
    else:
        next_kv_compute_chunk_idx = 0
    if next_kv_compute_chunk_idx == kv_compute_chunk_idx:
        can_offload_kv = False          # 下一块就是当前块（N=1），不能卸
    else:
        if next_kv_compute_chunk_idx != (len(global_k) - 1):
            with get_accelerator().stream(offload_stream):
                global_k[next_kv_compute_chunk_idx].load_to_gpu()
                global_v[next_kv_compute_chunk_idx].load_to_gpu()
...
if can_offload_kv:
    global_k[kv_compute_chunk_idx].offload()
    global_v[kv_compute_chunk_idx].offload()
```

这段代码是流水线的节拍器：每算完一个块对，先在 offload stream 上装载下一对 kv 块，再把当前 kv 块从显存释放；「下一块 == 当前块」只发生在全局仅一个 chunk 时（此时卸载再装载是纯浪费）。为 backward 预热的特判在 [deepspeed/sequence/fpdt_layer.py:786-792](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L786-L792)：最后一个块对完成时把第 0 块的 q/k/v/o/lse 全部 `load_to_gpu`——backward 从 (0,0) 开始，起手不能等拷贝。

保存给 backward 的形态对比（卸载版）：

[deepspeed/sequence/fpdt_layer.py:823-834](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L823-L834)

```python
if do_save:
    ctx.layernorm_output = layernorm_output_cpu     # SequenceChunk 列表（主机）
    ctx.global_q = global_q                          # SequenceChunk 列表
    ctx.global_k = global_k
    ctx.global_v = global_v
    ctx.attn_output = global_o
    ctx.attn_lse = global_lse
```

对比非卸载版 [deepspeed/sequence/fpdt_layer.py:332-343](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L332-L343)（`ctx.save_for_backward(layernorm_output)` + 裸张量列表），差别一目了然：**同一套数学，两种数据驻留策略**。backward 侧 dq 的主机双缓冲组见 [deepspeed/sequence/fpdt_layer.py:901-909](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L901-L909)——第 0 块在 GPU，其余 \(N-1\) 块是清零的 pinned 主机缓冲，随用随装、算完 `overwrite_to_cpu`（[deepspeed/sequence/fpdt_layer.py:1026-1027](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1026-L1027)）。

#### 4.4.4 代码实践

**实践目标**：通过手工追踪三个 chunk 下标变量，画出卸载前向的装载/计算/卸载时间线，理解「任意时刻 GPU 上最多几个 kv 块」。

**操作步骤**：

1. 取 \(N = 3\)（全局 3 个 chunk），逐块对 (i, k_i) 记录三元组 `(q_compute_chunk_idx, kv_compute_chunk_idx, next_kv_compute_chunk_idx)` 与发生的 `load_to_gpu` / `offload` 调用，按 [deepspeed/sequence/fpdt_layer.py:727-803](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L727-L803) 的代码顺序推进。注意 `len(global_k)` 在循环中随 i 增长（i=0 时只有 1 块，i=1 时 2 块……）。
2. 用三行泳道（compute / offload / general_offload）把事件画成时间线。
3. 回答：任意时刻 GPU 上最多同时存在几个 kv 块？几个 q 块？
4. （有 GPU 时）跑输出一致性回归 `pytest "tests/unit/v1/sequence_parallelism/test_ulysses.py::TestFPDTAttention" -v`，并对照 [tests/unit/v1/sequence_parallelism/test_ulysses.py:499-501](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L499-L501) 的断言——测试检查 `backend.global_q` 里所有 `cpu_chunk` 在 CPU 且**至少一块** `gpu_chunk is None`（即确实发生了卸载）。

**需要观察的现象**：时间线上 `load_to_gpu(next)` 总在 `offload(current)` 之前发起且在不同 stream；末尾出现一次对第 0 块的五连 `load_to_gpu`。

**预期结果**：不变式为「kv 最多 2 块驻留（当前 + 预取）、q 最多 2 块（当前 + 内层循环刚产出的输出）」；显存上界从「\(N\) 个 chunk 的 q/k/v/o/lse」降到「常数个 chunk」。GPU 测试结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cpu_chunk` 必须是 pinned memory，而普通 `torch.empty(..., device='cpu')` 不行？

**答案**：`copy_(non_blocking=True)` 只有在目标/源是页锁定内存时才真正异步（可分页内存需要先 staging）。双缓冲的全部收益来自「拷贝与计算重叠」，pinned 是前提。这与 u5-l4 ZeRO-Offload 的 pinned buffer 是同一原理。

**练习 2**：`overwrite_to_cpu()` 与 `offload()` 为什么是两个独立操作？

**答案**：`offload()` 只释放 GPU 张量（主机副本仍在）；`overwrite_to_cpu()` 把 GPU 上的**新值**回写主机。backward 中 dq 累加器每次 `add_` 之后都要把累计值写回主机（[deepspeed/sequence/fpdt_layer.py:1026-1027](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1026-L1027)），因为这块 GPU 缓冲很快要轮转给别的块用；「回写」与「释放」语义不同，合并成一个会丢失中间累计值。

**练习 3**：前向末尾为什么要专门预取第 0 块（而不是让 backward 第一次 `load_to_gpu` 时再装）？

**答案**：backward 的第一个块对 (0,0) 立刻需要 q/k/v/o/lse 的第 0 块。若不预取，backward 起步就要同步等待 H2D 拷贝，流水线在前后向交界处断流。把装载提前到前向收尾的 offload stream 上，衔接零等待（[deepspeed/sequence/fpdt_layer.py:786-792](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L786-L792)）。

### 4.5 FPDT_LogitsLoss 与 SP 全局损失归一化契约

#### 4.5.1 概念说明

词表投影是长序列训练最后一段显存大户：\(S' \times B\) 个位置各产生 `vocab_size` 维 logits（几万到十几万），整段 softmax 的 fp32 概率张量轻松爆卡。`FPDT_LogitsLoss` 把「投影 + NLL」按 chunk 重算，前向只保存 `lm_output`（转 CPU）与标签，backward 按块重算 softmax——又一次「重算换显存」。

它还承担通信：各 rank 算出本地损失 \([S'/P, B]\) 后 allgather 成**全局损失** \([S', B]\)，按 SP rank 顺序拼接返回。这就是 4.2 里 `generate()` 返回全局 gather 顺序 loss_mask 的对应面。

配套的**SP 全局损失归一化契约**（#9059 随 padding 一起写进教程）有三条：

1. 先在零权重位置把损失**置零**，再乘权重——`inf * 0 = NaN`，某些位置的损失可能是 inf（如数值溢出处），直接乘会把 NaN 播散到整个标量；
2. 用 **SP 全局**有效权重和归一化——`FPDT_LogitsLoss` 返回的是全局损失，且允许某个 rank 一个有效 token 都没有（4.2 练习 1 的 \(S=3\) 案例），本地归一化既错维度又会除零；
3. 权重可以是小数（负载均衡等场景），契约保留任意非零权重。

#### 4.5.2 核心流程

**forward**（输入 `lm_output [S'/P, B, H]`，`labels [B, S'/P]`，`logit_weights [V, H]`）：

1. 标签转置成 \([S'/P, B]\)（与 lm_output 的序列优先布局对齐）；
2. 逐 chunk：`logits = lm_output[st:ed] @ W^T`（升 fp32）→ `softmax().log()` → `nll_loss(reduction='none')` → 写入 `loss [B, S'/P]`；
3. 保存 `lm_output.to('cpu')` 与标签到 ctx（`logit_weights` 以引用保存）；
4. `loss.t()` 后 allgather，返回 `loss_all [S', B]`（SP rank 顺序）。

**backward**：

1. 上游梯度 `grad_output [S', B]` 先切出本 rank 的行块；
2. 逐 chunk：从主机取回 `lm_output` 块 → 重算 logits/softmax → 梯度 = softmax，在标签位置减 1 → 乘上游梯度 → 转回原 dtype；
3. `@ logit_weights` 得 `grad_lm_output` 块；softmax 梯度与 lm_output 块的矩阵乘累加进 `grad_logit_weights`（fp32）。

**使用方契约**（伪代码）：

```text
losses = FPDT_LogitsLoss(...)                    # [S', B]，SP rank 顺序
mask   = FPDT_InputConstruct(...).generate()[2]  # [B, S']，同一顺序
weights = mask.t()                               # [S', B]
losses = losses.masked_fill(weights == 0, 0.0)   # 先置零，防 inf*0=NaN
loss   = (losses * weights).sum() / weights.sum()  # SP 全局归一化
```

#### 4.5.3 源码精读

分块损失与 allgather：

[deepspeed/sequence/fpdt_layer.py:1317-1342](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1317-L1342)

```python
with torch.no_grad():
    for i in range(num_chunk):
        st = i * chunk_size
        ed = st + chunk_size
        logits_chunk = torch.matmul(lm_output[st:ed], logit_weights.t()).float()
        vocab_size = logits_chunk.size(2)
        # nll
        softmax = torch.nn.functional.softmax(logits_chunk, dim=-1)
        loss_chunk = torch.nn.functional.nll_loss(softmax.log().reshape(-1, vocab_size).contiguous(),
                                                  labels[st:ed, :].reshape(-1).contiguous(),
                                                  reduction='none')
        loss[:, st:ed] = loss_chunk.reshape(chunk_size, batch_size).t()
        del logits_chunk
ctx.save_for_backward(lm_output.to('cpu'), labels)
...
seqlen = local_seq_len * spg_size
loss = loss.t().contiguous()
loss_all = torch.empty(seqlen, batch_size, ...).contiguous()
dist.allgather_fn(loss_all, loss, group=spg)
return loss_all
```

这段代码含三个关键点：逐 chunk 投影 + `nll_loss(reduction='none')`（保留逐 token 损失，权重交给使用方）；`save_for_backward(lm_output.to('cpu'), ...)` 把大张量挪到主机；末尾 allgather 拼出 SP rank 顺序的全局损失。注意前向全程 `torch.no_grad()`——梯度全靠 backward 显式构造。

backward 的标签直接索引：

[deepspeed/sequence/fpdt_layer.py:1356-1374](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1356-L1374)

```python
grad_output = grad_output[rank * local_seq_len:(rank + 1) * local_seq_len]   # 只取本 rank 段
...
lm_output_chunk = lm_output[st:ed].to(device)     # 从主机取回一块
logits_chunk = torch.matmul(lm_output_chunk, logit_weights.t()).float()
softmax = torch.nn.functional.softmax(logits_chunk, dim=-1)
grad_input = softmax
grad_2d = grad_input.reshape(-1, vocab_size).contiguous()
arange_1d = torch.arange(start=0, end=grad_2d.size()[0], device=device)
grad_2d[arange_1d, labels[st:ed, :].reshape(-1).contiguous()] -= 1
grad_input.mul_(grad_output[:chunk_size, :].unsqueeze(dim=-1))
```

`grad_2d[arange, labels] -= 1` 就是交叉熵对 logits 梯度的 \((p - y)\)：softmax 减去 one-hot。**labels 在这里是纯索引**——若是 -100，负索引回绕选错类别；若 >= vocab_size 直接越界。这就是 padding 标签必须用 0 的根源（4.2 练习 2）。

教程中的契约示例（可直接运行）：

[docs/_tutorials/ulysses-offload.md:44-53](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L44-L53)

```python
losses = torch.tensor([[1., 2.], [3., 4.], [5., 6.], [float("inf"), float("inf")]])  # [sequence, batch]
loss_mask = torch.tensor([[1., 1., 1., 0.], [1., 1., 1., 0.]])  # [batch, sequence]
weights = loss_mask.t()
losses = losses.masked_fill(weights == 0, 0.0)
mean_loss = (losses * weights).sum() / weights.sum()
print(mean_loss.item())  # 3.5
```

第 4 个位置（sequence 维）损失是 inf 但权重为 0——不先 `masked_fill`，`inf * 0` 会把整个标量变成 NaN。

顺带一提同文件的 `FPDT_FFN`（[deepspeed/sequence/fpdt_layer.py:1218-1296](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1218-L1296)）：MLP + bias-GELU 同样按 chunk 前向（[deepspeed/sequence/fpdt_layer.py:1232-1242](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1232-L1242)），backward 逐块重算激活并把输入梯度**就地写回**保存的 x（[deepspeed/sequence/fpdt_layer.py:1289](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1289) 的 `x[st:ed].copy_(...)`），省掉整段中间量。它的 `bias_gelu` / `bias_gelu_back`（[deepspeed/sequence/fpdt_layer.py:1205-1215](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1205-L1215)）是手写的 tanh 近似 GELU 前向与导数。

#### 4.5.4 代码实践

**实践目标**：在 CPU 上亲手执行 SP 全局损失归一化契约，验证 3.5 的手算结果，并复现 `inf * 0 = NaN` 陷阱。

**操作步骤**：

1. 运行 4.5.3 引用的教程示例（项目原有代码，见 [docs/_tutorials/ulysses-offload.md:44-53](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/docs/_tutorials/ulysses-offload.md#L44-L53)）。
2. 手算验证：有效损失 \(1+2+3+4+5+6=21\)，权重和 \(6\)，\(21/6=3.5\)。
3. 做两个变体实验（示例代码）：

   ```python
   # 变体 1：不先置零，直接乘 —— 复现 NaN
   bad = (losses * weights).sum() / weights.sum()
   print(bad.item())        # 预期 nan

   # 变体 2：模拟「某 rank 无有效 token」—— 全零权重行
   mask2 = torch.tensor([[1., 1., 1., 0.], [0., 0., 0., 0.]])   # rank 1 全无效
   w2 = mask2.t()
   l2 = losses.masked_fill(w2 == 0, 0.0)
   print(((l2 * w2).sum() / w2.sum()).item())                    # 仍得 3.5
   ```

4. 阅读回归测试中的对应断言：[tests/unit/v1/sequence_parallelism/test_ulysses.py:502-505](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L502-L505) 用 `loss = (losses * global_weights.t()).sum() / global_weights.sum()`，注释明言「A rank can own no real tokens. Normalize by SP-global weight, never local weight」。

**需要观察的现象**：变体 1 输出 `nan`；变体 2 仍输出 3.5（说明全局归一化对「空 rank」稳健）。

**预期结果**：教程示例打印 3.5（文档与手算一致）；变体行为如上。本实践纯 CPU 可跑，但本讲义写作环境未执行，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`FPDT_LogitsLoss.forward` 为什么把 `lm_output` 转到 CPU 再保存，而 `logit_weights` 不转？

**答案**：`lm_output` 是 \(S'/P \times B \times H\) 的大激活，正是要省的对象；`logit_weights` 是 \([V, H]\) 的模型参数，本来就在 GPU 上参与后续矩阵乘，转走反而每次都要搬回。autograd 的 `save_for_backward` 存 CPU 张量完全合法，backward 里 `.to(device)` 取回（[deepspeed/sequence/fpdt_layer.py:1362](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1362)）。

**练习 2**：backward 里为什么先切 `grad_output[rank*local:(rank+1)*local]` 再逐块消费？

**答案**：allgather 返回的梯度是全局 \([S', B]\)，但重算 logits 只需要、也只能用**本 rank 的** lm_output（其他 rank 的激活不在本机）；且逐块消费时 `grad_output = grad_output[chunk_size:]` 顺次推进（[deepspeed/sequence/fpdt_layer.py:1356](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1356)、[deepspeed/sequence/fpdt_layer.py:1377](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/deepspeed/sequence/fpdt_layer.py#L1377)），避免为每个 chunk 重新计算切片偏移。

**练习 3**：如果使用方拿 `loss_all` 直接 `.mean()` 会发生什么？

**答案**：会把 padding 位置的损失（标签 0 下的正常 NLL 值，非零）和被掩码位置的 inf 一并平均，既引入偏差又可能得到 NaN/inf。必须走契约：置零、加权、按全局权重和归一——padding 与 inf 都被零权重隔离。

## 5. 综合实践

把本讲五个模块串成一个「从构造到损失」的完整验证。分三段，前两段 CPU 可完成，第三段需要 GPU。

**任务**：为一个 \(S=19, C=8, P=2\) 的迷你配置，端到端验证 FPDT 的输入契约、padding 等价性与损失归一化。

**第一段（CPU）：输入构造与契约**

1. 按 4.2.4 的脚本构造两个 rank 的输入，额外断言：
   - 两个 rank 的 token 拼起来（按 gather 顺序）覆盖 0..23 恰好一次（无丢失无重复）；
   - `w`（全局 mask）在 gather 顺序下与手工重排一致；
   - 构造前后调用方的四个输入张量逐位不变（对照 [tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py:65-68](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py#L65-L68) 的输入不可变性检查，包括「改输出不影响输入」）。
2. 把九组非法几何逐个喂给构造器，确认报错信息包含 S、C、P、rank 四个字段的数值（对照 [tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py:95-104](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py#L95-L104)）。

**第二段（CPU）：损失归一化**

3. 用第一段得到的 `w` 与一组自造的 `[24, B]` 损失（含一个 inf 行）演练 4.5.4 的契约三步，确认结果只由有效 token 决定；再构造「rank 1 全无效」的权重分布验证稳健性。

**第三段（GPU，待本地验证）：分布式回归**

4. 运行 padding 等价性回归（需要 flash-attn、算力 >= 8、双进程）：

   ```bash
   pytest "tests/unit/v1/sequence_parallelism/test_ulysses.py::TestFPDTPadding" -v
   ```

   对照 [tests/unit/v1/sequence_parallelism/test_ulysses.py:390-395](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L390-L395)：参数化 length 取 24/19/3 × offload 取 False/True——长度 19 走多 chunk 卸载路径、长度 3 落回单 chunk 非卸载路径、长度 24 是对齐基线。测试在三种 padding 模式（auto / explicit / perturbed，见 [tests/unit/v1/sequence_parallelism/test_ulysses.py:463-471](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L463-L471)）下断言：输出、输入梯度、QKV 权重/偏置梯度、head 梯度与未 padding 的参考逐项一致（[tests/unit/v1/sequence_parallelism/test_ulysses.py:509-515](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L509-L515)），标量损失对参考误差在 1e-5 量级，padding 位置梯度恒为零，且三种模式的数值两两仅差 fp16 噪声（[tests/unit/v1/sequence_parallelism/test_ulysses.py:516-520](https://github.com/deepspeedai/DeepSpeed/blob/663de9736f53e7f1e4deab75b7aa766694825b2a/tests/unit/v1/sequence_parallelism/test_ulysses.py#L516-L520)）。「perturbed」模式故意把 padding token 填成 0.75、标签填成 vocab-1——仍不影响真实 token 的任何输出，这是「因果性保证 padding 不可见」最硬的证据。

**预期结果**：第一、二段全部断言通过；第三段测试全绿（**待本地验证**）。完成后，你应当能独立回答：为什么长度 19 在 SP=2 下 rank 1 有一段纯 padding、为什么这不会污染损失与梯度、以及使用方代码必须遵守哪三步归一化。

## 6. 本讲小结

- **FPDT = 逐 chunk 的 Ulysses + 跨 chunk 流式 softmax + 主机双缓冲卸载**：全局序列按 \(C\) 切块，块内 all-to-all 换布局复用 u6-l2 的机制，块间用 fp32 的 lse 流式合并保证与整段注意力数值等价，激活峰值从整段序列降到常数个 chunk。
- **`FPDT_InputConstruct` 的几何与契约（#9059）**：入口先校验 \(S, C, P, rank\) 合法性与 \(C \bmod P = 0\)；\(S\) 不整除 \(C\) 时尾部补零（tokens/labels/loss_mask 补 0、position_ids 续接），要求提供 loss_mask 且 attention_mask=None——因果性保证真实 token 看不到 padding，对齐输入行为完全不变；分块采用 round-robin 使各 rank 计算量相等。
- **两条注意力路径**：`cpu_offloading=False` 或全局单 chunk 走 `_FPDTGPUAttentionImpl_`（全驻 GPU），否则走 `_FPDTGPUOffloadingAttentionImpl_`（`SequenceChunk` 双缓冲 + 三条 CUDA stream 流水装载/卸载）；两者共享同一套分块因果数学，flash-attn 调用数为每 rank \(N(N+1)/2\)。
- **`SequenceChunk`** 是显存调度原子：pinned 主机权威副本 + 按需装载的 GPU 副本，`offload` 释放、`overwrite_to_cpu` 回写，配合「预取下一块、用完即卸、末尾为反向预热第 0 块」的节拍，GPU 上最多驻留常数个 chunk。
- **`FPDT_LogitsLoss` 与归一化契约**：分块投影 + NLL、allgather 出 SP rank 顺序的全局损失；padding 标签用 0（backward 直接按标签索引，不认 ignore_index）；使用方必须「零权重先置零（防 `inf*0=NaN`）→ 加权求和 → 除以 SP 全局权重和（容忍空 rank）」。
- **方法论**：FPDT 通篇是「自定义 autograd + 重算换显存 + 流水线重叠」三种手段的组合应用，这与 ZeRO 的「切分」、激活检查点的「重算」一脉相承——理解本讲后，你可以把同样的三板斧用到自己的长序列/大激活算子上。

## 7. 下一步学习建议

- **补齐 Ulysses 单元的最后一块拼图**：回到 u6-l7 的 `deepspeed/sequence/vocab_parallel_ops.py`，对比「分块重算词表损失（FPDT_LogitsLoss）」与「词表分片 + 全局归一化统计（VocabTP）」两条正交路线——一个按序列切、一个按词表切，长序列大词表场景两者可以叠加。
- **延伸到混合引擎单元**：u7-l4 的静态图 decode 用 4-D 加性掩码规避 host 端检查，本讲的 `attention_mask is None` 断言是同一类「掩码语义约束」在训练侧的镜像；读 `hybrid_engine_rollout.py` 时可以带着「FPDT 为什么不能进 CUDA graph」的问题思考（分块 + 卸载 + 双向流水的控制流太动态）。
- **横向对比重算策略**：u8-l1 的激活检查点（按层重算）与本讲的按 chunk 重算、u5-l7 的 NVMe 交换（参数/状态三级存储）与本讲的主机双缓冲，构成 DeepSpeed 显存优化的完整光谱；建议写一份对比笔记：各自重算/搬运什么、依赖什么硬件特性、适用于什么瓶颈。
- **动手方向**：仿照 `tests/unit/v1/sequence_parallelism/test_fpdt_inputs.py` 的「oracle 对照」写法，为 `generate()` 的 gather 顺序写一个独立实现并交叉验证；或在小模型上把 `FPDT_Attention` 的 `chunk_size` 从大到小扫描，观察显存峰值与 step 时间的变化曲线（需 GPU，待本地验证）。
