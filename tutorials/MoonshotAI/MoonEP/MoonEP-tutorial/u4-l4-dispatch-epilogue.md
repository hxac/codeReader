# dispatch epilogue：本地重复展开

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 dispatch 内核结束后本地 NVL shard 上为什么留有「空洞」，以及 `DispatchEpilogueKernel` 如何用主行把这些空洞补齐，从而让 shard 成为完整的用户可见 `[NvS, H]` 布局。
2. 读懂 epilogue 的双 warp 流水线：warp 0 用 `cp.async.bulk` G2S 把一批主行搬进 smem，warp 1 把同一份 smem 数据 S2G 扇出到每个重复组的全部重复槽。
3. 理解启动几何的设计取舍：grid 恒等于 `num_sms_dedup`、组批次 round-robin 分派给 CTA、`_B_CANDIDATES` 为什么从大到小尝试且优先保住双缓冲。
4. 理解 `dup_counts[0]` 为什么必须在设备端读取，以及这一选择如何让整个 dispatch 前向保持「零宿主同步」。
5. 理解 epilogue 与 dispatch 的流水衔接：同流先后启动 + PDL（Programmatic Dependent Launch）可选加速，以及为什么 epilogue 自己不需要跨 rank 屏障。
6. 独立写出 epilogue 的 PyTorch 参考实现，并用随机重复组验证它与「逐组 copy」语义等价。

## 2. 前置知识

本讲是 u4 单元（通信内核）的第四篇，直接建立在前三讲之上。先用通俗语言回顾必须的概念。

### 2.1 负数 dst 与「主行 / 重复槽」

u3-l5 讲过去重编码：同一个 token 的多个 top-k 条目落到**同一个目的 rank** 时，只有最小 k 的那条会真正经 NVLink 拷贝 payload（这一行叫**主行**，primary row），其余条目的 dst 被规范化为 `-raw_dst - 1` 的负数编码——只散射路由权重，不拷 payload。于是在接收 rank 的本地 NVL shard（`hidden_buf_local`，形状 `[NvS, H]`）上，这些**重复槽**（duplicate slots）在 dispatch 内核结束时是没有任何人写入的。

### 2.2 去重三件套

u4-l3 讲过 dispatch 内核的 builder warps（fresh planning 路径）如何从 `src_info` 物化三个 plan 持有的张量：

- `dup_groups`，形状 `[NvS, 3]`：每行是一个组头三元组 `(primary_loff, dup_start, dup_n)`——主行在 shard 内的行号、该组的重复槽在 `dup_loffs` 里的起始下标、重复槽个数。
- `dup_loffs`，形状 `[NvS,]`：所有重复槽行号的紧凑扁平列表（**不含主行本身**）。
- `dup_counts`，形状 `[2,]`：`[有效组数, dup_loffs 有效长度]`。

只有紧凑前缀有效；组间顺序由 builder 的 atomicAdd 到达序决定，**不保证稳定**。plan 复用路径（`build_dedup_map=False`）不重建它们，直接沿用保存的快照。

### 2.3 两种异步拷贝的完成机制

u4-l1 讲过 `cp.async.bulk` 家族的两种完成跟踪方式，本讲两个 warp 各用一种：

- **G2S（global → shared）** 走 mbarrier 事务计数：发起前 `expect_tx` 声明字节数，每笔拷贝完成自动向 mbarrier「记 N 字节」，消费者等到「到达数 + 字节数」双条件满足即知数据就绪。
- **S2G（shared → global）** 走 bulk_group 编组：`commit_group` 把之前的 S2G 打包成一组，`wait_group<N>` 等到只剩 N 组未完成，没有 mbarrier 参与。

两者都是**单线程指令**，只能由一个 warp 的 lane 0 发射。

### 2.4 PDL（Programmatic Dependent Launch）

u4-l1 与 u4-l2 讲过 PDL 的两个原语：前驱内核末尾调 `griddepcontrol.launch_dependents`（提前放行后继的启动），后继内核开头调 `griddepcontrol.wait`（等前驱**整个网格**结束并冲刷写入后才继续）。效果是后继的「启动 + 开场准备」与前驱的收尾重叠，省掉一次内核启动空隙。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [moonep/dispatch_epilogue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py) | **本讲主角**。`DispatchEpilogueKernel` 设备内核 + `launch_dispatch_epilogue` 宿主封装，全文件仅 416 行，在七个通信内核里行数第二少（仅次于 inter_rank_sync 的 155 行） |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 调用编排：`_run_dispatch_on_current_stream` 里 dispatch → epilogue 的先后关系；`num_sms_dedup` 的解析（`MOONEP_NUM_SMS_DEDUP`） |
| [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) | 本讲用到的共享 PTX 助手：`cp_async_bulk_g2s` / `cp_async_bulk_s2g`、`pdl_wait_predecessor` / `pdl_trigger_dependents` |
| [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py) | 上游内核：consumer 对负 dst 跳过 payload 的代码点、出口 `cross_rank_barrier` + `pdl_trigger` 的衔接点 |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 去重三件套的契约定义（`MoonEPCommPlan` 字段注释）与分配 |
| [tests/test_dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py) | epilogue 的真实调用范本与语义校验（含 plan 复用路径、非法 plan 负例） |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | 去重结构的 PyTorch 参考构建（本讲实践的语义依据） |

## 4. 核心概念与源码讲解

本讲的最小模块是 `DispatchEpilogueKernel`。我们按「问题与总体设计 → 启动几何 → 设备内核 → 宿主封装」四步拆开。

### 4.1 问题与总体设计：dispatch 之后为什么要本地补行

#### 4.1.1 概念说明

回顾 u3-l5 的核心权衡：去重的目的是**发送端省 NVLink 带宽**——同一 token 落到同一目的 rank 的多个 top-k 条目只传一份 payload。但代价是接收端的 shard 上留下空洞：分组 GEMM 按 `cu_seqlens` 整段读取 shard，每个 token-topk 条目都必须在 shard 里有一行完整数据，空洞里是上一轮的脏数据。

`DispatchEpilogueKernel` 就是补洞的人：它读取 plan 持有的去重三件套，对每个重复组，把主行（dispatch 唯一真正写入的那份）**在本地显存内**复制到该组的所有重复槽。关键定位有三条：

