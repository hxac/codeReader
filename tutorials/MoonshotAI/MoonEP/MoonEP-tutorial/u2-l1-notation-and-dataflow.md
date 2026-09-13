# u2-l1 符号系统与端到端数据流

## 1. 本讲目标

学完本讲，你应该能够：

1. **熟练说出 MoonEP 的每一个符号**：S、K、E、R、B、H、H'、N、NvS、NvS_padded、token_padding、epn，以及它们之间的换算关系（例如 \( NvS = S \times K + (\text{token\_padding}-1) \times 2 \times \frac{E}{R} \)）。
2. **跟踪一次前向中所有张量的流转**：从 `dispatch` 的输入 `[S, H]`，到派发结果 `[NvS, H]`，再到 `combine` 归并回 `[S, H]`，每一步的形状、dtype、设备如何变化，做到心算可得。
3. **理解静态形状（static shapes）的价值**：为什么「每个 rank 恰好接收 S×K 个 token」能消除大模型训练中逐层 MoE 的宿主同步（host synchronization）与显存碎片。
4. **读懂 `_create_context` 的全部尺寸计算**：这是 Buffer 构造时一次性预分配所有通信缓冲的地方，是后续所有内核讲义的地基。

本讲是整个「内存基础设施」与「通信内核」两个单元的**通用语言课**——后面每一讲引用这些符号时都不再重复解释。

## 2. 前置知识

本讲假设你已完成 u1-l4（Buffer API 快速上手），知道 `Buffer` 有 `dispatch` / `prefetch_weight` / `combine` / `reduce_grad` 四个入口。此外需要以下基础概念：

- **张量的三个基本属性**：`shape`（形状）、`dtype`（数据类型，如 bf16/fp32/int32）、`device`（所在设备，如 `cuda:0`）。形状流转就是跟踪这三个属性在流水线中的变化。
- **token-major 与 expert-grouped 两种排布**：
  - *token-major*：一行一个 token，按 token 顺序排。`[S, H]` 就是 token-major。
  - *expert-grouped*（专家分组排布）：同一专家的 token 连续放在一起。dispatch 的输出 `[NvS, H]` 就是 expert-grouped——这样专家 GEMM 才能对每一段连续行做一次矩阵乘。
- **宿主同步（host synchronization）**：GPU 上的数据要想影响 Python 层的控制流（比如决定下一个张量的形状），必须从显存拷回主机内存（device-to-host copy），这一步会强制 CPU 等待 GPU——这就是一次「同步」。训练框架里每层来一次，代价巨大。
- **VMM 粒度对齐（粗略概念即可）**：MoonEP 用 CUDA 虚拟内存管理（VMM）把所有 rank 的显存映射成一块连续地址，但物理映射的最小单位是「粒度」（granularity，常见为 2 MiB），所以每块缓冲的字节数必须向上取整到粒度的倍数。细节在 u2-l2 详讲，本讲只需要接受「分配前要 pad」这一事实。
- **MoE 前向的直觉**：每个 token 经路由器选出 K 个专家（top-k），把自己的隐藏向量发出去算，再把 K 份结果加权求和收回来。u1-l1 已讲过 maxvio 与负载不均衡问题。

## 3. 本讲源码地图

| 文件 | 本讲关注点 | 作用 |
| --- | --- | --- |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1-L48) | 模块头文档（符号定义）、`_create_context`（尺寸计算）、四个入口的形状契约 | 顶层 API，本讲主战场 |
| [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L43) | Notation 段落、weight buffer 布局、静态形状声明 | 官方符号约定与权重契约 |
| [moonep/buffer.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L89-L110) | `pad_to_granularity` / `pad_dim0_for_alignment` | `NvS_padded` 的来源（本讲只用结论，u2-l2 详讲） |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1202-L1233) | `allocate_planning_outputs` 中 `cu_seqlens` 的分配 | 确认 `cu_seqlens` 形状为 `[E+B]`（语义细节 u3-l1 详讲） |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 符号系统**（api 模块头文档）、**4.2 `_create_context` 尺寸计算**、**4.3 端到端数据流**。

为讲述方便，全讲使用一个贯穿示例配置（与 README 用法示例一致）：

```
S=4096, H=7168, K=8, E=256, R=8, token_padding=128（默认）, num_sms=None（默认 32）
```

### 4.1 模块一：符号系统 —— api.py 头文档与 README Notation

#### 4.1.1 概念说明

MoonEP 的所有文档、注释、代码都在用同一套单字母符号。这套符号在 [moonep/api.py:L4-L8](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L4-L8) 的模块头文档中一次性定义，README 的 Integration 段落（[README.md:L43](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L43)）逐字重复了它。完整符号表如下：

| 符号 | 含义 | 示例值 | 备注 |
| --- | --- | --- | --- |
| `S` | 每个 rank 的输入 token 数（**S**equence/tokens per rank） | 4096 | 构造参数 |
| `K` | 每个 token 路由到的专家数，即 routed top-**k** | 8 | 构造参数 |
| `E` | EP 组内路由专家总数（**E**xperts） | 256 | 构造参数，须满足 `E % R == 0` |
| `R` | EP 组的 rank 数（**R**anks，EP comm size） | 8 | 构造参数 `num_ep_ranks` |
| `epn` | 每个 rank 持有的本地专家数（**e**xperts **p**er **n**ode）= E/R | 32 | 派生量 |
| `B` | 每个 rank 的权重预取槽数（prefetch slots） | 默认 `E/R`=32 | 构造参数，`None` 时取默认 |
| `H` | 隐藏维大小（**H**idden size） | 7168 | 构造参数 |
| `H'` | 专家 FFN 中间维大小 | 由用户权重决定 | 只出现在权重/梯度 `[E+B, H, H']` 中 |
| `N` | 每个 rank 派发的 token 拷贝总数 = S×K | 32768 | 派生量 |
| `NvS` | 每个 rank 的派发槽位数（**N**umber **v**s **S**eats / dispatched token slots per rank）：S×K 个真实 token 加上分段 padding 余量 | 40896 | 派生量，见 4.1.2 |
| `token_padding` | 每个非空专家分段的 token 数向上取整的基数 | 128（默认） | 构造参数 |

三个最容易被忽视的关系：

