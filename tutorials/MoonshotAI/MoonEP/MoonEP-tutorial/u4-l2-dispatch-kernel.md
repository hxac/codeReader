# dispatch 内核：warp 特化与 TMA 流水线

## 1. 本讲目标

本讲是通信内核单元的第一站，精读 `moonep/dispatch.py` 中的 `DispatchKernel` 设备内核与 `launch_dispatch` 宿主封装。学完本讲你应该能够：

1. 说出 dispatch 内核的 warp 角色分工（G2S 生产者 / S2G 消费者 / 零填充 / 去重构建）与启动几何（grid、block、cooperative launch）。
2. 理解「按行 TMA + mbarrier」流水线的生产消费协议：stage 如何被申请、填充、消费、延迟释放。
3. 理解设备 smem 预算如何决定流水线深度（`_smem_bytes` / `_pick_stages`），并能独立计算给定 `H` 下各 stage 数的共享内存用量。
4. 理解负数 `dst` 条目「只散射权重、不拷 payload」的执行路径，把 u3-l5 的编码契约落到内核分支上。
5. 掌握宿主侧 `launch_dispatch` 的校验链、编译缓存键与指针装配方式。

零填充 warp 与去重构建 warp 的内部细节属于下一讲 u4-l3，本讲只交代它们的职责与触发条件。

## 2. 前置知识

- **warp 与线程块**：GPU 上 32 个线程组成一个 warp，同一 warp 内指令同步执行；若干 warp 组成一个 CTA（线程块，CuTe DSL 里叫 block）；一次内核启动的所有 CTA 构成 grid。**warp 特化（warp specialization）** 指让不同 warp 固定扮演不同角色（生产者、消费者……），各角色并行工作、用显式同步原语交接数据，而不是所有线程做同一件事。
- **TMA 与 cp.async.bulk**：TMA（Tensor Memory Accelerator）是 Hopper 之后 GPU 上的异步拷贝引擎。MoonEP 绕开 DSL 的 TMA 封装、直发 PTX 指令 `cp.async.bulk`（见 u4-l1）：
  - **G2S**（global → shared）：拷贝完成时自动在 mbarrier 上累加事务字节数；
  - **S2G**（shared → global）：完成情况由 `cp.async_bulk_commit_group` / `cp_async_bulk_wait_group` 编组跟踪。
  - 两者都是**单线程指令**，只能由一个 elected lane 发射。
- **mbarrier 流水线**：把 smem 切成 `stages` 份环形缓冲，每份配 full/empty 两个 mbarrier（共 `2*stages` 个 i64）。生产者拿到 empty 才能写入，消费者等到 full 才能读取、确认数据离开 smem 后归还 empty——这就是经典的多阶段生产者-消费者环。
- **cooperative launch**：普通内核不保证所有 CTA 同时驻留；当内核里要用软件 grid 级栅栏（`grid_sync`）时必须以协作模式启动，保证全网格同时在场。dispatch 内核退出前要跨 GPU 同步，因此 `cooperative=True`。
- **承接 u3-l5**：规划器产出的 `dst` 是 int32 的 `目的rank × NvS + 槽内偏移` 编码；同一 token 的多条 top-k 落到同一目的 rank 时，只有最小 k 的主槽保持非负，其余写为 `-raw_dst - 1`（恒负、双射）。
- **承接 u2-l2/u2-l4**：`hidden_buf` 与 `meta_buf` 是跨 rank 对称内存——第 `r` 段物理上驻留在 rank `r` 的 GPU 上，但所有 rank 都握有一份布局相同的连续虚拟地址。因此内核里一次普通的「写本地指针」就可能落在远端 GPU。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `moonep/dispatch.py` | 本讲主角：`DispatchKernel` 设备内核 + `launch_dispatch` 宿主封装（含 `_smem_bytes` / `_pick_stages` / `_get_compiled` / 三组 `_check_*` 校验） |
| `moonep/constants.py` | 位宽与 warp 数常量：`KIDX_BITS`、`DEDUP_BUILDER_WARPS` |
| `moonep/_common.py` | PTX 助手（u4-l1 已精读）：`cp_async_bulk_g2s/s2g`、`cross_rank_barrier`、`pdl_trigger_dependents` 等 |
| `moonep/api.py` | 调用方：`_run_dispatch_on_current_stream` 编排规划→派发→epilogue；`_create_context` 分配 builder scratch |
| `moonep/planning.py` | 仅借用 `warp_inclusive_scan`（builder warp 聚合用，u4-l3 展开） |
| `tests/test_dispatch.py` | 七组参数化正确性用例 + 坏输入拒绝用例，本讲实践的运行对象 |

## 4. 核心概念与源码讲解

### 4.1 warp 角色分工与启动几何（DispatchKernel 的静态结构）

#### 4.1.1 概念说明

dispatch 要把本 rank 的 `S` 个 token 各复制 `K` 份、直写到（可能是远端的）expert 分组槽位。这本质上是「读本地 → 写远端」的批量搬运。MoonEP 把这份工作切成四类角色：

1. **G2S 生产者（warp 0）**：把本地 `hidden_sh` 的 token 行搬进 smem 流水线；
2. **S2G 消费者（warp 1）**：从 smem 把行写到（远端）`hidden_buf` 的目的槽，并散射路由权重；
3. **零填充 warp（warp 2）**：清零本 rank 分段的 padding 行（两条路径都运行）；
4. **去重构建 warps（warp 3..6）**：仅在新鲜规划路径（`build_dedup_map=True`）物化去重结构。