1. **纯本地**。主行已经在本地 shard 里，重复槽也在本地 shard 里，整个内核不发生任何跨 rank 通信——省下的是 NVLink 带宽，付出的是一次本地显存读写。这就是模块文档里「Runs entirely on local memory — no cross-rank communication」的含义。
2. **不涉及用户张量**。它原地修改 `hidden_buf_local`（NVL shard 本体）。`zero_copy=False` 时的边界拷贝是宿主侧普通的 `tensor.copy_`，`zero_copy=True` 时直接把 shard 视图返回给用户——所以对用户来说，**epilogue 跑完之前 shard 不是完整数据**。
3. **不需要自己的跨 rank 屏障**。它读的数据（主行）由 dispatch 的出口 `cross_rank_barrier` 发布；它写的数据（重复槽）只被本地 GEMM 消费，或等到 combine 的入口屏障才需要可见。

#### 4.1.2 核心流程

一次 dispatch 前向（`zero_copy=False`）中 epilogue 的位置：

```text
launch_dispatch（同一条流）
  ├─ 负 dst 条目：只散射权重，不拷 payload → 重复槽留空
  ├─ builder warps：物化 dup_groups / dup_loffs / dup_counts（fresh 路径）
  └─ 出口 cross_rank_barrier：把全网格的 NVL 写发布给 peer rank
      └─（可选）pdl_trigger_dependents：放行 epilogue 提前启动
launch_dispatch_epilogue（同一条流，可选 PDL 衔接）
  ├─ 读 dup_counts[0] 得有效组数 n_groups（设备端）
  ├─ 对每个组 g：主行 dup_groups[g][0] → smem → 扇出到
  │   dup_loffs[ dup_groups[g][1] : + dup_groups[g][2] ] 的每个槽
  └─ 排空 S2G bulk_group 后内核结束
宿主侧边界拷贝（zero_copy=False）：
  hidden_nvsh.copy_(hidden_buf_local)   # 普通拷贝，不在内核里
```

每个组的数据量是 \( H \times 2 \) 字节（bf16），扇出后的写总量为 \( n_{\text{groups}} \) 个组各自 \( (1 + \text{dup\_n}_g) \) 次行读写的本地流量；相比不去重时经 NVLink 传 \( \sum (1+\text{dup\_n}_g) \) 份，本地扇出把 \( \text{dup\_n}_g \) 份的传输从 NVLink 挪到了本地显存。

#### 4.1.3 源码精读

先看模块文档，它是全文件的「设计说明书」：

[moonep/dispatch_epilogue.py:1-18](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L1-L18) —— 模块 docstring，说明了四件事：epilogue 做什么（主行经 smem 一次暂存、存到同 shard 的每个重复槽）；不涉及用户张量（`zero_copy` 的边界拷贝在宿主侧）；padding 行已由 dispatch 的 zero warp 清零、路由权重已由 consumer 散射（所以 epilogue 只需管 payload）；以及为何不需要跨 rank 屏障（读的数据由 dispatch 出口屏障发布，写的数据本地消费）。

[moonep/dispatch_epilogue.py:44-61](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L44-L61) —— 类 docstring，给出本讲最重要的三句设计描述：双 warp 镜像 dispatch 数据通路；plan 持有的去重结构驱动全部工作、`dup_counts[0]` 在设备端读取（no host synchronization）；组批次 round-robin 分派（batch `i` = 组 `[i*B, i*B+B)` → block `i % num_sms`），因此 grid 必须等于 `num_sms`。

再看上游「留洞」的确切代码点。[moonep/dispatch.py:408-427](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L408-L427) —— dispatch consumer 逐 k 解码 dst：`store_token = dst_val >= 0`，负值先还原 `raw_dst = -dst_val - 1` 再拆出 `drank`/`loff`，但只有 `store_token` 为真才发 S2G 拷 payload；权重散射则无论正负都执行。这正是「重复槽留空、权重照常到位」的出处。

[moonep/dispatch.py:683-694](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L694) —— dispatch 内核的收尾：先 `cross_rank_barrier`（网格栅障 + 系统级 release/acquire 原子握手）把本网格对 NVL shard 的写发布出去，然后（若 `pdl_trigger` 为编译期真）调 `pdl_trigger_dependents(tidx)` 放行后继。epilogue 读的主行可能来自远端 rank 的 NVLink 写入，可见性就靠这道屏障保证。

最后看编排层。[moonep/api.py:643-653](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L643-L653) —— `_run_dispatch_on_current_stream` 的收尾三步：`launch_dispatch(..., build_dedup_map=planning_args is not None, pdl_trigger=self.enable_pdl)` → `launch_dispatch_epilogue(ctx, plan, pdl_launch=self.enable_pdl)` →（`zero_copy=False` 时）`hidden_nvsh.copy_(ctx['hidden_buf_local'])`。两个内核共享同一条当前流，先后顺序由流语义保证；PDL 只是让「epilogue 的启动」不必等「dispatch 的完全退出」才开始。

#### 4.1.4 代码实践

**实践目标**：从测试口径确认「epilogue 的正确性 = 负数 dst 条目也能在 shard 上读到正确 payload」。

**操作步骤**：

1. 打开 [tests/test_dispatch.py:209-231](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L209-L231)（`_verify_dispatch_by_dst` 的开头）。注意第 228 行：`raw_dst = -dst_val - 1 if dst_val < 0 else dst_val`——校验循环对**每个** dst 条目（包括负数）解出 `local_off` 并检查 `hidden_i16[local_off]` 的内容必须等于源 token 的可追溯标记。
2. 再看 [tests/test_dispatch.py:169-192](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L169-L192)（`_plan_and_dispatch` 的收尾）：`launch_dispatch(ctx, ..., build_dedup_map=True)` 之后紧跟 `launch_dispatch_epilogue(ctx, plan)`，然后才把 shard 拷进用户张量做校验。
3. 画出依赖链：dispatch 只保证非负条目落位 → 负数条目校验要成立，唯一的写入者就是 epilogue。

**需要观察的现象**：校验函数对负数条目不做任何跳过或特殊放宽——它们与非负条目走完全相同的比对逻辑。

**预期结果**：若注释掉 `launch_dispatch_epilogue` 那一行（仅作思想实验，不要真改源码），负数条目的 `local_off` 处将读到上一轮的脏数据，`actual_s != expected_s` 报错。真实运行需多卡 NVLink 环境：`torchrun --nproc_per_node=R -m pytest tests/test_dispatch.py`（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：epilogue 为什么不像 dispatch 那样在出口做 `cross_rank_barrier`？