1. **每个 rank 发送 S×K 份、也恰好接收 S×K 份。** 每个 token 复制 K 份发往 K 个专家，所以发送量是 \( N = S \times K \)。MoonEP 的核心保证是**接收量也恒等于 N**，与路由多偏无关（README 第 7 行的 "Perfect balance"）——这是静态形状的根基。
2. **接收缓冲要留 padding 余量，所以 NvS > N。** 规划器把接收到的 token 按专家分段，每段行数向上取整到 `token_padding` 的倍数（为了对齐 GEMM 的 M 维），于是总槽数会比 S×K 多出一截。
3. **`NvS` 是用户可见的形状，`NvS_padded` 是物理缓冲的形状。** 物理分配还要再对齐到 VMM 粒度（4.2 详讲），但多出来的行对用户不可见。

#### 4.1.2 核心流程

符号之间的派生关系可以画成一棵公式树（自上而下计算）：

```
R = num_ep_ranks                     (用户给定)
epn = E // R                         (每 rank 本地专家数)
B = E // R                           (默认值；训练必须取此值)
N = S * K                            (真实派发/接收 token 数)
NvS_capacity = S * K                 (= N，缓冲的"真实容量"下界)
token_padding_extra = (token_padding - 1) * 2 * epn
NvS = NvS_capacity + token_padding_extra   (逻辑槽位 = 真实 + padding 余量)
NvS_padded = pad_dim0_for_alignment([NvS, H], bf16)   (物理行数，VMM 对齐)
```

其中 padding 余量上界的推导（理解即可，源码注释里有原文）：每个目的 rank 最多接收来自 **2·E/R 个专家分段**的 token——E/R 个本地专家分段，加上至多 E/R 个远程专家分段（当前构造式规划器限制每个 rank 只从一个远程 home group 复制专家，u3-l2 详讲）。每个非空分段「至少 1 个真实 token 也要占 `token_padding` 行」，即最多浪费 \( \text{token\_padding} - 1 \) 行，所以：

\[ \text{extra} \le (\text{token\_padding} - 1) \times 2 \times \frac{E}{R} \]

代入示例配置：

\[ NvS = 32768 + 127 \times 2 \times 32 = 32768 + 8128 = 40896 \]

注意 40896 约是 S×K 的 1.25 倍——**为了静态形状，MoonEP 宁愿多分配 25% 的接收槽位**，也不让形状随路由变化。

#### 4.1.3 源码精读

**符号的权威定义**在模块头文档：

- [moonep/api.py:L4-L8](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L4-L8) —— 逐字定义了 S/K/E/R/B/NvS/H/H'，并明确 NvS = "S*K real tokens plus per-VM-group padding"。这里的 VM group 指权重张量 `[E+B, H, H']` 的一个专家行（README 第 45 行：「`cu_seqlens[E+B]` selects which expert rows are active」），每个 VM group 在接收缓冲里占一段 padded token 行。
- [README.md:L43](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L43) —— README Integration 段落的 Notation，与 api.py 一字不差，说明这套记号是**库作者与用户之间的正式契约**。

头文档的后半部分是一次完整生命周期的方法调用序列：

- [moonep/api.py:L10-L47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L10-L47) —— 从 `Buffer(S, H, K, E, num_ep_ranks, ...)` 构造，到 dispatch fwd → prefetch → combine fwd → combine bwd（复用 plan 再 dispatch）→ dispatch bwd（combine 归约梯度）→ `reduce_grad` → `destroy()`。这个顺序就是本讲 4.3 数据流跟踪的脚本。

**派生量的计算源码**（本节先看 NvS 一处，完整拆解读见 4.2.3）：

- [moonep/api.py:L263-L275](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L263-L275) —— 计算 `N = S * K`（第 263 行）；`epn = E // R` 并断言 `E % R == 0`（第 269-270 行）；`B` 缺省时取 `B = epn`（第 273-274 行）。
- [moonep/api.py:L277-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288) —— NvS 的全部计算：第 277 行 `NvS_capacity = S * K`；第 287 行 `token_padding_extra = (token_padding - 1) * 2 * epn`；第 288 行 `NvS = NvS_capacity + token_padding_extra`。第 279-286 行的注释解释了为什么上界是 `(token_padding-1) * 2 * E/R`：每个 rank 至多 E/R 个远程分段 + E/R 个本地分段，每个非空分段最多浪费 token_padding−1 行。

**B 的取值规则**在 README 有明确论述：

- [README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59) —— 训练**必须** `B = E/R`（保证组 GEMM 触及的每个专家都在本地有权重行）；推理推荐 `B = 3~4`，溢出时组 GEMM 直接经对称内存从 home rank 远读，慢一点但不影响正确性。

#### 4.1.4 代码实践

**实践一：手算一个小配置的符号表（纸笔实践，无需 GPU）**

1. **实践目标**：不看公式树，独立推导一个小配置下的全部符号值，检验对符号系统的掌握。
2. **操作步骤**：取 `S=128, K=4, E=32, R=4, token_padding=32`，按定义依次计算 `epn`、`B`（默认）、`N`、`token_padding_extra`、`NvS`。
3. **需要观察的现象**：NvS 相对 S×K 的放大比例，与本讲示例配置（1.25 倍）是否一致？为什么？
4. **预期结果**：
   - `epn = 32/4 = 8`，`B = 8`
   - `N = 128×4 = 512`
   - `extra = (32−1)×2×8 = 496`
   - `NvS = 512 + 496 = 1008 ≈ 1.97×S×K`——放大比例更大。原因：token_padding 相对 S×K 越大（本例 32 对 512），padding 余量占比越高。这正是 `token_padding` 作为调优参数的意义：更小的 padding 省显存，但 GEMM 的 M 维对齐变差。
   - 本实践为纯算术，结果可直接验证，无需标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：给定 `S=2048, K=4, E=64, R=8, token_padding=128`，求 `N`、`epn`、默认 `B`、`NvS`。

**答案**：`N = 2048×4 = 8192`；`epn = 64/8 = 8`；默认 `B = 8`；`extra = 127×2×8 = 2032`；`NvS = 8192 + 2032 = 10224`。

**练习 2**：为什么训练必须 `B = E/R`，而推理可以 `B < E/R`？

