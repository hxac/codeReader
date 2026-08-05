# 前向上下文与 MoE 通信类型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「前向上下文（forward context）」是什么，以及 vllm-ascend 在每次模型前向之前往里面注入了哪些 Ascend 专属的运行期信息。
2. 读懂 `MoECommType` 四种取值（`ALLGATHER` / `MC2` / `ALLTOALL` / `FUSED_MC2`）各自代表什么通信方式，以及它们各自适用的硬件与并行场景。
3. 跟着 `select_moe_comm_method` 的决策树，判断给定配置下一次 MoE 前向会走哪条通信路径，并能指出「决定走哪条路径的关键变量」。
4. 理解 `_EXTRA_CTX` 代理如何让 NPU 算子在不传参的情况下读到这些运行期信息。

## 2. 前置知识

- **MoE（Mixture of Experts，混合专家）模型**：把一个 FFN 层拆成很多个「专家」子网络，每个 token 只激活其中少数几个专家。代表模型有 DeepSeek、Qwen3-MoE 等。
- **专家并行（Expert Parallel, EP）**：把不同的专家分布到不同 NPU 卡上。一个 token 想去的专家可能在别的卡上，所以 MoE 的核心难题就是「卡间搬 token」——也就是本讲的「MoE 通信」。
- **MC2（MindSpore Communication-Computation Concurrency / 通信计算并发）**：昇腾上的一种 MoE 通信原语，让「搬 token」和「算专家」可以重叠执行，从而隐藏通信延迟。
- **TP / DP / EP / PCP**：张量并行、数据并行、专家并行、Prefill Context Parallel，详见 u7-l1。
- 本讲承接 u2-l1（`NPUPlatform` 平台钩子）与 u2-l2（`AscendConfig` 配置体系）。`AscendConfig` 里的 `enable_prefill_mc2` / `enable_fused_mc2` 开关会在本讲被反复用到。

如果你还没看过 u2-l1，请先理解「平台钩子返回类路径字符串、由 vLLM 延迟 import」这个机制——本讲的「前向上下文」就是运行期里与之对应的信息载体。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/ascend_forward_context.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py) | 本讲主角。定义 `MoECommType` 枚举、`select_moe_comm_method` 决策函数、`set_ascend_forward_context` 上下文管理器、`_EXTRA_CTX` 访问代理，以及 MC2 容量/mask 的预留函数。 |
| [vllm_ascend/ops/fused_moe/moe_comm_method.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py) | 把 `MoECommType` 映射到具体通信实现类（`AllGatherCommImpl` / `MC2CommImpl` / `AlltoAllCommImpl` / `FusedMC2CommImpl`）。 |
| [vllm_ascend/ops/fused_moe/fused_moe.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/fused_moe.py) | `AscendMoERunner`，在 `__init__` 里调用 `setup_moe_comm_method` 把四种实现注册好。 |
| [vllm_ascend/worker/model_runner_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py) | v1 ModelRunner。初始化时预留 MC2 容量/mask；每次前向用 `set_ascend_forward_context(...)` 包裹整个 `_model_forward`。 |
| [vllm_ascend/ascend_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py) | `enable_prefill_mc2` / `enable_fused_mc2` 两个配置开关的定义处。 |
| [tests/ut/test_ascend_forward_context.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_forward_context.py) | 针对 MC2 容量计算与 `select_moe_comm_method` 决策的单元测试，是本讲实践的主要依据。 |

## 4. 核心概念与源码讲解

### 4.1 前向上下文：一次前向的运行期信息总线

#### 4.1.1 概念说明

「前向上下文（forward context）」是上游 vLLM 提供的一个**线程局部、单次前向生命周期**的临时容器。它的设计目的是：**让模型里深层的算子能够拿到「这次前向」的运行期信息，而不必把这些信息层层透传成函数参数**。

打个比方：前向上下文像是一条「传送带」。ModelRunner 在每次前向开始时把这次前向的「身份证信息」（有几个 token、用哪种注意力、是否在图捕获阶段、MoE 用哪种通信……）放到传送带上；模型里任何一层、任何一个算子，只要伸手就能从传送带上拿到对应的信息。

上游 vLLM 自己在传送带上放了 `attn_metadata`、`vllm_config`、`num_tokens` 等字段。vllm-ascend 需要的是一批 Ascend 专属字段（MoE 通信方式、是否序列并行、图捕获标记等），这些字段就是由本讲的 `set_ascend_forward_context` 负责放上去的。

#### 4.1.2 核心流程

一次 v1 前向的上下文注入流程可以概括为：