**答案**：它写的重复槽只有两类消费者——本地分组 GEMM（同流后继，流顺序保证可见），以及 combine 阶段（combine 自己的入口屏障会发布这些写）。它读的主行可见性由 dispatch 的出口屏障负责。没有跨 rank 数据依赖就没有屏障开销，模块 docstring 明确写了这一点。

**练习 2**：`zero_copy=True` 时用户拿到的是 shard 视图。如果用户在 epilogue 结束前就读这个视图会发生什么？

**答案**：读到的重复槽是脏数据。不过实际上读不到——epilogue 与后续用户操作在同一条流上，流语义串行化了访问。真正的风险是 u6-l2 会讲的「视图被后续通信覆盖」，那是跨调用生命周期问题，不是本内核的问题。

**练习 3**：为什么说 epilogue 是「用本地带宽换 NVLink 带宽」？

**答案**：不去重时，同一 payload 需经 NVLink 传 \( 1+\text{dup\_n} \) 份；去重后 NVLink 只传 1 份，剩下 \( \text{dup\_n} \) 份由 epilogue 在本地显存内从主行复制。本地 HBM 带宽远高于跨卡 NVLink 有效带宽，且不占用卡间链路这一训练瓶颈资源。

### 4.2 启动几何：B/stages 选档与 round-robin 组批次

#### 4.2.1 概念说明

epilogue 的工作量由 `n_groups`（有效组数）决定，这是一个**运行期才知道的数据依赖量**（取决于路由碰撞）。内核把组组织成「批次」：每批 `B` 个组，批次 `i` 覆盖组 `[i·B, i·B+B)`，映射给 CTA `i % num_sms`。这个设计有两个自由度要定：

- **每批组数 B**。源码注释直白地给出了取舍依据：握手成本（每批一次 mbarrier 往返）占主导，流水深度超过 2 实测无感（"Handshake amortization dominates and pipeline depth beyond 2 is measurably irrelevant"），所以策略是**在还能保住双缓冲的前提下，选最大的 B**——批越大，摊到每个组的握手越少。
- **stage 数**。只需 ≥ 2（双缓冲）即可，多余深度不换性能。

但 B 有硬上限 32：消费 warp 要用「一个 lane 读一个组头 + shuffle 广播」的方式取批次内所有 `(dup_start, dup_n)`，32 个 lane 恰好覆盖 32 个组头，所以 `assert self.B <= 32, "batch size B must fit in one warp"`。

grid 大小则恒为 `num_sms_dedup`：它默认是设备 SM 数（`multi_processor_count`），但可被环境变量 `MOONEP_NUM_SMS_DEDUP` 覆盖——这是刻意做成环境变量而非 Buffer 参数的，为了让基准测试能扫描它而不污染公共 API。

#### 4.2.2 核心流程

选档算法（`_pick_geometry`）：

```text
for B in (32, 16, 8, 4, 2, 1):            # 从大到小
    s = 使 smem_bytes(H, s, B) ≤ budget 的最大 s ∈ (16,14,...,2)
    if s ≥ 2: return (B, s)               # 保住双缓冲即收工
return (1, pick_stages(H, budget, 1))     # 兜底：B=1，深度随缘（可能为 0 → 报错）
```

smem 需求公式（bf16 行数据 + 每 stage 两个 i64 mbar + 256 B cutlass 头部余量）：

\[ \text{smem} = \mathrm{roundup}_{128}(\text{stages} \times B \times H \times 2) + \mathrm{roundup}_{16}(\text{stages} \times 2 \times 8) + 256 \]

round-robin 分派（两个 warp 用同一套游标）：

```text
gi 起始 = bidx * B
while gi < n_groups:
    n = min(B, n_groups - gi)        # 尾批部分填充
    处理组 [gi, gi + n)
    gi += num_sms * B                # 跳过其他 CTA 的批次
```

即 block `b` 处理批次 `b, b+num_sms, b+2·num_sms, ...`。批次数为 \( \lceil n_{\text{groups}} / B \rceil \)。举例：`num_sms=8, B=8, n_groups=100` → 13 批（批 12 只含组 96..99，`n=4`）；block 4 分到批次 4 与 12，block 5/6/7 只有批次 5/6/7——负载天然均衡，且**不需要任何调度中枢**，每个 CTA 用 `bidx` 自己算出自己的批次序列。

#### 4.2.3 源码精读

[moonep/dispatch_epilogue.py:63-71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L63-L71) —— 几何常量：`num_threads = 64`（两个 warp）、`PRODUCER_WARP = 0`、`CONSUMER_WARP = 1`、`_B_CANDIDATES = (32, 16, 8, 4, 2, 1)`、`_MIN_STAGES = 2`，以及那段「握手摊销主导、深度 >2 无感」的取舍注释。

[moonep/dispatch_epilogue.py:73-92](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L73-L92) —— `__init__`：调用 `_pick_geometry` 定型 `(B, stages)`；第 87 行是 B ≤ 32 的 warp 容量断言；若兜底后 `stages == 0`（H 大到连 B=1 双缓冲都放不下），抛出带 smem 数字提示的 `RuntimeError`。

[moonep/dispatch_epilogue.py:94-105](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L94-L105) —— `_smem_bytes` 静态方法，即上面的公式：`round_up(stages*B*H*2, 128)` 的行数据（bf16 每元素 2 字节，128 字节对齐与 TMA 对齐习惯一致）+ `round_up(stages*2*8, 16)` 的 mbar（每 stage full/empty 两个 i64）+ 256 字节余量。

[moonep/dispatch_epilogue.py:107-120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L107-L120) —— `_pick_stages` 按 `(16,14,12,10,8,6,4,2)` 降深度试探；`_pick_geometry` 按 B 候选降序调用它，第一个 `s ≥ 2` 的组合即返回；全失败则退 `(1, …)`。

grid 数量的来源在 api 侧。[moonep/api.py:82-101](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L82-L101) —— `_num_sms_dedup_from_env`：读取 `MOONEP_NUM_SMS_DEDUP`，未设或空串则返回 `max_sms`；否则要求整数值落在 `[1, max_sms]`，越界抛 `ValueError`。docstring 写明它刻意是环境变量而非构造参数。[moonep/api.py:266-267](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L266-L267) —— `max_sms` 取自 `torch.cuda.get_device_properties(device).multi_processor_count`，结果存入 `ctx['num_sms_dedup']`（[moonep/api.py:403](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L403)），最终被 [moonep/dispatch_epilogue.py:383](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L383) 的 `num_sms = int(ctx['num_sms_dedup'])` 消费为 grid 大小。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 复现 `_pick_geometry` 的选档逻辑，验证「大 H 时自动降 B 保双缓冲」。