角色 3、4 的内部逻辑在 u4-l3 精读，本讲只要求记住「谁在跑、何时跑」。

#### 4.1.2 核心流程

启动几何与任务划分的规则：

- **block**：`num_threads = 96 + 32 × DEDUP_BUILDER_WARPS`（若 `build_dedup_map=False` 则只有 96，即前 3 个 warp）；
- **grid**：`(num_sms, 1, 1)`——每个 SM 恰好一个 CTA，配合 `cooperative=True` 保证全网格同时驻留（退出屏障需要）；
- **token 划分**：每个 CTA 认领一段连续 token：

\[ \text{tpb} = \lceil S / \text{num\_sms} \rceil,\quad \text{CTA}_b \text{ 负责 } [b \cdot \text{tpb},\ \min((b{+}1)\cdot\text{tpb},\ S)) \]

- **expert 划分**（零填充 warp）：CTA `b` 处理专家 `b, b+num_sms, b+2·num_sms, …`。

#### 4.1.3 源码精读

类的 docstring 完整声明了 warp 布局与去重契约，值得逐行读一遍：

[moonep/dispatch.py:50-73](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L50-L73) — `DispatchKernel` docstring：warp 0 生产者 / warp 1 消费者 / warp 2 零填充 / warp 3.. 去重构建；非负 `dst` 拷 payload，负 `dst` 只散射权重；重复行的原地展开在 `dispatch_epilogue.py`。

warp 编号与线程数是编译期常量：

[moonep/dispatch.py:75-81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L75-L81) — `num_threads = 96 + 32*DEDUP_BUILDER_WARPS`，`PRODUCER_WARP=0 / CONSUMER_WARP=1 / ZERO_WARP=2 / DEDUP_BUILDER_WARP=3`。`DEDUP_BUILDER_WARPS=4` 定义在 [moonep/constants.py:12-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L12-L17)，注释解释了取 4 的原因：builder 的分段扫描是延迟瓶颈，多 warp 分摊可把结构生成隐藏在 NVLink 传输之下，超过几个 warp 后收益变平。

构造函数在 JIT 前决定 `num_threads` 与流水线深度 `stages`：

[moonep/dispatch.py:113-122](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L113-L122) — `build_dedup_map` 决定是否加上 4 个 builder warp；`_pick_stages` 按 smem 预算选流水线深度，选不到 2 档就抛 `RuntimeError`（H 太大）。

内核实际启动处的几何：

[moonep/dispatch.py:258-264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L258-L264) — `launch(grid=(num_sms,1,1), block=(num_threads,1,1), smem=smem_bytes, cooperative=True)`。cooperative 启动保证所有 CTA 同时驻留——这是退出处软件 grid 栅障正确性的前提。

每 CTA 的 token 区间计算：

[moonep/dispatch.py:351-355](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L351-L355) — `tpb = (S + num_sms - 1) // num_sms`，本 CTA 处理 `[s_beg, s_end)`。生产者与消费者 warp 用**同一个区间**各自循环，靠流水线状态对齐。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，验证 warp 角色分工与启动几何的计算。

**操作步骤**（示例代码，可在装有 moonep 依赖的环境运行，无需多卡）：

```python
# geo_check.py（示例代码）
from moonep.constants import DEDUP_BUILDER_WARPS
from moonep.dispatch import DispatchKernel

for build in (True, False):
    nt = 96 + 32 * DEDUP_BUILDER_WARPS if build else 96
    print(f"build_dedup_map={build}: num_threads={nt}, warps={nt // 32}")

S, num_sms = 4096, 132
tpb = (S + num_sms - 1) // num_sms
print(f"tpb={tpb}, CTA0 覆盖 [0, {tpb}), 最后一个 CTA 覆盖到 {S}")
```

**需要观察的现象**：fresh 路径 224 线程（7 个 warp），reuse 路径 96 线程（3 个 warp）；`tpb = ceil(S/num_sms)`。

**预期结果**：`build_dedup_map=True → 224`；`False → 96`；S=4096、num_sms=132 时 tpb=32。

**待本地验证**：若当前环境未安装 nvidia-cutlass-dsl 依赖，`import moonep.dispatch` 会失败，此时可先完成 4.2.4 的纯算术实践。

#### 4.1.5 小练习与答案

1. **问**：`build_dedup_map=False`（plan 复用/反向路径）时，哪些 warp 仍在运行？
   **答**：warp 0（生产者）、warp 1（消费者）、warp 2（零填充）仍然运行；builder warps 被 `const_expr(self.build_dedup_map)` 编译期剪除——`num_threads` 也随之降为 96，相关指针只传占位符。
2. **问**：为什么 grid 恰好是 `num_sms` 个 CTA，而不是更多？
   **答**：每个 CTA 占用接近整块 opt-in smem，一个 SM 同时只能驻留一个 CTA；而内核退出处的 `cross_rank_barrier` 内部是软件 grid 栅障，要求**全网格同时活跃**（cooperative launch 的硬约束）。`num_sms` 个 CTA 恰好一 SM 一个，既填满机器又满足驻留要求。
