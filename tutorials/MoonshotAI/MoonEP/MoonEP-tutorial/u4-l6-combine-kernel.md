# combine 内核：K 求和与路由权重收集

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `CombineKernel` 的 warp 特化布局——1 个 G2S 生产者、4 个 fp32 ACC 消费者、1 个 S2G 写回、外加 1 个可选的权重收集 warp——以及它们各自消费什么、生产什么。
2. 读懂两条流水线（`load_pipe` 与 `out_pipe`）和一条 `NamedBarrier` 子块同步如何把「按行 TMA 加载 → 寄存器 fp32 累加 → TMA 写回」串成三段式流水。
3. 解释负数 dst 条目为什么在 hidden 路径被整体跳过、在权重路径却照常解码（`-raw_dst - 1` 双射编码的接收端视角）。
4. 回答「入口跨 rank 屏障为什么放在 combine 而不是 prologue」这个设计问题。
5. 理解同一个 `combine` 内核如何同时充当前向的归并算子与反向传播中 dispatch 的对偶（dispatch bwd）。
6. 用 PyTorch 独立写出 combine 的参考实现，并与 `tests/test_combine.py` 的断言口径对齐。

## 2. 前置知识

本讲是通信内核单元（u4）的最后一讲，建立在前面几讲的概念之上。开始前请确认你理解以下内容：

- **dst 编码**（u3-l4/u3-l5）：规划器为每个 token 的每个 top-k 条目产出一个 int32 目的槽位 `dst = dest_rank * NvS + loff`；当同一 token 的多个 top-k 落到同一目的 rank 时，只有最小 k 的条目保持非负（主条目），其余写成 `-raw_dst - 1`（重复条目）。这个编码恒负、与合法非负值构成双射，符号位本身就是标志位。
- **去重三件套与 prologue**（u3-l5/u4-l5）：combine 方向的重复槽不是由 dispatch epilogue 扇出补齐，而是由 `CombinePrologueKernel` 在 combine 之前把每个重复组**以 fp32 精度原地累加回主行**。因此进入 combine 内核时，主行的值已经是「全组之和」——这是 combine 敢于跳过负条目的前提。
- **cp.async.bulk 两种形态**（u4-l1）：G2S（global → shared）走 mbarrier 事务计数（`complete_tx::bytes`），S2G（shared → global）走 `bulk_group` 编组完成追踪；两者都是单线程指令，只允许 lane 0 发射。
- **自复位屏障**（u3-l6/u4-l5）：`grid_sync` 用哨兵位翻转免清零；`cross_rank_barrier` 在其外包一层「block 0 跨 rank ±1 相位信号」，出入口各有一道 proxy fence 衔接普通访存与 TMA 代理。本讲只引用、不重复推导。
- **符号**：S（每 rank token 数）、K（top-k）、R（EP rank 数）、NvS（逻辑槽位数）、NvS_padded（VMM 对齐后的物理槽位数）、H（隐藏维，须为 128 的倍数）。

一句话直觉：**dispatch 把一个 token 复制成 K 份散到各 rank，combine 就是把这 K 份（去重后是若干组）重新加回一行**。数学上，对第 \(s\) 个 token：

\[
\text{out}[s] = \sum_{k=0}^{K-1} w_{s,k} \cdot \text{FFN}_{e_{s,k}}\big(\text{hidden}[s]\big)
\]

MoonEP 的分工是：路由权重 \(w_{s,k}\) 的加权由后续 GEMM/用户代码完成，**combine 内核只负责把去重后的 K 份载荷按 fp32 精度求和回 token-major**，并顺带把散布在各 rank 权重区的路由权重收集回 `[S, K]`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [moonep/combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py) | 本讲主角：`CombineKernel` 设备内核 + `launch_combine` 宿主封装 + 编译缓存 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | `Buffer.combine` 公共入口与 `_run_combine_on_current_stream` 编排（staging → prologue → combine），以及四象限用法注释 |
| [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py) | 被组合的原语：`cross_rank_barrier`、`cp_async_bulk_g2s/s2g`、`pdl_wait_predecessor` |
| [moonep/combine_prologue.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py) | 上一讲的 prologue；本讲引用其「无跨 rank 屏障」的设计声明来回答屏障位置问题 |
| [tests/test_combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py) | 六组测试用例 + 官方 PyTorch 全局参考实现 `_combine_global_reference`，是本讲实践的对照口径 |

## 4. 核心概念与源码讲解

### 4.1 整体架构：warp 特化布局与启动几何

#### 4.1.1 概念说明

combine 是一个**带宽受限**的内核：它要从 `R * NvS_padded` 行的 NVL 对称内存里挑出本 rank 关心的行（可能驻留在任何 rank 的 GPU 上，经 NVLink 远程读），逐 token 做 fp32 累加，再写回 `[S, H]` 输出。让它跑满带宽的核心手段是 **warp 特化（warp specialization）**：把「搬数据」「算数」「写回」分给不同 warp，让加载延迟与计算重叠。

`CombineKernel` 的 warp 布局（类文档字符串写得非常清楚）：

| warp | 角色 | 线程数 | 干什么 |
| --- | --- | --- | --- |
| 0 | G2S 生产者 | 32 | 逐 token 逐 k 解码 dst，非负条目发 `cp.async.bulk` 把远端行搬进 smem |
| 1–4 | fp32 ACC 消费者 | 128 | 从 smem 读 bf16 行、升 fp32 累加进寄存器，K 求和后转 bf16 写输出 smem |
| 5 | S2G 消费者 | 32 | 把输出 smem 的行用 `cp.async.bulk` TMA 写回 gmem |
| 6（可选） | 权重收集 | 32 | 独立通道：逐条目解码 dst（含负数）从 meta 权重区 gather 路由权重 |

注意 `num_warps = 6 if not with_weights else 7`——权重收集 warp 只在调用方传入 `output_sk` 时才编译进内核。

#### 4.1.2 核心流程

一个 CTA 的生命周期：

1. （可选 PDL）`pdl_wait_predecessor` 等 prologue 前驱网格放行。
2. **入口跨 rank 屏障**：所有线程先过 `cross_rank_barrier`（详见 4.4）。
3. 划分 token 区间：`tpb = ceil(S / num_sms)`，本 CTA 负责 `[s_beg, s_end)`。
4. 各 warp 进入自己的角色循环，直到各自 token 区间耗尽。
5. warp 5 收尾：`cp_async_bulk_wait_group(0)` 排干在途写回。

grid 取 `num_sms`、以 `cooperative=True` 启动——协作模式是入口 `grid_sync` 的硬性前提（所有 CTA 必须同时常驻）。