**操作步骤**：

1. 新建 `epilogue_geometry.py`（示例代码，放在仓库外或临时目录均可），把 `_smem_bytes` / `_pick_stages` / `_pick_geometry` 三个函数照抄下来（它们是纯 Python 静态方法，不依赖任何 GPU 库）。
2. 以 H100 级设备的 optin 上限 227 KiB（232448 B）为例：`smem_budget = 232448 - 1024`（对应 [_get_compiled](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L347) 里的 `optin - 1024`），扫描 `H ∈ {7168, 4096, 2048, 512, 64}`，打印每个 H 的 `(B, stages)` 与 smem 字节数。
3. 手工核对 H=7168 这一行。

**需要观察的现象**：H=7168 时，`B=32/16` 连 `stages=2` 都放不下（`2×16×7168×2 = 458752 > 231424`）；`B=8` 时 `stages=2` 的需求为 `roundup(2×8×7168×2, 128) + roundup(32, 16) + 256 = 229376 + 32 + 256 = 229664 ≤ 231424`，恰好通过。

**预期结果**：H=7168 → `(B=8, stages=2)`；H 越小 B 越可能停在 32（例如 H=64 时 `B=32, stages=16` 也放得下，但按 `_pick_stages` 顺序会先命中 `stages=16`）。不同设备 optin 上限不同，精确数字**待本地验证**（在真机上打印 `torch.cuda.get_device_properties(i).shared_memory_per_block_optin` 即可）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_B_CANDIDATES` 止步于 32？换成 64 会破坏什么？

**答案**：消费 warp 的头读取协议是「lane `j` 读批次内第 `j` 个组的 `(dup_start, dup_n)`，`shuffle_sync(idx)` 广播」。一个 warp 只有 32 个 lane，B=64 时第 33~64 个组头没人读，`__init__` 里的断言会在构造期直接拦下。

**练习 2**：round-robin 分派相比「静态连续划分」（block 0 独占组 `[0, n/num_sms)`）好在哪？

**答案**：不同组的工作量差异很大（`dup_n` 从 1 到 K-1 不等），静态连续划分容易让某个 CTA 分到一堆大组成为尾部长尾。round-robin 把批次交错散给所有 CTA，组间方差被平均；且实现零同步——每个 CTA 只凭 `bidx` 与步长 `num_sms*B` 就知道自己的全部批次，不需要先知道 `n_groups` 的宿主值。

**练习 3**：`MOONEP_NUM_SMS_DEDUP=1` 时内核还能正确运行吗？性能会怎样？

**答案**：能。round-robin 公式退化为「唯一的 CTA 顺序处理所有批次」（`gi` 从 0 起步、步长 `1*B`），正确性不依赖 grid 大小；性能上失去并行度，全部扇出串行在一个 SM。这也是基准测试要扫描它的原因——量化 dedup 内核占用多少 SM 才不影响同流的其他工作。

### 4.3 设备内核精读：双 warp G2S→S2G 流水线

#### 4.3.1 概念说明

内核只有 64 个线程，两个有效角色，与 dispatch 数据通路同构但极简：

- **warp 0（生产者）**：按批次把 `B` 个主行从 gmem 用 G2S 拷进当前 stage 的 smem，一笔批次一次 `expect_tx(n·H_BYTES)` + `n` 笔 `cp.async.bulk`，等 mbarrier 事务计数收齐即「满」。
- **warp 1（消费者）**：等 stage「满」，把每个暂存行 S2G 扇出到它那组的全部重复槽；S2G 用 bulk_group 跟踪完成，批次末 `commit_group`，在途组数压到 `stages-1` 以内才释放该 stage 给生产者复用。

两个要点值得先想清楚：

1. **主行只从 gmem 读一次**。不管一个组扇出到多少个重复槽，gmem → smem 只有一笔 `H_BYTES` 的 G2S；所有扇出都从 smem 走。这在带宽上是最优的：本地读 \( n_{\text{slots}} \) 次、远端读 1 次。
2. **stage 的释放必须等 S2G 完成**。S2G 是异步的，发起后 smem 里的数据还在被拷贝引擎读；如果消费者一发起就释放 stage，生产者可能覆写尚未读完的 smem。所以释放被刻意延迟：消费者保持「在途 bulk_group ≤ stages-1」的节流，`wait_group(stages-1)` 保证被释放的那个 stage 的 S2G 已完成。这和 dispatch 内核「延迟 stages−1 个 token 才 release」是同一个模式（u4-l2）。

smem 的布局是 `(B, H, stages)` 三维张量、order `(1, 0, 2)`——stage 维最慢、batch 内行号维最快，于是第 `s` 个 stage 的 `B×H` 块整体连续，第 `idx` 行的地址就是 `base + s·B·H + idx·H`，与两个 warp 的地址算式严格对应。

#### 4.3.2 核心流程

两个 warp 的伪代码（`gi` 游标完全同步推进）：

```text
共同量：n_groups = dup_counts[0]            # 设备端读取！
        gi₀ = bidx * B，步长 = num_sms * B

warp 0（生产者，lane 0 发射）:
for gi = gi₀; gi < n_groups; gi += num_sms*B:
    n = min(B, n_groups - gi)
    wait stage[pi] empty                     # mbarrier 相位等待
    lane 0: mbarrier_arrive_and_expect_tx(mbar[pi], n * H_BYTES)
    for idx in 0..n:
        loff = dup_groups[(gi+idx) * 3 + 0]              # 主行行号
        cp.async.bulk G2S  smem[pi][idx] <- gmem[loff], H_BYTES
    advance pi

warp 1（消费者）:
for gi = gi₀; gi < n_groups; gi += num_sms*B:
    n = min(B, n_groups - gi)
    lane < n: (dup_start, dup_n) = dup_groups[(gi+lane)*3 + 1..2]   # 一 lane 一组头
    wait stage[ci] full
    for idx in 0..n:
        (cur_start, cur_n) = shuffle_sync(源 lane = idx)            # 广播组头
        lane 0:
          for k in 0..cur_n:
              slot = dup_loffs[cur_start + k]
              cp.async.bulk S2G  gmem[slot] <- smem[ci][idx], H_BYTES
          commit_group
    advance ci
    if issued ≥ stages-1:                  # 节流 + 延迟释放
        wait_group(stages-1); release stage[ri]; advance ri
    issued += 1