3. **问**：零填充 warp 与消费者 warp 都写本 rank 的 `hidden_buf` 分段，为什么不会写冲突？
   **答**：规划保证每行至多一个写入来源——padding 行只由零填充 warp 写（`zero_fill_ranges` 恰好覆盖消费者不写的段内空隙），数据行只由（远端或本地的）消费者写。见 [moonep/dispatch.py:459-465](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L459-L465) 的注释。

### 4.2 smem 预算与流水线深度自适应

#### 4.2.1 概念说明

流水线越深（`stages` 越大），生产与消费越能互相掩盖；但每增加一个 stage 就多占 `2H` 字节（一行 bf16）加 16 字节 mbarrier。GPU 每块可用 smem 有硬上限（opt-in 上限，H100 为 227 KB 量级），`H` 大时必须降档。MoonEP 不让用户配置深度，而是在 JIT 时按设备属性**自动选最大可行的偶数档位**。这一小模块只涉及三个纯函数，是全内核里最适合动手计算的部分。

#### 4.2.2 核心流程

给定 `H` 与设备 opt-in smem，流程为：

1. `smem_budget = shared_memory_per_block_optin − 1024`（留 1 KB 余量）；
2. 从候选表 `(16, 14, 12, 10, 8, 6, 4, 2)` 由大到小试，第一个满足预算的即中选；
3. 全部不满足则返回 0，构造函数抛异常。

smem 需求公式（所有项向上取整对齐）：

\[ \text{smem}(H, s) = \lceil 2Hs \rceil_{128} + \lceil 2H \rceil_{128} + \lceil 16s \rceil_{16} + 256 \]

四项分别对应：`stages` 份行缓冲（bf16 每行 `2H` 字节，128 B 对齐）、零填充 warp 用的单行 `zero_smem`、每 stage 两个 i64 mbarrier（`2×8=16` B，16 B 对齐）、以及 256 B 的 cutlass 内部静态 smem 余量。

#### 4.2.3 源码精读

[moonep/dispatch.py:124-136](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L124-L136) — `_smem_bytes`：即上式的直接翻译，逐项 `_round_up`。

[moonep/dispatch.py:138-143](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L138-L143) — `_pick_stages`：候选表 `(16, 14, 12, 10, 8, 6, 4, 2)` 从大到小，第一个不超预算的胜出，否则 0。

[moonep/dispatch.py:699-701](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L699-L701) — `_max_smem_per_block_optin`：`lru_cache` 缓存的设备属性查询；预算扣减 1024 发生在 [moonep/dispatch.py:721](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L721)（`smem_budget = _max_smem_per_block_optin(device_index) - 1024`）。

内核侧的 smem 布局与流水线对象创建：

[moonep/dispatch.py:314-337](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L314-L337) — `SmemAllocator` 依次分配 `2*stages` 个 i64（mbarrier 组）、`stages` 份各 `H` 个 bf16 的 `stage_smem`（128 B 对齐）与单行 `zero_smem`；随后 `PipelineTmaAsync.create` 以「生产者 1 线程、消费者 1 线程、每 stage 事务量 `tx_count=H*2` 字节」建立加载流水线——这正对应 warp 0/warp 1 各自只有 lane 0 干活的设定。

[moonep/dispatch.py:339-349](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L339-L349) — `zero_smem` 的一次性初始化：全部线程合作清零 `H` 个 bf16，然后 `fence_view_async_shared + barrier` 把这次 smem 写发布给 cp.async.bulk 的异步代理——此后零填充 warp 每次直接从这块全零行发出 S2G，无需重复清零。

#### 4.2.4 代码实践

**实践目标**：写出给定 `H` 下各 stage 数的 smem 用量计算，与 `_smem_bytes` 逐项核对，并复现 `_pick_stages` 的选择。

**操作步骤**（示例代码，纯算术、无需 GPU；有依赖时再对拍）：

```python
# smem_calc.py（示例代码）
def round_up(n, a): return (n + a - 1) // a * a

def smem_bytes(H, s):
    return (round_up(s * H * 2, 128) + round_up(H * 2, 128)
            + round_up(s * 2 * 8, 16) + 256)

def pick_stages(H, budget):
    for s in (16, 14, 12, 10, 8, 6, 4, 2):
        if smem_bytes(H, s) <= budget:
            return s
    return 0

OPTIN = 232448            # 常见 H100 量级：227 KB opt-in
budget = OPTIN - 1024
for H in (2048, 4096, 7168, 8192, 32768):
    rows = {s: smem_bytes(H, s) for s in (16, 14, 12, 10, 8, 6, 4, 2)}
    print(f"H={H}: 每行 {H*2} B, 预算 {budget} B, "
          f"选中 stages={pick_stages(H, budget)}")
    print("   ", rows)

# 有 moonep 环境时对拍（无需 CUDA 设备）：
# from moonep.dispatch import DispatchKernel
# assert DispatchKernel._smem_bytes(7168, 14) == smem_bytes(7168, 14)
# assert DispatchKernel._pick_stages(7168, budget) == pick_stages(7168, budget)
```

**需要观察的现象**：H=2048 时 stages=16 也只用约 70 KB；H=7168 时 16 档超预算、落到 14 档；H 继续增大档位逐级下降。

**预期结果**（H=7168，行 14336 B，各项均恰为 128 的倍数）：

- s=16：229376 + 14336 + 256 + 256 = **244224 B > 231424** ✗
- s=14：200704 + 14336 + 224 + 256 = **215520 B ≤ 231424** ✓ → 选中 14

