# dispatch epilogue：本地重复展开

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 dispatch 内核结束后 NVL shard 上为什么留有「空洞」，以及 `DispatchEpilogueKernel` 如何用主行把空洞补齐。
2. 读懂 epilogue 的双 warp 流水线：warp 0 用 TMA 把主行搬进 smem（G2S），warp 1 把同一份 smem 数据扇出到所有重复槽（S2G）。
3. 理解启动几何的设计取舍：grid 恒为 `num_sms_dedup`、组批次 round-robin 分派、`_B_CANDIDATES` 为什么从大到小尝试。
4. 理解 `dup_counts[0]` 为什么必须在设备端读取，以及这一选择如何免去宿主同步。
5. 理解 PDL（Programmatic Dependent Launch）如何让 epilogue 与 dispatch 流水衔接。
6. 独立写出 epilogue 的 PyTorch 参考实现，并用随机重复组验证它与「逐组 copy」语义等价。

## 2. 前置知识

本讲是 u4 单元（通信内核）的第四篇，建立在前三讲之上。先用通俗语言回顾几个必须的概念。

### 2.1 负数 dst 与「主行 / 重复槽」

u3-l5 讲过：同一个 token 的多个 top-k 条目落到**同一个目的 rank** 时，只有最小的那个 k 条目会真正经 NVLink 拷贝 payload（这一行叫**主行**，primary row），其余条目的 dst 被规范化为 `-raw_dst - 1` 的负数编码——只散射路由权重，不拷 payload。于是在接收 rank 的 NVL shard（`hidden_buf_local`，形状 `[NvS, H]`）上，这些**重复槽**（duplicate slots）在 dispatch 结束时是没有被写入的。

### 2.2 去重三件套

u4-l3 讲过 dispatch 内核的 builder warps 如何构建三个 plan 持有的张量：

- `dup_groups`，形状 `[NvS, 3]`：每行是一个组头三元组 `(primary_loff, dup_start, dup_n)`——主行在 shard 内的行号、该组的重复槽在 `dup_loffs` 中的起始下标、重复槽个数。
- `dup_loffs`，形状 `[NvS,]`：所有重复槽行号的紧凑扁平列表。
- `dup_counts`，形状 `[2,]`：`[有效组数, dup_loffs 有效长度]`。

只有紧凑前缀有效；组间顺序由 builder 的 atomicAdd 到达序决定，**不保证稳定**。

### 2.3 两条 TMA 指令与两种完成机制

u4-l1 讲过 `cp.async.bulk` 家族（对应 CUDA 的 TMA，Tensor Memory Accelerator）：

- **G2S**（global → shared）：完成后自动在 mbarrier 上累加「事务字节数」，消费者等 mbarrier 相位翻转即知数据到齐。
- **S2G**（shared → global）：没有 mbarrier 完成语义，改用 **bulk_group** 编组——`cp_async_bulk_commit_group` 把自上次 commit 以来的所有 S2G 编为一组，`cp_async_bulk_wait_group <N>` 等到在途组数 ≤ N。

两者都是**单线程指令**，只能由选举出的 lane（通常是 lane 0）发射。另外注意：bulk 拷贝只有 G2S / S2G 两个方向，**没有 global → global**，所以「把 gmem 的一行复制到 gmem 的多个位置」必须以 smem 为中转。

### 2.4 PDL：程序化依赖启动

同一条 stream 上先后两个内核，默认是「前驱完全结束后，后继才开始启动」——后继的启动开销（取指、分配资源、初始化）串行暴露。PDL 允许后继**提前启动**做不依赖前驱数据的准备工作，直到前驱显式调用 `griddepcontrol.launch_dependents` 放行、且后继在依赖点调用 `griddepcontrol.wait` 之后，才真正读取前驱的输出。u4-l1 已介绍这对原语，本讲关注它们在 dispatch → epilogue 之间的实际配对。

### 2.5 persistent CTA 与 round-robin 划分

普通内核「一个 CTA 处理一份数据，grid 大小由数据量决定」；persistent 风格则是「grid 大小固定为 SM 数（或其子集），每个 CTA 常驻并循环认领数据」。epilogue 采用后者：grid 恒为 `num_sms_dedup`，组批次按 `i % num_sms` 轮转分派给 CTA。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [moonep/dispatch_epilogue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py) | **本讲主角**：`DispatchEpilogueKernel` 设备内核 + `launch_dispatch_epilogue` 宿主封装 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 调用点：dispatch → epilogue 的顺序与 PDL 衔接、`MOONEP_NUM_SMS_DEDUP` 环境变量、`zero_copy` 边界拷贝 |
| [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) | PDL 助手与 `cp_async_bulk_g2s/s2g` 的 inline-PTX 封装（u4-l1 已精读） |
| [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py) | 上游：builder warps 写入三件套；退出屏障 + `pdl_trigger_dependents` 放行 epilogue |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 三件套的数据契约（字段注释与形状断言） |
| [tests/test_dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py) | 端到端验证：`duplicate_topk` 用例、epilogue 调用序列、按 dst 逐行验证 |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | 三件套的 PyTorch 参考构造（本讲实践任务的依据） |

## 4. 核心概念与源码讲解

本讲把 `DispatchEpilogueKernel` 拆成四个最小模块讲解：定位与衔接、数据契约、启动几何、设备内核与宿主封装。

### 4.1 epilogue 的定位：dispatch 之后的本地补行

#### 4.1.1 概念说明

回顾负数 dst 的动机：省 NVLink 带宽。同一 token 的 3 个 top-k 落到同一目的 rank 时，payload 只传 1 份而不是 3 份。但这个节省把问题留给了接收端——**分组 GEMM 按 `cu_seqlens` 整段读取 shard**，段内每一行都必须是有效数据，包括那些没被传输的重复槽。

`DispatchEpilogueKernel` 就是补洞的内核：对每个重复组，把主行**原地处**复制到同 shard 上的所有重复槽。它有三个关键性质：

1. **纯本地**：不发生任何跨 rank 通信，读写都在本 rank 的 NVL shard 上。
2. **不碰用户张量**：它只修改 `ctx['hidden_buf_local']`；`zero_copy=False` 时随后的整块 `copy_`、`zero_copy=True` 时直接返回视图，都发生在它之后。
3. **不需要自己的跨 rank 屏障**：它读的数据（远端 rank 经 NVLink 写入本 shard 的主行）由 dispatch 内核**出口处**的 `cross_rank_barrier` 保证已发布；它的输出只被本地 GEMM 消费、或由后续 combine 的入口屏障发布——两头都有别人负责，自己就无需再同步。