wait_group(0)                               # 收尾排空
```

时序上，生产者领先消费者至多 `stages` 个批次；消费者的 `use_state` 与 `rel_state` 相差 `stages-1`，恰好实现「第 `b` 批的 stage 在第 `b + stages - 1` 批处理完后才释放」。

#### 4.3.3 源码精读

宿主入口（`@cute.jit` 的 `__call__`）：[moonep/dispatch_epilogue.py:124-164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L124-L164) —— 把四个裸指针包装成 CuTe 张量（`hidden` 用 `(NvS, H)` 布局、`dup_groups` 摊平成 `NvS*3` 一维以便线性寻址），然后以 `grid=(num_sms,1,1)`、`block=(64,1,1)`、`smem=smem_bytes`、`cooperative=True`、`use_pdl=self.pdl_launch` 启动。H/NvS/stages/num_sms/B 全部是 `const_expr` 编译期常量——同一 `(H, NvS, num_sms, 设备, pdl)` 组合只编译一次。

内核开场：[moonep/dispatch_epilogue.py:168-208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L168-L208) —— 先做可选的 PDL 等待（第 182-183 行，`pdl_launch` 为编译期真时调 `pdl_wait_predecessor()`，等待 dispatch 网格完全结束并冲刷写入）；再分配 smem：`load_mbar`（`2*stages` 个 i64，full/empty 各一排）与 `stage_smem`（bf16，`(B, H, stages)`、order `(1,0,2)`、128 字节对齐）；然后建 `PipelineTmaAsync`，生产/消费两组都声明为「单线程」的 `CooperativeGroup(Agent.Thread, 1)`；最后第 208 行 `n_groups = dup_counts_tensor[0]` ——**在设备上读组数**，这一行的深意见 4.4。

warp 0 生产者：[moonep/dispatch_epilogue.py:210-247](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L210-L247) —— 先 `sync_object_empty.wait`（整 warp 等相位，第 224-226 行），随后仅 lane 0（`cp.async.bulk` 是单线程指令，第 227-228 行注释）做 `mbarrier_arrive_and_expect_tx(mbar, n * H_BYTES)` 并循环 `n` 次发 G2S：源地址 `hidden.iterator + Int64(loff) * Int64(H)`（注意用 Int64 乘法防 int32 回绕），目的地址 `stage_smem.iterator + state.index * B * H + idx * H`。游标推进 `gi += Int32(num_sms * B)` 即 round-robin 步长。

warp 1 消费者：[moonep/dispatch_epilogue.py:249-305](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L249-L305) —— 三段结构：

- 第 268-272 行，头预取：`if lane < n:` 时 lane 各自读 `(gi+lane)` 号组的 `dup_start`/`dup_n`（`dup_groups` 摊平寻址 `*3+1`、`*3+2`）。
- 第 274-291 行，`consumer_wait(use_state)` 等满后逐 `idx` 处理：`shuffle_sync(dup_start, Int32(idx))` 从 lane `idx` 把组头广播给全 warp（第 276-277 行），lane 0 双层循环 `for k in cur_dup_n`：`slot = dup_loffs[cur_dup_start + k]`，S2G 从 `smem[use_state.index][idx]` 写到 `hidden.iterator + Int64(slot) * Int64(H)`。**注意没有写主行的循环——主行是 dispatch 写的，`dup_loffs` 里也只存重复槽**。
- 第 292-305 行，完成跟踪：批次末 lane 0 `commit_group`；`issued ≥ stages-1` 时 `cp_async_bulk_wait_group(stages - 1)` 把在途组数压回界限、`consumer_release(rel_state)` 归还 stage、`rel_state` 前进；循环结束后 `cp_async_bulk_wait_group(0)` 排空尾部在途拷贝（第 305 行）。

PTX 助手本身在 _common.py：[moonep/_common.py:415-436](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L415-L436) 的 `cp_async_bulk_g2s` 对应 `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes`，完成时自动向 mbar 记 `size` 字节事务；[moonep/_common.py:439-460](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L439-L460) 的 `cp_async_bulk_s2g` 对应 bulk_group 形式，无 mbar，靠 commit/wait 配对。PDL 的另一端在 [moonep/_common.py:393-405](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L393-L405)：`pdl_trigger_dependents`（前驱用：同步 + `fence_acq_rel_sys` + `griddepcontrol_launch_dependents`）与 `pdl_wait_predecessor`（后继用：`griddepcontrol_wait`）。dispatch 末尾（dispatch.py:693-694）触发、epilogue 开头（182-183 行）等待，配对成一条 PDL 链。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：为 epilogue 写 PyTorch 参考实现，用随机重复组验证与「逐组 copy」语义等价——这正是测试方法论里「GPU 内核 ↔ PyTorch 参考对拍」的微缩版。

**操作步骤**：

1. 写参考实现（示例代码，CPU 即可运行）：

```python
# epilogue_reference.py（示例代码）
import torch

def dispatch_epilogue_reference(shard: torch.Tensor,
                                dup_groups: torch.Tensor,
                                dup_loffs: torch.Tensor,
                                dup_counts: torch.Tensor) -> torch.Tensor:
    """MoonEP DispatchEpilogueKernel 的 PyTorch 参考实现。

    shard:      [NvS, H] bf16，本地 NVL shard（主行已由 dispatch 写入）
    dup_groups: [NvS, 3] int32，紧凑前缀每行 (primary_loff, dup_start, dup_n)
    dup_loffs:  [NvS,]   int32，重复槽行号扁平列表
    dup_counts: [2,]     int32，[有效组数, dup_loffs 有效长度]
    返回展开后的 shard 副本（内核是原地写，这里拷贝一份以便对比）。
    """
    out = shard.clone()
    n_groups = int(dup_counts[0])
    for g in range(n_groups):
        primary_loff, dup_start, dup_n = (int(v) for v in dup_groups[g].tolist())
        slots = dup_loffs[dup_start:dup_start + dup_n]
        out[slots] = out[primary_loff]      # 主行 -> 每个重复槽
    return out
```

2. 写随机对拍脚本（示例代码）：

```python
# test_epilogue_reference.py（示例代码）
import torch