smem 预算上，fp32 累加器**完全活在寄存器里，不占 smem**；smem 只花在两处 bf16 行缓存（加载侧 `stages_l` 深、输出侧固定 2 深）加 mbarrier，因此流水线深度可以随 H 自适应选档。

#### 4.1.3 源码精读

类常量与构造逻辑——ACC 线程数 128（4 warp）、输出流水线固定 2 级，构造时按 smem 预算自动选加载流水线深度 `stages_l`，装不下则直接报错：

- [moonep/combine.py:L63-L98](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L63-L98) 定义 `ACC_THREADS = 128`、`STAGES_O = 2`；`__init__` 里 `self.stages_l = self._pick_stages_l(H, smem_budget)`、`self.num_warps = 6 if not with_weights else 7`，`stages_l == 0` 时抛出「H 对 smem 预算过大」的运行时错误。

smem 用量公式与选档表——注意 `stage_smem`（bf16 × stages_l 行）与 `out_smem`（bf16 × 2 行）各按 128 字节对齐，mbarrier 按 16 字节对齐，再留 256 字节余量：

- [moonep/combine.py:L100-L122](https://github.com/MoonshotAI/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L100-L122) `_smem_bytes` 计算 `round_up(stages_l·H·2, 128) + round_up(2·H·2, 128) + round_up(stages_l·2·8, 16) + round_up(2·2·8, 16) + 256`；`_pick_stages_l` 从候选 `(16, 14, 12, 10, 8, 6, 4, 2)` 自大到小取第一个装得下的档位。

宿主 JIT 入口与启动几何——所有形状都烧成 `const_expr` 编译期常量，launch 几何在编译期定死；输入张量按 int64 步长建行主视图（`R * NvS_padded * H` 生产规模下可超 int31，行偏移 `srow * H` 必须按 int64 求值，避免地址表达式回绕）：

- [moonep/combine.py:L150-L163](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L150-L163) 注释明确「`R*NvS_padded*H` can exceed 2^31 in production」，`gmem_in` 用 `cutlass.Int64` 步长构造 `[R*NvS_padded, H]` 视图。
- [moonep/combine.py:L187-L206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L187-L206) 内核以 `grid=(num_sms,1,1)`、`block=(num_threads,1,1)`、`cooperative=True`、`use_pdl=self.pdl_launch` 启动。

per-block token 切分：

- [moonep/combine.py:L299-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L299-L303) `tpb = (S + num_sms - 1) // num_sms`、`s_beg = bidx * tpb`、`s_end = min(s_beg + tpb, S)`——每个 CTA 独占一段连续 token，token 内的 K 个条目永远在同一 CTA 内处理（累加器不能跨 CTA 拆分）。

编译缓存——宿主侧 `_get_compiled` 以全部 constexpr 形状 + `with_weights` + 设备号 + `pdl_launch` 为键做 `lru_cache`，同一形状只 JIT 一次；smem 预算取自设备 opt-in 上限减 1024 字节：

- [moonep/combine.py:L517-L519](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L517-L519) `_max_smem_per_block_optin` 缓存 `shared_memory_per_block_optin`。
- [moonep/combine.py:L522-L562](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L522-L562) `_get_compiled` 用代表性指针参数走 `cute.compile`，其中 [L536](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L536) `smem_budget = optin - 1024`。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，独立复现 `_pick_stages_l` 的选档逻辑，理解 H 与流水线深度的权衡。

**操作步骤**（把公式抄进一个独立脚本 `stages_table.py`，无需 import moonep——它依赖 cutlass DSL）：

```python
# 示例代码：独立复现 CombineKernel 的 smem 选档公式
def round_up(n, a): return (n + a - 1) // a * a

def smem_bytes(H, s, stages_o=2):
    return (round_up(s * H * 2, 128) + round_up(stages_o * H * 2, 128)
            + round_up(s * 2 * 8, 16) + round_up(stages_o * 2 * 8, 16) + 256)

def pick_stages_l(H, budget):
    for s in (16, 14, 12, 10, 8, 6, 4, 2):
        if smem_bytes(H, s) <= budget:
            return s
    return 0

BUDGET = 232448 - 1024   # 以 H100 为例：opt-in smem 上限 232448 B
for H in (128, 2048, 7168, 8192, 16384):
    print(f"H={H:6d} -> stages_l={pick_stages_l(H, BUDGET)}")
```

**需要观察的现象**：H 越大、能装下的流水线深度越浅；H 超过某个值后返回 0（即 `__init__` 会抛错）。

**预期结果**：在 232448 B 预算下应得到 `H=128 → 16`、`H=7168 → 14`（`smem_bytes(7168, 14) = 229888 ≤ 231424`，而 16 档需 258592 放不下）、`H=8192 → 12`、`H=16384 → 4`。你机器上的绝对数值取决于 `torch.cuda.get_device_properties(i).shared_memory_per_block_optin`，档位选择逻辑是设备无关的。（脚本为纯 CPU 计算，结果确定；具体设备的 opt-in 值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 fp32 累加器放寄存器而不放 smem？如果放 smem，`_smem_bytes` 公式会多出哪一项？

**答案**：寄存器是每线程私有的、吞吐远高于 smem，ACC warp 每线程只拥有 `H/128` 个 fp32（H=7168 时 56 个寄存器），访问零冲突。若放 smem，公式需增加 `round_up(stages_o * H * 4, 128)` 一类的输出侧 fp32 缓冲（fp32 是 bf16 的两倍宽），smem 占用近似翻倍，会显著压低可选的 `stages_l` 档位。

**练习 2**：`launch` 为什么必须 `cooperative=True`？

**答案**：内核入口的 `cross_rank_barrier` 内部先做一次 `grid_sync`（跨 CTA 的网格栅障）。CUDA 只允许协作启动（cooperative launch）的网格执行网格级同步——它保证所有 CTA 同时常驻 SM，否则先到的 CTA 自旋等待时可能占满 SM、后到的 CTA 永远无法启动，形成死锁。

**练习 3**：token 区间为什么按连续段切分（`[s_beg, s_end)`），而不是像零填充 warp 那样用 grid-stride 循环？

**答案**：一个 token 的 K 个条目必须由同一个 CTA 累加（fp32 累加器是 CTA 私有寄存器状态，无法跨 CTA 合并）。连续段切分保证 token 的所有 K 个条目落在同一 CTA；grid-stride 会把同一 token 的条目拆给不同 CTA，破坏累加语义。

### 4.2 三段式流水线：G2S 生产者 → fp32 ACC 消费者 → S2G 写回

#### 4.2.1 概念说明

三段式流水线由两条独立的管道粘合：

- **`load_pipe`**（`PipelineTmaAsync`，`stages_l` 深度）：warp 0 生产（TMA 搬入 bf16 行），warps 1–4 消费（读出累加）。完成协议是 mbarrier **事务计数**——每次 G2S 拷贝完成会自动给 mbar 到账 `H*2` 字节（`tx_count=H*2`），消费者等满即知行已就位。
- **`out_pipe`**（`PipelineAsync`，固定 2 深度）：warps 1–4 生产（写输出 smem），warp 5 消费（TMA 写回 gmem）。它是纯计数握手（count-based arrive），没有事务字节。

关键同步原语是 **`NamedBarrier`（子块屏障）**：4 个 ACC warp 写完输出 smem 的一行后，必须**全部**写完才能让 warp 5 的 TMA 读这行。用 `__syncthreads` 会把毫无关系的 warp 0（还在预取）和 warp 5（还在写回）一起拖住，破坏流水线并发；`NamedBarrier(barrier_id=8, num_threads=128)` 只同步 128 个 ACC 线程，其余 warp 不受影响。id 取 8 是因为 0..7 已被 CUTLASS 流水线与 `__syncthreads` 保留。

两条管道的生产者/消费者组大小都经过精确设计（源码注释逐条解释）：

- `load_pipe` 生产者组 = 1：`producer_acquire` 的 arrive 内部用 `elect_one`，等效单个信号源；
- `load_pipe` 消费者组 = 4：`PipelineTmaAsync.consumer_release` 只让每个 warp 的 lane 0 发 arrive（signalling thread），4 个 ACC warp 恰好 4 次；
- `out_pipe` 生产者组 = 128：全部 128 个 ACC 线程都调用 `producer_commit`（计数式 arrive）；
- `out_pipe` 消费者组 = 32：warp 5 全部 32 线程调用 `consumer_release`。

#### 4.2.2 核心流程

对每个 token `s`（两级循环结构，K-cycle 的序幕/尾幕被提到 k 循环外面）：

```text
warp 0（生产者）:                     warps 1..4（ACC 消费者）:            warp 5（S2G）:
for s in [s_beg, s_end):              for s in [s_beg, s_end):             for s in [s_beg, s_end):
  for k in 0..K-1:                      acquire out_pipe 槽位                wait out_pipe（等行就绪）
    v = dst[s*K+k]                      acc_reg ← 0      # 清零            lane0: S2G(out_smem 行 → gmem_out[s])
    if v >= 0:        # 见 4.3          for k in 0..K-1:                    commit_group
      解码 drank/loff                     v = dst[s*K+k]                   if s_local >= 1:
      acquire load_pipe 槽位              if v >= 0:                        wait_group(≤1 在途)
      lane0: G2S(远端行 → smem)             wait load_pipe（等 TMA 到账）    release out_pipe（滞后释放）
      advance                              acc_reg += fp32(smem 行)
                                        转换 acc_reg → bf16 写 out_smem
                                        acc_bar.arrive_and_wait()  # 128 线子块同步
                                        fence + commit out_pipe
```

要点：

- ACC 的累加顺序按 k 升序、逐项 fp32 累加，最后一次转 bf16——**只有一次最终舍入**（prologue 已引入一次中间 bf16 舍入，见 4.4.4 的精度讨论）。
- `out_pipe` 只有 2 级，warp 5 的释放滞后 `STAGES_O - 1` 个 token，保证 TMA 读 smem 与 ACC 写下一版 smem 之间始终有双缓冲隔离；循环结束后还有一段尾部排水（drain）把剩余在途 stage 释放完。
- warp 0 与 warps 1–4 **各自独立读同一个 dst 值**、用同一个谓词决定是否步进流水线——这是流水线对齐的关键，见 4.3。

#### 4.2.3 源码精读

smem 布局与两条管道的构造——注意注释逐字解释了每个组大小的来历：

- [moonep/combine.py:L248-L293](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L248-L293) 先分配两组 mbarrier（`2*stages_l` 与 `2*STAGES_O` 个 Int64），再分配 `stage_smem`（`[H, stages_l]` bf16）与 `out_smem`（`[H, 2]` bf16），随后创建 `load_pipe`（`tx_count = H * 2`）与 `out_pipe`。

`NamedBarrier` 的构造与保留 id 说明：

- [moonep/combine.py:L295-L297](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L295-L297) `acc_bar = pipeline.NamedBarrier(barrier_id=8, num_threads=self.ACC_THREADS)`，注释指出用户 NamedBarrier id=0 映射到硬件 id 8、id 0..7 被 CUTLASS 流水线 / `__syncthreads` 保留。

warp 0 生产者——解码与单线程发射：

- [moonep/combine.py:L308-L350](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L308-L350) 双层循环里 `load_token = dst_val >= Int32(0)` 决定是否搬行；`drank = dst_val // NvS`、`loff = dst_val % NvS`、`srow = drank * NvS_padded + loff` 定位物理行；`if cute.arch.lane_idx() == 0:` 里经 `_cp_async_bulk_g2s` 发射拷贝——注释强调 cp.async.bulk 是单线程指令，32 个 lane 都发会导致 mbarrier 事务计数被重复扣减。

warps 1–4 的寄存器布局——strided 划分让同一 j 步的 128 线访存连续：

- [moonep/combine.py:L355-L366](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L355-L366) `acc_tid = tidx - 32`（0..127），第 j 个寄存器负责元素 `idx = j * 128 + acc_tid`；注释说明宿主断言 `H % 128 == 0` 让代码可以省掉 `idx < H` 谓词，否则该谓词会阻止编译器做 LDS→SHF→FADD 的软件流水。

两级循环的 K-cycle 序幕/尾幕提升：

- [moonep/combine.py:L375-L388](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L375-L388) 注释直说：把「acquire 输出槽 + 清零累加器」（k==0 的工作）和「转换 + commit」（k==K-1 的工作）提到 k 循环外，让 codegen 甩掉 `if k == 0` / `if k == K-1` 分支和 `s_local = li // K` 的整除降级。

K 循环内的累加与释放——release 前只需 warp 级同步的理由：

- [moonep/combine.py:L390-L424](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L390-L424) `consumer_wait` 等行到账后逐寄存器 `acc_reg[j] += Float32(stage_smem[idx, load_state.index])`；随后 `fence_view_async_shared()` + `sync_warp()` + `consumer_release`。注释解释：release 的 arrive 只从每个 warp 的 lane 0 发出（signalling thread），所以 warp 级同步就足以覆盖本 warp 32 线的读——跨 warp 的完成一致性已由 4 计数的空 mbarrier 把关。

尾幕：bf16 转换、子块屏障、commit：

- [moonep/combine.py:L426-L435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L426-L435) `out_smem[idx, stg_o] = BFloat16(acc_reg[j])` 后 `acc_bar.arrive_and_wait()` + `fence_view_async_shared()` + `out_pipe.producer_commit(out_state)`——128 线全部写完输出行、且对 async 代理可见后，才允许 warp 5 的 TMA 读。

warp 5 写回——滞后释放与尾部排水：

- [moonep/combine.py:L440-L480](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L440-L480) lane 0 发 `_cp_async_bulk_s2g` + `cp_async_bulk_commit_group`；`s_local >= STAGES_O - 1` 之后每轮先 `cp_async_bulk_wait_group(STAGES_O - 1)`（最多允许 1 个在途组）再 release；循环外 `wait_group(0)` 排干后还有一个 `STAGES_O - 1` 次的小循环补发剩余 release（`rel_state.count < use_state.count` 判断还有欠账）。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读 + 手工推演，验证「每个 token 的 `consumer_wait`/`consumer_release` 次数等于该 token 的非负 dst 条目数」，并理解 warp 5 的滞后释放。

**操作步骤**：

1. 打开 [moonep/combine.py:L308-L350](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L308-L350) 与 [L390-L424](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L390-L424)，并排阅读。
2. 手工构造一个 `K = 3`、`dst = [16, -17, 5]` 的 token（一个主条目、一个重复条目、又一个主条目），在纸上写下 warp 0 发射几次 G2S、ACC 执行几轮 `wait → 累加 → release`。
3. 再数 warp 5：`STAGES_O = 2` 时，前 `STAGES_O - 1 = 1` 个 token 它只 wait + 写、不 release；写出每个 `s_local` 上 `use_state` 与 `rel_state` 的 count 差。

**需要观察的现象**：warp 0 与 ACC 两边的流水线状态机在每个 token 上步进完全相同的次数；warp 5 的 use/rel 计数差恒 ≤ `STAGES_O`。

**预期结果**：示例 dst 下 warp 0 发 2 次 G2S（跳过 `-17`），ACC 做 2 轮 wait/release——两侧由**同一个谓词**（`dst_val >= 0`）保证对齐；warp 5 对每个 token 恰好一次 wait 与（滞后的）一次 release，循环结束时补发恰好 1 次尾部 release。这是纯逻辑推演，结论确定；如需运行验证，需多卡 NVLink 环境（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`load_pipe` 的消费者组大小为什么是 4 而不是 128？

**答案**：`PipelineTmaAsync.consumer_release` 的 arrive 由每个消费 warp 的 signalling thread（非集群模式下即 lane 0）发出，4 个 ACC warp 每轮合计恰好 4 次 arrive，mbarrier 的期望计数就配成 4。若配成 128，空 mbarrier 永远等不齐（只有 4 次 arrive），内核会挂死。这也是 K 循环内跳过谓词必须对 128 线 **uniform** 的原因之一——任何一个线程单独走偏都会让某侧的 arrive 次数错位。

**练习 2**：ACC 写完 `out_smem` 后为什么是 `acc_bar.arrive_and_wait()`（双向屏障）而不是单向 arrive？

**答案**：`out_pipe` 只有 2 级。第 `s` 个 token 写 `stg_o = s % 2` 号输出槽，而第 `s+2` 个 token 要写同一个槽。`arrive_and_wait` 保证本组 128 线不仅宣告「我写完了」，还等到**全组都写完**才继续——此后 `producer_commit` 放行 warp 5，warp 5 释放该槽后 ACC 才可能在两轮后安全复用。单向 arrive 无法表达「全组完成」这个汇合点。

**练习 3**：warp 5 为什么用 `wait_group(STAGES_O - 1)` 而不是每轮 `wait_group(0)`？

**答案**：`wait_group(0)` 要求所有在途 S2G 全部落盘才能继续，等于把 TMA 写回串行化，双缓冲失去意义；`wait_group(1)` 允许最多 1 组在途，warp 5 在上一组还在飞的时候就能处理下一行，写回带宽得以流水化。循环结束后的 `wait_group(0)` + 补发 release 才是真正的排干点。

### 4.3 负数 dst 契约与 warp 6 路由权重收集

#### 4.3.1 概念说明

回顾 u3-l5 的去重编码：同一 token 的多个 top-k 落到同一目的 rank 时，重复条目的 dst 被规范化为 `-raw_dst - 1`。combine 是这个编码的**接收端消费者**，且对两类数据采取截然相反的策略：

- **载荷（hidden）路径——跳过**：重复条目的贡献已被 prologue 预累加进主行，主行 = 全组之和。combine 若再加载重复槽就是重复计数。所以 warp 0 不为它发 TMA，ACC 不为它步进流水线。
- **权重（route weights）路径——照常解码**：路由权重是 **per-topk** 的量，从不参与去重（dispatch 侧就是逐条目散射的）。每个条目（无论正负）都拥有自己的权重槽，combine 的权重收集必须把 K 个权重一一收回，供反向传播计算 router 权重梯度。

这个「一跳一收」的契约由**双侧独立判定**保证：warp 0 和 warps 1–4 各自读一遍 `dst[s*K+k]`、各自算 `dst_val >= 0`。两边看到同一个值、走同一个分支，于是发射的加载与等待的到账逐 stage 对齐；同时该谓词对 128 个 ACC 线程是 uniform 的（大家读的是同一个 dst），不会破坏 `acc_bar` 与 mbarrier 的计数一致性。

权重收集被放进**独立的 warp 6**，而不是搭 ACC 消费者的便车：源码注释解释，早期把它放在某个 ACC 线程上会把「每 k 一次的全局 LDG」串到累加关键路径上，把带权重的反向路径卡到只有无权重路径的零头；独立 warp 与两条流水线毫无数据依赖，完全并发。

#### 4.3.2 核心流程

```text
载荷路径（warp 0 / warps 1..4，同一谓词两侧判定）:
  v = dst[s*K + k]
  if v >= 0:                       # 主条目
      srow = (v // NvS) * NvS_padded + (v % NvS)
      warp 0:  G2S(hidden_buf[srow] → smem)          # 远端读经对称内存
      ACC:     acc_reg += fp32(smem 行)
  else:                            # 重复条目：完全不碰载荷流水线
      (无操作)

权重路径（warp 6，逐条目无差别处理）:
  v = dst[s*K + k]
  raw = v if v >= 0 else -v - 1    # 解码
  drank, loff = raw // NvS, raw % NvS
  out_sk[s*K + k] = meta[drank * meta_stride + WEIGHTS_OFF + loff]   # 4 字节位拷贝
```

权重在 meta 缓冲中的寻址是 rank-stride 布局：第 `drank` 个 rank 的权重区起点为 `drank * meta_stride + weights_off`，槽位偏移 `loff` 与载荷槽一一对应（dispatch 散射权重与载荷用同一个 loff）。

#### 4.3.3 源码精读

载荷路径的跳过谓词与解码：

- [moonep/combine.py:L317-L327](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L317-L327) 注释写明「Negative dst marks a duplicate entry: its contribution was pre-reduced into the primary slot on the destination rank, so no payload row is loaded for it」，`load_token = dst_val >= Int32(0)` 后解码 `drank/loff/srow`。

消费者侧的镜像谓词——两段注释是理解流水线对齐的钥匙：

- [moonep/combine.py:L390-L397](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L390-L397) ACC 用「Same skip predicate as the producer (both read the same dst value)」保证 wait/release 与发射的加载 stage 对齐——每个 token 双方对每个非负条目**恰好各步进一次**；且谓词对 128 线 uniform，`acc_bar` 计数保持一致。

warp 6 权重收集——负条目照常解码：

- [moonep/combine.py:L485-L511](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L485-L511) warp 6 以 32 线 grid-stride 式分块（`n_chunks = (sk_total + 31) // 32`，每 lane 一个 `(s, k)` 条目）遍历本 CTA 的 `sk` 区间；[L499-L511](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L499-L511) `raw_dst = -raw_dst - Int32(1)`（当负）后照常 `// NvS`、`% NvS`，从 `meta_tensor[drank * meta_stride + weights_off + loff]` 取 4 字节写入 `sk_tensor`。注释强调「Duplicate entries still own their per-topk weight slot … mirroring the dispatch-side weight scatter」。

- [moonep/combine.py:L486-L489](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L486-L489) 独立 warp 的动机注释：gather 与两条流水线无数据依赖，放在 ACC 线程上会「serialized one global LDG per K-iter onto the critical path」，把带权重的路径限流到无权重路径的一小截。

宿主侧的位拷贝约定——fp32 权重经 int32 视图搬运：

- [moonep/combine.py:L624-L626](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L624-L626) `sk_int = output_sk.view(torch.int32)`，注释说明内核始终看到 int32 指针——4 字节 gather、不做任何 fp32 算术，所以位模式即数值，round-trip 无损。

测试对权重 round-trip 的断言口径：

- [tests/test_combine.py:L254-L273](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L254-L273) `test_combine_output_sk_gathers_route_weights` 断言 `out_weights` 与**原始输入** `weights` 逐元素相等（`assert_tensor_equal_all_ranks`，内部是 `torch.equal`）——dispatch 散射 + combine 收集构成恒等变换。注意 [L264-L267](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L264-L267) 的注释「Re-dispatch because combine reads the mutable NVL buffer」：第一次 combine 消费了 NVL 缓冲，比较前必须重新 dispatch 一遍。

#### 4.3.4 代码实践

**实践目标**：用手工小用例验证「载荷跳过、权重照收」的双轨契约。

**操作步骤**：

1. 设 `R=2, NvS=8, K=3`，某 token 的三个条目 `dst = [8+0, -(8+3)-1, 0+2]`（即 `[8, -12, 2]`）：k0 主条目落 rank1 槽 0，k1 是 k0 的重复（编码自 raw=11，即 rank1 槽 3），k2 主条目落 rank0 槽 2。
2. 在纸上写出：warp 0 对哪些 `srow` 发 G2S；ACC 累加哪几行；warp 6 对三个条目分别读 `meta` 的哪个下标。
3. 假设 prologue 已把 rank1 槽 3 的值并入 rank1 槽 0（主行 = 两行之和），写出 `out[s]` 的表达式；再按「不去重的朴素语义」（K 行全加）写出表达式，比较两者。

**需要观察的现象**：两条路径读的槽不同，但数学结果一致。

**预期结果**：warp 0 只对 `srow = 1*NvS_padded + 0` 与 `srow = 0*NvS_padded + 2` 发两次 G2S；ACC 累加 `shard[1,0] + shard[0,2]`，其中 `shard[1,0] = 行(k0) + 行(k1)`（prologue 归约后），故 `out[s] = 行(k0)+行(k1)+行(k2)`，与朴素 K 求和完全一致。warp 6 对三个条目分别读 `meta[1*stride + off + 0]`、`meta[1*stride + off + 3]`、`meta[0*stride + off + 2]`——`out_sk[s]` 恰好收回 dispatch 时散射的三个原始权重。纯逻辑推演，结论确定。

#### 4.3.5 小练习与答案

**练习 1**：如果 warp 0 与 ACC 的跳过谓词不一致（比如 ACC 忘了判断、总是 `consumer_wait`），会发生什么？

**答案**：流水线错位。ACC 每个条目都 wait 一次，但 warp 0 只为非负条目发起到账——遇到重复条目时 ACC 等的事务字节永远不齐，空 mbarrier 翻转不了，内核挂死（或谓词不 uniform 时 `acc_bar` 计数错位、同步语义破坏）。这正是源码把「两侧读同一 dst、用同一谓词」作为显式注释写下来的原因。

**练习 2**：为什么权重不能也去重（只传主条目的权重）？

**答案**：载荷去重的语义基础是「同一 token 经同一专家 FFN 的输出相同，主行一份即可代表」；而同组内各 top-k 条目的路由权重 \(w_{s,k}\) 彼此不同（它们对应不同专家的_gate 值），combine 后还要各自参与加权求和与 router 梯度计算，不是冗余数据。dispatch 端「权重逐条散射、载荷去重」与 combine 端「权重照收、载荷跳过」是同一契约的两面。

**练习 3**：`launch_combine` 里 `output_sk` 为什么要求 fp32，内核却拿 int32 指针读它？

**答案**：宿主断言（[moonep/combine.py:L601-L603](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L601-L603)）要求 `output_sk` 是连续 fp32；随后 `view(torch.int32)` 转成同样 4 字节宽的 int32 视图传入。gather 是纯位搬移（无算术），fp32 与 int32 位模式一一对应，取回后再按 fp32 解释即为原值——测试用 `torch.equal`（而非 allclose）断言，验证的正是这条无损链路。

### 4.4 入口跨 rank 屏障与 dispatch bwd 对偶性

#### 4.4.1 概念说明

**屏障为什么存在**：combine 读的行可能驻留在任何 rank 的 shard 里（对称内存远程读）。这些行的最终值 = 「staging 拷贝（用户张量 → NVL shard）+ prologue 的重复组归约」，而这两件事发生在**每个 rank 自己**的流上。读远端之前，必须存在一个全组汇合点，保证所有 rank 的写都已发布。`cross_rank_barrier` 就部署在 combine 内核入口、任何远端读之前。

**为什么在 combine 而非 prologue**：三个理由层层递进——

1. **必要性**：prologue 只写本地 shard、不读任何远端数据（其文档字符串明确声明「This kernel has no cross-rank barrier」），它根本不需要等别人；combine 是这些写的第一个消费者，屏障保护的数据依赖只在 combine 处成立。
2. **充分性/覆盖面**：屏障要发布的写包括 staging 拷贝（宿主流上的 `copy_`）和 prologue 归约两部分。若屏障放在 prologue 入口，staging 尚未完成就放行了；放在 combine 入口则恰好覆盖「staged + accumulated NVL writes」整体（`api.py` 的注释原话）。
3. **性能**：prologue 与 combine 之间用 PDL（programmatic dependent launch）衔接——prologue 结尾 `pdl_trigger_dependents` 提前放行 combine 的启动，combine 开头 `pdl_wait_predecessor` 等待前驱网格。把屏障放在 combine 入口，让「内核启动开销」与「等待全组到齐」重叠在同一段时间里，而不是在 prologue 出口白白多等一轮。

**dispatch bwd 对偶性**：`Buffer.combine` 的文档字符串写着「Also serves as dispatch bwd」。前向里 dispatch 把 token 打散成 K 份、combine 加权归并；反向传播恰好需要逆操作——对每个 token，把它 K 份散布在各 rank 的梯度拷贝**求和**回 token-major 梯度。这在数学上与 combine fwd 完全同构（把「专家输出」换成「梯度」），所以同一个内核、同一套 plan 直接复用。`api.py` 模块头部的四象限用法把这一对偶写成了对称的两行注释。

#### 4.4.2 核心流程

combine 在 `Buffer` 编排中的完整位置：

```text
Buffer.combine(plan, hidden_nvsh, route_weights_nvs, ...):
  ├─ (可选) inter_rank_sync            # 预同步发令枪（默认开）
  ├─ staging:
  │    zero_copy=False      → hidden_buf_local.copy_(hidden_nvsh)      # 普通张量搬进 shard
  │    router_weights_zero_copy=False → weights_buf_local.copy_(...)
  ├─ launch_combine_prologue(pdl_trigger=enable_pdl)   # 本地重复组 fp32 归约，结尾放行 PDL
  └─ launch_combine(pdl_launch=enable_pdl):
       ├─ pdl_wait_predecessor()        # 等 prologue 网格完成并冲刷写
       ├─ cross_rank_barrier(...)       # 入口全组汇合：发布 staged+accumulated 写
       ├─ 三段式流水线（4.2）+ 权重收集（4.3）
       └─ 每个 CTA 处理完自己的 token 段后退出
```

`cross_rank_barrier` 内部（引用 u3-l6 的结论，不重新推导）：`grid_sync` → 仅 block 0 的前 `num_ranks` 个线程向每个 peer 的相位槽发 `±1`（`red.release.sys`，跨 GPU 系统级可见）→ 自旋等本 rank 槽回到目标值 → 再 `grid_sync`；出入口分别是 `fence.proxy.alias`（把本线程此前的对称内存别名写发布成单播可读）与 `fence.proxy.async.global`（把屏障获得的数据桥接到后续 TMA 代理读）。

#### 4.4.3 源码精读

combine 入口的两行代码——PDL 等待与跨 rank 屏障，先于一切数据通路：

- [moonep/combine.py:L234-L242](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L234-L242) `if cutlass.const_expr(self.pdl_launch): pdl_wait_predecessor()` 之后立刻 `cross_rank_barrier(meta_tensor, meta_stride, barrier_off, rank, num_ranks, bar_tensor.iterator, Int32(self.num_sms), num_threads, tidx0)`——注释「entry barrier: every peer rank's combine has reached us」。

屏障原语的代理桥契约（引用自 `_common.py`）：

- [moonep/_common.py:L274-L346](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L274-L346) `cross_rank_barrier` 的文档字符串写明双 fence 语义：入口 `fence.proxy.alias` 是写者侧桥（此前的对称内存写以单播别名可读的状态发布），出口 `fence.proxy.async.global` 是消费者侧桥（屏障获得的数据对随后的 `cp.async.bulk` 可见）——**所有线程都要执行两道 fence**。[L304-L307](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L304-L307) 还有硬性断言 `num_threads >= num_ranks`（block 0 要能一次性给每个 peer 发信号；combine 的 192/224 线远大于典型 R）。

prologue 侧的设计声明——「我没有屏障，combine 才有」：

- [moonep/combine_prologue.py:L15-L18](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L15-L18) 「Launch it immediately before `launch_combine` on the same stream; the combine kernel performs the cross-rank publish barrier at entry before consuming peer ranks' staged rows」；[L77-L79](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L77-L79) 「This kernel has no cross-rank barrier … `combine` publishes the local NVL shard with an entry `cross_rank_barrier` before reading remote rows」。

宿主编排——屏障位置与 staging 的关系被注释一语道破：

- [moonep/api.py:L663-L696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L663-L696) `_run_combine_on_current_stream`：可选 `launch_inter_rank_sync` → `zero_copy=False` 时 `hidden_buf_local.copy_(hidden_nvsh)`（权重同理）→ `launch_combine_prologue(ctx, plan, pdl_trigger=self.enable_pdl)` → `launch_combine(ctx, hidden_sh, plan.dst, output_sk=route_weights_sk, pdl_launch=self.enable_pdl)`。[L676-L677](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L676-L677) 注释「combine's own entry cross_rank_barrier publishes the staged + accumulated NVL writes」。

dispatch bwd 对偶——模块头部的四象限注释：

- [moonep/api.py:L31-L36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L31-L36) 「combine bwd: re-dispatch the output grad with the saved plan」对应 `buffer.dispatch(grad_output_sh, plan=plan)`；「dispatch bwd: combine the hidden grad back to token-major」对应 `buffer.combine(plan=plan, hidden_nvsh=grad_hidden_nvsh)`——注意命名按**效果**取：`combine` 的反向是再 dispatch，`dispatch` 的反向是一次 combine。plan 在四个象限间复用，无需重新规划。
- [moonep/api.py:L961-L965](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L961-L965) `combine` 文档字符串「Also serves as dispatch bwd: combining `grad_hidden_nvsh` sums each token's K dispatched grad copies back to its token-major grad」。

宿主断言集——公共入口与内核入口两道关卡：

- [moonep/api.py:L999-L1020](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L999-L1020) `Buffer.combine` 断言 plan 类型、`hidden_nvsh` 为 `[NvS, H]` 连续 bf16、`route_weights_nvs` 为 `[NvS]` 连续 fp32；`zero_copy=True` 时用 `data_ptr()` 精确断言输入就是 dispatch 返回的 shard 视图。
- [moonep/combine.py:L595-L606](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L595-L606) `launch_combine` 再断言各张量 dtype/连续性，以及 `H % ACC_THREADS == 0`——注释说明这同时覆盖了「寄存器整分」与「16 字节 bulk 拷贝对齐」两个要求。

非法输入的测试口径：

- [tests/test_combine.py:L347-L376](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L347-L376) 逐一验证：传已被移除的 `hidden_sh` 参数抛 `TypeError`；缺 plan、缺输入、`route_weights_nvs` 形状给成 `[S, K]`（正确是 `[NvS]`）、`output_sk` 给 bf16 均抛断言错误。

#### 4.4.4 代码实践

**实践目标**：读懂官方测试的编排顺序，解释 identity 测试的参考值为何是 `hidden * K`，并理解精度口径。

**操作步骤**：

1. 阅读 [tests/test_combine.py:L116-L139](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L116-L139) 的 `_combine_full`：注意它复现了 `Buffer.combine` 的编排——`launch_inter_rank_sync` → NVL shard 拷贝（模拟 `zero_copy=False`）→ `launch_combine_prologue` → `launch_combine`。
2. 阅读 [tests/test_combine.py:L211-L221](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L211-L221) `test_combine_identity_round_trip`：专家 FFN 被置为恒等变换（dispatch 后 shard 里的行就是 token 行本身），参考值取 `ref = (hidden.float() * case.K).to(torch.bfloat16)`，用 `assert_close_all_ranks`（默认 `atol=0.05`，见 [tests/kernel_test_utils.py:L300-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L300-L303)）比较。
3. 思考：为什么这里用 0.05 容差而不是 `torch.equal`，而权重测试（4.3.3）却用 `torch.equal`？
4. 有 8 卡 NVLink 环境时运行：`torchrun --nproc_per_node=8 -m pytest -s tests/test_combine.py`（文件头部 [L3-L4](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L3-L4) 给出的官方命令）。

**需要观察的现象**：payload 断言与权重断言的严格程度差异；大形状用例 `large_hidden_stride_identity`（S=8192、K=16、epn=14、H=7168，[L72-L82](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L72-L82)）专测 int64 行偏移路径。

**预期结果**：全部测试通过。精度差异的来源：payload 链路上有**两次 bf16 舍入**——prologue 把组和以 fp32 算出后写回 bf16 主行（第一次），combine 累加后再转 bf16（第二次），而参考值 `hidden.float() * K` 只有一次最终舍入；权重链路是纯 4 字节位搬移、无任何算术，所以可以逐位比较。若无多卡环境，本实践退化为源码阅读，结论可由第 5 节的 CPU 模拟脚本验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：把入口屏障从 combine 挪到 prologue 出口（prologue 结尾加一次 `cross_rank_barrier`、combine 去掉入口屏障），功能上是否等价？性能上有什么差别？

**答案**：功能上看似等价（都能保证 staging+归约先于远端读），但破坏了 PDL 的收益：prologue 结尾的 `pdl_trigger_dependents` 本可在归约收尾时提前启动 combine 的 CTAs，把启动延迟藏进 prologue 执行时间里；屏障挪到 prologue 出口后，全组自旋等待发生在 combine 启动之前，启动开销与等待串行相加。屏障放在 combine 入口（`pdl_wait_predecessor` 之后），等待期间 combine 的 CTAs 已经驻留 SM。此外 combine 入口屏障紧跟 `fence.proxy.async.global`，与随后的 TMA 读在程序序上零距离，代理桥的覆盖范围最紧凑。

**练习 2**：`inter_rank_sync`（默认开启）与入口 `cross_rank_barrier` 是否重复？

**答案**：不重复，它们防的是不同的偏斜。`inter_rank_sync` 是**启动前**的预同步发令枪（u3-l6 讲过）：消除各 rank 从宿主流进入通信序列的启动偏差，让后续内核大致对齐；入口 `cross_rank_barrier` 是**数据**屏障：保证每个 rank 的 staging+prologue 写对其他 rank 可见。前者是性能对齐（且在 `zero_copy=True`、FFN 原地写 shard 的场景下，它还覆盖了 FFN 写完成这一更早的生产者），后者是正确性必需。api.py 的编排里两者各司其职（[moonep/api.py:L676-L689](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L676-L689)）。

**练习 3**：为什么说「combine 同时充当 dispatch bwd」而不需要任何内核改动？

**答案**：dispatch fwd 的数学作用是把 token 行复制/散射成 K 份 expert-grouped 行；其反向需要的恰是「把每个 token 的 K 份梯度行求和回一行」——这正是 combine fwd 的定义（对 `hidden_nvsh` 换成 `grad_hidden_nvsh` 逐字成立）。目的槽位、去重编码、权重散射在反向里语义不变（梯度也是「每 top-k 一份」的数据），因此同一个 `CombineKernel` 加同一份 plan 就完成了反向，`grad_hidden_sh, _, _ = buffer.combine(plan=plan, hidden_nvsh=grad_hidden_nvsh)` 一行即可（[moonep/api.py:L36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L36)）。

## 5. 综合实践

**任务**：编写 `combine_reference.py`——一个纯 CPU、单进程的 PyTorch 参考实现，从零模拟「dispatch 散射 → 用户专家计算（恒等）→ prologue 归约 → combine 求和 + 权重收集」全链路，并用与 `tests/test_combine.py` 相同口径的断言自检。这是本讲规格要求的实践：**用 PyTorch 写 combine 的参考实现（按 dst 分组累加回 token-major，并支持 route_weights 收集），与 tests/test_combine.py 的断言口径对齐**。

完整脚本（示例代码，只需 `torch`，CPU 可跑）：

```python
# combine_reference.py — 单进程模拟 MoonEP combine 语义（示例代码）
import torch

torch.manual_seed(0)
R, S, K, H = 2, 4, 3, 8   # 2 rank、4 token、top-3；R=2 且 K=3 时由鸽笼原理必有重复组
NvS_padded = 16           # 物理槽位（含 padding，模拟 NvS_padded；简化取 NvS == NvS_padded）
NvS = NvS_padded

# ---- 1. 手工构造路由目的 rank：每个 token 的 K 个条目各落到哪个 rank ----
# token0: k0,k1->rank1（重复组，k0 为主）; token1: k0,k2->rank0; token2: k0,k1->rank0; token3: k0,k2->rank1
dest = torch.tensor([[1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1]])

# ---- 2. 分配槽位并生成 dst（发送端规范化：组内最小 k 非负，其余 -raw-1）----
next_loff, dst, primary = [0, 0], torch.empty(S, K, dtype=torch.int32), {}
for s in range(S):
    for k in range(K):
        d = int(dest[s, k])
        loff = next_loff[d]; next_loff[d] += 1
        if (s, d) in primary:
            dst[s, k] = -(d * NvS + loff) - 1        # 重复条目
        else:
            primary[(s, d)] = loff
            dst[s, k] = d * NvS + loff               # 主条目

# ---- 3. 模拟 dispatch（含 epilogue 补齐）：逐条目散射 payload 与权重 ----
hidden = torch.randn(S, H).to(torch.bfloat16)
weights = torch.rand(S, K, dtype=torch.float32)
shard = torch.zeros(R, NvS_padded, H, dtype=torch.bfloat16)   # 各 rank 的 NVL shard
wbuf = torch.zeros(R, NvS_padded, dtype=torch.float32)        # meta 的 WEIGHTS 区（rank-stride 展平）
for s in range(S):
    for k in range(K):
        v = int(dst[s, k]); raw = -v - 1 if v < 0 else v
        d, loff = raw // NvS, raw % NvS
        shard[d, loff] = hidden[s]        # 恒等专家：行值即 token 行
        wbuf[d, loff] = weights[s, k]     # 权重逐条目散射，不参与去重

# ---- 4. 模拟 combine prologue：每组的重复行 fp32 累加进主行（写回 bf16，一次中间舍入）----
for s in range(S):
    by_rank = {}
    for k in range(K):
        v = int(dst[s, k]); raw = -v - 1 if v < 0 else v
        by_rank.setdefault(raw // NvS, []).append(raw % NvS)
    for d, loffs in by_rank.items():
        acc = torch.zeros(H, dtype=torch.float32)
        for loff in loffs:
            acc += shard[d, loff].float()
        shard[d, loffs[0]] = acc.to(torch.bfloat16)   # loffs[0] = 组内最小 k 的槽 = 主槽

# ---- 5. combine 参考实现：只加载非负 dst，fp32 累加 → bf16（对齐 CombineKernel 主路径）----
out = torch.empty(S, H, dtype=torch.bfloat16)
for s in range(S):
    acc = torch.zeros(H, dtype=torch.float32)
    for k in range(K):
        v = int(dst[s, k])
        if v >= 0:                        # 负条目跳过 payload
            acc += shard[v // NvS, v % NvS].float()
    out[s] = acc.to(torch.bfloat16)

# ---- 6. 权重收集参考实现：负条目照常解码（对齐 warp 6）----
out_sk = torch.empty(S, K, dtype=torch.float32)
for s in range(S):
    for k in range(K):
        v = int(dst[s, k]); raw = -v - 1 if v < 0 else v
        out_sk[s, k] = wbuf[raw // NvS, raw % NvS]

# ---- 7. 对拍：恒等专家下真值 = K 份 token 行之和（identity 测试口径）----
ref = torch.zeros(S, H, dtype=torch.float32)
for _ in range(K):
    ref += hidden.float()
ref = ref.to(torch.bfloat16)

print("dst（含负数编码）:\n", dst)
print("payload max err:", (out.float() - ref.float()).abs().max().item())   # 口径: assert_close_all_ranks, atol=0.05
print("weights round-trip equal:", torch.equal(out_sk, weights))            # 口径: assert_tensor_equal_all_ranks
```

**验证要点**（与官方口径逐条对齐）：

1. **payload**：`max err < 0.05`——对应 `assert_close_all_ranks` 的默认 `atol=0.05`（[tests/kernel_test_utils.py:L300-L303](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L300-L303)）。误差不为 0 的根源是 prologue 的中间 bf16 舍入（脚本第 4 步），这与真实内核行为一致。
2. **权重**：`torch.equal` 严格相等——对应 `assert_tensor_equal_all_ranks` 的逐位比较（[tests/kernel_test_utils.py:L133-L150](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L133-L150)），验证「dispatch 散射 + combine 收集 = 恒等」。
3. **官方参考对照**：把你第 5 步的实现改成「对每个 k（含负条目）都解码并累加」，应得到相同结果——这正是 [tests/test_combine.py:L199-L208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L199-L208) `_combine_global_reference` 的写法（它模拟的 shard 中重复槽也有值，等价于 prologue 前的全量状态；两种视角在 prologue 语义下数学等价）。
4. **有硬件时**：运行 `torchrun --nproc_per_node=8 -m pytest -s tests/test_combine.py`，六个测试（identity、global reference×2、output_sk、public staging、async、large stride、bad inputs）全绿即端到端确认。

**预期结果**：CPU 脚本打印的 `payload max err` 在 1e-2 量级以内、`weights round-trip equal: True`。脚本为确定性计算（固定种子），结果可复现；GPU 对拍部分待本地验证。

## 6. 本讲小结

- `CombineKernel` 是 6/7-warp 特化的三段式流水线：warp 0 经 `cp.async.bulk` G2S 按行拉取远端 NVL 行（mbarrier 事务计数）、warps 1–4 在寄存器里做 fp32 K 求和、warp 5 经 S2G bulk_group 写回，warp 6（可选）独立收集路由权重。
- 两条管道 + 一条 `NamedBarrier(id=8, 128 线)` 子块同步构成并发骨架：`load_pipe` 的消费者组大小 4 对应每 ACC warp 的 lane-0 signalling；`out_pipe` 双缓冲配 warp 5 的滞后释放；fp32 累加器全在寄存器，smem 只存 bf16 行缓存，深度随 H 自适应选档。
- 负数 dst 契约在接收端是「一跳一收」：载荷路径由 warp 0 与 ACC 用**同一个谓词**双侧判定、整体跳过（prologue 已把组和归约进主行）；权重路径在 warp 6 无差别解码 `-raw-1` 逐条收回——权重是 per-topk 量，永不参与去重。
- 入口 `cross_rank_barrier` 是正确性必需（发布 staged + accumulated 写、双 proxy fence 桥接 TMA），放在 combine 而非 prologue 是因为 prologue 只写本地、combine 是第一个远端消费者，且可与 PDL 启动重叠等待。
- 同一个 combine 内核天然充当 dispatch bwd：把 K 份梯度求和回 token-major 与 fwd 的归并数学同构，plan 四象限复用。
- 测试口径：payload 用 atol=0.05（两次 bf16 舍入），权重用逐位相等（纯位拷贝）；`large_hidden_stride_identity` 专测 int64 行偏移。

## 7. 下一步学习建议

本讲完成后，u4 通信内核单元（dispatch → epilogue → prologue → combine）全部结束，四条 warp 特化流水线已经闭环。建议：

1. **进入 u5 单元**：学 [moonep/prefetch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py)——权重预取内核是又一个持久化 CTA + 2D TMA 流水线的范例，与本讲的按行 TMA 对照阅读能加深对 tile 化 TMA 的理解；随后是镜像布局的 [moonep/grad_reduce.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py)。
2. **横向复习**：重读 [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py) 的消费者/生产者角色分配，注意 dispatch 是「S2G 为主、直写远端」，combine 是「G2S 为主、远端拉取」——方向相反决定了屏障位置的不同（dispatch 在**出口**发布，combine 在**入口**汇合）。
3. **工程视角**：u6-l1 将把 `launch_combine` 放进 comm stream 与 CUDA 事件握手的异步框架中（`async_finish=True` 路径），理解四个 API 共享一条通信流的串行化含义。