**待本地验证**：不同 GPU 的 `shared_memory_per_block_optin` 不同（可用 `torch.cuda.get_device_properties(i).shared_memory_per_block_optin` 查询），请以本机数值代入。

#### 4.2.5 小练习与答案

1. **问**：`H=7168`、opt-in 为 232448 B 时选中几档？
   **答**：14 档（计算见 4.2.4；16 档需 244224 B 超出预算 231424 B）。
2. **问**：`_pick_stages` 返回 0 会发生什么？什么样的 H 会触发？
   **答**：[moonep/dispatch.py:117-122](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L117-L122) 抛出 `RuntimeError`，报错信息里给出所需最小字节数（`_smem_bytes(H, 2)`）。触发条件是两档都放不下，即 `round_up(4H,128)+round_up(2H,128)+32+256 > budget`，约当 `6H·2 > 预算`（H100 上 H 大约超过 3.5 万时）。
3. **问**：为什么 stage 缓冲按 128 B 对齐、mbarrier 只按 16 B 对齐？
   **答**：`stage_smem` 是 cp.async.bulk 的源/目的，128 B 对齐满足 TMA 对块对齐的偏好（`allocate_tensor(byte_alignment=128)`，也是 `_smem_bytes` 里 128 取整的来源）；mbarrier 是 8 字节标量对象，16 B 对齐即可（`allocate_array` 后按 16 取整）。

### 4.3 数据通路：G2S 生产者、S2G 消费者与负 dst 路径

#### 4.3.1 概念说明

这是 dispatch 的主干：**token-major 本地行 → smem 流水线 → expert-grouped 远端行**。

- 生产者（warp 0）把第 `s` 个 token 的整行 `H` 个 bf16 用一次 G2S TMA 搬进 stage；
- 消费者（warp 1）等该 stage 就绪后，**逐 k 读取 `dst[s*K+k]`**：
  - `dst ≥ 0`：解码出 `目的rank = dst // NvS`、`槽内偏移 = dst % NvS`，把 smem 行用一次 S2G TMA 直写到 `hidden_buf` 的 `目的rank × NvS_padded + 槽内偏移` 行——因为对称内存，这个地址可能物理上位于远端 GPU；
  - `dst < 0`：解码 `raw = -dst - 1` 得到同样的目的 rank 与偏移，**跳过 payload 拷贝**，但权重散射照常执行。payload 只传主槽一份，重复槽由随后的 `dispatch_epilogue` 在接收端本地展开（u4-l4）。

注意权重散射本身也是一次**跨 rank 远端写**：`meta_tensor` 覆盖所有 rank 的 meta chunk，`drank*meta_stride + weights_off + loff` 直接落在目的 rank 的权重区。权重是 fp32，但内核经宿主侧 `.view(torch.int32)` 以 int32 位拷贝方式搬运，不做任何浮点运算。

#### 4.3.2 核心流程

以单个 token 的生命周期为主线（`stages = P`）：

```text
生产者 warp 0（lane 0）                消费者 warp 1（lane 0）
──────────────────────                ──────────────────────
for li in 0..n_tok-1:                 for li in 0..n_tok-1:
  acquire(stage[i%P])        ←empty—    wait(stage[i%P])      —full←
  G2S: hidden_sh[s] → smem[i%P]         for k in 0..K-1:
  (完成时 mbar 记 H*2 字节)                d = dst[s*K+k]
  advance                                 raw = d≥0 ? d : -d-1
                                          (drank, loff) = (raw/NvS, raw%NvS)
                                          d≥0: S2G smem[i%P] → hidden_buf[drank, loff]
                                          with_weights: meta[drank, loff] = w[s*K+k]
                                        commit_group
                                        if li ≥ P-1:
                                          wait_group(P-1)      # 在途 ≤ P-1
                                          release(stage[(i-P+1)%P]) —empty→
```

关键约束：

- 生产者必须先 `producer_acquire` 拿到 empty stage 才能发 G2S；消费者必须 `consumer_wait` 等 full（mbar 事务计数归零）才能读；
- 消费者的 `consumer_release` **延迟 P−1 个 token**：S2G 是 bulk_group 异步写，只有 `cp_async_bulk_wait_group(P-1)` 确认较早的行已离开 smem，才能把 stage 归还生产者复用——否则可能覆盖尚未写完的行；
- 每个 token 在 smem 中只驻留一份，却被 S2G 最多复用 K 次（每个 topk 目的各一次），这是「读一次本地、写 K 份远端」的带宽最优结构。

#### 4.3.3 源码精读

生产者循环：

[moonep/dispatch.py:360-382](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L360-L382) — warp 0：`producer_acquire` 后仅 lane 0 计算源行（`gmem_src.iterator + Int64(s)*Int64(H)`，注意 **Int64 行偏移**——`S×H` 可能超 int32）与 stage 行地址，调用 `cp_async_bulk_g2s`（带 mbar，字节数 `H_BYTES = H*2`），随后 `advance`。

消费者循环的 dst 解码与双分支：