**答案**：训练时预取槽会产生梯度，若专家不在本地行上，梯度归属与归约路径（`reduce_grad` 的 `[R, B, H, H']` reduce buffer）就无法成立；而规划器至多从一个远程 home group 复制（≤ E/R 个专家），所以 `B = E/R` 保证覆盖。推理没有梯度，溢出的专家可由组 GEMM 经对称内存直接从 home rank 读取——只是变慢，不影响正确性（依据 [README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)）。

**练习 3**：如果传入 `E=100, R=8`，Buffer 构造时会发生什么？

**答案**：断言失败。`_create_context` 中 `assert E % R == 0`（[moonep/api.py:L270](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L270)）要求 E 被 R 整除，因为每个 rank 必须均分持有 E/R 个 home 专家。

### 4.2 模块二：`_create_context` 尺寸计算 —— 从符号到显存

#### 4.2.1 概念说明

`_create_context` 是 Buffer 的「算地契」函数：**在构造时一次性算出并分配所有通信缓冲**，之后每个训练步、每一层 MoE 都只是复用这块显存，不再有任何分配。它解决的问题是：

1. **静态形状的物理载体**。4.1 里的符号不能只停留在纸面上——必须变成实际的 CUDA 张量，且形状永不变化。
2. **对称内存的布局协调**。`hidden_buf`（放派发 token）和 `meta_buf`（放路由权重与规划元数据）要被所有 rank 读写，每个 rank 拥有其中一段 chunk，chunk 的字节大小必须同时满足 VMM 映射对齐与组播绑定对齐，否则映射会失败。
3. **溢出防护**。内核里大量地址运算使用 int32（CuTe DSL 的 Int32 算术），构造期必须静态证明最大索引不会溢出。

理解本模块后，你看到的将不再是一个个孤立数字，而是「哪块缓冲、多大、为什么是这个大小」。

#### 4.2.2 核心流程

`_create_context` 的执行流程（按源码顺序）：

1. **参数校验与基础派生**：`num_sms` 缺省 32、校验为正整数；取 `rank`；断言 `num_ep_ranks == group world size`；计算 `N = S*K`、`epn = E//R`（断言整除）、`B` 缺省值（断言为正）。
2. **NvS 计算**：`NvS_capacity = S*K`，加 padding 余量得 `NvS`（4.1.2 已详讲）。
3. **规划常数与 int32 守卫**：`BLOCK_SIZE_P2 = 2048`，`num_vblocks = ceil(N / 2048)`（规划内核排序的分块数，u3-l4 详讲）；断言 `0 < N < 2^31−1`。
4. **物理对齐**：`NvS_padded = pad_dim0_for_alignment([NvS, H], bf16)`——把 `[NvS, H]` 的总字节数向上对齐到 VMM 粒度后折算回行数。
5. **meta_buf 一维布局**：在一个 int32 一维缓冲里按偏移切出 7 个区段（weights / tpe / plan / topk0 / order / order0 / barrier / src_info），每段起点做 4 元素（16 字节）对齐。
6. **chunk 双重对齐**：`meta_chunk_padded` 把 meta chunk 的字节数对齐到 `max(VMM 粒度, 组播粒度)`。
7. **int32 溢出守卫**：静态断言 `max_meta_index` 与 `max_hidden_index` 不超过 `2^31−1`。
8. **分配**：`create_nvl_dist_tensor` 分配 `hidden_buf`；`create_nvl_dist_multicast_tensor` 分配 `meta_buf` 及其组播视图；清零 barrier 区段。
9. **本地 scratch 与视图**：分配规划用的本地临时张量；从全局缓冲切出本 rank 的局部视图 `hidden_buf_local`（`[NvS, H]`）与 `weights_buf_local`（`[NvS]`）。
10. **打包进 ctx 字典**返回，并调用 `_log_context_buffer_size` 打印缓冲统计。

#### 4.2.3 源码精读

**(1) NvS 与 padding 余量**（4.1.3 已引用，此处看注释原文）：

- [moonep/api.py:L277-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288) —— 注释明确写出「The current constructive planner lets each destination rank receive tokens from at most one remote home group… so the extra logical NvS slot bound is (token_padding - 1) * 2 * E/R」。

**(2) NvS_padded 的物理对齐**：

- [moonep/api.py:L305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L305) —— `NvS_padded = pad_dim0_for_alignment([NvS, H], torch.bfloat16)`。
- [moonep/buffer.py:L89-L110](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L89-L110) —— `pad_to_granularity` 把字节数向上取整到 VMM 粒度；`pad_dim0_for_alignment` 先算每行字节数（`itemsize × 后续维度`），把总字节对齐后折算回行数。示例配置：每行 \( 7168 \times 2 = 14336 \) 字节，\( 40896 \times 14336 \approx 559.1 \) MiB，对齐到 2 MiB 粒度后为 560 MiB，折算 \( 560 \times 2^{20} / 14336 = 40960 \) 行，即 `NvS_padded = 40960`（多出的 64 行用户不可见）。

**(3) meta_buf 的分区偏移**（本讲只需看懂「一段 int32 缓冲切成七段」，各区段语义在 u2-l4/u3 逐个展开）：

- [moonep/api.py:L307-L333](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L307-L333) —— 从 `WEIGHTS_OFF = 0` 开始，依次是 `TPE_OFF`（每 rank 每专家 token 计数汇聚区，长度 R×E）、`PLAN_OFF`（rank0 规划结果的广播暂存区）、`TOPK0_OFF` / `ORDER_OFF` / `ORDER0_OFF`（三个长度各为 N4 = align_up(N,4) 的规划 scratch）、`BARRIER_OFF`（3 个 int32 的跨 rank 屏障）、`SRC_INFO_OFF`（长度 NvS 的溯源信息区）。每段起点用 `_align_up(x, 4)` 对齐到 4 个 int32（16 字节），保证内核向量化读写不越界。
- 辅助函数 [moonep/api.py:L77-L79](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L77-L79) —— `_align_up(x, alignment)` 就是标准的向上取整除法。
- `_create_context` 的 docstring（[moonep/api.py:L233-L252](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L233-L252)）给出了与代码同步更新的完整布局说明，其中 weights 区段「fp32, alias as int32」——路由权重按 fp32 存储、借 int32 缓冲的字节空间复用。

