# 测试、基准与数值校验

## 1. 本讲目标

DeepGEMM 是一个跨 Python / C++ / CUDA PTX 三层、横跨 SM90 与 SM100 两代硬件的高性能张量核 kernel 库。每一个 kernel 都要回答两个问题：**「算得对不对」**和**「算得快不快」**。本讲不新增任何 kernel，而是拆解支撑全仓库的测试与基准工程脚手架——它们是把上万个编译期形状组合锁在正确与高性能轨道上的安全网。

学完本讲，你应该能够：

1. 看懂 `tests/generators.py` 如何用「枚举（enumerate）+ 生成（generate）」两段式，把任意形状/布局/量化配置参数化成带 FP32 黄金参考的测试输入。
2. 掌握 `deep_gemm/testing/bench.py` 中 `bench` 与 `bench_kineto` 两条测时路径的区别，理解为什么用 `torch.profiler`（kineto）而非 CPU 计时，并能手算 TFLOPS 与 GB/s。
3. 读懂 `deep_gemm/testing/numeric.py` 中 `calc_diff` 的相似度度量数学定义，理解它与余弦相似度的关系，以及为何不同精度有不同阈值。
4. 能独立为一个新形状组合编写一段「调用 kernel → 数值校验 → 打印 TFLOPS 与相对 cuBLASLt 加速比」的测试。

## 2. 前置知识

本讲是工程实践层，不引入新概念，但默认你已掌握前面讲义建立的心智模型。复习要点：

- **GEMM 与 NT 布局**（u2-l1）：D = C + A @ B，`fp8_fp4_gemm_nt` 中 A、B 均为 K-major（NT 布局），输入是 `(tensor, sf)` 元组、输出 `d` 需调用方预分配。
- **缩放因子 SF 与 recipe**（u2-l2）：FP8/FP4 范围窄，须逐块缩放，粒度由 `recipe=(gran_mn, gran_k)` 描述；SM90 用 FP32 SF、SM100 用打包 UE8M0。`per_token_cast_to_fp8` 把 BF16 张量连同 SF 一起量化成元组。
- **架构派发开关**（u2-l3、u4-l1）：`get_arch_major()` 返回 `9`(Hopper/SM90) 或 `10`(Blackwell/SM100)，决定走哪条 kernel 路径。
- **kernel_type**（u5-l1）：`1D1D` / `1D2D` / `NoSF` 三类，分别对应 A/B 的 TMA 加载盒不切分、切分、以及无缩放因子。

几个本讲要用到的通用术语：

- **黄金参考（golden reference）**：用更高精度（这里是 FP32）独立计算出的「标准答案」，用来和被测 kernel 的输出比对，以隔离「参考误差」与「kernel 误差」。
- **kineto**：PyTorch / CUDA 内置的性能分析后端，`torch.profiler` 即基于它，可逐 kernel 精确统计 GPU 执行时间。
- **L2 cache flush**：基准前用大块 memset 填满 L2 缓存，确保每次测量都从冷显存读取，得到稳定、可比的访存耗时。

## 3. 本讲源码地图

本讲涉及三个核心文件，外加一个把它们串起来的测试入口：

| 文件 | 职责 |
|------|------|
| [tests/generators.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py) | 测试输入与黄金参考的生成器。定义 `KernelType`/`MajorTypeAB`/`QuantConfig`，提供 `enumerate_*`（枚举形状）与 `generate_*`（构造张量+参考）两套函数。 |
| [deep_gemm/testing/bench.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py) | 基准测时。`bench`（CUDA 事件计时）与 `bench_kineto`（profiler 精确计时）两条路径。 |
| [deep_gemm/testing/numeric.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py) | 数值度量。`calc_diff`（相似度误差）、`count_bytes`（字节数统计）、`assert_bitwise_equal`（逐位比对）。 |
| [tests/test_fp8_fp4.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py) | FP8/FP4 GEMM 的测试主入口，把上面三者组装成「校验+测速」的标准范式。 |

配套地，`deep_gemm/testing/` 还包含 [utils.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/utils.py)（提供 `get_arch_major`、`ignore_env` 等小工具），全部通过 [__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/__init__.py) 以 `from .x import *` 统一对外暴露。整个 `tests/` 目录的覆盖范围则对应不同 kernel 家族：`test_bf16.py`、`test_attention.py`、`test_einsum.py`、`test_hyperconnection.py`、`test_layout.py`、`test_mega_moe.py`、`test_sanitizer.py` 等，本讲以 `test_fp8_fp4.py` 为代表讲解统一范式。

---

## 4. 核心概念与源码讲解