```text
NPUModelRunner.execute_model
  │
  ├── （初始化阶段，只跑一次）
  │     set_mc2_tokens_capacity(...)   # 预留 MC2 单卡 token 上限
  │     set_mc2_mask(...)              # 预留 MC2 的 bool mask 缓冲
  │
  └── （每次前向）
        with set_ascend_forward_context(attn_metadata, vllm_config, num_tokens, ...):
              │  ① 调上游 set_forward_context 把基础字段放上传送带
              │  ② select_moe_comm_method(max_num_tokens, vllm_config) → MoECommType
              │  ③ 把 moe_comm_type / moe_comm_method 写入 forward_context
              │  ④ 计算序列并行(spad/mask)、图捕获标记等其它 Ascend 字段
              │
              hidden_states = self._model_forward(...)   # 模型前向
                    │  各层算子经 _EXTRA_CTX 读取这些字段
```

关键点：**MoE 通信方式是「逐次前向」决定的，不是启动时定死的**。因为选哪条路径取决于本次 batch 的 token 数（`num_tokens`），而 token 数每个 batch 都在变。

#### 4.1.3 源码精读

入口在 v1 ModelRunner，整段 `_model_forward` 被 `set_ascend_forward_context(...)` 包裹：

```python
with (
    record_function_or_nullcontext("forward"),
    set_ascend_forward_context(
        attn_metadata,
        self.vllm_config,
        num_tokens=num_tokens_padded,
        num_tokens_across_dp=num_tokens_across_dp,
        ...
    ),
    ...
):
    hidden_states = self._model_forward(...)
```