def make_random_case(NvS=256, H=32, max_dup=3, seed=0):
    g = torch.Generator().manual_seed(seed)
    shard = torch.randn(NvS, H, dtype=torch.bfloat16)
    used = torch.zeros(NvS, dtype=torch.bool)   # 模拟 dispatch：只写主行
    dup_groups, dup_loffs = [], []
    pos = 0
    for primary in torch.randperm(NvS, generator=g).tolist():
        if used[primary]:
            continue                            # 该槽已被先前组占用为重复槽
        free = (~used).nonzero().flatten().tolist()
        free = [i for i in free if i != primary]
        dup_n = int(torch.randint(0, max_dup + 1, (1,), generator=g))
        dup_n = min(dup_n, len(free))
        if dup_n == 0:
            continue                            # 无重复的组不进 dup_groups
        dups = free[:dup_n]
        used[dups] = True
        dup_groups.append([primary, pos, dup_n])
        dup_loffs.extend(dups)
        pos += dup_n
    n = len(dup_groups)
    groups_t = torch.zeros(NvS, 3, dtype=torch.int32)
    groups_t[:n] = torch.tensor(dup_groups, dtype=torch.int32)
    loffs_t = torch.zeros(NvS, dtype=torch.int32)
    loffs_t[:pos] = torch.tensor(dup_loffs, dtype=torch.int32)
    counts_t = torch.tensor([n, pos], dtype=torch.int32)
    return shard, groups_t, loffs_t, counts_t

def naive_group_copy(shard, groups, loffs, counts):
    """逐组 copy 语义：每组独立地把主行复制到自己的重复槽。"""
    out = shard.clone()
    for g in range(int(counts[0])):
        p, s, c = (int(v) for v in groups[g].tolist())
        for k in range(c):
            out[int(loffs[s + k])] = out[p]
    return out

for seed in range(100):
    shard, groups, loffs, counts = make_random_case(seed=seed)
    a = dispatch_epilogue_reference(shard, groups, loffs, counts)
    b = naive_group_copy(shard, groups, loffs, counts)
    assert torch.equal(a, b), f"seed={seed} mismatch"
    # 主行与未涉及槽必须原样保留
    assert torch.equal(a[groups[:int(counts[0]), 0]], shard[groups[:int(counts[0]), 0]])
print("100 个随机用例全部通过")
```

3. 语义对照：把参考实现的循环体与 [moonep/dispatch_epilogue.py:282-291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L282-L291) 的双层循环逐行对应——`for g in range(n_groups)` ↔ 外层 `gi` 游标循环；`out[slots] = out[primary_loff]` ↔ `slot = dup_loffs[cur_dup_start + k]` + S2G。真实内核语义依据见 [tests/planning_reference.py:281-290](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L281-L290)：参考 builder 也是把 `group_loffs[1:]`（不含主行）填进 `dup_loffs`。

**需要观察的现象**：100 个种子下向量化参考与朴素逐组 copy 完全一致；`dup_counts[0]` 之后的前缀区即使有垃圾数据也不影响结果（参考实现只读前缀）。

**预期结果**：脚本纯 CPU 可跑（无需 GPU/多卡），输出 `100 个随机用例全部通过`。我在本环境未执行，属**待本地验证**——但脚本只依赖 `torch.randn`/`randperm` 等稳定接口，行为确定。

#### 4.3.5 小练习与答案

**练习 1**：生产者 warp 里 `expect_tx` 为什么是 `n * H_BYTES` 而不是 `B * H_BYTES`？

**答案**：尾批只含 `n < B` 个组，生产者也只发 `n` 笔 G2S。mbarrier 的事务计数必须与实际发起的字节数严格相等，多声明会导致 stage 永远不满（消费者卡死），少声明会提前放行（读到未完成数据）。

**练习 2**：消费者的 `use_state` 和 `rel_state` 为什么要分开两个状态、相差 `stages-1`？

**答案**：`use_state` 追踪「正在读哪个 stage」，`rel_state` 追踪「可以归还哪个 stage」。S2G 异步发射后 smem 仍被拷贝引擎读，必须等 `wait_group(stages-1)` 确认该批次的 bulk_group 完成后才能归还；两个状态错开 `stages-1` 恰好把这段延迟流水化——归还不阻塞后续批次的消费，只限制生产者的领先距离。

**练习 3**：内核为什么用 `Int64(loff) * Int64(H)` 而不是 int32 乘法算行地址？

**答案**：`NvS × H` 在大模型配置下容易超过 \( 2^{31} \)（例如 NvS≈3.3 万、H=7168 时元素数约 2.4 亿尚可，但 bf16 字节地址再翻倍逼近上限；更大的 S/E 组合会越界）。int32 溢出会静默回绕到错误地址。u2-l4 讲过 `_create_context` 里的两道 int32 溢出断言，内核侧的对策就是把地址算式提升到 Int64。

### 4.4 宿主封装与 `dup_counts[0]` 的设备端读取

#### 4.4.1 概念说明

`n_groups`（有效组数）由路由碰撞决定，是**运行期数据**：同样的形状配置，均衡路由下可能只有几十个组，偏置路由下可能成千上万。它有三个候选处理位置：

1. **宿主读回**（`dup_counts[0].item()`）：一次 D2H 同步 + 流水线断流，MoonEP 的零宿主同步设计（静态形状、`cu_seqlens` 不回主机，见 u2-l1）就此破功，且每个 MoE 层、每个 micro-batch 都要付一次。
2. **编译期常量**：不可能，值随路由变化。
3. **设备端读取**：内核第 208 行一句 `n_groups = dup_counts_tensor[0]`，读取发生在内核启动后、进 SM 执行时，此时 builder 早已写完（同流的 dispatch 内核保证先后），宿主完全不知情。

MoonEP 选 3。代价是 grid 大小与实际工作量解耦——grid 固定为 `num_sms`，靠设备端的 `while gi < n_groups` 循环自适应（持久化内核风格）。这也是 round-robin 设计的前提：如果 `n_groups` 在宿主已知，本可以精确地只启动 \( \lceil n_{\text{groups}}/B \rceil \) 个 CTA。

#### 4.4.2 核心流程

`launch_dispatch_epilogue` 的宿主流程：

```text
读 ctx: H / NvS / num_sms_dedup / hidden_buf_local
前置断言（5 条）：H % 8 == 0；bf16 且连续；在 CUDA 上；
                形状恰为 (NvS, H)；device_index 非空
_check_epilogue_plan：plan 类型 / NvS 一致；
    dup_groups=(NvS,3) dup_loffs=(NvS,) dup_counts=(2,)
    均 int32、连续、在正确设备上
_get_compiled（lru_cache 键 = H, NvS, num_sms, device_index, pdl_launch）
    smem_budget = 设备 optin 上限 - 1024
    构造 DispatchEpilogueKernel → cute.compile 特化