### 4.1 输入生成器：参数化枚举与黄金参考

#### 4.1.1 概念说明

DeepGEMM 的测试要覆盖「kernel 类型 × 量化配置 × 形状 (m,n,k) × 内存布局 × 是否累加 × 输出精度 × 是否 psum」的庞大组合空间。如果手写每一个 case，测试代码会被重复的样板淹没。`tests/generators.py` 的核心设计是**两段式分离**：

- **枚举阶段（`enumerate_*`）**：用 Python generator 把「要测哪些组合」表达成一串元组，每个元组描述一个 case 的全部参数，**不构造任何张量**。这样枚举逻辑（哪些形状有意义、架构差异）与数据构造逻辑解耦。
- **生成阶段（`generate_*`）**：接收一组参数，真正在 GPU 上分配张量、做量化、并用 FP32 计算黄金参考 `ref_d`。

把「测什么」和「怎么造数据」分开，是这套测试框架能以几百行覆盖上千个 case 的根本原因。

#### 4.1.2 核心流程

以稠密 GEMM 为例，一个 case 的生命周期是：

1. `enumerate_normal(dtype)` 产出一串元组 `(kernel_type, quant_config, m, n, k, major_a, major_b, accumulate, out_dtype)`。
2. 测试代码取出这些参数，调用 `generate_normal(...)` 得到 `a, b, c, d, ref_d`（`a`、`b` 是量化后的 `(tensor, sf)` 元组，`ref_d` 是 FP32 黄金参考）。
3. 调用被测 kernel 把结果写进预分配的 `d`。
4. 用 `calc_diff(d, ref_d)` 比对、断言。

其中架构差异被封装进枚举：例如 SM90 仅支持 K-major、只有 1D2D kernel，SM100 支持 MN-major、用 1D1D——这些判断都藏在枚举函数里，调用方拿到的就是合法组合。

#### 4.1.3 源码精读

先看三个驱动的枚举类型。`KernelType` 区分三类 kernel 结构（[tests/generators.py:17-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L17-L29)）：

```python
class KernelType(enum.Enum):
    Kernel1D1D = 0
    Kernel1D2D = 1
    KernelNoSF = 2
```

`MajorTypeAB` 标记 A/B 的主维（[tests/generators.py:32-41](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L32-L41)）：

```python
class MajorTypeAB(enum.Enum):
    KMajor = 0
    MNMajor = 1
```

最关键的是 `QuantConfig`，它封装量化粒度与精度，并直接决定后面 `calc_diff` 用的误差阈值（[tests/generators.py:43-79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L43-L79)）。注意 `max_diff()` 按精度分级（[tests/generators.py:65-70](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L65-L70)）：

```python
def max_diff(self) -> float:
    if self.is_fp4_a and self.is_fp4_b:
        return 0.02
    if self.is_fp4_a or self.is_fp4_b:
        return 0.01
    return 0.001
```

这里埋下一条贯穿全讲的主线：**误差阈值随精度下降而放宽**——FP8×FP8 用 `0.001`，混入一个 FP4 放宽到 `0.01`，FP4×FP4 放宽到 `0.02`。这是因为低精度表示更粗糙，可接受误差更大。第 4.3 节会与 `calc_diff` 一起再讲它怎么用。

枚举阶段的核心是 `enumerate_normal`，它在「kernel_type × quant_config × 形状 × 前向/反向」上做笛卡尔积，并根据 `get_arch_major()` 注入架构特化（[tests/generators.py:115-154](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L115-L154)）。注意前向 / 反向（Dgrad / Wgrad）会派生不同形状与布局：

```python
# Backward
for m in m_bwd_list:
    for n, k in nk_list:
        override_major = MajorTypeAB.MNMajor
        override_kernel_type = kernel_type
        if get_arch_major() == 9 and dtype == torch.float8_e4m3fn:
            override_major = MajorTypeAB.KMajor          # SM90 FP8 反向仍需 K-major
            override_kernel_type = KernelType.Kernel1D1D
        yield kernel_type,          quant_config, m, k, n, ..., False, torch.bfloat16   # Dgrad
        yield override_kernel_type, quant_config, n, m, k, ..., True,  torch.float       # Wgrad(累加)
```

生成阶段的核心是 `generate_normal`，它先在 GPU 上造 BF16 随机张量，**用 FP32 算出黄金参考 `ref_d`**，再把 A、B 量化成 FP8/FP4 元组（[tests/generators.py:301-324](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L301-L324)）：

