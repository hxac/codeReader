# 训练全链路：fwd/bwd 四象限

## 1. 本讲目标

前面十讲我们把 MoonEP 拆成了一个个零件：规划器（u3）、dispatch/combine 内核族（u4）、权重预取与梯度归约（u5）、异步流与零拷贝（u6-l1、u6-l2）。本讲把这些零件**装回一台完整的机器**——一次 MoE 训练步。

读完本讲，你应该能够：

1. 画出训练步四象限（dispatch fwd / combine fwd / combine bwd / dispatch bwd）的 API 调用顺序与每一步的张量流转。
2. 说清 plan 复用路径（combine bwd 用保存的 plan 重新 dispatch）**为什么免规划、免预取、免重建去重结构**。
3. 理解 `cu_seqlens` 如何驱动分组 GEMM、以及它与 `[E+B, H, H']` 权重契约的关系。
4. 划清 `reduce_grad` 与训练框架自身梯度归约（DDP all-reduce 等）的边界与调用时机。

## 2. 前置知识

本讲是「编排层」讲义，不再深入内核内部，但假设你已建立以下认知（对应前置讲义）：

- **autograd 的对偶视角**：复合算子 `dispatch → 专家FFN → combine` 的反向执行顺序是 `combine 的反向 → FFN 的反向 → dispatch 的反向`。本讲的核心问题就是：**combine 的反向是什么？dispatch 的反向又是什么？**
- **dispatch / combine 的语义**（u1-l4、u4）：dispatch 把 token-major 的 `[S, H]` 散射成 expert-grouped 的 `[NvS, H]`（去重：同 token 落同 rank 只传一份 payload）；combine 把 K 份派发结果按路由权重求和回 `[S, H]`。
- **MoonEPCommPlan**（u3-l1）：frozen dataclass，规划、派发、归并、梯度归约之间唯一的共享契约，含 `dst` 表、去重三件套、`experts_to_copy` 等；**必须跨前向、反向保存**。
- **权重 / 梯度缓冲布局**（u5-l1、u5-l3）：每投影一个 `[E+B, H, H']` 连续张量，前 E 行是映射来的全组专家，后 B 行是预取槽；训练侧梯度是它的 fp32 镜像，预取槽梯度由独立的 `[R, B, H, H']` 归约缓冲背书。
- **`cu_seqlens` 分段语义**（u2-l1、u3-l3）：`[E+B]` 的分段结束偏移，每段向上对齐 `token_padding`；接收缓冲总容量 `NvS = S·K + (token_padding−1)·2·E/R`。

不需要重新读内核代码——本讲只引用三个文件：`README.md`、`moonep/api.py`、`tests/test_e2e.py`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的段落 |
| --- | --- | --- |
| [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md) | 用户契约文档 | API walkthrough 的四象限章节、权重/梯度缓冲契约 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | Buffer 门面，四入口的唯一实现 | 模块 docstring 的四象限用法、`dispatch` 的 plan 分支、`prefetch_weight` / `combine` / `reduce_grad` 的 docstring 与发射编排 |
| [tests/test_e2e.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py) | 官方端到端用法范本 | `test_e2e` 的场景矩阵：sync/async 对拍、plan 复用、免预取、zero_copy、梯度归约 |

辅以 [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) 中的 `MoonEPCommPlan` 定义与 `allocate_planning_outputs`（已在 u3-l1 精读，本讲只引用结论）。

## 4. 核心概念与源码讲解

### 4.1 Buffer 四入口与训练步四象限

#### 4.1.1 概念说明

MoonEP 对外只有 4 个入口：`dispatch`、`prefetch_weight`、`combine`、`reduce_grad`。困惑的根源在于命名：**入口名是算子名，而不是方向名**。一个训练步里每个入口可能被调用一次，但「方向」有四个：

- **dispatch fwd**：dispatch 的前向——把 token 散射到远端专家分组位置（附带在线规划）。
- **combine fwd**：combine 的前向——把 K 份专家输出加权求和回 token-major。
- **combine bwd**：combine 的反向——把输出梯度 `[S, H]` 重新散射回 expert-grouped 序。它**恰好就是一次 dispatch**（复用保存的 plan），所以调用的是 `buffer.dispatch(grad_output_sh, plan=plan)`。
- **dispatch bwd**：dispatch 的反向——把 K 份梯度拷贝求和回 token-major。它**恰好就是一次 combine**，另外权重侧还要补一次 `reduce_grad`。

为什么两个算子互相充当对方的反向？用矩阵语言看最直观。设 \(P\) 为规划器确定的散射布局（`dst` 表编码的「token 条目 → 接收槽位」映射，含去重与 padding；严格说不是置换矩阵，理解为布局表即可），\(W=\mathrm{diag}(w)\) 为路由权重对角阵，\(F\) 为逐段专家 FFN：

\[ H_{\text{nvsh}} = P\, x_{\text{flat}} \qquad\text{（dispatch fwd：散射，去重后仅主行承载 payload）} \]

\[ y_s = \sum_{k=1}^{K} w_{s,k}\, f_{e_{s,k}}(x_s) \qquad\text{（combine fwd：加权 K 求和）} \]

反向时梯度要沿同一条路流回去：

\[ \nabla_{\text{nvsh}} = P\, \nabla_y \qquad\text{（combine bwd：同一散射布局 → re-dispatch）} \]

\[ \nabla_x = P^{\top} \nabla_{\text{nvsh}} \qquad\text{（dispatch bwd：散射的转置 = 求和 → combine）} \]

即 **combine 的雅可比是 P 的转置，其反向就是 P 本身（dispatch）；dispatch 的反向就是 P 的转置（combine）**。MoonEP 不为反向写新内核，直接让两个入口互为对偶。（路由权重 \(w_{s,k}\) 这个标量因子何时乘回梯度，由集成框架的 autograd 决定——plan 复用路径的 dispatch 不碰权重，框架可以在 shard 上用 dispatch fwd 散射好的 `route_weights_nvs` 完成。）

#### 4.1.2 核心流程

一次训练 microbatch 的完整调用序列（README 的 API walkthrough 顺序）：