[moonep/dispatch.py:402-427](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L402-L427) — 仅 lane 0 执行：读 `dst_tensor[sK+k]`；`store_token = dst_val >= 0`，负值时 `raw_dst = -dst_val - 1` 还原原始编码；解出 `drank = raw_dst // NvS`、`loff = raw_dst % NvS`；`store_token` 为真才计算 `drow = drank*NvS_padded + loff` 并发 `cp_async_bulk_s2g`。这正是 u3-l5「负数 dst = 重复槽，只传权重不传 payload」契约的执行点。

权重散射（int32 位拷贝）：

[moonep/dispatch.py:429-433](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L429-L433) — `with_weights` 时把 `w_tensor[sK+k]` 写进 `meta_tensor[drank*meta_stride + weights_off + loff]`——对重复条目（`store_token=False`）也执行，这就是「负 dst 仍散射权重」的落点。

节流与收尾：

[moonep/dispatch.py:434-448](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L434-L448) — 每个 token 的 K 次 S2G 归为一个 `commit_group`；`li >= stages-1` 后 `wait_group(stages-1)` 把在途组压到 P−1，再 `consumer_release` 归还最早的 stage；循环后 `wait_group(0)` 排空尾随在途写。注释特意说明结尾 release 并非必需，只是让 mbar 停在已知状态以便内核重放。

辅助函数的 PTX 语义（u4-l1 的回顾）：

[moonep/_common.py:414-435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L414-L435) — `cp_async_bulk_g2s`：`cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes`，完成时自动向 mbar 记账 `size` 字节。
[moonep/_common.py:438-452](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L438-L452) — `cp_async_bulk_s2g`：`cp.async.bulk.global.shared::cta.bulk_group`，完成走 bulk_group，无 mbar。

#### 4.3.4 代码实践

**实践目标**：在**本地实验副本**上修改 `_pick_stages` 候选表，验证流水线深度不影响正确性；同时完成 4.2.4 的 smem 计算（本实践两问合一，即任务规格要求的那个实践）。

**操作步骤**：

1. 先跑基线（需 8 卡 NVLink 机器）：
   `torchrun --nproc_per_node=8 -m pytest -s tests/test_dispatch.py`，确认 7 个 DISPATCH_CASES 全绿。
2. 打开本地 `moonep/dispatch.py`，把 [L140](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L140) 的候选表 `(16, 14, 12, 10, 8, 6, 4, 2)` 改成 `(2,)`——强制所有配置都只有最浅流水线（注意：若 H 过大将触发 4.2.5-2 的 `RuntimeError`，这是预期行为，可同时把 LARGE case 的 H 临时调小验证）。
3. 重跑测试。之后用 `git checkout -- moonep/dispatch.py` 还原。
4. 无 GPU 时做替代阅读实践：对 `duplicate_topk` 用例（S=32, K=4, epn=4, token_padding=8）手推一张表——任选一个 token，列出它的 4 个 `dst` 条目中哪些非负、每个条目的 `(drank, loff)`、payload 写几行、权重散射几次。

**需要观察的现象**：候选表改为 `(2,)` 后所有用例**仍然全部通过**（stages 只影响性能与 smem 用量，不改变数据放置语义）；用 `bash -c 'python -c "from moonep.dispatch import DispatchKernel; print(DispatchKernel._pick_stages(7168, 231424))"'` 之类的小探针（示例代码）可确认选档确实变成了 2。

**预期结果**：正确性不变；每 CTA smem 需求从 `215520 B`（14 档）降到 `round_up(2*14336,128)+14336+32+256 = 43232 B` 量级（H=7168 时），流水线掩盖能力变差——数据量大的用例（如 `balanced`，S=256）会变慢。

**待本地验证**：多卡结果与耗时变化需在真实 8 卡环境观察；本环境未运行上述命令。

#### 4.3.5 小练习与答案

1. **问**：某 token 的 K=8 个条目里有 3 条落到同一目的 rank（互为重复），内核一共发出几次 S2G payload、几次权重散射？
   **答**：1 次 payload（规划保证仅最小 k 的主槽非负）+ 3 次权重散射（每个 topk 条目都要散射，重复条目经 `-dst-1` 解码后走 `store_token=False` 分支但保留权重写）。
2. **问**：为什么 K 个 S2G 与权重散射都限定 lane 0 单线程执行，而不是 32 个 lane 分摊？
   **答**：`cp.async.bulk` 本身是单线程指令；若 32 lane 都执行，bulk_group 会被重复提交 32 次、权重被散射 32 遍（[L402](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L402) 注释："32 lanes would over-decrement the bulk_group / re-scatter"）。warp 内其余 lane 在流水线状态机中保持空闲。
3. **问**：为什么源/目的行偏移都用 `Int64` 计算？
   **答**：[L178-180](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L178-L180) 注释明确：生产规模下 `R × NvS_padded × H` 可超 2^31，int32 行偏移会回绕成错误地址。

### 4.4 退出跨 rank 屏障与 PDL 触发

#### 4.4.1 概念说明

dispatch 结束时，本 rank 写出的行（S2G 远端写 + 零填充 + 权重）必须先「发布」给所有 peer rank，对端才能安全读取自己分段里的这些行（供 epilogue 展开、专家 GEMM 或 combine 消费）。GPU 间没有免费的全局一致性，MoonEP 用 `cross_rank_barrier`——grid 栅障 + system 作用域 release/acquire 原子——手工建立 happens-before。此外，当 `pdl_trigger=True` 时，内核末尾调用 PDL 原语提前放行下一个内核（epilogue），把启动延迟藏进本内核的收尾阶段。

