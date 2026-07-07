# 性能基准与配置搜索

## 1. 本讲目标

FA4 的 kernel 写得好不好，最终要落到两个问题上：**它跑得有多快**，以及**它的 tile/线程/级数等参数是否最优**。本讲把「测」和「调」这两件事的工程链路讲清楚。学完本讲你应当能够：

- 用仓库自带的计时工具测出一次前向/反向的耗时，并据此算出 **TFLOPs**（每秒万亿次浮点运算）和 **MFU**（硬件算力利用率）。
- 读懂 `sm90_config_search.py` 这台「可行性枚举机」：它如何在不碰 GPU 的情况下，把上百种 tile/原子布局/流水级数组合砍到只剩硬件放得下的少数几种。
- 理解搜索结果是怎样**回填**到 `interface.py` 的 `_tile_size_fwd_sm90` / `_tile_size_bwd_sm90` 查找表里的，以及为什么这是一张「人工微调表」而非运行时自动搜索。

本讲是专家层的「度量与调参」主题，承接 [u2-l2 架构分发与 tile 配置选择](u2-l2-arch-dispatch-and-config.md)。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **TFLOPs / MFU**：TFLOPs（Tera FLOPs per second）= 每秒完成的 \(10^{12}\) 次浮点运算；MFU（Model FLOPs Utilization）= 实测 TFLOPs ÷ 该 GPU 的峰值 TFLOPs，是一个 0~1（或百分比）的「打了多少折」指标。例如 H100 SXM5 fp16 峰值约 989 TFLOPs，若实测 600 TFLOPs，则 MFU≈61%。
- **FLOPs 计数约定**：一次「乘加」（multiply–accumulate, MAC）按 **2 次浮点运算**计数（一次乘、一次加）。这是 GPU 性能报告的通用约定，FA4 的 `flops()` 函数里那个系数 `2` 就是它。
- **tile / 级数 / warp-group**：来自 u2-l2、u6。前向 kernel 把 Q/K/V 切成 tile 在共享内存里搬运，K/V 各占若干「级」（stages）做循环缓冲流水；Hopper/Blackwell 用 warp-group（wg，128 线程）协同算一次 MMA。`num_wg` 是参与 MMA 的 warp-group 数，`num_threads = (num_wg + 1) * 128`（多出来的 1 个 wg 当 producer 搬数据）。
- **寄存器 / 共享内存预算**：H100 每线程最多 256 个 32 位寄存器，但 warp-group MMA 会预留一部分；每个 CTA 的共享内存上限约 228 KB。tile 越大、级数越多，寄存器和共享内存吃得越凶——配置搜索就是在这些预算里「塞最大的 tile」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/benchmark.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark.py) | 计时工具箱：`benchmark_forward` / `benchmark_backward` / `benchmark_combined` / `benchmark_memory`，封装 `torch.utils.benchmark.Timer`。 |
| [flash_attn/cute/bench_utils.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/bench_utils.py) | 度量算子：`flops()` 算浮点运算量、`bandwidth_fwd_bytes/bwd_bytes` 算 HBM 流量、`attention_ref` 参考实现、cuDNN 图构建助手。 |
| [flash_attn/cute/sm90_config_search.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py) | 静态可行性枚举：遍历 tile/原子布局/级数组合，按 GMMA 整除性、寄存器、共享内存预算裁剪，输出候选配置表。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 回填落点：`_tile_size_fwd_sm90` / `_tile_size_bwd_sm90` 把人工微调后的最优配置编成查找表，运行时按 head_dim/causal 取用。 |
| [flash_attn/cute/benchmark_flash_attention_fp8.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark_flash_attention_fp8.py) | 示范脚本：把计时工具与 `flops`/`efficiency` 拼起来，打印每个配置的 TFLOPs 与毫秒数。 |

---

## 4. 核心概念与源码讲解

### 4.1 基准测量与 TFLOPs / MFU

#### 4.1.1 概念说明

「测一个 kernel 快不快」听起来简单，坑却不少：

1. **首次调用包含 JIT 编译**（见 [u11-l1](u11-l1-jit-and-cache.md)），第一次跑会慢几十秒，绝不能计入。
2. **CUDA 是异步的**：Python 里 `fn()` 返回时 kernel 可能还没跑完，必须 `synchronize` 后再读时间，否则测的是「发射时间」而非「执行时间」。
3. **需要预热（warmup）和多次重复**：单次 timing 受噪声影响大，要跑很多轮取统计量。
4. **GPU 会动态降频/升频**：冷启动与时钟爬坡会让前几轮偏慢。

FA4 用 PyTorch 自带的 `torch.utils.benchmark.Timer` 来一揽子解决后三个问题（它会自动同步、多次 `timeit`、返回带统计的 `Measurement`）。编译问题则由调用方自己负责（先空跑一次 warmup 触发编译）。

#### 4.1.2 核心流程

一次标准前向基准的流程是：

```
1. 构造 q/k/v（确保已触发 JIT 编译：先空跑一次当 warmup）
2. benchmark_forward(flash_attn_func, q, k, v, causal=True, repeats=30)
     └─ 内部用 Timer(...).timeit(30) 自动同步 + 多轮计时
     └─ 返回 Measurement m，取 m.mean 得到平均秒数 t
3. flops(...) 算出该形状的浮点运算总量 F
4. TFLOPs = F / t / 1e12
5. MFU = TFLOPs / GPU峰值TFLOPs
```