```text
前向：
  Q1  dispatch fwd        buffer.dispatch(hidden, rw_sk, topk, tpe)
                          → (h_nvsh, w_nvs, cu_seqlens, plan)   # 内含在线规划
      prefetch_weight     buffer.prefetch_weight(plan, gate/up/down)  # 权重侧
      分组 FFN            按 cu_seqlens 分段读 [E+B,H,H'] 权重做组 GEMM
  Q2  combine fwd         buffer.combine(plan, expert_out_nvsh, w_nvs)
                          → (out_sh, gathered_rw)

反向（autograd 逆序）：
  Q3  combine bwd         buffer.dispatch(grad_out_sh, plan=plan)
                          → grad_eo_nvsh（cu_seqlens 为 None：免规划）
      FFN 反向            复用 fwd 已预取的权重槽；产出 grad_h_nvsh 与权重梯度
  Q4  dispatch bwd        buffer.combine(plan, grad_h_nvsh) → grad_hidden_sh
      权重梯度归约        buffer.reduce_grad(plan, full_*_grad, *_reduce_buffer)

收尾：
  buffer.destroy()        必须先于 dist.destroy_process_group()
```

四象限对照表：

| 象限 | API | 输入 | 输出 | README 章节 |
| --- | --- | --- | --- | --- |
| dispatch fwd | `dispatch(...)` + `prefetch_weight` | `hidden_sh [S,H]`、`rw_sk [S,K]`、`topk_sk [S,K]`、`tpe [E]` | `h_nvsh [NvS,H]`、`w_nvs [NvS]`、`cu_seqlens [E+B]`、`plan` | [README.md:L83-L104](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L83-L104) |
| combine fwd | `combine(plan, ...)` | 专家输出 `nvsh [NvS,H]`、可选 `w_nvs` | `out_sh [S,H]`、`gathered_rw [S,K]` | [README.md:L130-L140](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L130-L140) |
| combine bwd | `dispatch(grad, plan=plan)` | `grad_out_sh [S,H]` + 保存的 plan | `grad_eo_nvsh [NvS,H]`（`cu_seqlens=None`） | [README.md:L142-L152](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L142-L152) |
| dispatch bwd | `combine(plan, ...)` + `reduce_grad` | `grad_h_nvsh [NvS,H]`、梯度缓冲 | `grad_hidden_sh [S,H]` + 归约后的参数梯度 | [README.md:L106-L128](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L106-L128) |

#### 4.1.3 源码精读

四象限的权威定义写在 api.py 的模块 docstring 里，它就是官方推荐的调用模板：

- [moonep/api.py:L13-L29](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L13-L29)：dispatch fwd（dispatch + prefetch_weight）与 combine fwd 的调用样板，注释明确 plan 要「save it for prefetch/combine and both backward passes」。
- [moonep/api.py:L31-L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L31-L32)：combine bwd——**用保存的 plan 重新 dispatch 输出梯度**，注释点明「re-dispatch the output grad with the saved plan」。
- [moonep/api.py:L34-L45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L34-L45)：dispatch bwd——先 `combine(plan, grad_hidden_nvsh)` 把梯度求和回 token-major，再 `reduce_grad` 把冗余专家的权重梯度归约回家。
- [moonep/api.py:L47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L47)：`buffer.destroy()` 收尾。

四个入口中 plan 的地位并不对称，源码里的断言一目了然：

- [moonep/api.py:L726](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L726)：`dispatch` 的 `plan` 参数默认 `None`——None 触发在线规划，传入则复用。
- [moonep/api.py:L899](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L899)、[L999](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L999)、[L1133](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1133)：`prefetch_weight` / `combine` / `reduce_grad` 都断言 `plan is required`——它们没有规划能力，必须消费 dispatch 产出的 plan。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，用 `inspect` 把四个入口的签名打出来，亲眼确认「哪些参数必传、哪些可选、plan 在每个入口中的地位」，为画四象限图做准备。

**操作步骤**（示例代码，需已 `pip install -e .`，无需多卡）：

```python
# sig_tour.py（示例代码）
import inspect
from moonep import Buffer

for name in ("dispatch", "prefetch_weight", "combine", "reduce_grad"):
    print(f"{name}{inspect.signature(getattr(Buffer, name))}")
```

**需要观察的现象**：

- `dispatch` 的 `plan: MoonEPCommPlan | None = None` 排在位置参数之后——它是唯一「可选 plan」的入口。
- `prefetch_weight` 的三个权重张量在 `*` 之后，是 keyword-only 参数。
- `combine` / `reduce_grad` 的 `plan` 虽在签名里带默认值 `None`，但函数体第一步就断言非空。

**预期结果**：打印出 4 个签名，与 [moonep/api.py:L720-L732](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L720-L732)、[L860-L871](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L860-L871)、[L950-L960](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L950-L960)、[L1085-L1095](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1085-L1095) 一致。若尚未编译 `moonep._C`，`import moonep` 会直接失败——这是 u1-l2 讲过的两层结构所致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 combine 的反向不需要一个新的 gather 内核，直接复用 dispatch 就够了？

**答案**：combine fwd 是「按散射布局 P 的加权求和」，数学上是 \(P^{\top} W\) 作用；它的反向就是把 \(\nabla_y\) 按同一个 P 散射回去（权重因子由框架处理），这正是 dispatch 的语义。同一份 plan 里的 `dst` 表、去重结构、分段布局在两个方向通用，重写内核是纯浪费。

**练习 2**：一个训练 microbatch 中，`dispatch`（fresh 规划路径）总共被调用几次？

**答案**：1 次，在前向。反向的两次数据搬运分别是 `dispatch(grad, plan=plan)`（combine bwd，复用 plan）和 `combine(plan, grad_h_nvsh)`（dispatch bwd）——前者跳过规划，后者本来就不管规划。在线规划每层每 microbatch 只发生一次，这是 MoonEP 规划开销可控的根源。

**练习 3**：判断对错：`prefetch_weight` 内部会自己调用规划器决定搬哪些专家。

**答案**：错。它只消费 `plan.experts_to_copy`（见 [moonep/api.py:L899](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L899) 的断言与后文发射逻辑），规划只发生在 `dispatch` 的 fresh 路径里。

### 4.2 前向链路：dispatch fwd → prefetch → cu_seqlens 组 GEMM → combine fwd

#### 4.2.1 概念说明