#### 4.4.2 核心流程

```text
所有 warp 完成各自循环
  ↓
cross_rank_barrier:
  fence.proxy.alias          # 写侧代理桥（把先前的组播写纳入发布）
  grid_sync                  # 本 grid 全 CTA 到齐
  block0: 每个 rank 向全部 peer 的相位槽 ±1（release-sys）
          自旋等本 rank 相位槽回到目标值（acquire-sys，100s 看门狗）
  grid_sync
  fence.proxy.async.global   # 读侧代理桥：获得的可见性延伸到后续 TMA
  ↓
pdl_trigger_dependents（可选）: fence → griddepcontrol.launch_dependents
```

相位 ±1 交替使屏障**自复位**——无需每次使用前清零（u3-l6 已讲其变体，这里聚焦调用点）。

#### 4.4.3 源码精读

[moonep/dispatch.py:683-692](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L683-L692) — 内核最后两条语句：`cross_rank_barrier(meta_tensor, meta_stride, barrier_off, rank, num_ranks, bar_tensor.iterator, num_sms, num_threads, tidx)`，随后按 `pdl_trigger` 调 `pdl_trigger_dependents(tidx)`。所有 warp（包括空闲 lane）都要执行屏障。

[moonep/_common.py:274-346](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L274-L346) — `cross_rank_barrier` 全貌：出入口两道代理 fence 的契约写在 docstring——入口 `fence.proxy.alias` 让本 rank 早先经组播地址（multimem VA）写的规划数据与单播别名一致化，出口 `fence.proxy.async.global` 让屏障获得的数据对本线程后续的 `cp.async.bulk` 可见。断言 `num_threads >= num_ranks`（block0 需要至少每 rank 一个线程去发信号），超时 100 秒自陷以防死锁挂机。

[moonep/_common.py:393-400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L393-L400) — `pdl_trigger_dependents`：`sync_threads → fence_acq_rel_sys → griddepcontrol.launch_dependents`，先发布本 CTA 写、再放行依赖内核。

调用链上游：`Buffer.dispatch` 把 `pdl_trigger=self.enable_pdl` 传入（[moonep/api.py:643-650](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L643-L650)），随后的 epilogue 以 `pdl_launch` 配对启动（[moonep/api.py:653](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L653)）。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——写出 `cross_rank_barrier` 的到达/离开协议伪代码，并解释两道 fence 若被去掉会发生什么。

**操作步骤**：

1. 精读 [moonep/_common.py:310-346](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L310-L346)，把三段（entry fence / block0 信号与自旋 / exit fence）翻译成自己的伪代码；
2. 回答：dispatch 是规划（组播发布）之后的下一个内核，如果去掉入口的 `fence.proxy.alias`，对端 rank 通过**单播 VA** 读 PLAN 区时可能看到什么？
3. 回答：如果去掉出口的 `fence.proxy.async.global`，本 rank 下一内核（epilogue）的 TMA 读取可能出什么问题？

**需要观察的现象 / 预期结果**：无运行成分，答案自评——(2) 组播写停在 VMM 别名代理里、未经桥接，单播读者可能读到旧值；(3) 屏障获得的一致性不覆盖 async 代理，epilogue 的 `cp.async.bulk` 读可能绕过缓存拿到陈旧数据。两道 fence 是「代理一致性」而非可有可无的优化。

#### 4.4.5 小练习与答案

1. **问**：dispatch 已经有 `grid_sync`（bar_tensor），为什么还需要 `cross_rank_barrier`？
   **答**：`grid_sync` 只同步**本进程本 GPU** 的 CTA；跨 rank 的发布需要 peer 之间通过 meta_buf 屏障区做 system 作用域 release/acquire 原子握手，`cross_rank_barrier` = 两次 grid_sync 夹一次跨 rank 信号交换。
2. **问**：`pdl_trigger` 为何要放在屏障**之后**？
   **答**：`pdl_trigger_dependents` 的语义是「本 CTA 的写已发布，依赖内核可以开始」。若放在屏障前放行 epilogue，epilogue 可能在数据行尚未跨 rank 发布时就读取 NVL shard，读到不完整数据。
3. **问**：屏障的超时看门狗是多少？触发后行为是什么？
   **答**：100 秒（`BARRIER_TIMEOUT_CYCLES` 折算，[L335-341](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L335-L341)），超时打印 rank/相位/信号诊断后 `device_trap()` 自陷，避免整机挂死。

### 4.5 launch_dispatch 封装：校验、编译缓存与指针装配

#### 4.5.1 概念说明

`launch_dispatch` 是 `Buffer.dispatch` 与设备内核之间唯一的宿主胶水。它做三件事：

1. **校验**：三组断言层层过滤坏输入（张量形状/dtype/设备、plan 字段契约、位编码上限、builder scratch 存在性），错误尽量在宿主侧报出而不是内核里跑飞；
2. **取编译产物**：`_get_compiled` 的 `lru_cache` 键覆盖全部编译期常量（形状、开关、设备、pdl），同配置二次调用零编译开销；
3. **装配指针**：把每个 torch 张量包成 CuTe 的 `make_ptr`，权重先 `.view(torch.int32)` 统一类型，reuse 路径的 builder scratch 用 `plan.dst` 占位以满足非空指针约束。

#### 4.5.2 核心流程