#### 4.1.2 核心流程

一次 `dispatch` 的完整时序（对应 `Buffer._run_dispatch_on_current_stream`）：

```text
launch_inter_rank_sync（可选预同步）
        │
launch_planning          （fresh 路径；产出 dst / src_info / cu_seqlens ...）
        │
launch_dispatch          （负 dst 条目只散射权重；builder warps 产出三件套；
        │                  退出时 cross_rank_barrier 发布 NVL 写，
        │                  随后 pdl_trigger_dependents 放行后继）
        ▼
launch_dispatch_epilogue （本讲：主行 → 全部重复槽，原地扇出）
        │
zero_copy 边界           （False: hidden_nvsh.copy_(shard)；True: 直接返回视图）
```

epilogue 与 dispatch 在**同一条 stream** 上先后启动，这本身就是正确性的保证；PDL 只是把「后继的启动延迟」藏到前驱的执行时间里，属于性能优化。

#### 4.1.3 源码精读

模块 docstring 开宗明义地概括了上述定位——dispatch 只送主行、epilogue 原地展开、padding 行已由 zero warp 清零、权重已由 consumer 散射，以及「无自身跨 rank 屏障」的理由：

- [moonep/dispatch_epilogue.py:L1-L18](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L1-L18) —— 说明本内核是 dispatch 之后的**原地**（in-place）重复展开：主行（dispatch 经 NVLink 送出的唯一拷贝）经 smem 暂存一次，再写到同 shard 的每个重复槽；不涉及用户张量；必须在同 stream 上跟在 dispatch 之后（dispatch 的退出 `cross_rank_barrier` 发布了它要读的远端 NVL 写）；输出本地消费，故无需自己的跨 rank 屏障。

api.py 的调用点是这段时序的直接证据：

- [moonep/api.py:L643-L653](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L643-L653) —— `launch_dispatch(..., build_dedup_map=planning_args is not None, pdl_trigger=self.enable_pdl)` 之后就地调用 `launch_dispatch_epilogue(ctx, plan, pdl_launch=self.enable_pdl)`。注意两个 PDL 开关**必须同值**：dispatch 侧 `pdl_trigger` 负责放行，epilogue 侧 `pdl_launch` 负责等待，缺一即失配。
- [moonep/api.py:L654-L661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L654-L661) —— epilogue 之后的 master 风格边界拷贝：`zero_copy=False` 时整块 `copy_` 进用户张量。此时 shard 已含被补齐的重复行，「shard 持有完整用户可见 `[NvS, H]` 布局」的注释正说明 epilogue 是这一保证的提供者。

dispatch 内核出口处的放行代码：

- [moonep/dispatch.py:L683-L692](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L692) —— dispatch 先过 `cross_rank_barrier`（grid 栅障 + system 级 release/acquire 原子，把全 grid 的 NVL 写发布给对端 rank，也即本地 epilogue 要读的行），再在 `pdl_trigger` 打开时调用 `pdl_trigger_dependents(tidx)` 放行 epilogue。**先发布、后放行**的顺序是正确性的关键。

这对 PDL 原语的实现极薄：

- [moonep/_common.py:L393-L400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L393-L400) —— `pdl_trigger_dependents`：CTA 内 `sync_threads` → system 级 acquire/release fence → 再 `sync_threads` → tid 0 执行 `griddepcontrol.launch_dependents`。fence 夹在两道块同步之间，保证放行时本 CTA 的所有写已对外可见。
- [moonep/_common.py:L403-L406](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L403-L406) —— `pdl_wait_predecessor`：一条 `griddepcontrol.wait`，等待前驱 grid 完成并冲刷其写入。它被放在 epilogue 内核的第一行（见 4.4.3）。

`enable_pdl` 是 `Buffer` 的构造参数，默认 `True`：

- [moonep/api.py:L460](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L460) / [moonep/api.py:L495](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L495) —— `enable_pdl: bool = True` 存为 `self.enable_pdl`，供四处 launch 传递（dispatch/epilogue、combine prologue/combine 两对）。

#### 4.1.4 代码实践（阅读型 + 可选运行）

**实践目标**：在测试代码里亲眼确认「epilogue 之前重复槽无效、之后逐 dst 验证全部通过」这一因果。

**操作步骤**：

1. 打开 [tests/test_dispatch.py:L61-L84](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L61-L84)，阅读 `duplicate_topk` 与 `duplicate_topk_k32` 两个用例。它们使用 `routing="duplicate_topk"`。
2. 查看该路由模式的生成方式：[tests/kernel_test_utils.py:L107-L109](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L107-L109) —— 一个 token 的**全部 K 个 top-k 都指向同一个远程专家**，即 K 份拷贝全部落到同一目的 rank，是重复组的极端压力用例（每组 `dup_n = K-1`）。
3. 阅读 [tests/test_dispatch.py:L167-L206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L167-L206) 的调用序列：`launch_planning` → `launch_dispatch(build_dedup_map=True)` → 三件套语义校验 → **`launch_dispatch_epilogue`** → 边界 `copy_`。
4. 再读 [tests/test_dispatch.py:L209-L243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L209-L243) 的 `_verify_dispatch_by_dst`：对每个 `(src_rank, s, k)` 条目——**包括 `dst_val < 0` 的负数条目**——解码 `raw_dst = -dst_val - 1` 后同样检查 shard 对应行持有该 token 的数据（行首两个 int16 编码了 token 序号与源 rank）。

**需要观察的现象**：负数 dst 条目指向的行（重复槽）也能通过数据校验——这些行没有经过 NVLink 传输，唯一的写者就是 epilogue。这正是 epilogue 生效的直接证据。

**预期结果**：全部用例通过；若把步骤 3 中的 `launch_dispatch_epilogue` 一行注释掉，`duplicate_topk` 类用例应当在校验重复槽时失败（重复槽仍是上轮脏数据）。此项**待本地验证**（需要 ≥2 张 NVLink 互联的 GPU，运行 `torchrun --nproc_per_node=2 -m pytest tests/test_dispatch.py -k duplicate_topk`）。

#### 4.1.5 小练习与答案

**练习 1**：epilogue 为什么不需要自己的 `cross_rank_barrier`？

**答案**：它的输入（远端写入本 shard 的主行）由 dispatch 出口的 `cross_rank_barrier` 保证已发布且同 stream 先行；它的输出只被本地 GEMM 消费或由 combine 的入口屏障发布。两头都有既有的同步点，中间无需再加。

**练习 2**：如果把 `launch_dispatch_epilogue` 挪到 `launch_dispatch` **之前**执行，会发生什么？

