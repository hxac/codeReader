# 并行注意力：Ring 与 Ulysses 序列并行

## 1. 本讲目标

本讲承接 [u7-l1 注意力后端：role 感知选择](u7-l1-attention-backends.md)。在上一讲里，我们知道了扩散 Transformer 的每一处 `Attention` 站点都会在构造期选定一个**注意力后端（AttentionBackend）**——也就是一个具体的 kernel 实现（FlashAttention、SDPA、TRTLLM…）。后端回答的是「**怎么算**」。

本讲要回答另一个正交的问题：「**单张序列太长，一张卡装不下 / 算太慢，怎么把序列拆到多张卡上算**」。这就是**序列并行（Sequence Parallelism，SP）**。vLLM-Omni 在 attention 层内提供了两种 SP 策略：**Ulysses**（all-to-all 重分布）与 **Ring**（环形 P2P KV 通信），以及第三种 `AllGather-KV`（本讲附带说明）。

学完本讲，你应该能够：

1. 说清「注意力后端」与「并行注意力策略」为什么是**正交两件事**，以及二者如何在一次前向中组合。
2. 画出 Ulysses 的「序列维 ↔ 头维」all-to-all 重分布，并解释 strict 模式与 UAA（Ulysses Anything Attention）模式的区别。
3. 画出 Ring Attention 的环形 P2P KV 循环与在线 LSE（log-sum-exp）合并，并理解它为什么能把任意底层 kernel 复用成 ring 形态。
4. 掌握工程铁律 `ring_degree × ulysses_degree = sequence_parallel_size`，以及二者为何可以**混合（Hybrid Ulysses+Ring）**。

## 2. 前置知识

- **序列维度与头维度**。扩散 Transformer 的注意力张量通常形如 `(B, S, H, D)`：批次 `B`、序列长度 `S`（图像 patch 数 / 视频帧数 × patch 数）、注意力头数 `H`、每个头的隐维 `D`。高分辨率、长视频会让 `S` 变得极大，这正是 SP 要切的目标维度。
- **集合通信原语**。
  - **all-to-all**：每个 rank 各发一块、各收一块，是「数据转置」型通信。Ulysses 的灵魂。
  - **P2P send/recv**：两个指定 rank 之间点对点收发。Ring 的灵魂。
  - **all-gather**：所有 rank 都拿到全量数据。`AllGather-KV` 与 Ulysses 的 joint 输出回拼都用到它。
- **FlashAttention 与在线 LSE 合并**。FlashAttention 用分块（tiling）+ 数值稳定公式把分块的 `out` 与 `lse`（logsumexp）合并成全局结果。Ring 正是把这个「分块合并」从「同一张卡的循环」推广到「跨卡的环形通信」。
- **本系列已建立的认知**：
  - [u7-l1](u7-l1-attention-backends.md) 讲过 `Attention.__init__` 用 `role` 给注意力站点命名、按四级优先级选后端，**换后端即重建模型**。
  - [u5-l3](u5-l3-diffusion-worker-loader.md) 讲过 worker 进程会在 `init_device` 阶段调用 `initialize_model_parallel` 建好各并行组（本讲会用到其中的 SP 组）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vllm_omni/diffusion/attention/parallel/base.py` | 定义策略协议 `ParallelAttentionStrategy` 与默认实现 `NoParallelAttention` |
| `vllm_omni/diffusion/attention/parallel/factory.py` | 工厂 `build_parallel_attention_strategy`：按配置在 Ulysses / Ring / AllGather-KV / No-op 之间四选一 |
| `vllm_omni/diffusion/attention/parallel/ulysses.py` | Ulysses 策略：pre/post 的 all-to-all 重分布，含 strict 与 UAA 两条路径 |
| `vllm_omni/diffusion/attention/parallel/ring.py` | Ring 策略：`run_attention` 把底层 flash/sdpa kernel 包成 ring 形态 |
| `vllm_omni/diffusion/attention/parallel/allgather_kv.py` | AllGather-KV 策略：本地 Q 对全量 K/V（与 Ulysses/Ring 互斥） |
| `vllm_omni/diffusion/distributed/comm.py` | 通信原语 `SeqAllToAll4D`（all-to-all）与 `RingComm`（P2P send/recv） |
| `vllm_omni/diffusion/attention/backends/ring_flash_attn.py` | Ring 的核心循环：KV 块沿环走，逐步合并 LSE |
| `vllm_omni/diffusion/attention/layer.py` | `Attention` 模块：三段式 `pre_attention → kernel → post_attention` 的装配现场 |
| `vllm_omni/diffusion/distributed/parallel_state.py` | 建组：`set_seq_parallel_pg` 把 SP 组再切成 ulysses/ring 子组，并固化 `sp_size = ring × ulysses` |
| `docs/design/feature/sequence_parallel.md` | 用户向指南：如何为新模型用 `_sp_plan` 接入 SP |
| `docs/design/module/dit_module.md`（§5.2） | 设计总览：后端 vs 策略的正交关系 |

## 4. 核心概念与源码讲解

### 4.1 策略抽象：ParallelAttentionStrategy 与 build_parallel_attention_strategy

#### 4.1.1 概念说明

在 vLLM-Omni 里，「**算 attention 用哪个 kernel**」和「**长序列怎么拆到多卡**」被刻意拆成两件互不干涉的事：

- **AttentionBackend**（上一讲）：决定 kernel 实现。它只懂「给我 `(B, S, H, D)` 的 Q/K/V，我返回 attention 输出」。
- **ParallelAttentionStrategy**（本讲）：决定 Q/K/V 在多卡之间怎么切、怎么通、算完怎么收回去。它把切/通的工作做完后，**把规整好的局部 Q/K/V 喂给后端 kernel**。

设计文档把这一关系说得非常清楚：策略「works **on top of** AttentionBackend implementations … handling the parallelization/communication while the backends handle the actual attention computation」（见 [dit_module.md:677-685](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L677-L685)）。

正因为正交，所以同一个后端（比如 FlashAttention）既能跑在单卡（No-op 策略），也能被 Ulysses 包成「每卡算部分头」，还能被 Ring 包成「每卡算部分序列」。**换策略不换 kernel**——这是本讲最重要的一句话。

#### 4.1.2 核心流程

策略的生命周期被 `Attention` 模块固定成**三段式**（详见 4.1.3 的源码）：

```
pre_attention(Q,K,V)   →  把「按序列切」的输入重排成「后端能直接算」的形态
        ↓
   后端 kernel          →  Ulysses: 算"全序列 × 部分头"；Ring: 在 ring 内逐步合并
        ↓