**(4) chunk 双重对齐与 int32 守卫**：

- [moonep/api.py:L335-L340](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L335-L340) —— 注释「Chunk size must satisfy both VMM mapping and multicast binding」：`chunk_align_bytes = max(VMM 粒度, 组播粒度)`，`meta_chunk_padded` 是对齐后的 int32 元素数。
- [moonep/api.py:L342-L356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L342-L356) —— 两道静态守卫：`max_meta_index = (R−1)×meta_chunk_padded + meta_chunk_logical − 1` 与 `max_hidden_index = (R−1)×NvS_padded + NvS − 1` 都必须 ≤ `2^31−1`。原因是 CuTe DSL 地址表达式会用运行时 Int32 的 rank 编号乘以这些 constexpr 步长，int32 溢出会直接算出错误地址——构造期把最大可达索引挡下来，比运行期出错好排查得多。

**(5) 分配与本地视图**：

- [moonep/api.py:L361-L364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361-L364) —— `hidden_buf = create_nvl_dist_tensor([NvS_padded, H], bf16, rank, R, ...)`；`meta_buf, meta_mc = create_nvl_dist_multicast_tensor([meta_chunk_padded], int32, ...)`。传入的形状是**每 rank chunk** 的形状，返回的张量把 R 个 chunk 映射成一块连续虚拟地址，所以 `hidden_buf` 的总形状是 `[R×NvS_padded, H]`（本例 `[327680, 7168]` bf16 ≈ 4.375 GiB），`meta_buf` 总形状是 `[R×meta_chunk_padded]`。访问第 r 个 rank 的 chunk 用 `hidden_buf[r*NvS_padded : ...]`。
- [moonep/api.py:L432-L434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L432-L434) —— 本地视图：`hidden_buf_local = hidden_buf[rank*NvS_padded : rank*NvS_padded + NvS]`（注意切的是 **NvS** 不是 NvS_padded——用户只该看到逻辑槽位），`weights_buf_local` 同理切出 meta_buf 的 weights 区段。
- [moonep/api.py:L394-L435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L394-L435) —— ctx 字典把所有符号、偏移、缓冲打包，是后续每个 `launch_*` 内核的共同入参。本讲的符号表就是这个字典的 key 清单。

**(6) 构造期打印**（实践时的对拍工具）：

- [moonep/api.py:L143-L154](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L143-L154) —— `_log_context_buffer_size` 会在 rank 0 打印一行 `config: R=…, S=…, H=…, E=…, K=…, NvS=…, NvS_padded=…`。真实硬件上构造 Buffer 后，把这行日志与本讲综合实践的脚本输出逐项对照，即可验证你的手算。

**示例配置的完整数值表**（NvS 之前为纯算术、确定无疑；带 ※ 的两项依赖运行时查询的 VMM 粒度，下表按常见 2 MiB 粒度计算）：

| 量 | 公式 | 值 |
| --- | --- | --- |
| `N` | S×K = 4096×8 | 32768 |
| `epn` = `B` | E/R = 256/8 | 32 |
| `token_padding_extra` | 127×2×32 | 8128 |
| `NvS` | 32768+8128 | **40896** |
| `NvS_padded` ※ | 对齐 2 MiB | 40960 |
| `TPE_OFF` | align_up(40896, 4) | 40896 |
| `PLAN_OFF` | align_up(40896+2048, 4) | 42944 |
| `TOPK0_OFF` | PLAN_OFF + planning_out_elems(13328) | 56272 |
| `ORDER_OFF` / `ORDER0_OFF` / `BARRIER_OFF` | 每次 +N4(32768) | 89040 / 121808 / 154576 |
| `SRC_INFO_OFF` | BARRIER_OFF + 3 | 154579 |
| `meta_chunk_logical` | SRC_INFO_OFF + NvS | 195475 |
| `meta_chunk_padded` ※ | 对齐 2 MiB 后 ÷4 | 524288 |
| `max_hidden_index` | 7×40960+40895 | 327615 ≪ 2³¹−1 ✓ |

#### 4.2.4 代码实践

**实践二：在 Python REPL 里复现 meta 布局偏移（纯 CPU，无需 GPU 与 MoonEP 安装）**

1. **实践目标**：亲手执行 4.2.3 中（3）（4）的偏移公式，观察改 `token_padding` 对布局的连锁影响。
2. **操作步骤**：

   ```python
   # 示例代码：独立复现 _create_context 的 meta 偏移公式（对照 moonep/api.py L307-L333）
   def align_up(x, a):
       return ((x + a - 1) // a) * a        # 对应 _align_up

   S, K, E, R, token_padding = 4096, 8, 256, 8, 128
   N = S * K                                # 32768，对应 L263
   epn = E // R                             # 对应 L269
   B = epn                                  # B 默认 E//R，对应 L273-L274
   NvS = N + (token_padding - 1) * 2 * epn  # 对应 L287-L288
   N4 = align_up(N, 4)
   TPE_OFF = align_up(NvS, 4)
   PLAN_OFF = align_up(TPE_OFF + R * E, 4)
   planning_out_elems = 3*E*R + R*(E+B) + 2*R*(E+B) + B*R + 2*R   # 对应 L315-L321
   TOPK0_OFF = align_up(PLAN_OFF + planning_out_elems, 4)
   ORDER_OFF, ORDER0_OFF = TOPK0_OFF + N4, TOPK0_OFF + 2 * N4
   BARRIER_OFF, SRC_INFO_OFF = TOPK0_OFF + 3 * N4, TOPK0_OFF + 3 * N4 + 3
   meta_chunk_logical = SRC_INFO_OFF + NvS
   print(NvS, meta_chunk_logical)
   # 再把 token_padding 改成 64 重复一遍，观察各偏移如何整体左移。
   ```