make_ptr 包装四个裸指针（16 字节对齐假设；dup_counts 为 8）
取当前流，启动编译产物
```

H % 8 == 0 的约束来自 `cp.async.bulk` 要求 16 字节对齐的拷贝尺寸：每行 `H × 2` 字节（bf16），`H` 是 8 的倍数才有 `H_BYTES ≡ 0 (mod 16)`。

#### 4.4.3 源码精读

[moonep/dispatch_epilogue.py:208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L208) —— `n_groups = dup_counts_tensor[0]`：全讲最短也最关键的一行，设备端读取组数。`dup_counts` 的语义契约定义在 [moonep/planning.py:44-50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L44-L50)：`dup_counts = [n_groups, n_dup_loffs]`，仅紧凑前缀有效、顺序由 builder 的 atomicAdd 决定不稳定；分配处见 [moonep/planning.py:1229-1233](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1229-L1233)（`_round4` 过量分配防 128 位向量写越界）。

[moonep/dispatch_epilogue.py:312-314](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L312-L314) —— `_max_smem_per_block_optin` 按 `device_index` 做 `lru_cache`：设备属性查询每次都要走 CUDA driver，缓存后每设备只查一次。

[moonep/dispatch_epilogue.py:317-336](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L317-L336) —— `_check_epilogue_plan`：校验 `plan.NvS` 与 ctx 一致，三个 dedup 张量的 dtype/连续性/形状/设备逐项断言。这些是**防线前移**的典型——把「内核里读越界 / 错 dtype」这类难调试的设备侧故障，变成宿主侧一条带名字的断言。

[moonep/dispatch_epilogue.py:339-364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L339-L364) —— `_get_compiled`：`lru_cache` 以 `(H, NvS, num_sms, device_index, pdl_launch)` 为键——恰好覆盖内核里全部 `const_expr` 自由度；`smem_budget = optin - 1024` 留 1 KiB 余量；用占位指针 `make_ptr(BFloat16, 0, ...)` 编译出可复用 cubin，运行期只换真实指针。

[moonep/dispatch_epilogue.py:367-416](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L367-L416) —— `launch_dispatch_epilogue`：docstring 重申两个调用契约（必须在 `launch_dispatch` 之后的同一条流上；消费 fresh dispatch builder 写入的、或复用路径保存的 dedup 张量）；第 387 行 H % 8 断言；随后五条张量断言、`_check_epilogue_plan`、编译缓存查询、`make_ptr` 包装（第 399-406 行，注意 `dup_counts_ptr` 的 `assumed_align=8`——一个只有 2 个 int32 的张量只保证 8 字节对齐，其余三个 16）、取 `torch.cuda.current_stream()` 启动。

负例测试佐证这些断言真的会被触发：[tests/test_dispatch.py:491-498](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L491-L498) —— 测试故意把 `plan.dup_loffs` 换成 `flatten()[:-1].contiguous()`（长度差 1 的非法形状），`pytest.raises(AssertionError, match="dup_loffs")` 验证 `_check_tensor` 拦下。plan 复用路径的范本在同文件 [tests/test_dispatch.py:386-402](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L386-L402)：`plan_b` 用完后，重新以 `build_dedup_map=False` 跑 `plan_a`，再调 `launch_dispatch_epilogue(ctx, plan_a)`——复用路径的 epilogue 消费的就是快照里保存的 dedup 结构。

#### 4.4.4 代码实践

**实践目标**：宿主侧无 GPU 也能验证「防线前移」的断言清单，并理解设备端读取的零同步收益。

**操作步骤**：

1. 列出 `launch_dispatch_epilogue` 与 `_check_epilogue_plan` 的全部断言（共 9 条：H%8、bf16、连续、is_cuda、形状、device_index、plan 类型+NvS、三个 dedup 张量的 dtype/形状/设备），逐条注明它在防什么故障（例如 `dup_loffs` 形状错 → 内核 `slot = dup_loffs[...]` 读越界）。
2. 写一个纯 CPU 小脚本（示例代码），伪造非法 plan 触发 `_check_epilogue_plan` 的逻辑：

```python
# check_plan_fail_fast.py（示例代码）
import torch
from moonep.dispatch_epilogue import _check_epilogue_plan
from moonep.planning import MoonEPCommPlan  # 仅用于说明，构造完整 plan 见 allocate_planning_outputs

dev = torch.device("cpu")
NvS = 64
dup_groups = torch.zeros(NvS, 3, dtype=torch.int32)      # 合法
dup_loffs = torch.zeros(NvS - 1, dtype=torch.int32)      # 故意少 1 个元素
dup_counts = torch.zeros(2, dtype=torch.int32)
# 用最小桩对象代替完整 plan（_check_epilogue_plan 只访问 NvS 与三个字段）
class Stub:  pass
plan = Stub(); plan.NvS = NvS
plan.dup_groups, plan.dup_loffs, plan.dup_counts = dup_groups, dup_loffs, dup_counts
try:
    _check_epilogue_plan({'NvS': NvS}, plan, dev)
    print("未触发断言（异常）")
except AssertionError as e:
    print(f"按预期拦截: {e}")