post_attention(out)     →  把输出重排回「按序列切」的形态，还给上层
```

四种策略、以及它们的「pre/post 做不做通信」差异：

| 策略 | pre_attention | post_attention | 触发条件 |
|------|---------------|----------------|----------|
| `NoParallelAttention` | 什么都不做 | 什么都不做 | SP 未启用（`sp_size<=1`）或不在 SP 分片区 |
| `UlyssesParallelAttention` | all-to-all：散序列、聚头 | all-to-all：散头、聚序列 | `ulysses_degree > 1` |
| `RingParallelAttention` | 仅拼接 joint_query | 什么都不做（输出本就按序列切好） | `ring_degree > 1` |
| `AllGatherKVParallelAttention` | all-gather 全量 K/V | 什么都不做 | `allgather_degree > 1`（与 Ulysses/Ring 互斥） |

工厂 `build_parallel_attention_strategy` 就是这张表的代码化。

#### 4.1.3 源码精读

**(1) 策略协议** —— 先看接口有多薄。`ParallelAttentionStrategy` 是一个 `typing.Protocol`，只规定两个属性 + 两个方法：

[base.py:25-58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/base.py#L25-L58) 定义了这个协议，注释里直接点明「intentionally orthogonal to the attention *kernel* backend」。`pre_attention` 返回一个 `ParallelAttentionContext`（[base.py:14-23](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/base.py#L14-L23)），这是策略在 pre 阶段「寄存」的随身行李，供 post 阶段取回——比如 Ulysses 要记住 `joint_len`、`orig_head_cnt` 才能在 post 时把拼接好的输出再拆回去。

默认实现 `NoParallelAttention`（[base.py:61-82](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/base.py#L61-L82)）的 pre/post 都是透传，`enabled=False`，是「没有 SP」时的占位符。

**(2) 工厂的判定逻辑** —— `build_parallel_attention_strategy` 用一连串 `getattr` + 优先级 `if` 实现四选一：

[factory.py:26-95](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L26-L95) 是整段工厂。注意几个细节：

- 它**不**自己读命令行，而是从 `get_forward_context().omni_diffusion_config.parallel_config` 取 `ulysses_degree / ring_degree / allgather_degree`（[factory.py:40-49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L40-L49)）。这就是为什么 worker 的 `set_forward_context` 必须在前向之前把配置注入进程全局（见 u5-l3）。
- 它读不到 forward context、或 SP 组没初始化、或 `sp_world_size <= 1`，一律降级为 `NoParallelAttention`（[factory.py:51-64](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L51-L64)），并发出明确告警——避免「配了 SP 却静默走单卡」导致结果错乱。
- 判定优先级是 `allgather → ulysses → ring → no-op`。注意 **Ulysses 分支并不排斥 Ring**：当 `ulysses_degree > 1` 时返回 `UlyssesParallelAttention`，而 Ring 的「同时启用」是在 `Attention.__init__` 里**另开一条** `self.ring_runner`（见 4.3）。AllGather-KV 才是严格互斥的（[factory.py:66-76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L66-L76)）。

**(3) 三段式装配现场** —— 看看 `Attention` 模块怎么把策略和后端缝到一起。构造期建好策略（[layer.py:146-153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L146-L153)）：

```python
self.parallel_strategy = build_parallel_attention_strategy(
    scatter_idx=scatter_idx, gather_idx=gather_idx, use_sync=use_sync, causal=causal,
)
self._no_parallel_strategy = NoParallelAttention()   # 分片区外的兜底
```

而前向 `_forward_impl`（[layer.py:263-290](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L263-L290)）就是「三段式」的字面实现：

```python
strategy = self._get_active_parallel_strategy()   # sp_active=False 时退回 No-op
query, key, value, attn_metadata, ctx = strategy.pre_attention(query, key, value, attn_metadata)  # 1. 重排/通信
...
if self.use_ring and strategy is not self._no_parallel_strategy:
    out = self._run_ring_attention(query, key, value, attn_metadata)   # 2a. Ring 自带 kernel