3. **需要观察的现象**：`token_padding` 从 128 改成 64 时，`NvS` 从 40896 变为多少？哪些偏移随之变化、哪些不变（提示：`TPE_OFF` 之前的区段只依赖 NvS，`N4` 只依赖 N）？
4. **预期结果**：`token_padding=64` 时 `extra = 63×64 = 4032`，`NvS = 36800`，`meta_chunk_logical = SRC_INFO_OFF(36800+2048+13328+3×32768+3) + 36800`，所有依赖 NvS 的偏移整体减小。若你的输出与讲义数值表不一致，优先检查 `planning_out_elems` 是否漏加 `2*R` 项。纯算术可直接验证；带 ※ 的两项需在真实硬件上以 `get_vmm_granularity()` 查询（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`NvS` 与 `NvS_padded` 有何区别？哪一个决定 `dispatch` 返回张量的第一维？

**答案**：`NvS` 是逻辑槽位数（真实 S×K + padding 余量），决定用户可见形状（`dispatch` 返回 `[NvS, H]`）；`NvS_padded` 是把 chunk 字节对齐到 VMM 粒度后的物理行数，只存在于 `hidden_buf` 内部（每 rank chunk 为 `[NvS_padded, H]`），用户永远看不到多出的行。

**练习 2**：[moonep/api.py:L351-L356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L351-L356) 的 `max_hidden_index` 守卫在防什么？为什么必须在构造期而不是运行期检查？

**答案**：内核地址表达式以 Int32 计算 `(rank) × NvS_padded + 行号`，若最大索引 \( (R-1) \times NvS_{padded} + NvS - 1 \) 溢出 int32，会得到错误地址（可能静默写坏别的缓冲）。它是 S/H/K/E/R 的确定性函数，构造期一次断言即可永久排除，无需运行期开销。

**练习 3**：meta chunk 的对齐为什么要取 `max(VMM 粒度, 组播粒度)` 而不是只取 VMM 粒度？

**答案**：同一个 meta chunk 既要被 VMM 逐 rank 映射成连续虚拟地址，又要被 NVSwitch 组播对象绑定（`multimem.st` 一次写全 rank），两种机制各有自己的最小对齐下限，chunk 字节数必须同时满足两者（[moonep/api.py:L335-L340](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L335-L340) 注释「must satisfy both」）。组播机制本身在 u2-l4 展开。

### 4.3 模块三：一次前向的端到端数据流

#### 4.3.1 概念说明

现在把符号串成一条流动的河。一次 MoE 前向在 MoonEP 眼里是五幕剧：

1. **dispatch（派发）**：把 token-major 的 `[S, H]` 打散，每 token 复制 K 份，直接写到各 rank 接收缓冲中「目标专家分段」的位置上——输出是 expert-grouped 的 `[NvS, H]`，同时产出路由权重的重排 `[NvS]`、分段索引 `cu_seqlens [E+B]` 和通信计划 `plan`。
2. **prefetch_weight（权重预取）**：按 `plan` 把热门远程专家的权重搬进本地预取槽（权重张量 `[E+B, H, H']` 的行 `[E, E+B)`）。
3. **专家 FFN（用户代码）**：组 GEMM 按 `cu_seqlens` 对每个非空专家分段做门控/上行/下行投影，**原地**把 `[NvS, H]` 变换为专家输出——形状不变。
4. **combine（归并）**：把每个 token 的 K 份结果按 `plan.dst` 找回、以 fp32 精度加权累加，归约回 token-major 的 `[S, H]`。
5. **反向**：combine bwd 是「再 dispatch」（复用 plan），dispatch bwd 是「combine + reduce_grad」——对偶结构。

贯穿始终的关键约定：**`cu_seqlens` 停留在 GPU 上**。它由规划内核在设备上写出、由组 GEMM 在设备上消费，全程不回主机——这正是「静态形状消除宿主同步」的具体机制。

#### 4.3.2 核心流程

以示例配置（NvS=40896，E+B=288）列出完整张量流转表：

| 步骤 | 张量 | 形状 | dtype | 设备 | 排布 |
| --- | --- | --- | --- | --- | --- |
| dispatch 输入 | `hidden_sh` | [4096, 7168] | bf16 | cuda | token-major |
| dispatch 输入 | `route_weights_sk` | [4096, 8] | fp32 | cuda | token-major |
| dispatch 输入 | `topk_experts_sk` | [4096, 8] | int32 | cuda | token-major |
| dispatch 输入 | `tokens_per_expert` | [256] | int32 | cuda | 每专家计数 |
| （内部） | `hidden_buf` | [327680, 7168]（R×NvS_padded） | bf16 | NVL 对称显存 | expert-grouped |
| （内部） | `meta_buf` | [R×meta_chunk_padded] | int32 | NVL 对称显存 | 一维分区 |
| dispatch 输出 | `hidden_nvsh` | **[40896, 7168]** | bf16 | cuda | expert-grouped |
| dispatch 输出 | `route_weights_nvs` | [40896] | fp32 | cuda | expert-grouped |
| dispatch 输出 | `cu_seqlens` | [288]（E+B） | int32 | cuda（**不回主机**） | 分段前缀和 |
| dispatch 输出 | `plan` | `MoonEPCommPlan`（设备张量集合） | — | cuda | — |
| prefetch 输入 | `full_*_weight` | [288, 7168, H′] | bf16（或量化） | cuda | 按专家行 |
| 专家 FFN | `expert_output_nvsh` | [40896, 7168]（原地） | bf16 | cuda | expert-grouped |
| combine 输入 | `hidden_nvsh` | [40896, 7168] | bf16 | cuda | expert-grouped |
| combine 输出 | `output_sh` | **[4096, 7168]** | bf16 | cuda | token-major |
| combine 输出 | `gathered_route_weights_sk` | [4096, 8] 或 None | fp32 | cuda | token-major |
| reduce_grad 输入 | `full_*_grad` | [288, 7168, H′] | fp32 | cuda | 按专家行 |
| reduce_grad 输入 | `*_reduce_buffer` | [8, 32, 7168, H′]（[R,B,H,H′]） | fp32 | cuda | 全 rank 视图 |

`cu_seqlens` 的语义：`cu_seqlens[e]` 是第 e 个专家行（VM group）分段的 **padded 累计末偏移**，即该分段的 token 占据 `hidden_nvsh[cu_seqlens[e-1] : cu_seqlens[e]]`（首段从 0 开始）。每个非空分段的长度是 `token_padding` 的倍数；空段长度为 0。因此用户可见的有效行只到 `cu_seqlens[E+B-1]` 为止，且**行内容只在 cu_seqlens 覆盖的分段内有定义**——分段间的 padding 行由 dispatch 的零填充 warp 清零（u4-l3 详讲），不应读取其语义。