前向象限的关键是理解 **MoonEP 与框架的契约面**：MoonEP 不做专家计算，它交付给框架的是两样东西——

1. **一个 `[NvS, H]` 的 expert-grouped shard**（`hidden_nvsh`），token 已按接收 rank 的 VM 分组顺序排好；
2. **一张 `cu_seqlens[E+B]` 分段表**，告诉框架每个专家行（含预取槽行）对应 shard 的哪一段。

框架拿这两样东西去驱动**组 GEMM**：第 e 段的 token 行乘以权重张量的第 e 行。README 把这句话写成正式契约——「one contiguous symmetric-memory weight tensor per expert projection, plus a planner-produced `cu_seqlens`」（[README.md:L45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L45)）。因为被复制的专家 token 段被规划器指向**预取槽行**（rows `[E, E+B)`），组 GEMM 只按行索引寻址，根本感知不到「这个专家是本地的还是复制来的」。

#### 4.2.2 核心流程

`buffer.dispatch(hidden, rw_sk, topk, tpe)` 一次调用在内部按顺序做了 4 件事（对应 `_run_dispatch_on_current_stream` 的编排）：

```text
1. inter_rank_sync      # 可选：对齐各 rank 的发令枪（默认开）
2. launch_planning      # fresh 路径：消费 topk/tpe，产出 plan + cu_seqlens
3. launch_dispatch      # 散射 payload（去重）+ 权重；零填充 warp 清 padding 行
   launch_dispatch_epilogue   # 本地展开重复槽，补齐用户可见的 [NvS,H]
4. 边界拷贝             # zero_copy=False 时把 NVL shard 拷到返回的新张量
```

随后 `prefetch_weight` 独立发射（3 个投影各一次内核 + 可选 scale）；框架做组 GEMM；最后 `combine` 内部是「边界拷贝入 shard → prologue 重复组累加 → combine K 求和（可选权重收集）」三步。

#### 4.2.3 源码精读

- [moonep/api.py:L784-L791](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L784-L791)：fresh 分支——校验 `topk_flat`（int32、N 个元素）与 `tokens_per_expert`（int32、E 个、连续），然后 `allocate_planning_outputs(ctx)` 生成全新 plan 与 cu_seqlens，打包成 `planning_args` 待发射。
- [moonep/api.py:L797-L808](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L808)：输出张量的准备——`zero_copy=True` 时直接返回 `ctx['hidden_buf_local']` 视图，否则 `torch.empty_like` 新分配；权重缓冲同理（`router_weights_zero_copy` 独立控制）。
- [moonep/api.py:L631-L653](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L631-L653)：发射编排本体——先 `launch_inter_rank_sync`（可选）、再 `launch_planning`（仅 fresh）、`launch_dispatch`、`launch_dispatch_epilogue`。注意 [L638-L642](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L638-L642) 的注释：fresh 规划「刚发布完 dst/src_info」所以 dispatch 能安全地物化去重结构；dispatch 的零填充 warp 同时清掉本 rank 分段的 padding 行。
- [moonep/api.py:L158-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L158-L182)：`_launch_full_weight_prefetches`——对 gate/up/down 各调一次 `launch_prefetch(full_weight[:E], full_weight[E:], experts_to_copy, ...)`：源是前 E 行（对称映射的全组专家），目的是后 B 行预取槽。
- [moonep/api.py:L716](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L716)：注意预取只取 `experts_to_copy[rank]`——**本 rank 自己的那一行**（每个 rank 搬自己要用的远程专家）。
- [moonep/api.py:L1022-L1036](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1022-L1036)：combine 的输出由 MoonEP 分配（`hidden_sh [S,H]` 与可选的 `route_weights_sk [S,K]`），返回的永远是新张量（对比 dispatch 的可选视图）。
- [moonep/api.py:L663-L696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L663-L696)：`_run_combine_on_current_stream`——非零拷贝时先把输入拷入 NVL shard，再 `launch_combine_prologue`（重复组 fp32 累加回主行）、`launch_combine`（K 求和 + 可选权重收集）。
- [README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)：**B 的取值规则**——训练必须 `B = E/R`（规划器保证每 rank 至多从一个远程 home group 复制 ≤ E/R 个专家，于是组 GEMM 触及的所有权重行都是本地的）；推理才允许 `B < E/R`。

#### 4.2.4 代码实践

**实践目标**：用 `NvS = S·K + (token_padding−1)·2·E/R` 公式（[moonep/api.py:L277-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288) 的注释给出了推导）做纯 CPU 计算，为第 5 节的 train_step.py 准备形状断言。

**操作步骤**（示例代码，无 GPU 即可跑）：

```python
# nvs_calc.py（示例代码）
def nvs(S, K, E, R, tp=128):
    epn = E // R
    return S * K + (tp - 1) * 2 * epn

for cfg in [(4096, 8, 256, 8), (256, 4, 32, 8)]:   # README 配置 / 本讲实践配置
    print(cfg, "->", nvs(*cfg))
```

**需要观察的现象 / 预期结果**：输出 `40896` 与 `2040`。padding 余量 `(tp−1)·2·epn` 是「每个非空段从至少 1 个真实 token 补齐到 tp 的倍数」的上界（本地 E/R 段 + 远程 E/R 段），这也是 u3-l3 讲过的容量公式。若有多卡环境，可再对照 `dispatch` 返回的 `h_nvsh.shape[0]`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：训练时 B 为什么必须等于 E/R？

**答案**：z 矩阵「每列至多一个非零元」的不变量（u3-l2）保证每个 rank 至多从一个远程 home group 接收 token，即至多复制 E/R 个远程专家。`B = E/R` 保证组 GEMM 按 `cu_seqlens` 触及的所有权重行（本名行 + 预取槽行）都在本地显存里；`B` 更小就会有专家段溢出到远程直读，而反向要写梯度，远程写代价高且语义混乱（见 README.md:L58 的训练规则）。

**练习 2**：`cu_seqlens` 的长度是多少？空段长什么样？

**答案**：`[E+B]`——E 个本名行加 B 个预取槽行各一项，值是**padded** 段的结束偏移。没分到 token 的段相邻值相等（空段）；被复制的专家本名段为空、token 挂到预取槽段（u3-l3 的 Phase C 布局规则）。