else:
    out = self._run_local_attention(query, key, value, attn_metadata)  # 2b. Ulysses/No-op 走后端
out = strategy.post_attention(out, ctx)                                # 3. 反向重排/通信
```

这里有个微妙之处：`_get_active_parallel_strategy`（[layer.py:164-177](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L164-L177)）会查 `forward_context.sp_active`。因为模型的 `_sp_plan` 只在「transformer block 之间」才真正分片，block 外（比如 patch embed 前）的张量仍是全量，此时若强行做 all-to-all 就错了——所以用 `sp_active` 把 SP 通信严格限制在分片区内。

**(4) `ring_degree × ulysses_degree = sequence_parallel_size` 是怎么固化下来的** —— 这条铁律在 `set_seq_parallel_pg` 与 `initialize_model_parallel` 里写成强校验：

[parallel_state.py:800-809](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L800-L809) 计算 `expected_sequence_parallel_size`：

```python
expected_sequence_parallel_size = allgather_degree if allgather_degree > 1 else ring_degree * ulysses_degree
```

若用户传入的 `sequence_parallel_size` 与之不符，直接 `raise ValueError`。而 `set_seq_parallel_pg`（[parallel_state.py:551-620](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L551-L620)）的关键一行在 [parallel_state.py:620](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L620)：`sp_size = sp_ring_degree * sp_ulysses_degree`，随后把每个 SP 组**再切成两个正交子组**——一个连续块状（Ulysses 组）、一个跨组 stride 状（Ring 组），二者覆盖同样的 `sp_size` 个 rank，只是切法不同（[parallel_state.py:684-717](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/parallel_state.py#L684-L717)）。这就是「混合 Ulysses+Ring」得以成立的物理基础：同一批 rank 既能按 Ulysses 维做 all-to-all，又能按 Ring 维做 P2P。

#### 4.1.4 代码实践

**目标**：从源码确认「换策略不换 kernel」，并验证工厂的判定优先级。

**操作步骤**：

1. 打开 [layer.py:263-290](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L263-L290)，确认 `_run_local_attention`（[layer.py:292-303](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L292-L303)）里调用的是 `self.attention.forward(...)`，也就是构造期选定的后端 impl——它**完全不知道**外面套的是 Ulysses 还是 No-op。
2. 打开 [factory.py:66-95](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L66-L95)，按代码顺序手算：若 `ulysses_degree=2, ring_degree=2, allgather_degree=1`，会返回哪个策略类？再算 `allgather_degree=2, ulysses_degree=2`，会抛什么错？

**需要观察的现象**：

- 步骤 1：Ulysses 的 pre/post 只在「策略层」搬张量，kernel 调用与单卡完全一致——这就是「正交」的实证。
- 步骤 2：第一种返回 `UlyssesParallelAttention`（Ring 的同时启用靠 `Attention.__init__` 的 `self.ring_runner`，不靠工厂）；第二种在 [factory.py:69-74](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L69-L74) 抛 `ValueError("AllGather-KV SP is mutually exclusive with Ulysses/Ring ...")`。

**预期结果**：你能用一句话复述「后端决定怎么算，策略决定怎么拆，二者通过 `Attention._forward_impl` 的三段式组装」。运行命令的实测耗时与显存待本地验证（需要多卡）。

#### 4.1.5 小练习与答案

**练习 1**：为什么工厂取不到 forward context 时要降级成 `NoParallelAttention` 而不是报错？

> 参考答案：`Attention` 模块在**构造期**就会调用 `build_parallel_attention_strategy`（见 [layer.py:146](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L146)），而 forward context 是**前向期**才注入的；构造期没有 context 是正常的。降级为 No-op 保证构造不崩，真正的前向判定靠 `_get_active_parallel_strategy` 在运行时再查 `sp_active`。

**练习 2**：`AllGather-KV` 为什么要求 `causal=False`（见 [factory.py:67-68](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L67-L68)）？

> 参考答案：AllGather-KV 是「本地 Q 段对全量 K/V」做完整双向注意力，无法表达「只看过去」的因果掩码（它的查询范围切片 [allgather_kv.py:111-175](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/allgather_kv.py#L111-L175) 假定的是 full attention 语义）。扩散模型的自注意力通常是非因果的，所以它适用；自回归模型不行。

---

### 4.2 Ulysses 序列并行：all-to-all 重分布

#### 4.2.1 概念说明

Ulysses 的核心直觉是一句口诀：**「进来按序列切，算的时候按头切」**。

设总序列长 `S`、头数 `H`、并行度 `P`。Ulysses 在 `P` 张卡上：

1. **入口**：每张卡拿到**全头**的 `(B, S/P, H, D)`——序列已被上层 `_sp_plan` 切好。
2. **pre（all-to-all）**：把「每卡的部分序列 × 全头」重排成「每卡的**全序列** × **部分头**」`(B, S, H/P, D)`。
3. **算**：每张卡用后端 kernel 独立算 attention，因为持有全部序列，结果在数值上**等价于**单卡全量 attention——只是每个 rank 只负责 `H/P` 个头。
4. **post（反向 all-to-all）**：把输出 `(B, S, H/P, D)` 重排回 `(B, S/P, H, D)`，回到「按序列切」的形态还给上层。

为什么这样做能省显存、提速度？因为 attention 的中间矩阵（注意力分数）规模是 \(O(S^2 \cdot H)\)，Ulysses 把它降到 \(O(S^2 \cdot H/P)\)，且每卡只算自己那 `H/P` 个头，天然并行。

数学上，pre 的 all-to-all 等价于在「序列维」与「头维」之间做了一次张量转置后跨卡交换：

\[
\text{all\_to\_all}:\quad (B,\, S/P,\, H,\, D) \;\longleftrightarrow\; (B,\, S,\, H/P,\, D)
\]

注意它**不是**简单 reshape，而是真正跨卡的 `all_to_all_single`（见 4.2.3）。

#### 4.2.2 核心流程

```
输入 (B, S/P, H, D)        # 序列已切，全头
   │  pre_attention:
   │   strict 模式 ── SeqAllToAll4D(scatter_idx=2 头, gather_idx=1 序列)
   │   UAA 模式   ── _ulysses_all_to_all_any_qkv（可变长 + 头数 pad）
   ▼