```python
a = torch.randn((m, k), device='cuda', dtype=torch.bfloat16)
b = torch.randn((n, k), device='cuda', dtype=torch.bfloat16)
d = torch.randn((m, n), device='cuda', dtype=out_dtype) * 32 if accumulate else \
    torch.empty((m, n), device='cuda', dtype=out_dtype)
c = d if accumulate else None
ref_d = (a.float() @ b.float().t() + (c if accumulate else 0)).to(out_dtype)
...
a = cast_fp8_fp4_with_major(a, major_a, quant_config.gran_k_a, quant_config.is_fp4_a, use_ue8m0)
b = cast_fp8_fp4_with_major(b, major_b, quant_config.gran_k_b, quant_config.is_fp4_b, use_ue8m0, ...)
return a, b, c, d, ref_d
```

两个要点：其一，`ref_d` 用 `a.float() @ b.float().t()` 在 **FP32** 下计算——这是黄金参考之所以「黄金」的原因，它把参考实现本身的误差降到几乎为零，这样 `calc_diff(d, ref_d)` 衡量的就纯粹是「被测 kernel 相对理想浮点」的误差；其二，累加模式（`accumulate=True`）下 `c` 与 `d` 同址（`c = d`），呼应 u2-l3 讲过的 early_return「C/D 同址」约定。

量化封装在 `cast_fp8_fp4_with_major` 里（[tests/generators.py:269-277](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L269-L277)），它根据 `is_fp4` 选 `per_token_cast_to_fp4` 或 `per_token_cast_to_fp8`，并在 MN-major 时对张量做转置。分组版 `generate_m_grouped_contiguous`（[tests/generators.py:327-366](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L327-L366)）与之同构，额外构造 `grouped_layout` 标记每段 expert 归属（参见 u7-l1）。

#### 4.1.4 代码实践

**实践目标**：理解枚举与生成分离，亲手跑通「枚举一个 case → 生成数据 → 打印形状」。

**操作步骤**：

1. 在仓库根目录，确认已按 u1-l2 完成 `develop.sh` 本地构建，能 `import deep_gemm`。
2. 写一个最小脚本 `tmp_inspect.py`（注意 `test_fp8_fp4.py` 用的是 `from generators import ...`，故需保证 `generators.py` 可被导入，可设 `PYTHONPATH` 指向 `tests/`）：

```python
# 示例代码：仅供阅读理解，未在所有架构上运行
import torch
from generators import enumerate_normal, generate_normal, get_ue8m0_usage

dtype = torch.float8_e4m3fn
cases = list(enumerate_normal(dtype))
print(f'enumerate_normal 共产出 {len(cases)} 个 case')

# 取第一个 case，生成数据
kt, qc, m, n, k, ma, mb, acc, out_dt = cases[0]
use_ue8m0 = get_ue8m0_usage(kt)
a, b, c, d, ref_d = generate_normal(m, n, k, ma, mb, acc, out_dt, kt, use_ue8m0=use_ue8m0, quant_config=qc)
print(f'shape: m={m}, n={n}, k={k}, a={a[0].shape}/{a[0].dtype}, sf_a={a[1].shape}, ref_d={ref_d.shape}')
print(f'该 case 的 max_diff 阈值 = {qc.max_diff()}')
```

3. 在装有 SM90/SM100 GPU 的机器上运行（需把 `tests/` 加入 `PYTHONPATH`）。

**需要观察的现象**：打印出的 case 总数会随架构不同而不同（SM100 因支持更多布局与量化配置，case 更多）；`a` 是 FP8 张量、`a[1]` 是其 SF、`ref_d` 是 BF16/FP32。