**练习 3**：如果框架在 dispatch 返回后立刻 `cu_seqlens.cpu()` 读回主机分段，破坏了 MoonEP 的哪个卖点？

**答案**：静态形状 / 零宿主同步——`cu_seqlens` 的设计就是全程驻留 GPU、由分组 GEMM 内核直接消费（README.md:L45、u2-l1）。读回主机会重新引入逐层 MoE 宿主同步。本讲第 5 节的排练脚本为了教学简洁会 `.tolist()`，真实集成不要学这一步。

### 4.3 plan 复用路径：combine bwd 免规划、免预取、免重建

#### 4.3.1 概念说明

combine bwd 的调用是 `buffer.dispatch(grad_output_sh, plan=plan)`——同一入口、两种模式。区别只在 `plan` 是否为 None。复用路径带来三个「免」：

1. **免规划**：散射布局由 fwd 的 topk 路由唯一决定，plan 里的 `dst` 表、分段、去重结构对梯度散射同样成立（对偶性），重跑规划器是纯浪费。
2. **免预取**：反向组 GEMM 用的是**同一 microbatch 的同一批权重**。预取槽的物理内容直到**下一次** `prefetch_weight` 才会被覆盖，所以反向直接复用 fwd 已搬进来的本地行。combine bwd 本身只是数据搬运，不读权重。
3. **免重建去重结构**：去重三件套（`dup_groups/dup_loffs/dup_counts`）在 fwd 的 dispatch builder 里已经物化进 plan（u4-l3）；规划器发布的 `src_info` 是临时量，只在紧随 fresh planning 时有效，复用路径上早已过期，不能也不必重建。

但要注意**复用路径不返回 `cu_seqlens`**（返回 None）：分段表是规划输出之一，跳过规划就没有新表——**调用方必须保存 fwd 返回的 `cu_seqlens`** 来驱动反向组 GEMM。这是集成时最容易踩的坑。

#### 4.3.2 核心流程

`dispatch` 入口的分支逻辑（伪代码）：

```text
if plan is None:                        # fresh 路径（dispatch fwd）
    校验 topk / tpe
    plan, cu_seqlens = allocate_planning_outputs(ctx)
    planning_args = (topk_flat, tpe, cu_seqlens)
else:                                   # 复用路径（combine bwd）
    cu_seqlens = None                   # ← 规划输出被整体跳过
    planning_args = None

发射:
    launch_planning(...)                # 仅 planning_args 非 None 时
    launch_dispatch(..., build_dedup_map = (planning_args is not None))
    launch_dispatch_epilogue(...)
```

`build_dedup_map` 这个布尔值就是「免重建」的开关：fresh 时 builder warps 从 `src_info` 物化去重结构；复用时 dispatch 直接读 plan 里保存的三件套。

#### 4.3.3 源码精读

- [moonep/api.py:L792-L795](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L792-L795)：复用分支全部三行——`cu_seqlens = None`、`planning_args = None`、校验传入对象确实是 `MoonEPCommPlan`。
- [moonep/api.py:L744-L748](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L744-L748)：docstring 明说这是 combine bwd 路径：「re-dispatching `grad_output_sh` with the fwd plan scatters the output grad back to VM group order」，且 `topk_experts_sk` / `tokens_per_expert` 被忽略。
- [moonep/api.py:L777-L780](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L777-L780)：返回值约定——`cu_seqlens`: **None on the plan-reuse path**；plan 要「save it for prefetch/combine and both backward passes」。
- [moonep/api.py:L638-L650](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L638-L650)：`build_dedup_map=planning_args is not None` 的传参处。注释解释了两条路径的约束：fresh 时 dst/src_info 刚发布、可以安全物化去重结构；复用路径必须保持已保存结构不动（zero warp 的段清零则在两条路径都运行）。
- [moonep/api.py:L891-L895](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L891-L895)：`prefetch_weight` 独立成入口的理由写在这里——「Kept separate from `dispatch` so the plan-reuse path (combine bwd) can skip re-prefetching」。若预取揉在 dispatch 里，复用路径就无法跳过它。
- [tests/test_e2e.py:L272-L286](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L272-L286)：复用路径的官方断言集——`cu_reuse is None`（L282）、`plan_reuse is plan_snapshot`（L283，**原样回传同一对象**）、去重结构未被重建或变异（L284-L285）、散射结果与 fresh 逐位相等（L280）。
- [tests/test_e2e.py:L288-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L288-L303)：**免预取是被测试钉住的行为**——先用哨兵值 7.0 灌满预取槽（L291），只跑复用 dispatch 不跑 prefetch，断言哨兵原封不动（L303）。这正是「backward redispatch」的真实形态。

#### 4.3.4 代码实践