(B, S, H/P, D)             # 全序列，部分头 ← 后端 kernel 在此算
   │  AttentionBackend.forward (FlashAttn / SDPA / ...)
   ▼
(B, S, H/P, D)             # 输出，全序列，部分头
   │  post_attention:
   │   strict 模式 ── SeqAllToAll4D(gather_idx=2, scatter_idx=1) 反向
   │   UAA 模式   ── _ulysses_all_to_all_any_o
   ▼
(B, S/P, H, D)             # 回到「序列切、全头」
```

Ulysses 有**两条路径**，由 `ulysses_mode` 决定：

- **strict 模式**（默认、快路径）：要求 `head_cnt % ulysses_degree == 0` 且序列可均匀拆分。形状规则时走最直接的 `SeqAllToAll4D`。
- **advanced_uaa 模式**（实验性，Ulysses Anything Attention）：当头数不能被整除、或各卡序列分片不等长时，用**变长 all-to-all**（`output_split_sizes=seq_lens`）并对头维做临时 pad，算完再裁掉。文档见 [sequence_parallel.md:50-82](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/sequence_parallel.md#L50-L82)。

#### 4.2.3 源码精读

**(1) 通信原语 all-to-all 是怎么转置的** —— 先看最朴素的 strict 路径用的 `all_to_all_4D`。`scatter_idx=2, gather_idx=1`（散头、聚序列）的分支在 [comm.py:36-62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L36-L62)：

```python
bs, shard_seqlen, hc, hs = input.shape          # (B, S/P, H, D)
seqlen = shard_seqlen * seq_world_size           # 恢复全序列长
shard_hc = hc // seq_world_size                  # 每卡分到的头
# 把「头维」拆出 world_size 维，与序列维对调，以便 all-to-all 散序列/聚头
input_t = input.reshape(bs, shard_seqlen, seq_world_size, shard_hc, hs).transpose(0,2).contiguous()
output = torch.empty_like(input_t)
dist.all_to_all_single(output, input_t, group=group)   # ← 真正的跨卡交换
output = output.reshape(seqlen, bs, shard_hc, hs).transpose(0,1).contiguous().reshape(bs, seqlen, shard_hc, hs)
```

关键点：`all_to_all_single` 默认按「第 0 维均匀切块、互相交换」工作。所以代码先用 `reshape+transpose` 把「想散的维度（序列）」和「想聚的维度（头）」摆成第 0 维的等分结构，调一次 `all_to_all_single`，再 reshape 回去。`SeqAllToAll4D`（[comm.py:103-117](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L103-L117)）只是把它包成 `torch.autograd.Function`（本框架推理用 forward）。

**(2) pre_attention 的两条路径** —— `UlyssesParallelAttention.pre_attention`（[ulysses.py:199-412](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L199-L412)）。

strict 路径里，先做整除性 fail-fast（[ulysses.py:320-329](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L320-L329)），然后对 Q/K/V 各调一次 all-to-all：

```python
query = SeqAllToAll4D.apply(self._ulysses_pg, query, self._scatter_idx, self._gather_idx, self._use_sync)
key   = SeqAllToAll4D.apply(self._ulysses_pg, key,   self._scatter_idx, self._gather_idx, self._use_sync)
value = SeqAllToAll4D.apply(self._ulysses_pg, value, self._scatter_idx, self._gather_idx, self._use_sync)
```
（[ulysses.py:331-334](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L331-L334)）

UAA 路径则改用 `_ulysses_all_to_all_any_qkv`（[ulysses.py:52-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L52-L101)），它先 `all_gather` 各卡的本地序列长（[ulysses.py:297-299](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L297-L299)），把它们当作 `all_to_all_single` 的 `output_split_sizes`，从而支持不均匀分片；同时对头维做 pad 到 `world_size` 的倍数（[ulysses.py:70-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L70-L75)），反向交换后再裁剪。

**(3) post_attention 的反向** —— 算完 kernel，要把 `(B, S, H/P, D)` 还原。最末尾的标准反向就在 [ulysses.py:472-473](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L472-L473)：

```python
return SeqAllToAll4D.apply(ctx.ulysses_pg, attn_output, ctx.gather_idx, ctx.scatter_idx, ctx.use_sync)
```

注意 `scatter_idx` 与 `gather_idx` 对调了——pre 是「散头聚序列」，post 就是「散序列聚头」，互为逆操作。

**(4) joint 注意力的拼接时机** —— 这是最容易踩坑的细节。当模型有「图像序列 + 文本前缀」（joint attention，如 Qwen-Image/Z-Image）时，文本部分是**各卡复制**的。Ulysses 的处理顺序是：先对图像 Q/K/V 做 all-to-all，**再**把文本 joint 张量按头切好后 `torch.cat` 到序列维（[ulysses.py:339-360](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L339-L360)）。post 时则先把输出按 `joint_len` 切成 joint/img 两段，joint 段做 `all_gather`（聚头），img 段做反向 all-to-all（聚序列），再拼回去（[ulysses.py:417-461](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L417-L461)）。这就是 `_UlyssesCtx` 要寄存 `joint_len`/`joint_strategy`/`orig_head_cnt` 的原因。

#### 4.2.4 代码实践

**目标**：用现成离线脚本亲自跑一次 Ulysses SP，对比单卡结果。

**操作步骤**（需 ≥2 卡，参考 [sequence_parallel.md:344-353](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/sequence_parallel.md#L344-L353)）：

```bash
# 单卡基线
cd examples/offline_inference/text_to_image
python text_to_image.py \
  --model Tongyi-MAI/Z-Image-Turbo \
  --prompt "a cup of coffee on the table" \
  --ulysses-degree 1 --output /tmp/sp_deg1.png