**答案**：此时三件套尚未由 builder 写出（读到的是未初始化/旧值），且主行本身还没经 NVLink 到达本 shard——展开的是无效数据，随后 GEMM 读到的重复槽全部错误。同 stream 的顺序约束是 epilogue 正确性的硬前提。

**练习 3**：`zero_copy=True` 与 `zero_copy=False` 下，epilogue 的输出分别流向哪里？

**答案**：epilogue 始终原地写 `hidden_buf_local`。`zero_copy=False` 时随后整块 `copy_` 到用户张量 `hidden_nvsh`；`zero_copy=True` 时没有拷贝，用户直接拿到 shard 的视图——所以视图内容也必须是 epilogue 补齐后的完整布局。

### 4.2 数据契约：dup 三件套与设备端读取 `dup_counts[0]`

#### 4.2.1 概念说明

epilogue 的全部工作由三件套驱动，它自己是**纯消费者**：fresh dispatch 的 builder 写出三件套，plan 复用路径则原样传递上次保存的三件套（复用路径的 shard 早已展开过，重跑 epilogue 是幂等的——重复写同样的数据）。

契约的权威定义在 planning.py：

- [moonep/planning.py:L45-L50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L45-L50) —— 字段注释：`dup_counts = [n_groups, n_dup_loffs]`；`dup_groups` / `dup_loffs` 只有紧凑前缀有效，顺序由 builder 的 atomicAdd 决定、不保证稳定。
- [moonep/planning.py:L66-L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L66-L71) —— 形状断言：`dup_groups` 必须是 `[NvS, 3]` 的连续 int32，`dup_loffs` 是 `[NvS,]`，`dup_counts` 是 `[2,]`。

写入端在 dispatch 的 builder warps（u4-l3 已精读，这里只看契约的落点）：

- [moonep/dispatch.py:L663-L681](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L663-L681) —— 当 `loff == primary_loff` 且 `dup_count > 0` 时，写出组头三元组 `dup_groups[my_grp] = (loff, my_dup, dup_count)`，并把组内重复槽（按 kidx 升序、剔除主槽）逐个写入 `dup_loffs[my_dup + pos]`。

**一个关键的语义问题**：组间顺序不稳定，为什么 epilogue 不受影响？因为**各组写的槽位集合互不相交**（一个重复槽只属于一个组），展开操作彼此独立、与顺序无关；主行则不属于任何组的重复槽集合。这一性质也让测试必须比较「组集合」而非逐元素顺序（见 [tests/kernel_test_utils.py:L239-L281](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L239-L281) 的 `dedup_plan_semantic_errors`：把三件套还原成 `{primary_loff: 排序后的重复槽列表}` 的字典再比较）。

#### 4.2.2 核心流程：为什么 `n_groups` 必须在设备端读

朴素做法是宿主端先读组数再决定怎么启动：

```text
# 假想的宿主端方案（MoonEP 没有这么做）
n_groups = plan.dup_counts[0].item()   # D2H 拷贝 + 阻塞等待 dispatch 内核完成
grid = ceil(n_groups / B)              # 再据此定 grid
```

这条 `.item()` 的代价是：宿主必须等到 dispatch 内核**彻底完成**（三件套是它的 builder warps 写的）、再做一次设备到主机的拷贝。整个 CUDA 流水线在这一刻被斩断——这正是 MoonEP 全库极力避免的「宿主同步」（u2-l1 讲过静态形状把负载波动留在 GPU 上、`cu_seqlens` 全程不回主机，是同一个设计哲学）。

MoonEP 的做法：**launch 几何完全静态**（grid 恒为 `num_sms_dedup`，与组数无关），组数在内核内部作为循环上界读出：

```text
宿主：grid = num_sms_dedup（常量，构造期已知）     ← 无需知道 n_groups
设备：n_groups = dup_counts[0]（一次普通 gmem 加载） ← 循环边界
```

于是从 planning 到 epilogue 的整条链路一次宿主同步都没有。

#### 4.2.3 源码精读

- [moonep/dispatch_epilogue.py:L140-L148](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L140-L148) —— 宿主 jit 入口里四个张量的构造：`dup_groups` 被摊平成 `(NvS*3,)` 一维布局（内存上与 `[NvS,3]` 视图是同一块，见 [moonep/planning.py:L1229-L1233](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1229-L1233) 的分配：`torch.empty(_round4(NvS*3))[:NvS*3].view(NvS, 3)`），内核因此用 `(gi + idx) * 3` 这样的扁平下标访问组头。
- [moonep/dispatch_epilogue.py:L208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L208) —— 全篇最短也最关键的一行：`n_groups = dup_counts_tensor[0]`。这是**设备端**的一次全局内存加载，发生在两个 warp 分支之前，随后作为生产者与消费者各自 `while gi < n_groups` 循环的共同边界。类 docstring 明确标注 "read on device — no host synchronization"（[moonep/dispatch_epilogue.py:L53-L56](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L53-L56)）。

#### 4.2.4 代码实践（CPU 即可运行）

**实践目标**：写一个纯 Python 的三件套「解码器」，直观理解契约，并验证「组顺序无关」性质。

**操作步骤**（示例代码，保存为 `dup_decode.py`，只需 CPU 与 PyTorch）：

```python
# 示例代码：三件套解码器与顺序无关性验证
import torch

def decode(dup_groups, dup_loffs, dup_counts):
    """把三件套还原成 {primary_loff: [重复槽, ...]} 的字典。"""
    n_groups = int(dup_counts[0])
    out = {}
    for g in range(n_groups):
        primary_loff, dup_start, dup_n = dup_groups[g].tolist()
        out[primary_loff] = dup_loffs[dup_start:dup_start + dup_n].tolist()
    return out

torch.manual_seed(0)
nvs, H = 64, 8
shard = torch.randn(nvs, H)

# 手工构造 3 个组：主行 5 持 2 个重复槽，主行 17 持 1 个，主行 40 持 3 个
dup_groups = torch.zeros(nvs, 3, dtype=torch.int32)
dup_loffs  = torch.zeros(nvs,    dtype=torch.int32)
dup_groups[0] = (5,  0, 2); dup_loffs[0:2] = (9, 21)
dup_groups[1] = (17, 2, 1); dup_loffs[2:3] = (30,)
dup_groups[2] = (40, 3, 3); dup_loffs[3:6] = (1, 50, 63)
dup_counts = torch.tensor([3, 6], dtype=torch.int32)

print(decode(dup_groups, dup_loffs, dup_counts))
# 期望：{5: [9, 21], 17: [30], 40: [1, 50, 63]}

# 顺序无关性：打乱组顺序 + 重排 dup_loffs，展开结果应不变
def expand(shard, groups, loffs):
    for primary_loff, dup_start, dup_n in groups.tolist():
        for k in range(dup_n):
            shard[loffs[dup_start + k]] = shard[primary_loff]
    return shard

a = expand(shard.clone(), dup_groups, dup_loffs)

perm = torch.randperm(3)
g2 = torch.zeros_like(dup_groups); l2 = torch.zeros_like(dup_loffs); pos = 0
for new_i, old_i in enumerate(perm.tolist()):
    p, s, n = dup_groups[old_i].tolist()
    g2[new_i] = (p, pos, n); l2[pos:pos+n] = dup_loffs[s:s+n]; pos += n
b = expand(shard.clone(), g2, l2)

assert torch.equal(a, b), "组顺序不应影响展开结果"
print("order-invariance OK")
```