```text
launch_dispatch(ctx, hidden_sh, route_weights_sk, plan, build_dedup_map, pdl_trigger)
  ├─ with_weights = route_weights_sk is not None；None 时用 plan.dst 占位
  ├─ 断言 hidden_sh: bf16/连续/CUDA/[S,H]；H % 8 == 0（16B 拷贝对齐）
  ├─ _check_dispatch_plan: plan.dst/zero_fill_ranges/dup_* 的形状与设备
  ├─ build_dedup_map 时: _check_dedup_builder_bounds（位宽上限）+ _check_dedup_builder_tensors（scratch）
  ├─ _get_compiled(H..pdl_trigger) → 已缓存的特化 cubin
  ├─ w_int = route_weights_sk.view(torch.int32)
  ├─ 逐张量 make_ptr；scratch 不用时全部传 plan.dst 占位
  └─ compiled(hsh, hbf, w, dst, meta, zfr, bar, ..., rank, WEIGHTS_OFF, BARRIER_OFF, stream)
```

#### 4.5.3 源码精读

入口签名与 docstring（含负 dst 契约的官方表述）：

[moonep/dispatch.py:839-869](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L839-L869) — 参数表与 `build_dedup_map` 的语义：仅新鲜规划后为真；reuse/bwd 路径传假以免用过期的 `src_info` 重建 dedup 结构。

校验链：

[moonep/dispatch.py:883-905](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L883-L905) — hidden/weights 的 dtype、形状、设备断言；`H % 8 == 0`（`H*2` 字节须是 16 的倍数才满足 bulk 拷贝对齐）。
[moonep/dispatch.py:787-814](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L787-L814) — `_check_dispatch_plan`：plan 的 `dst`/`zero_fill_ranges`/`dup_groups[NvS,3]`/`dup_loffs[NvS]`/`dup_counts[2]` 必须是同设备连续 int32。
[moonep/dispatch.py:761-784](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L761-L784) — `_check_dedup_builder_bounds`：五条位编码上限——`S*K ≤ NvS`、`R*NvS ≤ int32_max`、`K ≤ 2^KIDX_BITS−1`、`NvS ≤ 2^(31−KIDX_BITS)−1`、`K ≤ 32`（kmask 是单字位掩码）。

编译缓存：

[moonep/dispatch.py:704-758](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L704-L758) — `_get_compiled` 被 `functools.lru_cache` 全参缓存；用 `make_ptr(类型, 0, ...)` 的哑指针做前向声明，`cute.compile` 按这些类型与 `DispatchKernel` 携带的常量特化出 cubin。返回的可调用对象**运行期只换指针不换几何**。

占位指针与权重 int32 视图：

[moonep/dispatch.py:920-946](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L920-L946) — `route_weights_sk.view(torch.int32)`（注释：与 C++ 做法一致，纯 4 字节 gather、无 fp32 运算）；`build_dedup_map=False` 时 `primary_packed/kmask/kidx_to_loff/builder_bar` 全部取 `plan.dst` 作无害占位。

最终调用：

[moonep/dispatch.py:965-984](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L965-L984) — 依 `__call__` 的参数序传入全部指针与 `rank/WEIGHTS_OFF/BARRIER_OFF` 标量，stream 取 `torch.cuda.current_stream()`（因此服从调用方的流上下文——异步通信流路径见 u6-l1）。

builder scratch 的出处（ctx 里这些键从哪来）：

[moonep/api.py:385-392](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L385-L392) — `_create_context` 分配 `primary_packed[R*S]`、`kmask[R*S]`、`kidx_to_loff[R*S*K]`（均 int32）与自复位 `builder_bar`，构造期一次分配、跨迭代复用。

#### 4.5.4 代码实践

**实践目标**：通过阅读 `tests/test_dispatch.py` 的坏输入用例，理解每条宿主断言拦截哪类错误。

**操作步骤**：