**静态形状为什么消除逐层宿主同步**——对比两种世界：

- **动态形状世界（无冗余专家的 EP）**：每个专家每步收到的 token 数 \( T_e \) 随路由波动。dispatch 完成后，框架必须知道每段的长度才能分配输出张量、确定每个 GEMM 的 M 维——这些信息在 GPU 上，于是每层 MoE 都要一次 D2H 拷贝 + CPU 等待（宿主同步）；形状每步都变还导致反复分配/释放，产生显存碎片，高不均衡下直接 OOM（[README.md:L31](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L31) 描述的 DeepEP 现象）。
- **MoonEP 的静态形状世界**：接收量恒为 S×K，`NvS` 在 Buffer 构造那一刻就定死，`hidden_nvsh` 每步形状相同、甚至可以是同一块缓冲的视图；每专家的动态边界全部编码进设备上的 `cu_seqlens`，组 GEMM 在设备上读取。于是**每层 MoE 零宿主同步、零分配**（[README.md:L9](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L9)：「statically known shapes eliminate per-layer MoE host synchronization」；[README.md:L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L32)：训练在任意不均衡下不 OOM）。

数学上，MoonEP 把逐层变化的每专家负载 \( T_e \)（正是 u1-l1 的 maxvio 来源）从「形状」降格为「设备上的数据」：形状层面只剩常量 \( NvS \)，波动被 padding 余量 \( (P-1) \cdot 2E/R \) 吸收。

#### 4.3.3 源码精读

**(1) dispatch 的形状契约**：

- [moonep/api.py:L736-L743](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L736-L743) —— 输入契约：`hidden_sh` 为 `[S, H]` bf16；`route_weights_sk` 为 `[S, K]` fp32（传 `None` 可整体跳过权重通道）；`topk_experts_sk` 为 `[S, K]` int32；`tokens_per_expert` 为 `[E]` int32（本 rank 的每专家计数）。
- [moonep/api.py:L769-L780](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L769-L780) —— 返回值契约：`hidden_nvsh` 为 `[NvS, H]` bf16（"dispatched tokens in physical VM group order"）；`route_weights_nvs` 为 `[NvS]` fp32；`cu_seqlens` 为 `[E+B]` int32 的 padded 分段末偏移，**plan 复用路径下为 None**；`plan` 需保存给 prefetch/combine 与两个反向。
- [moonep/api.py:L797-L808](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L808) —— 输出分配的实现：非零拷贝时 `hidden_nvsh = torch.empty_like(ctx['hidden_buf_local'])`——注意 local 视图切的是 `[NvS, H]`，所以返回形状精确等于 NvS；权重输出 `torch.empty(NvS, dtype=float32)`。零拷贝模式则直接返回通信缓冲视图（u6-l2 详讲风险）。
- [moonep/api.py:L782-L795](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L782-L795) —— `plan is None` 时才要求路由输入并走规划；传 `plan` 则跳过规划、`cu_seqlens = None`（第 792-794 行）——这就是 combine bwd 的「免规划再派发」。

**(2) prefetch_weight 的权重契约**：

- [moonep/api.py:L878-L886](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L878-L886) —— 三个权重张量都是 `[E+B, H, H']` 连续张量：行 `[0, E)` 是源专家权重（其中每个 rank 自己的 E/R 行物理上就是本机参数内存），行 `[E, E+B)` 是本次调用填充的预取槽。bf16 为标准路径，uint8 对应 MXFP4 量化（此时 H′ 语义变化，u5-l2 详讲）。
- [moonep/api.py:L903-L907](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L903-L907) —— 断言 dtype 合法、连续、`ndim==3` 且第 0 维恰为 `E+B`——形状契约由构造期的符号锁死。

**(3) combine 的形状契约与对偶性**：

- [moonep/api.py:L961-L996](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L961-L996) —— docstring 点明双重身份：combine fwd 把专家输出 K 求和回 `[S, H]`；dispatch bwd 则是把每 token 的 K 份梯度拷贝累加回 token-major——**同一个内核，两种用途**，因为「K 份累加回一」的语义完全相同。
- [moonep/api.py:L1001-L1008](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1001-L1008) —— 入口断言：`hidden_nvsh` 必须 bf16、连续、形状恰为 `(NvS, H)`；可选的 `route_weights_nvs` 必须为 `(NvS,)` fp32。方向与 dispatch 的输出严格镜像。
- [moonep/api.py:L1022-L1036](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1022-L1036) —— 输出分配：`hidden_sh = torch.empty(S, H, bf16)`，可选 `route_weights_sk = torch.empty(S, K, fp32)`——形状回到 token-major，一次前向的形状之旅闭环。

**(4) reduce_grad 的梯度契约**（训练专属，本讲只看形状）：

- [moonep/api.py:L1107-L1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1107-L1113) —— 梯度镜像权重布局但强制 fp32：`full_*_grad` 为 `[E+B, H, H']`，其中行 `[E, E+B)` 由 `[R, B, H, H']` 的 reduce buffer 支撑——预取槽梯度必须与参数梯度隔离，避免混入框架自身的梯度归约（u5-l3 详讲）。

**(5) cu_seqlens 的分配确认**：

- [moonep/planning.py:L1221](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1221) —— `cu_seqlens = torch.empty(_round4(E + B), dtype=int32)[:E + B]`：形状 `[E+B]`、int32、设备张量，与 dispatch docstring 一致（`_round4` 的过分配技巧是为向量化写尾部的安全余量，返回形状不变）。

**(6) 权重缓冲区的官方图解说明**：

- [README.md:L45-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L45-L59) —— 「one contiguous symmetric-memory weight tensor per expert projection, plus a planner-produced cu_seqlens」是 MoonEP 与训练框架的全部契约：框架提供连续的 `[E+B, H, H']` 权重与按 `cu_seqlens` 消费它的组 GEMM，其余通信细节全部由 MoonEP 内部消化。

#### 4.3.4 代码实践

**实践三：纸上数据流 —— 从 `[S, H]` 走回 `[S, H]`（源码阅读型实践，无需 GPU）**