# Ulysses SP（2 卡）
python text_to_image.py \
  --model Tongyi-MAI/Z-Image-Turbo \
  --prompt "a cup of coffee on the table" \
  --ulysses-degree 2 --output /tmp/sp_deg2.png
```

`text_to_image.py` 把 `--ulysses-degree` 直接透传进 `Omni(...)` 的 kwargs（[text_to_image.py:417-419](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L417-L419)），最终落到 `parallel_config.ulysses_degree`，被本讲的工厂读到。

**需要观察的现象**：两张图在数值上应当一致（或差异在浮点噪声量级）；启动日志会打印 `Parallel configuration: ... ulysses_degree=2 ...`（[text_to_image.py:467-473](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/text_to_image/text_to_image.py#L467-L473)）。

**预期结果**：输出图像一致、单卡显存占用约为单卡基线的一半左右（头被拆到两卡）。吞吐/显存的具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：strict 模式下，一个模型有 40 个注意力头、`ulysses_degree=4`，会发生什么？`ulysses_degree=3` 呢？

> 参考答案：`40 % 4 == 0`，正常工作；`40 % 3 != 0`，会在 [ulysses.py:321-329](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L321-L329) 抛 `ValueError`，提示可行的 `ulysses_degree` 取自 `_positive_divisors(40)`（即 1/2/4/5/8/10/20/40），或建议改用 `ulysses_mode='advanced_uaa'`。

**练习 2**：Ulysses 的 post 阶段为什么能把 joint 输出用 `all_gather` 而不是 all-to-all？

> 参考答案：joint（文本）张量在每卡上是**相同**的（复制语义），all-to-all 后每卡只持有它的 `H/P` 个头；要恢复全头只需把各卡的 `H/P` 沿头维拼起来，这正好是 `all_gather`（[ulysses.py:450-453](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L450-L453)）。而图像部分每卡的序列不同，必须用 all-to-all 重新按序列分发。

---

### 4.3 Ring 序列并行：环形 P2P 通信

#### 4.3.1 概念说明

Ring Attention 走的是完全不同的路子，口诀是：**「序列按卡切死不动，把 K/V 块沿环传一圈」**。

设环上 `P` 张卡，每卡持有序列的一段 `S/P`（Q/K/V 三者都在本卡、都是这一段）。要算 attention，本卡的 Q 需要看到**所有** P 段的 K/V。Ring 的做法是：

1. 把 `P` 张卡排成环（rank 0→1→2→…→P-1→0）。
2. 在每一步，每张卡用「本卡当前的 K/V」对本卡的 Q 算一次**局部** attention，得到分块 `out` 和 `lse`。
3. 然后把自己的 K/V **发给下一卡**，从上一卡**接收**新的 K/V；重复 `P` 步，每张卡就轮询过了所有段的 K/V。
4. 用 FlashAttention 的在线合并公式，把 `P` 个分块的 `out/lse` 合并成全局精确结果。

核心数学是 log-sum-exp 合并。设第 `i` 个分块给出未归一输出 \(o_i\) 与 logsumexp \(l_i=\log\sum_j e^{s_{ij}}\)，则全局合并（`update_out_and_lse`）为：

\[
l = \log(e^{l_{\text{old}}}+e^{l_i}), \quad
o = \frac{e^{l_{\text{old}}}}{e^{l}}\,o_{\text{old}} + \frac{e^{l_i}}{e^{l}}\,o_i
\]

这保证每一步只增量更新，且最终结果与「一次性算全序列」在数学上等价（仅浮点误差）。

Ring 的最大优势是**通信量与序列长成正比、且只搬 K/V 不搬 Q/输出**，对超长序列（如长视频）友好；代价是 `P` 步串行 P2P，时延随 `P` 线性增长。

#### 4.3.2 核心流程

```
Ring 的一次 forward（world_size=P 步）：
for step in 0..P-1:
    ┌─ 若非最后一步：comm.send_recv(k), comm.send_recv(v); comm.commit()  # 异步发起 P2P
    │
    ├─ 若 (非因果) 或 step<=rank：                       # 因果掩码下，跳过「未来」块
    │     block_out, block_lse = flash_attn(q, k, v)    # 复用底层 flash/sdpa kernel
    │     out, lse = update_out_and_lse(out, lse, block_out, block_lse)  # 在线 LSE 合并
    │
    └─ comm.wait(); k=next_k; v=next_v                    # 取回新 K/V，进入下一步