1. 精读 [tests/test_dispatch.py:453-498](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_dispatch.py#L453-L498)（`test_dispatch_rejects_bad_inputs`）；
2. 建立对照表：`hidden.float()` / 半截 `hidden` / `weights.double()` / 窄一列的 `weights` / `dst.long()` 的坏 plan / 坏 `dup_loffs` 的 plan 各命中哪一条断言（match 的正则是断言消息的前缀词）；
3. 有 8 卡环境时运行 `torchrun --nproc_per_node=8 -m pytest -s tests/test_dispatch.py::test_dispatch_rejects_bad_inputs -q` 观察全部按预期 `pytest.raises` 通过。

**需要观察的现象**：每类坏输入都抛 `AssertionError`，消息精确指明字段（如 `"hidden_sh must be shape ..."`）。

**预期结果**：六类坏输入分别命中 `hidden_sh`（dtype/形状两条）、`route_weights_sk`（dtype/形状两条）、`dst`、`dup_loffs` 断言——宿主侧快速失败，内核从不接触非法输入。

**待本地验证**：本环境无多卡 GPU，未运行该命令；对照表部分可离线完成。

#### 4.5.5 小练习与答案

1. **问**：`_get_compiled` 的缓存键里为什么有 `device_index`？同一个形状在两张不同型号卡上会共用一份 cubin 吗？
   **答**：不会。`stages` 由**设备各自的** `shared_memory_per_block_optin` 推出（[L721](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L721)），它作为 `DispatchKernel` 的 `const_expr` 参与特化，所以设备索引必须进缓存键，否则可能在 smem 更小的卡上启动超预算内核。
2. **问**：为什么把 fp32 权重 `view(torch.int32)` 而不是直接传 fp32 指针？
   **答**：内核只做 4 字节的位级搬运（写入 meta_buf 的 int32 权重区），不做浮点运算；统一成 int32 指针让 `_get_compiled` 的哑指针类型唯一，避免按 dtype 多份特化。fp32 的位模式原样落盘，读取端再 `.view(torch.float32)` 还原。
3. **问**：plan 复用路径（`build_dedup_map=False`）里 dedup 三件套为什么不能重建？
   **答**：builder 的输入是 planning 写入 `src_info` 区的溯源信息，属于当轮规划的临时产物；复用路径跳过 planning，`src_info` 已被后续轮次覆盖或过期，重建会把保存的 `dup_groups/dup_loffs/dup_counts` 破坏成错误数据（[L861-864](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L861-L864) docstring）。

## 5. 综合实践

把本讲内容串成一份 **dispatch 流水线配置报告**（示例代码框架如下，全部可离线完成，多卡部分标注待本地验证）：

```python
# dispatch_pipeline_report.py（示例代码）
def round_up(n, a): return (n + a - 1) // a * a

def smem_bytes(H, s):
    return (round_up(s*H*2, 128) + round_up(H*2, 128)
            + round_up(s*2*8, 16) + 256)

def pick_stages(H, budget):
    return next((s for s in (16,14,12,10,8,6,4,2)
                 if smem_bytes(H, s) <= budget), 0)

# 任务 1：对 H ∈ {2048, 4096, 7168, 8192, 16384, 32768} 与
# optin ∈ {166912, 232448}（A100/H100 量级）输出选中档位表，
# 标出 stages=0（无法启动）的组合。
#
# 任务 2：给定 S=4096, K=8, H=7168, R=8, num_sms=132：
#   - 计算 tpb 与各 CTA 的 token 区间；
#   - 一个 token 的 smem 行最多被多少次 S2G 复用（答：K 次）；
#   - 统计一轮 dispatch 至少发出多少次 G2S（S 次，每 token 一次）
#     与至多多少次 S2G（S*K 次，无重复时）。
#
# 任务 3（阅读型）：在 duplicate_topk 用例的随机路由下挑一个
# 重复 token，写出它的 4 条 dst（1 正 3 负）在 warp 1 循环里
# 依次经过的分支与效果，对照 4.3.3 的源码逐行核对。
```

多卡环境下的进阶：按 4.3.4 的步骤把候选表改为 `(2,)` 与 `(16,)`（后者对大 H 会抛 `RuntimeError`，验证 4.2 的预算逻辑），分别运行 `tests/test_dispatch.py` 并记录 `balanced` 用例的耗时差异，验证「stages 影响性能不影响语义」。**待本地验证**。

## 6. 本讲小结

- **warp 特化分工**：warp 0 G2S 生产者、warp 1 S2G 消费者、warp 2 零填充（两路径常驻）、warp 3..6 去重构建（仅 fresh planning）；block = `96 + 32×4` 或 96 线程，grid = `num_sms` 且 cooperative 启动。
- **smem 自适应**：`_smem_bytes(H,s) = ⌈2Hs⌉₁₂₈ + ⌈2H⌉₁₂₈ + ⌈16s⌉₁₆ + 256`，`_pick_stages` 在预算（opt-in − 1024）内从 16 档向下选最大偶数档，选不到即拒绝启动。
- **按行 TMA 流水线**：生产者 acquire→G2S（mbar 记 `H*2` 字节）→advance；消费者 wait→逐 k 解码 dst→（非负才）S2G→权重散射→commit；延迟 `stages−1` 个 token 才 `wait_group` + release，保证 stage 不被提前复用。
- **负 dst 路径**：`dst < 0` 时 `raw = -dst-1` 解码目的 rank 与偏移，跳过 payload、照常散射权重——发送端去重的执行点；重复行的补齐由 dispatch_epilogue 在接收端完成。
- **退出发布屏障**：`cross_rank_barrier` 用两次 grid_sync 夹 system 作用域原子握手，出入口两道 proxy fence 分别衔接组播写与后续 TMA 读；`pdl_trigger` 在屏障后放行 epilogue。
- **宿主封装**：三组 `_check_*` 断言快速失败，`_get_compiled` 按全量常量 lru_cache 特化，权重以 int32 视图位拷贝，reuse 路径 scratch 传占位指针。

## 7. 下一步学习建议

本讲只交代了 warp 2 与 builder warps 的「职责与门控」；下一讲 **u4-l3（dispatch 的零填充 warp 与去重构建 warp）** 将深入这两个角色：`zero_fill_ranges` 的段对齐语义、`primary_packed/kmask/kidx_to_loff` 三个 scratch 的 packed 编码（`kidx << NvS_BITS | loff`）、以及为什么 dedup 结构的组顺序因 atomicAdd 而不稳定。之后再进入 **u4-l4（dispatch epilogue）** 看重复槽如何在接收端被原地扇出补齐。复习锚点：`warp_inclusive_scan`（[moonep/planning.py:162](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L162)）与 `cross_warp_sync`（[moonep/_common.py:349-390](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L349-L390)）都将在 u4-l3 再次出现。