**需要观察的现象**：解码器输出的字典与手造的组一致；打乱组序后两种展开结果逐元素相等。

**预期结果**：打印 `{5: [9, 21], 17: [30], 40: [1, 50, 63]}` 与 `order-invariance OK`（本脚本只依赖 CPU PyTorch，可直接运行验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果宿主端用 `plan.dup_counts[0].item()` 读组数，除了同步开销，还会破坏什么？

**答案**：`.item()` 要求等待 dispatch 内核完成（三件套由其 builder 写出）。这会阻塞宿主线程直到在途内核排空，使后续 launch 无法提前入队——`async_finish=True` 所追求的「宿主立刻返回、通信在 stream 上异步推进」就失效了。

**练习 2**：`dup_counts` 的两个元素分别是什么？epilogue 用了哪一个？

**答案**：`[0]` 是有效组数 `n_groups`，`[1]` 是 `dup_loffs` 的有效长度（所有 `dup_n` 之和）。epilogue 只读 `dup_counts[0]` 作为循环边界；`[1]` 留给宿主校验 / 测试使用。

**练习 3**：builder 保证组内重复槽按 kidx 升序，这个顺序对 epilogue 有意义吗？

**答案**：没有。epilogue 对组内槽位逐个写入相同的主行数据，写序不影响结果；该顺序只影响三件套的可复现性（便于参考实现对拍时比较集合）。

### 4.3 启动几何：round-robin 组批次与 `_B_CANDIDATES` 的取舍

#### 4.3.1 概念说明

epilogue 的数据量（组数）每个训练步都不同，但它的 launch 几何完全静态：`grid = num_sms_dedup`、`block = 64` 线程（2 个 warp）。数据与 CTA 的解耦靠 round-robin 批次划分：

- 组序列 `[0, n_groups)` 被切成大小为 `B` 的**批次**（batch）：批次 \( j \) 覆盖组 \([jB,\; jB+B)\)；
- 批次 \( j \) 固定分派给 CTA \( j \bmod \text{num\_sms} \)；
- CTA \( b \) 从 `gi = b * B` 起步，每轮步进 `num_sms * B`。

用公式表达覆盖性：任意组 \( g \in [0, n_{\text{groups}}) \)，它属于批次 \( \lfloor g/B \rfloor \)，由 CTA \( \lfloor g/B \rfloor \bmod N_{\text{sm}} \) 处理——**每个组恰好被一个 CTA 处理一次**。注意 `num_sms` 同时出现在起步偏移（`bidx * B`）和步进（`num_sms * B`）里，两者必须与 grid 一致，否则会漏批次或重复处理（见练习 3）。

为什么 round-robin 而不是「每个 CTA 认领一大段连续组」？因为组的大小不均（`dup_n` 从 1 到 K−1 都有，`duplicate_topk` 路由下全是 K−1），细粒度轮转能把大小不一的组均匀撒到各个 CTA 上。

`num_sms` 实际取 `ctx['num_sms_dedup']`——epilogue/prologue 这对「去重修补」内核专用的 SM 数，可被环境变量 `MOONEP_NUM_SMS_DEDUP` 覆盖（默认等于全部 SM）：

- [moonep/api.py:L82-L102](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L82-L102) —— `_num_sms_dedup_from_env`：故意做成环境变量而非 `Buffer` 公开参数，让基准作业能扫描取值而不动公共 API 面。取值范围校验 `[1, max_sms]`。
- [moonep/dispatch_epilogue.py:L383](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L383) —— `num_sms = int(ctx['num_sms_dedup'])`，即内核的 `num_sms` 编译期常量来自这里。

#### 4.3.2 核心流程：`B` 与 `stages` 怎么选

每个 pipeline stage 需要的 smem 是一个 \( B \times H \) 的 bf16 块加屏障，总预算：

\[
\text{smem}(H, S, B) \;=\; \big\lceil 2\,B\,H\,S \big\rceil_{128} \;+\; \big\lceil 16\,S \big\rceil_{16} \;+\; 256
\]

三项分别是：\( S \) 个 stage × \( B \) 行 × \( H \) 元素 × 2 字节（128 字节对齐）、每 stage 两个 int64 mbarrier（full + empty）共 \( 16S \) 字节、以及 256 字节的 cutlass 头寸。

候选序列与选择规则：

- `_B_CANDIDATES = (32, 16, 8, 4, 2, 1)`，`_MIN_STAGES = 2`；
- 对每个 `B` 从大到小尝试 stage 数 `(16, 14, 12, 10, 8, 6, 4, 2)`，取第一个满足 smem 预算的；
- 返回第一个能让 `stages >= 2` 的 `B`；全都不行则退化到 `B=1`；连 `B=1, stages=2` 都放不下则直接报错（`H` 超过单块 smem 预算）。

取舍的依据写在源码注释里：**握手摊销占主导，深度超过 2 的流水实测无收益**——所以优先选「仍能双缓冲的最大批次 B」，把每批的 mbarrier 握手成本摊到尽可能多的行上。

举一个具体例子（H100，`shared_memory_per_block_optin = 232448` 字节，预算 = 232448 − 1024 = 231424）：`H = 7168` 时 `B=32`、`B=16` 连 `stages=2` 都放不下（\(2 \times 16 \times 7168 \times 2 = 458752\) 字节超预算），`B=8` 时 \(2 \times 8 \times 7168 \times 2 = 229376\)，加屏障与头寸后 229664 ≤ 231424，故选中 `(B=8, stages=2)`。

#### 4.3.3 源码精读