1. **实践目标**：不看本讲表格，仅凭源码 docstring 独立推导一次「dispatch → prefetch → FFN → combine fwd → combine bwd → dispatch bwd → reduce_grad」完整序列中每个张量的形状/dtype，训练按源码注释核对的习惯。
2. **操作步骤**：
   - 只打开 [moonep/api.py:L10-L47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L10-L47) 的用法示例与四个入口的 docstring（L733-L781、L871-L896、L960-L996、L1096-L1119），在纸上为每个变量标注 `(形状, dtype)`。
   - 特别标注三处「形状不变」：专家 FFN 前后都是 `[NvS, H]`；`zero_copy=True` 时 dispatch 返回的就是通信缓冲视图本身；combine bwd 再派发的输出与 fwd 的 `hidden_nvsh` 同形。
   - 再标注两处「形状还原」：combine 输出回到 `[S, H]`；`gathered_route_weights_sk` 回到 `[S, K]`。
3. **需要观察的现象**：整条流水线里**唯一**形状不静态可知的量是什么？（提示：它是数据不是形状。）
4. **预期结果**：唯一动态的量是 `cu_seqlens` 的**内容**（每分段的真实边界），而它的形状 `[E+B]` 也是构造期常量；整条链路上没有任何张量形状依赖路由结果。若你在纸上写出了任何一个形状含 \( T_e \)（某专家的真实 token 数）的张量，说明把「数据」误当成了「形状」，请回看 4.3.2 的对比。本实践结论可通过与 README 的 dispatch fwd 注释段（[README.md:L83-L104](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L83-L104)）对照验证。

#### 4.3.5 小练习与答案

**练习 1**：`combine` 的输入为什么必须是 expert-grouped 的 `[NvS, H]`，而不能直接收 `[S, K, H]` 或 `[S, H]`？

**答案**：专家 FFN 是按分段对权重 `[E+B, H, H']` 的行做组 GEMM 的，其输入输出天然按专家分段排布；K 份拷贝散布在不同 rank 的不同分段里，只有 `plan.dst` 记录了「token t 的第 k 份落在哪个槽位」。combine 内核正是按 `plan.dst` 逐份取回并 fp32 累加（[moonep/api.py:L690-L696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L690-L696) 调用 `launch_combine(ctx, hidden_sh, plan.dst, ...)`），所以输入必须保持 expert-grouped。

**练习 2**：`buffer.dispatch(grad_output_sh, plan=plan)`（combine bwd）返回的 `cu_seqlens` 是什么？为什么？

**答案**：`None`。传 `plan` 走复用路径，规划被整体跳过（[moonep/api.py:L792-L794](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L792-L794)），不再生成新的 `cu_seqlens`；反向重排用的是前向保存的同一个 plan，也无需预取（README「combine bwd」段：planning is skipped and no prefetch is needed）。

**练习 3**：静态形状消除了哪种同步？哪些同步仍然存在？

**答案**：消除了「把每专家 token 数拷回主机以决定张量形状/GEMM 维度」的**逐层宿主同步（隐式 D2H）**及随之而来的动态分配。仍存在的是设备端同步：内核间依赖（PDL/同流串行）、跨 rank 屏障（`inter_rank_sync`、combine 入口屏障），以及 `async_finish=True` 时用户读取结果前必须 `event.synchronize()` 的显式同步点——但这些都不阻塞 CPU 对形状的决策。

## 5. 综合实践

**综合实践：编写 `shapes_tour.py` —— 一张可执行的符号核对表**

这个脚本把本讲三个模块串起来：独立实现 `_create_context` 的全部尺寸公式，打印符号表、缓冲维度与四入口的输入输出形状，并内置自检断言。纯 CPU 可运行（不 import moonep、不需要 GPU），公式旁注明对应源码行号，便于逐项核对；有硬件时可扩展为真实对拍。

```python
# shapes_tour.py —— 示例代码：独立复现 _create_context 尺寸计算并核对（纯 CPU 可运行）
# 每行公式后括号内是对应源码位置（moonep/api.py @ 2bd860b）。

def align_up(x, a):                       # (_align_up, api.py L77-L79)
    return ((x + a - 1) // a) * a

def shapes_tour(S, H, K, E, R, token_padding=128, vmm_gran=2 * 1024 * 1024):
    # ---- 模块一：符号派生 ----
    N = S * K                             # (L263)
    epn = E // R                          # (L269)
    assert E % R == 0                     # (L270)
    B = epn                               # B 默认值 (L273-L274)
    NvS_capacity = S * K                  # (L277)
    extra = (token_padding - 1) * 2 * epn # (L287)
    NvS = NvS_capacity + extra            # (L288)

    # ---- 模块二：物理对齐与 meta 布局 ----
    row_bytes = 2 * H                     # bf16 每行字节数 (buffer.py L100-L104)
    padded = align_up(NvS * row_bytes, vmm_gran)      # (pad_to_granularity, buffer.py L89-L92)
    NvS_padded = padded // row_bytes      # (pad_dim0_for_alignment, buffer.py L95-L110)

    N4 = align_up(N, 4)                   # (L324)
    TPE_OFF = align_up(NvS, 4)            # (L309)
    PLAN_OFF = align_up(TPE_OFF + R * E, 4)           # (L310)
    pout = 3*E*R + R*(E+B) + 2*R*(E+B) + B*R + 2*R    # (L311-L321)
    TOPK0_OFF = align_up(PLAN_OFF + pout, 4)          # (L325)
    ORDER_OFF, ORDER0_OFF = TOPK0_OFF + N4, TOPK0_OFF + 2*N4   # (L326-L327)
    BARRIER_OFF = ORDER0_OFF + N4         # (L328)
    SRC_INFO_OFF = BARRIER_OFF + 3        # (L332, BARRIER_SLOTS=3)
    meta_chunk_logical = SRC_INFO_OFF + NvS           # (L333)
    meta_chunk_padded = align_up(meta_chunk_logical * 4, vmm_gran) // 4  # (L339-L340)

    # ---- 模块三：四入口输入输出形状 ----
    io = {
        "dispatch.in hidden_sh":        ((S, H), "bf16"),
        "dispatch.in route_weights_sk": ((S, K), "fp32"),
        "dispatch.in topk_experts_sk":  ((S, K), "int32"),
        "dispatch.in tokens_per_expert":((E,), "int32"),
        "dispatch.out hidden_nvsh":     ((NvS, H), "bf16"),
        "dispatch.out route_weights_nvs": ((NvS,), "fp32"),
        "dispatch.out cu_seqlens":      ((E + B,), "int32"),
        "prefetch.in full_*_weight":    ((E + B, H, "H'"), "bf16"),
        "combine.in hidden_nvsh":       ((NvS, H), "bf16"),
        "combine.out hidden_sh":        ((S, H), "bf16"),
        "combine.out route_weights_sk": ((S, K), "fp32"),
        "reduce_grad.in full_*_grad":   ((E + B, H, "H'"), "fp32"),
        "reduce_grad.in *_reduce_buffer": ((R, B, H, "H'"), "fp32"),
    }

    # ---- 打印 + 自检 ----
    print(f"S={S} H={H} K={K} E={E} R={R} token_padding={token_padding}")
    print(f"N={N} epn={epn} B={B} NvS={NvS} NvS_padded={NvS_padded}")
    print(f"hidden_buf=[{R*NvS_padded}, {H}] bf16 "
          f"({R*NvS_padded*row_bytes/2**30:.2f} GiB)")
    print(f"meta_chunk_logical={meta_chunk_logical} meta_chunk_padded={meta_chunk_padded}")
    print(f"meta_buf=[{R*meta_chunk_padded}] int32")
    for k, v in io.items():
        print(f"  {k:34s} {v[0]} {v[1]}")
    assert NvS == N + (token_padding - 1) * 2 * epn
    assert NvS_padded * row_bytes % vmm_gran == 0
    assert meta_chunk_padded * 4 % vmm_gran == 0
    assert (R - 1) * NvS_padded + NvS - 1 < 2**31 - 1   # (L351-L356)
    assert (R - 1) * meta_chunk_padded + meta_chunk_logical - 1 < 2**31 - 1  # (L344-L350)

shapes_tour(S=4096, H=7168, K=8, E=256, R=8)
```