**实践目标**：确认 `build_dedup_map` 在整个代码库里只有「定义处 + 一处传参」，从而理解它是 fresh/复用两路分叉的唯一开关。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "build_dedup_map" moonep/`（或用编辑器全局搜索）。
2. 阅读命中处的上下文，填写下面的表格。

**需要观察的现象 / 预期结果**（待本地验证，仅两处命中）：

| 位置 | 角色 |
| --- | --- |
| `moonep/dispatch.py`（`launch_dispatch` 的参数与内核判断） | 开关的消费端：True 时编译保留 builder warps |
| [moonep/api.py:L648](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L648) | 开关的生产端：`build_dedup_map=planning_args is not None` |

配合阅读 [tests/test_e2e.py:L274-L285](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L274-L285) 的五条断言，给每条写一句「它钉住了什么语义」（如 `plan_reuse is plan_snapshot` 钉住「回传同一对象、不 clone 不重建」）。

#### 4.3.5 小练习与答案

**练习 1**：复用路径返回的 `cu_seqlens` 是什么？集成框架应该用哪一份驱动反向组 GEMM？

**答案**：是 `None`。框架必须保存 fwd `dispatch` 返回的那份 `cu_seqlens`——分段布局在正反向是同一张表（对偶性），反向不能也不需要重新生成。

**练习 2**：为什么复用路径必须 `build_dedup_map=False`，让它直接读 plan 里的三件套？

**答案**：builder 的唯一输入 `src_info` 是规划器在 fresh planning 时发布到对称内存 SCRATCH 区的临时量（u3-l4、u4-l3），复用时早已过期。若强行重建，要么读到垃圾、要么覆盖 plan 里已保存的正确结构。test_e2e.py:L284-L285 正是钉住「不重建、不变异」。

**练习 3**：既然反向「免预取」，那预取槽的内容什么时候会失效？

**答案**：下一次 `prefetch_weight` 被调用时（通常 是下一个 microbatch 的 fwd）。所以在同一个 microbatch 的反向里读到的权重与 fwd 完全一致；跨 microbatch 则由新 plan 重新预取覆盖。

### 4.4 dispatch bwd：梯度 combine 与 reduce_grad 的归约时机

#### 4.4.1 概念说明

dispatch bwd 拆成两条并行支路：

- **隐式梯度通路**：`buffer.combine(plan=plan, hidden_nvsh=grad_hidden_nvsh)` 把每个 token 的 K 份梯度拷贝 fp32 求和回 token-major 的 `grad_hidden_sh [S,H]`——这就是 dispatch fwd 的转置。它没有专门的「dispatch_bwd」入口，因为 combine 内核本身就是那个转置（负 dst 条目跳过载荷、重复组由 prologue 预累加，见 u4-l5/u4-l6）。
- **显式权重梯度归约**：`buffer.reduce_grad(plan, full_*_grad, *_reduce_buffer)`。标准 EP 里每个专家参数只住在一个 rank 上、梯度就地累加，本不需要通信；**动态冗余专家打破了这个性质**——被复制的专家在多个 rank 的预取槽里各算出一份梯度，必须归约回 home rank 的参数梯度行，否则 owner 侧梯度不完整。

`reduce_grad` 与**框架自身的梯度归约**（DDP all-reduce、Megatron 的梯度归并）是两个正交的层，边界有三条：

1. **对象不同**：框架归约作用于「参数梯度的 DP 副本」（同一参数在数据并行各份的梯度）；`reduce_grad` 作用于「冗余专家预取槽的梯度」（同一专家在 EP 各 rank 的副本梯度）。
2. **物理隔离**：预取槽梯度 rows `[E, E+B)` 由独立的 `[R, B, H, H']` 归约缓冲背书，**不在参数梯度张量里**——README 明确要求它们「must stay invisible to the framework's own grad reduce」（[README.md:L68](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L68)），否则框架会把临时副本梯度也归约进去，污染参数梯度。
3. **时机约束**：`reduce_grad` 必须在 FFN 反向把 `full_grad[E:]` 写满之后、optimizer step（以及梯度反缩放 / clip）之前调用；它清空自己消费过的槽位，为下一个 microbatch 复用做准备。

#### 4.4.2 核心流程

```text
FFN 反向产出:
    grad_h_nvsh [NvS,H]          # 隐式通路输入
    full_gate/up/down_grad       # [E+B,H,H'] fp32，rows[E,E+B) 物理上是归约缓冲本 rank 切片
Q4:
    grad_hidden_sh = combine(plan, grad_h_nvsh)      # K 求和回 token-major
    reduce_grad(plan, full_*_grad, *_reduce_buffer)  # 每 rank:
        # 1) 远程读所有 rank 归约缓冲里属于自己专家的槽
        # 2) 以本地梯度为种子，按源 rank 升序累加进 owner 行
        # 3) 跨 rank 屏障后，本地清零自己段内被消费的活槽
```

一个容易忽略的对称性：**prefetch 只看自己那一行，reduce 看整张表**。预取时每 rank 只需 `experts_to_copy[rank]`（搬自己要用的）；归约时 owner 需要全表（任何 rank 都可能复制了我的专家）。源码对照见下。

#### 4.4.3 源码精读

- [moonep/api.py:L961-L966](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L961-L966)：combine 的 docstring 开宗明义——「Also serves as dispatch bwd: combining `grad_hidden_nvsh` sums each token's K dispatched grad copies back to its token-major grad」。同一内核、两种身份。
- [moonep/api.py:L1096-L1103](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1096-L1103)：reduce_grad 的语义三步——远程读自己专家的槽、累加进本地梯度行、清零已消费槽位。
- [moonep/api.py:L1117-L1119](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1117-L1119)：为什么不并入 combine——「Kept separate from `combine`; running on the shared comm stream keeps the Buffer's barrier/meta resources serialized」。入口正交，流序由共享通信流保证（u6-l1）。
- [moonep/api.py:L199-L219](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L199-L219)：`_launch_full_grad_reduces`——对三个投影各断言 fp32、连续、首维 E+B，然后 `launch_grad_reduce(full_grad[:E], reduce_buffer, experts_to_copy, ...)`；注意它传的是**整张** `experts_to_copy` 表，还传了 `meta_buf` 与 barrier 偏移（内核里要过跨 rank 屏障）。
- [moonep/api.py:L701-L704](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L701-L704) 对照 [L716](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L716)：`_run_reduce_grad_on_current_stream` 消费全表 vs `_run_prefetch_weight_on_current_stream` 只取 `experts_to_copy[rank]`——上一节提到的对称性。
- [README.md:L61-L69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L61-L69)：梯度缓冲契约——fp32 镜像布局、独立归约缓冲、对框架归约不可见。
- [README.md:L117-L127](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L117-L127)：dispatch bwd 的调用样板：先 combine 再 reduce_grad，六个张量成组传入。
- [tests/test_e2e.py:L375-L388](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L375-L388)：同步版编排——combine 之后再 reduce_grad，`assert_grad_reduced` 校验三件事：本地参数梯度行等于期望累加、非本地行不变、自己段内活跃槽被清零而其余槽不动（[tests/test_e2e.py:L162-L202](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L162-L202)）。
- [tests/test_e2e.py:L390-L412](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L390-L412)：异步版——combine 与 reduce_grad 都 `async_finish=True`，只需 `reduce_ev.wait()` 一次（共享通信流的流序保证两者串行，u6-l1）。

#### 4.4.4 代码实践

**实践目标**：把一个 microbatch 的 8 个步骤打乱后重新排序，检验你对四象限时序与「免」点的掌握。纯纸面练习，无 GPU。

**操作步骤**：下面是乱序列表，排出正确执行顺序，并标注哪些步骤**在反向中被省略**：

```text
A. buffer.combine(plan, grad_h_nvsh)            E. buffer.dispatch(hidden, rw, topk, tpe)
B. 分组 FFN 前向（组 GEMM）                      F. buffer.prefetch_weight(plan, ...)
C. buffer.dispatch(grad_out, plan=plan)          G. buffer.reduce_grad(plan, ...)
D. buffer.combine(plan, expert_out, w_nvs)      H. FFN 反向（读预取槽权重，写梯度）
```

**需要观察的现象 / 预期结果**：正确顺序为 `E → F → B → D → C → H → A → G`（前向 Q1/Q2，反向 Q3/Q4）。反向中省略的步骤：**在线规划**（C 内部跳过，`cu_seqlens` 返回 None）与**权重预取**（没有第二次 `prefetch_weight`——H 直接复用 F 搬进来的槽内容）。若你的答案把 F 排在反向之后，说明还没建立「同一 microbatch 的权重槽跨正反向存活」这个模型。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `full_gate_grad[E:]`（预取槽梯度）交给框架的 DDP 梯度归约处理，会发生什么？

**答案**：会污染参数梯度。预取槽梯度物理上驻留在独立的归约缓冲里（[README.md:L68](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L68)），它是「临时副本梯度」，正确的去向是被 `reduce_grad` 归约回 owner 行后清零。框架归约它等于把副本梯度混进参数梯度的 DP 归约，数值直接错。

**练习 2**：`reduce_grad` 放在 optimizer.step() 之后调用行不行？

**答案**：不行。那一步 optimizer 读到的参数梯度缺少冗余专家贡献的份额（本 microbatch 各 rank 副本算出的梯度还没归约回来）。它必须在 FFN 反向写满 `full_grad[E:]` 之后、optimizer step（含梯度反缩放/clip）之前完成。

**练习 3**：`reduce_grad` 为什么以「本地梯度为种子」累加，而不是把所有贡献求和后再写回？

**答案**：owner rank 自己的参数梯度行已经包含本地贡献（框架的反向在 owner 行上累加），远程贡献来自其他 rank 的预取槽。以本地为种子、按确定顺序（rb 升序，u5-l3）累加远程槽，既避免额外缓冲，也保证了可对拍的确定性顺序。

### 4.5 端到端测试：test_e2e.py 如何钉住四象限语义

#### 4.5.1 概念说明

[tests/test_e2e.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py) 是唯一把四个入口**按训练顺序串起来**的测试，也是官方用法范本（比 README 更可执行）。它的策略是「**sync 为基线，其余模式全部与 sync 对拍**」：任何路径（async、复用、零拷贝）只要产出与 sync 路径逐位（或语义）相等的结果，就证明该路径没有偏离语义。

#### 4.5.2 核心流程

测试的场景矩阵（按代码顺序）：

| 段落 | 场景 | 钉住的语义 |
| --- | --- | --- |
| L231-L243 | sync dispatch + prefetch 基线 | 四元组返回契约；`plan.clone()` 快照防御后续变异 |
| L245-L270 | async dispatch + prefetch | async 与 sync 逐位相等；dedup 三件套按**集合语义**比较 |
| L272-L286 | plan 复用 dispatch | `cu_seqlens=None`、plan 原样回传、去重结构不动、散射结果相等 |
| L288-L303 | 复用且**不** prefetch | 哨兵 7.0 不被改写——「免预取」是行为契约 |
| L320-L341 | combine（带/不带权重收集） | 权重收集不影响 hidden 输出；`gathered == 原始 rw` |
| L343-L373 | zero_copy 往返 | 视图路径与拷贝路径逐位相等；别名断言拒绝「值同址异」的输入 |
| L375-L388 | combine + sync reduce_grad | 梯度归约的三组校验（本地行/非本地行/清槽） |
| L390-L412 | async combine + reduce_grad | 异步路径同样满足归约契约；一次 wait 覆盖两个算子 |

#### 4.5.3 源码精读

- [tests/test_e2e.py:L216-L229](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L216-L229)：测试配置 `S=256, H=1024, K=4, E=R*4, B=2, Hp=128`（注意 B=2 是为测试提速；**训练集成应取 B=E/R**，见 4.2.3）。四组 prefetch 参数副本对应四个场景，互不污染。
- [tests/test_e2e.py:L238-L243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L238-L243)：`plan_snapshot = plan_sync.clone()`——后续任何 dispatch 都可能原地改写 plan 张量，先深拷贝快照再做对拍；`assert_prefetched` 逐槽校验「预取槽 b == 源专家 experts_to_copy[b]」。
- [tests/test_e2e.py:L245-L252](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L245-L252)：async 变体用 fresh inputs——注释点明「avoid NVL aliasing with sync run」：上一轮 dispatch 的输出还驻留在 NVL 缓冲里，直接复用输入会对不上基线（这正是 u6-l2 讲的视图生命周期问题的测试侧体现）。
- [tests/test_e2e.py:L264-L270](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L264-L270)：对拍口径——`dst/experts_to_copy/zero_fill_ranges/remote_stats` 逐位相等，**去重三件套只做语义比较**（builder 用 atomicAdd，组顺序不稳定，u4-l3/u6-l4 的主题）。
- [tests/test_e2e.py:L320-L341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L320-L341)：combine 之前先**重新 dispatch** 恢复 NVL 缓冲到已知状态——又一个「combine 的输入是 NVL shard 状态」的证据；`gathered_weights` 与原始 `weights` 逐位相等证明权重收集环路无损。
- [tests/test_e2e.py:L366-L373](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L366-L373)：`assert_raises_assertion("alias", ...)`——零拷贝 combine 拒绝非别名输入（data_ptr 精确匹配，u6-l2）。
- [tests/test_e2e.py:L414-L418](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L414-L418)：收尾顺序——`buffer.destroy()` **先于** `dist.destroy_process_group()`，与 [README.md:L175-L178](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L175-L178) 的要求一致。

#### 4.5.4 代码实践

**实践目标**：通过统计 `test_e2e` 中 `buffer.dispatch` 的调用次数并分类，确认你能在真实代码里分辨 fresh / 复用 / 零拷贝三条路径。

**操作步骤**：

1. 在 [tests/test_e2e.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py) 中搜索 `buffer.dispatch(`，逐个记录行号。
2. 按三分类标注：fresh（传 topk/tpe）、plan 复用（传 plan=）、fresh + zero_copy。

**需要观察的现象 / 预期结果**：共 **11 次** dispatch 调用——fresh 7 次（行 232、250、323、331、346、376、391，其中 346 带 `zero_copy=True`），plan 复用 4 次（行 275、293、305、311）。7 次 fresh 里 5 次是「为 combine 重新铺 NVL 缓冲」的辅助调用，说明 combine 的输入语义就锚定在 NVL shard 上。有多卡环境可运行 `torchrun --nproc_per_node=8 -m pytest tests/test_e2e.py` 亲眼看 PASS 输出（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 async 对拍段（L246-L248）要断言 `torch.equal(hidden, hidden2)`——两份「相同」的输入不是废话吗？

**答案**：不是。两个 rank 用 `seed + rank` 的生成器，`make_inputs` 两次调用产出相同张量；显式断言相等是在钉住「async 变体的输入与 sync 基线完全一致」，否则逐位对拍（L264-L265）没有意义。同时 fresh 张量避免了与 sync 运行共享 NVL 缓冲导致的别名。

**练习 2**：`assert_prefetched`（L89-L100）跳过 `expert < 0` 的槽，为什么？

**答案**：`experts_to_copy` 的空槽写 −1 哨兵（u3-l3 的 top-B 填充规则）——远程专家数不足 B 时允许空槽，此时预取内核不写该槽，槽内容保持原值，校验自然要跳过。

**练习 3**：`assert_grad_reduced` 为什么把「非本地前缀/后缀梯度行不变」「非本 rank 归约槽不变」也列为断言？

**答案**：梯度归约是**定向**操作：只应写 owner 的本地行、只应清自己消费过的槽。任何越界写入（碰了别人的行、清了别人的槽）都是竞态或寻址错误，这类「不该动的没动」断言正是分布式内核测试的标准配置（u6-l4 会展开这套方法论）。

## 5. 综合实践

**实践目标**：编写 `train_step.py`，把四象限完整走一遍——构造 Buffer、权重与梯度缓冲，按「dispatch fwd → prefetch → 组 FFN → combine fwd → combine bwd（plan 复用）→ FFN 反向 → dispatch bwd → reduce_grad」的顺序调用 API，并在 combine bwd 后校验 plan 复用生效（`cu_seqlens` 为 None）且各输出形状正确。

**操作步骤**：把下面的脚本保存为 `train_step.py`（示例代码，非项目自带文件），放在仓库根目录：

```python
# train_step.py —— MoonEP 训练步四象限演练（示例代码）
# 启动：torchrun --nproc_per_node=8 train_step.py   （需 8 卡 + NVLink）
import torch
import torch.distributed as dist
import torch.nn.functional as F

from moonep import Buffer

TOKEN_PADDING = 128  # Buffer 默认值，此处显式用于 NvS 公式


def expert_ffn_fwd(h_nvsh, cu_seqlens, gw, uw, dw):
    """cu_seqlens 驱动的分组 FFN（排练用；真实框架用分组 GEMM 内核，
    且 cu_seqlens 全程驻留 GPU，不会像这里 .tolist() 回主机）。"""
    out = torch.empty_like(h_nvsh)
    prev = 0
    for e, end in enumerate(cu_seqlens.tolist()):
        if end > prev:
            seg = h_nvsh[prev:end]
            out[prev:end] = (F.silu(seg @ gw[e]) * (seg @ uw[e])) @ dw[e].t()
        prev = end
    return out


def main():
    dist.init_process_group(backend="nccl")
    rank, R = dist.get_rank(), dist.get_world_size()
    torch.cuda.set_device(rank % torch.cuda.device_count())
    dev = "cuda"

    # ---- 形状与缓冲：注意训练铁律 B = E/R，Hp 取 128 的倍数（预取约束）----
    S, H, K, E = 256, 1024, 4, R * 4
    epn = E // R
    B = epn
    Hp = 128
    NvS = S * K + (TOKEN_PADDING - 1) * 2 * epn   # api.py:L287-L288 的容量公式

    buffer = Buffer(S, H, K, E, R, B=B, num_sms=32)

    g = torch.Generator(device=dev).manual_seed(1234 + rank)
    hidden = torch.randn(S, H, dtype=torch.bfloat16, device=dev, generator=g)
    rw_sk = torch.rand(S, K, dtype=torch.float32, device=dev, generator=g)
    topk = torch.randint(0, E, (S, K), dtype=torch.int32, device=dev, generator=g)
    tpe = torch.bincount(topk.flatten(), minlength=E).to(torch.int32)

    gw = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device=dev, generator=g)
    uw = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device=dev, generator=g)
    dw = torch.randn(E + B, H, Hp, dtype=torch.bfloat16, device=dev, generator=g)

    # ============ 象限 1：dispatch fwd + prefetch_weight ============
    h_nvsh, w_nvs, cu_seqlens, plan = buffer.dispatch(hidden, rw_sk, topk, tpe)
    assert h_nvsh.shape == (NvS, H) and h_nvsh.dtype == torch.bfloat16
    assert w_nvs.shape == (NvS,) and w_nvs.dtype == torch.float32
    assert cu_seqlens.shape == (E + B,) and cu_seqlens.dtype == torch.int32
    buffer.prefetch_weight(plan=plan, full_gate_weight=gw,
                           full_up_weight=uw, full_down_weight=dw)

    # ---- 分组 FFN 前向：按 cu_seqlens 分段读 [E+B,H,H'] 的行 ----
    expert_out = expert_ffn_fwd(h_nvsh, cu_seqlens, gw, uw, dw)

    # ============ 象限 2：combine fwd ============
    out_sh, gathered_rw, _ = buffer.combine(
        plan=plan, hidden_nvsh=expert_out, route_weights_nvs=w_nvs)
    assert out_sh.shape == (S, H) and out_sh.dtype == torch.bfloat16
    assert gathered_rw.shape == (S, K) and gathered_rw.dtype == torch.float32

    # ============ 象限 3：combine bwd（plan 复用） ============
    grad_out = torch.randn(S, H, dtype=torch.bfloat16, device=dev, generator=g)
    grad_eo_nvsh, w_none, cu_reuse, plan_echo = buffer.dispatch(grad_out, plan=plan)
    assert cu_reuse is None, "plan 复用路径必须跳过规划（cu_seqlens 为 None）"
    assert plan_echo is plan, "plan 复用路径应原样回传传入的 plan 对象"
    assert w_none is None, "未传 route_weights_sk 时不应分配权重缓冲"
    assert grad_eo_nvsh.shape == (NvS, H)

    # ---- FFN 反向（排练）：真实框架由 autograd 计算以下梯度 ----
    grad_h_nvsh = torch.randn_like(h_nvsh)

    # ============ 象限 4：dispatch bwd ============
    grad_hidden_sh, _, _ = buffer.combine(plan=plan, hidden_nvsh=grad_h_nvsh)
    assert grad_hidden_sh.shape == (S, H)

    # 权重梯度：全零初始化便于观察 reduce_grad 的增量（真实框架为 autograd 产出）
    kw = dict(dtype=torch.float32, device=dev)
    full_grads = [torch.zeros(E + B, H, Hp, **kw) for _ in range(3)]
    reduce_bufs = [torch.randn(R, B, H, Hp, generator=g, **kw) for _ in range(3)]
    buffer.reduce_grad(plan=plan,
                       full_gate_grad=full_grads[0], full_up_grad=full_grads[1],
                       full_down_grad=full_grads[2],
                       gate_reduce_buffer=reduce_bufs[0],
                       up_reduce_buffer=reduce_bufs[1],
                       down_reduce_buffer=reduce_bufs[2])

    # ---- 观察 1：被某 rank 复制过的本地专家行收到了归约增量 ----
    lo, hi = rank * epn, (rank + 1) * epn
    e2c = plan.experts_to_copy
    hot = set(int(e) for e in e2c[e2c >= 0].tolist()) & set(range(lo, hi))
    for i, fg in enumerate(full_grads):
        for e in hot:
            assert fg[e].abs().sum() > 0, f"投影 {i}：本地专家 {e} 未收到归约增量"

    # ---- 观察 2：自己 rank 行的活跃归约槽被清零（为下个 microbatch 复用）----
    active = e2c[rank] >= 0
    for i, rb in enumerate(reduce_bufs):
        if bool(active.any()):
            assert bool((rb[rank][active] == 0).all()), f"投影 {i}：已消费槽未清零"

    torch.cuda.synchronize()
    if rank == 0:
        print(f"[train_step] PASS  NvS={NvS}  hot_local_experts={sorted(hot)}")
    buffer.destroy()                       # 必须先于销毁进程组
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

**运行方式**：

```bash
torchrun --nproc_per_node=8 train_step.py
```

**需要观察的现象**：

1. 四象限全部断言通过，尤其 combine bwd 后 `cu_reuse is None`、`plan_echo is plan`——plan 复用路径生效的直接证据。
2. `NvS` 打印值与 `dispatch` 返回的 `h_nvsh.shape[0]` 一致（形状断言已隐式核对容量公式）。
3. 观察 1：`hot_local_experts` 非空（随机路由下几乎必有本地专家被别 rank 复制），这些行的梯度从 0 变为非 0——`reduce_grad` 把远程副本梯度归约回来了。
4. 观察 2：自己 rank 行的活跃槽（`experts_to_copy[rank] >= 0`）清零，其余槽保持 randn 原值——「只清自己消费过的槽」。

**预期结果**：rank 0 打印 `[train_step] PASS NvS=2040 hot_local_experts=[...]`。若在无 GPU 环境则无法运行，以上现象均为**待本地验证**。两点诚实的简化说明：其一，FFN 的前向/反向用随机张量排练（本实践聚焦通信编排与形状契约，真实梯度数值链路交给 autograd）；其二，`expert_ffn_fwd` 里 `cu_seqlens.tolist()` 仅为教学直观，真实集成中该表由分组 GEMM 内核在 GPU 上消费、从不同步回主机。

## 6. 本讲小结

- **四象限**：dispatch fwd（散射 + 在线规划）、combine fwd（加权 K 求和）、combine bwd（`dispatch(grad, plan=plan)` 重新散射）、dispatch bwd（`combine` 求和回 token-major + `reduce_grad`）——两个入口互为对偶：combine 的反向是 dispatch，dispatch 的反向是 combine。
- **plan 复用的三个「免」**：免规划（散射布局由 fwd 路由唯一决定）、免预取（权重槽内容跨正反向存活，直到下一次 `prefetch_weight`）、免重建去重（`build_dedup_map=planning_args is not None`，复用路径直接读 plan 里的三件套）；代价是调用方必须自己保存 fwd 的 `cu_seqlens`（复用路径返回 None）。
- **框架契约**：每投影一个 `[E+B, H, H']` 连续权重张量 + 一张 `cu_seqlens[E+B]` 分段表驱动组 GEMM；训练必须 `B = E/R`。
- **reduce_grad 的边界**：它归约的是「冗余专家预取槽梯度」（EP 内跨 rank、物理在独立归约缓冲、对框架自身梯度归约必须不可见），时机在 FFN 反向之后、optimizer step 之前。
- **test_e2e.py** 是官方用法范本：sync 为基线，async / 复用 / 免预取 / 零拷贝 / 归约全部与基线对拍，且「不该动的没动」类断言钉住定向性。

## 7. 下一步学习建议

- **u6-l4（测试方法论）**：本讲反复出现的「语义等价比较」「不该动的没动」断言正是下一讲的主题——为什么 atomicAdd 决定的去重组顺序不稳定、KernelCase 如何参数化、参考实现如何对拍。
- **u6-l5（基准测试）**：把本讲的 train_step 放到计时框架下，读 `benchmarks/bench_comm.py` 与 `bench_vs_deepep.py`，理解四象限各算子的带宽口径与 maxvio 扫描方法。
- **重读 [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) 全文**：此时你已具备理解每个 launch_* 调用点的全部前置知识，通读一遍会有「拼图完成」的效果。
- 若目标是接入训练框架：对照本讲 4.4 的三条边界，检查你的框架里梯度归约与 optimizer 的挂载点，再评估 `zero_copy` 能否安全开启（u6-l2 的风险模型）。