- [moonep/dispatch_epilogue.py:L63-L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L63-L71) —— 几何常量：`num_threads = 64`（恰好两个 warp），`PRODUCER_WARP = 0`、`CONSUMER_WARP = 1`；以及 `_B_CANDIDATES` 上方的取舍注释（handshake amortization dominates，深度 > 2 无实测收益，选能双缓冲的最大 B）。
- [moonep/dispatch_epilogue.py:L73-L92](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L73-L92) —— 构造函数：调用 `_pick_geometry` 定 `(B, stages)`；`assert self.B <= 32`（批次必须塞进一个 warp，理由见 4.4.3 的 shuffle 用法）；`stages == 0` 时抛出带诊断信息的异常（提示需要的最小 smem）。
- [moonep/dispatch_epilogue.py:L94-L105](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L94-L105) —— `_smem_bytes`：上面公式的直接实现，`_round_up` 辅助函数做对齐。
- [moonep/dispatch_epilogue.py:L107-L112](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L107-L112) —— `_pick_stages`：固定候选 `(16, 14, 12, 10, 8, 6, 4, 2)` 从深到浅找第一个放得下的；找不到返回 0。
- [moonep/dispatch_epilogue.py:L114-L120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L114-L120) —— `_pick_geometry`：对 `_B_CANDIDATES` 从大到小尝试，返回第一个 `stages >= _MIN_STAGES` 的组合；兜底 `(1, _pick_stages(H, budget, 1))`。
- [moonep/dispatch_epilogue.py:L152-L164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L152-L164) —— 内核 launch：`grid=(num_sms,1,1)`、`block=(64,1,1)`、按 `_smem_bytes` 申请 opt-in smem、`cooperative=True`、`use_pdl=self.pdl_launch`。grid 与步进公式共享同一个编译期 `num_sms`。
- [moonep/dispatch_epilogue.py:L339-L347](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L339-L347) —— `_get_compiled` 的 `lru_cache` 键包含 `(H, NvS, num_sms, device_index, pdl_launch)`：`num_sms` 参与步进公式、`pdl_launch` 参与内核体（有无 `pdl_wait_predecessor`），所以换任何一个都要重新编译；smem 预算按设备查询后留 1KB 余量。

#### 4.3.4 代码实践（CPU 即可运行）

**实践目标**：复现 `(B, stages)` 的选择逻辑，理解不同隐藏维下内核如何自适应。

**操作步骤**（示例代码 `geometry_table.py`）：

```python
# 示例代码：_pick_geometry 的纯公式复现（不 import moonep，避免依赖 GPU 环境）
def round_up(n, a): return (n + a - 1) // a * a

def smem_bytes(H, stages, B):
    return round_up(stages * B * H * 2, 128) + round_up(stages * 2 * 8, 16) + 256

def pick_stages(H, budget, B):
    for s in (16, 14, 12, 10, 8, 6, 4, 2):
        if smem_bytes(H, s, B) <= budget:
            return s
    return 0

def pick_geometry(H, budget):
    for b in (32, 16, 8, 4, 2, 1):
        s = pick_stages(H, budget, b)
        if s >= 2:
            return b, s
    return 1, pick_stages(H, budget, 1)

BUDGETS = {"H100(228KB optin)": 232448 - 1024, "A100(164KB optin)": 166912 - 1024}
for name, budget in BUDGETS.items():
    print(name)
    for H in (512, 1024, 2048, 4096, 7168, 8192):
        b, s = pick_geometry(H, budget)
        print(f"  H={H:5d} -> B={b:2d}, stages={s}, smem={smem_bytes(H, s, b)} B")
```

**需要观察的现象**：`H` 越大，被选中的 `B` 越小；`stages` 几乎总是 2（印证「深度 > 2 无收益、优先大 B」的取舍）。

**预期结果**：H100 预算下 `H=7168 -> B=8, stages=2`（与本节手算一致）、`H=2048 -> B=16, stages=2`；A100 预算下 `H=7168 -> B=4, stages=2`。有 GPU 环境时可加一行 `from moonep.dispatch_epilogue import DispatchEpilogueKernel; DispatchEpilogueKernel._pick_geometry(H, budget)` 对照（此步**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `B` 的候选从大到小尝试，而不是直接取 `B=1`、`stages` 尽量深？

**答案**：每个批次要付出一次 mbarrier 握手（等待 empty、arrive+expect_tx、consumer_wait/release）。注释明确说握手摊销主导而深度收益可忽略，因此同样 smem 下「大批次 + 双缓冲」优于「小批次 + 深流水」。

**练习 2**：`assert self.B <= 32` 防的是什么？

**答案**：消费者 warp 用「每 lane 读一个组头 + `shuffle_sync(idx)` 广播」的方式获取批次内第 `idx` 组的信息（见 4.4.3）。一条 warp shuffle 只能在 32 个 lane 之间取值，`B > 32` 时 `shuffle_sync(dup_start, idx)` 对 `idx >= 32` 无从取源。

**练习 3**：如果只把 launch 的 grid 改成 `2 * num_sms`，而内核里的步进公式仍用编译期常量 `num_sms`，会发生什么？反过来把 grid 改成 `num_sms // 2` 呢？

**答案**：grid 加倍时，block \( b \in [N, 2N) \) 的起步 `gi = b*B` 与 block \( b-N \) 相同，于是每个批次被两个 CTA 各处理一遍——重复写同样的数据，结果幂等但浪费一半算力；grid 减半时，起步在 \([N/2 \cdot B, N \cdot B)\) 的批次没有任何 CTA 认领，这些组的主行永不展开，重复槽保持脏数据——**正确性被破坏**。这说明「grid、起步偏移、步进」三者必须锚定同一个 `num_sms`。

### 4.4 设备内核与宿主封装：双 warp 扇出流水线

#### 4.4.1 概念说明

内核要完成的语义是：对每个组，`shard[dup_loffs[dup_start .. dup_start+dup_n)] = shard[primary_loff]`。两个 warp 镜像 dispatch 数据通路（但方向相反、规模小得多）：

- **warp 0（生产者）**：把一批 `B` 个组的主行逐行 G2S 搬进当前 stage 的 smem 块。
- **warp 1（消费者）**：等该 stage 数据到齐后，逐组把 smem 里的主行 S2G 扇出到该组的全部重复槽。

为什么以 smem 为中转而不是直接 gmem 拷 gmem？两层原因：其一，PTX 的 `cp.async.bulk` 只有 G2S / S2G 方向，没有 global→global 变体，中转 smem 是硬约束；其二，中转顺带带来带宽收益——**主行从 gmem 只读一次**，不管它扇出给多少个重复槽（docstring 原话："the primary row is read from gmem exactly once however many copies it fans out to"）。