out = 合并后的本卡 Q 段结果（序列维仍按卡切）
```

注意 Ring 与 Ulysses 在 `Attention` 模块里的**接入位置不同**：Ulysses 是「策略」(pre/post)，而 Ring 因为要驱动整个 `P` 步循环，干脆自己实现了 `run_attention`，由 `_run_ring_attention`（[layer.py:319-333](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L319-L333)）在「kernel 这一步」整段接管（见 [layer.py:281-284](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L281-L284)）。这也是为什么 Ring 的 `pre_attention` 几乎是空的、`post_attention` 是 no-op：通信发生在 kernel 内部。

#### 4.3.3 源码精读

**(1) P2P 原语 RingComm** —— [comm.py:228-263](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L228-L263) 定义了环上的收发：

```python
self.send_rank = (self.rank + 1) % self.world_size   # 发给下一卡
self.recv_rank = (self.rank - 1) % self.world_size   # 从上一卡收
...
send_op = dist.P2POp(dist.isend, to_send, self.send_rank, group=...)
recv_op = dist.P2POp(dist.irecv, res,    self.recv_rank, group=...)
self._ops += [send_op, recv_op]
```

`commit()` 用 `batch_isend_irecv` 一次性把「发 K + 收 K'」打包异步提交（[comm.py:265-275](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L265-L275)），`wait()` 才阻塞。这种「先提交通信、期间算 attention、再 wait」的编排，让 P2P 通信与计算尽可能重叠。

**(2) 环形主循环** —— [ring_flash_attn.py:60-103](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L60-L103) 是 Ring 的心脏：

```python
for step in range(comm.world_size):
    if step + 1 != comm.world_size:
        next_k = comm.send_recv(k)   # 提前为下一步准备 K/V
        next_v = comm.send_recv(v)
        comm.commit()
    if not causal or step <= comm.rank:        # 因果掩码：跳过未来块
        step_k, step_v = k, v
        if step == 0 and joint_tensor_key is not None:   # 文本前缀只在首步拼接
            step_k = torch.cat([joint_tensor_key, step_k], dim=1); ...
        block_out, block_lse = fn(q, step_k, step_v, ...)   # 底层 flash kernel
        out, lse = update_out_and_lse(out, lse, block_out, block_lse)
    if step + 1 != comm.world_size:
        comm.wait(); k, v = next_k, next_v