**预期结果**：能正确打印形状与阈值。具体 case 数量与设备型号相关——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate_normal` 要用 `a.float() @ b.float().t()` 而不是直接用 BF16 算 `ref_d`？

**答案**：用 FP32 计算黄金参考，把参考实现自身的舍入误差降到几乎为零。若用 BF16 算参考，参考本身就带 BF16 误差，`calc_diff` 测出的就分不清「是 kernel 错了」还是「参考本身就糙」。

**练习 2**：`QuantConfig.max_diff()` 对 FP8×FP8 返回 `0.001`，对 FP4×FP4 返回 `0.02`，为何放宽 20 倍？

**答案**：FP4（e2m1，仅 2 位指数 + 1 位尾数）的表示粒度远粗于 FP8（e4m3），量化误差天然更大，必须放宽阈值，否则合法的 FP4 kernel 会被误判为失败。

---

### 4.2 kineto 基准：用 profiler 精确测时

#### 4.2.1 概念说明

测 GPU kernel 耗时，最朴素（也最不准）的办法是用 Python 的 `time.time()` 或 `time.perf_counter()` 在调用前后取差。但这测的是 **CPU 墙钟时间**，会被 kernel launch 开销、CPU 调度抖动严重污染——尤其当 kernel 本身只跑几十微秒时，launch 开销可能占比过半。

DeepGEMM 在 [deep_gemm/testing/bench.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py) 提供两条更可靠的路径：

- **`bench`**：用 CUDA Event（`torch.cuda.Event(enable_timing=True)`）计时，测的是 GPU 侧一段执行区间，已排除部分 launch 抖动，适合粗粒度测时。
- **`bench_kineto`**：用 `torch.profiler`（kineto 后端）逐 kernel 统计 GPU 时间，能**按 kernel 名精确隔离**单个 kernel 的耗时，是 DeepGEMM 默认的基准方法。

仓库里所有 `test_*.py` 打印 TFLOPS / GB/s 用的都是 `bench_kineto`。

#### 4.2.2 核心流程

`bench_kineto(fn, kernel_names, ...)` 的执行流程：

1. **跳过开关**：若设了 `DG_USE_NVIDIA_TOOLS=1`（要与 Nsight / compute-sanitizer 同时跑），直接返回占位值，避免 profiler 冲突（[bench.py:89-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L89-L90)）。
2. **预热**：先调一次 `fn()`，触发 JIT 编译并热身（[bench.py:96](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L96)）。
3. **profiler 采样**：用 `wait=0, warmup=1, active=1` 的 schedule，在 active 区间内重复跑 `num_tests` 次 `fn()`，每次前用 8GB memset 刷 L2（[bench.py:101-116](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L101-L116)）。
4. **解析表格**：把 profiler 的 `key_averages().table(...)` 按行切分，对每个 `kernel_names` 子串匹配，从匹配行解析「总时间 × 调用次数」算平均（[bench.py:119-144](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L119-L144)）。
5. **返回**：平均单次 kernel 时间，单位**秒**。

拿到时间 `t`（秒）后，调用方自己算吞吐：

- **TFLOPS**：\(\text{TFLOPS} = \dfrac{2 \cdot m \cdot n \cdot k}{t \cdot 10^{12}}\)。分子乘 2，因为每个乘加算 2 个浮点操作（一次乘 + 一次加）。
- **GB/s**：\(\text{GB/s} = \dfrac{\text{count\_bytes}(a, b, d)}{t \cdot 10^{9}}\)，即读 A、读 B、写 D 的总字节数除以时间。

#### 4.2.3 源码精读

先看简单的 `bench`，理解 CUDA Event 计时的最小骨架（[bench.py:7-33](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L7-L33)）：

```python
def bench(fn, num_warmups: int = 5, num_tests: int = 10, high_precision: bool = False):
    torch.cuda.synchronize()
    cache = torch.empty(int(256e6 // 4), dtype=torch.int, device='cuda')
    cache.zero_()                      # 刷 L2（256MB）
    for _ in range(num_warmups):
        fn()                           # 预热
    ...
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    start_event.record()
    for i in range(num_tests):
        fn()
    end_event.record()
    torch.cuda.synchronize()
    return start_event.elapsed_time(end_event) / num_tests / 1e3   # 返回秒
```

关键点：`start_event.record()` 与 `end_event.record()` 在 GPU 流上插标记，`elapsed_time` 给出两者间 GPU 侧的真实时间；最后除以 `1e3` 把毫秒换算成秒。

再看 `bench_kineto` 的 profiler 采样段（[bench.py:99-116](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L99-L116)）：

```python
schedule = torch.profiler.schedule(wait=0, warmup=1, active=1, repeat=1)
profiler = torch.profiler.profile(
    activities=[torch.profiler.ProfilerActivity.CUDA], schedule=schedule, acc_events=True)
with profiler:
    for i in range(2):
        for _ in range(num_tests):
            if flush_l2:
                torch.empty(flush_l2_size, dtype=torch.int, device='cuda').zero_()  # 刷 L2（8GB）
            if barrier is not None:
                torch.cuda._sleep(int(2e7))   # ~10ms，压住 CPU 抖动
                barrier()
            fn()
        torch.cuda.synchronize()
        profiler.step()
```

注意 `wait=0, warmup=1, active=1`：kineto 只统计 `active=1` 那一段，前面 `warmup=1` 的数据被丢弃，从而排除冷启动。`flush_l2_size = int(8e9 // 4)`（[bench.py:93](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L93)）是 8GB 的 int 数组，远大于任何 L2，确保每次 `fn()` 都从冷显存起跑。

解析表格的逻辑把 profiler 文本表按 kernel 名子串匹配，并支持 `ms`/`us` 两种单位换算（[bench.py:130-144](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L130-L144)）：

```python
units = {'ms': 1e3, 'us': 1e6}
for name in kernel_names:
    total_time = 0
    total_num = 0
    for line in prof_lines:
        if name in line:
            time_str = line.split()[-2]
            num_str = line.split()[-1]
            for unit, scale in units.items():
                if unit in time_str:
                    total_time += float(time_str.replace(unit, '')) / scale * int(num_str)
                    total_num += int(num_str)
                    break
    kernel_times.append(total_time / total_num if total_num > 0 else 0)
```

`bench_kineto` 既接受单个 `kernel_names` 字符串，也接受元组——后者用于把一个逻辑操作拆成多个物理 kernel 求和。`test_fp8_fp4.py` 测 cuBLASLt 基准时就用了元组 `('nvjet', 'reduce')`（见下文 4.2.4）。

`bench_kineto` 返回的 `t` 是秒，吞吐换算由调用方完成。`test_fp8_fp4.py` 的换算一行写尽（[tests/test_fp8_fp4.py:62-65](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L62-L65)）：

```python
print(f' > Perf (...): '
      f'{t * 1e6:6.1f} us | {2 * m * n * k / t / 1e12:4.0f} TFLOPS | '
      f'{(count_bytes(a, b, d) + count_bytes(c) * int(accumulate)) / 1e9 / t:4.0f} GB/s | '
      f'{(cublas_t + split_k_t) / t:.2f}x cuBLAS')
```

这正是 4.2.2 给出的两个公式的代码体现。`count_bytes(c) * int(accumulate)` 表示仅累加模式才计读 C 的字节。

#### 4.2.4 代码实践

**实践目标**：用 `bench_kineto` 测一次 FP8 GEMM，并手算 TFLOPS 与理论峰值对比。

**操作步骤**：

1. 复用 4.1.4 生成的 `a, b, d`，写脚本（**示例代码**）：

```python
# 示例代码
import deep_gemm
from deep_gemm.testing import bench_kineto, count_bytes
from generators import generate_normal, KernelType, MajorTypeAB, get_ue8m0_usage
import torch

m, n, k = 4096, 7168, 7168
use_ue8m0 = get_ue8m0_usage(KernelType.Kernel1D1D)
a, b, c, d, ref_d = generate_normal(m, n, k, MajorTypeAB.KMajor, MajorTypeAB.KMajor,
                                    False, torch.bfloat16, KernelType.Kernel1D1D, use_ue8m0=use_ue8m0)

t = bench_kineto(lambda: deep_gemm.fp8_fp4_gemm_nt(a, b, d, c=c, disable_ue8m0_cast=not use_ue8m0),
                 'gemm_', suppress_kineto_output=True)
tflops = 2 * m * n * k / t / 1e12
gbs = count_bytes(a, b, d) / 1e9 / t
print(f'{t*1e6:.1f} us | {tflops:.0f} TFLOPS | {gbs:.0f} GB/s')
```

2. 对照你的 GPU 的 FP8 理论峰值（如 Hopper SM90 约 1979 TFLOPS FP8、Blackwell 更高），算实测占峰值比例。

**需要观察的现象**：`bench_kineto` 第一次调用会比后续慢（首次触发 JIT 编译），但 `bench_kineto` 内部已调一次 `fn()` 做预热，所以返回值是稳定后的耗时。

**预期结果**：大形状（如 4096×7168×7168）应达到峰值 TFLOPS 的较高比例。**具体数值待本地验证**——取决于 GPU 型号与 SXM/PCIe 版本。

#### 4.2.5 小练习与答案

**练习 1**：`bench_kineto` 为什么在每次 `fn()` 前都要 `torch.empty(flush_l2_size).zero_()`？

**答案**：用 8GB memset 填满并冲刷 L2 缓存，确保被测 kernel 每次都从冷 HBM 读取数据。否则首次运行后 A、B 留在 L2 里，后续运行访存耗时被低估，测出的 GB/s 失真。

**练习 2**：`bench_kineto(lambda: deep_gemm.cublaslt_gemm_nt(...), ('nvjet', 'reduce'))` 为什么要传两个 kernel 名相加？

**答案**：cuBLASLt 的 FP8 GEMM 在大 K 时会拆成「主计算 kernel（`nvjet`）+ split-K 归约 kernel（`reduce`）」两个物理 kernel 启动。只统计 `nvjet` 会漏掉归约开销，所以要把两者时间相加 `cublas_t + split_k_t` 才是 cuBLASLt 的公平总耗时。

---

### 4.3 数值误差度量：calc_diff 与阈值判定

#### 4.3.1 概念说明

测完正确性（断言）和速度（基准），还要有一个统一的「数值接近度」度量，把两个张量的差异压成一个标量，便于跨 case 比较、设阈值。最常见的两个选择是**最大绝对误差（max_abs_diff）**和**相对/相似度误差**。DeepGEMM 用的是后者，实现在 `calc_diff`。

`calc_diff` 不是简单的「1 − 余弦相似度」。它返回的是 1 减去一个**归一化内积相似度**，其分母是两向量平方和之和、而非范数乘积（后者才是余弦）。这种度量对整体能量（scale）敏感，比纯余弦更严格——余弦相似度对整体放缩不敏感，而 GEMM 输出的整体幅度正是我们要校验的。

#### 4.3.2 核心流程

对两个张量 `x`、`y`（先转成 `double` 提高度量精度），`calc_diff` 计算：

\[
\text{diff} = 1 - \frac{2 \sum_i x_i y_i}{\sum_i x_i^2 + \sum_i y_i^2}
\]

几个性质：

- **自比为零**：当 `x == y` 时，\(\frac{2\sum x_i^2}{2\sum x_i^2} = 1\)，故 `diff = 0`。
- **同号主导时非负**：由不等式 \(2ab \le a^2 + b^2\) 可得 \(2\sum x_i y_i \le \sum x_i^2 + \sum_i y_i^2\)（当 \(x_i, y_i\) 同号时），此时分数 ≤ 1，`diff ≥ 0`。GEMM 输出若有正有负、误差较大时分数可能偏离，但正常 kernel 下 `diff` 是很小的正数。
- **与余弦的关系**：余弦相似度为 \(\frac{\sum x_i y_i}{\|x\|\|y\|}\)。当 \(\|x\| = \|y\|\) 时，本式分母 \(\|x\|^2 + \|y\|^2 = 2\|x\|\|y\|\)，二者一致；否则本式更接近 Sørensen–Dice 系数。
- **全零特判**：分母为 0（两张量全零）时直接返回 `0.0`，避免除零。

阈值判定就是把这个 `diff` 与 `QuantConfig.max_diff()`（0.001 / 0.01 / 0.02）比较，`diff < max_diff` 即通过。

#### 4.3.3 源码精读

`calc_diff` 的实现极简却精准（[deep_gemm/testing/numeric.py:5-11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py#L5-L11)）：

```python
def calc_diff(x: torch.Tensor, y: torch.Tensor):
    x, y = x.double(), y.double()
    denominator = (x * x + y * y).sum()
    if denominator == 0:    # Which means that all elements in x and y are 0
        return 0.0
    sim = 2 * (x * y).sum() / denominator
    return 1 - sim
```

第一步 `x.double()` 是关键：把 BF16/FP32 输出提升到 FP64 再算度量，避免度量本身在低精度下舍入失真——与 4.1 节「参考用 FP32」是同一思路的延伸。

`count_bytes` 用递归把任意嵌套的 `(tensor, sf)` 元组展开统计字节数，是 4.2 节 GB/s 公式的配套工具（[deep_gemm/testing/numeric.py:14-21](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py#L14-L21)）：

```python
def count_bytes(*tensors):
    total = 0
    for t in tensors:
        if isinstance(t, (tuple, list)):
            total += count_bytes(*t)        # 递归展开 (tensor, sf) 元组
        elif t is not None:
            total += t.numel() * t.element_size()
    return total
```

这正是 `count_bytes(a, b, d)` 能自动把 `a=(fp8_tensor, sf)` 的张量和 SF 都计入的原因——`a[0]` 是 FP8（1 字节）、`a[1]` 是 SF（SM90 下 FP32，4 字节）。

回到阈值判定的真实用法。`test_fp8_fp4.py` 的 `test_gemm` 在跑完 kernel 后做断言（[tests/test_fp8_fp4.py:53-55](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L53-L55)）：

```python
diff = calc_diff(d, ref_d)
assert diff < quant_config.max_diff(), (f'{m=}, {n=}, {k=}, {kernel_opt}, {major_opt=}, ...'
                                        f'{diff:.5f}, alias={test_alias}')
```

注意断言失败信息里打印了 `diff` 与全部参数——这是定位「哪个 case 挂了」的关键，因为一个 `test_gemm` 要跑成百上千个 case。同一函数末尾的 cuBLASLt 对比与 TFLOPS 打印见 4.2.3 引用过的 [tests/test_fp8_fp4.py:58-68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L58-L68)，那里 `(cublas_t + split_k_t) / t` 就是相对 cuBLASLt 的加速比，并最终用几何平均汇总（[tests/test_fp8_fp4.py:68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L68)）：

```python
print(f"Average FP8xFP8 GEMM speedup over cuBLASLt: {float(np.prod(scores)) ** (1.0 / len(scores)):.3f}x\n")
```

几何平均（而非算术平均）用于汇总加速比，是因为各 case 加速比是乘性关系，几何平均才不受个别极端值主导。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `calc_diff` 的数学性质，理解阈值分级。

**操作步骤**：写一段纯 CPU 的 Python（**示例代码**，无需 GPU）：

```python
# 示例代码
import torch
from deep_gemm.testing.numeric import calc_diff

x = torch.randn(4096, 7168)
y = x.clone()
print('自比 (应≈0):', calc_diff(x, y))

y_noisy = x + 0.01 * torch.randn_like(x)        # 加 1% 噪声
print('1% 噪声:', calc_diff(x, y_noisy))
```

**需要观察的现象**：自比应得到接近 `0`（浮点下可能为 `1e-16` 量级）；1% 噪声应给出一个小正数。

**预期结果**：自比 ≈ 0；1% 噪声的 `diff` 通常在 `1e-4 ~ 1e-5` 量级，远小于 FP8 的 `0.001` 阈值。可改成 5% 噪声观察 `diff` 上升，体会它与误差幅度的单调关系。**具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`calc_diff` 第一步 `x, y = x.double()` 有什么作用？去掉会怎样？

**答案**：把输出提升到 FP64 再算度量，避免度量公式本身在 BF16/FP32 下舍入失真。若在 BF16 下直接算 `x * x`、求和，度量本身的误差可能与被测误差同量级，使 `diff` 不可信。这与黄金参考用 FP32 计算是同一原则。

**练习 2**：假设某 FP4×FP4 case 的 `calc_diff` 返回 `0.015`，它是否应通过测试？为什么 `max_diff` 对它放宽到 `0.02`？

**答案**：`0.015 < 0.02`，通过。放宽是因为 FP4 表示粒度极粗（2 位指数 + 1 位尾数），逐块量化误差天然比 FP8 大一个量级，若仍用 `0.001` 会把合法 FP4 kernel 误判为失败。

**练习 3**：为什么汇总各 case 的 cuBLASLt 加速比用几何平均而非算术平均？

**答案**：加速比是「快几倍」的乘性量（2x + 2x 的综合不是 3x 而是 2x），几何平均对乘性比率是无偏的；算术平均会被个别超大加速比拉高，不能反映「典型」表现。

---

## 5. 综合实践

把三个最小模块串起来，完成一个**完整的新形状测试**：调用 `fp8_fp4_gemm_nt`，做数值校验，并打印 TFLOPS 与相对 cuBLASLt 的加速比。

**任务**：选一个 `enumerate_normal` 未覆盖的自定义形状（例如 `m=2048, n=4096, k=8192`），写一段脚本，复用 `generate_normal` / `calc_diff` / `bench_kineto` / `count_bytes`，输出一行性能摘要，并断言数值正确。

**参考实现**（**示例代码**，需 SM90/SM100 GPU 与本地构建）：

```python
# my_shape_test.py —— 运行前：export PYTHONPATH=$(pwd)/tests  以便 import generators
import torch
import deep_gemm
from deep_gemm.testing import bench_kineto, calc_diff, count_bytes
from generators import (
    generate_normal, KernelType, QuantConfig, MajorTypeAB, get_ue8m0_usage
)

torch.manual_seed(0)

# 1) 自定义形状与配置
m, n, k = 2048, 4096, 8192
out_dtype = torch.bfloat16
accumulate = False
major_a = major_b = MajorTypeAB.KMajor
kernel_type = KernelType.Kernel1D1D
use_ue8m0 = get_ue8m0_usage(kernel_type)
quant_config = QuantConfig()                     # 默认 FP8×FP8，gran_k=128
disable_ue8m0_cast = not use_ue8m0

# 2) 生成输入与 FP32 黄金参考
a, b, c, d, ref_d = generate_normal(
    m, n, k, major_a, major_b, accumulate, out_dtype, kernel_type,
    use_ue8m0=use_ue8m0, quant_config=quant_config)

# 3) 调用被测 kernel
deep_gemm.fp8_fp4_gemm_nt(a, b, d, c=c,
                          disable_ue8m0_cast=disable_ue8m0_cast)

# 4) 数值校验
diff = calc_diff(d, ref_d)
assert diff < quant_config.max_diff(), f'{diff=}, 阈值={quant_config.max_diff()}'
print(f'数值校验通过: calc_diff={diff:.5f} (阈值 {quant_config.max_diff()})')

# 5) 测速：被测 kernel
t = bench_kineto(
    lambda: deep_gemm.fp8_fp4_gemm_nt(a, b, d, c=c, disable_ue8m0_cast=disable_ue8m0_cast),
    'gemm_', suppress_kineto_output=True)

# 6) 测速：cuBLASLt 基准（FP8×FP8 才有可比基线）
cublas_t, split_k_t = (0.0, 0.0)
if not quant_config.is_fp4_a and not quant_config.is_fp4_b:
    cublas_t, split_k_t = bench_kineto(
        lambda: deep_gemm.cublaslt_gemm_nt(a[0], b[0], d, c=c),
        ('nvjet', 'reduce'), suppress_kineto_output=True)

# 7) 汇总打印
tflops = 2 * m * n * k / t / 1e12
gbs = (count_bytes(a, b, d) + count_bytes(c) * int(accumulate)) / 1e9 / t
speedup = (cublas_t + split_k_t) / t if cublas_t > 0 else float('nan')
print(f'{t*1e6:6.1f} us | {tflops:4.0f} TFLOPS | {gbs:4.0f} GB/s | {speedup:.2f}x cuBLAS')
```

**验证要点**：

1. 数值校验应通过（`diff < 0.001`）。
2. TFLOPS 应达到该 GPU FP8 峰值的较高比例。
3. 加速比 `speedup` 反映 DeepGEMM 相对 cuBLASLt 的优势（通常 ≥ 1.0）。
4. 改 `quant_config` 为 `QuantConfig((128, 32, False, True))`（SM100 FP8×FP4）观察阈值放宽到 `0.01`、且 cuBLASLt 基线变为 0（cuBLASLt 不支持该路径）。

**注意**：上述脚本未在所有架构运行，TFLOPS/加速比**待本地验证**；cuBLASLt 的 kernel 名 `nvjet`/`reduce` 与驱动版本相关，若解析失败需检查 profiler 表格输出。

## 6. 本讲小结

- DeepGEMM 的测试工程以「**枚举（enumerate）+ 生成（generate）+ 校验（calc_diff）+ 测速（bench_kineto）**」四段式范式贯穿全部 `test_*.py`，`tests/generators.py` 是这套范式的数据源头。
- **黄金参考用 FP32 计算**（`a.float() @ b.float().t()`），把参考误差降到零，使 `calc_diff` 纯粹度量被测 kernel 的误差；度量自身又用 `double()` 提精度。
- **`bench_kineto` 用 `torch.profiler` 逐 kernel 计时**，靠 8GB L2 刷除与 `wait/warmup/active` schedule 排除冷启动与缓存效应，返回秒；调用方按 \(2mnk/t/10^{12}\) 算 TFLOPS、按字节数算 GB/s。
- **`calc_diff` 是 1 减归一化内积相似度**（分母为平方和之和，非范数乘积），比纯余弦更严格、对整体幅度敏感；阈值按精度分级（FP8×FP8 `0.001` / 混精 `0.01` / FP4×FP4 `0.02`）。
- **cuBLASLt 基准**用元组 `('nvjet', 'reduce')` 把主 kernel 与 split-K 归约相加得到公平总耗时，加速比跨 case 用**几何平均**汇总。
- 这套脚手架是把 DeepGEMM 海量编译期形状组合钉死在「正确 + 高性能」上的安全网，也是你为新 kernel 写测试时应遵循的模板。

## 7. 下一步学习建议

- **回到调试与剖析层**：本讲的 `bench_kineto` 只给耗时数字，下一讲 [u10-l4 环境变量、调试与性能剖析](u10-l4-env-vars-debug-profiling.md) 会用 `DG_JIT_DUMP_SASS`/`DG_JIT_WITH_LINEINFO` 配合 NCU 把耗时定位到具体指令，建议衔接阅读。
- **为其它 kernel 家族写测试**：参照本讲范式阅读 [tests/test_attention.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py)、[tests/test_mega_moe.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py)，体会 `generators.py` 如何为不同 kernel 提供专属的 `enumerate_*`/`generate_*`（如 `generate_m_grouped_masked`、`generate_k_grouped_contiguous_psum`）。
- **进阶数值校验**：`numeric.py` 还提供 `assert_bitwise_equal`（[numeric.py:24-44](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py#L24-L44)），用于 SF 布局变换等需要逐位一致的场景，可结合 [tests/test_layout.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_layout.py) 学习。
- **真机实操**：在本机或容器跑一遍 `cd tests && python test_fp8_fp4.py`，对照本讲理解每一行打印的来源，是巩固本讲最快的路径。