一条行数据在一个 stage 内的旅程：

```text
gmem 主行 ──cp.async.bulk G2S──▶ smem stage[idx][row] ──cp.async.bulk S2G──▶ gmem 重复槽 ×dup_n
        （mbarrier 事务字节）          （bulk_group 编组等待）
```

#### 4.4.2 核心流程

设备内核的完整协议（`stages` 个 stage 构成环形缓冲，`B` 为批次大小）：

```text
n_groups = dup_counts[0]                        # 设备端读，免宿主同步
若 pdl_launch: pdl_wait_predecessor()           # 等 dispatch 放行

warp 0（生产者），gi 从 bidx*B 起，步进 num_sms*B：
    n = min(B, n_groups - gi)                   # 尾部批次截断
    等 stage[idx] 为空（sync_object_empty.wait）
    lane 0: mbarrier_arrive_and_expect_tx(mbar, n * H_BYTES)
    lane 0: 对批次内每个组 g，发一条 cp.async.bulk G2S
            （源 = gmem 行 primary_loff，目的 = smem stage 第 idx 块第 g 行）
    推进 stage 状态

warp 1（消费者），同样的 gi 序列：
    n = min(B, n_groups - gi)
    lane < n: 每 lane 读一个组头 (dup_start, dup_n)        # 一次合并访存
    consumer_wait(use_state)                     # 等 stage 数据到齐
    对批次内每个组 idx：
        (dup_start, dup_n) = shuffle_sync 自 lane idx 广播
        lane 0: 对 k in [0, dup_n)：slot = dup_loffs[dup_start+k]
                发一条 cp.async.bulk S2G（smem 行 → gmem 行 slot）
    lane 0: cp_async_bulk_commit_group()         # 本批次的所有 S2G 编为一组
    若已发批次数 issued >= stages-1：
        cp_async_bulk_wait_group(stages-1)       # 在途组压到 stages-1 以内
        consumer_release(rel_state)              # 释放最老 stage 给生产者重用
    循环结束后：cp_async_bulk_wait_group(0)      # 排空尾部在途存储
```

三个值得注意的细节：

1. **动态事务字节**：管线创建时 `tx_count=0`（见源码精读），每批由 lane 0 手动 `arrive_and_expect_tx(n * H_BYTES)`——因为尾部批次只有 `n < B` 行，字节数无法在创建期写死。
2. **滞后的 stage 释放**：第 `j` 批的 stage 并非消费完立即释放，而是在处理到第 `j + stages - 1` 批时才 `wait_group(stages-1)` 后释放。原因：S2G 是异步的，commit 返回不代表 smem 已被读走；必须等 bulk_group 完成后才能让生产者重写该 stage。这同时把在途 bulk_group 数维持在 \( \le stages - 1 \)。
3. **组头的 warp 级加载**：消费者让 `lane < n` 的每个 lane 各读一个组头，再用 `shuffle_sync(…, idx)` 按 idx 广播——把 n 次 gmem 标量读合并为一次合并访问加寄存器内广播。

#### 4.4.3 源码精读

**入口与 PDL 等待**：

- [moonep/dispatch_epilogue.py:L168-L186](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L168-L186) —— 内核签名收四个张量；`H / H_BYTES / stages / num_sms / B` 全部 `const_expr` 烧成编译期常量（换任何一个都要重编译，对应 `_get_compiled` 的 cache 键）；若 `pdl_launch` 则第一件事调用 `pdl_wait_predecessor()`——与 dispatch 尾部的 `pdl_trigger_dependents` 配对。随后取 `bidx` 与 warp 均匀化的 `warp_idx`。

**smem 布局与管线**：

- [moonep/dispatch_epilogue.py:L188-L195](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L188-L195) —— `SmemAllocator` 分配 `2*stages` 个 int64 mbarrier 与 `(B, H, stages)` 的 bf16 stage 块（128 字节对齐；ordered layout 让「stage 内第 idx 行」的地址即 `stage_smem.iterator + stage_idx*B*H + idx*H`）。
- [moonep/dispatch_epilogue.py:L197-L206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L197-L206) —— `PipelineTmaAsync.create(..., producer_group=1 线程, consumer_group=1 线程, tx_count=0)`：生产者、消费者各声明为单线程（bulk 指令只能单线程发射）；`tx_count=0` 表示事务字节数不由管线管理，留给每批的 `arrive_and_expect_tx`。

**warp 0：G2S 生产者**：

- [moonep/dispatch_epilogue.py:L210-L230](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L210-L230) —— `gi = bidx * B` 起步；`n = min(B, n_groups - gi)` 处理尾部截断；先 `sync_object_empty.wait` 等 stage 空，随后 lane 0 执行 `mbarrier_arrive_and_expect_tx(mbar, n * H_BYTES)` 为本批次声明精确的事务字节数。
- [moonep/dispatch_epilogue.py:L232-L247](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L232-L247) —— 逐组读组头 `dup_groups_tensor[(gi+idx)*3]`（摊平布局的第 0 列即 `primary_loff`），把 gmem 行地址（64 位计算：`hidden.iterator + Int64(loff) * Int64(H)`，防大缓冲行号溢出）与 smem 行地址传给 `cp_async_bulk_g2s`，行字节数 `H_BYTES`；批末推进 stage 并 `gi += num_sms * B`。G2S 指令本身即 [moonep/_common.py:L414-L435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L414-L435) 封装的 `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes`，完成时自动向 mbarrier 记账字节数。

**warp 1：S2G 消费者**：

- [moonep/dispatch_epilogue.py:L260-L272](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L260-L272) —— 与生产者相同的 `gi` 序列与尾部截断；`lane < n` 的每个 lane 各读一个组头的第 1、2 列（`dup_start`、`dup_n`）。
- [moonep/dispatch_epilogue.py:L274-L291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L274-L291) —— `consumer_wait(use_state)` 等 stage 到齐；对批次内每个组 `idx`，用 `shuffle_sync(dup_start, idx)` / `shuffle_sync(dup_n, idx)` 从 lane idx 把组头广播到全 warp；lane 0 逐槽位读 `dup_loffs_tensor[cur_dup_start + k]`，把 smem 行 S2G 到 gmem 目标行（同样 64 位行地址计算）。这就是 4.3.5 练习 2 中 `B <= 32` 约束的用武之地。
- [moonep/dispatch_epilogue.py:L292-L301](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L292-L301) —— lane 0 `commit_group` 把本批全部 S2G 编组；当已发批次 `issued >= stages-1` 时 `wait_group(stages-1)` 后 `consumer_release(rel_state)` 释放最老 stage（独立的 `rel_state` 相对 `use_state` 滞后 `stages-1` 个批次，正是 4.4.2 细节 2 的实现）。
- [moonep/dispatch_epilogue.py:L304-L305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L304-L305) —— 循环外 `cp_async_bulk_wait_group(0)`：排空最后 `stages-1` 批仍在飞行的存储，确保内核退出前所有 S2G 写已完成（bulk 异步存储的完成只由 bulk_group 跟踪，必须显式等待）。S2G 指令即 [moonep/_common.py:L438-L459](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L438-L459) 封装的 `cp.async.bulk.global.shared::cta.bulk_group`。