```

3. 对照真实负例 [tests/test_dispatch.py:455-498](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L455-L498)，确认测试用的是同一套断言。

**需要观察的现象**：CPU 上断言同样触发——`_check_epilogue_plan` 不依赖 CUDA，错误信息里带张量名和期望形状。

**预期结果**：脚本输出 `按预期拦截: dup_loffs must be shape (64,), got (63,)`（**待本地验证**；若 `import moonep.dispatch_epilogue` 因缺少 CUDA 相关依赖失败，则退化为纯阅读型实践——直接阅读第 326-336 行逐条抄录断言与防御目标）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `n_groups` 改成宿主读回（`.item()`），最少要在哪些环节插入同步？代价是什么？

**答案**：builder 写 `dup_counts` 的 dispatch 内核必须先完成（一次 `torch.cuda.synchronize()` 或事件等待），宿主 `.item()` 触发 D2H 拷贝并阻塞，然后才能算 grid 大小启动 epilogue。每层 MoE 每个 micro-batch 一次宿主往返，正是 MoonEP 用静态形状消灭的「逐层宿主同步」（u2-l1），训练步的内核流水会被反复打断。

**练习 2**：设备端读 `n_groups` 后，如果 `n_groups == 0`（本 rank 无任何重复组），内核行为是什么？

**答案**：两个 warp 的 `while gi < n_groups` 循环体一次都不执行；生产者直接落到函数末尾，消费者只执行收尾的 `cp_async_bulk_wait_group(0)`（无在途组，立即返回），内核空转退出。grid 仍然启动了 `num_sms` 个 CTA，但每个都瞬间结束——固定 grid 换零同步的固定开销。

**练习 3**：`dup_counts_ptr` 的 `assumed_align=8` 而其他三个指针是 16，为什么可以更宽松？

**答案**：`make_ptr` 的 `assumed_align` 告诉编译器指针满足的对齐下限，编译器据此决定能否生成向量化访问。`dup_counts` 只有 2 个 int32（8 字节），`_round4(2)` 过量分配后也只切出 8 字节连续区，从它的基地址出发只可能做 8 字节对齐的访问；`hidden`/`dup_groups`/`dup_loffs` 则会被 128 位向量访问（TMA / 批量读），需要 16 字节承诺。声明过高的对齐而实际不满足，会生成非法地址访问。

## 5. 综合实践

把本讲所有环节串成一个「迷你端到端」：**从带重复的路由出发，自己构建 dedup 结构，再跑参考 epilogue，最后按测试口径校验**。

任务：编写 `mini_dedup_pipeline.py`（示例代码，CPU 可跑），完成三段：

1. **造路由**：随机生成 `S=64` 个 token、`K=4` 个 top-k、目的 rank 数 `R=2`（简化：直接给每个 token-topk 条目随机分配一个目的 rank 与槽位，故意让同一 token 的多个条目撞到同一 rank）。构造 `dst[S*K]`，并按 u3-l5 的规则把每个重复组里除最小 k 以外的条目编码为 `-raw_dst - 1`。
2. **构建三件套**：参考 [tests/planning_reference.py:252-290](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L252-L290) 的方式，为**每个目的 rank** 累积 `dup_groups`/`dup_loffs`/`dup_counts`（组头三元组、扁平重复槽、两个计数）。
3. **展开并校验**：对每个目的 rank 用 4.3 的 `dispatch_epilogue_reference` 展开 shard（主行内容用可追溯标记填充，模拟 `_traceable_hidden`），然后复刻 [tests/test_dispatch.py:227-231](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L227-L231) 的校验：对**每个** dst 条目（负数先 `-dst-1` 还原）检查对应槽位的行内容等于源 token 的标记——这等价于同时验证了「dispatch 只写主行」与「epilogue 补齐重复槽」两件事的合并效果。

成功标准：多个随机种子下校验零误差；再故意把展开步骤删掉跑一次，观察负数条目处必然报错（脏数据被检出）。

进阶（需多卡 NVLink 环境，**待本地验证**）：把同样的输入喂给真实内核——仿照 [tests/test_dispatch.py:169-192](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L169-L192) 的顺序调用 `launch_planning → launch_dispatch(build_dedup_map=True) → launch_dispatch_epilogue`，并用 `torchrun --nproc_per_node=2 -m pytest tests/test_dispatch.py` 观察现有测试的通过情况（注意测试文件里没有以 epilogue 命名的测试——epilogue 的正确性内嵌在 `test_dispatch_scatters_hidden_and_weights_by_dst`、`test_dispatch_saved_plan_hidden_only_reuses_dst_and_skips_weights` 等用例的校验链里）；随后设 `MOONEP_NUM_SMS_DEDUP=4` 重跑，对比时间体会 grid 缩小的性能影响。

## 6. 本讲小结

- **问题**：去重让 dispatch 只为每个重复组传一份 payload（省 NVLink 带宽），代价是接收端 shard 留空洞；`DispatchEpilogueKernel` 在 dispatch 之后**纯本地**地把主行扇出到所有重复槽，让 shard 成为完整的 `[NvS, H]` 用户可见布局。
- **结构**：64 线双 warp 极简内核——warp 0 批量 G2S 暂存主行（mbarrier 事务计数），warp 1 从 smem S2G 扇出（bulk_group 跟踪），主行从 gmem 只读一次；stage 释放延迟 `stages-1` 批次，保证异步 S2G 读完 smem 才让生产者复用。
- **几何**：握手摊销主导性能，`_B_CANDIDATES` 从大到小选「仍能双缓冲的最大批」；B ≤ 32 由「一 lane 一组头 + shuffle 广播」决定；批次 round-robin 映射到恒为 `num_sms_dedup` 的 grid（可用 `MOONEP_NUM_SMS_DEDUP` 覆盖以供基准扫描）。
- **零同步**：有效组数 `n_groups = dup_counts[0]` 在设备端读取，配合 `while gi < n_groups` 的持久化循环，宿主无需 `.item()` 读回——保住 MoonEP 零宿主同步的全链路设计。
- **衔接**：同流先后启动保证顺序，dispatch 的出口 `cross_rank_barrier` 发布 epilogue 要读的 NVL 写入，可选 PDL（`pdl_trigger_dependents` ↔ `pdl_wait_predecessor`）让 epilogue 的启动与 dispatch 收尾重叠；epilogue 自身无跨 rank 通信、无需自己的跨 rank 屏障。
- **工程**：宿主侧九条断言把设备侧故障前移（负例有测试覆盖），`lru_cache` 以 `(H, NvS, num_sms, device, pdl)` 为键复用 cubin；plan 复用路径原样消费快照里的 dedup 三件套。

## 7. 下一步学习建议

下一讲 **u4-l5《combine prologue：本地重复累加》** 是本讲的完美镜像：epilogue 在 dispatch 后把一份主行**扇出**到多个槽，prologue 则在 combine 前把多个槽**归约**回一份主行（fp32 累加），两者消费同一套 `dup_groups`/`dup_loffs`/`dup_counts` 契约。阅读时建议对比三个镜像点：生产者的「预扫描组头得到精确行数」vs 本讲的「一 lane 一组头」；`expect_tx` 的精确声明 vs 本讲的批量声明；以及反向传播中 dispatch bwd 与 combine bwd 如何共用这对内核的结构。

继续深读源码的路线：(1) [moonep/combine_prologue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py) 找出与本讲逐段对应的镜像代码；(2) [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) 与 [tests/kernel_test_utils.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py) 体会「顺序不稳定、只比集合语义」的对拍方法论（u6-l4 会展开）；(3) 回顾 [benchmarks/bench_comm.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py) 中 dispatch 算子的计时口径，思考 epilogue 的本地扇出成本被计在哪个算子里。