反向稍微绕一点：要先正向跑一次拿到输出 `y`、造一个随机的 `grad`，再在计时循环里反复「清 `.grad` → `backward(grad, retain_graph=True)`」。

#### 4.1.3 源码精读

**计时工具 `benchmark_forward`**：用 `torch.utils.benchmark.Timer` 包装任意可调用对象，调用 `.timeit(repeats)` 跑 `repeats` 轮，返回 `(timer, measurement)`。

[flash_attn/cute/benchmark.py:8-27](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark.py#L8-L27) —— 用 PyTorch Benchmark 计时任意函数的前向：

```python
def benchmark_forward(fn, *inputs, repeats=10, desc="", verbose=True, amp=False, amp_dtype=torch.float16, **kwinputs):
    def amp_wrapper(*inputs, **kwinputs):
        with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=amp):
            fn(*inputs, **kwinputs)
    t = benchmark.Timer(
        stmt="fn_amp(*inputs, **kwinputs)",
        globals={"fn_amp": amp_wrapper, "inputs": inputs, "kwinputs": kwinputs},
        num_threads=torch.get_num_threads(),
    )
    m = t.timeit(repeats)
    ...
    return t, m
```

要点：`stmt` 是字符串、`globals` 把真实对象塞进计时作用域；`.timeit(repeats)` 内部已处理同步与多轮，返回的 `m` 有 `.mean` / `.median` 等属性。`amp_wrapper` 让你可以选择用 autocast 跑。

**反向计时 `benchmark_backward`**：先正向算出 `y`，再造 `grad`，计时循环里反复清梯度后反传。

[flash_attn/cute/benchmark.py:30-69](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark.py#L30-L69) —— 关键是先把 `y = fn(...)` 拿出来，并在每次反传前 `x.grad = None` 以免梯度累加污染计时：

```python
with torch.autocast(...):
    y = fn(*inputs, **kwinputs)
    if type(y) is tuple:
        y = y[0]                       # FA4 的 flash_attn_func 返回 (out, lse)
if grad is None:
    grad = torch.randn_like(y)
def f(*inputs, y, grad):
    for x in inputs:
        if isinstance(x, torch.Tensor):
            x.grad = None              # 避免梯度累加带来的额外开销
    y.backward(grad, retain_graph=True)
```

注意 FA4 的 `flash_attn_func` 恒返回元组 `(out, lse)`（见 u2-l1），所以这里 `if type(y) is tuple: y = y[0]` 取出输出张量来造 `grad`。`benchmark_combined`（[L72-114](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark.py#L72-L114)）则把前向+反向塞进同一个计时函数，反映训练场景的真实步开销。

**FLOPs 计算 `flops()`**：标准注意力的浮点量来自两个矩阵乘：\(S = QK^\top\)（规约维 = headdim）和 \(O = PV\)（规约维 = headdim_v）。

[flash_attn/cute/bench_utils.py:15-47](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/bench_utils.py#L15-L47) —— 核心是这行：

```python
eff_headdim = headdim + headdim_v if has_qv else headdim
return batch * nheads * 2 * seqlen_q * avg_seqlen * (eff_headdim + headdim_v)
```

拆解：每个 (batch, head) 的注意力等于 \(2 \cdot \text{seqlen\_q} \cdot \text{seqlen\_k} \cdot (\text{headdim} + \text{headdim\_v})\) 次浮点运算（两个 GEMM，`2` = MAC 折算）。普通注意力里 `eff_headdim = headdim`，于是括号里是 `(headdim + headdim_v)`；MLA 吸收式（`has_qv=True`，见 u10-l2）多一项 \(Q_v V^\top\) 进得分，故 `eff_headdim = headdim + headdim_v`，括号变成 `(headdim + 2*headdim_v)`。

`avg_seqlen` 处理因果/滑窗的平均有效 KV 长度：因果时三角形的平均宽度是 \((\max(0,\, \text{seqlen\_k}-\text{seqlen\_q}) + \text{seqlen\_k})/2\)；全连接时就是 `seqlen_k`；滑窗时逐行算窗口宽再取均值（[L26-45](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/bench_utils.py#L26-L45)）。

**带宽 `bandwidth_fwd_bytes` / `bandwidth_bwd_bytes`**：算一次前向/反向读写 HBM 的字节数，用来评估 kernel 是「算力受限」还是「带宽受限」。

[flash_attn/cute/bench_utils.py:53-71](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/bench_utils.py#L53-L71) —— 前向 = 读 Q/K/V + 写 O：

```python
def bandwidth_fwd_bytes(..., dtype_bytes=2, has_qv=False, shared_kv=False):
    q = batch * nheads * seqlen_q * headdim
    k = batch * nheads_kv * seqlen_k * headdim
    v = batch * nheads_kv * seqlen_k * headdim_v if not shared_kv else 0
    o = batch * nheads * seqlen_q * headdim_v
    return (q + qv + k + v + o) * dtype_bytes
```

`dtype_bytes=2` 对应 fp16/bf16（每元素 2 字节）。把它除以耗时得到 GB/s，再与 GPU 的 HBM 带宽峰值比，就知道注意力是否卡在访存上（解码/短序列往往带宽受限，长序列算力受限）。

**示范：把计时与 FLOPs 拼成 TFLOPs**。`benchmark_flash_attention_fp8.py` 给出了最简洁的拼法。

[flash_attn/cute/benchmark_flash_attention_fp8.py:73-85](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark_flash_attention_fp8.py#L73-L85) —— 这是「hopper 基准的简化约定」（假设 `headdim==headdim_v`、`seqlen_q==seqlen_k`）：

```python
def flops(batch, seqlen, headdim, nheads, causal):
    return 4 * batch * seqlen**2 * nheads * headdim // (2 if causal else 1)

def efficiency(flop, seconds):
    return (flop / seconds / 1e12) if not math.isnan(seconds) else 0.0

def time_fwd(fn, *args, repeats, **kwargs):
    time.sleep(1)                       # 等 GPU 时钟稳定，减少残余降频
    _, m = benchmark_forward(fn, *args, repeats=repeats, verbose=False, **kwargs)
    return float(m.mean)
```

`4 = 2(MAC) × 2(两个 GEMM 都用 headdim)`；因果除以 2（三角形）。注意 `time.sleep(1)`——在两次基准之间睡 1 秒，等 GPU 频率爬上来，避免「前一组的余热」影响下一组。打印处见 [L414-420](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/benchmark_flash_attention_fp8.py#L414-L420)：`speeds[method] = efficiency(flops(...), t)` 后输出 `{TFLOPs/s} fwd: {x:.2f} TFLOPs/s, {t*1e3:.3f} ms`。

#### 4.1.4 代码实践

**实践目标**：用仓库自带工具测出 FA4 前向的 TFLOPs，体会「编译期不计入、多次计时取均值、除以 1e12」的全流程。

**操作步骤**（示例代码，可在装好 FA4 的 GPU 机器上运行）：

```python
# bench_tflops_demo.py —— 示例代码（非仓库原有文件）
import math, torch
from flash_attn.cute import flash_attn_func
from flash_attn.cute.benchmark import benchmark_forward

def flops(batch, nheads, seqlen, headdim, causal):
    # 两个 GEMM（QK 与 PV），headdim==headdim_v，MAC=2
    return 4 * batch * nheads * seqlen**2 * headdim // (2 if causal else 1)

dtype, d, h = torch.float16, 128, 16
for seqlen in (512, 1024, 2048, 4096, 8192):
    q = torch.randn(2, seqlen, h, d, dtype=dtype, device="cuda")
    k = torch.randn_like(q); v = torch.randn_like(q)
    _ = flash_attn_func(q, k, v, causal=True)        # warmup：触发 JIT 编译，不计入
    torch.cuda.synchronize()
    _, m = benchmark_forward(flash_attn_func, q, k, v, causal=True, repeats=30, verbose=False)
    t = float(m.mean)
    tf = flops(2, h, seqlen, d, True) / t / 1e12
    print(f"seqlen={seqlen:5d}  {t*1e3:7.3f} ms  {tf:6.1f} TFLOPs/s")
```

**需要观察的现象**：随 `seqlen` 增大，单步耗时近似线性增长（因 causal 下 FLOPs ∝ seqlen²，而耗时也 ∝ seqlen²，故 TFLOPs 趋于稳定并接近饱和）；短序列（如 512）TFLOPs 明显偏低（带宽受限、SM 喂不饱）。

**预期结果**：在 H100 上、fp16/hdim128/causal，长序列前向 TFLOPs 通常落在 500~700 区间（MFU 约 50%~70%，峰值按 ~989 TFLOPs 计）。具体数值**待本地验证**（取决于具体卡型、驱动、时钟）。

> 提示：如果你没有 GPU，本实践可改为**源码阅读型**——阅读 `benchmark_forward` 与 `efficiency`，说明为什么必须先 warmup 再 `timeit`，以及为什么 `m.mean` 比单次 `time.time()` 计时更可信。

#### 4.1.5 小练习与答案

**练习 1**：`flops()` 里那个系数 `2` 来自哪里？如果只算「乘法次数」（不把加法算进去），TFLOPs 会变成原来的几倍？

**答案**：`2` 来自 MAC 约定（一次乘 + 一次加 = 2 次浮点运算）。若只数乘法，FLOPs 减半，于是算出的 TFLOPs 也减半。

**练习 2**：为什么 `benchmark_backward` 要在计时函数里每次 `x.grad = None`？

**答案**：不清零的话，PyTorch 的梯度会**累加**到已有的 `.grad` 上，每轮都多一次「读旧梯度 + 加法 + 写回」的额外开销，且会让显存/带宽测量失真。清零保证每轮都是干净的「反传一次」。

**练习 3**：`time_fwd` 里为什么 `time.sleep(1)`？

**答案**：GPU 在连续高强度 kernel 之间会动态调频，刚跑完一组后时钟可能还没稳定；睡 1 秒让频率回到标称值，减少「前一组的余热」对下一组计时的污染，使多次测量更具可比性。

---

### 4.2 tile / 级数配置遍历（sm90_config_search.py）

#### 4.2.1 概念说明

Hopper（SM90）前向/反向 kernel 有大量「旋钮」可以拧：

- **tile 尺寸** `tile_m`（Q 块行数）、`tile_n`（KV 块列数）：越大算术强度越高，但寄存器/共享内存越吃紧。
- **warp-group 数** `num_wg`：2 或 3，决定并发度与每 wg 分到的寄存器。
- **swap_AB**：GEMM 的 A/B 是否交换主向（影响 GMMA 原子是否可用、寄存器布局）。
- **原子布局** `AtomLayoutM/N`：决定 M/N 维如何切给各 wg。
- **流水级数** `num_stages`：K/V（及 Q/dO）在共享内存里的缓冲份数。
- **`pv_is_rs`**（前向）：P@V 的 P 是来自寄存器（RS）还是共享内存（noRS）。
- **`overlap_wg`**（前向）：是否跨 wg 重叠 QK 与 PV 两段 GEMM。

全组合是数百到上千种。但其中绝大多数**硬件根本放不下**（寄存器爆、共享内存爆、GMMA 整除性不满足）。`sm90_config_search.py` 是一台**静态可行性枚举机**：它不跑 GPU，纯靠算术约束把候选集砍到只剩「放得下」的几十种，再按一个性能代理指标（共享内存流量）排序，供工程师拿去真机 benchmark。

> 关键认知：这个脚本**不做最终选型**，只做「可行性裁剪 + 代理排序」。真正的最优配置是人对幸存者跑 benchmark 后挑出来的（见 4.3）。

#### 4.2.2 核心流程

前向枚举的流程：

```
for num_wg in (2, 3):                 # MMA warp-group 数
  for tile_n in (64,80,...,192):      # KV 块列数
    for pv_is_rs in (True, False):    # P 在寄存器 or 共享内存
      for overlap_wg in (True, False):
        cfg = _check_fwd_config(...)   # 用硬约束判定
        if cfg is not None:            # 放得下才保留
          results.append(cfg)
sort by (-tile_n, smem_traffic_per_block)   # 大 tile 优先，同等则流量小优先
```

`_check_fwd_config` 的三道闸：

1. **GMMA 整除性**：`tile_n % 8 == 0`（GMMA 的 N 步长是 8）；`tile_m = num_wg * 64`（GMMA 原子 M=64，每个 wg 负责 64 行）。
2. **寄存器预算**：累加器寄存器 = `tile_m*tile_n / (num_wg*128)`（每 wg 128 线程均分），S/O/P 三套累加器相加后不得超过 `REG_LIMITS[num_wg]`。
3. **共享内存预算**：`sQ + sK(2级) + sV(2级) + sO + sP`（sP 在 `pv_is_rs` 时为 0）不得超过 `SMEM_LIMIT`。

反向更复杂：要同时满足 **4 个 MMA**（SdP、dK、dV、dQ）各自的整除性与寄存器，峰值寄存器按 `max(2*regs_SdP, regs_dQ) + regs_dK + regs_dV` 估算。

#### 4.2.3 源码精读

**硬件预算常量**：H100 的共享内存与每 wg 寄存器上限。

[flash_attn/cute/sm90_config_search.py:15-17](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L15-L17) —— 这两个数是裁剪的硬地板：

```python
SMEM_LIMIT = 224 * 1024   # 228 KB minus ~3 KB for LSE, dPsum, mbarriers
REG_LIMITS = {2: 216, 3: 128}   # per-WG budget: 2WG=240-24, 3WG=160-32
THREADS_PER_WG = 128
```

解读：H100 每 CTA 共享内存 228 KB，但 FA4 还要留约 3 KB 给 LSE、dPsum、mbarrier 等元数据，故可用上限取 224 KB。寄存器方面，每线程物理上有 256 个 32 位寄存器，但 warp-group MMA（GMMA）会预留若干（2WG 模式预留 24、3WG 预留 32），于是每 wg 的「可分配给累加器」上限分别是 216 与 128。`THREADS_PER_WG = 128` 是一个 warp-group 的线程数（4 个 warp）。

**累加器寄存器估算 `_acc_regs`**：把 tile 的元素总数均摊到该 wg 的所有线程。

[flash_attn/cute/sm90_config_search.py:24-41](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L24-L41) —— 每个 GMMA 的累加器是片上的，按线程均分：

```python
def _acc_regs(M, N, num_wg):
    return M * N // (num_wg * THREADS_PER_WG)

def _check_mma(M, N, num_wg, atom_layout_m, swap_AB):
    if swap_AB:
        M, N = N, M
        atom_layout_m = num_wg // atom_layout_m
    atom_layout_n = num_wg // atom_layout_m
    if M % (atom_layout_m * 64) != 0 or N % (atom_layout_n * 8) != 0:   # GMMA 整除性
        return None
    return _acc_regs(M, N, num_wg)
```

`M % (atom_layout_m*64)` 与 `N % (atom_layout_n*8)` 就是 GMMA 原子的整除约束（M 原子=64、N 步长=8）。不满足直接返回 `None`，该组合被丢弃。

**前向可行性 `_check_fwd_config`**：三道闸齐下。

[flash_attn/cute/sm90_config_search.py:260-312](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L260-L312) —— 关键判定：

```python
tile_m = num_wg * 64                     # 闸1：GMMA M=64，每wg管64行
if tile_n % 8 != 0:                       # 闸1：N步长8
    return None
regs_S = _acc_regs(tile_m, tile_n, num_wg)
regs_O = _acc_regs(tile_m, hdimv, num_wg)
regs_P = regs_S // 2                       # bf16 累加器是 f32 的一半
total_regs = (regs_S + regs_P + regs_O) if overlap_wg else (regs_S + regs_O)
if total_regs > reg_limit:                 # 闸2：寄存器预算
    return None
# 闸3：共享内存（Q 单级、K/V 各2级、O 与 Q 复用、sP 仅 noRS 时存在）
sQ = tile_m * hdim * 2
sK = tile_n * hdim * 2 * 2
sV = tile_n * hdimv * 2 * 2
sO = tile_m * hdimv * 2
sP = tile_m * tile_n * 2 if not pv_is_rs else 0
smem = max(sQ, sO) + sK + sV + sP          # O 与 Q 生命周期重叠，取较大者
if smem > SMEM_LIMIT:
    return None
```

注意两个细节：① `regs_P = regs_S // 2`，因为 P 在共享内存里是 bf16（f32 累加器的一半位宽）；② `smem = max(sQ, sO) + ...`，因为输出 O 复用 Q 的共享内存区（生命周期不重叠），取较大者而非相加——这是节省共享内存的关键技巧。

**枚举驱动 `find_feasible_fwd_configs`**：四层嵌套 + 排序。

[flash_attn/cute/sm90_config_search.py:315-333](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L315-L333) —— 遍历并对幸存者排序：

```python
def find_feasible_fwd_configs(head_dim, head_dim_v=None, tile_n_choices=(64,80,...,192)):
    ...
    for num_wg in (2, 3):
        for tile_n in tile_n_choices:
            for pv_is_rs in (True, False):
                for overlap_wg in (True, False):
                    cfg = _check_fwd_config(hdim, hdimv, tile_n, num_wg, pv_is_rs, overlap_wg)
                    if cfg is not None:
                        results.append(cfg)
    results.sort(key=lambda c: (-c["tile_n"], c["smem_traffic_per_block"]))
    return results
```

排序键 `(-tile_n, smem_traffic_per_block)`：先按 tile_n 从大到小（大 tile 算术强度高、通常更快），同等 tile_n 下按「每块共享内存流量」从小到大（流量小意味着 smem→rmem 搬运少、更省带宽）。

**反向可行性 `_check_bwd_config`**：要同时满足 4 个 MMA。

[flash_attn/cute/sm90_config_search.py:61-107](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L61-L107) —— 反向有 SdP、dK、dV、dQ 四个 GEMM，每个都要过 GMMA 整除性，且峰值寄存器按生命周期最坏情况估：

```python
regs_SdP = _check_mma(tile_m, tile_n, num_wg, AtomLayoutMSdP, SdP_swapAB)
regs_dK  = _check_mma(tile_n, hdim,  num_wg, AtomLayoutNdKV,  dKV_swapAB)
regs_dV  = _check_mma(tile_n, hdimv, num_wg, AtomLayoutNdKV,  dKV_swapAB)
regs_dQ  = _check_mma(tile_m, hdim,  num_wg, AtomLayoutMdQ,   dQ_swapAB)
if any(r is None for r in (regs_SdP, regs_dK, regs_dV, regs_dQ)):
    return None
# 峰值：S 与 dP 同生命周期(2*regs_SdP)，与 dQ 取较大，再叠加常驻的 dK+dV
total_regs = max(2 * regs_SdP, regs_dQ) + regs_dK + regs_dV
if total_regs > reg_limit:
    return None
```

`max(2*regs_SdP, regs_dQ)` 反映反向里 S 与 dP 两套累加器同时存活（故乘 2），它与 dQ 取较大者（二者生命周期互斥），再加上常驻的 dK、dV 累加器。反向枚举维度更多（三个 swap_AB、三个 atom layout），见 [find_feasible_bwd_configs L174-221](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L174-L221)。

**命令行入口**：可直接 `python -m` 运行，无需 GPU。

[flash_attn/cute/sm90_config_search.py:367-403](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm90_config_search.py#L367-L403) —— 支持 `--mode fwd/bwd/both`、`--headdim 128` 或 `--headdim 192-128`（MLA 形状）、`--tile-n` 自定义候选：

```python
parser.add_argument("--mode", choices=["fwd", "bwd", "both"], default="both")
parser.add_argument("--headdim", type=str, default="128", help="Head dim, or hdim-hdimv (e.g. 192-128)")
...
print_fwd_configs(find_feasible_fwd_configs(hdim, hdimv, tn), args.num_results)
```

#### 4.2.4 代码实践

**实践目标**：亲手跑一次可行性搜索，理解「这是静态算术裁剪、不需要 GPU」。

**操作步骤**（纯 CPU 即可，因为脚本只做整数算术）：

```bash
python -m flash_attn.cute.sm90_config_search --mode fwd --headdim 128 -n 15
```

**需要观察的现象**：输出一张表，列出 `wg / tm / tn / RS / olap / rS / rP / rO / tot/limit / smem / traffic / tr-per-blk`。注意 `tot`（总寄存器）总是 ≤ `limit`，`smem` 总是 ≤ 224K；大 tile_n 的组合排在前面。

**预期结果**：对 hdim=128 前向，排在前面的通常是 `num_wg=2, tile_m=128, tile_n=128` 附近的组合——这与 `interface.py` 里 hdim128 选 `FwdConfig(128, 128, RS=True, OL=True)` 高度吻合（见 4.3）。再试 `--headdim 64`，观察 hdim64 是否允许更大的 `tile_n`（如 128 之外还出现 144/160）。

#### 4.2.5 小练习与答案

**练习 1**：`_check_fwd_config` 里 `smem = max(sQ, sO) + sK + sV + sP`，为什么 Q 和 O 用 `max` 而不是相加？

**答案**：Q tile 在 prologue 加载后、主循环里常驻；O 在 epilogue 才写回。二者生命周期不重叠，可以复用同一块共享内存，所以取较大者即可，不必同时预留——这是 FA4 节省共享内存、提升占用率（occupancy）的技巧。

**练习 2**：`REG_LIMITS = {2: 216, 3: 128}`，为什么 `num_wg=3` 的预算反而比 `num_wg=2` 小很多？

**答案**：寄存器预算是**每 wg** 的。3 个 wg 把物理寄存器文件分得更细，加上 GMMA 预留也更多（3WG 预留 32、2WG 预留 24），所以每 wg 拿到的更少（128 vs 216）。`num_wg` 增大带来并发收益，但每个 wg 能用的累加器 tile 变小，是个权衡。

**练习 3**：脚本最后 `results.sort(key=lambda c: (-c["tile_n"], c["smem_traffic_per_block"]))`，这个排序为什么只是「代理」而非「最终答案」？

**答案**：`smem_traffic_per_block` 是一个静态估算的访存代理，没考虑寄存器压力对占用率的影响、L2 命中、流水深度、MMA 异步重叠等真机因素。它只能把明显差的组合往后排；真正的最优要靠在 GPU 上 benchmark 幸存者才能确定。

---

### 4.3 搜索结果如何回填到 interface.py

#### 4.3.1 概念说明

4.2 的搜索只给「候选名单」，没说哪个最快。FA4 的做法是**离线人工选型**：

1. 用 `sm90_config_search.py` 列出某 `(head_dim, head_dim_v)` 下所有可行配置。
2. 工程师在真机（H100 SXM）上对候选逐一跑 `benchmark_forward`，记录 TFLOPs。
3. 把每个 head_dim 档位下的**赢家**硬编码进 `_tile_size_fwd_sm90` / `_tile_size_bwd_sm90` 两张查找表。
4. 运行时，`interface.py` 按 head_dim / causal / local 直接查表，得到 `FwdConfig` / `BwdConfig`，喂给 kernel。

所以「配置搜索结果回填」不是运行时自动搜索，而是**搜索 → 真机验证 → 沉淀为查找表**的工程闭环。这也解释了为什么 u2-l2 强调：tile/线程/级数都进 `compile_key`，改 head_dim 或 causal 会触发重编译——因为它们对应不同的硬编码配置。

#### 4.3.2 核心流程

```
sm90_config_search.py                真机 benchmark                 interface.py（运行时）
──────────────────────               ──────────────────             ─────────────────────────
find_feasible_fwd_configs(hdim)  →  对候选逐个跑 fwd, 记 TFLOPs  →  _tile_size_fwd_sm90(hdim, causal, local)
   ↓ 输出候选表                       ↓ 挑出每个 hdim 档位的赢家        ↓ 返回 FwdConfig(m, n, pv_is_rs, overlap)
   (静态, 无 GPU)                     (需 GPU)                         ↓
                                                                       dispatch: arch//10==9 → 用此 cfg
                                                                              ==8/12 → 小硬编码 cfg
                                                                              用户传 tile_mn → 覆盖
```

#### 4.3.3 源码精读

**`FwdConfig` 数据类**：就是配置的载体。

[flash_attn/cute/interface.py:115-120](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L115-L120) —— 四个字段对应 4.2 提到的旋钮：

```python
@dataclass(frozen=True)
class FwdConfig:
    m_block_size: int
    n_block_size: int
    mma_pv_is_rs: bool      # P@V 的 P 来自寄存器(RS)还是共享内存(noRS)
    intra_wg_overlap: bool  # 是否跨 wg 重叠 QK 与 PV
```

**前向查找表 `_tile_size_fwd_sm90`**：每个 head_dim 档位的「赢家」。

[flash_attn/cute/interface.py:123-155](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L123-L155) —— 注释直接交代了配置的血统（源自 FA3 C++ 的 `hopper/tile_size.h`，再「benchmarked on H100 SXM」针对 Python kernel 的寄存器/共享内存差异重调）：

```python
def _tile_size_fwd_sm90(head_dim, head_dim_v, is_causal, is_local, sparse_block_size_q=None):
    if head_dim <= 64:
        # Python: 192×128 RS+OL is consistently best across seqlens.
        return FwdConfig(192, 128, True, True)
    elif head_dim <= 96:
        # Python: RS is catastrophic with 192× tiles (~300 vs ~600 TFLOPS).
        # noRS+OL is always required. Causal: 192×128 slightly better short seqlen.
        if is_causal or is_local:
            return FwdConfig(192, 128, False, True)
        else:
            return FwdConfig(192, 144, False, True)
    elif head_dim <= 128:
        return FwdConfig(128, 128, True, True)
    elif head_dim <= 192:
        tile_n = 96 if is_local else (128 if head_dim_v <= 128 else 112)
        return FwdConfig(128, tile_n, True, True)
    else:  # hdim 256
        tile_n = 64 if is_local else 80
        return FwdConfig(128, tile_n, True, True)
```

这段是「搜索回填」最直白的证据：`# Python: RS is catastrophic with 192× tiles (~300 vs ~600 TFLOPS)` —— 工程师在真机上发现 hdim96 用 RS（P 在寄存器）会让 TFLOPS 从 ~600 暴跌到 ~300，于是把整个 hdim≤96 档位强制改成 noRS（`mma_pv_is_rs=False`）。这种**用 benchmark 结论覆盖理论直觉**的决策，只有真机搜索能给出。

**反向查找表 `_tile_size_bwd_sm90`**：同样的模式，字段更多（级数、三个 swap_AB、三个 atom layout、num_wg）。

[flash_attn/cute/interface.py:174-236](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L174-L236) —— 反向的 `BwdConfig` 字段正好对应 `sm90_config_search.py` 枚举的那些维度，例如 hdim≤64 返回：

```python
return BwdConfig(
    m_block_size=128, n_block_size=128,
    num_stages_Q=2, num_stages_dO=2, num_stages_PdS=2,
    SdP_swapAB=True, dKV_swapAB=False, dQ_swapAB=False,
    AtomLayoutMSdP=1, AtomLayoutNdKV=2, AtomLayoutMdQ=2,
)
```

这些值正是搜索脚本会枚举并校验的字段——每一行都是某个候选在真机上跑赢后被「钉」下来的。

**运行时分发**：按架构选不同路径，SM90 走查找表。

[flash_attn/cute/interface.py:523-547](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L523-L547) —— 注意 SM80/SM120 用小硬编码配置（128 线程、4 warp），SM90 才调查找表；用户可用 `tile_mn` 覆盖：

```python
if arch // 10 in [8, 12]:
    num_threads = 128                          # SM80/SM120: 4 warps
fwd_cfg = FwdConfig(128, 128, True, True)      # default
if tile_mn is None:
    if arch // 10 == 12:
        fwd_cfg = FwdConfig(128, 128, True, True) if head_dim <= 64 else FwdConfig(128, 64, True, True)
    elif arch // 10 == 8:
        fwd_cfg = FwdConfig(128, 64, True, True)   # SM80, should tune（尚未精调）
    elif arch // 10 == 9:
        sparse_q = get_sparse_q_block_size(block_sparse_tensors, seqlen_q)
        fwd_cfg = _tile_size_fwd_sm90(head_dim, head_dim_v, causal, local, sparse_block_size_q=sparse_q)
else:
    fwd_cfg = FwdConfig(tile_mn[0], tile_mn[1], fwd_cfg.mma_pv_is_rs, fwd_cfg.intra_wg_overlap)
```

两个看点：① SM80 那行注释 `# SM80, should tune` 诚实地说明 Ampere 路径还没做精调（只给了一个保守默认），这正是「搜索回填」尚未覆盖到的留白；② `tile_mn` 参数让高级用户能手动注入自定义 tile，绕过查找表——这是把搜索能力开放给运行时的「逃生口」。

**`num_threads` 与 `num_wg` 的联动**：反向里线程数由配置的 `num_wg` 推出。

[flash_attn/cute/interface.py:1396](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1396) —— SM90 反向的线程数 = (MMA wg 数 + 1 个 producer wg) × 128：

```python
num_threads = (cfg.num_wg + 1) * 128
```

即 `num_wg=2` → 384 线程，`num_wg=3` → 512 线程。这与 `sm90_config_search.py` 的 `REG_LIMITS` / `THREADS_PER_WG` 完全对应：搜索时按 `num_wg` 算寄存器预算，运行时按 `num_wg` 算线程数——两端用的是同一套 warp-group 模型。

#### 4.3.4 代码实践

**实践目标**：验证「查找表的选择 = 搜索脚本的头部候选」，体会搜索→回填的闭环。

**操作步骤**：

1. 运行 `python -m flash_attn.cute.sm90_config_search --mode fwd --headdim 128 -n 10`，记录排在最前的几个 `(num_wg, tile_m, tile_n, RS, olap)`。
2. 打开 [interface.py:148-149](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L148-L149)，确认 hdim128 档位写的是 `FwdConfig(128, 128, True, True)`（即 tile_m=128、tile_n=128、RS=True、OL=True）。
3. 比对：查找表的值是否落在搜索脚本的头部候选里？RS=True 是不是头部里 `RS=T` 的那一档？

**需要观察的现象**：查找表选的 `(128,128,RS,OL)` 正是搜索头部候选之一（num_wg=2 时 tile_m=2×64=128），说明人工选型确实落在「最大可行 tile」的邻域。

**预期结果**：二者吻合。这是「搜索结果回填」最直接的交叉验证，**待本地验证**（输出取决于具体 head_dim）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_tile_size_fwd_sm90` 是一张手写的 `if/elif` 表，而不是运行时调 `find_feasible_fwd_configs` 自动选？

**答案**：① 可行性搜索只给候选，不测真机性能，无法直接选出最快；② 运行时搜索会让首次调用更慢（要枚举+benchmark），与 FA4「首次慢、之后命中缓存」的模型冲突；③ 最优配置在不同 head_dim 下差异大但**固定**，离线沉淀成表后运行时是 O(1) 查表，最稳最快。

**练习 2**：注释 `# SM80, should tune` 透露了什么？

**答案**：SM80（Ampere）前向路径还没做完整的配置搜索与真机微调，只给了一个保守的 `(128, 64, RS, OL)` 默认。说明「搜索回填」是个持续工程，并非所有架构都已覆盖——SM80 是留白区，有兴趣的二次开发者可以照同样的流程（搜索 → 真机 benchmark → 改表）为它补上精调。

**练习 3**：用户传 `tile_mn=(192, 128)` 会发生什么？

**答案**：会走 [L541-542](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L541-L542) 的 `else` 分支，构造 `FwdConfig(192, 128, fwd_cfg.mma_pv_is_rs, fwd_cfg.intra_wg_overlap)`，**覆盖**查找表的结果（但保留默认的 RS/OL 标志）。由于 tile 进了 `compile_key`（见 u2-l2），换 tile 会触发重编译。这是把搜索能力暴露给运行时的逃生口，供高级用户实验自定义配置。

---

## 5. 综合实践

把本讲三件事（测、搜、回填）串成一个迷你调参实验：

**任务**：为一个给定形状寻找「比你机器默认更快的 tile」。

1. **测基线**。固定 `batch=2, h=16, d=128, dtype=fp16, causal=True`，对 `seqlen ∈ {1024, 4096, 8192}` 用 4.1.4 的脚本测出默认配置的 TFLOPs，作为基线。
2. **搜候选**。运行 `python -m flash_attn.cute.sm90_config_search --mode fwd --headdim 128 -n 12`，挑出排名前 3 的 `(tile_m, tile_n)` 组合（注意 `tile_m = num_wg*64`）。
3. **真机验证**。对每个候选，用 `tile_mn=(tile_m, tile_n)` 调用 `flash_attn_func`，按 4.1.4 的方法测 TFLOPs（**待本地验证**：注意换 tile 会触发重编译，第一次调用慢是正常的，需 warmup）。
4. **回填思考**。把测得的最优 tile 与 `_tile_size_fwd_sm90` 里 hdim128 的硬编码值 `(128,128)` 对比：一致吗？若你测出更好的，说明你的卡型/驱动与 H100 SXM 不同——这正是查找表「按硬件微调」的本质，也是为什么注释里反复强调 `benchmarked on H100 SXM`。
5. **画曲线**。用 matplotlib（或手画）把「seqlen → TFLOPs」画成曲线，标注默认配置与你找到的最佳 tile 两条线，观察长序列是否趋于饱和（算力受限）、短序列是否明显偏低（带宽受限）。

**交付物**：一张性能曲线图 + 一段结论，说明你机器上 hdim128/causal 的最优 tile 是什么，以及它与仓库查找表的异同。

> 若无 GPU，可降级为**源码阅读型综合实践**：通读 `sm90_config_search.py` 的前向路径，画出「输入 (hdim, hdimv) → 候选表 → 排序键」的数据流图，并解释为什么 `REG_LIMITS` 与 `_tile_size_fwd_sm90` 选择的 tile 满足 `total_regs ≤ reg_limit`（用 hdim128 的 `(128,128,num_wg=2,RS,OL)` 手算一遍 regs_S/regs_O/regs_P 与 total）。

## 6. 本讲小结

- **计时**靠 `benchmark.py` 封装的 `torch.utils.benchmark.Timer`：自动同步、多轮 `timeit`、返回带统计的 `Measurement`；反向要清 `.grad` 并 `retain_graph=True`，FA4 的 `(out,lse)` 元组要先取 `[0]`。
- **TFLOPs** = `flops() / m.mean / 1e12`；`flops()` 的核心是 `2(折MAC) × seqlen_q × avg_seqlen × (headdim+headdim_v)`，因果/滑窗用 `avg_seqlen` 折算有效 KV 长度；**MFU** 再除以 GPU 峰值。
- **带宽**靠 `bandwidth_fwd/bwd_bytes` 算 HBM 流量，判断算力/带宽受限；解码与短序列常带宽受限。
- **配置搜索** `sm90_config_search.py` 是**纯静态**的可行性枚举（不需 GPU）：用 GMMA 整除性、`REG_LIMITS`、`SMEM_LIMIT=224KB` 三道闸裁剪候选，按 `(-tile_n, smem_traffic_per_block)` 代理排序，只给名单不测真机。
- **回填**是离线工程闭环：搜索出候选 → 真机 benchmark 挑赢家 → 硬编码进 `_tile_size_fwd/bwd_sm90` 查找表；注释 `# benchmarked on H100 SXM` / `# RS is catastrophic (~300 vs ~600 TFLOPS)` 是真机结论覆盖理论直觉的明证。
- **留白与逃生口**：SM80 标注 `# should tune`（未精调）；用户可用 `tile_mn` 参数覆盖查找表，注入自定义 tile（换值触发重编译）。

## 7. 下一步学习建议

- 学完本讲，你已经能测、能搜、能读懂查找表。下一步建议：
  - 阅读 [u11-l3 测试体系与参考实现](u11-l3-tests-and-reference.md)，了解 `attention_ref`（也在 `bench_utils.py`）如何作为正确性标尺，与这里的性能基准形成「又快又对」的双维度。
  - 结合 [u11-l1 JIT 编译与缓存](u11-l1-jit-and-cache.md) 与 [u11-l2 Constexpr 特化](u11-l2-constexpr-specialization.md)，理解为什么「换 tile 触发重编译」——tile 进 `compile_key`，这正是基准里必须 warmup 的根因。
  - 若对调参感兴趣，可尝试为 SM80 路径（`# should tune`）做一次完整搜索→真机→回填，作为二次开发的练手项目。
  - 进阶可阅读 `hopper/tile_size.h`（FA3 C++ 源配置），对照 `_tile_size_fwd_sm90` 注释里 `# C++: ...` 的行，理解 FA4 相对 FA3 的配置迁移与差异。