**宿主封装**：

- [moonep/dispatch_epilogue.py:L312-L314](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L312-L314) —— 按设备属性查询 opt-in smem 上限并做 `lru_cache`（设备属性查询有运行时开销）。
- [moonep/dispatch_epilogue.py:L317-L336](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L317-L336) —— `_check_epilogue_plan`：三件套必须是连续 int32、形状 `(NvS,3)/(NvS,)/(2,)`、与本设备一致——宿主侧的契约防线，与 planning.py 的 `__post_init__` 断言呼应。
- [moonep/dispatch_epilogue.py:L367-L416](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L367-L416) —— `launch_dispatch_epilogue`：取 `H/NvS/num_sms_dedup`；断言 `H % 8 == 0`（`H_BYTES` 必须是 16 的倍数才满足 bulk 拷贝对齐）、shard 是连续 bf16 CUDA 张量且形状恰为 `(NvS, H)`；经 `_get_compiled` 拿到编译产物，用 `make_ptr` 包装四个裸指针、取**当前 stream**（保证与 dispatch 同 stream 的先后关系）后调用。docstring 再次强调它必须在 `launch_dispatch` 之后运行、复用路径原样传递保存的三件套。

#### 4.4.4 代码实践（本讲主实践，CPU 即可运行）

**实践目标**：为 epilogue 写 PyTorch 参考实现（输入 `dup_groups/dup_loffs/dup_counts` 与 shard，输出展开后的 shard），用随机重复组验证它与「逐组 copy」语义等价——口径与 `tests/planning_reference.py` 构造三件套、`tests/test_dispatch.py` 验证 shard 的思路一致。

**操作步骤**（示例代码，保存为 `epilogue_reference.py`）：

```python
# 示例代码：DispatchEpilogueKernel 的 PyTorch 参考实现 + 随机对拍
import torch

def expand_by_loop(shard, dup_groups, dup_loffs, dup_counts):
    """ground truth：逐组 copy，直接翻译内核语义。原地修改并返回 shard。"""
    n_groups = int(dup_counts[0])            # 参考实现允许宿主端读
    for g in range(n_groups):
        primary_loff, dup_start, dup_n = dup_groups[g].tolist()
        for k in range(dup_n):
            shard[dup_loffs[dup_start + k]] = shard[primary_loff]
    return shard

def expand_by_vector(shard, dup_groups, dup_loffs, dup_counts):
    """向量化版本：等价于内核「读一次主行、扇出全部重复槽」的批量语义。"""
    n_groups, n_dup = int(dup_counts[0]), int(dup_counts[1])
    heads = dup_groups[:n_groups]                       # [n_groups, 3]
    src = heads[:, 0].repeat_interleave(heads[:, 2])    # 每个重复槽的主行
    dst = dup_loffs[:n_dup]                             # 全部重复槽（紧凑前缀）
    shard[dst] = shard[src]                             # 主行只读一次
    return shard

def random_groups(nvs, max_groups=16, max_dup=3, seed=0):
    """随机生成互不相交的 (主行, 重复槽) 组，返回三件套。"""
    g = torch.Generator().manual_seed(seed)
    n_groups = int(torch.randint(1, max_groups + 1, (1,), generator=g))
    dup_n = torch.randint(1, max_dup + 1, (n_groups,), generator=g)
    total = int(dup_n.sum())
    assert n_groups + total <= nvs, "shard 放不下，调小参数"
    # 随机挑主行与重复槽（互异、与主行不相交）
    rows = torch.randperm(nvs, generator=g)[: n_groups + total]
    primaries, dups = rows[:n_groups], rows[n_groups:]
    dup_groups = torch.zeros(nvs, 3, dtype=torch.int32)
    dup_loffs = torch.zeros(nvs, dtype=torch.int32)
    pos = 0
    for i in range(n_groups):
        dup_groups[i] = (int(primaries[i]), pos, int(dup_n[i]))
        pos += int(dup_n[i])
    dup_loffs[:total] = dups
    dup_counts = torch.tensor([n_groups, total], dtype=torch.int32)
    return dup_groups, dup_loffs, dup_counts

# ---- 随机对拍 ----
for seed in range(200):
    nvs, H = 128, 8
    shard = torch.randn(nvs, H)
    groups, loffs, counts = random_groups(nvs, seed=seed)
    a = expand_by_loop(shard.clone(), groups, loffs, counts)
    b = expand_by_vector(shard.clone(), groups, loffs, counts)
    assert torch.equal(a, b), f"seed={seed}: 两种实现不一致"

    # 不变量：dup_counts[1] == sum(dup_n)；重复槽与主行互不相交
    n_groups, n_dup = counts.tolist()
    assert n_dup == int(groups[:n_groups, 2].sum())
    assert len(set(groups[:n_groups, 0].tolist())
               & set(loffs[:n_dup].tolist())) == 0
print("200 组随机对拍全部通过")

# ---- round-robin 批次覆盖检查（复现内核的组分派） ----
def assigned_cta(g, B, num_sms):
    # 组 g 属于批次 j = g // B，批次 j 由 CTA j % num_sms 处理。
    # 注意是取模不是整除——写错成 (g // B) // num_sms 会漏掉后一半 CTA，
    # 正对应 4.3.5 练习 3 中「grid / 步进不一致」的教训。
    return (g // B) % num_sms

dup_groups, dup_loffs, dup_counts = random_groups(nvs, max_groups=50, seed=7)
n_groups = int(dup_counts[0])
B, num_sms = 8, 4
cover = {}
for g in range(n_groups):
    cover.setdefault(assigned_cta(g, B, num_sms), []).append(g)
all_assigned = sorted(x for v in cover.values() for x in v)
assert all_assigned == list(range(n_groups))   # 每组恰好被处理一次
print("round-robin 覆盖检查通过:", {k: len(v) for k, v in cover.items()})
```