```

两个关键点：

- **复用底层 kernel**：`fn = select_flash_attn_impl(attn_type, ...)`（[ring_flash_attn.py:79](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L79)），它就是普通的 FA2/FA3/FA4 kernel——Ring 没有重写 attention 数学，只是把它调用 `P` 次再合并。
- **因果掩码优化**：`causal and step == 0`（[ring_flash_attn.py:86](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L86)）只在本卡自己的块施加因果，其它块要么是「全可见的过去」（无需掩码）要么是「未来」（`step<=rank` 判断直接跳过）。

**(3) Ring 策略的薄壳** —— `RingParallelAttention`（[ring.py:36-94](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ring.py#L36-L94)）。它的 `pre_attention` 只做一件事：把 joint（文本）query 拼到 image query 前面（[ring.py:75-83](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ring.py#L75-L83)），并刻意**不**拼 joint K/V——因为 Ring 要把 joint K/V 作为「静态前缀」每步都拼到当前 K/V 上（见上面循环里的 `step == 0` 拼接，实际是每步都用），留在 `attn_metadata` 里更方便。`post_attention` 是 no-op（[ring.py:92-94](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ring.py#L92-L94)）：Ring 的输出本就按序列切好，无需反向通信。

真正的活儿在 `run_attention`（[ring.py:96-176](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ring.py#L96-L176)）：先按可用后端选 `AttnType`（FA4→FA3→AITER→FA，[ring.py:150-158](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ring.py#L150-L158)），float32 或无 flash-attn 时降级到 `ring_pytorch_attn_func`（SDPA），否则调 `ring_flash_attn_func`。

**(4) Ring 与 Ulysses 的混合** —— 当 `ulysses_degree>1` 且 `ring_degree>1` 时，工厂返回 Ulysses 策略（[factory.py:79-86](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/factory.py#L79-L86)），而 Ring 的启用由 `Attention.__init__` 里 `ring_degree > 1` 时设置 `self.ring_runner`（[layer.py:132-144](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L132-L144)）。于是前向变成「先 Ulysses 的 all-to-all（聚全序列、拆头），再在 ring_group 上跑 Ring」。混合模式有个硬约束：UAA 模式下要求「Ulysses 之后、各 ring rank 的序列长相等」，否则 Ring 的定长 P2P buffer 会失配（[ulysses.py:304-313](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/parallel/ulysses.py#L304-L313)）。

#### 4.3.4 代码实践

**目标**：把 Ring 的 `P` 步循环「在脑中单步执行」一遍，验证在线 LSE 合并的正确性。

**操作步骤**（纯源码阅读型，无需多卡）：

1. 读 [ring_flash_attn.py:60-103](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L60-L103)，假设 `world_size=3`、`rank=1`、非因果。列出 step=0/1/2 时：本卡 `k` 来自哪个原始 rank？是否进入「算+合并」分支？
2. 再读 [comm.py:245-275](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L245-L275)，画出 3 卡环上 K 的流动方向（谁发给谁）。

**需要观察的现象**：

- step=0：`k` 是本卡自己的（rank1 段）；step=1：`k` 是从 rank0 收到的（因为 rank0 的下一卡是 rank1）；step=2：`k` 是从 rank2（经 rank0）传来的。三步都满足 `not causal or step<=rank`（非因果时全算），所以合并 3 个分块。
- K 沿环 `rank0→rank1→rank2→rank0` 单向流动。

**预期结果**：你能手画一张「3 卡 × 3 步」的表格，标出每步每卡持有的 K 段来源，并解释为什么 `P` 步后每卡都见过了全部 K/V。

**多卡实测（可选）**：用 `--ulysses-degree 1 --ring-degree 2` 触发纯 Ring（参考 [sequence_parallel.md:344-353](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/sequence_parallel.md#L344-L353)）。注意 [layer.py:188-193](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L188-L193) 的约束：**Ring 与 KV-cache 量化互斥**（ring kernel 不传播 descale 因素）。实测吞吐/显存待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：Ring 的通信量与 Ulysses 相比，哪个对超长序列更友好？为什么？

> 参考答案：Ring 更友好。Ring 每步只 P2P 一个 K/V 块（大小 \(O(S/P)\)），共 `P-1` 步，总通信 \(O(S)\) 且只搬 K/V；Ulysses 的 all-to-all 一次性交换整张 Q/K/V（\(O(S\cdot H)\) 量级的数据要全员互发），当 `S` 极大时 all-to-all 的压力陡增。所以实践中「超长序列优先 Ring，追求低延迟且头数整除时用 Ulysses，二者可混合」。

**练习 2**：为什么 `ring_degree > 1` 时 `_init_kv_cache_quantization` 要直接 `raise`？

> 参考答案：Ring 把 attention 拆成 `P` 次分块调用，每个分块的 K/V 在卡间流动；量化 KV-cache 依赖的 descale 因子无法随 P2P 流动并在合并时正确还原，会导致数值错误。代码用 `raise ValueError` 而非静默降级（[layer.py:188-193](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L188-L193)），并建议改用 Ulysses。

---

## 5. 综合实践

**任务**：完成本讲规格里要求的核心实践——**比较 Ulysses 与 Ring 两种 SP 策略的「通信原语」与「拆分维度」，写出对照表，并说明它们如何与 AttentionBackend 组合**。

请综合 4.1–4.3 的源码，填写并扩展下表（这是参考答案，建议你先自己填再对照）：

| 对比维度 | Ulysses | Ring |
|----------|---------|------|
| **拆分维度** | 序列维（入口）经 all-to-all 转为**头维**（每卡：全序列 × 部分头） | 仅**序列维**（每卡：固定序列段 × 全头），头不拆 |
| **通信原语** | `all_to_all_single`（[comm.py:51](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L51)） | P2P `isend/irecv`（[comm.py:259-260](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/comm.py#L259-L260)） |
| **通信方向** | 双向：pre 散S聚H，post 散H聚S | 单向环形：K/V 块沿环传 `P-1` 步 |
| **通信频次/层** | 2 次集合通信（pre Q/K/V 合并、post 输出） | `P` 次分块调用 + `P-1` 次 P2P |
| **kernel 复用方式** | 后端 kernel 跑一次标准局部 attention（每卡全序列部分头） | 底层 flash/sdpa kernel 跑 `P` 次，用 `update_out_and_lse` 在线合并（[ring_flash_attn.py:98](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L98)） |
| **接入位置** | `pre_attention`/`post_attention`（策略层） | `run_attention`（在 kernel 步整段接管，[layer.py:281-284](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L281-L284)） |
| **形状约束** | strict：`head_cnt % ulysses_degree==0`；UAA 可 pad | ring 内各卡 post-Ulysses 序列长相等（混合时） |
| **因果掩码** | 由本地 kernel 决定，天然支持 | 支持：`step<=rank` 跳过未来块（[ring_flash_attn.py:68](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/ring_flash_attn.py#L68)） |
| **KV-cache 量化** | 兼容 | **互斥**（[layer.py:188-193](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L188-L193)） |
| **度量** | `ulysses_degree` | `ring_degree` |

**组合方式的总结**（这是本实践的「说明」部分）：

- 二者**都不改写** attention 的数学，只负责「把张量喂给 kernel 前/后的搬运」。
- Ulysses 把搬运放在 `Attention._forward_impl` 的 pre/post；搬运完直接调 `self.attention.forward(...)`（本地后端）。
- Ring 把搬运塞进自己的 `run_attention` 循环里，每步调一次底层 flash kernel，最后合并——所以它「看起来像」一个新的 attention 实现，但内核仍是后端 kernel。
- 二者**可混合**：`sp_size = ulysses_degree × ring_degree`，先 all-to-all（聚全序列拆头），再在 ring 上 P2P——这正是 vLLM-Omni 处理极长视频序列的主力方案。
- AllGather-KV 是第三条路（本地 Q 对全量 K/V），与上两者**互斥**，适合非因果、序列中等、想省通信的场景。

**进阶动手**（可选，需多卡）：跑一组对照实验，固定 `sp_size=4`，比较三种配置的吞吐与显存：`--ulysses-degree 4`、`--ring-degree 4`、`--ulysses-degree 2 --ring-degree 2`，验证「换策略不换 kernel、输出一致」。结果待本地验证。

## 6. 本讲小结

- 「**注意力后端**」（怎么算）与「**并行注意力策略**」（怎么拆）是正交两件事，靠 `Attention._forward_impl` 的三段式 `pre_attention → kernel → post_attention` 组装，换策略不换 kernel。
- 工厂 `build_parallel_attention_strategy` 按 `allgather → ulysses → ring → no-op` 优先级四选一，配置来自 forward context 的 `parallel_config`，取不到或 SP 组未就绪时安全降级为 `NoParallelAttention`。
- **Ulysses** 用 all-to-all 在「序列维 ↔ 头维」间重分布：每卡算「全序列 × 部分头」，strict 模式要求头数整除，UAA 模式靠变长 all-to-all + 头维 pad 处理不规则形状。
- **Ring** 用环形 P2P 让 K/V 块沿环传一圈，每步复用底层 flash kernel 算一个分块，用 log-sum-exp 在线合并成精确结果，对超长序列友好但与 KV-cache 量化互斥。
- 工程铁律 `ring_degree × ulysses_degree = sequence_parallel_size` 在 `initialize_model_parallel` 强校验；二者可混合（Hybrid Ulysses+Ring），混合时受「Ulysses 后各 ring rank 序列长相等」约束。
- 策略的「活动区」由 `forward_context.sp_active` 控制，SP 通信只发生在 `_sp_plan` 划定的分片区内，避免对 block 外的全量张量误操作。

## 7. 下一步学习建议

- **横向看并行策略**：本讲的 SP（Ulysses/Ring/AllGather-KV）只是 `sequence_parallel_size` 一个维度。完整的并行包括 TP/SP/PP/CFG/DP/HSDP/VAE，它们如何用 `RankGenerator` 生成正交并行组，见下一讲 [u7-4 并行策略：TP/SP/DP/CFG/PP/HSDP/VAE](u7-4-parallel-strategies.md)。
- **纵向看模型接入**：本讲默认「序列已经被切好了」，而真正把序列切下去、把输出 gather 回来的是 `_sp_plan` 钩子机制。建议阅读 [docs/design/feature/sequence_parallel.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/sequence_parallel.md) 与 `vllm_omni/diffusion/hooks/sequence_parallel.py`，理解「策略」与「模型分片计划」如何配合。
- **结合缓存**：SP 经常与 TeaCache/Cache-DiT 一起用（见 [u7-3 缓存加速](u7-l3-cache-acceleration.md)），注意 `cache_branch` 的正负分支与 SP 分片的交互。
- **实战**：选一个已支持 SP 的参考模型（如 `qwen_image_transformer.py` 或 `wan2_2_transformer.py`，见 [sequence_parallel.md:510-518](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/sequence_parallel.md#L510-L518)），对照本讲追踪一次前向，确认 `_sp_plan` 切片 → `Attention` 策略通信 → 后端 kernel 的完整链路。