**操作步骤与核对要点**：

1. 保存为 `shapes_tour.py`，直接 `python shapes_tour.py` 运行（无任何依赖）。
2. 与讲义 4.2.3 数值表逐项核对：`N=32768`、`B=32`、`NvS=40896`、`NvS_padded=40960`、`meta_chunk_logical=195475`、`meta_chunk_padded=524288`（后两项按 2 MiB 粒度假设）。
3. 调用 `shapes_tour(128, 512, 4, 32, 4, token_padding=32)` 复跑，与 4.1.4 手算结果对拍。
4. （可选，需多 GPU + NVLink，**待本地验证**）把 `token_padding` 换成 64 等非常规值，`torchrun --nproc_per_node=8` 下真实构造 `Buffer`，观察 `_log_context_buffer_size` 打印的 `NvS=` / `NvS_padded=`（[moonep/api.py:L143-L154](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L143-L154)）与脚本输出是否一致；注意脚本假设的 2 MiB 粒度需与 `moonep.buffer.get_vmm_granularity()` 的实际返回对照。

**预期结果**：所有断言通过；你得到一张「给定配置 → 全部形状」的自动对照表，此后阅读任何内核代码遇到形状疑问，改一行参数即可重算。

## 6. 本讲小结

- **符号即契约**：S/K/E/R/B/H/H′ 是用户与库的接口常量；派生量满足 \( N = S\times K \)、\( epn = B_{\text{default}} = E/R \)、\( NvS = S\times K + (\text{token\_padding}-1)\times 2E/R \)，全部在 `_create_context` 构造期一次算定。
- **两个 NvS 要分清**：`NvS`（40896）决定用户可见的 `[NvS, H]` 输出；`NvS_padded`（40960）只是 hidden_buf 每 rank chunk 的 VMM 对齐物理行数，用户不可见。
- **meta_buf 是一根 int32 长条**：从偏移 0 起依次切出 weights / tpe / plan / topk0 / order / order0 / barrier / src_info 八个区段，起点 16 字节对齐，chunk 字节同时满足 VMM 与组播双重对齐。
- **形状流转是 `[S,H] → [NvS,H] → [S,H]` 的闭环**：dispatch 打散成 expert-grouped，FFN 原地变换，combine 按 `plan.dst` K 求和归并回 token-major；`cu_seqlens [E+B]` 是分段边界的设备侧编码。
- **静态形状是性能命题**：接收量恒为 S×K 把每专家波动 \( T_e \) 从「形状」降为「设备上的数据」，逐层宿主同步与动态分配归零，maxvio 不再影响迭代时间与显存（README 的 e2e 对比结论）。
- **`_create_context` 是后续所有讲义的地基**：ctx 字典里的每个 key（缓冲、偏移、符号）都会在 u2/u3/u4 的内核代码中反复出现。

## 7. 下一步学习建议

本讲只回答了「缓冲多大、形状如何流转」，刻意回避了两个更深的「为什么」：

1. **下一讲 u2-l2（CUDA VMM 与 NVLink 对称内存）**：`create_nvl_dist_tensor` 如何用 `cuMemCreate`/`cuMemMap` 把 8 个 rank 的显存拼成一块连续虚拟地址？VMM 粒度从哪来？为什么 `hidden_buf[r*NvS_padded:]` 能直接读到别的 rank？——把本讲的「pad 到粒度」从结论变成机制。
2. **u2-l4（组播视图与 meta_buf 共享布局）**：本讲 4.2.3 只列了 meta 八个区段的偏移，那一讲解释组播对象如何叠加其上、rank0 的规划结果如何一次写全 rank，以及 `SRC_INFO` 的编码。
3. **单元三（u3 系列）**：`cu_seqlens` 的内容是怎么算出来的——surplus/deficit 平衡、top-B 专家选择、二分找槽位，全部围绕本讲的符号展开。
4. 阅读源码顺序建议：先重读 [moonep/api.py:L222-L437](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L222-L437)（`_create_context` 全文）直到每行都能对应本讲某个数字，再进入 u2-l2；带着「这块显存怎么映射的」问题去读 buffer.py 会顺畅得多。