参见 [vllm_ascend/worker/model_runner_v1.py:2027](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py#L2027) —— 这段 `with` 语句决定了「上下文只在模型前向期间有效」。

`set_ascend_forward_context` 的核心做两件事：先调用上游 `set_forward_context` 放基础字段，再追加 Ascend 字段：

```python
with set_forward_context(**forward_context_kwargs):
    forward_context = get_forward_context()
    ...
    moe_comm_type = select_moe_comm_method(max_num_tokens, vllm_config)
    forward_context.moe_comm_type = moe_comm_type
    forward_context.moe_comm_method = get_moe_comm_method(moe_comm_type)
```

参见 [vllm_ascend/ascend_forward_context.py:128-L143](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L128-L143)。注意它先算出 `max_num_tokens`（取 `num_tokens_across_dp` 的最大值，否则用本 rank 的 `num_tokens`），再传给 `select_moe_comm_method`——这是因为 DP 各 rank 的 token 数可能不同，通信方式必须按「最忙的那张卡」来选，否则就会出现「有的卡走 MC2、有的卡走 ALLTOALL」的不一致。

除了 MoE 通信，这个上下文还写入了一批运行期开关，例如：

- `capturing`：是否正处于 ACL Graph 捕获阶段（NPU 版 CUDA Graph，详见 u8-l3）；
- `mmrs_fusion`：matmul-reduce-scatter 融合开关（当 `tp_world_size <= 8` 时为真）；
- `flash_comm_v1_enabled` / `pad_size`：序列并行（FlashComm v1）开关及对齐 padding；
- `mc2_mask` / `padded_num_tokens`：MC2 路径需要的「有效 token 掩码」。

参见 [vllm_ascend/ascend_forward_context.py:151-L184](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L151-L184)。

#### 4.1.4 代码实践

**实践目标**：用一条命令把上游 `forward_context` 在前向期间「携带了哪些 Ascend 字段」打印出来。

**操作步骤**：

1. 打开 `vllm_ascend/ascend_forward_context.py`，定位到 `set_ascend_forward_context` 的 `yield` 之前（约 [L232](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L232)）。
2. （**示例代码**，仅用于阅读理解，不要真的改源码）在 `yield` 前临时加一行：
   ```python
   logger.warning("ASCEND FWD CTX: moe_comm_type=%s, flash_comm_v1=%s, num_tokens=%s, capturing=%s",
                  moe_comm_type, flash_comm_v1_enabled, num_tokens, forward_context.capturing)
   ```
3. 跑任意一个 MoE 模型的 decode 推理（或阅读 `tests/e2e/pull_request/two_card/test_sequence_parallelism_moe.py` 中 `with set_ascend_forward_context(...)` 的用法，见 [L318](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/e2e/pull_request/two_card/test_sequence_parallelism_moe.py#L318)）。

**需要观察的现象**：同一次推理的不同 batch，`moe_comm_type` 可能不同（prefill token 多时可能切到 ALLTOALL，decode token 少时回到 MC2）。

**预期结果**：日志会逐 batch 打印当前选中的 MoE 通信类型。若你无法在本地运行 NPU，则「待本地验证」——但可以通过阅读 4.3 的决策树**推断**出给定配置下的结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `moe_comm_type` 要在「每次前向」重新选择，而不是在启动时一次性确定？

> **参考答案**：因为它依赖 `num_tokens`（本 batch 的 token 数），而不同 batch 的 token 数差异巨大（prefill 可能有数千 token，decode 只有几十）。启动时无法预知运行期的 token 数，所以必须逐次前向动态选择。

**练习 2**：`set_ascend_forward_context` 用 `max_num_tokens = num_tokens_across_dp.max()` 而不是本 rank 的 `num_tokens`，原因是什么？

> **参考答案**：DP 各 rank 的 token 数可能不均。MoE 通信是集体操作（所有 rank 必须走同一条路径），只能按「token 最多的那张卡」来选，保证全体一致、避免死锁。

---

### 4.2 MoECommType：四种 MoE 通信类型

#### 4.2.1 概念说明

`MoECommType` 是一个枚举，列出 MoE 在 EP（专家并行）下「搬 token」的四种方式：

| 取值 | 含义 | 一句话直觉 |
| --- | --- | --- |
| `ALLGATHER` | 每张卡持有全部专家，token 不跨卡 | 「专家不动，我不动」——最通用、最保守 |
| `MC2` | 用昇腾 MC2 原语做通信-计算并发分发/汇总 | 「边搬边算」，隐藏通信延迟 |
| `ALLTOALL` | 经典 all-to-all：token 按目标专家搬到对应卡，算完再搬回 | 「token 去找专家」 |
| `FUSED_MC2` | 融合版 MC2（CANN MegaMoe / dispatch_ffn_combine 单算子） | 「把 dispatch+FFN+combine 融成一个 C++ 算子」 |

ALLGATHER 不需要专家并行（每卡都有全部专家），其余三种都假设 `enable_expert_parallel=True` 且 EP 组大于 1。

#### 4.2.2 核心流程

四种通信方式在 MoE 前向中的数据流对比（简化版）：

```text
ALLGATHER（无 EP）：每卡算自己分到的全部专家副本，最后 all-gather 汇总
   token → 本地所有专家 → all-gather → 输出

MC2（EP + 通信计算并发）：
   token → MC2 dispatch(分发到目标卡) ‖ 专家计算 → MC2 combine(汇总回原卡)

ALLTOALL（EP + 经典）：
   token → all-to-all(搬到目标卡) → 专家计算 → all-to-all(搬回) → 加权汇总

FUSED_MC2（EP + 单融合算子）：
   token → [CANN MegaMoe / dispatch_ffn_combine 一个算子完成 dispatch+FFN+combine]
```

关键差异：ALLTOALL 把通信和计算**串行**（先搬、再算、再搬），MC2 把它们**重叠**，FUSED_MC2 则**彻底融进一个 C++ kernel**（最快，但受 hidden_size / 量化类型等约束最多）。

#### 4.2.3 源码精读

枚举定义：

```python
class MoECommType(Enum):
    ALLGATHER = 0
    MC2 = 1
    ALLTOALL = 2
    FUSED_MC2 = 3
```

参见 [vllm_ascend/ascend_forward_context.py:26-L30](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L26-L30)。

枚举只是个「标签」，真正干活的是 `moe_comm_method.py` 里对应的实现类。`setup_moe_comm_method` 在 EP 时把四种实现都注册进字典：

```python
def setup_moe_comm_method(moe_config):
    if moe_config.ep_size > 1:
        _MoECommMethods[MoECommType.ALLTOALL] = AlltoAllCommImpl(moe_config)
        _MoECommMethods[MoECommType.ALLGATHER] = AllGatherCommImpl(moe_config)
        _MoECommMethods[MoECommType.MC2] = MC2CommImpl(moe_config)
        _MoECommMethods[MoECommType.FUSED_MC2] = FusedMC2CommImpl(moe_config)
    else:
        _MoECommMethods[MoECommType.ALLGATHER] = AllGatherCommImpl(moe_config)
```

参见 [vllm_ascend/ops/fused_moe/moe_comm_method.py:58-L65](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L58-L65)。`AscendMoERunner.__init__` 会调用它（见 [vllm_ascend/ops/fused_moe/fused_moe.py:83](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/fused_moe.py#L83)）。

四种实现的关键区别在它们各自选择的「token 分发器」与「prepare/finalize」组件：

- `AllGatherCommImpl` → `TokenDispatcherWithAllGather` + `PrepareAndFinalizeWithAllGather`，见 [moe_comm_method.py:199-L226](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L199-L226)；
- `MC2CommImpl` → `TokenDispatcherWithMC2` + `PrepareAndFinalizeWithMC2`，见 [moe_comm_method.py:229-L246](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L229-L246)；
- `AlltoAllCommImpl` → `TokenDispatcherWithAll2AllV` + `PrepareAndFinalizeWithAll2All`，见 [moe_comm_method.py:249-L270](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L249-L270)；
- `FusedMC2CommImpl` → 复用 MC2 分发器，但 `fused_experts` 直接调 CANN MegaMoe 或 `dispatch_ffn_combine` 单算子，见 [moe_comm_method.py:273-L489](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L273-L489)。

#### 4.2.4 代码实践

**实践目标**：对比 ALLTOALL 与 MC2 两种实现类的差异，并指出决定走哪条路径的关键变量。

**操作步骤**：

1. 打开 `moe_comm_method.py`，分别阅读 `AlltoAllCommImpl`（[L249](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L249)）与 `MC2CommImpl`（[L229](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L229)）的注释和 `_get_token_dispatcher` / `_get_prepare_finalize`。
2. 阅读基类 `MoECommMethod.fused_experts`（[L133-L185](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L133-L185)），它从 `_EXTRA_CTX.moe_comm_method` 取当前实现（[L147](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L147)），执行 `token_dispatch → apply_mlp → token_combine` 三步。

**需要观察的现象**：两者的 dispatcher 与 prepare/finalize 组件不同——ALLTOALL 用 `All2AllV` 做 token 跨卡搬运，MC2 用 MC2 原语做通信-计算并发。

**预期结果**：能写出下面这段对比（也是本讲的核心实践任务，见第 5 节综合实践）：

> - **ALLTOALL**：token 先 all-to-all 搬到目标专家所在卡 → 本地专家计算 → all-to-all 搬回 → 加权求和。通信与计算**串行**，适合 token 数超过 MC2 容量的大 batch。
> - **MC2**：用昇腾 MC2 原语（`npu_moe_distribute_dispatch` / `npu_moe_distribute_combine`）做分发与汇总，**通信与计算重叠**，适合 token 数在容量以内的小/中 batch。
> - **决定走哪条路径的关键变量**：在 A3 上是 `num_tokens` 与 `mc2_tokens_capacity` 的比较——`num_tokens <= mc2_tokens_capacity` 走 MC2，否则走 ALLTOALL（详见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：`ALLGATHER` 与另外三种最根本的区别是什么？

> **参考答案**：ALLGATHER 不依赖专家并行——每张卡都持有全部专家副本，token 不跨卡流动，只在最后做一次 all-gather 汇总。另外三种都要求 `enable_expert_parallel=True` 且 EP 组 > 1，token 需要跨卡搬到目标专家。

**练习 2**：为什么 `FusedMC2CommImpl` 要把 dispatch+FFN+combine 融成一个 C++ 算子，而不是继续用 MC2 的三步分离实现？

> **参考答案**：分离实现里 dispatch / FFN / combine 是三个独立 kernel launch，之间存在访存与同步开销；融合成单个 C++ 算子（CANN MegaMoe）后，中间结果可以留在片上高速缓存、省去多次 HBM 往返，通信与计算也能在算子内部更深地重叠，从而拿到更高吞吐。代价是它对 hidden_size 区间、量化类型、EP 规模有严格约束（见 4.3）。

---

### 4.3 通信选择决策树：select_moe_comm_method

#### 4.3.1 概念说明

`select_moe_comm_method(num_tokens, vllm_config)` 是本讲的核心决策函数。它的职责是：**根据「芯片型号 + 并行配置 + 本次 token 数」，从四种通信方式里挑出一种**。

影响决策的关键输入有三类：

1. **芯片型号**（`get_ascend_device_type()`）：A2 / A3 / A5 / 310P，不同代际硬件支持的通信原语不同；
2. **并行配置**：是否开 EP、EP 组大小、LoRA 是否开启；
3. **运行期 token 数** 与 **MC2 容量**（`mc2_tokens_capacity`）：token 太多会撑爆 MC2 的预分配缓冲，只能回退到 ALLTOALL/ALLGATHER。

#### 4.3.2 核心流程

`select_moe_comm_method` 的决策树（按代码顺序）：

```text
select_moe_comm_method(num_tokens, vllm_config):
  ① 非 MoE 模型?                         → 返回 None
  ② 未开 EP 或 EP 组 == 1?                → ALLGATHER
  ③ 开了 EP 且开了 LoRA?                  → ALLTOALL   （MC2/FusedMC2 是单融合算子，无法被 LoRA 打补丁）
  ④ 按芯片型号分流：
     A2  → _select_a2:  专家/卡≤24 且 EP≥16 且 token≤容量 → MC2；否则 ALLGATHER
     A3  → _select_a3:  enable_fused_mc2 且满足 MegaMoe/dispatch_ffn 约束 → FUSED_MC2；
                        否则 token≤容量 → MC2；否则 ALLTOALL
     A5  → _select_a5:  token≤容量 且 world>1 → MC2；
                        world≤topK → ALLGATHER；否则 ALLTOALL
     310P→ ALLGATHER（310P 不支持 MC2 类原语）
  ⑤ 其它型号 → raise ValueError
```

其中「MC2 容量」`mc2_tokens_capacity` 由 `set_mc2_tokens_capacity` 在初始化时算好，本质是「单卡最多能塞进 MC2 缓冲的 token 数 × TP 卡数」。它的计算来源分三种（按优先级）：

- 开了 `enable_prefill_mc2` → 用 `max_num_batched_tokens`；
- 否则若设了 cudagraph capture sizes → 用 `max_cudagraph_capture_size`；
- 否则 → `max_num_reqs * uniform_decode_query_len`。

容量还会被上限封顶：FUSED_MC2 路径受 `_MEGA_MOE_TOKENS_PER_RANK_LIMIT = 4096`（或无 MegaMoe 时的 512）约束，普通 MC2 受 `_MC2_TOKENS_PER_RANK_LIMIT = 512` 约束。

容量计算的数学表达（向上取整对齐到 TP 卡数）：

\[
\text{tokens\_per\_rank} = \left\lceil \frac{\text{max\_num\_tokens}}{\text{tp\_size}} \right\rceil
\]

\[
\text{mc2\_tokens\_capacity} = \text{tokens\_per\_rank} \times \text{tp\_size}
\]

#### 4.3.3 源码精读

主决策函数及注释（注释本身把规则总结得很清楚，值得逐条读）：

```python
def select_moe_comm_method(num_tokens, vllm_config) -> MoECommType | None:
    if not is_moe_model(vllm_config):
        return None
    ...
    if not vllm_config.parallel_config.enable_expert_parallel or get_ep_group().world_size == 1:
        moe_comm_type = MoECommType.ALLGATHER
    elif lora_config is not None and vllm_config.parallel_config.enable_expert_parallel:
        moe_comm_type = MoECommType.ALLTOALL
    elif soc_version == AscendDeviceType.A2:
        moe_comm_type = _select_a2_moe_comm_method(...)
    elif soc_version == AscendDeviceType.A3:
        moe_comm_type = _select_a3_moe_comm_method(...)
    elif soc_version == AscendDeviceType.A5:
        moe_comm_type = _select_a5_moe_comm_method(...)
    elif soc_version == AscendDeviceType._310P:
        moe_comm_type = MoECommType.ALLGATHER
    else:
        raise ValueError(...)
```

参见 [vllm_ascend/ascend_forward_context.py:344-L399](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L344-L399)。注意 LoRA+EP 强制走 ALLTOALL，注释说明了原因：MC2/FusedMC2 是单个融合 C++ 算子，Ascend MoE LoRA 无法对它打补丁。

A3 的选择最能体现「先试快的、不行再降级」的思路：

```python
def _select_a3_moe_comm_method(num_tokens, mc2_tokens_capacity, vllm_config) -> MoECommType:
    if get_ascend_config().enable_fused_mc2 == 1:
        mega_moe_enable = get_ep_group().world_size <= 64 and _cann_megamoe_supported_by_config(vllm_config)
        dispatch_ffn_combine_enable = get_ep_group().world_size <= 32
        if (_MEGA_MOE_SUPPORTED and mega_moe_enable) or dispatch_ffn_combine_enable:
            return MoECommType.FUSED_MC2
    if num_tokens is None or num_tokens <= mc2_tokens_capacity:
        return MoECommType.MC2
    return MoECommType.ALLTOALL
```

参见 [vllm_ascend/ascend_forward_context.py:308-L323](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L308-L323)。这里 `_MEGA_MOE_SUPPORTED` 是运行期探测 CANN 是否提供了 `cann_ops_transformer`（见 [L43](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L43)），`_cann_megamoe_supported_by_config` 则校验 hidden_size 必须落在 \([1024, 8192]\) 且是 512 的倍数、量化类型在白名单内（见 [L69-L93](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L69-L93)）。

MC2 容量预留函数：

```python
def set_mc2_tokens_capacity(vllm_config, max_num_reqs, uniform_decode_query_len):
    ...
    num_tokens_per_tp_rank = (max_num_tokens + tp_size - 1) // tp_size
    if get_ascend_config().enable_fused_mc2:
        if _MEGA_MOE_SUPPORTED:
            num_tokens_per_tp_rank = min(num_tokens_per_tp_rank, _MEGA_MOE_TOKENS_PER_RANK_LIMIT)
        else:
            num_tokens_per_tp_rank = min(num_tokens_per_tp_rank, _DISPATCH_FFN_COMBINE_TOKENS_PER_RANK_LIMIT)
    else:
        num_tokens_per_tp_rank = min(num_tokens_per_tp_rank, _MC2_TOKENS_PER_RANK_LIMIT)
    _mc2_tokens_capacity = num_tokens_per_tp_rank * tp_size
```

参见 [vllm_ascend/ascend_forward_context.py:242-L266](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L242-L266)。它在 v1 ModelRunner 初始化时被调用一次（见 [vllm_ascend/worker/model_runner_v1.py:452](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py#L452)）。

#### 4.3.4 代码实践

**实践目标**：运行 `select_moe_comm_method` 的单元测试，验证决策树，并回答「A3 上 token 数与容量的关系如何决定 MC2 / ALLTOALL」。

**操作步骤**：

1. 阅读测试 [tests/ut/test_ascend_forward_context.py:197-L217](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_forward_context.py#L197-L217)（`test_select_moe_comm_method_a3_without_fused_mc2`）。该测试把 `capacity` 钉死为 128，参数化两种 token 数：128 → MC2，129 → ALLTOALL。
2. 在无 NPU 的 UT 环境（设 `COMPILE_CUSTOM_KERNELS=0`，详见 u1-l3）下运行该测试：
   ```bash
   pytest tests/ut/test_ascend_forward_context.py -k "a3_without_fused_mc2 or a3_enable_fused_mc2" -v
   ```

**需要观察的现象**：`num_tokens=128`（恰等于容量）走 MC2；`num_tokens=129`（刚超容量）切到 ALLTOALL。

**预期结果**：测试通过。这正好印证了 `_select_a3_moe_comm_method` 里 `num_tokens <= mc2_tokens_capacity ? MC2 : ALLTOALL` 的边界条件。若你本地无法跑 pytest，则「待本地验证」，但结论可从源码 [L320-L323](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L320-L323) 直接推断。

#### 4.3.5 小练习与答案

**练习 1**：在 A3、`enable_fused_mc2=1`、EP 组=128、`num_tokens=4097`、容量=128 的条件下，会选哪种通信？为什么？

> **参考答案**：选 `ALLTOALL`。因为 `enable_fused_mc2=1` 时，FUSED_MC2 需要 EP 组 ≤ 64（MegaMoe）或 ≤ 32（dispatch_ffn_combine），这里 EP=128 两个条件都不满足，无法走 FUSED_MC2；而 `num_tokens=4097 > 容量=128`，也不满足 MC2 的容量条件，于是回退到 ALLTOALL。这正是测试 [L175](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_forward_context.py#L175) 覆盖的用例。

**练习 2**：`_cann_megamoe_supported_by_config` 为什么要把 `hidden_size` 限制在 \([1024, 8192]\) 且是 512 的倍数？

> **参考答案**：这是 CANN MegaMoe 内核的硬件约束——它的 dispatch/FFN/combine cube 分块要求 hidden 落在闭区间 \([1024, 8192]\) 且是 cube K-step（512）的倍数。超出范围（如 hidden=896 的小 Qwen 变体，或 hidden=9216 的大 head）会被静默回退到 MC2，避免算子启动失败。

---

### 4.4 _EXTRA_CTX：跨算子的统一读取代理

#### 4.4.1 概念说明

前面三节都在讲「怎么往传送带上放信息」。这一节讲「算子怎么从传送带上取信息」。

上游 vLLM 的 forward context 对象允许任意属性读写，但 vllm-ascend 想要一个**受控的、有白名单的、且能同时兼容 v1/v2 两种 ModelRunner** 的访问入口。于是它定义了一个单例代理 `_EXTRA_CTX`（`_ExtraForwardContextProxy` 的实例）。

它的设计要点：

1. **白名单**：只有 `extra_attrs` 元组里列出的属性名（如 `moe_comm_type`、`capturing`、`flash_comm_v1_enabled` 等）才允许读写，写错名字会直接抛 `AttributeError`，把 bug 提前暴露。
2. **v1/v2 兼容**：v1 把这些字段直接挂在 forward context 对象上；v2 ModelRunner 则把它们放进 `ctx.additional_kwargs` 字典。代理在 `__getattr__`/`__setattr__` 里按 `VLLM_USE_V2_MODEL_RUNNER` 自动切换存储位置。
3. **惰性默认值**：未设置的属性在 v2 下返回 `None`（而非抛错），让 `if _EXTRA_CTX.sinks:` 这类真值判断在前向早期也能安全运行。

#### 4.4.2 核心流程

算子读取运行期信息的流程：

```text
某 NPU 算子（如 fused_experts / attention / quant）
   │  执行 _EXTRA_CTX.moe_comm_method
   ↓
_ExtraForwardContextProxy.__getattr__("moe_comm_method")
   │  ① check_extra_attr: 在白名单? 否 → AttributeError
   │  ② get_forward_context(): 取当前线程的 forward context
   │  ③ V2?  → ctx.additional_kwargs.get(name)
   │     V1?  → getattr(ctx, name, None)
   ↓
   返回当前前向注入的 MoECommMethod 实例（或 None）
```

#### 4.4.3 源码精读

代理类定义与白名单：

```python
class _ExtraForwardContextProxy:
    extra_attrs = (
        "capturing", "moe_comm_type", "moe_comm_method", "mmrs_fusion",
        "num_tokens", "flash_comm_v1_enabled", "pad_size", "padded_length",
        ...  # 共 20+ 个受控属性
    )

    def __getattr__(self, name):
        self.check_extra_attr(name)
        ctx = self._ctx()
        if envs_vllm.VLLM_USE_V2_MODEL_RUNNER:
            return ctx.additional_kwargs.get(name)
        return getattr(ctx, name, None)

    def __setattr__(self, name, value):
        self.check_extra_attr(name)
        ctx = self._ctx()
        if envs_vllm.VLLM_USE_V2_MODEL_RUNNER:
            ctx.additional_kwargs[name] = value
        else:
            setattr(ctx, name, value)

_EXTRA_CTX = _ExtraForwardContextProxy()
```

参见 [vllm_ascend/ascend_forward_context.py:410-L469](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L410-L469)。

一个典型的消费点是 MoE 的 `fused_experts`：它从 `_EXTRA_CTX.moe_comm_method` 拿到当前前向选中的通信实现，再执行 dispatch → mlp → combine：

```python
moe_comm_method = _EXTRA_CTX.moe_comm_method
assert moe_comm_method is not None, "Missing communication context"
```

参见 [vllm_ascend/ops/fused_moe/moe_comm_method.py:147-L148](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/moe_comm_method.py#L147-L148)。其它消费点散布在注意力（`_EXTRA_CTX.capturing`、`_EXTRA_CTX.is_draft_model`）和图捕获补丁（`_EXTRA_CTX.moe_comm_type == MoECommType.ALLTOALL`）等处。

#### 4.4.4 代码实践

**实践目标**：统计 `_EXTRA_CTX` 在代码库里被读取的字段分布，理解「哪些运行期信息最常被算子消费」。

**操作步骤**：

1. 在仓库根目录搜索 `_EXTRA_CTX\.` 的所有出现（用 Grep 工具，pattern 为 `_EXTRA_CTX\.`，type 为 `py`）。
2. 按字段名分组统计，例如 `moe_comm_method`、`capturing`、`is_draft_model`、`flash_comm_v1_enabled` 各出现多少次、在哪些文件。

**需要观察的现象**：注意力后端（`attention/`）大量读取 `is_draft_model` / `capturing`；MoE 相关代码读取 `moe_comm_method` / `moe_comm_type`；序列并行补丁读取 `flash_comm_v1_enabled`。

**预期结果**：你能画一张「字段 → 主要消费方」的对照表，例如 `capturing` 主要被 ACL Graph 相关路径读取（决定是否走图回放），`moe_comm_type` 主要被 routed experts 捕获补丁读取（见 [vllm_ascend/patch/worker/patch_routed_experts_capture.py:151](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_routed_experts_capture.py#L151)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_ExtraForwardContextProxy` 要维护一个 `extra_attrs` 白名单，而不是允许任意属性读写？

> **参考答案**：为了把「拼写错误 / 用错属性名」这类 bug 提前到访问时暴露。forward context 对象本身允许任意属性，如果不加白名单，写错名字（比如 `moe_commmethod`）会静默返回 `None`，导致后续 `assert moe_comm_method is not None` 在很深的地方才报错，难以排查。白名单 + `AttributeError` 让错误在发生点立即显现。

**练习 2**：v1 和 v2 ModelRunner 对这些字段的存储位置有何不同？代理如何兼容两者？

> **参考答案**：v1 把字段直接挂在 forward context 对象上（`setattr(ctx, name, value)`）；v2 ModelRunner 用一个 `additional_kwargs` 字典来存（`ctx.additional_kwargs[name] = value`）。代理在 `__getattr__`/`__setattr__` 里读取 `envs_vllm.VLLM_USE_V2_MODEL_RUNNER`，按其真假分别走字典或对象属性两条路径，从而对上层算子屏蔽差异。

---

## 5. 综合实践

**任务**：给定一个具体配置，端到端推断一次 MoE 前向会选中哪种通信方式，并用单元测试验证你的推断。

**场景**：A3 卡、`enable_expert_parallel=True`、EP 组大小=8、`enable_fused_mc2=1`、量化类型 `w4a8`、`hidden_size=2048`、`num_tokens=4097`、MC2 容量被钉为 128。

**操作步骤**：

1. **判断是否 MoE**：是 MoE → 继续。
2. **判断 EP**：EP 开启且 EP 组=8 > 1 → 不是 ALLGATHER。
3. **判断 LoRA**：无 LoRA → 不强制 ALLTOALL。
4. **进入 A3 分支**（[L308-L323](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_forward_context.py#L308-L323)）：
   - `enable_fused_mc2==1`；
   - EP 组=8 ≤ 64，且 `w4a8` 满足 MegaMoe 量化白名单、hidden=2048 在 \([1024,8192]\) 且整除 512 → `mega_moe_enable=True`；
   - 若 CANN 提供 `cann_ops_transformer`（`_MEGA_MOE_SUPPORTED`）→ 返回 **FUSED_MC2**；否则 `dispatch_ffn_combine_enable`（EP≤32）也为真 → 仍返回 **FUSED_MC2**。
5. **验证**：这与测试 `test_select_moe_comm_method_a3_enable_fused_mc2_mode_1` 的 `(4097, 8, MoECommType.FUSED_MC2)` 用例完全一致，见 [tests/ut/test_ascend_forward_context.py:169-L194](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_forward_context.py#L169-L194)。注意：FUSED_MC2 的选择**不看 token 数与容量**，所以即使 `num_tokens=4097` 远超容量 128，只要满足 EP/量化约束就走 FUSED_MC2——这正是它与普通 MC2 的关键区别。

**延伸思考**：如果把 EP 组调到 128，其余不变，结果会变成什么？（提示：FUSED_MC2 的两个使能条件都不满足了，于是回落到 `num_tokens <= 容量` 的判断，4097 > 128 → ALLTOALL。对应测试 [L175](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_forward_context.py#L175)。）

## 6. 本讲小结

- **前向上下文**是上游 vLLM 提供的「单次前向生命周期」信息容器，vllm-ascend 通过 `set_ascend_forward_context` 在每次前向往里注入 Ascend 专属的运行期字段（MoE 通信方式、图捕获标记、序列并行开关等）。
- `MoECommType` 定义了四种 MoE 通信方式：**ALLGATHER**（无 EP，最通用）、**MC2**（通信-计算并发）、**ALLTOALL**（经典跨卡搬运）、**FUSED_MC2**（CANN 单融合算子，最快但约束最多）。
- `select_moe_comm_method` 按「芯片型号 + EP + LoRA + token 数 vs MC2 容量」逐级决策；A2 只在 MC2/ALLGATHER 间选，A3 优先 FUSED_MC2 再降级 MC2/ALLTOALL，A5 三者皆可，310P 固定 ALLGATHER，LoRA+EP 强制 ALLTOALL。
- **MC2 容量**由 `set_mc2_tokens_capacity` 在初始化时按 `max_num_batched_tokens` / cudagraph capture size / `max_num_reqs*decode_len` 之一计算，并受 MegaMoe/MC2 的单卡 token 上限封顶。
- `_EXTRA_CTX` 是带白名单、兼容 v1/v2 的 forward context 访问代理，让深层算子能安全、统一地读到这些运行期信息。
- 决定「ALLTOALL 还是 MC2」的关键变量，在 A3 上是 `num_tokens` 与 `mc2_tokens_capacity` 的大小关系（≤ 容量走 MC2，否则 ALLTOALL）；而 FUSED_MC2 是否被选中，取决于 `enable_fused_mc2`、EP 组规模与量化/hidden 约束，与 token 数无关。

## 7. 下一步学习建议

- 想看 MoE 通信被「真正执行」的细节，接着读 **u7-l3（Fused MoE 引擎与通信）**，那里会拆解 `token_dispatcher` 与 `prepare_finalize` 在 ALLTOALL/MC2 下的具体搬运逻辑。
- 想理解图捕获阶段为什么也要选通信方式，看 **u8-l3（ACL Graph 捕获与回放）**，并结合 `patch_routed_experts_capture.py` 里 `_EXTRA_CTX.moe_comm_type == MoECommType.ALLTOALL` 的分支。
- 想了解序列并行（`flash_comm_v1_enabled`）如何与 MoE 通信联动，看 **u8-l2（torch.fx 融合 Pass 实战）** 的 `sequence_parallelism` pass。
- 若你对 `enable_prefill_mc2` / `enable_fused_mc2` 的配置来源感兴趣，可回看 **u2-l2（AscendConfig 与 envs）** 中 `additional_config` 的解析机制。