**需要观察的现象**：

1. 两种实现（逐组 copy 与向量化扇出）在 200 组随机数据上逐元素相等；
2. `dup_counts[1]` 恒等于各组 `dup_n` 之和，主行与重复槽恒不相交；
3. round-robin 分派把所有组恰好覆盖一次，各组数大致均衡（因组大小随机，CTA 间批次数可能略不均）。

**预期结果**：打印 `200 组随机对拍全部通过` 与 `round-robin 覆盖检查通过: {0: .., 1: .., 2: .., 3: ..}`。脚本只依赖 CPU PyTorch，可直接运行；与真实内核的对照（多卡跑 `tests/test_dispatch.py -k duplicate_topk`）**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么管线创建用 `tx_count=0`，而在每批开始时手动 `arrive_and_expect_tx(n * H_BYTES)`？

**答案**：`tx_count` 是创建期写死的每 phase 事务字节数，适合每批字节数恒定的管线；这里尾部批次的行数 `n < B`，字节数随批次变化，只能由 lane 0 在每批动态声明精确值 `n * H_BYTES`，否则 mbarrier 永远等不到（或提前满足错误的）字节数。

**练习 2**：S2G 为什么用 bulk_group 而不是像 G2S 那样挂 mbarrier？

**答案**：两条指令的 PTX 形态决定的：G2S 变体自带 `mbarrier::complete_tx::bytes` 完成语义，S2G 变体只有 `bulk_group` 编组完成语义、没有 mbar 参数（见 [moonep/_common.py:L438-L448](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L438-L448) 的指令说明）。所以消费侧用 `commit_group` / `wait_group<N>` 管理。

**练习 3**：把循环外的 `cp_async_bulk_wait_group(0)` 删掉，最可能出什么问题？

**答案**：最后 `stages-1` 批的 S2G 仍在飞行时内核就退出了。bulk 异步存储的完成只由 bulk_group 跟踪、必须显式等待，删掉后不能保证退出时这些写已落盘，后续读取者（分组 GEMM）可能看到重复槽的旧数据。

## 5. 综合实践

把本讲四个模块串成一个「mini dispatch → epilogue」模拟器（CPU 即可运行，示例代码）：

**任务**：编写 `epilogue_sim.py`，模拟一个小规模单 rank 接收视角的重复展开全流程：

1. **构造路由**：仿照 `duplicate_topk` 路由（[tests/kernel_test_utils.py:L107-L109](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L107-L109)），令 `S=32` 个 token、`K=4`，每个 token 的 K 个 top-k 中随机让 2~4 个落到「本 rank」（制造重复组），其余落到别的 rank。
2. **模拟 dispatch**：为每个落到本 rank 的条目分配 shard 行号；每个 token 在本 rank 的多个条目中，取 k 最小者为主行（对应发送端 canonical 规则），其余为重复槽并编码负数 dst `-raw_dst-1`。
3. **模拟 builder**：参考 [tests/planning_reference.py:L263-L295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L263-L295) 的分组逻辑，产出三件套（注意 `dup_count = len(group_loffs) - 1`、组头记录 `(primary_loff, dup_start, dup_n)`、`dup_loffs` 存剔除主行后的槽位）。
4. **模拟 epilogue**：调用 4.4.4 的参考实现展开 shard。
5. **验证**（模拟 `_verify_dispatch_by_dst` 的口径，[tests/test_dispatch.py:L224-L243](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L224-L243)）：对每个条目——**包括负数 dst 的条目**——解码 `raw_dst = -dst_val - 1` 后断言 shard 对应行持有该 token 的标记数据；再断言 `dup_counts[1] == sum(dup_n)`、主行与重复槽不相交。

**预期结果**：全部断言通过；负数 dst 条目与主行条目校验出相同的数据，直观呈现「省 NVLink 传输（dispatch 只写主行）+ 本地扇出（epilogue 补齐）」这对设计的完整闭环。与真实多卡内核的对照**待本地验证**（运行 `torchrun --nproc_per_node=2 -m pytest tests/test_dispatch.py`）。

## 6. 本讲小结

- dispatch 的负数 dst 把「同一 token 多份拷贝」压缩成一次 NVLink 传输，代价是接收 shard 留下重复槽空洞；`DispatchEpilogueKernel` 在 dispatch 之后**原地、纯本地**地用主行补齐所有重复槽，且不需要自己的跨 rank 屏障（输入由 dispatch 退出屏障保证、输出由本地 GEMM / combine 入口屏障消费）。
- launch 几何完全静态：grid 恒为 `num_sms_dedup`（可用 `MOONEP_NUM_SMS_DEDUP` 覆盖），组批次 `i*B` round-robin 分派给 CTA `i % num_sms`，保证每组恰好被处理一次；`(B, stages)` 按「能双缓冲的最大批次」贪心选择，因为握手摊销主导、深度超过 2 无收益。
- `dup_counts[0]` 在**设备端**读取作为循环边界，加上编译期几何常量，使 planning → dispatch → epilogue 全链路零宿主同步；三件套组间顺序不稳定无碍正确性，因为各组写的槽位集合互不相交。
- 内核是双 warp 流水线：warp 0 逐行 G2S 主行进 stage smem（每批动态 `arrive_and_expect_tx(n*H_BYTES)`），warp 1 用「lane 读头 + shuffle 广播 + lane 0 逐槽 S2G」扇出；S2G 走 bulk_group 编组，stage 释放滞后 `stages-1` 批、在途组数压在 `stages-1` 以内，循环外 `wait_group(0)` 排空。
- PDL 让 epilogue 提前启动：dispatch 尾部 `cross_rank_barrier` 后 `pdl_trigger_dependents`（先 fence 发布再放行），epilogue 头部 `pdl_wait_predecessor` 等待——两个开关必须由同一个 `enable_pdl` 控制；即便关闭 PDL，同 stream 顺序仍保证正确性。

## 7. 下一步学习建议

下一讲 **u4-l5 combine prologue：本地重复累加** 是本讲的镜像：epilogue 在 dispatch 后做「扇出」（1 行 → N 行），prologue 在 combine 前做「归约」（N 行 fp32 累加 → 1 行），两者共享三件套契约、持久 CTA 几何与 smem 流水线骨架，但方向相反且多了「生产者预扫描 + `expect_tx` 精确声明」。建议先重读本讲的 4.4 节再去读 `moonep/combine_prologue.py`，对比两处 `while gi < n_groups` 循环的差异；之后进入 u4-l6 的 combine 主内核，看入口跨 rank 屏障为什么放在 combine 而非 prologue。
